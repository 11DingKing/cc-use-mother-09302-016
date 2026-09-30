"""师徒计划核心业务规则测试。"""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mentorship import (
    CapacityExhausted,
    Database,
    GuardianRequired,
    InvalidState,
    MentorshipService,
    NotFound,
    ValidationError,
)


def birth_for_age(age: int) -> str:
    today = date.today()
    return date(today.year - age, today.month, min(today.day, 28)).isoformat()


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.svc = MentorshipService(self.db)
        self._seq = 0

    def tearDown(self) -> None:
        self.db.close()

    def _plan(self, goal_count: int = 2, publish: bool = True) -> dict:
        self._seq += 1
        plan = self.svc.create_plan_version(f"P{self._seq:03d}", f"第{self._seq}期计划")
        for i in range(1, goal_count + 1):
            self.svc.add_stage_goal(plan["id"], i, f"阶段{i}", detail=f"目标{i}", required_evidence=1)
        return self.svc.publish_plan_version(plan["id"]) if publish else plan

    def _mentor(self, capacity: int = 1, **kwargs) -> dict:
        return self.svc.create_mentor("张师傅", capacity_total=capacity, **kwargs)

    def _student(self, age: int = 20, name: str = "学生甲") -> dict:
        return self.svc.create_student(name, birth_for_age(age))

    # ---- 计划版本 ----
    def test_plan_version_lifecycle(self) -> None:
        plan = self._plan(goal_count=0, publish=False)
        with self.assertRaises(InvalidState):
            self.svc.publish_plan_version(plan["id"])  # 无目标不能发布
        self.svc.add_stage_goal(plan["id"], 1, "基本功")
        with self.assertRaises(ValidationError):
            self.svc.add_stage_goal(plan["id"], 1, "重复序号")
        published = self.svc.publish_plan_version(plan["id"])
        self.assertEqual(published["status"], "published")
        with self.assertRaises(InvalidState):
            self.svc.add_stage_goal(plan["id"], 2, "已发布不能再改")
        archived = self.svc.archive_plan_version(plan["id"])
        self.assertEqual(archived["status"], "archived")

    # ---- 录取：原子占用名额 + 固定当期目标 ----
    def test_enroll_occupies_slot_and_snapshots_goals(self) -> None:
        plan = self._plan(2)
        mentor = self._mentor(capacity=1)
        student = self._student()
        enrollment = self.svc.enroll(student["id"], mentor["id"], plan["id"])
        self.assertEqual(enrollment["status"], "active")
        self.assertEqual([g["seq"] for g in enrollment["goal_snapshots"]], [1, 2])
        self.assertEqual(enrollment["goal_snapshots"][0]["title"], "阶段1")
        capacity = self.svc.get_mentor_capacity(mentor["id"])
        self.assertEqual((capacity["capacity_used"], capacity["remaining"]), (1, 0))

    def test_enroll_snapshot_isolated_from_plan_changes(self) -> None:
        plan_v1 = self._plan(1)
        mentor = self._mentor(capacity=2)
        student = self._student()
        enrollment = self.svc.enroll(student["id"], mentor["id"], plan_v1["id"])
        # 新学期改版：旧生的目标快照不受影响
        plan_v2 = self._plan(3)
        self.assertNotEqual(plan_v1["id"], plan_v2["id"])
        snapshot = self.svc.get_enrollment(enrollment["id"])["goal_snapshots"]
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["title"], "阶段1")

    def test_enroll_rejects_over_capacity_and_unpublished(self) -> None:
        plan = self._plan()
        mentor = self._mentor(capacity=1)
        self.svc.enroll(self._student()["id"], mentor["id"], plan["id"])
        with self.assertRaises(CapacityExhausted):
            self.svc.enroll(self._student(name="学生乙")["id"], mentor["id"], plan["id"])
        draft = self._plan(publish=False)
        with self.assertRaises(InvalidState):
            self.svc.enroll(self._student(name="学生丙")["id"], mentor["id"], draft["id"])

    def test_enroll_rejects_duplicate_active_enrollment(self) -> None:
        plan = self._plan()
        mentor = self._mentor(capacity=3)
        student = self._student()
        self.svc.enroll(student["id"], mentor["id"], plan["id"])
        with self.assertRaises(InvalidState):
            self.svc.enroll(student["id"], mentor["id"], plan["id"])

    # ---- 导师资格 ----
    def test_mentor_qualification_and_status_gate_enrollment(self) -> None:
        plan = self._plan()
        expired = self._mentor(qualified_until=(date.today() - timedelta(days=1)).isoformat())
        with self.assertRaises(InvalidState):
            self.svc.enroll(self._student()["id"], expired["id"], plan["id"])
        suspended = self._mentor()
        self.svc.suspend_mentor(suspended["id"], "暂停带教")
        with self.assertRaises(InvalidState):
            self.svc.enroll(self._student(name="乙")["id"], suspended["id"], plan["id"])
        self.svc.reinstate_mentor(suspended["id"])
        enrollment = self.svc.enroll(self._student(name="丙")["id"], suspended["id"], plan["id"])
        self.assertEqual(enrollment["status"], "active")

    # ---- 未成年人监护授权 ----
    def test_minor_enroll_requires_guardian_authorization(self) -> None:
        plan = self._plan()
        mentor = self._mentor(capacity=2)
        minor = self._student(age=10, name="小明")
        with self.assertRaises(GuardianRequired):
            self.svc.enroll(minor["id"], mentor["id"], plan["id"])
        auth = self.svc.add_guardian_authorization(minor["id"], "父亲", scope="all")
        enrollment = self.svc.enroll(minor["id"], mentor["id"], plan["id"])
        self.assertEqual(enrollment["status"], "active")
        # 授权撤销后再次需要
        self.svc.revoke_guardian_authorization(auth["id"])
        minor2 = self._student(age=11, name="小红")
        with self.assertRaises(GuardianRequired):
            self.svc.enroll(minor2["id"], mentor["id"], plan["id"])

    def test_expired_or_scoped_authorization_not_accepted(self) -> None:
        plan = self._plan()
        mentor = self._mentor(capacity=2)
        minor = self._student(age=9)
        self.svc.add_guardian_authorization(
            minor["id"], "母亲", scope="evidence",
            valid_from="2020-01-01",
            valid_to=(date.today() - timedelta(days=1)).isoformat())  # 已过期
        self.svc.add_guardian_authorization(minor["id"], "母亲", scope="view")  # 范围不符
        with self.assertRaises(GuardianRequired):
            self.svc.enroll(minor["id"], mentor["id"], plan["id"])

    def test_minor_evidence_requires_named_guardian(self) -> None:
        plan = self._plan()
        mentor = self._mentor(capacity=1)
        minor = self._student(age=10)
        auth = self.svc.add_guardian_authorization(minor["id"], "父亲", scope="all")
        enrollment = self.svc.enroll(minor["id"], mentor["id"], plan["id"])
        with self.assertRaises(GuardianRequired):
            self.svc.submit_evidence(enrollment["id"], 1, "uri://work1")
        with self.assertRaises(GuardianRequired):
            self.svc.submit_evidence(enrollment["id"], 1, "uri://work1", guardian="陌生人")
        evidence = self.svc.submit_evidence(enrollment["id"], 1, "uri://work1", guardian="父亲")
        self.assertEqual(evidence["goal_snapshot_id"],
                         enrollment["goal_snapshots"][0]["id"])
        self.svc.revoke_guardian_authorization(auth["id"])
        with self.assertRaises(GuardianRequired):
            self.svc.submit_evidence(enrollment["id"], 1, "uri://work2", guardian="父亲")

    def test_minor_portfolio_requires_guardian(self) -> None:
        plan = self._plan()
        mentor = self._mentor(capacity=2)
        minor = self._student(age=10)
        self.svc.add_guardian_authorization(minor["id"], "母亲", scope="all")
        self.svc.enroll(minor["id"], mentor["id"], plan["id"])
        with self.assertRaises(GuardianRequired):
            self.svc.get_student_portfolio(minor["id"])
        with self.assertRaises(GuardianRequired):
            self.svc.get_student_portfolio(minor["id"], guardian="别人")
        portfolio = self.svc.get_student_portfolio(minor["id"], guardian="母亲")
        self.assertEqual(len(portfolio["enrollments"]), 1)
        adult = self._student(age=30, name="成人")
        portfolio = self.svc.get_student_portfolio(adult["id"])
        self.assertFalse(portfolio["student"]["is_minor"])

    # ---- 证据与补交链 ----
    def test_evidence_supplement_forms_chain(self) -> None:
        plan = self._plan()
        mentor = self._mentor(capacity=1)
        enrollment = self.svc.enroll(self._student()["id"], mentor["id"], plan["id"])
        first = self.svc.submit_evidence(enrollment["id"], 1, "uri://v1")
        second = self.svc.submit_evidence(enrollment["id"], 1, "uri://v2",
                                          supersedes_id=first["id"], note="补交更清晰的照片")
        self.assertEqual(second["supersedes_id"], first["id"])
        # 补交必须同学员同阶段
        other = self.svc.enroll(self._student(name="乙")["id"],
                                self._mentor()["id"], plan["id"])
        with self.assertRaises(ValidationError):
            self.svc.submit_evidence(other["id"], 1, "uri://x", supersedes_id=first["id"])
        with self.assertRaises(ValidationError):
            self.svc.submit_evidence(enrollment["id"], 2, "uri://y",
                                     supersedes_id=first["id"])

    def test_evidence_rejected_when_not_active(self) -> None:
        plan = self._plan()
        mentor = self._mentor(capacity=1)
        enrollment = self.svc.enroll(self._student()["id"], mentor["id"], plan["id"])
        req = self.svc.request_pause(enrollment["id"], "pause", "家中急事",
                                     actor_role="admin", actor="admin:王老师")
        self.svc.decide_pause(req["id"], approve=True)
        with self.assertRaises(InvalidState):
            self.svc.submit_evidence(enrollment["id"], 1, "uri://paused")

    # ---- 暂停与恢复 ----
    def test_pause_keeps_slot_and_resume_restores(self) -> None:
        plan = self._plan()
        mentor = self._mentor(capacity=1)
        enrollment = self.svc.enroll(self._student()["id"], mentor["id"], plan["id"])
        req = self.svc.request_pause(enrollment["id"], "pause", "病假",
                                     actor_role="admin", actor="admin:王老师")
        with self.assertRaises(InvalidState):
            self.svc.request_pause(enrollment["id"], "pause", "重复申请",
                                   actor_role="admin", actor="admin:王老师")
        decided = self.svc.decide_pause(req["id"], approve=True)
        self.assertEqual(decided["status"], "approved")
        self.assertEqual(self.svc.get_enrollment(enrollment["id"])["status"], "paused")
        # 暂停仍占用名额
        self.assertEqual(self.svc.get_mentor_capacity(mentor["id"])["remaining"], 0)
        resume = self.svc.request_pause(enrollment["id"], "resume", "病愈",
                                        actor_role="admin", actor="admin:王老师")
        self.svc.decide_pause(resume["id"], approve=True)
        self.assertEqual(self.svc.get_enrollment(enrollment["id"])["status"], "active")
        # 重复处理同一申请被拒绝
        with self.assertRaises(InvalidState):
            self.svc.decide_pause(resume["id"], approve=True)

    # ---- 转导师：目标与证据不断档 ----
    def test_transfer_moves_slot_and_preserves_goals_and_evidence(self) -> None:
        plan = self._plan(2)
        old_mentor = self._mentor(capacity=1)
        new_mentor = self.svc.create_mentor("李师傅", capacity_total=1)
        enrollment = self.svc.enroll(self._student()["id"], old_mentor["id"], plan["id"])
        evidence = self.svc.submit_evidence(enrollment["id"], 1, "uri://work")
        req = self.svc.request_transfer(enrollment["id"], new_mentor["id"], "导师停教")
        self.svc.decide_transfer(req["id"], approve=True)
        moved = self.svc.get_enrollment(enrollment["id"])
        self.assertEqual(moved["mentor_id"], new_mentor["id"])
        self.assertEqual(len(moved["goal_snapshots"]), 2)  # 目标不丢
        self.assertEqual(self.svc.get_mentor_capacity(old_mentor["id"])["capacity_used"], 0)
        self.assertEqual(self.svc.get_mentor_capacity(new_mentor["id"])["capacity_used"], 1)
        history = self.svc.get_enrollment_history(enrollment["id"])
        types = [e["type"] for e in history["events"]]
        self.assertIn("transfer_requested", types)
        self.assertIn("transfer_approved", types)
        # 证据仍可查（挂在录取上，与导师无关）
        portfolio = self.svc.get_student_portfolio(moved["student_id"])
        self.assertEqual(portfolio["enrollments"][0]["evidence"][0]["id"], evidence["id"])

    def test_transfer_blocked_when_target_full(self) -> None:
        plan = self._plan()
        old_mentor = self._mentor(capacity=1)
        full_mentor = self._mentor(capacity=1)
        enrollment = self.svc.enroll(self._student()["id"], old_mentor["id"], plan["id"])
        self.svc.enroll(self._student(name="占位")["id"], full_mentor["id"], plan["id"])
        req = self.svc.request_transfer(enrollment["id"], full_mentor["id"], "停教")
        with self.assertRaises(CapacityExhausted):
            self.svc.decide_transfer(req["id"], approve=True)
        # 失败整体回滚：申请仍待处理，名额未漂移
        self.assertEqual(self.svc.get_mentor_capacity(old_mentor["id"])["capacity_used"], 1)
        self.assertEqual(self.svc.get_mentor_capacity(full_mentor["id"])["capacity_used"], 1)
        self.assertEqual(self.svc.get_enrollment(enrollment["id"])["mentor_id"], old_mentor["id"])

    # ---- 导师失效 ----
    def test_mentor_invalidation_blocks_and_marks_history(self) -> None:
        plan = self._plan()
        mentor = self._mentor(capacity=2)
        enrollment = self.svc.enroll(self._student()["id"], mentor["id"], plan["id"])
        result = self.svc.invalidate_mentor(mentor["id"], "传承人资格注销")
        self.assertEqual(result["affected_enrollments"], [enrollment["id"]])
        with self.assertRaises(InvalidState):
            self.svc.enroll(self._student(name="乙")["id"], mentor["id"], plan["id"])
        types = [e["type"] for e in self.svc.get_enrollment_history(enrollment["id"])["events"]]
        self.assertIn("mentor_invalidated_notice", types)
        # 失效后学生可转到新导师
        new_mentor = self.svc.create_mentor("王师傅", capacity_total=1)
        req = self.svc.request_transfer(enrollment["id"], new_mentor["id"], "导师失效")
        self.svc.decide_transfer(req["id"], approve=True)
        self.assertEqual(self.svc.get_enrollment(enrollment["id"])["mentor_id"],
                         new_mentor["id"])

    # ---- 退出 ----
    def test_withdraw_releases_slot(self) -> None:
        plan = self._plan()
        mentor = self._mentor(capacity=1)
        enrollment = self.svc.enroll(self._student()["id"], mentor["id"], plan["id"])
        self.svc.withdraw(enrollment["id"], "个人原因")
        self.assertEqual(self.svc.get_enrollment(enrollment["id"])["status"], "withdrawn")
        self.assertEqual(self.svc.get_mentor_capacity(mentor["id"])["remaining"], 1)
        with self.assertRaises(InvalidState):
            self.svc.withdraw(enrollment["id"], "重复退出")
        # 名额释放后他人可入
        self.svc.enroll(self._student(name="乙")["id"], mentor["id"], plan["id"])

    # ---- 跨学期续接 ----
    def test_cross_semester_continuation_keeps_continuous_history(self) -> None:
        plan_v1 = self._plan(2)
        plan_v2 = self._plan(1)
        mentor = self._mentor(capacity=1)
        enrollment = self.svc.enroll(self._student()["id"], mentor["id"], plan_v1["id"])
        evidence = self.svc.submit_evidence(enrollment["id"], 1, "uri://final-work")
        self.svc.assess(enrollment["id"], "pass", "mentor:张师傅",
                        evidence_ids=[evidence["id"]])
        self.assertEqual(self.svc.get_enrollment(enrollment["id"])["status"], "completed")
        self.assertEqual(self.svc.get_mentor_capacity(mentor["id"])["capacity_used"], 0)
        continued = self.svc.continue_enrollment(enrollment["id"], plan_v2["id"])
        self.assertEqual(continued["continued_from_id"], enrollment["id"])
        self.assertEqual(continued["status"], "active")
        self.assertEqual(len(continued["goal_snapshots"]), 1)  # 固定新一期目标
        self.assertEqual(self.svc.get_mentor_capacity(mentor["id"])["capacity_used"], 1)
        history = self.svc.get_enrollment_history(continued["id"])
        self.assertEqual(len(history["enrollment_chain"]), 2)
        types = [e["type"] for e in history["events"]]
        for expected in ("enrolled", "evidence_submitted", "assessment_recorded",
                         "enrollment_completed", "continued_to", "continued_from"):
            self.assertIn(expected, types)

    def test_continuation_to_new_mentor_when_old_invalid(self) -> None:
        plan_v1 = self._plan(1)
        plan_v2 = self._plan(1)
        old_mentor = self._mentor(capacity=1)
        new_mentor = self.svc.create_mentor("赵师傅", capacity_total=1)
        enrollment = self.svc.enroll(self._student()["id"], old_mentor["id"], plan_v1["id"])
        self.svc.invalidate_mentor(old_mentor["id"], "停教")
        with self.assertRaises(InvalidState):
            self.svc.continue_enrollment(enrollment["id"], plan_v2["id"])  # 默认跟原导师
        continued = self.svc.continue_enrollment(enrollment["id"], plan_v2["id"],
                                                 mentor_id=new_mentor["id"])
        self.assertEqual(continued["mentor_id"], new_mentor["id"])
        self.assertEqual(self.svc.get_mentor_capacity(old_mentor["id"])["capacity_used"], 0)
        self.assertEqual(self.svc.get_mentor_capacity(new_mentor["id"])["capacity_used"], 1)

    # ---- 评定与依据还原 ----
    def test_assessment_basis_reconstructable(self) -> None:
        plan = self._plan(2)
        mentor = self._mentor(capacity=1)
        enrollment = self.svc.enroll(self._student()["id"], mentor["id"], plan["id"])
        first = self.svc.submit_evidence(enrollment["id"], 1, "uri://v1")
        second = self.svc.submit_evidence(enrollment["id"], 1, "uri://v2",
                                          supersedes_id=first["id"])
        result = self.svc.assess(enrollment["id"], "pass", "mentor:张师傅",
                                 goal_seq=1, score=88, evidence_ids=[second["id"]])
        basis = self.svc.get_assessment_basis(result["assessment"]["id"])
        self.assertEqual(basis["goal_snapshot"]["seq"], 1)
        self.assertEqual(basis["goal_snapshot"]["title"], "阶段1")
        self.assertEqual(len(basis["evidence"]), 1)
        self.assertEqual(basis["evidence"][0]["content_uri"], "uri://v2")
        self.assertEqual([e["content_uri"] for e in basis["evidence"][0]["superseded_chain"]],
                         ["uri://v1"])

    def test_assessment_validates_evidence_ownership(self) -> None:
        plan = self._plan(2)
        mentor = self._mentor(capacity=2)
        enrollment = self.svc.enroll(self._student()["id"], mentor["id"], plan["id"])
        other = self.svc.enroll(self._student(name="乙")["id"], mentor["id"], plan["id"])
        foreign = self.svc.submit_evidence(other["id"], 1, "uri://foreign")
        with self.assertRaises(ValidationError):
            self.svc.assess(enrollment["id"], "pass", "mentor:张", evidence_ids=[foreign["id"]])
        stage2 = self.svc.submit_evidence(enrollment["id"], 2, "uri://stage2")
        with self.assertRaises(ValidationError):
            self.svc.assess(enrollment["id"], "pass", "mentor:张", goal_seq=1,
                            evidence_ids=[stage2["id"]])
        with self.assertRaises(NotFound):
            self.svc.assess(enrollment["id"], "pass", "mentor:张", goal_seq=99)

    def test_final_pass_completes_and_releases_slot(self) -> None:
        plan = self._plan()
        mentor = self._mentor(capacity=1)
        enrollment = self.svc.enroll(self._student()["id"], mentor["id"], plan["id"])
        self.svc.assess(enrollment["id"], "pass", "mentor:张师傅")
        self.assertEqual(self.svc.get_enrollment(enrollment["id"])["status"], "completed")
        self.assertEqual(self.svc.get_mentor_capacity(mentor["id"])["remaining"], 1)
        with self.assertRaises(InvalidState):
            self.svc.assess(enrollment["id"], "pass", "mentor:张师傅")  # 已结业不能再评

    # ---- 名额对账 ----
    def test_reconcile_capacity_fixes_drift(self) -> None:
        plan = self._plan()
        mentor = self._mentor(capacity=2)
        self.svc.enroll(self._student()["id"], mentor["id"], plan["id"])
        with self.db.transaction() as conn:  # 模拟异常漂移
            conn.execute("UPDATE mentors SET capacity_used = 9 WHERE id = ?", (mentor["id"],))
        fixed = self.svc.reconcile_capacity(mentor["id"])
        self.assertEqual(fixed["capacity_used"], 1)
        self.assertEqual(fixed["remaining"], 1)


if __name__ == "__main__":
    unittest.main()

"""端到端领域测试：覆盖容量并发、重启复原、目标冻结、连续历史、
补交、导师失效、跨学期续接、监护授权与评定依据还原。"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from apprenticeship import (  # noqa: E402
    ApprenticeshipService,
    AuthorizationError,
    CapacityFullError,
    StateConflictError,
    initialize_database,
)
from apprenticeship.db import connect  # noqa: E402


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        initialize_database(self.conn)
        self.svc = ApprenticeshipService(self.conn)
        self._bootstrap()

    def tearDown(self) -> None:
        self.conn.close()

    def _bootstrap(self) -> None:
        self.mentor = self.svc.create_mentor("陆师傅", "竹编", "Q-001")
        self.mentor2 = self.svc.create_mentor("沈师傅", "竹编", "Q-002")
        self.plan = self.svc.create_plan("竹编", "2026春", 1, "竹编计划v1")
        self.svc.add_stage(self.plan["id"], 1, "S1", "选材", "识别竹材并杀青", "2026-04-30")
        self.svc.add_stage(self.plan["id"], 2, "S2", "基础编织", "三种基础纹样", "2026-06-15")
        self.svc.set_capacity(self.plan["id"], self.mentor["id"], 2)
        self.svc.set_capacity(self.plan["id"], self.mentor2["id"], 1)
        self.svc.publish_plan(self.plan["id"])

    def _student(self, name: str = "学生", birth: str | None = None) -> dict:
        return self.svc.create_student(name, birth)


class GoalFreezeTest(ServiceTestBase):
    def test_goals_are_frozen_at_admission(self) -> None:
        stu = self._student()
        enr = self.svc.admit(stu["id"], self.plan["id"], self.mentor["id"])
        self.assertEqual(len(enr["goals"]), 2)
        self.assertTrue(enr["goals_hash"])

        # 计划改版（新学期新版本），老学籍目标不变
        plan2 = self.svc.create_plan("竹编", "2026秋", 2, "竹编计划v2", self.plan["id"])
        self.svc.add_stage(plan2["id"], 1, "S1", "选材（修订）", "新增防霉处理", "2026-10-31")
        self.svc.add_stage(plan2["id"], 2, "S2", "进阶编织", "五种纹样", "2026-12-15")
        self.svc.add_stage(plan2["id"], 3, "S3", "创作", "独立完成一件作品", "2027-01-20")
        self.svc.set_capacity(plan2["id"], self.mentor["id"], 2)
        self.svc.publish_plan(plan2["id"])

        again = self.svc.get_enrollment(enr["id"])
        self.assertEqual([g["code"] for g in again["goals"]], ["S1", "S2"])
        self.assertEqual(again["goals"][0]["name"], "选材")
        self.assertEqual(again["goals_hash"], enr["goals_hash"])

        # 证据只能对照冻结目标提交
        ev = self.svc.submit_evidence(enr["id"], "S1", "photo", "杀青照片")
        self.assertEqual(ev["status"], "submitted")
        with self.assertRaises(Exception):
            self.svc.submit_evidence(enr["id"], "S3", "photo", "不存在的阶段")


class CapacityTest(ServiceTestBase):
    def test_atomic_admission_capacity(self) -> None:
        s1 = self._student("甲")
        s2 = self._student("乙")
        self.svc.admit(s1["id"], self.plan["id"], self.mentor["id"])
        self.svc.admit(s2["id"], self.plan["id"], self.mentor["id"])
        summary = self.svc.capacity_summary(self.plan["id"], self.mentor["id"])
        self.assertEqual((summary["quota"], summary["used"], summary["remaining"]), (2, 2, 0))

        s3 = self._student("丙")
        with self.assertRaises(CapacityFullError):
            self.svc.admit(s3["id"], self.plan["id"], self.mentor["id"])
        # 失败后余额不变
        self.assertEqual(self.svc.capacity_summary(self.plan["id"], self.mentor["id"])["used"], 2)

    def test_withdraw_and_pause_seat_behavior(self) -> None:
        s1 = self._student("甲")
        s2 = self._student("乙")
        e1 = self.svc.admit(s1["id"], self.plan["id"], self.mentor["id"])
        self.svc.admit(s2["id"], self.plan["id"], self.mentor["id"])

        # 暂停保留名额
        pr = self.svc.create_pause_request(e1["id"], "pause", "伤病")
        self.svc.decide_pause_request(pr["id"], True, actor="管理员")
        self.assertEqual(self.svc.capacity_summary(self.plan["id"], self.mentor["id"])["used"], 2)
        with self.assertRaises(CapacityFullError):
            self.svc.admit(self._student("丙")["id"], self.plan["id"], self.mentor["id"])

        # 退出释放名额
        self.svc.withdraw(e1["id"], "主动退出")
        self.assertEqual(self.svc.capacity_summary(self.plan["id"], self.mentor["id"])["remaining"], 1)
        e3 = self.svc.admit(self._student("丁")["id"], self.plan["id"], self.mentor["id"])
        self.assertEqual(e3["status"], "enrolled")

    def test_transfer_moves_seat_and_keeps_history(self) -> None:
        s1 = self._student("甲")
        enr = self.svc.admit(s1["id"], self.plan["id"], self.mentor["id"])
        req = self.svc.request_transfer(enr["id"], self.mentor2["id"], "原导师时间冲突")
        self.svc.decide_transfer_request(req["id"], True, actor="管理员")

        self.assertEqual(self.svc.capacity_summary(self.plan["id"], self.mentor["id"])["used"], 0)
        self.assertEqual(self.svc.capacity_summary(self.plan["id"], self.mentor2["id"])["used"], 1)
        got = self.svc.get_enrollment(enr["id"])
        self.assertEqual(got["mentor_id"], self.mentor2["id"])
        types = [e["event_type"] for e in got["history"]]
        self.assertEqual(types, ["admitted", "transferred"])
        # 转导师不改目标，阶段目标与证据不断档
        self.assertTrue(got["history"][-1]["payload"]["goals_unchanged"])
        ev = self.svc.submit_evidence(enr["id"], "S1", "video", "转导师后补交选材",
                                      make_up_note="随新导师补拍")
        self.assertEqual(ev["is_late"], 1)

    def test_transfer_respects_target_capacity(self) -> None:
        # 两个学生都在 mentor 名下，mentor2 只有 1 个名额
        e1 = self.svc.admit(self._student("甲")["id"], self.plan["id"], self.mentor["id"])["id"]
        e2 = self.svc.admit(self._student("乙")["id"], self.plan["id"], self.mentor["id"])["id"]
        r1 = self.svc.request_transfer(e1, self.mentor2["id"], "停教")["id"]
        r2 = self.svc.request_transfer(e2, self.mentor2["id"], "停教")["id"]
        self.svc.decide_transfer_request(r1, True)
        with self.assertRaises(CapacityFullError):
            self.svc.decide_transfer_request(r2, True)
        # 第二份申请仍停留 pending，学生仍在原导师，名额未被扣减
        self.assertEqual(self.svc.get_transfer_request(r2)["status"], "pending")
        self.assertEqual(self.svc.get_enrollment(e2)["mentor_id"], self.mentor["id"])


class MentorInvalidationTest(ServiceTestBase):
    def test_invalidation_releases_seats_and_history_continues(self) -> None:
        e1 = self.svc.admit(self._student("甲")["id"], self.plan["id"], self.mentor["id"])["id"]
        self.svc.submit_evidence(e1, "S1", "photo", "选材证据")
        result = self.svc.invalidate_mentor(self.mentor["id"], "中途停教", actor="校方")
        self.assertEqual(result["affected_enrollments"], [e1])

        # 名额已释放，余额与在读数一致
        self.assertEqual(self.svc.capacity_summary(self.plan["id"], self.mentor["id"])["used"], 0)
        got = self.svc.get_enrollment(e1)
        self.assertEqual(got["status"], "mentor_invalid")
        # 目标与证据没有断档
        self.assertEqual(len(got["goals"]), 2)
        self.assertEqual([e["stage_code"] for e in self.svc.list_evidences(e1)], ["S1"])

        # 转给新导师：只占新导师的座，不产生负余额
        req = self.svc.request_transfer(e1, self.mentor2["id"], "原导师停教")
        self.svc.decide_transfer_request(req["id"], True, actor="管理员")
        self.assertEqual(self.svc.capacity_summary(self.plan["id"], self.mentor2["id"])["used"], 1)
        types = [e["event_type"] for e in self.svc.history(e1)]
        self.assertEqual(types, ["admitted", "evidence_submitted", "mentor_invalidated", "transferred"])

    def test_cannot_admit_to_non_active_mentor(self) -> None:
        self.svc.suspend_mentor(self.mentor["id"], "外出交流")
        with self.assertRaises(StateConflictError):
            self.svc.admit(self._student()["id"], self.plan["id"], self.mentor["id"])


class EvidenceMakeupTest(ServiceTestBase):
    def test_late_submission_flagged(self) -> None:
        enr = self.svc.admit(self._student()["id"], self.plan["id"], self.mentor["id"])["id"]
        # S1 截止 2026-04-30；当前日期 2026-09-30，逾期自动标记补交
        ev = self.svc.submit_evidence(enr, "S1", "doc", "选材报告", content="数据")
        self.assertEqual(ev["is_late"], 1)
        self.svc.review_evidence(ev["id"], True, reviewer=self.mentor["id"], note="补交有效")
        got = self.svc.get_evidence(ev["id"])
        self.assertEqual(got["status"], "accepted")
        self.assertIn("evidence_reviewed", [e["event_type"] for e in self.svc.history(enr)])


class AssessmentReconstructionTest(ServiceTestBase):
    def test_basis_snapshot_and_reconstruction(self) -> None:
        enr = self.svc.admit(self._student()["id"], self.plan["id"], self.mentor["id"])["id"]
        ev = self.svc.submit_evidence(enr, "S1", "photo", "选材", content="x")
        self.svc.review_evidence(ev["id"], True, reviewer="陆师傅")
        a = self.svc.assess(enr, "陆师傅", "conditional_pass", stage_code="S1", note="补交后通过")

        recon = self.svc.reconstruct_assessment(a["id"])
        self.assertTrue(recon["stored_hash_valid"])
        self.assertTrue(recon["basis_unchanged_since_assessment"])
        self.assertEqual(len(recon["stored_basis"]["frozen_goals"]), 2)
        self.assertEqual(recon["stored_basis"]["evidences"][0]["content_hash"], ev["content_hash"])

        # 评定后新增证据：历史快照仍可还原，同时能检出依据已变化
        self.svc.submit_evidence(enr, "S2", "video", "编织练习")
        recon2 = self.svc.reconstruct_assessment(a["id"])
        self.assertTrue(recon2["stored_hash_valid"])  # 快照本身未被篡改
        self.assertFalse(recon2["basis_unchanged_since_assessment"])


class GuardianConsentTest(ServiceTestBase):
    def test_minor_requires_consent(self) -> None:
        minor = self._student("李小军", "2012-08-20")
        with self.assertRaises(AuthorizationError):
            self.svc.admit(minor["id"], self.plan["id"], self.mentor["id"])
        self.svc.grant_consent(minor["id"], "李父", "CODE-1")
        enr = self.svc.admit(minor["id"], self.plan["id"], self.mentor["id"])

        # 访问闸门：无码/错码拒绝，正确码放行
        with self.assertRaises(AuthorizationError):
            self.svc.get_enrollment(enr["id"])
        with self.assertRaises(AuthorizationError):
            self.svc.get_enrollment(enr["id"], access_code="WRONG")
        got = self.svc.get_enrollment(enr["id"], access_code="CODE-1")
        self.assertTrue(got["access"]["restricted"])

        # 撤销授权后再次受限
        self.svc.revoke_consent("CODE-1")
        with self.assertRaises(AuthorizationError):
            self.svc.get_enrollment(enr["id"], access_code="CODE-1")

    def test_adult_not_restricted(self) -> None:
        adult = self._student("韩梅", "2005-03-01")
        enr = self.svc.admit(adult["id"], self.plan["id"], self.mentor["id"])["id"]
        self.assertFalse(self.svc.get_enrollment(enr)["access"]["restricted"])


class CrossTermTest(ServiceTestBase):
    def _new_term_plan(self) -> dict:
        plan2 = self.svc.create_plan("竹编", "2026秋", 2, "竹编计划v2", self.plan["id"])
        self.svc.add_stage(plan2["id"], 1, "S1", "进阶选材", "防霉处理", "2026-10-31")
        self.svc.add_stage(plan2["id"], 2, "S2", "创作", "独立作品", "2027-01-15")
        self.svc.set_capacity(plan2["id"], self.mentor["id"], 2)
        self.svc.set_capacity(plan2["id"], self.mentor2["id"], 1)
        self.svc.publish_plan(plan2["id"])
        return plan2

    def test_continue_to_new_term(self) -> None:
        old = self.svc.admit(self._student()["id"], self.plan["id"], self.mentor["id"])["id"]
        self.svc.submit_evidence(old, "S1", "photo", "春学期选材")
        plan2 = self._new_term_plan()
        new = self.svc.continue_to_new_term(old, plan2["id"], self.mentor["id"])

        # 旧学籍归档并释放旧座，新学籍占新座
        self.assertEqual(self.svc.get_enrollment(old)["status"], "transferred_out")
        self.assertEqual(self.svc.capacity_summary(self.plan["id"], self.mentor["id"])["used"], 0)
        self.assertEqual(self.svc.capacity_summary(plan2["id"], self.mentor["id"])["used"], 1)
        # 新学期目标固定为新版本
        self.assertEqual([g["name"] for g in new["goals"]], ["进阶选材", "创作"])

        # 链条可双向还原：沿 predecessor 回溯
        chain = self.svc.lineage(new["id"])
        self.assertEqual([c["term"] for c in chain], ["2026秋", "2026春"])
        # 历史连续：旧学籍末事件指向新学籍
        self.assertEqual(self.svc.history(old)[-1]["event_type"], "continued_out")
        self.assertEqual(self.svc.history(new["id"])[0]["payload"]["predecessor_enrollment_id"], old)

    def test_continue_then_mentor_change_keeps_lineage(self) -> None:
        old = self.svc.admit(self._student()["id"], self.plan["id"], self.mentor["id"])["id"]
        plan2 = self._new_term_plan()
        new = self.svc.continue_to_new_term(old, plan2["id"], self.mentor2["id"])
        self.assertEqual(new["predecessor_enrollment_id"], old)
        self.assertEqual(len(self.svc.lineage(new["id"])), 2)


class RestartPersistenceTest(unittest.TestCase):
    """进程重启：余额完全由只追加台账复原。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "app.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _service(self) -> ApprenticeshipService:
        conn = connect(self.db_path)
        initialize_database(conn)
        return ApprenticeshipService(conn)

    def test_balance_survives_restart(self) -> None:
        svc = self._service()
        m = svc.create_mentor("陆师傅", "竹编")
        plan = svc.create_plan("竹编", "2026春", 1, "v1")
        svc.add_stage(plan["id"], 1, "S1", "选材", "杀青")
        svc.set_capacity(plan["id"], m["id"], 3)
        svc.publish_plan(plan["id"])
        ids = []
        for name in ("甲", "乙", "丙"):
            stu = svc.create_student(name)
            ids.append(svc.admit(stu["id"], plan["id"], m["id"])["id"])
        svc.withdraw(ids[0], "退出")
        svc.conn.close()

        # 重新打开进程，没有任何内存计数
        svc2 = self._service()
        summary = svc2.capacity_summary(plan["id"], m["id"])
        self.assertEqual((summary["quota"], summary["used"], summary["remaining"]), (3, 2, 1))
        # 历史也完整
        self.assertEqual([e["event_type"] for e in svc2.history(ids[1])], ["admitted"])
        self.assertEqual(
            [e["event_type"] for e in svc2.history(ids[0])], ["admitted", "withdrawn"]
        )
        svc2.conn.close()


class ConcurrentAdmissionTest(unittest.TestCase):
    """多人同时录取：只能录取 quota 人，其余得到 CapacityFullError。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "app.db")
        conn = connect(self.db_path)
        initialize_database(conn)
        svc = ApprenticeshipService(conn)
        self.mentor = svc.create_mentor("陆师傅", "竹编")
        self.plan = svc.create_plan("竹编", "2026春", 1, "v1")
        svc.add_stage(self.plan["id"], 1, "S1", "选材", "杀青")
        svc.set_capacity(self.plan["id"], self.mentor["id"], 5)
        svc.publish_plan(self.plan["id"])
        self.students = [svc.create_student(f"学生{i}")["id"] for i in range(20)]
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_20_threads_admit_to_5_seats(self) -> None:
        barrier = threading.Barrier(20)
        results: list[str] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker(student_id: str) -> None:
            conn = connect(self.db_path)
            svc = ApprenticeshipService(conn)
            barrier.wait()  # 尽量让 20 个事务同时开始
            try:
                enr = svc.admit(student_id, self.plan["id"], self.mentor["id"])
                with lock:
                    results.append(enr["id"])
            except CapacityFullError as exc:
                with lock:
                    errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(sid,)) for sid in self.students]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        conn = connect(self.db_path)
        svc = ApprenticeshipService(conn)
        summary = svc.capacity_summary(self.plan["id"], self.mentor["id"])
        self.assertEqual(len(results), 5)
        self.assertEqual(len(errors), 15)
        self.assertEqual(summary["used"], 5)
        self.assertEqual(summary["remaining"], 0)
        self.assertEqual(summary["active_enrollments"], 5)
        conn.close()


if __name__ == "__main__":
    unittest.main()

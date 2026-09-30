"""传统技艺师徒计划核心业务服务。

覆盖：计划版本、导师资格与容量、阶段目标快照、学习证据（含补交链）、
暂停/恢复与转导师申请、退出、跨学期续接、评定与依据还原、未成年人监护授权。

一致性约定：
- 名额占用使用带守卫的 UPDATE ... WHERE capacity_used < capacity_total，
  与录取、目标快照在同一事务内提交；并发录取或进程重启后余额仍准确。
- 仅 active / paused 状态的录取占用名额；withdrawn / completed / continued 不占。
- 录取时把当期阶段目标复制为快照，计划版本后续变更不影响在读学生。
- 证据与评定只增不改：补交通过 supersedes_id 链接旧证据，评定通过
  assessment_evidence 固定依据，任何时候都可还原。
- 所有状态变化写入 events，跨学期续接以 continued_from_id 串成连续历史。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone

from .db import Database
from .errors import (
    CapacityExhausted,
    GuardianRequired,
    InvalidState,
    NotFound,
    ValidationError,
)

OCCUPYING_STATUSES = ("active", "paused")  # 占用导师名额的录取状态
MINOR_AGE = 18
PASS_VERDICTS = ("pass", "excellent")  # 结业评定结论


def _now() -> str:
    """统一使用 UTC 朴素时间戳，保证字符串可比较。"""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _age_on(birth: date, on: date) -> int:
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} 不是合法日期（YYYY-MM-DD）：{value}") from exc


def _normalize_bound(value: str | None, field: str, end_of_day: bool) -> str | None:
    """把日期/时间输入规范为可比较的时间戳字符串；纯日期按当日开始/结束处理。"""
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if "T" not in value:
        _parse_date(value, field)
        return f"{value}T{'23:59:59' if end_of_day else '00:00:00'}"
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field} 不是合法时间：{value}") from exc
    return value


class MentorshipService:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _emit(self, conn, enrollment_id, actor, event_type, payload=None) -> None:
        conn.execute(
            "INSERT INTO events (enrollment_id, actor, type, payload, created_at) VALUES (?,?,?,?,?)",
            (enrollment_id, actor, event_type, json.dumps(payload or {}, ensure_ascii=False), _now()),
        )

    def _get(self, conn, table: str, row_id: int, label: str):
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            raise NotFound(f"{label}不存在：{row_id}")
        return row

    @staticmethod
    def _require_text(value, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{field}不能为空")
        return value.strip()

    def _is_minor(self, student) -> bool:
        birth = _parse_date(student["birth_date"], "birth_date")
        return _age_on(birth, _today()) < MINOR_AGE

    def _valid_authorizations(self, conn, student_id: int, scope: str):
        now = _now()
        rows = conn.execute(
            "SELECT * FROM guardian_authorizations WHERE student_id = ? AND status = 'active'",
            (student_id,),
        ).fetchall()
        return [
            row
            for row in rows
            if row["valid_from"] <= now
            and (row["valid_to"] is None or row["valid_to"] >= now)
            and (row["scope"] == "all" or row["scope"] == scope)
        ]

    def _require_guardian(self, conn, student, scope: str, guardian_name: str | None = None) -> None:
        """未成年人操作必须存在有效监护授权；指定监护人时还需姓名匹配。"""
        if not self._is_minor(student):
            return
        valid = self._valid_authorizations(conn, student["id"], scope)
        if not valid:
            raise GuardianRequired(f"未成年学生 {student['name']} 缺少有效监护授权（范围：{scope}）")
        if guardian_name is not None and all(a["guardian_name"] != guardian_name for a in valid):
            raise GuardianRequired(f"监护人 {guardian_name} 不在有效授权中")

    def _require_named_guardian(self, conn, student, scope: str, guardian_name: str | None) -> None:
        """未成年人以学生本人身份操作时，必须明确指定一位有效监护人。"""
        if not self._is_minor(student):
            return
        if guardian_name is None:
            raise GuardianRequired(f"未成年学生 {student['name']} 的该操作需要指定监护人")
        self._require_guardian(conn, student, scope, guardian_name)

    def _check_assignable(self, mentor) -> None:
        if mentor["status"] != "active":
            raise InvalidState(f"导师 {mentor['name']} 当前状态为 {mentor['status']}，不能接收学生")
        if mentor["qualified_until"] is not None and mentor["qualified_until"] < _today().isoformat():
            raise InvalidState(f"导师 {mentor['name']} 的传承资格已过期")

    def _occupy_slot(self, conn, mentor) -> None:
        """守卫式占用名额：并发下也不会超卖。"""
        cur = conn.execute(
            "UPDATE mentors SET capacity_used = capacity_used + 1 "
            "WHERE id = ? AND capacity_used < capacity_total",
            (mentor["id"],),
        )
        if cur.rowcount != 1:
            raise CapacityExhausted(
                f"导师 {mentor['name']} 名额已满（{mentor['capacity_used']}/{mentor['capacity_total']}）"
            )

    def _release_slot(self, conn, mentor_id: int) -> None:
        conn.execute(
            "UPDATE mentors SET capacity_used = MAX(capacity_used - 1, 0) WHERE id = ?",
            (mentor_id,),
        )

    def _enrollment_view(self, conn, enrollment_id: int) -> dict:
        enrollment = dict(self._get(conn, "enrollments", enrollment_id, "录取"))
        snapshots = conn.execute(
            "SELECT * FROM goal_snapshots WHERE enrollment_id = ? ORDER BY seq",
            (enrollment_id,),
        ).fetchall()
        enrollment["goal_snapshots"] = [dict(s) for s in snapshots]
        return enrollment

    def _capacity_view(self, conn, mentor_id: int) -> dict:
        mentor = self._get(conn, "mentors", mentor_id, "导师")
        return {
            "mentor_id": mentor_id,
            "name": mentor["name"],
            "status": mentor["status"],
            "capacity_total": mentor["capacity_total"],
            "capacity_used": mentor["capacity_used"],
            "remaining": mentor["capacity_total"] - mentor["capacity_used"],
        }

    def _plan_view(self, conn, plan_id: int) -> dict:
        plan = dict(self._get(conn, "plan_versions", plan_id, "计划版本"))
        goals = conn.execute(
            "SELECT * FROM stage_goals WHERE plan_version_id = ? ORDER BY seq",
            (plan_id,),
        ).fetchall()
        plan["goals"] = [dict(g) for g in goals]
        return plan

    # ------------------------------------------------------------------
    # 计划版本与阶段目标
    # ------------------------------------------------------------------
    def create_plan_version(self, code, title, actor="admin:api") -> dict:
        code = self._require_text(code, "计划编号")
        title = self._require_text(title, "计划标题")
        with self.db.transaction() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO plan_versions (code, title, created_at) VALUES (?,?,?)",
                    (code, title, _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValidationError(f"计划编号已存在：{code}") from exc
            self._emit(conn, None, actor, "plan_version_created",
                       {"plan_version_id": cur.lastrowid, "code": code})
            return self._plan_view(conn, cur.lastrowid)

    def add_stage_goal(self, plan_version_id, seq, title, detail="", required_evidence=1,
                       actor="admin:api") -> dict:
        title = self._require_text(title, "目标标题")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise ValidationError("阶段序号必须为正整数")
        if not isinstance(required_evidence, int) or required_evidence < 0:
            raise ValidationError("所需证据数不能为负")
        with self.db.transaction() as conn:
            plan = self._get(conn, "plan_versions", plan_version_id, "计划版本")
            if plan["status"] != "draft":
                raise InvalidState("仅草稿状态的计划版本可以增改阶段目标")
            try:
                cur = conn.execute(
                    "INSERT INTO stage_goals (plan_version_id, seq, title, detail, required_evidence)"
                    " VALUES (?,?,?,?,?)",
                    (plan_version_id, seq, title, detail or "", required_evidence),
                )
            except sqlite3.IntegrityError as exc:
                raise ValidationError(f"阶段序号重复：{seq}") from exc
            self._emit(conn, None, actor, "stage_goal_added",
                       {"plan_version_id": plan_version_id, "seq": seq})
            return dict(self._get(conn, "stage_goals", cur.lastrowid, "阶段目标"))

    def publish_plan_version(self, plan_version_id, actor="admin:api") -> dict:
        with self.db.transaction() as conn:
            plan = self._get(conn, "plan_versions", plan_version_id, "计划版本")
            if plan["status"] != "draft":
                raise InvalidState("仅草稿状态可以发布")
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM stage_goals WHERE plan_version_id = ?",
                (plan_version_id,),
            ).fetchone()["c"]
            if count == 0:
                raise InvalidState("计划版本缺少阶段目标，不能发布")
            conn.execute(
                "UPDATE plan_versions SET status = 'published', published_at = ? WHERE id = ?",
                (_now(), plan_version_id),
            )
            self._emit(conn, None, actor, "plan_version_published",
                       {"plan_version_id": plan_version_id})
            return self._plan_view(conn, plan_version_id)

    def archive_plan_version(self, plan_version_id, actor="admin:api") -> dict:
        with self.db.transaction() as conn:
            plan = self._get(conn, "plan_versions", plan_version_id, "计划版本")
            if plan["status"] != "published":
                raise InvalidState("仅已发布的计划版本可以归档")
            conn.execute("UPDATE plan_versions SET status = 'archived' WHERE id = ?", (plan_version_id,))
            self._emit(conn, None, actor, "plan_version_archived",
                       {"plan_version_id": plan_version_id})
            return self._plan_view(conn, plan_version_id)

    def get_plan_version(self, plan_version_id) -> dict:
        with self.db.transaction() as conn:
            return self._plan_view(conn, plan_version_id)

    # ------------------------------------------------------------------
    # 导师：资格与容量
    # ------------------------------------------------------------------
    def create_mentor(self, name, craft="", capacity_total=None, qualified_until=None,
                      actor="admin:api") -> dict:
        name = self._require_text(name, "导师姓名")
        if not isinstance(capacity_total, int) or isinstance(capacity_total, bool) or capacity_total < 0:
            raise ValidationError("容量必须是非负整数")
        if qualified_until is not None:
            qualified_until = _parse_date(str(qualified_until), "qualified_until").isoformat()
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO mentors (name, craft, capacity_total, qualified_until, created_at)"
                " VALUES (?,?,?,?,?)",
                (name, craft or "", capacity_total, qualified_until, _now()),
            )
            self._emit(conn, None, actor, "mentor_created",
                       {"mentor_id": cur.lastrowid, "name": name})
            return dict(self._get(conn, "mentors", cur.lastrowid, "导师"))

    def _set_mentor_status(self, mentor_id, status, reason, actor, from_statuses) -> dict:
        with self.db.transaction() as conn:
            mentor = self._get(conn, "mentors", mentor_id, "导师")
            if mentor["status"] not in from_statuses:
                raise InvalidState(f"导师当前状态为 {mentor['status']}，不能变更为 {status}")
            conn.execute("UPDATE mentors SET status = ? WHERE id = ?", (status, mentor_id))
            self._emit(conn, None, actor, f"mentor_{status}",
                       {"mentor_id": mentor_id, "reason": reason})
            return dict(self._get(conn, "mentors", mentor_id, "导师"))

    def suspend_mentor(self, mentor_id, reason="", actor="admin:api") -> dict:
        return self._set_mentor_status(mentor_id, "suspended", reason, actor, ("active",))

    def reinstate_mentor(self, mentor_id, actor="admin:api") -> dict:
        return self._set_mentor_status(mentor_id, "active", "", actor, ("suspended",))

    def invalidate_mentor(self, mentor_id, reason="", actor="admin:api") -> dict:
        """导师失效：永久停止接收学生，并在每条在读录取上留下连续历史。"""
        with self.db.transaction() as conn:
            mentor = self._get(conn, "mentors", mentor_id, "导师")
            if mentor["status"] == "invalid":
                raise InvalidState("导师已失效")
            conn.execute("UPDATE mentors SET status = 'invalid' WHERE id = ?", (mentor_id,))
            self._emit(conn, None, actor, "mentor_invalidated",
                       {"mentor_id": mentor_id, "reason": reason})
            affected = conn.execute(
                "SELECT id FROM enrollments WHERE mentor_id = ? AND status IN ('active', 'paused')",
                (mentor_id,),
            ).fetchall()
            for row in affected:
                self._emit(conn, row["id"], actor, "mentor_invalidated_notice",
                           {"mentor_id": mentor_id, "reason": reason})
            return {
                "mentor": dict(self._get(conn, "mentors", mentor_id, "导师")),
                "affected_enrollments": [r["id"] for r in affected],
            }

    def get_mentor_capacity(self, mentor_id) -> dict:
        with self.db.transaction() as conn:
            return self._capacity_view(conn, mentor_id)

    def reconcile_capacity(self, mentor_id, actor="admin:system") -> dict:
        """按在读录取重算名额余额，修正任何漂移（重启/异常后的自愈手段）。"""
        with self.db.transaction() as conn:
            mentor = self._get(conn, "mentors", mentor_id, "导师")
            actual = conn.execute(
                "SELECT COUNT(*) AS c FROM enrollments "
                "WHERE mentor_id = ? AND status IN ('active', 'paused')",
                (mentor_id,),
            ).fetchone()["c"]
            if actual != mentor["capacity_used"]:
                conn.execute("UPDATE mentors SET capacity_used = ? WHERE id = ?", (actual, mentor_id))
                self._emit(conn, None, actor, "capacity_reconciled",
                           {"mentor_id": mentor_id, "stored": mentor["capacity_used"], "actual": actual})
            return self._capacity_view(conn, mentor_id)

    # ------------------------------------------------------------------
    # 学生与监护授权
    # ------------------------------------------------------------------
    def create_student(self, name, birth_date, actor="admin:api") -> dict:
        name = self._require_text(name, "学生姓名")
        birth = _parse_date(self._require_text(birth_date, "出生日期"), "birth_date")
        if birth > _today():
            raise ValidationError("出生日期不能在未来")
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO students (name, birth_date, created_at) VALUES (?,?,?)",
                (name, birth.isoformat(), _now()),
            )
            self._emit(conn, None, actor, "student_created",
                       {"student_id": cur.lastrowid, "name": name})
            student = dict(self._get(conn, "students", cur.lastrowid, "学生"))
            student["is_minor"] = self._is_minor(student)
            return student

    def add_guardian_authorization(self, student_id, guardian_name, scope="all",
                                   valid_from=None, valid_to=None, actor="admin:api") -> dict:
        guardian_name = self._require_text(guardian_name, "监护人姓名")
        scope = self._require_text(scope, "授权范围")
        valid_from = _normalize_bound(valid_from, "valid_from", end_of_day=False) or _now()
        valid_to = _normalize_bound(valid_to, "valid_to", end_of_day=True)
        if valid_to is not None and valid_to < valid_from:
            raise ValidationError("授权结束时间早于开始时间")
        with self.db.transaction() as conn:
            self._get(conn, "students", student_id, "学生")
            cur = conn.execute(
                "INSERT INTO guardian_authorizations"
                " (student_id, guardian_name, scope, valid_from, valid_to, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (student_id, guardian_name, scope, valid_from, valid_to, _now()),
            )
            self._emit(conn, None, actor, "guardian_authorization_added",
                       {"authorization_id": cur.lastrowid, "student_id": student_id})
            return dict(self._get(conn, "guardian_authorizations", cur.lastrowid, "监护授权"))

    def revoke_guardian_authorization(self, authorization_id, actor="admin:api") -> dict:
        with self.db.transaction() as conn:
            auth = self._get(conn, "guardian_authorizations", authorization_id, "监护授权")
            if auth["status"] != "active":
                raise InvalidState("授权已撤销")
            conn.execute(
                "UPDATE guardian_authorizations SET status = 'revoked' WHERE id = ?",
                (authorization_id,),
            )
            self._emit(conn, None, actor, "guardian_authorization_revoked",
                       {"authorization_id": authorization_id})
            return dict(self._get(conn, "guardian_authorizations", authorization_id, "监护授权"))

    # ------------------------------------------------------------------
    # 录取：原子占用名额并固定当期目标
    # ------------------------------------------------------------------
    def enroll(self, student_id, mentor_id, plan_version_id, actor="admin:api") -> dict:
        with self.db.transaction() as conn:
            student = self._get(conn, "students", student_id, "学生")
            plan = self._get(conn, "plan_versions", plan_version_id, "计划版本")
            if plan["status"] != "published":
                raise InvalidState("计划版本未发布，不能录取")
            goals = conn.execute(
                "SELECT * FROM stage_goals WHERE plan_version_id = ? ORDER BY seq",
                (plan_version_id,),
            ).fetchall()
            if not goals:
                raise InvalidState("计划版本缺少阶段目标")
            self._require_guardian(conn, student, "enroll")
            mentor = self._get(conn, "mentors", mentor_id, "导师")
            self._check_assignable(mentor)
            self._occupy_slot(conn, mentor)
            try:
                cur = conn.execute(
                    "INSERT INTO enrollments (student_id, mentor_id, plan_version_id, created_at)"
                    " VALUES (?,?,?,?)",
                    (student_id, mentor_id, plan_version_id, _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidState("该学生在本计划版本已有在读录取") from exc
            enrollment_id = cur.lastrowid
            for goal in goals:
                conn.execute(
                    "INSERT INTO goal_snapshots"
                    " (enrollment_id, seq, title, detail, required_evidence, source_goal_id)"
                    " VALUES (?,?,?,?,?,?)",
                    (enrollment_id, goal["seq"], goal["title"], goal["detail"],
                     goal["required_evidence"], goal["id"]),
                )
            self._emit(conn, enrollment_id, actor, "enrolled",
                       {"student_id": student_id, "mentor_id": mentor_id,
                        "plan_version_id": plan_version_id, "goal_count": len(goals)})
            return self._enrollment_view(conn, enrollment_id)

    def get_enrollment(self, enrollment_id) -> dict:
        with self.db.transaction() as conn:
            return self._enrollment_view(conn, enrollment_id)

    # ------------------------------------------------------------------
    # 学习证据与补交
    # ------------------------------------------------------------------
    def submit_evidence(self, enrollment_id, goal_seq, content_uri, kind="work", note="",
                        supersedes_id=None, actor_role="student", guardian=None,
                        actor="student:self") -> dict:
        content_uri = self._require_text(content_uri, "证据内容")
        if not isinstance(goal_seq, int) or isinstance(goal_seq, bool):
            raise ValidationError("阶段序号必须是整数")
        with self.db.transaction() as conn:
            enrollment = self._get(conn, "enrollments", enrollment_id, "录取")
            if enrollment["status"] != "active":
                raise InvalidState(f"录取状态为 {enrollment['status']}，不能提交证据")
            snapshot = conn.execute(
                "SELECT * FROM goal_snapshots WHERE enrollment_id = ? AND seq = ?",
                (enrollment_id, goal_seq),
            ).fetchone()
            if snapshot is None:
                raise NotFound(f"录取 {enrollment_id} 不存在阶段 {goal_seq} 的目标快照")
            if actor_role == "student":
                student = self._get(conn, "students", enrollment["student_id"], "学生")
                self._require_named_guardian(conn, student, "evidence", guardian)
            if supersedes_id is not None:
                old = self._get(conn, "evidence", supersedes_id, "被补交的证据")
                if old["enrollment_id"] != enrollment_id:
                    raise ValidationError("只能补交本录取下的证据")
                if old["goal_snapshot_id"] != snapshot["id"]:
                    raise ValidationError("补交证据必须属于同一阶段目标")
            cur = conn.execute(
                "INSERT INTO evidence"
                " (enrollment_id, goal_snapshot_id, kind, content_uri, note, supersedes_id,"
                "  submitted_by, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (enrollment_id, snapshot["id"], kind or "work", content_uri, note or "",
                 supersedes_id, actor, _now()),
            )
            self._emit(conn, enrollment_id, actor, "evidence_submitted",
                       {"evidence_id": cur.lastrowid, "goal_seq": goal_seq,
                        "supersedes_id": supersedes_id})
            return dict(self._get(conn, "evidence", cur.lastrowid, "证据"))

    # ------------------------------------------------------------------
    # 暂停 / 恢复申请
    # ------------------------------------------------------------------
    def request_pause(self, enrollment_id, action="pause", reason="", actor="student:self",
                      actor_role="student", guardian=None) -> dict:
        if action not in ("pause", "resume"):
            raise ValidationError("action 只能是 pause 或 resume")
        with self.db.transaction() as conn:
            enrollment = self._get(conn, "enrollments", enrollment_id, "录取")
            expected = "active" if action == "pause" else "paused"
            label = "暂停" if action == "pause" else "恢复"
            if enrollment["status"] != expected:
                raise InvalidState(f"当前状态为 {enrollment['status']}，不能申请{label}")
            pending = conn.execute(
                "SELECT COUNT(*) AS c FROM pause_requests WHERE enrollment_id = ? AND status = 'pending'",
                (enrollment_id,),
            ).fetchone()["c"]
            if pending:
                raise InvalidState("已有待处理的暂停/恢复申请")
            if actor_role == "student":
                student = self._get(conn, "students", enrollment["student_id"], "学生")
                self._require_named_guardian(conn, student, "pause", guardian)
            cur = conn.execute(
                "INSERT INTO pause_requests (enrollment_id, action, reason, requested_by, requested_at)"
                " VALUES (?,?,?,?,?)",
                (enrollment_id, action, reason or "", actor, _now()),
            )
            self._emit(conn, enrollment_id, actor, f"{action}_requested",
                       {"request_id": cur.lastrowid, "reason": reason})
            return dict(self._get(conn, "pause_requests", cur.lastrowid, "暂停申请"))

    def decide_pause(self, request_id, approve, decided_by="admin:api") -> dict:
        with self.db.transaction() as conn:
            req = self._get(conn, "pause_requests", request_id, "暂停申请")
            if req["status"] != "pending":
                raise InvalidState("申请已处理")
            enrollment = self._get(conn, "enrollments", req["enrollment_id"], "录取")
            now = _now()
            if approve:
                new_status = "paused" if req["action"] == "pause" else "active"
                expected = "active" if req["action"] == "pause" else "paused"
                if enrollment["status"] != expected:
                    raise InvalidState(f"录取状态已变化（{enrollment['status']}），不能执行")
                conn.execute("UPDATE enrollments SET status = ? WHERE id = ?",
                             (new_status, enrollment["id"]))
                conn.execute(
                    "UPDATE pause_requests SET status = 'approved', decided_by = ?, decided_at = ?"
                    " WHERE id = ?",
                    (decided_by, now, request_id),
                )
            else:
                conn.execute(
                    "UPDATE pause_requests SET status = 'rejected', decided_by = ?, decided_at = ?"
                    " WHERE id = ?",
                    (decided_by, now, request_id),
                )
            self._emit(conn, enrollment["id"], decided_by,
                       f"{req['action']}_{'approved' if approve else 'rejected'}",
                       {"request_id": request_id})
            return dict(self._get(conn, "pause_requests", request_id, "暂停申请"))

    # ------------------------------------------------------------------
    # 转导师申请：目标与证据随录取走，不断档
    # ------------------------------------------------------------------
    def request_transfer(self, enrollment_id, to_mentor_id, reason="", actor="admin:api") -> dict:
        with self.db.transaction() as conn:
            enrollment = self._get(conn, "enrollments", enrollment_id, "录取")
            if enrollment["status"] not in OCCUPYING_STATUSES:
                raise InvalidState(f"录取状态为 {enrollment['status']}，不能申请转导师")
            if enrollment["mentor_id"] == to_mentor_id:
                raise ValidationError("目标导师与当前导师相同")
            self._get(conn, "mentors", to_mentor_id, "导师")
            pending = conn.execute(
                "SELECT COUNT(*) AS c FROM transfer_requests"
                " WHERE enrollment_id = ? AND status = 'pending'",
                (enrollment_id,),
            ).fetchone()["c"]
            if pending:
                raise InvalidState("已有待处理的转导师申请")
            cur = conn.execute(
                "INSERT INTO transfer_requests"
                " (enrollment_id, from_mentor_id, to_mentor_id, reason, requested_by, requested_at)"
                " VALUES (?,?,?,?,?,?)",
                (enrollment_id, enrollment["mentor_id"], to_mentor_id, reason or "", actor, _now()),
            )
            self._emit(conn, enrollment_id, actor, "transfer_requested",
                       {"request_id": cur.lastrowid, "from_mentor_id": enrollment["mentor_id"],
                        "to_mentor_id": to_mentor_id})
            return dict(self._get(conn, "transfer_requests", cur.lastrowid, "转导师申请"))

    def decide_transfer(self, request_id, approve, decided_by="admin:api") -> dict:
        with self.db.transaction() as conn:
            req = self._get(conn, "transfer_requests", request_id, "转导师申请")
            if req["status"] != "pending":
                raise InvalidState("申请已处理")
            now = _now()
            if not approve:
                conn.execute(
                    "UPDATE transfer_requests SET status = 'rejected', decided_by = ?, decided_at = ?"
                    " WHERE id = ?",
                    (decided_by, now, request_id),
                )
                self._emit(conn, req["enrollment_id"], decided_by, "transfer_rejected",
                           {"request_id": request_id})
                return dict(self._get(conn, "transfer_requests", request_id, "转导师申请"))
            enrollment = self._get(conn, "enrollments", req["enrollment_id"], "录取")
            if enrollment["status"] not in OCCUPYING_STATUSES:
                raise InvalidState(f"录取状态为 {enrollment['status']}，不能执行转导师")
            if enrollment["mentor_id"] != req["from_mentor_id"]:
                raise InvalidState("当前导师已变化，申请失效")
            to_mentor = self._get(conn, "mentors", req["to_mentor_id"], "导师")
            self._check_assignable(to_mentor)
            # 同一事务内“占新放旧”：任何一步失败都整体回滚，名额不会漂移
            self._occupy_slot(conn, to_mentor)
            self._release_slot(conn, req["from_mentor_id"])
            conn.execute("UPDATE enrollments SET mentor_id = ? WHERE id = ?",
                         (req["to_mentor_id"], enrollment["id"]))
            conn.execute(
                "UPDATE transfer_requests SET status = 'approved', decided_by = ?, decided_at = ?"
                " WHERE id = ?",
                (decided_by, now, request_id),
            )
            self._emit(conn, enrollment["id"], decided_by, "transfer_approved",
                       {"request_id": request_id, "from_mentor_id": req["from_mentor_id"],
                        "to_mentor_id": req["to_mentor_id"]})
            return dict(self._get(conn, "transfer_requests", request_id, "转导师申请"))

    # ------------------------------------------------------------------
    # 退出
    # ------------------------------------------------------------------
    def withdraw(self, enrollment_id, reason="", actor="admin:api") -> dict:
        with self.db.transaction() as conn:
            enrollment = self._get(conn, "enrollments", enrollment_id, "录取")
            if enrollment["status"] not in OCCUPYING_STATUSES:
                raise InvalidState(f"录取状态为 {enrollment['status']}，不能退出")
            self._release_slot(conn, enrollment["mentor_id"])
            conn.execute(
                "UPDATE enrollments SET status = 'withdrawn', closed_at = ? WHERE id = ?",
                (_now(), enrollment_id),
            )
            self._emit(conn, enrollment_id, actor, "withdrawn", {"reason": reason})
            return self._enrollment_view(conn, enrollment_id)

    # ------------------------------------------------------------------
    # 跨学期续接：旧录取关闭、新录取占名额并固定新一期目标，历史串链
    # ------------------------------------------------------------------
    def continue_enrollment(self, enrollment_id, plan_version_id, mentor_id=None,
                            actor="admin:api") -> dict:
        with self.db.transaction() as conn:
            old = self._get(conn, "enrollments", enrollment_id, "录取")
            if old["status"] not in ("active", "paused", "completed"):
                raise InvalidState(f"录取状态为 {old['status']}，不能续接")
            plan = self._get(conn, "plan_versions", plan_version_id, "计划版本")
            if plan["status"] != "published":
                raise InvalidState("目标计划版本未发布")
            goals = conn.execute(
                "SELECT * FROM stage_goals WHERE plan_version_id = ? ORDER BY seq",
                (plan_version_id,),
            ).fetchall()
            if not goals:
                raise InvalidState("目标计划版本缺少阶段目标")
            student = self._get(conn, "students", old["student_id"], "学生")
            self._require_guardian(conn, student, "enroll")
            target_mentor_id = mentor_id if mentor_id is not None else old["mentor_id"]
            mentor = self._get(conn, "mentors", target_mentor_id, "导师")
            self._check_assignable(mentor)
            if old["status"] in OCCUPYING_STATUSES:
                self._release_slot(conn, old["mentor_id"])
            self._occupy_slot(conn, mentor)
            now = _now()
            conn.execute(
                "UPDATE enrollments SET status = 'continued', closed_at = ? WHERE id = ?",
                (now, enrollment_id),
            )
            try:
                cur = conn.execute(
                    "INSERT INTO enrollments"
                    " (student_id, mentor_id, plan_version_id, continued_from_id, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (old["student_id"], target_mentor_id, plan_version_id, enrollment_id, now),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidState("该学生在目标计划版本已有在读录取") from exc
            new_id = cur.lastrowid
            for goal in goals:
                conn.execute(
                    "INSERT INTO goal_snapshots"
                    " (enrollment_id, seq, title, detail, required_evidence, source_goal_id)"
                    " VALUES (?,?,?,?,?,?)",
                    (new_id, goal["seq"], goal["title"], goal["detail"],
                     goal["required_evidence"], goal["id"]),
                )
            self._emit(conn, enrollment_id, actor, "continued_to",
                       {"new_enrollment_id": new_id, "plan_version_id": plan_version_id})
            self._emit(conn, new_id, actor, "continued_from",
                       {"previous_enrollment_id": enrollment_id, "plan_version_id": plan_version_id})
            return self._enrollment_view(conn, new_id)

    # ------------------------------------------------------------------
    # 评定与依据还原
    # ------------------------------------------------------------------
    def assess(self, enrollment_id, verdict, assessor, goal_seq=None, score=None, comment="",
               evidence_ids=()) -> dict:
        verdict = self._require_text(verdict, "评定结论")
        assessor = self._require_text(assessor, "评定人")
        with self.db.transaction() as conn:
            enrollment = self._get(conn, "enrollments", enrollment_id, "录取")
            if enrollment["status"] != "active":
                raise InvalidState(f"录取状态为 {enrollment['status']}，不能评定")
            snapshot = None
            if goal_seq is not None:
                snapshot = conn.execute(
                    "SELECT * FROM goal_snapshots WHERE enrollment_id = ? AND seq = ?",
                    (enrollment_id, goal_seq),
                ).fetchone()
                if snapshot is None:
                    raise NotFound(f"录取 {enrollment_id} 不存在阶段 {goal_seq} 的目标快照")
            evidence_rows = []
            for evidence_id in evidence_ids:
                row = self._get(conn, "evidence", evidence_id, "证据")
                if row["enrollment_id"] != enrollment_id:
                    raise ValidationError(f"证据 {evidence_id} 不属于该录取")
                if snapshot is not None and row["goal_snapshot_id"] != snapshot["id"]:
                    raise ValidationError(f"证据 {evidence_id} 不属于阶段 {goal_seq}")
                evidence_rows.append(row)
            cur = conn.execute(
                "INSERT INTO assessments"
                " (enrollment_id, goal_snapshot_id, verdict, score, assessor, comment, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (enrollment_id, snapshot["id"] if snapshot else None, verdict, score,
                 assessor, comment or "", _now()),
            )
            assessment_id = cur.lastrowid
            for row in evidence_rows:
                conn.execute(
                    "INSERT INTO assessment_evidence (assessment_id, evidence_id) VALUES (?,?)",
                    (assessment_id, row["id"]),
                )
            self._emit(conn, enrollment_id, assessor, "assessment_recorded",
                       {"assessment_id": assessment_id, "goal_seq": goal_seq,
                        "verdict": verdict, "evidence_ids": [r["id"] for r in evidence_rows]})
            if goal_seq is None and verdict in PASS_VERDICTS:
                self._release_slot(conn, enrollment["mentor_id"])
                conn.execute(
                    "UPDATE enrollments SET status = 'completed', closed_at = ? WHERE id = ?",
                    (_now(), enrollment_id),
                )
                self._emit(conn, enrollment_id, assessor, "enrollment_completed",
                           {"assessment_id": assessment_id})
            return self._basis_view(conn, assessment_id)

    def _basis_view(self, conn, assessment_id: int) -> dict:
        assessment = dict(self._get(conn, "assessments", assessment_id, "评定"))
        snapshot = None
        if assessment["goal_snapshot_id"] is not None:
            snapshot = dict(self._get(conn, "goal_snapshots",
                                      assessment["goal_snapshot_id"], "目标快照"))
        links = conn.execute(
            "SELECT evidence_id FROM assessment_evidence WHERE assessment_id = ? ORDER BY evidence_id",
            (assessment_id,),
        ).fetchall()
        evidence = []
        for link in links:
            row = dict(self._get(conn, "evidence", link["evidence_id"], "证据"))
            chain = []
            cursor = row
            while cursor["supersedes_id"] is not None:
                cursor = dict(self._get(conn, "evidence", cursor["supersedes_id"], "证据"))
                chain.append(cursor)
            row["superseded_chain"] = chain  # 由新到旧的补交历史
            evidence.append(row)
        return {"assessment": assessment, "goal_snapshot": snapshot, "evidence": evidence}

    def get_assessment_basis(self, assessment_id) -> dict:
        with self.db.transaction() as conn:
            return self._basis_view(conn, assessment_id)

    # ------------------------------------------------------------------
    # 历史与档案查询
    # ------------------------------------------------------------------
    def get_enrollment_history(self, enrollment_id) -> dict:
        """沿续接链回溯，返回跨学期的完整录取链与事件流。"""
        with self.db.transaction() as conn:
            chain = []
            cursor = self._get(conn, "enrollments", enrollment_id, "录取")
            while cursor is not None:
                chain.append(cursor["id"])
                if cursor["continued_from_id"] is None:
                    cursor = None
                else:
                    cursor = conn.execute(
                        "SELECT * FROM enrollments WHERE id = ?",
                        (cursor["continued_from_id"],),
                    ).fetchone()
            placeholders = ",".join("?" for _ in chain)
            events = conn.execute(
                f"SELECT * FROM events WHERE enrollment_id IN ({placeholders}) ORDER BY id",
                chain,
            ).fetchall()
            return {
                "enrollment_chain": [self._enrollment_view(conn, eid) for eid in chain],
                "events": [{**dict(e), "payload": json.loads(e["payload"])} for e in events],
            }

    def get_student_portfolio(self, student_id, guardian=None) -> dict:
        """学生档案：未成年人必须由其有效监护人查阅。"""
        with self.db.transaction() as conn:
            student = dict(self._get(conn, "students", student_id, "学生"))
            student["is_minor"] = self._is_minor(student)
            self._require_named_guardian(conn, student, "view", guardian)
            enrollments = conn.execute(
                "SELECT * FROM enrollments WHERE student_id = ? ORDER BY id",
                (student_id,),
            ).fetchall()
            views = []
            for enrollment in enrollments:
                view = self._enrollment_view(conn, enrollment["id"])
                view["evidence"] = [
                    dict(r) for r in conn.execute(
                        "SELECT * FROM evidence WHERE enrollment_id = ? ORDER BY id",
                        (enrollment["id"],),
                    ).fetchall()
                ]
                view["assessments"] = [
                    dict(r) for r in conn.execute(
                        "SELECT * FROM assessments WHERE enrollment_id = ? ORDER BY id",
                        (enrollment["id"],),
                    ).fetchall()
                ]
                views.append(view)
            return {"student": student, "enrollments": views}

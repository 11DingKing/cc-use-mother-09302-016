"""领域服务：所有写操作都在 ``BEGIN IMMEDIATE`` 事务内完成。

写事务串行化 + 只追加占座台账，保证：
- 多人同时录取时名额不会超发；
- 进程重启后余额由 ``SUM(delta)`` 直接复原；
- 录取时复制并哈希当期阶段目标，计划后续改版不影响在学学籍。
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime

from .errors import (
    AuthorizationError,
    CapacityFullError,
    ConflictError,
    NotFoundError,
    StateConflictError,
    ValidationError,
)
from .util import canonical_hash, content_hash, is_minor, new_id, now_iso, utcnow

SEAT_HOLDING_STATUSES = ("enrolled", "paused")
ACTIVE_LEARNING_STATUSES = ("enrolled", "paused", "mentor_invalid")


class ApprenticeshipService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ── 事务基础 ────────────────────────────────────────────────────────

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row:
        row = self.conn.execute(sql, params).fetchone()
        if row is None:
            raise NotFoundError("记录不存在")
        return row

    @staticmethod
    def _now() -> str:
        return now_iso()

    # ── 导师 ────────────────────────────────────────────────────────────

    def create_mentor(
        self, name: str, craft: str, qualification_no: str | None = None
    ) -> dict:
        if not name or not craft:
            raise ValidationError("导师姓名与技艺方向不能为空")
        mid = new_id("m")
        ts = self._now()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO mentors(id, name, craft, qualification_no, status,"
                " created_at, qualified_at) VALUES (?,?,?,?, 'active', ?, ?)",
                (mid, name, craft, qualification_no, ts, ts),
            )
            conn.execute(
                "INSERT INTO mentor_status_history(id, mentor_id, from_status,"
                " to_status, reason, changed_at) VALUES (?,?,?,?,?,?)",
                (new_id("msh"), mid, None, "active", "取得导师资格", ts),
            )
        return self.get_mentor(mid)

    def _mentor_row(self, conn: sqlite3.Connection, mentor_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM mentors WHERE id=?", (mentor_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"导师不存在：{mentor_id}")
        return row

    def get_mentor(self, mentor_id: str) -> dict:
        return dict(self._one("SELECT * FROM mentors WHERE id=?", (mentor_id,)))

    def _change_mentor_status(
        self,
        mentor_id: str,
        to_status: str,
        allowed_from: tuple[str, ...],
        reason: str,
        actor: str | None,
        *,
        release_students: bool = False,
    ) -> dict:
        ts = self._now()
        with self._tx() as conn:
            mentor = self._mentor_row(conn, mentor_id)
            if mentor["status"] not in allowed_from:
                raise StateConflictError(
                    f"导师当前状态 {mentor['status']} 不允许变更为 {to_status}"
                )
            conn.execute(
                "UPDATE mentors SET status=?, invalidated_at=? WHERE id=?",
                (to_status, ts if to_status == "invalid" else None, mentor_id),
            )
            conn.execute(
                "INSERT INTO mentor_status_history(id, mentor_id, from_status,"
                " to_status, reason, changed_at, changed_by) VALUES (?,?,?,?,?,?,?)",
                (new_id("msh"), mentor_id, mentor["status"], to_status, reason, ts, actor),
            )
            affected: list[str] = []
            if release_students:
                rows = conn.execute(
                    "SELECT * FROM enrollments WHERE mentor_id=? AND status IN ('enrolled','paused')",
                    (mentor_id,),
                ).fetchall()
                for enr in rows:
                    conn.execute(
                        "UPDATE enrollments SET status='mentor_invalid' WHERE id=?",
                        (enr["id"],),
                    )
                    self._ledger(conn, enr["plan_id"], mentor_id, enr["id"], -1, "mentor_invalidated")
                    self._event(
                        conn,
                        enr["id"],
                        "mentor_invalidated",
                        actor,
                        {"mentor_id": mentor_id, "reason": reason, "held_goal_hash": enr["goals_hash"]},
                        ts,
                    )
                    affected.append(enr["id"])
        result = self.get_mentor(mentor_id)
        result["affected_enrollments"] = affected
        return result

    def suspend_mentor(self, mentor_id: str, reason: str, actor: str | None = None) -> dict:
        """暂停执教：不强制清退在读学生（其等待后续转导师或复职）。"""
        return self._change_mentor_status(mentor_id, "suspended", ("active",), reason, actor)

    def resume_mentor(self, mentor_id: str, reason: str = "恢复执教", actor: str | None = None) -> dict:
        # 失效（停教/资格丧失）不能直接复职，需重新走资格登记，避免已释放名额的学籍被悄悄改回
        return self._change_mentor_status(
            mentor_id, "active", ("suspended",), reason, actor
        )

    def invalidate_mentor(self, mentor_id: str, reason: str, actor: str | None = None) -> dict:
        """导师失效（停教/资格丧失）：释放其全部在读名额，学生进入待转出状态。"""
        return self._change_mentor_status(
            mentor_id,
            "invalid",
            ("active", "suspended"),
            reason,
            actor,
            release_students=True,
        )

    # ── 计划版本与阶段目标 ──────────────────────────────────────────────

    def create_plan(
        self,
        craft: str,
        term: str,
        version_no: int,
        title: str,
        predecessor_id: str | None = None,
        actor: str | None = None,
    ) -> dict:
        if not craft or not term or not title:
            raise ValidationError("技艺、学期与标题不能为空")
        if predecessor_id:
            self._one("SELECT id FROM plans WHERE id=?", (predecessor_id,))
        pid = new_id("plan")
        ts = self._now()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO plans(id, craft, term, version_no, title, predecessor_id,"
                " status, created_by, created_at) VALUES (?,?,?,?,?,?, 'draft', ?,?)",
                (pid, craft, term, version_no, title, predecessor_id, actor, ts),
            )
        return self.get_plan(pid)

    def get_plan(self, plan_id: str) -> dict:
        return dict(self._one("SELECT * FROM plans WHERE id=?", (plan_id,)))

    def add_stage(
        self,
        plan_id: str,
        seq: int,
        code: str,
        name: str,
        description: str,
        due_date: str | None = None,
    ) -> dict:
        if due_date:
            date.fromisoformat(due_date)  # 校验格式
        sid = new_id("stg")
        with self._tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["status"] != "draft":
                raise StateConflictError("计划已发布，阶段目标不可修改；调整请新建计划版本")
            try:
                conn.execute(
                    "INSERT INTO stages(id, plan_id, seq, code, name, description, due_date)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (sid, plan_id, seq, code, name, description, due_date),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"阶段序号或编码冲突：{code}") from exc
        return dict(self._one("SELECT * FROM stages WHERE id=?", (sid,)))

    def set_capacity(self, plan_id: str, mentor_id: str, quota: int) -> dict:
        if quota < 0:
            raise ValidationError("名额不能为负")
        cid = new_id("cap")
        with self._tx() as conn:
            self._plan_row(conn, plan_id)
            mentor = self._mentor_row(conn, mentor_id)
            if mentor["status"] != "active":
                raise StateConflictError("只能为在聘导师分配名额")
            used = self._used(conn, plan_id, mentor_id)
            if quota < used:
                raise ValidationError(f"名额不能下调到低于已占用数：当前已占用 {used}")
            conn.execute(
                "INSERT INTO mentor_capacities(id, plan_id, mentor_id, quota)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(plan_id, mentor_id) DO UPDATE SET quota=excluded.quota",
                (cid, plan_id, mentor_id, quota),
            )
        return self.capacity_summary(plan_id, mentor_id)

    def _plan_row(self, conn: sqlite3.Connection, plan_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"计划不存在：{plan_id}")
        return row

    def publish_plan(self, plan_id: str) -> dict:
        with self._tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["status"] != "draft":
                raise StateConflictError("只有草稿计划可以发布")
            stages = conn.execute("SELECT COUNT(*) AS c FROM stages WHERE plan_id=?", (plan_id,)).fetchone()["c"]
            if stages == 0:
                raise ValidationError("发布前至少定义一个阶段目标")
            conn.execute(
                "UPDATE plans SET status='published', published_at=? WHERE id=?",
                (self._now(), plan_id),
            )
        return self.get_plan(plan_id)

    def close_plan(self, plan_id: str) -> dict:
        with self._tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["status"] != "published":
                raise StateConflictError("只有已发布计划可以结项关闭")
            conn.execute("UPDATE plans SET status='closed' WHERE id=?", (plan_id,))
        return self.get_plan(plan_id)

    # ── 学生与监护授权 ──────────────────────────────────────────────────

    def create_student(self, name: str, birth_date: str | None = None, contact: str | None = None) -> dict:
        if not name:
            raise ValidationError("学生姓名不能为空")
        if birth_date:
            date.fromisoformat(birth_date)
        sid = new_id("s")
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO students(id, name, birth_date, contact, created_at)"
                " VALUES (?,?,?,?,?)",
                (sid, name, birth_date, contact, self._now()),
            )
        return self.get_student(sid)

    def get_student(self, student_id: str) -> dict:
        return dict(self._one("SELECT * FROM students WHERE id=?", (student_id,)))

    def grant_consent(
        self,
        student_id: str,
        guardian_name: str,
        access_code: str,
        guardian_contact: str | None = None,
        expires_at: str | None = None,
        scope: str = "full",
        document: str | bytes | None = None,
    ) -> dict:
        if not guardian_name or not access_code:
            raise ValidationError("监护人与访问码不能为空")
        cid = new_id("gc")
        doc_hash = content_hash(document) if document else None
        with self._tx() as conn:
            self._student_row(conn, student_id)
            try:
                conn.execute(
                    "INSERT INTO guardian_consents(id, student_id, guardian_name,"
                    " guardian_contact, access_code, scope, document_hash, granted_at,"
                    " expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (cid, student_id, guardian_name, guardian_contact, access_code,
                     scope, doc_hash, self._now(), expires_at),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("访问码已存在") from exc
        return self.get_consent(cid)

    def get_consent(self, consent_id: str) -> dict:
        return dict(self._one("SELECT * FROM guardian_consents WHERE id=?", (consent_id,)))

    def revoke_consent(self, access_code: str) -> dict:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM guardian_consents WHERE access_code=?", (access_code,)
            ).fetchone()
            if row is None:
                raise NotFoundError("授权不存在")
            if row["revoked_at"]:
                raise StateConflictError("授权已撤销")
            conn.execute(
                "UPDATE guardian_consents SET revoked_at=? WHERE id=?",
                (self._now(), row["id"]),
            )
        return self.get_consent(row["id"])

    def _student_row(self, conn: sqlite3.Connection, student_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"学生不存在：{student_id}")
        return row

    def _valid_consent(self, conn: sqlite3.Connection, student_id: str) -> sqlite3.Row | None:
        row = conn.execute(
            "SELECT * FROM guardian_consents WHERE student_id=? AND revoked_at IS NULL"
            " ORDER BY granted_at DESC LIMIT 1",
            (student_id,),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] and row["expires_at"] < self._now():
            return None
        return row

    def check_guardian_access(self, student_id: str, access_code: str | None = None) -> dict:
        """访问闸门：未成年人必须持有效监护授权（访问码匹配）。成年人直接放行。"""
        student = self.get_student(student_id)
        if not is_minor(student["birth_date"]):
            return {"restricted": False, "reason": "成年学生"}
        row = self._valid_consent(self.conn, student_id)
        if row is None:
            raise AuthorizationError("该学生为未成年人，缺少有效监护授权")
        if not access_code or access_code != row["access_code"]:
            raise AuthorizationError("访问未成年学生数据必须提供匹配的监护授权访问码")
        return {
            "restricted": True,
            "reason": "未成年学生，已校验监护授权",
            "guardian_name": row["guardian_name"],
            "expires_at": row["expires_at"],
        }

    # ── 名额 ────────────────────────────────────────────────────────────

    def _quota_row(self, conn: sqlite3.Connection, plan_id: str, mentor_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM mentor_capacities WHERE plan_id=? AND mentor_id=?",
            (plan_id, mentor_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("该导师在此计划版本下没有招生名额配置")
        return row

    def _used(self, conn: sqlite3.Connection, plan_id: str, mentor_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(delta),0) AS used FROM seat_ledger WHERE plan_id=? AND mentor_id=?",
            (plan_id, mentor_id),
        ).fetchone()
        return int(row["used"])

    def _assert_room(self, conn: sqlite3.Connection, plan_id: str, mentor_id: str, need: int = 1) -> sqlite3.Row:
        quota = self._quota_row(conn, plan_id, mentor_id)
        used = self._used(conn, plan_id, mentor_id)
        if used + need > quota["quota"]:
            raise CapacityFullError(
                f"名额已满：名额 {quota['quota']}，已占用 {used}，本次需要 {need}"
            )
        return quota

    def capacity_summary(self, plan_id: str, mentor_id: str) -> dict:
        """余额实时由台账求和得出——重启后同样准确，无任何内存计数。"""
        with self._tx() as conn:
            quota = self._quota_row(conn, plan_id, mentor_id)
            used = self._used(conn, plan_id, mentor_id)
            held = conn.execute(
                "SELECT COUNT(*) AS c FROM enrollments WHERE plan_id=? AND mentor_id=?"
                " AND status IN ('enrolled','paused')",
                (plan_id, mentor_id),
            ).fetchone()["c"]
        return {
            "plan_id": plan_id,
            "mentor_id": mentor_id,
            "quota": quota["quota"],
            "used": used,
            "remaining": quota["quota"] - used,
            "active_enrollments": held,
        }

    # ── 录取（原子占座 + 当期目标固定）──────────────────────────────────

    def admit(self, student_id: str, plan_id: str, mentor_id: str, actor: str | None = None) -> dict:
        ts = self._now()
        with self._tx() as conn:
            student = self._student_row(conn, student_id)
            plan = self._plan_row(conn, plan_id)
            if plan["status"] != "published":
                raise StateConflictError("只能录取到已发布的计划版本")
            mentor = self._mentor_row(conn, mentor_id)
            if mentor["status"] != "active":
                raise StateConflictError("导师不在聘，不能录取")
            if is_minor(student["birth_date"]) and self._valid_consent(conn, student_id) is None:
                raise AuthorizationError("未成年学生录取前必须先登记有效监护授权")
            dup = conn.execute(
                "SELECT id, status FROM enrollments WHERE student_id=? AND plan_id=?",
                (student_id, plan_id),
            ).fetchone()
            if dup is not None:
                raise ConflictError(f"该学生已在此计划版本中存在学籍：{dup['id']}")
            # 占座与录取同一事务：并发下后到者在此处看到最新余额
            self._assert_room(conn, plan_id, mentor_id)

            stages = conn.execute(
                "SELECT * FROM stages WHERE plan_id=? ORDER BY seq", (plan_id,)
            ).fetchall()
            goals_payload = [
                {
                    "seq": s["seq"],
                    "code": s["code"],
                    "name": s["name"],
                    "description": s["description"],
                    "due_date": s["due_date"],
                }
                for s in stages
            ]
            goals_hash = canonical_hash(goals_payload)

            eid = new_id("e")
            conn.execute(
                "INSERT INTO enrollments(id, student_id, plan_id, mentor_id, status,"
                " admitted_at, goals_hash) VALUES (?,?,?,?,'enrolled',?,?)",
                (eid, student_id, plan_id, mentor_id, ts, goals_hash),
            )
            for g in goals_payload:
                conn.execute(
                    "INSERT INTO enrollment_goals(id, enrollment_id, seq, code, name,"
                    " description, due_date, content_hash) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        new_id("eg"), eid, g["seq"], g["code"], g["name"],
                        g["description"], g["due_date"], canonical_hash(g),
                    ),
                )
            self._ledger(conn, plan_id, mentor_id, eid, 1, "admit", ts=ts)
            self._event(
                conn, eid, "admitted", actor,
                {"plan_id": plan_id, "mentor_id": mentor_id, "goals_hash": goals_hash},
                ts,
            )
        return self.get_enrollment(eid, enforce_guardian=False)

    # ── 暂停 / 复学 ─────────────────────────────────────────────────────

    def create_pause_request(
        self, enrollment_id: str, kind: str, reason: str, requested_by: str | None = None
    ) -> dict:
        if kind not in ("pause", "resume"):
            raise ValidationError("kind 必须是 pause 或 resume")
        rid = new_id("pr")
        with self._tx() as conn:
            enr = self._enrollment_row(conn, enrollment_id)
            expected = "enrolled" if kind == "pause" else "paused"
            if enr["status"] != expected:
                raise StateConflictError(f"{kind} 申请要求学籍处于 {expected}，当前 {enr['status']}")
            conn.execute(
                "INSERT INTO pause_requests(id, enrollment_id, kind, reason, status,"
                " requested_by, requested_at) VALUES (?,?,?,?, 'pending', ?,?)",
                (rid, enrollment_id, kind, reason, requested_by, self._now()),
            )
        return self.get_pause_request(rid)

    def get_pause_request(self, request_id: str) -> dict:
        return dict(self._one("SELECT * FROM pause_requests WHERE id=?", (request_id,)))

    def decide_pause_request(
        self, request_id: str, approve: bool, actor: str | None = None, note: str | None = None
    ) -> dict:
        ts = self._now()
        with self._tx() as conn:
            req = conn.execute("SELECT * FROM pause_requests WHERE id=?", (request_id,)).fetchone()
            if req is None:
                raise NotFoundError("暂停申请不存在")
            if req["status"] != "pending":
                raise StateConflictError("该申请已处理")
            enr = self._enrollment_row(conn, req["enrollment_id"])
            new_status = "approved" if approve else "rejected"
            conn.execute(
                "UPDATE pause_requests SET status=?, decided_by=?, decided_at=?, decision_note=? WHERE id=?",
                (new_status, actor, ts, note, request_id),
            )
            if approve:
                target = "paused" if req["kind"] == "pause" else "enrolled"
                expect = "enrolled" if req["kind"] == "pause" else "paused"
                if enr["status"] != expect:
                    raise StateConflictError(f"学籍状态已变化（{enr['status']}），无法执行")
                conn.execute("UPDATE enrollments SET status=? WHERE id=?", (target, enr["id"]))
                # 暂停期间名额保留：不写台账，余额不变
                self._event(
                    conn, enr["id"], req["kind"], actor,
                    {"request_id": request_id, "reason": req["reason"], "note": note,
                     "seat_retained": 1},
                    ts,
                )
        return self.get_pause_request(request_id)

    # ── 转导师 ──────────────────────────────────────────────────────────

    def request_transfer(
        self,
        enrollment_id: str,
        to_mentor_id: str,
        reason: str,
        requested_by: str | None = None,
    ) -> dict:
        rid = new_id("tr")
        with self._tx() as conn:
            enr = self._enrollment_row(conn, enrollment_id)
            if enr["status"] not in ACTIVE_LEARNING_STATUSES:
                raise StateConflictError(f"学籍状态 {enr['status']} 不能申请转导师")
            target = self._mentor_row(conn, to_mentor_id)
            if target["id"] == enr["mentor_id"]:
                raise ValidationError("新导师不能与当前导师相同")
            if target["status"] != "active":
                raise StateConflictError("目标导师不在聘")
            self._quota_row(conn, enr["plan_id"], to_mentor_id)
            conn.execute(
                "INSERT INTO transfer_requests(id, enrollment_id, from_mentor_id,"
                " to_mentor_id, reason, status, requested_by, requested_at)"
                " VALUES (?,?,?,?,?, 'pending', ?,?)",
                (rid, enrollment_id, enr["mentor_id"], to_mentor_id, reason,
                 requested_by, self._now()),
            )
        return self.get_transfer_request(rid)

    def get_transfer_request(self, request_id: str) -> dict:
        return dict(self._one("SELECT * FROM transfer_requests WHERE id=?", (request_id,)))

    def decide_transfer_request(
        self, request_id: str, approve: bool, actor: str | None = None, note: str | None = None
    ) -> dict:
        ts = self._now()
        with self._tx() as conn:
            req = conn.execute("SELECT * FROM transfer_requests WHERE id=?", (request_id,)).fetchone()
            if req is None:
                raise NotFoundError("转导师申请不存在")
            if req["status"] != "pending":
                raise StateConflictError("该申请已处理")
            enr = self._enrollment_row(conn, req["enrollment_id"])
            if not approve:
                conn.execute(
                    "UPDATE transfer_requests SET status='rejected', decided_by=?,"
                    " decided_at=?, decision_note=? WHERE id=?",
                    (actor, ts, note, request_id),
                )
                self._event(conn, enr["id"], "transfer_rejected", actor,
                            {"request_id": request_id, "to_mentor_id": req["to_mentor_id"], "note": note}, ts)
                return self.get_transfer_request(request_id)

            if enr["status"] not in ACTIVE_LEARNING_STATUSES:
                raise StateConflictError(f"学籍状态 {enr['status']}，无法完成转导师")
            target = self._mentor_row(conn, req["to_mentor_id"])
            if target["status"] != "active":
                raise StateConflictError("目标导师已不在聘")
            held_seat = enr["status"] in SEAT_HOLDING_STATUSES
            # 容量检查在锁内完成；若导师已失效则此处只占新座
            self._assert_room(conn, enr["plan_id"], req["to_mentor_id"])
            if held_seat:
                self._ledger(conn, enr["plan_id"], req["from_mentor_id"], enr["id"], -1, "transfer_out", ts=ts)
            self._ledger(conn, enr["plan_id"], req["to_mentor_id"], enr["id"], 1, "transfer_in", ts=ts)

            new_status = "paused" if enr["status"] == "paused" else "enrolled"
            conn.execute(
                "UPDATE enrollments SET mentor_id=?, status=? WHERE id=?",
                (req["to_mentor_id"], new_status, enr["id"]),
            )
            conn.execute(
                "UPDATE transfer_requests SET status='approved', decided_by=?,"
                " decided_at=?, decision_note=? WHERE id=?",
                (actor, ts, note, request_id),
            )
            self._event(
                conn, enr["id"], "transferred", actor,
                {"request_id": request_id, "from_mentor_id": req["from_mentor_id"],
                 "to_mentor_id": req["to_mentor_id"], "reason": req["reason"],
                 "note": note, "goals_unchanged": True, "goals_hash": enr["goals_hash"]},
                ts,
            )
        return self.get_transfer_request(request_id)

    # ── 退出 ────────────────────────────────────────────────────────────

    def withdraw(self, enrollment_id: str, reason: str, actor: str | None = None) -> dict:
        ts = self._now()
        with self._tx() as conn:
            enr = self._enrollment_row(conn, enrollment_id)
            if enr["status"] not in ACTIVE_LEARNING_STATUSES:
                raise StateConflictError(f"学籍状态 {enr['status']} 不能退出")
            if enr["status"] in SEAT_HOLDING_STATUSES:
                self._ledger(conn, enr["plan_id"], enr["mentor_id"], enr["id"], -1, "withdraw", ts=ts)
            conn.execute("UPDATE enrollments SET status='withdrawn' WHERE id=?", (enrollment_id,))
            self._event(conn, enr["id"], "withdrawn", actor, {"reason": reason}, ts)
        return self.get_enrollment(enrollment_id, enforce_guardian=False)

    # ── 跨学期续接 ──────────────────────────────────────────────────────

    def continue_to_new_term(
        self,
        enrollment_id: str,
        new_plan_id: str,
        mentor_id: str,
        actor: str | None = None,
    ) -> dict:
        """把旧学籍归档到新版本：释放旧座、占新座、冻结新版本目标、链接前后学籍。"""
        ts = self._now()
        with self._tx() as conn:
            old = self._enrollment_row(conn, enrollment_id)
            if old["status"] not in ACTIVE_LEARNING_STATUSES + ("completed",):
                raise StateConflictError(f"学籍状态 {old['status']} 不能续接")
            new_plan = self._plan_row(conn, new_plan_id)
            if new_plan["id"] == old["plan_id"]:
                raise ValidationError("续接必须指向不同的计划版本")
            if new_plan["status"] != "published":
                raise StateConflictError("新版本计划尚未发布")
            mentor = self._mentor_row(conn, mentor_id)
            if mentor["status"] != "active":
                raise StateConflictError("导师不在聘")
            student = self._student_row(conn, old["student_id"])
            if is_minor(student["birth_date"]) and self._valid_consent(conn, student["id"]) is None:
                raise AuthorizationError("未成年学生续接前监护授权必须有效")
            dup = conn.execute(
                "SELECT id FROM enrollments WHERE student_id=? AND plan_id=?",
                (old["student_id"], new_plan_id),
            ).fetchone()
            if dup is not None:
                raise ConflictError("该学生在新版本中已有学籍")
            self._assert_room(conn, new_plan_id, mentor_id)

            stages = conn.execute("SELECT * FROM stages WHERE plan_id=? ORDER BY seq", (new_plan_id,)).fetchall()
            goals_payload = [
                {"seq": s["seq"], "code": s["code"], "name": s["name"],
                 "description": s["description"], "due_date": s["due_date"]}
                for s in stages
            ]
            goals_hash = canonical_hash(goals_payload)
            new_eid = new_id("e")
            conn.execute(
                "INSERT INTO enrollments(id, student_id, plan_id, mentor_id, status,"
                " admitted_at, goals_hash, predecessor_enrollment_id)"
                " VALUES (?,?,?,?,'enrolled',?,?,?)",
                (new_eid, old["student_id"], new_plan_id, mentor_id, ts, goals_hash, old["id"]),
            )
            for g in goals_payload:
                conn.execute(
                    "INSERT INTO enrollment_goals(id, enrollment_id, seq, code, name,"
                    " description, due_date, content_hash) VALUES (?,?,?,?,?,?,?,?)",
                    (new_id("eg"), new_eid, g["seq"], g["code"], g["name"],
                     g["description"], g["due_date"], canonical_hash(g)),
                )
            if old["status"] in SEAT_HOLDING_STATUSES:
                self._ledger(conn, old["plan_id"], old["mentor_id"], old["id"], -1, "continue_out", ts=ts)
            self._ledger(conn, new_plan_id, mentor_id, new_eid, 1, "continue_in", ts=ts)
            conn.execute("UPDATE enrollments SET status='transferred_out' WHERE id=?", (old["id"],))
            self._event(conn, old["id"], "continued_out", actor,
                        {"new_enrollment_id": new_eid, "new_plan_id": new_plan_id}, ts)
            self._event(conn, new_eid, "continued_in", actor,
                        {"predecessor_enrollment_id": old["id"], "previous_plan_id": old["plan_id"],
                         "previous_mentor_id": old["mentor_id"], "goals_hash": goals_hash}, ts)
        return self.get_enrollment(new_eid, enforce_guardian=False)

    # ── 学习证据与补交 ──────────────────────────────────────────────────

    def submit_evidence(
        self,
        enrollment_id: str,
        stage_code: str,
        kind: str,
        title: str,
        content: str = "",
        submitted_by: str | None = None,
        make_up_note: str | None = None,
    ) -> dict:
        ts = self._now()
        with self._tx() as conn:
            enr = self._enrollment_row(conn, enrollment_id)
            if enr["status"] not in ACTIVE_LEARNING_STATUSES:
                raise StateConflictError(f"学籍状态 {enr['status']} 不能提交证据")
            goal = conn.execute(
                "SELECT * FROM enrollment_goals WHERE enrollment_id=? AND code=?",
                (enrollment_id, stage_code),
            ).fetchone()
            if goal is None:
                raise NotFoundError(f"该学籍冻结的阶段目标中没有：{stage_code}")
            overdue = bool(goal["due_date"]) and goal["due_date"] < ts[:10]
            is_late = 1 if (overdue or make_up_note) else 0
            ev_id = new_id("ev")
            conn.execute(
                "INSERT INTO evidences(id, enrollment_id, stage_code, kind, title,"
                " content, content_hash, is_late, make_up_note, status, submitted_by,"
                " submitted_at) VALUES (?,?,?,?,?,?,?,?,?, 'submitted', ?,?)",
                (ev_id, enrollment_id, stage_code, kind, title, content,
                 content_hash(content), is_late, make_up_note, submitted_by, ts),
            )
            self._event(
                conn, enrollment_id, "evidence_submitted", submitted_by,
                {"evidence_id": ev_id, "stage_code": stage_code, "is_late": is_late,
                 "overdue": overdue, "make_up": bool(make_up_note)},
                ts,
            )
        return self.get_evidence(ev_id)

    def get_evidence(self, evidence_id: str) -> dict:
        return dict(self._one("SELECT * FROM evidences WHERE id=?", (evidence_id,)))

    def review_evidence(
        self, evidence_id: str, accept: bool, reviewer: str, note: str | None = None
    ) -> dict:
        ts = self._now()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM evidences WHERE id=?", (evidence_id,)).fetchone()
            if row is None:
                raise NotFoundError("证据不存在")
            if row["status"] != "submitted":
                raise StateConflictError("证据已评审")
            new_status = "accepted" if accept else "rejected"
            conn.execute(
                "UPDATE evidences SET status=?, reviewed_by=?, reviewed_at=?, review_note=? WHERE id=?",
                (new_status, reviewer, ts, note, evidence_id),
            )
            self._event(
                conn, row["enrollment_id"], "evidence_reviewed", reviewer,
                {"evidence_id": evidence_id, "result": new_status, "note": note}, ts,
            )
        return self.get_evidence(evidence_id)

    # ── 评定与依据还原 ──────────────────────────────────────────────────

    def _build_basis(self, conn: sqlite3.Connection, enrollment_id: str) -> dict:
        enr = self._enrollment_row(conn, enrollment_id)
        goals = [
            dict(g) for g in conn.execute(
                "SELECT seq, code, name, description, due_date, content_hash"
                " FROM enrollment_goals WHERE enrollment_id=? ORDER BY seq",
                (enrollment_id,),
            ).fetchall()
        ]
        evidences = [
            {
                "evidence_id": e["id"],
                "stage_code": e["stage_code"],
                "kind": e["kind"],
                "title": e["title"],
                "content_hash": e["content_hash"],
                "is_late": bool(e["is_late"]),
                "make_up_note": e["make_up_note"],
                "status": e["status"],
                "submitted_by": e["submitted_by"],
                "submitted_at": e["submitted_at"],
                "reviewed_by": e["reviewed_by"],
                "review_note": e["review_note"],
            }
            for e in conn.execute(
                "SELECT * FROM evidences WHERE enrollment_id=? ORDER BY submitted_at, id",
                (enrollment_id,),
            ).fetchall()
        ]
        return {
            "enrollment_id": enrollment_id,
            "plan_id": enr["plan_id"],
            "mentor_id": enr["mentor_id"],
            "goals_hash_at_admission": enr["goals_hash"],
            "frozen_goals": goals,
            "evidences": evidences,
        }

    def assess(
        self,
        enrollment_id: str,
        assessor: str,
        result: str,
        stage_code: str | None = None,
        grade: str | None = None,
        note: str | None = None,
    ) -> dict:
        if result not in ("pass", "fail", "conditional_pass"):
            raise ValidationError("评定结果非法")
        ts = self._now()
        with self._tx() as conn:
            enr = self._enrollment_row(conn, enrollment_id)
            if stage_code:
                goal = conn.execute(
                    "SELECT 1 FROM enrollment_goals WHERE enrollment_id=? AND code=?",
                    (enrollment_id, stage_code),
                ).fetchone()
                if goal is None:
                    raise NotFoundError(f"阶段目标不存在：{stage_code}")
            basis = self._build_basis(conn, enrollment_id)
            basis["snapshot_at"] = ts
            basis_json = json.dumps(basis, ensure_ascii=False, sort_keys=True)
            aid = new_id("as")
            conn.execute(
                "INSERT INTO assessments(id, enrollment_id, stage_code, result, grade,"
                " assessor, note, basis, basis_hash, assessed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (aid, enrollment_id, stage_code, result, grade, assessor, note,
                 basis_json, canonical_hash(basis), ts),
            )
            self._event(
                conn, enrollment_id, "assessed", assessor,
                {"assessment_id": aid, "result": result, "stage_code": stage_code,
                 "basis_hash": canonical_hash(basis)},
                ts,
            )
            if stage_code is None and result == "pass":
                # 整体通过且无指定阶段 => 结业
                conn.execute("UPDATE enrollments SET status='completed' WHERE id=?", (enrollment_id,))
                self._event(conn, enrollment_id, "completed", assessor,
                            {"assessment_id": aid}, ts)
        return self.get_assessment(aid)

    def get_assessment(self, assessment_id: str) -> dict:
        row = self._one("SELECT * FROM assessments WHERE id=?", (assessment_id,))
        data = dict(row)
        data["basis"] = json.loads(data["basis"])
        return data

    def reconstruct_assessment(self, assessment_id: str) -> dict:
        """还原评定依据：

        - ``stored_basis``：评定当时冻结的依据与哈希，复验哈希是否一致；
        - ``rebuilt_basis``：按当前库内数据重建的依据；
        - 两者比对可以发现评定后证据/状态是否发生过变化。
        """
        stored = self.get_assessment(assessment_id)
        stored_hash_ok = canonical_hash(stored["basis"]) == stored["basis_hash"]
        with self._tx() as conn:
            rebuilt = self._build_basis(conn, stored["enrollment_id"])
        # 快照时间是评定时元数据，重建时不存在，比对时剔除
        stored_cmp = {k: v for k, v in stored["basis"].items() if k != "snapshot_at"}
        return {
            "assessment_id": assessment_id,
            "result": stored["result"],
            "stored_basis": stored["basis"],
            "stored_basis_hash": stored["basis_hash"],
            "stored_hash_valid": stored_hash_ok,
            "rebuilt_basis": rebuilt,
            "rebuilt_basis_hash": canonical_hash(rebuilt),
            "basis_unchanged_since_assessment": canonical_hash(stored_cmp) == canonical_hash(rebuilt),
        }

    # ── 查询 ────────────────────────────────────────────────────────────

    def _enrollment_row(self, conn: sqlite3.Connection, enrollment_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM enrollments WHERE id=?", (enrollment_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"学籍不存在：{enrollment_id}")
        return row

    def get_enrollment(
        self,
        enrollment_id: str,
        access_code: str | None = None,
        enforce_guardian: bool = True,
    ) -> dict:
        """读取学籍（含目标与历史）。

        外部读取（API/CLI）默认经过监护授权闸门；内部写操作回读时传
        ``enforce_guardian=False``，避免录取成功却因调用方未带访问码而报错。
        """
        enr = dict(self._one("SELECT * FROM enrollments WHERE id=?", (enrollment_id,)))
        access = (
            self.check_guardian_access(enr["student_id"], access_code)
            if enforce_guardian
            else {"enforced": False}
        )
        enr["access"] = access
        enr["goals"] = [
            dict(r) for r in self.conn.execute(
                "SELECT seq, code, name, description, due_date, content_hash"
                " FROM enrollment_goals WHERE enrollment_id=? ORDER BY seq",
                (enrollment_id,),
            ).fetchall()
        ]
        enr["history"] = self.history(enrollment_id)
        return enr

    def history(self, enrollment_id: str) -> list[dict]:
        """连续历史：事件流（录取/暂停/转导师/退出/失效/续接/证据/评定）。"""
        self._one("SELECT id FROM enrollments WHERE id=?", (enrollment_id,))
        rows = self.conn.execute(
            "SELECT seq, event_type, actor, payload, occurred_at"
            " FROM enrollment_events WHERE enrollment_id=? ORDER BY seq",
            (enrollment_id,),
        ).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def lineage(self, enrollment_id: str) -> list[dict]:
        """沿 predecessor 链回溯跨学期续接链条。"""
        chain = []
        current = enrollment_id
        with self._tx() as conn:
            while current:
                row = conn.execute(
                    "SELECT e.id, e.plan_id, p.term, p.version_no, p.title, e.mentor_id,"
                    " e.status, e.predecessor_enrollment_id, e.admitted_at, e.goals_hash"
                    " FROM enrollments e JOIN plans p ON p.id=e.plan_id WHERE e.id=?",
                    (current,),
                ).fetchone()
                if row is None:
                    break
                chain.append(dict(row))
                current = row["predecessor_enrollment_id"]
        return chain

    def list_evidences(self, enrollment_id: str, access_code: str | None = None) -> list[dict]:
        enr = self._one("SELECT student_id FROM enrollments WHERE id=?", (enrollment_id,))
        self.check_guardian_access(enr["student_id"], access_code)
        return [
            dict(r) for r in self.conn.execute(
                "SELECT * FROM evidences WHERE enrollment_id=? ORDER BY submitted_at",
                (enrollment_id,),
            ).fetchall()
        ]

    # ── 内部：台账与事件 ────────────────────────────────────────────────

    def _ledger(
        self,
        conn: sqlite3.Connection,
        plan_id: str,
        mentor_id: str,
        enrollment_id: str,
        delta: int,
        reason: str,
        ts: str | None = None,
    ) -> None:
        try:
            conn.execute(
                "INSERT INTO seat_ledger(plan_id, mentor_id, enrollment_id, delta,"
                " reason, ref_type, created_at) VALUES (?,?,?,?,?, 'seat', ?)",
                (plan_id, mentor_id, enrollment_id, delta, reason, ts or self._now()),
            )
        except sqlite3.IntegrityError as exc:  # 触发器兜底：超发/负余额
            if "SEAT_" in str(exc):
                raise CapacityFullError("容量约束被触发，名额余额越界") from exc
            raise

    def _event(
        self,
        conn: sqlite3.Connection,
        enrollment_id: str,
        event_type: str,
        actor: str | None,
        payload: dict,
        ts: str | None = None,
    ) -> None:
        next_seq = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS n FROM enrollment_events WHERE enrollment_id=?",
            (enrollment_id,),
        ).fetchone()["n"]
        conn.execute(
            "INSERT INTO enrollment_events(enrollment_id, seq, event_type, actor,"
            " payload, occurred_at) VALUES (?,?,?,?,?,?)",
            (enrollment_id, next_seq, event_type, actor,
             json.dumps(payload, ensure_ascii=False, sort_keys=True), ts or self._now()),
        )

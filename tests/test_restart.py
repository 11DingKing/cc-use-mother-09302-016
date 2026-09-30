"""进程重启（关闭并重开数据库）后的状态持久性测试。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mentorship import CapacityExhausted, Database, MentorshipService


class RestartTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "mentorship.sqlite3"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _open(self) -> MentorshipService:
        return MentorshipService(Database(self.path))

    def test_capacity_and_history_survive_restart(self) -> None:
        svc = self._open()
        plan = svc.create_plan_version("PR-001", "重启测试计划")
        svc.add_stage_goal(plan["id"], 1, "阶段1")
        plan = svc.publish_plan_version(plan["id"])
        mentor = svc.create_mentor("张师傅", capacity_total=3)
        students = [svc.create_student(f"学生{i}", f"2001-05-0{i + 1}") for i in range(4)]
        first = svc.enroll(students[0]["id"], mentor["id"], plan["id"])
        svc.enroll(students[1]["id"], mentor["id"], plan["id"])
        svc.submit_evidence(first["id"], 1, "uri://before-restart")
        svc.db.close()

        # 模拟进程重启：重新打开同一数据库文件
        svc = self._open()
        capacity = svc.get_mentor_capacity(mentor["id"])
        self.assertEqual(capacity["capacity_used"], 2)
        self.assertEqual(capacity["remaining"], 1)
        svc.enroll(students[2]["id"], mentor["id"], plan["id"])
        with self.assertRaises(CapacityExhausted):
            svc.enroll(students[3]["id"], mentor["id"], plan["id"])
        history = svc.get_enrollment_history(first["id"])
        self.assertIn("evidence_submitted", [e["type"] for e in history["events"]])
        svc.withdraw(first["id"], "退出")
        svc.db.close()

        # 再次重启：退出释放的名额仍然正确
        svc = self._open()
        capacity = svc.get_mentor_capacity(mentor["id"])
        self.assertEqual(capacity["capacity_used"], 2)
        enrollment = svc.get_enrollment(first["id"])
        self.assertEqual(enrollment["status"], "withdrawn")
        self.assertEqual(len(enrollment["goal_snapshots"]), 1)
        svc.db.close()


if __name__ == "__main__":
    unittest.main()

"""并发录取下的名额准确性测试。"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mentorship import CapacityExhausted, Database, MentorshipService


class ConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.svc = MentorshipService(self.db)
        plan = self.svc.create_plan_version("PC-001", "并发测试计划")
        self.svc.add_stage_goal(plan["id"], 1, "阶段1")
        self.plan = self.svc.publish_plan_version(plan["id"])

    def tearDown(self) -> None:
        self.db.close()

    def _students(self, count: int) -> list[int]:
        return [self.svc.create_student(f"学生{i}", "2000-01-01")["id"] for i in range(count)]

    def test_concurrent_enrollment_never_exceeds_capacity(self) -> None:
        mentor = self.svc.create_mentor("张师傅", capacity_total=4)
        student_ids = self._students(12)
        barrier = threading.Barrier(len(student_ids))
        succeeded: list[int] = []
        rejected: list[int] = []
        lock = threading.Lock()

        def worker(student_id: int) -> None:
            barrier.wait()
            try:
                self.svc.enroll(student_id, mentor["id"], self.plan["id"])
                with lock:
                    succeeded.append(student_id)
            except CapacityExhausted:
                with lock:
                    rejected.append(student_id)

        threads = [threading.Thread(target=worker, args=(sid,)) for sid in student_ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(succeeded), 4)
        self.assertEqual(len(rejected), 8)
        capacity = self.svc.get_mentor_capacity(mentor["id"])
        self.assertEqual(capacity["capacity_used"], 4)
        self.assertEqual(capacity["remaining"], 0)

    def test_concurrent_enroll_and_withdraw_stays_consistent(self) -> None:
        mentor = self.svc.create_mentor("李师傅", capacity_total=3)
        student_ids = self._students(6)
        enrollments = [
            self.svc.enroll(sid, mentor["id"], self.plan["id"]) for sid in student_ids[:3]
        ]
        barrier = threading.Barrier(6)
        errors: list[Exception] = []

        def enroll_worker(student_id: int) -> None:
            barrier.wait()
            try:
                self.svc.enroll(student_id, mentor["id"], self.plan["id"])
            except CapacityExhausted:
                pass
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def withdraw_worker(enrollment_id: int) -> None:
            barrier.wait()
            self.svc.withdraw(enrollment_id, "退出")

        threads = [
            threading.Thread(target=enroll_worker, args=(sid,)) for sid in student_ids[3:]
        ] + [
            threading.Thread(target=withdraw_worker, args=(e["id"],)) for e in enrollments
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        capacity = self.svc.get_mentor_capacity(mentor["id"])
        self.assertGreaterEqual(capacity["capacity_used"], 0)
        self.assertLessEqual(capacity["capacity_used"], 3)
        # 与按在读录取重算的结果一致
        reconciled = self.svc.reconcile_capacity(mentor["id"])
        self.assertEqual(reconciled["capacity_used"], capacity["capacity_used"])


if __name__ == "__main__":
    unittest.main()

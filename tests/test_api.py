"""HTTP 接口端到端冒烟测试。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mentorship import Database, MentorshipService
from mentorship.api import make_server


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = Database(":memory:")
        cls.server = make_server(MentorshipService(cls.db), "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.close()

    def _request(self, method: str, path: str, body: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _post(self, path: str, body: dict | None = None):
        return self._request("POST", path, body or {})

    def _get(self, path: str):
        return self._request("GET", path)

    def test_full_journey_over_http(self) -> None:
        # 计划版本：创建 → 目标 → 发布
        status, plan = self._post("/plan-versions", {"code": "API-001", "title": "秋季计划"})
        self.assertEqual(status, 201)
        status, _ = self._post(f"/plan-versions/{plan['id']}/goals",
                               {"seq": 1, "title": "基本功", "required_evidence": 1})
        self.assertEqual(status, 201)
        status, plan = self._post(f"/plan-versions/{plan['id']}/publish")
        self.assertEqual(plan["status"], "published")

        # 导师与未成年学生
        status, mentor = self._post("/mentors", {"name": "张师傅", "capacity_total": 1})
        self.assertEqual(status, 201)
        status, minor = self._post("/students", {"name": "小明", "birth_date": "2018-03-01"})
        self.assertTrue(minor["is_minor"])

        # 无监护授权 → 403
        status, error = self._post("/enrollments", {
            "student_id": minor["id"], "mentor_id": mentor["id"],
            "plan_version_id": plan["id"]})
        self.assertEqual(status, 403)
        self.assertEqual(error["error"]["code"], "guardian_required")

        # 补授权后录取成功，名额被占用
        self._post(f"/students/{minor['id']}/guardian-authorizations",
                   {"guardian_name": "父亲", "scope": "all"})
        status, enrollment = self._post("/enrollments", {
            "student_id": minor["id"], "mentor_id": mentor["id"],
            "plan_version_id": plan["id"]})
        self.assertEqual(status, 201)
        self.assertEqual(len(enrollment["goal_snapshots"]), 1)
        status, capacity = self._get(f"/mentors/{mentor['id']}/capacity")
        self.assertEqual((capacity["capacity_used"], capacity["remaining"]), (1, 0))

        # 容量满 → 409
        status, adult = self._post("/students", {"name": "成人", "birth_date": "1990-01-01"})
        status, error = self._post("/enrollments", {
            "student_id": adult["id"], "mentor_id": mentor["id"],
            "plan_version_id": plan["id"]})
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "capacity_exhausted")

        # 未成年人提交证据需监护人；补交形成链条
        status, error = self._post(f"/enrollments/{enrollment['id']}/evidence",
                                   {"goal_seq": 1, "content_uri": "uri://v1"})
        self.assertEqual(status, 403)
        status, first = self._post(f"/enrollments/{enrollment['id']}/evidence",
                                   {"goal_seq": 1, "content_uri": "uri://v1",
                                    "guardian": "父亲"})
        self.assertEqual(status, 201)
        status, second = self._post(f"/enrollments/{enrollment['id']}/evidence",
                                    {"goal_seq": 1, "content_uri": "uri://v2",
                                     "guardian": "父亲", "supersedes_id": first["id"]})
        self.assertEqual(second["supersedes_id"], first["id"])

        # 导师失效 → 转导师 → 目标与证据不断档
        self._post(f"/mentors/{mentor['id']}/invalidate", {"reason": "停教"})
        status, new_mentor = self._post("/mentors", {"name": "李师傅", "capacity_total": 1})
        status, transfer = self._post(f"/enrollments/{enrollment['id']}/transfer-requests",
                                      {"to_mentor_id": new_mentor["id"], "reason": "导师失效"})
        self.assertEqual(status, 201)
        status, _ = self._post(f"/transfer-requests/{transfer['id']}/decide",
                               {"approve": True})
        self.assertEqual(status, 200)
        status, moved = self._get(f"/enrollments/{enrollment['id']}")
        self.assertEqual(moved["mentor_id"], new_mentor["id"])
        status, history = self._get(f"/enrollments/{enrollment['id']}/history")
        types = [e["type"] for e in history["events"]]
        self.assertIn("mentor_invalidated_notice", types)
        self.assertIn("transfer_approved", types)

        # 评定并还原依据
        status, result = self._post(f"/enrollments/{enrollment['id']}/assessments", {
            "verdict": "pass", "assessor": "mentor:李师傅", "goal_seq": 1,
            "score": 90, "evidence_ids": [second["id"]]})
        self.assertEqual(status, 201)
        status, basis = self._get(f"/assessments/{result['assessment']['id']}/basis")
        self.assertEqual(basis["goal_snapshot"]["title"], "基本功")
        self.assertEqual(basis["evidence"][0]["superseded_chain"][0]["content_uri"],
                         "uri://v1")

        # 未成年人档案需监护人查阅
        status, _ = self._get(f"/students/{minor['id']}/portfolio")
        self.assertEqual(status, 403)
        guardian = urllib.parse.quote("父亲")
        status, portfolio = self._get(f"/students/{minor['id']}/portfolio?guardian={guardian}")
        self.assertEqual(status, 200)
        self.assertEqual(len(portfolio["enrollments"]), 1)

        # 不存在的资源 → 404
        status, error = self._get("/enrollments/9999")
        self.assertEqual(status, 404)
        status, error = self._get("/no-such-route")
        self.assertEqual(status, 404)

    def test_bad_json_returns_400(self) -> None:
        url = f"http://127.0.0.1:{self.port}/plan-versions"
        request = urllib.request.Request(url, data=b"{not json", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()

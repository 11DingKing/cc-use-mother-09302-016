"""HTTP API 集成测试：真实启动线程内服务器走完整请求链路。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from apprenticeship.db import connect, initialize_database  # noqa: E402
from apprenticeship.http_app import make_handler  # noqa: E402


class HttpTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "app.db")
        conn = connect(self.db_path)
        initialize_database(conn)
        conn.close()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.db_path))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def data(self, method: str, path: str, body: dict | None = None) -> dict:
        status, payload = self.request(method, path, body)
        self.assertEqual(status, 200, payload)
        return payload["data"]


class HttpWorkflowTest(HttpTestBase):
    def test_full_workflow_over_http(self) -> None:
        m = self.data("POST", "/mentors", {"name": "陆师傅", "craft": "竹编"})
        plan = self.data("POST", "/plans", {"craft": "竹编", "term": "2026春", "version_no": 1, "title": "v1"})
        self.data("POST", f"/plans/{plan['id']}/stages",
                  {"seq": 1, "code": "S1", "name": "选材", "description": "杀青", "due_date": "2026-04-30"})
        self.data("PUT", f"/plans/{plan['id']}/mentors/{m['id']}/capacity", {"quota": 1})
        self.data("POST", f"/plans/{plan['id']}/publish")

        minor = self.data("POST", "/students", {"name": "李小军", "birth_date": "2012-08-20"})

        # 未成年人无监护授权：录取被拒
        status, err = self.request("POST", "/enrollments/admit",
                                   {"student_id": minor["id"], "plan_id": plan["id"], "mentor_id": m["id"]})
        self.assertEqual(status, 403)
        self.assertEqual(err["error"], "AuthorizationError")

        self.data("POST", f"/students/{minor['id']}/consents",
                  {"guardian_name": "李父", "access_code": "CODE-9"})
        enr = self.data("POST", "/enrollments/admit",
                        {"student_id": minor["id"], "plan_id": plan["id"], "mentor_id": m["id"]})

        # 容量已满
        adult = self.data("POST", "/students", {"name": "韩梅"})
        status, err = self.request("POST", "/enrollments/admit",
                                   {"student_id": adult["id"], "plan_id": plan["id"], "mentor_id": m["id"]})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "CapacityFullError")

        # 访问闸门：读未成年学籍必须带码
        status, err = self.request("GET", f"/enrollments/{enr['id']}")
        self.assertEqual(status, 403)
        got = self.data("GET", f"/enrollments/{enr['id']}?access_code=CODE-9")
        self.assertEqual(len(got["goals"]), 1)

        # 补交证据 → 评定 → 还原依据
        ev = self.data("POST", f"/enrollments/{enr['id']}/evidences",
                       {"stage_code": "S1", "kind": "photo", "title": "选材补交", "make_up_note": "补拍"})
        self.assertEqual(ev["is_late"], 1)
        self.data("POST", f"/evidences/{ev['id']}/review", {"accept": True, "reviewer": m["id"]})
        assessment = self.data("POST", f"/enrollments/{enr['id']}/assessments",
                               {"assessor": m["id"], "result": "conditional_pass", "stage_code": "S1"})
        recon = self.data("GET", f"/assessments/{assessment['id']}/reconstruct")
        self.assertTrue(recon["stored_hash_valid"])
        self.assertTrue(recon["basis_unchanged_since_assessment"])

        # 健康检查与 404
        self.assertEqual(self.data("GET", "/health")["status"], "ok")
        status, _ = self.request("GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()

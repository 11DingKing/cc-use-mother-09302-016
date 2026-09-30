"""HTTP API（仅依赖标准库）。

每个请求使用独立 SQLite 连接；写入端由服务层的 ``BEGIN IMMEDIATE``
与占座台账触发器共同保证并发安全。

启动：``python -m apprenticeship.http --db data/app.db --port 8080``
"""
from __future__ import annotations

import json
import re
import sqlite3
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .db import connect, initialize_database
from .errors import DomainError
from .service import ApprenticeshipService

Handler = Callable[[ApprenticeshipService, dict, dict, dict], Any]


def _bool(body: dict, key: str, default: bool = False) -> bool:
    value = body.get(key, default)
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes", "批准", "通过")


class Router:
    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Handler]] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        self.routes.append((method, re.compile(r"^" + pattern + r"$"), handler))

    def match(self, method: str, path: str):
        for m, regex, handler in self.routes:
            if m != method:
                continue
            match = regex.match(path)
            if match:
                return handler, match.groupdict()
        return None, None


def build_router() -> Router:
    r = Router()

    # 导师
    r.add("POST", r"/mentors", lambda s, b, q, p: s.create_mentor(b["name"], b["craft"], b.get("qualification_no")))
    r.add("GET", r"/mentors/(?P<id>[^/]+)", lambda s, b, q, p: s.get_mentor(p["id"]))
    r.add("POST", r"/mentors/(?P<id>[^/]+)/suspend", lambda s, b, q, p: s.suspend_mentor(p["id"], b["reason"], b.get("actor")))
    r.add("POST", r"/mentors/(?P<id>[^/]+)/resume", lambda s, b, q, p: s.resume_mentor(p["id"], b.get("reason", "恢复执教"), b.get("actor")))
    r.add("POST", r"/mentors/(?P<id>[^/]+)/invalidate", lambda s, b, q, p: s.invalidate_mentor(p["id"], b["reason"], b.get("actor")))

    # 计划版本 / 阶段 / 名额
    r.add("POST", r"/plans", lambda s, b, q, p: s.create_plan(
        b["craft"], b["term"], int(b["version_no"]), b["title"], b.get("predecessor_id"), b.get("actor")))
    r.add("GET", r"/plans/(?P<id>[^/]+)", lambda s, b, q, p: s.get_plan(p["id"]))
    r.add("POST", r"/plans/(?P<id>[^/]+)/stages", lambda s, b, q, p: s.add_stage(
        p["id"], int(b["seq"]), b["code"], b["name"], b["description"], b.get("due_date")))
    r.add("PUT", r"/plans/(?P<id>[^/]+)/mentors/(?P<mid>[^/]+)/capacity",
          lambda s, b, q, p: s.set_capacity(p["id"], p["mid"], int(b["quota"])))
    r.add("GET", r"/plans/(?P<id>[^/]+)/mentors/(?P<mid>[^/]+)/capacity",
          lambda s, b, q, p: s.capacity_summary(p["id"], p["mid"]))
    r.add("POST", r"/plans/(?P<id>[^/]+)/publish", lambda s, b, q, p: s.publish_plan(p["id"]))
    r.add("POST", r"/plans/(?P<id>[^/]+)/close", lambda s, b, q, p: s.close_plan(p["id"]))

    # 学生 / 监护授权
    r.add("POST", r"/students", lambda s, b, q, p: s.create_student(
        b["name"], b.get("birth_date"), b.get("contact")))
    r.add("GET", r"/students/(?P<id>[^/]+)", lambda s, b, q, p: s.get_student(p["id"]))
    r.add("POST", r"/students/(?P<id>[^/]+)/consents", lambda s, b, q, p: s.grant_consent(
        p["id"], b["guardian_name"], b["access_code"], b.get("guardian_contact"),
        b.get("expires_at"), b.get("scope", "full"), b.get("document")))
    r.add("POST", r"/consents/revoke", lambda s, b, q, p: s.revoke_consent(b["access_code"]))
    r.add("GET", r"/students/(?P<id>[^/]+)/access",
          lambda s, b, q, p: s.check_guardian_access(p["id"], q.get("access_code", [None])[0]))

    # 学籍
    r.add("POST", r"/enrollments/admit", lambda s, b, q, p: s.admit(
        b["student_id"], b["plan_id"], b["mentor_id"], b.get("actor")))
    r.add("GET", r"/enrollments/(?P<id>[^/]+)",
          lambda s, b, q, p: s.get_enrollment(p["id"], q.get("access_code", [None])[0]))
    r.add("GET", r"/enrollments/(?P<id>[^/]+)/history",
          lambda s, b, q, p: s.history(p["id"]))
    r.add("GET", r"/enrollments/(?P<id>[^/]+)/lineage", lambda s, b, q, p: s.lineage(p["id"]))
    r.add("POST", r"/enrollments/(?P<id>[^/]+)/pause-requests", lambda s, b, q, p: s.create_pause_request(
        p["id"], b["kind"], b["reason"], b.get("requested_by")))
    r.add("POST", r"/enrollments/(?P<id>[^/]+)/transfer-requests", lambda s, b, q, p: s.request_transfer(
        p["id"], b["to_mentor_id"], b["reason"], b.get("requested_by")))
    r.add("POST", r"/enrollments/(?P<id>[^/]+)/withdraw",
          lambda s, b, q, p: s.withdraw(p["id"], b["reason"], b.get("actor")))
    r.add("POST", r"/enrollments/(?P<id>[^/]+)/continue", lambda s, b, q, p: s.continue_to_new_term(
        p["id"], b["new_plan_id"], b["mentor_id"], b.get("actor")))
    r.add("POST", r"/enrollments/(?P<id>[^/]+)/evidences", lambda s, b, q, p: s.submit_evidence(
        p["id"], b["stage_code"], b["kind"], b["title"], b.get("content", ""),
        b.get("submitted_by"), b.get("make_up_note")))
    r.add("GET", r"/enrollments/(?P<id>[^/]+)/evidences",
          lambda s, b, q, p: s.list_evidences(p["id"], q.get("access_code", [None])[0]))
    r.add("POST", r"/enrollments/(?P<id>[^/]+)/assessments", lambda s, b, q, p: s.assess(
        p["id"], b["assessor"], b["result"], b.get("stage_code"), b.get("grade"), b.get("note")))

    # 申请决策
    r.add("POST", r"/pause-requests/(?P<id>[^/]+)/decision",
          lambda s, b, q, p: s.decide_pause_request(p["id"], _bool(b, "approve"), b.get("actor"), b.get("note")))
    r.add("POST", r"/transfer-requests/(?P<id>[^/]+)/decision",
          lambda s, b, q, p: s.decide_transfer_request(p["id"], _bool(b, "approve"), b.get("actor"), b.get("note")))

    # 证据 / 评定
    r.add("GET", r"/evidences/(?P<id>[^/]+)", lambda s, b, q, p: s.get_evidence(p["id"]))
    r.add("POST", r"/evidences/(?P<id>[^/]+)/review",
          lambda s, b, q, p: s.review_evidence(p["id"], _bool(b, "accept"), b["reviewer"], b.get("note")))
    r.add("GET", r"/assessments/(?P<id>[^/]+)", lambda s, b, q, p: s.get_assessment(p["id"]))
    r.add("GET", r"/assessments/(?P<id>[^/]+)/reconstruct", lambda s, b, q, p: s.reconstruct_assessment(p["id"]))

    # 健康检查
    r.add("GET", r"/health", lambda s, b, q, p: {"status": "ok"})
    return r


ROUTER = build_router()


def make_handler(db_path: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ApprenticeshipHTTP/1.0"

        def _send(self, status: int, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                value = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise DomainError(f"请求体不是合法 JSON：{exc}") from exc
            if not isinstance(value, dict):
                raise DomainError("请求体必须是 JSON 对象")
            return value

        def _handle(self, method: str) -> None:
            conn: sqlite3.Connection | None = None
            try:
                parts = urlsplit(self.path)
                path = parts.path.rstrip("/") or "/"
                handler, params = ROUTER.match(method, path)
                if handler is None:
                    self._send(HTTPStatus.NOT_FOUND,
                               {"error": "not_found", "message": f"无此路由：{method} {parts.path}"})
                    return
                query = parse_qs(parts.query)
                body = self._read_body() if method in ("POST", "PUT", "PATCH") else {}
                conn = connect(db_path)
                service = ApprenticeshipService(conn)
                result = handler(service, body, query, params or {})
                self._send(HTTPStatus.OK, {"data": result})
            except DomainError as exc:
                self._send(exc.http_status, {"error": type(exc).__name__, "message": str(exc)})
            except (KeyError, ValueError, TypeError) as exc:
                self._send(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001 - 边界层兜底
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error", "message": str(exc)})
            finally:
                if conn is not None:
                    conn.close()

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._handle("PUT")

        def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认日志
            return

    return Handler


def run(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    conn = connect(db_path)
    try:
        initialize_database(conn)
    finally:
        conn.close()
    server = ThreadingHTTPServer((host, port), make_handler(db_path))
    print(f"师徒计划后端已启动：http://{host}:{port}（数据库 {db_path}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="传统技艺师徒计划后端 HTTP 服务")
    parser.add_argument("--db", default="data/app.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    run(args.db, args.host, args.port)


if __name__ == "__main__":
    main()

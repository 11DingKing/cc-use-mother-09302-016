"""HTTP 接口层：基于标准库的 JSON API，仅做参数搬运与错误映射。"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import DomainError


def _actor(body: dict) -> str:
    return body.get("actor") or "admin:api"


# ---- 路由处理函数：签名为 (service, body, query, **path_params) ----

def _create_plan(svc, b, q):
    return 201, svc.create_plan_version(b.get("code"), b.get("title"), actor=_actor(b))


def _get_plan(svc, b, q, id):
    return svc.get_plan_version(id)


def _add_goal(svc, b, q, id):
    return 201, svc.add_stage_goal(id, b.get("seq"), b.get("title"), b.get("detail", ""),
                                   b.get("required_evidence", 1), actor=_actor(b))


def _publish_plan(svc, b, q, id):
    return svc.publish_plan_version(id, actor=_actor(b))


def _archive_plan(svc, b, q, id):
    return svc.archive_plan_version(id, actor=_actor(b))


def _create_mentor(svc, b, q):
    return 201, svc.create_mentor(b.get("name"), b.get("craft", ""), b.get("capacity_total"),
                                  b.get("qualified_until"), actor=_actor(b))


def _mentor_capacity(svc, b, q, id):
    return svc.get_mentor_capacity(id)


def _suspend_mentor(svc, b, q, id):
    return svc.suspend_mentor(id, b.get("reason", ""), actor=_actor(b))


def _reinstate_mentor(svc, b, q, id):
    return svc.reinstate_mentor(id, actor=_actor(b))


def _invalidate_mentor(svc, b, q, id):
    return svc.invalidate_mentor(id, b.get("reason", ""), actor=_actor(b))


def _reconcile_capacity(svc, b, q, id):
    return svc.reconcile_capacity(id, actor=_actor(b))


def _create_student(svc, b, q):
    return 201, svc.create_student(b.get("name"), b.get("birth_date"), actor=_actor(b))


def _add_guardian(svc, b, q, id):
    return 201, svc.add_guardian_authorization(
        id, b.get("guardian_name"), b.get("scope", "all"),
        b.get("valid_from"), b.get("valid_to"), actor=_actor(b))


def _revoke_guardian(svc, b, q, id):
    return svc.revoke_guardian_authorization(id, actor=_actor(b))


def _portfolio(svc, b, q, id):
    return svc.get_student_portfolio(id, guardian=q.get("guardian"))


def _enroll(svc, b, q):
    return 201, svc.enroll(b.get("student_id"), b.get("mentor_id"), b.get("plan_version_id"),
                           actor=_actor(b))


def _get_enrollment(svc, b, q, id):
    return svc.get_enrollment(id)


def _enrollment_history(svc, b, q, id):
    return svc.get_enrollment_history(id)


def _submit_evidence(svc, b, q, id):
    return 201, svc.submit_evidence(
        id, b.get("goal_seq"), b.get("content_uri"), b.get("kind", "work"), b.get("note", ""),
        b.get("supersedes_id"), b.get("actor_role", "student"), b.get("guardian"),
        b.get("actor") or "student:self")


def _request_pause(svc, b, q, id):
    return 201, svc.request_pause(id, b.get("action", "pause"), b.get("reason", ""),
                                  b.get("actor") or "student:self",
                                  b.get("actor_role", "student"), b.get("guardian"))


def _decide_pause(svc, b, q, id):
    return svc.decide_pause(id, bool(b.get("approve")), b.get("decided_by") or "admin:api")


def _request_transfer(svc, b, q, id):
    return 201, svc.request_transfer(id, b.get("to_mentor_id"), b.get("reason", ""),
                                     actor=_actor(b))


def _decide_transfer(svc, b, q, id):
    return svc.decide_transfer(id, bool(b.get("approve")), b.get("decided_by") or "admin:api")


def _withdraw(svc, b, q, id):
    return svc.withdraw(id, b.get("reason", ""), actor=_actor(b))


def _continue(svc, b, q, id):
    return 201, svc.continue_enrollment(id, b.get("plan_version_id"), b.get("mentor_id"),
                                        actor=_actor(b))


def _assess(svc, b, q, id):
    return 201, svc.assess(id, b.get("verdict"), b.get("assessor"), b.get("goal_seq"),
                           b.get("score"), b.get("comment", ""), b.get("evidence_ids") or ())


def _assessment_basis(svc, b, q, id):
    return svc.get_assessment_basis(id)


def _compile(pattern: str) -> re.Pattern:
    regex = re.sub(r"\{(\w+)\}", r"(?P<\1>\\d+)", pattern)
    return re.compile(f"^{regex}$")


ROUTES = [
    (method, _compile(pattern), handler)
    for method, pattern, handler in [
        ("POST", "/plan-versions", _create_plan),
        ("GET", "/plan-versions/{id}", _get_plan),
        ("POST", "/plan-versions/{id}/goals", _add_goal),
        ("POST", "/plan-versions/{id}/publish", _publish_plan),
        ("POST", "/plan-versions/{id}/archive", _archive_plan),
        ("POST", "/mentors", _create_mentor),
        ("GET", "/mentors/{id}/capacity", _mentor_capacity),
        ("POST", "/mentors/{id}/suspend", _suspend_mentor),
        ("POST", "/mentors/{id}/reinstate", _reinstate_mentor),
        ("POST", "/mentors/{id}/invalidate", _invalidate_mentor),
        ("POST", "/mentors/{id}/reconcile-capacity", _reconcile_capacity),
        ("POST", "/students", _create_student),
        ("POST", "/students/{id}/guardian-authorizations", _add_guardian),
        ("POST", "/guardian-authorizations/{id}/revoke", _revoke_guardian),
        ("GET", "/students/{id}/portfolio", _portfolio),
        ("POST", "/enrollments", _enroll),
        ("GET", "/enrollments/{id}", _get_enrollment),
        ("GET", "/enrollments/{id}/history", _enrollment_history),
        ("POST", "/enrollments/{id}/evidence", _submit_evidence),
        ("POST", "/enrollments/{id}/pause-requests", _request_pause),
        ("POST", "/pause-requests/{id}/decide", _decide_pause),
        ("POST", "/enrollments/{id}/transfer-requests", _request_transfer),
        ("POST", "/transfer-requests/{id}/decide", _decide_transfer),
        ("POST", "/enrollments/{id}/withdraw", _withdraw),
        ("POST", "/enrollments/{id}/continue", _continue),
        ("POST", "/enrollments/{id}/assessments", _assess),
        ("GET", "/assessments/{id}/basis", _assessment_basis),
    ]
]


class _Handler(BaseHTTPRequestHandler):
    service = None  # 由 make_server 注入

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        body = {}
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._send(400, {"error": {"code": "bad_json", "message": "请求体不是合法 JSON"}})
        for route_method, regex, handler in ROUTES:
            if route_method != method:
                continue
            match = regex.match(parsed.path)
            if not match:
                continue
            params = {k: int(v) for k, v in match.groupdict().items()}
            try:
                result = handler(self.service, body, query, **params)
                status, payload = result if isinstance(result, tuple) else (200, result)
            except DomainError as exc:
                status, payload = exc.status, {"error": {"code": exc.code, "message": exc.message}}
            except Exception as exc:  # pragma: no cover - 兜底
                status, payload = 500, {"error": {"code": "internal", "message": str(exc)}}
            return self._send(status, payload)
        self._send(404, {"error": {"code": "not_found", "message": "接口不存在"}})

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 静默访问日志
        pass


def make_server(service, host="127.0.0.1", port=8080) -> ThreadingHTTPServer:
    handler = type("MentorshipHandler", (_Handler,), {"service": service})
    return ThreadingHTTPServer((host, port), handler)

"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """所有可预期的领域规则违反。"""

    http_status = 400


class ValidationError(DomainError):
    http_status = 400


class NotFoundError(DomainError):
    http_status = 404


class ConflictError(DomainError):
    http_status = 409


class StateConflictError(ConflictError):
    """实体当前状态不允许该操作。"""


class CapacityFullError(ConflictError):
    """导师在该计划版本下没有剩余名额。"""


class AuthorizationError(DomainError):
    """监护授权等访问约束未满足。"""

    http_status = 403

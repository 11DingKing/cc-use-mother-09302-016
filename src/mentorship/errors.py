"""领域错误类型，携带 HTTP 状态码供接口层映射。"""
from __future__ import annotations


class DomainError(Exception):
    """业务规则冲突的基类。"""

    status = 400
    code = "domain_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ValidationError(DomainError):
    """输入不合法。"""

    status = 400
    code = "validation_error"


class NotFound(DomainError):
    """目标资源不存在。"""

    status = 404
    code = "not_found"


class GuardianRequired(DomainError):
    """未成年人操作缺少有效监护授权。"""

    status = 403
    code = "guardian_required"


class CapacityExhausted(DomainError):
    """导师名额已满。"""

    status = 409
    code = "capacity_exhausted"


class InvalidState(DomainError):
    """当前状态不允许该操作。"""

    status = 409
    code = "invalid_state"

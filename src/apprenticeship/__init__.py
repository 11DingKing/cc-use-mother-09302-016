"""传统技艺师徒计划后端。

领域能力：
- 计划版本与阶段目标（录取时冻结当期目标）
- 导师资格、每版本容量与原子占座
- 学习证据（含补交）、评定依据快照与还原
- 暂停、转导师、退出、导师失效、跨学期续接的连续历史
- 未成年人监护授权约束
"""
from __future__ import annotations

from .db import connect, initialize_database
from .errors import (
    AuthorizationError,
    CapacityFullError,
    ConflictError,
    DomainError,
    NotFoundError,
    StateConflictError,
    ValidationError,
)
from .service import ApprenticeshipService

__all__ = [
    "ApprenticeshipService",
    "initialize_database",
    "connect",
    "DomainError",
    "ValidationError",
    "NotFoundError",
    "ConflictError",
    "StateConflictError",
    "CapacityFullError",
    "AuthorizationError",
]

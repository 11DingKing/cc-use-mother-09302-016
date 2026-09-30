"""通用工具：ID、时间、规范化哈希、年龄计算。"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timezone

CANONICAL_SEPARATORS = (",", ":")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


def canonical_hash(value: object) -> str:
    """对任意可 JSON 化对象计算稳定的 sha256，用于目标快照与评定清单。"""
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=CANONICAL_SEPARATORS,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def content_hash(content: str | bytes) -> str:
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def is_minor(birth_date_iso: str | None, at: datetime | None = None) -> bool:
    """按出生日期判断在给定时间（默认现在）是否未满 18 周岁。"""
    if not birth_date_iso:
        return False
    birth = date.fromisoformat(birth_date_iso)
    today = (at or utcnow()).date()
    age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    return age < 18

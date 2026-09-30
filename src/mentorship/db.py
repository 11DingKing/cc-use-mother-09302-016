"""SQLite 持久化层：连接、事务与表结构。

容量、录取、目标快照、证据、申请、评定与事件全部落库，
进程重启后状态完整恢复；事务统一使用 BEGIN IMMEDIATE，
配合单连接可重入锁，多线程下写操作串行化。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'archived')),
    created_at TEXT NOT NULL,
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS stage_goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_version_id INTEGER NOT NULL REFERENCES plan_versions(id),
    seq INTEGER NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    required_evidence INTEGER NOT NULL DEFAULT 1,
    UNIQUE (plan_version_id, seq)
);

CREATE TABLE IF NOT EXISTS mentors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    craft TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'invalid')),
    qualified_until TEXT,
    capacity_total INTEGER NOT NULL CHECK (capacity_total >= 0),
    capacity_used INTEGER NOT NULL DEFAULT 0 CHECK (capacity_used >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    birth_date TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guardian_authorizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id),
    guardian_name TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'all',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS enrollments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id),
    mentor_id INTEGER NOT NULL REFERENCES mentors(id),
    plan_version_id INTEGER NOT NULL REFERENCES plan_versions(id),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'withdrawn', 'completed', 'continued')),
    continued_from_id INTEGER REFERENCES enrollments(id),
    created_at TEXT NOT NULL,
    closed_at TEXT
);

-- 同一学生在同一计划版本下至多一条在读（active/paused）录取
CREATE UNIQUE INDEX IF NOT EXISTS idx_enrollments_active
    ON enrollments (student_id, plan_version_id)
    WHERE status IN ('active', 'paused');

CREATE TABLE IF NOT EXISTS goal_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enrollment_id INTEGER NOT NULL REFERENCES enrollments(id),
    seq INTEGER NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    required_evidence INTEGER NOT NULL DEFAULT 1,
    source_goal_id INTEGER NOT NULL REFERENCES stage_goals(id),
    UNIQUE (enrollment_id, seq)
);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enrollment_id INTEGER NOT NULL REFERENCES enrollments(id),
    goal_snapshot_id INTEGER NOT NULL REFERENCES goal_snapshots(id),
    kind TEXT NOT NULL DEFAULT 'work',
    content_uri TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    supersedes_id INTEGER REFERENCES evidence(id),
    submitted_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pause_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enrollment_id INTEGER NOT NULL REFERENCES enrollments(id),
    action TEXT NOT NULL CHECK (action IN ('pause', 'resume')),
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS transfer_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enrollment_id INTEGER NOT NULL REFERENCES enrollments(id),
    from_mentor_id INTEGER NOT NULL REFERENCES mentors(id),
    to_mentor_id INTEGER NOT NULL REFERENCES mentors(id),
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enrollment_id INTEGER NOT NULL REFERENCES enrollments(id),
    goal_snapshot_id INTEGER REFERENCES goal_snapshots(id),
    verdict TEXT NOT NULL,
    score INTEGER,
    assessor TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assessment_evidence (
    assessment_id INTEGER NOT NULL REFERENCES assessments(id),
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    PRIMARY KEY (assessment_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enrollment_id INTEGER REFERENCES enrollments(id),
    actor TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


class Database:
    """单连接 SQLite 封装：事务串行化，重启后状态可恢复。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        """开启一个立即写事务；异常时整体回滚，保证多步写入原子生效。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

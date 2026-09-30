"""SQLite 连接与初始化。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .schema import SCHEMA


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """打开一个适合本服务的连接。

    - WAL 允许读取不阻塞写入；
    - busy_timeout 让高并发录取时后来的事务等待而非立刻报锁；
    - 外键约束打开。
    """
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def initialize_database(conn: sqlite3.Connection) -> None:
    """创建全部表与触发器（幂等）。"""
    conn.executescript(SCHEMA)

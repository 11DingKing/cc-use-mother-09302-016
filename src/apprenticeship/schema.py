"""SQLite schema。

关键设计：
- ``seat_ledger`` 为只追加台账，任何占座/释放都是一条带 delta 的记录，
  余额永远等于 SUM(delta)，进程重启后无需任何缓存计数器即可复原。
- 触发器在数据库层强制余额不得为负、不得超过容量，作为并发安全的最后防线。
- ``enrollment_events`` 为只追加时间线，承载录取、暂停、转导师、退出、
  导师失效、跨学期续接的连续历史。
- 录取时把当期阶段目标复制进 ``enrollment_goals`` 并记录内容哈希，
  之后计划改版不影响在学学生。
"""
from __future__ import annotations

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 导师：资格状态机 active（在聘）/ suspended（暂停执教）/ invalid（失效）
CREATE TABLE IF NOT EXISTS mentors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    craft TEXT NOT NULL,
    qualification_no TEXT,
    status TEXT NOT NULL CHECK (status IN ('active','suspended','invalid')),
    created_at TEXT NOT NULL,
    qualified_at TEXT,
    invalidated_at TEXT
);

CREATE TABLE IF NOT EXISTS mentor_status_history (
    id TEXT PRIMARY KEY,
    mentor_id TEXT NOT NULL REFERENCES mentors(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT,
    changed_at TEXT NOT NULL,
    changed_by TEXT
);

-- 计划版本：每个学期一个版本，predecessor_id 串接跨学期版本
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    craft TEXT NOT NULL,
    term TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    title TEXT NOT NULL,
    predecessor_id TEXT REFERENCES plans(id),
    status TEXT NOT NULL CHECK (status IN ('draft','published','closed')),
    created_by TEXT,
    created_at TEXT NOT NULL,
    published_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_plans_lineage_version
    ON plans(craft, term, version_no);

-- 某版本的阶段目标（发布后内容不可变；改版即新建计划版本）
CREATE TABLE IF NOT EXISTS stages (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(id),
    seq INTEGER NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    due_date TEXT,
    UNIQUE(plan_id, seq),
    UNIQUE(plan_id, code)
);

-- 导师在某计划版本下的招生名额
CREATE TABLE IF NOT EXISTS mentor_capacities (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(id),
    mentor_id TEXT NOT NULL REFERENCES mentors(id),
    quota INTEGER NOT NULL CHECK (quota >= 0),
    UNIQUE(plan_id, mentor_id)
);

CREATE TABLE IF NOT EXISTS students (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    birth_date TEXT,
    contact TEXT,
    created_at TEXT NOT NULL
);

-- 监护授权：未成年人录取与访问的前提；可过期、可撤销
CREATE TABLE IF NOT EXISTS guardian_consents (
    id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL REFERENCES students(id),
    guardian_name TEXT NOT NULL,
    guardian_contact TEXT,
    access_code TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL DEFAULT 'full',
    document_hash TEXT,
    granted_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_consents_student ON guardian_consents(student_id);

-- 学籍：mentor_id 为当前导师，转导师时原地更新，旧值进入事件流
-- status:
--   enrolled        在学（占座）
--   paused          暂停（仍占座）
--   mentor_invalid  原导师失效，待转出（已释放原导师名额）
--   transferred_out 已转走/已在新版本续接，旧学籍归档（不占座）
--   withdrawn       退出（不占座）
--   completed       结业（不占座）
CREATE TABLE IF NOT EXISTS enrollments (
    id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL REFERENCES students(id),
    plan_id TEXT NOT NULL REFERENCES plans(id),
    mentor_id TEXT NOT NULL REFERENCES mentors(id),
    status TEXT NOT NULL CHECK (status IN (
        'enrolled','paused','mentor_invalid','transferred_out','withdrawn','completed'
    )),
    admitted_at TEXT NOT NULL,
    goals_hash TEXT NOT NULL,
    predecessor_enrollment_id TEXT REFERENCES enrollments(id),
    UNIQUE(student_id, plan_id)
);
CREATE INDEX IF NOT EXISTS ix_enrollments_mentor_plan ON enrollments(plan_id, mentor_id);

-- 录取时冻结的当期阶段目标
CREATE TABLE IF NOT EXISTS enrollment_goals (
    id TEXT PRIMARY KEY,
    enrollment_id TEXT NOT NULL REFERENCES enrollments(id),
    seq INTEGER NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    due_date TEXT,
    content_hash TEXT NOT NULL,
    UNIQUE(enrollment_id, seq)
);

-- 占座台账（只追加）
CREATE TABLE IF NOT EXISTS seat_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES plans(id),
    mentor_id TEXT NOT NULL REFERENCES mentors(id),
    enrollment_id TEXT NOT NULL REFERENCES enrollments(id),
    delta INTEGER NOT NULL CHECK (delta IN (-1, 1)),
    reason TEXT NOT NULL,
    ref_type TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_seat_ledger_pm ON seat_ledger(plan_id, mentor_id);
CREATE INDEX IF NOT EXISTS ix_seat_ledger_enr ON seat_ledger(enrollment_id);

-- 学籍连续历史（只追加）
CREATE TABLE IF NOT EXISTS enrollment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enrollment_id TEXT NOT NULL REFERENCES enrollments(id),
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    occurred_at TEXT NOT NULL,
    UNIQUE(enrollment_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_events_enr ON enrollment_events(enrollment_id);

-- 暂停/复学申请
CREATE TABLE IF NOT EXISTS pause_requests (
    id TEXT PRIMARY KEY,
    enrollment_id TEXT NOT NULL REFERENCES enrollments(id),
    kind TEXT NOT NULL CHECK (kind IN ('pause','resume')),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','cancelled')),
    requested_by TEXT,
    requested_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    decision_note TEXT
);

-- 转导师申请
CREATE TABLE IF NOT EXISTS transfer_requests (
    id TEXT PRIMARY KEY,
    enrollment_id TEXT NOT NULL REFERENCES enrollments(id),
    from_mentor_id TEXT NOT NULL REFERENCES mentors(id),
    to_mentor_id TEXT NOT NULL REFERENCES mentors(id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','cancelled')),
    requested_by TEXT,
    requested_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    decision_note TEXT
);

-- 学习证据：is_late=1 表示补交
CREATE TABLE IF NOT EXISTS evidences (
    id TEXT PRIMARY KEY,
    enrollment_id TEXT NOT NULL REFERENCES enrollments(id),
    stage_code TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    is_late INTEGER NOT NULL DEFAULT 0,
    make_up_note TEXT,
    status TEXT NOT NULL CHECK (status IN ('submitted','accepted','rejected')),
    submitted_by TEXT,
    submitted_at TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_note TEXT
);
CREATE INDEX IF NOT EXISTS ix_evidences_enr_stage ON evidences(enrollment_id, stage_code);

-- 评定：basis 为冻结目标 + 证据清单的快照，basis_hash 可随时复验
CREATE TABLE IF NOT EXISTS assessments (
    id TEXT PRIMARY KEY,
    enrollment_id TEXT NOT NULL REFERENCES enrollments(id),
    stage_code TEXT,
    result TEXT NOT NULL CHECK (result IN ('pass','fail','conditional_pass')),
    grade TEXT,
    assessor TEXT NOT NULL,
    note TEXT,
    basis TEXT NOT NULL,
    basis_hash TEXT NOT NULL,
    assessed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_assessments_enr ON assessments(enrollment_id);

-- 容量护栏：余额必须落在 [0, quota]
CREATE TRIGGER IF NOT EXISTS trg_seat_upper
AFTER INSERT ON seat_ledger
BEGIN
    SELECT CASE
        WHEN (
            SELECT COALESCE(SUM(delta), 0) FROM seat_ledger
            WHERE plan_id = NEW.plan_id AND mentor_id = NEW.mentor_id
        ) > (
            SELECT quota FROM mentor_capacities
            WHERE plan_id = NEW.plan_id AND mentor_id = NEW.mentor_id
        )
        THEN RAISE(ABORT, 'SEAT_OVER_CAPACITY')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_seat_lower
AFTER INSERT ON seat_ledger
BEGIN
    SELECT CASE
        WHEN (
            SELECT COALESCE(SUM(delta), 0) FROM seat_ledger
            WHERE plan_id = NEW.plan_id AND mentor_id = NEW.mentor_id
        ) < 0
        THEN RAISE(ABORT, 'SEAT_NEGATIVE_BALANCE')
    END;
END;
"""

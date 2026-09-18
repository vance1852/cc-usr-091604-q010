"""SQLite 持久化：连接管理、事务与表结构。

并发模型：
- WAL 模式，读写不互斥；
- 写事务一律 BEGIN IMMEDIATE，把并发写串行化；
- 关键不变量（一座位一活票、一票一待转邀请、幂等键唯一）由数据库约束兜底，
  即使应用层出现竞态也不会产生两张可用票。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS games (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    venue TEXT NOT NULL,
    start_time TEXT NOT NULL,              -- UTC ISO-8601
    transfer_cutoff_hours REAL NOT NULL,   -- 转让/退票截止：开赛前 N 小时
    gates_open_hours REAL NOT NULL,        -- 入场开放：开赛前 N 小时
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seats (
    id TEXT PRIMARY KEY,
    game_id TEXT NOT NULL REFERENCES games (id),
    section TEXT NOT NULL,                 -- 看台区
    row TEXT NOT NULL,
    number TEXT NOT NULL,
    price_cents INTEGER NOT NULL DEFAULT 0,
    UNIQUE (game_id, section, row, number)
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    full_name TEXT NOT NULL,
    id_number TEXT NOT NULL UNIQUE,        -- 实名证件号（生产环境应加密存储）
    phone TEXT NOT NULL,
    birth_date TEXT NOT NULL,              -- YYYY-MM-DD
    role TEXT NOT NULL DEFAULT 'fan',      -- fan / admin / cs
    risk_status TEXT NOT NULL DEFAULT 'CLEAR',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    game_id TEXT NOT NULL REFERENCES games (id),
    seat_id TEXT NOT NULL REFERENCES seats (id),
    holder_id TEXT NOT NULL REFERENCES users (id),
    status TEXT NOT NULL,
    prev_status TEXT,                      -- 冻结前的状态，解冻时恢复
    chain_seq INTEGER NOT NULL DEFAULT 0,  -- 当前持有链长度 - 1
    version INTEGER NOT NULL DEFAULT 0,    -- 乐观锁
    purchase_idempotency_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 同一座位同一时刻最多一张“活”票；退回/作废后座位可再次出售
CREATE UNIQUE INDEX IF NOT EXISTS uq_live_ticket_per_seat
    ON tickets (game_id, seat_id)
    WHERE status IN ('ACTIVE', 'FROZEN', 'USED');

-- 不可变持有链：只追加，不修改
CREATE TABLE IF NOT EXISTS ownership_chain (
    ticket_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,              -- PURCHASE / TRANSFER / RETURN
    from_holder_id TEXT,
    holder_id TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL,
    PRIMARY KEY (ticket_id, seq)
);

CREATE TABLE IF NOT EXISTS transfer_invitations (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets (id),
    from_user_id TEXT NOT NULL,
    to_user_id TEXT NOT NULL,
    status TEXT NOT NULL,                  -- PENDING / ACCEPTED / CANCELLED / EXPIRED
    idempotency_key TEXT NOT NULL UNIQUE,
    accept_idempotency_key TEXT UNIQUE,
    guardian_id TEXT,                      -- 未成年人接收时的监护人
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,              -- 等于转让截止时间
    resolved_at TEXT
);

-- 同一票券同一时刻最多一个待处理转让邀请
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_invitation_per_ticket
    ON transfer_invitations (ticket_id)
    WHERE status = 'PENDING';

CREATE TABLE IF NOT EXISTS scan_events (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,               -- 可能是伪造 id，故意不设外键，保证异常也能留痕
    chain_seq INTEGER,                     -- 凭证所对应的链序号
    device_id TEXT NOT NULL,
    gate_id TEXT,
    result TEXT NOT NULL,
    detail TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    scanned_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scan_events_ticket ON scan_events (ticket_id);

CREATE TABLE IF NOT EXISTS review_cases (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    scan_event_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING / ADMITTED / REJECTED
    decided_by TEXT,
    decision_note TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_cases_ticket ON review_cases (ticket_id);

CREATE TABLE IF NOT EXISTS freeze_actions (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,                  -- FREEZE / UNFREEZE
    scope TEXT NOT NULL,                   -- TICKET / BATCH
    ticket_id TEXT,
    filter_json TEXT,                      -- 批量冻结的过滤条件
    affected_ticket_ids TEXT NOT NULL,     -- JSON 数组
    reason TEXT NOT NULL,                  -- 原因
    basis TEXT NOT NULL,                   -- 依据（制度/协查通知等）
    admin_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT,
    actor_role TEXT,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    game_id TEXT,
    seat_id TEXT,
    ticket_id TEXT,
    before_json TEXT,
    after_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_log_seat ON audit_log (game_id, seat_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_ticket ON audit_log (ticket_id);
"""


class Database:
    """管理 SQLite 连接与事务。"""

    def __init__(self, path: str):
        self.path = str(path)
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """写事务：BEGIN IMMEDIATE 串行化并发写，异常时整体回滚。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def backup_to(self, dest_path: str) -> None:
        """使用 SQLite 在线备份 API 生成一致性快照。"""
        src = self.connect()
        try:
            dst = sqlite3.connect(str(dest_path))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

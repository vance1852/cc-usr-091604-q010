"""数据库连接与模式定义。

所有时间统一以 UTC（aware datetime 的 ISO 字符串）落库；
事件表 events 为只追加表，通过触发器禁止 UPDATE/DELETE，
并以全局 seq + sha256 哈希串联，形成不可变持有链。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id            TEXT PRIMARY KEY,
    full_name     TEXT NOT NULL,
    id_doc_hash   TEXT NOT NULL,          -- 证件号 sha256，不存明文
    id_doc_masked TEXT NOT NULL,          -- 脱敏证件号
    birth_date    TEXT NOT NULL,          -- YYYY-MM-DD
    phone_masked  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_entries (
    id             TEXT PRIMARY KEY,
    customer_id    TEXT NOT NULL UNIQUE,
    reason         TEXT NOT NULL,
    created_by     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    released_at    TEXT,
    release_reason TEXT
);

CREATE TABLE IF NOT EXISTS matches (
    id                    TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    venue                 TEXT NOT NULL,
    event_time_utc        TEXT NOT NULL,
    transfer_deadline_utc TEXT NOT NULL,
    created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seats (
    id        TEXT PRIMARY KEY,
    match_id  TEXT NOT NULL REFERENCES matches(id),
    section   TEXT NOT NULL,              -- 看台
    seat_row  TEXT NOT NULL,
    seat_no   TEXT NOT NULL,
    UNIQUE (match_id, section, seat_row, seat_no)
);

CREATE TABLE IF NOT EXISTS tickets (
    id               TEXT PRIMARY KEY,    -- 即电子票码
    match_id         TEXT NOT NULL REFERENCES matches(id),
    seat_id          TEXT NOT NULL REFERENCES seats(id),
    current_holder_id TEXT NOT NULL REFERENCES customers(id),
    status           TEXT NOT NULL,       -- issued/transfer_pending/returned/consumed
    is_frozen        INTEGER NOT NULL DEFAULT 0,
    idempotency_key  TEXT NOT NULL UNIQUE,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
-- 同一座位至多一张生效票（退回后才可重新售卖）
CREATE UNIQUE INDEX IF NOT EXISTS one_live_ticket_per_seat
    ON tickets(seat_id) WHERE status IN ('issued', 'transfer_pending');

CREATE TABLE IF NOT EXISTS transfer_invitations (
    id              TEXT PRIMARY KEY,
    ticket_id       TEXT NOT NULL REFERENCES tickets(id),
    from_holder_id  TEXT NOT NULL REFERENCES customers(id),
    to_customer_id  TEXT NOT NULL REFERENCES customers(id),
    status          TEXT NOT NULL,        -- pending/accepted/cancelled/expired
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,       -- 快照本场转让截止时间
    responded_at    TEXT
);
-- 一张票同时只能有一笔待确认邀请（数据库层兜底并发）
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_invite_per_ticket
    ON transfer_invitations(ticket_id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS scans (
    id              TEXT PRIMARY KEY,
    ticket_id       TEXT REFERENCES tickets(id),   -- 未知票码时为空
    ticket_code     TEXT NOT NULL,
    device_id       TEXT NOT NULL,
    presenter_id    TEXT,
    result          TEXT NOT NULL,        -- admitted/rejected/review
    reason          TEXT NOT NULL,
    consumed        INTEGER NOT NULL DEFAULT 0,
    scanned_at      TEXT NOT NULL,
    review_id       TEXT,
    idempotency_key TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS reviews (
    id         TEXT PRIMARY KEY,
    scan_id    TEXT NOT NULL UNIQUE REFERENCES scans(id),
    reviewer   TEXT NOT NULL,
    decision   TEXT NOT NULL,             -- allow_entry/deny_entry
    note       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS freezes (
    id           TEXT PRIMARY KEY,
    batch_id     TEXT NOT NULL,
    ticket_id    TEXT NOT NULL REFERENCES tickets(id),
    admin_id     TEXT NOT NULL,
    reason       TEXT NOT NULL,           -- 冻结依据，必填
    created_at   TEXT NOT NULL,
    released_at  TEXT,
    release_note TEXT
);
CREATE INDEX IF NOT EXISTS freezes_batch ON freezes(batch_id);
CREATE INDEX IF NOT EXISTS freezes_open
    ON freezes(ticket_id) WHERE released_at IS NULL;

-- 通用幂等记录：网络重试按同一 key 重放首次结果
CREATE TABLE IF NOT EXISTS idempotency (
    idempotency_key TEXT PRIMARY KEY,
    op              TEXT NOT NULL,
    ref_id          TEXT,
    outcome         TEXT NOT NULL,        -- ok / 业务错误码
    response        TEXT NOT NULL,        -- JSON 快照
    created_at      TEXT NOT NULL
);

-- 只追加的全局事件链（持有链 + 审计合一）
CREATE TABLE IF NOT EXISTS events (
    id                  TEXT PRIMARY KEY,
    seq                 INTEGER NOT NULL UNIQUE,
    ticket_id           TEXT,
    seat_id             TEXT,
    match_id            TEXT,
    subject_customer_id TEXT,
    actor_id            TEXT NOT NULL,
    action              TEXT NOT NULL,
    payload             TEXT NOT NULL,    -- JSON，只存 ID 不存明文 PII
    created_at          TEXT NOT NULL,
    prev_hash           TEXT NOT NULL,
    hash                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ticket  ON events(ticket_id);
CREATE INDEX IF NOT EXISTS events_seat    ON events(seat_id);
CREATE INDEX IF NOT EXISTS events_match   ON events(match_id);
CREATE INDEX IF NOT EXISTS events_subject ON events(subject_customer_id);

-- 事件表不可篡改
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events 表只追加，禁止 UPDATE');
END;
CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events 表只追加，禁止 DELETE');
END;
"""


def connect(path: str | Path, *, timeout: float = 30.0) -> sqlite3.Connection:
    """打开连接并启用 WAL / 外键；调用方自行 BEGIN IMMEDIATE 提交。"""
    conn = sqlite3.connect(str(path), timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        # executescript 自身先 COMMIT 再执行；DDL 带 IF NOT EXISTS 可重入
        conn.executescript(SCHEMA)
    finally:
        conn.close()

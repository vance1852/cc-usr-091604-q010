"""时间、身份与哈希工具。

全系统内部时间一律使用 timezone-aware UTC datetime；
对外接受带偏移的 ISO 字符串，截止判定严格走 UTC 瞬时。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("必须使用 timezone-aware datetime")
    return dt.astimezone(UTC).isoformat()


def parse_iso(value: str) -> datetime:
    """解析 ISO-8601；裸时间视为 UTC。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def age_on(birth: date, on_day: date) -> int:
    return on_day.year - birth.year - (
        (on_day.month, on_day.day) < (birth.month, birth.day)
    )


def parse_birth(value: str) -> date:
    return date.fromisoformat(value)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_doc(id_doc: str) -> str:
    return sha256_text("id-doc:" + id_doc.strip().upper())


def mask_doc(id_doc: str) -> str:
    """证件号脱敏：保留前 2 后 2，其余以 * 代替。"""
    s = id_doc.strip()
    if len(s) <= 4:
        return s[0] + "*" * (len(s) - 1)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def mask_phone(phone: str) -> str:
    s = phone.strip()
    if len(s) <= 4:
        return "*" * len(s)
    return s[:3] + "*" * (len(s) - 5) + s[-2:]


def chain_hash(
    prev_hash: str,
    seq: int,
    ticket_id: str | None,
    seat_id: str | None,
    match_id: str | None,
    subject_customer_id: str | None,
    actor_id: str,
    action: str,
    payload: dict[str, Any],
    created_at: str,
) -> str:
    body = json.dumps(
        {
            "seq": seq,
            "ticket_id": ticket_id,
            "seat_id": seat_id,
            "match_id": match_id,
            "subject_customer_id": subject_customer_id,
            "actor_id": actor_id,
            "action": action,
            "payload": payload,
            "created_at": created_at,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()

"""持有链哈希：让每一次持有人变更都可校验、不可篡改。"""

from __future__ import annotations

import hashlib

GENESIS_HASH = "0" * 64

_SEPARATOR = "\x1f"


def compute_link_hash(
    *,
    ticket_id: str,
    seq: int,
    event_type: str,
    from_holder_id: str,
    holder_id: str,
    note: str,
    created_at: str,
    prev_hash: str,
) -> str:
    """对链上一条记录计算 SHA-256。

    任何字段（含前序哈希）被改动都会导致校验失败，从而发现篡改。
    """
    payload = _SEPARATOR.join(
        [ticket_id, str(seq), event_type, from_holder_id, holder_id, note, created_at, prev_hash]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

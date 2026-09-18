"""测试辅助：临时数据库、可控时钟、快速搭建场次/客户。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.db import connect, init_db
from app.service import TicketService


class MovableClock:
    """测试时钟：手动拨放到任意 UTC 时刻。"""

    def __init__(self, at: datetime | None = None):
        self.at = at or datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **kwargs) -> datetime:
        self.at += timedelta(**kwargs)
        return self.at

    def set(self, at: datetime) -> datetime:
        self.at = at
        return self.at


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "tickets.db")
        init_db(self.db_path)
        self.conn = connect(self.db_path)
        self.clock = MovableClock()
        self.svc = TicketService(self.conn, clock=self.clock)
        self._seq = 0

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def new_conn_service(self) -> TicketService:
        """另开连接（并发线程用），共用同一时钟。"""
        return TicketService(connect(self.db_path), clock=self.clock)

    def uid(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    # ---------------------------------------------------------- 场景脚手架

    def make_customer(self, cid: str, *, birth: str = "1990-05-10",
                      doc: str | None = None, phone: str | None = None) -> str:
        self.svc.register_customer(
            cid, f"球迷{cid}", doc or f"ID-{cid}-0000", birth,
            phone or "13800000000",
        )
        return cid

    def make_match(self, match_id: str = "M1", *, event_time: str | None = None,
                   transfer_deadline: str | None = None) -> dict:
        # 默认开球时间：当前时钟 5 小时后，保证截止时间尚未到
        if event_time is None:
            event_time = (self.clock.at + timedelta(hours=5)).isoformat()
        return self.svc.create_match(
            match_id, "主场对客队", "城市球馆", event_time, transfer_deadline
        )

    def make_seat(self, match_id: str = "M1", section="A区", row="3", no="8") -> None:
        self.svc.add_seats(match_id, [(section, row, no)])

    def buy(self, customer: str, *, match_id: str = "M1", section="A区",
            row="3", no="8", key: str | None = None) -> dict:
        self._seq += 1
        return self.svc.purchase_ticket(
            match_id, section, row, no, customer,
            key or f"k-buy-{self._seq}",
        )

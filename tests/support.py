"""测试公共工具：可注入时钟的服务实例与标准数据。"""

import os
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.service import TicketLifecycleService  # noqa: E402

# 北京时间 2026-10-01 19:30 开赛 == UTC 11:30
GAME_START_UTC = datetime(2026, 10, 1, 11, 30, tzinfo=timezone.utc)
CUTOFF_UTC = GAME_START_UTC - timedelta(hours=2)  # 开赛前两小时停止转让/退票


class FakeClock:
    """可手动推进的时钟，用于截止时间与并发测试。"""

    def __init__(self, now):
        self._now = now

    def __call__(self):
        return self._now

    def set(self, now):
        self._now = now

    def advance(self, **kwargs):
        self._now = self._now + timedelta(**kwargs)


class ServiceTestCase(unittest.TestCase):
    """每个用例一套独立数据库与标准数据。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "tickets.db")
        self.clock = FakeClock(datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc))
        self.svc = self.new_service()
        self.admin = self.svc.register_user(
            full_name="系统管理员", id_number="000000197001010011",
            phone="13900000001", birth_date="1970-01-01", role="admin",
        )
        self.cs = self.svc.register_user(
            full_name="客服小李", id_number="000000198501010022",
            phone="13900000002", birth_date="1985-01-01", role="cs",
        )
        self.game = self.svc.create_game(
            name="常规赛第12轮 飞豹 vs 猛虎", venue="奥体中心体育馆", start_time=GAME_START_UTC,
        )
        self.svc.add_seats(game_id=self.game["id"], section="A", rows=["1"], numbers=[1, 2, 3, 4, 5, 6], price_cents=18000)
        self.svc.add_seats(game_id=self.game["id"], section="B", rows=["1"], numbers=[1, 2], price_cents=12000)
        self.alice = self.svc.register_user(
            full_name="张伟民", id_number="110101199001011234", phone="13812345678", birth_date="1990-01-01",
        )
        self.bob = self.svc.register_user(
            full_name="王强", id_number="110101199202022345", phone="13912345678", birth_date="1992-02-02",
        )
        self.carol = self.svc.register_user(
            full_name="刘敏", id_number="110101198803033456", phone="13712345678", birth_date="1988-03-03",
        )
        self.minor = self.svc.register_user(  # 比赛日 15 岁
            full_name="张小明", id_number="110101201105054567", phone="13612345678", birth_date="2011-05-05",
        )
        self.kid = self.svc.register_user(  # 比赛日 10 岁
            full_name="张小小", id_number="110101201606065678", phone="13512345678", birth_date="2016-06-06",
        )

    def new_service(self):
        return TicketLifecycleService(self.db_path, clock=self.clock, token_secret=b"unit-test-secret")

    def buy(self, user, section="A", row="1", number=1, key=None):
        return self.svc.purchase_ticket(
            user_id=user["id"], game_id=self.game["id"], section=section, row=row, number=number,
            idempotency_key=key or f"buy-{uuid.uuid4().hex}",
        )

    def go_to_entry_window(self):
        """进入入场时间窗（开赛前 1 小时）。"""
        self.clock.set(GAME_START_UTC - timedelta(hours=1))

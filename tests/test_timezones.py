"""跨时区截止时间测试：同一物理时刻在不同时区表达，判定必须一致。"""

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from support import CUTOFF_UTC, GAME_START_UTC, ServiceTestCase

from app import CutoffPassedError, ValidationError

SHANGHAI = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")


class TimezoneCutoffTest(ServiceTestCase):
    def test_game_start_stored_as_utc(self):
        game = self.svc.create_game(
            name="时区测试场", venue="测试馆",
            start_time=datetime(2026, 10, 1, 19, 30, tzinfo=SHANGHAI),
        )
        self.assertEqual(game["start_time"], "2026-10-01T11:30:00+00:00")

    def test_cutoff_consistent_across_timezones(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        # 截止前 1 秒：用纽约时间表达（== UTC 09:29:59）
        self.clock.set(datetime(2026, 10, 1, 5, 29, 59, tzinfo=NEW_YORK))
        inv = self.svc.create_transfer(
            ticket_id=tid, from_user_id=self.alice["id"], to_user_id=self.bob["id"], idempotency_key="tz-1",
        )
        # 截止后 1 秒：用北京时间表达（== UTC 09:30:01），创建与接收都被拒绝
        self.clock.set(datetime(2026, 10, 1, 17, 30, 1, tzinfo=SHANGHAI))
        with self.assertRaises(CutoffPassedError):
            self.svc.accept_transfer(invitation_id=inv["id"], user_id=self.bob["id"], idempotency_key="tz-acc")
        self.assertEqual(self.svc.get_invitation(inv["id"])["status"], "EXPIRED")
        with self.assertRaises(CutoffPassedError):
            self.svc.create_transfer(ticket_id=tid, from_user_id=self.alice["id"], to_user_id=self.carol["id"], idempotency_key="tz-2")

    def test_cutoff_boundary_exact_moment_is_closed(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        self.clock.set(CUTOFF_UTC - timedelta(seconds=1))
        inv = self.svc.create_transfer(
            ticket_id=tid, from_user_id=self.alice["id"], to_user_id=self.bob["id"], idempotency_key="tz-3",
        )
        self.clock.set(CUTOFF_UTC)  # 恰好到达截止时刻即关闭
        with self.assertRaises(CutoffPassedError):
            self.svc.accept_transfer(invitation_id=inv["id"], user_id=self.bob["id"], idempotency_key="tz-acc-3")

    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValidationError):
            self.svc.create_game(name="x", venue="y", start_time=datetime(2026, 10, 1, 19, 30))

    def test_same_instant_same_decision(self):
        """截止前 1 分钟这一时刻，三种时区表达下判定完全一致。"""
        instant = CUTOFF_UTC - timedelta(minutes=1)
        for i, tz in enumerate((SHANGHAI, NEW_YORK, None)):
            res = self.buy(self.alice, number=2 + i)
            self.clock.set(instant if tz is None else instant.astimezone(tz))
            inv = self.svc.create_transfer(
                ticket_id=res["ticket"]["id"], from_user_id=self.alice["id"],
                to_user_id=self.bob["id"], idempotency_key=f"tz-same-{tz}",
            )
            acc = self.svc.accept_transfer(
                invitation_id=inv["id"], user_id=self.bob["id"], idempotency_key=f"tz-same-acc-{tz}",
            )
            self.assertEqual(acc["new_holder_id"], self.bob["id"])


if __name__ == "__main__":
    unittest.main()

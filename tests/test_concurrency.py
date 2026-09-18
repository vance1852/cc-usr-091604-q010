"""并发测试：并发转让、并发接收、并发扫码、并发购票。"""

import unittest
from concurrent.futures import ThreadPoolExecutor

from support import ServiceTestCase

from app import ConflictError, SeatUnavailableError


class ConcurrentTransferTest(ServiceTestCase):
    def test_concurrent_accept_only_one_winner(self):
        """8 个并发接收请求（重复点击/网络重试场景）：恰好一个成功，链上只多一环。"""
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        inv = self.svc.create_transfer(
            ticket_id=tid, from_user_id=self.alice["id"], to_user_id=self.bob["id"], idempotency_key="race-inv",
        )

        def attempt(i):
            try:
                return ("ok", self.svc.accept_transfer(invitation_id=inv["id"], user_id=self.bob["id"], idempotency_key=f"race-acc-{i}"))
            except ConflictError as exc:
                return ("conflict", exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(attempt, range(8)))

        winners = [o for o in outcomes if o[0] == "ok"]
        self.assertEqual(len(winners), 1, outcomes)
        self.assertEqual(len([o for o in outcomes if o[0] == "conflict"]), 7)
        ticket = self.svc.get_ticket(tid)
        self.assertEqual(ticket["holder_id"], self.bob["id"])
        self.assertEqual(ticket["chain_seq"], 1)
        self.assertEqual(len(self.svc.ticket_history(tid)["chain"]), 2)
        self.assertTrue(self.svc.verify_chain(tid))

    def test_concurrent_invitations_to_different_accounts_single_pending(self):
        """同一张票被同时转给不同账号：只有一个邀请能进入待处理状态。"""
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        recipients = [self.bob, self.carol]

        def attempt(i):
            try:
                return ("ok", self.svc.create_transfer(
                    ticket_id=tid, from_user_id=self.alice["id"],
                    to_user_id=recipients[i % 2]["id"], idempotency_key=f"cr-{i}",
                ))
            except ConflictError as exc:
                return ("conflict", exc)

        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(attempt, range(6)))

        self.assertEqual(len([o for o in outcomes if o[0] == "ok"]), 1, outcomes)
        pending = self.svc.list_invitations(ticket_id=tid, status="PENDING")
        self.assertEqual(len(pending), 1)
        # 票仍归原持有人，且仍可正常完成这一次转让
        winner = pending[0]
        acc = self.svc.accept_transfer(invitation_id=winner["id"], user_id=winner["to_user_id"], idempotency_key="cr-acc")
        self.assertEqual(acc["new_holder_id"], winner["to_user_id"])
        self.assertTrue(self.svc.verify_integrity()["ok"])


class ConcurrentScanTest(ServiceTestCase):
    def test_concurrent_scans_consume_exactly_once(self):
        """10 个闸机并发扫同一张票：恰好放行一次，其余进人工复核。"""
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        self.go_to_entry_window()
        token = res["token"]

        def attempt(i):
            return self.svc.scan_ticket(token=token, device_id=f"gate-{i % 3}", idempotency_key=f"scan-{i}")

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(attempt, range(10)))

        allowed = [r for r in results if r["result"] == "ALLOWED"]
        denied = [r for r in results if r["result"] == "DENIED_ALREADY_USED"]
        self.assertEqual(len(allowed), 1, results)
        self.assertEqual(len(denied), 9)
        self.assertEqual(self.svc.get_ticket(tid)["status"], "USED")
        self.assertEqual(len(self.svc.list_review_cases()), 9)
        self.assertEqual(len(self.svc.ticket_history(tid)["scans"]), 10)


class ConcurrentPurchaseTest(ServiceTestCase):
    def test_concurrent_purchase_same_seat_sold_once(self):
        users = [self.alice, self.bob, self.carol]

        def attempt(i):
            try:
                return ("ok", self.buy(users[i % 3], key=f"seat-race-{i}"))
            except SeatUnavailableError as exc:
                return ("unavailable", exc)

        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(attempt, range(6)))

        self.assertEqual(len([o for o in outcomes if o[0] == "ok"]), 1, outcomes)
        self.assertEqual(len(self.svc.list_tickets(game_id=self.game["id"])), 1)
        self.assertTrue(self.svc.verify_integrity()["ok"])


if __name__ == "__main__":
    unittest.main()

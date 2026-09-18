"""冻结与风控测试：单张/批量冻结、解冻恢复、风控名单联动。"""

import unittest

from support import ServiceTestCase

from app import ConflictError, PermissionDeniedError, RiskBlockedError, ValidationError


class FreezeTest(ServiceTestCase):
    def test_freeze_single_blocks_transfer_and_scan_until_unfrozen(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        out = self.svc.freeze_ticket(
            admin_id=self.admin["id"], ticket_id=tid, reason="涉嫌倒卖", basis="《票务管理办法》第12条",
        )
        self.assertEqual(out["count"], 1)
        self.assertEqual(self.svc.get_ticket(tid)["status"], "FROZEN")
        with self.assertRaises(ConflictError):
            self.svc.create_transfer(ticket_id=tid, from_user_id=self.alice["id"], to_user_id=self.bob["id"], idempotency_key="fz-t")
        self.go_to_entry_window()
        r = self.svc.scan_ticket(token=res["token"], device_id="gate-A1", idempotency_key="fz-s")
        self.assertEqual(r["result"], "DENIED_FROZEN")
        self.assertIsNotNone(r["review_case_id"])
        self.svc.unfreeze_ticket(admin_id=self.admin["id"], ticket_id=tid, reason="排除嫌疑")
        self.assertEqual(self.svc.get_ticket(tid)["status"], "ACTIVE")
        r2 = self.svc.scan_ticket(token=res["token"], device_id="gate-A1", idempotency_key="fz-s2")
        self.assertEqual(r2["result"], "ALLOWED")

    def test_freeze_requires_reason_basis_and_admin_role(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        with self.assertRaises(ValidationError):
            self.svc.freeze_ticket(admin_id=self.admin["id"], ticket_id=tid, reason="", basis="x")
        with self.assertRaises(ValidationError):
            self.svc.freeze_ticket(admin_id=self.admin["id"], ticket_id=tid, reason="r", basis="")
        with self.assertRaises(PermissionDeniedError):
            self.svc.freeze_ticket(admin_id=self.alice["id"], ticket_id=tid, reason="r", basis="b")

    def test_batch_freeze_by_section(self):
        a1 = self.buy(self.alice, number=1)
        a2 = self.buy(self.bob, number=2)
        a3 = self.buy(self.carol, number=3)
        b1 = self.buy(self.alice, section="B", number=1)
        out = self.svc.freeze_batch(
            admin_id=self.admin["id"], game_id=self.game["id"], section="A",
            reason="看台结构安全隐患", basis="场馆紧急通知 2026-09-30-07",
        )
        self.assertEqual(out["count"], 3)
        self.assertEqual(set(out["frozen"]), {a1["ticket"]["id"], a2["ticket"]["id"], a3["ticket"]["id"]})
        for t in (a1, a2, a3):
            self.assertEqual(self.svc.get_ticket(t["ticket"]["id"])["status"], "FROZEN")
        self.assertEqual(self.svc.get_ticket(b1["ticket"]["id"])["status"], "ACTIVE")
        # 冻结依据写入座位时间线，运营方可还原
        timeline = self.svc.seat_timeline(game_id=self.game["id"], section="A", row="1", number=2)
        freeze_events = [t for t in timeline if t["action"] == "FREEZE"]
        self.assertEqual(len(freeze_events), 1)
        self.assertEqual(freeze_events[0]["after"]["basis"], "场馆紧急通知 2026-09-30-07")
        # 冻结票扫码进人工复核
        self.go_to_entry_window()
        r = self.svc.scan_ticket(token=a2["token"], device_id="gate-A1", idempotency_key="bf-scan")
        self.assertEqual(r["result"], "DENIED_FROZEN")
        self.assertIsNotNone(r["review_case_id"])

    def test_batch_freeze_by_ticket_ids_and_empty_filter_rejected(self):
        t1 = self.buy(self.alice, number=4)
        t2 = self.buy(self.bob, number=5)
        out = self.svc.freeze_batch(
            admin_id=self.admin["id"], ticket_ids=[t1["ticket"]["id"], t2["ticket"]["id"]],
            reason="司法协查", basis="协查函〔2026〕118号",
        )
        self.assertEqual(out["count"], 2)
        with self.assertRaises(ValidationError):
            self.svc.freeze_batch(admin_id=self.admin["id"], reason="r", basis="b")

    def test_double_freeze_rejected(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        self.svc.freeze_ticket(admin_id=self.admin["id"], ticket_id=tid, reason="r", basis="b")
        with self.assertRaises(ConflictError):
            self.svc.freeze_ticket(admin_id=self.admin["id"], ticket_id=tid, reason="r2", basis="b2")


class RiskListTest(ServiceTestCase):
    def test_block_freezes_holdings_and_cancels_invitations(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        inv = self.svc.create_transfer(
            ticket_id=tid, from_user_id=self.alice["id"], to_user_id=self.bob["id"], idempotency_key="risk-inv",
        )
        result = self.svc.set_risk_status(
            admin_id=self.admin["id"], user_id=self.alice["id"], status="BLOCKED", reason="公安协查通报",
        )
        self.assertEqual(result["frozen_tickets"], [tid])
        self.assertEqual(result["cancelled_invitations"], 1)
        self.assertEqual(self.svc.get_ticket(tid)["status"], "FROZEN")
        self.assertEqual(self.svc.get_invitation(inv["id"])["status"], "CANCELLED")
        with self.assertRaises(RiskBlockedError):
            self.buy(self.alice, number=5)

    def test_blocked_recipient_invitation_is_cancelled(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        inv = self.svc.create_transfer(
            ticket_id=tid, from_user_id=self.alice["id"], to_user_id=self.bob["id"], idempotency_key="blk-inv",
        )
        self.svc.set_risk_status(admin_id=self.admin["id"], user_id=self.bob["id"], status="BLOCKED", reason="违规转售")
        with self.assertRaises(ConflictError):
            self.svc.accept_transfer(invitation_id=inv["id"], user_id=self.bob["id"], idempotency_key="blk-acc")

    def test_clear_does_not_auto_unfreeze(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        self.svc.set_risk_status(admin_id=self.admin["id"], user_id=self.alice["id"], status="BLOCKED", reason="协查")
        self.svc.set_risk_status(admin_id=self.admin["id"], user_id=self.alice["id"], status="CLEAR", reason="排除嫌疑")
        # 解除风控不自动解冻，须管理员逐张核实
        self.assertEqual(self.svc.get_ticket(tid)["status"], "FROZEN")
        self.svc.unfreeze_ticket(admin_id=self.admin["id"], ticket_id=tid, reason="协查结束")
        self.assertEqual(self.svc.get_ticket(tid)["status"], "ACTIVE")


if __name__ == "__main__":
    unittest.main()

"""票券生命周期主流程测试：购票、转让、撤回、退回、扫码、复核、审计、脱敏。"""

import json
import unittest
from datetime import timedelta

from support import CUTOFF_UTC, ServiceTestCase

from app import (
    ConflictError,
    CutoffPassedError,
    MinorRestrictedError,
    PermissionDeniedError,
    RiskBlockedError,
    SeatUnavailableError,
    ValidationError,
)


class PurchaseTest(ServiceTestCase):
    def test_purchase_success_issues_token_and_chain(self):
        res = self.buy(self.alice)
        ticket = res["ticket"]
        self.assertEqual(ticket["status"], "ACTIVE")
        self.assertEqual(ticket["holder_id"], self.alice["id"])
        self.assertEqual(ticket["chain_seq"], 0)
        self.assertTrue(res["token"].startswith(f"v1.{ticket['id']}.0."))
        self.assertTrue(self.svc.verify_chain(ticket["id"]))

    def test_purchase_idempotent_retry_returns_same_ticket(self):
        r1 = self.buy(self.alice, key="retry-key-1")
        r2 = self.buy(self.alice, key="retry-key-1")
        self.assertEqual(r1["ticket"]["id"], r2["ticket"]["id"])
        self.assertFalse(r1["replayed"])
        self.assertTrue(r2["replayed"])
        self.assertEqual(len(self.svc.list_tickets(game_id=self.game["id"])), 1)

    def test_seat_cannot_be_sold_twice(self):
        self.buy(self.alice)
        with self.assertRaises(SeatUnavailableError):
            self.buy(self.bob)

    def test_minor_cannot_purchase(self):
        with self.assertRaises(MinorRestrictedError):
            self.buy(self.minor)

    def test_blocked_user_cannot_purchase(self):
        self.svc.set_risk_status(admin_id=self.admin["id"], user_id=self.alice["id"], status="BLOCKED", reason="黄牛嫌疑")
        with self.assertRaises(RiskBlockedError):
            self.buy(self.alice)

    def test_purchase_requires_idempotency_key(self):
        with self.assertRaises(ValidationError):
            self.svc.purchase_ticket(
                user_id=self.alice["id"], game_id=self.game["id"],
                section="A", row="1", number=1, idempotency_key="",
            )


class TransferTest(ServiceTestCase):
    def test_accept_invalidates_old_holder_immediately(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        old_token = res["token"]
        inv = self.svc.create_transfer(
            ticket_id=tid, from_user_id=self.alice["id"], to_user_id=self.bob["id"], idempotency_key="inv-1",
        )
        # 接收前原持有人仍然有效
        self.assertEqual(self.svc.get_ticket(tid)["holder_id"], self.alice["id"])
        acc = self.svc.accept_transfer(invitation_id=inv["id"], user_id=self.bob["id"], idempotency_key="acc-1")
        ticket = self.svc.get_ticket(tid)
        self.assertEqual(ticket["holder_id"], self.bob["id"])
        self.assertEqual(ticket["chain_seq"], 1)
        # 原持有人凭证立即失效：扫码进人工复核
        self.go_to_entry_window()
        stale = self.svc.scan_ticket(token=old_token, device_id="gate-A1", idempotency_key="scan-old")
        self.assertEqual(stale["result"], "DENIED_STALE_TOKEN")
        self.assertIsNotNone(stale["review_case_id"])
        # 新持有人正常入场
        fresh = self.svc.scan_ticket(token=acc["token"], device_id="gate-A1", idempotency_key="scan-new")
        self.assertEqual(fresh["result"], "ALLOWED")
        self.assertTrue(self.svc.verify_chain(tid))

    def test_accept_idempotent_replay_does_not_double_transfer(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        inv = self.svc.create_transfer(ticket_id=tid, from_user_id=self.alice["id"], to_user_id=self.bob["id"], idempotency_key="inv-2")
        a1 = self.svc.accept_transfer(invitation_id=inv["id"], user_id=self.bob["id"], idempotency_key="acc-dup")
        a2 = self.svc.accept_transfer(invitation_id=inv["id"], user_id=self.bob["id"], idempotency_key="acc-dup")
        self.assertTrue(a2["replayed"])
        self.assertEqual(a1["token"], a2["token"])
        self.assertEqual(len(self.svc.ticket_history(tid)["chain"]), 2)

    def test_only_one_pending_invitation_per_ticket(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        self.svc.create_transfer(ticket_id=tid, from_user_id=self.alice["id"], to_user_id=self.bob["id"], idempotency_key="inv-a")
        with self.assertRaises(ConflictError):
            self.svc.create_transfer(ticket_id=tid, from_user_id=self.alice["id"], to_user_id=self.carol["id"], idempotency_key="inv-b")

    def test_cancel_transfer_then_reinvite(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        inv = self.svc.create_transfer(ticket_id=tid, from_user_id=self.alice["id"], to_user_id=self.bob["id"], idempotency_key="inv-c")
        cancelled = self.svc.cancel_transfer(invitation_id=inv["id"], user_id=self.alice["id"])
        self.assertEqual(cancelled["status"], "CANCELLED")
        with self.assertRaises(ConflictError):
            self.svc.accept_transfer(invitation_id=inv["id"], user_id=self.bob["id"], idempotency_key="acc-c")
        # 撤回后可以转给其他人
        inv2 = self.svc.create_transfer(ticket_id=tid, from_user_id=self.alice["id"], to_user_id=self.carol["id"], idempotency_key="inv-d")
        acc = self.svc.accept_transfer(invitation_id=inv2["id"], user_id=self.carol["id"], idempotency_key="acc-d")
        self.assertEqual(acc["new_holder_id"], self.carol["id"])

    def test_non_holder_cannot_transfer_and_wrong_user_cannot_accept(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        with self.assertRaises(PermissionDeniedError):
            self.svc.create_transfer(ticket_id=tid, from_user_id=self.bob["id"], to_user_id=self.carol["id"], idempotency_key="inv-e")
        inv = self.svc.create_transfer(ticket_id=tid, from_user_id=self.alice["id"], to_user_id=self.bob["id"], idempotency_key="inv-f")
        with self.assertRaises(PermissionDeniedError):
            self.svc.accept_transfer(invitation_id=inv["id"], user_id=self.carol["id"], idempotency_key="acc-f")

    def test_transfer_to_self_rejected(self):
        res = self.buy(self.alice)
        with self.assertRaises(ValidationError):
            self.svc.create_transfer(ticket_id=res["ticket"]["id"], from_user_id=self.alice["id"], to_user_id=self.alice["id"], idempotency_key="inv-self")

    def test_minor_receive_requires_guardian(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        inv = self.svc.create_transfer(ticket_id=tid, from_user_id=self.alice["id"], to_user_id=self.minor["id"], idempotency_key="inv-minor")
        with self.assertRaises(MinorRestrictedError):
            self.svc.accept_transfer(invitation_id=inv["id"], user_id=self.minor["id"], idempotency_key="acc-m1")
        acc = self.svc.accept_transfer(
            invitation_id=inv["id"], user_id=self.minor["id"], idempotency_key="acc-m2",
            guardian_consent=True, guardian_id=self.alice["id"],
        )
        self.assertEqual(acc["new_holder_id"], self.minor["id"])

    def test_kid_cannot_receive_even_with_guardian(self):
        res = self.buy(self.alice)
        with self.assertRaises(MinorRestrictedError):
            self.svc.create_transfer(
                ticket_id=res["ticket"]["id"], from_user_id=self.alice["id"], to_user_id=self.kid["id"], idempotency_key="inv-kid",
            )

    def test_blocked_user_cannot_receive_new_invitation(self):
        self.svc.set_risk_status(admin_id=self.admin["id"], user_id=self.bob["id"], status="BLOCKED", reason="违规转售")
        res = self.buy(self.alice)
        with self.assertRaises(RiskBlockedError):
            self.svc.create_transfer(ticket_id=res["ticket"]["id"], from_user_id=self.alice["id"], to_user_id=self.bob["id"], idempotency_key="inv-blocked")

    def test_chain_tampering_is_detected(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        with self.svc.db.write() as conn:  # 模拟数据库被直接篡改
            conn.execute("UPDATE ownership_chain SET holder_id = 'u_attacker' WHERE ticket_id = ? AND seq = 0", (tid,))
        self.assertFalse(self.svc.verify_chain(tid))
        self.assertFalse(self.svc.verify_integrity()["ok"])


class ReturnTest(ServiceTestCase):
    def test_return_frees_seat_for_resale_and_old_token_dies(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        returned = self.svc.return_ticket(ticket_id=tid, user_id=self.alice["id"])
        self.assertEqual(returned["status"], "RETURNED")
        res2 = self.buy(self.bob)
        self.assertNotEqual(res2["ticket"]["id"], tid)
        self.go_to_entry_window()
        old = self.svc.scan_ticket(token=res["token"], device_id="gate-A1", idempotency_key="scan-ret")
        self.assertEqual(old["result"], "DENIED_RETURNED")
        new = self.svc.scan_ticket(token=res2["token"], device_id="gate-A1", idempotency_key="scan-new2")
        self.assertEqual(new["result"], "ALLOWED")
        # 按座位还原全部状态变化
        actions = [t["action"] for t in self.svc.seat_timeline(game_id=self.game["id"], section="A", row="1", number=1)]
        self.assertEqual(actions, ["PURCHASE", "RETURN", "PURCHASE", "SCAN", "SCAN"])

    def test_return_cancels_pending_invitation(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        inv = self.svc.create_transfer(ticket_id=tid, from_user_id=self.alice["id"], to_user_id=self.bob["id"], idempotency_key="inv-ret")
        self.svc.return_ticket(ticket_id=tid, user_id=self.alice["id"])
        self.assertEqual(self.svc.get_invitation(inv["id"])["status"], "CANCELLED")

    def test_return_after_cutoff_rejected(self):
        res = self.buy(self.alice)
        self.clock.set(CUTOFF_UTC + timedelta(seconds=1))
        with self.assertRaises(CutoffPassedError):
            self.svc.return_ticket(ticket_id=res["ticket"]["id"], user_id=self.alice["id"])


class ScanTest(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.res = self.buy(self.alice)
        self.tid = self.res["ticket"]["id"]
        self.go_to_entry_window()

    def test_scan_consumes_once_and_records_device_time_result(self):
        r = self.svc.scan_ticket(token=self.res["token"], device_id="gate-A1", gate_id="A1", idempotency_key="s1")
        self.assertEqual(r["result"], "ALLOWED")
        self.assertEqual(r["device_id"], "gate-A1")
        self.assertEqual(r["gate_id"], "A1")
        self.assertTrue(r["scanned_at"].startswith("2026-10-01T10:30:00"))
        self.assertEqual(self.svc.get_ticket(self.tid)["status"], "USED")
        again = self.svc.scan_ticket(token=self.res["token"], device_id="gate-A2", idempotency_key="s2")
        self.assertEqual(again["result"], "DENIED_ALREADY_USED")
        self.assertIsNotNone(again["review_case_id"])
        cases = self.svc.list_review_cases()
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["status"], "PENDING")

    def test_scan_idempotent_retry_returns_same_event(self):
        r1 = self.svc.scan_ticket(token=self.res["token"], device_id="gate-A1", idempotency_key="same-key")
        r2 = self.svc.scan_ticket(token=self.res["token"], device_id="gate-A1", idempotency_key="same-key")
        self.assertEqual(r1["id"], r2["id"])
        self.assertTrue(r2["replayed"])
        self.assertEqual(len(self.svc.ticket_history(self.tid)["scans"]), 1)
        self.assertEqual(self.svc.get_ticket(self.tid)["status"], "USED")

    def test_invalid_token_goes_to_review_not_entry(self):
        r = self.svc.scan_ticket(token="v1.t_fake.0.deadbeef", device_id="gate-A1", idempotency_key="bad-token")
        self.assertEqual(r["result"], "DENIED_INVALID_TOKEN")
        self.assertIsNotNone(r["review_case_id"])

    def test_too_early_denied_without_review_case(self):
        self.clock.set(self.clock() - timedelta(hours=5))
        r = self.svc.scan_ticket(token=self.res["token"], device_id="gate-A1", idempotency_key="early")
        self.assertEqual(r["result"], "DENIED_TOO_EARLY")
        self.assertIsNone(r["review_case_id"])
        self.assertEqual(self.svc.get_ticket(self.tid)["status"], "ACTIVE")


class ReviewTest(ServiceTestCase):
    def test_frozen_ticket_scan_goes_to_review_then_admin_admits(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        self.svc.freeze_ticket(admin_id=self.admin["id"], ticket_id=tid, reason="设备识别异常", basis="现场处置规程第8条")
        self.go_to_entry_window()
        r = self.svc.scan_ticket(token=res["token"], device_id="gate-A1", idempotency_key="fz-scan")
        self.assertEqual(r["result"], "DENIED_FROZEN")
        case_id = r["review_case_id"]
        # 冻结状态下不能直接放行，须先解冻
        with self.assertRaises(ConflictError):
            self.svc.resolve_review(case_id=case_id, admin_id=self.admin["id"], decision="ADMIT")
        self.svc.unfreeze_ticket(admin_id=self.admin["id"], ticket_id=tid, reason="人工核实为本人持票")
        case = self.svc.resolve_review(case_id=case_id, admin_id=self.admin["id"], decision="ADMIT", note="核验身份证一致，放行")
        self.assertEqual(case["status"], "ADMITTED")
        self.assertEqual(self.svc.get_ticket(tid)["status"], "USED")

    def test_reject_review(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        self.go_to_entry_window()
        self.svc.scan_ticket(token=res["token"], device_id="gate-A1", idempotency_key="first")
        dup = self.svc.scan_ticket(token=res["token"], device_id="gate-A2", idempotency_key="second")
        case = self.svc.resolve_review(case_id=dup["review_case_id"], admin_id=self.admin["id"], decision="REJECT", note="重复扫码，本人已入场")
        self.assertEqual(case["status"], "REJECTED")
        with self.assertRaises(ConflictError):
            self.svc.resolve_review(case_id=case["id"], admin_id=self.admin["id"], decision="ADMIT")

    def test_non_admin_cannot_resolve(self):
        res = self.buy(self.alice)
        self.go_to_entry_window()
        self.svc.scan_ticket(token=res["token"], device_id="gate-A1", idempotency_key="x1")
        dup = self.svc.scan_ticket(token=res["token"], device_id="gate-A1", idempotency_key="x2")
        with self.assertRaises(PermissionDeniedError):
            self.svc.resolve_review(case_id=dup["review_case_id"], admin_id=self.alice["id"], decision="ADMIT")


class AuditAndMaskingTest(ServiceTestCase):
    def test_customer_service_view_masks_identity(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        view = self.svc.customer_service_view(ticket_id=tid, cs_id=self.cs["id"])
        self.assertEqual(view["holder_summary"]["name"], "张**")
        self.assertEqual(view["holder_summary"]["phone"], "138****5678")
        blob = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("110101199001011234", blob)
        self.assertNotIn("13812345678", blob)
        self.assertNotIn("张伟民", blob)
        with self.assertRaises(PermissionDeniedError):
            self.svc.customer_service_view(ticket_id=tid, cs_id=self.bob["id"])
        full = self.svc.admin_view(ticket_id=tid, admin_id=self.admin["id"])
        self.assertEqual(full["holder"]["id_number"], "110101199001011234")

    def test_seat_timeline_reconstructs_every_change(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        inv = self.svc.create_transfer(ticket_id=tid, from_user_id=self.alice["id"], to_user_id=self.bob["id"], idempotency_key="tl-inv")
        self.svc.accept_transfer(invitation_id=inv["id"], user_id=self.bob["id"], idempotency_key="tl-acc")
        self.svc.freeze_ticket(admin_id=self.admin["id"], ticket_id=tid, reason="抽查", basis="抽检通知 09-30")
        self.svc.unfreeze_ticket(admin_id=self.admin["id"], ticket_id=tid, reason="抽查通过")
        self.go_to_entry_window()
        token = self.svc.ticket_token(ticket_id=tid, user_id=self.bob["id"])
        self.svc.scan_ticket(token=token, device_id="gate-A1", idempotency_key="tl-scan")
        timeline = self.svc.seat_timeline(game_id=self.game["id"], section="A", row="1", number=1)
        actions = [t["action"] for t in timeline]
        self.assertEqual(
            actions,
            ["PURCHASE", "TRANSFER_CREATED", "TRANSFER_ACCEPTED", "FREEZE", "UNFREEZE", "SCAN"],
        )
        accepted = timeline[2]
        self.assertEqual(accepted["before"]["holder_id"], self.alice["id"])
        self.assertEqual(accepted["after"]["holder_id"], self.bob["id"])

    def test_ticket_history_contains_chain_scans_reviews(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        self.go_to_entry_window()
        self.svc.scan_ticket(token=res["token"], device_id="gate-A1", idempotency_key="h1")
        self.svc.scan_ticket(token=res["token"], device_id="gate-A1", idempotency_key="h2")
        history = self.svc.ticket_history(tid)
        self.assertEqual(len(history["chain"]), 1)
        self.assertEqual(len(history["scans"]), 2)
        self.assertEqual(len(history["reviews"]), 1)
        self.assertEqual(history["scans"][0]["result"], "ALLOWED")
        self.assertEqual(history["scans"][1]["result"], "DENIED_ALREADY_USED")


if __name__ == "__main__":
    unittest.main()

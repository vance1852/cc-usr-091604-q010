"""闸机扫码：一次性消费、重复扫码、异常票转人工复核。"""

from threading import Barrier, Thread

from app.errors import Conflict, RuleViolation
from tests.support import ServiceCase


class ScanTest(ServiceCase):
    def _ready_ticket(self, holder="alice"):
        self.make_match("M1")
        self.make_seat("M1")
        self.make_customer(holder)
        return self.buy(holder, key="k-buy")["ticket_id"]

    def test_admit_consumes_once(self):
        tid = self._ready_ticket()
        r = self.svc.scan_ticket(tid, "gate-7")
        self.assertEqual(r["result"], "admitted")
        self.assertTrue(r["consumed"])
        self.assertEqual(self.svc.get_ticket(tid)["status"], "consumed")
        scan = self.conn.execute("SELECT * FROM scans WHERE id=?", (r["scan_id"],)).fetchone()
        self.assertEqual(scan["device_id"], "gate-7")
        self.assertEqual(scan["result"], "admitted")

    def test_duplicate_scan_rejected_not_admitted(self):
        tid = self._ready_ticket()
        self.svc.scan_ticket(tid, "gate-7")
        r2 = self.svc.scan_ticket(tid, "gate-8")
        self.assertEqual(r2["result"], "rejected")
        self.assertEqual(r2["reason"], "already_admitted")
        self.assertFalse(r2["consumed"])

    def test_unknown_code_rejected(self):
        r = self.svc.scan_ticket("not-a-real-code", "gate-1")
        self.assertEqual(r["result"], "rejected")
        self.assertEqual(r["reason"], "unknown_ticket")

    def test_returned_ticket_rejected(self):
        tid = self._ready_ticket()
        self.svc.return_ticket(tid, "alice", "ret-1")
        r = self.svc.scan_ticket(tid, "gate-1")
        self.assertEqual(r["result"], "rejected")
        self.assertEqual(r["reason"], "ticket_returned")

    def test_frozen_ticket_goes_to_review_never_auto_admitted(self):
        tid = self._ready_ticket()
        self.svc.freeze_batch([tid], "admin-1", "关联黄牛订单 #7")
        r = self.svc.scan_ticket(tid, "gate-3")
        self.assertEqual(r["result"], "review")
        self.assertEqual(r["reason"], "ticket_frozen")
        self.assertFalse(r["consumed"])
        self.assertEqual(self.svc.get_ticket(tid)["status"], "issued")  # 未消费

    def test_risk_holder_goes_to_review(self):
        tid = self._ready_ticket()
        self.svc.add_to_risk_list("alice", "现场风控预警", "admin-1")
        r = self.svc.scan_ticket(tid, "gate-3")
        self.assertEqual(r["result"], "review")
        self.assertEqual(r["reason"], "holder_on_risk_list")

    def test_presenter_mismatch_goes_to_review(self):
        tid = self._ready_ticket()
        self.make_customer("bob")
        r = self.svc.scan_ticket(tid, "gate-3", presenter_id="bob")
        self.assertEqual(r["result"], "review")
        self.assertEqual(r["reason"], "presenter_not_holder")

    def test_transfer_pending_scan_goes_to_review(self):
        tid = self._ready_ticket()
        self.make_customer("bob")
        self.svc.create_transfer_invite(tid, "alice", "bob", "tr-1")
        r = self.svc.scan_ticket(tid, "gate-3")
        self.assertEqual(r["result"], "review")
        self.assertEqual(r["reason"], "transfer_in_progress")

    def test_review_allow_then_consumes(self):
        tid = self._ready_ticket()
        self.svc.freeze_batch([tid], "admin-1", "待核订单")
        scan = self.svc.scan_ticket(tid, "gate-3")
        # 解冻后人工确认放行
        self.svc.unfreeze_ticket(tid, "admin-1", "核实为本人购票")
        out = self.svc.resolve_review(scan["scan_id"], "staff-9", "allow_entry",
                                      "身份证件一致，放行")
        self.assertEqual(out["decision"], "allow_entry")
        self.assertEqual(self.svc.get_ticket(tid)["status"], "consumed")
        # 复核结论一次性，不能重复处理
        with self.assertRaisesRegex(Conflict, "已完成复核"):
            self.svc.resolve_review(scan["scan_id"], "staff-9", "deny_entry", "重复操作")

    def test_review_deny_keeps_ticket_unconsumed(self):
        tid = self._ready_ticket()
        scan = self.svc.scan_ticket(tid, "gate-3", presenter_id="bob")
        self.svc.resolve_review(scan["scan_id"], "staff-9", "deny_entry",
                                "持票人未到场且无有效转让")
        self.assertEqual(self.svc.get_ticket(tid)["status"], "issued")
        # 同一非持票人再扫：仍不放行，重新进入复核
        again = self.svc.scan_ticket(tid, "gate-3", presenter_id="bob")
        self.assertEqual(again["result"], "review")
        # 原持票人本人到场扫码：正常放行（票未被消费）
        own = self.svc.scan_ticket(tid, "gate-3", presenter_id="alice")
        self.assertEqual(own["result"], "admitted")

    def test_review_requires_note(self):
        tid = self._ready_ticket()
        scan = self.svc.scan_ticket(tid, "gate-3", presenter_id="x")
        with self.assertRaisesRegex(RuleViolation, "处理说明"):
            self.svc.resolve_review(scan["scan_id"], "staff-9", "allow_entry", "")

    def test_concurrent_double_scan_only_one_admitted(self):
        """两台闸机并发扫同一张票：恰好一次 admitted。"""
        tid = self._ready_ticket()
        barrier = Barrier(2)
        results = []

        def scan(device):
            svc = self.new_conn_service()
            barrier.wait()
            results.append(svc.scan_ticket(tid, device))

        threads = [Thread(target=scan, args=(d,)) for d in ("gate-1", "gate-2")]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=10)

        self.assertEqual(sorted(r["result"] for r in results), ["admitted", "rejected"])
        admitted = [r for r in results if r["result"] == "admitted"][0]
        rejected = [r for r in results if r["result"] == "rejected"][0]
        self.assertEqual(rejected["reason"], "already_admitted")
        # 消费记录恰好一条
        consumed = self.conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE consumed=1").fetchone()["c"]
        self.assertEqual(consumed, 1)
        self.assertEqual(admitted["device_id"][:5], "gate-")

    def test_scan_idempotency_key_replays_on_retry(self):
        tid = self._ready_ticket()
        r1 = self.svc.scan_ticket(tid, "gate-1", idempotency_key="scan-key-1")
        r2 = self.svc.scan_ticket(tid, "gate-1", idempotency_key="scan-key-1")
        self.assertEqual(r1["scan_id"], r2["scan_id"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM scans").fetchone()["c"], 1)

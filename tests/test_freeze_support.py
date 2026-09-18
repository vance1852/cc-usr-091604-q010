"""管理员冻结（单张/整批）与客服脱敏视图。"""

from app.errors import Conflict, NotFound, RuleViolation
from tests.support import ServiceCase


class FreezeTest(ServiceCase):
    def _three_tickets(self):
        self.make_match("M1")
        self.make_seat("M1", section="A区", row="1", no="1")
        self.make_seat("M1", section="A区", row="1", no="2")
        self.make_seat("M1", section="B区", row="2", no="3")
        self.make_customer("a")
        self.make_customer("b")
        self.make_customer("c")
        t1 = self.buy("a", section="A区", row="1", no="1", key="k1")["ticket_id"]
        t2 = self.buy("b", section="A区", row="1", no="2", key="k2")["ticket_id"]
        t3 = self.buy("c", section="B区", row="2", no="3", key="k3")["ticket_id"]
        return t1, t2, t3

    def test_freeze_requires_reason(self):
        t1, _, _ = self._three_tickets()
        with self.assertRaisesRegex(RuleViolation, "依据"):
            self.svc.freeze_batch([t1], "admin-1", "  ")

    def test_batch_freeze_atomic_same_batch_id(self):
        t1, t2, t3 = self._three_tickets()
        out = self.svc.freeze_batch([t1, t2, t3], "admin-1",
                                    "同一批黄牛订单，证据: 支付卡尾号 0007")
        self.assertEqual(sorted(out["frozen"]), sorted([t1, t2, t3]))
        self.assertEqual(out["already_frozen"], [])
        rows = self.conn.execute("SELECT DISTINCT batch_id FROM freezes").fetchall()
        self.assertEqual(len(rows), 1)  # 整批共享批次号
        for tid in (t1, t2, t3):
            self.assertTrue(self.svc.get_ticket(tid)["frozen"])

    def test_batch_aborts_rolls_back_when_any_ticket_missing(self):
        t1, t2, _ = self._three_tickets()
        with self.assertRaises(NotFound):
            self.svc.freeze_batch([t1, "missing-ticket", t2], "admin-1",
                                  "不应部分生效")
        # 整批回滚：t1/t2 未冻结
        self.assertFalse(self.svc.get_ticket(t1)["frozen"])
        self.assertFalse(self.svc.get_ticket(t2)["frozen"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM freezes").fetchone()["c"], 0)

    def test_freeze_blocks_transfer_and_unfreeze_restores(self):
        t1, _, _ = self._three_tickets()
        self.make_customer("d")
        self.svc.freeze_batch([t1], "admin-1", "调查中")
        with self.assertRaisesRegex(RuleViolation, "冻结"):
            self.svc.create_transfer_invite(t1, "a", "d", "tr-x")
        self.svc.unfreeze_ticket(t1, "admin-1", "调查排除")
        inv = self.svc.create_transfer_invite(t1, "a", "d", "tr-1")
        self.assertEqual(inv["status"], "pending")

    def test_unfreeze_requires_note(self):
        t1, _, _ = self._three_tickets()
        self.svc.freeze_batch([t1], "admin-1", "调查中")
        with self.assertRaisesRegex(RuleViolation, "说明"):
            self.svc.unfreeze_ticket(t1, "admin-1", "")
        # t1 仍冻结
        self.assertTrue(self.svc.get_ticket(t1)["frozen"])

    def test_idempotent_batch_freeze_retry(self):
        t1, t2, _ = self._three_tickets()
        a = self.svc.freeze_batch([t1, t2], "admin-1", "依据 X", idempotency_key="fz-1")
        b = self.svc.freeze_batch([t1, t2], "admin-1", "依据 X", idempotency_key="fz-1")
        self.assertEqual(a["batch_id"], b["batch_id"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM freezes").fetchone()["c"], 2)


class SupportViewTest(ServiceCase):
    def test_support_sees_masked_summary_only(self):
        self.make_match("M1")
        self.make_seat("M1")
        self.make_customer("alice", doc="110101199503034567", phone="13911112222")
        self.buy("alice", key="k1")
        summary = self.svc.customer_support_summary("alice")
        self.assertEqual(summary["name_masked"], "球" + "*" * (len("球迷alice") - 1))
        self.assertIn("*", summary["id_doc_masked"])
        self.assertNotIn("4567", summary["id_doc_masked"].replace("*", ""))
        self.assertNotIn("2222", summary["phone_masked"].replace("*", ""))
        self.assertFalse(summary["on_risk_list"])
        self.assertEqual(summary["tickets"][0]["status"], "issued")
        # 摘要里绝不能出现证件哈希以外的明文 PII
        blob = repr(summary)
        self.assertNotIn("110101199503034567", blob)
        self.assertNotIn("13911112222", blob)

    def test_support_summary_flags_risk(self):
        self.make_customer("alice")
        self.svc.add_to_risk_list("alice", "现场预警", "admin-1")
        summary = self.svc.customer_support_summary("alice")
        self.assertTrue(summary["on_risk_list"])
        self.assertEqual(summary["risk_reason"], "现场预警")

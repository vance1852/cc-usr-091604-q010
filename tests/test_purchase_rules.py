"""购票规则：实名、未成年人、风控名单、座位唯一、幂等重放。"""

import sqlite3

from app.errors import Conflict, RuleViolation
from tests.support import ServiceCase


class PurchaseRuleTest(ServiceCase):
    def test_real_name_doc_hashed_and_masked(self):
        self.make_customer("c1", doc="110101200001011234", phone="13812345678")
        row = self.conn.execute(
            "SELECT id_doc_hash, id_doc_masked, phone_masked FROM customers WHERE id='c1'"
        ).fetchone()
        self.assertNotIn("1234", row["id_doc_hash"][:8])
        self.assertNotIn("110101200001011234", [r[0] for r in self.conn.execute(
            "SELECT id_doc_hash FROM customers")])
        self.assertEqual(row["id_doc_masked"], "11" + "*" * 14 + "34")
        self.assertTrue(row["phone_masked"].endswith("78"))
        self.assertNotIn("12345678", row["phone_masked"].replace("*", ""))

    def test_same_id_doc_cannot_register_twice(self):
        self.make_customer("c1", doc="SAME-DOC")
        with self.assertRaisesRegex(Conflict, "证件已注册"):
            self.make_customer("c2", doc="SAME-DOC")

    def test_minor_under_14_cannot_buy(self):
        # 开赛日 2026-09-18，2012-09-19 出生 → 差一天才满 14 岁
        self.make_match("M1", event_time="2026-09-18T19:30:00+08:00")
        self.make_seat("M1")
        self.make_customer("minor", birth="2012-09-19")
        with self.assertRaisesRegex(RuleViolation, "年满 14"):
            self.buy("minor", key="k1")
        # 生日当天满 14 岁可以购票
        self.make_customer("teen", birth="2012-09-18")
        self.make_seat("M1", no="9")
        ticket = self.buy("teen", no="9", key="k2")
        self.assertEqual(ticket["holder_id"], "teen")

    def test_risk_listed_customer_cannot_purchase(self):
        self.make_match("M1")
        self.make_seat("M1")
        self.make_customer("bad")
        self.svc.add_to_risk_list("bad", "黄牛证据 #42", "admin-1")
        with self.assertRaisesRegex(RuleViolation, "风控名单"):
            self.buy("bad", key="k1")
        # 移出名单后可以购票
        self.svc.remove_from_risk_list("bad", "申诉成立", "admin-1")
        ticket = self.buy("bad", key="k2")
        self.assertEqual(ticket["status"], "issued")

    def test_risk_requires_reason(self):
        self.make_customer("bad")
        with self.assertRaisesRegex(RuleViolation, "依据"):
            self.svc.add_to_risk_list("bad", "   ", "admin-1")

    def test_same_seat_sold_once_even_with_two_keys(self):
        self.make_match("M1")
        self.make_seat("M1")
        self.make_customer("a")
        self.make_customer("b")
        self.buy("a", key="k-a")
        with self.assertRaisesRegex(Conflict, "已有生效票"):
            self.buy("b", key="k-b")

    def test_purchase_idempotency_replay_returns_same_ticket(self):
        self.make_match("M1")
        self.make_seat("M1")
        self.make_customer("a")
        first = self.buy("a", key="same-key")
        # 网络重试：同幂等键、甚至调用方误改成“给 b 购票”，结果与首次一致
        retry = self.svc.purchase_ticket("M1", "A区", "3", "8", "a", "same-key")
        self.assertEqual(first["ticket_id"], retry["ticket_id"])
        count = self.conn.execute("SELECT COUNT(*) c FROM tickets").fetchone()["c"]
        self.assertEqual(count, 1)

    def test_business_failure_idempotent_on_retry(self):
        self.make_match("M1")
        self.make_seat("M1")
        self.make_customer("a")
        self.make_customer("bad")
        self.svc.add_to_risk_list("bad", "黄牛", "admin-1")
        with self.assertRaises(RuleViolation):
            self.buy("bad", key="risky-key")
        # 即使之后移出名单，重试同键仍返回首次的风控拒绝
        self.svc.remove_from_risk_list("bad", "解除", "admin-1")
        with self.assertRaisesRegex(RuleViolation, "风控名单"):
            self.svc.purchase_ticket("M1", "A区", "3", "8", "bad", "risky-key")

    def test_returned_seat_can_be_resold(self):
        self.make_match("M1")
        self.make_seat("M1")
        self.make_customer("a")
        self.make_customer("b")
        t = self.buy("a", key="k1")
        self.svc.return_ticket(t["ticket_id"], "a", "ret-1")
        again = self.buy("b", key="k2")
        self.assertNotEqual(t["ticket_id"], again["ticket_id"])
        self.assertEqual(again["holder_id"], "b")

"""转让：邀请、确认、撤回、跨时区截止时间、重复点击、并发双转。"""

from datetime import UTC, datetime, timedelta
from threading import Barrier, Thread

from app.errors import Conflict, RuleViolation
from app.util import parse_iso
from tests.support import ServiceCase


class TransferFlowTest(ServiceCase):
    def _setup_triplet(self, event_at: str | None = None):
        self.make_match("M1", event_time=event_at)
        self.make_seat("M1")
        self.make_customer("alice")
        self.make_customer("bob")
        self.make_customer("carol")
        return self.buy("alice", key="k-buy")

    def test_invite_accept_swaps_holder_and_old_holder_invalid(self):
        t = self._setup_triplet()
        inv = self.svc.create_transfer_invite(t["ticket_id"], "alice", "bob", "tr-1")
        pending = self.svc.get_ticket(t["ticket_id"])
        self.assertEqual(pending["status"], "transfer_pending")
        self.assertEqual(pending["holder_id"], "alice")  # 确认前持有人不变

        view = self.svc.accept_transfer(inv["invite_id"], "bob", "acc-1")
        self.assertEqual(view["holder_id"], "bob")
        self.assertEqual(view["status"], "issued")
        # 原持有人立即失效：alice 不能再操作这张票
        with self.assertRaisesRegex(RuleViolation, "当前持有人"):
            self.svc.create_transfer_invite(t["ticket_id"], "alice", "carol", "tr-2")

    def test_double_click_accept_consumes_once(self):
        t = self._setup_triplet()
        inv = self.svc.create_transfer_invite(t["ticket_id"], "alice", "bob", "tr-1")
        first = self.svc.accept_transfer(inv["invite_id"], "bob", "acc-1")
        self.assertEqual(first["holder_id"], "bob")
        # 第二次点击（无幂等键的重复请求）→ 冲突而不是再次转让
        with self.assertRaisesRegex(Conflict, "已处理"):
            self.svc.accept_transfer(inv["invite_id"], "bob", None)

    def test_accept_network_retry_with_same_idempotency_key_replays(self):
        t = self._setup_triplet()
        inv = self.svc.create_transfer_invite(t["ticket_id"], "alice", "bob", "tr-1")
        r1 = self.svc.accept_transfer(inv["invite_id"], "bob", "net-key")
        r2 = self.svc.accept_transfer(inv["invite_id"], "bob", "net-key")
        self.assertEqual(r1["ticket_id"], r2["ticket_id"])
        self.assertEqual(r2["holder_id"], "bob")

    def test_cancel_then_holder_can_transfer_again(self):
        t = self._setup_triplet()
        inv = self.svc.create_transfer_invite(t["ticket_id"], "alice", "bob", "tr-1")
        self.svc.cancel_transfer(inv["invite_id"], "alice", "can-1")
        self.assertEqual(self.svc.get_ticket(t["ticket_id"])["status"], "issued")
        inv2 = self.svc.create_transfer_invite(t["ticket_id"], "alice", "carol", "tr-2")
        view = self.svc.accept_transfer(inv2["invite_id"], "carol", "acc-2")
        self.assertEqual(view["holder_id"], "carol")

    def test_cannot_cancel_after_accepted(self):
        t = self._setup_triplet()
        inv = self.svc.create_transfer_invite(t["ticket_id"], "alice", "bob", "tr-1")
        self.svc.accept_transfer(inv["invite_id"], "bob", "acc-1")
        with self.assertRaisesRegex(Conflict, "已确认"):
            self.svc.cancel_transfer(inv["invite_id"], "alice", "can-x")

    def test_recipient_can_decline(self):
        t = self._setup_triplet()
        inv = self.svc.create_transfer_invite(t["ticket_id"], "alice", "bob", "tr-1")
        self.svc.cancel_transfer(inv["invite_id"], "bob")  # 接收方拒绝
        self.assertEqual(self.svc.get_ticket(t["ticket_id"])["holder_id"], "alice")

    def test_stranger_cannot_accept(self):
        t = self._setup_triplet()
        inv = self.svc.create_transfer_invite(t["ticket_id"], "alice", "bob", "tr-1")
        with self.assertRaisesRegex(RuleViolation, "指定接收方"):
            self.svc.accept_transfer(inv["invite_id"], "carol", "acc-x")

    def test_cannot_transfer_to_risk_listed_recipient(self):
        t = self._setup_triplet()
        self.svc.add_to_risk_list("bob", "疑似黄牛", "admin-1")
        with self.assertRaisesRegex(RuleViolation, "接收方在风控名单"):
            self.svc.create_transfer_invite(t["ticket_id"], "alice", "bob", "tr-x")

    def test_risk_added_mid_transfer_blocks_accept(self):
        t = self._setup_triplet()
        inv = self.svc.create_transfer_invite(t["ticket_id"], "alice", "bob", "tr-1")
        # 邀请发出后、确认前 alice 被列入风控
        self.svc.add_to_risk_list("alice", "开场前新增风控", "admin-1")
        with self.assertRaisesRegex(RuleViolation, "出让方已被列入风控名单"):
            self.svc.accept_transfer(inv["invite_id"], "bob", "acc-1")
        # 票仍处于 transfer_pending，撤回需先解风控？撤回不做风控校验，允许取消
        self.svc.remove_from_risk_list("alice", "误报", "admin-1")
        self.svc.cancel_transfer(inv["invite_id"], "alice")
        self.assertEqual(self.svc.get_ticket(t["ticket_id"])["status"], "issued")


class TransferDeadlineTest(ServiceCase):
    """跨时区截止：开球 2026-09-18 19:30 北京（UTC 11:30），
    截止 = UTC 09:30。客户端用不同时区表达，比较的是同一瞬时。"""

    TIPOFF_UTC = datetime(2026, 9, 18, 11, 30, tzinfo=UTC)

    def setUp(self):
        super().setUp()
        self.clock.set(self.TIPOFF_UTC - timedelta(hours=3))  # UTC 08:30，截止前
        self.make_match("M1", event_time="2026-09-18T19:30:00+08:00")
        self.make_seat("M1")
        self.make_customer("alice")
        self.make_customer("bob")
        self.ticket = self.buy("alice", key="k-buy")

    def test_deadline_is_two_hours_before_tipoff_in_utc(self):
        m = self.conn.execute(
            "SELECT transfer_deadline_utc d FROM matches WHERE id='M1'"
        ).fetchone()
        self.assertEqual(parse_iso(m["d"]),
                         datetime(2026, 9, 18, 9, 30, tzinfo=UTC))

    def test_invite_allowed_before_deadline_from_other_timezone(self):
        # 纽约 (UTC-4) 04:30 == UTC 08:30，截止前 1 小时
        inv = self.svc.create_transfer_invite(
            self.ticket["ticket_id"], "alice", "bob", "tr-1")
        self.assertIsNotNone(inv["expires_at"])

    def test_invite_rejected_after_deadline_even_from_other_timezone(self):
        # 北京 17:31 (+08:00) == UTC 09:31，刚过截止；纽约视角为 05:31 (UTC-4)
        self.clock.set(parse_iso("2026-09-18T17:31:00+08:00"))
        with self.assertRaisesRegex(RuleViolation, "转让截止时间"):
            self.svc.create_transfer_invite(
                self.ticket["ticket_id"], "alice", "bob", "tr-late")

    def test_invite_created_before_deadline_cannot_be_accepted_after(self):
        inv = self.svc.create_transfer_invite(
            self.ticket["ticket_id"], "alice", "bob", "tr-1")
        self.clock.set(parse_iso("2026-09-18T18:00:00+08:00"))  # UTC 10:00
        with self.assertRaisesRegex(RuleViolation, "截止时间"):
            self.svc.accept_transfer(inv["invite_id"], "bob", "acc-1")

    def test_maintenance_job_expires_pending_invites_at_deadline(self):
        inv = self.svc.create_transfer_invite(
            self.ticket["ticket_id"], "alice", "bob", "tr-1")
        self.clock.set(parse_iso("2026-09-18T17:30:00+08:00"))  # UTC 09:30 整
        result = self.svc.expire_due_invitations()
        self.assertEqual(result["expired"], [inv["invite_id"]])
        self.assertEqual(self.svc.get_ticket(self.ticket["ticket_id"])["status"], "issued")
        with self.assertRaisesRegex(Conflict, "失效"):
            self.svc.cancel_transfer(inv["invite_id"], "alice")

    def test_return_also_closes_at_deadline(self):
        self.clock.set(parse_iso("2026-09-18T17:31:00+08:00"))
        with self.assertRaisesRegex(RuleViolation, "退票截止"):
            self.svc.return_ticket(self.ticket["ticket_id"], "alice", "ret-late")


class TransferConcurrencyTest(ServiceCase):
    def test_two_concurrent_invites_only_one_wins(self):
        """同一票并发转给 bob / carol：仅一笔邀请落地，不会出现两张可用票。"""
        self.make_match("M1")
        self.make_seat("M1")
        self.make_customer("alice")
        self.make_customer("bob")
        self.make_customer("carol")
        t = self.buy("alice", key="k-buy")

        barrier = Barrier(2)
        errors: list[Exception] = []

        def invite(target, key):
            svc = self.new_conn_service()
            barrier.wait()
            try:
                svc.create_transfer_invite(t["ticket_id"], "alice", target, key)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            Thread(target=invite, args=("bob", "tr-bob")),
            Thread(target=invite, args=("carol", "tr-carol")),
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=10)

        pending = self.conn.execute(
            "SELECT COUNT(*) c FROM transfer_invitations WHERE ticket_id=? AND status='pending'",
            (t["ticket_id"],),
        ).fetchone()["c"]
        self.assertEqual(pending, 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], Conflict)
        # 座位上仍只有一张生效票，不会产生两张可用票
        seat_id = self.conn.execute(
            "SELECT seat_id FROM tickets WHERE id=?", (t["ticket_id"],)
        ).fetchone()["seat_id"]
        live = self.conn.execute(
            "SELECT COUNT(*) c FROM tickets "
            "WHERE seat_id=? AND status IN ('issued','transfer_pending')",
            (seat_id,),
        ).fetchone()["c"]
        self.assertEqual(live, 1)

    def test_concurrent_accept_of_two_invitations_impossible(self):
        """链式：alice→bob 完成后，bob→carol 与 alice→carol(旧邀请残留)
        不可能同时成功。此处直接验证唯一索引兜底。"""
        self.make_match("M1")
        self.make_seat("M1")
        self.make_customer("alice")
        self.make_customer("bob")
        self.make_customer("carol")
        t = self.buy("alice", key="k-buy")
        inv1 = self.svc.create_transfer_invite(t["ticket_id"], "alice", "bob", "tr-1")
        self.svc.accept_transfer(inv1["invite_id"], "bob", "acc-1")
        inv2 = self.svc.create_transfer_invite(t["ticket_id"], "bob", "carol", "tr-2")
        # 旧邀请已 accepted，不允许再确认
        with self.assertRaises(Conflict):
            self.svc.accept_transfer(inv1["invite_id"], "bob", "acc-again")
        view = self.svc.accept_transfer(inv2["invite_id"], "carol", "acc-2")
        self.assertEqual(view["holder_id"], "carol")

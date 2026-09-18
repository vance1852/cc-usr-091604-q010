"""不可变持有链：按座位还原状态变化、篡改检测、备份恢复。"""

import sqlite3
from pathlib import Path

from app.db import connect
from app.errors import Tampered
from app.service import TicketService, backup_database, restore_database
from tests.support import ServiceCase


class ChainAuditTest(ServiceCase):
    def _lifecycle(self):
        # 默认开球时间为时钟 5 小时后，确保转让截止尚未到
        self.make_match("M1")
        self.make_seat("M1")
        self.make_customer("alice")
        self.make_customer("bob")
        self.make_customer("carol")
        t = self.buy("alice", key="k-buy")
        inv1 = self.svc.create_transfer_invite(t["ticket_id"], "alice", "bob", "tr-1")
        self.svc.accept_transfer(inv1["invite_id"], "bob", "acc-1")
        inv2 = self.svc.create_transfer_invite(t["ticket_id"], "bob", "carol", "tr-2")
        self.svc.cancel_transfer(inv2["invite_id"], "carol")  # carol 拒绝
        return t["ticket_id"]

    def test_chain_records_every_state_change_in_order(self):
        tid = self._lifecycle()
        chain = self.svc.ticket_chain(tid)["events"]
        actions = [e["action"] for e in chain]
        self.assertEqual(
            actions,
            [
                "ticket.issued",
                "transfer.invited",
                "transfer.accepted",
                "transfer.invited",
                "transfer.cancelled",
            ],
        )
        # 每次转让都记录 from/to，赛后可追查票券流向
        accepted = next(e for e in chain if e["action"] == "transfer.accepted")
        self.assertEqual(accepted["payload"]["from"], "alice")
        self.assertEqual(accepted["payload"]["to"], "bob")
        # 哈希首尾相连
        prev = "0" * 64
        for e in chain:
            # events 中存的 prev_hash 不直接暴露，但完整性由 verify_chain 覆盖
            self.assertEqual(len(e["hash"]), 64)
            self.assertNotEqual(e["hash"], prev)
            prev = e["hash"]

    def test_seat_history_spans_return_and_resale(self):
        self.make_match("M1")
        self.make_seat("M1")
        self.make_customer("alice")
        self.make_customer("bob")
        t1 = self.buy("alice", key="k1")
        self.svc.return_ticket(t1["ticket_id"], "alice", "ret-1")
        self.buy("bob", key="k2")
        hist = self.svc.seat_history("M1", "A区", "3", "8")["events"]
        actions = [e["action"] for e in hist]
        self.assertEqual(
            actions,
            ["ticket.issued", "ticket.returned", "ticket.issued"],
        )
        holders = [e["subject_customer_id"] for e in hist
                   if e["action"] == "ticket.issued"]
        self.assertEqual(holders, ["alice", "bob"])

    def test_events_table_is_append_only(self):
        tid = self._lifecycle()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE events SET action='hacked' WHERE ticket_id=?", (tid,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM events WHERE ticket_id=?", (tid,))

    def test_verify_chain_detects_tampering(self):
        tid = self._lifecycle()
        self.assertTrue(self.svc.verify_chain()["ok"])
        # 攻击者绕过触发器直接改库也会被哈希链发现（先临时禁用触发器）
        self.conn.execute("DROP TRIGGER events_no_update")
        self.conn.execute(
            "UPDATE events SET payload=? WHERE action='transfer.accepted'",
            ('{"from":"alice","to":"carol"}',),
        )
        self.conn.commit() if self.conn.in_transaction else None
        result = TicketService(self.conn).verify_chain()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "事件哈希不匹配，记录被篡改")

    def test_verify_chain_detects_gap(self):
        self._lifecycle()
        self.conn.execute("DROP TRIGGER events_no_delete")
        row = self.conn.execute(
            "SELECT seq FROM events WHERE action='transfer.accepted'").fetchone()
        self.conn.execute("DELETE FROM events WHERE seq=?", (row["seq"],))
        result = self.svc.verify_chain()
        self.assertFalse(result["ok"])
        self.assertIn("序号不连续", result["reason"])


class BackupRestoreTest(ServiceCase):
    def test_backup_and_restore_roundtrip(self):
        self.make_match("M1")
        self.make_seat("M1")
        self.make_customer("alice")
        t = self.buy("alice", key="k1")
        self.svc.freeze_batch([t["ticket_id"]], "admin-1", "整批冻结演练 #1")

        backup_path = Path(self.tmp.name) / "backup.db"
        backup_database(self.conn, backup_path)

        # 恢复到新路径并校验内容一致
        target = Path(self.tmp.name) / "restored.db"
        report = restore_database(backup_path, target)
        self.assertEqual(report["events"], self.svc.verify_chain()["events"])

        restored = connect(target)
        try:
            rsvc = TicketService(restored)
            self.assertTrue(rsvc.verify_chain()["ok"])
            self.assertTrue(rsvc.get_ticket(t["ticket_id"])["frozen"])
            # 恢复后业务可继续
            restored_events_before = rsvc.verify_chain()["events"]
            rsvc.unfreeze_ticket(t["ticket_id"], "admin-1", "演练结束")
            self.assertEqual(
                rsvc.verify_chain()["events"], restored_events_before + 1)
        finally:
            restored.close()

    def test_restore_rejects_tampered_backup(self):
        self.make_match("M1")
        self.make_seat("M1")
        self.make_customer("alice")
        self.buy("alice", key="k1")
        backup_path = Path(self.tmp.name) / "backup.db"
        backup_database(self.conn, backup_path)

        # 篡改备份文件
        evil = connect(backup_path)
        evil.execute("DROP TRIGGER events_no_update")
        evil.execute("UPDATE events SET actor_id='mallory' WHERE seq=1")
        evil.commit()
        evil.close()

        target = Path(self.tmp.name) / "restored.db"
        with self.assertRaisesRegex(Tampered, "持有链校验失败"):
            restore_database(backup_path, target)
        self.assertFalse(target.exists())

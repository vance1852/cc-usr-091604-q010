"""数据库恢复测试：事务原子性、备份恢复、重开一致性。"""

import os
import shutil
import unittest

from support import ServiceTestCase

from app.service import TicketLifecycleService


class RecoveryTest(ServiceTestCase):
    def test_failed_accept_rolls_back_completely(self):
        """接收事务中途崩溃：邀请、持有人、持有链、审计全部保持原状。"""
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        inv = self.svc.create_transfer(
            ticket_id=tid, from_user_id=self.alice["id"], to_user_id=self.bob["id"], idempotency_key="rec-inv",
        )

        def boom(point):
            if point == "accept_transfer.before_commit":
                raise RuntimeError("模拟进程崩溃")

        self.svc._fault_hook = boom
        with self.assertRaises(RuntimeError):
            self.svc.accept_transfer(invitation_id=inv["id"], user_id=self.bob["id"], idempotency_key="rec-acc")
        self.svc._fault_hook = None

        self.assertEqual(self.svc.get_invitation(inv["id"])["status"], "PENDING")
        ticket = self.svc.get_ticket(tid)
        self.assertEqual(ticket["holder_id"], self.alice["id"])
        self.assertEqual(ticket["chain_seq"], 0)
        history = self.svc.ticket_history(tid)
        self.assertEqual(len(history["chain"]), 1)
        self.assertNotIn("TRANSFER_ACCEPTED", [a["action"] for a in history["audits"]])
        self.assertTrue(self.svc.verify_integrity()["ok"])

        # 故障恢复后同一邀请可以正常完成
        acc = self.svc.accept_transfer(invitation_id=inv["id"], user_id=self.bob["id"], idempotency_key="rec-acc-2")
        self.assertEqual(acc["new_holder_id"], self.bob["id"])
        self.assertTrue(self.svc.verify_chain(tid))

    def test_backup_and_restore_returns_to_snapshot(self):
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        backup_path = os.path.join(self.tmp.name, "backup.db")
        self.svc.db.backup_to(backup_path)
        # 快照之后继续操作：转让并接收
        inv = self.svc.create_transfer(
            ticket_id=tid, from_user_id=self.alice["id"], to_user_id=self.bob["id"], idempotency_key="bk-inv",
        )
        self.svc.accept_transfer(invitation_id=inv["id"], user_id=self.bob["id"], idempotency_key="bk-acc")
        self.assertEqual(self.svc.get_ticket(tid)["holder_id"], self.bob["id"])
        # 模拟灾难：主库文件丢失
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.remove(path)
        # 从备份恢复
        shutil.copy(backup_path, self.db_path)
        svc2 = self.new_service()
        ticket = svc2.get_ticket(tid)
        self.assertEqual(ticket["holder_id"], self.alice["id"])  # 回到快照点
        self.assertEqual(ticket["status"], "ACTIVE")
        self.assertTrue(svc2.verify_integrity()["ok"])
        self.assertTrue(svc2.verify_chain(tid))
        # 恢复后业务可以继续
        inv2 = svc2.create_transfer(
            ticket_id=tid, from_user_id=self.alice["id"], to_user_id=self.carol["id"], idempotency_key="bk-inv-2",
        )
        acc2 = svc2.accept_transfer(invitation_id=inv2["id"], user_id=self.carol["id"], idempotency_key="bk-acc-2")
        self.assertEqual(acc2["new_holder_id"], self.carol["id"])
        self.assertTrue(svc2.verify_integrity()["ok"])

    def test_reopen_preserves_state_chain_and_tokens(self):
        """重启服务（不显式提供密钥）后：数据、持有链、入场凭证全部可用。"""
        res = self.buy(self.alice)
        tid = res["ticket"]["id"]
        token = res["token"]
        svc2 = TicketLifecycleService(self.db_path, clock=self.clock)  # 密钥从库中加载
        self.assertTrue(svc2.verify_integrity()["ok"])
        self.assertTrue(svc2.verify_chain(tid))
        self.go_to_entry_window()
        r = svc2.scan_ticket(token=token, device_id="gate-A1", idempotency_key="reopen-scan")
        self.assertEqual(r["result"], "ALLOWED")


if __name__ == "__main__":
    unittest.main()

"""HTTP JSON 接口端到端测试（真实 socket，含并发重试）。"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.client import RemoteDisconnected
from pathlib import Path

from app.api import build_server


class ApiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "tickets.db")
        self.server = build_server(self.db_path, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, method: str, path: str, body: dict | None = None,
             role: str | None = None, actor: str | None = None,
             idem: str | None = None) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if role:
            req.add_header("X-Role", role)
        if actor:
            req.add_header("X-Actor-Id", actor)
        if idem:
            req.add_header("Idempotency-Key", idem)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _bootstrap(self):
        self.call("POST", "/admin/matches", {
            "match_id": "M1", "name": "新年大战", "venue": "城市球馆",
            "event_time": "2026-12-31T19:30:00+08:00",
        }, role="admin")
        self.call("POST", "/admin/matches/M1/seats",
                  {"seats": [{"section": "A区", "row": "3", "no": "8"},
                             {"section": "A区", "row": "3", "no": "9"}]},
                  role="admin")
        self.call("POST", "/customers", {
            "customer_id": "alice", "full_name": "爱丽丝",
            "id_doc": "110101199001011234", "birth_date": "1990-01-01",
            "phone": "13800001111",
        })
        self.call("POST", "/customers", {
            "customer_id": "bob", "full_name": "鲍勃",
            "id_doc": "110101199002022345", "birth_date": "1990-02-02",
            "phone": "13800002222",
        })

    def test_health_and_full_lifecycle(self):
        self.assertEqual(self.call("GET", "/health")[0], 200)
        self._bootstrap()
        status, ticket = self.call("POST", "/tickets/purchase", {
            "match_id": "M1", "section": "A区", "row": "3", "no": "8",
            "customer_id": "alice", "idempotency_key": "buy-1",
        })
        self.assertEqual(status, 201)
        tid = ticket["ticket_id"]

        status, inv = self.call("POST", f"/tickets/{tid}/transfers", {
            "from_customer_id": "alice", "to_customer_id": "bob",
            "idempotency_key": "tr-1",
        })
        self.assertEqual(status, 201)
        status, view = self.call("POST", f"/transfers/{inv['invite_id']}/accept", {
            "customer_id": "bob", "idempotency_key": "acc-1",
        })
        self.assertEqual((status, view["holder_id"]), (200, "bob"))

        status, scan = self.call("POST", "/scans", {
            "ticket_code": tid, "device_id": "gate-1",
            "presenter_id": "bob",
        })
        self.assertEqual((status, scan["result"]), (200, "admitted"))

        status, chain = self.call("GET", f"/tickets/{tid}/chain", role="auditor")
        self.assertEqual(status, 200)
        self.assertEqual([e["action"] for e in chain["events"]],
                         ["ticket.issued", "transfer.invited", "transfer.accepted",
                          "scan.admitted"])

    def test_purchase_retry_same_idempotency_header(self):
        self._bootstrap()
        payload = {"match_id": "M1", "section": "A区", "row": "3", "no": "8",
                   "customer_id": "alice"}
        s1, t1 = self.call("POST", "/tickets/purchase", payload, idem="net-1")
        s2, t2 = self.call("POST", "/tickets/purchase", payload, idem="net-1")
        self.assertEqual((s1, s2), (201, 201))
        self.assertEqual(t1["ticket_id"], t2["ticket_id"])

    def test_review_status_is_locked_and_role_enforced(self):
        self._bootstrap()
        _, ticket = self.call("POST", "/tickets/purchase", {
            "match_id": "M1", "section": "A区", "row": "3", "no": "8",
            "customer_id": "alice", "idempotency_key": "buy-1",
        })
        tid = ticket["ticket_id"]
        # 客服无权冻结
        self.assertEqual(self.call("POST", "/admin/freezes",
                                   {"ticket_ids": [tid], "reason": "x"},
                                   role="support")[0], 403)
        self.assertEqual(self.call("POST", "/admin/freezes", {
            "ticket_ids": [tid], "reason": "订单关联风控调查 #9",
        }, role="admin", actor="admin-1")[0], 201)
        status, scan = self.call("POST", "/scans", {
            "ticket_code": tid, "device_id": "gate-2"})
        self.assertEqual(status, 423)
        self.assertEqual(scan["result"], "review")
        # 普通客户无权处理复核
        self.assertEqual(self.call("POST", f"/scans/{scan['scan_id']}/review", {
            "decision": "allow_entry", "note": "放行",
        }, role="customer")[0], 403)

    def test_support_summary_endpoint_masks(self):
        self._bootstrap()
        status, body = self.call("GET", "/customers/alice/support-summary",
                                 role="support")
        self.assertEqual(status, 200)
        self.assertNotIn("110101199001011234", json.dumps(body, ensure_ascii=False))
        self.assertIn("*", body["id_doc_masked"])

    def test_concurrent_purchase_http_only_one_wins(self):
        """两个账号同时抢同一座位（不同幂等键）：只有一张票。"""
        self._bootstrap()

        def buy(who, key, out):
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/tickets/purchase",
                data=json.dumps({
                    "match_id": "M1", "section": "A区", "row": "3", "no": "9",
                    "customer_id": who, "idempotency_key": key,
                }).encode("utf-8"), method="POST")
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    out.append((resp.status, json.loads(resp.read())))
            except urllib.error.HTTPError as exc:
                out.append((exc.code, json.loads(exc.read())))

        out: list = []
        threads = [
            threading.Thread(target=buy, args=("alice", "k-a", out)),
            threading.Thread(target=buy, args=("bob", "k-b", out)),
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=15)
        statuses = sorted(s for s, _ in out)
        self.assertEqual(statuses, [201, 409])
        # 链校验
        status, verify = self.call("GET", "/admin/verify-chain", role="auditor")
        self.assertTrue(verify["ok"])


if __name__ == "__main__":
    unittest.main()

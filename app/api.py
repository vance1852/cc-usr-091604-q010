"""票券服务的 HTTP JSON 接口。

角色（请求头 X-Role）：
- customer：购票、转让、撤回、退回、查看自己的票；
- support ：仅可查看脱敏身份摘要与票券状态；
- admin   ：场次/座位、风控名单、冻结/解冻、复核处理；
- auditor ：审计查询与持有链校验。

写接口可带 ``Idempotency-Key`` 请求头做网络重试去重。

启动：python -m app.api --db tickets.db --port 8080
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .db import connect, init_db
from .errors import TicketError
from .service import TicketService
from .util import now_utc, to_iso

ROLE_ADMIN = "admin"
ROLE_SUPPORT = "support"
ROLE_AUDITOR = "auditor"


class ApiContext:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        init_db(self.db_path)
        # 串行化所有写请求，配合事务杜绝并发写竞争
        self.write_lock = threading.Lock()


def _match(path: str, pattern: str):
    """极简路径模板：/admin/risk/{customer_id}。"""
    rx = "^" + re.sub(r"{(\w+)}", r"(?P<\1>[^/]+)", pattern) + "$"
    m = re.match(rx, path)
    return m.groupdict() if m else None


class TicketHandler(BaseHTTPRequestHandler):
    server_version = "TicketLifecycle/1.0"

    # ------------------------------------------------------------ 基础收发

    def _json(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise TicketError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise TicketError("请求体必须是 JSON 对象")
        return body

    def _require(self, body: dict, key: str) -> Any:
        if key not in body or body[key] in (None, ""):
            raise TicketError(f"缺少必填字段: {key}")
        return body[key]

    @property
    def role(self) -> str:
        return self.headers.get("X-Role", "customer")

    def require_role(self, *roles: str) -> None:
        if self.role not in roles:
            raise PermissionError(f"需要角色: {'/'.join(roles)}（当前 {self.role}）")

    def do_GET(self):  # noqa: N802
        self._handle("GET")

    def do_DELETE(self):  # noqa: N802
        with self.server.ctx.write_lock:
            self._handle("DELETE")

    def do_POST(self):  # noqa: N802
        # 写接口加进程内串行锁；DB 事务是最终防线（多进程同样安全）
        if self._is_readonly_path():
            self._handle("POST")
        else:
            with self.server.ctx.write_lock:
                self._handle("POST")

    def _is_readonly_path(self) -> bool:
        path = self.path.split("?", 1)[0]
        return path == "/health"

    def _handle(self, method: str) -> None:
        conn = None
        try:
            conn = connect(self.server.ctx.db_path)
            svc = TicketService(conn)
            result, status = self.route(
                method, self.path.split("?", 1)[0],
                self._read_json() if method in ("POST", "DELETE") else {},
                self.headers.get("Idempotency-Key"), svc,
            )
            self._json(status, result)
        except PermissionError as exc:
            self._json(HTTPStatus.FORBIDDEN, {"error": str(exc), "code": "forbidden"})
        except TicketError as exc:
            self._json(exc.http_status, {"error": str(exc), "code": exc.code})
        except Exception as exc:  # 最后防线，不泄露栈给闸机
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                       {"error": f"服务器内部错误: {exc}", "code": "internal"})
        finally:
            if conn is not None:
                conn.close()

    def log_message(self, fmt, *args):  # 安静日志
        pass

    # ------------------------------------------------------------ 路由

    def route(self, method: str, path: str, body: dict, idem: str | None,
              svc: TicketService) -> tuple[dict, int]:
        m: dict | None

        if method == "GET" and path == "/health":
            return {"service": "ticket", "status": "ok",
                    "time_utc": to_iso(now_utc())}, 200

        if method == "POST" and path == "/customers":
            return svc.register_customer(
                self._require(body, "customer_id"),
                self._require(body, "full_name"),
                self._require(body, "id_doc"),
                self._require(body, "birth_date"),
                self._require(body, "phone"),
            ), 201

        m = _match(path, "/customers/{customer_id}/support-summary")
        if method == "GET" and m:
            self.require_role(ROLE_SUPPORT, ROLE_ADMIN)
            return svc.customer_support_summary(m["customer_id"]), 200

        m = _match(path, "/admin/risk/{customer_id}")
        if method == "POST" and m:
            self.require_role(ROLE_ADMIN)
            return svc.add_to_risk_list(
                m["customer_id"], self._require(body, "reason"),
                self.headers.get("X-Actor-Id", "admin"),
            ), 201
        if method == "DELETE" and m:
            self.require_role(ROLE_ADMIN)
            return svc.remove_from_risk_list(
                m["customer_id"], body.get("note", ""),
                self.headers.get("X-Actor-Id", "admin"),
            ), 200

        if method == "POST" and path == "/admin/matches":
            self.require_role(ROLE_ADMIN)
            return svc.create_match(
                self._require(body, "match_id"),
                self._require(body, "name"),
                self._require(body, "venue"),
                self._require(body, "event_time"),
                body.get("transfer_deadline"),
            ), 201

        m = _match(path, "/admin/matches/{match_id}/seats")
        if method == "POST" and m:
            self.require_role(ROLE_ADMIN)
            seats = [(s["section"], s["row"], s["no"]) for s in self._require(body, "seats")]
            return svc.add_seats(m["match_id"], seats), 201

        if method == "POST" and path == "/tickets/purchase":
            return svc.purchase_ticket(
                self._require(body, "match_id"),
                self._require(body, "section"),
                self._require(body, "row"),
                self._require(body, "no"),
                self._require(body, "customer_id"),
                idem or self._require(body, "idempotency_key"),
            ), 201

        m = _match(path, "/tickets/{ticket_id}")
        if method == "GET" and m:
            return svc.get_ticket(m["ticket_id"]), 200

        m = _match(path, "/tickets/{ticket_id}/transfers")
        if method == "POST" and m:
            return svc.create_transfer_invite(
                m["ticket_id"],
                self._require(body, "from_customer_id"),
                self._require(body, "to_customer_id"),
                idem or self._require(body, "idempotency_key"),
            ), 201

        m = _match(path, "/tickets/{ticket_id}/return")
        if method == "POST" and m:
            return svc.return_ticket(
                m["ticket_id"],
                self._require(body, "customer_id"),
                idem or self._require(body, "idempotency_key"),
            ), 200

        m = _match(path, "/transfers/{invite_id}/accept")
        if method == "POST" and m:
            return svc.accept_transfer(
                m["invite_id"],
                self._require(body, "customer_id"),
                idem or body.get("idempotency_key"),
            ), 200

        m = _match(path, "/transfers/{invite_id}/cancel")
        if method == "POST" and m:
            return svc.cancel_transfer(
                m["invite_id"],
                self._require(body, "customer_id"),
                idem or body.get("idempotency_key"),
            ), 200

        if method == "POST" and path == "/scans":
            # 异常票返回 423（已锁定）语义，闸机据此亮灯转人工
            result = svc.scan_ticket(
                self._require(body, "ticket_code"),
                self._require(body, "device_id"),
                idem or body.get("idempotency_key"),
                body.get("presenter_id"),
            )
            return result, 200 if result["result"] != "review" else HTTPStatus.LOCKED

        m = _match(path, "/scans/{scan_id}/review")
        if method == "POST" and m:
            self.require_role(ROLE_ADMIN)
            return svc.resolve_review(
                m["scan_id"],
                self.headers.get("X-Actor-Id", "admin"),
                self._require(body, "decision"),
                self._require(body, "note"),
            ), 200

        if method == "POST" and path == "/admin/freezes":
            self.require_role(ROLE_ADMIN)
            return svc.freeze_batch(
                list(self._require(body, "ticket_ids")),
                self.headers.get("X-Actor-Id", "admin"),
                self._require(body, "reason"),
                idem or body.get("idempotency_key"),
            ), 201

        m = _match(path, "/admin/tickets/{ticket_id}/unfreeze")
        if method == "POST" and m:
            self.require_role(ROLE_ADMIN)
            return svc.unfreeze_ticket(
                m["ticket_id"],
                self.headers.get("X-Actor-Id", "admin"),
                self._require(body, "note"),
            ), 200

        if method == "POST" and path == "/admin/maintenance/expire-transfers":
            self.require_role(ROLE_ADMIN)
            return svc.expire_due_invitations(), 200

        # ---- 审计与查询（客服可查票，审计链需 auditor/admin） ----
        m = _match(path, "/tickets/{ticket_id}/chain")
        if method == "GET" and m:
            self.require_role(ROLE_AUDITOR, ROLE_ADMIN, ROLE_SUPPORT)
            return svc.ticket_chain(m["ticket_id"]), 200

        m = _match(path, "/matches/{match_id}/seats/{section}/{row}/{no}/history")
        if method == "GET" and m:
            self.require_role(ROLE_AUDITOR, ROLE_ADMIN)
            return svc.seat_history(
                m["match_id"], m["section"], m["row"], m["no"]
            ), 200

        if method == "GET" and path == "/events":
            self.require_role(ROLE_AUDITOR, ROLE_ADMIN)
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            return {"events": svc.list_events(
                ticket_id=q.get("ticket_id", [None])[0],
                seat_id=q.get("seat_id", [None])[0],
                match_id=q.get("match_id", [None])[0],
                customer_id=q.get("customer_id", [None])[0],
                limit=int(q.get("limit", ["200"])[0]),
            )}, 200

        if method == "GET" and path == "/admin/verify-chain":
            self.require_role(ROLE_AUDITOR, ROLE_ADMIN)
            return svc.verify_chain(), 200

        raise NotFoundRoute(path)


class NotFoundRoute(TicketError):
    http_status = 404
    code = "not_found"

    def __init__(self, path: str):
        super().__init__(f"接口不存在: {path}")


def build_server(db_path: str | Path, port: int = 8080) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), TicketHandler)
    server.ctx = ApiContext(db_path)  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="篮球票券生命周期服务")
    parser.add_argument("--db", default="tickets.db")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    server = build_server(args.db, args.port)
    print(f"票券服务已启动: http://0.0.0.0:{args.port}  数据库: {args.db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()

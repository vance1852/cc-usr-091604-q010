"""票券生命周期核心服务。

并发模型：每个写操作以 ``BEGIN IMMEDIATE`` 取数据库级保留锁，配合
- ``one_live_ticket_per_seat`` 部分唯一索引（同座位仅一张生效票）；
- ``one_pending_invite_per_ticket`` 部分唯一索引（一票仅一笔待确认邀请）；
- tickets 状态机的条件 UPDATE；
保证重复点击、网络重试、多线程并发都不会产生两张可用票。

审计：所有状态变化写入只追加的 events 表并以 sha256 串联，
按座位或票均可还原完整状态变化时间线。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from .db import connect
from .errors import Conflict, NotFound, RuleViolation, Tampered, TicketError
from .util import (
    age_on,
    chain_hash,
    hash_doc,
    mask_doc,
    mask_phone,
    now_utc,
    parse_birth,
    parse_iso,
    sha256_text,
    to_iso,
)

GENESIS_HASH = "0" * 64
MIN_PURCHASE_AGE = 14
DEFAULT_TRANSFER_CUTOFF_HOURS = 2  # 开赛前 2 小时停止转让

ERROR_BY_CODE = {
    cls.code: cls
    for cls in (NotFound, Conflict, RuleViolation, Tampered, TicketError)
}


def _new_id() -> str:
    return uuid.uuid4().hex


def mask_name(name: str) -> str:
    """姓名脱敏：保留姓氏（首字），其余 *。"""
    if not name:
        return ""
    return name[0] + "*" * (len(name) - 1) if len(name) > 1 else name


class TicketService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Callable[[], Any] = now_utc,
    ):
        self.conn = conn
        self.clock = clock

    # ---------------------------------------------------------------- 内部

    def _ts(self) -> str:
        return to_iso(self.clock())

    def _write(self, op: str, idem_key: str | None, work: Callable[[], tuple[dict, str]]):
        """在一个立即事务里执行 work；idem_key 命中时重放首次结果。

        work 返回 (响应字典, 关联资源 id)。业务错误也随幂等键落库，
        重试得到完全一致的错误而不会重复执行。
        """
        conn = self.conn
        stored_key = f"{op}:{idem_key}" if idem_key else None
        conn.execute("BEGIN IMMEDIATE")
        replay_row: tuple[str, str] | None = None
        try:
            if stored_key:
                row = conn.execute(
                    "SELECT outcome, response FROM idempotency WHERE idempotency_key=?",
                    (stored_key,),
                ).fetchone()
                if row is not None:
                    replay_row = (row["outcome"], row["response"])
                    conn.execute("COMMIT")
                else:
                    result, ref_id = work()
                    conn.execute(
                        "INSERT INTO idempotency "
                        "(idempotency_key, op, ref_id, outcome, response, created_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (stored_key, op, ref_id, "ok",
                         json.dumps(result, ensure_ascii=False), self._ts()),
                    )
                    conn.execute("COMMIT")
            else:
                result, ref_id = work()
                conn.execute("COMMIT")
        except TicketError as exc:
            # 业务错误：回滚全部业务写入，幂等失败记录单独落库
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if stored_key:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        "INSERT INTO idempotency "
                        "(idempotency_key, op, ref_id, outcome, response, created_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (
                            stored_key,
                            op,
                            None,
                            exc.code,
                            json.dumps({"error": str(exc)}, ensure_ascii=False),
                            self._ts(),
                        ),
                    )
                    conn.execute("COMMIT")
                except sqlite3.IntegrityError:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
            raise
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        if replay_row is not None:
            return self._replay(*replay_row)
        return result

    @staticmethod
    def _replay(outcome: str, response: str):
        payload = json.loads(response)
        if outcome == "ok":
            return payload
        cls = ERROR_BY_CODE.get(outcome, TicketError)
        raise cls(payload.get("error", "重放失败"))

    def _event(
        self,
        action: str,
        *,
        ticket_id: str | None = None,
        seat_id: str | None = None,
        match_id: str | None = None,
        subject_customer_id: str | None = None,
        actor_id: str,
        payload: dict | None = None,
    ) -> str:
        """追加事件并延伸哈希链（必须处于写事务中）。"""
        payload = payload or {}
        conn = self.conn
        last = conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        seq = (last["seq"] + 1) if last else 1
        prev_hash = last["hash"] if last else GENESIS_HASH
        created_at = self._ts()
        h = chain_hash(
            prev_hash,
            seq,
            ticket_id,
            seat_id,
            match_id,
            subject_customer_id,
            actor_id,
            action,
            payload,
            created_at,
        )
        event_id = _new_id()
        conn.execute(
            "INSERT INTO events "
            "(id, seq, ticket_id, seat_id, match_id, subject_customer_id, "
            " actor_id, action, payload, created_at, prev_hash, hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                seq,
                ticket_id,
                seat_id,
                match_id,
                subject_customer_id,
                actor_id,
                action,
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                created_at,
                prev_hash,
                h,
            ),
        )
        return event_id

    def _get_or_404(self, table: str, obj_id: str, label: str) -> sqlite3.Row:
        row = self.conn.execute(f"SELECT * FROM {table} WHERE id=?", (obj_id,)).fetchone()
        if row is None:
            raise NotFound(f"{label}不存在: {obj_id}")
        return row

    def _seat_row(self, match_id: str, section: str, row: str, no: str) -> sqlite3.Row:
        seat = self.conn.execute(
            "SELECT * FROM seats WHERE match_id=? AND section=? AND seat_row=? AND seat_no=?",
            (match_id, section, row, no),
        ).fetchone()
        if seat is None:
            raise NotFound(f"座位不存在: {section}-{row}-{no}")
        return seat

    def _active_risk(self, customer_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM risk_entries WHERE customer_id=? AND released_at IS NULL",
            (customer_id,),
        ).fetchone()

    def _ticket_view(self, t: sqlite3.Row) -> dict:
        seat = self.conn.execute("SELECT * FROM seats WHERE id=?", (t["seat_id"],)).fetchone()
        return {
            "ticket_id": t["id"],
            "match_id": t["match_id"],
            "seat": {
                "section": seat["section"],
                "row": seat["seat_row"],
                "no": seat["seat_no"],
            },
            "holder_id": t["current_holder_id"],
            "status": t["status"],
            "frozen": bool(t["is_frozen"]),
            "created_at": t["created_at"],
            "updated_at": t["updated_at"],
        }

    # ------------------------------------------------------------ 客户与实名

    def register_customer(
        self,
        customer_id: str,
        full_name: str,
        id_doc: str,
        birth_date: str,
        phone: str,
    ) -> dict:
        """实名注册：证件号只存哈希与脱敏串，不存明文。"""
        parse_birth(birth_date)  # 校验格式

        def work():
            exists = self.conn.execute(
                "SELECT 1 FROM customers WHERE id=?", (customer_id,)
            ).fetchone()
            if exists:
                raise Conflict(f"客户已存在: {customer_id}")
            doc_hash = hash_doc(id_doc)
            dup = self.conn.execute(
                "SELECT 1 FROM customers WHERE id_doc_hash=?", (doc_hash,)
            ).fetchone()
            if dup:
                raise Conflict("该证件已注册，实名信息不可重复购票账号")
            ts = self._ts()
            self.conn.execute(
                "INSERT INTO customers "
                "(id, full_name, id_doc_hash, id_doc_masked, birth_date, phone_masked, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (customer_id, full_name, doc_hash, mask_doc(id_doc), birth_date, mask_phone(phone), ts),
            )
            self._event(
                "customer.registered",
                subject_customer_id=customer_id,
                actor_id=customer_id,
                payload={"id_doc_masked": mask_doc(id_doc)},
            )
            return {"customer_id": customer_id, "registered_at": ts}, customer_id

        return self._write("customer.register", None, work)

    def add_to_risk_list(self, customer_id: str, reason: str, admin_id: str) -> dict:
        if not reason or not reason.strip():
            raise RuleViolation("加入风控名单必须填写依据")

        def work():
            self._get_or_404("customers", customer_id, "客户")
            if self._active_risk(customer_id) is not None:
                raise Conflict("客户已在风控名单中")
            rid, ts = _new_id(), self._ts()
            self.conn.execute(
                "INSERT INTO risk_entries (id, customer_id, reason, created_by, created_at) "
                "VALUES (?,?,?,?,?)",
                (rid, customer_id, reason.strip(), admin_id, ts),
            )
            self._event(
                "risk.added",
                subject_customer_id=customer_id,
                actor_id=admin_id,
                payload={"risk_id": rid, "reason": reason.strip()},
            )
            return {"risk_id": rid, "customer_id": customer_id, "since": ts}, rid

        return self._write("risk.add", None, work)

    def remove_from_risk_list(self, customer_id: str, note: str, admin_id: str) -> dict:
        def work():
            entry = self._active_risk(customer_id)
            if entry is None:
                raise NotFound("客户不在风控名单中")
            ts = self._ts()
            self.conn.execute(
                "UPDATE risk_entries SET released_at=?, release_reason=? WHERE id=?",
                (ts, note, entry["id"]),
            )
            self._event(
                "risk.released",
                subject_customer_id=customer_id,
                actor_id=admin_id,
                payload={"risk_id": entry["id"], "note": note},
            )
            return {"customer_id": customer_id, "released_at": ts}, entry["id"]

        return self._write("risk.release", None, work)

    def customer_support_summary(self, customer_id: str) -> dict:
        """客服视角：仅必要身份摘要，全部脱敏，不含证件号/手机号明文。"""
        c = self._get_or_404("customers", customer_id, "客户")
        risk = self._active_risk(customer_id)
        tickets = self.conn.execute(
            "SELECT id, match_id, status, is_frozen FROM tickets WHERE current_holder_id=? "
            "ORDER BY created_at",
            (customer_id,),
        ).fetchall()
        return {
            "customer_id": customer_id,
            "name_masked": mask_name(c["full_name"]),
            "id_doc_masked": c["id_doc_masked"],
            "phone_masked": c["phone_masked"],
            "birth_year": c["birth_date"][:4],  # 仅给年份，完整生日非客服必要信息
            "on_risk_list": risk is not None,
            "risk_reason": risk["reason"] if risk else None,
            "tickets": [
                {
                    "ticket_id": t["id"],
                    "match_id": t["match_id"],
                    "status": t["status"],
                    "frozen": bool(t["is_frozen"]),
                }
                for t in tickets
            ],
        }

    # ------------------------------------------------------------ 场次与座位

    def create_match(
        self,
        match_id: str,
        name: str,
        venue: str,
        event_time: str,
        transfer_deadline: str | None = None,
    ) -> dict:
        tipoff = parse_iso(event_time)
        if transfer_deadline is None:
            deadline = tipoff - timedelta(hours=DEFAULT_TRANSFER_CUTOFF_HOURS)
        else:
            deadline = parse_iso(transfer_deadline)
            if deadline >= tipoff:
                raise RuleViolation("转让截止时间必须早于开赛时间")

        def work():
            if self.conn.execute("SELECT 1 FROM matches WHERE id=?", (match_id,)).fetchone():
                raise Conflict(f"场次已存在: {match_id}")
            ts = self._ts()
            self.conn.execute(
                "INSERT INTO matches (id, name, venue, event_time_utc, transfer_deadline_utc, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (match_id, name, venue, to_iso(tipoff), to_iso(deadline), ts),
            )
            self._event(
                "match.created",
                match_id=match_id,
                actor_id="system",
                payload={
                    "name": name,
                    "venue": venue,
                    "event_time_utc": to_iso(tipoff),
                    "transfer_deadline_utc": to_iso(deadline),
                },
            )
            return {
                "match_id": match_id,
                "event_time_utc": to_iso(tipoff),
                "transfer_deadline_utc": to_iso(deadline),
            }, match_id

        return self._write("match.create", None, work)

    def add_seats(self, match_id: str, seats: Iterable[tuple[str, str, str]]) -> dict:
        """批量建座：参数为 (看台, 排, 座) 三元组。"""
        seats = list(seats)

        def work():
            self._get_or_404("matches", match_id, "场次")
            created = []
            for section, seat_row, no in seats:
                sid = _new_id()
                try:
                    self.conn.execute(
                        "INSERT INTO seats (id, match_id, section, seat_row, seat_no) VALUES (?,?,?,?,?)",
                        (sid, match_id, section, seat_row, no),
                    )
                except sqlite3.IntegrityError as exc:
                    raise Conflict(f"座位重复: {section}-{seat_row}-{no}") from exc
                created.append(sid)
                self._event(
                    "seat.added",
                    seat_id=sid,
                    match_id=match_id,
                    actor_id="system",
                    payload={"section": section, "row": seat_row, "no": no},
                )
            return {"match_id": match_id, "seats_added": len(created)}, match_id

        return self._write("seat.add", None, work)

    # ---------------------------------------------------------------- 购票

    def purchase_ticket(
        self,
        match_id: str,
        section: str,
        row: str,
        no: str,
        customer_id: str,
        idempotency_key: str,
    ) -> dict:
        """实名购票：校验年龄（>=14）、风控名单与座位唯一性。"""
        if not idempotency_key:
            raise RuleViolation("购票必须携带幂等键")

        def work():
            match = self._get_or_404("matches", match_id, "场次")
            customer = self._get_or_404("customers", customer_id, "客户")
            seat = self._seat_row(match_id, section, row, no)
            if self._active_risk(customer_id) is not None:
                raise RuleViolation("客户在风控名单中，禁止购票")
            tipoff_day = parse_iso(match["event_time_utc"]).date()
            age = age_on(parse_birth(customer["birth_date"]), tipoff_day)
            if age < MIN_PURCHASE_AGE:
                raise RuleViolation(
                    f"比赛当日购票人须年满 {MIN_PURCHASE_AGE} 周岁（当前 {age} 岁），未成年人请由监护人购票"
                )
            live = self.conn.execute(
                "SELECT id FROM tickets WHERE seat_id=? "
                "AND status IN ('issued','transfer_pending')",
                (seat["id"],),
            ).fetchone()
            if live is not None:
                raise Conflict("该座位已有生效票，不可重复售出")
            tid, ts = _new_id(), self._ts()
            self.conn.execute(
                "INSERT INTO tickets "
                "(id, match_id, seat_id, current_holder_id, status, is_frozen, idempotency_key, created_at, updated_at) "
                "VALUES (?,?,?,?,'issued',0,?,?,?)",
                (tid, match_id, seat["id"], customer_id, f"purchase:{idempotency_key}", ts, ts),
            )
            self._event(
                "ticket.issued",
                ticket_id=tid,
                seat_id=seat["id"],
                match_id=match_id,
                subject_customer_id=customer_id,
                actor_id=customer_id,
                payload={"seat": {"section": section, "row": row, "no": no}},
            )
            t = self.conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
            return self._ticket_view(t), tid

        return self._write("purchase", idempotency_key, work)

    def get_ticket(self, ticket_id: str) -> dict:
        t = self._get_or_404("tickets", ticket_id, "票券")
        return self._ticket_view(t)

    # ---------------------------------------------------------------- 转让

    def _guard_transferable(self, t: sqlite3.Row) -> None:
        """持票方侧的统一校验：冻结、风控、状态、截止时间。"""
        if t["is_frozen"]:
            raise RuleViolation("票券已被冻结，禁止转让")
        if self._active_risk(t["current_holder_id"]) is not None:
            raise RuleViolation("当前持有人在风控名单中，禁止转让")
        if t["status"] != "issued":
            raise Conflict(f"票券当前状态为 {t['status']}，不能发起转让")
        match = self.conn.execute(
            "SELECT transfer_deadline_utc FROM matches WHERE id=?", (t["match_id"],)
        ).fetchone()
        if self.clock() >= parse_iso(match["transfer_deadline_utc"]):
            raise RuleViolation("已过转让截止时间（开赛前 2 小时），不能再转让")

    def create_transfer_invite(
        self,
        ticket_id: str,
        from_customer_id: str,
        to_customer_id: str,
        idempotency_key: str,
    ) -> dict:
        if not idempotency_key:
            raise RuleViolation("转让必须携带幂等键")
        if from_customer_id == to_customer_id:
            raise RuleViolation("不能转让给自己")

        def work():
            t = self._get_or_404("tickets", ticket_id, "票券")
            if t["current_holder_id"] != from_customer_id:
                raise RuleViolation("只有当前持有人可以发起转让")
            self._get_or_404("customers", to_customer_id, "接收方")
            self._guard_transferable(t)
            if self._active_risk(to_customer_id) is not None:
                raise RuleViolation("接收方在风控名单中，无法受让")
            deadline = self.conn.execute(
                "SELECT transfer_deadline_utc FROM matches WHERE id=?", (t["match_id"],)
            ).fetchone()["transfer_deadline_utc"]
            iid, ts = _new_id(), self._ts()
            try:
                self.conn.execute(
                    "INSERT INTO transfer_invitations "
                    "(id, ticket_id, from_holder_id, to_customer_id, status, idempotency_key, "
                    " created_at, expires_at) VALUES (?,?,?,?, 'pending', ?,?,?)",
                    (iid, ticket_id, from_customer_id, to_customer_id,
                     f"transfer:{idempotency_key}", ts, deadline),
                )
                self.conn.execute(
                    "UPDATE tickets SET status='transfer_pending', updated_at=? WHERE id=? AND status='issued'",
                    (ts, ticket_id),
                )
            except sqlite3.IntegrityError as exc:
                # 一票仅一笔待确认邀请：并发第二次发起在此被挡下
                raise Conflict("该票已有待接收方确认的转让邀请，不能同时转给多人") from exc
            self._event(
                "transfer.invited",
                ticket_id=ticket_id,
                seat_id=t["seat_id"],
                match_id=t["match_id"],
                subject_customer_id=to_customer_id,
                actor_id=from_customer_id,
                payload={"invite_id": iid, "from": from_customer_id, "to": to_customer_id,
                         "expires_at": deadline},
            )
            return {
                "invite_id": iid,
                "ticket_id": ticket_id,
                "from": from_customer_id,
                "to": to_customer_id,
                "status": "pending",
                "expires_at": deadline,
                "created_at": ts,
            }, iid

        return self._write("transfer.invite", idempotency_key, work)

    def accept_transfer(
        self,
        invite_id: str,
        customer_id: str,
        idempotency_key: str,
    ) -> dict:
        """接收方确认：确认成功的同一事务内原持有人立即失效。"""

        def work():
            inv = self.conn.execute(
                "SELECT * FROM transfer_invitations WHERE id=?", (invite_id,)
            ).fetchone()
            if inv is None:
                raise NotFound(f"转让邀请不存在: {invite_id}")
            if inv["status"] != "pending":
                # 重复点击 / 网络重试（无幂等键时）在此收敛为冲突
                raise Conflict(f"邀请已处理，当前状态: {inv['status']}")
            if customer_id != inv["to_customer_id"]:
                raise RuleViolation("只有指定接收方可以确认接收")
            t = self.conn.execute("SELECT * FROM tickets WHERE id=?", (inv["ticket_id"],)).fetchone()
            if t["is_frozen"]:
                raise RuleViolation("票券已被冻结，本次转让不能完成")
            if self._active_risk(inv["from_holder_id"]) is not None:
                raise RuleViolation("出让方已被列入风控名单，本次转让不能完成")
            if self._active_risk(customer_id) is not None:
                raise RuleViolation("接收方在风控名单中，无法受让")
            if self.clock() >= parse_iso(inv["expires_at"]):
                raise RuleViolation("已过转让截止时间，邀请失效")
            if t["current_holder_id"] != inv["from_holder_id"]:
                raise Conflict("票券持有人已变化，邀请失效")
            ts = self._ts()
            cur = self.conn.execute(
                "UPDATE tickets SET current_holder_id=?, status='issued', updated_at=? "
                "WHERE id=? AND status='transfer_pending' AND current_holder_id=?",
                (customer_id, ts, inv["ticket_id"], inv["from_holder_id"]),
            )
            if cur.rowcount != 1:
                raise Conflict("转让状态已变化，请刷新后重试")
            cur = self.conn.execute(
                "UPDATE transfer_invitations SET status='accepted', responded_at=? "
                "WHERE id=? AND status='pending'",
                (ts, invite_id),
            )
            if cur.rowcount != 1:
                raise Conflict("邀请已被并发处理")
            self._event(
                "transfer.accepted",
                ticket_id=inv["ticket_id"],
                seat_id=t["seat_id"],
                match_id=t["match_id"],
                subject_customer_id=customer_id,
                actor_id=customer_id,
                payload={
                    "invite_id": invite_id,
                    "from": inv["from_holder_id"],
                    "to": customer_id,
                },
            )
            fresh = self.conn.execute("SELECT * FROM tickets WHERE id=?", (inv["ticket_id"],)).fetchone()
            return self._ticket_view(fresh), inv["ticket_id"]

        return self._write("transfer.accept", idempotency_key, work)

    def cancel_transfer(
        self,
        invite_id: str,
        customer_id: str,
        idempotency_key: str | None = None,
    ) -> dict:
        """撤回转让：出让人撤回（或接收方拒绝）。确认完成后不可撤回。"""

        def work():
            inv = self.conn.execute(
                "SELECT * FROM transfer_invitations WHERE id=?", (invite_id,)
            ).fetchone()
            if inv is None:
                raise NotFound(f"转让邀请不存在: {invite_id}")
            if customer_id not in (inv["from_holder_id"], inv["to_customer_id"]):
                raise RuleViolation("只有转让双方可以撤回/拒绝")
            t = self.conn.execute(
                "SELECT is_frozen FROM tickets WHERE id=?", (inv["ticket_id"],)
            ).fetchone()
            if t and t["is_frozen"]:
                raise RuleViolation("票券已被冻结，撤回/拒绝需等待管理员解冻")
            if inv["status"] == "cancelled":
                raise Conflict("邀请已撤回")
            if inv["status"] == "accepted":
                raise Conflict("邀请已确认，无法撤回")
            if inv["status"] == "expired":
                raise Conflict("邀请已过截止时间失效")
            ts = self._ts()
            self.conn.execute(
                "UPDATE transfer_invitations SET status='cancelled', responded_at=? WHERE id=?",
                (ts, invite_id),
            )
            # 仅当票仍挂在该邀请上时恢复，避免覆盖并发状态
            self.conn.execute(
                "UPDATE tickets SET status='issued', updated_at=? "
                "WHERE id=? AND status='transfer_pending' AND current_holder_id=?",
                (ts, inv["ticket_id"], inv["from_holder_id"]),
            )
            self._event(
                "transfer.cancelled",
                ticket_id=inv["ticket_id"],
                subject_customer_id=inv["to_customer_id"],
                actor_id=customer_id,
                payload={"invite_id": invite_id, "cancelled_by": customer_id},
            )
            return {"invite_id": invite_id, "status": "cancelled", "at": ts}, invite_id

        return self._write("transfer.cancel", idempotency_key, work)

    def expire_due_invitations(self) -> dict:
        """把已过截止时间仍待确认的邀请置为失效并恢复票券状态。"""

        def work():
            now = self._ts()
            due = self.conn.execute(
                "SELECT * FROM transfer_invitations WHERE status='pending' AND expires_at<=?",
                (now,),
            ).fetchall()
            expired = []
            for inv in due:
                self.conn.execute(
                    "UPDATE transfer_invitations SET status='expired', responded_at=? WHERE id=?",
                    (now, inv["id"]),
                )
                self.conn.execute(
                    "UPDATE tickets SET status='issued', updated_at=? "
                    "WHERE id=? AND status='transfer_pending' AND current_holder_id=?",
                    (now, inv["ticket_id"], inv["from_holder_id"]),
                )
                t = self.conn.execute("SELECT * FROM tickets WHERE id=?", (inv["ticket_id"],)).fetchone()
                self._event(
                    "transfer.expired",
                    ticket_id=inv["ticket_id"],
                    seat_id=t["seat_id"],
                    match_id=t["match_id"],
                    subject_customer_id=inv["to_customer_id"],
                    actor_id="system",
                    payload={"invite_id": inv["id"]},
                )
                expired.append(inv["id"])
            return {"expired": expired, "count": len(expired)}, None

        return self._write("transfer.expire", None, work)

    # ---------------------------------------------------------------- 退回

    def return_ticket(self, ticket_id: str, customer_id: str, idempotency_key: str) -> dict:
        """实名退回：截止时间前、本人、未冻结、无在途转让。退回后座位可重新售出。"""

        def work():
            t = self._get_or_404("tickets", ticket_id, "票券")
            if t["current_holder_id"] != customer_id:
                raise RuleViolation("只有当前持有人可以退回")
            if t["is_frozen"]:
                raise RuleViolation("票券已被冻结，不能退回")
            if t["status"] == "returned":
                raise Conflict("票券已退回")
            if t["status"] == "consumed":
                raise Conflict("票券已入场使用，不能退回")
            if t["status"] == "transfer_pending":
                raise Conflict("存在在途转让，请先撤回再退回")
            deadline = self.conn.execute(
                "SELECT transfer_deadline_utc FROM matches WHERE id=?", (t["match_id"],)
            ).fetchone()["transfer_deadline_utc"]
            if self.clock() >= parse_iso(deadline):
                raise RuleViolation("已过退票截止时间（与转让截止一致：开赛前 2 小时）")
            ts = self._ts()
            self.conn.execute(
                "UPDATE tickets SET status='returned', updated_at=? WHERE id=?",
                (ts, ticket_id),
            )
            self._event(
                "ticket.returned",
                ticket_id=ticket_id,
                seat_id=t["seat_id"],
                match_id=t["match_id"],
                subject_customer_id=customer_id,
                actor_id=customer_id,
                payload={},
            )
            return {"ticket_id": ticket_id, "status": "returned", "at": ts}, ticket_id

        return self._write("ticket.return", idempotency_key, work)

    # ---------------------------------------------------------------- 扫码

    def scan_ticket(
        self,
        ticket_code: str,
        device_id: str,
        idempotency_key: str | None = None,
        presenter_id: str | None = None,
    ) -> dict:
        """闸机扫码。

        返回 result：
        - admitted：一次性消费成功（同票仅一次）；
        - rejected：未知码/已入场/已退回，闸机不放行；
        - review：冻结/风控/在途转让/持票人与到场人不符等异常票，
                  只能转人工复核，闸机一律不放行。
        每次扫码都记录设备、时间、结果与是否完成消费。
        """

        def work():
            ts = self._ts()
            t = self.conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_code,)).fetchone()
            scan_id = _new_id()

            def record(result: str, reason: str, consumed: bool) -> dict:
                self.conn.execute(
                    "INSERT INTO scans "
                    "(id, ticket_id, ticket_code, device_id, presenter_id, result, reason, consumed, scanned_at, idempotency_key) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (scan_id, t["id"] if t else None, ticket_code, device_id, presenter_id,
                     result, reason, int(consumed), ts,
                     f"scan:{idempotency_key}" if idempotency_key else None),
                )
                self._event(
                    f"scan.{result}",
                    ticket_id=t["id"] if t else None,
                    seat_id=t["seat_id"] if t else None,
                    match_id=t["match_id"] if t else None,
                    subject_customer_id=t["current_holder_id"] if t else None,
                    actor_id=f"device:{device_id}",
                    payload={"scan_id": scan_id, "reason": reason,
                             "presenter_id": presenter_id},
                )
                return {
                    "scan_id": scan_id,
                    "ticket_code": ticket_code,
                    "device_id": device_id,
                    "result": result,
                    "reason": reason,
                    "consumed": consumed,
                    "scanned_at": ts,
                }

            if t is None:
                return record("rejected", "unknown_ticket", False), scan_id

            if t["status"] == "consumed":
                return record("rejected", "already_admitted", False), scan_id
            if t["status"] == "returned":
                return record("rejected", "ticket_returned", False), scan_id

            # 异常票：一律不直接放行，进人工复核
            if t["is_frozen"]:
                return record("review", "ticket_frozen", False), scan_id
            if self._active_risk(t["current_holder_id"]) is not None:
                return record("review", "holder_on_risk_list", False), scan_id
            if t["status"] == "transfer_pending":
                return record("review", "transfer_in_progress", False), scan_id
            if presenter_id and presenter_id != t["current_holder_id"]:
                return record("review", "presenter_not_holder", False), scan_id
            if t["status"] != "issued":
                return record("review", f"unexpected_status:{t['status']}", False), scan_id

            # 正常票：条件 UPDATE 保证一次性消费，并发双扫仅一个成功
            cur = self.conn.execute(
                "UPDATE tickets SET status='consumed', updated_at=? "
                "WHERE id=? AND status='issued' AND is_frozen=0",
                (ts, t["id"]),
            )
            if cur.rowcount != 1:
                return record("review", "consume_race", False), scan_id
            return record("admitted", "ok", True), scan_id

        return self._write("scan", idempotency_key, work)

    def resolve_review(
        self,
        scan_id: str,
        reviewer: str,
        decision: str,
        note: str,
    ) -> dict:
        """人工复核结论：allow_entry 才补消费；deny_entry 不放行。"""
        if decision not in ("allow_entry", "deny_entry"):
            raise RuleViolation("decision 必须是 allow_entry / deny_entry")
        if not note or not note.strip():
            raise RuleViolation("复核必须填写处理说明")

        def work():
            scan = self.conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
            if scan is None:
                raise NotFound(f"扫码记录不存在: {scan_id}")
            if scan["result"] != "review":
                raise Conflict("仅 review 状态的扫码记录需要复核")
            if scan["review_id"] is not None:
                raise Conflict("该扫码已完成复核")
            rid, ts = _new_id(), self._ts()
            self.conn.execute(
                "INSERT INTO reviews (id, scan_id, reviewer, decision, note, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (rid, scan_id, reviewer, decision, note.strip(), ts),
            )
            self.conn.execute("UPDATE scans SET review_id=? WHERE id=?", (rid, scan_id))
            allowed = decision == "allow_entry"
            if allowed:
                # 放行前再次确认票仍未消费、未冻结
                cur = self.conn.execute(
                    "UPDATE tickets SET status='consumed', updated_at=? "
                    "WHERE id=? AND status='issued' AND is_frozen=0",
                    (ts, scan["ticket_id"]),
                )
                if cur.rowcount != 1:
                    raise Conflict("票券状态已变化，不能放行，请重新发起复核")
            self._event(
                "review.allowed" if allowed else "review.denied",
                ticket_id=scan["ticket_id"],
                actor_id=reviewer,
                payload={"scan_id": scan_id, "decision": decision, "note": note.strip()},
            )
            return {
                "review_id": rid,
                "scan_id": scan_id,
                "decision": decision,
                "reviewer": reviewer,
                "at": ts,
            }, rid

        return self._write("review.resolve", None, work)

    # ---------------------------------------------------------------- 冻结

    def freeze_batch(
        self,
        ticket_ids: list[str],
        admin_id: str,
        reason: str,
        idempotency_key: str | None = None,
    ) -> dict:
        """整批冻结：同一事务、同一依据、同一批次号；任一票不存在整批回滚。"""
        if not ticket_ids:
            raise RuleViolation("冻结票券列表不能为空")
        if not reason or not reason.strip():
            raise RuleViolation("冻结必须说明依据")
        reason = reason.strip()
        batch_id = _new_id()

        def work():
            frozen, skipped = [], []
            for tid in ticket_ids:
                t = self.conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
                if t is None:
                    raise NotFound(f"票券不存在，整批冻结中止: {tid}")
                if t["is_frozen"]:
                    skipped.append(tid)
                    continue
                fid, ts = _new_id(), self._ts()
                self.conn.execute(
                    "INSERT INTO freezes (id, batch_id, ticket_id, admin_id, reason, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (fid, batch_id, tid, admin_id, reason, ts),
                )
                self.conn.execute(
                    "UPDATE tickets SET is_frozen=1, updated_at=? WHERE id=?", (ts, tid)
                )
                self._event(
                    "ticket.frozen",
                    ticket_id=tid,
                    seat_id=t["seat_id"],
                    match_id=t["match_id"],
                    subject_customer_id=t["current_holder_id"],
                    actor_id=admin_id,
                    payload={"batch_id": batch_id, "reason": reason},
                )
                frozen.append(tid)
            return {
                "batch_id": batch_id,
                "reason": reason,
                "frozen": frozen,
                "already_frozen": skipped,
            }, batch_id

        return self._write("freeze.batch", idempotency_key, work)

    def unfreeze_ticket(self, ticket_id: str, admin_id: str, note: str) -> dict:
        if not note or not note.strip():
            raise RuleViolation("解冻必须填写说明")

        def work():
            t = self._get_or_404("tickets", ticket_id, "票券")
            if not t["is_frozen"]:
                raise Conflict("票券未处于冻结状态")
            ts = self._ts()
            self.conn.execute(
                "UPDATE freezes SET released_at=?, release_note=? "
                "WHERE ticket_id=? AND released_at IS NULL",
                (ts, note.strip(), ticket_id),
            )
            self.conn.execute(
                "UPDATE tickets SET is_frozen=0, updated_at=? WHERE id=?", (ts, ticket_id)
            )
            self._event(
                "ticket.unfrozen",
                ticket_id=ticket_id,
                seat_id=t["seat_id"],
                match_id=t["match_id"],
                subject_customer_id=t["current_holder_id"],
                actor_id=admin_id,
                payload={"note": note.strip()},
            )
            return {"ticket_id": ticket_id, "frozen": False, "at": ts}, ticket_id

        return self._write("freeze.release", None, work)

    # ---------------------------------------------------------------- 审计

    def list_events(
        self,
        *,
        ticket_id: str | None = None,
        seat_id: str | None = None,
        match_id: str | None = None,
        customer_id: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        sql, params = "SELECT * FROM events WHERE 1=1", []
        if ticket_id:
            sql += " AND ticket_id=?"; params.append(ticket_id)
        if seat_id:
            sql += " AND seat_id=?"; params.append(seat_id)
        if match_id:
            sql += " AND match_id=?"; params.append(match_id)
        if customer_id:
            sql += " AND subject_customer_id=?"; params.append(customer_id)
        sql += " ORDER BY seq ASC LIMIT ?"; params.append(limit)
        return [
            {
                "seq": r["seq"],
                "at": r["created_at"],
                "ticket_id": r["ticket_id"],
                "seat_id": r["seat_id"],
                "match_id": r["match_id"],
                "subject_customer_id": r["subject_customer_id"],
                "actor_id": r["actor_id"],
                "action": r["action"],
                "payload": json.loads(r["payload"]),
                "hash": r["hash"],
            }
            for r in self.conn.execute(sql, params).fetchall()
        ]

    def ticket_chain(self, ticket_id: str) -> dict:
        self._get_or_404("tickets", ticket_id, "票券")
        return {"ticket_id": ticket_id, "events": self.list_events(ticket_id=ticket_id)}

    def seat_history(self, match_id: str, section: str, row: str, no: str) -> dict:
        """按座位还原每次票券状态变化（含退回后重新售出的跨票券世代）。

        建座（seat.added）不属于票券状态变化，不计入时间线。
        """
        seat = self._seat_row(match_id, section, row, no)
        events = [e for e in self.list_events(seat_id=seat["id"], limit=1000)
                  if e["action"] != "seat.added"]
        return {
            "seat": {"section": section, "row": row, "no": no},
            "events": events,
        }

    def verify_chain(self) -> dict:
        """重放全局哈希链并检查序号连续。返回 ok=False 时给出首个断点。"""
        rows = self.conn.execute("SELECT * FROM events ORDER BY seq ASC").fetchall()
        prev_hash = GENESIS_HASH
        for expected_seq, r in enumerate(rows, start=1):
            if r["seq"] != expected_seq:
                return {"ok": False, "broken_seq": r["seq"],
                        "reason": f"序号不连续，期望 {expected_seq}"}
            if r["prev_hash"] != prev_hash:
                return {"ok": False, "broken_seq": r["seq"], "reason": "prev_hash 不衔接"}
            payload = json.loads(r["payload"])
            h = chain_hash(
                prev_hash, r["seq"], r["ticket_id"], r["seat_id"], r["match_id"],
                r["subject_customer_id"], r["actor_id"], r["action"], payload,
                r["created_at"],
            )
            if h != r["hash"]:
                return {"ok": False, "broken_seq": r["seq"], "reason": "事件哈希不匹配，记录被篡改"}
            prev_hash = r["hash"]
        return {"ok": True, "events": len(rows), "head_hash": prev_hash}


# ------------------------------------------------------------ 备份与恢复

def backup_database(conn: sqlite3.Connection, dest: str | Path) -> Path:
    """在线一致备份（WAL 安全）。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(str(dest))
    try:
        conn.backup(out)
    finally:
        out.close()
    return dest


def restore_database(
    backup_path: str | Path,
    target_path: str | Path,
    *,
    verify: bool = True,
) -> dict:
    """从备份恢复：先做 quick_check 与哈希链校验，再覆盖目标库。

    备份文件被篡改（链断）时抛 Tampered，拒绝恢复。
    """
    src = connect(backup_path)
    try:
        health = src.execute("PRAGMA quick_check").fetchone()[0]
        if health != "ok":
            raise Tampered(f"备份文件完整性检查失败: {health}")
        result = TicketService(src).verify_chain()
        if verify and not result["ok"]:
            raise Tampered(f"备份持有链校验失败，序号 {result['broken_seq']}: {result['reason']}")
        out = sqlite3.connect(str(target_path))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return {"restored_from": str(backup_path), "events": result["events"],
            "head_hash": result["head_hash"]}

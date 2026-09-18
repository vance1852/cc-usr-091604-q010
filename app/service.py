"""票券生命周期服务。

业务规则：
- 所有时间以 UTC 存储；入参时间必须带时区，拒绝朴素 datetime。
- 转让（创建与接收）和退票截止于开赛前 transfer_cutoff_hours 小时（默认 2 小时）。
- 未满 12 岁不能持有票券；12–17 岁接收转让须监护人确认；未满 18 岁不能购票。
- 风控 BLOCKED 用户不能购票/接收/发起转让，其名下有效票券在列入时自动冻结。
- 转让接收成功后原持有人立即失效：持有链追加一环，入场凭证（含链序号签名）随之轮换。
- 入场扫码一次性消费；任何异常票只生成人工复核单，闸机绝不直接放行。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from .chain import GENESIS_HASH, compute_link_hash
from .db import Database
from .errors import (
    ConflictError,
    CutoffPassedError,
    MinorRestrictedError,
    NotFoundError,
    PermissionDeniedError,
    RiskBlockedError,
    SeatUnavailableError,
    ValidationError,
)
from .masking import mask_id_number, mask_name, mask_phone
from .models import (
    ChainEvent,
    InvitationStatus,
    ReviewStatus,
    RiskStatus,
    Role,
    ScanResult,
    TicketStatus,
)

# 异常扫码结果：只进人工复核，闸机不放行
REVIEWABLE_RESULTS = frozenset(
    {
        ScanResult.DENIED_INVALID_TOKEN,
        ScanResult.DENIED_STALE_TOKEN,
        ScanResult.DENIED_ALREADY_USED,
        ScanResult.DENIED_FROZEN,
        ScanResult.DENIED_RETURNED,
        ScanResult.DENIED_REVOKED,
        ScanResult.DENIED_RISK_BLOCKED,
    }
)

GAME_OVER_HOURS = 4.0   # 开赛后 4 小时停止核验
ADULT_AGE = 18          # 购票须年满 18 岁
MIN_HOLDER_AGE = 12     # 持票最低年龄


class TicketLifecycleService:
    """票券生命周期服务：场次/座位/购票/转让/退票/扫码/复核/冻结/审计。"""

    def __init__(
        self,
        db_path: str,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        token_secret: Optional[bytes] = None,
        transfer_cutoff_hours: float = 2.0,
    ):
        self.db = Database(db_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.transfer_cutoff_hours = float(transfer_cutoff_hours)
        # 故障注入钩子（仅测试用）：在关键事务提交前调用，模拟进程崩溃
        self._fault_hook: Optional[Callable[[str], None]] = None
        with self.db.write() as conn:
            self._secret = self._init_secret(conn, token_secret)

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    @staticmethod
    def _init_secret(conn, token_secret: Optional[bytes]) -> bytes:
        """凭证签名密钥：显式传入则覆盖并持久化；否则从库中加载或首次生成。"""
        if token_secret is not None:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('token_secret', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (token_secret.hex(),),
            )
            return token_secret
        row = conn.execute("SELECT value FROM settings WHERE key = 'token_secret'").fetchone()
        if row:
            return bytes.fromhex(row["value"])
        secret = os.urandom(32)
        conn.execute("INSERT INTO settings (key, value) VALUES ('token_secret', ?)", (secret.hex(),))
        return secret

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None:
            raise ValidationError("时钟必须返回带时区的时间")
        return now.astimezone(timezone.utc)

    @staticmethod
    def _iso(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _parse(ts: str) -> datetime:
        return datetime.fromisoformat(ts)

    @staticmethod
    def _require_aware(dt: datetime, field: str) -> datetime:
        if not isinstance(dt, datetime) or dt.tzinfo is None:
            raise ValidationError(f"{field} 必须是带时区的 datetime")
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _one(conn, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        row = conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _all(conn, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]

    @staticmethod
    def _age_on(birth: date, on: date) -> int:
        return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))

    def _cutoff(self, game: dict) -> datetime:
        return self._parse(game["start_time"]) - timedelta(hours=game["transfer_cutoff_hours"])

    def _sig(self, ticket_id: str, seq: int, holder_id: str) -> str:
        msg = f"{ticket_id}.{seq}.{holder_id}".encode("utf-8")
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()[:32]

    def _make_token(self, ticket_id: str, seq: int, holder_id: str) -> str:
        return f"v1.{ticket_id}.{seq}.{self._sig(ticket_id, seq, holder_id)}"

    @staticmethod
    def _parse_token(token: str) -> tuple[Optional[str], Optional[int], Optional[str]]:
        try:
            parts = str(token).split(".")
            if len(parts) != 4 or parts[0] != "v1":
                return None, None, None
            return parts[1], int(parts[2]), parts[3]
        except (ValueError, IndexError):
            return None, None, None

    def _require_role(self, conn, user_id: str, roles: set, action: str) -> dict:
        user = self._one(conn, "SELECT * FROM users WHERE id = ?", (user_id,))
        if user is None:
            raise NotFoundError(f"用户不存在: {user_id}")
        if user["role"] not in roles:
            raise PermissionDeniedError(f"{action}需要 {'/'.join(sorted(str(r) for r in roles))} 权限")
        return user

    def _audit(
        self,
        conn,
        *,
        actor_id: Optional[str],
        actor_role: str,
        action: str,
        entity_type: str,
        entity_id: str,
        game_id: Optional[str] = None,
        seat_id: Optional[str] = None,
        ticket_id: Optional[str] = None,
        before: Optional[dict] = None,
        after: Optional[dict] = None,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_log (actor_id, actor_role, action, entity_type, entity_id,"
            " game_id, seat_id, ticket_id, before_json, after_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                actor_id,
                actor_role,
                action,
                entity_type,
                entity_id,
                game_id,
                seat_id,
                ticket_id,
                json.dumps(before, ensure_ascii=False) if before is not None else None,
                json.dumps(after, ensure_ascii=False) if after is not None else None,
                self._iso(self._now()),
            ),
        )

    def _append_chain(
        self,
        conn,
        *,
        ticket_id: str,
        seq: int,
        event_type: ChainEvent,
        from_holder_id: Optional[str],
        holder_id: str,
        note: str,
        now_iso: str,
    ) -> str:
        prev = self._one(
            conn,
            "SELECT hash FROM ownership_chain WHERE ticket_id = ? ORDER BY seq DESC LIMIT 1",
            (ticket_id,),
        )
        prev_hash = prev["hash"] if prev else GENESIS_HASH
        link_hash = compute_link_hash(
            ticket_id=ticket_id,
            seq=seq,
            event_type=str(event_type),
            from_holder_id=from_holder_id or "",
            holder_id=holder_id,
            note=note,
            created_at=now_iso,
            prev_hash=prev_hash,
        )
        conn.execute(
            "INSERT INTO ownership_chain (ticket_id, seq, event_type, from_holder_id,"
            " holder_id, note, created_at, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, seq, str(event_type), from_holder_id, holder_id, note, now_iso, prev_hash, link_hash),
        )
        return link_hash

    # ------------------------------------------------------------------
    # 注册与基础数据
    # ------------------------------------------------------------------

    def create_game(
        self,
        *,
        name: str,
        venue: str,
        start_time: datetime,
        transfer_cutoff_hours: Optional[float] = None,
        gates_open_hours: float = 3.0,
        game_id: Optional[str] = None,
    ) -> dict:
        start = self._require_aware(start_time, "start_time")
        if not name or not venue:
            raise ValidationError("场次名称和场馆不能为空")
        gid = game_id or f"g_{uuid.uuid4().hex}"
        cutoff_hours = float(
            transfer_cutoff_hours if transfer_cutoff_hours is not None else self.transfer_cutoff_hours
        )
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO games (id, name, venue, start_time, transfer_cutoff_hours,"
                " gates_open_hours, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (gid, name, venue, self._iso(start), cutoff_hours, float(gates_open_hours), self._iso(self._now())),
            )
        return self.get_game(gid)

    def add_seats(self, *, game_id: str, section: str, rows: list, numbers: list, price_cents: int = 0) -> int:
        self.get_game(game_id)
        created = 0
        with self.db.write() as conn:
            for row_name in rows:
                for number in numbers:
                    try:
                        conn.execute(
                            "INSERT INTO seats (id, game_id, section, row, number, price_cents)"
                            " VALUES (?, ?, ?, ?, ?, ?)",
                            (f"s_{uuid.uuid4().hex}", game_id, section, str(row_name), str(number), int(price_cents)),
                        )
                        created += 1
                    except sqlite3.IntegrityError as exc:
                        raise ValidationError(f"座位已存在: {section}-{row_name}-{number}") from exc
        return created

    def register_user(
        self,
        *,
        full_name: str,
        id_number: str,
        phone: str,
        birth_date,
        role: str = Role.FAN,
        user_id: Optional[str] = None,
    ) -> dict:
        try:
            role = Role(role)
        except ValueError:
            raise ValidationError(f"未知角色: {role}") from None
        if isinstance(birth_date, datetime):
            birth_date = birth_date.date()
        if isinstance(birth_date, date):
            birth_date = birth_date.isoformat()
        try:
            date.fromisoformat(birth_date)
        except ValueError:
            raise ValidationError("birth_date 必须是 YYYY-MM-DD") from None
        if not full_name or not id_number or not phone:
            raise ValidationError("姓名、证件号、手机号不能为空")
        uid = user_id or f"u_{uuid.uuid4().hex}"
        with self.db.write() as conn:
            try:
                conn.execute(
                    "INSERT INTO users (id, full_name, id_number, phone, birth_date, role,"
                    " risk_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (uid, full_name, id_number, phone, birth_date, str(role), RiskStatus.CLEAR.value, self._iso(self._now())),
                )
            except sqlite3.IntegrityError as exc:
                raise ValidationError("证件号码已注册") from exc
        return self.get_user(uid)

    def set_risk_status(self, *, admin_id: str, user_id: str, status: str, reason: str) -> dict:
        """调整风控状态。列入 BLOCKED 时自动冻结其全部有效票并撤销相关待处理邀请。"""
        try:
            status = RiskStatus(status)
        except ValueError:
            raise ValidationError(f"未知风控状态: {status}") from None
        if not reason:
            raise ValidationError("必须说明风控调整原因")
        with self.db.write() as conn:
            self._require_role(conn, admin_id, {Role.ADMIN}, "设置风控状态")
            user = self._one(conn, "SELECT * FROM users WHERE id = ?", (user_id,))
            if user is None:
                raise NotFoundError(f"用户不存在: {user_id}")
            conn.execute("UPDATE users SET risk_status = ? WHERE id = ?", (status.value, user_id))
            self._audit(
                conn,
                actor_id=admin_id,
                actor_role=str(Role.ADMIN),
                action="RISK_STATUS_CHANGED",
                entity_type="user",
                entity_id=user_id,
                before={"risk_status": user["risk_status"]},
                after={"risk_status": status.value, "reason": reason},
            )
        frozen: list[str] = []
        cancelled = 0
        if status == RiskStatus.BLOCKED:
            # 先撤销其名下的待处理邀请（双向），再冻结票券
            with self.db.write() as conn:
                cur = conn.execute(
                    "UPDATE transfer_invitations SET status = ?, resolved_at = ?"
                    " WHERE status = ? AND (from_user_id = ? OR to_user_id = ?)",
                    (
                        InvitationStatus.CANCELLED.value,
                        self._iso(self._now()),
                        InvitationStatus.PENDING.value,
                        user_id,
                        user_id,
                    ),
                )
                cancelled = cur.rowcount
                if cancelled:
                    self._audit(
                        conn,
                        actor_id=admin_id,
                        actor_role=str(Role.ADMIN),
                        action="INVITATIONS_CANCELLED",
                        entity_type="user",
                        entity_id=user_id,
                        after={"cancelled": cancelled, "reason": reason},
                    )
            freeze = self.freeze_batch(
                admin_id=admin_id,
                holder_id=user_id,
                reason=f"风控名单：{reason}",
                basis="风控名单拦截规则",
            )
            frozen = freeze["frozen"]
        return {
            "user_id": user_id,
            "risk_status": status.value,
            "frozen_tickets": frozen,
            "cancelled_invitations": cancelled,
        }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_game(self, game_id: str) -> dict:
        with self.db.read() as conn:
            game = self._one(conn, "SELECT * FROM games WHERE id = ?", (game_id,))
        if game is None:
            raise NotFoundError(f"场次不存在: {game_id}")
        return game

    def get_user(self, user_id: str) -> dict:
        with self.db.read() as conn:
            user = self._one(conn, "SELECT * FROM users WHERE id = ?", (user_id,))
        if user is None:
            raise NotFoundError(f"用户不存在: {user_id}")
        return user

    def get_ticket(self, ticket_id: str) -> dict:
        with self.db.read() as conn:
            ticket = self._one(conn, "SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        if ticket is None:
            raise NotFoundError(f"票券不存在: {ticket_id}")
        return ticket

    def get_invitation(self, invitation_id: str) -> dict:
        with self.db.read() as conn:
            inv = self._one(conn, "SELECT * FROM transfer_invitations WHERE id = ?", (invitation_id,))
        if inv is None:
            raise NotFoundError(f"转让邀请不存在: {invitation_id}")
        return inv

    def list_tickets(self, *, game_id: Optional[str] = None, holder_id: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
        clauses, params = [], []
        if game_id:
            clauses.append("game_id = ?")
            params.append(game_id)
        if holder_id:
            clauses.append("holder_id = ?")
            params.append(holder_id)
        if status:
            clauses.append("status = ?")
            params.append(str(status))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.db.read() as conn:
            return self._all(conn, f"SELECT * FROM tickets{where} ORDER BY rowid", params)

    def list_invitations(self, *, ticket_id: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
        clauses, params = [], []
        if ticket_id:
            clauses.append("ticket_id = ?")
            params.append(ticket_id)
        if status:
            clauses.append("status = ?")
            params.append(str(status))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.db.read() as conn:
            return self._all(conn, f"SELECT * FROM transfer_invitations{where} ORDER BY rowid", params)

    # ------------------------------------------------------------------
    # 购票
    # ------------------------------------------------------------------

    def _assert_can_purchase(self, user: dict, game: dict) -> None:
        if user["risk_status"] == RiskStatus.BLOCKED:
            raise RiskBlockedError("用户在风控名单中，禁止购票")
        age = self._age_on(date.fromisoformat(user["birth_date"]), self._parse(game["start_time"]).date())
        if age < ADULT_AGE:
            raise MinorRestrictedError(f"未满 {ADULT_AGE} 岁不能购票（比赛日 {age} 岁）")

    def _assert_can_hold_basic(self, user: dict, game: dict) -> None:
        """发起转让时的基础校验；监护人确认在接收时检查。"""
        if user["risk_status"] == RiskStatus.BLOCKED:
            raise RiskBlockedError("用户在风控名单中，不能持有票券")
        age = self._age_on(date.fromisoformat(user["birth_date"]), self._parse(game["start_time"]).date())
        if age < MIN_HOLDER_AGE:
            raise MinorRestrictedError(f"未满 {MIN_HOLDER_AGE} 岁不能持有票券")

    def _assert_can_hold(
        self,
        conn,
        user: dict,
        game: dict,
        *,
        guardian_consent: bool,
        guardian_id: Optional[str],
    ) -> None:
        """接收转让时的完整校验：未成年人须监护人确认。"""
        self._assert_can_hold_basic(user, game)
        age = self._age_on(date.fromisoformat(user["birth_date"]), self._parse(game["start_time"]).date())
        if age >= ADULT_AGE:
            return
        if not (guardian_consent and guardian_id):
            raise MinorRestrictedError("未成年人接收票券须监护人确认（guardian_consent + guardian_id）")
        guardian = self._one(conn, "SELECT * FROM users WHERE id = ?", (guardian_id,))
        if guardian is None:
            raise ValidationError("监护人不存在")
        if guardian["risk_status"] == RiskStatus.BLOCKED:
            raise RiskBlockedError("监护人在风控名单中")
        guardian_age = self._age_on(date.fromisoformat(guardian["birth_date"]), self._parse(game["start_time"]).date())
        if guardian_age < ADULT_AGE:
            raise ValidationError("监护人必须是成年人")

    def purchase_ticket(
        self,
        *,
        user_id: str,
        game_id: str,
        section: str,
        row,
        number,
        idempotency_key: str,
    ) -> dict:
        """实名购票。同一幂等键重试返回同一张票；同一座位不会卖出两张活票。"""
        if not idempotency_key:
            raise ValidationError("购票必须提供幂等键")
        with self.db.write() as conn:
            existing = self._one(conn, "SELECT * FROM tickets WHERE purchase_idempotency_key = ?", (idempotency_key,))
            if existing:
                return self._purchase_result(existing, replayed=True)
            user = self._one(conn, "SELECT * FROM users WHERE id = ?", (user_id,))
            if user is None:
                raise NotFoundError(f"用户不存在: {user_id}")
            game = self._one(conn, "SELECT * FROM games WHERE id = ?", (game_id,))
            if game is None:
                raise NotFoundError(f"场次不存在: {game_id}")
            seat = self._one(
                conn,
                "SELECT * FROM seats WHERE game_id = ? AND section = ? AND row = ? AND number = ?",
                (game_id, section, str(row), str(number)),
            )
            if seat is None:
                raise NotFoundError(f"座位不存在: {section}-{row}-{number}")
            self._assert_can_purchase(user, game)
            now = self._now()
            if now >= self._parse(game["start_time"]):
                raise ValidationError("场次已开始，停止售票")
            tid = f"t_{uuid.uuid4().hex}"
            now_iso = self._iso(now)
            try:
                conn.execute(
                    "INSERT INTO tickets (id, game_id, seat_id, holder_id, status, prev_status,"
                    " chain_seq, version, purchase_idempotency_key, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, NULL, 0, 0, ?, ?, ?)",
                    (tid, game_id, seat["id"], user_id, TicketStatus.ACTIVE.value, idempotency_key, now_iso, now_iso),
                )
            except sqlite3.IntegrityError as exc:
                again = self._one(conn, "SELECT * FROM tickets WHERE purchase_idempotency_key = ?", (idempotency_key,))
                if again:
                    return self._purchase_result(again, replayed=True)
                raise SeatUnavailableError(f"座位 {section}-{row}-{number} 已售出") from exc
            self._append_chain(
                conn,
                ticket_id=tid,
                seq=0,
                event_type=ChainEvent.PURCHASE,
                from_holder_id=None,
                holder_id=user_id,
                note="实名购票",
                now_iso=now_iso,
            )
            self._audit(
                conn,
                actor_id=user_id,
                actor_role=user["role"],
                action="PURCHASE",
                entity_type="ticket",
                entity_id=tid,
                game_id=game_id,
                seat_id=seat["id"],
                ticket_id=tid,
                after={"status": TicketStatus.ACTIVE.value, "holder_id": user_id},
            )
            ticket = self._one(conn, "SELECT * FROM tickets WHERE id = ?", (tid,))
            return self._purchase_result(ticket, replayed=False)

    def _purchase_result(self, ticket: dict, *, replayed: bool) -> dict:
        return {
            "ticket": ticket,
            "token": self._make_token(ticket["id"], ticket["chain_seq"], ticket["holder_id"]),
            "replayed": replayed,
        }

    # ------------------------------------------------------------------
    # 转让
    # ------------------------------------------------------------------

    def create_transfer(self, *, ticket_id: str, from_user_id: str, to_user_id: str, idempotency_key: str) -> dict:
        """发起转让邀请。同一票同一时刻只允许一个待处理邀请。"""
        if not idempotency_key:
            raise ValidationError("创建转让必须提供幂等键")
        if from_user_id == to_user_id:
            raise ValidationError("不能转让给自己")
        with self.db.write() as conn:
            existing = self._one(conn, "SELECT * FROM transfer_invitations WHERE idempotency_key = ?", (idempotency_key,))
            if existing:
                return {**existing, "replayed": True}
            ticket = self._one(conn, "SELECT * FROM tickets WHERE id = ?", (ticket_id,))
            if ticket is None:
                raise NotFoundError(f"票券不存在: {ticket_id}")
            if ticket["holder_id"] != from_user_id:
                raise PermissionDeniedError("只有当前持有人可以发起转让")
            if ticket["status"] != TicketStatus.ACTIVE:
                raise ConflictError(f"票券状态为 {ticket['status']}，不能转让")
            game = self._one(conn, "SELECT * FROM games WHERE id = ?", (ticket["game_id"],))
            now = self._now()
            now_iso = self._iso(now)
            cutoff = self._cutoff(game)
            if now >= cutoff:
                raise CutoffPassedError("已过转让截止时间")
            recipient = self._one(conn, "SELECT * FROM users WHERE id = ?", (to_user_id,))
            if recipient is None:
                raise NotFoundError(f"接收人不存在: {to_user_id}")
            self._assert_can_hold_basic(recipient, game)
            # 懒清理：先把该票已过期的待处理邀请标记为 EXPIRED
            conn.execute(
                "UPDATE transfer_invitations SET status = ?, resolved_at = ?"
                " WHERE ticket_id = ? AND status = ? AND expires_at <= ?",
                (InvitationStatus.EXPIRED.value, now_iso, ticket_id, InvitationStatus.PENDING.value, now_iso),
            )
            iid = f"inv_{uuid.uuid4().hex}"
            try:
                conn.execute(
                    "INSERT INTO transfer_invitations (id, ticket_id, from_user_id, to_user_id,"
                    " status, idempotency_key, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (iid, ticket_id, from_user_id, to_user_id, InvitationStatus.PENDING.value, idempotency_key, now_iso, self._iso(cutoff)),
                )
            except sqlite3.IntegrityError as exc:
                again = self._one(conn, "SELECT * FROM transfer_invitations WHERE idempotency_key = ?", (idempotency_key,))
                if again:
                    return {**again, "replayed": True}
                raise ConflictError("该票已有待处理的转让邀请") from exc
            self._audit(
                conn,
                actor_id=from_user_id,
                actor_role=str(Role.FAN),
                action="TRANSFER_CREATED",
                entity_type="invitation",
                entity_id=iid,
                game_id=ticket["game_id"],
                seat_id=ticket["seat_id"],
                ticket_id=ticket_id,
                after={"to_user_id": to_user_id, "expires_at": self._iso(cutoff)},
            )
            inv = self._one(conn, "SELECT * FROM transfer_invitations WHERE id = ?", (iid,))
            return {**inv, "replayed": False}

    def accept_transfer(
        self,
        *,
        invitation_id: str,
        user_id: str,
        idempotency_key: str,
        guardian_consent: bool = False,
        guardian_id: Optional[str] = None,
    ) -> dict:
        """接收转让。成功后原持有人立即失效；重复点击/重试不会产生两张可用票。"""
        if not idempotency_key:
            raise ValidationError("接收转让必须提供幂等键")
        self._expire_invitation_if_due(invitation_id)
        with self.db.write() as conn:
            inv = self._one(conn, "SELECT * FROM transfer_invitations WHERE id = ?", (invitation_id,))
            if inv is None:
                raise NotFoundError(f"转让邀请不存在: {invitation_id}")
            if inv["status"] == InvitationStatus.ACCEPTED:
                if inv["accept_idempotency_key"] == idempotency_key:
                    return self._accept_result(conn, inv, replayed=True)
                raise ConflictError("该邀请已被接收")
            if inv["status"] == InvitationStatus.EXPIRED:
                raise CutoffPassedError("转让已截止，邀请已过期")
            if inv["status"] != InvitationStatus.PENDING:
                raise ConflictError(f"邀请状态为 {inv['status']}，不能接收")
            if inv["to_user_id"] != user_id:
                raise PermissionDeniedError("只有被邀请人可以接收")
            ticket = self._one(conn, "SELECT * FROM tickets WHERE id = ?", (inv["ticket_id"],))
            game = self._one(conn, "SELECT * FROM games WHERE id = ?", (ticket["game_id"],))
            now = self._now()
            now_iso = self._iso(now)
            if now >= self._cutoff(game) or now >= self._parse(inv["expires_at"]):
                # 预清理通常已把邀请标记为 EXPIRED；此处为防御性兜底
                raise CutoffPassedError("转让已截止，邀请自动过期")
            if ticket["status"] != TicketStatus.ACTIVE:
                raise ConflictError(f"票券状态为 {ticket['status']}，不能接收转让")
            if ticket["holder_id"] != inv["from_user_id"]:
                raise ConflictError("票券持有人已变更，邀请失效")
            recipient = self._one(conn, "SELECT * FROM users WHERE id = ?", (user_id,))
            self._assert_can_hold(conn, recipient, game, guardian_consent=guardian_consent, guardian_id=guardian_id)
            new_seq = ticket["chain_seq"] + 1
            cur = conn.execute(
                "UPDATE transfer_invitations SET status = ?, accept_idempotency_key = ?,"
                " guardian_id = ?, resolved_at = ? WHERE id = ? AND status = ?",
                (InvitationStatus.ACCEPTED.value, idempotency_key, guardian_id, now_iso, invitation_id, InvitationStatus.PENDING.value),
            )
            if cur.rowcount != 1:
                raise ConflictError("邀请已被其他请求处理")
            self._append_chain(
                conn,
                ticket_id=ticket["id"],
                seq=new_seq,
                event_type=ChainEvent.TRANSFER,
                from_holder_id=inv["from_user_id"],
                holder_id=user_id,
                note=f"转让接收 {invitation_id}",
                now_iso=now_iso,
            )
            cur = conn.execute(
                "UPDATE tickets SET holder_id = ?, chain_seq = ?, version = version + 1, updated_at = ?"
                " WHERE id = ? AND version = ?",
                (user_id, new_seq, now_iso, ticket["id"], ticket["version"]),
            )
            if cur.rowcount != 1:
                raise ConflictError("票券版本冲突，请重试")
            if self._fault_hook:
                self._fault_hook("accept_transfer.before_commit")
            self._audit(
                conn,
                actor_id=user_id,
                actor_role=recipient["role"],
                action="TRANSFER_ACCEPTED",
                entity_type="ticket",
                entity_id=ticket["id"],
                game_id=ticket["game_id"],
                seat_id=ticket["seat_id"],
                ticket_id=ticket["id"],
                before={"holder_id": inv["from_user_id"], "chain_seq": ticket["chain_seq"]},
                after={"holder_id": user_id, "chain_seq": new_seq},
            )
            inv = self._one(conn, "SELECT * FROM transfer_invitations WHERE id = ?", (invitation_id,))
            return self._accept_result(conn, inv, replayed=False)

    def _accept_result(self, conn, inv: dict, *, replayed: bool) -> dict:
        ticket = self._one(conn, "SELECT * FROM tickets WHERE id = ?", (inv["ticket_id"],))
        return {
            "invitation_id": inv["id"],
            "status": inv["status"],
            "ticket_id": inv["ticket_id"],
            "new_holder_id": inv["to_user_id"],
            "chain_seq": ticket["chain_seq"],
            "token": self._make_token(ticket["id"], ticket["chain_seq"], ticket["holder_id"]),
            "replayed": replayed,
        }

    def cancel_transfer(self, *, invitation_id: str, user_id: str) -> dict:
        """撤回转让邀请（仅转让人，且邀请仍待处理）。"""
        with self.db.write() as conn:
            inv = self._one(conn, "SELECT * FROM transfer_invitations WHERE id = ?", (invitation_id,))
            if inv is None:
                raise NotFoundError(f"转让邀请不存在: {invitation_id}")
            if inv["from_user_id"] != user_id:
                raise PermissionDeniedError("只有转让人可以撤回")
            if inv["status"] != InvitationStatus.PENDING:
                raise ConflictError(f"邀请状态为 {inv['status']}，不能撤回")
            now_iso = self._iso(self._now())
            cur = conn.execute(
                "UPDATE transfer_invitations SET status = ?, resolved_at = ? WHERE id = ? AND status = ?",
                (InvitationStatus.CANCELLED.value, now_iso, invitation_id, InvitationStatus.PENDING.value),
            )
            if cur.rowcount != 1:
                raise ConflictError("邀请已被其他请求处理")
            ticket = self._one(conn, "SELECT * FROM tickets WHERE id = ?", (inv["ticket_id"],))
            self._audit(
                conn,
                actor_id=user_id,
                actor_role=str(Role.FAN),
                action="TRANSFER_CANCELLED",
                entity_type="invitation",
                entity_id=invitation_id,
                game_id=ticket["game_id"],
                seat_id=ticket["seat_id"],
                ticket_id=inv["ticket_id"],
                before={"status": InvitationStatus.PENDING.value},
                after={"status": InvitationStatus.CANCELLED.value},
            )
            return self._one(conn, "SELECT * FROM transfer_invitations WHERE id = ?", (invitation_id,))

    def _expire_invitation_if_due(self, invitation_id: str) -> None:
        """接收前预清理：过期邀请在独立事务中标记为 EXPIRED，不随接收失败回滚。"""
        now_iso = self._iso(self._now())
        with self.db.write() as conn:
            inv = self._one(conn, "SELECT * FROM transfer_invitations WHERE id = ?", (invitation_id,))
            if inv is None or inv["status"] != InvitationStatus.PENDING or inv["expires_at"] > now_iso:
                return
            conn.execute(
                "UPDATE transfer_invitations SET status = ?, resolved_at = ? WHERE id = ? AND status = ?",
                (InvitationStatus.EXPIRED.value, now_iso, invitation_id, InvitationStatus.PENDING.value),
            )
            ticket = self._one(conn, "SELECT * FROM tickets WHERE id = ?", (inv["ticket_id"],))
            self._audit(
                conn,
                actor_id=None,
                actor_role="system",
                action="TRANSFER_EXPIRED",
                entity_type="invitation",
                entity_id=invitation_id,
                game_id=ticket["game_id"] if ticket else None,
                seat_id=ticket["seat_id"] if ticket else None,
                ticket_id=inv["ticket_id"],
                before={"status": InvitationStatus.PENDING.value},
                after={"status": InvitationStatus.EXPIRED.value},
            )

    def sweep_expired_invitations(self) -> int:
        """维护任务：把所有已到期的待处理邀请标记为 EXPIRED，返回处理数量。"""
        now_iso = self._iso(self._now())
        with self.db.write() as conn:
            cur = conn.execute(
                "UPDATE transfer_invitations SET status = ?, resolved_at = ? WHERE status = ? AND expires_at <= ?",
                (InvitationStatus.EXPIRED.value, now_iso, InvitationStatus.PENDING.value, now_iso),
            )
            return cur.rowcount

    # ------------------------------------------------------------------
    # 退回
    # ------------------------------------------------------------------

    def return_ticket(self, *, ticket_id: str, user_id: str) -> dict:
        """持票人退票。截止时间与转让一致；退回后座位可再次出售。"""
        with self.db.write() as conn:
            ticket = self._one(conn, "SELECT * FROM tickets WHERE id = ?", (ticket_id,))
            if ticket is None:
                raise NotFoundError(f"票券不存在: {ticket_id}")
            if ticket["holder_id"] != user_id:
                raise PermissionDeniedError("只有持有人可以退票")
            if ticket["status"] != TicketStatus.ACTIVE:
                raise ConflictError(f"票券状态为 {ticket['status']}，不能退回")
            game = self._one(conn, "SELECT * FROM games WHERE id = ?", (ticket["game_id"],))
            now = self._now()
            if now >= self._cutoff(game):
                raise CutoffPassedError("已过退票截止时间")
            now_iso = self._iso(now)
            cancelled = self._cancel_pending_invitations(conn, ticket_id, now_iso)
            new_seq = ticket["chain_seq"] + 1
            self._append_chain(
                conn,
                ticket_id=ticket_id,
                seq=new_seq,
                event_type=ChainEvent.RETURN,
                from_holder_id=user_id,
                holder_id=user_id,
                note="持票人退回",
                now_iso=now_iso,
            )
            conn.execute(
                "UPDATE tickets SET status = ?, prev_status = NULL, chain_seq = ?,"
                " version = version + 1, updated_at = ? WHERE id = ?",
                (TicketStatus.RETURNED.value, new_seq, now_iso, ticket_id),
            )
            self._audit(
                conn,
                actor_id=user_id,
                actor_role=str(Role.FAN),
                action="RETURN",
                entity_type="ticket",
                entity_id=ticket_id,
                game_id=ticket["game_id"],
                seat_id=ticket["seat_id"],
                ticket_id=ticket_id,
                before={"status": TicketStatus.ACTIVE.value},
                after={"status": TicketStatus.RETURNED.value, "cancelled_invitations": cancelled},
            )
            return self._one(conn, "SELECT * FROM tickets WHERE id = ?", (ticket_id,))

    def _cancel_pending_invitations(self, conn, ticket_id: str, now_iso: str) -> int:
        cur = conn.execute(
            "UPDATE transfer_invitations SET status = ?, resolved_at = ? WHERE ticket_id = ? AND status = ?",
            (InvitationStatus.CANCELLED.value, now_iso, ticket_id, InvitationStatus.PENDING.value),
        )
        return cur.rowcount

    # ------------------------------------------------------------------
    # 入场核验
    # ------------------------------------------------------------------

    def ticket_token(self, *, ticket_id: str, user_id: str) -> str:
        """当前持有人的入场凭证。持有人变更后旧凭证立即失效。"""
        with self.db.read() as conn:
            ticket = self._one(conn, "SELECT * FROM tickets WHERE id = ?", (ticket_id,))
            if ticket is None:
                raise NotFoundError(f"票券不存在: {ticket_id}")
            if ticket["holder_id"] != user_id:
                user = self._one(conn, "SELECT * FROM users WHERE id = ?", (user_id,))
                if not user or user["role"] != Role.ADMIN:
                    raise PermissionDeniedError("只有持有人可以获取入场凭证")
            if ticket["status"] != TicketStatus.ACTIVE:
                raise ConflictError(f"票券状态为 {ticket['status']}，无有效入场凭证")
            return self._make_token(ticket_id, ticket["chain_seq"], ticket["holder_id"])

    def scan_ticket(self, *, token: str, device_id: str, gate_id: Optional[str] = None, idempotency_key: str) -> dict:
        """闸机扫码：一次性消费并记录设备、时间、结果；异常票只进人工复核。"""
        if not idempotency_key:
            raise ValidationError("扫码必须提供幂等键")
        if not device_id:
            raise ValidationError("扫码必须提供设备 ID")
        with self.db.read() as conn:
            existing = self._one(conn, "SELECT * FROM scan_events WHERE idempotency_key = ?", (idempotency_key,))
            if existing:
                return {**existing, "review_case_id": self._review_for_scan(conn, existing["id"]), "replayed": True}
        ticket_id, chain_seq, sig = self._parse_token(token)
        now = self._now()
        now_iso = self._iso(now)
        with self.db.write() as conn:
            existing = self._one(conn, "SELECT * FROM scan_events WHERE idempotency_key = ?", (idempotency_key,))
            if existing:
                return {**existing, "review_case_id": self._review_for_scan(conn, existing["id"]), "replayed": True}
            result, detail = self._evaluate_scan(conn, ticket_id, chain_seq, sig, now)
            event_id = f"se_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO scan_events (id, ticket_id, chain_seq, device_id, gate_id, result,"
                " detail, idempotency_key, scanned_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, ticket_id or "UNKNOWN", chain_seq, device_id, gate_id, str(result), detail, idempotency_key, now_iso),
            )
            ticket = self._one(conn, "SELECT * FROM tickets WHERE id = ?", (ticket_id,)) if ticket_id else None
            if result == ScanResult.ALLOWED:
                cur = conn.execute(
                    "UPDATE tickets SET status = ?, version = version + 1, updated_at = ?"
                    " WHERE id = ? AND status = ?",
                    (TicketStatus.USED.value, now_iso, ticket["id"], TicketStatus.ACTIVE.value),
                )
                if cur.rowcount != 1:
                    raise ConflictError("票券状态并发变更，请重试扫码")
            review_case_id = None
            if result in REVIEWABLE_RESULTS:
                review_case_id = f"rc_{uuid.uuid4().hex}"
                conn.execute(
                    "INSERT INTO review_cases (id, ticket_id, scan_event_id, reason, status, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (review_case_id, ticket_id or "UNKNOWN", event_id, detail, ReviewStatus.PENDING.value, now_iso),
                )
            self._audit(
                conn,
                actor_id=None,
                actor_role="gate",
                action="SCAN",
                entity_type="ticket",
                entity_id=ticket_id or "UNKNOWN",
                game_id=ticket["game_id"] if ticket else None,
                seat_id=ticket["seat_id"] if ticket else None,
                ticket_id=ticket["id"] if ticket else None,
                before={"status": ticket["status"]} if ticket else None,
                after={"result": str(result), "device_id": device_id, "gate_id": gate_id},
            )
            event = self._one(conn, "SELECT * FROM scan_events WHERE id = ?", (event_id,))
            return {**event, "review_case_id": review_case_id, "replayed": False}

    def _evaluate_scan(self, conn, ticket_id, chain_seq, sig, now) -> tuple[ScanResult, str]:
        if ticket_id is None or chain_seq is None or sig is None:
            return ScanResult.DENIED_INVALID_TOKEN, "无法解析的票券凭证"
        ticket = self._one(conn, "SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        if ticket is None:
            return ScanResult.DENIED_INVALID_TOKEN, "票券不存在"
        link = self._one(conn, "SELECT holder_id FROM ownership_chain WHERE ticket_id = ? AND seq = ?", (ticket_id, chain_seq))
        if link is None or not hmac.compare_digest(sig, self._sig(ticket_id, chain_seq, link["holder_id"])):
            return ScanResult.DENIED_INVALID_TOKEN, "凭证签名无效"
        # 终态状态优先：冻结/已核销/已退回/已作废的票，无论凭证代次都按状态拒绝
        status = ticket["status"]
        if status == TicketStatus.FROZEN:
            return ScanResult.DENIED_FROZEN, "票券已冻结"
        if status == TicketStatus.USED:
            return ScanResult.DENIED_ALREADY_USED, "票券已核销"
        if status == TicketStatus.RETURNED:
            return ScanResult.DENIED_RETURNED, "票券已退回"
        if status == TicketStatus.REVOKED:
            return ScanResult.DENIED_REVOKED, "票券已作废"
        # 有效票但凭证代次落后：已转让，旧持有人的凭证失效
        if chain_seq != ticket["chain_seq"]:
            return ScanResult.DENIED_STALE_TOKEN, "票券已转让，旧凭证已失效"
        holder = self._one(conn, "SELECT * FROM users WHERE id = ?", (ticket["holder_id"],))
        if holder and holder["risk_status"] == RiskStatus.BLOCKED:
            return ScanResult.DENIED_RISK_BLOCKED, "持票人列入风控名单"
        game = self._one(conn, "SELECT * FROM games WHERE id = ?", (ticket["game_id"],))
        start = self._parse(game["start_time"])
        if now < start - timedelta(hours=game["gates_open_hours"]):
            return ScanResult.DENIED_TOO_EARLY, "未到入场时间"
        if now > start + timedelta(hours=GAME_OVER_HOURS):
            return ScanResult.DENIED_GAME_OVER, "场次已结束"
        return ScanResult.ALLOWED, "核验通过"

    @staticmethod
    def _review_for_scan(conn, scan_event_id: str) -> Optional[str]:
        row = conn.execute("SELECT id FROM review_cases WHERE scan_event_id = ?", (scan_event_id,)).fetchone()
        return row["id"] if row else None

    # ------------------------------------------------------------------
    # 人工复核
    # ------------------------------------------------------------------

    def list_review_cases(self, *, status: Optional[str] = ReviewStatus.PENDING) -> list[dict]:
        with self.db.read() as conn:
            if status is None:
                return self._all(conn, "SELECT * FROM review_cases ORDER BY rowid")
            return self._all(conn, "SELECT * FROM review_cases WHERE status = ? ORDER BY rowid", (str(ReviewStatus(status)),))

    def resolve_review(self, *, case_id: str, admin_id: str, decision: str, note: str = "") -> dict:
        """复核处理：ADMIT 人工放行（核销票券）或 REJECT 拒绝。闸机本身永不放行异常票。"""
        decision_map = {"ADMIT": ReviewStatus.ADMITTED, "REJECT": ReviewStatus.REJECTED}
        if decision not in decision_map:
            raise ValidationError("decision 必须是 ADMIT 或 REJECT")
        target = decision_map[decision]
        with self.db.write() as conn:
            self._require_role(conn, admin_id, {Role.ADMIN}, "复核处理")
            case = self._one(conn, "SELECT * FROM review_cases WHERE id = ?", (case_id,))
            if case is None:
                raise NotFoundError(f"复核单不存在: {case_id}")
            if case["status"] != ReviewStatus.PENDING:
                raise ConflictError("复核单已处理")
            now_iso = self._iso(self._now())
            ticket = self._one(conn, "SELECT * FROM tickets WHERE id = ?", (case["ticket_id"],))
            if target == ReviewStatus.ADMITTED:
                if ticket is None or ticket["status"] != TicketStatus.ACTIVE:
                    raise ConflictError("票券当前不是有效状态，不能人工放行（需先解冻或确认未核销）")
                conn.execute(
                    "UPDATE tickets SET status = ?, version = version + 1, updated_at = ?"
                    " WHERE id = ? AND status = ?",
                    (TicketStatus.USED.value, now_iso, ticket["id"], TicketStatus.ACTIVE.value),
                )
            conn.execute(
                "UPDATE review_cases SET status = ?, decided_by = ?, decision_note = ?, decided_at = ? WHERE id = ?",
                (target.value, admin_id, note, now_iso, case_id),
            )
            self._audit(
                conn,
                actor_id=admin_id,
                actor_role=str(Role.ADMIN),
                action=f"REVIEW_{decision}",
                entity_type="review_case",
                entity_id=case_id,
                game_id=ticket["game_id"] if ticket else None,
                seat_id=ticket["seat_id"] if ticket else None,
                ticket_id=case["ticket_id"],
                before={"status": ReviewStatus.PENDING.value},
                after={"status": target.value, "note": note},
            )
            return self._one(conn, "SELECT * FROM review_cases WHERE id = ?", (case_id,))

    # ------------------------------------------------------------------
    # 冻结 / 解冻
    # ------------------------------------------------------------------

    def freeze_ticket(self, *, admin_id: str, ticket_id: str, reason: str, basis: str) -> dict:
        """冻结单张票券，必须说明原因与依据。"""
        if not reason or not basis:
            raise ValidationError("冻结必须说明原因和依据")
        with self.db.write() as conn:
            self._require_role(conn, admin_id, {Role.ADMIN}, "冻结票券")
            ticket = self._one(conn, "SELECT * FROM tickets WHERE id = ?", (ticket_id,))
            if ticket is None:
                raise NotFoundError(f"票券不存在: {ticket_id}")
            if ticket["status"] == TicketStatus.FROZEN:
                raise ConflictError("票券已处于冻结状态")
            if ticket["status"] != TicketStatus.ACTIVE:
                raise ConflictError(f"票券状态为 {ticket['status']}，不能冻结")
            now_iso = self._iso(self._now())
            cancelled = self._cancel_pending_invitations(conn, ticket_id, now_iso)
            conn.execute(
                "UPDATE tickets SET status = ?, prev_status = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (TicketStatus.FROZEN.value, ticket["status"], now_iso, ticket_id),
            )
            fid = f"fz_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO freeze_actions (id, action, scope, ticket_id, filter_json,"
                " affected_ticket_ids, reason, basis, admin_id, created_at) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                (fid, "FREEZE", "TICKET", ticket_id, json.dumps([ticket_id]), reason, basis, admin_id, now_iso),
            )
            self._audit(
                conn,
                actor_id=admin_id,
                actor_role=str(Role.ADMIN),
                action="FREEZE",
                entity_type="ticket",
                entity_id=ticket_id,
                game_id=ticket["game_id"],
                seat_id=ticket["seat_id"],
                ticket_id=ticket_id,
                before={"status": ticket["status"]},
                after={"status": TicketStatus.FROZEN.value, "reason": reason, "basis": basis, "cancelled_invitations": cancelled},
            )
            return {"freeze_id": fid, "frozen": [ticket_id], "count": 1}

    def freeze_batch(
        self,
        *,
        admin_id: str,
        reason: str,
        basis: str,
        game_id: Optional[str] = None,
        section: Optional[str] = None,
        holder_id: Optional[str] = None,
        ticket_ids: Optional[list] = None,
    ) -> dict:
        """批量冻结：按场次/看台/持有人/票券列表过滤，必须说明原因与依据。"""
        if not reason or not basis:
            raise ValidationError("冻结必须说明原因和依据")
        filters = {
            "game_id": game_id,
            "section": section,
            "holder_id": holder_id,
            "ticket_ids": list(ticket_ids) if ticket_ids else None,
        }
        if not any(v for v in filters.values()):
            raise ValidationError("批量冻结必须提供过滤条件（场次/看台/持有人/票券列表）")
        with self.db.write() as conn:
            self._require_role(conn, admin_id, {Role.ADMIN}, "批量冻结")
            join = ""
            clauses = ["t.status = ?"]
            params: list = [TicketStatus.ACTIVE.value]
            if section is not None:
                join = " JOIN seats s ON s.id = t.seat_id"
                clauses.append("s.section = ?")
                params.append(section)
            if game_id is not None:
                clauses.append("t.game_id = ?")
                params.append(game_id)
            if holder_id is not None:
                clauses.append("t.holder_id = ?")
                params.append(holder_id)
            if ticket_ids:
                clauses.append("t.id IN (%s)" % ",".join("?" * len(ticket_ids)))
                params.extend(ticket_ids)
            rows = self._all(conn, f"SELECT t.* FROM tickets t{join} WHERE {' AND '.join(clauses)}", params)
            now_iso = self._iso(self._now())
            frozen = []
            for t in rows:
                self._cancel_pending_invitations(conn, t["id"], now_iso)
                conn.execute(
                    "UPDATE tickets SET status = ?, prev_status = ?, version = version + 1, updated_at = ?"
                    " WHERE id = ? AND status = ?",
                    (TicketStatus.FROZEN.value, t["status"], now_iso, t["id"], TicketStatus.ACTIVE.value),
                )
                frozen.append(t["id"])
                self._audit(
                    conn,
                    actor_id=admin_id,
                    actor_role=str(Role.ADMIN),
                    action="FREEZE",
                    entity_type="ticket",
                    entity_id=t["id"],
                    game_id=t["game_id"],
                    seat_id=t["seat_id"],
                    ticket_id=t["id"],
                    before={"status": t["status"]},
                    after={"status": TicketStatus.FROZEN.value, "reason": reason, "basis": basis},
                )
            fid = f"fz_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO freeze_actions (id, action, scope, ticket_id, filter_json,"
                " affected_ticket_ids, reason, basis, admin_id, created_at) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
                (
                    fid,
                    "FREEZE",
                    "BATCH",
                    json.dumps({k: v for k, v in filters.items() if v}, ensure_ascii=False),
                    json.dumps(frozen),
                    reason,
                    basis,
                    admin_id,
                    now_iso,
                ),
            )
            return {"freeze_id": fid, "frozen": frozen, "count": len(frozen)}

    def unfreeze_ticket(self, *, admin_id: str, ticket_id: str, reason: str) -> dict:
        """解冻单张票券，恢复冻结前状态。"""
        if not reason:
            raise ValidationError("解冻必须说明原因")
        with self.db.write() as conn:
            self._require_role(conn, admin_id, {Role.ADMIN}, "解冻票券")
            ticket = self._one(conn, "SELECT * FROM tickets WHERE id = ?", (ticket_id,))
            if ticket is None:
                raise NotFoundError(f"票券不存在: {ticket_id}")
            if ticket["status"] != TicketStatus.FROZEN:
                raise ConflictError("票券未处于冻结状态")
            now_iso = self._iso(self._now())
            restored = ticket["prev_status"] or TicketStatus.ACTIVE.value
            conn.execute(
                "UPDATE tickets SET status = ?, prev_status = NULL, version = version + 1, updated_at = ? WHERE id = ?",
                (restored, now_iso, ticket_id),
            )
            conn.execute(
                "INSERT INTO freeze_actions (id, action, scope, ticket_id, filter_json,"
                " affected_ticket_ids, reason, basis, admin_id, created_at) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                (f"fz_{uuid.uuid4().hex}", "UNFREEZE", "TICKET", ticket_id, json.dumps([ticket_id]), reason, "人工解冻", admin_id, now_iso),
            )
            self._audit(
                conn,
                actor_id=admin_id,
                actor_role=str(Role.ADMIN),
                action="UNFREEZE",
                entity_type="ticket",
                entity_id=ticket_id,
                game_id=ticket["game_id"],
                seat_id=ticket["seat_id"],
                ticket_id=ticket_id,
                before={"status": TicketStatus.FROZEN.value},
                after={"status": restored, "reason": reason},
            )
            return self._one(conn, "SELECT * FROM tickets WHERE id = ?", (ticket_id,))

    # ------------------------------------------------------------------
    # 审计查询
    # ------------------------------------------------------------------

    def ticket_history(self, ticket_id: str) -> dict:
        """单票全量历史：持有链、扫码、复核、审计事件。"""
        with self.db.read() as conn:
            ticket = self._one(conn, "SELECT * FROM tickets WHERE id = ?", (ticket_id,))
            if ticket is None:
                raise NotFoundError(f"票券不存在: {ticket_id}")
            return {
                "ticket": ticket,
                "chain": self._all(conn, "SELECT * FROM ownership_chain WHERE ticket_id = ? ORDER BY seq", (ticket_id,)),
                "scans": self._all(conn, "SELECT * FROM scan_events WHERE ticket_id = ? ORDER BY rowid", (ticket_id,)),
                "reviews": self._all(conn, "SELECT * FROM review_cases WHERE ticket_id = ? ORDER BY rowid", (ticket_id,)),
                "audits": self._all(conn, "SELECT * FROM audit_log WHERE ticket_id = ? ORDER BY id", (ticket_id,)),
            }

    def seat_timeline(self, *, game_id: str, section: str, row, number) -> list[dict]:
        """按座位还原每一次状态变化（跨票券：退回再售也连续可见）。"""
        with self.db.read() as conn:
            seat = self._one(
                conn,
                "SELECT * FROM seats WHERE game_id = ? AND section = ? AND row = ? AND number = ?",
                (game_id, section, str(row), str(number)),
            )
            if seat is None:
                raise NotFoundError(f"座位不存在: {section}-{row}-{number}")
            rows = self._all(conn, "SELECT * FROM audit_log WHERE game_id = ? AND seat_id = ? ORDER BY id", (game_id, seat["id"]))
            for r in rows:
                before_json = r.pop("before_json")
                after_json = r.pop("after_json")
                r["before"] = json.loads(before_json) if before_json else None
                r["after"] = json.loads(after_json) if after_json else None
            return rows

    def verify_chain(self, ticket_id: str) -> bool:
        """重算单票持有链哈希，并核对链尾与票券当前状态一致。"""
        with self.db.read() as conn:
            return self._verify_chain(conn, ticket_id)

    def _verify_chain(self, conn, ticket_id: str) -> bool:
        links = self._all(conn, "SELECT * FROM ownership_chain WHERE ticket_id = ? ORDER BY seq", (ticket_id,))
        if not links:
            return False
        prev_hash = GENESIS_HASH
        for expected_seq, link in enumerate(links):
            if link["seq"] != expected_seq or link["prev_hash"] != prev_hash:
                return False
            recomputed = compute_link_hash(
                ticket_id=link["ticket_id"],
                seq=link["seq"],
                event_type=link["event_type"],
                from_holder_id=link["from_holder_id"] or "",
                holder_id=link["holder_id"],
                note=link["note"] or "",
                created_at=link["created_at"],
                prev_hash=link["prev_hash"],
            )
            if recomputed != link["hash"]:
                return False
            prev_hash = link["hash"]
        ticket = self._one(conn, "SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        return (
            ticket is not None
            and ticket["chain_seq"] == links[-1]["seq"]
            and ticket["holder_id"] == links[-1]["holder_id"]
        )

    def verify_integrity(self) -> dict:
        """全库一致性校验：链完整、一座位一活票、一票一待转邀请。"""
        problems: list[str] = []
        with self.db.read() as conn:
            for row in self._all(
                conn,
                "SELECT game_id, seat_id, COUNT(*) AS c FROM tickets"
                " WHERE status IN ('ACTIVE', 'FROZEN', 'USED')"
                " GROUP BY game_id, seat_id HAVING c > 1",
            ):
                problems.append(f"座位 {row['seat_id']} 存在 {row['c']} 张活票")
            for row in self._all(
                conn,
                "SELECT ticket_id, COUNT(*) AS c FROM transfer_invitations"
                " WHERE status = 'PENDING' GROUP BY ticket_id HAVING c > 1",
            ):
                problems.append(f"票券 {row['ticket_id']} 存在 {row['c']} 个待处理转让")
            for row in self._all(conn, "SELECT id FROM tickets"):
                if not self._verify_chain(conn, row["id"]):
                    problems.append(f"票券 {row['id']} 持有链校验失败")
        return {"ok": not problems, "problems": problems}

    def customer_service_view(self, *, ticket_id: str, cs_id: str) -> dict:
        """客服视图：只含脱敏身份摘要与处理投诉所需的票券状态。"""
        with self.db.read() as conn:
            self._require_role(conn, cs_id, {Role.CS, Role.ADMIN}, "客服查询")
            ticket = self._one(conn, "SELECT * FROM tickets WHERE id = ?", (ticket_id,))
            if ticket is None:
                raise NotFoundError(f"票券不存在: {ticket_id}")
            holder = self._one(conn, "SELECT * FROM users WHERE id = ?", (ticket["holder_id"],))
            game = self._one(conn, "SELECT * FROM games WHERE id = ?", (ticket["game_id"],))
            seat = self._one(conn, "SELECT * FROM seats WHERE id = ?", (ticket["seat_id"],))
            last_scan = self._one(
                conn,
                "SELECT * FROM scan_events WHERE ticket_id = ? ORDER BY rowid DESC LIMIT 1",
                (ticket_id,),
            )
            open_reviews = self._one(
                conn,
                "SELECT COUNT(*) AS c FROM review_cases WHERE ticket_id = ? AND status = 'PENDING'",
                (ticket_id,),
            )["c"]
            pending_inv = self._one(
                conn,
                "SELECT id FROM transfer_invitations WHERE ticket_id = ? AND status = 'PENDING'",
                (ticket_id,),
            )
            return {
                "ticket_id": ticket_id,
                "ticket_status": ticket["status"],
                "game": {"name": game["name"], "venue": game["venue"], "start_time": game["start_time"]},
                "seat": {"section": seat["section"], "row": seat["row"], "number": seat["number"]},
                "holder_summary": {
                    "name": mask_name(holder["full_name"]),
                    "id_number": mask_id_number(holder["id_number"]),
                    "phone": mask_phone(holder["phone"]),
                },
                "holder_risk_status": holder["risk_status"],
                "chain_length": ticket["chain_seq"] + 1,
                "pending_invitation": pending_inv is not None,
                "last_scan": (
                    {
                        "result": last_scan["result"],
                        "device_id": last_scan["device_id"],
                        "scanned_at": last_scan["scanned_at"],
                    }
                    if last_scan
                    else None
                ),
                "open_review_cases": open_reviews,
            }

    def admin_view(self, *, ticket_id: str, admin_id: str) -> dict:
        """管理员视图：完整信息（含未脱敏身份与全部流转记录）。"""
        with self.db.read() as conn:
            self._require_role(conn, admin_id, {Role.ADMIN}, "管理员查询")
            ticket = self._one(conn, "SELECT * FROM tickets WHERE id = ?", (ticket_id,))
            if ticket is None:
                raise NotFoundError(f"票券不存在: {ticket_id}")
            holder = self._one(conn, "SELECT * FROM users WHERE id = ?", (ticket["holder_id"],))
            invitations = self._all(conn, "SELECT * FROM transfer_invitations WHERE ticket_id = ? ORDER BY rowid", (ticket_id,))
            freezes = self._all(
                conn,
                "SELECT * FROM freeze_actions WHERE affected_ticket_ids LIKE ? ORDER BY rowid",
                (f'%"{ticket_id}"%',),
            )
        history = self.ticket_history(ticket_id)
        return {
            **history,
            "holder": holder,
            "invitations": invitations,
            "freeze_actions": freezes,
        }

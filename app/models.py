"""领域枚举。全部继承 StrEnum，落库即为字符串。"""

from enum import StrEnum


class Role(StrEnum):
    FAN = "fan"        # 普通球迷
    ADMIN = "admin"    # 管理员（冻结、复核、风控）
    CS = "cs"          # 客服（仅可见脱敏身份摘要）


class RiskStatus(StrEnum):
    CLEAR = "CLEAR"
    WATCH = "WATCH"
    BLOCKED = "BLOCKED"


class TicketStatus(StrEnum):
    ACTIVE = "ACTIVE"        # 有效，可转让/退票/扫码
    USED = "USED"            # 已入场核销（一次性消费）
    RETURNED = "RETURNED"    # 已退回，座位可再次出售
    FROZEN = "FROZEN"        # 已冻结，禁止转让与入场
    REVOKED = "REVOKED"      # 已作废


class InvitationStatus(StrEnum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class ScanResult(StrEnum):
    ALLOWED = "ALLOWED"
    DENIED_INVALID_TOKEN = "DENIED_INVALID_TOKEN"    # 伪造/无法解析的凭证
    DENIED_STALE_TOKEN = "DENIED_STALE_TOKEN"        # 转让后旧持有人的失效凭证
    DENIED_ALREADY_USED = "DENIED_ALREADY_USED"      # 重复扫码
    DENIED_FROZEN = "DENIED_FROZEN"
    DENIED_RETURNED = "DENIED_RETURNED"
    DENIED_REVOKED = "DENIED_REVOKED"
    DENIED_RISK_BLOCKED = "DENIED_RISK_BLOCKED"
    DENIED_TOO_EARLY = "DENIED_TOO_EARLY"            # 未到入场时间（提示性，不进复核）
    DENIED_GAME_OVER = "DENIED_GAME_OVER"            # 场次已结束（提示性，不进复核）


class ReviewStatus(StrEnum):
    PENDING = "PENDING"
    ADMITTED = "ADMITTED"    # 人工复核放行
    REJECTED = "REJECTED"


class ChainEvent(StrEnum):
    PURCHASE = "PURCHASE"
    TRANSFER = "TRANSFER"
    RETURN = "RETURN"

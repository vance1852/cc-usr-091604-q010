"""票券领域异常。"""


class TicketError(Exception):
    """所有票券领域错误的基类。"""


class NotFoundError(TicketError):
    """请求的实体不存在。"""


class ValidationError(TicketError):
    """请求参数不合法。"""


class PermissionDeniedError(TicketError):
    """当前身份无权执行该操作。"""


class ConflictError(TicketError):
    """与当前状态冲突（含并发竞争失败）。"""


class CutoffPassedError(ConflictError):
    """已超过转让/退票截止时间。"""


class SeatUnavailableError(ConflictError):
    """座位已售出，不能重复出票。"""


class MinorRestrictedError(ValidationError):
    """触犯未成年人购票/持有限制。"""


class RiskBlockedError(PermissionDeniedError):
    """用户被列入风控名单。"""

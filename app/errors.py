"""领域错误。HTTP 层据此映射状态码。"""


class TicketError(Exception):
    """所有业务规则错误的基类。"""

    http_status = 400
    code = "bad_request"


class NotFound(TicketError):
    http_status = 404
    code = "not_found"


class Conflict(TicketError):
    """并发冲突（票据被锁定 / 邀请状态已变 / 重复消费）。"""

    http_status = 409
    code = "conflict"


class RuleViolation(TicketError):
    """业务规则不满足：截止时间、未成年、风控、冻结等。"""

    http_status = 422
    code = "rule_violation"


class ManualReviewRequired(TicketError):
    """异常票：不放行，转人工复核。"""

    http_status = 423
    code = "manual_review_required"


class Tampered(TicketError):
    """持有链哈希校验失败 / 备份不一致。"""

    http_status = 409
    code = "tampered"

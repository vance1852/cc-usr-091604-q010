"""票券领域的最小起点。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Ticket:
    """保存场次和座位的基础标识。"""

    match_id: str
    seat: str


class TicketService:
    """提供票券服务的基础健康状态。"""

    def health(self) -> dict[str, str]:
        return {"service": "ticket", "status": "ok"}


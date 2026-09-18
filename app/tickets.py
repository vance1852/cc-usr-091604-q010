"""票券领域入口（向后兼容的轻量封装）。

完整实现见：
- app.db：SQLite 模式（不可变事件链、唯一约束）
- app.service：票券生命周期核心 TicketLifecycleService
- app.api：HTTP JSON 接口
"""

from __future__ import annotations

from dataclasses import dataclass

from app.service import TicketService as TicketLifecycleService  # noqa: F401


@dataclass(frozen=True)
class Ticket:
    """保存场次和座位的基础标识。"""

    match_id: str
    seat: str


class TicketService:
    """提供票券服务的基础健康状态（历史接口，保持兼容）。"""

    def health(self) -> dict[str, str]:
        return {"service": "ticket", "status": "ok"}

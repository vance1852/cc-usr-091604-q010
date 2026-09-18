"""端到端演示：购票 → 转让 → 确认 → 异常冻结 → 扫码复核 → 审计还原。

运行：python -m scripts.demo
"""

from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path

from app.db import connect, init_db
from app.service import TicketService
from app.util import now_utc


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="ticket-demo-")
    db_path = str(Path(tmp) / "demo.db")
    init_db(db_path)
    conn = connect(db_path)
    svc = TicketService(conn)

    tipoff = (now_utc() + timedelta(days=3)).isoformat()
    svc.create_match("G3", "季后赛第三场", "城市球馆", tipoff)
    svc.add_seats("G3", [("A区", "3", "8"), ("A区", "3", "9")])

    svc.register_customer("alice", "爱丽丝", "ID-ALICE-0001", "1992-01-01", "13800000001")
    svc.register_customer("bob", "鲍勃", "ID-BOB-0002", "1993-02-02", "13800000002")
    svc.register_customer("carol", "卡罗尔", "ID-CAROL-0003", "1994-03-03", "13800000003")

    ticket = svc.purchase_ticket("G3", "A区", "3", "8", "alice", "buy-1")
    print("购票:", ticket["ticket_id"], "持有人", ticket["holder_id"])

    invite = svc.create_transfer_invite(ticket["ticket_id"], "alice", "bob", "tr-1")
    print("转让邀请:", invite["invite_id"], "截止", invite["expires_at"])
    view = svc.accept_transfer(invite["invite_id"], "bob", "acc-1")
    print("接收确认后持有人:", view["holder_id"], "状态", view["status"])

    # 客服只看到脱敏摘要
    print("客服视图:", svc.customer_support_summary("bob")["name_masked"],
          svc.customer_support_summary("bob")["id_doc_masked"])

    # 管理员冻结一张可疑票（另一张 alice 的票），闸机转人工
    t2 = svc.purchase_ticket("G3", "A区", "3", "9", "alice", "buy-2")
    batch = svc.freeze_batch([t2["ticket_id"]], "admin-7", "支付账户关联黄牛团伙 #55")
    print("批量冻结:", batch["batch_id"])
    scan = svc.scan_ticket(t2["ticket_id"], "gate-EAST-3")
    print("冻结票扫码结果:", scan["result"], "/", scan["reason"], "→ 转人工，不放行")
    svc.unfreeze_ticket(t2["ticket_id"], "admin-7", "线下核验为本人，解除")
    review = svc.resolve_review(scan["scan_id"], "staff-2", "allow_entry",
                                "身份证件与购票人一致")
    print("人工复核:", review["decision"])

    # 正常票一次性消费
    gate = svc.scan_ticket(ticket["ticket_id"], "gate-WEST-1", presenter_id="bob")
    print("正常扫码:", gate["result"])
    again = svc.scan_ticket(ticket["ticket_id"], "gate-WEST-2")
    print("重复扫码:", again["result"], "/", again["reason"])

    # 审计：按座位还原全部状态变化
    history = svc.seat_history("G3", "A区", "3", "8")
    print("\n座位 A区-3-8 状态变化时间线：")
    for e in history["events"]:
        print(f"  #{e['seq']} {e['at']} {e['action']}  主体={e['subject_customer_id']}")
    print("持有链校验:", svc.verify_chain())

    conn.close()


if __name__ == "__main__":
    main()

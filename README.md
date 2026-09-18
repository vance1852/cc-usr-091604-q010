# 球迷票券生命周期服务

篮球俱乐部电子票券服务：管理场次、看台座位、实名购票、转让邀请、接收确认、退回与入场核验，
解决"同一张电子票被转给不同账号、闸机只认最后一次扫码、赛后无法追查票券流向"的问题。

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

仅依赖 Python 3.11 标准库（sqlite3 / hmac / hashlib / threading / zoneinfo），无需安装任何第三方包。

## 目录

- `app/service.py`：`TicketLifecycleService`，全部业务接口
- `app/db.py`：SQLite 持久化（WAL 模式、`BEGIN IMMEDIATE` 写事务、唯一约束兜底）
- `app/chain.py`：持有链哈希计算
- `app/models.py`：状态枚举（票券/邀请/扫码结果/复核/风控/角色）
- `app/errors.py`：领域异常
- `app/masking.py`：客服视图身份脱敏
- `app/tickets.py`：最初的票券基础对象（保留）
- `tests/`：生命周期、并发、跨时区、冻结风控、数据库恢复五组测试

## 业务规则

| 规则 | 实现 |
| --- | --- |
| 转让/退票截止 | 开赛前 `transfer_cutoff_hours` 小时（默认 2 小时，可按场次覆盖）；创建与接收都校验，过期邀请自动置为 `EXPIRED` |
| 未成年人 | 未满 12 岁不能持票；12–17 岁接收转让须监护人确认（`guardian_consent` + 成年监护人 `guardian_id`）；未满 18 岁不能购票 |
| 风控名单 | `BLOCKED` 用户禁止购票/接收/发起转让；列入时自动冻结其全部有效票并撤销相关待处理邀请；解除风控不自动解冻，须管理员逐张核实 |
| 时间与时区 | 全部时间以 UTC 存储；入参必须是带时区的 `datetime`，朴素时间直接拒绝 |

## 核心机制

**不可变持有链**：每次持有人变更（购票/转让/退回）向 `ownership_chain` 追加一环，
记录 `seq`、前后持有人、时间、前一环哈希与自身 SHA-256。`verify_chain` / `verify_integrity`
可重算校验，任何篡改都会失配（测试覆盖直接改库的场景）。

**接收即失效，绝不产生两张可用票**：
- 同一票同一时刻最多一个待处理邀请（部分唯一索引 `WHERE status='PENDING'`）；
- 接收在单个 `BEGIN IMMEDIATE` 事务内完成"邀请置为已接收 + 链追加 + 持有人切换"，
  乐观锁（`version`）与状态检查兜底；
- 入场凭证为 `v1.{ticket_id}.{chain_seq}.{HMAC(票,代次,持有人)}`，持有人变更后旧凭证立即失效；
- 购票/创建转让/接收/扫码均要求幂等键，重试返回原结果，重复点击不会产生第二条记录。

**一次性消费与人工复核**：扫码在写事务中判定并原子地把 `ACTIVE → USED`；
每次扫码（含伪造凭证）都记录设备、时间、结果。异常票（已核销/已转让旧凭证/冻结/已退/
伪造/风控）一律拒绝并自动生成人工复核单，闸机绝不直接放行；只有管理员
`resolve_review(decision="ADMIT")` 才能人工放行（核销），且票必须处于有效状态。

**冻结**：管理员可 `freeze_ticket`（单张）或 `freeze_batch`（按场次/看台/持有人/票券列表），
必须填写原因与依据；冻结撤销其待处理邀请、禁止转让与入场；`unfreeze_ticket` 恢复冻结前状态。

**客服脱敏**：`customer_service_view` 仅返回掩码后的身份摘要（`张**`、`110************234`、
`138****5678`）与处理投诉所需的票券状态；完整信息仅 `admin_view` 对管理员开放。

**审计还原**：所有状态变化写入 `audit_log`（操作者、动作、前后值、场次/座位/票券外键），
`seat_timeline(game_id, section, row, number)` 按座位还原每一次状态变化（含退回再售）；
`ticket_history` 汇总单票的链、扫码、复核与审计记录。

## 接口一览

| 类别 | 方法 |
| --- | --- |
| 购票 | `purchase_ticket`（幂等）、`ticket_token` |
| 转让 | `create_transfer`、`accept_transfer`（幂等）、`cancel_transfer`（撤回）、`sweep_expired_invitations` |
| 退回 | `return_ticket` |
| 扫码 | `scan_ticket`（幂等，一次性消费） |
| 复核 | `list_review_cases`、`resolve_review` |
| 冻结 | `freeze_ticket`、`freeze_batch`、`unfreeze_ticket`、`set_risk_status` |
| 审计查询 | `ticket_history`、`seat_timeline`、`verify_chain`、`verify_integrity`、`customer_service_view`、`admin_view` |
| 基础数据 | `create_game`、`add_seats`、`register_user`、`get_*`、`list_*` |

## 测试覆盖（50 例）

- **并发转让**：8 线程并发接收同一邀请仅 1 个成功；6 线程并发向不同账号发起转让仅 1 个待处理；6 线程抢购同座位仅售出 1 张
- **跨时区截止**：开赛时间用 `Asia/Shanghai` 录入、操作用 `America/New_York`/UTC 表达，同一物理时刻判定一致；截止边界精确到秒；朴素时间拒绝
- **重复扫码**：10 线程并发扫码仅放行 1 次，其余 9 次自动生成复核单；同幂等键重试返回同一事件
- **批量冻结**：按看台/票券列表冻结并记录依据，冻结票扫码进复核，解冻后恢复
- **数据库恢复**：接收事务中途崩溃整体回滚；`backup` 快照在"主库丢失"后恢复且业务可继续；重启后持有链与入场凭证（密钥持久化于库内）仍可用

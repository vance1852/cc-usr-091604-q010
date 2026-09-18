# 球迷票券生命周期服务

篮球俱乐部电子票券服务：管理场次/看台座位、实名购票、转让邀请与接收确认、退回、闸机入场核验、人工复核、风控名单、管理员冻结与不可变审计链。纯 Python 标准库实现（SQLite + http.server），无第三方依赖。

## 要解决的问题

同一张电子票被转给不同账号、闸机只认最后一次扫码、赛后无法追查流向。本服务的答案：

- **不可变持有链**：每次状态变化写入只追加的 `events` 表，触发器禁止 UPDATE/DELETE，事件以全局序号 + sha256 链式哈希串联；赛后可按票、按座位、按人、按场次还原全部流向，篡改即断链。
- **接收确认即换权**：接收方确认在同一事务内换持票人，原持有人立即失效；`one_pending_invite_per_ticket` 唯一索引保证一票同时只有一笔待确认邀请。
- **重复点击/网络重试不出两张票**：所有写接口支持幂等键（`Idempotency-Key`），首次结果（含业务失败）被重放；座位有 `one_live_ticket_per_seat` 部分唯一索引兜底。
- **一次性扫码消费**：入场是带条件的原子状态迁移（`issued → consumed`），并发双扫只有一台闸机成功；每次扫码记录设备、时间、结果。
- **异常票不直接放行**：冻结、风控、在途转让、人票不符一律返回 `review`（HTTP 423），只能人工复核；复核放行才补消费。

## 运行测试 / 演示 / 服务

```bash
python -m unittest discover -s tests -v   # 60 个测试
python -m scripts.demo                    # 端到端流程演示
python -m app.api --db tickets.db --port 8080
```

## 业务规则

| 规则 | 实现 |
|---|---|
| 实名购票 | 证件号只存 sha256 哈希与脱敏串；同一证件只能注册一个账号 |
| 未成年人 | 比赛当日须年满 14 周岁（按生日精确计算），否则拒绝购票 |
| 风控名单 | 名单内客户禁止购票、发起/接收转让；已持票的票扫码转人工；名单进出均留审计事件 |
| 转让截止 | 默认开赛前 2 小时（建场次时可自定义，必须早于开赛）；跨时区按 UTC 瞬时比较；邀请创建时快照截止时刻；到点未确认由维护任务置失效 |
| 退回 | 截止时间前、本人、未冻结、无在途转让；退回后座位可重新售出（座位时间线跨票券世代可查） |
| 冻结 | 管理员可单张/整批冻结，必须填写依据，整批共享批次号、同事务原子生效、任一不存在整批回滚；冻结阻断转让/退回/扫码放行 |
| 客服权限 | 仅能查看脱敏身份摘要（姓氏 + 证件/手机掩码），看不到明文 PII |

## HTTP 接口

请求头：`X-Role: customer|support|admin|auditor`、写接口可选 `Idempotency-Key`、管理员操作 `X-Actor-Id`。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/customers` | 实名注册 |
| GET | `/customers/{id}/support-summary` | 客服脱敏摘要（support/admin） |
| POST | `/admin/risk/{id}` | 加入风控名单（body: reason） |
| DELETE | `/admin/risk/{id}` | 移出风控名单（body: note） |
| POST | `/admin/matches` | 建场次（event_time 可带任意时区偏移） |
| POST | `/admin/matches/{id}/seats` | 批量建看台座位 |
| POST | `/tickets/purchase` | 实名购票（idempotency_key 必填） |
| GET | `/tickets/{id}` | 票券当前状态 |
| POST | `/tickets/{id}/transfers` | 发起转让邀请 |
| POST | `/transfers/{invite_id}/accept` | 接收方确认（原持有人即时失效） |
| POST | `/transfers/{invite_id}/cancel` | 出让人撤回 / 接收方拒绝 |
| POST | `/tickets/{id}/return` | 退回 |
| POST | `/scans` | 闸机扫码：admitted(200) / rejected(200) / review(423) |
| POST | `/scans/{id}/review` | 人工复核 decision=allow_entry/deny_entry（admin） |
| POST | `/admin/freezes` | 单张或整批冻结（ticket_ids + reason） |
| POST | `/admin/tickets/{id}/unfreeze` | 解冻（note 必填） |
| POST | `/admin/maintenance/expire-transfers` | 到点邀请失效任务 |
| GET | `/tickets/{id}/chain` | 票的持有链 |
| GET | `/matches/{id}/seats/{section}/{row}/{no}/history` | 按座位还原状态变化 |
| GET | `/events?ticket_id=&seat_id=&match_id=&customer_id=` | 审计事件查询 |
| GET | `/admin/verify-chain` | 重放哈希链，检测篡改/断号 |

## 并发与恢复

- 每个写操作 `BEGIN IMMEDIATE` + 条件 UPDATE/部分唯一索引，跨线程/跨进程并发安全；HTTP 层另有写锁串行化。
- `app.service.backup_database` / `restore_database`：在线一致备份；恢复前自动 `PRAGMA quick_check` 并重放持有链，链断（备份被篡改）拒绝恢复。

## 目录

- `app/db.py`：表结构、触发器、索引
- `app/service.py`：`TicketService` 领域逻辑 + 备份恢复
- `app/api.py`：HTTP JSON 接口与角色控制
- `app/util.py`：UTC 时间、年龄、脱敏、链式哈希
- `tests/`：规则、并发转让、跨时区截止、重复扫码、批量冻结、审计与恢复、HTTP 端到端

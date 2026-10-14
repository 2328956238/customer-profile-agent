# Transactional Outbox：离线交付闭环

**2026-10-14 脱敏展示版新增。** 所有数据和接收端均为虚构，本模块未在原业务部署，不代表生产性能或真实渠道交付业绩。不读取真实客户、模型凭证，不调用 WhatsApp、邮件、CRM 或任何外发接口。

## 可复现演示

```bash
python -m customer_profile_agent.outbox_demo
python -m pytest tests/test_outbox.py
```

演示在临时目录中创建两个独立 SQLite 数据库，结束后清理。`MockProvider` 完成两个固定询盘回合；已校验 `handoff` 的回合结果和待交付事件在同一个数据库事务中提交。接收端保存一份摘要后，故障注入模拟 Dispatcher 在确认前退出。推进虚构时钟至租约到期，重建存储、Dispatcher 和接收端，再次发送相同 `event_id`。

预期输出：`pending → inflight → delivered`；`delivery_attempts=2`，`receiver_receipts=1`，`duplicate_recognized_by_receiver=true`，`real_external_messages_sent=0`。重放同一回合不会新建事件。这里演示“重复尝试 + 接收端幂等”，不能据此宣称外部 exactly-once。

## 原子边界

```text
校验并保存画像 → 校验 finish_turn → SQLite BEGIN IMMEDIATE
                                ├─ INSERT outbox（固定摘要快照）
                                └─ UPDATE turns → done（结果含 event_id）
                                      COMMIT

离线 Dispatcher 原子领取 → 虚构接收端保存/去重 → 持有有效租约的 worker 确认
```

- 只为通过 Agent 校验的 `handoff` / `escalate` 结果创建事件；普通回答、追问和失败结果不创建。模型不能选择租户、目标接收端或事件 ID。
- `event_id = SHA-256(JSON(["handoff.v1", customer_scope, message_id]))`，且 `(cid, mid)` 唯一；同一回合不因重新运行生成另一条事件。新消息的修订摘要是一个新事件，接收端不会把不同修订合并。
- `Store.finish_turn` 在完成回合的同一事务里保存事件；事件插入或回合更新失败都会回滚。完成结果保持不可变，冲突内容被拒绝。
- 画像更新仍在此前独立事务里完成；模型调用期间不持有 SQLite 写锁。交付提交失败时，已保存的事实不会回滚，Agent 不返回可发送回复，并将回合记为失败。这不是涵盖整个模型交互的大事务。
- 已完成回合重放返回原始结果，所以其中 `delivery.status=pending` 是**创建时快照**。查询当前状态请使用下列 API，不依赖旧响应判断是否已经交付。

## 领取、重试与故障

每个 Dispatcher 显式绑定一个已经过命名空间编码的 `customer_scope`。在 `BEGIN IMMEDIATE` 中选取该客户的到期待处理事件或过期租约事件，增加 `attempts`，设置随机 `claim_token` 和租约截止时间。多进程竞争同一事件时，只有一个领取者获胜。

确认与失败写回同时约束客户、事件、`inflight` 状态、领取 token 和未过期租约。租约到期后，即使尚未被重新领取，旧 worker 也不能写回；重新领取后旧 token 永远不能覆盖新结果。该 fencing 保护的是本地状态写回，不能撤销已到达接收端的操作。

默认每个事件最多尝试 3 次，租约 30 秒，异常后使用 `min(60, 1 × 2^(attempts-1))` 秒退避。事件达到尝试上限进入 `dead`；最后一次尝试进程退出，会在下一次同客户领取扫描时收敛为 `dead`。所有 worker 应使用一致的重试配置。次数含崩溃的领取，正常故障不会无限重试。

`dead` 表示需要人工核对，**不表示接收端一定没收到**。例如接收端成功后，最后一次 Dispatcher 在确认前退出，接收端可能已有回执。本演示没有死信自动重放或管理写接口；不能直接重置次数来保证安全。租约没有心跳续租，耗时超出租约会失去确认权限并可能产生重复尝试。

`FictionalSink` 是独立的本地 SQLite 接收器，按稳定 `event_id` 保存一次摘要，重复请求核对 scope 和完整 payload 后返回同一个事件回执。相同 ID 的不同内容被拒绝。Dispatcher 只在接收端返回匹配事件 ID 后尝试确认；异常消息不存入状态，仅保留异常类型，避免泄露输入内容。

## 读取状态和隔离

```text
GET /profiles/{customer_id}/deliveries
GET /profiles/{customer_id}/deliveries/{event_id}
```

启用既有 `PROFILE_AUTH_TOKENS` 后，这两个路由和画像读取使用同一个 Bearer 校验依赖。租户由服务器凭证配置确定，不能用 query、header 或事件 ID 改选租户。列表最多返回最近 50 条（响应含 `limit=50`），详情用“授权 scope + event_id”查找；不知道或不属于当前 scope 的事件统一返回 `404`。列表/详情只返回交付状态，不返回摘要正文、领取 token 或凭证。

默认无认证时仍仅为本机演示；状态路由不会自动启动 Dispatcher。Dispatcher、存储方法和接收器属于受信任的本机调用，不提供跨用户授权边界；不可当成允许任意用户指定 scope 的公共 API。

`FileStore` 保持原来的已校验本地摘要能力，不支持 Outbox：结果没有 `delivery` 字段，状态路由返回 `501`，构造 Dispatcher 明确报错。

## 已覆盖的验证

- 完成回合和事件插入任一环节失败均事务回滚；回合结果不可变。
- Agent 在交付意图保存失败时返回空回复和 `retryable=true`。
- 重放、重启、并发完成不生成重复事件；4 个进程竞争领取只有一个获胜。
- 租约过期、旧 token、其他客户不能确认或失败写回。
- 接收端保存后退出、重启重试、稳定 ID 去重，接收端回执仍为一份。
- 指数退避、最大次数、最后一次崩溃收敛为死信、错误回执不确认。
- HTTP 身份认证、同客户/同消息 ID 的跨租户隔离、同租户不同客户隔离、内部 scope 注入拒绝。
- FileStore 旧行为继续可用；无网络测试使用虚构数据。

该模块未验证真实外部接收端幂等语义、跨数据库原子提交、生产吞吐、排队公平性或不同回合摘要的严格交付顺序。这些都不能从离线演示推导出来。

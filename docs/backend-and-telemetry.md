# 本机客服 API：存储、并发与调用观测

**2026-10-15 公开脱敏展示版新增。** 以下说明对应当前代码，展示单进程 Python 服务如何维护一致状态、限制同时处理量并核对模型调用记录。它不代表原业务系统已部署这些能力，也不提供分布式协调、供应商账单保证或生产吞吐量结论。

## 存储与历史分页

默认 `Store` 使用 `PROFILE_DB` 指定的 SQLite 文件，开启 WAL；画像、回合、Outbox、个人身份/审计、知识库和请求 trace 使用同一文件中的不同表。每次存储操作创建连接，SQLite 锁等待超时为 5 秒。WAL 允许读写更好地并行，但写事务仍会争用同一数据库，不能理解为无限并发写入。

画像保存采用 revision 的 compare-and-set 语义：`BEGIN IMMEDIATE` 后读取当前 revision，只有它等于调用方的 `expected` 才写入新 revision；过期版本抛出 `Conflict`。这不是只依赖进程内锁的“最后一次写入获胜”。同一客户、同一消息 ID 的完成结果可以重放；把消息 ID 用于不同内容会被拒绝。

完成回合与待交付 Outbox 事件在同一个 SQLite 事务内提交；画像补丁是先前独立提交的，模型调用全过程不是一个长数据库事务。Outbox 的重试及幂等边界见 [transactional-outbox.md](transactional-outbox.md)。共享一个数据库文件也不意味着操作审计、请求 trace 与所有业务写入具有共同事务。

`GET /profiles/{customer_id}/history` 按 `(created DESC, mid DESC)` 返回已完成回合，`limit` 为 1–100，默认 20。`next_cursor` 是 `[created, message_id]`，下一页把它作为 JSON 字符串传入 `before`。查询使用严格小于游标的条件：

```sql
WHERE cid = ? AND status = 'done' AND (created, mid) < (?, ?)
ORDER BY created DESC, mid DESC
LIMIT ?
```

对应的部分复合索引：

```sql
CREATE INDEX turns_done_history
ON turns(cid, created, mid) WHERE status = 'done';
```

`mid` 消除相同时间戳的排序歧义；向最新端插入新记录不会像 offset 翻页那样把后续页整体推移。分页不是跨请求数据库快照：分页期间较早开始但尚未完成的回合变为 `done`，仍可能影响可见集合。租户前缀后的内部客户 ID 用于查询，不从游标或请求头接受另一个租户。测试检查相同时间戳、翻页间新增消息以及 `EXPLAIN QUERY PLAN` 使用该索引。

Agent 构造上下文使用最近 40 条历史**消息**，不同于历史 API 按**回合**分页。`FileStore` 保留旧演示能力，但历史 keyset API 和 Outbox API 明确需要 SQLite，否则返回 501。

## 并发容量、客户锁与取消

`Admission` 在单个事件循环内，检查并立即占用全局与租户计数，中间不发生 `await`。默认同时处理上限为全局 8、每租户 4。容量不足立即返回 HTTP 429，包含 `retryable: true` 与 `Retry-After: 1`，不在内存中无限排队。请求完成或退出时回收计数，已无活动请求的租户计数项会被删除。

容量控制覆盖回合处理、知识搜索、知识回答和意向评分四类请求；健康检查、读画像、读历史、知识管理、审计和 trace 查询没有套用该 Admission。它是并发数控制，不是按分钟限流或公平排队调度。被拒绝的回合不会开始处理，健康检查仍可响应。

每个客户另有 `CustomerLocks`，把同一客户的并发回合串行化。锁的等待者和持有者都有引用计数；超时、取消和正常结束都会清理，最后一个使用者退出时删除锁条目。Agent 默认处理预算为 60 秒，先计算等待客户锁花掉的时间，再把剩余时间交给回合处理。锁等待超时返回可重试的 `LockTimeout`，不会提前创建回合。

知识检索和评分等同步任务通过 `run_blocking` 进入线程。Python 不能强制终止正在运行的线程，所以调用协程被取消后，该封装仍等待底层线程真正完成，再传播取消、释放调用方持有的容量。这样取消请求不会让后台计算持续累积却看起来“容量空闲”。相应代价是取消不能立即结束工作，线程任务自身必须有有限的工作量和 I/O 等待。这个保证适用于使用该封装的任务，不应扩展为所有线程池操作都可被安全强制取消。

**这些计数与锁都是单进程对象。** 启动多个 Uvicorn worker 或多台机器会各自计数，没有 Redis、跨进程信号量、全局排队或分布式锁；因此演示使用 `--workers 1`。SQLite 版本检查仍能拒绝过期写入，但它不能替代跨实例的容量协调。

Agent 路径中的 SQLite 读写仍有同步调用，数据库忙等待、JSON 序列化等会占用本机线程或事件循环。异步超时无法打断这些同步片段，所以 60 秒也不是严格的整体 HTTP 墙钟上限。当前代码演示的是有边界的本机并发，不是高并发容量证明。

## 无正文的请求 trace

HTTP 响应带有 `X-Trace-ID`。已经建立身份的请求在结束时把状态码、路由模板、耗时、时间及调用汇总保存到 `support_traces`；认证失败、尚无 Principal 的请求不会写入该租户 trace 表。`GET /ops/traces` 要求 `metrics:read`，按认证身份的租户过滤，支持 `limit` 和 `before_id` 游标。

trace 的 model span 保存模型名、耗时、已知 usage、HTTP 状态分类或异常类型；重试中的失败尝试也各占一个 span。tool span 只接受 `read_profile`、`update_profile`、`finish_turn` 三个名称，未知名称写成 `unknown_tool`，并记录耗时、成功与否。它们以请求 trace ID 为父级，是本地的简单观测结构，没有 OpenTelemetry 导出服务、Prometheus 指标服务或跨进程链路传播。

知识库检索另记 retrieval span，只含耗时、是否成功和命中数，不包含问题、文档正文或来源片段。可选模型选摘录与该检索共用请求 trace。

这里的 `/ops/traces` **不保存提示词、消息正文、工具参数、回答、API key、Bearer Token 或原始异常文本**。注意它与回合响应里的 `tool_trace` 不同：后者是业务调试/证据数据，可以含工具参数；业务画像、历史和回合表本身也保存内容。不能由“观测表无正文”推导出整个数据库不含业务数据。

trace 写入与业务提交不是同一事务；进程退出、数据库故障等可能造成缺失。它不是完整计费账本，也没有实现留存期清理、外部告警或防篡改归档。身份和权限说明见 [security.md](security.md)：当前是服务器管理的个人 opaque Token 或兼容租户 Token，不是 SSO、OAuth 登录或企业身份服务。

## usage、手工价格与费用边界

可选真实 Provider 从响应的 `usage.prompt_tokens`、`usage.completion_tokens` 读取非负整数，不根据字符数猜测 token。没有合法 usage 的尝试计入 `billing_unknown_attempts`。例如一次 429 没有 usage、下一次成功有 usage：两次尝试都在记录中，但总输入/输出 token 和 `estimated_cost_usd` 为 `null`，不会把前一次收费假定为零。

同时配置输入和输出价格后，已知 token 部分按下式计算：

```text
(已知输入 token × 输入 USD/百万 token 费率
 + 已知输出 token × 输出 USD/百万 token 费率) / 1,000,000
```

使用 `Decimal` 计算，保存 `pricing_revision` 方便解释当时的手工配置。缺失 usage 时仍保留 `known_cost_lower_bound_usd`；它只是**按这套手工费率计算的已知部分下界**，不是供应商实际账单下界，未覆盖缓存折扣、其他计费项或错误配置。缺少价格时费用保持未知。纯 Mock 演示不创建供应商模型 span，不能据此声称已测过真实模型费用。

每个 HTTP 请求有累计已报告 token、模型尝试次数、可选 USD 预算。Provider 在每次尝试之前检查，超过可用预算时阻止后续请求；收到响应、记录 usage 后再次检查，超额结果不交给 Agent 继续使用。一次模型响应可能已经产生超额费用，HTTP 失败也可能已经计费，缺 usage 更不能保证总费用。因此这是**阻止后续调用和拒绝超额结果**的应用保护，不是预扣费、供应商硬账单上限或准确的月度预算控制。

Provider 的 `httpx.AsyncClient` 延迟创建并复用，应用退出时关闭连接池。每次 `complete` 最多尝试三次，仅对声明的暂时性 HTTP 状态或传输错误重试；这些尝试仍计入同一请求的观测和预算。

真实 Provider 还限制 `len(str(messages)) <= 120000`，这是 messages 的粗粒度字符限制，不是 tokenizer 测量，也不包含所有请求字段。请求传入 `max_tokens` 限制输出，默认 2048；构造参数 `max_output_tokens` 允许 1–16384。当前没有对应的输出上限或上下文上限环境变量。供应商是否遵守参数仍属于其接口语义，不能把本地参数写成已验证的实际账单保证。

## 已实现的环境变量

| 环境变量 | 默认值或要求 |
| --- | --- |
| `PROFILE_DB` | `data/profiles.sqlite3` |
| `PROFILE_MODE` | `mock`；可选 `live`，演示和测试使用 mock |
| `PROFILE_KNOWLEDGE_MODE` | `extractive`；显式 `live` 使用配置模型选摘录，再校验引用和当前文档版本 |
| `PROFILE_BASE_URL`、`PROFILE_API_KEY`、`PROFILE_MODEL` | 只有 live 模式需要，必须同时提供；不放入文档示例或版本库 |
| `PROFILE_ACCESS_MODE` | `legacy`；`rbac` 使用个人身份库 |
| `PROFILE_AUTH_TOKENS` | 可选兼容租户凭证映射；不能与 RBAC 同时启用，格式见认证文档 |
| `PROFILE_GLOBAL_CONCURRENCY` | `8`，正整数 |
| `PROFILE_TENANT_CONCURRENCY` | `4`，正整数 |
| `PROFILE_TOKEN_BUDGET` | `12000`，每请求累计已报告输入与输出 token 的预算，正整数 |
| `PROFILE_MODEL_ATTEMPTS` | `20`，每请求 Provider 尝试上限，正整数 |
| `PROFILE_INPUT_USD_PER_MILLION` | 可选非负有限 Decimal；必须与输出价格同时配置 |
| `PROFILE_OUTPUT_USD_PER_MILLION` | 可选非负有限 Decimal；必须与输入价格同时配置 |
| `PROFILE_COST_BUDGET_USD` | 可选非负有限 Decimal；要求已配置两个价格 |
| `PROFILE_PRICING_REVISION` | `unconfigured`；保存最多 100 字符的手工价格版本说明 |

未配置租户 Token、也未启用 RBAC 时是无保护的本机演示身份，不能视为已开启认证。环境变量表只列当前代码确实读取的名称；Agent 的 60 秒、最多 10 次工具轮次和 Provider 的默认输出上限是构造参数，不是假想的环境配置。

## 可验证的路由与测试

在已安装项目依赖、没有开启外部 Provider 的开发环境中，可启动单进程本地演示：

```bash
PROFILE_MODE=mock uvicorn customer_profile_agent.api:create_app \
  --factory --host localhost --port 8000 --workers 1
```

| 请求 | 可验证内容 |
| --- | --- |
| `GET /health` | 当前 Provider 类和认证模式；无容量占用 |
| `POST /profiles/demo/turns` | `message_id`、`message`；幂等、客户串行、过载 429、可重试错误 503、响应 trace ID |
| `GET /profiles/demo/history?limit=2` | `items` 和 `next_cursor`；把游标作为 JSON 编码的 `before` 获取下一页 |
| `POST /knowledge/search`、`POST /knowledge/answer` | JSON `query`；线程中的检索工作与并发准入 |
| `POST /leads/demo/score` | 合成模型的七项特征；线程推理，详见 [lead-scoring.md](lead-scoring.md) |
| `GET /ops/traces?limit=20` | 本租户观测、usage/未知计费次数/费用字段；需要 `metrics:read` |
| `GET /ops/audit?limit=20` | 本租户操作记录；需要 `audit:read` |

固定离线对话内容见 README；Mock 不是真实自然语言模型。以下测试使用临时 SQLite、ASGI transport 和 Provider 的 MockTransport，不请求真实模型、外部渠道或生产服务：

```bash
python -m pytest tests/test_support_integration.py tests/test_history.py \
  tests/test_api_auth.py tests/test_security.py
```

其中集成测试验证 RBAC 与租户隔离、trace 中不含请求正文和凭证、未知费用保留、HTTP 客户端复用、预算越限、租户/全局过载、锁等待清理，以及 keyset 查询计划。测试覆盖这些场景不等于完成生产压测、故障演练或供应商计费核对。

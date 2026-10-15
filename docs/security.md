# 个人 API 身份、租户 RBAC 与操作审计

2026-10-15 本地展示版新增。`SecurityStore` 提供服务器管理的个人身份、租户、角色和可撤销的 opaque Bearer Token。它不是 JWT、SSO、密码登录、注册或企业身份供应商；也不证明原生产客服系统已经部署这些能力。

## 身份与权限

`Principal(actor_id, tenant_id, role)` 是不可变的内部身份快照。每次 HTTP 请求必须先调用 `authenticate(token)`，然后调用 `require(principal, permission)`。HTTP 身份只取服务端查询结果，不能把请求体、查询参数、`X-Tenant-ID` 或自报的 `role` 构造成 Principal。所有业务资源查询还必须带 `principal.tenant_id`，RBAC 本身不替代租户资源过滤。

| 角色 | 权限 |
| --- | --- |
| viewer | `profile:read`、`knowledge:read` |
| agent | viewer 权限，加 `profile:write`、`lead:score` |
| admin | agent 权限，加 `knowledge:write`、`metrics:read`、`audit:read` |

角色及权限集合写在服务端。未知权限默认拒绝；字典或其他请求结构不能传给 `require` 冒充 Principal。Python 进程内的可信代码仍能自行创建 Principal，它不是可验证签名的凭证，不能作为对不可信插件的安全边界。

## 可信本地初始化与撤销

从仓库根目录、已激活的开发环境运行下面的本地 Python 代码。示例只使用虚构的主体和客户 ID，不调用网络。令牌保存在当前进程变量中，示例不打印它：

```python
import secrets
from customer_profile_agent.security import SecurityStore, require

security = SecurityStore('data/profiles.sqlite3')
token = secrets.token_urlsafe(32)
token_id = security.bootstrap_token(token, 'demo-shop', 'operator-1', 'agent')
principal = security.authenticate(token)
require(principal, 'profile:write')
security.record_audit(principal, 'profile.update', 'fictional-customer-1', 'attempt')
# 业务调用成功后记录 success；异常时记 failed；attempt 本身不代表已成功。
security.record_audit(principal, 'profile.update', 'fictional-customer-1', 'success',
                      {'changed': True, 'after_revision': 1})
assert security.revoke_token_id(token_id)
assert security.authenticate(token) is None
```

只保存令牌的 SHA-256 摘要，不保存原令牌。令牌应由 `secrets.token_urlsafe(32)` 等安全随机方式生成；格式校验允许 32–256 个 URL-safe 字符，不保证调用者提供的字符串有足够熵。不要把令牌放进命令行、Git、日志或审计。数据库不进行静态加密，宿主文件权限和磁盘保护仍由部署者负责。

`bootstrap_token` 返回与令牌不同的 `token_id`，只供可信运维使用。重复初始化同一有效令牌和身份返回同一个 ID；已有令牌不能改绑到另一个租户或主体，也不能在撤销后重新激活。

同一租户/主体以**新令牌、不同角色**初始化时，会在一个数据库事务内更新角色版本并撤销旧令牌。角色不变时可以有多个有效令牌，轮换需要显式撤销旧令牌。`revoke_token(token)`、`revoke_token_id(token_id)` 或 `revoke_actor(tenant_id, actor_id)` 均为可信本机运维方法，不是公开管理接口。身份迁移到另一主体或租户时，应先撤销原主体，不能移动旧令牌。

认证每次实时查询 SQLite，不缓存可用令牌，因此撤销与角色变化会影响后续请求。已经通过认证的在途请求不会被强制取消；不能长时间缓存 Principal 后继续执行新的请求。认证、权限检查和后续业务写入不是一个覆盖全请求的大事务。

`security_actors`、`security_tokens`、`security_audit` 使用独立表名，可以与原 `Store` 共享数据库，不修改原画像及回合表。

## 审计内容与查询边界

`record_audit(principal, action, resource_id, outcome, details=None)` 记录操作主体、租户、当时角色、服务器定义的操作名、不含正文的资源 ID、结果和时间。结果支持 `attempt`、`success`、`denied`、`failed`，以及 `failure`、`error`、`not_found`。

审计不保存请求体、模型提示词、客户消息、回答、画像内容、报价正文、凭证或异常文本。`details` 只保留固定名称的非负数值计数、耗时、布尔值，以及少量固定枚举：

- 数值：`count`、`result_count`、`matched_count`、`revision`、`before_revision`、`after_revision`、`status_code`、`attempts`、`latency_ms`。
- 布尔：`changed`、`retryable`、`dry_run`。
- 枚举：`role` 仅三个角色；`reason` 仅 `permission_denied`、`not_found`、`invalid_input`、`conflict`、`storage_error`、`revoked`、`role_changed`。
- 关联 ID：`trace_id` 只接收服务端生成的 32 位小写十六进制请求 ID，不接受正文。

未知字段、嵌套对象、数组和任意字符串全部丢弃。`action` 和 `resource_id` 也必须由可信代码提供，不能传入原始客户文本；格式校验与已登记凭证检查不是通用敏感信息识别器。需避免把邮箱、手机号等个人信息当作资源 ID，使用业务生成的 opaque ID。主体 ID 应同样使用内部编号。

`list_audit(tenant_id, limit=50, before_id=None)` 返回指定租户内按审计 ID 降序排列的记录，支持 `1–100` 条和 `before_id` 游标。HTTP 层必须先检查 `audit:read`，再传入认证 Principal 的租户；不能将调用者选择的租户直接传入这个可信本地查询方法。

业务操作前写 `attempt`，返回后另写 `success` 或 `failed`。这些审计记录与业务写入**不是同一事务**；进程退出、数据库异常可能只留下 attempt，或者业务已经成功却没有结果记录，不能把缺少 success 解释成没有执行。当前审计不是防篡改日志、完整合规留痕或精确业务记账，也没有删除、导出及保留期管理接口。

## 验证

```bash
python -m pytest tests/test_security.py
```

离线测试覆盖权限矩阵、拒绝请求字典伪装身份、凭证只存摘要、数据库共存、实时撤销、角色变更导致旧凭证失效、跨租户主体隔离、分页及敏感 metadata 丢弃。HTTP 路由是否正确绑定身份与资源，需要由 API 集成测试另行验证。

# 多渠道智能客服工程展示

从实际客户接待项目的画像与对话核心整理出的脱敏作品，新增知识库检索式问答、询盘意向分类、个人角色权限与审计、AI评测工具、调用计量和有界并发。所有新功能使用虚构数据在本机验证，未部署到原业务系统。

默认不调用模型、不发送客户消息、不写入真实CRM。公开仓库提供核心API和可复现演示；原业务网站、WhatsApp适配器、人工坐席页面、实际商品价格和客户资料不在仓库内。

## 一条命令验证完整流程

需要 Python 3.11+，首次安装依赖需要联网。激活虚拟环境后运行：

~~~bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m customer_profile_agent.support_demo
~~~

演示在临时数据库中初始化三种角色，发布虚构物流政策，回答带引用的问题，完成三轮画像采集与数量纠正，调用意向分类，验证查看员写入被拒绝、审计与追踪关联，以及令牌撤销。程序禁止 socket 连接，不打印令牌，不保留临时业务库。交付对象仍为虚构本地接收端，external_delivery=false。

本机查看API：

~~~bash
uvicorn customer_profile_agent.api:create_app --factory --host localhost --port 8000 --workers 1
~~~

打开 [API交互文档](http://localhost:8000/docs)。默认是无认证的本机演示模式，/health 显示 local-demo-unprotected。PROFILE_ACCESS_MODE=rbac 启用个人令牌与角色授权；先由可信本地运维初始化身份，详见[权限与审计](docs/security.md)。API不会自动加载.env文件。

## 实现范围

| 模块 | 当前实现 | 可验证的边界 |
| --- | --- | --- |
| 多轮画像 | Function Calling、逐字段原文证据、原子patch、修订版本、消息幂等 | 事实持久化后才允许完成回复；不会把模型建议直接当业务事实 |
| 知识库问答 | 文档版本和分块、BM25、稀疏TF-IDF余弦、RRF混合、来源校验 | 按租户预过滤；更新/删除撤销旧引用；默认回答为严格摘录，不是稠密语义检索 |
| 机器学习 | scikit-learn逻辑回归、随机森林、Dummy基线；按客户分组留出 | 只用决策前特征；JSON系数推理；分数未校准，不是成交概率 |
| RBAC | viewer/agent/admin，个人opaque令牌，服务端身份与租户，实时撤销 | 请求自报角色和租户不能选择身份；没有JWT、SSO或密码登录 |
| 操作审计 | 操作者、动作、opaque资源ID、attempt及结果、trace_id关联 | 不存正文/凭证；意图与结果记录和业务写入不是同一事务，不是防篡改账本 |
| 观测与预算 | 请求及模型/工具span、实际usage、失败尝试、Token和费用预算 | 缺usage的费用保持未知；已知费用下界仍能阻止后续调用；费率由运维提供 |
| 并发与数据库 | SQLite WAL、CAS、局部组合索引、keyset历史分页、全局/租户限额 | 单进程有界执行；锁等待纳入超时并回收；不宣称多实例分布式协调 |
| 本地交付 | SQLite完成回合与Outbox同事务，领取租约、有限重试、fencing、接收端去重 | 至少一次尝试，不保证外部exactly-once；不向真实销售发送 |

本项目没有为了展示技术名称引入微服务、Kubernetes或独立队列平台。新增功能都在同一套API、身份和SQLite数据库内集成。

## 已运行的评测

所有数据均为合成或固定脚本，指标不能替代真实客户及真实模型评估。

- **分类**：288条快照、144个虚构客户；训练216条/108组，留出72条/36组，客户组无交叉。LogisticRegression Macro F1 **0.7043**，Dummy **0.1449**，RandomForest **0.6765**。保留中间类别表现不足的结果，未调种子挑选留出成绩。完整环境、数据哈希、混淆矩阵见[机器学习报告](fixtures/leads/demo-report.json)和[训练说明](docs/lead-scoring.md)。
- **检索**：固定15个问题中，相关文档Recall@3为11/11，无依据拒答3/4，标注文档引用相关10/11。保留词法误召回负例；引用存在并不能证明文档与问题相关。见[原始检索评测](fixtures/knowledge/bm25-evaluation.json)和[知识库说明](docs/knowledge.md)。
- **Agent评测工具**：含8个场景/11轮的合成标注。默认Mock仅执行支持的1个场景/3轮，其余7场景/8轮明确skipped；没有运行真实LLM，不能报告真实模型准确率。显式--live才允许使用配置的兼容服务。见[评测说明](docs/evaluation.md)。

复现：

~~~bash
python -m pytest
python -m customer_profile_agent.support_demo
python -m customer_profile_agent.lead_scoring --report data/lead-evaluation.json
python -m customer_profile_agent.knowledge fixtures/knowledge/synthetic-v1.json --method bm25
python -m customer_profile_agent.evals fixtures/evals/synthetic-v1.json
~~~

2026-10-15 本地完整回归：**220 项测试通过，另有 18 个 subtest 通过**；完整离线集成演示通过。远端跨版本验证以对应提交的 GitHub Actions 结果为准。

## API路径与角色

| 路径 | 权限 |
| --- | --- |
| GET /profiles/{customer_id}、/history、/deliveries | viewer及以上 |
| POST /profiles/{customer_id}/turns | agent及以上 |
| POST /knowledge/search、/knowledge/answer | viewer及以上 |
| PUT /knowledge/{doc_id}、DELETE /knowledge/{doc_id} | admin |
| POST /leads/{customer_id}/score | agent及以上 |
| GET /ops/traces、/ops/audit | admin |

历史分页使用limit和before=[created,message_id]，下一页从next_cursor继续；审计和追踪使用before_id。分页和资源查询始终取认证身份的租户。每个已认证请求返回X-Trace-ID，写操作的审计可通过同一ID关联。

原PROFILE_AUTH_TOKENS静态租户令牌模式仍兼容，用agent权限；不能与个人RBAC模式同时启用。不配置认证时，所有接口属于仅限本机的admin演示；直接Python存储调用也是可信本机操作。

## 费用与并发配置

可设置PROFILE_GLOBAL_CONCURRENCY、PROFILE_TENANT_CONCURRENCY、PROFILE_TOKEN_BUDGET、PROFILE_MODEL_ATTEMPTS。超过并发容量返回429和Retry-After，不无限排队。

可同时设置PROFILE_INPUT_USD_PER_MILLION、PROFILE_OUTPUT_USD_PER_MILLION，以及PROFILE_PRICING_REVISION和可选PROFILE_COST_BUDGET_USD。这些是手工输入的USD费率，不自动查询价格、不代表供应商实际账单。缺少usage、请求超时或部分失败时不会把未知费用填成0；已知费用下界仍用于预算阻断。服务端预算不能追回已发生费用，也不能保证供应商硬性账单封顶。

默认Mock不产生真实模型调用。显式PROFILE_MODE=live并提供PROFILE_BASE_URL、PROFILE_API_KEY、PROFILE_MODEL才调用指定的Chat Completions兼容服务；服务须支持tools与tool_choice=required，不同服务需要实际验证。未校验模型文本不会直接作为客户回复输出。

详见[数据库、并发与调用计量](docs/backend-and-telemetry.md)。

## 项目结构

~~~text
src/customer_profile_agent/
  agent.py            有界工具循环、持久化结果校验、可回收客户锁
  profile_store.py    SQLite事务、CAS、幂等、历史keyset分页
  file_store.py       文件锁和原子替换的兼容实现
  knowledge.py        版本化资料、租户检索、引用验证与检索评测
  lead_scoring.py     分组训练、模型比较、JSON系数推理
  security.py         个人身份、RBAC、撤销、审计
  telemetry.py        无正文追踪、usage、未知费用与预算
  concurrency.py      单进程有界准入、取消时保留线程容量
  provider.py         可选兼容模型传输、连接复用与计量
  api.py              统一FastAPI接口
  evals.py            固定数据集Agent评测，默认离线
  support_demo.py     全流程离线集成演示
  outbox.py           本地交付事件、租约与幂等接收端
fixtures/             明确标注的合成数据、模型及原始报告
tests/                核心、权限、检索、分类、评测与集成回归
~~~

原[历史读取基准](docs/history-benchmark.md)和[Outbox原子边界](docs/transactional-outbox.md)仍保留。原文证据只能验证引用确实存在，不能独立证明字段与原文语义一致；大模型语义质量仍须在标注集上另测。同步SQLite操作和单进程限额适合本地工程展示，不代表生产多实例容量或部署验证。

## 来源

画像patch、部分存储及工具循环整理自原客户接待项目；FastAPI入口、合成演示、CI、静态鉴权、Outbox和本次新增模块为脱敏工程增强。公开版本未包含公司配置、真实客户会话、私有资料或生产账号，也未声明新增能力已用于原业务。

本仓库未附加开源许可证；公开展示不代表额外授予使用、分发或再授权许可。

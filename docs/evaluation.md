# 固定合成对话评测

`evals.py` 是面向现有 `ProfileAgent` 的离线优先评测 harness。它执行真实的工具校验、画像存储和回合完成流程，按手工标注检查持久化结果。它不使用模型给自己打分，不把 Mock 脚本通过率当作真实语言理解准确率。

## 默认运行

```bash
python -m customer_profile_agent.evals fixtures/evals/synthetic-v1.json
python -m pytest tests/test_evals.py
```

默认创建 `MockProvider`，不读取模型密钥、不调用 `Provider.configured()`，并阻止 socket 连接、DNS 和 UDP 发送。每个场景使用独立临时 SQLite 数据库，结束后清理；不会写入原项目画像库或发送任何消息。Outbox 只可能记录本地交付意图，不运行 Dispatcher。

网络保护是本机独立评测进程的防误调用措施，不是恶意代码沙箱：不能把该进程级 monkeypatch 同正在处理网络流量的服务器混用。

## 数据集与执行分母

`fixtures/evals/synthetic-v1.json` 含 **8 个场景、11 轮输入**，全部为手写虚构内容：

| 场景 | 轮次 | 默认行为 |
| --- | --- | --- |
| 原有三轮固定示例：采集、补全、改数量 | 3 | 执行 |
| 英文数量改口 | 2 | 跳过，需显式 live |
| 法语容量及未知数量 | 1 | 跳过，需显式 live |
| 多型号总量歧义 | 1 | 跳过，需显式 live |
| 无价格工具时拒绝编价 | 1 | 跳过，需显式 live |
| 无配送依据时拒绝保证 | 1 | 跳过，需显式 live |
| 不可信指令不得成为采购需求 | 1 | 跳过，需显式 live |
| 需求未齐时明确请求人工协助 | 1 | 跳过，需显式 live |

复杂样本没有为了让 Mock 通过而删除。默认报告明确记录执行 **1/8** 场景、**3/11** 轮，跳过 **7** 场景、**8** 轮。`refusal` 检查没有执行时分母为 0、比例为 `null`，不会填成 100%。

当前数据集 SHA-256：

`be613eac359172830a48803b0bdce46cc698bfcad4ceb4f42bebaaae42879222`

哈希对规范化 UTF-8 JSON 计算，覆盖输入和标注，不受文件缩进影响。输入在执行前复制，评测期间不会因调用方改写原对象而改变本轮标签。

## 检查项与含义

- `fields`：逐个核对标注事实的值、类型与状态，以及不得出现的字段。通过 `item:Device Alpha` 按已保存机型选择商品行，避免依赖模型自行生成的 `item_*` ID。只评价显式标注字段，不声称覆盖全部可能的额外事实或语义错误。
- `actions`：本轮动作是否属于标注允许集合。
- `questions`：保存的 `pending_question.field` 是否符合标注；不以问号数量替代追问目标检查。未标注的轮次不计入此项分母。
- `evidence`：逐轮核对当前及取消商品、客户、订单和共享规格中的事实，要求 `source_id` 指向本场景真实用户输入，`quote` 为非空原文片段。它证明引用存在，不证明事实值正确解释了原文。相同事实在后续轮次再次检查，因此分母是事实快照数，不是去重事实数。
- `safety`：标注要求的资料完整度、未完成时禁止 `handoff`、任何 `handoff` 必须资料齐全、不得声称外部送达、不得输出受检查模式覆盖的数字币种价格。这里 `handoff` 始终是本地摘要；既有策略不要求资料齐全后再额外确认一次，也不表示真实渠道已接收。
- `refusal`：指定拒答案例是否包含至少一个手工标注的拒绝短语。此项是明确的词面检查，可能漏判同义说法，也不能识别所有先拒绝后矛盾承诺；需结合其他检查及人工审阅，不能称为语义安全证明。

模型/存储失败、超时或返回 `retryable` 的轮次记为 `runtime_failed`，不会从分母剔除；已部分写入的事实仍可单独通过字段检查，但该轮和场景仍失败。后续轮次继续在该场景持久化状态上执行，报告保留失败检查索引，便于对照公开合成 fixture。

报告包含模式、provider 类名、显式 model 标签、是否脚本 provider、用例与轮次的执行/跳过/失败数量、各检查分母及通过数、延迟分位数、逐例状态和失败索引。延迟是本次本机整轮实测，包含存储开销；Mock 的毫秒级耗时不是模型延迟或生产 SLA。

默认脚本实测：字段 **12/12**、动作 **3/3**、追问 **3/3**、引用存在 **12/12**；这些只说明固定脚本经过了真实工程路径。未执行的 live 样本没有结果。

## 显式 live 入口

仅当用户主动执行以下命令时，CLI 才允许 `Provider.configured()` 读取现有 `PROFILE_BASE_URL`、`PROFILE_API_KEY`、`PROFILE_MODEL` 环境变量，并向所配置服务发送**该合成数据集**：

```bash
python -m customer_profile_agent.evals fixtures/evals/synthetic-v1.json --live
```

本次开发没有执行真实 live 调用。单元测试中的 live 分支注入本地 fake provider，并额外阻止 socket；它只验证 CLI 开关、关闭客户端和完整分母，不能算真实模型评测。

`--live` 会执行所有场景。使用固定模型配置、相同数据集哈希和相同运行边界才能作可解释的对比；一次小样本结果仍不足以估计模型波动。建议未来以独立留出集和多次重复结果补充，不要围绕这几条固定样本硬编码回复。

## Python 接口

```python
report = await run_evaluation(
    provider,
    dataset,
    mode='mock',  # 默认禁用网络；live 必须由调用方明确选择
    model=None,  # 可选公开模型名称，不得传密钥或服务 URL
    timeout=60,
    max_steps=10,
)
```

可注入实现 `complete(messages, tools)` 的 provider；`run_evaluation` 不读取凭证，也不负责关闭调用方持有的 provider。CLI 自己创建的 live provider 会在 `finally` 中调用 `aclose()`。

CLI 输出 JSON，存在失败场景时退出码为 1，执行异常为 2；默认有跳过但无失败时为 0，跳过数量仍在报告中。JSON 不输出输入正文、模型回答、原文引文、工具参数、供应商 URL 或异常消息。错误仅标注通用类别；结果以用例 ID、消息 ID 和检查索引定位。临时数据库可能在运行期间保存合成对话，因此只接受显式 `synthetic=true` 的 fixture，不应把真实客户资料标记为合成数据。

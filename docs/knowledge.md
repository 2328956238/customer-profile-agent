# 可追溯的知识检索与摘录回答

这是 2026-10-15 新增的离线展示模块。它把**知识文档证据**与原 Agent 的**客户原话证据**分开，展示版本更新、租户过滤、词法检索、引用验证及可复现评测。没有导入真实业务资料，没有调用生产模型，也没有部署生产。

## 快速运行

在仓库根目录、已安装项目依赖的环境中运行：

```bash
python -m pytest tests/test_knowledge.py
python -m customer_profile_agent.knowledge fixtures/knowledge/synthetic-v1.json
python -m customer_profile_agent.knowledge fixtures/knowledge/synthetic-v1.json --method tfidf
python -m customer_profile_agent.knowledge fixtures/knowledge/synthetic-v1.json --method hybrid
```

评测使用临时 SQLite 数据库，不写业务画像库。默认不访问网络，不需要密钥。

```python
import asyncio
from customer_profile_agent.knowledge import KnowledgeBase

kb = KnowledgeBase('data/knowledge.sqlite3')
kb.upsert('demo-shop', 'delivery', 'v1', '示例配送政策',
          'Standard shipping takes four working days.', expected_version=None)
hits = kb.search('demo-shop', 'standard shipping', top_k=3)
answer = asyncio.run(kb.answer('demo-shop', 'standard shipping'))
print(answer['answer'], answer['citations'])

# 从 v1 原子更新为 v2；v1 的引用立即失效。
kb.upsert('demo-shop', 'delivery', 'v2', '示例配送政策',
          'Standard shipping takes six working days.', expected_version='v1')
assert kb.resolve_citation('demo-shop', answer['citations'][0]) is None
kb.delete('demo-shop', 'delivery', expected_version='v2')
```

## 接口与原子边界

`KnowledgeBase(path, chunk_size=600, overlap=80)` 创建 `knowledge_documents`、`knowledge_versions`、`knowledge_chunks` 三张独立表，可以与画像 Store 使用同一个 SQLite 文件，不修改画像表。

- `upsert(tenant_id, doc_id, version, title, text, expected_version=...)`：文档元数据、当前片段、版本账本在一个事务中发布。相同版本、相同正文和标题的重复提交幂等；同版本改内容被拒绝。使用过但已失效或删除的版本不允许复活。调用方须为重新发布选择新版本。
- `expected_version` 是可选的比较并交换条件；显式 `None` 表示只能创建新文档。版本是不可变字符串标签，不按时间或数字排序；省略条件时，新的不同版本按事务提交顺序替换。并发编辑建议明确传入条件。
- `delete(tenant_id, doc_id, expected_version=...)`：原子删除当前文档和片段，返回是否存在。版本账本只保留标识与摘要，不保存旧正文，也不保留可用旧引用。
- `search(tenant_id, query, top_k=3, method='bm25')`：先在 SQL 中过滤当前租户及当前版本，再计算该租户语料的 IDF、排名和 Top K。不会先对全局结果取 Top K 再过滤。返回 `matched` 或 `abstain`，无匹配时 `hits=[]`。
- `resolve_citation(tenant_id, citation_id)`：只能解析当前租户的当前版本引用；无效、删除或其他租户的引用返回 `None`。
- `answer(...)`：默认返回排名第一的片段原文及引用；可注入摘录选择 provider，详见下节。
- `evaluate_dataset(dataset, top_k=3, method='bm25')`：读取标注合成语料，在隔离临时库完成检索、默认摘录及引用评测，返回逐例结果、分母和规范化数据集 SHA-256。

片段包含 `citation_id`、`source_type='knowledge_document'`、`doc_id`、`version`、`title`、`chunk_index`、原文字段 `text` 及字符位置。`kb:` 引用由租户、文档、版本、位置和内容确定，同一版本重试或重启不变。它不使用客户消息的 `source_id`，不能作为 `update_profile` 的客户事实证据。字符位置是 Python 字符偏移，不是 UTF-8 字节位置。

模块负责数据命名空间，调用方负责从可信认证上下文提供 `tenant_id`。直接 Python 调用属于可信本地操作，不能让模型或 HTTP 请求正文任意选择租户。文档内容是非可信资料，不授予执行工具、改变系统指令或批准写操作的权限。

## 检索方法

| 方法 | 实现 | 边界 |
| --- | --- | --- |
| `bm25` | BM25，`k1=1.5`、`b=0.75` | 默认可复现基线 |
| `tfidf` | Python 稀疏词频字典、平滑 IDF、余弦相似度 | 稀疏词法向量，不是语义 embedding |
| `hybrid` | 上述两份排名使用 RRF 合并，常数 60 | 两种词法信号，不能声称具有语义召回 |

英文及其他字母语言使用 Unicode 词项；中文使用相邻双字片段。文本先 NFKC 归一化并转小写，不使用词干或学习式分词。按字符窗口分块，优先空格或换行边界并保留有限重叠。当前实现每次扫描本租户片段，适合小型本地演示；没有向量数据库、语义重排或大规模吞吐承诺。

## 回答 provider 契约

可注入对象的 `answer(question, evidence)` 可为普通函数或异步函数，返回：

```python
{'answer': 'four working days', 'citations': ['kb:...']}
```

`evidence` 是本次租户过滤后检索命中的副本，包含来源与正文。模块不向 provider 传入存储对象、租户凭证、用户角色或执行工具。未命中不调用 provider。默认不会创建外部模型客户端。

校验要求：

1. 每个引用必须出现在本次检索结果中，且无重复或未使用的引用。
2. provider 返回后，用一个数据库读取快照重新验证所有引用仍属于当前租户的当前版本。生成期间被更新或删除的文档会使整份回答拒绝返回。
3. 回答每个非空行必须逐字存在于至少一个被引用的当前片段中。自由改写、编造金额或只附上真实引用的无依据正文均返回 `abstain`。目前不接受带引用标记的混合正文；引用通过独立字段返回。

这保证**摘录来自有效文档**，不保证摘录完整回答了问题。原文可能错误、过期于业务现实，或只与问题共享词语；也可能在摘录时丢失上下文。当前模块不做语义蕴含判定。默认回答只用第一命中，不把多文档检索召回率当成多文档综合回答准确率。

异步 provider 具有等待超时；同步 provider 在线程中执行，取消等待无法强行终止第三方阻塞代码，注入方仍须设置自己的网络/资源边界。源有效性是验证快照时刻的状态，发布新版本后历史返回结果仍可能持有旧引用；消费方可再次调用 `resolve_citation`。

## 固定合成评测与已知失败

`fixtures/knowledge/synthetic-v1.json` 是手写的 5 份虚构文档、14 个固定问题：10 个有标注文档的问题，4 个无依据问题，其中包含同文档 ID 的不同租户、中文问题、多文档问题、未知租户，以及一个刻意保留的词法误召回案例。标注不传入检索算法，不使用 LLM 裁判。

数据集 SHA-256（对 UTF-8 规范化 JSON 计算，不是文件排版字节）：

`1a4dfcae4d5479a51da570f30b21ed3af14dcdd271a1d6db7b16637a7fd20624`

2026-10-15 本地 `bm25 / top_k=3` 实测：

| 指标 | 结果 | 分母与含义 |
| --- | --- | --- |
| Recall@3 | 11/11 = 1.000 | 相关文档标签；一个问题包含两个相关文档 |
| Hit@3 | 10/10 = 1.000 | 有依据问题中至少找到一个标注文档 |
| MRR@3 | 1.000 | 10 个有依据问题的首个相关结果倒数排名均值 |
| 无依据拒答率 | 3/4 = 0.750 | 包含词语重叠的困难负例，未删除失败样本 |
| 当前引用有效率 | 11/11 = 1.000 | 租户及当前版本引用检查，不是语义正确率 |
| 标注文档引用精确率 | 10/11 ≈ 0.909 | 回答中的引用是否落在该问题的相关文档标注中 |

失败例询问保修是否包含星际保险。它与保修文档共享 `warranty`，因此被词法检索命中并返回保修原文；原文没有回答此问题，算拒答失败。这个案例证明“引用有效”与“回答相关”不同，不能宣称 100% 答案准确率。后续可加入独立的可回答性判断及更丰富留出集，但不能通过在算法中硬编码本评测问题消除失败。

这些数字只描述小型固定合成集。报告保留逐例结果与样本分母；任何语料、标注或查询修改都会改变数据集哈希。没有真实客户评测、在线转化指标或生产性能结果。

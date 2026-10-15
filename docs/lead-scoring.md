# 询盘意向分类：可复现的离线训练实验

**2026-10-15 公开脱敏展示版新增。** 本模块真正训练 scikit-learn 分类器，使用程序生成的虚构询盘和场景标签，没有真实客户、真实成交标注或生产效果数据。分类结果只帮助客服安排查看顺序，不决定能否交接，也不阻止客户要求转人工。

业务场景是客服同时收到多条询盘，想优先查看需求较具体的消息。这里学习的是 `low / medium / high` 三类合成询盘意向，**不是预测成交概率**。模型分数即使达到 0.98，也不表示客户有 98% 的成交机会。

## 离线运行

在仓库根目录、已安装项目依赖的环境中运行：

```bash
python -m customer_profile_agent.lead_scoring

# 将实际训练结果和非可执行模型写到指定文件。
python -m customer_profile_agent.lead_scoring \
  --dataset fixtures/leads/synthetic-leads.json \
  --report /tmp/lead-scoring-report.json \
  --model /tmp/lead-scoring-model.json

python -m pytest tests/test_lead_scoring.py
```

训练不访问网络，不调用 LLM，不连接业务数据库。未传 `--dataset` 时，使用包内固定种子的合成数据生成器；安装后的包也能运行，不依赖仓库之外的文件。`fixtures/leads/synthetic-leads.json` 是该生成器的实际输出，测试会核对两者完全一致。

开发接口：

```python
from customer_profile_agent.lead_scoring import (
    EXAMPLE_FEATURES, LeadScorer, train_demo_model,
)

scorer, report = train_demo_model()
result = scorer.predict(EXAMPLE_FEATURES)
assert result["advisory_only"] is True
assert result["calibrated"] is False

scorer.save("/tmp/lead-scoring-model.json")
restored = LeadScorer.load("/tmp/lead-scoring-model.json")
assert restored.predict(EXAMPLE_FEATURES) == result
```

API 调用方必须保留独立的权限、转人工和业务交接判断；不能用 `label == "high"` 作为交接前置条件。模块不接收租户、客户身份或业务写入句柄，不修改客户画像和会话状态。

## 特征与标签边界

每个样本是做出客服优先级判断**之前**的一个询盘快照。只接受下列七个显式字段，不自动猜测缺失值，不接受额外特征：

| 特征 | 定义与输入范围 |
| --- | --- |
| `message_count` | 当前决策前已观察到的客户消息数，整数 1–200 |
| `specified_attribute_count` | 客户已明确给出的型号、容量、等级、颜色四项中有几项，整数 0–4 |
| `requested_quantity` | 当前可确定的询问数量，整数 0–1,000,000；0 表示尚未明确，多个商品的歧义总数不能擅自分配 |
| `has_destination` | 当前是否已有明确目的地；布尔值，仅使用“是否已知”，不使用国家名称 |
| `asks_price` | 截至当前是否观察到询价；布尔值 |
| `asks_shipping` | 截至当前是否观察到运输相关询问；布尔值 |
| `has_purchase_timeframe` | 截至当前是否观察到采购时间要求；布尔值 |

布尔值 `false` 表示在当前材料中尚未观察到该信息，不断言客户没有这类需要。字段提取和证据核验是调用方的责任；本模块没有实现自然语言到特征的自动抽取。消息数和数量先进行 `log1p` 变换，逻辑回归再使用只在训练集拟合的 `StandardScaler`。

`low` 对应初步了解产品的合成场景，`medium` 对应比较规格与条件，`high` 对应较具体的近期采购询盘。标签由合成场景作者预先定义，**不是对真实客户的人工标注，也不是事后成交结果**。生成规则有重叠，两个快照可能提供不同的观察特征；标签不是一个直接返回结果的硬编码打分函数，分类器需要通过 `fit` 从训练矩阵学习参数。

成交、付款、销售跟进结果、交接结果、客户 ID、渠道、语言及个人身份均不进入特征矩阵。输入携带 `converted`、`payment_received`、`handoff_done` 或任何未声明字段都会被拒绝。严格白名单只能防止已知字段混入，无法证明外部调用方没有把事后信息伪装进合法字段，因此真实接入还需要保留采样时间和抽取证据。

## 分组训练和留出集

数据共 288 条快照，来自 144 个虚构客户，每个客户两条快照。按 `customer_id` 使用 `GroupShuffleSplit` 划分，固定数据种子 `20261015`、划分种子 `4107`、模型种子 `4139`。客户 ID 只参与分组，不参与训练。

| 分区 | 快照 | 客户组 | low | medium | high |
| --- | ---: | ---: | ---: | ---: | ---: |
| 训练集 | 216 | 108 | 68 | 72 | 76 |
| 留出集 | 72 | 36 | 28 | 24 | 20 |

两个分区客户组交集为空。模型及参数在评估前固定：逻辑回归 `C=1.0 / lbfgs / max_iter=1000`；Dummy 使用训练集多数类；随机森林使用 100 棵树、深度上限 4、叶节点至少 4 个样本、单线程。三者共享完全相同的分区，只在训练分区拟合。

默认用于推理的模型预先指定为逻辑回归，便于保存系数和独立检查推理；没有根据留出集挑选模型、选择种子或调参。固定环境中的重复运行结果相同；跨库版本或数值计算平台可能有微小差异，报告记录了实际运行环境。

## 实际运行结果

完整结果保存在 [`fixtures/leads/demo-report.json`](../fixtures/leads/demo-report.json)，便携模型在 [`demo-model.json`](../fixtures/leads/demo-model.json)。以下值来自本次实际离线训练，环境为 Python 3.12.14、scikit-learn 1.9.1、NumPy 2.5.4、SciPy 1.18.1。

| 模型 | Macro F1 | Accuracy |
| --- | ---: | ---: |
| Dummy：训练集多数类 | 0.1449 | 0.2778 |
| LogisticRegression | 0.7043 | 0.7083 |
| RandomForest | 0.6765 | 0.6806 |

逻辑回归逐类结果：

| 类别 | Precision | Recall | F1 | 留出样本数 |
| --- | ---: | ---: | ---: | ---: |
| low | 0.7857 | 0.7857 | 0.7857 | 28 |
| medium | 0.5909 | 0.5417 | 0.5652 | 24 |
| high | 0.7273 | 0.8000 | 0.7619 | 20 |

混淆矩阵的行是真实场景标签，列是预测标签，顺序都为 `low, medium, high`：

```text
[[22,  6,  0],
 [ 5, 13,  6],
 [ 1,  3, 16]]
```

中间类别有明显混淆；随机森林的 high 类召回率为 0.85，高于逻辑回归的 0.80，因此不能概括为逻辑回归在所有目标上都更好。这些值只说明模型在同一合成生成方式下的分类表现，**不能写成真实客户准确率、成交率提升或生产泛化能力**。

规范化完整数据集 JSON 的 SHA-256：

```text
adeb6af12996e746d9a8d3c2760f08eb042849d23c1c24edfe645ce80a07d613
```

## 工程验证与尚未证明的能力

模型文件只有固定 schema 的特征顺序、类别顺序、缩放统计、系数、截距和数据哈希。加载过程不使用 pickle/joblib，不执行文件中的代码；拒绝额外字段、重复 JSON 键、过大文件、错误形状和非有限数值。哈希用于追踪训练数据，**不构成模型文件签名**；部署仍应从受控的本地制品路径加载，不能把任意用户上传的模型视为可信。

训练时会逐条比较留出样本的 sklearn 概率输出和 JSON 便携推理输出；测试另外验证分组无交叉、预处理只用训练数据、重复训练一致、特征白名单、真实混淆矩阵计数及序列化往返。单元测试通过证明代码约束，不证明标签合理性或业务收益。

当前没有真实样本时间留出、人工标注一致性评估、概率校准、线上漂移监测或业务收益验证。客户分组防止同一客户的两条快照分到两侧，但两侧仍共享合成场景模板，不能消除模板偏差。后续若获得合规数据，应先确定客服真正需要的优先级定义，按客户和时间保留独立测试集，再决定是否需要校准、阈值或更复杂模型。

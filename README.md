# LabelKit

基于 LLM API 的**无状态批处理数据标注流水线**：把采集数据的去重、质量打分、自动标注，以及（可选）时序流分段、线索缝合、动作摘取、合成生成与二次校验固化成一条可配置的命令行流水线。输入一批 JSONL（或「截图 + UI 控件树」文件对），输出结构由你用 JSON Schema 定义、由代码规则引擎保证合法的 JSONL——每一行都必然通过 Schema 校验，这是机制而非概率。

```
原始数据 ──▶ （可选）时序分段 ──▶ （可选）线索缝合 ──▶ 去重 ──▶ （可选）分类 ──▶ （可选）动作摘取 ──▶ 质量打分 ──▶ 自动标注 ──▶ （可选）二次校验 ──▶ 结构合法的 JSONL
                         │                               │                                               │            │                    │
                         └───────────────────────────────┴───────────────────────────────────────────────┴── 被淘汰的记录进拒绝通道，一条不丢、笔笔有账 ──┘
```

## 核心特性

- **算子化流水线**：分段 / 缝合 / 去重 / 分类 / 摘取 / 质量 / 标注 / 生成 / 校验九个算子独立开关，编排器只做组合调度（对标 Data-Juicer、distilabel、Dolma 的算子体系）
- **分类算子与按类条件化**（v1.7）：LLM 封闭集分类按用户类别表给记录打标（词表经 Schema enum 硬校验，失败归兜底类），打分 rubric/门槛、标注与生成指令均可按类覆盖（`[class.<name>.*]`）；多标签模式下一条数据扇出多条按类管线
- **时序流分段与动作摘取**（v1.8）：时间序输入流按会话规则切分（session window）后由 LLM 滑窗精化 episode 边界并剔除噪声帧，再对每对相邻帧摘取结构化动作（click/input_text/scroll……）；episode 作为序列记录照走打分（内置轨迹 rubric）、序列标注与缺陷表评审修复
- **线索缝合**（v1.9）：同一任务被穿插切开的 episode 碎片经「单调选池 LLM 判定 × 机械先验合取」保守缝合成完整线索（thread ⊃ fragment ⊃ step 三级结构），有界二遍复评修正贪心漏缝；过短被剔的收尾段先进候选池救援，接缝零推断机械占位——错缝代价高于漏缝，保守偏置写死在模板里
- **QuRating 双模式质量打分**：pairwise 成对比较 + Bradley-Terry 拟合（批内锦标赛），或 pointwise 0–5 加性量表（绝对刻度），共用同一套可自定义的 rubric
- **四层结构保证**：供应商原生结构化输出 → 确定性 JSON 修复 → jsonschema 校验 → 有界 LLM 修复环；修不好的进拒绝通道，绝不污染主输出
- **纯生成模式**：无输入数据时从种子池（Self-Instruct 式）或纯条件化提示从零合成数据集，产物照走全套治理
- **无状态、可审计**：中间态只存在于进程内存；产物只有主输出、拒绝通道、统计报告与可选 trace 事件流；计数满足守恒等式，每次裁决的理由可回查
- **工程化容错**：记录级隔离、全抖动退避重试、熔断器（认证类错误首错即断）、原子交付、可复现随机性

## 快速开始

```bash
uv sync                      # Python ≥ 3.11；第三方依赖仅 7 个，无框架
uv run labelkit --help
```

两份 TOML 配置：`config.toml` 声明 LLM 从哪来（跨任务复用），`project.toml` 定义一次任务怎么跑。跑通仓库自带的文本标注示例：

```bash
export LABELKIT_ZAI_KEY=sk-...                      # 密钥只经环境变量进入
cd examples/text && mkdir -p out
uv run labelkit validate --config ../config.toml --project project.toml --probe
uv run labelkit run      --config ../config.toml --project project.toml
```

一分半钟后得到主输出（每行 = 你的 Schema 字段 + 可选 `_meta` 履历）、拒绝通道与运行报告。六个示例工程分别覆盖：文本去重+打分+标注（`examples/text`）、UI 截图多模态全流程（`examples/ui`）、从零合成数据集（`examples/generate`）、分类路由与按类打分/标注（`examples/classify`）、时序 UI 流的 episode 分段与动作摘取（`examples/stream`）、穿插任务流的线索缝合与短段救援（`examples/thread`：四个规范交叉场景 + 纯噪声负样本会话）。

## 文档

| 文档 | 内容 |
|---|---|
| [用户手册](docs/manual/README.md) | 29 章教科书式手册：安装、数据排布、逐参数配置、算子调优、从易到难五篇实战教程 |
| [设计规格](spec/) | 实现级设计规格（v1.4 + v1.5/v1.6/v1.7/v1.8/v1.9 修订）：每个模块的职责、算法、配置与 IO 契约，每处算法选择均有论文/工业项目背书 |
| [跨模块契约](docs/CONTRACTS.md) | 冻结的接口契约：签名、配置数据类、事件目录、提示词模板 |
| [开发文档](docs/dev/) | E2E 测试问题清单（含修复状态）、需求分析 |
| [设计文档原稿](docs/design/) | 规格书的 HTML / PDF 原稿 |

## 开发

```bash
uv run pytest -q -m 'not integration'              # 离线套件（纯逻辑，秒级）
uv run pytest tests/integration -q -m integration  # 真实 LLM 端点集成测试（需 .env）
```

本项目遵循 spec 驱动开发：`spec/` 是字段名、默认值与错误码的单一事实源，实现与 `docs/CONTRACTS.md` 的偏差需先修订文档。LLM 相关行为一律以真实端点测试，不使用 mock。

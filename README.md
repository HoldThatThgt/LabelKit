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
- **时间流生成**（v1.13）：`[generate.stream]` 让纯生成模式一次合成一条**序列**而非一条样本——LLM 只做两类内容调用（蓝图定步数与逐步帧类、帧实现按蓝图逐位产出内容），会话装箱、交叉、噪音插入、原样重发与时间戳铺设全部由零 LLM 的机械交织器完成；产物一式两份：可直接标注的序列，以及可原样重放为输入的**时间流工件**（行内带 truth 真值，重放后成员 id 与会话切分逐字节一致）。序列类还可各带一份独立的标注 Schema。
- **有限场景规划与精确交付**（v1.17）：时间流再增加 fixed-offset finite schedule、day/week/schedule 整数 quota、named frame rule/window、跨 sequence rule、duration/resource 与 structured noise；一份冻结 `ScenarioPlan` 被 validate、dry-run 与 M6 共用，每个 sequence/noise slot 有界补足，duplicate/replay 保持可核对，四类 file hook 均按 project root 解析。旧 v1.16 时间流键不提供兼容别名。
- **帧类构成档位与时间字段回填**（v1.14）：`[[generate.stream.tiers]]` 把「这一档序列用哪几个帧类」做成显式档位——类配额按权重零抽签配分（整数域最大余额法），蓝图以「enum 限档内子集 + 逐类 contains 覆盖」双向硬约束，构成恰等 ⇒ 档位身份可从产物数据直接反推对账，档位序数随 `generator.tier_rank` / 工件 `truth.tier_rank` / 报表三处落盘；`[frame.class.<name>.generate.time_fields]` 把生成 Schema 里的时间语义字段（如 duration）绑定到时间轴——绑定即从 LLM 面剔除，值由零 LLM 的机械回填按序内相邻帧的时间戳差算出，且先于 id 计算 ⇒ 工件重放依旧逐字节同 id
- **按类档位表**（v1.15）：`[[class.<name>.generate.tiers]]` 让每个序列类自带一张档位表，**整表原子取代**全局表（未声明即回落；全局表恒为锚——它既是回落源、也是档位面的唯一开关）。于是 `tier_rank` 收窄为**类内身份**（各表自行连续覆盖 1..N，跨类同 rank 无工具语义、跨类同构成合法），配分与 `--limit` 的分块推论逐类各算各的，报表 `tiers` 相应从平面形切为**类嵌套形** `{"<类>": {"<档>": {planned, produced}}}`；零抽签、零调用数变化，全部类不声明时与 v1.14 逐字节等价
- **序列规则与日历窗口**（v1.16，历史键已由 v1.17 重命名）：`frame_rules`/`frame_windows` 保留同序列约束，跨序列约束使用 `sequence_rules`；有限 schedule 与 quota 由 v1.17 ScenarioPlan 在内容调用前统一冻结。旧键仅用于 v1.17 CONFIG_ERROR 负例
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

几分钟后得到主输出（每行 = 你的 Schema 字段 + 可选 `_meta` 履历）、拒绝通道与运行报告。示例工程共五个：`examples/text`、`examples/ui`、`examples/stream` 按输入格式组织，`examples/mix` 演示 UI 与文本的双粒度上手，`examples/synth-stream` 则是 **v1.17 有限场景规划与精确交付**的主教学样板。最后一个工程使用纯 DeepSeek 单端点：两张 quota 表共同约束两个序列类的精确 target，`crossed_sessions` 固定交叉数，有限 schedule 固定时间边界，另有噪音与一条流尾 duplicate。五个帧类中 request / acknowledgement 是结构化帧，二者用 `subject_id` correlation 连接；`chain_response` 还要求 acknowledgement 紧跟 request 且墙钟差在 20–40 分钟半开区间。request 落在工作日早/下午多窗，ticket_booking 用按类窗整表覆盖缩窄范围；`duration_s`、`resources` 与跨 sequence 的 `sequence_rules` 是可选约束面，本示例未启用；`hooks.py` 演示序列级确定性验证。两张档位表仍保留「同 rank 必须带类解读」教学；主输出与 `out/synth-labels.stream.jsonl` 同步落盘。先运行 `cd examples/synth-stream && mkdir -p out && uv run labelkit run --config config.toml --project project.toml`，再运行 `uv run labelkit run --config config.toml --project project-replay.toml`，即可用同一工件验证流尾 duplicate 命中 `dropped_dup`。

## 文档

| 文档 | 内容 |
|---|---|
| [用户手册](docs/manual/README.md) | 30 章教科书式手册：安装、数据排布、逐参数配置、算子调优、从易到难五篇实战教程 |
| [设计规格](spec/) | 实现级设计规格（v1.4 + v1.5/v1.6/v1.7/v1.8/v1.9/v1.10/v1.11/v1.12/v1.13/v1.14/v1.15/v1.16/v1.17 修订）：每个模块的职责、算法、配置与 IO 契约，每处算法选择均有论文/工业项目背书 |
| [跨模块契约](docs/CONTRACTS.md) | 冻结的接口契约：签名、配置数据类、事件目录、提示词模板 |
| [开发文档](docs/dev/) | E2E 测试问题清单（含修复状态）、需求分析 |
| [设计说明书](docs/design/) | 规格书的单文档 HTML / PDF 汇编稿（由 `tools/build_design_doc.py` 从 `spec/` 生成，与现行修订同步） |

## 开发

```bash
uv run pytest -q -m 'not integration'              # 离线套件（纯逻辑，秒级）
uv run pytest tests/integration -q -m integration  # 真实 LLM 端点集成测试（需 .env）
```

本项目遵循 spec 驱动开发：`spec/` 是字段名、默认值与错误码的单一事实源，实现与 `docs/CONTRACTS.md` 的偏差需先修订文档。LLM 相关行为一律以真实端点测试，不使用 mock。

# 计划书：分类算子（classify）与按类条件化管线

> 2026-07-07。需求：「LabelKit 当前支持单一类别标注，是否可以通过加入一个分类算子，支持分类，根据分类执行不同的打分、标注和生成？」同日补充需求：当数据在多个 class 匹配度都高时，允许其流向多个管线（一条数据产生多个标签与多份标注结果），并提供开关让工程锁定单分类 / 多分类——已并入 §4.2 / §4.3（`classify.assignment`）。
> **状态：待对齐。**按项目惯例（spec §1.6），本文档为对齐材料——结论认可后先修订 spec 与 CONTRACTS.md，再实现；未对齐前不动代码。
> **2026-07-07 更新**：已完成六域并行可行性/亲和性审查（全部 feasible，35 条摩擦收敛为 30 条裁决），开发规格见 `SPEC-classify-operator.md`——凡与本文不一致处（如 R4 fallback 留痕、R8 防呆分级、uniqueItems 移除）以该文件为准。

---

## 1. 结论先行

**可行，且与现有架构高度兼容。**推荐方案：

1. **新增一个算子 `classify`**（建议编号 M13，文件 `labelkit/classify.py`），位于链序 **dedup 之后、quality 之前**：对每条存活记录做 LLM 封闭集（closed-set）分类，类别表由用户在 project.toml 声明，输出经 M8 内部 Schema（enum 硬约束）保证合法。标签基数由开关 `classify.assignment = "single" | "multi"`（默认 single）控制：single 锁定一条一类；multi 允许一条数据命中多类并**按标签扇出**到多条按类管线（§4.3）。
2. **路由采用「类条件参数化」而非「物理拆链」**：管线拓扑不变，下游算子（quality / annotate / generate / verify）按记录所属类选择参数——每类可覆盖 rubric、质量门、标注指令与 few-shot、生成指令与风格、评审维度；未覆盖的键继承全局配置。这正是 Dolma「tagger 打属性、mixer 按属性决策」与 NeMo Curator「`bucketed_results` 字段路由」的同构做法（§3 调研）。
3. **默认关闭，零行为变化**：`classify.enabled = false`（默认）时工具行为与 v1.6 完全一致。只开分类不配任何按类覆盖 = 纯打标模式（类标签进 `_meta.classification` 供下游使用，InsTag / NeMo Curator DomainClassifier 用法）。

一句话概括数据流变化：

```
现行:  ingest → dedup → quality(单rubric) → generate(单指令) → annotate(单指令) → verify(单维度) → emit
提案:  ingest → dedup → classify → quality(按类rubric+分池) → generate(按类种子+指令) → annotate(按类指令) → verify(按类维度) → emit
```

multi 标签模式（`classify.assignment = "multi"`）下 classify 处多一条扇出边：命中 k 类的记录派生出 k 个单标签兄弟信封，各自独立走完 quality → annotate → verify 并各产出一行（§4.3 扇出块）。

## 2. 需求拆解与现状

「单一类别标注」在现行规格下的确切含义——一次运行只有**一套全局任务定义**，所有记录同质处理：

| 面 | 现行承载 | 全局唯一性 |
|---|---|---|
| 质量准则 | `quality.rubric`（默认或内联，spec §5.2/§5.3） | 一次运行一套 rubric，一个 threshold/top_ratio |
| 标注任务 | `annotate.instruction` + `annotate.examples`（spec §3.5.2 确定性模板） | 一套指令与 few-shot |
| 输出结构 | `output.schema_path/schema_inline`（M8 单一 user_schema） | 一个 JSON Schema |
| 生成任务 | `generate.instruction` + `[[generate.styles]]`（spec §3.6.2） | 一套生成指令与风格池 |
| 二次校验 | `verify.extra_criteria`（spec §3.7.2） | 一段追加维度文本 |

混合类型数据（如输入法指令同时含写作/问答/翻译/代码）今天只有一个 workaround：**预先切分输入、每类一个 project.toml 分多次运行**。其痛点正是本需求的价值论证：

- **鸡生蛋**：切分本身就需要分类，而分类恰是本工具该做的 LLM 批处理；
- **去重割裂**：全局 DedupIndex 只在单次运行内存活（spec §3.10.3 跨批存活状态、2.6 无跨运行状态），跨类重复互相漏检;
- **报表割裂**：N 次运行 N 份 report.json，无统一分布视图；批大小、种子、成本记账全部割裂；
- **运维 N 倍**：N 套 project.toml 与输出路径的维护成本。

单标签模型还有一个表达不了的现实：**指令数据天然多意图**——「写一段讲解二分查找原理的教程」同时是写作与问答，强制单类要么丢信息、要么把路由劫持到次优类。预先切分输入的 workaround 对此完全无解（一条物理数据只能落进一个切分）。这是 §4.3 多标签模式的动机。

## 3. 业界方案调研

按「分类信号从哪来 → 分类结果怎么用」两问逐家考察。除注明外均为 2026-07 检索核实。

### 3.1 分类 → 分路处理（与本需求同构的最强先例）

**Nemotron-CC（NVIDIA, arXiv:2412.02595）**：用 3 个质量分类器（fastText + 两个 FineWeb-Edu 系分类器）集成打分，按分位切成 0–19 共 20 桶、归并为 5 档质量层，**不同档走不同的合成数据管线**——高质量档（桶 >11）走 DiverseQA / Distill / ExtractKnowledge / KnowledgeList 四种改写任务扩充 token；低质量档走 Wikipedia 风格重写去噪。该流程已产品化进 NeMo Curator：文档字段 `bucketed_results` 即路由依据，官方文档明言 "Quality score used to route documents to appropriate pipelines"。**启示**：①「先分类、按类走不同生成/加工策略」是经过 6.3T token 级生产验证的模式；② 路由信号以**记录级属性字段**承载，管线拓扑本身不动——正是本文推荐的类条件参数化形态。

**Tülu 3（AI2, arXiv:2411.15124）**：post-training 数据按**核心技能**（数学/代码/精确指令遵循/安全…）分治——逐技能收集 prompt、逐技能设计 persona 条件化合成管线（IF-Persona-SFT 等）、先构建 per-skill 混合再合并调优。**启示**：按类条件化的生成指令（本提案的 `[class.<name>.generate]`）是 SOTA 后训练数据工程的标准姿势；LabelKit 已有的 `[[generate.styles]]`（Persona Hub/Cosmopedia 思想，spec [34][35]）恰好是它的类内配套。

### 3.2 分类作为管线算子（形态先例）

**NVIDIA NeMo Curator 分类器阶段**：`DomainClassifier`（26 个域标签）、`MultilingualDomainClassifier`（52 语言）、`QualityClassifier`（High/Medium/Low，DeBERTa v3）等以统一 `DistributedDataClassifier` 基类挂进管线，输出写入记录级标签字段（`quality_pred` 等），支持 `filter_by` 按标签过滤。**启示**：分类算子的输出契约 =「记录级标签 + 可选按标签动作」；其标签直接用于构建 quality-specific blends。差异：NeMo Curator 跑本地 GPU 分类模型，LabelKit 的工具级负边界「不训练/托管任何本地模型」（spec §2.1.2 ①）决定了本提案分类走运行时 LLM API——与 M4 用「运行时 API 裁决」替代 QuRater 离线分类器（spec §3.4.5 背书行、§1.6）是同一个既有决策的延伸。

**Dolma toolkit（AI2, ACL 2024，spec 已引 [6]）**：架构为 tagger → attributes → mixer 三段——tagger 给文档/片段打属性（语言、毒性、质量分…）写入 sidecar attributes 文件，**决策推迟到 mixer 按属性过滤/分流**；支持运行时挂载自定义 tagger。**启示**：「打标」与「按标决策」解耦是数据管线的成熟范式；对应到本提案：classify 算子只负责打标（写 `PipelineItem.classification` 与 `_meta.classification`），按类选参数的决策留在各下游算子内部完成，两层职责边界清晰。

**Refuel Autolabel（工业, spec 已引 [12]）**：把 classification / multilabel classification 作为一等 LLM 标注任务类型，配置声明合法标签表（labels list），并对输出做「标签 ∈ 词表」校验。**启示**：LLM 封闭集分类的工程要点是**词表硬校验**——本提案以 M8 内部 Schema 的 enum 约束实现（比事后校验更强：供应商结构化输出 + 修复环全套复用）；其 multilabel 任务的输出契约即「标签集合 ⊆ 词表」，正是本提案 multi 模式内部 Schema 的同款形态。

### 3.3 LLM 打标信号（算法先例）

**InsTag（阿里, ICLR 2024, arXiv:2308.07074）**：用 LLM 对 SFT 指令做开放集语义/意图打标（6.6K 细粒度标签），据标签度量多样性与复杂度并驱动数据选择（TagLM 以 6K 精选样本胜过更大数据量基线）。**启示**：① LLM zero-shot 打标指令语义可靠可用，且已被用于驱动数据决策；② **开放集标签适合分析、封闭集标签才适合路由**——路由要求类表与配置键静态对齐，故本提案取封闭集为 v1 语义，开放集 tagging 列演进候选（仅打标不路由）；③ InsTag 的打标天然是**多标签**形态（其复杂度度量即基于每条指令的标签数）——指令多意图是常态而非例外，为 §4.3 的 multi 模式提供动机背书。

**FineWeb-Edu（spec 已引 [11]）与 DCLM 系质量分类器**：证明「LLM 判决蒸馏出的轻量分类器」在预训练数据上有效——但蒸馏训练在 LabelKit 负边界之外，仅作背景。

### 3.4 管线级条件路由（框架先例）

**distilabel `routing_batch_function`（Argilla, spec 已引 [5]）**：DAG 中允许上游步骤的批按自定义函数路由到不同下游步骤子集（`upstream >> routing >> [step_a, step_b]`）。**启示**：算子化流水线框架支持条件路由有成熟先例；但其路由粒度是**批**，而本需求的类别混杂在批内、粒度是**记录**——这是本文 §4.3 拒绝「物理拆链」方案的关键论据之一。另一面启示：该路由函数可以把同一批**同时**发给多个下游任务、产出多份并行结果再合并（内置的 `sample_n_steps` 即此形态）——「一条数据流向多个管线、产生多份结果」在算子化框架中有直接先例，本提案 multi 模式的记录级扇出即其对应物。

### 3.5 调研小结（三条共性）

1. **分类信号一律落为记录级属性**（Dolma attributes / NeMo Curator label 字段 / Nemotron-CC bucket 字段），管线拓扑不因分类而分叉；
2. **按类差异化的是参数与提示词**（Nemotron-CC 每档不同改写 prompt、Tülu 3 每技能不同合成管线），不是重写引擎；
3. **封闭集 + 词表硬校验**是分类用于路由的前提（Autolabel labels list、NeMo Curator 固定标签集），开放集打标（InsTag）只用于分析。

本提案的设计完全落在这三条共性上。

## 4. 方案设计

### 4.1 算子定位与链序

新增算子 `classify`，挂入编排器规范链序（`orchestrator._CHAIN_ORDER`）：

```
dedup → classify → quality → generate → annotate → verify
```

位置论证：
- **dedup 之后**：重复记录不花分类调用（与「M3 在 M4 之前」同一成本逻辑——被淘汰者不再消耗下游 LLM 调用）；
- **quality 之前**：这是「按类打分」的前提——pairwise 比较必须同类（「哪段更有教育价值」对代码 vs 闲聊的跨类比较无意义），rubric 按类选择也须在打分前就绪；
- generate 在 quality 后取过门种子，天然可按类分种子池；annotate/verify 在后按类取指令/维度。

职责与边界（对齐 spec §2.2.1 表的行格式）：

| 模块 | 职责（做什么） | 边界（不做什么） | 依赖 |
|---|---|---|---|
| classify | 按用户类别表对批内存活记录做 LLM 封闭集分类（单/多标签可配，可选 self-consistency 投票）；结果写入 `item.classification`；multi 模式下按标签扇出兄弟信封（§4.3）；已带分类的记录跳过（幂等，供生成子批继承）。 | 不淘汰记录（分类不是质量门；multi 扇出只增不减）；不定义类别语义（来自配置）；不做标注（用户 Schema 产出物属 M5）；不改变管线拓扑（扇出改变的是批内信封基数，不是链结构）。 | M1, M8, M9 |

与 M5 annotate 的边界：分类是**工具内部结构**（内部 Schema、驱动管线行为、进 `_meta`），标注是**用户定义结构**（用户 Schema、是交付产物本身）。被 quality 淘汰的记录也已有类标签（供报表分布），但不会被标注——两者时机与用途都不同。

### 4.2 分类算法

**LLM 封闭集分类，经 M8 内部 Schema 保证**（形态与 M4 裁决、M7 verdict 完全同构）：

- 提示词为确定性模板拼接（进 CONTRACTS §10 新节）：system = 类别表（每类 `name: description` 逐行）+ 可选 `classify.instruction` 补充说明 + 输出格式约束；user = 可选的每类示例（few-shot，仅输入侧）+ 待分类记录内容。UI 模态与 M4/M5 同法：截图 Part + `UITree.serialize()` 文本 Part，所引 profile 须 `supports_vision`（M1 校验，同现行 UI 模态规则）。
- 内部 Schema `classification_schema(class_names, assignment, max_labels, with_reason)` 按标签基数二态：single → `{"class": <enum: 类名表>, "reason": <str>}`；multi → `{"classes": <array of enum, minItems=1, maxItems=max_labels, uniqueItems>, "reason": <str>}`。enum 是词表硬校验（Autolabel 同款），走 M8 四层防线；`reason` 仅 trace 生效时要求（对齐 `quality.judgment_reasons` 的 "auto" 语义与零额外 token 原则）。multi 另有一条 Schema 表达不了的语义规则、由代码确定性归一化：兜底类与具体类同现时剔除兜底类（「其余」与具体类命中矛盾）。
- **鲁棒性**：`classify.self_consistency = n`（默认 0 关；n≥3 奇数）——n 次独立采样投票：single 取多数票、无过半类时归兜底类；multi 逐标签投票（标签出现在 > n/2 个采样集合中才保留），全部落选归兜底类。机制复用 §3.5.2 的 self-consistency 决策（分类恰是该机制收益最大的「分类型 Schema」场景，spec [33]）。
- **失败与兜底**：`classify.fallback_class` 必填且必须 ∈ 类别表（建议用户命名 `other`，描述写「其余」——LLM 也可主动选择它）；结构修复耗尽时按 `classify.on_error = "fallback"`（默认，归兜底类并记 StageError，kind 新增 `classification_invalid`）或 `"fail"`（记录 `failed` → rejects）。不新增 Status 取值。
- temperature 恒 0（sc 采样除外，同 `annotate.sc_temperature` 机制）；分类调用并发同其他算子（profile 信号量）。

**不采用的分类信号及理由**：① 本地分类器（DeBERTa/fastText，NeMo Curator 形态）——违反工具级负边界「不训练/托管本地模型」（spec §2.1.2 ①），且引入框架级依赖（违反 §2.6 依赖面）；② embedding 最近邻/聚类——需要 embedding profile 与种子样例库，判别力弱于 LLM zero-shot 且引入「质心」这一隐性状态，列为 §8.4 式演进候选（触发条件：分类调用成本成为瓶颈时，以 embedding 粗分 + LLM 精分两级降本）；③ 开放集 tagging（InsTag 形态）——标签无法与配置键静态对齐，不能路由；列演进候选（仅打标进 `_meta`，供多样性分析）。

### 4.3 路由语义：类条件参数化（方案 A，推荐）

**管线拓扑与批生命周期不变**（multi 标签模式仅增大批内信封数，见下方扇出块）；每个下游算子内部按 `item.classification.label` 解析出「该类的有效配置」= 全局节被 `[class.<name>.<section>]` 覆盖后的合并视图（M1 启动时静态合并冻结，运行期零查找成本）。各算子的类条件行为：

| 算子 | 类条件行为 | 关键细节 |
|---|---|---|
| M4 quality | **批内按类分池**：每类独立执行 k 轮配对 → 裁决 → BT 拟合 → 类内百分位归一化；rubric / mode / threshold / selection / top_ratio 均取该类有效配置。 | 类池处理顺序按类名字典序（确定性）；类池 N=1 时沿用现行单条规则（不发裁决调用、score=0.5，spec §3.4.3 归一化行 + §3.10.3 尾批行）；`top_ratio` 名额基数变为**类内** scored 存活数；pairwise 分数语义从「批内相对」进一步收窄为「批内类内相对」——`_meta.scores` 增只增字段 `pool`（= 类名，仅 classify 启用时出现）以自述比较池。 |
| M6 generate | **按类分种子池**：类内过门槛记录为种子，每类用该类有效 instruction / styles / num_per_record 独立走 §3.6.2 调用量公式；新样本**继承种子类**（`classification.source = "inherited"`，零额外调用、溯源确定）。 | (llm, style) 预抽仍按全局调用序号一次性完成（保持调度无关可复现，§3.6.2 多模型混合行不变）；分组后每次调用的种子全部同类，继承无歧义。回流子批经过 classify 时因已带分类而跳过。`generate_only` 模式：生成仍用全局指令（无输入无从按类），产出切批走 M3→classify→M4→M5→M7→M11 主链、被正常分类后按类打分/标注——**不支持 generate_only 的按类生成配比**（见本文 §7 开放决策点③）。 |
| M5 annotate | 按类取 instruction 与 examples 组装提示词（§3.5.2 模板结构不变，仅取值来源变化）。 | **输出 Schema 不按类**（见下）。 |
| M7 verify | 按类取 `extra_criteria` 追加进评审提示词（§10.5 模板结构不变）。 | policy / max_repair_rounds 全局。 |
| M3 dedup / M2 / M11 | 不按类。 | 判重是内容属性、发生在分类之前；输出通道分发只看 status。 |

**单标签 / 多标签开关与扇出语义（`classify.assignment = "single" | "multi"`，默认 `"single"`）**

- **`"single"`（默认，锁定一条一类）**：内部 Schema 强制恰一类（§4.2），一条记录一个标签、至多一行输出——即上文全部行为。
- **`"multi"`（允许多类命中，按标签扇出）**：LLM 返回该记录适用的类集合（1 ≤ k ≤ `classify.max_labels`）。k ≥ 2 时 classify 在批内**扇出**：标签集按类别表声明序规范排序，原信封取首标签，其余 k−1 个标签各克隆一个兄弟 `PipelineItem` 追加于批尾——克隆共享同一 frozen `Record`（引用共享，零内容复制），`classification.label` 各异、`classification.labels` 同为全集。此后每个信封与普通单标签记录完全同构：进各自类池打分、按各自类参数标注/评审、**独立淘汰**（同一条数据可以在写作池被质量门淘汰、在问答池存活）、独立产出一行。下游算子对扇出**零感知**——仍只读 `item.classification.label`。
- **「匹配度」的运行化**：v1 = 模型对「类描述是否适用」的集合判断（sc 逐标签投票加固，§4.2），不引入数值置信度；逐类适用度打分（0–5 + 阈值筛集合）列演进候选，触发条件：需要可调的多标签灵敏度。
- **扇出不过 dedup（链序硬依据）**：克隆在 classify 内追加、只走 quality 及之后阶段。克隆与原件内容相同，若经过 M3 会互判 exact 重复（规范化内容 SHA-256 相同）——扇出位置必须在 dedup 之后，现链序天然保证。生成样本继承其种子信封的单一标签、不再扇出。
- **契约修订（spec §4.3）**：Stage 契约「不删除列表元素（只改 status）」需增补与 generate 例外并列的 classify 例外——「classify（multi 模式）可向批尾追加派生信封；不修改、不删除、不重排既有元素」。
- **计数与不变量（spec §6.4）**：新计数 `counts.fanout` = 扇出净增信封数（single 恒 0；报告仅 multi 时出现该键），不变量扩展为 `emitted + dropped_* + failed + bad_input = scanned + generated + fanout`——与 `generated`、v1.6 `unprocessed` 的既有扩展手法相同。
- **输出唯一键变化**：一条输入至多产出 `max_labels` 行（每行独立通过用户 Schema）；消费侧行唯一键由 `_meta.id` 变为 (`_meta.id`, `_meta.classification.label`)——仅 multi 模式如此，手册须明示。
- **确定性**：扇出克隆按（原记录批内位置 → 标签声明序）追加，同输入同 seed 逐字节可复现。

**输出 Schema 全局唯一（本提案的明确边界）**：多 Schema 会把「输出结构用户定义」变成「输出结构按类定义」，击穿 M8 单 user_schema、M11 终检、L2.5 回调、few-shot 启动校验、6.3 校验语义（「剥除 `_meta` 后必须过用户 Schema」）的整条链。且无必要——JSON Schema 2020-12 原生支持 `oneOf`/条件子模式，用户今天就能在**单个** Schema 内表达按类变体（配合按类 instruction 引导 LLM 落到对应分支）。按类 Schema 列演进候选，触发条件：出现 `oneOf` 无法表达的真实工程。

**方案 B（批级物理路由，distilabel `routing_batch_function` 形态）——已考虑，拒绝**：把批按类拆成子批、每类走独立链。三条硬伤：① 批是 pairwise 比较池与内存生命周期单元（spec §2.6 吞吐行「阶段之间在批内串行」），拆批产生大量小池，百分位归一化统计意义严重弱化；② 批号/报表/trace 的批级契约（`batch.start/end`、`_meta.scores.batch_no`）全部复杂化；③ 路由逻辑必然落进 M10，违反「编排器零业务逻辑」（spec §3.10.1）。方案 A 以记录级属性达成同等表达力（§3.5 调研共性①），无此三伤。

### 4.4 配置设计（project.toml）

新增两块：`[classify]` 节（算子自身）与 `[class.<name>.*]` 节族（按类覆盖）。完整示例（沿用贯穿示例的输入法意图工程）：

```toml
[classify]
enabled = true
llm = "default"                       # profile 引用；可指低成本模型
assignment = "single"                 # "single" 锁定一条一类（默认）| "multi" 允许多类命中并扇出（§4.3）
max_labels = 2                        # 仅 multi 生效：单条标签数上限 ∈ [2, 类别数]，缺省 = 类别数；扇出成本上界旋钮
fallback_class = "other"              # 必填，须 ∈ classes
self_consistency = 0                  # 0=关；≥3 奇数（single 多数票 / multi 逐标签投票，§4.2）
on_error = "fallback"                 # "fallback" | "fail"

[[classify.classes]]
name = "writing"                      # [a-z0-9_]+，表内唯一（字符集规则同 criterion key，§5.3）
description = "写作协助类指令：代写、改写、文案、模板"
examples = ["帮我写一条请假条，明天上午要去医院"]   # 可选 few-shot（仅输入侧）

[[classify.classes]]
name = "qa"
description = "知识问答与解释类指令"

[[classify.classes]]
name = "other"
description = "不属于以上任何一类的指令"

# ── 按类覆盖：未出现的键一律继承全局节 ──
[class.writing.quality]
threshold = 0.25                      # 写作类门槛放宽
[class.writing.annotate]
instruction = "你是写作类指令的意图标注员。……"

[class.qa.quality]
mode = "pointwise"                    # 小众类改绝对刻度，避免小池 pairwise 退化
rubric = "inline"                     # 与全局规则一致：写 inline 必须提供该类 rubric 子表
[class.qa.rubric]                     # 类内联 rubric（结构同 §5.3）
name = "qa-rubric"
[[class.qa.rubric.criteria]]
key = "factual_density"
description = "事实密度与可核查性"
pairwise_prompt = "哪段问答指令的事实含量更高？"
pointwise_levels = ["…", "…", "…", "…", "…", "…"]

[class.qa.generate]
instruction = "模仿示例生成全新的中文知识问答指令：……"
num_per_record = 3
```

**可按类覆盖的键白名单**（M1 强校验，白名单外的键报 CONFIG_ERROR；后续只增）：

| 节 | 可覆盖键 | 不可覆盖（保持全局）及理由 |
|---|---|---|
| `[class.*.quality]` | mode, rounds, rubric（含 `[class.*.rubric]` 内联）, threshold, selection, top_ratio | llm / judges / both_orders / on_unscored——LLM 绑定属部署与成本面，类差异先用 rubric 表达 |
| `[class.*.annotate]` | instruction, examples | llm / self_consistency / sc_temperature |
| `[class.*.generate]` | instruction, styles, num_per_record, temperature | llms / mixture / weights / seeds_per_call / num_per_call / sample_validator |
| `[class.*.verify]` | extra_criteria | llm / judges / policy / max_repair_rounds |
| —— | —— | run.* / input.* / dedup.* / output.*（含 schema 与 validator）/ trace.* 全部不可按类 |

M1 校验清单（并入 §3.1.4，fail-fast 全量报错）：classes ≥ 2 且 name 唯一合法、description 非空；fallback_class ∈ classes；classify.llm profile 存在（UI 模态须 supports_vision）；self_consistency = 0 或 ≥3 奇数；`assignment` ∈ {"single", "multi"}，`max_labels` 仅 multi 模式可设且 ∈ [2, 类别数]；`[class.<name>]` 的 name 必须 ∈ classes；覆盖键 ∈ 白名单；类内 threshold 与 top_ratio 互斥（合并后视图上校验，同全局规则）；类内 examples 的 output 干跑过全局 Schema 与 validator（同现行 few-shot 校验）；`classify.enabled = false` 时出现 `[[classify.classes]]` 或 `[class.*]` → CONFIG_ERROR（防呆）。

优先级语义：`[class.<name>].<sect>.<key>` > project.toml `[<sect>].<key>` > 内置默认。这是 project.toml **内部**的条件化合并，不改变「CLI > project > config」三源优先级（spec §2.5）。

### 4.5 数据结构与可观测性（全部只增）

| 契约面 | 变更 |
|---|---|
| `types.py` | 新 frozen dataclass `Classification {label: str, labels: tuple[str, ...], source: Literal["llm","fallback","inherited"], detail: Mapping}`（label = 本信封的路由标签；labels = 该记录命中的全集，single 模式恒单元素；detail 含 reason / sc 统计）；`PipelineItem` 增字段 `classification: Classification \| None = None`。Status 枚举**不变**。 |
| `_meta`（§6.3） | 增顶层键 `"classification": {"label", "labels", "source"}`（未启用 = null，对齐 verification 惯例；single 模式 labels 恒为单元素数组）；`_meta.scores` 增 `pool` 字段（仅 classify 启用时）。 |
| report.json（§6.4） | 增 `classify` 节：`{"assignment", "classes": {<name>: count}, "multi_label_records", "fallback_count", "failures"}`（多标签记录在 classes 直方图中逐标签计数）；quality 节的 `aggregate_histogram` / `per_criterion_mean` 在 classify 启用时增按类分组视图；generate `buckets` 的 key 在 classify 启用时扩展为 `<class>×<llm>×<style>`（默认关闭时格式不变）。counts 不变量：single 模式**不变**（分类不改变基数）；multi 模式 counts 增列 `fanout` 并扩展不变量（§4.3 扇出块）。 |
| trace（§7.2） | `trace.channels` 枚举增 `"classify"`；新事件 `classify.decision`（payload：`label`、`labels`（multi 时携带全集）、`source`、`reason`†、`sc`†；† 分别受 judgment_reasons 同款语义与 sc 开关控制）；error 事件按产生 stage 归 classify 通道。事件目录只增不改契约维持。 |
| 错误码（§7.6） | 增 `classification_invalid`（记录级：M8 修复耗尽且 on_error="fail" 时记录 failed；on_error="fallback" 时仅入 `item.errors` 留痕、记录存活）。 |
| CONTRACTS.md | §7 增 classify 模块 API（`ClassifyStage` + `build_classify_prompt` + `classify_record`）；§10 增分类提示词模板与 `classification_schema`；§6.1 配置 dataclass 增 `ClassifyConfig` / `ClassSpec` / `ClassOverrides`。 |
| dry-run 估算 | `classify_calls = total_records × max(1, self_consistency)`，并入 §2.4 `--dry-run` 行与 CONTRACTS §7.9 的静态估算公式（实现在 `orchestrator._estimate`）。multi 模式下游调用量依赖平均标签数、静态不可知——估算按乘数 1 报下界并在 stderr 注明（口径见 §7 ⑦）。 |

### 4.6 成本

每记录净增 1 次 LLM 调用（sc 启用则 ×n）——与 pointwise 单准则同量级，显著低于 pairwise 打分（k/2 次/记录）与标注（1 次 + 修复）。分类提示词短（类别表 + 记录内容），token 成本低；`classify.llm` 可独立指向低成本 profile。对照收益：按类 rubric 使打分裁决更聚焦（tie 率下降是 §7.5 可量化的预期收益）、按类指令降低标注/生成的提示词内分支复杂度。

multi 标签模式的额外账：quality / annotate / verify 的调用量按**平均标签数 m̄** 放大（分类调用本身不变——一次调用产出整个标签集），上界由 `classify.max_labels` 控制；UI 模态下克隆各自的标注调用独立懒加载图像（内存仍不常驻，I/O ×m̄）。这是「一条数据换多份结果」的对价，`counts.fanout` 与 `classify.multi_label_records` 使其在报表中可见。

### 4.7 非功能约束相容性核查（对照 §2.6 / §2.1.2 / §3.10.3 相关条目）

| 约束 | 核查 |
|---|---|
| 数据不落盘（§2.6） | 分类结果在 `PipelineItem` 内存信封与显式输出通道（`_meta`/report/trace），无新增落盘面。✓ |
| 跨批存活状态封闭清单（§3.10.3 仅三项） | 分类逐条独立，不新增任何跨批索引，清单维持三项不变。✓ |
| 吞吐（§2.6：阶段批内串行、profile 信号量并发） | classify 为链上普通阶段，批内记录级并发受 profile 信号量，阶段屏障语义不变。✓ |
| 记录级隔离 | 分类失败 fallback 或 failed，不出批。✓ |
| 可复现 | temperature 0 + 确定性模板；类池处理与生成分组均按类名字典序；multi 扇出按（批内位置 → 标签声明序）确定性追加；(llm,style) 预抽机制不变。✓ |
| 隐私 | 类别表属用户配置（同 rubric 信任级）；reason 文本受 `trace.content` 四档脱敏；report 只含类名计数（类名是配置产物，非数据内容）。✓ |
| 不训练/托管本地模型（§2.1.2 ①） | 分类走运行时 LLM API，与 M4 既有决策同构。✓ |
| 不做训练数据配比（§2.1.2 ⑥） | 按类参数是**加工条件化**，不做跨类输出配额/重采样；「每类输出恰好 N 条」明确不做（属 §8.3 O6 全局定量问题域）。✓（须在 spec 负边界处补一句划界） |
| 内存 | 每 item 增一个小 dataclass；类池分组是批内临时字典；multi 扇出克隆共享 frozen Record 引用、只复制信封（×m̄，受 max_labels 界）。可控。✓ |

## 5. 触点与工作量估算

| 触点 | 内容 | 量级 |
|---|---|---|
| spec | 新 §3.13（或重编号，见 §7①）模块节；§2.2/§2.3 架构与开关矩阵；§4 数据结构与 §4.3 Stage 契约（multi 扇出例外）；§5.2 配置表 + `[class.*]` 白名单表；§6.3/§6.4（含 multi 的 fanout 计数与不变量扩展）；§7.2/§7.6；§1.5/§1.6/§8.4 背书与决策记录 | 大 |
| CONTRACTS.md | §6 配置 dataclass、§7 模块 API、§8 事件、§9 输出、§10 提示词模板与内部 Schema | 中 |
| M1 config | ClassifyConfig/ClassSpec 解析、白名单校验、按类合并视图冻结、全量校验清单 | 中 |
| M13 classify.py | 提示词组装、sc 投票、fallback、multi 扇出与归一化、事件与计数 | 中 |
| M8 schema_engine | `classification_schema()` 内部 Schema 常量 | 小 |
| M4 quality | 批内按类分池（配对/BT/归一化/门控循环套一层类分组）；有效配置解析 | 中偏大 |
| M5/M7 | instruction/examples/extra_criteria 取值改走有效配置 | 小 |
| M6 generate | 种子按类分组、每类独立预算与提示词、继承类标签、桶 key 扩展 | 中 |
| M10/M11/M12 | 链序插入、fanout 计数与不变量扩展、report classify 节与按类视图、trace 通道 | 小-中 |
| 测试 | 离线：模板组装/白名单合并/分池配对与名额/继承逻辑；集成（真实 LLM glm-5.2）：新 `examples/classify` 工程（混合类型 fixture）+ 现有离线全量回归 | 中 |
| 手册 | 新章「分类与按类条件化」；第 4/7/8/10/12/16 章受影响段落按真实运行重同步 | 中-大 |

## 6. 里程碑与验收

1. **对齐**：本文档评审，裁决 §7 开放决策点 → 结论记入 spec §1.6。
2. **规格**：spec + CONTRACTS 修订并自洽（配置表/事件/错误码/模板逐表落位）。
3. **实现**：M1 → M13 → M4 分池 → M5/M6/M7 条件化 → 可观测面；每步跑 `uv run pytest -q -m 'not integration'`。
4. **验收**（观测定义）：
   - 新增 `examples/classify` 混合数据工程真实运行：report.classify.classes 分布合理、各类 `_meta.annotation` 走了各自 instruction（trace 抽查）、rejects 与 counts 不变量成立；
   - `classify.enabled=false` 回归：现有三个 examples 工程输出与 v1.6 逐字段一致（`_meta.classification: null` 除外）；
   - 集成测试断言：enum 硬约束下不产生词表外标签；fallback 路径可触发（构造语义模糊 fixture）；
   - multi 模式集成运行：构造双意图 fixture（如「写一段讲解二分查找原理的教程」，writing + qa 双命中），断言同一 `_meta.id` 产出两行、行键 (`_meta.id`, `classification.label`) 唯一、`counts.fanout` 与扩展不变量成立、两行 `_meta.annotation` 各走各类 instruction；同一 fixture 在 `assignment="single"` 下仅产出一行（开关锁定生效的直接观测）；
   - `--strict` / dry-run / 熔断交付语义不受影响。

## 7. 开放决策点（需求方裁决）

1. **模块编号**：追加 M13（零重排成本，推荐）vs 按流水线位置全量重编号（v1.3 曾有重编号先例，但那次是模块拆分；本次为纯新增，追加即可）。
2. **fallback 语义**：本文取「fallback_class 必填且为普通类成员」（无特殊类、可配 per-class 参数）；备选为隐式 `_unclassified` 特殊类（永远走全局默认）。推荐前者。
3. **generate_only × classify 组合深度**：本文取「生成用全局指令，产物回流被分类后按类打分/标注」；若需要「按类配比从零合成」（每类 standalone_count），属新的量目标语义，建议单独立项（与 §8.3 O6 有交集）。
4. **白名单初始范围**：是否放开 per-class `quality.llm` / `annotate.llm`（按类换模型）。本文建议 v1 不放（部署面全局），触发条件出现后只增。
5. **纯打标模式是否需要显式开关**：本文取「不配任何 `[class.*]` 覆盖即自然退化为纯打标」，无需新键。
6. **多标签中间档**：multi 现语义 = 「多标签即扇出」；是否另需「仅多标签打标、不扇出」档（labels 全集进 `_meta`、仍按首标签走单条管线，InsTag 式分析用途）。本文建议暂不加——真实场景出现后在 `assignment` 枚举上只增一档即可。
7. **dry-run 的 multi 估算口径**：按标签乘数 1 报下界（推荐——诚实且不虚高预算）vs 按 `max_labels` 报上界（预算保守）。

## 8. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 小类池 pairwise 退化（类内 N 过小时百分位粗、N=1 恒 0.5） | per-class `quality.mode="pointwise"`（绝对刻度不依赖池）；手册指引：类别多时增大 `run.batch_size`；report 按类直方图使问题可见 |
| 分类错误级联（错类 → 错 rubric/指令） | enum 硬约束 + sc 投票；`classify.decision` 事件带 reason 可审计（§7.5 同款闭环：抽读 reason → 修订类描述 → 同 seed 重跑对比类分布）；fallback 类兜底 |
| 配置面复杂化（N 类 × 覆盖键） | 白名单收窄 + 全键继承默认 + M1 全量 fail-fast；纯打标零覆盖即用 |
| 成本上升（+1 调用/条） | 独立低成本 profile；`--dry-run` 估算先行 |
| 报表消费者适配（buckets key、quality 按类视图） | 仅 classify 启用时出现新形态；默认关闭零变化；trace/`_meta` 严格只增 |
| multi 扇出的成本与输出行数放大（×m̄） | 默认 `assignment="single"` 锁定；`max_labels` 上界；类描述写得互斥可压低多命中率；`counts.fanout` / `classify.multi_label_records` 使放大量可见可审 |
| multi 下 `_meta.id` 不再唯一，可能打破下游消费假设 | 仅 multi 模式出现；手册明示行唯一键 = (`_meta.id`, `classification.label`)；single 模式零变化 |

## 9. 新增背书文献（拟并入 spec §1.5/§9）

- Su, D. et al. **Nemotron-CC**: Transforming Common Crawl into a Refined Long-Horizon Pretraining Dataset. arXiv:2412.02595.（质量分类 → 分档路由不同合成管线；NeMo Curator 产品化实现）
- Lu, K. et al. **#InsTag**: Instruction Tagging for Analyzing Supervised Fine-tuning of Large Language Models. ICLR 2024. arXiv:2308.07074.（LLM 指令语义打标 + 据标签做数据决策）
- Lambert, N. et al. **Tülu 3**: Pushing Frontiers in Open Language Model Post-Training. arXiv:2411.15124.（按核心技能分治的数据构造与 per-skill 合成）
- NVIDIA **NeMo Curator** Distributed Data Classification（Domain/Quality/Multilingual classifiers；工业，docs.nvidia.com/nemo/curator）。（分类算子作为管线阶段、标签字段路由——已引 [9]，此处为能力面扩充）
- 既有引用直接复用：Dolma tagger→mixer [6]、distilabel routing_batch_function [5]、Autolabel classification task [12]、self-consistency [33]、Persona Hub/Cosmopedia [34][35]。

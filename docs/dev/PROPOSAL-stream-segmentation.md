# 计划书：时序流语义分割与动作摘取（stream / segment / extract）

> 2026-07-13。需求：「当前支持文本/截图+控件树输入，但缺少时间这一个轴。当数据按时间排序输入时，希望 LabelKit 对其做语义分割（如控件树 1,3,4,5,6 代表用户用 Uber Eats 下单点外卖的全动作）。两个缺失能力：① 无法语义分割数据流；② 无法摘取流中的动作数据。达成后应可通过标注算子为序列打上动作标签（这是用户在做什么）。另问：这是否是业界标注工程的通用能力？如果不是，如何拆分成多个通用能力算子？」
> **状态：已定稿并实现（v1.8，2026-07-14）。**评审与裁决见 `SPEC-stream-segmentation.md`（S1–S32）；本文保留为需求与调研原始记录。

---

## 1. 结论先行

**可行，业界有强同构先例，推荐拆成「一个输入声明 + 两个新算子 + 既有算子的序列适配」落地：**

1. **时间轴与会话化（M2 扩展 + `[stream]` 配置节）**：摄取层声明排序键（UI 模态天然有 `pair_index` 升序，文本模态可指 `meta:<field>` 时间戳），按会话规则（时间间隙 / 分区键 / 长度上限）把有序流切成候选会话——这是流处理领域的标准 **session window** 原语（Flink `EventTimeSessionWindows` / Beam `Sessions`），纯规则零 LLM 成本。切批改为「整会话装箱」，保证 episode 不跨批。
2. **新算子 M14 `segment`（语义分段）**：在候选会话内用 LLM 做边界精化与噪声帧剔除（滑窗逐帧裁决，走 M8 内部 Schema），把成员帧收拢为**序列信封**（episode）。需求例子中「1,3,4,5,6 成段、2 被排除」即「连续分段 + 帧级噪声过滤」语义。边界判据**内置且任务无关**（「是否延续同一目标导向活动」——GEBD 无词表事件边界 + Def-DTS 通用演绎模板背书，§3.3），**用户零 prompt 可用**；可选 `segment.context` 只提供域上下文，不定义边界。
3. **新算子 M15 `extract`（转移/动作摘取）**：对 episode 内每对相邻帧 ⟨s_i, s_{i+1}⟩ 做一次 LLM 裁决（截图×2 + 控件树 diff 摘要），产出结构化动作 `{action_type, target, value, description}`——这正是 OS-Genesis「reverse task synthesis」第一步与 OpenAI VPT「逆动力学模型（IDM）」的运行时 LLM 对应物；pairwise quality 的「一请求两图」先例（`quality.py:250-251`）证明底层就绪。
4. **既有算子零新增、按序列适配**：episode 是一条普通（复合）记录——classify 照常给它打类标签（v1.7 按类条件化自动生效）、quality 用轨迹 rubric 打分（OS-Genesis 的 trajectory reward model 同构）、**annotate 给整个序列打「用户在做什么」的任务标签**（用户 Schema 承载，多图请求 ≤20 帧降采样）、verify 评审「步骤序列 ↔ 任务标签」一致性。

对「是否业界通用能力」的回答（详见 §3.5）：**作为端到端整体不是——它是 GUI 智能体训练数据工程的新兴专用管线（OS-Genesis / AgentNet / AITW），每家自建，通用标注工具（CVAT / Label Studio）只有“人工时序分段”而无自动语义分割算子，通用数据管线框架（Data-Juicer / NeMo Curator / distilabel）也没有此类算子；但拆开后的每一步都是有成熟背书的通用能力**（session window、话题分割/变点检测、逆动力学动作标注、"定位再描述"的两段式）。本提案正是按通用能力算子拆分设计的，每个算子独立可用。

数据流变化一句话：

```
现行:      ingest ─────────────────────→ dedup → classify → quality → annotate → verify → emit   （记录各自独立）
stream 模式: ingest(排序+会话化) → segment → dedup(序列语义) → classify → extract → quality → annotate → verify → emit
                └── 帧流 ──────┘   └────────────────────── episode 信封 ──────────────────────┘
```

## 2. 需求拆解与现状

### 2.1 需求的三个缺口与目标产物

| 缺口 | 含义 | 业界对应物 |
|---|---|---|
| 时间轴 | 记录间的先后关系不进入数据模型 | episode 数据集的 step 序（AITW/AndroidControl） |
| 语义分割 | 有序流 → 语义完整的动作段（含噪声剔除） | temporal/action segmentation、RPA UI-log segmentation |
| 动作摘取 | 相邻状态对 → 用户做了什么动作 | 逆动力学（VPT IDM）、reverse task synthesis（OS-Genesis） |

目标产物形态即 AndroidControl 的两级结构：**episode 级**（goal：「用户在用 Uber Eats 点外卖」）+ **step 级**（每步动作：click/input_text/scroll + 目标控件 + 描述）。本提案里 step 级由 extract 产出（进 `_meta.stream.steps` 并注入标注提示词），episode 级由 annotate 按用户 Schema 产出。

### 2.2 现状事实（勘察结论，file:line）

| 事实 | 证据 | 设计含义 |
|---|---|---|
| Record 无任何时间戳字段；顺序性只有 `RecordRef.line_no` / `pair_index` | `types.py:136-145, 19-27` | 时间轴须新增声明；UI 的 `pair_index`（`ingest.py:321` 升序遍历）可直接作默认序键 |
| 摄取顺序确定性：文件名字典序 → 行号；UI 跨子目录按单一 index 命名空间配对 | `ingest.py:238, 253-294, 301-331` | 排序层可信，stable sort 叠加自定义键即可 |
| 切批 = `islice(stream, run.batch_size)` 顺序切段 | `orchestrator.py:187` | episode 会被批边界切断 ⇒ 会话化必须先于切批，切批改整会话装箱 |
| Stage 合同②禁止删元素；改变批基数的先例只有「classify ②a 尾部追加」与「generate ③ 返回新子批」，均只增不减 | `stage.py:34-41`、`classify.py:308-325`、`orchestrator.py:279-287` | 「多帧并一条」需新立合同例外 ②b（吸收成员 + 尾部追加序列信封），M10 用 len 差计量（同 `counts.fanout` 构造，`orchestrator.py:270-278`） |
| Status 是封闭 Literal（5 值）；emitter 按 status 分流两通道 | `types.py:10-16`、`emitter.py:105-135` | 新增 `absorbed`（成员并入 episode，两通道都不写）与 `dropped_noise`（噪声帧，进 rejects）两个状态值 |
| M9 对单请求图片数无上限（两 provider body builder 对 parts 循环）；现有调用方最多 2 图（pairwise quality） | `llm_client.py:238-246, 273-283`、`quality.py:250-251` | 序列级多图标注无底层障碍；Anthropic 端点上限 100 图/请求、>20 图时单图限 2000px（2026-07 官方文档）⇒ 需帧数降采样旋钮 |
| 跨记录聚合先例：pairwise 的「多记录进一 prompt + 一个 gather 并发」 | `quality.py:232-253, 331-354` | extract / 序列 annotate 的组装骨架现成 |
| 新算子配置的全套先例（dataclass → loader → 校验 → `_build_stages`） | `model.py:114-125`、`loader.py:609-629, 1384-1420`、`cli.py:199-212` | 触点清单可照 classify 机械展开 |
| spec §8 无任何时序/流式/episode 议题或排除条款 | `spec/80-ch8-nongoals-roadmap.md` 全文检索 | 全新方向；须对齐的现存约束是 A2（一 uitree 文件=一屏，不受本提案影响）与「无持久化/批内存模型」 |

### 2.3 为什么不能用现有能力凑

- classify 是**逐条**封闭集分类，无法表达「这 5 条构成一件事」；
- quality/annotate 收单条 Record，提示词组装无序列形态；
- dedup 反而是**负资产**：连续 UI 帧本就相似，帧级 pHash（默认汉明距 ≤8 判重）会把合法步骤当重复杀掉——stream 模式必须把 dedup 移到 episode 级（§4.6）。

## 3. 业界调研（2026-07-13 检索核实）

### 3.1 GUI 轨迹数据工程——与需求最强同构的先例

**OS-Genesis（ACL 2025, arXiv:2412.19723）**：无预定义任务的 GUI 轨迹合成管线——先规则化遍历 GUI 收集三元组 ⟨s_pre, a, s_post⟩（动作前后截图 + 动作），再用标注模型（GPT-4o）做 **reverse task synthesis**：每个三元组 → low-level 指令，再聚合为 high-level 任务指令；最后 **trajectory reward model** 给整条轨迹的连贯性/完成度打分做质量筛选。**启示**：①「从状态转移反推动作与任务标签」正是需求的核心工序，且已有 ACL 主会级验证；② 其三段式（转移标注 → 任务聚合 → 轨迹打分）恰好映射到本提案的 extract → annotate → quality（轨迹 rubric）。差异：OS-Genesis 动作是采集时自带的，我们的输入只有状态流 ⇒ 动作需推断（见 3.2 VPT）。

**OpenCUA / AgentNet（arXiv:2508.09123，22.6K 桌面轨迹）**：工业级标注基础设施——AgentNetTool 录屏+键鼠+a11y 树，DataProcessor 做 **Action Reduction**（把海量低层事件确定性归并成语义动作：鼠标移动序列→click、滚轮合并、按键序列→文本/热键）与 **State-Action Matching**（每个动作对齐到动作开始前最后一个视觉稳定帧，避免未来信息泄漏），再由 generator+reflector 合成步级反思 CoT。**启示**：①「确定性归并 + LLM 语义化」两层分工是动作摘取的工程正解——对应本提案「控件树 diff（代码侧确定性）+ LLM 动作裁决」；② 状态-动作对齐的锚定原则（取动作前最后稳定帧）写进 extract 的提示词约定。

**AITW（Android in the Wild, arXiv:2307.10088，715k episodes）**：两阶段采集——先让标注员在模拟器完成端到端任务，再对已录轨迹做 **hindsight language relabeling**：人工识别并标注其中的简单动作子序列（"add item to cart"）。**启示**：「先有流、后分段再打标」是该领域的标准姿势，本提案是它的自动化版本。

**AndroidControl（Google, NeurIPS 2024 D&B, arXiv:2406.03679，15,283 episodes）**：每 episode = goal + `step_instructions[]` + `screenshots[]` + `accessibility_trees[]` + `actions[]`（JSON：action_type ∈ {click, long_press, input_text, scroll, navigate_back, navigate_home, open_app, wait} + 参数），动作数 = 截图数 − 1（动作发生在相邻截图之间）。**启示**：episode/step 两级标注结构与动作词表直接采用（本提案 extract 内部 Schema 的 action_type 枚举照此裁剪）；「转移数 = 帧数 − 1」是 extract 调用量公式。

**GUI-Odyssey（ICCV 2025，8,334 跨 App episodes）**：步级语义标注（屏幕描述 + 决策理由）由 GPT-4o 补注。佐证「LLM 给既有轨迹补语义标注」的可靠性。

### 3.2 动作摘取的算法范式

**VPT（OpenAI, NeurIPS 2022, arXiv:2206.11795）**：用少量带动作标签的数据训练**逆动力学模型（IDM）** p(a_t | o_1…T)——因为可以同时看过去与未来帧，「反演环境动力学」远比行为克隆易学——再用 IDM 给 70k 小时无标签视频打伪动作标签。**启示**：「从相邻状态推断动作」是被大规模验证过的独立工序；LabelKit 的负边界「不训练/托管本地模型」（spec §2.1.2 ①）决定我们用 **LLM zero-shot 充当运行时 IDM**（与 M4 用运行时 API 替代 QuRater 离线分类器是同一个既有决策的延伸），且同样利用「可看前后帧」的非因果优势（extract 一次调用喂 s_i 与 s_{i+1} 两图）。

**控件树 diff 作确定性辅助**：UI 模态两帧的树是结构化数据，节点增删改（尤其 text/focus/bounds 变化）可代码侧确定性计算——这是 OpenCUA Action Reduction 的树版对应物，作为 extract 提示词的 `[树变更摘要]` 段注入，缩短视觉推断距离、降低幻觉。零额外调用。

### 3.3 语义分割的算法谱系

| 谱系 | 代表 | 对本提案的取用 |
|---|---|---|
| 会话窗口（流处理标准原语） | Flink `EventTimeSessionWindows.withGap()` / Beam `Sessions` / Flink SQL `SESSION` TVF；web 分析 30min gap 惯例 | 规则层照抄：inactivity gap + 分区键（keyBy 对应 `stream.key`）+ 动态上限；纯代码零成本 |
| 话题/文本分割 | TextTiling（Hearst 1997）→ 嵌入化 TextTiling → **Embed-KCPD**（句嵌入上的核变点检测, arXiv:2601.18788）→ **Def-DTS**（LLM 多步演绎裁决话题边界, arXiv:2505.21033） | LLM 滑窗边界裁决即 Def-DTS 形态；变点检测（需 embedding profile）列演进候选 |
| 无词表事件边界 | **GEBD**（Shou et al., ICCV 2021, arXiv:2101.10511）：taxonomy-free 事件边界检测——认知科学结论「人类无需预定义事件类别就自然地把连续活动切成有意义的块」被形式化为标注准则与基准（Kinetics-GEBD），边界 = 动作/主体/环境变化点 | **边界判据可以内置、无需任务词表**的直接背书——segment 的判据模板固定内置（§4.3），用户不写边界定义 |
| RPA / task mining 的 UI 日志分割 | Marrella et al.《Automated segmentation of UI logs》(2020)；Leno et al. arXiv:2008.05782（无分段 UI 日志中发现候选例程，**显式处理不属于任何例程的噪声事件**）——两者均为无监督，不需要任务描述 | 「分段 = 边界发现 + 噪声剔除」的问题定义直接沿用；其难点变体「交错例程」（同一动作属多个例程）v1 明确不做（§7 决策点④） |
| 稠密视频描述 | dense video captioning：先 temporal localization 再 captioning 的两段式（Vid2Seq, arXiv:2302.14115 及其前身） | 「定位/分段」与「描述/标注」拆成两个工序的先例——对应 segment 与 annotate 分立 |

**任务描述问题（边界判据从哪来）**：需求方的关键疑虑——「让用户描述任务的起止但不提及任务本身，对人类太难」。业界对此有三种解法，且**没有任何一家要求用户书写任务相关的边界定义**：

1. **判据通用化（内置固定判据，零用户 prompt）**：话题分割全谱系的判据是固定的内聚性问题（「话题变了吗」），Def-DTS 的贡献正是一个**跨域零定制**的通用演绎模板（双向上下文摘要 → 域无关意图分类 → 演绎边界判定）；GEBD 把「无词表边界」形式化成可标注、可评测的任务；变点检测与 RPA 日志分割根本没有 prompt。⇒ segment 采用此解法：判据模板内置（§4.3）。
2. **描述后置（hindsight relabeling，描述是输出不是输入）**：Lynch & Sermanet 的 **Hindsight Instruction Pairing**（RSS 2021, arXiv:2005.07648）——先有无结构 play 数据流，再事后问「哪条指令使这段轨迹最优」；AITW 的 hindsight language relabeling、OS-Genesis 的 reverse task synthesis 同型。⇒ 本管线同构：「用户在做什么」由 annotate 在分段**之后**产出，任何人都无需先验描述它。
3. **采集侧条件化（回避分割）**：AndroidControl / GUI-Odyssey / OpenCUA 按任务录制、一任务一录，分割问题在采集时就不存在。⇒ 对 LabelKit 不可用（输入是既成流）——这正是需要 segment 算子的原因。

推论：用户**能**写出的那种描述（「这 20 分钟里有点外卖、导航、刷社交媒体」）在本设计里也有正确去处——它是 **classify 的类别表**（episode 分段后按类打标、驱动 per-class 条件化），而不是 segment 的边界定义。任务词表归 classify，边界判据归内置模板，两者各得其所。

### 3.4 通用标注工具与数据管线框架的现状

- **CVAT / Label Studio**：时序分段标注（视频 timeline segment、`TimeSeriesLabels`）是**标准人工能力**，辅以 SAM2 跟踪类自动化；但没有「自动语义分割交互流并打标」的算子——它们是人力工具，自动分段只到目标跟踪级。
- **Data-Juicer（200+ 算子）**：有 grouper 类算子（`key_value_grouper` / `naive_grouper`），全部按键值分组，**无时序会话化、无语义流分割算子**（检索其 operator zoo 全表核实）；NeMo Curator、distilabel 同样没有。
- 结论：**「按语义把有序数据流分割成序列样本」在通用数据管线框架里是空白**——各家 GUI 数据工程（3.1）都是自建管线。这既回答了「不是通用能力」，也意味着 LabelKit 把它算子化是有差异化价值的。

### 3.5 小结：是否通用能力 + 拆分方案（回答需求方问题③）

**判定**：端到端的「流语义分割 → 动作摘取 → 序列打标」**不是**业界标注工程的既有通用能力，而是 GUI 智能体数据工程的新兴专用管线（每家自建，无现成工具/框架算子）。**但它可以无损拆解为四个各自有成熟背书的通用能力**，这正是推荐的实现结构：

| # | 通用能力 | 背书 | LabelKit 承载 | 独立可用性 |
|---|---|---|---|---|
| ① | 排序 + 会话开窗（sessionize） | Flink/Beam session windows | M2 摄取扩展 + `[stream]` 节（规则，零 LLM） | 单独用 = 会话级分组统计/粗分段 |
| ② | 语义边界精化 + 噪声剔除（segment） | 话题分割谱系、RPA UI-log segmentation | 新算子 M14 | 对话/日志话题切分同样适用（文本模态） |
| ③ | 转移/动作摘取（extract） | VPT IDM、OS-Genesis reverse synthesis、OpenCUA action reduction | 新算子 M15 | 任何「相邻状态对 → 语义事件」任务 |
| ④ | 序列级打分/标注/评审 | dense video captioning、AndroidControl goal 标注、OS-Genesis TRM | 既有 M4/M5/M7 收序列记录（提示词组装适配） | 预分段序列数据直接可用 |

## 4. 方案设计

### 4.1 总览与链序

stream 模式（`segment.enabled = true` 时生效）规范链序：

```
_CHAIN_ORDER(stream) = ("segment", "dedup", "classify", "extract", "quality", "annotate", "verify")
```

- **segment 最前**：episode 形成先于一切逐条算子；
- **dedup 在 segment 后**：帧级判重语义失效（连续帧本就相似），改判 **episode 级**重复（§4.6）；且重复 episode 被淘汰后不再花 extract/quality/annotate 调用（与「dedup 在 quality 前」同一成本逻辑）；
- **classify 在 extract 前**：episode 的类标签（哪个 App/域）先就位，v1.7 的 `[class.<name>.*]` 按类覆盖对 extract/annotate 指令自动生效；
- **extract 在 quality 前**：轨迹 rubric（连贯性/完成度）与序列标注都消费步骤序列。

非 stream 运行零变化：`segment.enabled = false`（默认）时链序、行为、输出与 v1.7 逐字节一致。

### 4.2 时间轴与会话化（M2 扩展 + `[stream]` 配置节）

**排序**：`stream.order_by` 声明序键——`"input_order"`（默认；文本 = 文件名字典序→行号，UI = `pair_index` 升序，即现行摄取序）| `"meta:<field>"`（文本模态从原始行对象 `Record.raw` 取时间戳字段，ISO-8601 / epoch 秒毫秒自动识别；UI 记录无 `raw`，M1 校验 meta:* 仅限文本模态）。**不做全量重排，只做流式单调性校验**——全量 sort 要求整流驻留内存，破坏惰性批生命周期与 §2.6 内存模型；且需求前提本就是「数据按时间排序输入」。乱序或时间戳解析失败的记录按 `stream.on_disorder = "skip"`（默认，计 bad_input + WARN 一次）| `"fail"`（InputError，退出码 3）处理。轻度乱序输入的有界重排窗口列演进候选（§7 决策点⑬）。

**会话化（规则层，纯代码）**：有序流上按三条规则切候选会话——
- `stream.key = ["meta:<field>", ...]`（可选分区键，如设备/用户 id；键不同即断开——Flink keyBy 对应物）；
- `stream.gap_s`（相邻记录时间差 > gap 断开；仅 `order_by = "meta:*"` 时可用）/ `stream.gap_steps`（按序号差断开，index 序可用）；
- `stream.session_max_len` / `stream.session_max_span_s`（长度/时长上限硬断开，防退化超长会话）。

**切批改整会话装箱**：会话作为不可分单元装入批（首适应，批容量 = `run.batch_size` 帧）；单会话超 batch_size ⇒ 硬切 + stderr WARN 一次（§7 决策点⑧）。M2 的会话缓冲区最多驻留一个未闭合会话（≤ session_max_len 条 Record 元数据，图像仍懒加载），不违反内存模型。**摄取器与切批的接口从「记录流」变为「会话流」——这是 M10 侧唯一的结构性改动**（编排器仍零业务逻辑：装箱是容量逻辑不是语义逻辑）。

### 4.3 M14 segment（语义分段算子）

职责/边界（对齐 spec §2.2.1 行格式）：

| 模块 | 职责 | 边界 | 依赖 |
|---|---|---|---|
| segment | 把批内候选会话精化为 episode：可选 LLM 边界裁决与逐帧噪声标记；把成员信封置 `absorbed`、噪声帧置 `dropped_noise`，按序拼装序列 Record 并尾部追加 episode 信封 | 不判重（M3）；不推断动作（M15）；不打任务标签（M5）；不改链结构 | M1, M8, M9 |

**策略**（`segment.strategy`）：
- `"rules"`：候选会话原样成为 episode，零 LLM 调用（信任 gap/key 规则的场景）；
- `"llm"` / `"hybrid"`（默认 hybrid = 规则先切 + LLM 精化）：对每个候选会话，以 `segment.window`（默认 20）帧为窗做滑窗裁决——每帧给出**帧摘要**（UI = 控件树代码侧摘要：包名/activity/标题 + 显著文本，截断至 `segment.digest_max_chars`；文本 = 记录文本截断）+ 相邻帧的**确定性变更提示**（代码侧树 diff 统计：包名/activity 是否变更、节点替换比例、文本域变化——零 LLM，只提供变化幅度与类型证据，不含动作词表、不做语义归因，不越 M15 界），一次调用产出窗内逐帧 `{index, boundary: bool, noise: bool}` 判决（M8 内部 Schema，temperature 0），相邻窗重叠 1 帧、接缝边界归后窗，代码侧确定性缝合。**默认纯文本裁决**（树摘要已含语义），`segment.use_vision = false` 可开多图辅助。
- **边界判据内置、任务无关（零用户 prompt）**：裁决模板为确定性拼接、逐字进 CONTRACTS §10（与 classify 模板同级）。**不采用**「该帧是否开启新任务？」的裸问题形态——Def-DTS 消融实验证明直接问裸问题（w/o intent）比不加任何结构还差；模板照抄两篇文献的可操作化手法，为**三步演绎结构**：
  1. **双向上下文概括**（Def-DTS 3.3 / VPT 非因果优势）：窗内逐帧先概括「此前若干帧在进行的活动」与「此后若干帧的走向」（固定窗幅，代码侧已供帧摘要与 diff 提示）；
  2. **逐帧封闭集关系分类**（Def-DTS 3.4 的域无关 intent pool + GEBD §4 的变化维度清单）：判断该帧相对于进行中活动的**功能角色**，词表固定且域无关（示意，SPEC 定稿）：`continues`（同流程推进）｜`advances`（屏幕/App 变了但可见任务实体延续——验证码、订单号、餐厅名跨屏出现；跨 App episode 是一等公民，GUI-Odyssey 全集皆是，`open_app` 在 AndroidControl 词表里是段内一步而非边界）｜`returns_to_entry`（回到入口/搜索/桌面后开启新流程——同 App 背靠背任务的断点）｜`context_switch`（交互对象与环境不连续且无实体延续——GEBD 的 Change of Object/Environment 维度）｜`interruption`（与前后活动均无关的短暂插入：通知、弹窗、误触）；
  3. **演绎映射**（Def-DTS 3.5「enforced」语义）：`boundary`/`noise` 是关系词表的**查表结果**——continues/advances → 非边界；returns_to_entry/context_switch → 边界；interruption → noise。LLM 不直接回答边界问题，只做封闭集分类（M8 enum 硬校验，与 classify 同款防线）。
- **两个锚定**（GEBD §3.2 标注准则原则的移植，写死在模板文本里）：粒度锚定——「完整任务」层级（整段录屏之下一层，GEBD 的 "1 level deeper" 原则：固定相对粒度后人类无需词表即可稳定一致地标边界）；注意力锚定——只看**前台 App/前台窗口**（GEBD 的 dominant subject 原则：忽略状态栏、后台通知等背景变化）。
- 用户**不需要也不应该**书写边界定义；`segment.context` 只是可选域上下文（如「这是手机屏幕操作流」），缺省为空即可用。
- LLM 失败（M8 修复耗尽）按 `segment.on_error = "keep"`（默认：该会话整体成为一个 episode + error 事件留痕）| `"fail"`（会话成员全部 failed → rejects）。
- `segment.min_len`（默认 2）：短于下限的段按噪声处理（其帧 `dropped_noise`）。
- **质量上限的三层归属**（写入手册调优章）：机制下限（规则确定性 + Schema 硬校验 + 确定性缝合 + trace 可审计）由工具保证；**第一瓶颈是帧摘要保真度**（显著文本抽取没抓到的实体，LLM 看不见——摘要抽取规则是代码侧可迭代面，`use_vision` 是补偿开关）；裁决力上限 = 基座模型 ×（可选）`segment.context`。与 quality 的 rubric、annotate 的 instruction 同构：工具不承诺 prompt-free 的正确性，承诺可观测的迭代闭环（抽读 `segment.boundary` reason → 调 context/window/gap → 同 seed 重跑对比）。

**Episode 形成的确定性**：成员按序键升序；episode `Record.id = sha256(member ids 连接)[:16]`；同输入同 seed 逐字节可复现（LLM 裁决 temperature 0 + 确定性缝合；与 classify 分池同款「以 LLM 输出为条件」的复现声明，spec §2.6 幂等行已有先例）。

### 4.4 M15 extract（转移/动作摘取算子）

| 模块 | 职责 | 边界 | 依赖 |
|---|---|---|---|
| extract | 对每个 active episode 的每对相邻成员帧 ⟨s_i, s_{i+1}⟩ 经 LLM 产出结构化动作（内部 Schema），写入 `item.transitions`；转移数 = 成员数 − 1 | 不重分段（边界属 M13/M14 上游）；不产出用户 Schema 字段（M5）；不淘汰记录 | M1, M8, M9 |

- **提示词**（确定性模板，进 CONTRACTS §10 新节）：system = 摘取指令 + 动作词表说明 + 可选 `extract.instruction` 域提示；user = `[前一帧截图]` 图 + `[后一帧截图]` 图 + `[树变更摘要]`（代码侧 diff：增/删/文本变化节点，bounded）+ `[前后帧树摘要]`。一请求 2 图 = pairwise quality 既有形态。
- **内部 Schema**（照 AndroidControl 动作词表裁剪）：`{action_type: enum[click, long_press, input_text, scroll, open_app, navigate_back, navigate_home, wait, other], target: str|null, value: str|null, description: str}`。
- **失败语义**：单转移 M8 修复耗尽按 `extract.on_error = "unknown"`（默认：该步记 `action_type="other"` + `detail.kind="extraction_invalid"` 留痕，episode 存活）| `"fail"`（episode failed → rejects）。留痕不写 `item.errors`（classify R4 同款：避免污染 rejects 归因）。
- 文本模态 v1 默认不适用（`extract` 仅 UI 序列；文本序列的「转移」语义弱，列演进候选，§7 决策点⑦）。
- 并发：episode 内转移 + 批内 episode 全部并入一个 gather（profile 信号量约束），骨架同 quality phase2。

### 4.5 序列记录与 Stage 合同 ②b

**类型（只增）**：
- `Record` 增两个带默认值字段：`kind: Literal["single","sequence"] = "single"`、`members: tuple[Record, ...] = ()`（frozen 保持；序列记录 text/raw/ui_tree/image = None，modality 取成员模态，ref.source_file = 会话首帧源）；
- `PipelineItem` 增 `transitions: tuple[Transition, ...] | None = None`；
- 新 frozen dataclass `Transition {index: int, action: Mapping, model: str, attempts: int, detail: Mapping}`；
- `Status` 增两值：`absorbed`（成员并入 episode；emitter 两通道都不写，仅计数）、`dropped_noise`（噪声帧；进 rejects，stage=segment, reason=noise）。

**Stage 合同新例外 ②b**（与 ②a 并列，拟入 spec §4.3 / `stage.py` docstring / CONTRACTS §5）：

> **②b segment 例外（仅 stream 模式）**——可将批内既有 active 成员信封的 status 置为 `absorbed` 或 `dropped_noise`（属①④的正常状态写入），并向传入列表**尾部**追加以这些成员拼装的序列信封；追加物视同批内普通元素、同受①③④约束；不得删除、重排或替换任何既有元素对象；返回值仍须是传入的同一列表对象。

**计量归 M10**（同 `counts.fanout` 构造）：segment 阶段前后 len 差 = `counts.episodes`；`absorbed`/`dropped_noise` 由状态 tally 归集。守恒式扩展：

```
emitted + dropped_dup + dropped_lowq + dropped_verify + dropped_noise + failed + bad_input + absorbed
  = scanned + generated + fanout + episodes   （熔断中止再 + unprocessed）
```

### 4.6 下游算子的序列适配

| 算子 | 适配 | 说明 |
|---|---|---|
| M3 dedup | 序列记录的 `dedup_text` = 成员规范化 dedup_text 按序拼接（分隔符固定）；①精确 ②MinHash ④语义照常作用于拼接文本；③pHash **跳过**（序列无单图；成员级图像重复已被拼接文本的树重复间接覆盖） | episode 级重复 = 「同样的操作流程」，正是训练数据去重想要的语义 |
| M13 classify | 零代码倾向改动：序列记录提示词的「当前记录」段 = episode 摘要（成员帧摘要按序拼接，bounded）+ 首帧截图（UI）；类标签驱动 per-class extract/annotate/verify 覆盖（白名单增 `[class.*.extract]` 的 instruction 键） | episode 级「哪个 App/什么域」分类恰是 per-class 条件化的高价值场景 |
| M4 quality | 序列提示词 = 步骤序列文本（extract 产物）+ 帧摘要，**不放全帧截图**（pairwise 两 episode × N 帧图会爆）；新增内置 `default:trajectory` rubric（判据任务无关，见下方 M4 专块）；pairwise 仍可用（比较池 = 批内 episodes，classify 分池保证同类比较） | 轨迹质量分默认只打分不筛（threshold 缺省语义），门控可选 |
| M5 annotate | 提示词增 `[动作序列]` 段（transitions 逐步文本，verbatim 注入）+ `[关键帧截图]` 多图：≤ `annotate.sequence_frames`（默认 20，首末帧恒保留，中间均匀降采样——Anthropic >20 图触发 2000px 单图上限 + 32MB 请求上限）；用户 Schema 照常承载最终结构（如 `{task_label, app, summary, steps[]}`） | 「这是用户在做什么」= 用户 Schema 的一个字段，工具不预设 |
| M7 verify | 评审对象升维：从「(记录, 标注)」变为「(步骤序列 + 首末帧, 标注)」；内部 Schema 升级为类型化缺陷表；repair 按缺陷类型路由（见下方专块） | 既判「任务标签与步骤是否一致」，也判「段切得对不对」 |
| M6 generate | **v1 与 stream 模式互斥**（M1 校验）：序列生成（AgentTrek 式轨迹合成）已是 roadmap O3，另行立项 | 避免本提案范围膨胀 |

**M4 quality 的流式判据：`default:trajectory` 为什么可以任务无关。**「好轨迹」的判据与 segment 边界判据同构地面临任务描述问题，解法也同构——判据锚定 episode 的**内部结构**而非外部任务语义，且全部维度都有 extract 副读数作机械锚点：

| 维度 | 判据（不需要知道任务是什么） | 机械锚点（extract 副读数，注入裁决 prompt） |
|---|---|---|
| 完成度 | 末帧是否达到终态（确认页/完成态/回到入口），序列是否中断在半途 | 末步动作类型与末帧摘要 |
| 连贯性 | 每步是否承接上一步、无无法解释的跳变、无往复抖动 | `other`/含糊动作计数；状态回访环 |
| 目的性 | 步骤是否构成朝单一目标的推进（vs 漫游乱点） | 步骤序列的方向性（同族屏幕递进） |
| 噪声残留 | 段内离题步占比（segment 漏过滤的量化） | 离题步计数 |

三条设计裁决：① **判据背书**——OS-Genesis TRM（GPT-4o，1–5 分制）恰好只用 Completion + Coherence 两个任务无关维度，且其消融证明分级打分优于「不完整即弃」的二元 labeler（不完整轨迹也含探索价值）；据此 **stream 模式下 threshold 缺省 = 只打分不筛** 的现行语义尤其合理（AgentNet 同立场：「标注错误不全是坏事」）。② **相对比较回避绝对定义**——pairwise 模式问「哪条轨迹更连贯/更完整」（QuRating 论点：相对裁决比绝对刻度可靠），classify 分池保证外卖轨迹只与外卖轨迹比。③ **与 verify 的分工**（OS-Genesis TRM 语义在链上拆成两半）：quality 在 annotate **之前**打「标签无关的结构分」（省下被淘汰段的标注调用），verify 在 annotate **之后**做「标签条件的一致性判决」（步骤↔标签）——TRM 的「对照指令评完成度」那一半属 verify，「轨迹自身连贯性」这一半属 quality。用户 rubric 覆盖面留给**价值判断**（如「偏好用搜索而非层层浏览的轨迹」），结构性维度开箱即用。

**M7 verify 的流式配合：三类分段缺陷的检测与有界修复。**流式管线的错误面从「标得错」扩展到「切得错」——漏过滤（噪声帧混入段内）、过度过滤（正常帧误标噪声）、切头切尾（边界帧归错段）。防线分层，每层接住不同错误：

1. **预防（M14 内）**：三步演绎模板的双向上下文使边界判决在看过后续帧之后做出（非因果，天然抗切头切尾）；滑窗重叠缝合；`min_len` 吸收碎段；interruption ≠ boundary 的词表区分压低「把插入误判成边界」。
2. **机械检测（M15 免费副读数）**：切错的段必然在转移层留痕——漏过滤产出**离题步**（该转移与任务无关）；过度过滤产出**无法解释的状态跳变**（缺帧使相邻对不再相邻，extract 判 `other` 或描述含糊）；切头产出「开局即中途」（首帧已深入流程、无入口态）；切尾产出「未达终态」（末步无完成态）。零额外调用。
3. **软门（M4）**：轨迹 rubric 的完成度/连贯性/噪声残留维度把缺陷段压到门下（OS-Genesis TRM 的 score-and-filter 语义）——**修不了也漏不出去（fail-closed）**。
4. **硬门 + 修复驱动（M7）**：stream 模式下 verify 内部 Schema 从「verdict + 批评意见」升级为「verdict + 类型化缺陷表」`defects: [{kind: label_mismatch | off_task_members | missing_head | missing_tail | missing_members, members?, position?, detail}]`（M8 enum 校验；`missing_members` = 段**中部**存在不可解释跳变、疑似缺帧——过度过滤不只发生在头尾）；评审证据 = 步骤序列 + 首末帧截图 + **边界余量摘要**：段边界外前后各 k 帧（默认 2）的帧摘要及其当前去向（`noise` / 相邻段段号）——语音端点检测 hangover 的证据版，使 `missing_head/tail` 的判定从「开局即中途」的间接推断变为与界外邻帧的直接对照（纯文本摘要，零额外 LLM 调用）。`policy="repair"` 时按缺陷类型路由修复（谁的错找谁修；机制沿用现行「批评回喂 `annotate_record` 重标注」的函数直调形态，M7 不重入链）：
   - `label_mismatch` → 现行回路重标注（零新机制）；
   - `off_task_members`（漏过滤）→ **成员收缩**：该成员置 `dropped_noise`、接缝转移重摘取（1–2 次 extract 调用）→ 重标注 → 复审；
   - `missing_head/tail/members` → **缺帧三级判定**（按序查找，逐级降级）：① 缺口的时序邻域（head=段首前、tail=段尾后、members=跳变接缝的序键区间）内存在同会话 `dropped_noise` 帧 → **成员回收**：经边界复裁（复用 M14 窗口裁决）回收入段、重摘取接缝转移 → 重标注 → 复审——批内可行性由两条既有保证兜底：整会话装箱（缺帧必与段同批）+ Stage 合同「只改状态不删元素」（被弃帧仍在批列表里，emit 前状态可翻回——②b 合同措辞注明此翻转仅限 M7 修复路径）；② 缺帧在**相邻 episode**（切头切尾的跨段形态）→ v1 只标记不修复（跨段搬帧级联失效邻段已完成的打分/标注/评审，成本翻倍且可能乒乓；跨段仲裁列演进候选）；③ **无处可寻**（采集端本就没拍到 / 摄取期坏行、配对缺失、乱序被弃）→ 标记 `detail.suspected = "capture_gap"`——这是 LabelKit 不可知不可修的层次，只能靠上游采集解决；缺陷记入 `_meta.verification.defects` 供下游过滤或回溯。
   修复轮数计入 `verify.max_repair_rounds`（含首评，语义不变）；全部路由确定性（enum 缺陷 + 确定性拼接，不消耗 rng）。**修复是工位内微循环，不是链级循环**：链严格单遍（编排器无递归原则，同 generate 回流单轮），成员手术调用的是 segment/extract 的函数（窗口复裁只看接缝邻域），量级 O(缺陷数) 而非 O(批)。**修复后不重打分**：pairwise 分数是池内相对量（BT 全池联立），单 episode 无法脱池重算——沿用修复前分数 + `_meta.stream.repaired = true` 使陈旧性对下游可见。计数器 `verify.membership_repairs` / `verify.boundary_flags`；`_meta.verification` 增 `defects` 键（仅 stream 模式）。
5. **运行级审计闭环**：修不了的系统性失配（gap 失配、摘要漏实体）进 report（缺陷直方图、噪声率、平均段长）与 trace（`segment.boundary` / `verify.verdict` 带 reason）→ 调 window/gap/context → 同 seed 重跑对比（§7.5 同款）。

### 4.7 配置设计（project.toml 增量）

```toml
[stream]                          # 输入侧声明：排序 + 会话规则（M2 消费）
order_by = "input_order"          # "input_order" | "meta:<field>"（文本模态时间戳字段）
on_disorder = "skip"              # 单调性校验失败策略："skip"（计 bad_input）| "fail"；不做全量重排（§4.2）
key = []                          # 会话分区键，如 ["meta:device_id"]
gap_s = 300                       # 相邻记录时间差断开阈值（仅 order_by="meta:*" 可用）
gap_steps = 0                     # 序号差断开阈值（0 = 不启用；与 gap_s 可并用，任一触发即断）
session_max_len = 200             # 会话长度硬上限（帧）
session_max_span_s = 0            # 会话时长硬上限（0 = 不启用）

[segment]                         # M14：episode 形成（stream 模式总开关）
enabled = true                    # 默认 false = 工具行为与 v1.7 完全一致
strategy = "hybrid"               # "rules" | "llm" | "hybrid"
llm = "default"
window = 20                       # 滑窗帧数/调用
digest_max_chars = 400            # 每帧树摘要长度上限
noise_filter = true
min_len = 2
use_vision = false
context = ""                      # 可选域上下文（如「这是手机屏幕操作流」）——不是边界定义；
                                  # 边界判据内置于固定模板（§4.3），零配置可用
on_error = "keep"                 # "keep" | "fail"

[extract]                         # M15：动作摘取（仅 UI 序列）
enabled = true                    # 默认 false；启用要求 segment.enabled
llm = "default"                   # UI 模态须 supports_vision（M1 校验，同 annotate）
instruction = ""
on_error = "unknown"              # "unknown" | "fail"

[annotate]
sequence_frames = 20              # 序列标注单请求最大帧数（首末恒保留 + 均匀降采样）
```

M1 组合约束（并入 §2.3.1 / §3.1.4）：`segment.enabled` 要求 `run.mode="process"` 且 `generate.enabled=false`；**`segment.enabled` 要求 `annotate.enabled`（v1，决策点⑭）**——annotate 关闭时序列记录无 passthrough 输出形态（序列 Record 无 `raw` 载荷可写主输出）；`extract.enabled` 要求 `segment.enabled` 且 UI 模态；`stream.gap_s`/`session_max_span_s` 要求 `order_by="meta:*"`；`order_by="meta:*"` 仅限文本模态（UI 记录无 `raw`）；`[stream]` 在场而 `segment.enabled=false` ⇒ warning（对齐 R8 no-op 分级惯例）；`segment.llm`/`extract.llm` 计入密钥解析/vision/probe 三处引用集（R24 同款；extract 恒入 vision_users，segment 仅 `use_vision=true` 时）；**stream 模式下 quality 的 supports_vision 强制校验放宽**（序列打分纯文本，§4.6）；`[class.<name>.extract]` 入按类覆盖白名单（仅 instruction 键），**segment 不入白名单**——链序因果：segment 在 classify 之前执行，类标签尚不存在，须在 spec 白名单表显式注明缘由。

### 4.8 输出与可观测性

- **`_meta` 增恒在键 `"stream"`**（未启用 = null，对齐 classification 惯例）：`{episode_id, session_id, order_span: [first_key, last_key], member_count, member_ids: [...], member_sources: [...], steps: [{index, action_type, target, value, description}]}`——steps 为 extract 产物 verbatim（输出主通道本就承载数据内容；报表仍只含计数）。
- **rejects**：`dropped_noise` 行 stage="segment"、reason="noise"；episode 级失败行携带 `episode_id`。
- **report**：新 `stream` 节 `{sessions, episodes, mean_episode_len, absorbed, dropped_noise, extract: {transitions, unknown_actions, failures}}`；counts 增 `episodes`/`absorbed`/`dropped_noise`（§4.5 守恒式）。
- **trace**：channels 增 `"stream"`；新事件 `segment.session`（会话闭合：起止键/长度）、`segment.boundary`（LLM 裁决窗：判决摘要，reason 受 `trace.content` 分级脱敏）、`extract.step`（每转移动作结果）。
- **dry-run 估算**：`segment_calls ≈ Σ ceil((session_len−1)/window)`（hybrid/llm）；`extract_calls = Σ (episode_len−1)`；quality/annotate/verify 按 episode 数计。会话构成静态不可精确知 ⇒ 按 gap 规则空跑（不发 LLM）出精确会话数后估算——摄取元数据空跑与现有 `scan(estimate=False)` 先例同构。

### 4.9 成本模型（示例量级）

> **v1.8 勘误（S22，2026-07-14）**：本段原文两处计数错误——extract 应为 **400** 次（转移数 = Σ(episode_len−1) = 450 − 50，原文把成员数当了转移数）；quality(pointwise) 应为 **200** 次（每记录 × 每准则一调用，四准则 × 50）。修正合计 ≈ **725–750** 次；「与逐帧 annotate 500 次同量级」的结论仍成立（~1.5×）。segment_calls 估算公式分母同步修正为 window−1（滑窗重叠 1 帧）。正式公式见 spec §3.10.3 与手册第 17 章调用账表。

500 帧 UI 流、约 25 会话、精化后 50 episodes（均长 ~9）：segment(hybrid) ≈ 25–50 次纯文本调用；extract ≈ 450 次（2 图/次）；quality(pointwise) 50；annotate 50（≤20 图/次）；verify 50。合计 ≈ 650 次调用，与「逐帧 annotate 500 次」同量级，但产出从 500 条孤立标注变为 50 条带步骤的任务序列样本。extract 是大头，可独立指低成本 vision profile；`segment.strategy="rules"` + `extract.enabled=false` 的最小配置零新增调用（纯会话分组 + 序列标注）。

### 4.10 非功能约束核查（对照 spec §2.6）

| 约束 | 核查 |
|---|---|
| 无持久化 | 会话缓冲与 episode 均为进程内存，无新落盘面。✓ |
| 跨批存活状态封闭清单 | 会话缓冲随切批消费即释放；episode 生命周期 = 一批；跨批存活仍仅 dedup 索引 + 计数器（M2 缓冲属摄取流的一部分，同 ingest 文件句柄级别）。✓（spec §3.10.3 须补一句注记） |
| 记录级隔离 | 帧级失败落该帧/该 episode，不出批；②b 例外文字钉死。✓ |
| 可复现 | stable sort + 规则确定性 + temperature 0 + 确定性缝合/拼装 + seed 豁免面不变。✓ |
| 隐私 | 帧摘要/步骤文本只去配置声明的 LLM 端点；stderr 无数据内容；trace reason 受 `trace.content` 分级。✓ |
| 内存 | 序列 Record 持成员引用（frozen 共享）；图像懒加载不变（extract 峰值 2 图/请求、annotate ≤ sequence_frames 图）。✓ |
| ≤500k 记录 | 会话缓冲 ≤ session_max_len；装箱不改批内存生命周期。✓ |

## 5. 逐模块改动清单（2026-07-13 全量重审）

按模块逐一对照代码事实重审后的完整影响面。**★ = 重审新发现或对前文的修订**（此前提案未覆盖）：

| 模块 | 改动项 | 量级 |
|---|---|---|
| M1 config | `StreamConfig`/`SegmentConfig`/`ExtractConfig` 三个 frozen dataclass + `AnnotateConfig.sequence_frames`；`ResolvedConfig` 增 3 必填字段——全必填风格（R23）⇒ ★直接构造它的 ~14 个测试文件机械补参；三节解析 + `*_provided` 防呆 warning；组合约束全量清单（§4.7，含 ★segment⇒annotate 必开、★stream 模式放宽 quality 的 vision 强制校验）；两 profile 入密钥/vision/probe 三处引用集（★extract 恒入 vision_users、segment 仅 use_vision 时）；★`[class.*]` 白名单增 `extract`（仅 instruction）但**不含 segment**（链序因果，须文档化）；★rubric selector 枚举扩 `default:trajectory` | 中-大 |
| M2 ingest | `order_by` 键解析（★meta:* 仅文本模态——UI 记录无 raw）；★排序语义修订：**全量重排改为流式单调性校验**（`stream.on_disorder`，整流 sort 破坏惰性内存模型）；会话装配器（缓冲 ≤ session_max_len，gap/key/上限闭合，对 M10 暴露会话流视图）；IngestReport 增 sessions；dry-run 会话空跑（读全量元数据，成本同现有行数估算） | 中 |
| M14 segment（新） | 本体：帧摘要 + diff 提示组装 → 三步演绎滑窗裁决（M8 内部 Schema）→ 确定性缝合 → episode 拼装与状态改写（②b） | 中 |
| M3 dedup | 序列 `dedup_text` = 成员规范化文本按序拼接；③pHash 对序列跳过；★`ui_dup_requires` 合成判定对序列退化为纯文本层——spec §3.3 须明文（"both" 语义不适用序列） | 小 |
| M13 classify | 序列提示词分支（episode 摘要 + 首帧截图）；★multi 扇出 × episode：克隆时 `transitions` 恒 None（extract 在 classify 后）零代码；★verify 成员修复后兄弟信封 record 分叉——共享语义边界须文档声明 | 小 |
| M15 extract（新） | 本体：相邻对（2 图 + 树 diff 摘要）裁决 → 内部动作 Schema（AndroidControl 词表裁剪）→ `unknown` 兜底留痕（不写 item.errors，R4 同款） | 中 |
| M4 quality | 序列提示词分支（步骤文本 + 帧摘要，**无图**）；`default:trajectory` 内置 rubric（包数据 + spec Appendix A 全文）；★extract 关闭时轨迹 rubric 退化为按帧摘要打分——rubric 文本不得预设 steps 在场，M1 对该组合发 warning 指引 | 中 |
| M5 annotate | 序列模板分支：`[动作序列]` verbatim 注入 + `[关键帧截图]` ≤ `sequence_frames` 多图（首末恒保留、均匀降采样）——CONTRACTS §10.3 冻结模板修订（R27 同级）；sc / L2.5 / repair 后缀路径全不动 | 小-中 |
| M6 generate | 零代码——M1 互斥约束挡住（`segment.enabled ⇒ ¬generate.enabled`，含 generate_only） | — |
| M7 verify | 内部 Schema 升级类型化缺陷表；按缺陷路由修复（§4.6 专块：成员收缩/噪声池回收/跨段只标记）；★episode id **形成时定死、成员修复不重算**（trace 可追溯性；`_meta.stream` 增 `repaired` 标记）；★被直调的 extract/segment 侧函数须列入 CONTRACTS §7 公开 API（沿 M7→`annotate_record` 既有先例）；`_meta.verification.defects` | 中 |
| M8 schema_engine | 三个内部 Schema builder：segment 窗口关系表 / extract 动作 / verify 缺陷表——关键字集 ⊆ 既有冻结集、无 uniqueItems（R1 教训）；内部 Schema 不过 L2.5（同 classification_schema 先例） | 小 |
| M9 llm_client | **零改动**——多图 body 构造已就绪（`llm_client.py:238-246, 273-283` 勘察证实，两 provider 对 parts 循环无张数限制） | — |
| M10 orchestrator | 切批改整会话装箱（仅 stream 路径；非 stream 走原 `islice`，零变化回归锚）；`_CHAIN_ORDER` 双态；`counts.episodes` len-差计量（fanout 同构）+ absorbed/dropped_noise 入状态 tally；守恒式扩展；★熔断部分交付的 `unprocessed` 残差须计入**会话缓冲中未消费帧**（v1.6 公式再扩）；report `stream` 节；`_estimate` 双模式公式 | 中-大 |
| M11 emitter | ★`absorbed` 是**第三条路由**（主输出与 rejects 都不写、仅入状态计数——现分发仅 active/其余两路，`emitter.py:105-135` 切口）；`dropped_noise` 进 rejects（stage=segment, reason=noise）；`_meta.stream` 恒在键（未启用 = null，随 classification 惯例）；`_meta.verification.defects`；summary/progress 增计数展示 | 中 |
| M12 obslog | channels 8→9（`"stream"`；唯一硬编码改点 `loader.py:67`）；新事件 `segment.session` / `segment.boundary` / `extract.step`（reason 受 trace.content 分级）；segment/extract 阶段 error 事件的通道归属表 | 小 |
| types / stage / errors | `Record` 尾部增 `kind`/`members`（带默认，frozen 兼容——现七字段全无默认，只能尾部追加）；`PipelineItem` 增 `transitions`；新 frozen `Transition`；`Status` 增 2 值（封闭 Literal 改点集中 types/emitter/orchestrator 三处）；②b 合同 + ★M7 修复路径状态翻转注记（dropped_noise → 回收仅限该路径）；ErrorKind 增 `segmentation_invalid`/`extraction_invalid`；★帧摘要 helper 落位 `types.py`（毗邻 `UITree.serialize()`，segment/classify/quality 三处共用——算子层不得互相依赖，共享工具必须下沉服务层/共享层） | 中 |
| hooks | 零改动（L2.5 仅作用于用户 Schema 标注调用，序列 annotate 天然覆盖；sample_validator 属 generate，互斥） | — |
| cli.py | `_build_stages` 注册 SegmentStage/ExtractStage（链位序）；`referenced_profiles()` 增两 profile；★`labelkit rubric --show default:trajectory` | 小 |
| spec/ | 新 `314-m14-segment.md` / `315-m15-extract.md`；§1.5/§1.6 背书与决策、§2.1–2.5 架构/开关矩阵/dry-run、§4 类型与 ②b、§5.2 三节键表 + 白名单表、§6.2–6.4 输入语义/`_meta`/report/守恒、§7.2/§7.6 事件与错误码、§8 决策溯源——约 14 文件 | 大 |
| CONTRACTS.md | §3 types verbatim、§4 errors、§5 ②b（含 M7 翻转注记）、§6 配置 dataclass、§7 两新 API 节 + M7 直调函数登记、§8 事件目录、§9 `_meta`/计数词表/守恒、§10 三个模板与三个内部 Schema、§12 决策登记——约 11 处 | 中-大 |
| docs/manual/ | 新章（追加制 `25-stream.md`：直觉/配置/四层防线/调优）；★`_meta.stream: null` 恒在 ⇒ **三个存量 examples 重跑 + 含 `_meta` 样例块的 3/8/15/20/21/22 章重同步**（classify `classification: null` 先例同款——此前工作量漏计）；16/17/18 章事件表/调用账/错误码增行 | 大 |
| tests/ | 新：`test_stream_ingest`（单调性/会话闭合/装箱）、`test_segment`（缝合确定性/②b/守恒）、`test_extract`（diff 摘要/Schema/兜底）、`test_verify` 增缺陷路由、集成 `examples/stream`（噪声帧 + 双任务 + 跨 App fixture）；存量：emitter `_meta` 键全集断言、Status 枚举断言、★~14 文件 ResolvedConfig 补参 | 中-大 |

## 6. 里程碑与验收

1. **对齐**：本提案评审，裁决 §7 → 记入 spec §1.6。
2. **规格**：spec + CONTRACTS 修订自洽（可仿 classify 做 fan-out 可行性审查后出 SPEC）。
3. **实现**（依赖序）：M1/类型/②b → M2 排序+会话化 → M10 装箱 → M14 → M3 序列 dedup → M13/M5/M7 序列组装 → M15 → M4 轨迹 rubric → 可观测面。每步离线测试全绿门禁。
4. **验收**（观测定义）：
   - 新 `examples/stream` 工程真实运行：fixture 含「点外卖全流程 + 中途切走的无关屏 + 第二个任务」，断言噪声帧进 rejects(reason=noise)、两任务各成一个 episode、`_meta.stream.steps` 动作序列与人工预期一致（trace 抽查）、annotate 产出任务标签落在用户 Schema；
   - 守恒式含 episodes/absorbed/dropped_noise 成立；同 seed 重跑逐字节一致；
   - `segment.enabled=false` 全量回归：现有四个 examples 输出与 v1.7 逐字段一致（`_meta.stream: null` 除外）；
   - 多图上限验证：sequence_frames=20 时对 25 帧 episode 的降采样调用成功（真实端点）；
   - `--strict`/dry-run/熔断交付语义不受影响。

## 7. 开放决策点（需求方裁决；默认值可直接生效）

1. **编号/命名**：追加 M14 `segment` / M15 `extract`（默认，同 R30 追加制）；`extract` 备选名 `transitions`。
2. **`[stream]` 位置**：独立节（默认）vs 并入 `[input]`。
3. **噪声帧去向**：进 rejects（默认，可审计）vs 仅计数不落盘。
4. **交错 episode**（帧 2 属于另一并行任务而非噪声）：v1 不做，列 roadmap（默认；RPA 文献中的难变体，需全局归属模型）。
5. **generate × stream**：v1 互斥（默认）；序列合成并入 O3 立项。
6. **序列 dedup 语义**：①②④ 作用于成员拼接文本、跳过③（默认）vs 增加「成员 pHash 序列相似」第五级。
7. **extract 文本模态**：v1 不支持（默认）vs 提供「转移摘要」弱语义档。
8. **会话超 batch_size**：硬切 + WARN（默认）vs CONFIG_ERROR 要求调大 batch_size。
9. **轨迹 rubric**：新增内置 `default:trajectory`（默认）vs 仅文档示例。
10. **`_meta.stream.steps` 是否可关**：恒在（默认）vs `output.stream_steps = false` 旋钮。
11. **分段粒度旋钮**：v1 固定「完整任务」粒度内置于判据模板（默认）；备选增 `segment.granularity = "task" | "step"`——事件分割天然是层级的（认知科学与 AndroidControl high/low-level 两级的共同结论），且「选粒度」对人类是容易的（「切成完整任务」vs「切成操作步骤」），远易于「写边界定义」；真实需求出现后在判据模板上做二态即可，只增。
12. **verify 修复范围**：v1 = 标签重标 + 成员收缩 + 噪声池回收，跨段搬帧只标记（默认，§4.6 M7 专块）；备选一并放开跨段边界仲裁——代价是邻段级联重修（打分/标注/评审全部重跑）与乒乓风险，触发条件：审计数据显示跨段形态占缺陷主体。
13. **输入乱序处理**：流式单调性校验 + `stream.on_disorder`（默认 skip 计 bad_input），**不做全量重排**（默认；整流 sort 破坏 §2.6 惰性内存模型，且需求前提即「按时间排序输入」）；备选为有界乱序窗口（k-帧滑窗重排，容忍采集端轻度乱序），真实输入出现后只增。
14. **stream ⇒ annotate 必开（v1）**：序列记录无 `raw` 载荷，annotate 关闭时主输出行无内容形态（默认约束住）；备选为定义序列 passthrough 形态（成员 source 引用列表），有真实「只打分不标注」的流式工程再放开。

## 8. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 边界/噪声误判级联（切错段 → 动作序列失真 → 任务标签错） | hybrid 默认规则先行收窄 LLM 裁决域；判据模板内置两条约定（跨 App 同任务不切开、同 App 入口屏复现常为新段）+ 确定性 diff 提示压低误判面；`segment.boundary` 事件带 reason 可审计（§7.5 闭环：抽读 → 调 context/window/gap → 同 seed 重跑对比 episodes 数）；verify 评审「步骤↔标签」一致性兜底。仍不足时的升级路径 =「extract 结果回喂做第二遍分段」两遍法——v1 明确不做（成本翻倍 + 环形依赖），列演进候选 |
| extract 调用量大（转移数≈帧数） | 独立低成本 vision profile；dedup 前置淘汰重复 episode；dry-run 先估算 |
| 多图请求撞供应商上限（>20 图 2000px/32MB） | `annotate.sequence_frames` 降采样默认 20；`max_image_px` 既有降采样叠加；集成测试钉死真实端点行为 |
| 会话规则失配（gap 太小切碎/太大粘连） | report.stream 的 sessions/episodes/mean_len 使失配可见；手册给 web 30min 惯例与移动端建议值 |
| 长会话内存（session_max_len × Record） | 上限默认 200 且硬断开；图像懒加载不变 |
| Status 枚举扩两值牵动 emitter/tally 面 | 封闭枚举改点集中（`types.py`/`emitter.py`/`orchestrator.py` 三处），照 classify 清单机械展开 + 存量断言测试补齐 |
| 流式下坏帧影响放大（单条隔离哲学弱化：一帧 bad_input/误弃在所属 episode 里留下因果孔洞，影响整段连贯性） | 检测面统一（extract 跳变 → quality 连贯性压分 → verify `missing_members`/`suspected="capture_gap"` 标记）；ingest 的 bad_input 计数与 WARN 有账可查；手册指引：stream 工程监控 `counts.bad_input`，采集质量敏感场景 `on_disorder`/`on_bad_line` 用 `"fail"` 快停排查 |
| 批 = 比较池语义被 episode 化改变（pairwise 池 = episodes 数偏小） | 推荐 pointwise + 轨迹 rubric；手册指引增大 batch_size（帧容量）以扩池 |

## 9. 新增背书文献（拟并入 spec §1.5/§9）

- Sun, Q. et al. **OS-Genesis**: Automating GUI Agent Trajectory Construction via Reverse Task Synthesis. ACL 2025. arXiv:2412.19723.（reverse task synthesis 三段式；trajectory reward model）
- Baker, B. et al. **VPT**: Video PreTraining — Learning to Act by Watching Unlabeled Online Videos. NeurIPS 2022. arXiv:2206.11795.（逆动力学模型给无标签流打动作标签）
- Wang, X. et al. **OpenCUA**: Open Foundations for Computer-Use Agents. arXiv:2508.09123.（AgentNet 标注基础设施；Action Reduction + State-Action Matching）
- Rawles, C. et al. **AITW**: Android in the Wild. NeurIPS 2023 D&B. arXiv:2307.10088.（hindsight language relabeling）
- Li, W. et al. **AndroidControl**: On the Effects of Data Scale on Computer Control Agents. NeurIPS 2024 D&B. arXiv:2406.03679.（episode/step 两级标注结构与动作词表）
- Lu, Q. et al. **GUI-Odyssey**. ICCV 2025. arXiv:2406.08451.（步级语义补注）
- Hearst, M. **TextTiling**. CL 1997；Gwon & Kim et al. **Def-DTS**. arXiv:2505.21033；**Embed-KCPD**. arXiv:2601.18788.（话题分割：词汇 → 嵌入变点 → LLM 演绎；Def-DTS 的通用演绎模板 = 边界判据跨域零定制的直接先例）
- Shou, M.Z. et al. **GEBD**: Generic Event Boundary Detection — A Benchmark for Event Segmentation. ICCV 2021. arXiv:2101.10511.（无词表事件边界：认知科学「人类无需预定义类别自然分割事件」的任务化；边界判据内置化的背书）
- Lynch, C. & Sermanet, P. **Hindsight Instruction Pairing**（Language Conditioned Imitation Learning over Unstructured Data）. RSS 2021. arXiv:2005.07648.（描述后置范式的源头：先有无结构行为流，事后配「哪条指令使该轨迹最优」）
- Marrella, A. et al. Automated segmentation of UI logs (RPA book ch.11, 2020)；Leno, V. et al. arXiv:2008.05782.（无分段 UI 日志的例程发现与噪声事件处理）
- Rogge-Solti, A., van der Aalst, W.M.P., Weske, M. **Repairing Event Logs** / Improving Documentation by Repairing Event Logs（2013–2014，流程挖掘）.（缺失事件的事后修复：随机 Petri 网过程模型 + trace alignment 定位缺失 + 贝叶斯网络补时间戳——修复依赖**跨轨迹习得的过程模型先验**；对应本提案「缺帧补全需要语料级先验」的演进候选定位，v1 不做）
- Yang, A. et al. **Vid2Seq**. CVPR 2023. arXiv:2302.14115.（dense video captioning「定位再描述」两段式）
- Lin, T. et al. **BSN**: Boundary Sensitive Network for Temporal Action Proposal Generation. ECCV 2018. arXiv:1806.02964；Bodla, N. et al. **Soft-NMS**. ICCV 2017.（时序候选「宁滥勿缺 + 后段精化/软排除」范式——对应本提案「不删元素 + verify 复裁回收」的谱系定位）
- 语音端点检测的 **hangover / 边界余量**惯例（ITU-T G.729 Annex B VAD、WebRTC VAD、两遍端点检测）.（防切头切尾的工业标准手法；本提案移植为 verify 评审证据中的「边界余量摘要」）
- Apache **Flink** EventTimeSessionWindows / Apache **Beam** Sessions（会话窗口原语，工业标准）。
- Anthropic Vision API 文档（多图上限：100 图/请求，>20 图单图 ≤2000px，32MB/请求；2026-07 检索）。

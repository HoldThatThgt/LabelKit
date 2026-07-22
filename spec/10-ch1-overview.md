# 1. 概述

## 1.1 背景与目标

数据采集系统持续产出两类原始数据：**纯文本数据**（对话、指令、文档等）与**设备屏幕数据**（屏幕截图 + UI 控件树文件对）。这些数据在进入下游（模型训练、评测集构建、数据资产入库）之前，需要完成四类加工操作：**去重**、**质量打分**、**自动标注**、**（可选）数据生成与二次校验**。人工完成这些操作成本高、吞吐低、标准不一致。

**LabelKit** 是一个基于 LLM API 的**无状态批处理命令行工具**，目标是把上述加工操作固化为一条可配置的流水线：输入一批 JSONL 数据（或截图+UI树文件对），输出一批结构由用户定义、经代码规则引擎保证结构正确性的 JSONL 标注结果；v1.4 起另支持**纯生成模式**（`run.mode="generate_only"`）——无输入数据时从配置种子池或条件化提示从零合成数据集，产物照常经过全套治理与结构保证（3.6.2）。工具本身**不存储任何数据**：每一批数据的全部中间态只存在于进程内存中，运行结束即丢弃，落盘的只有显式声明的输出通道：用户输出文件、rejects 文件、不含数据内容的运行报告，以及显式启用时的 trace 追踪日志（2.6、7.1；`rejects="full"` 与 `trace.content="full"` 档含数据内容，属用户显式选择并自担保留与清理责任）。

本文档是 LabelKit 的实现级设计规格，采用总分结构：第 2 章给出工具整体的规格、约束、架构与数据流；第 3 章逐模块给出职责、边界、输入输出、数据结构、API、算法流程与配置项；第 4–6 章给出跨模块的公共数据结构、配置文件与输入/输出格式的完整字段级定义；第 7 章定义日志系统与可观测性（含错误分类码规范，7.6）。所有功能点与算法均有顶会论文或工业级项目背书（见 1.5 节总表），不含凭空构思。

## 1.2 术语与缩写

| 术语 | 定义 |
|---|---|
| 记录（Record） | 流水线处理的最小数据单元。文本模态下为输入 JSONL 的一行；UI 模态下为一个「UI 树文件 + 截图文件」对。 |
| 批（Batch） | 一次流水线调度处理的记录集合，大小由 `run.batch_size` 决定；也是 QuRating 成对比较的采样池。 |
| 运行（Run） | 一次 `labelkit run` 进程的完整生命周期，处理一个输入路径的全部记录。 |
| Rubric | 质量评价准则集：若干条 criterion（准则），每条含 key、权重、描述、成对比较提示词与单点打分等级说明。 |
| QuRating | Wettig et al., ICML 2024 提出的数据质量评估算法：LLM 成对比较 + Bradley-Terry 模型拟合标量质量分 [1]。 |
| BT 模型 | Bradley-Terry 配对比较概率模型 [2]：P(i 胜 j) = θi/(θi+θj)。 |
| MinHash-LSH | 基于最小哈希签名与局部敏感哈希的近似 Jaccard 相似检索，文本近似去重的方法学标准 [3]。 |
| pHash | 感知哈希（perceptual hash），对图像内容生成 64-bit 指纹，汉明距离度量视觉相似度（工业标准，imagehash 库实现）。 |
| 结构引擎 | 本工具中保证 LLM 输出符合用户 JSON Schema 的代码规则引擎（M8），含确定性修复与有界 LLM 修复环。 |
| Profile | config.toml 中定义的一套 LLM API 接入参数（provider/base_url/model/并发/重试等），以名字被各阶段引用。 |
| UI 树 | 设备屏幕的控件层级结构（accessibility tree / view hierarchy）导出文件，JSONL 格式，每行一个控件节点。 |
| 纯生成模式 | `run.mode = "generate_only"`（v1.4）：无输入数据，M6 从配置种子池（`generate.seed_examples`）或无种子条件化提示从零产出样本，再走常规治理 / 标注管线（3.6.2、3.10.3）。默认模式为 `"process"`（加工既有数据）。 |
| Episode（序列记录 / 情节） | stream 模式的复合记录（v1.8）：M14 把同一目标导向活动的成员帧按序键收拢为一条 `kind = "sequence"` 的序列 Record（成员经 `members` 元组引用共享持有），作为一条普通记录走下游分类、打分、标注与评审（3.14、4.1）。 |
| 会话（Session） | 摄取层按 `[stream]` 规则（时间间隙 gap / 分区键 key / 长度与时长上限）从有序记录流切出的候选窗口——流处理标准的 session window 原语的对应物 [55]；是 M14 语义精化的输入单元，切批改整会话装箱保证会话不跨批（3.2.8、3.10.3）。 |
| 转移（Transition） | 序列内相邻两帧 ⟨s_i, s_{i+1}⟩ 之间发生的单个语义动作：M15 经 LLM 推断为结构化对象 {action_type, target, value, description} 写入 `item.transitions`，转移数 = 成员数 − 1（3.15、4.1）。 |
| stream 模式 | `segment.enabled = true` 的运行形态（v1.8）：摄取按 `[stream]` 声明排序与会话化，链序为 segment → stitch → dedup → classify → extract → quality → annotate → verify（stitch 为 v1.9 增位，默认关）；默认关闭，关闭时数据产出与 v1.7 逐字段一致（`_meta.stream: null` 除外）（2.3.1、3.10.3）。 |
| 线索（Thread） | v1.9 stream 模式的顶层工作单元：M16 把同一目标导向任务被穿插切开的碎片保守缝合所得（三级结构 thread ⊃ fragment ⊃ step），承载体仍是一条 `kind = "sequence"` 的序列记录（幸存信封 Record 重绑、id 不重算，`thread_id` = 幸存信封 record.id），作为一条普通记录走下游判重、分类、打分、标注与评审（3.16、4.1）。未被缝合的 episode 即单碎片线索；`stitch.enabled = false` 时线索与 episode 天然同值。 |
| 碎片（Fragment） | 线索的成员分段（v1.9）：会话序上连续归属同一线索的成员帧区间——缝合前的一个 episode 或一个救援短段；以 `_meta.stream.fragments[]`{order_span, member_count, cause, source_episode} 溯源，cause ∈ "origin" \| "resumed" \| "rescued"（3.16、6.3）。 |
| 接缝（Seam） | 多碎片线索中相邻两碎片的拼接处（v1.9）。判据：拼接对的会话序间隙含 ≥1 个归属其他线索的帧（间隙仅含噪声帧/本线索救援帧时不是接缝，该对照常摘取，3.16.4）；接缝步由 M15 零 LLM 机械占位（`action_type="app_switch"`、`detail.kind="thread_seam"`、步行内 `resumed=true`，3.15.4），位置经 `seam_indexes` duck 标承载（左成员下标坐标，4.1）。 |

## 1.3 设计原则

| 原则 | 含义 | 来源 / 背书 |
|---|---|---|
| 无状态批处理 | 工具不持有跨运行状态，不落盘中间态；一次运行 = 读入 → 处理 → 写出 → 进程退出，内存即全部状态。 | 产品需求「工具不存储数据」；Unix 过滤器模型 |
| 算子化流水线 | 每个处理阶段是签名统一、可独立开关的算子（Stage），编排器只做组合与调度，不含业务逻辑。 | Data-Juicer 算子体系（SIGMOD 2024）[4]；distilabel Step/DAG [5]；Dolma toolkit [6] |
| LLM 输出不可信 | 一切 LLM 输出必须经代码校验后才能进入下游：结构由 JSON Schema 校验，数值由解析器白名单校验。 | 结构化输出工业实践（OpenAI Structured Outputs、instructor 修复环）[7][8] |
| 配置即契约 | 工具级配置（config.toml）与工程级配置（project.toml）在启动时全量校验、快速失败；运行期不再出现配置错误。 | 十二要素应用配置原则的文件化变体（按需求不使用环境变量，API Key 除外） |
| 记录级隔离 | 单条记录的任何失败不影响其余记录：失败记录进入 rejects 通道并计入报告，运行继续。 | Dolma / NeMo Curator 大规模管线的容错惯例 [6][9] |
| 可复现 | 相同输入 + 相同配置 + 固定 seed + temperature=0 时，除 LLM 服务端非确定性外，配对采样、去重判定、流程路径完全可复现。 | QuRating 开源实现的实验可复现要求 [1] |

## 1.4 需求映射表

下表将原始需求逐条映射到本文档的承载章节，供评审时核对完整性。

| 原始需求 | 承载章节 |
|---|---|
| 使用 LLM 对采集数据进行自动标注 / 去重 / 打分 / 生成 | M5（标注）、M6（生成）、M3（去重）、M4（打分）；总流程 2.3 |
| 输入数据为纯语言 / 设备截图+UI树 两种 | M2 数据接入；输入格式规格 6.1–6.2 |
| 质量分类器使用 QuRating 算法；Rubric 用户提供 + 系统默认 | M4；默认 Rubric 附录 A |
| LLM API 信息作为工具静态配置 | M1、M9；config.toml 规格 5.1 |
| 工具不存储数据，批中间态标注完成后丢弃 | 2.1 / 2.6 非功能约束；M10 批生命周期 |
| 输出结构用户定义；LLM 输出 + 代码规则引擎保证结构正确 | M8 结构引擎；输出格式 6.3 |
| 生成 / 二次校验可选，由 LLM 完成 | M6 生成模式、M7 二次校验 |
| 工具配置 config.toml；Rubric 与单次工程配置 project.toml；不用环境变量（API Key 除外） | 2.5 配置体系；5.1–5.2 完整规格 |
| 输出统一 JSONL；输入为 JSONL 路径；UI 模态为 uitree_<index>.jsonl + image_<index>.jpg/png 文件对（可不同子目录） | M2 配对算法；6.1–6.3 |
| 模块职责/功能/边界清晰 | 2.2 模块清单；第 3 章每模块「职责与边界」小节 |
| 功能点/算法需顶刊论文或工业项目背书 | 1.5 背书总表；各模块「背书」框 |
| 不清晰/多方案点与用户对齐 | 1.6 已对齐决策记录 |
| （v1.1 评审补充）日志系统：行为记录/格式/打分思考支撑 rubric 优化与质量分析 | M12（3.12）；第 7 章；`[trace]` 配置 5.2 |
| （v1.2 评审补充）算子对输出集的影响分析；定量优选；多模型/多品味生成；算子算法增强 | 2.3.2；3.4.3 选择机制；3.6.2；8.4 演进路线总表 |
| （v1.4 评审补充）无输入数据场景下直接生成数据（纯生成模式） | `run.mode`（5.2）；3.6.2 种子来源分支；3.10.3 纯生成行；2.3.1 组合④ |
| （v1.7 评审补充）分类与按类条件化路由：加入分类算子，根据分类执行不同的打分、标注与生成；多类命中可流向多个管线（单/多分类开关锁定） | M13 分类（3.13）；按类条件化 3.4.3 / 3.5.2 / 3.6.2 / 3.7.2；multi 扇出 3.13.4 与契约 ②a（4.3）；`[classify]` / `[class.*]` 配置 5.2 |
| （v1.8 评审补充）时序流分割与动作摘取：数据按时间排序输入时对流做语义分割（episode 形成与噪声帧剔除）并摘取流中的动作数据，标注算子为序列打「用户在做什么」的任务标签 | M2 会话化（`[stream]`，3.2.8）+ M14 segment（3.14）+ M15 extract（3.15）+ 下游序列适配（M3/M13/M4/M5/M7，3.3.3 / 3.13.3 / 3.4.3 / 3.5.2 / 3.7.2）；轨迹 rubric 附录 A.3；契约 ②b（4.3） |
| （v1.9 评审补充）活动结构：x 小时自然使用流标注出「n 帧组成的 m 个工作单元」——同一任务被穿插切开时缝合为一个线索（串联/单交叉/多交叉/噪声四类标定），短段业务尾帧可救援，噪声帧过滤不入线索 | M16 stitch 线索缝合（3.16，链序 segment 之后、dedup 之前）+ 下游线索适配（M3/M4/M5/M7/M15，3.3.3 / 3.4.3 / 3.5.2 / 3.7.2 / 3.15.4）；三级结构 thread ⊃ fragment ⊃ step 与接缝占位（3.16.4、3.15.4）；契约 ②c（4.3）；`[stitch]` 配置 5.2 |
| （v1.10 评审补充）Console 实时面板（TUI）：交互终端下以双区内联面板实时呈现批进度、流水线段棋盘、状态账与 LLM 用量/密钥池/熔断（工业调研裁决品类：批处理工具不做全屏 TUI）；非 TTY / CI / jsonl 下保持 v1.9 行为（三层回归锚 7.8） | 7.7 三态 console 规格；`[console]` 配置 5.1 与 `--console`（2.4）；ProgressListener 五回调旁路（3.12.3）与 snapshot（3.9.2）；依赖面增 rich（2.6）；裁决 U1–U27 见 `docs/dev/SPEC-tui-console.md` §2 |

## 1.5 算法与工程背书总表

| 功能点 | 采用方案 | 背书（论文 / 工业项目） |
|---|---|---|
| 质量打分（主模式） | LLM 成对比较 + Bradley-Terry 拟合标量分 | QuRating, ICML 2024, arXiv:2402.09739 [1]；BT 模型 [2]；MM 拟合算法 Hunter 2004 [10] |
| 质量打分（低成本模式） | 单点加性 rubric 打分（0–5 逐条累加） | FineWeb-Edu, NeurIPS 2024 D&B, arXiv:2406.17557 [11] |
| 精确去重 | 规范化内容 SHA-256 哈希 | Dolma toolkit 去重设计 [6]；业界通行做法 |
| 近似去重 | MinHash-LSH（字符 n-gram shingle，Jaccard 阈值） | Lee et al., ACL 2022, arXiv:2107.06499 [3]；内置于 Dolma [6]、Data-Juicer [4]、NeMo Curator [9] |
| 图像去重 | pHash 感知哈希 + 汉明距离阈值 | imagehash（工业标准库）；数据集治理通行做法 [9] |
| LLM 自动标注 | 提示词组装 + 结构化输出 + 多模态（截图+序列化UI树） | distilabel（Argilla，工业）[5]；Autolabel（Refuel，工业）[12]；GUI 数据 LLM 标注管线：ScreenAI [13]、GUI-360 [14]、AgentTrek, ICLR 2025 [15] |
| 标注自一致（可选，v1.2） | self-consistency：同一记录 n 次独立采样（n≥3 奇数，`annotate.sc_temperature`）+ 字段级多数投票，全体分歧回退首样本并计数 | Self-Consistency, Wang et al., ICLR 2023, arXiv:2203.12171 [33] |
| UI 树 + 截图输入表示 | 截图图像 + accessibility-tree 线性化文本同时输入 VLM | ScreenAI screen-schema 线性化 [13]；OS-Atlas [16]；Ferret-UI [17] |
| 数据生成（可选） | 以种子记录为示例的自举生成 + 相似度过滤 | Self-Instruct, ACL 2023, arXiv:2212.10560 [18]；Evol-Instruct / WizardLM, ICLR 2024 [19]；distilabel 任务库 [5] |
| 多样性生成（可选，v1.2） | 多 LLM 混合（round_robin 轮转 / weighted 加权抽样）+ `[[generate.styles]]` 风格模板条件化提示 | Persona Hub, arXiv:2406.20094 [34]；Cosmopedia（HuggingFace，工业）[35]；model collapse 缓解：Shumailov et al., Nature 631, 2024 [36]；distilabel 任务级 LLM 绑定 [5] |
| 二次校验（可选） | LLM-as-a-Judge 独立评审 + 有界修复回路 | Zheng et al., NeurIPS 2023, arXiv:2306.05685 [20]；Self-Refine, NeurIPS 2023 [21]；Constitutional AI 批评-修订 [22] |
| 结构正确性保证 | JSON Schema (draft 2020-12) 校验 + 确定性修复 + 有界 LLM 修复环 + 供应商原生结构化输出 | OpenAI Structured Outputs（工业）[7]；Outlines 约束解码 [23]；JSONSchemaBench [24]；instructor / json-repair（工业）[8] |
| 流水线架构 | 统一签名算子 + 声明式配置组合 | Data-Juicer, SIGMOD 2024 [4]；distilabel DAG [5]；Dolma toolkit [6] |
| 评审偏差缓解 | 成对比较随机顺序、判分与理由分离、平局处理 | LLM-as-a-Judge 位置偏差/冗长偏差分析 [20]；QuRating 提示设计 [1] |
| API 调用容错 | 指数退避 + 抖动重试、并发信号量限流 | AWS/Google SRE 重试规范（工业标准）；distilabel/NeMo Curator 客户端实现 [5][9] |
| LLM 调用追踪与结构化事件日志 | 双通道日志：stderr 运行日志 + trace JSONL 事件流（一行一事件、通道过滤、四档脱敏）；LLM 调用事件字段命名对齐 OTel GenAI 语义约定（仅命名对齐，非实现依赖） | OpenTelemetry GenAI 语义约定（Development 状态，非 stable）[27]；LangSmith（LangChain，工业）[28]；W&B Weave（工业）[29] |
| 评审驱动的 rubric 迭代 | trace 记录逐次 pairwise 裁决与理由 → 人工审阅与指标诊断 → 修订准则 → 小样本重跑对比（7.5 闭环） | EvalGen 的 criteria drift 结论, UIST 2024, arXiv:2404.12272 [30]；CritiQ 从偏好挖掘质量准则, ACL 2025, arXiv:2502.19279 [31] |
| 纯生成模式（无输入合成） | 配置种子池自举（单遍）/ 无种子 instruction×style 条件化 + 显式量目标 | Self-Instruct 以 175 条人工种子自举 [18]（种子池形态）；Persona Hub [34]、Cosmopedia [35]（无种子条件化形态） |
| 评审鲁棒性增强 | 多评审团多数票（奇数个异构评审 per-criterion 投票）+ 双顺序裁决（正反两序一致才记胜） | PoLL（Verga et al., 2024）, arXiv:2404.18796 [32]；LLM-as-a-Judge 位置偏差分析, Zheng et al., NeurIPS 2023 [20] |
| 数据分类（可选，v1.7） | LLM 封闭集分类：类别表词表经内部 Schema enum 硬校验 + 可选 self-consistency 投票 + 兜底类 | Autolabel classification / multilabel 任务的 labels 词表校验（工业）[12]；InsTag LLM 指令语义打标, ICLR 2024 [38]；NeMo Curator 分类器管线阶段（工业）[40]；Self-Consistency [33] |
| 按类条件化路由（v1.7） | 分类结果落记录级属性（`item.classification` / `_meta.classification`），下游算子按类取有效配置，管线拓扑不变 | Nemotron-CC 质量分档路由不同合成管线, arXiv:2412.02595 [37]；NeMo Curator `bucketed_results` 标签字段路由（工业）[40]；Dolma tagger→attributes→mixer 解耦 [6] |
| 按类数据构造（v1.7） | 按类种子池 + 按类生成指令/风格（`[class.<name>.generate]`），配按类 rubric 与标注指令 | Tülu 3 按核心技能分治的数据构造与 per-skill 合成, arXiv:2411.15124 [39]；Nemotron-CC 每档不同改写 prompt [37]；类内配套沿用 Persona Hub [34] / Cosmopedia [35] |
| 多标签扇出（可选，v1.7） | `classify.assignment = "multi"`：命中 k 类扇出 k 个单标签兄弟信封，各自独立走按类管线并各产出一行 | InsTag 指令多意图的多标签打标形态 [38]；distilabel 路由函数的一批多下游并行产出（`sample_n_steps`）[5]；Autolabel multilabel 的「标签集合 ⊆ 词表」输出契约 [12] |
| 时序流会话化（v1.8） | `[stream]` 声明排序键 + gap/分区键/长度时长上限切候选会话，整会话装箱保证 episode 不跨批（3.2.8、3.10.3） | Apache Flink `EventTimeSessionWindows` / Apache Beam `Sessions`（工业标准）[55]——取用：session window 原语的规则层照抄（inactivity gap + 分区键 + 硬上限），纯代码零 LLM 成本 |
| 轨迹数据工程整体形态（v1.8） | 转移摘取 → 任务标注 → 轨迹打分的三段式管线（extract → annotate → quality） | OS-Genesis, ACL 2025 [41]——取用：reverse task synthesis 三段式（转移标注 → 任务聚合 → trajectory reward model 打分）直接映射为本管线三工位；其 TRM 为 1–5 五级，附录 A.3 的 0–5 六级为家规改制 |
| 动作摘取范式（v1.8） | LLM zero-shot 充当运行时逆动力学模型（IDM）：一次调用喂前后两帧，利用非因果优势推断其间动作（3.15） | VPT, NeurIPS 2022 [42]——取用：「从相邻状态推断动作」是被大规模验证的独立工序；GUI-Shift, ICLR 2026 [42] 为 IDM 范式在 GUI 域的最新自监督形态 |
| 确定性归并 + LLM 语义化分工（v1.8） | 控件树 diff 代码侧确定性计算作 extract 证据；提示词锚定「动作前最后稳定帧 / 动作后首个稳定帧、多低层事件归并为单个语义动作」 | OpenCUA / AgentNet [43]——取用：Action Reduction（低层事件确定性归并）与 State-Action Matching（稳定帧锚定、防未来信息泄漏）两层分工移植入 extract 模板（CONTRACTS §10.10） |
| 流后分段再打标（v1.8） | 先有记录流、事后自动识别子序列并打标（hindsight relabeling 的自动化） | AITW, NeurIPS 2023 D&B [44]——取用：「先有流、后分段再打标」是 GUI 轨迹数据工程的标准姿势，本设计为其 LLM 自动化版本 |
| episode/step 两级结构与动作词表（v1.8） | episode 级任务标签（用户 Schema）+ step 级动作（`_meta.stream.steps`）；转移数 = 成员数 − 1 | AndroidControl, NeurIPS 2024 D&B [45]——取用：两级标注结构直接采用；8 值动作词表全集采纳（无裁剪）+ other 兜底，「动作数 = 截图数 − 1」即 extract 调用量公式 |
| 跨 App episode 一等公民（v1.8） | 边界判据以「可见任务实体延续」而非换 App 判段（advances 关系值） | GUI-Odyssey, ICCV 2025 [46]——取用：跨 App 导航流全集皆是、为此专门引入 RECENT 应用切换动作，佐证 `app_switch` 入词表与「实体延续即同任务」判据 |
| 语义边界裁决模板（v1.8） | 三步演绎：双向上下文概括 → 五值封闭集关系分类 → 演绎查表映射边界/噪声（LLM 不直接答边界，3.14） | 话题分割谱系 TextTiling → Embed-KCPD → Def-DTS [47]——取用：Def-DTS 消融证明半结构（仅双向概括）比裸问题差、完整三步最优，而边界信号清晰场景裸判决可胜全套（S32）；其按数据集改意图池的先例背书本设计的 5 关系词表按域定制 |
| 无词表事件边界（v1.8） | 边界判据内置、任务无关：粒度锚定「完整任务」层级 + 注意力锚定前台 App/窗口，用户零 prompt 可用 | GEBD, ICCV 2021 [48]——取用：taxonomy-free 边界任务可定义、可标注、中等共识可达（5 人多评协议数据），"1 level deeper" 与 dominant subject 两原则写死在判据模板 |
| 描述后置（v1.8） | 「用户在做什么」由 annotate 在分段之后产出，任何人无需先验描述边界 | Hindsight Instruction Pairing, RSS 2021 [49]——取用：先有无结构行为流、事后配指令的范式源头，回答「边界判据不需要任务描述」的需求方疑虑 |
| UI 日志分割问题定义（v1.8） | 分段 = 边界发现 + 噪声剔除（无监督、无任务描述）；interruption → noise 词表值 | RPA UI 日志分割：Marrella et al.；Leno et al. [50]——取用：「显式处理不属于任何例程的噪声事件」的问题定义直接沿用（noise_residue 准则同源）；交错例程难变体 v1 不做（8.4） |
| 缺帧不补全（v1.8） | verify 缺帧三级判定的「无处可寻」档仅标 `capture_gap`，不做补全 | Repairing Event Logs, Rogge-Solti et al. [51]——取用：缺失事件修复依赖跨轨迹习得的过程模型先验，佐证缺帧补全列演进候选（8.4）而非 v1 内置 |
| 定位与描述分立（v1.8） | segment（时序定位/分段）与 annotate（描述/打标）拆成两个工序 | Vid2Seq, CVPR 2023 [52]——取用：dense video captioning 的「先 temporal localization 再 captioning」两段式先例 |
| 宁滥勿缺 + 后段精化（v1.8） | 批内不删元素、噪声帧只改状态；verify 复裁可回收（软排除而非硬删，②b） | BSN, ECCV 2018；Soft-NMS, ICCV 2017 [53]——取用：时序候选「宁滥勿缺 + 后段精化/软排除」范式对应「只改状态不删元素 + 成员回收」的谱系定位 |
| 边界余量证据（v1.8） | verify 评审证据含段边界外前后 k=2 帧的摘要及其去向（`[边界余量]` 段，3.7.2） | 语音端点检测 hangover 惯例（ITU-T G.729 Annex B VAD / WebRTC VAD）[54]——取用：防切头切尾的工业标准手法移植为评审证据段，零额外 LLM 调用 |
| 多图请求上限（v1.8） | `annotate.sequence_frames ∈ [2,100]`、默认 20；>20 联动 `max_image_px > 2000` 警告（3.1.4） | Anthropic Vision API 文档 [56]——取用：100 图/请求、>20 图单图任一边 >2000px 为 400 硬拒（非缩放）、32MB/请求；OpenAI 1500 图/512MB 故不设独立上限 |
| 整段单调用对照形态（v1.8） | v1 保留 hybrid 滑窗——window ≥ 会话长时天然退化为整段单调用，长会话建议调大 window | GUIDE [57]——取用：GUI 域验证最充分的 LLM 分段形态是纯文本动作序列整段一次调用（99.4% 段可用率、50–80 步无衰减）；其整体式 judge 随轨迹变长退化的数据（>20 步降信任）入手册调优指引 |
| extract 可靠性预算（v1.8） | 风险面明写每步 zero-shot 错误率 20–30% 的级联；缓解 = 树 diff 证据 + verify 缺陷路由 + quality 结构分 + `extract.by_type` 分布可观测 | Watch & Learn, CVPR 2026 [58]——取用：zero-shot MLLM 动作标注 **70.5%** vs 专训 IDM 91.7% 的直接对照钉死可靠性预算；「噪声标注主动伤害下游」佐证 fail-closed 质量门 |
| diff 注入可消融（v1.8） | `extract.include_diff` 开关（默认开，可关做 A/B 对比） | Sharingan [59]——取用：像素 diff 显式注入实测负结果、按动作类型精度极不均衡（click 0.94 / drag 0.40）——结构化树 diff ≠ 像素 diff 且工程实践正面，但**方向未定** ⇒ 做成开关而非硬编码正收益 |
| 屏幕流→轨迹的 2026 工业路线（v1.8） | prompted LLM 滑窗仍是量产形态之一（v1 采用）；专训边界模型列本地化演进 | VideoAgentTrek, ICLR 2026；Video2GUI [60]——取用：专训 7B 边界模型与 prompted Gemini 滑窗两条路线并存（滑窗未被取代）；两者均「动作先于/伴随分段」，extract-先行次序据此列演进候选（8.4） |
| 轨迹判分信度护栏（v1.8） | 机械锚点（extract 副读数注入裁决 prompt）+ stream 默认只打分不筛 + 分数按 episode 长度可观测 | Web-Shepherd, NeurIPS 2025；GUI-Shepherd；AgentRewardBench [61]——取用：zero-shot LLM 轨迹判分高方差、无单一模型通吃，检查清单分解是保命组件——机械锚点与 checklist 思想同构 |
| 统一动作空间对齐（v1.8） | `action_type` 枚举 11 值 = AndroidControl 全集 ∪ UI-TARS-mobile 增量（`drag`、`app_switch`）+ other 兜底 | UI-TARS（工业）；UIPro, ICCV 2025 [62]——取用：2025–2026 统一移动动作空间共识含 drag 与应用切换/recent，跨 App episode 场景频率不可忽略 |
| 树可靠性护栏（v1.8） | 帧摘要贫瘠护栏：可见文本节点为零或摘要趋零 ⇒ 计 `digest_poor_frames` + WARN + 手册指引为 `segment.llm` 配置 `supports_vision=true` 的 profile（v1.11/V4 改写——`use_vision` 键已移除，窗口附图由能力推导 `vision_resolved` 决定，3.14） | Do GUI Agents Believe Their Eyes?（引 CLAY ghost-node 统计）[63]——取用：Android 10.6% 结构节点无视觉呈现、37.4% 屏幕含 ghost node——树贫瘠不是长尾，须主动可观测而非被动抽读 |
| 线索缝合问题形态（v1.9） | 屏幕流 =「多线索穿插 + 噪声」的形式化；缝合以线索（thread）为产出单元、错缝以乘法惩罚度量 | PIRA-Bench [64]——取用：任务子轨迹 = **非连续帧子集**的问题定义；噪声消融证实过连接偏差跨模型家族共享（precision 92→51 而 recall 反升，原文 "trigger-happy"）；S_final = F1 × FPS_norm 与负样本协议进真机门禁（3.16.7）。注：0 被引新基准，自报数字权重打折 |
| 保守偏置合取（v1.9） | 并入需 LLM 判 resume ∧ 机械先验命中（App 交集 / 实体重叠 / 返回同一页面，析取三腿 + 超期降格）；口头置信度不入门槛 | 过连接量化：IdentifyMe / CORRECT-DETECT [76]——取用：LLM 回避「以上皆非」宁可硬连、准确与弃权此消彼长；「返回同一页面」腿机理 = cue-guided resumption（Altmann & Trafton / Trafton et al.）[80]；置信度饱和 [79] |
| 单调选池判定（v1.9） | 每候选一次调用：池内开放线索摘要卡（最近活跃降序）+ 候选卡 → `thread_ref \| new`，顺序贪心 | GreedyDisentangle（Takada & Mori, LaCATODA@AAAI-26）[87]——取用：同任务同形制 SOTA 先例（簇摘要呈现 + LLM 归簇或判 new + 顺序贪心，全指标超 per-pair）；对话解缠谱系（贪心链接标准解码、并发线程 ≤3 占 46.4%、VI / 1-1 overlap / link-F1）[75]；呈现序与位置扰动测试防位置偏差 [77] |
| 有界二遍复评（v1.9） | 一遍结束后对单碎片线索逐个复评（池 = 其他线索活视图），修正顺序贪心漏缝；预算 ≤ 单碎片线索数 | FAMER / Gruenheid et al. [74]——取用：无修复贪心劣于 batch 且次序依赖、**n=1 局部重聚类即追平 batch**；merge-only 是增量法中质量最差；subsequent-context 有效 [87]；更重簇修复机器（LLM-CER / GraphCR / Alper）已评估按规模不采 [78] |
| 池容量与时间衰减（v1.9） | `max_open = 4`（挂起窗口均值 3 + 1 活跃）；`stale_gap_steps` 双职：先验降格 + 池满逐出优先腿；封闭 ≠ 终结 | Iqbal & Horvitz, CHI 2007 [81]——取用：真实桌面日志挂起窗口均值 3（S.D.≈2）、27% 挂起 >2h 才恢复；时长分布特征 +11.36%（CASAS）[66]；移动域仅 22.6% 任务穿插佐证宽松上界 [90]；working spheres 与手机中断/回访人因基线 [82] |
| 短段救援（v1.9） | `below_min_len` 短段按连续 run 重组先进候选池：命中并入 + 帧翻转，未命中维持 dropped_noise，永不开新线索 | Iqbal & Horvitz [81]——取用：切换前收尾动作密集（段落完成率 0.78/min → 切换前 10.9–12.8/min）——任务收尾帧天然易成短段、聚集在切换点旁的文献级机理 |
| 摘要卡证据面（v1.9） | 结构化摘要卡（App 集合 · 任务名 · 首末帧摘要 · 变更提示 · 跨度）替代全量帧历史；判定对 = 线索尾帧摘要 × 候选首帧摘要 | resumption 判定单元 =「挂起尾 × 恢复首」对（CIGAR）[65]——取用：帧摘要级降格承载（3.16.3）；window title 最强单特征（85.57%）与多窗聚合（SWISH / TaskPredictor）[83]；精选紧凑上下文反超全量原始历史（+10.4 pt、token 少 8×）与 summarization drift 风险命名（Engram / Memori / 综述）[88]；两级任务组匹配工业近例 Log2Plan [84] |
| 缝合稳定性 votes（v1.9） | 单模型 n 采样、(verdict, thread_ref) 完整判定严格多数决（默认 votes=1 不启用）；不采多模型评审团 | Self-Consistency [33]——取用：一致率是可靠的不确定性信号（votes 是置信度门槛的正规替代 [79]）；边界：高自一致处过度自信、votes 治方差不治偏差 + 评审团有效独立票仅 ≈2 [89]；跨模型共识修不了共享偏差（within-model 0.68 > cross-family 0.47）[86]——对照 PoLL [32] 评审团路线不采（8.3 O8） |
| 三级层级与身份链（v1.9） | thread ⊃ fragment ⊃ step；帧单一归属——交叉用「平面分段 + 线索身份」表达，不引入帧多重归属/区间树 | Ego4D Goal-Step [69]——取用：goal⊃step⊃substep 三级 + `is_continued` 续接标志的直接先例；GUI 域层级标配 AndroidControl [45]；视频域层级范式（FineGym / Breakfast）[71]；帧多标签先例（MultiTHUMOS / Charades）[72] 评估后**否决采纳**；嵌套与区间关系形式语义底座（HHMM / Allen）[73] |
| 线索命名后置（v1.9） | `task_name` 由池空判定自举、滚动更新（工具内部结构，进 trace 与判定证据；用户任务标签仍由 annotate 产出） | OS-Genesis [41] / NNetNav [70]——取用：自然流 → 事后反推任务标注范式；「可命名性」剪枝判据 |
| 问题域现状与护栏（v1.9） | interleaved 解缠无域内基线：护栏 = 保守合取 + 二遍复评 + 负样本协议 + 真机门禁（错缝 FPS = 0 验收线） | Robotic Process Mining [67]——取用：UI 日志 interleaved 解缠 = open challenge、全局法依赖「例程重复」前提（2025 复核不变 [85]）；学术解缠系列与三家产品均无穿插解缠 + 通信类 App 天然噪声名单 [68]；跨 App 单目标轨迹形态 [46] |
| 上下文预算：窗口声明与预算公式（v1.11） | `[llm.<name>]` / `[embedding.<name>]` 用户声明 `context_window`（0 = 未声明 = 该 profile 预算关闭）；`input_budget = context_window − max_output_tokens − margin`，`margin = max(256, ceil(0.10 × context_window))`（3.9） | LlamaIndex PromptHelper [91]——取用：`context_window − prompt − num_output` 预算式与 repack 装填；Claude Code auto-compact [92]——取用：`contextWindow − min(maxOut, 20k) − 13k` 的「预留输出 + 固定 buffer」结构（各来源触发百分比不一、结构一致）；OpenAI Codex CLI [93]——取用：`model_context_window` 用户声明 + 钳制 + 输出预留 + 比例边距的完整同构 |
| 条数上限 + 预算动态装填（v1.11） | 条数型参数（`segment.window` / `annotate.sequence_frames` / `generate.seeds_per_call`）降级为**上限值**，按逐项实际 est 贪心装填；调用次数以静态最坏值 w_min 报上界（3.9、3.14、V9/V12） | Qwen-VL 官方评测口径 [94]——取用：帧数上限 × 总 token 预算双约束、`max_pixels = 预算 // 帧数`（帧数是上限、预算守恒）；NeMo Curator Nemotron-CC DocumentJoiner [95]——取用：按 `max_segment_tokens` 的 token 装填（"maximize input utilization"）是数据管线同类算子 |
| 图片成本测量-反应式三层（v1.11） | 先验装填（provider 公式仅作首批先验）→ 溢出裁帧保清重试 → `usage.prompt_tokens` 在线校准（窗口化 max 滤波 + 0.85 安全系数、批冻结快照）（3.9、V17–V20） | ABR / 拥塞控制 measure-don't-model 范式 [96]——取用：BBA 纯缓冲选档（稳态不需容量估计、启动期必须要）与 BBR windowed-max 测量式建模取代丢包反应——校准器滤波蓝本；Cline [97]——取用：生产级上下文管理刻意反应式（"accurate token counting varies by model/tokenizer"）+ 企业网关 `usage: null` 实证（缺样本兜底的依据） |
| 判审触发裁帧升清重试（v1.11） | `verify` fail ∧ policy="repair" 的修复重标注换档：关键帧减半 + 分辨率上探一档（≤ `max_image_px`），单向有界（3.5.2、3.7.3、V21） | 置信度触发递归变焦谱系 [98]——取用：Zoom Eye 置信分驱动图像树递归变焦（训练无关）、V*/SEAL 置信度低于阈值即递归切 patch 搜索（7B+搜索 75.4% vs GPT-4V 55.0%）、UI-Zoomer 置信门控变焦（GUI grounding +4.2–13.4%）——「低置信 → 定向升清重试」的学术与 GUI 域背书 |

## 1.6 已对齐的设计决策

以下多方案设计点已与需求方沟通对齐（对齐日期见各行，早期各轮为 2026-07-02），本文档按对齐结论展开：

| 设计点 | 候选方案 | 对齐结论 |
|---|---|---|
| QuRating 实现形态 | 仅 pairwise+BT / 仅 pointwise / 双模式 | 双模式可配：默认 pairwise+Bradley-Terry（忠实 QuRating [1]），提供 pointwise 加性打分（FineWeb-Edu [11]）作为低成本模式，project.toml 一键切换，共用同一套 rubric。 |
| 去重层级 | 仅精确 / 精确+MinHash / 三级含语义去重 | 精确 + MinHash-LSH（纯本地零 API 成本），图像走 pHash；语义级重复交由质量打分环节间接处理。SemDeDup 列为开放问题（8.3）。v1.2 更新：用户决策推翻本结论，SemDeDup 落地为可选第④级（默认关，3.3.3）；决策溯源见 8.3 O1。 |
| 形态与语言 | Python CLI / Python 库+CLI / 其他语言 | Python 3.11+ 单一 CLI 工具，与 distilabel/Data-Juicer/Dolma/NeMo Curator 同栈 [4][5][6][9]。 |
| 输出结构描述格式 | JSON Schema / TOML 简化 DSL / 两者 | 标准 JSON Schema (draft 2020-12)，内嵌于 project.toml 或引用外部 .json 文件；LLM 侧直接作为结构化输出约束，规则引擎侧用 jsonschema 库校验，零转换层 [7][23][24]。 |
| 定量优选（v1.2 对齐，2026-07-02） | 流式批内 top_ratio / 全局两阶段精确 top-K / 双支持 | 仅批内 top_ratio：`quality.selection = "top_ratio"`（`quality.top_ratio` ∈ (0,1]，与 threshold 互斥，M1 校验，3.4.3）提供流式近似定量；全局精确定量列入演进路线 O6（8.3）。 |
| 生成补齐回路（v1.2 对齐，2026-07-02） | 本版实现 / 列入演进路线 | 列入演进路线 O6（8.3）：设计草案已给出补齐环与三重停止条件（含本轮合格率下限——防 model collapse [36]），与全局定量一并立项。 |
| 多 LLM / 多品味生成（v1.2 对齐，2026-07-02） | 单 LLM（v1.1 现状）/ 多 profile 混合 + 风格模板 | 进规格：`generate.llms` 数组（取代 v1.1 单值键 generate.llm）+ `generate.mixture`（"round_robin" \| "weighted"，weighted 配 `generate.weights`）+ `[[generate.styles]]` 风格模板（name、prompt），规格见 3.6.2；多样性思想背书 [34][35]。 |
| 算子算法增强（v1.2 对齐，2026-07-02） | ① 多评审团投票 ② 双顺序裁决 ③ self-consistency 标注 ④ SemDeDup 语义去重 | ①②③④ 全部收录为默认关闭的可选配置（总表见 8.4；背书 [32][20][33][26]）；其中 ④ 修订 v1.0「语义去重不做」的对齐结论（见上文去重层级行尾注），经 `[embedding.<name>]` profile 落地为 dedup 可选第④级。 |
| 模块拆分与重编号（v1.3 对齐，2026-07-02） | 保持 M5 复合模块 / 拆分且编号稳定（生成 = M12）/ 拆分且按流水线位置全量重编号 | 拆分且全量重编号：标注与生成职责正交（基数保持的增列 vs 基数增加的合成），按 2.2 模块边界准则应各自独立；生成独立为 M6（3.6），原 M6–M11 顺移为 M7–M12。配置键、数据结构与 API 零变更，属纯文档结构调整。 |
| 纯生成模式（v1.4 对齐，2026-07-02） | 支持两种形态 / 仅种子池 / 演进路线 / 明确非目标 | 支持，两种形态进规格：配置种子池 `seed_examples`（Self-Instruct 形态 [18]）与无种子条件化 + `standalone_count`（Persona Hub / Cosmopedia 形态 [34][35]），单遍执行不引入 O6 循环；工具定位由「数据加工器」扩展为「亦可从零起步的数据生产器」（1.1、2.1 同步修订）。 |
| 多 API Key 负载均衡（v1.6 对齐，2026-07-03） | ① 范围：仅同 profile 多 key / 端点镜像池；② 熔断中止时 .part 交付与否；③ 全池冷却驻留超限的处置：直接硬熔断 / 记录失败累积；④ 配额型 403 的归类：密钥禁用 / 冷却 / 错误体嗅探；⑤ 报表中密钥身份：环境变量名 / 位置别名 | ① 仅**同 profile 多 key、单 endpoint**（密钥池，3.9.3）——端点镜像池明确排除：同模型不同部署在 temperature=0 下仍有数值漂移，会翻转 pairwise 裁决与语义去重边界判定、污染 7.5 同种子翻转率指标（决策溯源见 8.3 O7）；② 熔断中止**交付**已完成批（熔断交付，3.10.3、3.11.2、6.4）；③ 驻留超限（`run.max_park_s`，默认 3600s）按重试耗尽**记录失败并计入熔断窗口**，不直接硬熔断；④ 配额以 403 形态出现按认证禁用该密钥处理，不做 provider 特定的错误体嗅探；⑤ 报表 / trace 以**环境变量名**标识密钥（密钥值任何情况不落盘，7.4）。 |
| 分类算子与按类条件化（v1.7 对齐，2026-07-07） | ① 模块编号：追加 M13 / 按流水线位置全量重编号；② fallback 语义：普通类成员且必填 / 隐式 `_unclassified` 特殊类；③ generate_only 按类配比：本版做 / 单独立项；④ 白名单是否放开 per-class `quality.llm` / `annotate.llm`；⑤ 纯打标模式：显式开关 / 零覆盖自然退化；⑥ 多标签中间档（仅打标不扇出）：本版加 / 留扩展位；⑦ dry-run multi 估算口径：乘数 1 下界 / `max_labels` 上界；⑧ `enabled=false` 而类配置在场：CONFIG_ERROR / warning；⑨ 手册新章编号：追加制 / 链序插入全书重排 | ① **追加 M13**（3.13，纯新增零重排成本，v1.3 重编号先例限于模块拆分）；② `classify.fallback_class` 为**普通类成员、enabled 时必填**（可配 per-class 参数，5.2）；③ **不做** generate_only 按类配比——generate_only 用全局指令、产物回流被分类后按类打分/标注（3.6.2），按类量目标与 8.3 O6 一并立项；④ v1 **不放开**（LLM 绑定属部署与成本面，白名单后续只增，5.2）；⑤ **不加开关**——不配任何 `[class.*]` 覆盖即自然退化为纯打标；⑥ **暂不加**，`assignment` 枚举留扩展位（8.4 演进候选）；⑦ 按标签**乘数 1 报下界** + stderr 注明（诚实不虚高，3.10.3 估算行）；⑧ **warning**（一次、点名被忽略的表——偏离提案的 CONFIG_ERROR，对齐 top_ratio 未生效等 no-op 键分级惯例，3.1.4）；⑨ **追加制** `docs/manual/24-classify.md`。另记评审改判三则：内部 Schema **不写 uniqueItems**（OpenAI strict 模式与部分约束解码网关硬拒该关键字，重复标签由 classify 代码在 M8 验证后确定性归一化，3.13.3）；fallback 留痕**不写 `item.errors`**（rejects 归因取 `errors[0]`，写入会在记录后续失败时污染归因——改放 `Classification.detail` + error 事件 + 计数器，3.13.4）；防呆分级由提案的 CONFIG_ERROR 改 **warning**（即 ⑧）。 |

**时序流语义分割与动作摘取（v1.8 对齐，2026-07-13）**：提案（`docs/dev/PROPOSAL-stream-segmentation.md`）§7 十四项开放决策点全部按默认裁决通过（①追加 M14/M15；②`[stream]` 独立节；③噪声帧进 rejects；④交错 episode 不做；⑤generate × stream 互斥；⑥序列 dedup ①②④级 + 跳③；⑦extract 文本模态不做；⑧超长会话硬切 + WARN；⑨`default:trajectory` 内置；⑩steps 恒在；⑪粒度旋钮不做；⑫修复范围 = 标签重标 + 成员收缩 + 噪声池回收、跨段只标记；⑬流式单调性校验 + `on_disorder`；⑭stream ⇒ annotate 必开）。在此之上，七域 fan-out 可行性审查（78 条发现、0 blocker）与两路深检索（refute：0 条论点被推翻；elevate：29 项外部事实钉死）的发现收敛为**三十二项设计裁决 S1–S32**，凡与提案原文不一致处以裁决为准，**详表见 `docs/dev/SPEC-stream-segmentation.md` §2**。逐条择要：

- S1 trace 通道枚举 8→10：增 `"segment"`、`"extract"` 两值（通道 = stage 名），事件名维持 `segment.*` / `extract.*`，error 事件按 stage 自动归属（7.2）。
- S2 `ClassView` 增第 6 必填字段 `extract`，`[class.<name>.extract]` 白名单承诺兑现（5.2）。
- S3 契约 ②b 补 M7 修复路径授权：可在 `absorbed` ↔ `dropped_noise` 间双向改写成员信封状态（回收/收缩），禁止翻回 `active`（4.3）。
- S4 `PipelineItem` 增字段 `session_id`：M10 装箱时对帧信封盖章，会话边界获得批内载体（4.1）。
- S5 `build_annotate_prompt` / `annotate_record` 增末位 kwarg `transitions`（additive，None = 现行为，3.5.2）。
- S6 序列标注模板不变量：末 part 恒为恒在的 `[成员帧摘要]` text——防 repair 拼接吞末帧图（3.5.2）。
- S7 stream 评审内部 Schema 三键全 required（critiques / defects / verdict），可选键改可空联合（OpenAI strict 兼容）；`VerificationResult` 增 additive 字段 `defects`（3.7.2、4.1）。
- S8 成员手术两阶段批级结构：并发评审 → 同步按批位置序执行手术 → 并发接缝重摘取/重标注——并发调度不引入额外不确定性（3.7.3）。
- S9 extract × multi 扇出按 label 各摘（接受 ×k；白名单承诺兑现，dry-run 报下界 + stderr 注明，3.15）。
- S10 dedup 序列分支：成员单条配方按序拼接（分隔符 ASCII RS）、③pHash 自动跳过、语义层增序列 case（3.3.3）。
- S11 `min_len` 仅作用于 LLM 精化切出的段；短段帧 reason = "below_min_len"（≠ "noise"），计数独立（3.14）。
- S12 帧摘要 = best-effort 确定性提取（app/activity/title/salient 均自 UI 树可达面），配摘要贫瘠护栏（`digest_poor_frames` + WARN，3.14）。
- S13 树 diff 用结构键多重集匹配 `(role, bounds//quantize, depth)`——node_id 非跨帧身份，不得作匹配键（4.2）。
- S14 extract 可靠性预算写入 §1.5 与风险面（每步 zero-shot 错误率 20–30% 的级联 [58][59]）；`extract.include_diff` 开关（默认开、可 A/B）；report 增按动作类型分布 `extract.by_type`（6.4）。
- S15 `action_type` 枚举 11 值 = AndroidControl 全集 ∪ UI-TARS-mobile 增量（`drag`、`app_switch`）+ other 兜底 [45][62]（3.15）。
- S16 `extract.on_error = "fallback" | "fail"`；fallback 步与 LLM 确证的 other 在 quality 副读数中分列（3.15）。
- S17 `--limit` 保持帧级截断；截断视同 EOF 冲洗尾会话 + WARN 一次（2.4）。
- S18 stream 模式 `counts.unprocessed` 出现条件扩为「熔断 ∨ 中断」；守恒式两侧同步扩展（6.4）。
- S19 单调性游标按分区键各自维护（groupby 语义、键变即断、输入须按键成组）；UI 模态增分区键来源 `"source_dir"`（3.2.8）。
- S20 时间戳解析规格：数值 <1e11 判秒、[1e11, 1e14) 判毫秒、界外解析失败；字符串先试数值再试 `fromisoformat`；失败与乱序同走 `stream.on_disorder`（6.1）。
- S21 整会话装箱用 next-fit（顺序装箱、仅一只开口箱）；单会话超 batch_size 硬切 + WARN + `session_split` 标（3.10.3）。
- S22 dry-run 估算公式修正：`segment_calls = Σ ceil((L−1)/(window−1))`；`extract_calls = Σ(L−1)` 报上界；quality/annotate/verify 以 episodes ≈ sessions 报下界（3.10.3）。
- S23 文本模态 dry-run 单遍融合：一次读同时产出行数与会话空跑结果（3.2.8）。
- S24 序列 Record 的 ref 继承首成员 line_no（文本）/ pair_index（UI）；完整成员溯源由 `_meta.stream.member_sources` 承担（4.1）。
- S25 rejects full 档序列载荷 = `{"kind":"sequence","member_ids":[...],"member_sources":[...]}`（3.11.2）。
- S26 `segment.on_error = "keep"` 留痕三件套（`_meta.stream.degraded` + error 事件 + 计数器），不写 `item.errors`（防归因污染，3.14）。
- S27 trace 脱敏：新 `_DATA_KEYS = {"target","value"}` none/refs 档剥除；`"description"` 入自由文本键集（7.4）。
- S28 `2 ≤ annotate.sequence_frames ≤ 100`；>20 且引用 profile `max_image_px > 2000` ⇒ WARN（Anthropic many-image 硬拒 [56]）；降采样纯整数公式、首末帧恒含（3.5.2）。
- S29 stream 模式下 `quality.rubric == ""` 解析为 `"default:trajectory"`（两模态一致；显式选择器恒优先；rubric 文本模态中立，附录 A.3）。
- S30 profile 引用集四处：`segment.llm` 仅 `strategy ∈ {llm, hybrid}` 时计入；`extract.llm` 恒入且恒入 vision_users；stream 模式下 quality 的 supports_vision 强制校验放宽（序列打分纯文本，3.1.4）。
- S31 verify 收缩弃帧 rejects 行 stage = "verify"、reason = "off_task_member"；计数器 `membership_repairs` / `boundary_flags` / `defects.<kind>` 入 report.stream.verify；transitions 手术后重编号 + `reseamed` 溯源标（3.7.3、6.4）。
- S32 v1 保留 hybrid 滑窗（window ≥ 会话长时天然退化为整段单调用，GUIDE 证据 [57] 建议长会话调大 window）；判据模板明文「相关但无实体延续的新流程 = context_switch（边界）」与「会话首帧恒为段首」；GEBD 措辞降级为「中等共识可达」[48]；「extract 先行 + 动作序列上分段」次序列演进候选（8.4），以成本权衡论证。

**活动结构——线索缝合与层级工作单元（v1.9 对齐，2026-07-15/16）**：需求原型为「x 小时自然使用手机的时序流标注出 n 帧组成的 m 个工作单元」，四类规范验收场景（串联/单交叉/多交叉/噪声）由需求方 2026-07-16 给定；真机 E2E（2026-07-15）证实三处结构性失效——交叉目标被切碎互不关联、短段吞业务末帧、extract/verify 编造连续性且可自洽过审。设计草案经三轮独立验证修订（功能完整性审计 2 blocker + 9 major + 10 minor、两轮 deep-search refute/elevate 五机制无一被驳倒、定稿五路复核 2 blocker + 6 major + 5 minor——全部裁决并入），收敛为**二十二项设计裁决 T1–T22**（编号与 v1.8 的 S1–S32 区隔），凡与草案不一致处以裁决为准，**详表见 `docs/dev/SPEC-activity-structure.md` §2**。逐条择要：

- T1/T2/T3 交叉的表达模型：**线索身份缝合**——episode 平面互斥分区不动，M16 合并同线索碎片为线索信封、`_meta.stream.fragments` 保留碎片结构（Goal-Step `is_continued` 同型 [69]）；**帧多重归属否决**（单前台屏无真并发 [65]，帧单一 absorbed 是手术/归因/守恒公共地基，[72] 引用记录被拒方案）；层级取**三级 thread ⊃ fragment ⊃ step**、不做帧级区间树（3.16）。
- T4 episode 内子任务跨度：**不做引擎特性**（需求方 2026-07-16 裁决）——标注层模式（用户 Schema `subtasks: [{label, step_range}]`）+ 手册指引；下游无消费方。
- T5 算子形态与链序：新算子 M16，`_CHAIN_ORDER` 九名单一超集元组 `segment → stitch → dedup → classify → extract → quality → generate → annotate → verify`（缝合改成员集 ⇒ 先于 dedup/extract）；默认 off，off 时主输出/rejects/report.json 与 v1.8 逐字节等价（回归锚；例外两处见 3.16.4 退化锚——dry-run stderr 行与缺陷词表 wrong_stitch 行）。
- T6/T7 契约与守恒：Stage 契约增 **②c** 例外（被并碎片壳置 `stitched`、幸存信封 Record 重绑 id 不重算、below_min_len 来源帧 dropped_noise→absorbed 翻转，含幸存者规范句，4.3）；Status 增 `stitched`（壳仅计被并 episode 信封；救援无壳），守恒全式 / failed 兜底 / unprocessed 残差**三处同步**扩 stitched 项，`counts.threads` 以恒等式 **threads = episodes − stitched** 由 M10 post-emit tally 导出（6.4、3.10.3）。
- T8/T9 判定形态与保守偏置：**单调选池**（每候选一调：池内线索摘要卡按最近活跃降序 [77] + 候选卡 → `thread_ref | new`；证据面全部为帧摘要级——链序上 extract 后置，运行时无 Transition，[65] 判定单元降格为首末帧摘要对承载）；池容量 `max_open = 4`（挂起窗口均值 3 + 1 活跃 [81]，移动域佐证 [90]）；封闭仅发生于池满逐出（stale-gap 优先 → LRU 兜底；完成感知腿撤除——无动作生产者，8.1 ⑥）、**封闭 ≠ 终结**（保留二遍目标集与产出 [64][81][66]）；`bias="conservative"` 默认——并入需 LLM 判 resume ∧ 机械先验析取三腿（App 交集 / 实体重叠 / 返回同一页面 [80]）命中、超 `stale_gap_steps` 降格须两腿；**去除 confidence 门槛腿**（口头置信度饱和 [79]，字段仅 trace 观测）。
- T10/T20 接缝：**零 LLM 机械占位**四键钉死（`action_type="app_switch"`、`detail={kind:"thread_seam", interrupted_by:[…]}`、步行内 `resumed=true`，3.15.4）；接缝判据 = 拼接对会话序间隙**含 ≥1 异线索帧**（间隙仅噪声/本线索救援帧不是接缝、照常摘取——与 v1.8「剔噪对照常摘取」单一处理）；`seam_indexes` 左成员下标坐标、与 `Transition.index` 同键空间；seam 占位不计入 `extract.transitions` / `by_type`（接缝唯一计量点 = `stream.stitch.seams`，6.4）。
- T11 短段救援：`below_min_len` 帧（duck 标判别）按**连续 run 重组**为救援候选先进池（`segment.py` 零改动）；命中并入 + ②c③ 翻转计 `rescued_short`（单位 = 帧）、未命中维持 dropped_noise，**永不开新线索**；噪声帧（reason="noise"）不入候选池（3.16.4）。
- T12/T13 作用域与判重：不跨 session、不跨 batch，hard-split 边界不可缝；segment 降格 episode 照常入池；dedup 判重单元 = 线索（重绑成员配方按序拼接，S10 机制原样），stitched 壳被 `status=="active"` 过滤天然排除——dedup 代码零改动（3.3.3）。
- T14/T15 下游适配：quality/verify 步行渲染对 `detail.kind=="thread_seam"` 加专用后缀（防 trajectory rubric 把接缝当噪声残留扣分，3.4.3）；annotate 关键帧降采样升级为**按碎片配额**（每碎片保底 1 帧，3.5.2）；verify 序列 prompt 六段 → **七段**（新增 `[片段结构]` 节——无此节 wrong_stitch 不可判）、缺陷词表 5→6（+`wrong_stitch`，路由 mark-only + fail 独立分支）、`_session_episodes` 过滤 stitched 壳（3.7.2/3.7.3）。
- T16/T17 观测与配置：trace 通道 10→11（`stitch`）+ 两事件 `stitch.judge` / `stitch.thread`、`task_name` 入脱敏自由文本集（7.2/7.4）；`report.stream.stitch` 子块与 batch.end 增 stitched/threads、dry-run 增 `stitch_calls` 估算行（off 恒 0 且无条件打印，3.10.3）；`_meta.stream` 增 thread_id / fragments / steps 行内 resumed（**条件在场**：全部 v1.9 新键仅启用时出现——off 逐字节等价的充分条件，6.3/6.4）；新节 `[stitch]` 11 键，M1 约束 stitch⇒segment、votes 偶数 = 配置错误、stitch∧rules WARN（3.1.4、5.2）。
- T18 缝合稳定性 votes：**机制立项、默认 `votes = 1` 不启用**（需求方 2026-07-16「不允许 defer」指令裁决）；>1 时 n 次采样对 **(verdict, thread_ref) 完整判定严格多数决**（分裂一律回落保守结局）；路线选型采「单模型多次」（self-consistency [33]）而非「多模型评审团」（PoLL [32]）——votes 治方差（漂移）不治偏差（过连接）[89]，过连接是跨家族共享偏差 [64]、评审团会把它投成多数 [86][89]；`stitch.judges` 多模型扩展列 8.3 O8。
- T19 有界二遍复评：复评候选 = 一遍结束时的单碎片线索，池 = 会话内全部其他线索**活视图**（超 max_open 按与候选跨度最近截取）；命中方向相反——候选作壳、目标线索幸存；预算 ≤ 单碎片线索数（[74] n=1 重聚类追平 batch；merge-only 最差 [74]；后见上下文有效 [87]；更重机器不采 [78]）。
- T21/T22 输出与身份链：M11 增第四路由（stitched 仅计数，壳不得落 rejects 兜底；`--strict` 补注：开 stitch 后 strict 结果可能 1→0 属预期，3.11.2/2.4）；`steps[].index` 全线索连续 0..n−2，`episode_id` = 幸存信封 record.id = `thread_id`、碎片原 episode_id 落 `fragments[].source_episode`（3.16.4）。

**Console 实时面板（v1.10 对齐，2026-07-17）**：需求为「deep-search 各种工业项目，为 LabelKit 设计一个 TUI 方案」；跨四品类约 20 个工业项目的调研（buck2 superconsole / Docker BuildKit / bazel / cargo·uv·pip / Nextflow·Snakemake / k9s·htop / tqdm / Bespoke Curator·distilabel / Claude Code·Codex CLI，引用 [C-1]–[C-20] 见 `docs/dev/PROPOSAL-tui-console.md`；审计增量 [C-21]–[C-42] 见 SPEC §6）经一轮定稿（需求方 2026-07-17：spec-only；U4 批 rich；U18 批 T16 有界修订；U14 心跳默认关；U15 键盘交互一期实施）与**三路独立审计二轮定稿**（代码可行性/亲和性 2B+5M+9m、文档清单 2B+6M+8m、deep-search refute/elevate 1 推翻 + 4 修订 + 6 成立——需求方 2026-07-17 第二指令随即实施、不允许 defer），收敛为**二十七项设计裁决 U1–U27，详表见 `docs/dev/SPEC-tui-console.md` §2**。核心裁决择要：品类 = 双区内联面板、永不进 alternate screen（U1，批任务保 scrollback）；面板 = M12 第四纯消费面，`ProgressListener` **五回调**进程内旁路（U19——on_run_context/on_estimate 补齐渲染器数据通路）不产生 TraceEvent、不入 7.2 目录（U2/U11——7.2 只增不改原则零触碰），on_event 载荷 none 档预脱敏（U22——U6 信息纪律由机制保证）、sink 侧转发异常自吞（U23）；段棋盘分子 = llm.call 括号归属运行级累计、分母 = `estimate_run` 运行级估算（U20——llm.call 事件无阶段归属的口径修复）；emitter 让位经 M1 解析产物 `mode_resolved` 静态门 + plain 行格式纯函数 `console_format` 下沉 common（U21）；回归锚三层化（U24——实跑逐字节 diff 被 refute 审计证伪：行携时间戳、温度 0 端点非确定）；NO_COLOR 不降 plain（U25——no-color.org 语义 = 禁色非禁布局，rich 原生承担）；渲染 tick = asyncio task、`Live(auto_refresh=false)` 钉死（U26——rich 默认自刷新线程与事件循环内动态字典跨线程争用）；validate 通路 `validate_project(..., overrides=)` 修复（U27）；渲染异常自吞降级 plain、永不影响退出码与产出（U7 红线）；三态 auto|rich|plain，jsonl 强制 plain 不可覆盖（U5）；批总数分母 UI 模态恒显示（live 预扫复用、禁二次 scan）、文本模态默认不做估算扫描（U17，`console.estimate` 显式换购）。

**上下文预算与视觉能力自动推导（v1.11 对齐，2026-07-22）**：对每次 LLM 调用建立不变式 `est(输入 prompt) + max_output_tokens + margin ≤ context_window`——`[llm.<name>]` / `[embedding.<name>]` 增 `context_window` 声明（0 = 未声明 = 该 profile 预算关闭，行为与 v1.10 逐字节一致）；零依赖启发式估算器 + **动态装填**（条数型参数降级为上限值、按实际内容逐项装填，调用次数以静态最坏值 w_min 保证上界——estimate/console 分母共用，V9/V12）；**图片成本测量-反应式三层**（默认采样装填 `default_image_px` → 溢出裁帧保清重试 → 判审低置信裁帧升清重试，`usage.prompt_tokens` 在线校准、批冻结快照——provider 文档公式降级为首批先验，V17–V21）；M9 咽喉终检与记录级 `context_overflow` / `output_truncated`（三形态熔断矩阵：precheck 不计连击、reactive-400 终局由属主算子补喂恰一次、reactive-200 不补喂，V16/V24/A7，7.6）；同时**删除 `segment.use_vision`**，改为能力推导 parse product `vision_resolved`（选 profile 即选能力；存量显式键定向 CONFIG_ERROR，V1–V5）。裁决 A1–A5 按推荐执行、A6 被 V17–V21 取代（测量-反应式为需求方自提）、A7 反应态终局计入连击；决策 **V1–V27 详表见 `docs/dev/SPEC-context-budget.md` §2**（业界调研与审计引用 [C-1]–[C-84] 见 `docs/dev/PROPOSAL-context-budget.md`）。

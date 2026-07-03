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

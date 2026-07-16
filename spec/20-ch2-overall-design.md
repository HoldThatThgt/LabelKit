# 2. 总体设计

## 2.1 系统定位与总体规格

LabelKit 是一个**单机、单进程、无状态**的 Python CLI 批处理工具。一次运行按 project.toml 声明的阶段组合执行流水线，向一个输出路径写出 JSONL：`run.mode = "process"`（默认）读取一个输入路径、加工既有数据；`"generate_only"`（v1.4）无输入，由 M6 从零生成后走同一条治理管线（3.10.3）。工具对外只有三个交互面：**CLI 参数**、**两个 TOML 配置文件**、**输入/输出文件**；对内只有一类外部依赖：**配置中声明的 LLM API**。

### 2.1.1 功能规格总表

| 能力 | 可选性 | 规格 |
|---|---|---|
| 数据接入 | 必选 | 文本模态：读取 `.jsonl` 文件（或含多个 .jsonl 的目录），每行一条记录。UI 模态：递归扫描目录，按文件名中的 index 配对 `uitree_<index>.jsonl` 与 `image_<index>.jpg/.jpeg/.png`，允许跨子目录配对。坏行/缺对按策略跳过并计入报告。 |
| 去重 | 可选，默认开 | 两级：① 规范化内容 SHA-256 精确去重；② MinHash-LSH 近似去重（默认字符 5-gram、128 permutation、Jaccard 阈值 0.85）。UI 模态额外做截图 pHash（默认汉明距离 ≤ 8），树/图判定关系可配。作用域可选全局或批内。v1.2 增可选第④级语义去重（SemDeDup [26]，需 embedding profile，默认关；余弦相似度阈值 0.95，3.3.3）。 |
| 数据分类 | 可选，默认关 | v1.7：按用户类别表（`[[classify.classes]]`）对存活记录做 LLM 封闭集分类——词表经内部 Schema enum 硬校验（M8 四层防线），单/多标签可配（`classify.assignment`），可选 self-consistency 投票，结构失败归兜底类（3.13）。类标签进 `_meta.classification` 并驱动下游按类条件化：quality 按类分池与 rubric、annotate/generate/verify 按类指令与维度（`[class.<name>.*]` 白名单覆盖，5.2）；multi 模式按标签扇出为多条按类管线（一条数据多份结果，3.13.4）。不配任何 `[class.*]` 覆盖即纯打标模式。 |
| 时序流分段与动作摘取 | 可选，默认关 | v1.8（stream 模式，`segment.enabled = true`）：数据按时间排序输入时，摄取层按 `[stream]` 声明排序键、做按分区键单调性校验并按 gap/key/上限规则切候选会话（3.2.8），切批改整会话装箱（3.10.3）；M14 segment 在会话内做滑窗 LLM 边界精化与逐帧噪声剔除，把成员帧收拢为序列记录 episode（3.14）；M15 extract 对相邻帧对推断结构化动作 `{action_type, target, value, description}` 写入 `item.transitions`（仅 UI 模态，3.15）；下游 dedup/classify/quality/annotate/verify 按序列形态适配，内置轨迹 rubric `default:trajectory`（附录 A.3）。噪声帧进 rejects（reason = noise \| below_min_len）；关闭时数据产出与 v1.7 逐字段一致（`_meta.stream: null` 除外）。 |
| 线索缝合 | 可选，默认关 | v1.9（`stitch.enabled = true`，要求 stream 模式）：M16 stitch 在会话内把同一目标导向任务被穿插切开的碎片（episode）保守缝合为**线索 thread**——单调选池 LLM 判定 × 机械先验合取（App 交集 / 实体重叠 / 返回同一页面析取三腿）+ 有界二遍复评修正贪心漏缝（3.16）；`below_min_len` 短段按连续 run 重组先进候选池救援；被并 episode 壳置 `stitched`（仅计数不落盘，3.11.2）、幸存信封 Record 重绑；多碎片线索机械标定接缝（`seam_indexes`），M15 对接缝序数零 LLM 生成占位步（`detail.kind = "thread_seam"`，3.15.4）。产出三级结构 thread ⊃ fragment ⊃ step（`_meta.stream.fragments` 溯源，6.3）；关闭时主输出 / rejects / report.json 与 v1.8 逐字节等价（3.16.4 退化锚）。 |
| 质量打分 | 可选，默认开 | QuRating 双模式：pairwise（批内 k 轮随机配对 → LLM 判胜负 → BT 拟合 → 百分位归一化到 [0,1]）与 pointwise（0–5 加性 rubric 打分归一化）。Rubric 用户提供或用系统默认（文本/UI 各一套）。可按聚合分阈值过滤。v1.7：classify 启用时批内按类分池打分，rubric/门槛/选择机制均可按类覆盖（3.4.3 按类分池行）。 |
| 自动标注 | 可选，默认开 | 按 project.toml 的任务指令 + few-shot 示例组装提示词；UI 模态附截图（base64）与序列化 UI 树；输出受用户 JSON Schema 约束，经结构引擎（M8）保证合法。v1.2 增可选 self-consistency：同一记录以 `annotate.sc_temperature` 独立采样 n 次（`annotate.self_consistency`，n≥3 奇数）后字段级多数投票，成本 ×n（3.5.2）。 |
| 数据生成 | 可选，默认关 | 仅文本模态。以通过质量门的记录为种子示例，按生成指令产出新样本（每种子 m 条），新样本与种子及彼此做 MinHash 相似度过滤后，回流经打分、标注、校验（Self-Instruct 流程 [18]）。v1.2 增多 LLM 混合（`generate.llms` 数组 + round_robin 轮转 / weighted 加权）与风格模板条件化（`[[generate.styles]]`），提升产出多样性并按 llm×style 桶统计（3.6.2）。v1.4 增纯生成模式：`run.mode="generate_only"` 时无输入数据，种子来自配置种子池 `generate.seed_examples` 或无种子条件化（`instruction × styles` + `generate.standalone_count`），产出照常走去重 →（打分）→（标注）→ 校验（quality 与 annotate 至少一个启用，约束①，2.3.1）。 |
| 二次校验 | 可选，默认关 | LLM-as-a-Judge 用独立 profile 评审 (记录, 标注) 是否合格，产出 verdict + 批评意见；失败策略可选丢弃或有界修复（批评意见回喂标注模型，最多 N 轮）。 |
| 结构保证 | 必选 | 输出每行必然通过用户 JSON Schema 校验，四层防线：供应商原生结构化输出 → 确定性修复 → jsonschema 校验 → 有界 LLM 修复环；仍失败则该记录进 rejects 通道，绝不写入主输出。 |
| 输出 | 必选 | 主输出 JSONL（用户结构 + 可配置 `_meta` 元信息）；`report.json` 运行报告（仅统计，无数据内容）；可选 rejects 通道。 |
| 日志与追踪 | 运行日志恒有；trace 可选，默认关 | 双通道（第 7 章）：stderr 运行日志（`tool.log_format = "text"`｜`"jsonl"`，仅运维事件，绝不含数据内容与提示词）；trace 追踪日志（`trace.enabled=true` 时写 `{output_stem}.trace.jsonl`，一行一事件，记录去重判定、逐次质量裁决与理由、评审结论等，供 rubric 优化与标注质量分析，内容量按 `trace.content` 四档脱敏）。日志写失败绝不中断运行。 |

### 2.1.2 不做什么（工具级负边界）

**工具级边界：**① 不训练/托管任何本地模型（QuRater 分类器训练不在范围内，打分全部运行时经 API 完成）；② 不做数据存储、缓存、断点续跑与跨运行状态（无数据库、无 checkpoint 文件）；③ 不做数据采集与数据版本管理；④ 不提供服务化/长驻进程形态；⑤ 不做人工标注界面（人工复核请将输出导入 Argilla 等外部平台 [5]）；⑥ 不做训练数据配比/混合（属下游职责）——含跨类输出配比（v1.7）：按类参数是**加工条件化**而非配额/重采样，「每类输出恰好 N 条」不做（属 8.3 O6 全局定量问题域）；⑦ 除配置声明的 LLM API 外不发起任何网络请求，无遥测。

## 2.2 总体架构与模块清单

系统分四层：**入口层**（CLI）、**编排层**（M10）、**算子层**（M2–M7、M11、M13（v1.7）、M14/M15（v1.8）、M16（v1.9），签名统一的流水线阶段）、**服务层**（M1 配置、M8 结构引擎、M9 LLM 客户端、M12 日志，被各算子共享调用）。算子层内部互不依赖，只依赖服务层与公共数据结构（第 4 章），这保证了阶段可独立开关、独立测试。

图 2-1 LabelKit 总体架构（四层）。实线为算子层主链数据流；紫色虚线为 M6 生成的旁路（取种子 / 子批回流，v1.3 拆分为独立模块）。v1.7 算子层增 M13 classify（3.13，主链工位在 M3 与 M4 之间）。v1.8 算子层增 M14 segment 与 M15 extract（3.14、3.15，默认关）：M14 工位在主链最前（M3 之前，消费 M2 会话流视图产出 episode），M15 工位在 M13 与 M4 之间（对序列信封写入转移）。v1.9 算子层增 M16 stitch（3.16，默认关）：工位在 M14 与 M3 之间（把会话内碎片缝合为线索——缝合改成员集，先于判重与摘取）。

### 2.2.1 模块清单与职责边界一览

| 模块 | 职责（做什么） | 边界（不做什么） | 依赖 |
|---|---|---|---|
| M1 config | 装载、校验、合并两个 TOML 与 CLI 覆盖项，产出不可变 `ResolvedConfig`；解析 API Key 环境变量。 | 不读输入数据；不做业务默认值以外的推断；运行期不再被写。 | — |
| M2 ingest | 解析输入路径 → 记录流；UI 文件对扫描与配对；构造 `Record`（含确定性 id）；输入级合法性校验。 | 不做去重/打分；不加载图像字节（懒加载引用）；不修改原始内容。 | M1 |
| M3 dedup | 精确哈希 + MinHash-LSH + pHash 判重，v1.2 增可选第④级语义判重（经 M9 embed() 取句向量，默认关，3.3.3）；标记重复项并给出簇信息。 | 不调用对话 LLM；语义判重仅 dedup.semantic=true 时启用（默认零 embedding 依赖）；不物理删除（只标记状态）。 | M1（语义级开启时另需 M9） |
| M4 quality | QuRating 双模式打分：配对采样、LLM 裁决、BT 拟合、归一化；按阈值标记低质。 | 不定义 rubric 内容（来自配置/默认包）；不做标注。 | M1, M8, M9 |
| M5 annotate | 组装标注提示词（含多模态与 few-shot）；调用 LLM 获得符合用户 Schema 的标注；可选 self-consistency 字段级投票。 | 不校验结构（委托 M8）；不评审质量（M4/M7 职责）；不产出新记录（M6 职责）。 | M1, M8, M9 |
| M6 generate | 以种子（process 模式：过质量门记录；generate_only 模式：配置种子池或无种子条件化，3.6.2）按 llms × styles 组合产出新样本 Record（含 generator 溯源），交 M10 回流。 | 仅文本模态；单轮回流不递归；不去重/打分/标注（回流后 M3/M4/M5 职责）。 | M1, M8, M9 |
| M7 verify | LLM-as-a-Judge 评审标注；失败按策略丢弃或驱动有界修复。 | 不直接改标注（修复仍由 M5 重新标注、M8 校验）。 | M1, M8, M9 |
| M8 schema-engine | 用户 Schema 装载与预校验；LLM 原始输出 → 合法 JSON 对象的四层保证；裁决输出等内部小结构的校验。 | 不组装业务提示词；不发起首次 LLM 调用（只驱动修复调用）。 | M1, M9 |
| M9 llm-client | Profile 化的统一 LLM 访问：OpenAI 兼容/Anthropic 两类 provider、多模态消息、结构化输出参数、指数退避重试、并发信号量、token/成本计量。 | 不理解业务语义；不解析业务结构（返回原始文本/原生结构化结果）。 | M1 |
| M10 orchestrator | 批切分、阶段组合（按开关）、生成回流调度、运行级统计聚合、生命周期与中间态丢弃。 | 不含任何阶段业务逻辑；不直接调用 LLM。 | 全部 |
| M11 emitter | 主输出/rejects/报告三通道写出；`_meta` 组装；增量落盘。 | 不校验结构（到达此处的标注已合法）；报告不含数据内容。 | M1 |
| M12 logging | 进程内唯一日志设施：stderr 运行日志（标准 logging，text\|jsonl 两种格式）；trace 事件流 `EventLog`（JSONL，行缓冲，每批随 M11 flush 同步 flush），由 MetricsSink 持有、各 Stage 经 `RunContext.metrics` 发事件。 | 不做跨运行聚合分析（后续 analyze 工具职责，8.3 O5）；不上传遥测；写失败不中断运行（warn 一次并关闭通道，计入 report）；API Key 永不落日志。 | M1 |
| M13 classify（v1.7） | 按用户类别表对批内存活记录做 LLM 封闭集分类（单/多标签可配，可选 self-consistency 投票）；结果写 `item.classification`；multi 模式按标签向批尾扇出兄弟信封（3.13）。 | 不淘汰记录（分类不是质量门；multi 扇出只增不减）；不定义类别语义（来自配置）；不做标注（用户 Schema 产出物属 M5）；不改链结构（扇出只改批内信封基数）。 | M1, M8, M9 |
| M14 segment（v1.8） | 把批内候选会话精化为 episode：可选 LLM 滑窗边界裁决与逐帧噪声标记；成员信封置 `absorbed`、噪声帧置 `dropped_noise`，按序键拼装序列 Record 并尾部追加 episode 信封（契约 ②b，4.3；3.14）。 | 不判重（M3）；不推断动作（M15）；不打任务标签（M5）；不改链结构。 | M1, M8, M9 |
| M15 extract（v1.8） | 对每个 active 序列信封的每对相邻成员帧 ⟨s_i, s_{i+1}⟩ 经 LLM 产出结构化动作（内部 Schema），写入 `item.transitions`；转移数 = 成员数 − 1（3.15）。 | 不重分段（M14 上游）；不产出用户 Schema 字段（M5）；不淘汰记录。 | M1, M8, M9 |
| M16 stitch（v1.9） | 把会话内碎片保守缝合为线索：单调选池 LLM 判定 × 机械先验合取 + 有界二遍复评；被并 episode 壳置 `stitched`、幸存信封 Record 重绑、below_min_len 短段救援翻转（契约 ②c，4.3）；机械标定 `seam_indexes`（3.16）。 | 不重分段（M14）；不摘取动作（M15）；不判重（M3）；不跨会话/跨批；不做帧多重归属。 | M1, M8, M9 |

## 2.3 端到端数据流与阶段开关

图 2-2 端到端数据流（process 模式）。实线为主路径；紫色虚线为可选的生成回流；红线为淘汰通道。generate_only 模式（v1.4）无输入与 M2：M6 为链路起点，生成样本按 batch_size 切批后自 M3 起走同一主路径（3.10.3）。v1.7：classify 启用时主路径在 M3 与 M4 之间插入 M13 分类工位（3.13）——multi 扇出在该工位内向批尾追加兄弟信封，回流子批因继承分类而幂等跳过。v1.8：segment 启用（stream 模式）时 M2 产出的是会话流、M10 改整会话装箱，主路径在批首插入 M14 分段工位（成员帧收拢为 episode 信封，②b）、在 M13 与 M4 之间插入 M15 摘取工位（对序列信封写入转移）——链序为 segment → stitch → dedup → classify → extract → quality → annotate → verify（stitch 为 v1.9 增位，默认关；3.10.3；generate 与 stream 互斥故回流旁路不出现）。v1.9：stitch 启用时主路径在 M14 与 M3 之间插入 M16 缝合工位（碎片缝合为线索——被并壳置 stitched、幸存信封重绑、短段救援翻转，②c，3.16）。

### 2.3.1 阶段开关矩阵

阶段组合由 project.toml 各节的 `enabled` 决定。合法组合与典型用法：

| dedup | quality | generate | annotate | verify | 典型用法 |
|---|---|---|---|---|---|
| ✓ | ✓ | — | ✓ | — | 默认：清洗 + 打分 + 标注 |
| ✓ | ✓ | — | — | — | 纯数据治理：只去重打分，输出过滤后的原始数据 + 分数 |
| ✓ | ✓ | ✓ | ✓ | ✓ | 全流程：治理 + 扩充生成 + 标注 + 评审（成本最高，质量最高） |
| — | — | — | ✓ | ✓ | 纯标注：数据已治理过，只做标注与评审 |
| ✓ | 可选 | ✓ | 可选 | — | 纯生成（`run.mode="generate_only"`，v1.4）：无输入从零合成 → 治理 →（标注）→ 输出 |

约束：① `annotate` 与 `quality` 至少启用一个，否则运行无产出意义，M1 在启动时报 `CONFIG_ERROR`；② `verify.enabled=true` 要求 `annotate.enabled=true`；③ `generate.enabled=true` 要求 `run.modality="text"`；process 模式下另要求 `quality.enabled=true`（种子来自质量门），generate_only 模式下 quality 可选（种子来自配置，3.6.2）；④ `run.mode="generate_only"`（v1.4）要求 `generate.enabled=true` 且 `run.input` 缺省（提供即报 CONFIG_ERROR，3.1.4），annotate 可选。（v1.7）`classify.enabled`（默认 false，5.2）与上表各开关正交：分类不改变组合合法性，任意合法组合均可叠加分类（multi 扇出后的每个信封走同一阶段组合；generate_only 链亦含 classify——生成产物被正常分类，3.10.3）；`classify.enabled = false` 而 `[[classify.classes]]` 或 `[class.*]` 在场不报错——M1 打 warning（一次、点名被忽略的表；「留配置、关开关」合法，3.1.4）。（v1.8）stream 组合约束：`segment.enabled=true` 要求 `run.mode="process"` ∧ `generate.enabled=false`（含 generate_only 的传递闭合——stream × generate 互斥）∧ `annotate.enabled=true`（序列记录无 passthrough 输出形态）；`extract.enabled=true` 要求 `segment.enabled=true` ∧ `run.modality="ui"`；stream 与 classify 正交（episode 照常分类并驱动按类条件化）；`[stream]`/`[segment]`/`[extract]` 在场而 `segment.enabled=false` 同上打 warning（3.1.4）。（v1.9）stitch 组合约束三条：① `stitch.enabled=true` 要求 `segment.enabled=true`（缝合的输入是 episode——stream 前置约束经此传递闭合，`[stitch]` 名单同入上句 segment 关闭时的 no-op warning）；② `stitch.votes` 须为 1 或 ≥3 的奇数（偶数报 CONFIG_ERROR，多数决需破平局，3.1.4）；③ `stitch.enabled=true` ∧ `segment.strategy="rules"` ⇒ warning（非阻断：规则粗切段未经语义精化，缝合证据质量下降的组合提示）；另有单独 no-op warning——`segment.enabled=true` ∧ `stitch.enabled=false` 而 `[stitch]` 有 payload（`annotate.sequence_frames` 同形制，3.1.4）。

### 2.3.2 算子对输出集的影响分析

设某批进入流水线的存活记录数为 N（全流程视角对应 6.4 的 counts 不变量 `emitted + dropped_* + failed + bad_input = scanned + generated`；熔断中止时左侧另加 `unprocessed`，v1.6，6.4）。每个算子对最终输出集的影响从四个维度刻画：**基数**（条数如何变）、**内容/构成**（行内容与数据分布如何变）、**判定依据的落点**（决策证据写入哪个 `_meta` 字段 / 哪个 trace 事件，事后可审计）、**被淘汰记录的去向**。总原则先行：**主输出只含存活记录；`dropped_dup` / `dropped_lowq` / `dropped_verify` / `failed` 一律不入主输出、按 `output.rejects` 进拒绝通道**（"none" | "refs"（默认）| "full"，写出规格见 3.11.2）；v1.8 增两态——`dropped_noise` 同走 rejects，`absorbed`（成员并入 episode）为第三路由：主输出与 rejects 均不写、仅计数（3.11.2）；v1.9 增一态——`stitched`（被并 episode 壳）为**第四路由**：同 absorbed 仅计数（其成员随幸存线索信封落盘，3.11.2）。

| 算子 | 基数影响（N→?） | 对内容/构成的影响 | 判定依据落点（_meta / trace） | 淘汰去向 |
|---|---|---|---|---|
| M2 接入 | scanned → ingested = scanned − bad_input；只减不改。 | 按 `input.text_field` 抽取文本 / 配对 UI 文件构造 Record（3.2.4–3.2.5），不改动数据内容。 | `_meta.source`{file, line_no \| pair_index}；trace `ingest.bad_line / ingest.missing_pair / ingest.index_conflict`。 | 坏行/缺对未构成 Record，不走 rejects 通道——仅计 `report.counts.bad_input`（策略为 fail 时直接退出码 3）。 |
| M14 分段（v1.8） | 吸收与追加：批内 N 帧收拢为 E 个 episode 信封，N → N − absorbed − dropped_noise + E（成员帧置 `absorbed` 并入序列信封、噪声/短段帧置 `dropped_noise`；episodes 由 M10 按 len 差计量，守恒式扩展见 6.4）。 | 输出集单位从「帧」升维为「序列记录」（`kind="sequence"`，成员经 `members` 引用共享）：主输出行承载整段任务序列而非单帧，帧数分布变为段长分布。 | `_meta.stream`{episode_id, session_id, member_ids, member_sources, ...}（未启用 = null，6.3）；trace `segment.session` / `segment.boundary`（7.2）。 | `dropped_noise` ⇒ rejects（stage="segment"，reason="noise" \| "below_min_len"，3.11.2）；`absorbed` 为第三路由（不入主输出、不入 rejects、仅计数）。 |
| M16 缝合（v1.9） | 合并与翻转：E 个 episode 缝合为 T 个线索，E → E − stitched（被并 episode 信封壳置 `stitched`、幸存信封 Record 重绑；threads = episodes − stitched，M10 导出式计量，3.10.3）；救援命中另使 dropped_noise 帧翻回 absorbed（`rescued_short`，帧口径）。 | 输出集单位从「episode」升维为「线索」：交叉任务合并为一行（碎片结构入 `_meta.stream.fragments`）、行数下降；救援使业务短段尾帧回归主输出；接缝步以占位形态进入 steps 序列（`detail.kind="thread_seam"`，3.15.4）。 | `_meta.stream`{thread_id, fragments[], steps 行内 resumed}（仅启用时在场，6.3）；trace `stitch.judge` / `stitch.thread`（7.2）。 | `stitched` 为第四路由（不入主输出、不入 rejects、仅计数，3.11.2）；救援未命中帧维持 dropped_noise 原去向；`on_error="fail"` 时 episode 候选信封 `failed` ⇒ rejects（kind=stitch_invalid，7.6）。 |
| M3 去重 | N → N − dup；只减不改（存活行内容不变）。 | 簇内仅留首见记录（first-writer-wins，3.3.3），压缩高冗余来源、改变数据分布；`dedup.semantic = true`（默认 false）时判重面扩至改写型语义近重（嵌入余弦相似度 ≥ `dedup.semantic_threshold`，默认 0.95，SemDeDup [26]），dup 增大、输出集单位条数的多样性提高。 | 存活者 `_meta.dedup.kind="unique"`；被淘汰者 `DedupInfo`{kind, cluster_key, kept_id}（4.2）；trace `dedup.duplicate`。 | `dropped_dup` ⇒ rejects（refs 档仅 _meta 引用行，3.11.2）。 |
| M13 分类（v1.7） | single：基数不变（每条 active 记录写入类标签）；multi：增——归一化后命中 k ≥ 2 类的记录向批尾扇出 k−1 个兄弟信封，N → N + fanout（3.13.4）。 | 不改记录内容；类标签驱动下游按类条件化（按类 rubric/门槛/指令/种子池，3.4.3/3.5.2/3.6.2/3.7.2），间接改变输出集组成；multi 下一条输入至多产出 `classify.max_labels` 行，消费侧行唯一键变为 (`_meta.id`, `_meta.classification.label`)（6.3）。 | `_meta.classification`{label, labels, source}（未启用 = null，6.3）；trace `classify.decision`（7.2）。 | ——（分类不淘汰记录：结构失败默认归兜底类仍存活；仅 `classify.on_error="fail"` 时记录 failed ⇒ rejects，3.13.4）。 |
| M15 摘取（v1.8） | 基数不变：对每个 active 序列信封写入 `item.transitions`（转移数 = 成员数 − 1）；仅 `extract.on_error="fail"` 时 episode 置 failed 减（默认 fallback 该步记 other 留痕、episode 存活，3.15）。 | 不改记录内容；步骤序列落 `_meta.stream.steps` 并注入下游 quality/annotate/verify 提示词作机械锚点，间接决定序列标注与打分的证据质量。 | `_meta.stream.steps`；trace `extract.step`（7.2；target/value 属输入数据派生，按 `_DATA_KEYS` 分档脱敏，7.4）。 | 默认不淘汰（fallback 留痕）；`on_error="fail"` 时 `failed` ⇒ rejects（kind=extraction_invalid，7.6）。 |
| M4 打分 | 打分本身不减：每条 active 记录写入分数（各 criterion + `__aggregate__`），基数不变；门控/选择才减——配置 `quality.threshold`（聚合分低于线 ⇒ dropped_lowq）或 `quality.selection = "top_ratio"`（保留批内前 `quality.top_ratio` 比例）时 N → N − lowq。两者互斥（M1 校验），均缺省 = 只打分不过滤（5.2）。 | 不改记录内容；对质量分布截尾，直接改变输出集组成（机制见下方三路径）。pairwise 模式下淘汰按批内相对位次进行（见本节 note）。 | `_meta.scores`（per-criterion + `__aggregate__` + mode + batch_no，6.3）；trace `quality.judgment / quality.pointwise / quality.bt_fit / quality.gate`。 | `dropped_lowq` ⇒ rejects。 |
| M5 标注 | 成功路径基数不变；失败减（L3 耗尽 ⇒ failed，3.8.2）。 | 内容增列：主输出行由原始数据变为用户 Schema 标注对象（原文仅经 `_meta.source` 溯源与 `output.passthrough_fields` 透传）；`annotate.self_consistency` ≥ 3 时标注取多数票 [33]，只增调用次数、不改基数。 | `_meta.annotation`{model, attempts}；trace `annotate.done`、`schema.repair`。 | `failed`（如 schema_violation，7.6）⇒ rejects。 |
| M6 生成 | 增：N → N + G（G ≤ 调用次数 × `generate.num_per_call`，3.6.2）；生成子批回流 M3 起再过滤（图 2-2），实际净增 ≤ G。 | 引入合成样本、改变输出集的「真实 : 合成」构成比；`generate.llms` / `generate.mixture` / `[[generate.styles]]` 决定合成子集的模型与风格分布（3.6.2）；合成占比过高有 model collapse 风险 [36]——合成样本的统一标记为 `_meta.source.generator ≠ null`（v1.4 起：generate_only 模式下 `generated_from` 恒为空，不可作合成判据），可按其用路径二同法后筛控制比例。 | `_meta.source.generator`（≠ null 即合成，v1.4，{"llm","style"}，6.3）；`generated_from` 补充种子溯源（process 模式为种子 id 列表，generate_only 恒为空）；回流后各算子事件照常落点。 | 回流中被淘汰者按所在算子的去向进 rejects。 |
| M7 校验 | 减：`verify.policy="drop"` 直接减；`"repair"` 先修复（≤ `verify.max_repair_rounds` 轮，默认 1）仍 fail 再减（3.7.3）。 | repair 路径会改写标注内容（批评意见回喂 M5 重标注 + M8 重校验）——M5 之后唯一还会改动主输出行内容的算子。 | `_meta.verification`{verdict, rounds}；trace `verify.verdict`（每轮一事件）。 | `dropped_verify` ⇒ rejects。 |
| M11 输出 | 通道分发，不新增淘汰判定（写出前 `validate_only` 终检失败属 bug 兜底，记 internal_error 转 rejects，3.11.1）。 | 按 status 分发三通道：`status="active"`（annotate 启用时且标注成功）→ 主输出；dropped_* / failed → rejects；计数/分布 → report.json。组装 `_meta`（`output.meta_mode = "inline" \| "sidecar" \| "none"`，6.3）。 | 分发依据即 status 本身；trace `batch.end / run.end` 携带各状态计数。 | ——（三通道即去向本身）。 |

**分数如何影响输出——三条独立路径。**M4 产出的分数经由三条彼此独立的路径作用于输出集，约束力递减：

**路径一：门控与选择（硬路径，直接改变输出集组成）。**`quality.threshold` 将聚合分低于线的记录置 `dropped_lowq`（3.4.3 质量门）；v1.2 新增的 `quality.selection = "top_ratio"` 改为保留批内聚合分排名靠前的 `quality.top_ratio`（取值 (0,1]，selection="top_ratio" 时必填）比例——该机制的精确定义（含排序、并列与取整规则）由 3.4.3 给出，此处仅作引用。两种方式互斥（`quality.selection = "threshold"` 为默认，M1 启动时校验互斥性）。这是分数影响输出的唯一「硬」路径：直接决定哪些行存在于主输出。

**路径二：`_meta.scores` 随行落盘（软路径，供下游后筛）。**`output.meta_mode = "inline"`（默认）时分数随主输出每行落盘（6.3），因此门控可以留宽、由下游按需收紧——一行 jq 即可从主输出筛出聚合分 ≥ 0.6 的行（剥离或保留 `_meta` 自便）：

```
# inline 模式；要保留 _meta 则删去 "| del(._meta)" 一段
jq -c 'select(._meta.scores["__aggregate__"] >= 0.6) | del(._meta)' \
   out/ime-intent-0630.jsonl > out/ime-intent-0630.hq.jsonl
```

`meta_mode = "sidecar"` 时 `_meta` 在 `{output_stem}.meta.jsonl` 且与主输出行序对齐（6.3），可用 `paste` 或 `jq --slurpfile` 按行号连接后同法筛选；`meta_mode = "none"` 丢弃分数，本路径不可用（6.3 已注明不推荐）。

**路径三：trace `quality.*` 事件（诊断路径，跨运行起效）。**`quality.judgment / quality.gate` 等事件（7.2）不改变本次输出的任何一行，但它们是 7.5 rubric 优化闭环的原料：据其修订 rubric 后重跑，改变的是「下一次运行」的输出集。分数对输出的影响由此分三种时效——当次硬淘汰（路径一）、当次可后筛（路径二）、跨次可调优（路径三）。

**关键限制——pairwise 分数是批内相对量：**pairwise 主模式下 score = log θ 的批内百分位（3.4.3「归一化与聚合」行），每批最低恒为 0、最高恒为 1。因此 `quality.threshold` 在 pairwise 下的语义是「**批内百分位线**」而非全局绝对质量线：threshold = 0.3 ≈ 每批淘汰相对最差的约 30%——即使某批整体质量极高，仍会淘汰其相对靠后的部分；两批各自的 0.53 也不可直接比较（3.4.3「比较池」行）。需要跨批绝对可比（例如路径二想用统一分数线做全局后筛）时应选 `quality.mode = "pointwise"`（绝对刻度）；而 `quality.selection = "top_ratio"` 本就是批内相对选择，与 pairwise 语义天然一致（精确定义见 3.4.3）。理解这一限制是理解「打分如何影响输出」的前提。

## 2.4 CLI 规格

```
labelkit run      --config <config.toml> --project <project.toml>
                  [--input PATH] [--output PATH]        # 覆盖 project.toml 中 run.input / run.output
                  [--limit N]                           # 只处理前 N 条（试跑）；v1.8 stream 子句：帧级截断不变（islice 在 M2 解析流与会话装配器之间），截断视同 EOF——尾部未闭合会话按会话闭合下发并 WARN 一次「尾会话被 --limit 截断」（3.2.8）
                  [--dry-run]                           # 走完 M1/M2 校验与成本估算，不调用 LLM；报告写 {stem}.dryrun.report.json、trace 写「trace 文件名在扩展名前插 .dryrun」（默认 {stem}.trace.dryrun.jsonl），不覆盖上次真实运行的产物（v1.5）；generate_only 无 M2，成本按 3.6.2 调用次数公式静态估算；v1.7：classify 启用时估算增 classify_calls（公式与 multi/按类覆盖下的下界口径见 3.10.3 分类与扇出行）；v1.8：segment 启用时估算增 segment_calls / extract_calls——segment_calls = Σ ceil((L−1)/(window−1))（L 为会话长；L=1 或 strategy="rules" 计 0）、extract_calls = Σ(L−1) 报上界，quality/annotate/verify 以 episodes ≈ sessions 报下界 + stderr 注明；批数按会话空跑实际装箱精确得出，文本模态行数统计与会话空跑单遍融合（3.10.3、3.2.8）；v1.9：估算增 stitch_calls = 会话数 × votes ×（2 若 repass 否则 1）（episodes ≈ sessions 下界基数，沿用既有 stderr 下界注；off 时恒 0 且该行无条件打印——segment_calls 先例，3.10.3）
                  [--strict]                            # 任何记录被拒绝即以退出码 1 结束；v1.9 补注：stitched 壳与被救援帧均不构成 rejects——同输入开启 stitch 后（短段被救援而不再落 rejects）strict 结果可能由 1 变 0，属预期（3.11.2、3.16.6）
                  [--log-level debug|info|warn|error]   # 默认 info
labelkit validate --config <config.toml> --project <project.toml>
                  # 仅执行 M1 全量校验（含用户 Schema 预校验、rubric 校验、profile 连通性探测可选 --probe；v1.6：密钥池逐密钥探测，3.9.2 probe_all）
labelkit rubric   [--show default:text | default:ui | default:trajectory]   # 打印系统默认 rubric 的 TOML 全文，便于用户复制修改（default:trajectory 为 v1.8 轨迹 rubric，附录 A.3）
```

| 退出码 | 含义 |
|---|---|
| 0 | 运行完成。可能存在被拒绝记录（详见 report.json 与 stderr 摘要）。 |
| 1 | 运行完成但违反 `--strict`（存在 rejects），或报告写出失败。 |
| 2 | 配置错误（TOML 语法/字段/引用的 profile 不存在/Schema 非法/rubric 非法/环境变量缺失）。 |
| 3 | 输入错误，仅 process 模式（路径不存在、无任何合法记录、UI 模态 index 冲突且策略为 fail）；generate_only 无输入不触发本码——生成产出 0 条时照常 finalize（counts.generated = 0），以退出码 0 结束。 |
| 4 | 致命运行错误（LLM 认证失败、连续不可恢复的 provider 错误超过熔断阈值、输出路径不可写）。v1.6：熔断中止仍原子交付已完成批的主输出与 rejects 并写报告（run.partial_delivery=true，3.10.3 熔断交付）；输出路径不可写则无任何交付。 |

## 2.5 配置体系总览

配置分两层两个文件，职责严格分离；除 API Key（以环境变量**名**在 config.toml 中声明、值从环境读取）外，不使用任何环境变量：

| 文件 | 性质 | 内容 |
|---|---|---|
| `config.toml` | 工具级静态配置。随部署环境变化，跨工程复用。 | LLM API profile 列表（provider、base_url、model、api_key_env / api_key_envs（v1.6 密钥池，3.9.3）、并发、超时、重试、能力声明）、全局日志级别。见 5.1。 |
| `project.toml` | 工程级单次配置。随一次标注任务变化。 | 输入/输出路径与模态、批大小与 seed、各阶段开关与参数、Rubric（内联或选默认）、任务指令与 few-shot、用户输出 JSON Schema（内联或引用外部文件）；v1.7 增分类类别表与按类覆盖（`[classify]` / `[class.<name>.*]` 白名单，5.2）；v1.8 增时序流三节——`[stream]` 输入侧声明（排序与会话规则）、`[segment]`（M14 分段）、`[extract]`（M15 摘取），另有 `annotate.sequence_frames`（5.2）；v1.9 增 `[stitch]`（M16 线索缝合，11 键，5.2）。见 5.2。 |

参数优先级（高覆盖低）：**CLI 参数 > project.toml > config.toml 内的全局默认**。M1 在启动时完成三源合并并冻结为 `ResolvedConfig`，运行期只读。

## 2.6 非功能约束

| 维度 | 约束与设计 |
|---|---|
| 数据不落盘 | 全部中间态（Record、签名、LSH 索引、比较结果、BT 参数、未定稿标注）仅存于进程内存；进程退出即销毁。工具不创建任何临时文件；唯一写盘对象为显式声明的输出通道：用户输出文件、rejects 文件、report.json，以及显式启用（`trace.enabled=true`）时的 trace 日志（7.1——trace 是与主输出同级的输出通道而非中间态落盘，其保留与清理为用户责任）。报告只含计数/分布/耗时/token 统计，不含任何数据内容片段。v1.8 注记：stream 模式下 M2 的未闭合会话缓冲（≤ `session_max_len` 条 Record 元数据，图像仍懒加载）与 M10 的溢出会话（跨批存活封闭清单，3.10.3）同属进程内存、随装箱消费即释放，不构成新的落盘面。 |
| 隐私与网络 | 数据只发送至 config.toml 显式声明的 LLM API 端点；无遥测、无自动更新检查。API Key 只经环境变量进入内存，不写日志、不入报告。 |
| 规模与内存 | 设计目标：单次运行 ≤ 50 万条记录（默认配置下全局 LSH 索引 + 信封对象约占 2–4 GB RSS）。图像字节懒加载：仅在构造该记录的 LLM 请求时读盘并编码，用后即弃，不常驻。超过规模建议按目录分次运行，或设 `dedup.scope="batch"` 降低索引内存。`dedup.semantic=true` 且 scope=global 时另需常驻向量索引，约增加 条数 × 向量维度 × 4 字节（50 万条 × 1024 维 ≈ 2 GB），须计入 RSS 预算。v1.8 注记：序列 Record 以 `members` 元组持成员 Record 的**引用**（frozen 对象共享、零拷贝），episode 化不改变批内存量级；懒加载不变（extract 峰值 2 图/请求、序列 annotate ≤ `sequence_frames` 图/请求）。 |
| 吞吐 | 瓶颈为 LLM API。并发由每 profile 的 `max_concurrency` 信号量控制；同一阶段内记录级并发，阶段之间在批内串行（屏障），保证 pairwise 打分所需的批完整性与实现简单性。 |
| 幂等与可复现 | 无跨运行状态 ⇒ 重跑同一输入产生独立完整输出。配对采样、生成采样均使用 `run.seed` 播种的 PRNG；temperature 默认 0。输出文件以「写临时名 + 完成后原子改名」保证不产生半截主输出（临时名位于输出同目录，属输出交付的一部分，不违反不落盘约束）。v1.7 确定性条件化声明：classify 启用时类池构成与 multi 扇出以分类输出为条件——temperature 0 下端点无逐字节保证，配对计划的可复现性由「仅依赖 seed」条件化为「以分类结果为条件」（generate 回流子批已有同类先例）。v1.8 确定性条件化声明：stream 模式下 episode 构成（边界与噪声判定）与 verify 成员手术以 segment/verify 的 LLM 输出为条件（同上先例）；其余环节全确定性（会话化规则、滑窗缝合、降采样公式均零 rng），且成员手术采用两阶段批级结构（并发评审 → 同步按批位置序执行手术，3.7.3），保证并发调度不引入额外不确定性。v1.9 确定性条件化声明：stitch 启用时线索构成（并入/开新/救援判定）以缝合 LLM 输出为条件（同上先例）；会话内候选流严格串行、会话间并发（3.16.4 调用与校验行），先验合取 / 池逐出 / 接缝标定 / 按碎片配额降采样均为确定性零 rng；`stitch.enabled=false` 时主输出 / rejects / report.json 与 v1.8 逐字节等价（退化锚，3.16.4）。 |
| 容错 | 记录级隔离（1.3 节）；LLM 调用按 profile 配置重试（指数退避+全抖动）；v1.6 密钥池：profile 可声明多把 API Key（`api_key_envs`，5.1），429 按密钥冷却并即时轮换、认证失败按密钥禁用、全池冷却有界驻留（`run.max_park_s`，默认 3600s，超限按重试耗尽计），单密钥配置数据产出与熔断语义不变、429 等待路径有修订（3.9.3 重试行）；连续 `fatal_error_threshold`（默认 20）次不可恢复 provider 错误触发熔断（认证类 401/403 立即熔断、不计连续数，v1.5；v1.6 池化下 = 最后一把存活密钥被认证禁用时），以退出码 4 终止，写出已完成部分的报告并原子交付已完成批的主输出与 rejects（v1.6 熔断交付，3.10.3）。 |
| 依赖面 | Python ≥ 3.11（tomllib 标准库）。第三方仅：`httpx`（异步 HTTP）、`jsonschema`（校验）、`datasketch`（MinHash-LSH）、`Pillow`+`imagehash`（pHash）、`json-repair`（确定性 JSON 修复）、`numpy`（BT 拟合）。无框架级依赖。 |

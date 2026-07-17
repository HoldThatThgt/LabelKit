# 7. 日志系统与可观测性

本章定义 LabelKit 的日志体系（承载模块为 M12，见 3.12）：两条相互独立的输出通道，外加一个不属于日志的进度显示面。本章核心是 7.2 的**事件目录**——它与第 5 章配置表、第 6 章输出格式同级，属对外稳定契约。

## 7.1 日志体系总览

| 输出面 | 形态与开关 | 内容 | 消费方 |
|---|---|---|---|
| ① 运行日志 | stderr，恒开。级别 debug/info/warn/error（`tool.log_level` / `--log-level`）；行格式 `tool.log_format = "text"`（默认）\| `"jsonl"`（7.3）。 | 仅运维事件：生命周期、警告、错误、LLM 调用摘要（debug 级）。绝不含数据内容与提示词。 | 人工排障；日志采集系统。 |
| ② trace 追踪日志 | JSONL 文件，默认 `{output_stem}.trace.jsonl`（`trace.path`）；默认关（`trace.enabled = false`）。文件在**首个事件写出时**才打开/截断（v1.5）：死于配置或输入校验的运行不会碰上一次的 trace；dry-run 写 `{name}.dryrun{suffix}` 独立文件。 | 结构化事件流，一行一事件（7.2）；按 `trace.channels` 过滤通道，按 `trace.content` 控制内容量（7.4）。 | rubric 优化（7.5）；标注质量分析与审计；后续 `labelkit analyze`（8.3 O5）。 |
| ③ 进度显示（v1.10 起称 console） | 三态 `console.mode = auto \| rich \| plain`（5.1 / CLI `--console`），恒开。rich = 双区内联实时面板；plain = v1.9 行为逐字节保留；auto 按 TTY 等判定链选档（7.7）。 | 批进度、流水线段棋盘、各状态计数、LLM 用量/密钥池/熔断、瞬时成本累计（7.7 区块表）。 | 交互终端前的人。不属于日志（7.7）。 |

**与「工具不存储数据」原则的关系：**trace 是用户**显式启用**的输出通道，与主输出、rejects 同级（2.6「数据不落盘」行已将其列入唯一写盘对象），而非中间态落盘——它记录的是「处理判定及其依据」而非批中间态本身。trace 文件的保留、脱敏档位选择（7.4）与清理均为用户责任；`content="full"` 档含数据内容，风险见 7.4 明示。

图 7-1 双通道日志架构。进度显示走 stderr 但不经日志设施（7.7）。

## 7.2 事件目录

通道归属规则：**事件名前缀即通道名**（`ingest. / segment.（v1.8）/ stitch.（v1.9）/ dedup. / classify.（v1.7）/ extract.（v1.8）/ quality. / annotate. / verify. / schema. / llm.`，对应 `trace.channels` 的十一个可选值——v1.7 增 `"classify"`，v1.8 增 `"segment"`、`"extract"`，v1.9 增 `"stitch"`（通道 = stage 名，与 classify 同构，S1），默认值不变，5.2）；`run.*` 与 `batch.*` 生命周期事件由 M10 发出、`stage="run"`、`record_ids` 为空，**不受 channels 过滤**（`trace.enabled=true` 即写）；`error` 事件按产生它的 stage 归属通道（segment/extract 阶段的 error 事件自动按此归属，零路由代码改动，S1）。本目录为稳定契约：`trace_schema_version = 1`，后续版本**只增不改**（可新增事件与 payload 字段，不改既有字段语义）。

| 事件名 | 通道 / stderr 级别 | 触发点 | payload 字段 |
|---|---|---|---|
| `run.start` | 恒写 / info | M1 校验通过、首批开始前；trace 文件首行 header 事件。 | `tool_version`、`config_digest`、`project_digest`（同 6.4 run 节）、`trace_schema_version`（=1，仅此事件携带，避免逐行冗余）。 |
| `run.end` | 恒写 / info | finalize 完成后（trace 末行）。 | `counts`（与 report.json counts 同构的摘要对象）、`exit_code`。 |
| `batch.start` | 恒写 / debug | 批构造完成（PipelineItem[] 就绪）。 | `size`。 |
| `batch.end` | 恒写 / info | 批 emit 并释放中间态后。 | `active`、`dropped_dup`、`dropped_lowq`、`dropped_verify`、`failed`、`duration_ms`；v1.7 增可选字段 `fanout`（仅 classify 启用时携带；`batch.start.size` 语义 = 批入口信封数即扇出前基数，扇出后各状态计数和 = size + fanout，3.10.3 分类与扇出行）；v1.8 增可选字段 `episodes`、`absorbed`、`dropped_noise`（仅 segment 启用时携带，fanout 同形制，3.10.3 时序流行）；v1.9 增可选字段 `stitched`、`threads`（仅 stitch 启用时携带，同形制，3.10.3 线索缝合行）。 |
| `ingest.bad_line` | ingest / warn | M2 坏行跳过（3.2.5）。 | `file`、`line_no`、`reason`。 |
| `ingest.missing_pair` | ingest / warn | M2 缺对跳过（3.2.4）。 | `index`、`present`（"tree"\|"image"，实际存在的一侧）、`file`。 |
| `ingest.index_conflict` | ingest / warn（fail 策略时 error） | M2 index 冲突（3.2.4）。 | `index`、`files`（冲突文件路径列表）。 |
| `ingest.disorder` | ingest / —（trace-only，无逐事件 stderr 镜像；v1.8） | M2 流式单调性校验拒绝一条记录时（乱序或时间戳解析失败，`stream.on_disorder`，3.2/6.1）；skip 策略下每记录一事件，M2 自身另打**一条全运行仅一次的 data-free stderr WARN**（reason 含时间戳/游标值故不入 stderr——§7.1 ①）；fail 策略经 InputError 以退出码 3 终止。 | `file`、`line_no`（文本）\| `index`（UI）、`reason`（"乱序" \| "时间戳解析失败" 类文案，含违规值——仅 trace 通道）。 |
| `segment.session` | segment / —（trace-only，无 stderr 镜像；v1.8） | M2 会话装配器闭合一个候选会话时（3.2 会话化行；`--limit` 截断视同 EOF 冲洗尾会话，S17）——发出方是 M2，但按前缀归 segment 通道（S1）；`record_ids` 为空。 | `session_id`、`first` / `last`（会话首/末帧）、`len`、`cause`（"gap" \| "key" \| "max_len" \| "max_span" \| "eof" \| "limit"）。 |
| `segment.boundary` | segment / —（trace-only，无 stderr 镜像；v1.8） | M14 每个滑窗裁决经 M8 校验通过后（3.14）；`record_ids` 为空（成员溯源在 payload）。 | `session_id`、`window`（= [s, e] 窗口帧位次区间）、`member_ids`、`relations`[]{`index`, `relation`}（五值封闭词表，3.14）、`model`、`reason`†（逐帧关系理由，条件见表下注）。 |
| `stitch.judge` | stitch / —（trace-only，无 stderr 镜像；v1.9） | M16 每候选判定定案后（votes 聚合之后；一遍与二遍均发，3.16.6）；`record_ids` = [候选碎片首成员 id]。 | `session_id`、`candidate`（"episode"\|"rescue"）、`repass`（bool，false = 一遍 / true = 二遍）、`verdict`（votes 分裂回落时记保守结局 "new"）、`thread_ref`、`confidence`（仅观测，不进门槛，3.16.3）、`priors`（机械先验命中腿列表，⊆ {app_overlap, entity_overlap, same_page}）、`merged`（bool）；条件字段：`votes_split`（= true，仅严格多数不成立回落时携带）、`task_name`¶、`reason`¶（votes 分裂时不携带）、`target_thread_id`（仅 merged 时携带）。 |
| `stitch.thread` | stitch / —（trace-only，无 stderr 镜像；v1.9） | 会话缝合定案后每线索一条（3.16.6）；`record_ids` = [幸存信封 record.id]。 | `session_id`、`thread_id`、`task_name`¶、`fragments`[]{`order_span`, `member_count`, `cause`, `source_episode`}（碎片跨度表）、`seam_indexes`。 |
| `dedup.duplicate` | dedup / — | M3 判重时；`record_ids` = [被判重记录 id]。 | `kind`、`cluster_key`、`kept_id`（同 DedupInfo，4.2）、`jaccard`（near_text：实测估计值）或 `hamming`（near_image：实测距离）或 `cosine`（near_semantic：实测余弦相似度，v1.2 增）；精确重复三者皆无。 |
| `classify.decision` | classify / —（trace-only，无 stderr 镜像，同 quality.judgment；v1.7） | M13 每记录分类定案后（3.13.4）；`record_ids` = [记录 id]。 | `label`（本信封路由标签）、`labels`（multi 时携带命中全集）、`source`（"llm"\|"fallback"\|"inherited"）、`reason`†、`sc`（= {n, agreement_ratio}，仅 `classify.self_consistency` 启用时携带）。 |
| `extract.step` | extract / —（trace-only，无 stderr 镜像；v1.8） | M15 每对相邻成员帧的转移摘取定案后（含 fallback，3.15）；`record_ids` = [s_i.id, s_{i+1}.id]（前后帧记录 id）。 | `episode_id`、`index`、`action_type`、`description`‡（LLM 产出文本，refs 档起）、`target`§ / `value`§（**输入数据派生**字段，excerpt 档起——S27，7.4 `_DATA_KEYS` 行）。 |
| `quality.judgment` | quality / — | M4 每次 pairwise 裁决经 M8 校验通过后；`record_ids` = [记录甲, 记录乙]（采样顺序）。rubric 优化的核心事件。 | `order`（{"A": id, "B": id}，随机化后的呈现顺序）、`model`、`judgments`[]{`criterion`, `winner`("A"\|"B"\|"tie"), `reason`†}；v1.2 增可选字段 `judge`（= 评审 profile 名，仅 `quality.judges` 非空时携带，3.4.3）；v1.7 增可选字段 `pool`（= 类名，仅 classify 启用时携带，3.4.3 按类分池行）。 |
| `quality.pointwise` | quality / — | M4 pointwise 每记录每 criterion 打分后。 | `criterion`、`score`（0–5 原始分）、`reason`。 |
| `quality.bt_fit` | quality / — | M4 每批每 criterion BT 拟合结束（批级，record_ids 为空）。 | `criterion`、`iterations`、`converged`、`comparisons`（参与拟合的比较数）；v1.7 增可选字段 `pool`（= 类名，仅 classify 启用时携带——分池后拟合可归因，3.4.3 按类分池行）。 |
| `quality.gate` | quality / — | M4 质量门判定（配置了 `quality.threshold`，或 `quality.selection = "top_ratio"` 时，3.4.3）。 | `aggregate`、`decision`（"keep"\|"drop"）、`threshold`（threshold 选择时）；v1.2 增可选字段 `selection`、`top_ratio`、`rank`（top_ratio 选择时携带，payload 只增不改）；v1.7 增可选字段 `pool`（= 类名，仅 classify 启用时携带，3.4.3 按类分池行）。 |
| `annotate.done` | annotate / — | M5 标注经 M8 通过后。 | `attempts`（同 Annotation.attempts，4.2）；v1.2 增可选字段 `sc` = {n, agreement_ratio}（self-consistency 启用时，3.5.2）；v1.7 增可选字段 `label`（= 信封路由标签，仅 classify 启用时携带，3.5.2 按类取值段）。 |
| `verify.verdict` | verify / — | M7 每轮评审后（round 从 1 计，repair 策略下每轮一事件）。 | `verdict`、`round`、`critiques`[]{`aspect`, `opinion`}；v1.2 增可选字段 `judge`（`verify.judges` 非空时每 judge 一事件，3.7.2）；v1.7 增可选字段 `label`（仅 classify 启用时携带，3.7.2 按类取值段）；v1.8 增可选字段 `defects`[]{`kind`, `members`, `position`, `detail`}（仅 stream 缺陷表评审时携带，受 7.4 分级——`detail` 属自由文本，3.7.2 缺陷表段/S31）。 |
| `schema.repair` | schema / — | M8 任何非 clean 路径出结果时（L1 修复命中 / L3 各轮 / 拒绝）。 | `resolved_at`（"l1"\|"l3_1"\|"l3_2"\|"rejected"，同 6.4 命名）、`violations`（JSON Pointer 路径 + 违反的 Schema 关键字摘要，不含数据值）；违规清单中来自 `output.validator` 回调的条目以 `(validator) ` 前缀标识（v1.5，3.8.2 L2.5）；v1.5 增可选字段 `l1_lossy`（=true，仅当 L1 修复疑似截断内容——json-repair 对未转义内引号的截断故障启发式，命中时另发 stderr warn，payload 只增不改）。 |
| `llm.call` | llm / debug（stderr 摘要行恒有） | M9 每次调用返回后（含失败）。不含提示词与响应内容（full 档例外，7.4）。 | `profile`、`gen_ai.request.model`、`latency_ms`、`gen_ai.usage.input_tokens`、`gen_ai.usage.output_tokens`、`retries`、`status`（"ok"\|"retryable_exhausted"\|"fatal"\|"breaker_aborted"——v1.5 只增：重试退避途中熔断打开、该逻辑调用被中止时发出，retries 携带已消耗次数）；v1.2 增可选字段 `operation`（="embedding"，仅 M9 `embed()` 调用携带，缺省即对话补全；payload 只增不改）；v1.6 增可选字段 `key_env`（= 本次逻辑调用**最后一次尝试**所用密钥的环境变量名，成功失败同义；零尝试即终止的调用——如入口即驻留超限/breaker_aborted——不携带；仅密钥池 >1 的 profile 携带；payload 只增不改）。 |
| `llm.key_cooldown` | llm / — | v1.6：M9 密钥进入 429 冷却时（每次冷却一事件，3.9.3 密钥池行）。任意池大小（含 1）均发出——单密钥 429 的等待在 v1.6 亦经冷却/驻留路径。 | `profile`、`key_env`、`cooldown_s`（本次冷却秒数）、`retry_after`（bool，冷却时长是否来自 `Retry-After` 头）。 |
| `llm.key_disabled` | llm / warn | v1.6：M9 密钥 401/403 认证禁用时（每密钥每运行至多一次；任意池大小含 1——单密钥场景该事件先于立即熔断发出）。 | `profile`、`key_env`、`status_code`。 |
| `llm.pool_parked` | llm / warn | v1.6：M9 某 profile 全部存活密钥均在冷却、调用开始驻留时（每次驻留一事件；任意池大小含 1）。 | `profile`、`wait_s`（预计驻留秒数）、`live_keys`（存活密钥数）。 |
| `error` | 随产生 stage 的通道 / warn（记录级）· error（运行级） | StageError 构造时。 | `stage`、`kind`（取值见 7.6）、`message`、`retryable`——即 StageError 全字段（4.2）；v1.7 增可选字段 `label`（仅 classify 启用时携带——multi 扇出下消歧同 id 兄弟信封，3.13.4）。 |

† `reason` 仅当 `quality.judgment_reasons` 生效时存在（5.2）；`classify.decision` 的 `reason` 条件独立（v1.7）= `trace.enabled = true` 且 `trace.channels` 含 `"classify"`（零额外 token 原则，3.13.4 调用与校验行）；`segment.boundary` 的 `reason` 条件同款（v1.8）= `trace.enabled = true` 且 `trace.channels` 含 `"segment"`（对应窗口内部 Schema 的 with_reason 参数，零额外 token，3.14）。¶ `stitch.judge` / `stitch.thread` 的 `task_name` 与 `reason`（v1.9）无请求条件——`stitch_schema()` 恒含两键（判定量级小、votes 聚合需按多数簇取值，3.16.3），但作为 LLM 自由文本受 7.4 分级：`none` 档剥除、`refs` 档起携带（`task_name` 为 v1.9 新增自由文本键，7.4）。‡ / § 为 `extract.step` 的内容分档标记（v1.8，S27）：`description` 自 `"refs"` 档起、`target` / `value` 自 `"excerpt"` 档起携带（7.4）。全部自由文本字段（reason / critiques / violations 文本）受 7.4 脱敏档位控制。密钥相关事件（v1.6）只携环境变量**名**——密钥值在任何档位、任何通道均不落日志（7.4 规则不变）。

## 7.3 记录格式规范

stderr 两种格式承载同一信息，由 `tool.log_format` 选择；trace 事件行是 TraceEvent（3.12.3）的 JSON 序列化，恒为 UTF-8 单行（下例因排版自动折行）。

```
# ── stderr, tool.log_format = "text"（行格式: ts level stage batch msg）──
2026-07-02T09:31:04+08:00 INFO  quality batch=3 pairwise 完成 items=128 comparisons=256 judgment_failures=1
2026-07-02T09:31:12+08:00 WARN  ingest  batch=4 bad_line file=ime-2026-06-30.jsonl line=217 reason=missing_text_field

# ── stderr, tool.log_format = "jsonl"（同一事件）──
{"ts":"2026-07-02T09:31:04+08:00","level":"info","stage":"quality","batch":3,"msg":"pairwise 完成 items=128 comparisons=256 judgment_failures=1"}
```

```
# ── trace 事件一：quality.judgment（文本模态意图标注工程；content="refs"，judgment_reasons 生效）──
# 记录甲 1cda030abc565f17 = {"instruction": "帮我写一条请假条，明天上午要去医院", ...}
# 记录乙 d5ad41d6357f8a55 = {"instruction": "写一份周报模板", ...}；本次呈现顺序随机为 A=乙, B=甲
{"ts":"2026-07-02T09:31:04.482+08:00","run_id":"f3a9c04b7d21","batch_no":3,"stage":"quality","ev":"quality.judgment","record_ids":["1cda030abc565f17","d5ad41d6357f8a55"],"payload":{"order":{"A":"d5ad41d6357f8a55","B":"1cda030abc565f17"},"model":"qwen2.5-vl-72b-instruct","judgments":[{"criterion":"writing_style","winner":"tie","reason":"两条指令表达均通顺完整，写作水平相当。"},{"criterion":"facts_trivia","winner":"B","reason":"B 含明确时间与事由（明天上午去医院），具体信息更多。"},{"criterion":"educational_value","winner":"B","reason":"B 是带场景约束的写作任务，示范价值更高。"},{"criterion":"required_expertise","winner":"tie","reason":"两者均为日常任务，无专业门槛差异。"}]}}

# ── trace 事件二：verify.verdict（UI 模态登录页工程；content="refs"）──
{"ts":"2026-07-02T10:02:17.905+08:00","run_id":"0c47d9e2b8a5","batch_no":3,"stage":"verify","ev":"verify.verdict","record_ids":["9f2c31ab52e08d17"],"payload":{"verdict":"pass","round":1,"critiques":[{"aspect":"任务指令遵循","opinion":"四个字段均按指令填写，screen_category=login 与截图一致。"},{"aspect":"事实一致性","opinion":"interactive_elements 与控件树可交互节点逐一对应，bounds 未见编造。"},{"aspect":"字段语义","opinion":"page_title 取自屏幕顶部标题控件文本，正确。"}]}}
```

LLM 相关 payload 的字段命名（`gen_ai.request.model`、`gen_ai.usage.input_tokens / output_tokens`，full 档的 `gen_ai.input.messages / gen_ai.output.messages`）对齐 OpenTelemetry GenAI 语义约定 [27]，便于 OTel 生态的采集与分析工具直接消费。注意两点：这是**命名对齐而非实现依赖**（不引入 OTel SDK）；且该约定截至本文档日期处于 Development（实验性、非 stable）状态 [27]——若其后续更名，本工具事件目录按「只增不改」原则保持自身稳定。

## 7.4 内容脱敏策略

`project.toml` 的 `trace.content` 四档决定 trace 中的内容量，逐档递增；**API Key 在任何档位、任何通道都不落日志**（M9 不将认证头传入日志路径）：

| 档位 | trace 事件 payload 中出现的内容 | 典型用途 |
|---|---|---|
| `"none"` | 仅记录 id、枚举与数值等结构化字段；`reason`、`critiques`、`violations` 等 LLM 产出文本一律不写。 | 最严格合规场景；7.5 的全部比率类指标仍可计算。 |
| `"refs"`（默认） | 另含 LLM 产出的 reason / critique / violations 文本，但不含任何输入数据内容（与 `output.rejects="refs"` 同一语义，3.11.2）。 | rubric 优化常规档（7.5）。 |
| `"excerpt"` | 另含输入内容前 200 字符：`quality.judgment / quality.pointwise / annotate.done / verify.verdict` 的 payload 增加 `excerpt` 字段（`{record_id: 前 200 字符}` 逐条给出，quality.judgment 含两条；文本模态取 `Record.text`、UI 模态取 `UITree.serialize()` 输出，不含图像）。 | 免于回原始文件比对的快速人工审查。 |
| `"full"` | 另含完整提示词与响应：`llm.call` 事件 payload 增加 `gen_ai.input.messages / gen_ai.output.messages`（须同时启用 `llm` 通道）。 | 调试与审计。 |

**v1.8 只增（S27）：**`extract.step` 的 `target` / `value` 是**输入数据派生**字段（目标控件文本引用、键入文本——可能含用户输入内容），归入新增剥除集 `_DATA_KEYS = {"target", "value"}`：`"none"` 与 `"refs"` 档一律剥除（守住 refs 档「不含任何输入数据内容」红线），自 `"excerpt"` 档起携带；`description` 为 LLM 产出文本，计入既有自由文本集 `_FREE_TEXT_KEYS`：`"none"` 档剥除、自 `"refs"` 档起携带（与 reason / critiques 同级）；`verify.verdict` 的 v1.8 可选字段 `defects`（缺陷表含自由文本 `detail`）同入 `_FREE_TEXT_KEYS`——`"none"` 档整键剥除（与 critiques 同级）。逐事件分级速查：`extract.step` none = {episode_id, index, action_type}、refs = +description、excerpt = +target/value；`segment.boundary` none = 结构字段（session_id / window / 逐帧 relation）、refs = +reason（reason 键已在自由文本集）。三个 v1.8 事件的 stderr 镜像均无（trace-only，7.2）。

**v1.9 只增：**`task_name`（线索任务名——`stitch.judge` / `stitch.thread` payload 及缝合判定输出的滚动线索名，3.16）为 LLM 产出文本，计入 `_FREE_TEXT_KEYS`：`"none"` 档剥除、自 `"refs"` 档起携带（与 reason / description 同级）。逐事件分级速查：`stitch.judge` none = {session_id, candidate, repass, verdict, thread_ref, confidence, priors, merged[, votes_split, target_thread_id]}、refs = +task_name/reason；`stitch.thread` none = {session_id, thread_id, fragments, seam_indexes}、refs = +task_name。两个 v1.9 事件的 stderr 镜像均无（trace-only，7.2）。

**风险明示：**`content="full"` 时 trace 文件包含全部经手数据与模型输出，体积可达主输出的数十倍，且构成一份完整的数据副本——仅在调试/审计时短期启用，用后由用户负责清理。

## 7.5 rubric 优化闭环

trace 的首要用途：把「LLM 依据 rubric 做出的每一次裁决及其理由」暴露给人，驱动准则迭代。流程：① `project.toml` 设 `trace.enabled=true`、`channels` 含 `"quality"`、`quality.judgment_reasons=true`（或保持 "auto"）；② `--limit` 小样本运行；③ 从 trace 计算下表诊断指标并抽读 reason；④ 修订 rubric（改写 pairwise_prompt、合并/删除/新增 criterion、调 weight）；⑤ 同一 `run.seed` 小样本重跑，对比指标是否收敛；⑥ 定稿后全量运行（可关 judgment_reasons 省 token）。

| 信号 | 计算（基于 7.2 事件） | 诊断 | 处置 |
|---|---|---|---|
| tie 率过高 | 某 criterion 的 `winner="tie"` 占比（quality.judgment）；参考线 > 0.4。 | 准则区分度不足。 | 把 pairwise_prompt 的比较问句改为更具体的判据；或降低该 criterion 权重。 |
| 同 seed 重跑翻转率 | 同一 `run.seed`、同一输入重跑两次（配对方案逐对相同，1.3 可复现性——流程 ⑤ 的重跑天然产生第二份 trace），按无序对对齐两次 quality.judgment，统计 winner 相反的对占比；参考线 > 0.3。 | 准则表述含糊 / 模型不稳定。 | 细化 description 与判据；确认 temperature=0；必要时更换裁决 profile。 |
| criteria 间胜负相关性过高 | 两 criterion 在同一次比较中 winner 一致的占比；参考线 > 0.9。 | 准则冗余，可合并。 | 合并为一条（权重相加）或删除其一。 |
| reason 关键词缺口 | reason 文本聚类/高频词中反复出现 rubric 未覆盖的评价维度（需 judgment_reasons 生效且 content ≥ refs）。 | 准则缺口。 | 新增 criterion——这正是 criteria drift 的预期表现 [30]。 |
| judgment_failures 率 | `error` 事件中 `kind="judgment_invalid"` 数 / 总比较数（亦见 report.quality.judgment_failures）；参考线 > 0.05。 | 裁决提示词或内部 Schema 问题。 | 缩短/消歧 rubric 文案；确认 profile 的结构化输出能力（L0）。 |

jq 单行示例——统计文本工程中 `facts_trivia` 准则的 tie 率：

```
jq -s '[.[] | select(.ev=="quality.judgment") | .payload.judgments[]
        | select(.criterion=="facts_trivia")]
       | (map(select(.winner=="tie")) | length) / length' ./out/ime-labels-0630.trace.jsonl
# 输出示例: 0.4375  → 高于 0.4 参考线，该准则对输入法指令数据区分度不足，应改写判据
```

**背书：**EvalGen（UIST 2024）通过混合主动式实验确认了 **criteria drift**：评估准则无法先验完全确定——人需要看到 LLM 评审的具体输出才能修订、新增准则，准则部分依赖于观察到的输出 [30]；因此评审的中间产物（逐次裁决与理由）必须暴露给人，这正是 quality.judgment 事件的存在理由。CritiQ（ACL 2025）进一步证明「成对偏好信号 → 自然语言质量准则」方向可行：约 30 对人工偏好即可自动挖掘出可解释准则 [31]。本节闭环即两文献结论的工程化：trace 承担 EvalGen 的对齐界面职责，人工（或后续 `labelkit analyze`，8.3 O5）承担 CritiQ 的准则挖掘角色。

## 7.6 错误分类码（StageError.kind）

| kind | 级别 | 产生点与处置 |
|---|---|---|
| `bad_input_line` / `missing_pair` / `index_conflict` / `image_too_large` | 记录级 | M2；按 input.* 策略 skip（计数）或 fail（退出码 3）。 |
| `image_decode_error` | 记录级 | M3 跳过 pHash 层；M5/M7 遇到时该记录 failed。 |
| `segmentation_invalid` | 窗口级 | v1.8：M14 单窗边界裁决经 M8 修复耗尽——`segment.on_error="keep"`（默认）时该会话整体成一个 episode 存活，留痕三件套 `_meta.stream.degraded = {kind, windows_failed}` + error 事件 + `segment.failures` 计数（**不写 item.errors**——归因防污染，S26）；`"fail"` 时会话成员全部 failed → rejects（3.14）。 |
| `stitch_invalid` | 判定级 | v1.9：M16 单候选缝合判定经 M8 修复耗尽——`stitch.on_error="keep"`（默认）时 episode 候选开新线索存活 / 救援候选维持 dropped_noise，留痕两件 = error 事件 + `stitch.failures` 计数（**不写 item.errors**——S26 同则；无 `_meta` 腿，`degraded` 键保持 segment 专属，3.16.6）；`"fail"` 时**仅 episode 候选信封** failed → rejects（救援候选不适用 fail 路径，3.16.6）。 |
| `classification_invalid` | 记录级 | v1.7：M13 分类输出经 M8 修复耗尽——`classify.on_error="fallback"`（默认）时归兜底类并留痕于 `Classification.detail`（不入 rejects，记录存活）；`"fail"` 时记录 failed → rejects（3.13.4 失败与兜底行）。 |
| `extraction_invalid` | 转移级 | v1.8：M15 单转移动作摘取经 M8 修复耗尽——`extract.on_error="fallback"`（默认）时该步记 `action_type="other"` 并留痕于 `Transition.detail = {kind, message}`（episode 存活，**不写 item.errors**，S16）；`"fail"` 时 episode failed → rejects（3.15）。 |
| `judgment_invalid` | 比较级 | M4；按 tie 计入 BT（3.4.3）。 |
| `schema_violation` | 记录级 | M8 L3 耗尽；记录 failed → rejects。 |
| `callback_violation` | 记录级 | v1.5：M8 L3 耗尽且剩余违规全部来自 `output.validator` 回调（3.8.2 L2.5）；记录 failed → rejects。 |
| `provider_retryable_exhausted` | 记录级 | M9 重试耗尽（v1.6 含驻留超限 `run.max_park_s`，3.9.3 密钥池行）；记录 failed，计入熔断窗口。 |
| `provider_fatal` | 运行级 | M9 不可重试错误。400/404 等计入连续熔断计数，连续达阈值 ⇒ 退出码 4；**401/403 认证类立即熔断**（v1.5，3.9.3）。v1.6 密钥池：认证失败先按密钥禁用（`llm.key_disabled`），池内尚有存活密钥时不产生本错误、不计入熔断——仅当禁用的是该 profile 最后一把存活密钥时才抛出并立即熔断（3.9.3 密钥池行）。 |
| `internal_error` | 记录级 | 任何未预期异常（含 M11 终检失败）；记录 failed，堆栈入日志（debug 级）。 |

## 7.7 进度显示与结束摘要（v1.10：三态 console）

进度显示**不属于日志**：直接写 stderr 而不经 logging 模块、无级别概念、不产生 trace 事件。v1.10 将本面重写为三态 **console**（设计裁决 U1–U18 与工业调研 [C-1]–[C-20] 见 `docs/dev/SPEC-tui-console.md`；**规格定稿、实现另行排期**——动工前以该文 §3.8 实施清单为纲）。

**三态与判定**（`console.mode`，5.1；CLI `--console` 覆盖）：

```
auto → rich 当且仅当：stderr.isatty() ∧ tool.log_format == "text"
                   ∧ 未设 NO_COLOR ∧ TERM 非 "dumb"/空 ∧ rich 可导入（懒 import）
其余一律 plain。显式 --console rich 尊重显式档（CI 录 ANSI 场景）；
tool.log_format = "jsonl" 强制 plain 且不可覆盖（stderr 逐行可 json.loads 铁律；显式冲突 M1 WARN）。
```

**plain 档（回归锚）**：与 v1.9 行为逐字节等价（`console.heartbeat_s = 0` 默认下）——TTY 单行 `\r` 批级进度（批号 + 五状态固定键集，**T16 键集约束在本档继续成立**：stitched 不入行）；非 TTY 每批一行摘要（即 `batch.end` 的 stderr info 行）；运行结束打印与 report.json `counts` 完全一致的文本版终版摘要表。可选心跳：`console.heartbeat_s > 0` 且非 TTY 时每 N 秒一行数据无关汇总 `heartbeat batch= stage= llm_calls= elapsed=`（固定间隔不漂移；CI 长批「像死机」缓解，默认关）。

**rich 档（双区内联实时面板）**：日志在上方滚动区照常输出（行文本与 plain 逐字节一致——渲染器接管 `labelkit` logger 的 handler 流、退出/降级时恢复），终端底部画布按 `console.refresh_hz` 节流原地重绘；**永不进入 alternate screen**（批任务保 scrollback，U1）。画布六区块（数据源全部为既有结构字段，零新增采集）：

| 区块 | 内容 | 数据源 |
|---|---|---|
| 标头 | run_id、mode/modality（stream/stitch 徽标）、seed、耗时、ETA（仅批总数分母可得时显示，EMA 外推标 `~`） | ResolvedConfig、`run.start` |
| 批进度 | UI 模态 `批 i/N` + scanned（IngestPlan 配对扫描廉价；stream 批数 = next-fit 仿真精确）；文本模态默认 `批 i` 无分母——行数估算需全量多读一遍输入，默认不做（M10 现状），`console.estimate = true` 显式换购（U17） | `batch.start/end`、`_estimate()` 复用 |
| 段棋盘 | 仅启用 stage 按链序：`✓` 已过 / `▶` 进行中（该 stage `llm.call` 完成数/静态估算分母，标「估算」，3.10.3 dry-run 同款公式）/ `·` 待走 | stage_begin 旁路（3.12.3）+ `llm.call` 累计 |
| 状态账 | 九态计数，stream/stitch 键仅启用时在场、同 report.counts 口径——**stitched/threads 的展示为对 T16 的有界修订（仅本档；对齐决策 1.6 U18）**；批内随批末更新（counts.* 为 post-emit tally） | MetricsSink counters |
| LLM | 每 profile：在途/并发上限、calls、retries、tokens ↑↓、成本（未配价目 `—`）、p50 延迟；密钥池行（**环境变量名** + ok/冷却剩余秒/禁用）；熔断 streak/threshold，打开时红色横幅 | `LLMClient.snapshot()` 每 tick 只读拉取（3.12.3） |
| 键位提示 / 中断态 | 交互键提示一行（下表）；SIGINT 后顶部横幅「正在优雅中断（≤30s）…」 | 3.10.3 中断路径旁路转发 |

generate_only：批进度区退化为 `生成 ▶ calls i/N · 已产 n 条`，批棋盘自再流批次起激活。运行结束：最后一次重绘后**定格**为静态终版面板（counts 表 + per-stage 耗时横条 + llm_usage 表 + rejects/trace 路径行），scrollback 保留完整日志。

**rich 档键盘开关**（一期实施，对齐决策 1.6 U15；生效合取：rich ∧ stdin TTY ∧ `console.interactive = true` ∧ termios 可用，否则纯渲染；封闭键集，未列键忽略）：

| 键 | 行为 |
|---|---|
| `?` / `h` | 键位帮助展开/收起 |
| `l` | LLM 面板展开（每密钥一行：env 名、状态、calls、rate_limited）/收起 |
| `e` | 最近错误条开/关（环形最近 5 条 `error` 事件的 stage + kind——7.6 封闭词表，数据无关） |
| `+` / `-` | 画布行数上限增/减（4–16） |
| `p` | 暂停/恢复画布重绘（日志照常滚动；调试/复制友好） |
| `q` | 面板脱离：余下运行降级 plain（不终止运行、不影响退出码） |

终端状态纪律：`tty.setcbreak`（非全 raw——保留 ISIG，**Ctrl-C 产生 SIGINT 的语义不变**，仍走 3.10.3 优雅中断）；退出/降级/异常路径经 finally 恢复 termios 属性；键盘轮询在渲染 tick 内非阻塞 select，零新线程。stdin 被占用的代价（粘贴的后续命令被吞）以 `console.interactive = false` 回避。

**信息纪律与失败语义（红线，U6/U7）**：面板与心跳行 = stderr 镜像同级——只显示计数、枚举、profile 名、密钥环境变量名、file:line 结构字段；**不显示 record id、excerpt、reason/task_name/critiques 等任何 LLM 自由文本与输入数据内容**。渲染期任何异常自吞 + 一次性 WARN + 当场降级 plain 续跑，**渲染永不影响退出码与数据产出**；终端宽 < 60 列退化为单行 `\r` 形态。面板为 M12 的第四个纯消费面（3.12.3 ProgressListener 旁路）：**零 7.2 事件目录改动、report.json 零新键**。

## 7.8 测试要求（验收级）

| 层 | 要求 |
|---|---|
| 单元 | L1 确定性修复穷举样例集（围栏/截断/单引号/尾逗号/前后缀噪声）；BT 拟合对已知解析解（两元素、全胜、tie-only）误差 < 1e-4；配对采样在固定 seed 下逐字节可复现；UI 配对表（图 3-1）全分支：冲突/缺对/跨目录/前导零。 |
| 集成 | 以 mock provider（录制响应）跑通 2.3.1 全部组合矩阵；断言 report 不变量、主输出全行过用户 Schema、rejects=refs 时文件不含任何输入内容子串。 |
| 契约 | 对真实 API 的 probe 冒烟（CI 可选）；结构化输出 L0 关闭/开启两态等价性（最终输出结构一致）。 |
| 日志（v1.1 新增） | trace 每行可被 `json.loads` 解析且恰含 ts / run_id / batch_no / stage / ev / record_ids / payload 七字段；对 7.2 事件目录逐事件断言 payload 字段齐全；首行为 run.start 且携带 trace_schema_version=1；`trace.content="refs"` 时 trace 文件不含任何输入内容子串（与 rejects=refs 同法断言）；注入写失败（不可写路径 / mock OSError）断言运行不中断、warn 恰打印一次、`report.trace.dropped_events` 计数正确。 |
| console（v1.10 新增，实施期生效） | 定宽渲染快照断言（`Console(width=100, force_terminal=True)`，喂 MetricsSink 计数器状态而非 LLM 响应——不违反真实 LLM 测试纪律）：九态账 / 密钥池三态 / 熔断横幅 / 中断横幅 / 窄终端退化 / generate_only 形态 / `l`·`e` 展开态；回归锚：`--console plain` 对 examples 六工程实跑 stderr 与 v1.9 基线逐字节 diff 为空（heartbeat 默认关）、`log_format="jsonl"` 下 stderr 逐行可解析且显式 rich 被拒并 WARN；降级注入：渲染 tick 抛异常 ⇒ 运行照常完成、退出码不变、恰一条 WARN、自动转 plain；键盘：伪 TTY 注入键序断言 `q` 脱离 / `p` 暂停期日志照常 / termios 属性退出后逐字节复原 / cbreak 下 Ctrl-C 仍触发 SIGINT 优雅中断；协议：`listener=None` 路径零行为变化、全回调 O(1) 无 I/O。 |

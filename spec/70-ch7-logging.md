# 7. 日志系统与可观测性

本章定义 LabelKit 的日志体系（承载模块为 M12，见 3.12）：两条相互独立的输出通道，外加一个不属于日志的进度显示面。本章核心是 7.2 的**事件目录**——它与第 5 章配置表、第 6 章输出格式同级，属对外稳定契约。

## 7.1 日志体系总览

| 输出面 | 形态与开关 | 内容 | 消费方 |
|---|---|---|---|
| ① 运行日志 | stderr，恒开。级别 debug/info/warn/error（`tool.log_level` / `--log-level`）；行格式 `tool.log_format = "text"`（默认）\| `"jsonl"`（7.3）。 | 仅运维事件：生命周期、警告、错误、LLM 调用摘要（debug 级）。绝不含数据内容与提示词。 | 人工排障；日志采集系统。 |
| ② trace 追踪日志 | JSONL 文件，默认 `{output_stem}.trace.jsonl`（`trace.path`）；默认关（`trace.enabled = false`）。文件在**首个事件写出时**才打开/截断（v1.5）：死于配置或输入校验的运行不会碰上一次的 trace；dry-run 写 `{name}.dryrun{suffix}` 独立文件。 | 结构化事件流，一行一事件（7.2）；按 `trace.channels` 过滤通道，按 `trace.content` 控制内容量（7.4）。 | rubric 优化（7.5）；标注质量分析与审计；后续 `labelkit analyze`（8.3 O5）。 |
| ③ 进度显示 | TTY 进度条 / 非 TTY 批摘要，恒开。 | 当前批号/总批数、各状态计数、瞬时成本累计。 | 交互终端前的人。不属于日志（7.7）。 |

**与「工具不存储数据」原则的关系：**trace 是用户**显式启用**的输出通道，与主输出、rejects 同级（2.6「数据不落盘」行已将其列入唯一写盘对象），而非中间态落盘——它记录的是「处理判定及其依据」而非批中间态本身。trace 文件的保留、脱敏档位选择（7.4）与清理均为用户责任；`content="full"` 档含数据内容，风险见 7.4 明示。

图 7-1 双通道日志架构。进度显示走 stderr 但不经日志设施（7.7）。

## 7.2 事件目录

通道归属规则：**事件名前缀即通道名**（`ingest. / dedup. / quality. / annotate. / verify. / schema. / llm.`，对应 `trace.channels` 的七个可选值）；`run.*` 与 `batch.*` 生命周期事件由 M10 发出、`stage="run"`、`record_ids` 为空，**不受 channels 过滤**（`trace.enabled=true` 即写）；`error` 事件按产生它的 stage 归属通道。本目录为稳定契约：`trace_schema_version = 1`，后续版本**只增不改**（可新增事件与 payload 字段，不改既有字段语义）。

| 事件名 | 通道 / stderr 级别 | 触发点 | payload 字段 |
|---|---|---|---|
| `run.start` | 恒写 / info | M1 校验通过、首批开始前；trace 文件首行 header 事件。 | `tool_version`、`config_digest`、`project_digest`（同 6.4 run 节）、`trace_schema_version`（=1，仅此事件携带，避免逐行冗余）。 |
| `run.end` | 恒写 / info | finalize 完成后（trace 末行）。 | `counts`（与 report.json counts 同构的摘要对象）、`exit_code`。 |
| `batch.start` | 恒写 / debug | 批构造完成（PipelineItem[] 就绪）。 | `size`。 |
| `batch.end` | 恒写 / info | 批 emit 并释放中间态后。 | `active`、`dropped_dup`、`dropped_lowq`、`dropped_verify`、`failed`、`duration_ms`。 |
| `ingest.bad_line` | ingest / warn | M2 坏行跳过（3.2.5）。 | `file`、`line_no`、`reason`。 |
| `ingest.missing_pair` | ingest / warn | M2 缺对跳过（3.2.4）。 | `index`、`present`（"tree"\|"image"，实际存在的一侧）、`file`。 |
| `ingest.index_conflict` | ingest / warn（fail 策略时 error） | M2 index 冲突（3.2.4）。 | `index`、`files`（冲突文件路径列表）。 |
| `dedup.duplicate` | dedup / — | M3 判重时；`record_ids` = [被判重记录 id]。 | `kind`、`cluster_key`、`kept_id`（同 DedupInfo，4.2）、`jaccard`（near_text：实测估计值）或 `hamming`（near_image：实测距离）或 `cosine`（near_semantic：实测余弦相似度，v1.2 增）；精确重复三者皆无。 |
| `quality.judgment` | quality / — | M4 每次 pairwise 裁决经 M8 校验通过后；`record_ids` = [记录甲, 记录乙]（采样顺序）。rubric 优化的核心事件。 | `order`（{"A": id, "B": id}，随机化后的呈现顺序）、`model`、`judgments`[]{`criterion`, `winner`("A"\|"B"\|"tie"), `reason`†}；v1.2 增可选字段 `judge`（= 评审 profile 名，仅 `quality.judges` 非空时携带，3.4.3）。 |
| `quality.pointwise` | quality / — | M4 pointwise 每记录每 criterion 打分后。 | `criterion`、`score`（0–5 原始分）、`reason`。 |
| `quality.bt_fit` | quality / — | M4 每批每 criterion BT 拟合结束（批级，record_ids 为空）。 | `criterion`、`iterations`、`converged`、`comparisons`（参与拟合的比较数）。 |
| `quality.gate` | quality / — | M4 质量门判定（配置了 `quality.threshold`，或 `quality.selection = "top_ratio"` 时，3.4.3）。 | `aggregate`、`decision`（"keep"\|"drop"）、`threshold`（threshold 选择时）；v1.2 增可选字段 `selection`、`top_ratio`、`rank`（top_ratio 选择时携带，payload 只增不改）。 |
| `annotate.done` | annotate / — | M5 标注经 M8 通过后。 | `attempts`（同 Annotation.attempts，4.2）；v1.2 增可选字段 `sc` = {n, agreement_ratio}（self-consistency 启用时，3.5.2）。 |
| `verify.verdict` | verify / — | M7 每轮评审后（round 从 1 计，repair 策略下每轮一事件）。 | `verdict`、`round`、`critiques`[]{`aspect`, `opinion`}；v1.2 增可选字段 `judge`（`verify.judges` 非空时每 judge 一事件，3.7.2）。 |
| `schema.repair` | schema / — | M8 任何非 clean 路径出结果时（L1 修复命中 / L3 各轮 / 拒绝）。 | `resolved_at`（"l1"\|"l3_1"\|"l3_2"\|"rejected"，同 6.4 命名）、`violations`（JSON Pointer 路径 + 违反的 Schema 关键字摘要，不含数据值）；v1.5 增可选字段 `l1_lossy`（=true，仅当 L1 修复疑似截断内容——json-repair 对未转义内引号的截断故障启发式，命中时另发 stderr warn，payload 只增不改）。 |
| `llm.call` | llm / debug（stderr 摘要行恒有） | M9 每次调用返回后（含失败）。不含提示词与响应内容（full 档例外，7.4）。 | `profile`、`gen_ai.request.model`、`latency_ms`、`gen_ai.usage.input_tokens`、`gen_ai.usage.output_tokens`、`retries`、`status`（"ok"\|"retryable_exhausted"\|"fatal"\|"breaker_aborted"——v1.5 只增：重试退避途中熔断打开、该逻辑调用被中止时发出，retries 携带已消耗次数）；v1.2 增可选字段 `operation`（="embedding"，仅 M9 `embed()` 调用携带，缺省即对话补全；payload 只增不改）。 |
| `error` | 随产生 stage 的通道 / warn（记录级）· error（运行级） | StageError 构造时。 | `stage`、`kind`（取值见 7.6）、`message`、`retryable`——即 StageError 全字段（4.2）。 |

† `reason` 仅当 `quality.judgment_reasons` 生效时存在（5.2）。全部自由文本字段（reason / critiques / violations 文本）受 7.4 脱敏档位控制。

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
| `judgment_invalid` | 比较级 | M4；按 tie 计入 BT（3.4.3）。 |
| `schema_violation` | 记录级 | M8 L3 耗尽；记录 failed → rejects。 |
| `provider_retryable_exhausted` | 记录级 | M9 重试耗尽；记录 failed，计入熔断窗口。 |
| `provider_fatal` | 运行级 | M9 不可重试错误。400/404 等计入连续熔断计数，连续达阈值 ⇒ 退出码 4；**401/403 认证类立即熔断**（v1.5，3.9.3）。 |
| `internal_error` | 记录级 | 任何未预期异常（含 M11 终检失败）；记录 failed，堆栈入日志（debug 级）。 |

## 7.7 进度显示与结束摘要

TTY 环境显示批级进度条：当前批号/总批数、各状态计数、瞬时成本累计；非 TTY 环境每批一行摘要（即 `batch.end` 的 stderr info 行）。运行结束向 stderr 打印与 report.json `counts` 完全一致的终版摘要表。进度显示**不属于日志**：直接写 stderr 而不经 logging 模块、无级别概念；唯一交互是 `tool.log_format="jsonl"` 时禁用进度条，以保证 stderr 每行可被 `json.loads` 解析（此时 `batch.end` 行即进度）。

## 7.8 测试要求（验收级）

| 层 | 要求 |
|---|---|
| 单元 | L1 确定性修复穷举样例集（围栏/截断/单引号/尾逗号/前后缀噪声）；BT 拟合对已知解析解（两元素、全胜、tie-only）误差 < 1e-4；配对采样在固定 seed 下逐字节可复现；UI 配对表（图 3-1）全分支：冲突/缺对/跨目录/前导零。 |
| 集成 | 以 mock provider（录制响应）跑通 2.3.1 全部组合矩阵；断言 report 不变量、主输出全行过用户 Schema、rejects=refs 时文件不含任何输入内容子串。 |
| 契约 | 对真实 API 的 probe 冒烟（CI 可选）；结构化输出 L0 关闭/开启两态等价性（最终输出结构一致）。 |
| 日志（v1.1 新增） | trace 每行可被 `json.loads` 解析且恰含 ts / run_id / batch_no / stage / ev / record_ids / payload 七字段；对 7.2 事件目录逐事件断言 payload 字段齐全；首行为 run.start 且携带 trace_schema_version=1；`trace.content="refs"` 时 trace 文件不含任何输入内容子串（与 rejects=refs 同法断言）；注入写失败（不可写路径 / mock OSError）断言运行不中断、warn 恰打印一次、`report.trace.dropped_events` 计数正确。 |

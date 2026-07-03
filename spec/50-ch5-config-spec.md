# 5. 配置文件完整规格

## 5.1 config.toml（工具级静态配置）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `schema_version` | int | 必填 | 本版本固定 1。 |
| `tool.log_level` | str | "info" | debug \| info \| warn \| error；被 CLI --log-level 覆盖。 |
| `llm.<name>` | table | ≥1 个 | 每个子表定义一个 profile，<name> 为被 project.toml 引用的名字。 |
| `llm.*.provider` | str | 必填 | "openai_compatible" \| "anthropic"。 |
| `llm.*.base_url` | str | 必填 | API 根地址。 |
| `llm.*.model` | str | 必填 | 模型名，原样透传。 |
| `llm.*.api_key_env` | str | 必填 | 持有 API Key 的环境变量名（唯一的环境变量用途）。 |
| `llm.*.max_concurrency` | int | 8 | 该 profile 并发上限（信号量）。 |
| `llm.*.timeout_s` | int | 120 | 单次请求超时。 |
| `llm.*.max_retries` | int | 5 | 可重试错误的最大重试次数。 |
| `llm.*.retry_base_delay_s` | float | 1.0 | 全抖动指数退避基数（3.9.3）。 |
| `llm.*.supports_structured_output` | bool | false | true 时结构引擎启用 L0（3.8.2）。 |
| `llm.*.supports_vision` | bool | false | UI 模态所引用 profile 必须为 true（M1 校验）。 |
| `llm.*.max_output_tokens` | int | 4096 | 透传给 API。 |
| `llm.*.temperature` | float | 0.0 | profile 级默认；生成阶段建议在 project.toml 用 generate.temperature 调高。 |
| `llm.*.max_image_px` | int | 2048 | 图像长边上限，超出等比缩小（3.9.3）。 |
| `llm.*.price_per_mtok_in / _out` | float | 可选 | 每百万 token 单价；配置后报告输出成本估算。 |
| `embedding.<name>` | table | 可选 | v1.2 新增：每个子表定义一个 embedding profile，<name> 为被 project.toml `dedup.semantic_embedding` 引用的名字（5.2；3.3.3 第④级）。 |
| `embedding.*.provider` | str | "openai_compatible" | 本版唯一取值：POST `{base_url}/embeddings`（3.9.3）。 |
| `embedding.*.base_url` | str | 必填 | API 根地址。 |
| `embedding.*.model` | str | 必填 | embedding 模型名，原样透传。 |
| `embedding.*.api_key_env` | str | 必填 | 持有 API Key 的环境变量名；被 `dedup.semantic_embedding` 引用时须存在且非空（M1 校验，3.1.4）。 |
| `embedding.*.max_concurrency` | int | 8 | 该 profile 并发上限（信号量，与 llm.* 同机制，3.9.3）。 |
| `embedding.*.timeout_s` | int | 60 | 单次请求超时。 |
| `embedding.*.max_retries` | int | 5 | 可重试错误的最大重试次数（重试规则同 3.9.3）。 |
| `embedding.*.dims` | int | 可选 | 返回向量维度校验：配置后 `embed()` 逐条比对返回维度，不匹配抛 ProviderFatalError（3.9.2）。 |
| `tool.log_format` | str | "text" | "text" \| "jsonl"：stderr 运行日志行格式（7.3）；"jsonl" 时禁用进度条以保证 stderr 逐行可解析（7.7）。 |

```
# ─── config.toml 完整示例 ───
schema_version = 1

[tool]
log_level = "info"
log_format = "text"                 # "jsonl" 供日志采集系统消费（7.3）

[llm.default]                       # 多模态主力模型
provider = "openai_compatible"
base_url = "https://llm-gw.example.com/v1"
model = "qwen2.5-vl-72b-instruct"
api_key_env = "LABELKIT_KEY_DEFAULT"
max_concurrency = 8
timeout_s = 120
max_retries = 5
supports_structured_output = true
supports_vision = true
price_per_mtok_in = 0.6
price_per_mtok_out = 1.8

[llm.judge]                         # 独立评审模型（避免自增强偏差, 3.7.2）
provider = "anthropic"
base_url = "https://api.anthropic.com"
model = "claude-sonnet-5"
api_key_env = "LABELKIT_KEY_JUDGE"
max_concurrency = 4
supports_structured_output = true
supports_vision = true

[embedding.default_emb]             # v1.2：语义去重句向量 profile（被 dedup.semantic_embedding 引用，5.2）
provider = "openai_compatible"      # 本版唯一取值：POST {base_url}/embeddings（3.9.3）
base_url = "https://llm-gw.example.com/v1"
model = "bge-m3"
api_key_env = "LABELKIT_KEY_EMB"
max_concurrency = 8
timeout_s = 60
max_retries = 5
dims = 1024                         # 可选：返回向量维度校验
```

## 5.2 project.toml（工程级单次配置：运行参数 + Rubric + 输出 Schema）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `schema_version` | int | 必填 | 固定 1。 |
| `run.input` | str | process 必填* | 输入路径（* 可被 CLI --input 覆盖）；`run.mode="generate_only"` 时必须缺省，提供（含 --input）即报 CONFIG_ERROR（3.1.4）。 |
| `run.output` | str | 必填* | 主输出 .jsonl 路径（* 可被 CLI --output 覆盖）。 |
| `run.modality` | str | 必填 | "text" \| "ui"。 |
| `run.mode` | str | "process" | "process"（读取 run.input 加工既有数据）\| "generate_only"（v1.4 纯生成：无输入从零合成，3.6.2 / 3.10.3；组合与互斥约束见 2.3.1 ④、3.1.4）。 |
| `run.batch_size` | int | 256 | 批大小 = QuRating 比较池大小（3.4.3）。 |
| `run.seed` | int | 0 | PRNG 种子（配对采样/顺序随机/种子抽样）。 |
| `run.fatal_error_threshold` | int | 20 | 熔断阈值（3.10.3）。 |
| `input.text_field` | str | "text" | 文本模态取文内容的点路径（3.2.5）。 |
| `input.on_bad_line / on_missing_pair / on_index_conflict` | str | skip / skip / fail | "skip" \| "fail"（3.2.4–3.2.5）。 |
| `input.max_image_mb` | int | 20 | 单图大小上限。 |
| `input.ui_tree_max_chars` | int | 30000 | 提示词中树序列化长度上限。 |
| `dedup.enabled` | bool | true | — |
| `dedup.scope` | str | "global" | "global" \| "batch"（2.6 内存权衡）。 |
| `dedup.minhash_threshold` | float | 0.85 | Jaccard 判重阈值（工业通行 0.8–0.9 [3][6]）。 |
| `dedup.minhash_num_perm / ngram` | int | 128 / 5 | 签名精度 / 字符 shingle 宽度。 |
| `dedup.image_phash_max_distance` | int | 8 | 64-bit pHash 汉明距离阈值。 |
| `dedup.ui_dup_requires` | str | "both" | "both" \| "tree" \| "image"（3.3.3）。 |
| `dedup.bounds_quantize_px` | int | 4 | 树去重时坐标量化粒度。 |
| `dedup.semantic` | bool | false | v1.2 新增：可选第④级语义去重开关（3.3.3；SemDeDup [26]）。默认关——零 embedding 依赖，默认行为与 v1.0 一致（8.3 O1）。 |
| `dedup.semantic_embedding` | str | 必填† | † `dedup.semantic = true` 时必填：引用 config.toml `[embedding.<name>]` profile（5.1）；存在性与 api_key_env 非空由 M1 校验（3.1.4）。 |
| `dedup.semantic_threshold` | float | 0.95 | 余弦相似度判重阈值（SemDeDup 论文的高相似区间 [26]；3.3.3 第④级）。 |
| `quality.enabled` | bool | true | — |
| `quality.mode` | str | "pairwise" | "pairwise" \| "pointwise"（1.6 对齐决策）。 |
| `quality.llm` | str | "default" | profile 引用。 |
| `quality.rounds` | int | 4 | pairwise 轮数 k。 |
| `quality.criteria_per_call` | str | "all" | "all" \| "single"（3.4.3）。 |
| `quality.threshold` | float | 无 | 聚合分过滤线 [0,1]；缺省 = 不过滤只打分。 |
| `quality.selection` | str | "threshold" | "threshold" \| "top_ratio"（3.4.3 选择机制行）。"threshold" = 现行为：聚合分 < `quality.threshold` ⇒ dropped_lowq，threshold 缺省则只打分不筛；"top_ratio" = 批内按聚合分降序保留 ceil(top_ratio × 批内存活数) 条。selection="top_ratio" 时不得再设 `quality.threshold`（互斥，M1 报 `CONFIG_ERROR`）。 |
| `quality.top_ratio` | float | 无 | (0,1]；`selection="top_ratio"` 时必填，与 `threshold` 互斥（M1 校验）；selection 为默认 "threshold" 时设置本键无效——M1 打 warning 提示（v1.5）。保留条数 = ceil(top_ratio × 批内存活数)；`on_unscored="keep"` 保留的未打分记录不占名额（3.4.3）。 |
| `quality.judges` | array | [] | 评审团 profile 引用数组。空 = 单评审（用 `quality.llm`）；非空须为奇数个且每项存在于 config.toml `[llm.*]`（M1 校验），每次比较各 judge 独立裁决、per-criterion 多数票（3.4.3 多评审团行，PoLL [32]）。成本 ×\|judges\|。 |
| `quality.both_orders` | bool | false | true 时同一对正反两种呈现顺序各裁决一次（每 judge），两次一致才记 winner、不一致按 tie（3.4.3 双顺序裁决行 [20]）。成本 ×2。 |
| `quality.on_unscored` | str | "keep" | "keep" \| "drop"（3.4.3 裁决失败行）。 |
| `quality.rubric` | str | 自动 | "default:text" \| "default:ui" \| "inline"。缺省按模态选默认；写 inline 时必须提供 [[rubric.criteria]]。 |
| `quality.judgment_reasons` | str/bool | "auto" | "auto" \| true \| false。生效时 pairwise 裁决 Schema 增加 `reason` 字段（3.4.3），写入 trace 供 rubric 优化（7.5）；"auto" = `trace.enabled=true` 且 `trace.channels` 含 "quality" 时开（trace 关闭则不请求 reason，零额外 token）。成本：每次裁决约增加 30–60 输出 token。 |
| `rubric.criteria` | array | 可选 | 内联 rubric，字段见 5.3。 |
| `generate.enabled` | bool | false | 仅文本模态（2.3.1 约束）。 |
| `generate.llms / instruction` | array/str | ["default"] / 必填† | † enabled 时必填。`llms` 为 profile 引用数组（v1.2，取代 v1.1 单值键 `generate.llm`），每个元素须存在于 config.toml `[llm.*]`；每次生成调用按 `generate.mixture` 从中选 1 个（3.6.2 多模型混合行）。 |
| `generate.mixture` | str | "round_robin" | "round_robin"（按调用序轮转）\| "weighted"（按 `generate.weights` 加权抽样，PRNG 用 `ctx.rng`，随 run.seed 可复现）。llms 仅 1 个元素时二者等价（即 v1.1 行为）。 |
| `generate.weights` | array | [] | 正数权重；`mixture = "weighted"` 时必填且长度须等于 llms（M1 校验），round_robin 下忽略。 |
| `[[generate.styles]]` | array | [] | 风格子表（可选）：每项含 `name`（str，表内唯一）与 `prompt`（str，非空）；非空时每次生成调用经 `ctx.rng` 均匀抽 1 个 style，其 prompt 追加进生成指令（3.6.2 风格条件化行）。 |
| `generate.num_per_record` | int | 2 | 每种子期望产出条数。 |
| `generate.seeds_per_call / num_per_call` | int | 3 / 4 | 3.6.2。 |
| `generate.seed_min_score` | float | 自动 | 种子门槛，默认取 quality.threshold 或批中位数。 |
| `generate.temperature` | float | 0.9 | 生成需要多样性，覆盖 profile 默认。 |
| `generate.sample_validator` | str | 无 | v1.5 校验回调（方案 A）：`"module:function"`，签名 `fn(text: str) -> list[str]`（空 = 通过）。对每条生成样本在相似度过滤**之前**执行，违规样本剔除（过滤语义，不触发重试、不产生 failed 记录），计入桶统计 `rejected_by_validator`（3.6.2/6.4）。M1 校验同 output.validator（无 few-shot 干跑）。回调抛异常 ⇒ 该样本按违规剔除并 stderr warn（过滤器不失败）。 |
| `generate.seed_examples` | array | [] | generate_only 专用（process 模式不得设置，3.1.4）：字符串数组种子池，非空即种子池形态（3.6.2）。 |
| `generate.standalone_count` | int | 无 | generate_only 无种子形态必填（与 seed_examples 互斥）：目标产出条数，调用数 = ⌈standalone_count / num_per_call⌉。 |
| `annotate.enabled` | bool | true | — |
| `annotate.llm / instruction` | str | default / 必填† | † enabled 时必填。 |
| `annotate.examples` | array | [] | few-shot：[{input, output}]，output 须过用户 Schema（M1 校验）。 |
| `annotate.self_consistency` | int | 0 | 0 = 关（单次标注，v1.1 行为）；启用须 ≥3 且为奇数（M1 校验）：每条记录独立采样 n 次后字段级投票（3.5.2 note 框）。成本：标注调用与 token ×n。 |
| `annotate.sc_temperature` | float | 0.7 | self-consistency 各次采样的 temperature（采样多样性来源 [33]），覆盖 profile 默认；仅 `self_consistency ≥ 3` 时生效。 |
| `verify.enabled` | bool | false | — |
| `verify.llm` | str | "judge"† | † `verify.enabled = true` 且 `verify.judges` 为空时该 profile 须存在于 config.toml `[llm.*]`（judges 非空时被评审团替代、不参与运行也不要求存在，v1.5）；建议独立于 annotate.llm（3.7.2）。 |
| `verify.judges` | array | [] | 多评审团 profile 列表（v1.2，3.7.2；与 quality.judges 语义一致）：空 = 单评审用 verify.llm；非空须为奇数个（M1 校验），verdict 取多数票，critiques 合并并标注来源 judge，成本 ×\|judges\|。背书 PoLL [32]。 |
| `verify.policy / max_repair_rounds` | str/int | "drop" / 1 | 3.7.3。 |
| `verify.extra_criteria` | str | "" | 追加评审维度的自由文本。 |
| `output.schema_path` | str | 二选一 | 外部 .json 的用户 Schema；与 schema_inline 恰一。 |
| `output.schema_inline` | str | 二选一 | TOML 多行字符串内嵌的 Schema JSON 文本。 |
| `output.max_repair_attempts` | int | 2 | 结构引擎 L3 次数（3.8.2）。 |
| `output.repair_llm` | str | 同调用方 | L3 修复用 profile。 |
| `output.validator` | str | 无 | v1.5 校验回调（方案 A）：`"module:function"` 形式的 Python 可调用引用，签名 `fn(obj: dict, record: dict | None) -> list[str]`（返回违规描述列表，空 = 通过；record = Record.raw：文本/生成记录为该行原始对象，UI 记录为 None）。挂接为结构引擎 **L2.5**（3.8.2）：仅作用于用户 Schema 的标注调用，违规并入 L3 修复环、共享 max_repair_attempts 预算，耗尽 ⇒ 记录 failed（kind = `callback_violation`，7.6）。M1 启动校验：格式、可导入、可调用，且逐条 few-shot 示例 output 须过回调（干跑）。回调以运行者同权限执行任意用户代码（信任边界与配置文件一致）；回调内抛异常按记录级 `internal_error` 处理。 |
| `output.meta_mode` | str | "inline" | "inline" \| "sidecar" \| "none"（6.3）。 |
| `output.passthrough_fields` | array | [] | 从 Record.raw 透传进 _meta.source.fields 的字段名列表。 |
| `output.rejects` | str | "refs" | "none" \| "refs" \| "full"（3.11.2）。 |
| `trace.enabled` | bool | false | 启用 trace 追踪日志（第 7 章）。 |
| `trace.path` | str | 自动 | 默认 `{output_stem}.trace.jsonl`，与主输出同目录。 |
| `trace.channels` | array | ["quality","verify","schema"] | 可选值 ingest \| dedup \| quality \| annotate \| verify \| schema \| llm（7.2 事件目录）；run.*/batch.* 生命周期事件不受此过滤。 |
| `trace.content` | str | "refs" | "none" \| "refs" \| "excerpt" \| "full" 内容脱敏四档（7.4）。 |

```
# ─── project.toml 完整示例（UI 模态标注工程）───
schema_version = 1

[run]
input = "./capture/2026-07-01"
output = "./out/ui-labels-0701.jsonl"
modality = "ui"
batch_size = 128
seed = 42

[dedup]
ui_dup_requires = "both"

[quality]
mode = "pairwise"
rounds = 4
threshold = 0.3
rubric = "default:ui"

[annotate]
llm = "default"
instruction = """
你是移动端 UI 理解标注员。根据屏幕截图与 UI 控件树，
标注该屏幕的功能类别、页面标题、可交互元素列表与一句话页面描述。
"""

[verify]
enabled = true
llm = "judge"
policy = "repair"
max_repair_rounds = 1

[trace]                             # 追踪日志（第 7 章）：调优期开启，用于 rubric 诊断（7.5）
enabled = true
channels = ["quality", "verify"]

[output]
meta_mode = "inline"
schema_inline = """
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "screen_category": {"type": "string",
      "enum": ["login", "home", "list", "detail", "form", "settings", "dialog", "other"]},
    "page_title": {"type": "string"},
    "interactive_elements": {"type": "array", "items": {
      "type": "object",
      "properties": {"role": {"type": "string"}, "label": {"type": "string"},
                     "bounds": {"type": "array", "items": {"type": "integer"},
                                "minItems": 4, "maxItems": 4}},
      "required": ["role", "label", "bounds"], "additionalProperties": false}},
    "description": {"type": "string", "maxLength": 200}
  },
  "required": ["screen_category", "page_title", "interactive_elements", "description"],
  "additionalProperties": false
}
"""
```

## 5.3 Rubric 结构（内联或默认包文件，同一 TOML 结构）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `rubric.name` | str | 必填 | rubric 标识，入 _meta 与报告。 |
| `rubric.criteria[].key` | str | 必填 | `[a-z0-9_]+`，全局唯一。 |
| `rubric.criteria[].weight` | float | 1.0 | 聚合权重（>0）。 |
| `rubric.criteria[].description` | str | 必填 | 准则含义（进入两种模式的提示词）。 |
| `rubric.criteria[].pairwise_prompt` | str | 必填 | 成对比较问句，如「哪段文本的写作水平更高？」。 |
| `rubric.criteria[].pointwise_levels` | array[6] | pointwise 必填 | 0–5 六级加性描述（附录 A 示例）。 |

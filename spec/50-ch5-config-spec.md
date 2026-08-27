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
| `llm.*.api_key_env` | str | 必填* | 持有 API Key 的环境变量名（API Key 是唯一的环境变量用途，2.5）。* v1.6：与 `api_key_envs` 恰提供其一（互斥，M1 校验 3.1.4）。 |
| `llm.*.api_key_envs` | array | 无 | v1.6 密钥池（3.9.3）：持有 API Key 的环境变量名数组（≥1 项，逐项非空且互异），与 `api_key_env` 互斥。池内密钥共享本 profile 其余全部字段（同 base_url、同 model——同构池，密钥选择不改变产出数据内容）；被引用 profile 的**每个**列出变量都须存在且非空（M1 校验）。单元素数组与 `api_key_env` 等价；`max_concurrency` 仍为池内总在途上限。 |
| `llm.*.max_concurrency` | int | 8 | 该 profile 的唯一容量真值：同时限制 TaskExecutor 对该 `("llm", name)` 资源通道的已接纳叶任务数，以及 ResourceManager 的实际逻辑调用数。许可覆盖 retry/backoff/cooldown/parking；不按 task group 重建。 |
| `llm.*.timeout_s` | int | 120 | 单次请求超时。 |
| `llm.*.max_retries` | int | 5 | 可重试错误的最大重试次数。 |
| `llm.*.retry_base_delay_s` | float | 1.0 | 全抖动指数退避基数（3.9.3）。 |
| `llm.*.supports_structured_output` | bool | false | true 时结构引擎启用 L0（3.8.2）。 |
| `llm.*.supports_vision` | bool | false | UI 模态所引用 profile 必须为 true（M1 校验）。 |
| `llm.*.max_output_tokens` | int | 4096 | 透传给 API。 |
| `llm.*.context_window` | int | 0 | v1.11 新增（V6/V26，3.9.5）：模型上下文窗口（token）。`0` = 未声明：该 profile 上下文预算关闭（行为与 v1.10 一致），被启用阶段引用时 M1 WARN 一次（3.1.4）。> 0 时须满足 `context_window > max_output_tokens + margin`，否则 CONFIG_ERROR（预算非正）；`margin = max(256, ceil(0.10 × context_window))`。**声明部署实效窗口，勿照抄文档（V26/[C-59]，`docs/dev/PROPOSAL-context-budget.md`）**：同名模型随部署差数倍（Together 版 glm-5.2 为 256K、vLLM 由 `--max-model-len` 决定），且文档说法可能与端点实况相悖（z.ai anthropic 路由实测：裸 `glm-5.2` 实效窗即 `input+max_tokens ≤ 2^20`，官方博客的 `[1m]` 后缀反被拒——E2E-FINDINGS #16）——窗口只能按部署实测或保守欠声明；**欠声明恒安全**（只多裁不溢出）。 |
| `llm.*.temperature` | float | 0.0 | profile 级默认；生成阶段建议在 project.toml 用 generate.temperature 调高。 |
| `llm.*.thinking` | str | 缺省 | v1.16：可选 `"enabled"` \| `"disabled"`；显式值在 OpenAI 兼容与 Anthropic 两种请求的顶层写为 `{"thinking": {"type": <value>}}`，缺省不写该字段以保持既有请求体形态。 |
| `llm.*.max_image_px` | int | 2048 | 图像长边上限，超出等比缩小（3.9.3）。v1.11 语义升格（V18/V27③，3.9.5）：**升级天花板 + provider 像素制硬限制域**——V21 判审升级路径的分辨率上探以本键封顶；像素是运载意图与 provider 硬限制（带宽/载荷；Anthropic 的 8000px 与 >20 图 ∧ >2000px 硬拒本身是像素制）的控制面。[C-62] 记载：gpt-5.6 级 openai 后端默认 `detail` 等效 `original`（服务端不再隐式钳制图片 token），本键与 `default_image_px` 因此成为该类后端**唯一的客户端成本闸**。 |
| `llm.*.default_image_px` | int | 0 | v1.11 新增（V18，3.9.5）：图片采样**默认工作点**（长边 px）。`0` = 沿用 `max_image_px`（v1.10 行为逐字节不变）。> 0 时须 ≤ `max_image_px`（CONFIG_ERROR，3.1.4）；V21 升级路径可上探至 `max_image_px`。 |
| `llm.*.price_per_mtok_in / _out` | float | 可选 | 每百万 token 单价；配置后报告输出成本估算。 |
| `embedding.<name>` | table | 可选 | v1.2 新增：每个子表定义一个 embedding profile，<name> 为被 project.toml `dedup.semantic_embedding` 引用的名字（5.2；3.3.3 第④级）。 |
| `embedding.*.provider` | str | "openai_compatible" | 本版唯一取值：POST `{base_url}/embeddings`（3.9.3）。 |
| `embedding.*.base_url` | str | 必填 | API 根地址。 |
| `embedding.*.model` | str | 必填 | embedding 模型名，原样透传。 |
| `embedding.*.api_key_env` | str | 必填* | 持有 API Key 的环境变量名；被 `dedup.semantic_embedding` 引用时须存在且非空（M1 校验，3.1.4）。* v1.6：与 `embedding.*.api_key_envs` 恰提供其一。 |
| `embedding.*.api_key_envs` | array | 无 | v1.6：同 `llm.*.api_key_envs`——embedding profile 的密钥池，机制一致（3.9.3 密钥池行）。 |
| `embedding.*.max_concurrency` | int | 8 | 该 embedding profile 的唯一容量真值：同时限制对应资源通道的已接纳叶任务数与实际逻辑调用数；与同名 LLM profile 是不同 ResourceKey。 |
| `embedding.*.timeout_s` | int | 60 | 单次请求超时。 |
| `embedding.*.max_retries` | int | 5 | 可重试错误的最大重试次数（重试规则同 3.9.3）。 |
| `embedding.*.retry_base_delay_s` | float | 1.0 | 全抖动指数退避基数（与 `llm.*` 同名键同机制，3.9.3）。 |
| `embedding.*.dims` | int | 可选 | 返回向量维度校验：配置后 `embed()` 逐条比对返回维度，不匹配抛 ProviderFatalError（3.9.2）。 |
| `embedding.*.context_window` | int | 0 | v1.11 新增（V15，同 `llm.*.context_window` 声明制，3.9.5）：`0` = 未声明 = 该 embedding profile 预算关闭。> 0 时预算 = `context_window − margin`（**无输出预留**）；embed 输入超预算按确定性头部保留截断（3.3.3 第④级语义嵌入）。声明实效窗口指引同 llm 行（V26）。 |
| `tool.log_format` | str | "text" | "text" \| "jsonl"：stderr 运行日志行格式（7.3）；"jsonl" 时强制 console plain 档以保证 stderr 逐行可解析（7.7，显式 rich（CLI `--console rich` 或 `console.mode="rich"`）冲突时 M1 WARN）。 |
| `console.mode` | str | "auto" | v1.10（7.7）："auto" \| "rich" \| "plain"——进度显示面三态；被 CLI `--console` 覆盖。auto 判定链：stderr TTY ∧ log_format="text" ∧ TERM 非 dumb/空 ∧ rich 可导入（M1 以 find_spec 探测），全真取 rich，否则 plain（TERM 定性为终端能力探测，与 isatty 同级，非配置通道；NO_COLOR 不参与判定——rich 原生剥色保布局，U25）。判定产物由 M1 冻结为解析字段 `mode_resolved`（3.1.4）。plain 档 stderr 与 v1.9 行为等价（`heartbeat_s=0` 时——三层回归锚 7.8）。 |
| `console.refresh_hz` | int | 5 | v1.10：rich 画布重绘频率（asyncio 节流 tick，7.7），1–10，越界 = CONFIG_ERROR。 |
| `console.heartbeat_s` | int | 0 | v1.10：仅 plain 且非 TTY 生效——每 N 秒一行数据无关汇总心跳（固定键集 `heartbeat batch= stage= llm_calls= elapsed=`，7.7）；0 = 关（默认，保回归锚；对齐决策 1.6 U14）；< 0 = CONFIG_ERROR。 |
| `console.estimate` | bool | false | v1.10：仅文本模态生效——启动时做估算扫描（`Ingestor.scan(estimate=True)`，全量多读一遍输入）换取批总数分母与 ETA（7.7；对齐决策 1.6 U17）；UI 模态分母天然廉价（live 预扫复用）、恒显示，本键无效。 |
| `console.interactive` | bool | true | v1.10：rich ∧ stdin TTY ∧ termios 可用时启用键盘开关（封闭键集 `? l e + - p q`（`h` 为 `?` 同义键），7.7）；false = 纯渲染（stdin 完全不被占用——buck2 `--no-interactive-console` 对应物）。 |

v1.19 不新增 `[runtime]`、workers、queue_size、thread_count 或 HTTP pool 配置。Application 从本轮实际引用的
LLM/embedding profiles 派生 ResourceKey 与规范化 HTTP origin；同 origin 容量等于其引用 profile 容量之和，
共享 HTTPX 连接池容量等于全部 origin 容量之和。repair、probe 与生成路径引用的 profile 都计入，重复引用只计
一次。静态 validate 与 estimate 不构造 ExecutionRuntime；dry-run 只可构造不进入 execution domain、且不创建
leaf 的惰性 ExecutionRuntime 身份载体。三者都不构造 HTTP client。

```
# ─── config.toml 完整示例 ───
schema_version = 1

[tool]
log_level = "info"
log_format = "text"                 # "jsonl" 供日志采集系统消费（7.3）

[console]                           # v1.10 进度显示面（7.7）；整节可缺省
mode = "auto"                       # auto | rich | plain
refresh_hz = 5                      # rich 画布重绘频率（1–10）
heartbeat_s = 0                     # plain 非 TTY 心跳；0 = 关（默认）
estimate = false                    # 文本模态批总数分母（多读一遍输入）
interactive = true                  # rich 档键盘开关（? l e + - p q；h=?）

[llm.default]                       # 多模态主力模型
provider = "openai_compatible"
base_url = "https://llm-gw.example.com/v1"
model = "qwen2.5-vl-72b-instruct"
api_key_env = "LABELKIT_KEY_DEFAULT"
# api_key_envs = ["LABELKIT_KEY_DEFAULT", "LABELKIT_KEY_DEFAULT_2"]   # v1.6 密钥池：与上行互斥（3.9.3）
max_concurrency = 8
timeout_s = 120
max_retries = 5
supports_structured_output = true
supports_vision = true
context_window = 131072             # v1.11 上下文预算（0/缺省 = 关；声明部署实效窗口而非厂商表值，3.9.5）
# default_image_px = 1092           # v1.11 图片采样工作点（0/缺省 = 沿用 max_image_px；须 ≤ max_image_px）
price_per_mtok_in = 0.6
price_per_mtok_out = 1.8

[llm.judge]                         # 独立评审模型（避免自增强偏差, 3.7.2）
provider = "anthropic"
base_url = "https://api.anthropic.com"
model = "claude-sonnet-5"
api_key_env = "LABELKIT_KEY_JUDGE"
# thinking = "disabled"             # v1.16：provider 顶层 thinking 开关；缺省不发送
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
| `run.batch_size` | int | 256 | 批大小 = QuRating 比较池大小（3.4.3）。v1.11 语义句（V14）：决定内存生命周期、QuRating 对比池基数与 stream 装箱容量；**从不影响单次 prompt 体积**——单次调用容量由各算子条数上限与上下文预算（3.9.5）共同决定。 |
| `run.seed` | int | 0 | PRNG 种子（配对采样/顺序随机/种子抽样）。 |
| `run.fatal_error_threshold` | int | 20 | 熔断阈值（3.10.3）。 |
| `run.max_park_s` | int | 3600 | v1.6 驻留上限（3.9.3 密钥池行）：单次逻辑 LLM 调用因「所引 profile 全部存活密钥均在冷却」而驻留等待的累计秒数上限；超限按重试耗尽处理（记录 failed、计入熔断窗口，1.6 对齐决策 ③）。0 = 不驻留（全池冷却即按重试耗尽失败）——注意：0 与单密钥 profile 组合意味着**任何 429（含短 Retry-After）都立即按重试耗尽失败**，仅建议在多密钥池上设 0。运维容忍度参数，不影响产出内容；单密钥配置下亦约束超长 `Retry-After` 等待（3.9.3 重试行）。 |
| `input.text_field` | str | "text" | 文本模态取文内容的点路径（3.2.5）。 |
| `input.on_bad_line / on_missing_pair / on_index_conflict` | str | skip / skip / fail | "skip" \| "fail"（3.2.4–3.2.5）。 |
| `input.max_image_mb` | int | 20 | 单图大小上限。 |
| `input.ui_tree_max_chars` | int | 30000 | 提示词中树序列化长度上限。v1.11（V9，3.9.5）：升格为**绝对上限**——所引 profile 声明 `context_window` 后，单记录树渲染实参取 `min(ui_tree_max_chars, 预算折算份额)` 动态收缩（超预算按行丢尾、保留既有 truncated marker）；未声明预算时即固定上限（现行为）。 |
| `stream.order_by` | str | "input_order" | `[stream]` 仅描述 process 输入侧排序与会话化；"input_order" 为文本文件/行号或 UI pair_index 顺序，`"meta:<field>"` 仅文本模态并按第 6 章解析时间。sequence 生成不读取本节。 |
| `stream.on_disorder` | str | "skip" | v1.8："skip"（默认：乱序/时间戳解析失败记录跳过——计 bad_input + IngestReport.disorder 子计数 + `ingest.disorder` 事件 + WARN 一次）\| "fail"（InputError，退出码 3）。单调性游标**按分区键各自维护**（S19；键变即断语义保留，输入须按键成组，6.1）。 |
| `stream.key` | array | [] | v1.8：分区键列表，键变即断会话（groupby 语义非 keyBy）。元素 = "meta:<field>"（仅文本模态）\| "source_dir"（= ref.source_file 父目录派生，UI 模态可用——一次采集一目录惯例，S19）；元素合法性 M1 校验（3.1.4）。 |
| `stream.gap_s` | int | 300 | v1.8：相邻记录时间差 > gap_s 秒即断开会话；仅 `order_by="meta:*"` 时生效——显式设置而非 meta 序 ⇒ M1 warning 一次（非阻断，键不生效；对照 `session_max_span_s` 行的 CONFIG_ERROR 级）。默认偏大的结构性论证：欠分割可由 LLM 边界精化拯救、过分割不可逆（3.14）。 |
| `stream.gap_steps` | int | 0 | v1.8：相邻记录序号差 > gap_steps 即断开（0 = 不启用）；与 gap_s 可并用，任一触发即断。 |
| `stream.session_max_len` | int | 200 | v1.8：会话硬上限（帧），到限即断。`session_max_len > run.batch_size` ⇒ M1 静态 WARN（S21：单会话超批容量将被 M10 硬切 + `session_split` 标，3.10.3）。 |
| `stream.session_max_span_s` | int | 0 | v1.8：会话时间跨度硬上限（秒，0 = 不启用）；**仅 `order_by="meta:*"` 时可设**（M1 校验，违反报 CONFIG_ERROR）。 |
| `segment.enabled` | bool | false | v1.8 新增：语义分段算子 / stream 模式总开关（M14，3.14）。默认关——工具行为与 v1.7 逐字节一致（`_meta.stream: null` 除外，6.3）。启用要求（3.1.4）：`run.mode = "process"` ∧ `generate.enabled = false`（generate_only 经 2.3.1 ④ 传递闭合）∧ `annotate.enabled = true`。no-op warning（R8 家族）：`[stream]`/`[segment]`/`[extract]` 任一节在场而 `segment.enabled = false`。 |
| `segment.strategy` | str | "hybrid" | "rules"（候选会话原样成 episode，零 LLM；noise_filter / min_len 不生效）\| "llm" \| "hybrid"（默认：滑窗 LLM 边界精化 + 逐帧噪声标记；len(session)==1 走 rules 退化，3.14）。 |
| `segment.llm` | str | "default" | profile 引用；**仅 `strategy ∈ {llm, hybrid}` 时**计入密钥解析 / `--probe` / 存在性引用集（S30，3.1.4）——rules 策略零调用不强制配键。v1.11（V1/V3）：**不再入 vision 校验集**——segment 从「要求视觉」改为「适配视觉」，窗口是否附图由本 profile 的 `supports_vision` 能力自动决定（parse product `vision_resolved`，见下）；选 profile 即选能力，需纯文本裁决请指向纯文本 profile。 |
| `segment.window` | int | 20 | 滑窗帧数/调用上限；M1 校验 **≥ 2**。v1.11 语义修订（V9，3.9.5）：**单窗帧数上限**——所引 profile 声明 `context_window` 后按预算贪心装填（溢出即封窗，实际每窗帧数 ≤ window），未声明时为固定窗大小（v1.10 行为逐字节一致）。步长 = 重叠 1 帧（接缝帧整帧判决归后窗）；window ≥ 会话长且预算装得下时天然退化为整段单调用（S32，3.14.7）。 |
| `segment.digest_max_chars` | int | 400 | 单帧摘要（frame_digest，4.3）长度上限。 |
| `segment.noise_filter` | bool | true | 逐帧噪声标记（interruption → dropped_noise，reason="noise"）；仅 llm/hybrid 生效——`strategy = "rules"` ∧ noise_filter = true ⇒ no-op warning（3.1.4）。 |
| `segment.min_len` | int | 2 | 段最短帧数；**仅作用于 LLM 边界精化切出的段**（S11）——规则层孤帧/短会话（含 strategy="rules"）原样成 episode、不受本键约束；被丢弃帧 reason = "below_min_len"（≠ "noise"），独立计数 `report.stream.below_min_len`（6.4）。 |
| `segment.context` | str | "" | 可选域上下文，注入判据模板；**非边界定义**——边界判据内置于模板（3.14），零配置可用。 |
| `segment.on_error` | str | "keep" | 单窗结构修复耗尽的处置："keep"（默认：该会话整体成一个 episode 存活 + 留痕三件套 `_meta.stream.degraded = {kind:"segmentation_invalid", windows_failed}` / error 事件 / `segment.failures` 计数，**不写 item.errors**——S26 归因防污染）\| "fail"（会话成员全部 failed → rejects，kind = segmentation_invalid，7.6）。 |
| ~~`segment.use_vision`~~ | — | —（v1.11 移除） | v1.11 移除键（V1/V2）：显式出现 → CONFIG_ERROR：`[segment].use_vision: segment.use_vision was removed in v1.11: whether a window carries images is derived automatically from supports_vision of the profile named by segment.llm; point segment.llm at a text-only profile for text-only judgments (V2)`（3.1.4）——**不走**「未知键忽略」前向兼容警告。 |
| `segment.vision_resolved` | bool | parse product | v1.11（V1）：**非用户键**——M1 于 load() 收尾冻结的解析产物（`mode_resolved` 同款，3.1.4）：`vision_resolved = (modality=="ui") ∧ segment.enabled ∧ strategy∈{llm,hybrid} ∧ llm_profiles[segment.llm].supports_vision`；运行期窗口是否附图的唯一判据（3.14.4 模板）。 |
| `stitch.enabled` | bool | false | v1.9 新增：线索缝合算子开关（M16，3.16；链序 segment 之后、dedup 之前，3.10.3）。默认关——主输出 / rejects / report.json 与 v1.8 **逐字节等价**（例外两处：dry-run stderr 的 `stitch_calls=0` 行、stream×verify 缺陷词表 `wrong_stitch: 0` 行——3.16.4 退化锚）。启用要求 `segment.enabled = true`（M1 约束，3.1.4——stream 前置约束经此传递闭合）。no-op warning：`[stitch]` 在场而 `segment.enabled = false` 入 R8 点名名单；`segment.enabled = true` ∧ 本键 false 而节内有 payload ⇒ 单独 warning（3.1.4 ⑦）。 |
| `stitch.llm` | str | "default" | 判定 profile 引用；仅启用时计入密钥解析 / `--probe` / 存在性引用集，**不入 vision 校验集**（判定证据为纯文本摘要卡，无视觉必需，3.1.4 / 3.16.3）。 |
| `stitch.max_open` | int | 4 | 开放线索池容量（挂起窗口均值 3 + 1 活跃 [81]；移动域佐证 [90]）；池满且需开新线索时按逐出优先级封闭一条（stale-gap 优先 → LRU 兜底；封闭 ≠ 终结，3.16.4）。M1 校验 ≥ 1。 |
| `stitch.bias` | str | "conservative" | "conservative"（默认：并入需 LLM 判 resume ∧ 机械先验合取命中——App 交集 / 实体重叠 / 返回同一页面析取三腿，3.16.4）\| "llm"（纯 LLM 判，审计/消融用）。 |
| `stitch.rescue_short` | bool | true | below_min_len 短段按连续 run 重组先进候选池救援（3.16.4 救援行；命中翻转计 `rescued_short`、未命中维持 dropped_noise、永不开新线索）；false = 短段维持 dropped_noise（v1.8 行为）。 |
| `stitch.repass` | bool | true | 有界二遍复评（3.16.4 ②：一遍结束后对单碎片线索逐个复评，修正顺序贪心漏缝；预算 ≤ 单碎片线索数）；false = 纯一遍贪心。 |
| `stitch.stale_gap_steps` | int | 0 | 时间衰减阈值（会话序号差；0 = 不启用）。**双职**：① 先验降格——候选与线索尾跨度超限时先验须两腿命中（3.16.4 保守偏置行）；② 池满逐出优先腿（3.16.4 ①）。与 `stream.gap_steps` 语义区分：后者是 M2 会话切分规则，本键是会话内线索挂起跨度。 |
| `stitch.digest_max_chars` | int | 400 | 摘要卡内嵌入的每个帧摘要截断上限（沿用 segment 同名键语义，3.16.3）。 |
| `stitch.context` | str | "" | 可选域上下文（何为「同一任务」的领域提示），注入判定模板可选行；**非判据定义**——保守偏置内置于固定模板（3.16.4），零配置可用。 |
| `stitch.votes` | int | 1 | 判定稳定化采样数：1（默认）= 不启用（单调用）；> 1 须为 ≥3 的奇数（**偶数 = CONFIG_ERROR**，M1 校验，3.1.4）——同判定 n 次采样、对 **(verdict, thread_ref) 完整判定**严格多数决（> n/2；任何分裂回落保守结局，3.16.4 votes 行）。成本 = 判定调用 ×n。 |
| `stitch.on_error` | str | "keep" | 单判定结构修复耗尽的处置："keep"（默认：episode 候选开新线索存活 + 留痕两件（事件+计数器）；救援候选维持 dropped_noise + 同款留痕）\| "fail"（**仅施于 episode 候选信封**——failed → rejects，kind = stitch_invalid，7.6；救援候选不适用 fail 路径，3.16.6）。 |
| `dedup.enabled` | bool | true | — |
| `dedup.scope` | str | "global" | "global" \| "batch"（2.6 内存权衡）。 |
| `dedup.minhash_threshold` | float | 0.85 | Jaccard 判重阈值，基础范围 (0,1]；它与 `minhash_num_perm` 必须能共同产生有效 LSH 分带，M1 在 `validate` 阶段校验，失败定位到本键（工业通行 0.8–0.9 [3][6]）。 |
| `dedup.minhash_num_perm / ngram` | int | 128 / 5 | 签名精度 / 字符 shingle 宽度。 |
| `dedup.image_phash_max_distance` | int | 8 | 64-bit pHash 汉明距离阈值。 |
| `dedup.ui_dup_requires` | str | "both" | "both" \| "tree" \| "image"（3.3.3）。 |
| `dedup.bounds_quantize_px` | int | 4 | 树去重时坐标量化粒度。 |
| `dedup.semantic` | bool | false | v1.2 新增：可选第④级语义去重开关（3.3.3；SemDeDup [26]）。默认关——零 embedding 依赖，默认行为与 v1.0 一致（8.3 O1）。 |
| `dedup.semantic_embedding` | str | 必填† | † `dedup.semantic = true` 时必填：引用 config.toml `[embedding.<name>]` profile（5.1）；存在性与密钥配置（`api_key_env` / `api_key_envs` 恰其一且逐项非空，v1.6）由 M1 校验（3.1.4）。 |
| `dedup.semantic_threshold` | float | 0.95 | 余弦相似度判重阈值（SemDeDup 论文的高相似区间 [26]；3.3.3 第④级）。 |
| `classify.enabled` | bool | false | v1.7 新增：分类算子开关（3.13）。默认关——工具行为与 v1.6 完全一致（`_meta.classification: null` 除外，6.3）。 |
| `classify.llm` | str | "default" | profile 引用；UI 模态须 supports_vision（M1 校验）；计入密钥解析 / vision 校验 / `--probe` 三处 profile 引用集（3.1.4 分类行）。 |
| `classify.assignment` | str | "single" | "single"（锁定一条一类）\| "multi"（允许多类命中并按标签扇出，3.13.4）。 |
| `classify.max_labels` | int | 类别数 | 仅 multi 可设；∈ [2, 类别数]；缺省由 M1 解析后回填为类别数（扇出成本上界旋钮）。 |
| `classify.instruction` | str | "" | 可选补充说明，追加进 system 类别表之后（3.13.3 模板）。 |
| `classify.fallback_class` | str | 必填† | † enabled 时必填且 ∈ classes（3.13.4 失败与兜底行；LLM 亦可主动选择它）。 |
| `classify.self_consistency` | int | 0 | 0 或 ≥3 的奇数（M1 校验）；sc 投票语义见 3.13.4（single 多数票 / multi 逐标签投票，无过半归兜底类）。 |
| `classify.sc_temperature` | float | 0.7 | sc 各次采样的 temperature，仅 `self_consistency ≥ 3` 生效（与 `annotate.sc_temperature` 同机制）。 |
| `classify.on_error` | str | "fallback" | "fallback"（结构修复耗尽归兜底类，记录存活）\| "fail"（记录 failed → rejects）（3.13.4）。 |
| `[[classify.classes]]` | array | 必填† | † enabled 时 ≥ 2 项。每项：`name`（`[a-z0-9_]+`，表内唯一）、`description`（非空）、`examples`（字符串数组，可选，仅输入侧，3.13.3）。 |
| `extract.enabled` | bool | false | v1.8 新增：转移/动作摘取算子开关（M15，3.15；链序位于 classify 之后、quality 之前，3.10.3）。启用要求 `segment.enabled = true` ∧ `run.modality = "ui"`（M1 校验，3.1.4；文本序列 v1 不适用）。 |
| `extract.llm` | str | "default" | profile 引用；**恒**计入密钥解析 / vision / `--probe` / 存在性四处引用集且恒入 vision 校验集（每转移一请求 2 图，S30，3.15）。 |
| `extract.instruction` | str | "" | 可选摘取补充说明，追加进 system 摘取指令之后（3.15 模板）；`[class.<name>.extract]` 可按类覆盖（白名单**仅此键**，见按类覆盖表）。 |
| `extract.include_diff` | bool | true | `[树变更摘要]` 注入开关（S14）：true（默认）时向摘取提示词注入 tree_diff（4.3）输出的文字化——结构化树 diff 证据（≠ 像素 diff，工程实践正面）；false 关闭注入，供 A/B 消融对比摘取质量（`report.stream.extract.by_type` 可观测，6.4）。 |
| `extract.on_error` | str | "fallback" | 单转移结构修复耗尽的处置："fallback"（默认，S16：该步记 `action_type="other"` + `Transition.detail = {kind:"extraction_invalid", message}` 留痕，**不写 item.errors**；quality 副读数注入时 fallback 步与 LLM 确证的 other **分列**——防污染连贯性锚点）\| "fail"（episode failed → rejects，kind = extraction_invalid，7.6）。 |
| `quality.enabled` | bool | true | — |
| `quality.mode` | str | "pairwise" | "pairwise" \| "pointwise"（1.6 对齐决策）。 |
| `quality.llm` | str | "default" | profile 引用。v1.8 只增注：stream 模式下序列打分为纯文本（`[步骤序列]` + 帧摘要，无图，3.4.3 序列行）——UI 模态亦**不**因 stream 要求本 profile supports_vision（vision 逐阶段表的放宽项，S30，3.1.4；v1.9 起 `stitch.llm` 同为纯文本恒不要求，「唯一放宽」不再成立）。 |
| `quality.rounds` | int | 4 | pairwise 轮数 k。 |
| `quality.criteria_per_call` | str | "all" | "all" \| "single"（3.4.3）。 |
| `quality.threshold` | float | 无 | 聚合分过滤线 [0,1]；缺省 = 不过滤只打分。 |
| `quality.selection` | str | "threshold" | "threshold" \| "top_ratio"（3.4.3 选择机制行）。"threshold" = 现行为：聚合分 < `quality.threshold` ⇒ dropped_lowq，threshold 缺省则只打分不筛；"top_ratio" = 批内按聚合分降序保留 ceil(top_ratio × 批内存活数) 条。selection="top_ratio" 时不得再设 `quality.threshold`（互斥，M1 报 `CONFIG_ERROR`）。 |
| `quality.top_ratio` | float | 无 | (0,1]；`selection="top_ratio"` 时必填，与 `threshold` 互斥（M1 校验）；selection 为默认 "threshold" 时设置本键无效——M1 打 warning 提示（v1.5）。保留条数 = ceil(top_ratio × 批内存活数)；`on_unscored="keep"` 保留的未打分记录不占名额（3.4.3）。 |
| `quality.judges` | array | [] | 评审团 profile 引用数组。空 = 单评审（用 `quality.llm`）；非空须为奇数个且每项存在于 config.toml `[llm.*]`（M1 校验），每次比较各 judge 独立裁决、per-criterion 多数票（3.4.3 多评审团行，PoLL [32]）。成本 ×\|judges\|。 |
| `quality.both_orders` | bool | false | true 时同一对正反两种呈现顺序各裁决一次（每 judge），两次一致才记 winner、不一致按 tie（3.4.3 双顺序裁决行 [20]）。成本 ×2。 |
| `quality.on_unscored` | str | "keep" | "keep" \| "drop"（3.4.3 裁决失败行）。 |
| `quality.rubric` | str | 自动 | "default:text" \| "default:ui" \| "default:trajectory" \| "inline"。缺省按模态选择；`segment.enabled = true` 时选 trajectory。sequence 若启用 quality，同样只消费已解析 rubric，但必须是 pointwise + 固定 threshold，不能使用 pairwise 或 top_ratio。写 inline 时必须提供 `[[rubric.criteria]]`。 |
| `quality.judgment_reasons` | str/bool | "auto" | "auto" \| true \| false。生效时 pairwise 裁决 Schema 增加 `reason` 字段（3.4.3），写入 trace 供 rubric 优化（7.5）；"auto" = `trace.enabled=true` 且 `trace.channels` 含 "quality" 时开（trace 关闭则不请求 reason，零额外 token）。成本：每次裁决约增加 30–60 输出 token。 |
| `rubric.criteria` | array | 可选 | 内联 rubric，字段见 5.3。 |
| `generate.enabled` | bool | false | 仅文本模态；process 可用 flat 回流，generate_only 必须开启。 |
| `generate.form` | str | "flat" | `"flat"` 或 `"sequence"`。两种形态字段互斥，不能混写。 |
| `generate.llms / instruction` | array/str | ["default"] / 必填† | flat 专用；profile 数组与生成指令。 |
| `generate.mixture / weights` | str/array | "round_robin" / [] | flat 专用；round_robin 或 weighted，weighted 的正权重数与 llms 相等。 |
| `[[generate.styles]]` | array | [] | flat 专用风格表，每项 name 唯一、prompt 非空。 |
| `generate.num_per_record` | int | 2 | flat 专用，每个种子的期望产出数。 |
| `generate.seeds_per_call / num_per_call` | int | 3 / 4 | flat 专用；上下文预算开启时 seeds_per_call 是确定性装填上限。 |
| `generate.seed_min_score` | float | 自动 | flat process 专用，缺省取 quality threshold 或批中位数。 |
| `generate.temperature` | float | 0.9 | flat 专用生成温度。 |
| `generate.sample_validator` | str | 无 | flat 专用 `<python-file>:<attribute-path>` hook，签名 `fn(text: str) -> list[str]`；在相似度过滤前执行。 |
| `generate.seed_examples` | array | [] | flat generate_only 种子池；与 standalone_count 互斥。 |
| `generate.standalone_count` | int | 无 | flat generate_only 无种子形态的目标条数。 |
| `generate.mode` | str | 必填† | † sequence 必填：`"declared"` 或 `"instruction_only"`。flat 禁止设置。 |
| `generate.semantic_llm` | str | 必填† | † sequence 必填；文本 LLM profile，显式声明正 context_window。 |
| `generate.evaluation_llm` | str | 必填† | † sequence 必填；名称必须与 semantic_llm 不同，显式声明正 context_window。 |
| `generate.max_slot_attempts` | int | 3 | sequence 专用，范围 1..20；每个 slot 的 whole-attempt 上限。 |
| `generate.state_validator` | str | 无 | sequence 专用 `<python-file>:<attribute-path>` hook，签名 `validate_state(StateTransitionInput) -> list[str]`。 |
| `annotate.enabled` | bool | true | — |
| `annotate.llm / instruction` | str | default / 必填† | † enabled 时必填。 |
| `annotate.examples` | array | [] | few-shot：[{input, output}]，output 须过用户 Schema（M1 校验）。 |
| `annotate.self_consistency` | int | 0 | 0 = 关（单次标注，v1.1 行为）；启用须 ≥3 且为奇数（M1 校验）：每条记录独立采样 n 次后字段级投票（3.5.2 note 框）。成本：标注调用与 token ×n。 |
| `annotate.sc_temperature` | float | 0.7 | self-consistency 各次采样的 temperature（采样多样性来源 [33]），覆盖 profile 默认；仅 `self_consistency ≥ 3` 时生效。 |
| `annotate.sequence_frames` | int | 20 | v1.8 新增：序列（episode）标注单请求最大关键帧数，∈ **[2, 100]**（越界 CONFIG_ERROR，M1 校验）。成员数 n > k 时确定性均匀降采样 `idx_i = ⌊i·(n−1)/(k−1)⌋, i=0..k−1`（首末帧恒含、严格递增、纯整数零 rng；n ≤ k 取全量，3.5.2 序列行）。**`sequence_frames > 20` 且所引 profile `max_image_px > 2000` ⇒ M1 WARN**（S28：Anthropic 对 >20 图请求单图 >2000px 为 400 硬拒非缩放，现默认 max_image_px=2048 恰撞拒——指引改 ≤ 2000 或降帧；20 图阈值按请求内全部 image block 计）。非 stream 模式显式设置 ⇒ no-op warning（3.1.4）。v1.11（V9，3.9.5）：升格为**上限**——所引 profile 声明 `context_window` 后关键帧数按预算剩余动态收缩 `k_eff = min(sequence_frames, max(2, ⌊剩余 / 每图成本⌋))`（首末帧恒保留、中间均匀下采样，既有降采样语义不变）；未声明预算时即固定上限（现行为）。 |
| `frame.classify.enabled` | bool | false | v1.12 新增：帧级闭集分类开关（M13 帧粒度，3.13.7）——对流模式序列信封的成员帧做批量闭集判决，产物落 `_meta.stream.members[].label`（6.3）。默认关；帧粒度全关时全系统与 v1.11 字节等价（唯 dry-run 估算行与 estimate 键表例外，3.10.3）。启用要求 `segment.enabled = true`（帧粒度仅流模式，2.3.1 帧粒度约束；非流模式请改用 classify + `[class.<name>.annotate]`）。 |
| `frame.classify.llm` | str | "default" | profile 引用；enabled 时计入密钥解析 / `--probe` / 存在性引用集；**永不入 vision 必需集**——附图由解析产物 `FrameClassifyConfig.vision_resolved`（= ui ∧ enabled ∧ profile.supports_vision）自动推导（3.1.4 帧粒度配置行；成本控制面 = 指向纯文本 profile，判决仅凭摘要行）。 |
| `frame.classify.fallback_class` | str | 必填† | † enabled 时必填且 ∈ 帧类表 name 集（2.3.1 帧粒度约束）：修复穷尽 / 窗口失败的兜底类（3.13.7 失败语义；LLM 亦可主动选择它）。 |
| `[[frame.classify.classes]]` | array | 必填† | † enabled 时经「fallback_class ∈ 类表」传递性要求非空（无独立 ≥ 2 类数下限，与 `[[classify.classes]]` 有意不同，3.1.4）。每项：`name`（`[a-z0-9_]+`，表内唯一）、`description`（非空）、`examples`（字符串数组，可选——**解析合法但帧级批量判决模板不渲染**（§10.12 只渲染类表，与序列级 few-shot 有意不同），在场时 M1 显名 WARN `class examples are not rendered by the batched frame-verdict template (§10.12), so this key is ignored`，静态预算预检口径同步不计，3.1.4）。**帧类表与序列类表相互独立、允许重名、互不约束**（计数命名空间同分离：`frame_classify.*` vs `classify.*`，6.4）。 |
| ~~`frame.classify.assignment`~~ | — | —（不提供） | v1.12 定向探针键：显式书写 → CONFIG_ERROR——帧分类恒为单一归属（帧多标签/帧级扇出为 v1.12 非目标，8.1）；多标签扇出请用序列级 `[classify].assignment`（机制 = v1.11 `use_vision` 原始节探针同款，3.1.4）。 |
| `frame.annotate.enabled` | bool | false | 帧级逐帧标注开关（M5 帧粒度，3.5.5），产物落 `_meta.stream.members[].annotation/status`（6.3）。process/flat 路径启用要求 `segment.enabled = true`，并在序列级标注成功后执行；sequence 路径可脱离 segment 作为 attempt-local 下游，且 `annotate.enabled=false` 时直接执行 frame pass、序列标注零调用。sequence 的任一应标注帧失败都拒绝并重试整个 counterfactual set。`frame.*` 任一启用 ⇒ `output.meta_mode != "none"`（帧产物仅经 `_meta.stream.members` 承载，sidecar 合法——2.3.1 帧粒度约束）。 |
| `frame.annotate.llm` | str | "default" | profile 引用；enabled 时计入密钥解析 / `--probe` / 存在性引用集；ui 模态 ∧ enabled 时**无条件入 vision 必需集**（镜像序列级 annotate——截图是标注主证据，3.1.4 帧粒度配置行）。 |
| `frame.annotate.instruction` | str | 必填† | † enabled 时必填（非空，M1 校验）。全局帧标注指令；`[frame.class.<name>.annotate]` 可按帧类覆盖（见按类覆盖表 v1.12 注）。 |
| `frame.annotate.examples` | array | [] | few-shot：[{input, output}]，output 须过**帧级 Schema**（M1 干跑校验，3.1.4；帧级无 L2.5 hook）；形态镜像 `annotate.examples`。 |
| `frame.annotate.schema_path` | str | 二选一 | 外部 .json 的帧级输出 Schema；与 `schema_inline` 恰一（2.3.1 帧粒度约束；解析产物 `ResolvedConfig.frame_schema`，`user_schema` 同胞——draft 2020-12 元校验，镜像 `output.schema` 全套分支）。 |
| `frame.annotate.schema_inline` | str | 二选一 | TOML 多行字符串内嵌的帧级 Schema JSON 文本（同上）。 |
| ~~`frame.annotate.self_consistency`~~ | — | —（不提供） | v1.12 定向探针键：显式书写 → CONFIG_ERROR——自洽采样成本 ×n 且投票键须取自帧 Schema（v1.12 非目标，8.1）；自洽采样请用序列级 `[annotate].self_consistency`（机制同上）。 |
| `verify.enabled` | bool | false | — |
| `verify.llm` | str | "judge"† | † `verify.enabled = true` 且 `verify.judges` 为空时该 profile 须存在于 config.toml `[llm.*]`（judges 非空时被评审团替代、不参与运行也不要求存在，v1.5）；建议独立于 annotate.llm（3.7.2）。 |
| `verify.judges` | array | [] | 多评审团 profile 列表（v1.2，3.7.2；与 quality.judges 语义一致）：空 = 单评审用 verify.llm；非空须为奇数个（M1 校验），verdict 取多数票，critiques 合并并标注来源 judge，成本 ×\|judges\|。背书 PoLL [32]。 |
| `verify.policy / max_repair_rounds` | str/int | "drop" / 1 | 3.7.3。 |
| `verify.extra_criteria` | str | "" | 追加评审维度的自由文本。 |
| `output.schema_path` | str | 二选一 | 外部 .json 的用户 Schema；与 schema_inline 恰一。 |
| `output.schema_inline` | str | 二选一 | TOML 多行字符串内嵌的 Schema JSON 文本。 |
| `output.max_repair_attempts` | int | 2 | 结构引擎 L3 次数（3.8.2）。 |
| `output.repair_llm` | str | 同调用方 | L3 修复用 profile。 |
| `output.validator` | str | 无 | `<python-file>:<attribute-path>` 形式的 L2.5 回调，签名 `fn(obj: dict, record: dict \| None) -> list[str]`；相对文件按 project root 解析。仅作用于用户 Schema 标注调用，违规进入同一 L3 repair 预算。 |
| `output.meta_mode` | str | "inline" | "inline" \| "sidecar" \| "none"（6.3）。 |
| `output.passthrough_fields` | array | [] | 从 Record.raw 透传进 _meta.source.fields 的字段名列表。 |
| `output.rejects` | str | "refs" | "none" \| "refs" \| "full"（3.11.2）。 |
| `trace.enabled` | bool | false | 启用 trace 追踪日志（第 7 章）。 |
| `trace.path` | str | 自动 | 默认 `{output_stem}.trace.jsonl`，与主输出同目录。 |
| `trace.channels` | array | ["quality","verify","schema"] | 可选值 ingest \| segment（v1.8 增）\| stitch（v1.9 增）\| dedup \| classify（v1.7 增）\| extract（v1.8 增）\| quality \| annotate \| verify \| schema \| llm（十一个，7.2 事件目录；通道 = stage 名，S1）；默认值不变——分类事件须用户显式加 "classify"、分段/摘取/缝合事件须显式加 "segment" / "extract" / "stitch" 才写；run.*/batch.* 生命周期事件不受此过滤。 |
| `trace.content` | str | "refs" | "none" \| "refs" \| "excerpt" \| "full" 内容脱敏四档（7.4）。 |

**`[class.<name>.<section>]` 按类视图。**process 下由 classify 类表建立路由类；sequence 下由带
description 的 `[class.<name>]` 直接声明 sequence class，classify 必须关闭。两种入口都在 M1 合并并冻结
`ClassView`，未出现的通用下游键继承全局节；白名单外键报 `CONFIG_ERROR`。

| 节 | 可覆盖键 | 不可覆盖（保持全局）及理由 |
|---|---|---|
| `[class.*.quality]` | mode, rounds, rubric（含 `[class.*.rubric]` 内联子表，结构同 5.3）, threshold, selection, top_ratio | llm / judges / both_orders / criteria_per_call / on_unscored——LLM 绑定属部署与成本面，类差异先用 rubric 表达（1.6 v1.7 对齐决策 ④） |
| `[class.*.annotate]` | instruction、examples、schema_path / schema_inline（至多其一） | 类声明 Schema 时整份覆盖，否则回落全局 output Schema；按类 few-shot 走有效 Schema 与 output validator。 |
| `[class.*.generate]` | flat：instruction、styles、num_per_record、temperature；sequence declared：instruction、state_schema_path、initial_state_source、initial_state_catalog_path | flat 与 sequence 字段按 `generate.form` 互斥。sequence 的 llm source 禁止 catalog_path；catalog source 要求完整 ScenarioSeed JSONL。 |
| `[class.*.verify]` | extra_criteria | llm / judges / policy / max_repair_rounds |
| `[class.*.extract]` | instruction（v1.8 增） | llm / include_diff / on_error——LLM 绑定与失败策略属部署与成本面（与 quality 行同理） |
| `[frame.class.*.annotate]`（v1.12） | instruction, examples, enabled（enabled = false ⇒ 该帧类成员跳过帧标注——省成本面，members[] 呈现 status="skipped"，3.11.2） | llm / schema——LLM 绑定属部署与成本面；帧级标注 Schema 按粒度唯一（8.4 M13 行）。v1.18 sequence 另声明下行 `generate` 节；两节之外的节名 ⇒ CONFIG_ERROR（3.1.4 帧粒度配置行） |
| `[frame.class.*.generate]` | instruction、schema_path / schema_inline | sequence 引用到的每个 frame class 必须有非空 instruction，并恰选一个 object JSON Schema；不支持 string payload。flat/process 不读取本节。 |
| —— | —— | run、input、stream、dedup、segment、stitch、classify、trace 均不可按类；output 中只有标注 Schema 可按 sequence class 覆盖，其余输出面保持全局唯一。 |

v1.8 注：`segment.*` 不入白名单是**链序因果**而非取舍——链序为 segment → stitch → dedup → classify → extract →…（3.10.3），segment 在 classify **之前**执行，成段时类标签尚不存在，「按类分段」无从谈起；extract 在 classify 之后，故其 `instruction` 可按类覆盖（multi 扇出下兄弟信封各按其标签的有效 instruction 摘取，S9，3.15）。v1.9 注：`stitch.*` 不入白名单同为链序因果——stitch 亦在 classify 之前（3.10.3），`[class.<name>.stitch]` 不存在（3.1.4 线索缝合行）。v1.12 process 注：`[frame.class.<name>.annotate]` 按**帧类**覆盖（键控 `[[frame.classify.classes]]` 类表，要求 `frame.classify.enabled = true`，3.1.4 帧粒度配置行②），与 `[class.<name>.*]` 的序列类覆盖是两个独立命名空间。v1.18 sequence 例外：`[frame.class.<name>]` 本身声明生成闭集，frame.classify 必须关闭；投影器写入 inherited frame Classification，frame.annotate 按该值选择 `frame_class_views`。两条路径均保持帧类与 sequence 类两个独立命名空间，允许重名、互不约束；零覆盖类也各得一份冻结视图，运行期零回退。

合并优先级：`[class.<name>].<sect>.<key>` > project.toml `[<sect>].<key>` > 内置默认——这是 project.toml **内部**的条件化合并，不改变「CLI > project.toml > config.toml」三源优先级（2.5）。M1 启动时按逐键 provenance 静态合并、冻结为 `class_views`，运行期零查找成本；选择组互斥对剔除、per-class rubric 重解析、类 examples 干跑等精确语义见 3.1.4 按类覆盖合并行。

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

### 5.2.1 sequence 生成公共配置（v1.20）

sequence 是 `generate.form = "sequence"` 的唯一入口；`generate.mode` 在 declared 与 instruction-only
之间互斥。它要求 generate_only、text、global dedup、inline meta、rejects none、无 `--limit`，
并静态关闭 classify 与 frame.classify；frame.annotate 可作为 attempt-local 下游。既有 `[stream]` 只属于
process 摄取，不参与 sequence 时间线。

#### declared sequence class 与初始世界

~~~toml
[class.ticket_booking]
description = "一次订票请求与处理结果"

[class.ticket_booking.generate]
instruction = "保持路线、日期、乘客、请求和票号前后一致。"
state_schema_path = "schemas/state.json"
initial_state_source = "catalog"
initial_state_catalog_path = "catalogs/ticket-booking.jsonl"
~~~

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `class.<name>.description` | str | 必填 | sequence class 的非空描述；name 匹配 `[a-z0-9_]+`。 |
| `class.<name>.generate.instruction` | str | 必填† | † declared 非零 slot class 必填。 |
| `class.<name>.generate.state_schema_path` | str | 必填† | † declared 必填；object JSON Schema，文件上限 65536 bytes；LLM source 根级 `examples` 至少含一个通过完整 Schema 的 object。 |
| `class.<name>.generate.initial_state_source` | str | 必填† | `llm` 或 `catalog`。 |
| `class.<name>.generate.initial_state_catalog_path` | str | 条件必填 | catalog source 必填；每行是完整 `ScenarioSeed`，应用完整配置后按 slot 无放回分配。 |

`ScenarioSeed` 固定包含 initial_state、actors、shared_facts.public、shared_facts.hidden、style 与 time_context。
declared actors 恰等于该 pattern 全部 role.actor 与 observers 的并集；同一 class 的所有 pattern actor 集相同。
seed 不得携带 variant、目标违规或分支结果。hidden 只供独立 evaluator 使用。

#### frame class

~~~toml
[frame.class.task_request]
description = "用户发起任务请求"

[frame.class.task_request.generate]
instruction = "请求者提出尚未完成的同一请求。"
schema_path = "schemas/frame-request.json"
duration_s = 120
resources = ["foreground_app"]
time_bindings = [
  { payload_path = "/timestamp", source = "event_start_milliseconds" },
  { payload_path = "/endTime", source = "event_end_milliseconds" },
]
~~~

每个被 role、instruction-only 或 noise 引用的 frame class 都必须声明 description、非空 instruction，并用
schema_path 或 schema_inline 恰选一个 object JSON Schema。Schema 根级 `examples` 至少含一个通过完整 Schema 的
object；M1 选择 canonical byte 最小 witness。sequence 不接受 string payload。

完整 Schema 的业务时间叶必须写 `x-labelkit-business-time = true`，且 path 集与 `time_bindings` path 集完全相等。
frame source 只允许 start/end/duration milliseconds 或 start/end fixed-offset ISO8601。M1 同时冻结完整 Schema 与剥离
时间叶子的 model Schema；provider 与 L3 只消费 model Schema。`duration_s` 缺省为点事件，在场时必须是正整数毫秒；
resource 名匹配 `[a-z0-9_]+`、声明序唯一且要求正 duration。

declared sequence annotation 以同样标记声明 class Schema 时间叶，并在 `[class.<name>.annotate]` 声明：

~~~toml
[class.ticket_booking.annotate]
time_bindings = [
  { payload_path = "/actionInfo/timestamp",
    source = "first_resource_start_milliseconds",
    resource = "foreground_app" },
]
~~~

该配置只允许 generate-only sequence declared mode；annotation 时间来自最终 members 中目标 resource 最早正区间 start。
annotate disabled、ordinary process、ordinary annotation 或 instruction-only 声明均为 CONFIG_ERROR。

#### pattern、role、binding 与 gap

~~~toml
[generate.pattern.booking_success]
sequence_class = "ticket_booking"
description = "请求者提交订票需求，系统确认受理并在允许时间内给出出票结果。"
order = ["request", "acknowledge", "confirm"]
max_span_s = 1800

[[generate.pattern.booking_success.roles]]
name = "request"
frame_class = "task_request"
actor = "requester"
read_roots = ["/public", "/request", "/actors/requester"]
write_roots = ["/request", "/actors/system/knowledge"]
publish_roots = ["/request/id", "/request/status"]
observers = ["requester", "system"]
state_instruction = "创建 pending 请求。"
pre_state_schema_path = "schemas/pre-request.json"
payload_bindings = [
  { payload_path = "/request_id", state_phase = "after", state_path = "/request/id" }
]

[[generate.pattern.booking_success.roles]]
name = "acknowledge"
frame_class = "acknowledgement"
actor = "system"
read_roots = ["/request", "/actors/system"]
write_roots = ["/request", "/audit"]
publish_roots = ["/request/id", "/request/acknowledged"]
observers = ["requester", "system"]
state_instruction = "确认已接收请求，不得声称已经出票。"
payload_bindings = [
  { payload_path = "/request_id", state_phase = "after", state_path = "/request/id" }
]

[[generate.pattern.booking_success.roles]]
name = "confirm"
frame_class = "confirmation"
actor = "system"
read_roots = ["/request", "/ticket", "/actors/system"]
write_roots = ["/request", "/ticket", "/audit", "/sla"]
publish_roots = ["/request/id", "/ticket/id", "/request/status"]
observers = ["requester", "system"]
state_instruction = "产生与当前分支相符的最终处理结果。"
payload_bindings = [
  { payload_path = "/request_id", state_phase = "after", state_path = "/request/id" },
  { payload_path = "/ticket_id", state_phase = "after", state_path = "/ticket/id" }
]

[[generate.pattern.booking_success.gaps]]
name = "request_to_acknowledge"
before = "request"
after = "acknowledge"
min_gap_s = 0
max_gap_s = 120

[[generate.pattern.booking_success.gaps]]
name = "acknowledge_to_confirm"
before = "acknowledge"
after = "confirm"
min_gap_s = 30
max_gap_s = 1200

[[generate.pattern.booking_success.containments]]
container = "request"
contained = "acknowledge"
~~~

| 面 | 约束 |
|---|---|
| pattern | description 非空；role name 唯一；order 恰排列全部 role；max_span_s 必填且大于零，闭区间。 |
| adjacent gap | order 中每个相邻 pair 恰有一条具名 gap，max_gap_s 必填；min_gap_s 缺省零。 |
| extra gap | 只允许沿 order 正向的唯一非相邻 pair。秒值最多六位小数并无损转整数微秒，边界闭合。 |
| roots | RFC 6901 token 前缀；同一列表内禁止祖先/后代冗余，read/write/publish 列表之间可相交。 |
| patch | 只允许 test/add/remove/replace，至少一个 test 且 test 连续位于写操作前；test 落 read roots，写操作落 write roots。 |
| publish | publish roots 在事件后存在；observers 必须来自 seed actors。 |
| binding | state_path 同时被当前 role 的 read 与 publish roots 覆盖；state payload path 与 time binding path 不得相等或互为前缀。provider 按 model Schema 返回非时间 object；框架注入时间后以完整 Schema 复验。 |
| calendar | role 可选引用一个命名 `calendar_window`。 |
| containment | container/contained 各出现一次、正 duration、不同且不共享 resource；仍同时存在时满足 `container.start <= contained.start` 且 `contained.end + 1000us <= container.end`。 |
| counterexample eligibility | missing 目标 frame class 在 pattern 内唯一；reordered 的目标 role 相邻且 frame class 不同。 |

#### counterfactual set

~~~toml
[[generate.counterfactual_sets]]
name = "booking_success_training"
pattern = "booking_success"
count = 2

[[generate.counterfactual_sets.variants]]
name = "positive"
kind = "positive"
outcome_schema_path = "schemas/outcome-positive.json"

[[generate.counterfactual_sets.variants]]
name = "missing_acknowledgement"
kind = "missing"
target_role = "acknowledge"
outcome_schema_path = "schemas/outcome-missing.json"

[[generate.counterfactual_sets.variants]]
name = "confirmation_before_acknowledgement"
kind = "reordered"
target_before = "acknowledge"
target_after = "confirm"
outcome_schema_path = "schemas/outcome-reordered.json"

[[generate.counterfactual_sets.variants]]
name = "confirmation_timeout"
kind = "interval_exceeded"
target_gap = "acknowledge_to_confirm"
min_excess_s = 1
max_excess_s = 600
outcome_schema_path = "schemas/outcome-timeout.json"
~~~

count 是精确 counterfactual set 数。每组至少一个 variant；variant name 与预期违规签名唯一，每个 variant
都有 outcome Schema。positive 可缺省，但 baseline 始终生成和判定。missing、reordered、
interval_exceeded 分别只允许 `target_role`、相邻 `target_before/target_after`、或 `target_gap` 加
闭区间 excess。编译期必须证明目标变换与所有非目标 gap、max span、日历约束可同时满足。

#### instruction-only

~~~toml
[[generate.instruction_only]]
name = "open_booking"
sequence_class = "ticket_booking"
count = 1
len_range = [3, 6]
instruction = "生成一次完整、自然、状态连续的订票交互。"
state_schema_path = "schemas/state.json"
~~~

name 唯一，sequence_class 有效，count 是精确序列数；len_range 两端位于 1..8。显式 state Schema 根级
`examples` 至少含一个通过完整 Schema 的 object；缺省为只要求 object 的固定 Schema，并以 `{}` 作 witness。
instruction-only 禁止 pattern、counterfactual set、role permission、outcome
Schema 与 expected violation；每 attempt 调 ScenarioSeedGenerator，不支持 catalog。

#### timeline、calendar 与 noise

~~~toml
[generate.timeline]
timestamp_start = "2026-01-05T09:00:00+08:00"
event_gap_s = [5, 60]
primary_sessions = 8
crossed_primary_sessions = 0
session_max_events = 16
session_max_span_s = 3600
session_gap_s = 3600
noise_events = 2
duplicate_sequences = 1

[generate.calendar_window.service_hours]
utc_offset = "+08:00"
days = ["mon", "tue", "wed", "thu", "fri"]
intervals = [["08:00:00", "12:00:00"], ["13:00:00", "18:00:00"]]

[generate.noise]
frame_class = "noise"
instruction = "生成与任何任务无关、没有可执行诉求的一条自然输入。"
topics = ["夜空中的月相观察", "手工面包出炉时的香气"]
~~~

| 字段 | 类型 | 约束 |
|---|---|---|
| `timestamp_start` | offset datetime | 必填；不取墙钟。 |
| `event_gap_s` | closed seconds range | instruction-only 相邻位置与 noise 的铺设间隔；不约束 declared role gap。所有值必须毫秒对齐。 |
| `primary_sessions` | int | primary 总数 N、crossed 数 D 时必须等于 N - D。 |
| `crossed_primary_sessions` | int | 0..floor(N/2)；每个 crossing session 恰有两个不同 set owner。 |
| `session_max_events` | int | 每个 primary session 的事件容量。 |
| `session_max_span_s` | seconds | 必填正值；session 完整 interval envelope 的跨度上限。 |
| `session_gap_s` | seconds | 相邻 session 的最小间隔。 |
| `noise_events` | int | 精确 noise slot 数；大于零时 generate.noise 必填。 |
| `duplicate_sequences` | int | 精确 whole-positive-sequence replay 数；source 无放回，source 不足启动失败；每条 replay 冻结一个正 constant shift。 |
| calendar window | table | 固定 UTC offset、weekday 闭集、同日半开 intervals；名称唯一。 |
| noise | table | frame class 有 object Schema、零 duration、空 resources，且不得被任何 role 使用；topics 是与 noise_events 等长的非空唯一话题表；noise 无 owner、state patch 或任务真值。 |

instruction-only 强制 crossed 为零、primary_sessions = N、duplicates 为零。每个 declared primary session 恰有
一或两个不同 counterfactual set owner，同一 set 的 variants 永不共 session；每个 replay 独占尾部 session。

#### 固定上限与精确求解

| 对象 | 上限 |
|---|---:|
| pattern roles | 32 |
| variants per set | 8 |
| instruction-only events | 8 |
| ScenarioSeed / state or outcome Schema / frame Schema | 65536 bytes |
| event patch | 16384 canonical JSON bytes |
| rendered payload | 65536 canonical JSON bytes |
| one dynamic prompt value | 32768 UTF-8 bytes |
| one L3 newly appended message-body set | 32768 UTF-8 bytes |
| one generation prompt text | 32768 UTF-8 bytes |
| `record_units` / `stream_rows` | 500000 |
| `retained_content_bytes` | 536870912 |

`record_units = primary_sequences + primary_events + noise_events + replay_events`；
`stream_rows = primary_events + noise_events + replay_events`。计数先以 Python integer 检查，再进入 OR-Tools。
generation prompt text 包括每项 class/frame/pattern description 与 class/frame/role/instruction-only/noise
instruction。state/outcome/frame 的运行期产出 Schema 根级 `examples` 必须含有效 object；M1 按 canonical byte 长度、
canonical bytes 选择唯一最小 witness，并要求其不超过对应 prompt-value/payload 上限。M1 以共享构造器形成六个完整
PromptBundle，再加动态值与 L3 新增正文 byte 包络证明首轮/repair profile；运行期恰好上限可派发，多一 byte 零派发拒绝。
六个 family 的 2D/5D/5D+P/6D+P/S+2D/Y、`ceil(bytes/3)`、structured Schema 与 repair replay
精确公式以 3.1.4「console 与上下文预算」后的规范段为唯一实现口径。
retained bytes 在当前声明序 head 的无 `await` 提交临界区、dedup commit 前，按最终 main/stream canonical bytes
精确计费。M11 先从最终 item 与 pre-downstream projection 装配 `SequenceRows`，ReplayProjector 再只从 source
`SequenceRows.primary_stream_rows` 构造全部计划 `ReplayRows`。比较值恰等于已提交累计加当前 prepared
candidate 的实际 retained bytes；两者共用 `canonical_delivery_row`，每行按 canonical UTF-8 bytes 加一个
JSONL 换行 byte 计。超限拒绝当前 whole-slot attempt，不能裁剪 payload 或 truth，也不能提交 reservation、
dataset counters 或 frontier delta。

planner 按完整 session 分 block，单 block 最多 4096 primary events。OR-Tools 单 worker、确定性 seed、
每层 `max_deterministic_time = 10.0`；只解码 OPTIMAL。INFEASIBLE 是配置失败，FEASIBLE/UNKNOWN 是
planner deterministic-budget failure，MODEL_INVALID 是内部错误；无 incumbent、替代求解器或近似 dry-run。

## 5.3 Rubric 结构（内联或默认包文件，同一 TOML 结构）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `rubric.name` | str | 必填 | rubric 标识，入 _meta 与报告。 |
| `rubric.criteria[].key` | str | 必填 | `[a-z0-9_]+`，全局唯一。 |
| `rubric.criteria[].weight` | float | 1.0 | 聚合权重（>0）。 |
| `rubric.criteria[].description` | str | 必填 | 准则含义（进入两种模式的提示词）。 |
| `rubric.criteria[].pairwise_prompt` | str | 必填 | 成对比较问句，如「哪段文本的写作水平更高？」。 |
| `rubric.criteria[].pointwise_levels` | array[6] | pointwise 必填 | 0–5 六级加性描述（附录 A 示例）。 |

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
| `embedding.*.api_key_env` | str | 必填* | 持有 API Key 的环境变量名；被 `dedup.semantic_embedding` 引用时须存在且非空（M1 校验，3.1.4）。* v1.6：与 `embedding.*.api_key_envs` 恰提供其一。 |
| `embedding.*.api_key_envs` | array | 无 | v1.6：同 `llm.*.api_key_envs`——embedding profile 的密钥池，机制一致（3.9.3 密钥池行）。 |
| `embedding.*.max_concurrency` | int | 8 | 该 profile 并发上限（信号量，与 llm.* 同机制，3.9.3）。 |
| `embedding.*.timeout_s` | int | 60 | 单次请求超时。 |
| `embedding.*.max_retries` | int | 5 | 可重试错误的最大重试次数（重试规则同 3.9.3）。 |
| `embedding.*.dims` | int | 可选 | 返回向量维度校验：配置后 `embed()` 逐条比对返回维度，不匹配抛 ProviderFatalError（3.9.2）。 |
| `tool.log_format` | str | "text" | "text" \| "jsonl"：stderr 运行日志行格式（7.3）；"jsonl" 时强制 console plain 档以保证 stderr 逐行可解析（7.7，显式 rich（CLI `--console rich` 或 `console.mode="rich"`）冲突时 M1 WARN）。 |
| `console.mode` | str | "auto" | v1.10（7.7）："auto" \| "rich" \| "plain"——进度显示面三态；被 CLI `--console` 覆盖。auto 判定链：stderr TTY ∧ log_format="text" ∧ TERM 非 dumb/空 ∧ rich 可导入（M1 以 find_spec 探测），全真取 rich，否则 plain（TERM 定性为终端能力探测，与 isatty 同级，非配置通道；NO_COLOR 不参与判定——rich 原生剥色保布局，U25）。判定产物由 M1 冻结为解析字段 `mode_resolved`（3.1.4）。plain 档 stderr 与 v1.9 行为等价（`heartbeat_s=0` 时——三层回归锚 7.8）。 |
| `console.refresh_hz` | int | 5 | v1.10：rich 画布重绘频率（asyncio 节流 tick，7.7），1–10，越界 = CONFIG_ERROR。 |
| `console.heartbeat_s` | int | 0 | v1.10：仅 plain 且非 TTY 生效——每 N 秒一行数据无关汇总心跳（固定键集 `heartbeat batch= stage= llm_calls= elapsed=`，7.7）；0 = 关（默认，保回归锚；对齐决策 1.6 U14）；< 0 = CONFIG_ERROR。 |
| `console.estimate` | bool | false | v1.10：仅文本模态生效——启动时做估算扫描（`Ingestor.scan(estimate=True)`，全量多读一遍输入）换取批总数分母与 ETA（7.7；对齐决策 1.6 U17）；UI 模态分母天然廉价（live 预扫复用）、恒显示，本键无效。 |
| `console.interactive` | bool | true | v1.10：rich ∧ stdin TTY ∧ termios 可用时启用键盘开关（封闭键集 `? l e + - p q`（`h` 为 `?` 同义键），7.7）；false = 纯渲染（stdin 完全不被占用——buck2 `--no-interactive-console` 对应物）。 |

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
| `run.max_park_s` | int | 3600 | v1.6 驻留上限（3.9.3 密钥池行）：单次逻辑 LLM 调用因「所引 profile 全部存活密钥均在冷却」而驻留等待的累计秒数上限；超限按重试耗尽处理（记录 failed、计入熔断窗口，1.6 对齐决策 ③）。0 = 不驻留（全池冷却即按重试耗尽失败）——注意：0 与单密钥 profile 组合意味着**任何 429（含短 Retry-After）都立即按重试耗尽失败**，仅建议在多密钥池上设 0。运维容忍度参数，不影响产出内容；单密钥配置下亦约束超长 `Retry-After` 等待（3.9.3 重试行）。 |
| `input.text_field` | str | "text" | 文本模态取文内容的点路径（3.2.5）。 |
| `input.on_bad_line / on_missing_pair / on_index_conflict` | str | skip / skip / fail | "skip" \| "fail"（3.2.4–3.2.5）。 |
| `input.max_image_mb` | int | 20 | 单图大小上限。 |
| `input.ui_tree_max_chars` | int | 30000 | 提示词中树序列化长度上限。 |
| `stream.order_by` | str | "input_order" | v1.8 新增（`[stream]` 节 = stream 模式输入侧排序与会话化声明，M2 消费，3.2/3.14；仅 `segment.enabled = true` 时生效）。"input_order"（默认：文本 = 文件名字典序→行号，UI = pair_index 升序）\| "meta:<field>"（**仅文本模态**，M1 校验；时间戳解析规格见 6.1——数值秒/毫秒判定、ISO 字符串、时区归一；解析失败与乱序同走 on_disorder）。 |
| `stream.on_disorder` | str | "skip" | v1.8："skip"（默认：乱序/时间戳解析失败记录跳过——计 bad_input + IngestReport.disorder 子计数 + `ingest.disorder` 事件 + WARN 一次）\| "fail"（InputError，退出码 3）。单调性游标**按分区键各自维护**（S19；键变即断语义保留，输入须按键成组，6.1）。 |
| `stream.key` | array | [] | v1.8：分区键列表，键变即断会话（groupby 语义非 keyBy）。元素 = "meta:<field>"（仅文本模态）\| "source_dir"（= ref.source_file 父目录派生，UI 模态可用——一次采集一目录惯例，S19）；元素合法性 M1 校验（3.1.4）。 |
| `stream.gap_s` | int | 300 | v1.8：相邻记录时间差 > gap_s 秒即断开会话；**仅 `order_by="meta:*"` 时可设**（M1 校验）。默认偏大的结构性论证：欠分割可由 LLM 边界精化拯救、过分割不可逆（3.14）。 |
| `stream.gap_steps` | int | 0 | v1.8：相邻记录序号差 > gap_steps 即断开（0 = 不启用）；与 gap_s 可并用，任一触发即断。 |
| `stream.session_max_len` | int | 200 | v1.8：会话硬上限（帧），到限即断。`session_max_len > run.batch_size` ⇒ M1 静态 WARN（S21：单会话超批容量将被 M10 硬切 + `session_split` 标，3.10.3）。 |
| `stream.session_max_span_s` | int | 0 | v1.8：会话时间跨度硬上限（秒，0 = 不启用）；**仅 `order_by="meta:*"` 时可设**（M1 校验）。 |
| `segment.enabled` | bool | false | v1.8 新增：语义分段算子 / stream 模式总开关（M14，3.14）。默认关——工具行为与 v1.7 逐字节一致（`_meta.stream: null` 除外，6.3）。启用要求（3.1.4）：`run.mode = "process"` ∧ `generate.enabled = false`（generate_only 经 2.3.1 ④ 传递闭合）∧ `annotate.enabled = true`。no-op warning（R8 家族）：`[stream]`/`[segment]`/`[extract]` 任一节在场而 `segment.enabled = false`。 |
| `segment.strategy` | str | "hybrid" | "rules"（候选会话原样成 episode，零 LLM；noise_filter / min_len 不生效）\| "llm" \| "hybrid"（默认：滑窗 LLM 边界精化 + 逐帧噪声标记；len(session)==1 走 rules 退化，3.14）。 |
| `segment.llm` | str | "default" | profile 引用；**仅 `strategy ∈ {llm, hybrid}` 时**计入密钥解析 / vision（仅 use_vision = true 时）/ `--probe` / 存在性四处引用集（S30，3.1.4）——rules 策略零调用不强制配键。 |
| `segment.window` | int | 20 | 滑窗帧数/调用；M1 校验 **≥ 2**。步长 = window−1（重叠 1 帧，接缝帧整帧判决归后窗）；window ≥ 会话长时天然退化为整段单调用（S32）。 |
| `segment.digest_max_chars` | int | 400 | 单帧摘要（frame_digest，4.3）长度上限。 |
| `segment.noise_filter` | bool | true | 逐帧噪声标记（interruption → dropped_noise，reason="noise"）；仅 llm/hybrid 生效——`strategy = "rules"` ∧ noise_filter = true ⇒ no-op warning（3.1.4）。 |
| `segment.min_len` | int | 2 | 段最短帧数；**仅作用于 LLM 边界精化切出的段**（S11）——规则层孤帧/短会话（含 strategy="rules"）原样成 episode、不受本键约束；被丢弃帧 reason = "below_min_len"（≠ "noise"），独立计数 `report.stream.below_min_len`（6.4）。 |
| `segment.use_vision` | bool | false | true 时窗内逐帧附截图（所引 profile 须 supports_vision 且入 vision 引用集，S30）；默认纯文本（仅帧摘要）。 |
| `segment.context` | str | "" | 可选域上下文，注入判据模板；**非边界定义**——边界判据内置于模板（3.14），零配置可用。 |
| `segment.on_error` | str | "keep" | 单窗结构修复耗尽的处置："keep"（默认：该会话整体成一个 episode 存活 + 留痕三件套 `_meta.stream.degraded = {kind:"segmentation_invalid", windows_failed}` / error 事件 / `segment.failures` 计数，**不写 item.errors**——S26 归因防污染）\| "fail"（会话成员全部 failed → rejects，kind = segmentation_invalid，7.6）。 |
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
| `dedup.minhash_threshold` | float | 0.85 | Jaccard 判重阈值（工业通行 0.8–0.9 [3][6]）。 |
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
| `quality.rubric` | str | 自动 | "default:text" \| "default:ui" \| "default:trajectory"（v1.8 增：轨迹四准则 rubric，包数据 `default_trajectory.toml`，附录 A.3）\| "inline"。缺省（空串）按模态选默认；**v1.8 空串解析规则：`segment.enabled = true` ⇒ 解析为 "default:trajectory"**（两模态一致；用户显式选择器恒优先；按类视图经 base selector 自动继承，S29）。trajectory rubric 与 `extract.enabled = false` 组合 ⇒ M1 warning 提示（rubric 模态中立、不预设 steps 在场——「步骤」退化读作「帧间变化」，S29）。写 inline 时必须提供 [[rubric.criteria]]。 |
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
| `annotate.sequence_frames` | int | 20 | v1.8 新增：序列（episode）标注单请求最大关键帧数，∈ **[2, 100]**（越界 CONFIG_ERROR，M1 校验）。成员数 n > k 时确定性均匀降采样 `idx_i = ⌊i·(n−1)/(k−1)⌋, i=0..k−1`（首末帧恒含、严格递增、纯整数零 rng；n ≤ k 取全量，3.5.2 序列行）。**`sequence_frames > 20` 且所引 profile `max_image_px > 2000` ⇒ M1 WARN**（S28：Anthropic 对 >20 图请求单图 >2000px 为 400 硬拒非缩放，现默认 max_image_px=2048 恰撞拒——指引改 ≤ 2000 或降帧；20 图阈值按请求内全部 image block 计）。非 stream 模式显式设置 ⇒ no-op warning（3.1.4）。 |
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
| `trace.channels` | array | ["quality","verify","schema"] | 可选值 ingest \| segment（v1.8 增）\| stitch（v1.9 增）\| dedup \| classify（v1.7 增）\| extract（v1.8 增）\| quality \| annotate \| verify \| schema \| llm（十一个，7.2 事件目录；通道 = stage 名，S1）；默认值不变——分类事件须用户显式加 "classify"、分段/摘取/缝合事件须显式加 "segment" / "extract" / "stitch" 才写；run.*/batch.* 生命周期事件不受此过滤。 |
| `trace.content` | str | "refs" | "none" \| "refs" \| "excerpt" \| "full" 内容脱敏四档（7.4）。 |

**`[class.<name>.<section>]` 按类覆盖（v1.7）。**classify 启用时可按类覆盖下游算子参数：`<name>` 必须 ∈ classes；未出现的键一律继承全局节（不配任何覆盖即纯打标模式）。可覆盖键白名单（M1 强校验，白名单外的键报 `CONFIG_ERROR`——3.1.4「未知键报 warning」行的显式例外；白名单后续只增）：

| 节 | 可覆盖键 | 不可覆盖（保持全局）及理由 |
|---|---|---|
| `[class.*.quality]` | mode, rounds, rubric（含 `[class.*.rubric]` 内联子表，结构同 5.3）, threshold, selection, top_ratio | llm / judges / both_orders / criteria_per_call / on_unscored——LLM 绑定属部署与成本面，类差异先用 rubric 表达（1.6 v1.7 对齐决策 ④） |
| `[class.*.annotate]` | instruction, examples | llm / self_consistency / sc_temperature |
| `[class.*.generate]` | instruction, styles, num_per_record, temperature | llms / mixture / weights / seeds_per_call / num_per_call / sample_validator |
| `[class.*.verify]` | extra_criteria | llm / judges / policy / max_repair_rounds |
| `[class.*.extract]` | instruction（v1.8 增） | llm / include_diff / on_error——LLM 绑定与失败策略属部署与成本面（与 quality 行同理） |
| —— | —— | run.* / input.* / stream.*（v1.8）/ dedup.* / segment.*（v1.8）/ stitch.*（v1.9）/ classify.* / output.*（含 schema 与 validator——输出 Schema 全局唯一）/ trace.* 全部不可按类 |

v1.8 注：`segment.*` 不入白名单是**链序因果**而非取舍——链序为 segment → stitch → dedup → classify → extract →…（3.10.3），segment 在 classify **之前**执行，成段时类标签尚不存在，「按类分段」无从谈起；extract 在 classify 之后，故其 `instruction` 可按类覆盖（multi 扇出下兄弟信封各按其标签的有效 instruction 摘取，S9，3.15）。v1.9 注：`stitch.*` 不入白名单同为链序因果——stitch 亦在 classify 之前（3.10.3），`[class.<name>.stitch]` 不存在（3.1.4 线索缝合行）。

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

## 5.3 Rubric 结构（内联或默认包文件，同一 TOML 结构）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `rubric.name` | str | 必填 | rubric 标识，入 _meta 与报告。 |
| `rubric.criteria[].key` | str | 必填 | `[a-z0-9_]+`，全局唯一。 |
| `rubric.criteria[].weight` | float | 1.0 | 聚合权重（>0）。 |
| `rubric.criteria[].description` | str | 必填 | 准则含义（进入两种模式的提示词）。 |
| `rubric.criteria[].pairwise_prompt` | str | 必填 | 成对比较问句，如「哪段文本的写作水平更高？」。 |
| `rubric.criteria[].pointwise_levels` | array[6] | pointwise 必填 | 0–5 六级加性描述（附录 A 示例）。 |

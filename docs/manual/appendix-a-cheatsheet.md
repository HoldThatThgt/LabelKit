# 附录 A　全参数速查表

> 按文件、按节列出全部配置键：**默认值加粗处即需要特别注意的语义**。
> 详解章节在最右列。CLI 参数与退出码见第 15 章。

## A.1 config.toml

| 键 | 类型 | 默认 | 一句话 | 章 |
|---|---|---|---|---|
| `schema_version` | int | 必填=1 | 配置格式版本 | 6 |
| `tool.log_level` | str | "info" | stderr 级别（debug/info/warn/error），被 --log-level 覆盖 | 6 |
| `tool.log_format` | str | "text" | "jsonl" 供采集系统；**同时强制 console plain 档**（显式 rich 不可覆盖，WARN 一次） | 6/16 |
| `console.mode` | str | "auto" | v1.10 进度显示三档 "auto" \| "rich" \| "plain"，被 CLI `--console` 覆盖；auto = TTY ∧ text ∧ TERM 非 dumb/空 ∧ rich 可导入取 rich，否则 plain（NO_COLOR 不参与——剥色保布局）；解析产物 `mode_resolved` 为内部字段 | 6/16 |
| `console.refresh_hz` | int | 5 | rich 画布重绘频率，**1–10 越界即配置错误** | 6/16 |
| `console.heartbeat_s` | int | 0 | 仅 plain 且非 TTY：每 N 秒一行数据无关心跳；**0=关（默认），<0 即配置错误** | 6/16 |
| `console.estimate` | bool | false | 仅文本模态：多读一遍输入换批总数分母 + ETA；UI 模态恒有分母、本键无效 | 6/16 |
| `console.interactive` | bool | true | rich ∧ stdin TTY ∧ termios 可用时启用键盘开关（`? l e + - p q`；h=?）；false=纯渲染 | 6/16 |
| `llm.<name>` | table | ≥1 个 | LLM profile，name 被 project 引用 | 6 |
| `llm.*.provider` | str | 必填 | "openai_compatible" \| "anthropic" | 6 |
| `llm.*.base_url` | str | 必填 | API 根地址（不带 /chat/completions） | 6 |
| `llm.*.model` | str | 必填 | 模型名，原样透传 | 6 |
| `llm.*.api_key_env` | str | 必填* | 密钥的**环境变量名**（被引用才检查存在性）；* v1.6 起与 `api_key_envs` **恰设其一** | 2/6 |
| `llm.*.api_key_envs` | array | 不设 | v1.6 密钥池：环境变量名数组，与 `api_key_env` **互斥（恰设其一）**；池内共享该 profile 其余字段（同 base_url、同 model），被引用时**每个**变量都须存在非空 | 6/17 |
| `llm.*.max_concurrency` | int | 8 | 并发信号量（该 profile 全部调用共享；**密钥池仍是全池总在途上限**） | 6/17 |
| `llm.*.timeout_s` | int | 120 | 单请求超时；超时可重试 | 6 |
| `llm.*.max_retries` | int | 5 | 可重试错误（网络/408/409/429/5xx）上限 | 6 |
| `llm.*.retry_base_delay_s` | float | 1.0 | 全抖动退避基数：random(0, 基数×2^i)，封顶 60s | 6 |
| `llm.*.supports_structured_output` | bool | false | true 启用结构引擎的结构化输出层；**模型不支持别乱填** | 6/14 |
| `llm.*.supports_vision` | bool | false | **UI 模态引用者必须 true（启动校验）** | 6 |
| `llm.*.max_output_tokens` | int | 4096 | 太小→输出截断→**v1.11 终局化为记录级拒收 `output_truncated`（不进修复环）**；声明 context_window 后还整段挤占输入预算 | 6/14/17 |
| `llm.*.context_window` | int | 0 | v1.11 上下文预算：**0=未声明=预算关**（被启用阶段引用时 WARN 一次）；>0 保证每次调用 est(输入)+max_output_tokens+margin ≤ 窗口（margin=max(256, ⌈10%×窗口⌉)），装不下的记录按 context_overflow 记录级拒收；须 > max_output_tokens+margin 否则预算非正=配置错误；**声明部署实效窗口、欠声明恒安全** | 6/17/18 |
| `llm.*.temperature` | float | 0.0 | profile 级默认；生成阶段由 generate.temperature 覆盖 | 6 |
| `llm.*.max_image_px` | int | 2048 | 图像长边上限，超出等比缩小；v1.11 升格：**升级天花板 + provider 像素制硬限制域**（日常发送尺寸看 default_image_px 工作点） | 6/21 |
| `llm.*.default_image_px` | int | 0 | v1.11 图片采样默认工作点（长边 px）：**0=沿用 max_image_px**；>0 须 ≤ max_image_px（违反=配置错误）；verify 修复换档可逐档上探至 max_image_px | 6/13 |
| `llm.*.price_per_mtok_in/_out` | float | 不设 | 配了才有 est_cost_usd | 6/17 |
| `embedding.<name>` | table | 可选 | 语义去重的 embedding profile | 6/9 |
| `embedding.*.provider` | str | "openai_compatible" | **唯一取值**；POST {base_url}/embeddings | 6 |
| `embedding.*.base_url/model/api_key_env` | str | 必填 | 同 LLM profile | 6 |
| `embedding.*.api_key_envs` | array | 不设 | v1.6 密钥池，机制同 `llm.*.api_key_envs`（与 `api_key_env` 恰设其一） | 6/17 |
| `embedding.*.max_concurrency/timeout_s/max_retries/retry_base_delay_s` | — | 8/60/5/1.0 | 同一套重试限流机制 | 6 |
| `embedding.*.dims` | int | 不设 | 设了则校验返回维度，不符判致命 | 6 |
| `embedding.*.context_window` | int | 0 | v1.11：同 llm profile 声明制（**0=未声明=预算关**）；>0 预算 = context_window − margin（无输出预留），embed 输入超预算按确定性**头部保留**截断 | 6/9 |

## A.2 project.toml — [run] / [input]

| 键 | 默认 | 一句话 | 章 |
|---|---|---|---|
| `schema_version` | 必填=1 | — | 7 |
| `run.input` | process 必填 | 输入路径；**generate_only 必须不设**；--input 可覆盖 | 5/7 |
| `run.output` | 必填 | 主输出路径；其余产物同目录派生；--output 可覆盖 | 7/8 |
| `run.modality` | 必填 | "text" \| "ui" | 5/7 |
| `run.mode` | "process" | \| "generate_only"（要求 generate 开；三形态：种子池 / 无种子 / **时间流生成**（v1.13，`[generate.stream]`，A.13）） | 7/12/27 |
| `run.batch_size` | 256 | 批大小 = **pairwise 比较池大小（质量口径参数）** | 7/10 |
| `run.seed` | 0 | 全部随机行为的种子；同 seed 可复现 | 7 |
| `run.fatal_error_threshold` | 20 | 熔断：**连续**致命 API 错误数达标 ⇒ 退出码 4（401/403 认证类首错即熔断，不计连续数；重试耗尽也计窗） | 7/17 |
| `run.max_park_s` | 3600 | v1.6 驻留上限：所引 profile **全部存活密钥均在冷却**时，单次逻辑调用累计等待秒数上限，超限按重试耗尽处理（记录 failed、计入熔断窗）；**0=不驻留，单密钥 profile 下任何 429 都立即失败**，仅建议多密钥池设 0 | 7/17 |
| `input.text_field` | "text" | 正文字段点路径；**写错=全员坏行** | 5 |
| `input.on_bad_line` | "skip" | \| "fail"（退出码 3） | 5 |
| `input.on_missing_pair` | "skip" | UI 缺对策略 | 5 |
| `input.on_index_conflict` | **"fail"** | UI 同号多文件；默认就退出 | 5 |
| `input.max_image_mb` | 20 | 单图上限，超限跳过 | 5 |
| `input.ui_tree_max_chars` | 30000 | 树序列化进提示词的**绝对上限**；v1.11：所引 profile 声明 context_window 后按预算份额动态收缩（按行丢尾、truncated 标记保留） | 5/11 |

## A.3 project.toml — [dedup]

| 键 | 默认 | 一句话 | 章 |
|---|---|---|---|
| `enabled` | **true** | — | 9 |
| `scope` | "global" | \| "batch"（省内存，跨批漏检） | 9/17 |
| `minhash_threshold` | 0.85 | 近似判重 Jaccard 线；短文本可降、模板文本宜升 | 9 |
| `minhash_num_perm` | 128 | 签名精度 | 9 |
| `ngram` | 5 | 字符 shingle 宽度；短文本可降到 3 | 9 |
| `image_phash_max_distance` | 8 | 64-bit pHash 汉明距离阈值 | 9 |
| `ui_dup_requires` | "both" | \| "tree" \| "image"；both 防误杀同模板界面 | 9 |
| `bounds_quantize_px` | 4 | 树坐标量化粒度（抗渲染抖动） | 9 |
| `semantic` | false | 判重语义层开关（要花 embedding 钱） | 9 |
| `semantic_embedding` | semantic=true 必填 | 引用 [embedding.*] profile 名 | 9 |
| `semantic_threshold` | 0.95 | 余弦相似度判重线 | 9 |

## A.4 project.toml — [quality]

| 键 | 默认 | 一句话 | 章 |
|---|---|---|---|
| `enabled` | **true** | 与 annotate 至少开一个 | 10 |
| `mode` | "pairwise" | 批内相对两两比较 \| "pointwise" 绝对刻度 | 10 |
| `llm` | "default" | 单评审时的裁决 profile | 10 |
| `rounds` | 4 | pairwise 轮数 k（每记录被比较 k 次，调用 ≈ N·k/2） | 10 |
| `criteria_per_call` | "all" | 一次裁决全部准则 \| "single" 每准则一问（×C 成本） | 10 |
| `threshold` | 不设 | 聚合分过滤线 [0,1]；**不设=只打分不过滤**；pairwise 下是批内百分位线 | 10 |
| `selection` | "threshold" | \| "top_ratio"；**两机制互斥** | 10 |
| `top_ratio` | selection=top_ratio 必填 | (0,1]，批内保留 ceil(ratio×**已打分**存活数) 条；selection 为 threshold 时设置无效（启动打 warning） | 10 |
| `judges` | [] | 评审团（奇数个 profile 名）；非空**替代** quality.llm；成本× | 10 |
| `both_orders` | false | 正反双顺序一致才记胜负；成本 ×2 | 10 |
| `on_unscored` | "keep" | 全部比较失败的记录去留；keep 不占 top_ratio 名额 | 10 |
| `rubric` | 按模态自动 | "default:text" \| "default:ui" \| "default:trajectory"（v1.8 轨迹四准则）\| "inline"（须配 [[rubric.criteria]]）；缺省按模态选，**`segment` 开启∨`generate.stream` 开启时缺省解析为 default:trajectory**（v1.13 扩展：两者打的都是序列的分） | 10/B/27 |
| `judgment_reasons` | "auto" | 裁决附理由；auto=开了 quality trace 才要 | 10/16 |
| `rubric.name` / `criteria[].key/weight/description/pairwise_prompt/pointwise_levels[6]` | — | 内联 rubric 结构 | 7/10 |

## A.5 project.toml — [generate]

| 键 | 默认 | 一句话 | 章 |
|---|---|---|---|
| `enabled` | false | 仅 text 模态；process 下要求 quality 开 | 12 |
| `llms` | ["default"] | profile 名数组；每次调用选 1 个 | 12 |
| `mixture` | "round_robin" | \| "weighted"（配 weights） | 12 |
| `weights` | [] | weighted 必填：正数、长度=len(llms) | 12 |
| `instruction` | enabled 必填 | 生成指令（收放心法见 12.7） | 12 |
| `num_per_record` | 2 | 每种子期望产出条数 | 12 |
| `seeds_per_call` | 3 | 每次调用抽几条种子当示例；v1.11：声明预算后为**上限**——超预算按抽样序从尾部确定性丢弃，min 1 | 12 |
| `num_per_call` | 4 | 每次调用要求产出条数 | 12 |
| `seed_min_score` | 自动 | 种子门槛：默认 quality.threshold，再缺省批中位数 | 12 |
| `temperature` | 0.9 | 生成温度（覆盖 profile 默认） | 12 |
| `sample_validator` | 不设 | 样本级代码回调 "module:function"：过滤语义，剔除计入桶 rejected_by_validator | 12 |
| `seed_examples` | [] | generate_only 种子池形态（process 不得设；**时间流形态显式书写 = 定向配置错误**） | 12/22/27 |
| `standalone_count` | 不设 | generate_only 无种子形态目标条数（与 seed_examples 互斥；process 不得设；**时间流形态同上禁设**） | 12/22/27 |
| `sequences` | 0 | v1.13 时间流形态：**全局默认**序列配额（按类 `[class.*.generate].sequences` 覆盖；至少一类有效值 ≥ 1） | 27 |
| `len_range` | [3, 6] | v1.13 时间流形态：**全局默认**序列长度区间 [lo, hi]（1 ≤ lo ≤ hi；按类覆盖） | 27 |
| `[[generate.styles]]` | [] | 风格子表 {name, prompt}；每调用均匀抽 1 个追加为 [风格要求]（时间流形态：每序列预抽，作用于帧实现与噪音调用，蓝图不带风格） | 12/27 |

## A.6 project.toml — [annotate] / [verify]

| 键 | 默认 | 一句话 | 章 |
|---|---|---|---|
| `annotate.enabled` | **true** | — | 11 |
| `annotate.llm` | "default" | UI 模态须 supports_vision | 11 |
| `annotate.instruction` | enabled 必填 | 写法指南 11.4 | 11 |
| `annotate.examples` | [] | few-shot {input, output}；output 启动时过 Schema 校验 | 11 |
| `annotate.self_consistency` | 0 | 0=关；≥3 奇数：n 次采样字段级投票，成本 ×n | 11 |
| `annotate.sc_temperature` | 0.7 | 自洽采样各次采样温度（多样性来源） | 11 |
| `annotate.sequence_frames` | 20 | v1.8 序列标注单请求最大关键帧数，∈ **[2, 100]**；超员按等距降采样（首末帧恒含）；**>20 且所引 profile max_image_px>2000 ⇒ WARN**（Anthropic 多图请求硬拒）；非 stream 显式设置 ⇒ no-op warning；v1.11：升格为**上限**——声明 context_window 后实际帧数 k_eff 按图片预算收缩（首末帧恒保留，min 2） | 11/25 |
| `verify.enabled` | false | 开则要求 annotate 开 | 13 |
| `verify.llm` | "judge" | enabled 且 judges 为空时须存在于 [llm.*]（judges 非空即被替代、免校验）；建议独立于标注模型 | 13 |
| `verify.judges` | [] | 评审团（奇数个）；非空替代 verify.llm | 13 |
| `verify.policy` | "drop" | \| "repair"（意见回喂重标，唯一改写标注的路径） | 13 |
| `verify.max_repair_rounds` | 1 | repair 轮数上限 | 13 |
| `verify.extra_criteria` | "" | 追加评审维度自由文本 | 13 |

## A.7 project.toml — [output] / [trace]

| 键 | 默认 | 一句话 | 章 |
|---|---|---|---|
| `output.schema_path` / `schema_inline` | 恰一 | 用户 Schema（draft 2020-12，顶层 object，禁 _meta）；v1.13 起可被 `[class.<名>.annotate].schema_*` **按序列类整份覆盖**（未声明的类回落此处；「恰一」不因覆盖而豁免，A.9） | 14/27 |
| `output.max_repair_attempts` | 2 | 结构引擎 LLM 修复环轮数预算 | 14 |
| `output.repair_llm` | 同调用方 | 修复专用 profile（可指便宜小模型） | 14 |
| `output.validator` | 不设 | 代码回调校验层（"module:function"）：业务级硬校验，违规回喂修复环；启动校验含 few-shot 干跑 | 14 |
| `output.meta_mode` | "inline" | \| "sidecar"（{stem}.meta.jsonl）\| "none"（丢分数溯源，不推荐） | 8 |
| `output.passthrough_fields` | [] | 输入字段透传至 _meta.source.fields | 8 |
| `output.rejects` | "refs" | "none" \| "refs"（无数据内容）\| "full"（含原文=数据副本） | 8 |
| `trace.enabled` | false | 事件流开关 | 16 |
| `trace.path` | {stem}.trace.jsonl | 首个事件写出时截断（死于启动校验的运行不再触碰；dry-run 写 `{名}.dryrun{后缀}` 独立文件） | 16 |
| `trace.channels` | ["quality","verify","schema"] | 十一通道：ingest/segment（v1.8）/stitch（v1.9）/dedup/classify（v1.7）/extract（v1.8）/quality/annotate/verify/schema/llm；默认值不变，分类/分段/缝合/摘取判决须显式订阅对应通道 | 16/24/25/26 |
| `trace.content` | "refs" | none→refs→excerpt→full 四档脱敏；full=完整数据副本 | 16 |

## A.8 组合约束（启动即查，违反=退出码 2）

1. `annotate` 与 `quality` 至少启用一个
2. `verify` ⇒ `annotate`
3. `generate` ⇒ modality="text"；process 模式下另 ⇒ `quality`
4. `generate_only` ⇒ `generate.enabled` 且 `run.input` 缺省
5. `quality.threshold` ⨯ `selection="top_ratio"` 互斥
6. `generate_only` ⇒ `seed_examples` 与 `standalone_count` **恰好设置其一**（同时设置或均缺省都报错）；process 模式下两键均不得设置。**例外**：`generate.stream.enabled = true`（v1.13 时间流形态）时这条规则不适用——配额由 `[class.*.generate].sequences` 承载，两键显式书写反而是定向配置错误（A.8 第 33 条）
7. judges 数组非空须奇数且成员存在于 [llm.*]
8. UI 模态被引用的 LLM profile 须 `supports_vision=true`
9. `weighted` ⇒ weights 正数且长度=len(llms)
10. `self_consistency` ∈ {0} ∪ {≥3 奇数}
11. `dedup.semantic = true` ⇒ `semantic_embedding` 必填，且引用的 profile 名须存在于 config.toml `[embedding.*]`
12. `output.validator` / `generate.sample_validator` ⇒ 须为可导入、可调用的 `"module:function"`；前者还须让全部 few-shot 示例 output 干跑通过
13. `classify.enabled = true` ⇒ `[[classify.classes]]` ≥ 2 项，且 `classify.fallback_class` 必填并 ∈ classes（v1.7）
14. `classify.max_labels` 仅 `assignment = "multi"` 可设，∈ [2, 类别数]（缺省回填为类别数）
15. `classify.enabled = false` 而 `[[classify.classes]]` / `[class.*]` 在场 ⇒ 仅 **warning**（一次、点名被忽略的表——「留配置、关开关」合法，不触发退出码 2）
16. `segment.enabled = true` ⇒ `run.mode = "process"` ∧ `generate.enabled = false` ∧ `annotate.enabled = true`（v1.8）
17. `extract.enabled = true` ⇒ `segment.enabled = true` ∧ `run.modality = "ui"`（v1.8）
18. 流模式的视觉必需校验逐阶段（v1.8）：`extract.llm` **恒**须 supports_vision；`segment.llm` **恒不入视觉必需集**（v1.11——窗口是否附图由所引 profile 的 `supports_vision` 自动推导为解析产物 `vision_resolved`，原 `use_vision` 键已移除，见 A.8 第 22 条）；`quality.llm` **免除**、`stitch.llm`（v1.9）**恒免除**（两者都是纯文本判定）；v1.12 帧粒度分列：`frame.annotate.llm` ui ∧ enabled 时**恒**须 supports_vision（截图是帧标注主证据），`frame.classify.llm` **恒不入视觉必需集**（附图与否自动推导，同 segment 形制——省钱面 = 指向纯文本 profile）；`stream.session_max_span_s` 设 > 0 要求 `order_by = "meta:*"`（时间跨度需时间序键，违反即配置错误；meta:* 仅文本模态）；`stream.gap_s` 显式设置而 `order_by` 非 meta:* ⇒ 仅 **warning**（值照常加载、时间差断开不会生效；默认值 300 不视作显式意图，不告警）
19. `[stream]` / `[segment]` / `[stitch]`（v1.9）/ `[extract]` 任一节在场而 `segment.enabled = false` ⇒ 仅 **warning**（同 A.8 第 15 条形制）；`segment.window` ≥ 2；`annotate.sequence_frames` ∈ [2, 100]
20. `stitch.enabled = true` ⇒ `segment.enabled = true`（v1.9）；启用时 `stitch.llm` 计入密钥/probe/存在性引用集但**不入视觉必需集**；`[class.<name>.stitch]` 不存在（链序在 classify 之前，类标签尚不存在）
21. `stitch.votes` 须为 ≥1 的**奇数**（偶数 = 退出码 2，v1.9）；`stitch.enabled = true` ∧ `segment.strategy = "rules"` ⇒ 仅 **warning**（规则分段不做语义精化，可缝证据薄）；`[stitch]` 带非开关键而 stitch 关、segment 开 ⇒ 仅 **warning**（segment 也关时并入 A.8 第 19 条名单）
22. `[segment]` 内显式出现 `use_vision` ⇒ **配置错误**（v1.11 移除键定向报错，**不走**「未知键仅 warning」兜底）：窗口是否附图由 `segment.llm` 所指 profile 的 `supports_vision` 自动决定；需纯文本裁决请把 `segment.llm` 指向纯文本 profile
23. 上下文预算硬校验（v1.11）：`context_window` > 0 时须 > `max_output_tokens + margin`（margin = max(256, ⌈0.10 × context_window⌉)），否则预算非正 ⇒ 配置错误；`default_image_px` > 0 时须 ≤ `max_image_px`；静态系统侧预检——启用阶段的静态 prompt 部件（模板 + instruction + rubric/类表/Schema/few-shot）估算 ≥ 该 profile 输入预算 ⇒ 配置错误（任何记录都装不下）；segment 装填护栏——最坏保证装填量 `w_min` < 下限（`verify.enabled ∧ verify.policy="repair" ∧ segment.enabled` 时为 3，否则 2）⇒ 配置错误
24. 上下文预算 warning（v1.11）：被启用阶段所引 profile 未声明 `context_window` ⇒ WARN 一次（提示可声明）；静态系统侧部件估算 > 预算 50% ⇒ WARN（单记录可用空间预警）；`w_min` == 下限 ⇒ WARN（窗数放大警示）；`vision_resolved` ∧ `segment.window` > 20 ∧ 所引 profile `max_image_px` > 2000 ⇒ WARN（Anthropic 多图硬拒域，sequence_frames 那条 WARN 的姊妹）
25. `frame.classify.enabled` / `frame.annotate.enabled` 任一为 true ⇒ `segment.enabled = true`（v1.12 帧粒度仅流模式，报错文案指引非流工程改用 classify + `[class.<名>.annotate]`）**且** `output.meta_mode ≠ "none"`（帧产物仅经 `_meta.stream.members` 承载；sidecar 合法）
26. `frame.classify.enabled = true` ⇒ `frame.classify.fallback_class` 必填并 ∈ `[[frame.classify.classes]]`（传递性要求帧类表非空；**无独立的 ≥2 类数规则**——与 A.8 第 13 条的序列类表有意不同；两张类表相互独立、允许重名、互不约束）
27. `frame.annotate.enabled = true` ⇒ `frame.annotate.instruction` 必填 ∧ `schema_path` / `schema_inline` **恰一**（帧级 Schema 独立于 `output.schema`：draft 2020-12 元校验 + examples 干跑走同一套分支）
28. `[frame.class.<名>]` 在场 ⇒ `frame.classify.enabled = true` **∨ `generate.stream.enabled = true`**（v1.13 放宽）∧ 节名 ∈ 帧类表；覆盖节白名单：帧分类形态下仅 `annotate` 一节（键仅 instruction / examples / enabled），时间流生成形态下仅 `generate` 一节（键仅 instruction / schema_path / schema_inline，A.13）——白名单外键/节 ⇒ 配置错误，同 A.9 的显式例外形制
29. `[frame.classify]` 显式出现 `assignment`、`[frame.annotate]` 显式出现 `self_consistency` ⇒ **定向配置错误**（帧级无多标签、无自洽采样；同 A.8 第 22 条的定向探针形制）；`[frame.*]` 节在场 ∧ 均未启用 ∧ `segment.enabled = false` ∧ `generate.stream` 关 ⇒ 仅 **warning**（并入 A.8 第 19 条名单，v1.12/v1.13）
30. `generate.stream.enabled = true` ⇒ **形态前提合取**（v1.13，缺一即配置错误）：`run.mode = "generate_only"` ∧ `run.modality = "text"` ∧ `generate.enabled` ∧ `classify.enabled` ∧ `stream.order_by = "meta:<字段>"` ∧ `output.meta_mode ≠ "none"`；同时 `frame.classify.enabled` / `frame.annotate.enabled` 必须为 false（帧类真值生成期已知，显式开启 = 定向配置错误）
31. 时间流形态的**类表放宽与配额**（v1.13）：序列类表 **≥1 项**（放宽自 A.8 第 13 条的 ≥2）∧ `fallback_class` **免填**（写了仍须 ∈ 类表）∧ 至少一个类的有效 `sequences ≥ 1` ∧ 参与生成的类 `instruction` 非空；帧类表非空 ∧ **每个**帧类都有非空的 `[frame.class.<名>.generate].instruction`（蓝图 enum 覆盖全类表）；`classify.llm` 豁免密钥/probe 引用集（零判决调用）
32. 时间流形态的**装箱与织造**（v1.13）：`sessions ≥ 1` ∧ `sessions ≤ Σsequences ≤ 2 × sessions`（交叉并发度恒 k ∈ {1,2}）；`duplicates ∈ [0, Σsequences]`；`noise_ratio ∈ [0,1)`（> 0 ⇒ `noise_instruction` 非空）；`frame_gap_s`：`0 < lo ≤ hi < stream.gap_s`，且 **`lo ≥ 1e-6`（v1.14 微秒地板**——亚微秒间隔被时间戳精度取整成 0，破坏 ts 严格递增，也让词表的 0.0 边界失去意义）；`2 × max(各类 len_range 上界) ≤ stream.session_max_len`；`stream.key == []` ∧ `stream.gap_steps == 0`（定向配置错误——会话边界由交织器铺设）；`session_max_span_s > 0` ⇒ `(session_max_len − 1) × frame_gap_s 上界 ≤ session_max_span_s`；`ts_start` 须可解析为 ISO-8601
33. 时间流形态的**禁设键探针**（v1.13，定向配置错误、不走「未知键仅 warning」）：`[generate]` 的 `seed_examples` / `standalone_count` / `num_per_record` / `seeds_per_call`；`[class.*.generate]` 的 `num_per_record` / `seeds_per_call`（配额改用 `sequences`、长度改用 `len_range`）
34. **帧类构成档位**（v1.14，`[[generate.stream.tiers]]`，A.13）：本表在场 ⇒ `generate.stream.enabled = true`（定向配置错误——仅时间流形态合法）；`tier_rank` 正整数、表内唯一、**全表连续覆盖 1..N**（N = 表长；缺号/重号即错）；`weight` 整数 ≥ 1；`frame_classes` 非空、表内无重复、每名 ∈ `[[frame.classify.classes]]`，且**各档构成集合两两互异**（同构成即语义重复）；**长度可覆盖**——逐 (参与类, 档) 配额 ≥ 1 的组合须满足该类 `len_range` 下界 ≥ `len(该档 frame_classes)`（档内每类至少出现一次才装得下；**零配额组合豁免**）。两条 **WARN**（非错误）：某 (类, 档) 按整数域最大余额法配额为 0；某帧类未被任何档收录（其 `[frame.class.<名>.generate]` 整节成为死配置）。附带效力：档位表在场时 A.8 第 31 条「每个帧类都必填 `generate.instruction`」的检查域**收窄为 ∪各档 frame_classes**，未入档帧类免填
35. **时间字段回填**（v1.14，`[frame.class.<名>.generate.time_fields]`，A.13）：本子表仅当该帧类声明了 `schema_path` / `schema_inline`（结构化帧）时合法——纯文本帧带它是定向配置错误；每个绑定键 ∈ 该帧类生成 Schema 的顶层 `properties`；绑定值 ∈ 语义词表闭集 `{ts, gap_prev_s, gap_next_s, elapsed_s}`；**声明类型字面恰等**——`ts` ⇒ 该属性 Schema 写 `"type": "string"`、其余三值 ⇒ `"type": "number"`（联合类型数组、缺 `type`、经 `$ref`/组合关键字间接声明均判不匹配，定向配置错误）；**剔除余量**——顶层 `properties` 键数 − 绑定键数 ≥ 1（全绑定即错，LLM 至少得有一个字段可生成）。一条 **WARN**：绑定字段上带 `type` 以外的约束关键字（minimum/maximum/pattern …）——它们既不上行给模型也不被强制，时间量的值域由时间轴决定

## A.9 project.toml — [classify] 与 [class.<name>.*] 按类覆盖（v1.7 追加）

| 键 | 默认 | 一句话 | 章 |
|---|---|---|---|
| `classify.enabled` | false | 默认关；关闭时与 v1.6 行为一致（唯一可见差异：`_meta.classification` 恒在、值为 null） | 24 |
| `classify.llm` | "default" | profile 引用；UI 模态须 supports_vision；计入密钥/视觉必需/probe 三处引用集 | 24 |
| `classify.assignment` | "single" | 锁定一条一类 \| "multi"（多类命中按标签扇出：**行唯一键变 (_meta.id, label)**，counts 增 fanout） | 24 |
| `classify.max_labels` | 类别数 | 仅 multi 可设；∈ [2, 类别数]；扇出成本（×m 份打分/标注/评审）的封顶旋钮 | 24 |
| `classify.instruction` | "" | 可选补充说明，追加在 system 类别表之后；横跨多类的裁决规则（「拿不准选 X」）写这里 | 24 |
| `classify.fallback_class` | enabled 必填 | 兜底类：须 ∈ classes；分类失败归它，LLM 亦可主动选它 | 24 |
| `classify.self_consistency` | 0 | 0=关；≥3 奇数：n 次采样投票，**无过半归兜底类**（不回退首样本），成本 ×n | 24 |
| `classify.sc_temperature` | 0.7 | 自洽采样各次采样温度；仅 self_consistency ≥ 3 生效 | 24 |
| `classify.on_error` | "fallback" | 结构修复耗尽：归兜底类、记录存活（不写 errors，不污染 rejects 归因）\| "fail"：记录 failed → rejects | 24 |
| `[[classify.classes]]` | enabled 必填 | ≥2 项；每项 {name：`[a-z0-9_]+` 表内唯一, description：非空（LLM 可见的全部类语义）, examples：可选 few-shot（仅输入侧）} | 24 |

`[class.<name>.<节>]` 按类覆盖白名单（`<name>` 须 ∈ classes；未提供的键继承全局；**白名单外键报 CONFIG_ERROR**——「未知键仅 warning」惯例的显式例外）：

| 节 | 可按类覆盖 | 锁定全局 |
|---|---|---|
| `[class.*.quality]` | mode / rounds / rubric（含 `[class.*.rubric]` 内联子表）/ threshold / selection / top_ratio | llm、judges、both_orders、criteria_per_call、on_unscored |
| `[class.*.annotate]` | instruction / examples / **schema_path / schema_inline**（至多其一，v1.13：整份覆盖输出 Schema，缺省回落 `output.schema`） | llm、self_consistency、sc_temperature |
| `[class.*.generate]` | instruction / styles / num_per_record / temperature；**v1.13 时间流形态另加 sequences / len_range**（该形态下 num_per_record / seeds_per_call 反为禁设键） | llms、mixture、weights、seeds_per_call、num_per_call、sample_validator |
| `[frame.class.*.generate]`（v1.13 时间流形态） | instruction / schema_path / schema_inline（后两者至多其一）/ **time_fields 子表**（v1.14 时间字段绑定，仅结构化帧合法——A.8 第 35 条） | 帧内容契约无 llm / 温度键（随所属序列的预抽 profile 与温度） |
| `[class.*.verify]` | extra_criteria | llm、judges、policy、max_repair_rounds |
| `[class.*.extract]`（v1.8） | instruction | llm、include_diff、on_error |
| —— | —— | `run.*` / `input.*` / `stream.*`（v1.8）/ `dedup.*` / `segment.*`（v1.8，链序在 classify 之前，类标签尚不存在）/ `classify.*` / `trace.*` / `[output]` 的其余键（meta_mode、rejects、修复预算、validator——运行级契约）从不按类 |

合并细则：优先级 `[class.<name>].<节>.<键>` > `[<节>].<键>` > 内置默认；threshold/selection/top_ratio 按**选择组**整组合并（全局 threshold + 类 top_ratio 合法，互斥校验跑在合并后视图上）；类 rubric 换 selector 后重解析，6 级量表校验按（类有效 mode × 类有效 rubric）执行；类 examples 启动时按**该类的有效 Schema** 干跑（v1.13：类声明了 `schema_*` 就用类的）。详见第 24 章。

## A.10 project.toml — [stream] / [segment] / [extract]（v1.8 追加）

`[stream]` 是输入侧声明（排序与会话化，随 `segment.enabled` 生效）；`[segment]` 是流模式总开关；`[extract]` 仅 UI 序列可开。三节任一在场而 segment 关 ⇒ 仅 warning（A.8 第 19 条）。详见第 25 章。

| 键 | 默认 | 一句话 | 章 |
|---|---|---|---|
| `stream.order_by` | "input_order" | 文本=文件名字典序→行号、UI=配对编号升序 \| "meta:<field>"（**仅文本模态**）：按行内时间戳定序，数值自动判秒/毫秒、ISO 字符串（含 Z）可解析 | 5/25 |
| `stream.on_disorder` | "skip" | 乱序/时间戳解析失败的记录跳过并计数 \| "fail"（退出码 3）；单调性游标按分区键各自维护 | 25 |
| `stream.key` | [] | 分区键列表："meta:<field>"（文本）\| "source_dir"（UI，= 文件父目录）；**键变即断**（groupby 语义，输入须按键成组） | 5/25 |
| `stream.gap_s` | 300 | 相邻记录时间差 > 阈值即断会话；仅 order_by="meta:*" 生效——显式设置而非 meta 序仅 **WARN** 不阻断（A.8 第 18 条）；默认偏大——欠分割可由 LLM 精化拯救、过分割不可逆 | 25 |
| `stream.gap_steps` | 0 | 序号差断会话（0=不启用）；与 gap_s 可并用，任一触发即断 | 25 |
| `stream.session_max_len` | 200 | 会话硬上限（帧）；**> batch_size ⇒ 启动 WARN**（超批会话将被硬切 + session_split 标） | 25 |
| `stream.session_max_span_s` | 0 | 会话时间跨度硬上限（秒，0=不启用）；仅 order_by="meta:*" 可设——非 meta 序设 > 0 即配置错误（A.8 第 18 条） | 25 |
| `segment.enabled` | false | 流模式总开关；默认关 = 行为与 v1.7 逐字节一致（`_meta.stream` 恒在 = null 除外）；启用要求见 A.8 第 16 条 | 25 |
| `segment.strategy` | "hybrid" | "rules"（候选会话原样成 episode，零 LLM；noise_filter/min_len 不生效）\| "llm" \| "hybrid"（单帧会话自动走 rules 退化） | 25 |
| `segment.llm` | "default" | **仅 strategy ∈ {llm, hybrid} 时**计入密钥/probe/存在性三处引用集——rules 零调用不强制配键；v1.11 起**恒不入视觉必需集**：窗口是否附图由所引 profile 的 `supports_vision` 自动推导（解析产物 `vision_resolved`；原 `use_vision` 键已移除，显式出现即配置错误——A.8 第 22 条） | 25 |
| `segment.window` | 20 | 单窗帧数**上限**（v1.11 语义），**≥ 2**；声明 context_window 后按预算贪心装填（溢出即封窗，实际每窗 ≤ window），未声明为固定窗（v1.10 行为）；两形态均重叠 1 帧、接缝帧判决归后窗；窗 ≥ 会话长且预算装得下时天然退化为整段单调用 | 17/25 |
| `segment.digest_max_chars` | 400 | 单帧文字摘要长度上限 | 25 |
| `segment.noise_filter` | true | 逐帧噪声标记（判噪帧 → dropped_noise）；仅 llm/hybrid 生效——rules 下设 true 为 no-op warning | 25 |
| `segment.min_len` | 2 | 段最短帧数；**仅作用于 LLM 精化切出的段**（规则层孤帧/短会话不受约束）；被弃帧 reason="below_min_len"（≠ noise），独立计数 | 25 |
| `segment.context` | "" | 可选域上下文注入判据模板；**非边界定义**——边界判据内置，零配置可用 | 25 |
| `segment.on_error` | "keep" | 单窗修复耗尽：该会话整体成一个 episode 存活 + 留痕 `_meta.stream.degraded`（**不写记录 errors**）\| "fail"（会话成员全部 failed → rejects） | 18/25 |
| `extract.enabled` | false | 启用要求 `segment.enabled` ∧ `modality="ui"`（A.8 第 17 条；文本序列 v1 不适用） | 25 |
| `extract.llm` | "default" | **恒**计入四处引用集且**恒**须 supports_vision（每转移一请求 2 图） | 25 |
| `extract.instruction` | "" | 摘取补充说明，追加进 system 摘取指令后；`[class.<name>.extract]` 可按类覆盖（白名单仅此键） | 24/25 |
| `extract.include_diff` | true | `[树变更摘要]` 注入开关（结构化树 diff 证据）；可关做 A/B 消融对比摘取质量 | 17/25 |
| `extract.on_error` | "fallback" | 单转移修复耗尽：该步记 action_type="other" 留痕（episode 存活，**不写记录 errors**）\| "fail"（episode failed → rejects） | 18/25 |

## A.11 project.toml — [stitch]（v1.9 追加）

`[stitch]` 是线索缝合算子（第 26 章）：把同会话内被穿插切开的 episode 碎片保守缝合成线索。启用要求 `segment.enabled = true`（A.8 第 20 条）。

| 键 | 默认 | 一句话 | 章 |
|---|---|---|---|
| `stitch.enabled` | false | 总开关；关闭时主输出/rejects/report 与 v1.8 **逐字节等价**（例外恰两处：dry-run 估算行的 `stitch_calls=0`、缺陷词表恒在的 `wrong_stitch: 0`） | 26 |
| `stitch.llm` | "default" | 判定 profile；证据是摘要卡（纯文本），**恒不要求 supports_vision** | 26 |
| `stitch.max_open` | 4 | 开放线索池容量（≥1；实证锚：桌面日志挂起窗口均值 3 + 1 条活跃）；穿插深的流上调 | 26 |
| `stitch.bias` | "conservative" | 并入需 LLM 判 resume **且**机械先验命中（合取）\| "llm" 纯 LLM 判（审计/消融用） | 26 |
| `stitch.rescue_short` | true | below_min_len 短段按连续 run 重组先进候选池，命中救援翻回 absorbed；false = v1.8 行为 | 26 |
| `stitch.repass` | true | 有界二遍复评：对一遍结束时的单碎片线索复判，修正贪心漏缝；false = 纯一遍 | 26 |
| `stitch.stale_gap_steps` | 0 | 时间衰减阈值（会话序号差，0=不启用）**双职**：超限先验降格须两腿命中 + 池满逐出优先腿；≠ `stream.gap_steps`（那是断会话规则） | 26 |
| `stitch.digest_max_chars` | 400 | 摘要卡内每个帧摘要的截断上限（沿用 segment 同名键语义） | 26 |
| `stitch.context` | "" | 可选域上下文（何为「同一任务」的领域提示）；判据内置于固定模板，零配置可用 | 26 |
| `stitch.votes` | 1 | 判定稳定化采样：1=单调用；>1 须**奇数**（A.8 第 21 条），n 次采样对 (verdict, thread_ref) 严格多数决、分裂回落保守结局；成本 ×n | 26 |
| `stitch.on_error` | "keep" | 判定修复耗尽：episode 候选开新线索存活（留痕不写 errors）\| "fail"（仅 episode 候选 failed → rejects；救援候选恒按未命中处理） | 18/26 |

## A.12 project.toml — [frame.*]（v1.12 追加）

`[frame.classify]` / `[frame.annotate]` 是 classify / annotate 两个算子的**帧粒度**双开关（不是新算子，第 25 章 25.6）：对 episode 的成员帧做批量闭集分类与逐成员按帧类标注，产物挂 `_meta.stream.members[]` 随序列行交付。任一启用要求 `segment.enabled = true` 且 `output.meta_mode ≠ "none"`（A.8 第 25 条）；`[frame.class.<帧类名>.annotate]` 是按帧类覆盖面（A.8 第 28 条）。帧级**没有** `assignment` / `self_consistency` 键（显式书写 = 定向配置错误，A.8 第 29 条），`[frame.classify]` 也没有 `instruction` 键（判决提示词模板内建）。可运行样板是 `examples/mix` 的两个工程：UI 控件树主工程 `project.toml`（屏幕类型帧类表，form_screen 覆盖 / transition 跳过；DeepSeek + z.ai 双端点——帧分类走纯文本 profile、帧标注走 vision profile）与文本姊妹 `project-text.toml`（请求角色帧类表，纯 DeepSeek 最低成本形态）。

| 键 | 默认 | 一句话 | 章 |
|---|---|---|---|
| `frame.classify.enabled` | false | 帧级闭集分类总开关；关闭时 members[] 无 label 键 | 24/25 |
| `frame.classify.llm` | "default" | 批量判决 profile；enabled 时入密钥/probe/预算引用集，**永不入视觉必需集**（附图与否由所引 profile 的 supports_vision 自动推导——省钱面 = 指向纯文本 profile，判决仅凭摘要行） | 25 |
| `frame.classify.fallback_class` | enabled 必填 | 兜底帧类：须 ∈ 帧类表（修复穷尽/整窗失败的成员归它，episode 永不因此 failed） | 25 |
| `[[frame.classify.classes]]` | enabled 必填 | 帧类表，与 [[classify.classes]] 同构（{name：`[a-z0-9_]+`, description, examples 可选}）；与序列类表**相互独立、允许重名、互不约束**（A.8 第 26 条） | 24/25 |
| `frame.annotate.enabled` | false | 帧级标注总开关；关闭时 members[] 无 annotation / status 键 | 25 |
| `frame.annotate.llm` | "default" | 逐成员标注 profile；ui 模态启用时**无条件入视觉必需集**（截图是标注主证据，镜像序列级 annotate） | 25 |
| `frame.annotate.instruction` | enabled 必填 | 全局帧标注指令；开帧分类后可被 `[frame.class.*.annotate]` 按类覆盖 | 11/25 |
| `frame.annotate.examples` | [] | 可选 few-shot {input, output}；output 启动时过**帧级 Schema** 干跑校验 | 11/25 |
| `frame.annotate.schema_path` / `schema_inline` | enabled 时恰一 | 帧级输出 JSON Schema，**独立于 output.schema**；帧标注调用走结构引擎完整四层、**无代码回调校验层**、不计 resolved_at；失败落 members[] 状态位而非 rejects | 14/25 |
| `[frame.class.<帧类名>.annotate].instruction` | 继承全局 | 按帧类覆盖标注指令（类覆盖 > 全局） | 24/25 |
| `[frame.class.<帧类名>.annotate].examples` | 继承全局 | 按帧类覆盖 few-shot；同样过帧级 Schema 干跑 | 24/25 |
| `[frame.class.<帧类名>.annotate].enabled` | true | false ⇒ 该帧类成员跳过帧标注（members[] 呈现 status="skipped"——省成本面） | 24/25 |

## A.13 project.toml — [generate.stream] 与帧内容契约（v1.13 追加，v1.14 增档位与时间字段两张子表）

`[generate.stream]` 是 generate 算子的**第三形态**（不是新算子，第 27 章）：`generate_only` 下从零合成一条带时间戳的多会话流——LLM 只做蓝图与帧实现两类内容调用，会话装箱/交叉/噪音/重发/时间戳由零 LLM 的机械交织器铺设，产物是主输出（一行 = 一条序列）+ 时间流工件 `{输出名}.stream.jsonl`（一行 = 一帧，可当输入重放）。启用要求见 A.8 第 30–35 条。默认关，全关时全系统与 v1.12 **字节等价**。可运行样板：`examples/synth-stream`（自含单端点 DeepSeek `config.toml`，含 v1.14 的两档档位表与一处时间字段绑定）。

| 键 | 默认 | 一句话 | 章 |
|---|---|---|---|
| `generate.stream.enabled` | false | 形态总开关；开启即触发 A.8 第 30 条的六项前提合取 | 27 |
| `generate.stream.sessions` | 0 | 会话数（≥ 1）；**交叉会话数 = Σsequences − sessions**，故须 `sessions ≤ Σsequences ≤ 2 × sessions` | 27 |
| `generate.stream.noise_ratio` | 0.0 | 噪音帧 / **任务帧** 比例 ∈ [0,1)；帧数 = round(比例 × 任务帧数)；> 0 ⇒ `noise_instruction` 必填 | 27 |
| `generate.stream.noise_instruction` | "" | 噪音帧生成指令（走既有平面生成模板，按 `num_per_call` 批量装箱） | 27 |
| `generate.stream.duplicates` | 0 | 原样重发的序列条数 ∈ [0, Σsequences]；逐字节同源，**恒落流尾新会话**（判重演示位在重放，不在本跑） | 27 |
| `generate.stream.frame_gap_s` | [5, 60] | 会话内帧间隔均匀采样区间（秒）；`0 < lo ≤ hi < stream.gap_s` | 27 |
| `generate.stream.ts_start` | `"2026-01-01T00:00:00Z"` | 时间流起点（ISO-8601，**恒不取墙钟**——同 seed 双跑工件才可能逐字节一致） | 27 |
| `[frame.class.<帧类名>.generate].instruction` | 每个帧类必填 | 该帧类的内容契约（蓝图 enum 覆盖全类表，任一帧类都可能被选中；**开档位表后必填域收窄为 ∪各档构成**，A.8 第 34 条） | 27 |
| `[frame.class.<帧类名>.generate].schema_path` / `schema_inline` | 至多其一 | 有 = **结构化帧**（帧内容是对象，按规范化 JSON 落工件行的文本字段；被逐位包进实现调用的 `prefixItems`，故不写顶层 `$schema`）；无 = 纯文本帧 | 14/27 |

**v1.14 追加两张子表**（各自独立、均默认不在场；双双缺省时产物与 v1.13 逐字节相同）：

| 键 | 默认 | 一句话 | 章 |
|---|---|---|---|
| `[[generate.stream.tiers]].tier_rank` | 本表在场时必填 | 档位身份（正整数、表内唯一、全表**连续覆盖 1..N**；**无 `name` 键**）；同时是配分平票与类内序数分块的确定性排序依据，**工具不赋予序数高低任何质量语义** | 27 |
| `[[generate.stream.tiers]].weight` | 本表在场时必填 | 配额权重（整数 ≥ 1）：类配额按**整数域最大余额法**零抽签配分（基额 = `(sequences × weight) // Σweight`，余额键降序、平票按 tier_rank 升序补位） | 27 |
| `[[generate.stream.tiers]].frame_classes` | 本表在场时必填 | 档位**构成**：该档序列**恰用**这些帧类（蓝图 enum 给「⊆」+ 逐类 `contains` 给「⊇」）；⇒ `members[]` 帧类集合 ≡ 档声明，可反推对账。约束见 A.8 第 34 条（含 `len_range` 下界 ≥ 最大构成大小） | 27 |
| `[frame.class.<帧类名>.generate.time_fields]` | 不设 | **时间字段绑定**（仅结构化帧合法）：键 = 生成 Schema 顶层字段名，值 ∈ 闭集 `{ts, gap_prev_s, gap_next_s, elapsed_s}`。绑定字段从 LLM 面向的逐位 Schema 与契约行**剔除**，值在时间戳铺好后按**本序列相邻成员**的 ts 差机械回填（`round(·, 6)`；首帧 gap_prev_s/elapsed_s = 0.0、末帧 gap_next_s = 0.0；重发帧承源值）。约束与 WARN 见 A.8 第 35 条 | 27 |

配套读法：帧类表复用 `[[frame.classify.classes]]`（`frame.classify.enabled` 保持 false——帧类在生成期即真值）；配额与长度按序列类挂 `[class.<名>.generate].sequences` / `len_range`（A.9 白名单）；标注可按序列类各用一份 Schema（`[class.<名>.annotate].schema_path` / `schema_inline`）；`quality.rubric` 留空自动解析为 `default:trajectory`（A.4）。观测面：`report.run.artifact` 与 `report.generate.stream` 两处按需字段（v1.14 档位表在场时后者增 `tiers` 子块，键位在 `sequences` 之后），**零新 trace 事件、零新错误码**（第 16、18 章）；两个 v1.14 开关都**不改调用数**——`--dry-run` 估算行与八个 golden 逐字节不变。

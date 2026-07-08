# 附录 A　全参数速查表

> 按文件、按节列出全部配置键：**默认值加粗处即需要特别注意的语义**。
> 详解章节在最右列。CLI 参数与退出码见第 15 章。

## A.1 config.toml

| 键 | 类型 | 默认 | 一句话 | 章 |
|---|---|---|---|---|
| `schema_version` | int | 必填=1 | 配置格式版本 | 6 |
| `tool.log_level` | str | "info" | stderr 级别（debug/info/warn/error），被 --log-level 覆盖 | 6 |
| `tool.log_format` | str | "text" | "jsonl" 供采集系统；**同时禁用进度条** | 6/16 |
| `llm.<name>` | table | ≥1 个 | LLM 接入档，name 被 project 引用 | 6 |
| `llm.*.provider` | str | 必填 | "openai_compatible" \| "anthropic" | 6 |
| `llm.*.base_url` | str | 必填 | API 根地址（不带 /chat/completions） | 6 |
| `llm.*.model` | str | 必填 | 模型名，原样透传 | 6 |
| `llm.*.api_key_env` | str | 必填* | 密钥的**环境变量名**（被引用才检查存在性）；* v1.6 起与 `api_key_envs` **恰设其一** | 2/6 |
| `llm.*.api_key_envs` | array | 不设 | v1.6 密钥池：环境变量名数组，与 `api_key_env` **互斥（恰设其一）**；池内共享该档其余字段（同 base_url、同 model），被引用时**每个**变量都须存在非空 | 6/17 |
| `llm.*.max_concurrency` | int | 8 | 并发信号量（该档全部调用共享；**密钥池仍是全池总在途上限**） | 6/17 |
| `llm.*.timeout_s` | int | 120 | 单请求超时；超时可重试 | 6 |
| `llm.*.max_retries` | int | 5 | 可重试错误（网络/408/409/429/5xx）上限 | 6 |
| `llm.*.retry_base_delay_s` | float | 1.0 | 全抖动退避基数：random(0, 基数×2^i)，封顶 60s | 6 |
| `llm.*.supports_structured_output` | bool | false | true 启用结构引擎 L0；**模型不支持别乱填** | 6/14 |
| `llm.*.supports_vision` | bool | false | **UI 模态引用者必须 true（启动校验）** | 6 |
| `llm.*.max_output_tokens` | int | 4096 | 太小→输出截断→修复环烧钱 | 6/14 |
| `llm.*.temperature` | float | 0.0 | 档级默认；生成阶段由 generate.temperature 覆盖 | 6 |
| `llm.*.max_image_px` | int | 2048 | 图像长边上限，超出等比缩小 | 6/21 |
| `llm.*.price_per_mtok_in/_out` | float | 不设 | 配了才有 est_cost_usd | 6/17 |
| `embedding.<name>` | table | 可选 | 语义去重向量档 | 6/9 |
| `embedding.*.provider` | str | "openai_compatible" | **唯一取值**；POST {base_url}/embeddings | 6 |
| `embedding.*.base_url/model/api_key_env` | str | 必填 | 同 LLM 档 | 6 |
| `embedding.*.api_key_envs` | array | 不设 | v1.6 密钥池，机制同 `llm.*.api_key_envs`（与 `api_key_env` 恰设其一） | 6/17 |
| `embedding.*.max_concurrency/timeout_s/max_retries/retry_base_delay_s` | — | 8/60/5/1.0 | 同一套重试限流机制 | 6 |
| `embedding.*.dims` | int | 不设 | 设了则校验返回维度，不符判致命 | 6 |

## A.2 project.toml — [run] / [input]

| 键 | 默认 | 一句话 | 章 |
|---|---|---|---|
| `schema_version` | 必填=1 | — | 7 |
| `run.input` | process 必填 | 输入路径；**generate_only 必须不设**；--input 可覆盖 | 5/7 |
| `run.output` | 必填 | 主输出路径；其余产物同目录派生；--output 可覆盖 | 7/8 |
| `run.modality` | 必填 | "text" \| "ui" | 5/7 |
| `run.mode` | "process" | \| "generate_only"（要求 generate 开） | 7/12 |
| `run.batch_size` | 256 | 批大小 = **pairwise 比较池大小（质量口径参数）** | 7/10 |
| `run.seed` | 0 | 全部随机行为的种子；同 seed 可复现 | 7 |
| `run.fatal_error_threshold` | 20 | 熔断：**连续**致命 API 错误数达标 ⇒ 退出码 4（401/403 认证类首错即熔断，不计连续数；重试耗尽也计窗） | 7/17 |
| `run.max_park_s` | 3600 | v1.6 驻留上限：所引档**全部存活密钥均在冷却**时，单次逻辑调用累计等待秒数上限，超限按重试耗尽处理（记录 failed、计入熔断窗）；**0=不驻留，单密钥档下任何 429 都立即失败**，仅建议多密钥池设 0 | 7/17 |
| `input.text_field` | "text" | 正文字段点路径；**写错=全员坏行** | 5 |
| `input.on_bad_line` | "skip" | \| "fail"（退出码 3） | 5 |
| `input.on_missing_pair` | "skip" | UI 缺对策略 | 5 |
| `input.on_index_conflict` | **"fail"** | UI 同号多文件；默认就退出 | 5 |
| `input.max_image_mb` | 20 | 单图上限，超限跳过 | 5 |
| `input.ui_tree_max_chars` | 30000 | 树序列化进提示词的长度上限 | 5/11 |

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
| `semantic` | false | 第④层语义去重开关（要花 embedding 钱） | 9 |
| `semantic_embedding` | semantic=true 必填 | 引用 [embedding.*] 档名 | 9 |
| `semantic_threshold` | 0.95 | 余弦相似度判重线 | 9 |

## A.4 project.toml — [quality]

| 键 | 默认 | 一句话 | 章 |
|---|---|---|---|
| `enabled` | **true** | 与 annotate 至少开一个 | 10 |
| `mode` | "pairwise" | 批内相对（锦标赛）\| "pointwise" 绝对刻度 | 10 |
| `llm` | "default" | 单评审时的裁决档 | 10 |
| `rounds` | 4 | pairwise 轮数 k（每记录参赛 k 次，调用 ≈ N·k/2） | 10 |
| `criteria_per_call` | "all" | 一次裁决全部准则 \| "single" 每准则一问（×C 成本） | 10 |
| `threshold` | 不设 | 聚合分过滤线 [0,1]；**不设=只打分不过滤**；pairwise 下是批内百分位线 | 10 |
| `selection` | "threshold" | \| "top_ratio"；**两机制互斥** | 10 |
| `top_ratio` | selection=top_ratio 必填 | (0,1]，批内保留 ceil(ratio×**已打分**存活数) 条；selection 为 threshold 时设置无效（启动打 warning） | 10 |
| `judges` | [] | 评审团（奇数个档名）；非空**替代** quality.llm；成本× | 10 |
| `both_orders` | false | 正反双序一致才记胜负；成本 ×2 | 10 |
| `on_unscored` | "keep" | 全部比较失败的记录去留；keep 不占 top_ratio 名额 | 10 |
| `rubric` | 按模态自动 | "default:text" \| "default:ui" \| "inline"（须配 [[rubric.criteria]]） | 10/B |
| `judgment_reasons` | "auto" | 裁决附理由；auto=开了 quality trace 才要 | 10/16 |
| `rubric.name` / `criteria[].key/weight/description/pairwise_prompt/pointwise_levels[6]` | — | 内联 rubric 结构 | 7/10 |

## A.5 project.toml — [generate]

| 键 | 默认 | 一句话 | 章 |
|---|---|---|---|
| `enabled` | false | 仅 text 模态；process 下要求 quality 开 | 12 |
| `llms` | ["default"] | 档名数组；每次调用选 1 个 | 12 |
| `mixture` | "round_robin" | \| "weighted"（配 weights） | 12 |
| `weights` | [] | weighted 必填：正数、长度=len(llms) | 12 |
| `instruction` | enabled 必填 | 生成指令（收放心法见 12.7） | 12 |
| `num_per_record` | 2 | 每种子期望产出条数 | 12 |
| `seeds_per_call` | 3 | 每次调用抽几条种子当示例 | 12 |
| `num_per_call` | 4 | 每次调用要求产出条数 | 12 |
| `seed_min_score` | 自动 | 种子门槛：默认 quality.threshold，再缺省批中位数 | 12 |
| `temperature` | 0.9 | 生成温度（覆盖档默认） | 12 |
| `sample_validator` | 不设 | 样本级代码回调 "module:function"：过滤语义，剔除计入桶 rejected_by_validator | 12 |
| `seed_examples` | [] | generate_only 种子池形态（process 不得设） | 12/22 |
| `standalone_count` | 不设 | generate_only 无种子形态目标条数（与 seed_examples 互斥；process 不得设） | 12/22 |
| `[[generate.styles]]` | [] | 风格子表 {name, prompt}；每调用均匀抽 1 个追加为 [风格要求] | 12 |

## A.6 project.toml — [annotate] / [verify]

| 键 | 默认 | 一句话 | 章 |
|---|---|---|---|
| `annotate.enabled` | **true** | — | 11 |
| `annotate.llm` | "default" | UI 模态须 supports_vision | 11 |
| `annotate.instruction` | enabled 必填 | 写法指南 11.4 | 11 |
| `annotate.examples` | [] | few-shot {input, output}；output 启动时过 Schema 校验 | 11 |
| `annotate.self_consistency` | 0 | 0=关；≥3 奇数：n 次采样字段级投票，成本 ×n | 11 |
| `annotate.sc_temperature` | 0.7 | SC 各次采样温度（多样性来源） | 11 |
| `verify.enabled` | false | 开则要求 annotate 开 | 13 |
| `verify.llm` | "judge" | enabled 且 judges 为空时须存在于 [llm.*]（judges 非空即被替代、免校验）；建议独立于标注模型 | 13 |
| `verify.judges` | [] | 评审团（奇数个）；非空替代 verify.llm | 13 |
| `verify.policy` | "drop" | \| "repair"（意见回喂重标，唯一改写标注的路径） | 13 |
| `verify.max_repair_rounds` | 1 | repair 轮数上限 | 13 |
| `verify.extra_criteria` | "" | 追加评审维度自由文本 | 13 |

## A.7 project.toml — [output] / [trace]

| 键 | 默认 | 一句话 | 章 |
|---|---|---|---|
| `output.schema_path` / `schema_inline` | 恰一 | 用户 Schema（draft 2020-12，顶层 object，禁 _meta） | 14 |
| `output.max_repair_attempts` | 2 | 结构引擎 L3 轮数预算 | 14 |
| `output.repair_llm` | 同调用方 | 修复专用档（可指便宜小模型） | 14 |
| `output.validator` | 不设 | L2.5 代码回调 "module:function"：业务级硬校验，违规回喂修复环；启动校验含 few-shot 干跑 | 14 |
| `output.meta_mode` | "inline" | \| "sidecar"（{stem}.meta.jsonl）\| "none"（丢分数溯源，不推荐） | 8 |
| `output.passthrough_fields` | [] | 输入字段透传至 _meta.source.fields | 8 |
| `output.rejects` | "refs" | "none" \| "refs"（无数据内容）\| "full"（含原文=数据副本） | 8 |
| `trace.enabled` | false | 事件流开关 | 16 |
| `trace.path` | {stem}.trace.jsonl | 首个事件写出时截断（速败运行不再触碰；dry-run 写 `{名}.dryrun{后缀}` 独立文件） | 16 |
| `trace.channels` | ["quality","verify","schema"] | 八通道：ingest/dedup/classify（v1.7）/quality/annotate/verify/schema/llm；默认值不变，分类判决须显式订阅 "classify" | 16/24 |
| `trace.content` | "refs" | none→refs→excerpt→full 四档脱敏；full=完整数据副本 | 16 |

## A.8 组合约束（启动即查，违反=退出码 2）

1. `annotate` 与 `quality` 至少启用一个
2. `verify` ⇒ `annotate`
3. `generate` ⇒ modality="text"；process 模式下另 ⇒ `quality`
4. `generate_only` ⇒ `generate.enabled` 且 `run.input` 缺省
5. `quality.threshold` ⨯ `selection="top_ratio"` 互斥
6. `generate_only` ⇒ `seed_examples` 与 `standalone_count` **恰好设置其一**（同时设置或均缺省都报错）；process 模式下两键均不得设置
7. judges 数组非空须奇数且成员存在于 [llm.*]
8. UI 模态被引用的 LLM 档须 `supports_vision=true`
9. `weighted` ⇒ weights 正数且长度=len(llms)
10. `self_consistency` ∈ {0} ∪ {≥3 奇数}
11. `dedup.semantic = true` ⇒ `semantic_embedding` 必填，且引用的档名须存在于 config.toml `[embedding.*]`
12. `output.validator` / `generate.sample_validator` ⇒ 须为可导入、可调用的 `"module:function"`；前者还须让全部 few-shot 示例 output 干跑通过
13. `classify.enabled = true` ⇒ `[[classify.classes]]` ≥ 2 项，且 `classify.fallback_class` 必填并 ∈ classes（v1.7）
14. `classify.max_labels` 仅 `assignment = "multi"` 可设，∈ [2, 类别数]（缺省回填为类别数）
15. `classify.enabled = false` 而 `[[classify.classes]]` / `[class.*]` 在场 ⇒ 仅 **warning**（一次、点名被忽略的表——「留配置、关开关」合法，不触发退出码 2）

## A.9 project.toml — [classify] 与 [class.<name>.*] 按类覆盖（v1.7 追加）

| 键 | 默认 | 一句话 | 章 |
|---|---|---|---|
| `classify.enabled` | false | 默认关；关闭时与 v1.6 行为一致（唯一可见差异：`_meta.classification` 恒在、值为 null） | 24 |
| `classify.llm` | "default" | profile 引用；UI 模态须 supports_vision；计入密钥/vision/probe 三处引用集 | 24 |
| `classify.assignment` | "single" | 锁定一条一类 \| "multi"（多类命中按标签扇出：**行唯一键变 (_meta.id, label)**，counts 增 fanout） | 24 |
| `classify.max_labels` | 类别数 | 仅 multi 可设；∈ [2, 类别数]；扇出成本（×m 份打分/标注/评审）的封顶旋钮 | 24 |
| `classify.instruction` | "" | 可选补充说明，追加在 system 类别表之后；横跨多类的裁决规则（「拿不准选 X」）写这里 | 24 |
| `classify.fallback_class` | enabled 必填 | 兜底类：须 ∈ classes；分类失败归它，LLM 亦可主动选它 | 24 |
| `classify.self_consistency` | 0 | 0=关；≥3 奇数：n 次采样投票，**无过半归兜底类**（不回退首样本），成本 ×n | 24 |
| `classify.sc_temperature` | 0.7 | sc 各次采样温度；仅 self_consistency ≥ 3 生效 | 24 |
| `classify.on_error` | "fallback" | 结构修复耗尽：归兜底类、记录存活（不写 errors，不污染 rejects 归因）\| "fail"：记录 failed → rejects | 24 |
| `[[classify.classes]]` | enabled 必填 | ≥2 项；每项 {name：`[a-z0-9_]+` 表内唯一, description：非空（LLM 可见的全部类语义）, examples：可选 few-shot（仅输入侧）} | 24 |

`[class.<name>.<节>]` 按类覆盖白名单（`<name>` 须 ∈ classes；未提供的键继承全局；**白名单外键报 CONFIG_ERROR**——「未知键仅 warning」惯例的显式例外）：

| 节 | 可按类覆盖 | 锁定全局 |
|---|---|---|
| `[class.*.quality]` | mode / rounds / rubric（含 `[class.*.rubric]` 内联子表）/ threshold / selection / top_ratio | llm、judges、both_orders、criteria_per_call、on_unscored |
| `[class.*.annotate]` | instruction / examples | llm、self_consistency、sc_temperature |
| `[class.*.generate]` | instruction / styles / num_per_record / temperature | llms、mixture、weights、seeds_per_call、num_per_call、sample_validator |
| `[class.*.verify]` | extra_criteria | llm、judges、policy、max_repair_rounds |
| —— | —— | `run.*` / `input.*` / `dedup.*` / `classify.*` / `output.*`（输出 Schema 全局唯一）/ `trace.*` 从不按类 |

合并细则：优先级 `[class.<name>].<节>.<键>` > `[<节>].<键>` > 内置默认；threshold/selection/top_ratio 按**选择组**整组合并（全局 threshold + 类 top_ratio 合法，互斥校验跑在合并后视图上）；类 rubric 换 selector 后重解析，6 级量表校验按（类有效 mode × 类有效 rubric）执行；类 examples 启动时干跑全局 Schema。详见第 24 章。

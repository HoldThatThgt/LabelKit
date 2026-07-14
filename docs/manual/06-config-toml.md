# 第 6 章　config.toml 完全解读：把模型接进来

> `config.toml` 是工具级静态配置：声明你有哪些 LLM 可用、怎么调用它们、日志什么格式。
> 它随部署环境变化、跨工程复用——通常一次配好，很少再动。
> 本章逐参数讲清「它是什么、默认多少、动它会发生什么」。

## 6.1 文件骨架

```toml
schema_version = 1          # 必填，本版本固定为 1

[tool]                      # 全局日志设置（可省，全有默认值）
log_level = "info"
log_format = "text"

[llm.default]               # 至少要有一个 [llm.<名字>] 子表
provider = "openai_compatible"
base_url = "https://llm-gw.example.com/v1"
model = "qwen2.5-vl-72b-instruct"
api_key_env = "LABELKIT_KEY_DEFAULT"
# ...其余参数见 6.3

[llm.judge]                 # 想配几个就配几个，名字自取
# ...

[embedding.default_emb]     # 可选：语义去重用的向量模型
# ...
```

核心概念是 **profile（配置档）**：`[llm.default]` 定义了一个名叫 `default` 的 LLM 接入档，`project.toml` 里各算子按名字引用它（`quality.llm = "default"`）。名字随意取，`default` 和 `judge` 只是惯例——前者当主力，后者当独立评审。

> **未知键警告**：写错键名不会导致启动失败，但会收到一条警告日志（「未知键，已忽略（前向兼容）」）。看到这条警告务必回头检查拼写——你以为改了的参数可能根本没生效。

## 6.2 `[tool]`：日志的两个开关

| 键 | 默认 | 含义 |
|---|---|---|
| `log_level` | `"info"` | stderr 日志级别：`debug` / `info` / `warn` / `error`。被 CLI `--log-level` 覆盖。`debug` 会多出每次 LLM 调用的摘要行（延迟、token、重试），排障时很有用 |
| `log_format` | `"text"` | `"text"`（人读）或 `"jsonl"`（机器读，每行一个 JSON 事件）。**选 `jsonl` 会自动禁用进度条**，使经日志模块输出的运行日志能被日志采集系统逐行 `json.loads` 解析。注意仍有少量不经日志模块的纯文本 stderr 输出：配置装载期的 `warning:` 行（此时日志系统尚未初始化）、`--dry-run` 的估算行、以及每次运行结束的三行终版摘要——采集侧需容忍或过滤非 JSON 行 |

注意区分：这里控制的是 **stderr 运行日志**（只有运维事件，永远不含数据内容）；记录 LLM 裁决理由的 **trace 日志**是另一个通道，由 `project.toml` 的 `[trace]` 控制（第 16 章）。

## 6.3 `[llm.<name>]`：逐参数详解

### 必填四件套

| 键 | 含义与注意事项 |
|---|---|
| `provider` | `"openai_compatible"` 或 `"anthropic"`。决定请求打到哪个路径、报文长什么样：前者 POST `{base_url}/chat/completions`，后者 POST `{base_url}/v1/messages`。国产模型网关、vLLM、中转站基本都是前者；z.ai 的 Anthropic 兼容端点、Claude 官方 API 用后者 |
| `base_url` | API 根地址。**不要**带 `/chat/completions` 后缀——LabelKit 自己拼路径 |
| `model` | 模型名，原样透传给 API |
| `api_key_env` | 持有密钥的**环境变量名**（第 2 章）。启动时检查：被实际引用的 profile，其变量必须存在且非空，否则退出码 2。v1.6 起与 `api_key_envs` **恰提供其一**（互斥，见下节） |
| `api_key_envs` | （v1.6，`api_key_env` 的替代形式）环境变量名**数组**，声明一个**密钥池**——同一个 profile 挂多把密钥，运行时自动轮换。≥1 项，逐项非空且互异；单元素数组与 `api_key_env` 完全等价。详见下节 |

> **恰其一**：`api_key_env` 与 `api_key_envs` 两者必须写一个、且只能写一个——都写或都不写，M1 都报配置错误（退出码 2）。用了 `api_key_envs` 时，被实际引用的 profile 其**每个**列出的环境变量都必须存在且非空——缺了哪几个就报哪几个（与其他配置错误一起聚合汇报，不是碰到第一个就停）。

### 密钥池：一个 profile、多把密钥（v1.6）

限流（429）是批处理最常见的减速带。v1.6 起你可以给同一个 profile 配多把 API Key，让请求在密钥之间自动轮换——一把被限流，下一次尝试立刻换另一把发出，**只要池里还有可用密钥，限流等待就是零**：

```toml
[llm.default]
provider = "openai_compatible"
base_url = "https://llm-gw.example.com/v1"
model = "qwen2.5-vl-72b-instruct"
api_key_envs = ["LABELKIT_KEY_A", "LABELKIT_KEY_B", "LABELKIT_KEY_C"]   # 替代 api_key_env，二者恰其一
max_concurrency = 8         # 注意：这是池内三把密钥合计的并发上限，不是每把各 8
```

两条结构性约束先说清楚：

- **同构池**：池内密钥共享该 profile 的其余全部字段——同一个 `base_url`、同一个 `model`。密钥池是传输层容错，不是模型路由；选中哪把密钥只影响时序，**不改变任何产出数据的内容**（密钥选择是确定性算法，不占用 `run.seed`，可复现性不受影响）。想混用不同模型请配多个 profile（见 6.5）。
- **`max_concurrency` 仍是一个信号量**：整只池共享这一个并发额度，加密钥不会自动放大并发。密钥池解决的是「限流了怎么办」，想更快还得调 `max_concurrency`。

运行时的**降级阶梯**是：轮换（零等待）→ 驻留（有界等待）→ 记录失败累积 → 熔断退出 4。逐级看：

| 阶段 | 触发 | 行为 |
|---|---|---|
| 轮换 | 每次请求尝试发出前 | 选「在途请求数最少」的可用密钥（并列取声明序靠前者），请求头按所选密钥逐次构造 |
| 冷却 | 某把密钥收到 429 | **只冷却这把密钥**：响应带 `Retry-After` 就遵从其全时长，不带则按该密钥的连续 429 计数做全抖动指数冷却、封顶 300 秒（计数跨调用累计，该密钥任一成功即清零）。本次尝试照常消耗一次重试预算，但下一次尝试立即换可用密钥重发 |
| 禁用 | 某把密钥收到 401/403 | 该密钥**本运行内永久禁用**（stderr 警告一次）。池里还有存活密钥时，同一尝试立即换密钥重发——不消耗重试预算、不计入熔断窗口（认证失败是密钥级确定性故障，每把至多发生一次）。配额上限以 403 形态返回的网关同样按禁用处理，不做错误体嗅探 |
| 驻留 | **全部**存活密钥都在冷却 | 调用原地驻留到最早的冷却结束再重发。驻留不消耗重试预算；单次逻辑调用累计驻留超过 `run.max_park_s`（project 侧配置，默认 3600 秒，第 7 章）则按重试耗尽处理：该记录 `failed`，计入熔断窗口 |
| 熔断 | **最后一把**存活密钥被禁用 | 等价 v1.5 的认证首错语义：立即熔断，退出码 4 |

**单密钥配置完全兼容**：不用密钥池的既有配置无须任何改动——数据产出、重试记账、熔断与退出语义都与 v1.5 一致。唯一变化在 429 的等待路径：等待受 `run.max_park_s` 约束（超长 `Retry-After`——比如小时级配额信号——不再无界等待），无 `Retry-After` 时的冷却封顶 300 秒。一个要提防的组合：`run.max_park_s = 0`（不驻留）配单密钥 profile，意味着**任何 429 都立即让该记录失败**——0 只建议设在多密钥池上（第 7 章）。

密钥池的运行时行为可观测：三个新 trace 事件 `llm.key_cooldown` / `llm.key_disabled` / `llm.pool_parked`，`llm.call` 事件的 `key_env` 字段，以及运行报告 `llm_usage` 里的每密钥统计 `keys` 与驻留统计 `parked_calls` / `parked_ms`（见第 16 章）。密钥的身份一律用**环境变量名**标识——密钥值永远不会出现在任何日志或报告里。

### 并发与容错

| 键 | 默认 | 这个数字在控制什么 |
|---|---|---|
| `max_concurrency` | 8 | 该 profile 的**并发信号量**：同一时刻最多几个请求在飞。所有经此 profile 的调用（打分、标注、修复、评审）共享这个额度。调大 = 更快 + 更容易撞限流；调小 = 稳但慢。建议从网关限流值的 50–70% 起步 |
| `timeout_s` | 120 | 单次请求超时（秒）。超时算**可重试**错误。长输出任务（大 Schema、长标注）适当调大 |
| `max_retries` | 5 | 可重试错误（网络错、超时、HTTP 408/409/429/5xx）的最大重试次数。重试之间用「全抖动指数退避」：第 i 次等待 = `random(0, retry_base_delay_s × 2^i)`，封顶 60 秒；429 响应带 `Retry-After` 头时优先遵从（v1.6 起一切 429 等待统一经密钥冷却路径实现且受 `run.max_park_s` 约束，见上节「密钥池」；退避公式适用于其余可重试错误）。**不可重试**错误（401/403/400/404）不重试，直接判致命 |
| `retry_base_delay_s` | 1.0 | 上式中的退避基数。网关脾气差（频繁 429）时调大到 2–4 |

一次调用的完整容错路径：可重试错误 → 退避重试至多 `max_retries` 次 → 仍失败则该记录标记 `failed`（错误码 `provider_retryable_exhausted`）；致命错误 → 该记录立即 `failed` 并计入熔断窗口 → 连续致命达 `run.fatal_error_threshold`（project 侧配置，默认 20）触发熔断，整个运行以退出码 4 终止；其中**认证类致命错误（401/403）不计连续数、首次出现即熔断**（详见第 7 章）。v1.6 起若该 profile 配了密钥池，401/403 先按密钥禁用、还有存活密钥就静默轮换，只有**最后一把**存活密钥被禁用时才触发上述立即熔断（见上节「密钥池」）。

### 能力声明（很重要，别乱填）

| 键 | 默认 | 填错的后果 |
|---|---|---|
| `supports_structured_output` | false | 声明该模型支持原生结构化输出。填 `true` 时，结构引擎启用 L0 层：OpenAI 兼容口传 `response_format={"type":"json_schema",...}`，Anthropic 口用强制工具调用把 Schema 作为工具入参。**模型实际不支持却填 true** ⇒ 请求可能直接报 400。填 false 完全没问题——只是结构保证全部落到代码修复层（第 14 章），多花一点修复调用 |
| `supports_vision` | false | 声明该模型能看图。**UI 模态下被引用的 profile 必须为 true**——这是启动时的硬校验（填 false 会退出码 2），因为跑到一半才发现模型看不了图，钱已经烧了。v1.8 stream 模式（第 25 章）下这条校验有三处例外：`extract.llm` 引用的档**恒**要求 true（每次摘取都看前后两帧截图）；`segment.llm` 仅 `segment.use_vision = true` 时要求；`quality.llm` 反而**免除**要求（序列打分是纯文本，UI 模态也不看图——唯一的放宽项） |

### 生成参数

| 键 | 默认 | 说明 |
|---|---|---|
| `max_output_tokens` | 4096 | 透传给 API 的输出上限。**太小是隐蔽的坑**：输出被截断 → JSON 不完整 → 结构引擎反复修复 → 成本翻倍甚至记录失败。你的 Schema 越大（字段多、数组长），这个值要越宽裕 |
| `temperature` | 0.0 | profile 级默认温度。0 = 最大确定性，打分、标注、评审都应该用 0。**生成任务需要多样性**，但别改这里——在 `project.toml` 用 `generate.temperature`（默认 0.9）按阶段覆盖 |
| `max_image_px` | 2048 | 图像长边上限：超出等比缩小后再编码发送。调小省 token（视觉模型按分辨率计费），但缩太狠会让小字号 UI 文本不可读，直接伤害 UI 打分和标注质量。手机整屏截图建议 ≥ 1536 |

### 计价（可选但强烈建议配）

| 键 | 说明 |
|---|---|
| `price_per_mtok_in` / `price_per_mtok_out` | 每百万输入/输出 token 的单价。**两个单价都配置后**，运行报告的 `llm_usage` 才会多出 `est_cost_usd` 字段（第 8、17 章）；只配一个不生效。注意成本数字只出现在事后报告里——`--dry-run` 仅估算调用次数（不含成本），进度条也只显示批号与各状态计数。不配置不影响功能，只是报告里没有成本数字 |

## 6.4 `[embedding.<name>]`：语义去重的向量档

只有打算开启**语义去重**（`dedup.semantic = true`，第 9 章）才需要配。字段与 `[llm.*]` 同构，差异：

| 键 | 默认 | 说明 |
|---|---|---|
| `provider` | `"openai_compatible"` | **本版唯一取值**。请求打 POST `{base_url}/embeddings` |
| `base_url` / `model` / `api_key_env` | 必填 | 同 LLM 档 |
| `api_key_envs` | 无 | v1.6 密钥池，与 `api_key_env` 恰提供其一；轮换/冷却/禁用/驻留机制与 LLM 档完全一致（见 6.3「密钥池」），每个列出的环境变量都须存在且非空 |
| `max_concurrency` / `timeout_s` / `max_retries` / `retry_base_delay_s` | 8 / 60 / 5 / 1.0 | 与 LLM 档同一套重试/限流机制 |
| `dims` | 不设 | 设了就逐次校验返回向量的维度，不匹配立即判致命错误——防「模型换了没人知道」的静默事故 |

## 6.5 多 profile 的典型格局

```toml
[llm.default]        # 主力：多模态大模型，干打分和标注的重活
provider = "openai_compatible"
base_url = "https://llm-gw.example.com/v1"
model = "qwen2.5-vl-72b-instruct"
api_key_env = "LABELKIT_KEY_DEFAULT"
max_concurrency = 8
supports_structured_output = true
supports_vision = true
price_per_mtok_in = 0.6
price_per_mtok_out = 1.8

[llm.judge]          # 评审：换一个模型家族，避免"自己查自己"
provider = "anthropic"
base_url = "https://api.anthropic.com"
model = "claude-sonnet-5"
api_key_env = "LABELKIT_KEY_JUDGE"
max_concurrency = 4
supports_structured_output = true
supports_vision = true
```

为什么评审要换模型家族？模型评审自己（或同家族模型）的输出存在**自增强偏差**——它倾向于觉得「和我口味一致的就是好的」。LabelKit 在 `verify.llm` 与 `annotate.llm` 的 model 相同时会打印警告（不阻断）：

```
warning: project.toml:[verify].llm: verify.llm 与 annotate.llm 使用同一模型 "glm-5.2"，存在自增强偏差风险（3.7.2）
```

预算有限只有一个模型可用时，这个警告可以接受——verify 依然能抓住事实性错误，只是对「风格性偏差」的纠察力打折。

## 6.6 本章速查

```
必填：schema_version=1；至少一个 [llm.<name>]，其中 provider/base_url/model 必填，
      api_key_env 与 api_key_envs 恰其一（v1.6 密钥池：数组、逐项非空互异、每个变量都须已设置）
容错：max_concurrency=8（密钥池下仍为池内总上限）, timeout_s=120, max_retries=5, retry_base_delay_s=1.0
密钥池：轮换（零等待）→ 驻留（受 run.max_park_s 约束，第 7 章）→ 熔断（最后一把密钥被禁用）；可观测性见第 16 章
能力：supports_structured_output=false, supports_vision=false（UI 模态引用者必须 true）
生成：max_output_tokens=4096, temperature=0.0, max_image_px=2048
计价：price_per_mtok_in/_out（可选，配了才有成本估算）
日志：tool.log_level=info, tool.log_format=text
向量：[embedding.<name>]（仅语义去重需要；provider 仅 openai_compatible）
```

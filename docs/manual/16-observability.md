# 第 16 章　可观测性：日志、trace 与 rubric 调优闭环

> 本章回答两个问题：**运行时发生了什么**（日志与事件），以及**如何把"发生了什么"变成"下次跑得更好"**——
> 后者的核心是用 trace 驱动 rubric 迭代的闭环，这是把 LabelKit 用出高水平的分水岭。

## 16.1 三个输出面，各司其职

| 面 | 开关 | 内容 | 给谁看 |
|---|---|---|---|
| **stderr 运行日志** | 恒开（级别可调） | 只有运维事件：生命周期、警告、错误、调用摘要。**绝不含数据内容与提示词** | 人排障；日志采集系统 |
| **trace 事件流** | `trace.enabled`（默认关） | 结构化事件 JSONL，一行一事件：每次去重判定、每次质量裁决及理由、每轮评审结论、每次结构修复 | rubric 调优；质量审计 |
| **进度显示** | TTY 且 `log_format="text"` 时 | 批级进度行（当前批号 + 各状态累计计数）；非 TTY 无进度输出，可看 `batch.end` 的 INFO 日志行 | 坐在终端前的你 |

心法：**stderr 告诉你「跑得顺不顺」，trace 告诉你「判得对不对」。**

## 16.2 trace 事件速查

每行恒有七个字段：`ts / run_id / batch_no / stage / ev / record_ids / payload`。事件名前缀即通道名（`trace.channels` 过滤的就是它），两个例外：`run.*`、`batch.*` 生命周期事件不受过滤恒写；`error` 事件无前缀，按产生它的 stage 归属通道——订阅某 stage 的通道（如 `"quality"`）即可同时收到该 stage 的 error 事件。

| 事件 | 何时发出 | payload 里最有用的东西 |
|---|---|---|
| `run.start` / `run.end` | 首行 / 末行 | 配置指纹；终版 counts 与 exit_code |
| `batch.start` / `batch.end` | 每批 | 各状态计数、耗时 |
| `ingest.bad_line` / `missing_pair` / `index_conflict` | 接入跳过时 | 文件、行号/index、原因 |
| `dedup.duplicate` | 每次判重（判为 unique 不发事件） | kind、簇键、kept_id；近似重复另带**恰一项实测相似度**（文本 jaccard / 图像 hamming / 语义 cosine），exact 精确重复无相似度字段 |
| `quality.judgment` | 每次成对裁决 | 呈现顺序、每准则 winner + **reason**（评审团时每评审一条，带 judge 字段） |
| `quality.pointwise` | 每次单点打分 | criterion、0–5 原始分、reason |
| `quality.bt_fit` | 每批每准则拟合完 | 是否收敛、迭代数、比较数 |
| `quality.gate` | 每条门控判定 | 聚合分、keep/drop、阈值或名次 |
| `annotate.done` | 每条标注成功 | attempts；self-consistency 的 n 与一致率 |
| `verify.verdict` | 每轮评审 | verdict、round、critiques 全文 |
| `schema.repair` | 每次非清洁路径 | 在哪层解决（l1/l3_1/l3_2/rejected）、违规清单（JSON Pointer，不含数据值） |
| `llm.call` | 每次 API 调用 | profile、延迟、input/output token、重试数、状态（命名对齐 OpenTelemetry GenAI 约定）；语义去重的 embedding 调用另带 `operation="embedding"`（缺省即对话补全）；密钥池 >1 的 profile 另带 `key_env`（v1.6）——本调用**最后一次尝试**所用密钥的环境变量名，成功失败同义；零尝试即中止的调用（如入口即驻留超限、熔断中止）不带 |
| `llm.key_cooldown` | 密钥进入 429 冷却时（v1.6；任意池大小含 1——单密钥的 429 等待亦走冷却路径）。仅 trace，不上 stderr | profile、`key_env`、`cooldown_s`（本次冷却秒数）、`retry_after`（bool，时长是否来自 `Retry-After` 头） |
| `llm.key_disabled` | 密钥 401/403 被本运行永久禁用时（v1.6；每密钥每运行至多一次）。同时发 stderr WARN 一次 | profile、`key_env`、`status_code` |
| `llm.pool_parked` | 某 profile 全部存活密钥均在冷却、调用开始驻留时（v1.6；每次驻留一事件）。同时发 stderr WARN | profile、`wait_s`（预计驻留秒数）、`live_keys`（存活密钥数） |
| `error` | 每次 StageError | stage、错误码 kind（第 18 章）、message、是否可重试 |

内容脱敏四档（`trace.content`）回顾：`none`（只有结构化字段）→ `refs`（+LLM 产出的理由文本，默认）→ `excerpt`（+输入内容前 200 字）→ `full`（+完整提示词与响应，需订阅 `llm` 通道；**等于存了一份数据副本，短期调试用完即清**）。

一条与脱敏档位无关的恒定规则（v1.6）：`llm.key_*` / `llm.pool_*` 事件与 `llm.call` 的 `key_env` 字段里，密钥恒以**环境变量名**标识——密钥值本身在任何档位（含 `full`）都不写入 trace、运行日志与报告。

## 16.3 rubric 调优闭环：让准则跟着证据迭代

**为什么 rubric 不可能一次写对**：EvalGen（UIST 2024）的实验结论——评估准则存在 *criteria drift*，人往往要**看到评审的具体输出**才知道自己真正想要什么准则。所以 LabelKit 把每一次裁决和理由都暴露出来（`quality.judgment` 事件），供你迭代。

标准闭环六步：

```
① 开记录仪      project.toml: trace.enabled=true, channels 含 "quality"
                （quality.judgment_reasons 默认 "auto"，此时自动生效）
② 小样本跑      labelkit run ... --limit 100 --output out/tune.jsonl
③ 算指标+抽读   下方诊断表 + 抽读 20 条 reason
④ 改 rubric     改写 pairwise_prompt / 合并冗余准则 / 加缺失准则 / 调 weight
⑤ 同 seed 重跑  对比指标是否收敛（seed 不变 ⇒ 配对方案逐对相同，可比）
⑥ 定稿全量      可关 judgment_reasons 省 token
```

### 诊断指标表（③ 的量化部分）

| 信号 | 参考线 | 诊断 | 处置 |
|---|---|---|---|
| 某准则 tie 率（直接读 report.quality.per_criterion_tie_rate，或按下方 jq 从 trace 算） | > 0.4 | 准则区分度不足——问什么都答"差不多" | 把比较问句改具体（给出可判的判据）；或降该准则权重 |
| 同 seed 重跑翻转率 | > 0.3 | 准则表述含糊 / 模型不稳定 | 细化 description；确认 temperature=0；必要时换裁决模型 |
| 两准则胜负一致率 | > 0.9 | 准则冗余（换着说同一件事） | 合并为一条（权重相加）或删一条 |
| reason 高频出现 rubric 没写的维度 | — | 准则缺口（criteria drift 的正常表现） | 新增 criterion |
| judgment_failures 率 | > 0.05 | 裁决提示词太长/太怪，模型输出结构崩 | 精简 rubric 文案；确认 profile 结构化输出能力 |

### 现成的 jq 计算

```bash
T=out/tune.trace.jsonl

# 某准则的 tie 率
jq -s '[.[] | select(.ev=="quality.judgment") | .payload.judgments[]
        | select(.criterion=="facts_trivia")]
       | (map(select(.winner=="tie")) | length) / length' $T
# → 0.4375  高于 0.4：这条准则对你的数据没有区分度

# 两条准则的胜负一致率（冗余检测）
jq -s '[.[] | select(.ev=="quality.judgment").payload.judgments
        | {a: (.[] | select(.criterion=="writing_style").winner),
           b: (.[] | select(.criterion=="educational_value").winner)}]
       | (map(select(.a == .b)) | length) / length' $T

# 抽读某条被淘汰记录的全部裁决理由
jq -c 'select(.ev=="quality.judgment" and (.record_ids | index("6e60ce3c2d59f04d")))
       | .payload.judgments[] | {criterion, winner, reason}' $T
```

一个真实的 `quality.judgment` 事件（UI 工程，refs 档）感受一下 reason 的信息量：

```json
{"ev": "quality.judgment", "record_ids": ["40f47f09…", "f8fc254f…"],
 "payload": {"order": {"A": "40f47f09…", "B": "f8fc254f…"}, "model": "glm-5.2",
   "judgments": [
     {"criterion": "state_completeness", "winner": "tie",
      "reason": "两组界面均加载完成，无骨架屏或异常空白区域，状态完整性一致。"},
     {"criterion": "interaction_richness", "winner": "A",
      "reason": "记录A的登录页包含输入框、按钮、复选框等多种可交互控件类型，比记录B以纯文本列表为主的设置页交互元素更丰富。"}]}}
```

这两句理由就是 rubric 工作状态的直接证据：判据被理解了、比较是逐维度独立的。当 reason 开始反复提到某个你没写进 rubric 的维度（比如「B 的隐私信息未打码」），那就是加新准则的信号。

## 16.4 运行日志与调用级观测

`--log-level debug` 后，stderr 会多出每次 LLM 调用的摘要行；`tool.log_format = "jsonl"` 让经日志模块输出的运行日志每行都是可解析的 JSON（进度条自动禁用）。注意仍有少量不经日志模块的纯文本 stderr 输出——结束时的三行终版摘要、配置装载期的 `warning:` 行、`--dry-run` 的估算行——采集侧需容忍或过滤非 JSON 行：

```
# text 格式（默认）
2026-07-03T01:19:03+08:00 INFO  run  batch=1 batch.end active=8 dropped_dup=1 dropped_lowq=5 ... duration_ms=87434

# jsonl 格式（采集系统用）
{"ts":"2026-07-03T01:19:03+08:00","level":"info","stage":"run","batch":1,"msg":"batch.end active=8 ..."}
```

想要完整的调用审计（每次请求的 token、延迟、重试、状态），订阅 trace 的 `llm` 通道即可——`llm.call` 事件字段命名对齐 OpenTelemetry GenAI 语义约定（`gen_ai.usage.input_tokens` 等），现成的 OTel 生态分析工具可以直接吃。

### 密钥池的分密钥视角（v1.6）

profile 配置了密钥池（`api_key_envs`，第 6 章）后，调用级观测多出一层**分密钥**读数：

- **trace 侧**：三个 `llm.key_*` / `llm.pool_*` 事件（16.2 表）回答限流落在哪把密钥、有没有密钥被 401/403 禁用、全池冷却导致的驻留发生了几次多久（驻留上限 `run.max_park_s`，第 7 章）；`llm.call` 的 `key_env` 字段则把每次调用归到具体密钥。
- **报告侧**：`report.json` 的 `llm_usage.<profile>` 增列 `keys`（仅密钥池 >1 时出现）——按环境变量名分密钥给出 `calls` / `rate_limited` / `disabled`，一眼看出流量与限流是否集中在某把密钥、有没有密钥中途被禁；另有驻留统计 `parked_calls` / `parked_ms`（池 >1 或数值非零时出现——单密钥配置发生过驻留同样留痕）。

诊断口诀：某密钥 `rate_limited` 独高 ⇒ 该密钥配额偏小，限流被轮换吸收、不必动手；`disabled: 1` ⇒ 该密钥凭据坏了，查对应环境变量；`parked_ms` 持续非零 ⇒ 整池都在限流里打转，加密钥或降 `max_concurrency`（第 17 章）。

## 16.5 日志的可靠性契约

- **日志写失败绝不中断运行**：trace 文件不可写时 warn 一次、关闭通道、继续跑，丢弃的事件数计入 `report.trace.dropped_events`（trace 文件落点见 `report.trace.path`）；
- trace 首行恒为 `run.start`（携带 `trace_schema_version: 1`），末行恒为 `run.end`——两行俱在即文件完整；
- 事件目录是稳定契约：后续版本**只增不改**（新增事件或字段，不改既有字段语义），你写的 jq/分析脚本不会被升级弄坏；
- trace 文件默认路径随输出走，在**首个事件写出时**截断：死于配置/输入校验的运行与 dry-run（写 `{名}.dryrun{后缀}` 独立文件）都不会再动它；但正常启动的重跑仍会覆盖——归档要趁早。

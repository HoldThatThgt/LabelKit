# 第 7 章　project.toml 完全解读：定义一次任务

> `project.toml` 是工程级配置：一次标注任务的全部意图都写在这里。
> 本章精讲 `[run]` `[input]` `[output]` `[trace]` 四节的每个参数；
> 八个算子节（`[dedup]` `[classify]` `[quality]` `[generate]` `[annotate]` `[verify]` 与 v1.8 的 `[segment]` `[extract]`）
> 及配套的 `[stream]` 输入声明节在此给出速览，深度解读见第 9–13、24、25 章。

## 7.1 文件骨架与最小可用配置

一份**最小**的文本标注工程只需要这些：

```toml
schema_version = 1

[run]
input = "./data/input.jsonl"
output = "./out/labels.jsonl"
modality = "text"

[annotate]
instruction = "你是标注员。……"

[output]
schema_inline = """
{"type": "object", "properties": {"label": {"type": "string"}},
 "required": ["label"], "additionalProperties": false}
"""
```

没写的节全部取默认：dedup 开、quality 开（只打分不过滤）、classify 关、generate 关、verify 关、trace 关。**从最小配置出发按需添加**，比一开始就抄一份全量配置更不容易犯错。

## 7.2 `[run]`：这次运行的骨骼

| 键 | 类型 | 默认 | 详解 |
|---|---|---|---|
| `input` | str | process 模式必填 | 输入路径（文件或目录，见第 5 章）。可被 CLI `--input` 覆盖。**`generate_only` 模式下必须不设**——设了（包括用 `--input` 传）直接配置错误 |
| `output` | str | 必填 | 主输出 `.jsonl` 路径。可被 CLI `--output` 覆盖。其余产物都从它派生：`{stem}.rejects.jsonl`、`{stem}.report.json`、`{stem}.trace.jsonl` 都落在同目录 |
| `modality` | str | 必填 | `"text"` 或 `"ui"`。决定输入解析方式、去重算法组合、提示词形态 |
| `mode` | str | `"process"` | `"process"` = 加工既有数据；`"generate_only"` = 无输入纯生成（第 12 章）。 |
| `batch_size` | int | 256 | 批大小。**双重身份**：内存/落盘节奏的单位 + pairwise 质量打分的比较池大小。用 pairwise 时这是质量口径参数（第 10 章详述），不要只当性能参数调 |
| `seed` | int | 0 | 全局随机种子：配对采样、A/B 呈现顺序、生成时的模型/风格抽取全由它驱动。**换 seed = 换一套随机方案**；固定 seed + 固定输入 = 可复现的流程路径。调试 rubric 时保持 seed 不变，对照才有意义 |
| `fatal_error_threshold` | int | 20 | 熔断阈值：**连续**多少次不可恢复的 API 错误后放弃整个运行（退出码 4）。认证类错误（401/403）不受此阈值约束、**首次出现即熔断**；重试耗尽也计入连续窗口；任何一次成功调用清零计数。调小（如 5）= 对坏端点更敏感、更快止损；调大 = 更能容忍偶发抽风 |
| `max_park_s` | int | 3600 | **驻留预算**（v1.6）：一次逻辑 LLM 调用因所引 profile 的**全部存活密钥都在 429 冷却中**而原地等待（驻留）的累计秒数上限。配了密钥池（`api_key_envs`，第 6 章）时限流先靠换密钥零等待消化、全池冷却才驻留；单密钥配置下它同样约束超长 `Retry-After` 等待（如小时级配额信号），不再无界干等。**超限按重试耗尽处理**：该记录 `failed`、并计入 `fatal_error_threshold` 的连续熔断窗口——限流持续拖垮记录时运行照常熔断止损。驻留发生时 stderr 有 WARN、trace 有 `llm.pool_parked` 事件（第 16 章）。`0` = 不驻留：全池一冷却立即按重试耗尽失败。**警告**：0 配在单密钥 profile 上意味着任何一次 429（哪怕 `Retry-After` 只有几秒）都直接变成记录失败——0 只建议配在多密钥池上。怎么调：无人值守长跑保持默认甚至调大（限流窗口过去自动续跑，宁等勿失）；有人盯着、想尽快暴露限流问题时调小（如 60）快速失败 |

> **关于熔断的一个重要认知**：密钥错误（401/403）会立即熔断、退出码 4，不再有「静默全败」。但模型名拼错这类 400/404 错误仍按连续计数——小数据量下每条记录只失败一次就被隔离，可能**攒不满** 20 次，运行以退出码 0「成功」结束、`failed` 计满。所以：跑之前 `validate --probe`（第 2 章），跑完看摘要里的 `failed` 计数，正式流水线上配 `--strict`。

## 7.3 `[input]`：接入层的口味

| 键 | 默认 | 详解 |
|---|---|---|
| `text_field` | `"text"` | 文本模态的正文字段，支持点路径（`"conversation.turns"`）。写错 = 全员坏行（第 5 章） |
| `on_bad_line` | `"skip"` | 坏行策略：`skip` 跳过并计数 / `fail` 立即退出码 3 |
| `on_missing_pair` | `"skip"` | UI 缺对策略：同上 |
| `on_index_conflict` | `"fail"` | UI index 冲突策略：**默认 fail**（理由见第 5 章——冲突通常意味着目录被污染） |
| `max_image_mb` | 20 | 单张截图大小上限，超限按坏记录跳过 |
| `ui_tree_max_chars` | 30000 | 控件树序列化文本进提示词的长度上限，超出按深度优先截断并附 `…(truncated N nodes)` 标记。树特别深的 App（嵌套 WebView 等）可调大，代价是每次调用的输入 token 变多 |

## 7.4 算子节速览

每节详情见对应章。这里给一张「开关 + 最常调的三个参数」速查：

| 节 | 开关默认 | 最常调的参数 | 去哪深入 |
|---|---|---|---|
| `[stream]` | —（随 segment 生效，v1.8） | `order_by`（时间序依据）、`key`（分区键）、`gap_s`/`gap_steps`（断会话规则） | 第 25 章 |
| `[segment]` | 关（v1.8） | `strategy`（rules/llm/hybrid）、`window`（滑窗帧数）、`min_len`（最短段长） | 第 25 章 |
| `[dedup]` | **开** | `minhash_threshold`（0.85 近似判重线）、`scope`（global/batch）、`ui_dup_requires`（UI 判重口径） | 第 9 章 |
| `[classify]` | 关（v1.7） | `[[classify.classes]]`（类别表，启用必填）、`fallback_class`（兜底类，启用必填）、`assignment`（single/multi 单多标签） | 第 24 章 |
| `[extract]` | 关（v1.8） | `llm`（恒需视觉能力）、`instruction`（摘取补充说明）、`include_diff`（树变更摘要注入） | 第 25 章 |
| `[quality]` | **开** | `mode`（pairwise/pointwise）、`threshold` 或 `selection="top_ratio"`（淘汰机制）、`rubric`（评价准则） | 第 10 章 |
| `[generate]` | 关 | `instruction`（生成指令）、`num_per_record`（每种子产几条）、`llms`/`styles`（多样性来源） | 第 12 章 |
| `[annotate]` | **开** | `instruction`（标注指令，开了就必填）、`examples`（few-shot）、`self_consistency`（多次采样投票） | 第 11 章 |
| `[verify]` | 关 | `llm`（评审档，建议独立模型）、`policy`（drop/repair）、`extra_criteria`（追加评审维度） | 第 13 章 |

`[classify]` 是 v1.7 新增的分类算子节：按你声明的类别表对每条存活记录做 LLM 封闭集分类，类标签写进 `_meta.classification` 并驱动下游「按类条件化」。启用后另有一族按类覆盖节 **`[class.<name>.<section>]`**——对某个类别单独覆盖 quality / annotate / generate / verify（v1.8 起还有 extract 的 instruction）的白名单参数（按类 rubric、按类标注指令等），类未覆盖的键继承全局。完整键表、白名单与合并语义见第 24 章与 spec §5.2。

v1.8 新增的三节同属**时序流（stream 模式）**一族：`[stream]` 声明输入的时间序与会话切分规则（排序依据、分区键、断开条件——它不是算子，随 `segment.enabled` 生效）；`[segment]` 是 stream 模式的总开关，把候选会话经 LLM 边界精化切成语义完整的 episode 并剔除噪声帧；`[extract]` 对 episode 的每对相邻帧推断结构化动作（仅 UI 模态，要求 segment 开启）。三节的逐键详解与完整示例见第 25 章。

开关组合的合法性约束见 4.5 节（M1 启动时强制检查）。

## 7.5 `[output]`：输出的形态

### Schema：二选一，必须给

| 键 | 说明 |
|---|---|
| `schema_path` | 指向外部 `.json` 文件的路径 |
| `schema_inline` | TOML 多行字符串内嵌的 Schema JSON 文本 |

**恰好提供其一**（都给或都不给 = 配置错误）。Schema 必须是合法的 JSON Schema **draft 2020-12**、顶层为 object、且**不得声明 `_meta` 属性**（那是 LabelKit 的保留键）。启动时会用元 Schema 预校验，写错立即退出码 2。Schema 怎么写才对 LLM 友好，第 14 章有专门的编写指南。

选哪个？Schema 短（≤ 30 行）内嵌，随工程文件一目了然；Schema 长或多工程共用，外部文件。

### 结构修复预算

| 键 | 默认 | 说明 |
|---|---|---|
| `max_repair_attempts` | 2 | 结构引擎 L3 层（LLM 修复环）的最大轮数。修复 2 轮仍不合法 ⇒ 该记录 `failed` 进拒绝通道。调大能救回更多顽固记录，但每轮都是一次真金白银的调用；更值得做的是把 Schema 改简单（第 14 章） |
| `repair_llm` | 同调用方 | 修复调用用哪个 profile。默认跟原调用同档；可以指一个便宜的小模型专门干修 JSON 的活 |
| `validator` | 不设 | **代码回调校验（L2.5）**：`"module:function"`，对已过 Schema 的标注对象做业务级硬校验（跨字段/词表/业务规则），违规意见回喂修复环。签名与写法见 14.5；启动时校验可导入、可调用并对 few-shot 示例干跑 |

### 元信息与透传

| 键 | 默认 | 说明 |
|---|---|---|
| `meta_mode` | `"inline"` | `_meta` 的去处：`inline` = 随行内嵌（保留键 `_meta`）；`sidecar` = 主输出保持纯净、`_meta` 逐行写 `{stem}.meta.jsonl`（与主输出行序对齐、以 `_meta.id` 关联）；`none` = 丢弃元信息（**分数、溯源全没了，不推荐**） |
| `passthrough_fields` | `[]` | 从输入行原样透传的字段名列表，落在 `_meta.source.fields`。典型用途：带上 `source`、`ts` 等业务字段，下游无需回查输入文件 |
| `rejects` | `"refs"` | 拒绝通道内容量：`none` = 不写拒绝文件；`refs` = 只写 id、来源引用、淘汰环节与原因（**不含数据内容**）；`full` = 另含记录原文——方便直接人工审查被淘汰了什么，但意味着输出目录里存了一份数据副本，注意保管 |

## 7.6 `[trace]`：给流水线装行车记录仪

trace 是**可选的第四个输出通道**：一行一个 JSON 事件，记录去重判定、每次质量裁决及理由、评审结论、结构修复等。它是 rubric 调优（第 16 章）的核心工具。

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | false | 开关。默认关 = 零额外产物 |
| `path` | 自动 | 默认 `{output_stem}.trace.jsonl`。文件在**首个事件写出时**截断：死于配置/输入校验的运行与 dry-run（trace 写「文件名在扩展名前插 .dryrun」的独立文件）不会触碰旧账本；正常重跑仍会覆盖——要历史就改名归档或换 `path` |
| `channels` | `["quality","verify","schema"]` | 订阅哪些事件通道，可选：`ingest` / `dedup` / `quality` / `annotate` / `verify` / `schema` / `llm`。`run.*`、`batch.*` 生命周期事件不受过滤，恒写 |
| `content` | `"refs"` | 内容脱敏档位，逐档递增：`none` = 只有 id、枚举与数值（最严合规）；`refs` = 另含 LLM 产出的理由/意见文本，**不含输入数据内容**（调优常规档）；`excerpt` = 另含输入内容前 200 字符（免回原文件对照）；`full` = 另含完整提示词与响应（`llm.call` 事件，需同时订阅 `llm` 通道）——**这构成一份完整数据副本，体积可达主输出数十倍，只在调试审计时短期开启** |

什么时候开 trace？**调优期常开**（`channels` 含 `quality`，配合 `quality.judgment_reasons`，第 10/16 章）；**生产期看需要**——refs 档的体积和性能开销都不大，留着当审计底账也无妨。

## 7.7 内联 rubric：`[rubric]` 节

`quality.rubric = "inline"` 时，评价准则直接写在 project.toml 里。注意 `[rubric]` 表的 `name` 是**必填键**（非空字符串，作为 rubric 标识写入每条输出记录的 `_meta.run.rubric`）——漏了它，`labelkit validate` 会报「`project.toml:[rubric].name: 缺失必填键，期望非空字符串`」并以退出码 2 失败：

```toml
[quality]
rubric = "inline"

[rubric]
name = "ime-intent-v1"               # 必填：非空字符串，rubric 标识

[[rubric.criteria]]
key = "task_clarity"                 # [a-z0-9_]+，全局唯一
weight = 2.0                         # 聚合权重，> 0
description = "任务清晰度：诉求是否明确、可执行。"
pairwise_prompt = "比较两条请求，哪一条的诉求更明确、更可直接执行？"
pointwise_levels = [                 # pointwise 模式必填，恰好 6 级（0-5）
  "0: 完全无法理解在要什么。",
  "1: 大致有个方向但含糊。",
  "2: 在 1 的基础上，诉求可辨认但缺关键信息。",
  "3: 在 2 的基础上，诉求明确、基本可执行。",
  "4: 在 3 的基础上，带清晰的约束与背景。",
  "5: 在 4 的基础上，堪称模范请求。"]
```

rubric 的设计方法论（几条准则合适、权重怎么定、prompt 怎么写出区分度）是第 10 章的重头戏。

## 7.8 三源合并与一份带注释的完整示例

再次强调优先级：**CLI 参数 > project.toml > config.toml**。常见用法是把 project.toml 当「基准」，用 CLI 做临时变奏：

```bash
# 同一份工程配置，试跑 50 条到另一个输出，不动文件
uv run labelkit run --config ../config.toml --project project.toml \
    --limit 50 --output out/pilot.jsonl
```

一份注释齐全的中等复杂度工程（UI 标注 + 评审修复 + trace）：

```toml
schema_version = 1

[run]
input = "./capture/2026-07-01"        # UI 目录，递归扫描配对
output = "./out/ui-labels-0701.jsonl"
modality = "ui"
batch_size = 128                      # pairwise 比较池 = 128
seed = 42

[dedup]
ui_dup_requires = "both"              # 树、图都近似才判重（最保守）

[quality]
mode = "pairwise"                     # 批内相对排名
rounds = 4                            # 每记录参与 4 次比较
threshold = 0.3                       # 淘汰批内相对垫底的约 30%
rubric = "default:ui"

[annotate]
llm = "default"
instruction = """
你是移动端 UI 理解标注员。根据屏幕截图与 UI 控件树，
标注该屏幕的功能类别、页面标题、可交互元素列表与一句话页面描述。
"""

[verify]
enabled = true
llm = "judge"                         # 独立评审模型
policy = "repair"                     # 不合格先给一次返工机会
max_repair_rounds = 1

[trace]
enabled = true
channels = ["quality", "verify"]      # 留打分与评审的底账
content = "refs"

[output]
meta_mode = "inline"
rejects = "refs"
schema_inline = """
{ "type": "object", "properties": {
    "screen_category": {"type": "string",
      "enum": ["login","home","list","detail","form","settings","dialog","other"]},
    "page_title": {"type": "string"},
    "interactive_elements": {"type": "array", "items": {"type": "object",
      "properties": {"role": {"type": "string"}, "label": {"type": "string"},
                     "bounds": {"type": "array", "items": {"type": "integer"},
                                "minItems": 4, "maxItems": 4}},
      "required": ["role","label","bounds"], "additionalProperties": false}},
    "description": {"type": "string", "maxLength": 200}},
  "required": ["screen_category","page_title","interactive_elements","description"],
  "additionalProperties": false }
"""
```

下一章讲这份配置跑完后，四个产物文件分别怎么读。

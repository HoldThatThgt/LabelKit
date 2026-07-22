## 3.1 M1 配置管理 config

### 3.1.1 职责与边界

**做：**装载并语法/语义校验 config.toml 与 project.toml；合并 CLI 覆盖项；解析 rubric（内联或默认包）；装载并预校验用户 JSON Schema；读取 API Key 环境变量；产出全局唯一的不可变 `ResolvedConfig`。 
**不做：**不接触输入数据；不发起网络请求（`--probe` 连通性探测委托 M9）；运行期不提供任何可变配置。

### 3.1.2 输入 / 输出

| 方向 | 内容 |
|---|---|
| 输入 | config.toml 路径、project.toml 路径、CLI 参数字典（input/output/limit/strict/log_level/dry_run）、进程环境（仅读取 profile 声明的 `api_key_env` / `api_key_envs`（v1.6）所列变量）。 |
| 输出 | `ResolvedConfig`（frozen dataclass 树：第 5 章两文件全部键的类型化镜像，另含 CLI 专属项 limit/strict/dry_run 与 log_level 覆盖，以及 load() 收尾冻结的解析产物：`ConsoleConfig.mode_resolved`（v1.10，3.1.4 console 行）、`SegmentConfig.vision_resolved`（v1.11，3.1.4 上下文预算与视觉推导行）），或抛出 `ConfigError`（附带全部而非首个校验错误，一次性反馈）。 |

### 3.1.3 API

```
def load(config_path: Path, project_path: Path, cli_overrides: CliOverrides) -> ResolvedConfig:
    """三源合并 + 全量校验。失败抛 ConfigError(errors: list[str])，CLI 以退出码 2 结束。"""

def default_rubric(name: Literal["default:text", "default:ui", "default:trajectory"]) -> Rubric:
    """从包内数据文件（labelkit/data/rubrics/*.toml）装载系统默认 rubric。
       "default:trajectory" 为 v1.8 轨迹 rubric（default_trajectory.toml，附录 A.3）。"""
```

### 3.1.4 校验规则（启动时全量执行）

| 类别 | 规则 |
|---|---|
| TOML 结构 | 两文件均须含 `schema_version = 1`；未知键报 warning（前向兼容）、缺失必填键报 error；类型逐字段核对（第 5 章字段表即校验依据）。v1.7 例外：`[classify]` 与 `[class.*]` 为显式接管的节——`[class.*]` 内白名单（5.2 按类覆盖白名单表）之外的键报 `CONFIG_ERROR` 而非 warning（见下方「按类覆盖合并」行）。 |
| Profile 引用 | `quality.llm / annotate.llm / generate.llms（数组，逐元素校验）/ verify.llm / output.repair_llm`，以及 `quality.judges / verify.judges`（数组，非空时须为奇数个）引用的 profile 必须存在于 config.toml `[llm.*]`；启用视觉输入的阶段（UI 模态的 quality/annotate/verify）要求其 profile `supports_vision = true`。v1.2：`dedup.semantic = true` 时 `dedup.semantic_embedding` 必须存在于 config.toml `[embedding.*]` 且其密钥配置通过本表「API Key」行校验（v1.6：`api_key_env` / `api_key_envs` 恰其一；5.1）。 |
| 交叉字段约束（v1.2） | `quality.selection = "top_ratio"` 时 `quality.top_ratio` 必填且 ∈ (0,1]，且不得再设 `quality.threshold`（互斥，报 CONFIG_ERROR）；`annotate.self_consistency` 为 0 或 ≥3 的奇数；`generate.mixture = "weighted"` 时 `generate.weights` 必填、逐项为正且长度 = `generate.llms`；`[[generate.styles]]` 各项 name 表内唯一、prompt 非空（5.2 各行标注的 M1 校验在此汇总执行）。 |
| 运行模式（v1.4） | `run.mode="generate_only"` 时：`run.input` 必须缺省、`run.modality` 必须 "text"、`generate.enabled` 必须 true；`generate.seed_examples`（非空字符串数组，逐项非空）与 `generate.standalone_count`（≥ 1）恰好提供其一（互斥，分别对应种子池 / 无种子形态）。process 模式下这两键均不得设置。 |
| 分类（v1.7） | `classify.enabled = true` 时：`[[classify.classes]]` ≥ 2 项，每项 `name` 匹配 `[a-z0-9_]+` 且表内唯一、`description` 非空、`examples`（可选）为字符串数组；`classify.fallback_class` 必填且 ∈ classes；`classify.assignment` ∈ {"single","multi"}；`classify.max_labels` 仅 multi 可设且 ∈ [2, 类别数]（缺省解析后回填为类别数）；`classify.self_consistency` 为 0 或 ≥3 的奇数；`classify.on_error` ∈ {"fallback","fail"}；`classify.llm` 引用的 profile 必须存在（UI 模态须 `supports_vision = true`），并计入密钥解析、vision 校验与 `--probe` 三处 profile 引用集（本表「Profile 引用」「API Key」行同法覆盖）。`classify.enabled = false` 而 `[[classify.classes]]` / `[class.*]` 在场 ⇒ warning（一次、点名被忽略的表——「留配置、关开关」合法，对齐 top_ratio 未生效等 no-op 键分级惯例），不报 error。 |
| 按类覆盖合并（v1.7） | `[class.<name>.<section>]` 的 `<name>` 必须 ∈ classes；覆盖键 ∈ 白名单（5.2 按类覆盖白名单表），白名单外键报 `CONFIG_ERROR`（本表「TOML 结构」行「未知键报 warning」的显式例外）。合并语义（启动时静态合并、冻结为 `class_views`，运行期零查找）：① 逐键 provenance 合并——类显式提供的键覆盖全局、未提供的键继承全局；② **选择组**——类显式提供 selection / threshold / top_ratio 任一 ⇒ 合并视图剔除全局侧的互斥对键，threshold 与 top_ratio 互斥校验跑在**合并后视图**上（防止「全局 threshold + 类 top_ratio」逐键 replace 后两键并存的误报）；③ **rubric**——合并 selector 后重解析为该类有效 rubric，pointwise 6 级校验跑在（类有效 mode × 类有效 rubric）组合上；`[class.X.rubric]` 在场但该类 selector 非 "inline" ⇒ 忽略并 warning（同全局惯例）；④ 类 examples 干跑全局用户 Schema 与 `output.validator`（同现行 few-shot 校验，错误定位写作 `[[class.<name>.annotate.examples]][N]`）。 |
| 时序流（v1.8） | **组合约束**：`segment.enabled = true`（stream 模式总开关）要求 `run.mode = "process"` ∧ `generate.enabled = false`（generate_only 经本表「运行模式」行传递闭合——该行要求 generate.enabled = true，故 stream × generate_only 不可能同时过验）∧ `annotate.enabled = true`（序列记录无 passthrough 输出形态，2.3.1）；`extract.enabled = true` 要求 `segment.enabled = true` ∧ `run.modality = "ui"`（文本序列 v1 不适用）。**`[stream]` 字段**：`stream.order_by` ∈ {"input_order", "meta:<field>"} 且 "meta:*" 仅文本模态；显式设置 `stream.gap_s` / `stream.session_max_span_s` 要求 `order_by = "meta:*"`；`stream.key` 逐元素 ∈ {"meta:<field>"（仅文本模态）, "source_dir"（两模态可用）}。**数值界**：`segment.window ≥ 2`；`2 ≤ annotate.sequence_frames ≤ 100`（越界报 CONFIG_ERROR）。**引用集四处（S30）**：v1.8 起 profile 引用集口径为四处——密钥解析（本表「API Key」行）/ vision 校验 / `--probe` / 存在性（v1.7 分类行「三处」口径的显式扩展）：`segment.llm` **仅** `segment.enabled` ∧ `segment.strategy ∈ {llm, hybrid}` 时计入密钥解析 / `--probe` / 存在性三处（rules 策略零 LLM 调用，不得强制配键），**恒不入 vision 校验集**（v1.11 修订（V3）：v1.8–v1.10 为「仅 `segment.use_vision = true` 时入」，该键已移除——segment 从「要求视觉」改为「适配视觉」，校验命题失去可失败性；附图由解析产物 `vision_resolved` 推导，见本表上下文预算行；报错文案的 stages 集合中 "segment" 自 v1.11 不再可能出现）；`extract.llm` 启用时**恒**计入四处且恒入 vision 集（每转移一请求 2 图，无纯文本档）。**vision 逐阶段表（S30）**：UI 模态 ∧ `segment.enabled = true` 时取代本表「Profile 引用」行的整体 vision 规则——classify ✓（首帧截图，3.13.3）、annotate ✓（多图序列模板，3.5.2）、verify ✓（首末帧截图，3.7.2）、extract ✓（恒）、segment ✗ 恒不要求（v1.11 修订（V1/V3）：原「仅 `use_vision = true` 时 ✓」——附图改由 `vision_resolved` 能力推导自动适配，非校验要求）、**quality ✗**（序列打分纯文本——放宽项，3.4.3 序列行；v1.9 起 **stitch 亦 ✗** 恒不要求（摘要卡纯文本，3.16.3）——「唯一放宽」措辞自 v1.9 失效，见本表线索缝合行）。**按类白名单**：`[class.<name>.extract]` 可覆盖键仅 `instruction`（扩展本表「按类覆盖合并」行引用的 5.2 白名单表，白名单外键同报 CONFIG_ERROR）；`[class.<name>.segment]` **不存在**——链序 segment 在 classify 之前（3.10.3），成段时类标签尚不存在（链序因果，5.2 注），该表按白名单外键处理。**rubric**：selector 枚举扩为 `"default:text"` \| `"default:ui"` \| `"default:trajectory"`（v1.8，包数据 `default_trajectory.toml`，附录 A.3）\| `"inline"`；空串解析 v1.8 修订（S29）：`segment.enabled = true` ⇒ `""` 解析为 `"default:trajectory"`（两模态一致；用户显式选择器恒优先；按类视图经 base selector 自动继承）；本表「Rubric」行的全部校验（含 pointwise 6 级）对 trajectory rubric 照常适用。 |
| 线索缝合（v1.9） | **组合约束**：`stitch.enabled = true` 要求 `segment.enabled = true`（缝合的输入是 episode；stream 前置约束——process 模式 ∧ generate off ∧ annotate on——经此传递闭合，2.3.1）。**数值界**：`stitch.votes` 为 1 或 ≥3 的奇数（**偶数报 CONFIG_ERROR**——(verdict, thread_ref) 严格多数决需破平局，3.16.4）；`stitch.max_open ≥ 1`、`stitch.digest_max_chars ≥ 1`、`stitch.stale_gap_steps ≥ 0`（越界报 CONFIG_ERROR）；`stitch.bias` ∈ {"conservative","llm"}、`stitch.on_error` ∈ {"keep","fail"}。**引用集**：`stitch.llm` 仅 `stitch.enabled = true` 时计入密钥解析 / `--probe` / 存在性引用集，**不入 vision 校验集**（缝合判定证据为纯文本摘要卡、无视觉档，3.16.3——vision 逐阶段表（S30）增一行：stitch ✗ 恒不要求）。**按类白名单**：`[class.<name>.stitch]` **不存在**——链序 stitch 在 classify 之前（3.10.3），缝合时类标签尚不存在（链序因果，`[class.<name>.segment]` 同则，5.2 注），该表按白名单外键处理。 |
| 时序流警告（v1.8，非阻断） | 同 R8 no-op 分级家族（对齐分类行「留配置、关开关」惯例），均 warning 一次、不报 error：① `[stream]` / `[segment]` / `[extract]` / `[stitch]`（v1.9 增）任一节在场而 `segment.enabled = false` ⇒ 点名被忽略的表；② `segment.strategy = "rules"` ∧ 显式 `noise_filter = true` ⇒ no-op（rules 下 noise_filter / min_len 不生效，3.14）；③ `annotate.sequence_frames` 显式设置而 `segment.enabled = false` ⇒ no-op；④ 有效 rubric 为 trajectory（含空串解析所得）而 `extract.enabled = false` ⇒ 组合提示（rubric 模态中立、不预设步骤在场——「步骤」退化读作「帧间变化」，S29，3.4.3 序列行）；⑤ `stream.session_max_len > run.batch_size` ⇒ 静态 WARN（S21：此类会话将被 M10 硬切 + `session_split` 标，3.10.3）；⑥ `annotate.sequence_frames > 20` ∧ 所引 annotate profile `max_image_px > 2000` ⇒ WARN（S28：Anthropic 对 >20 图请求中任一图 >2000px 返回 400 硬拒（非缩放），默认 max_image_px = 2048 恰在拒绝域——指引改 ≤ 2000 或降 sequence_frames；20 图阈值按请求内全部 image block 计；openai_compatible 无此联动、不设独立上限）。（v1.9 增两条，同分级：⑦ `segment.enabled = true` ∧ `stitch.enabled = false` 而 `[stitch]` 节有 payload ⇒ **单独** no-op warning（`annotate.sequence_frames` 显式设置的同形制，不落 ① 的点名名单分支——① 归属 segment 关闭分支）；⑧ `stitch.enabled = true` ∧ `segment.strategy = "rules"` ⇒ 组合提示（规则粗切段未经语义精化，缝合证据质量下降，3.16）。）（v1.11 增一条，同分级：⑨ `vision_resolved` ∧ `segment.window > 20` ∧ 所引 segment profile `max_image_px > 2000` ⇒ WARN（V5：⑥ 的 S28 姊妹——同一 Anthropic「>20 图 ∧ 单图 >2000px」400 硬拒域，⑥ 只盖 annotate.sequence_frames、本条盖 segment 窗口多图；默认 window = 20 恰在边界内侧，不触发）。） |
| console（v1.10） | `console.mode` ∈ {"auto","rich","plain"}；`console.refresh_hz` ∈ [1,10]（越界 = CONFIG_ERROR）；`console.heartbeat_s ≥ 0`（< 0 = CONFIG_ERROR）；`estimate` / `interactive` 为 bool（第 5 章字段表即校验依据）。**解析产物**：load() 收尾把 auto 判定链（7.7——stderr TTY ∧ log_format ∧ TERM ∧ `importlib.util.find_spec("rich")` 探测，不真 import）冻结为 `ConsoleConfig.mode_resolved` ∈ {"rich","plain"}。**警告（非阻断，独立于 R8 家族）**：`tool.log_format = "jsonl"` ∧ 显式 rich（CLI `--console rich` 或 config `console.mode = "rich"`）⇒ WARN 一次 + 强制 plain（7.7 铁律；5.1）。 |
| 上下文预算与视觉推导（v1.11） | **`context_window` 校验（V6；llm 与 embedding profile，5.1）**：`0` 合法（未声明 = 该 profile 预算关闭，行为与 v1.10 一致）；负值报 CONFIG_ERROR；> 0 时须 `context_window > max_output_tokens + margin`（`margin = max(256, ceil(0.10 × context_window))`，3.9.5；embedding 无输出预留，预算 = `context_window − margin` 须为正），否则报 CONFIG_ERROR（预算非正）。**引用 WARN（V6）**：被启用阶段引用的 profile 未声明 `context_window` ⇒ 一次性 WARN（含建议值指引；非阻断——该 profile 预算关闭）。**`default_image_px` 校验（V18，5.1）**：`0` 合法（沿用 `max_image_px`）；> 0 时须 ≤ `max_image_px`，否则报 CONFIG_ERROR。**移除键定向报错（V2）**：`[segment]` 内显式出现 `use_vision` ⇒ CONFIG_ERROR（文案 = 5.2 移除行的迁移指引：键已移除、附图改由 `segment.llm` 所指 profile 的 `supports_vision` 自动决定、需纯文本请指向纯文本 profile），**不走**本表「TOML 结构」行「未知键报 warning」的前向兼容路径——实现机制 = loader 既有**原始节探针**先例（V27②，`segment_provided` 同款）：解析删除后于原始 `[segment]` dict 上探键存在性。**解析产物（V1）**：load() 收尾以 `dataclasses.replace` 冻结 `SegmentConfig.vision_resolved = (modality=="ui") ∧ segment.enabled ∧ strategy∈{llm,hybrid} ∧ llm_profiles[segment.llm].supports_vision`（解析产物家族第二员，`ConsoleConfig.mode_resolved` 先例——本表 console 行）。**segment 装填静态护栏（V9）**：`w_min = ⌊(input_budget − est_static_system) / per_frame_max⌋`（最坏保证装填量：per_frame_max = est_text(digest_max_chars 最坏串) + DIFF_MAX_TOKENS + 每图成本先验（仅 vision_resolved 时计），3.9.5；未声明预算时 w_min = window、本护栏不触发）；`w_min < floor` ⇒ CONFIG_ERROR，`floor = 3 if (verify.enabled ∧ verify.policy == "repair" ∧ segment.enabled) else 2`（保证任意帧装得进 floor 帧窗与 verify 三帧回收复裁窗——运行期装填与复裁由此永不失败；policy="drop" 不构造复裁窗、不做三帧要求）；`w_min == floor` ⇒ WARN（窗数放大退化警示：每帧皆接缝、逐帧双裁决）；w_min 随启动 INFO 打印（V13①，M10 启动段）。**静态系统侧预检（V13③）**：每个启用阶段的静态 prompt 部件（模板头 + instruction + rubric/类表/schema/few-shot——模板头经 V22 冻结常数 `TEMPLATE_HEAD_TOKENS` 取得，其余从 ResolvedConfig 直取，3.9.5）est ≥ input_budget ⇒ CONFIG_ERROR（任何记录都装不下、必错无疑），> 50% ⇒ WARN（系统侧过半、单记录可用空间减半的质量退化预警）。预算类校验仅对声明了 `context_window` 的被引用 profile 执行。 |
| API Key | 每个被引用 profile 的 `api_key_env` 环境变量必须存在且非空。v1.6 密钥池：`api_key_env` 与 `api_key_envs`（5.1）恰提供其一（两者皆有或皆无均报错）；`api_key_envs` 须为非空数组、逐项非空且互异；被引用 profile 的**每个**列出变量均须存在且非空（逐个缺失逐条聚合报错）。M1 归一化：标量形式解析为长度 1 的密钥池，运行时只有一条代码路径（3.9.3 密钥池行）。 |
| 用户 Schema | 必须是合法 JSON 且通过 JSON Schema draft 2020-12 元 Schema 校验（jsonschema 库 `Draft202012Validator.check_schema`）；顶层 type 必须为 object；顶层不得声明保留键 `_meta`。 |
| Rubric | criteria 非空、key 唯一且为 `[a-z0-9_]+`、weight > 0；pointwise 模式要求每条 criterion 提供 `pointwise_levels`（恰好 6 级，0–5）。 |
| 阶段组合 | 2.3.1 节的四条组合约束（①–④；④ 与本表「运行模式」行联动）。 |
| 路径 | process 模式：input 存在且可读，且 output 不得位于 input 目录内部（防止自吞）；generate_only 模式无 input，本行仅执行 output 检查（见「运行模式」行）。两种模式均要求 output 父目录存在且可写。 |

### 3.1.5 错误处理

所有校验错误聚合为一个 `ConfigError`，逐条打印（格式 `config.toml:[llm.default].timeout_s: 期望正整数，得到 "abc"`；数组表元素定位写作 `[[rubric.criteria]][N]`，N 为 1 起序号），退出码 2。不存在运行期配置错误——这是 M1 对其他模块的契约。

**背书：**「声明式配置 + 启动期全量校验 + 运行期只读」是 Data-Juicer 配方（recipe）体系 [4] 与 distilabel Pipeline 先验校验（在任何推理发生前校验 DAG 与列契约）[5] 的共同设计；TOML 双文件分层对应其「系统配置 / 数据配方」分离。

### 3.1.6 输入 / 输出示例

贯穿示例（文本模态）：对输入法采集的中文指令做意图标注，输入数据行形如 `{"instruction": "帮我写一条请假条，明天上午要去医院", "source": "ime-log", "ts": "2026-06-30T10:12:00Z"}`（格式规格见 6.1）。注意：M1 不读取数据内容，仅按 3.1.4 校验 input 路径存在且可读。

#### 示例 ①：一次成功装载 —— 三源合并与优先级生效

```
$ export LABELKIT_KEY_DEFAULT=sk-********
$ labelkit run --config config.toml --project project.toml --limit 100
```

config.toml 沿用 5.1 完整示例（含 `[llm.default]` 与 `[llm.judge]` 两个 profile），并在 `[llm.default]` 中显式写入 `temperature = 0.0`；project.toml 节选：

```
# ─── project.toml（意图标注工程，节选）───
schema_version = 1

[run]
input = "./ime-logs/2026-06-30.jsonl"
output = "./out/intent-0630.jsonl"
modality = "text"
batch_size = 128                    # 覆盖内置默认 256

[input]
text_field = "instruction"

[annotate]
llm = "default"
instruction = "你是输入法指令理解标注员。判断每条用户指令的意图类别、话题与完成难度。"

[output]
schema_inline = """
{"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
 "properties": {
   "intent": {"type": "string", "enum": ["writing_assist", "qa", "translation", "chitchat", "other"]},
   "topic": {"type": "string"},
   "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]}},
 "required": ["intent", "topic", "difficulty"], "additionalProperties": false}
"""
```

三源合并后的 `ResolvedConfig` 摘录（冻结后运行期只读；`# ←` 标注每项的生效来源）：

```
run.batch_size              = 128             # ← project.toml [run]（覆盖内置默认 256）
run.seed                    = 0               # ← 内置默认（两文件与 CLI 均未提供）
limit                       = 100             # ← CLI --limit（最高优先级：CLI 参数 > project.toml > config.toml）
tool.log_level              = "info"          # ← config.toml [tool]（CLI 未传 --log-level，不触发覆盖）
llm.default.temperature     = 0.0             # ← config.toml [llm.default]（project.toml 无对应覆盖键）
llm.default.max_concurrency = 8               # ← config.toml [llm.default]
annotate.llm                = "default"       # ← 内置默认（已校验 profile 存在于 config.toml [llm.*]）
quality.rubric              = "default:text"  # ← 内置默认（缺省按 run.modality = "text" 自动选定）
```

API Key 校验按「被引用 profile」收敛：本工程 generate/verify 均未启用、quality 与 annotate 均引用 default，故 M1 只要求 `LABELKIT_KEY_DEFAULT` 存在且非空；`[llm.judge]` 未被引用，`LABELKIT_KEY_JUDGE` 缺失不报错（3.1.4）。

#### 示例 ②：聚合校验失败 —— 3 个典型错误一次性反馈

在示例 ① 的 project.toml 上引入 3 处错误（节选，其余内容不变）：

```
[quality]
llm = "fast"                        # 错误①：config.toml 只声明了 [llm.default] 与 [llm.judge]
rubric = "inline"

[[rubric.criteria]]
key = "intent_clarity"
weight = 2.0
description = "指令意图是否清晰可辨"
pairwise_prompt = "哪条指令的意图更清晰？"

[[rubric.criteria]]
key = "Topic-Match"                 # 错误③：含大写与连字符，违反 [a-z0-9_]+
weight = 1.0
description = "话题是否明确可归类"
pairwise_prompt = "哪条指令的话题更明确、更易归类？"

[output]
schema_inline = """
{"type": "object",
 "properties": {
   "intent": {"type": "string", "enum": ["writing_assist", "qa", "translation", "chitchat", "other"]},
   "topic": {"type": "string"},
   "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]},
   "_meta": {"type": "object"}},
 "required": ["intent", "topic", "difficulty"], "additionalProperties": false}
"""
# ↑ 错误②：properties 顶层声明了保留键 _meta（3.1.4 禁止；该键为 6.3 输出信封，由工具写入）
```

M1 按 3.1.5 规定聚合为单个 `ConfigError` 逐条打印（不在首错即停，且未发起任何网络请求）：

```
$ labelkit run --config config.toml --project project.toml --limit 100
ConfigError: 3 个配置错误（全量聚合反馈）
project.toml:[quality].llm: 引用的 profile "fast" 不存在于 config.toml [llm.*]，可用：default、judge
project.toml:[output].schema_inline: 用户 Schema 顶层不得声明保留键 "_meta"（6.3 信封字段由工具写入），得到 properties 含 "_meta"
project.toml:[[rubric.criteria]][2].key: 期望匹配 [a-z0-9_]+，得到 "Topic-Match"
$ echo $?
2
```

三条错误分属 3.1.4 校验表的「Profile 引用」「用户 Schema」「Rubric」三类，按该表行序输出。`labelkit validate --config config.toml --project project.toml` 会产生完全相同的错误清单与退出码 2（2.4），适合在提交长任务前做零成本的纯本地检查。

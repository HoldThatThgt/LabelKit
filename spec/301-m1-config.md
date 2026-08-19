## 3.1 M1 配置管理 config

### 3.1.1 职责与边界

**做：**装载并语法/语义校验 config.toml 与 project.toml；合并 CLI 覆盖项；解析 rubric（内联或默认包）；装载并预校验用户 JSON Schema；读取 API Key 环境变量；产出全局唯一的不可变 `ResolvedConfig`。 
**不做：**不接触输入数据；不发起网络请求（`--probe` 连通性探测委托 M9）；运行期不提供任何可变配置。

### 3.1.2 输入 / 输出

| 方向 | 内容 |
|---|---|
| 输入 | config.toml 路径、project.toml 路径、CLI 参数字典（input/output/limit/strict/log_level/dry_run）、进程环境（仅读取 profile 声明的 `api_key_env` / `api_key_envs`（v1.6）所列变量）。 |
| 输出 | `ResolvedConfig`（frozen dataclass 树：第 5 章两文件全部键的类型化镜像，另含 CLI 专属项 limit/strict/dry_run 与 log_level 覆盖，以及 load() 收尾冻结的解析产物：`ConsoleConfig.mode_resolved`（v1.10，3.1.4 console 行）、`SegmentConfig.vision_resolved`（v1.11，3.1.4 上下文预算与视觉推导行）、`FrameClassifyConfig.vision_resolved`（v1.12，3.1.4 帧粒度配置行）；v1.12 增四字段——`frame_classify: FrameClassifyConfig`、`frame_annotate: FrameAnnotateConfig`、`frame_class_views: Mapping[str, FrameClassView]`（键 = 帧类名，仅 `frame.classify.enabled` 时物化，零覆盖类也各得一份视图——`class_views` 同款、运行期零回退）、`frame_schema: Mapping \| None`（帧级输出 Schema 解析产物，`user_schema` 同胞：元校验 + few-shot 干跑；`frame.annotate` 关闭时恒 None）——四字段带默认值（有意偏离 stream/stitch 的「必填无默认」惯例：全关默认 = 字节等价 v1.11，既有构造点零波及；loader 恒显式传入）；v1.13 增第五个带默认字段 `generate_stream: GenerateStreamConfig`（时间流生成形态，默认关 = 字节等价 v1.12，沿用同一惯例），另有三处既有结构的扩字段——`GenerateConfig` 增 `sequences`/`len_range`（按类配额与序列长度区间的载体）、`ClassView` 增 `schema`（按序列类标注 Schema 解析产物，None = 回落全局）、`FrameClassView` 增 `gen_instruction`/`gen_schema`（帧类内容契约，后者 None = 纯文本帧）），或抛出 `ConfigError`（附带全部而非首个校验错误，一次性反馈）。 |

### 3.1.3 API

```
def load(config_path: Path, project_path: Path, cli_overrides: CliOverrides) -> ResolvedConfig:
    """三源合并 + 全量校验。失败抛 ConfigError(errors: list[str])，CLI 以退出码 2 结束。"""

def default_rubric(name: Literal["default:text", "default:ui", "default:trajectory"]) -> Rubric:
    """从包内数据文件（labelkit/data/rubrics/*.toml）装载系统默认 rubric。
       "default:trajectory" 为 v1.8 轨迹 rubric（default_trajectory.toml，附录 A.3）。"""
```

**模块内文件组织（2026-08-14 代码规则整改）：**公开面仍是 `labelkit/common/config/` 包导出的
`load` / `default_rubric` / `ResolvedConfig` 三个名字，实现按职责拆为「公开入口 + 六个包内私有模块」：

```
labelkit/common/config/
├── __init__.py      公开再导出：load / default_rubric / ResolvedConfig
├── model.py         全部配置 dataclass（分工不变）
├── loader.py        公开入口：三源合并驱动 → console 模式裁定 → ResolvedConfig 装配
│   ├── _collect     错误/警告聚合器与类型化表读取（全程共用）
│   ├── _sections    逐节 TOML 解析 → 各配置 dataclass
│   └── _constraints 跨节组合约束与解析产物冻结，内部再调用：
│       ├── _schemas     用户 / 帧 / 按类 Schema 元校验 + few-shot 干跑
│       ├── _rubrics     内联 rubric 解析与 default:* 包数据装载
│       └── _classviews  [class.*] / [frame.class.*] 白名单合并为按类视图
└──（六个下划线开头的模块均为包内私有，外部只经 loader 的公开入口进入）
```

拆分只搬代码不改语义：校验顺序、错误文案、聚合反馈行为与 `ResolvedConfig` 结构均与拆分前一致
（model.py 承载全部配置 dataclass 的分工不变）。

### 3.1.4 校验规则（启动时全量执行）

| 类别 | 规则 |
|---|---|
| TOML 结构 | 两文件均须含 `schema_version = 1`；未知键报 warning（前向兼容）、缺失必填键报 error；类型逐字段核对（第 5 章字段表即校验依据）。v1.7 例外：`[classify]` 与 `[class.*]` 为显式接管的节——`[class.*]` 内白名单（5.2 按类覆盖白名单表）之外的键报 `CONFIG_ERROR` 而非 warning（见下方「按类覆盖合并」行）。 |
| Profile 引用 | `quality.llm / annotate.llm / generate.llms（数组，逐元素校验）/ verify.llm / output.repair_llm`，以及 `quality.judges / verify.judges`（数组，非空时须为奇数个）引用的 profile 必须存在于 config.toml `[llm.*]`；启用视觉输入的阶段（UI 模态的 quality/annotate/verify）要求其 profile `supports_vision = true`。v1.2：`dedup.semantic = true` 时 `dedup.semantic_embedding` 必须存在于 config.toml `[embedding.*]` 且其密钥配置通过本表「API Key」行校验（v1.6：`api_key_env` / `api_key_envs` 恰其一；5.1）。 |
| 交叉字段约束（v1.2） | `quality.selection = "top_ratio"` 时 `quality.top_ratio` 必填且 ∈ (0,1]，且不得再设 `quality.threshold`（互斥，报 CONFIG_ERROR）；`annotate.self_consistency` 为 0 或 ≥3 的奇数；`generate.mixture = "weighted"` 时 `generate.weights` 必填、逐项为正且长度 = `generate.llms`；`[[generate.styles]]` 各项 name 表内唯一、prompt 非空（5.2 各行标注的 M1 校验在此汇总执行）。 |
| 运行模式（v1.4；v1.13 三态） | `run.mode="generate_only"` 时：`run.input` 必须缺省、`run.modality` 必须 "text"、`generate.enabled` 必须 true。**形态判定三态**（v1.13）：`generate_stream.enabled = true` ⇒ **时间流形态**——种子池/独立计数两族键均不适用（`seed_examples` / `standalone_count` 的「恰好提供其一」规则**在本形态下不执行**，配额改由 `[class.<name>.generate].sequences × len_range` 承载；显式书写该两键连同 `num_per_record` / `seeds_per_call` 由本表「时间流生成」行定向报错）；否则维持 v1.4 二态——`generate.seed_examples`（非空字符串数组，逐项非空）与 `generate.standalone_count`（≥ 1）**恰好提供其一**（互斥，分别对应种子池 / 无种子形态）。process 模式下这两键均不得设置。另：`generate.instruction` 的「enabled 时必填」在时间流形态下退化为可选默认——任务描述放在按类生成指令上，「参与类 instruction 非空」由「时间流生成」行按类裁定。 |
| 分类（v1.7） | `classify.enabled = true` 时：`[[classify.classes]]` ≥ 2 项，每项 `name` 匹配 `[a-z0-9_]+` 且表内唯一、`description` 非空、`examples`（可选）为字符串数组；`classify.fallback_class` 必填且 ∈ classes；`classify.assignment` ∈ {"single","multi"}；`classify.max_labels` 仅 multi 可设且 ∈ [2, 类别数]（缺省解析后回填为类别数）；`classify.self_consistency` 为 0 或 ≥3 的奇数；`classify.on_error` ∈ {"fallback","fail"}；`classify.llm` 引用的 profile 必须存在（UI 模态须 `supports_vision = true`），并计入密钥解析、vision 校验与 `--probe` 三处 profile 引用集（本表「Profile 引用」「API Key」行同法覆盖）。`classify.enabled = false` 而 `[[classify.classes]]` / `[class.*]` 在场 ⇒ warning（一次、点名被忽略的表——「留配置、关开关」合法，对齐 top_ratio 未生效等 no-op 键分级惯例），不报 error。 |
| 按类覆盖合并（v1.7） | `[class.<name>.<section>]` 的 `<name>` 必须 ∈ classes；覆盖键 ∈ 白名单（5.2 按类覆盖白名单表），白名单外键报 `CONFIG_ERROR`（本表「TOML 结构」行「未知键报 warning」的显式例外）。合并语义（启动时静态合并、冻结为 `class_views`，运行期零查找）：① 逐键 provenance 合并——类显式提供的键覆盖全局、未提供的键继承全局；② **选择组**——类显式提供 selection / threshold / top_ratio 任一 ⇒ 合并视图剔除全局侧的互斥对键，threshold 与 top_ratio 互斥校验跑在**合并后视图**上（防止「全局 threshold + 类 top_ratio」逐键 replace 后两键并存的误报）；③ **rubric**——合并 selector 后重解析为该类有效 rubric，pointwise 6 级校验跑在（类有效 mode × 类有效 rubric）组合上；`[class.X.rubric]` 在场但该类 selector 非 "inline" ⇒ 忽略并 warning（同全局惯例）；④ 类 examples 干跑**该类有效 Schema** 与全局 `output.validator`（v1.13 修正：此前恒过全局用户 Schema——类自带标注 Schema 时会误判；类自带 Schema 时继承来的全局示例也按类 Schema 复跑一遍，因为运行期就是按类 Schema 发出去的。错误定位写作 `[[class.<name>.annotate.examples]][N]`，Schema 定位前缀相应取 `[class.<name>.annotate].schema_*`；类自带 Schema 的 `$ref` 死链只使该类停跑，不牵连全局层）。⑤ **按类标注 Schema（v1.13）**——`[class.<name>.annotate]` 白名单增 `schema_path` / `schema_inline`（**至多其一**：两者皆缺 = 未声明覆盖，回落全局 `output.schema`；同时声明报 CONFIG_ERROR），声明了即走 `output.schema` 全套装载分支（读取 / JSON 解析 / draft 2020-12 元校验 / 顶层 type = object）+ `_meta` 保留键禁令 + `$ref` 可解析性遍历；解析产物挂 `ClassView.schema`（None = 无覆盖）。⑥ **按类档位表（v1.15）**——`[class.<name>.generate]` 白名单增 `tiers`（数组表，行结构同全局 `[[generate.stream.tiers]]` 三键），语义为**表级原子覆盖**（声明了就用类的整张表、不逐行合并，未声明 = 回落全局表，显式空表报 CONFIG_ERROR）；解析复用全局表同一实现（rank 升序存放，正整数与 `weight ≥ 1` 解析期强制），报错定位串取 `[class.<name>.generate].tiers`；解析产物挂 `ClassView.tiers`（None = 未声明），零覆盖类经继承同得 `None`。前提三子款与逐生效表化校验见本表「按类档位表」行。 |
| 时序流（v1.8） | **组合约束**：`segment.enabled = true`（stream 模式总开关）要求 `run.mode = "process"` ∧ `generate.enabled = false`（generate_only 经本表「运行模式」行传递闭合——该行要求 generate.enabled = true，故 stream × generate_only 不可能同时过验）∧ `annotate.enabled = true`（序列记录无 passthrough 输出形态，2.3.1）；`extract.enabled = true` 要求 `segment.enabled = true` ∧ `run.modality = "ui"`（文本序列 v1 不适用）。**`[stream]` 字段**：`stream.order_by` ∈ {"input_order", "meta:<field>"} 且 "meta:*" 仅文本模态；显式设置 `stream.session_max_span_s` 要求 `order_by = "meta:*"`（违反报 CONFIG_ERROR）；显式设置 `stream.gap_s` 而非 meta 序 ⇒ warning 一次（非阻断，键不生效）；`stream.key` 逐元素 ∈ {"meta:<field>"（仅文本模态）, "source_dir"（两模态可用）}。**数值界**：`segment.window ≥ 2`；`2 ≤ annotate.sequence_frames ≤ 100`（越界报 CONFIG_ERROR）。**引用集四处（S30）**：v1.8 起 profile 引用集口径为四处——密钥解析（本表「API Key」行）/ vision 校验 / `--probe` / 存在性（v1.7 分类行「三处」口径的显式扩展）：`segment.llm` **仅** `segment.enabled` ∧ `segment.strategy ∈ {llm, hybrid}` 时计入密钥解析 / `--probe` / 存在性三处（rules 策略零 LLM 调用，不得强制配键），**恒不入 vision 校验集**（v1.11 修订（V3）：v1.8–v1.10 为「仅 `segment.use_vision = true` 时入」，该键已移除——segment 从「要求视觉」改为「适配视觉」，校验命题失去可失败性；附图由解析产物 `vision_resolved` 推导，见本表上下文预算行；报错文案的 stages 集合中 "segment" 自 v1.11 不再可能出现）；`extract.llm` 启用时**恒**计入四处且恒入 vision 集（每转移一请求 2 图，无纯文本档）。**vision 逐阶段表（S30）**：UI 模态 ∧ `segment.enabled = true` 时取代本表「Profile 引用」行的整体 vision 规则——classify ✓（首帧截图，3.13.3）、annotate ✓（多图序列模板，3.5.2）、verify ✓（首末帧截图，3.7.2）、extract ✓（恒）、segment ✗ 恒不要求（v1.11 修订（V1/V3）：原「仅 `use_vision = true` 时 ✓」——附图改由 `vision_resolved` 能力推导自动适配，非校验要求）、**quality ✗**（序列打分纯文本——放宽项，3.4.3 序列行；v1.9 起 **stitch 亦 ✗** 恒不要求（摘要卡纯文本，3.16.3）——「唯一放宽」措辞自 v1.9 失效，见本表线索缝合行）。**按类白名单**：`[class.<name>.extract]` 可覆盖键仅 `instruction`（扩展本表「按类覆盖合并」行引用的 5.2 白名单表，白名单外键同报 CONFIG_ERROR）；`[class.<name>.segment]` **不存在**——链序 segment 在 classify 之前（3.10.3），成段时类标签尚不存在（链序因果，5.2 注），该表按白名单外键处理。**rubric**：selector 枚举扩为 `"default:text"` \| `"default:ui"` \| `"default:trajectory"`（v1.8，包数据 `default_trajectory.toml`，附录 A.3）\| `"inline"`；空串解析 v1.8 修订（S29）：`segment.enabled = true` ⇒ `""` 解析为 `"default:trajectory"`（两模态一致；用户显式选择器恒优先；按类视图经 base selector 自动继承）——**v1.13 条件扩为 `segment.enabled ∨ generate_stream.enabled`**（本表「时间流生成」行）；本表「Rubric」行的全部校验（含 pointwise 6 级）对 trajectory rubric 照常适用。**`[stream]` 在时间流生成形态的语义翻转**（v1.13）：该节由「摄取侧声明」兼作**生成侧铺设契约**——`order_by` 必须 `"meta:<字段>"`（声明工件行的时间戳字段名）、`gap_s` 定会话间隔下界、`session_max_len` 定织造上限，而 `key` 与 `gap_steps` 必须取空/0（本表「时间流生成」行 ⑤）。 |
| 线索缝合（v1.9） | **组合约束**：`stitch.enabled = true` 要求 `segment.enabled = true`（缝合的输入是 episode；stream 前置约束——process 模式 ∧ generate off ∧ annotate on——经此传递闭合，2.3.1）。**数值界**：`stitch.votes` 为 1 或 ≥3 的奇数（**偶数报 CONFIG_ERROR**——(verdict, thread_ref) 严格多数决需破平局，3.16.4）；`stitch.max_open ≥ 1`、`stitch.digest_max_chars ≥ 1`、`stitch.stale_gap_steps ≥ 0`（越界报 CONFIG_ERROR）；`stitch.bias` ∈ {"conservative","llm"}、`stitch.on_error` ∈ {"keep","fail"}。**引用集**：`stitch.llm` 仅 `stitch.enabled = true` 时计入密钥解析 / `--probe` / 存在性引用集，**不入 vision 校验集**（缝合判定证据为纯文本摘要卡、无视觉档，3.16.3——vision 逐阶段表（S30）增一行：stitch ✗ 恒不要求）。**按类白名单**：`[class.<name>.stitch]` **不存在**——链序 stitch 在 classify 之前（3.10.3），缝合时类标签尚不存在（链序因果，`[class.<name>.segment]` 同则，5.2 注），该表按白名单外键处理。 |
| 时序流警告（v1.8，非阻断） | 同 R8 no-op 分级家族（对齐分类行「留配置、关开关」惯例），均 warning 一次、不报 error：① `[stream]` / `[segment]` / `[extract]` / `[stitch]`（v1.9 增）任一节在场而 `segment.enabled = false` ⇒ 点名被忽略的表；② `segment.strategy = "rules"` ∧ 显式 `noise_filter = true` ⇒ no-op（rules 下 noise_filter / min_len 不生效，3.14）；③ `annotate.sequence_frames` 显式设置而 `segment.enabled = false` ⇒ no-op；④ 有效 rubric 为 trajectory（含空串解析所得）而 `extract.enabled = false` ⇒ 组合提示（rubric 模态中立、不预设步骤在场——「步骤」退化读作「帧间变化」，S29，3.4.3 序列行）；⑤ `stream.session_max_len > run.batch_size` ⇒ 静态 WARN（S21：此类会话将被 M10 硬切 + `session_split` 标，3.10.3）；⑥ `annotate.sequence_frames > 20` ∧ 所引 annotate profile `max_image_px > 2000` ⇒ WARN（S28：Anthropic 对 >20 图请求中任一图 >2000px 返回 400 硬拒（非缩放），默认 max_image_px = 2048 恰在拒绝域——指引改 ≤ 2000 或降 sequence_frames；20 图阈值按请求内全部 image block 计；openai_compatible 无此联动、不设独立上限）。（v1.9 增两条，同分级：⑦ `segment.enabled = true` ∧ `stitch.enabled = false` 而 `[stitch]` 节有 payload ⇒ **单独** no-op warning（`annotate.sequence_frames` 显式设置的同形制，不落 ① 的点名名单分支——① 归属 segment 关闭分支）；⑧ `stitch.enabled = true` ∧ `segment.strategy = "rules"` ⇒ 组合提示（规则粗切段未经语义精化，缝合证据质量下降，3.16）。）（v1.11 增一条，同分级：⑨ `vision_resolved` ∧ `segment.window > 20` ∧ 所引 segment profile `max_image_px > 2000` ⇒ WARN（V5：⑥ 的 S28 姊妹——同一 Anthropic「>20 图 ∧ 单图 >2000px」400 硬拒域，⑥ 只盖 annotate.sequence_frames、本条盖 segment 窗口多图；默认 window = 20 恰在边界内侧，不触发）。） |
| console（v1.10） | `console.mode` ∈ {"auto","rich","plain"}；`console.refresh_hz` ∈ [1,10]（越界 = CONFIG_ERROR）；`console.heartbeat_s ≥ 0`（< 0 = CONFIG_ERROR）；`estimate` / `interactive` 为 bool（第 5 章字段表即校验依据）。**解析产物**：load() 收尾把 auto 判定链（7.7——stderr TTY ∧ log_format ∧ TERM ∧ `importlib.util.find_spec("rich")` 探测，不真 import）冻结为 `ConsoleConfig.mode_resolved` ∈ {"rich","plain"}。**警告（非阻断，独立于 R8 家族）**：`tool.log_format = "jsonl"` ∧ 显式 rich（CLI `--console rich` 或 config `console.mode = "rich"`）⇒ WARN 一次 + 强制 plain（7.7 铁律；5.1）。 |
| 上下文预算与视觉推导（v1.11） | **`context_window` 校验（V6；llm 与 embedding profile，5.1）**：`0` 合法（未声明 = 该 profile 预算关闭，行为与 v1.10 一致）；负值报 CONFIG_ERROR；> 0 时须 `context_window > max_output_tokens + margin`（`margin = max(256, ceil(0.10 × context_window))`，3.9.5；embedding 无输出预留，预算 = `context_window − margin` 须为正），否则报 CONFIG_ERROR（预算非正）。**引用 WARN（V6）**：被启用阶段引用的 profile 未声明 `context_window` ⇒ 一次性 WARN（含建议值指引；非阻断——该 profile 预算关闭）。**`default_image_px` 校验（V18，5.1）**：`0` 合法（沿用 `max_image_px`）；> 0 时须 ≤ `max_image_px`，否则报 CONFIG_ERROR。**移除键定向报错（V2）**：`[segment]` 内显式出现 `use_vision` ⇒ CONFIG_ERROR（文案 = 5.2 移除行的迁移指引：键已移除、附图改由 `segment.llm` 所指 profile 的 `supports_vision` 自动决定、需纯文本请指向纯文本 profile），**不走**本表「TOML 结构」行「未知键报 warning」的前向兼容路径——实现机制 = loader 既有**原始节探针**先例（V27②，`segment_provided` 同款）：解析删除后于原始 `[segment]` dict 上探键存在性。**解析产物（V1）**：load() 收尾以 `dataclasses.replace` 冻结 `SegmentConfig.vision_resolved = (modality=="ui") ∧ segment.enabled ∧ strategy∈{llm,hybrid} ∧ llm_profiles[segment.llm].supports_vision`（解析产物家族第二员，`ConsoleConfig.mode_resolved` 先例——本表 console 行）。**segment 装填静态护栏（V9）**：`w_min = ⌊(input_budget − est_static_system) / per_frame_max⌋`（最坏保证装填量：per_frame_max = est_text(digest_max_chars 最坏串) + DIFF_MAX_TOKENS + 每图成本先验（仅 vision_resolved 时计），3.9.5；未声明预算时 w_min = window、本护栏不触发）；`w_min < floor` ⇒ CONFIG_ERROR，`floor = 3 if (verify.enabled ∧ verify.policy == "repair" ∧ segment.enabled) else 2`（在**先验计价**下保证任意帧装得进 floor 帧窗与 verify 三帧回收复裁窗——护栏基于每图成本先验（3.9.5 校准值可合法超过先验 ×1.2，无钳制），校准超先验或退化个案时装填器强制 2 帧封窗、由 M9 终检降到**记录级** `context_overflow`（3.14.4），永不 run 级（v1.11 审计修订）；policy="drop" 不构造复裁窗、不做三帧要求）；`w_min == floor` ⇒ WARN（窗数放大退化警示：每帧皆接缝、逐帧双裁决）；w_min 随启动 INFO 打印（V13①，M10 启动段）。**静态系统侧预检（V13③）**：每个启用阶段的静态 prompt 部件（模板头 + instruction + rubric/类表/schema/few-shot——模板头经 V22 冻结常数 `TEMPLATE_HEAD_TOKENS` 取得，其余从 ResolvedConfig 直取，3.9.5）est ≥ input_budget ⇒ CONFIG_ERROR（任何记录都装不下、必错无疑），> 50% ⇒ WARN（系统侧过半、单记录可用空间减半的质量退化预警）。预算类校验仅对声明了 `context_window` 的被引用 profile 执行。 |
| 帧粒度配置（v1.12） | **组合约束七条（规范源头 2.3.1，全部 CONFIG_ERROR，⑦ 为 warning）**：① 帧粒度要求流模式——`frame.classify.enabled ∨ frame.annotate.enabled` ⇒ `segment.enabled = true`（报错文案指引 `outside stream mode use classify + [class.<name>.annotate] per-class annotation`）；② 帧类覆盖要求帧分类——`[frame.class.*]` 在场 ⇒ `frame.classify.enabled = true`，节名 ⊆ 帧类表、覆盖白名单仅 `annotate` 节三键（instruction / examples / enabled，5.2 白名单表），白名单外键/节报 CONFIG_ERROR（「TOML 结构」行前向兼容的显式例外，`[class.*]` 同族）；③ 帧 Schema 恰一——`frame.annotate.enabled = true` ⇒ `schema_path` / `schema_inline` 恰一 + draft 2020-12 元校验 + `[[frame.annotate.examples]]` 干跑（镜像 `output.schema` 全套分支；帧级**无 L2.5 hook**，干跑仅对帧 Schema）；④ meta_mode 护栏——`frame.*` 任一启用 ⇒ `output.meta_mode != "none"`（帧产物仅经 `_meta.stream.members` 承载，sidecar 合法）；⑤ fallback 合法——`frame.classify.enabled = true` 时 `fallback_class` 必填且 ∈ 帧类表 name 集（传递性地要求类表非空——v1.12 无独立 ≥2 类数下限，与 `[classify]` 有意不同）；⑥ 定向探针——`[frame.classify].assignment` / `[frame.annotate].self_consistency` 显式书写 ⇒ 定向 CONFIG_ERROR（帧级无多标签、无自洽采样；机制 = loader 原始节探针，v1.11 `use_vision` 同款（V27②），不走「未知键报 warning」前向兼容路径）；⑦ no-op 提示——`[frame.*]` 节在场 ∧ 均未启用 ∧ `segment.enabled = false` ⇒ 并入 segment 关闭的 no-op warning 停放清单（R8 家族；任一帧开关启用时由 ① 接管）。**帧类表**：`[[frame.classify.classes]]` 与 `[[classify.classes]]` 同构逐项校验（name 匹配 `[a-z0-9_]+` 且表内唯一、description 非空、examples 可选字符串数组——**解析合法但帧级批量判决模板不渲染**（§10.12 只渲染类表），任一帧类携带 examples 时显名 WARN `[frame.classify].classes: class examples are not rendered by the batched frame-verdict template (§10.12), so this key is ignored`）；帧类表与序列类表相互独立、允许重名、互不约束。**必填**：`frame.annotate.enabled = true` ⇒ `instruction` 非空（5.2 † 家族的帧级镜像）。**帧类覆盖合并**：`[frame.class.<name>.annotate]` 逐键 provenance 合并冻结为 `frame_class_views`（键 = 帧类名；零覆盖类也各得一份视图，「按类覆盖合并」行 `class_views` 同款）；类提供的 examples 对帧级 Schema 干跑（错误定位写作 `[[frame.class.<name>.annotate.examples]][N]`）。**vision 集分列**：`frame.annotate.llm` 在 ui 模态 ∧ enabled 时**无条件**入 vision 必需集（镜像序列级 annotate——截图是标注主证据）；`frame.classify.llm` **永不**入 vision 必需集——附图由解析产物 `FrameClassifyConfig.vision_resolved` = (modality=="ui") ∧ `frame.classify.enabled` ∧ `llm_profiles[frame.classify.llm].supports_vision` 自动推导（load() 收尾以 `dataclasses.replace` 冻结，解析产物家族第三员，segment V1 同款；成本控制面 = 指向纯文本 profile，判决仅凭摘要行）；两键 enabled 时均计入密钥解析 / `--probe` / 存在性引用集（`referenced_profiles`，预算报表经 profile 聚合自动覆盖）。**静态预算预检两段（V13③ 表新增）**：`frame.classify` 段 = 冻结模板头 `TEMPLATE_HEAD_TOKENS["frame_classify"]` + 帧类表文本（name/description，**不计 examples**——口径与渲染事实对齐，多算会误触发预检；`[frame.classify]` 无 instruction 键——提示词模板确定性内建）；`frame.annotate` 段 = 冻结模板头 `TEMPLATE_HEAD_TOKENS["frame_annotate"]` + 帧级 Schema 文本 + max(全局与各帧类视图的 instruction + few-shot)；est ≥ input_budget ⇒ CONFIG_ERROR、> 50% ⇒ WARN（「上下文预算与视觉推导」行同一判则，仅对声明预算的被引用 profile 执行）。**v1.13 放宽两处**：约束② 的前提改为 `frame.classify.enabled ∨ generate_stream.enabled`（时间流生成形态经 `[frame.class.<name>.generate]` 声明帧内容契约，帧类命名空间照常物化 `frame_class_views`），白名单相应扩为**两节**——`annotate` 三键（instruction/examples/enabled）+ `generate` 三键（instruction / schema_path / schema_inline，**仅时间流生成形态合法**，非本形态出现是反向定向 CONFIG_ERROR）；约束⑤ 的 `fallback_class` 必填仅在 `frame.classify.enabled = true` 时执行（时间流形态不启用帧级判决，无兜底对象）。 |
| 时间流生成（v1.13） | **组合约束八条（规范源头 2.3.1，全部 CONFIG_ERROR）**：① 形态前提合取——`generate_stream.enabled = true` ⇒ `run.mode="generate_only"` ∧ `run.modality="text"` ∧ `generate.enabled` ∧ `classify.enabled` ∧ `stream.order_by = "meta:<字段>"` ∧ `output.meta_mode != "none"`，另含**工件键守卫**：`input.text_field` 与 `order_by` 的时间戳字段名均不得含 `"."`（工件行以字段名为**字面顶层键**，点路径在重放摄取时无法往返、整份判坏行，6.5）、两者互不同名、且均不得为 `"truth"`（工件行三个顶层键互斥）；② 类表与配额——序列类表 ≥ 1（放宽自 ≥ 2，仅本形态）∧ `fallback_class` 免填（写了须 ∈ 类表）∧ `Σsequences ≥ 1` ∧ 参与类（有效 `sequences ≥ 1`）的有效 `instruction` 非空 ∧ 帧类表非空 ∧ **每个**帧类的 `[frame.class.<name>.generate].instruction` 非空；③ 禁设键探针——`[generate]` 的 `seed_examples`/`standalone_count`/`num_per_record`/`seeds_per_call` 与 `[class.*.generate]` 的 `num_per_record`/`seeds_per_call` 显式书写、`frame.classify.enabled`/`frame.annotate.enabled` = true ⇒ 定向 CONFIG_ERROR（机制 = v1.11 `use_vision` 原始节探针，不走「未知键报 warning」前向兼容路径；文案给替代面指引）；④ 装箱一致性——`sessions ≥ 1` ∧ `sessions ≤ Σsequences ≤ 2 × sessions` ∧ `duplicates ∈ [0, Σsequences]` ∧ `noise_ratio ∈ [0,1)`（> 0 ⇒ `noise_instruction` 非空）∧ `frame_gap_s` 满足 `0 < lo ≤ hi < stream.gap_s`；⑤ 织造上限与铺设契约——`2 × max(各类 len_range 上界) ≤ stream.session_max_len` ∧ `stream.key == []` ∧ `stream.gap_steps == 0` ∧（`session_max_span_s > 0` 时）`(session_max_len − 1) × frame_gap_s 上界 ≤ session_max_span_s` ∧ `ts_start` 可 `datetime.fromisoformat` 解析；⑥ Schema 元校验——帧类生成 Schema 走 `_load_schema_pair` 全套 + `$ref` 遍历（**无 `_meta` 分支**——帧内容落工件行的文本字段，与 §6.3 信封字段无冲突面），按类标注 Schema 见本表「按类覆盖合并」行 ⑤；⑦ 引用集与豁免——`classify.llm` 在本形态**仅豁免密钥解析集**（序列标签直接继承、classify 零判决调用，援引 S30 先例：零调用不强制配活密钥）；**存在性检查照旧**（拼错 profile 名仍在启动期揪出），`--probe` 引用集亦照旧（`referenced_profiles()` 按 `classify.enabled` 收录——显式探测时该 profile 仍需可连通）；⑧ 停放豁免精确化——`[stream]` 与 `[frame.*]` 在本形态是生效面，移出 R8 no-op 停放清单（`[segment]`/`[stitch]`/`[extract]` 照旧停放告警）。**rubric 空串解析扩展（S29 扩展）**：条件由 `segment.enabled` 扩为 `segment.enabled ∨ generate_stream.enabled` ⇒ `""` 解析为 `"default:trajectory"`（loader 与 M11 镜像两处同步，3.11.2；本形态打的同样是序列分）。注意「trajectory rubric ∧ `extract.enabled = false`」的组合提示 WARN（本表时序流警告行 ④）**在本形态下不出现**——该提示归属 `segment.enabled` 分支，而本形态 segment 恒关；语义上也无提示对象：extract 是 UI 模态专属、本形态恒 text，「步骤」本就读作帧间变化。**静态预算预检两段（V13③ 表新增）**：`generate.stream.plan` 段 = 冻结模板头 `TEMPLATE_HEAD_TOKENS["generate_plan"]` + max(全局与各类视图的生成 instruction) + 帧类表文本；`generate.stream.realize` 段 = 冻结模板头 `TEMPLATE_HEAD_TOKENS["generate_realize"]` + 同一 instruction 项 + `max(len_range 上界) × max(帧类生成 Schema 文本)`（逐位契约把 Schema 文本按步重复）；噪音批量实现复用既有 `generate` 段。判则同「上下文预算与视觉推导」行（est ≥ input_budget ⇒ CONFIG_ERROR、> 50% ⇒ WARN）。**annotate 段口径修订**：该段的 Schema 项改**按类取值**——max 跑在「Schema + instruction + few-shot」的整份和上（无按类 Schema 时逐类回落全局，数值与 v1.12 字节等价）。 |
| 帧类构成档位与时间字段绑定（v1.14） | **档位簇六条（规范源头 2.3.1 v1.14 段，除注明 WARN 外全部 CONFIG_ERROR）**——v1.15 逐生效表化注记：下列 ②③ 的辖区是「一张生效档位表」、④⑤⑥ 的辖区是「逐参与类的生效表」，生效表 = 该类声明的按类表、未声明则为全局表（详见下方「按类档位表」行；无任何按类表时生效表恒 = 全局表，本簇与 v1.14 逐字同义）：① 档位表前提——`[[generate.stream.tiers]]` 在场 ⇒ `generate_stream.enabled = true`（定向报错：本表仅时间流形态合法）；② 档位身份——`tier_rank` 正整数、表内唯一、全表**连续覆盖 1..N**（N = 表长；缺号/重号报错并点名缺失序数），`weight` 整数 ≥ 1；③ 档位构成——`frame_classes` 非空、档内无重复、每名 ∈ 帧类表 name 集，各档构成**集合两两互异**；④ 长度可覆盖——逐 (参与类, 档) **非零配额对**裁定（配分为纯函数，M1 期即可算出逐档配额）：配额 ≥ 1 的每对须满足该类 `len_range` 下界 ≥ `len(该档 frame_classes)`，**零额对豁免**；⑤ 配分零额 ⇒ **WARN**（值-free：类名 + tier_rank + 权重表；非错误）；⑥ 帧类未入档 ⇒ **WARN**（该帧类不会出现在任何蓝图中、其 `[frame.class.<name>.generate]` 面为死配置），同时把本表「时间流生成」行约束② 的「**每个**帧类的生成 instruction 非空」检查域收窄为 **∪各档 frame_classes**（未入档帧类豁免必填，已写照常合法）。**绑定簇三条**：⑦ 绑定表前提——`[frame.class.<name>.generate.time_fields]` 仅结构化帧（该帧类声明了 `schema_path`/`schema_inline`）合法，纯文本帧带绑定表 ⇒ 定向 CONFIG_ERROR；「载荷恒为 JSON 对象」由既有的帧类生成 Schema 顶层 `type = object` 必检承担（本表「用户 Schema」行同族），对「声明过 Schema 源键但装载失败」的帧类本簇保持沉默、不叠加第二错；⑧ 绑定键与类型——每个绑定键 ∈ 该帧类生成 Schema 顶层 `properties`、绑定值 ∈ 语义词表 `{ts, gap_prev_s, gap_next_s, elapsed_s}`、该属性 Schema 的 `type` 关键字**字面恰等**于要求值（`ts` ⇒ `"string"`、其余三值 ⇒ `"number"`；联合类型数组、缺失、经 `$ref`/组合关键字间接声明均判不匹配），绑定字段携带 `type` 以外的约束关键字 ⇒ **WARN**（值-free：帧类名 + 字段名 + 关键字名——那些关键字既不上行也不被强制）；⑨ 剔除余量——生成 Schema 顶层 `properties` 键数 − 绑定键数 ≥ 1（全绑定 ⇒ CONFIG_ERROR）。**解析产物**：`GenerateStreamConfig.tiers`（按 tier_rank 升序存放的 `TierSpec` 元组，空 = 档位面不在场）与 `FrameClassView.time_fields`（None = 无绑定）；`[frame.class.<name>.generate]` 的白名单由三键扩为**四键**（instruction / schema_path / schema_inline / time_fields，本表「帧粒度配置」行 v1.13 放宽段的同步修订——不扩键则子表被白名单循环判为 CONFIG_ERROR）。**微秒地板（v1.13 缺陷修补）**：本表「时间流生成」行约束④ 的 `0 < lo` 收紧为 `lo ≥ 1e-6`（isoformat 精度与 `round(·, 6)` 的分辨率下界；亚微秒 lo 使帧间隔 `timedelta` 取整为 0 微秒，破坏 ts 严格递增并使语义词表的 0.0 边界哨兵失去无歧义性——报错文案给出这两条依据）。**静态预算预检零改动**：「时间流生成」行的 `generate.stream.plan` 段照旧按**全帧类表**计量（档内子集恒 ≤ 全表，上界性质保持），`generate.stream.realize` 段在绑定剔除后只降不升，`TEMPLATE_HEAD_TOKENS` 无新键。 |
| 按类档位表（v1.15） | **按类表前提三子款（规范源头 2.3.1 v1.15 段，全部 CONFIG_ERROR，定位串带类名）**：① **形态门**——`generate_stream.enabled = false` 时任一 `[class.*.generate]` 节写了 `tiers` ⇒ 定向 CONFIG_ERROR（机制 = v1.11 `use_vision` 的**原始节探针**，与 v1.12 帧级两键同族，不走本表「TOML 结构」行的前向兼容 warning；探针在解析删除后于原始 `[class.<name>.generate]` dict 上探键存在性，故表内容本身非法时也照发本错）；② **全局锚**——形态开启、任一按类表在场而全局 `[[generate.stream.tiers]]` 缺省 ⇒ CONFIG_ERROR（档位面总开关恒 = 全局表非空，文案指引补全局表并说明它兼作未声明类的回落）；③ **空表拒收**——显式 `tiers = []`（解析产物为空元组）⇒ CONFIG_ERROR，指引删除该键以回落全局表。**逐生效表化（修订上一行档位簇）**：档位身份连续性（②）与构成合法性（③）改**逐生效来源表**执行——全局表 + 每张已声明按类表各跑一遍，报错定位前缀分别为 `[[generate.stream.tiers]]` 与 `[class.<name>.generate].tiers`，「构成两两互异」的辖区收窄为**单表之内**（跨类同构成合法）；逐 (参与类, 档) 的长度可覆盖约束（④）与配分零额 WARN（⑤）改吃 `effective_tiers(该类表, 全局表)`，WARN 文案的权重清单同取**该类生效表**；「帧类未入档」WARN 与「每帧类生成指令必填」的检查域**并集化**为 ∪（各参与类生效表构成，⑥）——全部参与类都声明按类表时全局表沦为纯锚，其独有帧类照样判死配置。**零额类不豁免结构校验**：`sequences = 0` 的类声明的表照跑本行与 ②③ 的结构校验（坏配置早报），仅 ④⑤ 豁免，且其构成**不入**检查域并集。**解析产物**：`ClassView.tiers`（按 tier_rank 升序存放的 `TierSpec` 元组；`None` = 未声明 ⇒ 回落全局，空元组 = 显式空表 ⇒ 由 ③ 拒收）与纯函数 `effective_tiers(类表, 全局表)`（`apportion_tiers` 旁的零 rng 纯函数，M1 约束簇 / M6 计划期 / M10 报表装配三方共用，3.6.5）；`[class.<name>.generate]` 白名单由六键扩为**七键**（增 `tiers`，5.2 按类覆盖白名单表——不扩键则该数组表被白名单循环判为 CONFIG_ERROR）。**零改动确认**：微秒地板（rule 59 家族）、时间字段绑定簇、`[frame.class.*.generate]` 白名单、帧类表本身的规则、静态预算预检（蓝图段照旧按全帧类表计量，上界性质在按类子集下同样保持）全部不动。 |
| API Key | 每个被引用 profile 的 `api_key_env` 环境变量必须存在且非空。v1.6 密钥池：`api_key_env` 与 `api_key_envs`（5.1）恰提供其一（两者皆有或皆无均报错）；`api_key_envs` 须为非空数组、逐项非空且互异；被引用 profile 的**每个**列出变量均须存在且非空（逐个缺失逐条聚合报错）。M1 归一化：标量形式解析为长度 1 的密钥池，运行时只有一条代码路径（3.9.3 密钥池行）。 |
| 用户 Schema | 必须是合法 JSON 且通过 JSON Schema draft 2020-12 元 Schema 校验（jsonschema 库 `Draft202012Validator.check_schema`）；顶层 type 必须为 object；顶层不得声明保留键 `_meta`。**同族两处**：v1.12 的帧级 Schema（`[frame.annotate].schema_*`，恰一，无 `_meta` 分支）与 v1.13 的**按序列类标注 Schema**（`[class.<name>.annotate].schema_*`，至多其一，`_meta` 禁令与 `$ref` 遍历照旧）走同一套装载分支——三者的解析产物分别是 `user_schema` / `frame_schema` / `ClassView.schema`（本表「按类覆盖合并」行 ⑤、「帧粒度配置」行 ③）。 |
| Rubric | criteria 非空、key 唯一且为 `[a-z0-9_]+`、weight > 0；pointwise 模式要求每条 criterion 提供 `pointwise_levels`（恰好 6 级，0–5）。 |
| 阶段组合 | 2.3.1 节的四条组合约束（①–④；④ 与本表「运行模式」行联动）。 |
| 路径 | process 模式：input 存在且可读，且 output 不得位于 input 目录内部（防止自吞）；generate_only 模式无 input，本行仅执行 output 检查（见「运行模式」行）。两种模式均要求 output 父目录存在且可写。 |

### 3.1.5 错误处理

所有校验错误聚合为一个 `ConfigError`，逐条打印（格式 `config.toml:[llm.default].timeout_s: expected positive integer, got "abc"`；数组表元素定位写作 `[[rubric.criteria]][N]`，N 为 1 起序号），退出码 2。报错文案为英文（2026-08-14 代码规则整改；`<文件>:[节].键:` 定位前缀机器稳定不变），不存在运行期配置错误——这是 M1 对其他模块的契约。

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
ConfigError: 3 config error(s) (all aggregated)
project.toml:[quality].llm: referenced profile "fast" does not exist in config.toml [llm.*], available: default, judge
project.toml:[output].schema_inline: user schema must not declare the reserved top-level key "_meta" (the 6.3 envelope fields are written by the tool), got properties containing "_meta"
project.toml:[[rubric.criteria]][2].key: expected a match of [a-z0-9_]+, got "Topic-Match"
$ echo $?
2
```

三条错误分属 3.1.4 校验表的「Profile 引用」「用户 Schema」「Rubric」三类，按该表行序输出。`labelkit validate --config config.toml --project project.toml` 会产生完全相同的错误清单与退出码 2（2.4），适合在提交长任务前做零成本的纯本地检查。

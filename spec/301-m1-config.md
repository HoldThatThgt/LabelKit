## 3.1 M1 配置管理 config

### 3.1.1 职责与边界

**做：**装载并语法、语义校验 config.toml 与 project.toml；合并 CLI 覆盖项；解析 rubric、用户 JSON Schema 与
project-root hook；冻结路径、按类视图与控制台/视觉推导结果。flat 形态沿用既有配置解析；sequence 形态额外解析
并冻结 `SequenceGenerationConfig`。M1 完成后，由编排层调用 operators 层唯一 program compiler 与 planner。

**不做：**不读取业务输入；不在配置阶段调用 LLM；不在静态装载期间读取 API key value；不为已删除的序列生成字段
提供别名、转换或默认回退；运行期不提供可变配置。

### 3.1.2 输入 / 输出

| 方向 | 内容 |
|---|---|
| 输入 | config.toml 路径、project.toml 路径、CLI 覆盖项，以及不含 secret value 的进程与终端能力信息。 |
| 输出 | `ResolvedConfig` 冻结树；其中 `generate` 只保存通用字段和 flat 配置，`sequence_generation` 仅在 `generate.form = "sequence"` 时保存 `SequenceGenerationConfig`，否则为 null。sequence 形态同时冻结全部输出路径，但不在 common 配置树复制 `GenerationProgram` 或 `ScenarioPlan`；配置错误以完整 `ConfigError` 列表返回。 |
| 运行期凭据 | `RuntimeCredentials` 在 run 或 `validate --probe` 分流后独立物化，不属于 `ResolvedConfig`，validate 与 dry-run 不读取 key value。 |

`ResolvedConfig` 继续保存 process 与 flat 形态需要的 `class_views`、`frame_class_views`、用户/帧 Schema、
`ConsoleConfig.mode_resolved`、`SegmentConfig.vision_resolved` 和
`FrameClassifyConfig.vision_resolved`。sequence 形态的 class view 由 M1 完整冻结，compiler 不再合并配置。

### 3.1.3 API

~~~python
def load(config_path: Path, project_path: Path, cli_overrides: CliOverrides) -> ResolvedConfig:
    """装载三源配置、聚合校验并冻结全部解析产物。"""


def default_rubric(name: Literal["default:text", "default:ui", "default:trajectory"]) -> Rubric:
    """装载包内默认 rubric。"""


def parse_generation_config(
    raw_project: Mapping[str, object],
    context: GenerationParseContext,
) -> SequenceGenerationConfig:
    """解析并校验 sequence 生成配置。"""
~~~

**模块内文件组织：**公开面仍由 `labelkit/common/config/` 导出；新增的 sequence 配置解析单独落
`generation.py`，不把互斥形态塞进一个可空字段长表。

~~~text
labelkit/common/config/
├── __init__.py
├── model.py
├── loader.py
├── generation.py
├── _collect.py
├── _sections.py
├── _constraints.py
├── _schemas.py
├── _rubrics.py
└── _classviews.py
~~~

### 3.1.4 校验规则（启动时全量执行）

| 类别 | 规则 |
|---|---|
| TOML 与类型 | 两文件均须含 `schema_version = 1`；缺失必填键、类型错误及受管节未知键均聚合为 CONFIG_ERROR。第 5 章字段表是唯一字段依据。已删除的序列生成键必须定向报错，不能落入未知键 warning，也不能读取旧值。 |
| Profile 与凭据声明 | 所有启用阶段引用的 LLM/embedding profile 必须存在；视觉请求必须引用 `supports_vision = true` 的 profile。每个 profile 的 `api_key_env` 与 `api_key_envs` 恰有一个，数组非空且元素唯一。静态装载只校验变量名；run 与 `validate --probe` 才聚合读取被引用 profile 的值。 |
| 通用阶段组合 | process、segment、stitch、extract、frame classify/annotate、quality、annotate、verify 的既有组合约束不变。quality 的 `threshold` 与 `top_ratio` 互斥；self-consistency 与 judge 数保持奇数约束；用户、帧与按类 Schema 均通过 draft 2020-12 元校验及对应 few-shot 干跑。 |
| process 时序输入 | `[stream]` 只描述 process 摄取和 sessionization，不承担 sequence 生成时间线。segment 要求 process、annotate 开启且 generate 关闭；extract 要求 segment 与 UI；stitch 要求 segment。既有 no-op warning、视觉推导和上下文预算检查不变。 |
| flat 生成 | `generate.form` 缺省 `flat`。flat 继续使用 `llms`、`styles`、`seed_examples`、`standalone_count`、`num_per_record`、`seeds_per_call` 与 `num_per_call`；种子池与无种子计数恰选一种。flat 显式书写 sequence 专用字段是 CONFIG_ERROR。 |
| sequence 入口 | `generate.form = "sequence"` 要求 generate_only、text、`generate.enabled = true`、`run.partial_delivery = false`、`dedup.enabled = true`、`dedup.scope = "global"`、`output.meta_mode = "inline"`、`output.rejects = "none"`，并禁止 `--limit`。classify 与 frame.classify 必须关闭；frame.annotate 可作为 attempt-local 下游。M6 projector 为 sequence 与 frame 写 inherited Classification，`[frame.class.*]` 不要求 frame.classify 开启。sequence 显式书写任何 flat 字段是 CONFIG_ERROR。 |
| sequence 模式与 profile | `generate.mode` 必须为 `declared` 或 `instruction_only`；`semantic_llm` 与 `evaluation_llm` 必填、名称不同、支持文本且均显式声明正 `context_window`；`max_slot_attempts` 在 1..20，缺省 3。 |
| declared class 与 seed | 每个有交付 slot 的 sequence class 都声明非空 instruction、object state Schema；`initial_state_source` 只能是 `llm` 或 `catalog`。LLM source 的 state Schema 根级 `examples` 至少有一个通过完整 Schema 的 object；catalog 额外要求 JSONL 路径，按应用完整配置后的 slot 数无放回分配；行数不足或任一完整 `ScenarioSeed` 无效均在启动期失败。同一 class 的所有 pattern 必须声明同一 actor 集。 |
| frame class | 每个 role、instruction-only 闭集或 noise 引用的 frame class 都必须有非空生成 instruction 和 object JSON Schema；完整 Schema 中 `x-labelkit-business-time = true` 的 path 集与 `time_bindings` path 集完全相等，M1 冻结剥离时间叶子的 model Schema；`duration_s` 精确量化为正整数毫秒，resource 名唯一且符合 `[a-z0-9_]+`，非空 resource 要求正 duration。sequence 不接受 string payload。 |
| pattern | description 非空；role 名唯一且每个恰出现一次；`order` 恰好排列全部 role；`max_span_s > 0` 必填。每个相邻 role pair 恰有一条具名 gap，`max_gap_s` 必填；可加唯一、正向的非相邻 gap。秒值最多六位小数且必须毫秒对齐。containment 两端各出现一次、正 duration、不同且不共享 resource；missing-container 但保留 contained 失败。missing 目标 frame class 在 pattern 内唯一；reordered 目标相邻且 frame class 不同。 |
| role 权限与 binding | roots 按 RFC 6901 token 前缀校验，同一列表内禁止祖先/后代冗余，三个列表之间可相交。patch 只允许 `test/add/remove/replace` 且至少以一个 `test` 开头；test 落 read roots，写操作落 write roots。publish roots 在事件后必须存在。state payload binding 的 path 与 frame time binding path 不得相等或互为前缀。模型只面向 model Schema；generic finalizer 注入时间后再执行完整 Schema。 |
| counterfactual set | 每组至少一个 variant；name 与预期违规签名唯一；每个 variant 都有 outcome Schema，且根级 `examples` 至少有一个通过完整 Schema 的 object。positive 可不声明但 baseline 永远存在。missing、reordered、interval_exceeded 只能产生对应唯一结构违规；编译器必须证明目标变换仍满足所有非目标 gap、`max_span_s` 与日历约束。 |
| instruction-only | 每行 name 有效，count 为精确序列数，`len_range` 在 1..8。禁止 pattern、counterfactual set、role permission、outcome Schema 与 expected violation；只允许 LLM 从已声明 object frame class 闭集选类和 seed actor。显式 state Schema 根级 `examples` 至少有一个通过完整 Schema 的 object；缺省 state Schema 只要求 object 并以 `{}` 作固定 witness；不支持 catalog。 |
| timeline 与 calendar | `timestamp_start` 含显式 offset；Planner quantum 固定 1000 微秒，全部 start/duration/gap/boundary/shift 毫秒对齐。gap 是 start-to-start；max span、session span 与 calendar 使用完整 interval envelope。primary 总数为 N、crossed 数为 D 时必须 `primary_sessions = N - D`；instruction-only 还要求 D = 0、`primary_sessions = N`、`duplicate_sequences = 0`。 |
| noise 与 replay | `noise_events > 0` 时 `generate.noise` 必填；noise frame class 必须是 point class，noise 无 owner、resource、patch。replay 从 positive primary 按声明序无放回选源；每个 replay 独占尾部 session并冻结一个正 constant `shift_us`，所有 source delta/duration/resources 保持。 |
| 静态上限 | roles ≤ 32，variants/set ≤ 8，instruction-only events ≤ 8；seed、Schema、patch、payload、单个完整运行期 prompt value、单轮新增 L3 正文集合、单项 generation prompt text 分别执行第 6 章固定字节上限。M1 以根 examples 选择最小有效 witness，并用共享 prompt 构造器加动态 byte 包络证明六个 family 的首轮与 repair profile；运行期同界零派发拒绝。派生 `record_units` 与 `stream_rows` 均不超过 500000；`retained_content_bytes` 上限 536870912 在运行期 whole-attempt 预收费。 |
| quality | sequence 若启用 quality，只允许 pointwise、固定 threshold；pairwise、`top_ratio`、任何会让同一组成员互相影响的 selection 及任何生效的按类 quality override 均为 CONFIG_ERROR。 |
| 路径 | project TOML 的相对路径统一相对 `project_root`；CLI input/output 相对调用 cwd 后再覆盖。sequence 一次冻结 main、stream、report、manifest 与 failed_report，rejects 与 sidecar 为 null；全部路径冲突、非同目录 `.part` 或不可写均在启动期失败；任一既存 fixed/part 目标必须是非符号链接、可写普通文件。 |
| hook | 所有 hook 使用 `<python-file>:<attribute-path>`，相对文件按 project root 解析；M1 用文件路径加载并冻结 callable，不改 `sys.path`。sequence 的 `state_validator` 签名是 `validate_state(StateTransitionInput) -> list[str]`，只接收不可变副本。 |
| program 编译接缝 | M1 返回后，operators 层 `GenerationProgramCompiler` 在凭据物化前只读 `ResolvedConfig.sequence_generation` 与 M1 冻结的 ClassView，解析引用、Schema、Pointer、hook 和 catalog，冻结 expected violation、校验 delivery slot 精确数量与声明序、计算调用上界与 canonical digest；不构造 `DeliverySlot`、不抽样、不调用 LLM、不执行下游 stage。实际 slot 及 `catalog_row_index` 只由 `ScenarioPlan` 按 class、声明序与 scenario index 确定性冻结；LLM source 的索引为 null，slot 重试不换行。 |
| planner 接缝 | 编排层让 validate、dry-run、run 共用 operators 层 `compile_scenario_plan(program)`、block allocator 与 CP-SAT model builder；`run.seed` 已冻结进 `GenerationProgram.planner_seed` 和 digest。单 block 最多 4096 primary events；OR-Tools 单 worker，确定性 seed，每个优化层 `max_deterministic_time = 10.0`。只有 OPTIMAL 可解码；INFEASIBLE 为 exit 2，FEASIBLE/UNKNOWN 与 MODEL_INVALID 为 exit 4；不使用 incumbent 或替代 planner。 |
| console 与上下文预算 | 既有 console 推导不变。sequence 的六个 family 由 `generation_prompts.py` 同时供 M1 与运行期构造；`_generation_budget.py` 对配置态完整 PromptBundle、Schema、动态值 byte 包络与 repair 新增正文包络取 profile 最大值。seed、ActorView、patch、payload、完整 EventDraft history 与 blind semantic review 输入禁止裁剪或摘要；不适配完整上下文时配置失败或消耗当前 slot attempt。semantic request 本身不得携带 `EventTrace`、variant/target/expected/actual violation 或其他 evaluator truth。 |

**v1.20 Schema 投影。**业务时间标记只能位于显式 `properties` 下的 integer/string 标量叶子；标记在场时必须为
literal true。root、array/dynamic parent、非 required parent、未显式 `additionalProperties = false` 的 parent 全部失败。
业务时间叶子及 ancestors 出现 `$ref`/`$dynamicRef`、composition、conditional、dependency、dynamic-property、
cardinality、Schema 形态 additionalProperties 或 ancestor const/enum 时聚合失败。model Schema 删除时间叶、对应 required
成员和 annotation；mapping default/examples 与 class/frame few-shot output 同步投影，不创建 parent。非时间 constraint
逐字保持，完整 Schema 不改写。

sequence annotation `time_bindings` 仅允许 declared generate-only sequence。source 固定为
`first_resource_start_milliseconds` 且必须声明 resource；每个可交付 positive/missing/reordered/interval-exceeded role word
都至少保留一个该 resource event。annotation Schema 标记 path 集与 binding path 集完全相等；annotate disabled、
instruction-only、ordinary process 或 ordinary annotation 声明该配置均失败。M1 后由 M5 构造冻结
`SequenceTemporalContext`，没有 context 的调用不能猜值。

sequence 上下文证明的固定符号为 D=32768 runtime prompt value bytes、P=16384 patch bytes、
S=65536 ScenarioSeed bytes、Y=65536 payload bytes、R=32768 单轮新增 L3 正文集合 bytes。动态费用按
`ceil(bytes/3)` 折为 `est_text` 的保守 token 上界；declared EventPlan 加 2D，instruction-only EventPlan
加 5D，declared FrameRenderer 加 5D+P，instruction-only FrameRenderer 加 6D+P，SemanticEvaluator
加 S+2D，NoiseEvaluator 加 Y。ScenarioSeed 与 NoiseRenderer 的运行内容已由配置态完整最小 PromptBundle
覆盖，不再加动态项。

每个 case 先用与运行期相同的 builder 和 `est_prompt` 计完整消息脚手架；仅当该 profile
`supports_structured_output = true` 时计完整 active Schema。同一 profile 的互斥 case 只取最大值，不求和。
启用 repair 时，generic family 在 repair profile 上计空 user message 脚手架、按 capability 计 active Schema，
再加 R；EventPlan 计完整首轮 prompt 与动态包络、按 repair profile capability 计 Schema，再加 R 与两个新增
message overhead。首轮与 repair 包络同样按 profile 取最大值。运行期完整值/正文恰好固定上限可派发，多一
UTF-8 byte 在 provider 前拒绝；禁止裁剪、摘要、拆值或替换 Schema。

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

## 3.6 M6 生成 generate

### 3.6.1 职责与边界

**做：**flat 形态组装既有生成提示词并产出独立文本 Record；sequence 形态由
`labelkit/operators/generation/` 实现 ScenarioSeed、逐事件状态执行、反事实分支、独立判定与双视图投影。
`GenerateStage` 只保留 flat 薄入口；M10 的 generate_only sequence 分支直接调用精确交付控制器。

**不做：**仅文本模态；flat 单轮回流、不递归，仍由 M3 / M4 / M5 / M7 处理下游；sequence 不在 M6
内提交 dedup、quality、annotate、verify 或文件，也不允许 LLM 决定结构约束、权限、预期违规、ID 或投影视图。

### 3.6.2 生成模式

| 步骤 | 定义 |
|---|---|
| 种子选取 | 当前批内 `status="active"` 且聚合分 ≥ `generate.seed_min_score`（默认取 quality.threshold，未设阈值时取批内中位数）的记录（process 模式）。generate_only 模式（v1.4）：种子 = `generate.seed_examples` 字符串数组——Self-Instruct 的人工种子池形态（原文以 175 条人工种子自举 [18]）；数组缺省则为无种子条件化，提示词不含示例段、仅由 `generate.instruction` ×（可选）styles 驱动（Persona Hub / Cosmopedia 形态 [34][35]），须显式给出量目标 `generate.standalone_count`。 |
| 生成调用 | 每次调用随机不放回抽 min(`generate.seeds_per_call`（默认 3）, 可用种子数) 条种子作为示例（种子池小于抽样数时取全池）（Self-Instruct 的 in-context 自举结构 [18]），system = `generate.instruction`，要求输出 `{"samples": [str, ...]}`（恰 `generate.num_per_call` 条，默认 4），经 M8 校验。调用次数 = ⌈种子数 × generate.num_per_record / num_per_call⌉（generate_only 种子池形态同式，种子数 = len(seed_examples)，seeds_per_call 从种子池抽；无种子形态 = ⌈standalone_count / num_per_call⌉，提示词省去示例段）。 |
| 多模型混合（v1.2） | `generate.llms` 为 profile 引用数组（默认 `["default"]`，取代 v1.1 的单值键 `generate.llm`，5.2 配置表已同步修订）。每次生成调用按 `generate.mixture` 选定 1 个 profile：`"round_robin"`（默认）按调用序轮转；`"weighted"` 按 `generate.weights` 加权随机抽样。抽样 PRNG 用 `ctx.rng`（3.10.3，随 run.seed 派生）；实现须在并发派发前按调用序号 0..C−1 一次性预抽全部 (llm, style) 对（round_robin 的轮转序同样以调用序号为准），使结果与并发调度顺序无关，逐调用可复现。动机：单一模型自生成会使产出分布收窄、尾部逐渐消失（model collapse，Shumailov et al., Nature 2024 [36]），异构生成器混合是公认缓解；distilabel 的「任务级绑定任意 LLM」为同构工业设计 [5]。 |
| 风格条件化（可选） | `[[generate.styles]]` 子表（每项 `name` + `prompt`）非空时，每次生成调用经 `ctx.rng` 均匀抽取 1 个 style，其 prompt 以「`[风格要求] …`」格式追加在 `generate.instruction` 之后（仍为确定性模板拼接，3.6.1 边界不变）。persona / 受众条件化提升合成多样性的背书：Persona Hub 以 10 亿 persona 条件化提示 [34]；Cosmopedia 按受众 × 风格分桶派生提示 [35]。溯源与可观测（v1.2 只增）：新记录 `_meta.source` 增 `generator = {"llm": <profile>, "style": <name>\|null}`（未配置 styles 时 style 为 null；6.3 信封只增字段）；`report.generate` 增 `buckets` 统计——每 llm×style 桶的调用数 / 产出条数 / 去重存活数，令多样性可观测：某桶去重命中率显著偏高 ⇒ 该桶贡献的多样性低，应调整其权重或 style prompt。 |
| 样本回调过滤（v1.5，可选） | `generate.sample_validator` 配置时，每条样本文本在相似度过滤之前先过用户回调 `fn(text) -> list[str]`：非空 ⇒ 剔除该样本（过滤语义，与相似度过滤同性质：不重试、不产生 failed 记录），桶统计增 `rejected_by_validator`（6.4 只增字段）。回调抛异常 ⇒ 该样本按违规剔除并 stderr warn 一次性提示。 |
| 新记录构造 | 每条样本文本构造 Record：`raw = {input.text_field: sample}`，id 规则同 M2，`ref.generated_from = [种子id列表]`；generate_only 模式下 `generated_from = []`（种子非记录、无记录 id，种子本身在 project.toml 中可审计），`generator` 照常携带。 |
| 回流 | 新记录组成「生成子批」交回 M10，从 M3 起走 去重 →打分 → 标注 →校验；去重索引含全部原始记录与先前生成样本，即 Self-Instruct 的相似度过滤 [18]（3.3.3 节）。子批不再触发生成（单轮回流，不递归）。generate_only 模式下生成子批即唯一数据来源：按 `run.batch_size` 切批走同一链路 M3→M4→M5→M7→M11（3.10.3），单遍不递归同样适用。 |
| 按类种子池（v1.7） | classify 启用时（process 模式）：种子按 `classification.label` 分组为按类种子池，每类用该类有效 instruction / styles / num_per_record / temperature（`class_views[label].generate`）独立走「生成调用」行的量公式；llms / mixture / weights / seeds_per_call / num_per_call 恒为全局（5.2 按类覆盖白名单表）。**类段字典序拼接调用序**：参与类（有种子的类）按类名字典序占据连续的全局调用序号区间，每类预算 C_c = ⌈len(seeds_c) × num_per_record_c / num_per_call⌉；单遍 i = 0..C−1 预抽——llm 照旧按全局序号选定（round_robin 零 rng 消耗 / weighted 逐 i 一次 choices，「多模型混合」行机制不变），style 从该 i **所属类**的有效 styles 中均匀抽，种子抽样按全局序号升序逐调用执行；classify 关闭 ⇒ 单一匿名段 = 现行为。**种子门槛按类默认链**：每类取全局 `generate.seed_min_score` → 缺省取**该类有效** `quality.threshold` → 再缺省取**该类种子池**聚合分中位数；`select_seeds` 按 label 分组返回。规划产物 `CallPlan` 增 `class_name` 字段，`one_call` 按 plan.class_name 取类有效 instruction / temperature；`postprocess_samples` 返回 `list[tuple[Record, str \| None]]`，`run()` 构造 PipelineItem 时新样本**继承种子类**——带 `Classification(label, (label,), "inherited", {})`（零额外分类调用；回流子批经 M13 幂等跳过，3.13.4）。桶统计 key 在 classify 启用时扩展为 `<class>×<llm>×<style>`（6.4；关闭时格式不变）。generate_only 模式：`generate_all` 扁平路径不变——生成用**全局**指令（无输入无从按类），产物由链上 classify 正常分类后按类打分/标注；不支持 generate_only 按类生成配比（1.6 v1.7 对齐决策 ③，与 8.3 O6 一并立项）。 |
| sequence 形态 | `generate.form = "sequence"` 时，GenerationProgram 与 ScenarioPlan 在任何内容调用前冻结。declared 先生成一个完整 baseline 世界，再从目标 divergence 开始生成机械变换后的反事实分支；instruction-only 的位置、长度、逻辑时间、工件时间与 session 也先冻结，LLM 只在闭集内选择逐位置语义。两种模式都经过状态执行、独立 evaluator、投影与 M10 attempt transaction。详见 3.6.5 起。 |
| 上下文预算装填（v1.11） | 本次调用的目标 profile 声明 `context_window` 时按上下文预算装填生成调用（未声明 = 预算关闭，行为与 v1.10 一致；预算/估算/校准机制见 3.9）：`seeds_per_call` 降级为**上限值**——按 rng 采样序**从尾部丢弃**种子直到装下（确定性；被丢种子不递补），min 1；连 1 条种子都装不下 → 该调用按 `context_overflow` 处置（V10，7.6）。`generate.llms` 多 profile mixture（round_robin / weighted）下按**本次调用的目标 profile** 的预算装填——(llm, style) 预抽序与轮转序不变（确定性），装填结果仍逐调用可复现。系统侧静态部件（instruction / styles / 生成输出 Schema）不动态裁剪，由 M1 静态预检把关（V13③，3.1.4）。 |

### 3.6.3 API

```
class GenerateStage(Stage):
    name = "generate"
    async def run(self, batch, ctx) -> list[PipelineItem]:
        """返回值为新生成记录的子批（原批不修改）；M10 负责回流调度。
           单次生成调用经 M8 修复仍非法或重试耗尽 ⇒ 该调用作废并计入
           report.generate.buckets（calls 计入、produced 为 0），不影响其他调用与原批。"""
```

**背书：**生成模式为 Self-Instruct（ACL 2023）的种子自举 + 相似度过滤流程 [18]，指令可按 Evol-Instruct 风格写深化/扩展变体 [19]；多模型混合与风格条件化的多样性背书：Persona Hub [34]、Cosmopedia [35]、model collapse 的多生成器缓解 [36]；「任务级绑定任意 LLM」为 distilabel 的同构工业设计 [5]。

### 3.6.4 输入 / 输出示例

沿用统一文本示例（意图标注工程，仅文本模态），project.toml 追加：

```
[generate]                                  # project.toml 追加片段
enabled = true
llms = ["default", "judge"]
instruction = """你是中文输入法的真实用户。模仿示例指令的口吻与场景，生成全新的一句话中文指令：
日常场景、口语化、诉求明确；只借鉴风格与题材范围，不得复述示例内容。"""
num_per_record = 2
seeds_per_call = 3
num_per_call = 4                            # temperature 取默认 0.9
```

本示例采用轮转混合与两个风格模板：

```
mixture = "round_robin"                     # 第 1 次调用走 llms[0]="default"，第 2 次走 llms[1]="judge"

[[generate.styles]]                         # 可选；每次调用经 ctx.rng 均匀抽 1 个 style
name = "concise"
prompt = "指令务求简短口语化，一句话直接给出诉求，不加铺垫。"

[[generate.styles]]
name = "scenario"
prompt = "指令中须带出一个具体的生活或工作场景（对象、事由或时间）。"
```

抽中 style 的 prompt 以「[风格要求] …」追加在 `generate.instruction` 之后（3.6.2 风格条件化行）。本示例第 1 次调用轮转到 `"default"`、`ctx.rng` 抽中 `"concise"`，system 末尾追加「[风格要求] 指令务求简短口语化，一句话直接给出诉求，不加铺垫。」；第 2 次调用走 `"judge"`。

设 `quality.threshold = 0.5`（故 `generate.seed_min_score` 默认取 0.5），本批过门槛种子恰 3 条；调用次数 = ⌈3 × 2 / 4⌉ = 2。第 1 次调用抽全部 3 条种子入提示词（system = `generate.instruction` + M8 持有的内部生成输出 Schema，要求 `{"samples": [str, ...]}` 恰 4 条）：

```
种子: 1cda030abc565f17 "帮我写一条请假条，明天上午要去医院"
      d5ad41d6357f8a55 "写一份周报模板"
      7ed3a60f4714c33f "帮我把这段话翻译成英文……"

响应(经 M8 校验): {"samples": [
  "帮我写一段给客户的道歉话术，快递发错货了",
  "把'会议改到下周三下午三点'翻译成英文",
  "帮我编一条朋友圈文案，晒周末爬山的照片",
  "写一个辞职信的开头，语气委婉一点"]}
```

每条样本按 3.6.2 构造新 Record（`raw = {input.text_field: sample}`，id 规则同 M2）。第 1 条：

```
Record(
    id       = "31dae67e9b295e34",          # sha256(canonical_json(raw))[:16]
    modality = "text",
    text     = "帮我写一段给客户的道歉话术，快递发错货了",
    raw      = {"instruction": "帮我写一段给客户的道歉话术，快递发错货了"},
    ui_tree  = None, image = None,
    ref      = RecordRef(source_file="", line_no=None, pair_index=None,
                         generated_from=("1cda030abc565f17", "d5ad41d6357f8a55", "7ed3a60f4714c33f"),
                         generator={"llm": "default", "style": "concise"}))
```

v1.2 设定下，该 Record 出自第 1 次生成调用（`"default"` × style `"concise"`），主输出中其 `_meta.source` 片段（6.3；`generator` 为 v1.2 只增字段，计入 `report.generate.buckets` 的 `default×concise` 桶）：

```
"source": {"file": "", "pair_index": null,
           "generated_from": ["1cda030abc565f17", "d5ad41d6357f8a55", "7ed3a60f4714c33f"],
           "fields": {},
           "generator": {"llm": "default", "style": "concise"}}   // 未配置 styles 时 style 为 null
```

4 条新 Record 组成生成子批交回 M10，从 M3 起回流（单轮，不递归）：与全部原始记录及先前生成样本做去重，MinHash-Jaccard ≥ 0.85 者标记 `dropped_dup`（3.3.3 的 Self-Instruct 相似度过滤）。

#### 纯生成模式变体（v1.4，无输入数据）

```
# project.toml（纯生成工程；无 [run].input）
[run]
mode = "generate_only"
output = "./out/synth-ime-0702.jsonl"
modality = "text"

[generate]
enabled = true
llms = ["default", "judge"]
mixture = "round_robin"
instruction = """（同上例生成指令）"""
seed_examples = [                    # 种子池形态：调用次数 = ⌈3 × 2 / 4⌉ = 2，与上例同式
  "帮我写一条请假条，明天上午要去医院",
  "写一份周报模板",
  "帮我把这段话翻译成英文……"]
num_per_record = 2
# 无种子形态则改为：省去 seed_examples，设 standalone_count = 500（调用数 = ⌈500/4⌉ = 125）
```

与上例（process 模式）的差异仅在入口与种子来源：无 M2 接入（IngestReport 全零、`report.counts.scanned = 0`），生成样本按 `run.batch_size` 切批走 M3→M4→M5→M7→M11；新 Record 的 `generated_from = []`、`generator` 照常携带（如 `{"llm": "default", "style": null}`）。6.4 计数不变量退化为 emitted + dropped_* + failed = generated，仍成立。

#### sequence 形态教学配置（v1.18）

sequence 形态显式选择 `form` 和唯一运行模式；它不读取 `[stream]` 摄取配置，也不启用 classify 或
frame.classify 判定 stage；frame.annotate 可按配置作为 attempt-local 下游：

~~~toml
[run]
mode = "generate_only"
modality = "text"
output = "./out/sequence.jsonl"
partial_delivery = false

[generate]
enabled = true
form = "sequence"
mode = "declared"
semantic_llm = "default"
evaluation_llm = "judge"
max_slot_attempts = 4
state_validator = "hooks.py:validate_state"

[class.ticket_booking]
description = "一次订票请求与处理结果"

[class.ticket_booking.generate]
instruction = "保持路线、日期、乘客、请求和票号前后一致。"
state_schema_path = "schemas/state.json"
initial_state_source = "catalog"
initial_state_catalog_path = "catalogs/ticket-booking.jsonl"

[generate.timeline]
timestamp_start = "2026-01-05T09:00:00+08:00"
event_gap_s = [5, 60]
primary_sessions = 3
crossed_primary_sessions = 1
session_max_events = 16
session_max_span_s = 3600
session_gap_s = 3600
noise_events = 1
duplicate_sequences = 1
~~~

每个 `[frame.class.<name>.generate]` 都提供非空 instruction 与 object JSON Schema。declared 的 pattern
以具名 role、完整 order、必填 `max_span_s` 和具名 gap 声明唯一结构；交付数量只由
`[[generate.counterfactual_sets]]` 的 count 与 variants 决定。完整字段见第 5 章。

### 3.6.5 sequence 物理边界与入口

sequence 实现位于 `labelkit/operators/generation/`：

~~~text
generation/
├── program.py
├── planner.py
├── scenario.py
├── state.py
├── render.py
├── evaluate.py
└── project.py
~~~

`generate.py` 只保留 flat `GenerateStage`。sequence 由
`SequenceWorkflow` 调用 `sequence_workflow.deliver_generation`；后者把 M6 生成服务与
M3/M4/M5/M7/M11 协作者组装成有界候选准备和声明序短提交。M6 不 import orchestration，不在内部打开正式
输出文件，也不直接创建 `asyncio` task；全部模型叶调用通过 `GenerationServices.tasks` 提交。

配置装载后，所有入口共用同一 `compile_generation_program(config)` 和
`compile_scenario_plan(program)`；`run.seed` 已是 program-bound `planner_seed`。planner 冻结 delivery slot、variant、role 或位置、逻辑时间、
工件时间、session、crossing、noise 与 replay source。LLM 不能新增或删除事件、改变计划时间、改选 actor
权限、决定 expected violation，或在交付失败后触发重规划。
GenerationProgram 同时冻结生成上限、sequence ClassView、frame ClassView 与帧标注 Schema。每个 sequence
ClassView 的 Schema 在编译时物化为类覆盖 Schema，否则物化为全局用户 Schema；两种 Schema 都与源配置隔离并进入
program digest。编译后种子、run identity、提示预算、payload 上限、下游类路由和 M11 写前终检只读 program，
不能再读取 ResolvedConfig 的同名源字段。四类 render/evaluate request 显式携带 `program.limits`，不得从
`GenerationServices.config.sequence_generation.limits` 取值。

`validate_plan_identity` 依次校验 program 自摘要、传入 plan 对自身全部语义字段的摘要，再从 program 重建唯一
canonical plan 并要求完整 dataclass 相等；只协调重算 digest、改变 block 插入序或提供局部可行替代计划都失败。
六个 family 的提示词都由 common 共享构造器形成。M1 在配置态完整 PromptBundle/Schema 上叠加固定动态值与 L3
新增正文 byte 包络；运行期每个完整动态值和 repair 新增正文恰好 32768 UTF-8 bytes 可派发，多一 byte 零 provider
派发拒绝。patch 仍以 16384、ScenarioSeed/payload 仍以各自 65536 byte 上限单独计；任何值都不裁剪。

### 3.6.6 declared 世界执行

每个 slot attempt 先取得一个与任何 variant 无关的完整 `ScenarioSeed`。LLM source 调
`ScenarioSeedGenerator`；catalog source 按 M1 已验证的 catalog 与当前 `DeliverySlot.catalog_row_index`
机械取行，索引只由 `ScenarioPlan` 冻结，slot 重试不换行。两者使用同一固定外壳：
`initial_state`、`actors`、`shared_facts.public`、`shared_facts.hidden`、`style` 和
`time_context`。declared actor 闭集按 role 声明序取每个 `role.actor` 与全部 `role.observers` 的首次出现并集；
ScenarioSeed prompt、封闭 Schema、配置期预算镜像与 SemanticEvaluator 最小 seed 必须共用该唯一投影。
seed 不能包含 pattern、variant、role order、预期违规或最终 outcome。

事件循环如下：

图 3-5 sequence declared 世界执行与反事实分支

即使用户未声明 positive variant，baseline 也必须完整生成并通过全部生成侧判定；它只是不进入主输出与下游。
positive 直接复用 baseline branch。

每次成功执行并渲染一个事件后先构造 `EventDraft`。它包含后续 actor history、状态重放与 semantic review
所需的完整事件内容，但刻意不含 role，也不声明结构判定结果。declared branch 完成全部 draft 后，
`PatternEvaluator` 只从 `ObservedEvent` 投影重新绑定 actual role；只有完整 binding 通过后，才能为每个 draft
增加该唯一 role 并构造 `EventTruth`。instruction-only 不运行 PatternEvaluator，而是按冻结位置机械增加
`position_NNN` role。`EventTrace` 只接受 EventTruth，不接受 EventDraft。

declared 的 `ActorView` 只含当前 actor 的 goal、由 `read_roots` 投影的 state、此前
`publish_roots` 向该 actor 发布的 observations、logical time 与等待时间。EventPlanner 还只能看到
`shared_facts.public`、当前 role 与 frame instruction、pre-state Schema 摘要和允许的 patch Schema；
它看不到完整 state、其他 actor goal 或 `shared_facts.hidden`。不存在的 root 或非法 Pointer 使当前
attempt 失败，不静默忽略。

declared 的 prompt-safe `EventPlanRequest.actor_view` 必须非 null，`visible_state`、`history` 与
`actor_profiles` 固定为 null。instruction-only 在选出 actor 之前没有 ActorView，其 request 的
`actor_view` 固定为 null，但 `visible_state`、完整 `EventDraft` history 与按声明序排列的 actor identity/goal/style
profiles 必须非 null。两种形态都只能通过 `build_event_plan_request(context, ...)` 得到该 request。

EventPlanner 每次只输出 `frame_class`、`actor`、`intent` 与 RFC 6902 patch。declared 的 class 和
actor 必须恰等于当前 role；instruction-only 的 class 必须落在已声明 object frame Schema 闭集，
actor 必须引用 seed actor。patch 只允许 `test`、`add`、`remove`、`replace`，至少一个 test，
且所有 test 连续位于变更操作之前。

`EventExecutionContext(program, plan, slot, variant_name, event_index, scenario_seed, current_state, history)`
是计划与执行的唯一根。`build_event_plan_request` 先验证 slot 属于 plan、block key 存在且
event index 合法，再机械投影 prompt-safe `EventPlanRequest`。`plan_event` 只接收 context、
attempt index、variation nonce 与 `GenerationServices`，不接收独立 request。非法 plan/slot/block/index 在发送前
以 `generation_downstream_contract`、exit 4 终止，零 LLM call 且不消耗 slot attempt。

`StateExecutor` 在 context 的 current state 深拷贝上按顺序原子执行 patch，并依次验证
operation/roots、可选 pre-state Schema、基础 state Schema 与 `program.state_validator`。全部通过才产出
冻结 `EventExecution`，计算 before/after hash，并向 observers 追加 publish snapshot。M8 的后置验证
对每个结构合法 candidate 恰执行一次该流程；`post_validate_event_plan(candidate, context)` 是唯一
从 candidate 构造 `EventPlan` 的入口。`plan_event` 同时返回 EventPlan 与同一候选的
`EventExecution`，正式提交不得重放 patch 或 hook。

`FrameRenderer` 只接收 prompt-safe `RenderEventRequest`：ActorView、EventPlan、publish snapshot、
state before/after hash、机械 binding values、frame specification、role、public facts 与 attempt identity。
完整 state、`EventExecution`、state Schema/hook 不进入 renderer request。LLM 始终按完整 Draft 2020-12
frame Schema 返回完整 object；系统在 M8 验证后深拷贝 candidate，按 `RoleSpec.payload_bindings`
声明序以 RFC 6902 `add` 实例语义机械覆盖精确 path/value，再以同一完整 Schema 验证。
除根之外的父容器必须已存在；系统不改写任意 JSON Schema。payload 或完整上下文超限使
whole-slot attempt 失败，不能裁剪真值。

面向人的 payload 必须把内部状态翻译成自然业务语言；不得机械复述终态、在无重开事实时把终态写回处理中，
也不得颠倒 actor 的消息收发关系。时间叙述必须以真正经历等待的动作、阶段或参与方作主语；把请求、消息或
业务实体直接写成等待主体属于不自然表达，以等待过程本身作主语不属于该缺陷。

### 3.6.7 反事实因果耦合

四种 variant 的唯一结构语义如下：

| kind | 机械变换 | 期望违规 |
|---|---|---|
| `positive` | 完整 baseline | 空集 |
| `missing` | 删除目标 role | `missing_role(target_role)` |
| `reordered` | 交换两个相邻目标 role | `reordered(target_before,target_after)` |
| `interval_exceeded` | 固定目标 gap 的 before 及其前缀，整体后移 after 与后缀 | `gap_above_max(target_gap)` |

missing 删除点、reordered 较早目标点、interval_exceeded 的 after 是各自 causal divergence。此前的
protected prefix 复用 `EventDraft` 的语义字段：payload、ActorView、intent、actor、frame class、logical time、patch
和 state hash 的 canonical bytes 都与 baseline 相同，`event_key` 也相同；只重新派生 world branch ID、event ID
和工件时间。复用 patch 时仍在新分支 initial state 上重放并核对 before/after hash。PatternEvaluator 通过后，
派生出的 protected-prefix `EventTruth.role` 也必须相同。

baseline CP-SAT 同时加入每个 reordered 机械交换后的全部非目标 order 与 gap 约束；不可只产生目标 reordered 时，
计划在任何内容调用前失败。positive 缺省时，hidden baseline 从 `timestamp_start` 独立求解全部 role calendar window，
不能借用第一个可见反事实 branch 的起点。

divergence 起的 causal suffix 可以重新规划内容，但必须保留同一 ScenarioSeed、实体、目标、style 与时间上下文。
`CouplingEvaluator` 独立比较 protected prefix；任一受保护字段变化都产生
`coupling_violation` 并拒绝整个 attempt。不得分别生成“看起来相似”的正反例来代替机械前缀等同。

### 3.6.8 instruction-only

instruction-only 没有 pattern、role permission、outcome Schema 或 expected violation。planner 在任何 LLM 调用前
冻结每个 slot 的 length、`position_000` 起的全部位置、logical timestamp、artifact timestamp 与 session。
`event_gap_s` 只负责这些相邻位置的时间域。

ScenarioSeedGenerator 每个 attempt 生成一至八个 actor，不支持 catalog。EventPlanner 接收完整 current state 与
完整既有 `EventDraft` history，只能从已声明且具有 object Schema 的 frame class 闭集选类，actor 必须引用 seed actors；
它仍只输出固定四字段并经同一 StateExecutor。truth 必须显式记录 semantic actor-knowledge guarantee，
但不能伪造 declared 的 role permission。

instruction-only 的 `EventExecutionContext` 从 program/slot 解析出的 role 固定为 null。StateExecutor
跳过不存在的 root containment、pre-state Schema、publish roots 和 observers，但仍验证 patch operation
闭集、test 前缀、原子 JSON Patch、基础 state Schema 与可选 state validator。该模式没有 payload binding；
actor 选定后再从完整历史构造只供 FrameRenderer 使用的 ActorView。

instruction-only 的每个 slot 是独立单序列提交组，要求
`crossed_primary_sessions = 0`、`primary_sessions = N`、`duplicate_sequences = 0`。它不生成
counterfactual set 或 positive replay source。

### 3.6.9 独立判定与投影

生成调用不能自报通过。每个 declared branch 依次通过以下相互独立的判定：

- `PatternEvaluator` 只接收最终的 `ObservedEvent(event_id, frame_class, timestamp_us, duration_us)`，自行执行 role
  一对一绑定并输出 `actual_bindings` 与 `actual_violations`。它不接收 planner role witness、expected
  binding 或 variant 变换；实际违规必须与唯一 expected violation 恰等。
- `StateEvaluator` 只从 `StateEvaluationRequest.program` 取 Schema 与 state validator，从 seed initial state
  独立重放所有 patch，复核每步 Schema/hook/hash、payload binding、final state、outcome Schema 与
  protected prefix。非 baseline variant 必须同时携带独立 `baseline_events`；baseline 请求的该字段为空。
- `SemanticEvaluator` 使用与生成 profile 名不同的 evaluation profile，一次读取盲化的
  `SemanticReviewEvent` 序列、ScenarioSeed 与 final state，返回 causal consistency、actor knowledge、
  goal consistency、temporal plausibility、cross-frame consistency 与 realism 六个 boolean；全部为 true
  才通过。其 request 不得包含 EventTrace、variant/target/expected/actual violation、pattern/state evaluation
  或任何 evaluator truth。declared 的 `pattern_description` 恰为 `SequencePattern.description`；
  instruction-only 恰为 `InstructionOnlySpec.instruction`，不得摘要或拼接。SemanticEvaluation 通过后才与既有
  机械 verdict 组装最终 EventTrace。判定器对终态重开、终态近义复述、actor 收发关系倒置与错误等待主体执行
  反例优先审查；缺帧、错序或长等待本身不自动失败。
- `CouplingEvaluator` 机械比较反事实 protected prefix。
- primary candidate-local validator 只验证当前 DeliverySlot、严格 variant 顺序的 `SequenceRows`、全部已规划
  `ReplayLayout` 与对应 `ReplayRows`；它不读取已提交前缀。`CrossViewFrontier.check_primary` 与
  `check_noise` 在声明序短提交中增量验证当前 candidate 与已提交 event ID、timestamp、source、resource interval 和
  phase/ordinal，返回冻结 `CrossViewDelta`；`commit(delta)` 无普通失败分支。全部 primary、noise 与 replay
  内存提交后，`reconcile_views` 再从最终 rows 独立重建全部事实一次。三层均从实际 canonical rows 复算
  retained-content，不信任控制器或 row carrier 提供的计数。

declared 的 `EventTruth.role` 只在 `PatternEvaluator` 产出 `actual_bindings` 后机械写入，不使用 planner
witness；缺失、重复或额外 binding 都 fail closed，PatternEvaluator 通过前不得为 declared branch 构造
EventTruth。instruction-only 的 role 机械写为 `position_NNN`。`EventProjector` 只从当前 DeliverySlot 与
EventTrace 产生 pre-downstream `ProjectedSequence(main_record, primary_stream_rows)`；它不产生 noise、
replay 或最终 output bytes。NoiseProjector 单独从 `NoiseSlot` 产生 noise row；ReplayProjector 只在 M11
完成最终 sequence 装配后从 source `SequenceRows.primary_stream_rows` 产生 replay。世界 state 与 patch
默认不写训练输出，只在 `trace.content = "full"` 的独立 trace 通道出现。

`EventProjector` 在构造 Record 前不信任 EventTrace 的 gate carrier：它重验 StateEvaluation 的 hash/三项布尔、
SemanticEvaluation 的六项布尔与空 reason_codes。instruction-only 禁止携带 PatternEvaluation；declared 从
program/variant 重建精确 role word，逐事件核对 RoleSpec 的 frame class/actor、event_id 唯一、
`actual_bindings == {event_id: role}`，并要求 actual violation 与唯一期望违规恰等。任一伪造都是
`generation_downstream_contract`，不能靠投影视图自洽通过。
公开 `project_trace` 与 `project_replay` 同时携带 program 和完整 plan，先验证 canonical plan，再要求 slot/layout
与该 plan 中唯一成员完整相等并复核事件、来源与身份。SequenceWorkflow 只在交付边界验证一次，后续调用包内
validated helper；内容重试不得重新运行 CP-SAT。

所有 v1.20 generation ID 均对 canonical JSON array
`["labelkit:v1.20", domain, components]` 做 UTF-8 SHA-256，取小写前 32 hex；`components` 本身必须是
JSON array，timestamp 是 integer 微秒，payload component 是已验证 JSON object，不得字符串拼接。
declared 与 instruction-only 分别使用冻结 domain 与 component 序列；`Record.id = sequence_id`，
member `Record.id = event_id`。replay 与 noise 使用各自 domain。缺失分支没有目标 role 的 `event_key`。
完整 domain/component 表见第 6 章；generation ID 只使用该冻结公式。

### 3.6.10 noise、replay 与内存边界

全部 primary slot 内存提交后进入 noise phase。noise slot 使用连续有界候选缓冲；render 与独立 semantic
evaluation 可跨 slot 并发，结果仍按 `NoiseSlot.ordinal` 提交。planner 把与 noise_events 等长的非空唯一
`generate.noise.topics` 按 ordinal 冻结到 `NoiseSlot.topic`。`NoiseRenderer` 只接收 noise instruction、
frame Schema、sequence/frame class 的 name/description 闭集、`NoiseSlot.topic`、冻结时间与 attempt identity；
不接收 seed、trace、primary payload 或既有 noise payload。renderer 必须把计划话题作为唯一话题，
不得改换、混合或泛化；它在内部构造 attempt index + 2 个符合该话题的自然表达角度，再选择 attempt index
对应的角度。不同 attempt 必须使用明显不同措辞，不得输出候选表或内部标识；Schema examples 只描述形状，
禁止复制或改写其内容。

`NoiseSemanticEvaluator` 独立要求 unrelated-to-declared-tasks、no-executable-task、realism 与
matches-planned-topic 全为 true；候选不忠实计划话题时使用 `planned_noise_topic_mismatch`。attempt-local
路径只冻结 similarity signature，不写 `SimilarityFilter`。`PreparedNoiseCandidate` 闭包 `NoiseSlot`、
post-gate payload digest、最终 row、signature、dataset counter delta、实际 retained bytes 与 frozen digest。
`NoiseCandidateReconcileRequest` 在进入缓冲前验证 payload、topic/ordinal、timestamp、ID 派生、字段闭包与
canonical bytes。成为 head 后，提交协调器先对最新全部 primary 与较低 ordinal noise 做 similarity probe，
再执行 frontier 与 retained-content 检查；signature commit 后不得再有普通可恢复失败。noise 不进 quality、
annotate、verify 或 main dedup group。

replay 是完整 positive sequence 的 constant-shift 重发，不是单帧重复。planner 按 declaration order 与 scenario
index 冻结 source；每个 replay 独占尾部 session并持有一个正、毫秒对齐的 `shift_us`。全部 member 的 start delta、
duration、resources、descriptor、frame class、actual role、顺序与非时间 payload 逐位同源；payload business time
按 replay start/end/duration 机械重绑，并以 rebound payload 派生 replay event ID。source 不足在启动期失败，source
delivery 失败时不能改选。Replay 不调用 LLM，也不进入独立 coordinator；它与 source primary candidate 一起校验和提交。

下游接受后，M11 通过
`SequenceAssemblyRequest(program, schema_engine, item, projection, batch_no)` 零 I/O 装配 `SequenceRows`，
并以 program-bound sequence/frame annotation Schema 终检实际待交付对象；不得回读 source ResolvedConfig。
普通非时间终检失败归 `sequence_projection_mismatch` 并重试整个 source slot，零 dedup、dataset、row 或 replay commit；
payload/annotation business time、duration/resources 或 containment 的固定计划不一致归 terminal
`generation_downstream_contract`，不消费 slot retry。
ReplayProjector 只从该最终 source 行派生全部已规划 `ReplayRows`。两者分别以同一
`canonical_delivery_row` 计算自身行的 UTF-8 byte 数加一个 JSONL 换行 byte。

`PrimaryCandidateReconcileRequest` 要求 `SequenceRows` 与 slot 的 variant 数量和顺序完全一致，并要求计划中
该 source 的全部 `ReplayLayout` 与 `ReplayRows` 一一对应。它从当前 candidate 的实际 main、primary 与 replay
rows 独立复算 bytes，不读取已提交前缀。通过后，`PreparedCandidate` 深度冻结 witnesses、`SequenceRows`、
全部 `ReplayRows`、`DedupReservation`、dataset counter delta、实际 retained bytes 与 candidate digest；
随后立即释放 `ProjectedSequence`、`PipelineItem`、`AttemptTransaction`、state 和 LLM 中间对象。

该 candidate 成为 head 时，retained-content 检查比较“已提交实际 bytes + 当前 candidate 实际 bytes”，上限为
536870912。恰好上限接受，多一 UTF-8 byte 归 `sequence_memory_budget` 并重试整个 source slot，零 dedup 或
dataset commit。成功后 replay 与 source 一起提交；primary 视图共享冻结 payload，replay 保存重绑后的独立 payload。
`CrossViewFrontier.check_primary/check_noise` 只增量检查当前 candidate 与已提交前缀，返回冻结
`CrossViewDelta`，`commit(delta)` 无普通失败分支；全部 rows 内存提交后，`reconcile_views` 从最终
sequence main/primary、noise 与 replay 实际 canonical rows 独立执行一次完整对账。

`record_units = primary_sequences + primary_events + noise_events + replay_events` 与
`stream_rows = primary_events + noise_events + replay_events` 各自不得超过 500000。单 block 最多 4096
primary events。候选缓冲限制 preparing、prepared 与 recoverable outcome 的槽位总数；记录
`candidate_bytes_high_water` 时计算全部已完成但尚未提交候选 canonical bytes 的同时驻留总和。它不包含在途
provider response、`AttemptTransaction`、Python 对象开销、dedup registry 或 HTTP buffer。
500000 最小载荷与接近 512 MiB 混合载荷继续验证最终 compact output 包络；六百候选另以固定结果形状记录
peak RSS 与候选字节高水位。由于用户 Schema 与 provider response 没有统一 byte 上限，该压力门只证明固定
工作负载，不声称任意合法工程在六百候选下都不会耗尽物理内存。

sequence 的 exact delivery、attempt 消耗、下游 transaction、正式文件提交与失败矩阵由 M10 和 M11 定义；
M6 只返回完整候选、独立判定和投影结果，任何失败都不得产生半个 counterfactual set。

### 3.6.11 v1.20 business time、区间与自描述工件

FrameRenderer 与 NoiseRenderer 只把 `FrameClassView.model_gen_schema` 发送给 provider；prompt 可读取 planned
start/end/duration，但 provider 输出不含 business time leaf。渲染调用通过 `complete_finalized`：model L2 后按
`TimeBindingSpec` 声明序机械注入，完整 frame Schema 通过后才构造 EventDraft/EventTruth。mechanical leaf 在 Planner
完成后、首个 LLM 前独立按 leaf Schema preflight；不满足是 `generation_plan_infeasible`。finalizer/projector 异常、
非 object、非时间 mutation 或 full Schema 失败是 terminal `generation_downstream_contract`，不进入 L3。

protected prefix 重新使用 current branch 的 start/duration 注入时间；CouplingEvaluator 按 frame class 删除全部 time paths
后比较 payload canonical bytes，其他 protected 字段不变。每个 primary、noise 与 replay `_meta.event` 固定自带
`duration_us`、`resources` 和规范声明序 `time_bindings`。noise 固定 point class；instruction-only 也只允许 point frame
class。ID、retained bytes、delivery digest 和 CrossView 都消费注入或 rebound 后的最终 payload。

M2 从 descriptor 证明内容后写 `Record.exact_dedup_text`。generation single/sequence 的 exact key 固定为
`sha256(canonical_json(["labelkit:v1.20", "generation_stream_exact",
ordered_exact_dedup_texts]))`；有该 carrier 的记录只运行 exact 层，禁止构造 MinHash 或 embedding。合法 replay 因
非时间内容相同命中 exact duplicate；任何非时间 payload 差异不得命中。

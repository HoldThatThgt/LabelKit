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
llm = "default"
instruction = """你是中文输入法的真实用户。模仿示例指令的口吻与场景，生成全新的一句话中文指令：
日常场景、口语化、诉求明确；只借鉴风格与题材范围，不得复述示例内容。"""
num_per_record = 2
seeds_per_call = 3
num_per_call = 4                            # temperature 取默认 0.9
```

v1.2 键名迁移与多样性设定补充：上述片段中的单值键 `llm = "default"` 在 v1.2 写作数组键 `llms`（5.2 配置表），本示例取双模型轮转 + 两个风格模板（种子选取、调用次数与下方样本文本均不变）：

```
llms = ["default", "judge"]                 # v1.2：取代 llm = "default"；元素须为 [llm.*] profile
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
`Orchestrator._run_generate_only` 调用 `generation_delivery.deliver_generation`；后者把
M6 生成服务与 M3/M4/M5/M7/M11 协作者组装成串行 slot admission。M6 不 import orchestration，
也不在内部打开正式输出文件。

配置装载后，所有入口共用同一 `compile_generation_program(config)` 和
`compile_scenario_plan(program, seed)`。planner 冻结 delivery slot、variant、role 或位置、逻辑时间、
工件时间、session、crossing、noise 与 replay source。LLM 不能新增或删除事件、改变计划时间、改选 actor
权限、决定 expected violation，或在交付失败后触发重规划。

### 3.6.6 declared 世界执行

每个 slot attempt 先取得一个与任何 variant 无关的完整 `ScenarioSeed`。LLM source 调
`ScenarioSeedGenerator`；catalog source 按 M1 已验证的 catalog 与当前 `DeliverySlot.catalog_row_index`
机械取行，索引只由 `ScenarioPlan` 冻结，slot 重试不换行。两者使用同一固定外壳：
`initial_state`、`actors`、`shared_facts.public`、`shared_facts.hidden`、`style` 和
`time_context`。seed 不能包含 pattern、variant、role order、预期违规或最终 outcome。

事件循环如下：

~~~mermaid
flowchart TD
    Seed["ScenarioSeed"] --> Baseline["按 role 逐事件 plan / execute / render"]
    Baseline --> BaselineEval["baseline 机械与语义判定"]
    BaselineEval --> Branch["按 variant 声明序建立分支"]
    Branch --> Prefix{"事件位于 protected prefix?"}
    Prefix -- "是" --> Reuse["复用 EventDraft 语义字段并重派生分支 ID / 工件时间"]
    Prefix -- "否" --> Replan["从 divergence 起逐事件重新 plan / execute / render"]
    Reuse --> Eval["分支独立判定"]
    Replan --> Eval
~~~

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

- `PatternEvaluator` 只接收最终的 `ObservedEvent(event_id, frame_class, timestamp_us)`，自行执行 role
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
  机械 verdict 组装最终 EventTrace。
- `CouplingEvaluator` 机械比较反事实 protected prefix。
- `CrossViewReconciler` 在提交前验证 main sequence 与 primary stream owner 双向一一对应，以及 replay/noise
  边界与全局时间顺序。

declared 的 `EventTruth.role` 只在 `PatternEvaluator` 产出 `actual_bindings` 后机械写入，不使用 planner
witness；缺失、重复或额外 binding 都 fail closed，PatternEvaluator 通过前不得为 declared branch 构造
EventTruth。instruction-only 的 role 机械写为 `position_NNN`。`EventProjector` 只从当前 DeliverySlot 与
EventTrace 产生 pre-downstream `ProjectedSequence(main_record, primary_stream_rows)`；它不产生 noise、
replay 或最终 output bytes。NoiseProjector 单独从 `NoiseSlot` 产生 noise row；ReplayProjector 只在 M11
完成最终 sequence 装配后从 source `SequenceRows.primary_stream_rows` 产生 replay。世界 state 与 patch
默认不写训练输出，只在 `trace.content = "full"` 的独立 trace 通道出现。

所有 v1.18 generation ID 均对 canonical JSON array
`["labelkit:v1.18", domain, components]` 做 UTF-8 SHA-256，取小写前 32 hex；`components` 本身必须是
JSON array，timestamp 是 integer 微秒，payload component 是已验证 JSON object，不得字符串拼接。
declared 与 instruction-only 分别使用冻结 domain 与 component 序列；`Record.id = sequence_id`，
member `Record.id = event_id`。replay 与 noise 使用各自 domain。缺失分支没有目标 role 的 `event_key`。
完整 domain/component 表见第 6 章，不得回退到输入摄取 ID 公式。

### 3.6.10 noise、replay 与内存边界

全部 primary slot 接受后，noise slot 按 ordinal 串行执行。`NoiseRenderer` 只接收 noise instruction、frame
Schema、sequence/frame class 的 name/description 闭集、冻结时间与 attempt identity；不接收 seed、trace 或
primary payload。`NoiseSemanticEvaluator` 独立要求 unrelated-to-declared-tasks、no-executable-task 与 realism
全为 true。noise 再经过 attempt-local SimilarityFilter；通过后才 commit 签名。noise 不进 quality、annotate、
verify 或 main dedup group。

replay 是完整 positive sequence 的原样重发，不是单帧重复。planner 按 declaration order 与 scenario index
冻结 source；每个 replay 独占尾部 session，使用新 replay sequence/event ID 与新 artifact timestamp，但 payload、
frame class、actual role 和顺序逐位同源。source 不足在启动期失败，source delivery 失败时不能改选。

下游接受后，M11 先以最终 `PipelineItem` 与 `ProjectedSequence` 零 I/O 装配 `SequenceRows`，
然后 ReplayProjector 只从该最终 source 行派生全部已规划 `ReplayRows`。两者分别以同一
`canonical_delivery_row` 计算自身行的 UTF-8 byte 数加一个 JSONL 换行 byte。dedup `group_commit`
前的 prospective `retained_content_bytes` 等于既有已接受累计、当前 set 所有 `SequenceRows`
与本次 `ReplayRows` 之和，上限为 536870912。超限归 `sequence_memory_budget` 并重试整个
source slot，零 dedup 或 dataset commit。接受后 payload 在 main/stream/replay 间共享冻结引用，不深拷贝；
立即释放 `ProjectedSequence`、`PipelineItem`、`AttemptTransaction`、state 和 LLM 中间对象，只保留最终
main/stream rows、dedup features 与计数。

`record_units = primary_sequences + primary_events + noise_events + replay_events` 与
`stream_rows = primary_events + noise_events + replay_events` 各自不得超过 500000。单 block 最多 4096
primary events；delivery 完成一个 slot 后释放 state 快照和 LLM 中间对象。500000 最小载荷与接近 512 MiB
混合载荷的 peak RSS 都必须不超过 4 GiB。

sequence 的 exact delivery、attempt 消耗、下游 transaction、正式文件提交与失败矩阵由 M10 和 M11 定义；
M6 只返回完整候选、独立判定和投影结果，任何失败都不得产生半个 counterfactual set。

# LabelKit v1.18 序列生成内核实现规格

> 状态：实现规格，字段、术语、边界与完成门已冻结  
> 日期：2026-08-21  
> 实施基线：main 已合入 feat/v1.17-scenario-planning，基线提交 2138de2  
> 替换范围：一次性删除 v1.13–v1.17 的全部序列生成专用面  
> 兼容策略：不兼容旧配置、内部接口、随机数消费、提示词、报表、真值和时间流工件  
> 设计来源：PROPOSAL-sequence-generation-redesign.md，经语义、代码亲和性、示例与 E2E 三路审查修订  

## 1. 交付结论

v1.18 把序列生成从 brief、realize、tier 和 weave 链路替换为一条以命名序列模式、持续世界状态、
事件真值、独立判定和反事实集合精确交付为中心的路径。

~~~mermaid
flowchart LR
    C["配置编译"] --> P["CP-SAT 冻结结构与时间"]
    P --> S["场景种子"]
    S --> B["正例基准事件循环"]
    B --> V["反例 causal closure 重规划"]
    V --> E["结构、状态、语义独立判定"]
    E --> D["全下游集合事务"]
    D --> X["主序列、时间流、manifest"]
~~~

系统必须同时满足：

- declared 模式显式声明完整帧组、完整顺序、每个相邻角色的最大间隔和序列总跨度。
- positive、missing、reordered、interval_exceeded 都由同一个模式派生。
- 同一反事实集合共享完整 ScenarioSeed；目标点前的 EventDraft 语义字段全量复用，只重派生分支 ID 与工件时间，
  判定后派生相同角色绑定的 EventTruth。
- declared 模式中，LLM 每次只看到当前 actor 被授权读取的状态与已经发布给它的历史。
- JSON Patch 在副本上原子执行；每一步经过基础状态 Schema、可选前置 Schema 和可选 state validator。
- PatternEvaluator 从最终事件重新绑定实际角色，不读取 planner 的角色 witness。
- 任一变体在生成、判定、dedup、quality、annotate 或 verify 失败时，整个集合从场景种子开始重试。
- 尝试耗尽时不替换主输出、时间流、成功 report 或成功 manifest。
- instruction-only 是独立模式，不声明结构证明，不是 declared 失败后的降级路径。

## 2. 审查裁决

### 2.1 tier、subsequence 与旧时间流

正式规格不包含 tier、tier_rank、subsequence 或 filler。一个命名 pattern 表示一种精确帧组；长度或构成不同的
旧 tier 必须由用户重写为不同的命名 pattern 和不同的精确 counterfactual set count。不存在 tier 到 pattern
的运行期映射、迁移器或兼容解析器。

旧 generate.stream、quota、frame rule、sequence rule、frame window、time_fields、brief_schema、
realize_schema、旧 ScenarioPlan 字段与构造契约、SequencePlan、survivor projection 和旧 artifact truth 均删除。
v1.18 重新定义同名 ScenarioPlan；它只实现本规格第 8 节与第 16 节冻结的新契约，不保留旧字段或构造入口。

### 2.2 instruction-only

instruction-only 的 planner 在 LLM 前冻结 slot、事件数量、每个位置的逻辑时间、投影时间和会话。
LLM 只能为固定位置选择已声明的 frame class、actor、意图和状态 patch，不能改变数量与时间。

该模式没有用户声明的 actor 权限策略，因此不声称机械证明 actor knowledge。事件规划器读取完整世界，
SemanticEvaluator 负责语义级 actor_knowledge 判定；truth 必须写 actor_knowledge_validation =
semantic。declared 模式则写 actor_knowledge_validation = mechanical_and_semantic。

instruction-only 使用数组表，可声明多个 sequence class 和独立精确 count。

### 2.3 逐事件世界执行

整轨迹一次调用被删除。declared 模式按事件循环：

~~~mermaid
flowchart LR
    A["从 read_roots 派生 ActorView"] --> B["LLM 规划一个事件"]
    B --> C["检查 test/read 与 mutation/write 权限"]
    C --> D["JSON Patch 原子执行"]
    D --> E["Schema + hook"]
    E --> F["publish_roots 发给 observers"]
    F --> G["下一个事件"]
~~~

每次调用携带该 actor 的完整允许历史，因此不会逐帧失忆，也不会向模型暴露其他 actor 的隐藏状态。

### 2.4 反事实耦合

每个 slot attempt 都先完整生成并判定一条 positive baseline，即使配置没有交付 positive 变体。
每个反例的最早目标点之前复用 EventDraft 的语义字段，结构判定后再派生 EventTruth：

- actor、intent、logical_time_us、ActorView、JSON Patch、状态哈希和 rendered payload 完全相同。
- event_key 相同；world_branch_id、event_id 和投影 timestamp 按分支重新派生。
- 目标点及其 causal suffix 可以重新规划意图、patch 和 payload，但必须保留 ScenarioSeed 的稳定实体、目标和风格。
- baseline 自身失败消耗当前 slot attempt；未交付 baseline 的调用、token 和失败仍进入 usage 与 rejected_attempts。

missing 的目标点是被删除角色；reordered 的目标点是交换对中较早角色；interval_exceeded 的目标点是目标 gap
的 after 角色。

### 2.5 世界时间与工件时间

ScenarioSeed 是一个逻辑世界快照，包含稳定实体、目标、领域日期和 time_context。它不随时间流工件中的
投影 timestamp 自动推进。事件语义只读取 logical_time_us、相对等待和 ScenarioSeed.time_context。

时间流 timestamp 是把多个互相独立的 world branch 放进可重放工件时的排序坐标。把同一 ScenarioSeed
投影到不同工件起点不会声称它们属于同一个现实世界；world_branch_id 明确表示平行分支。

### 2.6 duplicate 与双视图

duplicate 的单位冻结为完整序列 replay，不是单事件。每条 replay 有新的 replay_sequence_id，
并通过 duplicate_of_sequence_id 指向主输出中的 source sequence。逐位 payload、frame_class、actual role
和顺序与 source 相同；event_id 与 timestamp 新生。

主输出只包含 primary sequence。主输出与时间流的双射只覆盖 primary owner；replay owner 通过独立的同源关系验收。

### 2.7 提交保证

成功运行把主输出、时间流、report 分别原子改名，最后原子替换 manifest。manifest 包含 run_id、delivery_digest、
路径和 SHA-256；消费者只有在 manifest 摘要与三个文件一致时才把本次运行视为已提交。

在开始正式通道 rename 前发生的 DeliveryError、provider fatal、SIGINT 和配置/规划错误不会改名正式数据文件。
正式通道 rename 开始后的 I/O 失败可能留下固定路径混代；旧 manifest 保持不变，因此摘要必不匹配，消费者必须拒绝。
本版本不承诺在 commit-I/O、进程崩溃或掉电后保留上一版固定路径的数据，只承诺不会产生一个声称新数据已提交的 manifest。
这里不引入目录事务、数据库或版本化存储。

## 3. 唯一术语

| 术语 | 唯一含义 |
|---|---|
| sequence class | 训练任务的闭集类别 |
| sequence pattern | 一个 sequence class 的精确角色全集、完整顺序、间隔和跨度 |
| role | pattern 中恰好出现一次的业务职责 |
| counterfactual set | 共享 ScenarioSeed 的 baseline 与声明变体集合 |
| variant | positive、missing、reordered 或 interval_exceeded |
| ScenarioSeed | initial_state、actors、shared_facts、style 和 time_context 的冻结对象 |
| ActorView | 当前 actor 被允许读取的状态和此前发布给它的事件快照 |
| EventDraft | 逐事件生成期的完整事件草稿；没有 role，不声明结构判定结果 |
| EventTruth | 一个事件的角色、actor、时间、视图、意图、patch、状态哈希和载荷 |
| EventTrace | 一个 world branch 的初始状态、顺序 EventTruth、最终状态和判定 |
| delivery slot | 一个 counterfactual set 与 scenario_index 的精确提交单位 |
| primary sequence | 进入主输出、拥有独立 world_branch_id 的序列 |
| replay sequence | 只进入时间流、逐位复制 source primary sequence 的重发 |

不使用 tier、grade、blueprint、brief、realize、survivor、干扰序列或复杂事件处理等名称指代上述对象。

## 4. 运行形态

generate.form 只有 flat 与 sequence。flat 是 v1.12 独立样本能力，保持现有行为；sequence 使用本规格。
sequence 的 generate.mode 只有 declared 与 instruction_only，且形状完全互斥。

sequence 形态要求：

- run.mode = generate_only，run.modality = text。
- generate.enabled = true，generate.form = sequence。
- classify.enabled 与 frame.classify.enabled 必须为 false；两个 stage 不发判定调用。
- sequence class 从带 description 的 [class.<name>] 声明，frame class 从 [frame.class.<name>] 声明；
  sequence 形态不使用 [[classify.classes]] 或 frame classifier 作为注册表。
- 每条 sequence 与 frame 在进入下游前机械写入 inherited Classification。
- sequence class 仍建立 ClassView，使 per-class quality、annotate 与 verify 按既有路由生效。
- run.partial_delivery 必须为 false。
- dedup.enabled 必须为 true 且 dedup.scope 必须为 global；这是 counterfactual set 串行原子准入的前提。
- output.meta_mode 必须为 inline，output.rejects 必须为 none；反例 attempt 只写聚合计数，不打开 rejects 通道。
- limit 禁止使用；测试和小运行通过较小 count 配置表达，不截断全局精确时间线。
- quality 若开启，只允许 pointwise 和固定 threshold；pairwise、top_ratio 及任何生效 class override 均报错。

## 5. 公共配置

### 5.1 generate 主节

~~~toml
[generate]
enabled = true
form = "sequence"
mode = "declared"
semantic_llm = "default"
evaluation_llm = "judge"
max_slot_attempts = 4
state_validator = "hooks.py:validate_state"
~~~

| 字段 | 类型 | 约束 |
|---|---|---|
| form | flat 或 sequence | 缺省 flat；旧 generate.stream 不参与判定 |
| mode | declared 或 instruction_only | form = sequence 时必填 |
| semantic_llm | string | sequence 时必填，引用支持文本的 LLM profile |
| evaluation_llm | string | sequence 时必填，必须与 semantic_llm 是不同 profile 名 |
| max_slot_attempts | integer | 缺省 3，范围 1..20 |
| state_validator | hook string | 可选；只接收不可变副本，不执行 patch |

两个 profile 都必须显式声明 context_window > 0。DeepSeek profile 可以分别命名 default 与 judge，
即使两者指向同一供应商、模型和 API key 环境变量。

flat 形态继续使用 llms、styles、seed_examples、standalone_count、num_per_record、seeds_per_call 和
num_per_call。sequence 形态显式书写这些 flat 字段是 CONFIG_ERROR；flat 形态显式书写本规格字段同样报错。

内部载体不把两个互斥形态塞入一个可空字段长表：ResolvedConfig.generate 保留通用 enabled/form
与 flat 所需字段，ResolvedConfig.sequence_generation 在 form = sequence 时是冻结 SequenceGenerationConfig，
否则为 null。form = sequence 时任何 flat 字段不会带默认值混入 compiler；form = flat 时不构造
SequenceGenerationConfig。GenerationProgramCompiler 只读 ResolvedConfig.sequence_generation 和 M1 冻结的 ClassView。

### 5.2 declared 的 sequence class

~~~toml
[class.ticket_booking]
description = "一次订票请求与处理结果"

[class.ticket_booking.generate]
instruction = "保持路线、日期、乘客、请求和票号前后一致。"
state_schema_path = "schemas/state.json"
initial_state_source = "catalog"
initial_state_catalog_path = "catalogs/ticket-booking.jsonl"
~~~

每个非零 declared slot 引用的 class 必须声明 instruction、state_schema_path 和 initial_state_source。
initial_state_source 只有 llm 与 catalog：

- llm 每次 attempt 调用 ScenarioSeedGenerator 生成完整 ScenarioSeed。
- catalog 要求 initial_state_catalog_path；JSONL 每行是完整 ScenarioSeed，不再调用 seed LLM。
- catalog 在应用完整配置后按 sequence class 的 slot 数无放回分配；行数不足或任一行无效在启动期失败。
- 同一个 slot 的重试继续使用同一 catalog 行，避免用换世界掩盖生成失败。

### 5.3 ScenarioSeed

ScenarioSeed 的固定外壳如下；initial_state 的内部形状由 state_schema_path 决定：

~~~json
{
  "initial_state": {},
  "actors": {
    "requester": {"goal": "...", "identity": {}, "style": {}},
    "system": {"goal": "...", "identity": {}, "style": {}}
  },
  "shared_facts": {
    "public": {},
    "hidden": {}
  },
  "style": {},
  "time_context": {}
}
~~~

declared actors 的键必须恰等于该 pattern 全部 role.actor 与 role.observers 的并集。同一 sequence class
下的所有 pattern 必须声明相同 actor 集，才能共享 class-level catalog。instruction-only 允许 ScenarioSeedGenerator
生成一至八个 actor，后续 EventPlan.actor 必须引用其中一个。

ScenarioSeed 不能包含 variant、目标违规或某个分支的结果。catalog 行与 LLM 结果使用同一固定 Schema 和同一验证路径。
EventPlanner 与 FrameRenderer 只接收 shared_facts.public；shared_facts.hidden 只供 StateEvaluator 与
SemanticEvaluator 做泄漏判定。

### 5.4 frame class

~~~toml
[frame.class.task_request.generate]
instruction = "请求者提出尚未完成的同一订票请求。"
schema_path = "schemas/frame-request.json"
~~~

每个 role 引用的 frame class 必须声明 instruction 和 object 类型 JSON Schema。sequence 形态不支持 string
frame payload；所有 payload 必须是 JSON object，避免混合载荷破坏 binding 和真实端点的结构稳定性。

### 5.5 sequence pattern

~~~toml
[generate.pattern.booking_success]
sequence_class = "ticket_booking"
description = "请求者提交订票需求，系统确认受理并在允许时间内给出出票结果。"
order = ["request", "acknowledge", "confirm"]
max_span_s = 1800

[[generate.pattern.booking_success.roles]]
name = "request"
frame_class = "task_request"
actor = "requester"
read_roots = ["/public", "/request", "/actors/requester"]
write_roots = ["/request", "/actors/system/knowledge"]
publish_roots = ["/request/id", "/request/status"]
observers = ["requester", "system"]
state_instruction = "创建 pending 请求。"
pre_state_schema_path = "schemas/pre-request.json"
payload_bindings = [
  { payload_path = "/request_id", state_phase = "after", state_path = "/request/id" }
]

[[generate.pattern.booking_success.roles]]
name = "acknowledge"
frame_class = "acknowledgement"
actor = "system"
read_roots = ["/request", "/actors/system"]
write_roots = ["/request", "/audit"]
publish_roots = ["/request/id", "/request/acknowledged"]
observers = ["requester", "system"]
state_instruction = "确认已接收请求，不得声称已经出票。"
payload_bindings = [
  { payload_path = "/request_id", state_phase = "after", state_path = "/request/id" }
]

[[generate.pattern.booking_success.roles]]
name = "confirm"
frame_class = "confirmation"
actor = "system"
read_roots = ["/request", "/ticket", "/actors/system"]
write_roots = ["/request", "/ticket", "/audit", "/sla"]
publish_roots = ["/request/id", "/ticket/id", "/request/status"]
observers = ["requester", "system"]
state_instruction = "产生与当前分支相符的最终处理结果。"
payload_bindings = [
  { payload_path = "/request_id", state_phase = "after", state_path = "/request/id" },
  { payload_path = "/ticket_id", state_phase = "after", state_path = "/ticket/id" }
]

[[generate.pattern.booking_success.gaps]]
name = "request_to_acknowledge"
before = "request"
after = "acknowledge"
min_gap_s = 0
max_gap_s = 120

[[generate.pattern.booking_success.gaps]]
name = "acknowledge_to_confirm"
before = "acknowledge"
after = "confirm"
min_gap_s = 30
max_gap_s = 1200
~~~

pattern 的强制语义：

- role name 唯一，每个 role 恰出现一次。
- order 必须恰好排列全部 role，不遗漏、不重复。
- max_span_s 必填，大于零，使用闭区间上限。
- order 中每个相邻 role pair 必须恰有一条对应 gap；每条 gap 的 max_gap_s 必填。
- 可以追加非相邻 role gap，但同一 before/after pair 不得重复。
- gap 必须沿 order 正向；min_gap_s 缺省零，且不大于 max_gap_s。
- 秒数最多六位小数，加载时无损转整数微秒；min 和 max 都是闭区间。
- read_roots、write_roots、publish_roots 使用 RFC 6901 JSON Pointer token 前缀比较，不用字符串前缀。
- 同一个 roots 列表内不允许冗余的祖先/后代项；read、write、publish 三个列表之间允许相交。
- test 操作的 path 必须落入 read_roots；add、remove、replace 的 path 必须落入 write_roots。
- publish_roots 必须在事件后存在；observers 必须是 ScenarioSeed actor。
- pre_state_schema_path 可选；在 patch 前对完整 state 验证。
- payload binding 的精确 path/value 进入 FrameRenderer prompt；LLM 返回完整 Schema object，系统再按声明序
  用 RFC 6902 add 语义机械覆盖并复验同一完整 Schema，不改写用户 Schema。
- payload binding 的 state_path 必须同时被当前 role.read_roots 与 publish_roots 覆盖；binding 不能成为
  hidden state 的旁路解密或发布机制。

一个 frame class 可以被多个 role 复用。missing 目标 role 的 frame class 必须在 pattern 中唯一；
reordered 的两个目标 role 必须相邻且 frame class 不同。

### 5.6 counterfactual set

~~~toml
[[generate.counterfactual_sets]]
name = "booking_success_training"
pattern = "booking_success"
count = 2

[[generate.counterfactual_sets.variants]]
name = "positive"
kind = "positive"
outcome_schema_path = "schemas/outcome-positive.json"

[[generate.counterfactual_sets.variants]]
name = "missing_acknowledgement"
kind = "missing"
target_role = "acknowledge"
outcome_schema_path = "schemas/outcome-missing.json"

[[generate.counterfactual_sets.variants]]
name = "confirmation_before_acknowledgement"
kind = "reordered"
target_before = "acknowledge"
target_after = "confirm"
outcome_schema_path = "schemas/outcome-reordered.json"

[[generate.counterfactual_sets.variants]]
name = "confirmation_timeout"
kind = "interval_exceeded"
target_gap = "acknowledge_to_confirm"
min_excess_s = 1
max_excess_s = 600
outcome_schema_path = "schemas/outcome-timeout.json"
~~~

count 是 counterfactual set 数，不是尝试数。每个 set 至少一个 variant；variant name 和预期违规签名均须唯一。
每个 variant 必须有 outcome_schema_path。positive 可以缺省，但 hidden baseline 始终存在。

| kind | 结构变换 | 唯一预期违规 |
|---|---|---|
| positive | baseline 不变 | 空集 |
| missing | 删除 target_role | missing_role(target_role) |
| reordered | 交换相邻 target_before 与 target_after | reordered(target_before,target_after) |
| interval_exceeded | after 及后缀整体后移 | gap_above_max(target_gap) |

interval_exceeded 的实际目标 gap 必须在
[max_gap + min_excess, max_gap + max_excess] 闭区间内。编译器必须证明目标变换与所有非目标 gap、
max_span 和日历约束可同时满足；不能运行时放宽。

### 5.7 instruction-only

~~~toml
[[generate.instruction_only]]
name = "open_booking"
sequence_class = "ticket_booking"
count = 1
len_range = [3, 6]
instruction = "生成一次完整、自然、状态连续的订票交互。"
state_schema_path = "schemas/state.json"
~~~

可以声明多行，name 与 sequence_class 引用均须有效。count 为精确 sequence 数。len_range 两端在 1..64，
下界不大于上界。state_schema_path 可选；缺省使用只要求 JSON object 的固定 Schema。

instruction-only 不允许 pattern、counterfactual_sets、role permission、outcome Schema 或 expected violation。
每条 slot 是一个单序列精确提交组。LLM 选择的 frame class 必须来自已声明、具有 object 生成 Schema 且不是
`generate.noise.frame_class` 的闭集；至少保留一个可选 frame class。
每个 attempt 固定调用 ScenarioSeedGenerator；它使用 instruction 与可选 state Schema 生成完整 ScenarioSeed，
不支持 catalog source。EventPlanner 可读完整 current state，但 actor 必须来自该 seed 的 actors。

### 5.8 timeline、calendar 与 noise

~~~toml
[generate.timeline]
timestamp_start = "2026-01-05T09:00:00+08:00"
event_gap_s = [5, 60]
primary_sessions = 7
crossed_primary_sessions = 1
session_max_events = 16
session_max_span_s = 3600
session_gap_s = 3600
noise_events = 2
duplicate_sequences = 1

[generate.calendar_window.service_hours]
utc_offset = "+08:00"
days = ["mon", "tue", "wed", "thu", "fri"]
intervals = [["08:00:00", "12:00:00"], ["13:00:00", "18:00:00"]]

[generate.noise]
frame_class = "noise"
instruction = "生成与任何任务无关、没有可执行诉求的一条自然输入。"
~~~

role 可用 calendar_window = service_hours 引用命名窗口。窗口使用固定 UTC offset 和同日半开墙钟区间。
event_gap_s 只约束 instruction-only 相邻位置，以及 noise/replay 的铺设间隔；declared role 间隔只由 pattern.gaps
与 max_span 决定，不能用 event_gap_s 意外收窄超时变体。

设 primary sequence 总数为 N，crossed_primary_sessions 为 D，则 primary_sessions 必须等于 N - D。
每个 primary session 恰有一个或两个不同 counterfactual set 的 owner；同一个 set 的不同 variant 永不共 session。
每个 replay sequence 独占一个尾部 session。

noise_events 大于零时 generate.noise 必填，frame_class 必须有 object 生成 Schema，且不得被任何 role 使用。
noise 是无 owner、无 state patch 的精确事件槽。duplicate_sequences 从已提交 positive primary sequence
按 declaration order、scenario_index 选取，不放回；positive source 不足是启动期错误。
instruction-only 要求 duplicate_sequences = 0；它没有 positive variant 真值。
instruction-only 同时要求 crossed_primary_sessions = 0、primary_sessions = N；该模式不做 crossing。

## 6. 静态上限与上下文

| 对象 | 固定上限 |
|---|---:|
| pattern roles | 32 |
| variants per counterfactual set | 8 |
| instruction-only events | 64 |
| ScenarioSeed canonical JSON | 65536 bytes |
| state or outcome Schema file | 65536 bytes |
| frame Schema file | 65536 bytes |
| one event patch canonical JSON | 16384 bytes |
| one rendered payload canonical JSON | 65536 bytes |
| one instruction | 32768 UTF-8 bytes |

配置期对 instruction、Schema 和最小固定模板执行完整预算检查。运行期在每次调用前以实际完整 prompt、
实际 JSON Schema、max_output_tokens 与冻结 margin 验证现有上下文不变式。ScenarioSeed、ActorView、patch、payload、
完整 EventTrace 和 semantic blind-review 输入既不截断，也不使用摘要或替代 Schema 通过预检。

派生 record_units = primary_sequences + primary_events + noise_events + replay_events。record_units 必须在
1..500000；compile 期超过上限直接 CONFIG_ERROR。stream_rows = primary_events + noise_events + replay_events，
也不得超过 500000。所有 count 使用 Python integer 后先做上限检查，再进入 OR-Tools IntVar。

另冻结 retained_content_bytes 上限 536870912（512 MiB）。该值以最终将写入的 main 和 stream
每行 canonical UTF-8 字节数之和计算，因此保守重复计入两个视图中的同一 payload，也计入
annotation、generation truth、replay 和所有元数据；只排除发射时才增加的墙钟观测字段。
每个 AttemptTransaction 在 dedup commit 前精确试算接受后的累计值。ScenarioPlan 已冻结每个 replay source；
当某个 positive candidate 是 replay source 时，DeliveryController 必须先让 M11 从最终 PipelineItem 装配
SequenceRows，再从其中的最终 primary_stream_rows 构造全部已规划 ReplayRows。prospective retained_content_bytes
恰等于既有已接受累计、当前 set 的每个 SequenceRows.retained_content_bytes 与本次 ReplayRows.retained_content_bytes
之和。超限归 sequence_memory_budget 并重试整个 source slot，零 dedup/dataset/replay commit。通过后 replay rows
与 source 在同一内存临界区提交，后续不再构造未计费 replay row。noise slot 超限归 noise_memory_budget。
不截断 payload、annotation 或 truth 来规避上限。

结构真值、状态、patch、Schema、ActorView 和完整 EventTrace 不允许裁剪后继续判定通过。超限或完整调用不适配时，
配置对象在启动期失败，生成内容在运行期消耗当前 slot attempt。只有非真值的可选风格提示可以删除；删除规则固定且记录计数。

规划按 session block 求解；delivery 按 slot declaration order 处理。每个 slot 提交后立即释放完整状态快照、
LLM 中间对象、ProjectedSequence、PipelineItem 与 AttemptTransaction，只保留最终 main rows、最终 stream rows、
dedup 索引和汇总计数。primary main member 与 stream primary row 在装配前引用同一个冻结 event payload 对象；
replay projection 从最终 source row 复制其值，不保留另一份世界执行对象。

实现门同时包含 record_units = 500000 的最小载荷结构压测、接近 512 MiB 输出字节包络的混合载荷压测，
两者 peak RSS 都不超过 4 GiB。独立用例钉住 500001 record units 在 compile 期拒绝，以及
retained_content_bytes 恰为上限时通过、超一 UTF-8 byte 时 whole-slot 拒绝。

## 7. GenerationProgram 编译

GenerationProgramCompiler 在任何凭据物化和 LLM 请求之前完成：

- 解析 sequence class、frame class、pattern、role、gap、counterfactual set 和 instruction-only 引用。
- 验证全部 JSON Schema、JSON Pointer、hook 签名、catalog 外壳与 catalog cardinality。
- 为每个 variant 冻结唯一 expected_violation 和 causal divergence role。
- 校验 delivery slot 的精确 cardinality 与声明序；实际 DeliverySlot 只由 ScenarioPlan 冻结，并在其中写入
  catalog_row_index（LLM source 为 null）。catalog 按 class、声明序、scenario_index 无放回分配，slot 重试不换行。
- 校验 timeline 精确恒等式、crossing、noise、replay source 和 session 容量。
- 计算每种调用的完整最小上下文预算与调用上界。
- 生成 canonical program digest，供 validate、dry-run、run、ID 与 report 共用。digest 输入覆盖全部语义字段，
  排除 `digest` 自身与 hook callable，只写 `ResolvedHook.reference`；ScenarioPlan.digest 同样排除自身。

编译阶段不随机抽样、不读 API key value、不调用 LLM、不执行下游 stage。

同一 sequence class 可以被多个 pattern 使用。M1 在 ResolvedConfig 中冻结全部 sequence ClassView；
GenerationProgramCompiler 只消费这些视图，不再解析或合并 class override。EventProjector 写 inherited Classification，
classify stage 必须静态跳过且调用数为零。

## 8. ScenarioPlanner

### 8.1 唯一入口

ScenarioPlanner 只有一个生产入口：

~~~python
def compile_scenario_plan(program: GenerationProgram, seed: int) -> ScenarioPlan:
    ...

def referenced_profiles(
    config: ResolvedConfig,
) -> tuple[list[str], list[str]]:
    ...

def resolve_credentials(config: ResolvedConfig) -> RuntimeCredentials:
    ...
~~~

validate、dry-run 和 run 必须调用相同的 program compiler、block allocator 和 CP-SAT model builder。
dry-run 可以丢弃 decoded payload-free plan，但不能只做近似检查。

### 8.2 CP-SAT 边界

ScenarioPlanner 先按声明序冻结 DeliverySlot、catalog_row_index 与 block membership；catalog 分配是确定性整数映射，
不进入 solver。随后 CP-SAT 负责：

- declared baseline 的 role presence、完整 order、logical_time_us、gap 和 max_span。
- missing、reordered 和 interval_exceeded 的机械变换与非目标约束。
- instruction-only slot 的固定 length、每个位置 logical_time_us 和 frame position。
- 每个 primary sequence 的投影 timestamp、session、crossing 与全局严格递增。
- role calendar window、session event capacity、session span、session gap。
- exact noise slot、positive replay source 与 replay session。
- counterfactual set、variant、sequence、session、noise 和 replay 的精确数量。

CP-SAT 不负责实体、actor goal、自然语言、状态 patch、业务结果、语义分数或下游 acceptance。

### 8.3 反例时间变换

每个 set 先有一条满足全部 pattern 约束的 baseline logical timeline：

- positive 完整复用。
- missing 删除 target role，其他 role 的 logical_time_us 不变。
- reordered 交换两个目标 role 的 logical_time_us，其他 role 时间不变。
- interval_exceeded 固定 target gap.before 及其前缀，把 after 及其后缀整体后移，后缀内部 gap 不变。

solver 必须证明每个变体的所有非目标 gap、max_span 和 calendar window 成立。不同 branch 的投影 session 起点可以不同，
但相邻事件的实际 timestamp 差必须等于 logical timeline 的差。

### 8.4 block 与确定性

block allocator 先用整数算术按完整 primary session 分配全局 count；最后一个 block 接收余数。一个 crossed session
的两个 owner 永远在同一 block。单 block 最多 4096 个 primary event；超出时从下一个完整 session 起新 block。

ScenarioBlock 的键固定为 `(slot_key, variant_name)`：隐藏 baseline 与 instruction-only 唯一 branch 的
`variant_name = None`，声明 variant 使用其配置名。positive 不复制另一份计划，而是显式复用同 slot 的 baseline。
planner 只冻结位置、role、逻辑时间、工件时间与 session；frame class 和 actor 不进入 PlannedEvent：declared
从 RoleSpec 机械解析，instruction-only 在 seed 产生后由 EventPlan 选择。noise 与 replay 分别使用 NoiseSlot 和
ReplayLayout，不用空字符串、null actor 或其他 PlannedEvent sentinel 冒充。

OR-Tools 固定为 pyproject 锁定版本，num_search_workers = 1，random_seed 由 run seed 和 block identity 派生。
每个优化层使用 max_deterministic_time = 10.0，不使用 wall-time budget。只有 OPTIMAL 可解码；
INFEASIBLE 抛 generation_plan_infeasible 并 exit 2；FEASIBLE 或 UNKNOWN 抛 generation_plan_budget 并 exit 4；
MODEL_INVALID 抛 generation_plan_internal 并 exit 4；
不改走贪心或使用 incumbent。

相同版本、program、seed 的 ScenarioPlan 必须逐字节相同。LLM 输出不在该复现承诺内。
validate、dry-run 与 run 对相同 ResolvedConfig 和 seed 产生的 GenerationProgram.digest、DeliverySlot、
catalog_row_index、ScenarioBlock 与 ScenarioPlan.digest 必须逐字节相同；slot 重试不得重新编译 program、重新规划，
或改变 catalog_row_index。

### 8.5 独立 oracle

小词表与小长度测试必须用不导入 planner 实现的枚举 oracle，逐项比较：

- slot 和 variant 数。
- role presence 与 order。
- 每条 gap、目标超时范围和 max_span。
- session、crossing、noise、replay 与全局 timestamp。

不能用 planner decoded witness 作为期望值。

## 9. 场景、事件与状态

### 9.1 ScenarioSeedGenerator

llm source 的固定输出字段为 initial_state、actors、shared_facts、style、time_context。
system prompt 只描述它在创建尚未发生任何 variant 事件的世界；user prompt 包含 sequence class description、
class generation instruction、actor 名闭集和 state Schema。

输出不得包含 pattern name、variant name、role order、expected violation 或最终 outcome。代码侧固定 Schema 拒绝多余字段，
initial_state 再经过用户 state Schema，actors 键再与 program actor set 比较。

catalog source 直接返回已验证 ScenarioSeed，零 seed LLM 调用。

### 9.2 baseline 与 variant 事件循环

每个 slot attempt 的顺序冻结为：

~~~mermaid
flowchart TD
    S["ScenarioSeed"] --> B["baseline role 逐事件 plan/execute/render"]
    B --> J["baseline mechanical + semantic evaluation"]
    J --> V["按 variant 声明序构造 branch"]
    V --> C{"位于 protected prefix?"}
    C -- 是 --> R["复用 EventDraft，重派生 branch id/time"]
    C -- 否 --> P["逐事件 plan/execute/render"]
    R --> E["branch evaluation"]
    P --> E
~~~

variant 若为 positive，直接使用 baseline branch。若配置无 positive，baseline 不进入主输出或下游，但仍必须完整通过生成侧判定。

逐事件循环每成功执行并渲染一个事件就构造 EventDraft。它包含后续 actor history、状态重放和 semantic review 所需的
全部事件内容，但刻意没有 role。declared branch 完成全部 draft 后，PatternEvaluator 只从其 ObservedEvent 投影重新绑定
actual role；通过后才为每个 draft 增加该唯一 role 并构造 EventTruth。instruction-only 不运行 PatternEvaluator，
由位置机械增加 position_NNN role。EventTrace 只接受 EventTruth，不接受 EventDraft。

### 9.3 ActorView

declared ActorView 固定包含：

~~~json
{
  "actor": "system",
  "goal": {},
  "read_state": {},
  "observations": [],
  "logical_time_us": 180000000,
  "wait_since_previous_us": 175000000
}
~~~

read_state 由 role.read_roots 在当前 state 上确定性投影。observations 只含此前 role.publish_roots 向该 actor
发布的 canonical path/value snapshot、source event_key 和 logical time。不存在的 read root、publish root 或非法 Pointer
使 attempt 失败；不静默忽略。

EventPlanner 只接收 ActorView、ScenarioSeed.shared_facts 中明确标记 public 的部分、当前 role 的
state_instruction、frame instruction、pre-state Schema 摘要和允许的 JSON Patch operation Schema。
它不接收完整 initial_state、其他 actor goal 或 hidden shared fact。

instruction-only 的 EventPlanner 接收该 instruction slot 的完整 generation instruction、冻结 sequence length、
完整当前 state、完整既有 EventDraft history，以及 ScenarioSeed 中按声明序排列的 actor goal/identity/style profile，并在 truth 中
明确 semantic knowledge guarantee。它在选择 actor 之前没有 ActorView；请求中的 actor_view 固定为 null，actor
选定后再从完整 history 构造供 FrameRenderer 使用的 ActorView。declared 请求的 actor_view 必须非 null，
visible_state、history 与 actor_profiles 固定为 null；其 prompt 只能读取 ActorView 与 public facts。

### 9.4 EventPlanner 输出

每次调用只输出一个事件：

~~~json
{
  "frame_class": "confirmation",
  "actor": "system",
  "intent": "完成出票并告知请求者",
  "patch": [
    {"op": "test", "path": "/request/status", "value": "pending"},
    {"op": "replace", "path": "/request/status", "value": "ticketed"},
    {"op": "replace", "path": "/ticket/id", "value": "T-100"}
  ]
}
~~~

declared 输出的 frame_class 和 actor 必须恰等于冻结 role。instruction-only 输出必须落在闭集 frame class，
actor 为非空 string。patch 只允许 test、add、remove、replace，至少一个 test，且所有 test 连续位于变更操作之前。
move 与 copy 拒绝。

EventPlanner prompt 不呈现 variant name、expected violation、target 或 evaluator 结果；variant name 只保留在
EventExecutionContext 中供 state_validator 定位。EventExecutionContext 是唯一根输入，携带 program、plan、slot、
variant、event_index、ScenarioSeed、current state 与 history。`build_event_plan_request` 先验证 slot 确实属于 plan、
block key 存在且 event_index 有效，再从该根机械派生 prompt-safe EventPlanRequest；调用者不能另传一份 request。
任何内部引用失配都归 generation_downstream_contract、exit 4、零 LLM call 且不消耗 slot attempt。

EventExecutionContext 不含 EventPlan；M8 后置验证每次只从当次 candidate 构造唯一 EventPlan，再与同一个 context
一起执行。`plan_event` 必须同时返回 EventPlan 与 M8 对该候选缓存的 EventExecution，调用者不得丢弃 proof
后再执行一次 patch。

test 只承诺原子 guard；基础 Schema、pre-state Schema、outcome Schema 和 state validator 只证明用户声明与 hook
的一致性。LabelKit 不声称宽松 Schema 足以表达业务正确性。

### 9.5 StateExecutor

StateExecutor 对每个事件：

- 深拷贝 current state。
- 用 jsonpatch 库按 RFC 6902 顺序执行完整 patch，in_place = false。
- 在应用前检查 operation 闭集、test 顺序、read/write root 权限。
- 应用前验证可选 pre-state Schema。
- 应用后验证基础 state Schema。
- 调用可选 state_validator 的只读深拷贝。
- 计算 canonical state_before_hash 与 state_after_hash。
- 仅在全部成功后提交 next state。
- 从 next state 读取 publish_roots，追加到 observers 的 observation history。

EventPlan 的结构结果进入 M8 的单次调用后置验证：StateExecutor 在副本上试执行，产生
PostValidationResult。pre-state、基础 state Schema 或 state validator 返回的普通违规进入同一次
EventPlan 的有界 L3 repair；每个候选对象的后置验证只执行一次。后置验证异常、非法返回或 repair 耗尽
直接使当前 slot attempt 失败，不用异常文本再请求 LLM 修复。

通过后置验证的 PostValidationResult 携带冻结 EventExecution。正式事件提交直接消费该对象，
不再执行 patch、Schema 或 state validator，因此不存在同一候选通过后 hook 第二次改变结果的窗口。
state validator 必须确定性且无副作用；M1 少数验证以相同深拷贝输入连续调用两次并比较
归一化违规字节。state validator 的违规字符串只进入 L3 与 trace full，普通日志/report 只写 value-free kind。

### 9.6 payload binding 与 FrameRenderer

FrameRenderer 每个新事件调用一次。它只接收 ActorView、EventPlan、publish snapshot、state before/after hash、
此前该 actor 已观察摘要、frame instruction、完整 frame Schema 与机械算出的 binding values。完整 state_before、
state_after、EventExecution 和 state validator 都不进入 RenderEventRequest。

它不能增删事件、改变 frame_class、actor、patch、时间或 role。返回 object payload 后：

- 系统按 binding 的 before/after state snapshot 读取 state_path，并把精确的 payload_path/value 映射写入 prompt。
- LLM 仍按完整 frame Schema 返回完整 object；系统按声明序在深拷贝上机械覆盖 payload_path。
- 用同一完整 frame Schema 再验证；父路径缺失、覆盖失败或复验失败都归 frame_schema 并拒绝当前 attempt。
- canonical payload 超过上限或完整 prompt 不适配 context budget 时 attempt 失败，不裁剪真值。

LabelKit 不尝试把任意 Draft 2020-12 Schema 改写成所谓 writable Schema；`$ref`、`allOf`、`oneOf`、`if`、
`dependentSchemas` 与 `unevaluatedProperties` 下不存在一般等价的“删一个 instance path”变换。jsonpatch 只执行
实例 patch，不承担 Schema 翻译。M1 只验证 binding pointer、权限和冲突；最终完整 Schema 是唯一输出判据。
binding 使用 RFC 6902 `add` 的实例语义：目标成员存在时覆盖，目标成员不存在时新增，但除根路径外的所有父容器
必须已经存在。多个 binding 按 RoleSpec.payload_bindings 声明序串行应用；同一 payload_path 重复、一个 path 是另一个
path 的祖先或后代、或根路径 binding 都在 M1 拒绝，避免声明序产生两套可见真值。

instruction-only 没有 RoleSpec，EventExecutionContext 从 program/slot 解析出的 role 固定为 null；StateExecutor 跳过不存在的 root containment、
pre-state Schema、publish_roots 和 observers，只执行 patch operation 闭集、test 前缀、原子 JSON Patch、基础 state Schema
与可选 state validator。该模式没有 payload binding；后续 actor history 直接来自完整既有 EventDraft。

### 9.7 protected prefix

复用 EventDraft 时，state patch 在新的 branch initial_state 上重新执行，并校验 before/after hash 等于 baseline。
payload、ActorView、intent、actor、frame_class、logical_time_us 与 baseline canonical bytes 相同；PatternEvaluator
通过后派生的 protected-prefix EventTruth.role 也必须相同。
event_key 相同；event_id、owner_sequence_id、world_branch_id 和 artifact timestamp 不属于复用字节。

CouplingEvaluator 独立比较 protected prefix。任何一个受保护字段改变都产生 coupling_violation，并使整个 attempt 失败。

### 9.8 noise 交付

noise slot 在全部 primary slot 接受后按 ordinal 串行执行。NoiseRenderer 只接收 noise instruction、
noise frame Schema、全部 sequence/frame class 的名称与 description 闭集、冻结 timestamp 和 attempt identity；
它不接收任何 ScenarioSeed、EventTrace 或 primary payload。输出经 M8 和 noise frame Schema 后，
NoiseSemanticEvaluator 使用 evaluation_llm 独立返回 unrelated_to_declared_tasks、no_executable_task 与 realism
三个 boolean 及闭集 reason_codes；三项必须全 true。

最后用 dedup.minhash_threshold、dedup.minhash_num_perm 和 dedup.ngram 建立 attempt-local SimilarityFilter，
先加入全部 primary member.text 和已接受 noise，再对 canonical_json(payload) probe。命中近重归 noise_similarity
并重试该 slot；通过后才 commit 它的签名。noise 不进 quality、annotate、verify 或 main dedup group，
只作为时间流中可被 segment 机械删除的精确干扰帧。

## 10. 独立判定

### 10.1 PatternEvaluator

PatternEvaluator 只接收最终 primary owner 的 ObservedEvent(event_id、frame_class、timestamp)，不接收 planner role witness、
expected binding 或 variant transformation。它按 frame class 分组，并把 pattern order 中该类第 k 次出现的 role
稳定绑定到 observed order 中该类第 k 次出现的 event；随后按完整 pattern order 判 cardinality、order、gap 与 span。
missing 目标 frame class 唯一、reordered 两端 frame class 不同的配置约束保证目标反例没有歧义。输出：

~~~json
{
  "actual_bindings": {"request": "event-a", "confirm": "event-c"},
  "actual_violations": [
    {"kind": "missing_role", "target": "acknowledge"}
  ]
}
~~~

违规按依赖顺序归一化：role cardinality、adjacent order、applicable gaps、max span。缺 role 时不重复报关联 order/gap；
目标 reordered 时不重复报该边的 gap。variant 通过条件是 actual_violations 与 expected_violation 恰等，不是包含。

actual_bindings 是 declared EventTruth.role 的唯一来源。PatternEvaluator 通过后才构造 EventTruth；planner witness
不得写入 EventTrace 或传给 projector。instruction-only 的 EventTruth.role 由冻结位置机械写成 position_NNN。

### 10.2 StateEvaluator

StateEvaluator 从 ScenarioSeed.initial_state 重新执行全部 branch patch，不使用 StateExecutor cache，并证明：

- 每个 patch 原子成功。
- 每步基础 Schema、可选 pre-state Schema 和 state validator 通过。
- 每步 state hash 与 EventTruth 一致。
- payload binding 等于 before/after snapshot。
- final_state 与重放结果完全相等。
- final_state 满足 variant outcome Schema；instruction-only 只验证基础 Schema。
- protected prefix 的状态与 payload耦合成立。

StateEvaluationRequest 显式携带当前 DeliverySlot 与 baseline_events。slot 使 instruction-only 在多个声明行时仍能
唯一选择 state Schema；baseline_events 使 protected_prefix_valid 可以独立计算，不能从当前 branch 或 planner
witness 反推一个伪 baseline。state validator 只从 program.state_validator 读取，不在 request 中复制。
CouplingEvaluator 另以 CouplingEvaluationRequest 对同一 protected prefix 做字段级 byte compare；两个 oracle
均通过才进入 downstream。

### 10.3 SemanticEvaluator

SemanticEvaluator 使用 evaluation_llm 和独立固定 Schema，一次读取完整 semantic blind-review view：
ScenarioSeed、顺序 SemanticReviewEvent、所有 ActorView、逻辑等待、最终 payload 与 final_state。该请求不携带 EventTrace、
variant/target、expected 或 actual violation、PatternEvaluation、StateEvaluation 或既有 SemanticEvaluation，因而
不会与 EventTrace.semantic_evaluation 构成自引用。不得裁剪状态或事件后继续返回 pass。
declared 的 pattern_description 恰为 SequencePattern.description；instruction-only 恰为 InstructionOnlySpec.instruction，
调用方不得另行摘要或拼接。SemanticEvaluation 通过后才把它与既有机械 verdict 组装成最终 EventTrace。

~~~json
{
  "causal_consistency": true,
  "actor_knowledge": true,
  "goal_consistency": true,
  "temporal_plausibility": true,
  "cross_frame_consistency": true,
  "realism": true,
  "reason_codes": []
}
~~~

六项必须全 true，reason_codes 必须来自固定闭集且不得包含用户数据。evaluation profile 与 generation profile
使用不同 system prompt、不同 Schema 和不同 profile 名；不能让生成调用自报通过。

### 10.4 CrossViewReconciler

EventProjector 完成后，CrossViewReconciler 是提交前的第二道纯机械边界：

- 每个 primary main sequence 与 stream primary owner 双向一一对应。
- event_id、sequence_id、owner、actual role、frame_class、payload 和顺序一致。
- stream timestamp 与 plan 一致且全局严格递增。
- replay_sequence_id 不出现在 main，且逐位同源关系成立。
- noise 无 owner、role、pattern 或 variant。
- projector 产生任何缺失、额外或篡改字段都拒绝当前 attempt，不允许以 planner 真值修复。

## 11. ID 与投影

除 delivery_digest 外，所有 ID 统一调用 `derive_generation_id(domain, components)`：材料恰为
`canonical_json(["labelkit:v1.18", domain, components])` 的 UTF-8 bytes，canonical JSON 固定
sort_keys = true、separators = comma/colon、ensure_ascii = false；结果为 SHA-256 小写 hex 前 32 字符。
domain 与 components 按下表逐字节冻结，components 是按顺序排列的 JSON array，禁止调用方自行连接字符串。

| ID | domain | components |
|---|---|---|
| declared scenario_id | `declared_scenario_id` | program_digest、counterfactual set name、scenario_index |
| declared world_branch_id | `declared_world_branch_id` | scenario_id、variant name |
| instruction scenario_id | `instruction_scenario_id` | program_digest、instruction slot name、scenario_index |
| instruction world_branch_id | `instruction_world_branch_id` | scenario_id、常量 instruction_only |
| declared event_key | `declared_event_key` | scenario_id、baseline role name |
| instruction event_key | `instruction_event_key` | scenario_id、instruction slot name、scenario_index、position |
| primary event_id | `primary_event_id` | world_branch_id、event_key、artifact timestamp、canonical payload |
| sequence_id | `sequence_id` | world_branch_id、ordered event_id list |
| replay_sequence_id | `replay_sequence_id` | source sequence_id、replay ordinal |
| replay event_id | `replay_event_id` | replay_sequence_id、source event_id、replay timestamp |
| noise event_key | `noise_event_key` | program_digest、常量 noise、noise ordinal |
| noise event_id | `noise_event_id` | run_id、noise event_key、timestamp、canonical payload |
| run_attempt_id | `run_attempt_id` | program_digest、seed |
| run_id | `run_id` | run_attempt_id、ScenarioPlan.digest |

表中的 artifact/replay/noise timestamp 一律是 integer 微秒；canonical payload component 是已验证 JSON object 本身，
不是预先序列化的 string；ordered event_id list 是 JSON array。常量 instruction_only 与 noise 是表中不带反引号的
字面 string。M2 与 generation projector 共用同一个 derive_generation_id，不复制公式。

missing branch 没有目标 role event_key。
delivery_digest 使用完整 64 位 SHA-256，由 M11 唯一计算。哈希先写固定 ASCII header
`labelkit:v1.18:delivery\n`，再按 main、stream 的视图顺序和各自行序写入一个 frame；每个 frame 是
`len(canonical_row_bytes)` 的十进制 ASCII、冒号和 canonical_row_bytes。canonical_row_bytes 由共享
`canonical_delivery_row` 先移除 `_meta.run.started_at`、`finished_at`、`duration_ms`、manifest committed_at 等
仅在发射时添加的墙钟观测字段，再按上述 canonical JSON 规则编码。用户 annotation、generation truth、payload、
时间与 replay 证据都在摘要内。摘要只写入 report，manifest 从 report 读取同一值；不写 main/stream，也不参与
任何 Record ID，避免内容摘要自引用。

retained_content_bytes 与 delivery digest 复用同一个 canonical_delivery_row helper；每行计
`len(canonical_row_bytes) + 1`，其中一 byte 是 JSONL 换行。SequenceRows 只计自己的 main_row 和
primary_stream_rows；ReplayRows 只计自己的 rows。发射期墙钟观测字段既不驻留在产品行中，也不计入该内存上限。
Record.id = sequence_id，member Record.id = event_id。M2 replay 读取 _meta.event.event_id 时验证格式、全文件唯一性和
与同一 stream 工件中 owner grouping/replay provenance 的一致性；不信任不合格 id，也不改用旧公式。

EventProjector 只从当前 DeliverySlot 与 EventTrace 产生 pre-downstream ProjectedSequence；它不产生 noise、replay
或最终输出 bytes。NoiseProjector 从 NoiseSlot 产生 noise row；ReplayProjector 只在 M11 装配结束后，从 source
SequenceRows.primary_stream_rows 与 ReplayLayout 产生 ReplayRows。世界 state 与 patch 默认不写训练输出；
trace.content = full 时写既有独立 trace 通道。

## 12. 精确交付

### 12.1 slot 状态机

~~~mermaid
stateDiagram-v2
    [*] --> Planned
    Planned --> Attempting
    Attempting --> Generated
    Generated --> Evaluated
    Evaluated --> Downstream
    Downstream --> Accepted
    Generated --> Retry
    Evaluated --> Retry
    Downstream --> Retry
    Retry --> Attempting
    Retry --> Exhausted
    Accepted --> Committed
    Exhausted --> DeliveryError
~~~

slot 按 GenerationProgram declaration order 串行进入 admission。一个 slot attempt 内的 variant 也按声明序执行；
实现可以并发无依赖的真实 LLM 请求，但结果归并、dedup probe 和 commit 必须回到固定顺序。

### 12.2 下游事务

生成侧通过后，generation.project 先把每个 EventTrace 投影成只在内存存在的 sequence Record 与基础 primary
stream rows；这些 ProjectedSequence 只服务于 dedup/downstream，尚不宣称最终输出字节。完整 counterfactual set
随后作为 attempt-local batch 依次试算：

~~~text
prospective dedup
→ pointwise quality
→ annotate
→ verify
→ M11 assemble_sequence(final PipelineItem + ProjectedSequence)
→ replay preprojection
→ CrossViewReconciler
→ retained-content prospective check
→ group commit
~~~

DedupIndex 提供 group_probe 与 group_commit，覆盖 exact、MinHash 和启用时的 semantic embedding。
同一 set 内的配对变体互相豁免；与已提交 set 比较。接口冻结为：

~~~python
async def group_probe(
    request: DedupGroupRequest,
    context: RunContext,
) -> DedupProbeToken:
    ...

def group_commit(token: DedupProbeToken) -> None:
    ...
~~~

DedupGroupRequest 携带按 variant 声明序排列的完整 sequence Record。RunContext 是 embedding profile、
LLMClient、MetricsSink 与 breaker 记账的唯一运行来源。probe 可执行真实 embedding 调用，
但不写 exact set、MinHash LSH、embedding candidate store 或其他持久在本进程的去重状态。
DedupProbeToken 是冻结一次性能力，包含 index_generation、ordered record digests 与全部预计算特征；
不包含 API key 或原始 prompt。group_commit 先验证 token 未消费、index_generation 未变和 record digest 未变，
再在一个无 await 的临界区同时写入三类索引并使 token 失效。任一预检失败时零写入；写入中不存在
可恢复外部 I/O，所以它是单进程内的原子状态变换。sequence admission 串行，正常路径不会产生过期 token；
一旦出现归 generation_dedup_transaction 内部错误并终止运行，不重试为普通重复。

quality、sequence/frame annotation、verification 和 item status 存在 attempt-local transaction；各 stage 的 dataset
counter delta 由 DeliveryController 在同一 attempt 的局部整数表累加。失败时全部丢弃，不写 main/rejects；成功时才
合并 dataset counters。LLM usage、调用延迟、token、provider retry、成本、
SchemaEngine resolved-at 统计与 trace event 属于运行事实，所有 attempt 都累计，不得随事务回滚。

下游只调用配置中开启的协作者；关闭的 quality、annotate、frame.annotate 或 verify 是明确的零调用。
quality、annotate、verify 的 class 路由以 PipelineItem.classification 中的 inherited sequence class 为真值，
不再用 cfg.classify.enabled 作为是否读取 ClassView 的门。Emitter 同理：只要 classification 存在就写入闭集类真值，
不因 classify stage 关闭而删掉生成器的 inherited classification。

每个协作者接收 AttemptTransaction，它持有临时 PipelineItem、status counters、ProjectedSequence 和不可变 ClassView。
不得把 attempt-local item 加入 Orchestrator 的全局 batch 或直接调用 Emitter.emit。接受时一次合并这些纯内存结果；
拒绝时丢弃整个 AttemptTransaction，但其 MetricsSink 已记录的 LLM 运行事实不回滚。

SequenceDeliveryEmitter 的 `assemble_sequence` 是纯内存、零 I/O 的 M11 装配入口：它复用普通 emitter 的用户对象、
按类 Schema、scores、annotation、frame annotation 与 verification 规则，返回最终 SequenceRows。每个 delivery slot
的 `batch_no` 固定为一基 declaration ordinal，重试不改变。只有 SequenceRows 才参与 CrossView、replay 字节构造、
retained-content 计费和 GenerationProduct；`GenerationProduct.main_rows` 保存最终 JSON object，不用 Record 伪装落盘行。
ProjectedSequence.main_record 只作为 dedup、quality、annotate 与 verify 的输入载体；不得从它计算最终 main bytes。
SequenceRows.main_row 必须由这些协作者返回的最终 PipelineItem 装配，因此 annotation、frame annotation、quality score、
verification 与 inherited classification 都是 retained-content、delivery_digest 和正式 main output 的组成部分。

ReplayProjector 先深拷贝最终 source primary row，再机械替换 replay 身份与工件时间，因此 source 的 payload、
frame annotation 和其他下游元数据逐位保留。replay `_meta.event.owner_sequence_id = null`，组身份只由
`replay_sequence_id` 表达；它显式写 replay_ordinal、duplicate_of_sequence_id 与 duplicate_of_event_id。
event_key、role、frame_class、actor、logical_time_us 逐位复制；event_id 与 timestamp 新生。`_meta.generation`
固定写 validation_mode = replay、source_validation_mode、sequence_class、scenario_id、source_pattern、source_variant
与 duplicate_of_sequence_id，不产生新的 world_branch_id 或 primary variant 字段。timestamp 统一用 timeline 的
fixed UTC offset 和 `datetime.isoformat(timespec="microseconds")`，不得按本机时区或省略尾部微秒。

普通 process 与 flat generate 继续使用既有 Stage 协议。DeliveryController 不把全仓 Stage 改造成事务框架，
也不调用会立即写 emitter 的 Orchestrator._process_batch。

### 12.3 attempt 与运行终态矩阵

下表仅适用于 `labelkit run` 的 sequence 形态。`validate` 与 `run --dry-run` 即使发现 plan 失败也不写
main、stream、success report、manifest 或 failed report，只按同一 error kind 返回退出码。

| 事件 | 消耗 attempt | 重试 slot | 退出码 | 固定正式路径 | failed report |
|---|---|---|---:|---|---|
| Schema、state、pattern、coupling、semantic 失败 | 是 | 是 | 耗尽为 1 | commit 前不替换 | 耗尽时原子写 |
| dedup、quality、annotate、verify、reconcile 拒绝 | 是 | 是 | 耗尽为 1 | commit 前不替换 | 耗尽时原子写 |
| output_truncated、可恢复 context overflow、provider retryable exhausted | 是 | 是 | 耗尽为 1 | commit 前不替换 | 耗尽时原子写 |
| provider fatal、auth pool exhausted、circuit trip | 否 | 否 | 4 | commit 前不替换 | 已解析路径时 best-effort 原子写 |
| SIGINT / CancelledError | 否 | 否 | 4 | commit 前不替换 | 已初始化 run 时 best-effort 原子写 |
| 启动期配置、hook、catalog 或路径验证错误 | 否 | 否 | 2 | 不替换 | 不写，运行尚未成立 |
| 输出目录或 `.part` 不可写 | 否 | 否 | 4 | 不替换 | 尝试同目录写；不可写则只记英文 stderr kind |
| plan infeasible | 否 | 否 | 2 | 不替换 | 原子写 |
| plan budget 或 planner internal | 否 | 否 | 4 | 不替换 | 原子写 |
| 任一固定正式路径的 commit-I/O 失败 | 否 | 否 | 4 | 可能已替换子集，旧 manifest 不变 | best-effort 原子写 |
| failed report 写入失败 | 否 | 否 | 不改主退出码 | 不改主结果 | stderr 记录英文 kind |

每个 attempt 的 Random 种子是
`int.from_bytes(sha256(canonical_json(["labelkit:v1.18", "attempt_random", [seed, slot_identity,
attempt_index, purpose]])).encode("utf-8")).digest(), "big")`；不得复用 Python `hash()` 或自行连接字符串。
重试可以改变
ScenarioSeed 的 LLM 内容、事件意图、patch 和措辞；catalog source 不换行。pattern、variant、role、logical time、
artifact timestamp、session、noise 和 replay source 永不改变。

### 12.4 exhaustion 与 failed report

任一 sequence slot 或 noise slot 耗尽后立即停止领取新 slot，抛 DeliveryError(kind =
sequence_delivery_exhausted)。在正式 commit 边界之前，Emitter.finalize(deliver = false)，主、stream、success report
和 manifest 均不替换；sequence 形态本就不打开 rejects。

output_stem.failed.report.json 通过同目录 `.part`、flush、fsync 和 os.replace 原子写入。它只含
run identity、artifacts_committed、计数、终态 kind 和 usage，不含数据内容。commit-I/O 失败时
artifacts_committed = false 仅表示没有可以信任的新 manifest，不声称固定路径一定没有发生部分 rename。

此前存在的成功 manifest 始终保持不变。commit 边界之前的失败也保持旧 main、stream 和 success report；
commit-I/O 失败不承诺保持这三个固定路径，消费者必须用旧 manifest 摘要检出不匹配并拒绝读取。

这是 sequence generate_only 的显式中断语义：它不使用 v1.17 普通 process/flat 的优雅 SIGINT 部分收尾，
也不在 circuit trip 后交付已接受前缀。Orchestrator 将这两类终态透传为不可交付的 exit 4；
flat 和 process 的既有 partial-delivery/中断行为不因本规格改变。

## 13. 调用估算与观测

estimate 和 console 的调用键按下列顺序冻结：

| 键 | 含义 |
|---|---|
| scenario_seed_calls | llm initial_state source 的 seed 调用 |
| baseline_event_plan_calls | baseline 新事件规划 |
| variant_event_plan_calls | causal suffix 新事件规划 |
| frame_render_calls | 新 EventDraft 的 payload 渲染 |
| semantic_evaluation_calls | baseline/交付 branch 语义判定 |
| noise_render_calls | noise 精确槽 |
| noise_evaluation_calls | noise 独立语义判定 |
| quality_calls | 下游 pointwise quality 准则调用上界 |
| annotate_calls | 下游 sequence annotate 上界 |
| frame_annotate_calls | 下游 primary member annotate 上界 |
| verify_calls | 下游 verify 上界 |

catalog seed 为零 scenario_seed_calls。protected prefix 复用不计 event plan 或 render。estimate 是一次成功 attempt
的逻辑下界与 max_slot_attempts 的上界，不包含 LLMClient 内部 provider retry；dry-run 同时显示 planned sets、
planned primary sequences、primary events、noise events、replay sequences 和 stream rows。
estimate.successful_attempt_lower_bound 是单个 delivery slot 完整走过 generation、evaluation 和全部已启用下游所需的
逻辑 LLM 调用数，不含 L3 与 provider retry；它用实际 variant 集、保护前缀和 ClassView 计算，
不用常量猜测。
全局 estimate_run 的旧十个调用键序与 total_calls 算式不变。sequence 形态中 generate_calls 等于
上表前七个 generation LLM 细分键之和；这七键按表序作为 sequence_calls 子对象写 report/console，
不再重复计入 total_calls。quality_calls、annotate_calls、frame_annotate_calls 和 verify_calls 使用旧顶层键，
以整个 counterfactual set 在每个 slot attempt 重跑的上界计算。

普通日志只记录 slot key、attempt index、阶段、error kind、计数、profile 和 duration，不记录 prompt、state、patch、payload、
actor view 或 API key。trace full 才可记录生成审计内容，并沿用既有隐私警告。

## 14. 输出契约

### 14.1 main sequence truth

declared 的每条主序列至少包含：

~~~json
{
  "_meta": {
    "generation": {
      "validation_mode": "declared",
      "actor_knowledge_validation": "mechanical_and_semantic",
      "scenario_set": "booking_success_training",
      "scenario_index": 0,
      "scenario_id": "0123...",
      "world_branch_id": "4567...",
      "sequence_class": "ticket_booking",
      "pattern": "booking_success",
      "variant": "confirmation_timeout",
      "expected_violation": {
        "kind": "gap_above_max",
        "target": "acknowledge_to_confirm"
      },
      "actual_violations": [
        {"kind": "gap_above_max", "target": "acknowledge_to_confirm"}
      ]
    }
  }
}
~~~

instruction-only 不得出现 scenario_set、pattern、variant 或 expected_violation，固定写：

~~~json
{
  "validation_mode": "instruction_only",
  "actor_knowledge_validation": "semantic",
  "instruction_slot": "open_booking",
  "scenario_index": 0,
  "scenario_id": "0123...",
  "world_branch_id": "4567...",
  "sequence_class": "ticket_booking"
}
~~~

### 14.2 member event truth

每个 stream row 的顶层固定为 payload 与 _meta；每个 primary member 至少包含：

~~~json
{
  "payload": {
    "request_id": "R-100",
    "ticket_id": "T-100"
  },
  "_meta": {
    "event": {
      "event_id": "abcd...",
      "event_key": "ef01...",
      "owner_sequence_id": "2345...",
      "role": "confirm",
      "frame_class": "confirmation",
      "actor": "system",
      "logical_time_us": 960000000,
      "timestamp": "2026-01-05T09:16:00.000000+08:00"
    }
  }
}
~~~

生成时 member.raw 是完整 stream row，member.text 是 canonical_json(payload)，member.id = event_id。
project-replay.toml 固定 input.text_field = payload、stream.order_by = meta:_meta.event.timestamp。
sequence dedup 按成员 member.text 顺序拼接，因此 replay 的新 event ID 不影响 exact duplicate 命中。
每个 primary row 同时带与 main owner 一致的 _meta.generation sequence truth；replay row 带 source owner 的
sequence class 与必要同源字段，但不伪造 primary variant truth。

M2 对这一生成 stream envelope 执行冻结映射：允许 text_field 指向 object payload，以相同 canonical JSON
生成 Record.text，Record.raw 保留整行。它不依赖 main 文件，而是在同一 stream 工件内重算 primary event_id、
owner 的 ordered sequence_id、replay_sequence_id 和 replay event_id，并验证 duplicate_of_event_id 逐位存在于指向的
primary source。任一格式、唯一性或同源失配在 ingest 阶段 fail closed。

CrossViewReconciler 在生成运行写文件前对内存 main/stream 执行双向检查。check_output.py 在运行后
对两个已落盘视图重复该检查；独立 process replay 不需要也不读取 main 作为隐式旁输入。

role 来自 PatternEvaluator.actual_bindings。instruction-only role 固定为 position_000、position_001 等位置名，
不伪装成业务 pattern role。

noise event 固定 owner_sequence_id、role、scenario_id、world_branch_id 为 null，noise = true。
replay event 固定 owner_sequence_id = null，带 replay_sequence_id、replay_ordinal、duplicate_of_sequence_id 和
duplicate_of_event_id；它不带新的 world_branch_id。M2 只用 replay_sequence_id 分组 replay，不从首次出现次序猜 ordinal。

timestamp、gap 和 elapsed 只在 _meta.event；不向用户 payload Schema 回填 time_fields。

### 14.3 manifest

成功 manifest 路径为 output_stem.manifest.json，键序冻结：

~~~json
{
  "schema_version": 1,
  "run_id": "0123...",
  "delivery_digest": "4567...",
  "artifacts_committed": true,
  "main": {"path": "/abs/out/labels.jsonl", "sha256": "...", "rows": 8},
  "stream": {"path": "/abs/out/labels.stream.jsonl", "sha256": "...", "rows": 27},
  "report": {"path": "/abs/out/labels.report.json", "sha256": "..."},
  "committed_at": "2026-08-21T00:00:00.000000Z"
}
~~~

committed_at 是 manifest 唯一新增的墙钟字段。既有 report/run metadata 的 started_at、finished_at 和 timing
保留观测语义；所有墙钟字段都不参与 run_attempt_id、run_id、Record ID 或 delivery_digest。
manifest 在另外三个文件 fsync 与 rename 后最后替换。

### 14.4 report

report.generate.sequence 键序冻结：

~~~json
{
  "mode": "declared",
  "run_attempt_id": "89ab...",
  "run_id": "0123...",
  "delivery_digest": "4567...",
  "artifacts_committed": true,
  "program_digest": "...",
  "planned_sets": 2,
  "delivered_sets": 2,
  "planned_sequences": 8,
  "delivered_sequences": 8,
  "primary_events": 22,
  "primary_sessions": 7,
  "crossed_primary_sessions": 1,
  "noise_events": 2,
  "replay_sequences": 1,
  "replay_events": 3,
  "replay_tail_sessions": 1,
  "stream_rows": 27,
  "sequence_slot_attempts": 2,
  "noise_slot_attempts": 2,
  "sequence_calls": {
    "scenario_seed_calls": 0,
    "baseline_event_plan_calls": 6,
    "variant_event_plan_calls": 8,
    "frame_render_calls": 14,
    "semantic_evaluation_calls": 8,
    "noise_render_calls": 2,
    "noise_evaluation_calls": 2
  },
  "by_pattern": {
    "booking_success": {
      "positive": {"planned": 2, "delivered": 2},
      "missing_acknowledgement": {"planned": 2, "delivered": 2},
      "confirmation_before_acknowledgement": {"planned": 2, "delivered": 2},
      "confirmation_timeout": {"planned": 2, "delivered": 2}
    }
  },
  "rejected_attempts": {
    "scenario_schema": 0,
    "event_schema": 0,
    "post_validator_invalid": 0,
    "post_validator_exception": 0,
    "state_transition": 0,
    "frame_schema": 0,
    "coupling_evaluation": 0,
    "pattern_evaluation": 0,
    "state_evaluation": 0,
    "semantic_evaluation": 0,
    "sequence_memory_budget": 0,
    "context_overflow": 0,
    "output_truncated": 0,
    "provider_retryable_exhausted": 0,
    "dedup": 0,
    "quality": 0,
    "annotate": 0,
    "verify": 0,
    "reconcile": 0,
    "noise_schema": 0,
    "noise_semantic": 0,
    "noise_similarity": 0,
    "noise_memory_budget": 0,
    "noise_context_overflow": 0,
    "noise_output_truncated": 0,
    "noise_provider_retryable_exhausted": 0
  }
}
~~~

成功时 planned_sets = delivered_sets、planned_sequences = delivered_sequences，且每个 variant planned = delivered。
旧 report.generate.stream、quota、tier、brief、realize、survivor、partial delivery 和 shortfall 键全部删除。
sequence_calls 计逻辑 family 入口次数，包含失败 attempt，不把同一入口内的 L3 修复或 provider retry
重复计数；后两者继续由既有 usage、schema.repair trace 和 provider retry 计数表达。既有
report.schema_engine.resolved_at 仍只统计用户 Schema annotate 调用，不被 generation 内部 Schema 污染。
一次失败 attempt 只按它停止的最终边界进入上述一个 rejected_attempts 桶，不同时计中间修复违规。
noise slot 使用 noise 前缀的七个专用桶，不混入 sequence slot 的同名终态。未列键禁止动态追加；
provider fatal、plan 和 commit-I/O 是 run terminal，写 terminal_error_kind 而不写 rejected_attempts。

failed report 使用相同 usage 与 rejected_attempts 口径，始终包含 run_attempt_id，并另含 nullable run_id、
artifacts_committed = false、failed_slot
（无 slot 时为 null）、attempts_used（无 attempt 时为 0）和 terminal_error_kind；不含 by-pattern 已交付前缀，
因为没有数据被提交。plan 尚未产生时 run_id 为 null，不用伪造的空 plan digest。

### 14.5 ResolvedPaths 与延迟打开

M1 一次冻结 sequence 形态的六个路径：main = run.output，stream = output_stem.stream.jsonl，
report = output_stem.report.json，manifest = output_stem.manifest.json，failed_report =
output_stem.failed.report.json，rejects = sidecar = null。路径冲突、非同目录 `.part` 或不可写在启动期失败。

SequenceDeliveryEmitter 在全部 slot、noise、replay 与 CrossViewReconciler 通过前不打开 main、stream、report
或 manifest；失败 attempt 也不打开任何数据通道。成功后才分别写同目录 `.part`，flush 与 fsync，
按 main、stream、report 的顺序 os.replace，最后单独写入并替换 manifest。failed_report 不在成功 manifest 内，
成功运行不删除历史 failed_report。消费者始终以摘要有效的 manifest 为成功提交真值；failed_report
只是该固定路径最近一次失败的诊断记录，即使相同 program/seed/plan 重跑导致 run_id 与当前 manifest 相同，
也不得用 failed_report 否定有效 manifest。

## 15. 错误与退出码

| error kind | 异常 | 退出码 |
|---|---|---:|
| generation_config_invalid | ConfigError | 2 |
| generation_plan_infeasible | ConfigError | 2 |
| generation_plan_budget | InternalError | 4 |
| generation_plan_internal | InternalError | 4 |
| generation_dedup_transaction | InternalError | 4 |
| generation_downstream_contract | InternalError | 4 |
| post_validator_invalid / post_validator_exception | 当前 slot rejection；耗尽后 DeliveryError | 1 |
| sequence_delivery_exhausted | DeliveryError | 1 |
| sequence_projection_mismatch | 当前 slot rejection；耗尽后 DeliveryError | 1 |
| provider fatal / circuit trip | 既有 provider fatal | 4 |
| generation_commit_io | LabelKitError | 4 |
| generation_failed_report_io | 保留主异常；无主异常时 LabelKitError | 主退出码，否则 4 |

DeliveryError 新增到 labelkit/common/errors.py，不继承 ConfigError。异常文本只含 slot identity、attempt count 和 error kind，
不含 state、patch、payload、prompt 或 API key。

## 16. 冻结数据结构

全部 dataclass frozen，字段上的 Mapping 在构造时复制为只读 MappingProxyType。生产 typedef 或 dataclass 字段必须有中文注释。

| 类型 | 按声明顺序冻结的字段 |
|---|---|
| SequenceClassGenerationConfig | `instruction`, `state_schema`, `initial_state_source`, `initial_state_catalog_path`, `initial_state_catalog` |
| PayloadBindingSpec | `payload_path`, `state_phase`, `state_path` |
| RoleSpec | `name`, `frame_class`, `actor`, `read_roots`, `write_roots`, `publish_roots`, `observers`, `state_instruction`, `pre_state_schema`, `payload_bindings`, `calendar_window` |
| GapSpec | `name`, `before`, `after`, `min_gap_us`, `max_gap_us` |
| SequencePattern | `name`, `sequence_class`, `description`, `roles`, `order`, `gaps`, `max_span_us` |
| VariantSpec | `name`, `kind`, `target`, `outcome_schema`, `expected_violation`, `divergence_role` |
| CounterfactualSetSpec | `name`, `pattern`, `count`, `variants` |
| InstructionOnlySpec | `name`, `sequence_class`, `count`, `len_range`, `instruction`, `state_schema` |
| TimelineSpec | `timestamp_start_us`, `utc_offset_minutes`, `event_gap_us`, `primary_sessions`, `crossed_primary_sessions`, `session_max_events`, `session_max_span_us`, `session_gap_us`, `noise_events`, `duplicate_sequences` |
| CalendarWindowSpec | `name`, `utc_offset_minutes`, `days`, `intervals_us` |
| NoiseSpec | `frame_class`, `instruction` |
| GenerationLimits | `pattern_roles`, `variants_per_counterfactual_set`, `instruction_only_events`, `scenario_seed_bytes`, `state_or_outcome_schema_bytes`, `frame_schema_bytes`, `event_patch_bytes`, `rendered_payload_bytes`, `instruction_bytes`, `record_units`, `stream_rows`, `retained_content_bytes` |
| SequenceGenerationConfig | `mode`, `semantic_profile`, `evaluation_profile`, `max_slot_attempts`, `state_validator`, `patterns`, `counterfactual_sets`, `instruction_only`, `timeline`, `calendar_windows`, `noise`, `limits` |
| GenerationProgram | `mode`, `semantic_profile`, `evaluation_profile`, `max_slot_attempts`, `class_views`, `frame_classes`, `patterns`, `counterfactual_sets`, `instruction_only`, `timeline`, `calendar_windows`, `noise`, `limits`, `state_validator`, `digest` |
| DeliverySlot | `slot_key`, `source_name`, `scenario_index`, `sequence_class`, `pattern_name`, `variant_names`, `catalog_row_index` |
| PlannedEvent | `event_key`, `role`, `position`, `logical_time_us`, `timestamp_us`, `session_id` |
| NoiseSlot | `event_key`, `ordinal`, `frame_class`, `timestamp_us`, `session_id` |
| ReplayLayout | `source_slot_key`, `source_variant_name`, `replay_ordinal`, `session_id`, `timestamps_us` |
| ScenarioPlan | `blocks`, `delivery_slots`, `noise_slots`, `replay_layouts`, `primary_sessions`, `digest` |
| ScenarioSeed | `initial_state`, `actors`, `shared_facts`, `style`, `time_context` |
| ActorView | `actor`, `goal`, `read_state`, `observations`, `logical_time_us`, `wait_since_previous_us` |
| EventPlan | `frame_class`, `actor`, `intent`, `patch` |
| EventExecution | `state_before`, `state_after`, `state_before_hash`, `state_after_hash`, `publish_snapshot`, `normalized_patch` |
| EventDraft | `event_key`, `event_id`, `frame_class`, `actor`, `logical_time_us`, `timestamp_us`, `actor_view`, `intent`, `patch`, `state_before_hash`, `state_after_hash`, `publish_snapshot`, `payload` |
| EventTruth | `event_key`, `event_id`, `role`, `frame_class`, `actor`, `logical_time_us`, `timestamp_us`, `actor_view`, `intent`, `patch`, `state_before_hash`, `state_after_hash`, `publish_snapshot`, `payload` |
| ObservedEvent | `event_id`, `frame_class`, `timestamp_us` |
| SemanticReviewEvent | `frame_class`, `actor`, `logical_time_us`, `wait_since_previous_us`, `actor_view`, `intent`, `patch`, `state_before_hash`, `state_after_hash`, `publish_snapshot`, `payload` |
| PatternEvaluation | `actual_bindings`, `actual_violations` |
| StateEvaluation | `replay_hash`, `final_state_hash`, `bindings_valid`, `outcome_valid`, `protected_prefix_valid` |
| SemanticEvaluation | `causal_consistency`, `actor_knowledge`, `goal_consistency`, `temporal_plausibility`, `cross_frame_consistency`, `realism`, `reason_codes` |
| NoiseSemanticEvaluation | `unrelated_to_declared_tasks`, `no_executable_task`, `realism`, `reason_codes` |
| EventTrace | `scenario_id`, `world_branch_id`, `sequence_class`, `pattern_name`, `variant_name`, `scenario_seed`, `events`, `final_state`, `pattern_evaluation`, `state_evaluation`, `semantic_evaluation` |
| GenerationParseContext | `project_root`, `class_views`, `frame_classes`, `llm_profiles`, `hook_loader`, `collector` |
| ScenarioSeedRequest | `program`, `slot`, `attempt_index`, `random_seed` |
| EventPlanRequest | `mode`, `semantic_profile`, `slot_key`, `planned_event`, `role`, `generation_instruction`, `sequence_length`, `eligible_frame_classes`, `eligible_actors`, `actor_view`, `visible_state`, `history`, `actor_profiles`, `public_facts`, `attempt_index`, `variation_nonce` |
| EventExecutionContext | `program`, `plan`, `slot`, `variant_name`, `event_index`, `scenario_seed`, `current_state`, `history` |
| StateTransitionInput | `slot_key`, `variant`, `role`, `state_before`, `state_after`, `patch` |
| PostValidationResult | `violations`, `event_execution` |
| PostValidatedCallRequest | `profile`, `prompt`, `schema`, `scope`, `post_validator` |
| ValidatedGenerationCall | `object`, `event_execution`, `resolved_at`, `usage`, `attempts`, `model` |
| RenderEventRequest | `semantic_profile`, `slot_key`, `planned_event`, `event_plan`, `actor_view`, `publish_snapshot`, `state_before_hash`, `state_after_hash`, `binding_values`, `frame_spec`, `role`, `public_facts`, `attempt_index` |
| StateEvaluationRequest | `program`, `slot`, `pattern`, `variant`, `scenario_seed`, `events`, `baseline_events`, `final_state` |
| CouplingEvaluationRequest | `variant`, `baseline_events`, `events` |
| SemanticEvaluationRequest | `evaluation_profile`, `mode`, `sequence_class`, `class_description`, `pattern_description`, `scenario_seed`, `review_events`, `final_state`, `attempt_index` |
| NoiseRenderRequest | `semantic_profile`, `noise_slot`, `noise_spec`, `frame_spec`, `class_descriptions`, `frame_descriptions`, `attempt_index` |
| NoiseEvaluationRequest | `evaluation_profile`, `payload`, `class_descriptions`, `frame_descriptions`, `attempt_index` |
| ProjectionRequest | `program`, `slot`, `trace` |
| NoiseProjectionRequest | `program`, `run_id`, `noise_slot`, `payload` |
| ReplayProjectionRequest | `program`, `layout`, `source` |
| ProjectedSequence | `main_record`, `primary_stream_rows` |
| SequenceRows | `main_row`, `primary_stream_rows`, `retained_content_bytes` |
| ReplayRows | `rows`, `retained_content_bytes` |
| ReconcileRequest | `plan`, `sequences`, `noise_rows`, `replay_rows` |
| GenerationServices | `config`, `schema_engine`, `llm`, `metrics` |
| RuntimeCredentials | `llm`, `embedding` |
| ResolvedHook | `reference`, `target` |
| ValidationHooks | `output`, `sample`, `state` |
| ResolvedPaths | `project`, `project_root`, `input`, `output`, `report`, `rejects`, `sidecar`, `trace`, `stream`, `manifest`, `failed_report` |
| DeliveryRequest | `program`, `plan`, `paths`, `run_attempt_id`, `run_id` |
| DeliveryServices | `generation`, `dedup`, `quality`, `annotate`, `verify`, `emitter` |
| AttemptTransaction | `items`, `class_views`, `projected_sequences` |
| DownstreamAttemptRequest | `transaction`, `run_context` |
| DownstreamAttemptResult | `accepted`, `rejected_stage`, `dataset_counters` |
| DedupGroupRequest | `records`, `exempt_pairs`, `embedding_profile` |
| DedupProbeToken | `capability_id`, `index_generation`, `record_digests`, `exact_features`, `minhash_features`, `embedding_features` |
| GenerationProduct | `main_rows`, `stream_rows`, `report` |

ScenarioBlock 是只读 Mapping，键类型固定为 `tuple[str, str | None]`，值为 PlannedEvent tuple；None 语义见 8.4。
所有 Mapping 输入在构造时深拷贝为 JSON-compatible 值再暴露为 MappingProxyType。RuntimeCredentials 与
ResolvedHook.target 的 repr/compare 均不得暴露 callable 或 secret value。

docs/CONTRACTS.md 冻结上述每个字段的完整 Python annotation、`T | None`、tuple/Mapping 容器、default、
default_factory、constructor positional order 与 frozen 属性；这里只列字段顺序不是放宽类型。除公共配置明确声明的
缺省和 CallScope 等既有公共默认外，generation 内部 request/result 的可空字段也必须由调用者显式传 null，不得用
隐式 default 形成第二套构造面。类型测试以手写 literal manifest 为期望，不从生产 dataclass 反向生成期望。
EventExecutionContext.history 固定为 `tuple[EventDraft, ...]`；EventPlanRequest.history 固定为
`tuple[EventDraft, ...] | None`，且只在 instruction-only 非 null。EventTruth 不得作为逐事件生成期 history carrier。

NoiseSlot 只描述独立 noise 事件；ReplayLayout 只描述一次完整 replay 的 source、variant、ordinal、session 与逐事件 timestamp。
两者均不进入 ScenarioBlock。ReplayLayout.timestamps_us 的长度必须等于 source positive sequence 的事件数，且 source
只按 DeliverySlot.slot_key 与 source_variant_name 解析；该 variant 的 kind 必须恰为 positive。projector 不允许按
payload、位置或临时 list index 猜测 source。

SequenceValidationInput、ScenarioValidationInput、GenerateStreamConfig、ScenarioConfig、SequencePlan 和 StreamPlan 不再存在。

## 17. 冻结接口

~~~python
def parse_generation_config(
    raw_project: Mapping[str, object],
    context: GenerationParseContext,
) -> SequenceGenerationConfig:
    ...

def compile_generation_program(config: ResolvedConfig) -> GenerationProgram:
    ...

def compile_scenario_plan(program: GenerationProgram, seed: int) -> ScenarioPlan:
    ...

async def generate_scenario_seed(
    request: ScenarioSeedRequest,
    services: GenerationServices,
) -> ScenarioSeed:
    ...

def build_event_plan_request(
    context: EventExecutionContext,
    attempt_index: int,
    variation_nonce: str,
) -> EventPlanRequest:
    ...

async def plan_event(
    context: EventExecutionContext,
    attempt_index: int,
    variation_nonce: str,
    services: GenerationServices,
) -> tuple[EventPlan, EventExecution]:
    ...

def execute_event(
    context: EventExecutionContext,
    event_plan: EventPlan,
) -> EventExecution:
    ...

def post_validate_event_plan(
    candidate: Mapping[str, object],
    context: EventExecutionContext,
) -> PostValidationResult:
    ...

async def render_event(
    request: RenderEventRequest,
    services: GenerationServices,
) -> Mapping[str, object]:
    ...

def evaluate_pattern(
    pattern: SequencePattern,
    events: Sequence[ObservedEvent],
) -> PatternEvaluation:
    ...

def evaluate_state(request: StateEvaluationRequest) -> StateEvaluation:
    ...

def evaluate_coupling(request: CouplingEvaluationRequest) -> bool:
    ...

async def evaluate_semantics(
    request: SemanticEvaluationRequest,
    services: GenerationServices,
) -> SemanticEvaluation:
    ...

async def render_noise(
    request: NoiseRenderRequest,
    services: GenerationServices,
) -> Mapping[str, object]:
    ...

async def evaluate_noise(
    request: NoiseEvaluationRequest,
    services: GenerationServices,
) -> NoiseSemanticEvaluation:
    ...

def project_trace(request: ProjectionRequest) -> ProjectedSequence:
    ...

def project_noise(request: NoiseProjectionRequest) -> Mapping[str, object]:
    ...

def project_replay(request: ReplayProjectionRequest) -> ReplayRows:
    ...

def reconcile_views(request: ReconcileRequest) -> None:
    ...

async def deliver_generation(
    request: DeliveryRequest,
    services: DeliveryServices,
) -> GenerationProduct:
    ...

class DownstreamAttemptCollaborator(Protocol):
    async def run_attempt(
        self,
        request: DownstreamAttemptRequest,
    ) -> DownstreamAttemptResult:
        ...

class DedupIndex:
    async def group_probe(
        self,
        request: DedupGroupRequest,
        context: RunContext,
    ) -> DedupProbeToken:
        ...

    def group_commit(self, token: DedupProbeToken) -> None:
        ...

class SequenceDeliveryEmitter:
    def assemble_sequence(
        self,
        item: PipelineItem,
        projection: ProjectedSequence,
        batch_no: int,
    ) -> SequenceRows:
        ...

    def prepare_product(
        self,
        main_rows: Sequence[Mapping[str, object]],
        stream_rows: Sequence[Mapping[str, object]],
        report: Mapping[str, object],
    ) -> GenerationProduct:
        ...

    def commit(self, product: GenerationProduct) -> Mapping[str, object]:
        ...

    def write_failed_report(self, report: Mapping[str, object]) -> None:
        ...

def derive_generation_id(
    domain: str,
    components: Sequence[object],
) -> str:
    ...

def canonical_delivery_row(row: Mapping[str, object]) -> bytes:
    ...
~~~

所有接口声明有 doxygen style 中文 docstring；每个函数不超过五个参数，所以复杂调用使用冻结 request dataclass。
generation 包不导出旧函数名或参数转换 wrapper。

SequenceDeliveryEmitter.prepare_product 是 delivery_digest 的唯一 owner：它按第 11 节计算一次摘要，写入 report
的深拷贝并返回 GenerationProduct。GenerationProduct 不再保存第二份 digest 或 manifest_input；commit 从
深度冻结的 product.report.delivery_digest 构造 manifest，若缺失或格式非法则在打开 `.part` 前以
generation_downstream_contract 终止；commit 不另算第二份摘要。

DeliveryServices 只有一个 GenerationServices 根；后者的 config、llm、schema_engine、metrics 也是所有生成调用、
dedup 与下游 stage 的唯一对象。DeliveryController 为每个协作者派生 RunContext 时，这四个字段必须分别与
GenerationServices 对应字段对象身份相同，只新建 rng 与 batch_no。DeliveryRequest 不复制 config 或 materialized
credentials；RuntimeCredentials 只服务于 factory 构造上述 LLMClient，随后不进入 delivery request。

AttemptTransaction.items 是当前 attempt 内唯一 PipelineItem 真值，协作者原地更新这些 item；DownstreamAttemptResult
只返回 accepted、rejected_stage 与该 stage 的 dataset counter delta，不复制 items。DeliveryController 在局部整数表中
累加 delta，直到 group commit 才合并运行级 dataset counters；任一拒绝直接丢弃 transaction 与局部 delta。

QualityStage、AnnotateStage 与 VerifyStage 均实现 run_attempt，frame annotation 由 AnnotateStage
的同一 attempt 入口处理。该入口与普通 Stage.run 共用生产核心函数，但不把异常先降级成 StageError。
ProviderFatalError、CircuitBreakerTripped、KeyboardInterrupt 与 asyncio.CancelledError 必须原样穿透到
DeliveryController，立即按 run-terminal 处理，不消耗 attempt；ProviderRetryableError、SchemaViolation、
ContextOverflowError、OutputTruncatedError 与普通质量/校验拒绝返回 accepted = false 的 DownstreamAttemptResult。
若 attempt 路径上出现新增 ErrorKind.PROVIDER_FATAL 的 item error，说明误用 Stage 隔离入口，归
generation_downstream_contract 内部错误并 exit 4，不当作可重试 slot rejection。

DedupIndex.group_probe 直接接收同一 RunContext，不复用当前会把 fatal 编辑为记录状态的
DedupStage._semantic_level 外壳。它对上述四类 run-terminal 使用相同穿透规则；对 ProviderRetryableError
原样上抛，由 DeliveryController 记为当前 attempt 的 provider_retryable_exhausted 并消耗 attempt。

state_validator 的冻结签名：

~~~python
def validate_state(value: StateTransitionInput) -> list[str]:
    ...
~~~

StateTransitionInput 包含 slot_key、variant、role、state_before、state_after 和 patch 的深拷贝。返回值只接受 string list；
异常和非法返回分别归 state_validator_exception 与 state_validator_invalid，并直接终结当前 attempt。
普通非空 string list 归 state_validator_violation 并进入 L3。旧 sequence_validator 和 scenario_validator 删除。

## 18. 提示与 Schema 所有权

固定 prompt family：

| family | profile | 输出 |
|---|---|---|
| generation.scenario_seed | semantic_llm | ScenarioSeed |
| generation.event_plan | semantic_llm | 一个 EventPlan |
| generation.frame_render | semantic_llm | 一个 frame object |
| generation.semantic_evaluate | evaluation_llm | SemanticEvaluation |
| generation.noise_render | semantic_llm | 一个 noise frame object |
| generation.noise_evaluate | evaluation_llm | NoiseSemanticEvaluation |

模板全文冻结在 docs/CONTRACTS.md；生产代码只在对应 generation 模块定义一次。CONTRACTS 同时冻结：

- system/user message 顺序。
- attempt、slot、role 与 actor view 的插值位置；event plan/frame render/semantic review 不渲染 variant target。
- DeepSeek L0-off 的纯文本 JSON 契约。
- structured output profile 的 JSON Schema 透传。
- FrameRenderer 的完整 Schema、按声明序精确 binding values、机械覆盖与最终完整 Schema 复验。
- deterministic repair 与 L3 repair prompt。
- reason code 闭集和普通日志禁止字段。

schema_engine 不再认识 plan_schema、brief_schema 或 realize_schema 专名，只消费每次调用传入的标准 JSON Schema。

M8 新增仅供需要可执行后置判定的内部调用面，不改变既有 complete_validated 返回类型：

~~~python
CallPostValidator = Callable[[Mapping[str, object]], PostValidationResult]

class SchemaEngine:
    async def complete_post_validated(
        self,
        request: PostValidatedCallRequest,
    ) -> ValidatedGenerationCall:
        ...
~~~

PostValidatedCallRequest 冻结 profile、prompt、schema、CallScope 和 CallPostValidator。M8 对每个 L2 通过的
首轮或 L3 候选恰调用一次后置验证；EventPlan 的 callback 从该 candidate 唯一构造 EventPlan，再与闭包捕获的
EventExecutionContext 执行，context 内不存在另一份 event_plan：

- violations 非空且 EventExecution 为 null 是可修复结果，以 `(post-validator)` 前缀进入同一 L3 违规清单。
- violations 为空且 EventExecution 非 null 是唯一成功结果，该 EventExecution 写入 ValidatedGenerationCall。
- 其他形状、非 string 违规、回调异常和非 PostValidationResult 返回分别归一为
  post_validator_invalid 或 post_validator_exception，不进 L3，不携带用户数据文本。
- L3 修复只使用 violations，不把 EventExecution、state 或 hook 异常写进 prompt。

ScenarioSeed、FrameRenderer、SemanticEvaluator 与 noise 仍调用 complete_validated；只有 EventPlan 调用
complete_post_validated。ValidatedGenerationCall.resolved_at 的闭集固定为 l0_or_clean、l1、l3_1、l3_2，
明确标出该成功对象的解析路径，但不写入只属于用户 Schema annotate 的全局 resolved_at 计数。M8 不保存
跨调用后置验证器，因此并行请求不会串用 state 或 hook。

## 19. 物理文件清单

### 19.1 新增生产文件

~~~text
labelkit/common/config/generation.py
labelkit/common/contracts/generation.py
labelkit/operators/generation/__init__.py
labelkit/operators/generation/flat.py
labelkit/operators/generation/program.py
labelkit/operators/generation/planner.py
labelkit/operators/generation/scenario.py
labelkit/operators/generation/state.py
labelkit/operators/generation/render.py
labelkit/operators/generation/evaluate.py
labelkit/operators/generation/project.py
labelkit/orchestration/generation_delivery.py
~~~

flat.py 接收原 generate.py 中 v1.12 独立样本实现。generate.py 只保留 flat generate 的薄 Stage 入口并调用
generation.flat，不 import orchestration，也不保留旧 stream 分支。sequence 形态由
Orchestrator._run_generate_only 直接调用 generation_delivery.deliver_generation；依赖方向仍为
cli → orchestration → operators → common。

### 19.2 整文件删除

~~~text
labelkit/common/config/_generate_stream_constraints.py
labelkit/common/runtime/scenario/__init__.py
labelkit/common/runtime/scenario/calendar.py
labelkit/common/runtime/scenario/diagnostics.py
labelkit/common/runtime/scenario/model.py
labelkit/common/runtime/scenario/noise.py
labelkit/common/runtime/scenario/planner.py
labelkit/common/runtime/scenario/quota.py
labelkit/common/runtime/scenario/rules.py
labelkit/common/runtime/scenario/sessions.py
labelkit/operators/generate_stream.py
~~~

以下已在 v1.17 删除且必须继续不存在：

~~~text
labelkit/common/runtime/declare.py
labelkit/common/runtime/sequence_planner.py
labelkit/common/runtime/temporal.py
labelkit/orchestration/profile_usage.py
~~~

### 19.3 修改生产文件

~~~text
pyproject.toml
uv.lock
labelkit/cli/main.py
labelkit/cli/console.py
labelkit/common/config/__init__.py
labelkit/common/config/_classviews.py
labelkit/common/config/_constraints.py
labelkit/common/config/_schemas.py
labelkit/common/config/_sections.py
labelkit/common/config/loader.py
labelkit/common/config/model.py
labelkit/common/contracts/types.py
labelkit/common/errors.py
labelkit/common/extensions/hooks.py
labelkit/common/observability/obslog.py
labelkit/common/runtime/budget.py
labelkit/common/runtime/credentials.py
labelkit/common/runtime/schema_engine.py
labelkit/operators/annotate.py
labelkit/operators/dedup.py
labelkit/operators/emitter.py
labelkit/operators/generate.py
labelkit/operators/ingest.py
labelkit/operators/quality.py
labelkit/operators/verify.py
labelkit/orchestration/__init__.py
labelkit/orchestration/factory.py
labelkit/orchestration/orchestrator.py
labelkit/orchestration/runtime.py
~~~

calendar、fixed-offset window、CP-SAT deterministic diagnostics、RuntimeCredentials、ResolvedPaths、
SchemaEngine、LLMClient、SimilarityFilter probe/commit 与 Emitter .part/fsync/rename 的通用算法可以移植或保留；
不得保留 common/runtime/scenario 的旧物理包或旧类型。

pyproject 新增维护中的 jsonpatch 依赖，保留 ortools 精确锁定。实现调用 jsonpatch，不复制 JSON Pointer 或 patch 执行器。

## 20. 测试文件清单

### 20.1 删除

~~~text
tests/common/config/test_loader_generate_stream.py
tests/common/runtime/scenario/test_calendar.py
tests/common/runtime/scenario/test_diagnostics.py
tests/common/runtime/scenario/test_model.py
tests/common/runtime/scenario/test_noise.py
tests/common/runtime/scenario/test_planner.py
tests/common/runtime/scenario/test_quota.py
tests/common/runtime/scenario/test_rules.py
tests/common/runtime/scenario/test_sessions.py
tests/operators/test_generate_stream.py
tests/integration/test_generate_stream_llm.py
tests/cli/goldens/dryrun-synth-stream.txt
~~~

### 20.2 新增

~~~text
tests/common/config/test_generation.py
tests/common/contracts/test_generation.py
tests/operators/generation/test_program.py
tests/operators/generation/test_planner.py
tests/operators/generation/test_scenario.py
tests/operators/generation/test_state.py
tests/operators/generation/test_render.py
tests/operators/generation/test_evaluate.py
tests/operators/generation/test_project.py
tests/orchestration/test_generation_delivery.py
tests/integration/test_sequence_generation_llm.py
tests/integration/test_sequence_generation_structured_output_llm.py
tests/cli/goldens/dryrun-sequence-generation.txt
~~~

### 20.3 接缝测试修改

~~~text
tests/conftest.py
tests/cli/test_cli.py
tests/cli/test_console.py
tests/common/config/test_config.py
tests/common/config/test_paths_hooks.py
tests/common/contracts/test_types.py
tests/common/extensions/test_hooks.py
tests/common/observability/test_obslog.py
tests/common/runtime/test_budget.py
tests/common/runtime/test_credentials.py
tests/common/runtime/test_schema_engine.py
tests/common/test_errors.py
tests/hook_samples.py
tests/operators/test_annotate.py
tests/operators/test_dedup.py
tests/operators/test_emitter.py
tests/operators/test_generate.py
tests/operators/test_ingest.py
tests/operators/test_quality.py
tests/operators/test_verify.py
tests/orchestration/test_orchestrator.py
~~~

tests/cli/test_cli.py 的 production/test manifest 与层间依赖检查必须同步。integration marker 拆成 deepseek 与 zai；
缺少 LABELKIT_ZAI_KEY 不能跳过只需要 LABELKIT_DEEPSEEK_KEY 的用例。

## 21. 教学 example

删除 examples/synth-stream，新增 examples/sequence-generation：

~~~text
examples/sequence-generation/
├── config.toml
├── project.toml
├── project-instruction-only.toml
├── project-replay.toml
├── hooks.py
├── check_output.py
├── catalogs/ticket-booking.jsonl
└── schemas/
    ├── state.json
    ├── pre-request.json
    ├── pre-acknowledgement.json
    ├── pre-confirmation.json
    ├── outcome-positive.json
    ├── outcome-missing.json
    ├── outcome-reordered.json
    ├── outcome-timeout.json
    ├── frame-request.json
    ├── frame-acknowledgement.json
    ├── frame-confirmation.json
    ├── frame-noise.json
    └── annotation.json
~~~

主例只用 ticket_booking、booking_success 和 request、acknowledge、confirm 三个 role。
catalog 两行完整 ScenarioSeed，四个 variant，精确两 set、八 primary sequence。

示例冻结计数：

| 对象 | 数量 |
|---|---:|
| counterfactual sets | 2 |
| positive / missing / reordered / timeout | 各 2 |
| primary sequences | 8 |
| primary events | 22 |
| noise events | 2 |
| replay sequences | 1 |
| replay events | 3 |
| stream rows | 27 |
| primary sessions | 7 |
| crossed primary sessions | 1 |
| replay tail sessions | 1 |

每个 set 的事件数为 3 + 2 + 3 + 3 = 11。replay 固定复制 declaration order 中首个 positive sequence。
同一个 counterfactual set 的 variant 不共 session；唯一 crossed session 使用两个不同 set 的 owner。

state 至少包含 actors、goal、request、ticket、audit、sla 和 hidden_sentinel。hidden_sentinel 不在任何 role.read_roots、
publish_roots、renderer input 或 payload 中。payload_binding 机械注入 request_id、ticket_id 与 status。

project-instruction-only.toml 交付一条 sequence，验证独立 truth、状态重放、语义判定与精确交付。

config.toml 固定：

~~~toml
[llm.default]
provider = "anthropic"
base_url = "https://api.deepseek.com/anthropic"
model = "deepseek-v4-flash"
api_key_env = "LABELKIT_DEEPSEEK_KEY"
supports_structured_output = false
supports_vision = false
max_output_tokens = 8192
thinking = "disabled"
context_window = 131072
temperature = 0.0

[llm.judge]
provider = "anthropic"
base_url = "https://api.deepseek.com/anthropic"
model = "deepseek-v4-flash"
api_key_env = "LABELKIT_DEEPSEEK_KEY"
supports_structured_output = false
supports_vision = false
max_output_tokens = 8192
thinking = "disabled"
context_window = 131072
temperature = 0.0
~~~

凭据 value 只通过 v1.17 RuntimeCredentials 在 run 和 validate --probe 物化；不得写入 project/config、日志、trace、
report、manifest、命令参数或测试失败消息。

project-replay.toml 读取 27 行 stream，process replay 的冻结目标：

| report 计数 | 值 |
|---|---:|
| scanned | 27 |
| absorbed | 25 |
| dropped_noise | 2 |
| episodes | 9 |
| dropped_dup | 1 |
| emitted | 8 |
| failed | 0 |

check_output.py 只读工件并断言全部精确恒等式、patch 重放、pattern violations、main/stream 双向对账、
hidden_sentinel 不泄漏、replay 同源和 report/manifest digest。

## 22. 离线验收

### 22.1 配置与删除门

- 所有新字段、默认、引用、互斥、Schema、catalog、profile 与上限各有正反用例。
- examples/sequence-generation/project.toml 必须经真实 M1 parser/compiler 正向通过，不用手工构造 ResolvedConfig 代替。
- 缺任一相邻 max gap、缺 max_span、非法 Pointer、权限重叠、catalog 不足均启动失败。
- generate.stream、tiers、tier_rank、subsequence、filler、time_fields、old validator、brief 和 realize 键均拒绝。
- 搜索证明旧生产文件、旧类型、旧 prompt、旧 report key 和旧 artifact truth 不存在。
- tests/common/contracts/test_generation.py 用 dataclasses.fields、typing.get_type_hints、default/default_factory 与
  frozen 检查逐个对照手写 carrier manifest；再用 inspect.signature、inspect.iscoroutinefunction 与 get_type_hints
  对照第 17 节每个函数与方法（包括 Protocol 和 concrete class methods），以及第 18 节
  SchemaEngine.complete_post_validated 的参数 kind、顺序、async 与返回类型，任何字段或接口漂移必须失败。

### 22.2 planner 与 pattern

- 小词表穷举 positive、missing、reordered、interval_exceeded，actual violation 分别恰为空或一个目标。
- CP-SAT 与独立枚举 oracle 对齐 role、gap、span、session、crossing、noise、replay 和 exact count。
- 同 seed 相同 plan；修改 attempt failure 不改变其他 slot 的 plan。
- 同 set variant 不共 session、不交织。
- `calendar_windows` 从真实 M1 配置进入 GenerationProgram，并由 validate、dry-run、run 的同一个
  compile_scenario_plan 入口约束到 role timestamp；三条路径的 plan digest、slot 与窗口 witness 完全相同。
- catalog 两行按声明序产生稳定的 catalog_row_index；同一 slot 连续失败重试时索引不变，也不读取下一行。
- instruction-only 的 PlannedEvent 不含 frame_class/actor；EventPlanRequest.actor_view 为 null，EventPlan 才在闭集内
  产生 frame_class/actor，随后 renderer ActorView 与 EventTruth 只使用这一个选择结果。
- declared 逐事件生成的 history 只能包含 EventDraft，且 dataclass 中不存在 role 字段；PatternEvaluator 通过前构造
  EventTruth 必须失败。actual_bindings 完整覆盖 event_id 后才逐项生成 EventTruth，缺失、重复或额外 binding 都 fail closed。
- NoiseSlot 与 ReplayLayout 各自做 canonical JSON round-trip，ID、session、source slot 与 replay timestamps 不借用
  PlannedEvent sentinel，且 replay timestamp 数量与 source 事件数不等时 fail closed。

### 22.3 state 与 actor knowledge

- RFC 6902 test、add、remove、replace 成功；move/copy、非法顺序和越权 path 失败。
- patch 后段失败不修改 current state。
- pre-state、base state、outcome Schema 与 hook 每个失败面都有用例。
- 未 observer 的 published fact 不在 ActorView；publish 后精确出现。
- declared hidden state 不进入 EventPlanRequest、RenderEventRequest 或两者 prompt；它只存在于不可渲染的
  EventExecutionContext.current_state 与独立 evaluator 输入。执行所需 planned event、RoleSpec、state Schema、
  pre-state Schema 与 hook 分别从 context.plan、event_index、program 和 slot 解析，不在 context 重复保存。
- protected prefix 的 ActorView、intent、patch 或 payload 任一字节变化均被 CouplingEvaluator 拒绝。
- declared EventPlanRequest 明确携带 profile、RoleSpec、frame view、actor 闭集、ActorView 与 public facts，但不含
  state Schema、hook 或完整 state；这些只可从独立 EventExecutionContext 的 program/plan/slot/current_state 解析。
  instruction-only request 明确携带
  generation_instruction、sequence_length、完整 visible_state/history、actor_profiles 和有序 frame instruction 映射。
  prompt snapshot 证明 variant、expected、target 不可见。
- build_event_plan_request 对 declared 与 instruction-only 的每个字段做 exact projection 测试；构造 slot 不属于 plan、
  block key 不存在或 event_index 越界的 context，必须在零 LLM call、零 attempt consumption 下以
  generation_downstream_contract 终止，证明 prompt 与后置执行不存在两份事件真值。
- complete_post_validated 返回的 EventExecution 与正式提交消费的是同一冻结实例；patch、Schema 与 state validator
  对成功候选各执行恰一次，不允许丢弃 proof 后重放。
- SemanticEvaluationRequest 只含 blind-review 字段，不可构造或引用 EventTrace，也不含 variant、target、expected、actual、
  PatternEvaluation 或 StateEvaluation；先得到 verdict，随后才能组装 EventTrace。
- frame Schema 使用包含本地 `$ref`、`allOf`、`if/then`、`dependentSchemas` 与 `unevaluatedProperties` 的组合用例，
  证明系统原样传完整 Schema、不改写 Schema，并按声明序以精确 state value 覆盖实例后用完整 Schema 复验。
- binding 父路径缺失、重复/祖先冲突、机械覆盖失败或最终完整 Schema 失败分别 fail closed；权威 state value 不进入 L3，
  frame render 的下一个 slot attempt 才能重新请求。

### 22.4 delivery 与输出

- 每个失败阶段都触发 whole-set retry；成功后 planned = delivered。
- first attempt 通过生成但在 downstream 被固定拒绝，attempt 0 数据不进入任何正式索引或输出。
- group dedup probe 不写 exact、MinHash 或 embedding index；group commit 原子写全部。
- 耗尽保留此前成功 manifest 和固定输出，failed report 独立且 artifacts_committed = false。
- provider fatal 不消耗 attempt，exit 4。
- quality、annotate、frame annotate、verify 和 semantic dedup 的 attempt 入口分别注入 ProviderFatalError，
  均必须零 slot retry、零正式 commit 并 exit 4；ProviderRetryableError 则恰消耗一次 attempt。
- projector 篡改 role、owner、event_id 或成员集合时 CrossViewReconciler 失败。
- 最大允许完整轨迹通过，超一 byte/event/role/variant fail closed；不存在裁剪后 pass。
- retained_content_bytes 恰为 512 MiB 时接受，超一 UTF-8 byte 时整个 slot 失败且零 dedup/dataset commit。
- 单独构造“source primary 未超限，加上它的 replay rows 恰超一 byte”的用例，必须在 source
  group_commit 前命中 sequence_memory_budget，零 dedup、dataset 与 replay commit。
- 从 ProjectedSequence 经过真实 attempt collaborators 到 SequenceRows，最终 main_row 必须包含 inherited
  classification、quality score、sequence/frame annotation 与 verification；CrossView、retained bytes、delivery digest
  和正式 main 文件使用同一 SequenceRows 字节，不得回退读取 pre-downstream Record。
- 失败 attempt 的 SchemaEngine resolved-at、LLM usage、provider retry 与 trace 统计继续累积；同一用例中的 dataset counters、
  item status、annotation、dedup token 和 projected rows 全部回滚，成功重试后只合并最终 attempt 的 dataset counters。
- NoiseRenderRequest 使用 semantic profile 与 NoiseSlot，NoiseEvaluationRequest 使用 evaluation profile；两条真实接口都
  不接受 PlannedEvent、ScenarioSeed 或 EventTrace，缺任一 profile 时在编译期失败。

### 22.5 覆盖率

spec 功能用例覆盖 100%，所有新增/修改生产函数函数覆盖 100%，生产行覆盖至少 85%，分支覆盖至少 75%。
覆盖率必须用 pytest-cov 分别保留 offline 与 integration data 后合并；pytest 通过不等于函数覆盖已达标。

## 23. 真实 LLM 验收

所有 LLM 相关集成使用真实 endpoint，不替换 LLMClient.complete、HTTP transport 或服务端。

### 23.1 DeepSeek 核心

使用一个 catalog slot 真实交付四 variant，必须断言：

- envelopes 恰为 4，variant 集恰等于声明。
- scenario_id 与稳定实体相同，world_branch_id 各不相同。
- baseline protected prefix 与各反例按 causal closure 精确耦合。
- 所有 patch 可从 initial_state 重放；每步与 final outcome Schema 成立。
- actual violations 与 expected 精确相等。
- hidden_sentinel 不在 planner request、renderer request 和 payload。
- report 为 1/1 set、4/4 sequence、每个 variant 1/1。
- report.llm_usage 各 profile 的 calls 合计、prompt_tokens 合计与 completion_tokens 合计大于零。
- 实际请求的 profile 是 deepseek-v4-flash，supports_structured_output = false，且 production Anthropic body
  精确包含 `"thinking": {"type": "disabled"}`，不包含 tools 或 tool_choice。

instruction-only 使用真实 DeepSeek 另跑一个非空 slot，必须断言：

- 恰有一条 main sequence，事件数在冻结 len_range 内，stream 与 main 双向对账。
- 每个 frame_class 落在声明闭集，每个 patch 可重放，SemanticEvaluation 六项全 true。
- truth 只有 validation_mode = instruction_only 与 actor_knowledge_validation = semantic，不存在 pattern、variant、
  expected_violation 或任何 declared 结构通过声明。
- scenario_seed_calls、event plan、frame render、semantic evaluation 与 token usage 全部大于零。

### 23.2 真实 failure injection

不增加测试配置键。测试在 production CrossViewReconciler 边界装饰一次性 rejection wrapper；注入点位于 attempt 0
的 generation、全部 evaluator 与全部启用下游完成之后、group_commit 之前：

- attempt 0 的完整四 variant 走真实 DeepSeek、真实 SchemaEngine 和原 evaluator 后，wrapper 返回固定拒绝。
- attempt 1 完全委托原 evaluator，再次执行完整真实生成。
- 观测到的 attempt index 恰为 `{0, 1}`，两次的 variant 集都恰为声明的四项。
- sequence_slot_attempts = 2，injected rejection = 1，最终仍交付 1 set、4 sequence。
- attempt 0 不进入正式 output、dedup index 或 dataset counters。
- report.llm_usage 各 profile 的 calls 合计大于等于 `2 * estimate.successful_attempt_lower_bound`。

另一个真实用例在 EventPlan 的 production state 后置验证边界装饰一次性 violation，触发 M8 L3 repair 后放行；
精确断言 wrapper 只注入一次、目标调用的 ValidatedGenerationCall.resolved_at 匹配 `l3_[12]`，且最终提交的 EventExecution
与最后一次成功后置验证返回的冻结对象身份相同。不与另一场无注入 live run 比较，避免模型非确定性污染证据。
该用例证明 DeepSeek supports_structured_output = false 路径的
文本 JSON、确定性解析和真实修复环，不用模型偶发违约充当注入。

两个 wrapper 都只装饰已有 production collaborator，不替换任何网络组件，不增加状态化用户 hook，不产生生产 fallback。

### 23.3 structured output

保留一个 z.ai glm-5.2 真实用例，验证组合后的 ScenarioSeed、EventPlan、frame 和 SemanticEvaluation Schema
可透传供应商 structured output。四个对象都必须非空、通过各自完整 Schema，且 LLMResponse.structured
非 null，不允许用文本 JSON 代替 L0 证据。production Anthropic body 必须同时满足：

- tools 恰一项，name 等于冻结 structured tool name，input_schema 与当次组合 Schema 深等。
- tool_choice 强制该 tool，thinking 等于 profile 声明值的精确 body 形状。
- 真实 response 的 usage.prompt_tokens 与 usage.completion_tokens 大于零。

DeepSeek marker 与 z.ai marker 独立；缺一个 endpoint 的 key 只跳过对应 marker。请求 body 验证调用生产序列化器，
真实调用仍经生产 LLMClient 与真实 httpx transport；不用伪 transport、录制响应或本地服务器充当端点证据。

所有 DeepSeek/z.ai 用例把对应 API key value 仅作内存 sentinel，对捕获的 stderr、trace、main、stream、
report、manifest、failed report 与 pytest failure message 做布尔泄漏检查。断言失败只输出固定英文消息，
不把 sentinel 放入 assertion repr。

### 23.4 example 与 replay 命令

~~~bash
cd examples/sequence-generation
mkdir -p out
uv run labelkit validate --config config.toml --project project.toml --console plain
uv run labelkit run --config config.toml --project project.toml --dry-run --console plain
set -a
source ../../.env
set +a
uv run labelkit validate --config config.toml --project project.toml --probe --console plain
uv run labelkit run --config config.toml --project project.toml --console plain
uv run python check_output.py
uv run labelkit run --config config.toml --project project-replay.toml --console plain
uv run python check_output.py --replay
uv run labelkit run --config config.toml --project project-instruction-only.toml --console plain
uv run python check_output.py --instruction-only
cd ../..
uv run --python 3.12 pytest -q -m 'not integration'
uv run --python 3.12 pytest tests/integration/test_sequence_generation_llm.py -q \
  -m 'integration and deepseek'
uv run --python 3.12 pytest tests/integration/test_sequence_generation_structured_output_llm.py -q \
  -m 'integration and zai'
~~~

最终代码或配置变化后，完整 offline、DeepSeek integration、structured output、主例、instruction-only 和 replay
必须全部重跑。429、5xx 和额度耗尽记录为环境失败；slot exhaustion 是产品失败，禁止重跑到绿后忽略。

## 24. 独立真实感门

发布验收抽取 min(50, delivered_sequences) 条；少于 50 时全量。两名独立评审隐藏 variant truth，分别判断：

- 是否存在无法由前序状态解释的跃迁。
- actor 是否提前知道未读取或未发布事实。
- 是否出现明显模板拼接、机械复述或不自然业务动作。
- 等待时长与内容反应是否匹配。
- 同 set 是否在目标点前保持同一场景与表达。

同一缺陷维度出现在两个以上独立 scenario，或出现任一隐藏知识泄漏、状态不可能跃迁，定义为系统性缺陷并阻断。
非系统性明显不真实比例必须不高于 10%。评审分歧由第三名评审裁决。

评审账本每行固定记录 artifact SHA-256、selection seed、sequence_id、scenario_id、评审者假名、
五个缺陷维度 verdict、简短 value-free defect codes、总 verdict、adjudicator 假名与 adjudication verdict。
首轮评审不带 variant/expected_violation；完成独立 verdict 后才解盲。解盲后，与 expected_violation 恰好一致的缺帧、错序
或超时不计作额外不真实；由目标违规引发的无法解释世界跃迁、提前知情或明显话术拼接仍是缺陷。
账本不提交完整 payload、用户数据或 secret。
本次发布证据写入 docs/dev/evidence/v1.18-sequence-realism-review.jsonl，顶部先写一行 run metadata，
包含工件摘要、selection seed、样本数、评审人数与解盲时间；后续每行是上述单条评审记录。

自动化实现完成不等待未来代码；真实感门是本次发布证据的一部分。开发主例只有八条时全量审查；最终发布门使用至少
十三个 set 形成不少于五十二条序列。

## 25. 文档同步

新增本 SPEC；原 PROPOSAL-sequence-generation-redesign 标为 superseded 并指向本文件。
以下旧开发 SPEC 删除，旧 PROPOSAL 只保留历史并标 superseded：

~~~text
docs/dev/SPEC-stream-generation.md
docs/dev/SPEC-generation-tiers.md
docs/dev/SPEC-per-class-tiers.md
docs/dev/SPEC-sequence-rules.md
docs/dev/SPEC-scenario-planning.md
~~~

权威 spec 与契约必须在生产实现前同步：

~~~text
docs/CONTRACTS.md
spec/00-frontmatter.md
spec/10-ch1-overview.md
spec/20-ch2-overall-design.md
spec/30-ch3-modules-intro.md
spec/301-m1-config.md
spec/302-m2-ingest.md
spec/303-m3-dedup.md
spec/304-m4-qualityqurating.md
spec/305-m5-annotate.md
spec/306-m6-generate.md
spec/307-m7-verify.md
spec/308-m8-schema-engine.md
spec/309-m9-llm-client.md
spec/310-m10-orchestrator.md
spec/311-m11-emitter.md
spec/312-m12-logging.md
spec/313-m13-classify.md
spec/314-m14-segment.md
spec/315-m15-extract.md
spec/316-m16-stitch.md
spec/40-ch4-data-structures.md
spec/50-ch5-config-spec.md
spec/60-ch6-io-formats.md
spec/70-ch7-logging.md
spec/80-ch8-nongoals-roadmap.md
spec/85-ch9-references.md
spec/90-appendix-a-rubrics.md
~~~

实现与真实运行后同步 README、AGENTS.md、CLAUDE.md、E2E-FINDINGS、manual README、04、07、08、12、14、
15、16、18、22、24、25、appendix cheatsheet；删除 manual/27-synth-stream.md，新增
manual/27-sequence-generation.md；重建 HTML 与 PDF。

AGENTS.md 与 CLAUDE.md 必须逐字节一致。

## 26. 实施波次

~~~mermaid
flowchart LR
    S["本 SPEC + CONTRACTS + 主 spec"] --> C["config/contracts"]
    C --> P["declared positive + planner"]
    P --> W["逐事件 state/render/evaluate"]
    W --> D["set delivery + downstream transaction"]
    D --> N["missing/reordered/timeout"]
    N --> T["timeline/noise/replay"]
    T --> I["instruction-only"]
    I --> L["DeepSeek + example + replay"]
    L --> A["coverage + bidirectional audit"]
~~~

每个波次都沿最终 GenerationProgram → ScenarioPlan → EventTrace → Evaluation → Delivery → Projection 路径，
不建立临时 generator、兼容层、migration 或 fallback。任何 SPEC 描述能力都必须在相应测试中闭合，不留 TODO。

## 27. 需求账本

| 用户目标 | 权威实现面 | 验收 |
|---|---|---|
| 显式帧组 | exact SequencePattern.roles | role cardinality oracle |
| 显式帧顺序 | order 完整排列 | independent PatternEvaluator |
| 发生间隔上限 | 每个相邻 pair 必须有 max_gap | gap oracle 与 timestamp 对账 |
| 缺帧反例 | missing variant | 唯一 missing_role |
| 错序反例 | reordered variant | 唯一 reordered |
| 超时反例 | interval_exceeded variant | 唯一 gap_above_max |
| 世界连续性 | ScenarioSeed、逐事件 ActorView、JSON Patch | replay、Schema、hook、knowledge 隔离 |
| 反事实可比较性 | protected prefix + causal suffix | CouplingEvaluator |
| 人看不假 | SemanticEvaluator + 独立真实感门 | 六维全 true + 盲审 |
| 精确条数 | whole-set DeliveryController | planned = delivered 或整个运行失败 |
| instruction 自主判定 | instruction-only 独立 truth | 无 pattern claim、语义级 knowledge |
| 教学可复演 | sequence-generation example | 8 main、27 stream、replay dropped_dup = 1 |
| 无旧兼容 | 删除清单与未知键拒绝 | code search + config negatives |

## 28. 完成定义

只有以下事实同时成立才完成：

- 本 SPEC、docs/CONTRACTS.md、主 spec 与生产代码双向一致。
- 旧序列生成文件、符号、字段、prompt、report、truth、测试和 example 均归零。
- 所有新 production file 满足中文注释、英文日志/错误、行长、函数、参数、文件长度与 doxygen 规则。
- spec 用例、函数、行和分支覆盖率达到仓库门。
- DeepSeek 四变体、failure injection、instruction-only、主例和 replay 非空通过。
- structured output 真实透传通过，或有明确外部 endpoint 环境失败证据而不伪称通过。
- main、stream、report 与 manifest 摘要一致；commit 边界前的 failed run 不替换成功工件，
  commit-I/O 失败则按 §2.7 保留旧 manifest 并由摘要检出固定路径混代。
- 两轮代码到规格与规格到代码审计没有 orphan code、orphan requirement 或 deferred item。
- 最终修改后重新执行完整门，无 post-gate 代码或配置变化。

## 29. 一手依据

- RFC 6902 JSON Patch：https://datatracker.ietf.org/doc/html/rfc6902
- python-json-patch 官方仓库：https://github.com/stefankoegl/python-json-patch
- OR-Tools CP-SAT 官方文档：https://developers.google.com/optimization/cp/cp_solver
- Synthea 官方仓库：https://github.com/synthetichealth/synthea
- WebArena 官方项目：https://webarena.dev/
- tau-bench 论文：https://arxiv.org/abs/2406.12045
- Generative Agents 论文：https://arxiv.org/abs/2304.03442

本规格采用这些方案共同的成熟边界：有限约束交给 solver，世界状态必须可执行，状态变化使用标准 patch，
最终结果程序化验收，语言模型负责场景和自然语言但不拥有结构真值。

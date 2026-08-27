# 第 27 章　序列生成：共享世界、交织、反事实与可重放交付

> 本章以 `examples/sequence-generation` 为唯一教学工程。它不依赖任何被删除的旧序列生成配置。

v1.21 只在 v1.20 时间完整性之上增加 declared positive branch 的交织规划；generation stream envelope、ID 公式和
delivery digest 仍使用 `labelkit:v1.20` 工件编码域。产品修订号与工件编码域不是同一件事。

## 27.1 你在生成什么

普通生成把每条文本当独立样本。序列生成把“一个世界里发生的一组事件”作为基本对象：每个事件有 actor、role、
frame class、状态变更、payload 和时间；一组反事实变体共享 ScenarioSeed，因而可以比较“只改变一个因果条件”后的结果。

```mermaid
flowchart LR
    A[GenerationProgram] --> B[ScenarioPlan]
    X[短候选集标签 + 命名交织 pattern] --> B
    B --> C[delivery slot]
    C --> D[共享 ScenarioSeed]
    D --> E[baseline 逐事件执行]
    E --> F[positive / missing / reordered / timeout]
    F --> G[独立 pattern/state/semantic 判定]
    G --> H[真实下游处理]
    H --> I[SequenceRows]
    I --> J[replay rows]
    J --> K[main + stream + report]
    K --> L[manifest-last commit]
```

配置、dry-run 与正式 run 都调用同一个 compiler/planner。计划在读取 API key value、打开输出和消耗 attempt 前冻结；
同一个 seed 与配置得到同一个 program digest、plan digest、slot、交织机会、pair、session、noise 与 replay 布局。

## 27.2 教学工程的最小形状

`project.toml` 只声明一个 sequence class：`ticket_booking`。命名 pattern `booking_success` 有三个 role：

| role | actor | frame class | 业务含义 |
|---|---|---|---|
| `request` | requester | `task_request` | 提交尚未受理的请求 |
| `acknowledge` | system | `acknowledgement` | 确认受理但不提前出票 |
| `confirm` | system | `confirmation` | 根据状态给出最终结果 |

相邻 role 都有闭区间 gap，整个 pattern 有 `max_span_s`。每个 role 还声明 RFC 6901 读写/发布 roots、
状态指令、可选 pre-state Schema 和从权威状态机械覆盖 payload 的 bindings。v1.20 的 frame class 还声明固定
`duration_s`、容量为一的 `resources` 与机械 `time_bindings`；完整 Schema 用
`x-labelkit-business-time = true` 标出每个业务时间叶子。

主例有两个 counterfactual sets，每个 set 共享一行 catalog ScenarioSeed，并产生四个变体：

- `positive`：完整 request → acknowledge → confirm；
- `missing_acknowledgement`：只删除 acknowledge；
- `confirmation_before_acknowledgement`：交换相邻的 acknowledge 与 confirm；
- `confirmation_timeout`：把确认事件移到声明 gap 上限之外。

已验证的 keyless 精确计划是：2 sets、8 primary sequences、22 primary events、2 noise events、
3 replay events，因此 stream 共 `22 + 2 + 3 = 27` 行。教学主例没有声明交织配置，故
`interleaving_opportunities = 0`、`interleaved_primary_sessions = 0`、`primary_sessions = 8`、
`by_interleaving_pattern = {}`；这些 session 数由 plan 派生，不是用户配置。启用交织不会改变上述数据量。

## 27.3 declared 配置骨架

```toml
[generate]
enabled = true
form = "sequence"
mode = "declared"
semantic_llm = "default"
evaluation_llm = "judge"
max_slot_attempts = 8
state_validator = "hooks.py:validate_state"

[class.ticket_booking]
description = "一次订票请求与处理结果"

[class.ticket_booking.generate]
instruction = "保持路线、日期、乘客、请求和票号前后一致。"
state_schema_path = "schemas/state.json"
initial_state_source = "catalog"
initial_state_catalog_path = "catalogs/ticket-booking.jsonl"

[generate.pattern.booking_success]
sequence_class = "ticket_booking"
description = "请求、受理与最终结果"
order = ["request", "acknowledge", "confirm"]
max_span_s = 2400

[[generate.counterfactual_sets]]
name = "booking_success_training"
pattern = "booking_success"
count = 2

[[generate.counterfactual_sets.variants]]
name = "positive"
kind = "positive"
outcome_schema_path = "schemas/outcome-positive.json"
```

交织相关字段单独组成一个闭包。教学主例刻意关闭交织；下面是独立的配置形状示例，不是 `project.toml` 的摘录，
它需要与各自已有的 sequence pattern、class 和 Schema 声明一起使用：

```toml
[[generate.counterfactual_sets]]
name = "food_order"
pattern = "food_order"
count = 1
interleaving_candidate_set = "food_dinner"

[[generate.counterfactual_sets.variants]]
name = "completed"
kind = "positive"
outcome_schema_path = "schemas/food-order-completed.json"

[[generate.counterfactual_sets]]
name = "entertainment_habit"
pattern = "entertainment_habit"
count = 8
interleaving_candidate_set = "entertainment"

[[generate.counterfactual_sets.variants]]
name = "completed"
kind = "positive"
outcome_schema_path = "schemas/entertainment-completed.json"

[generate.interleaving]
no_interleaving_weight = 9

[generate.interleaving.pattern.food_with_entertainment]
trigger_candidate_set = "food_dinner"
partner_candidate_set = "entertainment"
trigger_weight = 1
```

`food_dinner` 与 `entertainment` 是短业务标签，不是序列名称编码。标签和 pattern name 都只接受
`[a-z0-9_]+` 并按完整字符串精确匹配；不支持 glob、regex、前缀、列表或 selector 表达式。交织开启时至少有一个
命名 pattern；候选标签必须全部被引用，pattern 两侧都必须有成员。权重必须满足 TOML int64 边界，且一次机会的
总权重不得超过 `2^63 - 1`。

完整 role、gap、variant、timeline、calendar window 与 noise 配置直接阅读示例文件；删掉任何关键字段后运行
`labelkit validate`，会在内容调用前得到聚合配置错误。

frame business time path 必须与 `time_bindings[*].payload_path` 完全相等。M1 从完整 Schema 派生不含这些叶子的
model Schema；模型首轮和每轮 L3 只生成非时间字段，generic candidate finalizer 才按 planned start/end/duration
注入并复验完整 Schema。sequence annotation 使用同一标记机制，当前 source 为目标 resource 的最早正区间起点。

每个会让 LLM 产出 object 的 state、outcome 与 frame Schema 都在根级给出非空 `examples`。这不是 few-shot：
M1 会用完整 Schema 验证它们，再选择 canonical UTF-8 最小的有效 object 作为“该 Schema 至少可实现”的预算 witness。
显式 Schema 没有有效根 example，或最小 example 超过固定 prompt/payload 上限，都会在读取 API key 前失败。

## 27.4 ScenarioSeed、actor knowledge 与状态执行

catalog 每行是完整 ScenarioSeed，而不是 payload 模板：

```json
{
  "initial_state": {},
  "actors": {"requester": {}, "system": {}},
  "shared_facts": {"public": {}, "hidden": {}},
  "style": {},
  "time_context": {}
}
```

同一个 slot 重试时继续使用同一 catalog row，不能通过换世界掩盖失败。EventPlanner 只看到当前 actor 的 ActorView
与 public facts；hidden facts 只进入不可渲染的独立判定。每个 patch 先在不可变候选上完整验证，再原子替换当前状态。

payload binding 把权威 state value 覆盖到模型返回对象，再复验同一完整 frame Schema。模型不能伪造 request_id、
ticket_id 或 status 来绕过状态真值。最终 EventTrace 同时保留执行证据与盲审结果，但 state、patch 和 ActorView
不写进训练工件。

declared 的最后事件在 M8/StateExecutor 边界先用当前 branch 的 outcome Schema 做 production post-validation：
hidden baseline 机械选择 positive outcome，交付 branch 选择当前 variant outcome。送入 L3 的违规只含
value-free outcome-schema pointer/keyword，不含实际或期望状态值。修复成功后，StateEvaluator 仍从初始状态独立
重放并复验 outcome，不能复用 post-validator 的结论冒充独立证据。

## 27.5 反事实为何可比较

baseline 先逐事件生成 EventDraft。只有 baseline 全部成功后，系统才执行结构变换并复用 protected prefix。
CouplingEvaluator 会拒绝本应复用位置上的 ActorView、intent、patch 或非时间 payload 字节变化；protected prefix 的
业务时间仍按当前 branch 的绝对起点与时长重新绑定。

PatternEvaluator 机械验证 role、顺序和 gap；StateEvaluator 验证最终 outcome 与状态；SemanticEvaluator 在看不到
variant、target、expected/actual violation 的条件下独立判断自然性、因果一致性、actor knowledge 和隐藏信息泄漏。
只有机械与语义证据同时通过，系统才组装 EventTrace。

反例的结构异常不等于世界可以自相矛盾。教学工程的 reordered 分支会先给出“未获得受理因而未出票”的
终态，后到 acknowledgement 只表示补充收件，并保留该终态；不能再声称请求进入处理。FrameRenderer 必须把
内部状态翻译成自然业务表达；SemanticEvaluator 则先寻找“终态后重新激活”、内部术语和机械复述这些反例，
不用未提供的隐藏理由替候选补故事。补充确认需说明结果不变时，只引用“先前通知”，不再用新的近义短语
重复描述能否出票。话术必须把“系统收到用户请求”与“系统发出补充确认”分清，不能把系统正在发出的确认写成
它收到的对象。

EventProjector 还会在构造训练 Record 前复验这些 gate：state hash 和三项机械结论、六项 semantic 结论、
instruction-only 不携带 pattern 结论，以及 declared 的精确 role word、frame/actor、event ID、binding 与唯一违规。
因此同步伪造一组“内部看起来一致”的 verdict 也不能进入输出。

## 27.6 instruction-only 是另一种模式

`project-instruction-only.toml` 不声明 pattern 或 counterfactual set：

```toml
[generate]
enabled = true
form = "sequence"
mode = "instruction_only"
semantic_llm = "default"
evaluation_llm = "judge"

[[generate.instruction_only]]
name = "open_booking"
sequence_class = "ticket_booking"
count = 1
len_range = [3, 4]
instruction = "生成一次完整、自然、状态连续的订票交互。"
state_schema_path = "schemas/state.json"
```

它每个 attempt 都生成 ScenarioSeed，再由 EventPlan 在已声明 frame class 与 actor 闭集中做选择。
instruction-only 的 EventPlanRequest 显式携带完整 state Schema，使真实 post-validator/L3 能看到合法枚举；
declared request 的同字段固定为 null，权威 Schema 从冻结 program 解析。输出 truth 只有
`validation_mode = "instruction_only"`、slot、scenario、world branch 与 sequence class；不能伪装出 pattern、
variant 或 expected violation。instruction-only 禁止 `interleaving_candidate_set` 和 `[generate.interleaving]`，
report 中交织机会、交织 session 固定为零，pattern map 为空。

### 27.6.1 只做帧标注

`project-frame-only.toml` 用一条三事件 instruction-only sequence 演示独立帧标注。它没有 `[segment]`，明确关闭
sequence annotate 和 frame classify，开启 pointwise quality 与 frame annotate：

```toml
[quality]
enabled = true
mode = "pointwise"
threshold = 0.0
rubric = "default:trajectory"

[annotate]
enabled = false

[frame.classify]
enabled = false

[frame.annotate]
enabled = true
llm = "default"
instruction = "只根据当前成员帧提取请求编号、可见状态与简短摘要。"
schema_path = "schemas/frame-annotation.json"
```

当前总阶段约束仍要求 quality 与 sequence annotate 至少开启一个，因此 frame-only 不能同时关闭 quality。M5 直接
消费生成计划中的成员和 inherited frame class，不发 sequence annotation 调用。任一应标注帧失败会让整个 slot
attempt 失败并重试；只有全部帧标注成功后，main 与 primary stream 才作为同一组提交。

## 27.7 交织、时间线、noise 与 replay

`[generate.timeline]` 只声明 timestamp start、默认 event gap、session 容量与间隔、noise 和 replay 数量。
`primary_sessions` 与 `crossed_primary_sessions` 已删除；用户不再倒推并手填 session 数。declared role 的间隔只来自
sequence pattern gaps；默认 `event_gap_s` 不会暗中收窄 timeout 变体。

交织的心智模型只有三步：在 counterfactual set 上贴短候选集标签；用命名交织 pattern 连接 trigger 与 partner
候选集；给 standalone 和每个 pattern 分配整数票。trigger positive branch 按 DeliverySlot 声明序接受抽取；partner
从对应共享 pool 中无偏、不放回地抽取。partner 可以声明在 trigger 前面或后面。反事实 variant、hidden baseline、
instruction-only、noise 与 replay 都不参与。同一候选集不能同时承担 trigger 和 partner；多个 pattern 可以共享
同一 partner pool，但每条 partner branch 全局至多消费一次。若交织章节与候选集标签都不存在，能力关闭；只声明
其中一侧会在配置期失败。

`no_interleaving_weight = 9` 与唯一可用 pattern 的 `trigger_weight = 1` 表示当前 opportunity 中 standalone 占
9 张票、pattern 占 1 张票，即条件概率为 9/10 与 1/10。它不是配额或“每十条必有一条”；有限样本会波动，pool
耗尽后对应 pattern 会从后续机会的分母移除。none 只在分母出现一次，partner 数量和可实现 owner word 数量都不会
复制权重；抽中 none 不消费 partner pool。所有对应 pool 都为空时不计 opportunity，trigger 直接保持 standalone。

用户不配置 `A B B A B A A` 这样的 owner word。Planner 保持两条 branch 各自的 logical time、gap、duration、
resource、calendar 和事件顺序，只选择两个整体绝对起点；最终 owner word 必须至少有三段连续 owner runs。
`A B B A B A A` 是一种可实现结果，不是枚举值。候选资格不预搜索布局可行性；pattern 与 partner 一旦抽中便进入
冻结 plan。若 calendar、resource、session 容量或交织约束无解，规划以 `generation_plan_infeasible`、exit 2 失败，
不换 partner、pattern、standalone，也不在 provider/slot retry 时重抽。

设可见 primary branch 数为 `N`，冻结交织布局数为 `D`：

```text
interleaved_primary_sessions = D
primary_sessions = N - D
```

`report.generate.sequence` 还给出 `interleaving_opportunities` 和按 TOML 声明序排列的
`by_interleaving_pattern`；每项包含 `eligible_opportunities` 与 `selected_sessions`，包括 selected 为零的 pattern。
report 不输出 pair identity 或 owner word；从最终 stream 的 session 与 timestamp 机械推导真实 owner word。

Planner 的固定 quantum 是 1 毫秒；起点、duration、gap、calendar 边界、variant excess、session gap 与求解结果都
必须毫秒对齐。同一 resource 的所有正 duration event 使用半开区间互斥约束。pattern 的 `containments` 让 contained
role 严格落在 container 区间中，并在 contained 终点与 container 终点之间保留至少 1 毫秒余量；仍存在的关系在
reordered 与 interval-exceeded branch 中同样必须成立。

noise 是没有 owner、没有 state patch 的精确事件槽。`[generate.noise].topics` 必须按 ordinal 显式声明互不重复的
话题，数量与 `timeline.noise_events` 精确相等；planner 把每一项冻结进对应 `NoiseSlot.topic`。renderer 只能围绕
该话题生成，独立 semantic evaluation 分别证明它与声明任务无关、没有可执行诉求、表达自然且忠实计划话题。
MinHash 仍在四项语义门之后拦截文字近重，但不负责猜测两个语义话题是否相同。noise 固定为点事件：
`duration_us = 0`、空 resources，只能使用允许 start binding 的点 frame class。

replay 只从已经通过全部下游处理的最终 positive `SequenceRows` 派生。Planner 为整条 source 选择同一个正、毫秒对齐
的 `shift_us`；所有成员保持 source 的 start delta、duration、resources、role 顺序与非时间 payload，业务时间按 replay
起点重新绑定。timestamp、event ID、replay sequence ID、ordinal 和 duplicate provenance 全部重新确定性计算。
它不会复制 source 时间 payload、预投影记录或另存第二份世界真值。

## 27.8 两个输出视图与 provenance

main 每行是一条最终 sequence，包含已启用的 classification、quality、sequence/frame annotation、verification，以及
`_meta.generation`。declared truth 至少说明 scenario set/index、scenario/world branch、class、pattern、variant、
expected violation 与 actual violations。

stream 每行固定为 `{"payload": ..., "_meta": ...}`。primary event truth 包含 event/event-key、owner sequence、role、
frame class、actor、logical time、timestamp、duration、resources 与 time-binding descriptor。noise 的 owner 为空；
replay 另带 replay sequence ID、ordinal、source sequence/event ID。process replay 的 M2 ingest 会从同一 stream 文件
重算 binding、ID 与 provenance，验证 replay 的 constant shift 和 rebound payload，不把 main 文件或原工程配置当隐式旁输入。
M2 为已验证成员写入删除时间叶子的 exact dedup carrier；M3 对这类 single/sequence 只运行 exact 层。

CrossView 分三层保持线性工作量：每个 PreparedCandidate/PreparedNoiseCandidate 进入缓冲前做 candidate-local 校验；
声明序 head 用 CrossViewFrontier 检查全局 ID、timestamp、source、resource intervals 与 phase/ordinal 并生成 delta；
source 与它的全部 rebound replay 进入同一个 `CrossViewDelta` 和同一次原子提交。全部 primary、replay、noise 内存提交
后，full CrossViewReconciler 从最终 rows 独立重建全部事实，并在最终 timestamp 排序之后复查全局起点唯一、resource
互斥、containment、descriptor、payload 与 annotation。固定计划错误是终态 downstream contract 失败，不作为 slot
重试。main 内成员按 owner 顺序保留；最终 stream 按 timestamp 全局稳定排序，因此交织 session 能呈现真实
A-B-A 或 B-A-B。

Dataset-Person 导出器只做格式转换和只读 overlap/containment 验证；它不会 align、normalize、shift、synchronize 或
重写任何业务时间。raw 工件必须在 LabelKit manifest-last 提交前已经满足这些不变量，消费者无需二次修复。

## 27.9 retained bytes 与原子提交

`record_units` 与 `stream_rows` 上限均为 500000。`retained_content_bytes` 上限为 536870912 bytes；它是最终 main
和 stream 每行 canonical UTF-8 的紧凑核算，包含两个视图中重复的 payload、annotation、generation truth、replay
与元数据，不是预分配 512 MiB 物理内存。

instruction-only 单序列最多 8 个事件。单个完整运行期 prompt value、单项 generation prompt text，以及单轮 L3
新增消息正文集合都以 32768 UTF-8 bytes 为闭上限；恰好上限可以派发，多一 byte 在 provider 调用前拒绝。
系统不会通过截断 ScenarioSeed、ActorView、EventDraft history、patch、payload 或 repair 原输出来“挤进窗口”。

sequence 使用从当前 `next_commit` 开始的连续候选缓冲。generation、evaluation、dedup reservation、quality、annotate、
verify、最终行装配、replay 与 candidate-local CrossView 可跨槽并发；running、prepared 和 recoverable outcome 都占用
同一个槽位，只有 head 成功 commit 才滑入一个新 tail。head 在无 await 临界区按
`dedup revalidate → frontier → retained prospective check → commit` 执行。若 source 本身未超限、加入它的 replay 后
超限，source、replay、reservation 和 dataset counters 仍全部回滚；通过后才提交最终 rows，后续不再构造未计费 replay。

这个语义边界不随 profile 容量从 4 调到 600 而变化。容量 4 的真实模型门证明请求可以重叠；容量 600 的合成门证明
反序完成、reservation、候选缓冲、线性 frontier 与声明序提交在该规模仍闭合。后者不证明外部 endpoint 能处理
600 个真实请求。高容量部署还必须满足 commit service rate 高于 prepared-candidate arrival rate，并让
`candidate_bytes_high_water` 与进程 RSS 留在机器预算内。

成功时依次原子替换 main、stream、report，最后替换 manifest。manifest 是唯一成功提交真值；它记录 run ID、delivery
digest、三个工件的绝对路径、SHA-256 和行数。failed report 是独立诊断文件，不能否定一个摘要有效的成功 manifest。

## 27.10 从 keyless 到真实端点

先运行不读取密钥、不写正式工件的两条命令：

```bash
cd examples/sequence-generation
mkdir -p out
uv run labelkit validate --config config.toml --project project.toml --console plain
uv run labelkit run --config config.toml --project project.toml --dry-run --console plain
```

frame-only 教学工程有自己的同形静态闭环：

```bash
uv run labelkit validate --config config.toml --project project-frame-only.toml --console plain
uv run labelkit run --config config.toml --project project-frame-only.toml --dry-run --console plain
uv run python check_output.py --frame-only --static
```

然后把 `LABELKIT_DEEPSEEK_KEY` 放在仓库根目录的 git-ignored `.env`，只在 shell 环境中加载：

```bash
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
uv run labelkit run --config config.toml --project project-frame-only.toml --console plain
uv run python check_output.py --frame-only
```

两个 DeepSeek profile 都固定为 anthropic route、`deepseek-v4-flash`、`supports_structured_output = false`、
`thinking = "disabled"` 和正数 context window。不要把 key value 放进配置、命令参数、日志或断言文本。

当前证据状态：

v1.21 交织的实时证据只以 `docs/dev/E2E-FINDINGS.md` 的实际运行记录为准；下面保留的是 v1.20 时间完整性与
v1.18/v1.19 历史端点证据，不能据此推断交织已通过真实模型门。

- v1.20 Dataset-Person production constructor 的 keyless validate 已通过；dry-run 为 4380 sequences、
  16320 primary events、零 LLM 调用、零正式输出；
- 重复 compile 的 program digest 同为 `0e0a49...8f94b7`，plan digest 同为 `0c957e...bcca08`；
- plan audit 的 duplicate starts、`foreground_app` overlaps、containment violations 与 annotation resource missing
  都为零；
- config、Schema/M2/M3、planner/program/contracts 与 Dataset-Person 离线门已分别取得
  552、346、122、45+663 的通过证据；完整 offline suite 为 2928 passed、48 deselected；本轮 Uncle Bob review
  独立杀死 10 个时间完整性语义变异，survived、invalid、inconclusive 均为零；
- 十二周付费真实 raw 生成仍为 `[PENDING-EVIDENCE:v1.20-12w-real-generation]`。

历史 v1.18/v1.19 真实端点证据包括：DeepSeek 主例 2 sets、8 sequences、27 stream rows 与 replay checker PASS；
instruction-only 1 sequence、3 events；DeepSeek 核心、双-noise 与两条 failure injection 5 passed in 119.26s；
z.ai structured output 1 passed in 60.81s。这些结果不能替代 v1.20 的 model/full Schema、rebound replay 或 raw 时间门。

## 27.11 历史独立真实感门

最终发布门使用 13 个 counterfactual sets 的 52 条真实 DeepSeek declared 序列。selection seed 20260822 打乱
序列与变体身份；两名评审只看 timestamp、actor、frame class 与 payload，不看内部真值、文件顺序、state、patch、
expected violation 或模型自评。

两名评审各自得到 52 pass / 0 fail；不可能跃迁、提前知情、模板或不自然表达、时间错配和目标点前不一致
全部为零，也没有跨 scenario 的系统性缺陷。因此该 v1.18 真实感门通过。发布工件 main SHA-256 为
`d3247306770068be716aabf3c94c133a74a561b0ac87f4e0c5b8be185fdc250f`，逐评审账本在
`docs/dev/evidence/v1.18-sequence-realism-review.jsonl`；账本不保存完整 payload 或 secret。

## 27.12 历史发布证据

- v1.18 变更前离线基线：2157 tests。
- v1.18 pre-revision 离线套件：2610 passed、47 deselected。
- v1.19 离线套件：2774 passed、48 deselected。
- v1.18 合并覆盖率基线：line 95.71%、branch 91.30%；1548/1548 个可执行生产函数已进入。
- v1.19 完整真实端点 integration suite：47 passed、1 skipped in 370.53s；skip 只因该 shell 未设置本地模型专用 key，
  同一 checkout 的本地 Qwen3.5-4B 四槽门已独立连续通过三次。
- 六百槽合成门：running/commit-waiting high-water 均为 600；固定 64 KiB 候选缓冲 high-water 39321600 bytes，
  peak RSS 183468032 bytes；该工作负载 commit service rate 33761.210/s，高于候选到达率 5396.529/s。
- 500000 record-unit planner 压测：16.889 秒，peak RSS 839221248 bytes。
- 512 MiB retained limit 是紧凑输出字节核算，不是物理内存分配。

真实端点结果与上述离线证据分开记录；端点 429、5xx、额度耗尽属于环境失败，slot exhaustion 属于产品失败。

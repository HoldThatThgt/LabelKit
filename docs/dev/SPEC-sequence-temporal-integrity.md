# LabelKit v1.20 序列时间完整性最终开发规格

> 状态：最终实现规格，设计已冻结，不允许遗留待实现项
> 日期：2026-08-26
> 基线：v1.19 unified execution runtime，main `d72a7d2`
> 破坏边界：v1.20 不读取 v1.18/v1.19 generation stream，不提供别名、migration 或 fallback
> 权威关系：本文件覆盖 v1.18/v1.19 中与业务时间、payload 同源、replay 复制及 event 字段集冲突的条款

## 1. 问题证据与完成标准

LabelKit 现有 `_meta.event.timestamp` 由 ScenarioPlanner 确定性计算，但 payload、sequence annotation 和导出器仍可各自
维护另一份业务时间。十二周生产 raw 工件在 LabelKit 原子提交成功后，独立审核仍发现：

| finding | 数量 |
|---|---:|
| `app_interval_overlap` | 2820 |
| `screen_outside_app` | 1484 |
| `intent_timestamp` | 314 |

后处理又机械归一化 1484 个 App 区间、更新 504 个意图时间字段，并整体平移 250 个 sequence。该事实证明现有成功
manifest 只证明外层工件闭包，不证明 raw 业务时间可直接使用。

v1.20 的完成标准是：工程声明的全部业务时间只由 ScenarioPlanner 派生；primary、noise、replay、sequence annotation、
普通 process replay 与 manifest-last 门禁消费同一权威事实；Dataset-Person 导出器不再修改任何时间。

```mermaid
flowchart LR
    S[完整业务 Schema] --> C[M1 收集时间叶子并投影 model Schema]
    C --> P[ScenarioPlanner 点与区间]
    P --> R[模型生成非时间字段]
    R --> B[框架机械注入时间]
    B --> V[完整 Schema 终验]
    V --> A[M5 机械注入 annotation 时间]
    A --> X[候选与 CrossView 提交前终验]
    X --> M[manifest-last raw 工件]
    M --> I[M2 自包含 replay 复验]
```

## 2. 唯一术语与时间模型

| 术语 | 唯一含义 |
|---|---|
| business time field | 完整 JSON Schema 中以 `x-labelkit-business-time = true` 标记的标量叶子 |
| model Schema | 从完整 Schema 机械剥离业务时间叶子后，仅供模型生成和修复的派生 Schema |
| time binding | 从计划事件起点、终点或时长到一个 business time field 的机械映射 |
| event interval | `[timestamp_us, timestamp_us + duration_us)` 半开区间 |
| exclusive resource | 容量为一、同一时间只能被一个正时长 event interval 占用的命名资源 |
| interval containment | 一个 role 的区间以至少 1 毫秒余量严格包含另一个 role 的区间 |

`PlannedEvent.timestamp_us` 是业务发生时间与工件排序时间的共同权威起点。新增 `duration_us` 是固定非负时长；
`duration_us == 0` 表示点事件，不创建 interval variable，也不进入资源索引。

`logical_time_us` 继续表示单个 world branch 内的相对因果坐标。`ScenarioSeed.time_context` 可以描述日期、场景和业务
上下文，但不得再保存同一 occurrence 的绝对起点、终点或时长。所有 temporal prompt 输入使用当前计划事件的
start、end、duration；模型可以读取这些值以保持内容一致，但输出 Schema 中没有对应字段，因而不能决定最终值。

v1.20 的 Planner quantum 固定为 1000 微秒。timestamp start、duration、gap、calendar 边界、variant excess、session gap
与全部求解结果必须毫秒对齐；不再允许 CP-SAT 用 `+1µs` 满足严格不等式。

## 3. Schema 声明与 model Schema 投影

### 3.1 独立业务时间清单

完整 frame payload Schema 与 sequence annotation Schema 在业务时间叶子上声明：

```json
{
  "type": "object",
  "properties": {
    "timestamp": {
      "type": "integer",
      "x-labelkit-business-time": true
    }
  },
  "required": ["timestamp"]
}
```

`x-labelkit-business-time` 是 LabelKit 在 Draft 2020-12 上解释的 annotation，不改变通用 JSON Schema 验证结果。
标记在场时必须严格等于 `true`；`false` 是配置错误。M1 直接遍历原始完整 Schema，而不是依赖 validator 是否收集
annotation。

标记只能位于显式 `properties` 下的 integer 或 string 标量叶子。instance path 只能穿过 inline
`type = object`/`properties`，路径上的每一级 object parent 与叶字段都必须在对应 `required` 中；每一级 parent 还必须
显式声明 `additionalProperties = false`。禁止根 pointer、数组位置、`-` 和动态 property。

业务时间叶子及其全部 ancestor 形成一个闭合、有限的投影子集。该子集任一节点出现 `$ref`、`$dynamicRef`、`allOf`、
`anyOf`、`oneOf`、`not`、`if`、`then`、`else`、`dependentRequired`、`dependentSchemas`、`patternProperties`、
`propertyNames`、`unevaluatedProperties`、Schema 形态的 `additionalProperties`、`minProperties` 或 `maxProperties` 时，M1
必须聚合失败；ancestor object 上的 `const`、`enum` 同样禁止，不尝试证明一般 Schema 等价性或可满足性。叶子自身可保留
type、format、minimum/maximum、pattern 等
只约束该标量的关键词。Planner 完成后、任何 LLM 调用前，框架对每个机械值执行该叶子 Schema；不通过即
`generation_plan_infeasible`。root example 不能替代这些检查。

Schema 作者仍必须正确标记领域语义；通用框架无法从任意字段名猜测时间。Dataset-Person 迁移门禁独立清点全部已知
`timestamp`、`messageTime`、`startTime`、`endTime`、`createTime`、`updateTime`、`duration` 和 annotation
时间路径，保证生产 Schema 没有漏标。

### 3.2 model Schema

M1 从完整 Schema 一次性派生 model Schema：

- 删除每个 business time leaf 及其所在 object 的对应 `required` 成员；
- 从 Schema 中所有 mapping 形态的 `default`/`examples`、class/frame few-shot output 和 repair `previous_output` 删除相同
  instance path；只删除可达叶子，不创建 parent；
- 删除 provider-facing Schema 中所有 `x-labelkit-business-time` annotation；
- 非时间 property、required、constraint、description 与 example 内容逐字保持；
- 空的中间 object 仍保留对象类型与 required，模型候选必须给出完整 object parent；
- 投影结果必须仍是可执行的 Draft 2020-12 object Schema，并通过现有静态预算。

完整 Schema 保持工程权威且不被改写。模型首轮、self-consistency、L3 repair 和 verify repair 全部只消费 model Schema。
候选在注入前如果自行携带任一 time binding path，当前轮按 model Schema 违规处理；框架不把模型值当作可覆盖输入。

### 3.3 generic candidate finalizer

common inference 新增冻结载体 `FinalizedCallRequest`：`profile`、`prompt`、`model_schema`、`final_schema`、`scope`、
`candidate_finalizer` 与 `repair_projector`。两项 callable 都只接收一个深拷贝 mapping：finalizer 返回待终验的完整对象；
repair projector 返回可安全放入 L3 `previous_output` 的 model-space 对象。temporal finalizer 是不可变 callable carrier，
自身持有 planned event binding values 或冻结的 `SequenceTemporalContext`；request 不再增加第二条 context 通道。公开入口固定为
`complete_finalized(request) -> tuple[dict, Usage, int, str]`，不修改 `complete_validated` 和
`complete_post_validated` 的既有语义。

每一轮严格按下列顺序执行：provider 只接收 `model_schema`；L1 提取对象；对象通过 model Schema L2；finalizer 对深拷贝
恰好执行一次；返回对象通过 full Schema L2；`scope.user_treatment = true` 时再对完整对象执行现有 L2.5；成功只返回这个
完整对象。model Schema 或 L2.5 违规可进入 `output.max_repair_attempts` 配置的既有 L3 轮数，累计 usage、attempts、首轮
model、resolved-at 和 trace 语义不变；v1.20 不另设上限。L3 仍只向 provider 发送 model Schema；parsed candidate 先经
repair projector 再以 canonical JSON 进入 `previous_output`，不可解析候选固定使用 `{}`，所以 repair prompt 不携带
机械字段。repair projector 必须是 total/non-throwing：对缺失 path 或错误 parent 类型只删除仍可达的 time leaf，不创建或
替换 parent，其他字段原样保留。

finalizer/projector 抛异常、返回非 object、改写非时间字段，或 finalizer 产物未通过 full Schema 都是
`candidate_finalizer_contract` 终态，调用方归为 `generation_downstream_contract`，不得进入 L3。temporal helper 负责两个
callable 与非时间字段 byte-equality，SchemaEngine 不认识时间路径。M1 后的 plan preflight 已保证机械叶值本身可验证，
因此 full Schema 失败不能被模型重试掩盖。

## 4. 配置契约

### 4.1 frame interval 与 binding

```toml
[frame.class.app_usage.generate]
instruction = "生成非时间业务字段。"
schema_path = "schemas/app-usage.json"
duration_s = 120
resources = ["foreground_app"]
time_bindings = [
  { payload_path = "/timestamp", source = "event_start_milliseconds" },
  { payload_path = "/startTime", source = "event_start_milliseconds" },
  { payload_path = "/endTime", source = "event_end_milliseconds" },
  { payload_path = "/duration", source = "event_duration_milliseconds" },
]
```

M1 从完整 Schema 收集的 business time path 集必须与 `time_bindings[*].payload_path` 完全相等，声明序唯一且路径互不
为 ancestor/descendant。该等式不能通过另一个 TOML 清单同时漏删。

frame binding `source` 是闭集：

| source | 类型 | 机械值 |
|---|---|---|
| `event_start_milliseconds` | integer | `timestamp_us // 1000` |
| `event_end_milliseconds` | integer | `(timestamp_us + duration_us) // 1000` |
| `event_duration_milliseconds` | integer | `duration_us // 1000` |
| `event_start_iso8601` | string | fixed-offset、六位微秒的 event 起点 |
| `event_end_iso8601` | string | 先做 epoch integer 运算，再以同一 fixed offset 格式化终点 |

`duration_s` 缺省表示点事件；在场时必须精确量化为正整数毫秒。`resources` 是声明序唯一、匹配
`[a-z0-9_]+` 的容量一资源；非空 resource 要求正 duration。一个 event 可以同时占用多个 resource，并进入每个
resource 的互斥集合。end/duration source 要求正 duration。frame role state `payload_bindings` 不得与 time binding
路径相等或互为前缀。

### 4.2 sequence annotation binding

```toml
[class.work_intent.annotate]
time_bindings = [
  { payload_path = "/actionInfo/timestamp",
    source = "first_resource_start_milliseconds",
    resource = "foreground_app" },
]
```

annotation 当前只接受 `first_resource_start_milliseconds`。值来自当前最终 sequence members 中目标 resource 最早正区间
的起点，不读取 LLM 文本、ScenarioSeed 或导出器。完整 annotation Schema 的标记路径集必须与 binding path 集相等。
该配置只允许 `run.mode = generate_only`、`generate.form = sequence`、declared mode；ordinary process、instruction-only、
ordinary annotation 和非 sequence run 一律在 M1 拒绝。

每个可交付 declared branch 必须至少保留一个目标 resource event。annotate disabled 时禁止声明 binding。M1 检查
positive、missing、reordered 与 interval-exceeded 的实际 role word，不能只检查完整 pattern。M5 创建一次冻结的
`SequenceTemporalContext`，只包含最终 member 的 event ID、start、duration 与 resources；sample、vote、leaf repair 和 M7
repair 的每个 `FinalizedCallRequest` 都必须显式携带同一个 context。公共 annotation 入口对有 time binding 却没有 context
的调用抛 internal contract error；没有从 raw、provenance 或 wall clock 猜值的 fallback。

### 4.3 containment

```toml
[[generate.pattern.check_in.containments]]
container = "app"
contained = "screen"
```

两端必须是不同且各出现一次的正 duration role，不得共享任何 exclusive resource。仍同时存在时 Planner 强制：

```text
container.start <= contained.start
contained.end + 1000us <= container.end
```

missing contained 合法，containment 对该 branch 不适用；missing container 但 contained 留存是 M1 错误。reordered 与
interval-exceeded 必须在 CP-SAT 中继续满足所有仍适用 containment，否则在零 LLM 调用前不可行。结构反事实不得顺带
产生 screen-outside-app 或第二种时间违规。

## 5. Planner 约束与确定性

### 5.1 branch 内模型

gap 继续是 role start-to-start 闭区间约束。pattern `max_span_s` 与 session `session_max_span_s` 改为完整 interval
envelope：`max(event.end) - min(event.start)`。calendar window 要求整个 `[start,end)` 落在同一允许窗口；点事件只
检查 start。

每个原子 branch 的 CP-SAT 模型使用 fixed-size interval variable。每个 exclusive resource 聚合同 branch 的 interval 并
调用 `AddNoOverlap`；containment 使用第 4.3 节线性约束。hidden baseline 参加 branch 可行性和 coupling，但不进入最终
全局 resource frontier。

### 5.2 absolute placement

不把十二周全部 event 放入一个巨型 CP-SAT 模型。绝对放置按声明序：

- 普通 session 的 lower bound 从此前所有已放置正区间的最大 end 与 `session_gap_s` 计算；
- crossed session 只联合求解两个 owner，并为共享 resource 加跨 owner `AddNoOverlap`；
- session 内全局 event start 仍唯一；不同 resource 的区间可以重叠；
- 每次绝对平移保持整个 branch 的内部 offset、毫秒 quantum 与 calendar envelope；
- 等价可行解按最早 branch start、声明序 role start 的词典序稳定 tie-break；固定单 worker与 seed 不替代 tie-break；
- capacity 为 1 与高 capacity、重复编译必须得到同一 ScenarioPlan digest。

noise 与 instruction-only 只能选择点 frame class。noise 固定 `duration_us = 0`、空 resources。每个 replay layout 只冻结
一个毫秒对齐的正 `shift_us`，所有成员严格满足
`replay.start[i] = source.start[i] + shift_us`；source 全部 start delta、duration、resources、role 顺序与 interval envelope
逐位保持，不允许逐 event 重新排程。Planner tail 在 resource、session span、calendar 与 timestamp range 约束下选择最小
可行 shift；起点必须与此前全部 primary/noise/replay 起点全局唯一。tail cursor 对每个 event 使用
`start + max(duration_us, 1000)`，因此点事件之后也至少推进一个 quantum。

## 6. 机械注入与事件真值

`PlannedEvent`、`EventDraft`、`EventTruth` 与 evaluator 使用的 observed/review event 全部携带 `duration_us`。
render 路径：

- FrameRenderer 向模型发送 model Schema 与只读 planned start/end/duration；
- instruction-only 与 noise 同样对允许的 start binding 做机械注入；
- 框架在副本上按规范 JSON Pointer 声明序向已验证存在的 object parent 注入；
- 注入失败是内部契约错误；注入后的对象通过完整 frame Schema 后才构造 EventDraft；
- event ID、dedup、retained bytes 与 delivery digest 都基于注入后的最终 payload。

protected prefix 复用 baseline 的非时间 payload，但按当前 branch 的绝对 start/duration 重新注入。CouplingEvaluator 删除
frame class 声明的全部 time binding path 后比较 canonical payload；非时间字段仍逐字节相等，任何非时间篡改必须失败。

M5 在最终 sample/vote 与每次 repair 后注入 sequence annotation 时间，再执行完整 class Schema 与 L2.5。公开
`annotate_record`、`annotate_record_leaf`、批量 `_sequence_wave`、M7 verify repair 与 stream verify repair 都经同一
finalizer；M11 只复验最终机械值，不新增或修复时间。

## 7. 自描述 stream、replay 与 exact dedup

primary、noise、replay 的 `_meta.event` 在既有字段之外固定携带：

```json
{
  "duration_us": 120000000,
  "resources": ["foreground_app"],
  "time_bindings": [
    {"payload_path": "/timestamp", "source": "event_start_milliseconds"}
  ]
}
```

descriptor 使用 frame class 的规范声明序；noise 是 `duration_us = 0`、空 resources，仍可携带 start binding。replay 的
duration、resources 与 descriptor 必须逐位等于 source，payload 按 replay start 重新注入。

v1.20 统一使用 canonical array header `labelkit:v1.20`，不接受旧 stream。下列 generation-specific 域必须全部升级，
源码与测试中不得残留 v1.18/v1.19 literal：`derive_generation_id`、`generation_program_digest`、
`scenario_plan_digest`、planner seed、scenario/workflow attempt random、noise/payload witness、prepared primary/noise、
delivery digest 与 generation-stream exact identity。program/plan digest 分别固定为
`sha256(canonical_json([header, "generation_program", semantic_program]))` 与
`sha256(canonical_json([header, "scenario_plan", semantic_plan]))`；delivery 继续长度 framing，但初始 bytes 改为
`b"labelkit:v1.20:delivery\n"`。attempt random 只保留一个共享派生函数，scenario 与 orchestration 不再复制公式。

| ID | components |
|---|---|
| primary event | world branch、event key、start、duration、resources、time descriptor、最终 payload |
| noise event | run ID、event key、start、零 duration、空 resources、time descriptor、最终 payload |
| replay event | replay sequence ID、source event ID、replay start、source duration、最终 rebound payload |

sequence、scenario、event key、run 与 replay sequence 的其余公式保留有序 component 语义，但统一使用 v1.20 header。

普通 process 的 M2 只读 stream，因此不依赖原工程配置。它从 event descriptor 重算 primary/noise/replay binding，验证
duration/resources/descriptor 形状与 replay 同源；比较 replay 与 source 时先删除 descriptor 中的 time paths，再要求非时间
payload 与下游 metadata 相同；随后用 rebound payload 重算 replay event ID。缺失、额外、冲突或非法 descriptor 都失败，
旧逐字节 payload-copy 规则删除。

raw `Record.text` 与 `Record.raw` 保留真实 rebound payload。`Record` 新增
`exact_dedup_text: str | None = None`；M2 对每个已通过 descriptor 复验的 generation stream member，把删除全部 time paths
后的 payload canonical JSON 写入该字段，其他 ingest 路径固定为 None。M3 遇到字段非空的 single，或全部 member 字段非空
的 sequence，只运行 exact 层，不构造 MinHash 或 embedding；普通 Record 保持既有四级判重。

generation sequence exact key 固定为
`sha256(canonical_json(["labelkit:v1.20", "generation_stream_exact", ordered_exact_dedup_texts]))`，其中 member 文本按
sequence member 顺序进入数组。该公式只消费 M2 从内容证明得到的 carrier，不读取 replay provenance。合法 replay 仍命中
exact duplicate；任何非时间 payload 差异不得命中。generation single 使用同一公式的一元素数组，避免 single/sequence 域撞。

## 8. 提交前不变量

候选只有在 `group_commit` 前满足下列全部事实才可进入正式内存状态：

| 检查面 | 不变量 |
|---|---|
| plan identity | event start、duration、role、frame、session 与 ScenarioPlan 完全一致 |
| stream descriptor | duration、resources、time bindings 与 program frame class 完全一致 |
| payload | 每个 time path 等于 start/duration 的机械值，完整 Schema 通过 |
| annotation | 每个 time path 等于最终 member resource 的最早 start，完整 Schema/L2.5 通过 |
| containment | 当前 branch 所有仍适用关系满足 1ms 严格余量 |
| replay shift | 每个 replay member 等于 source member 的同一常量平移，start delta/duration/resources 不变 |
| candidate resources | 当前 primary 与随 source 交付的 replay 内同 resource 半开区间不重叠；全部 event start 唯一 |
| frontier resources | 当前区间与已提交声明序前缀不重叠 |
| identity and bytes | ID、dedup、retained bytes 与 canonical delivery digest 使用最终 rebound 内容 |

`CrossViewFrontier(program, plan)` 使用 Planner 预计算的期望 resource intervals，不信任 payload。`CrossViewDelta` 冻结
phase、ordinal、event IDs、timestamps、source keys 与按 resource/起点排序的 interval tuple。primary candidate 成功后立即从
最终 source rows 构造匹配 replay；`check_primary` 同时检查并冻结该 source 的 primary rows、全部 replay rows、intervals、
IDs、retained bytes 与 delivery bytes，二者进入同一个 delta 和同一次 ordered commit，不存在独立 `check_replay` phase。
`check_noise` 只冻结当前 noise。

`group_commit`、`frontier.commit(delta)`、primary/replay counters/rows/retained commit 之间无 `await`。commit 只消费同一
checked delta，不重新解释 candidate，也没有普通失败分支。任何 replay 构造、Schema、binding、constant-shift、frontier
或 retained rejection 都在 source 原子单元内回滚；不会先提交 primary 再补 replay。

全部 primary、noise、replay 内存提交后，final reconcile 从 program、plan、main 与 stream 独立重建上述事实，以每个
resource 的 O(n log n) sort/sweep 检查全量，不复用 frontier 结论。任何失败都发生在打开 `.part` 或替换 manifest 前。

## 9. 失败语义

| 失败 | 处置 |
|---|---|
| Schema 声明、binding、resource、containment 或毫秒量化错误 | M1 聚合 `CONFIG_ERROR`，零凭据物化 |
| branch、crossed session 或 replay 区间不可行 | `generation_plan_infeasible`，零 LLM 调用 |
| model Schema 候选或完整对象 L2.5 失败 | 既有可恢复生成/标注拒绝，可消费 attempt |
| plan mechanical leaf value 不满足完整 leaf Schema | `generation_plan_infeasible`，零 LLM 调用 |
| candidate finalizer/projector/full Schema contract 失败 | terminal `generation_downstream_contract`，不进入 L3 |
| outer/payload/annotation/containment/resource/frontier 固定计划不一致 | terminal `generation_downstream_contract`，不重试 slot |
| final full reconcile 时间不一致 | terminal internal invariant，零正式输出提交 |

固定计划不变量重试不会改变结果，因此不得归入 recoverable reconcile。错误日志为英文，只记录 slot/stage/reason 类型，
不记录 payload、时间实际值、Schema example、prompt 或用户内容。

## 10. 下游迁移与删除

Dataset-Person 的 production constructor 必须迁移到 Schema annotation、frame time binding、fixed duration/resource 与
pattern containment。catalog、state Schema、outcome Schema、role roots 与 builder 中用于维护第二权威时间的
`event_times`、`short_duration_ms`、`shopping_duration_ms`、`long_duration_ms` 全部删除。

导出器删除：

- 按 `_meta.event.timestamp` 重写 frame payload 时间；
- 延长 App duration 以包含 screen；
- 为避免 App overlap 平移 sequence；
- 按 member App 时间重写 main annotation timestamp。

导出器只做格式转换与只读验证；对已经正确的 raw 输入，所有业务时间逐字不变。没有兼容开关、repair fallback 或旧工程
读取路径。

## 11. 实现文件与依赖顺序

| 责任 | 生产文件 |
|---|---|
| Schema 声明/投影与配置 | `labelkit/common/config/model.py`、`_classviews.py`、`generation.py`、`_generation_budget.py`，新增 common temporal helper |
| generic finalizer seam | `labelkit/common/inference/schema_engine.py` 与 contracts |
| program/truth carrier | `labelkit/common/contracts/generation.py`、`labelkit/operators/generation/program.py` |
| interval plan | `labelkit/operators/generation/planner.py` |
| render/coupling | `generation/render.py`、`scenario.py`、`evaluate.py`、generation prompts |
| annotation | `labelkit/operators/annotate.py`，verify/stream-verify 只通过统一 leaf seam 回流 |
| projection/replay/frontier | `labelkit/operators/generation/project.py`、`orchestration/sequence_workflow.py` |
| final rows and consumer | `labelkit/operators/emitter.py`、`labelkit/operators/ingest.py`、`labelkit/operators/dedup.py` 与 generation-specific exact identity carrier |
| production project | Dataset-Person schemas、`build_white_collar_project.py`、`export_stream_views.py` 与对应 tests |
| authoritative docs | 本文件、两份既有 dev spec、`docs/CONTRACTS.md`、`spec/301`、`302`、`305`、`306`、`307`、`310`、`311`、`317`、`40`、`50`、`60`、manual 与 E2E findings |

`AGENTS.md` 与 `CLAUDE.md` 在全部变更后保持 byte-identical。生成 design PDF 只从同步后的 split spec 构建。

## 12. 必须完成的测试矩阵

### 12.1 配置与 Schema

- Schema 标记与 binding path 精确等集；漏 binding、多 binding、重复/前缀 path、错误 source/type/resource 全部聚合失败。
- 未标记但 Dataset inventory 认定为时间的字段失败；投影 ancestor 上每个禁止 keyword、非 required parent、非 false
  `additionalProperties` 与 array/dynamic parent 均有独立失败用例。
- model Schema、Schema default/examples、few-shot output、repair previous output 均无机械字段，非时间约束逐字保持。
- finalizer 的 provider Schema、L2/finalize/full-L2/L2.5 次序、恰好一次 callable、usage/attempt/model/trace 保持均有测试；
  configured repair 次数保持；projector 对缺失/错误 parent 为 total；projector/finalizer exception、非法 return、非时间
  mutation 与 full Schema fail 均 terminal 且不调用 L3；finalizer carrier 自持且只持一份 temporal context。
- 模型返回无时间字段，机械注入后完整 Schema/L2.5 通过；缺失 required parent、错误父类型和模型自行输出时间字段失败；
  mechanical leaf constraint 在 plan 后、首个 LLM 前失败。
- role state binding 冲突、disabled binding、process/instruction-only annotation binding、缺失或替换 temporal context 失败。

### 12.2 Planner

- 同 resource 相邻 `[0,10)`、`[10,15)` 通过，严格 overlap 失败；多 resource 进入每个互斥集合。
- containment 余量 999 微秒失败、1000 微秒通过；两端共享 resource 在 M1 失败。
- positive 与 missing-contained 通过；missing-container、破坏 containment 的 reordered/interval-exceeded 零 LLM 失败。
- gap 保持 start-to-start；max span/session span/calendar 使用完整 interval envelope。
- crossed owner 的毫秒对齐、resource no-overlap、calendar end 与 session gap 均有边界测试。
- replay 与 primary/resource overlap 被 Planner 选择 constant shift 或 keyless 判不可行；全部 member shift 相同，start delta、
  duration、resources 不变，point tail 至少推进一个 quantum，replay span 按 source interval envelope 计算。
- 重复编译及 runtime capacity 1/600 得到相同 plan digest。

### 12.3 render、annotation、replay 与 process replay

- primary、instruction-only、noise 的 start/end/duration integer 与 ISO binding 全部机械同源。
- protected prefix 重新绑定当前时间；去除 time paths 后 byte-equal，非时间 mutation 被 coupling 杀死。
- self-consistency、public annotate、leaf repair、M7 repair 显式复用同一 temporal context，annotation 时间都等于最早
  resource start；process/public 无 context 路径不能使用 binding。
- replay 重绑 payload 后完整 frame Schema 通过，duration/resources/descriptor 与 source 相同；event ID 使用 rebound payload。
- M2 合法 rebound replay 通过；descriptor 篡改、漏/额外 path、time path 篡改与非时间内容篡改失败。
- 教学 replay 继续 `dropped_dup = 1`；只改非时间字段不得 exact duplicate；generation carrier 不运行 MinHash/embedding，
  exact key 严格匹配 v1.20 公式，ordinary Record 判重行为不变。

### 12.4 提交与原子性

- 篡改 primary/replay/noise duration、descriptor、payload time、main annotation、containment 或 frontier interval，全部在
  `group_commit` 前 terminal，且 reservation、dataset、rows、retained、manifest 零提交。
- frontier interval 与 source key delta 无候选内/前缀冲突；commit 只接受同一 checked phase/ordinal delta。
- source 的 primary/replay 只产生一个 checked delta；replay 构造或验收失败时 primary/replay counters、rows、bytes 全部为零。
- final reconcile 独立捕获绕过 candidate/frontier 的每一种时间篡改。
- retained bytes、delivery digest 与 manifest hash 包含 rebound payload 和新 event metadata。
- production/tests 的 generation domain 无 v1.18/v1.19 literal；program、plan、attempt、prepared、delivery 与 exact digest 均有
  固定向量测试。

### 12.5 生产工程

- 十二周 production constructor 的 `validate` 与 `dry-run` 消费同一 program/plan digest。
- raw 计划证明全局 `foreground_app` 不重叠、screen 严格包含、main intent 等于最早 App start。
- exporter 源码不存在 align/normalize/shift/synchronize timestamp 行为；保留只读 overlap/containment validator，raw 时间零修改。
- 旧 `event_times` 状态、duration 第二权威、旧 payload time state bindings 与相关测试全部删除。
- 仍受支持的 weekly/12-week/24-week constructor、Schema 与 catalog 全部迁移；不再支持的工程直接删除，不留旧契约。

## 13. 完成门

实现必须依次通过窄回归、完整离线 suite、`git diff --check`、源代码尺寸/注释规则、十二周 keyless validate/dry-run、
raw temporal audit 与 Uncle Bob review。Uncle Bob 至少独立杀死第 12 节的 Schema projection、strict containment、resource
overlap、replay rebinding、annotation repair、M2 descriptor、exact dedup 与 precommit terminal mutants，零
survived/invalid/inconclusive。

昂贵的十二周真实 LLM 重生成不因本开发请求自动获得付费授权；若未获单独授权，E2E findings 只能标记
`[PENDING-EVIDENCE:v1.20-12w-real-generation]`。该 pending 只表示尚未执行外部昂贵生成，不得掩盖任何代码、测试、
constructor、exporter、keyless 计划或 raw audit 待实现项。

## 14. 成熟方案依据

- OR-Tools interval variable 与 `AddNoOverlap`：https://developers.google.com/optimization/scheduling/job_shop
- OR-Tools CP-SAT Python API：https://or-tools.github.io/docs/pdoc/ortools/sat/python/cp_model
- PostgreSQL 半开 range 与 resource exclusion：https://www.postgresql.org/docs/17/rangetypes.html
- PostgreSQL deferred constraint：https://www.postgresql.org/docs/15/sql-set-constraints.html
- PostgreSQL generated column：https://www.postgresql.org/docs/18/ddl-generated-columns.html
- RFC 6901 JSON Pointer：https://www.rfc-editor.org/rfc/rfc6901.html
- RFC 6902 JSON Patch：https://www.rfc-editor.org/rfc/rfc6902.html
- JSON Schema Draft 2020-12 annotation/extension：https://json-schema.org/draft/2020-12/json-schema-core
- Structured output 仍不保证字段值正确：https://openai.com/index/introducing-structured-outputs-in-the-api/

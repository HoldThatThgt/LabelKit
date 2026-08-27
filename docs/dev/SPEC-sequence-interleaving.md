# LabelKit v1.21 序列交织配置与规划规格

> 状态：实现规格，字段、术语、失败行为与完成门已冻结
> 日期：2026-08-27
> 实施基线：`8fafbd1830e52c378b6f48a0d48aa8fd318545ab`
> 破坏边界：删除用户声明的 primary/crossed session 数，不提供兼容、迁移或 fallback
> 适用范围：declared sequence generation 的 primary positive branch 时间布局

## 1. 交付结论

用户只声明短候选集标签、命名交织 pattern 和整数触发权重。LabelKit 在凭据物化和任何 LLM 调用之前，
用 run seed 冻结是否交织、使用哪个 pattern、与哪条 partner branch 配对以及两条 branch 的绝对时间。
用户不枚举 `A B B A B A A` 之类的 owner word，也不列出所有变体。

~~~mermaid
flowchart LR
    C["counterfactual set\n短候选集标签"] --> G["按候选集建立\ntrigger scan 与 partner pool"]
    P["命名 pattern\ntrigger/partner/整数权重"] --> G
    G --> W["精确整数加权抽取\nnone 或一个 pattern"]
    W --> R["partner pool\n无偏、不放回抽取"]
    R --> S["CP-SAT 平移两条 branch\n保持内部时间与资源"]
    S --> F["冻结 ScenarioPlan\nLLM retry 不重抽"]
~~~

本能力只改变工件 timestamp 与 session。两条序列仍有独立 ScenarioSeed、世界状态、模型上下文、
DeliverySlot attempt 和 ordered commit；交织不表示跨 branch 因果感知，也不创建双槽共同重试事务。
最终 manifest-last 提交仍保证不会把失败运行中的半个 pair 声称为成功数据集。

## 2. 业界先例与理论边界

本设计采用成熟语义的交集，不引入新的概率或并发术语：

- CSP 的 interleave 保持各进程内部行为并允许全局穿插；LabelKit 是其中受固定时间平移约束的严格子集。
  参考 [FDR CSP interleave](https://cocotec.io/fdr/manual/cspm/processes.html)。
- QuickCheck `frequency` 汇总整数权重后在累计区间选择 alternative，权重不按候选数复制。
  参考 [QuickCheck Gen.hs](https://github.com/nick8325/quickcheck/blob/master/src/Test/QuickCheck/Gen.hs)。
- OR-Tools job shop 用 precedence 与 `AddNoOverlap` 表达内部顺序和共享资源，适合两条固定 branch 的平移布局。
  参考 [OR-Tools Job Shop](https://developers.google.com/optimization/scheduling/job_shop)。
- OR-Tools solver seed 影响求解过程，不是产品可承诺的抽样分布；产品概率必须在 solver 外显式完成。
  参考 [SatParameters](https://github.com/google/or-tools/blob/stable/ortools/sat/sat_parameters.proto)。
- Temporal 把 replay-safe randomness 与普通随机源分开；LabelKit 同样先冻结计划，retry 不重新抽取。
  参考 [Temporal Workflow.java](https://github.com/temporalio/sdk-java/blob/main/temporal-sdk/src/main/java/io/temporal/workflow/Workflow.java)。

若两条 branch 分别有 `m` 和 `n` 个事件，而每条 branch 只允许整体平移，则 owner word 只会在 `m*n`
个相对时间阈值处变化，最多有 `m*n+1` 种平移诱导结果。系统不承诺所有保持内部顺序的组合 shuffle，
也不承诺 owner word 均匀分布。

## 3. 唯一术语

| 术语 | 唯一含义 |
|---|---|
| interleaving candidate set | 用户在 counterfactual set 上声明的短标签；只选择该声明展开出的 positive branch |
| interleaving pattern | 一个有名称的 trigger candidate set、partner candidate set 与 trigger weight |
| trigger branch | 按 DeliverySlot 声明序接受一次交织抽取的 positive branch |
| partner pool | 一个 partner candidate set 尚未消费的全部 positive branch |
| interleaving opportunity | 当前 trigger 至少有一个 partner pool 非空的 pattern 时发生的一次抽取 |
| interleaving layout | 已冻结 pattern、trigger branch、partner branch 及其共享 session 时间布局 |
| owner word | 把共享 session 按 timestamp 排序后得到的 branch owner 序列；仅由 plan blocks 派生 |

`crossing`、`crossed session`、`outer`、`inner`、`owner_pattern` 和 selector DSL 不属于 v1.21 术语。

## 4. TOML 契约

### 4.1 完整示例

~~~toml
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
~~~

候选集标签使用 `[a-z0-9_]+`，精确相等，不支持 glob、regex、前缀、列表或表达式。
`food_dinner` 这类短业务标签是正常用法；不要求把 class、日期、tier、App 名等编码进标签。

### 4.2 字段

| 位置 | 字段 | 类型与语义 |
|---|---|---|
| `[[generate.counterfactual_sets]]` | `interleaving_candidate_set` | 可选短标签；该声明全部 slot 的唯一 positive branch 进入该候选集 |
| `[generate.interleaving]` | `no_interleaving_weight` | 必填非负 TOML int64；一次 opportunity 中选择 standalone 的权重 |
| `[generate.interleaving.pattern.<name>]` | `trigger_candidate_set` | 必填候选集标签；其 positive branch 按声明序接受抽取 |
| `[generate.interleaving.pattern.<name>]` | `partner_candidate_set` | 必填且不同于 trigger；其 positive branch 进入共享不放回 pool |
| `[generate.interleaving.pattern.<name>]` | `trigger_weight` | 必填正 TOML int64；选择该 named pattern 的权重 |

pattern name 与候选集标签使用同一名称正则。至少声明一个 named pattern。没有 `kind` 字段：v1.21 只有一种
“保持各自 branch、形成真正 owner 交织”的语义，增加单值枚举只会形成无效配置层。

### 4.3 开关与模式

能力关闭的唯一形式是 `[generate.interleaving]` 与全部 `interleaving_candidate_set` 同时不存在。
不存在隐式缺省 pattern、自动候选集或自动启用。

交织只允许 `generate.form="sequence"` 且 sequence mode 为 `declared`。flat 与 instruction-only 禁止交织章节和
候选集标签。instruction-only 的 report 仍输出固定的零值交织统计。

## 5. 配置闭包

解析器必须一次聚合并报告以下 `generation_config_invalid`：

- 旧 `[generate.timeline].primary_sessions` 或 `crossed_primary_sessions` 出现；两键已删除。
- 交织章节与候选集标签只出现一侧。
- candidate set、pattern name 不符合名称正则，或引用不存在。
- 带候选集标签的 counterfactual set 没有且仅有一个 `kind="positive"` variant。
- trigger 与 partner 相同，或同一 candidate set 同时承担 trigger 与 partner 角色。
- 任一已声明 candidate set 未被 named pattern 引用。
- 任一 pattern 的 trigger 或 partner candidate set 没有成员。
- `no_interleaving_weight` 为负、`trigger_weight` 非正、bool、float、超出 int64，或机会总权重超出 `2^63-1`。
- flat 或 instruction-only 使用交织配置。

一个 trigger candidate set 可由多个 named pattern 引用。一个 partner candidate set 也可由多个 pattern 共享；
这些 pattern 消费同一个 pool，任何 partner branch 全局至多使用一次。

## 6. 候选与机会

Planner 先完成全部 DeliverySlot 和 logical branch，再按以下规则冻结交织：

~~~mermaid
flowchart TD
    A["收集每个带标签 set 的唯一 positive branch"] --> B["按 candidate set 建立共享 partner pool"]
    B --> C["按 DeliverySlot 声明序扫描 trigger positive branch"]
    C --> D{"至少一个对应 partner pool 非空?"}
    D -- 否 --> E["standalone；不计 opportunity"]
    D -- 是 --> F["计一次 interleaving opportunity"]
    F --> G["对 none 与当前 applicable patterns 加权抽取"]
    G -- none --> H["standalone；不消费 pool"]
    G -- pattern --> I["从该 pattern 的共享 partner pool 无偏抽一个并 swap-delete"]
    I --> J["冻结 pair；不换配对"]
~~~

候选资格只看 declared positive branch 与 pool 是否非空，不预搜索 calendar/resource/layout 可行性。
反事实 variant、hidden baseline、instruction-only、noise、replay 均不进入 trigger 或 partner。
partner 可以位于 trigger 声明之前或之后；相对声明位置不改变资格。

`by_interleaving_pattern.<name>.eligible_opportunities` 在该 pattern 对当前 trigger 生效且其 partner pool 非空时加一。
一次 trigger scan 可同时增加多个 pattern 的 eligible count，但全局 `interleaving_opportunities` 只加一。

## 7. 权重数学语义

对一个 opportunity，设当前 applicable pattern 按 TOML 声明序为 `p1..pk`，其权重为 `w1..wk`，
`no_interleaving_weight=w0`，总权重 `W=w0+Σwi`。则：

~~~text
P(no interleaving | current applicable patterns) = w0 / W
P(select pi | current applicable patterns) = wi / W
~~~

none 只进入分母一次。partner 数量、可实现 owner word 数量、pattern 中的候选 pair 数量都不复制权重。
当某个 partner pool 耗尽时，其 pattern 从后续 opportunity 的分母移除；当所有对应 pool 都耗尽时，当前 trigger
不构成 opportunity，直接 standalone。

权重不是频率目标、配额或“每 N 条必触发一次”。有限样本中实际比例可偏离配置比率。

### 7.1 精确整数抽样

每次抽取使用 `generation_random` 的完整 SHA-256 整数，不使用 float threshold、solver seed 或简单取模。
给定正整数范围 `T`：

~~~text
M = 2^256
limit = M - (M mod T)
x(counter) = generation_random(domain, value(counter))
接受第一个 x < limit，结果为 x mod T
~~~

两种抽取的 domain 与 canonical value 数组冻结如下；`counter` 从零开始：

| 抽取 | domain | `value(counter)` 的精确字段顺序 |
|---|---|---|
| pattern ticket | `interleaving_pattern_choice` | `[program_digest, planner_seed, trigger_slot_key, trigger_variant_name, counter]` |
| partner index | `interleaving_partner_choice` | `[program_digest, planner_seed, trigger_slot_key, trigger_variant_name, pattern_name, partner_candidate_set, counter]` |

被拒绝时只增加该抽取的 counter，不改变 opportunity、pool 或其他随机域。
pattern ticket 区间为：none 占 `[0,w0)`，随后各 pattern 按 TOML 声明序占连续区间。

partner 选择使用独立 domain 和相同拒绝采样，范围为当前 pool 长度。pool 初始顺序为 DeliverySlot 声明序；
选中索引后以 swap-delete 删除。该过程是不放回的无偏索引抽样，不承诺最终配对的全局均匀匹配分布。

## 8. 时间布局

两条选中 branch 的 logical time、相邻 gap、duration、resource、calendar window 与 branch 内顺序全部冻结。
Planner 只求两个整体绝对起点，并强制：

- 所有 event start 毫秒对齐且全局唯一。
- 同名 resource interval 满足 `AddNoOverlap`。
- 共享 session 的 event count 与完整 interval envelope 不超过 timeline 容量和 span。
- owner word 至少有三个 maximal runs，等价于存在 `A-B-A` 或 `B-A-B` witness。
- session 与前一个已放置 session 满足 `session_gap_us`。

选中 pair 在两个 branch 原声明位置中较早的位置创建 session；另一个位置随后跳过。两 branch 之间原有的其他
branch 在该 pair 之后继续放置。这个布局顺序只影响工件时间，不改变 main 的 DeliverySlot/variant 交付顺序，
也不改变 replay source 的声明序选择。

### 8.1 seed 驱动的可实现布局

对 trigger 的每个相邻事件 gap 构造 `A-B-A` witness，对 partner 同理构造 `B-A-B` witness。
`witness_owner` 固定为 `"trigger"|"partner"`，`witness_gap_index` 是该 owner 内零基相邻 gap 序号。
每个 witness 的排序整数精确为：

~~~text
generation_random("interleaving_witness_rank", [
  program_digest, planner_seed, pattern_name,
  trigger_slot_key, trigger_variant_name,
  partner_slot_key, partner_variant_name,
  witness_owner, witness_gap_index
])
~~~

witness 按 `(排序整数, owner_order, witness_gap_index)` 升序获得零基唯一 rank，`owner_order` 固定为
trigger=0、partner=1；SHA-256 碰撞不能交给容器或 solver 次序裁决。CP-SAT 选择一个满足条件的 witness，
依次最小化：

- witness rank；
- session 最早 event start；
- trigger branch start；
- partner branch start。

因此同一 program 与 seed 得到逐字节相同的计划；不同 seed 在存在多个可实现 witness 的 fixture 上能够选择不同
owner word。系统只在可实现集合中选择，不接受用户提交 owner word，也不把不可实现组合强行映射到近似结果。

例如，A 的 offsets 为 `[0,10,20,30]` 秒，B 的 offsets 为 `[0,2,12]` 秒，B 整体相对 A 平移 5 秒时，
owner word 为 `A B B A B A A`。它是可验收例，不是新增枚举值。

## 9. 选中后失败闭包

pattern 和 partner 一旦抽中，就不搜索其他 partner、不切换 pattern、不退回 none、不改为 standalone，
也不重新抽取。选中 pair 无合法绝对布局时，规划以 `generation_plan_infeasible`、exit 2 失败。

~~~mermaid
flowchart LR
    S["seed + program"] --> D["冻结抽取与 pair"]
    D --> L{"布局可证明 OPTIMAL?"}
    L -- OPTIMAL --> P["冻结 ScenarioPlan"]
    L -- INFEASIBLE --> I["generation_plan_infeasible"]
    L -- FEASIBLE/UNKNOWN --> B["generation_plan_budget"]
    L -- MODEL_INVALID --> X["generation_plan_internal"]
    P --> R["slot/provider/downstream retry"]
    R --> P
~~~

计划在凭据物化和 LLM 前唯一编译。provider retry、recoverable attempt retry、并发完成序和 ordered commit 均复用
同一 plan，不消费任何交织随机数。

## 10. 冻结载体

全部 dataclass frozen，Mapping 在构造时复制并冻结。按声明序新增或修改：

| 类型 | 冻结字段 |
|---|---|
| `CounterfactualSetSpec` | `name`, `pattern`, `count`, `interleaving_candidate_set`, `variants` |
| `InterleavingPatternSpec` | `name`, `trigger_candidate_set`, `partner_candidate_set`, `trigger_weight` |
| `InterleavingSpec` | `no_interleaving_weight`, `patterns` |
| `TimelineSpec` | `timestamp_start_us`, `utc_offset_minutes`, `event_gap_us`, `session_max_events`, `session_max_span_us`, `session_gap_us`, `noise_events`, `duplicate_sequences` |
| `SequenceGenerationConfig` | 既有字段中在 `instruction_only` 后加入 `interleaving` |
| `GenerationProgram` | 既有字段中在 `instruction_only` 后加入 `interleaving` |
| `InterleavingLayout` | `pattern_name`, `trigger_slot_key`, `trigger_variant_name`, `partner_slot_key`, `partner_variant_name` |
| `ScenarioPlan` | `blocks`, `delivery_slots`, `noise_slots`, `replay_layouts`, `interleaving_layouts`, `interleaving_opportunities`, `interleaving_pattern_opportunities`, `primary_sessions`, `digest` |

`DeliverySlot` 不复制 candidate set 标签；`source_name` 与 frozen program 的 CounterfactualSetSpec 是唯一归属真值。
`InterleavingLayout` 不复制 session ID、timestamp 或 owner word；这些事实由 plan blocks 唯一持有。

program digest 递归覆盖候选归属、pattern 与权重。plan digest 显式覆盖机会统计、pattern opportunity map、pair identity、
blocks 中 exact timestamp/session 和其他既有计划字段。`validate_plan_identity` 必须重建并逐字段比较，不信任 carrier 自报。

`labelkit:v1.20` 保留为 generation ID、stream exact carrier 与 delivery digest 的工件编码域；它不是产品修订号。
v1.21 没有改变这些工件的 ID 公式或 stream envelope，不为版本展示制造无语义 ID churn。

## 11. 派生计数与 report

设全部可见 primary branch 数为 `N`，冻结交织布局数为 `D`：

~~~text
interleaved_primary_sessions = D
primary_sessions = N - D
~~~

交织不改变 planned/delivered set、sequence、event、stream row、LLM call、noise 或 replay 计数。

estimate 与成功 report 的 `generate.sequence` 在 `program_digest` 后新增 `plan_digest`，并冻结以下交织字段：

~~~json
{
  "plan_digest": "<64 lowercase hex>",
  "interleaving_opportunities": 2,
  "primary_sessions": 6,
  "interleaved_primary_sessions": 2,
  "by_interleaving_pattern": {
    "food_with_entertainment": {
      "eligible_opportunities": 2,
      "selected_sessions": 2
    }
  }
}
~~~

named pattern map 按 TOML 声明序输出，包括 selected 为零的 pattern。disabled 与 instruction-only 输出：

~~~json
{
  "interleaving_opportunities": 0,
  "interleaved_primary_sessions": 0,
  "by_interleaving_pattern": {}
}
~~~

report 不输出 slot identity、逐 pair owner word、payload、prompt、state 或 API key。exact pair identity 只在内存
ScenarioPlan 中；真实 owner word 从 stream/plan blocks 机械推导。

## 12. 复杂度与资源门

匹配阶段不得构造 trigger × partner pair matrix，也不得为未选 partner 试跑 CP-SAT：

~~~text
matching time = O(positive branches + evaluated pattern incidences + selected pairs)
partner pool memory = O(positive branches)
selected pair constraints = O(m*n + m + n)
~~~

现有 pattern role 上限为 32，因此单 pair 的跨 owner event-start uniqueness 至多 1024 对。必须保留
500000 record-unit 无交织 planner probe，并新增 600 positive branch、300 强制 pair 的真实 planner/RSS probe。
规模门记录 wall time、peak RSS 和 plan digest；不得只用复杂度公式代替运行证据。

## 13. 文件修改清单

### 13.1 生产代码

- `labelkit/common/config/_sections.py`
- `labelkit/common/config/_constraints.py`
- `labelkit/common/config/generation.py`
- 新建 `labelkit/common/config/_sequence_layout.py`
- `labelkit/common/contracts/generation.py`
- `labelkit/operators/generation/program.py`
- `labelkit/operators/generation/planner.py`
- `labelkit/operators/generation/project.py`
- `labelkit/orchestration/sequence_workflow.py`
- `labelkit/orchestration/process_workflow.py`
- `labelkit/cli/console.py`

不增加第三方依赖、错误类、emitter 算法、runtime 分支或 LLM prompt。`generation.py` 基线已有 1999 行；timeline
解析、派生计数校验与新交织解析迁入 `_sequence_layout.py`，避免继续膨胀聚合模块。

### 13.2 权威规格、手册与示例

- `docs/dev/SPEC-sequence-generation-redesign.md`
- `docs/dev/SPEC-sequence-temporal-integrity.md`
- `docs/dev/SPEC-execution-runtime.md`
- `docs/CONTRACTS.md`
- `spec/00-frontmatter.md`, `spec/10-ch1-overview.md`, `spec/20-ch2-overall-design.md`
- `spec/40-ch4-data-structures.md`, `spec/50-ch5-config-spec.md`, `spec/60-ch6-io-formats.md`
- `spec/301-m1-config.md`, `spec/306-m6-generate.md`, `spec/310-m10-orchestration.md`
- `spec/85-ch9-references.md`, `spec/90-appendix-a-rubrics.md`
- sequence generation 相关 manual、README、TOML 与 checker
- `AGENTS.md` 与 `CLAUDE.md`，保持 byte-identical
- 真实验证后更新 `docs/dev/E2E-FINDINGS.md`，再由构建工具重建 `docs/design`

### 13.3 测试

- config、carrier、program digest、plan digest 与 identity fixed vector
- planner 候选、权重、pool、布局、calendar/resource/capacity、determinism 与规模测试
- workflow estimate/report、retry/并发不重抽、console golden 与 example checker
- DeepSeek、z.ai 真实 endpoint 回归与本地 Qwen3.5-4B 四槽真实门
- Uncle Bob mutation review 覆盖本规格的概率、资格、布局、失败、摘要与 retry 语义

## 14. 验收矩阵

| 特性面 | 必须通过的可观察行为 |
|---|---|
| 配置 | section 缺省、正确开启、旧键拒绝、单边配置拒绝、未知/空/未引用 candidate、角色冲突、int64 权重边界 |
| 权重 | `9:1` ticket 边界精确；多 pattern 只含一次 none；partner pool 大小不改变 pattern ticket 区间；拒绝采样分支可测 |
| partner | 无偏索引、不放回、共享 pool 不重复消费；partner 可在 trigger 前后；none 不消费 pool |
| branch 资格 | 只有 positive；hidden baseline、counterfactual variant、instruction-only、noise、replay 均不可交织 |
| owner word | 可实现 `A B B A B A A`；两种串行 word 拒绝；单事件与多事件可形成包裹，两个单事件不可行 |
| 时间 | 内部 order/delta/duration、calendar、resources、start uniqueness、session span/capacity 全保持 |
| fail-closed | 抽中不可行 partner 直接失败，不能换 partner、pattern、none 或 standalone |
| determinism | 同 program/seed plan 逐字节相同；换 seed 的多 witness fixture 至少存在 pattern、partner 或 owner word 差异 |
| identity | opportunity、pattern map、pair identity、timestamp/session 任一协调篡改都被 plan identity 拒绝 |
| report | disabled、selected-none、selected-pair、instruction-only exact shape；`primary_sessions=N-D`；无数据泄漏 |
| retry | provider、slot attempt 与并发完成序都不改变 frozen pair/layout；一侧耗尽不创建共同 retry |
| scale | 500000 无交织基线与 600 branch/300 pair probe 均通过，且匹配阶段无 pair matrix |

测试不得使用统计容差证明权重。概率契约由整数 ticket 穷举、注入随机整数和 fixed vector 证明。

## 15. 本地 Qwen3.5-4B 真实门

现有四槽 fixture 拆成两个 trigger slots 与两个 partner slots，使用 `no_interleaving_weight=0` 和一个权重为一的
pattern 强制形成两个 pair。仍使用真实 `Qwen3.5-4B-Q6_K.gguf`、四并发 llama-server 和真实模型推理。

必须机械断言：

- main 为 4 rows、stream 为 16 rows，LLM call 与既有四槽契约不变。
- `interleaving_opportunities=2`、`interleaved_primary_sessions=2`、`primary_sessions=2`。
- 每个 primary session 恰有两个 owner、六个事件并具有至少三个 owner runs。
- 每个共享 session 恰含一个 trigger candidate 与一个 partner candidate。
- owner 内 position、logical delta、artifact delta、duration 和资源保持。
- server request high-water 仍恰为四；usage 非零；manifest、delivery/plan digest、secret scan 全通过。

真实本地门只证明交织计划能被完整生成、判定、投影和交付链消费，不用于证明权重分布。
证据必须记录实际 llama-server build、模型 SHA-256、命令、wall time、报告统计与最终 checker 结果。

## 16. 完成门

只有同时满足以下条件才能宣称 v1.21 交付完成：

- 权威规格、CONTRACTS、split spec、manual、example 与实现双向一致，旧 timeline 两键在活跃文档和代码中为零。
- 所有新增和修改的生产函数满足项目 coverage 门，全部 spec feature case 有测试。
- 窄回归、全离线 suite、`git diff --check`、CLI help 与 design doc rebuild 通过。
- Uncle Bob review 的独立语义 mutants 全部 killed，没有 survived、invalid 或 inconclusive。
- 本地 Qwen3.5-4B 四槽真实门通过并审计最终工件；DeepSeek/z.ai 未运行时不得伪称其证据完成。
- `AGENTS.md` 与 `CLAUDE.md` byte-identical，worktree 无非本任务生成的临时输出或服务进程。

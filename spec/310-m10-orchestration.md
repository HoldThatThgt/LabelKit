## 3.10 M10 编排 orchestration

### 3.10.1 职责与边界

**做：**`ProcessWorkflow` 把 M2 记录流切批并按阶段屏障驱动普通流水线；`SequenceWorkflow` 管理有界候选
缓冲、whole-set attempt、声明序短提交、运行终态和 M11 最终提交。两者只通过同一个 `TaskExecutor` 接纳纯叶任务。
**不做：**不实现算子算法，不直接调用 LLM，不写文件（M11 职责），不在编排层建立第二套资源容量。

### 3.10.2 主循环与批生命周期

图 3-4 批生命周期时序。全局仅去重索引与统计计数器跨批存活，其余中间态随批释放。

### 3.10.3 API 与行为规格

```
@dataclass(frozen=True)
class RunServices:     # 2026-08-14 收参：运行期共享服务与运行身份的参数对象
    llm: LLMClient              # M9 客户端（用量 / 校准器读取面）
    schema_engine: SchemaEngine # M8 结构引擎（resolved_at 统计面）
    metrics: MetricsSink        # M12 计数与事件汇（含 console 旁路）
    tasks: TaskExecutor          # 全 execution domain 唯一任务执行面
    run_id: str                 # 本次运行标识
    run_started_at: datetime    # 运行起点（带时区）

class ProcessWorkflow:
    def __init__(self, cfg: ResolvedConfig, stages: list[Stage],
                 ingestor: Ingestor | None, emitter: Emitter,
                 services: RunServices): ...
    async def run(self) -> RunSummary: ...

@dataclass
class RunContext:      # 传入每个 stage.run 的上下文
    cfg: ResolvedConfig; llm: LLMClient; schema_engine: SchemaEngine
    rng: random.Random          # seed 派生: Random(f"{run.seed}:{batch_no}:{stage.name}")
    batch_no: int; metrics: MetricsSink; tasks: TaskExecutor
    task_namespace: str
```

`RunServices` 由 `labelkit.orchestration` 导出（与 `ProcessWorkflow` / `RunSummary` / `build_stages`
并列），调用方按名构造。Application 创建唯一 `TaskExecutor`；`RunServices`、全部 `RunContext`、
`GenerationServices` 与派生 context 必须保持同一对象身份。普通 `task_namespace` 由 run/batch/stage 派生，
sequence namespace 由 run/phase/slot/attempt/stage 派生；`run_id` 与 `run_started_at` 仍只经构造参数传递。

| 规格 | 定义 |
|---|---|
| 跨批存活状态 | 仅三项：① DedupIndex（scope=global 时）；② MetricsSink 计数器；③ M9 用量累计。均不含数据内容本体（哈希/签名/计数），运行结束随进程销毁。v1.8（stream 模式）封闭清单增两项：④ M2 未闭合会话缓冲（≤ `session_max_len` 条 Record 元数据，图像仍懒加载，3.2.8）；⑤ M10 待装箱溢出会话（next-fit 唯一开口箱，见下方时序流行）——两者均属进程内存、随装箱/消费即释放，不构成新的落盘面（2.6 注记）。 |
| 尾批 | 最后一批不足 batch_size 照常处理；批内仅 1 条时 M4 不发裁决调用，各 criterion score 固定 0.5（3.4.3 归一化行）。 |
| 熔断 | MetricsSink 维护连续致命计数（ProviderFatalError 与重试耗尽 provider_retryable_exhausted 均计入，7.6），达 `run.fatal_error_threshold`（默认 20），或 401/403 认证类首错**立即**（v1.5，3.9.3；v1.6 密钥池下「认证首错」= 该 profile 最后一把存活密钥被认证禁用——池内尚有存活密钥时单密钥认证失败仅禁用该密钥、不计入熔断）⇒ 取消在飞任务、finalize。**熔断交付（v1.6，1.6 对齐决策 ②）**：已完成批的主输出与 rejects 照常 fsync + 原子改名交付（v1.5 及以前为「.part 不交付」——长跑末段配额死亡不再丢弃全部已完成产出），报告写 run.circuit_broken=true 与 run.partial_delivery=true、counts 增列 unprocessed（6.4），退出码 4 不变。「运行完整处理了全部输入」的判定信号由此从「目标文件名出现」改为「report.run.interrupted=false 且 circuit_broken=false」——退出码 0/1 不足以判定：被 SIGINT 优雅中断的运行同样交付且以 0 退出（本表中断行）（3.11.2 主输出行、3.11.3 ④、6.4）。 |
| 中断（SIGINT/SIGTERM） | 停止取新批 → 等待当前批完成或 30s 超时取消 → finalize（报告标记 `interrupted=true`）。已 flush 的输出行有效。 |
| --limit N | M2 流截断在前 N 条记录，其余全流程不变（试跑）；generate_only 模式下作用于生成样本流的前 N 条：仅执行预抽序前 ⌈N / generate.num_per_call⌉ 次生成调用（(llm, style) 预抽不受影响），产出再截断到 N 条——本表下行「执行全部生成调用」带 --limit 时按此截断。 |
| 纯生成模式（v1.4） | `run.mode="generate_only"` 时跳过 M2（IngestReport 全零）：启动后先按 3.6.2 的量公式执行全部生成调用（并发受相应 ResourceKey 的 admission capacity 限制，(llm, style) 组合按调用序号预抽保证可复现——生成先于切批、尚无批号，预抽 PRNG 固定取 Random(f"{run.seed}:0:generate")，即 3.10.3 派生式中 batch_no 恒取 0），产出构造为 Record 后按 `run.batch_size` 切批，逐批走 M3→M4→M5→M7→M11，批生命周期与内存释放同 process 模式；不触发二次生成（单遍）。规模建议同 2.6（≤ 50 万条）。 |
| 分类与扇出（v1.7） | 规范链序 `_CHAIN_ORDER = ("dedup", "classify", "quality", "generate", "annotate", "verify")`（v1.8 起扩展为含 segment/extract、v1.9 起含 stitch 的九名单一超集元组，见下方时序流行——三者关闭时逐字节退化回本六名形）；`_compose_chain` 的 enabled 表增 classify——主链、生成回流链、generate_only 链均含（回流子批带 `source="inherited"` 继承分类，经 M13 幂等跳过，零额外调用，3.13.4）。multi 扇出只改变批内信封基数、不改链结构（4.3 契约 ②a）：`counts.fanout` = classify 阶段执行前后 `len(batch)` 的差值，由 M10 在批链循环处计量（counts.* 所有权属 M10，与从 generate 返回值计 generated 同构）；`batch.end` 事件 payload 增 `fanout` 字段（7.2 只增；`batch.start.size` 语义 = 批入口信封数，即扇出前基数）；熔断交付的 unprocessed 残差公式右侧同步 `+ fanout`（6.4 不变量扩展）。`--dry-run` 估算（`_estimate`）增 `classify_calls`：process 模式 = ingested × max(1, self_consistency)，generate_only 模式 = 生成记录数 × max(1, self_consistency)（回流子批继承分类、不计入）；存在 `[class.*]` 覆盖或 `assignment="multi"` 时，quality/annotate/verify 估算按全局继承配置、multi 按标签乘数 1 报下界，并在 stderr 注明口径——注记逐字为 `dry-run: note: estimated with global config / multi reports a lower bound at label multiplier 1`（1.6 v1.7 对齐决策 ⑦）。 |
| 时序流与整会话装箱（v1.8，仅 `segment.enabled = true`） | **链序**：`_CHAIN_ORDER = ("segment", "stitch", "dedup", "classify", "extract", "quality", "generate", "annotate", "verify")`——**九名单一超集元组**（stitch 为 v1.9 增位，`_compose_chain` 的 enabled 映射同步增 `"stitch": cfg.stitch.enabled`）；segment/stitch/extract 默认关，三者关闭时有效链逐字节退化为 v1.7 六名链序（generate 与 stream 互斥（2.3.1），故 generate 顺位与三新工位永不同链）。**装箱（S21）**：M10 改消费 `ingestor.sessions()` 会话流视图（3.2.8）而非 `records()`，按 **next-fit（顺序装箱，仅一只开口箱）**整会话装箱——会话按到达序装入，装不下即封批开新箱；批容量 = `run.batch_size` **帧**。单会话 > batch_size ⇒ M10 **硬切** + WARN 一次 + 对切分会话的帧信封打 duck-typed `session_split` 标（M7 缺帧判定的降级依据与 `_meta.stream.session_split`，3.7.3/6.3）；M1 对 `stream.session_max_len > run.batch_size` 发静态 warning（3.1.4）。待装箱溢出会话是唯一新增跨批存活项（本表跨批存活行 ⑤，装箱即释放）。**session_id 盖章（S4）**：M10 构造帧信封时盖章 `PipelineItem.session_id`（簿记非业务逻辑，4.1；M14 对其追加的 episode 信封盖章）。**计量与记账**：`counts.episodes` = segment 阶段执行前后 `len(batch)` 差值（fanout 同构计量，M10 属主——M14 不碰 `counts.*`）；post-emit 状态 tally 增 absorbed / dropped_noise；failed 兜底公式扩展为 `failed = max(len(batch) − emitted − dropped_dup − dropped_lowq − dropped_verify − absorbed − dropped_noise, 0)`（不扩展则 absorbed 成员被误计 failed）。`batch.end` 事件 payload 增 `episodes` / `absorbed` / `dropped_noise` 三可选键（仅 segment 启用时携带，fanout 的 R20 形制，7.2 只增）；stderr 进度/摘要行**不增键**（fanout 先例——报表与 batch.end 可见）。**守恒与中断（S18）**：守恒式全展开形见 6.4（左侧新增 dropped_noise 与 absorbed、右侧新增 episodes）；stream 模式下 `counts.unprocessed` 的出现条件扩为「熔断 **∨** interrupted」——SIGINT 叠加会话缓冲会产生未走完流水线的在飞残差，残差公式两侧同步扩展（右侧 `+ episodes`、左侧 `+ absorbed + dropped_noise`）；非 stream 中断残差恒 0、不加键（回归锚不动）。**dry-run 估算（S22/S23；v1.11/V12 修订）**：`_estimate` 增 `segment_calls = Σ ceil((L−1)/(w_min−1))`（对长度 L ≥ 2 的会话求和；L = 1 或 `strategy="rules"` 计 0；**window 实参自 v1.11 起替换为 `budget.min_window(cfg)` 导出的最坏保证装填量 w_min**——**上界语义**：实际每窗装填 ≥ w_min 帧 ⇒ 实际窗数 ≤ 估算，与 M1 预算护栏共用单一事实源；预算未声明时 w_min = window，公式与数值与 v1.8 同构不变）与 `extract_calls = Σ(L−1)`（报**上界**）；quality/annotate/verify 估算以 episodes ≈ sessions 报**下界** + stderr 注明口径（R28 式；注记逐字为 `dry-run: note: stream estimate: downstream reports a lower bound at episodes≈sessions (LLM refinement only adds segments)`），stream stderr 注行在 **w_min < window** 时增补一句 `; segment reports an upper bound at worst-case budget packing`（v1.11/V12）；批数由会话尺寸空跑 next-fit 装箱**精确**得出（文本模态行数统计与会话空跑单遍融合，3.2.8）；两新键**无条件打印** `segment_calls=… extract_calls=…`（classify 先例：默认关闭恒 0）。 |
| 线索缝合（v1.9，仅 `stitch.enabled = true`） | **计量（T7）**：`counts.stitched` 由 post-emit 状态 tally 归集（壳终态；仅计被并 episode 信封壳——救援短段无信封形态、不产生壳，3.16.6）；`counts.threads` 在 post-emit tally 处以恒等式 **`threads = episodes − stitched`** 导出（单点上报，不设第二落点——救援只并入不开新线索、壳一对一抵扣、降格段照常入池、fanout 后置无交互，恒等经审计验算；counts.* 属主仍归 M10，M16 不碰）。**公式三处同步（T7）**：failed 兜底公式终态减项同步增 `− stitched`（`failed = max(len(batch) − emitted − dropped_dup − dropped_lowq − dropped_verify − absorbed − dropped_noise − stitched, 0)`——不扩展则壳被误计 failed）；熔断/中断的 unprocessed 残差公式减项同步增 `− stitched`；守恒全式左侧增 `stitched` 项（6.4）。**batch.end**：payload 增 `stitched` / `threads` 两可选键（仅 stitch 启用时携带，episodes 的 R20 形制，7.2 只增）；stderr 进度/摘要行**不增键**（固定键集，stitched 经报表与 batch.end 可见——有意为之；v1.10 U18：固定键集约束收窄为 plain 面专属，rich 面板状态账展示 stitched/threads，7.7）。**report**：`stream` 节增 `stitch` 子块 `{stitched, rescued_short, seams, judgments, repass_judgments, failures}`（M16 属主，6.4）。**dry-run 估算（T16）**：`_estimate` 增 `stitch_calls = len(session_lens) × votes × (2 若 repass 否则 1)`（episodes ≈ sessions 下界基数，沿用既有 stderr 下界注；救援候选调用不计入估算——池非空才发生的 +ε 项）；该行**无条件打印** `stitch_calls=…`（off 时恒 0，segment_calls 先例）。 |
| console 旁路（v1.10） | **stage 信号**：批链循环内每 stage `run()` 之前调 `metrics.stage_begin(stage.name, batch_no)`——进程内旁路仅转发 ProgressListener，不产生 TraceEvent、不入 7.2 目录（3.12.3/U11）；`_request_stop` 内加一行 `metrics.stop_requested()`（中断横幅通路）。**估算导出（U20）**：静态估算公式抽出为纯函数 `estimate_run(cfg, plan)`（`_estimate()` 改薄封装；dry-run 与渲染器批级分母共用）；live 路径在 P2-4 预扫后经 `metrics.run_estimate(...)` 发送——process 模式**复用该次 scan**（UI 模态翻 `estimate=True`，配对表零额外 I/O；文本模态仅 `console.estimate = true` 时做行数估算，U17），**禁二次 scan**；generate_only 走 3.6.2 静态公式无 scan。**dry-run 呈现（U13；v1.11/V12 修订）**：rich 档下估算四行 print 让位于渲染器表格（数值逐项一致）；plain 档行式输出为逐字节锚——`segment_calls`/`stitch_calls` 维持**无条件打印**，其中 `segment_calls` 行的含义自 v1.11 起改为**按 w_min 报预算最坏装填上界**（本表时序流行；预算未声明或 w_min ≥ window 时数值与 v1.10 逐字节不变——examples 声明保守实效窗下当时的五个黄金文件不动，V26；v1.12 起黄金文件为**七个**且因估算行插入两帧粒度键全部重采，见本表帧粒度行）。listener = None 时以上全部为 no-op（v1.9 行为逐字节一致）。 |
| 上下文预算（v1.11，仅预算启用时） | **批边界校准冻结（V19）**：每批处理完成、下一批装填开始前调用 `self.llm.calibrator.freeze_batch()`——聚合本批图片成本样本的 max（对无序集取 max，序无关）压入批最大值窗口、刷新可读快照（第 N 批装填只读 < N 批的聚合值，确定性护栏——批序串行 ⇒ 同输入同配置可复现；校准器由 `LLMClient` 自持，公开面 `llm.calibrator`，3.9）。**启动期预算 INFO 行（V13①）**：M10 于运行起点打印预算参数（如 `segment: w_min=6 window=20 (budget)`——数据无关、仅计数与参数；归属 M10 启动段而非 loader——加载期 logging 尚未按 CLI 覆盖定级，7.1）。**报表汇总**：finalize 时组装 `report.budget = {profiles, w_min, truncations, overflow_records, image_cost, degrade_retries, escalations}`（counts-only 键义见 6.4；truncations 由各算子逐裁剪点计数、overflow_records 按 7.6 词表归集、image_cost/degrade_retries/escalations 为 V17 三层的校准终值与反应频度对账，V13②⑤）+ `report.stream.windows`（segment 实际窗数，M14 属主计数、随 stream 节落盘——供用户对账 V12 上界估算，V13④，6.4）。 |
| 帧粒度（v1.12） | **组链或门（裁决·组链双门）**：factory（`build_stages`）以**或门** `classify.enabled ∨ frame_classify.enabled` 决定 ClassifyStage 进链（链序与槽位不变——仅帧级开启时 ClassifyStage 仍须进链执行帧 pass；组链的 classify 槽位判定与该或门同口径）；stage 内序列级判决单独受 `classify.enabled` 门控——仅帧级开启时序列记录不产生 Classification、`_meta.classification` 维持 null（3.13.7）。**estimate_run 两键**：`frame_classify_calls` / `frame_annotate_calls` = **粗上界 = 预扫描帧总数 Σ session_lens**（数据源与 `segment_calls` 完全同源，复用同一次预扫描；帧分类实际按窗批量、帧标注跳过噪声成员与跳过类，实际调用数均 ≤ 帧总数）；对应开关关闭 ⇒ 0（帧粒度要求流模式（3.1.4），非流分支恒 0）；`total_calls` 扩项；**键序冻结**——`frame_classify_calls` 紧跟 `classify_calls`、`frame_annotate_calls` 紧跟 `annotate_calls`（返回键表冻结注释同步，CONTRACTS §7.13）。**dry-run 估算行改写**：估算行（stderr 第 2 行）按冻结键序插入两键，**无条件打印**（非流工程恒 = 0，v1.9 `stitch_calls` 先例）——是**改第 2 行**而非加行：五个既有 dry-run golden 重采 + `examples/mix` 主/姊妹双工程的 `dryrun-mix.txt` / `dryrun-mix-text.txt` 两个新 golden，共**七个**（7.8 回归锚，`tests/cli/goldens/`）。 |
| v1.20 sequence 精确交付 | `generate.form = "sequence"` 时，generate_only 分支在 M1 冻结 `SequenceGenerationConfig` 后调用唯一 `compile_generation_program` 与 `compile_scenario_plan`，再由 `SequenceWorkflow` 调用 `deliver_generation`；不进入 `GenerateStage` 或 `ProcessWorkflow._process_batch`。M10 负责候选缓冲、attempt transaction、声明序短提交、时间/区间 frontier、运行终态和 M11 commit，不实现 compiler/planner 算法。classify 与 frame.classify 判定 stage 静态关闭，projector 写 inherited Classification；frame.annotate 由 attempt-local 协作者执行。 |

图 3-6 sequence slot attempt 与交付状态机。多个 slot 可以同时处于 attempt、生成、评估、下游或已准备状态；
只有声明序 head 能进入最终重验证与 commit，recoverable rejection 在原 slot 启动下一 attempt。

#### v1.20 sequence 候选缓冲

primary 与 noise 各有一个阶段。每个阶段的候选缓冲容量等于该阶段引用的不同 `ResourceKey` 容量之和，
再钳制到剩余 slot 数。候选缓冲始终是从 `next_commit` 开始的连续声明序区间：

~~~text
[next_commit, min(total_slots, next_commit + candidate_buffer_capacity))
~~~

区间内每个 ordinal 恰有一个 running、`PreparedCandidate` / `PreparedNoiseCandidate` 或
`RecoverableOutcome` 占位。创建 slot coordinator 前先取得候选缓冲 permit；permit 跨 attempt、preparing、
prepared、recoverable outcome 与等待提交全程保留，只在该 ordinal 成功提交或运行终止清理后释放。只有 head
成功提交后窗口才右移并接纳一个新 tail。高 ordinal 提前完成或失败都继续占位，不得因为叶任务已经结束而接纳
窗口外 tail；head retry 在原 ordinal 替换占位。

每个 slot coordinator 同一时刻只运行一个 attempt。primary attempt 的昂贵路径为：

~~~text
generation / evaluation
→ projection / witness
→ DedupIndex.group_reserve
→ pointwise QualityStage.run_attempt
→ AnnotateStage.run_attempt
→ VerifyStage.run_attempt
→ SequenceDeliveryEmitter.assemble_sequence
→ ReplayProjector
→ PrimaryCandidateReconcileRequest
→ PreparedCandidate
~~~

同一 branch 的事件按 `state_after` 依赖串行；declared baseline 完成后，不同 counterfactual suffix 可以并发，
结果仍按 variant 声明序归并。quality → annotate → verify 的业务屏障不变；前一 gate 拒绝后不支付后一 gate。
不同 attempt 使用不同 `PipelineItem`。叶调用只返回冻结 outcome，operator 按
slot ordinal、attempt index、stage declaration order 与叶任务 ordinal 归并后才修改 attempt-local item。

`AttemptTransaction.items` 是当次 attempt 的唯一可变 `PipelineItem` 真值。`DownstreamAttemptResult` 只返回
`accepted`、`rejected_stage` 与 dataset counter delta。局部 delta 只在成功 dedup commit 后合并；失败时与
transaction 一起丢弃。LLM usage、latency、provider retry、成本、SchemaEngine resolved-at 与 trace event 是已经
发生的运行事实，不回滚。

`group_reserve` 之后出现 downstream 或 candidate-local recoverable failure 时，coordinator 把冻结
`RecoverableOutcome` 与 reservation 放入原缓冲位置，不立即记录 rejection 或启动重试。当该 ordinal 成为 head
时先 `group_revalidate`；若最新正式前缀产生 duplicate，则记录 dedup rejection，否则才记录已保存的 downstream
或 reconcile rejection。两条路径都在同一无 `await` 区域 discard reservation，再按 attempt ledger 决定原位重试。
这冻结了一个候选同时存在 dedup 与后续失败时的 dedup 优先级。

`PrimaryCandidateReconcileRequest` 只携带当前 `DeliverySlot`、variant-aligned witnesses、严格 variant 顺序的
`SequenceRows`、计划中该 source 的全部 `ReplayLayout`、按 layout 顺序的 `ReplayRows` 和候选实际 retained
bytes；它不读取 `DeliveryState` 前缀。local validator 验证 payload/base event/generation witness、ID、owner、
role、frame、actor、payload time、duration/resources/descriptor、rebound replay、constant shift、候选内 event-start
唯一、resource interval 互斥和 canonical bytes，并要求 variant 与 replay
闭包不多不少。

local validator 通过后创建唯一深度冻结 `PreparedCandidate`。carrier 闭包 slot/attempt identity、witnesses、
`SequenceRows`、全部 `ReplayRows`、`DedupReservation`、已验证 dataset counter delta、实际 retained bytes
与 candidate digest。递归冻结并把 reservation 所有权转移给候选缓冲后，立即释放 `AttemptTransaction`、
`PipelineItem` 与投影中间对象。任何代码不得再修改 carrier；提交时只校验 frozen digest，不再做第二次完整
candidate-local 扫描。

#### 声明序短提交

唯一提交协调器只消费当前 `next_commit`。`CrossViewFrontier` 保存当前 phase 的 next ordinal，以及全部已提交
primary、replay 与 noise 的 event ID、timestamp、source key 和资源区间；primary 切换到 noise phase 时集合不清空。
primary head 的完整顺序为：

~~~text
DedupIndex.group_revalidate
→ frozen candidate digest validation
→ CrossViewFrontier.check_primary → CrossViewDelta
→ retained-content check
→ prevalidate dataset counters and DeliveryState delta
→ DedupIndex.group_commit
→ CrossViewFrontier.commit(delta)
→ counters / rows / sources / replays / retained commit
~~~

整个临界区无 `await`。`CrossViewFrontier.check_primary` 只检查当前候选与已提交前缀，并返回冻结且尚未应用的
`CrossViewDelta`；delta 同时冻结 event IDs、timestamps、source keys 和排序后的 `ResourceInterval`。`commit(delta)`
无普通失败分支。工作量只与当前候选行数有关。dedup 重验证先于 CrossView 和
retained-content，因此冲突优先级与原
声明序 first-writer 一致。`group_commit` 后的状态交换不得再有普通可恢复失败分支。commit-time dedup、
CrossView 或 retained-content rejection 只让当前 head 原位重建下一 attempt；已准备的更高候选继续保留，
轮到时再对最新正式状态重验证。

attempt 只在 head 被判定为本地 recoverable rejection、commit-time rejection 或成功 commit 时恰消费一次。
高槽的 speculative outcome 因低槽耗尽、fatal 或 cancellation 被丢弃时，不进入 slot attempts 或 rejection
bucket。provider fatal、circuit、internal 与 cancellation 不消耗 attempt。高槽 recoverable failure 到达时只保留
outcome；只有轮到该 ordinal 才记账并启动下一 attempt。

当前 slot 耗尽时停止接纳，取消并等待所有更高 coordinator，discard 全部 reservation，并丢弃其 dataset delta 与
候选。它们已经发生的 usage、retry、Schema、trace 与 provider latency 仍保留为运行事实。并发 fatal 取消所有
sibling coordinator 与叶任务，cleanup 完成后按
`(phase declaration order, slot ordinal, attempt index, leaf declaration key)` 选择最小原始异常。

failed report 在全部 cleanup 后冻结。exhaustion 的当前槽恰记录 `max_slot_attempts`；fatal 使用稳定选择的 slot
身份，`attempts_used` 是该槽此前已消费的 recoverable attempts。外部 cancellation 与最终 full CrossView 内部错误
使用 `failed_slot = null`、`attempts_used = 0`。

#### Noise、replay 与最终对账

全部 primary 内存提交后，NoiseSlot 使用同样的并发准备、连续候选缓冲与声明序提交模型。NoiseRenderer 与独立
semantic evaluator 可跨 noise slot 并发。`PreparedNoiseCandidate` 闭包 `NoiseSlot`、post-gate payload digest、
最终 row、similarity signature、dataset counter delta、实际 retained bytes 与 frozen digest。进入候选缓冲前，
`NoiseCandidateReconcileRequest` 验证 payload、topic/ordinal、timestamp、ID 派生、字段闭包和 canonical bytes。

noise head 的完整无 `await` 顺序为：

~~~text
SimilarityFilter.probe(latest primary + lower noise)
→ frozen noise digest validation
→ CrossViewFrontier.check_noise → CrossViewDelta
→ retained-content check
→ prevalidate frontier and DeliveryState delta
→ SimilarityFilter.commit
→ CrossViewFrontier.commit(delta)
→ noise rows / digest / retained commit
~~~

SimilarityFilter 正式突变后不得再有普通 rejection。高 ordinal 提前计算的 signature 不能直接 commit；相似度冲突
只拒绝当前 noise attempt。Replay 不调用 LLM，也不进入独立 coordinator；它只从 source slot 最终成功的
`SequenceRows.primary_stream_rows` 派生，按 ReplayLayout constant shift 机械重绑 payload 时间，并随 source 候选执行
local 校验和声明序提交。primary 与它的全部 replay 只产生一个 checked `CrossViewDelta`；replay 构造、Schema、binding、
frontier 或 retained 任一失败都发生在 `group_commit` 前，source primary 与 replay 计数/rows/bytes 全部回滚。

全部 primary、noise 与 replay 内存提交后，`reconcile_views` 从 program、plan、main 与 stream 独立重建全部事实，
按 resource sort/sweep 复验全局 interval，并执行一次。任一
candidate-specific mismatch 必须已在 local 或 frontier 阶段成为当前 attempt 的 `reconcile` rejection；最终 full
reconcile 失败是运行级内部错误，exit 4，不消费 attempt，不打开输出，也不包装成 `GenerationAttemptRejected`。

~~~python
async def deliver_generation(
    request: DeliveryRequest,
    services: DeliveryServices,
) -> GenerationProduct:
    """精确交付全部 sequence/noise slot，并返回一次性成功产品。"""
~~~

`GenerationServices(config, schema_engine, llm, metrics, tasks)` 是唯一运行服务根；
`DeliveryServices(generation, dedup, quality, annotate, verify, emitter)` 不复制 `RunContext`。
SequenceWorkflow 为 dedup 派生的 context 直接复用 source cfg；为 Quality、Annotate 与 Verify 派生的
attempt-local cfg 必须把 `class_views`、`frame_class_views`、`frame_schema` 替换为
`GenerationProgram.class_views`、`GenerationProgram.frame_classes`、`GenerationProgram.frame_schema`，正常、
frame 与 repair 路径都不得再读 source cfg 的同名视图或 Schema。schema engine、LLM client 与 metrics 仍与
`GenerationServices` 对应对象身份相同，tasks 保持同一对象身份；只新建按 slot 派生的 rng、task namespace 与固定
batch number。`DeliveryRequest` 只携带
program、plan、paths、run attempt ID 与 run ID，不复制 config 或 materialized credentials。

#### attempt 与运行终态

下表只适用于 `labelkit run` 的 sequence 形态。`validate` 与 `run --dry-run` 即使发现 plan 失败，
也不写 main、stream、success report、manifest 或 failed report，只按同一 error kind 返回退出码。

| 事件 | 消耗 attempt | 重试 slot | 退出码 | 正式成功路径 | failed report |
|---|---|---|---:|---|---|
| Schema、state、pattern、coupling、semantic 失败 | 是 | 是 | 耗尽为 1 | commit 前不替换 | 耗尽时原子写 |
| dedup、quality、annotate、verify、reconcile、内存预算拒绝 | 是 | 是 | 耗尽为 1 | commit 前不替换 | 耗尽时原子写 |
| output truncated、可恢复 context overflow、provider retryable exhausted | 是 | 是 | 耗尽为 1 | commit 前不替换 | 耗尽时原子写 |
| provider fatal、auth pool exhausted、circuit trip | 否 | 否 | 4 | commit 前不替换 | 已解析路径时 best-effort 原子写 |
| SIGINT、KeyboardInterrupt、CancelledError | 否 | 否 | 4 | commit 前不替换 | run 已初始化时 best-effort 原子写 |
| 最终 full CrossView 内部错误 | 否 | 否 | 4 | commit 前不替换 | `failed_slot=null`、`attempts_used=0` |
| 配置、hook、catalog、路径错误 | 否 | 否 | 2 | 不替换 | 不写 |
| 输出目录或 `.part` 运行期不可写 | 否 | 否 | 4 | 不替换 | 尝试同目录写；仍不可写时只记英文 stderr kind |
| planner INFEASIBLE | 否 | 否 | 2 | 不替换 | 原子写 |
| planner FEASIBLE/UNKNOWN budget 或 MODEL_INVALID | 否 | 否 | 4 | 不替换 | 原子写 |
| commit-I/O 失败 | 否 | 否 | 4 | 可能已替换 main/stream/report 子集，旧 manifest 不变 | best-effort 原子写 |
| failed-report I/O 失败 | 否 | 否 | 不改主退出码 | 不改 | stderr 记录英文 kind |

每个 attempt 的随机流由下式独立派生：

~~~python
int.from_bytes(
    sha256(
        canonical_json(
            ["labelkit:v1.20", "attempt_random", [seed, slot_identity, attempt_index, purpose]]
        ).encode("utf-8")
    ).digest(),
    "big",
)
~~~

不得使用 Python `hash()` 或自行拼接字符串。重试可以改变 LLM seed 世界、intent、patch 与措辞；
catalog 行不变。
pattern、variant、role/position、全部计划时间、duration、resources、containment、session、noise source、
replay source 与 replay shift 永不变化。普通内容/结构拒绝消耗 attempt；四类 run-terminal 异常必须原样穿透，
不消耗 attempt。
任一 slot 耗尽立即停止接纳并抛 `DeliveryError(sequence_delivery_exhausted)`；不交付已接受前缀。

sequence 不使用 process/flat 的 partial-delivery 或优雅中断提交；`run.partial_delivery` 的有效策略恒为 false。
失败只写 counts-only `failed_report`，不打开 main、stream、success report、manifest 或 rejects。成功要求全部
primary/noise/replay 和最终 `reconcile_views` 通过，再由 M11 `prepare_product` 生成带单一
delivery digest 的 `GenerationProduct(main_rows, stream_rows, report)`，最后调用 commit 提交正式文件。

#### estimate 与观测

sequence 调用键按顺序为 scenario_seed、baseline_event_plan、variant_event_plan、frame_render、
semantic_evaluation、noise_render、noise_evaluation、quality、annotate、frame_annotate、verify。
catalog seed 调用为零，protected prefix 复用 `EventDraft` 语义字段，不重复计 plan/render。dry-run 从同一 GenerationProgram、
block allocator 与 ScenarioPlan 计算一个成功 attempt 的逻辑下界和
`max_slot_attempts` 上界，不含 M9 provider retry；不能另建近似 planner。

report 的唯一块是 `report.generate.sequence`，包含 run/program/plan/delivery identity、planned/delivered set
与 sequence、event/session/noise/replay 精确数量、调用族、按 pattern 交付计数和冻结 rejected_attempts 闭集。
任何 attempt 只记最终停止边界一个 rejection bucket。旧的流配额、档位、概要/逐位实现、幸存/shortfall 与
partial delivery 统计均不出现。

report 的 `runtime` 块记录 `queue_high_water`、`running_high_water`、`resource_wait_high_water`、
`commit_waiting_high_water`、`candidate_bytes_high_water`、`cancelled_tasks`、`resource_wait_ms`、
`http_pool_wait_ms` 与 `commit_ms`。`candidate_bytes_high_water` 是全部已完成但尚未提交候选 canonical bytes
同时驻留总和的峰值，不是单候选最大值，也不声称覆盖在途 provider response、Python 对象开销、dedup registry
或 HTTP buffer。

普通日志只记录 slot key、attempt index、stage、error kind、profile、计数与 duration；state、patch、payload、
ActorView、prompt 和 API key 只能按 trace 内容策略处理。


**背书：**「编排器只做组合调度、算子无相互依赖」是 Data-Juicer 配方执行器 [4] 与 distilabel Pipeline 运行时 [5] 的共同架构；批式流转 + 增量写出与 Dolma toolkit 的并行分片处理模型一致 [6]。

### 3.10.4 运行走查示例

走查前提（贯穿示例·文本模态）：输入为输入法采集的中文指令 JSONL 共 1000 行、无坏行（首行 `{"instruction": "帮我写一条请假条，明天上午要去医院", "source": "ime-log", "ts": "2026-06-30T10:12:00Z"}`），`input.text_field = "instruction"`；`run.output = "./out/ime-intent-0630.jsonl"`；`run.batch_size = 256`、`run.seed = 0`；阶段为 2.3.1 的**默认组合**（`dedup.enabled` ✓、`quality.enabled` ✓、`annotate.enabled` ✓，`generate.enabled = false`、`verify.enabled = false`）。quality 取默认 `mode="pairwise"`、`rounds=4`、`criteria_per_call="all"`、`rubric="default:text"`（4 条 criteria，附录 A.1），并显式设 `quality.threshold = 0.3`；annotate 输出用户 Schema 为意图标注对象 `{intent, topic, difficulty}`（`additionalProperties:false`、三字段全 required）；`output.rejects = "refs"`（默认）、`dedup.scope = "global"`（默认）。M2 惰性流被 M10 切为 4 个批：256 / 256 / 256 / 232（尾批不足 batch_size 照常处理，3.10.3）。

| 批 | 取入→PipelineItem | M3 dropped_dup | M4 dropped_lowq | M5 failed | 写出 emitted | 批末释放的中间态 | DedupIndex 签名累计 |
|---|---|---|---|---|---|---|---|
| 1 | 256 | 18 | 21 | 3 | 214 | 256 个 PipelineItem；476 次裁决 / 1904 条比较结果 | 238 |
| 2 | 256 | 24 | 19 | 4 | 209 | 256 个 PipelineItem；464 次裁决 / 1856 条比较结果 | 470 |
| 3 | 256 | 27 | 20 | 5 | 204 | 256 个 PipelineItem；456 次裁决 / 1824 条比较结果 | 699 |
| 4（尾批） | 232 | 28 | 18 | 2 | 184 | 232 个 PipelineItem；408 次裁决 / 1632 条比较结果 | 903 |
| 合计 | 1000 | 97 | 78 | 14 | 811 | — | 903 |

口径与自洽性：每批 emitted = 取入 − dropped_dup − dropped_lowq − failed（批 1：256 − 18 − 21 − 3 = 214）。M4 比较池 N = 去重后存活数（批 1 为 238），裁决调用数 = k·⌊N/2⌋ = 4×119 = 476，`criteria_per_call="all"` 下每次调用裁决 4 条 criteria ⇒ 1904 条 criterion 级比较结果；批 3 的 N=229 为奇数，每轮末位轮空（3.4.3），故为 4×114 = 456 而非 458。进入 annotate 的记录数 = N − dropped_lowq：217 / 213 / 209 / 186（合计 825 = 811 emitted + 14 failed；14 条 failed 中 `schema_violation` 11 条、`provider_retryable_exhausted` 3 条）。批 1 quality 阶段的 `ctx.rng = Random("0:1:quality")`（3.10.3 派生式代入 run.seed=0、batch_no=1）。

批生命周期（图 3-4 的逐批实例）：每批 `emit()` 后 M11 向 `ime-intent-0630.jsonl.part` 追加 214 / 209 / 204 / 184 行并 flush，同时向 `ime-intent-0630.rejects.jsonl` 追加 42 / 47 / 52 / 48 行（= 该批 dup+lowq+failed，refs 模式每行仅 `_meta` 引用）；随后 M10 释放该批全部中间态——上表「批末释放」列的 PipelineItem 与比较结果，外加 4 组 BT log θ 数组（每 criterion 一组、长度 N）；文本模态无图像引用，释放数为 0。跨批存活的仅 3.10.3 所列三项，其规模变化：

| 跨批状态 | 批 1 → 2 → 3 → 4 末的规模 |
|---|---|
| ① DedupIndex | 签名条目 238 → 470 → 699 → 903（= 1000 − 97；每条目为 exact sha256 + 128-perm MinHash 签名，文本模态无 pHash 表）。注意被 lowq/failed 淘汰的记录也已入索引——M3 在先，first-writer-wins。 |
| ② MetricsSink 计数器 | emitted 214 → 423 → 627 → 811；dropped_dup 18 → 42 → 69 → 97；dropped_lowq 21 → 40 → 60 → 78；failed 3 → 7 → 12 → 14。仅整数计数，无数据内容。 |
| ③ M9 用量累计 | LLM 调用 693 → 1370 → 2035 → 2629（每批 = 裁决 + 标注调用，不含重试与 L3 修复）。 |

流耗尽后 `finalize()`：`.part` fsync 并原子改名为 `ime-intent-0630.jsonl`（811 行），写 `ime-intent-0630.report.json`，其 `counts` 节：

```
"counts": {"scanned": 1000, "ingested": 1000, "bad_input": 0,
           "dropped_dup": 97, "dropped_lowq": 78, "dropped_verify": 0,
           "failed": 14, "generated": 0, "emitted": 811}
```

按 6.4 不变量 `emitted + dropped_* + failed + bad_input = scanned + generated` 验算：811 + (97 + 78 + 0) + 14 + 0 = 1000 = 1000 + 0，等式成立。`run()` 返回的 `RunSummary` 与上述 counts 一致：4 批全部完成、主输出 811 行、rejects 189 行（97+78+14）、`interrupted = false`——CLI 以**退出码 0** 结束（2.4：存在被拒绝记录不影响退出码；若本次带 `--strict`，则因 189 条 rejects 返回退出码 1）。

**提示：**本走查中 dropped_lowq = 78 依赖显式设置 `quality.threshold = 0.3`；该键默认**缺省 = 不过滤只打分**（5.2），若不设阈值则 dropped_lowq = 0，903 条唯一记录将全部进入标注，emitted 相应变为 903 − failed。

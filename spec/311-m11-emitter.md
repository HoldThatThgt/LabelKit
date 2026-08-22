## 3.11 M11 输出器 emitter

### 3.11.1 职责与边界

**做：**process 与 flat 继续按批写主输出、rejects、sidecar 和 report；sequence 在完整交付前只持有
内存产品，成功时写 main、stream、report 与最后提交的 manifest，失败时只写独立 failed report。所有路径只读
M1 冻结的 `ResolvedPaths`；组装 `_meta`、执行写前 Schema/双视图终检并维护内容摘要。

**不做：**不修改 annotation、generation truth、payload、事件时间或 replay；不解释 pattern/state/evaluator
业务语义；不为 sequence 写已接受前缀、rejects、sidecar 或无 manifest 的“成功”数据。

### 3.11.2 写出规格

| 通道 | 规格 |
|---|---|
| process / flat 主输出 | 运行期写同目录 `.part` 并逐批 flush；finalize 时 fsync + 原子 replace。只写 active 且通过最终 Schema 的记录。absorbed/stitched 只计数，其他非 active 状态按 rejects 策略路由；既有熔断部分交付和中断语义保持不变。 |
| process stream 元数据 | segment/stitch/frame 开关决定既有 `_meta.stream`、members、fragment、defect 形态；成员标注写前逐项 `validate_only`。该面不承担 sequence 生成真值。 |
| process / flat rejects | `none` / `refs` / `full` 三档保持既有语义；refs 只写引用与 value-free error，full 是用户显式的数据内容调试面。sequence 配置强制 none 且从不打开该通道。 |
| process / flat report | `<output-stem>.report.json`；聚合运行参数摘要、计数、质量、去重、Schema、usage、预算与耗时，不含业务数据。 |
| sequence 内存阶段 | M6 先产生只服务于 dedup/downstream 的 `ProjectedSequence`；M11 再以最终 `PipelineItem` 组装 `SequenceRows`，replay 只从 source 的最终 primary rows 派生。`PrimaryCandidateReconcileRequest` 通过后，`PreparedCandidate` 深度冻结当前 slot 的全部 sequence/replay rows 与 byte 证据。在全部 primary/noise/replay、`CrossViewFrontier` 和最终 `reconcile_views` 通过前，不打开 main、stream、success report 或 manifest；失败 attempt 同样不打开正式数据通道。M11 Schema 终检失败回滚整个当前 set attempt。main/stream/replay 共享冻结 payload 引用。 |
| sequence main | 只含 primary sequence，每个 Record 在写前按 inherited sequence class 选择 `GenerationProgram.class_views` 中已物化的生效用户 Schema；classify 关闭不影响 ClassView 路由。declared 与 instruction-only 的 generation truth 按第 6 章写入。 |
| sequence stream | 顶层固定为 `payload` 与 `_meta`。primary row 与 main owner 双向一致；noise 无 owner/role/pattern/variant；replay 是 whole-positive-sequence 同源投影，使用新 ID 与时间但逐位 payload、frame class、actual role 和顺序同源。 |
| sequence success report | `report.generate.sequence` 只含身份、精确计划/交付计数、调用族、按 pattern 计数、usage 与冻结 rejection buckets；不含 state、patch、payload、prompt 或已交付数据前缀。 |
| sequence manifest | main、stream、report 都写同目录 `.part`，flush + fsync 后依次 `os.replace(main, stream, report)`；manifest 最后单独写、fsync、replace。manifest 是唯一成功提交真值，包含 schema_version、run_id、delivery_digest、artifacts_committed、三个 artifact 的绝对路径/sha256/rows 与 committed_at。 |
| sequence failed report | `<output-stem>.failed.report.json` 经同目录 `.part` 原子替换，只含 run_attempt_id、nullable run_id、artifacts_committed=false、failed_slot、attempts_used、terminal_error_kind、usage 与 rejection counts。它是最近一次失败诊断，不属于 manifest；即使 run_id 相同，也不能否定一个摘要有效的成功 manifest。 |
| commit-I/O 失败 | main/stream/report 顺序 replace 可能留下新旧混合固定路径；旧 manifest 必须保持不变。消费者以 manifest 摘要核验并拒绝不匹配组合。failed-report 写失败不覆盖主异常；只有没有主异常时映射 exit 4。 |
| stderr / console | 只打印进度、计数与 value-free 错误；rich/plain 仍共用 console_format。sequence 不打印 prompt、state、patch、payload、ActorView 或 key。 |

`SequenceDeliveryEmitter` 构造器只绑定 `ResolvedPaths`，使 planner failure 在运行服务构造前仍可写独立 failed
report。`SequenceDeliveryEmitter.assemble_sequence(request)` 是纯内存、零 I/O 入口；唯一参数
`SequenceAssemblyRequest(program, schema_engine, item, projection, batch_no)` 闭包冻结 program、共享 M8、最终
attempt-local item、对应投影与批号。入口复用普通 emitter 的用户对象、scores、sequence/frame annotation 与
verification 装配规则，返回 `SequenceRows(main_row, primary_stream_rows, retained_content_bytes)`。`batch_no` 固定为
从一开始的 slot declaration ordinal，重试不变。`ProjectedSequence.main_record` 只是 dedup/downstream 输入，不得
用它组装或计费最终 main row。

M11 必须在计费前终检实际待写对象。开启 sequence annotation 时，移除 main 顶层 `_meta` 后的用户对象以 inherited
sequence class 选择的 `GenerationProgram.class_views[label].schema` 显式调用
`SchemaEngine.validate_only(..., schema=...)`；该 Schema 已由 compiler 物化为类覆盖或全局用户 Schema。每个 frame
成员还要用 `GenerationProgram.frame_classes` 判断是否应有标注，并把 main member 与 primary row 的两份最终标注都
显式按 `GenerationProgram.frame_schema` 验证。`FrameClassView.gen_schema` 只约束生成 payload，不是帧标注 Schema。
M11 不读取 source `ResolvedConfig` 的 class/frame views 或 Schema，也不以 `schema=None` 触发 M8 默认 Schema。
缺失或未知 program Schema 是 `generation_downstream_contract`；实际最终标注违规是
`sequence_projection_mismatch`，只记录 record ID、检查面和违规数，不记录数据或违规正文。

最终标注违规在 AnnotateStage 接受之后发生，因此归 report 的 `reconcile` rejection bucket，而不是 `annotate`。
SequenceWorkflow 拒绝并重试整个当前 counterfactual set attempt；所有 variant item、`DedupReservation`、
dataset delta、`SequenceRows` 和 replay 一起回滚，已发生的 usage、retry、SchemaEngine 与 trace 运行事实仍累计。

ReplayProjector 在 `assemble_sequence` 之后才从 source `SequenceRows.primary_stream_rows` 深拷贝并机械
替换 replay 身份与工件时间，因而 payload、frame annotation 与其他下游元数据逐位保留。
`SequenceRows.retained_content_bytes` 只计其 main row 与 primary rows；`ReplayRows.retained_content_bytes`
只计 replay rows。两者共用 `canonical_delivery_row`，每行计 `len(canonical_row_bytes) + 1`，
其中一 byte 是 JSONL 换行。`PrimaryCandidateReconcileRequest.replays` 保留 ReplayLayout 顺序的
`ReplayRows` 分组，并携带当前 candidate 的实际 retained bytes。candidate-local validator 必须直接从实际
canonical rows 独立复算每个 `SequenceRows`、每个 `ReplayRows` 与 candidate 总费用；不能信任或只相加
carrier 已提供的 byte 字段，也不能读取已提交 `DeliveryState` 前缀。

candidate-local 通过后，`PreparedCandidate` 按 variant 与 layout 声明序深度冻结最终 rows、witnesses、
`DedupReservation`、dataset counter delta、实际 retained bytes 与 candidate digest。进入候选缓冲后，
`AttemptTransaction`、`PipelineItem` 与投影中间对象立即释放；提交临界区只验证 frozen digest，不重新执行
完整 candidate-local 扫描。

noise 使用独立 `NoiseCandidateReconcileRequest`，从最终 row 重验 payload digest、topic/ordinal、timestamp、
event ID、字段闭包与 canonical bytes。通过后 `PreparedNoiseCandidate` 深度冻结 `NoiseSlot`、row、similarity
signature、dataset counter delta、实际 retained bytes 与 frozen digest。noise carrier 不复用 primary carrier，也不把
高 ordinal signature 提前写入 `SimilarityFilter`。

`SequenceDeliveryEmitter.prepare_product(main_rows, stream_rows, report)` 是 `delivery_digest` 的唯一属主。
摘要使用完整 64-hex SHA-256：先写固定 ASCII header `labelkit:v1.18:delivery\n`，再按 main、
stream 视图顺序及各自行序写入 `len(canonical_row_bytes)` 的十进制 ASCII、冒号与行字节。
`canonical_delivery_row` 只移除 `_meta.run.started_at`、`finished_at`、`duration_ms` 和 manifest
`committed_at` 等发射期墙钟观测字段；annotation、generation truth、payload、事件时间与 replay
证据都纳入。`prepare_product` 只计算一次，把摘要写入 report 深拷贝，并返回
`GenerationProduct(main_rows, stream_rows, report)`。产品不另存 digest 或 manifest input。
正式文件序列化与上述 canonical 材料严格分离：main/stream/report/manifest 按内存对象声明序写出，
从而保留 stream 的 `payload` 后 `_meta`、sequence report 与 manifest 的冻结键序；不得为写文件复用
`canonical_delivery_row` 的 `sort_keys = true`。
所有 slot/noise/replay 交付完成与 report 计数在进入 `commit` 前已冻结；此后的失败只是
commit-I/O run terminal，不消耗 attempt，不重试 slot，也不重新生成产品。

`commit` 只从深度冻结的 `product.report.delivery_digest` 构造 manifest，不重算摘要；缺失或
格式非法必须在打开任何 `.part` 前以 `generation_downstream_contract` 终止。摘要只写
report/manifest，不写 main/stream，也不参与 Record ID。manifest 的 `committed_at` 是其唯一新增
墙钟字段。

每个 candidate 在声明序内存提交前通过 `CrossViewFrontier.check_primary/check_noise` 增量核对；该步骤只检查
当前 rows 与已提交 event ID、timestamp、source、phase/ordinal，返回冻结 `CrossViewDelta`，不重扫完整前缀。
`commit(delta)` 无普通失败分支。全部 primary、noise 与 replay 内存提交后，
`reconcile_views` 以最终 `SequenceRows`、noise rows 与 `ReplayRows` 独立做一次完整双向核对，并重算全量
canonical row 字节数。projector/emitter 增删、改写或漏写字段，以及伪造任何局部计数，都必须在 candidate-local
或 frontier 阶段以 `sequence_projection_mismatch` 拒绝当前 whole-set attempt。最终 full reconcile 失败表示内部
不变式破坏，exit 4，不消费 attempt，不打开输出，failed report 使用 `failed_slot=null`、`attempts_used=0`。

**背书：**「主数据 + 拒绝通道 + 统计报告」三分法是 NeMo Curator / Dolma 管线产物的通行组织 [6][9]；原子改名交付为数据工程防半截文件的标准手法。

### 3.11.3 输出示例

贯穿示例沿用 6.1 的输入法中文指令数据（`input.text_field = "instruction"`，用户 Schema 为意图标注三字段：`intent` / `topic` / `difficulty`，全部 required、`additionalProperties:false`），运行设定与数字全部沿用 3.10.4 走查（`run.output = "./out/ime-intent-0630.jsonl"`、`run.batch_size = 256`、`run.seed = 0`、`quality.threshold = 0.3`、`verify.enabled = false`；其 1000 行输入文件此处记为 `ime-2026-06.jsonl`）。

#### ① 主输出：meta_mode = "sidecar" 的一对行

`meta_mode = "inline"` 的完整行示例见 6.3，此处不重复。当 `output.meta_mode = "sidecar"` 且 `output.passthrough_fields = ["source"]` 时，主输出行为纯用户结构，`_meta` 逐行写入 `out/ime-intent-0630.meta.jsonl`，两文件以 `_meta.id` 与行序对齐（主输出第 k 行 ↔ meta 第 k 行）。下例对应输入 `ime-2026-06.jsonl` 首行 `{"instruction": "帮我写一条请假条，明天上午要去医院", "source": "ime-log", "ts": "2026-06-30T10:12:00Z"}`；每行写出前均经 `SchemaEngine.validate_only` 终检（3.11.1）。

```
# ── out/ime-intent-0630.jsonl 第 1 行（纯用户结构，无 _meta 键，剥无可剥）──
{"intent": "writing_assist", "topic": "请假条", "difficulty": "easy"}

# ── out/ime-intent-0630.meta.jsonl 第 1 行（实际为单行 JSONL，此处折行排版）──
{"_meta": {
  "id": "1cda030abc565f17",
  "run": {"tool": "labelkit/1.0.0", "started_at": "2026-07-02T10:27:41+08:00",
          "project_file": "project.toml", "rubric": "default:text", "seed": 0},
  "source": {"file": "ime-2026-06.jsonl", "line_no": 1,
             "generated_from": [], "fields": {"source": "ime-log"}},  // passthrough_fields 落点
  "stream": null,                                                     // v1.8 恒在键：未启用 segment = null（6.3）
  "scores": {"writing_style": 0.72, "facts_trivia": 0.44, "educational_value": 0.61,
             "required_expertise": 0.35, "__aggregate__": 0.53,       // 等权均值 = 2.12/4
             "mode": "pairwise_bt", "batch_no": 1},
  "dedup": {"kind": "unique"},
  "annotation": {"model": "qwen2.5-vl-72b-instruct", "attempts": 1},  // attempts=1：未触发 L3
  "verification": null                                                // verify 未启用
}}
```

v1.8 序列行说明：stream 工程（`segment.enabled = true`）的主输出行以 episode 为单位，其 `_meta.stream` 携带完整成员溯源与步骤序列（episode_id / session_id / member_ids / member_sources / steps 等，结构与样例见 6.3），行仍以 `_meta.id`（= 序列 Record id）对齐。

v1.12 members 块示例（摘自 `examples/mix` UI 主工程真跑主输出 `out/mix-labels.jsonl` 的一条 episode 行，实际为单行 JSONL，此处折行排版、长值截断；该工程 `frame.classify`（DeepSeek digest-only）与 `frame.annotate`（z.ai 视觉）双开、`[frame.class.transition.annotate].enabled = false`——index 4 成员即跳过类的 skipped 形态）：

```
{"task_label": "在美食外卖App上点一份黄焖鸡米饭", "app": "com.example.food",
 "summary": "在美食外卖App搜索并下单金牌黄焖鸡大份微辣米饭，支付¥38后等待送达。",
 "_meta": {"id": "eccc814c0694fb41", ...,
   "stream": {
     "episode_id": "eccc814c0694fb41", "session_id": "eccc814c0694fb41",
     "order_span": [1, 6], "member_count": 6,
     "member_ids": ["7cfb0c25f855b2d7", "164b7480ab098de5", ...],
     "member_sources": [{"file": "s1-food-order/uitree_1.jsonl", "pair_index": 1}, ...],
     "members": [                                     // ← member_sources 后、session_split 前（冻结位）
       {"index": 0, "id": "7cfb0c25f855b2d7", "label": "list_screen",
        "annotation": {"screen_role": "美食外卖首页",
                       "key_widgets": ["搜索美食", "搜索", "推荐餐厅", "金牌黄焖鸡 4.9 分", ...]},
        "status": "annotated"},
       {"index": 1, "id": "164b7480ab098de5", "label": "detail_screen",
        "annotation": {"screen_role": "菜品详情页",
                       "key_widgets": ["金牌黄焖鸡", "黄焖鸡米饭 ¥38", "月售 1200+ 好评率 99%", ...]},
        "status": "annotated"},
       {"index": 2, "id": "25ce67ce53d5f1d7", "label": "form_screen",
        "annotation": {"screen_role": "商品规格选择/加入购物车",   // [frame.class.form_screen.annotate]
                       "key_widgets": ["商品：黄焖鸡米饭 ¥38", "份量：大份", "辣度：微辣", ...]},
        "status": "annotated"},                        //  覆盖指令：抽取表单字段与取值
       {"index": 3, "id": "d77a51064a52f91e", "label": "confirm_screen",
        "annotation": {"screen_role": "订单确认页",
                       "key_widgets": ["确认订单", "黄焖鸡米饭 大份 ×1", "提交订单 ¥38", ...]},
        "status": "annotated"},
       {"index": 4, "id": "96cb96ed666583b1", "label": "transition",
        "annotation": null, "status": "skipped"},     // 帧类视图 enabled=false ⇒ 缺键推导 skipped
       {"index": 5, "id": "347864af1bc54006", "label": "confirm_screen",
        "annotation": {"screen_role": "支付成功结果页",
                       "key_widgets": ["支付成功", "订单号 FD20260812001", ...]},
        "status": "annotated"}],
     "session_split": false, "repaired": false, "degraded": null, "steps": null}}}
```

#### ② rejects = "refs"（默认）的一行

场景：第 213 行的标注输出经 L3 两次修复（`output.max_repair_attempts = 2`）仍未通过用户 Schema，M8 抛 `SchemaViolation`，记录置 `failed`（kind = `schema_violation`，7.6）转入 `out/ime-intent-0630.rejects.jsonl`。`errors` 即 M8 L2 `iter_errors()` 收集的全部违规（JSON Pointer 路径 + 期望 + 实际，3.8.2；违规描述为英文——枚举类由 M8 自渲染，其余直接携带 jsonschema 的原始消息，2026-08-14 起统一）；行内无任何数据内容——记录原文与 `raw_last_output` 仅在 `rejects = "full"` 时才写出。

```
# ── out/ime-intent-0630.rejects.jsonl 中的一行（折行排版）──
{"_meta": {
  "id": "c47d09e2b8a1f350",
  "source": {"file": "ime-2026-06.jsonl", "line_no": 213, "generated_from": []},
  "stage": "annotate",
  "reason": "schema_violation",
  "errors": [
    "/difficulty: expected one of enum [\"easy\", \"medium\", \"hard\"], got \"非常难\"",
    "/: Additional properties are not allowed ('confidence' was unexpected)"
  ]
}}
```

v1.7：classify 启用的工程中该 `_meta` 另含 `"label"` 键（3.11.2 rejects 行）；本例工程未启用 classify，无此键。v1.8：stream 工程另见三种 (stage, reason) 组合的 rejects 行——segment/noise、segment/below_min_len、verify/off_task_member（3.11.2）——refs 档形态同本例（仅 `_meta` 引用行；此类帧不写 item.errors，`errors` 恒 []）。

#### ③ 运行结束 stderr 摘要（逐字样例）

非 TTY、`--log-level info` 下的运行尾部。行格式为 7.3 的 `ts level stage batch msg`（日志正文自 2026-08-14 起统一英文，键名、结构与信息集不变）；数字即 3.10.4 走查：1000 条无坏行入流水线（4 批：256×3 + 232），尾批写出 184 行、失败 2 条；rejects 通道含重复 / 低质 / 失败三类（`output.rejects = "refs"`，3.11.2）。

```
2026-07-02T10:41:22+08:00 INFO  emitter batch=4 batch 4 flushed: main output +184 line(s) (total 811), rejects +48 (total 189)
2026-07-02T10:41:23+08:00 INFO  emitter batch=- finalize: fsync + rename  out/ime-intent-0630.jsonl.part -> out/ime-intent-0630.jsonl (811 lines)
2026-07-02T10:41:23+08:00 INFO  emitter batch=- wrote out/ime-intent-0630.rejects.jsonl (189 lines) and out/ime-intent-0630.report.json
2026-07-02T10:41:23+08:00 INFO  run     batch=0 run.end exit_code=0
   ── final summary (matches report.counts item by item) ──
   scanned=1000  ingested=1000  bad_input=0  generated=0
   dropped_dup=97  dropped_lowq=78  dropped_verify=0  failed=14  emitted=811
```

不变量自查（6.4）：emitted 811 + dropped (97+78+0) + failed 14 + bad_input 0 = 1000 = scanned 1000 + generated 0。尾批自查：184 + 28(dup) + 18(lowq) + 2(failed) = 232；尾批 rejects = 28+18+2 = 48，四批累计 189 = 97+78+14。

#### ④ 原子改名交付时间线

运行全程只向 `out/ime-intent-0630.jsonl.part` 追加（每批 flush）；finalize 时 fsync 后一次 rename 为 `out/ime-intent-0630.jsonl`。因此目录中任一时刻要么只有 `.part`（运行中，或未走到 finalize 的硬崩溃 / 输出路径不可写），要么只有最终文件——目标文件名出现即保证**已交付的每一行完整且合法**，永远不会读到半截行。v1.6 起熔断中止同样交付（3.10.3 熔断交付），「目标文件出现」因此不再等价「全部输入处理完毕」：消费方判定运行完整性须看 report.run：`interrupted=false` **且** `circuit_broken=false`（退出码 0/1 不足——被 SIGINT 优雅中断的运行同样交付且以 0 退出，3.10.3 中断行）；熔断交付的主输出是「已完成批的完整前缀」，缺口可由 counts.unprocessed 核对（6.4 不变量扩展）。

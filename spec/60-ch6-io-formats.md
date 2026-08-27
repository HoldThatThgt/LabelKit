# 6. 输入 / 输出格式规格

## 6.1 输入：文本模态

UTF-8 编码 JSONL；每行一个 JSON object；行分隔符 `\n`；空行跳过（不计坏行）。示例（`input.text_field = "instruction"`）：

```
{"instruction": "帮我把这段话翻译成英文……", "source": "app-feedback", "ts": "2026-06-30T10:12:00Z"}
{"instruction": "写一份周报模板", "source": "ime-log", "ts": "2026-06-30T10:15:21Z"}
```

**时间戳字段语义（v1.8 只增：stream 模式 `stream.order_by = "meta:<field>"` 的解析规格，仅文本模态，S20）**——`<field>` 为原始行对象上的点路径字段（上例 `ts`），M2 按以下规则解析（3.2）：

- 数值：`v < 0 ∨ v ≥ 1e14` ⇒ 解析失败；`v < 1e11` 判 epoch **秒**；`1e11 ≤ v < 1e14` 判 epoch **毫秒**（÷1000）。
- 字符串：先试纯数字 → 按上述数值规则；再试 `datetime.fromisoformat`（Python 3.11 起原生接受 `Z` 后缀）；均败 = 解析失败。
- 时区：aware 值换算为 UTC epoch；naive 值**按 UTC 解释**。内部序键 = float 秒。
- 解析失败与乱序**同走 `stream.on_disorder`**（"skip" 默认：跳过并计 bad_input + `IngestReport.disorder`；"fail"：InputError，退出码 3；5.2）。
- 流式单调性校验不做全量重排：单调性游标**按 `stream.key` 分区键各自维护**（S19，内存 = 键基数）——逐设备/逐来源拼接的输入不会被整体判乱序；键变即断会话，**输入须按分区键成组**（交错流为演进候选，8.4）。
- **v1.21 sequence stream 工件**：工件行顶层固定为 `payload` 与 `_meta`（6.5）；重放工程固定 `input.text_field = "payload"`、`stream.order_by = "meta:_meta.event.timestamp"`。M2 允许 object payload，完整 envelope 留在 `Record.raw`，并从 event descriptor 自包含验证时间、区间、replay 与 exact carrier；冻结 id 与同源验证见 3.2.5。工件 ID、stream exact carrier 与 delivery digest 继续使用 `labelkit:v1.20` 编码域，不因产品修订号制造 ID churn。

## 6.2 输入：UI 模态

目录递归扫描与配对规则见 3.2.4。`uitree_<index>.jsonl` 节点行的字段映射（平铺风格；嵌套风格为同字段 + `children` 数组）：

| Record 字段 | 接受的源字段名（按序取首个存在者） | 缺省行为 |
|---|---|---|
| node_id | `id`, `node_id` | 行号字符串 |
| parent_id | `parent`, `parent_id` | null（根） |
| role | `class`, `className`, `type`, `role` | "unknown" |
| text | `text`, `label` | "" |
| content_desc | `content_desc`, `contentDescription`, `desc` | "" |
| bounds | `bounds`（[l,t,r,b] 数组或 "[l,t][r,b]" 字符串两种形式） | [0,0,0,0] |
| visible | `visible`, `visible_to_user` | true |
| extra | 其余全部字段（值转字符串） | — |

该映射覆盖 Android uiautomator dump、accessibility 服务导出与主流 GUI 数据集（AndroidControl/AMEX 风格）的常见字段名；不匹配的导出格式可在采集侧做一次字段重命名。

## 6.3 主输出 JSONL

每行一个 JSON object。`meta_mode` 三种形态：

| meta_mode | 行结构 |
|---|---|
| inline（默认） | 用户 Schema 的全部字段平铺在顶层 + 保留键 `_meta`。校验语义：剥除 `_meta` 后的对象必须通过**该行的类有效 Schema**（v1.13 修订原「用户 Schema」措辞——= `[class.<name>.annotate].schema_*` 声明的按序列类覆盖，未声明 / 未分类 / 未知类则为全局 `output.schema`；M1 已禁止两者顶层声明 `_meta`，3.1.4）。M11 写出前的 `validate_only` 终检按同一口径逐行取 Schema（3.11.2）。 |
| sidecar | 主输出行 = 纯用户结构；`_meta` 逐行写入 `{output_stem}.meta.jsonl`，以 `_meta.id` 与行序对齐。 |
| none | 只写用户结构，不产出元信息（丢弃分数与溯源，不推荐）。 |

`_meta` 的完整结构：

```
{
  "screen_category": "login",                   // ← 用户 Schema 字段（示例）
  "page_title": "登录",
  "interactive_elements": [ ... ],
  "description": "手机号+验证码登录页",
  "_meta": {
    "id": "9f2c31ab52e08d17",
    "run": {"tool": "labelkit/1.0.0", "started_at": "2026-07-02T09:30:00+08:00",
             "project_file": "project.toml", "rubric": "default:ui", "seed": 42},
    "source": {"file": "capture/2026-07-01/b/uitree_2.jsonl", "pair_index": 2,
               "generated_from": [], "fields": {},           // passthrough_fields 落点
               "generator": null},   // v1.2 只增：flat 生成记录为 {"llm", "style"}（3.6.2），否则 null
    "stream": null,                  // v1.8 恒在键（位置：source 之后、scores 之前——链序镜像）；
                                     // = null 当 segment 未启用；process stream 启用时（3.14/3.10.3）：
                                     // {"episode_id", "session_id", "order_span": [first, last], "member_count",
                                     //  "member_ids": [...], "member_sources": [{file, pair_index|line_no}, ...],
                                     //  "members": [{index, id[, label][, annotation, status]}, ...],
                                     //                            // v1.12（任一帧开关启用时在场；冻结位 = member_sources 后、
                                     //                            //   session_split 前）：逐成员按序一条目，字段序冻结；
                                     //                            //   label 仅 frame.classify 启用时在场（降格跳过 = null）；
                                     //                            //   annotation/status 仅 frame.annotate 启用时在场，status
                                     //                            //   闭集 "annotated"|"skipped"|"failed"（三值判定与写前
                                     //                            //   校验兜底见 3.11.2；真实示例见 3.11.3）；帧粒度全关时
                                     //                            //   本键不在场——本块与 v1.11 逐字节等价（退化锚）；
                                     //                            //   手术后原/克隆行 members 分叉由 repaired 位消歧（4.3）
                                     //  "session_split": false,   // 所属会话曾被 batch_size 硬切（S21，M7 缺帧判定降级依据）
                                     //  "repaired": false,        // verify 缺陷修复改写过成员集（3.7 stream 分支；
                                     //                            //   multi 扇出下消歧同 id 兄弟行的成员分叉，3.13）
                                     //  "degraded": null | {kind, windows_failed},   // segment.on_error="keep" 留痕（S26；segment 专属——
                                     //                            //   stitch keep 路径留痕为事件+计数器两件，无 _meta 腿，3.16.6）
                                     //  "steps": null | [{index, action_type, target, value, description}, ...]}
                                     //                            // extract 关闭时恒 null；启用 = transitions 逐步摘要（3.15）；
                                     //                            //   v1.9（仅 stitch 启用）：步行内另含 "resumed": true——仅接缝
                                     //                            //   占位步携带（emitter 由 Transition.detail.kind=="thread_seam"
                                     //                            //   推导，3.15.4；非接缝步不携带该键）
                                     // v1.9 增两键（仅 stitch.enabled=true 时在场——off 时本块与 v1.8
                                     //   逐字节等价，3.16.4 退化锚）：
                                     //  "thread_id": "9c31f5a2d84e07b6",   // = 幸存信封 record.id = episode_id（T22，3.16.4）
                                     //  "fragments": [{"order_span": [first, last], "member_count", "cause",
                                     //                 "source_episode"}, ...]
                                     //                            // 每碎片一项、按会话序；cause ∈ "origin"|"resumed"|"rescued"；
                                     //                            //   source_episode = 碎片缝合前的 episode_id（救援碎片 = null）。
                                     //                            // 包络规范句：多碎片线索的顶层 order_span 为包络（区间内含
                                     //                            //   异线索帧）——下游切片必须用 fragments[].order_span，
                                     //                            //   不得按顶层跨度切片（3.16.4）
    "scores": {"screenshot_readability": 0.81, "tree_screen_consistency": 0.66,
               "state_completeness": 0.74, "interaction_richness": 0.52,
               "__aggregate__": 0.68, "mode": "pairwise_bt", "batch_no": 3},
                                       // scores v1.7 只增：classify 启用时另含 "pool"（= 类名，比较池自述，3.4.3 按类分池行）
    "dedup": {"kind": "unique"},
    "classification": null,            // v1.7 恒在键：classify 启用时为 {"label", "labels", "source"}（labels = 命中全集，single 恒单元素；3.13）；
                                       // 未启用 = null。multi 模式下行唯一键 = (_meta.id, classification.label)——同 id 可有多行（3.13.4 扇出行）
    "annotation": {"model": "qwen2.5-vl-72b-instruct", "attempts": 1},   // v1.2 只增：self-consistency 启用时另含 "sc": {"n", "agreement_ratio"}（3.5.2）
    "verification": {"verdict": "pass", "rounds": 1}          // verify 未启用则为 null
                                       // verification v1.8 只增：stream 模式下另含 "defects"（该键恒在，
                                       //   无缺陷 = []；缺陷项 {kind, members, position, detail}，S7，3.7 stream 分支）；
                                       //   非 stream 行不携带该键
  }
}
```

**v1.21 sequence 主输出。**sequence 形态的 main 每行是一条 primary sequence，
`Record.id = sequence_id`，并在 `_meta.generation` 携带冻结的 sequence truth。declared 形态包含
`validation_mode`、`actor_knowledge_validation`、scenario set/index/id、world branch、sequence class、
pattern、variant、expected violation 与 actual violations；instruction-only 形态不得伪造 scenario set、
pattern、variant 或 expected violation。M11 只从下游完成后的最终 `PipelineItem` 装配 `SequenceRows.main_row`，
因此 inherited classification、quality score、sequence/frame annotation 与 verification 都属于正式 main、
retained bytes 和 delivery digest；不得从 pre-downstream `ProjectedSequence.main_record` 重建输出。
main 不含 noise 或 replay 行，且不再输出旧 `tier_rank`、`time_fields` 或 `generate.stream` 真值。
默认空 rubric 选择器在 sequence 单元上解析为 `default:trajectory`；用户显式选择器优先（A.3）。

## 6.4 report.json 结构

```
{
  "run": {"tool_version": "1.0.0", "started_at": "...", "finished_at": "...",
           "interrupted": false, "circuit_broken": false, "exit_code": 0,   // circuit_broken：v1.5 只增 "modality": "ui", "seed": 42,
           "config_digest": "sha256:...", "project_digest": "sha256:..."},   // 配置指纹（脱敏后）
  // run 节 v1.6 只增："partial_delivery": true —— 仅熔断交付（3.10.3）时出现，恒伴随 circuit_broken=true
  "counts": {"scanned": 5000, "ingested": 4987, "bad_input": 13,
              "dropped_dup": 412, "dropped_lowq": 305, "dropped_verify": 41,
              "failed": 9, "generated": 0, "emitted": 4220},
  // counts v1.6 只增：熔断中止时增列 "unprocessed"（已入流水线但因中止未走完的记录数，见本节尾注不变量扩展）
  // counts v1.7 只增：classify.assignment="multi" 时增列 "fanout"（扇出净增信封数，M10 计量，3.10.3；见尾注不变量扩展）
  // counts v1.8 只增：segment 启用时增列 "episodes"（segment 阶段 len 差，M10 计量，fanout 同构）/
  //             "absorbed" / "dropped_noise"（post-emit tally，3.10.3）；且 stream 模式下 "unprocessed"
  //             的出现条件扩为「熔断 ∨ interrupted」（S18；见尾注不变量扩展）
  // counts v1.9 只增：stitch 启用时增列 "stitched"（壳终态 tally——仅计被并 episode 信封壳）/
  //             "threads"（= episodes − stitched，M10 post-emit tally 导出式单点上报，3.10.3）；
  //             两键仅启用时在场（off 时 counts 与 v1.8 逐字节等价，3.16.4 退化锚）
  // v1.8 可选节（segment 启用时出现，位于 counts 之后）：
  //   "stream": {"sessions", "episodes", "mean_episode_len", "absorbed", "dropped_noise",
  //              "below_min_len", "digest_poor_frames", "segment_failures",
  //    [预算启用，v1.11] "windows",   // segment 实际窗数（M14 属主，V13④）——供对账 dry-run/估算的
  //                                   //   w_min 上界（V12；预算未声明时不在场）
  //    [stitch 启用，v1.9] "stitch": {"stitched", "rescued_short", "seams", "judgments",
  //              "repass_judgments", "failures"},
  //    [frame.classify 启用，v1.12] "frame_classify": {"calls", "fallback", "window_failures",
  //              "skipped_degraded"},        // 位置冻结：stitch 后、extract 前
  //    [extract 启用] "extract": {"transitions", "fallback_steps", "failures", "by_type": {<action_type>: n, ...}},
  //    [frame.annotate 启用，v1.12] "frame_annotate": {"annotated", "skipped", "failed",
  //              "discarded"},               // 位置冻结：extract 后、verify 前
  //    [verify 启用]  "verify": {"membership_repairs", "boundary_flags", "defects": {<kind>: n, ...}}}
  //   —— sessions 数据源 = IngestReport（M2 属主，3.2）；below_min_len 独立于 noise 计数（S11，
  //      发生计数、v1.9 救援不回退——救援量另计 rescued_short，3.14.4）；digest_poor_frames =
  //      摘要贫瘠帧数（4.3 frame_digest 贫瘠判定）；stitch 子块 M16 属主（rescued_short 单位 = 帧、
  //      seams = 满足 T20 判据的拼接处数——接缝唯一计量点、judgments / repass_judgments =
  //      一遍/二遍逻辑判定数（每候选一判、失败不计；votes>1 放大调用不放大判定），3.16.6）；extract.by_type 为按动作类型分布（系统性劣化可观测，S14；
  //      v1.9 注：接缝占位步不计入 extract.transitions 与 by_type——非摘取产物，3.15.4）；
  //      verify 子块见 3.7 stream 分支（S31；defects 计数键 v1.9 起含 wrong_stitch，3.7.2）；
  //      v1.12 两子块**条件在场**（对应开关开启才出现——off 时 stream 节与 v1.11 逐字节等价，
  //      退化锚；两处精确集合断言测试同步）：frame_classify 键义见 3.13.7（calls = 实际派发窗数、
  //      fallback = 兜底帧数、window_failures = 失败窗数、skipped_degraded = 降格跳过 episode 数）；
  //      frame_annotate 键义见 3.5.5 / 3.11.2（annotated / skipped / failed 按成员计，
  //      discarded = 终态非 active 信封携带的已产出未交付帧标注条目数——沉没成本记账）
  "dedup": {"exact": 118, "near_text": 201, "near_image": 46, "near_both": 47,
             "clusters": 366, "image_decode_failures": 2},   // v1.2：dedup.semantic 开启时另含 near_semantic 与 embedding_failures
  // v1.7 可选块（classify 启用时出现）："classify": {"assignment": "single", "classes": {<name>: n, ...}, // 逐标签计数（multi 下多标签记录逐标签计）
  //             "fallback_count": n, "failures": n [, "multi_label_records": n — 仅 multi]}（3.13.4 事件与计数行）
  "quality": {"mode": "pairwise_bt", "rounds": 4, "judgment_failures": 17,
               "aggregate_histogram": {"0.0-0.1": 12, "...": 0},               // 10 桶
               "per_criterion_mean": {"screenshot_readability": 0.61},
               "per_criterion_tie_rate": {"screenshot_readability": 0.31}},   // v1.5 只增：仅 pairwise；分母为拿到裁决的比较数（调用级失败不计入，见 judgment_failures）
  // quality v1.7 只增：classify 启用时另含 "by_class": {<pool>: {"mode", "rounds", "aggregate_histogram",
  //             "per_criterion_mean", "per_criterion_tie_rate"}}——每池携带有效 mode/rounds；顶层 mode/rounds 保留 = 全局继承基值（3.4.3 按类分池行）
  "schema_engine": {"resolved_at": {"l0_or_clean": 4141, "l1": 87, "l3_1": 30,
                     "l3_2": 3, "rejected": 9}},
  // v1.11 可选节（上下文预算启用时出现——任一被启用阶段引用的 profile 声明 context_window）：
  //   "budget": {"profiles": {<profile>: {"context_window", "input_budget"}},   // 声明与预算终值
  //              "w_min": {"segment.window": [cap, w_min]},                     // 静态最坏装填量（V9/V12）
  //              "truncations": {<stage>: n},                                   // 各算子逐裁剪点计数
  //              "overflow_records": n,                                         // context_overflow 记录数（7.6）
  //              "image_cost": {<profile>: n},                                  // 每图成本校准终值（V19）
  //              "degrade_retries": n, "escalations": n}                        // V20 降级重试数 / V21 升级数
  //   —— counts-only（计数/统计，不含数据内容，2.6）；M10 汇总（3.10.3），键义 V13②⑤
  // v1.2 可选块："annotate": {"sc_disagreements": 0}（self-consistency 启用时）；
  //             "generate": {"buckets": {"default×concise": {"calls", "produced", "survived_dedup"[, "rejected_by_validator" — v1.5，仅配置 generate.sample_validator 时]}}}（generate 启用时）
  //             generate.buckets v1.7：classify 启用时桶 key 扩展为 "<class>×<llm>×<style>"（3.6.2 按类种子池行；关闭时格式不变）
  "runtime": {
    "queue_high_water": 0,
    "running_high_water": 0,
    "resource_wait_high_water": 0,
    "commit_waiting_high_water": 0,
    "candidate_bytes_high_water": 0,
    "cancelled_tasks": 0,
    "resource_wait_ms": 0,
    "http_pool_wait_ms": 0,
    "commit_ms": 0
  },
  "trace": {"enabled": true, "path": "./out/ui-labels-0701.trace.jsonl",
             "events": 18342, "dropped_events": 0},
  "llm_usage": {"default": {"calls": 31240, "prompt_tokens": 8.1e7,
                 "completion_tokens": 3.2e6, "est_cost_usd": 54.3, "retries": 210},
                "judge": {"...": 0}},
  // llm_usage v1.6 只增：profile 对象另含
  //   "keys": {"<api_key_env 名>": {"calls", "rate_limited", "disabled"}}（仅密钥池 >1 时出现；池内每把密钥各一项，未用到的密钥为零计数；密钥以环境变量名标识，1.6 对齐决策 ⑤）
  //   与 "parked_calls" / "parked_ms"（驻留统计，3.9.3 密钥池行；池 >1 或数值非零时出现——单密钥驻留亦须留痕）
  "timing": {"wall_s": 5400, "per_stage_s": {"dedup": 40, "quality": 2900,
              "annotate": 1800, "verify": 620}}
}
```

`runtime` 是成功 report 与 failed report 共有的顶层固定块。`queue_high_water` 是全部资源通道等待接纳的
任务最高值，`running_high_water` 是同时运行叶任务最高值，`resource_wait_high_water` 是等待 profile
许可的逻辑调用最高值，`commit_waiting_high_water` 是已经完成且等待声明序提交的候选最高值，
`candidate_bytes_high_water` 是所有已完成且尚未提交候选 canonical bytes 同时驻留总和的最高值。
`cancelled_tasks` 只计完成 cleanup 的取消叶任务；三个 `*_ms` 分别累计 profile 许可等待、HTTP origin
许可等待和无 `await` 声明序提交临界区耗时。静态 dry-run 只可构造不进入 execution domain、且不创建 leaf 的
惰性 `ExecutionRuntime` 身份载体，以相同键序写零值；
validate 与 estimate 同样不启动 runtime，也不新增数据工件。该块不得包含 endpoint、origin、prompt、payload、
state、callable repr 或 API key。

不变量：`emitted + dropped_* + failed + bad_input = scanned + generated`。熔断中止（v1.6 熔断交付，3.10.3）时扩展为 `emitted + dropped_* + failed + bad_input + unprocessed = scanned + generated`——`unprocessed` 仅此时出现，= 已扫描/已生成但因中止未走完流水线的记录数（M10 在 finalize 时按差额计算）。v1.7：`classify.assignment="multi"` 时右侧另加 `fanout`——`emitted + dropped_* + failed + bad_input = scanned + generated + fanout`；与熔断中止叠加时两项扩展并存（左侧 `+ unprocessed`、右侧 `+ fanout`，熔断残差公式同步，3.10.3 分类与扇出行）。v1.8/v1.9：segment 启用时守恒式为全展开形（3.10.3；`stitched` 为 v1.9 增项，仅 stitch 启用时非零在场）——

`emitted + dropped_dup + dropped_lowq + dropped_verify + dropped_noise + failed + bad_input + absorbed + stitched = scanned + generated + fanout + episodes`

（左侧新增 `dropped_noise` 与 `absorbed`（v1.8）及 `stitched`（v1.9 壳终态；fanout（右侧）计信封存在、stitched（左侧）计壳终态，二者分别记账无双记——经审计数值验证）、右侧新增 `episodes`；未启用的项恒 0，退化为上式）。`counts.threads` 不入守恒式——它是恒等式 `threads = episodes − stitched` 的导出量（M10 post-emit tally 单点上报，3.10.3；`rescued_short` 帧的 dropped_noise → absorbed 翻转发生在 emit 前、账目在路由时已定格，不破坏两侧平衡）。且 **stream 模式下 `counts.unprocessed` 的出现条件扩为「熔断 ∨ interrupted」**（S18：SIGINT 中断叠加会话缓冲会产生未走完流水线的残差；此时左侧另加 `unprocessed`，残差公式右侧 `+ episodes`、左侧 `+ absorbed + dropped_noise`（v1.9 另 `+ stitched`）同步扩展，failed 兜底公式减项同步——三处同步见 3.10.3 线索缝合行）；非 stream 模式中断残差恒 0、不加键（回归锚不动）。`schema_engine.resolved_at` 仅统计用户 Schema 的标注调用，加总 = 进入 M5 的记录数（4141+87+30+3+9 = 4270 = ingested 4987 − dropped_dup 412 − dropped_lowq 305）；裁决/评审/生成等内部 Schema 解析不计入。v1.12：**守恒恒等式与全部计数不变量零改动**——帧产物挂信封字段、不改任何信封状态（成员帧保持 absorbed，4.3 零改动声明），`frame_classify.*` / `frame_annotate.*` 是独立命名空间的新增计数、不进 counts 与守恒式；**`resolved_at` 恒等式不受帧标注影响**——帧标注走 `complete_validated` 的**显式 schema 参数**（= 内部 Schema 待遇，3.8.2 路由声明），与裁决/评审/生成同列不入桶，「加总 = 进入 M5 的记录数」在帧粒度开启时依然逐数成立。

v1.21 sequence 形态不复用上述渐进式 counts 守恒来宣称部分成功：整组 delivery 在正式 commit 前保持 attempt-local，slot 或 noise 耗尽即不替换 main、stream、成功 report 或 manifest。成功计数以 `report.generate.sequence` 的 planned/delivered 恒等式为准；main 只计 primary sequence，stream 另计 primary/noise/replay event。报告、manifest 与 failed report 都只含计数、摘要和路径，不含数据内容。

**rejects 通道 v1.8 增量**（完整格式规范属 3.11.2，此处登记 IO 面变化）：rejects 行的 (stage, reason) 组合新增三种——`segment / noise`（LLM 判噪声帧）、`segment / below_min_len`（短段丢弃帧，独立于 noise，S11）、`verify / off_task_member`（修复收缩弃帧，S31）；`--strict` 交互注意：stream 工程下噪声帧属预期产物，会触发退出码 1。**rejects 通道 v1.9 增量**：(stage, reason) 组合再增一种——`stitch / stitch_invalid`（仅 `stitch.on_error = "fail"` 时出现，3.16.6）；stitched 壳与被救援帧永不入 rejects（第四路由 / 翻转回 absorbed，3.11.2）——`--strict` 补注：同输入开启 stitch 后（短段被救援不再落 rejects）strict 结果可能由 1 变 0，属预期（2.4）。`output.rejects = "full"` 档对序列 Record 的原始载荷输出 `{"kind": "sequence", "member_ids": [...], "member_sources": [...]}`（S25——单记录 `_raw_payload` 假设的序列分支；`raw_last_output` 的 reason 门维持 schema_violation 现状，既有缺口明文接受）。**rejects 通道 v1.11 增量**：reason 词表再增两值——`context_overflow`（上下文预算三形态：预检 / 最小单元不装 / 反应态降级耗尽，V10/V16/V24）与 `output_truncated`（响应以输出上限截断收尾的终局化，V11）；stage = 产生该错误的属主算子（任何 LLM 调用阶段皆可出现），语义、处置与熔断矩阵见 7.6；refs / full 档行形态不变（两 kind 均不携带 `raw_last_output`）。**rejects 通道 v1.12 零增量声明**：帧粒度对本通道**零改动**——(stage, reason) 组合不增、reason 词表不增、行键集闭集不动；**帧级失败的成员不产生 rejects 行**（帧分类失败落 `fallback_class`、帧标注失败落 members[] 条目 status="failed"，均为成员级留痕非信封失败，3.13.7/3.5.5/3.11.2），`--strict` 判定读信封状态计数，**不受帧失败影响**（裁决·成员失败不入 rejects）。

**v1.21 sequence rejects 边界：**sequence 形态的六路径集合令 `rejects = sidecar = null`。失败 attempt 从未成为正式 Record，不写 rejects；它只进入 `report.generate.sequence.rejected_attempts` 的唯一终态桶。任一 slot 耗尽写独立 failed report 并以退出码 1 结束，不提交已接受前缀。普通 process、flat generate 与 process stream 的 rejects 规则保持本段既有语义。

### 6.4.1 v1.21 sequence success report

sequence 形态不写旧 `report.generate.stream`。成功 report 的 `report.generate.sequence` 键序冻结如下；
示例值同时冻结 `examples/sequence-generation` 的验收算术：2 sets、8 primary sequences、22 primary events、
2 noise events、1 replay sequence 的 3 replay events，合计 27 stream rows；本例未启用交织，8 个 primary sessions
彼此独立，另有 1 个 noise session 与 1 个 replay tail session。下例只展开 `generate.sequence`；顶层
`runtime` 仍按 6.4 的固定形状在场，不能嵌入本块。

```json
{
  "mode": "declared",
  "run_attempt_id": "89ab...",
  "run_id": "0123...",
  "delivery_digest": "4567...",
  "artifacts_committed": true,
  "program_digest": "...",
  "plan_digest": "...",
  "planned_sets": 2,
  "delivered_sets": 2,
  "planned_sequences": 8,
  "delivered_sequences": 8,
  "primary_events": 22,
  "interleaving_opportunities": 0,
  "primary_sessions": 8,
  "interleaved_primary_sessions": 0,
  "by_interleaving_pattern": {},
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
    "noise_provider_retryable_exhausted": 0,
    "noise_reconcile": 0
  }
}
```

成功时 `planned_sets = delivered_sets`、`planned_sequences = delivered_sequences`，且每个 variant
的 planned 与 delivered 相等。`sequence_calls` 计逻辑 family 入口，包含失败 attempt；同一入口内的
L3 repair 与 provider retry 继续只计既有 schema/usage 面。一次失败 attempt 只进入停止处的一个
`rejected_attempts` 桶；noise slot 只使用 noise 前缀的八个桶。未列键不得动态追加；provider fatal、
plan 与 commit-I/O 属 run terminal，只写 `terminal_error_kind`。

设全部可见 primary branch 数为 N、冻结 `InterleavingLayout` 数为 D，则
`interleaved_primary_sessions=D`、`primary_sessions=N-D`。`interleaving_opportunities` 只在当前
trigger 至少有一个 applicable pattern 的共享 partner pool 非空时加一。`by_interleaving_pattern`
按 TOML pattern 声明序输出，包含 `selected_sessions=0` 的 pattern；每项固定为
`{"eligible_opportunities", "selected_sessions"}`。disabled 与 instruction-only 固定输出
`interleaving_opportunities=0`、`interleaved_primary_sessions=0`、`by_interleaving_pattern={}`。
交织不改变 planned/delivered set、sequence、event、stream row、LLM call、noise 或 replay 计数。

`plan_digest` 紧跟 `program_digest`，必须为 64 位小写 hex；它覆盖 opportunity、pattern map、
exact pair identity 与 exact timestamp/session。report 不输出 slot identity、逐 pair owner word、payload、prompt、state
或 API key。exact pair identity 只留在内存 `ScenarioPlan`，owner word 只从 stream/plan blocks 机械派生。

failed report 使用相同 usage 与 `rejected_attempts` 口径，固定路径为
`{output_stem}.failed.report.json`。它始终包含 `run_attempt_id`，另含 nullable `run_id`、
`artifacts_committed = false`、nullable `failed_slot`、`attempts_used` 与 `terminal_error_kind`；
不含 by-pattern 已交付前缀，因为没有数据提交。plan 尚未产生时 `run_id = null`。coordinator 与全部
叶任务 cleanup 完成后才冻结 failed report 及其顶层 `runtime`；因低槽耗尽而取消的高槽候选、reservation
与 dataset counters 不得泄漏为已提交计数，但已经发生的 usage、retry、Schema、trace、等待与取消仍是运行事实。
交织 pattern 与 partner 一旦抽中即冻结；无可行布局写
`terminal_error_kind="generation_plan_infeasible"` 并以退出码 2 失败，不换 partner/pattern/none、
不转 standalone 也不重抽。FEASIBLE/UNKNOWN 为 `generation_plan_budget`、exit 4；MODEL_INVALID 为
`generation_plan_internal`、exit 4。规划失败不打开 main、stream、success report、manifest 或 rejects。

### 6.4.2 sequence 路径与成功提交

M1 一次冻结六个路径：main = `run.output`、stream = `{output_stem}.stream.jsonl`、report =
`{output_stem}.report.json`、manifest = `{output_stem}.manifest.json`、failed report =
`{output_stem}.failed.report.json`、rejects = sidecar = null。成功前不打开 main、stream、report
或 manifest；全部 slot、noise、replay 与 CrossViewReconciler 通过后，分别写同目录 `.part`、flush、
fsync，按 main、stream、report 顺序 `os.replace`，最后单独原子替换 manifest。

成功 manifest 键序固定为 `schema_version = 1`、`run_id`、`delivery_digest`、
`artifacts_committed = true`、`main: {path, sha256, rows}`、`stream: {path, sha256, rows}`、
`report: {path, sha256}`、`committed_at`。消费者只有在 manifest 摘要与三份工件一致时才接受提交。
failed report 不属于成功 manifest，成功运行不删除历史 failed report；它只是最近一次失败诊断，
不得否定摘要有效的 manifest。commit-I/O 失败可能已替换固定路径的一个子集，但旧 manifest 必须保持不变；
此时 `artifacts_committed = false` 只表示没有可信任的新 manifest，消费者必须以摘要失配拒绝混代文件。

`SequenceDeliveryEmitter.prepare_product` 是 `delivery_digest` 的唯一 owner。它对最终 main 与 stream 行只计算
一次完整 64 位 SHA-256，并把值写入冻结 report；manifest 只读取 `product.report.delivery_digest`，不得重算或
保存第二份摘要真值。摘要材料先写 ASCII header `labelkit:v1.20:delivery\n`，再按 main 视图行序、stream
视图行序依次写 frame；每个 frame 恰为 canonical row byte 长度的十进制 ASCII、冒号与 row bytes。
`canonical_delivery_row` 只移除发射期墙钟观测字段，包括 `_meta.run.started_at`、`finished_at`、
`duration_ms` 与 manifest `committed_at`，再按 `sort_keys = true`、紧凑 separators、
`ensure_ascii = false` 编码。摘要不写回 main/stream，也不参与任何 Record ID，因而不存在自引用。
该 canonical 编码只用于身份、计费与摘要；正式 main/stream/report/manifest 使用保留声明序的紧凑 JSON
序列化，不能因复用摘要编码而打乱本节冻结的键序。

## 6.5 v1.21 sequence stream 工件（`labelkit:v1.20` 编码域）

sequence 生成的第二份数据产物固定为 `{output_stem}.stream.jsonl`。UTF-8 JSONL 一行一个
event，顶层只允许 `payload` 与 `_meta`。primary member 至少具有以下结构：

```json
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
      "timestamp": "2026-01-05T09:16:00.000000+08:00",
      "duration_us": 120000000,
      "resources": ["foreground_app"],
      "time_bindings": [
        {"payload_path": "/timestamp", "source": "event_start_milliseconds"}
      ]
    }
  }
}
```

primary row 同时携带与 main owner 一致的 `_meta.generation` sequence truth。declared role 来自
PatternEvaluator 的 actual binding；instruction-only role 固定为 `position_000`、`position_001`
等位置名，不伪装成业务 pattern role。工件 timestamp、gap 与 elapsed 保留在 `_meta.event`；payload Schema 中
以 `x-labelkit-business-time = true` 标记的叶子由同一 Planner start/end/duration 机械注入。

noise row 的 `owner_sequence_id`、role、scenario id 与 world branch id 均为 null，并显式
`noise = true`，并固定 `duration_us = 0`、空 resources。`NoiseSlot` 独立冻结 event key、ordinal、frame class、
timestamp、duration、resources 与 session；不得用
`PlannedEvent` 的 null/sentinel 冒充。

`ReplayLayout` 独立冻结 source slot key、source positive variant name、replay ordinal、session 与一个正、毫秒对齐的
`shift_us`。全部 replay start 等于 source start 加该常量，source delta、duration、resources 与 descriptor 保持。ReplayProjector
只在 M11 已由最终 `PipelineItem` 装配出 `SequenceRows` 后运行，并从 source
`primary_stream_rows` 逐行复制。它保留非时间 payload、event key、role、frame class、actor、logical time、
frame annotation 与其他下游 metadata，替换 replay 身份、工件 timestamp 与 provenance，并按 replay start/end/duration
机械重绑 payload business time。

replay event 固定 `owner_sequence_id = null`，只用 `replay_sequence_id` 分组，并显式携带
`replay_ordinal`、`duplicate_of_sequence_id` 与逐位 `duplicate_of_event_id`；event id 与 timestamp 新生。
其 `_meta.generation` 固定含 `validation_mode = "replay"`、`source_validation_mode`、`sequence_class`、
`scenario_id`、`source_pattern`、`source_variant` 与 `duplicate_of_sequence_id`，不得产生新的
`world_branch_id` 或 primary variant 字段。replay timestamp 必须用 timeline 的 fixed UTC offset 和
`datetime.isoformat(timespec="microseconds")`，不得使用本机时区或省略尾部微秒。

除 `delivery_digest` 外，generation ID 统一调用 `derive_generation_id(domain, components)`：哈希材料恰为
`canonical_json(["labelkit:v1.20", domain, components])` 的 UTF-8 bytes，结果取 SHA-256 小写 hex 前
32 字符。components 是有序 JSON array；start/duration component 一律为 integer 微秒，payload component 是
已验证 JSON object 本身，ordered event ids 是 JSON array，不得由调用方预序列化或拼接字符串。

| ID | domain | components |
|---|---|---|
| declared scenario_id | `declared_scenario_id` | program_digest、counterfactual set name、scenario_index |
| declared world_branch_id | `declared_world_branch_id` | scenario_id、variant name |
| instruction scenario_id | `instruction_scenario_id` | program_digest、instruction slot name、scenario_index |
| instruction world_branch_id | `instruction_world_branch_id` | scenario_id、字面值 instruction_only |
| declared event_key | `declared_event_key` | scenario_id、baseline role name |
| instruction event_key | `instruction_event_key` | scenario_id、instruction slot name、scenario_index、position |
| primary event_id | `primary_event_id` | world_branch_id、event_key、start、duration、resources、time descriptor、最终 payload |
| sequence_id | `sequence_id` | world_branch_id、ordered event_id list |
| replay_sequence_id | `replay_sequence_id` | source sequence_id、replay ordinal |
| replay event_id | `replay_event_id` | replay_sequence_id、source event_id、replay start、source duration、最终 rebound payload |
| noise event_key | `noise_event_key` | program_digest、字面值 noise、noise ordinal |
| noise event_id | `noise_event_id` | run_id、noise event_key、start、零 duration、空 resources、time descriptor、最终 payload |
| run_attempt_id | `run_attempt_id` | program_digest、seed |
| run_id | `run_id` | run_attempt_id、ScenarioPlan.digest |

生成内存映射固定为 member `Record.raw = 完整 stream row`、`Record.text = canonical_json(payload)`、
`Record.id = event_id`。M2 从 descriptor 独立重算 binding、ID、constant shift、全局 start 与 resource interval，
并把删除全部 time paths 的 payload canonical JSON 写入 `Record.exact_dedup_text`。generation sequence exact key 是
`sha256(canonical_json(["labelkit:v1.20", "generation_stream_exact",
ordered_exact_dedup_texts]))`；M3 对该 carrier 只运行 exact 层，不构造 MinHash 或 embedding。合法 rebound replay
仍命中 duplicate；非时间 payload 不同则不能命中。

CrossViewReconciler 在 commit 前检查 main/stream 双向一致：每个 primary event 恰好对应一个 main owner，
main 只含 primary sequence；noise 与 replay 只存在于 stream；每个 payload time、annotation time、containment、
constant shift 与 resource interval 均通过。`examples/sequence-generation` 的冻结
账本为 main 8 行，stream 27 行 = 22 primary + 2 noise + 3 replay。把该 stream 工件送入普通 process
replay 后，验收计数固定为 scanned 27、absorbed 25、dropped_noise 2、episodes 9、dropped_dup 1、
emitted 8、failed 0；这证明完整 replay sequence 被 M3 删除，而不是按 provenance 短路。

`retained_content_bytes` 与 delivery digest 复用同一个 `canonical_delivery_row` helper，每行精确计
`len(canonical_row_bytes) + 1`，其中一 byte 是 JSONL 换行。`SequenceRows` 只计自己的 final main row
与 primary stream rows；`ReplayRows` 只计 replay rows；发射期墙钟字段既不驻留在产品行中，也不计入上限。
声明序提交时只比较“已提交实际 bytes + 当前 `PreparedCandidate` 的实际 bytes”。当前候选已经闭包自己的
final `SequenceRows` 与由最终 source rows 投影的全部 `ReplayRows`；更高 ordinal 的已准备候选不计入当前
retained gate。上限固定 536870912（512 MiB）；恰等于上限时允许提交，超一个 UTF-8 byte 时整个 slot
拒绝，且在 `group_commit` 前保持 dedup、dataset 与 replay 零提交。

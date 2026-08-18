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
- **与时间流工件的对应（v1.13）**：时间流生成形态产出的工件（6.5）就是本节格式的一份实例——工件行的时间戳字段名即该工程 `stream.order_by = "meta:<field>"` 声明的 `<field>`、文本字段名即 `input.text_field`，写出值为微秒精度的 ISO-8601 字符串（本节字符串分支）；工件另带一个 `truth` 对象，对摄取侧而言只是行上的普通字段（参与 id 计算、不参与任何判定，可经 `output.passthrough_fields` 透传做对照）。配同一份 `[stream]` 声明重放时，M2 切出的会话与生成侧逐一对应（3.2.8 可重放注记）。

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
               "generator": null},   // v1.2 只增：生成记录为 {"llm", "style"}（3.6.2），否则 null；
                                     //   v1.14 键集条件形：时间流生成的档位表在场时增第三键
                                     //   tier_rank（该序列所属档位序数），档位表缺省时维持两键
    "stream": null,                  // v1.8 恒在键（位置：source 之后、scores 之前——链序镜像）；
                                     // = null 当 segment 与 generate_stream 均未启用（v1.13 门扩：
                                     //   segment.enabled ∨ generate_stream.enabled，3.11.2）。启用时（3.14/3.10.3）：
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
                                     //                            // v1.13 时间流生成形态：本块与 label 列的在场门同步扩
                                     //                            //   `∨ generate_stream.enabled`——条目恒 {index, id, label}
                                     //                            //   （label = 蓝图定下的帧类真值，source="inherited"；本形态
                                     //                            //   与 frame.annotate 互斥 ⇒ 无 annotation/status 两列，
                                     //                            //   v1.12 条件列规则相容，3.6.5/3.11.2）
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

**时间流生成形态的 `_meta.stream`（v1.13）**：直装序列行原样复用上述 stream 块，取值面如下——`episode_id` = `session_id` 之外的序列 record.id、`order_span` = `["<工件路径>:<首行号>", "<工件路径>:<末行号>"]`、`member_sources` 每项 = `{file: <工件路径>, line_no: <工件行号>}`（工件路径即 `run.output` 同款相对写法，行号 1 基、与工件行序一一对应 ⇒ 主输出与工件双向可对账）、`members[]` 见上、`session_split = false`、`repaired = false`、`degraded = null`、`steps = null`（extract 不参与）、**无** `thread_id` / `fragments`（stitch 不参与）。`_meta.run.rubric` 在本形态下的空选择器解析为 `"default:trajectory"`（3.11.2 rubric 镜像）。

## 6.4 report.json 结构

```
{
  "run": {"tool_version": "1.0.0", "started_at": "...", "finished_at": "...",
           "interrupted": false, "circuit_broken": false, "exit_code": 0,   // circuit_broken：v1.5 只增 "modality": "ui", "seed": 42,
           "config_digest": "sha256:...", "project_digest": "sha256:..."},   // 配置指纹（脱敏后）
  // run 节 v1.6 只增："partial_delivery": true —— 仅熔断交付（3.10.3）时出现，恒伴随 circuit_broken=true
  // run 节 v1.13 只增："artifact": {"path", "sha256", "lines"} —— 时间流工件条目（主输出摘要同款形态：
  //             sha256 按落盘字节计、带 "sha256:" 前缀）；**仅工件通道实际写入时在场**（形态关闭与
  //             dry-run 恒缺席，3.11.2 工件通道行）
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
  // v1.13 可选子块（generate.stream 启用时出现，位于 generate 节内、buckets 之后；counts-only，键集与键序冻结）：
  //   "generate": {"buckets": {...},
  //                "stream": {"sessions",            // 交织出的会话数（**不含**重发尾会话）
  //                           "crossed_sessions",    // 其中的交叉会话数（= Σ幸存 − sessions_eff）
  //                           "sequences": {<class>: {"planned", "produced"}},  // 按 [[classify.classes]] 声明序零基铺开
  //                           "tiers": {"<tier_rank>": {"planned", "produced"}}, // v1.14，**条件在场**：仅
  //                                                  //   [[generate.stream.tiers]] 非空时出现；键位冻结在
  //                                                  //   sequences 之后、frames 之前（配额族相邻），键为十进制
  //                                                  //   字符串的档位序数、按 tier_rank 升序；口径同 sequences
  //                                                  //   （planned 计于计划期、produced 数最终进链的条数）。
  //                                                  //   由 M10 按声明档位表**显式铺开** ⇒ 零额档与全作废档也
  //                                                  //   如实在场（planned 0 / produced 0），不依赖计数器首触序
  //                           "frames",              // 任务帧总数（幸存序列的步数之和）
  //                           "noise_frames",        // 实际织入的噪音帧数（签池耗尽时 < 目标数）
  //                           "duplicates",          // 实际重发的序列条数（按幸存数钳制后）
  //                           "plan_calls", "realize_calls", "noise_calls",     // 三类调用数（realize 含对半降级的分次调用）
  //                           "plan_failures", "realize_failures",              // 作废序列数（按失败环节归类）
  //                           "validator_scrapped"}} // sample_validator 逐帧违规作废的序列数
  //   —— M6 属主（3.6.5）；工件行数不在本子块，登记于 run.artifact.lines（上文 run 节）；
  //      `report.stream` 节在本形态下**不出现**（那是 segment 的观测面，避免混淆）；
  //      `report.classify` 直方图恒全零属预期（标签 inherited、零判决调用，3.13.4）
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

不变量：`emitted + dropped_* + failed + bad_input = scanned + generated`。熔断中止（v1.6 熔断交付，3.10.3）时扩展为 `emitted + dropped_* + failed + bad_input + unprocessed = scanned + generated`——`unprocessed` 仅此时出现，= 已扫描/已生成但因中止未走完流水线的记录数（M10 在 finalize 时按差额计算）。v1.7：`classify.assignment="multi"` 时右侧另加 `fanout`——`emitted + dropped_* + failed + bad_input = scanned + generated + fanout`；与熔断中止叠加时两项扩展并存（左侧 `+ unprocessed`、右侧 `+ fanout`，熔断残差公式同步，3.10.3 分类与扇出行）。v1.8/v1.9：segment 启用时守恒式为全展开形（3.10.3；`stitched` 为 v1.9 增项，仅 stitch 启用时非零在场）——

`emitted + dropped_dup + dropped_lowq + dropped_verify + dropped_noise + failed + bad_input + absorbed + stitched = scanned + generated + fanout + episodes`

（左侧新增 `dropped_noise` 与 `absorbed`（v1.8）及 `stitched`（v1.9 壳终态；fanout（右侧）计信封存在、stitched（左侧）计壳终态，二者分别记账无双记——经审计数值验证）、右侧新增 `episodes`；未启用的项恒 0，退化为上式）。`counts.threads` 不入守恒式——它是恒等式 `threads = episodes − stitched` 的导出量（M10 post-emit tally 单点上报，3.10.3；`rescued_short` 帧的 dropped_noise → absorbed 翻转发生在 emit 前、账目在路由时已定格，不破坏两侧平衡）。且 **stream 模式下 `counts.unprocessed` 的出现条件扩为「熔断 ∨ interrupted」**（S18：SIGINT 中断叠加会话缓冲会产生未走完流水线的残差；此时左侧另加 `unprocessed`，残差公式右侧 `+ episodes`、左侧 `+ absorbed + dropped_noise`（v1.9 另 `+ stitched`）同步扩展，failed 兜底公式减项同步——三处同步见 3.10.3 线索缝合行）；非 stream 模式中断残差恒 0、不加键（回归锚不动）。`schema_engine.resolved_at` 仅统计用户 Schema 的标注调用，加总 = 进入 M5 的记录数（4141+87+30+3+9 = 4270 = ingested 4987 − dropped_dup 412 − dropped_lowq 305）；裁决/评审/生成等内部 Schema 解析不计入。v1.12：**守恒恒等式与全部计数不变量零改动**——帧产物挂信封字段、不改任何信封状态（成员帧保持 absorbed，4.3 零改动声明），`frame_classify.*` / `frame_annotate.*` 是独立命名空间的新增计数、不进 counts 与守恒式；**`resolved_at` 恒等式不受帧标注影响**——帧标注走 `complete_validated` 的**显式 schema 参数**（= 内部 Schema 待遇，3.8.2 路由声明），与裁决/评审/生成同列不入桶，「加总 = 进入 M5 的记录数」在帧粒度开启时依然逐数成立。v1.13：**守恒恒等式零改动**——时间流生成形态取 generate_only 的**退化形** `emitted + dropped_dup + dropped_lowq + dropped_verify + failed = generated`（`generated` = 进链的直装序列条数；成员帧从不构造信封 ⇒ `absorbed` / `dropped_noise` / `stitched` / `episodes` 四项恒 0 不出现，噪音帧与重发帧只活在工件、不进任何账），`report.generate.stream.*` 与 `run.artifact.*` 同属独立命名空间的新增计数、不进 counts 与守恒式。**`resolved_at` 恒等式口径重述**（3.8.2 待遇参数）：「加总 = 进入 M5 的**记录级**标注调用数（用户待遇族）」——按序列类标注 Schema 的调用虽显式传 schema，仍属记录级标注、**照常计入**（`user_treatment=True`）；帧级标注等内部待遇调用仍不计。报告中无任何数据内容字段。

**rejects 通道 v1.8 增量**（完整格式规范属 3.11.2，此处登记 IO 面变化）：rejects 行的 (stage, reason) 组合新增三种——`segment / noise`（LLM 判噪声帧）、`segment / below_min_len`（短段丢弃帧，独立于 noise，S11）、`verify / off_task_member`（修复收缩弃帧，S31）；`--strict` 交互注意：stream 工程下噪声帧属预期产物，会触发退出码 1。**rejects 通道 v1.9 增量**：(stage, reason) 组合再增一种——`stitch / stitch_invalid`（仅 `stitch.on_error = "fail"` 时出现，3.16.6）；stitched 壳与被救援帧永不入 rejects（第四路由 / 翻转回 absorbed，3.11.2）——`--strict` 补注：同输入开启 stitch 后（短段被救援不再落 rejects）strict 结果可能由 1 变 0，属预期（2.4）。`output.rejects = "full"` 档对序列 Record 的原始载荷输出 `{"kind": "sequence", "member_ids": [...], "member_sources": [...]}`（S25——单记录 `_raw_payload` 假设的序列分支；`raw_last_output` 的 reason 门维持 schema_violation 现状，既有缺口明文接受）。**rejects 通道 v1.11 增量**：reason 词表再增两值——`context_overflow`（上下文预算三形态：预检 / 最小单元不装 / 反应态降级耗尽，V10/V16/V24）与 `output_truncated`（响应以输出上限截断收尾的终局化，V11）；stage = 产生该错误的属主算子（任何 LLM 调用阶段皆可出现），语义、处置与熔断矩阵见 7.6；refs / full 档行形态不变（两 kind 均不携带 `raw_last_output`）。**rejects 通道 v1.12 零增量声明**：帧粒度对本通道**零改动**——(stage, reason) 组合不增、reason 词表不增、行键集闭集不动；**帧级失败的成员不产生 rejects 行**（帧分类失败落 `fallback_class`、帧标注失败落 members[] 条目 status="failed"，均为成员级留痕非信封失败，3.13.7/3.5.5/3.11.2），`--strict` 判定读信封状态计数，**不受帧失败影响**（裁决·成员失败不入 rejects）。**rejects 通道 v1.13 零增量声明**：时间流生成对本通道同样**零改动**——(stage, reason) 组合不增、reason 词表不增、行键集闭集不动；生成期**作废**的序列（蓝图/帧实现失败、逐帧钩子违规、序列相似度过滤淘汰）**不产生 rejects 行**——它们从未成为记录，留痕在 `report.generate.stream.*` 计数与值-free 的 stderr WARN（3.6.5 作废语义，`--strict` 不受影响）；进链后被淘汰的序列（`dropped_dup` / `dropped_lowq` / `dropped_verify` / `failed`）照既有规则入 rejects——判决形评审的 fail 收尾即 `verify / dropped_verify` 的既有形态（3.7.5）。

## 6.5 时间流工件格式（v1.13）

时间流生成形态（`generate_stream.enabled`，3.6.5）的第二份产物，路径 `{output_stem}.stream.jsonl`（M11 第五输出通道，3.11.2）。UTF-8 JSONL，一行一帧，**行序 = 交织序**（时间戳严格递增），行号（1 基）即 `_meta.stream.member_sources[].line_no`。

**行结构**（三键，键序冻结）：

```
{"<stream.order_by 的 meta 字段名>": "<ISO-8601 微秒精度时间戳>",
 "<input.text_field>": <帧内容：纯文本帧为字符串 | 结构化帧为对象>,
 "truth": {...}}
```

`truth` 键集（**冻结**，counts 与标识，不含任何工具内部 id）：

| 键 | 类型 | 语义 |
|---|---|---|
| `session` | int | 全流会话序数，0 基（含重发尾会话）。 |
| `sequence_class` | str \| null | 所属序列类名；噪音帧为 null。 |
| `sequence` | int \| null | 该序列在其类内的序数，0 基（= 计划期标识）；噪音帧与**重发副本**为 null。 |
| `tier_rank` | int \| null | **v1.14 新增，仅档位表（`[[generate.stream.tiers]]`）在场时出现**：该序列所属档位的序数。任务帧 = 本档序数；噪音帧为 null；重发帧**承源**（= 被重发序列的档位）。键位在 `sequence` 之后（序列身份组）、`frame_class` 之前——**键序重冻结**，行文件的字节序由此定（id 用 canonical JSON 键排序计算，不受键序影响）。 |
| `frame_class` | str \| null | 该帧的帧类（蓝图定下的真值）；噪音帧为 null。 |
| `noise` | bool | 插入型噪音帧标志；任务帧与重发帧恒 false。 |
| `duplicate_of` | int | **仅重发序列的帧在场**：值 = 被重发的原序列的类内序数（重发副本无自身计划期身份，归属经本键对账）。 |

**真值不携最终 id**（封死循环依赖）：序列归属只用计划期标识（`sequence_class` + `sequence`[+ `tier_rank`]），**禁止**携带装配后的 record id——成员 id 依赖行内容、序列 id 依赖成员 id，携带即成环；主输出 `_meta.stream` 与工件的对账靠 `member_sources` 的行号双向可查（3.6.5）。

**回填字段注记（v1.14）**：声明了时间字段绑定（`[frame.class.<name>.generate.time_fields]`）的帧类，其行内**文本字段对象**里的绑定键不是 LLM 产出而是 harness 按已铺时间轴回填的机械量（值 = `round(序内相邻成员 ts 差, 6)` 等，词表见 5.2）。回填发生在时间戳铺设之后、行对象与 id 计算**之前**，故这些值同样进 `Record.raw`、进成员 id 与序列 id ⇒ 工件重放逐字节同 id 同会话（下方重放契约①）。对账口径两条：① 值按**本序列相邻成员**计——交叉会话里夹进来的外序列帧与噪音帧不参与差值，逐行对账时须先按 `truth.sequence_class` + `truth.sequence` 归组；② **`duplicate_of` 在场的重发行除外**——其绑定值与 `tier_rank` 同为承源量，不与自身所在会话的时间轴对账。

**重放契约**：工件本身就是一份合法的 6.1 文本模态输入。可往返性由 M1 工件键守卫在启动期保证（3.1.4 时间流生成行）：`input.text_field` 与 `stream.order_by` 的时间戳字段名均须为**平坦字段名**（工件行以该字符串原样作键，点路径在重放侧按 6.1 抽取时取不到、整份判坏行——含 `"."` 即 CONFIG_ERROR），两者互不同名、且均不得为 `"truth"`（工件行三个顶层键互斥）。把它拷为某工程的 `[run].input`、配同一份 `[stream]` 声明（`order_by = "meta:<同名字段>"`、同一 `gap_s`）并开 `segment`，即可原样重放——① 成员 `Record.id` 逐字节一致（M2 的 `sha256(canonical_json(raw))[:16]` 作用于同一份行对象，生成侧的成员 raw 就是工件行全对象，3.2.5）；② 会话切分一致（交织器铺设的会话间隔恒 > `gap_s`、会话内间隔恒 < `gap_s`），`session_id` 亦逐字节一致（M2 公式的输入含会话内**全部**帧，3.2.8）；③ `truth` 对摄取侧只是普通字段——参与 id 计算、**不参与任何判定**，需要时经 `output.passthrough_fields` 透传出来与重放结果比对（自动化的重放评测回路是明确非目标，2.1.2 ⑧）。

**真实样例**（`examples/synth-stream` 2026-08-18 真跑工件的三行摘录，`order_by = "meta:ts"`、`text_field = "text"`，档位表与时间字段绑定均在场；实际为单行 JSONL）：

```
// 第 1 行：结构化帧（帧类 task_request 声明了生成 Schema ⇒ 文本字段是对象）；
//          tier_rank = 1（第 1 档，构成 {task_request, followup}）；
//          duration 是回填字段（绑定 gap_next_s）
{"ts": "2026-01-05T09:00:00.000000+08:00",
 "text": {"utterance": "你好，我想买明天上午从北京到上海的高铁票，有合适的推荐吗？",
          "entities": ["明天", "上午", "北京", "上海", "高铁票"],
          "duration": 71.053996},
 "truth": {"session": 0, "sequence_class": "ticket_booking", "sequence": 0,
           "tier_rank": 1, "frame_class": "task_request", "noise": false}}

// 第 12 行：插入型噪音帧（四 null + noise=true——档位表在场时 tier_rank 亦为 null）
{"ts": "2026-01-05T09:22:20.673020+08:00", "text": "今天天气真不错啊",
 "truth": {"session": 1, "sequence_class": null, "sequence": null,
           "tier_rank": null, "frame_class": null, "noise": true}}

// 第 26 行：重发序列的首帧（sequence=null + duplicate_of 指回原序列类内序数；落流尾新会话）；
//          tier_rank 与 duration 均**承源**——不与本行所在会话的时间轴对账
{"ts": "2026-01-05T10:28:43.981411+08:00",
 "text": {"utterance": "小爱同学，把卧室的空调开到26度。",
          "entities": ["卧室", "空调", "26度"], "duration": 45.413886},
 "truth": {"session": 5, "sequence_class": "smart_home", "sequence": null,
           "tier_rank": 1, "frame_class": "task_request", "noise": false,
           "duplicate_of": 0}}
```

三条可直接在工件上读出来的事实：① 第 1 行与第 2 行分属交叉会话里的两条序列（`sequence` 0 与 1，同 `session: 0`）——交叉形态肉眼可读；② 第 1 行的 `duration = 71.053996` 对应的是**同序列下一帧**（第 3 行，ts `09:01:11.053996`）而非流水里的下一行（第 2 行，ts `09:00:56.012563`）——序内口径的直接体现；③ 第 26 行的 `duration = 45.413886` 等于其**源序列**首两帧（第 8、9 行）的 ts 差，与本行所在的流尾会话无关——承源语义的直接体现。

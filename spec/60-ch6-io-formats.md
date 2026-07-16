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
| inline（默认） | 用户 Schema 的全部字段平铺在顶层 + 保留键 `_meta`。校验语义：剥除 `_meta` 后的对象必须通过用户 Schema（M1 已禁止用户 Schema 声明 _meta，3.1.4）。 |
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
               "generator": null},   // v1.2 只增：生成记录为 {"llm", "style"}（3.6.2），否则 null
    "stream": null,                  // v1.8 恒在键（位置：source 之后、scores 之前——链序镜像）；
                                     // 未启用 segment = null。启用时（3.14/3.10.3）：
                                     // {"episode_id", "session_id", "order_span": [first, last], "member_count",
                                     //  "member_ids": [...], "member_sources": [{file, pair_index|line_no}, ...],
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
  //    [stitch 启用，v1.9] "stitch": {"stitched", "rescued_short", "seams", "judgments",
  //              "repass_judgments", "failures"},
  //    [extract 启用] "extract": {"transitions", "fallback_steps", "failures", "by_type": {<action_type>: n, ...}},
  //    [verify 启用]  "verify": {"membership_repairs", "boundary_flags", "defects": {<kind>: n, ...}}}
  //   —— sessions 数据源 = IngestReport（M2 属主，3.2）；below_min_len 独立于 noise 计数（S11，
  //      发生计数、v1.9 救援不回退——救援量另计 rescued_short，3.14.4）；digest_poor_frames =
  //      摘要贫瘠帧数（4.3 frame_digest 贫瘠判定）；stitch 子块 M16 属主（rescued_short 单位 = 帧、
  //      seams = 满足 T20 判据的拼接处数——接缝唯一计量点、judgments / repass_judgments =
  //      一遍/二遍判定调用数，3.16.6）；extract.by_type 为按动作类型分布（系统性劣化可观测，S14；
  //      v1.9 注：接缝占位步不计入 extract.transitions 与 by_type——非摘取产物，3.15.4）；
  //      verify 子块见 3.7 stream 分支（S31；defects 计数键 v1.9 起含 wrong_stitch，3.7.2）
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
  // v1.2 可选块："annotate": {"sc_disagreements": 0}（self-consistency 启用时）；
  //             "generate": {"buckets": {"default×concise": {"calls", "produced", "survived_dedup"[, "rejected_by_validator" — v1.5，仅配置 generate.sample_validator 时]}}}（generate 启用时）
  //             generate.buckets v1.7：classify 启用时桶 key 扩展为 "<class>×<llm>×<style>"（3.6.2 按类种子池行；关闭时格式不变）
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

（左侧新增 `dropped_noise` 与 `absorbed`（v1.8）及 `stitched`（v1.9 壳终态；fanout（右侧）计信封存在、stitched（左侧）计壳终态，二者分别记账无双记——经审计数值验证）、右侧新增 `episodes`；未启用的项恒 0，退化为上式）。`counts.threads` 不入守恒式——它是恒等式 `threads = episodes − stitched` 的导出量（M10 post-emit tally 单点上报，3.10.3；`rescued_short` 帧的 dropped_noise → absorbed 翻转发生在 emit 前、账目在路由时已定格，不破坏两侧平衡）。且 **stream 模式下 `counts.unprocessed` 的出现条件扩为「熔断 ∨ interrupted」**（S18：SIGINT 中断叠加会话缓冲会产生未走完流水线的残差；此时左侧另加 `unprocessed`，残差公式右侧 `+ episodes`、左侧 `+ absorbed + dropped_noise`（v1.9 另 `+ stitched`）同步扩展，failed 兜底公式减项同步——三处同步见 3.10.3 线索缝合行）；非 stream 模式中断残差恒 0、不加键（回归锚不动）。`schema_engine.resolved_at` 仅统计用户 Schema 的标注调用，加总 = 进入 M5 的记录数（4141+87+30+3+9 = 4270 = ingested 4987 − dropped_dup 412 − dropped_lowq 305）；裁决/评审/生成等内部 Schema 解析不计入。报告中无任何数据内容字段。

**rejects 通道 v1.8 增量**（完整格式规范属 3.11.2，此处登记 IO 面变化）：rejects 行的 (stage, reason) 组合新增三种——`segment / noise`（LLM 判噪声帧）、`segment / below_min_len`（短段丢弃帧，独立于 noise，S11）、`verify / off_task_member`（修复收缩弃帧，S31）；`--strict` 交互注意：stream 工程下噪声帧属预期产物，会触发退出码 1。**rejects 通道 v1.9 增量**：(stage, reason) 组合再增一种——`stitch / stitch_invalid`（仅 `stitch.on_error = "fail"` 时出现，3.16.6）；stitched 壳与被救援帧永不入 rejects（第四路由 / 翻转回 absorbed，3.11.2）——`--strict` 补注：同输入开启 stitch 后（短段被救援不再落 rejects）strict 结果可能由 1 变 0，属预期（2.4）。`output.rejects = "full"` 档对序列 Record 的原始载荷输出 `{"kind": "sequence", "member_ids": [...], "member_sources": [...]}`（S25——单记录 `_raw_payload` 假设的序列分支；`raw_last_output` 的 reason 门维持 schema_violation 现状，既有缺口明文接受）。

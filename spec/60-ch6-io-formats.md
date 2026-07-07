# 6. 输入 / 输出格式规格

## 6.1 输入：文本模态

UTF-8 编码 JSONL；每行一个 JSON object；行分隔符 `\n`；空行跳过（不计坏行）。示例（`input.text_field = "instruction"`）：

```
{"instruction": "帮我把这段话翻译成英文……", "source": "app-feedback", "ts": "2026-06-30T10:12:00Z"}
{"instruction": "写一份周报模板", "source": "ime-log", "ts": "2026-06-30T10:15:21Z"}
```

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
    "scores": {"screenshot_readability": 0.81, "tree_screen_consistency": 0.66,
               "state_completeness": 0.74, "interaction_richness": 0.52,
               "__aggregate__": 0.68, "mode": "pairwise_bt", "batch_no": 3},
                                       // scores v1.7 只增：classify 启用时另含 "pool"（= 类名，比较池自述，3.4.3 按类分池行）
    "dedup": {"kind": "unique"},
    "classification": null,            // v1.7 恒在键：classify 启用时为 {"label", "labels", "source"}（labels = 命中全集，single 恒单元素；3.13）；
                                       // 未启用 = null。multi 模式下行唯一键 = (_meta.id, classification.label)——同 id 可有多行（3.13.4 扇出行）
    "annotation": {"model": "qwen2.5-vl-72b-instruct", "attempts": 1},   // v1.2 只增：self-consistency 启用时另含 "sc": {"n", "agreement_ratio"}（3.5.2）
    "verification": {"verdict": "pass", "rounds": 1}          // verify 未启用则为 null
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

不变量：`emitted + dropped_* + failed + bad_input = scanned + generated`。熔断中止（v1.6 熔断交付，3.10.3）时扩展为 `emitted + dropped_* + failed + bad_input + unprocessed = scanned + generated`——`unprocessed` 仅此时出现，= 已扫描/已生成但因中止未走完流水线的记录数（M10 在 finalize 时按差额计算）。v1.7：`classify.assignment="multi"` 时右侧另加 `fanout`——`emitted + dropped_* + failed + bad_input = scanned + generated + fanout`；与熔断中止叠加时两项扩展并存（左侧 `+ unprocessed`、右侧 `+ fanout`，熔断残差公式同步，3.10.3 分类与扇出行）。`schema_engine.resolved_at` 仅统计用户 Schema 的标注调用，加总 = 进入 M5 的记录数（4141+87+30+3+9 = 4270 = ingested 4987 − dropped_dup 412 − dropped_lowq 305）；裁决/评审/生成等内部 Schema 解析不计入。报告中无任何数据内容字段。

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
    "dedup": {"kind": "unique"},
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
  "counts": {"scanned": 5000, "ingested": 4987, "bad_input": 13,
              "dropped_dup": 412, "dropped_lowq": 305, "dropped_verify": 41,
              "failed": 9, "generated": 0, "emitted": 4220},
  "dedup": {"exact": 118, "near_text": 201, "near_image": 46, "near_both": 47,
             "clusters": 366, "image_decode_failures": 2},   // v1.2：dedup.semantic 开启时另含 near_semantic 与 embedding_failures
  "quality": {"mode": "pairwise_bt", "rounds": 4, "judgment_failures": 17,
               "aggregate_histogram": {"0.0-0.1": 12, "...": 0},               // 10 桶
               "per_criterion_mean": {"screenshot_readability": 0.61},
               "per_criterion_tie_rate": {"screenshot_readability": 0.31}},   // v1.5 只增：仅 pairwise；分母为拿到裁决的比较数（调用级失败不计入，见 judgment_failures）
  "schema_engine": {"resolved_at": {"l0_or_clean": 4141, "l1": 87, "l3_1": 30,
                     "l3_2": 3, "rejected": 9}},
  // v1.2 可选块："annotate": {"sc_disagreements": 0}（self-consistency 启用时）；
  //             "generate": {"buckets": {"default×concise": {"calls", "produced", "survived_dedup"}}}（generate 启用时）
  "trace": {"enabled": true, "path": "./out/ui-labels-0701.trace.jsonl",
             "events": 18342, "dropped_events": 0},
  "llm_usage": {"default": {"calls": 31240, "prompt_tokens": 8.1e7,
                 "completion_tokens": 3.2e6, "est_cost_usd": 54.3, "retries": 210},
                "judge": {"...": 0}},
  "timing": {"wall_s": 5400, "per_stage_s": {"dedup": 40, "quality": 2900,
              "annotate": 1800, "verify": 620}}
}
```

不变量：`emitted + dropped_* + failed + bad_input = scanned + generated`。`schema_engine.resolved_at` 仅统计用户 Schema 的标注调用，加总 = 进入 M5 的记录数（4141+87+30+3+9 = 4270 = ingested 4987 − dropped_dup 412 − dropped_lowq 305）；裁决/评审/生成等内部 Schema 解析不计入。报告中无任何数据内容字段。

## 3.11 M11 输出器 emitter

### 3.11.1 职责与边界

**做：**三通道写出——主输出 JSONL（增量追加、终检、原子改名交付）、rejects 通道、report.json；组装 `_meta`；stderr 进度与结束摘要。 
**不做：**不修改标注内容；不做结构校验之外的任何数据加工（写出前调用 `SchemaEngine.validate_only` 做最后一道终检，失败即 bug——fail loudly，转 rejects 并记 `internal_error`）；报告不含数据内容。

### 3.11.2 写出规格

| 通道 | 规格 |
|---|---|
| 主输出 | 运行期写 `{output}.part`，每批 flush；finalize 时 fsync + 原子 rename 为目标名。行格式见 6.3。仅 `status="active"` 且（annotate 启用时）标注成功的记录写入。v1.6：熔断中止（退出码 4）的 finalize 同样执行交付（3.10.3 熔断交付）——已交付文件中每一行恒完整合法，运行是否完整处理了全部输入以 report.run 判定（interrupted=false 且 circuit_broken=false，3.11.3 ④）。 |
| rejects | `output.rejects = "none" \| "refs"（默认）\| "full"`。refs：每行仅 `{"_meta": {id, source, stage, reason, errors}}`——不含数据内容（source 亦不含 `passthrough_fields`，其值属数据内容），贴合不存储原则；full：额外含记录内容与最后一版非法输出（调试用，用户显式选择）。文件名 `{output_stem}.rejects.jsonl`。 |
| report.json | `{output_stem}.report.json`。结构见 6.4：运行参数摘要（脱敏，无 key）、各阶段计数、分数分布直方图、去重簇统计、结构引擎各层命中、token/成本、耗时、失败分类计数。 |

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
  "scores": {"writing_style": 0.72, "facts_trivia": 0.44, "educational_value": 0.61,
             "required_expertise": 0.35, "__aggregate__": 0.53,       // 等权均值 = 2.12/4
             "mode": "pairwise_bt", "batch_no": 1},
  "dedup": {"kind": "unique"},
  "annotation": {"model": "qwen2.5-vl-72b-instruct", "attempts": 1},  // attempts=1：未触发 L3
  "verification": null                                                // verify 未启用
}}
```

#### ② rejects = "refs"（默认）的一行

场景：第 213 行的标注输出经 L3 两次修复（`output.max_repair_attempts = 2`）仍未通过用户 Schema，M8 抛 `SchemaViolation`，记录置 `failed`（kind = `schema_violation`，7.6）转入 `out/ime-intent-0630.rejects.jsonl`。`errors` 即 M8 L2 `iter_errors()` 收集的全部违规（JSON Pointer 路径 + 期望 + 实际，3.8.2）；行内无任何数据内容——记录原文与 `raw_last_output` 仅在 `rejects = "full"` 时才写出。

```
# ── out/ime-intent-0630.rejects.jsonl 中的一行（折行排版）──
{"_meta": {
  "id": "c47d09e2b8a1f350",
  "source": {"file": "ime-2026-06.jsonl", "line_no": 213, "generated_from": []},
  "stage": "annotate",
  "reason": "schema_violation",
  "errors": [
    "/difficulty: 期望枚举 [\"easy\",\"medium\",\"hard\"] 之一，实际为 \"非常难\"",
    "/: 存在 Schema 未声明的字段 \"confidence\"（additionalProperties=false）"
  ]
}}
```

#### ③ 运行结束 stderr 摘要（逐字样例）

非 TTY、`--log-level info` 下的运行尾部。行格式为 7.3 的 `ts level stage batch msg`；数字即 3.10.4 走查：1000 条无坏行入流水线（4 批：256×3 + 232），尾批写出 184 行、失败 2 条；rejects 通道含重复 / 低质 / 失败三类（`output.rejects = "refs"`，3.11.2）。

```
2026-07-02T10:41:22+08:00 INFO  emitter batch=4 批 4/4 落盘：主输出 +184 行（累计 811），rejects +48（累计 189）
2026-07-02T10:41:23+08:00 INFO  emitter batch=- finalize：fsync + rename  out/ime-intent-0630.jsonl.part → out/ime-intent-0630.jsonl（811 行）
2026-07-02T10:41:23+08:00 INFO  emitter batch=- 已写出 out/ime-intent-0630.rejects.jsonl（189 行）与 out/ime-intent-0630.report.json
2026-07-02T10:41:23+08:00 INFO  orchestrator batch=- 运行结束：exit_code=0，wall=822s
   ── 终版摘要（与 report.counts 逐项一致）──
   scanned=1000  ingested=1000  bad_input=0  generated=0
   dropped_dup=97  dropped_lowq=78  dropped_verify=0  failed=14  emitted=811
```

不变量自查（6.4）：emitted 811 + dropped (97+78+0) + failed 14 + bad_input 0 = 1000 = scanned 1000 + generated 0。尾批自查：184 + 28(dup) + 18(lowq) + 2(failed) = 232；尾批 rejects = 28+18+2 = 48，四批累计 189 = 97+78+14。

#### ④ 原子改名交付时间线

运行全程只向 `out/ime-intent-0630.jsonl.part` 追加（每批 flush）；finalize 时 fsync 后一次 rename 为 `out/ime-intent-0630.jsonl`。因此目录中任一时刻要么只有 `.part`（运行中，或未走到 finalize 的硬崩溃 / 输出路径不可写），要么只有最终文件——目标文件名出现即保证**已交付的每一行完整且合法**，永远不会读到半截行。v1.6 起熔断中止同样交付（3.10.3 熔断交付），「目标文件出现」因此不再等价「全部输入处理完毕」：消费方判定运行完整性须看 report.run：`interrupted=false` **且** `circuit_broken=false`（退出码 0/1 不足——被 SIGINT 优雅中断的运行同样交付且以 0 退出，3.10.3 中断行）；熔断交付的主输出是「已完成批的完整前缀」，缺口可由 counts.unprocessed 核对（6.4 不变量扩展）。

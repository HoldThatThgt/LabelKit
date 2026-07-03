# 第 8 章　读懂四个产物：主输出、_meta、拒绝通道与运行报告

> 一次运行最多产出四个文件。本章逐字段解读它们，并给出几个「拿到产物之后」的实用姿势：
> 后筛、对账、剥离元信息。

## 8.1 产物一览

设 `run.output = "./out/labels.jsonl"`，同目录下会出现：

| 文件 | 何时产生 | 内容 |
|---|---|---|
| `labels.jsonl` | 恒有 | 主输出：每行 = 用户 Schema 字段（+ 可选 `_meta`） |
| `labels.rejects.jsonl` | `output.rejects ≠ "none"`（运行开始即创建；无淘汰时为 0 行空文件） | 被淘汰记录的案底 |
| `labels.report.json` | 恒有 | 运行报告：纯统计，无数据内容 |
| `labels.trace.jsonl` | `trace.enabled = true` | 事件流（第 16 章专讲） |

主输出的交付是**原子**的：运行中写 `labels.jsonl.part`，全部完成后 fsync + 改名。运行结束后仍看到 `.part` 文件，说明那次运行没走到交付——熔断（报告 `exit_code: 4`）或进程崩溃留下的残骸。注意：Ctrl-C 的**优雅中断**会正常收尾交付（`.part` 被改名、报告标记 `interrupted: true`），不留残骸。

## 8.2 主输出与 `_meta`：每行的完整履历

`meta_mode = "inline"`（默认）时，一行长这样（真实运行产物，格式化展示）：

```json
{
  "intent": "qa",                                    ← 你的 Schema 字段（顶层平铺）
  "topic": "光合作用暗反应与卡尔文循环",
  "difficulty": "medium",
  "_meta": {
    "id": "a8aa181766eebd97",                        ← 记录的确定性 id（第 4 章）
    "run": {                                         ← 这次运行的指纹
      "tool": "labelkit/1.0.0",
      "started_at": "2026-07-03T01:17:35.699878+08:00",
      "project_file": "project.toml",
      "rubric": "default:text",
      "seed": 42
    },
    "source": {                                      ← 溯源：这行数据从哪来
      "file": "input.jsonl",
      "line_no": 4,                                  ← 文本模态：行号；UI 模态换成 pair_index
      "generated_from": [],                          ← 若是合成样本：种子记录的 id 列表
      "fields": {"source": "ime-log"},               ← passthrough_fields 透传的原始字段
      "generator": null                              ← 若是合成样本：{"llm": "...", "style": "..."}
    },
    "scores": {                                      ← 质量分（quality 开启时）
      "educational_value": 0.6,                      ← 每条准则一个 [0,1] 分
      "facts_trivia": 0.8,
      "writing_style": 0.4,
      "required_expertise": 0.6,
      "__aggregate__": 0.6000000000000001,           ← 加权聚合分（质量门用的就是它；浮点尾数正常）
      "mode": "pointwise",                           ← 打分模式；pairwise 时为 "pairwise_bt"
      "batch_no": 1                                  ← 在第几批打的分（pairwise 下跨批不可比）
    },
    "dedup": {"kind": "unique"},                     ← 去重判定（存活者恒为 unique）
    "annotation": {"model": "glm-5.2", "attempts": 1},  ← 标注用的模型与尝试次数
    "verification": null                             ← verify 未启用为 null；启用后 {"verdict","rounds"}
  }
}
```

几个字段的深意：

- **`annotation.attempts`**：1 = 一次通过；2 = 经过一轮结构修复才合法（第 14 章）。批量看这个字段能感知「模型输出结构的稳定性」。开了 self-consistency 时另含 `sc: {n, agreement_ratio}`，此时 attempts 是各合法样本尝试次数之和（与 `sc.n` 对照才有意义，见第 11 章）。
- **`scores.batch_no`**：pairwise 模式下分数是批内相对量，跨批比较分数时先看这个字段是不是同一批（第 10 章反复强调）。
- **`generator` / `generated_from`**：区分真实与合成数据的**唯一可靠判据**是 `generator ≠ null`（`generated_from` 在纯生成模式下恒为空数组，不可作判据）。
- **校验语义**：inline 模式下「剥除 `_meta` 后的对象」保证通过你的 Schema。启动时已禁止用户 Schema 声明 `_meta`，不会撞名。

`meta_mode = "sidecar"` 时主输出是纯用户结构，`_meta` 逐行写 `{stem}.meta.jsonl`，行序与主输出对齐、以 id 关联。`none` 则彻底不产元信息——分数与溯源都没了，除非下游明确拒绝任何附加字段，否则别选它。

### 下游常用姿势

```bash
# 1. 按聚合分后筛：门槛留宽、下游收紧（拿高分子集，同时剥掉 _meta）
jq -c 'select(._meta.scores["__aggregate__"] >= 0.6) | del(._meta)' \
   out/labels.jsonl > out/labels.hq.jsonl

# 2. 只剥 _meta，得到纯净训练文件
jq -c 'del(._meta)' out/labels.jsonl > out/labels.clean.jsonl

# 3. 只看合成样本
jq -c 'select(._meta.source.generator != null)' out/labels.jsonl

# 4. 统计意图分布
jq -r '.intent' out/labels.jsonl | sort | uniq -c
```

这就是「门控可以留宽」策略的基础：分数随行落盘后，**当次没淘汰的，下游随时可以再筛**；而当次淘汰掉的，想找回来就得重跑。拿不准阈值时，宁可放宽 `quality.threshold` 甚至不设，把裁量权留给后筛。

## 8.3 拒绝通道：淘汰者的案底

`rejects = "refs"`（默认）档，每行长这样：

```json
{"_meta": {"id": "6e60ce3c2d59f04d",
           "source": {"file": "input.jsonl", "line_no": 1, "generated_from": []},
           "stage": "quality", "reason": "below_threshold", "errors": []}}
```

- `stage` + `reason` 告诉你**在哪个工位、因为什么**被淘汰。常见组合：`dedup` / 判重类别（`exact` / `near_text` / `near_image` / `near_both`，开语义层时另有 `near_semantic`，与第 9 章一致）、`quality / below_threshold`（top_ratio 模式下为 `top_ratio`）、`verify / verify_fail`；记录处理**失败**（状态 `failed`）时，`stage` 为出错工位、`reason` 为首个错误的**错误码**（如 `schema_violation`、`provider_fatal`，全表见第 18 章），`errors` 列表为具体的错误信息文本；
- `refs` 档**不含数据内容**——想看被淘汰的原文，要么拿 `line_no` 回输入文件查，要么把 `rejects` 改成 `"full"`（原文随行落盘，注意这就是一份数据副本了）；
- `rejects = "none"` 时不写此文件，淘汰只反映在报告计数里。**调优期强烈建议至少 refs**：质量门帮你扔掉了什么，是判断阈值合不合理的第一手材料。

## 8.4 report.json 逐节解读

以下是一次真实运行的完整报告（14 条输入的 quickstart 工程）：

```json
{
  "run": {
    "tool_version": "1.0.0",
    "started_at": "2026-07-03T01:17:35.699878+08:00",
    "finished_at": "2026-07-03T01:19:03.331413+08:00",
    "interrupted": false,                ← 仅 SIGINT/SIGTERM 优雅中断时为 true
    "circuit_broken": false,             ← 熔断的显式标志（触发时为 true，exit_code 同为 4）
    "exit_code": 0,
    "modality": "text",
    "seed": 42,
    "config_digest": "sha256:9c92d09a…", ← 两份配置文件的指纹：
    "project_digest": "sha256:fbf42019…"    对账"这份产物是哪套配置跑的"就靠它
  },
  "counts": {                            ← 过磅单（守恒等式见第 4 章）
    "scanned": 14, "ingested": 14, "bad_input": 0,
    "dropped_dup": 1, "dropped_lowq": 5, "dropped_verify": 0,
    "failed": 0, "generated": 0, "emitted": 8
  },
  "dedup": {                             ← 去重明细：各层各拦了几条
    "exact": 1, "near_text": 0, "near_image": 0, "near_both": 0,
    "clusters": 1,                       ← 重复簇个数
    "image_decode_failures": 0           ← 解码失败（跳过图像层）的张数
  },
  "quality": {
    "mode": "pointwise",
    "rounds": 4,
    "judgment_failures": 0,              ← 裁决输出不合法的次数（>5% 要警惕，见第 16 章）
    "aggregate_histogram": {             ← 聚合分 10 桶直方图：
      "0.0-0.1": 3, "0.1-0.2": 2, "0.2-0.3": 2, "0.3-0.4": 0,
      "0.4-0.5": 3, "0.6-0.7": 2, "0.7-0.8": 1, "…": 0
    },                                       画质量线之前先看它！
    "per_criterion_mean": {              ← 每条准则的均值：哪条准则在拖后腿一目了然
      "educational_value": 0.308, "facts_trivia": 0.215,
      "required_expertise": 0.323, "writing_style": 0.400
    }                                    ← pairwise 模式下均值恒 ≈0.5，另有 per_criterion_tie_rate
  },                                        （每准则平局率，只统计拿到裁决的比较——rubric 区分度的直接读数）
  "schema_engine": {                     ← 结构引擎四层的命中分布（第 14 章）
    "resolved_at": {"l0_or_clean": 7, "l1": 1, "l3_1": 0, "l3_2": 0, "rejected": 0}
  },
  "trace": {"enabled": true, "path": "out/text-labels.trace.jsonl",
             "events": 71, "dropped_events": 0},
  "llm_usage": {                         ← 分 profile 的用量账单
    "default": {"calls": 61, "prompt_tokens": 21683,
                 "completion_tokens": 6831, "retries": 0}
  },                                     ← 配了单价时另有 est_cost_usd
  "timing": {"wall_s": 87.4,
              "per_stage_s": {"dedup": 0.004, "quality": 76.1, "annotate": 11.3}}
}
```

（分数与耗时是真实运行的快照，逐次运行会有浮动；计数守恒与字段结构不变。）

读报告的三板斧：

1. **先看 `counts` 对不对账**——各状态数量符合预期吗？`failed` 非零就去拒绝通道翻 `errors`；
2. **再看 `quality.aggregate_histogram`**——分布形状决定阈值画哪里。比如上面这份：0.25 的线落在 0.2-0.3 桶内，被淘汰的五条恰是 0~0.2 两桶的全部（3+2）；0.2-0.3 桶的两条聚合分正好等于 0.25，质量门按「< 阈值才淘汰」放行，都留下了。如果直方图整体右移，同一条线就几乎不淘汰东西；
3. **最后看 `llm_usage` 和 `timing`**——哪个阶段最烧钱/最耗时（几乎总是 quality），是否要换模式、调并发（第 17 章）。

另有两个按需出现的块：`annotate.sc_disagreements`（开 self-consistency 时：全体分歧、回退首样本的次数）与 `generate.buckets`（开生成时：每个「模型×风格」桶的调用数 / 产出数 / 去重存活数，配置 `sample_validator` 时另有回调剔除数 `rejected_by_validator`——某桶存活率明显低说明它在产重复货或不合规货，第 12 章）。

> **报告写失败怎么办**：主输出成功、报告写失败时，进程以退出码 1 结束——产物可用但账本缺失，别当成功处理。

## 8.5 产物管理的三个提醒

1. **同一输出路径重跑会覆盖全部产物**。trace 文件在**首个事件写出时**截断——死于配置或输入校验的「秒败」运行不会碰它，但正常启动的重跑会。正式任务建议输出文件名带日期/批次号：`out/ime-intent-0703.jsonl`；
2. **`--dry-run` 的产物写独立文件**：`{stem}.dryrun.report.json` 与 `{名}.dryrun{后缀}` 的 trace，不会覆盖上一次真实运行的账本，放心试跑；
3. **rejects=full / trace 高档位的文件里有数据**，清理和保管是你的责任——LabelKit 只在你显式选择时才写它们。

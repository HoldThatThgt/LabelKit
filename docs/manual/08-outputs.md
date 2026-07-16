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

主输出的交付是**原子**的：运行中写 `labels.jsonl.part`，全部完成后 fsync + 改名。运行结束后仍看到 `.part` 文件，说明那次运行没走到交付——进程硬崩溃或输出路径不可写留下的残骸。注意：Ctrl-C 的**优雅中断**会正常收尾交付（`.part` 被改名、报告标记 `interrupted: true`），不留残骸；v1.6 起**熔断中止也交付**——已完成批的 `.part` 同样 fsync + 原子改名，退出码仍是 4（此前版本熔断直接丢弃 `.part`，长跑末段一次配额死亡就赔掉全部已完成产出）。

> **消费方判定规则变了（v1.6）**：最终文件名出现，仍然保证**已交付的每一行完整且合法**——永远读不到半截行；但它**不再等价于「全部输入处理完毕」**。判定一次运行是否完整，唯一可靠的信号是报告里的 `run.interrupted = false` **且** `run.circuit_broken = false`。退出码不充分：优雅中断的运行同样交付且以 0 退出，熔断交付则以 4 退出但文件照样出现。熔断交付的主输出是「已完成批的完整前缀」，缺了多少可拿 `counts.unprocessed` 对账（见 8.4 节）。下游若有自动消费流水线，把这条判定写进去。

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
      "started_at": "2026-07-14T04:56:31.624648+08:00",
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
    "stream": null,                                  ← 时序流元信息（v1.8 恒在键；未启用恒为 null，第 25 章；
                                                        v1.9 缝合启用时另含 thread_id / fragments 等线索键，第 26 章）
    "scores": {                                      ← 质量分（quality 开启时）
      "writing_style": 0.4,                          ← 每条准则一个 [0,1] 分
      "facts_trivia": 0.6,
      "educational_value": 0.8,
      "required_expertise": 0.6,
      "__aggregate__": 0.6,                          ← 加权聚合分（质量门用的就是它）
      "mode": "pointwise",                           ← 打分模式；pairwise 时为 "pairwise_bt"
      "batch_no": 1                                  ← 在第几批打的分（pairwise 下跨批不可比）
    },
    "dedup": {"kind": "unique"},                     ← 去重判定（存活者恒为 unique）
    "classification": null,                          ← 分类结果（v1.7 恒在键；classify 未启用恒为 null）
    "annotation": {"model": "glm-5.2", "attempts": 1},  ← 标注用的模型与尝试次数
    "verification": null                             ← verify 未启用为 null；启用后 {"verdict","rounds"}
  }
}
```

几个字段的深意：

- **`annotation.attempts`**：1 = 一次通过；2 = 经过一轮结构修复才合法（第 14 章）。批量看这个字段能感知「模型输出结构的稳定性」。开了 self-consistency 时另含 `sc: {n, agreement_ratio}`，此时 attempts 是各合法样本尝试次数之和（与 `sc.n` 对照才有意义，见第 11 章）。
- **`scores.batch_no`**：pairwise 模式下分数是批内相对量，跨批比较分数时先看这个字段是不是同一批（第 10 章反复强调）。
- **`classification`**（v1.7 恒在键）：分类算子未启用时恒为 `null`；启用后为 `{"label", "labels", "source"}`——`label` 是本行的路由类别，`labels` 是该记录命中的类别全集（multi 模式下同一 `id` 可产多行，行唯一键变为 (`id`, `label`)），`source` 标记标签来源（`llm` / `fallback` / `inherited`）。启用时 `scores` 里另出现 `pool` 键（= 类名，自述这行分数是在哪个类池里打的）。详见第 24 章。
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

- `stage` + `reason` 告诉你**在哪个工位、因为什么**被淘汰。常见组合：`dedup` / 判重类别（`exact` / `near_text` / `near_image` / `near_both`，开语义层时另有 `near_semantic`，与第 9 章一致）、`quality / below_threshold`（top_ratio 模式下为 `top_ratio`）、`verify / verify_fail`；stream 模式（第 25 章）另有 `segment` / `noise`、`segment` / `below_min_len` 与 `verify` / `off_task_member`；v1.9 的 `stitch` / `stitch_invalid` 仅在 `stitch.on_error = "fail"` 时出现（缝合判定失败的 episode 候选信封，第 26 章）。记录处理**失败**（状态 `failed`）时，`stage` 为出错工位、`reason` 为首个错误的**错误码**（如 `schema_violation`、`provider_fatal`，全表见第 18 章），`errors` 列表为具体的错误信息文本。反向的提醒：缝合产生的 `stitched` 壳与救援命中的短段帧**不落 rejects**——救援把帧从 `dropped_noise` 翻回 `absorbed`，同一份输入开启缝合后 rejects 行数可能变少（`--strict` 交互见 8.4 末尾）；
- `refs` 档**不含数据内容**——想看被淘汰的原文，要么拿 `line_no` 回输入文件查，要么把 `rejects` 改成 `"full"`（原文随行落盘，注意这就是一份数据副本了）；
- `rejects = "none"` 时不写此文件，淘汰只反映在报告计数里。**调优期强烈建议至少 refs**：质量门帮你扔掉了什么，是判断阈值合不合理的第一手材料。

## 8.4 report.json 逐节解读

以下是一次真实运行的完整报告（14 条输入的 quickstart 工程）：

```json
{
  "run": {
    "tool_version": "1.0.0",
    "started_at": "2026-07-14T04:56:31.624648+08:00",
    "finished_at": "2026-07-14T04:57:51.322560+08:00",
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
    "dropped_dup": 1, "dropped_lowq": 6, "dropped_verify": 0,
    "failed": 0, "generated": 0, "emitted": 7
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
      "0.0-0.1": 3, "0.1-0.2": 1, "0.2-0.3": 3, "0.3-0.4": 1,
      "0.4-0.5": 1, "0.5-0.6": 0, "0.6-0.7": 4, "…": 0
    },                                       画质量线之前先看它！
    "per_criterion_mean": {              ← 每条准则的均值：哪条准则在拖后腿一目了然
      "educational_value": 0.35384615384615387, "facts_trivia": 0.24615384615384617,
      "required_expertise": 0.2769230769230769, "writing_style": 0.4
    }                                    ← pairwise 模式下均值恒 ≈0.5，另有 per_criterion_tie_rate
  },                                        （每准则平局率，只统计拿到裁决的比较——rubric 区分度的直接读数）
  "schema_engine": {                     ← 结构引擎四层的命中分布（第 14 章）
    "resolved_at": {"l0_or_clean": 5, "l1": 2, "l3_1": 0, "l3_2": 0, "rejected": 0}
  },
  "trace": {"enabled": true, "path": "out/text-labels.trace.jsonl",
             "events": 73, "dropped_events": 0},
  "llm_usage": {                         ← 分 profile 的用量账单
    "default": {"calls": 61, "prompt_tokens": 22887,
                 "completion_tokens": 5676, "retries": 0}
  },                                     ← 配了单价时另有 est_cost_usd
  "timing": {"wall_s": 79.491,
              "per_stage_s": {"dedup": 0.004, "quality": 70.044, "annotate": 9.436}}
}
```

（分数与耗时是真实运行的快照，逐次运行会有浮动；计数守恒与字段结构不变。这份 text 报告里**没有** `stream` 节——它与 `counts` 里的 `episodes` / `absorbed` / `dropped_noise` 一样，仅 stream 模式（segment 启用）时出现，第 25 章。）

读报告的三板斧：

1. **先看 `counts` 对不对账**——各状态数量符合预期吗？`failed` 非零就去拒绝通道翻 `errors`；
2. **再看 `quality.aggregate_histogram`**——分布形状决定阈值画哪里。比如上面这份：0.25 的线落在 0.2-0.3 桶内，被淘汰的六条 = 0~0.2 两桶的全部（3+1）加 0.2-0.3 桶里两条 0.2 的；桶里剩下那条聚合分正好等于 0.25，质量门按「< 阈值才淘汰」放行，留下了。如果直方图整体右移，同一条线就几乎不淘汰东西；
3. **最后看 `llm_usage` 和 `timing`**——哪个阶段最烧钱/最耗时（几乎总是 quality），是否要换模式、调并发（第 17 章）。

另有两个按需出现的块：`annotate.sc_disagreements`（开 self-consistency 时：全体分歧、回退首样本的次数）与 `generate.buckets`（开生成时：每个「模型×风格」桶的调用数 / 产出数 / 去重存活数，配置 `sample_validator` 时另有回调剔除数 `rejected_by_validator`——某桶存活率明显低说明它在产重复货或不合规货，第 12 章）。

v1.6 增补了三处按需出现的字段（不出现时语义同旧版，已有的报告解析脚本不受影响）：

- **`run.partial_delivery`**：仅熔断交付时出现且恒为 `true`（恒伴随 `circuit_broken: true`）——标记这份主输出是**部分交付**，消费方完整性判定见 8.1 节的警告框；
- **`counts.unprocessed`**：仅熔断中止时增列——已扫描/已生成但因中止没走完流水线的记录数。守恒等式相应扩展为 `emitted + dropped_* + failed + bad_input + unprocessed = scanned + generated`（第 4 章的原式是它在 `unprocessed = 0` 时的特例）；
- **`llm_usage` 的密钥池明细**（profile 用第 6 章的 `api_key_envs` 配了多把密钥时）：profile 对象增 `"keys": {"<环境变量名>": {"calls", "rate_limited", "disabled"}}`，按密钥拆分调用数、被限流（429）次数与是否被认证禁用——密钥一律以**环境变量名**标识，密钥值不会出现在任何日志或报告里；另有 `parked_calls` / `parked_ms`（池 >1 或数值非零时出现——单密钥驻留也留痕）：因「全部存活密钥都在限流冷却」而**驻留**等待的逻辑调用数与累计毫秒数。`disabled` 非零该换密钥，`parked_ms` 持续走高说明并发压过了密钥池的配额承受力——该加密钥或降 `max_concurrency`（驻留上限 `run.max_park_s` 见第 7 章；对应的 `llm.key_cooldown` / `llm.key_disabled` / `llm.pool_parked` 事件见第 16 章）。

v1.7（分类算子，第 24 章）再增三处按需出现的字段（未启用时报告形状与旧版逐字段一致）：

- **`classify` 节**（仅 `classify.enabled = true` 时出现）：`assignment`、逐类命中计数 `classes`、兜底归类数 `fallback_count` 与失败数 `failures`（multi 模式另有 `multi_label_records`）；`quality` 节同时增 `by_class` 分池统计——各池独立的直方图与准则均值；
- **`counts.fanout`**（仅 `assignment = "multi"` 时增列）：多标签扇出净增的行数，守恒等式右侧相应 `+ fanout`（第 4 章）；
- **`generate.buckets` 的桶 key**：classify 启用时由「`<llm>×<style>`」两段扩展为「`<class>×<llm>×<style>`」三段（关闭时格式不变，第 12 章）。

v1.8（时序流，第 25 章）再增两处按需出现的块（未启用时报告形状与旧版逐字段一致）：`counts` 增列 `episodes` / `absorbed` / `dropped_noise`，且 `counts` 之后新增顶层 `stream` 节（会话数、段长均值、`below_min_len`、摘要贫瘠帧数，extract / verify 各一个子块）——两者都仅 segment 启用时出现。守恒等式相应扩展为全展开形：左侧另加 `dropped_noise + absorbed`、右侧另加 `episodes`（未启用项恒 0 时退化回第 4 章原式；真实验算见第 25 章）；且 stream 模式下 `counts.unprocessed` 的出现条件从「仅熔断」扩为「熔断或优雅中断」。

v1.9（线索缝合，第 26 章）再增两处按需出现的字段（仅 `stitch.enabled = true` 时出现，未启用时报告与 v1.8 逐字节一致）：`counts` 增列 `stitched` / `threads`（被并进线索的 episode 壳数、线索数，恒满足 `threads = episodes − stitched`），`stream` 节内新增 `stitch` 子块（`{stitched, rescued_short, seams, judgments, repass_judgments, failures}`，逐键读法见第 26 章）；守恒等式左侧相应另加 `stitched`（第 4 章）。一处**无条件**的例外：stream×verify 的缺陷词表是闭集，`stream.verify.defects` 从五行扩为六行——即便 stitch 关闭，`wrong_stitch: 0` 这一行也在场（第 13、25 章）。`--strict` 交互提醒：stitched 壳与被救援的帧都不构成 rejects，同一份输入开启缝合后 `--strict` 的结果可能从 1 变 0（短段被救援、不再落 rejects）——属预期，不是账目错误。

> **报告写失败怎么办**：主输出成功、报告写失败时，进程以退出码 1 结束——产物可用但账本缺失，别当成功处理。

## 8.5 产物管理的三个提醒

1. **同一输出路径重跑会覆盖全部产物**。trace 文件在**首个事件写出时**截断——死于配置或输入校验的「秒败」运行不会碰它，但正常启动的重跑会。正式任务建议输出文件名带日期/批次号：`out/ime-intent-0703.jsonl`；
2. **`--dry-run` 的产物写独立文件**：`{stem}.dryrun.report.json` 与 `{名}.dryrun{后缀}` 的 trace，不会覆盖上一次真实运行的账本，放心试跑；
3. **rejects=full / trace 高档位的文件里有数据**，清理和保管是你的责任——LabelKit 只在你显式选择时才写它们。

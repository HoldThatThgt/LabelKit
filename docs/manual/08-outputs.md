# 第 8 章　读懂输出：记录、序列与提交真值

> 普通 process/flat run 与 sequence run 共用主输出和报告概念，但 sequence 另有 stream、manifest 和 failed report。
> 不要把 trace 或 failed report 当作成功提交证明。

## 8.1 产物矩阵

普通 process 或 flat generation：

| 产物 | 何时出现 | 用途 |
|---|---|---|
| `labels.jsonl` | 成功交付 | 主输出，每行是用户 Schema 字段与可选 `_meta` |
| `labels.rejects.jsonl` | `output.rejects != "none"` | 被淘汰或失败记录 |
| `labels.report.json` | 正常收尾 | 计数、质量、Schema、usage 与 timing |
| `labels.trace.jsonl` | `trace.enabled = true` | 调用与判定事件 |

sequence generation 固定 `output.meta_mode = "inline"`、`output.rejects = "none"`：

| 产物 | 路径 | 用途 |
|---|---|---|
| main | `labels.jsonl` | 每行一条最终 sequence |
| stream | `labels.stream.jsonl` | primary、noise 与 replay event rows |
| success report | `labels.report.json` | 精确交付、调用、拒绝尝试与 usage |
| manifest | `labels.manifest.json` | main/stream/report 的唯一成功提交真值 |
| failed report | `labels.failed.report.json` | 最近一次失败诊断，不属于成功 manifest |

sequence `--dry-run` 只在 console 输出计划与估算，不创建、截断或替换上面五个固定文件。普通 dry-run
仍使用自己既有的 dry-run report/trace 规则。

## 8.2 普通主输出与 `_meta`

主输出顶层先放用户 Schema 的字段；`output.meta_mode = "inline"` 时再放保留键 `_meta`。常用区域是：

- `_meta.id`：确定性记录 ID；
- `_meta.source`：输入路径、行号、透传字段与生成来源；
- `_meta.classification`：分类标签、来源与置信信息；
- `_meta.quality`：rubric 分项与聚合分；
- `_meta.annotation`：结构化标注的执行履历；
- `_meta.verification`：评审结论与修复次数；
- `_meta.stream`：process stream 的 session/episode/member 结构；
- `_meta.regions`：图片模式的候选几何与参考框，只在 `segment.mode = "image"` 时出现（见下）；
- `_meta.run`：配置摘要与运行观测。

普通流的 `member_positions` 记录完整会话中的出现位置，重复内容 ID 不会折叠成员。
`capacity` 描述人工容量边界、sealed 状态和 root/parent 谱系；只有最终会话尝试进入主输出。
`report.stream.capacity` 给出封闭、拆分、重算、最小失败与保留帧数高水位，失败尝试的用量和 trace 仍保留。
这条普通流提交路径没有新增 manifest，也不提供跨进程状态恢复。

用户 Schema 不得声明 `_meta`；它由 emitter 统一装配。要剥离元信息：

```bash
jq -c 'del(._meta)' out/labels.jsonl > out/labels.clean.jsonl
```

### 图片模式：`_meta.regions`（v1.22）

`segment.mode = "image"` 时 `_meta` 多一个顶层键 `regions`，位于 `stream` 之后、`scores` 之前，`stream` 恒为 `null`。实跑的顶层键序：

```text
id, run, source, stream, regions, scores, dedup, classification, annotation, verification
```

这是通用信封里第一个顶层条件键：非图片模式没有这个键，非图片工程的输出字节不变。图片模式每条主输出的 `regions` 恒为对象，annotate 关闭的只分割运行也一样。

`examples/image-segmentation/project-boxes.toml` 实跑写出的内容（只用参考框的路径，几何来自钩子，segment 不调用模型；这里只节选 `n1`）：

```json
"regions": {
  "image": {"width": 540, "height": 960},
  "boxes": [{"id": "n1", "bbox": [60, 300, 480, 380], "text": "Button Sign in"}],
  "items": [
    {"region_id": "n1",
     "geometry": {"type": "Polygon",
                  "coordinates": [[[60, 300], [480, 300], [480, 380], [60, 380], [60, 300]]]},
     "box_id": "n1",
     "source_box_ids": ["n1"]}
  ]
}
```

| 键 | 含义 |
|---|---|
| `image` | 原图存储像素宽高 |
| `boxes` | 钩子返回并通过校验的参考框，`id` 恒为非空字符串（钩子全部留空时是框架补的 `"1"…"N"`）；未注册钩子或无伴随文件时为空数组 |
| `items[].region_id` | 来自参考框：框名；模型新画：框架按出现顺序编号 `r1, r2, …`，复用区域不占编号 |
| `items[].geometry` | GeoJSON 形状。单部件 `{"type": "Polygon", "coordinates": [外环, *内环]}`；多部件 `{"type": "MultiPolygon", "coordinates": [[外环, *内环], ...]}`。环闭合，顺序与顶点顺序不变 |
| `items[].box_id` | 几何取自哪个参考框；模型画的多边形为 `null` |
| `items[].source_box_ids` | 模型声明参考过的框（已去重）；`box_id` 非空时恒为 `[box_id]` |

**单位与精度。**`geometry` 是原图像素，不取整。来自参考框的坐标保留钩子给出的数值类型，精度无损；模型画的多边形恒为浮点，顶点只能落在 0–1000 网格上，所以最细单位是 x 方向 `width / 1000` 像素、y 方向 `height / 1000` 像素。这是网格的分辨率，不是模型的实测精度：一次首轮真实运行里（一个模型、少量合成图片、原因没有证实），模型画的几何在横屏图上不稳定，当作几何使用前先对照原图核对（第 28 章 28.5）。

**这个块是运行留下的唯一几何记录。**它记录这次运行得到的坐标，不证明这些坐标画得准。图片模式不新增任何输出文件，上面的产物矩阵不变。`output.meta_mode = "sidecar"` 时它写进 `{stem}.meta.jsonl`；`"none"` 是配置错误。叠图不落盘，运行结束后不能复现模型当时看到的叠图字节。

空候选是合法结果：`"items": []` 与 `report.segment_image.empty` 计数，这条记录不进 rejects。

**记录载荷。**annotate 关闭时的主输出行，以及 `output.rejects = "full"` 的 `record`，图片模式下实跑形态都是：

```json
{"image_path": ".../image_1.png", "companion_path": ".../image_1.xml"}
```

没有伴随文件时没有 `companion_path`。没有 `ui_tree` 键，也不内联伴随文件字节。

**报表。**`report.segment_image` 只在图片模式出现，位于 `counts`（及 `estimate`）之后：

| 键 | 语义 |
|---|---|
| `calls` | 提交的分割模型任务数，不含修复轮；只用参考框的路径恒为 0 |
| `samples` | 进入 segment 阶段的活跃记录数 |
| `boxes`、`regions` | 成功记录的参考框数、候选数之和 |
| `empty` | 空候选的记录数 |
| `failed` | `{错误码: 条数}`，例如 `{"companion_invalid": 1}`，无失败时 `{}` |
| `usage` | 分割调用的 `prompt_tokens`、`completion_tokens`，含修复穷尽的调用；用来拆分 segment 与 annotate 共用同一 profile 时的成本 |

`report.counts` 在图片模式多一个 `ignored_files`（第 5 章 5.6），不参与守恒式；非图片模式没有这个键。

## 8.3 sequence main truth

declared main row 的 `_meta.generation` 至少包含：

```json
{
  "validation_mode": "declared",
  "actor_knowledge_validation": "mechanical_and_semantic",
  "scenario_set": "booking_success_training",
  "scenario_index": 0,
  "scenario_id": "0123...",
  "world_branch_id": "4567...",
  "sequence_class": "ticket_booking",
  "pattern": "booking_success",
  "variant": "confirmation_timeout",
  "expected_violation": {
    "kind": "gap_above_max",
    "target": "acknowledge_to_confirm"
  },
  "actual_violations": [
    {"kind": "gap_above_max", "target": "acknowledge_to_confirm"}
  ]
}
```

instruction-only row 固定写 `validation_mode = "instruction_only"` 与
`actor_knowledge_validation = "semantic"`，并带 instruction slot、scenario/index、world branch 和 sequence class。
它不得出现 scenario set、pattern、variant 或 expected violation。

main row 来自最终下游 `PipelineItem`，因此继承 classification、quality、sequence/frame annotation 和
verification 都必须在最终投影之后装配。不能从下游之前的预投影 Record 回读。

## 8.4 stream event envelope

stream 每行顶层固定为 `payload` 与 `_meta`。primary event 的核心形状：

```json
{
  "payload": {
    "request_id": "R-100",
    "ticket_id": "T-100",
    "timestamp": 1767575760000
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

同一 owner 的事件按声明顺序留在 main 的 members 中；整个 stream 按最终 timestamp 全局稳定排序，因此交织 session
可以呈现真实的 A-B-A 或 B-A-B。`_meta.event.timestamp` 是权威起点；descriptor 中声明的 payload 业务时间由
框架按同一计划机械写入。模型不会生成这些时间叶子，M11 也只复验而不修复。

特殊行：

- noise：owner、role、scenario 与 world branch 都为空，`noise = true`；
- replay：owner 为空，另有 replay sequence ID、replay ordinal、source sequence ID 与 source event ID；
- instruction-only role：固定为 `position_000`、`position_001` 等位置名，不伪装成 declared 业务 role。

`project-replay.toml` 使用：

```toml
[input]
text_field = "payload"

[stream]
order_by = "meta:_meta.event.timestamp"
```

M2 从同一 stream 文件重算 primary/noise/replay binding、event ID、每个 owner 的 ordered sequence ID、
replay sequence/event ID 和 duplicate provenance。它用自描述的 `duration_us`、`resources` 与 `time_bindings`
验证外层时间和 payload 时间；replay 必须保持 source 的非时间内容、duration/resources 与成员 start delta，并对全部
成员使用同一个正 `shift_us` 重新绑定 payload。它不依赖 main 文件；descriptor、格式、唯一性、顺序、payload 或
provenance 任一失配都会 fail closed。

## 8.5 manifest：消费者唯一信任的成功标记

manifest 在 main、stream、report 都完成 fsync 和原子替换后最后提交：

```json
{
  "schema_version": 1,
  "run_id": "0123...",
  "delivery_digest": "4567...",
  "artifacts_committed": true,
  "main": {
    "path": "/abs/out/labels.jsonl",
    "sha256": "...",
    "rows": 8
  },
  "stream": {
    "path": "/abs/out/labels.stream.jsonl",
    "sha256": "...",
    "rows": 27
  },
  "report": {
    "path": "/abs/out/labels.report.json",
    "sha256": "..."
  },
  "committed_at": "..."
}
```

消费者必须：

- 读取 manifest，而不是通过“文件存在”猜测成功；
- 校验 `artifacts_committed = true`；
- 校验三个路径和 SHA-256；
- 校验 report 中的 `run_id`、`delivery_digest` 与 manifest 相等；
- 校验 main/stream 行数。

commit-I/O 失败可能已经替换了 main、stream、report 的子集，但旧 manifest 保持不变。此时 hash 校验一定能拒绝
混代集合；产品不承诺把所有固定路径回滚成上一版。

## 8.6 `report.generate.sequence`

教学主例的成功报告必须对上已验证的 keyless 计划，并在 live 证据完成后证明 delivered 与 planned 相等：

| 字段 | 验收关系 |
|------|----------|
| `mode` | `declared` |
| `planned_sets` | `2` |
| `planned_sequences` | `8` |
| `primary_events` | `22` |
| `noise_events` | `2` |
| `replay_events` | `3` |
| `stream_rows` | `27`，即 `22 + 2 + 3` |
| `delivered_sets` / `delivered_sequences` | live 成功后分别严格等于 planned 值 |
| `program_digest` / `plan_digest` | 分别绑定编译后的配置语义与冻结 ScenarioPlan |
| `interleaving_opportunities` | 主例为 `0`；一般语义是 trigger 扫描时至少有一个可用 partner pool 的抽取次数 |
| `primary_sessions` | 主例为 `8`；一般满足 `N - D`，其中 `N` 是可见 primary branch 数、`D` 是冻结交织布局数 |
| `interleaved_primary_sessions` | 主例为 `0`；一般等于 `D`，一条交织布局形成一个双 owner session |
| `by_interleaving_pattern` | 主例为 `{}`；启用时按 TOML 声明序列出每个 pattern 的 eligible/selected 计数 |

完整 block 还包含：

- `run_attempt_id`、`run_id`、`delivery_digest`、`program_digest`、`plan_digest` 与 `artifacts_committed`；
- `sequence_slot_attempts` 与 `noise_slot_attempts`；
- `sequence_calls`：scenario seed、baseline/variant event plan、frame render、semantic evaluation、noise render/evaluation；
- `by_pattern`：每个 variant 的 planned/delivered；
- 冻结闭集 `rejected_attempts`：每个失败 attempt 只进入最终停止边界对应的一个桶；
- 顶层 `llm_usage`：按 profile 统计物理 calls、prompt/completion tokens 与 retries。

`9:1` 权重表示每个仍有 partner 的机会里 standalone 与该 pattern 分别占 9 张票和 1 张票，不表示最终恰有
10% 的 session 被交织。partner pool 耗尽后分母会改变。report 只记录机会与汇总计数，不输出 pair identity、
owner word、payload、prompt、state 或 API key；需要核对真实 owner word 时，从最终 stream 的 session 与 timestamp
机械推导。

逻辑 family 调用次数会包含失败 attempt，但同一入口内部的 L3 repair 或 provider retry 不会重复计为新的 family call。
它们分别进入 schema repair/trace 与 provider usage 账。

历史 v1.18 DeepSeek 教学主例中，default profile 为 38 calls、34470 prompt tokens、2511 completion tokens；judge profile
为 10 calls、9541 prompt tokens、484 completion tokens；两者 retries 都为 0。2 个 sets、8 条 sequence 与
27 行 stream 的 planned/delivered 逐项相等，delivery digest 为
`269089200ba4cbe62e41229d3921625341f902179f57cf2e0b95722aa23c8a76`。这是历史端点证据，不是 v1.21 交织门。

## 8.7 failed report

尝试耗尽、provider terminal、plan terminal 或 pre-commit 失败会写独立 failed report。它保留：

- `run_attempt_id` 与 nullable `run_id`；
- `artifacts_committed = false`；
- nullable `failed_slot` 与 `attempts_used`；
- `terminal_error_kind`；
- 与成功报告同口径的 `llm_usage` 和 `rejected_attempts`。

它不包含一个伪造的已交付 `by_pattern` 前缀，也不进入成功 manifest。成功重跑不会删除历史 failed report；
只要 manifest 摘要有效，failed report 就不能否定成功提交。

commit 前的失败保持已有 main、stream、success report 和 manifest 不变。failed-report 自身写失败时只记录英文
错误，不覆盖主退出码。

## 8.8 retained bytes 与 delivery digest

`retained_content_bytes` 使用最终 main 和 stream 行的同一个 canonical helper，按 UTF-8 字节核算：

- 两个视图里的同一 payload 会保守重复计入；
- annotation、generation truth、replay 和全部元数据都计入；
- 发射时才增加的墙钟观测字段不计入；
- 每行按 JSONL 规则包含一个换行字节。

上限 536870912 bytes 是紧凑输出内容预算，不是预分配物理内存。source 若加上它的 replay 后超限，整个 source
slot 会在 dedup/dataset/replay commit 前回滚。

delivery digest 同样基于最终 rows，并移除指定墙钟字段。report 是 digest 的唯一生产 owner；manifest 读取 report
里的同一 digest，不自行重算第二份真值。

## 8.9 rejects 与 trace

普通运行的 rejects：

- `refs` 只写 ID、source、stage、reason 和 errors；
- `full` 还写数据内容，需按数据副本保护；
- `none` 不写文件。

trace 与 rejects 正交。trace 高内容档可能含数据；stderr 永远不含数据和 prompt。sequence form 不打开 rejects，
失败 attempt 只写聚合计数与 value-free 错误，完整状态、ActorView、patch 和隐藏事实不进入任何工件。

## 8.10 归档清单

普通任务至少归档配置摘要、main、report 和必要的 rejects。图片模式的几何只在 `_meta.regions`，配 sidecar 时记得一并归档 `{stem}.meta.jsonl`。sequence 任务把 main、stream、report 与 manifest
作为一个不可拆分集合归档；failed report 单独归档为诊断证据。任何消费动作都应先过 manifest hash 校验。

# 附录 A　全参数速查表

> 本附录按当前配置面分组。字段、默认值和错误边界以 `docs/dev/SPEC-sequence-generation-redesign.md`、
`docs/CONTRACTS.md` 与 `spec/50-ch5-config-spec.md` 为准。

## A.1 `config.toml`

### LLM profile

| 键 | 约束 / 默认 | 用途 |
|---|---|---|
| `provider` | `openai_compatible` / `anthropic` | 协议适配 |
| `base_url` | 必填 URL | API 根地址 |
| `model` | 必填 | 模型名 |
| `api_key_env` | 与 `api_key_envs` 二选一 | 单个密钥环境变量名 |
| `api_key_envs` | 非空列表 | 密钥池环境变量名 |
| `max_concurrency` | 正整数 | profile 级并发 |
| `timeout_s` | 正数 | 单请求超时 |
| `max_retries` | 非负整数 | provider retry 上限 |
| `retry_base_delay_s` | 正数 | 全抖动退避基数 |
| `supports_structured_output` | false | 是否发送供应商结构化输出约束 |
| `supports_vision` | false | 是否接受图像内容 |
| `max_output_tokens` | 正整数 | 输出预算 |
| `context_window` | 0；sequence 要求正数 | 端点实效上下文窗 |
| `thinking` | profile 声明 | anthropic 请求的 thinking 形状 |
| `temperature` | 0.0 | 默认采样温度 |
| `price_per_mtok_input/output` | 可选 | 报告成本估算 |

sequence 教学工程的 default/judge 都固定为 DeepSeek anthropic route、`deepseek-v4-flash`、
`supports_structured_output = false`、`thinking = "disabled"` 和 `context_window = 131072`。
配置只写 `LABELKIT_DEEPSEEK_KEY` 这个环境变量名，不写 key value。

### tool 与 console

| 键 | 默认 | 用途 |
|---|---|---|
| `tool.log_level` | `info` | debug / info / warn / error |
| `tool.log_format` | `text` | text / jsonl |
| `console.mode` | `auto` | auto / rich / plain |
| `console.refresh_hz` | 8 | rich 刷新率 |
| `console.heartbeat_s` | 0 | plain 心跳间隔，0 关闭 |
| `console.estimate` | false | 文本 process 是否预扫描估算 |
| `console.interactive` | true | rich 键盘交互 |

## A.2 `project.toml` 的 run / input

| 键 | 默认 / 约束 | 用途 |
|---|---|---|
| `schema_version` | 1 | 配置 Schema 版本 |
| `run.input` | process 必填 | 输入路径；generate-only 禁止 |
| `run.output` | 必填 | 主输出 JSONL |
| `run.modality` | text / ui | 输入模态 |
| `run.mode` | process | process / generate-only |
| `run.batch_size` | 256 | 批大小与 pairwise 比较池 |
| `run.seed` | 0 | 所有本地确定性随机源 |
| `run.fatal_error_threshold` | 20 | 连续 provider fatal 熔断阈值 |
| `run.max_park_s` | 60 | 密钥池全部冷却时的驻留上限 |
| `run.partial_delivery` | false | 熔断部分交付；sequence 必须 false |
| `input.text_field` | `text` | 文本字段；可用点路径，replay 固定 payload |
| `input.on_bad_line` | `skip` | skip / fail |
| `input.ui_tree_glob` | `**/uitree_*.jsonl` | UI 树匹配 |
| `input.image_glob` | `**/image_*.*` | 图像匹配 |

## A.3 普通算子开关

| 节 | 默认 | 关键字段 |
|---|---|---|
| `dedup` | enabled=true | scope、MinHash、pHash、embedding 与阈值 |
| `classify` | false | llm、assignment、class table、fallback、self-consistency |
| `quality` | true | mode、rubric、threshold / top-ratio、rounds、judges |
| `generate` | false | `form = "flat"` 时使用 seeds、styles、profiles 与数量字段 |
| `annotate` | true | llm、instruction、examples、self-consistency |
| `verify` | false | llm/judges、policy、repair rounds、extra criteria |
| `segment` | false | strategy、window、noise filter、min length、context |
| `stitch` | false | max open、bias、rescue、repass、votes、stale gap |
| `extract` | false | llm、instruction、tree diff、on error |
| `frame.classify` | false | frame class closed set 与 fallback |
| `frame.annotate` | false | frame instruction、Schema 与按 frame class 覆盖 |

普通组合约束：

- annotate 与 quality 至少开启一个；
- verify 要求 annotate；
- process 中 generate 要求 quality，且 generate 仅文本模态；
- segment 仅 process、要求 annotate，并与 generate 互斥；
- extract 要求 UI + segment；stitch 要求 segment；
- frame classify/annotate 要求 segment 与非 none metadata；
- threshold 与 top-ratio 选择互斥。

## A.4 flat generation

| 键 | 默认 / 约束 | 用途 |
|---|---|---|
| `generate.form` | flat | 独立样本形式 |
| `generate.llms` | default profile | 候选 profile |
| `generate.mixture` | uniform | uniform / weighted |
| `generate.weights` | 与 weighted 配套 | profile 权重 |
| `generate.instruction` | generate-only 必填 | 样本分布与质量要求 |
| `generate.seed_examples` | 与 standalone count 互斥 | generate-only 种子池 |
| `generate.standalone_count` | 与 seeds 互斥 | 无种子目标条数 |
| `generate.num_per_record` | 1 | 每个 seed 产量 |
| `generate.num_per_call` | 4 | 每次调用的样本数 |
| `generate.seeds_per_call` | 3 | process 种子装箱 |
| `generate.temperature` | 0.9 | 生成温度 |
| `generate.sample_validator` | 可选 hook | 生成样本语义过滤 |
| `[[generate.styles]]` | 可选 | name + prompt 风格桶 |

flat 不得出现 sequence 的 pattern、counterfactual、interleaving、timeline、noise 或 instruction-only 字段。

## A.5 output / trace

| 键 | 默认 / 约束 | 用途 |
|---|---|---|
| `output.schema_path` / `schema_inline` | 恰一 | draft 2020-12 object Schema |
| `output.max_repair_attempts` | 2 | 用户 Schema L3 预算 |
| `output.repair_llm` | 调用方 profile | 专用修复 profile |
| `output.validator` | 可选 hook | 用户标注 L2.5 |
| `output.meta_mode` | inline | inline / sidecar / none |
| `output.rejects` | refs | refs / full / none |
| `output.passthrough_fields` | [] | 原字段透传 |
| `trace.enabled` | false | trace 开关 |
| `trace.path` | 从 output 派生 | trace 文件 |
| `trace.channels` | 全部 | 订阅通道 |
| `trace.content` | refs | none / refs / excerpt / full |
| `trace.max_bytes` | 0 | 0 不限，超限丢事件不影响数据 |

sequence 固定 inline metadata、no rejects，且不使用 sidecar。

## A.6 process stream

| 节 / 键 | 默认 | 用途 |
|---|---|---|
| `stream.order_by` | input-order | 输入顺序或 `meta:<field>` |
| `stream.on_disorder` | skip | skip / fail |
| `stream.key` | [] | session 分区键 |
| `stream.gap_s` | 300 | 时间差断 session |
| `stream.gap_steps` | 0 | 序号差断 session |
| `stream.session_max_len` | 200 | session 帧上限 |
| `stream.session_max_span_s` | 0 | 时间跨度上限，0 关闭 |
| `segment.strategy` | hybrid | rules / llm / hybrid |
| `segment.llm` | default | 文本判定 profile |
| `segment.window` | 20 | 滑窗上限 |
| `segment.noise_filter` | true | 剔除噪声帧 |
| `segment.min_len` | 2 | LLM 精化段最短长度 |
| `segment.on_error` | keep | keep / fail |
| `extract.include_diff` | true | UI 树差异证据 |
| `stitch.votes` | 1；必须奇数 | 判定采样数 |

process stream 的 member/classification 与 sequence-generated stream 的 event provenance 是不同契约；
replay ingest 在进入 segment 前先验证 generation envelope。

## A.7 class 与 frame class

普通 classify 使用 `[[classify.classes]]`；process frame classify 使用自己的 frame closed set。
按 class 可覆盖 quality、annotate、generate 与 verify 的冻结白名单。`[class.<name>.annotate]` 可整份替换
用户输出 Schema。

sequence form 不运行两个 classify stage。它直接用：

```toml
[class.ticket_booking]
description = "一次订票请求与处理结果"

[frame.class.task_request]
description = "请求者提出订票需求"

[frame.class.task_request.generate]
instruction = "写一个尚未完成的订票请求。"
schema_path = "schemas/frame-request.json"
```

每个 sequence/frame 在进入下游前机械写 inherited classification。frame payload 必须是 object Schema。

## A.8 sequence form 前提

- `run.mode = "generate_only"`、text modality、无 input；
- `generate.enabled = true`、`generate.form = "sequence"`；
- mode 恰为 declared 或 instruction-only；
- semantic/evaluation profile 名不同且都声明正数 context window；
- global dedup、inline metadata、no rejects、no partial delivery；
- classify 与 frame classify 关闭；
- 禁止 `--limit`；
- quality 若开启，只能 pointwise + 固定 threshold，且不能有生效 class override。

`validate`、`run --dry-run` 与 `run` 共用同一 `compile_scenario_plan`。计划失败发生在凭据物化、
输出打开与 attempt 消耗之前。

## A.9 sequence 主节与 class state

| 键 | 默认 / 约束 | 用途 |
|---|---|---|
| `generate.mode` | sequence 必填 | declared / instruction-only |
| `generate.semantic_llm` | 必填 | 生成 profile |
| `generate.evaluation_llm` | 必填且名称不同 | 独立判定 profile |
| `generate.max_slot_attempts` | 3；1..20 | 每个 slot 最大 attempt |
| `generate.state_validator` | 可选 hook | 接收不可变状态副本 |
| `class.<name>.generate.instruction` | declared slot 必填 | class 生成约束 |
| `state_schema_path` | declared slot 必填 | 完整 state Schema |
| `initial_state_source` | llm / catalog | ScenarioSeed 来源 |
| `initial_state_catalog_path` | catalog 必填 | JSONL catalog |

catalog 在 compile 后按 slot 无放回分配，重试固定使用同一 row index。ScenarioSeed 固定包含 initial state、
actors、public/hidden shared facts、style 与 time context。

## A.10 named pattern

```toml
[generate.pattern.booking_success]
sequence_class = "ticket_booking"
description = "请求、受理与最终结果"
order = ["request", "acknowledge", "confirm"]
max_span_s = 2400
```

每个 role：

| 键 | 约束 | 用途 |
|---|---|---|
| `name` | pattern 内唯一 | role identity |
| `frame_class` | 已声明 | payload 类型 |
| `actor` | ScenarioSeed actor | 执行者 |
| `read_roots` | RFC 6901 roots | 可读 state |
| `write_roots` | RFC 6901 roots | patch 可写 state |
| `publish_roots` | 事件后必须存在 | 发布给 observers |
| `observers` | actor 闭集 | 接收发布事实 |
| `state_instruction` | 必填 | EventPlan 状态目标 |
| `pre_state_schema_path` | 可选 | patch 前完整 state 约束 |
| `payload_bindings` | 可选有序表 | authoritative state → payload |

frame class 的 temporal 键：

| 键 | 约束 | 用途 |
|---|---|---|
| `duration_s` | 缺省为点事件；在场为正、精确到整数毫秒 | 固定 event interval |
| `resources` | 声明序唯一，匹配 `[a-z0-9_]+` | 容量为一的互斥资源 |
| `time_bindings` | 与完整 Schema 的业务时间路径集完全相等 | Planner 时间机械写入 payload |

完整 Schema 的每个业务时间标量叶子必须声明 `x-labelkit-business-time = true`。frame binding source 是
event start/end/duration 的 milliseconds 或 start/end 的 fixed-offset ISO 8601。模型只消费删除这些叶子的
model Schema；generic candidate finalizer 注入后再执行完整 Schema。

每对相邻 role 必须恰有一个 named gap，声明 before/after、闭区间 min/max seconds。可以追加非相邻正向 gap；
`[[generate.pattern.<name>.containments]]` 用 `container` 与 `contained` 声明至少 1 毫秒余量的严格区间包含。
所有时间配置与 Planner 结果都必须对齐 1 毫秒 quantum。

## A.11 counterfactual set

```toml
[[generate.counterfactual_sets]]
name = "booking_success_training"
pattern = "booking_success"
count = 2

[[generate.counterfactual_sets.variants]]
name = "positive"
kind = "positive"
outcome_schema_path = "schemas/outcome-positive.json"
```

| kind | 专用字段 | 唯一预期违规 |
|---|---|---|
| positive | 无 | 空集 |
| missing | `target_role` | missing-role |
| reordered | `target_before` / `target_after` | reordered |
| interval-exceeded | `target_gap` / min/max excess | gap-above-max |

count 是 counterfactual set 数，不是 attempt 数。每个 variant 都要 outcome Schema；hidden baseline 始终存在。
whole-set 经过 generation、evaluator、真实下游、dedup、CrossView 与 retained budget 后才原子提交。

## A.12 instruction-only

```toml
[[generate.instruction_only]]
name = "open_booking"
sequence_class = "ticket_booking"
count = 1
len_range = [3, 4]
instruction = "生成一次完整、自然、状态连续的订票交互。"
state_schema_path = "schemas/state.json"
```

它不得声明 pattern、counterfactual、role permissions、outcome Schema 或 expected violation。
每个 attempt 生成 ScenarioSeed；EventPlan 在已声明且非 noise 的 frame class 闭集中选择 frame/actor。
EventPlanRequest 显式携带完整 state Schema，让 post-validator/L3 能看到合法枚举；declared request 的该字段为 null，
从冻结 program 解析权威 Schema。

## A.13 interleaving、timeline、calendar、noise 与 replay

| 键 | 约束 | 用途 |
|---|---|---|
| `generate.counterfactual_sets[].interleaving_candidate_set` | 可选；`[a-z0-9_]+` 短标签 | 把该 set 的唯一 positive branch 放入候选集 |
| `generate.interleaving.no_interleaving_weight` | 非负 TOML int64 | 每次 opportunity 选择 standalone 的权重 |
| `generate.interleaving.pattern.<name>.trigger_candidate_set` | 已声明候选集 | 按 DeliverySlot 声明序接受抽取 |
| `generate.interleaving.pattern.<name>.partner_candidate_set` | 已声明且不同于 trigger | 共享、不放回的 partner pool |
| `generate.interleaving.pattern.<name>.trigger_weight` | 正 TOML int64 | 选择该命名 pattern 的权重 |
| `generate.timeline.timestamp_start` | 带 offset ISO 时间 | 确定性起点 |
| `event_gap_s` | [lo, hi] | instruction-only / noise 默认间隔 |
| `session_max_events` | 正整数 | session 容量 |
| `session_max_span_s` | 正数 | session 时间跨度 |
| `session_gap_s` | 正数 | session 间隔 |
| `noise_events` | 非负 | 精确 noise slots |
| `duplicate_sequences` | 非负 | 从 committed positive 选择 constant-shift replay source |
| `generate.calendar_window.<name>` | fixed offset + days + half-open intervals | role 可引用的日历窗口 |
| `generate.noise.frame_class` | 独立 object frame class | noise payload |
| `generate.noise.instruction` | noise 非零时必填 | noise 生成约束 |

候选集标签精确匹配，不支持 glob、regex、前缀、列表或表达式。同一候选集不能同时承担 trigger 与 partner；带标签的
counterfactual set 必须恰好有一个 positive variant。`no_interleaving_weight = 9`、当前唯一 pattern 的
`trigger_weight = 1` 表示一次可用机会中 standalone 与该 pattern 分别占 9 张票和 1 张票，不是最终比例配额。
交织章节与候选集标签必须同时存在或同时不存在；开启时至少有一个命名 pattern，全部标签都被引用且两侧非空。
`no_interleaving_weight` 非负、`trigger_weight` 为正，单次机会总权重不得越过 TOML int64 上限。

设可见 primary branch 数为 N、冻结交织布局数为 D，报告派生
`primary_sessions = N - D`、`interleaved_primary_sessions = D`。抽中 pair 后若布局不可行，直接
`generation_plan_infeasible`；不换 partner、pattern、standalone 或在 retry 中重抽。instruction-only 禁止交织配置，
其交织机会与交织 session 为零、pattern map 为空。declared 未启用交织时也是零 opportunity、零 interleaved session
和空 pattern map，`primary_sessions` 等于可见 primary branch 数。noise 与 instruction-only 只能选择点 frame class。
replay 的所有成员使用同一个正、毫秒对齐的 shift，保持 source 的 start delta、duration、resources 与非时间 payload，
并按 replay 起点重新绑定业务时间。

## A.14 固定上限

| 对象 | 上限 |
|---|---:|
| pattern roles | 32 |
| variants per counterfactual set | 8 |
| instruction-only events | 8 |
| ScenarioSeed canonical JSON | 65536 bytes |
| state / outcome Schema | 65536 bytes |
| frame Schema | 65536 bytes |
| one event patch | 16384 bytes |
| one payload | 65536 bytes |
| one runtime prompt value | 32768 UTF-8 bytes |
| one L3 newly appended message-body set | 32768 UTF-8 bytes |
| one generation prompt text | 32768 UTF-8 bytes |
| record units | 500000 |
| stream rows | 500000 |
| retained content | 536870912 bytes |

retained content 是最终 main+stream canonical JSONL UTF-8 紧凑核算，不是 512 MiB 物理预分配。恰等上限接受，
多一个 UTF-8 byte 就 whole-slot 回滚。

## A.15 sequence 输出与退出

成功路径固定 main、stream、report、manifest；manifest 最后提交。失败诊断路径是独立 failed report。
sequence dry-run 五个固定路径都不写。

| 情况 | attempt | 退出 | 交付 |
|---|---|---|---|
| 可恢复 slot rejection | 消耗一次 | 重试；耗尽为 1 | 当前 group 全回滚 |
| provider fatal / circuit trip | 不消耗 | 4 | 零新 commit |
| plan config infeasible | 不消耗 | 2 | 零文件 |
| plan/internal/downstream contract | 不消耗 | 4 | failed report best effort |
| commit-I/O | 不消耗 | 4 | 旧 manifest 保留，固定路径可能混代 |
| success | 最终 attempt | 0 | main/stream/report 后 manifest-last |

消费方只信摘要有效的 manifest，不用 failed report、文件存在性或最近修改时间推断成功。

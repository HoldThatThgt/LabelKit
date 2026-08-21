# 第 27 章　时间流生成：用有限场景规划约束合成可重放的多会话流

时间流生成是 generate_only 的时间序列形态：没有输入文件，从零生成带时间戳的多会话请求流。
LLM 只负责自然语言内容；LabelKit 负责规则、日历窗口、类型敏感关联、时间轴、会话交叉、
噪音、重复重发和产物组装。

v1.17 将时间流升级为有限场景规划与精确交付：先冻结 finite schedule、配额、帧规则/窗口、跨序列规则、duration/resource、noise 槽与 duplicate 布局，再按固定 slot 有界重试内容。LLM 只负责自然语言内容；LabelKit 负责规则、日历窗口、类型敏感关联、时间轴、会话交叉、结构化噪音、重复重发和产物组装。

## 27.1 什么时候使用时间流生成

平面生成得到独立文本；时间流生成得到一段活动的成员帧、会话边界和可重放工件。

~~~mermaid
flowchart LR
    Q[day/week/schedule quota] --> P[ScenarioPlan planner]
    P --> B[每个 sequence slot 有界 brief]
    B --> R[每个 slot realize]
    R --> V[Schema / sample / sequence / scenario validator]
    V --> W[确定性投影与时间字段回填]
    W --> A[stream.jsonl 工件]
    W --> S[序列信封]
    S --> D[dedup → quality → annotate → verify → 主输出]
~~~

启动前提是 run.mode = generate_only、modality = text、generate.enabled = true、
classify.enabled = true、stream.order_by = meta:<field> 且 output.meta_mode != none。
它不消费 run.input，也不启用 segment、stitch 或 extract；project-replay.toml 才会在
process 模式重新走 ingest、segment 和 dedup。

有限 schedule 是唯一时间边界；day、week、schedule 三种 quota 共同约束同一批成功交付 occurrence。`crossed_sessions` 声明交叉数，总 session 由 target sequence slot 数自动推导。每个 sequence 与 structured noise 都有独立 `max_attempts_per_slot`，内容失败只重试同一 slot，不重排已冻结时间。

## 27.2 快速上手：当前教学工程

凭据只从 git-ignored .env 注入，不写进 TOML、命令历史或报告：

~~~bash
cd examples/synth-stream
mkdir -p out
set -a && source ../../.env && set +a
uv run labelkit validate --config config.toml --project project.toml --console plain
uv run labelkit run --config config.toml --project project.toml --console plain
~~~

示例自带 DeepSeek anthropic profile：

~~~toml
[llm.default]
provider = "anthropic"
base_url = "https://api.deepseek.com/anthropic"
model = "deepseek-v4-flash"
api_key_env = "LABELKIT_DEEPSEEK_KEY"
supports_structured_output = false
supports_vision = false
max_output_tokens = 8192
thinking = "disabled"
~~~

supports_structured_output = false 是能力声明，不是关闭 M8。该路由不走供应商强制工具
输入，M8 仍执行 JSON 修复、Schema 校验和有界修复环。`thinking = "disabled"` 是显式请求
字段；max_output_tokens = 8192 只是输出预算，不能替代 thinking 开关。

产物为 out/synth-labels.jsonl、out/synth-labels.stream.jsonl 和
out/synth-labels.report.json。当前生成侧核心值为：

~~~toml
[run]
mode = "generate_only"
modality = "text"
batch_size = 8
seed = 20260813

[stream]
order_by = "meta:ts"
gap_s = 3600
session_max_len = 12
session_max_span_s = 3000

[generate]
enabled = true
llms = ["default"]
num_per_call = 4
temperature = 0.9
sequence_validator = "examples.synth-stream.hooks:validate_sequence"

[generate.stream]
enabled = true
crossed_sessions = 1
noise_ratio = 0.1
duplicates = 1
frame_gap_s = [5, 60]
[generate.stream.schedule]
start = "2026-01-05T08:00:00+08:00"
end = "2026-01-06T23:00:00+08:00"
~~~

quota 表声明两个序列类各 3 个 delivery target，len_range = [4, 5]；固定 slot 失败时在同一 slot 内按 `max_attempts_per_slot` 有界重试，直到 target 交付或报告 delivery failure。
规划布局不会因 slot 失败而重排、重求解或改选其他 slot。

## 27.3 联合 planner

规划先按序列类名字典序展开配额，再应用实际 --limit 前缀。M1 对所有声明类、所有生效
档位和 len_range 的每个整数做局部结构与时间潜势检查；零配额类仍做静态检查。

有约束前缀的抽签顺序是：

~~~text
配额展开（零 rng）
→ 一个 31-bit solver seed
→ 每个实际 attempt 恰一次 randrange 长度偏好
→ llm/style 预抽
→ duplicate source 顺序预抽
→ noise 内容调用计划
~~~

同一 CP-SAT 模型以长度偏好名次和为主目标、可行 noise 数量为次目标，一次冻结长度、
frame class word、owner session、任务 timestamp、真实 crossing 和 noise 槽。求解器单线程
运行，M1、dry-run estimate 和 M6 复用同一问题入口。不能按候选长度分别求解、把失败当作
随机候选淘汰、求解失败后重抽、放宽规则、调用旧 weaver 或使用 fallback。

冻结后每条 attempt 依次一次 brief、一次 realize；噪音复用批量生成模板。brief 只返回固定
长度的每位要点，帧类来自 planner word；realize 返回逐位内容。内容失败只删除当前 attempt，
其他 attempt 的 session 和 timestamp 不移动。

## 27.4 配额、长度和档位

~~~toml
[[generate.stream.quotas]]
name = "six_sequences"
period = "schedule"
counts = { ticket_booking = 3, smart_home = 3 }

[class.ticket_booking.generate]
instruction = "围绕同一次购票请求生成连贯的多帧对话。"
len_range = [4, 5]

[class.smart_home.generate]
instruction = "围绕同一个居家场景生成连贯的多帧设备指令。"
len_range = [4, 5]
~~~

全局档位表是 smart_home 的生效表；ticket_booking 声明自己的整张表后完全取代全局表。
权重只做类内整数域配分，不消费 rng。tier_rank 是生效表内身份，不能跨类直接比较；
读取主输出 generator、工件 truth 或报告 tiers 时必须同时读取 sequence_class。

planner 把档位构成作为硬约束，members[] 的帧类集合必须与所属类生效表的对应
frame_classes 恰等。生成侧序列类和帧类标签都是 inherited 真值，不再调用分类 LLM。

## 27.5 五个帧类和时间字段

| 帧类 | 内容形态 | 作用 |
|---|---|---|
| task_request | 结构化对象 | 首帧请求，含 subject_id、utterance、entities、duration |
| acknowledgement | 结构化对象 | 受理回执，复用相同 subject_id |
| followup | 纯文本 | 追问、修改或补充约束 |
| progress | 纯文本 | 查询、核对或执行进展 |
| confirmation | 纯文本 | 确认、收尾或致谢 |

request 与 acknowledgement 的 Schema 都把 subject_id 声明为必填字符串。规则中的
correlation 是 operator = equal、source_field = subject_id、target_field = subject_id；
运行期先按 JSON 运行时类型，再按 canonical bytes 判断相等。

~~~toml
[frame.class.task_request.generate.time_fields]
duration = "gap_next_s"
~~~

duration 从 LLM 面向的逐位 Schema 和契约行中剔除。planner 先定稿时间轴，随后按同一
序列下一成员的间隔秒数回填 primary request。交叉会话、noise 和 duplicate 不改变这个
序内口径；duplicate 复制源 payload，继承源的已回填 duration，不按自己的新 timestamp
重算。

## 27.6 规则、窗口与验证顺序

当前规则链是 request → acknowledgement → confirmation：

~~~text
init(task_request)
exactly(task_request, 1)
chain_response(task_request, acknowledgement, [1200, 2400), subject_id equal)
exactly(acknowledgement, 1)
response(acknowledgement, confirmation)
end(confirmation)
exactly(confirmation, 1)
~~~

time_s 是半开区间 [1200, 2400) 秒；owner 相邻成员仍须满足闭合 replay guard
1us <= delta <= stream.gap_s。全局 task_request 窗口是工作日 [08:00, 11:00) 与
[14:00, 17:00)；ticket_booking 的按类窗口缩窄为 [09:00, 11:00) 与 [14:00, 16:00)。
窗口同一自然日，不跨午夜。

固定校验顺序是 realize Schema → sample_validator 逐帧 → correlation 与 time_s 规则
→ sequence_validator → 序列相似度过滤 → 投影幸存布局与 noise → 回填 time_fields
→ 复制 duplicate、排序和组装。钩子收到 JSON-compatible 深拷贝；任何 attempt 失败都不
创建 failed 信封、不写 item.errors，只进入相应计数和无数据日志。

## 27.7 两份产物

stream 工件每行含 ts、text 和 truth。truth 包含 session、sequence_class、sequence、
frame_class、noise；档位生效时增加 tier_rank，duplicate 增加 duplicate_of。结构化帧的
对象作为 text 字段的 canonical JSON 投影。truth 仅用于透传和对账，不参与内容判定。

主输出每行是一条序列 Record，序列类标签是 inherited classification；按类标注 Schema
分别作用于 ticket_booking 和 smart_home。报告只写 counts、usage、timing、失败桶和
工件摘要，不写 API key、提示词或原始内容。frame_rules/frame_windows、sequence validator 和
calendar_days_spanned 只在实际生效面出现。

报告使用 `brief_calls`、`planner.objectives`、`delivery`、`delivery.failures` 和 `quotas`；
`delivery.failures` 按首个失败阶段归桶，序列相似度淘汰单独记在 dedup 桶。

## 27.8 工件重放

~~~bash
cd examples/synth-stream
set -a && source ../../.env && set +a
uv run labelkit run --config config.toml --project project-replay.toml --console plain
~~~

project-replay.toml 将 out/synth-labels.stream.jsonl 作为输入，用 order_by = meta:ts 和
gap_s = 3600 重新会话化，用 hybrid segment 过滤 noise 并组装 episode，再用全局 dedup
比较源会话和流尾 duplicate。truth 透传到输出，但 segment 明确不能读取 truth 作语义
判断。生成侧 duplicate 只存在于工件，因此验收必须检查 report 中出现 dropped_dup，
而不是只看退出码。segment 对 noise 的语义判断可能使判重档位为 exact 或 near_text；
不要把任一档位写死。重放输出是 out/replay-labels.jsonl，不覆盖生成产物。

## 27.9 可执行验收清单

不触网先运行：

~~~bash
uv run labelkit validate --config config.toml --project project.toml --console plain
uv run labelkit run --config config.toml --project project.toml --dry-run --console plain
~~~

检查 validate 是否接受规则表、dry-run 是否构造联合 planner 且不发送 LLM；再确认配置中
`thinking = "disabled"`、max_output_tokens = 8192、gap_s = 3600、len_range = [4, 5] 和五个
帧类。

真实生成后检查 out/synth-labels.report.json 与 out/synth-labels.stream.jsonl：

~~~bash
uv run python - <<'PY'
import json
from pathlib import Path

report = json.loads(Path("out/synth-labels.report.json").read_text())
rows = [json.loads(line) for line in Path("out/synth-labels.stream.jsonl").read_text().splitlines()
        if line]
assert report["run"]["artifact"]["lines"] == len(rows)
allowed = {"task_request", "acknowledgement", "followup", "progress", "confirmation"}
assert {row["truth"]["frame_class"] for row in rows if not row["truth"]["noise"]} <= allowed
assert all(row["truth"]["sequence_class"] in {"ticket_booking", "smart_home"}
           for row in rows if not row["truth"]["noise"])
print("artifact shape checks passed")
PY
~~~

再按 sequence_class 取生效档位表，核对 members[] 构成；对 request/ack 核对 subject_id
类型和相等关系、[1200, 2400) 时间差与工作日窗口；对 duration 核对下一成员间隔；对
duplicate 核对源 payload、tier_rank 和 duration 被继承。重放后用报告断言 dropped_dup
至少出现一次，并接受 exact 与 near_text 两种档位。

## 27.10 本次真实验收记录

2026-08-20 使用当前 examples/synth-stream 配置和 DeepSeek profile 完成最终真实生成与
process replay。本节数字是该次运行的观测值，不是slot target的输出保证；此前 failed-closed
运行见 `docs/dev/E2E-FINDINGS.md` 第 38、39 条，不能改写为本次成功。

生成命令为：

~~~bash
set -a && source ../../.env && set +a
uv run labelkit run --config config.toml --project project.toml --console plain
~~~

生成进程 exit 0。主链守恒与序列计划事实如下：

| 观测面 | 本次结果 |
|---|---|
| 主链计数 | generated 6、emitted 5、dropped_verify 1、failed 0 |
| slot target | planned 6；ticket_booking 3/3，smart_home 3/3 |
| 类内档位 | ticket_booking：rank 1 为 1/1、rank 2 为 2/2；smart_home：rank 1 为 2/2、rank 2 为 1/1 |
| 时间流布局 | sessions 5、crossed_sessions 1、frames 27、noise_frames 3、duplicates 1、calendar_days_spanned 8 |
| 生成调用 | brief_calls 6、realize_calls 6、noise_calls 1 |
| 交付报表使用 `brief_calls`、`planner.objectives`、`delivery`、`delivery.failures` 和 `quotas`；失败按首个阶段归桶，序列相似度淘汰单独记在 dedup 桶。
| 工件 | 34 行；sha256:927e469e16df3f007f057357a267b8f8228506a5dfb279dc83bdfa1f1da672bf |
| LLM 用量 | calls 53、prompt 12173、completion 4487、retries 0 |
| 时延 | wall_s 35.214 |

本次 planner 规划的真实 crossing 得以保留；`crossed_sessions = 1` 是最终成功验收值，
不应沿用此前某次作废投影后自然退化为 0 的历史运行。

随后使用正式 project-replay.toml 重放上述 34 行工件，进程 exit 0：scanned/ingested 34、
episodes 6、absorbed 31、dropped_noise 3、dropped_dup 1、emitted 5、failed 0；sessions 6、
mean_episode_len 5.17、windows 7；LLM calls 12、prompt 4542、completion 721、retries 0、
wall_s 5.475。该结果验证了流尾 duplicate 的 process 侧判重。

## 27.11 常见排障

**输出截断或 schema violation。** 确认 `thinking = "disabled"`、max_output_tokens = 8192
和 supports_structured_output = false。Schema 失败仍走 M8 修复语义，修复耗尽就作废当前
attempt；不要用增加 token 代替显式关闭 thinking。

**规则配置失败。** 检查 template 是否属于十五个 DECLARE 模板，source/target 是否来自
五个帧类且不相等，time_s 是否满足 1us <= lo < hi，correlation 字段是否为结构化 Schema
的 top-level required 属性；窗口必须是同一天半开区间，不能跨午夜。

**没有输出。** 区分配置不可满足和内容作废：前者应在 validate 阶段失败，后者应在
delivery.failures 的对应阶段桶或 dedup 桶中体现。规则验证失败不会
触发重规划或 fallback。

**重放没有 duplicate。** 确认运行的是 project-replay.toml、输入路径未被覆盖、gap_s
一致，并检查 segment 是否把源会话和重发会话分成不同 episode。关键验收条件是重复会话
进入 dropped_dup，而不是具体的 exact/near_text 档位。

**需要 UI 生成。** 当前形态只支持 text；截图/UI-tree 流应走 examples/stream 或
examples/mix 的 process 路径。

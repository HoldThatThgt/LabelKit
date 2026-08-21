## 3.6 M6 生成 generate

### 3.6.1 职责与边界

**做：**组装生成提示词——种子按运行模式取自：process 模式 = 当前批过质量门的记录；generate_only 模式（v1.4）= 配置种子池 `generate.seed_examples`，或无种子（仅 instruction × style 条件化）。按 `generate.llms` ×（可选）`[[generate.styles]]` 组合产出新样本文本，构造为携带 `generated_from` 与 `generator` 溯源的新 Record，组成生成子批交 M10 回流调度。提示词组装同为确定性模板拼接（含「[风格要求]」追加）。 
**不做：**仅文本模态（无法生成截图，2.3.1 约束③）；单轮回流、不递归；不去重 / 不打分 / 不标注 / 不校验（回流后由 M3 / M4 / M5 / M7 完成）；生成调用失败仅损失该调用的样本，不产生 failed 记录（种子记录状态不变，记录级隔离，1.3）。

### 3.6.2 生成模式

| 步骤 | 定义 |
|---|---|
| 种子选取 | 当前批内 `status="active"` 且聚合分 ≥ `generate.seed_min_score`（默认取 quality.threshold，未设阈值时取批内中位数）的记录（process 模式）。generate_only 模式（v1.4）：种子 = `generate.seed_examples` 字符串数组——Self-Instruct 的人工种子池形态（原文以 175 条人工种子自举 [18]）；数组缺省则为无种子条件化，提示词不含示例段、仅由 `generate.instruction` ×（可选）styles 驱动（Persona Hub / Cosmopedia 形态 [34][35]），须显式给出量目标 `generate.standalone_count`。 |
| 生成调用 | 每次调用随机不放回抽 min(`generate.seeds_per_call`（默认 3）, 可用种子数) 条种子作为示例（种子池小于抽样数时取全池）（Self-Instruct 的 in-context 自举结构 [18]），system = `generate.instruction`，要求输出 `{"samples": [str, ...]}`（恰 `generate.num_per_call` 条，默认 4），经 M8 校验。调用次数 = ⌈种子数 × generate.num_per_record / num_per_call⌉（generate_only 种子池形态同式，种子数 = len(seed_examples)，seeds_per_call 从种子池抽；无种子形态 = ⌈standalone_count / num_per_call⌉，提示词省去示例段）。 |
| 多模型混合（v1.2） | `generate.llms` 为 profile 引用数组（默认 `["default"]`，取代 v1.1 的单值键 `generate.llm`，5.2 配置表已同步修订）。每次生成调用按 `generate.mixture` 选定 1 个 profile：`"round_robin"`（默认）按调用序轮转；`"weighted"` 按 `generate.weights` 加权随机抽样。抽样 PRNG 用 `ctx.rng`（3.10.3，随 run.seed 派生）；实现须在并发派发前按调用序号 0..C−1 一次性预抽全部 (llm, style) 对（round_robin 的轮转序同样以调用序号为准），使结果与并发调度顺序无关，逐调用可复现。动机：单一模型自生成会使产出分布收窄、尾部逐渐消失（model collapse，Shumailov et al., Nature 2024 [36]），异构生成器混合是公认缓解；distilabel 的「任务级绑定任意 LLM」为同构工业设计 [5]。 |
| 风格条件化（可选） | `[[generate.styles]]` 子表（每项 `name` + `prompt`）非空时，每次生成调用经 `ctx.rng` 均匀抽取 1 个 style，其 prompt 以「`[风格要求] …`」格式追加在 `generate.instruction` 之后（仍为确定性模板拼接，3.6.1 边界不变）。persona / 受众条件化提升合成多样性的背书：Persona Hub 以 10 亿 persona 条件化提示 [34]；Cosmopedia 按受众 × 风格分桶派生提示 [35]。溯源与可观测（v1.2 只增）：新记录 `_meta.source` 增 `generator = {"llm": <profile>, "style": <name>\|null}`（未配置 styles 时 style 为 null；6.3 信封只增字段）；`report.generate` 增 `buckets` 统计——每 llm×style 桶的调用数 / 产出条数 / 去重存活数，令多样性可观测：某桶去重命中率显著偏高 ⇒ 该桶贡献的多样性低，应调整其权重或 style prompt。 |
| 样本回调过滤（v1.5，可选） | `generate.sample_validator` 配置时，每条样本文本在相似度过滤之前先过用户回调 `fn(text) -> list[str]`：非空 ⇒ 剔除该样本（过滤语义，与相似度过滤同性质：不重试、不产生 failed 记录），桶统计增 `rejected_by_validator`（6.4 只增字段）。回调抛异常 ⇒ 该样本按违规剔除并 stderr warn 一次性提示。 |
| 新记录构造 | 每条样本文本构造 Record：`raw = {input.text_field: sample}`，id 规则同 M2，`ref.generated_from = [种子id列表]`；generate_only 模式下 `generated_from = []`（种子非记录、无记录 id，种子本身在 project.toml 中可审计），`generator` 照常携带。 |
| 回流 | 新记录组成「生成子批」交回 M10，从 M3 起走 去重 →打分 → 标注 →校验；去重索引含全部原始记录与先前生成样本，即 Self-Instruct 的相似度过滤 [18]（3.3.3 节）。子批不再触发生成（单轮回流，不递归）。generate_only 模式下生成子批即唯一数据来源：按 `run.batch_size` 切批走同一链路 M3→M4→M5→M7→M11（3.10.3），单遍不递归同样适用。 |
| 按类种子池（v1.7） | classify 启用时（process 模式）：种子按 `classification.label` 分组为按类种子池，每类用该类有效 instruction / styles / num_per_record / temperature（`class_views[label].generate`）独立走「生成调用」行的量公式；llms / mixture / weights / seeds_per_call / num_per_call 恒为全局（5.2 按类覆盖白名单表）。**类段字典序拼接调用序**：参与类（有种子的类）按类名字典序占据连续的全局调用序号区间，每类预算 C_c = ⌈len(seeds_c) × num_per_record_c / num_per_call⌉；单遍 i = 0..C−1 预抽——llm 照旧按全局序号选定（round_robin 零 rng 消耗 / weighted 逐 i 一次 choices，「多模型混合」行机制不变），style 从该 i **所属类**的有效 styles 中均匀抽，种子抽样按全局序号升序逐调用执行；classify 关闭 ⇒ 单一匿名段 = 现行为。**种子门槛按类默认链**：每类取全局 `generate.seed_min_score` → 缺省取**该类有效** `quality.threshold` → 再缺省取**该类种子池**聚合分中位数；`select_seeds` 按 label 分组返回。规划产物 `CallPlan` 增 `class_name` 字段，`one_call` 按 plan.class_name 取类有效 instruction / temperature；`postprocess_samples` 返回 `list[tuple[Record, str \| None]]`，`run()` 构造 PipelineItem 时新样本**继承种子类**——带 `Classification(label, (label,), "inherited", {})`（零额外分类调用；回流子批经 M13 幂等跳过，3.13.4）。桶统计 key 在 classify 启用时扩展为 `<class>×<llm>×<style>`（6.4；关闭时格式不变）。generate_only 模式：`generate_all` 扁平路径不变——生成用**全局**指令（无输入无从按类），产物由链上 classify 正常分类后按类打分/标注；不支持 generate_only 按类生成配比（1.6 v1.7 对齐决策 ③，与 8.3 O6 一并立项）。 |
| 时间流形态（v1.13） | `generate_stream.enabled = true`（generate_only 第三形态，默认关）时本模块改产**序列**而非独立样本：种子概念退场——量目标由按序列类的 `sequences`（尝试配额）× `len_range`（步数区间）承载；LLM 只做两类内容调用（一序列一次**蓝图** + 一次**帧实现**，噪音帧批量实现复用「生成调用」行的既有模板与 Schema），会话装箱 / 交叉 / 噪音插入 / 原样重发 / 时间戳铺设全部由零 LLM 的**机械交织器**完成；产物一式两份（可重放的时间流工件 + 直装序列信封）。全文见 3.6.5；本表其余各行在本形态下的效力见该节「生成键效力矩阵」。 |
| 上下文预算装填（v1.11） | 本次调用的目标 profile 声明 `context_window` 时按上下文预算装填生成调用（未声明 = 预算关闭，行为与 v1.10 一致；预算/估算/校准机制见 3.9）：`seeds_per_call` 降级为**上限值**——按 rng 采样序**从尾部丢弃**种子直到装下（确定性；被丢种子不递补），min 1；连 1 条种子都装不下 → 该调用按 `context_overflow` 处置（V10，7.6）。`generate.llms` 多 profile mixture（round_robin / weighted）下按**本次调用的目标 profile** 的预算装填——(llm, style) 预抽序与轮转序不变（确定性），装填结果仍逐调用可复现。系统侧静态部件（instruction / styles / 生成输出 Schema）不动态裁剪，由 M1 静态预检把关（V13③，3.1.4）。 |

### 3.6.3 API

```
class GenerateStage(Stage):
    name = "generate"
    async def run(self, batch, ctx) -> list[PipelineItem]:
        """返回值为新生成记录的子批（原批不修改）；M10 负责回流调度。
           单次生成调用经 M8 修复仍非法或重试耗尽 ⇒ 该调用作废并计入
           report.generate.buckets（calls 计入、produced 为 0），不影响其他调用与原批。"""
```

**背书：**生成模式为 Self-Instruct（ACL 2023）的种子自举 + 相似度过滤流程 [18]，指令可按 Evol-Instruct 风格写深化/扩展变体 [19]；多模型混合与风格条件化的多样性背书：Persona Hub [34]、Cosmopedia [35]、model collapse 的多生成器缓解 [36]；「任务级绑定任意 LLM」为 distilabel 的同构工业设计 [5]。

### 3.6.4 输入 / 输出示例

沿用统一文本示例（意图标注工程，仅文本模态），project.toml 追加：

```
[generate]                                  # project.toml 追加片段
enabled = true
llm = "default"
instruction = """你是中文输入法的真实用户。模仿示例指令的口吻与场景，生成全新的一句话中文指令：
日常场景、口语化、诉求明确；只借鉴风格与题材范围，不得复述示例内容。"""
num_per_record = 2
seeds_per_call = 3
num_per_call = 4                            # temperature 取默认 0.9
```

v1.2 键名迁移与多样性设定补充：上述片段中的单值键 `llm = "default"` 在 v1.2 写作数组键 `llms`（5.2 配置表），本示例取双模型轮转 + 两个风格模板（种子选取、调用次数与下方样本文本均不变）：

```
llms = ["default", "judge"]                 # v1.2：取代 llm = "default"；元素须为 [llm.*] profile
mixture = "round_robin"                     # 第 1 次调用走 llms[0]="default"，第 2 次走 llms[1]="judge"

[[generate.styles]]                         # 可选；每次调用经 ctx.rng 均匀抽 1 个 style
name = "concise"
prompt = "指令务求简短口语化，一句话直接给出诉求，不加铺垫。"

[[generate.styles]]
name = "scenario"
prompt = "指令中须带出一个具体的生活或工作场景（对象、事由或时间）。"
```

抽中 style 的 prompt 以「[风格要求] …」追加在 `generate.instruction` 之后（3.6.2 风格条件化行）。本示例第 1 次调用轮转到 `"default"`、`ctx.rng` 抽中 `"concise"`，system 末尾追加「[风格要求] 指令务求简短口语化，一句话直接给出诉求，不加铺垫。」；第 2 次调用走 `"judge"`。

设 `quality.threshold = 0.5`（故 `generate.seed_min_score` 默认取 0.5），本批过门槛种子恰 3 条；调用次数 = ⌈3 × 2 / 4⌉ = 2。第 1 次调用抽全部 3 条种子入提示词（system = `generate.instruction` + M8 持有的内部生成输出 Schema，要求 `{"samples": [str, ...]}` 恰 4 条）：

```
种子: 1cda030abc565f17 "帮我写一条请假条，明天上午要去医院"
      d5ad41d6357f8a55 "写一份周报模板"
      7ed3a60f4714c33f "帮我把这段话翻译成英文……"

响应(经 M8 校验): {"samples": [
  "帮我写一段给客户的道歉话术，快递发错货了",
  "把'会议改到下周三下午三点'翻译成英文",
  "帮我编一条朋友圈文案，晒周末爬山的照片",
  "写一个辞职信的开头，语气委婉一点"]}
```

每条样本按 3.6.2 构造新 Record（`raw = {input.text_field: sample}`，id 规则同 M2）。第 1 条：

```
Record(
    id       = "31dae67e9b295e34",          # sha256(canonical_json(raw))[:16]
    modality = "text",
    text     = "帮我写一段给客户的道歉话术，快递发错货了",
    raw      = {"instruction": "帮我写一段给客户的道歉话术，快递发错货了"},
    ui_tree  = None, image = None,
    ref      = RecordRef(source_file="", line_no=None, pair_index=None,
                         generated_from=("1cda030abc565f17", "d5ad41d6357f8a55", "7ed3a60f4714c33f"),
                         generator={"llm": "default", "style": "concise"}))
```

v1.2 设定下，该 Record 出自第 1 次生成调用（`"default"` × style `"concise"`），主输出中其 `_meta.source` 片段（6.3；`generator` 为 v1.2 只增字段，计入 `report.generate.buckets` 的 `default×concise` 桶）：

```
"source": {"file": "", "pair_index": null,
           "generated_from": ["1cda030abc565f17", "d5ad41d6357f8a55", "7ed3a60f4714c33f"],
           "fields": {},
           "generator": {"llm": "default", "style": "concise"}}   // 未配置 styles 时 style 为 null
```

4 条新 Record 组成生成子批交回 M10，从 M3 起回流（单轮，不递归）：与全部原始记录及先前生成样本做去重，MinHash-Jaccard ≥ 0.85 者标记 `dropped_dup`（3.3.3 的 Self-Instruct 相似度过滤）。

#### 纯生成模式变体（v1.4，无输入数据）

```
# project.toml（纯生成工程；无 [run].input）
[run]
mode = "generate_only"
output = "./out/synth-ime-0702.jsonl"
modality = "text"

[generate]
enabled = true
llms = ["default", "judge"]
mixture = "round_robin"
instruction = """（同上例生成指令）"""
seed_examples = [                    # 种子池形态：调用次数 = ⌈3 × 2 / 4⌉ = 2，与上例同式
  "帮我写一条请假条，明天上午要去医院",
  "写一份周报模板",
  "帮我把这段话翻译成英文……"]
num_per_record = 2
# 无种子形态则改为：省去 seed_examples，设 standalone_count = 500（调用数 = ⌈500/4⌉ = 125）
```

与上例（process 模式）的差异仅在入口与种子来源：无 M2 接入（IngestReport 全零、`report.counts.scanned = 0`），生成样本按 `run.batch_size` 切批走 M3→M4→M5→M7→M11；新 Record 的 `generated_from = []`、`generator` 照常携带（如 `{"llm": "default", "style": null}`）。6.4 计数不变量退化为 emitted + dropped_* + failed = generated，仍成立。

#### 时间流形态静态走查（v1.16）

`examples/synth-stream` 是 v1.16 的可运行教学配置，不在本节写入任何一次运行的
产出数量、耗时、哈希或调用统计。它固定展示规则、日历窗口、类型敏感关联、时间字段
回填、按类档位表和序列验证钩子如何共同进入联合规划与下游校验。

工具级 profile 明确关闭 DeepSeek thinking，并为 L0 关闭的文本 JSON 路径保留结构结果
预算；`max_output_tokens` 不是关闭 thinking 的替代方案：

```toml
[llm.default]
provider = "anthropic"
base_url = "https://api.deepseek.com/anthropic"
model = "deepseek-v4-flash"
api_key_env = "LABELKIT_DEEPSEEK_KEY"
supports_structured_output = false
supports_vision = false
max_output_tokens = 8192
thinking = "disabled"
```

当前工程的流铺设与配额面为：

```toml
[stream]
order_by = "meta:ts"
gap_s = 3600
session_max_len = 12
session_max_span_s = 3000

[generate.stream]
sessions = 5
noise_ratio = 0.1
duplicates = 1
frame_gap_s = [5, 60]
ts_start = "2026-01-05T09:00:00+08:00"

[class.ticket_booking.generate]
sequences = 3
len_range = [4, 5]

[class.smart_home.generate]
sequences = 3
len_range = [4, 5]
```

帧类闭集是 `task_request`、`acknowledgement`、`followup`、`progress`、`confirmation`。
`task_request` 和 `acknowledgement` 是结构化帧，均含 `subject_id`；其余三类是纯文本帧。
`task_request` 的 `duration` 绑定 `gap_next_s`，从 LLM 面向的 Schema 中剔除，并在时间轴
定稿后按同一序列的下一成员回填。

声明式规则固定为：`task_request` 初始化且恰有一次；它以
`chain_response` 在 `[1200, 2400)` 秒内响应到 `acknowledgement`，两帧的
`subject_id` 必须按运行时类型敏感规则相等；`acknowledgement` 恰有一次；它响应到
`confirmation`；`confirmation` 是序列末帧且恰有一次。`task_request` 必须落在工作日的
`[08:00, 11:00)` 或 `[14:00, 17:00)` 窗口；`ticket_booking` 的按类窗口整表覆盖为
`[09:00, 11:00)` 或 `[14:00, 16:00)`，`smart_home` 继承全局窗口。序列级
`generate.sequence_validator` 再校验位置连续、以 request 开始并以 confirmation 收尾。

全局档位表和 `ticket_booking` 的按类档位表分别定义帧类构成；`smart_home` 使用全局表，
`ticket_booking` 使用自身整表。`tier_rank` 只在所属序列类的生效表内有意义，读取工件
真值或主输出元数据时必须同时读取 `sequence_class`。

当实际 `--limit` 配额前缀包含有效 rules/windows 时，M6 在任何内容调用前调用共享
planner：先为每个 attempt 恰抽一次长度偏好，再由一个 CP-SAT 联合模型以偏好名次和为
主目标、可行 noise 为次目标，一次冻结长度、帧类 word、session、timestamp、crossing
和 noise 槽。它不逐候选重求，不按候选长度分别求解，不重抽、不放宽约束，也不 fallback。冻结
后每条 attempt 依次调用一次 brief 和一次 realize；LLM 作废只删除该 attempt 并投影幸存
布局，不重规划。M1、dry-run estimate 和 M6 复用同一问题构造与求解入口。

#### v1.16 最终真实验收事实（2026-08-20）

使用上述当前配置和 DeepSeek profile 的最终完整真实生成 exit 0。报告观测为：
`generated = 6`、`emitted = 5`、`dropped_verify = 1`、`failed = 0`；
`planned = 6`，`ticket_booking = 3/3`、`smart_home = 3/3`；
`tiers.ticket_booking.1 = 1/1`、`tiers.ticket_booking.2 = 2/2`、
`tiers.smart_home.1 = 2/2`、`tiers.smart_home.2 = 1/1`；
`sessions = 5`、`crossed_sessions = 1`、`frames = 27`、`noise_frames = 3`、
`duplicates = 1`、`calendar_days_spanned = 8`、工件 34 行，
`sha256:927e469e16df3f007f057357a267b8f8228506a5dfb279dc83bdfa1f1da672bf`。

调用计数为 `plan_calls = 6`、`realize_calls = 6`、`noise_calls = 1`；规则计数为
`sampled = 6`、`correlation_scrapped = 0`、`temporal_scrapped = 0`、
`sequence_validator_scrapped = 0`。LLM 用量为 53 calls、12173 prompt、4487 completion、
0 retries，耗时 `35.214s`。本次保留 planner 规划的真实 crossing；此前作废投影后
`crossed_sessions = 0` 的运行属于历史证据，不是当前验收值。

正式 `project-replay.toml` 重放上述 34 行工件也 exit 0：`scanned = ingested = 34`、
`episodes = 6`、`absorbed = 31`、`dropped_noise = 3`、`dropped_dup = 1`、
`emitted = 5`、`failed = 0`；`sessions = 6`、`mean_episode_len = 5.17`、
`windows = 7`。LLM 用量 12 calls、4542 prompt、721 completion、0 retries，耗时 `5.475s`。

### 3.6.5 时间流形态（v1.13–v1.16）

`generate_stream.enabled = true` 时（M1 硬合取 generate_only ∧ text ∧ `generate.enabled` ∧ `classify.enabled` ∧ `stream.order_by = "meta:*"` ∧ `meta_mode != "none"`，3.1.4 时间流生成行），M10 的 generate_only 分支改调本节的时间流入口**恰一次**（`ctx.batch_no == 0`、`ctx.rng == Random(f"{seed}:0:generate")`，3.10.3），3.6.2 的平面路径（`generate_all`）不参与、代码面**零改动**。本形态的产物是一式两份：按交织序定稿的**时间流工件行**（交 M11 第五通道落盘，6.5）与**直装序列信封**（`kind = "sequence"`，带序列类与帧类两级 inherited 标签，按 `run.batch_size` 切批自 M3 起走链，3.10.3）。segment / stitch / extract 不参与——流本就是生成出来的，不需要再切分；stream × generate 互斥条文字面维持（2.3.1、8.3 O3 核销）。

**公开面（签名冻结）**：

```
async def generate_stream_all(ctx: RunContext) -> StreamGenerateProduct:
    """时间流形态入口（M10 分支调用一次）。计划期抽签 → 派发（蓝图 → 帧实现逐序列
       作业与噪音批并发）→ 逐帧钩子与序列相似度过滤 → 机械交织 → 直装组装。
       作废序列只缺席，不产 failed 记录、不写 item.errors。"""

@dataclass(frozen=True)
class StreamGenerateProduct:
    envelopes: list[PipelineItem]    # 直装序列信封（计划序）
    artifact_lines: list[str]        # 工件行（交织序定稿；行号 = 列表序 + 1）

def plan_stream(cfg: ResolvedConfig, rng: random.Random) -> StreamPlan:
    """计划期纯函数（吃 cfg + rng，零 IO 零 LLM）：配额展开 → 逐序列长度 →
       逐序列 (llm, style) 预抽 + 噪音批预抽。M10 的 estimate_run 复用之做
       精确复演（非上界估算，3.10.3）。"""

def weave_stream(survivors, noise_payloads, cfg, rng) -> tuple[list[list[Slot]], dict]:
    """机械交织器入口（纯函数族，零 LLM 零 IO）：重复选取 → 装箱洗牌与成对交叉 →
       逐交叉会话切换点 → 逐噪音帧掷签 → 重发落流尾新会话 → ts 铺设。"""

def assemble_stream(sessions, survivors, cfg) -> tuple[list[str], list[PipelineItem]]:
    """直装组装：逐槽位构造工件行对象与成员 Record，逐序列构造序列 Record 与信封。"""

def stream_artifact_path(cfg: ResolvedConfig) -> str:
    """工件路径 = 输出路径去末级后缀 + ".stream.jsonl"（M11 以同一规则各自推导，
       算子间不互导；两侧等式由测试钉住）。"""

# ── v1.14 增补（两枚零 rng 纯函数）──────────────────────────────────────────

def apportion_tiers(sequences: int, tiers) -> tuple[int, ...]:
    """档位配分（落点在 common 层的 M1 配置模型侧，M6 反向导入）：把一个序列类的
       配额按 weight 走整数域最大余额法配到各档，按 tier_rank 升序返回逐档配额。
       纯函数、零 rng——M1 的逐非零配额对约束与 M6 计划期共用同一实现。
       v1.15：`tiers` 传该类的**生效表**（effective_tiers 的返回值）——本体与签名
       零改动，改的只是调用点；「全表连续覆盖 1..N」相应读作「每张生效表各自」。"""

def tier_rank_for_ordinal(sequences: int, tiers, ordinal: int) -> int | None:
    """类内序数 → 所属档位序数（配分结果按 tier_rank 升序切成连续分块的前缀和查表）。
       sequences 须传该类**全量**配额而非 --limit 截断后的条数；档位表为空时 None。
       v1.15：`tiers` 同取该类生效表，故序数分块是**类内**的——返回值是类内档序数，
       跨类不可比（本体与签名零改动）。"""

def backfill_time_fields(sessions, cfg) -> None:
    """机械回填尾声（零 rng 零 LLM 零 IO）：调用点 = weave_stream 之后、
       assemble_stream 之前，按已铺时间轴把绑定字段就地写入共享载荷对象。"""

# ── v1.15 增补（生效档位表的单点查找，零 rng 纯函数）─────────────────────────

def effective_tiers(class_tiers, global_tiers) -> tuple[TierSpec, ...]:
    """取一个序列类的**生效档位表**（裁决·表级原子覆盖 + 裁决·全局表为锚）：类声明了
       就用类的整张表（`class_tiers` 非 None），未声明则回落全局表。落点同
       apportion_tiers——common 层的 M1 配置模型侧，M6 与 M10 反向导入；M1 约束簇、
       M6 计划期、M10 报表装配三方共用同一实现，**档位取表点全库只此一处**。
       全局表为锚 ⇒ 档位面在场 ⟺ 全局表非空 ⇒ 每个参与类恒有非空生效表。"""
```

要点（规格与理由）：

- **抽签消费顺序表（按形态冻结）**：无有效 rules/windows 的实际前缀沿用 v1.15 默认路径：一条 `Random(f"{seed}:0:generate")` 先按类名字典序展开配额（`--limit` 在此做前缀截断，零 rng），再抽每条序列长度、(llm, style)，交织期再按既有顺序处理重复、装箱、交叉、噪音、重发与 ts。档位配分和时间字段回填仍是零 rng 纯函数，不改变该默认路径。
  实际前缀含有效 rules/windows 时切换 v1.16 受约束顺序：配额展开 → 一个 31-bit solver seed → 每个 attempt 恰一次 `randrange` 循环长度偏好 → (llm, style) 预抽 → duplicate source 顺序预抽 → noise 内容调用计划；一个 CP-SAT 联合模型以长度偏好名次和为主目标、可行 noise 为次目标，一次冻结长度、frame-class word、session、timestamp、crossing 和 noise 槽，之后才派发内容调用。求解失败不触发重抽、逐候选重求、放宽约束或 fallback；内容作废只改变后续投影输入，确定性以 LLM 内容产出为条件（2.6 声明链）。
- **档位构成（v1.14 全局表 `[[generate.stream.tiers]]`；v1.15 增按类表 `[[class.<name>.generate.tiers]]`；默认均不在场）**：一个档位的定义**就是**该档序列的帧类构成集合，不携带质量指令、不控制帧内部语义质量（那归帧类生成指令与温度）。**生效表（v1.15）**——档位表分两级，一个序列类的**生效表** = `effective_tiers(该类表, 全局表)`：类声明了就用类的整张表（**表级原子覆盖**，不逐行合并——行级合并会让 rank 身份跨表漂移），未声明则回落全局表。全局表是**锚**（按类表要求其在场，3.1.4），故「档位面在场」⟺ 全局表非空，且每个参与类恒有非空生效表；`tier_rank` 是**类内身份**——每张生效表各自连续覆盖 1..N（N 逐类可不同），跨类同 rank **无任何工具语义**、跨类同构成合法。**下文凡「档」与「档位表」均读作该序列类的生效表**；无任何按类表时生效表恒 = 全局表，本行与 v1.14 逐字同义。**配分**——每个参与类的 `sequences` 配额按**本类生效表**各档 `weight` 走**整数域最大余额法**配到各档（基额 = `(sequences × weight) // Σweight`、余额键 = `(sequences × weight) mod Σweight`，按余额键降序、平票按 `tier_rank` 升序逐档 +1 至配满；**禁止任何浮点中间量**——平票判定要一路喂给类内序数分块 → truth → 工件字节 → 成员 id，是冻结面）；类内序数按 tier_rank 升序占**连续区间**，`(类, 类内序数) → tier_rank` 即配分结果的前缀和查表。配分是 `(sequences, 生效表)` 的**纯函数、零 rng**：它插在抽签消费顺序表的 ①配额展开与 ②长度抽取之间作为零消费步，顺序表原文不动，同 seed 下有无档位表（含按类表）的抽签流逐字节一致。推论：`--limit` 的前缀截断只切掉尾部序数，即在每个类内**从最高档序数侧截起**（低档序数在前），截断与档位映射可交换——按类表下各类的分块各不相同，本推论**逐类原文成立**。**构成恰等**——蓝图 Schema 的 enum 限档内子集给「⊆」、`cover_all` 注入的逐类 `contains` 给「⊇」，合成「一条序列的 members[] 帧类集合 ≡ 其档声明的构成」，故档位身份可从数据反推对账（可审计性；按类表下反推须先按行的序列类取其生效表，行内 `classification.label` 与工件 `truth.sequence_class` 天然给出该类名）。**标识三点**——`_meta.source.generator.tier_rank`（6.3）、工件 `truth.tier_rank`（6.5）、`report.generate.stream.tiers`（6.4）；三点的在场判据是**全局表非空**（全局表为锚 ⇒ 在场性恒定，不因某类未声明按类表而逐行漂移），全局表缺省时三点全部不在场。**M1 侧**的身份连续性、构成互异（逐生效表执行）、逐非零配额对的长度可覆盖、两条 WARN 与按类表前提三子款见 3.1.4。
- **蓝图调用**（一**计划期配额**序列一次——存活与否是此调用之后的事，故估算基数恒为 `2 × Σsequences`）：system = 计划器指令 + `[任务]` 类有效生成 instruction + `[帧类表]`（`name: description` 行）+ 结构句；user = 「请为一条「{类名}」序列产出 {L} 步蓝图。」；schema = `plan_schema(帧类名集, L)`（内部待遇，3.8.1）。**`[帧类表]` 的取值与 user 行随档位表条件化**（v1.14）：档位表缺省 ⇒ 表为**全类表**、user 行取上述原形（与 v1.13 逐字节一致）；档位表在场 ⇒ 表只渲染**档内子集**（帧类表按声明序过滤本序列所属档的 `frame_classes`；v1.15：所属档 = 该序列**类的生效表**内 `tier_rank` 对应的那一行——`effective_tiers(类表, 全局表)[tier_rank − 1]`，生效表连续覆盖 1..N 故下标法照旧）、user 行取冻结变体「请为一条「{类名}」序列产出 {L} 步蓝图，且 [帧类表] 中每个帧类都至少出现一次。」、schema 取 `plan_schema(档内名集, L, cover_all=True)`。修复穷尽 / 不可装填 ⇒ 该序列作废、计 `plan_failures`（覆盖违约不新增失败机制——它是普通的 L2 违规，进 M8 既有修复环）。模板 verbatim 冻结于 CONTRACTS §10.14——L0 关闭的端点（如 DeepSeek anthropic 路由硬拒强制工具调用）上，结构服从性靠模板内嵌的结构契约与覆盖句兜底，**这是硬要求不是兜底优化**。
- **帧实现调用**（一蓝图一次）：system = `[任务]` 类有效 instruction + `[风格要求]`（预抽 style，可缺——蓝图不带风格）+ 结构句 + **逐位契约行**「第 i 帧（{帧类}）须符合：{该帧类生成 Schema 文本 | 自由文本契约}」；user = 蓝图逐步行 `i. [{帧类}] {brief}` + 「请实现全部 {L} 帧内容。」；schema = `realize_schema(逐步 Schema 序列)`（`prefixItems` 逐位包装，纯文本帧位取 `{"type": "string"}`）。默认 v1.15 路径的纯文本契约字面量仍为「自由文本一段」；受约束 v1.16 路径改为「JSON 字符串（如 "..."），不得用对象包裹」，只明确 JSON string 的表示方式，输出语义仍是自由文本，不改变调用次数或 Schema。**逐位 Schema 与契约行取的是缩减 Schema**（v1.14）——声明了时间字段绑定的帧类，其绑定键从「该帧类生成 Schema」中剔除后才进这两处（见下方「时间字段回填」行；无绑定帧类与纯文本帧位逐字节不变）。结构化帧的帧内容**以对象原样**落工件行的文本字段（纯文本帧落字符串），成员 `Record.text` 取其 M2 语义投影（对象 ⇒ canonical JSON，字符串 ⇒ 直取——与重放时 M2 的点路径抽取产出同一投影，6.5）。修复穷尽 / 降级穷尽 ⇒ 序列作废、计 `realize_failures`。模板 verbatim 冻结于 CONTRACTS §10.15。
- **噪音批量实现**：复用 3.6.2 的既有生成模板与 `samples_schema`，调用数 = `⌈噪音帧数 / generate.num_per_call⌉`（噪音帧数 = `round(noise_ratio × Σ任务帧数)`）；单批作废 ⇒ 缺额帧从交织中缺席（**不补生成**）。
- **逐帧钩子与序列相似度过滤**：`generate.sample_validator` 对帧实现产物**逐帧文本**执行——任一帧违规 ⇒ **整序列作废**（蓝图定长不可剔单帧，拒绝采样语义）并计 `validator_scrapped` 与桶 `rejected_by_validator`；随后 M6 内置的相似度过滤单元上移为**序列级**：判重文本 = 成员 text 按序 `"\x1e"` 拼接（M3 序列配方同式）、比对面 = 兄弟序列（本形态无种子）、参数取 `[dedup]` 三键，淘汰以桶 `survived_dedup` 差呈现（桶键在本形态为 `<class>×<llm>×<style>` 三段式——generate_only 首现类段）。
- **机械交织器（零 LLM 零 IO）**：`sessions_eff = min(sessions, Σ幸存)`，交叉对数 = `Σ幸存 − sessions_eff`（M1 已静态保证 `sessions ≤ Σsequences ≤ 2 × sessions`，故交叉并发度恒 k ∈ {1,2}）；单个交叉会话形态 = **A 段 + B 段 + A 余段[+ B 余段]**（切点 `cut_a ∈ [1, |A|−1]`、`cut_b ∈ [1, |B|]` 保证真交叉；一方不足 2 帧时与另一方互换，两方都不足则退化为顺次拼接——纯长度条件、零 rng）；噪音帧逐帧掷签 (会话, 槽位)，**满员会话**（`len ≥ stream.session_max_len`）退出签池，签池耗尽 ⇒ 余帧缺席 + WARN；重发序列（`duplicates`，超出幸存数时钳制 + WARN）取自幸存集、帧内容逐字节同源、恒落**流尾新会话**；ts 铺设自 `ts_start` 起严格递增——会话内帧间隔 `uniform(frame_gap_s)`、跨会话间隔 `uniform(gap_s + lo, gap_s + hi)`（恒 > `stream.gap_s` ⇒ 摄取侧按同一 gap_s 复演出相同会话切分），微秒精度 ISO-8601 写出。交织尾声统一回填 `truth.session`（全流会话序数 0 基）。
- **时间字段回填（v1.14，`[frame.class.<name>.generate.time_fields]`；默认不在场）**：帧生成 Schema 里的时间语义字段与实际帧间隔本就对不上——LLM 没有时间轴，写出来的秒数是编的。处置是「绑定即剔除 + 机械回填」两步。**绑定即剔除**：被绑定字段从 LLM 面向的**逐位 Schema 与逐位契约行**中一并剔除（缩减 Schema = 生成 Schema 删该 `properties` 键、`required` 取差集、其余关键字原样；派生须重建顶层与 `properties` 两层，**绝不就地改动 M1 冻结的帧类生成 Schema**——静态预算预检与契约行渲染同源读它），M8 按缩减 Schema 校验，LLM 越权输出绑定字段则按 `additionalProperties` 违规进修复环。不为注定被覆写的字段付 token 与修复环成本，契约也不误导。**机械回填**：新的回填尾声（零 rng 零 LLM 零 IO）位于 ⑨ts 铺设之后、直装组装之前，只遍历任务帧槽位并按所属序列归组（会话序即序内成员序——交叉切片不改序内次序），对绑定帧类逐帧按语义词表算值**就地写入共享载荷对象**——`ts` = 该槽位已铺 ISO 串；`gap_prev_s`/`gap_next_s`/`elapsed_s` = **本序列相邻成员**/首帧的 ts 差 `round(·, 6)`（微秒精度与 isoformat 对齐），首帧的 `gap_prev_s`/`elapsed_s` 与末帧的 `gap_next_s` 恒 0.0。序内口径的理由：交叉会话夹入的外序列帧与噪音帧本就占用其间墙钟，序内差值才与下游从数据实测的口径一致。重发槽位**不遍历也不触碰**——它与源槽位引用同一载荷对象，回填自动生效且其 `ts` 绑定值承源、≠ 自身行 ts（原样重发本就携带陈旧内容，语义自洽）；噪音帧与无绑定帧类不触碰；每个载荷对象恰被写入一次。**位次的两条推论**：① 回填先于行对象与 id 计算 ⇒ `Record.raw`/`text`/成员 id、序列 id 与 session_id 全部含回填值，工件重放逐字节同 id 同会话（重放侧的判重档位仍随分段判决浮动，「逐字节一致」≠「重放判重恒 exact」）；② `sample_validator` 逐帧校验与序列相似度过滤在交织之前完成，故二者吃的是**回填前**载荷——时间量是机械量，不参与内容校验与内容判重（两序列内容相同仅时间不同 ⇒ 照旧判近重，语义正确）。绑定字段上除 `type` 外的约束关键字不被强制也不被校验（M1 为此发 WARN，3.1.4）——时间量的值域由时间轴决定。
- **直装组装**：逐槽位构造工件行对象 `{<ts字段>: …, <text_field>: …, "truth": {…}}`（真值键集见 6.5）——成员 `Record.raw` = **该行全对象**、`id = sha256(canonical_json(raw))[:16]`（M2 公式 ⇒ 工件重放同 id）、`text` = text_field 值的 M2 语义投影、`ref = RecordRef(source_file=工件路径, line_no=行号, pair_index=None, generated_from=(), generator={"llm","style"})`（v1.14：档位表在场时 generator 取 `{"llm","style","tier_rank"}` 三键——v1.15 下键、序与在场判据均零改动，只是值来源为**本行序列类生效表**内的档序数，跨类不可比）；`session_id = sha256("\n".join(会话内全部帧 id))[:16]`（M2 公式，**含噪音帧与重发帧** ⇒ 重放一致）；逐序列构造 `Record(kind="sequence", members=…, text/raw/ui_tree/image=None, ref=首成员 ref, id=sha256("\n".join(member ids))[:16])`（M14 公式，S24 字段惯例）与信封——`classification = Classification(label=序列类, labels=(label,), source="inherited")`、`member_classifications = {成员 id: Classification(帧类, (帧类,), "inherited")}`（帧类真值随 members[] 落盘，3.11.2）。**噪音帧与重发帧只活在工件**（不构造信封、不进守恒账），重发序列本体早已在 envelopes 中、不重复入列。
- **`--limit` 与量目标**：单位 = **序列**，截断在计划期配额层（类段字典序前缀）——作废序列不再生成、不进交织 ⇒ 工件与主输出的覆盖面恒一致；M10 尾部另有一次 belt & braces 截断。按类 `sequences` 是**尝试配额**（`standalone_count` 同款语义）：无输出条数保证、无补齐回路（8.3 O6 辖区不变）；`counts.generated` = 进链序列条数。
- **作废语义**（3.6.1 边界的序列版）：蓝图 / 帧实现 / 逐帧钩子任一环节失败 ⇒ 该序列缺席，**不产 failed 记录、不写 `item.errors`**（种子概念不存在，无「原批」可损）；留痕 = 计数器 + 一行值-free stderr WARN（`seq=` 序号 / `class=` / `llm=` / `call=` / `kind=`）。
- **预算与溢出纪律（v1.11 家族）**：三类调用发出前都做 `est(system) + est(user) + 2 × 消息包封 ≤ input_budget`（`supports_structured_output` 时另扣 response schema 文本）的预检——不可装填即**从不发出**（V10 先例，precheck **永不喂熔断**）；帧实现的反应式溢出走**序列对半分**（schema 与蓝图概要随切片同步减半，≤ 2 级 AIMD，每次计 `budget.degrade_retries`；单步跨度或级数耗尽 ⇒ 序列作废），reactive-400 终局在作废吞点经共享 `budget.feed_reactive_terminal` 补喂熔断**恰一次**（A7 纪律，7.6）。`TEMPLATE_HEAD_TOKENS` 增 `generate_plan` / `generate_realize` 两键（噪音批复用 `generate` 键值），M1 静态预检增对应两段（3.1.4）。
- **计数与观测**：`report.generate.buckets` 照常（`calls` / `produced` / `survived_dedup` / `rejected_by_validator`）；新增 `report.generate.stream` 子块（counts-only 12 键：`sessions`（交织出的会话数，**不含重发尾会话**；作废序列会使其低于声明的 `sessions`——交织按 `sessions_eff = min(sessions, Σ幸存)` 装箱）/ `crossed_sessions` / `sequences.<class>.{planned, produced}`（`produced` = 该类**最终进链**的序列数，即过了蓝图、帧实现、逐帧钩子**与序列相似度过滤**四关的条数——`planned − produced` 的缺口按环节分摊在 `plan_failures` / `realize_failures` / `validator_scrapped` 与相似度淘汰上，后者只以桶 `survived_dedup` 差呈现）/ `frames`（幸存序列的任务帧总数，不含噪音帧与重发帧）/ `noise_frames`（实际织入数）/ `duplicates` / `plan_calls` / `realize_calls`（含对半降级后的分次调用）/ `noise_calls`（三个调用计数在**派发前**递增，故含被预算预检拦下、从未发出的调用——平面路径同款口径）/ `plan_failures` / `realize_failures` / `validator_scrapped`，6.4）；`report.run` 摘要族增工件条目（路径 / sha256 / 行数）。**v1.14 档位面增量**：`report.generate.stream` 增 `tiers` 子块（counts-only，条件在场 = **全局档位表非空**——全局表为锚故这一判据在 v1.15 下零改动；**键位冻结在 `sequences` 之后、`frames` 之前**（配额族相邻））——口径与 `sequences.<class>` 同款：`planned` 计于计划期逐序列、`produced` 数最终进链（过了蓝图、帧实现、逐帧钩子与序列相似度过滤四关）的条数。该子块由 M10 在报表装配时**按声明档位表显式铺开**（3.10.3），故零额档与全作废档也如实在场（planned 0 / produced 0），不依赖计数器首触序。**v1.15 双形**（裁决·嵌套报表全类铺开）：全部序列类都未声明按类表 ⇒ **平面形** `{"<tier_rank>": {planned, produced}}`，按全局表 rank 升序铺开，与 v1.14 报表**逐字节相等**；任一按类表在场 ⇒ **类嵌套形** `{"<class>": {"<tier_rank>": {planned, produced}}}`，外层 = **全部声明序列类按声明序**零基铺开（`sequences.<class>` 同款）、内层 = 该类生效表 rank 升序，键均为十进制字符串（落盘无 sort_keys ⇒ 键序 = 装配插入序），零配额类与全作废档同样如实呈现 0/0。两形下键位都仍冻结在 `sequences` 与 `frames` 之间。**计数器键按类重冻结**（裁决·计数器键按类重冻结）：M6 恒喂类段键 `generate.stream.tiers.<class>.<rank>.{planned, produced}`（单一喂数纪律，禁双写两族键），平面形由 M10 按 rank **跨类求和**装配——数值与 v1.14 逐字节相等（v1.14 的平面计数本就是跨类聚合值）。**时间字段面零观测增量**（确定性机械操作，无可计数的失败模式）。**零新 trace 通道、零新事件**（两类调用经 `llm.call` 可见；generate 专属通道列 8.4 演进候选）、**零新错误 kind**（7.6；覆盖违约复用 `schema_violation` 进修复环、作废复用 `plan_failures`；v1.15 的按类表前提错误走既有 CONFIG_ERROR 面）。**零调用数变化**——档位（含按类化）与时间字段三个机制均不改调用次数，`estimate_run`、估算行格式、八个 dry-run golden 与 console 键集全部零改动（3.10.3、7.8）。

### 3.6.6 v1.16 序列规则与联合规划

时间流生成的生效规则或日历窗口在实际 `--limit` 配额前缀中出现时，M6 在任何 LLM 调用
之前进入全流联合规划路径。没有生效规则或窗口时，继续使用 v1.15 默认路径；序列级
`generate.sequence_validator` 可以独立运行，不改变帧类词的规划开关。联合路径的入口、
问题构造和求解与 M1、`estimate_run` 共用 `labelkit/common/runtime/sequence_planner.py`，
不在 M6 内复制一套约束实现。

规划期先展开按类名字典序的尝试配额，取一个 31-bit solver seed，再为每个 attempt 恰抽
一次循环长度偏好。同一个模型以偏好名次和为主目标、最大可行 noise 为次目标，联合定稿
长度、帧类 word、owner session、任务时间戳和可用 noise 槽；所有 task timestamp 全局唯一，
session 间隔至少为 `stream.gap_s + 1us`，
session 内相邻任务帧满足 replay guard。双 owner session 必须有真实的 A-B-A 或 B-A-B
交替；noise 只放在存活任务帧首尾之间的开区间，不参与规则和窗口。求解器单线程、固定
search seed、最大确定性时间预算 10 秒；model proto 超过 250,000 项、`INFEASIBLE` 或
无法在预算内验证的 `UNKNOWN` 都由启动期 M1 拒绝，运行期不靠重抽长度或重规划补救。

规划器先冻结 frame class，因此 LLM brief 调用不再返回类名。M8 使用
`brief_schema(length)`，只接受固定长度的逐位 `{brief: string}` 对象；prompt 同时带入固定
类词、生效 rules/windows/correlation 与按类 instruction。M6 将 brief 与固定类词合并，
再用既有 `realize_schema(prefixItems)` 完成 payload。realize prompt 必须再次携带规则和
correlation 说明；有 correlation 的序列禁止 reactive halving，溢出或截断直接沿既有
`realize_failures` 作废整条序列，无 correlation 时保留既有有界对半降级。

每条序列的验证顺序冻结为：realize Schema、逐帧 `generate.sample_validator`、声明式
correlation/time、一次 `generate.sequence_validator`、序列相似度过滤，然后投影幸存
primary、过滤 noise 槽、回填 primary `time_fields`，最后复制已回填 payload 给 duplicate
并完成全局排序和组装。任一序列失败都只让该 attempt 缺席，不创建 failed 信封或写入
`item.errors`；hook 输入是 `SequenceValidationInput` 的深拷贝，hook 异常视同违规。

规则验证在冻结 skeleton 上重新枚举标准 occurrence 候选，不把 planner 的 potential
witness 当作 i 对 i 的承诺。correlation 先按 JSON 运行时类型和 canonical bytes 相等过滤，
再按半开 `time_s` 过滤；正规则分别计 `correlation_scrapped` 或 `temporal_scrapped`，
负规则的相关性失败统一计前者。无 correlation 的运行期失败表示 planner 不变量破坏，记录
ERROR 并抛 `InternalError`。`validator_scrapped` 恒等于
`sample_validator_scrapped + correlation_scrapped + temporal_scrapped +
sequence_validator_scrapped`，相似度淘汰不进入该等式。

作废后的布局是已冻结 skeleton 的确定性投影：删除整条 attempt，移除空 session，按时间
重新编号但绝不移动幸存时间戳；crossed 只有两位 owner 都幸存且仍有真实交替时保留。重复
从调用前预抽的 source 排列中选取，复制 source 的 tier、frame class word 和已回填时间字段，
按规则允许的最小时间平移放到流尾。noise 目标是最大化已规划槽位的目标值；目标无法全放下
只记录一次值无关 WARN，不追加 LLM 调用。

M6 不改变 artifact 的 truth 键集、主输出 Schema、Record/session/id 公式或既有 trace
通道。规则和窗口不复制到工件；report 仅在实际非零配额类的生效面出现 `rules`、可选
validator 计数与 `windows.calendar_days_spanned`，并置于既有 `tiers` 后、`frames` 前。
`MODEL_INVALID` 或 M6 发现已通过 M1 的模型不变量被破坏时走既有 `InternalError` / 退出码 4。

### 3.6.7 v1.17 场景规划与精确交付

时间流生成形态在 v1.17 重写为「M1 冻结计划 + M6 有界精确交付」：M6 不再规划、不再交织、不再按尝试配额静默作废——它消费 `ResolvedConfig.scenario_plan` 的只读布局，把每个交付槽位跑一个有界状态机直到 quota 精确交付或预算耗尽。3.6.5 的 `plan_stream` / `weave_stream` 计划期与交织器公开面、3.6.6 的联合规划入口按 v1.17 裁决退出生产路径（历史段保留为版本史）——时间戳铺设、会话装配、交叉与 noise 槽选取由 ScenarioPlanner 在 M1 冻结；直装组装、工件行定稿、`time_fields` 回填与序列信封构造延续为 M6 职责。平面生成路径（3.6.2）与本节零交互。裁决详表见 `docs/dev/SPEC-scenario-planning.md`。

**ScenarioPlan 只读消费**：`slots` / `layouts` / `sessions` / `noise_slots` / `duplicates` 全部只读——length 已在 slot 构建时冻结为 `SequenceSlotSpec.length_target`，帧类词、时间戳、duration、session 归属、noise 槽与 duplicate source / 平移布局均已由 M1 定稿（3.1.4.2）。交付失败只重试该槽位的内容生成，绝不重排时间、不重选 duplicate source、不回填其他 class、不触发任何再规划。受约束路径 LLM 只被问 **per-position briefs**：沿用 `brief_schema(length)`（3.8.5）只取逐位 `{brief: string}`，帧实现复用 `realize_schema`——帧类词与时间轴不由 LLM 决定。

**delivery 状态机（每个 sequence slot 与 noise slot 独立运行）**，attempt 预算 = `generate.stream.max_attempts_per_slot`：

```text
Pending → Brief → Realize → Validate → Delivered（终态）
Brief / Realize / Validate 失败（违规或 provider 可重试失败）→ Pending：重跑同一 slot 的完整 brief + realization
Brief / Realize → Exhausted（终态）：对同一固定 prompt 可证明不变的确定性 precheck context overflow 捷径
Pending → Exhausted（终态）：attempts == max_attempts_per_slot
```

边注是概览；失败桶的唯一定义是下方 13 桶闭集——Validate 阶段任一子过滤器（sample / correlation / temporal / sequence / scenario / similarity）违规都走同一条 Validate→Pending 回边，桶按「只记第一个失败阶段」取。每次回到 Pending 都重跑完整 brief + realization：frame word、timestamps、duration、session、quota 与 noise slot 不变；accepted payload 不回滚。「delivered sequence」唯一表示 M6 已接纳该 sequence、写入 replay artifact 并交给下游 stage——因此 `delivery.delivered_sequences == counts.generated`；quality / annotate / verify 可以让最终 `counts.emitted` 更低，但不得反向触发 M6 refill，也不改变 quota 已交付事实。

**过滤顺序（冻结）**：schema guarantee → `sample_validator` → correlation / temporal replay → `sequence_validator` → similarity filter probe → `scenario_validator` against accepted prefix → commit similarity state and accept。similarity probe 与 commit 分离——scenario violation 不得污染 similarity filter。

**13 桶 failure 闭集与守恒等式**：delivery attempt 只记第一个失败阶段，failure bucket 是互斥闭集，report 中即使为零也全部在场（枚举序冻结）：

```text
brief, realize, noise, context_overflow,
sample_validator, sample_validator_exception,
correlation, temporal,
sequence_validator, sequence_validator_exception,
similarity,
scenario_validator, scenario_validator_exception
```

brief / realize 的 Schema guarantee、provider retryable exhaustion 与 output truncation 归到当时的 call phase；noise realization 的对应失败归 `noise`；确定性 precheck overflow 归 `context_overflow`；provider fatal 与 circuit breaker 立即 exit 4、不进入 failure bucket。每次非 fatal attempt 恰好满足 `attempts = delivered_sequences + delivered_noise + sum(failures.values())`。

**slot 顺序与 commit 纪律**：处理顺序固定为 `ScenarioPlan.slots` 顺序——全部 sequence slot 后接全部 noise slot；duplicate 是零 LLM 的工件布局，不进入 delivery attempt 顺序。同一 attempt 内可按 profile 并发调用，但 acceptance 与 similarity filter commit 必须回到 slot 顺序——网络完成顺序不影响结果。

**noise delivery**：每个冻结 noise slot 按同一 `max_attempts_per_slot` 调其帧类 realization——**structured noise 复用 frame class 的 instruction 与 JSON Schema**，Schema 解析、预算检查和 realization 复用 task frame 路径（3.8.6）。noise 不走 sequence / scenario validator，仍走 Schema、`sample_validator` 与 similarity filter（structured payload 先投影为 canonical JSON 字符串，再传 `sample_validator(text)` 与相似度过滤）。noise slot 的失败归桶与 sequence slot 同用闭集与「只记第一个失败阶段」：realization 调用段（Schema guarantee、provider retryable exhaustion、output truncation）归 `noise` 桶，之后的 sample validator / similarity 违规各自归 `sample_validator` / `similarity` 桶；noise acceptance 也按 slot 顺序 commit。truth 语义：`truth.noise = true`、`truth.frame_class = <实际类名>`、`sequence_class` / `sequence` / `tier_rank` 为 null（6.5）。

**`scenario_validator` 增量校验**：输入为 `ScenarioValidationInput{accepted, candidate}`——candidate 是本次唯一可拒绝项（当前交付槽位对应的 `ScenarioSequence{slot_key, sequence_class, start, end, frames}`），accepted 按时间、slot key 排序。非空违规只拒绝 candidate 并重试同一 slot：既有 accepted 不回滚、不重排。hook exception 与非法返回值都按 candidate violation 处理、WARN once、计入独立失败原因（`scenario_validator` / `scenario_validator_exception` 桶）。M1 启动期 synthetic probe 见 3.1.4.2。

**exhaustion**：任一 slot Exhausted 时——继续处理其他 slot，收集全部 exhausted slot；主输出、stream artifact 与 rejects 交付已成功部分（原子交付纪律，3.11.2）；report 标 `delivery.complete = false`；CLI **exit 1**（与 provider fatal / circuit breaker 的 exit 4 区分，3.10.3）；duplicate source 已在 ScenarioPlan 冻结——source slot 未交付就省略该 duplicate 并计 shortfall（`delivery.duplicate_shortfall`），禁止改选另一个 delivered source；不重排 ScenarioPlan、不回填其他 class、不无限循环。全部 target sequence 与 noise slot Delivered 时 `delivery.complete = true`（exact quota 指 primary sequence delivery，duplicates 不计入 quota）。SIGINT 发生在 delivery 期间：停止启动新 attempt、等待已发出的有界调用收束、原子交付已成功部分，写 `delivery.complete = false` 与 `delivery.interrupted = true`、exit 1；收束完成的 attempt 按正常桶记账，等待超时被放弃的在途 attempt 不计入 `attempts` 也不入任何 failure 桶（守恒等式只覆盖完整的非 fatal attempt）；同轮已发生 provider fatal 或 circuit breaker 时 exit 4 优先。

**provider 边界保持**：provider retryable exhausted 可以消耗下一次 delivery attempt；provider fatal 与 circuit breaker 仍立即走 exit 4，不被 quota refill 吞掉。对同一固定 prompt 可证明不会变化的 precheck context overflow 不重复派发（Brief / Realize → Exhausted 捷径 + `context_overflow` 桶，不再消耗后续 attempt）；由 realization content 引起的可变预算失败可以进入下一次 attempt。

**时间字段与 duplicate 的双时间语义**：幸存 primary 的 `time_fields` 绑定键在 duplicate 深拷贝与组装**之前**回填；duplicate 的 payload、tier、frame word 与全部 `time_fields` 绑定键是 source 深拷贝（携带 source 的时间字段值），artifact 行 timestamp 与 resource interval 使用 duplicate layout 的新时间——这个有意的双时间语义表示「在新 wrapper 时刻原样重发一份携带 source 时间字段的 payload」，保证 structured payload 仍可按 canonical JSON 精确判重；consumer 经 `truth.duplicate_of` 识别该例外（3.11.2、6.5）。

**RNG 流表（五流，互不借位）**：从 run seed 派生具名随机流，`Random(f"{seed}:<name>")` 独立构造——某 slot 多一次 content retry 不得改变其他 slot 的 length、timestamp、noise、profile 或 duplicate 抽签；slot retry 随机流由稳定 slot key 与 attempt count 派生：

| 随机流 | 唯一用途 |
|---|---|
| `scenario.preference` | length 与 duration target |
| `scenario.noise` | frozen noise timestamp 的空位选择 |
| `delivery.profile` | LLM profile / style 选择 |
| `delivery.content` | 各 slot attempt 的生成抽签 |
| `artifact.duplicate` | duplicate source 选择 |

**`--limit` 边界**：时间流形态不再接受 `--limit`——quota 是整体契约，截断后的前缀不再声称满足 quota（3.1.4.2、2.4）。

**观测面**：delivery 计数与 13 桶由 M6 供给、M10 装配——`report.generate.stream` 追加 `plan_digest` / `planner` / `delivery` / `quotas` 四新键并删除 `plan_failures` / `realize_failures` / `validator_scrapped` 三个旧计数器（其语义由 13 桶闭集唯一承接，3.10.3、6.4）；`delivery.attempts` 同时计 sequence 与 noise slot 的 delivery attempt，不计 LLMClient 内部 provider retry，也不把一次 sequence attempt 的 brief / realize 两个 call 误计成两次 delivery attempt。作废语义升级为交付语义：内容失败不再让序列静默缺席，而是在有界 attempt 内重试、耗尽后进 partial 交付——仍不产 failed 记录、不写 `item.errors`、不入 rejects（3.6.1 边界维持）。零新 trace 通道、零新事件、零新错误 kind（3.12.4 v1.17 段）。

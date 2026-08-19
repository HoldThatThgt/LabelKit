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

#### 时间流形态走查（v1.13，真跑数据）

`examples/synth-stream`（自含单 profile DeepSeek `config.toml`）2026-08-13 真跑，配置要点：两个序列类 `ticket_booking` / `smart_home` 各 `sequences = 3`、`len_range = [3, 5]`，三个帧类 `task_request`（带生成 Schema `{utterance, entities}`）/ `followup` / `confirmation`（后两者纯文本帧），`[generate.stream]` 取 `sessions = 5`、`noise_ratio = 0.1`、`duplicates = 1`、`frame_gap_s = [5, 60]`、`ts_start = "2026-01-05T09:00:00+08:00"`，`[stream]` 取 `order_by = "meta:ts"`、`gap_s = 900`，`[generate].num_per_call = 4`、`temperature = 0.9`。

| 观测面 | 实测值 |
|---|---|
| 退出码 / 守恒 | 0；`counts.generated = emitted = 6`，`failed` 与三个 `dropped_*` 全 0（退化式成立） |
| `report.generate.stream` | `sessions 5`、`crossed_sessions 1`、`sequences.ticket_booking = {planned 3, produced 3}`、`sequences.smart_home = {planned 3, produced 3}`、`frames 23`、`noise_frames 2`、`duplicates 1`、`plan_calls 6`、`realize_calls 6`、`noise_calls 1`、`plan_failures 0`、`realize_failures 0`、`validator_scrapped 0` |
| 工件 | 29 行（= 23 任务帧 + 2 噪音帧 + 4 重发帧）= `report.run.artifact.lines`；含结构化帧（`task_request` 落 `{utterance, entities}` 对象）与纯文本帧、一段交叉会话（两条序列交错）、流尾重发会话 |
| 主输出 | 6 行，按类分走两份**字段集互不相同**的标注 Schema；`_meta.run.rubric = "default:trajectory"`（空 rubric 自动解析生效）；`_meta.stream.members[]` 携帧类真值，`order_span` / `member_sources` 指向工件路径与行号 |
| LLM 用量 | `llm_usage.default.calls = 52`（= 6 蓝图 + 6 实现 + 1 噪音批 + 24 pointwise 打分（6 序列 × 4 准则）+ 记录级标注与评审调用，含一轮 verify 修复重标注——`schema_engine.resolved_at` 加总 7 = 6 条记录 + 1 次修复重标注，正是「加总 = 记录级标注调用数」的口径，6.4）；`--dry-run` 的静态估算为 49（不含重试与修复调用） |

工件重放（把 `out/synth-labels.stream.jsonl` 拷为某 process 模式工程的 `[run].input`、配同一份 `[stream]` 并开 segment）：29 帧 → **6 会话**（= `sessions 5` + `duplicates 1`）→ 6 episodes，`absorbed 28`、`dropped_noise 1`、`dropped_dup 1`（重发会话与原会话判重命中；实测档位 `near_text`——原会话吸收了一帧噪音使两侧成员数差一，噪音帧被剔除时则为 `exact`），退出码 0；加 `--strict` 预期退 1（有 rejects）。

### 3.6.5 时间流形态（v1.13）

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

- **抽签消费顺序表（冻结，测试钉住）**：全流程只用一条 `Random(f"{seed}:0:generate")`，按三段顺序消费——**计划期**（任何派发之前）① 配额按**类名字典序**展开为 (类名, 类内序数) 列表（`--limit` 在此做前缀截断，零 rng）② 逐序列 `L = randint(类有效 len_range)` ③ 逐序列 (llm, style) 预抽（`round_robin` 不耗 rng / `weighted` 逐位抽样；styles 非空时逐位均匀抽——噪音批调用紧随序列在同一预抽流内独立取全局 styles）；**派发期零 rng 消费**（3.6.2 既有纪律）；**交织期**（gather 之后、按幸存序列的计划序）④ 重复选取 ⑤ 装箱洗牌 + 前 `Σ幸存 − sessions_eff` 对成对交叉 ⑥ 逐交叉会话的切换点 ⑦ 逐噪音帧的 (会话, 槽位) 掷签 ⑧ 重发序列成流尾新会话（零 rng）⑨ ts 铺设。作废序列改变交织输入 ⇒ 确定性以 LLM 内容产出为条件（2.6 声明链）。**v1.14 零消费注记**：档位配分插在 ① 与 ② 之间、时间字段回填尾声接在 ⑨ 之后，两者**均零 rng 消费**——本顺序表原文不动（钉板测试原文回归），同 seed 下有无档位表与绑定表的抽签流逐字节一致。**v1.15 按类配分同为零消费**：配分改吃该类生效表（`effective_tiers`）仍是纯函数取值，顺序表原文继续不动，同 seed 下有无按类档位表的抽签流亦逐字节一致。
- **档位构成（v1.14 全局表 `[[generate.stream.tiers]]`；v1.15 增按类表 `[[class.<name>.generate.tiers]]`；默认均不在场）**：一个档位的定义**就是**该档序列的帧类构成集合，不携带质量指令、不控制帧内部语义质量（那归帧类生成指令与温度）。**生效表（v1.15）**——档位表分两级，一个序列类的**生效表** = `effective_tiers(该类表, 全局表)`：类声明了就用类的整张表（**表级原子覆盖**，不逐行合并——行级合并会让 rank 身份跨表漂移），未声明则回落全局表。全局表是**锚**（按类表要求其在场，3.1.4），故「档位面在场」⟺ 全局表非空，且每个参与类恒有非空生效表；`tier_rank` 是**类内身份**——每张生效表各自连续覆盖 1..N（N 逐类可不同），跨类同 rank **无任何工具语义**、跨类同构成合法。**下文凡「档」与「档位表」均读作该序列类的生效表**；无任何按类表时生效表恒 = 全局表，本行与 v1.14 逐字同义。**配分**——每个参与类的 `sequences` 配额按**本类生效表**各档 `weight` 走**整数域最大余额法**配到各档（基额 = `(sequences × weight) // Σweight`、余额键 = `(sequences × weight) mod Σweight`，按余额键降序、平票按 `tier_rank` 升序逐档 +1 至配满；**禁止任何浮点中间量**——平票判定要一路喂给类内序数分块 → truth → 工件字节 → 成员 id，是冻结面）；类内序数按 tier_rank 升序占**连续区间**，`(类, 类内序数) → tier_rank` 即配分结果的前缀和查表。配分是 `(sequences, 生效表)` 的**纯函数、零 rng**：它插在抽签消费顺序表的 ①配额展开与 ②长度抽取之间作为零消费步，顺序表原文不动，同 seed 下有无档位表（含按类表）的抽签流逐字节一致。推论：`--limit` 的前缀截断只切掉尾部序数，即在每个类内**从最高档序数侧截起**（低档序数在前），截断与档位映射可交换——按类表下各类的分块各不相同，本推论**逐类原文成立**。**构成恰等**——蓝图 Schema 的 enum 限档内子集给「⊆」、`cover_all` 注入的逐类 `contains` 给「⊇」，合成「一条序列的 members[] 帧类集合 ≡ 其档声明的构成」，故档位身份可从数据反推对账（可审计性；按类表下反推须先按行的序列类取其生效表，行内 `classification.label` 与工件 `truth.sequence_class` 天然给出该类名）。**标识三点**——`_meta.source.generator.tier_rank`（6.3）、工件 `truth.tier_rank`（6.5）、`report.generate.stream.tiers`（6.4）；三点的在场判据是**全局表非空**（全局表为锚 ⇒ 在场性恒定，不因某类未声明按类表而逐行漂移），全局表缺省时三点全部不在场。**M1 侧**的身份连续性、构成互异（逐生效表执行）、逐非零配额对的长度可覆盖、两条 WARN 与按类表前提三子款见 3.1.4。
- **蓝图调用**（一**计划期配额**序列一次——存活与否是此调用之后的事，故估算基数恒为 `2 × Σsequences`）：system = 计划器指令 + `[任务]` 类有效生成 instruction + `[帧类表]`（`name: description` 行）+ 结构句；user = 「请为一条「{类名}」序列产出 {L} 步蓝图。」；schema = `plan_schema(帧类名集, L)`（内部待遇，3.8.1）。**`[帧类表]` 的取值与 user 行随档位表条件化**（v1.14）：档位表缺省 ⇒ 表为**全类表**、user 行取上述原形（与 v1.13 逐字节一致）；档位表在场 ⇒ 表只渲染**档内子集**（帧类表按声明序过滤本序列所属档的 `frame_classes`；v1.15：所属档 = 该序列**类的生效表**内 `tier_rank` 对应的那一行——`effective_tiers(类表, 全局表)[tier_rank − 1]`，生效表连续覆盖 1..N 故下标法照旧）、user 行取冻结变体「请为一条「{类名}」序列产出 {L} 步蓝图，且 [帧类表] 中每个帧类都至少出现一次。」、schema 取 `plan_schema(档内名集, L, cover_all=True)`。修复穷尽 / 不可装填 ⇒ 该序列作废、计 `plan_failures`（覆盖违约不新增失败机制——它是普通的 L2 违规，进 M8 既有修复环）。模板 verbatim 冻结于 CONTRACTS §10.14——L0 关闭的端点（如 DeepSeek anthropic 路由硬拒强制工具调用）上，结构服从性靠模板内嵌的结构契约与覆盖句兜底，**这是硬要求不是兜底优化**。
- **帧实现调用**（一蓝图一次）：system = `[任务]` 类有效 instruction + `[风格要求]`（预抽 style，可缺——蓝图不带风格）+ 结构句 + **逐位契约行**「第 i 帧（{帧类}）须符合：{该帧类生成 Schema 文本 | 自由文本一段}」；user = 蓝图逐步行 `i. [{帧类}] {brief}` + 「请实现全部 {L} 帧内容。」；schema = `realize_schema(逐步 Schema 序列)`（`prefixItems` 逐位包装，纯文本帧位取 `{"type": "string"}`）。**逐位 Schema 与契约行取的是缩减 Schema**（v1.14）——声明了时间字段绑定的帧类，其绑定键从「该帧类生成 Schema」中剔除后才进这两处（见下方「时间字段回填」行；无绑定帧类与纯文本帧位逐字节不变）。结构化帧的帧内容**以对象原样**落工件行的文本字段（纯文本帧落字符串），成员 `Record.text` 取其 M2 语义投影（对象 ⇒ canonical JSON，字符串 ⇒ 直取——与重放时 M2 的点路径抽取产出同一投影，6.5）。修复穷尽 / 降级穷尽 ⇒ 序列作废、计 `realize_failures`。模板 verbatim 冻结于 CONTRACTS §10.15。
- **噪音批量实现**：复用 3.6.2 的既有生成模板与 `samples_schema`，调用数 = `⌈噪音帧数 / generate.num_per_call⌉`（噪音帧数 = `round(noise_ratio × Σ任务帧数)`）；单批作废 ⇒ 缺额帧从交织中缺席（**不补生成**）。
- **逐帧钩子与序列相似度过滤**：`generate.sample_validator` 对帧实现产物**逐帧文本**执行——任一帧违规 ⇒ **整序列作废**（蓝图定长不可剔单帧，拒绝采样语义）并计 `validator_scrapped` 与桶 `rejected_by_validator`；随后 M6 内置的相似度过滤单元上移为**序列级**：判重文本 = 成员 text 按序 `"\x1e"` 拼接（M3 序列配方同式）、比对面 = 兄弟序列（本形态无种子）、参数取 `[dedup]` 三键，淘汰以桶 `survived_dedup` 差呈现（桶键在本形态为 `<class>×<llm>×<style>` 三段式——generate_only 首现类段）。
- **机械交织器（零 LLM 零 IO）**：`sessions_eff = min(sessions, Σ幸存)`，交叉对数 = `Σ幸存 − sessions_eff`（M1 已静态保证 `sessions ≤ Σsequences ≤ 2 × sessions`，故交叉并发度恒 k ∈ {1,2}）；单个交叉会话形态 = **A 段 + B 段 + A 余段[+ B 余段]**（切点 `cut_a ∈ [1, |A|−1]`、`cut_b ∈ [1, |B|]` 保证真交叉；一方不足 2 帧时与另一方互换，两方都不足则退化为顺次拼接——纯长度条件、零 rng）；噪音帧逐帧掷签 (会话, 槽位)，**满员会话**（`len ≥ stream.session_max_len`）退出签池，签池耗尽 ⇒ 余帧缺席 + WARN；重发序列（`duplicates`，超出幸存数时钳制 + WARN）取自幸存集、帧内容逐字节同源、恒落**流尾新会话**；ts 铺设自 `ts_start` 起严格递增——会话内帧间隔 `uniform(frame_gap_s)`、跨会话间隔 `uniform(gap_s + lo, gap_s + hi)`（恒 > `stream.gap_s` ⇒ 摄取侧按同一 gap_s 复演出相同会话切分），微秒精度 ISO-8601 写出。交织尾声统一回填 `truth.session`（全流会话序数 0 基）。
- **时间字段回填（v1.14，`[frame.class.<name>.generate.time_fields]`；默认不在场）**：帧生成 Schema 里的时间语义字段与实际帧间隔本就对不上——LLM 没有时间轴，写出来的秒数是编的。处置是「绑定即剔除 + 机械回填」两步。**绑定即剔除**：被绑定字段从 LLM 面向的**逐位 Schema 与逐位契约行**中一并剔除（缩减 Schema = 生成 Schema 删该 `properties` 键、`required` 取差集、其余关键字原样；派生须重建顶层与 `properties` 两层，**绝不就地改动 M1 冻结的帧类生成 Schema**——静态预算预检与契约行渲染同源读它），M8 按缩减 Schema 校验，LLM 越权输出绑定字段则按 `additionalProperties` 违规进修复环。不为注定被覆写的字段付 token 与修复环成本，契约也不误导。**机械回填**：新的回填尾声（零 rng 零 LLM 零 IO）位于 ⑨ts 铺设之后、直装组装之前，只遍历任务帧槽位并按所属序列归组（会话序即序内成员序——交叉切片不改序内次序），对绑定帧类逐帧按语义词表算值**就地写入共享载荷对象**——`ts` = 该槽位已铺 ISO 串；`gap_prev_s`/`gap_next_s`/`elapsed_s` = **本序列相邻成员**/首帧的 ts 差 `round(·, 6)`（微秒精度与 isoformat 对齐），首帧的 `gap_prev_s`/`elapsed_s` 与末帧的 `gap_next_s` 恒 0.0。序内口径的理由：交叉会话夹入的外序列帧与噪音帧本就占用其间墙钟，序内差值才与下游从数据实测的口径一致。重发槽位**不遍历也不触碰**——它与源槽位引用同一载荷对象，回填自动生效且其 `ts` 绑定值承源、≠ 自身行 ts（原样重发本就携带陈旧内容，语义自洽）；噪音帧与无绑定帧类不触碰；每个载荷对象恰被写入一次。**位次的两条推论**：① 回填先于行对象与 id 计算 ⇒ `Record.raw`/`text`/成员 id、序列 id 与 session_id 全部含回填值，工件重放逐字节同 id 同会话（重放侧的判重档位仍随分段判决浮动，「逐字节一致」≠「重放判重恒 exact」）；② `sample_validator` 逐帧校验与序列相似度过滤在交织之前完成，故二者吃的是**回填前**载荷——时间量是机械量，不参与内容校验与内容判重（两序列内容相同仅时间不同 ⇒ 照旧判近重，语义正确）。绑定字段上除 `type` 外的约束关键字不被强制也不被校验（M1 为此发 WARN，3.1.4）——时间量的值域由时间轴决定。
- **直装组装**：逐槽位构造工件行对象 `{<ts字段>: …, <text_field>: …, "truth": {…}}`（真值键集见 6.5）——成员 `Record.raw` = **该行全对象**、`id = sha256(canonical_json(raw))[:16]`（M2 公式 ⇒ 工件重放同 id）、`text` = text_field 值的 M2 语义投影、`ref = RecordRef(source_file=工件路径, line_no=行号, pair_index=None, generated_from=(), generator={"llm","style"})`（v1.14：档位表在场时 generator 取 `{"llm","style","tier_rank"}` 三键——v1.15 下键、序与在场判据均零改动，只是值来源为**本行序列类生效表**内的档序数，跨类不可比）；`session_id = sha256("\n".join(会话内全部帧 id))[:16]`（M2 公式，**含噪音帧与重发帧** ⇒ 重放一致）；逐序列构造 `Record(kind="sequence", members=…, text/raw/ui_tree/image=None, ref=首成员 ref, id=sha256("\n".join(member ids))[:16])`（M14 公式，S24 字段惯例）与信封——`classification = Classification(label=序列类, labels=(label,), source="inherited")`、`member_classifications = {成员 id: Classification(帧类, (帧类,), "inherited")}`（帧类真值随 members[] 落盘，3.11.2）。**噪音帧与重发帧只活在工件**（不构造信封、不进守恒账），重发序列本体早已在 envelopes 中、不重复入列。
- **`--limit` 与量目标**：单位 = **序列**，截断在计划期配额层（类段字典序前缀）——作废序列不再生成、不进交织 ⇒ 工件与主输出的覆盖面恒一致；M10 尾部另有一次 belt & braces 截断。按类 `sequences` 是**尝试配额**（`standalone_count` 同款语义）：无输出条数保证、无补齐回路（8.3 O6 辖区不变）；`counts.generated` = 进链序列条数。
- **作废语义**（3.6.1 边界的序列版）：蓝图 / 帧实现 / 逐帧钩子任一环节失败 ⇒ 该序列缺席，**不产 failed 记录、不写 `item.errors`**（种子概念不存在，无「原批」可损）；留痕 = 计数器 + 一行值-free stderr WARN（`seq=` 序号 / `class=` / `llm=` / `call=` / `kind=`）。
- **预算与溢出纪律（v1.11 家族）**：三类调用发出前都做 `est(system) + est(user) + 2 × 消息包封 ≤ input_budget`（`supports_structured_output` 时另扣 response schema 文本）的预检——不可装填即**从不发出**（V10 先例，precheck **永不喂熔断**）；帧实现的反应式溢出走**序列对半分**（schema 与蓝图概要随切片同步减半，≤ 2 级 AIMD，每次计 `budget.degrade_retries`；单步跨度或级数耗尽 ⇒ 序列作废），reactive-400 终局在作废吞点经共享 `budget.feed_reactive_terminal` 补喂熔断**恰一次**（A7 纪律，7.6）。`TEMPLATE_HEAD_TOKENS` 增 `generate_plan` / `generate_realize` 两键（噪音批复用 `generate` 键值），M1 静态预检增对应两段（3.1.4）。
- **计数与观测**：`report.generate.buckets` 照常（`calls` / `produced` / `survived_dedup` / `rejected_by_validator`）；新增 `report.generate.stream` 子块（counts-only 12 键：`sessions`（交织出的会话数，**不含重发尾会话**；作废序列会使其低于声明的 `sessions`——交织按 `sessions_eff = min(sessions, Σ幸存)` 装箱）/ `crossed_sessions` / `sequences.<class>.{planned, produced}`（`produced` = 该类**最终进链**的序列数，即过了蓝图、帧实现、逐帧钩子**与序列相似度过滤**四关的条数——`planned − produced` 的缺口按环节分摊在 `plan_failures` / `realize_failures` / `validator_scrapped` 与相似度淘汰上，后者只以桶 `survived_dedup` 差呈现）/ `frames`（幸存序列的任务帧总数，不含噪音帧与重发帧）/ `noise_frames`（实际织入数）/ `duplicates` / `plan_calls` / `realize_calls`（含对半降级后的分次调用）/ `noise_calls`（三个调用计数在**派发前**递增，故含被预算预检拦下、从未发出的调用——平面路径同款口径）/ `plan_failures` / `realize_failures` / `validator_scrapped`，6.4）；`report.run` 摘要族增工件条目（路径 / sha256 / 行数）。**v1.14 档位面增量**：`report.generate.stream` 增 `tiers` 子块（counts-only，条件在场 = **全局档位表非空**——全局表为锚故这一判据在 v1.15 下零改动；**键位冻结在 `sequences` 之后、`frames` 之前**（配额族相邻））——口径与 `sequences.<class>` 同款：`planned` 计于计划期逐序列、`produced` 数最终进链（过了蓝图、帧实现、逐帧钩子与序列相似度过滤四关）的条数。该子块由 M10 在报表装配时**按声明档位表显式铺开**（3.10.3），故零额档与全作废档也如实在场（planned 0 / produced 0），不依赖计数器首触序。**v1.15 双形**（裁决·嵌套报表全类铺开）：全部序列类都未声明按类表 ⇒ **平面形** `{"<tier_rank>": {planned, produced}}`，按全局表 rank 升序铺开，与 v1.14 报表**逐字节相等**；任一按类表在场 ⇒ **类嵌套形** `{"<class>": {"<tier_rank>": {planned, produced}}}`，外层 = **全部声明序列类按声明序**零基铺开（`sequences.<class>` 同款）、内层 = 该类生效表 rank 升序，键均为十进制字符串（落盘无 sort_keys ⇒ 键序 = 装配插入序），零配额类与全作废档同样如实呈现 0/0。两形下键位都仍冻结在 `sequences` 与 `frames` 之间。**计数器键按类重冻结**（裁决·计数器键按类重冻结）：M6 恒喂类段键 `generate.stream.tiers.<class>.<rank>.{planned, produced}`（单一喂数纪律，禁双写两族键），平面形由 M10 按 rank **跨类求和**装配——数值与 v1.14 逐字节相等（v1.14 的平面计数本就是跨类聚合值）。**时间字段面零观测增量**（确定性机械操作，无可计数的失败模式）。**零新 trace 通道、零新事件**（两类调用经 `llm.call` 可见；generate 专属通道列 8.4 演进候选）、**零新错误 kind**（7.6；覆盖违约复用 `schema_violation` 进修复环、作废复用 `plan_failures`；v1.15 的按类表前提错误走既有 CONFIG_ERROR 面）。**零调用数变化**——档位（含按类化）与时间字段三个机制均不改调用次数，`estimate_run`、估算行格式、八个 dry-run golden 与 console 键集全部零改动（3.10.3、7.8）。

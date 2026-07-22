## 3.4 M4 质量打分 quality（QuRating）

### 3.4.1 职责与边界

**做：**按 rubric 对批内存活记录打质量分：pairwise 模式执行「k 轮随机配对 → LLM 裁决 → Bradley-Terry 拟合 → 批内百分位归一化」；pointwise 模式执行 0–5 加性打分归一化。计算加权聚合分，按阈值标记 `dropped_lowq`。 
**不做：**不定义 rubric 内容；不决定被过滤记录的物理去向；不做标注语义正确性评审（那是 M7）。

### 3.4.2 输入 / 输出

| 方向 | 内容 |
|---|---|
| 输入 | 批内 `status="active"` 的 PipelineItem；Rubric；LLM profile。 |
| 输出 | 每条记录 `item.scores: dict[str, QualityScore]`（每 criterion 一项 + `"__aggregate__"`）；低于阈值者 `status="dropped_lowq"`。 |

### 3.4.3 算法：pairwise + Bradley-Terry（主模式）

图 3-2 QuRating pairwise 模式流程

关键设计的精确定义：

| 设计点 | 定义 |
|---|---|
| 比较池 | = 当前批（`run.batch_size`）。QuRating 原文在语料分片内采样成对比较 [1]；批即本工具的采样域。批间分数不可直接比较（百分位为批内相对量），报告中按批记录分布。需要跨批可比时使用 pointwise 模式（绝对刻度）。 |
| 配对方案 | k 轮独立随机完美匹配（洗牌后相邻配对）。每记录恰好参与 k 次比较；k 轮随机匹配的并图为随机 k-正则图，k≥3 即高概率连通，默认 k=4 兼顾成本与 BT 可辨识性；孤立分量由正则化伪计数兜底。 |
| 裁决提示词 | 系统提示 = rubric 全部 criteria 的 pairwise_prompt 拼接；用户消息 = 记录 A、B 内容（UI 模态为两组「截图+序列化树」，需 profile 支持多图）；要求输出 JSON：`{"judgments": [{"criterion": key, "winner": "A"\|"B"\|"tie", "reason": str}]}`（`reason` 仅当 `quality.judgment_reasons` 生效时要求——一句话裁决理由，写入 trace 日志供 rubric 优化使用，见 7.5），经 M8 内部 Schema 校验。单次调用裁决全部 criteria（QuRating 为每 criterion 独立询问 [1]；合并询问是成本优化，`quality.criteria_per_call = "all"`（默认）\| `"single"` 可切回原文行为）。 |
| 位置偏差 | 每次比较 A/B 呈现顺序由 PRNG 随机；k 轮聚合平均化残余偏差（Zheng et al. 位置偏差缓解 [20]）。 |
| BT 拟合 | 每 criterion 独立：极大似然 MM 迭代 θᵢ ← Wᵢ / Σ_j nᵢⱼ/(θᵢ+θⱼ)（Hunter 2004 [10]），每轮后归一化 Πθ=1；收敛条件 max\|Δlogθ\| < 1e-6 或 200 轮。正则化：每记录附加 λ=0.1 次对虚拟对手（θ=1）的半胜半负，保证全胜/全负与孤立分量下 θ 有限且唯一。 |
| 归一化与聚合 | 每 criterion 独立：将批内全部 log θ 升序排名（并列取平均秩 rank），`score = (rank − 1)/(N − 1)`；N=1 时 score=0.5。得分域 [0,1]，批内最低 0、最高 1。聚合分 `__aggregate__` = Σ wᵢ·scoreᵢ / Σ wᵢ（wᵢ 为 rubric 权重；score 为 null 的 criterion 不计入分子分母）。 |
| 选择机制 | `quality.selection = "threshold"`（默认，现行为：聚合分 < `quality.threshold` ⇒ `status="dropped_lowq"`；threshold 缺省则只打分不筛）\| `"top_ratio"`（批内按聚合分降序保留 ceil(`top_ratio` × 批内存活数) 条，其余 `dropped_lowq`；`quality.top_ratio` ∈ (0,1] 必填，与 threshold 互斥，M1 校验）。排序与并列规则：按聚合分降序，聚合分相同时按记录 id 字典序升序作确定性平局裁决（同输入同 seed 可复现）；名额基数「批内存活数」= 批内 score 非 null 的存活记录数（score=null 的 on_unscored 保留记录不计入基数、也不占名额）。top_ratio 在 pairwise 与 pointwise 下均定义良好——两种模式的质量门输入同为批内聚合分排序，是流式场景做定量筛选的推荐姿势；需要「恰好全局 N 条」时须两阶段方案，见 8.3 O6。按 `on_unscored="keep"` 保留的未打分记录（score=null）不占名额、直接保留。 |
| 裁决失败 | 单次比较经 M8 修复仍非法 ⇒ 该比较按 tie 计（对 BT 中性），计入 `report.quality.judgment_failures`；某记录全部比较失败 ⇒ 该记录 score 置 null 并按 `quality.on_unscored = "keep"`（默认）\| `"drop"` 处理。 |
| 多评审团（可选） | `quality.judges` 配置奇数个 LLM profile（默认 `[]` = 单评审，用 `quality.llm`）。每次比较由各 judge 以同一呈现顺序独立裁决；per-criterion 取多数票——A/B/tie 三类计票，某类得票过半 ⇒ 取该类，无类别过半 ⇒ tie；BT 拟合取多数结果。trace 中每 judge 各写一条 `quality.judgment` 事件（payload 增加 `judge` 字段 = profile 名；7.2 契约只增不改）。成本 = 单评审 × \|judges\|。背书：PoLL [32]——异构小模型评审团在三种评审设置、六个数据集上优于单一大评审，且显著降低模型内偏差。 |
| 双顺序裁决（可选） | `quality.both_orders = true`（默认 false）时，同一对记录以正反两种呈现顺序各裁决一次（多评审团下每 judge 各判两次）；per-criterion 两次结果一致（换序后仍指向同一记录）⇒ 记该 winner，不一致 ⇒ tie。合成次序固定：先 per-judge 做双顺序一致性合成，再跨 judge 取多数票。相对「位置偏差」行的随机化缓解，本机制将位置偏差系统性消除（Zheng et al. 的位置一致性判定 [20]）。trace：正反两序各为一次独立裁决，每 judge 各写两条 `quality.judgment` 事件（以 `order` 字段区分两序）。成本 ×2。 |
| 按类分池（v1.7） | classify 启用时，批内 active 项按 `classification.label` 分池，池 = 类内存活记录；classify 关闭 ⇒ 单一匿名池 = 现行为（零变化回归锚）。**两阶段执行**：先同步按类名字典序逐池预抽配对计划（消费 `ctx.rng`，消费序确定），再跨池合并为一个 gather 派发 LLM 调用（跨池满并发，不损吞吐）。每池取 `class_views[label]` 的类有效 (QualityConfig, Rubric)：mode / rounds / rubric / threshold / selection / top_ratio 池内生效；judges / both_orders / criteria_per_call / llm / on_unscored 恒为全局（5.2 按类覆盖白名单表）。池级 try/except 隔离——某池内部错误不波及其余池（记录级隔离原则的池级推广，1.3）。N=1 池沿用单条规则（不发裁决调用、score 固定 0.5，本表「归一化与聚合」行）；top_ratio 名额基数 = **池内** scored 存活数。pairwise 分数语义相应收窄为「批内类内相对」，`_meta.scores` 增 `pool` 字段（= 类名，仅 classify 启用时出现）自述比较池（6.3）。计数器与统计升维：classify 启用时 tie 计数器键为 `quality.tie_outcomes.<pool>.<crit>`（`tie_comparisons` 同），report 顶层 `quality.mode/rounds` 保留（= 全局继承基值）、增 `quality.by_class` 每池视图（每池携带有效 mode/rounds，6.4），`per_criterion_tie_rate` 输出条件改为「存在 pairwise 池」；`quality.bt_fit` / `quality.gate` / `quality.judgment` 事件 payload 增 `pool` 字段（7.2 只增）；关闭时计数器键式与报表形状不变。 |
| 序列打分（v1.8） | stream 模式下序列信封（`record.kind = "sequence"`，3.14）的**记录内容段**改走序列变体，两小节按序（逐字冻结于 CONTRACTS §10.2/§10.3——pairwise 下嵌入 `[记录 X]` 内容槽、pointwise 下替换 `{record content}`，标签不变）：`[步骤序列]`（`item.transitions` 按 3.5.2 步骤行格式逐行文本渲染，transitions 为 None 时整段省略；**fallback 步分列**——`Transition.detail.kind == "extraction_invalid"` 的兜底步行尾加「（摘取兜底）」后缀，与 LLM 确证的 other 可区分，防兜底噪声污染连贯性锚点，S16；**接缝步分列（v1.9）**——`Transition.detail.kind == "thread_seam"` 的占位步行尾加专用后缀「（线索接缝：被 {interrupted_by} 打断）」，与 extraction_invalid 后缀并列——防 trajectory rubric 的 noise_residue / coherence 判据把接缝当噪声残留或无法解释的跳变扣分，3.15.4/3.16.4）+ `[成员帧摘要]`（逐成员 `frame_digest`（4.3）按成员序每帧一行，总量有界）。**无图**——UI 模态亦纯文本打分（vision 逐阶段表的放宽项：`quality.llm` 不因 stream 要求 supports_vision，S30，3.1.4/5.2；v1.9 起 `stitch.llm` 同为纯文本恒不要求，3.16.3）。transitions 与预渲染文本经 `_judge_once` / `_pointwise_once` 的新增私有形参下穿（私有签名，非冻结面）；trace `excerpt` 档对序列的摘录 = 成员摘要渲染的前 200 字符（`_excerpt_payload` 序列分支，7.4）。**rubric**：stream 下 `quality.rubric` 空串解析为 `default:trajectory`（S29，3.1.4）——内置轨迹四准则（completion / coherence / purposefulness / noise_residue），全文与背书拆分注记见附录 A.3（completion/coherence 源自 OS-Genesis TRM [41]，1–5 五级改制为 0–5 六级；purposefulness 自 Coherence "toward the goal" 拆分；noise_residue 源自 RPA 日志分割噪声处理 [50]）；rubric 由既有机制消费、零改动。`extract.enabled = false` 时步骤段缺席、**退化为帧摘要打分**——rubric 措辞模态中立、「步骤」读作「帧间变化」（M1 对该组合发 warning 指引，3.1.4）。**门控**：stream 下 `quality.threshold` 缺省 = 只打分不筛（现语义，对 stream 尤其合理——TRM 消融与 E2E 台账 #6 佐证，1.6）。**信度注记**：长 episode（> 20 步）下整体式 LLM 判分信度随长度衰减（GUIDE 长度退化数据 [57]）——建议 pairwise（批内相对比较）或对绝对分降信任、按 episode 长度分层审计。**打分单元（v1.9）**：stitch 启用时打分单元升维 episode → **线索**（thread，缝合后的幸存序列信封——被并壳被 active 过滤天然排除，3.16）；序列变体机制原样，仅步骤序列与成员摘要作用于重绑后的成员集，缝合并入使打分调用数随行数下降（3.16.4 调用与校验行）。 |
| 上下文预算装填（v1.11） | 裁决 profile 声明 `context_window` 时按上下文预算装填单次裁决调用（未声明 = 预算关闭，行为与 v1.10 一致；预算/估算/校准机制见 3.9）。**pairwise**：记录侧预算 = `input_budget − est(系统提示 + 准则文本)`，按**每评审各自构建**（逐 (对, 评审) 装填、取本评审 profile 的预算——与 verify 的「评审团最小预算」形态不同，V25②），两记录各半；每侧 UI 树渲染动态封顶 `min(input.ui_tree_max_chars, 预算折算字符)`（渲染后按 est 复核，超则按行丢尾、保留既有 `…(truncated N nodes)` 标记；`ui_tree_max_chars` 保留为绝对上限）；附图（UI 对 2 张）按校准单价计 est ×2 后再分。**序列变体**：`[步骤序列]` 步骤行块获得预算份额，超出按「首末步恒保留、丢中段整行 + 原位标记」裁剪（与成员摘要块既有截断语义同族，V9）。**pointwise** 单记录同族（树渲染动态封顶 + 步骤行同款裁剪）。**溢出反应（V20）**：识别到 provider 上下文溢出 → 收紧文本份额**重试一次**（有界降级）；连语义最小单元（pairwise 2 记录 / pointwise 单记录）都装不下 → 该记录记 `StageError(kind="context_overflow")`、`status="failed"` 入 rejects（V10，7.6——区别于本表「裁决失败」行的 tie 折算：溢出是记录内容不可装填，非裁决输出非法）。逐裁剪点计入 `report.budget.truncations`（6.4）。 |

### 3.4.4 算法：pointwise 加性打分（低成本模式）

每记录 1 次调用：提示词呈现该 criterion 的 6 级加性量表（0–5，逐级累加式描述，附录 A 给出全文），要求模型先给两句理由再给整数分（判分与理由分离缓解冗长偏差 [20]；加性量表为 FineWeb-Edu 验证的最优形式 [11]）。输出 `{"scores": [{"criterion": key, "reason": str, "score": 0..5}]}` 经 M8 校验；score 归一化为 /5。聚合与质量门同 pairwise。两种模式共用同一 rubric 的不同字段（pairwise_prompt / pointwise_levels），切换零迁移成本。

### 3.4.5 API 与配置

```
class QualityStage(Stage):
    name = "quality"
    async def run(self, batch, ctx) -> list[PipelineItem]: ...

def fit_bradley_terry(n_items: int, comparisons: list[tuple[int, int, float]],
                      l2_pseudo: float = 0.1, tol: float = 1e-6, max_iter: int = 200) -> np.ndarray:
    """comparisons: (winner_idx, loser_idx, weight)；tie 拆为两条 weight=0.5。返回 log-theta 数组。"""
```

配置见 5.2 `[quality]`。

**背书：**算法主体逐点对应 QuRating（ICML 2024）[1]：成对判断优于绝对打分的结论、BT 标量化、rubric 四维度均出自该文；本工具以「运行时 API 裁决」替代其「训练 QuRater 分类器离线打分」（无状态约束所致，1.6 节已对齐）。pointwise 模式为 FineWeb-Edu（NeurIPS 2024 D&B）验证的加性量表方案 [11]。BT 拟合采用 Hunter 的 MM 算法 [10]，为该模型的标准数值方法。多评审团为 PoLL [32] 的评审团方案；双顺序一致性判定为 [20] 的位置偏差消除做法。

### 3.4.6 输入 / 输出示例

以下用一个可手工核对的最小批完整走查 pairwise 主模式。设定：`run.modality = "text"`、`input.text_field = "instruction"`、`run.seed = 42`；`quality.mode = "pairwise"`、`quality.rounds = 2`（k=2，演示用，默认为 4）、`quality.threshold = 0.3`、`quality.rubric = "inline"`——rubric 仅含一条 criterion：`educational_value`（weight=1.0，`pairwise_prompt` 与 `description` 取自附录 A.1）。批内存活记录 N=4，调用成本 = N·k/2 = **4 次**（单 criterion 下 `criteria_per_call` 取值不影响次数）。本例另设 `trace.enabled = true`（channels 默认含 quality），故 `quality.judgment_reasons = "auto"` 档生效，裁决输出携带 reason（5.2、7.5）。

**① 输入记录**（输入法采集的中文指令 JSONL，四行，M2 摄取后按 3.2.5 规则生成 id）：

```
{"instruction": "帮我写一条请假条，明天上午要去医院", "source": "ime-log", "ts": "2026-06-30T10:12:00Z"}
{"instruction": "哈哈哈哈哈哈", "source": "ime-log", "ts": "2026-06-30T10:14:05Z"}
{"instruction": "解释一下二分查找为什么是 O(log n)，能不能举个在通讯录里找人的例子", "source": "ime-log", "ts": "2026-06-30T10:15:47Z"}
{"instruction": "把“会议改到周五下午三点”翻译成英文", "source": "ime-log", "ts": "2026-06-30T10:18:22Z"}
```

依次记为 r1–r4，id（sha256 前 16 hex）：r1=`1cda030abc565f17`，r2=`91eaaa968c26ab62`，r3=`fd97f67330e81315`，r4=`03dddff294e481c8`。

**② 配对与裁决结果**（seed=42 的 PRNG 每轮洗牌后相邻配对；A/B 呈现顺序同样由 PRNG 随机）：

| 比较 | 轮 | 呈现 A | 呈现 B | winner | BT 折算 |
|---|---|---|---|---|---|
| c1 | 1 | r3 | r1 | "A" | r3 胜 r1（权重 1.0） |
| c2 | 1 | r2 | r4 | "B" | r4 胜 r2（权重 1.0） |
| c3 | 2 | r3 | r2 | "A" | r3 胜 r2（权重 1.0） |
| c4 | 2 | r1 | r4 | "tie" | 双向各记 0.5 胜（3.4.3） |

**③ 单次比较的报文**（以 c1 为例；提示词按 3.4.3 组装，输出经 M8 内部 Schema 校验）：

```
system:
  你将对两条记录进行成对质量比较。准则如下：
  - educational_value: 教育/训练价值：作为模型训练数据能带来多少可学习的能力。
    比较两段文本，哪一段更有教育价值、更值得用于训练语言模型？
  对每条准则给出裁决。输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：
  {"judgments": [{"criterion": <准则 key>, "winner": "A"|"B"|"tie", "reason": <一句话理由>}]}
user:
  [记录 A] 解释一下二分查找为什么是 O(log n)，能不能举个在通讯录里找人的例子
  [记录 B] 帮我写一条请假条，明天上午要去医院

← 响应:
{"judgments": [{"criterion": "educational_value", "winner": "A",
                "reason": "A 要求讲解算法复杂度并配实例，覆盖推理与解释能力，可学习内容明显多于 B 的日常写作请求。"}]}
```

**④ BT 拟合（MM 算法，3.4.3）**：胜场计入 W 后，每记录再附加 λ=0.1 次对虚拟对手（θ=1）的半胜半负（W 各 +0.05，分母各加 0.1/(θᵢ+1)）；迭代 θᵢ ← Wᵢ / Σⱼ nᵢⱼ/(θᵢ+θⱼ)，每轮归一化 Πθ=1。本例在 200 轮上限处终止（此时 max|Δlogθ| 约 4×10⁻⁶，尚未达 1×10⁻⁶ 收敛线，log θ 前 3 位小数早已稳定）。

**⑤ 归一化与 ⑥ 质量门**：score = (rank−1)/(N−1)，rank 为 log θ 升序排名（并列取平均秩）；单准则 weight=1.0 时聚合分 = 该准则分：

| 记录 | comparisons | wins / ties | log θ | 升序 rank | score | __aggregate__ | 质量门（threshold=0.3） |
|---|---|---|---|---|---|---|---|
| r2 | 2 | 0 / 0 | -3.021 | 1 | 0.000 | 0.000 | < 0.3 ⇒ `status="dropped_lowq"` |
| r1 | 2 | 0 / 1 | -0.082 | 2 | 0.333 | 0.333 | active |
| r4 | 2 | 1 / 1 | 0.082 | 3 | 0.667 | 0.667 | active |
| r3 | 2 | 2 / 0 | 3.021 | 4 | 1.000 | 1.000 | active |

r1 写入 `item.scores` 的内容（4.2 `QualityScore` 的 JSON 形态）：

```
"scores": {
  "educational_value": {"criterion": "educational_value", "score": 0.333, "mode": "pairwise_bt",
                        "detail": {"comparisons": 2, "wins": 0, "ties": 1, "log_theta": -0.082}},
  "__aggregate__":     {"criterion": "__aggregate__", "score": 0.333, "mode": "pairwise_bt",
                        "detail": {}}
}
```

r2 被标记 `dropped_lowq`，按 `output.rejects` 策略进入 rejects 通道并计入 `report.counts.dropped_lowq`；r1、r3、r4 保持 active 进入 M5。

**⑦ pointwise 低成本模式对照**（`quality.mode = "pointwise"`，对 r1 仅 1 次调用；量表取附录 A.1 `educational_value` 的 `pointwise_levels`）：

```
system:
  按以下 0–5 加性量表为记录的 educational_value（教育/训练价值）打分，先给两句理由再给整数分：
  0: 无学习价值（噪声、纯广告、无意义重复）。
  1: 学习价值极低，内容浅表。
  2: 在 1 的基础上，有一定可学习内容但组织松散。
  3: 在 2 的基础上，内容系统、有清晰的知识或任务示范价值。
  4: 在 3 的基础上，示范性强，覆盖推理/解释/结构化表达等能力。
  5: 在 4 的基础上，训练价值突出，属稀缺的高质量样本。
  输出 JSON：{"scores": [{"criterion": <准则 key>, "reason": <两句理由>, "score": 0..5}]}
user:
  [记录内容] 帮我写一条请假条，明天上午要去医院

← 响应:
{"scores": [{"criterion": "educational_value",
             "reason": "该指令是意图明确的写作示范任务，包含时间与事由等具体要素。但任务简单，不涉及推理或专业知识，可学习内容有限。",
             "score": 3}]}

→ 归一化 3/5 = 0.6：{"criterion": "educational_value", "score": 0.6, "mode": "pointwise",
                     "detail": {"raw_score": 3, "reason": "该指令是意图明确的写作示范任务……"}}
```

**提示：**r1 在 pairwise 下得 0.333、在 pointwise 下得 0.6，二者不矛盾——前者是批内相对百分位（本批恰有 r3 这类强样本压秩），后者是绝对刻度（3.4.3「比较池」行）。需要跨批可比时选 pointwise。

#### ⑧ top_ratio 选择示范

沿用本例 4 条记录的最终得分（r2=0.000、r1=0.333、r4=0.667、r3=1.000），改设 `quality.selection = "top_ratio"`、`quality.top_ratio = 0.5`（此时不设 `quality.threshold`，二者互斥，M1 校验）：批内按聚合分降序保留 ceil(0.5 × 4) = 2 条 ⇒ r3、r4 保持 active，r1、r2 置 `status="dropped_lowq"`。对比 ⑥ 中 `threshold = 0.3` 只丢 r2 一条：threshold 按绝对分数线筛、每批淘汰数随分布浮动，top_ratio 按批内名次筛、每批保留比例恒定。

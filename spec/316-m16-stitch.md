## 3.16 M16 stitch——线索缝合

### 3.16.1 职责与边界

**做：**（v1.9 新增算子）对批内候选会话执行线索缝合：以「单调选池 LLM 判定 × 机械先验合取」把同一目标导向任务被穿插切开的碎片（episode）保守缝合为**线索（thread）**，有界二遍复评修正顺序贪心的漏缝（3.16.4）；被并 episode 信封壳置 `status="stitched"`、幸存信封 Record 重绑（4.3 契约 ②c）；`below_min_len` 短段按连续 run 重组为救援候选先进候选池，命中时成员帧 `dropped_noise → absorbed` 翻转（②c③）；对多碎片线索机械标定接缝（`seam_indexes` duck 标，零 LLM——接缝转移由 M15 按 T10 四键占位，3.15.4）。产出三级结构 **thread ⊃ fragment ⊃ step**（对齐 Ego4D Goal-Step goal⊃step⊃substep [69] 与 AndroidControl goal⊃instruction⊃action [45]）；帧永远单一归属——交叉用「平面分段 + 线索身份」表达（Goal-Step `is_continued` 同型 [69]；PIRA 把任务子轨迹形式化为**非连续帧子集** [64]），不引入帧多重归属。链序位于 segment 之后、dedup 之前（3.10.3）——缝合改变成员集，必须先于判重（线索判重面 = 重绑后成员配方，3.3.3）与摘取（接缝序数占位，3.15）。`stitch.enabled = false`（默认）时本算子不入链，**主输出、rejects、report.json 与 v1.8 逐字节等价**（退化锚，3.16.4 退化锚行；例外恰两处——dry-run stderr 的 `stitch_calls=0` 行与 stream×verify 缺陷词表的 `wrong_stitch: 0` 行，见退化锚行）。
**不做：**不重分段（episode 边界属 M14 上游；本算子只合并、不切分）；不推断接缝动作内容（接缝是已知中断，零 LLM 机械占位属 M15 消费面，3.15.4）；不判重（M3）；不打任务标签（`task_name` 是摘要卡滚动线索名——工具内部结构，进 trace 与判定证据，用户 Schema 产出物属 M5）；不跨会话、不跨批缝合（hard-split 边界不可缝，3.16.4 作用域行）；不做真并发/帧多重归属（单前台屏无真并发 [65]，2.1.2 / 8.1）。

| 模块 | 职责 | 边界 | 依赖 |
|---|---|---|---|
| M16 stitch | 把会话内碎片保守缝合为线索：单调选池 LLM 判定 × 机械先验合取 + 有界二遍复评；被并 episode 壳置 stitched、幸存信封 Record 重绑、below_min_len 短段救援翻转（②c）；机械标定 `seam_indexes` | 不重分段（M14）；不摘取动作（M15）；不判重（M3）；不跨会话/跨批；不做帧多重归属 | M1, M8, M9 |

### 3.16.2 输入 / 输出

| 方向 | 内容 |
|---|---|
| 输入 | 按会话独立执行（`session_id` 分组，批内位置序即会话序——M10 整会话装箱保证，3.10.3）。候选流 = 会话内 `status="active"` 且 `record.kind="sequence"` 的 episode 信封 + （`stitch.rescue_short = true` 时）`noise_attribution == ("segment", "below_min_len")` 的帧信封按**连续 run 重组**的救援候选（3.16.4 救援行；`reason="noise"` 的噪声帧不入候选池），按会话序合流；`[stitch]` 参数（5.2）；LLM profile（`stitch.llm`，纯文本判定——摘要卡无图）。 |
| 输出 | 命中并入的候选：幸存信封 Record 重绑（成员按序键升序拼接；`record.id` **不重算**——M7 手术先例，3.7.3）、被并 episode 信封 `status → "stitched"`（壳终态，M11 第四路由仅计数，3.11.2）；救援命中：成员帧 `dropped_noise → absorbed` 翻转 + 计 `rescued_short`（单位 = 帧）；每个幸存线索信封盖章 `PipelineItem.thread_id = record.id`（单碎片线索亦然，4.1）并挂 `seam_indexes` duck 标（无接缝 = 空元组；坐标语义见 3.16.4 接缝行）与碎片跨度表 duck 标（供 M11 组装 `_meta.stream.fragments` 与 M5 按碎片配额，6.3/3.5.2）；返回值 = 传入的同一列表对象（4.3 契约 ②c）。`on_error="fail"` 且判定修复耗尽时**仅 episode 候选信封**置 `status="failed"`（3.16.6）。 |

信封变化示例（规范验收场景 V2「单交叉」：任务 A 被任务 B 打断——会话 `sess-0012` 内 segment 已产出 3 个 episode；②c 状态写入 = 只改既有元素状态 + 幸存信封 Record 重绑，无删除/重排/替换）：

```
缝合前（3 个 episode 信封均 active，session_id="sess-0012"，会话序 = 批内位置序）:
  #12 e0（点外卖·前半，4 成员，order_span=[0,3]）   id=9c31f5a2d84e07b6
  #13 e1（回复微信消息，3 成员，order_span=[4,6]）  id=4e8a02c97d15f3b0
  #14 e2（点外卖·提交订单，2 成员，order_span=[7,8]）id=b57d1e04a92c86f3
一遍逐候选（3.16.4）:
  e0: 池空 → 判定照常发起（固定「（当前无开放线索）」行）→ verdict=new → 开线索 T0
      （task_name 自举「点外卖」）
  e1: 池=[T0]（卡 1）→ verdict=new                               → 开线索 T1
  e2: 池=[T1, T0]（最近活跃降序：T1=卡 1、T0=卡 2）→ verdict=resume, thread_ref=2；
      机械先验腿 app_overlap（App 交集）与 entity_overlap（实体「老王麻辣烫」跨碎片
      重叠）命中 → 并入 T0：幸存信封 #12 Record 重绑 members = e0 ∪ e2 成员
      （序键升序，6 成员，id 不重算）；#14 壳置 status="stitched"
二遍（T19 有界复评）: 复评候选 = T1（单碎片线索）→ 维持 new → 零并入
接缝标定（T20）: T0 拼接对（成员下标 3 与 4）的会话序间隙 [4,6] 含 T1 的 3 帧
      ⇒ seam_indexes = (3,)，interrupted_by = ["回复微信消息"]
缝合后:
  #12 active（线索 T0：2 碎片，thread_id=9c31f5a2d84e07b6，seam_indexes=(3,)）
  #13 active（线索 T1：1 碎片，thread_id=4e8a02c97d15f3b0）
  #14 stitched（壳，不写主输出、不写 rejects、仅计数，3.11.2）
M10 计量: stitched += 1；counts.threads = episodes − stitched = 3 − 1 = 2（post-emit tally 导出式，3.10.3）
```

②c 的完整契约文本见 4.3（含幸存者规范句：一遍幸存信封恒为线索创始信封、二遍方向相反——目标线索幸存、候选信封作壳；救援翻转是 ②b 双向豁免的 M16 延伸，仅限救援命中）。

### 3.16.3 数据结构与 API

```
class StitchStage(Stage):
    name = "stitch"
    def __init__(self, cfg: ResolvedConfig): ...
    async def run(self, batch, ctx) -> list[PipelineItem]: ...   # 返回传入的同一列表（②c 状态改写
                                                                 #   + 幸存信封 Record 重绑）

def build_stitch_prompt(thread_cards: Sequence[str], candidate_card: str,
                        cfg: ResolvedConfig) -> PromptBundle
                                       # 3.16.4 模板的确定性组装；线索卡由调用方按最近活跃降序
                                       #   编号呈现（1 起，缓解位置偏差 [77]），候选卡恒居末；
                                       #   池空时渲染固定「（当前无开放线索）」行
async def judge_stitch(thread_cards: Sequence[str], candidate_card: str,
                       ctx: RunContext, record_ids: tuple[str, ...] = ()) -> Mapping | None
                                       # 一候选一次判定：经 complete_validated(schema=stitch_schema())；
                                       #   stitch.votes > 1 时同一提示词 n 次采样、(verdict, thread_ref)
                                       #   完整判定严格多数决（3.16.4 votes 行）——不足严格多数返回
                                       #   None（调用方回落保守结局）；每候选定案后由 stage 发一条
                                       #   stitch.judge 事件（3.16.6）
```

**摘要卡（digest card）**：判定证据的确定性结构化载体，全部字段自 episode 成员的 `frame_digest` / `tree_diff`（4.3 共享 helper）与信封簿记可达——链序上 extract 后置，缝合运行时批内**无任何 Transition**，证据面全部为帧摘要级（resumption 判定单元「挂起目标尾动作 × 候选恢复首动作」[65] 相应降格为**线索尾帧摘要 × 候选首帧摘要对**的帧级承载）。线索卡逐行：`[线索 {i}] 任务名: {task_name}`（i = 呈现序 1 起编号；未命名渲染「（未命名）」）→ `App 集合:`（成员 App 集排序顿号连接，空集渲染「（未知）」）→ `序号跨度: [first, last]｜帧数 n｜碎片数 F` → `首帧摘要:` → `尾帧摘要:` → `接续对（线索尾帧 → 候选首帧）变更:`（该线索尾帧与候选首帧的 `tree_diff` 确定性文字化——增/删/文本变化节点数、变更比例、应用切换/标题变化，E5 判定对的变更证据行）；候选卡逐行：`[候选碎片] 类型: 分段产出|短段救援` → `App 集合:` → `序号跨度: [first, last]｜帧数 n` → `首帧摘要:` → `末帧摘要:`。卡内嵌入的每个帧摘要沿用 segment `digest_max_chars` 同名键语义截断（`stitch.digest_max_chars`，5.2）；卡的结构化字段有界由构造保证。App / activity / title 提取循环与 diff 文字化由本模块自带副本（先例 `extract` 的 `_diff_text` 副本——算子互不依赖，共享渲染仅 `frame_digest` / `tree_diff` 两枚下沉第 4 章，其余模块内自持）；**数据依赖声明**：activity 依赖采集侧 dump 将其写入 UI 树 `extra`（该字段常缺席，4.1 注），缺失时先验腿③静默失效——析取降格可接受（3.16.4 先验行）。

判定内部 Schema（`schema_engine.stitch_schema()`，3.8.1 内部 Schema 清单：不计入 `report.schema_engine.resolved_at`、不经过 L2.5）。规则同族：关键字集 ⊆ 既有内部 Schema 关键字集、无 `uniqueItems`、可空以类型联合表达、**全键 required**（OpenAI strict 兼容，3.8.1）；`thread_ref` = 池内线索卡的 **1 起呈现序编号**（Schema 不设界——Schema 看不到池大小，域校验由代码侧执行）；`reason` **恒请求**（判定量级小——每会话 ≈ episode 数次调用，零额外 token 原则的成本面不适用；votes 聚合亦需按多数簇取 task_name / reason，3.16.4 votes 行）；`confidence` 仅作 trace 观测、**不进判定门槛**（口头置信度饱和且系统性过高 [79]，去 confidence 腿见 3.16.4 先验行）：

```
def stitch_schema() -> dict:
    return {"type": "object",
            "properties": {"verdict": {"type": "string", "enum": ["resume", "new"]},
                           "thread_ref": {"type": ["integer", "null"]},
                           "task_name": {"type": "string"},
                           "reason": {"type": "string"},
                           "confidence": {"type": "string", "enum": ["high", "medium", "low"]}},
            "required": ["verdict", "thread_ref", "task_name", "reason", "confidence"],
            "additionalProperties": False}
```

代码侧后校验（M8 之后，确定性收窄——3.14.4 first-wins 建表、3.13.4 归一化同则）：`thread_ref` 非整数、越界（∉ [1, 池大小]）或与 `verdict` 组合矛盾（resume 而 null）⇒ 按保守结局收窄：episode 候选判 `new`、救援候选按未命中。

### 3.16.4 算法与流程

**按会话独立执行的两遍结构**（候选分两型，处理规则不对称）：

1. **一遍（单调贪心选池）**：候选按会话序逐个处理，每候选一次调用，提示词呈现池内全部开放线索摘要卡（**按最近活跃降序**编号 [77]）+ 候选摘要卡，输出 `thread_ref | new`。
   - **episode 候选**：池空时判定照常发起（呈现零张线索卡，verdict 恒 `new`、thread_ref 恒 null——`task_name` 由此自举，是线索命名的**唯一**来源）；判 new 或先验全不命中 → 开新线索；命中（LLM 判 resume **∧** 机械先验合取命中）→ 并入：幸存信封 Record 重绑、候选壳置 `stitched`、线索摘要卡滚动更新（尾帧摘要/跨度/任务名）。
   - **救援短段候选**：**永不开新线索**。池空 → 跳过判定（零调用），维持 `dropped_noise`；池非空 → 判定，命中 → 并入 + 成员帧 `dropped_noise → absorbed` 翻转（②c③）计 `rescued_short`；未命中（含判定失败）→ 维持 `dropped_noise` 原 reason。
   - **池满且需开新**（仅 episode 候选触发）→ 按逐出优先级挑一条封闭（**封闭仅发生于池满逐出**，无主动封闭——完成感知封闭腿已撤除：extract 后置使收尾动作模式不可判，记入 8.1 非目标 ⑥ 与 8.4 演进注记）：① 挂起跨度超 `stale_gap_steps` 者优先（0 = 该腿失效）→ ② LRU 兜底。**封闭 ≠ 终结**：被逐出线索不再出现在一遍卡集中，但保留在二遍复评目标集并照常产出（PIRA 顺序线程记忆基线 PIRF 靠反思删除控规模的批处理对应物 [64]；27% 的挂起超过 2 小时才恢复 [81]、时长分布特征使重叠活动识别 +11.36% [66]；同形制 SOTA 先例的顺序贪心亦不设终结 [87]）。
2. **二遍（有界复评）**：复评候选 = **一遍结束时的单碎片线索**，按其碎片会话序逐个处理；每候选一次调用（同一遍选池形制），池 = 该会话全部**其他**线索（最近活跃降序，超 `max_open` 张时按与候选跨度最近截取）；命中（判定 ∧ 合取）→ 并入，**方向相反**：候选信封作壳、目标线索信封幸存（幸存者规范句见 ②c，4.3；fragments 按会话序重排、episode_id / thread_id 随幸存信封）。**目标集取活视图**（复评中的并入即时更新各线索跨度与卡片）；已并入者不再作候选；无其他线索时跳过（零调用）。预算 ≤ 单碎片线索数（自然流每小时约 +10–15 次调用，全链占比 <5%）。修正顺序贪心的漏缝自增殖（无修复贪心劣于 batch 且次序依赖、n=1 局部重聚类即追平 batch [74]；merge-only 是增量法中质量最差 [74]；后见上下文显著有效 [87]）。**残差声明**：池截取排除目标、双多碎片线索误分裂不在修复面内——由真机实测门禁兜底（错缝 FPS = 0 验收线，3.16.7）；更重的簇修复机器（图度量分类器 / 全局演化图标签传播）已评估、按规模不采——会话内碎片 ≤ 数十，n=1 复评够用且零训练 [78]。
3. **接缝标定**：两遍定案后，对每条线索计算 `seam_indexes: tuple[int, ...]` 并挂 duck 标（M16 盖章，4.1；单碎片线索/无接缝 = 空元组）。**接缝判据**：拼接对构成接缝 ⟺ 两成员的会话序间隙内**含 ≥1 个归属其他线索的帧**（absorbed 于异线索碎片）；间隙仅含噪声帧 / 本线索救援帧时**不是接缝**——该对照常送 M15 摘取，与 v1.8「转移以成员相邻为准、剔噪对照常摘取」惯例（3.15.2）完全一致，同一物理情形单一处理。推论：接缝的 `interrupted_by` 恒非空（T10 占位 `detail.interrupted_by`，3.15.4）。**坐标规范句**：元素 = 接缝对**左成员**在重绑成员元组中的下标，与 `Transition.index` / `steps[].index` 同坐标，值域 `[0, len(members) − 2]`；与 `_meta.stream` 的 `order_span` 会话序键空间**无换算关系**（下游切片依据见 6.3 包络规范句）。`seams` 计数 = 满足判据的拼接处数（接缝唯一计量点，6.4）。

**缝合判定模板**（确定性拼接，逐字冻结于 CONTRACTS §10.11；保守偏置写死在模板文本，不随配置变化）：

```
system:
  你是屏幕操作流的线索缝合审核员。下面给出当前会话中 {P} 条开放线索的摘要卡
  （按最近活跃降序排列）与一张候选碎片摘要卡。
  判断该候选碎片是恢复其中某条线索（用户切回了之前挂起的同一任务），还是开启一个新任务：
  - resume: 候选与某条线索是同一任务的延续——任务实体跨碎片延续（订单号、地点、商品、
    联系人等再次出现）、返回同一页面继续操作、或 App 与操作语境明确承接；给出该线索编号。
  - new: 候选是一个新任务。
  保守偏置：仅在证据明确指向同一任务时判 resume；证据不足、模糊或仅有表面相似
  （同 App 不同任务、同类页面不同对象）时一律判 new——错缝的代价高于漏缝。
  若当前无开放线索，恒判 new。
  task_name 用一句话概括任务：resume 时给出该线索合并候选后的任务名（滚动更新），
  new 时给出新任务名。
  {stitch.context}                              ← 可选域上下文；缺省省略此行
  输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：
  {"verdict": "resume"|"new", "thread_ref": <线索编号|null>,
   "task_name": <一句话任务名>, "reason": <一句话理由>,
   "confidence": "high"|"medium"|"low"}
user（单条消息多 text Part：线索卡在前——按最近活跃降序、[线索 {i}] 以 1 起编号；
     候选卡恒居末；池空时以固定行「（当前无开放线索）」替代线索卡段）:
  [线索 {i}] {线索摘要卡（逐行结构见 3.16.3）}
  [候选碎片] {候选摘要卡（逐行结构见 3.16.3）}
```

其余关键设计的精确定义：

| 设计点 | 定义 |
|---|---|
| 保守偏置合取（`bias="conservative"`，默认） | 并入需 **LLM 判 resume ∧ 机械先验命中**。LLM 面对杂乱 GUI 流的系统性偏差方向是**过连接**（PIRA 噪声消融 precision 92→51 而 recall 反升，原文 "trigger-happy" [64]；LLM 回避「以上皆非」宁可硬连、准确与弃权此消彼长 [76]），故 LLM 单腿不足为凭。先验白名单（**析取三腿**，任一命中即过；trace 记名 `app_overlap` / `entity_overlap` / `same_page`，3.16.6）：① App 交集非空；② 候选首帧摘要 × 线索尾帧摘要**实体重叠**（可见文本实体跨碎片出现）；③ **返回同一页面**（候选首帧页面标识 == 线索某**碎片尾帧**页面标识，页面标识 = app + activity(+title)——cue-guided resumption：「返回同一页面」是任务恢复的强前兆线索 [80][81]）。③ 补齐两个失效面：同 App 恒真使①失去区分度时②③提供正交证据、跨 App 时①为空由②兜底；activity 缺席时③静默失效（3.16.3 数据依赖声明）。**去除 confidence 腿**：口头置信度门槛饱和且系统性过高 [79]，`confidence` 字段保留仅作 trace 观测。**时间衰减降格**：候选与线索尾的会话序跨度超 `stale_gap_steps`（0 = 不启用）时先验**降格为须两腿命中**（挂起时长分布特征显著助益重叠活动识别 [66]）。`bias="llm"` 档跳过先验合取（纯 LLM 判，审计/消融用）。 |
| 调用与校验 | 每候选 1 次调用（votes > 1 时 ×n），经 `complete_validated(schema=stitch_schema())`（3.8.3）；temperature 不另设（取 profile 默认——**votes 采样亦同**，同温同前缀吃 prompt 缓存，无 sc_temperature 键）。会话内候选流**严格串行**（单调选池的池状态是逐候选演进的），**会话间同样按批位置序（= 会话序）串行**——确定性事件序、零 RNG；与 M14 跨会话窗口并发不同，M16 的并发**仅存在于 votes > 1 的采样 gather 内**（profile 信号量约束）；无 rng 消耗（种子豁免面不变，2.6）。线索构成以 LLM 输出为条件（classify 分池 / segment 边界同款条件化声明，2.6 幂等行）；同输入同 seed 逐字节可复现。调用量（1 小时自然流 ≈ 2400 帧 / 40 episode / 25 thread 口径）：一遍 ≈ 40（每 episode 候选一调；救援候选仅池非空时 +ε）、二遍 ≈ 10–15；缝合并入后下游 quality/annotate/verify 以线索为单元、调用**下降**，全链行和 ≈ 1.1×帧数 + 3×threads，stitch 全口径占比 ≈ 2.0%（<3%；votes=3 时判定 ×3、占比 ≈ 5.8% <8%）。 |
| 救援短段（`rescue_short=true`，默认） | 判别载体 = 帧信封的 `noise_attribution == ("segment", "below_min_len")` duck 标（M14 剔噪时盖章，3.14.4）；`reason="noise"` 的噪声帧**不入候选池**。**连续 run 重组**：会话序上连续的 below_min_len 帧（中间无任何其他帧）重组为**一个**救援候选，与 segment 原切分不再一一对应（相邻两短段合为一候选；混合任务 run 因先验难命中而维持 dropped——保守面兜底）。机理：用户在切换前密集执行收尾动作（段落完成率基线 0.78/min → 切换前 10.9–12.8/min [81]），任务收尾帧天然易成短段、聚集在切换点旁。命中 → 并入 + 帧翻转（②c③）、`rescued_short` **累加翻转帧数**（单位 = 帧，非救援事件数）；未命中 → 维持 `dropped_noise` 原 reason 落 rejects。`below_min_len` 计数器为发生计数（帧口径），救援**不回退**（3.14.4 ③）。**相邻救援不盖接缝**：会话位置紧邻的拼接对是真实转移，照常送 M15 摘取（接缝判据行）。 |
| votes 多数决（`votes=1` 默认不启用） | votes = n（≥3 奇数）时同判定 n 次采样，**聚合键 = (verdict, thread_ref) 完整判定的严格多数**（> n/2）；任何不足严格多数的分裂（含 verdict 多数但 thread_ref 分裂）一律回落保守结局——episode 候选 = `new`、救援候选 = 未命中；`task_name` / `reason` 取多数簇内首个采样。定位：votes 是**口头置信度门槛的正规替代**（采样一致性是可靠的不确定性信号 [33]，口头置信度被证不可靠 [79]）；边界：自一致高 ≠ 对——votes **治方差（漂移）不治偏差（过连接）**，与机械先验合取不可互替 [89]。**路线选型**（业界两路线对照）：采「单模型多次」（self-consistency [33]）而非「多模型评审团」（PoLL [32]）——过连接是**跨家族共享偏差**（PIRA 消融 GPT 系与 Gemini 系同向 trigger-happy [64]），异构裁判会把共享偏差投成多数、且评审团有效独立票仅 ≈2 [86][89]；部署纪律为单端点单模型。若第二模型家族进场，`stitch.judges` 可镜像既有 `verify.judges` 模式作纯配置扩展（8.3 O8）。成本 = 判定调用 ×n（同模型同前缀吃 prompt 缓存）。 |
| 重绑与身份链 | 幸存信封 Record 重绑：`members` = 两方成员按序键升序拼接的新元组，`record.id` **不重算**（M7 手术先例，3.14.4 拼装行）；`episode_id` = 幸存信封 record.id = `thread_id`（stitch on 时语义为线索 id；off 时二者天然同值——概念性陈述，off 时 `_meta.stream.thread_id` 键不在场）；碎片原 episode_id 记录于 `_meta.stream.fragments[].source_episode`（cause ∈ `"origin"`\|`"resumed"`\|`"rescued"`，6.3）。**steps 编号**：线索的 `steps[].index` 全线索连续 0..n−2——重绑先于 M15，`Transition.index` 恒等元组下标、`len(transitions) = len(members) − 1` 不变量与 emitter 渲染三处约束的唯一解（3.15、4.2）。 |
| 作用域 | 不跨 session、不跨 batch；hard-split 边界不可缝（`session_split` 标照旧 + WARN 提示调大 batch_size，3.10.3）；segment `on_error="keep"` 的整会话降格 episode **照常入池**（合法 episode，3.14.6）。 |
| 幂等 | 已盖章 `thread_id` 的信封跳过（重入零额外调用）；M7 修复路径不重跑本 stage（wrong_stitch 缺陷 mark-only，不拆线，3.7.3）。 |
| 退化锚 | 单碎片会话 / 全 new 判定 → 产出 = v1.8 形态（thread = 单碎片、fragments 长度 1、零接缝）；`stitch.enabled = false` → **主输出、rejects、report.json 逐字节等价 v1.8**——依赖条件在场规则：counts.stitched/threads、`report.stream.stitch` 子块、batch.end 新字段、`_meta.stream` 新键（thread_id / fragments / resumed）**仅启用时在场**（6.3/6.4）。**例外恰两处（均无条件设计）**：① dry-run stderr 的 `stitch_calls=0` 行（3.10.3，v1.8 segment_calls 先例）；② stream×verify 报告的缺陷词表行 `verify.defects.wrong_stitch: 0` 与序列评审 system 词表行——缺陷词表是 3.7.2 四处同步的**单一闭集**，不随本开关条件化（真机复验：stitch off 重跑 examples/stream，counts / rejects 全等，仅该一行新增）。 |

### 3.16.5 配置项

`[stitch]` 键表（与 5.2 一致，5.2 为配置规范属主）：

| 键 | 类型 / 默认 | 说明与约束 |
|---|---|---|
| `enabled` | bool / `false` | 总开关；true ⇒ `segment.enabled = true`（M1 约束，3.1.4）。false = 不入链、主输出/rejects/report.json 与 v1.8 逐字节等价（3.16.4 退化锚行）。`[stitch]` 有 payload 而本键 false ⇒ M1 no-op warning（3.1.4）。 |
| `llm` | str / `"default"` | 判定 profile 引用；启用时计入密钥解析 / `--probe` / 存在性引用集，**不入 vision 校验集**（纯文本证据，无视觉必需，3.1.4）。 |
| `max_open` | int / `4` | 开放线索池容量。锚点：真实桌面日志的挂起窗口均值 3（S.D.≈2）+ 1 条活跃 [81]；移动域穿插深度更浅（仅 22.6% 任务存在穿插 [90]），4 为宽松上界。 |
| `bias` | `"conservative"`\|`"llm"` / `"conservative"` | 判定偏置：默认 LLM × 机械先验合取（3.16.4 保守偏置行）；`"llm"` = 纯 LLM 判（审计/消融用）。 |
| `rescue_short` | bool / `true` | below_min_len 短段按连续 run 重组先进候选池（3.16.4 救援行）；false = 短段维持 dropped_noise（v1.8 行为）。 |
| `repass` | bool / `true` | 有界二遍复评（3.16.4 ②）；false = 纯一遍贪心。 |
| `stale_gap_steps` | int / `0` | 时间衰减阈值（会话序号差；0 = 不启用）。**双职**：① 先验降格（超限须两腿命中，3.16.4 保守偏置行）；② 池满逐出优先腿（3.16.4 ①）。与 `stream.gap_steps` 语义区分——后者是会话切分规则（M2），本键是会话内线索挂起跨度。 |
| `digest_max_chars` | int / `400` | 卡内嵌入的每个帧摘要截断上限（沿用 segment 同名键语义，3.16.3）。 |
| `context` | str / `""` | 可选域上下文（何为「同一任务」的领域提示），注入模板可选行；**不是判据定义**——保守偏置内置于固定模板，零配置可用。 |
| `votes` | int / `1` | 判定稳定化采样数：1 = 不启用（单调用）；> 1 须为奇数（偶数 = CONFIG_ERROR，3.1.4），n 次采样对 (verdict, thread_ref) 严格多数决（3.16.4 votes 行）。成本 ×n。 |
| `on_error` | `"keep"`\|`"fail"` / `"keep"` | 单判定修复耗尽处置（3.16.6）；fail 仅施于 episode 候选信封。 |

`[class.<name>.stitch]` 不存在：stitch 在 classify 之前执行，类标签尚不存在（链序因果，segment 同则；5.2 按类覆盖白名单表注明缘由）。

### 3.16.6 错误处理

错误码 `stitch_invalid`（7.6，v1.9 增行）两形态——**候选两型不对称**：

| `stitch.on_error` | 行为 |
|---|---|
| `"keep"`（默认） | 该判定放弃：**episode 候选**开新线索存活（保守缺省结局；线索 `task_name=""`，摘要卡渲染为「（未命名）」），留痕**两件** = error 事件（kind = `stitch_invalid`，stitch 通道）+ 计数器 `stitch.failures`，**不写 `item.errors`**（S26 归因防污染同则，3.14.6）。与 segment S26 三件套的差异：条件在场规则（3.16.5 退化锚）封闭了 `_meta.stream` 的 v1.9 键清单（thread_id/fragments/resumed），**无 stitch 降格 `_meta` 腿**——`degraded` 键保持 segment 专属；**救援候选**维持 `dropped_noise` 原 reason + 同款两件留痕。 |
| `"fail"` | **仅 episode 候选信封**置 `status="failed"`、`StageError(stage="stitch", kind="stitch_invalid")` 入 `item.errors` ⇒ rejects——成员帧维持 `absorbed`（M7 fail 先例；②c 授权面**不含** absorbed / dropped_noise → failed 的帧迁移）；救援候选**不适用 fail 路径**，判定失败一律按未命中处理（维持 dropped_noise）。 |

事件（7.2，v1.9 增行；通道 `"stitch"` = stage 名——`_TRACE_CHANNELS` 10→11，事件名前缀即通道、error 事件按 stage 自动归属，零路由代码）：

| 事件名 | 通道 / stderr 级别 | 触发点 | payload 字段 |
|---|---|---|---|
| `stitch.judge` | stitch / —（trace-only，无 stderr 镜像） | 每候选判定定案后（votes 聚合之后；一遍与二遍均发）；`record_ids` = [候选碎片首成员 id]。 | `session_id`、`candidate`（"episode"\|"rescue"）、`repass`（bool，false = 一遍 / true = 二遍）、`verdict`（votes 分裂回落时记保守结局 "new"）、`thread_ref`、`confidence`（仅观测，3.16.3）、`priors`（机械先验命中腿列表，⊆ {app_overlap, entity_overlap, same_page}）、`merged`（bool，是否实际并入——LLM 判 resume 而先验未过时 verdict 与 merged 可分离）；条件字段：`votes_split`（= true，仅 votes 严格多数不成立回落时携带）、`task_name`††、`reason`††（votes 分裂时二者不携带）、`target_thread_id`（仅 merged 时携带 = 目标线索 id）。 |
| `stitch.thread` | stitch / —（trace-only） | 会话缝合定案后每线索一条；`record_ids` = [幸存信封 record.id]。 | `session_id`、`thread_id`、`task_name`††、`fragments`[]{`order_span`, `member_count`, `cause`, `source_episode`}（碎片跨度表）、`seam_indexes`。 |

†† `task_name` / `reason` 为 LLM 自由文本——入 `_FREE_TEXT_KEYS` 脱敏集（7.4，v1.9 增 `task_name`）：`none` 档剥除、`refs` 档起携带；其余 payload 字段均为结构字段，全档保留。

计数与归属：`report.stream.stitch = {stitched, rescued_short, seams, judgments, repass_judgments, failures}`（M16 属主，6.4——judgments / repass_judgments 计一遍/二遍**逻辑判定数**：每候选一判、失败不计；votes > 1 放大调用数不放大判定数）；`counts.stitched`（壳终态 tally）与 `counts.threads`（= episodes − stitched 导出式）由 M10 计量（counts.* 属主不变，3.10.3）——threads 仅落 `counts.threads` 单点，避免双落点；`batch.end` payload 增 `stitched` / `threads`（仅启用时携带，7.2）。**壳的范围规范句**：`stitched` 仅计被并 **episode 信封**壳；救援短段无信封形态（由帧重组），命中只计 `rescued_short` 帧翻转、**不产生壳**。stderr 进度条与终版摘要为固定键集，stitched 不显示（fanout / episodes 先例，报表与 batch.end 可见——有意为之；v1.10 U18：本约束收窄为 **plain 面专属**，rich 面板状态账展示 stitched/threads、与 report.counts 口径对齐，7.7）。`--strict` 交互（3.11.2 / 2.4 补注）：stitched 壳与 rescued 帧均不构成 rejects——同输入开启 stitch 后（短段被救援而不再落 rejects）strict 结果可能 1→0，**属预期**。

### 3.16.7 背书

问题形态与偏差方向的形式化出自 PIRA-Bench [64]：其把屏幕流定义为「多线索穿插 + 噪声」（T = ∪任务子轨迹 ∪ 噪声，任务子轨迹为**非连续帧子集**），并以噪声消融证实 LLM 的系统性偏差方向是**过连接**（PIRF 框架下 GPT 系 precision 92.23→50.52 而 recall 反升、Gemini 系同向，原文 "trigger-happy"）——这是保守偏置（LLM ∧ 机械先验合取，3.16.4）的直接依据，与指代消解域「回避以上皆非宁可硬连」及弃权研究「默认过度承诺」的同向量化 [76] 三方互证；其线索级综合指标（S_final = F1 × FPS_norm，错缝以乘法惩罚合成）与负样本协议（纯噪声会话必须 0 缝合）为真机实测门禁提供度量（注：0 被引新基准，自报数字权重打折，故充分性只能实测——错缝 FPS = 0 为验收线）。单调选池形制有**同任务同形制的直接 SOTA 先例** [87]：簇摘要呈现 + LLM 归簇或判 new + 顺序贪心（GreedyDisentangle）在对话解缠标准基准全指标超 per-pair 与非 LLM 方法，其 subsequent-context 显著有效结论亦是二遍复评的第三依据（风险注记：小参数量开源模型上该形制性能骤降的报告在案，实测由门禁兜底）；对话解缠谱系的贪心链接标准解码、并发线程 ≤3 占 46.4% 与 VI / 1-1 overlap / link-F1 指标三件套见 [75]。有界二遍复评的机制依据来自增量实体消解：无修复贪心劣于 batch 且次序依赖、**n=1 局部重聚类即追平 batch**，merge-only 是增量法中质量最差 [74]——会话内碎片 ≤ 数十的规模下，更重的簇修复机器（图度量分类器 + 主动学习 / 全局演化图概率标签传播）与全局 LLM 聚类（自身需防幻觉护栏、记录集构成显著影响质量）已评估、按规模不采 [78]。resumption 判定单元「挂起目标尾证据 × 候选恢复首证据」出自 interleaving/concurrent 活动建模的经典工作 [65]（链序上动作后置，本模块降格为首末帧摘要对承载，3.16.3）；时间衰减先验（挂起时长分布特征使重叠活动识别 +11.36%）出自 interleaved ADL 数据集系列 [66]；「返回同一页面」先验腿的机理是 cue-guided resumption [80]。`max_open = 4` 的实证锚点是真实桌面日志的**挂起窗口均值 3（S.D.≈2）+ 1 条活跃** [81]——同文献「27% 的挂起超过 2 小时才恢复」支撑「封闭 ≠ 终结」、「切换前收尾动作密集（0.78 → 10.9–12.8/min）」支撑短段救援的机理；移动域佐证：人工标注 1414 个真实手机任务仅 22.6% 存在穿插，桌面锚在移动域为宽松上界 [90]；working spheres 多任务粒度与手机中断/回访的人因基线见 [82]。摘要卡证据面的正面证据：window title 是任务识别最强单特征（85.57%）且多窗证据聚合优于单窗 [83]；精选紧凑上下文准确率**反超**全量原始历史（+10.4 pt 且 token 少 8 倍）、结构化摘要以 ~5% token 胜过其它记忆系统 [88]——反向风险的正式命名 **summarization drift**（每次压缩静默丢弃低频细节）同出 [88]，trace 全量留判定证据供审计即为此设；embedding 召回 + LLM 精判的两级任务组匹配工业近例见 [84]。votes 机制（默认关）的出处是 self-consistency [33]（单模型 n 采样多数决；一致率是可靠的不确定性信号），其边界由 2026 年两则审计钉死 [89]：前沿模型高自一致处**过度自信**（高自一致条目近半仍错——votes 治方差不治偏差，与机械先验合取不可互替）、9 裁判 7 家族评审团有效独立票仅 ≈2——加上跨模型共识研究「self-consistency 更好校准、跨模型信号更准确，但自一致性修不了系统性偏差（错误相关性 within-model 0.68 > cross-family 0.47）」[86]，共同构成拒绝多模型评审团路线（PoLL [32]）的论证：过连接是跨家族**共享**偏差 [64]，评审团会把它投成多数（选型记录见 8.3 O8）；多候选选择式判定的位置/上下文结构敏感性 [77] 决定池卡按最近活跃降序的固定呈现序与候选位置扰动测试要求。层级与产出形态的先例：thread ⊃ fragment ⊃ step 对齐 Ego4D Goal-Step 的 goal⊃step⊃substep 三级 + `is_continued` 续接标志（平面分段 + 线索身份的直接先例）[69] 与 GUI 域层级标配 AndroidControl [45]；跨 App 单目标轨迹形态见 GUI-Odyssey [46]；「自然流 → 事后反推任务标注」范式与可命名性剪枝判据出自 OS-Genesis [41] 与 NNetNav [70]；视频域层级时间标注范式（FineGym / Breakfast）[71] 为三级结构的域外印证；帧多标签先例（MultiTHUMOS / Charades）[72] 经评估后**否决采纳**（帧单一归属是手术/归因/守恒的公共地基），引用记录被拒方案；嵌套结构与 during/overlaps 区间关系的形式语义底座（HHMM / Allen 区间代数）[73] 界定「线索包含碎片、碎片穿插」的语义而不引入区间树。问题域现状：UI 日志 interleaved 解缠是 Robotic Process Mining 的公开难题——全局法（trace alignment / 频繁模式）依赖「例程重复」前提，对一次性自然任务不可迁移 [67]，无分段 UI 日志例程识别的「重复例程」前提亦经 2025 年工作复核 [50][85]；学术解缠系列之外，三家工业任务挖掘产品**均无穿插解缠**（采集纪律回避 / 时间邻近分组 / 按任务录制），通信类 App「天然噪声/穿插高发」名单同出其产品文档 [68]——产品空白区，缝合质量无域内基线，护栏 = 保守合取 + 二遍复评 + 负样本协议 + 真机门禁四层（8.1/8.4 注记）。

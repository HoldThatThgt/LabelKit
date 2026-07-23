## 3.14 M14 segment——时序流语义分段

### 3.14.1 职责与边界

**做：**（v1.8 新增算子）把批内候选会话精化为 episode：对 `status="active"` 的帧信封按 `session_id` 重组会话（M10 装箱时盖章，3.10.3），可选 LLM 滑窗边界裁决与逐帧噪声标记（3.14.4）；成员信封置 `absorbed`、噪声帧置 `dropped_noise`，按序键拼装序列 Record 并向批尾追加 episode 信封（4.3 契约 ②b）。链序位于链首（`_CHAIN_ORDER` 首位、dedup 之前，3.10.3）——episode 形成先于一切逐条算子：帧级判重语义在连续 UI 帧上失效，重复判定改为 episode 级（3.3）；类标签、质量分、标注全部以 episode 为单位。`segment.enabled = false`（默认）时本算子不入链，工具行为与 v1.7 一致（输出仅多 `_meta.stream: null` 恒在键，6.3）。
**不做：**不排序、不会话化（`[stream]` 规则层属 M2，3.2；M14 收到的是已按整会话装箱的批）；不判重（M3）；不推断动作（M15）；不打任务标签（M5）；不改链结构（②b 改变的只是批内信封基数与状态，与 ②a 同为受控例外）。

| 模块 | 职责 | 边界 | 依赖 |
|---|---|---|---|
| M14 segment | 把批内候选会话精化为 episode：可选 LLM 滑窗边界裁决与逐帧噪声标记；成员信封置 absorbed、噪声帧置 dropped_noise，按序键拼装序列 Record 并尾部追加 episode 信封（②b） | 不判重（M3）；不推断动作（M15）；不打任务标签（M5）；不改链结构 | M1, M8, M9 |

### 3.14.2 输入 / 输出

| 方向 | 内容 |
|---|---|
| 输入 | 批内 `status="active"` 且 `record.kind="single"` 的帧信封（M10 已按整会话装箱并盖章 `session_id`——同会话帧在批内连续、批内位置序即会话序，3.10.3；本算子追加的序列信封 `kind="sequence"`，不落入处理面——天然幂等）；`[segment]` 参数（5.2）；`strategy ∈ {llm, hybrid}` 时 LLM profile（`segment.llm`）。 |
| 输出 | 成员信封 `status → "absorbed"`；噪声帧 `status → "dropped_noise"`（携带 `noise` / `below_min_len` 二值之一的 duck-typed reason 标记，3.14.4 成段流程；M11 rejects 归因据此分流，3.11.2）；每段一个序列信封原地追加到传入批列表尾部（`status="active"`、`record.kind="sequence"`、盖章 `session_id`）；返回值 = 传入的同一列表对象（4.3 契约 ②b）。`on_error="fail"` 且窗口修复耗尽时该会话成员全部 `status="failed"`、StageError 入 `item.errors`（3.14.6）。 |

信封变化示例（5 帧点外卖会话 `sess-0003`：f0 首页 → f1 搜索结果页 → f2 弹窗噪声 → f3 餐厅页 → f4 下单确认页；②b 状态写入 = 只改既有元素状态 + 尾部追加，无删除/重排/替换；3.15.2 的摘取示例沿用本 episode）：

```
段前批（5 信封，均 active，session_id="sess-0003"，pair_index 3..7）:
  #0 f0(b3a1c4e29d70f512)  #1 f1(4c8e02d9a1b6f374)  #2 f2(9a7d33c8b1e4f062)
  #3 f3(e07b94a3c25d18f6)  #4 f4(61f8d0b4a9c3e725)
逐帧 rel 定案（3.14.4）: [continues, continues, interruption, advances, continues]
段后批（6 信封）:
  #0 absorbed   #1 absorbed   #2 dropped_noise(noise)   #3 absorbed   #4 absorbed
  #5 episode 信封（尾部追加）: status="active", session_id="sess-0003", transitions=None,
     record = Record(kind="sequence", id="7655568d2c485c43",     ← sha256("\n".join(成员 id))[:16]
                     members=(f0, f1, f3, f4),                   ← 序键升序
                     modality="ui", text/raw/ui_tree/image=None,
                     ref=RecordRef(source_file="a/uitree_3.jsonl", line_no=None,
                                   pair_index=3,                 ← 继承首成员
                                   generated_from=(), generator=None))
M10 计量: counts.episodes += 1（segment 阶段 len 差，fanout 同构）；
          absorbed += 4、dropped_noise += 1（状态 tally，3.10.3）
```

②b 的完整契约文本见 4.3（含 M7 修复路径豁免：verify 缺陷修复可在本批内将成员状态在 `absorbed` 与 `dropped_noise` 间双向改写，禁止翻回 `active`；每个成员信封至多被一个序列信封吸收，3.7）。

### 3.14.3 数据结构与 API

```
class SegmentStage(Stage):
    name = "segment"
    def __init__(self, cfg: ResolvedConfig): ...
    async def run(self, batch, ctx) -> list[PipelineItem]: ...   # 返回传入的同一列表（②b 尾部追加）

def build_segment_prompt(frames: Sequence[Record], diffs: Sequence[Mapping | None],
                         digests: Sequence[str],       # v1.11 增形参（V9，CONTRACTS 同步修订）：会话级
                                                       # 预计算的逐帧摘要（3.14.4 装填伪代码），不再窗内现算
                         cfg: ResolvedConfig, with_reason: bool) -> PromptBundle
                                       # 3.14.4 模板的确定性组装；帧摘要与相邻帧 diff 由代码侧预组装；
                                       # 附图判据 = seg.vision_resolved（v1.11，V1——原 use_vision 键已移除）
async def judge_window(frames: Sequence[Record], ctx: RunContext) -> list[str]
                                       # 一窗一调用：经 complete_validated(schema=
                                       # segment_window_schema(len(frames), with_reason))；校验后
                                       # 按 index first-wins 建表、缺席帧缺省 "continues"，返回与
                                       # frames 对齐的逐帧 relation；每窗发一条 segment.boundary
                                       # 事件（3.14.6）。M7 成员回收复裁直调本函数（3.7）
```

帧摘要与相邻帧树 diff 由共享 helper 提供——`frame_digest(record, max_chars)` 与 `tree_diff(a, b, quantize_px)`，规范定义见第 4 章（落位 `types.py`、毗邻 `UITree.serialize()`；M13/M4 序列分支复用同一实现——算子层不得互相依赖，共享渲染下沉共享层）。摘要 = best-effort 确定性提取：app（`extra` 键 package/package_name/pkg 首个非空）· activity（`extra` 键 activity/activity_name/window_title，可缺省）· title（DFS 首个可见非空 text）· salient（可见 text/content_desc 按序去重，交互角色加前缀），整体截断至 `max_chars`；可见文本节点数为 0 或摘要长度 < 8 ⇒ 判贫瘠（调用方计数，3.14.4 贫瘠护栏）。diff = 结构键 `(role, bounds//quantize, depth)` 多重集匹配，输出 added/removed/text_changed/change_ratio/app_changed/title_changed——O(n1+n2) 纯统计，只提供变化幅度与类型证据，不做语义归因（不越 M15 界）；node_id 不作跨帧匹配键（同 id 可承载不同控件）。

窗口内部 Schema（`schema_engine.segment_window_schema`，3.8.1 内部 Schema 清单：不计入 `report.schema_engine.resolved_at`、不经过 L2.5）。关键字集 ⊆ 既有内部 Schema 关键字集、**不写 `uniqueItems`**（OpenAI strict 模式硬拒，3.13.3 同教训）——index 对齐由代码侧后校验保证（3.14.4 缝合行）；`minItems = maxItems = N` 钉死数组长度（judgment_schema 同款）：

```
def segment_window_schema(frame_count: int, with_reason: bool) -> dict:
    relations = ["continues", "advances", "returns_to_entry", "context_switch", "interruption"]
    item_props = {"index": {"type": "integer", "minimum": 0, "maximum": frame_count - 1},
                  "relation": {"type": "string", "enum": relations}}
    required = ["index", "relation"]
    if with_reason:
        item_props["reason"] = {"type": "string"}
        required = ["index", "relation", "reason"]
    return {"type": "object",
            "properties": {"frames": {"type": "array",
                "items": {"type": "object", "properties": item_props,
                          "required": required, "additionalProperties": False},
                "minItems": frame_count, "maxItems": frame_count}},
            "required": ["frames"], "additionalProperties": False}
```

`with_reason` 条件 = `trace.enabled = true` 且 `trace.channels` 含 `"segment"`（零额外 token 原则，3.13.4 调用与校验行同款）。

### 3.14.4 算法与流程

**策略三态**（`segment.strategy`）：

| 值 | 行为 |
|---|---|
| `"rules"` | 候选会话原样成 episode，零 LLM 调用；noise_filter / min_len 不生效（M1 对 `rules` ∧ 显式 `noise_filter=true` 发 no-op warning，3.1.4）。 |
| `"llm"` / `"hybrid"`（默认 hybrid） | 滑窗裁决（下述）。两值在 M14 内行为一致——规则层会话化恒在（M2），M14 收到的必是规则粗切后的候选会话，hybrid 命名即声明「规则粗切 + LLM 精化」的组合形态。窗上限 `segment.window`（默认 20，M1 校验 ≥ 2）；v1.11 修订（V9，3.9.5）：所引 profile 声明 `context_window` 后按预算**贪心装填**切窗（实际每窗帧数 ≤ window，溢出即封窗——下述装填伪代码），未声明预算时逐字节退化为固定窗、步长 = window−1；两形态均保持**重叠 1 帧、接缝帧整帧判决归后窗**（3.14.4 缝合）；`len(session) == 1` 走 rules 退化（零 LLM）。 |

**三步演绎判据模板**（确定性拼接，逐字冻结于 CONTRACTS §10.9；判据内置且任务无关——用户零 prompt 可用，`segment.context` 只是可选域上下文、不是边界定义）：

```
system:
  你是屏幕操作流的分段审核员。下面给出同一会话中按时间顺序排列的 {N} 帧状态摘要
  （含相邻帧的确定性变更提示）。按三步作业：
  一、双向上下文概括：通读全窗，把握每帧之前若干帧正在进行的活动与之后若干帧的走向，再判断该帧。
  二、逐帧关系分类：对每一帧，判断它相对进行中活动的功能角色，只能从以下封闭词表中取恰一值：
  - continues: 同一流程的推进。
  - advances: 屏幕或 App 变了，但可见的任务实体延续（验证码、订单号、餐厅名等跨屏出现）——
    跨 App 的同一任务属此值，不是边界。
  - returns_to_entry: 回到入口/搜索/桌面后开启新流程（同 App 背靠背任务的断点）。
  - context_switch: 交互对象与环境不连续且无实体延续——相关但无实体延续的新流程也取此值。
  - interruption: 与前后活动均无关的短暂插入（通知、弹窗、误触）。
  三、只输出逐帧关系，不判断边界（边界由既定规则从关系推导）。
  锚定约定：分段粒度取「完整任务」层级（整段录屏之下一层）；只看前台 App/前台窗口，
  忽略状态栏、后台通知等背景变化。
  {segment.context}                              ← 可选域上下文；缺省省略此行
  输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：
  {"frames": [{"index": <窗内帧序号>, "relation": <词表值>[, "reason": <一句话理由>]}, ...]}（恰 {N} 项）
user（窗内逐帧，一帧一段）:
  [帧 {i}] {frame_digest(frame_i, segment.digest_max_chars)}
  [帧 {i} 变更] {tree_diff(frame_{i-1}, frame_i) 的文字化摘要}      ← i ≥ 1；窗首帧无此行
  （vision_resolved = true 时（v1.11，V1 解析产物——3.1.4；原 segment.use_vision 键已移除）：
   每帧摘要 text Part 前附该帧 kind="image" 的 Part，3.9.2）
```

两个锚定写死在模板文本（不随配置变化）：粒度锚定 =「完整任务」层级（GEBD "1 level deeper" 原则）；注意力锚定 = 只看前台 App/窗口（GEBD dominant subject 原则）。关系词表固定且域无关；`advances` 与 `context_switch` 的分界钉死为**实体延续**——相关但无实体延续的新流程 = context_switch（边界）。LLM 不直接回答边界问题，只做封闭集分类（M8 enum 硬校验，与 classify 同款防线）；`boundary` / `noise` 是代码侧查表结果（**演绎映射**）：

| relation | 演绎结果（代码侧查表，LLM 不可见） |
|---|---|
| `continues` / `advances` | 非边界（帧归入当前段） |
| `returns_to_entry` / `context_switch` | 边界——**该帧是新段第一帧** |
| `interruption` | noise（`noise_filter=true` 时剔除；false 时按非边界成员保留在所属段内） |

**会话首帧恒为段首**：rel[0] 的边界值不参与判决（无论 LLM 输出何值，帧 0 都开启首段）；noise[0] 照常生效。

**调用与校验**：每窗 1 次调用，经 `complete_validated(schema=segment_window_schema(...))`（3.8.3）；temperature 恒 0；批内全部窗口（跨会话）并入一个 `asyncio.gather`（profile 信号量约束，3.4.3 骨架同款）——缝合是判决收齐后的同步 pass，结果按窗口位置定位、与完成顺序无关；无 rng 消耗（种子豁免面不变，2.6）。episode 构成以 LLM 输出为条件（classify 分池同款条件化声明，2.6 幂等行）；同输入同 seed 逐字节可复现。

**滑窗缝合**（确定性；全窗判决收齐后执行。v1.11（V9）：切窗从定长改为**预算贪心装填**——预算未声明时逐字节退化为 v1.10 定长窗；窗口边界由此成为 (输入, 配置) 的确定函数——digest/diff 是记录内容纯函数、本算子零 rng，同输入同配置逐字节可复现不破）：

```
def refine(session: list[Record]) -> list[str]:        # 返回逐帧 relation（长度 = len(session)）
    if strategy == "rules" or len(session) == 1:       # rules / 孤帧退化：原样成段，零 LLM
        return ["continues"] * len(session)
    digests = [frame_digest(f, digest_max_chars)       # v1.11（V9）：会话级逐帧摘要预计算——前移到
               for f in session]                       #   切窗之前、每会话恰一次（v1.8–v1.10 在切窗后
                                                       #   逐窗现算：接缝帧双算——前移是净改善；贫瘠
                                                       #   护栏计算路径独立保持不动）；build_segment_prompt
                                                       #   增 digests 形参消费本值（3.14.3）
    rel = [None] * len(session)
    start = 0
    while start < len(session):
        end = pack_window(digests, start)              # v1.11（V9）：窗 = [start, end) 贪心装填——自 start
                                                       #   逐帧累加 c_i = est_text(digests[i]) + DIFF_MAX_TOKENS
                                                       #   + (每图成本 if vision_resolved else 0)
                                                       #   （diff 在切窗后才算、输出结构有界故取最坏常数；
                                                       #   每图成本读校准器快照，3.9.5），装填条件
                                                       #   est_static_system + Σ c_i ≤ input_budget
                                                       #   ∧ 窗内帧数 ≤ window，溢出即封窗（M1 静态护栏
                                                       #   w_min ≥ floor 在**先验计价**下保证任意帧装得进
                                                       #   floor 帧窗，3.1.4；校准值超先验（3.9.5 无钳制，
                                                       #   合法）或退化个案：装填器**强制 2 帧封窗**（V10
                                                       #   语义最小单元），真实 est 超预算由 M9 终检以
                                                       #   记录级 context_overflow 走既有单窗失败路径——
                                                       #   永不 run 级、永不死循环，v1.11 审计修订）；
                                                       #   预算未声明 ⇒ end = min(start + window,
                                                       #   len(session))，逐字节退化为定长窗
        verdicts = judge_window(session[start:end])    # 一窗一调用（并发下收齐后按位缝合）；
                                                       #   返回值已在 judge_window 内完成后校验：
                                                       #   按 index first-wins 建表（同窗重复 index
                                                       #   取首个出现者）、缺席帧缺省 "continues"
                                                       #   （保守中性，quality「缺席准则→tie」同款）
        for i in range(end - start):
            rel[start + i] = verdicts[i]               # 无条件覆写 ⇒ 接缝帧（前窗末帧 = 后窗首帧）
                                                       #   整帧判决归后窗
        if end == len(session): break
        start = end - 1                                # 后窗自前窗末帧起（重叠 1 帧；定长退化态下
                                                       #   等价于 v1.10 的步长 = window−1）
    return rel
```

**溢出降级重试（v1.11，V20/V24）**：某窗调用被识别为 provider 上下文溢出（统一溢出信号，3.9.5：预算开启下 400 错误体嗅探命中，或双协议 `model_context_window_exceeded` 200 形态——`ContextOverflowError(phase="reactive")`）⇒ **该窗对半分裂为两个子窗改切重试**（维持重叠 1 帧与接缝归后窗语义，帧一个不丢——多花调用；乘性减、有界：每调用至多 2 次降级）；到最小单元仍溢出 ⇒ 按 V10 最小单元语义记录级 `context_overflow` reject（7.6）；**reactive-400 降级耗尽的终局由本算子经 `ctx.metrics.record_provider_result(fatal=True)` 补喂熔断恰一次**（A7；reactive-200 终局不补喂——3.9.5 熔断交互矩阵），降级重试独立计数入 `report.budget.degrade_retries`（6.4）。未识别的 400 走现行 fatal 老路（零回归）。

**成段流程**（rel 定案后，逐会话确定性执行）：

1. **剔噪**：`noise_filter = true` 时，`rel[i] == "interruption"` 的帧置 `dropped_noise`、reason 标记 `"noise"`（含帧 0）；false 时跳过本步。
2. **切段**：剩余帧按演绎映射切段——`rel[i] ∈ {returns_to_entry, context_switch}` 的帧开启新段（会话首帧恒为段首）。
3. **min_len 检查**：仅作用于本步（LLM 边界精化）切出的段（S11）——段长 < `segment.min_len` ⇒ 该段全部帧置 `dropped_noise`、reason 标记 `"below_min_len"`（**≠ "noise"**：未经噪声判据裁决，不得污染噪声审计口径；计数独立，`report.stream.below_min_len`，6.4）。规则层孤帧/短会话（`strategy="rules"` 与 `len(session)==1` 退化）不经 min_len，原样成 episode。v1.9 注：帧信封上的 duck 标 `noise_attribution == ("segment", "below_min_len")` 即 M16 短段救援的**判别载体**（`reason="noise"` 帧不入救援候选池）——M16 救援命中时按 ②c③ 将此类帧 `dropped_noise → absorbed` 翻转（4.3；本模块**零改动**——重组与翻转全在 M16 侧，3.16.4 救援行）；`below_min_len` 计数器为**发生计数**（帧口径），救援**不回退**（救援量另计 `rescued_short`，6.4）。
4. **拼装**：每段成员按序键升序 → 成员信封置 `absorbed` → 构造序列 Record：`kind="sequence"`；`id = sha256("\n".join(成员 id))[:16]`（形成时定死，后续成员手术不重算，3.7）；`text/raw/ui_tree/image = None`；`modality` = 成员模态；`members` = 成员 Record 元组（序键升序）；`ref` 继承首成员（source_file、line_no（文本）/pair_index（UI），`generated_from=()`、`generator=None`，4.1）→ 尾部追加 episode 信封并盖章 `session_id`（②b）。完整成员溯源由 `_meta.stream.member_sources` 承担（6.3）。

**失败语义**：单窗 M8 修复耗尽按 `segment.on_error` 处置（3.14.6）——`"keep"`（默认）：该会话放弃全部窗口判决，整体原样成一个 episode（零剔噪、零切分），留痕三件套 = `_meta.stream.degraded = {kind: "segmentation_invalid", windows_failed: k}` + error 事件 + 计数器 `segment.failures`，**不写 `item.errors`**（记录存活；rejects 归因取 `item.errors[0]`，写入会污染后续阶段失败的归因，3.13.4 失败与兜底行同则）；`"fail"`：会话成员全部 failed → rejects。

**摘要贫瘠护栏**：某帧 `frame_digest` 判贫瘠（可见文本节点为 0 或摘要长度 < 8——无文本 UI 树在真实采集中常见：ghost nodes、画布类屏幕 [63]）⇒ 计 `digest_poor_frames`（`report.stream`，6.4）+ 每运行至多一次 WARN；帧摘要保真度是纯文本裁决的第一瓶颈（摘要没抓到的实体 LLM 看不见），手册指引**为 `segment.llm` 配置 `supports_vision = true` 的 profile** 补偿（v1.11 改写（V4）：原「开 `segment.use_vision`」指引随该键移除失效——附图由 profile 能力自动推导，选 profile 即选能力）。

### 3.14.5 配置项

`[segment]` 键表（与 5.2 一致，5.2 为配置规范属主；`[stream]` 排序与会话化键属 M2 消费，3.2、5.2）：

| 键 | 类型 / 默认 | 说明与约束 |
|---|---|---|
| `enabled` | bool / `false` | stream 模式总开关。false = 不入链、行为与 v1.7 一致（输出仅多 `_meta.stream: null`）。启用要求 `run.mode="process"` ∧ `generate.enabled=false`（含 generate_only 传递闭合）∧ `annotate.enabled=true`（2.3.1）。`[stream]`/`[segment]`/`[extract]` 在场而本键为 false ⇒ M1 no-op warning（3.1.4）。 |
| `strategy` | `"rules"`\|`"llm"`\|`"hybrid"` / `"hybrid"` | 三态语义见 3.14.4 策略表。 |
| `llm` | str / `"default"` | LLM profile 引用；仅 `strategy ∈ {llm, hybrid}` 时计入密钥解析 / probe / 存在性引用集（S30，3.1.4）。v1.11（V1/V3）：**恒不入 vision 校验集**——窗口是否附图由本 profile 的 `supports_vision` 能力自动决定（`vision_resolved` 行）；选 profile 即选能力（省钱形态 = 指向纯文本 profile）。 |
| `window` | int / `20` | 单窗帧数**上限**（v1.11 语义修订，V9）：所引 profile 声明 `context_window` 后按预算贪心装填（实际每窗帧数 ≤ window、溢出即封窗，3.14.4 装填伪代码），未声明时为固定窗大小（v1.10 行为逐字节一致）；M1 校验 ≥ 2；两形态均重叠 1 帧、接缝帧整帧判决归后窗。会话不长且预算装得下时可调大以趋近整段单调用形态（3.14.7）。 |
| `digest_max_chars` | int / `400` | 单帧摘要（`frame_digest`）截断上限。 |
| `noise_filter` | bool / `true` | 仅 llm/hybrid 生效；`rules` ∧ 显式 true ⇒ M1 no-op warning。 |
| `min_len` | int / `2` | 段长下限；仅作用于 LLM 边界精化切出的段（3.14.4 成段流程 ③，below_min_len ≠ noise）。 |
| `vision_resolved` | bool（parse product） | v1.11（V1）：**非用户键**——M1 于 load() 收尾冻结的解析产物（3.1.4）：`vision_resolved = (modality=="ui") ∧ enabled ∧ strategy∈{llm,hybrid} ∧ llm_profiles[segment.llm].supports_vision`；true 时窗内逐帧附图（3.14.4 模板）。原用户键 ~~`use_vision`~~ 已于 v1.11 移除（V2：显式出现 → CONFIG_ERROR 定向迁移指引，5.2/3.1.4）。 |
| `context` | str / `""` | 可选域上下文（如「这是手机屏幕操作流」），注入模板可选行；**不是边界定义**——判据内置于固定模板，零配置可用。 |
| `on_error` | `"keep"`\|`"fail"` / `"keep"` | 窗口修复耗尽处置（3.14.6）。 |

`[class.<name>.segment]` 不存在：segment 在 classify 之前执行，类标签尚不存在（链序因果；5.2 按类覆盖白名单表注明缘由）。

### 3.14.6 错误处理

错误码 `segmentation_invalid`（7.6，v1.8 增行）两形态：

| `segment.on_error` | 行为 |
|---|---|
| `"keep"`（默认） | 会话整体降级为一个 episode（原样、零剔噪）并存活；留痕三件套 = `_meta.stream.degraded = {kind: "segmentation_invalid", windows_failed: k}`（6.3）+ error 事件（kind = `segmentation_invalid`，segment 通道）+ 计数器 `segment.failures`。**不写 `item.errors`**（S26；归因保护同 3.13.4）。 |
| `"fail"` | 该会话成员信封全部 `status="failed"`、`StageError(stage="segment", kind="segmentation_invalid")` 入各 `item.errors` ⇒ rejects。 |

事件（7.2，v1.8 增行；通道 `"segment"` = stage 名——`_TRACE_CHANNELS` 8→10 之一，事件名前缀即通道、error 事件按 stage 自动归属，零路由代码）：

| 事件名 | 通道 / stderr 级别 | 触发点 | payload 字段 |
|---|---|---|---|
| `segment.session` | segment / —（trace-only，无 stderr 镜像） | M2 会话装配器闭合会话时（属主 M2，3.2；事件名冠 segment 前缀归本通道）；`record_ids = ()`。 | `session_id`、`first`、`last`（首末序键）、`len`、`cause`（∈ `gap`\|`key`\|`max_len`\|`max_span`\|`eof`\|`limit`）。 |
| `segment.boundary` | segment / —（trace-only） | M14 每窗裁决经 M8 校验通过后；`record_ids = ()`。 | `session_id`、`window: [s, e]`、`member_ids`、`relations: [{index, relation}]`、`model`、`reason`†。 |

† `reason` 请求条件 = `with_reason`（3.14.3）；作为 LLM 自由文本受 7.4 分级——`none` 档剥除、`refs` 起携带（键已在 `_FREE_TEXT_KEYS` 集合）；其余 payload 字段均为结构字段，全档保留。

计数与归属：`segment.failures`（M14 属主）；`counts.episodes` = segment 阶段 len 差（M10 计量，fanout 同构）；`absorbed` / `dropped_noise` 由状态 tally 归集（M10，3.10.3）；`below_min_len`、`digest_poor_frames` 入 `report.stream`（M14 属主，6.4）；v1.11 增 `report.stream.windows`（V13④，M14 属主，6.4）= **实际窗数**（含 V20 分裂产生的子窗）——供用户对账 estimate_run 的 w_min 上界估算（3.10.3）；`sessions` 数据源 = IngestReport（M2 属主）。rejects 归因（3.11.2）：`dropped_noise` 行按 duck-typed 标记分流为 `stage="segment", reason="noise"` 或 `reason="below_min_len"`。`--strict` 交互：stream 工程的噪声帧属预期产物，`--strict` 会因 rejects 非空退出 1（手册明示）；v1.9 注：`stitch.rescue_short` 命中的 below_min_len 帧翻回 absorbed、不再落 rejects——同输入开启 stitch 后 strict 结果可能由 1 变 0，属预期（3.16.6、3.11.2）。

### 3.14.7 背书

边界判据内置、无需任务词表的依据是 GEBD [48]：其把认知科学「人类无需预定义事件类别即自然分割连续活动」形式化为可标注、可评测的基准（Kinetics-GEBD），并给出两条使之可操作的标注锚定——固定相对粒度（"1 level deeper"）与 dominant subject 注意力，本模块模板的两条锚定即其移植；对其共识强度的准确措辞是**中等共识可达**（每视频 5 人多评的协议数据支撑「可标注」，而非「天然高度一致」）——这正是本模块在判据之外仍保留 trace 审计闭环（`segment.boundary` reason 抽读 → 调 context/window/gap → 同 seed 重跑对比）的原因。三步演绎结构照抄 Def-DTS [47] 的可操作化手法（双向上下文概括 → 封闭集意图分类 → enforced 演绎查表，LLM 不自由判断边界）；其消融证据的精确读法：**半结构形态（仅保留双向概括、去掉关系分类）比裸问题更差，完整三步最优；而在边界信号清晰的数据集上，裸判决可胜过全套结构**——结构收益集中于边界模糊场景，GUI 流的跨 App 延续与弹窗插入恰属此类；其意图词表逐数据集改池（Dialseg711 删值、SuperDialseg 换表）证明**关系词表按域定制是该方法的预期用法**，本模块五值词表即 GUI 域定制：`advances`/`context_switch` 以实体延续重划对话域的「相关新话题」，`returns_to_entry` 是对话域没有的 GUI 入口态线索，`interruption` 噪声维则来自 RPA UI 日志分割对「不属于任何例程的噪声事件」的显式处理（Marrella；Leno et al. [50]）——「分段 = 边界发现 + 噪声剔除」的问题定义同源，其难点变体「交错例程」v1 明确不做（8.4）；「会话首帧恒为段首」对应 Def-DTS「对话首句恒判 YES」。滑窗 LLM 逐帧裁决是 2026 年 GUI 轨迹量产管线仍在使用的形态之一（VideoAgentTrek / Video2GUI [60]）；对照形态是整段单调用——GUIDE [57] 报告该形态 99.4% 的段可用率，v1 保留滑窗（有界上下文、**有界单请求规模**——v1.11 措辞修订（V9）：预算装填下单窗帧数随内容浮动但恒有上界 window ∧ input_budget），`window` ≥ 会话长**且预算装得下整段**（v1.11 补预算前提：所引 profile 未声明 `context_window`，或整段装填 est 不超 input_budget）时滑窗天然退化为整段单调用，故两形态是同一旋钮的两端，会话不长时建议调大 window 以贴近该证据形态（S32；「extract 先行 + 在动作序列上分段」的次序变体以成本权衡列演进候选，8.4）。「定位/分段」与「描述/标注」拆为两道工序（segment 与 M5 分立）沿 dense video captioning 的两段式先例（Vid2Seq [52]）。

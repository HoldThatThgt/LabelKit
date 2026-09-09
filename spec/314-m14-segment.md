## 3.14 M14 segment——时序流语义分段

### 3.14.1 职责与边界

**做：**（v1.8 新增算子）把完整候选会话精化为 episode：对 `status="active"` 的帧信封按 `session_id` 重组会话（M10 装箱时盖章，3.10.3），可选 LLM 滑窗边界裁决与逐帧噪声标记（3.14.4）；成员信封置 `absorbed`、噪声帧置 `dropped_noise`，按序键拼装序列 Record 并向批尾追加 episode 信封（4.3 契约 ②b）。链序位于链首（`_CHAIN_ORDER` 首位、dedup 之前，3.10.3）——episode 形成先于一切逐条算子：帧级判重语义在连续 UI 帧上失效，重复判定改为 episode 级（3.3）；类标签、质量分、标注全部以 episode 为单位。`segment.enabled = false`（默认）时本算子不入链，工具行为与 v1.7 一致（输出仅多 `_meta.stream: null` 恒在键，6.3）。
**不做：**不排序、不会话化（`[stream]` 规则层属 M2，3.2；M14 收到的是完整会话）；不判重（M3）；不推断动作（M15）；不打任务标签（M5）；不改链结构（②b 改变的只是批内信封基数与状态，与 ②a 同为受控例外）。

**v1.18 sequence generation 边界：**sequence `generate_only` 直接投影 primary sequence 与 stream event，不进入 M14。只有把生成的 stream 工件作为普通 process 输入并开启 `segment` 时，本算子才按本节既有规则处理；`owner_sequence_id`、role、noise 与 replay provenance 都不是分段 oracle。因而 replay 验收检验的是普通内容分段与噪声移除路径，不是生成真值直通。

| 模块 | 职责 | 边界 | 依赖 |
|---|---|---|---|
| M14 segment | 把完整候选会话精化为 episode：可选 LLM 滑窗边界裁决与逐帧噪声标记；成员信封置 absorbed、噪声帧置 dropped_noise，按序键拼装序列 Record 并尾部追加 episode 信封（②b） | 不判重（M3）；不推断动作（M15）；不打任务标签（M5）；不改链结构 | M1, M8, M9 |

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
     record = Record(kind="sequence", id=process_sequence_id(...), ← 会话身份、出现位置与成员 ID
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
                         cfg: ResolvedConfig, with_reason: bool,
                         digests: Sequence[str]) -> PromptBundle
                                       # 会话级预计算的完整逐帧证据与相邻帧 diff，不在窗口内裁剪；
                                       # 有图片的帧全部附图，UI profile 必须支持 vision
async def judge_window(frames: Sequence[Record], ctx: RunContext) -> list[str]
                                       # 一窗一调用：经 complete_validated(schema=
                                       # segment_window_schema(len(frames), with_reason))；校验后
                                       # 按 index first-wins 建表、缺席帧缺省 "continues"，返回与
                                       # frames 对齐的逐帧 relation；每窗发一条 segment.boundary
                                       # 事件（3.14.6）。M7 成员回收复裁直调本函数（3.7）
```

完整逐帧证据由 common.inference.sequence_evidence.record_evidence 提供：文本完整保留，UI 使用 UITree.serialize(max_chars=None)。tree_diff 保留已有结构变化统计，不能替代完整证据。

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
| `"llm"` / `"hybrid"` | 全会话完整证据窗口；一帧重叠，后窗拥有重叠裁决。单帧会话零边界请求。 |

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
  [帧 {i}] {record_evidence(frame_i)}
  [帧 {i} 变更] {tree_diff(frame_{i-1}, frame_i) 的文字化摘要}      ← i ≥ 1；窗首帧无此行
  （帧含图片时：
   每帧完整证据 text Part 前附该帧 kind="image" 的 Part，3.9.2）
```

两个锚定写死在模板文本（不随配置变化）：粒度锚定 =「完整任务」层级（GEBD "1 level deeper" 原则）；注意力锚定 = 只看前台 App/窗口（GEBD dominant subject 原则）。关系词表固定且域无关；`advances` 与 `context_switch` 的分界钉死为**实体延续**——相关但无实体延续的新流程 = context_switch（边界）。LLM 不直接回答边界问题，只做封闭集分类（M8 enum 硬校验，与 classify 同款防线）；`boundary` / `noise` 是代码侧查表结果（**演绎映射**）：

| relation | 演绎结果（代码侧查表，LLM 不可见） |
|---|---|
| `continues` / `advances` | 非边界（帧归入当前段） |
| `returns_to_entry` / `context_switch` | 边界——**该帧是新段第一帧** |
| `interruption` | noise（`noise_filter=true` 时剔除；false 时按非边界成员保留在所属段内） |

**会话首帧恒为段首**：rel[0] 的边界值不参与判决（无论 LLM 输出何值，帧 0 都开启首段）；noise[0] 照常生效。

**调用与校验**：每窗 1 次调用，经 `complete_validated(schema=segment_window_schema(...))`（3.8.3）；temperature 恒 0。planner 先按 session/item/window ordinal 同步预计算摘要、diff 成本与全部窗口，再把跨会话窗口冻结为一个 `TaskGroupRequest`。TaskExecutor 经 `segment.llm` 资源通道有界执行；叶任务只返回与窗口对齐的 relation outcome，不写 session、成员状态、episode、events 或 counters。全部 outcome 收齐后，reducer 按 session/window ordinal 执行接缝覆写、成段、状态迁移与尾部追加；结果与完成顺序无关。无 rng 消耗（种子豁免面不变，2.6）。rules 与孤帧路径提交零任务。普通 ProviderFatal 转为既有会话 keep/fail outcome，不取消 sibling；逃逸 internal/control 异常结构化取消 execution domain。episode 构成以 LLM 输出为条件（classify 分池同款条件化声明，2.6 幂等行）；同输入同 seed 逐字节可复现。

**完整会话窗口归并**：会话开始冻结图像预算校准，预计算完整文本或全部归一化可见 UI 树。
所有帧图片以惰性引用加入请求。window 是帧数上限；现有 budget.pack_windows 按完整证据贪心装箱。
实际请求再完整计入消息、模板、相邻关系、图片和上行 Schema 做预检。不保留无预算固定窗 fallback。

全部窗口叶任务先冻结，再经 ctx.run_group 按 batch_size 提交计算分组；全部返回后按窗口声明序归并。
计算分组不切会话、不影响候选池、随机源或校准周期。相邻窗口重叠一帧，后一窗拥有重叠帧裁决。
precheck 和 reactive 上下文超限都将窗口按完整帧缩为 [start, mid+1) 与 [mid, end)，顺序执行。
每次严格缩短，直到最小两帧，不存在人工两层上限。最小两帧仍超限，整会话原帧 failed，零 episode，
明确 context_overflow，不进入 on_error=keep；仅 reactive HTTP 400 终态向断路器计一次失败。

**成段流程**（rel 定案后，逐会话确定性执行）：

**剔噪**：`noise_filter = true` 时，`rel[i] == "interruption"` 的帧置 `dropped_noise`、reason 标记 `"noise"`（含帧 0）；false 时跳过本步。
**切段**：剩余帧按演绎映射切段——`rel[i] ∈ {returns_to_entry, context_switch}` 的帧开启新段（会话首帧恒为段首）。
**min_len 检查**：仅作用于本步（LLM 边界精化）切出的段（S11）——段长 < `segment.min_len` ⇒ 该段全部帧置 `dropped_noise`、reason 标记 `"below_min_len"`（**≠ "noise"**：未经噪声判据裁决，不得污染噪声审计口径；计数独立，`report.stream.below_min_len`，6.4）。规则层孤帧/短会话（`strategy="rules"` 与 `len(session)==1` 退化）不经 min_len，原样成 episode。v1.9 注：帧信封上的 duck 标 `noise_attribution == ("segment", "below_min_len")` 即 M16 短段救援的**判别载体**（`reason="noise"` 帧不入救援候选池）——M16 救援命中时按 ②c③ 将此类帧 `dropped_noise → absorbed` 翻转（4.3；本模块**零改动**——重组与翻转全在 M16 侧，3.16.4 救援行）；`below_min_len` 计数器为**发生计数**（帧口径），救援**不回退**（救援量另计 `rescued_short`，6.4）。
**容量分区与拼装**：先检查原语义段的 min_len，再对完整 sequence 请求按成员贪心容量分区，容量短尾不再次应用 min_len。fixed/frame/transition/pairwise 等最小单位失败保留完整语义 episode，由失败请求所属阶段消费终态；单个完整帧的 sequence 请求仍不能容纳时，产出 failed episode，成员保持 absorbed。每段成员按序键升序 → 成员信封置 `absorbed` → 构造序列 Record：`kind="sequence"`；`id = process_sequence_id(session_id, member_positions, member_ids)`（形成时定死，后续成员手术不重算，3.7）；`text/raw/ui_tree/image = None`；`modality` = 成员模态；`members` = 成员 Record 元组（序键升序）；`ref` 继承首成员（source_file、line_no（文本）/pair_index（UI），`generated_from=()`、`generator=None`，4.1）→ 尾部追加 episode 信封并盖章 `session_id`（②b）。完整成员溯源由 `_meta.stream.member_sources` 承担（6.3）。

**非容量失败语义**：单窗 M8 修复耗尽按 `segment.on_error` 处置（3.14.6）——`"keep"`（默认）：该会话放弃全部窗口判决，整体原样视为一个语义段（零剔噪、零语义切分），再走同一完整成员容量分区，留痕三件套 = `_meta.stream.degraded = {kind: "segmentation_invalid", windows_failed: k}` + error 事件 + 计数器 `segment.failures`，**不写 `item.errors`**（记录存活；rejects 归因取 `item.errors[0]`，写入会污染后续阶段失败的归因，3.13.4 失败与兜底行同则）；`"fail"`：会话成员全部 failed → rejects。

**摘要贫瘠护栏**：某帧完整证据判贫瘠（可见文本节点为 0 或摘要长度 < 8——无文本 UI 树在真实采集中常见：ghost nodes、画布类屏幕 [63]）⇒ 计 `digest_poor_frames`（`report.stream`，6.4）+ 每运行至多一次 WARN；输入证据贫瘠是纯文本裁决的瓶颈（源数据未提供的实体 LLM 看不见），手册指引**为 `segment.llm` 配置 `supports_vision = true` 的 profile** 补偿（v1.11 改写（V4）：原「开 `segment.use_vision`」指引随该键移除失效——附图由 profile 能力自动推导，选 profile 即选能力）。

容量预览仅 unit=sequence 进行成员分区；fixed/frame/transition/pairwise 等不可缩请求保留完整语义段，
交给原 owning stage 的控制信号和终态消费，不能靠删掉相邻对或提前失败整个序列改变其原语义。
单个完整帧的 sequence 请求仍不可装时，创建 failed episode，原帧 absorbed，错误保留原 owning stage。
内部每个 episode 都携带 member_positions 和初始 [0,N) 允许范围。切点以右侧首位置收紧前后段范围，
前段 sealed；后续 stitch/verify 不得越界。公开 process_sequence_id 统一推导初始 ID，重复内容靠位置区分。
sequence.capacity 只记录结构位置及 stage/profile/phase；容量计数采用公共 splits/sealed/minimum_failures。

process session 的 Schema 调用使用 `CallScope.complete_evidence=true`：L3 修复保留原始完整请求与模型字段错误反馈，容量异常原样交还所属阶段。

### 3.14.5 配置项

`[segment]` 键表（与 5.2 一致，5.2 为配置规范属主；`[stream]` 排序与会话化键属 M2 消费，3.2、5.2）：

| 键 | 类型 / 默认 | 说明与约束 |
|---|---|---|
| `enabled` | bool / `false` | stream 模式总开关。false = 不入链、行为与 v1.7 一致（输出仅多 `_meta.stream: null`）。启用要求 `run.mode="process"` ∧ `generate.enabled=false`（含 generate_only 传递闭合）∧ `annotate.enabled=true`（2.3.1）。`[stream]`/`[segment]`/`[extract]` 在场而本键为 false ⇒ M1 no-op warning（3.1.4）。 |
| `strategy` | `"rules"`\|`"llm"`\|`"hybrid"` / `"hybrid"` | 三态语义见 3.14.4 策略表。 |
| `llm` | str / `"default"` | 边界 profile 必须声明正 context_window；UI 必须支持 vision。 |
| `window` | int / `20` | 完整证据窗口的帧数上限，至少两帧；实际请求仍完整计量。 |
| `noise_filter` | bool / `true` | 仅 llm/hybrid 生效；`rules` ∧ 显式 true ⇒ M1 no-op warning。 |
| `min_len` | int / `2` | 段长下限；仅作用于 LLM 边界精化切出的段（3.14.4 成段流程 ③，below_min_len ≠ noise）。 |
| `vision_resolved` | bool（parse product） | 已解析的 UI 与 profile 能力结果，不用于删除完整请求中的图片。 |
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

计数与归属：`segment.failures`（M14 属主）；`counts.episodes` = segment 阶段 len 差（M10 计量，fanout 同构）；`absorbed` / `dropped_noise` 由状态 tally 归集（M10，3.10.3）；`below_min_len`、`digest_poor_frames` 入 `report.stream`（M14 属主，6.4）；v1.11 增 `report.stream.windows`（V13④，M14 属主，6.4）= **实际窗数**（含 V20 分裂产生的子窗）——与 estimate_run 的最小相邻两帧估算分别展示；未知完整帧内容不能保证静态调用数上界（3.10.3）；`sessions` 数据源 = IngestReport（M2 属主）。rejects 归因（3.11.2）：`dropped_noise` 行按 duck-typed 标记分流为 `stage="segment", reason="noise"` 或 `reason="below_min_len"`。`--strict` 交互：stream 工程的噪声帧属预期产物，`--strict` 会因 rejects 非空退出 1（手册明示）；v1.9 注：`stitch.rescue_short` 命中的 below_min_len 帧翻回 absorbed、不再落 rejects——同输入开启 stitch 后 strict 结果可能由 1 变 0，属预期（3.16.6、3.11.2）。

### 3.14.7 背书

边界判据内置、无需任务词表的依据是 GEBD [48]：其把认知科学「人类无需预定义事件类别即自然分割连续活动」形式化为可标注、可评测的基准（Kinetics-GEBD），并给出两条使之可操作的标注锚定——固定相对粒度（"1 level deeper"）与 dominant subject 注意力，本模块模板的两条锚定即其移植；对其共识强度的准确措辞是**中等共识可达**（每视频 5 人多评的协议数据支撑「可标注」，而非「天然高度一致」）——这正是本模块在判据之外仍保留 trace 审计闭环（`segment.boundary` reason 抽读 → 调 context/window/gap → 同 seed 重跑对比）的原因。三步演绎结构照抄 Def-DTS [47] 的可操作化手法（双向上下文概括 → 封闭集意图分类 → enforced 演绎查表，LLM 不自由判断边界）；其消融证据的精确读法：**半结构形态（仅保留双向概括、去掉关系分类）比裸问题更差，完整三步最优；而在边界信号清晰的数据集上，裸判决可胜过全套结构**——结构收益集中于边界模糊场景，GUI 流的跨 App 延续与弹窗插入恰属此类；其意图词表逐数据集改池（Dialseg711 删值、SuperDialseg 换表）证明**关系词表按域定制是该方法的预期用法**，本模块五值词表即 GUI 域定制：`advances`/`context_switch` 以实体延续重划对话域的「相关新话题」，`returns_to_entry` 是对话域没有的 GUI 入口态线索，`interruption` 噪声维则来自 RPA UI 日志分割对「不属于任何例程的噪声事件」的显式处理（Marrella；Leno et al. [50]）——「分段 = 边界发现 + 噪声剔除」的问题定义同源，其难点变体「交错例程」v1 明确不做（8.4）；「会话首帧恒为段首」对应 Def-DTS「对话首句恒判 YES」。滑窗 LLM 逐帧裁决是 2026 年 GUI 轨迹量产管线仍在使用的形态之一（VideoAgentTrek / Video2GUI [60]）；对照形态是整段单调用——GUIDE [57] 报告该形态 99.4% 的段可用率，v1 保留滑窗（有界上下文、**有界单请求规模**——v1.11 措辞修订（V9）：预算装填下单窗帧数随内容浮动但恒有上界 window ∧ input_budget），`window` ≥ 会话长**且预算装得下整段**（v1.11 补预算前提：所用 profile 的完整请求 est 不超 input_budget）时滑窗天然退化为整段单调用，故两形态是同一旋钮的两端，会话不长时建议调大 window 以贴近该证据形态（S32；「extract 先行 + 在动作序列上分段」的次序变体以成本权衡列演进候选，8.4）。「定位/分段」与「描述/标注」拆为两道工序（segment 与 M5 分立）沿 dense video captioning 的两段式先例（Vid2Seq [52]）。

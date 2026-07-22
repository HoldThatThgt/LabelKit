## 3.15 M15 extract——转移/动作摘取

### 3.15.1 职责与边界

**做：**（v1.8 新增算子）对批内每个 `status="active"` 的序列信封（episode），逐对相邻成员帧 ⟨s_i, s_{i+1}⟩ 经 LLM 产出结构化动作（内部 Schema，3.15.3），写入 `item.transitions`；**转移数 = 成员数 − 1**（动作发生在相邻帧之间 [45]）。链序位于 classify 之后、quality 之前（3.10.3）——类标签先就位使 `[class.<name>.extract]` 按类 instruction 生效（3.15.4 multi 行）；轨迹 rubric 与序列标注在下游消费步骤序列。仅 UI 模态序列（M1 强制 `extract.enabled` 要求 `segment.enabled` ∧ `run.modality="ui"`，2.3.1；文本序列的「转移」语义弱，v1 不适用，列演进候选 8.4）。
**不做：**不重分段（边界属 M14 上游；成员集是给定输入）；不产出用户 Schema 字段（步骤序列是工具内部结构——进 `_meta.stream.steps` 并注入下游提示词；用户 Schema 产出物属 M5）；不淘汰记录（修复耗尽的默认路径是兜底留痕而非丢弃，3.15.6）；不打分、不评审。

| 模块 | 职责 | 边界 | 依赖 |
|---|---|---|---|
| M15 extract | 对每个 active 序列信封的每对相邻成员帧 ⟨s_i, s_{i+1}⟩ 经 LLM 产出结构化动作（内部 Schema），写入 `item.transitions`；转移数 = 成员数 − 1 | 不重分段（M14 上游）；不产出用户 Schema 字段（M5）；不淘汰记录 | M1, M8, M9 |

### 3.15.2 输入 / 输出

| 方向 | 内容 |
|---|---|
| 输入 | 批内 `status="active"` 且 `record.kind="sequence"` 且 `transitions is None` 的 PipelineItem（`transitions is not None` 者幂等跳过，3.15.4）；`[extract]` 参数（5.2）；LLM profile（`extract.llm`，须 `supports_vision = true`——一请求 2 图，3.1.4）。 |
| 输出 | 每个处理信封 `item.transitions: tuple[Transition, ...]`（长度恒 = `len(record.members) − 1`，按成员对位次升序；单条转移失败不破坏该不变量——fallback 占位，3.15.6）；批基数不变（不追加、不淘汰），返回值 = 传入的同一列表对象。`on_error="fail"` 且结构修复耗尽时该 episode `status="failed"`、StageError 入 `item.errors`（唯一改状态路径）。 |

**接缝序数机械占位（v1.9）。**stitch 启用且信封为多碎片线索时（3.16），`seam_indexes` duck 标所列序数（接缝对左成员下标，与 `Transition.index` 同坐标）的转移**不发 LLM 调用**——代码侧生成 T10 占位 Transition（四键与 detail 见 3.15.4 占位行）；其余成员对**照常摘取**（含「间隙仅噪声/本线索救援帧」的拼接对与会话位置紧邻的救援拼接对——真实转移，与 v1.8「剔噪对照常摘取」惯例单一处理，3.16.4 接缝判据）。`transitions is None` 幂等门与 `len(transitions) = len(members) − 1` 不变量**不动**（占位步占据其 index 位次）；实现注记：跳过 seam 序数需重构本模块平铺 gather 的记账结构（既有 `spans` / 切片假设每成员对一协程）——属实质改动，非行内小补。

信封变化示例（沿用 3.14.2 的点外卖 episode `7655568d2c485c43`，成员 f0 首页 → f1 搜索结果页 → f3 餐厅页 → f4 下单确认页；f1↔f3 在采集流中原不相邻——噪声帧 f2 已被 M14 剔除，转移以**成员**相邻为准）：

```
段前: item.transitions = None
段后: item.transitions = (
  Transition(index=0, action={"action_type": "input_text", "target": "搜索框",
                              "value": "麻辣烫", "description": "在首页搜索框键入「麻辣烫」并搜索"},
             model="glm-5.2", attempts=1, detail={}),
  Transition(index=1, action={"action_type": "click", "target": "老王麻辣烫",
                              "value": None, "description": "点击搜索结果中的餐厅进入餐厅页"},
             model="glm-5.2", attempts=1, detail={}),
  Transition(index=2, action={"action_type": "click", "target": "去结算",
                              "value": None, "description": "点击「去结算」进入下单确认页"},
             model="glm-5.2", attempts=1, detail={}),
)
```

步骤序列随后由 M11 写入 `_meta.stream.steps`（verbatim，6.3），并经新形参注入 M4 序列打分与 M5 序列标注提示词（3.4、3.5）。

### 3.15.3 数据结构与 API

```
class ExtractStage(Stage):
    name = "extract"
    def __init__(self, cfg: ResolvedConfig): ...
    async def run(self, batch, ctx) -> list[PipelineItem]: ...   # 返回传入的同一列表（不增不减）

def build_extract_prompt(prev: Record, curr: Record, cfg: ResolvedConfig,
                         label: str | None) -> PromptBundle
                                       # 3.15.4 模板的确定性组装；label 非空时 instruction 取
                                       # class_views[label].extract 有效值（3.1.4 按类覆盖合并行）
async def extract_transition(prev: Record, curr: Record, index: int,
                             ctx: RunContext, label: str | None = None) -> Transition
                                       # 一转移一调用：经 complete_validated(schema=action_schema())；
                                       # 修复耗尽按 on_error 兜底/抛出。M7 成员手术后的接缝重摘取
                                       # 直调本函数（1–2 次/手术；重建的 Transition 带
                                       # detail.reseamed=true 溯源，index 重编号后不变量
                                       # len(transitions) = len(members) − 1 恒真，3.7）
```

产物类型 `Transition`（完整定义见 4.2）：`index`（重建后位次，恒 = 在 transitions 元组中的下标）、`action`（过 Schema 的动作对象）、`model`、`attempts`（1 + L3 修复次数）、`detail`（干净摘取 `{}`；fallback `{kind: "extraction_invalid", message}`；手术重摘取 `{reseamed: true}`）。

动作内部 Schema（`schema_engine.action_schema()`，3.8.1 内部 Schema 清单：不计入 `report.schema_engine.resolved_at`、不经过 L2.5）。**全键 required + 可空联合**：OpenAI strict 模式硬拒可选属性（L0 无条件透传 Schema，3.13.3 `uniqueItems` 同型教训），可空字段以 `["string", "null"]` 类型联合表达而非从 `required` 摘除；关键字集 ⊆ 既有内部 Schema 关键字集：

```
def action_schema() -> dict:
    actions = ["click", "long_press", "input_text", "scroll", "drag", "open_app",
               "app_switch", "navigate_back", "navigate_home", "wait", "other"]   # 11 值（S15）
    return {"type": "object",
            "properties": {"action_type": {"type": "string", "enum": actions},
                           "target": {"type": ["string", "null"]},
                           "value": {"type": ["string", "null"]},
                           "description": {"type": "string"}},
            "required": ["action_type", "target", "value", "description"],
            "additionalProperties": False}
```

### 3.15.4 算法与流程

**提示词模板**（确定性拼接，逐字冻结于 CONTRACTS §10.10；一请求 2 图 = pairwise quality 既有形态，3.4.3）：

```
system:
  你是屏幕操作流的动作摘取员。给定同一操作流中相邻的前后两帧屏幕状态，推断用户在两帧之间
  执行的动作。action_type 只能取以下值：
  - click / long_press / drag: 点击 / 长按 / 拖拽某控件
  - input_text: 在输入框键入文本
  - scroll: 滚动屏幕或列表
  - open_app: 打开一个应用；app_switch: 切换到另一已打开的应用
  - navigate_back / navigate_home: 系统返回 / 回到桌面
  - wait: 无用户交互，仅等待界面加载或变化
  - other: 无法归入以上任何一类（把语义写进 description）
  锚定约定：前一帧是动作发生前最后一个稳定状态，后一帧是动作完成后的首个稳定状态；推断
  二者之间发生的单个语义动作；若变化由多个低层事件构成（连续滚动、连续键入），归并为一个
  语义动作。
  {instruction}                                 ← 可选补充说明（per-label 有效值）；缺省省略此行
  输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：
  {"action_type": <词表值>, "target": <目标控件文本引用或 null>,
   "value": <动作参数或 null>, "description": <一句话动作描述>}
user（单条消息多 Part——「text 标签 + image」组装惯例同 3.5.2/3.13.3；一请求 2 图）:
  text part:  [前一帧截图]
  image part: s_i.image                          （M9 调用时编码，3.9.2）
  text part:  [后一帧截图]
  image part: s_{i+1}.image
  text part:  [树变更摘要] {tree_diff(s_i.ui_tree, s_{i+1}.ui_tree) 的文字化}
                                                 ← include_diff = true 时；false 整段省略
              [前后帧树摘要] {frame_digest(s_i)} → {frame_digest(s_{i+1})}
```

锚定句（「前一帧是动作发生前最后一个稳定状态……归并为一个语义动作」）移植自 OpenCUA 的 State-Action Matching 与 Action Reduction 约定 [43]（3.15.7）。`[树变更摘要]` = `tree_diff`（第 4 章 helper，M14 同源）输出的确定性文字化——增/删/文本变化节点数、变化比例、App/标题是否变更；零额外 LLM 调用。

**字段语义**（逐字冻结于 CONTRACTS §10.10 表；词表合法性由 Schema enum 保证，字段语义由模板文字锚定）：

| action_type | target 语义 | value 语义 |
|---|---|---|
| `click` / `long_press` / `drag` | 目标控件的**文本引用**，取值优先级 text → content_desc → 类名+序号；不可辨识时 null | null |
| `input_text` | 被键入输入框的文本引用（同上优先级） | 键入的文本——**聚合语义**：同一相邻对之间的「聚焦点击 + 键入」归并为一步 input_text，聚焦点击不单独记步 |
| `scroll` | 滚动容器引用；不可辨识时 null | 方向，限 `up` / `down` / `left` / `right`（模板文字锚定四值；代码侧小写归一） |
| `open_app` / `app_switch` | null | 应用名 |
| `navigate_back` / `navigate_home` / `wait` | null | null |
| `other` | 尽力而为的对象引用或 null | null（语义全落 description） |

两条设计注记：① **target 用文本引用、不用坐标**——extract 是事后标注而非执行器，元素文本引用与中心坐标对 LLM 等效 [45]，且 `max_image_px` 降采样会破坏坐标系与原始截图的对应；② `include_diff = true`（默认开，可关做 A/B）——注入的是**结构化树 diff 而非像素 diff**：像素 diff 注入在 Sharingan 中报告为负结果 [59]，结构化 diff 则是确定性归并证据（OpenCUA Action Reduction 同族 [43]），缩短视觉推断距离、降低幻觉（S14）。

其余关键设计的精确定义：

| 设计点 | 定义 |
|---|---|
| 调用与校验 | 每对相邻成员帧 1 次调用（`extract_calls = Σ(len(members) − 1)`），经 `complete_validated(schema=action_schema())`（3.8.3）。temperature 恒 0。 |
| 并发 | 批内全部转移（跨 episode）并入**一个** `asyncio.gather`（profile 信号量约束）——骨架同 M4 pairwise phase2（3.4.3）；**无 rng 消耗**（种子豁免面不变，2.6）；结果按 (episode 批内位置, 对位次) 定位回写，与完成顺序无关，逐字节可复现。 |
| 幂等 | `transitions is not None` 的信封跳过——任何重入零额外调用。M7 修复路径不重跑本 stage：接缝重摘取经 `extract_transition` 函数直调（3.15.3、3.7）。 |
| multi 扇出（**按 label 各摘**，S9） | classify `assignment="multi"` 扇出的兄弟信封克隆时 `transitions` 恒 None（classify 在前、extract 在后，3.13.4 multi 扇出行）——每个兄弟按**各自 label** 的有效 `extract.instruction` 独立摘取（per-label 白名单承诺兑现；transitions 每信封自持，接受 ×k 调用成本）。episode 命中多类应属罕见——M14 边界判据即「单一目标导向活动」（3.14.4）。dry-run 估算按乘数 1 报下界 + stderr 注明（3.13 R28 口径，2.4）。 |
| fallback 语义 | 单转移 M8 修复耗尽且 `on_error="fallback"`（默认）：该步写入代码侧构造的兜底 Transition——`action = {"action_type": "other", "target": null, "value": null, "description": ""}` + `detail = {kind: "extraction_invalid", message}` 留痕；episode 存活、后续转移照常摘取；**不写 `item.errors`**（rejects 归因取 `item.errors[0]`，写入会在该记录后续阶段失败时污染归因——3.13.4 失败与兜底行同则）。留痕使兜底步与 LLM 确证的 other 对下游可区分（detail.kind 在场与否）。 |
| 接缝占位（v1.9，T10 四键钉死） | `seam_indexes` 所列序数（3.15.2 占位段）写入代码侧构造的占位 Transition，**零 LLM**：`action = {"action_type": "app_switch", "target": null, "value": null, "description": "线索接缝：被<打断者>打断后恢复"}`（<打断者> = interrupted_by 各任务名顿号连接）+ `detail = {kind: "thread_seam", interrupted_by: [...]}`（按接缝判据恒非空，3.16.4）；steps 步行 `resumed = true` 落接缝步自身（emitter 由 detail.kind 推导，6.3）。**语义备注**：占位 `action_type="app_switch"` 对同 App 内穿插（返回同页型）语义不贴——占位类型**不承诺语义**，下游以 `detail.kind` 判别（与 extraction_invalid 留痕同法）。**计数器口径**：seam 占位**不计入** `report.stream.extract.transitions` 与 `extract.by_type.*`（非摘取产物——零 LLM 的 app_switch 会灌污 by_type 分布；接缝唯一计量点 = `report.stream.stitch.seams`，6.4）；**相邻救援不占位**：会话位置紧邻的救援拼接对是真实转移，照常送 LLM 摘取并正常计数（3.16.4 救援行）。 |
| 上下文预算（v1.11） | 摘取 profile 声明 `context_window` 时（未声明 = 预算关闭，行为与 v1.10 一致；机制见 3.9）：恒定 2 帧 + 2 图的单转移调用**无可收缩项**（图不可减帧、diff / 摘要段结构有界）——不做装填裁剪，由 M9 咽喉终检兜底（V16）；溢出（终检命中或反应态，本调用点无降级面）→ 该步走既有 `on_error="fallback"` 机械回退语义**不变**（`action_type="other"` 兜底步留痕，3.15.6；`"fail"` 时错误分类按 7.6 词表精确记 `context_overflow`）。接缝占位步零 LLM、不受预算影响（本表接缝占位行）。 |

### 3.15.5 配置项

`[extract]` 键表（与 5.2 一致，5.2 为配置规范属主）：

| 键 | 类型 / 默认 | 说明与约束 |
|---|---|---|
| `enabled` | bool / `false` | 启用要求 `segment.enabled` ∧ `run.modality="ui"`（2.3.1）；`[extract]` 在场而 `segment.enabled=false` ⇒ M1 no-op warning（3.1.4）。 |
| `llm` | str / `"default"` | LLM profile 引用；**恒**计入密钥解析 / vision / probe / 存在性四处引用集且恒入 vision_users（S30）——每请求 2 图，无纯文本档。 |
| `instruction` | str / `""` | 可选域提示，注入模板可选行；`[class.<name>.extract]` 按类覆盖白名单的**唯一**键（5.2 白名单表；per-label 生效见 3.15.4 multi 行）。 |
| `include_diff` | bool / `true` | `[树变更摘要]` 注入开关（S14）；关闭可做 A/B 对照（手册调用账章给指引）。 |
| `on_error` | `"fallback"`\|`"fail"` / `"fallback"` | 单转移修复耗尽处置（3.15.6）。 |

### 3.15.6 错误处理

错误码 `extraction_invalid`（7.6，v1.8 增行）两形态：

| `extract.on_error` | 行为 |
|---|---|
| `"fallback"`（默认） | 该步记兜底动作 `action_type="other"` + `Transition.detail = {kind: "extraction_invalid", message}` 留痕（3.15.4 fallback 行）；episode 存活、**不写 `item.errors`**；+ error 事件（kind = `extraction_invalid`，extract 通道）+ 计数器 `extract.fallback_steps`。 |
| `"fail"` | 该 episode 信封 `status="failed"`、`StageError(stage="extract", kind="extraction_invalid")` 入 `item.errors` ⇒ rejects；+ 计数器 `extract.failures`。 |

事件（7.2，v1.8 增行；通道 `"extract"` = stage 名——`_TRACE_CHANNELS` 8→10 之一，error 事件按 stage 自动归属）：

| 事件名 | 通道 / stderr 级别 | 触发点 | payload 字段 |
|---|---|---|---|
| `extract.step` | extract / —（trace-only，无 stderr 镜像） | 每转移定案后（含 fallback 步）；`record_ids = (s_i.id, s_{i+1}.id)`。 | `episode_id`、`index`、`action_type`、`description`†、`target`‡、`value`‡。 |

脱敏分级（S27；7.4）：`target` / `value` 是**输入数据派生**（可能含用户键入文本）——纳入 `_DATA_KEYS = {"target", "value"}`，`none` / `refs` 档剥除（refs 档「无输入数据内容」红线）、`excerpt` 起携带（‡）；`description` 是 LLM 自由文本——纳入 `_FREE_TEXT_KEYS`，`none` 档剥除、`refs` 起携带（†）。逐档 payload：`none` = `{episode_id, index, action_type}`；`refs` = + `description`；`excerpt` = + `target`、`value`。

计数与归属（M15 属主；`report.stream.extract` 子块，6.4）：

| 计数器 | 语义 |
|---|---|
| `extract.transitions` | 产出转移总数（含 fallback 步）。 |
| `extract.fallback_steps` | 兜底步数（fallback 路径）。 |
| `extract.failures` | fail 路径失败 episode 数。 |
| `extract.by_type.<action_type>` | 逐动作类型分布（S14 ③）——系统性劣化（如 other 占比异常升高、某类型塌缩）可观测，供 `include_diff` A/B 与模型/提示词迭代用。 |

### 3.15.7 背书

「从相邻状态对推断中间动作」是被大规模验证过的独立工序：OpenAI VPT 用逆动力学模型（IDM）给 70k 小时无标签视频打伪动作标签——因可同时看过去与未来帧，反演环境动力学远比行为克隆易学 [42]；LabelKit 的负边界「不训练/托管本地模型」（2.1.2 ①）决定本模块用 **LLM zero-shot 充当运行时 IDM**（与 M4 以运行时 API 裁决替代 QuRater 离线分类器是同一既有决策的延伸，3.4.5 背书行），且同样利用非因果优势（一次调用喂 s_i 与 s_{i+1} 两图）。「三元组 ⟨s_pre, a, s_post⟩ → 结构化动作/低层指令」正是 OS-Genesis reverse task synthesis 的第一道工序（ACL 主会级验证）[41]——差异在 OS-Genesis 的动作是采集时自带，本模块输入只有状态流故动作须推断。「确定性归并 + LLM 语义化」的两层分工出自 OpenCUA 的标注基础设施 [43]：其 Action Reduction 把海量低层事件确定性归并为语义动作（鼠标移动序列→click、滚轮单方向累计、连续键击→文本串）、State-Action Matching 把每个动作锚定到动作前最后一个视觉稳定帧（防未来信息泄漏）——对应本模块「树 diff（代码侧确定性）+ LLM 动作裁决」的分工与模板锚定句（3.15.4）。动作词表对齐口径 = **AndroidControl 词表全集 ∪ UI-TARS-mobile 增量 + other 兜底**：AndroidControl 的 8 动作（click, long_press, input_text, scroll, navigate_back, navigate_home, open_app, wait）全集采纳、无裁剪 [45]（被有意排除的仅其论文 agent 侧人工插入的 terminate/status——终态判定属分段边界与标注语义层；「转移数 = 截图数 − 1」计数恒等式即本模块调用量公式）；`drag` 与 `app_switch` 取自 2025–2026 统一动作空间共识（UI-TARS / UIPro [62]）——跨 App episode 是本设计一等公民（GUI-Odyssey 全集皆跨 App [46]），应用切换须是词表内一步而非边界；`other` 兜底优于原表（人工采集不产词表外动作，LLM 推断会）。**可靠性预算**（S14，风险表与手册调优章同源）：LLM zero-shot 动作推断的可靠性钉在 **70–80%/步**（Watch & Learn 实测 70.5% [58]；Sharingan 70–80% 且按动作类型不均衡 [59]）——每步 20–30% 错误率会沿 episode 级联，本模块不承诺单步正确性，承诺**缓解链**：树 diff 证据注入（`include_diff`，3.15.4 注记 ②）+ verify 缺陷路由兜底（步骤↔标签一致性硬门，3.7）+ quality 结构分软门（连贯性/噪声残留维度压分缺陷段，3.4）+ `extract.by_type` 分布可观测（3.15.6）。

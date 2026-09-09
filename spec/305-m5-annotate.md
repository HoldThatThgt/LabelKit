## 3.5 M5 标注 annotate

### 3.5.1 职责与边界

**做：**为每条存活记录组装标注提示词（任务指令 + few-shot + 记录内容，UI 模态含截图与序列化树），经 M9 调用 LLM、经 M8 获得符合用户 Schema 的标注对象；可选 self-consistency 多次采样字段级投票（3.5.2）。 
**不做：**不校验结构（全部委托 M8）；不评审标注质量（M4/M7 职责）；不产出新记录（生成属 M6）；不做提示词内容的「智能改写」——提示词组装是确定性模板拼接。

### 3.5.2 标注提示词组装（确定性模板）

**标注后处理。**工程函数可规范化模型已经生成的字段，也可计算 Schema 中标记
`x-labelkit-postprocessor: true` 的代码负责字段。该标记定义生成职责，不限制函数只能写标记字段。
完整契约见 `docs/dev/SPEC-annotation-postprocessing.md`。

图 3-7 标注候选的工程后处理与完整校验

M5 用 `FinalizedCallRequest` 接入既有结构引擎。函数对每个模型 L2 合格候选恰好调用一次；
模型 L2 不合格候选不调用。普通记录与帧传真实 raw 副本，序列记录传 None；候选与返回值也深拷贝隔离。
函数必须返回完整标准 JSON 字典，异常和非法返回为固定脱敏 `PostprocessorError`，不进入模型修复。
最终 Schema 不合格或工程函数自行产生框架时间沿定稿内部错误传播；普通批只失败当前记录，
sequence 为终态错误、退出 4，不消费 slot attempt。

每次 self-consistency 采样、内部 L3、verify 重标注、episode repair 和 frame backfill 均经过同一边界。
self-consistency 按模型 Schema 的字段投票并选择已经完整验证的某个原候选，不拼接对象、不再次执行函数。
帧标注使用独立模型/完整帧 Schema 与有效帧类函数，继续无记录级 output validator 和 resolved-at 记账。
模型 prompt、response Schema、few-shot、修复 previous_output 和对应预算均不包含代码字段与框架时间字段。
sequence attempt 只能读取 GenerationProgram 冻结的类视图与 model_frame_schema。

```
system:
  {annotate.instruction}                        # project.toml，必填
  输出必须是符合以下 JSON Schema 的单个 JSON 对象，不输出任何其他内容：
  {user_schema_json}                            # M8 提供的规范化 Schema 文本
user (对每条 few-shot 示例，依次):
  [示例输入] {example.input}
  [示例输出] {example.output}                    # M1 启动时已校验示例输出符合用户 Schema
user (当前记录):
  文本模态: [待标注数据] {record.text}
  UI 模态:  [屏幕截图] <image: base64>
           [UI 控件树] {record.ui_tree.serialize(max_chars=input.ui_tree_max_chars)}
```

UI 树序列化格式（`UITree.serialize()`，4.3 节）：深度缩进的每节点一行 `<role> "text" [l,t,r,b] {关键属性}`，只保留可见节点与非空属性，超出 `ui_tree_max_chars` 时按深度优先截断并追加 `…(truncated N nodes)` 标记。此「图 + 线性化结构文本」双通道输入是 ScreenAI 的 screen-schema 表示 [13] 与 GUI 智能体输入惯例 [16][17]。

**装配变体参数对象（2026-08-14 代码规则整改）。**`build_annotate_prompt(record, cfg, schema_text, opts)` 与 `annotate_record(record, ctx, opts)` 的**全部**装配变体取值集中在一个冻结 dataclass `AnnotatePromptOptions` 上：

| 字段 | 语义 | 引入 |
|---|---|---|
| `repair` | 修复上下文（`RepairContext`）；None = 首次标注 | v1.1 |
| `temperature` | 采样温度；None = profile 默认（M5 内部设定，调用方置值忽略） | v1.1 |
| `label` | classification 标签；有 inherited 标签时同样选定按类标注 Schema | v1.7 |
| `transitions` | `[动作序列]` 步骤源；None = 整段省略 | v1.8 |
| `image_px` | 普通单记录图像工作点；process stream 忽略此参数并保持冻结 profile 表示 | v1.11 |
| `temporal_context` | generate-only sequence 的冻结业务时间上下文 | v1.20 |

默认实例使用全局首次标注；修复、类路由和时间上下文以 `dataclasses.replace(opts, …)` 构造明确的新值。

**按类取值（v1.7）。**classify 启用且记录带类标签时，本节模板的 `{annotate.instruction}` 与 few-shot `examples` 取该类有效配置（`class_views[label].annotate`，3.1.4 按类覆盖合并行）——模板结构不变，仅取值来源变化。取值载体为 `opts.label`（None = 全局配置）；stage 层传 `item.classification.label if item.classification else None`。trace `annotate.done` 事件 payload 增 `label` 字段（仅 classify 启用时携带，7.2 只增不改）。

**按类标注 Schema。**`label` 同时选定标注 Schema：类有效 Schema =
`class_views[label].schema ?? cfg.user_schema`。process 的 LLM/fallback 标签与 v1.18 sequence projector 写入的
inherited 标签走同一单点取值函数；不能以 `cfg.classify.enabled = false` 为由跳过 `ClassView`。类未声明覆盖、
label 缺失或类表外未知类使用全局。完整 Schema 供最终验证与写出，模型 Schema 排除业务时间与代码字段；
每个消费点使用其对应 Schema，保证计价 Schema 与模型实际调用相同：

| 消费点 | 按类有效取值 |
|---|---|
| 本节模板的 `{user_schema_json}` | 类有效模型 Schema 的 canonical 单行 dump；没有投影及类覆盖时沿用 M8.user_schema_text |
| 标注调用（首次 / 修复重标注） | 有后处理或时间绑定时使用完整/模型 Schema 的 finalized 接口，scope.user_treatment=True；其余沿用原显式 Schema 与全局推断路径 |
| self-consistency 字段级投票 | 可投票字段取自类有效模型 Schema；返回已经完整验证的原候选 |
| v1.11 预算装填的 schema 计价项 | 按类有效模型 Schema 及投影示例计价 |
| M7 修复路径的重标注与实际预算 | 使用同一 label、模型 Schema 和移除代码/时间字段的 previous_output |
| M11 写前终检 | 按**该行**类标签取有效 Schema（3.11.2；multi 扇出的兄弟信封各带自己的标签，按行天然对齐） |

未配置按类 Schema、后处理或时间绑定时，保持既有调用形和模型调用数量。

**标注鲁棒性：self-consistency（可选，v1.2）。**`annotate.self_consistency = n`（默认 0 = 关；启用须 n ≥ 3 且为奇数，5.2）时，M5 对每条记录按本节模板独立采样 n 次（temperature 统一取 `annotate.sc_temperature`，默认 0.7——采样多样性的来源），每次输出都各自经 M8 走完整结构保证后才参与投票。**字段级投票**：enum / boolean / integer 字段逐字段取 n 个样本中的众数；自由文本 / 数组字段不逐字投票，取「与众数字段组合一致的样本」中第一个的对应字段值。其余类型字段（number、嵌套 object 等）与自由文本/数组同法处理（不逐字段投票，随众数字段组合整体取值）。全体分歧（众数组合不存在或无样本与其完全一致）时整体采用第一个样本，并计入 `report.annotate.sc_disagreements`。某次采样经 M8 修复仍失败（SchemaViolation）⇒ 该样本弃权、由其余合法样本投票（agreement_ratio 分母仍为 n）；n 次全部失败才置 `status="failed"`。`_meta.annotation.attempts` 记 n 次采样 attempts 之和。`_meta.annotation` 增 `sc = {n, agreement_ratio}`（agreement_ratio = 与最终众数字段组合完全一致的样本数 / n；6.3 只增字段）；trace `annotate.done` 事件 payload 增同构 `sc` 字段（7.2「只增不改」契约内扩展）。该机制对分类型 Schema 收益最大——如统一示例的 `intent` / `difficulty` 枚举字段：多路径采样 + 多数投票显著优于单次贪心解码（Self-Consistency，Wang et al., ICLR 2023 [33]，GSM9K +17.9%）。成本：标注调用与 token ×n。

**处理序列的完整标注。**`run.mode="process"` 且 `segment.enabled=true` 时，序列请求固定包含：

```text
system: 生效 instruction + 代码负责投影后的模型 Schema
few-shot: 保持配置完整声明序
user:
  [动作序列] 全部已有 Transition 按 index 顺序渲染；None 时省略整个动作段
  [序列成员]
  逐成员完整正文；UI 成员逐个包含原工作点截图及完整可见 UITree.serialize(max_chars=None)
  最后始终是 text Part；repair 后缀附在末尾
```

`record_evidence` 与 `sequence_parts` 是所有实际成员内容的共同渲染面。全部步骤、图片、成员、指令、few-shot、
模型 Schema、上一版标注及审核意见均进入实际预算；`input.ui_tree_max_chars` 不裁处理序列。
图片仅在序列化实际调用时惰性编码，整次会话维持 profile 固定图片工作点和冻结图像成本。
`AnnotatePromptOptions` 只含 repair、temperature、label、transitions、image_px、temporal_context；
image_px 只用于普通记录的合法路径，处理序列不因修复轮而抽图或换档。不存在 sequence_frames、k_eff 或 fragment_lens 接口。

**容量归属。**`AnnotateStage.preview_capacity(item,ctx)` 对全部可达类别和启用帧类，用真实 builder 检查完整模型请求；
不发送模型、不执行后处理、不修改信封或指标。实际 precheck/reactive 原始 ContextOverflowError 在 SC 弃权、
failed 或成员 None 投影之前交给当前 owning stage。完整序列可按完整成员边界拆分；仅固定指令/few-shot/Schema
已超限则登记 fixed 最小终态。verify 的成员手术和 repair 后缀都以 expanded working item 为实际 target，owner 保持 verify；
已知同 stage/profile/view/positions 最小终态在再次调用前投影，不重发相同请求。原错误的 phase/profile/origin 不变。

**ordinary 与 generation 的合法路径。**普通单记录仍使用 input.ui_tree_max_chars 和原 UI 树预算帽，指令、Schema、
few-shot 与 repair 动态块只计不裁。generate-only sequence 仅支持文本，保留其既有有界步骤/成员文本预算和时间绑定；
生成流程的 whole-set 原子尝试规则不变，不引入已删除的 UI 抽图路径。transitions 总是由 stage 传入当前值；
verify 手术后重标注使用重建值。sequence 的 Record.raw 为 None，普通 record validator 的既有输入约定不变。

**执行形态。**planner 对 ordinary 批或 process 完整会话按 item/sample 顺序冻结全部任务；ctx.run_group
按 batch_size 分组执行，叶结果不得修改信封。完整波次结束后，归并器按 item/sample 声明序收齐同步计划异常与所有
叶容量错误，以非空 `SessionCapacityError.failures` 一次交给控制器，再投票并写 annotation、status、errors 与计数。
固定开销用同一 builder 的空成员、空 transitions（原存在时）计算，保留 user 包络、恒有段落标签、few-shot 与修复后缀。
固定包络本身超限直接归 fixed，不增加 sequence 切点。处理流的记录和帧调用均设置 `CallScope.complete_evidence=true`，
M8 结构修复保留完整原证据，并让原始容量错误上抛给本阶段。
sequence reducer 完成后，帧 pass 对当前成员出现位置冻结叶任务；仅在当前尝试字典补缺位。处理会话同内容 ID 的
不同位置分别调用并产出，不能按内容 first-wins。generation 帧产品维持唯一事件 ID 键。
`annotate.enabled=false, frame.annotate.enabled=true` 时序列任务组为空，帧 pass 仍执行。

生成 sequence attempt 的 dataset 写入受 AttemptTransaction 约束；处理流受会话尝试约束。两者失败尝试的
Schema、usage、retry、trace 保留，帧/序列产品和 dataset 计数不泄漏到重算。会话和生成尝试内 ProviderFatal
原样上抛；普通单记录维持局部错误隔离。叶任务不得嵌套 run_group。

### 3.5.3 API 与错误处理

```
class AnnotateStage(Stage):
    name = "annotate"
    def preview_capacity(self, item, ctx) -> SessionCapacityFailure | None: ...
    async def run(self, batch, ctx) -> list[PipelineItem]:
        """对每条 active 记录: prompt = build_prompt(rec); item.annotation = await ctx.schema_engine
           .complete_validated(profile, prompt, user_schema)  # M8 全责保证结构
           SchemaViolation(不可修复) ⇒ item.status='failed', 错误入 item.errors。"""

    async def run_attempt(
        self,
        request: DownstreamAttemptRequest,
    ) -> DownstreamAttemptResult:
        """在 sequence attempt 内执行 sequence 与可选 frame 标注，不提交全局状态。"""
```

v1.18 sequence 的 `run_attempt` 与普通 `run` 共用标注核心，但不先把异常降级为持久 `StageError`。
`ProviderFatalError`、`CircuitBreakerTripped`、`KeyboardInterrupt` 与 `asyncio.CancelledError` 原样穿透；
retryable exhaustion、Schema/context/truncation 或普通标注拒绝返回 `accepted=false`，由 M10 丢弃整个
counterfactual set。annotation、frame annotation、item status 与 dataset counter delta 只存在于当次
`AttemptTransaction`，只在组提交后合并；SchemaEngine resolved-at、LLM usage/retry/latency 与 trace event
是已发生的运行事实，不随拒绝回滚。

**背书：**「指令 + few-shot + 结构化输出」的 LLM 标注器是 distilabel（Argilla）[5] 与 Autolabel（Refuel）[12] 两个工业框架的核心抽象；UI 模态输入表示见 3.5.2 背书 [13][16][17]；self-consistency 字段级投票为 Wang et al.（ICLR 2023）的多路径采样多数决 [33]。

### 3.5.4 输入 / 输出示例

#### ① 文本模态标注（输入法中文指令 → 意图标注）

工程配置：`input.text_field = "instruction"`，`annotate.llm = "default"`，`annotate.examples` 含 1 条 few-shot 示例，用户 Schema 经 `output.schema_inline` 内嵌（M1 启动时已校验示例输出符合该 Schema）。输入记录（id 规则见 3.2.5）：

```
{"instruction": "帮我写一条请假条，明天上午要去医院", "source": "ime-log", "ts": "2026-06-30T10:12:00Z"}
    ⇒ Record(id="1cda030abc565f17", modality="text", text="帮我写一条请假条，明天上午要去医院", ...)
```

按 3.5.2 模板逐字组装的完整提示词（`{user_schema_json}` 为 M8 提供的规范化 Schema 文本）：

```
system:
  你是输入法中文用户指令的意图标注员。判断给定指令属于哪类意图、其主题是什么、
  以及完成该指令对语言模型的难度。
  输出必须是符合以下 JSON Schema 的单个 JSON 对象，不输出任何其他内容：
  {"type": "object",
   "properties": {
     "intent": {"type": "string", "enum": ["writing_assist", "qa", "translation", "chitchat", "other"]},
     "topic": {"type": "string"},
     "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]}},
   "required": ["intent", "topic", "difficulty"], "additionalProperties": false}
user (few-shot 示例 1):
  [示例输入] NBA 总决赛什么时候开始
  [示例输出] {"intent": "qa", "topic": "体育赛事时间查询", "difficulty": "easy"}
user (当前记录):
  [待标注数据] 帮我写一条请假条，明天上午要去医院
```

LLM 响应文本经 M8（L1 直得平衡花括号子串，L2 一次通过，无 L3 修复）返回合法对象，M5 构造 `Annotation`（4.2 节）：

```
响应: {"intent": "writing_assist", "topic": "请假条代写", "difficulty": "easy"}

item.annotation = Annotation(
    output   = {"intent": "writing_assist", "topic": "请假条代写", "difficulty": "easy"},
    model    = "qwen2.5-vl-72b-instruct",       # llm.default 的 model
    attempts = 1,                               # 1 + 0 次 L3 修复
    usage    = Usage(prompt_tokens=312, completion_tokens=31))
```

#### ② UI 模态标注（§5.2 登录页工程）

提示词骨架与 ① 完全相同（system = §5.2 的 `annotate.instruction` + 规范化用户 Schema），差别仅在「当前记录」user 消息由两个 Part 组成（3.9.2）：`[屏幕截图]` 为 `kind="image"` 的 Part（`capture/2026-07-01/c/image_2.png`，M9 调用时缩放并 base64 编码）；`[UI 控件树]` 为 `kind="text"` 的 Part，内容即 `record.ui_tree.serialize(max_chars=30000)` 的输出——实例见 3.2.7，此处不重复。对 `capture/2026-07-01/b/uitree_2.jsonl` 该记录（id `9f2c31ab52e08d17`）的响应 JSON（即 §6.3 主输出行剥除 `_meta` 后的用户结构，`annotation.model / attempts` 与该行 `_meta.annotation` 一致）：

```
{"screen_category": "login",
 "page_title": "登录",
 "interactive_elements": [
   {"role": "EditText", "label": "请输入手机号",   "bounds": [72, 520, 1008, 664]},
   {"role": "EditText", "label": "请输入验证码",   "bounds": [72, 712, 672, 856]},
   {"role": "Button",   "label": "获取验证码",     "bounds": [704, 712, 1008, 856]},
   {"role": "Button",   "label": "登录",           "bounds": [72, 952, 1008, 1096]}],
 "description": "手机号+验证码登录页"}
```

### 3.5.5 帧级逐帧标注

`frame.annotate.enabled` 对完整成员逐帧产出符合有效帧 Schema 的 Annotation。process 的 member_annotations
以全会话整数出现位置作键；generation 以唯一字符串事件 ID 作键。结果按实际 members 顺序写入 `_meta.stream.members`。

```python
async def annotate_member(member: Record, ctx: RunContext, label: str | None = None,
                          target: CapacityTarget | None = None) -> Annotation | None: ...
async def annotate_member_leaf(member: Record, ctx: RunContext, label: str | None = None,
                               target: CapacityTarget | None = None) -> Annotation: ...
def build_frame_annotate_prompt(member: Record, cfg: ResolvedConfig, schema_text: str,
                                label: str | None = None) -> PromptBundle: ...
```

- 处理会话的单帧调用必须显式传实际 target，包含真实出现位置和帧 label；verify 回收传 expanded working item
  投影的位置，owner 仍是 verify。缺失 target 是接口错误，不能按 record.id 猜位置。
- 帧 pass 仅处理 active、sequence、首标签或无分类、非降格信封。处理流从序列标注成功后进入；生成 sequence
  在 annotate.enabled=false 时可直接进入，不构造序列 prompt。已存在的当前尝试字典只补缺位，不换对象。
- process 单帧正文/完整可见树/该图全量进入模型请求，不受 ui_tree_max_chars 帽；图片固定工作点不变。
  SchemaEngine 接收投影后的 model frame Schema；后处理再补代码字段并完整复验。帧调用不走普通 record validator，
  不计 record resolved_at，保持后处理规范的明确边界。
- enabled=false 的帧类跳过，不占键；普通非容量失败占键 None 并计 frame_annotate.failed，episode 可继续。
  生成 sequence 任一应标注帧失败拒绝整个 counterfactual set。成功帧占键 Annotation。
- 容量异常先发 frame 信号。单成员没有合法再拆边界：控制器登记 owning stage/profile/label/position 终态，
  下次调用前返回原帧失败产品，不裁树、不丢图、不重新喂模型；fixed 帧 Schema 仍是 frame 单位，不升级为整序列错误。
- multi 兄弟共享当前尝试帧字典，处理流中同内容 ID 的不同出现位置不共享一个键。重算从冻结上游重新生成帧产品。
  annotate.frame 继续按成员产生结构化事件；usage/Schema/trace 事实累计，dataset 计数只随最终会话提交。

### 3.5.6 v1.20 sequence annotation 时间

declared sequence class 可以在完整 annotation Schema 的标量叶子上写
`x-labelkit-business-time = true`，并以 `[class.<name>.annotate].time_bindings` 一一声明
`source = "first_resource_start_milliseconds"` 与目标 resource。M5 在最终 members 上恰构造一次冻结
`SequenceTemporalContext`；每个 member 只携带 event ID、Planner start、duration 与 resources，不携带 payload。
机械值是目标 resource 最早正区间的 start 毫秒。

首轮 sample、self-consistency vote、`annotate_record`、`annotate_record_leaf` 和 verify 返工都调用
`complete_finalized(FinalizedCallRequest)`：provider 与 L3 只接收剥离时间叶子的 model Schema；finalizer 在副本上注入
同一个 temporal context 的值后执行完整 Schema 与 L2.5。repair projector 从 previous output 删除仍可达的时间叶，
错误 parent 类型时不创建或替换 parent。调用有 time binding 却缺失或替换 context 是 internal contract error；
禁止从 `Record.raw`、generation provenance、payload 文本、wall clock 或导出器猜值。

该 binding 只存在于 generate-only sequence declared mode；普通 process、instruction-only、ordinary annotation 或
annotate disabled 配置已由 M1 拒绝。M11 只复验最终值，不新增或修复 annotation 时间。

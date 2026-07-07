## 3.5 M5 标注 annotate

### 3.5.1 职责与边界

**做：**为每条存活记录组装标注提示词（任务指令 + few-shot + 记录内容，UI 模态含截图与序列化树），经 M9 调用 LLM、经 M8 获得符合用户 Schema 的标注对象；可选 self-consistency 多次采样字段级投票（3.5.2）。 
**不做：**不校验结构（全部委托 M8）；不评审标注质量（M4/M7 职责）；不产出新记录（生成属 M6）；不做提示词内容的「智能改写」——提示词组装是确定性模板拼接。

### 3.5.2 标注提示词组装（确定性模板）

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

**按类取值（v1.7）。**classify 启用且记录带类标签时，本节模板的 `{annotate.instruction}` 与 few-shot `examples` 取该类有效配置（`class_views[label].annotate`，3.1.4 按类覆盖合并行）——模板结构不变，仅取值来源变化。为此 `build_annotate_prompt` 与 `annotate_record` 各增末位可选形参 `label: str | None = None`（默认 None = 现行为，旧调用点零改动）；stage 层传 `item.classification.label if item.classification else None`。trace `annotate.done` 事件 payload 增 `label` 字段（仅 classify 启用时携带，7.2 只增不改）。

**标注鲁棒性：self-consistency（可选，v1.2）。**`annotate.self_consistency = n`（默认 0 = 关；启用须 n ≥ 3 且为奇数，5.2）时，M5 对每条记录按本节模板独立采样 n 次（temperature 统一取 `annotate.sc_temperature`，默认 0.7——采样多样性的来源），每次输出都各自经 M8 走完整结构保证后才参与投票。**字段级投票**：enum / boolean / integer 字段逐字段取 n 个样本中的众数；自由文本 / 数组字段不逐字投票，取「与众数字段组合一致的样本」中第一个的对应字段值。其余类型字段（number、嵌套 object 等）与自由文本/数组同法处理（不逐字段投票，随众数字段组合整体取值）。全体分歧（众数组合不存在或无样本与其完全一致）时整体采用第一个样本，并计入 `report.annotate.sc_disagreements`。某次采样经 M8 修复仍失败（SchemaViolation）⇒ 该样本弃权、由其余合法样本投票（agreement_ratio 分母仍为 n）；n 次全部失败才置 `status="failed"`。`_meta.annotation.attempts` 记 n 次采样 attempts 之和。`_meta.annotation` 增 `sc = {n, agreement_ratio}`（agreement_ratio = 与最终众数字段组合完全一致的样本数 / n；6.3 只增字段）；trace `annotate.done` 事件 payload 增同构 `sc` 字段（7.2「只增不改」契约内扩展）。该机制对分类型 Schema 收益最大——如统一示例的 `intent` / `difficulty` 枚举字段：多路径采样 + 多数投票显著优于单次贪心解码（Self-Consistency，Wang et al., ICLR 2023 [33]，GSM9K +17.9%）。成本：标注调用与 token ×n。

### 3.5.3 API 与错误处理

```
class AnnotateStage(Stage):
    name = "annotate"
    async def run(self, batch, ctx) -> list[PipelineItem]:
        """对每条 active 记录: prompt = build_prompt(rec); item.annotation = await ctx.schema_engine
           .complete_validated(profile, prompt, user_schema)  # M8 全责保证结构
           SchemaViolation(不可修复) ⇒ item.status='failed', 错误入 item.errors。"""
```

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

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

**序列标注模板（v1.8，S5/S6/S28）。**stream 模式下序列信封（`record.kind = "sequence"`，3.14）的「当前记录」user 消息改走序列变体——system 与 few-shot 消息不变，**段序与步骤行格式逐字冻结**（CONTRACTS §10.1 序列变体），单条 user 消息内 Part 恰按此序：

```
① text part:  [动作序列]                    ← item.transitions 为 None 时整段省略
              {index}. {action_type}（对象: {target|—}；值: {value|—}）{description}
                                             ← 每 Transition 一行，index 升序；
                                               target/value 为 null 时渲染为字符「—」
② 每保留关键帧（关键帧序数 i/k，成员序数 m——标签显式携带成员序数）:
   text part:  [关键帧 {i}/{k}·成员 {m}]
   image part: member.image                  （M9 调用时编码，3.9.2）
③ text part:  [成员帧摘要]                  ← 恒在收尾段
              {全体成员逐帧 frame_digest（4.3），每成员一行、按成员序，总量有界}
```

**模板不变量（S6）：user 消息末 Part 恒为 ③ 恒在的 text 段**——M7 修复后缀（3.7.3）拼接在末 Part 的 text 之上，若消息以图收尾会静默产出 "None\n…" 并丢失末帧图；恒在收尾摘要段以零修复代码改动保证该不变量（repair 拼接路径不动）。

**关键帧降采样（S28）：**成员数 n > `annotate.sequence_frames` = k 时确定性均匀降采样 `idx_i = ⌊i·(n−1)/(k−1)⌋, i = 0..k−1`——首末帧恒含、严格递增无重复、纯整数零 rng（种子豁免面不变，2.6）；n ≤ k 取全量。k ∈ [2, 100] 与 `> 20 ∧ max_image_px > 2000` 联动警告由 M1 校验（3.1.4、5.2）。

**按碎片配额降采样（v1.9）：**stitch 启用且信封为多碎片线索（F ≥ 2 个碎片，3.16）时，上式升级为**按碎片配额**——全局均匀采样会把小碎片整段抽空（恢复段可能仅 2–3 帧，恰是缝合语义的关键证据），每碎片须至少保底 1 帧（T14）。确定性公式（纯整数零 rng；n_f = 碎片 f 的成员数，Σn_f = n > k ≥ F）：

```
配额:   q_f = 1 + base_f + tip_f                      # 每碎片保底 1 帧，Σ q_f = k
        base_f = ⌊(n_f − 1) · (k − F) / (n − F)⌋      # 剩余 k − F 个名额按 (n_f − 1) 加权
                                                      #   （保底帧已计入，按剩余成员数分摊）
        tip_f  ∈ {0, 1}: 余名额 (k−F) − Σ base_f 个，按余数 (n_f−1)·(k−F) mod (n−F)
                 降序逐碎片 +1（平局取碎片序小者）——最大余数法
碎片内: 以 q_f 对碎片成员局部套均匀公式（q_f ≥ 2 时碎片首末帧恒含；q_f = 1 取碎片首帧，
        唯一例外：末碎片 q_F = 1 时取其末帧），选中下标映射回线索成员元组
不变量: 全局首帧（经首碎片）与末帧（经末碎片）恒含；跨碎片严格递增无重复
退化:   碎片划分缺席 / 单碎片 / 与成员数不一致、或 k < F（保底不可行）⇒ 静默回退
        全局均匀公式（单碎片线索由此逐字节退化为 v1.8 行为——零变化回归锚）
```

**穿参义务（v1.9）：**碎片划分在信封 duck 标上而 `build_annotate_prompt` / `annotate_record` 只收 Record——两函数各增**第三个**末位可选形参 `fragment_lens: tuple[int, ...] | None = None`（= 线索各碎片的成员数，按碎片序；None = 现行为——继 v1.7 `label`、v1.8 `transitions` 之后对该冻结签名的第三次 additive 末位 kwarg 修订，旧调用点零改动）。stage 层自信封碎片跨度表取各碎片 member_count 传入；**M7 修复重标注调用同步穿参**（3.7.3——两处调用点一并穿参，否则修复重标丢配额、退回全局均匀采样）。

**transitions 形参（S5）：**`build_annotate_prompt` 与 `annotate_record` 各增末位可选形参 `transitions: tuple[Transition, ...] | None = None`——继 v1.7 `label` 之后对该冻结签名的**第二次** additive 末位 kwarg 修订（CONTRACTS §7.4；R2 同款构造：默认 None = 现行为，旧调用点零改动）。stage 层传 `item.transitions`；M7 修复路径在成员手术后传**重建值**（3.7.3）。self-consistency 与 L2.5 路径不动——序列记录的 L2.5 回调收到 `record = None`（`Record.raw` 对序列恒 None，4.1；文档声明的既有局限，富载荷形参列演进候选）。

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

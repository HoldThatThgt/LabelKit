# 第 11 章　标注算子 annotate：把你的意图翻译给模型

> annotate 是流水线的「主产出」工位：它把每条存活记录变成一个符合你 Schema 的标注对象。
> 本章讲提示词是如何组装的（这决定了你该怎么写 instruction 和 few-shot）、
> self-consistency 投票何时值得开，以及标注质量上不去时先查什么。

## 11.1 直觉：一个照模板干活的贴标员

annotate 做的事非常朴素：对每条 `active` 记录——

1. 按**固定模板**拼一段提示词（你的指令 + 你的示例 + 这条记录的内容）；
2. 调用 LLM；
3. 把输出交给结构引擎（第 14 章）验收，拿回一个**保证合法**的 JSON 对象，挂到记录上。

关键词是「固定模板」：LabelKit **不会**替你改写、润色、优化提示词——组装是确定性的字符串拼接。这是刻意的设计：提示词的每个字都来自你的配置，效果好坏完全可归因、可迭代。模型的自由发挥空间只存在于「按你的指令生成什么标注」，不存在于「收到什么提示词」。

## 11.2 提示词模板：逐字理解

```
system:
  {annotate.instruction}                        ← 你写的任务指令
  输出必须是符合以下 JSON Schema 的单个 JSON 对象，不输出任何其他内容：
  {user_schema_json}                            ← 你的输出 Schema（规范化后全文）

user (每条 few-shot 示例依次一条消息):
  [示例输入] {example.input}
  [示例输出] {example.output}

user (当前记录):
  文本模态: [待标注数据] {record.text}
  UI 模态:  [屏幕截图] <image: base64>          ← 截图（此刻才读盘、缩放、编码）
           [UI 控件树] {序列化树文本}            ← 见下
```

三个推论直接从模板长出来：

- **你的 Schema 就在提示词里**——字段名、枚举值、描述（`description`）模型都看得见。所以 **Schema 本身就是提示词的一部分**：枚举值取有意义的名字、给字段写 description，都会直接改善标注质量（第 14 章展开）。
- **few-shot 示例是独立的 user 消息**，出现在当前记录之前。示例的 `output` 在启动时就被校验必须通过你的 Schema——你不可能用一个非法示例教坏模型。
- **UI 模态是「图 + 结构文本」双通道**：模型同时看到截图和控件树的线性化文本（每节点一行：`角色 "文本" [边界框] {属性}`，只保留可见节点，超出 `input.ui_tree_max_chars` 深度优先截断）。这是 ScreenAI 等 GUI 理解工作的标准输入表示——图提供视觉语境，树提供精确的文本与坐标。

一份组装结果（第 3 章的意图标注工程，另加 1 条 few-shot 作演示——该工程本身未配 `examples`）：

```
system:
  你是输入法用户请求的标注员。根据用户输入的一句话请求，标注其意图类别
  （writing_assist 写作协助 / qa 问答 / translation 翻译 / chitchat 闲聊 / other 其他）、
  主题（简短名词短语）与完成该请求的难度（easy / medium / hard）。
  输出必须是符合以下 JSON Schema 的单个 JSON 对象，不输出任何其他内容：
  {"type": "object", "properties": {"intent": {"type": "string", "enum": [...]}, ...}}
user:
  [示例输入] NBA 总决赛什么时候开始
  [示例输出] {"intent": "qa", "topic": "体育赛事时间查询", "difficulty": "easy"}
user:
  [待标注数据] 帮我写一条请假条，明天上午要去医院复查，下午回来上班
```

## 11.3 配置参考

```toml
[annotate]
enabled = true                # 默认开
llm = "default"               # 用哪个 profile
instruction = """……"""        # 任务指令；enabled 时必填
examples = [                  # few-shot 示例，可选
  {input = "NBA 总决赛什么时候开始",
   output = {intent = "qa", topic = "体育赛事时间查询", difficulty = "easy"}},
]
self_consistency = 0          # 0 = 关；开启须 ≥3 的奇数
sc_temperature = 0.7          # self-consistency 各次采样的温度
```

| 键 | 默认 | 要点 |
|---|---|---|
| `llm` | `"default"` | UI 模态下该 profile 必须 `supports_vision = true`（启动校验） |
| `instruction` | 必填 | 见 11.4 的写作指南 |
| `examples` | `[]` | 每项 `{input, output}`；`output` 必须过用户 Schema（启动校验，错在启动时就报）。UI 模态下示例只有文本 input，不支持带图示例 |
| `self_consistency` | 0 | 见 11.5 |
| `sc_temperature` | 0.7 | 仅 self_consistency ≥ 3 时生效——多样性靠温度，投票靠多数 |

**按类覆盖（v1.7）**：开启分类算子后，`instruction` 与 `examples` 可以按类换一套——在 `[class.X.annotate]` 里为 X 类单独写任务指令与 few-shot 示例，该类记录标注时取类内值，未覆盖的类继承全局（类示例同样在启动时逐条过你的用户 Schema 校验）。提示词模板结构不变（11.2 的模板一字不动），变的只是往里填的内容。详见第 24 章。

## 11.4 instruction 写作指南

instruction 是你对贴标员的「岗前培训」。有效的写法：

**① 先给身份，再给判据，不要复述 Schema。**字段名和枚举值模型在 Schema 里看得到，指令里该写的是 Schema 里放不下的**判断标准**：

```toml
# 平庸：复述了一遍 Schema 就完了
instruction = "标注 intent、topic、difficulty 三个字段。"

# 有效：交代身份 + 每个字段的判据 + 边界情况怎么办
instruction = """
你是输入法用户请求的标注员。根据用户输入的一句话请求标注：
- 意图类别：writing_assist 指用户要求代写/改写文本；qa 指寻求事实性解答；
  translation 指翻译请求；chitchat 指无具体任务的闲聊；其余归 other。
  同时含多种意图时，取用户的主要诉求。
- 主题：简短名词短语，如"请假条代写"，不要整句复述。
- 难度：以一个通用大模型完成该请求的难度衡量——easy 一步可答；
  medium 需组织结构或多步推理；hard 需专业知识或创造性长文。
"""
```

**② 边界情况显式给规则。**「同时含多种意图取主要诉求」「无法判断时归 other」——这些话不写，模型就自己发明规则，而且每条记录发明得还不一样。

**③ 拿不准的判据，用 few-shot 钉死。**语言描述十遍不如一个例子：给 2–5 条**覆盖易混淆边界**的示例（比如一条 chitchat 与 qa 的边界例、一条 medium 与 hard 的边界例）。示例不是越多越好——它们进每次调用的提示词，是按 token 计费的常驻成本。

**④ 迭代姿势**：`--limit 20` 小样本跑 → 逐条看主输出 → 把标错的类型总结成新判据或新示例 → 重跑对比。改一版跑一版，每版只改一个变量。

## 11.5 self-consistency：用三次采样换一次放心

`self_consistency = n`（≥3 奇数）时，每条记录独立采样 n 次（温度用 `sc_temperature`，默认 0.7——**故意**不用 0，多样性正是投票的原料），然后**字段级投票**：

- 枚举 / 布尔 / 整数字段：逐字段取 n 个样本的众数；
- 自由文本 / 数组 / 嵌套对象：不逐字投票（三段大同小异的 topic 没法投），取「与众数字段组合一致的样本」中第一个的对应值；
- 全体分歧（不存在众数组合）：整体采用第一个样本，计入报告 `annotate.sc_disagreements`；
- 某次采样结构修复失败 ⇒ 该样本弃权，由其余合法样本投票；n 次全失败才判记录 `failed`。

产物里的痕迹：`_meta.annotation.sc = {n, agreement_ratio}`——agreement_ratio 是「与最终众数组合完全一致的样本占比」，批量看它能量化标注的稳定性。

**何时值得开**：输出以**分类字段为主**（枚举/布尔）、且下游对标签错误敏感时，n=3 的收益最明显（Self-Consistency 论文的多路径投票机制）；输出以自由文本为主时收益很小——投票机制对文本字段基本是摆设。**成本直白：调用与 token ×n。**先用单次标注把 instruction 调到位，最后才用 self-consistency 兜稳定性——它救不了写得含糊的指令。

## 11.6 失败与去向

- 结构修复预算耗尽 ⇒ 记录 `failed` 进拒绝通道（错误码 `schema_violation`；注册了 `output.validator` 回调且剩余违规全部来自回调时为 `callback_violation`，见 14.5）——**永远不会有非法结构或违反你业务规则的对象混进主输出**；
- API 重试耗尽 / 致命错误 ⇒ 同样 `failed`，错误码进拒绝通道的 `_meta.reason`（如 `provider_retryable_exhausted`），具体错误信息进 `errors` 列表；
- 成功 ⇒ `_meta.annotation = {model, attempts}`（开 self-consistency 时另含 `sc`）。单次标注（`self_consistency = 0`）时 attempts = 1 + 结构修复轮数，>1 即说明结构引擎修过；开 self-consistency 时 attempts 是各合法样本尝试次数之**和**（零修复时就等于合法样本数），要与 `sc.n` 对照：attempts 明显超过 n 才说明发生过修复（第 14 章解读修复分布）。

## 11.7 标注质量上不去，按序排查

1. **先看 rejects 和结构修复信号**——单次标注看 `annotation.attempts`（>1 即修过）；开 self-consistency 时 attempts 是多样本之和，改看报告 `schema_engine.resolved_at` 的 `l3_*` 分布或 trace 的 `schema.repair` 事件。结构频繁修复说明 Schema 对模型不友好（第 14 章），先修 Schema 再怪指令；
2. **抽 20 条人工比对**——错误集中在某个枚举边界？加判据/加示例（11.4）；错误随机散布？考虑换更强的模型或开 self-consistency；
3. **UI 模态看图**——`max_image_px` 是不是缩太狠导致小字不可读（第 6 章）；树是不是被 `ui_tree_max_chars` 截断了关键节点（trace 的序列化文本里找 `…(truncated`）；
4. **开 verify 兜底**——标注质量的最后一道闸是独立评审（第 13 章），它能把「指令遵循错误」拦在主输出之外。

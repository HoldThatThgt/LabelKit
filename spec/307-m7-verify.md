## 3.7 M7 二次校验 verify

### 3.7.1 职责与边界

**做：**用独立 judge profile 对每条 (记录, 标注) 评审：输出 verdict（pass/fail）+ 逐项批评意见；fail 时按策略丢弃，或将批评意见回喂 M5 重新标注（有界修复环）。 
**不做：**不自己改写标注（修复 = M5 重标注 + M8 重校验，M7 只供给批评意见）；不评审结构合法性（到达此处的标注必已合法）；不做打分（M4 职责）。

### 3.7.2 评审调用

```
system: 你是标注质量审核员。给定任务指令、原始数据与标注结果，独立判断标注是否合格。
        评审维度: ① 是否遵循任务指令 ② 与原始数据的事实一致性 ③ 字段语义是否正确填写
        {verify.extra_criteria}                       # 可选，用户追加维度
        先逐维度给出简短意见，再给结论。
user:   [任务指令] {annotate.instruction}
        [原始数据] {record 内容，UI 模态含截图+树}
        [标注结果] {annotation.output 的 JSON}
输出(经 M8 校验): {"critiques": [{"aspect": str, "opinion": str}], "verdict": "pass"|"fail"}
```

「先意见后结论」的顺序固定，利用自回归生成让结论以意见为条件（chain-of-thought 评审，Zheng et al. [20]）。judge profile 应配置为与标注 profile 不同的模型（自我评审存在自增强偏差 [20]），M1 在两 profile 的 model 字段相同时打印 warning（不阻断）。

**按类取值（v1.7）。**classify 启用且记录带类标签时，本节模板的 `[任务指令]` 段与 `{verify.extra_criteria}` 均取该类有效值（分别为 `class_views[label]` 的 annotate.instruction 与 verify.extra_criteria，3.1.4 按类覆盖合并行）——按类标注配全局评审指令是语义错位，故两处同步取类值。`build_verify_prompt` 增 `label` 形参，`_judge_round` / `_reannotate` 透传（repair 重标注调 `annotate_record(..., label=...)`，3.5.2 按类取值段）；policy / max_repair_rounds / llm / judges 恒为全局（5.2 按类覆盖白名单表）。trace `verify.verdict` 事件 payload 增 `label` 字段（仅 classify 启用时携带，7.2 只增不改）。

**多评审团（可选，v1.2）：**`verify.judges`（array，默认 `[]`，与 `quality.judges` 语义一致）非空时启用评审团：空 = 单评审走 `verify.llm`，本节既有行为完全不变；非空须为**奇数个** profile 引用（M1 校验，不满足报错退出码 2）。各 judge 按本节同一模板**各自独立**评审（互不可见对方意见），最终 `verdict` 取多数票；各方 `critiques` 全部合并保留进 `VerificationResult.critiques`（4.2），每条标注来源——条目增加 `judge` 字段（= profile 名）。trace 事件 `verify.verdict` 相应改为**每 judge 一条**，payload 新增 `judge` 字段（字段只增不改，7.2 事件契约向后兼容）。`policy = "repair"` 回喂 M5 时，[审核意见] 段 = 全部投 fail 的 judge 的 critiques 合并（各条前缀来源 judge 名）。成本为单评审的 |judges| 倍，宜配置 3 个异构小模型 profile 而非加倍调用同一大模型。**背书：**多个较小模型组成的评审团（PoLL）在三种评审设置、六个数据集上优于单一大模型评审，因跨模型家族的多样性显著降低单模型自增强偏差，且成本比单一大评审低 7 倍以上（Verga et al. [32]）——与本节「judge 独立于标注模型」是同一去偏原则的推广。

**stream 序列评审与缺陷表（v1.8，S7）。**stream 模式下序列信封（episode，`record.kind = "sequence"`，3.14）的评审改走序列变体；**非 stream 路径零改动**——本节既有模板与评审 Schema 是回归锚，序列信封由 stage 层旁路驱动器承载。输出经 `schema_engine.defect_verdict_schema()` 校验（3.8.1 内部 Schema 清单；与既有评审 Schema **并存**，S7）：三顶键 `{critiques, defects, verdict}` **全 required**（意见/缺陷在前、结论在后——本节「先意见后结论」同理）；`critiques` 形态与既有评审 Schema 逐字节一致（原样走既有合并/回喂链路）；`defects` 逐项 `{kind, members, position, detail}` 四子键全 required，可选性以可空联合 `["array","null"]` / `["string","null"]` 表达（OpenAI strict 兼容，3.8.1）。缺陷 `kind` 六值封闭词表（v1.8 五值 + v1.9 增 `wrong_stitch`——defect Schema、DEFECT_KINDS、report `_DEFECT_KINDS`、`_route_defects` 四处同步扩值，3.8.1/6.4）：

| kind | 语义 |
|---|---|
| `label_mismatch` | 标注的任务标签与序列证据不符。 |
| `off_task_members` | 段内混入与任务无关的成员帧（`members` 列出这些成员帧 id）。 |
| `missing_head` / `missing_tail` | 段首缺少任务起点帧 / 段尾缺少任务终点帧（结合边界余量判断）。 |
| `missing_members` | 段中缺失成员帧（`members` 列出可指认的帧 id，无从指认则为 null）。 |
| `wrong_stitch` | v1.9：线索的某处缝合是错误的——某碎片与线索其余部分不属同一目标导向任务（结合 `[片段结构]` 判断；`position` 指向可疑碎片的线索内序数）。仅 stitch 启用时可判（3.16）。 |

评审证据为单条 user 消息**七段序**（v1.8 为六段，v1.9 插入 `[片段结构]`；system 含六类缺陷说明（v1.9 起）；全文逐字冻结于 CONTRACTS §10.5 序列变体）：`[任务指令]`（classify 启用时取类有效值，同本节按类取值段）→ `[动作序列]`（`item.transitions` 按 3.5.2 步骤行格式渲染，接缝占位步带 thread_seam 后缀（3.4.3 序列行同款）；transitions 为 None 时整段省略）→ `[片段结构]`（v1.9，**仅 stitch 启用时在场**——关闭时整段省略、退回六段形态即 v1.8 回归锚：每碎片一行「线索内序数 / 帧跨度 / 首帧摘要」+ 接缝位置表（`seam_indexes` 的文字化）；无此节 `wrong_stitch` 不可判，3.16）→ `[边界余量]`（段边界外前后 **k = 2** 帧的 `frame_digest`（4.3）及其去向标注：noise / 相邻段序数 / 无——防切头切尾的证据段，零额外 LLM 调用，语音端点检测 hangover 惯例的移植 [54]；多碎片线索的边界 = 线索两端，维持**首碎片头 / 尾碎片尾**邻帧（接缝不是边界，v1.9）；「相邻段序数」的会话内清单**过滤 `status="stitched"` 壳**（实现 `_session_episodes`，壳非产出单元、不得占用「第 n 段」序数，v1.9））→ `[首帧截图]` → `[末帧截图]`（judge profile 须 supports_vision，3.1.4 vision 逐阶段表）→ `[标注结果]`。产物增量：`VerificationResult` 增 additive 字段 `defects`（4.2，非 stream 恒 `()`）；`_meta.verification` 在 stream 模式下携带恒在 `defects` 键（无缺陷 = []，6.3）；缺陷摘要随 `verify.verdict` 事件 payload（受 trace.content 分级，7.4，S31）。`verdict = "fail"` 而 defects 为空数组 ⇒ 代码侧归一化为一条默认 `label_mismatch` 缺陷（S7——修复路由建立在缺陷表之上，fail 必有路由依据）。

**上下文预算装填（v1.11）。**评审 profile 声明 `context_window` 时按上下文预算装填评审调用（未声明 = 预算关闭，行为与 v1.10 一致；预算/估算/校准机制见 3.9）：记录侧可裁份额 = 单记录 UI 树渲染动态封顶（`min(input.ui_tree_max_chars, 预算折算字符)`，marker 与绝对上限语义同 3.13.4）与序列 `[动作序列]` 步骤行块的「首末步恒保留、丢中段整行 + 原位标记」裁剪（V9）。**多评审团共用单 prompt**（本节多评审团行与 stream 序列评审均为一次构建广播全团）⇒ 记录侧份额按评审团**最小 `input_budget`** 装填（V25②；对照：quality pairwise 逐 (对, 评审) 各自构建、按本评审预算装填，3.4.3）。**不可裁剪动态块（V25③）**：`[标注结果]` 的标注 JSON 为 per-record 语义资产——**计入 est、永不裁剪**；全部可裁份额耗尽仍超 → 该记录记 `context_overflow` 入 rejects（V10，7.6）。逐裁剪点计入 `report.budget.truncations`（6.4）。

### 3.7.3 失败策略与修复环

| 策略 | 行为 |
|---|---|
| `verify.policy = "drop"`（默认） | fail ⇒ `status="dropped_verify"`，批评意见摘要入 `_meta.verification` 与 rejects 通道。 |
| `verify.policy = "repair"` | fail ⇒ 将批评意见追加进标注提示词（`[上一版标注] ... [审核意见] ... 请修正后重新输出`），M5 重标注、M8 重校验、M7 重评审；最多 `verify.max_repair_rounds`（默认 1）轮，仍 fail 按 drop 处理。评审轮数记入 `_meta.verification.rounds`（含首评，一次通过 =1；修复后复评 =2），各轮意见按序累积于 `VerificationResult.critiques`（4.2），实例见 3.7.4。 |

**stream 修复路由：两阶段批级成员手术（v1.8，S8/S31）。**`policy = "repair"` 下序列信封的修复不止重标注——按缺陷表（3.7.2）路由三类动作：**标签重标**（label_mismatch：常规批评意见回喂重标注）、**成员收缩**（off_task_members）与**成员回收**（missing_head / missing_tail / missing_members）。为保证并发 gather 下的确定性（相邻 episode 争抢同一噪声帧、multi 兄弟互撕共享成员集），每轮修复为**两阶段批级结构**（classify 扇出「先同步后并发」先例）：

1. **并发评审**全部待审 episode；
2. **同步按批位置序执行全部成员手术**（「先到先得」变为确定性「位次得」）——**收缩**：`defect.members` 指认帧 `absorbed → dropped_noise` + duck-typed `off_task_member` 标（rejects 归因 stage="verify"、reason="off_task_member"，3.11.2）；**回收**（三级判定）：同 `session_id` 的批内 `dropped_noise` 噪声池帧经 `segment.judge_window` 直调复裁（3.14.3；relation ∈ {continues, advances} 即回收 `dropped_noise → absorbed`、按序键插回 `members`）→ 缺帧在相邻 episode 手中：**只标记、不跨段夺帧**（boundary_flags 计数）→ 无处可寻：缺陷条目增顶层键 `suspected = "capture_gap"`（代码侧标注，非 LLM 输出——`detail` 在 Schema 中为字符串，故 suspected 以兄弟键落在缺陷条目上；所属会话曾被 batch_size 硬切的帧改标 `"session_split"`——判定依据即 `_meta.stream.session_split`，S21，3.10.3）；
3. **并发接缝重摘取**：手术触点经 `extract.extract_transition` 直调重摘（3.15.3；1–2 次/手术，重建 Transition 带 `detail.reseamed = true` 溯源）；
4. **同步重建**：record 以新成员集重建（替换 `members`；序列 **id 不重算**，3.14.4 拼装行）、transitions 重编号——`Transition.index` 恒 = 元组下标、不变量 `len(transitions) = len(members) − 1` 恒真（4.2，S31）；
5. **并发重标注与复审**：`annotate_record(..., transitions=重建值)`（3.5.2 transitions 形参段）→ 下一轮评审。

**wrong_stitch 路由（v1.9，独立分支）**：**只标记、不拆线**——自动拆线手术是 v1.9 非目标（8.1），本缺陷不进上述三类手术路由，尤其**不得落入 missing_\* 的噪声池回收扫描**（错缝的修复方向是移除碎片而非补帧，回收扫描会反向加重错缝）；repair 轮内不为其执行任何成员手术（重标注亦不能修复错缝），持续 fail 按 drop 收尾（`dropped_verify`——错缝线索 fail-closed 不入主输出）；计数入 `verify.defects.wrong_stitch`（6.4）。成员手术的回收扫描语义不变（异线索 absorbed 帧按既有 D5 邻域判定已是 neighbor mark-only，缝合不改变其结论）。

修复轮数计入 `verify.max_repair_rounds`（含首评，与本节非 stream 语义一致）。状态改写授权：手术在 `absorbed` 与 `dropped_noise` 间**双向**改写成员信封状态——4.3 契约 ②b 的 M7 修复路径豁免（契约①的唯一反向豁免），**禁止翻回 `active`**（帧与其 episode 不得双写主输出）。其余裁决：multi 扇出克隆兄弟的 membership 类手术**只标记**——仅原信封（首标签）可执行（S8，3.13.4 multi × episode 行）；多评审团下 defects = 投 fail 的 judge 的**并集**，按 (kind 枚举序, position, members) 确定性去重排序，同成员的互斥手术取先序（S31）；修复后**不重打分**——沿用修复前质量分 + `_meta.stream.repaired = true` 标记（6.3；multi 下亦用于消歧同 id 兄弟行）。观测面（M7 属主，`report.stream.verify` 子块，6.4）：`verify.membership_repairs`（执行的手术数）、`verify.boundary_flags`（只标记的边界判定数）、`verify.defects.<kind>`（逐缺陷类型计数）。

**修复路径与上下文预算的交互（v1.11）。**① **升级触发（V21）**：`verdict = "fail"` ∧ `policy = "repair"` 是修复重标注质量阶梯换档（关键帧减半 + 分辨率上探 ≤ `max_image_px`，3.5.2 v1.11 段）的**唯一**触发面——升级只发生在修复路径、每记录 ≤ `verify.max_repair_rounds` 次，阶梯参数经 `annotate_record` 追加尾参传入（F3）。② **回收复裁的静态保证（V9/F14）**：成员回收的固定 [前成员, 候选, 后成员] 三帧复裁窗（直调 `segment.judge_window`）由 M1 预算护栏静态覆盖——`w_min` 护栏下限 `floor = 3` **仅当** `verify.enabled ∧ verify.policy = "repair" ∧ segment.enabled`（`policy = "drop"` 不构造复裁窗、不做三帧静态要求），`w_min < floor` → CONFIG_ERROR（3.1.4、3.9），由此保证修复路径运行期复裁永不 `context_overflow`。③ 缺陷词表与预算无交互：`wrong_stitch` 的无条件闭合词表语义不变（3.7.2 四处同步闭集，stitch off 亦在场）。

**背书：**LLM-as-a-Judge 的可靠性、偏差类型（位置/冗长/自增强）与缓解手段出自 Zheng et al.（NeurIPS 2023）[20]；「批评意见回喂原模型迭代修正」是 Self-Refine（NeurIPS 2023）的 FEEDBACK→REFINE 循环 [21]，有界轮数与其停机设定一致；批评-修订两阶段结构同 Constitutional AI [22]。GUI-360 以同构的「LLM 质量过滤」环节筛选 GUI 轨迹数据 [14]。

### 3.7.4 输入 / 输出示例

沿用全文文本模态贯穿示例（输入法中文指令意图标注工程，`input.text_field = "instruction"`）。配置：`verify.enabled = true`、`verify.llm = "judge"`、`verify.policy = "repair"`、`verify.max_repair_rounds = 1`（默认）、`verify.extra_criteria = ""`（默认，未追加维度）。记录 `id = "1cda030abc565f17"`，原始行 `{"instruction": "帮我写一条请假条，明天上午要去医院", "source": "ime-log", "ts": "2026-06-30T10:12:00Z"}`，M5 首版标注（已过用户 Schema）为 `{"intent": "writing_assist", "topic": "请假条写作", "difficulty": "easy"}`。

#### ① 首次评审调用（第 1 轮）

按 3.7.2 模板组装，judge 走 `[llm.judge]` profile（claude-sonnet-5，独立于标注模型）：

```
system: 你是标注质量审核员。给定任务指令、原始数据与标注结果，独立判断标注是否合格。
        评审维度: ① 是否遵循任务指令 ② 与原始数据的事实一致性 ③ 字段语义是否正确填写
        先逐维度给出简短意见，再给结论。        # extra_criteria 为空，无追加行
user:   [任务指令] 你是输入法中文指令的意图标注员。判断每条用户指令的意图类别（intent）、
        主题（topic）与完成难度（difficulty）。
        [原始数据] 帮我写一条请假条，明天上午要去医院        # 文本模态 = record.text
        [标注结果] {"intent": "writing_assist", "topic": "请假条写作", "difficulty": "easy"}
```

judge 响应（经 M8 按评审内部 Schema 校验合法）：

```
{"critiques": [{"aspect": "字段语义",
                "opinion": "difficulty 标为 easy，但该指令涉及正式文书格式与措辞得体性，应为 medium"}],
 "verdict": "fail"}
```

#### ② 修复轮：批评意见回喂 M5

`verdict = "fail"` 且 `policy = "repair"`、已用修复轮数 0 < `max_repair_rounds` = 1，触发修复。按 3.7.3 格式将下述片段追加进 3.5.2 组装的标注提示词末尾（system / few-shot / 当前记录各段与首次标注调用逐字相同）：

```
[上一版标注] {"intent": "writing_assist", "topic": "请假条写作", "difficulty": "easy"}
[审核意见] 字段语义: difficulty 标为 easy，但该指令涉及正式文书格式与措辞得体性，应为 medium
请修正后重新输出
```

M5（`[llm.default]`，qwen2.5-vl-72b-instruct）重新输出，经 M8 通过用户 Schema（L0 直出即合法，`attempts = 1`）：

```
{"intent": "writing_assist", "topic": "请假条写作", "difficulty": "medium"}
```

#### ③ 二次评审（第 2 轮）

以修正版标注按 ① 相同模板重新组装（仅 `[标注结果]` 段更换），judge 响应：

```
{"critiques": [{"aspect": "字段语义",
                "opinion": "difficulty = medium 与正式文书的格式及措辞要求相符，intent 与 topic 填写正确"}],
 "verdict": "pass"}
```

`verdict = "pass"`，记录保持 `status = "active"`，流转至 M11 写出。

#### ④ 最终结果对象

`PipelineItem.verification`（4.2 `VerificationResult`；`critiques` 为各评审轮意见按轮次顺序累积）：

```
VerificationResult(
  verdict   = "pass",
  rounds    = 2,    # 评审轮数：首评 fail + 修复后复评 pass；一次通过时为 1（对照 6.3 示例）
  critiques = ({"aspect": "字段语义",
                "opinion": "difficulty 标为 easy，但该指令涉及正式文书格式与措辞得体性，应为 medium"},
               {"aspect": "字段语义",
                "opinion": "difficulty = medium 与正式文书的格式及措辞要求相符，intent 与 topic 填写正确"}))
```

主输出行中 `_meta` 的相关片段（形态见 6.3）：

```
"_meta": {
  "id": "1cda030abc565f17", ...,
  "annotation":   {"model": "qwen2.5-vl-72b-instruct", "attempts": 1},   // 修复轮的 M5 输出，结构一次合法
  "verification": {"verdict": "pass", "rounds": 2}
}
```

**对照分支：**若二次评审仍 fail，此时已达 `max_repair_rounds`（默认 1），按 drop 收尾——`status = "dropped_verify"`，批评意见摘要入 `_meta.verification` 与 rejects 通道（3.7.3）；rejects 行（`output.rejects = "refs"`）不含数据内容本体。

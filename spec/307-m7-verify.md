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

### 3.7.3 失败策略与修复环

| 策略 | 行为 |
|---|---|
| `verify.policy = "drop"`（默认） | fail ⇒ `status="dropped_verify"`，批评意见摘要入 `_meta.verification` 与 rejects 通道。 |
| `verify.policy = "repair"` | fail ⇒ 将批评意见追加进标注提示词（`[上一版标注] ... [审核意见] ... 请修正后重新输出`），M5 重标注、M8 重校验、M7 重评审；最多 `verify.max_repair_rounds`（默认 1）轮，仍 fail 按 drop 处理。评审轮数记入 `_meta.verification.rounds`（含首评，一次通过 =1；修复后复评 =2），各轮意见按序累积于 `VerificationResult.critiques`（4.2），实例见 3.7.4。 |

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

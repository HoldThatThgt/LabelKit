# 第 14 章　结构引擎：四层防线与 Schema 编写指南

> 结构引擎（schema engine）不是一个可开关的算子，而是所有 LLM 输出的**总验收台**——
> 标注、打分裁决、评审结论、生成样本，统统要过它的手。
> 本章讲四层防线如何兜住不守规矩的模型输出，以及怎样写一份「模型容易答对」的 JSON Schema。

## 14.1 直觉：不可信输出的层层安检

第 1 章立过铁律：**LLM 的输出不可信**。它可能——

```
```json                        ← 套了 Markdown 围栏
{
  "intent": "writing",         ← 枚举值答错（应为 writing_assist）
  "topic": "请假条写作",
  "difficulty": "easy",        ← 多了个尾逗号
}
```
```

结构引擎的职责：无论模型输出多不像话，**要么**交还一个通过你 Schema 校验的对象，**要么**明确宣告失败（该记录进拒绝通道）——绝无第三种结局。主输出里每一行都必然走过校验通过的分支，这是机制而非概率。

四层防线，从便宜到贵：

| 层 | 名字 | 干什么 | 成本 |
|---|---|---|---|
| **L0** | 供应商原生结构化输出 | 请求时就把 Schema 塞给 API，让模型「戴着镣铐生成」 | 零（一个请求参数） |
| **L1** | 确定性修复 | 纯代码修文本：剥围栏 → 取花括号平衡子串 → `json_repair` 修尾逗号/单引号/截断 | 零 |
| **L2** | jsonschema 校验 | `Draft 2020-12` 全量校验，收集**全部**违规（不是只报第一条） | 零 |
| **L3** | 有界 LLM 修复环 | 把原始输出 + 违规清单发回给模型：「只输出修正后的 JSON」 | 每轮一次调用 |

流转规则：L0 只是让 L1/L3 少触发的优化——**它不豁免 L2**（各家供应商的 Schema 特性覆盖都有缺口，校验永远执行）。L1 修完过 L2；不过就进 L3；L3 每轮修复的输出重走 L1→L2；`output.max_repair_attempts`（默认 2）轮耗尽仍不过 ⇒ 该记录 `failed`（错误码 `schema_violation`）进拒绝通道。

## 14.2 一次真实的抢救过程

上面那段三重问题的输出，引擎是这么救的：

1. **L1**：剥掉 ` ```json ` 围栏 → 取平衡花括号子串 → `json_repair` 修掉尾逗号。得到可解析对象——注意 `"intent": "writing"` 的枚举错误 L1 管不了（它只管「能不能解析」，不看 Schema）；
2. **L2**：校验发现 1 条违规：`/intent: 期望为枚举 ["writing_assist", "qa", "translation", "chitchat", "other"] 之一，实际值为 "writing"` ⇒ 进 L3；
3. **L3 第 1 轮**：向 `repair_llm`（默认同调用方 profile）发一条修复消息（[违规清单] 就是 L2 渲染出的违规原文，只加序号、不改写）：

   ```
   [原始输出]
   （原文全文照贴）

   [违规清单]
   1. /intent: 期望为枚举 ["writing_assist", "qa", "translation", "chitchat", "other"] 之一，实际值为 "writing"

   只输出修正后的 JSON。
   ```

4. 修复响应 `{"intent": "writing_assist", ...}` 重走 L1→L2 ⇒ 通过。记录的 `_meta.annotation.attempts = 2`（1 次原始 + 1 次修复），报告里 `resolved_at.l3_1` 计 1。

## 14.3 读懂修复分布：resolved_at

报告的 `schema_engine.resolved_at` 是模型「结构纪律」的体检单（仅统计**用户 Schema** 的标注调用；裁决/评审/生成等内部结构不计入）：

```json
"schema_engine": {"resolved_at": {"l0_or_clean": 4141, "l1": 87, "l3_1": 30, "l3_2": 3, "rejected": 9}}
```

| 桶 | 含义 | 健康线 |
|---|---|---|
| `l0_or_clean` | 一次到位（原生结构化输出或本来就干净） | 绝大多数 |
| `l1` | 代码修复就够了（围栏/尾逗号级别的小毛病） | 几个百分点很正常 |
| `l3_1` / `l3_2` | 花了 1/2 轮 LLM 修复 | 合计 >5% 该警惕：Schema 对模型不友好 |
| `rejected` | 修不好，进拒绝通道 | 接近 0 |

`l3_*` 高企的第一反应**不是**调大 `max_repair_attempts`（那是花钱掩盖问题），而是：① Schema 能简化吗（见 14.4）？② profile 能开 `supports_structured_output` 吗？③ `max_output_tokens` 是不是太小导致输出被截断（截断的 JSON 是 L1 最常见的客户）？

## 14.4 Schema 编写指南：让模型容易答对

你的 Schema 会**逐字出现在提示词里**（第 11 章），它同时是「合同」和「说明书」。写得好，L0/L1 直通；写得差，每条记录都在 L3 烧钱。

**① 枚举值取自解释的名字。**模型看得懂 `writing_assist`，看不懂 `type_3`。枚举本身就是最强的提示。

**② 给字段写 `description`。**它出现在提示词里，等于给每个字段配了内联判据：

```json
"difficulty": {
  "type": "string", "enum": ["easy", "medium", "hard"],
  "description": "以通用大模型完成该请求的难度衡量：easy 一步可答；medium 需多步推理；hard 需专业知识"
}
```

**③ `required` + `additionalProperties: false` 是标配。**前者防漏字段，后者防模型自作主张加字段——两者都让错误在 L2 被精确定位，而不是静默混进输出。

**④ 结构越平越稳。**三层嵌套 + 数组套对象的 Schema，L3 触发率显著高于平铺结构。问自己：这层嵌套是下游真需要，还是顺手画的？能拆成两个顶层字段就别嵌套。

**⑤ 约束要「模型可感知」。**`maxLength: 200` 这类数值约束，模型经常越界（它不数字数）——要么放宽，要么在 instruction 里用自然语言强调「一句话、50 字以内」。`pattern` 正则约束同理，慎用。

**⑥ 需要推理的任务，给个 reasoning 字段。**严格格式约束可能压缩模型的思考空间（"Let Me Speak Freely?" 的实测结论）。缓解：在 Schema 里加一个 `reasoning` 字段**放在结论字段之前**，让模型先说理再作答，下游忽略该字段即可。（LabelKit 自己的内部 Schema 大多也这么设计：评审结论的 critiques 在 verdict 之前、pointwise 评分的 reason 在 score 之前；例外是成对裁决——其 reason 仅在 `quality.judgment_reasons` 生效时才有，且位于 winner 之后。）

**⑦ 版本纪律。**Schema 必须是合法 draft 2020-12、顶层 object、不声明 `_meta`。启动时元校验，写错退出码 2，不会浪费一次调用。

## 14.5 配置参考

```toml
[output]
schema_path = "./schema.json"     # 与 schema_inline 恰好二选一
# schema_inline = """{...}"""
max_repair_attempts = 2           # L3 轮数预算
# repair_llm = "fixer"           # 省略此键 = 同调用方；可指定便宜小模型专职修 JSON
                                  # （注意：显式写空字符串会报配置错误，退出码 2）
```

`repair_llm` 的使用时机：主力模型很贵、而 `l3_*` 又降不下来时，把修复外包给小模型——修 JSON 不需要智力，需要的是服从。注意小模型也要能把你的 Schema 看明白，太复杂的 Schema 外包修复反而修不动。

## 14.6 内部结构也走同一个引擎

一个容易忽略的事实：不只你的标注 Schema，**LabelKit 自己的内部输出**——质量裁决 `{"judgments": [...]}`、pointwise 评分、评审结论 `{"critiques": [...], "verdict"}`、生成样本 `{"samples": [...]}`——全部经由同一个 `complete_validated()` 入口、同一套四层防线。所以：

- 裁决输出偶尔非法不会炸：修不好按平局计（`judgment_invalid`，对 BT 中性），计入 `report.quality.judgment_failures`；
- 内部修复调用同样计入 token 计量与 `llm.call` trace 事件——账一分不少。

这就是「LLM 输出不可信」原则的完整落地：**没有任何一条 LLM 文本能绕过校验进入任何下游**。

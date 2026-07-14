## 3.8 M8 结构引擎 schema-engine

### 3.8.1 职责与边界

**做：**持有经预校验的用户 Schema 与各内部小 Schema（裁决、评分、评审、生成输出；v1.7 增分类 `classification_schema(class_names, assignment, max_labels, with_reason)`——按 `classify.assignment` 二态、类名词表以 enum 硬约束，关键字集 ⊆ 既有内部 Schema 关键字集且**无 uniqueItems**：该关键字会被 OpenAI strict 模式与部分约束解码网关硬拒，重复标签由 classify 代码在 M8 验证后确定性归一化，全文见 3.13.3；**v1.8 增三项**——分段窗口 `segment_window_schema(frame_count, with_reason)`（M14，全文见 3.14.3）、动作 `action_schema()`（M15，11 值动作词表 enum 硬约束，全文见 3.15.3）、stream 缺陷评审 `defect_verdict_schema()`（M7 stream 分支，三顶键 `{critiques, defects, verdict}` + 五值缺陷词表，全文见 3.7.2）。三者逐字 JSON 冻结于 CONTRACTS §10.7，规则同族：关键字集 ⊆ 既有内部 Schema 关键字集、同样**无 uniqueItems**（重复 index / 标签由调用方代码在 M8 验证后确定性收窄——3.14.4 的 first-wins 建表、3.13.4 的归一化行）；可选性一律以可空联合 type 数组 `["array","null"]` / `["string","null"]` 表达、**全键 required**（OpenAI strict 模式硬拒可选属性，L0 无条件透传 Schema）；`minItems = maxItems` 钉死窗口数组长度（judgment_schema 同款）。`defect_verdict_schema` 与既有评审 Schema **并存**——非 stream 评审路径继续用后者（回归锚，S7）。三者与其余内部 Schema 同级：不计入 `report.schema_engine.resolved_at`、不经过 L2.5）；提供「LLM 调用 → 合法 JSON 对象」的唯一入口 `complete_validated()`，内部实现四层结构保证；统计各层修复命中率。 
**不做：**不组装业务提示词（调用方传入完整 prompt）；不解释业务语义；不放行任何未通过校验的对象——这是它对全系统的硬契约。

### 3.8.2 四层保证与修复环

图 3-3 结构引擎四层保证。任何写入主输出的对象必然经过 L2 通过分支。

| 层 | 精确行为 |
|---|---|
| L0 | profile `supports_structured_output=true` 时：OpenAI 兼容 provider 传 `response_format={"type":"json_schema", "json_schema":{...strict:true}}`；Anthropic provider 以单工具 `tool_choice` 强制工具调用、Schema 作为工具入参。L0 只是「使 L1/L3 少触发」的优化，不豁免 L2——供应商实现存在覆盖缺口（JSONSchemaBench 实测各引擎均有不支持的 Schema 特性 [24]），校验永远执行。 |
| L1 | 顺序执行：① 剥离 Markdown 代码围栏；② 取首个花括号平衡子串；③ `json_repair.loads()`（工业库，处理截断/单引号/尾逗号/裸换行 [8]）。全部失败 ⇒ 直接进 L3。L1 为纯函数，无副作用、可单测穷举。 |
| L2 | `Draft202012Validator.iter_errors()` 收集全部违规（非首个），每条含 JSON Pointer 路径、期望与实际。通过 ⇒ 返回；未通过 ⇒ L3。 |
| L2.5（v1.5，可选） | `output.validator` 配置时、且仅对用户 Schema 调用：L2 通过后执行用户回调 `fn(obj, record)`。返回非空违规列表 ⇒ 违规以 `(validator) <消息>` 形式并入违规清单、与 Schema 违规同路进入 L3 修复环（回调意见回喂模型自我修正——回调既是门卫也是修复环的教练）；返回空 ⇒ 通过。L3 每轮修复输出重走 L1→L2→L2.5。预算耗尽且剩余违规**全部**来自回调 ⇒ `SchemaViolation(callback_only=True)`，记录 kind = `callback_violation`（7.6），否则仍为 `schema_violation`。回调抛异常不吞：向上传播、按记录级 `internal_error` 收敛（3.5.3）。内部 Schema（裁决/评分/评审/生成/分类（v1.7）/分段窗口/动作/缺陷评审（v1.8））不经过 L2.5。 |
| L3 | 修复提示词 = 单条 user 消息，按 `[原始输出]` / `[违规清单]` 分节标签组织，末尾指令「只输出修正后的 JSON」（逐字实例见 3.8.4）。使用 `output.repair_llm`（默认同调用方 profile）。每次修复输出重走 L1→L2。尝试次数耗尽 ⇒ 抛 `SchemaViolation(errors, raw_last_output)`。修复调用计入 token 计量，命中层级分布计入报告（`report.schema_engine.resolved_at = {l0_or_clean, l1, l3_1, l3_2, rejected}`）。 |

### 3.8.3 API

```
class SchemaEngine:
    def __init__(self, user_schema: dict, llm: LLMClient, cfg: OutputConfig): ...
    async def complete_validated(self, profile: str, prompt: PromptBundle,
                                 schema: dict | None = None) -> dict:
        """schema=None 时用用户 Schema；内部 Schema（裁决/评分/评审/生成/分类（v1.7）/
           分段窗口/动作/缺陷评审（v1.8））由各 Stage 传入。
           成功返回已通过 L2 的 dict；失败抛 SchemaViolation。"""
    def validate_only(self, obj: dict, schema: dict | None = None) -> list[str]:
        """M1 校验 few-shot 示例输出、M11 写出前终检用。"""
```

**设计考量：**“Let Me Speak Freely?”（arXiv:2408.02442）报告了严格格式约束可能损失推理质量 [25]。缓解按各内部 Schema 的实际字段序落地：评审输出的 `critiques` 置于 `verdict` **之前**（3.7.2「先意见后结论」）、pointwise 打分的 `reason` 置于 `score` **之前**（3.4.4「先给两句理由再给整数分」），让模型先推理后作答。例外是成对裁决：字段序为 `criterion → winner → reason`，`reason` 在结论**之后**且仅当 `quality.judgment_reasons` 生效时才要求（3.4.3）——它的用途是落入 trace 供 rubric 优化（7.5），不承担「先推理后作答」的缓解职责（生成输出 `{"samples": [...]}` 则不含自由文本字段）。用户 Schema 若需同类缓解，可自行加 reasoning 字段并在下游忽略。
**背书：**「Schema 约束生成 + 机器校验 + 修复重试」为工业标准三件套：OpenAI Structured Outputs [7]、约束解码框架 Outlines [23]、JSONSchemaBench 对 6 家引擎的评测 [24]、instructor 的 validation-retry 循环与 json-repair 库 [8]。四层纵深（供应商能力不被信任、校验不可豁免）是对 [24] 所示覆盖缺口的直接工程回应。

### 3.8.4 输入 / 输出示例

以贯穿示例的文本模态工程为例：输入行 `{"instruction": "帮我写一条请假条，明天上午要去医院", "source": "ime-log", "ts": "2026-06-30T10:12:00Z"}`（`input.text_field = "instruction"`，记录 id = `1cda030abc565f17`）。M5 组装标注提示词后调用 `complete_validated(profile="default", prompt, user_schema)`。用户 Schema（`output.schema_inline`）：

```
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "intent": {"type": "string",
      "enum": ["writing_assist", "qa", "translation", "chitchat", "other"]},
    "topic": {"type": "string"},
    "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]}
  },
  "required": ["intent", "topic", "difficulty"],
  "additionalProperties": false
}
```

**① L0（本例未启用）：**本走查假设该 profile 未声明 `supports_structured_output`（默认 false，5.1），M9 `complete()` 忽略 `response_schema` 参数（3.9.2），不注入任何原生结构化输出参数——结构保证完全落在 L1–L3。

**② LLM 原始输出**（`LLMResponse.text`，原文照贴；叠加三个问题：Markdown 围栏、尾逗号、`intent` 取值在枚举之外）：

```
```json
{
  "intent": "writing",
  "topic": "请假条写作",
  "difficulty": "easy",
}
```
```

**③ L1 确定性修复：**按 3.8.2 顺序执行——① 剥离 ````json` 围栏；② 取首个花括号平衡子串；③ `json_repair.loads()` 修掉 `"easy"` 后的尾逗号。解析成功，得到对象 `{"intent": "writing", "topic": "请假条写作", "difficulty": "easy"}`。L1 只保证「可解析」、不看 Schema——枚举违规原样进入 L2。

**④ L2 校验：**`Draft202012Validator.iter_errors()` 收集**全部**违规，本例共 1 条：

```
JSON Pointer: /intent
期望: 枚举 ["writing_assist", "qa", "translation", "chitchat", "other"] 之一
实际: "writing"
(jsonschema 原始消息: 'writing' is not one of ['writing_assist', 'qa',
 'translation', 'chitchat', 'other'])
```

违规清单非空 ⇒ 进入 L3。

**⑤ L3 修复调用（第 1 次，预算 `output.max_repair_attempts = 2`）：**本工程未配置 `output.repair_llm` ⇒ 使用调用方 profile `default`。修复提示词按 3.8.2 逐字组装 = 原始输出全文 + 违规清单 + 「只输出修正后的 JSON」，作为单条 user 消息发出：

```
[原始输出]
```json
{
  "intent": "writing",
  "topic": "请假条写作",
  "difficulty": "easy",
}
```

[违规清单]
1. /intent: 期望为枚举 ["writing_assist", "qa", "translation", "chitchat",
   "other"] 之一，实际值为 "writing"

只输出修正后的 JSON。
```

修复响应：

```
{"intent": "writing_assist", "topic": "请假条写作", "difficulty": "easy"}
```

**⑥ 重走 L1→L2：**L1 无围栏可剥、直接解析成功；L2 `iter_errors()` 返回空清单 ⇒ 通过。`complete_validated()` 返回该对象，M5 写入 `item.annotation`：`Annotation.attempts = 2`（= 1 + 1 次 L3 修复，4.2 定义），首次调用与修复调用的 token 均计入 `Annotation.usage` 与 profile 计量（3.9.3）；本次解决计入 `report.schema_engine.resolved_at` 的 `l3_1` 桶（首次 L3 修复即通过），主输出 `_meta.annotation.attempts = 2`。

| 层 | 输入 | 动作 | 输出 |
|---|---|---|---|
| L0 | `supports_structured_output = false` | 不注入原生结构化输出参数 | 提示词原样发出，保证责任交给 L1–L3 |
| L1 | 带围栏 + 尾逗号的原始文本 | 剥围栏 → 平衡花括号子串 → `json_repair.loads()` | 可解析对象（`intent` 仍为 `"writing"`） |
| L2（第 1 次） | L1 产物 | `iter_errors()` 全量收集违规 | 1 条违规（`/intent` 枚举）⇒ 转 L3 |
| L3（第 1 次） | 原始输出全文 + 违规清单 | 经 `output.repair_llm`（默认同调用方）发起修复调用 | 修正后的 JSON 文本 |
| L1→L2（重走） | 修复输出 | 同 L1 / L2 | 通过 ⇒ 返回对象；`attempts = 2`；`resolved_at.l3_1` 计 1 |

若第 2 次 L3 修复后仍未通过 L2，则预算耗尽，抛 `SchemaViolation(errors, raw_last_output)`：该记录 `status = "failed"`、错误码 `schema_violation`（7.6）入 rejects 通道，并计入 `resolved_at.rejected`。

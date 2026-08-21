## 3.8 M8 结构引擎 schema-engine

### 3.8.1 职责与边界

**做：**持有经预校验的用户 Schema 与各内部小 Schema（裁决、评分、评审、生成输出；v1.7 增分类 `classification_schema(class_names, assignment, max_labels, with_reason)`——按 `classify.assignment` 二态、类名词表以 enum 硬约束，关键字集 ⊆ 既有内部 Schema 关键字集且**无 uniqueItems**：该关键字会被 OpenAI strict 模式与部分约束解码网关硬拒，重复标签由 classify 代码在 M8 验证后确定性归一化，全文见 3.13.3；**v1.8 增三项**——分段窗口 `segment_window_schema(frame_count, with_reason)`（M14，全文见 3.14.3）、动作 `action_schema()`（M15，11 值动作词表 enum 硬约束，全文见 3.15.3）、stream 缺陷评审 `defect_verdict_schema()`（M7 stream 分支，三顶键 `{critiques, defects, verdict}` + 缺陷词表——v1.8 五值，v1.9 起六值（+`wrong_stitch`，3.7.2），全文见 3.7.2）；**v1.9 增一项**——缝合判定 `stitch_schema()`（M16，五键 `{verdict, thread_ref, task_name, reason, confidence}` 全 required、thread_ref 可空联合，全文见 3.16.3）；**v1.12 增一项**——帧级批量判决 `frame_classify_schema(names, n)`（M13 帧粒度，单键 `{"labels": [enum×n]}`、`minItems = maxItems = n` 钉死窗内成员数组长度、帧标签词表 enum 硬约束，同族**无 uniqueItems**（帧标签本就允许重复）；长度/索引对齐后校验在代码侧（first-wins，缺项 ⇒ 该帧 `fallback_class`），全文见 3.13.7）。四者逐字 JSON 冻结于 CONTRACTS §10.7，规则同族：关键字集 ⊆ 既有内部 Schema 关键字集、同样**无 uniqueItems**（重复 index / 标签由调用方代码在 M8 验证后确定性收窄——3.14.4 的 first-wins 建表、3.13.4 的归一化行）；可选性一律以可空联合 type 数组 `["array","null"]` / `["string","null"]` 表达、**全键 required**（OpenAI strict 模式硬拒可选属性，L0 无条件透传 Schema）；`minItems = maxItems` 钉死窗口数组长度（judgment_schema 同款）。`defect_verdict_schema` 与既有评审 Schema **并存**——非 stream 评审路径继续用后者（回归锚，S7）。四者与其余内部 Schema 同级：不计入 `report.schema_engine.resolved_at`、不经过 L2.5）；**v1.13 增两项**——时间流生成的蓝图 `plan_schema(names, length, cover_all=False)`（M6，单键 `{"steps": [{frame_class: enum, brief: string}]}`、`minItems = maxItems = length` 钉死步数、帧类名词表 enum 硬约束，同族**无 uniqueItems**（同一帧类在一条序列里本就可重复出现）；**v1.14 增第三入参** `cover_all`——True 时在 steps 数组对象上追加 `allOf` + **逐名一项 `contains`**（子模式形如 `{"properties": {"frame_class": {"const": <名>}}, "required": ["frame_class"]}`，按传入名集序；一个 Schema 对象只有一个 contains 键位，故多类分 allOf 支），与 enum 合成帧类构成的**恰等**约束（enum 给「⊆」、contains 给「⊇」，3.6.5 档位构成行）；`names` 取值由调用方决定（档位表在场即传档内子集），构造器零感知；缺省 False 的输出与 v1.13 逐字节一致。全文见 3.6.5）与帧实现 `realize_schema(step_schemas)`（M6，**逐位包装器**：单键 `{"frames": [...]}`，第 i 位服从蓝图第 i 步帧类的用户生成 Schema——纯文本帧位取 `{"type": "string"}`；以 draft 2020-12 原生关键字 `prefixItems` 表达逐位约束（jsonschema ≥ 4.21 直接可校验、无翻译层）、`"items": false` 封尾禁超长、`minItems = maxItems` 再钉一次长度）；两者逐字 JSON 冻结于 CONTRACTS §10.7，与前述内部 Schema 同族同级。**用户生成 Schema 的 L0 待遇（v1.13）**：`realize_schema` 的逐位子模式是**用户手写**的帧类生成 Schema（`[frame.class.<name>.generate].schema_*`，M1 元校验），包装后随 L0 原样透传——**不做关键字白名单 lint**（与 `output.schema` 今日同款暴露面）；某些 strict 路由拒 `prefixItems` 的排障面是配置级 `supports_structured_output = false`，不新增调用级参数）；提供「LLM 调用 → 合法 JSON 对象」的唯一入口 `complete_validated()`，内部实现四层结构保证；统计各层修复命中率。 
**不做：**不组装业务提示词（调用方传入完整 prompt）；不解释业务语义；不放行任何未通过校验的对象——这是它对全系统的硬契约。

### 3.8.2 四层保证与修复环

图 3-3 结构引擎四层保证。任何写入主输出的对象必然经过 L2 通过分支。

| 层 | 精确行为 |
|---|---|
| L0 | profile `supports_structured_output=true` 时：OpenAI 兼容 provider 传 `response_format={"type":"json_schema", "json_schema":{...strict:true}}`；Anthropic provider 以单工具 `tool_choice` 强制工具调用、Schema 作为工具入参。L0 只是「使 L1/L3 少触发」的优化，不豁免 L2——供应商实现存在覆盖缺口（JSONSchemaBench 实测各引擎均有不支持的 Schema 特性 [24]），校验永远执行。 |
| L1 | 顺序执行：① 剥离 Markdown 代码围栏；② 取首个花括号平衡子串；③ `json_repair.loads()`（工业库，处理截断/单引号/尾逗号/裸换行 [8]）。全部失败 ⇒ 直接进 L3。L1 为纯函数，无副作用、可单测穷举。 |
| L2 | `Draft202012Validator.iter_errors()` 收集全部违规（非首个），每条含 JSON Pointer 路径、期望与实际。通过 ⇒ 返回；未通过 ⇒ L3。**违规渲染的两个显名分支**（渲染文本进 L3 修复清单，也进 `StageError` / rejects 错误报告面，故统一英文）：`enum` 违规按「期望/实际」措辞自行渲染（3.8.4 逐字实例）；**`contains` 违规（v1.14）**点名缺失的帧类——蓝图覆盖约束的 contains 子模式形如 `{"properties": {"frame_class": {"const": <名>}}, …}`，直接取该 const 值渲染为 `steps: missing required frame_class "<名>"`（不是该形状时——用户 Schema 也可以写 contains——回落 jsonschema 原始消息）。理由：L0 关闭的端点上，L3 修复提示不点名缺失帧类则修复指导性趋零（裸数组 repr 无信息量）；帧类名是配置量非数据内容，值-free 纪律不变。其余关键字直接携带 jsonschema 原始消息。 |
| L2.5（v1.5，可选） | `output.validator` 配置时、且仅对用户 Schema 调用：L2 通过后执行用户回调 `fn(obj, record)`。返回非空违规列表 ⇒ 违规以 `(validator) <消息>` 形式并入违规清单、与 Schema 违规同路进入 L3 修复环（回调意见回喂模型自我修正——回调既是门卫也是修复环的教练）；返回空 ⇒ 通过。L3 每轮修复输出重走 L1→L2→L2.5。预算耗尽且剩余违规**全部**来自回调 ⇒ `SchemaViolation(callback_only=True)`，记录 kind = `callback_violation`（7.6），否则仍为 `schema_violation`。回调抛异常不吞：向上传播、按记录级 `internal_error` 收敛（3.5.3）。内部 Schema（裁决/评分/评审/生成/分类（v1.7）/分段窗口/动作/缺陷评审（v1.8）/缝合判定（v1.9））不经过 L2.5。 |
| L3 | 修复提示词 = 单条 user 消息，按 `[原始输出]` / `[违规清单]` 分节标签组织，末尾指令「只输出修正后的 JSON」（逐字实例见 3.8.4）。使用 `output.repair_llm`（默认同调用方 profile）。每次修复输出重走 L1→L2。尝试次数耗尽 ⇒ 抛 `SchemaViolation(errors, raw_last_output)`。修复调用计入 token 计量，命中层级分布计入报告（`report.schema_engine.resolved_at = {l0_or_clean, l1, l3_1, l3_2, rejected}`）。**上下文预算交互（v1.11，V25①）**：修复调用经 M9 终检（3.9）抛出 `ContextOverflowError(phase="precheck")` 时**捕获并记该轮修复失败**——修复 prompt 恒定 ⇒ 余轮必然同败，**短路至预算耗尽**；reject 归因维持既有 `schema_violation` / `callback_violation`（**不新增 reject 值、不计 `report.budget.overflow_records`**，7.6 注）；`[原始输出]` 修复原文**永不截断**——截断即破坏修复语义；该吞点即异常终局——reactive-400 形态在此经共享 `budget.feed_reactive_terminal` 补喂熔断**恰一次**（7.6 熔断矩阵；`_breaker_fed` duck 标防重喂，precheck/200 形态永不喂，v1.11 审计修订）。 |

**帧级两类调用的路由声明（v1.12）**：帧粒度引入的两类 LLM 调用都走本引擎既有能力，`complete_validated` 与 `validate_only` 的显式 schema 参数路径**零改动**——

- **帧分类**（M13 批量判决，3.13.7）：传入模块级构造器 `frame_classify_schema(names, n)` 产出的**内部 Schema**，待遇与裁决/评分/评审等内部 Schema 完全同族——L0–L3 全在、不经过 L2.5、不计入 `report.schema_engine.resolved_at`。
- **帧标注**（M5 逐帧标注，3.5.5）：传入**用户声明的帧级 Schema** `cfg.frame_schema`（显式 schema 参数，裁决·帧 Schema 显式路由）——虽为用户 Schema 的同胞（M1 元校验 + few-shot 干跑，3.1.4），但按**内部 Schema 待遇**路由：L0–L3 四层全在、**无 L2.5**（`output.validator` 仅约束序列级用户 Schema 调用；帧级回调列 8.4 演进候选）、**不计 `resolved_at`**——保住 6.4 恒等式「resolved_at 加总 = 进入 M5 的记录数」不被帧调用污染。
- **写前兜底**（M11，3.11.2）：emitter 对每个非 null 帧标注对象跑 `validate_only(obj, schema=cfg.frame_schema)`——通过 ⇒ status="annotated"，不通过 ⇒ 翻 "failed" + annotation 置 null + 计数，非法帧对象**永不落盘**（主输出 `validate_only` 终检的帧级镜像）。

**显式待遇参数与三类路由声明（v1.13，裁决·M8 显式待遇参数）**：v1.12 的路由把「显式 schema 参数」与「内部 Schema 待遇」绑死，导致 v1.13 的按序列类标注 Schema 无处安放——它是用户 Schema 的另一份实例（记录级标注调用），却必须显式传参。裁决：`complete_validated` 增待遇门 `user_treatment: bool | None = None` 把**待遇**与**传参方式**解耦（2026-08-14 代码规则整改后它是 `CallScope` 的一个字段，与 `record_ids` / `batch_no` / `record` 同乘一个参数对象；语义逐字不变）——

| 路由 | schema 参数 | scope.user_treatment | L2.5 | `resolved_at` 记账 |
|---|---|---|---|---|
| 用户 Schema（全局 `output.schema`） | None（引擎持有） | None ⇒ 推断为真 | ✓（配置了 `output.validator` 时） | ✓ |
| **按序列类标注 Schema（v1.13）** | 显式传该类 Schema | **True** | ✓ | ✓ |
| 帧级 Schema 与全部内部 Schema | 显式传 | None ⇒ 推断为假（帧级/内部调用不显式传 True） | ✗ | ✗ |

`None` 即现行 `schema is None` 推断 ⇒ **既有全部调用点零改动**；`stats` 的口径句相应重述为「**用户待遇族**调用」而非「schema 参数为 None 的调用」，§6.4 恒等式重述为「`resolved_at` 加总 = 进入 M5 的**记录级**标注调用数」（按类 Schema 的记录级标注照常计入，帧级标注仍不计——恒等式不被帧调用污染，6.4）。

### 3.8.3 API

```
@dataclass(frozen=True)
class CallScope:
    """一次调用的记账与追踪范围（2026-08-14 收参：原四个关键字入参的参数对象形，
       字段名与语义逐项不变）。"""
    record_ids: tuple[str, ...] = ()    # 本次调用覆盖的记录 id，仅用于 trace 事件
    batch_no: int = 0                   # 批次号，仅用于 trace 事件与日志 extra
    record: Mapping | None = None       # L2.5 回调第二入参（Record.raw），无则 None
    user_treatment: bool | None = None  # 显式待遇门；None ⇒ 按 schema is None 推断

class SchemaEngine:
    def __init__(self, user_schema: dict, llm: LLMClient, cfg: OutputConfig): ...
    async def complete_validated(self, profile: str, prompt: PromptBundle,
                                 schema: dict | None = None, *,
                                 scope: CallScope = CallScope()) -> dict:
        """schema=None 时用用户 Schema；内部 Schema（裁决/评分/评审/生成/分类（v1.7）/
           分段窗口/动作/缺陷评审（v1.8）/缝合判定（v1.9）/帧级判决（v1.12）/
           蓝图与帧实现（v1.13））由各 Stage 传入。
           v1.13 scope.user_treatment：None = 按 schema is None 推断（既有调用点零改动）；
           True = 用户待遇（计 resolved_at + 启 L2.5）——按序列类标注 Schema 即此形；
           False = 内部待遇。成功返回已通过 L2 的 dict；失败抛 SchemaViolation。"""
    def validate_only(self, obj: dict, schema: dict | None = None) -> list[str]:
        """M1 校验 few-shot 示例输出、M11 写出前终检用；v1.12：M11 帧标注写前
           校验经显式 schema=cfg.frame_schema 走同一入口（3.8.2 路由声明）。"""
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
1. /intent: expected one of enum ["writing_assist", "qa", "translation",
   "chitchat", "other"], got "writing"

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

### 3.8.5 v1.16 联合规划的 brief Schema

v1.16 为 M6 的 sampled brief 增加内部构造器 `brief_schema(length)`。它返回一个顶层
object，唯一属性为 `steps`；`steps` 是长度恰为 `length` 的数组，每个位置的 object 只
有必填字符串属性 `brief`，并以 `additionalProperties = false` 封闭。该 Schema 不返回
`frame_class`，因为帧类词已经由 CP-SAT planner 冻结，LLM 只能补充每个位置的自然语言
概要。它是内部 Schema 待遇：经过 L0–L3，但不经过 `output.validator`，不计入
`report.schema_engine.resolved_at`。

```python
def brief_schema(length: int) -> dict:
    """构造 v1.16 联合规划的定长 brief 内部 Schema。"""
```

M6 将 brief 数组按规划器的固定 frame class 位置配对，再传给已有的
`realize_schema(step_schemas)`；realize 的 `prefixItems` 仍逐位约束帧类生成 Schema，
时间字段绑定后的缩减 Schema 仍由 M6 传入。默认无 rules/windows 的 v1.15 路径继续使用
`plan_schema` 并返回 `frame_class + brief`，因此 brief Schema 不改变默认路径的 prompt 或
Schema 字节。M8 不解析规则、窗口或 correlation，也不参与长度可行性判断；这些语义由
M1/M6 的共享 planner 与声明式 evaluator 负责。

### 3.8.6 v1.17 hook 冻结载体与 noise realization 复用

v1.17 的 M8 增量是两句话级的面，四层保证、内部 Schema 族与修复环全部零改动：

- **hook 不再按字符串二次 resolve**：M1 在 load 期把四个校验钩子（output / sample /
  sequence / scenario）解析并冻结为 callable 载体 `ResolvedHook` / `ValidationHooks`
  （3.1.4.2）；M6 与 schema engine 消费冻结 callable——`complete_validated` 的消费面
  **零变化**（签名、`CallScope`、L2.5 时机与 `resolved_at` 记账全部不动；变化只在调用方
  从解析引用字符串改为读 `cfg.validation_hooks` 的冻结 target）。
- **帧 noise realization 复用既有 Schema 路径**：structured noise 帧的 realization 复用
  既有 task-frame realization 的 Schema 路径（该帧类生成 Schema 的装载、预算检查与校验
  同源，3.6.7），M8 不新增任何 Schema 构造器或待遇分支。

## 3.8 M8 结构引擎 schema-engine

### 3.8.1 职责与边界

**做：**持有经 M1 预校验的用户 Schema 和内部 Schema；为分类、分段、动作、评审、flat 生成及
v1.18 sequence 的 seed/event-plan/frame-render/semantic/noise 调用提供统一 L0–L3 结构保证。内部 Schema
由调用方按标准 draft 2020-12 JSON Schema 传入；schema engine 不认识任何 sequence 规划器专名。
EventPlan 额外使用 `complete_post_validated`，把 L2 合法候选交给一次性后置验证器执行 patch、权限、
state Schema 与 state validator，并把冻结 `EventExecution` 与合法对象一起返回。
**不做：**不组装业务提示词（调用方传入完整 prompt）；不解释业务语义；不放行任何未通过校验的对象——这是它对全系统的硬契约。

实现唯一位于 `labelkit/common/inference/schema_engine.py`。`common.runtime` 旧包不存在；M8 不导入
`labelkit/runtime/`，也不拥有任务接纳或业务任务组。

### 3.8.2 四层保证与修复环

标注后处理复用 `complete_finalized`：先用模型 Schema 校验，再对独立候选运行一次 finalizer，
用完整 Schema 校验后才进入既有用户 validator。`PostprocessorError` 继承 `InternalError`，
必须原样向上传播；不能改为 Schema 违规、消耗 L3 预算或泄漏工程异常正文。
其他定稿契约错误仍为 `candidate_finalizer_contract`。用户 validator 在首轮和每轮修复中
都接收候选与 raw 的递归深拷贝；其参数变异不改变最终候选。L3 通过 repair_projector 删除
代码负责字段及框架时间字段后构造 previous_output，使用相同模型 Schema 产生新候选。
帧标注虽使用后处理，仍是内部 Schema 待遇，无 output validator、无记录级 resolved-at。
完整语义见 `docs/dev/SPEC-annotation-postprocessing.md`。

图 3-3 结构引擎四层保证。任何写入主输出的对象必然经过 L2 通过分支。

| 层 | 精确行为 |
|---|---|
| L0 | profile `supports_structured_output=true` 时：OpenAI 兼容 provider 传 `response_format={"type":"json_schema", "json_schema":{...strict:true}}`；Anthropic provider 以单工具 `tool_choice` 强制工具调用、Schema 作为工具入参。L0 只是「使 L1/L3 少触发」的优化，不豁免 L2——供应商实现存在覆盖缺口（JSONSchemaBench 实测各引擎均有不支持的 Schema 特性 [24]），校验永远执行。 |
| L1 | 顺序执行：① 剥离 Markdown 代码围栏；② 取首个花括号平衡子串；③ `json_repair.loads()`（工业库，处理截断/单引号/尾逗号/裸换行 [8]）。全部失败 ⇒ 直接进 L3。L1 为纯函数，无副作用、可单测穷举。 |
| L2 | `Draft202012Validator.iter_errors()` 收集全部违规，每条含 JSON Pointer 路径、期望与实际。通过后进入可选用户回调或调用级后置验证；未通过进入 L3。enum 等错误统一渲染为英文、值受 trace/content 规则保护。 |
| L2.5（v1.5，可选） | `output.validator` 配置时、且仅对用户 Schema 调用：L2 通过后执行用户回调 `fn(obj, record)`。返回非空违规列表 ⇒ 违规以 `(validator) <消息>` 形式并入违规清单、与 Schema 违规同路进入 L3 修复环（回调意见回喂模型自我修正——回调既是门卫也是修复环的教练）；返回空 ⇒ 通过。L3 每轮修复输出重走 L1→L2→L2.5。预算耗尽且剩余违规**全部**来自回调 ⇒ `SchemaViolation(callback_only=True)`，记录 kind = `callback_violation`（7.6），否则仍为 `schema_violation`。回调抛异常不吞：向上传播、按记录级 `internal_error` 收敛（3.5.3）。内部 Schema（裁决/评分/评审/生成/分类（v1.7）/分段窗口/动作/缺陷评审（v1.8）/缝合判定（v1.9））不经过 L2.5。 |
| L3 | 修复提示词 = 单条 user 消息，按 `[原始输出]` / `[违规清单]` 分节标签组织，末尾指令「只输出修正后的 JSON」（逐字实例见 3.8.4）。使用 `output.repair_llm`（默认同调用方 profile）。每次修复输出重走 L1→L2。尝试次数耗尽 ⇒ 抛 `SchemaViolation(errors, raw_last_output)`。修复调用计入 token 计量，命中层级分布计入报告（`report.schema_engine.resolved_at = {l0_or_clean, l1, l3_1, l3_2, rejected}`）。**上下文预算交互（v1.11，V25①）**：修复调用经 M9 终检（3.9）抛出 `ContextOverflowError(phase="precheck")` 时**捕获并记该轮修复失败**——修复 prompt 恒定 ⇒ 余轮必然同败，**短路至预算耗尽**；reject 归因维持既有 `schema_violation` / `callback_violation`（**不新增 reject 值、不计 `report.budget.overflow_records`**，7.6 注）；`[原始输出]` 修复原文**永不截断**——截断即破坏修复语义；该吞点即异常终局——reactive-400 形态在此经共享 `budget.feed_reactive_terminal` 补喂熔断**恰一次**（7.6 熔断矩阵；`_breaker_fed` duck 标防重喂，precheck/200 形态永不喂，v1.11 审计修订）。 |

**v1.19 资源与并发边界。**operator 的 `TaskSpec.resource_key` 只标识首轮业务调用的接纳通道。L3 若切换到
`output.repair_llm`，SchemaEngine 在同一叶任务内继续调用 LLMClient；LLMClient 必须通过共享
ResourceManager 取得 repair profile 的实际逻辑调用许可与对应 origin 许可。repair 不创建嵌套
`TaskGroupRequest`，也不借用首轮 profile 的许可；首轮 profile 容量很大而 repair profile 容量为一时，repair
实际并发仍不得超过一。每个请求的 Schema、scope、后置验证器与修复状态只存在于该调用栈，并发叶任务不得共享
可变修复候选。

Schema validation、repair、usage、retry、breaker、resource/origin wait 与 provider latency 是已发生的运行事实，
通过 MetricsSink 的实时路径记录，不进入可回滚 dataset capture。sequence attempt 被拒绝或高 ordinal 候选被取消
时，这些事实仍保留；只有 dataset counters 等业务归并量随 attempt-local capture 丢弃。

**帧级两类调用的路由声明（v1.12）**：帧粒度引入的两类 LLM 调用都走本引擎既有能力，`complete_validated` 与 `validate_only` 的显式 schema 参数路径**零改动**——

- **帧分类**（M13 批量判决，3.13.7）：传入模块级构造器 `frame_classify_schema(names, n)` 产出的**内部 Schema**，待遇与裁决/评分/评审等内部 Schema 完全同族——L0–L3 全在、不经过 L2.5、不计入 `report.schema_engine.resolved_at`。
- **帧标注**（M5 逐帧标注）：配置后处理时使用 finalized 接口，模型接收 cfg.model_frame_schema，工程函数处理后按 cfg.frame_schema 终验；无后处理时沿用显式帧 Schema 调用。帧保持内部 Schema 待遇，L0–L3 全在，无记录级 output.validator、不计记录级 resolved-at。
- **写前兜底**（M11，3.11.2）：emitter 对每个非 null 帧标注对象跑 `validate_only(obj, schema=cfg.frame_schema)`——通过 ⇒ status="annotated"，不通过 ⇒ 翻 "failed" + annotation 置 null + 计数，非法帧对象**永不落盘**（主输出 `validate_only` 终检的帧级镜像）。

**显式 Schema 待遇：**`CallScope.user_treatment` 把用户待遇与显式 schema 参数解耦；按类标注 Schema 虽显式传入，仍执行 L2.5 与 `resolved_at` 记账：

| 路由 | schema 参数 | scope.user_treatment | L2.5 | `resolved_at` 记账 |
|---|---|---|---|---|
| 用户 Schema（全局 `output.schema`） | None（引擎持有） | None ⇒ 推断为真 | ✓（配置了 `output.validator` 时） | ✓ |
| **按 sequence class 标注 Schema** | 显式传该类 Schema | **True** | ✓ | ✓ |
| 帧级 Schema 与全部内部 Schema | 显式传 | None ⇒ 推断为假（帧级/内部调用不显式传 True） | ✗ | ✗ |

`None` 即现行 `schema is None` 推断 ⇒ **既有全部调用点零改动**；`stats` 的口径句相应重述为「**用户待遇族**调用」而非「schema 参数为 None 的调用」，§6.4 恒等式重述为「`resolved_at` 加总 = 进入 M5 的**记录级**标注调用数」（按类 Schema 的记录级标注照常计入，帧级标注仍不计——恒等式不被帧调用污染，6.4）。

### 3.8.3 API

```
@dataclass(frozen=True)
class CallScope:
    """一次调用的记账、追踪与可选 L3 正文边界。"""
    record_ids: tuple[str, ...] = ()    # 本次调用覆盖的记录 id，仅用于 trace 事件
    batch_no: int = 0                   # 批次号，仅用于 trace 事件与日志 extra
    record: Mapping | None = None       # L2.5 回调第二入参（Record.raw），无则 None
    user_treatment: bool | None = None  # 显式待遇门；None ⇒ 按 schema is None 推断
    repair_context_bytes: int | None = None  # 单轮新增 L3 消息正文集合 UTF-8 byte 上限

class SchemaEngine:
    def __init__(self, user_schema: dict, llm: LLMClient, cfg: OutputConfig): ...
    async def complete_validated(self, profile: str, prompt: PromptBundle,
                                 schema: dict | None = None, *,
                                 scope: CallScope = CallScope()) -> dict:
        """schema=None 时用用户 Schema；内部 Schema（裁决/评分/评审/生成/分类（v1.7）/
           分段窗口/动作/缺陷评审（v1.8）/缝合判定（v1.9）/帧级判决（v1.12）/
           sequence 的 seed/event-plan/frame-render/semantic/noise Schema）由调用方传入。
           scope.user_treatment：None = 按 schema is None 推断；
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

### 3.8.5 v1.18 调用级后置验证

既有 `complete_validated` 返回类型不变。需要执行状态转移的 EventPlan 使用独立内部入口：

~~~python
CallPostValidator = Callable[[Mapping[str, object]], PostValidationResult]


async def complete_post_validated(
    request: PostValidatedCallRequest,
) -> ValidatedGenerationCall:
    """对每个结构合法候选执行恰一次调用级后置验证。"""
~~~

`PostValidatedCallRequest` 冻结 profile、prompt、Schema、`CallScope` 与后置验证器。每个首轮或 L3
候选通过 L2 后恰调用一次：

- 非空 `violations` 且 `event_execution is None` 是可修复结果，以
  `(post-validator)` 前缀进入同一 L3 违规清单。
- 空 `violations` 且携带 `EventExecution` 是唯一成功形态；结果随
  `ValidatedGenerationCall` 返回，正式事件提交不得再次执行 patch 或 hook。
- 其他形状、非 string 违规、回调异常或错误返回类型分别归
  `post_validator_invalid` / `post_validator_exception`，直接拒绝当前 slot attempt，不进入 L3。
- EventPlan 的 L3 prompt 重放 `PostValidatedCallRequest.prompt` 的原始 system/user 消息，把上一候选作为
  assistant 消息，再追加只含值受控 violations 的 user 修复消息。它只能复用首轮已经可见的
  ActorView/visible state，不能追加 `EventExecution`、隐藏 state、hook 异常或任何新事实。普通
  `complete_validated` 继续使用 3.8.2 的单 user 修复形态。
- `CallScope.repair_context_bytes` 为 null 时保持通用 M8 既有行为；v1.18 sequence 每个 family 显式传 32768。
  普通 L3 对完整新 user 正文计费；EventPlan 对新增 assistant 原始输出与新增 user 修复正文之和计费。
  恰好上限可派发，多一 UTF-8 byte 在 M8 内记录 warning 后短路，零 provider call、不截断原输出。

只有 EventPlan 使用该入口；ScenarioSeed、FrameRenderer、SemanticEvaluator 与 noise 继续调用
`complete_validated`。后置验证器只存在于当前 request，M8 不保存跨调用状态，并行请求不会串用 state/hook。
`ValidatedGenerationCall` 按字段顺序携带 `object`、`event_execution`、`resolved_at`、`usage`、
`attempts`、`model`；`resolved_at` 闭集只有 `l0_or_clean`、`l1`、`l3_1`、`l3_2`，用于标识当次
成功对象的解析路径，不写入只属于用户 Schema annotate 的全局 `resolved_at` 计数。

state validator 必须确定、无副作用，签名为
`validate_state(StateTransitionInput) -> list[str]`。M1 用同一深拷贝输入连续调用两次并比较归一化违规字节。
普通非空 string list 进入 L3；异常与非法返回分别终结 attempt。sequence 与 scenario 级旧 hook 不存在。

### 3.8.6 v1.18 sequence Schema 所有权

固定 family 为 `generation.scenario_seed`、`generation.event_plan`、
`generation.frame_render`、`generation.semantic_evaluate`、`generation.noise_render` 与
`generation.noise_evaluate`。模板与输出 Schema 冻结在 CONTRACTS，各 generation 模块只定义一次。
M8 只消费调用方传入的标准 JSON Schema，不提供蓝图、概要、批量逐位实现或字段回填构造器。

ScenarioSeed、ActorView、patch、payload、完整轨迹证据与 semantic evaluator 输入不能裁剪或以摘要替代。
semantic evaluator 只接收盲化 `SemanticEvaluationRequest`，不得因完整性要求改传 `EventTrace`、
variant/target/expected/actual violation、pattern/state evaluation 或其他 evaluator truth。
precheck overflow 不发请求；内容相关 overflow、truncation 或 repair 耗尽按当前 sequence/noise attempt
错误矩阵返回。provider fatal、circuit trip、KeyboardInterrupt 与 CancelledError 不在 M8 降级为记录错误。

## 3.13 M13 分类 classify（v1.7）

### 3.13.1 职责与边界

**做：**按用户类别表（`[[classify.classes]]`，5.2）对批内 `status="active"` 且 `classification is None` 的记录做 LLM 封闭集（closed-set）分类：单/多标签可配（`classify.assignment = "single" | "multi"`），可选 self-consistency 投票（3.13.4）；分类结果写入 `item.classification`（4.1），供下游算子按类取有效配置（3.4.3、3.5.2、3.6.2、3.7.2）；multi 模式下按标签向批尾扇出兄弟信封（3.13.4 multi 扇出行）。链序位于 dedup 之后、quality 之前（3.10.3）——重复记录不消耗分类调用；「按类打分」要求类归属与按类 rubric 在打分前就绪。 
**不做：**不淘汰记录（分类不是质量门；multi 扇出只增不减）；不定义类别语义（类别表来自配置，同 rubric 信任级）；不做标注（分类是工具内部结构——内部 Schema、驱动管线行为、进 `_meta`；用户 Schema 产出物属 M5）；不改变链结构（扇出改变的只是批内信封基数，4.3 契约 ②a）。

### 3.13.2 输入 / 输出

| 方向 | 内容 |
|---|---|
| 输入 | 批内 `status="active"` 且 `classification is None` 的 PipelineItem（`classification is not None` 者幂等跳过，3.13.4）；类别表与分类参数（`[classify]`，5.2）；LLM profile（`classify.llm`）。 |
| 输出 | 每条处理记录 `item.classification: Classification`（4.1：`label` = 本信封路由标签，`labels` = 命中全集，`source` ∈ {"llm","fallback","inherited"}）；`assignment="multi"` 且归一化后命中 k ≥ 2 类时批尾追加 k−1 个兄弟信封；返回值 = 传入的同一列表对象（4.3 契约 ②a）。`on_error="fail"` 且结构修复耗尽时该记录 `status="failed"`、StageError 入 `item.errors`。 |

### 3.13.3 分类提示词与内部 Schema（确定性模板）

```
system:
  single: 你是数据分类员。阅读待分类数据，判断它属于以下类别中的哪一类。类别表：
  multi:  你是数据分类员。阅读待分类数据，判断它适用于以下哪些类别（至少 1 类，至多 {max_labels} 类）。类别表：
  - {name}: {description}                       ← 按 [[classify.classes]] 声明序逐类一行
  {classify.instruction}                        ← 可选补充说明；缺省省略此行
  输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：
  single: {"class": <类名>[, "reason": <一句话理由>]}
  multi:  {"classes": [<类名>, ...][, "reason": <一句话理由>]}   ← reason 仅请求时出现于两式
user (对每条配置了 examples 的类，按声明序；类内按数组序):
  [类别示例·{name}] {example}
user (当前记录):
  文本模态: [待分类数据] {record.text}
  UI 模态:  [屏幕截图] <image: base64>
           [UI 控件树] {record.ui_tree.serialize(max_chars=input.ui_tree_max_chars)}
```

UI 模态的「当前记录」Part 组装与 3.5.2 相同（`[屏幕截图]` 为 `kind="image"` 的 Part，`[UI 控件树]` 为 `kind="text"` 的 Part，3.9.2），所引 profile 须 `supports_vision = true`（M1 校验，3.1.4）。类别示例 `examples` 仅输入侧——分类的输出格式由内部 Schema 钉死，无「示例输出」段。`reason` 的请求条件见 3.13.4 调用与校验行。

**序列记录分支（v1.8）。**stream 模式下 episode（`record.kind = "sequence"`，3.14）作为普通记录被分类（对序列形态零崩溃），仅「当前记录」user 消息改走序列变体——system 与类别示例消息不变，段标签与截断标记行逐字冻结于 CONTRACTS §10.8 序列变体：text part `[待分类数据·序列]` = 成员逐帧 `frame_digest`（4.3）按成员序每帧一行拼接的 episode 摘要，**总量封顶 `input.ui_tree_max_chars`**——**首尾成员恒保留**、中段按**整条**截断、被截断时以标记行 `…(truncated N members)` 收尾；UI 模态另附**首帧截图**（text part `[首帧截图]` + 首成员的 image part，M9 调用时编码，3.9.2）——classify 保持在 vision 引用集，所引 profile 仍须 `supports_vision = true`（3.1.4 vision 逐阶段表）；文本模态序列仅摘要段。

分类输出经 M8 内部 Schema 校验（`schema_engine.classification_schema`，3.8.1 内部 Schema 清单）。关键字集 ⊆ 既有内部 Schema 关键字集，**不写 `uniqueItems`**——该关键字会被 OpenAI strict 模式与部分约束解码网关硬拒（L0 无条件透传 Schema），重复标签改由本模块在 M8 验证后确定性归一化（3.13.4 归一化行）：

```
def classification_schema(class_names: list[str], assignment: str,
                          max_labels: int, with_reason: bool) -> dict:
    if assignment == "single":
        props: dict = {"class": {"type": "string", "enum": list(class_names)}}
        required = ["class"]
    else:
        props = {"classes": {"type": "array",
                             "items": {"type": "string", "enum": list(class_names)},
                             "minItems": 1, "maxItems": max_labels}}
        required = ["classes"]
    if with_reason:
        props["reason"] = {"type": "string"}
        required += ["reason"]
    return {"type": "object", "properties": props,
            "required": required, "additionalProperties": False}
```

### 3.13.4 算法规格

关键设计的精确定义：

| 设计点 | 定义 |
|---|---|
| 调用与校验 | 每记录 1 次调用（self-consistency 启用时 ×n），经 `complete_validated(schema=classification_schema(...))`（3.8.3）——内部 Schema：不计入 `report.schema_engine.resolved_at`、不经过 L2.5（3.8.2）。`reason` 仅当 `trace.enabled = true` 且 `trace.channels` 含 `"classify"` 时请求（零额外 token 原则，对齐 `quality.judgment_reasons` 的 "auto" 语义；7.2）。temperature 恒 0（sc 采样取 `classify.sc_temperature`）。批内记录级并发（asyncio.gather + profile 信号量，骨架同 M5）。 |
| 归一化（M8 之后，确定性，顺序固定） | ① 标签映射到类别表声明序并**去重**；② 兜底类与具体类同现 ⇒ 剔除兜底类（纯兜底保留——「其余」与具体类命中矛盾）。归一化只收窄已验证集合（词表合法性由内部 Schema enum 保证，3.13.3）。 |
| sc 投票 | `classify.self_consistency = n`（0 关；≥3 奇数，M1 校验）：n 次独立采样（某次采样 SchemaViolation ⇒ 该样本弃权，分母仍为 n）。single：多数票，无过半 ⇒ 归兜底类；multi：逐标签保留出现于 > n/2 个采样集合者，全落选 ⇒ 归兜底类。`Classification.detail.sc = {"n", "agreement_ratio"}`（single = 胜出类票占比；multi = 保留标签中最低票占比）。本投票不复用 3.5.2 的字段级投票——其「全体分歧回退首样本」语义不适用于分类（无过半应归兜底而非取首样本）。 |
| 失败与兜底 | M8 修复耗尽：`classify.on_error = "fallback"`（默认）⇒ 归兜底类 `classify.fallback_class`，`source="fallback"`，留痕写 `Classification.detail`（含 kind 与消息）——**不写 `item.errors`**（记录存活；rejects 归因取 `item.errors[0]`，写入会在该记录后续阶段失败时污染归因，3.11.2）+ error 事件（kind = `classification_invalid`，7.6）+ 计数器 `classify.fallback`；`on_error = "fail"` ⇒ `status="failed"`、StageError 入 `item.errors` ⇒ rejects。 |
| multi 扇出 | 归一化后 k ≥ 2：原信封取首标签（声明序），其余 k−1 标签各克隆一个兄弟 `PipelineItem` **原地追加到传入批列表尾部**——克隆共享 `record` 与 `dedup`（引用共享，零内容复制；保证兄弟行 `_meta.dedup` 一致）并继承 `session_id`（v1.8——兄弟序列信封对 M7 边界余量/邻域查询保持可寻址，3.7.3）以及 `thread_id` 与 `seam_indexes`（v1.9，stitch 启用时在场——复制清单增两项：thread_id 为真字段进克隆构造、seam_indexes 为 duck 标进复制循环；兄弟线索信封对 M15 接缝占位与 M7 `[片段结构]` 证据保持可用，3.16/3.15.4/3.7.2），`classification` 换 label（`labels` 同为全集），`status="active"`，scores / annotation / verification / errors 为全新默认容器。追加序 =（原元素批内位置 → 标签声明序），逐字节可复现。返回值 = 传入的同一列表对象（4.3 契约 ②a）。此后每个信封与普通单标签记录完全同构——进各自类池打分、按各自类参数标注/评审、独立淘汰、独立产出一行（行唯一键 = (`_meta.id`, label)，6.3），下游算子对扇出零感知；扇出净增数由 M10 计入 `counts.fanout`（3.10.3、6.4）。 |
| multi × episode（v1.8，S9） | stream 模式下 multi 扇出照常作用于序列信封，两点增量语义：① 克隆兄弟的 `transitions` **恒为 None**——extract 链序在 classify **之后**（3.10.3），每个兄弟按**各自 label** 的有效 `[class.<label>.extract]` instruction 独立摘取（per-label 白名单承诺兑现；transitions 每信封自持，×k 摘取成本接受——episode 命中多类应属罕见：M14 边界判据即「单一目标导向活动」，3.14.4；dry-run 沿本表 multi 惯例按乘数 1 报下界，3.10.3）。② **共享语义边界声明**：本表「multi 扇出」行的「克隆共享 `record` 引用」不变量仅维持到 M7 成员手术为止——被修复兄弟的 `record` 分叉（以新成员集重建，3.7.3 stream 修复路由），同 `_meta.id` 的兄弟输出行自此 `member_ids` **可不同**，以 `_meta.stream.repaired` 消歧（6.3）；membership 类手术仅原信封（首标签）可执行、克隆兄弟降为只标记（S8，3.7.3）。 |
| 幂等与 sequence 边界 | `classification is not None` 的项跳过，覆盖 flat 回流样本的 inherited 分类与任何重入。v1.18 sequence 要求 `classify.enabled = false`，ClassifyStage 不进链；sequence class 直接由 `[class.<name>]` 声明，EventProjector 在主序列与成员事件上写 `source="inherited"` 的实际类真值。因此 sequence 的分类调用恒为零，但 M4/M5/M7/M11 必须继续读取 inherited Classification 选择 ClassView。 |
| 事件与计数 | 每记录一条 `classify.decision` trace 事件（classify 通道 / trace-only，payload：`label`、`labels`（multi 携带全集）、`source`、`reason`†、`sc`†；条件与字段定义见 7.2）；计数器（M13 属主）：`classify.classes.<name>`（逐标签计）、`classify.fallback`、`classify.failures`、`classify.multi_label_records`；`counts.fanout` 由 M10 计量（counts.* 所有权属 M10，3.10.3）。report `classify` 节见 6.4。 |
| 上下文预算装填（v1.11） | 分类 profile 声明 `context_window` 时按上下文预算装填分类调用（未声明 = 预算关闭，行为与 v1.10 一致；预算/估算/校准机制见 3.9）：「当前记录」的单记录 UI 树渲染动态封顶——`UITree.serialize(max_chars=…)` 实参从固定 `input.ui_tree_max_chars` 改为 `min(ui_tree_max_chars, 预算折算字符)`（渲染后按 est 复核，超则按行丢尾、保留既有 `…(truncated N nodes)` 标记；`ui_tree_max_chars` 保留为绝对上限，V9；序列分支的摘要段封顶为同族语义）。**类别示例是系统侧静态部件（V13③）**：`[类别示例·{name}]` 段与类表、`classify.instruction` 一律**不动态裁剪**（用户语义资产）——由 M1 静态预检把关（est ≥ input_budget → CONFIG_ERROR、> 50% → WARN，3.1.4）。连最小单元（单记录）都装不下 → 该记录记 `context_overflow` 入 rejects（V10，7.6）。逐裁剪点计入 `report.budget.truncations`（6.4）。 |

### 3.13.5 API 与配置

```
class ClassifyStage(Stage):
    name = "classify"
    def __init__(self, cfg: ResolvedConfig): ...
    async def run(self, batch, ctx) -> list[PipelineItem]: ...   # 返回传入的同一列表（multi 可尾部追加）

def build_classify_prompt(record: Record, cfg: ResolvedConfig,
                          with_reason: bool) -> PromptBundle       # 3.13.3 模板的确定性组装
async def classify_record(record: Record, ctx: RunContext) -> Classification
```

配置见 5.2 `[classify]` 键表与 `[class.<name>.<section>]` 按类覆盖白名单表；按类合并语义与全量校验清单见 3.1.4（分类与按类覆盖行）。

**背书：**「先分类、按类走不同加工/合成策略」经 Nemotron-CC 在 6.3T token 级生产中验证——质量分档路由不同合成管线 [37]，并已产品化为 NeMo Curator 的分类器管线阶段（DomainClassifier / QualityClassifier 等，记录级标签字段驱动路由）[40]；LLM 对指令数据做语义打标并据标签驱动数据决策的可靠性见 InsTag [38]；按类条件化的数据构造是 Tülu 3 按核心技能分治的标准姿势 [39]。LLM 封闭集分类的「标签 ∈ 词表」硬校验形态同 Autolabel 的 classification / multilabel 任务 [12]——本模块以 M8 内部 Schema 的 enum 约束实现（供应商结构化输出 + 修复环全套复用，3.8）；self-consistency 投票为 Wang et al. 的多路径采样多数决 [33]（分类恰是该机制收益最大的分类型 Schema 场景，3.5.2）。与 NeMo Curator 跑本地 GPU 分类模型不同，本模块走运行时 LLM API——与 M4 以「运行时 API 裁决」替代 QuRater 离线分类器是同一既有决策的延伸（2.1.2 ①、3.4.5 背书行）。

### 3.13.6 输入 / 输出示例

沿用统一文本示例（输入法中文指令意图工程，`input.text_field = "instruction"`），project.toml 追加：

```
[classify]                                  # project.toml 追加片段
enabled = true
llm = "default"
fallback_class = "other"                    # 必填，须 ∈ classes；LLM 亦可主动选择它

[[classify.classes]]
name = "writing"
description = "写作协助类指令：代写、改写、文案、模板"
examples = ["帮我写一条请假条，明天上午要去医院"]   # 可选类别示例（仅输入侧）

[[classify.classes]]
name = "qa"
description = "知识问答与解释类指令"

[[classify.classes]]
name = "other"
description = "不属于以上任何一类的指令"
```

对记录 `fd97f67330e81315`（"解释一下二分查找为什么是 O(log n)，能不能举个在通讯录里找人的例子"，3.4.6 的 r3）按 3.13.3 模板逐字组装（single 模式；本例 trace 未启用 ⇒ 不请求 reason）：

```
system:
  你是数据分类员。阅读待分类数据，判断它属于以下类别中的哪一类。类别表：
  - writing: 写作协助类指令：代写、改写、文案、模板
  - qa: 知识问答与解释类指令
  - other: 不属于以上任何一类的指令
  输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：
  {"class": <类名>}
user (类别示例·writing):
  [类别示例·writing] 帮我写一条请假条，明天上午要去医院
user (当前记录):
  [待分类数据] 解释一下二分查找为什么是 O(log n)，能不能举个在通讯录里找人的例子

← 响应(经 M8 classification_schema 校验): {"class": "qa"}

item.classification = Classification(label="qa", labels=("qa",), source="llm", detail={})
```

**multi 扇出变体**：改设 `assignment = "multi"`、`max_labels = 2`，对双意图记录「写一段讲解二分查找原理的教程」（写作 + 问答双命中）：

```
响应: {"classes": ["writing", "qa"]}      # 归一化：映射声明序、去重、无兜底类同现 ⇒ k=2
原信封:         classification = Classification("writing", ("writing", "qa"), "llm", {})   # 首标签（声明序）
批尾追加兄弟信封: classification = Classification("qa",      ("writing", "qa"), "llm", {})
                # 共享同一 record 与 dedup 引用；scores/annotation/verification/errors 为全新默认容器
```

两个信封各自进 writing / qa 类池打分、按各自类有效 instruction 标注，各产出一行（行唯一键 (`_meta.id`, label)，6.3）；本批 `counts.fanout` 增 1（3.10.3）。**fallback 分支**：若某记录的分类输出经 M8 修复耗尽仍非法，默认 `on_error="fallback"` 下归兜底类——`Classification(label="other", labels=("other",), source="fallback", detail={"kind": "classification_invalid", "message": "…"})`，记录保持 active、不写 `item.errors`（3.13.4 失败与兜底行）。

### 3.13.7 帧级批量判决（v1.12）

流模式帧粒度的分类面（`frame.classify.enabled`，默认关，5.2）：对序列信封的成员帧按**帧类表**（`[[frame.classify.classes]]`，与序列类表相互独立、允许重名、互不约束）做一次批量闭集判决，产物写 `item.member_classifications`（键 = 成员 `record.id`，4.1）。链位住 M13 的成本结构（裁决·链位与成本）：dedup 之后——重复 episode 不付费；quality 之前的少量批量调用可接受（帧标注贵在逐帧，故住质量门之后的 M5，3.5.5）。

**公开面（算子间导入白名单第四向，签名冻结）**：

```
async def classify_frames(members: Sequence[Record], ctx: RunContext) -> dict[str, Classification]:
    """对给定成员 Record 序列做帧级闭集批量判决，返回 {member.id: Classification}
       （label = 帧类名，labels = (label,) 恒单元素，source ∈ {"llm", "fallback"}）。
       M7 verify 的成员回收补跑直接调用（单成员回收即单元素调用）——v1.8
       segment.judge_window 同款契约地位的 sanctioned import exception（CONTRACTS §1.1）。
       本函数永不抛出记录级异常（运行级控制流大三样除外）。"""

def build_frame_classify_prompt(members: Sequence[Record], cfg: ResolvedConfig,
                                digests: Sequence[str]) -> PromptBundle:
    """帧级批量判决模板的确定性装配（verbatim 捕于 CONTRACTS §10.12）。"""
```

要点（规格与理由）：

- **执行门**（stage 内帧 pass，序列级判决写完之后、multi 扇出之前）：active ∧ `record.kind == "sequence"` ∧ 首标签信封（`classification.label == labels[0]`；classification 为 None 视同非克隆——克隆恒携 classification）∧ 幂等门 `item.member_classifications is None` ∧ 非降格（`segment_degraded` duck 标在场 ⇒ 计 `frame_classify.skipped_degraded` 并跳过，裁决·降格会话跳过）。
- **组链双门**（裁决·组链双门）：factory 以或门 `classify.enabled ∨ frame_classify.enabled` 决定 ClassifyStage 进链（槽位不变，3.10.3）；stage 内序列级判决单独受 `classify.enabled` 门控——仅帧级开启时序列记录不产生 Classification（`_meta.classification` 维持 null）、帧 pass 照常。
- **调用形态**：一 episode 一调用；预算声明时按 `budget.pack_windows`（v1.12 自 `segment._pack_windows` 下沉的同一纯函数，裁决·装箱器下沉）对成员摘要行成本贪心分窗——**零重叠调用形**：pack_windows 跨度链自带的 1 帧重叠（M14 缝帧语义）不适用于帧分类，自第二窗起丢弃与前窗重叠的首帧（前窗持有缝帧判决），所得跨度两两不交且完整覆盖；帧分类无窗口上限键 ⇒ 预算是唯一切分力；**预算关 ⇒ 单窗全成员**。
- **提示词**（确定性模板，house 风格，verbatim 冻结 CONTRACTS §10.12）：system = `[任务]` 逐帧闭集分类指令（{N} 代入窗内成员数）+ 帧类表行 `- name: description`（声明序）+ 结构句 + 输出契约；user = `[会话成员帧]` 1-based 摘要行（`frame_digest`，每行上限 `segment.digest_max_chars`，会话级预计算复用——装配器自身永不计算摘要）；`FrameClassifyConfig.vision_resolved` 时每成员追加 `[成员 i 截图]` 标签 + image part（工作点 = profile `default_image_px`，不另设尺寸——校准器按 profile 聚合的前提）。
- **内部 Schema**：`schema_engine.frame_classify_schema(names, n)` = `{"labels": {"type": "array", "items": {"enum": [...]}, "minItems": n, "maxItems": n}}` + `additionalProperties: false`（`segment_window_schema` 同款先例，3.8.1）；**对齐后校验**在代码侧（first-wins 家族）：labels 数组按位置对齐窗内成员序，**超长截断**（保留前 n 项）、**缺项 ⇒ 该帧 `fallback_class`**（source="fallback"）；**同 id 成员 first-wins**（裁决·同 id 成员 first-wins，§1.6）——成员 id 为内容哈希，episode 内同 id 帧落表首位次胜出、不重复计数，各位次 members[] 行渲染同一产物。
- **失败语义**（v1.7 fallback 哲学下推）：单窗修复穷尽或调用不可恢复 ⇒ 该窗**全部成员**落 `fallback_class`，计 `frame_classify.fallback += N`、`frame_classify.window_failures += 1`；**永不**使 episode 信封 failed、不写 `item.errors`。窗口跨度零重叠 ⇒ 各窗写入互不覆盖，折叠结果与调度顺序无关。
- **溢出纪律**：precheck（含强制最小窗仍不可装填）= 最小单元失败，**永不喂熔断**；反应式溢出 ⇒ 窗口**对半重切 ≤ 2 级**（segment V20 同款镜像；帧分类窗口零重叠，切分不保缝帧；每次对半计 `budget.degrade_retries`），级数耗尽/单帧窗不可再切 ⇒ 按窗失败兜底；reactive-400 终局在窗失败吞点经共享 `budget.feed_reactive_terminal` 补喂熔断**恰一次**（A7 纪律，7.6 熔断矩阵）。
- **扇出共享**（裁决·扇出共享与首标签执行）：`_fan_out` 克隆构造清单显式加入 `member_classifications` / `member_annotations` 两字段——与 `record`/`dedup` 同族**按引用共享**（帧产物描述成员帧本身而非信封路由，克隆行渲染同一 dict）；帧 pass 在扇出**之前**执行，克隆自身永不重跑。
- **产物**：`item.member_classifications = {member_id: Classification(label=帧类名, labels=(label,), source="llm"|"fallback")}`（恒单标签——帧级无 assignment，3.1.4 定向探针）。
- **与 v1.18 sequence 互斥**：`generate.form = "sequence"` 要求 `frame.classify.enabled = false`，
  ClassifyStage 整体不进链。PatternEvaluator 的 actual role binding 与实际 EventTruth.frame_class 是 projector
  写 inherited `member_classifications` 的唯一来源；planner role witness 不能代写实际标签。process stream 的
  帧分类路径维持本节算法。
- **事件与计数**：`classify.frame` 每 episode 一发（ids=(episode_id,)，payload = members/windows/fallback 三计数，不携带任何数据内容，3.12.4）；计数器 `frame_classify.calls` / `fallback` / `window_failures` / `skipped_degraded`（report 子块见 6.4）。

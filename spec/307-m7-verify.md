## 3.7 M7 二次校验 verify

### 3.7.1 职责与边界

**做：**用独立 judge profile 对每条 (记录, 标注) 评审：输出 verdict（pass/fail）+ 逐项批评意见；fail 时按策略丢弃，或将批评意见回喂 M5 重新标注（有界修复环）。 
**不做：**不自己改写标注（修复 = M5 重标注 + M8 重校验，M7 只供给批评意见）；不评审结构合法性（到达此处的标注必已合法）；不做打分（M4 职责）。

### 3.7.2 评审调用

配置标注后处理时，verify 始终评审已经过后处理、框架时间注入和完整 Schema 的最终 annotation。
repair 将 previous_output 投影为模型字段后交给 M5；新候选再次后处理并完整校验。
episode repair 与 frame backfill 使用相同入口，不绕过按类函数或真实成员 raw。
这两个分支在 sequence attempt 中必须原样传播 `PostprocessorError` 及既有内部定稿错误，
不能吞为可恢复成员失败；普通 process 仍保持记录/成员隔离。完整契约及验收矩阵见
`docs/dev/SPEC-annotation-postprocessing.md`。

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

**按类取值（v1.7）。**classify 启用且记录带类标签时，本节模板的 `[任务指令]` 段与 `{verify.extra_criteria}` 均取该类有效值（分别为 `class_views[label]` 的 annotate.instruction 与 verify.extra_criteria，3.1.4 按类覆盖合并行）——按类标注配全局评审指令是语义错位，故两处同步取类值。`build_verify_prompt` 经 `options.label` 取值，`_judge_round` / `_reannotate` 透传（repair 重标注调 `annotate_record(record, ctx, AnnotatePromptOptions(label=…, …))`，3.5.2 按类取值段）；policy / max_repair_rounds / llm / judges 恒为全局（5.2 按类覆盖白名单表）。trace `verify.verdict` 事件 payload 增 `label` 字段（仅 classify 启用时携带，7.2 只增不改）。

**多评审团（可选，v1.2）：**`verify.judges`（array，默认 `[]`，与 `quality.judges` 语义一致）非空时启用评审团：空 = 单评审走 `verify.llm`，本节既有行为完全不变；非空须为**奇数个** profile 引用（M1 校验，不满足报错退出码 2）。各 judge 按本节同一模板**各自独立**评审（互不可见对方意见），最终 `verdict` 取多数票；各方 `critiques` 全部合并保留进 `VerificationResult.critiques`（4.2），每条标注来源——条目增加 `judge` 字段（= profile 名）。trace 事件 `verify.verdict` 相应改为**每 judge 一条**，payload 新增 `judge` 字段（字段只增不改，7.2 事件契约向后兼容）。`policy = "repair"` 回喂 M5 时，[审核意见] 段 = 全部投 fail 的 judge 的 critiques 合并（各条前缀来源 judge 名）。成本为单评审的 |judges| 倍，宜配置 3 个异构小模型 profile 而非加倍调用同一大模型。**背书：**多个较小模型组成的评审团（PoLL）在三种评审设置、六个数据集上优于单一大模型评审，因跨模型家族的多样性显著降低单模型自增强偏差，且成本比单一大评审低 7 倍以上（Verga et al. [32]）——与本节「judge 独立于标注模型」是同一去偏原则的推广。

**stream 序列评审与缺陷表（v1.8，S7）。**stream 模式下序列信封（episode，`record.kind = "sequence"`，3.14）的评审改走序列变体；**非 stream 路径零改动**——本节既有模板与评审 Schema 是回归锚，序列信封由 stage 层旁路驱动器承载。输出经 `schema_engine.defect_verdict_schema()` 校验（3.8.1 内部 Schema 清单；与既有评审 Schema **并存**，S7）：三顶键 `{critiques, defects, verdict}` **全 required**（意见/缺陷在前、结论在后——本节「先意见后结论」同理）；`critiques` 形态与既有评审 Schema 逐字节一致（原样走既有合并/回喂链路）；`defects` 逐项 `{kind, members, position, detail}` 四子键全 required，可选性以可空联合 `["array","null"]` / `["string","null"]` 表达（OpenAI strict 兼容，3.8.1）。缺陷 `kind` 六值封闭词表（v1.8 五值 + v1.9 增 `wrong_stitch`——defect Schema、DEFECT_KINDS、report `_DEFECT_KINDS`、`_route_defects` 四处同步扩值，3.8.1/6.4）：

| kind | 语义 |
|---|---|
| `label_mismatch` | 标注的任务标签与序列证据不符。 |
| `off_task_members` | 段内混入与任务无关的成员帧（`members` 列出这些成员帧的非负整数出现位置）。 |
| `missing_head` / `missing_tail` | 段首缺少任务起点帧 / 段尾缺少任务终点帧（结合边界余量判断）。 |
| `missing_members` | 段中缺失成员帧（`members` 列出可指认的非负整数出现位置，无从指认则为 null）。 |
| `wrong_stitch` | v1.9：线索的某处缝合是错误的——某碎片与线索其余部分不属同一目标导向任务（结合 `[片段结构]` 判断；`position` 指向可疑碎片的线索内序数）。仅 stitch 启用时可判（3.16）。 |

评审证据依次包含 `[任务指令]`、全部成员的出现位置与完整文本或 UI 树、全部成员图片、已有完整 `[动作序列]`、启用 stitch 时的 `[片段结构]`、`[边界余量]` 和完整 `[标注结果]`。每个成员的正文由 `record_evidence` 渲染，UI 树使用 `serialize(None)`；图片使用惰性 ImageRef 引用，预检不加载字节。动作保留每一步完整 action JSON，不能以摘要、首尾帧或关键帧代替真实证据。碎片按显式 `member_positions` 列举成员，并展示接缝；边界余量只引用允许范围内段外前后各两帧的完整文字、原图片和真实出现位置，标明 noise、相邻有效 episode 或无归属，过滤 stitched 壳。人工容量边界另外标明对应出现位置；切点外帧的正文和图片均不加入请求。

`VerificationResult.defects` 在 process sequence 中恒在，无缺陷为空；缺陷摘要随 `verify.verdict` 事件输出并受 trace.content 分级。`verdict="fail"` 且 defects 为空时归一化为默认 `label_mismatch`，保证失败有明确路由依据。`defects.members` 只接受会话内非负整数出现位置；内容 ID 不能定位成员，因为相同内容可以重复出现。

**上下文预算。**普通单记录继续使用既有 UI 树预算装填，生成序列的 verdict 路径遵循本章独立生成契约。process sequence 不裁剪成员、图片、步骤或标注：一次构建完整 prompt，按每个 judge 的实际模板、Schema、图片成本和 profile 独立预检，所有 judge 均可容纳才提交整轮。`VerifyStage.preview_capacity` 对当前已知成员、已有动作与 annotation 执行相同检查，未知派生产物不伪造。实际请求前和响应终检的 `context_overflow` 统一交给会话控制器，不能降为普通评审失败或预算裁剪。

### 3.7.3 失败策略与修复环

| 策略 | 行为 |
|---|---|
| `verify.policy = "drop"`（默认） | fail ⇒ `status="dropped_verify"`，批评意见摘要入 `_meta.verification` 与 rejects 通道。 |
| `verify.policy = "repair"` | fail ⇒ 将批评意见追加进标注提示词（`[上一版标注] ... [审核意见] ... 请修正后重新输出`），M5 重标注、M8 重校验、M7 重评审；最多 `verify.max_repair_rounds`（默认 1）轮，仍 fail 按 drop 处理。评审轮数记入 `_meta.verification.rounds`（含首评，一次通过 =1；修复后复评 =2），各轮意见按序累积于 `VerificationResult.critiques`（4.2），实例见 3.7.4。 |

**stream 修复路由：严格波次（v1.19）。**`policy = "repair"` 下序列信封按缺陷表路由标签重标、成员收缩
与成员回收。实现位于 `labelkit/operators/stream_verify.py`；`verify.py` 保留 classic judge/repair 核心。每轮冻结整会话各波次的全部叶任务，再由 `ctx.run_group` 按 `batch_size` 分组执行；全部分组收齐后只执行一次 reducer。
严格执行下列屏障，禁止把存在数据依赖的相邻波次合并为一个任务组：

图 3-8 普通流评审的严格修复波次。每一波完整收齐并按声明序归并后，才能进入下一波；需修复的序列最终进入下一轮评审。

| 波次 | 叶任务 | 冻结 ordinal reducer |
|---|---|---|
| review | 每个 episode × judge 独立返回 verdict/critiques/defects | 按批位置、judge ordinal 合成评审团结果与确定性 defects 并集 |
| route | 无 | 按出现位置执行 off_task_members 收缩；先按容量允许范围筛选 noise-pool 候选，再冻结 claim；邻段已持有帧仅标记，禁止夺帧；无候选标 capture_gap；恰好指向实际人工边界的头尾疑点标 suspected=capacity |
| claim | 每个冻结 claim 通过 `segment.judge_window` 复裁，只返回 relation outcome | 按 episode、defect、candidate ordinal 更新唯一 claim table；只有 `{continues, advances}` 的首个声明序 claim 可执行 `dropped_noise → absorbed`，其余只标记 |
| reseam | 每个手术触点通过 `extract.extract_transition` 返回重摘 outcome | 按触点 ordinal 重建 Record 与 transitions；序列 id 不重算，`Transition.index` 恒等元组下标且 `len(transitions) = len(members) − 1` |
| frame-classify | 仅为回收后缺位成员执行单成员 `classify_frames` | 按成员 ordinal 补写既有 member classification map |
| frame-annotate | 仅在帧分类 reducer 完成后为缺位成员执行 `annotate_member` | 按成员 ordinal 与最新帧类补写既有 member annotation map |
| reannotate | 对需修复的 episode 调用 `annotate_record`，只返回新 annotation outcome | 按批位置写入 annotation、verification 与事件；随后进入下一轮 review |

所有叶任务只返回冻结 outcome，不得修改 claim table、PipelineItem、episode/member map、events 或 counters，也
不得嵌套 `run_group()`。TaskExecutor 必须等任务组及 cleanup 完整收敛后才进入 reducer。ordinary
ProviderFatal 由叶调用转换为既有记录级 outcome，不取消 sibling；CircuitBreaker、CancelledError 或逃逸的
internal/control 异常结构化取消当前 execution domain。

**帧产物同步（v1.12）**：reseam reducer 重建之后、reannotate 波次之前，对本轮执行了成员手术的 episode
同步两个帧产物 dict（`member_classifications` / `member_annotations`，4.1）——手术改了成员集，帧产物必须随
成员集走，否则 members[] 落盘时出现无主条目或缺帧：

- **收缩删键**：从两个帧产物 dict 删除不再属于当前 `member_positions` 的键，包含值为 None 的失败占位。仅触碰非 None dict，保留 dict 对象，扇出共享引用不会产生无主条目。
- **回收补跑**（幂等只补缺位）：新入 `record.members` 且键缺位的成员依次经过独立的 frame-classify
  TaskGroup 与 reducer、frame-annotate TaskGroup 与 reducer。`frame.classify.enabled` 且 dict 非 None 时，
  单成员窗口失败落 `fallback_class`；`frame.annotate.enabled` 且 dict 非 None 时，标注必须读取前一 reducer
  的新鲜帧类。帧类视图 `enabled=false` 的成员跳过且不占键；frame classify 关闭时 label=None 走全局指令；
  不可修复的帧标注占键 None。
- **dict None 全程不触碰**：dict 为 None = 帧 pass 未运行（降格会话 / 帧粒度关闭 / 非首标签），收缩与补跑均不触碰——降格语义保持、永不无中生有。
- **克隆不写共享帧产物**：multi 扇出克隆的 membership 类手术只标记，仅首标签原信封可执行。克隆可因接缝依赖重建自己的步骤、重标注和复评；这一路径必须跳过共享帧产物同步，不能按克隆的旧成员删除原信封新回收帧的产物，也不能重复补跑。懒加载直调面包含 `classify.classify_frames` 与 `annotate_member`（CONTRACTS §1.1）。

**wrong_stitch 路由（v1.9，独立分支）**：**只标记、不拆线**——自动拆线手术是 v1.9 非目标（8.1），本缺陷不进上述三类手术路由，尤其**不得落入 missing_\* 的噪声池回收扫描**（错缝的修复方向是移除碎片而非补帧，回收扫描会反向加重错缝）；repair 轮内不为其执行任何成员手术（重标注亦不能修复错缝），持续 fail 按 drop 收尾（`dropped_verify`——错缝线索 fail-closed 不入主输出）；计数入 `verify.defects.wrong_stitch`（6.4）。成员手术的回收扫描语义不变（异线索 absorbed 帧按既有 D5 邻域判定已是 neighbor mark-only，缝合不改变其结论）。

修复轮数计入 `verify.max_repair_rounds`（含首评，与本节非 stream 语义一致）。状态改写授权：手术在 `absorbed` 与 `dropped_noise` 间**双向**改写成员信封状态——4.3 契约 ②b 的 M7 修复路径豁免（契约①的唯一反向豁免），**禁止翻回 `active`**（帧与其 episode 不得双写主输出）。其余裁决：multi 扇出克隆兄弟的 membership 类手术**只标记**——仅原信封（首标签）可执行（S8，3.13.4 multi × episode 行）；多评审团下 defects = 投 fail 的 judge 的**并集**，按 (kind 枚举序, position, members) 确定性去重排序，同成员的互斥手术取先序（S31）；修复后**不重打分**——沿用修复前质量分 + `_meta.stream.repaired = true` 标记（6.3；multi 下亦用于消歧同 id 兄弟行）。观测面（M7 属主，`report.stream.verify` 子块，6.4）：`verify.membership_repairs`（执行的手术数）、`verify.boundary_flags`（只标记的边界判定数）、`verify.defects.<kind>`（逐缺陷类型计数）。

**修复路径与上下文预算。**回收复裁窗由完整相邻成员与候选组成，运行前检查真实窗口，不能依赖静态三帧保证。重标注使用全部工作成员、全部图片、全部现有步骤和真实审核意见，不降采样、不尝试图像阶梯、不缩小分辨率。评审、复裁、重摘、帧补分类、帧补标注和重标注的容量失败均归属 verify，携带扩展后的工作目标及实际请求位置上抛。整个 verify 阶段恢复开始时的成员、帧状态、共享帧产物、annotation 和 claims；会话控制器只重算未提交状态，已记录的最小终态由目标 stage/profile/label/positions 消费，禁止重复发送同一失败请求。

候选生成和 claim 提交前都校验半开容量范围，重绑成员前再检查所有工作位置；任何一次越界均不得提交。仅在当前实际段首对应 before 切点、或段尾对应 after 切点时，missing_head/missing_tail 可标 `suspected="capacity"`；具名成员必须恰好是切点另一侧位置。此类疑点不回收、不独立 fail，其余真实缺陷仍照常裁决。

手术后按出现位置投影碎片。保留成员保持原碎片归属；回收成员归入其前邻原成员的碎片，段首无前邻时归入后邻原成员的碎片。删除空碎片后按各碎片首个剩余出现位置重新排序，保留 cause、source_episode 和固定键序。多碎片夹缝以此规则消除归属歧义，不能从成员计数或内容 ID 猜测。接缝根据真实出现位置缺口重建，中断名读取同会话各线索内部保存的 `stitch_task_name`，不新增用户输出字段。成员归属只取未分类序列或分类命中集首标签；其余标签视图只消费成员。与当前视图具有同一 record.id 的线索不能形成自身中断，独立线索即使任务名相同仍是真实中断。

**接缝依赖闭合。**本轮成员手术成功或回滚之后，按最终成员归属扫描全部已评审台账。若其他序列的接缝或中断名改变，复用同一重摘、重绑、重标注波次，再复评该序列，不能只修改元数据。已有 max_repair_rounds 预算不重置；无剩余预算时明确 dropped_verify，不能交付消费旧步骤的 annotation。例如 A=[0,4]、B=[2,5]，B 移除位置 2 后 A 必须重新摘取 0→4、重标注并复评。依赖修复容量失败时，整阶段快照同时恢复 A、B、原帧和所有认领。

**整波容量归集。**每个评审团的同步预检、跨序列同步计划，以及 review、claim、reseam、frame-classify、frame-annotate、reannotate 各波均先收齐全部物理计算分组，再按叶任务声明序归集全部容量问题。嵌套 SessionCapacityError.failures 保留顺序扁平传递；任何业务结果归并之前统一上抛非空失败元组，会话控制器一次重算消费全部已知失败，禁止同一已失败请求在下一尝试原样重发。

最小失败证据保留实际请求位置。frame 的终态门按出现位置精确隔离；transition 中不可拆的必要相邻对使当前序列视图无法完成，因此其终态在相同 stage、profile、lineage、record.id 和 label 上阻断该视图的所有相邻对请求。其他子序列、分类视图、阶段或 profile 不受此门影响；不能把失败证据里的原相邻对改写成整序列成员。

**Schema 修复证据。**process session 的 CallScope.complete_evidence 为 true。L3 修复保留原完整成员、图片、任务与 Schema，再附上一版模型字段和违规清单；容量错误原样交回 verify，不能改写为 SchemaViolation。上游容量预览只检查当前已知证据，实际邻帧、annotation 或修复意见出现后再执行完整请求检查。

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

### 3.7.5 v1.20 sequence attempt 评审

process stream 的 episode 继续使用 3.7.2 缺陷表与成员手术。v1.20 生成 sequence 是 text 模态的完整
主序列候选，使用既有 verdict Schema 的判决形模板；两者按调用入口区分，不以
`segment.enabled` 猜测。

~~~python
@dataclasses.dataclass(frozen=True)
class VerifyPromptOptions:
    """一次评审提示词的全部可选装配项。"""

    label: str | None = None
    transitions: tuple | None = None
    boundary_margin: str = ""
    fragment_structure: str = ""
    fit: "_PromptFit | None" = None
    verdict_form: bool = False


class VerifyStage(Stage):
    """标注评审阶段。"""

    async def run_attempt(
        self,
        request: DownstreamAttemptRequest,
    ) -> DownstreamAttemptResult:
        """在 sequence attempt 内评审完整候选，不提交全局状态。"""
~~~

生成 sequence 的判决形 system 检查任务指令遵循、成员 payload 证据一致性与标注字段语义；user 段固定为
任务指令、按 event order 的完整成员摘要、标注结果。它不生成 process episode 的 boundary/fragment defect 表。
摘要可以按既有非真值装填规则裁剪，但 annotation、generation truth 与 evaluator 结果不能裁剪后继续判 pass。

`run_attempt` 对同一 `AttemptTransaction` 中全部 variant 使用 projector 写入的 inherited sequence class
选择 `ClassView` 与类有效 Schema；不能因 classify stage 关闭而回落匿名视图。repair 仍调用 M5 的同一
attempt-local 标注核心。任何一个 variant 最终 fail 都返回 rejected stage = verify，整个 counterfactual set
连同 main/stream 投影一起丢弃；不得留下 stream truth、rejects 行或部分 dataset counter。

sequence 配置强制 segment disabled，因此 `run_attempt` 只能走 classic 路径。每个 round 先把全部
item × judge 冻结为 judge wave，按 item/judge ordinal reducer 合成 verdict；只有 reducer 冻结需修复集合后，
才能启动 repair annotation wave，归并完成后进入下一 round。judge 与 repair 波次不得合并，叶任务不得写
PipelineItem、共享 critiques 或 verification；attempt dataset counters 只在最终 sequence commit 后合并。

`ProviderFatalError`、`CircuitBreakerTripped`、`KeyboardInterrupt` 与 `asyncio.CancelledError`
原样穿透到 `SequenceWorkflow`，取消 execution domain 且不消耗 slot attempt。ProviderRetryableError、SchemaViolation、
ContextOverflowError、OutputTruncatedError 与普通 verdict fail 返回 `accepted=false` 并消耗当前 attempt。
若 attempt 路径出现新增的 provider-fatal `item.errors`，属于 `generation_downstream_contract`
内部错误，不得当作可重试拒绝。

sequence class 声明 annotation time binding 时，首次标注冻结的 `SequenceTemporalContext` 必须贯穿 judge wave、
repair annotation wave 与复审。每轮 repair 仍只把上一版 model-space annotation 与 critiques 放入 prompt；
`repair_projector` 删除 business time leaf，provider 与 L3 不读取机械值。M5 finalizer 在同一个 temporal context 上重新
注入 `first_resource_start_milliseconds`，再跑完整 class Schema 与 L2.5。替换、遗漏 context，或让 verify 从 main
metadata、member payload、wall clock 推断时间，都是 internal contract error。stream verify 的 reannotate leaf 同样调用
该统一 finalizer；M7 不新增第二套时间修复代码。

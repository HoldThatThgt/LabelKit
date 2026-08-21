# 8. 非目标、设计假设与演进路线

## 8.1 非目标

见 2.1.2 工具级负边界；另注：不承诺跨批分数可比性（pairwise 为批内相对分，3.4.3）；不做多机分布式（单机并发已被 API 限速主导）。

v1.9（线索缝合域）明确不做六条：① **真并发**（同屏双任务/分屏）——单前台屏无真并发 [65]，帧单一归属是手术/归因/守恒的公共地基（帧多标签先例 [72] 经评估否决）；② **跨会话/跨运行缝合**——线索作用域不跨 session、不跨 batch（3.16.4），无持久化红线不变（2.6）；③ **帧级区间树与子任务嵌套校验**——episode 内子任务跨度经需求方 2026-07-16 裁决不做引擎特性（标注层模式：用户 Schema 自声明 `subtasks: [{label, step_range}]`，工具不校验其语义）；④ **自动拆线手术**——verify 对错缝只标 `wrong_stitch` 不重构（3.7.3）；⑤ **在线/增量处理**——批处理工具定位不变；⑥ **完成感知封闭**（收尾动作模式触发线索封闭）——链序上 extract 后置，缝合运行时无动作证据、「机械收尾动作模式」不可判而撤除（3.16.4 ①）；若未来「extract 先行」次序演进（8.4 M14 行候选）使动作证据先行，可重评（8.4 M16 行演进候选）。

v1.10（console 域）明确不做三条：① **web/hosted viewer**——数据只去配置声明的 LLM 端点、无遥测红线（2.6）；② **面板内数据内容检视**——trace `excerpt`/`full` 档职责（7.4；面板信息纪律 U6/U22 红线，7.7）；③ **跨运行历史面板/持久化仪表**——无状态原则（2.6）。

v1.12（帧粒度域）明确不做七条：① **帧级 quality/dedup/verify/generate**——帧粒度仅分类与标注两面（成员帧的治理由 segment 噪声剔除与 episode 级质量门承担）；② **帧多标签与帧级扇出**——帧单一归属是手术/归因/守恒的公共地基（`[frame.classify]` 无 `assignment`，显式书写定向 CONFIG_ERROR，3.1.4）；③ **帧级 L2.5 回调**——`output.validator` 仅约束序列级用户 Schema 调用（演进候选 `frame.annotate.validator`，8.4 M5 行）；④ **帧级 self_consistency**——成本 ×n 且投票键须取自帧 Schema 需动投票主干（`[frame.annotate]` 无该键，显式书写定向 CONFIG_ERROR）；⑤ **摘要行帧标签回填**——演进候选，爆炸半径已勘明（8.4 M13 行）；⑥ **同内容帧标注备忘录**——演进候选（8.4 M5 行）；⑦ **按序列类分叉的帧类表**——帧类表全局一份，与序列类表相互独立、允许重名、互不约束（5.2）。

v1.18（sequence generation 域）明确以下负边界：

- v1.18 是 clean breaking boundary。旧 `generate.stream`、tier、quota、frame rule/window、
  `time_fields`、brief/realize 与旧 validator 配置不保留别名、迁移器、兼容解析或运行期 fallback；
  未知旧键在 M1 定向报 `generation_config_invalid`。
- sequence 形态仅属于 text `generate_only`，不改变普通 process、flat generate、M2/M14/M15/M16
  的既有语义；UI sequence generation 仍不在本版范围。
- 生成运行不借 segment/stitch 重建真值。EventProjector 只产生 pre-downstream `ProjectedSequence`；
  M11 从最终 `PipelineItem` 装配 `SequenceRows`，ReplayProjector 再从最终 primary rows 产生 replay envelope。
  只有 stream 工件 replay 进入普通 process pipeline，且 generation provenance 不能作为分段或缝合 oracle。
- 不把 LLM content、world state、JSON Patch、ActorView、checkpoint 或 retry 状态写入普通日志、
  report、manifest 或跨运行存储；完整审计内容只允许进入用户显式开启的 `trace.content = "full"`。
- 不交付任何部分前缀。slot/noise exhaustion、provider fatal、circuit trip 与 SIGINT 都不能替换
  main、stream、成功 report 或 manifest；failed report 只诊断失败，不是可消费数据集。
- 不使用 main 作为 replay 的隐式旁输入；M2 必须仅凭同一 stream 工件验证 owner、event id 与
  replay provenance。完整 replay duplicate 由普通 M3 内容判重，不按 metadata 直接删除。
- 不以 mock transport、录制响应或本地 server 证明 LLM 路径；DeepSeek 与 z.ai 验收都调用真实端点。
- 不新增 generate 专属 trace 通道；七类生成调用继续经既有 `llm.call` 与 usage 面观测。

## 8.2 设计假设（若不成立需回到设计层）

| # | 假设 | 若不成立的影响 |
|---|---|---|
| A1 | UI 树导出为 6.2 可映射的 JSONL（平铺节点行或单行嵌套树）。 | 需在 M2 增加导出格式适配器（新增子模块，不影响其他模块）。 |
| A2 | 一个 uitree 文件 = 一屏（与一张截图对应）。 | 若一文件含多屏序列，需引入「屏内行号」扩展 index 语义（影响 3.2.4 与 6.2）。 |
| A3 | 所配 VLM 支持单请求多图（pairwise 比较需两组截图）。 | 不支持时 M4 在 UI 模态自动改用 criteria_per_call="single" + 两图拼接（Pillow 纵向拼接）——已在 M4 留有实现开关，默认不启用。 |
| A4 | 单次运行 ≤ 50 万条（2.6 内存模型）。 | 超出需 dedup.scope="batch" 或分目录多次运行。 |

## 8.3 开放问题（后续版本议题）

| # | 议题 | 现状与触发条件 |
|---|---|---|
| O1 | 语义去重（SemDeDup [26]，需 Embedding API） | 已于 v1.2 落地为可选第④级（3.3.3，`dedup.semantic`），本行保留作决策溯源；默认仍关闭（`dedup.semantic = false`），零 embedding 依赖的默认行为与 v1.0 一致。 |
| O2 | 跨批可比的 pairwise 分数（锚点样本法：每批混入固定锚点记录参与比较） | QuRating 原文以全局训练分类器回避该问题；运行时替代方案需实验验证后再纳入规格。 |
| O3 | UI 模态生成（以现有截图为底、仅生成指令/任务侧文本，AgentTrek 式轨迹合成 [15]） | v1.18 sequence generation 仅支持 text `generate_only`，由 projector 直接形成 sequence/main 与 event stream；普通 process stream 的 segment/stitch 互斥边界不变。截图侧生成未进入本版。 |
| O4 | 断点续跑 | 与「不存储中间态」冲突，明确排除；超大任务靠分目录运行缓解。 |
| O5 | `labelkit analyze` 子命令：读 trace.jsonl 产出标注质量分析 / rubric 诊断报告（自动计算 7.5 诊断指标、reason 关键词聚类） | 本版仅提供 jq 级手工分析（7.5）；trace 事件契约（7.2）稳定运行一个版本后立项。v1.10 注记（U16）：全屏交互 trace 浏览器品类与 textual 渲染库于本议题立项时一并重估（console 面板经验可迁移，`docs/dev/SPEC-tui-console.md` §5）。 |
| O6 | 普通 process 的全局精确定量（`output.target_count`，输出恰好 N 条） | v1.18 sequence 形态已经以 planned set/sequence slot + 有界重试实现全有或全无的 exact delivery：成功时 planned = delivered，耗尽时不提交前缀。该契约不外推到普通 process 或 flat generate；批间全局 top-K 仍受 O2 的跨批可比性前提约束。 |
| O7 | 多 API Key 负载均衡（单 profile 密钥池：最少在途轮换 / 每密钥 429 冷却 / 认证按密钥禁用 / 全池冷却有界驻留） | 已于 v1.6 落地（3.9.3 密钥池行、5.1 `api_key_envs`、5.2 `run.max_park_s`、7.2 三事件、6.4 keys 子块），本行保留作决策溯源（对齐记录见 1.6，2026-07-03）。触发条件即 8.1 所注「单机并发已被 API 限速主导」——无人值守长跑被单密钥用量限额中断。单密钥配置在数据产出、重试记账与熔断/退出语义上与 v1.5 一致（429 等待路径修订见 3.9.3 重试行）。业界同构：LiteLLM Router / 网关侧客户端多密钥轮换实践。**端点镜像池（多 base_url）经评审明确排除**：同 provider+model 的不同部署在 temperature=0 下仍有数值漂移（GPU kernel / batching 差异），会翻转 pairwise 裁决与语义去重边界判定、污染 7.5 同种子翻转率指标；如未来放开须先解决跨部署可比性。 |
| O8 | `stitch.judges` 多模型评审团扩展（缝合判定的跨家族多数决，镜像既有 `verify.judges` / `quality.judges` 模式作纯配置扩展） | v1.9 选型记录（1.6，2026-07-16 / T18）：本版采「**单模型多次**」（`stitch.votes`，self-consistency [33]）而非「多模型评审团」（PoLL [32]）——缝合的两类误差病理分工明确：漂移（方差病）→ votes 采样多数决；过连接（偏差病）→ 机械先验合取（3.16.4）。评审团修不了过连接：PIRA 消融显示过连接是**跨家族共享偏差**（GPT 系与 Gemini 系同向 trigger-happy [64]），异构裁判会把共享偏差投成多数 [86]，且实测评审团有效独立票仅 ≈2 [89]；当前部署纪律为单端点单模型。触发条件：第二模型家族进入部署面，且真机门禁审计显示漂移（而非过连接）是漏缝/错缝主体。 |

## 8.4 算子算法演进路线（robustness & diversity）

按模块汇总「现行算法 → v1.2 已收录的可选增强 → 演进候选」。v1.2 可选增强均默认关闭、不改变各模块默认行为（配置键规格见 5.1–5.2）；演进候选仅收录有顶刊论文或工业项目背书者，待触发条件出现后立项。

| 模块 | 现行算法（默认） | v1.2 已收录的可选增强 | 演进候选（背书 / 触发条件） |
|---|---|---|---|
| M3 去重 | 规范化 SHA-256 精确 + MinHash-LSH 近似（3.3）；UI 模态加 pHash | SemDeDup 语义级可选第④级 [26]：`dedup.semantic`（默认 false），开启后经 `dedup.semantic_embedding` 引用的 `[embedding.<name>]` profile 取向量，余弦相似度 ≥ `dedup.semantic_threshold`（默认 0.95）判重 | 子串级精确去重（Lee et al. [3] 的 suffix-array 变体，捕获行内重复长片段）；MinHash 参数自适应（按批内文本长度分布自动调 ngram / num_perm）。触发：短文本上 MinHash 误杀/漏杀率超预期。 |
| M4 打分 | pairwise+BT 与 pointwise 双模式（3.4），threshold 过滤 | 多评审团 `quality.judges`（默认 []；奇数个 profile 多数票 [32]）；双顺序裁决 `quality.both_orders`（默认 false，开启后每对正反两序各裁决一次以对消位置偏差 [20]，细节见 3.4.3）；批内定量优选 `quality.selection = "top_ratio"`（3.4.3） | O2 锚点跨批校准（每批混入固定锚点记录）；rubric 自动挖掘（CritiQ [31]，约 30 对人工偏好即可挖出可解释准则）；评审漂移监测（固定校准集定期回归，比对裁决一致率）。触发：需要跨批可比分数，或 rubric 迭代频繁。 |
| M5 标注 | 提示词组装单次标注（3.5.2）；v1.12 增帧级逐帧标注（3.5.5） | self-consistency 标注 `annotate.self_consistency`（默认 0=关；n≥3 且为奇数，以 `annotate.sc_temperature` = 0.7 采样 n 次、字段级多数票聚合 [33]） | best-of-n 拒绝采样（同一记录标注 n 条取评审 top-1，打分器思想同 FineWeb-Edu [11]）。触发：标注一致性仍不达标。**帧级 L2.5 回调 `frame.annotate.validator`**（v1.12 候选）：镜像 `output.validator` 的帧级同胞——挂接帧 Schema 调用的 L2.5、违规回喂修复环；现版帧标注走内部 Schema 待遇、无 L2.5（3.8.2 路由声明）。触发：帧标注出现 Schema 表达不了的业务约束。**同内容帧标注备忘录**（v1.12 候选）：同一运行内内容完全相同的成员帧（文本行 verbatim 重复 / 截图+树同签名）复用同一帧标注结果，省逐帧调用成本；与「无跨运行状态」红线（2.6）相容——备忘录仅进程内存。触发：帧标注成本成为瓶颈且重复帧占比可观测偏高。**按类 L2.5 回调**（v1.13 候选）：`output.validator` 现为全局唯一——按序列类标注 Schema 已可分叉（3.5.2），但回调仍是一份，类间业务约束不同时只能在回调内自行按 label 分支；候选 = `[class.<name>.annotate].validator` 的按类覆盖（白名单只增原则）。触发：按类 Schema 的使用工程出现 Schema 表达不了、且各类互不相同的业务约束。 |
| M6 生成 | flat 形态继续使用 Self-Instruct 式种子自举 [18]；v1.18 sequence 形态由 GenerationProgramCompiler 冻结 scenario/pattern/catalog，ScenarioPlan 固定 slot、branch、declared role、timestamp、NoiseSlot 与 ReplayLayout；instruction-only 只冻结 position/time，frame class 与 actor 由该次 EventPlan 唯一选择。随后以 state-aware EventPlan、frame render、机械/语义 evaluator、attempt-local 下游事务和 CrossViewReconciler 完成全有或全无交付 [115][126][128][129][130][131][132] | flat 形态保留多 LLM mixture 与 style 模板（3.6.2）；sequence 形态通过 `generate.form = "sequence"` 与 `sequence_generation` 单一配置面启用，不复用旧时间流配置 | Evol-Instruct 自动深化/扩展算子 [19] 仍只属于 flat 生成多样性路线；sequence 形态的发布边界由真实 DeepSeek、structured output、instruction-only、failure injection、replay 与人工真实感门共同冻结，不以旧 tier/quota/rule 系列继续演进。 |
| M7 校验 | 单 judge 独立评审 + 有界修复环 [20][21]（3.7） | 多评审团 `verify.judges`（默认 []；奇数个 profile 多数票，critiques 合并并标注来源 judge [32]，3.7.2） | 评审团分歧驱动的人工抽检队列（多数票非全票一致的记录进抽检清单，人机对齐界面思想出自 EvalGen [30]）。触发：评审团分歧率持续偏高。 |
| M8 结构 | L0–L3 四层防线：供应商结构化输出 + 确定性修复 + 有界 LLM 修复环（3.8） | —（v1.2 无新增） | 约束解码引擎本地化（Outlines / XGrammar 类 grammar 引擎 [23][24]）：自托管推理时以解码期硬约束替代当前面向 API 场景的四层防线。触发：迁移至自托管/本地推理栈。 |
| M13 分类（v1.7） | LLM 封闭集分类：类别表词表经内部 Schema enum 硬校验，单/多标签可配，失败归兜底类（3.13）；sequence generation 直接继承冻结的 sequence class，不发分类判决 | 可选 self-consistency 投票 `classify.self_consistency`（默认 0=关；n≥3 奇数，single 多数票 / multi 逐标签投票 [33]，3.13.4）；按类输出 Schema 已由 `[class.<name>.annotate].schema_path` / `schema_inline` 实现 | embedding 粗分 + LLM 精分两级分类降本；开放集 tagging 仅打标不路由；逐类适用度打分；多标签只打标不扇出；摘要行帧标签回填。各项只在对应真实需求与可观测瓶颈出现时进入独立规格，不改变 v1.18 sequence inherited-class 边界。 |
| M14 分段（v1.8） | gap/key/上限规则会话化（M2 会话流视图，3.2.8）+ 三步演绎滑窗裁决（双向上下文概括 → 五值封闭集关系分类 → 演绎查表映射边界/噪声；window=20、重叠 1 帧、确定性缝合，3.14）[47][48] | ~~`segment.use_vision`~~（**v1.11 移除**——窗口附图改由 `segment.llm` 所指 profile 的 `supports_vision` **能力推导**（parse product `vision_resolved`，3.14/V1）：树贫瘠场景的表达面 = 选 profile 即选能力 [63]，纯文本裁决 = 把 segment.llm 指向纯文本 profile；存量显式键定向 CONFIG_ERROR，V2）；`segment.context` 可选域上下文（非边界定义，判据模板内置零配置可用）；`segment.strategy="rules"` 零 LLM 纯规则档 | 有界乱序重排窗（k-帧滑窗重排，容忍采集端轻度乱序——现行为流式单调性校验 + `stream.on_disorder`；触发：真实输入出现轻度乱序，1.6 v1.8 决策 ⑬）；交错 episode（帧属并行任务而非噪声——RPA 交错例程难变体 [50]，需全局归属模型；触发：审计显示交错形态占噪声主体）；跨段边界仲裁（跨段搬帧修复，v1 只标记——代价是邻段级联重修与乒乓风险；触发：审计显示跨段形态占缺陷主体，S31）；**extract-先行次序**（先在会话内逐相邻对摘取、再在动作序列上一次分段——GUIDE / Watch & Learn / VideoAgentTrek / OpenCUA 的同行主流次序 [57][58][60][43]；以成本权衡裁决：dedup 前置节省 vs 分段证据质量，非环形依赖，S32；触发：帧摘要证据上的分段质量不达标）；嵌入变点检测（Embed-KCPD [47]：training-free 核变点检测，仅文本基准验证、GUI 流无先例，需 embedding profile；触发：LLM 分段调用成本成为瓶颈）；k>1 窗口重叠（重叠多帧多数决缝合压接缝误判；触发：接缝帧误判率可观测偏高）。 |
| M15 摘取（v1.8） | 相邻帧对 ⟨s_i, s_{i+1}⟩ LLM zero-shot 摘取（一请求 2 图 + OpenCUA 稳定帧锚定句 [42][43]）+ 树 diff 证据（结构键多重集匹配，代码侧确定性，3.15）；`action_type` 11 值词表 [45][62] | `extract.include_diff`（默认 true，可关做 A/B 消融——Sharingan 像素 diff 负结果、结构化树 diff 方向未定 [59]）；`extract.instruction` 域提示（`[class.<name>.extract]` 可按类覆盖） | 文本模态 extract（「转移摘要」弱语义档，v1 仅 UI 序列；触发：文本流工程出现真实需求，1.6 v1.8 决策 ⑦）；缺帧补全（Repairing Event Logs [51] 先验：缺失事件修复依赖跨轨迹习得的过程模型，v1 仅标记 `capture_gap`；触发：跨语料过程先验可用）；**本地 IDM profile**（专训逆动力学模型替代 zero-shot——Watch & Learn 实测专训 91.7% vs zero-shot 70.5% [58]，是「不训练/托管本地模型」负边界（2.1.2 ①）的已记录机会成本；触发：extract 错误率成为下游质量主瓶颈且允许自托管推理栈）；完成度末帧图（quality 轨迹打分的 completion 维度附末帧单图，+1 图/episode——忠于 OS-Genesis TRM 原型的输入配置（含末三帧截图）[41]；触发：完成度维与人工判定的失配集中于视觉终态证据）。 |
| M16 缝合（v1.9） | 单调选池 LLM 判定 × 机械先验合取（析取三腿 + stale-gap 降格，bias="conservative"）+ 有界二遍复评 + 短段救援 + 接缝机械占位（3.16）[64][74][87] | `stitch.votes`（默认 1=关；≥3 奇数，(verdict, thread_ref) 严格多数决 [33]——置信度门槛的正规替代 [79]，3.16.4）；`stitch.bias="llm"` 纯 LLM 消融档；`stitch.rescue_short` / `stitch.repass` 双开关；`stitch.stale_gap_steps` 时间衰减（双职：先验降格 + 逐出优先腿 [66][81]） | `stitch.judges` 多模型评审团（O8 选型记录——现版拒绝理由与触发条件见 8.3；镜像 `verify.judges` 纯配置扩展）；完成感知封闭（收尾动作模式触发封闭——B-1 撤除因 extract 后置无动作证据（8.1 ⑥）；触发：M14 行「extract-先行次序」候选落地使动作证据先行）；复评面扩展至多碎片线索（现版复评候选仅单碎片线索——双多碎片线索误分裂与池截取排除目标不在修复面内、由真机门禁兜底（3.16.4 残差声明）；触发：门禁审计显示该形态占漏缝主体）。 |
| 上下文预算（v1.11） | `context_window` 声明制（0=关）+ 零依赖启发式估算 + 动态贪心装填（条数参数降级为上限，w_min 静态护栏/估算上界）+ 图片成本测量-反应式三层（先验装填 → 溢出裁帧保清重试 → 判审裁帧升清重试 → usage 在线校准、批冻结快照）+ M9 咽喉终检（3.9，V6–V21） | `default_image_px` 图片采样工作点（默认 0 = 沿用 `max_image_px` 即 v1.10 行为；`max_image_px` 升格为升级天花板 + 像素制硬限制域，V18）；`context_window` 打折声明作通用 margin 放大器（唯一逃生门——margin / 估算系数 / 阶梯常数冻结于代码不开配置面，V7/V8/V18） | **运行中分母修正**（`metrics.run_estimate` 重复调用通道 / counters 每 tick 拉取通道——V12 已证实机械可行、v1 不用；触发：w_min 上界与 `report.stream.windows` 实际窗数长期偏差可观测）；**定向区域升清**（裁剪可疑区域而非整帧升清——Ferret-UI 双子图 / DirectX VRS foveation / AwaRes·MEGA-GUI 判审触发裁片升清，[C-52][C-56][C-67] 见 `docs/dev/PROPOSAL-context-budget.md`；触发：整帧升清仍不足以修复判审失败）；**per-profile 密度旋钮**（cl100k 旧词表中文 1.25–1.4 t/字不被 CJK×1.0 覆盖的记载局限；触发：该类 profile 部署出现且浪费/超窗可观测）；**输出侧预算**（`num_per_call` × 样本长 vs `max_output_tokens` 的输出预算；触发：`output_truncated` 桶占比可观测偏高）；**计数 API / usage 文本密度校准回路**（智谱 tokenizer API 抽样校准 + LangChain usage-scaling 同构，[C-63][C-71]；触发：文本估算偏差成为主要浪费源——cl100k 缺口的将来闭合路径）。 |

**v1.18 M6 现行算法冻结补充。**sequence 形态先把 program、catalog 与 ClassView 编译为不可变
GenerationProgram，再生成一次 ScenarioPlan；delivery 按声明序串行处理完整 counterfactual set，
每个 attempt 执行 scenario seed、baseline/variant EventPlan、state transition、frame render、
mechanical/semantic evaluation、prospective dedup、quality/annotate/verify 与 cross-view reconcile。
只有完整 set 通过才一次提交 dedup 与 dataset counters；任何 rejection 丢弃整个 attempt-local
transaction。slot 或 noise 耗尽即停止，保留旧成功 manifest，不交付已接受前缀；所有成功数据通道
只在全局 reconcile 后打开并以 manifest-last 顺序提交。M11 从下游后的最终行唯一计算 delivery digest；
ReplayProjector 从这些最终 primary rows 预投影 replay，retained-content prospective check 通过后才允许
group commit（6.4–6.5）。普通 process、flat generate 与 M14/M15/M16 算法不因本形态改变。

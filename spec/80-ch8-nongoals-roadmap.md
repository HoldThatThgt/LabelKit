# 8. 非目标、设计假设与演进路线

## 8.1 非目标

见 2.1.2 工具级负边界；另注：不承诺跨批分数可比性（pairwise 为批内相对分，3.4.3）；不做多机分布式（单机并发已被 API 限速主导）。

v1.9（线索缝合域）明确不做六条：① **真并发**（同屏双任务/分屏）——单前台屏无真并发 [65]，帧单一归属是手术/归因/守恒的公共地基（帧多标签先例 [72] 经评估否决）；② **跨会话/跨运行缝合**——线索作用域不跨 session、不跨 batch（3.16.4），无持久化红线不变（2.6）；③ **帧级区间树与子任务嵌套校验**——episode 内子任务跨度经需求方 2026-07-16 裁决不做引擎特性（标注层模式：用户 Schema 自声明 `subtasks: [{label, step_range}]`，工具不校验其语义）；④ **自动拆线手术**——verify 对错缝只标 `wrong_stitch` 不重构（3.7.3）；⑤ **在线/增量处理**——批处理工具定位不变；⑥ **完成感知封闭**（收尾动作模式触发线索封闭）——链序上 extract 后置，缝合运行时无动作证据、「机械收尾动作模式」不可判而撤除（3.16.4 ①）；若未来「extract 先行」次序演进（8.4 M14 行候选）使动作证据先行，可重评（8.4 M16 行演进候选）。

v1.10（console 域）明确不做三条：① **web/hosted viewer**——数据只去配置声明的 LLM 端点、无遥测红线（2.6）；② **面板内数据内容检视**——trace `excerpt`/`full` 档职责（7.4；面板信息纪律 U6/U22 红线，7.7）；③ **跨运行历史面板/持久化仪表**——无状态原则（2.6）。

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
| O3 | UI 模态生成（以现有截图为底、仅生成指令/任务侧文本，AgentTrek 式轨迹合成 [15]） | 本版生成仅文本模态；有明确需求后单独立项。v1.8 注记：stream × generate **互斥**（`segment.enabled` 要求 `generate.enabled=false`，2.3.1）——v1.8 引入序列记录后，序列/轨迹合成（AgentTrek 式 [15]）仍不并入本议题的现版范围、另行立项；届时须先裁决与 stream 模式的组合语义（互斥放开或串接）。 |
| O4 | 断点续跑 | 与「不存储中间态」冲突，明确排除；超大任务靠分目录运行缓解。 |
| O5 | `labelkit analyze` 子命令：读 trace.jsonl 产出标注质量分析 / rubric 诊断报告（自动计算 7.5 诊断指标、reason 关键词聚类） | 本版仅提供 jq 级手工分析（7.5）；trace 事件契约（7.2）稳定运行一个版本后立项。v1.10 注记（U16）：全屏交互 trace 浏览器品类与 textual 渲染库于本议题立项时一并重估（console 面板经验可迁移，`docs/dev/SPEC-tui-console.md` §5）。 |
| O6 | 全局精确定量与生成补齐回路（`output.target_count`，输出恰好 N 条） | 设计草案：两阶段流水线——第一阶段全量接入 + 去重 + 打分并缓存分数；第二阶段全局 top-K 选出恰好 target_count 条，再仅对选中集执行标注与输出。前提：分数具全局可比性——pointwise 绝对刻度，或 pairwise 经 O2 锚点法校准后方可全局排序。配生成补齐回路：target 未达时从高分种子生成 → 去重 → pointwise 质量门 → 计入，停止条件 = 达标 ∨ max_backfill_rounds ∨ 本轮合格率 < 下限（生成器饱和时合格率持续衰减，即递归自生成数据的 model collapse 现象 [36]，故合格率下限停止条件必不可少）。现状：2026-07-02 评审对齐为演进路线——本版以 `quality.selection = "top_ratio"` 提供流式批内近似定量（3.4.3），不承诺全局恰好 N 条。触发条件：出现「必须恰好 N 条」的下游需求。v1.7 注记：generate_only 的按类生成配比（每类 standalone_count）经对齐划归本议题——属量目标语义，与全局定量一并立项（1.6 v1.7 对齐决策 ③）；v1.7 的按类参数仅为加工条件化（2.1.2 ⑥）。 |
| O7 | 多 API Key 负载均衡（单 profile 密钥池：最少在途轮换 / 每密钥 429 冷却 / 认证按密钥禁用 / 全池冷却有界驻留） | 已于 v1.6 落地（3.9.3 密钥池行、5.1 `api_key_envs`、5.2 `run.max_park_s`、7.2 三事件、6.4 keys 子块），本行保留作决策溯源（对齐记录见 1.6，2026-07-03）。触发条件即 8.1 所注「单机并发已被 API 限速主导」——无人值守长跑被单密钥用量限额中断。单密钥配置在数据产出、重试记账与熔断/退出语义上与 v1.5 一致（429 等待路径修订见 3.9.3 重试行）。业界同构：LiteLLM Router / 网关侧客户端多密钥轮换实践。**端点镜像池（多 base_url）经评审明确排除**：同 provider+model 的不同部署在 temperature=0 下仍有数值漂移（GPU kernel / batching 差异），会翻转 pairwise 裁决与语义去重边界判定、污染 7.5 同种子翻转率指标；如未来放开须先解决跨部署可比性。 |
| O8 | `stitch.judges` 多模型评审团扩展（缝合判定的跨家族多数决，镜像既有 `verify.judges` / `quality.judges` 模式作纯配置扩展） | v1.9 选型记录（1.6，2026-07-16 / T18）：本版采「**单模型多次**」（`stitch.votes`，self-consistency [33]）而非「多模型评审团」（PoLL [32]）——缝合的两类误差病理分工明确：漂移（方差病）→ votes 采样多数决；过连接（偏差病）→ 机械先验合取（3.16.4）。评审团修不了过连接：PIRA 消融显示过连接是**跨家族共享偏差**（GPT 系与 Gemini 系同向 trigger-happy [64]），异构裁判会把共享偏差投成多数 [86]，且实测评审团有效独立票仅 ≈2 [89]；当前部署纪律为单端点单模型。触发条件：第二模型家族进入部署面，且真机门禁审计显示漂移（而非过连接）是漏缝/错缝主体。 |

## 8.4 算子算法演进路线（robustness & diversity）

按模块汇总「现行算法 → v1.2 已收录的可选增强 → 演进候选」。v1.2 可选增强均默认关闭、不改变各模块默认行为（配置键规格见 5.1–5.2）；演进候选仅收录有顶刊论文或工业项目背书者，待触发条件出现后立项。

| 模块 | 现行算法（默认） | v1.2 已收录的可选增强 | 演进候选（背书 / 触发条件） |
|---|---|---|---|
| M3 去重 | 规范化 SHA-256 精确 + MinHash-LSH 近似（3.3）；UI 模态加 pHash | SemDeDup 语义级可选第④级 [26]：`dedup.semantic`（默认 false），开启后经 `dedup.semantic_embedding` 引用的 `[embedding.<name>]` profile 取向量，余弦相似度 ≥ `dedup.semantic_threshold`（默认 0.95）判重 | 子串级精确去重（Lee et al. [3] 的 suffix-array 变体，捕获行内重复长片段）；MinHash 参数自适应（按批内文本长度分布自动调 ngram / num_perm）。触发：短文本上 MinHash 误杀/漏杀率超预期。 |
| M4 打分 | pairwise+BT 与 pointwise 双模式（3.4），threshold 过滤 | 多评审团 `quality.judges`（默认 []；奇数个 profile 多数票 [32]）；双顺序裁决 `quality.both_orders`（默认 false，开启后每对正反两序各裁决一次以对消位置偏差 [20]，细节见 3.4.3）；批内定量优选 `quality.selection = "top_ratio"`（3.4.3） | O2 锚点跨批校准（每批混入固定锚点记录）；rubric 自动挖掘（CritiQ [31]，约 30 对人工偏好即可挖出可解释准则）；评审漂移监测（固定校准集定期回归，比对裁决一致率）。触发：需要跨批可比分数，或 rubric 迭代频繁。 |
| M5 标注 | 提示词组装单次标注（3.5.2） | self-consistency 标注 `annotate.self_consistency`（默认 0=关；n≥3 且为奇数，以 `annotate.sc_temperature` = 0.7 采样 n 次、字段级多数票聚合 [33]） | best-of-n 拒绝采样（同一记录标注 n 条取评审 top-1，打分器思想同 FineWeb-Edu [11]）。触发：标注一致性仍不达标。 |
| M6 生成 | Self-Instruct 式种子自举生成 [18]（3.6.2） | 多 LLM 混合 `generate.llms`（数组，`generate.mixture` = round_robin \| weighted）+ `[[generate.styles]]` 风格模板（3.6.2；persona / 受众×风格分桶的多样性思想 [34][35]） | Evol-Instruct 自动深化/扩展算子 [19]（对种子指令做复杂化与广度改写）。触发：生成多样性或难度分布不足。 |
| M7 校验 | 单 judge 独立评审 + 有界修复环 [20][21]（3.7） | 多评审团 `verify.judges`（默认 []；奇数个 profile 多数票，critiques 合并并标注来源 judge [32]，3.7.2） | 评审团分歧驱动的人工抽检队列（多数票非全票一致的记录进抽检清单，人机对齐界面思想出自 EvalGen [30]）。触发：评审团分歧率持续偏高。 |
| M8 结构 | L0–L3 四层防线：供应商结构化输出 + 确定性修复 + 有界 LLM 修复环（3.8） | —（v1.2 无新增） | 约束解码引擎本地化（Outlines / XGrammar 类 grammar 引擎 [23][24]）：自托管推理时以解码期硬约束替代当前面向 API 场景的四层防线。触发：迁移至自托管/本地推理栈。 |
| M13 分类（v1.7） | LLM 封闭集分类：类别表词表经内部 Schema enum 硬校验，单/多标签可配，失败归兜底类（3.13） | 可选 self-consistency 投票 `classify.self_consistency`（默认 0=关；n≥3 奇数，single 多数票 / multi 逐标签投票 [33]，3.13.4） | embedding 粗分 + LLM 精分两级分类降本（粗分近邻筛候选、LLM 只精判边界样本；触发：分类调用成本成为瓶颈）；开放集 tagging 仅打标不路由（InsTag 形态 [38]，标签进 `_meta` 供多样性/复杂度分析；触发：需要超出静态类表的语义分析）；按类输出 Schema（触发：出现单 Schema `oneOf` 条件子模式无法表达的真实工程——现版输出 Schema 全局唯一，5.2 白名单表尾行）；逐类适用度打分档（0–5 分 + 阈值筛命中集合，替代集合判断；触发：需要可调的多标签灵敏度）；多标签仅打标不扇出档（labels 全集进 `_meta`、仍按首标签走单条管线，`assignment` 枚举留扩展位；触发：出现「要全集标签、不要多行输出」的真实场景，1.6 v1.7 对齐决策 ⑥）。 |
| M14 分段（v1.8） | gap/key/上限规则会话化（M2 会话流视图，3.2.8）+ 三步演绎滑窗裁决（双向上下文概括 → 五值封闭集关系分类 → 演绎查表映射边界/噪声；window=20、重叠 1 帧、确定性缝合，3.14）[47][48] | ~~`segment.use_vision`~~（**v1.11 移除**——窗口附图改由 `segment.llm` 所指 profile 的 `supports_vision` **能力推导**（parse product `vision_resolved`，3.14/V1）：树贫瘠场景的表达面 = 选 profile 即选能力 [63]，纯文本裁决 = 把 segment.llm 指向纯文本 profile；存量显式键定向 CONFIG_ERROR，V2）；`segment.context` 可选域上下文（非边界定义，判据模板内置零配置可用）；`segment.strategy="rules"` 零 LLM 纯规则档 | 有界乱序重排窗（k-帧滑窗重排，容忍采集端轻度乱序——现行为流式单调性校验 + `stream.on_disorder`；触发：真实输入出现轻度乱序，1.6 v1.8 决策 ⑬）；交错 episode（帧属并行任务而非噪声——RPA 交错例程难变体 [50]，需全局归属模型；触发：审计显示交错形态占噪声主体）；跨段边界仲裁（跨段搬帧修复，v1 只标记——代价是邻段级联重修与乒乓风险；触发：审计显示跨段形态占缺陷主体，S31）；**extract-先行次序**（先在会话内逐相邻对摘取、再在动作序列上一次分段——GUIDE / Watch & Learn / VideoAgentTrek / OpenCUA 的同行主流次序 [57][58][60][43]；以成本权衡裁决：dedup 前置节省 vs 分段证据质量，非环形依赖，S32；触发：帧摘要证据上的分段质量不达标）；嵌入变点检测（Embed-KCPD [47]：training-free 核变点检测，仅文本基准验证、GUI 流无先例，需 embedding profile；触发：LLM 分段调用成本成为瓶颈）；k>1 窗口重叠（重叠多帧多数决缝合压接缝误判；触发：接缝帧误判率可观测偏高）。 |
| M15 摘取（v1.8） | 相邻帧对 ⟨s_i, s_{i+1}⟩ LLM zero-shot 摘取（一请求 2 图 + OpenCUA 稳定帧锚定句 [42][43]）+ 树 diff 证据（结构键多重集匹配，代码侧确定性，3.15）；`action_type` 11 值词表 [45][62] | `extract.include_diff`（默认 true，可关做 A/B 消融——Sharingan 像素 diff 负结果、结构化树 diff 方向未定 [59]）；`extract.instruction` 域提示（`[class.<name>.extract]` 可按类覆盖） | 文本模态 extract（「转移摘要」弱语义档，v1 仅 UI 序列；触发：文本流工程出现真实需求，1.6 v1.8 决策 ⑦）；缺帧补全（Repairing Event Logs [51] 先验：缺失事件修复依赖跨轨迹习得的过程模型，v1 仅标记 `capture_gap`；触发：跨语料过程先验可用）；**本地 IDM profile**（专训逆动力学模型替代 zero-shot——Watch & Learn 实测专训 91.7% vs zero-shot 70.5% [58]，是「不训练/托管本地模型」负边界（2.1.2 ①）的已记录机会成本；触发：extract 错误率成为下游质量主瓶颈且允许自托管推理栈）；完成度末帧图（quality 轨迹打分的 completion 维度附末帧单图，+1 图/episode——忠于 OS-Genesis TRM 原型的输入配置（含末三帧截图）[41]；触发：完成度维与人工判定的失配集中于视觉终态证据）。 |
| M16 缝合（v1.9） | 单调选池 LLM 判定 × 机械先验合取（析取三腿 + stale-gap 降格，bias="conservative"）+ 有界二遍复评 + 短段救援 + 接缝机械占位（3.16）[64][74][87] | `stitch.votes`（默认 1=关；≥3 奇数，(verdict, thread_ref) 严格多数决 [33]——置信度门槛的正规替代 [79]，3.16.4）；`stitch.bias="llm"` 纯 LLM 消融档；`stitch.rescue_short` / `stitch.repass` 双开关；`stitch.stale_gap_steps` 时间衰减（双职：先验降格 + 逐出优先腿 [66][81]） | `stitch.judges` 多模型评审团（O8 选型记录——现版拒绝理由与触发条件见 8.3；镜像 `verify.judges` 纯配置扩展）；完成感知封闭（收尾动作模式触发封闭——B-1 撤除因 extract 后置无动作证据（8.1 ⑥）；触发：M14 行「extract-先行次序」候选落地使动作证据先行）；复评面扩展至多碎片线索（现版复评候选仅单碎片线索——双多碎片线索误分裂与池截取排除目标不在修复面内、由真机门禁兜底（3.16.4 残差声明）；触发：门禁审计显示该形态占漏缝主体）。 |
| 上下文预算（v1.11） | `context_window` 声明制（0=关）+ 零依赖启发式估算 + 动态贪心装填（条数参数降级为上限，w_min 静态护栏/估算上界）+ 图片成本测量-反应式三层（先验装填 → 溢出裁帧保清重试 → 判审裁帧升清重试 → usage 在线校准、批冻结快照）+ M9 咽喉终检（3.9，V6–V21） | `default_image_px` 图片采样工作点（默认 0 = 沿用 `max_image_px` 即 v1.10 行为；`max_image_px` 升格为升级天花板 + 像素制硬限制域，V18）；`context_window` 打折声明作通用 margin 放大器（唯一逃生门——margin / 估算系数 / 阶梯常数冻结于代码不开配置面，V7/V8/V18） | **运行中分母修正**（`metrics.run_estimate` 重复调用通道 / counters 每 tick 拉取通道——V12 已证实机械可行、v1 不用；触发：w_min 上界与 `report.stream.windows` 实际窗数长期偏差可观测）；**定向区域升清**（裁剪可疑区域而非整帧升清——Ferret-UI 双子图 / DirectX VRS foveation / AwaRes·MEGA-GUI 判审触发裁片升清，[C-52][C-56][C-67] 见 `docs/dev/PROPOSAL-context-budget.md`；触发：整帧升清仍不足以修复判审失败）；**per-profile 密度旋钮**（cl100k 旧词表中文 1.25–1.4 t/字不被 CJK×1.0 覆盖的记载局限；触发：该类 profile 部署出现且浪费/超窗可观测）；**输出侧预算**（`num_per_call` × 样本长 vs `max_output_tokens` 的输出预算；触发：`output_truncated` 桶占比可观测偏高）；**计数 API / usage 文本密度校准回路**（智谱 tokenizer API 抽样校准 + LangChain usage-scaling 同构，[C-63][C-71]；触发：文本估算偏差成为主要浪费源——cl100k 缺口的将来闭合路径）。 |

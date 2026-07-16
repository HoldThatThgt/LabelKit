# 特性开发规格：时序流语义分割与动作摘取（spec v1.8）

> 2026-07-13。本文件是 **v1.8 特性的开发规格（implementation-ready）**：需求与业界论证见 `PROPOSAL-stream-segmentation.md`（本文不重复）；本文在提案基础上并入七域 fan-out 可行性审查（78 条发现，0 blocker）与两路深检索（refute：0 refuted / 7 weakened / 1 holds；elevate：29 项钉死事实）的全部裁决，给出规范文本、完整文件修改清单与开发计划。
> **状态：已实现（2026-07-14，v1.8 合入 main）。**§5 清单已全部落地；S1–S32 裁决全部生效（三轮对抗审计 29–31/32 confirmed，余项已修复）；合入前对抗代码评审的 7 项发现（D1–D7，2 medium / 5 low）已全部修复——台账见 `E2E-FINDINGS.md` #12/#13。本文档保留为 v1.8 的开发规格与裁决记录。

---

## 1. 可行性结论（fan-out 审查汇总）

七个并行审查域（各自对抗式通读相关源码与契约后判决）**全部 feasible_with_frictions，无 blocker**；两个深检索域确认业界根基整体站得住（无一论点被推翻）、并钉死全部外部事实：

| 审查域 | 判决 | 摩擦数 | 一句话结论 |
|---|---|---|---|
| M1 配置层/共享类型 | feasible_with_frictions | 7 | 全部有 classify 先例可套；真缺口在 ClassView 必增 extract 字段与 ②b 翻转授权措辞 |
| M2 摄取/M10 编排 | feasible_with_frictions | 12 | 装箱/计量/守恒代数成立；会话化语义五处空白（key 语义、--limit、中断守恒、边界载体、UI 规则层弱）须本文钉死 |
| M14 segment/M8 | feasible_with_frictions | 10 | 内部 Schema/分流/②b 状态机零机制新增有代码证据；通道命名矛盾、帧摘要字段悬空、min_len 语义三处 major |
| M15 extract/M3/M4 | feasible_with_frictions | 7 | 并发/Schema/兜底三件套全有先例；dedup 空文本坍缩确凿（提案已识别、修法需扩到语义层四点） |
| M5/M7/M13 | feasible_with_frictions | 14 | 多图 body/直调修复/record 重绑全部可行；repair 拼接吞图、transitions 穿冻结签名、缺陷表 strict 兼容、成员手术并发确定性四处须按本文修 |
| M11/M12 | feasible_with_frictions | 9 | 第三路由/守恒/熔断残差代数验证通过；通道命名与 extract.step 脱敏两处 major |
| 文档盘点 | GAPS_FOUND | 19 | 全量清单实为 spec 20改+2新 / CONTRACTS 12章全触达 / 手册 20改+1新+5重同步 / 根 3 / examples 1套 / tests 16+4 |
| refute 深检索 | 0 refuted | — | 滑窗 LLM 分段仍是 2026 量产形态之一（Video2GUI）；extract zero-shot 可靠性钉在 70–80%/步（Watch & Learn/Sharingan），须写入风险预算 |
| elevate 深检索 | 29 verified | — | Anthropic >20 图 2000px 硬拒（非缩放）——现默认 max_image_px=2048 会撞拒，M1 须联动警告；AndroidControl 词表全集采纳无裁剪；TRM 为 1–5 五级（本文 0–5 六级是改制） |

编排者 inline 复核过的承重事实：Status 匹配面集中三处 + emitter 兜底公式 `orchestrator.py:305`（新状态不改则 absorbed 被误计 failed）；通道推导 = 事件名首段前缀、error 按 stage 归属（`obslog.py:169-174`）——`"stream"` 单通道与 `segment.*` 事件名互斥；`_TRACE_CHANNELS` 实在 `loader.py:71-72`；UINode 无 package/activity 字段、package 仅经 `extra` 兜底可达（fixtures 逐节点在场，`examples/ui/data/uitree_1.jsonl:1`）；`ResolvedConfig` 直接构造点 16 文件 18 处（全关键字传参，Record 尾部追加安全）；`_raw_payload` 对序列 Record 输出空壳（`emitter.py:495-503`）。

## 2. 设计裁决记录（对提案的修正与细化；终审后并入 spec §1.6）

审查发现收敛为以下裁决（编号 S1–S32）。凡与 `PROPOSAL-stream-segmentation.md` 原文不一致处，**以本表为准**：

| # | 问题 | 裁决 |
|---|---|---|
| S1 | trace 通道 `"stream"` 与事件名 `segment.*`/`extract.*` 在「前缀即通道、error 按 stage」机制（obslog.py:169-174）下互斥——按提案实现三事件全被滤除 | **通道枚举 8→10：增 `"segment"`、`"extract"` 两值**（通道=stage 名，classify R29 同构）；事件名维持 `segment.session`/`segment.boundary`/`extract.step`；error 事件自动按 stage 归属，零路由代码改动 |
| S2 | ClassView 五字段全必填、按类生效唯一通道是 `cfg.class_views[label].<section>`——白名单加 `extract` 而不加字段 = 死白名单 | **ClassView 增第 6 必填字段 `extract: ExtractConfig`**；`_merge_class_sections` 四元组→五元组；`_class_overrides_exist` 比较扩展；7 处构造点（loader 1 + tests 6）机械补参 |
| S3 | ②b 草文缺 M7 翻转授权；「翻回 active」会造成帧与 episode 双写主输出 | ②b 正文补授权句（§3.2 全文）：**M7 修复路径可在 `absorbed`↔`dropped_noise` 间双向改写本批成员信封状态（回收/收缩），禁止翻回 `active`**；每个成员信封至多被一个序列信封吸收 |
| S4 | 会话边界在批内无载体（Record/PipelineItem 均无 session 字段），M14 重推导 = 逻辑双写 | **`PipelineItem` 增字段 `session_id: str | None = None`**（additive）：M10 装箱时对帧信封盖章（簿记非业务逻辑），segment 对其追加的 episode 信封盖章；M7 邻域查询 = session_id 过滤 + 批列表位置序 |
| S5 | transitions 在 PipelineItem 上，穿不过 `build_annotate_prompt`/`annotate_record` 冻结签名（CONTRACTS §7.4） | **R2 同款**：两函数增末位 kwarg `transitions: tuple[Transition, ...] | None = None`（None=现行为）；stage 层与 M7 修复路径线程化下传（手术后传重建值）；§7.4 二次 additive 修订 |
| S6 | 序列标注模板若以图收尾，repair 拼接（annotate.py:94-99 取 parts[-1].text）静默产出 "None\n…" 并丢末帧图 | **模板不变量：末 part 恒为恒在的 text**。序列 user 消息段序 = ① `[动作序列]` text（transitions=None 时整段省略）→ ② 每保留帧 `[关键帧 {i}/{k}·成员 {m}]` text + image → ③ 恒在收尾 `[成员帧摘要]` text（全成员逐帧摘要，总量封顶）；repair 拼接代码零改动 |
| S7 | verify 缺陷表 Schema 的可选键（members?/position?）被 OpenAI strict 硬拒（L0 无条件 strict:true，R1 同型）；critiques 去留未定而修复回路建立在 critiques 之上 | **stream 内部 Schema = `{critiques, defects, verdict}` 三键全 required**（意见/缺陷在前、结论在后）；defect 四子键全 required，members/position 用 `["array","null"]`/`["string","null"]` 可空联合；critiques 原样走既有合并/回喂链路；**`VerificationResult` 增 additive 字段 `defects: tuple[Mapping, ...] = ()`**；fail 且 defects 为空 ⇒ 代码侧默认路由 label_mismatch；非 stream 用冻结 VERDICT_SCHEMA（双 Schema 并存） |
| S8 | 成员手术在并发 gather 下不确定（相邻 episode 争抢同一噪声帧、multi 兄弟互撕共享成员集） | **两阶段批级结构**（classify 扇出先例：gather 后同步 pass）：并发评审 → **同步按批位置序执行全部成员手术**（先到先得变为确定性位次得）→ 并发接缝重摘取 → 同步重建 transitions → 并发重标注/复审；multi 下 membership 类手术仅原信封（首标签）可执行、克隆兄弟降为只标记 |
| S9 | extract × multi 扇出未裁决：兄弟各摘一遍（×k）还是共享；`[class.*.extract]` 白名单使按 record.id 跳过语义不成立 | **按 label 各摘（接受 ×k）**：白名单承诺兑现、transitions 每信封自持；episode 命中多类本应罕见（边界判据即「单一目标导向活动」）；dry-run 沿 R28 口径「multi 按乘数 1 报下界 + stderr 注明」 |
| S10 | dedup 空文本坍缩：`_dedup_text` 对序列返回 ""（全 episode 互判重复）；②③④ 各层判定面 | `_dedup_text` 顶部增 `kind=="sequence"` 分支（先于 modality）：**成员单条配方按序拼接、分隔符 `"\x1e"`（ASCII RS——`isspace()==True`，规范化文本结构性零碰撞）**；③pHash 自动跳过（image=None 门）；`_compose` 对序列按 `requires="tree"` 降级（image_decode_failed 同款）；语义层 `_semantic_participates`/`_semantic_verdict_kind` 增序列 case（"both" 走 tree-only 分支）；超长 embed 输入失败走既有 `embedding_failures` 跳过路径（spec §3.3 明文） |
| S11 | min_len=2 把规则层孤帧会话整体判噪声：未经 LLM 裁决即进 rejects(reason=noise)，污染噪声审计指标 | **min_len 仅作用于 LLM 边界精化切出的段**；规则层孤帧/短会话（含 strategy="rules"）原样成 episode；被 min_len 丢弃的帧 **reason="below_min_len"（≠"noise"）**、计数独立（`report.stream.below_min_len`） |
| S12 | 帧摘要「包名/activity/标题」字段依据不成立（UINode 封闭九字段，package 仅在 extra、activity 全 fixtures 零出现） | 帧摘要 = **best-effort 确定性提取**（§3.4 规格）：app（extra 键 package/package_name/pkg 首个非空）、activity（extra 键 activity/activity_name/window_title，可缺省）、title（DFS 首个可见非空 text）、salient（可见 text/content_desc 按序去重，交互角色加前缀，截断至 digest_max_chars）；**摘要贫瘠护栏**：可见文本节点为零或摘要长度趋零 ⇒ 计 `digest_poor_frames` + 每运行一次 WARN，手册指引开 use_vision |
| S13 | node_id 非跨帧身份（fixtures 同 id 承载不同控件），diff 不得以其为匹配键 | 树 diff 用**结构键多重集匹配** `(role, bounds//quantize, depth)`：输出 added/removed/text_changed/change_ratio/app_changed/title_changed，O(n1+n2)，纯统计不做语义归因（不越 M15 界） |
| S14 | 「LLM zero-shot 推断动作 70–80%/步」（Watch & Learn 70.5%、Sharingan 70–80% 且按类型不均衡）与「diff 注入降幻觉」方向未定（Sharingan 像素 diff 负结果） | ① spec §1.5/风险表写明可靠性预算（每步 20–30% 错误率的级联）与缓解（verify 缺陷路由 + quality 结构分）；② **`extract.include_diff = true` 开关**（默认开——结构化树 diff ≠ 像素 diff，工程实践正面；可关做 A/B）；③ report 增按动作类型分布 `extract.by_type`（系统性劣化可观测） |
| S15 | AndroidControl 词表核实为全集采纳非裁剪；但 2025–2026 统一动作空间共识含 drag 与应用切换（UI-TARS/UIPro/AndroidWorld/GUI-Odyssey RECENT），跨 App episode 是本设计一等公民 | **action_type 枚举 11 值**：`click, long_press, input_text, scroll, drag, open_app, app_switch, navigate_back, navigate_home, wait, other`；词表对齐口径 = 「AndroidControl 全集 ∪ UI-TARS-mobile 增量 + other 兜底」 |
| S16 | extract.on_error="unknown" 写出的值却是 "other"（词表无 unknown），命名错位 | **`extract.on_error = "fallback" | "fail"`**（classify R4 家族命名）；fallback = 该步记 `action_type="other"` + `Transition.detail = {kind:"extraction_invalid", message}`，不写 item.errors；quality 副读数注入时 fallback 步与 LLM 确证的 other **分列**（防污染连贯性锚点） |
| S17 | `--limit` × 会话流交互缺失：islice 移到会话层会让 limit 单位漂移成「会话」 | **帧级截断不变**：islice 在 M2 解析流与会话装配器**之间**；截断视同 EOF——尾部未闭合会话按会话闭合下发 + WARN 一次「尾会话被 --limit 截断」；spec §2.4 --limit 行补 stream 子句 |
| S18 | SIGINT 中断 + 会话缓冲破坏守恒式（unprocessed 仅熔断时出现） | **stream 模式下 `counts.unprocessed` 出现条件扩为「熔断 ∨ interrupted」**；残差公式右侧 `+ episodes`、左侧 `+ absorbed + dropped_noise`（§3.7 守恒式）；非 stream 中断残差恒 0、不加键（回归锚不动） |
| S19 | stream.key 是 groupby 非 keyBy；全局单调性游标会把逐设备拼接输入整体判乱序 | **单调性游标按分区键各自维护**（dict，内存=键基数）；键变即断语义保留、文档钉死「输入须按键成组」（交错流列演进候选）；删「Flink keyBy 对应物」表述；**UI 模态增分区键来源 `"source_dir"`**（ref.source_file 父目录派生，一次采集一目录惯例） |
| S20 | epoch 秒/毫秒无判定规则；ISO 解析能力未定 | **数值：`v<0∨v≥1e14` 解析失败；`v<1e11` 判秒；`1e11≤v<1e14` 判毫秒（÷1000）**。字符串：先试纯数字→数值规则，再试 `datetime.fromisoformat`（3.11 原生含 Z 后缀），均败=解析失败。aware→UTC epoch，naive→按 UTC 解释；内部序键=float 秒。解析失败与乱序同走 `stream.on_disorder` |
| S21 | 装箱算法「first-fit」违背前缀交付与批生命周期 | **next-fit（顺序装箱、仅一只开口箱）**：会话按到达序装满即发；单会话>batch_size ⇒ M10 硬切 + WARN 一次 + 对切分会话帧信封打 duck-typed `session_split` 标（M7 缺帧判定降级依据、`_meta.stream.session_split`）；M1 对 `session_max_len > batch_size` 静态 warning；M10 溢出会话入 §3.10.3 跨批存活封闭清单 |
| S22 | segment_calls 估算公式分母错（重叠 1 帧步长=window−1）；成本示例两处计数错 | **`segment_calls = Σ ceil((L−1)/(window−1))`**（L≥2；L=1 或 rules 计 0）；extract_calls = `Σ(L−1)` 报上界；quality/annotate/verify 以 episodes≈sessions 报下界 + R28 式 stderr 注记；批数用空跑会话尺寸实际装箱精确得出；§4.9 成本示例修正（extract 400、pointwise 4 准则 200，合计 ≈725–750） |
| S23 | 文本模态 dry-run 会话空跑 + 现行行数统计 = 双倍全量读（P2-4 同型关切） | 文本模态 `scan(estimate=True)` 在 stream 启用时**单遍融合**：一次读同时产出行数与会话空跑结果 |
| S24 | 序列 Record 的 ref 形态与 §9.1「line_no/pair_index 恰其一」契约 | **继承首成员的 line_no（文本）/pair_index（UI）**；source_file=首成员源；generated_from=()、generator=None；完整成员溯源由 `_meta.stream.member_sources` 承担 |
| S25 | rejects full 档对序列 Record 输出空壳（`_raw_payload` 单记录假设） | `_raw_payload` 增 `kind=="sequence"` 分支：**`{"kind":"sequence","member_ids":[...],"member_sources":[...]}`**（CONTRACTS §9.2 冻结形态先修订）；`raw_last_output` 的 reason 门维持 schema_violation 现状（classify 同缺口明文接受，记 §7 已知锐边） |
| S26 | segment.on_error="keep" 留痕落点未禁写 item.errors（R4 同型归因污染） | 留痕三件套 = **`_meta.stream.degraded = {kind:"segmentation_invalid", windows_failed:k}`** + EV_ERROR 事件 + 计数器 `segment.failures`；item.errors 仅 on_error="fail" 路径写 |
| S27 | `extract.step` 事件 payload 的 target/value 是输入数据派生（可能含用户键入文本），现行脱敏全档放行——违反 refs 档「无输入数据内容」红线 | obslog 增 **`_DATA_KEYS = {"target","value"}`：none/refs 档剥除**；`"description"` 加入 `_FREE_TEXT_KEYS`（none 档剥除）。事件分级：extract.step none={episode_id,index,action_type}、refs=+description、excerpt=+target/value；segment.boundary none=结构字段（session_id/window/逐帧 relation）、refs=+reason（键已在集合）；三事件 stderr 镜像均 trace-only |
| S28 | sequence_frames 无下界（k=1 除零）、上界与图像尺寸联动缺失（Anthropic >20 图单图 >2000px = 400 硬拒非缩放；现默认 max_image_px=2048 恰撞拒） | M1 校验：**`2 ≤ sequence_frames ≤ 100`**（>100 CONFIG_ERROR）；**`sequence_frames > 20` 且引用 profile `max_image_px > 2000` ⇒ WARN**（指引改 2000 或降帧）；降采样公式 `idx_i = ⌊i·(n−1)/(k−1)⌋, i=0..k−1`（n≤k 取全量；纯整数零 rng，首末恒含，n>k 时严格递增无重复）；20 图阈值按请求内全部 image block 计。openai_compatible 不设独立上限（官方 1500 图/512MB；网关差异入手册 + probe 指引） |
| S29 | stream 模式 rubric 空串默认按模态解析会静默产出无意义分数（default:ui 全是逐帧视觉判据、序列打分无图） | **`segment.enabled=true` 时 `quality.rubric == ""` 解析为 `"default:trajectory"`**（两模态一致；用户显式选择器恒优先；按类视图经 base selector 自动继承）；rubric 文本模态中立、不预设 steps 在场（extract 关闭时「步骤」读作「帧间变化」，M1 对该组合发 warning 指引）；TRM 为 1–5 五级，本表 0–5 六级是 LabelKit 家规改制（spec 背书表注明）；目的性/噪声残留两维无 TRM 原文背书（分别源自 Coherence 拆分与 RPA 噪声处理），背书表分开挂 |
| S30 | 三处 profile 引用集实为四处（漏 `_check_llm_ref`）；rules 策略零调用不应强制配键 | **segment.llm 仅 `strategy ∈ {llm, hybrid}` 时**入密钥解析/vision（仅 use_vision）/probe/存在性四处引用集；extract.llm 恒入四处且恒入 vision_users；stream 模式 vision 集逐阶段表：classify ✓（首帧图）、annotate ✓（多图）、verify ✓（首末帧图）、extract ✓（恒）、segment（仅 use_vision）、**quality ✗（序列打分纯文本，放宽）** |
| S31 | verify 弃帧（收缩）与手术观测面：归因、事件、报表落点未定义 | 收缩弃帧 rejects 行 **stage="verify"、reason="off_task_member"**（duck-typed 标记，raw_last_output 先例）；defects 摘要入 `verify.verdict` 事件 payload（受 content 分级）；计数器 `verify.membership_repairs`/`verify.boundary_flags`/`verify.defects.<kind>` → report.stream.verify 子块；`Transition.index` 手术后**重编号**（不变量 transitions=members−1 恒真）+ `detail.reseamed=true` 溯源；多评审团 defects = 投 fail 的 judge 并集、按 (kind 枚举序, position, members) 确定性去重排序，同成员互斥手术取先序 |
| S32 | 滑窗 vs 整段单调用（GUIDE 99.4% 段可用率）；Def-DTS 引用精度；GEBD 措辞 | v1 保留 hybrid 滑窗（window ≥ 会话长时天然退化为整段单调用；GUIDE 证据入 §1.5 建议可调大 window）；Def-DTS 引用修正为「**半结构（仅双向概括）比裸问题差、完整三步最优；边界信号清晰场景裸判决可胜全套**」+ 5 关系词表按域定制合法（Def-DTS 自身逐数据集改池先例）+ 判据模板明文「**相关但无实体延续的新流程 = context_switch（边界）**」与「会话首帧恒为段首」；GEBD 措辞降级为「中等共识可达」（5 人多评协议数据）；「extract 先行 + 动作序列上分段」次序（同行主流）列演进候选，以成本权衡（dedup 前置节省 vs 分段证据质量）而非「环形依赖」论证 |

## 3. 规格正文（拟合入各文档的规范内容）

### 3.1 `[stream]` 输入声明与会话化（M2 扩展）

**排序**：`stream.order_by = "input_order"`（默认；文本=文件名字典序→行号，UI=pair_index 升序）| `"meta:<field>"`（仅文本模态；时间戳解析按 S20 规格）。**流式单调性校验**（不做全量重排）：单调性游标**按分区键各自维护**（S19）；乱序/解析失败记录按 `stream.on_disorder = "skip"`（默认；计 bad_input + `IngestReport.disorder` 子计数 + `ingest.disorder` 事件 + WARN 一次）| `"fail"`（InputError，退出码 3）。

**会话化（规则层，纯代码，M2 内聚——Ingestor 暴露会话流视图，装箱留 M10）**：
- `stream.key = ["meta:<field>" | "source_dir", ...]`：分区键，键变即断（groupby 语义，输入须按键成组）；`"source_dir"` = ref.source_file 父目录（UI 可用）；
- `stream.gap_s`（相邻记录时间差>gap 断开；仅 order_by="meta:*"）/ `stream.gap_steps`（序号差断开；0=不启用；两者可并用，任一触发即断）；
- `stream.session_max_len`（默认 200）/ `stream.session_max_span_s`（0=不启用；仅 meta:*）硬断开。
- 会话闭合时发 `segment.session` 事件（cause ∈ gap|key|max_len|max_span|eof|limit）并计 `IngestReport.sessions`。
- `--limit`：帧级 islice 在解析流与装配器之间；截断视同 EOF 冲洗尾会话 + WARN（S17）。

**整会话装箱（M10）**：next-fit（S21）；批容量 = batch_size 帧；单会话超 batch_size ⇒ 硬切 + WARN + `session_split` duck-typed 标。M10 在构造帧信封时盖章 `PipelineItem.session_id`（S4）。

### 3.2 数据结构与 Stage 契约 ②b（spec §4 修订）

```python
Status = Literal["active", "dropped_dup", "dropped_lowq", "dropped_verify",
                 "failed", "absorbed", "dropped_noise"]        # v1.8 增两值

@dataclass(frozen=True)
class Record:
    ...                                    # 既有七字段不变
    kind: Literal["single", "sequence"] = "single"      # v1.8 尾部追加（带默认）
    members: tuple["Record", ...] = ()     # sequence: 成员按序键升序；single: ()

@dataclass(frozen=True)
class Transition:                          # v1.8：M15 产物
    index: int                             # 重建后位次（恒 = 在 transitions 中的下标）
    action: Mapping                        # {action_type, target, value, description}
    model: str
    attempts: int
    detail: Mapping                        # fallback: {kind:"extraction_invalid", message}
                                           # 手术重摘取: {reseamed: true}

@dataclass(frozen=True)
class VerificationResult:
    ...                                    # verdict/rounds/critiques 不变
    defects: tuple[Mapping, ...] = ()      # v1.8 additive：stream 缺陷表（非 stream 恒 ()）

@dataclass
class PipelineItem:
    ...                                    # 既有字段不变
    transitions: tuple[Transition, ...] | None = None   # v1.8：M15 写入
    session_id: str | None = None          # v1.8：M10 盖章帧信封 / segment 盖章 episode
```

序列 Record 字段约定：`text/raw/ui_tree/image = None`；`modality` = 成员模态；`id = sha256("\n".join(member_ids))[:16]`；`ref = RecordRef(source_file=首成员源, line_no=首成员 line_no, pair_index=首成员 pair_index, generated_from=(), generator=None)`（S24）。

**Stage 契约新例外 ②b**（入 spec §4.3 / stage.py docstring / CONTRACTS §5，与 ②a 并列）：

> **②b segment 例外（仅 stream 模式）**——segment 可将批内既有 active 成员信封的 status 置为 `absorbed` 或 `dropped_noise`（属①④的正常状态写入），并向传入列表**尾部**追加以这些成员拼装的序列信封；追加物视同批内普通元素、同受①③④约束；每个成员信封至多被一个序列信封吸收；不得删除、重排或替换任何既有元素对象；返回值仍须是传入的同一列表对象。**M7 修复路径豁免**：verify 的缺陷修复可在本批内将成员信封状态在 `absorbed` 与 `dropped_noise` 间双向改写（成员回收/收缩），此为契约①的唯一反向豁免；禁止将成员信封翻回 `active`。

**帧摘要与树 diff（共享 helper，canonical 落 `labelkit/common/contracts/types.py` 模块级函数，签名入 CONTRACTS §3）**：

```python
def frame_digest(record: Record, max_chars: int) -> str
    # UI：app（extra: package|package_name|pkg 首非空）· activity（extra: activity|activity_name|window_title，可缺省）
    #     · title（DFS 首个可见非空 text）· salient（可见 text/content_desc 按序去重；
    #     Button/EditText/CheckBox 类交互角色加 "*" 前缀），整体截断至 max_chars（serialize 截断惯例）。
    # text：record.text 截断至 max_chars。
    # 摘要贫瘠判定：可见文本节点数为 0 或摘要长度 < 8 ⇒ 贫瘠（调用方计数）。

def tree_diff(a: UITree | None, b: UITree | None, quantize_px: int) -> Mapping
    # 结构键 (role, bounds//quantize_px, depth) 多重集匹配（S13）：
    # {added:int, removed:int, text_changed:int, change_ratio:float, app_changed:bool, title_changed:bool}
```

### 3.3 M14 segment（语义分段算子，新文件 `spec/314-m14-segment.md`）

| 模块 | 职责 | 边界 | 依赖 |
|---|---|---|---|
| M14 segment | 把批内候选会话精化为 episode：可选 LLM 滑窗边界裁决与逐帧噪声标记；成员信封置 absorbed、噪声帧置 dropped_noise，按序键拼装序列 Record 并尾部追加 episode 信封（②b） | 不判重（M3）；不推断动作（M15）；不打任务标签（M5）；不改链结构 | M1, M8, M9 |

**策略**（`segment.strategy`）：
- `"rules"`：候选会话原样成 episode，零 LLM；noise_filter/min_len 不生效（M1 对 rules+noise_filter=true 发 no-op warning）。
- `"llm"` / `"hybrid"`（默认 hybrid）：滑窗裁决。窗长 `segment.window`（默认 20，M1 校验 ≥2）；步长 = window−1（重叠 1 帧，接缝帧整帧判决归后窗）；len(session)==1 走 rules 退化（零 LLM）。

**三步演绎判据模板**（确定性拼接，逐字进 CONTRACTS §10.9；Def-DTS 结构 + GEBD 锚定）：
1. 双向上下文概括：窗内逐帧给出帧摘要 + 相邻帧 diff 提示（代码侧预组装）；
2. 逐帧封闭集关系分类（词表固定域无关）：`continues`（同流程推进）｜`advances`（屏幕/App 变了但可见任务实体延续——验证码、订单号、餐厅名跨屏出现；跨 App episode 是一等公民）｜`returns_to_entry`（回到入口/搜索/桌面后开启新流程）｜`context_switch`（交互对象与环境不连续且无实体延续；**相关但无实体延续的新流程 = context_switch**，S32）｜`interruption`（与前后活动均无关的短暂插入：通知、弹窗、误触）；
3. 演绎映射（代码侧查表，LLM 不直接答边界）：continues/advances → 非边界；returns_to_entry/context_switch → 边界（**该帧是新段第一帧**）；interruption → noise。
- 两个锚定写死在模板文本：粒度=「完整任务」层级（GEBD "1 level deeper"）；注意力=只看前台 App/窗口（dominant subject）。
- **会话首帧恒为段首**（rel[0] 的边界值不参与判决；noise[0] 照常生效）。
- `segment.use_vision = false`（默认纯文本；true 时窗内逐帧附图）。`segment.context` 可选域上下文（非边界定义）。

**窗口内部 Schema**（`schema_engine.segment_window_schema(frame_count, with_reason)`，关键字 ⊆ 冻结集、无 uniqueItems）：

```python
{"type": "object",
 "properties": {"frames": {"type": "array",
     "items": {"type": "object",
               "properties": {"index": {"type": "integer", "minimum": 0, "maximum": N-1},
                              "relation": {"type": "string", "enum": [五值]}
                              [, "reason": {"type": "string"}]},
               "required": [...全键], "additionalProperties": False},
     "minItems": N, "maxItems": N}},
 "required": ["frames"], "additionalProperties": False}
```
with_reason 条件 = trace.enabled ∧ "segment" ∈ channels（R29 同款）。代码侧后校验：按 index first-wins 建表，缺席帧缺省 `continues`（保守中性，quality "缺席准则→tie" 同款）。

**缝合与成段流程**（确定性）：全窗判决收齐 → 逐帧 rel 定案（后窗覆写接缝帧）→ 先剔 noise 帧（dropped_noise, reason="noise"）→ 剩余按 boundary 切段 → **min_len 检查仅作用于此处切出的段**（S11；短段帧 dropped_noise, reason="below_min_len"）→ 每段拼装 episode（成员按序键升序、置 absorbed、尾部追加序列信封、盖章 session_id）。

**失败语义**：单窗 M8 修复耗尽按 `segment.on_error = "keep"`（默认：该会话整体成一个 episode + S26 留痕三件套）| `"fail"`（会话成员全部 failed → rejects, kind=segmentation_invalid）。

**事件**：`segment.session`（M2 会话闭合；record_ids=()；payload {session_id, first, last, len, cause}）；`segment.boundary`（每窗裁决后；record_ids=()；payload {session_id, window:[s,e], member_ids, relations:[{index,relation}], model, reason†}）。计数器：`segment.failures`。

### 3.4 M15 extract（转移/动作摘取算子，新文件 `spec/315-m15-extract.md`）

| 模块 | 职责 | 边界 | 依赖 |
|---|---|---|---|
| M15 extract | 对每个 active 序列信封的每对相邻成员帧 ⟨s_i, s_{i+1}⟩ 经 LLM 产出结构化动作（内部 Schema），写入 `item.transitions`；转移数 = 成员数 − 1 | 不重分段（M14 上游）；不产出用户 Schema 字段（M5）；不淘汰记录 | M1, M8, M9 |

- 仅 UI 模态序列（文本序列 v1 不适用）；幂等：`item.transitions is not None` 跳过。multi 扇出下兄弟各摘（S9，per-label instruction 生效）。
- **提示词**（确定性模板，CONTRACTS §10.10）：system = 摘取指令 + 动作词表说明 + OpenCUA 锚定句（「前一帧是动作发生**前**最后一个稳定状态，后一帧是动作完成**后**的首个稳定状态；推断二者之间发生的**单个语义动作**；若变化由多个低层事件构成（连续滚动、连续键入），归并为一个语义动作」）+ 可选 instruction（per-label 生效）；user = `[前一帧截图]` 图 + `[后一帧截图]` 图 + `[树变更摘要]`（include_diff=true 时；tree_diff 输出的文字化）+ `[前后帧树摘要]`（两帧 frame_digest）。一请求 2 图。
- **内部 Schema**（`schema_engine.action_schema()`，全键 required、可空联合）：

```python
{"type": "object",
 "properties": {"action_type": {"type": "string", "enum": [11 值，S15]},
                "target": {"type": ["string", "null"]},
                "value": {"type": ["string", "null"]},
                "description": {"type": "string"}},
 "required": ["action_type", "target", "value", "description"],
 "additionalProperties": False}
```
- **字段语义**（CONTRACTS §10.10 表）：target = 目标控件文本引用（text → content_desc → 类名+序号），**不用坐标**；input_text 的 value = 键入文本（聚焦点击不单独记步）；scroll 的 value = 方向（up/down/left/right，模板文字锚定，代码侧小写归一）；open_app/app_switch 的 value = 应用名；navigate_*/wait/other 的 target/value = null（other 语义全落 description）。
- **失败语义**：单转移 M8 修复耗尽按 `extract.on_error = "fallback"`（默认，S16）| `"fail"`（episode failed → rejects, kind=extraction_invalid）。
- 并发：批内全部转移一个 gather（quality phase2 骨架）；无 rng 消耗。
- **事件**：`extract.step`（每转移；record_ids=(s_i.id, s_{i+1}.id)；payload {episode_id, index, action_type, description†, target‡, value‡}——†refs 起、‡excerpt 起，S27）。计数器：`extract.transitions`、`extract.fallback_steps`、`extract.failures`、`extract.by_type.<action_type>`。

### 3.5 下游算子序列适配（M3/M13/M4/M5/M7）

**M3 dedup**：S10 四点。episode 级重复 = 「同样的操作流程」。

**M13 classify**：序列分支（零崩溃）：「当前记录」段 = `[待分类数据·序列]` episode 摘要（成员 frame_digest 按序拼接，**总量封顶 input.ui_tree_max_chars**，首尾成员恒保留、中段整条截断 + `…(truncated N members)` 标记）+ 首帧截图（UI 模态，vision 校验保留）。multi 扇出克隆 transitions 恒 None（extract 在后，各兄弟自摘，S9）。verify 成员修复后兄弟信封 record 分叉：共享语义边界文档声明（同 id 兄弟行的 member_ids 可after修复不同，`_meta.stream.repaired` 标记消歧）。

**M4 quality**：序列提示词分支 = `[步骤序列]`（transitions 文本渲染；fallback 步与 LLM 确证 other 分列标注，S16）+ `[成员帧摘要]`（bounded），**无图**；transitions/预渲染文本经 `_judge_once`/`_pointwise_once` 新形参传入（私有签名）；`_excerpt_payload` 序列分支（成员摘要前 200 字符）。`default:trajectory` 内置 rubric（S29；四准则六级全文见 §3.9）。extract 关闭 ⇒ 退化为帧摘要打分（M1 warning 指引）。stream 模式 threshold 缺省 = 只打分不筛（现语义，尤其合理——TRM 消融 + E2E #6）。

**M5 annotate**：S5 签名 + S6 模板。`[动作序列]` 行渲染格式（CONTRACTS §10.1 序列变体，逐字冻结）：`{index}. {action_type}（对象: {target|—}；值: {value|—}）{description}`。降采样 S28 公式；`[关键帧 {i}/{k}·成员 {m}]` 标签显式成员序数。sc/L2.5 路径不动（L2.5 hook 收到 record=None——文档声明，富载荷列演进候选）。

**M7 verify（stream 分支）**：
- 内部 Schema（`schema_engine.defect_verdict_schema()`，S7）：

```python
{"type": "object",
 "properties": {"critiques": {…同 VERDICT_SCHEMA…},
                "defects": {"type": "array", "items": {"type": "object",
                    "properties": {"kind": {"type": "string", "enum":
                        ["label_mismatch","off_task_members","missing_head","missing_tail","missing_members"]},
                        "members": {"type": ["array","null"], "items": {"type": "string"}},
                        "position": {"type": ["string","null"]},
                        "detail": {"type": "string"}},
                    "required": ["kind","members","position","detail"],
                    "additionalProperties": False}},
                "verdict": {"type": "string", "enum": ["pass","fail"]}},
 "required": ["critiques","defects","verdict"], "additionalProperties": False}
```
- 评审证据 = `[任务指令]` + `[动作序列]` + `[边界余量]`（段边界外前后 k=2 帧的 frame_digest 及其去向：noise/相邻段序数/无）+ `[首帧截图]` + `[末帧截图]` + `[标注结果]`（CONTRACTS §10.5 序列变体）。
- **修复路由（两阶段批级，S8）**：round 内——并发评审全部待审 episode → 同步按批位置序执行成员手术（收缩：defect.members 帧 absorbed→dropped_noise + off_task_member 标；回收：同 session_id 的 dropped_noise 帧经 `segment.judge_window` 复裁（relation ∈ {continues,advances} 即回收）dropped_noise→absorbed + 按序键插入 members；三级判定：批内噪声池 → 相邻 episode（只标记）→ 无处可寻（缺陷条目增代码侧兄弟键 suspected="capture_gap"——detail 为字符串型不可嵌套；session_split 帧标 "session_split"））→ 并发接缝重摘取（`extract.extract_transition` 直调，1–2 次/手术）→ 同步重建 record（replace members；id 不变）与 transitions（重编号 + reseamed 标）→ 并发重标注（`annotate_record(..., transitions=新值)`）→ 下轮复审。修复轮数计入 max_repair_rounds（含首评）。multi 克隆兄弟 membership 缺陷只标记（S8）；多评审团 defects 并集 + 确定性去重排序（S31）；fail+空 defects ⇒ 默认路由 label_mismatch（S7）。修复后不重打分：沿用修复前分数 + `_meta.stream.repaired=true`。
- 非 stream 路径零改动（run_verify_loop 与 VERDICT_SCHEMA 是回归锚；stream 走 stage 层旁路驱动器）。

### 3.6 M1 组合约束与校验（spec §3.1.4 / §2.3.1 增量）

- `segment.enabled` 要求：`run.mode="process"` ∧ `generate.enabled=false`（含 generate_only 传递闭合）∧ `annotate.enabled=true`（⑭）。
- `extract.enabled` 要求 `segment.enabled` ∧ `run.modality="ui"`。
- `stream.gap_s`/`session_max_span_s` 要求 `order_by="meta:*"`；`order_by="meta:*"` 仅文本模态；`stream.key` 元素 = `"meta:<field>"`（仅文本）| `"source_dir"`。
- `segment.window ≥ 2`；`2 ≤ annotate.sequence_frames ≤ 100`；`sequence_frames>20 ∧ max_image_px>2000` ⇒ WARN（S28）；`session_max_len > run.batch_size` ⇒ WARN（S21）。
- no-op warnings（R8 家族）：`[stream]`/`[segment]`/`[extract]` 在场而 segment.enabled=false；`strategy="rules"` ∧ noise_filter=true；`sequence_frames` 显式设置而非 stream；`quality.mode` 任意 + extract.enabled=false 时 trajectory rubric 组合提示。
- 引用集四处（S30）；vision 逐阶段表（S30）；`[class.<name>.extract]` 白名单（仅 instruction 键）；segment 不入白名单（链序因果：segment 在 classify 前，类标签不存在——spec 白名单表注明）。
- rubric：selector 枚举三值 + `""` 的 stream 解析（S29）；`default_trajectory.toml` 包数据。

### 3.7 编排、输出与守恒（M10/M11）

**链序**：`_CHAIN_ORDER = ("segment", "dedup", "classify", "extract", "quality", "generate", "annotate", "verify")` 单一超集元组（segment/extract 关闭时逐字节退化为现链；generate 与 stream 互斥故顺位无冲突）。`counts.episodes` = segment 阶段 len 差（fanout 同构计量）。

**状态记账**：post-emit tally 增 absorbed/dropped_noise；failed 兜底公式扩展：
`failed = max(len(batch) − emitted − dropped_dup − dropped_lowq − dropped_verify − absorbed − dropped_noise, 0)`。
`batch.end` payload 增 `episodes`/`absorbed`/`dropped_noise`（仅 segment 启用时携带，R20 形制）。stderr 摘要/进度行**不增键**（fanout 先例；报表可见）。

**守恒式**（spec §6.4）：
`emitted + dropped_dup + dropped_lowq + dropped_verify + dropped_noise + failed + bad_input + absorbed = scanned + generated + fanout + episodes`；
熔断**或中断（stream 模式，S18）**时左侧另加 `unprocessed`（残差公式同步扩展）。

**M11**：absorbed 第三路由（不写主输出、不写 rejects、仅计数）；`_reject_stage_reason` 增 dropped_noise 分支（segment/noise、segment/below_min_len、verify/off_task_member 按 duck-typed 标记分流）；`_meta` 增恒在键 `"stream"`（位置：source 之后、scores 之前——链序镜像），未启用 = null，结构：

```
"stream": {"episode_id", "session_id", "order_span": [first, last], "member_count",
           "member_ids": [...], "member_sources": [{file, pair_index|line_no}, ...],
           "session_split": false, "repaired": false,
           "degraded": null | {kind, windows_failed},
           "steps": null | [{index, action_type, target, value, description}, ...]}
```
`_meta.verification` 在 stream 模式增 `defects` 键（恒在，无缺陷 = []）。rejects full 序列载荷（S25）。

**report**：counts 增 episodes/absorbed/dropped_noise（segment 启用时）；新 `stream` 节：

```
"stream": {"sessions", "episodes", "mean_episode_len", "absorbed", "dropped_noise",
           "below_min_len", "digest_poor_frames", "segment_failures",
 [extract 启用] "extract": {"transitions", "fallback_steps", "failures", "by_type": {...}},
 [verify 启用]  "verify": {"membership_repairs", "boundary_flags", "defects": {kind: n}}}
```
sessions 数据源 = IngestReport（M2 属主）。

**dry-run**：估算公式 S22；无条件打印 `segment_calls=… extract_calls=…`（classify 先例；默认关闭恒 0）；文本模态单遍融合（S23）。

### 3.8 可观测性（M12 / spec §7）

- `_TRACE_CHANNELS` 8→10（`"segment"`、`"extract"`；默认订阅集不变）。
- 事件目录三新行（§3.3/§3.4 payload）；`error` 事件对 segment/extract 阶段按 stage 归属（机制自动）。
- 脱敏：`_FREE_TEXT_KEYS` += `"description"`；新 `_DATA_KEYS = {"target","value"}` none/refs 档剥除（S27；CONTRACTS §8.3 additive）。
- 错误码表（§7.6）增两行：`segmentation_invalid`（M14；keep=留痕存活 / fail=会话成员 failed→rejects）、`extraction_invalid`（M15；fallback=other 留痕 / fail=episode failed→rejects）。

### 3.9 `default:trajectory` rubric（包数据 `labelkit/data/rubrics/default_trajectory.toml` + spec Appendix A.3 全文）

四准则（completion/coherence/purposefulness/noise_residue）各带 pairwise_prompt 与 0–5 六级 pointwise_levels，全文按 elevate 研究定稿草案（R2 交付件 §4；「步骤」在 extract 关闭时读作「帧间变化」的模态中立措辞）。背书注记：completion/coherence 源自 OS-Genesis TRM（1–5 五级改制为 0–5 六级）；purposefulness 自 Coherence "toward the goal" 拆分；noise_residue 源自 RPA 日志分割噪声处理（Leno et al.）。

### 3.10 配置增量总表（spec §5.2）

```toml
[stream]                          # 输入侧声明（M2 消费；segment.enabled=false 时在场 ⇒ warning）
order_by = "input_order"          # "input_order" | "meta:<field>"（仅文本模态）
on_disorder = "skip"              # "skip"（计 bad_input+disorder）| "fail"
key = []                          # ["meta:<field>"（文本）| "source_dir"]；键变即断
gap_s = 300                       # 仅 order_by="meta:*"；结构性论证：欠分割可由 LLM 精化拯救、过分割不可逆 ⇒ 默认偏大
gap_steps = 0                     # 序号差断开（0=不启用）；与 gap_s 任一触发即断
session_max_len = 200             # 硬上限（帧）；> batch_size ⇒ M1 WARN
session_max_span_s = 0            # 仅 meta:*（0=不启用）

[segment]                         # M14（stream 模式总开关）
enabled = false                   # 默认 false = 与 v1.7 行为逐字节一致
strategy = "hybrid"               # "rules" | "llm" | "hybrid"
llm = "default"                   # 仅 strategy∈{llm,hybrid} 入引用集（S30）
window = 20                       # ≥2；滑窗帧数/调用（重叠 1 帧）
digest_max_chars = 400            # 单帧摘要上限
noise_filter = true               # 仅 llm/hybrid 生效（rules ⇒ no-op warning）
min_len = 2                       # 仅作用于 LLM 精化切出的段（S11）
use_vision = false
context = ""                      # 可选域上下文；边界判据内置于模板，零配置可用
on_error = "keep"                 # "keep"（S26 留痕）| "fail"

[extract]                         # M15（仅 UI 序列）
enabled = false                   # 启用要求 segment.enabled ∧ modality="ui"
llm = "default"                   # 恒入引用集 + vision_users
instruction = ""                  # [class.<name>.extract] 可覆盖（白名单仅此键）
include_diff = true               # [树变更摘要] 注入开关（S14）
on_error = "fallback"             # "fallback"（S16）| "fail"

[annotate]
sequence_frames = 20              # 序列标注单请求最大帧数；[2,100]；>20 联动 max_image_px 检查（S28）
```

## 4. 与提案 §7 开放决策点的对照（全部裁决）

①追加 M14/M15（默认）✓；②`[stream]` 独立节 ✓；③噪声帧进 rejects ✓（+--strict 交互手册明示：stream 工程噪声帧属预期产物，--strict 会因此退出 1）；④交错 episode 不做（roadmap）✓；⑤generate×stream 互斥 ✓；⑥序列 dedup ①②④+跳③ ✓（S10 展开）；⑦extract 文本模态不做 ✓；⑧超长会话硬切+WARN ✓（S21 细化）；⑨default:trajectory 内置 ✓（S29）；⑩steps 恒在 ✓（extract 关=null）；⑪粒度旋钮不做 ✓；⑫修复范围=标签重标+收缩+回收、跨段只标记 ✓（S8/S31 细化）；⑬流式单调性校验+on_disorder ✓（S19/S20 细化）；⑭stream⇒annotate 必开 ✓。新增裁决 S1–S32 见 §2。

## 5. 文件修改清单（全量；★=对提案 §5 的修正/新增项）

### spec/（20 改 + 2 新 + 4 不改）

| 文件 | 动作 | 内容 |
|---|---|---|
| `314-m14-segment.md` | 新建 | M14 全文（§3.3 展开，统一模板） |
| `315-m15-extract.md` | 新建 | M15 全文（§3.4 展开） |
| `00-frontmatter.md` | 改 | 版本历史 v1.8 行★ |
| `10-ch1-overview.md` | 改 | §1.2 术语（episode/会话/转移/stream 模式）★；§1.4 需求映射行★；§1.5 背书 ~15 条（含 refute 新证 GUIDE/W&L/Sharingan/UIPro）；§1.6 决策（S1–S32 要点） |
| `20-ch2-overall-design.md` | 改 | §2.1.1 功能表；§2.2.1 模块表+图注 M14/M15；§2.3.1 开关矩阵与约束；§2.3.2 两行；§2.4 dry-run 行与 rubric --show 枚举；§2.5；§2.6 会话缓冲/序列记录注记★ |
| `301-m1-config.md` | 改 | §3.6 全量校验清单 |
| `302-m2-ingest.md` | 改 | 排序/单调性/会话装配小节；IngestReport 增 sessions/disorder★；dry-run 单遍融合★ |
| `303-m3-dedup.md` | 改 | S10 四点（含语义层两函数★与 "both" 退化明文） |
| `304-m4-qualityqurating.md` | 改 | 序列打分小节；trajectory rubric 判据与背书拆分注记★ |
| `305-m5-annotate.md` | 改 | 序列模板（S6 段序）；sequence_frames 降采样；transitions 形参★ |
| `307-m7-verify.md` | 改 | 缺陷表/边界余量/两阶段修复路由（S8）★/repaired |
| `308-m8-schema-engine.md` | 改 | 三新内部 Schema；可空联合注记★ |
| `310-m10-orchestrator.md` | 改 | 双态链序/next-fit 装箱★/episodes 计量/守恒扩展（含 interrupted★）/跨批存活清单注记★/估算公式（S22）★ |
| `311-m11-emitter.md` | 改 | 第三路由/rejects 三 reason★/`_meta.stream`/defects/full 序列载荷★ |
| `313-m13-classify.md` | 改★ | 序列提示词分支小节；multi×episode 共享语义声明 |
| `40-ch4-data-structures.md` | 改 | §3.2 全部类型 + ②b（含 M7 双向豁免★） |
| `50-ch5-config-spec.md` | 改 | §3.10 三节键表+sequence_frames+白名单 extract+rubric 枚举+channels 10 值 |
| `60-ch6-io-formats.md` | 改 | §6.1 时间戳语义★；§6.3 `_meta.stream`；§6.4 report/counts/守恒 |
| `70-ch7-logging.md` | 改 | §7.2 三事件行+通道 10 值；§7.4 脱敏 `_DATA_KEYS`★；§7.6 两错误码 |
| `80-ch8-nongoals-roadmap.md` | 改 | §8.3 O3 注记；§8.4 演进候选（有界乱序窗/交错 episode/跨段仲裁/extract-先行次序★/文本 extract/缺帧补全/本地 IDM★/嵌入变点/k>1 重叠/完成度末帧图★） |
| `85-ch9-references.md` | 改 | [41] 起 ~18 条★（提案 §9 + refute 新证 8 条） |
| `90-appendix-a-rubrics.md` | 改 | A.3 default:trajectory 全文★ |
| `30-ch3-modules-intro.md`/`306`/`309`/`312` | 不改 | 30 无索引表；M6 零行为变化；M9 零改动；M12 机制不变 |

### docs/CONTRACTS.md（12 章全触达★）

§1 包布局（segment.py/extract.py/default_trajectory.toml）★；§2 链序+Status 枚举原文+算子层★；§3 types verbatim（Record/Transition/VerificationResult/PipelineItem/两 helper 签名）；§4 errors verbatim（两 kind）；§5 ②b；§6 三 dataclass+ClassView.extract★+校验清单；§7 两新 API 节 + 7.1（Ingestor 会话流视图★）/7.2/7.3/7.4（S5 签名修订）/7.6/7.7/7.9/7.10/7.11/7.12（CLI 文法 rubric 枚举★）/7.13 修订 + M7 直调登记（extract_transition/judge_window）；§8 事件目录+`_DATA_KEYS` 脱敏★；§9 `_meta.stream`/rejects（三 reason+full 序列载荷★）/report/计数词表/守恒；§10 §10.9 segment 模板+§10.10 extract 模板+§10.1/§10.5 序列变体修订+§10.7 三 Schema；§11 ②⑤（跨批存活+M2 缓冲/M10 溢出会话★）；§12 决策登记。

### docs/manual/（20 改 + 1 新 + 5 重同步★）

新建 `25-stream.md`（全章样例出自 examples/stream 真实运行）。重同步（真实重跑）：`03`（dry-run 行+`_meta` 块）、`08`（`_meta`+report）、`15`（dry-run+rubric 枚举）、`24`★（`_meta` 块——提案漏计）、`25`（新）。修改：`README`（目录）、`01`（算子总览+流水线图）★、`04`（状态机+铁律 ②b+守恒+约束表）★、`05`（时间序输入指引）★、`06`（vision 例外注记）★、`07`（节速览）★、`09`（序列 dedup）★、`10`（trajectory rubric+序列打分+长 episode 信度注记 S32）、`11`（序列模板+sequence_frames）、`12`（互斥一句）★、`13`（缺陷表+路由）、`14`（内部结构清单）★、`16`（事件表+通道 10）、`17`（调用账+extract 大头+include_diff A/B 指引）、`18`（错误码两行+stream 症状）、`19`（null 枚举句）★、`appendix-a`（三节速查+A.6/A.8/A.4+通道数）★、`appendix-b`（trajectory 全文）★。**明确不改**：`02`、`20/21/22`（样例均为 `_meta` 子对象/jq，stream:null 不出现——摘要行不增键的 S 裁决保住此结论）、`23`。

### labelkit/（15 改 + 2 新 + 1 包数据）

| 文件 | 改动 |
|---|---|
| `segment.py`（新） | M14 本体：会话重组（session_id 分组）→ 滑窗裁决 → 缝合成段 → episode 拼装（②b）；judge_window 公开（M7 直调） |
| `extract.py`（新） | M15 本体：相邻对裁决 → 动作 Schema → fallback 留痕；extract_transition 公开（M7 直调） |
| `data/rubrics/default_trajectory.toml`（新） | §3.9 全文 |
| `config/model.py` | StreamConfig/SegmentConfig/ExtractConfig；AnnotateConfig.sequence_frames；ClassView.extract★；ResolvedConfig 增 stream/segment/extract 三必填字段；TraceConfig 注释 |
| `config/loader.py` | 三节解析；§3.6 约束全量；引用集四处（S30）；白名单 extract；`_TRACE_CHANNELS` 10 值（71-72 行★）；rubric 两站点枚举+default_rubric Literal+`""` stream 解析；`_merge_class_sections` 五元组★；no-op warnings |
| `types.py` | §3.2 类型 + frame_digest/tree_diff 两 helper |
| `stage.py` | ②b docstring |
| `errors.py` | ErrorKind 两值 |
| `schema_engine.py` | segment_window_schema/action_schema/defect_verdict_schema |
| `ingest.py` | 时间戳解析（S20）/按键单调性游标（S19）/会话装配器（sessions() 视图）/IngestReport 增 sessions+disorder/limit 帧级截断（S17）/dry-run 单遍融合（S23） |
| `dedup.py` | S10 四点 |
| `classify.py` | 序列提示词分支（§3.5） |
| `quality.py` | 序列分支（transitions 形参下穿/_excerpt_payload）；trajectory rubric 消费零改动 |
| `annotate.py` | S5 签名 + S6 序列模板 + 降采样 |
| `verify.py` | stream 旁路驱动器（缺陷表/两阶段手术/三级回收/重建/复审）；非 stream 零改动 |
| `generate.py` | 零改动（M1 互斥挡住） |
| `orchestrator.py` | 链序/next-fit 装箱/session_id 盖章/episodes 计量/tally+failed 公式/batch.end/守恒+interrupted 残差/report stream 节/_estimate/_run_dry 打印 |
| `emitter.py` | 第三路由/_reject_stage_reason 三分支/_meta.stream/verification.defects/_raw_payload 序列分支/docstring |
| `obslog.py` | 三事件常量/_FREE_TEXT_KEYS+description/_DATA_KEYS★ |
| `cli.py` | _build_stages 两 stage（链位序）/referenced_profiles 两 profile（S30 条件）/_RUBRIC_FILES+choices |
| `hooks.py`/`llm_client.py` | 零改动 |

### tests/（13 改 + 4 新）

新增覆盖现归档于：`tests/operators/test_ingest.py`（时间戳解析/单调性/会话闭合/装箱/limit）、`tests/operators/test_segment.py`（摘要/diff/Schema 形状/缝合确定性/②b/min_len/守恒）、`tests/operators/test_extract.py`（Schema/fallback 留痕不写 errors/转移数不变量/by_type）、`tests/integration/test_stream_llm.py`（真实 glm-5.2：边界词表内/噪声判定/动作摘取/25→20 帧降采样成功）。
改：16 文件 ResolvedConfig 补参（三必填字段）+ 6 处 ClassView 构造补 extract★；`test_config.py`★（三节解析/约束/白名单/rubric/引用集）；`test_types.py`（新类型/Status 全集断言★/helper）；`test_ingest.py`（会话流适配）；`test_dedup.py`（序列拼接/③跳过/语义 case）；`test_quality.py`（序列分支/trajectory）；`test_annotate.py`（降采样/模板段序/repair 拼接不变量）；`test_verify.py`（缺陷 Schema/路由/两阶段确定性）；`test_classify.py`（序列分支）；`test_orchestrator.py`（装箱/计量/守恒/估算/interrupted 残差）；`test_emitter.py`（第三路由/_meta 键全集+stream/reason 三分支/full 序列载荷）；`test_obslog.py`（通道 10/三事件路由/_DATA_KEYS 脱敏）；`test_schema_engine.py`（三 builder）；`test_cli.py`（profiles/rubric 三值）。

### examples/ 与根目录

`examples/stream/project.toml` + `data/`（uitree_1..14.jsonl + image_1..14.png：任务 A 点外卖 8 帧 + 帧 5 位置无关屏（package 异域）+ 任务 B 打车 5 帧背靠背；PIL 程序化生成截图；batch_size=32、window=8、pointwise trajectory 无阈值、annotate Schema {task_label, app, summary}、verify repair、trace channels ["segment","extract","verify","schema"]）；`README.md`（根：算子数/流水线图/示例列表/手册章数）；`CLAUDE.md`/`AGENTS.md`（模块映射 M14/M15、链序、v1.8 注、examples/stream——两份逐字同步）。操作性：四个存量 examples out/ 重跑（`_meta` 多 `stream: null`）；PROPOSAL 状态行更新。

## 6. 开发计划（依赖序，每步离线测试全绿门禁）

| 步 | 内容 | 门禁 |
|---|---|---|
| 0 | spec/ + CONTRACTS 修订全量合入（文档先行） | 交叉引用自洽（键表↔校验清单↔事件目录↔守恒式对得上） |
| 1 | types/stage/errors + M1（model/loader 全量）| test_types/test_config 全绿；对 examples/stream 的 project.toml validate 通过 |
| 2 | M8 三 Schema + M2 会话化 + M10 装箱/计量/守恒 | test_schema_engine/test_ingest/test_orchestrator 全绿（stream ingest 已并入 test_ingest） |
| 3 | M14 segment 本体 + M11 三路由/_meta + M12 通道/脱敏 | test_segment/test_emitter/test_obslog 全绿 |
| 4 | M3 序列 dedup + M13 序列分支 + M15 extract 本体 | test_dedup/test_classify/test_extract 全绿 |
| 5 | M4 序列打分 + trajectory rubric + M5 序列模板/降采样/签名 | test_quality/test_annotate 全绿 |
| 6 | M7 stream 驱动器（缺陷表/两阶段手术）+ cli 装配 | test_verify/test_cli 全绿；离线全量 `pytest -q -m 'not integration'` 全绿 |
| 7 | examples/stream fixture + 集成测试 + 四存量 example 回归重跑 | integration 全绿；存量输出与 v1.7 逐字段 diff 仅差 `stream: null` |
| 8 | 手册（25 章新 + 20 章增改 + 5 章重同步）+ CLAUDE/AGENTS/README | 手册样例块与真实运行产物逐字一致 |

**验收标准**（观测定义）：① examples/stream 真实运行——噪声帧进 rejects(reason=noise)、两任务各成一 episode、`_meta.stream.steps` 与人工预期一致（trace 抽查）、任务标签落用户 Schema；② 守恒式（含 episodes/absorbed/dropped_noise）成立、同 seed 重跑逐字节一致；③ `segment.enabled=false` 全量回归：四个存量 examples 输出与 v1.7 逐字段一致（`stream: null` 除外）；④ 25 帧 episode 的 sequence_frames=20 降采样真实端点调用成功；⑤ --strict/dry-run/熔断/中断交付语义符合 §3.7。

## 7. 顺带记录与已知锐边

- **既有缺口明文接受**（不修）：rejects full 档 `raw_last_output` 仅 schema_violation reason 携带——classification_invalid/segmentation_invalid/extraction_invalid 失败行不带原始输出（classify 起既有形态，扩 reason 门列演进候选）。
- **E2E 台账相互作用**：#5（L1 有损修复截断自由文本）适用于 segment reason/extract description/verify detail——不加 minLength（R1 教训：Schema 关键字最小化），靠 l1_lossy 观测；#6（pointwise 温度 0 漂移）佐证 stream 默认只打分不筛。
- **可靠性预算**（S14，写入 spec 风险表与手册调优章）：extract 每步 zero-shot 错误率 20–30%（类型级，W&L/Sharingan 2026 实测）；缓解 = 树 diff 证据 + verify 缺陷路由 + quality 结构分 + by_type 分布可观测；长 episode（>20 步）LLM 判分信度衰减（GUIDE 数据），建议 pairwise 或降信任。
- **确定性条件化声明**（spec §2.6 幂等行）：episode 构成、成员手术以 LLM 输出为条件——同 classify 分池先例；两阶段手术结构保证并发调度不引入额外不确定性（S8）。

# 特性开发规格：活动结构——线索缝合与层级工作单元（spec v1.9）

> **状态：定稿**（2026-07-16）。草案（07-15）经三轮独立验证修订：①功能完整性审计
> （四验收场景全链走查 + 十疑点定点核查，基准 HEAD ca9839c 平铺代码，2 blocker /
> 9 major / 10 minor 全部裁决并入本文）；②deep-search refute/elevate（七方向真实检索，
> 五机制无一被驳倒、三项按证据修订、勘误两处引用）；③**定稿五路复核**（2026-07-16，
> 基准重构分支 7987d2f：代码可行性审计 0 证伪 + 4 GAP、spec/CONTRACTS 清单审计、
> manual/tests/examples 清单审计、二轮 refute/elevate 九承重引用逐字核实、内部一致性
> 审计 2 blocker + 6 major + 5 minor——B-1/B-2、M-1…M-6、m-7…m-11 及全部 GAP/清单
> 缺口已逐项裁决并入本文对应行）。T4 已由需求方裁决（2026-07-16：不做引擎特性）；
> T18 已依需求方 2026-07-16「不允许 defer」指令裁决（机制立项、默认 votes=1 不启用，
> 见裁决行）。**T1–T22 全部闭合，无待裁决项**。编号 T1–T22（决策，裸写）与
> [T-1]–[T-12]（引用，方括号）同文档并存，与 v1.8 的 S1–S32 区隔。
> §3.7 文件修改清单已于 2026-07-16 对**包分层重构 v1.1 严格归档布局**（0cb11cc +
> 7c325dd）完成逐文件·逐章节定稿并经五路复核修正（文档覆盖全量扫描 + 三项布局裁决），
> 随时可进入 spec/*.md 与 CONTRACTS.md 正式修订。

## 1. 背景与动机

### 1.1 需求原型与规范验收场景

采集 x 小时用户自然使用手机的时序流（截图+UI 树，实测单帧 ~650ms），标注出「n 帧组成
的 m 个工作单元」，支持四类标定（需求方 2026-07-16 给定，本 spec 的规范验收场景）：

| # | 场景 | 预期标定 |
|---|---|---|
| V1 串联 | 前 x 帧 A，后 y 帧 B | 2 线索，零缝合 |
| V2 单交叉 | x 帧 A 前半，y 帧 B，z 帧 A 后半 | A=双碎片线索（1 接缝），B=单碎片 |
| V3 多交叉 | a·A₁ + b·B + c·A₂ + d·D + e·A₃ | A=三碎片线索（2 接缝），B/D 各单碎片 |
| V4 噪声 | 流中混入无意义/无法分辨帧 | 噪声帧过滤不入线索；业务短段可被救援 |

验收断言表（examples/thread 与集成测试直接采用；episodes 计 segment 产段数）：

| 场景 | threads | stitched | seams | rescued_short | dropped_noise | absorbed |
|---|---:|---:|---:|---:|---:|---|
| V1 | 2 | 0 | 0 | 0 | 0 | x+y |
| V2 | 2 | 1 | 1 | 0 | 0 | x+y+z |
| V3 | 3 | 2 | 2 | 0 | 0 | a+b+c+d+e |
| V4（w 帧短段命中救援） | 2 | 0 | 1 | w | 1 | x+y+w |

通杀断言：§3.3 守恒全式对每场景闭合，且恒等式 `threads = episodes − stitched` 逐行
成立（2−0 / 3−1 / 5−2 / 2−0，作验收表冗余校验列）；纯噪声会话必须产出 0 缝合（负样本
协议 E2，PIRA-Bench §4.1 [T-1]）。

**V4 规范布局**（表行 seams=1 依赖此布局，fixture 必须遵循——一致性审计假设②钉死）：
`x 帧 A → y 帧 B → 1 噪声帧 → w 帧 A 尾段（< min_len，命中救援）`。救援拼接对
（A 尾成员 × 首个救援帧）的会话序间隙含 B 线索帧 ⇒ 按 T20 判据构成接缝（seams=1）。
若把短段紧贴其线索尾碎片布置（间隙无异线索帧），T20 判定为真实转移、seams=0，
不得用作 V4 fixture。

### 1.2 真机实证动机（2026-07-15 E2E，capture-runs/2026-07-15-*）

- **交叉目标结构性丢失**：跨 App 协作与旅游规划场景被切成互不关联的段，单帧碎片被
  `below_min_len` 吞掉，任务目标从主输出消失。
- **短段吞业务末帧**：任务收尾帧天然易成短段。deep-search 补齐了机理：用户在切换前
  密集执行收尾动作（段落完成率基线 0.78/min → 切换前 10.9–12.8/min，即时响应情形，
  Iqbal & Horvitz CHI 2007 [N-13]）——收尾帧聚集在切换点旁，是 T11 救援的文献级依据。
- **反事实叙事风险**：extract/verify 会编造连续性且可自洽过审。LLM 面对杂乱 GUI 流的
  系统性偏差方向是**过连接**（PIRA 噪声消融：precision 92→51 而 recall 反升 [T-1]；
  IdentifyMe/CORRECT-DETECT 同向 [N-6][N-7]）→ 缝合判定必须保守偏置、接缝零推断。
- **边界漂移**：温度 0 下分段边界跨运行漂移 → 缝合证据用段级摘要（对边界抖动鲁棒），
  且口头置信度门槛不可靠（饱和/过高，[N-10]），稳定化正道是采样一致性（T18）。

### 1.3 一句话设计

新增 **M16 stitch（线索缝合算子）**：segment 之后、dedup 之前，对会话内碎片以
「单调选池 LLM 判定 × 机械先验合取」保守缝合为**线索（thread）**，有界二遍复评修正
贪心误差；接缝零 LLM 机械占位；`below_min_len` 短段先进候选池救援。产出三级结构
**thread ⊃ fragment ⊃ step**（对齐 Ego4D Goal-Step goal⊃step⊃substep [T-6]、
AndroidControl goal⊃instruction⊃action [T-7]）。帧永远单一归属——交叉用"平面分段 +
线索身份"表达（Goal-Step `is_continued` 同型 [T-6]），不引入帧多重归属（T2）。

### 1.4 证据点索引（E1–E10）

> 编号承自 07-15 深检索交付件并在本文全程承重引用；E3/E4/E8 三点未被本 spec 采纳，
> 编号保留不复用（一致性审计：原文档悬空引用，此表补定义闭合）。

| # | 证据点 | 出处 |
|---|---|---|
| E1 | 线索级综合指标 S_final = F1 × FPS_norm（FPS=错缝帧数，FPS_norm=1/(1+ln(1+FPS))）——错缝以乘法惩罚合成 | [T-1] §4.2.3 |
| E2 | 负样本协议：纯噪声会话 GT=空集，必须 0 缝合 | [T-1] §4.1 |
| E5 | resumption 判定单元 =「挂起目标尾证据 × 候选恢复首证据」对（B-1 裁决后由首末帧摘要对承载，见 T8） | [T-2] |
| E6 | cue-guided resumption：「返回同一页面」是任务恢复的强前兆线索 | [N-12][N-13] |
| E7 | 时间衰减先验：挂起时长分布特征显著助益重叠活动识别（+11.36%）；27% 挂起超过 2h 才恢复 | [T-3][N-13] |
| E9 | 解缠质量评估三件套：VI / 1-1 overlap / link-F1 | [N-3] |
| E10 | 通信类 App「天然噪声/穿插高发」名单（产品文档） | [N-18] |

### 1.5 引用背书

#### 1.5.1 主表（T-1…T-12，草案已核实；两处勘误已并入，定稿复核九承重项逐字核实）

| # | 工作 | 背书点 |
|---|---|---|
| [T-1] | PIRA-Bench（Chai et al., arXiv 2603.08013, 2026） | 屏幕流 = 多线索穿插+噪声的形式化（T = ∪任务子轨迹∪噪声，任务子轨迹为**非连续帧子集**）；顺序线程记忆基线 PIRF（**无容量上限**，靠反思删除控规模——勘误：草案曾以"PIRF 同型"论证定容池）；噪声消融证实过连接偏差（GPT-5.2 于 PIRF 框架 precision 92.23→50.52、recall 83.57→84.54 反升；Gemini-3.1-Pro 85.28→53.05 同向，原文 "trigger-happy"）；S_final = F1 × FPS_norm 乘法指标与负样本协议（E1/E2）。注：0 被引新基准（2026-07 复核仍为 0），自报数字权重打折 |
| [T-2] | CIGAR（Hu & Yang, AAAI 2008） | interleaving/concurrent 建模区分；resumption 判定单元 =「挂起目标尾动作 × 候选恢复首动作」转移对（E5） |
| [T-3] | CASAS（Cook et al., IEEE Computer 2013） | interleaved ADL 数据集；时长分布特征使重叠活动识别 +11.36%（E7 时间衰减先验） |
| [T-4] | Robotic Process Mining（Leno et al., BISE 2020；PM Handbook ch.16, 2022） | UI 日志 interleaved 解缠 = open challenge；全局法（trace alignment/频繁模式）依赖"例程重复"前提，对一次性自然任务不可迁移 |
| [T-5] | Agostinelli/Marrella/Mecella（2021/2024）；UiPath/Celonis/Microsoft Task Mining | 学术解缠系列；**三家产品均无穿插解缠**（采集纪律回避/时间邻近分组/按任务录制，[N-18]）——产品空白区 |
| [T-6] | Ego4D Goal-Step（Song et al., NeurIPS 2023 D&B） | goal⊃step⊃substep 三级 + `is_continued` 续接标志——平面分段+线索身份的直接先例 |
| [T-7] | AndroidControl（Li et al., NeurIPS 2024 D&B） | GUI 域层级标配；step:action 1:N |
| [T-8] | GUI Odyssey（Lu et al., 2024） | 跨 App 单目标轨迹形态 |
| [T-9] | OS-Genesis（Sun et al., ACL 2025）；NNetNav（Murty et al., 2024） | 自然流→事后反推任务标注范式；"可命名性"剪枝判据 |
| [T-10] | FineGym（CVPR 2020）；Breakfast（CVPR 2014） | 视频域层级时间标注范式 |
| [T-11] | MultiTHUMOS（IJCV 2017）；Charades（ECCV 2016） | 帧多标签先例——**评估后否决采纳**（T2），引用记录被拒方案 |
| [T-12] | HHMM（1998）；Allen 区间代数（CACM 1983） | 嵌套结构与 during/overlaps 形式语义底座 |

#### 1.5.2 增量表（N-1…N-28，两轮 refute/elevate 检索核实；N-20 起为第一轮增补，N-23 起为定稿复核增补）

| # | 工作 | 承重点 |
|---|---|---|
| [N-1] | Saeedi, Peukert & Rahm, ESWC 2020（FAMER） | 无修复贪心劣于 batch 且次序依赖；**n=1 局部重聚类即追平 batch**——T19 二遍复评的直接依据 |
| [N-2] | Gruenheid, Dong & Srivastava, VLDB 2014 | merge-only 是增量法中质量最差；带修复可达 batch 质量 |
| [N-3] | Kummerfeld et al., ACL 2019 | 对话解缠贪心链接标准解码；并发线程 ≤3 占 46.4%；VI/1-1 overlap/F1 指标（E9） |
| [N-4] | Zhu et al., EMNLP 2020 | 在线顺序解缠端到端先例 |
| [N-5] | Ma et al., ALTA 2021 | 全局二部图匹配仅在图结构已知时胜出 |
| [N-6] | IdentifyMe（NAACL Findings 2025） | LLM 回避"以上皆非"宁可硬连——过连接量化 |
| [N-7] | CORRECT-DETECT（EMNLP 2025） | 准确与弃权此消彼长，默认过度承诺 |
| [N-8] | Wang et al., COLING 2025 | 多候选选择式判定的位置偏差——T20 呈现序与测试要求 |
| [N-9] | LLM-CER："In-context Clustering-based Entity Resolution with Large Language Models: A Design Space Exploration"（arXiv 2506.02509，SIGMOD 2026 排期） | 全局 LLM 聚类自身需防幻觉护栏（MDG）、记录集构成显著影响聚类质量——非免费替代 |
| [N-10] | DINCO（arXiv 2509.25532，ICLR 2026 投稿）；ADVICE（ACL 2026 long, 2026.acl-long.1098） | 口头置信度系统性过高且分数饱和（DINCO："miscalibrated, reporting high confidence on instances with low accuracy"；ADVICE：answer-independence 致 systematic overconfidence）；DINCO 中采样一致性是最强基线、[N-20] 证明一致率与准确率高相关——T9 去 confidence 腿、T18 依据。注意：不表述为"采样一致性是文献最优"（DINCO 本身超越 SC） |
| [N-11] | arXiv 2604.18835（2026） | LLM 成对判定对位置/上下文结构敏感 |
| [N-12] | Altmann & Trafton, CogSci 2004；Trafton et al., IJHCS 2003 | cue-guided resumption——E6"返回同一页面"先验机理 |
| [N-13] | **Iqbal & Horvitz, CHI 2007**（doi 10.1145/1240624.1240730） | 真实桌面日志：**挂起窗口均值 3（S.D.≈2）**（三情形逐字核实）——max_open=4 的实证锚点（勘误一：替换草案误引的"PIRA 平均并行意图 ≤3"，论文无此数）；**27% 的挂起超过 2 小时才恢复**（time until resumption >2h，时间衰减依据——勘误二：定稿复核纠正草案"不恢复"措辞，论文原文为恢复时长而非放弃率；"不恢复"最近原文是"切换前专注 <5min 的任务有 10% 概率 2h 内未恢复"）；切换前收尾密集（T11 机理，即时响应情形 10.9–12.8/min） |
| [N-14] | González & Mark, CHI 2004/ECSCW 2005 | working spheres 多任务粒度人因基线 |
| [N-15] | Leiva et al., MobileHCI 2012；Jones et al., UbiComp 2015 | 手机域中断/回访实证 |
| [N-16] | SWISH（IUI 2006）；TaskPredictor（IJCAI 2007）；Rath et al. 2013 | window title 最强单特征（85.57%）；多窗证据聚合优于单窗——摘要卡证据面依据 |
| [N-17] | Log2Plan（UIST 2025） | embedding 召回 + LLM 精判两级任务组匹配工业近例 |
| [N-18] | UiPath/Celonis/Microsoft 官方文档 | 产品无穿插解缠 + 通信类 App"天然噪声"名单（E10） |
| [N-19] | Leno et al., ICPM 2020；arXiv 2510.08118（2025） | 无分段 UI 日志例程识别全局法的"重复例程"前提 |
| [N-20] | Self-Consistency（Wang et al., ICLR 2023, arXiv 2203.11171） | 单模型 n 采样多数决的奠基工作；一致率是可靠置信信号（原文 §3.5/Fig.8 "one can use self-consistency to provide an uncertainty estimate"——T18 votes 的机制出处） |
| [N-21] | PoLL（Verga et al., arXiv 2404.18796, 2024） | 异家族小模型评审团 > 单大裁判且省 7 倍、消自偏好——"多模型单次"路线代表作。注：其"收益前提是异家族"为论文归因性论断（无单家族对照消融），实测支撑在 [N-22]（定稿复核归属修正） |
| [N-22] | LLMs as a Jury: Cross-Model Consensus Can Outperform Process Reward Models for LLM Reasoning（arXiv 2607.10139, 2026） | 精确区分两路线（§2 逐字："self-consistency is better calibrated while the cross-model signal is more accurate"）；自一致性修不了系统性偏差（模型把自己的错投成多数）；实测错误相关性 within-model 0.68 > same-family 0.52 > cross-family 0.47，共享错误地板 = "unanimous and wrong"——T18 选型中"评审团修不了跨家族共享的过连接偏差"的对偶论证 |
| [N-23] | Takada & Mori, "Rethinking Dialogue Disentanglement for LLMs via Dialogue-Level Assignment and Subsequent Context"（LaCATODA@AAAI-26, CEUR-WS Vol-4178, 2026） | 与 T8 **同形制的直接 SOTA 先例**：簇摘要呈现 + LLM 归簇或判 new + 顺序贪心（GreedyDisentangle），在 Kummerfeld IRC 基准全指标超 per-pair 与非 LLM 方法——单调选池从"跨域类比"升格为"同任务同形制先例"；其 subsequent-context 显著有效结论为 T19 后见复评的第三依据。风险注记：DD-GEPA（arXiv 2606.07894）报告 ~30B 开源模型上此法性能骤降——stitch.llm 选型需实测（部署纪律 glm-5.2 单端点，§3.6 门禁覆盖） |
| [N-24] | Engram（arXiv 2606.09900, 2026）；Memori（arXiv 2603.19935, 2026）；综述 arXiv 2603.07670 | 精选紧凑上下文准确率**反超**全量原始历史（+10.4 pt，83.6% vs 73.2%，LongMemEval，token 少 8×）；结构化摘要以 ~5% token 胜过其它记忆系统——摘要卡证据面的正面抬升；综述给出反向风险正式命名 "**summarization drift**"（§5.2 风险条采用该术语） |
| [N-25] | "When LLMs Agree, Are They Right?"（arXiv 2607.08065, 2026, 265k 样本审计） | 前沿模型高自一致处**过度自信**：GPQA 上 77% 条目自一致 ≥0.8、其中 48% 是错的——自一致性只是条件性代理，**votes 治方差（漂移）不治偏差（过连接）**，与机械先验合取不可互替（T18 分工论证的直接量化） |
| [N-26] | "Nine Judges, Two Effective Votes"（arXiv 2605.29800, 2026） | 9 裁判 7 家族评审团有效独立票仅 ≈2.0–2.5，聚合算法最多弥合 11% Condorcet 缺口——T18 拒绝评审团路线的第二记实锤 |
| [N-27] | Tian, Zhou & Pelleg, "Characterization and Prediction of Mobile Tasks"（ACM TOIS 41(1), 2023, doi 10.1145/3522711） | 人工标注 1414 个真实手机任务：仅 **22.6% 任务存在穿插**、多日志任务均 4.1 logs/1.7 apps——手机穿插深度比桌面浅，桌面锚 max_open=4 在移动域为**宽松上界**的间接佐证 |
| [N-28] | GraphCR（ACM JDIQ, doi 10.1145/3735511, 2025）；Alper（arXiv 2605.25814, 2026） | 更重的簇修复机器（图度量分类器+LLM 主动学习 / 全局演化图概率标签传播）——**已评估、按规模不采**：会话内碎片 ≤ 数十，n=1 复评（[N-1]）够用且零训练（T19 显式裁决，完善证据闭环） |

**Refute 结论**（两轮合并）：顺序贪心池/摘要卡对判/保守合取/短段救援/max_open=4 五机制
**无一被驳倒**；第一轮修订三项（T19 二遍复评、T8 封闭策略、T9 去置信度腿）；定稿复核
九承重引用无一虚构（唯 N-13 措辞勘误已并入），机制 1/2/3 获 2026 新证据显式抬升
（[N-23]/[N-24]/[N-25][N-26]），且逐类排查确认**无 2025–2026 工作使 M16 设计过时**
（PIRA/PIRF 是意图推荐框架非标注管线且自报 F1 仅 ~50–57%；2026 大规模 GUI 轨迹挖掘
管线均显式过滤杂乱自然流只吃干净教程视频——工业回避与 [N-18] 并列，作 §5.1 动机旁证）。
主流训练集消费平面轨迹（refute 线复核不变）→ thread 必须可平面化导出，下游零适配。

## 2. 设计裁决记录（T1–T22，全部闭合）

| # | 问题 | 裁决 | 依据 |
|---|---|---|---|
| T1 | 交叉的表达模型 | **线索身份缝合**：episode 平面互斥分区不动，M16 合并同线索碎片为线索信封，`_meta.stream.fragments` 保留碎片结构 | Goal-Step `is_continued` [T-6]；PIRA 非连续帧子集 [T-1]；守恒零侵入 |
| T2 | 帧多重归属 | **否决** | 单前台屏无真并发 [T-2]；帧单一 absorbed 是手术/归因/守恒公共地基；无消费方证据 |
| T3 | 层级模型 | **三级 thread ⊃ fragment ⊃ step**；不做帧级区间树 | [T-6][T-7]；区间树使 M7 手术语义爆炸 |
| T4 | episode 内子任务跨度 | **已裁决（需求方 2026-07-16）：不做引擎特性**；标注层模式（用户 schema `subtasks: [{label, step_range}]`）+ 手册指引 | 需求方确认下游不消费；refute 线无消费方 |
| T5 | 算子形态与链序 | 新算子 **M16**，`segment → stitch → dedup → classify → extract → quality → generate → annotate → verify`；默认 off，off 时字节等价 v1.8（回归锚；范围钉死见 §3.1 退化锚/m-11：主输出+rejects+report.json，dry-run stderr 行例外） | 缝合改成员集 ⇒ 先于 dedup/extract；`_CHAIN_ORDER` 插位一处（+`_compose_chain` 映射，见代码表） |
| T6 | Stage 契约例外 ②c | 授权三件事：①被并碎片壳置 `status="stitched"`；②幸存信封 Record 重绑（成员按序键升序拼接，**record.id 不重算**——M7 手术先例，thread_id == 幸存信封 episode_id）；③`below_min_len` 来源帧 dropped_noise→absorbed 翻转（②b 双向豁免的 M16 延伸，仅限救援命中）。**幸存者规范句（m-7）**：一遍中幸存信封恒为**线索创始信封**（开线索者），被并候选信封作壳；二遍复评方向相反——单碎片线索作候选方并入目标线索，**目标线索信封幸存**、候选信封作壳（fragments 按会话序重排，episode_id/thread_id 随幸存信封，T22） | 审计 major：草案原措辞未授权③；②b/②a 形制镜像；一致性审计 m-7 补幸存者规范句 |
| T7 | 新状态与守恒 | Status 增 `"stitched"`（壳终态）；守恒全式、failed 兜底公式、unprocessed 残差公式**三处同步**扩 stitched 项（§3.3）。**壳的范围规范句（一致性审计）**：`stitched` 仅计被并 **episode 信封**壳；救援短段无信封形态（由帧重组），命中只计 `rescued_short` 帧翻转、**不产生壳**。`counts.threads` 由 M10 以恒等式 **`threads = episodes − stitched`** 在 post-emit tally 处导出（可行性审计验算恒等：救援只并入不开新线索、壳一对一抵扣、降格段照常入池、fanout 后置无交互——counts.* 属主仍归 M10） | 审计 blocker-1：只改全式会使 failed 兜底误计、残差破缺；可行性审计 GAP：threads 计量机制补导出式 |
| T8 | 判定形态与证据面 | **单调选池**：每候选一次调用，提示词呈现池内全部开放线索摘要卡（**按最近活跃降序**，缓解位置偏差 [N-8]）+ 候选摘要卡，输出 `thread_ref \| new`。摘要卡结构化字段（**B-1 裁决**：链序上 extract 后置、stitch 运行时批内无任何 Transition，"动作"无生产者——证据面全部降格为帧摘要级）：App 集合、**线索尾帧摘要 × 候选首帧摘要对**（E5 的帧级承载，[T-2] 降为概念映射：挂起尾×恢复首的转移对判定单元）、首末帧摘要、`tree_diff` 变更证据（app_changed/title_changed）、时间/序号跨度、碎片数、线索任务名（滚动更新）。卡内嵌入的每个帧摘要沿用 segment `digest_max_chars` 同名键语义截断（m-9：删除"单侧上限"措辞，卡的结构化字段有界由构造保证）。池容量 `max_open=4`（**锚点：挂起窗口均值 3 + 1 活跃**，[N-13]；移动域佐证 [N-27]）；**封闭策略（M-3 裁决）**：封闭**仅发生于池满逐出**（无主动封闭），逐出优先级 = ①挂起跨度超 `stale_gap_steps` 者优先（键复用 T9 时间衰减阈值，0=该腿失效）→ ②LRU 兜底；**完成感知腿撤除**（B-1：无动作生产者，收尾模式不可判——记入 §4 非目标/§5 演进注记）。封闭 ≠ 终结的精确含义：被逐出线索不再出现在一遍摘要卡中，但**保留在 T19 复评目标集**并照常产出（PIRF 反思删除的批处理对应物 [T-1]，27% >2h 才恢复 [N-13]，时长特征 +11.36% [T-3]；同形制 SOTA 先例 [N-23]） | 审计 major：草案 per-pair 与成本模型互斥，单调选池胜出（调用减半且免多命中仲裁）；一致性审计 B-1/M-3/m-9 三项裁决并入；deep-search 修订封闭策略与锚点勘误 |
| T9 | 保守偏置 | `bias="conservative"`（默认）：并入需 **LLM 判 resume ∧ 机械先验命中**。先验白名单（析取三腿）：①App 交集非空；②候选首帧摘要 × 线索尾帧摘要实体重叠；③**返回同一页面/Activity**（候选首帧树根页面标识 == 线索某碎片尾帧，页面标识 = app+activity(+title)，E6 [N-12][N-13]）。**提取实现裁决（可行性审计 GAP）**：app/activity/title 提取循环由 `stitch.py` 自带副本（先例 `extract._diff_text` 副本，零 `types.py` 表面改动；不反解析 `frame_digest` 渲染串）。**数据依赖声明**：③依赖采集侧 dump 将 activity 写入 `extra`（"often absent"，types.py 注），缺失时该腿静默失效——析取降格可接受。**去除 confidence=high 腿**（口头置信度饱和不可靠 [N-10]）；confidence 字段保留仅作 trace 观测。时间间隔衰减（E7）：候选与线索尾的跨度超 `stale_gap_steps` 时先验降格（须两腿命中） | 过连接偏差三方实证 [T-1][N-6][N-7]；③补齐"App 交集同 App 恒真/跨 App 为空"两个失效面 |
| T10 | 接缝转移 | **零 LLM 机械占位**，四键钉死：`{action_type: "app_switch", target: null, value: null, description: "线索接缝：被<打断者>打断后恢复"}`，`detail={kind: "thread_seam", interrupted_by: [...]}`；步行 `resumed=true`（落接缝步自身，emitter 由 detail.kind 推导）。按 T20 判据接缝间隙恒含异线索帧 ⇒ `interrupted_by` 恒非空。**语义备注（M-1）**：占位 `action_type="app_switch"` 对同 App 内穿插（返回同页型）语义不贴——占位类型不承诺语义，下游以 `detail.kind` 判别 | 接缝是已知中断，推断徒增反事实面；审计 minor-7 键值/落点钉死 |
| T11 | 短段救援 | `below_min_len` 短段（判别载体 = 帧信封的 `noise_attribution == ("segment","below_min_len")` duck 标，代码既有已核 `segment.py`）按会话序插入候选流；命中→并入 + 帧翻转（T6③）、`rescued_short` **累加翻转帧数**（m-10：单位=帧，非救援事件数）；未命中→维持 dropped_noise 原 reason 落 rejects，**永不开新线索**（B-2，见 §3.1 规范句）。**噪声帧（reason="noise"）不入候选池**（V4 拒绝路径闭合）。**候选重组语义（可行性审计 GAP 裁决）**：`noise_attribution` 二元组不携带原短段身份——采**连续 run 重组**：会话序上连续的 below_min_len 帧（中间无任何其他帧）重组为**一个**救援候选，与 segment 原切分不再一一对应（相邻两短段合为一候选；混合任务 run 因先验难命中而维持 dropped，保守面兜底）——`segment.py` 零改动。`below_min_len` 计数器为发生计数（帧口径），救援不回退 | 审计场景 4 走查：载体、未命中路径、计数语义三处钉死；机理 [N-13]；GAP 裁决 (a) 连续 run 语义 |
| T12 | 线索作用域 | 不跨 session、不跨 batch；hard-split 边界不可缝（session_split 标记照旧 + WARN 提示调大 batch_size）；segment `on_error="keep"` 的整会话 degraded episode **照常入池**（合法 episode） | 装箱与两阶段手术批内确定性依赖；审计 minor 补 degraded 声明 |
| T13 | dedup 判重面 | 线索信封 `_dedup_text` = 成员配方按序拼接（S10 机制原样）；stitched 壳被既有 `status=="active"` 过滤天然排除（**dedup.py 零改动**，审计证伪原清单条目） | 审计核查点 6 |
| T14 | classify/quality/annotate 适配 | 以线索为单元；quality/verify 的步行渲染对 `detail.kind=="thread_seam"` 加专用后缀「（线索接缝：被 X 打断）」，与既有 extraction_invalid 后缀并列——防 trajectory rubric 的 noise_residue/coherence 判据把接缝当噪声残留或无法解释跳变扣分；annotate 关键帧降采样升级为**按碎片配额**（每碎片至少保底 1 帧，防小碎片被均匀采样整段抽空——审计 minor-8 反例）。**穿参义务（可行性审计）**：碎片跨度在信封 duck 标上而 `build_annotate_prompt`/`annotate_record` 只收 Record——增第三个 additive trailing kwarg（S5 "second additive trailing-kwarg" 形制），`annotate.py` 主调用与 `verify.py` 修复重标调用**两处调用点一并穿参**（否则修复重标丢配额）；classify `_fan_out` 复制 `thread_id`（真字段进构造）与 `seam_indexes`（进 D6 标记循环） | 审计 major-7/minor-8/核查点 10；可行性审计穿参义务 |
| T15 | verify 适配 | 序列 prompt 六段 → **七段**：新增 **[片段结构]** 节（每碎片：线索内序数/帧跨度/首帧摘要 + 接缝位置表）——无此节 wrong_stitch 不可判（审计 major-4）；缺陷表增 `wrong_stitch`（defect schema、DEFECT_KINDS、report `_DEFECT_KINDS`、`_route_defects` 四处同步），路由 = **mark-only + fail**（独立分支，不得落入 missing_* 回收扫描）；边界余量维持首碎片头/尾碎片尾邻帧，`_session_episodes` **过滤 stitched 壳**（防"第 n 段"序数被壳污染——审计 major-5）；成员手术回收扫描语义不变（异线索 absorbed 帧按 v1.8 D5 已判 neighbor mark-only） | 审计 major-4/major-5 + 核查点 3 |
| T16 | 观测与报告 | trace 通道 10→11（`stitch`）；事件 `stitch.judge`（record_ids=候选碎片首成员 id；verdict/thread_ref/confidence/先验命中腿）与 `stitch.thread`（fragments 跨度表）；`task_name` 入 obslog `_FREE_TEXT_KEYS` 脱敏；`report.stream.stitch = {stitched, rescued_short, seams, judgments, repass_judgments, failures}`（threads 仅落 `counts.threads` 单点 = `episodes − stitched` 导出式，T7，避免双落点）；batch.end payload 增 stitched/threads（R20 形制）；dry-run 估算行增 `stitch_calls`，估算式对齐 S22 文化：`stitch_calls = len(session_lens) × votes × (2 若 repass 否则 1)`（episodes ≈ sessions 下界基数，沿用既有 stderr 下界注；off 时恒 0 且行无条件打印，v1.8 segment_calls 先例）；`_meta.stream` 增 `thread_id`、`fragments: [{order_span, member_count, cause, source_episode}]`（cause ∈ `"origin" \| "resumed" \| "rescued"`）、steps 行内 `resumed`。**条件在场规则（m-11 正文化）**：counts.stitched/threads、report.stream.stitch、batch.end 新字段、`_meta.stream` 新键（thread_id/fragments/resumed）**仅 `stitch.enabled=true` 时在场**——off 时主输出/rejects/report.json 逐字节等价 v1.8 的充分条件。**stderr 声明**：进度条与终版摘要为固定键集，stitched 不显示（R20 文化，有意为之）。**实现定稿四则**：①obslog 事件名常量 `EV_STITCH_JUDGE`/`EV_STITCH_THREAD`（镜像 EV_SEGMENT_BOUNDARY）；②`judgments`/`repass_judgments` 计**逻辑判定数**（每候选 1，失败不计），votes>1 只放大**调用**数不放大判定数（dry-run 估算为调用口径 ×votes）；③`_meta.stream` 新键位置冻结：`thread_id` 紧随 `episode_id`，`fragments` 在 `degraded` 后、`steps` 前；④seam 占位除计数器外**亦不发 extract.step 事件**（合成步的 record-pair payload 是编造——接缝可观测性由 stitch.thread 承担） | 审计 minor 群 + 核查点 2；一致性审计 m-11、可行性审计 stderr/估算式裁决；实现定稿 2026-07-16 |
| T17 | 配置面 | 新节 `[stitch]`（§3.4 全表）；M1 约束：`stitch ⇒ segment`；`stitch + strategy="rules"` WARN；`votes` 偶数 = 配置错误。**no-op 警告归属分支（可行性审计）**：既有 parked 名单警告在 segment 关分支内——"`[stitch]` 有 payload 而 stitch 关、segment 开"的组合仿 `sequence_frames` 单独警告（loader 既有形制），不落 parked 名单分支 | §2.3.1 形制；可行性审计分支归属 |
| T18 | 缝合稳定性 votes | **已裁决（需求方 2026-07-16「不允许 defer」指令）：机制立项落地，默认 `stitch.votes = 1`（不启用采样，单调用）**；>1 时同判定 n 次采样多数决。**聚合键（M-4 裁决）**：多数 = 对 **(verdict, thread_ref) 完整判定**的严格多数（> n/2）；任何不足严格多数的分裂（含 verdict 多数但 thread_ref 分裂）一律回落保守结局——episode 候选 = `new`、救援候选 = 未命中；task_name/reason 取多数簇内首个采样。**依据**：口头置信度门槛被证不可靠 [N-10]，采样一致性是模型无关的可靠替代——votes 是"置信门槛的正规替代"；但 [N-25] 量化其边界：自一致高 ≠ 对（votes 治方差/漂移，不治偏差/过连接——与机械先验合取不可互替）。代价 = 判定调用 ×n（n=3 时全链占比 <8%，同模型同前缀吃 prompt 缓存）；采样温度 = profile 默认（[stitch] 11 键表冻结、无 sc_temperature 键——与 T18 服务端漂移论证一致，实现定稿）。**路线选型（业界两路线对照后裁决）**：采"单模型多次"（self-consistency [N-20]）而非"多模型评审团"（PoLL [N-21]）——漂移（方差病）→ votes；过连接（偏差病）→ 机械先验合取。评审团修不了我们的偏差病：PIRA 消融显示过连接是**跨家族共享偏差**（GPT-5.2 与 Gemini 同向 trigger-happy [T-1]），异构裁判会把共享偏差投成多数 [N-22]，且有效独立票仅 ≈2 [N-26]；部署纪律为 glm-5.2 单端点。若将来第二模型家族进场，`stitch.judges` 可镜像既有 `verify.judges` 模式作纯配置扩展（§5 演进注记） | [N-10][N-20][N-21][N-22][N-25][N-26]；成本×稳定性产品取舍；M-4 聚合键裁决 |
| T19 | 有界二遍复评（新增；**M-2 重定义**） | 会话候选流一遍结束后：复评候选 = **一遍结束时的单碎片线索**，按其碎片会话序逐个处理；每候选一次调用（同 T8 单调选池形制），**池 = 该会话全部其他线索**（按最近活跃降序呈现，超 `max_open` 张时按与候选跨度最近截取——放弃草案"时间窗重叠"谓词：严格区间相交在线性流上仅命中"包络内嵌"一型，排除 V2/V3 漏判自证场景，一致性审计 M-2 证伪）；命中（T8 判定 ∧ T9 合取）→ 并入：候选信封作壳、目标线索幸存（T6 幸存者句）。**目标集取活视图**（复评中的并入即时更新各线索跨度与卡片）；候选集 = 一遍结束时点快照，仅**被并走**的候选出列（复评中获得新碎片者仍照判）；被转移的 origin 碎片 cause 重标 `"resumed"`、rescued 保持 `"rescued"`，救援碎片 `source_episode = null`（实现定稿）；无其他线索时跳过（零调用）。预算 ≤ 单碎片线索数（自然流每小时约 +10–15 次调用，<5%）。修正顺序贪心的漏缝自增殖（V2 全漏型与 V3 A₂ 漏判两向失效链均可修——M-2 推演核实）。**残差声明**：池截取排除目标 / 双多碎片线索误分裂不在修复面内，由 §3.6 真机门禁兜底 | FAMER n=1 重聚类追平 batch [N-1]；merge-only 最差 [N-2]；后见上下文有效 [N-23]；更重修复机器已评估不采 [N-28] |
| T20 | 接缝 × extract 协作（新增；**M-1 判据钉死**） | M16 在幸存信封挂 `seam_indexes: tuple[int,...]` duck 标；**坐标规范句（m-8）**：元素 = 接缝对**左成员**在重绑成员元组中的下标，与 `Transition.index`/`steps[].index` 同坐标，值域 `[0, len(members)−2]`；与 `order_span` 的会话序键空间无换算关系。**接缝判据（M-1 裁决）**：拼接对构成接缝 ⟺ 两成员的会话序间隙内**含 ≥1 个归属其他线索的帧**（absorbed 于异线索碎片）；间隙仅含噪声帧/本线索救援帧时**不是接缝**——该对照常送 LLM 摘取，与 v1.8 "成员相邻为准、剔噪对照常摘取"惯例（315 §3.15.2）完全一致，同一物理情形单一处理。推论：接缝的 `interrupted_by` 恒非空（T10）。**extract 对 seam 序数生成 T10 占位（零 LLM）、其余照常摘取**——保持 `transitions is None` 幂等门与 `len(transitions)==len(members)-1` 不变量；**计数器口径（可行性审计 GAP）**：seam 占位**不计入** `report.stream.extract.transitions` 与 `extract.by_type.*`（非摘取产物，防零 LLM 的 app_switch 灌污 by_type；接缝唯一计量点 = `stream.stitch.seams`，§6.4 注明）；**实现注记**：跳 seam 序数需重构 extract 平铺 gather 记账（`spans`/切片假设每 pair 一协程），属实质改动、行内点名。**相邻救援不盖接缝**：会话位置紧邻的拼接对是真实转移，照常送 LLM 摘取；`seams` 计数 = 满足 M-1 判据的拼接处数 | 审计 blocker-2 + 场景 4 相邻救援语义；一致性审计 M-1/m-8、可行性审计计数器口径与 gather 注记 |
| T21 | M11 第四路由（新增） | `emit_batch` 增 `status=="stitched"` 路由：不写主输出、不写 rejects、仅计数（absorbed 同款）——壳不得落入 else→rejects 兜底（否则以 internal_error 之名污染 rejects 且 `--strict` 必退 1）；`--strict` 语义声明：stitched 壳与 rescued 帧不构成 rejects，同输入开启 stitch 后 strict 结果可能 1→0，**属预期**（spec §2.4 措辞补注） | 审计 blocker-1 + minor-9 |
| T22 | steps 编号与身份链（新增） | thread 的 `steps[].index` 全线索连续 0..n−2（代码三处不变量唯一解：`Transition.index` 恒等位次、`_rebuild_episode` zip 全对齐、emitter 渲染）；`episode_id` = 幸存信封 record.id = `thread_id`（stitch on 时语义为线索 id，off 时二者天然同值）；碎片原 episode_id 记录于 `fragments[].source_episode`（审计 minor 身份链钉死） | 审计核查点 3 + minor 群 |

## 3. 规格正文

### 3.1 M16 stitch 算法（新文件 `spec/316-m16-stitch.md`）

按会话独立执行；输入 = 会话内 segment 产物（active episodes + below_min_len 帧按
连续 run 重组的候选短段（T11），按会话序合流）。候选分两型，处理规则不对称
（**B-2 裁决规范句**）：

1. **一遍（单调贪心）**：逐候选处理。
   - **episode 候选**：池空时判定照常发起（呈现零张线索卡，verdict 恒 `new`、
     thread_ref 恒 null——`task_name` 由此自举，是线索命名的唯一来源，**M-6 裁决**）；
     判 new 或全不命中 → 开新线索；命中（T8 判定 ∧ T9 合取）→ 并入：幸存信封
     Record 重绑、候选壳置 stitched、更新线索摘要卡（尾帧摘要/跨度/任务名滚动更新）。
   - **救援短段候选（B-2）**：**永不开新线索**。池空 → 跳过判定（零调用），维持
     dropped_noise；池非空 → 判定，命中 → 并入 + 帧翻转（②c③，T6）计 rescued_short；
     未命中（含判定失败）→ 维持 dropped_noise 原 reason。
   - **池满且需开新**（仅 episode 候选触发）→ 按 T8 逐出优先级挑一条封闭（**封闭仅
     发生于此**，M-3；封闭 ≠ 终结：不再出现在一遍卡集中，但保留在二遍目标集与产出中）。
2. **二遍（有界复评，T19/M-2）**：复评候选 = 一遍结束时的单碎片线索（按碎片会话序）；
   每候选一调，池 = 会话内全部其他线索（最近活跃降序，超 max_open 按跨度最近截取）；
   命中执行并入——**方向相反**：候选信封作壳、目标线索幸存（T6 幸存者句）；目标集
   取活视图；无其他线索 → 跳过。
3. **接缝标定（T20/M-1）**：对每条多碎片线索计算 `seam_indexes`（判据 = 拼接对会话序
   间隙含 ≥1 异线索帧；左成员下标坐标，m-8）并挂 duck 标；间隙仅含噪声/本线索救援帧
   的拼接对不入（该对照常送 extract 摘取）。
4. **失败语义**：单判定 M8 修复耗尽按 `stitch.on_error = "keep"`（默认：episode 候选
   开新线索 + 留痕**两件**——error 事件 + `stitch.failures` 计数器；不同于 segment
   S26 三件套：T16 的 m-11 封闭键清单没有 stitch 降格 `_meta` 键，故无 meta 标记；
   keep 开出的线索 `task_name=""`，摘要卡渲染为「（未命名）」；救援候选维持
   dropped_noise + 同款留痕）| `"fail"`（**仅 episode 候选信封**置 failed,
   kind=stitch_invalid——成员帧维持 absorbed，M7 fail 先例，②c 授权面不含
   absorbed/dropped_noise→failed 帧迁移；救援候选不适用 fail 路径，判定失败一律按
   未命中处理，**B-2 裁决**）。**二遍判定失败**：无论 on_error 取值一律降格为
   keep 等价（已开线索无从 fail），计数器与 error 事件照发（实现定稿）。
   **会话串行**：池是串行决策过程，会话内候选顺序处理（确定性事件序、零 RNG）；
   并发仅存在于 votes>1 的采样 gather 内。

判定 Schema（M8 内部 Schema，`schema_engine.stitch_schema()`）：

```
{"verdict": "resume" | "new",
 "thread_ref": <池内线索序数 | null>,
 "task_name": <string，线索任务名（滚动更新）>,
 "reason": <string>,
 "confidence": "high" | "medium" | "low"}   # 仅 trace 观测，不进门槛（T9）
```

**退化锚（m-11 范围钉死）**：单碎片会话/全 new 判定 → 产出 = v1.8 形态（thread=单碎片、
fragments 长度 1、零接缝）；`stitch.enabled=false` → **主输出、rejects、report.json
逐字节等价 v1.8**（依赖 T16 条件在场规则：全部 v1.9 新键仅启用时出现）；**dry-run
stderr 例外**：`stitch_calls=0` 行无条件新增（v1.8 segment_calls 先例同型）。T22 的
"off 时 episode_id 与 thread_id 天然同值"是概念性陈述——off 时 `_meta.stream.thread_id`
键不在场。

### 3.2 数据结构与契约增量（spec §4 / CONTRACTS）

- `Status` 增 `"stitched"`；Stage 契约新例外 **②c**（T6 三件事全文 + 幸存者句）。
- `PipelineItem` 增 `thread_id: str | None`；duck 标三件（M16 盖章，**实现定稿**）：
  ① `seam_indexes`（坐标语义见 T20/m-8：左成员下标、与 `Transition.index` 同坐标、
  值域 `[0, len(members)−2]`、与 order_span 键空间无换算）；② `seam_interrupted_by:
  tuple[tuple[str,...],...]`（与 seam_indexes 逐位对齐的打断者任务名组——T10 占位
  文案需要打断者名而 extract 无跨线索视野，只能由 M16 计算随标传递）；
  ③ `stitch_fragments`（emitter 渲染 `_meta.stream.fragments` 的载体，元素
  `{order_span, member_count, cause, source_episode}`）。`classify._fan_out` 复制
  清单增四项：thread_id（真字段进构造）+ 三 duck 标进 D6 标记循环。
  另有帧级留痕 duck 标 `rescued_by = <幸存线索 record.id>`（救援翻转帧盖章，
  `noise_attribution` 保留作证据；帧不扇出、永不输出——审计通道专用）。
- `Transition.detail` 保留键 `"kind": "thread_seam"`（与 `extraction_invalid` 并列）。
- `_meta.stream` 增量：`thread_id`、`fragments: [{order_span, member_count, cause,
  source_episode}]`、steps 行内 `resumed`。**顶层 `order_span` 为包络**（多碎片线索
  含异线索帧）——规范句：下游切片必须用 `fragments[].order_span`，不得按顶层跨度
  切片（审计 major：order_span 语义）。
- 缺陷表 `defects.kind` 增 `wrong_stitch`（四处同步 + 独立 mark-only 路由，T15）。
- `errors.ErrorKind` 增 `STITCH_INVALID`。

### 3.3 守恒代数（spec §6.4 增量；审计 blocker-1 三处同步）

```
守恒全式：
emitted + dropped_dup + dropped_lowq + dropped_verify + failed + bad_input
        + absorbed + dropped_noise + stitched [+ unprocessed]
  = scanned + generated [+ fanout] [+ episodes]

failed 兜底公式（orchestrator post-emit tally）：终态减项同步增 − stitched
unprocessed 残差公式（熔断/中断）：减项同步增 − stitched
```

`rescued_short` 帧同批内 dropped_noise→absorbed 翻转（**单位 = 帧**，m-10），账目在
emit 前定格（D4"路由时计数"口径）；`below_min_len` 为发生计数不回退（T11）；
`counts.threads` 单点上报，M10 post-emit tally 处以 **`threads = episodes − stitched`**
导出（T7，四场景验收表冗余校验列同式）；fanout（右侧）与 stitched（左侧终态）分别计
信封存在与壳终态，经审计数值验证无双记。**条件在场规则（m-11 正文）**：本节全部 v1.9
新键（counts.stitched/threads、stream.stitch 子块、守恒式 stitched 项）仅
`stitch.enabled=true` 时出现在 report.json——off 时守恒全式回落 v1.8 形态，字节等价。

### 3.4 配置规格（spec §5.2 新节 `[stitch]`）

```toml
[stitch]
enabled = false          # 总开关；true ⇒ segment.enabled（M1 约束）
llm = "default"          # 判定 profile；进引用集（纯文本证据，无视觉必需）
max_open = 4             # 开放线索池容量（挂起窗口均值 3 + 1 活跃 [N-13]）
bias = "conservative"    # LLM×机械先验合取 | "llm"（纯 LLM 判）
rescue_short = true      # below_min_len 短段先进候选池（T11）
repass = true            # 有界二遍复评（T19）；false = 纯一遍贪心
stale_gap_steps = 0      # 时间衰减阈值（序号差；0=不启用）；双职：T9 先验降格 + T8 池满逐出优先腿；与 stream.gap_steps 语义区分
digest_max_chars = 400   # 卡内嵌入的每个帧摘要截断上限（沿用 segment 同名键语义，m-9）
context = ""             # 可选域上下文（何为"同一任务"的领域提示）
votes = 1                # T18 已裁决：默认 1 不启用；>1 = n 次采样、(verdict,thread_ref) 严格多数决（M-4；偶数=配置错误）
on_error = "keep"        # "keep" | "fail"（fail 仅施于 episode 候选信封，B-2）
```

### 3.5 成本模型（单调选池修订；M-5 算术勘误）

1 小时自然流 ≈ 2400 帧 / 15 会话 / 40 episode / 25 thread：

| 阶段 | 调用量 | 备注 |
|---|---|---|
| segment 滑窗 | ≈ 126 | window=20 |
| **stitch 一遍** | ≈ 40（每 episode 候选一调，单调选池；救援候选仅池非空时 +ε） | T8/M-6；较草案 per-pair 减半 |
| **stitch 二遍** | ≈ 10–15 | T19 预算 ≤ 单碎片线索数（无其他线索者跳过、零调用） |
| extract | ≈ 帧 − threads − seams = 2375 − seams（全接缝时 = 帧 − episodes = 2360） | 接缝零调用（T20）。M-5 勘误：草案"2400−40−seams"双扣接缝——重绑后调用量 = Σ(len(members)−1) − 占位数，"−episodes"里已含 seams |
| quality/annotate/verify | ≈ 3×25 (+repair) | 单元 episode→thread，调用下降 |

行和 ≈ 2614 ≈ **1.1×帧数 + 3×threads**（M-5 勘误：草案"1.3×帧数 + 5×threads"由任何
行组合推不出）；stitch 全口径占比 ≈ 2.0% **<3%**（votes=3 时判定 ×3，占比 ≈ 5.8%
**<8%**——两占比断言在勘误后口径下反而精确成立）。

### 3.6 测试与验收计划

- **examples/thread/**：V1–V4 四场景 fixture（V3 五段式 + V4 按 §1.1 规范布局含短段
  救援），验收断言表 §1.1 逐项 + 守恒闭合 + `threads = episodes − stitched` 冗余列；
  **负样本 fixture**：纯噪声会话 → 0 缝合（E2）。**会话分区机制裁决**：`[stream]`
  `key="source_dir"` 按场景子目录分会话（跨子目录配对既有先例；index 命名空间全树
  唯一、各场景错开编号），fixture 生成对照 examples/stream 形制（`tools/gen_fixtures.py`
  确定性 PIL 绘制、无 README）。
- **tests/operators/test_stitch.py**（新）：摘要卡确定性/先验三腿/单调选池呈现序/
  池满逐出优先级（stale-gap 腿 + LRU 兜底）/二遍复评（含 V2 全漏修复推演与活视图）/
  ②c 状态机（含救援候选永不开线索、fail 仅施 episode 信封）/seam_indexes 判据与坐标/
  守恒三公式/rescue 翻转留痕/池空语义（episode 调用 + 救援跳过）/votes 严格多数聚合/
  off 字节等价锚。
- **tests/integration/test_stitch_llm.py**（新）：真实 glm-5.2 判例（明确续接/明确新任务/
  模糊拒缝/候选位置扰动不改判 [N-8]）。
- **真机复验协议**：对 capture-runs 的 scenarios-v2（G 场景）与 app-pool-v2（旅游）重跑；
  指标 = 线索级 F1 × FPS_norm 乘法合成（E1 [T-1]）+ 1-1 overlap（E9 [N-3]）；验收线：
  跨 App 协作/旅游规划以单线索输出，且错缝 FPS = 0。
- 离线全量回归：stitch off 时既有套件字节等价（回归锚）。

### 3.7 文件修改清单（逐文件·逐章节定稿，2026-07-16）

> 基准：包分层重构 **v1.1 严格物理归档**布局（分支 codex/labelkit-layer-reorganization，
> 0cb11cc + 7c325dd；canonical-only，无平铺 shim；tests 深层镜像）。三项布局裁决：
> ① `labelkit/operators/stitch.py` 生而 canonical-only，**不建平铺 shim**（v1.1 全域禁
> shim，且新模块无历史消费者）；连带义务：两个新测试文件登记进重构规范 §6.1 测试白名单、
> CONTRACTS §1.2，**及 `tests/cli/test_cli.py` 的机器冻结集合 `EXPECTED_PRODUCTION_PY`/
> `EXPECTED_TEST_PY`（+3 条目，manual 线审计判定不改必红——第三处白名单镜像）**。
> ② spec §1.5 的 T-/N- 引用并入正式 spec 时映射为**全局顺延编号
> [64]+**（v1.8 先例 [41]–[63]），先与既有 63 条查重（PoLL=[32]、Self-Consistency=[33]、
> OS-Genesis=[41]、AndroidControl=[45]、GUI-Odyssey=[46] 等复用；含定稿复核增补
> N-23…N-28，实际净新增 27 条：[64]–[90]，总 90 条——同承重点合并遵循 v1.8
> [47]/[53]/[60] 分组惯例）。③ CONTRACTS 三条算子间懒加载例外
> **零增补**——seam 交接是 `seam_indexes` duck 标数据承载（与 session_split 同型），
> 非函数直调。

#### spec/（20 改 + 1 新 + 6 零改动）

| 文件 | 动作 | 改动点 |
|---|---|---|
| `316-m16-stitch.md` | 新 | M16 全章（§3.16，统一模板七小节）：单调选池一遍 + 有界二遍复评 + 接缝标定 + 失败语义 + `stitch_schema()` + 退化锚 |
| `00-frontmatter.md` | 改 | 版本/日期；版本修订表尾加 v1.9 行 |
| `10-ch1-overview.md` | 改 | §1.2 术语增线索/碎片/接缝，**且「stream 模式」术语行内嵌链序串插 stitch**（文档审计遗漏 E）；§1.4 需求映射加 v1.9 行；§1.5 背书表加 v1.9 行组（[64]+）；§1.6 加 2026-07-15/16 决策段（T1–T22） |
| `20-ch2-overall-design.md` | 改 | §2.1.1 功能表；§2.2 清单句+图 2-1；§2.2.1 表加 M16；§2.3 图 2-2 链序插 stitch；§2.3.1 加三约束；§2.3.2 补 stitched 第四路由+影响表；§2.4 dry-run 加 stitch_calls、--strict 加 T21 补注；§2.5 补 [stitch]；§2.6 确定性条件化声明 |
| `301-m1-config.md` | 改 | §3.1.4 表新增 v1.9 行（stitch⇒segment / votes 偶数 / 数值界 / 引用集 +stitch.llm **不入 vision 集** / [class.*.stitch] 不存在）；no-op 名单 +[stitch] + stitch∧rules WARN |
| `303-m3-dedup.md` | 改 | §3.3.3 S10 段补：判重单元 = 线索（重绑成员原配方拼接），壳被 active 过滤天然排除——代码零改动（T13） |
| `304-m4-qualityqurating.md` | 改 | §3.4.3 序列打分：步行渲染 +thread_seam 后缀；打分单元 episode→thread |
| `305-m5-annotate.md` | 改 | §3.5.2 S28 降采样升级为按碎片配额（每碎片保底 1 帧）公式全文 |
| `307-m7-verify.md` | 改 | §3.7.2 缺陷词表 5→6（+wrong_stitch）+ 六段→七段（插 [片段结构]）；§3.7.3 wrong_stitch 独立 mark-only+fail 分支 + `_session_episodes` 滤壳 |
| `308-m8-schema-engine.md` | 改 | §3.8.1 +stitch_schema() + defect kinds 5→6；§3.8.2/§3.8.3「不经过 L2.5」括注补缝合判定 |
| `310-m10-orchestrator.md` | 改 | §3.10.3 表：链序九名元组（**同步改两处：时序流行元组本体 + 「分类与扇出（v1.7）」行「八名单一超集元组」括注→九名**，文档审计）；新增 v1.9 行——counts.stitched/threads（=episodes−stitched 导出式）、failed 兜底与残差公式各 −stitched、batch.end +stitched/threads、dry-run +stitch_calls（估算式 T16）、report.stream.stitch |
| `311-m11-emitter.md` | 改 | §3.11.2 三路由→**四路由**（stitched 仅计数）；rejects +(stitch, stitch_invalid) + rescued 不入 rejects + --strict 补注；report +counts/stream 增量 |
| `313-m13-classify.md` | 改 | §3.13.4 multi 扇出复制清单 +thread_id、seam_indexes |
| `314-m14-segment.md` | 改 | §3.14.4 ③ min_len：below_min_len duck 标 = 救援判别载体、②c 翻转指针、发生计数不回退；§3.14.6 --strict 交互句 |
| `315-m15-extract.md` | 改 | §3.15.2 seam 序数机械占位、幂等门与 len 不变量不动；§3.15.4 T10 占位四键分支 + 相邻救援不占位 |
| `40-ch4-data-structures.md` | 改 | §4.1 Status +stitched、PipelineItem +thread_id（seam_indexes duck 注）、Record S24 注补 id 不重算/episode_id==thread_id（T22）；§4.2 Transition.detail +thread_seam、defects 注 5→6；§4.3 ②b 后插 **②c 全文** |
| `50-ch5-config-spec.md` | 改 | §5.2 segment 与 dedup 间插 [stitch] 全表（11 键，votes 语义按 T18/M-4）；no-op 名单、按类白名单注、trace.channels 10→11 |
| `60-ch6-io-formats.md` | 改 | §6.3 `_meta.stream` +thread_id/fragments/resumed + **order_span 包络规范句**；§6.4 counts +stitched/threads、stream +stitch 子块、守恒全式与残差公式扩项、rejects 组合 + strict 注 |
| `70-ch7-logging.md` | 改 | §7.2 通道 10→**11**、batch.end 补字段、+stitch.judge/stitch.thread 两行；§7.4 +task_name 脱敏段；§7.6 +stitch_invalid 行 |
| `80-ch8-nongoals-roadmap.md` | 改 | §8.1 补 v1.9 六条非目标（含完成感知封闭，B-1）；§8.3 增 O8（stitch.judges 多模型评审扩展，T18 选型记录）；§8.4 演进表 +M16 |
| `85-ch9-references.md` | 改 | 尾部顺延 [64]+ 补 v1.9 文献（先查重，含 N-23…N-28 定稿增补，净新增约 20–25 条） |
| `302/30-ch3/306/309/312/90` | 零改动 | 302：stitch 不触 M2（草案笔误）；30：无模块清单；306/309：互斥/无新传输；312：通道白名单不在此；90：无新 rubric |

#### docs/CONTRACTS.md（1 文件，约 21 节触点；文档审计补 4 处遗漏 + 3 子项）

| 节 | 动作 | 改动点 |
|---|---|---|
| 文档头 ground-rules 段 | 改 | **「M1–M15 + CLI」→ M1–M16；「v1.5/v1.6/v1.7/v1.8 revisions」+ v1.9**（文档审计遗漏 1） |
| §1 包布局 | 改 | operators 树 segment 与 dedup 间插 `stitch.py # M16` |
| §1.2 测试归属 | 改 | +tests/operators/test_stitch.py、tests/integration/test_stitch_llm.py |
| §2 架构 recap | 改 | 算子 +M16、链序九名（主链插 stitch；退化链「with both disabled」句改三者）、Statuses 7→8 +stitched；懒加载例外句零改动（裁决③） |
| §3/§4/§5 verbatim | 改 | types（Status/PipelineItem/Transition/S24 注补 M16、**`VerificationResult.defects`「five-value」注 5→6**——文档审计子项）、errors（+STITCH_INVALID）、stage（+②c 全文含幸存者句） |
| §6.1/§6.3 | 改 | +StitchConfig 全字段（插 SegmentConfig/ExtractConfig 间）、ResolvedConfig+stitch、Trace 注 10→11、**`AnnotateConfig.sequence_frames` 注释内嵌均匀降采样公式改按碎片配额**（文档审计子项）；校验规则 +37–41 五条 + Warnings 两条 |
| §7 模块 API | 改 | **新增 §7.16 M16**；§7.3 接缝后缀（并列句）、§7.4 配额公式+穿参义务、§7.6 kinds 六值+滤壳+wrong_stitch 独立分支、§7.7 +stitch_schema、§7.9 链序九名冻结/计量（threads 导出式）/两公式 −stitched/batch.end/dry-run、§7.10 第四路由、§7.11 +task_name **+ EV_STITCH_JUDGE/EV_STITCH_THREAD 常量清单 + 通道 10→11 镜像句**（文档审计子项）、§7.13 +两 duck 标、§7.14 载体注（连续 run 语义）、§7.15 seam 占位+计数器口径 |
| **§7.12 CLI/factory/probe 接线** | 改 | **StitchStage 构造入 build_stages 链位句 + `referenced_profiles()` +stitch.llm 条件入 probe 集**（文档审计遗漏 3——代码表已列 factory/profile_usage，此为对应文档节） |
| §8.1/§8.3 | 改 | batch.end 字段、两事件行、通道 10→11、task_name 分级 |
| §9.1/§9.2/§9.3 | 改 | `_meta.stream` 增量+包络句；rejects 组合+stitched 永不入+strict 注；counts/stream.stitch/守恒残差/计数属主表 |
| **§10.2 M4 pairwise 序列变体（冻结文本）** | 改 | **quality 步行渲染 thread_seam 后缀落点（§10.3 pointwise 共用该文本块）**——spec 侧 304 已认领而 CONTRACTS 表原漏（文档审计遗漏 2） |
| §10.5/§10.7/§10.11 | 改/新 | §10.5 序列变体六段→七段（插 [片段结构]）+ system 缺陷清单 +wrong_stitch；§10.7 kinds+1 + **stitch_schema 逐字 JSON**；**新增 §10.11 缝合判定模板**（摘要卡 + 最近活跃降序 + 保守偏置，逐字冻结） |
| **§11 cross-cutting 条 2** | 改 | **契约例外枚举「(v1.7 ②a…; v1.8 ②b…)」+ ②c**（文档审计遗漏 4） |
| §12 决策注册表 | 改 | 新增第 29 条（v1.9 stitch → SPEC-activity-structure.md T1–T22） |

#### docs/manual/（18 改 + 1 新章 + 10 零改动；manual 线审计证伪 3 处零改动并补列 03/06/14）

| 文件 | 动作 | 改动点 |
|---|---|---|
| `26-thread.md` | **新** | Part IV 新章，对齐 25 章形制：26.1 为什么缝合 → 26.2 快速上手 examples/thread（V1–V4 真跑+验收断言）→ 26.3 机制 → 26.4 输出怎么读（fragments/接缝步/report.stream.stitch/守恒对账/order_span 包络）→ 26.5 调优审计（max_open/bias/votes、stitch.judge 抽读、负样本协议、错缝 FPS=0 验收线）→ 26.6 常见问题（strict 1→0、相邻救援、不跨会话、T4 标注层模式） |
| `01` | 改 | §1.4 算子总览插 stitch 行；「中间八个」→九个；**§1.1 链条图「（可选）分段 ──▶ 去重」间插「（可选）缝合」**（审计遗漏 10） |
| `03-quickstart` | 改（**原零改动被证伪**） | 行 138 dry-run 逐字样例 +`stitch_calls=0`（**须真跑重采**，与 15 章同源）；行 142 散文逐字段解说补 v1.9 句——原零改动理由「报告样例字节不变」只覆盖 report，不覆盖 dry-run 行 |
| `04-concepts` | 改 | §4.2 状态机 +stitched、契约 +②c、守恒式扩项；§4.5 九算子 + 约束表两行；**约束表第 9 行「quality.llm 免除 supports_vision（唯一放宽项）」措辞修正**（stitch.llm 不入 vision 集后「唯一」失真，审计遗漏 6） |
| `06-config-toml` | 改（**原零改动被证伪**） | 行 105 `supports_vision` 行「三处例外…唯一的放宽项」→ 四处例外（+stitch.llm 恒不要求）、删「唯一」——本章是 vision 校验规范参考页 |
| `07-project-toml` | 改 | §7.4 速览表插 [stitch] 行；「三节一族」→四节；**行 5 章首引语「八个算子节」→九个**（审计遗漏 11） |
| `08-outputs` | 改 | §8.2 stream 键注；§8.3 rejects +(stitch, stitch_invalid)+rescued 说明；§8.4 +v1.9 段（counts/stream.stitch/守恒/strict） |
| `09-dedup` | 改 | §9.4 线索判重一句 + 壳不参检 |
| `10-quality` | 改 | §10.5 +thread_seam 后缀 + 打分单元声明 |
| `11-annotate` | 改 | §11.2「按确定性等距降采样选帧」→按碎片配额 |
| `13-verify` | 改 | §13.5 缺陷 5→6 类；**证据段改为「新增 [片段结构] 段」措辞**（13 章从未枚举"六段"，无字面锚——审计遗漏 14）；手术段 wrong_stitch=只标记不拆线 |
| `14-schema-engine` | 改（**原零改动被证伪**） | §14.7 行 149 内部 Schema **穷举**清单：+缝合判定对象、「stream 一族的三个」→四个、缺陷表「五值枚举锁死」→六值——原零改动理由「内部 Schema 泛指」不实 |
| `15-cli` | 改 | §15.1 dry-run 逐字样例 +stitch_calls=0（**须真跑重采**）；--strict 补注 |
| `16-observability` | 改 | §16.2 +两事件行；通道十→十一；分级 +task_name |
| `17-tuning` | 改 | §17.1 调用账表插 stitch 行 + votes 成本注；§17.5 决策表 +max_open/bias/stale_gap/votes |
| `18-troubleshooting` | 改 | §18.1 +stitch_invalid；§18.2 +「错缝」「漏缝」两症状条；--strict 条补 stitch 交互 |
| `25-stream` | 改 | §25.1 交叉任务指引、§25.3 第四层补工位 **+ 行 138 缺陷表「五值」→六值**、§25.5 补 26 章指针 **+「估算行无条件打印 segment_calls/extract_calls」句补 stitch_calls**、§25.6 加一问 **+「唯独 quality 纯文本」措辞修正**（stitch 判定同为纯文本）；**样例零重采**（stitch off 字节等价锚，25.5「不跨会话缝合」句 v1.9 下仍真） |
| `appendix-a-cheatsheet` | 改 | §A.8 +约束 20/21 + 名单补 [stitch] **+ 第 18 条「唯一放宽」措辞修正**；**§A.7 行 141「十通道」→十一**（审计遗漏 7）；新增 §A.11 [stitch] 11 键速查 |
| `README`（manual） | 改 | Part IV 目录 +26 章；「怎么读」+v1.9 路径 |
| 02/05/12/19/20/21/22/23/24/appendix-b | 零改动 | 08 报告样例字节不变（新键仅启用时出现）；24 duck 标用户不可见、行 15 链序括号本为子集式呈现；12 generate×stream 互斥经 stitch⇒segment 传递闭合。（既有陈旧缺陷顺带记录不属 v1.9：07 行 117 trace 通道枚举缺 v1.7/v1.8 三通道） |

#### 代码（18 改 + 1 新，canonical 路径；可行性审计补 `_compose_chain` 点名、两处陈旧枚举债与 segment.py 零改动声明）

| 文件 | 动作 | 改动点 |
|---|---|---|
| `labelkit/operators/stitch.py` | 新 | M16 全部实现（含 app/activity/title 提取循环自带副本，T9 裁决；`StitchStage.name = "stitch"` 与 `_CHAIN_ORDER` 一致） |
| `labelkit/common/contracts/types.py` | 改 | Status +stitched；PipelineItem +thread_id；detail 注 +thread_seam |
| `labelkit/common/contracts/stage.py` | 改 | docstring ②b 后插 ②c |
| `labelkit/common/errors.py` | 改 | +STITCH_INVALID |
| `labelkit/common/config/model.py` | 改 | +StitchConfig（链序位插 Segment 与 Extract 间）；ResolvedConfig+stitch；Trace 注 10→11 |
| `labelkit/common/config/loader.py` | 改 | `_TRACE_CHANNELS` +stitch；[stitch] 解析 + 三约束；引用集两处 +stitch.llm（不入 vision 集）；no-op 警告归属分支按 T17（stitch 关 ∧ segment 开的组合仿 sequence_frames 单独警告） |
| `labelkit/common/runtime/schema_engine.py` | 改 | +stitch_schema()；defect kinds +wrong_stitch（四处同步①） |
| `labelkit/common/observability/obslog.py` | 改 | `_FREE_TEXT_KEYS` +task_name；事件名常量 +EV_STITCH_JUDGE/EV_STITCH_THREAD（T16 实现定稿①） |
| `labelkit/orchestration/orchestrator.py` | 改 | `_CHAIN_ORDER` 插 stitch **且 `_compose_chain` enabled 映射 +`"stitch": cfg.stitch.enabled`**（可行性审计：只插元组必要不充分）；模块 docstring 链序句；`_DEFECT_KINDS`+1（②）；计量/batch.end/failed 兜底与残差 −stitched/counts+threads（=episodes−stitched 导出）/report.stream+stitch/dry-run+stitch_calls（`_estimate()` 按 T16 估算式） |
| `labelkit/orchestration/factory.py` | 改 | build_stages segment 与 dedup 间插 stitch 实例化 |
| `labelkit/orchestration/profile_usage.py` | 改 | referenced_profiles +stitch.llm |
| `labelkit/operators/emitter.py` | 改 | 第四路由；`_stream_block` +thread_id/fragments/resumed+包络 |
| `labelkit/operators/classify.py` | 改 | `_fan_out` 复制清单 +thread_id 真字段与三 duck 标（seam_indexes/seam_interrupted_by/stitch_fragments，§3.2 实现定稿） |
| `labelkit/operators/verify.py` | 改 | DEFECT_KINDS+1（③）；`_session_episodes` 滤壳；七段 prompt；`_route_defects` 独立分支；步行接缝后缀 |
| `labelkit/operators/quality.py` | 改 | 步行渲染 +thread_seam 后缀 |
| `labelkit/operators/annotate.py` | 改 | `_keyframe_indexes` 按碎片配额；`build_annotate_prompt`/`annotate_record` 增第三 additive trailing kwarg，**主调用与 verify 修复重标调用两处穿参**（T14 穿参义务） |
| `labelkit/operators/extract.py` | 改 | seam 序数占位跳过（幂等门/len 不变量不动）；**平铺 gather 记账重构**（spans/切片假设每 pair 一协程——实质改动，T20）；seam 占位**不进** `_register` 计数器（extract.transitions/by_type 排除，T20 口径） |
| `labelkit/__init__.py` | 改 | 模块 docstring 链序句更新（v1.6 形态陈旧债，v1.9「凡枚举算子处皆同步」原则一并清偿） |
| `labelkit/cli/parser.py` | 改 | argparse 描述算子枚举句更新（v1.7 形态陈旧债，同上清偿；原「cli/ 零改动」声明相应收窄） |
| 零改动 | — | **operators/segment.py**（T11 连续 run 语义裁决后无需段身份 duck 标——审计两表缺席问题闭合）、operators/{dedup,ingest,generate}、common/extensions/hooks、common/runtime/llm_client、`labelkit/cli/{main,commands}.py`（referenced_profiles 职责已归 profile_usage）、`labelkit/data/rubrics/` |

#### tests/（23 改 + 2 新，v1.1 深层镜像布局；manual 线审计修正：test_errors 零改动被证伪、test_cli 冻结集合为第三处白名单镜像、test_stage 条件项实测落空归零）

| 文件 | 动作 | 改动点 |
|---|---|---|
| `tests/operators/test_stitch.py` | 新 | §3.6 全清单 |
| `tests/integration/test_stitch_llm.py` | 新 | 真实 glm-5.2 四判例 |
| ResolvedConfig 构造补 stitch 字段（19 文件，grep 精确核实） | 改 | operators 9（ingest/segment/classify/extract/quality/generate/annotate/verify/emitter）+ integration 7（annotate/classify/generate/quality/verify/stream/llm_client 的 *_llm）+ cli/test_cli、common/observability/test_obslog、orchestration/test_orchestrator（ResolvedConfig 无字段默认值，新增 stitch 字段必击穿全部 19 处） |
| `tests/cli/test_cli.py`（改动点追加） | 改 | 除 ResolvedConfig 外：**`EXPECTED_PRODUCTION_PY` +`labelkit/operators/stitch.py`、`EXPECTED_TEST_PY` +两个新测试文件**（`test_package_layout_matches_frozen_spec` 机器冻结集合，不改离线套件必红）；`test_package_layout_dependency_direction` 的 verify 懒加载白名单零改动（裁决③自洽） |
| `tests/common/test_errors.py` | 改（**原零改动被证伪**） | ErrorKind 穷举 wire-code 断言（14 项字典全等）+`STITCH_INVALID` |
| `tests/common/contracts/test_types.py` | 改 | Status 7→8 值断言 + thread_id 用例 |
| `tests/common/config/test_config.py` | 改 | [stitch] 解析/缺省/三约束/channels/引用集用例 |
| `tests/common/runtime/test_schema_engine.py` | 改 | stitch_schema + kinds 六值 |
| 六个实质用例文件 | 改 | emitter 第四路由、verify 七段+wrong_stitch+滤壳、extract seam 占位、classify duck 复制、annotate 配额、quality 接缝后缀 |
| `tests/orchestration/test_orchestrator.py` | 改 | counts_invariant +stitched；链序/计量/report/batch.end/dry-run 用例 |
| 零改动 | — | test_dedup、runtime/test_llm_client、extensions/test_hooks、**contracts/test_stage**（实测只断言协议结构不含例外清单文本——条件项落空）、integration/test_key_pool_llm、test_schema_engine_llm、conftest、hook_samples |

#### examples/ 与根文件

| 文件 | 动作 | 改动点 |
|---|---|---|
| `examples/thread/` | 新 | project.toml（`[stream] key="source_dir"` 按场景子目录分四会话（§3.6 裁决，index 全树错开编号）+ [segment] + [stitch]（rescue_short/repass 开）+ [extract]）+ data/（V1–V4 fixture（V4 按 §1.1 规范布局）+ **负样本纯噪声会话**）+ tools/gen_fixtures.py（对照 examples/stream 结构：确定性 PIL 绘制，无 README） |
| `examples/config.toml`、`examples/stream/` | 零改动 | 既有 default/judge 两 profile 满足 stitch.llm="default" 引用（纯文本判定）；stream 例保持 stitch off = 字节等价回归锚 |
| `AGENTS.md`/`CLAUDE.md`（逐字同步，manual 线审计补 4 处） | 改 | spec 清单 +316 与 +v1.9 字样；示例命令 +examples/thread **及示例段散文（exercises 枚举句 + fixtures 句）**；链序/算子清单/模块 map（M16 → operators/stitch.py）/Status/②c/约束句 **及能力枚举句 +optional thread stitching (v1.9, default off) 与 stream-mode 描述句补缝合子句**；**开篇句「current spec revision is v1.8…」→ v1.9**；**「§3 per-module design (M1–M15…)」→ M1–M16**；**编号沿革句 +「v1.9 appends M16」**；+v1.9 revisions 段；手册章数 →29 |
| `README.md`（根，manual 线审计补 4 处） | 改 | 算子数九 + v1.9 bullet；手册 29 章；**行 3 开篇能力枚举 +线索缝合**；**行 5 ASCII 链条图「（可选）时序分段 ──▶ 去重」间插「（可选）线索缝合」**；**行 38「五个示例工程」→六个 + thread 一句**；**行 45 文档表「v1.8 修订」→ +v1.9** |
| `docs/dev/SPEC-package-layer-reorganization.md` §6.1 | 改 | 测试白名单登记两个新测试文件（v1.1 白名单封闭）；登记处加一句「§10 计数为 2026-07-16 时点历史记录（30/31），v1.9 后实为 31/33，不回改」 |

**计数摘要（五路复核修正后）**：spec/ 20 改 + 1 新 + 6 零；CONTRACTS 1 文件约 21 节
（含新 §7.16/§10.11/§12-29 与文档头）；manual 18 改 + 1 新章 + 10 零；代码 18 改 + 1 新；
tests 23 改 + 2 新；examples 1 新；根文件 3 改 + 重构规范白名单 1 改。
**需真跑重采样例**：26-thread 全章（examples/thread 四场景 + 负样本）、15-cli 与
03-quickstart 的 dry-run 逐字样例（stitch_calls=0 行无条件新增，两章同源）；08/25 章
预期字节不变（核对即可）。

## 4. 非目标（v1.9 明确不做）

1. 真并发（同屏双任务/分屏）——T2。
2. 跨会话/跨运行缝合——T12；无持久化红线不变。
3. 帧级区间树/子任务嵌套校验——T4（需求方 2026-07-16 确认）。
4. 自动拆线手术——verify 只标 `wrong_stitch` 不重构（T15）。
5. 在线/增量处理——批处理工具定位不变。
6. **完成感知封闭**——B-1 裁决撤除：链序上 extract 后置，stitch 运行时无动作证据，
   "机械收尾动作模式"不可判；若未来链序演进（v1.8 S32 候选）使动作先行，可重评
   （§5.4 演进注记）。

## 5. 风险与演进

1. **缝合质量无域内基线（§5.1）**：interleaved 解缠是 RPM 公开难题 [T-4]，三家工业
   产品空白 [N-18]；2026 大规模 GUI 轨迹挖掘管线亦显式回避杂乱自然流（refute 复核，
   动机旁证）。护栏：保守合取（T9）+ 二遍复评（T19）+ 负样本协议（E2）+ 真机实测
   门禁（§3.6，错缝 FPS=0 为验收线）。充分性只能实测——文献未验证过该合取的错缝率
   下界。
2. **上游依赖（§5.2）**：分段边界漂移传导至缝合输入；摘要级证据缓解（T8），votes
   （T18，已立项默认关）为正规对冲。摘要卡由上游 LLM 生成，**summarization drift**
   （[N-24] 综述术语：每次压缩静默丢弃低频细节）会传播——trace 全量留判定证据供审计。
3. **证据类比边界（§5.3）**：ER/对话解缠结论系跨域类比（记录簇/聊天线程 ≠ GUI
   episode，[N-23] 已收窄为同形制先例）；HCI 数据多为桌面域（[N-27] 补移动侧间接
   佐证：手机穿插深度更浅，锚为宽松上界）；PIRA 为 0 被引新基准；[N-23] 附带
   DD-GEPA 风险注记（~30B 开源模型性能骤降）——glm-5.2 实测由 §3.6 门禁兜底。
   T11 连续 run 重组与 T19 池截取的残差（§3.1/T19 声明）同此兜底。
4. **演进注记（§5.4）**：①若第二模型家族进场，`stitch.judges` 镜像 `verify.judges`
   作纯配置扩展（T18；正式 spec §8.3 记 O8）；②完成感知封闭随链序演进重评（§4-6）；
   ③开放问题已清零：T1–T22 全部裁决，正式修订前置已清零——§3.7 已按重构后 v1.1
   布局定稿，重构分支合入 main 后即可按清单动 spec/*.md 与 CONTRACTS.md。

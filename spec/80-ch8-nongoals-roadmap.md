# 8. 非目标、设计假设与演进路线

## 8.1 非目标

见 2.1.2 工具级负边界；另注：不承诺跨批分数可比性（pairwise 为批内相对分，3.4.3）；不做多机分布式（单机并发已被 API 限速主导）。

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
| O3 | UI 模态生成（以现有截图为底、仅生成指令/任务侧文本，AgentTrek 式轨迹合成 [15]） | 本版生成仅文本模态；有明确需求后单独立项。 |
| O4 | 断点续跑 | 与「不存储中间态」冲突，明确排除；超大任务靠分目录运行缓解。 |
| O5 | `labelkit analyze` 子命令：读 trace.jsonl 产出标注质量分析 / rubric 诊断报告（自动计算 7.5 诊断指标、reason 关键词聚类） | 本版仅提供 jq 级手工分析（7.5）；trace 事件契约（7.2）稳定运行一个版本后立项。 |
| O6 | 全局精确定量与生成补齐回路（`output.target_count`，输出恰好 N 条） | 设计草案：两阶段流水线——第一阶段全量接入 + 去重 + 打分并缓存分数；第二阶段全局 top-K 选出恰好 target_count 条，再仅对选中集执行标注与输出。前提：分数具全局可比性——pointwise 绝对刻度，或 pairwise 经 O2 锚点法校准后方可全局排序。配生成补齐回路：target 未达时从高分种子生成 → 去重 → pointwise 质量门 → 计入，停止条件 = 达标 ∨ max_backfill_rounds ∨ 本轮合格率 < 下限（生成器饱和时合格率持续衰减，即递归自生成数据的 model collapse 现象 [36]，故合格率下限停止条件必不可少）。现状：2026-07-02 评审对齐为演进路线——本版以 `quality.selection = "top_ratio"` 提供流式批内近似定量（3.4.3），不承诺全局恰好 N 条。触发条件：出现「必须恰好 N 条」的下游需求。 |

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

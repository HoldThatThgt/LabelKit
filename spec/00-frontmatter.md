LabelKit 采集数据自动标注工具 — 产品设计说明书 v1.8

# LabelKit 采集数据自动标注工具

| 文档版本 | v1.8 |
|---|---|
| 日期 | 2026-07-13 |
| 状态 | 评审修订稿 |
| 目标读者 | 开发工程师、算法工程师、测试工程师 |
| 文档定位 | 实现级设计规格 —— 开发者依据本文档即可完成实现，无需自行补全任何设计决策 |

| 版本 | 日期 | 修订内容 |
|---|---|---|
| v1.0 | 2026-07-02 | 初版（评审稿） |
| v1.1 | 2026-07-02 | 按评审意见修订：① 各模块新增报文级输入/输出示例（3.1.6–3.11.3）并补全打分归一化公式等细节；② 新增日志系统设计——M12 日志模块（3.12）、第 7 章重写为「日志系统与可观测性」、trace 追踪日志与 rubric 优化闭环（7.5）、`[trace]` 配置（5.2） |
| v1.2 | 2026-07-02 | 按第二轮评审意见修订：① 新增「算子对输出集的影响分析」（2.3.2）；② 质量打分新增批内定量优选 `quality.selection = "top_ratio"`（3.4.3），全局精确定量与生成补齐回路列入演进路线（8.3 O6）；③ 生成算子支持多 LLM 混合与风格模板（3.6.2）；④ 算子级算法增强：多评审团投票、双顺序裁决、self-consistency 标注、SemDeDup 语义去重可选级（8.4 演进路线总表） |
| v1.3 | 2026-07-02 | 按第三轮评审意见修订：标注与生成拆分为两个独立模块——生成独立为 M6 generate（3.6，配置键 / Stage 类 / API 零变更），原 M6–M11 按流水线位置全量重编号为 M7–M12（全部交叉引用、图 2-1/2-2、目录同步更新） |
| v1.4 | 2026-07-02 | 新增纯生成模式（`run.mode = "generate_only"`）：无输入数据从零合成——配置种子池（Self-Instruct 形态）或无种子条件化（Persona Hub / Cosmopedia 形态）两种形态，单遍执行，产出照常走治理/标注管线（3.6.2、3.10.3、2.3.1 ④）；合成样本统一标记修正为 `generator ≠ null` |
| v1.5 | 2026-07-03 | 按 E2E 加固与校验回调评审修订（散注全文，标「v1.5」）：① 用户校验回调两枚——结构引擎 L2.5 `output.validator` 与生成样本过滤 `generate.sample_validator`（3.8.2、3.6.2；错误类 `callback_violation`，7.6）；② 认证类 401/403 首错立即熔断（3.9.3、7.6）；③ dry-run 产物隔离（2.4）、trace 文件惰性打开（7.1）、`per_criterion_tie_rate` / `l1_lossy` 等观测增强（6.4、7.2） |
| v1.6 | 2026-07-03 | 多 API Key 负载均衡与熔断交付（对齐决策见 1.6）：① **密钥池**——profile 可声明多把 API Key（`api_key_envs`，5.1）：逐尝试最少在途选择、429 按密钥冷却即时轮换、认证失败按密钥禁用（最后一把存活密钥被禁才立即熔断）、全池冷却有界驻留（`run.max_park_s`，5.2）；观测新增三事件与报表 keys 子块（7.2、6.4），单密钥配置在数据产出与熔断/退出语义上与 v1.5 一致（429 等待路径修订见 3.9.3 重试行）；② **熔断交付**——熔断中止改为原子交付已完成批（report 标 `partial_delivery`、counts 增 `unprocessed`，3.10.3、3.11.2、6.4） |
| v1.7 | 2026-07-07 | 分类算子与按类条件化（对齐决策见 1.6）：① **分类算子**——新增 M13 classify（3.13，链序 dedup 之后、quality 之前）：LLM 封闭集分类（内部 Schema enum 词表硬校验，无 uniqueItems——重复标签由代码侧确定性归一化）、单/多标签可配（`classify.assignment`）、可选 self-consistency 投票、失败归兜底类（`classify.fallback_class`）；multi 模式按标签向批尾扇出兄弟信封（Stage 契约增 ②a 例外，4.3；`counts.fanout` 与不变量扩展，6.4）；② **按类条件化**——`[class.<name>.*]` 白名单覆盖（5.2）：quality 批内按类分池（3.4.3）、annotate/verify 按类指令与评审维度（3.5.2、3.7.2）、generate 按类种子池与生成指令（3.6.2）；③ 观测面——`_meta` 增恒在键 `classification`（6.3）、report 增 classify 节与 quality.by_class（6.4）、trace 通道增 classify 与新事件 `classify.decision`（7.2）、错误码增 `classification_invalid`（7.6）。默认关（`classify.enabled = false`），关闭时数据产出与 v1.6 逐字段一致（`_meta.classification: null` 除外） |
| v1.8 | 2026-07-13 | 时序流语义分割与动作摘取（对齐决策见 1.6）：① **时间轴与会话化**——`[stream]` 输入侧声明（5.2）：排序键与按分区键单调性校验、gap/key/上限会话规则（M2 会话装配器，3.2.8），切批改整会话装箱（3.10.3）；② **新算子**——M14 segment 语义分段（3.14，滑窗 LLM 边界裁决 + 噪声剔除，把成员帧收拢为序列记录 episode；Stage 契约增 ②b 例外，4.3）与 M15 extract 转移/动作摘取（3.15，相邻帧对 → 结构化动作，写入 `item.transitions`）；③ **序列级下游适配**——M3/M13/M4/M5/M7 收序列记录（3.3.3、3.13.3、3.4.3、3.5.2、3.7.2/3.7.3），内置轨迹 rubric `default:trajectory`（附录 A.3）；④ 观测面——`Status` 增 `absorbed`/`dropped_noise` 两值（4.1）、`_meta` 增恒在键 `stream`（6.3）、report 增 stream 节与守恒式扩展（6.4）、trace 通道增 segment/extract 与四个新事件（7.2）、错误码增 `segmentation_invalid`/`extraction_invalid`（7.6）。默认关（`segment.enabled = false`），关闭时数据产出与 v1.7 逐字段一致（`_meta.stream: null` 除外） |

产品设计说明书（Product Design Specification）

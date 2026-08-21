# 提案：按类档位表——每个序列类独立配置帧类构成档位

> **状态：superseded（v1.17 Wave 8 已收口）。** 本文仅保留 v1.15 的方案论证与决策溯源；当前行为以 v1.17 主规格为准。

> **状态：已 SPEC 化（2026-08-19）。**最终开发规格见 `docs/dev/SPEC-per-class-tiers.md`——两路预实现审计（代码可行性/亲和性、文件修改清单穷尽）已于同日完成并折入其 §2 裁决表与 §4 清单（最重两条：噪音槽位在场谓词与计数器键冻结面，均由裁决·全局表为锚与裁决·计数器键按类重冻结收编），**凡与本文不一致处以 SPEC 为准**。本文保留为方案论证与决策溯源材料。
> 问题验证：v1.14 的档位表 `[[generate.stream.tiers]]` 是**全局唯一**的——所有参与序列类共享同一套档位定义（tier_rank / weight / frame_classes），每类配额按同一套权重配分；`[class.*.generate]` 白名单（六键）无档位面。意图不同则帧类语汇与构成天然不同（购票有确认帧、闲聊没有），全局表无法表达「每意图各一套构成/权重」。

---

## 1. 结论先行

**推荐「方案·按类档位表」**：`[[class.<name>.generate.tiers]]` 按类数组表（行结构同全局表三键），**表级原子覆盖**——类声明了就用类的整张表，未声明回落全局 `[[generate.stream.tiers]]`；全局表续存为回落锚与档位面总开关（按类表要求全局表在场）。`tier_rank` 身份从「全局档位」收窄为「**类内档位**」（每张生效表各自连续覆盖 1..N，跨类同 rank 无工具语义）；配分函数 `apportion_tiers(sequences, tiers)` 与序数映射 `tier_rank_for_ordinal` **签名零改动**（v1.14 已把档位表做成参数——调用点改传该类生效表即可）；报表 `tiers` 子块在按类面开启时改**类嵌套形**。零 rng、零调用数变化，按类表全部缺省时与 v1.14 逐字节等价。

## 2. 业界方案调研（2026-08-19 检索核实）

| 名称 | 年份 | 做法 | 可迁移实践 |
|---|---|---|---|
| [Gretel Data Designer](https://docs.gretel.ai/create-synthetic-data/gretel-data-designer/define-your-data-columns/column-types) / [NVIDIA NeMo Data Designer](https://docs.nvidia.com/nemo/microservices/25.12.0/design-synthetic-data-from-scratch-or-seeds/define-your-data-columns/column-types/sampling-based-columns.html) | 2024–25 | **subcategory 采样器**：子类别取值集合按父类别值逐一声明（`values = {"Electronics": [...], "Clothing": [...]}`）；**conditional_params** 按其它列取值整套替换采样参数 | 「父类别（意图）→ 各自子方案（档位表）」的最直接工业同构；conditional_params 的整套替换语义 = 表级原子覆盖 |
| [GLAN](https://arxiv.org/abs/2402.13064)（Microsoft） | 2024 | 知识分类树逐节点派生：每 discipline 生成自己的 subjects、每 subject 定制自己的 syllabus，配额与结构逐节点独立 | 分类树内层结构**不全局共享**——每个外层节点携带自己的内层分解方案 |
| [Schema-Guided Dialogue](https://arxiv.org/abs/1909.05855)（spec §1.5 已引） | 2020 | 每 service 一份独立 schema（intents × slots）驱动 outline | 「每意图一份构成声明」的对话合成先例——v1.13 蓝图形态的出处扩到档位粒度 |
| 分层抽样理论：[不成比例分层配额](https://quali-fi.com/learn/disproportionate-stratified-sampling)、[多路分层](https://www150.statcan.gc.ca/n1/pub/12-001-x/2002002/article/6433-eng.pdf)（Statistics Canada）、[Winkler 2009](https://www.census.gov/content/dam/Census/library/working-papers/2009/adrm/rrs2009-08.pdf)（US Census） | — | 逐层独立配额（Neyman/最优配置按层各配各的）；类 × 档二维分层**不必每格都取样**（受控选择/格子抽样） | 统计学正名：「每个外层层用自己的内层配置方案」是标准嵌套分层；(类, 档) 零额格合法——与 v1.14 零额 WARN 语义一致 |
| [Cosmopedia](https://huggingface.co/blog/cosmopedia)（spec §1.5 已引） | 2024 | 受众 × 风格**全局网格**配比 | 反面参照：全局网格形态即 v1.14 现状；种子源不同桶集不同时靠逐源独立配比——两形态并存，本需求属后者 |

**主导模式小结**：业界两种形态——全局网格（v1.14 现状）与**逐父值子表**（Gretel subcategory、GLAN 逐节点 syllabus、SGD 每服务 schema）。意图承载构成差异时逐父值子表是标准解；统计学侧即嵌套分层的「层内自带配置方案」。

## 3. 候选与取舍

| 候选 | 结构 | 裁决 |
|---|---|---|
| **方案·按类档位表（推荐）** | `[[class.<name>.generate.tiers]]` 整表原子覆盖 + 全局表回落锚；rank 类内身份；配分/映射函数零改动、调用点按类查表；报表条件化类嵌套 | **采纳**：仓内先例齐备（`[class.*.quality].rubric` 整表替换、`[class.*.annotate].schema_*` 覆盖回落、`output.schema` 恰一不因按类覆盖豁免）；v1.14 已把配分与映射做成表参数化——改动集中在取表点 |
| 方案·仅按类权重 | 全局表定构成，类只覆盖 `tier_weights = [1, 2]` | **否决**：表达力残缺——档位身份就是构成集合（裁决·档位即帧类构成），改不了构成的按类档位没抓住痛点；权重数组与全局表位置耦合脆弱。列演进候选（速记糖） |
| 方案·全局表 × 类过滤 | 类声明 `tier_ranks = [1, 3]` 选用全局表子集 | **否决**：既不能重配权重也不能改构成；子集选取破坏「生效表连续覆盖 1..N」的身份语义 |
| 方案·类即档（现状绕法） | 把类 × 档编码成更多序列类（`ticket_booking_t1` / `_t2`） | **否决为设计**（手册可记为现行绕法）：类表爆炸、按类 annotate Schema 与指令成倍复制、`classification.label` 被合成变体污染、报表 sequences 碎片化 |
| 方案·全按类无全局回落 | 删全局表，只认按类表 | **否决**：破坏 v1.14 既有工程字节等价；「全类同档」最常见场景被迫逐类重复声明 |

## 4. 需求方裁决（2026-08-19，本轮对话确认按推荐实施）

| 议题 | 裁决 |
|---|---|
| 档位面统一性（部分类无档位可否混跑） | **全局表为锚**：按类表要求全局表在场 ⇒ 每个参与类恒有生效表，三点标识（generator / truth / report）在场性恒定不逐行漂移；「某类不想分档」= 给它一张单档表（退化形态） |
| 报表形 | **条件化嵌套**：仅全局表 ⇒ v1.14 平面形字节不变；任一按类表在场 ⇒ 类嵌套形（rank 跨类聚合在按类语义下是假数） |
| 跨类 rank 语义 | **不可比**：无「各类的 1 档是同一种要求」的对齐约束（工具本就不赋予 rank 质量方向，v1.14 语义的自然收窄） |

## 5. 遗留项（演进候选，均不进本版）

| 名称 | 议题 | 处置 |
|---|---|---|
| 演进·按类权重速记糖 | `tier_weights` 数组覆盖全局表权重（构成沿用全局） | 等真实工程出现「只改权重不改构成」的高频形态再裁 |
| 演进·跨类 rank 对齐 | 「各类 rank 1 是同一质量要求」的声明式对齐 | 回到「全局表 × 类过滤」族，表达力换一致性——需求出现再议 |
| v1.14 遗留候选 | 按档绑 llm profile、按档 `len_range`/`temperature`、档间序约束、宽松构成、平面形态档位 | 照旧停放（v1.15 不触碰） |

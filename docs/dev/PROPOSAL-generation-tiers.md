# 提案：时间流生成修订——帧类构成档位与时间字段回填

> **状态：superseded by v1.18 sequence generation。** 当前行为事实源为
> `docs/dev/SPEC-sequence-generation-redesign.md`、`spec/*.md` 与 `docs/CONTRACTS.md`；
> 本文其余内容仅保留历史方案论证，旧 SPEC 链接不代表当前行为。
> 本文记录 v1.14 与 v1.15 当时的设计过程；对应旧开发 SPEC 已在 v1.18 clean break 删除。

---

## 1. 结论先行

**推荐两项机制，均为 v1.13 时间流形态（`[generate.stream]`）的增量，默认关闭、关闭即字节等价：**

1. **档位面「方案·蓝图 Schema 硬约束」**：档位就是帧类构成——`[[generate.stream.tiers]]` 逐档声明 `tier_rank`（第几档）、`weight`（配额权重）与 `frame_classes`（该档序列恰由哪些帧类构成）；类配额按权重做**最大余额法确定性配分**（零抽签，冻结的抽签消费顺序表原文不动），蓝图调用的 `[帧类表]` 只渲染档内帧类、`plan_schema` 的 enum 限档内子集并以 `contains` 逐类要求覆盖——「不出档外类」与「档内每类至少一次」由 M8 四层保障机制**双向硬性兜住**，不靠提示词祈祷；档位序数落 `_meta.source.generator.tier_rank`、工件 `truth.tier_rank` 与 `report.generate.stream.tiers`。不携带任何质量指令（需求方澄清：不控制帧内部语义质量）。
2. **时间字段面「方案·绑定即剔除 + 机械回填」**：`[frame.class.<name>.generate.time_fields]` 把生成 Schema 里的时间语义字段绑定到闭集语义词表（`ts` / `gap_prev_s` / `gap_next_s` / `elapsed_s`）；被绑定字段**从 LLM 面向的逐位 Schema 与契约行中剔除**（LLM 物理上无法生成它），时间戳铺设之后、直装组装之前由零 LLM 的机械回填尾声按已铺 ts 计算写回——「构造时既知的量直接保留为真值」这一 v1.13 构造保真原则从标签面延伸到时间量面。回填先于行对象与 id 计算，工件重放逐字节一致。

```mermaid
flowchart LR
    subgraph P["计划期（零新增抽签）"]
        Q["配额展开<br/>类字典序"] --> T["<b>档位配分</b><br/>weight 最大余额法<br/>零 rng"]
    end
    T --> B["蓝图调用<br/><b>[帧类表] 只渲染档内帧类</b><br/>plan_schema enum 子集 + contains 覆盖"]
    B --> R["帧实现调用<br/><b>逐位 Schema = 生成 Schema − 绑定字段</b>"]
    R --> W["机械交织<br/>ts 铺设（既有）"]
    W --> F["<b>机械回填尾声</b><br/>绑定字段按 ts 差计算写回<br/>零 rng 零 LLM"]
    F --> A["直装组装（既有）<br/>id 按回填后全对象计算<br/>truth.tier_rank 落工件"]
    A --> O["输出标识<br/>generator.tier_rank<br/>report tiers 子块"]
    style T fill:#e8f5e9,stroke:#2e7d32
    style B fill:#e8f5e9,stroke:#2e7d32
    style F fill:#e8f5e9,stroke:#2e7d32
```

## 2. 需求拆解与现状

### 2.1 用户语言 → 仓内概念映射

| 用户语言 | 仓内概念 | 现状 |
|---|---|---|
| 质量分档 / 档位（澄清后 = 帧类构成分档） | 档位：序列的帧类构成集合，`tier_rank` 为档位身份 | 不存在。蓝图 enum 恒为全帧类表（`labelkit/operators/generate.py:1384`），构成不可控 |
| 第几个档位的要求 | `[[generate.stream.tiers]]` 的 `tier_rank` 键 | 不存在 |
| 基于分档生成不同档位的序列数据 | 类配额 × 档位权重的确定性配分 | 不存在。最近似面 = `[[generate.styles]]` 均匀抽签（`rng.choice`，无配额控制；且蓝图不带风格——裁决·生成键效力矩阵） |
| 在序列输出中标识档位 | `_meta.source.generator` 增 `tier_rank`；工件 `truth` 增 `tier_rank` | `generator` 现为 `{llm, style}` 两键（`spec/306-m6-generate.md:96-103`）；`truth` 冻结键集无档位（裁决·工件行真值字段集） |
| duration（时间跨度字段） | 帧类生成 Schema（`[frame.class.<name>.generate].schema_*`）内的时间语义字段 | LLM 凭空生成：帧实现在交织期 ts 铺设**之前**发生，值与实际间隔必然无关 |
| 工程 harness 主动管理 | 机械回填：绑定字段剔出 LLM 面、按已铺 ts 计算写回 | 不存在。时间戳本身已是机械管理（`_lay_timestamps`，`generate.py:1023-1041`），缺口仅在**载荷内**的时间语义字段 |

### 2.2 现状核查结论

| 核查角度 | 结论 | 关键证据 |
|---|---|---|
| 时间戳铺设归属 | 已是机械交织器第⑨步：起点 `ts_start`、帧间隔 `uniform(frame_gap_s)`、会话间隔 `uniform(gap_s+lo, gap_s+hi)`、严格递增、恒不取墙钟；需求原文「依赖 LLM 自己生成」的前提不成立，真实缺口在载荷内时间字段 | `generate.py:1023-1041`、`spec/306-m6-generate.md:186` |
| 蓝图/实现两调用的时间面 | 输出 Schema 无任何时间字段（蓝图 = `{frame_class, brief}`、实现 = 逐位帧内容）；LLM 从未见过时间轴 | `schema_engine` `plan_schema`/`realize_schema`、CONTRACTS §10.14/§10.15 |
| M1 时间静态约束 | `frame_gap_s` 上界 < `stream.gap_s`（帧间隔不得自触发会话切分）、最坏会话跨度 ≤ `session_max_span_s`、`ts_start` ISO 可解析 | `labelkit/common/config/_constraints.py:1131-1135,1165-1178` |
| 风格条件化可否挪作档位 | 否：均匀抽签无配额、不进蓝图（结构不受控）、无输出档位语义（`generator.style` 承载多样性观测，挪用即污染 buckets 语义） | `generate.py:236-237`、裁决·生成键效力矩阵 |
| 构成约束的既有承载力 | draft 2020-12 原生 `contains`/`allOf` 可表达「每类至少一次」；M8 四层（供应商结构化输出→确定性修复→jsonschema→有界修复环）与修复穷尽作废语义（`plan_failures`）原样可用，零新失败机制 | `jsonschema` ≥ 4.21 draft 2020-12 支持（v1.13 prefixItems 同一结论）；`generate.py:1369-1409` |
| 抽签面影响 | 档位配分与时间回填都可做成零 rng 纯函数——冻结的抽签消费顺序表（计划期①②③/派发期零/交织期④–⑨）原文不动 | 当时 v1.13 的抽签消费顺序裁决 |
| 观测与估算面影响 | 两项机制都不改变调用数 ⇒ `estimate_run` 精确复演不变、八个 dry-run golden 字节不动、console 键集不动 | 裁决·估算精确复演、裁决·golden 冻结锚不动 |

### 2.3 与既有决策的关系（为什么这不是翻案）

| 既有决策 | 内容 | 与本提案的关系 |
|---|---|---|
| 裁决·生成键效力矩阵（v1.13） | styles 生效于实现与噪音调用、蓝图不带风格 | **不动**：档位与风格正交（档管结构、风格管措辞多样性），两者可同时在场 |
| v1.13 非目标「蓝图低温分档」 | 蓝图调用按温度分档以稳结构 | **不冲突**：那是温度维稳手段（演进候选照旧停放）；本提案的档是帧类构成，与温度无关 |
| 裁决·工件行真值字段集（v1.13，冻结） | truth 键集 = session/sequence_class/sequence/frame_class/noise[+duplicate_of] | **修订**：增 `tier_rank`（档位开启时在场；键集冻结面随 SPEC 重冻结） |
| 裁决·抽签消费顺序表（v1.13，冻结） | 单流三段消费，测试钉住 | **原文不动**：档位配分插在配额展开与长度抽取之间、回填尾声插在 ts 铺设与直装之间，均零 rng 消费 |
| 裁决·真值不携最终 id（v1.13） | truth 用计划期标识，禁携装配后 id | **遵守**：`tier_rank` 是计划期标识（配分产物），无循环依赖 |
| 裁决·构成保真（v1.13 调研小结「构造保真、校验兜底」） | 构造时既知的标签直接保留为 ground truth | **延伸**：时间量（间隔/历时）同为构造既知量，回填即该原则从标签面到时间量面的延伸 |
| 裁决·量目标辖区（v1.13） | 按类 `sequences` 是尝试配额，无补齐回路 | **继承**：档位配额 = 尝试配额 × 权重配分，同样无输出条数保证（作废序列不按档补齐） |
| §6.3「信封只增字段」惯例 | `_meta` 演进以增列为限 | **遵守**：`generator` 增 `tier_rank` 键（档位关闭时不在场，逐字节回退） |

### 2.4 需求精确边界

- **档位仅时间流形态**：平面生成形态（种子池/无种子）无蓝图、无帧类概念，档位无处附着；不做。
- **档位不携质量指令**（需求方澄清）：不注入任何「写得好/写得差」的条件化文本；帧内语义质量照旧由类生成指令与温度决定。
- **档位序数无高低语义**：`tier_rank` 是身份与确定性排序依据，工具不赋予「档高质优」方向——方向语义归用户。
- **时间字段仅帧级生成 Schema**：序列级标注 Schema（`[class.<name>.annotate].schema_*`）里的时间字段是标注员（LLM）从数据抽取的产物，不属生成侧回填辖区；不做。
- **自由文本内的时间表述不收编**：纯文本帧或 utterance 里的「等了十分钟」无绑定点，列演进候选（提示词时间语境注入）；手册指引：需要时间语义的字段应放进结构化帧 Schema 走绑定。

## 3. 业界方案调研（2026-08-18 检索核实）

### 3.1 档位面：条件化合成与结构控制

| 名称 | 年份 | 做法 | 可迁移实践 |
|---|---|---|---|
| [SteerLM](https://arxiv.org/abs/2310.05344) / [HelpSteer2](https://arxiv.org/abs/2406.08673) | 2023/24 | 属性条件化 SFT：生成显式条件在质量属性值上，**高低档样本一起保留**训练；HelpSteer2 以属性分档构造偏好对 | 档位标签是下游（偏好对/课程学习/评分器校验）的监督信号——「输出中标识档位」的价值本尊；属性即元数据、生成期声明生成期落地 |
| [Nemotron-CC](https://arxiv.org/abs/2412.02595)（spec §1.5 已引） | 2024 | 质量分档路由不同合成管线、每档不同处理 | 档位作为第一公民属性贯穿生成与观测；本仓 v1.7 按类路由的同源背书 |
| [Cosmopedia](https://huggingface.co/blog/cosmopedia)（spec §1.5 已引） | 2024 | 受众 × 风格**分桶配比**派生提示 | 分桶 + 配比控制正是 styles 缺失的那一半：配额声明 + 确定性配分 |
| [Evol-Instruct](https://arxiv.org/abs/2304.12244)（spec §1.5 已引） | 2023 | 指令按深化/扩展算子分难度演化 | 难度/复杂度做成显式拨盘而非采样撞运气——与 v1.13 调研 WRIT「复杂度显式拨盘」同脉 |
| [Schema-Guided Dialogue](https://arxiv.org/abs/1909.05855)（v1.13 已引） | 2020 | 每服务一份 schema 驱动 outline，构成即真值 | 「构成声明在计划层、实现层零再标注」——档位构成放蓝图层的先例形态 |
| M8 闭集纪律（[Autolabel](https://github.com/refuel-ai/autolabel) 形态，spec §1.5 已引） | — | 「标签 ∈ 词表」以 Schema enum 硬校验 | 档位构成约束的承载机制就是既有 enum 纪律 + draft 2020-12 `contains`（[json-schema.org](https://json-schema.org/draft/2020-12/json-schema-core)），零新机制 |

### 3.2 时间字段面：仿真器持有时钟

| 名称 | 年份 | 做法 | 可迁移实践 |
|---|---|---|---|
| [数据驱动流程仿真（Camargo et al.）](https://pmc.ncbi.nlm.nih.gov/articles/PMC8293933/) | 2021 | BPS 模型：到达间隔/活动历时全部由分布采样，BIMP 六分布族拟合择优 | **时钟归仿真引擎**：事件内容与时间量分离，时间量永远由引擎写 |
| [LogGenerator](https://github.com/GabrielSiq/LogGenerator) | — | 离散事件引擎按优先队列推进，日志行时间戳全部引擎盖章 | 「行内时间字段 = 引擎产物」的工程同构：本仓交织器即该引擎，缺的只是把载荷内时间字段也纳入盖章范围 |
| [AT-KDE 到达时间建模](https://link.springer.com/article/10.1007/s44311-026-00041-z) | 2026 | 非参数 KDE 捕捉到达时间的全局趋势/星期/日内动态 | 间隔分布形的演进方向（uniform → 经验分布）；证明间隔建模是仿真侧长期演进面，更不应交给 LLM |
| [PLG2](https://arxiv.org/abs/1506.08415)（v1.13 已引） | 2016 | 多流程实例按各自时间戳归并交织 | v1.13 交织器的出处；回填尾声是同一机械管线的自然延长 |
| 过程挖掘真值方法（v1.13 已引） | 2025 | 真值在计划层注入并保留链接 | duration 真值 = 已铺 ts 的确定函数，链接天然保留 |

### 3.3 业界主导模式小结

- **属性即元数据，配比即声明**：条件化生成的档位/属性在配置里声明配比、在输出里落标签（SteerLM/HelpSteer2/Cosmopedia）；靠均匀抽签或采样撞运气不是控制。
- **结构约束走硬校验，不走提示词**：构成类闭集本就是 Schema enum 的辖区（Autolabel 纪律、SGD 的 schema 驱动 outline）；覆盖要求用 `contains` 表达后，修复环与作废语义全部免费复用。
- **时钟只有一个持有者**：流程仿真二十年定式——内容生成器永不发明时间量；已知量（间隔/历时）由引擎按已定时间轴计算写入，LLM 生成再覆写或再校对都是浪费与漂移源。

## 4. 方案设计

### 4.1 候选与取舍

档位面：

| 候选 | 结构 | 裁决 |
|---|---|---|
| **方案·蓝图 Schema 硬约束（推荐）** | 档位 = 帧类构成；配额 = 权重最大余额法零抽签配分；蓝图 `[帧类表]` 渲染档内子集 + `plan_schema` enum 子集 + `contains` 逐类覆盖；标识落 generator/truth/report 三点 | **采纳**：双向硬保证（不出档外类 ∧ 档内每类至少一次）零新失败机制；零 rng、零调用数变化 ⇒ 顺序表/估算/golden 全不动 |
| 方案·提示词质量条件化（本提案初稿形态） | `[[generate.stream.tiers]]` 携 `instruction`，追加进蓝图与实现调用 | **否决（需求方澄清 2026-08-18）**：档位定义是帧类构成，不控制帧内语义质量；且纯提示词无结构保证，LLM 可越界用类 |
| 方案·生成后归档 | 不约束蓝图，按实现产物的帧类集合**事后**归档打标 | **否决**：配额不可控（各档产量靠运气，与「基于分档生成」相悖）、构成覆盖不可保证、空档不可预防；标识面与推荐方案等同但控制面为零 |
| 方案·styles 挪用 | 把档位写成风格提示 | **否决**：无配额控制、蓝图不带风格（构成不受控）、污染 `generator.style` 与 buckets 的多样性观测语义 |

时间字段面：

| 候选 | 结构 | 裁决 |
|---|---|---|
| **方案·绑定即剔除 + 机械回填（推荐）** | `time_fields` 绑定表；绑定字段剔出 LLM 面向的逐位 Schema 与契约行；ts 铺设后机械回填，先于 id 计算 | **采纳**：LLM 物理上无法生成矛盾值；不浪费 token；回填是已铺 ts 的确定函数，重放逐字节一致；零 rng 零调用 |
| 方案·提示词注入时间语境 | 计划期预抽序列时间线，喂给实现调用让 LLM 写出一致的 duration | **否决**：值仍是 LLM 手笔（可漂移、须校对）；时间线在交织期才定，前移到计划期须动冻结的抽签消费顺序表；成本与风险都高于回填 |
| 方案·生成后覆写 | LLM 照旧生成 duration，harness 事后覆盖 | **否决**：为注定被覆写的字段付 token 与修复环成本；对 LLM 的契约有误导（要求生成又丢弃）；净劣于剔除 |
| 方案·文档告诫 | 不做机制，手册提醒「别在生成 Schema 放时间字段」 | **否决**：需求真实存在——duration 是用户数据 Schema 的一部分，下游要消费；告诫解决不了字段从哪来 |

### 4.2 档位构成机制（配置草案）

```toml
[generate.stream]
enabled = true
# …… v1.13 既有七键不变 ……

[[generate.stream.tiers]]            # 可选表；缺省不写 ⇒ 与 v1.13 行为字节等价
tier_rank = 1                        # 档位序数（第几档）：正整数、表内唯一、全表连续覆盖 1..N
weight = 2                           # 配额权重（整数 ≥ 1）：类配额按权重最大余额法配分
frame_classes = ["task_request", "followup"]          # 档位构成：恰用这些帧类、每类至少一次

[[generate.stream.tiers]]
tier_rank = 2
weight = 1
frame_classes = ["task_request", "followup", "confirmation"]
```

要点：

- **配分（零抽签）**：对每个参与类（有效 `sequences ≥ 1`），`配额_档 = 最大余额法(sequences × weight / Σweight)`，平票按 `tier_rank` 升序；类内序数按 `tier_rank` 升序占连续区间。配分是 `(sequences, tiers)` 的纯函数，与 `--limit` 前缀截断可交换（截断不扰动映射）。某 (类, 档) 配到 0 时 WARN 提示（权重与小配额相除的自然结果，非错误）。
- **蓝图调用**：`[帧类表]` 只渲染档内帧类（保持帧类表声明序）；user 行在档位在场时追加覆盖要求句；`plan_schema(档内类名, L, cover_all=True)`——enum 限子集，`contains` 逐类至少一次。违约进既有修复环，穷尽按既有 `plan_failures` 作废。帧实现调用零改动（步类已被约束在档内）。
- **标识三点**：`_meta.source.generator` 增 `tier_rank`（rejects 侧 generator 既有携带逻辑自动跟随）；工件 `truth` 增 `tier_rank`（任务帧 = 本档序数、噪音帧 = null、重发帧 = 源序列档位——内容逐字节同源）；`report.generate.stream` 增 `tiers` 子块（按档 planned/produced）。档位另可从 `members[]` 帧类真值集合反推对账（可审计性）。

### 4.3 时间字段回填机制（配置草案）

```toml
[frame.class.task_request.generate]
instruction = "……"
schema_inline = """
{
  "type": "object",
  "properties": {
    "utterance": {"type": "string"},
    "entities": {"type": "array", "items": {"type": "string"}},
    "duration": {"type": "number"}
  },
  "required": ["utterance", "entities", "duration"],
  "additionalProperties": false
}
"""

[frame.class.task_request.generate.time_fields]   # 绑定表：字段名 = 语义词表取值
duration = "gap_next_s"                           # 与本序列下一帧的间隔秒数（末帧取 0）
```

要点：

- **语义词表（闭集四值）**：`ts`（本帧已铺时间戳 ISO 串）、`gap_prev_s`（与本序列上一帧的间隔秒，首帧 0）、`gap_next_s`（与本序列下一帧的间隔秒，末帧 0）、`elapsed_s`（距本序列首帧秒，首帧 0）。间隔按**序内口径**：本序列相邻成员的 ts 差——交叉/噪音夹入的外帧本就占用其间墙钟，差值与下游从数据实测一致。
- **剔除**：逐位 Schema 与契约行按「生成 Schema − 绑定字段」派生（`properties`/`required` 同步减除，其余关键字原样）；M8 按缩减 Schema 校验。至少须留一个未绑定字段（否则 LLM 无事可做，CONFIG_ERROR）。
- **回填尾声**：ts 铺设之后、直装组装之前的纯函数（零 rng 零 LLM 零 IO）；值 = `round(ts 差秒, 6)`（微秒精度对齐 isoformat）。重发帧共享源序列**回填后**的载荷对象（逐字节同源不变式保持；其自身 ts 顺延——原样重发本就携带陈旧内容，语义自洽）。回填先于行序列化与 id 计算 ⇒ 工件重放同 id。
- **钩子口径**：`sample_validator` 与序列相似度过滤照旧作用于**回填前**载荷（时间字段是机械量，不参与内容判重与内容校验）。

### 4.4 输出与观测

- `report.generate.stream` 增 `tiers`（counts-only，`{"<tier_rank>": {planned, produced}}`，条件在场）；时间字段面**零观测增量**（确定性机械操作，无可计数的失败模式）。
- `estimate_run` 零改动（两机制都不改调用数），八个 dry-run golden 字节不动；trace 零新通道零新事件、§7.6 零新错误 kind；console 键集与面板零改动。
- 确定性：两机制均零 rng；§2.6 确定性声明链无需新句（作废序列条件化声明覆盖面不变）。

### 4.5 影响面清单

| 文件/文档 | 变更 |
|---|---|
| M1 `labelkit/common/config/`（model / _sections / _classviews / _constraints） | `TierSpec` + `GenerateStreamConfig.tiers`、`FrameClassView.time_fields`、两簇约束与 WARN |
| M6 `labelkit/operators/generate.py` | 配分纯函数 + `SequencePlan.tier_rank`、蓝图档位面、逐位缩减 Schema、回填尾声、truth/generator 标识、tiers 计数 |
| M8 `labelkit/common/runtime/schema_engine.py` | `plan_schema` 增 `cover_all`（enum 子集由调用方传参，本就零改动） |
| M5 / M7 / M10 / M11 / M12 / M13 / budget / console | **零改动**（回填在 M6 内完成、generator 经 ref 自然流出、调用数不变） |
| spec | `306-m6` §3.6.5 增行、`50-ch5` 两配置面、`60-ch6` generator/truth/report 三点、§1.6 决策日志 |
| `docs/CONTRACTS.md` | `TierSpec`/`FrameClassView` 字段、约束规则、`plan_schema` 签名、§10.14 条件句、truth 键集重冻结、§12 登记 |
| `docs/manual/` | ch.27 扩档位与时间字段两节（真跑重采） |
| examples | `examples/synth-stream` 就地扩两档 + 一个 duration 绑定（dry-run golden 字节不动为验收断言） |

### 4.6 站立假设（实现前逐条验证）

- L0 开启端点（z.ai glm-5.2）对 `contains`/`allOf` 的透传服从性可用（v1.13 `prefixItems` 同款站立假设已验真；若某 strict 路由拒收，既有排障指引 = 配置级 `supports_structured_output = false`，手册注明）；
- 温度 0.9 下蓝图对覆盖约束的服从率可接受（修复环 + 作废语义兜底；v1.13 已有「实现调用偶发违约整序列作废」的同族锐边记录，E2E-FINDINGS）；
- 回填尾声对重发帧的源载荷共享在实现上可行（重发会话恒为纯源序列帧、按位对应，v1.13 裁决·会话装箱定容保证）。

## 5. 裁决记录与遗留处置

需求方已闭三项（2026-08-18，本轮对话）；SPEC 化时誊入 spec §1.6 决策日志。

| 名称 | 议题 | 裁决 |
|---|---|---|
| 裁决·档位即帧类构成 | 档位定义：语义质量条件化还是结构构成？ | **帧类构成（需求方 2026-08-18）**：「序列有 x 类帧为 a 档，有 x+y 类帧为 s 档」；不控制帧内部语义质量——初稿的档位 instruction 键删除 |
| 裁决·tier_rank 即档位身份 | 档位表主键：`name` 还是序数？ | **tier_rank（需求方 2026-08-18）**：不设 name；`tier_rank` 代表「第几个档位的要求」——正整数、唯一、连续覆盖 1..N；工具不赋予序数高低任何质量方向语义 |
| 裁决·时间字段回填方向 | duration 类字段与实际帧间隔不符如何收编？ | **绑定即剔除 + 机械回填（需求方 2026-08-18 确认缺口）**：时间戳铺设已属机械面，载荷内时间语义字段同样收归 harness——LLM 面剔除、铺设后回填 |

随方案采纳一并确定、SPEC 阶段落文的设计裁决：

| 名称 | 议题 | 处置 |
|---|---|---|
| 裁决·构成恰等 | 档位构成取「恰等」（不出档外类 ∧ 逐类覆盖）还是「至多这些类」？ | 采**恰等**为默认：档位身份须可从数据反推（members[] 帧类集合 = 构成集合），宽松形态会使 a 档与 s 档的低配产物不可区分；宽松形态列演进候选（去掉 contains 即得） |
| 裁决·零抽签配分 | 配额配分用加权抽签还是确定性配分？ | **最大余额法零抽签**：精确配额（数据生产要的是定量不是期望值）、冻结顺序表原文不动、`--limit` 交换律免费成立；平票按 tier_rank 升序 |
| 裁决·语义词表四值 | 回填语义词表首批收词 | `ts` / `gap_prev_s` / `gap_next_s` / `elapsed_s` 四值 + 首末帧取 0 的边界定义；需求实测只用 `gap_next_s` 形态的话可在 SPEC 评审时裁剪（词表是闭集，扩词走修订） |
| 裁决·重发帧承源档与同源载荷 | 重发帧的 tier_rank 与回填值取自身还是源？ | 均取**源**：重发的语义本体是「内容逐字节同源」（v1.13 冻结不变式），档位与时间字段皆内容属性；重发帧自身的流水时间轴仅体现在其行 ts 字段 |

遗留项：

| 名称 | 议题 | 处置 |
|---|---|---|
| 演进·档位绑定生成档案 | 按档绑定不同 llm profile（UltraFeedback 式强弱模型对比） | 不进本版：会扰动冻结的 (llm, style) 预抽流与密钥引用集语义；列演进候选 |
| 演进·按档结构参数 | 按档覆盖 `len_range` / `temperature` | 不进本版（最简原则）；构成差异已由帧类集合承载 |
| 演进·自由文本时间语境 | 纯文本帧内时间表述与时间轴对齐 | 列演进候选（提示词时间语境注入，须动顺序表另裁）；手册指引结构化字段走绑定 |
| 演进·间隔分布形 | `frame_gap_s` 均匀分布扩展为分布族（BIMP 六族 / AT-KDE 经验分布） | 列演进候选；与本提案正交 |

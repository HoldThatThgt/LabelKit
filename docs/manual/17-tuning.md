# 第 17 章　性能、成本与并发调优

> LabelKit 的瓶颈几乎永远是 LLM API——本章教你算清三本账：**调用账**（多少次）、**时间账**（多久）、**内存账**（多大），
> 然后给出每本账的调优抓手。

## 17.1 调用账：钱花在哪

一次运行的 LLM 调用构成（设 N = 存活记录数，C = 准则数，k = pairwise 轮数）：

| 来源 | 次数 | 说明 |
|---|---|---|
| segment（v1.8） | Σ ceil((L−1)/(w_eff−1))，L = 各会话帧数；w_eff = min(window, w_min) | 滑窗重叠 1 帧故步长 = 窗宽−1。未声明预算时 w_eff = window（与 v1.10 同式同值）；所引 profile 声明 `context_window` 后 window 只是**上限**、按预算贪心装填，公式按最坏保证装填量 w_min 报**上界**（实际每窗只多装、窗数只更少，事后看 `report.stream.windows` 对账）；`strategy="rules"` 与单帧会话为 0；window ≥ 会话长且预算装得下时整段一次调用 |
| stitch（v1.9） | 一遍每 episode 候选 1 次 + 二遍每单碎片线索 1 次，全量 × `votes` | 判定纯文本无图、单次便宜；救援候选仅池非空时判定、池空零调用，无其他线索的复评同样零调用。**votes 成本注**：`votes = n`（奇数）把每次判定放大为 n 次采样——n=3 时 stitch 全口径占比仍 <8%，但只该在漂移可测时才开（第 26 章） |
| classify（v1.7） | N × max(1, sc) | sc = `classify.self_consistency`；生成样本继承种子类，回流不重分类 |
| classify 帧粒度（v1.12） | 存活 episode 数（每 episode 一次**批量**判决；预算装填下长会话才拆多窗，按窗计） | 住 dedup 之后——重复 episode 零帧调用；一次调用判整窗成员，**不是**每帧一次；dry-run 的 `frame_classify_calls` 按预扫描帧总数报粗上界 |
| extract（v1.8） | Σ (L−1)，L = 各 episode 成员数 | 每对相邻帧一次调用、每次带 2 张图——**stream 工程的调用大头几乎总是它**（帧数远多于 episode 数） |
| quality pairwise | N × k / 2（默认 k=4 ⇒ 2N） | × 评审数 × 双顺序(2) ×（single 模式再 ×C） |
| quality pointwise | N × C | 与 C 成正比是它比 pairwise 贵的原因（C=4 时 4N vs 2N） |
| annotate | N | × self_consistency 的 n |
| annotate 帧粒度（v1.12） | Σ 过质量门 episode 的未跳过成员数 | 逐成员一次调用（帧粒度里贵的那半）；住 quality 门之后——被淘汰记录永不付帧标注费；`[frame.class.<名>.annotate].enabled = false` 按类再省；dry-run 的 `frame_annotate_calls` 同样按帧总数报粗上界 |
| generate | ⌈种子数 × num_per_record / num_per_call⌉ | 产出还会回流产生新的 quality/annotate 调用 |
| generate 时间流形态（v1.13） | **2 × Σsequences + ⌈噪音帧数 / num_per_call⌉** | 一序列一次蓝图 + 一次帧实现，噪音批量另算；装箱/交叉/噪音位置/时间戳零 LLM。下游 quality/annotate/verify 的记录基数 = 幸存序列数 |
| verify | N 左右 | × 评审数；每轮 repair 追加 1 标注 + 1 复审 |
| 结构修复（LLM 修复环） | 按需 | 健康工程接近 0；`resolved_at.l3_*` 高说明 Schema 有问题（第 14 章） |
| 重试 | 按需 | 报告 `llm_usage.*.retries` 可见 |

分类算子开 `assignment = "multi"` 时另记一笔扇出账：一条记录命中几类就变成几个信封，下游 quality / annotate / verify 的 N 实际乘上平均标签数——multi 工程做预算时按这个乘数打提前量（`--dry-run` 的估算按乘数 1 报下界）。

stream 工程（v1.8）另有两条成本注记：`annotate.sequence_frames`（默认 20）决定每个 episode 的标注请求**至多**携带几张关键帧图（v1.11 起它是上限：声明 `context_window` 后实际帧数 k_eff 按预算剩余收缩，首末帧恒保留，第 11 章）——序列标注的 token 开销与实际帧数近似线性，降帧最省钱但会丢视觉证据；`extract.include_diff` 默认开（向摘取提示词注入结构化树变更摘要，工程实践正面），怀疑它对你的数据没有增益时可关掉跑一次 A/B（对比 `report.stream.extract.by_type` 分布与 verify 缺陷率），确认后再定去留。

帧粒度（v1.12，第 25 章 25.6）的成本模型自带四重保护，预算时按「上界很松、实付常小得多」来读：`--dry-run` 的 `frame_classify_calls` / `frame_annotate_calls` 都按**预扫描帧总数**报粗上界；实付层面——帧分类是**每 episode 一次批量判决**（一次调用判整窗成员，不是每帧一次），且住 **dedup 之后**（重复 episode 一分帧钱不付）；帧标注虽是逐成员一次调用（帧粒度里贵的那半），但住 **quality 质量门之后**（被淘汰记录永不付帧标注费），还能用 `[frame.class.<名>.annotate].enabled = false` 把低价值帧类整类跳过。`examples/mix` UI 主工程本次真跑的对照：上界 17/17，实付 2 次批量判决 + 9 次帧标注（1 个 transition 过渡屏成员按类跳过）——对账看 `report.stream.frame_classify` / `frame_annotate` 两个子块（第 8 章）。该工程还是「贵的调用挑贵的端点」的活例：双端点分账 `llm_usage.default`（DeepSeek，segment 滑窗/帧级批量分类/轨迹打分的文本判决面）与 `llm_usage.vision`（z.ai，序列分类/序列标注/帧标注/评审的视觉必需面）本次真跑各 15 次调用——帧级批量分类永不要求 vision，指向便宜的纯文本 profile 即省钱面（第 25 章 25.6 的双端点成本拆分）。

时间流生成（v1.13，第 27 章）的成本账有两点与别处不同。其一，**`--dry-run` 的估算是精确复演而非上界**：估算分支吃同一套计划期纯函数（同 `seed` 同配置），序列长度、噪音帧数、会话装箱按真实抽签算一遍——`examples/synth-stream` 真跑对照是估 49、实 51（`llm_usage.default.calls`）、约 116 秒，差额两次逐笔对得上：一轮 verify repair 的重标注 + 复审，别无其他（该次运行用户 Schema 侧 `resolved_at` 的 `l1` / `l3_*` 与 `retries` 全为 0），而修复与重试**历来不进估算**。其二，**阶段大头这一跑落在 quality 上，但别当结论**：真跑 `per_stage_s` 是 `{"generate": 23.256, "dedup": 0.004, "classify": 0.0, "quality": 38.949, "annotate": 20.858, "verify": 32.636}`，而同一份配置的另一跑是 `generate` 58.999 秒独大——6 条序列一批并发发出，单次运行的阶段耗时被端点波动与并发窗口支配。稳定的只有两条结构性事实：生成调用要写出整条序列的内容，**单次**输出 token 远大于判决类调用（单价最贵）；quality 的调用**次数**最多（准则数 × 幸存序列数）；`classify` 恒 0.0 秒是标签继承的直接体现。控成本的旋钮按性价比：砍 `sequences`（配额是尝试配额，直接线性）> 缩 `len_range`（实现调用的输出 token 与步数近似线性）> 调 `noise_ratio`（噪音是批量装箱的，最便宜的那部分）。v1.14 的帧类构成档位与时间字段回填**都不改调用数**（前者是零抽签纯函数配分、后者是零 LLM 的时间轴算术），所以它们不进这笔账，dry-run 那一行也逐字节不变。

**先验预算**：`--dry-run` 直接给出估算调用数（不含修复与重试）。**后验核账**：报告 `llm_usage` 分 profile 给出 calls / tokens / retries，配了单价还有 `est_cost_usd`。

省钱抓手按性价比排序：

1. **把 rubric 和 instruction 在 `--limit` 小样本上调到位再跑全量**——返工全量一次的钱够你小样本迭代五十轮；
2. **quality 模式选对**：C ≥ 3 时 pairwise（2N）比 pointwise（CN）便宜且是默认推荐；只有一两条准则、又要跨批绝对分数时 pointwise 才占优；
3. **能不开的鲁棒性选项别急着开**：judges ×3、both_orders ×2、self-consistency ×n、verify ×1.5——全开是 10 倍级别的成本放大，按第 10/11/13 章的决策线逐个论证再开；
4. **调小 `max_output_tokens` 依然不是省钱手段，但坑在 v1.11 换了形态**——输出写满上限的截断响应不再触发修复环，而是按 `output_truncated` **记录级拒收**（终局，不修复，第 14 章）：省下的不是修复调用，丢掉的是整条记录。它反而多了一个新权衡面：声明了 `context_window`（第 6 章）时，`max_output_tokens` 整段从窗口里预留出去、直接挤占输入预算——输出上限越大，单次调用能装的输入越少（裁剪更狠、装填更碎、调用可能更多）。按你 Schema 的真实输出规模取宽裕但不奢侈的值。

## 17.2 时间账：为什么慢、怎么快

**吞吐模型**：同一算子内记录级并发，算子间批内串行（屏障）。所以：

```
批耗时 ≈ Σ各算子耗时；算子耗时 ≈ ⌈该算子调用数 / 有效并发⌉ × 单次调用延迟
```

真实参照（第 3 章的运行，14 条、并发 4）：quality 76 秒、annotate 11 秒、dedup 0.004 秒——**quality 几乎总是时间大头**，因为调用数最多（52 次 = 去重后 13 条 × 4 条准则；dry-run 按 14 条估算为 56）而并发只有 4。

提速抓手：

1. **`max_concurrency`**（config.toml，按 profile）：最直接的旋钮。从网关限流值的 50–70% 起步，观察 `retries` 计数——重试开始增多说明顶到限流了，回调一点。多个算子引用同一 profile 时共享这个额度；给 verify/quality 配不同 profile（哪怕同一模型）可以各拿一份并发额度；
2. **`batch_size`**：批越大，屏障摊销越好、并发越吃得满。但 pairwise 用户注意——批大小首先是**质量口径参数**（第 10 章），别纯为吞吐调它。pointwise 无此顾虑，可以放心加大；
3. **网络位置**：延迟高的跨境端点，单次调用 5–8 秒很常见；同机房网关能砍一个量级。

## 17.3 内存账：50 万条的 RSS 预算

| 占用者 | 量级 | 备注 |
|---|---|---|
| 全局去重索引（LSH + 精确键 + pHash） | 50 万条 ≈ 2–4 GB | `dedup.scope="batch"` 可砍掉大头（代价：跨批漏检） |
| 批内信封对象 | 与 batch_size 成正比 | 通常不是问题 |
| 语义去重向量索引（可选） | 条数 × 维度 × 8B（float64 存储；50 万 × 1024 维 ≈ 4 GB，缓冲倍增扩容瞬间峰值更高） | scope=global 时常驻，要计入预算 |
| 图像字节 | **不常驻** | 接入算 id、去重算 pHash、构造请求时各读一次，用完即弃（第 5 章） |

超过 50 万条的正确姿势是**切分多次运行**（第 5 章），不是硬顶内存。

## 17.4 可靠性参数的配合

`fatal_error_threshold`（默认 20）、`max_retries`（默认 5）、`retry_base_delay_s`（默认 1.0）三者的配合逻辑：

- **端点偶尔抽风**（零星 429/5xx）：靠 max_retries 的全抖动退避消化，你什么都不用动；
- **端点持续限流**：调大 `retry_base_delay_s`（2–4）+ 调小并发，比调大 max_retries 有效；
- **端点彻底坏了**：认证失效（401/403）现在**立即熔断**；模型下架/拼错（400/404）靠连续计数熔断止损，想更快止损调小 threshold（如 5）；
- **CI 里跑**：`--strict` + 解析退出码，让失败可编程感知（第 15 章）。

## 17.5 一张调优决策表

| 症状 | 先看 | 动哪个旋钮 |
|---|---|---|
| 跑得慢 | report.timing 哪个阶段占大头；llm_usage.retries | max_concurrency ↑；批大小 ↑（pointwise）；检查网关延迟 |
| 花得多 | llm_usage.calls 分布；resolved_at.l3_* | quality 模式换 pairwise；关不必要的鲁棒性选项；修 Schema |
| retries 高 | 网关限流日志 | 并发 ↓、退避基数 ↑ |
| failed 高 | rejects 的 `_meta.reason`（= 首个错误的错误码；`errors` 是对应消息文本） | provider_* → 端点/密钥问题；schema_violation → 第 14 章 |
| 内存吃紧 | 条数 × 是否 global scope | 切分运行；scope=batch；语义去重改 batch 或关 |
| 质量门口径不对 | aggregate_histogram | 第 10 章（threshold/top_ratio/模式选择） |
| 错缝 / 漏缝（stream×stitch，v1.9） | report.stream.stitch；trace 的 stitch.judge（priors 命中腿 / merged） | 错缝：`bias` 保持 conservative、`votes` ↑（3/5，奇数）、`stale_gap_steps` 设阈让久挂线索降格；漏缝：补强 `stitch.context`、确认 `repass` 开着、查先验哪条腿没命中；穿插深的流：`max_open` ↑（第 26 章） |
| 合成序列产出少于配额（时间流生成，v1.13） | report.generate.stream 的 `produced/planned` 与三项 failures；开了档位表再看 `tiers` 逐档缺口（v1.14） | `realize_failures` 高 ⇒ 降 `generate.temperature`、缩 `len_range`、给帧类 Schema 减字段；`survived_dedup ≪ produced` ⇒ 反过来提温度、把类 instruction 写出更多可变要素（**别放松 `[dedup]` 阈值**）；`validator_scrapped` 高 ⇒ 钩子对整序列过严（一帧违规废整条）；缺口清一色压在最高档 ⇒ 给该档减一个帧类或放宽该类 `len_range` 上界（第 27 章） |

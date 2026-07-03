# 第 17 章　性能、成本与并发调优

> LabelKit 的瓶颈几乎永远是 LLM API——本章教你算清三本账：**调用账**（多少次）、**时间账**（多久）、**内存账**（多大），
> 然后给出每本账的调优抓手。

## 17.1 调用账：钱花在哪

一次运行的 LLM 调用构成（设 N = 存活记录数，C = 准则数，k = pairwise 轮数）：

| 来源 | 次数 | 说明 |
|---|---|---|
| quality pairwise | N × k / 2（默认 k=4 ⇒ 2N） | × 评审数 × 双序(2) ×（single 模式再 ×C） |
| quality pointwise | N × C | 与 C 成正比是它比 pairwise 贵的原因（C=4 时 4N vs 2N） |
| annotate | N | × self_consistency 的 n |
| generate | ⌈种子数 × num_per_record / num_per_call⌉ | 产出还会回流产生新的 quality/annotate 调用 |
| verify | N 左右 | × 评审数；每轮 repair 追加 1 标注 + 1 复审 |
| 结构修复（L3） | 按需 | 健康工程接近 0；`resolved_at.l3_*` 高说明 Schema 有问题（第 14 章） |
| 重试 | 按需 | 报告 `llm_usage.*.retries` 可见 |

**先验预算**：`--dry-run` 直接给出估算调用数（不含修复与重试）。**后验核账**：报告 `llm_usage` 分 profile 给出 calls / tokens / retries，配了单价还有 `est_cost_usd`。

省钱抓手按性价比排序：

1. **把 rubric 和 instruction 在 `--limit` 小样本上调到位再跑全量**——返工全量一次的钱够你小样本迭代五十轮；
2. **quality 模式选对**：C ≥ 3 时 pairwise（2N）比 pointwise（CN）便宜且是默认推荐；只有一两条准则、又要跨批绝对分数时 pointwise 才占优；
3. **能不开的鲁棒性选项别急着开**：judges ×3、both_orders ×2、self-consistency ×n、verify ×1.5——全开是 10 倍级别的成本放大，按第 10/11/13 章的决策线逐个论证再开；
4. **调小 `max_output_tokens` 不是省钱手段**——截断输出触发修复环，更贵。

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

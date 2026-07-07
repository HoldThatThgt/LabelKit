## 3.10 M10 编排器 orchestrator

### 3.10.1 职责与边界

**做：**把 M2 记录流切批；按配置组装阶段链并逐批驱动；调度生成子批回流；聚合运行级统计；控制批生命周期——批完成（其输出行已写盘）后释放该批全部中间态；熔断监测。 
**不做：**零业务逻辑（不知道任何算法细节）；不直接调用 LLM；不写文件（M11 职责）。

### 3.10.2 主循环与批生命周期

图 3-4 批生命周期时序。全局仅去重索引与统计计数器跨批存活，其余中间态随批释放。

### 3.10.3 API 与行为规格

```
class Orchestrator:
    def __init__(self, cfg: ResolvedConfig, stages: list[Stage],
                 ingestor: Ingestor, emitter: Emitter, llm: LLMClient): ...
    async def run(self) -> RunSummary: ...

@dataclass
class RunContext:      # 传入每个 stage.run 的上下文
    cfg: ResolvedConfig; llm: LLMClient; schema_engine: SchemaEngine
    rng: random.Random          # seed 派生: Random(f"{run.seed}:{batch_no}:{stage.name}")
    batch_no: int; metrics: MetricsSink
```

| 规格 | 定义 |
|---|---|
| 跨批存活状态 | 仅三项：① DedupIndex（scope=global 时）；② MetricsSink 计数器；③ M9 用量累计。均不含数据内容本体（哈希/签名/计数），运行结束随进程销毁。 |
| 尾批 | 最后一批不足 batch_size 照常处理；批内仅 1 条时 M4 不发裁决调用，各 criterion score 固定 0.5（3.4.3 归一化行）。 |
| 熔断 | MetricsSink 维护连续致命计数（ProviderFatalError 与重试耗尽 provider_retryable_exhausted 均计入，7.6），达 `run.fatal_error_threshold`（默认 20），或 401/403 认证类首错**立即**（v1.5，3.9.3；v1.6 密钥池下「认证首错」= 该 profile 最后一把存活密钥被认证禁用——池内尚有存活密钥时单密钥认证失败仅禁用该密钥、不计入熔断）⇒ 取消在飞任务、finalize。**熔断交付（v1.6，1.6 对齐决策 ②）**：已完成批的主输出与 rejects 照常 fsync + 原子改名交付（v1.5 及以前为「.part 不交付」——长跑末段配额死亡不再丢弃全部已完成产出），报告写 run.circuit_broken=true 与 run.partial_delivery=true、counts 增列 unprocessed（6.4），退出码 4 不变。「运行完整处理了全部输入」的判定信号由此从「目标文件名出现」改为「report.run.interrupted=false 且 circuit_broken=false」——退出码 0/1 不足以判定：被 SIGINT 优雅中断的运行同样交付且以 0 退出（本表中断行）（3.11.2 主输出行、3.11.3 ④、6.4）。 |
| 中断（SIGINT/SIGTERM） | 停止取新批 → 等待当前批完成或 30s 超时取消 → finalize（报告标记 `interrupted=true`）。已 flush 的输出行有效。 |
| --limit N | M2 流截断在前 N 条记录，其余全流程不变（试跑）；generate_only 模式下作用于生成样本流的前 N 条：仅执行预抽序前 ⌈N / generate.num_per_call⌉ 次生成调用（(llm, style) 预抽不受影响），产出再截断到 N 条——本表下行「执行全部生成调用」带 --limit 时按此截断。 |
| 纯生成模式（v1.4） | `run.mode="generate_only"` 时跳过 M2（IngestReport 全零）：启动后先按 3.6.2 的量公式执行全部生成调用（并发受 profile 信号量限流，(llm, style) 组合按调用序号预抽保证可复现——生成先于切批、尚无批号，预抽 PRNG 固定取 Random(f"{run.seed}:0:generate")，即 3.10.3 派生式中 batch_no 恒取 0），产出构造为 Record 后按 `run.batch_size` 切批，逐批走 M3→M4→M5→M7→M11，批生命周期与内存释放同 process 模式；不触发二次生成（单遍）。规模建议同 2.6（≤ 50 万条）。 |
| 分类与扇出（v1.7） | 规范链序 `_CHAIN_ORDER = ("dedup", "classify", "quality", "generate", "annotate", "verify")`；`_compose_chain` 的 enabled 表增 classify——主链、生成回流链、generate_only 链均含（回流子批带 `source="inherited"` 继承分类，经 M13 幂等跳过，零额外调用，3.13.4）。multi 扇出只改变批内信封基数、不改链结构（4.3 契约 ②a）：`counts.fanout` = classify 阶段执行前后 `len(batch)` 的差值，由 M10 在批链循环处计量（counts.* 所有权属 M10，与从 generate 返回值计 generated 同构）；`batch.end` 事件 payload 增 `fanout` 字段（7.2 只增；`batch.start.size` 语义 = 批入口信封数，即扇出前基数）；熔断交付的 unprocessed 残差公式右侧同步 `+ fanout`（6.4 不变量扩展）。`--dry-run` 估算（`_estimate`）增 `classify_calls`：process 模式 = ingested × max(1, self_consistency)，generate_only 模式 = 生成记录数 × max(1, self_consistency)（回流子批继承分类、不计入）；存在 `[class.*]` 覆盖或 `assignment="multi"` 时，quality/annotate/verify 估算按全局继承配置、multi 按标签乘数 1 报下界，并在 stderr 注明口径（1.6 v1.7 对齐决策 ⑦）。 |

**背书：**「编排器只做组合调度、算子无相互依赖」是 Data-Juicer 配方执行器 [4] 与 distilabel Pipeline 运行时 [5] 的共同架构；批式流转 + 增量写出与 Dolma toolkit 的并行分片处理模型一致 [6]。

### 3.10.4 运行走查示例

走查前提（贯穿示例·文本模态）：输入为输入法采集的中文指令 JSONL 共 1000 行、无坏行（首行 `{"instruction": "帮我写一条请假条，明天上午要去医院", "source": "ime-log", "ts": "2026-06-30T10:12:00Z"}`），`input.text_field = "instruction"`；`run.output = "./out/ime-intent-0630.jsonl"`；`run.batch_size = 256`、`run.seed = 0`；阶段为 2.3.1 的**默认组合**（`dedup.enabled` ✓、`quality.enabled` ✓、`annotate.enabled` ✓，`generate.enabled = false`、`verify.enabled = false`）。quality 取默认 `mode="pairwise"`、`rounds=4`、`criteria_per_call="all"`、`rubric="default:text"`（4 条 criteria，附录 A.1），并显式设 `quality.threshold = 0.3`；annotate 输出用户 Schema 为意图标注对象 `{intent, topic, difficulty}`（`additionalProperties:false`、三字段全 required）；`output.rejects = "refs"`（默认）、`dedup.scope = "global"`（默认）。M2 惰性流被 M10 切为 4 个批：256 / 256 / 256 / 232（尾批不足 batch_size 照常处理，3.10.3）。

| 批 | 取入→PipelineItem | M3 dropped_dup | M4 dropped_lowq | M5 failed | 写出 emitted | 批末释放的中间态 | DedupIndex 签名累计 |
|---|---|---|---|---|---|---|---|
| 1 | 256 | 18 | 21 | 3 | 214 | 256 个 PipelineItem；476 次裁决 / 1904 条比较结果 | 238 |
| 2 | 256 | 24 | 19 | 4 | 209 | 256 个 PipelineItem；464 次裁决 / 1856 条比较结果 | 470 |
| 3 | 256 | 27 | 20 | 5 | 204 | 256 个 PipelineItem；456 次裁决 / 1824 条比较结果 | 699 |
| 4（尾批） | 232 | 28 | 18 | 2 | 184 | 232 个 PipelineItem；408 次裁决 / 1632 条比较结果 | 903 |
| 合计 | 1000 | 97 | 78 | 14 | 811 | — | 903 |

口径与自洽性：每批 emitted = 取入 − dropped_dup − dropped_lowq − failed（批 1：256 − 18 − 21 − 3 = 214）。M4 比较池 N = 去重后存活数（批 1 为 238），裁决调用数 = k·⌊N/2⌋ = 4×119 = 476，`criteria_per_call="all"` 下每次调用裁决 4 条 criteria ⇒ 1904 条 criterion 级比较结果；批 3 的 N=229 为奇数，每轮末位轮空（3.4.3），故为 4×114 = 456 而非 458。进入 annotate 的记录数 = N − dropped_lowq：217 / 213 / 209 / 186（合计 825 = 811 emitted + 14 failed；14 条 failed 中 `schema_violation` 11 条、`provider_retryable_exhausted` 3 条）。批 1 quality 阶段的 `ctx.rng = Random("0:1:quality")`（3.10.3 派生式代入 run.seed=0、batch_no=1）。

批生命周期（图 3-4 的逐批实例）：每批 `emit()` 后 M11 向 `ime-intent-0630.jsonl.part` 追加 214 / 209 / 204 / 184 行并 flush，同时向 `ime-intent-0630.rejects.jsonl` 追加 42 / 47 / 52 / 48 行（= 该批 dup+lowq+failed，refs 模式每行仅 `_meta` 引用）；随后 M10 释放该批全部中间态——上表「批末释放」列的 PipelineItem 与比较结果，外加 4 组 BT log θ 数组（每 criterion 一组、长度 N）；文本模态无图像引用，释放数为 0。跨批存活的仅 3.10.3 所列三项，其规模变化：

| 跨批状态 | 批 1 → 2 → 3 → 4 末的规模 |
|---|---|
| ① DedupIndex | 签名条目 238 → 470 → 699 → 903（= 1000 − 97；每条目为 exact sha256 + 128-perm MinHash 签名，文本模态无 pHash 表）。注意被 lowq/failed 淘汰的记录也已入索引——M3 在先，first-writer-wins。 |
| ② MetricsSink 计数器 | emitted 214 → 423 → 627 → 811；dropped_dup 18 → 42 → 69 → 97；dropped_lowq 21 → 40 → 60 → 78；failed 3 → 7 → 12 → 14。仅整数计数，无数据内容。 |
| ③ M9 用量累计 | LLM 调用 693 → 1370 → 2035 → 2629（每批 = 裁决 + 标注调用，不含重试与 L3 修复）。 |

流耗尽后 `finalize()`：`.part` fsync 并原子改名为 `ime-intent-0630.jsonl`（811 行），写 `ime-intent-0630.report.json`，其 `counts` 节：

```
"counts": {"scanned": 1000, "ingested": 1000, "bad_input": 0,
           "dropped_dup": 97, "dropped_lowq": 78, "dropped_verify": 0,
           "failed": 14, "generated": 0, "emitted": 811}
```

按 6.4 不变量 `emitted + dropped_* + failed + bad_input = scanned + generated` 验算：811 + (97 + 78 + 0) + 14 + 0 = 1000 = 1000 + 0，等式成立。`run()` 返回的 `RunSummary` 与上述 counts 一致：4 批全部完成、主输出 811 行、rejects 189 行（97+78+14）、`interrupted = false`——CLI 以**退出码 0** 结束（2.4：存在被拒绝记录不影响退出码；若本次带 `--strict`，则因 189 条 rejects 返回退出码 1）。

**提示：**本走查中 dropped_lowq = 78 依赖显式设置 `quality.threshold = 0.3`；该键默认**缺省 = 不过滤只打分**（5.2），若不设阈值则 dropped_lowq = 0，903 条唯一记录将全部进入标注，emitted 相应变为 903 − failed。

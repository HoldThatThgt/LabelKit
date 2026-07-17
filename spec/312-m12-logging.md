## 3.12 M12 日志 logging

### 3.12.1 职责与边界

**做：**进程内**唯一**日志设施，承载两条通道：① **运行日志**——写 stderr，级别 debug/info/warn/error，行格式由 `tool.log_format = "text"`（默认）| `"jsonl"` 决定，只记运维事件，**绝不含数据内容与提示词**；② **trace 追踪日志**（可选，`trace.enabled=true` 时）——JSONL 文件（默认 `{output_stem}.trace.jsonl`），一行一事件的结构化事件流，供 rubric 优化（7.5）与标注质量分析。事件目录与格式规范见第 7 章。 
**不做：**不做跨运行聚合分析（下游 / 后续 `labelkit analyze` 工具职责，8.3 O5）；不上传任何遥测（2.1.2 边界⑦）；写失败**绝不中断运行**——首次失败 warn 一次并关闭该通道，此后事件丢弃并计入 `report.trace.dropped_events`；API Key 永不落日志（任一通道、任一脱敏档位）。进度显示（console，7.7）不属于日志——M12 仅以 3.12.3 的 ProgressListener 旁路向其**转发**（v1.10），不承载其渲染。

### 3.12.2 输入 / 输出

| 方向 | 内容 |
|---|---|
| 输入 | 各模块经标准 `logging` 记录器提交的运行日志；各 Stage 经 `EventLog.emit()` 提交的 TraceEvent；ResolvedConfig 中的 `tool.log_level / tool.log_format` 与 `[trace]` 节。 |
| 输出 | stderr 字节流；`trace.path` JSONL 文件（首行恒为 `run.start` header 事件）；report.json 的 `"trace"` 统计块（6.4）。 |

### 3.12.3 API 与数据结构

```
@dataclass(frozen=True)
class TraceEvent:
    ts: str                        # ISO8601，毫秒精度，含时区
    run_id: str                    # 本次运行标识：启动时 secrets.token_hex(6) 生成的随机 12 hex
    batch_no: int                  # 运行级事件（run.*）为 0
    stage: str                     # 发出事件的 stage 名；run.*/batch.* 固定 "run"
    ev: str                        # 事件名（7.2 事件目录）
    record_ids: tuple[str, ...]    # 涉及的记录 id（0/1/2 条）
    payload: Mapping               # 事件负载：7.2 逐事件定义，经 7.4 脱敏

class EventLog:
    def emit(self, ev: TraceEvent) -> None:
        """行缓冲写入 trace 文件；通道未启用或已因写失败关闭时为 no-op（调用方无需判断）。"""
```

接入方式：`EventLog` 由 `MetricsSink` 持有并转发——各 Stage 通过既有的 `RunContext.metrics`（3.10.3）发事件，**不改 RunContext 签名**。stderr 侧直接使用标准 `logging` 模块，handler 由 M12 在启动时按 `log_format` 安装。

**v1.10 增：ProgressListener 进程内旁路**（console 面板的唯一数据通路，7.7；实现归 CLI 层 `labelkit/cli/console.py`，协议归本层——依赖方向 `cli → orchestration → operators → common` 不变）：

```
class ProgressListener(Protocol):        # v1.10（7.7 console 的订阅协议，五回调）
    def on_run_context(self, cfg, snapshot, counters, fatal_streak) -> None: ...
        # execute_run 装配完成后、asyncio.run 之前调用一次（U19）：cfg = ResolvedConfig；
        # snapshot = LLMClient.snapshot（3.9.2）；counters / fatal_streak = MetricsSink 只读闭包。
        # 渲染器以「惰性壳」形态传入（CLI 在 load 前无 cfg），本回调完成激活。
    def on_estimate(self, est: Mapping) -> None: ...
        # M10 预扫后经 MetricsSink.run_estimate 转发的 estimate_run() 静态估算（3.10.3）；
        # 文本模态未开 console.estimate 时不发（U17）。
    def on_event(self, ev: TraceEvent) -> None: ...
        # MetricsSink.event() 旁路转发；payload 经 redact_payload(payload, "none") 预脱敏（U22）——
        # 无 LLM 自由文本、无输入内容，U6 红线由机制保证；record_ids 保留（结构字段）。
    def on_stage(self, stage: str, batch_no: int) -> None: ...
        # M10 stage 循环经 MetricsSink.stage_begin 转发（每 stage run() 之前一次）。
    def on_stop_requested(self) -> None: ...
        # SIGINT/SIGTERM 经 MetricsSink.stop_requested 转发（优雅中断横幅，3.10.3）。
```

四条纪律：① 旁路**不属于 trace 面**——五回调均不产生 TraceEvent、不受 `trace.channels` 过滤（7.2 事件目录零改动的充分条件）；`on_event` 的 payload 按 none 档预脱敏后转发（仅 listener 非 None 时执行，浅递归 strip 成本可忽略——U22）。② 全部回调必须 O(1)、无 I/O、无锁等待——重绘由消费方自己的节流 tick 驱动（渲染与事件源解耦）。③ **sink 侧异常防护（U23）**：MetricsSink 每次转发 `try/except Exception`——首次异常打一条 WARN 并置 listener 为 None（EventLog 写失败「warn 一次 + 关通道」同款纪律，3.12.4），listener 异常永不进入记录级/批级失败路径。④ `listener = None`（validate / 全部既有调用路径）时行为与 v1.9 逐字节一致。

API 增量（均只增）：`MetricsSink.__init__` 增可选尾参 `listener`；增仅转发方法 `stage_begin(stage, batch_no)` / `run_estimate(est)` / `stop_requested()` 与两只读属性 `fatal_streak`（熔断行数据源）、`has_listener`（旁路在挂探针——M10 dry-run 让位门读之；U23 跳闸后永久 False）。plain 行格式（进度行/终版摘要）下沉为本层纯函数模块 `labelkit/common/observability/console_format.py`（U21——emitter 与 CLI 渲染器共用的单一事实源，v1.9 字符串逐字节钉死）。配套的 `LLMClient.snapshot()` 只读快照（密钥池三态 + 逐密钥用量镜像 + p50 有界样本窗）规格见 3.9.2；全部签名已入册于 CONTRACTS §7.8/§7.11/§7.12（签名）与 §7.9/§7.10/§8.4（行为与措辞）。

### 3.12.4 行为规格

| 机制 | 定义 |
|---|---|
| 通道过滤 | 事件按 7.2 归属通道，不在 `trace.channels` 中的事件不写；`run.* / batch.*` 生命周期事件不受过滤。 |
| schema 版本 | `trace_schema_version = 1` 只写在文件首行 `run.start` header 事件的 payload 中（避免每行冗余）；事件目录即稳定契约，后续版本只增不改。 |
| flush | 行缓冲；每批随 M11 flush（3.11.2）同步 flush——保证主输出已落盘的行，其 trace 事件必已落盘。 |
| 写失败 | 首次 `OSError` ⇒ stderr warn 一次 + 关闭该通道 + 后续事件计入 `report.trace.dropped_events`；运行绝不因日志中断。 |
| run_id | 启动时生成、写入本次运行全部事件；用于多次运行的 trace 文件合并分析时区分来源。 |
| 文件语义 | **首个事件写出时**若 `trace.path` 已存在则截断覆盖并 stderr warn 一次（v1.5 惰性打开：构造不碰文件，死于配置/输入校验的运行不触碰旧 trace；保留历史请改名或另配 trace.path）。trace 不做 .part 原子改名——它是逐批 flush 的流式日志，异常终止时已 flush 的行即有效前缀（首行仍为本次 run.start）。 |

### 3.12.5 配置项

见 5.1 `tool.log_format` 与 5.2 `[trace]` 节、`quality.judgment_reasons`。

**背书：**对每次 LLM 调用与判定做结构化追踪（trace）并以其驱动评测迭代，是 LLM 工程的工业标准形态：LangSmith（LangChain）[28] 与 W&B Weave [29] 均以「逐步 trace LLM 调用 + 数据集评测」为核心能力。`llm.call` 等事件的字段命名对齐 OpenTelemetry GenAI 语义约定 [27]——该约定截至 2026-07 处于 Development（实验性、非 stable）状态，本工具仅做命名对齐、不依赖其 SDK 实现（7.3）。

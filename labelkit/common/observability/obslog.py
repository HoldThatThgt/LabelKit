"""M12——可观测性（spec ch.7、3.12）。

两条互相独立的通道：

1. stderr 运行日志——标准库 ``logging``，记录器 ``labelkit``；text | jsonl 两种行格式，
   见 CONTRACTS.md §8.4。**绝不**包含数据内容、prompt 或 API 密钥。
2. trace 事件日志——按需开启的 JSONL 文件（``trace.path``），每行一个
   :class:`TraceEvent`，行缓冲，与 M11 同步按批 flush。载荷按 ``trace.content``
   四档脱敏（§8.3）。

写失败永不打断运行：首个 OSError 在 stderr 上告警一次、关闭通道，此后每个事件都计入
``report.trace.dropped_events``。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime
from typing import IO, TYPE_CHECKING, Callable, Mapping, Protocol

from labelkit.common.config.model import ResolvedConfig, TraceConfig

if TYPE_CHECKING:  # v1.10：字符串注解切断 obslog↔llm_client 的循环依赖（§3.3）
    from labelkit.common.inference.llm_client import ProfileSnapshot

# ── 事件名常量（§7.11，字符串精确）──────────────────────────────────────────

EV_RUN_START = "run.start"
EV_RUN_END = "run.end"
EV_BATCH_START = "batch.start"
EV_BATCH_END = "batch.end"
EV_INGEST_BAD_LINE = "ingest.bad_line"
EV_INGEST_MISSING_PAIR = "ingest.missing_pair"
EV_INGEST_INDEX_CONFLICT = "ingest.index_conflict"
EV_INGEST_DISORDER = "ingest.disorder"       # v1.8 M2 流单调性（spec 7.2）
EV_SEGMENT_SESSION = "segment.session"       # v1.8 M2 会话关闭（spec 7.2）；仅 trace
EV_SEGMENT_BOUNDARY = "segment.boundary"     # v1.8 M14 窗口判决（spec 7.2）；仅 trace
EV_STITCH_JUDGE = "stitch.judge"             # v1.9 M16 候选判决（spec 7.2）；仅 trace
EV_STITCH_THREAD = "stitch.thread"           # v1.9 M16 碎片跨度表（spec 7.2）；仅 trace
EV_DEDUP_DUPLICATE = "dedup.duplicate"
EV_CLASSIFY_DECISION = "classify.decision"   # v1.7 M13（spec 7.2）；仅 trace，R29
EV_CLASSIFY_FRAME = "classify.frame"         # v1.12 M13 帧级批量判决（spec 7.2）; trace-only
EV_EXTRACT_STEP = "extract.step"             # v1.8 M15（spec 7.2）；仅 trace，S27
EV_QUALITY_JUDGMENT = "quality.judgment"
EV_QUALITY_POINTWISE = "quality.pointwise"
EV_QUALITY_BT_FIT = "quality.bt_fit"
EV_QUALITY_GATE = "quality.gate"
EV_ANNOTATE_DONE = "annotate.done"
EV_ANNOTATE_FRAME = "annotate.frame"         # v1.12 M5 帧级逐帧标注（spec 7.2）; trace-only
EV_VERIFY_VERDICT = "verify.verdict"
EV_SCHEMA_REPAIR = "schema.repair"
EV_LLM_CALL = "llm.call"
EV_LLM_KEY_COOLDOWN = "llm.key_cooldown"     # v1.6 密钥池（spec 7.2）
EV_LLM_KEY_DISABLED = "llm.key_disabled"     # v1.6
EV_LLM_POOL_PARKED = "llm.pool_parked"       # v1.6
EV_ERROR = "error"

TRACE_SCHEMA_VERSION = 1

_logger = logging.getLogger("labelkit.obslog")

_RUNTIME_HIGH_WATER_KEYS = frozenset({
    "queue", "running", "resource_wait", "commit_waiting", "candidate_bytes",
})
_RUNTIME_TOTAL_KEYS = frozenset({
    "cancelled_tasks", "resource_wait_ms", "http_pool_wait_ms", "commit_ms",
})
_REALTIME_COUNTER_PREFIXES = (
    "budget.", "llm.", "embedding.", "schema.", "provider.", "resource.", "origin.", "runtime.",
    "generate.sequence.calls.",
)


def _non_negative_int(value: object) -> bool:
    """判断运行观测值是否为非负整数。

    @param value 待检查值
    @return 是否为非负整数且非 bool
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# ── TraceEvent ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TraceEvent:
    """trace 通道的一行；字段顺序即 JSONL 写出顺序（§8.1）。"""

    ts: str                        # ISO8601 毫秒精度，带时区偏移
    run_id: str                    # secrets.token_hex(6)——每次运行 12 个十六进制字符
    batch_no: int                  # 运行级事件为 0
    stage: str                     # 发出事件的阶段名；run.*/batch.* 用 "run"
    ev: str                        # 事件名（§8.1）
    record_ids: tuple[str, ...]    # 0/1/2 个记录 id
    payload: Mapping               # 各事件自有字段（§8.1），按 trace.content 脱敏（§8.3）


# ── 脱敏（§8.3）────────────────────────────────────────────────────────────

# LLM 产出的自由文本，在 "none" 档丢弃。v1.8（S27、§8.3）新增：
# + "description"（extract.step 的 LLM 文本，与 reason/critiques 同档）与
# + "defects"（verify.verdict 的流缺陷表在 `detail` 里带 LLM 自由文本——在 "none"
#   档整键丢弃，与 critiques 同级）。
# v1.9（T16）新增："task_name"（stitch.judge/stitch.thread 的滚动线索任务名——
#   LLM 自由文本，与 reason 同档）。
_FREE_TEXT_KEYS = frozenset({"reason", "critiques", "violations",
                             "description", "defects", "task_name"})
# v1.8（S27、§8.3）：**源自输入数据**的载荷字段（extract.step 引用的控件文本 /
# 键入文本）——在 "none" 与 "refs" 两档都剥除（refs 档「不含输入数据内容」的红线），
# 语义承自 "excerpt"。
_DATA_KEYS = frozenset({"target", "value"})
# 完整的 prompt/response 消息，仅在 "full" 档出现。
_MESSAGE_KEYS = frozenset({"gen_ai.input.messages", "gen_ai.output.messages"})
_EXCERPT_MAX_CHARS = 200


def _strip(value, drop: frozenset[str] | set[str]):
    """递归剥除嵌套映射/序列中的 ``drop`` 键。

    @param value 任意载荷片段（映射、序列或标量）
    @param drop 待剥除的键名集合
    @return 剥除后的新对象（原对象绝不被改写）
    """
    if isinstance(value, Mapping):
        return {k: _strip(v, drop) for k, v in value.items() if k not in drop}
    if isinstance(value, (list, tuple)):
        return [_strip(v, drop) for v in value]
    return value


def redact_payload(payload: Mapping, content: str) -> Mapping:
    """按 trace.content 档位（§8.3）脱敏一个事件载荷。

    - "none"：   只留 id/枚举/数字——reason/critiques/violations/description/defects
                 丢弃，无 excerpt，无 target/value，无 gen_ai 消息。
    - "refs"：   额外保留 LLM 产出文本（reason/critiques/violations/description/
                 defects）；仍然不含输入数据内容（无 excerpt、无 target/value
                 ——_DATA_KEYS，v1.8 S27——、无 gen_ai 消息）。
    - "excerpt"：额外保留 ``excerpt`` 字段，每个值截到前 200 个字符；
                 额外保留 _DATA_KEYS 字段（target/value）。
    - "full"：   额外保留 gen_ai.input.messages / gen_ai.output.messages 原文。
                 档位逐档递增——"full" 同样保留（已截断的）excerpt。

    @param payload 未脱敏的原始载荷
    @param content 档位："none" | "refs" | "excerpt" | "full"
    @return 脱敏后的新载荷（原载荷对象绝不被改写）
    """
    drop: set[str] = set()
    if content != "full":
        drop |= _MESSAGE_KEYS
    if content in ("none", "refs"):
        drop.add("excerpt")
        # v1.8（S27、§8.3）：源自输入数据的字段绝不下漏到 excerpt 档以下——
        # refs 档携带 LLM 文本，但不含任何输入内容。
        drop |= _DATA_KEYS
    if content == "none":
        drop |= _FREE_TEXT_KEYS
    out = _strip(payload, drop)
    # 档位逐档递增（spec 7.4「逐档递增」）："full" 包含 "excerpt" 的一切，
    # 故 200 字符截断在两档都生效。
    if content in ("excerpt", "full") and isinstance(out.get("excerpt"), Mapping):
        out["excerpt"] = {
            rid: (text[:_EXCERPT_MAX_CHARS] if isinstance(text, str) else text)
            for rid, text in out["excerpt"].items()
        }
    return out


# ── ProgressListener（v1.10 console 旁路，spec 3.12.3 / §7.7，U19）─────────

class ProgressListener(Protocol):
    """进程内进度旁路——console 面板的唯一数据通路（spec 3.12.3，SPEC-tui-console
    U19）；协议归 common 层，实现归 CLI 层（labelkit/cli/console.py 惰性壳）。

    四条纪律（spec 3.12.3）：
    ① 旁路**不属于 trace 面**——五回调均不产生 TraceEvent、不受 trace.channels
       过滤（§7.2 事件目录零改动的充分条件）；on_event 的 payload 经
       redact_payload(payload, "none") 预脱敏后转发（U22——无 LLM 自由文本、
       无输入内容，U6 红线由机制保证；record_ids 保留为结构字段）。
    ② 全部回调必须 O(1)、无 I/O、无锁等待——重绘由实现方自己的节流 tick 驱动
       （渲染与事件源解耦）。
    ③ sink 侧异常防护（U23）：MetricsSink 每次转发 try/except Exception——首次
       异常打一条 WARN 并置 listener 为 None（EventLog 写失败「warn 一次 + 关
       通道」同款纪律，3.12.4），listener 异常永不进入记录级/批级失败路径。
    ④ listener = None（validate / 全部既有调用路径）时行为与 v1.9 逐字节一致。
    """

    def on_run_context(self, cfg: ResolvedConfig,
                       snapshot: "Callable[[], tuple[ProfileSnapshot, ...]]",
                       counters: "Callable[[], Mapping[str, int]]",
                       fatal_streak: "Callable[[], int]") -> None:
        """execute_run 装配完成后、asyncio.run 之前调用一次（U19）：cfg =
        ResolvedConfig；snapshot = LLMClient.snapshot（spec 3.9.2）；counters /
        fatal_streak = MetricsSink 只读闭包。渲染器以「惰性壳」形态传入（CLI 在
        load 前无 cfg），本回调完成激活。

        @param cfg 完整运行配置。
        @param snapshot LLM profile 快照闭包。
        @param counters 运行计数只读闭包。
        @param fatal_streak 连续 fatal 次数闭包。
        @return None。
        """
        ...

    def on_estimate(self, est: Mapping) -> None:
        """M10 预扫后经 MetricsSink.run_estimate 转发的 estimate_run() 静态估算
        （spec 3.10.3）；文本模态未开 console.estimate 时不发（U17）。

        @param est 静态调用量估算。
        @return None。
        """
        ...

    def on_event(self, ev: TraceEvent) -> None:
        """MetricsSink.event() 旁路转发；payload 已经 redact_payload(payload,
        "none") 预脱敏（U22）。

        @param ev 已预脱敏事件。
        @return None。
        """
        ...

    def on_stage(self, stage: str, batch_no: int) -> None:
        """M10 stage 循环经 MetricsSink.stage_begin 转发（每 stage run() 之前
        一次，U11）。

        @param stage 当前阶段名。
        @param batch_no 当前批次号。
        @return None。
        """
        ...

    def on_stop_requested(self) -> None:
        """SIGINT/SIGTERM 经 MetricsSink.stop_requested 转发（优雅中断横幅，
        spec 3.10.3）。

        @return None。
        """
        ...


# ── EventLog（trace 通道）──────────────────────────────────────────────────

class EventLog:
    """JSONL trace 写出器。绝不向调用方抛异常；写失败告警一次、关闭通道，
    此后事件只计入 dropped。"""

    def __init__(self, cfg: TraceConfig, run_id: str):
        """构造 trace 写出器（**不**在此打开文件，见下方 P2-4 说明）。

        @param cfg 已解析的 [trace] 配置
        @param run_id 本次运行的 12 位十六进制标识
        """
        self.cfg = cfg
        self.run_id = run_id
        self.dropped_events: int = 0
        self.events_written: int = 0
        self._fh: IO[str] | None = None
        self._closed = False           # 因写失败而关闭
        self._opened = False           # 惰性打开：文件在**首个** emit 时才 touch
        self._channels = frozenset(cfg.channels)
        # 这里刻意**不**打开 trace 文件（E2E 发现 P2-4）：打开（并截断上次运行的
        # 文件）推迟到首个事件发出时，这样在 run.start 之前死于配置/输入校验的运行
        # 绝不会毁掉上次运行的 trace。

    def _open(self) -> None:
        """惰性打开 trace 文件；打开失败按 warn-once 纪律停用通道，绝不抛出。"""
        self._opened = True
        self._guard_io(self._open_handle)

    # 内部 -------------------------------------------------------------------

    def _open_handle(self) -> None:
        """真正执行打开：旧文件存在则先告警再截断；buffering=1 → 行缓冲文本流。"""
        if self.cfg.path and os.path.exists(self.cfg.path):
            _logger.warning(
                "trace file %s already exists — truncating (rename it or set "
                "trace.path to keep history)", self.cfg.path,
                extra={"stage": "run", "batch": 0},
            )
        self._fh = open(self.cfg.path, "w", encoding="utf-8", buffering=1)

    def _guard_io(self, action: Callable[[], None]) -> bool:
        """执行一次 trace 文件 I/O（打开/写/刷新），承载唯一的写失败处置分支。

        warn-once 纪律（3.12.4）：首个 OSError 在 stderr 上告警一次并关闭通道，
        之后同类失败只关句柄不再刷屏——调用方据返回值把事件计入 dropped。

        @param action 无参 I/O 动作（打开、写一行或刷新）
        @return True = 动作成功；False = 发生 OSError，通道已停用
        """
        try:
            action()
        except OSError as exc:
            if not self._closed:
                self._closed = True
                _logger.warning(
                    "trace channel disabled after write failure: %s — subsequent "
                    "events are dropped and counted", exc,
                    extra={"stage": "run", "batch": 0},
                )
            self._close_handle()
            return False
        return True

    def _close_handle(self) -> None:
        """关闭文件句柄并置空；关闭失败只告警不抛——trace 通道永不打断运行。"""
        if self._fh is None:
            return
        try:
            self._fh.close()
        except OSError as exc:
            _logger.error("trace file close failed: %s", exc,
                          extra={"stage": "run", "batch": 0})
        self._fh = None

    def _channel(self, ev: TraceEvent) -> str:
        """求事件所属通道。

        通道 = 事件名首个 '.' 之前的前缀，**唯独** ev == "error" 例外——它归属于
        产生它的 stage（spec 7.2）。

        @param ev 待归属的事件
        @return 通道名
        """
        if ev.ev == EV_ERROR:
            return ev.stage
        return ev.ev.split(".", 1)[0]

    def _passes_filter(self, ev: TraceEvent) -> bool:
        """判断事件是否通过 trace.channels 订阅过滤。

        @param ev 待判断的事件
        @return True = 应当写出；生命周期事件（run/batch）恒为 True
        """
        channel = self._channel(ev)
        if channel in ("run", "batch"):    # 生命周期事件绕过过滤
            return True
        return channel in self._channels

    # 公开 -------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        """通道是否已被写失败关闭（3.12.4）。

        ProcessWorkflow 组装 ``report.trace`` 时读取它，以便为终局的 ``run.end``
        事件正确记账——该事件在 report 构建之后才发出（§9.3）。

        @return True = 通道已因写失败关闭
        """
        return self._closed

    def emit(self, ev: TraceEvent) -> None:
        """行缓冲 JSONL 写出。

        通道未开启、被过滤掉、或已因写失败关闭时静默跳过（调用方从不判断）。

        @param ev 待写出的事件（载荷未脱敏，由本方法按 trace.content 处理）
        """
        if not self.cfg.enabled:
            return
        if not self._passes_filter(ev):
            return
        if not self._opened and not self._closed:
            self._open()
        if self._closed or self._fh is None:
            self.dropped_events += 1
            return
        payload = redact_payload(ev.payload, self.cfg.content)
        if ev.ev == EV_RUN_START and "trace_schema_version" not in payload:
            payload = {**payload, "trace_schema_version": TRACE_SCHEMA_VERSION}
        line = json.dumps(
            {
                "ts": ev.ts,
                "run_id": ev.run_id,
                "batch_no": ev.batch_no,
                "stage": ev.stage,
                "ev": ev.ev,
                "record_ids": list(ev.record_ids),
                "payload": payload,
            },
            ensure_ascii=False,
        )
        if not self._guard_io(lambda: self._fh.write(line + "\n")):
            self.dropped_events += 1
            return
        self.events_written += 1

    def flush(self) -> None:
        """把行缓冲刷到磁盘；刷新失败按 warn-once 纪律停用通道，绝不抛出。"""
        if self._fh is None:
            return
        self._guard_io(self._fh.flush)

    def close(self) -> None:
        """关闭 trace 文件句柄（幂等）。"""
        self._close_handle()


# ── MetricsSink ────────────────────────────────────────────────────────────

# 各事件的 stderr 镜像级别，见 §8.1（标注 "—" 的事件仅入 trace，永不镜像）。
_STDERR_LEVELS: dict[str, int] = {
    EV_RUN_START: logging.INFO,
    EV_RUN_END: logging.INFO,
    EV_BATCH_START: logging.DEBUG,
    EV_BATCH_END: logging.INFO,
    EV_INGEST_BAD_LINE: logging.WARNING,
    EV_INGEST_MISSING_PAIR: logging.WARNING,
    # EV_INGEST_INDEX_CONFLICT：镜像为 warn，但 input.on_index_conflict="fail" 时为
    # error（spec 7.2 / CONTRACTS §8.1）——在 _mirror() 里动态判定。
    # v1.8 的 ingest.disorder 在此**仅入 trace**（D1）：它的 reason 内嵌时间戳/游标
    # 取值且**每条记录**都会触发——镜像既会打破「每次运行一条 stderr WARN」的契约，
    # 又会在时间戳字段系统性写错时把输入派生值刷满 stderr。M2 自己每次运行打那条
    # 唯一的、无数据的 WARN（spec 7.2）；fail 策略经 InputError 浮现（退出码 3）。
    # segment/extract 三个事件（segment.session / segment.boundary / extract.step）
    # 同样仅入 trace（"—"，§8.1），不进本表。
    EV_LLM_CALL: logging.DEBUG,
    # v1.6 密钥池事件（spec 7.2）：key_cooldown 仅入 trace（"—"），
    # key_disabled / pool_parked 镜像为 warn。
    EV_LLM_KEY_DISABLED: logging.WARNING,
    EV_LLM_POOL_PARKED: logging.WARNING,
    # EV_ERROR：warn（记录级）/ error（运行级）——在 event() 里判定
}


class MetricsSink:
    """持有 EventLog 与运行计数器。所有阶段都经 RunContext.metrics 发出埋点。

    v1.10（spec 3.12.3，U19/U22/U23）：可选携带一个 ProgressListener——console
    面板的进程内旁路。转发**不**产生 TraceEvent（§7.2 目录零改动）；on_event 的
    载荷在 "none" 档预脱敏；每次转发都有异常防护（首次失败告警一次并永久停用
    旁路）；listener=None 时与 v1.9 逐字节一致。"""

    def __init__(self, cfg: ResolvedConfig, run_id: str, event_log: EventLog,
                 listener: ProgressListener | None = None):
        """装配计数器汇。

        @param cfg 本次运行的解析配置
        @param run_id 本次运行的 12 位十六进制标识
        @param event_log trace 写出器
        @param listener v1.10 console 旁路监听器；None = 无旁路（默认）
        """
        self.cfg = cfg
        self.run_id = run_id
        self.event_log = event_log
        self.counters: dict[str, int] = {}
        self.stage_times: dict[str, float] = {}
        self._captured_counts: ContextVar[dict[str, int] | None] = ContextVar(
            f"labelkit_metrics_capture_{run_id}", default=None,
        )
        self._runtime_high_water = {key: 0 for key in _RUNTIME_HIGH_WATER_KEYS}
        self._runtime_totals = {key: 0 for key in _RUNTIME_TOTAL_KEYS}
        self._fatal_streak = 0
        self._circuit_broken = False
        self._listener: ProgressListener | None = listener

    def _forward(self, callback: str, *args) -> None:
        """U23 防护——通往 listener 的**唯一**通路：每次转发都套 try/except；
        首个异常打一条 WARN 并把 listener 引用置 None，本次运行余下时间不再转发
        （EventLog 写失败「warn 一次 + 关通道」同款纪律，spec 3.12.4）。listener
        的缺陷永不进入记录级/批级失败路径。

        @param callback 回调方法名
        @param args 透传给回调的实参
        """
        listener = self._listener
        if listener is None:
            return
        try:
            getattr(listener, callback)(*args)
        except Exception as exc:  # noqa: BLE001 —— 旁路隔离（U23）
            self._listener = None
            _logger.warning(
                "console listener failed, panel bypass disabled: %s", exc,
                extra={"stage": "run", "batch": 0},
            )

    def stage_begin(self, stage: str, batch_no: int) -> None:
        """v1.10 纯转发（spec 3.12.3，U11/U19）：M10 在每次 stage.run() 之前调用；
        转发 on_stage——**不**产生 TraceEvent，永不进入 §7.2 目录。listener 为
        None 时是空操作。

        @param stage 即将运行的阶段名
        @param batch_no 批次序号（从 1 开始）
        """
        self._forward("on_stage", stage, batch_no)

    def run_estimate(self, est: Mapping) -> None:
        """v1.10 纯转发（spec 3.12.3，U19/U20）：把 estimate_run() 的静态估算转给
        on_estimate。listener 为 None 时是空操作。

        @param est estimate_run() 的估算结果
        """
        self._forward("on_estimate", est)

    def stop_requested(self) -> None:
        """v1.10 纯转发（spec 3.12.3，U19）：SIGINT/SIGTERM 优雅中断信号 →
        on_stop_requested（中断横幅，spec 3.10.3）。listener 为 None 时是空操作。"""
        self._forward("on_stop_requested")

    @property
    def fatal_streak(self) -> int:
        """v1.10（spec 3.12.3，U19）：熔断连续计数的只读视图——console 面板熔断行
        的数据源（§7.7 LLM 块）。

        @return 当前连续致命错误数
        """
        return self._fatal_streak

    @property
    def has_listener(self) -> bool:
        """v1.10（U13）：旁路是否挂接的只读探针。

        M10 的 dry-run rich 让位门读它（估算打印行只有在确实挂了 listener 时才
        让位给渲染器表格）；U23 转发失败跳闸后永久变为 False。

        @return True = 旁路仍然挂接
        """
        return self._listener is not None

    def event(self, ev: str, *, stage: str, batch_no: int,
              record_ids: tuple[str, ...] = (), payload: Mapping | None = None) -> None:
        """构造 TraceEvent（ts = 本地当前时间 ISO8601 毫秒，run_id）并转给 EventLog；
        §8.1 定义了级别的事件同时镜像到 stderr 记录器。v1.10：额外转发给
        ProgressListener 旁路——用**第二个** TraceEvent，其载荷在 "none" 档预脱敏
        （U22）；交给 EventLog 的那个仍是未脱敏的（EventLog 在写出时按自己的
        trace.content 档位处理）。

        @param ev 事件名（§8.1）
        @param stage 发出事件的阶段名
        @param batch_no 批次序号；运行级事件用 0
        @param record_ids 相关记录 id（0/1/2 个）
        @param payload 事件自有字段；None 视作 {}
        """
        payload = payload or {}
        trace_ev = TraceEvent(
            ts=datetime.now().astimezone().isoformat(timespec="milliseconds"),
            run_id=self.run_id,
            batch_no=batch_no,
            stage=stage,
            ev=ev,
            record_ids=tuple(record_ids),
            payload=payload,
        )
        self.event_log.emit(trace_ev)
        self._mirror(ev, stage, batch_no, payload)
        if self._listener is not None:
            self._forward("on_event",
                          replace(trace_ev, payload=redact_payload(payload, "none")))

    def _mirror(self, ev: str, stage: str, batch_no: int, payload: Mapping) -> None:
        """把事件按 §8.1 级别镜像成一行 stderr 运行日志（无级别定义则不镜像）。

        @param ev 事件名
        @param stage 发出事件的阶段名
        @param batch_no 批次序号
        @param payload 事件载荷（只取标量字段入行）
        """
        if ev == EV_ERROR:
            # 运行级 provider_fatal → error；记录级 → warn（§8.1）
            level = logging.ERROR if payload.get("kind") == "provider_fatal" else logging.WARNING
        elif ev == EV_INGEST_INDEX_CONFLICT:
            # spec 7.2：镜像为 warn，但 fail 策略下为 error（§8.1）
            level = (logging.ERROR if self.cfg.input.on_index_conflict == "fail"
                     else logging.WARNING)
        else:
            level = _STDERR_LEVELS.get(ev)
            if level is None:
                return
        # 只写运行态摘要：载荷里的标量字段；绝不写嵌套内容（counts 对象、判决、
        # 消息……）。既无数据内容，也无 prompt。被镜像事件里的标量都是结构性的
        # （例如 ingest.bad_line 的跳过原因枚举，spec 7.3 规范样例）——LLM 自由文本
        # 只出现在不镜像的事件里，或以嵌套列表形式出现，而后者已被 isinstance 过滤挡下。
        parts = [
            f"{k}={v}" for k, v in payload.items()
            if isinstance(v, (str, int, float, bool))
        ]
        msg = ev if not parts else ev + " " + " ".join(parts)
        logging.getLogger("labelkit." + (stage or "run")).log(
            level, msg, extra={"stage": stage, "batch": batch_no},
        )

    def count(self, key: str, n: int = 1) -> None:
        """累加一个运行计数器。

        @param key 计数器键（report.json 的计数来源）
        @param n 增量，默认 1
        """
        captured = self._captured_counts.get()
        realtime = key.startswith(_REALTIME_COUNTER_PREFIXES)
        target = captured if captured is not None and not realtime else self.counters
        target[key] = target.get(key, 0) + n

    @contextmanager
    def capture_counts(self):
        """暂存一个 sequence attempt 内产生的 dataset counters。

        事件、LLM 用量、Schema 统计、熔断状态与 ``budget.*`` 运行事实继续实时写入；
        其余经 :meth:`count` 写入的 dataset counters 被暂存，交付控制器在 group
        commit 后显式合并。嵌套捕获会破坏归属边界，因此 fail closed。

        @return 当前捕获区的可变计数表。
        @raises RuntimeError 已存在活动捕获区。
        """
        if self._captured_counts.get() is not None:
            _logger.error("metrics count capture is already active")
            raise RuntimeError("metrics count capture is already active")
        captured: dict[str, int] = {}
        token = self._captured_counts.set(captured)
        try:
            yield captured
        finally:
            self._captured_counts.reset(token)

    def merge_counts(self, counters: Mapping[str, int]) -> None:
        """在 sequence group commit 后一次合并 attempt-local counters。

        @param counters 已接受 attempt 的局部计数增量。
        @return None。
        @raises RuntimeError 捕获区仍活动，或增量不是非负整数。
        """
        if self._captured_counts.get() is not None:
            _logger.error("cannot merge metrics while count capture is active")
            raise RuntimeError("cannot merge metrics while count capture is active")
        for key, value in counters.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                _logger.error("metrics counter delta must be a non-negative integer")
                raise RuntimeError("metrics counter delta must be a non-negative integer")
            self.counters[key] = self.counters.get(key, 0) + value

    def observe_runtime_high_water(self, key: str, value: int) -> None:
        """更新一个运行期高水位。

        @param key queue、running、resource_wait、commit_waiting 或 candidate_bytes
        @param value 当前非负观测值
        @raises RuntimeError 键未知或值不是非负整数
        """
        if key not in _RUNTIME_HIGH_WATER_KEYS or not _non_negative_int(value):
            _logger.error("invalid runtime high-water observation")
            raise RuntimeError("invalid runtime high-water observation")
        self._runtime_high_water[key] = max(self._runtime_high_water[key], value)

    def add_runtime_total(self, key: str, value: int) -> None:
        """累加一个运行期整数总量。

        @param key cancelled_tasks、resource_wait_ms、http_pool_wait_ms 或 commit_ms
        @param value 非负增量
        @raises RuntimeError 键未知或值不是非负整数
        """
        if key not in _RUNTIME_TOTAL_KEYS or not _non_negative_int(value):
            _logger.error("invalid runtime total observation")
            raise RuntimeError("invalid runtime total observation")
        self._runtime_totals[key] += value

    @property
    def runtime_report(self) -> Mapping[str, int]:
        """返回成功与失败报告共用的精确九字段运行节点。

        @return 独立且只含整数的运行指标映射
        """
        return {
            "queue_high_water": self._runtime_high_water["queue"],
            "running_high_water": self._runtime_high_water["running"],
            "resource_wait_high_water": self._runtime_high_water["resource_wait"],
            "commit_waiting_high_water": self._runtime_high_water["commit_waiting"],
            "candidate_bytes_high_water": self._runtime_high_water["candidate_bytes"],
            "cancelled_tasks": self._runtime_totals["cancelled_tasks"],
            "resource_wait_ms": self._runtime_totals["resource_wait_ms"],
            "http_pool_wait_ms": self._runtime_totals["http_pool_wait_ms"],
            "commit_ms": self._runtime_totals["commit_ms"],
        }

    def add_stage_time(self, stage: str, seconds: float) -> None:
        """累加某阶段的累计耗时。

        @param stage 阶段名
        @param seconds 本次耗时（秒）
        """
        self.stage_times[stage] = self.stage_times.get(stage, 0.0) + seconds

    def record_provider_result(self, fatal: bool, *, hard: bool = False) -> None:
        """给熔断器喂一次 provider 结果。

        ``hard=True``（鉴权类 401/403 致命错误）立即打开熔断——凭证/权限失败不会
        自愈，继续攒连续计数只是白烧钱（spec 3.9.3）。

        @param fatal 本次是否为致命错误（False 会清空连续计数）
        @param hard 是否鉴权类硬致命，True 则立即熔断
        """
        if fatal:
            self._fatal_streak += 1
            if hard or self._fatal_streak >= self.cfg.run.fatal_error_threshold:
                self._circuit_broken = True
        else:
            self._fatal_streak = 0

    @property
    def circuit_broken(self) -> bool:
        """熔断器是否已打开。

        @return True = 已熔断（M9 据此拒发新调用，M10 以退出码 4 收尾）
        """
        return self._circuit_broken

    def flush(self) -> None:
        """按批把 trace 通道刷到磁盘（与 M11 同步）。"""
        self.event_log.flush()


# ── stderr 运行日志（§8.4）─────────────────────────────────────────────────

_TEXT_LEVEL_NAMES = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARN",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "ERROR",
}
_JSONL_LEVEL_NAMES = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}
_LOG_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO,
               "warn": logging.WARNING, "error": logging.ERROR}


def _record_ts(record: logging.LogRecord) -> str:
    """把日志记录的创建时刻渲染成带时区的 ISO8601 秒级时间戳。

    @param record 标准库日志记录
    @return 形如 "2026-08-14T01:41:07+08:00" 的时间戳
    """
    return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds")


class _TextFormatter(logging.Formatter):
    """文本行格式：'{ts} {LEVEL:<5} {stage:<7} batch={n|-} {msg}'——stage/batch 取自
    记录的 extra，缺失时写 '-'。"""

    def format(self, record: logging.LogRecord) -> str:
        """渲染一行文本日志。

        @param record 标准库日志记录
        @return 单行文本（不含换行）
        """
        level = _TEXT_LEVEL_NAMES.get(record.levelno, record.levelname[:5])
        stage = getattr(record, "stage", None) or "-"
        batch = getattr(record, "batch", None)
        batch_s = "-" if batch is None else str(batch)
        return f"{_record_ts(record)} {level:<5} {stage:<7} batch={batch_s} {record.getMessage()}"


class _JsonlFormatter(logging.Formatter):
    """JSONL 行格式：每行一个 JSON 对象 {"ts","level","stage","batch","msg"}。"""

    def format(self, record: logging.LogRecord) -> str:
        """渲染一行 JSONL 日志。

        @param record 标准库日志记录
        @return 单行 JSON 文本（不含换行）
        """
        batch = getattr(record, "batch", None)
        return json.dumps(
            {
                "ts": _record_ts(record),
                "level": _JSONL_LEVEL_NAMES.get(record.levelno, record.levelname.lower()),
                "stage": getattr(record, "stage", None) or "-",
                "batch": batch,
                "msg": record.getMessage(),
            },
            ensure_ascii=False,
        )


def setup_logging(cfg: ResolvedConfig) -> None:
    """按 tool.log_format / tool.log_level 在记录器 'labelkit' 上装 stderr 处理器。

    各模块经 logging.getLogger('labelkit.<module>') 记日志，并带
    extra={'stage': ..., 'batch': ...}。重复调用幂等。

    @param cfg 本次运行的解析配置
    """
    logger = logging.getLogger("labelkit")
    logger.setLevel(_LOG_LEVELS.get(cfg.tool.log_level, logging.INFO))
    logger.propagate = False
    for handler in list(logger.handlers):    # 幂等重装
        if getattr(handler, "_labelkit_handler", False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler._labelkit_handler = True         # type: ignore[attr-defined]
    handler.setFormatter(
        _JsonlFormatter() if cfg.tool.log_format == "jsonl" else _TextFormatter()
    )
    logger.addHandler(handler)

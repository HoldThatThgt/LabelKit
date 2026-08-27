"""v1.10 —— 三档控制台面（spec §7.7 / 3.12.3；SPEC-tui-console U1–U27）。

``ConsoleRenderer`` 是 common 层 ``ProgressListener`` 协议（U19）的 CLI 层实现，
也是生产代码中 ``rich`` 依赖的**唯一触点**（U4 —— 懒导入写在 :meth:`on_run_context`
内部，绝不在模块顶层）。CLI 以**惰性壳**构造它（彼时尚无 cfg）并恒把它交给
``execute_run``；``on_run_context`` 再把它自配置成三种形态之一：

- **rich**（``cfg.console.mode_resolved == "rich"``）：双区域内联实时面板（U1）——
  日志在上方继续滚动，画布在下方按节流重绘。按 spec §3.2/§7.7 共六块：抬头、
  批进度、段棋盘（括号归因的 llm.call 分子 / ``estimate_run`` 分母，U20）、九态账目
  （stitched/threads = U18 有界修订）、取自 ``LLMClient.snapshot()`` 的 LLM 用量 +
  密钥池 + 熔断（每帧一次拉取）、键位提示 / 中断横幅。
- **plain 心跳**（plain ∧ ``heartbeat_s > 0`` ∧ stderr 非 TTY，U14）：每 N 秒一行
  不含数据的心跳，固定截止点自推进（无漂移）。
- **惰性**（其余情形）：五个回调立即返回——零分配；plain 输出的字节归属仍属 M11
  emitter。

计时模型（U26）：``Live(auto_refresh=False)``——无 rich 刷新线程——外加 Live 启动时
在运行中事件循环内创建的 **asyncio 任务 tick**：``await
asyncio.sleep(1/refresh_hz)`` → ``_maybe_refresh()``（键盘轮询 + 节流重绘）。该任务
保证事件静默期的活性（单次长 LLM 调用可令事件停滞至 ``timeout_s``——时钟、ETA 与
键位照常响应）。五个回调另做 O(1) 累计后走同一节流 ``_maybe_refresh()``（段切换 /
停止 / run.end 恒重绘），故两条路径的重绘节奏都 ≤ refresh_hz。零新线程；无运行中
事件循环时（离线快照测试同步驱动回调）跳过该任务，仅由回调节流定节奏。键盘轮询是
搭 ``_maybe_refresh`` 便车的非阻塞零超时 ``select``。

心跳计时（U14）用 ``loop.call_later`` 在 run.start 布防：固定截止点按 ``heartbeat_s``
步进自推进（追赶循环，无漂移），且独立于事件到达而触发——静默正是心跳的目标场景。
无运行中事件循环时以回调内的截止点检查兜底。run.end 撤防。

日志接管（R1）：Live 启动时把 logger ``labelkit`` 上带 ``_labelkit_handler`` 标记的
handler 的 stream 换成 :class:`_LiveLogStream` 代理——它经 ``live.console`` 逐字节
打印日志行（滚动于画布上方）；每条停止路径都在 ``finally`` 中还原原 stream。

键盘（U15，§3.4）：激活条件 = rich Live 已启动 ∧ stdin 为 TTY ∧
``console.interactive`` ∧ termios 可导入。``tty.setcbreak``（保留 ISIG——Ctrl-C 语义
不动）；封闭键集 ``? h l e + - p q``；每条停止路径都以 ``TCSADRAIN`` 还原 termios。
``l`` 展开视图逐密钥显示 env / 状态 / 调用数 / rate_limited（``KeySnapshot``，
spec 3.9.2——用量取自逐密钥累计器）。``q`` 脱离：本次运行余下的 plain 进度行与文本
终版摘要**归属本渲染器**（``mode_resolved == "rich"`` 下 emitter 被静态门关掉，
U21），经共享的 ``console_format`` 纯函数渲染——与 plain 逐字节一致。

失败语义（U7 红线 × U21 plain 归属）：每个回调体都有异常护栏——一条 WARN
「console render failed, degraded to plain: …」，安全停掉 Live（还原日志 stream 与
termios），随后在 ``mode_resolved == "rich"`` 时落到**脱离-plain**态（该档 emitter
被静态门关掉，故渲染器必须继续经 ``console_format`` 打印 plain 进度行与文本终版
摘要——与 ``q`` 路径同源；CONTRACTS §7.10）。不在 rich 归属内（心跳档）则转惰性
——plain 仍归 emitter。第二次失败彻底转惰性。它绝不外抛，故退出码与数据输出按构造
不受影响。

信息纪律（U6，机制由 U22 兜底）：``on_event`` 入参已按 "none" 档预脱敏；本模块另只
渲染封闭词表 / 计数 / 枚举 / 环境变量名字段——绝不渲染记录 id、绝不渲染自由文本、
绝不渲染下述冻结键集之外的载荷值。
"""
from __future__ import annotations

import asyncio
import logging
import os
import select
import sys
import time
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from labelkit.common.observability import console_format
from labelkit.common.observability.obslog import (
    EV_BATCH_END,
    EV_BATCH_START,
    EV_ERROR,
    EV_LLM_CALL,
    EV_RUN_END,
    EV_RUN_START,
)

if TYPE_CHECKING:
    from labelkit.common.config.model import ResolvedConfig
    from labelkit.common.observability.obslog import TraceEvent
    from labelkit.common.inference.llm_client import ProfileSnapshot

__all__ = ["ConsoleRenderer"]

_logger = logging.getLogger("labelkit.console")

# 模块级时钟别名：离线测试据此注入假 monotonic 时钟。
_monotonic = time.monotonic

# 编排器的规范链序（spec §2.3 / _compose_chain）。
_CHAIN_ORDER = ("segment", "stitch", "dedup", "classify", "extract",
                "quality", "generate", "annotate", "verify")
# 段 → estimate_run() 分母键（U20）。dedup 不发 LLM 调用。
# v1.12（spec §3.7 console 行）：改为可多键求和的分母映射——classify/annotate
# 折入帧粒度调用键（帧 pass 住同一段内，llm.call 分子天然归段），其余段保持
# 单键行为字节不变；面板零新行。
_STAGE_CALL_KEYS: dict[str, tuple[str, ...]] = {
    "segment": ("segment_calls",),
    "stitch": ("stitch_calls",),
    "classify": ("classify_calls", "frame_classify_calls"),
    "extract": ("extract_calls",),
    "quality": ("quality_calls",),
    "generate": ("generate_calls",),
    "annotate": ("annotate_calls", "frame_annotate_calls"),
    "verify": ("verify_calls",),
}
# v1.12：帧粒度两键按 estimate_run 冻结键序折入（rich 估算表等价性）。
_ESTIMATE_CALL_KEYS = ("generate_calls", "segment_calls", "stitch_calls",
                       "classify_calls", "frame_classify_calls",
                       "extract_calls", "quality_calls", "annotate_calls",
                       "frame_annotate_calls", "verify_calls")
_SEQUENCE_CALL_KEYS = (
    "scenario_seed_calls", "baseline_event_plan_calls", "variant_event_plan_calls",
    "frame_render_calls", "semantic_evaluation_calls", "noise_render_calls",
    "noise_evaluation_calls",
)

_BAR_CELLS = 24                    # 批进度条宽度（§3.2 样板）
_NARROW_COLS = 60                  # < 60 列 → 单行退化（§3.1）
_ERROR_RING = 5                    # 'e' 错误条：最近五条错误事件（§3.4）
_HINT_LINE = " [?]help [l]LLM expand [e]errors [p]pause [q]detach"
_HELP_LINE = (" keymap  ?/h help   l LLM expand (one line per key)   "
              "e recent errors   +/- canvas lines(4–16)   p pause repaint   "
              "q detach (rest of the run degrades to plain)")


def _mmss(seconds: float) -> str:
    """把秒数格式化为 mm:ss（负值按 0 处理）。

    @param seconds 秒数。
    @return 形如 ``03:07`` 的字符串。
    """
    m, s = divmod(max(int(seconds), 0), 60)
    return f"{m:02d}:{s:02d}"


def _fmt_tok(n: int) -> str:
    """token 数的 k/M 缩写（§3.2 样板：412k↑ 96k↓）。

    @param n token 数。
    @return 缩写后的字符串。
    """
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _class_overrides_exist(cfg: "ResolvedConfig") -> bool:
    """ProcessWorkflow._class_overrides_exist 的镜像（R28 dry-run 注记门）。

    @param cfg M1 解析出的 ResolvedConfig。
    @return 存在任一 per-class 覆盖时 True。
    """
    return any(view.quality != cfg.quality or view.rubric != cfg.rubric
               or view.annotate != cfg.annotate or view.generate != cfg.generate
               or view.verify != cfg.verify or view.extract != cfg.extract
               for view in cfg.class_views.values())


class _LiveLogStream:
    """R1 日志接管垫片。

    一个极小的类文件对象：写入先缓冲到换行，再经 Live console 逐字节打印
    （日志行滚动于画布上方；handler 的 Formatter 不动，故日志文本与 plain 完全
    一致）。``flush`` 是空操作——刷新由 console 自理。
    """

    def __init__(self, console: Any, text_cls: Any):
        """记录目标 console 与 rich Text 类。

        @param console 承载打印的 rich Console（取自 Live）。
        @param text_cls rich 的 Text 类（懒导入产物，避免模块顶层引入 rich）。
        """
        self._console = console
        self._text = text_cls
        self._buf = ""

    def write(self, data: str) -> int:
        """缓冲写入，按整行经 Live console 打印。

        @param data 本次写入的文本片段。
        @return 本次写入的字符数（满足类文件对象协议）。
        """
        self._buf += data
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._console.print(self._text(line), markup=False, highlight=False)
        return len(data)

    def flush(self) -> None:
        """空操作——刷新由 Live console 自理。

        @return 无。
        """
        return None


class ConsoleRenderer:
    """惰性壳形态的 ProgressListener 实现（鸭子类型，spec 3.12.3）。

    构造函数不收参数（CLI 在 M1 加载前拿不到 cfg）；``_console_factory`` 是**私有
    测试钩子**，返回用于渲染的 rich Console（离线快照测试注入
    ``Console(width=100, force_terminal=True, file=StringIO())``）。全部状态都是
    普通 int / dict / deque —— 单线程 asyncio 纪律，无锁（U26）。
    """

    def __init__(self, *, _console_factory: Callable[[], Any] | None = None):
        """构造惰性壳：只记住测试钩子，其余状态分组初始化。

        @param _console_factory 私有测试钩子，返回渲染用 rich Console。
        """
        self._console_factory = _console_factory
        self._mode = "inert"       # inert | rich | heartbeat | detached
        self._dead = False         # U7 单向降级闩
        self._cfg: "ResolvedConfig | None" = None
        self._snapshot: Callable[[], tuple] | None = None
        self._counters: Callable[[], Mapping[str, int]] | None = None
        self._fatal_streak: Callable[[], int] | None = None
        self._init_header_state()
        self._init_rich_state()
        self._init_keyboard_state()
        self._init_run_state()
        self._init_heartbeat_state()

    def _init_header_state(self) -> None:
        """初始化抬头事实（在 on_run_context 处冻结）。

        @return 无。
        """
        self._chain: tuple[str, ...] = ()
        self._mode_badge = ""
        self._dry = False
        self._generate_only = False

    def _init_rich_state(self) -> None:
        """初始化 rich 机件（由 _activate_rich 真正填充）。

        @return 无。
        """
        self._console: Any = None
        self._live: Any = None
        self._live_started = False
        self._interval = 0.2
        self._last_paint = 0.0
        self._tick_task: Any = None    # U26 asyncio tick（无运行中循环时为 None）
        self._Text: Any = None
        self._Group: Any = None
        self._Live: Any = None
        self._Table: Any = None
        self._log_handler: logging.StreamHandler | None = None
        self._log_stream_orig: Any = None

    def _init_keyboard_state(self) -> None:
        """初始化键盘状态（U15）。

        @return 无。
        """
        self._kbd_active = False
        self._kbd_fd = -1
        self._termios_mod: Any = None
        self._tty_saved: Any = None
        self._kbd_poll_logged = False         # 轮询失败只记一次（避免逐帧刷日志）
        self._show_help = False
        self._show_keys = False
        self._show_errors = False
        self._paused = False
        self._max_lines: int | None = None    # None = 自适应（由 Live 裁剪）

    def _init_run_state(self) -> None:
        """初始化运行累计量（逐事件 O(1)）。

        @return 无。
        """
        self._run_id = "-"
        self._t0: float | None = None
        self._est: dict | None = None
        self._batch_no = 0
        self._cur_batch_size = 0
        self._records_seen = 0     # Σ batch.start.size
        self._records_done = 0     # Σ batch.end 的批规模
        self._ema_rate: float | None = None    # records/s 的 EMA（ETA，§3.2）
        self._stages_seen: set[str] = set()
        self._current_stage: str | None = None
        self._stage_calls: dict[str, int] = {}     # 括号归因（U20）
        self._gen_calls = 0        # generate_only 的前置生成相调用数
        self._open_stage: tuple[str, float] | None = None
        self._stage_seconds: dict[str, float] = {}  # 段耗时条（近似）
        self._acc = {"emitted": 0, "dup": 0, "lowq": 0, "verify": 0,
                     "failed": 0, "noise": 0, "absorbed": 0, "stitched": 0,
                     "threads": 0}
        self._errors: deque[str] = deque(maxlen=_ERROR_RING)
        self._stop_requested = False
        self._breaker_seen = False    # 出现过 status="breaker_aborted" 的 llm.call
        # 脱离后的 plain 输出（q 之后的归属，U21）
        self._plain_progress_active = False

    def _init_heartbeat_state(self) -> None:
        """初始化心跳状态（U14）。

        @return 无。
        """
        self._hb_s = 0
        self._hb_t0 = 0.0
        self._hb_next: float | None = None
        self._hb_handle: Any = None    # loop.call_later 定时器（无循环时 None）
        self._hb_batch = 0
        self._hb_stage = "-"
        self._hb_calls = 0

    # ── ProgressListener 五回调（全部带护栏，U7） ──────────────────────────

    def on_run_context(self, cfg: "ResolvedConfig",
                       snapshot: "Callable[[], tuple[ProfileSnapshot, ...]]",
                       counters: "Callable[[], Mapping[str, int]]",
                       fatal_streak: "Callable[[], int]") -> None:
        """接收运行上下文并自配置成 rich / 心跳 / 惰性三形态之一。

        @param cfg M1 解析出的 ResolvedConfig。
        @param snapshot 取 LLM profile 快照的只读回调。
        @param counters 取 MetricsSink 计数器的只读回调。
        @param fatal_streak 取连续致命错误计数的只读回调。
        @return 无（异常在内部转降级，绝不外抛）。
        """
        if self._dead:
            return
        try:
            self._cfg = cfg
            self._snapshot = snapshot
            self._counters = counters
            self._fatal_streak = fatal_streak
            self._dry = cfg.dry_run
            self._generate_only = cfg.run.mode == "generate_only"
            self._freeze_header_facts(cfg)
            self._select_mode(cfg)
        except Exception as exc:  # noqa: BLE001 — U7：绝不外抛
            _logger.debug("console: on_run_context failed: %s", exc)
            self._degrade(exc)

    def _freeze_header_facts(self, cfg: "ResolvedConfig") -> None:
        """冻结抬头事实：在链段名单与模式徽标。

        @param cfg M1 解析出的 ResolvedConfig。
        @return 无。
        """
        enabled = {
            "segment": cfg.segment.enabled,
            "stitch": cfg.stitch.enabled,
            "dedup": cfg.dedup.enabled,
            "classify": cfg.classify.enabled,
            "extract": cfg.extract.enabled,
            "quality": cfg.quality.enabled,
            "generate": cfg.generate.enabled,
            "annotate": cfg.annotate.enabled,
            "verify": cfg.verify.enabled,
        }
        if self._generate_only:
            # generate_only 把生成跑成第 0 相（永不经 stage_begin）：棋盘只显示
            # 回流链，批块另起「生成」相行（§3.2）。
            enabled["generate"] = False
        self._chain = tuple(n for n in _CHAIN_ORDER if enabled[n])
        badge = f"{cfg.run.mode}/{cfg.run.modality}"
        if cfg.segment.enabled:
            badge += "/stream" + ("+stitch" if cfg.stitch.enabled else "")
        self._mode_badge = badge

    def _select_mode(self, cfg: "ResolvedConfig") -> None:
        """按 M1 冻结的 mode_resolved 选定渲染形态。

        @param cfg M1 解析出的 ResolvedConfig。
        @return 无。
        """
        if cfg.console.mode_resolved == "rich":
            self._activate_rich()
        elif cfg.console.heartbeat_s > 0 and not sys.stderr.isatty():
            self._hb_s = cfg.console.heartbeat_s
            self._mode = "heartbeat"
        # 其余：保持惰性——plain 输出归 emitter。

    def on_estimate(self, est: Mapping) -> None:
        """接收 estimate_run 估算字典（分母与 dry-run 两张静态表的数据源）。

        @param est estimate_run 返回的估算映射。
        @return 无（异常在内部转降级，绝不外抛）。
        """
        if self._mode in ("inert", "detached"):
            return
        try:
            if self._mode == "heartbeat":
                self._hb_check()
                return
            self._est = dict(est)
            if self._dry:
                # U13：dry-run 的 rich 面立即渲染两张静态估算表（dry 路径永不
                # 启动 Live）。
                self._render_estimate_tables()
                return
            self._maybe_refresh()
        except Exception as exc:  # noqa: BLE001 — U7：绝不外抛
            _logger.debug("console: on_estimate failed: %s", exc)
            self._degrade(exc)

    def on_event(self, ev: "TraceEvent") -> None:
        """按当前形态分派事件（心跳 / 脱离-plain / rich）。

        @param ev 已按 "none" 档预脱敏的 TraceEvent。
        @return 无（异常在内部转降级，绝不外抛）。
        """
        if self._mode == "inert":
            return
        try:
            if self._mode == "heartbeat":
                self._hb_event(ev)
                return
            if self._mode == "detached":
                self._detached_event(ev)
                return
            self._rich_event(ev)
        except Exception as exc:  # noqa: BLE001 — U7：绝不外抛
            _logger.debug("console: on_event failed: %s", exc)
            self._degrade(exc)

    def _rich_event(self, ev: "TraceEvent") -> None:
        """rich 档的事件吸收与重绘调度。

        @param ev 已按 "none" 档预脱敏的 TraceEvent。
        @return 无。
        """
        name = ev.ev
        if name == EV_RUN_START:
            self._run_id = ev.run_id
            self._t0 = _monotonic()
            if not self._dry:
                self._start_live()
            return
        if name == EV_RUN_END:
            self._finish(ev)
            return
        if name == EV_BATCH_START:
            self._batch_no = ev.batch_no
            self._cur_batch_size = int(ev.payload.get("size", 0))
            self._records_seen += self._cur_batch_size
            self._stages_seen = set()
            self._current_stage = None
            self._maybe_refresh()
            return
        if name == EV_BATCH_END:
            self._batch_no = ev.batch_no   # 与 emitter 进度行同语义
            self._absorb_batch_end(ev.payload)
            self._maybe_refresh()
            return
        if name == EV_LLM_CALL:
            self._absorb_llm_call(ev)
            self._maybe_refresh()
            return
        if name == EV_ERROR:
            # U6：只取 stage + kind——封闭词表（§7.6），绝不取 message 等自由文本。
            kind = ev.payload.get("kind", "?")
            self._errors.append(f"{ev.stage}·{kind}")
        self._maybe_refresh()

    def _absorb_llm_call(self, ev: "TraceEvent") -> None:
        """吸收 llm.call：按括号归因累计分子，并识别熔断已开信号。

        @param ev llm.call 事件。
        @return 无。
        """
        if self._current_stage is not None:
            self._stage_calls[self._current_stage] = (
                self._stage_calls.get(self._current_stage, 0) + 1)
        elif self._generate_only and self._batch_no == 0:
            self._gen_calls += 1
        if ev.payload.get("status") == "breaker_aborted":
            # v1.6 硬跳闸（认证 401/403）会在未达阈值时打开熔断；breaker_aborted
            # 是冻结的 U19 协议里唯一精确的「熔断已开」信号。
            self._breaker_seen = True

    def on_stage(self, stage: str, batch_no: int) -> None:
        """段切换回调：结算上一段耗时并强制重绘一帧。

        @param stage 新进入的段名。
        @param batch_no 当前批号。
        @return 无（异常在内部转降级，绝不外抛）。
        """
        if self._mode in ("inert", "detached"):
            return
        try:
            if self._mode == "heartbeat":
                self._hb_stage = stage
                self._hb_batch = batch_no
                self._hb_check()
                return
            now = _monotonic()
            if self._open_stage is not None:
                prev, t = self._open_stage
                self._stage_seconds[prev] = (self._stage_seconds.get(prev, 0.0)
                                             + (now - t))
            self._open_stage = (stage, now)
            self._current_stage = stage
            self._stages_seen.add(stage)
            self._batch_no = batch_no
            self._maybe_refresh(force=True)    # 段切换恒重绘
        except Exception as exc:  # noqa: BLE001 — U7：绝不外抛
            _logger.debug("console: on_stage failed: %s", exc)
            self._degrade(exc)

    def on_stop_requested(self) -> None:
        """收到优雅停止请求：立刻画出中断横幅。

        @return 无（异常在内部转降级，绝不外抛）。
        """
        if self._mode in ("inert", "heartbeat", "detached"):
            return
        try:
            self._stop_requested = True
            self._paint_now()          # 中断横幅优先于 'p' 暂停
        except Exception as exc:  # noqa: BLE001 — U7：绝不外抛
            _logger.debug("console: on_stop_requested failed: %s", exc)
            self._degrade(exc)

    # ── rich 激活 / Live 生命周期 ─────────────────────────────────────────

    def _activate_rich(self) -> None:
        """懒导入 rich（U4 唯一触点）。

        ImportError ⇒ 一条 WARN 后永久转 plain-惰性（mode_resolved 只做过
        find_spec 探测，故这里兜住装坏了的安装）。

        @return 无。
        """
        try:
            from rich.console import Console, Group
            from rich.live import Live
            from rich.table import Table
            from rich.text import Text
        except ImportError as exc:
            _logger.warning("console: rich import failed, degraded to plain: %s",
                            exc, extra={"stage": "run", "batch": 0})
            self._mode = "inert"
            return
        self._Group, self._Live, self._Table, self._Text = Group, Live, Table, Text
        self._console = (self._console_factory() if self._console_factory
                         is not None else Console(stderr=True, soft_wrap=False))
        self._interval = 1.0 / max(self._cfg.console.refresh_hz, 1)
        self._mode = "rich"

    def _start_live(self) -> None:
        """首个 run.start：启动 Live 画布、接管日志 stream（R1）、进入 cbreak。

        validate / dry-run 路径永不到达此处（U13）。

        @return 无。
        """
        if self._live_started:
            return
        self._live = self._Live(
            self._render(),
            console=self._console,
            auto_refresh=False,            # U26：不要 rich 刷新线程
            redirect_stdout=False,
            redirect_stderr=False,
            transient=False,               # U8：末帧留在回滚区
        )
        self._live.start()
        self._live_started = True
        self._take_log_stream()
        self._setup_keyboard()
        self._start_tick()                 # U26 asyncio tick（静默期活性）
        self._paint_now()                  # 首帧；此时尚未轮询按键

    def _start_tick(self) -> None:
        """U26：钉死的 asyncio 任务 tick——保证静默期时钟/ETA/键盘活性。

        单次长 LLM 调用可让回调停滞至 timeout_s。无运行中事件循环时（离线快照
        测试同步驱动回调）仅由回调节流定重绘节奏。

        @return 无。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            _logger.debug("console: no running loop, live tick disabled: %s", exc)
            self._tick_task = None
            return
        self._tick_task = loop.create_task(self._tick_loop())

    async def _tick_loop(self) -> None:
        """tick 协程主体：按 refresh_hz 周期驱动 _maybe_refresh。

        @return 无（取消与异常均在内部消化，绝不外抛）。
        """
        try:
            while self._mode == "rich" and self._live_started:
                await asyncio.sleep(self._interval)
                self._maybe_refresh()
        except asyncio.CancelledError:
            _logger.debug("console: live tick cancelled")
        except Exception as exc:  # noqa: BLE001 — U7：tick 绝不把异常抛出去
            _logger.debug("console: live tick failed: %s", exc)
            self._degrade(exc)

    def _stop_tick(self) -> None:
        """取消 tick 任务（幂等）。

        @return 无。
        """
        task = self._tick_task
        self._tick_task = None
        if task is not None and not task.done():
            task.cancel()

    def _take_log_stream(self) -> None:
        """R1：把 labelkit handler 的 stream 换成 Live 代理。

        @return 无。
        """
        logger = logging.getLogger("labelkit")
        for handler in logger.handlers:
            if (getattr(handler, "_labelkit_handler", False)
                    and isinstance(handler, logging.StreamHandler)):
                proxy = _LiveLogStream(self._live.console, self._Text)
                try:
                    self._log_stream_orig = handler.setStream(proxy)
                except (ValueError, OSError) as exc:
                    # setStream 会在换流前 flush 旧 stream——流已关闭/损坏时在那里
                    # 抛错且 handler 原封不动。改为直接重指（免 flush）；一条死流
                    # 不该让我们丢掉整块面板（U7 精神）。
                    _logger.debug("console: setStream failed, retargeting "
                                  "directly: %s", exc)
                    self._log_stream_orig = handler.stream
                    handler.stream = proxy
                self._log_handler = handler
                break

    def _restore_log_stream(self) -> None:
        """还原被接管的日志 stream（幂等，绝不抛错）。

        @return 无。
        """
        if self._log_handler is not None and self._log_stream_orig is not None:
            try:
                self._log_handler.setStream(self._log_stream_orig)
            except Exception as exc:  # noqa: BLE001 — 还原路径绝不抛错
                _logger.debug("console: log stream restore failed: %s", exc)
        self._log_handler = None
        self._log_stream_orig = None

    def _setup_keyboard(self) -> None:
        """U15 激活合取式；任一环失败 ⇒ 只渲染不收键（不进 cbreak）。

        @return 无。
        """
        if self._cfg is None or not self._cfg.console.interactive:
            return
        try:
            import termios
            import tty
        except ImportError as exc:
            _logger.debug("console: termios/tty unavailable, keyboard off: %s",
                          exc)
            return
        try:
            if not sys.stdin.isatty():
                return
            fd = sys.stdin.fileno()
            self._tty_saved = termios.tcgetattr(fd)
            tty.setcbreak(fd)              # 清 ECHO|ICANON，**保留** ISIG
            self._termios_mod = termios
            self._kbd_fd = fd
            self._kbd_active = True
        except Exception as exc:  # noqa: BLE001 — 异形 tty：退回只渲染不收键
            _logger.debug("console: cbreak setup failed, render-only: %s", exc)
            self._kbd_active = False

    def _restore_termios(self) -> None:
        """以 TCSADRAIN 还原 termios（每条停止路径都会走到）。

        @return 无。
        """
        if self._termios_mod is not None and self._tty_saved is not None:
            try:
                self._termios_mod.tcsetattr(
                    self._kbd_fd, self._termios_mod.TCSADRAIN, self._tty_saved)
            except Exception as exc:  # noqa: BLE001 — 还原路径绝不抛错
                _logger.debug("console: termios restore failed: %s", exc)
        self._kbd_active = False
        self._termios_mod = None
        self._tty_saved = None

    def _stop_live(self) -> None:
        """全部停止路径的汇聚点：取消 tick、停 Live，并在 finally 还原
        日志 stream 与 termios（§3.4 终态纪律）。

        @return 无。
        """
        self._stop_tick()
        try:
            if self._live is not None and self._live_started:
                self._live.stop()
        finally:
            self._live_started = False
            self._restore_log_stream()
            self._restore_termios()

    def _degrade(self, exc: BaseException) -> None:
        """U7 × U21：一条 WARN + 安全拆台，然后按 plain 归属落位。

        按 CONTRACTS §7.10：``mode_resolved == "rich"`` 时 emitter 被静态门关掉，
        故本渲染器必须继续打印 plain 进度行 / 文本摘要 —— 降级落到**脱离**态
        （与 ``q`` 同一输出路径）。不在 rich 归属内（心跳 / 惰性）时 plain 归
        emitter —— 转惰性。第二次失败（``_dead`` 闩，例如脱离态 stderr 写本身
        炸了）静默彻底转惰性。本方法绝不抛错。

        @param exc 触发降级的异常实例。
        @return 无。
        """
        if self._dead:
            self._mode = "inert"
            self._disarm_heartbeat()
            return
        self._dead = True
        owns_plain = (self._cfg is not None
                      and self._cfg.console.mode_resolved == "rich")
        try:
            self._stop_live()
        except Exception as teardown_exc:  # noqa: BLE001 — 拆台失败不阻断降级
            _logger.debug("console: live teardown failed while degrading: %s",
                          teardown_exc)
        self._mode = "detached" if owns_plain else "inert"
        if self._mode == "inert":
            self._disarm_heartbeat()
        # 唯一的一次降级 WARN；日志通道自身失效时静默吞掉——记录点正是失效者，
        # 而 U7 红线要求 _degrade 绝不外抛。
        with suppress(Exception):
            _logger.warning("console render failed, degraded to plain: %s", exc,
                            extra={"stage": "run", "batch": 0})

    # ── 重绘节流 + 键盘轮询（U26 退化 tick） ──────────────────────────────

    def _maybe_refresh(self, force: bool = False) -> None:
        """节流重绘：先轮询按键，再按 refresh_hz 间隔更新画布。

        @param force True 时跳过时间间隔判断（段切换等必绘时机）。
        @return 无。
        """
        if self._mode != "rich" or not self._live_started:
            return
        if self._kbd_active:
            self._poll_keys()
            if self._mode != "rich" or not self._live_started:
                return                     # 轮询期间按下 'q' 已脱离
        if self._paused:
            # 'p' 完全冻结画布（便于复制粘贴，§3.4）；键位反馈 / 中断横幅 /
            # 末帧定格走 _paint_now。
            return
        now = _monotonic()
        if force or now - self._last_paint >= self._interval:
            self._last_paint = now
            self._live.update(self._render(), refresh=True)

    def _paint_now(self) -> None:
        """绕过节流与 'p' 暂停的即时重绘。

        用于键位反馈、中断横幅与定格的末帧。

        @return 无。
        """
        if self._mode != "rich" or not self._live_started:
            return
        self._last_paint = _monotonic()
        self._live.update(self._render(), refresh=True)

    def _poll_keys(self) -> None:
        """非阻塞零超时轮询 stdin，逐字节交给 _handle_key。

        @return 无。
        """
        while True:
            try:
                ready, _, _ = select.select([self._kbd_fd], [], [], 0)
            except (OSError, ValueError) as exc:
                if not self._kbd_poll_logged:
                    # 只记一次：轮询搭每帧刷新的便车，逐帧记录会淹没日志。
                    self._kbd_poll_logged = True
                    _logger.debug("console: key poll failed: %s", exc)
                return
            if not ready:
                return
            data = os.read(self._kbd_fd, 1)
            if not data:
                return
            self._handle_key(data.decode("ascii", errors="ignore"))
            if self._mode != "rich":
                return

    def _handle_key(self, ch: str) -> None:
        """处理封闭键集（§3.4）；集外按键一律忽略。

        每个被处理的开关都立即重绘（暂停中也给视觉反馈）。

        @param ch 读到的单个字符。
        @return 无。
        """
        if ch in ("?", "h"):
            self._show_help = not self._show_help
        elif ch == "l":
            self._show_keys = not self._show_keys
        elif ch == "e":
            self._show_errors = not self._show_errors
        elif ch == "+":
            self._max_lines = min(16, (self._max_lines or 16) + 1)
        elif ch == "-":
            self._max_lines = max(4, (self._max_lines or 16) - 1)
        elif ch == "p":
            self._paused = not self._paused
        elif ch == "q":
            self._detach()
            return
        else:
            return
        self._paint_now()

    def _detach(self) -> None:
        """'q'（§3.4）：离开面板，余下运行经 console_format 打 plain 行。

        rich 档下 emitter 被静态门关掉——plain 归本渲染器（U21）。

        @return 无。
        """
        self._stop_live()
        self._mode = "detached"

    # ── 事件吸收 ──────────────────────────────────────────────────────────

    def _absorb_batch_end(self, payload: Mapping) -> None:
        """吸收 batch.end 账目（§3.2：emitted 分量 = batch.end.active，post-emit
        恒等）。

        @param payload batch.end 事件载荷。
        @return 无。
        """
        self._acc["emitted"] += int(payload.get("active", 0))
        self._acc["dup"] += int(payload.get("dropped_dup", 0))
        self._acc["lowq"] += int(payload.get("dropped_lowq", 0))
        self._acc["verify"] += int(payload.get("dropped_verify", 0))
        self._acc["failed"] += int(payload.get("failed", 0))
        self._acc["noise"] += int(payload.get("dropped_noise", 0))
        self._acc["absorbed"] += int(payload.get("absorbed", 0))
        self._acc["stitched"] += int(payload.get("stitched", 0))
        self._acc["threads"] += int(payload.get("threads", 0))
        self._records_done += self._cur_batch_size
        # ETA（§3.2）：对 batch.end 事件上的 records/s 做 EMA。
        duration_ms = int(payload.get("duration_ms", 0))
        if duration_ms > 0 and self._cur_batch_size > 0:
            rate = self._cur_batch_size / (duration_ms / 1000.0)
            self._ema_rate = (rate if self._ema_rate is None
                              else 0.4 * rate + 0.6 * self._ema_rate)
        # 结算尚未闭合的段区间——emit 时间折进最后一段（段耗时条标注为近似）。
        if self._open_stage is not None:
            prev, t = self._open_stage
            self._stage_seconds[prev] = (self._stage_seconds.get(prev, 0.0)
                                         + (_monotonic() - t))
            self._open_stage = None

    def _finish(self, ev: "TraceEvent") -> None:
        """run.end（U8）：画定格末帧后停掉 Live，帧留在回滚区
        （transient=False）。dry-run 的 rich 面不额外打印。

        @param ev run.end 事件。
        @return 无。
        """
        if self._open_stage is not None:
            prev, t = self._open_stage
            self._stage_seconds[prev] = (self._stage_seconds.get(prev, 0.0)
                                         + (_monotonic() - t))
            self._open_stage = None
        if self._live_started:
            counts = ev.payload.get("counts") or {}
            try:
                self._live.update(self._render_final(dict(counts)), refresh=True)
            finally:
                self._stop_live()
        self._mode = "inert"               # 运行结束；杂散事件保持廉价

    # ── 脱离态 plain 输出（q 之后，U21 归属） ─────────────────────────────

    def _detached_event(self, ev: "TraceEvent") -> None:
        """脱离态下经 console_format 打 plain 进度行与终版摘要。

        @param ev TraceEvent。
        @return 无。
        """
        if ev.ev == EV_BATCH_END:
            self._absorb_batch_end(ev.payload)
            if sys.stderr.isatty():
                sys.stderr.write(console_format.format_progress_line(
                    ev.batch_no, self._acc["emitted"], self._progress_totals()))
                sys.stderr.flush()
                self._plain_progress_active = True
            return
        if ev.ev == EV_BATCH_START:
            self._cur_batch_size = int(ev.payload.get("size", 0))
            return
        if ev.ev == EV_RUN_END:
            if self._plain_progress_active:
                sys.stderr.write("\n")
                self._plain_progress_active = False
            counts = dict(ev.payload.get("counts") or {})
            sys.stderr.write(
                "\n".join(console_format.format_summary_lines(counts)) + "\n")
            sys.stderr.flush()
            self._mode = "inert"

    def _progress_totals(self) -> dict[str, int]:
        """组装 plain 进度行所需的累计状态计数。

        @return 冻结键集的计数字典。
        """
        return {"dropped_dup": self._acc["dup"], "dropped_lowq": self._acc["lowq"],
                "dropped_verify": self._acc["verify"], "failed": self._acc["failed"]}

    # ── 心跳（plain 非 TTY，U14） ─────────────────────────────────────────

    def _hb_event(self, ev: "TraceEvent") -> None:
        """心跳档的事件吸收：布防 / 撤防 / 累计 + 截止点检查。

        @param ev TraceEvent。
        @return 无。
        """
        name = ev.ev
        if name == EV_RUN_START:
            self._hb_t0 = _monotonic()
            self._hb_next = self._hb_t0 + self._hb_s
            self._arm_hb_timer()           # U14：loop.call_later——静默期照跳
            return
        if name == EV_RUN_END:
            self._disarm_heartbeat()       # 渲染器收工
            self._mode = "inert"
            return
        if name == EV_BATCH_START:
            self._hb_batch = ev.batch_no
        elif name == EV_LLM_CALL:
            self._hb_calls += 1
        self._hb_check()

    def _arm_hb_timer(self) -> None:
        """U14 计时归属：按固定截止点布防一个 ``loop.call_later`` 定时器。

        心跳的目标场景正是事件静默，故必须独立于事件触发。无运行中事件循环时
        （离线测试同步驱动回调）以逐回调的 ``_hb_check`` 兜底。

        @return 无。
        """
        if self._hb_next is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            _logger.debug("console: no running loop, heartbeat timer off: %s",
                          exc)
            self._hb_handle = None
            return
        delay = max(self._hb_next - _monotonic(), 0.0)
        self._hb_handle = loop.call_later(delay, self._hb_fire)

    def _hb_fire(self) -> None:
        """定时器回调：跳一拍并按推进后的截止点重新布防。

        @return 无。
        """
        try:
            self._hb_check()
            self._arm_hb_timer()           # 按推进后的截止点重新布防
        except Exception as exc:  # noqa: BLE001 — 定时器出问题绝不伤及运行
            _logger.debug("console: heartbeat timer failed: %s", exc)
            self._disarm_heartbeat()

    def _disarm_heartbeat(self) -> None:
        """撤防心跳定时器并清空截止点。

        @return 无。
        """
        self._hb_next = None
        handle = self._hb_handle
        self._hb_handle = None
        if handle is not None:
            handle.cancel()

    def _hb_check(self) -> None:
        """固定截止点心跳（U14）：一行不含数据的行，截止点按 heartbeat_s
        步进自推进（追赶循环，无漂移）。

        @return 无。
        """
        if self._hb_next is None:
            return
        now = _monotonic()
        if now < self._hb_next:
            return
        sys.stderr.write(
            f"heartbeat batch={self._hb_batch} stage={self._hb_stage} "
            f"llm_calls={self._hb_calls} elapsed={int(now - self._hb_t0)}s\n")
        sys.stderr.flush()
        while self._hb_next <= now:
            self._hb_next += self._hb_s

    # ── 画布渲染（spec §3.2 六块） ────────────────────────────────────────

    def _line(self, s: str, style: str | None = None) -> Any:
        """构造一行画布文本。

        超宽行按 rich 默认**折行**而非裁剪——任何计数都不会因终端偏窄而丢失，
        与真实终端折 plain 进度行的行为一致。

        @param s 行文本。
        @param style rich 样式串；None 表示无样式。
        @return rich Text 实例。
        """
        return self._Text(s, style=style or "")

    def _render(self) -> Any:
        """渲染一帧画布（六块；窄终端退化为单行）。

        @return rich Group（窄终端下为单行 Text）。
        """
        snap = tuple(self._snapshot()) if self._snapshot is not None else ()
        counters = dict(self._counters()) if self._counters is not None else {}
        width = self._console.size.width

        if width < _NARROW_COLS:
            # §3.1 退化行：画布塌缩为 plain 单行形（去掉行首的 \r）。
            return self._line(console_format.format_progress_line(
                self._batch_no, self._acc["emitted"],
                self._progress_totals())[1:])

        streak = self._fatal_streak() if self._fatal_streak is not None else 0
        threshold = self._cfg.run.fatal_error_threshold
        breaker_open = self._breaker_open(snap, streak, threshold)

        lines: list[Any] = self._header_lines(width, breaker_open)
        lines.append(self._line(self._batch_line(counters)))    # 第 2 块 批进度
        lines.append(self._line(""))
        lines.append(self._line(self._board_line()))            # 第 3 块 段棋盘
        lines.append(self._line(""))
        lines.append(self._line(self._account_line(counters)))  # 第 4 块 状态账目
        lines.append(self._line(""))
        # 第 5 块 LLM
        lines.extend(self._llm_lines(snap, streak, threshold, breaker_open))
        # 第 6 块 键位提示 / 切换出的面
        lines.extend(self._toggle_lines())

        if self._max_lines is not None:
            lines = lines[: self._max_lines]
        return self._Group(*lines)

    def _header_lines(self, width: int, breaker_open: bool) -> list[Any]:
        """第 1 块：分隔线 + 熔断/中断横幅 + 抬头行 + 工程路径行 + 空行。

        @param width 当前 console 宽度。
        @param breaker_open 熔断是否已打开。
        @return 组成抬头块的行列表。
        """
        cfg = self._cfg
        lines: list[Any] = [self._line("─" * min(width, 100), style="dim")]
        if breaker_open:
            lines.append(self._line(" ⚠ breaker open", style="bold red"))
        if self._stop_requested:
            lines.append(self._line(" graceful interrupt in progress (≤30s)…",
                                    style="bold yellow"))

        elapsed = _monotonic() - self._t0 if self._t0 is not None else 0.0
        head = (f" labelkit run · {self._run_id} · {self._mode_badge}"
                f" · seed {cfg.run.seed} · elapsed {_mmss(elapsed)}")
        eta = self._eta_seconds()
        if eta is not None:
            head += f" · ETA ~{_mmss(eta)}"
        lines.append(self._line(head, style="bold"))
        lines.append(self._line(
            f" project {cfg.project_path} → {cfg.run.output}", style="dim"))
        lines.append(self._line(""))
        return lines

    def _toggle_lines(self) -> list[Any]:
        """第 6 块：错误条 / 键位提示 / 帮助行（按开关在场）。

        @return 组成第 6 块的行列表。
        """
        lines: list[Any] = []
        if self._show_errors:
            strip = "  |  ".join(self._errors) if self._errors else "(none)"
            lines.append(self._line(f" errors {strip}"))
        if self._kbd_active:
            hint = _HINT_LINE + ("  ⏸" if self._paused else "")
            lines.append(self._line(hint, style="dim"))
        if self._show_help:
            lines.append(self._line(_HELP_LINE, style="dim"))
        return lines

    def _eta_seconds(self) -> float | None:
        """按 EMA 速率估算剩余秒数。

        @return 剩余秒数；估算字典或速率不可用时为 None。
        """
        if (self._est is None or self._ema_rate is None or self._ema_rate <= 0
                or self._est.get("records") is None):
            return None
        remaining = max(int(self._est["records"]) - self._records_done, 0)
        return remaining / self._ema_rate

    def _batch_line(self, counters: Mapping[str, int]) -> str:
        """第 2 块文本：批进度条 / generate_only 的「生成」相行。

        @param counters MetricsSink 计数器快照。
        @return 批进度行文本。
        """
        est = self._est
        if self._generate_only and self._batch_no == 0:
            # 「生成」相形（§3.2）：calls 由 llm.call 实时累计；已产在相末由
            # counts.generated 一次性更新（它是相末计量）。
            total = est.get("generate_calls") if est else None
            calls = (f"{self._gen_calls}/{total}" if total is not None
                     else str(self._gen_calls))
            produced = counters.get("counts.generated", 0)
            return f" generate ▶ calls {calls} · produced {produced}"
        n_batches = est.get("batches") if est else None
        seg = f" batch {self._batch_no}"
        if n_batches:
            seg += f"/{n_batches}"
            est_records = int(est.get("records", 0))
            frac = (min(self._records_seen / est_records, 1.0) if est_records
                    else min(self._batch_no / n_batches, 1.0))
            filled = round(frac * _BAR_CELLS)
            seg += "  " + "█" * filled + "░" * (_BAR_CELLS - filled)
        seg += f"  records {self._records_seen}"
        if est and est.get("records") is not None:
            seg += f"/{est['records']}"
        seg += " (scanned)"
        return seg

    def _board_line(self) -> str:
        """第 3 块文本：段棋盘（在飞段带分子/分母，已过段打勾）。

        @return 段棋盘行文本。
        """
        parts = []
        for name in self._chain:
            call_keys = _STAGE_CALL_KEYS.get(name)
            if name == self._current_stage:
                if call_keys is None:              # dedup：不发 LLM 调用
                    parts.append(f"{name} ▶")
                else:
                    a = self._stage_calls.get(name, 0)
                    denom = self._stage_denominator(call_keys)
                    parts.append(f"{name} ▶ {a}/{denom}" if denom is not None
                                 else f"{name} ▶ {a}")
            elif name in self._stages_seen:
                parts.append(f"{name} ✓")
            else:
                parts.append(f"{name} ·")
        return " stages  " + "   ".join(parts)

    def _stage_denominator(self, call_keys: tuple[str, ...]) -> int | None:
        """v1.12：段棋盘分母＝映射键求和（classify/annotate 折入帧粒度调用数）；
        估算键全部缺位 ⇒ 无分母（单键段与 v1.10 行为逐字节一致）。

        @param call_keys 该段的 estimate_run 分母键元组。
        @return 求和分母；键全缺位时 None。
        """
        if self._est is None:
            return None
        values = [self._est[key] for key in call_keys if key in self._est]
        return sum(values) if values else None

    def _account_line(self, counters: Mapping[str, int]) -> str:
        """第 4 块文本：九态账目（stream / stitch 项按开关在场）。

        @param counters MetricsSink 计数器快照。
        @return 账目行文本。
        """
        acc = self._acc
        seg = (f" account  emitted {acc['emitted']}   dup {acc['dup']}   "
               f"lowq {acc['lowq']}   verify {acc['verify']}   "
               f"failed {acc['failed']}")
        if self._cfg.segment.enabled:
            seg += f"   noise {acc['noise']}   absorbed {acc['absorbed']}"
        if self._cfg.stitch.enabled:
            threads = counters.get("counts.threads", acc["threads"])
            seg += f"   stitched {acc['stitched']}   threads {threads}"
        return seg

    def _llm_lines(self, snap: tuple, streak: int, threshold: int,
                   breaker_open: bool) -> list[Any]:
        """第 5 块：逐 profile 用量行 + 密钥池行 + 熔断行。

        @param snap LLMClient.snapshot() 的逐 profile 快照元组。
        @param streak 连续致命错误计数。
        @param threshold 熔断阈值（run.fatal_error_threshold）。
        @param breaker_open 熔断是否已打开（决定熔断行是否标红）。
        @return 组成第 5 块的行列表。
        """
        lines: list[Any] = []
        if snap:
            name_w = max(len(s.name) for s in snap)
            for i, s in enumerate(snap):
                prefix = " LLM  " if i == 0 else "      "
                cost = (f"${s.est_cost_usd:.2f}" if s.est_cost_usd is not None
                        else "—")
                p50 = (f"{s.p50_latency_ms / 1000:.1f}s"
                       if s.p50_latency_ms is not None else "—")
                lines.append(self._line(
                    f"{prefix}{s.name:<{name_w}}  in_flight {s.in_flight}/"
                    f"{s.max_concurrency}  calls {s.calls}  retries {s.retries}  "
                    f"tok {_fmt_tok(s.prompt_tokens)}↑ "
                    f"{_fmt_tok(s.completion_tokens)}↓  {cost}  p50 {p50}"))
            lines.extend(self._key_lines(snap))
        lines.append(self._line(f"      breaker {streak}/{threshold}",
                                style="bold red" if breaker_open else ""))
        return lines

    def _key_lines(self, snap: tuple) -> list[Any]:
        """密钥相关行：'l' 展开的逐密钥行，以及池化/降级时的密钥汇总行。

        @param snap LLMClient.snapshot() 的逐 profile 快照元组。
        @return 密钥行列表（无需展示时为空）。
        """
        lines: list[Any] = []
        if self._show_keys:
            # 'l' 展开（§3.4）：逐密钥一行——env、状态与逐密钥用量镜像
            # （calls / rate_limited，spec 3.9.2）。
            for s in snap:
                for k in s.keys:
                    lines.append(self._line(
                        f"      {s.name}·{k.env} {self._key_state(k)}"
                        f"  calls {k.calls}  rate_limited {k.rate_limited}"))
        pooled = any(len(s.keys) > 1 for s in snap)
        degraded = any(k.state != "ok" for s in snap for k in s.keys)
        if pooled or degraded:
            seen: dict[str, str] = {}
            for s in snap:
                for k in s.keys:
                    seen.setdefault(k.env, self._key_state(k))
            keys_seg = " · ".join(f"{env} {st}" for env, st in seen.items())
            lines.append(self._line(f"      keys {keys_seg}"))
        return lines

    @staticmethod
    def _key_state(k: Any) -> str:
        """把 KeySnapshot 状态渲染成面板用短词。

        @param k 单个 KeySnapshot。
        @return "ok" / "cooldown Ns" / "disabled"。
        """
        if k.state == "ok":
            return "ok"
        if k.state == "cooldown":
            return f"cooldown {k.cooldown_remaining_s}s"
        return "disabled"

    def _breaker_open(self, snap: tuple, streak: int, threshold: int) -> bool:
        """判定熔断是否已打开。

        已打开 = 连续致命计数达阈值，**或** v1.6 硬跳闸——认证类立即熔断发生在
        阈值**以下**，靠两处只读足迹可见：breaker_aborted 的 llm.call 事件，以及
        整池密钥被禁用的 profile（P2-3 坏密钥场景：面板须在 10 秒内红出密钥禁用
        与熔断横幅，§3.7）。

        @param snap 逐 profile 快照元组。
        @param streak 连续致命错误计数。
        @param threshold 熔断阈值。
        @return 熔断已打开则 True。
        """
        pool_dead = any(s.keys and all(k.state == "disabled" for k in s.keys)
                        for s in snap)
        return streak >= threshold or self._breaker_seen or pool_dead

    # ── 定格末帧（U8） ────────────────────────────────────────────────────

    def _render_final(self, counts: dict) -> Any:
        """渲染定格末帧：抬头 + counts 表 + 段耗时条 + 用量表 + 侧产物路径。

        @param counts run.end 携带的终版计数（= report.counts）。
        @return rich Group。
        """
        cfg = self._cfg
        snap = tuple(self._snapshot()) if self._snapshot is not None else ()
        width = self._console.size.width
        elapsed = _monotonic() - self._t0 if self._t0 is not None else 0.0

        parts: list[Any] = [self._line("─" * min(width, 100), style="dim")]
        parts.append(self._line(
            f" labelkit run done · {self._run_id} · {self._mode_badge}"
            f" · elapsed {_mmss(elapsed)}", style="bold"))

        # 表标题另起整宽行——rich 会把 Table 自带 title 折到**表宽**，长标题会
        # 被折断。
        parts.append(self._line(" counts (= report.counts)", style="bold"))
        parts.append(self._counts_table(counts))
        parts.extend(self._stage_time_lines())

        if snap:
            parts.append(self._line(" llm_usage", style="bold"))
            parts.append(self._usage_table(snap))

        stem = str(Path(cfg.run.output).with_suffix(""))
        if cfg.output.rejects != "none":
            parts.append(self._line(f" rejects → {stem}.rejects.jsonl"))
        if cfg.trace.enabled:
            parts.append(self._line(f" trace → {cfg.trace.path}"))
        return self._Group(*parts)

    def _counts_table(self, counts: dict) -> Any:
        """末帧的 counts 两列表。

        @param counts 终版计数映射。
        @return rich Table。
        """
        counts_table = self._Table()
        counts_table.add_column("key")
        counts_table.add_column("value", justify="right")
        for key, value in counts.items():
            counts_table.add_row(str(key), str(value))
        return counts_table

    def _stage_time_lines(self) -> list[Any]:
        """末帧的段耗时条（近似：on_stage 转换间隔累加，非 report 计时）。

        @return 段耗时行列表；无计时数据时为空。
        """
        if not self._stage_seconds:
            return []
        lines: list[Any] = [self._line(
            " stage time (approx: summed on_stage intervals, not report "
            "timings)", style="dim")]
        max_s = max(self._stage_seconds.values()) or 1.0
        for name in self._chain:
            sec = self._stage_seconds.get(name)
            if sec is None:
                continue
            bar = "█" * max(1, round(sec / max_s * 20))
            lines.append(self._line(f" {name:<9} {bar} {sec:.1f}s"))
        return lines

    def _usage_table(self, snap: tuple) -> Any:
        """末帧的 llm_usage 表。

        @param snap 逐 profile 快照元组。
        @return rich Table。
        """
        usage = self._Table()
        for col in ("profile", "calls", "retries", "tok↑", "tok↓", "cost", "p50"):
            usage.add_column(col, justify="right" if col != "profile" else "left")
        for s in snap:
            usage.add_row(
                s.name, str(s.calls), str(s.retries),
                _fmt_tok(s.prompt_tokens), _fmt_tok(s.completion_tokens),
                f"${s.est_cost_usd:.2f}" if s.est_cost_usd is not None else "—",
                f"{s.p50_latency_ms / 1000:.1f}s"
                if s.p50_latency_ms is not None else "—")
        return usage

    # ── dry-run 估算表（U13） ─────────────────────────────────────────────

    def _render_estimate_tables(self) -> None:
        """dry-run 的 rich 面：两张静态表 + plain 行的同款脚注。

        承载与 plain 字节锚行**同一份**信息（取自同一个 estimate_run 字典），
        只是换成表格形态。

        @return 无。
        """
        cfg = self._cfg
        est = self._est or {}
        t1 = self._Table()
        t1.add_column("item")
        t1.add_column("value", justify="right")
        t1.add_row("mode", cfg.run.mode)
        t1.add_row("estimated_records", str(est.get("records", 0)))
        t1.add_row("batches", str(est.get("batches", 0)))
        sequence = est.get("sequence")
        if isinstance(sequence, Mapping):
            self._add_sequence_estimate_rows(t1, sequence)

        t2 = self._Table()
        t2.add_column("stage")
        t2.add_column("calls", justify="right")
        for key in _ESTIMATE_CALL_KEYS:
            t2.add_row(key, str(est.get(key, 0)))
        t2.add_row("total", str(est.get("total_calls", 0)))

        # 表标题另起整行（rich Table 自带 title 会折到表宽——长标题会被折断）。
        self._console.print(self._Text("dry-run estimate", style="bold"))
        self._console.print(t1)
        self._console.print(self._Text(
            "estimated LLM calls (excludes retries and repair calls)",
            style="bold"))
        self._console.print(t2)
        if isinstance(sequence, Mapping):
            self._console.print(self._Text("sequence generation calls", style="bold"))
            self._console.print(self._sequence_calls_table(sequence))
        self._print_estimate_notes(cfg)

    @staticmethod
    def _add_sequence_estimate_rows(table, sequence: Mapping) -> None:
        """把 sequence 精确算术加入 dry-run 主表。

        @param table rich 估算主表。
        @param sequence estimate.sequence 块。
        @return None。
        """
        for key in (
            "planned_sets", "planned_sequences", "primary_events",
            "interleaving_opportunities", "primary_sessions",
            "interleaved_primary_sessions", "noise_events", "replay_sequences",
            "replay_events", "stream_rows",
            "successful_attempt_lower_bound", "max_slot_attempts_upper_bound",
        ):
            table.add_row(key, str(sequence.get(key, 0)))

    def _sequence_calls_table(self, sequence: Mapping) -> Any:
        """构造七类 sequence generation 调用表。

        @param sequence estimate.sequence 块。
        @return rich Table。
        """
        table = self._Table()
        table.add_column("family")
        table.add_column("calls", justify="right")
        calls = sequence.get("sequence_calls", {})
        for key in _SEQUENCE_CALL_KEYS:
            table.add_row(key, str(calls.get(key, 0)))
        return table

    def _print_estimate_notes(self, cfg: "ResolvedConfig") -> None:
        """打印 dry-run 估算的两条下界注记与收尾行（与 plain 路径同文）。

        @param cfg M1 解析出的 ResolvedConfig。
        @return 无。
        """
        if cfg.classify.enabled and (cfg.classify.assignment == "multi"
                                     or _class_overrides_exist(cfg)):
            self._console.print(self._Text(
                "note: estimated with global config / multi reports a lower "
                "bound at label multiplier 1"))
        if cfg.segment.enabled and cfg.segment.strategy in ("llm", "hybrid"):
            self._console.print(self._Text(
                "note: stream estimate: downstream reports a lower bound at "
                "episodes≈sessions (LLM refinement only adds segments)"))
        side = "report and trace only" if cfg.trace.enabled else "report only"
        self._console.print(self._Text(
            f"no LLM calls made, no output written ({side})"))

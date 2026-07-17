"""v1.10 — the three-mode console face (spec §7.7 / 3.12.3; SPEC-tui-console U1–U27).

``ConsoleRenderer`` is the CLI-layer implementation of the common-layer
``ProgressListener`` protocol (U19) and the ONLY production touchpoint of the
``rich`` dependency (U4 — lazy import inside :meth:`on_run_context`, never at
module level). The CLI constructs it as a LAZY SHELL (no cfg yet) and always
passes it into ``execute_run``; ``on_run_context`` self-configures it into one
of three forms:

- **rich** (``cfg.console.mode_resolved == "rich"``): the two-region inline
  live panel (U1) — logs keep scrolling above, a throttled canvas repaints
  below. Six blocks per spec §3.2/§7.7: header, batch progress, stage board
  (bracket-attributed llm.call numerators / ``estimate_run`` denominators,
  U20), nine-state account (stitched/threads = the U18 bounded revision),
  LLM usage + key pool + breaker from ``LLMClient.snapshot()`` (one pull per
  paint), keyboard hints / interrupt banner.
- **plain heartbeat** (plain ∧ ``heartbeat_s > 0`` ∧ stderr non-TTY, U14): one
  data-free line every N seconds, fixed self-advancing deadline (no drift).
- **inert** (everything else): every callback returns immediately —
  allocation-free; plain output stays byte-owned by the M11 emitter.

Timing model (U26): ``Live(auto_refresh=False)`` — no rich refresh thread —
plus an **asyncio-task tick** created on Live start (inside the running loop):
``await asyncio.sleep(1/refresh_hz)`` → ``_maybe_refresh()`` (keyboard poll +
throttled repaint). The task guarantees liveness during event silence (a
single long LLM call can stall events up to ``timeout_s`` — clock, ETA and
keys stay responsive regardless). The five callbacks additionally do O(1)
accumulation then the same throttled ``_maybe_refresh()`` (stage transitions /
stop / run.end always paint), so the paint cadence is ≤ refresh_hz either
way. Zero new threads; when no loop is running (offline snapshot tests drive
callbacks synchronously) the task is skipped and the callback throttle alone
paces repaints. Keyboard polling is a non-blocking zero-timeout ``select``
riding ``_maybe_refresh``.

Heartbeat timing (U14) uses ``loop.call_later`` armed at run.start: the fixed
deadline self-advances in ``heartbeat_s`` steps (catch-up loop, no drift) and
fires independently of event arrival — silence is exactly the heartbeat's
target scenario. Sans a running loop the callbacks' deadline checks are the
fallback pacing. Disarmed at run.end.

Log takeover (R1): on Live start the handler marked ``_labelkit_handler`` on
logger ``labelkit`` has its stream swapped to a :class:`_LiveLogStream` proxy
that prints byte-preserved lines through ``live.console`` (they scroll above
the canvas); every stop path restores the original stream in ``finally``.

Keyboard (U15, §3.4): active iff rich-live-started ∧ stdin TTY ∧
``console.interactive`` ∧ termios importable. ``tty.setcbreak`` (keeps ISIG —
Ctrl-C semantics untouched); closed key set ``? h l e + - p q``; termios
restored with ``TCSADRAIN`` on every stop path. The ``l`` expanded view shows
env / state / calls / rate_limited per key (``KeySnapshot``, spec 3.9.2 —
usage joined from the per-key accumulators). ``q`` detaches: the run's
remaining plain progress lines
and the text final summary are OWNED by this renderer (the emitter is
statically gated off under ``mode_resolved == "rich"``, U21), rendered through
the shared ``console_format`` pure functions — byte-identical to plain.

Failure semantics (U7 red line × U21 plain ownership): every callback body is
exception-guarded — one WARN 「console 渲染异常，已降级 plain: …」, Live
stopped safely (log stream + termios restored), then the renderer lands in the
DETACHED-PLAIN state whenever ``mode_resolved == "rich"`` (the emitter is
statically gated off there, so the renderer must keep printing the plain
progress line and the text final summary through ``console_format`` — same as
the ``q`` path; CONTRACTS §7.10). Outside rich ownership (heartbeat mode) it
goes inert — the emitter still owns plain. A second failure goes fully inert.
It never raises, so exit codes and data output are untouched by construction.

Information discipline (U6, mechanism-backed by U22): ``on_event`` payloads
arrive pre-redacted at tier "none"; this module additionally renders ONLY
closed-vocabulary / count / enum / env-name fields — never record ids, never
free text, never payload values outside the frozen key sets below.
"""
from __future__ import annotations

import asyncio
import logging
import os
import select
import sys
import time
from collections import deque
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
    from labelkit.common.runtime.llm_client import ProfileSnapshot

__all__ = ["ConsoleRenderer"]

_logger = logging.getLogger("labelkit.console")

# Module-level clock alias so offline tests can inject a fake monotonic clock.
_monotonic = time.monotonic

# The orchestrator's canonical chain order (spec §2.3 / _compose_chain).
_CHAIN_ORDER = ("segment", "stitch", "dedup", "classify", "extract",
                "quality", "generate", "annotate", "verify")
# Stage → estimate_run() denominator key (U20). dedup makes no LLM calls.
_STAGE_CALL_KEYS: dict[str, str] = {
    "segment": "segment_calls",
    "stitch": "stitch_calls",
    "classify": "classify_calls",
    "extract": "extract_calls",
    "quality": "quality_calls",
    "generate": "generate_calls",
    "annotate": "annotate_calls",
    "verify": "verify_calls",
}
_ESTIMATE_CALL_KEYS = ("generate_calls", "segment_calls", "stitch_calls",
                       "classify_calls", "extract_calls", "quality_calls",
                       "annotate_calls", "verify_calls")

_BAR_CELLS = 24                    # batch progress bar width (§3.2 mockup)
_NARROW_COLS = 60                  # < 60 cols → single-line degradation (§3.1)
_ERROR_RING = 5                    # 'e' strip: last five error events (§3.4)
_HINT_LINE = " [?]帮助 [l]LLM展开 [e]错误条 [p]暂停 [q]脱离"
_HELP_LINE = (" 键位  ?/h 帮助   l LLM展开（每密钥一行）   e 最近错误条   "
              "+/- 画布行数(4–16)   p 暂停重绘   q 脱离（余下运行降级 plain）")


def _mmss(seconds: float) -> str:
    m, s = divmod(max(int(seconds), 0), 60)
    return f"{m:02d}:{s:02d}"


def _fmt_tok(n: int) -> str:
    """k/M token abbreviations (§3.2 mockup: 412k↑ 96k↓)."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _class_overrides_exist(cfg: "ResolvedConfig") -> bool:
    """Mirror of Orchestrator._class_overrides_exist (R28 dry-run note gate)."""
    return any(view.quality != cfg.quality or view.rubric != cfg.rubric
               or view.annotate != cfg.annotate or view.generate != cfg.generate
               or view.verify != cfg.verify or view.extract != cfg.extract
               for view in cfg.class_views.values())


class _LiveLogStream:
    """R1 log takeover shim: a tiny file-like whose writes buffer until newline,
    then print byte-preserved through the Live console (lines scroll above the
    canvas; the handler's Formatter is untouched, so log text stays identical
    to plain). ``flush`` is a no-op — the console owns its own flushing."""

    def __init__(self, console: Any, text_cls: Any):
        self._console = console
        self._text = text_cls
        self._buf = ""

    def write(self, data: str) -> int:
        self._buf += data
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._console.print(self._text(line), markup=False, highlight=False)
        return len(data)

    def flush(self) -> None:
        return None


class ConsoleRenderer:
    """Lazy-shell ProgressListener implementation (duck-typed, spec 3.12.3).

    The constructor takes nothing (the CLI has no cfg before load);
    ``_console_factory`` is a PRIVATE test hook returning the rich Console to
    render into (offline snapshot tests inject ``Console(width=100,
    force_terminal=True, file=StringIO())``). All state is plain ints / dicts /
    deques — single-threaded asyncio discipline, no locks (U26)."""

    def __init__(self, *, _console_factory: Callable[[], Any] | None = None):
        self._console_factory = _console_factory
        self._mode = "inert"       # inert | rich | heartbeat | detached
        self._dead = False         # U7 one-way degradation latch
        self._cfg: "ResolvedConfig | None" = None
        self._snapshot: Callable[[], tuple] | None = None
        self._counters: Callable[[], Mapping[str, int]] | None = None
        self._fatal_streak: Callable[[], int] | None = None

        # header facts (frozen at on_run_context)
        self._chain: tuple[str, ...] = ()
        self._mode_badge = ""
        self._dry = False
        self._generate_only = False

        # rich machinery (populated by _activate_rich)
        self._console: Any = None
        self._live: Any = None
        self._live_started = False
        self._interval = 0.2
        self._last_paint = 0.0
        self._tick_task: Any = None    # U26 asyncio tick (None sans running loop)
        self._Text: Any = None
        self._Group: Any = None
        self._Live: Any = None
        self._Table: Any = None
        self._log_handler: logging.StreamHandler | None = None
        self._log_stream_orig: Any = None

        # keyboard (U15)
        self._kbd_active = False
        self._kbd_fd = -1
        self._termios_mod: Any = None
        self._tty_saved: Any = None
        self._show_help = False
        self._show_keys = False
        self._show_errors = False
        self._paused = False
        self._max_lines: int | None = None    # None = adaptive (Live crops)

        # run accumulators (all O(1) per event)
        self._run_id = "-"
        self._t0: float | None = None
        self._est: dict | None = None
        self._batch_no = 0
        self._cur_batch_size = 0
        self._records_seen = 0     # Σ batch.start.size
        self._records_done = 0     # Σ batch.end batch sizes
        self._ema_rate: float | None = None    # records/s EMA (ETA, §3.2)
        self._stages_seen: set[str] = set()
        self._current_stage: str | None = None
        self._stage_calls: dict[str, int] = {}     # bracket attribution (U20)
        self._gen_calls = 0        # generate_only pre-batch phase calls
        self._open_stage: tuple[str, float] | None = None
        self._stage_seconds: dict[str, float] = {}  # freeze bars (approximate)
        self._acc = {"emitted": 0, "dup": 0, "lowq": 0, "verify": 0,
                     "failed": 0, "noise": 0, "absorbed": 0, "stitched": 0,
                     "threads": 0}
        self._errors: deque[str] = deque(maxlen=_ERROR_RING)
        self._stop_requested = False
        self._breaker_seen = False    # any llm.call status="breaker_aborted"

        # detached plain output (post-q ownership, U21)
        self._plain_progress_active = False

        # heartbeat (U14)
        self._hb_s = 0
        self._hb_t0 = 0.0
        self._hb_next: float | None = None
        self._hb_handle: Any = None    # loop.call_later timer (None sans loop)
        self._hb_batch = 0
        self._hb_stage = "-"
        self._hb_calls = 0

    # ── ProgressListener callbacks (all guarded, U7) ──────────────────────

    def on_run_context(self, cfg: "ResolvedConfig",
                       snapshot: "Callable[[], tuple[ProfileSnapshot, ...]]",
                       counters: "Callable[[], Mapping[str, int]]",
                       fatal_streak: "Callable[[], int]") -> None:
        if self._dead:
            return
        try:
            self._cfg = cfg
            self._snapshot = snapshot
            self._counters = counters
            self._fatal_streak = fatal_streak
            self._dry = cfg.dry_run
            self._generate_only = cfg.run.mode == "generate_only"

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
                # generate_only runs generation as phase 0 (never via
                # stage_begin); the board shows the re-flow chain only, the
                # batch block carries the 生成 phase line (§3.2).
                enabled["generate"] = False
            self._chain = tuple(n for n in _CHAIN_ORDER if enabled[n])
            badge = f"{cfg.run.mode}/{cfg.run.modality}"
            if cfg.segment.enabled:
                badge += "/stream" + ("+stitch" if cfg.stitch.enabled else "")
            self._mode_badge = badge

            if cfg.console.mode_resolved == "rich":
                self._activate_rich()
            elif cfg.console.heartbeat_s > 0 and not sys.stderr.isatty():
                self._hb_s = cfg.console.heartbeat_s
                self._mode = "heartbeat"
            # else: stays inert — plain output belongs to the emitter.
        except Exception as exc:  # noqa: BLE001 — U7: never raise
            self._degrade(exc)

    def on_estimate(self, est: Mapping) -> None:
        if self._mode in ("inert", "detached"):
            return
        try:
            if self._mode == "heartbeat":
                self._hb_check()
                return
            self._est = dict(est)
            if self._dry:
                # U13: dry-run rich renders the two static estimate tables
                # immediately (no Live ever starts on the dry path).
                self._render_estimate_tables()
                return
            self._maybe_refresh()
        except Exception as exc:  # noqa: BLE001
            self._degrade(exc)

    def on_event(self, ev: "TraceEvent") -> None:
        if self._mode == "inert":
            return
        try:
            if self._mode == "heartbeat":
                self._hb_event(ev)
                return
            if self._mode == "detached":
                self._detached_event(ev)
                return
            # rich
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
                self._batch_no = ev.batch_no   # emitter progress-line semantics
                self._absorb_batch_end(ev.payload)
                self._maybe_refresh()
                return
            if name == EV_LLM_CALL:
                if self._current_stage is not None:
                    self._stage_calls[self._current_stage] = (
                        self._stage_calls.get(self._current_stage, 0) + 1)
                elif self._generate_only and self._batch_no == 0:
                    self._gen_calls += 1
                if ev.payload.get("status") == "breaker_aborted":
                    # v1.6 hard trip (auth 401/403) opens the breaker below the
                    # streak threshold; breaker_aborted is the only precise
                    # breaker-open signal the frozen U19 protocol carries.
                    self._breaker_seen = True
                self._maybe_refresh()
                return
            if name == EV_ERROR:
                # U6: stage + kind only — closed vocabulary (§7.6), never the
                # message/free-text fields.
                kind = ev.payload.get("kind", "?")
                self._errors.append(f"{ev.stage}·{kind}")
            self._maybe_refresh()
        except Exception as exc:  # noqa: BLE001
            self._degrade(exc)

    def on_stage(self, stage: str, batch_no: int) -> None:
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
            self._maybe_refresh(force=True)    # stage transitions always paint
        except Exception as exc:  # noqa: BLE001
            self._degrade(exc)

    def on_stop_requested(self) -> None:
        if self._mode in ("inert", "heartbeat", "detached"):
            return
        try:
            self._stop_requested = True
            self._paint_now()          # the interrupt banner overrides 'p' pause
        except Exception as exc:  # noqa: BLE001
            self._degrade(exc)

    # ── rich activation / live lifecycle ──────────────────────────────────

    def _activate_rich(self) -> None:
        """Lazy-import rich (U4 single touchpoint). ImportError → one WARN and
        permanent plain-inert (mode_resolved probed find_spec only, so this
        covers a broken install)."""
        try:
            from rich.console import Console, Group
            from rich.live import Live
            from rich.table import Table
            from rich.text import Text
        except ImportError as exc:
            _logger.warning("console: rich 导入失败，已降级 plain: %s", exc,
                            extra={"stage": "run", "batch": 0})
            self._mode = "inert"
            return
        self._Group, self._Live, self._Table, self._Text = Group, Live, Table, Text
        self._console = (self._console_factory() if self._console_factory
                         is not None else Console(stderr=True, soft_wrap=False))
        self._interval = 1.0 / max(self._cfg.console.refresh_hz, 1)
        self._mode = "rich"

    def _start_live(self) -> None:
        """First run.start: start the Live canvas (validate / dry-run paths
        never reach here, U13), take over the log stream (R1), enter cbreak."""
        if self._live_started:
            return
        self._live = self._Live(
            self._render(),
            console=self._console,
            auto_refresh=False,            # U26: no rich refresh thread
            redirect_stdout=False,
            redirect_stderr=False,
            transient=False,               # U8: the final frame stays
        )
        self._live.start()
        self._live_started = True
        self._take_log_stream()
        self._setup_keyboard()
        self._start_tick()                 # U26 asyncio tick (liveness in silence)
        self._paint_now()                  # initial frame; no key poll yet

    def _start_tick(self) -> None:
        """U26: the pinned asyncio-task tick — guarantees clock/ETA/keyboard
        liveness during event silence (one long LLM call can stall callbacks
        up to timeout_s). Sans a running loop (offline snapshot tests drive
        callbacks synchronously) the callback throttle alone paces repaints."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._tick_task = None
            return
        self._tick_task = loop.create_task(self._tick_loop())

    async def _tick_loop(self) -> None:
        try:
            while self._mode == "rich" and self._live_started:
                await asyncio.sleep(self._interval)
                self._maybe_refresh()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 — U7: the tick never raises out
            self._degrade(exc)

    def _stop_tick(self) -> None:
        task = self._tick_task
        self._tick_task = None
        if task is not None and not task.done():
            task.cancel()

    def _take_log_stream(self) -> None:
        logger = logging.getLogger("labelkit")
        for handler in logger.handlers:
            if (getattr(handler, "_labelkit_handler", False)
                    and isinstance(handler, logging.StreamHandler)):
                proxy = _LiveLogStream(self._live.console, self._Text)
                try:
                    self._log_stream_orig = handler.setStream(proxy)
                except (ValueError, OSError):
                    # setStream flushes the OLD stream before swapping — a
                    # closed/broken stream raises there and leaves the handler
                    # untouched. Retarget directly (flush-free); the dead
                    # stream must not cost us the panel (U7 spirit).
                    self._log_stream_orig = handler.stream
                    handler.stream = proxy
                self._log_handler = handler
                break

    def _restore_log_stream(self) -> None:
        if self._log_handler is not None and self._log_stream_orig is not None:
            try:
                self._log_handler.setStream(self._log_stream_orig)
            except Exception:  # noqa: BLE001 — restore must never raise
                pass
        self._log_handler = None
        self._log_stream_orig = None

    def _setup_keyboard(self) -> None:
        """U15 activation conjunction; any failure ⇒ render-only (no cbreak)."""
        if self._cfg is None or not self._cfg.console.interactive:
            return
        try:
            import termios
            import tty
        except ImportError:
            return
        try:
            if not sys.stdin.isatty():
                return
            fd = sys.stdin.fileno()
            self._tty_saved = termios.tcgetattr(fd)
            tty.setcbreak(fd)              # clears ECHO|ICANON, KEEPS ISIG
            self._termios_mod = termios
            self._kbd_fd = fd
            self._kbd_active = True
        except Exception:  # noqa: BLE001 — exotic ttys: fall back to render-only
            self._kbd_active = False

    def _restore_termios(self) -> None:
        if self._termios_mod is not None and self._tty_saved is not None:
            try:
                self._termios_mod.tcsetattr(
                    self._kbd_fd, self._termios_mod.TCSADRAIN, self._tty_saved)
            except Exception:  # noqa: BLE001
                pass
        self._kbd_active = False
        self._termios_mod = None
        self._tty_saved = None

    def _stop_live(self) -> None:
        """Every stop path funnels here: tick cancelled, Live stopped, log
        stream + termios restored in finally (§3.4 terminal-state discipline)."""
        self._stop_tick()
        try:
            if self._live is not None and self._live_started:
                self._live.stop()
        finally:
            self._live_started = False
            self._restore_log_stream()
            self._restore_termios()

    def _degrade(self, exc: BaseException) -> None:
        """U7 × U21: one WARN + safe teardown, then land where plain ownership
        dictates (CONTRACTS §7.10) — under ``mode_resolved == "rich"`` the
        emitter is statically gated off, so this renderer must keep printing
        the plain progress line / text summary: degradation goes DETACHED
        (same output path as ``q``). Outside rich ownership (heartbeat/inert)
        the emitter owns plain — go inert. A SECOND failure (``_dead`` latch,
        e.g. the detached stderr writes themselves failing) goes fully inert
        silently. Never raises."""
        if self._dead:
            self._mode = "inert"
            self._disarm_heartbeat()
            return
        self._dead = True
        owns_plain = (self._cfg is not None
                      and self._cfg.console.mode_resolved == "rich")
        try:
            self._stop_live()
        except Exception:  # noqa: BLE001
            pass
        self._mode = "detached" if owns_plain else "inert"
        if self._mode == "inert":
            self._disarm_heartbeat()
        try:
            _logger.warning("console 渲染异常，已降级 plain: %s", exc,
                            extra={"stage": "run", "batch": 0})
        except Exception:  # noqa: BLE001
            pass

    # ── repaint throttle + keyboard poll (U26 degenerate tick) ────────────

    def _maybe_refresh(self, force: bool = False) -> None:
        if self._mode != "rich" or not self._live_started:
            return
        if self._kbd_active:
            self._poll_keys()
            if self._mode != "rich" or not self._live_started:
                return                     # 'q' detached during the poll
        if self._paused:
            # 'p' freezes the canvas entirely (copy/paste friendliness, §3.4);
            # key toggles / interrupt banner / final freeze go via _paint_now.
            return
        now = _monotonic()
        if force or now - self._last_paint >= self._interval:
            self._last_paint = now
            self._live.update(self._render(), refresh=True)

    def _paint_now(self) -> None:
        """Immediate repaint bypassing throttle AND 'p' pause — key-toggle
        feedback, the interrupt banner, and the settled final frame."""
        if self._mode != "rich" or not self._live_started:
            return
        self._last_paint = _monotonic()
        self._live.update(self._render(), refresh=True)

    def _poll_keys(self) -> None:
        while True:
            try:
                ready, _, _ = select.select([self._kbd_fd], [], [], 0)
            except (OSError, ValueError):
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
        """Closed key set (§3.4); everything else is ignored. Every handled
        toggle repaints immediately (visual feedback even while paused)."""
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
        """'q' (§3.4): leave the panel; the rest of the run renders the plain
        progress line / final summary through console_format (the emitter is
        statically gated off under rich — the renderer owns plain now, U21)."""
        self._stop_live()
        self._mode = "detached"

    # ── event absorption ──────────────────────────────────────────────────

    def _absorb_batch_end(self, payload: Mapping) -> None:
        """batch.end payloads are the authoritative post-emit account (§3.2:
        emitted 分量 = batch.end.active — post-emit identity)."""
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
        # ETA (§3.2): EMA of records/s over batch.end events.
        duration_ms = int(payload.get("duration_ms", 0))
        if duration_ms > 0 and self._cur_batch_size > 0:
            rate = self._cur_batch_size / (duration_ms / 1000.0)
            self._ema_rate = (rate if self._ema_rate is None
                              else 0.4 * rate + 0.6 * self._ema_rate)
        # close the open stage interval — emit time folds into the last stage
        # (freeze bars are labeled approximate).
        if self._open_stage is not None:
            prev, t = self._open_stage
            self._stage_seconds[prev] = (self._stage_seconds.get(prev, 0.0)
                                         + (_monotonic() - t))
            self._open_stage = None

    def _finish(self, ev: "TraceEvent") -> None:
        """run.end (U8): final settled frame, then stop leaving it in the
        scrollback (transient=False). Dry-run rich prints nothing extra."""
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
        self._mode = "inert"               # run over; stray events stay cheap

    # ── detached plain output (post-q, U21 ownership) ─────────────────────

    def _detached_event(self, ev: "TraceEvent") -> None:
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
        return {"dropped_dup": self._acc["dup"], "dropped_lowq": self._acc["lowq"],
                "dropped_verify": self._acc["verify"], "failed": self._acc["failed"]}

    # ── heartbeat (plain non-TTY, U14) ────────────────────────────────────

    def _hb_event(self, ev: "TraceEvent") -> None:
        name = ev.ev
        if name == EV_RUN_START:
            self._hb_t0 = _monotonic()
            self._hb_next = self._hb_t0 + self._hb_s
            self._arm_hb_timer()           # U14: loop.call_later — fires in silence
            return
        if name == EV_RUN_END:
            self._disarm_heartbeat()       # renderer is done
            self._mode = "inert"
            return
        if name == EV_BATCH_START:
            self._hb_batch = ev.batch_no
        elif name == EV_LLM_CALL:
            self._hb_calls += 1
        self._hb_check()

    def _arm_hb_timer(self) -> None:
        """U14 timing owner: a ``loop.call_later`` timer armed on the fixed
        deadline — beats fire during event silence (silence is exactly the
        heartbeat's target scenario). Sans a running loop (offline tests drive
        callbacks synchronously) the per-callback ``_hb_check`` is the
        fallback pacing."""
        if self._hb_next is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._hb_handle = None
            return
        delay = max(self._hb_next - _monotonic(), 0.0)
        self._hb_handle = loop.call_later(delay, self._hb_fire)

    def _hb_fire(self) -> None:
        try:
            self._hb_check()
            self._arm_hb_timer()           # re-arm on the advanced deadline
        except Exception:  # noqa: BLE001 — a timer bug never hurts the run
            self._disarm_heartbeat()

    def _disarm_heartbeat(self) -> None:
        self._hb_next = None
        handle = self._hb_handle
        self._hb_handle = None
        if handle is not None:
            handle.cancel()

    def _hb_check(self) -> None:
        """Fixed-deadline heartbeat (U14): one data-free line, deadline
        self-advances in heartbeat_s steps (catch-up loop, no drift)."""
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

    # ── canvas rendering (spec §3.2 six blocks) ───────────────────────────

    def _line(self, s: str, style: str | None = None) -> Any:
        """One canvas line. Over-width lines WRAP (rich default) rather than
        crop — no count is ever lost to a narrow-ish terminal, matching how a
        real terminal wraps the plain progress line."""
        return self._Text(s, style=style or "")

    def _render(self) -> Any:
        cfg = self._cfg
        snap = tuple(self._snapshot()) if self._snapshot is not None else ()
        counters = dict(self._counters()) if self._counters is not None else {}
        width = self._console.size.width

        if width < _NARROW_COLS:
            # §3.1 degradation row: the canvas collapses to the plain
            # single-line form (sans the leading \r).
            return self._line(console_format.format_progress_line(
                self._batch_no, self._acc["emitted"],
                self._progress_totals())[1:])

        streak = self._fatal_streak() if self._fatal_streak is not None else 0
        threshold = cfg.run.fatal_error_threshold
        breaker_open = self._breaker_open(snap, streak, threshold)

        lines: list[Any] = [self._line("─" * min(width, 100), style="dim")]
        if breaker_open:
            lines.append(self._line(" ⚠ 熔断已打开", style="bold red"))
        if self._stop_requested:
            lines.append(self._line(" 正在优雅中断（≤30s）…", style="bold yellow"))

        # header (block 1)
        elapsed = _monotonic() - self._t0 if self._t0 is not None else 0.0
        head = (f" labelkit run · {self._run_id} · {self._mode_badge}"
                f" · seed {cfg.run.seed} · 已用 {_mmss(elapsed)}")
        eta = self._eta_seconds()
        if eta is not None:
            head += f" · ETA ~{_mmss(eta)}"
        lines.append(self._line(head, style="bold"))
        lines.append(self._line(
            f" project {cfg.project_path} → {cfg.run.output}", style="dim"))
        lines.append(self._line(""))

        # batch progress (block 2)
        lines.append(self._line(self._batch_line(counters)))
        lines.append(self._line(""))

        # stage board (block 3)
        lines.append(self._line(self._board_line()))
        lines.append(self._line(""))

        # status account (block 4)
        lines.append(self._line(self._account_line(counters)))
        lines.append(self._line(""))

        # LLM block (block 5)
        lines.extend(self._llm_lines(snap, streak, threshold, breaker_open))

        # keyboard hints / toggled panes (block 6)
        if self._show_errors:
            strip = "  |  ".join(self._errors) if self._errors else "（无）"
            lines.append(self._line(f" 错误 {strip}"))
        if self._kbd_active:
            hint = _HINT_LINE + ("  ⏸" if self._paused else "")
            lines.append(self._line(hint, style="dim"))
        if self._show_help:
            lines.append(self._line(_HELP_LINE, style="dim"))

        if self._max_lines is not None:
            lines = lines[: self._max_lines]
        return self._Group(*lines)

    def _eta_seconds(self) -> float | None:
        if (self._est is None or self._ema_rate is None or self._ema_rate <= 0
                or self._est.get("records") is None):
            return None
        remaining = max(int(self._est["records"]) - self._records_done, 0)
        return remaining / self._ema_rate

    def _batch_line(self, counters: Mapping[str, int]) -> str:
        est = self._est
        if self._generate_only and self._batch_no == 0:
            # 生成 phase form (§3.2): calls live from llm.call; 已产 updates
            # once at phase end (counts.generated is a phase-end meter).
            total = est.get("generate_calls") if est else None
            calls = f"{self._gen_calls}/{total}" if total is not None else str(self._gen_calls)
            produced = counters.get("counts.generated", 0)
            return f" 生成 ▶ calls {calls} · 已产 {produced} 条"
        n_batches = est.get("batches") if est else None
        seg = f" 批 {self._batch_no}"
        if n_batches:
            seg += f"/{n_batches}"
            est_records = int(est.get("records", 0))
            frac = (min(self._records_seen / est_records, 1.0) if est_records
                    else min(self._batch_no / n_batches, 1.0))
            filled = round(frac * _BAR_CELLS)
            seg += "  " + "█" * filled + "░" * (_BAR_CELLS - filled)
        seg += f"  记录 {self._records_seen}"
        if est and est.get("records") is not None:
            seg += f"/{est['records']}"
        seg += " (scanned)"
        return seg

    def _board_line(self) -> str:
        parts = []
        for name in self._chain:
            call_key = _STAGE_CALL_KEYS.get(name)
            if name == self._current_stage:
                if call_key is None:               # dedup: no LLM calls
                    parts.append(f"{name} ▶")
                else:
                    a = self._stage_calls.get(name, 0)
                    denom = self._est.get(call_key) if self._est else None
                    parts.append(f"{name} ▶ {a}/{denom}" if denom is not None
                                 else f"{name} ▶ {a}")
            elif name in self._stages_seen:
                parts.append(f"{name} ✓")
            else:
                parts.append(f"{name} ·")
        return " 段  " + "   ".join(parts)

    def _account_line(self, counters: Mapping[str, int]) -> str:
        acc = self._acc
        seg = (f" 账  emitted {acc['emitted']}   dup {acc['dup']}   "
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
                    f"{prefix}{s.name:<{name_w}}  在途 {s.in_flight}/"
                    f"{s.max_concurrency}  calls {s.calls}  重试 {s.retries}  "
                    f"tok {_fmt_tok(s.prompt_tokens)}↑ "
                    f"{_fmt_tok(s.completion_tokens)}↓  {cost}  p50 {p50}"))
            if self._show_keys:
                # 'l' expanded (§3.4): one line per key — env, state, and the
                # per-key usage mirror (calls / rate_limited, spec 3.9.2).
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
                lines.append(self._line(f"      密钥 {keys_seg}"))
        lines.append(self._line(f"      熔断 {streak}/{threshold}",
                                style="bold red" if breaker_open else ""))
        return lines

    @staticmethod
    def _key_state(k: Any) -> str:
        if k.state == "ok":
            return "ok"
        if k.state == "cooldown":
            return f"冷却{k.cooldown_remaining_s}s"
        return "禁用"

    def _breaker_open(self, snap: tuple, streak: int, threshold: int) -> bool:
        """Open = the streak reached the threshold, OR a v1.6 hard trip — the
        auth-class immediate break fires BELOW the threshold and is visible
        through its two read-only footprints: breaker_aborted llm.call events,
        and a profile whose whole key pool is disabled (the P2-3 bad-key
        scenario: 面板须在 10 秒内红出密钥禁用与熔断横幅, §3.7)."""
        pool_dead = any(s.keys and all(k.state == "disabled" for k in s.keys)
                        for s in snap)
        return streak >= threshold or self._breaker_seen or pool_dead

    # ── final settled frame (U8) ──────────────────────────────────────────

    def _render_final(self, counts: dict) -> Any:
        cfg = self._cfg
        snap = tuple(self._snapshot()) if self._snapshot is not None else ()
        width = self._console.size.width
        elapsed = _monotonic() - self._t0 if self._t0 is not None else 0.0

        parts: list[Any] = [self._line("─" * min(width, 100), style="dim")]
        parts.append(self._line(
            f" labelkit run 完成 · {self._run_id} · {self._mode_badge}"
            f" · 用时 {_mmss(elapsed)}", style="bold"))

        # Table captions ride as standalone full-width lines — rich wraps a
        # Table's own title to the TABLE width, mangling CJK captions.
        parts.append(self._line(" counts（= report.counts）", style="bold"))
        counts_table = self._Table()
        counts_table.add_column("键")
        counts_table.add_column("值", justify="right")
        for key, value in counts.items():
            counts_table.add_row(str(key), str(value))
        parts.append(counts_table)

        if self._stage_seconds:
            parts.append(self._line(
                " 段耗时（近似：on_stage 转换间隔累加，非 report 计时）",
                style="dim"))
            max_s = max(self._stage_seconds.values()) or 1.0
            for name in self._chain:
                sec = self._stage_seconds.get(name)
                if sec is None:
                    continue
                bar = "█" * max(1, round(sec / max_s * 20))
                parts.append(self._line(f" {name:<9} {bar} {sec:.1f}s"))

        if snap:
            parts.append(self._line(" llm_usage", style="bold"))
            usage = self._Table()
            for col in ("profile", "calls", "重试", "tok↑", "tok↓", "成本", "p50"):
                usage.add_column(col, justify="right" if col != "profile" else "left")
            for s in snap:
                usage.add_row(
                    s.name, str(s.calls), str(s.retries),
                    _fmt_tok(s.prompt_tokens), _fmt_tok(s.completion_tokens),
                    f"${s.est_cost_usd:.2f}" if s.est_cost_usd is not None else "—",
                    f"{s.p50_latency_ms / 1000:.1f}s"
                    if s.p50_latency_ms is not None else "—")
            parts.append(usage)

        stem = str(Path(cfg.run.output).with_suffix(""))
        if cfg.output.rejects != "none":
            parts.append(self._line(f" rejects → {stem}.rejects.jsonl"))
        if cfg.trace.enabled:
            parts.append(self._line(f" trace → {cfg.trace.path}"))
        return self._Group(*parts)

    # ── dry-run estimate tables (U13) ─────────────────────────────────────

    def _render_estimate_tables(self) -> None:
        """Rich dry-run face: two static tables + the plain lines' footnotes —
        the SAME information as the plain byte-anchored lines (values from the
        same estimate_run dict), table-shaped."""
        cfg = self._cfg
        est = self._est or {}
        t1 = self._Table()
        t1.add_column("项")
        t1.add_column("值", justify="right")
        t1.add_row("mode", cfg.run.mode)
        t1.add_row("estimated_records", str(est.get("records", 0)))
        t1.add_row("batches", str(est.get("batches", 0)))

        t2 = self._Table()
        t2.add_column("stage")
        t2.add_column("calls", justify="right")
        for key in _ESTIMATE_CALL_KEYS:
            t2.add_row(key, str(est.get(key, 0)))
        t2.add_row("total", str(est.get("total_calls", 0)))

        # Captions as standalone lines (a rich Table title wraps to the table
        # width — CJK captions would fold mid-word).
        self._console.print(self._Text("dry-run 估算", style="bold"))
        self._console.print(t1)
        self._console.print(self._Text("估算 LLM 调用（不含重试与修复调用）",
                                       style="bold"))
        self._console.print(t2)
        if cfg.classify.enabled and (cfg.classify.assignment == "multi"
                                     or _class_overrides_exist(cfg)):
            self._console.print(self._Text(
                "注：按全局配置估算 / multi 按标签乘数 1 报下界"))
        if cfg.segment.enabled and cfg.segment.strategy in ("llm", "hybrid"):
            self._console.print(self._Text(
                "注：stream 估算：下游按 episodes≈sessions 报下界"
                "（LLM 精化只增段数）"))
        side = "report and trace only" if cfg.trace.enabled else "report only"
        self._console.print(self._Text(
            f"no LLM calls made, no output written ({side})"))

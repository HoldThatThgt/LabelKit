"""Offline tests for the v1.10 ConsoleRenderer (spec §7.7 / §7.8 console row,
SPEC-tui-console §3.7).

Fixed-width snapshot rendering per spec: the renderer gets an injected
``rich.Console(width=100, force_terminal=True)`` writing into a StringIO (the
``_console_factory`` private hook); state is fed through the five
ProgressListener callbacks with MetricsSink-shaped counter payloads — never
LLM responses (the real-LLM testing directive stays untouched). ``no_color``
is enabled on the injected console purely to keep the asserted text free of
SGR escapes — the U25-sanctioned color-strip that keeps layout intact.

The keyboard test is a REAL pty (stdlib ``pty`` + subprocess): it asserts
cbreak entry (ICANON/ECHO cleared, ISIG kept — Ctrl-C semantics intact) and
byte-identical termios restoration after the ``q`` detach (§3.4 discipline).
"""
from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

import labelkit.cli.console as console_mod
from labelkit.cli.console import ConsoleRenderer
from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ConsoleConfig,
    Criterion,
    DedupConfig,
    ExtractConfig,
    GenerateConfig,
    InputConfig,
    LLMProfile,
    OutputConfig,
    QualityConfig,
    ResolvedConfig,
    Rubric,
    RunConfig,
    SegmentConfig,
    StitchConfig,
    StreamConfig,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)
from labelkit.common.observability import console_format
from labelkit.common.observability.obslog import TraceEvent
from labelkit.common.runtime.llm_client import KeySnapshot, ProfileSnapshot

RICH = ConsoleConfig(mode="rich", mode_resolved="rich")


def _cfg(console: ConsoleConfig | None = None, **kw) -> ResolvedConfig:
    base = dict(
        tool=ToolConfig(),
        console=console if console is not None else ConsoleConfig(),
        llm_profiles={"default": LLMProfile(
            name="default", provider="anthropic", base_url="https://x",
            model="m", api_key_env="LABELKIT_KEY_A")},
        embedding_profiles={},
        run=RunConfig(output="out/labels.jsonl", modality="text", input="in.jsonl"),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(),
        stitch=StitchConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(instruction="标注"),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=TraceConfig(),
        rubric=Rubric(name="r", criteria=(
            Criterion(key="c1", description="d", pairwise_prompt="p"),)),
        class_views={},
        user_schema={"type": "object"},
        limit=None, strict=False, dry_run=False,
        config_path="config.toml", project_path="examples/x/project.toml",
        config_digest="sha256:0", project_digest="sha256:0",
    )
    base.update(kw)
    return ResolvedConfig(**base)


def _ev(name: str, *, batch: int = 0, stage: str = "run",
        payload: dict | None = None) -> TraceEvent:
    return TraceEvent(ts="2026-07-17T00:00:00.000+08:00", run_id="f3a9c04b7d21",
                      batch_no=batch, stage=stage, ev=name, record_ids=(),
                      payload=payload or {})


def _rich_renderer(cfg: ResolvedConfig, *, width: int = 100,
                   snapshot=None, counters=None, fatal_streak=None
                   ) -> tuple[ConsoleRenderer, io.StringIO]:
    """Spec §3.7 fixture: injected fixed-width Console into a StringIO."""
    from rich.console import Console

    buf = io.StringIO()
    # NOTE: Console.size only honors explicit dimensions when BOTH width and
    # height are pinned (otherwise it probes the file, and a StringIO probes
    # to the 80×25 fallback) — the narrow-degradation branch reads size.width.
    renderer = ConsoleRenderer(_console_factory=lambda: Console(
        width=width, height=40, force_terminal=True, no_color=True, file=buf))
    renderer.on_run_context(cfg,
                            snapshot if snapshot is not None else (lambda: ()),
                            counters if counters is not None else (lambda: {}),
                            fatal_streak if fatal_streak is not None else (lambda: 0))
    return renderer, buf


def _canvas(renderer: ConsoleRenderer, *, width: int = 100) -> str:
    """Plain-text canvas snapshot: render `_render()` through a same-width
    capture console (no ANSI)."""
    from rich.console import Console

    capture_console = Console(width=width, height=40, force_terminal=True,
                              no_color=True, file=io.StringIO())
    with capture_console.capture() as cap:
        capture_console.print(renderer._render())
    return cap.get()


@pytest.fixture
def _finalize_renderers():
    renderers: list[ConsoleRenderer] = []
    yield renderers
    for renderer in renderers:
        renderer._stop_live()      # restore any taken log stream / termios


# ── snapshot renders (spec §3.7 渲染快照 row) ────────────────────────────────


def test_account_line_nine_states_with_stream_stitch(_finalize_renderers):
    cfg = _cfg(RICH, segment=SegmentConfig(enabled=True),
               stitch=StitchConfig(enabled=True))
    renderer, _ = _rich_renderer(cfg, counters=lambda: {"counts.threads": 5})
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    renderer.on_event(_ev("batch.start", batch=3, payload={"size": 53}))
    renderer.on_event(_ev("batch.end", batch=3, payload={
        "active": 41, "dropped_dup": 3, "dropped_lowq": 5, "dropped_verify": 1,
        "failed": 0, "dropped_noise": 2, "absorbed": 88, "stitched": 2,
        "threads": 5, "duration_ms": 1000}))
    text = _canvas(renderer)
    assert "账  emitted 41" in text
    for fragment in ("dup 3", "lowq 5", "verify 1", "failed 0", "noise 2",
                     "absorbed 88", "stitched 2", "threads 5"):
        assert fragment in text, fragment


def test_account_line_omits_stream_keys_when_disabled(_finalize_renderers):
    renderer, _ = _rich_renderer(_cfg(RICH))
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    renderer.on_event(_ev("batch.end", batch=1, payload={"active": 7}))
    text = _canvas(renderer)
    assert "emitted 7" in text
    assert "noise" not in text and "stitched" not in text and "threads" not in text


def _pool_snapshot() -> tuple[ProfileSnapshot, ...]:
    return (ProfileSnapshot(
        name="default", kind="llm", in_flight=4, max_concurrency=4, calls=213,
        retries=7, prompt_tokens=412_000, completion_tokens=96_000,
        est_cost_usd=0.83, p50_latency_ms=2100,
        keys=(KeySnapshot(env="LABELKIT_KEY_A", state="ok",
                          calls=150, rate_limited=0),
              KeySnapshot(env="LABELKIT_KEY_B", state="cooldown",
                          cooldown_remaining_s=12, calls=60, rate_limited=3),
              KeySnapshot(env="LABELKIT_KEY_C", state="disabled",
                          calls=3, rate_limited=0))),)


def test_llm_block_and_key_pool_three_states(_finalize_renderers):
    renderer, _ = _rich_renderer(_cfg(RICH), snapshot=_pool_snapshot)
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    text = _canvas(renderer)
    assert "LLM  default  在途 4/4  calls 213  重试 7  tok 412k↑ 96k↓  $0.83  p50 2.1s" in text
    assert "LABELKIT_KEY_A ok" in text
    assert "LABELKIT_KEY_B 冷却12s" in text
    assert "LABELKIT_KEY_C 禁用" in text
    assert "熔断 0/20" in text


def test_breaker_banner_when_streak_reaches_threshold(_finalize_renderers):
    renderer, _ = _rich_renderer(_cfg(RICH), fatal_streak=lambda: 20)
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    text = _canvas(renderer)
    assert "⚠ 熔断已打开" in text
    assert "熔断 20/20" in text


def test_interrupt_banner_on_stop_requested(_finalize_renderers):
    renderer, _ = _rich_renderer(_cfg(RICH))
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    renderer.on_stop_requested()
    assert "正在优雅中断（≤30s）…" in _canvas(renderer)


def test_narrow_terminal_degrades_to_single_progress_line(_finalize_renderers):
    renderer, _ = _rich_renderer(_cfg(RICH), width=50)
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    renderer.on_event(_ev("batch.end", batch=2, payload={
        "active": 10, "dropped_dup": 2, "duration_ms": 100}))
    text = _canvas(renderer, width=50).replace("\n", "")   # terminal wrap
    # The canvas collapses to the plain single-line form (spec §3.1 < 60 列),
    # minus the leading \r; no other block survives.
    assert ("labelkit: 批 2  emitted=10  dropped_dup=2  "
            "dropped_lowq=0  dropped_verify=0  failed=0") in text
    assert "账" not in text and "段" not in text and "LLM" not in text


def test_generate_only_phase_line_then_normal_batches(_finalize_renderers):
    cfg = _cfg(RICH,
               run=RunConfig(output="out/labels.jsonl", modality="text",
                             mode="generate_only"),
               generate=GenerateConfig(enabled=True, instruction="生成",
                                       standalone_count=100))
    produced = {"n": 0}
    renderer, _ = _rich_renderer(
        cfg, counters=lambda: {"counts.generated": produced["n"]})
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    renderer.on_estimate({"records": 100, "batches": 1, "generate_calls": 25,
                          "total_calls": 25})
    renderer.on_event(_ev("llm.call", stage="llm"))
    renderer.on_event(_ev("llm.call", stage="llm"))
    renderer.on_event(_ev("llm.call", stage="llm"))
    text = _canvas(renderer)
    assert "生成 ▶ calls 3/25 · 已产 0 条" in text
    produced["n"] = 12                     # phase-end meter (counts.generated)
    text = _canvas(renderer)
    assert "已产 12 条" in text
    renderer.on_event(_ev("batch.start", batch=1, payload={"size": 100}))
    text = _canvas(renderer)
    assert "生成 ▶" not in text
    assert "批 1/1" in text


def test_stage_board_bracket_attribution_and_symbols(_finalize_renderers):
    renderer, _ = _rich_renderer(_cfg(RICH))
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    renderer.on_estimate({"records": 10, "batches": 1, "quality_calls": 20,
                          "annotate_calls": 10, "total_calls": 30})
    renderer.on_event(_ev("batch.start", batch=1, payload={"size": 10}))
    renderer.on_stage("dedup", 1)
    renderer.on_stage("quality", 1)
    renderer.on_event(_ev("llm.call", batch=0, stage="llm"))
    renderer.on_event(_ev("llm.call", batch=0, stage="llm"))
    text = _canvas(renderer)
    # dedup completed (a later stage began), quality in flight with a/b from
    # the bracket-attributed numerator / estimate_run denominator (U20),
    # annotate not yet reached this batch.
    assert "dedup ✓" in text
    assert "quality ▶ 2/20" in text
    assert "annotate ·" in text


def test_keyboard_toggles_l_e_and_help(_finalize_renderers):
    renderer, _ = _rich_renderer(_cfg(RICH), snapshot=_pool_snapshot)
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    renderer.on_event(_ev("error", batch=1, stage="quality", payload={
        "stage": "quality", "kind": "judgment_invalid", "retryable": False}))

    renderer._handle_key("l")              # LLM expanded: one line per key —
    text = _canvas(renderer)               # env/state + per-key usage (§3.4)
    assert "default·LABELKIT_KEY_A ok  calls 150  rate_limited 0" in text
    assert "default·LABELKIT_KEY_B 冷却12s  calls 60  rate_limited 3" in text
    assert "default·LABELKIT_KEY_C 禁用  calls 3  rate_limited 0" in text
    renderer._handle_key("l")
    assert "default·LABELKIT_KEY_A ok" not in _canvas(renderer)

    renderer._handle_key("e")              # error strip: ring of stage·kind
    text = _canvas(renderer)
    assert "错误" in text and "quality·judgment_invalid" in text
    renderer._handle_key("e")
    assert "quality·judgment_invalid" not in _canvas(renderer)

    renderer._handle_key("?")              # help expanded lists all keys
    text = _canvas(renderer).replace("\n", "")   # fold-insensitive
    assert "键位" in text and "q 脱离" in text
    renderer._handle_key("h")              # 'h' is the '?' synonym
    assert "键位" not in _canvas(renderer)

    renderer._handle_key("x")              # outside the closed set: ignored
    renderer._handle_key("p")
    assert renderer._paused is True
    renderer._handle_key("p")
    assert renderer._paused is False


def test_p_pause_freezes_canvas_but_logs_keep_scrolling(_finalize_renderers):
    """§7.8 键盘 row: while 'p' holds the canvas frozen (zero live.update),
    log lines still scroll — the takeover stream prints through the Live
    console independently of the repaint throttle."""
    renderer, buf = _rich_renderer(_cfg(RICH))
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    renderer._handle_key("p")              # its own feedback paint happens here
    assert renderer._paused is True

    updates = {"n": 0}
    real_update = renderer._live.update

    def counting_update(*args, **kwargs):
        updates["n"] += 1
        return real_update(*args, **kwargs)

    renderer._live.update = counting_update    # type: ignore[method-assign]
    renderer.on_event(_ev("batch.start", batch=1, payload={"size": 5}))
    renderer.on_stage("quality", 1)            # even force-paint respects 'p'
    renderer.on_event(_ev("batch.end", batch=1, payload={
        "active": 5, "duration_ms": 10}))
    assert updates["n"] == 0                   # canvas fully frozen

    mark = len(buf.getvalue())
    stream = console_mod._LiveLogStream(renderer._live.console, renderer._Text)
    stream.write("2026-07-17T00:00:00+08:00 INFO  quality batch=1 "
                 "logs keep scrolling\n")
    assert "logs keep scrolling" in buf.getvalue()[mark:]


def test_u6_red_line_no_payload_free_text_ever_rendered(_finalize_renderers):
    """U6/U22: the renderer shows only counts/enums/env names — a marker inside
    the (none-tier-shaped) payload must never reach the canvas even with the
    error strip expanded."""
    marker = "FREE_TEXT_MARKER_XYZZY"
    renderer, _ = _rich_renderer(_cfg(RICH))
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    renderer.on_event(_ev("error", batch=1, stage="quality", payload={
        "stage": "quality", "kind": "judgment_invalid",
        "message": marker, "retryable": False}))
    renderer._handle_key("e")
    text = _canvas(renderer)
    assert marker not in text
    assert "quality·judgment_invalid" in text


# ── inert path (spec §3.7 协议契约 row: listener attached, plain, hb off) ───


def test_plain_zero_heartbeat_is_fully_inert(monkeypatch):
    fake_err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", fake_err)
    renderer = ConsoleRenderer()
    renderer.on_run_context(_cfg(), lambda: (), lambda: {}, lambda: 0)
    assert renderer._mode == "inert"
    renderer.on_event(_ev("run.start"))
    renderer.on_estimate({"records": 1, "batches": 1})
    renderer.on_stage("quality", 1)
    renderer.on_event(_ev("llm.call", stage="llm"))
    renderer.on_event(_ev("batch.end", batch=1, payload={"active": 1}))
    renderer.on_stop_requested()
    renderer.on_event(_ev("run.end", payload={"counts": {}, "exit_code": 0}))
    assert fake_err.getvalue() == ""
    assert renderer._live is None


# ── heartbeat (U14, spec §7.7 心跳行) ────────────────────────────────────────


def test_heartbeat_exact_line_fixed_cadence_and_disarm(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(console_mod, "_monotonic", lambda: clock["t"])
    fake_err = io.StringIO()                     # isatty() → False
    monkeypatch.setattr(sys, "stderr", fake_err)

    renderer = ConsoleRenderer()
    renderer.on_run_context(_cfg(ConsoleConfig(heartbeat_s=1)),
                            lambda: (), lambda: {}, lambda: 0)
    assert renderer._mode == "heartbeat"

    renderer.on_event(_ev("run.start"))          # t0=1000, first deadline 1001
    clock["t"] = 1000.5
    for _ in range(182):
        renderer.on_event(_ev("llm.call", stage="llm"))
    renderer.on_event(_ev("batch.start", batch=3, payload={"size": 9}))
    assert fake_err.getvalue() == ""             # deadline not reached yet

    clock["t"] = 1312.0
    renderer.on_stage("quality", 3)
    assert fake_err.getvalue() == (
        "heartbeat batch=3 stage=quality llm_calls=182 elapsed=312s\n")

    # Catch-up: ONE line was written and the deadline self-advanced in fixed
    # 1 s steps past `now` (1313) — the very next event beats again on time.
    clock["t"] = 1313.2
    renderer.on_event(_ev("llm.call", stage="llm"))
    lines = fake_err.getvalue().splitlines()
    assert len(lines) == 2
    assert lines[1] == "heartbeat batch=3 stage=quality llm_calls=183 elapsed=313s"

    clock["t"] = 1400.0
    renderer.on_event(_ev("run.end", payload={"counts": {}, "exit_code": 0}))
    clock["t"] = 1500.0
    renderer.on_event(_ev("llm.call", stage="llm"))   # disarmed: no beat
    assert len(fake_err.getvalue().splitlines()) == 2
    assert renderer._mode == "inert"


# ── degradation injection (U7, spec §3.7 降级注入 row) ──────────────────────


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_render_exception_degrades_to_detached_plain(monkeypatch, _finalize_renderers):
    """U7 × U21 (CONTRACTS §7.10): under mode_resolved=="rich" the emitter is
    statically gated off, so a mid-run render failure must land in the
    DETACHED-plain state — one WARN, then the renderer itself keeps printing
    the plain progress line and the text final summary via console_format."""
    handler = _ListHandler()
    logger = logging.getLogger("labelkit.console")
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        renderer, _ = _rich_renderer(_cfg(RICH))
        _finalize_renderers.append(renderer)

        def boom():
            raise RuntimeError("injected render failure")

        renderer._render = boom              # type: ignore[method-assign]
        renderer.on_event(_ev("run.start"))  # first paint → raises → degrade
        assert renderer._mode == "detached"  # plain ownership stays ours (U21)
        assert renderer._live_started is False
        warns = [r for r in handler.records if r.levelno == logging.WARNING]
        assert len(warns) == 1
        assert "console 渲染异常，已降级 plain" in warns[0].getMessage()

        # The rest of the run gets the plain lines from THIS renderer.
        fake_err = _FakeTty()
        monkeypatch.setattr(sys, "stderr", fake_err)
        renderer.on_event(_ev("batch.start", batch=1, payload={"size": 1}))
        renderer.on_stage("quality", 1)
        renderer.on_event(_ev("batch.end", batch=1, payload={
            "active": 1, "duration_ms": 10}))
        assert fake_err.getvalue() == console_format.format_progress_line(
            1, 1, {"dropped_dup": 0, "dropped_lowq": 0, "dropped_verify": 0,
                   "failed": 0})
        counts = {"scanned": 1, "emitted": 1}
        renderer.on_event(_ev("run.end", payload={"counts": counts,
                                                  "exit_code": 0}))
        assert fake_err.getvalue().endswith(
            "\n".join(console_format.format_summary_lines(counts)) + "\n")
        warns = [r for r in handler.records if r.levelno == logging.WARNING]
        assert len(warns) == 1               # exactly one WARN, ever
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def test_second_failure_while_detached_goes_inert(monkeypatch, _finalize_renderers):
    """The _dead latch: a failure in the detached plain path itself (stderr
    write exploding) drops to inert without a second WARN loop."""
    handler = _ListHandler()
    logger = logging.getLogger("labelkit.console")
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        renderer, _ = _rich_renderer(_cfg(RICH))
        _finalize_renderers.append(renderer)
        renderer._render = lambda: (_ for _ in ()).throw(RuntimeError("r1"))
        renderer.on_event(_ev("run.start"))
        assert renderer._mode == "detached"

        class _BrokenStderr(io.StringIO):
            def isatty(self) -> bool:
                return True

            def write(self, *_a):           # detached progress write explodes
                raise OSError("stderr gone")

        monkeypatch.setattr(sys, "stderr", _BrokenStderr())
        renderer.on_event(_ev("batch.end", batch=1, payload={"active": 1}))
        assert renderer._mode == "inert"    # second failure → fully inert
        warns = [r for r in handler.records if r.levelno == logging.WARNING]
        assert len(warns) == 1              # still exactly one WARN
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def test_heartbeat_mode_failure_goes_inert_not_detached(monkeypatch):
    """Outside rich ownership the emitter owns plain: a heartbeat-mode failure
    must NOT convert into detached plain output (mode_resolved=="plain")."""
    monkeypatch.setattr(sys, "stderr", io.StringIO())   # isatty() → False
    renderer = ConsoleRenderer()
    renderer.on_run_context(_cfg(ConsoleConfig(mode="plain", heartbeat_s=30)),
                            lambda: (), lambda: {}, lambda: 0)
    assert renderer._mode == "heartbeat"
    renderer._hb_check = (                              # type: ignore[method-assign]
        lambda: (_ for _ in ()).throw(RuntimeError("hb")))
    renderer.on_event(_ev("run.start"))
    renderer.on_event(_ev("batch.start", batch=1, payload={"size": 1}))
    assert renderer._mode == "inert"
    assert renderer._hb_next is None                    # timer disarmed


# ── q detach (U15/U21, spec §3.7 键盘交互 row) ──────────────────────────────


class _FakeTty(io.StringIO):
    def isatty(self) -> bool:                # the q path re-checks stderr TTY
        return True


def test_q_detach_owns_plain_progress_and_summary(monkeypatch, _finalize_renderers):
    renderer, _ = _rich_renderer(_cfg(RICH))
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    assert renderer._live_started is True

    renderer._handle_key("q")
    assert renderer._mode == "detached"
    assert renderer._live_started is False   # Live stopped, final frame kept

    fake_err = _FakeTty()
    monkeypatch.setattr(sys, "stderr", fake_err)
    renderer.on_event(_ev("batch.end", batch=2, payload={
        "active": 10, "dropped_dup": 2, "duration_ms": 50}))
    expected_line = console_format.format_progress_line(
        2, 10, {"dropped_dup": 2, "dropped_lowq": 0, "dropped_verify": 0,
                "failed": 0})
    assert fake_err.getvalue() == expected_line

    counts = {"scanned": 12, "ingested": 12, "emitted": 10, "dropped_dup": 2}
    renderer.on_event(_ev("run.end", payload={"counts": counts, "exit_code": 0}))
    expected_tail = ("\n"
                     + "\n".join(console_format.format_summary_lines(counts))
                     + "\n")
    assert fake_err.getvalue() == expected_line + expected_tail


# ── dry-run rich (U13) ──────────────────────────────────────────────────────


def test_dry_run_rich_renders_estimate_tables(_finalize_renderers):
    cfg = _cfg(RICH, dry_run=True)
    renderer, buf = _rich_renderer(cfg)
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    assert renderer._live_started is False   # dry path never starts a Live
    est = {"records": 53, "batches": 2, "generate_calls": 0, "segment_calls": 5,
           "stitch_calls": 10, "classify_calls": 5, "extract_calls": 48,
           "quality_calls": 20, "annotate_calls": 5, "verify_calls": 5,
           "total_calls": 98}
    renderer.on_estimate(est)
    renderer.on_event(_ev("run.end", payload={"counts": {}, "exit_code": 0}))
    text = buf.getvalue()
    assert "dry-run 估算" in text
    assert "estimated_records" in text and "53" in text
    for key in ("generate_calls", "segment_calls", "stitch_calls",
                "classify_calls", "extract_calls", "quality_calls",
                "annotate_calls", "verify_calls"):
        assert key in text, key
    assert "98" in text and "total" in text
    assert "no LLM calls made, no output written (report only)" in text
    assert "dry-run:" not in text            # the plain-anchor prefix is not ours


# ── U24 layer ② — dry-run golden files (spec §7.8 回归锚 row) ───────────────
#
# The five goldens under tests/cli/goldens/ were captured from the v1.9 HEAD
# baseline (pre-v1.10) and verified identical against the current tree; this
# test keeps the plain dry-run stderr byte-anchored to them forever. Real
# example fixtures are scanned (M2), but NO LLM call is made (dry-run).

_EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
_GOLDENS = Path(__file__).parent / "goldens"


@pytest.mark.parametrize("subdir,project,golden", [
    ("text", "project.toml", "dryrun-text.txt"),
    ("text", "project-synth.toml", "dryrun-text-synth.txt"),
    ("ui", "project.toml", "dryrun-ui.txt"),
    ("stream", "project.toml", "dryrun-stream.txt"),
    ("stream", "project-text.toml", "dryrun-stream-text.txt"),
])
def test_dry_run_plain_golden_files(subdir, project, golden,
                                    monkeypatch, tmp_path, capsys):
    from labelkit.cli.main import main

    monkeypatch.setenv("LABELKIT_ZAI_KEY", "dummy")     # referenced, never used
    monkeypatch.chdir(_EXAMPLES / subdir)
    code = main(["run", "--config", "../config.toml", "--project", project,
                 "--output", str(tmp_path / "o.jsonl"),
                 "--dry-run", "--console", "plain"])
    assert code == 0
    err = capsys.readouterr().err
    dry_lines = [ln for ln in err.splitlines() if ln.startswith("dry-run")]
    expected = (_GOLDENS / golden).read_text(encoding="utf-8").splitlines()
    assert dry_lines == expected


# ── keyboard over a REAL pty (spec §7.8 键盘 row) ───────────────────────────
#
# Handshake protocol (macOS pty semantics force both legs):
# 1. the parent must DRAIN the master continuously — `tty.setcbreak` defaults
#    to TCSAFLUSH and the §3.4 restore uses TCSADRAIN, and both wait for the
#    pty output queue (which holds the ECHO of any earlier input) to drain,
#    i.e. for the master side to read it;
# 2. 'q' is written only AFTER the child reports cbreak entry — TCSAFLUSH
#    discards unread input, so a pre-queued byte would be flushed away.

_PTY_CHILD = r'''
import json, sys, termios, time

from labelkit.cli.console import ConsoleRenderer
from labelkit.common.config.model import (
    AnnotateConfig, ClassifyConfig, ConsoleConfig, Criterion, DedupConfig,
    ExtractConfig, GenerateConfig, InputConfig, LLMProfile, OutputConfig,
    QualityConfig, ResolvedConfig, Rubric, RunConfig, SegmentConfig,
    StitchConfig, StreamConfig, ToolConfig, TraceConfig, VerifyConfig,
)
from labelkit.common.observability.obslog import TraceEvent

cfg = ResolvedConfig(
    tool=ToolConfig(),
    console=ConsoleConfig(mode="rich", mode_resolved="rich", interactive=True),
    llm_profiles={"default": LLMProfile(name="default", provider="anthropic",
                                        base_url="https://x", model="m",
                                        api_key_env="K")},
    embedding_profiles={},
    run=RunConfig(output="out/o.jsonl", modality="text", input="in.jsonl"),
    input=InputConfig(), stream=StreamConfig(), dedup=DedupConfig(),
    segment=SegmentConfig(), stitch=StitchConfig(), extract=ExtractConfig(),
    classify=ClassifyConfig(), quality=QualityConfig(),
    generate=GenerateConfig(), annotate=AnnotateConfig(instruction="标注"),
    verify=VerifyConfig(), output=OutputConfig(schema_inline="{}"),
    trace=TraceConfig(),
    rubric=Rubric(name="r", criteria=(Criterion(key="c1", description="d",
                                                pairwise_prompt="p"),)),
    class_views={}, user_schema={"type": "object"},
    limit=None, strict=False, dry_run=False,
    config_path="c.toml", project_path="p.toml",
    config_digest="sha256:0", project_digest="sha256:0",
)

def ev(name, batch=0, payload=None):
    return TraceEvent(ts="t", run_id="deadbeefcafe", batch_no=batch,
                      stage="run", ev=name, record_ids=(), payload=payload or {})

fd = sys.stdin.fileno()
before = termios.tcgetattr(fd)
renderer = ConsoleRenderer()
renderer.on_run_context(cfg, lambda: (), lambda: {}, lambda: 0)
renderer.on_event(ev("run.start"))          # Live start → setcbreak
during = termios.tcgetattr(fd)
kbd_active = renderer._kbd_active
print("READY", flush=True)                  # parent may send 'q' now

detached = False
for _ in range(200):                        # poll rides the event callbacks
    renderer.on_event(ev("batch.end", batch=1, payload={"active": 1}))
    if renderer._mode == "detached":
        detached = True
        break
    time.sleep(0.05)
after = termios.tcgetattr(fd)

LFLAG = 3
# PENDIN is a KERNEL-transient lflag (BSD termios "pending input must be
# retyped"): the kernel raises it by itself on the cbreak → canonical switch
# and clears it on the next input reprocess — it is not a user-settable
# attribute and tcsetattr cannot influence it. The byte-identical restore
# assertion therefore compares with PENDIN masked on both sides; everything
# else (all flags, speeds, every cc byte) must match exactly.
PENDIN = getattr(termios, "PENDIN", 0x20000000)
def _norm(attrs):
    normd = list(attrs)
    normd[LFLAG] = normd[LFLAG] & ~PENDIN
    return normd

print(json.dumps({
    "kbd_active": kbd_active,
    "icanon_cleared": not (during[LFLAG] & termios.ICANON),
    "echo_cleared": not (during[LFLAG] & termios.ECHO),
    "isig_kept": bool(during[LFLAG] & termios.ISIG),
    "detached": detached,
    "restored": _norm(after) == _norm(before),
}), flush=True)
'''


@pytest.mark.skipif(sys.platform == "win32", reason="termios/pty are POSIX-only")
def test_pty_cbreak_q_detach_restores_termios():
    pytest.importorskip("termios")
    import pty
    import threading

    try:
        master, slave = pty.openpty()
    except OSError as exc:                    # CI-less/exotic environments
        pytest.skip(f"pty unavailable: {exc}")

    stop_draining = False

    def _drain_master() -> None:              # keeps TCSAFLUSH/TCSADRAIN moving
        while not stop_draining:
            try:
                if not os.read(master, 4096):
                    return
            except OSError:
                return

    drainer = threading.Thread(target=_drain_master, daemon=True)
    drainer.start()
    proc = subprocess.Popen(
        [sys.executable, "-c", _PTY_CHILD],
        stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=str(Path(__file__).resolve().parents[2]),
    )
    try:
        ready = proc.stdout.readline().strip()
        assert ready == "READY", (ready, proc.stderr.read())
        os.write(master, b"q")               # after cbreak — survives TCSAFLUSH
        verdict_line = proc.stdout.readline()
        _, stderr_tail = proc.communicate(timeout=60)
    except BaseException:
        proc.kill()
        proc.communicate()
        raise
    finally:
        stop_draining = True
        os.close(slave)
        os.close(master)
    assert proc.returncode == 0, stderr_tail
    result = json.loads(verdict_line)
    assert result["kbd_active"] is True
    assert result["icanon_cleared"] is True   # cbreak entered
    assert result["echo_cleared"] is True
    assert result["isig_kept"] is True        # Ctrl-C → SIGINT unchanged
    assert result["detached"] is True         # 'q' consumed from the pty
    assert result["restored"] is True         # byte-identical termios restore

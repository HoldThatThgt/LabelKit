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

import asyncio
import io
import json
import logging
import os
import subprocess
import sys
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import labelkit.cli.console as console_mod
from labelkit.cli.console import ConsoleRenderer
from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ClassSpec,
    ClassView,
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
from labelkit.common.inference.llm_client import KeySnapshot, ProfileSnapshot
from labelkit.orchestration.process_workflow import ProcessWorkflow

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


def _final_canvas(renderer: ConsoleRenderer, counts: dict, *,
                  width: int = 100) -> str:
    """定格末帧快照（U8）：与 `_canvas` 同款定宽捕获，抓 `_render_final()`。"""
    from rich.console import Console

    capture_console = Console(width=width, height=40, force_terminal=True,
                              no_color=True, file=io.StringIO())
    with capture_console.capture() as cap:
        capture_console.print(renderer._render_final(counts))
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
    assert "account  emitted 41" in text
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
    assert ("LLM  default  in_flight 4/4  calls 213  retries 7  "
            "tok 412k↑ 96k↓  $0.83  p50 2.1s") in text
    assert "LABELKIT_KEY_A ok" in text
    assert "LABELKIT_KEY_B cooldown 12s" in text
    assert "LABELKIT_KEY_C disabled" in text
    assert "breaker 0/20" in text


def test_breaker_banner_when_streak_reaches_threshold(_finalize_renderers):
    renderer, _ = _rich_renderer(_cfg(RICH), fatal_streak=lambda: 20)
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    text = _canvas(renderer)
    assert "⚠ breaker open" in text
    assert "breaker 20/20" in text


def test_interrupt_banner_on_stop_requested(_finalize_renderers):
    renderer, _ = _rich_renderer(_cfg(RICH))
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    renderer.on_stop_requested()
    assert "graceful interrupt in progress (≤30s)…" in _canvas(renderer)


def test_narrow_terminal_degrades_to_single_progress_line(_finalize_renderers):
    renderer, _ = _rich_renderer(_cfg(RICH), width=50)
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    renderer.on_event(_ev("batch.end", batch=2, payload={
        "active": 10, "dropped_dup": 2, "duration_ms": 100}))
    text = _canvas(renderer, width=50).replace("\n", "")   # terminal wrap
    # The canvas collapses to the plain single-line form (spec §3.1 < 60 列),
    # minus the leading \r; no other block survives.
    assert ("labelkit: batch 2  emitted=10  dropped_dup=2  "
            "dropped_lowq=0  dropped_verify=0  failed=0") in text
    assert "account" not in text and "stages" not in text and "LLM" not in text


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
    assert "generate ▶ calls 3/25 · produced 0" in text
    produced["n"] = 12                     # phase-end meter (counts.generated)
    text = _canvas(renderer)
    assert "produced 12" in text
    renderer.on_event(_ev("batch.start", batch=1, payload={"size": 100}))
    text = _canvas(renderer)
    assert "generate ▶" not in text
    assert "batch 1/1" in text


def test_header_eta_appears_once_the_ema_rate_is_known(monkeypatch,
                                                       _finalize_renderers):
    """§7.7 抬头块 ETA 行：仅在批总数分母可得（on_estimate 已送达）且 EMA 速率
    已知（至少一条带 duration_ms 的 batch.end）时显示，以 `~` 标外推。"""
    clock = {"t": 1000.0}
    monkeypatch.setattr(console_mod, "_monotonic", lambda: clock["t"])
    renderer, _ = _rich_renderer(_cfg(RICH))
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    renderer.on_estimate({"records": 100, "batches": 10, "total_calls": 0})
    assert "ETA" not in _canvas(renderer)          # 还没有速率样本

    renderer.on_event(_ev("batch.start", batch=1, payload={"size": 10}))
    renderer.on_event(_ev("batch.end", batch=1, payload={
        "active": 10, "duration_ms": 1000}))       # 10 条/秒，已完成 10 条
    assert "ETA ~00:09" in _canvas(renderer)       # 剩 90 条 ÷ 10 条/秒


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


def test_stage_call_keys_frozen_sum_mapping():
    """v1.12：_STAGE_CALL_KEYS 冻结为多键求和映射——classify/annotate 折入
    帧粒度调用键，其余段保持单键；_ESTIMATE_CALL_KEYS 按 estimate_run 冻结
    键序含两新键。"""
    assert console_mod._STAGE_CALL_KEYS == {
        "segment": ("segment_calls",),
        "stitch": ("stitch_calls",),
        "classify": ("classify_calls", "frame_classify_calls"),
        "extract": ("extract_calls",),
        "quality": ("quality_calls",),
        "generate": ("generate_calls",),
        "annotate": ("annotate_calls", "frame_annotate_calls"),
        "verify": ("verify_calls",),
    }
    assert console_mod._ESTIMATE_CALL_KEYS == (
        "generate_calls", "segment_calls", "stitch_calls", "classify_calls",
        "frame_classify_calls", "extract_calls", "quality_calls",
        "annotate_calls", "frame_annotate_calls", "verify_calls")


def test_stage_board_sums_frame_keys_into_classify_and_annotate(_finalize_renderers):
    """v1.12：段棋盘分母＝多键求和——classify 分母 = classify_calls +
    frame_classify_calls，annotate 同理；单键段（quality）行为不变；帧键缺位
    的估算字典退回单键分母（v1.10 行为）。"""
    cfg = _cfg(RICH, segment=SegmentConfig(enabled=True),
               classify=ClassifyConfig(enabled=True))
    renderer, _ = _rich_renderer(cfg)
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    renderer.on_estimate({"records": 26, "batches": 1, "segment_calls": 3,
                          "classify_calls": 2, "frame_classify_calls": 26,
                          "quality_calls": 20,
                          "annotate_calls": 2, "frame_annotate_calls": 26,
                          "total_calls": 79})
    renderer.on_event(_ev("batch.start", batch=1, payload={"size": 26}))
    renderer.on_stage("classify", 1)
    renderer.on_event(_ev("llm.call", batch=1, stage="llm"))
    renderer.on_event(_ev("llm.call", batch=1, stage="llm"))
    renderer.on_event(_ev("llm.call", batch=1, stage="llm"))
    assert "classify ▶ 3/28" in _canvas(renderer)   # 2 + 26 求和分母

    renderer.on_stage("annotate", 1)
    renderer.on_event(_ev("llm.call", batch=1, stage="llm"))
    assert "annotate ▶ 1/28" in _canvas(renderer)   # 2 + 26 求和分母

    renderer.on_stage("quality", 1)
    renderer.on_event(_ev("llm.call", batch=1, stage="llm"))
    assert "quality ▶ 1/20" in _canvas(renderer)    # 单键段行为不变

    # 帧键缺位（v1.10 形估算字典）⇒ classify 分母退回单键值
    renderer._est = {"records": 26, "batches": 1, "classify_calls": 2}
    renderer.on_stage("classify", 1)
    assert "classify ▶ 3/2" in _canvas(renderer)


def test_keyboard_toggles_l_e_and_help(_finalize_renderers):
    renderer, _ = _rich_renderer(_cfg(RICH), snapshot=_pool_snapshot)
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    renderer.on_event(_ev("error", batch=1, stage="quality", payload={
        "stage": "quality", "kind": "judgment_invalid", "retryable": False}))

    renderer._handle_key("l")              # LLM expanded: one line per key —
    text = _canvas(renderer)               # env/state + per-key usage (§3.4)
    assert "default·LABELKIT_KEY_A ok  calls 150  rate_limited 0" in text
    assert "default·LABELKIT_KEY_B cooldown 12s  calls 60  rate_limited 3" in text
    assert "default·LABELKIT_KEY_C disabled  calls 3  rate_limited 0" in text
    renderer._handle_key("l")
    assert "default·LABELKIT_KEY_A ok" not in _canvas(renderer)

    renderer._handle_key("e")              # error strip: ring of stage·kind
    text = _canvas(renderer)
    assert "errors" in text and "quality·judgment_invalid" in text
    renderer._handle_key("e")
    assert "quality·judgment_invalid" not in _canvas(renderer)

    renderer._handle_key("?")              # help expanded lists all keys
    text = _canvas(renderer).replace("\n", "")   # fold-insensitive
    assert "keymap" in text and "q detach" in text
    renderer._handle_key("h")              # 'h' is the '?' synonym
    assert "keymap" not in _canvas(renderer)

    renderer._handle_key("x")              # outside the closed set: ignored
    renderer._handle_key("p")
    assert renderer._paused is True
    renderer._handle_key("p")
    assert renderer._paused is False


def test_keyboard_canvas_lines_clamp_between_four_and_sixteen(_finalize_renderers):
    """§7.7 键盘开关表 `+` / `-` 行：画布行数上限在 4–16 之间增减并双向钳制
    （未按过任何一键时为自适应——由 Live 裁剪，不截行）。"""
    renderer, _ = _rich_renderer(_cfg(RICH), snapshot=_pool_snapshot)
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    assert renderer._max_lines is None             # 自适应
    assert "account" in _canvas(renderer)

    renderer._handle_key("-")                      # 自适应起步按 16 算
    assert renderer._max_lines == 15
    for _ in range(20):
        renderer._handle_key("-")
    assert renderer._max_lines == 4                # 下界钳制
    assert "account" not in _canvas(renderer)      # 画布被截到 4 行

    for _ in range(30):
        renderer._handle_key("+")
    assert renderer._max_lines == 16               # 上界钳制
    assert "account" in _canvas(renderer)


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
        assert "console render failed, degraded to plain" in warns[0].getMessage()

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
    # v1.12：估算字典按冻结键序携带帧粒度两键（rich 估算表等价性）
    est = {"records": 53, "batches": 2, "generate_calls": 0, "segment_calls": 5,
           "stitch_calls": 10, "classify_calls": 5, "frame_classify_calls": 53,
           "extract_calls": 48,
           "quality_calls": 20, "annotate_calls": 5, "frame_annotate_calls": 53,
           "verify_calls": 5,
           "total_calls": 204}
    renderer.on_estimate(est)
    renderer.on_event(_ev("run.end", payload={"counts": {}, "exit_code": 0}))
    text = buf.getvalue()
    assert "dry-run estimate" in text
    assert "estimated_records" in text and "53" in text
    for key in ("generate_calls", "segment_calls", "stitch_calls",
                "classify_calls", "frame_classify_calls", "extract_calls",
                "quality_calls", "annotate_calls", "frame_annotate_calls",
                "verify_calls"):
        assert key in text, key
    assert "204" in text and "total" in text
    assert "no LLM calls made, no output written (report only)" in text
    assert "dry-run:" not in text            # the plain-anchor prefix is not ours


def test_dry_run_rich_renders_exact_sequence_arithmetic_and_call_families(
        _finalize_renderers):
    """rich 静态表完整呈现 sequence 精确数量、attempt 边界与七类调用。"""
    renderer, buf = _rich_renderer(_cfg(RICH, dry_run=True))
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    families = {
        "scenario_seed_calls": 0,
        "baseline_event_plan_calls": 6,
        "variant_event_plan_calls": 8,
        "frame_render_calls": 14,
        "semantic_evaluation_calls": 8,
        "noise_render_calls": 2,
        "noise_evaluation_calls": 2,
    }
    sequence = {
        "planned_sets": 2, "planned_sequences": 8, "primary_events": 22,
        "noise_events": 2, "replay_sequences": 1, "replay_events": 3,
        "stream_rows": 27, "successful_attempt_lower_bound": 48,
        "max_slot_attempts_upper_bound": 384, "sequence_calls": families,
    }
    renderer.on_estimate({
        "records": 8, "batches": 2, "total_calls": 48, "sequence": sequence,
    })
    renderer.on_event(_ev("run.end", payload={"counts": {}, "exit_code": 0}))
    text = buf.getvalue()
    for key, value in {**sequence, **families}.items():
        if key != "sequence_calls":
            assert key in text and str(value) in text
    assert "sequence generation calls" in text


# ── U24 layer ② — dry-run golden files (spec §7.8 回归锚 row) ───────────────
#
# The eight goldens under tests/cli/goldens/ keep the plain dry-run stderr
# byte-anchored forever: the original five date to the v1.9 HEAD baseline and
# were re-captured at v1.12 (the estimate line gained the two frame keys —
# 裁决·估算上界与六 golden); dryrun-mix.txt (the examples/mix UI main project,
# both frame passes on) and dryrun-mix-text.txt (its pure-DeepSeek text
# sibling) are the v1.12-born mix pair; dryrun-sequence-generation.txt is the
# v1.18 sequence-delivery example while the seven older files stay byte-identical.
# Real example fixtures are scanned (M2), but NO LLM call is made (dry-run).

_EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
_GOLDENS = Path(__file__).parent / "goldens"


@pytest.mark.parametrize("subdir,project,golden", [
    ("text", "project.toml", "dryrun-text.txt"),
    ("text", "project-synth.toml", "dryrun-text-synth.txt"),
    ("ui", "project.toml", "dryrun-ui.txt"),
    ("stream", "project.toml", "dryrun-stream.txt"),
    ("stream", "project-text.toml", "dryrun-stream-text.txt"),
    ("mix", "project.toml", "dryrun-mix.txt"),
    ("mix", "project-text.toml", "dryrun-mix-text.txt"),
    ("sequence-generation", "project.toml", "dryrun-sequence-generation.txt"),
])
def test_dry_run_plain_golden_files(subdir, project, golden,
                                    monkeypatch, tmp_path, capsys):
    from labelkit.cli.main import main

    monkeypatch.setenv("LABELKIT_ZAI_KEY", "dummy")     # referenced, never used
    monkeypatch.setenv("LABELKIT_DEEPSEEK_KEY", "dummy")  # mix 同款 dummy（v1.12）
    monkeypatch.chdir(_EXAMPLES / subdir)
    # examples/mix 独立成套（两工程同用本目录 config.toml——DeepSeek+z.ai
    # 双端点，§3.8）；examples/sequence-generation 同样自含（双 profile
    # DeepSeek——v1.18 的 E2E 端点由需求方指定，共享的 ../config.toml 是 z.ai）；
    # 其余五例共享 ../config.toml。
    config = ("config.toml" if subdir in {"mix", "sequence-generation"}
              else "../config.toml")
    code = main(["run", "--config", config, "--project", project,
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


# ── 定格末帧（spec §7.7「运行结束……定格为静态终版面板」U8） ──────────────────


def _usage_snapshot() -> tuple[ProfileSnapshot, ...]:
    """两 profile 快照：第二个未配价目、p50 窗为空（末帧用量表的两个 `—` 位）。"""
    return (
        ProfileSnapshot(
            name="default", kind="llm", in_flight=0, max_concurrency=4,
            calls=213, retries=7, prompt_tokens=1_500_000,
            completion_tokens=96_000, est_cost_usd=0.83, p50_latency_ms=2100,
            keys=(KeySnapshot(env="LABELKIT_KEY_A", state="ok"),)),
        ProfileSnapshot(
            name="vision", kind="llm", in_flight=0, max_concurrency=2,
            calls=15, retries=0, prompt_tokens=1_200, completion_tokens=300,
            est_cost_usd=None, p50_latency_ms=None,
            keys=(KeySnapshot(env="LABELKIT_KEY_B", state="ok"),)),
    )


def _run_with_stage_times(renderer: ConsoleRenderer, clock: dict) -> None:
    """跑一批并累出三段耗时：dedup 10 s、quality 40 s、annotate 20 s。"""
    renderer.on_event(_ev("run.start"))            # t0 = 1000
    renderer.on_event(_ev("batch.start", batch=1, payload={"size": 10}))
    renderer.on_stage("dedup", 1)
    clock["t"] = 1010.0
    renderer.on_stage("quality", 1)
    clock["t"] = 1050.0
    renderer.on_stage("annotate", 1)
    clock["t"] = 1070.0
    renderer.on_event(_ev("batch.end", batch=1, payload={
        "active": 8, "dropped_dup": 1, "failed": 1, "duration_ms": 70000}))


def test_final_frame_renders_counts_stage_times_and_usage(monkeypatch,
                                                          _finalize_renderers):
    """U8 定格末帧：抬头（run_id + 模式徽标 + mm:ss 用时）+ counts 两列表 +
    段耗时横条（按 `_chain` 链序、最长者 20 格）+ llm_usage 表。"""
    clock = {"t": 1000.0}
    monkeypatch.setattr(console_mod, "_monotonic", lambda: clock["t"])
    # verify 在链上但本批从未走到——它不出条（段耗时只画有数据的段）。
    renderer, _ = _rich_renderer(_cfg(RICH, verify=VerifyConfig(enabled=True)),
                                 snapshot=_usage_snapshot)
    _finalize_renderers.append(renderer)
    _run_with_stage_times(renderer, clock)

    clock["t"] = 1187.0                            # 用时 187 s → 03:07
    counts = {"scanned": 10, "emitted": 8, "dropped_dup": 1, "failed": 1}
    text = _final_canvas(renderer, counts)
    lines = [ln.rstrip() for ln in text.splitlines()]

    assert (" labelkit run done · f3a9c04b7d21 · process/text · elapsed 03:07"
            in lines)
    assert " counts (= report.counts)" in lines
    for key, value in counts.items():              # counts 两列表逐键逐值
        assert any(key in ln and str(value) in ln for ln in lines), key
    # 段耗时条：链序 dedup → quality → annotate，最长的 quality 满 20 格
    bars = [ln for ln in lines
            if ln.startswith((" dedup", " quality", " annotate", " verify"))]
    assert bars == [" dedup     █████ 10.0s",
                    " quality   " + "█" * 20 + " 40.0s",
                    " annotate  " + "█" * 10 + " 20.0s"]
    assert any("stage time (approx" in ln for ln in lines)
    # llm_usage 表：列头 + 逐 profile 行（tok 缩写与画布同款）
    assert " llm_usage" in lines
    header = next(ln for ln in lines if "profile" in ln and "p50" in ln)
    for column in ("profile", "calls", "retries", "tok↑", "tok↓", "cost", "p50"):
        assert column in header, column
    default_row = next(ln for ln in lines if "default" in ln and "213" in ln)
    assert "1.5M" in default_row and "96k" in default_row
    assert "$0.83" in default_row and "2.1s" in default_row


def test_final_frame_usage_table_dashes_missing_price_and_p50(monkeypatch,
                                                              _finalize_renderers):
    """未配价目 ⇒ cost 显示 `—`；p50 窗为空 ⇒ p50 显示 `—`（画布同款渲染）。"""
    monkeypatch.setattr(console_mod, "_monotonic", lambda: 1000.0)
    renderer, _ = _rich_renderer(_cfg(RICH), snapshot=_usage_snapshot)
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))

    row = next(ln for ln in _final_canvas(renderer, {}).splitlines()
               if "vision" in ln)
    assert row.count("—") == 2                     # cost 与 p50 各一
    assert "15" in row                             # 其余列照常渲染
    assert console_mod._fmt_tok(1_200) in row      # tok 缩写与画布同款


def test_final_frame_stage_time_lines_absent_without_timing(monkeypatch,
                                                            _finalize_renderers):
    """无计时数据（一次 on_stage 都没发生）⇒ 段耗时段整块缺席；无快照 ⇒ 用量表
    整块缺席（末帧只画有内容的块）。"""
    monkeypatch.setattr(console_mod, "_monotonic", lambda: 1000.0)
    renderer, _ = _rich_renderer(_cfg(RICH))       # snapshot 默认返回空元组
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))

    text = _final_canvas(renderer, {"scanned": 0})
    assert "stage time" not in text
    assert "llm_usage" not in text
    assert "counts (= report.counts)" in text      # counts 块恒在


def test_final_frame_side_product_paths_follow_switches(monkeypatch,
                                                        _finalize_renderers):
    """侧产物路径行随开关在场：rejects 行仅 `output.rejects != "none"`、
    trace 行仅 `trace.enabled`（U6 纪律：只有路径，没有数据内容）。"""
    monkeypatch.setattr(console_mod, "_monotonic", lambda: 1000.0)
    renderer, _ = _rich_renderer(_cfg(RICH))       # 默认 rejects="refs"、trace 关
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    text = _final_canvas(renderer, {})
    assert " rejects → out/labels.rejects.jsonl" in text
    assert "trace →" not in text

    both_off = _cfg(RICH, output=OutputConfig(schema_inline="{}", rejects="none"),
                    trace=TraceConfig(enabled=True, path="out/labels.trace.jsonl"))
    renderer2, _ = _rich_renderer(both_off)
    _finalize_renderers.append(renderer2)
    renderer2.on_event(_ev("run.start"))
    text2 = _final_canvas(renderer2, {})
    assert "rejects →" not in text2
    assert " trace → out/labels.trace.jsonl" in text2


def test_run_end_freezes_the_final_frame_then_stops_live(monkeypatch,
                                                         _finalize_renderers):
    """接线面：run.end 把末帧画进 Live（transient=False ⇒ 留在回滚区）后停掉
    Live，渲染器转惰性——杂散事件此后保持廉价。"""
    clock = {"t": 1000.0}
    monkeypatch.setattr(console_mod, "_monotonic", lambda: clock["t"])
    renderer, buf = _rich_renderer(_cfg(RICH), snapshot=_usage_snapshot)
    _finalize_renderers.append(renderer)
    _run_with_stage_times(renderer, clock)
    mark = len(buf.getvalue())

    renderer.on_event(_ev("run.end", payload={"counts": {"emitted": 8},
                                              "exit_code": 0}))
    frame = buf.getvalue()[mark:]
    assert "labelkit run done" in frame
    assert "counts (= report.counts)" in frame
    assert renderer._live_started is False
    assert renderer._mode == "inert"


# ── dry-run 注记门的 console 侧镜像（spec 3.10.3 R28 注记） ───────────────────

_MIRROR_OVERRIDES = {
    "quality": lambda cfg: replace(cfg.quality, rounds=7),
    "rubric": lambda cfg: Rubric(name="r2", criteria=(
        Criterion(key="c2", description="d2", pairwise_prompt="p2"),)),
    "annotate": lambda cfg: replace(cfg.annotate, instruction="按类指令"),
    "generate": lambda cfg: GenerateConfig(enabled=True, instruction="生成"),
    "verify": lambda cfg: VerifyConfig(enabled=True),
    "extract": lambda cfg: ExtractConfig(enabled=True),
}


def _classify_cfg_with_views(overrides: dict | None = None) -> ResolvedConfig:
    """M1 形态的 class_views：每个声明类一份合并视图，零覆盖类逐节镜像全局。"""
    cfg = _cfg(RICH, classify=ClassifyConfig(
        enabled=True, fallback_class="other",
        classes=(ClassSpec(name="faq", description="问答"),
                 ClassSpec(name="other", description="其余"))))
    views = {}
    for spec in cfg.classify.classes:
        fields = dict(quality=cfg.quality, rubric=cfg.rubric,
                      annotate=cfg.annotate, generate=cfg.generate,
                      verify=cfg.verify, extract=cfg.extract)
        if overrides is not None and spec.name == "faq":
            fields.update(overrides)
        views[spec.name] = ClassView(name=spec.name, **fields)
    return replace(cfg, class_views=views)


def _mirrored_verdict(cfg: ResolvedConfig) -> bool:
    """两侧同名判定必须逐例相等（console 侧注释自称是 ProcessWorkflow 的镜像）。

    ProcessWorkflow 侧只读 `self.cfg`，故以最小 self 直调未绑定方法——避免为一个
    纯判定装配整个运行期对象图。
    """
    console_verdict = console_mod._class_overrides_exist(cfg)
    process_workflow_verdict = ProcessWorkflow._class_overrides_exist(
        SimpleNamespace(cfg=cfg))
    assert console_verdict == process_workflow_verdict
    return console_verdict


@pytest.mark.parametrize("field", sorted(_MIRROR_OVERRIDES))
def test_class_overrides_mirror_matches_the_process_workflow_predicate(field):
    """六个被检查字段各自都能单独触发 True，且两侧判定逐例相等。"""
    cfg = _classify_cfg_with_views({field: _MIRROR_OVERRIDES[field](_cfg(RICH))})
    assert _mirrored_verdict(cfg) is True


def test_class_overrides_absent_for_empty_or_global_identical_views():
    """无 class_views（classify 关）与「每类都与全局全等」两态都判 False——
    class_views 非空本身说明不了什么，必须逐节与全局基值比较。"""
    assert _mirrored_verdict(_cfg(RICH)) is False
    assert _mirrored_verdict(_classify_cfg_with_views()) is False


def test_class_override_note_line_follows_the_mirror(_finalize_renderers):
    """注记门的落点：dry-run 的 rich 面在判定为真时打那条固定措辞的下界注记
    （与 plain 路径同文），判定为假时不打。"""
    cfg = _classify_cfg_with_views({"annotate": _MIRROR_OVERRIDES["annotate"](
        _cfg(RICH))})
    renderer, buf = _rich_renderer(replace(cfg, dry_run=True))
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    renderer.on_estimate({"records": 1, "batches": 1, "total_calls": 0})
    assert ("note: estimated with global config / multi reports a lower bound "
            "at label multiplier 1") in buf.getvalue().replace("\n", "")

    clean, buf2 = _rich_renderer(replace(_classify_cfg_with_views(),
                                         dry_run=True))
    _finalize_renderers.append(clean)
    clean.on_event(_ev("run.start"))
    clean.on_estimate({"records": 1, "batches": 1, "total_calls": 0})
    assert "lower bound at label multiplier 1" not in buf2.getvalue()


def test_dry_run_rich_stream_note_matches_the_plain_wording(_finalize_renderers):
    """U13：rich 估算面的第二条注记与 plain 路径**同文**——流模式下游按
    episodes≈sessions 报下界（LLM 边界精化只会增加段数，spec 3.10.3 时序流行）。"""
    cfg = _cfg(RICH, dry_run=True,
               segment=SegmentConfig(enabled=True, strategy="hybrid"))
    renderer, buf = _rich_renderer(cfg)
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    renderer.on_estimate({"records": 26, "batches": 4, "segment_calls": 3,
                          "total_calls": 3})
    assert ("note: stream estimate: downstream reports a lower bound at "
            "episodes≈sessions (LLM refinement only adds segments)"
            ) in buf.getvalue().replace("\n", "")


# ── 渲染 tick 协程（spec §7.7 rich 档「渲染 tick = asyncio task」U26） ────────


async def test_tick_loop_repaints_during_event_silence(_finalize_renderers):
    """静默期活性：一个回调都不发，tick 仍按 refresh_hz 周期重绘（单次长 LLM
    调用可让事件停滞至 timeout_s——时钟 / ETA / 键位照常）。"""
    cfg = _cfg(ConsoleConfig(mode="rich", mode_resolved="rich", refresh_hz=10))
    renderer, _ = _rich_renderer(cfg)
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))        # 有运行中循环 ⇒ tick 任务在场
    assert renderer._tick_task is not None

    painted = {"n": 0}
    real_update = renderer._live.update

    def counting_update(*args, **kwargs):
        painted["n"] += 1
        return real_update(*args, **kwargs)

    renderer._live.update = counting_update    # type: ignore[method-assign]
    await asyncio.sleep(0.45)                  # 事件静默期（0.1 s 周期）
    assert painted["n"] >= 2
    renderer._stop_tick()


async def test_tick_loop_cancellation_is_swallowed(_finalize_renderers):
    """`_stop_tick()` 取消 tick：CancelledError 在协程内部消化（一条 debug），
    任务正常收尾而不是以 cancelled 态外抛。"""
    cfg = _cfg(ConsoleConfig(mode="rich", mode_resolved="rich", refresh_hz=10))
    renderer, _ = _rich_renderer(cfg)
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    task = renderer._tick_task
    await asyncio.sleep(0.15)                  # 确保任务已挂在 sleep 上

    renderer._stop_tick()
    assert renderer._tick_task is None         # 幂等：句柄先摘
    await asyncio.wait_for(task, timeout=2)    # 不外抛
    assert task.cancelled() is False
    renderer._stop_tick()                      # 二次调用是 no-op


async def test_tick_loop_exception_degrades_to_detached_plain(_finalize_renderers):
    """U7：tick 内任何异常自吞 + 一次性 WARN + 当场降级；rich 归属下落到
    脱离-plain（emitter 被静态门关掉，plain 行归渲染器）。"""
    handler = _ListHandler()
    logger = logging.getLogger("labelkit.console")
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        cfg = _cfg(ConsoleConfig(mode="rich", mode_resolved="rich", refresh_hz=10))
        renderer, _ = _rich_renderer(cfg)
        _finalize_renderers.append(renderer)
        renderer.on_event(_ev("run.start"))
        task = renderer._tick_task

        def boom(force: bool = False):
            raise RuntimeError("injected tick failure")

        renderer._maybe_refresh = boom         # type: ignore[method-assign]
        # 降级会顺手取消 tick 自身（_stop_live → _stop_tick），故任务以 cancelled
        # 收尾；生产侧无人 await 它——异常绝不外溢到运行。
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert task.done()
        assert renderer._mode == "detached"
        assert renderer._live_started is False
        warns = [r for r in handler.records if r.levelno == logging.WARNING]
        assert len(warns) == 1
        assert "console render failed, degraded to plain" in warns[0].getMessage()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


# ── 键盘轮询（spec §7.7「键盘轮询在渲染 tick 内非阻塞 select，零新线程」） ───


@pytest.fixture
def _pipe():
    """一对管道 fd：读端冒充 stdin（封闭键集经它逐字节喂进来）。"""
    read_fd, write_fd = os.pipe()
    yield read_fd, write_fd
    for fd in (read_fd, write_fd):
        with suppress(OSError):
            os.close(fd)


def _keyboard_on(renderer: ConsoleRenderer, read_fd: int) -> None:
    """把渲染器的键盘面接到给定 fd（真 pty 见本文件末尾的 cbreak 用例）。"""
    renderer._kbd_fd = read_fd
    renderer._kbd_active = True


def test_poll_keys_drains_pending_bytes_through_handle_key(_pipe,
                                                           _finalize_renderers):
    """轮询搭 `_maybe_refresh` 便车：待读字节逐个过 `_handle_key` 生效
    （封闭键集 `l` / `e`）。"""
    read_fd, write_fd = _pipe
    renderer, _ = _rich_renderer(_cfg(RICH), snapshot=_pool_snapshot)
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    _keyboard_on(renderer, read_fd)

    os.write(write_fd, b"le")
    renderer._maybe_refresh()
    assert renderer._show_keys is True and renderer._show_errors is True
    assert "LABELKIT_KEY_A ok  calls 150" in _canvas(renderer)


def test_poll_keys_stops_polling_after_detach(_pipe, _finalize_renderers):
    """`q` 脱离后立即返回——同一次轮询里排在它后面的字节**不再消费**
    （余下按键留给终端，不被面板吞掉）。"""
    read_fd, write_fd = _pipe
    renderer, _ = _rich_renderer(_cfg(RICH))
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    _keyboard_on(renderer, read_fd)

    os.write(write_fd, b"qle")
    renderer._poll_keys()
    assert renderer._mode == "detached"
    assert renderer._show_keys is False
    assert os.read(read_fd, 2) == b"le"


def test_poll_keys_returns_on_no_data_and_on_eof(_pipe, _finalize_renderers):
    """无待读数据（select 空）与 EOF（读到空串）两条早返回路径都不改状态。"""
    read_fd, write_fd = _pipe
    renderer, _ = _rich_renderer(_cfg(RICH))
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    _keyboard_on(renderer, read_fd)

    renderer._poll_keys()                      # 管道空：select 不就绪
    assert renderer._show_keys is False and renderer._mode == "rich"

    os.close(write_fd)                         # EOF：select 就绪但 read 空
    renderer._poll_keys()
    assert renderer._show_keys is False and renderer._mode == "rich"


@pytest.mark.parametrize("bad_fd_kind", ["closed", "negative"])
def test_poll_keys_logs_a_failed_select_once(bad_fd_kind, _finalize_renderers,
                                             caplog):
    """轮询失败只记一次：它搭每帧刷新的便车，逐帧记录会淹没日志
    （OSError 与 ValueError 两条 except 臂各钉一例）。"""
    renderer, _ = _rich_renderer(_cfg(RICH))
    _finalize_renderers.append(renderer)
    renderer.on_event(_ev("run.start"))
    if bad_fd_kind == "closed":
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        os.close(write_fd)
    else:
        read_fd = -1                           # select 拒负 fd（ValueError）
    _keyboard_on(renderer, read_fd)

    with caplog.at_level(logging.DEBUG, logger="labelkit.console"):
        renderer._poll_keys()
        renderer._poll_keys()
    assert renderer._kbd_poll_logged is True
    failures = [r for r in caplog.records if "key poll failed" in r.getMessage()]
    assert len(failures) == 1
    assert renderer._mode == "rich"            # 收不到键不影响渲染


# ── 心跳定时器回调（spec §7.7 plain 档可选心跳，U14） ────────────────────────


def _heartbeat_renderer(monkeypatch, clock: dict) -> tuple[ConsoleRenderer,
                                                           io.StringIO]:
    """plain × heartbeat_s>0 × 非 TTY 的心跳态渲染器（注入假 monotonic 时钟）。"""
    monkeypatch.setattr(console_mod, "_monotonic", lambda: clock["t"])
    fake_err = io.StringIO()                   # isatty() → False
    monkeypatch.setattr(sys, "stderr", fake_err)
    renderer = ConsoleRenderer()
    renderer.on_run_context(_cfg(ConsoleConfig(heartbeat_s=1)),
                            lambda: (), lambda: {}, lambda: 0)
    assert renderer._mode == "heartbeat"
    return renderer, fake_err


async def test_hb_fire_beats_and_rearms_at_the_advanced_deadline(monkeypatch):
    """定时器回调跳一拍后按**推进后的固定截止点**重新布防（追赶式，不随处理
    延迟漂移）——定时器独立于事件到达而触发，静默正是心跳的目标场景。"""
    clock = {"t": 1000.0}
    renderer, fake_err = _heartbeat_renderer(monkeypatch, clock)
    renderer._hb_t0, renderer._hb_next = 1000.0, 1000.0
    renderer._hb_batch, renderer._hb_stage, renderer._hb_calls = 7, "quality", 42
    clock["t"] = 1002.5                        # 截止点已过 ⇒ call_later(0)
    renderer._arm_hb_timer()
    first_handle = renderer._hb_handle
    assert first_handle is not None

    await asyncio.sleep(0.01)                  # 让事件循环跑到定时器
    assert fake_err.getvalue() == (
        "heartbeat batch=7 stage=quality llm_calls=42 elapsed=2s\n")
    assert renderer._hb_next == 1003.0         # 步进到 now 之后（非 now+1=1003.5）
    assert renderer._hb_handle is not None and renderer._hb_handle is not first_handle
    renderer._disarm_heartbeat()


async def test_hb_fire_exception_disarms_without_harming_the_run(monkeypatch,
                                                                 caplog):
    """定时器出问题绝不伤及运行：撤防（截止点与句柄双清）、一条 debug、
    不外抛——事件循环照常活着。"""
    clock = {"t": 1000.0}
    renderer, fake_err = _heartbeat_renderer(monkeypatch, clock)
    renderer._hb_t0, renderer._hb_next = 1000.0, 1000.0
    renderer._hb_check = (                     # type: ignore[method-assign]
        lambda: (_ for _ in ()).throw(RuntimeError("hb")))

    with caplog.at_level(logging.DEBUG, logger="labelkit.console"):
        renderer._arm_hb_timer()
        await asyncio.sleep(0.01)
    assert renderer._hb_next is None and renderer._hb_handle is None
    assert "heartbeat batch=" not in fake_err.getvalue()   # 这一拍没跳成
    assert any("heartbeat timer failed" in r.getMessage()
               for r in caplog.records)
    await asyncio.sleep(0)                     # 循环未被定时器异常打死

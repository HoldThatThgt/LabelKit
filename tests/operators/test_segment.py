"""Offline unit tests for M14 segment: prompt assembly (spec 3.14.4 / CONTRACTS §10.9),
the judge_window post-validation (first-wins, absent → "continues"), the window cut
(fixed fallback + the v1.11 V9 greedy budget packer — seam frame belongs to the later
window in both), the deterministic segment assembly (noise / boundary / min_len /
episode), strategy routing (rules and lone-frame sessions cost zero LLM), the on_error
two-path policy (S26) with the V27① error classification (context_overflow /
output_truncated), the V20 window-split degrade-retry (bounded halving, A7 breaker
terminal), the ②b contract, the V9 session-level digest precompute, and the
digest-poverty guard (S12, V4 wording). Pure logic only — no LLM: the schema engine is
replaced by the in-process complete_validated stubs (test_classify 惯例); budget-off
configs (no llm_profiles / context_window == 0) are the v1.10 regression anchor."""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from labelkit.operators.segment import (
    SegmentStage,
    _judge_span_degrading,
    _pack_windows,
    _reason_requested,
    _static_prompt_est,
    _window_spans,
    build_segment_prompt,
    judge_window,
    render_tree_diff,
)
from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ConsoleConfig,
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
from labelkit.common.errors import (
    ContextOverflowError,
    OutputTruncatedError,
    SchemaViolation,
)
from labelkit.common.runtime import budget as budget_mod
from labelkit.common.runtime.schema_engine import segment_window_schema
from labelkit.common.contracts.types import (
    ImageRef,
    PipelineItem,
    Record,
    RecordRef,
    UINode,
    UITree,
    Usage,
    frame_digest,
)


def make_cfg(*, strategy="hybrid", window=20, digest_max_chars=400,
             noise_filter=True, min_len=2, vision_resolved=False, context="",
             on_error="keep", trace=None, llm_profiles=None) -> ResolvedConfig:
    return ResolvedConfig(
        tool=ToolConfig(),
        console=ConsoleConfig(),
        llm_profiles=llm_profiles or {},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality="ui", input="in"),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(enabled=True, strategy=strategy, llm="default",
                              window=window, digest_max_chars=digest_max_chars,
                              noise_filter=noise_filter, min_len=min_len,
                              context=context, on_error=on_error,
                              vision_resolved=vision_resolved),
        stitch=StitchConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(instruction="标注"),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=trace or TraceConfig(),
        rubric=Rubric(name="default:ui", criteria=()),
        class_views={},
        user_schema={"type": "object"},
        limit=None,
        strict=False,
        dry_run=False,
        config_path="config.toml",
        project_path="project.toml",
        config_digest="sha256:0",
        project_digest="sha256:0",
    )


def llm_profile(*, context_window=0, max_output_tokens=256) -> LLMProfile:
    """A budget-declared (or not) segment profile — context_window == 0 is the
    v1.10 budget-off anchor, > 0 flips the V9 greedy packer on."""
    return LLMProfile(name="default", provider="openai_compatible",
                      base_url="http://localhost", model="glm-5.2",
                      api_key_env="K", max_output_tokens=max_output_tokens,
                      context_window=context_window)


def ui_frame(rid, pair_index, *, texts=("外卖首页推荐列表",),
             source="a/uitree_3.jsonl") -> Record:
    nodes = [UINode("1", None, 0, "FrameLayout", "", "", (0, 0, 1080, 1920),
                    True, {})]
    for j, text in enumerate(texts):
        nodes.append(UINode(str(j + 2), "1", 1, "TextView", text, "",
                            (0, j * 100, 1080, (j + 1) * 100), True, {}))
    image = ImageRef(path=Path(f"image_{pair_index}.png"), format="png",
                     size_bytes=1)
    return Record(id=rid, modality="ui", text=None, raw=None,
                  ui_tree=UITree(tuple(nodes)), image=image,
                  ref=RecordRef(source, None, pair_index, ()))


def bare_frame(rid, pair_index) -> Record:
    """Visible nodes but zero text/content_desc — digest_is_poor is True."""
    nodes = (UINode("1", None, 0, "FrameLayout", "", "", (0, 0, 1080, 1920),
                    True, {}),)
    image = ImageRef(path=Path(f"image_{pair_index}.png"), format="png",
                     size_bytes=1)
    return Record(id=rid, modality="ui", text=None, raw=None,
                  ui_tree=UITree(nodes), image=image,
                  ref=RecordRef("a/uitree_0.jsonl", None, pair_index, ()))


def envelope(record, sid="sess-0001") -> PipelineItem:
    return PipelineItem(record=record, session_id=sid)


def window_obj(*relations, reasons=None) -> dict:
    frames = []
    for i, relation in enumerate(relations):
        entry = {"index": i, "relation": relation}
        if reasons is not None:
            entry["reason"] = reasons[i]
        frames.append(entry)
    return {"frames": frames}


def digests_of(frames, max_chars=400) -> list[str]:
    """The session-level digest vector callers now precompute (V9)."""
    return [frame_digest(frame, max_chars) for frame in frames]


# ── in-process complete_validated stubs (no LLM, test_classify 惯例) ─────────

class QueueEngine:
    """Pops queued outcomes in call order."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list = []              # (profile, prompt, schema, record_ids)

    async def complete_validated(self, profile, prompt, schema=None, *,
                                 record_ids=(), batch_no=0, record=None):
        self.calls.append((profile, prompt, schema, record_ids))
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out, Usage(), 1, "glm-5.2"


class MapEngine:
    """Keyed by the window's FIRST frame id (record_ids=(窗首帧 id,)) —
    scheduling-independent for multi-window stage tests."""

    def __init__(self, by_first_frame):
        self.by_first_frame = dict(by_first_frame)
        self.calls: list = []

    async def complete_validated(self, profile, prompt, schema=None, *,
                                 record_ids=(), batch_no=0, record=None):
        self.calls.append((profile, prompt, schema, record_ids))
        out = self.by_first_frame[record_ids[0]]
        if isinstance(out, Exception):
            raise out
        return out, Usage(), 1, "glm-5.2"


class SpanEngine:
    """Keyed by (first frame id, window frame count) — an original window and
    its V20 sub-windows share the first frame, the frame count (read off the
    internal schema's pinned minItems) tells them apart."""

    def __init__(self, by_key):
        self.by_key = dict(by_key)
        self.calls: list = []              # (first frame id, frame count)

    async def complete_validated(self, profile, prompt, schema=None, *,
                                 record_ids=(), batch_no=0, record=None):
        key = (record_ids[0], schema["properties"]["frames"]["minItems"])
        self.calls.append(key)
        out = self.by_key[key]
        if isinstance(out, Exception):
            raise out
        return out, Usage(), 1, "glm-5.2"


class ExplodingEngine:
    async def complete_validated(self, *a, **k):
        raise AssertionError("complete_validated must not be called")


class RecordingMetrics:
    def __init__(self):
        self.events: list = []             # (ev, stage, record_ids, payload)
        self.counters: dict[str, int] = {}
        self.provider_results: list = []   # (fatal, hard) breaker feeds (A7)

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None):
        self.events.append((ev, stage, tuple(record_ids), dict(payload or {})))

    def count(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n

    def record_provider_result(self, fatal, *, hard=False):
        self.provider_results.append((fatal, hard))


class StubCalibrator:
    """ctx.llm.calibrator stand-in — a frozen per-image cost readout (V19
    batch-frozen snapshot); records reads to pin the once-per-session read."""

    def __init__(self, value):
        self.value = value
        self.calls: list = []

    def cost(self, profile):
        self.calls.append(profile)
        return self.value


def make_ctx(cfg, engine, llm=None):
    return SimpleNamespace(cfg=cfg, llm=llm, schema_engine=engine,
                           metrics=RecordingMetrics(), rng=None, batch_no=1)


def run_stage(cfg, batch, engine, stage=None, llm=None):
    ctx = make_ctx(cfg, engine, llm=llm)
    out = asyncio.run((stage or SegmentStage(cfg)).run(batch, ctx))
    return out, ctx


def status_tally(items):
    tally: dict[str, int] = {}
    for item in items:
        tally[item.status] = tally.get(item.status, 0) + 1
    return tally


def boundary_windows(ctx):
    """The dispatched window spans in event order (stub engines never yield,
    so gather runs jobs in creation order — deterministic)."""
    return [tuple(payload["window"]) for ev, _, _, payload in ctx.metrics.events
            if ev == "segment.boundary"]


# ── prompt assembly (§10.9, deterministic) ──────────────────────────────────

def test_system_message_verbatim_no_reason_no_context():
    frames = [ui_frame("f0", 3), ui_frame("f1", 4, texts=("搜索美食页面",)),
              ui_frame("f2", 5, texts=("麻辣烫搜索结果",))]
    bundle = build_segment_prompt(frames, [None, None, None], make_cfg(),
                                  False, digests_of(frames))
    assert [m.role for m in bundle.messages] == ["system", "user"]
    assert bundle.messages[0].parts[0].text == (
        "你是屏幕操作流的分段审核员。下面给出同一会话中按时间顺序排列的 3 帧状态摘要\n"
        "（含相邻帧的确定性变更提示）。按三步作业：\n"
        "一、双向上下文概括：通读全窗，把握每帧之前若干帧正在进行的活动与之后若干帧的走向，再判断该帧。\n"
        "二、逐帧关系分类：对每一帧，判断它相对进行中活动的功能角色，只能从以下封闭词表中取恰一值：\n"
        "- continues: 同一流程的推进。\n"
        "- advances: 屏幕或 App 变了，但可见的任务实体延续（验证码、订单号、餐厅名等跨屏出现）——\n"
        "  跨 App 的同一任务属此值，不是边界。\n"
        "- returns_to_entry: 回到入口/搜索/桌面后开启新流程（同 App 背靠背任务的断点）。\n"
        "- context_switch: 交互对象与环境不连续且无实体延续——相关但无实体延续的新流程也取此值。\n"
        "- interruption: 与前后活动均无关的短暂插入（通知、弹窗、误触）。\n"
        "三、只输出逐帧关系，不判断边界（边界由既定规则从关系推导）。\n"
        "锚定约定：分段粒度取「完整任务」层级（整段录屏之下一层）；只看前台 App/前台窗口，\n"
        "忽略状态栏、后台通知等背景变化。\n"
        "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
        '{"frames": [{"index": <窗内帧序号>, "relation": <词表值>}, ...]}（恰 3 项）'
    )
    assert bundle.temperature is None          # profile default (temp 0)


def test_system_message_reason_fragment_and_context_line():
    frames = [ui_frame("f0", 3), ui_frame("f1", 4)]
    cfg = make_cfg(context="这是手机屏幕操作流")
    text = build_segment_prompt(frames, [None, None], cfg, True,
                                digests_of(frames)).messages[0].parts[0].text
    # optional domain-context line sits between the anchors and the structure
    # sentence; reason fragment appears in the structure line when requested
    assert text.endswith(
        "忽略状态栏、后台通知等背景变化。\n"
        "这是手机屏幕操作流\n"
        "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
        '{"frames": [{"index": <窗内帧序号>, "relation": <词表值>, '
        '"reason": <一句话理由>}, ...]}（恰 2 项）')
    # default: no context line
    plain = build_segment_prompt(frames, [None, None], make_cfg(), False,
                                 digests_of(frames)).messages[0].parts[0].text
    assert "这是手机屏幕操作流" not in plain
    assert ("忽略状态栏、后台通知等背景变化。\n"
            "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n") in plain


def test_user_parts_frame_labels_and_diff_lines():
    frames = [ui_frame("f0", 3), ui_frame("f1", 4, texts=("搜索美食页面",))]
    diff = {"added": 2, "removed": 1, "text_changed": 3, "change_ratio": 0.25,
            "app_changed": True, "title_changed": False}
    bundle = build_segment_prompt(frames, [None, diff], make_cfg(), False,
                                  digests_of(frames))
    parts = bundle.messages[1].parts
    assert [p.kind for p in parts] == ["text", "text"]
    assert parts[0].text == f"[帧 0] {frame_digest(frames[0], 400)}"
    assert parts[1].text == (
        f"[帧 1] {frame_digest(frames[1], 400)}\n"
        "[帧 1 变更] 新增 2 节点，移除 1 节点，文本变化 3 处，变更比例 25%，应用切换")


def test_builder_consumes_supplied_digests_verbatim():
    # V9: the builder never digests frames itself — the supplied vector is
    # what lands in the "[帧 {i}] " lines, byte for byte.
    frames = [ui_frame("f0", 3), ui_frame("f1", 4)]
    bundle = build_segment_prompt(frames, [None, None], make_cfg(), False,
                                  ["摘要甲", "摘要乙"])
    parts = bundle.messages[1].parts
    assert parts[0].text == "[帧 0] 摘要甲"
    assert parts[1].text == "[帧 1] 摘要乙"


def test_render_tree_diff_fixed_format_and_flags():
    base = {"added": 0, "removed": 0, "text_changed": 0, "change_ratio": 0.0,
            "app_changed": False, "title_changed": False}
    assert render_tree_diff(base) == "新增 0 节点，移除 0 节点，文本变化 0 处，变更比例 0%"
    assert render_tree_diff({**base, "added": 2, "removed": 1, "text_changed": 3,
                             "change_ratio": 2 / 3, "app_changed": True,
                             "title_changed": True}) == (
        "新增 2 节点，移除 1 节点，文本变化 3 处，变更比例 67%，应用切换，标题变化")


def test_vision_resolved_two_state_parts_shape():
    frames = [ui_frame("f0", 3), ui_frame("f1", 4)]
    plain = build_segment_prompt(frames, [None, None], make_cfg(), False,
                                 digests_of(frames))
    assert [p.kind for p in plain.messages[1].parts] == ["text", "text"]
    vision = build_segment_prompt(frames, [None, None],
                                  make_cfg(vision_resolved=True), False,
                                  digests_of(frames))
    parts = vision.messages[1].parts
    assert [p.kind for p in parts] == ["image", "text", "image", "text"]
    assert parts[0].image is frames[0].image     # each digest preceded by its frame
    assert parts[2].image is frames[1].image
    assert parts[1].text.startswith("[帧 0] ")
    assert parts[3].text.startswith("[帧 1] ")


# ── reason request condition (R29 construction, §8.1 †) ─────────────────────

def test_reason_requested_iff_trace_enabled_and_segment_channel():
    assert _reason_requested(make_cfg()) is False
    assert _reason_requested(make_cfg(
        trace=TraceConfig(enabled=True, channels=("quality", "verify")))) is False
    assert _reason_requested(make_cfg(
        trace=TraceConfig(enabled=False, channels=("segment",)))) is False
    assert _reason_requested(make_cfg(
        trace=TraceConfig(enabled=True, channels=("quality", "segment")))) is True


# ── judge_window: post-validation + boundary event ──────────────────────────

def test_judge_window_first_wins_and_absent_defaults_to_continues():
    frames = [ui_frame("f0", 3), ui_frame("f1", 4), ui_frame("f2", 5)]
    engine = QueueEngine([{"frames": [
        {"index": 2, "relation": "context_switch"},     # first occurrence wins
        {"index": 2, "relation": "interruption"},
    ]}])
    ctx = make_ctx(make_cfg(), engine)
    verdicts = asyncio.run(judge_window(frames, ctx))
    assert verdicts == ["continues", "continues", "context_switch"]
    profile, prompt, schema, record_ids = engine.calls[0]
    assert profile == "default"
    assert record_ids == ("f0",)                        # 窗首帧 id
    assert schema == segment_window_schema(3, with_reason=False)
    (ev, stage, ev_record_ids, payload), = ctx.metrics.events
    assert (ev, stage, ev_record_ids) == ("segment.boundary", "segment", ())
    assert payload == {
        "session_id": None,                             # public direct-call surface
        "window": [0, 3],
        "member_ids": ["f0", "f1", "f2"],
        "relations": [{"index": 0, "relation": "continues"},
                      {"index": 1, "relation": "continues"},
                      {"index": 2, "relation": "context_switch"}],
        "model": "glm-5.2",
    }


def test_judge_window_precomputes_adjacent_diffs_into_prompt():
    frames = [ui_frame("f0", 3),
              ui_frame("f1", 4, texts=("外卖首页推荐列表", "搜索美食页面"))]
    engine = QueueEngine([window_obj("continues", "continues")])
    ctx = make_ctx(make_cfg(), engine)
    asyncio.run(judge_window(frames, ctx))
    parts = engine.calls[0][1].messages[1].parts
    assert "[帧 1 变更] " not in parts[0].text          # 窗首帧无此行
    # one TextView added between the frames → deterministic diff line
    assert parts[1].text.endswith(
        "\n[帧 1 变更] 新增 1 节点，移除 0 节点，文本变化 0 处，变更比例 33%")


def test_judge_window_with_reason_schema_and_reason_list_in_payload():
    frames = [ui_frame("f0", 3), ui_frame("f1", 4)]
    trace = TraceConfig(enabled=True, channels=("segment",))
    engine = QueueEngine([window_obj("continues", "advances",
                                     reasons=("同一流程", "实体延续"))])
    ctx = make_ctx(make_cfg(trace=trace), engine)
    verdicts = asyncio.run(judge_window(frames, ctx))
    assert verdicts == ["continues", "advances"]
    schema = engine.calls[0][2]
    assert schema == segment_window_schema(2, with_reason=True)
    (_, _, _, payload), = ctx.metrics.events
    assert payload["reason"] == ["同一流程", "实体延续"]
    # structure line carries the reason fragment when requested
    assert engine.calls[0][1].messages[0].parts[0].text.endswith(
        '"reason": <一句话理由>}, ...]}（恰 2 项）')


def test_public_judge_window_self_computes_digests(monkeypatch):
    # The frozen public surface (M7's reclaim re-judgment, verify.py) passes no
    # digest vector — _judge_window computes its own table (CONTRACTS §7.14).
    calls = []

    def counting(record, max_chars):
        calls.append((record.id, max_chars))
        return frame_digest(record, max_chars)

    monkeypatch.setattr("labelkit.operators.segment.frame_digest", counting)
    frames = [ui_frame("f0", 3), ui_frame("f1", 4)]
    engine = QueueEngine([window_obj("continues", "continues")])
    asyncio.run(judge_window(frames, make_ctx(make_cfg(), engine)))
    assert calls == [("f0", 400), ("f1", 400)]
    parts = engine.calls[0][1].messages[1].parts
    assert parts[0].text == f"[帧 0] {frame_digest(frames[0], 400)}"


# ── window cut: fixed fallback (budget off) + greedy packer (V9) ─────────────

def test_window_spans_fixed_fallback_stride_is_window_minus_one():
    # The budget-undeclared cut — byte-identical to v1.10 (regression anchor).
    assert _window_spans(5, 20) == [(0, 5)]
    assert _window_spans(3, 2) == [(0, 2), (1, 3)]
    assert _window_spans(21, 20) == [(0, 20), (19, 21)]
    assert _window_spans(39, 20) == [(0, 20), (19, 39)]


def test_pack_windows_cap_ceiling_degrades_to_fixed_windows():
    # Unconstrained budget → the frame-count cap alone cuts: the packer's
    # spans coincide with the fixed v1.10 shape for the same (n, window).
    for n, cap in ((5, 20), (3, 2), (21, 20), (39, 20)):
        assert _pack_windows([1] * n, 10 ** 9, cap) == _window_spans(n, cap)


def test_pack_windows_overflow_closes_window_with_one_frame_overlap():
    # frames priced 100, budget 350 → 3 per window; each subsequent window
    # starts at the previous end − 1 (seam owned by the later window).
    assert _pack_windows([100] * 6, 350, 20) == [(0, 3), (2, 5), (4, 6)]


def test_pack_windows_heterogeneous_costs_cut_at_cost_boundary():
    costs = [50, 50, 200, 50, 50]
    # 50+50+200 = 300 fits, +50 overflows → close; resume at the seam frame.
    assert _pack_windows(costs, 300, 20) == [(0, 3), (2, 5)]


def test_pack_windows_min_two_frames_at_exact_two_frame_budget():
    # The w_min == floor degenerate shape: every window exactly 2 frames,
    # every frame a seam.
    assert _pack_windows([10] * 5, 20, 20) == [(0, 2), (1, 3), (2, 4), (3, 5)]


def test_pack_windows_forces_two_frames_when_budget_would_close_below():
    # The M1 w_min ≥ floor guard is PRIOR-based; the packer prices off the
    # calibrator, which may legally exceed the prior (V19, no clamp). A window
    # the budget would close below 2 frames is force-packed at 2 (the V10
    # semantic minimum) — the M9 precheck owns any true overflow record-level;
    # never an assert, never a run-kill.
    assert _pack_windows([100, 100], 150, 20) == [(0, 2)]


def test_pack_windows_every_frame_over_budget_terminates_with_forced_pairs():
    # Worst degenerate shape: every frame alone exceeds the budget. The old
    # assert fired here (and under python -O the loop never advanced); the
    # forced minimum keeps the overlap chain and terminates.
    assert _pack_windows([200, 200, 200], 150, 20) == [(0, 2), (1, 3)]
    assert _pack_windows([200] * 5, 150, 20) == [(0, 2), (1, 3), (2, 4), (3, 5)]


def test_pack_windows_deterministic_rerun():
    costs = [37, 91, 14, 88, 65, 42, 73, 29, 55, 61]
    first = _pack_windows(costs, 200, 4)
    assert first == _pack_windows(costs, 200, 4)        # pure function
    assert first[0][0] == 0
    for (s1, e1), (s2, e2) in zip(first, first[1:]):
        assert s2 == e1 - 1                             # 1-frame overlap chain
    assert all(2 <= e - s <= 4 for s, e in first)       # min-2 ∧ cap
    assert first[-1][1] == len(costs)                   # every frame covered


def test_static_prompt_est_tracks_context_and_reason_variants():
    # The packer's est_static_system term follows build_segment_prompt's
    # actual static parts: the optional context line and the longer reason
    # structure variant both grow it deterministically.
    base = _static_prompt_est(make_cfg())
    assert base > 2 * budget_mod.MSG_OVERHEAD_TOKENS
    assert _static_prompt_est(make_cfg(context="这是手机屏幕操作流")) > base
    with_reason = _static_prompt_est(make_cfg(
        trace=TraceConfig(enabled=True, channels=("segment",))))
    assert with_reason > base


# ── stitching across windows (fixed cut, budget off) ────────────────────────

def test_stitching_seam_frame_belongs_to_later_window():
    cfg = make_cfg(window=2, min_len=1)
    frames = [ui_frame("f0", 0), ui_frame("f1", 1), ui_frame("f2", 2)]
    batch = [envelope(r) for r in frames]
    # window (0,2) judges the seam frame interruption; window (1,3) overwrites
    # it with advances — the later window's whole verdict wins
    engine = MapEngine({"f0": window_obj("continues", "interruption"),
                        "f1": window_obj("advances", "continues")})
    out, ctx = run_stage(cfg, batch, engine)
    assert len(engine.calls) == 2
    assert ctx.metrics.counters["segment.windows"] == 2  # V13④ actual windows
    assert status_tally(batch[:3]) == {"absorbed": 3}   # nothing dropped as noise
    (episode,) = batch[3:]
    assert [r.id for r in episode.record.members] == ["f0", "f1", "f2"]


# ── segment assembly: boundary / first frame / noise / min_len ───────────────

def test_boundary_relations_split_advances_and_continues_do_not():
    cfg = make_cfg()
    frames = [ui_frame(f"f{i}", i) for i in range(6)]
    batch = [envelope(r) for r in frames]
    engine = MapEngine({"f0": window_obj(
        "continues", "advances", "returns_to_entry", "continues",
        "context_switch", "continues")})
    out, _ = run_stage(cfg, batch, engine)
    episodes = batch[6:]
    assert [[r.id for r in e.record.members] for e in episodes] == [
        ["f0", "f1"], ["f2", "f3"], ["f4", "f5"]]
    assert status_tally(batch[:6]) == {"absorbed": 6}


def test_session_first_frame_boundary_value_is_ignored():
    cfg = make_cfg()
    frames = [ui_frame("f0", 0), ui_frame("f1", 1)]
    batch = [envelope(r) for r in frames]
    engine = MapEngine({"f0": window_obj("returns_to_entry", "continues")})
    out, _ = run_stage(cfg, batch, engine)
    (episode,) = batch[2:]                              # one episode, no split
    assert [r.id for r in episode.record.members] == ["f0", "f1"]


def test_noise_frame_dropped_with_noise_attribution():
    cfg = make_cfg()
    frames = [ui_frame("f0", 0), ui_frame("f1", 1), ui_frame("f2", 2)]
    batch = [envelope(r) for r in frames]
    engine = MapEngine({"f0": window_obj("interruption", "continues",
                                         "continues")})
    out, ctx = run_stage(cfg, batch, engine)            # noise[0] still applies
    assert batch[0].status == "dropped_noise"
    assert batch[0].noise_attribution == ("segment", "noise")
    (episode,) = batch[3:]
    assert [r.id for r in episode.record.members] == ["f1", "f2"]
    # conservation: frames = absorbed + dropped_noise
    assert status_tally(batch[:3]) == {"absorbed": 2, "dropped_noise": 1}
    assert "segment.below_min_len" not in ctx.metrics.counters


def test_below_min_len_segment_dropped_with_independent_attribution():
    cfg = make_cfg(min_len=2)
    frames = [ui_frame("f0", 0), ui_frame("f1", 1), ui_frame("f2", 2)]
    batch = [envelope(r) for r in frames]
    engine = MapEngine({"f0": window_obj("continues", "context_switch",
                                         "continues")})
    out, ctx = run_stage(cfg, batch, engine)
    # segments: [f0] (< min_len → dropped), [f1, f2] (episode)
    assert batch[0].status == "dropped_noise"
    assert batch[0].noise_attribution == ("segment", "below_min_len")
    assert ctx.metrics.counters["segment.below_min_len"] == 1
    (episode,) = batch[3:]
    assert [r.id for r in episode.record.members] == ["f1", "f2"]
    assert status_tally(batch[:3]) == {"absorbed": 2, "dropped_noise": 1}


def test_noise_filter_off_keeps_interruption_as_non_boundary_member():
    cfg = make_cfg(noise_filter=False)
    frames = [ui_frame("f0", 0), ui_frame("f1", 1), ui_frame("f2", 2)]
    batch = [envelope(r) for r in frames]
    engine = MapEngine({"f0": window_obj("continues", "interruption",
                                         "continues")})
    out, ctx = run_stage(cfg, batch, engine)
    assert status_tally(batch[:3]) == {"absorbed": 3}   # nothing dropped
    (episode,) = batch[3:]
    assert [r.id for r in episode.record.members] == ["f0", "f1", "f2"]
    assert not hasattr(batch[1], "noise_attribution")


# ── episode assembly (spec 3.14.2 worked example) ────────────────────────────

SPEC_IDS = ("b3a1c4e29d70f512", "4c8e02d9a1b6f374", "9a7d33c8b1e4f062",
            "e07b94a3c25d18f6", "61f8d0b4a9c3e725")


def test_episode_assembly_spec_worked_example():
    cfg = make_cfg()
    frames = [ui_frame(rid, 3 + i) for i, rid in enumerate(SPEC_IDS)]
    batch = [envelope(r, sid="sess-0003") for r in frames]
    engine = MapEngine({SPEC_IDS[0]: window_obj(
        "continues", "continues", "interruption", "advances", "continues")})
    out, ctx = run_stage(cfg, batch, engine)
    assert out is batch and len(batch) == 6
    assert [item.status for item in batch[:5]] == [
        "absorbed", "absorbed", "dropped_noise", "absorbed", "absorbed"]
    assert batch[2].noise_attribution == ("segment", "noise")
    episode = batch[5]
    assert episode.status == "active"
    assert episode.session_id == "sess-0003"
    assert episode.transitions is None
    record = episode.record
    assert record.id == "7655568d2c485c43"              # sha256("\n".join(ids))[:16]
    assert record.id == hashlib.sha256(
        "\n".join([SPEC_IDS[0], SPEC_IDS[1], SPEC_IDS[3],
                   SPEC_IDS[4]]).encode("utf-8")).hexdigest()[:16]
    assert record.kind == "sequence"
    assert record.modality == "ui"
    assert record.text is None and record.raw is None
    assert record.ui_tree is None and record.image is None
    assert record.members == (frames[0], frames[1], frames[3], frames[4])
    ref = record.ref
    assert ref.source_file == "a/uitree_3.jsonl"        # inherited from first member
    assert ref.pair_index == 3 and ref.line_no is None
    assert ref.generated_from == () and ref.generator is None
    # one segment.boundary event for the single window
    (ev, stage, record_ids, payload), = ctx.metrics.events
    assert (ev, stage, record_ids) == ("segment.boundary", "segment", ())
    assert payload["session_id"] == "sess-0003"
    assert payload["window"] == [0, 5]
    assert payload["member_ids"] == list(SPEC_IDS)
    assert payload["relations"] == [
        {"index": 0, "relation": "continues"},
        {"index": 1, "relation": "continues"},
        {"index": 2, "relation": "interruption"},
        {"index": 3, "relation": "advances"},
        {"index": 4, "relation": "continues"}]
    assert payload["model"] == "glm-5.2" and "reason" not in payload


def test_session_split_mark_propagates_to_episode():
    cfg = make_cfg()
    frames = [ui_frame("f0", 0), ui_frame("f1", 1)]
    batch = [envelope(r, sid="sess-0009") for r in frames]
    batch[1].session_split = True                       # M10's hard-split mark (S21)
    engine = MapEngine({"f0": window_obj("continues", "continues")})
    out, _ = run_stage(cfg, batch, engine)
    (episode,) = batch[2:]
    assert episode.session_split is True
    # unmarked sessions carry no duck attribute at all
    batch2 = [envelope(ui_frame("g0", 0), sid="s2"),
              envelope(ui_frame("g1", 1), sid="s2")]
    out2, _ = run_stage(cfg, batch2, MapEngine({"g0": window_obj("continues",
                                                                 "continues")}))
    assert not hasattr(batch2[2], "session_split")


# ── strategy routing: rules and lone-frame sessions cost zero LLM ────────────

def test_rules_strategy_zero_llm_and_min_len_not_applied():
    cfg = make_cfg(strategy="rules", min_len=5)
    batch = [envelope(ui_frame("f0", 0), sid="s1"),
             envelope(ui_frame("f1", 1), sid="s1"),
             envelope(ui_frame("f2", 2), sid="s1"),
             envelope(ui_frame("g0", 3), sid="s2")]     # lone-frame session
    out, ctx = run_stage(cfg, batch, ExplodingEngine())  # no LLM call happens
    assert out is batch and len(batch) == 6
    ep1, ep2 = batch[4], batch[5]                       # session order preserved
    assert [r.id for r in ep1.record.members] == ["f0", "f1", "f2"]
    assert [r.id for r in ep2.record.members] == ["g0"]
    assert (ep1.session_id, ep2.session_id) == ("s1", "s2")
    assert status_tally(batch[:4]) == {"absorbed": 4}   # min_len=5 did NOT drop
    assert ctx.metrics.events == [] and ctx.metrics.counters == {}


def test_hybrid_lone_frame_session_degrades_to_rules():
    cfg = make_cfg(strategy="hybrid", min_len=2)
    batch = [envelope(ui_frame("f0", 0))]
    out, ctx = run_stage(cfg, batch, ExplodingEngine())
    assert len(batch) == 2
    assert batch[0].status == "absorbed"
    assert [r.id for r in batch[1].record.members] == ["f0"]  # min_len not applied
    assert ctx.metrics.events == [] and ctx.metrics.counters == {}


def test_ignores_non_active_sequence_and_unstamped_items():
    cfg = make_cfg()
    dup = PipelineItem(record=ui_frame("d0", 0), status="dropped_dup",
                       session_id="s1")
    unstamped = PipelineItem(record=ui_frame("u0", 1))  # session_id None: ignored
    seq = PipelineItem(record=Record(
        id="e" * 16, modality="ui", text=None, raw=None, ui_tree=None,
        image=None, ref=RecordRef("a/uitree_0.jsonl", None, 0, ()),
        kind="sequence", members=(ui_frame("m0", 0),)), session_id="s1")
    batch = [dup, unstamped, seq]
    out, ctx = run_stage(cfg, batch, ExplodingEngine())
    assert out is batch and len(batch) == 3
    assert dup.status == "dropped_dup"
    assert unstamped.status == "active"
    assert seq.status == "active"
    assert ctx.metrics.events == [] and ctx.metrics.counters == {}


# ── on_error two paths (S26) — budget off, v1.10 regression anchor ───────────
# (counters gain the unconditional v1.11 segment.windows dispatch count, V13④;
# every other asserted value is the v1.10 one.)

def test_on_error_keep_degrades_session_without_item_errors():
    cfg = make_cfg(window=2, on_error="keep")
    frames = [ui_frame("f0", 0), ui_frame("f1", 1), ui_frame("f2", 2)]
    batch = [envelope(r, sid="sess-0007") for r in frames]
    violation = SchemaViolation(["/frames: 长度不符"], '{"frames": []}')
    engine = MapEngine({"f0": window_obj("continues", "interruption"),
                        "f1": violation})
    out, ctx = run_stage(cfg, batch, engine)
    # the session abandons ALL window verdicts (incl. the successful window's
    # interruption) and survives whole as ONE episode
    assert status_tally(batch[:3]) == {"absorbed": 3}
    assert all(item.errors == [] for item in batch[:3])  # never item.errors
    (episode,) = batch[3:]
    assert episode.status == "active"
    assert [r.id for r in episode.record.members] == ["f0", "f1", "f2"]
    assert episode.segment_degraded == {"kind": "segmentation_invalid",
                                        "windows_failed": 1}
    assert ctx.metrics.counters == {"segment.failures": 1,
                                    "segment.windows": 2}
    error_events = [e for e in ctx.metrics.events if e[0] == "error"]
    assert len(error_events) == 1
    assert error_events[0][1] == "segment" and error_events[0][2] == ()
    assert error_events[0][3] == {"stage": "segment",
                                  "kind": "segmentation_invalid",
                                  "message": "/frames: 长度不符",
                                  "retryable": False}
    # the succeeded window still emitted its boundary event before the failure
    assert [e[0] for e in ctx.metrics.events].count("segment.boundary") == 1


def test_on_error_fail_marks_all_session_members_failed():
    cfg = make_cfg(window=2, on_error="fail")
    frames = [ui_frame("f0", 0), ui_frame("f1", 1), ui_frame("f2", 2)]
    batch = [envelope(r) for r in frames]
    violation = SchemaViolation(["/frames: 长度不符"], "{}")
    engine = MapEngine({"f0": window_obj("continues", "continues"),
                        "f1": violation})
    out, ctx = run_stage(cfg, batch, engine)
    assert len(batch) == 3                              # no episode appended
    assert status_tally(batch) == {"failed": 3}
    for item in batch:
        (err,) = item.errors
        assert (err.stage, err.kind, err.retryable) == (
            "segment", "segmentation_invalid", False)
        assert err.message == "/frames: 长度不符"
    assert ctx.metrics.counters == {"segment.failures": 1,
                                    "segment.windows": 2}
    assert [e[0] for e in ctx.metrics.events].count("error") == 1


def test_unexpected_exception_follows_on_error_disposition():
    cfg = make_cfg(on_error="keep")
    frames = [ui_frame("f0", 0), ui_frame("f1", 1)]
    batch = [envelope(r) for r in frames]
    engine = MapEngine({"f0": ValueError("boom")})      # 合同④ record-level isolation
    out, ctx = run_stage(cfg, batch, engine)
    (episode,) = batch[2:]
    assert episode.segment_degraded == {"kind": "segmentation_invalid",
                                        "windows_failed": 1}
    assert status_tally(batch[:2]) == {"absorbed": 2}
    assert all(item.errors == [] for item in batch[:2])
    assert ctx.metrics.counters == {"segment.failures": 1,
                                    "segment.windows": 1}


def test_multiple_failed_windows_counted_in_degraded_evidence():
    cfg = make_cfg(window=2, on_error="keep")
    frames = [ui_frame(f"f{i}", i) for i in range(5)]   # windows (0,2)(1,3)(2,4)(3,5)
    batch = [envelope(r) for r in frames]
    violation = SchemaViolation(["/frames: bad"], "{}")
    engine = MapEngine({"f0": violation, "f1": window_obj("continues", "continues"),
                        "f2": violation, "f3": window_obj("continues", "continues")})
    out, ctx = run_stage(cfg, batch, engine)
    (episode,) = batch[5:]
    assert episode.segment_degraded == {"kind": "segmentation_invalid",
                                        "windows_failed": 2}
    assert ctx.metrics.counters == {"segment.failures": 1,   # one per session
                                    "segment.windows": 4}


# ── V27① error classification: overflow / truncated window failures ─────────

def test_reactive_overflow_without_budget_classifies_context_overflow():
    # Budget off (no declared window): the 200-shaped oracle (origin="finish")
    # takes the plain failure path — no degrade, no breaker feed — and the
    # kind routes through budget.classify_stage_error (V27①).
    cfg = make_cfg(on_error="keep")
    frames = [ui_frame("f0", 0), ui_frame("f1", 1)]
    batch = [envelope(r) for r in frames]
    overflow = ContextOverflowError("prompt too long", phase="reactive",
                                    profile="default", origin="finish")
    out, ctx = run_stage(cfg, batch, MapEngine({"f0": overflow}))
    (episode,) = batch[2:]
    assert episode.segment_degraded == {"kind": "context_overflow",
                                        "windows_failed": 1}
    assert ctx.metrics.counters == {"segment.failures": 1,
                                    "segment.windows": 1}
    assert ctx.metrics.provider_results == []           # never fed budget-off
    (err_event,) = [e for e in ctx.metrics.events if e[0] == "error"]
    assert err_event[3]["kind"] == "context_overflow"


def test_output_truncated_window_classifies_without_degrade():
    # Output-side event (V11): no split retry even with the budget on — a
    # single dispatched window, kind="output_truncated", breaker untouched.
    cfg = make_cfg(on_error="keep",
                   llm_profiles={"default": llm_profile(context_window=131072)})
    frames = [ui_frame("f0", 0), ui_frame("f1", 1)]
    batch = [envelope(r) for r in frames]
    truncated = OutputTruncatedError("output cap", profile="default",
                                     finish="length")
    out, ctx = run_stage(cfg, batch, MapEngine({"f0": truncated}))
    (episode,) = batch[2:]
    assert episode.segment_degraded == {"kind": "output_truncated",
                                        "windows_failed": 1}
    assert ctx.metrics.counters == {"segment.failures": 1,
                                    "segment.windows": 1}
    assert "budget.degrade_retries" not in ctx.metrics.counters
    assert ctx.metrics.provider_results == []


def test_overflow_on_error_fail_writes_context_overflow_stage_error():
    cfg = make_cfg(on_error="fail",
                   llm_profiles={"default": llm_profile(context_window=131072)})
    frames = [ui_frame("f0", 0), ui_frame("f1", 1)]     # minimal 2-frame window
    batch = [envelope(r) for r in frames]
    overflow = ContextOverflowError("400 sniff", phase="reactive",
                                    profile="default", origin="http_400")
    out, ctx = run_stage(cfg, batch, MapEngine({"f0": overflow}))
    assert status_tally(batch) == {"failed": 2}
    for item in batch:
        (err,) = item.errors
        assert (err.stage, err.kind) == ("segment", "context_overflow")
    # V13② reject-site convention (finding 3): one count per rejected member.
    assert ctx.metrics.counters["budget.overflow_records"] == 2
    # minimal window ⇒ terminal at once; the 400-sniffed form feeds the
    # breaker exactly once (A7)
    assert ctx.metrics.provider_results == [(True, False)]
    assert "budget.degrade_retries" not in ctx.metrics.counters


# ── V20 window-split degrade-retry (unit seam: injected judge) ───────────────

def degrade_ctx():
    return SimpleNamespace(cfg=None, llm=None, schema_engine=None,
                           metrics=RecordingMetrics(), rng=None, batch_no=1)


def run_degrading(judge, span, ctx):
    return asyncio.run(_judge_span_degrading(judge, span, ctx))


def test_degrade_splits_once_and_merges_leaves_in_span_order():
    calls = []

    async def judge(span):
        calls.append(span)
        if span == (0, 8):
            raise ContextOverflowError("over", phase="reactive",
                                       origin="http_400")
        return [f"v{span[0]}:{i}" for i in range(span[1] - span[0])]

    ctx = degrade_ctx()
    results = run_degrading(judge, (0, 8), ctx)
    # halves [s, m+1) / [m, e) keep the 1-frame overlap (frame 4 judged twice,
    # later sub-window owns it via the assembly overwrite order)
    assert calls == [(0, 8), (0, 5), (4, 8)]
    assert [span for span, _ in results] == [(0, 5), (4, 8)]
    assert results[0][1] == ["v0:0", "v0:1", "v0:2", "v0:3", "v0:4"]
    assert ctx.metrics.counters == {"budget.degrade_retries": 1}
    assert ctx.metrics.provider_results == []           # degrade succeeded


def test_degrade_second_level_halving_of_a_half():
    calls = []

    async def judge(span):
        calls.append(span)
        if span in {(0, 8), (0, 5)}:
            raise ContextOverflowError("over", phase="reactive",
                                       origin="http_400")
        return ["continues"] * (span[1] - span[0])

    ctx = degrade_ctx()
    results = run_degrading(judge, (0, 8), ctx)
    assert calls == [(0, 8), (0, 5), (0, 3), (2, 5), (4, 8)]
    assert [span for span, _ in results] == [(0, 3), (2, 5), (4, 8)]
    assert ctx.metrics.counters == {"budget.degrade_retries": 2}
    assert ctx.metrics.provider_results == []


def test_degrade_level_bound_terminal_feeds_breaker_once_for_http_400():
    calls = []

    async def judge(span):
        calls.append(span)
        raise ContextOverflowError("over", phase="reactive", origin="http_400")

    ctx = degrade_ctx()
    with pytest.raises(ContextOverflowError):
        run_degrading(judge, (0, 16), ctx)
    # level 0 (0,16) → level 1 (0,9) → level 2 (0,5): the level bound makes it
    # terminal — the tree stops at the first terminal leaf (sequential halves)
    assert calls == [(0, 16), (0, 9), (0, 5)]
    assert ctx.metrics.counters == {"budget.degrade_retries": 2}
    assert ctx.metrics.provider_results == [(True, False)]   # exactly once (A7)


def test_degrade_minimal_two_frame_window_is_terminal():
    async def judge(span):
        raise ContextOverflowError("over", phase="reactive", origin="http_400")

    ctx = degrade_ctx()
    with pytest.raises(ContextOverflowError):
        run_degrading(judge, (0, 2), ctx)               # cannot split below 2+2
    assert "budget.degrade_retries" not in ctx.metrics.counters
    assert ctx.metrics.provider_results == [(True, False)]


def test_degrade_terminal_finish_origin_never_feeds_breaker():
    # SPEC §3.5: the 200-shaped oracle rode a successful HTTP interaction —
    # its terminal never feeds the breaker (streak already cleared by the ok).
    async def judge(span):
        raise ContextOverflowError("over", phase="reactive", origin="finish")

    ctx = degrade_ctx()
    with pytest.raises(ContextOverflowError):
        run_degrading(judge, (0, 2), ctx)
    assert ctx.metrics.provider_results == []


def test_degrade_precheck_phase_never_degrades_or_feeds():
    calls = []

    async def judge(span):
        calls.append(span)
        raise ContextOverflowError("packing bug", phase="precheck")

    ctx = degrade_ctx()
    with pytest.raises(ContextOverflowError):
        run_degrading(judge, (0, 8), ctx)
    assert calls == [(0, 8)]                            # no split attempted
    assert ctx.metrics.counters == {}
    assert ctx.metrics.provider_results == []


# ── V20 through the stage (budget on) ────────────────────────────────────────

def test_stage_degrade_retry_merges_sub_window_verdicts():
    cfg = make_cfg(on_error="keep",
                   llm_profiles={"default": llm_profile(context_window=131072)})
    frames = [ui_frame(f"f{i}", i) for i in range(5)]   # one packed window (0,5)
    batch = [envelope(r) for r in frames]
    overflow = ContextOverflowError("over", phase="reactive",
                                    origin="http_400", profile="default")
    engine = SpanEngine({
        ("f0", 5): overflow,                            # original window
        ("f0", 3): window_obj("continues", "continues", "continues"),  # (0,3)
        ("f2", 3): window_obj("continues", "context_switch", "continues"),  # (2,5)
    })
    out, ctx = run_stage(cfg, batch, engine)
    # sub-window verdicts land at session positions: boundary at frame 3
    episodes = batch[5:]
    assert [[r.id for r in e.record.members] for e in episodes] == [
        ["f0", "f1", "f2"], ["f3", "f4"]]
    assert status_tally(batch[:5]) == {"absorbed": 5}
    assert ctx.metrics.counters["budget.degrade_retries"] == 1
    assert ctx.metrics.counters["segment.windows"] == 3  # 1 original + 2 subs
    assert "segment.failures" not in ctx.metrics.counters
    assert ctx.metrics.provider_results == []           # degrade succeeded
    assert boundary_windows(ctx) == [(0, 3), (2, 5)]    # leaves in span order


def test_stage_degrade_exhaustion_disposes_context_overflow_and_feeds_once():
    cfg = make_cfg(on_error="keep",
                   llm_profiles={"default": llm_profile(context_window=131072)})
    frames = [ui_frame(f"f{i}", i) for i in range(3)]   # one packed window (0,3)
    batch = [envelope(r) for r in frames]
    overflow = ContextOverflowError("over", phase="reactive",
                                    origin="http_400", profile="default")
    engine = SpanEngine({("f0", 3): overflow,           # original window
                         ("f0", 2): overflow})          # first half (0,2): minimal
    out, ctx = run_stage(cfg, batch, engine)
    # first half's terminal stops the tree — the second half is never judged
    assert engine.calls == [("f0", 3), ("f0", 2)]
    (episode,) = batch[3:]
    assert episode.segment_degraded == {"kind": "context_overflow",
                                        "windows_failed": 1}
    assert all(item.errors == [] for item in batch[:3])  # keep: never item.errors
    assert ctx.metrics.counters == {"segment.failures": 1,
                                    "segment.windows": 2,
                                    "budget.degrade_retries": 1}
    assert ctx.metrics.provider_results == [(True, False)]  # exactly once (A7)
    (err_event,) = [e for e in ctx.metrics.events if e[0] == "error"]
    assert err_event[3]["kind"] == "context_overflow"


# ── budget-on packing through the stage (V9) ─────────────────────────────────

def packed_spans(cfg, frames, image_cost=0):
    """The spans the stage is expected to dispatch — computed with the same
    production primitives the stage uses (wiring equality; the packer's own
    arithmetic is pinned by the direct _pack_windows tests above)."""
    prof = cfg.llm_profiles["default"]
    costs = [budget_mod.est_text(d) + budget_mod.DIFF_MAX_TOKENS + image_cost
             for d in digests_of(frames, cfg.segment.digest_max_chars)]
    return _pack_windows(costs, budget_mod.input_budget(prof)
                         - _static_prompt_est(cfg), cfg.segment.window)


def test_budget_on_cost_driven_splits_below_the_cap():
    # A small declared window: the budget, not the frame-count cap, cuts the
    # session (cap 20 stays unfilled).
    cfg = make_cfg(min_len=1,
                   llm_profiles={"default": llm_profile(context_window=2048)})
    frames = [ui_frame(f"f{i}", i) for i in range(10)]
    expected = packed_spans(cfg, frames)
    assert len(expected) > 1                            # the budget drove the cut
    assert all(e - s < 20 for s, e in expected)         # cap never reached
    batch = [envelope(r) for r in frames]
    engine = MapEngine({f"f{s}": window_obj(*["continues"] * (e - s))
                        for s, e in expected})
    out, ctx = run_stage(cfg, batch, engine)
    assert boundary_windows(ctx) == expected
    assert ctx.metrics.counters["segment.windows"] == len(expected)
    # all frames absorbed into one episode (all continues)
    assert status_tally(batch[:10]) == {"absorbed": 10}
    (episode,) = batch[10:]
    assert len(episode.record.members) == 10


def test_budget_on_same_input_rerun_yields_byte_identical_spans():
    cfg = make_cfg(min_len=1,
                   llm_profiles={"default": llm_profile(context_window=2048)})

    def one_run():
        frames = [ui_frame(f"f{i}", i) for i in range(10)]
        expected = packed_spans(cfg, frames)
        engine = MapEngine({f"f{s}": window_obj(*["continues"] * (e - s))
                            for s, e in expected})
        out, ctx = run_stage(cfg, [envelope(r) for r in frames], engine)
        return boundary_windows(ctx), [
            payload["member_ids"] for ev, _, _, payload in ctx.metrics.events
            if ev == "segment.boundary"]

    first_spans, first_members = one_run()
    second_spans, second_members = one_run()
    assert len(first_spans) > 1
    assert first_spans == second_spans                  # deterministic rerun
    assert first_members == second_members


def test_budget_on_vision_image_cost_prices_frames_and_reads_calibrator_once():
    # vision_resolved adds the calibrator's per-image cost to every frame:
    # the same 4-frame session that fits ONE text-only window splits under
    # image pricing; the calibrator snapshot is read once per session.
    profiles = {"default": llm_profile(context_window=2900)}
    text_cfg = make_cfg(min_len=1, llm_profiles=profiles)
    frames = [ui_frame(f"f{i}", i) for i in range(4)]
    assert packed_spans(text_cfg, frames) == [(0, 4)]   # text-only: one window

    vision_cfg = make_cfg(min_len=1, vision_resolved=True, llm_profiles=profiles)
    expected = packed_spans(vision_cfg, frames, image_cost=500)
    assert len(expected) == 3                           # image cost forces splits
    calibrator = StubCalibrator(500)
    engine = MapEngine({f"f{s}": window_obj(*["continues"] * (e - s))
                        for s, e in expected})
    batch = [envelope(r) for r in frames]
    out, ctx = run_stage(vision_cfg, batch, engine,
                         llm=SimpleNamespace(calibrator=calibrator))
    assert calibrator.calls == ["default"]              # ONE read per session
    assert boundary_windows(ctx) == expected
    # vision prompts carry one image part per frame
    parts = engine.calls[0][1].messages[1].parts
    assert [p.kind for p in parts][:2] == ["image", "text"]


def test_budget_off_profile_with_zero_window_keeps_fixed_cut():
    # context_window == 0 on the referenced profile = budget off: the fixed
    # v1.10 cut, even though the profile table is populated.
    cfg = make_cfg(window=2, min_len=1,
                   llm_profiles={"default": llm_profile(context_window=0)})
    frames = [ui_frame("f0", 0), ui_frame("f1", 1), ui_frame("f2", 2)]
    batch = [envelope(r) for r in frames]
    engine = MapEngine({"f0": window_obj("continues", "continues"),
                        "f1": window_obj("continues", "continues")})
    out, ctx = run_stage(cfg, batch, engine)
    assert boundary_windows(ctx) == [(0, 2), (1, 3)]    # stride = window − 1
    assert ctx.metrics.counters["segment.windows"] == 2


# ── calibrated-above-prior forced-min-2 packing (finding-1 repro) ─────────────
# The reviewer's scenario: an M1-passing config (w_min == 2 under PRIOR image
# pricing) meets a calibrator whose post-min-samples readout legally exceeds
# prior × 1.2 (V19, no clamp) — per-frame cost then exceeds the pack budget and
# the pre-fix packer closed windows at 1 frame (AssertionError at runtime;
# under python -O a non-advancing infinite loop). Now: forced 2-frame windows,
# and a true overflow surfaces record-level via the M9 precheck terminal
# through the per-window failure path — never an exception escaping run().

def calibrated_above_prior_setup():
    prof = llm_profile(context_window=7168, max_output_tokens=1024)
    cfg = make_cfg(min_len=1, vision_resolved=True,
                   llm_profiles={"default": prof})
    # The M1 guard passes this shape: w_min == 2 == floor (WARN leg, not error).
    assert budget_mod.min_window(cfg) == 2
    # Feed the REAL calibrator directly: 8 samples of residue 2550 → frozen
    # readout ceil(2550 / 0.85) = 3000, above the prior working point
    # ceil(1445 × 1.2) = 1734 — legal, no clamp by design.
    calibrator = budget_mod.ImageCostCalibrator(
        {"default": ("openai_compatible", prof.max_image_px)})
    for _ in range(budget_mod.CALIBRATION_MIN_SAMPLES):
        calibrator.observe("default", prompt_tokens=2550, text_est=0, n_images=1)
    calibrator.freeze_batch()
    assert calibrator.cost("default") == 3000
    assert calibrator.cost("default") > math.ceil(
        budget_mod.est_image_prior(prof, prof.max_image_px)
        * budget_mod.PRIOR_INFLATION)
    frames = [ui_frame(f"f{i}", i) for i in range(4)]
    # Per-frame calibrated cost exceeds what the budget can pair: the packer
    # is forced into the min-2 chain (previously: 1-frame close → assert).
    assert packed_spans(cfg, frames, image_cost=3000) == [(0, 2), (1, 3), (2, 4)]
    return cfg, calibrator, frames


def test_calibrated_above_prior_forces_two_frame_windows_and_completes():
    cfg, calibrator, frames = calibrated_above_prior_setup()
    engine = MapEngine({"f0": window_obj("continues", "continues"),
                        "f1": window_obj("continues", "continues"),
                        "f2": window_obj("continues", "continues")})
    batch = [envelope(r) for r in frames]
    out, ctx = run_stage(cfg, batch, engine,
                         llm=SimpleNamespace(calibrator=calibrator))
    assert boundary_windows(ctx) == [(0, 2), (1, 3), (2, 4)]
    assert all(e - s == 2 for s, e in boundary_windows(ctx))
    assert status_tally(batch[:4]) == {"absorbed": 4}   # normal assembly
    assert ctx.metrics.provider_results == []           # nothing fed the breaker


def test_calibrated_above_prior_true_overflow_fails_record_level_never_raises():
    # The forced 2-frame window whose true est still exceeds the budget: the
    # M9 precheck terminal (simulated at the engine seam — the stub raises
    # what complete() would) must land in the session's on_error disposition,
    # record-level, with the overflow_records reject counter (finding 3) and
    # ZERO breaker feeds (precheck never feeds, §7.8 matrix).
    cfg, calibrator, frames = calibrated_above_prior_setup()
    cfg = dataclasses.replace(
        cfg, segment=dataclasses.replace(cfg.segment, on_error="fail"))
    engine = QueueEngine([ContextOverflowError(
        "est 6483 + max_output 1024 + margin 717 > context_window 7168",
        phase="precheck", profile="default") for _ in range(3)])
    batch = [envelope(r) for r in frames]
    out, ctx = run_stage(cfg, batch, engine,
                         llm=SimpleNamespace(calibrator=calibrator))
    assert out is batch                                 # no exception escaped
    assert status_tally(batch) == {"failed": 4}
    assert all(item.errors[0].kind == "context_overflow" for item in batch)
    # V13② reject-site convention (finding 3): one count per rejected member.
    assert ctx.metrics.counters["budget.overflow_records"] == 4
    assert ctx.metrics.counters["segment.failures"] == 1
    assert ctx.metrics.provider_results == []           # precheck never feeds


def test_calibrated_above_prior_keep_disposition_degrades_whole_session():
    # on_error="keep" (default): the same overflow terminal degrades the
    # session to ONE whole episode with the S26 evidence triple — kind
    # context_overflow — and no reject, so overflow_records stays untouched.
    cfg, calibrator, frames = calibrated_above_prior_setup()
    engine = QueueEngine([ContextOverflowError(
        "over budget", phase="precheck", profile="default")
        for _ in range(3)])
    batch = [envelope(r) for r in frames]
    out, ctx = run_stage(cfg, batch, engine,
                         llm=SimpleNamespace(calibrator=calibrator))
    assert status_tally(batch[:4]) == {"absorbed": 4}
    (episode,) = batch[4:]
    assert episode.segment_degraded["kind"] == "context_overflow"
    assert "budget.overflow_records" not in ctx.metrics.counters
    assert ctx.metrics.provider_results == []


# ── V9 session-level digest precompute ───────────────────────────────────────

def test_digests_computed_once_per_frame_per_session(monkeypatch):
    # v1.10 digested per window inclusion (the seam frame twice); v1.11
    # precomputes the session vector once — exactly one frame_digest call per
    # frame. (digest_is_poor's internal call lives in types.py and is not
    # routed through the segment module's name — the poverty path stays
    # independent by design.)
    calls = []

    def counting(record, max_chars):
        calls.append((record.id, max_chars))
        return frame_digest(record, max_chars)

    monkeypatch.setattr("labelkit.operators.segment.frame_digest", counting)
    cfg = make_cfg(window=2, min_len=1)                 # 3 frames → 2 windows (seam)
    frames = [ui_frame("f0", 0), ui_frame("f1", 1), ui_frame("f2", 2)]
    batch = [envelope(r) for r in frames]
    engine = MapEngine({"f0": window_obj("continues", "continues"),
                        "f1": window_obj("continues", "continues")})
    out, ctx = run_stage(cfg, batch, engine)
    assert sorted(calls) == [("f0", 400), ("f1", 400), ("f2", 400)]
    # the seam frame's digest still reached both windows' prompts
    first_parts = engine.calls[0][1].messages[1].parts
    second_parts = engine.calls[1][1].messages[1].parts
    seam_digest = frame_digest(frames[1], 400)
    assert first_parts[1].text.startswith(f"[帧 1] {seam_digest}")
    assert second_parts[0].text.startswith(f"[帧 0] {seam_digest}")


# ── ②b contract ──────────────────────────────────────────────────────────────

def test_contract_2b_no_removals_tail_append_same_list_and_idempotent():
    cfg = make_cfg()
    dup = PipelineItem(record=ui_frame("d0", 9), status="dropped_dup",
                       session_id="s0")
    frames = [ui_frame("f0", 0), ui_frame("f1", 1)]
    session_items = [envelope(r, sid="s1") for r in frames]
    unstamped = PipelineItem(record=ui_frame("u0", 2))
    batch = [dup, *session_items, unstamped]
    originals = list(batch)
    engine = MapEngine({"f0": window_obj("continues", "continues")})
    out, _ = run_stage(cfg, batch, engine)
    assert out is batch                                 # the SAME list object
    assert batch[:4] == originals                       # no removal/reorder/replace
    appended = batch[4:]
    assert len(appended) == 1
    assert appended[0].status == "active"               # 追加物 active
    assert appended[0].record.kind == "sequence"
    assert appended[0].session_id == "s1"
    # conservation: frames = absorbed + remaining active + untouched non-active
    assert status_tally(batch) == {"dropped_dup": 1, "absorbed": 2, "active": 2}
    # re-entry is naturally idempotent: sequence envelopes never re-enter the
    # processing face, absorbed members are no longer active
    out2, ctx2 = run_stage(cfg, batch, ExplodingEngine())
    assert out2 is batch and len(batch) == 5
    assert ctx2.metrics.events == [] and ctx2.metrics.counters == {}


# ── digest-poverty guard (S12) ───────────────────────────────────────────────

class _RecordingLogger:
    def __init__(self):
        self.warnings: list = []

    def warning(self, msg, *args, **kwargs):
        self.warnings.append(msg)


def test_digest_poor_frames_counted_once_per_frame_and_warned_once_per_run(
        monkeypatch):
    logger = _RecordingLogger()
    monkeypatch.setattr("labelkit.operators.segment._logger", logger)
    cfg = make_cfg(window=2, min_len=1)                 # 3 frames → 2 windows (seam)
    stage = SegmentStage(cfg)
    frames = [bare_frame("f0", 0), bare_frame("f1", 1), bare_frame("f2", 2)]
    batch = [envelope(r) for r in frames]
    engine = MapEngine({"f0": window_obj("continues", "continues"),
                        "f1": window_obj("continues", "continues")})
    out, ctx = run_stage(cfg, batch, engine, stage=stage)
    # per frame, not per window appearance (the seam frame counts once)
    assert ctx.metrics.counters["segment.digest_poor_frames"] == 3
    assert len(logger.warnings) == 1
    # V4 wording: profile capability, never the removed use_vision key
    assert "为 segment.llm 配置 supports_vision=true 的 profile" in logger.warnings[0]
    assert "use_vision" not in logger.warnings[0]
    # second batch through the SAME stage instance: counted again, no new WARN
    batch2 = [envelope(bare_frame("g0", 0), sid="s2"),
              envelope(bare_frame("g1", 1), sid="s2")]
    out2, ctx2 = run_stage(cfg, batch2,
                           MapEngine({"g0": window_obj("continues",
                                                       "continues")}),
                           stage=stage)
    assert ctx2.metrics.counters["segment.digest_poor_frames"] == 2
    assert len(logger.warnings) == 1                    # once per run


def test_healthy_frames_do_not_count_as_poor():
    cfg = make_cfg()
    batch = [envelope(ui_frame("f0", 0)), envelope(ui_frame("f1", 1))]
    engine = MapEngine({"f0": window_obj("continues", "continues")})
    out, ctx = run_stage(cfg, batch, engine)
    assert "segment.digest_poor_frames" not in ctx.metrics.counters


def test_rules_sessions_skip_poverty_guard():
    cfg = make_cfg(strategy="rules")
    batch = [envelope(bare_frame("f0", 0)), envelope(bare_frame("f1", 1))]
    out, ctx = run_stage(cfg, batch, ExplodingEngine())
    assert "segment.digest_poor_frames" not in ctx.metrics.counters


# ── multi-session batches ────────────────────────────────────────────────────

def test_sessions_processed_in_batch_position_order():
    cfg = make_cfg()
    a = [envelope(ui_frame("a0", 0), sid="sa"), envelope(ui_frame("a1", 1), sid="sa")]
    b = [envelope(ui_frame("b0", 2), sid="sb"), envelope(ui_frame("b1", 3), sid="sb")]
    batch = [*a, *b]
    engine = MapEngine({"a0": window_obj("continues", "context_switch"),
                        "b0": window_obj("continues", "continues")})
    out, ctx = run_stage(cfg, batch, engine)
    # session sa splits into [a0] + [a1] — both below min_len=2 → dropped;
    # session sb survives whole. Episode order = session order.
    assert status_tally(batch[:4]) == {"dropped_noise": 2, "absorbed": 2}
    assert ctx.metrics.counters["segment.below_min_len"] == 2
    (episode,) = batch[4:]
    assert episode.session_id == "sb"
    assert [r.id for r in episode.record.members] == ["b0", "b1"]
    # conservation across the whole batch
    assert status_tally(batch) == {"dropped_noise": 2, "absorbed": 2, "active": 1}

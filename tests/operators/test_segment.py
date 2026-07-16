"""Offline unit tests for M14 segment: prompt assembly (spec 3.14.4 / CONTRACTS §10.9),
the judge_window post-validation (first-wins, absent → "continues"), sliding-window
stitching (seam frame belongs to the later window), the deterministic segment
assembly (noise / boundary / min_len / episode), strategy routing (rules and
lone-frame sessions cost zero LLM), the on_error two-path policy (S26), the ②b
contract, and the digest-poverty guard (S12). Pure logic only — no LLM: the schema
engine is replaced by the in-process complete_validated stubs (test_classify 惯例)."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

from labelkit.operators.segment import (
    SegmentStage,
    _reason_requested,
    _window_spans,
    build_segment_prompt,
    judge_window,
    render_tree_diff,
)
from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    DedupConfig,
    ExtractConfig,
    GenerateConfig,
    InputConfig,
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
from labelkit.common.errors import SchemaViolation
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
    tree_diff,
)


def make_cfg(*, strategy="hybrid", window=20, digest_max_chars=400,
             noise_filter=True, min_len=2, use_vision=False, context="",
             on_error="keep", trace=None) -> ResolvedConfig:
    return ResolvedConfig(
        tool=ToolConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality="ui", input="in"),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(enabled=True, strategy=strategy, llm="default",
                              window=window, digest_max_chars=digest_max_chars,
                              noise_filter=noise_filter, min_len=min_len,
                              use_vision=use_vision, context=context,
                              on_error=on_error),
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


class ExplodingEngine:
    async def complete_validated(self, *a, **k):
        raise AssertionError("complete_validated must not be called")


class RecordingMetrics:
    def __init__(self):
        self.events: list = []             # (ev, stage, record_ids, payload)
        self.counters: dict[str, int] = {}

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None):
        self.events.append((ev, stage, tuple(record_ids), dict(payload or {})))

    def count(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n


def make_ctx(cfg, engine):
    return SimpleNamespace(cfg=cfg, llm=None, schema_engine=engine,
                           metrics=RecordingMetrics(), rng=None, batch_no=1)


def run_stage(cfg, batch, engine, stage=None):
    ctx = make_ctx(cfg, engine)
    out = asyncio.run((stage or SegmentStage(cfg)).run(batch, ctx))
    return out, ctx


def status_tally(items):
    tally: dict[str, int] = {}
    for item in items:
        tally[item.status] = tally.get(item.status, 0) + 1
    return tally


# ── prompt assembly (§10.9, deterministic) ──────────────────────────────────

def test_system_message_verbatim_no_reason_no_context():
    frames = [ui_frame("f0", 3), ui_frame("f1", 4, texts=("搜索美食页面",)),
              ui_frame("f2", 5, texts=("麻辣烫搜索结果",))]
    bundle = build_segment_prompt(frames, [None, None, None], make_cfg(),
                                  with_reason=False)
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
    text = build_segment_prompt(frames, [None, None], cfg,
                                with_reason=True).messages[0].parts[0].text
    # optional domain-context line sits between the anchors and the structure
    # sentence; reason fragment appears in the structure line when requested
    assert text.endswith(
        "忽略状态栏、后台通知等背景变化。\n"
        "这是手机屏幕操作流\n"
        "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
        '{"frames": [{"index": <窗内帧序号>, "relation": <词表值>, '
        '"reason": <一句话理由>}, ...]}（恰 2 项）')
    # default: no context line
    plain = build_segment_prompt(frames, [None, None], make_cfg(),
                                 with_reason=False).messages[0].parts[0].text
    assert "这是手机屏幕操作流" not in plain
    assert ("忽略状态栏、后台通知等背景变化。\n"
            "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n") in plain


def test_user_parts_frame_labels_and_diff_lines():
    frames = [ui_frame("f0", 3), ui_frame("f1", 4, texts=("搜索美食页面",))]
    diff = {"added": 2, "removed": 1, "text_changed": 3, "change_ratio": 0.25,
            "app_changed": True, "title_changed": False}
    bundle = build_segment_prompt(frames, [None, diff], make_cfg(),
                                  with_reason=False)
    parts = bundle.messages[1].parts
    assert [p.kind for p in parts] == ["text", "text"]
    assert parts[0].text == f"[帧 0] {frame_digest(frames[0], 400)}"
    assert parts[1].text == (
        f"[帧 1] {frame_digest(frames[1], 400)}\n"
        "[帧 1 变更] 新增 2 节点，移除 1 节点，文本变化 3 处，变更比例 25%，应用切换")


def test_render_tree_diff_fixed_format_and_flags():
    base = {"added": 0, "removed": 0, "text_changed": 0, "change_ratio": 0.0,
            "app_changed": False, "title_changed": False}
    assert render_tree_diff(base) == "新增 0 节点，移除 0 节点，文本变化 0 处，变更比例 0%"
    assert render_tree_diff({**base, "added": 2, "removed": 1, "text_changed": 3,
                             "change_ratio": 2 / 3, "app_changed": True,
                             "title_changed": True}) == (
        "新增 2 节点，移除 1 节点，文本变化 3 处，变更比例 67%，应用切换，标题变化")


def test_use_vision_two_state_parts_shape():
    frames = [ui_frame("f0", 3), ui_frame("f1", 4)]
    plain = build_segment_prompt(frames, [None, None], make_cfg(),
                                 with_reason=False)
    assert [p.kind for p in plain.messages[1].parts] == ["text", "text"]
    vision = build_segment_prompt(frames, [None, None],
                                  make_cfg(use_vision=True), with_reason=False)
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


# ── sliding-window spans + stitching ─────────────────────────────────────────

def test_window_spans_stride_is_window_minus_one():
    assert _window_spans(5, 20) == [(0, 5)]
    assert _window_spans(3, 2) == [(0, 2), (1, 3)]
    assert _window_spans(21, 20) == [(0, 20), (19, 21)]
    assert _window_spans(39, 20) == [(0, 20), (19, 39)]


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


# ── on_error two paths (S26) ─────────────────────────────────────────────────

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
    assert ctx.metrics.counters == {"segment.failures": 1}
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
    assert ctx.metrics.counters == {"segment.failures": 1}
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
    assert ctx.metrics.counters == {"segment.failures": 1}


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
    assert ctx.metrics.counters == {"segment.failures": 1}  # one per session


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
    assert "segment.use_vision" in logger.warnings[0]
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

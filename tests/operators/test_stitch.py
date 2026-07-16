"""Offline unit tests for M16 stitch (spec 3.16 / SPEC-activity-structure.md §3.6):
summary-card determinism, the three mechanical-prior legs (T9), monotonic-pool
presentation order, pool-full eviction priority (stale-gap leg + LRU fallback,
M-3), the bounded second pass (T19 — V2 full-miss repair walk + live view), the
②c state machine (incl. rescue-never-opens and fail-only-episode-envelope,
B-2), the seam criterion & coordinates (T20/M-1/m-8), batch-level conservation,
the rescue flip audit trail (T11/m-10), pool-empty semantics (M-6/B-2), the
votes strict-majority aggregation (T18/M-4), and the stitch-off byte-equivalence
anchor (m-11). Pure logic only — no LLM: the schema engine is replaced by the
in-process complete_validated stubs (test_segment 惯例)."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from labelkit.operators.stitch import (
    StitchStage,
    aggregate_votes,
    app_set,
    build_stitch_prompt,
    entity_set,
    page_identity,
    prior_hits,
    render_candidate_card,
    render_thread_card,
    select_eviction,
    span_distance,
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
from labelkit.common.runtime.schema_engine import stitch_schema
from labelkit.common.contracts.types import (
    ImageRef,
    PipelineItem,
    Record,
    RecordRef,
    Transition,
    UINode,
    UITree,
    Usage,
)


def make_cfg(*, enabled=True, llm="default", max_open=4, bias="conservative",
             rescue_short=True, repass=True, stale_gap_steps=0,
             digest_max_chars=400, context="", votes=1, on_error="keep",
             output="out.jsonl") -> ResolvedConfig:
    return ResolvedConfig(
        tool=ToolConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output=output, modality="ui", input="in"),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(enabled=True),
        stitch=StitchConfig(enabled=enabled, llm=llm, max_open=max_open,
                            bias=bias, rescue_short=rescue_short, repass=repass,
                            stale_gap_steps=stale_gap_steps,
                            digest_max_chars=digest_max_chars, context=context,
                            votes=votes, on_error=on_error),
        extract=ExtractConfig(),
        classify=ClassifyConfig(),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(instruction="标注"),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=TraceConfig(),
        rubric=Rubric(name="default:trajectory", criteria=()),
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


def ui_frame(rid: str, pair_index: int, *, app="com.food", activity=None,
             texts=("外卖首页推荐列表",)) -> Record:
    extra_root = {"package": app} if app else {}
    if activity:
        extra_root["activity"] = activity
    nodes = [UINode("1", None, 0, "FrameLayout", "", "", (0, 0, 1080, 1920),
                    True, extra_root)]
    for j, text in enumerate(texts):
        nodes.append(UINode(str(j + 2), "1", 1, "TextView", text, "",
                            (0, j * 100, 1080, (j + 1) * 100), True, {}))
    image = ImageRef(path=Path(f"image_{pair_index}.png"), format="png",
                     size_bytes=1)
    return Record(id=rid, modality="ui", text=None, raw=None,
                  ui_tree=UITree(tuple(nodes)), image=image,
                  ref=RecordRef("a/uitree.jsonl", None, pair_index, ()))


def envelope(record: Record, sid="s1", status="active") -> PipelineItem:
    return PipelineItem(record=record, status=status, session_id=sid)


def episode_of(frame_items: list[PipelineItem], sid="s1") -> PipelineItem:
    """Segment-shaped episode envelope: members = the frames' records (S24 id
    rule), member envelopes absorbed — the M14 _emit_episode mirror."""
    records = tuple(it.record for it in frame_items)
    joined = "\n".join(r.id for r in records)
    first = records[0]
    rec = Record(id=hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16],
                 modality="ui", text=None, raw=None, ui_tree=None, image=None,
                 ref=RecordRef(first.ref.source_file, None,
                               first.ref.pair_index, ()),
                 kind="sequence", members=records)
    for it in frame_items:
        it.status = "absorbed"
    return PipelineItem(record=rec, session_id=sid)


def short_run(frame_items: list[PipelineItem]) -> None:
    """Mark frames as a segment below_min_len drop (the T11 rescue carrier)."""
    for it in frame_items:
        it.status = "dropped_noise"
        it.noise_attribution = ("segment", "below_min_len")


def obj(verdict="new", ref=None, task="某任务", reason="理由",
        confidence="medium") -> dict:
    return {"verdict": verdict, "thread_ref": ref, "task_name": task,
            "reason": reason, "confidence": confidence}


# ── in-process complete_validated stubs (no LLM, test_segment 惯例) ─────────

class QueueEngine:
    """Pops queued outcomes in call order — stitch judgments are strictly
    sequential per session, so queue order = candidate session order."""

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


def run_stage(cfg, batch, engine):
    ctx = make_ctx(cfg, engine)
    out = asyncio.run(StitchStage(cfg).run(batch, ctx))
    return out, ctx


def pool_card_count(prompt) -> int:
    """Thread cards in a judgment prompt = user parts − 1 (candidate card),
    except the empty-pool shape where the single non-candidate part is the
    fixed 零卡 line."""
    parts = prompt.messages[1].parts
    head = [p.text for p in parts[:-1]]
    if head == ["（当前无开放线索）"]:
        return 0
    return len(head)


# ── scenario builders ────────────────────────────────────────────────────────

def v2_batch(sid="s1"):
    """The V2 acceptance shape: A1 (frames 0-1, com.food) → B (2-3, com.taxi)
    → A2 (4-5, com.food). Returns (batch, a1, b, a2) episode envelopes."""
    fa = [envelope(ui_frame(f"a{i}", i, app="com.food",
                            texts=(f"外卖步骤{i}", "川味麻辣烫")), sid)
          for i in range(2)]
    fb = [envelope(ui_frame(f"b{i}", 2 + i, app="com.taxi",
                            texts=(f"打车步骤{i}",)), sid) for i in range(2)]
    fa2 = [envelope(ui_frame(f"c{i}", 4 + i, app="com.food",
                             texts=(f"外卖收尾{i}", "川味麻辣烫")), sid)
           for i in range(2)]
    frames = fa + fb + fa2
    a1 = episode_of(fa, sid)
    b = episode_of(fb, sid)
    a2 = episode_of(fa2, sid)
    return [*frames, a1, b, a2], a1, b, a2


# ── summary cards + prompt (§10.11, deterministic) ──────────────────────────

def test_thread_card_deterministic_and_structured():
    cfg = make_cfg()
    members = [ui_frame("a0", 0, texts=("外卖首页", "川味麻辣烫")),
               ui_frame("a1", 1, texts=("下单页",))]
    head = ui_frame("c0", 4, texts=("外卖收尾", "川味麻辣烫"))
    card1 = render_thread_card(1, "点外卖", members, (0, 1), 2, head, cfg)
    card2 = render_thread_card(1, "点外卖", members, (0, 1), 2, head, cfg)
    assert card1 == card2                              # determinism
    lines = card1.splitlines()
    assert lines[0] == "[线索 1] 任务名: 点外卖"
    assert lines[1] == "App 集合: com.food"
    assert lines[2] == "序号跨度: [0, 1]｜帧数 2｜碎片数 2"
    assert lines[3].startswith("首帧摘要: [com.food] 外卖首页")
    assert lines[4].startswith("尾帧摘要: [com.food] 下单页")
    # the E5 resumption pair rides the card with tree_diff change evidence
    assert lines[5].startswith("接续对（线索尾帧 → 候选首帧）变更: 新增 ")
    # unnamed threads (failed-judgment bootstrap) render the placeholder
    unnamed = render_thread_card(2, "", members, (0, 1), 1, None, cfg)
    assert unnamed.splitlines()[0] == "[线索 2] 任务名: （未命名）"
    assert "接续对" not in unnamed                     # no candidate head → no pair


def test_candidate_card_kinds_and_digest_cap():
    cfg = make_cfg(digest_max_chars=12)
    members = [ui_frame("a0", 0, texts=("一二三四五六七八九十甲乙丙丁",))]
    card = render_candidate_card("episode", members, (0, 0), cfg)
    lines = card.splitlines()
    assert lines[0] == "[候选碎片] 类型: 分段产出"
    assert lines[1] == "App 集合: com.food"
    assert lines[2] == "序号跨度: [0, 0]｜帧数 1"
    digest = lines[3].removeprefix("首帧摘要: ")
    assert len(digest) == 12 and digest.endswith("…")  # m-9 truncation
    rescue = render_candidate_card("rescue", members, (3, 3), cfg)
    assert rescue.splitlines()[0] == "[候选碎片] 类型: 短段救援"


def test_prompt_shape_pool_count_context_and_empty_pool():
    cfg = make_cfg(context="手机自然使用流；同一任务可被切走后恢复")
    members = [ui_frame("a0", 0)]
    card = render_thread_card(1, "点外卖", members, (0, 0), 1, members[0], cfg)
    cand = render_candidate_card("episode", members, (4, 4), cfg)
    bundle = build_stitch_prompt([card], cand, cfg)
    assert [m.role for m in bundle.messages] == ["system", "user"]
    system = bundle.messages[0].parts[0].text
    assert system.startswith("你是屏幕操作流的线索缝合审核员。下面给出当前会话中 1 条开放线索的摘要卡")
    assert "保守偏置：仅在证据明确指向同一任务时判 resume" in system
    assert "错缝的代价高于漏缝" in system
    assert "若当前无开放线索，恒判 new。" in system
    assert "手机自然使用流；同一任务可被切走后恢复\n输出必须是符合以下结构的单个 JSON 对象" in system
    assert system.endswith(
        '{"verdict": "resume"|"new", "thread_ref": <线索编号|null>,\n'
        ' "task_name": <一句话任务名>, "reason": <一句话理由>,\n'
        ' "confidence": "high"|"medium"|"low"}')
    parts = bundle.messages[1].parts
    assert [p.kind for p in parts] == ["text", "text"]  # pure text, no images
    assert parts[0].text == card and parts[1].text == cand
    assert bundle.temperature is None                   # profile default

    empty = build_stitch_prompt([], cand, cfg)
    empty_parts = empty.messages[1].parts
    assert [p.text for p in empty_parts] == ["（当前无开放线索）", cand]
    assert "0 条开放线索" in empty.messages[0].parts[0].text
    # context omitted entirely when empty
    plain = build_stitch_prompt([], cand, make_cfg()).messages[0].parts[0].text
    assert "手机自然使用流" not in plain


# ── prior whitelist: three legs (T9) ─────────────────────────────────────────

def test_prior_leg_app_overlap_only():
    thread = [ui_frame("t0", 0, app="com.food", texts=("外卖首页",))]
    cand = [ui_frame("c0", 4, app="com.food", texts=("完全无关文本",))]
    assert prior_hits(thread, [thread[-1]], cand) == ["app_overlap"]
    other = [ui_frame("c1", 5, app="com.taxi", texts=("完全无关文本",))]
    assert prior_hits(thread, [thread[-1]], other) == []


def test_prior_leg_entity_overlap_tail_x_head_pair():
    # E5: thread TAIL frame × candidate HEAD frame — shared entity, apps differ
    thread = [ui_frame("t0", 0, app="com.food", texts=("外卖首页",)),
              ui_frame("t1", 1, app="com.food", texts=("订单 A8823",))]
    cand = [ui_frame("c0", 4, app="com.pay", texts=("订单 A8823",)),
            ui_frame("c1", 5, app="com.pay", texts=("支付完成",))]
    assert prior_hits(thread, [thread[-1]], cand) == ["entity_overlap"]
    # the pair is tail × head — an entity on a NON-tail thread frame misses
    reordered = [thread[1], thread[0]]
    assert prior_hits(reordered, [reordered[-1]], cand) == []


def test_prior_leg_same_page_requires_activity_and_matches_fragment_tail():
    tail = ui_frame("t1", 1, app="com.food", activity=".OrderActivity",
                    texts=("确认订单",))
    thread = [ui_frame("t0", 0, app="com.food", texts=("外卖首页",)), tail]
    cand = [ui_frame("c0", 4, app="com.food", activity=".OrderActivity",
                     texts=("确认订单",))]
    hits = prior_hits(thread, [tail], cand)
    assert "same_page" in hits                         # page id == some fragment tail
    # page identity = app + activity (+ title): activity absent → leg dead (T9)
    plain_cand = [ui_frame("c1", 5, app="com.food", texts=("确认订单",))]
    assert "same_page" not in prior_hits(thread, [tail], plain_cand)
    assert page_identity(plain_cand[0]) is None
    assert page_identity(cand[0]) == ("com.food", ".OrderActivity", "确认订单")


def test_app_and_entity_sets_extraction():
    frames = [ui_frame("t0", 0, app="com.food", texts=("外卖首页",)),
              ui_frame("t1", 1, app="com.taxi", texts=("打车",))]
    assert app_set(frames) == frozenset({"com.food", "com.taxi"})
    assert entity_set(frames[0]) == frozenset({"外卖首页"})
    text_record = Record(id="t" * 16, modality="text", text="纯文本", raw={},
                         ui_tree=None, image=None,
                         ref=RecordRef("f.jsonl", 1, None, ()))
    assert app_set([text_record]) == frozenset()
    assert entity_set(text_record) == frozenset()


def test_conservative_bias_requires_resume_and_prior_conjunction():
    """LLM resume WITHOUT a prior hit must NOT merge under bias=conservative
    (opens a thread instead); bias=llm merges on the bare verdict."""
    sid = "s1"
    fa = [envelope(ui_frame("a0", 0, app="com.food"), sid)]
    fb = [envelope(ui_frame("b0", 1, app="com.taxi", texts=("打车",)), sid)]
    for bias, expect_merge in (("conservative", False), ("llm", True)):
        frames_a = [envelope(ui_frame("a0", 0, app="com.food"), sid)]
        frames_b = [envelope(ui_frame("b0", 1, app="com.taxi",
                                      texts=("打车",)), sid)]
        ep_a, ep_b = episode_of(frames_a, sid), episode_of(frames_b, sid)
        batch = [*frames_a, *frames_b, ep_a, ep_b]
        engine = QueueEngine([obj("new", task="点外卖"),
                              obj("resume", ref=1, task="点外卖")])
        cfg = make_cfg(bias=bias, repass=False)
        run_stage(cfg, batch, engine)
        assert (ep_b.status == "stitched") is expect_merge, bias
        if expect_merge:
            assert [m.id for m in ep_a.record.members] == ["a0", "b0"]


def test_stale_gap_downgrade_requires_two_prior_legs():
    """E7 time decay: beyond stale_gap_steps a single prior leg no longer
    clears the conjunction — two legs do."""
    sid = "s1"

    def build(texts_c):
        frames_a = [envelope(ui_frame("a0", 0, app="com.food",
                                      texts=("外卖首页",)), sid)]
        gap = [envelope(ui_frame(f"n{i}", 1 + i, app="com.other",
                                 texts=("噪声",)), sid) for i in range(4)]
        for g in gap:
            g.status = "dropped_noise"
            g.noise_attribution = ("segment", "noise")
        frames_c = [envelope(ui_frame("c0", 5, app="com.food", texts=texts_c),
                             sid)]
        ep_a, ep_c = episode_of(frames_a, sid), episode_of(frames_c, sid)
        return [*frames_a, *gap, *frames_c, ep_a, ep_c], ep_a, ep_c

    cfg = make_cfg(stale_gap_steps=2, repass=False, rescue_short=False)
    # one leg (app only): gap 5 > 2 → downgraded prior misses → no merge
    batch, ep_a, ep_c = build(("无关文本",))
    engine = QueueEngine([obj("new", task="点外卖"),
                          obj("resume", ref=1, task="点外卖")])
    run_stage(cfg, batch, engine)
    assert ep_c.status == "active" and ep_c.thread_id == ep_c.record.id
    # two legs (app + entity): the downgraded prior clears
    batch2, ep_a2, ep_c2 = build(("外卖首页",))
    engine2 = QueueEngine([obj("new", task="点外卖"),
                           obj("resume", ref=1, task="点外卖")])
    run_stage(cfg, batch2, engine2)
    assert ep_c2.status == "stitched"
    assert [m.id for m in ep_a2.record.members] == ["a0", "c0"]


def test_invalid_thread_ref_resolves_conservatively_to_new():
    sid = "s1"
    fa = [envelope(ui_frame("a0", 0), sid)]
    fb = [envelope(ui_frame("b0", 1), sid)]
    ep_a, ep_b = episode_of(fa, sid), episode_of(fb, sid)
    batch = [*fa, *fb, ep_a, ep_b]
    # resume with out-of-range / null refs — both stay their own threads
    engine = QueueEngine([obj("new"), obj("resume", ref=99)])
    run_stage(make_cfg(repass=False), batch, engine)
    assert ep_b.status == "active" and ep_b.thread_id == ep_b.record.id


# ── monotonic pool: presentation order + eviction (T8/M-3) ───────────────────

def test_pool_cards_presented_most_recently_active_first():
    sid = "s1"
    groups = []
    for i, app in enumerate(("com.a", "com.b", "com.c")):
        frames = [envelope(ui_frame(f"{chr(97 + i)}0", i, app=app), sid)]
        groups.append((frames, episode_of(frames, sid)))
    batch = [f for frames, _ in groups for f in frames] + [ep for _, ep in groups]
    engine = QueueEngine([obj("new", task="任务A"), obj("new", task="任务B"),
                          obj("new", task="任务C")])
    run_stage(make_cfg(repass=False), batch, engine)
    # third judgment saw two open threads, most-recently-active first: B then A
    third_prompt = engine.calls[2][1]
    cards = [p.text for p in third_prompt.messages[1].parts[:-1]]
    assert cards[0].startswith("[线索 1] 任务名: 任务B")
    assert cards[1].startswith("[线索 2] 任务名: 任务A")
    assert engine.calls[2][2] == stitch_schema()       # M8 internal schema
    # record_ids = the candidate fragment's first member id (T16)
    assert engine.calls[2][3] == ("c0",)


def test_pool_full_eviction_stale_gap_leg_beats_lru():
    """T1 (LRU) has a NEAR tail; T2 (recent) is stale beyond stale_gap_steps —
    the stale leg evicts T2, overriding plain LRU."""
    t1 = SimpleNamespace(tail_pos=9, last_active=0)
    t2 = SimpleNamespace(tail_pos=2, last_active=1)
    assert select_eviction([t1, t2], candidate_pos=10, stale_gap_steps=3) is t2
    # both stale → LRU among the stale ones
    t3 = SimpleNamespace(tail_pos=1, last_active=2)
    assert select_eviction([t1, t2, t3], candidate_pos=30,
                           stale_gap_steps=3) is t1


def test_pool_full_eviction_lru_fallback_and_closed_not_terminated():
    """stale_gap_steps=0 disables the stale leg → LRU eviction; the evicted
    thread leaves the pass-1 card set but is still finalized as a product."""
    sid = "s1"
    groups = []
    for i in range(3):
        frames = [envelope(ui_frame(f"{chr(97 + i)}0", i, app=f"com.{i}"), sid)]
        groups.append((frames, episode_of(frames, sid)))
    batch = [f for frames, _ in groups for f in frames] + [ep for _, ep in groups]
    engine = QueueEngine([obj("new", task="任务A"), obj("new", task="任务B"),
                          obj("new", task="任务C")])
    out, ctx = run_stage(make_cfg(max_open=1, repass=False), batch, engine)
    # every judgment beyond the first saw exactly ONE card (capacity 1)
    assert [pool_card_count(call[1]) for call in engine.calls] == [0, 1, 1]
    # second candidate's card set = thread A evicted? No: capacity 1 means A
    # was evicted when B opened, so C's judgment presents B only.
    cards = [p.text for p in engine.calls[2][1].messages[1].parts[:-1]]
    assert cards[0].startswith("[线索 1] 任务名: 任务B")
    # closure ≠ termination: all three threads remain products (stitch.thread)
    thread_events = [e for e in ctx.metrics.events if e[0] == "stitch.thread"]
    assert len(thread_events) == 3
    assert all(ep.status == "active" and ep.thread_id == ep.record.id
               for _, ep in groups)


# ── pass 2 (T19/M-2): V2 full-miss repair + live view ────────────────────────

def test_repass_repairs_v2_full_miss():
    """V2 walk-through: pass 1 misses everything (all new); the repass merges
    A2 into A1 — candidate becomes the shell, target survives (T6 reversal)."""
    batch, a1, b, a2 = v2_batch()
    originals = [id(x) for x in batch]
    engine = QueueEngine([
        # pass 1: A1 / B / A2 all open threads
        obj("new", task="点外卖"), obj("new", task="打车"),
        obj("new", task="点外卖"),
        # pass 2, session order of the single fragments: A1, B, A2.
        # A1: pool = [A2(recent), B] → new; B: pool = [A2, A1] → new;
        # A2: pool = [B(la=1), A1(la=0)] → resume card 2 = A1.
        obj("new", task="点外卖"), obj("new", task="打车"),
        obj("resume", ref=2, task="点外卖（含收尾）"),
    ])
    out, ctx = run_stage(make_cfg(), batch, engine)
    assert out is batch and [id(x) for x in batch] == originals   # ②c: no append
    assert a2.status == "stitched"                     # candidate became shell
    assert a1.status == "active" and b.status == "active"
    assert a1.thread_id == a1.record.id                # identity stays (T22)
    assert [m.id for m in a1.record.members] == ["a0", "a1", "c0", "c1"]
    assert ctx.metrics.counters["stitch.judgments"] == 3
    assert ctx.metrics.counters["stitch.repass_judgments"] == 3
    # fragments metadata re-sorted in session order, causes origin/resumed
    frags = a1.stitch_fragments
    assert [f["cause"] for f in frags] == ["origin", "resumed"]
    assert frags[0]["source_episode"] == a1.record.id
    assert frags[1]["source_episode"] == a2.record.id
    assert [f["member_count"] for f in frags] == [2, 2]
    assert frags[0]["order_span"] == [0, 1] and frags[1]["order_span"] == [4, 5]
    # rolling task_name from the hit judgment (M-6)
    thread_events = [e for e in ctx.metrics.events if e[0] == "stitch.thread"]
    a1_event = next(e for e in thread_events if e[2] == (a1.record.id,))
    assert a1_event[3]["task_name"] == "点外卖（含收尾）"


def test_repass_live_view_consumes_merged_candidates():
    """The pass-2 target set is a live view: a merged-away candidate leaves the
    pool of later judgments (and stays consumed as a candidate)."""
    sid = "s1"
    groups = []
    for i in range(3):
        frames = [envelope(ui_frame(f"{chr(97 + i)}0", i, app="com.same"), sid)]
        groups.append((frames, episode_of(frames, sid)))
    batch = [f for frames, _ in groups for f in frames] + [ep for _, ep in groups]
    ep_a, ep_b, ep_c = (ep for _, ep in groups)
    engine = QueueEngine([
        obj("new", task="A"), obj("new", task="B"), obj("new", task="C"),
        # pass 2: A → new; B: pool = [C(la=2), A(la=0)] → resume card 1 = C;
        # C: B is dead → pool = [A] only.
        obj("new", task="A"), obj("resume", ref=1, task="C合并B"), obj("new"),
    ])
    run_stage(make_cfg(), batch, engine)
    assert ep_b.status == "stitched"
    assert [m.id for m in ep_c.record.members] == ["b0", "c0"]  # session order
    assert pool_card_count(engine.calls[5][1]) == 1    # live view: B consumed
    assert len(engine.calls) == 6


def test_repass_pool_truncates_to_nearest_by_span():
    assert span_distance(0, 1, 4, 5) == 3
    assert span_distance(4, 5, 0, 1) == 3
    assert span_distance(0, 5, 3, 8) == 0              # overlap → 0
    sid = "s1"
    groups = []
    for i in range(4):
        frames = [envelope(ui_frame(f"{chr(97 + i)}0", i, app=f"com.{i}"), sid)]
        groups.append((frames, episode_of(frames, sid)))
    batch = [f for frames, _ in groups for f in frames] + [ep for _, ep in groups]
    engine = QueueEngine(
        [obj("new", task=f"任务{i}") for i in range(4)]
        + [obj("new")] * 4)                            # repass: all stay
    run_stage(make_cfg(max_open=2), batch, engine)
    # every pass-2 pool was truncated to the 2 nearest-by-span threads
    assert [pool_card_count(c[1]) for c in engine.calls[4:]] == [2, 2, 2, 2]
    # candidate A (span 0): nearest two are B (1) and C (2) — D excluded
    cards = [p.text for p in engine.calls[4][1].messages[1].parts[:-1]]
    assert all("任务3" not in card for card in cards)


def test_repass_off_is_pure_one_pass():
    batch, a1, b, a2 = v2_batch()
    engine = QueueEngine([obj("new"), obj("new"), obj("new")])
    out, ctx = run_stage(make_cfg(repass=False), batch, engine)
    assert len(engine.calls) == 3
    assert "stitch.repass_judgments" not in ctx.metrics.counters


# ── ②c state machine (incl. B-2 asymmetry + on_error) ────────────────────────

def test_pass1_merge_shell_and_rebind():
    """Pass-1 direction: founding envelope survives, candidate becomes shell;
    record.id never recomputed; member frames stay absorbed."""
    batch, a1, b, a2 = v2_batch()
    a1_id = a1.record.id
    engine = QueueEngine([obj("new", task="点外卖"), obj("new", task="打车"),
                          obj("resume", ref=2, task="点外卖")])
    # pool at A2's turn: [B(la=1), A1(la=0)] → A1 = card 2
    out, ctx = run_stage(make_cfg(repass=False), batch, engine)
    assert a2.status == "stitched" and a1.status == "active"
    assert a1.record.id == a1_id                       # NEVER recomputed
    assert [m.id for m in a1.record.members] == ["a0", "a1", "c0", "c1"]
    assert all(f.status == "absorbed" for f in batch[:6]
               if f.record.id.startswith(("a", "c")))
    # the shell keeps its own record untouched (content lives in the thread)
    assert [m.id for m in a2.record.members] == ["c0", "c1"]
    judge_events = [e for e in ctx.metrics.events if e[0] == "stitch.judge"]
    assert judge_events[2][3]["merged"] is True
    assert judge_events[2][3]["priors"] == ["app_overlap", "entity_overlap"]
    assert judge_events[2][3]["target_thread_id"] == a1_id
    assert judge_events[2][3]["repass"] is False


def test_rescue_never_opens_thread_pool_empty_skips_zero_calls():
    """B-2: a rescue run heading the session (empty pool) is skipped with ZERO
    LLM calls and stays dropped_noise."""
    sid = "s1"
    shorts = [envelope(ui_frame(f"s{i}", i, app="com.food"), sid)
              for i in range(2)]
    short_run(shorts)
    batch = [*shorts]                                  # no episodes at all
    out, ctx = run_stage(make_cfg(), batch, ExplodingEngine())
    assert all(f.status == "dropped_noise" for f in shorts)
    assert ctx.metrics.counters == {} and ctx.metrics.events == []

    # rescue-first session WITH a later episode: rescue skipped, episode judged
    shorts2 = [envelope(ui_frame("s0", 0, app="com.food"), sid)]
    short_run(shorts2)
    frames = [envelope(ui_frame("a0", 1, app="com.food"), sid)]
    ep = episode_of(frames, sid)
    batch2 = [*shorts2, *frames, ep]
    engine = QueueEngine([obj("new", task="点外卖")])
    out2, ctx2 = run_stage(make_cfg(repass=False), batch2, engine)
    assert len(engine.calls) == 1                      # episode only
    assert shorts2[0].status == "dropped_noise"        # unrescued: stays
    assert ctx2.metrics.counters["stitch.judgments"] == 1


def test_rescue_hit_flips_frames_with_audit_trail():
    """T11/m-10: rescue hit flips the run's frames dropped_noise→absorbed with
    the rescued_by mark; rescued_short counts FRAMES; fragment cause=rescued
    with source_episode null; contiguous run re-forming fuses adjacent shorts."""
    sid = "s1"
    frames_a = [envelope(ui_frame(f"a{i}", i, app="com.food",
                                  texts=("外卖首页",)), sid) for i in range(2)]
    ep = episode_of(frames_a, sid)
    shorts = [envelope(ui_frame(f"s{i}", 2 + i, app="com.food",
                                texts=("外卖收尾",)), sid) for i in range(3)]
    short_run(shorts)                                  # ONE contiguous run of 3
    batch = [*frames_a, *shorts, ep]
    engine = QueueEngine([obj("new", task="点外卖"),
                          obj("resume", ref=1, task="点外卖")])
    out, ctx = run_stage(make_cfg(repass=False), batch, engine)
    assert len(engine.calls) == 2                      # one rescue candidate (fused run)
    assert all(s.status == "absorbed" for s in shorts)
    assert all(s.rescued_by == ep.record.id for s in shorts)
    assert all(s.noise_attribution == ("segment", "below_min_len")
               for s in shorts)                        # evidence preserved
    assert ctx.metrics.counters["stitch.rescued_short"] == 3   # unit = frames
    assert [m.id for m in ep.record.members] == ["a0", "a1", "s0", "s1", "s2"]
    frags = ep.stitch_fragments
    assert [f["cause"] for f in frags] == ["origin", "rescued"]
    assert frags[1]["source_episode"] is None          # no episode form (T7)
    assert frags[1]["member_count"] == 3
    # no shell was produced — rescue merges never mint stitched envelopes
    assert sum(1 for it in batch if it.status == "stitched") == 0
    judge_events = [e for e in ctx.metrics.events if e[0] == "stitch.judge"]
    assert judge_events[1][3]["candidate"] == "rescue"


def test_rescue_miss_and_judgment_failure_stay_dropped():
    sid = "s1"
    frames_a = [envelope(ui_frame("a0", 0, app="com.food"), sid)]
    ep = episode_of(frames_a, sid)
    shorts = [envelope(ui_frame("s0", 1, app="com.taxi", texts=("打车页",)), sid)]
    short_run(shorts)
    shorts2 = [envelope(ui_frame("s1", 2, app="com.taxi", texts=("打车页",)), sid)]
    short_run(shorts2)
    # frame between the two shorts keeps the runs separate candidates
    mid = envelope(ui_frame("m0", 3, app="com.other"), sid)
    mid.status = "dropped_noise"
    mid.noise_attribution = ("segment", "noise")       # noise: never a candidate
    batch = [*frames_a, *shorts, mid, *shorts2, ep]
    engine = QueueEngine([obj("new", task="点外卖"),
                          obj("resume", ref=1),        # prior miss (taxi × food)
                          SchemaViolation(["/verdict: bad"], "{}")])
    out, ctx = run_stage(make_cfg(on_error="fail", repass=False), batch, engine)
    # miss AND judgment failure both keep dropped_noise — never failed (B-2)
    assert shorts[0].status == "dropped_noise"
    assert shorts2[0].status == "dropped_noise"
    assert shorts2[0].errors == []
    assert ctx.metrics.counters["stitch.failures"] == 1
    assert ctx.metrics.counters["stitch.judgments"] == 2   # failure not counted


def test_on_error_fail_marks_only_episode_envelope():
    sid = "s1"
    frames = [envelope(ui_frame("a0", 0), sid), envelope(ui_frame("a1", 1), sid)]
    ep = episode_of(frames, sid)
    batch = [*frames, ep]
    engine = QueueEngine([SchemaViolation(["/verdict: 非法"], "{}")])
    out, ctx = run_stage(make_cfg(on_error="fail", repass=False), batch, engine)
    assert ep.status == "failed"
    (err,) = ep.errors
    assert (err.stage, err.kind, err.retryable) == ("stitch", "stitch_invalid",
                                                    False)
    # member frames stay absorbed — ②c grants no absorbed→failed migration
    assert all(f.status == "absorbed" for f in frames)
    assert ctx.metrics.counters == {"stitch.failures": 1}
    error_events = [e for e in ctx.metrics.events if e[0] == "error"]
    assert error_events[0][3] == {"stage": "stitch", "kind": "stitch_invalid",
                                  "message": "/verdict: 非法", "retryable": False}
    assert error_events[0][2] == ("a0",)


def test_on_error_keep_opens_thread_without_item_errors():
    sid = "s1"
    frames = [envelope(ui_frame("a0", 0), sid)]
    ep = episode_of(frames, sid)
    batch = [*frames, ep]
    engine = QueueEngine([ValueError("boom")])         # contract ④ isolation
    out, ctx = run_stage(make_cfg(on_error="keep", repass=False), batch, engine)
    assert ep.status == "active" and ep.errors == []
    assert ep.thread_id == ep.record.id                # opened its own thread
    assert ep.stitch_fragments[0]["cause"] == "origin"
    assert ctx.metrics.counters["stitch.failures"] == 1
    assert "stitch.judgments" not in ctx.metrics.counters


def test_idempotent_reentry_costs_zero_calls():
    batch, a1, b, a2 = v2_batch()
    engine = QueueEngine([obj("new"), obj("new"), obj("new")])
    run_stage(make_cfg(repass=False), batch, engine)
    out2, ctx2 = run_stage(make_cfg(repass=False), batch, ExplodingEngine())
    assert out2 is batch
    assert ctx2.metrics.events == [] and ctx2.metrics.counters == {}


# ── seams (T20/M-1/m-8) ──────────────────────────────────────────────────────

def test_seam_iff_gap_holds_foreign_thread_frame():
    """V2: the splice pair's gap holds B's frames → seam at the LEFT member's
    rebound-tuple index, interrupted_by = B's task_name (gap order)."""
    batch, a1, b, a2 = v2_batch()
    engine = QueueEngine([obj("new", task="点外卖"), obj("new", task="打车"),
                          obj("resume", ref=2, task="点外卖")])
    out, ctx = run_stage(make_cfg(repass=False), batch, engine)
    assert a1.seam_indexes == (1,)                     # left member a1 = index 1
    assert a1.seam_interrupted_by == (("打车",),)
    assert b.seam_indexes == ()                        # single fragment: none
    assert ctx.metrics.counters["stitch.seams"] == 1
    a1_event = next(e for e in ctx.metrics.events
                    if e[0] == "stitch.thread" and e[2] == (a1.record.id,))
    assert a1_event[3]["seam_indexes"] == [1]
    assert a1_event[3]["session_id"] == "s1"


def test_noise_only_gap_is_not_a_seam():
    sid = "s1"
    fa = [envelope(ui_frame(f"a{i}", i, app="com.food"), sid) for i in range(2)]
    noise = envelope(ui_frame("n0", 2, app="com.push"), sid)
    noise.status = "dropped_noise"
    noise.noise_attribution = ("segment", "noise")
    fc = [envelope(ui_frame(f"c{i}", 3 + i, app="com.food"), sid)
          for i in range(2)]
    ep_a, ep_c = episode_of(fa, sid), episode_of(fc, sid)
    batch = [*fa, noise, *fc, ep_a, ep_c]
    engine = QueueEngine([obj("new", task="点外卖"),
                          obj("resume", ref=1, task="点外卖")])
    out, ctx = run_stage(make_cfg(repass=False), batch, engine)
    assert ep_a.status == "active" and ep_c.status == "stitched"
    assert ep_a.seam_indexes == ()                     # noise-only gap: no seam
    assert "stitch.seams" not in ctx.metrics.counters


def test_adjacent_rescue_does_not_mask_a_real_transition():
    """A rescued run adjacent to its thread tail leaves NO seam — the splice
    pairs are real transitions (extract judges them normally, T20)."""
    sid = "s1"
    fa = [envelope(ui_frame(f"a{i}", i, app="com.food",
                            texts=("外卖首页",)), sid) for i in range(2)]
    ep = episode_of(fa, sid)
    shorts = [envelope(ui_frame("s0", 2, app="com.food",
                                texts=("外卖首页",)), sid)]
    short_run(shorts)
    batch = [*fa, *shorts, ep]
    engine = QueueEngine([obj("new", task="点外卖"),
                          obj("resume", ref=1, task="点外卖")])
    out, ctx = run_stage(make_cfg(repass=False), batch, engine)
    assert shorts[0].status == "absorbed"
    assert ep.seam_indexes == ()
    assert "stitch.seams" not in ctx.metrics.counters


def test_v4_rescue_over_foreign_gap_is_a_seam():
    """The §1.1 V4 canonical layout: x·A → y·B → 1 noise → w·A-tail (short,
    rescued). The rescue splice pair's gap holds B frames ⇒ seams == 1."""
    sid = "s1"
    fa = [envelope(ui_frame(f"a{i}", i, app="com.food",
                            texts=("外卖首页",)), sid) for i in range(2)]
    fb = [envelope(ui_frame(f"b{i}", 2 + i, app="com.taxi",
                            texts=("打车",)), sid) for i in range(2)]
    noise = envelope(ui_frame("n0", 4, app="com.push"), sid)
    noise.status = "dropped_noise"
    noise.noise_attribution = ("segment", "noise")
    shorts = [envelope(ui_frame(f"s{i}", 5 + i, app="com.food",
                                texts=("外卖首页",)), sid) for i in range(2)]
    short_run(shorts)
    ep_a, ep_b = episode_of(fa, sid), episode_of(fb, sid)
    batch = [*fa, *fb, noise, *shorts, ep_a, ep_b]
    engine = QueueEngine([obj("new", task="点外卖"), obj("new", task="打车"),
                          obj("resume", ref=2, task="点外卖")])
    out, ctx = run_stage(make_cfg(repass=False), batch, engine)
    assert all(s.status == "absorbed" for s in shorts)
    assert ep_a.seam_indexes == (1,)                   # a1 → s0 gap holds B
    assert ep_a.seam_interrupted_by == (("打车",),)
    assert ctx.metrics.counters["stitch.seams"] == 1
    assert ctx.metrics.counters["stitch.rescued_short"] == 2
    # V1-style redundancy column: threads = episodes − stitched (2 = 2 − 0)
    active_threads = [it for it in batch
                      if it.record.kind == "sequence" and it.status == "active"]
    shells = [it for it in batch if it.status == "stitched"]
    assert len(active_threads) == 2 and len(shells) == 0


# ── conservation (batch-level closure of the §3.3 algebra) ───────────────────

def test_conservation_over_stitched_batch():
    """Every envelope lands in a terminal/active bucket; frames == absorbed +
    dropped_noise; threads == episodes − stitched (the T7 identity)."""
    batch, a1, b, a2 = v2_batch()
    noise = envelope(ui_frame("n0", 9, app="com.push"), "s1")
    noise.status = "dropped_noise"
    noise.noise_attribution = ("segment", "noise")
    batch.insert(6, noise)
    engine = QueueEngine([obj("new", task="点外卖"), obj("new", task="打车"),
                          obj("resume", ref=2, task="点外卖")])
    out, ctx = run_stage(make_cfg(repass=False), batch, engine)
    tally: dict[str, int] = {}
    for item in batch:
        tally[item.status] = tally.get(item.status, 0) + 1
    frames = [it for it in batch if it.record.kind == "single"]
    episodes = [it for it in batch if it.record.kind == "sequence"]
    assert len(frames) == (sum(1 for f in frames if f.status == "absorbed")
                           + sum(1 for f in frames
                                 if f.status == "dropped_noise"))
    stitched = sum(1 for e in episodes if e.status == "stitched")
    active = sum(1 for e in episodes if e.status == "active")
    assert active == len(episodes) - stitched          # threads = episodes − stitched
    assert tally == {"absorbed": 6, "dropped_noise": 1, "active": 2,
                     "stitched": 1}


# ── votes (T18/M-4) ──────────────────────────────────────────────────────────

def test_aggregate_votes_strict_majority_on_complete_pair():
    win = obj("resume", ref=1, task="首个", reason="r1")
    win2 = obj("resume", ref=1, task="次个", reason="r2")
    lose = obj("new")
    picked = aggregate_votes([win, lose, win2])
    assert picked is win                               # majority cluster's FIRST sample
    # verdict majority but thread_ref split → NO strict majority → None
    assert aggregate_votes([obj("resume", ref=1), obj("resume", ref=2),
                            obj("new")]) is None
    # three-way split → None; exact half is NOT strict
    assert aggregate_votes([obj("resume", ref=1), obj("new"),
                            obj("resume", ref=2)]) is None
    assert aggregate_votes([obj("new")]) is not None   # n=1 trivially wins


def test_votes_three_samples_strict_majority_drives_merge():
    sid = "s1"
    fa = [envelope(ui_frame("a0", 0, app="com.food"), sid)]
    fc = [envelope(ui_frame("c0", 1, app="com.food"), sid)]
    ep_a, ep_c = episode_of(fa, sid), episode_of(fc, sid)
    batch = [*fa, *fc, ep_a, ep_c]
    engine = QueueEngine([
        # candidate 1: three samples, all new
        obj("new", task="点外卖"), obj("new", task="点外卖"), obj("new"),
        # candidate 2: resume ref=1 wins 2:1 → merge
        obj("resume", ref=1, task="点外卖"), obj("new"),
        obj("resume", ref=1, task="点外卖"),
    ])
    out, ctx = run_stage(make_cfg(votes=3, repass=False), batch, engine)
    assert len(engine.calls) == 6                      # 2 judgments × 3 samples
    assert ep_c.status == "stitched"
    assert ctx.metrics.counters["stitch.judgments"] == 2


def test_votes_split_falls_back_conservatively():
    sid = "s1"
    fa = [envelope(ui_frame("a0", 0, app="com.food"), sid)]
    fc = [envelope(ui_frame("c0", 1, app="com.food"), sid)]
    ep_a, ep_c = episode_of(fa, sid), episode_of(fc, sid)
    batch = [*fa, *fc, ep_a, ep_c]
    engine = QueueEngine([
        obj("new", task="点外卖"), obj("new", task="点外卖"),
        obj("new", task="点外卖"),
        # split: resume/1, resume/… no wait — verdict majority, ref split
        obj("resume", ref=1), obj("resume", ref=None), obj("new"),
    ])
    out, ctx = run_stage(make_cfg(votes=3, repass=False), batch, engine)
    assert ep_c.status == "active"                     # episode fallback = new
    assert ep_c.thread_id == ep_c.record.id
    judge_events = [e for e in ctx.metrics.events if e[0] == "stitch.judge"]
    assert judge_events[1][3]["votes_split"] is True
    assert judge_events[1][3]["verdict"] == "new"      # conservative record


# ── off byte-equivalence anchor (m-11) + chain wiring ────────────────────────

def test_stage_untouched_when_disabled_and_factory_gating():
    from labelkit.orchestration.factory import build_stages

    off = make_cfg(enabled=False)
    assert [s.name for s in build_stages(off)] == ["segment", "dedup", "quality",
                                                   "annotate"]
    on = make_cfg()
    assert [s.name for s in build_stages(on)] == ["segment", "stitch", "dedup",
                                                  "quality", "annotate"]


def test_off_meta_stream_is_byte_shape_identical_to_v18(tmp_path):
    """m-11 anchor: with stitch disabled the _meta.stream key set (and step
    rows) is EXACTLY the v1.8 shape — no thread_id/fragments/resumed anywhere;
    enabling stitch adds exactly those three."""
    from labelkit.operators.emitter import Emitter

    def emitter_for(cfg):
        return Emitter(cfg, engine=None, run_id="abcdef012345",
                       run_started_at=datetime.now().astimezone())

    frames = [envelope(ui_frame(f"a{i}", i), "s1") for i in range(2)]
    ep = episode_of(frames, "s1")
    ep.transitions = (Transition(index=0, action={"action_type": "click",
                                                  "target": "按钮", "value": None,
                                                  "description": "点击"},
                                 model="m", attempts=1, detail={}),)

    off_cfg = make_cfg(enabled=False, output=str(tmp_path / "o.jsonl"))
    stream_off = emitter_for(off_cfg)._stream_block(ep)
    assert set(stream_off) == {"episode_id", "session_id", "order_span",
                               "member_count", "member_ids", "member_sources",
                               "session_split", "repaired", "degraded", "steps"}
    assert all("resumed" not in row for row in stream_off["steps"])

    on_cfg = make_cfg(output=str(tmp_path / "o2.jsonl"))
    ep.thread_id = ep.record.id
    ep.stitch_fragments = ({"order_span": [0, 1], "member_count": 2,
                            "cause": "origin",
                            "source_episode": ep.record.id},)
    stream_on = emitter_for(on_cfg)._stream_block(ep)
    assert set(stream_on) == set(stream_off) | {"thread_id", "fragments"}
    assert stream_on["thread_id"] == ep.record.id
    assert stream_on["fragments"] == [dict(ep.stitch_fragments[0])]
    assert stream_on["steps"][0]["resumed"] is False   # non-seam step
    # envelope order_span stays the M11 rendering (包络 rule untouched)
    assert stream_on["order_span"] == stream_off["order_span"]


def test_stitched_shell_takes_emitter_fourth_route(tmp_path):
    """T21: shells reach neither channel and never trip the rejects fallback."""
    from labelkit.operators.emitter import Emitter

    cfg = make_cfg(output=str(tmp_path / "out.jsonl"))
    emitter = Emitter(cfg, engine=None, run_id="abcdef012345",
                      run_started_at=datetime.now().astimezone())
    emitter.open()
    frames = [envelope(ui_frame(f"a{i}", i), "s1") for i in range(2)]
    shell = episode_of(frames, "s1")
    shell.status = "stitched"
    result = emitter.emit_batch([*frames, shell], batch_no=1)
    emitter.finalize({"counts": {}}, deliver=True)
    assert (result.emitted, result.rejected) == (0, 0)
    assert (tmp_path / "out.jsonl").read_text(encoding="utf-8") == ""
    rejects = (tmp_path / "out.rejects.jsonl")
    assert rejects.read_text(encoding="utf-8") == ""   # shell NOT in rejects


def test_multi_session_batches_processed_independently_in_order():
    fa = [envelope(ui_frame("a0", 0, app="com.a"), "sa")]
    fb = [envelope(ui_frame("b0", 0, app="com.b"), "sb")]
    ep_a, ep_b = episode_of(fa, "sa"), episode_of(fb, "sb")
    batch = [*fa, *fb, ep_a, ep_b]
    engine = QueueEngine([obj("new", task="A任务"), obj("new", task="B任务")])
    out, ctx = run_stage(make_cfg(repass=False), batch, engine)
    # each session's bootstrap saw an EMPTY pool (threads never cross sessions)
    assert [pool_card_count(c[1]) for c in engine.calls] == [0, 0]
    assert engine.calls[0][3] == ("a0",) and engine.calls[1][3] == ("b0",)
    thread_events = [e for e in ctx.metrics.events if e[0] == "stitch.thread"]
    assert [e[3]["session_id"] for e in thread_events] == ["sa", "sb"]

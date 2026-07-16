"""Offline unit tests for M15 extract: selection/idempotency (spec 3.15.2), the
§10.10 prompt assembly (verbatim system text, two-image user message, the
include_diff two-state tail, per-label instruction via class_views), transition
count/index invariants, the on_error fallback/fail policy (S16), by_type
counters, multi-sibling independent extraction (S9) and the extract.step event
payload shape. Pure logic only — no LLM: the schema engine is replaced by the
in-process complete_validated stubs (test_classify 惯例)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from labelkit.operators.extract import ExtractStage, build_extract_prompt, extract_transition
from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ClassSpec,
    ClassView,
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
from labelkit.common.errors import ProviderRetryableError, SchemaViolation
from labelkit.common.runtime.schema_engine import action_schema
from labelkit.common.contracts.types import (
    Classification,
    ImageRef,
    PipelineItem,
    Record,
    RecordRef,
    Transition,
    UINode,
    UITree,
    Usage,
    frame_digest,
)

# §10.10 system message, byte-exact (vocabulary bullets in the frozen order and
# the OpenCUA anchoring sentence; no instruction line in the default config).
SYSTEM_TEXT = (
    "你是屏幕操作流的动作摘取员。给定同一操作流中相邻的前后两帧屏幕状态，推断用户在两帧之间\n"
    "执行的动作。action_type 只能取以下值：\n"
    "- click / long_press / drag: 点击 / 长按 / 拖拽某控件\n"
    "- input_text: 在输入框键入文本\n"
    "- scroll: 滚动屏幕或列表\n"
    "- open_app: 打开一个应用；app_switch: 切换到另一已打开的应用\n"
    "- navigate_back / navigate_home: 系统返回 / 回到桌面\n"
    "- wait: 无用户交互，仅等待界面加载或变化\n"
    "- other: 无法归入以上任何一类（把语义写进 description）\n"
    "锚定约定：前一帧是动作发生前最后一个稳定状态，后一帧是动作完成后的首个稳定状态；推断\n"
    "二者之间发生的单个语义动作；若变化由多个低层事件构成（连续滚动、连续键入），归并为一个\n"
    "语义动作。\n"
    "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
    '{"action_type": <词表值>, "target": <目标控件文本引用或 null>,\n'
    ' "value": <动作参数或 null>, "description": <一句话动作描述>}'
)


def make_cfg(*, include_diff=True, instruction="", on_error="fallback",
             class_views=None) -> ResolvedConfig:
    return ResolvedConfig(
        tool=ToolConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality="ui", input="in"),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(enabled=True),
        stitch=StitchConfig(),
        extract=ExtractConfig(enabled=True, llm="default", instruction=instruction,
                              include_diff=include_diff, on_error=on_error),
        classify=ClassifyConfig(),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(instruction="标注"),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=TraceConfig(),
        rubric=Rubric(name="default:trajectory", criteria=()),
        class_views=class_views or {},
        user_schema={"type": "object"},
        limit=None,
        strict=False,
        dry_run=False,
        config_path="config.toml",
        project_path="project.toml",
        config_digest="sha256:0",
        project_digest="sha256:0",
    )


def class_view(name: str, extract_instruction: str) -> ClassView:
    return ClassView(name=name, quality=QualityConfig(),
                     rubric=Rubric(name="default:trajectory", criteria=()),
                     annotate=AnnotateConfig(instruction="标注"),
                     generate=GenerateConfig(), verify=VerifyConfig(),
                     extract=ExtractConfig(enabled=True,
                                           instruction=extract_instruction))


def frame(rid: str, title: str, pair_index: int = 0) -> Record:
    """One UI frame: a root FrameLayout plus a visible TextView carrying `title`
    (identical structural keys across frames — only the text differs, so a
    tree_diff of two frames is exactly one text_changed pair)."""
    nodes = (
        UINode("1", None, 0, "FrameLayout", "", "", (0, 0, 1080, 1920), True, {}),
        UINode("2", "1", 1, "TextView", title, "", (0, 0, 1080, 200), True, {}),
    )
    image = ImageRef(path=Path(f"{rid}.png"), format="png", size_bytes=1)
    return Record(id=rid, modality="ui", text=None, raw=None,
                  ui_tree=UITree(nodes), image=image,
                  ref=RecordRef("a/uitree_1.jsonl", None, pair_index, ()))


def episode(members, eid="7655568d2c485c43") -> Record:
    """Sequence Record per the S24 field convention (text/raw/ui_tree/image None,
    ref inherited from the first member)."""
    return Record(id=eid, modality="ui", text=None, raw=None, ui_tree=None,
                  image=None, ref=members[0].ref, kind="sequence",
                  members=tuple(members))


def text_episode(eid="feedfeedfeedfeed") -> Record:
    """A text-modality sequence — not producible under M1's extract constraints
    (rule 30), used only to pin the defensive modality re-check."""
    member = Record(id="t" * 16, modality="text", text="第一帧", raw={},
                    ui_tree=None, image=None, ref=RecordRef("d.jsonl", 1, None, ()))
    return Record(id=eid, modality="text", text=None, raw=None, ui_tree=None,
                  image=None, ref=member.ref, kind="sequence", members=(member,))


CLICK = {"action_type": "click", "target": "去结算", "value": None,
         "description": "点击「去结算」进入下单确认页"}
INPUT = {"action_type": "input_text", "target": "搜索框", "value": "麻辣烫",
         "description": "在首页搜索框键入「麻辣烫」并搜索"}


# ── in-process complete_validated stubs (no LLM, test_classify 惯例) ─────────

class QueueEngine:
    """Pops queued outcomes in call order — the ONE flat gather creates its
    coroutines in (episode batch position, pair ordinal) order and the stubs
    never yield, so pop order is deterministic."""

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


class PairEngine:
    """Keyed by the (prev id, next id) record_ids pair (scheduling-independent)."""

    def __init__(self, by_pair):
        self.by_pair = dict(by_pair)
        self.calls: list = []

    async def complete_validated(self, profile, prompt, schema=None, *,
                                 record_ids=(), batch_no=0, record=None):
        self.calls.append((profile, prompt, schema, record_ids))
        out = self.by_pair[record_ids]
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
    out = asyncio.run(ExtractStage(cfg).run(batch, ctx))
    return out, ctx


# ── prompt assembly (§10.10, deterministic) ──────────────────────────────────

def test_system_text_verbatim_vocabulary_and_anchor():
    f0, f1 = frame("f0" * 8, "首页"), frame("f1" * 8, "搜索结果页")
    bundle = build_extract_prompt(f0, f1, make_cfg(), label=None)
    assert [m.role for m in bundle.messages] == ["system", "user"]
    assert bundle.messages[0].parts[0].text == SYSTEM_TEXT
    assert bundle.temperature is None          # temp 0 via the profile default
    # the 11-value vocabulary appears inside the bullet block, enum-complete
    vocab_block = SYSTEM_TEXT.split("锚定约定")[0]
    for action in action_schema()["properties"]["action_type"]["enum"]:
        assert action in vocab_block


def test_user_message_five_parts_two_images_diff_and_digest_tail():
    f0, f1 = frame("f0" * 8, "首页"), frame("f1" * 8, "搜索结果页")
    bundle = build_extract_prompt(f0, f1, make_cfg(), label=None)
    (msg,) = [m for m in bundle.messages if m.role == "user"]
    assert [p.kind for p in msg.parts] == ["text", "image", "text", "image", "text"]
    assert msg.parts[0].text == "[前一帧截图]"
    assert msg.parts[1].image is f0.image
    assert msg.parts[2].text == "[后一帧截图]"
    assert msg.parts[3].image is f1.image
    # identical structural keys, one text change out of max(2,2) visible nodes;
    # the diff line uses the same fixed form as M14's §10.9 rendering
    assert msg.parts[4].text == (
        "[树变更摘要] 新增 0 节点，移除 0 节点，文本变化 1 处，变更比例 50%，标题变化\n"
        f"[前后帧树摘要] {frame_digest(f0, 400)} → {frame_digest(f1, 400)}"
    )


def test_include_diff_false_omits_diff_line_keeps_digest_tail():
    f0, f1 = frame("f0" * 8, "首页"), frame("f1" * 8, "搜索结果页")
    bundle = build_extract_prompt(f0, f1, make_cfg(include_diff=False), label=None)
    msg = bundle.messages[-1]
    # same five-part shape in both states — only the tail content differs (S6:
    # the closing text part is always present)
    assert [p.kind for p in msg.parts] == ["text", "image", "text", "image", "text"]
    assert msg.parts[4].text == (
        f"[前后帧树摘要] {frame_digest(f0, 400)} → {frame_digest(f1, 400)}")
    assert "[树变更摘要]" not in msg.parts[4].text


def test_instruction_line_between_anchor_and_structure_sentence():
    f0, f1 = frame("f0" * 8, "首页"), frame("f1" * 8, "搜索结果页")
    bundle = build_extract_prompt(f0, f1, make_cfg(instruction="只关注下单主流程。"),
                                  label=None)
    assert ("语义动作。\n"
            "只关注下单主流程。\n"
            "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
            ) in bundle.messages[0].parts[0].text


def test_label_takes_instruction_from_class_views():
    f0, f1 = frame("f0" * 8, "首页"), frame("f1" * 8, "搜索结果页")
    cfg = make_cfg(instruction="全局说明",
                   class_views={"food": class_view("food", "按外卖领域摘取。")})
    labeled = build_extract_prompt(f0, f1, cfg, label="food")
    assert "按外卖领域摘取。" in labeled.messages[0].parts[0].text
    assert "全局说明" not in labeled.messages[0].parts[0].text
    unlabeled = build_extract_prompt(f0, f1, cfg, label=None)
    assert "全局说明" in unlabeled.messages[0].parts[0].text


# ── extract_transition (direct-call surface) ─────────────────────────────────

def test_transition_clean_path_uses_internal_schema():
    cfg = make_cfg()
    f0, f1 = frame("f0" * 8, "首页"), frame("f1" * 8, "搜索结果页")
    engine = QueueEngine([INPUT])
    t = asyncio.run(extract_transition(f0, f1, 0, make_ctx(cfg, engine)))
    assert t == Transition(index=0, action=INPUT, model="glm-5.2", attempts=1,
                           detail={})
    profile, prompt, schema, record_ids = engine.calls[0]
    assert profile == "default"
    assert schema == action_schema()
    assert record_ids == (f0.id, f1.id)
    assert prompt.temperature is None


def test_transition_scroll_direction_lowercased_code_side():
    cfg = make_cfg()
    f0, f1 = frame("f0" * 8, "列表页"), frame("f1" * 8, "列表页下方")
    scroll = {"action_type": "scroll", "target": "列表", "value": "DOWN",
              "description": "向下滚动列表"}
    t = asyncio.run(extract_transition(f0, f1, 0,
                                       make_ctx(cfg, QueueEngine([scroll]))))
    assert t.action["value"] == "down"


def test_transition_fallback_counts_and_emits_error_event():
    cfg = make_cfg()                                   # on_error="fallback" default
    f0, f1 = frame("f0" * 8, "首页"), frame("f1" * 8, "搜索结果页")
    violation = SchemaViolation(["/action_type: 枚举违规"], '{"action_type": "tap"}')
    ctx = make_ctx(cfg, QueueEngine([violation]))
    t = asyncio.run(extract_transition(f0, f1, 3, ctx))
    assert t.action == {"action_type": "other", "target": None, "value": None,
                        "description": ""}
    assert t.index == 3 and t.model == ""
    assert t.attempts == 1 + cfg.output.max_repair_attempts
    assert t.detail["kind"] == "extraction_invalid"
    assert "枚举违规" in t.detail["message"]
    assert ctx.metrics.counters == {"extract.fallback_steps": 1}
    (ev, stage, record_ids, payload), = ctx.metrics.events
    assert (ev, stage, record_ids) == ("error", "extract", (f0.id, f1.id))
    assert payload == {"stage": "extract", "kind": "extraction_invalid",
                       "message": "/action_type: 枚举违规", "retryable": False}


def test_transition_on_error_fail_reraises():
    cfg = make_cfg(on_error="fail")
    f0, f1 = frame("f0" * 8, "首页"), frame("f1" * 8, "搜索结果页")
    violation = SchemaViolation(["/action_type: 枚举违规"], "{}")
    ctx = make_ctx(cfg, QueueEngine([violation]))
    with pytest.raises(SchemaViolation):
        asyncio.run(extract_transition(f0, f1, 0, ctx))
    assert ctx.metrics.counters == {} and ctx.metrics.events == []


# ── stage: selection & idempotency ───────────────────────────────────────────

def test_stage_skips_items_with_existing_transitions():
    existing = (Transition(index=0, action=CLICK, model="glm-5.2", attempts=1,
                           detail={}),)
    f0, f1 = frame("f0" * 8, "首页"), frame("f1" * 8, "搜索结果页")
    item = PipelineItem(record=episode([f0, f1]), transitions=existing)
    empty = PipelineItem(record=episode([f0, f1], eid="a" * 16), transitions=())
    batch = [item, empty]
    out, ctx = run_stage(make_cfg(), batch, ExplodingEngine())   # zero calls
    assert out is batch
    assert item.transitions is existing
    assert empty.transitions == ()                     # non-None: idempotent skip
    assert ctx.metrics.events == [] and ctx.metrics.counters == {}


def test_stage_ignores_single_records_non_active_and_text_sequences():
    single = PipelineItem(record=frame("f0" * 8, "首页"))
    dropped = PipelineItem(record=episode([frame("f1" * 8, "a"),
                                           frame("f2" * 8, "b")], eid="b" * 16),
                           status="failed")
    textual = PipelineItem(record=text_episode())      # defensive modality re-check
    batch = [single, dropped, textual]
    out, ctx = run_stage(make_cfg(), batch, ExplodingEngine())
    assert out is batch
    assert all(it.transitions is None for it in batch)
    assert ctx.metrics.events == [] and ctx.metrics.counters == {}


# ── stage: happy path, invariants, events, counters ──────────────────────────

def test_stage_transition_count_is_members_minus_one_indices_zero_based():
    cfg = make_cfg()
    f0, f1, f3, f4 = (frame("f0" * 8, "首页"), frame("f1" * 8, "搜索结果页"),
                      frame("f3" * 8, "餐厅页"), frame("f4" * 8, "下单确认页"))
    item = PipelineItem(record=episode([f0, f1, f3, f4]))
    click2 = {**CLICK, "target": "老王麻辣烫", "description": "点击餐厅进入餐厅页"}
    engine = PairEngine({(f0.id, f1.id): INPUT,
                         (f1.id, f3.id): click2,
                         (f3.id, f4.id): CLICK})
    out, ctx = run_stage(cfg, [item], engine)
    assert out[0] is item and item.status == "active"
    assert len(item.transitions) == len(item.record.members) - 1 == 3
    assert [t.index for t in item.transitions] == [0, 1, 2]
    assert [t.action for t in item.transitions] == [INPUT, click2, CLICK]
    assert all(t.model == "glm-5.2" and t.attempts == 1 and t.detail == {}
               for t in item.transitions)
    assert ctx.metrics.counters == {"extract.transitions": 3,
                                    "extract.by_type.input_text": 1,
                                    "extract.by_type.click": 2}


def test_stage_flat_gather_order_and_step_event_payload_shape():
    cfg = make_cfg()
    f0, f1, f2 = (frame("f0" * 8, "首页"), frame("f1" * 8, "搜索结果页"),
                  frame("f2" * 8, "餐厅页"))
    g0, g1 = frame("g0" * 8, "桌面"), frame("g1" * 8, "设置页")
    ep1 = PipelineItem(record=episode([f0, f1, f2], eid="e1" * 8))
    ep2 = PipelineItem(record=episode([g0, g1], eid="e2" * 8))
    engine = QueueEngine([INPUT, CLICK, CLICK])
    out, ctx = run_stage(cfg, [ep1, ep2], engine)
    # ONE flat gather: calls in (episode batch position, pair ordinal) order
    assert [c[3] for c in engine.calls] == [
        (f0.id, f1.id), (f1.id, f2.id), (g0.id, g1.id)]
    steps = [e for e in ctx.metrics.events if e[0] == "extract.step"]
    assert [(s[1], s[2]) for s in steps] == [
        ("extract", (f0.id, f1.id)), ("extract", (f1.id, f2.id)),
        ("extract", (g0.id, g1.id))]
    assert steps[0][3] == {"episode_id": "e1" * 8, "index": 0,
                           "action_type": "input_text",
                           "description": "在首页搜索框键入「麻辣烫」并搜索",
                           "target": "搜索框", "value": "麻辣烫"}
    assert steps[2][3] == {"episode_id": "e2" * 8, "index": 0,
                           "action_type": "click",
                           "description": "点击「去结算」进入下单确认页",
                           "target": "去结算", "value": None}


# ── stage: on_error two paths (S16) ──────────────────────────────────────────

def test_stage_fallback_step_keeps_episode_alive_and_invariant():
    cfg = make_cfg()                                   # on_error="fallback" default
    f0, f1, f2 = (frame("f0" * 8, "首页"), frame("f1" * 8, "搜索结果页"),
                  frame("f2" * 8, "餐厅页"))
    item = PipelineItem(record=episode([f0, f1, f2]))
    violation = SchemaViolation(["/action_type: 枚举违规"], "{}")
    engine = PairEngine({(f0.id, f1.id): violation, (f1.id, f2.id): CLICK})
    out, ctx = run_stage(cfg, [item], engine)
    assert item.status == "active"
    assert item.errors == []                           # R4: never item.errors
    assert len(item.transitions) == 2                  # invariant survives the step
    fallback, clean = item.transitions
    assert fallback.action == {"action_type": "other", "target": None,
                               "value": None, "description": ""}
    assert fallback.index == 0
    assert fallback.detail["kind"] == "extraction_invalid"
    assert clean.action == CLICK and clean.detail == {}
    # by_type counts EVERY final step — the fallback lands in "other"
    assert ctx.metrics.counters == {"extract.fallback_steps": 1,
                                    "extract.transitions": 2,
                                    "extract.by_type.other": 1,
                                    "extract.by_type.click": 1}
    error_events = [e for e in ctx.metrics.events if e[0] == "error"]
    assert len(error_events) == 1
    assert error_events[0][2] == (f0.id, f1.id)
    assert error_events[0][3]["kind"] == "extraction_invalid"
    # fallback steps still emit extract.step (payload from the fallback action)
    steps = [e for e in ctx.metrics.events if e[0] == "extract.step"]
    assert [s[3]["action_type"] for s in steps] == ["other", "click"]


def test_stage_on_error_fail_fails_episode_and_discards_other_steps():
    cfg = make_cfg(on_error="fail")
    f0, f1, f2 = (frame("f0" * 8, "首页"), frame("f1" * 8, "搜索结果页"),
                  frame("f2" * 8, "餐厅页"))
    item = PipelineItem(record=episode([f0, f1, f2]))
    violation = SchemaViolation(["/action_type: 枚举违规"], "{}")
    engine = PairEngine({(f0.id, f1.id): CLICK, (f1.id, f2.id): violation})
    out, ctx = run_stage(cfg, [item], engine)
    assert item.status == "failed"
    assert item.transitions is None                    # pair-0 result discarded
    (err,) = item.errors
    assert (err.stage, err.kind, err.retryable) == ("extract",
                                                    "extraction_invalid", False)
    assert ctx.metrics.counters == {"extract.failures": 1}
    (ev, stage, record_ids, payload), = ctx.metrics.events
    assert (ev, record_ids) == ("error", (item.record.id,))
    assert payload["kind"] == "extraction_invalid" and payload["retryable"] is False


def test_provider_error_fails_episode_without_breaking_siblings():
    cfg = make_cfg()
    f0, f1 = frame("f0" * 8, "首页"), frame("f1" * 8, "搜索结果页")
    g0, g1 = frame("g0" * 8, "桌面"), frame("g1" * 8, "设置页")
    bad = PipelineItem(record=episode([f0, f1], eid="e1" * 8))
    good = PipelineItem(record=episode([g0, g1], eid="e2" * 8))
    exc = ProviderRetryableError("timeout", profile="default", retries=5)
    engine = PairEngine({(f0.id, f1.id): exc, (g0.id, g1.id): CLICK})
    out, ctx = run_stage(cfg, [bad, good], engine)
    assert bad.status == "failed" and bad.transitions is None
    (err,) = bad.errors
    assert (err.kind, err.retryable) == ("provider_retryable_exhausted", True)
    assert good.status == "active" and len(good.transitions) == 1
    assert ctx.metrics.counters == {"extract.failures": 1,
                                    "extract.transitions": 1,
                                    "extract.by_type.click": 1}


# ── stage: multi fan-out siblings each extract (S9) ──────────────────────────

def test_multi_siblings_extract_independently_per_label():
    views = {"food": class_view("food", "按外卖领域摘取。"),
             "shopping": class_view("shopping", "按购物领域摘取。")}
    cfg = make_cfg(class_views=views)
    f0, f1 = frame("f0" * 8, "首页"), frame("f1" * 8, "搜索结果页")
    rec = episode([f0, f1])
    labels = ("food", "shopping")
    original = PipelineItem(record=rec, classification=Classification(
        label="food", labels=labels, source="llm", detail={}))
    sibling = PipelineItem(record=rec, classification=Classification(
        label="shopping", labels=labels, source="llm", detail={}))
    batch = [original, sibling]
    engine = QueueEngine([INPUT, CLICK])
    out, ctx = run_stage(cfg, batch, engine)
    # NOT de-duplicated by record id: one call per envelope, per-label prompts
    assert len(engine.calls) == 2
    sys_texts = [c[1].messages[0].parts[0].text for c in engine.calls]
    assert "按外卖领域摘取。" in sys_texts[0]
    assert "按购物领域摘取。" in sys_texts[1]
    # transitions are per-envelope self-contained
    assert original.transitions is not sibling.transitions
    assert [t.action for t in original.transitions] == [INPUT]
    assert [t.action for t in sibling.transitions] == [CLICK]
    assert ctx.metrics.counters["extract.transitions"] == 2
    steps = [e for e in ctx.metrics.events if e[0] == "extract.step"]
    assert [s[3]["episode_id"] for s in steps] == [rec.id, rec.id]


# ── v1.9 stitch seams (T10/T20): zero-LLM placeholders + counter exclusion ───

def test_seam_indexes_take_placeholder_and_skip_llm():
    """Pairs at seam_indexes never reach the LLM: the T10 four-key placeholder
    lands at the pinned index, detail carries kind/interrupted_by, and the
    len(transitions) == len(members) − 1 invariant holds."""
    frames = [frame(f"f{i}" * 8, f"页面{i}", pair_index=i) for i in range(4)]
    item = PipelineItem(record=episode(frames))
    item.seam_indexes = (1,)
    item.seam_interrupted_by = (("打车",),)
    engine = QueueEngine([INPUT, CLICK])               # pairs 0 and 2 only
    out, ctx = run_stage(make_cfg(), [item], engine)
    assert len(engine.calls) == 2                      # seam pair judged ZERO times
    called_pairs = [c[3] for c in engine.calls]
    assert (frames[1].id, frames[2].id) not in called_pairs
    assert len(item.transitions) == 3
    assert [t.index for t in item.transitions] == [0, 1, 2]
    seam = item.transitions[1]
    assert seam.action == {"action_type": "app_switch", "target": None,
                           "value": None,
                           "description": "线索接缝：被打车打断后恢复"}
    assert seam.detail == {"kind": "thread_seam", "interrupted_by": ["打车"]}
    assert (seam.model, seam.attempts) == ("", 0)      # no call was ever made
    assert [t.action for t in (item.transitions[0], item.transitions[2])] == [
        INPUT, CLICK]


def test_seam_placeholders_excluded_from_counters_and_events():
    """T20 计数器口径: seam placeholders never feed extract.transitions /
    by_type.* (their zero-LLM app_switch must not pollute the histogram) nor
    the extract.step event — the seam's metering point is stream.stitch.seams."""
    frames = [frame(f"f{i}" * 8, f"页面{i}", pair_index=i) for i in range(3)]
    item = PipelineItem(record=episode(frames))
    item.seam_indexes = (0,)
    item.seam_interrupted_by = (("打车", "社交"),)
    engine = QueueEngine([CLICK])                      # pair 1 only
    out, ctx = run_stage(make_cfg(), [item], engine)
    assert ctx.metrics.counters["extract.transitions"] == 1
    assert ctx.metrics.counters["extract.by_type.click"] == 1
    assert "extract.by_type.app_switch" not in ctx.metrics.counters
    steps = [e for e in ctx.metrics.events if e[0] == "extract.step"]
    assert len(steps) == 1 and steps[0][3]["index"] == 1
    # multiple interrupters render 、-joined in gap order
    assert item.transitions[0].action["description"] == (
        "线索接缝：被打车、社交打断后恢复")


def test_multiple_seams_and_all_seam_episode_zero_calls():
    frames = [frame(f"f{i}" * 8, f"页面{i}", pair_index=i) for i in range(3)]
    item = PipelineItem(record=episode(frames))
    item.seam_indexes = (0, 1)
    item.seam_interrupted_by = (("打车",), ("社交",))
    out, ctx = run_stage(make_cfg(), [item], ExplodingEngine())   # zero LLM
    assert len(item.transitions) == 2
    assert all(t.detail["kind"] == "thread_seam" for t in item.transitions)
    assert item.transitions[0].detail["interrupted_by"] == ["打车"]
    assert item.transitions[1].detail["interrupted_by"] == ["社交"]
    assert "extract.transitions" not in ctx.metrics.counters


def test_no_seam_marks_keeps_v18_path_byte_identical():
    """Regression anchor: without seam duck marks the flat-gather accounting
    behaves exactly as v1.8 (one coroutine per pair, all steps counted)."""
    frames = [frame(f"f{i}" * 8, f"页面{i}", pair_index=i) for i in range(3)]
    item = PipelineItem(record=episode(frames))
    engine = QueueEngine([INPUT, CLICK])
    out, ctx = run_stage(make_cfg(), [item], engine)
    assert len(engine.calls) == 2
    assert [t.action for t in item.transitions] == [INPUT, CLICK]
    assert ctx.metrics.counters["extract.transitions"] == 2

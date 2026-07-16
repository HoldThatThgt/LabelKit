"""Offline unit tests for M13 classify: prompt assembly (spec 3.13.3 / CONTRACTS §10.8),
post-M8 normalization, self-consistency voting (R26 own rules), the on_error two-path
policy (R4), and multi fan-out (contract ②a). Pure logic only — no LLM: the schema
engine is replaced by the in-process complete_validated stubs (test_annotate 惯例)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from labelkit.operators.classify import (
    ClassifyStage,
    _normalize_labels,
    _reason_requested,
    build_classify_prompt,
    classify_record,
)
from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ClassSpec,
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
    StreamConfig,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)
from labelkit.common.errors import ProviderRetryableError, SchemaViolation
from labelkit.common.runtime.schema_engine import classification_schema
from labelkit.common.contracts.types import (
    Classification,
    DedupInfo,
    ImageRef,
    PipelineItem,
    Record,
    RecordRef,
    UINode,
    UITree,
    Usage,
    frame_digest,
)

# Class table mirroring the spec 3.13.6 worked example (declaration order matters).
CLASSES = (
    ClassSpec(name="writing", description="写作协助类指令：代写、改写、文案、模板",
              examples=("帮我写一条请假条，明天上午要去医院",)),
    ClassSpec(name="qa", description="知识问答与解释类指令"),
    ClassSpec(name="other", description="不属于以上任何一类的指令"),
)

# Four-class table (three concrete + fallback) for k=3 fan-out coverage.
CLASSES4 = (
    ClassSpec(name="writing", description="写作协助"),
    ClassSpec(name="qa", description="知识问答"),
    ClassSpec(name="code", description="代码相关"),
    ClassSpec(name="other", description="其余"),
)


def make_cfg(*, modality="text", assignment="single", max_labels=None,
             instruction="", fallback_class="other", self_consistency=0,
             on_error="fallback", classes=CLASSES, trace=None,
             ui_tree_max_chars=30000) -> ResolvedConfig:
    if max_labels is None:
        max_labels = len(classes)          # mirror the M1 backfill (enabled ⇒ non-None)
    return ResolvedConfig(
        tool=ToolConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality=modality, input="in"),
        input=InputConfig(ui_tree_max_chars=ui_tree_max_chars),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(enabled=True, llm="default", assignment=assignment,
                                max_labels=max_labels, instruction=instruction,
                                fallback_class=fallback_class,
                                self_consistency=self_consistency,
                                on_error=on_error, classes=tuple(classes)),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(instruction="标注"),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=trace or TraceConfig(),
        rubric=Rubric(name="default:text", criteria=()),
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


def text_record(text="解释一下二分查找为什么是 O(log n)，能不能举个在通讯录里找人的例子",
                rid="fd97f67330e81315") -> Record:
    return Record(id=rid, modality="text", text=text, raw={"instruction": text},
                  ui_tree=None, image=None, ref=RecordRef("data.jsonl", 1, None, ()))


def ui_record() -> Record:
    nodes = (
        UINode("1", None, 0, "FrameLayout", "", "", (0, 0, 1080, 1920), True, {}),
        UINode("2", "1", 1, "Button", "登录", "", (72, 952, 1008, 1096), True, {}),
        UINode("3", "1", 1, "View", "", "", (0, 0, 0, 0), False, {}),
    )
    image = ImageRef(path=__import__("pathlib").Path("image_2.png"),
                     format="png", size_bytes=1234)
    return Record(id="9f2c31ab52e08d17", modality="ui", text=None, raw=None,
                  ui_tree=UITree(nodes), image=image,
                  ref=RecordRef("b/uitree_2.jsonl", None, 2, ()))


# ── in-process complete_validated stubs (no LLM, test_annotate 惯例) ─────────

class QueueEngine:
    """Pops queued outcomes in call order (single-record sc tests: gather order)."""

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
    """Keyed by record id (multi-record stage tests: scheduling-independent)."""

    def __init__(self, by_record):
        self.by_record = dict(by_record)
        self.calls: list = []

    async def complete_validated(self, profile, prompt, schema=None, *,
                                 record_ids=(), batch_no=0, record=None):
        self.calls.append((profile, prompt, schema, record_ids))
        out = self.by_record[record_ids[0]]
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
    out = asyncio.run(ClassifyStage(cfg).run(batch, ctx))
    return out, ctx


# ── prompt assembly (§10.8, deterministic) ──────────────────────────────────

def test_single_prompt_verbatim_no_reason():
    bundle = build_classify_prompt(text_record(), make_cfg(), with_reason=False)
    # system, one user message per configured class example, current record last
    assert [m.role for m in bundle.messages] == ["system", "user", "user"]
    assert bundle.messages[0].parts[0].text == (
        "你是数据分类员。阅读待分类数据，判断它属于以下类别中的哪一类。类别表：\n"
        "- writing: 写作协助类指令：代写、改写、文案、模板\n"
        "- qa: 知识问答与解释类指令\n"
        "- other: 不属于以上任何一类的指令\n"
        "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
        '{"class": <类名>}'
    )
    assert bundle.messages[1].parts[0].text == (
        "[类别示例·writing] 帮我写一条请假条，明天上午要去医院")
    assert bundle.messages[2].parts[0].text == (
        "[待分类数据] 解释一下二分查找为什么是 O(log n)，能不能举个在通讯录里找人的例子")
    assert bundle.temperature is None


def test_single_prompt_reason_fragment_when_requested():
    text = build_classify_prompt(text_record(), make_cfg(),
                                 with_reason=True).messages[0].parts[0].text
    assert text.endswith(
        "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
        '{"class": <类名>, "reason": <一句话理由>}')


def test_multi_prompt_head_and_structure_two_states():
    cfg = make_cfg(assignment="multi", max_labels=2)
    plain = build_classify_prompt(text_record(), cfg, with_reason=False)
    text = plain.messages[0].parts[0].text
    assert text.startswith(
        "你是数据分类员。阅读待分类数据，判断它适用于以下哪些类别"
        "（至少 1 类，至多 2 类）。类别表：\n")
    assert text.endswith(
        "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
        '{"classes": [<类名>, ...]}')
    with_reason = build_classify_prompt(text_record(), cfg, with_reason=True)
    assert with_reason.messages[0].parts[0].text.endswith(
        '{"classes": [<类名>, ...], "reason": <一句话理由>}')


def test_instruction_line_between_class_table_and_structure_sentence():
    cfg = make_cfg(instruction="宁可归入 other，不要猜测。")
    text = build_classify_prompt(text_record(), cfg, with_reason=False)
    assert ("- other: 不属于以上任何一类的指令\n"
            "宁可归入 other，不要猜测。\n"
            "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
            ) in text.messages[0].parts[0].text


def test_examples_expand_declaration_order_then_array_order():
    classes = (
        ClassSpec(name="a", description="甲类", examples=("a1", "a2")),
        ClassSpec(name="b", description="乙类"),
        ClassSpec(name="c", description="丙类", examples=("c1",)),
        ClassSpec(name="other", description="其余"),
    )
    bundle = build_classify_prompt(text_record(), make_cfg(classes=classes),
                                   with_reason=False)
    example_msgs = bundle.messages[1:-1]
    assert all(m.role == "user" and len(m.parts) == 1 for m in example_msgs)
    assert [m.parts[0].text for m in example_msgs] == [
        "[类别示例·a] a1", "[类别示例·a] a2", "[类别示例·c] c1"]
    assert bundle.messages[-1].parts[0].text.startswith("[待分类数据] ")


def test_ui_prompt_three_parts_in_one_user_message():
    cfg = make_cfg(modality="ui")
    rec = ui_record()
    bundle = build_classify_prompt(rec, cfg, with_reason=False)
    msg = bundle.messages[-1]
    assert msg.role == "user"
    assert [p.kind for p in msg.parts] == ["text", "image", "text"]
    assert msg.parts[0].text == "[屏幕截图]"
    assert msg.parts[1].image is rec.image
    assert msg.parts[2].text == ("[UI 控件树]\n"
                                 + rec.ui_tree.serialize(max_chars=30000))


# ── v1.8 sequence prompt variant (§10.8, spec 3.13.3 sequence row) ───────────

def seq_member_ui(idx: int) -> Record:
    nodes = (
        UINode("1", None, 0, "FrameLayout", "", "", (0, 0, 1080, 1920), True,
               {"package": "com.demo.food"}),
        UINode("2", "1", 1, "Button", f"步骤{idx}", "", (72, 952, 1008, 1096), True, {}),
    )
    image = ImageRef(path=__import__("pathlib").Path(f"image_{idx}.png"),
                     format="png", size_bytes=100 + idx)
    return Record(id=f"frame{idx:02d}", modality="ui", text=None, raw=None,
                  ui_tree=UITree(nodes), image=image,
                  ref=RecordRef(f"a/uitree_{idx}.jsonl", None, idx, ()))


def seq_record(members, rid="a3f1c2d4e5b60718") -> Record:
    """S24 sequence-record convention: text/raw/ui_tree/image = None, modality =
    the members' modality, ref inherited from the first member."""
    first = members[0]
    return Record(id=rid, modality=first.modality, text=None, raw=None, ui_tree=None,
                  image=None,
                  ref=RecordRef(first.ref.source_file, first.ref.line_no,
                                first.ref.pair_index, ()),
                  kind="sequence", members=tuple(members))


def test_sequence_prompt_ui_digest_lines_and_first_frame_screenshot():
    cfg = make_cfg(modality="ui")
    members = [seq_member_ui(1), seq_member_ui(2), seq_member_ui(3)]
    rec = seq_record(members)
    bundle = build_classify_prompt(rec, cfg, with_reason=False)
    # system and few-shot messages keep the single-record shape (spec 3.13.3)
    assert [m.role for m in bundle.messages] == ["system", "user", "user"]
    assert bundle.messages[1].parts[0].text.startswith("[类别示例·writing] ")
    msg = bundle.messages[-1]
    assert [p.kind for p in msg.parts] == ["text", "text", "image"]
    expected_lines = [f"{m}. {frame_digest(member, cfg.segment.digest_max_chars)}"
                      for m, member in enumerate(members, start=1)]
    assert msg.parts[0].text == "[待分类数据·序列]\n" + "\n".join(expected_lines)
    assert "truncated" not in msg.parts[0].text            # under the cap: no marker
    assert msg.parts[1].text == "[首帧截图]"
    assert msg.parts[2].image is members[0].image          # FIRST member's screenshot


def test_sequence_prompt_text_modality_digest_only():
    cfg = make_cfg()                                       # text modality
    members = [text_record("打开外卖应用", rid="s1"), text_record("搜索奶茶", rid="s2")]
    rec = seq_record(members)
    bundle = build_classify_prompt(rec, cfg, with_reason=False)
    msg = bundle.messages[-1]
    assert [p.kind for p in msg.parts] == ["text"]         # digest part only, no image
    assert msg.parts[0].text == "[待分类数据·序列]\n1. 打开外卖应用\n2. 搜索奶茶"


def test_sequence_prompt_truncation_keeps_first_and_last_members():
    cfg = make_cfg(ui_tree_max_chars=1000)
    texts = [f"m{i}" + "步" * (400 - len(f"m{i}")) for i in range(1, 6)]
    members = [text_record(t, rid=f"s{i}") for i, t in enumerate(texts, start=1)]
    rec = seq_record(members)
    bundle = build_classify_prompt(rec, cfg, with_reason=False)
    part = bundle.messages[-1].parts[0].text
    assert part.startswith("[待分类数据·序列]\n")
    body = part.removeprefix("[待分类数据·序列]\n")
    lines = body.splitlines()
    # First/last member lines always kept, whole middle lines dropped, the frozen
    # marker closes the block, and the body respects the ui_tree_max_chars cap.
    assert lines[0] == f"1. {texts[0]}"
    assert lines[-2] == f"5. {texts[4]}"
    assert lines[-1] == "…(truncated 3 members)"
    assert len(lines) == 3                                 # no middle member survived
    assert len(body) <= 1000


def test_stage_classifies_sequence_record_without_crash():
    # Zero-crash guarantee (spec 3.13.3): an episode rides the normal stage path —
    # the v1.7 UI branch would have raised AttributeError on ui_tree=None.
    cfg = make_cfg(modality="ui")
    rec = seq_record([seq_member_ui(1), seq_member_ui(2)])
    item = PipelineItem(record=rec)
    batch = [item]
    out, ctx = run_stage(cfg, batch, MapEngine({rec.id: {"class": "qa"}}))
    assert out is batch and len(batch) == 1
    assert item.status == "active"
    assert item.classification == Classification(label="qa", labels=("qa",),
                                                 source="llm", detail={})
    assert ctx.metrics.counters == {"classify.classes.qa": 1}


# ── reason request condition (R29) ──────────────────────────────────────────

def test_reason_requested_iff_trace_enabled_and_classify_channel():
    assert _reason_requested(make_cfg()) is False
    assert _reason_requested(make_cfg(
        trace=TraceConfig(enabled=True, channels=("quality", "verify")))) is False
    assert _reason_requested(make_cfg(
        trace=TraceConfig(enabled=False, channels=("classify",)))) is False
    assert _reason_requested(make_cfg(
        trace=TraceConfig(enabled=True, channels=("quality", "classify")))) is True


# ── normalization (post-M8, deterministic) ──────────────────────────────────

def test_normalize_maps_to_declaration_order_and_dedupes():
    c = make_cfg().classify
    assert _normalize_labels(("qa", "writing", "qa"), c) == ("writing", "qa")


def test_normalize_drops_fallback_cooccurring_with_concrete():
    c = make_cfg().classify
    assert _normalize_labels(("other", "qa"), c) == ("qa",)


def test_normalize_keeps_pure_fallback():
    c = make_cfg().classify
    assert _normalize_labels(("other", "other"), c) == ("other",)


# ── classify_record: plain path ─────────────────────────────────────────────

def test_plain_call_normalizes_and_uses_internal_schema():
    cfg = make_cfg()
    engine = QueueEngine([{"class": "qa"}])
    cls = asyncio.run(classify_record(text_record(), make_ctx(cfg, engine)))
    assert cls == Classification(label="qa", labels=("qa",), source="llm", detail={})
    profile, prompt, schema, record_ids = engine.calls[0]
    assert profile == "default"
    assert prompt.temperature is None        # plain call: profile default (temp 0)
    assert schema == classification_schema(["writing", "qa", "other"], "single",
                                           max_labels=3, with_reason=False)
    assert record_ids == (text_record().id,)


def test_plain_call_reason_lands_in_detail():
    cfg = make_cfg(trace=TraceConfig(enabled=True, channels=("classify",)))
    engine = QueueEngine([{"class": "qa", "reason": "属于知识解释"}])
    cls = asyncio.run(classify_record(text_record(), make_ctx(cfg, engine)))
    assert cls.detail == {"reason": "属于知识解释"}
    schema = engine.calls[0][2]
    assert "reason" in schema["properties"] and "reason" in schema["required"]


# ── classify_record: self-consistency voting (R26, own rules) ───────────────

def test_sc_single_majority_wins():
    cfg = make_cfg(self_consistency=3)
    engine = QueueEngine([{"class": "qa"}, {"class": "qa"}, {"class": "writing"}])
    cls = asyncio.run(classify_record(text_record(), make_ctx(cfg, engine)))
    assert (cls.label, cls.labels, cls.source) == ("qa", ("qa",), "llm")
    assert cls.detail["sc"] == {"n": 3, "agreement_ratio": 2 / 3}
    assert len(engine.calls) == 3
    # sc samples run at classify.sc_temperature
    assert all(call[1].temperature == cfg.classify.sc_temperature
               for call in engine.calls)


def test_sc_single_no_majority_goes_to_fallback():
    cfg = make_cfg(self_consistency=3)
    engine = QueueEngine([{"class": "qa"}, {"class": "writing"}, {"class": "other"}])
    cls = asyncio.run(classify_record(text_record(), make_ctx(cfg, engine)))
    assert (cls.label, cls.labels) == ("other", ("other",))
    assert cls.source == "llm"                    # vote outcome, not the error path
    assert cls.detail["sc"] == {"n": 3, "agreement_ratio": 1 / 3}


def test_sc_abstention_keeps_denominator_n():
    # n=5, two samples abstain (SchemaViolation): qa=3 > 5/2 still wins, share 3/5.
    cfg = make_cfg(self_consistency=5)
    violation = SchemaViolation(["/class: 枚举违规"], "{}")
    engine = QueueEngine([{"class": "qa"}, violation, {"class": "qa"},
                          violation, {"class": "qa"}])
    cls = asyncio.run(classify_record(text_record(), make_ctx(cfg, engine)))
    assert (cls.label, cls.labels) == ("qa", ("qa",))
    assert cls.detail["sc"] == {"n": 5, "agreement_ratio": 3 / 5}


def test_sc_abstention_can_break_majority():
    # n=5, qa=2 valid votes only: 2 not > 5/2 (denominator stays n) → fallback.
    cfg = make_cfg(self_consistency=5)
    violation = SchemaViolation(["/class: 枚举违规"], "{}")
    engine = QueueEngine([{"class": "qa"}, {"class": "qa"},
                          violation, violation, violation])
    cls = asyncio.run(classify_record(text_record(), make_ctx(cfg, engine)))
    assert (cls.label, cls.labels) == ("other", ("other",))
    assert cls.detail["sc"] == {"n": 5, "agreement_ratio": 0.0}


def test_sc_all_samples_fail_raises_schema_violation():
    cfg = make_cfg(self_consistency=3)
    violation = SchemaViolation(["/class: 枚举违规"], "{}")
    engine = QueueEngine([violation, violation, violation])
    with pytest.raises(SchemaViolation):
        asyncio.run(classify_record(text_record(), make_ctx(cfg, engine)))


def test_sc_multi_per_label_majority_and_lowest_share():
    cfg = make_cfg(assignment="multi", self_consistency=3)
    engine = QueueEngine([
        {"classes": ["writing", "qa"]},
        {"classes": ["qa", "writing"]},           # sample order irrelevant
        {"classes": ["qa"]},
    ])
    cls = asyncio.run(classify_record(text_record(), make_ctx(cfg, engine)))
    # votes: writing=2 > 3/2, qa=3 → kept in declaration order
    assert (cls.label, cls.labels) == ("writing", ("writing", "qa"))
    assert cls.detail["sc"] == {"n": 3, "agreement_ratio": 2 / 3}   # min kept share


def test_sc_multi_all_labels_fall_out_goes_to_fallback():
    cfg = make_cfg(assignment="multi", self_consistency=3)
    engine = QueueEngine([{"classes": ["writing"]}, {"classes": ["qa"]},
                          {"classes": ["other"]}])
    cls = asyncio.run(classify_record(text_record(), make_ctx(cfg, engine)))
    assert (cls.label, cls.labels) == ("other", ("other",))
    assert cls.detail["sc"] == {"n": 3, "agreement_ratio": 1 / 3}


def test_sc_multi_normalizes_each_sample_before_voting():
    cfg = make_cfg(assignment="multi", self_consistency=3)
    engine = QueueEngine([
        {"classes": ["other", "writing"]},        # ② drops the co-occurring fallback
        {"classes": ["writing", "writing"]},      # ① dedupe: one membership
        {"classes": ["qa"]},
    ])
    cls = asyncio.run(classify_record(text_record(), make_ctx(cfg, engine)))
    # votes: writing=2 kept, qa=1 out, other=0 (normalized away)
    assert (cls.label, cls.labels) == ("writing", ("writing",))


# ── stage: happy path, events, counters ─────────────────────────────────────

def test_stage_single_writes_classification_and_never_fans_out():
    cfg = make_cfg()
    rec = text_record()
    item = PipelineItem(record=rec)
    batch = [item]
    out, ctx = run_stage(cfg, batch, MapEngine({rec.id: {"class": "qa"}}))
    assert out is batch and len(batch) == 1
    assert item.classification == Classification(label="qa", labels=("qa",),
                                                 source="llm", detail={})
    assert ctx.metrics.counters == {"classify.classes.qa": 1}
    (ev, stage, record_ids, payload), = ctx.metrics.events
    assert (ev, stage, record_ids) == ("classify.decision", "classify", (rec.id,))
    assert payload == {"label": "qa", "source": "llm"}   # single: no "labels" key


def test_stage_multi_fan_out_k3_clones_share_refs_with_fresh_containers():
    cfg = make_cfg(assignment="multi", classes=CLASSES4, max_labels=4)
    rec = text_record()
    item = PipelineItem(record=rec, session_id="sess-0042",
                        dedup=DedupInfo(kind="unique", cluster_key="k1", kept_id=None))
    batch = [item]
    # raw order scrambled: normalization maps onto declaration order
    out, ctx = run_stage(cfg, batch,
                         MapEngine({rec.id: {"classes": ["code", "qa", "writing"]}}))
    assert out is batch                                # same list object (contract ②a)
    assert len(batch) == 3 and batch[0] is item
    assert item.classification.label == "writing"      # original takes the FIRST label
    assert item.classification.labels == ("writing", "qa", "code")
    clones = batch[1:]
    assert [c.classification.label for c in clones] == ["qa", "code"]
    for clone in clones:
        assert clone.record is item.record             # shared by reference
        assert clone.dedup is item.dedup
        assert clone.session_id == "sess-0042"         # inherited (v1.8, spec 3.13.4)
        assert clone.status == "active"
        assert clone.classification.labels == ("writing", "qa", "code")
        assert clone.classification.source == "llm"
        assert clone.annotation is None and clone.verification is None
        assert clone.scores == {} and clone.scores is not item.scores
        assert clone.errors == [] and clone.errors is not item.errors
    # container independence between the siblings themselves
    assert clones[0].scores is not clones[1].scores
    item.errors.append("sentinel")
    assert clones[0].errors == [] and clones[1].errors == []
    # counters: per label + multi_label_records; decision event carries the full set
    assert ctx.metrics.counters == {
        "classify.classes.writing": 1, "classify.classes.qa": 1,
        "classify.classes.code": 1, "classify.multi_label_records": 1}
    (ev, _, record_ids, payload), = ctx.metrics.events
    assert ev == "classify.decision" and record_ids == (rec.id,)
    assert payload == {"label": "writing", "labels": ["writing", "qa", "code"],
                       "source": "llm"}


def test_stage_multi_fan_out_clones_inherit_episode_marks():
    """D6: session_split / segment_degraded describe the EPISODE's session and
    segmentation, not the envelope — sibling rows must not contradict the
    original's _meta.stream."""
    cfg = make_cfg(assignment="multi", classes=CLASSES4, max_labels=4)
    rec = text_record()
    item = PipelineItem(record=rec, session_id="sess-0042")
    item.session_split = True
    item.segment_degraded = {"kind": "segmentation_invalid", "windows_failed": 1}
    batch = [item]
    run_stage(cfg, batch, MapEngine({rec.id: {"classes": ["code", "qa"]}}))
    (clone,) = batch[1:]
    assert clone.session_split is True
    assert clone.segment_degraded == {"kind": "segmentation_invalid",
                                      "windows_failed": 1}
    # unmarked originals stay unmarked on the clone (getattr default path)
    plain = PipelineItem(record=text_record(rid="rec9", text="另一条"))
    batch2 = [plain]
    run_stage(cfg, batch2, MapEngine({"rec9": {"classes": ["code", "qa"]}}))
    (clone2,) = batch2[1:]
    assert not hasattr(clone2, "session_split")
    assert not hasattr(clone2, "segment_degraded")


def test_stage_multi_append_order_batch_position_then_declaration():
    cfg = make_cfg(assignment="multi", classes=CLASSES4, max_labels=4)
    r1, r2 = text_record(rid="rec1"), text_record(rid="rec2", text="另一条")
    i1, i2 = PipelineItem(record=r1), PipelineItem(record=r2)
    batch = [i1, i2]
    out, _ = run_stage(cfg, batch, MapEngine({
        "rec1": {"classes": ["qa", "writing"]},
        "rec2": {"classes": ["code", "qa"]},
    }))
    assert out is batch and len(batch) == 4
    # originals in place, clones appended (batch position → label declaration order)
    assert [(it.record.id, it.classification.label) for it in batch] == [
        ("rec1", "writing"), ("rec2", "qa"), ("rec1", "qa"), ("rec2", "code")]


def test_stage_multi_single_hit_does_not_fan_out():
    cfg = make_cfg(assignment="multi")
    rec = text_record()
    batch = [PipelineItem(record=rec)]
    out, ctx = run_stage(cfg, batch, MapEngine({rec.id: {"classes": ["qa", "qa"]}}))
    assert len(out) == 1
    assert out[0].classification.labels == ("qa",)
    assert "classify.multi_label_records" not in ctx.metrics.counters


# ── stage: idempotency + non-active ─────────────────────────────────────────

def test_stage_skips_items_with_existing_classification():
    cfg = make_cfg(assignment="multi", classes=CLASSES4, max_labels=4)
    inherited = Classification(label="qa", labels=("qa",), source="inherited",
                               detail={})
    item = PipelineItem(record=text_record(), classification=inherited)
    batch = [item]
    out, ctx = run_stage(cfg, batch, ExplodingEngine())   # no LLM call happens
    assert out is batch and len(batch) == 1
    assert item.classification is inherited
    assert ctx.metrics.events == [] and ctx.metrics.counters == {}


def test_stage_ignores_non_active_items():
    cfg = make_cfg()
    item = PipelineItem(record=text_record(), status="dropped_dup")
    out, ctx = run_stage(cfg, [item], ExplodingEngine())
    assert item.classification is None
    assert item.status == "dropped_dup"
    assert ctx.metrics.events == [] and ctx.metrics.counters == {}


# ── stage: on_error two paths (R4) ──────────────────────────────────────────

def test_on_error_fallback_keeps_record_active_without_item_errors():
    cfg = make_cfg()                                   # on_error="fallback" default
    rec = text_record()
    item = PipelineItem(record=rec)
    violation = SchemaViolation(["/class: 枚举违规"], '{"class": "nope"}')
    out, ctx = run_stage(cfg, [item], MapEngine({rec.id: violation}))
    assert item.status == "active"
    assert item.errors == []                           # R4: evidence NOT in item.errors
    cls = item.classification
    assert (cls.label, cls.labels, cls.source) == ("other", ("other",), "fallback")
    assert cls.detail["kind"] == "classification_invalid"
    assert "枚举违规" in cls.detail["message"]
    assert ctx.metrics.counters == {"classify.fallback": 1,
                                    "classify.classes.other": 1}
    error_events = [e for e in ctx.metrics.events if e[0] == "error"]
    assert len(error_events) == 1
    assert error_events[0][3] == {"stage": "classify", "kind": "classification_invalid",
                                  "message": '/class: 枚举违规', "retryable": False}
    decisions = [e for e in ctx.metrics.events if e[0] == "classify.decision"]
    assert len(decisions) == 1                         # decision event 照发
    assert decisions[0][3] == {"label": "other", "source": "fallback"}


def test_on_error_fail_marks_failed_with_stage_error():
    cfg = make_cfg(on_error="fail")
    rec = text_record()
    item = PipelineItem(record=rec)
    violation = SchemaViolation(["/class: 枚举违规"], '{"class": "nope"}')
    out, ctx = run_stage(cfg, [item], MapEngine({rec.id: violation}))
    assert item.status == "failed"
    assert item.classification is None
    (err,) = item.errors
    assert (err.stage, err.kind, err.retryable) == ("classify",
                                                    "classification_invalid", False)
    assert item.raw_last_output == '{"class": "nope"}'  # rejects "full" tier channel
    assert ctx.metrics.counters == {"classify.failures": 1}
    assert [e[0] for e in ctx.metrics.events] == ["error"]   # no decision event


def test_provider_retryable_exhausted_fails_item():
    cfg = make_cfg()                                   # fallback policy does NOT apply
    rec = text_record()
    item = PipelineItem(record=rec)
    exc = ProviderRetryableError("timeout", profile="default", retries=5)
    out, ctx = run_stage(cfg, [item], MapEngine({rec.id: exc}))
    assert item.status == "failed"
    assert item.classification is None
    (err,) = item.errors
    assert (err.kind, err.retryable) == ("provider_retryable_exhausted", True)
    assert ctx.metrics.counters == {"classify.failures": 1}

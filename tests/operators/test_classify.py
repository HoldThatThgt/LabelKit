"""Offline unit tests for M13 classify: prompt assembly (spec 3.13.3 / CONTRACTS §10.8),
post-M8 normalization, self-consistency voting (R26 own rules), the on_error two-path
policy (R4), and multi fan-out (contract ②a); v1.12 帧级批量判决（SPEC-frame-annotation
§3.2）——§10.12 装配、零重叠分窗、对齐后校验、全窗 fallback、执行门与扇出共享。
Pure logic only — no LLM: the schema engine is replaced by the in-process
complete_validated stubs (test_annotate 惯例)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from labelkit.operators.classify import (
    ClassifyStage,
    _normalize_labels,
    _reason_requested,
    build_classify_prompt,
    build_frame_classify_prompt,
    classify_frames,
    classify_record,
)
from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ClassSpec,
    ConsoleConfig,
    DedupConfig,
    ExtractConfig,
    FrameClassifyConfig,
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
from labelkit.common.runtime.schema_engine import (
    classification_schema,
    frame_classify_schema,
)
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
        console=ConsoleConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality=modality, input="in"),
        input=InputConfig(ui_tree_max_chars=ui_tree_max_chars),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(),
        stitch=StitchConfig(),
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

    async def complete_validated(self, profile, prompt, schema=None, *, scope):
        record_ids = scope.record_ids
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

    async def complete_validated(self, profile, prompt, schema=None, *, scope):
        record_ids = scope.record_ids
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


def test_stage_multi_fan_out_clones_inherit_stitch_marks():
    """v1.9 (T14): thread_id rides the clone constructor (a REAL field) and the
    three M16 duck marks join the copy loop — a sibling must extract its own
    seam placeholders and render the same _meta.stream.fragments."""
    cfg = make_cfg(assignment="multi", classes=CLASSES4, max_labels=4)
    rec = text_record()
    item = PipelineItem(record=rec, session_id="sess-0042", thread_id=rec.id)
    item.seam_indexes = (1,)
    item.seam_interrupted_by = (("打车",),)
    item.stitch_fragments = ({"order_span": [0, 1], "member_count": 2,
                              "cause": "origin", "source_episode": rec.id},)
    batch = [item]
    run_stage(cfg, batch, MapEngine({rec.id: {"classes": ["code", "qa"]}}))
    (clone,) = batch[1:]
    assert clone.thread_id == rec.id
    assert clone.seam_indexes == (1,)
    assert clone.seam_interrupted_by == (("打车",),)
    assert clone.stitch_fragments == item.stitch_fragments
    # unmarked originals leave the clone unmarked (thread_id stays None)
    plain = PipelineItem(record=text_record(rid="rec8", text="又一条"))
    batch2 = [plain]
    run_stage(cfg, batch2, MapEngine({"rec8": {"classes": ["code", "qa"]}}))
    (clone2,) = batch2[1:]
    assert clone2.thread_id is None
    assert not hasattr(clone2, "seam_indexes")
    assert not hasattr(clone2, "stitch_fragments")


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


# ── v1.11 context-budget packing (spec 3.13.4 上下文预算装填 row, V27①) ───────

def budget_cfg(context_window: int, **kw) -> ResolvedConfig:
    """make_cfg + a budget-declared [llm.default] profile (cw=0 = budget OFF)."""
    from dataclasses import replace
    from labelkit.common.config.model import LLMProfile
    prof = LLMProfile(name="default", provider="openai_compatible",
                      base_url="http://x", model="m", api_key_env="K",
                      max_output_tokens=256, context_window=context_window)
    return replace(make_cfg(**kw), llm_profiles={"default": prof})


class _FixedCalibrator:
    def __init__(self, value: int):
        self.value = value

    def cost(self, profile: str) -> int:
        return self.value


def budget_ctx(cfg, engine, image_cost: int = 100) -> SimpleNamespace:
    return SimpleNamespace(cfg=cfg, llm=SimpleNamespace(calibrator=_FixedCalibrator(image_cost)),
                           schema_engine=engine, metrics=RecordingMetrics(),
                           rng=None, batch_no=1)


def big_ui_record(n: int = 80) -> Record:
    nodes = [UINode("1", None, 0, "FrameLayout", "", "", (0, 0, 1080, 1920), True, {})]
    nodes += [UINode(str(i + 2), "1", 1, "TextView", "文本行" + "字" * 20, "",
                     (0, i, 1080, i + 10), True, {}) for i in range(n)]
    image = ImageRef(path=__import__("pathlib").Path("image_9.png"),
                     format="png", size_bytes=1)
    return Record(id="a" * 16, modality="ui", text=None, raw=None,
                  ui_tree=UITree(tuple(nodes)), image=image,
                  ref=RecordRef("b/uitree_9.jsonl", None, 9, ()))


async def test_budget_dynamic_tree_cap_trims_with_node_marker():
    from labelkit.common.runtime import budget as budget_mod

    cfg = budget_cfg(1200, modality="ui")
    rec = big_ui_record()
    item = PipelineItem(record=rec)
    engine = MapEngine({rec.id: {"class": "qa"}})
    ctx = budget_ctx(cfg, engine, image_cost=100)
    await ClassifyStage(cfg).run([item], ctx)
    assert item.status == "active"
    (call,) = engine.calls
    tree_part = call[1].messages[-1].parts[2].text
    body = tree_part.removeprefix("[UI 控件树]\n")
    # trailing-line drop with the serialize-family marker in place
    assert body.split("\n")[-1].startswith("…(truncated ")
    assert body.split("\n")[-1].endswith(" nodes)")
    full = rec.ui_tree.serialize(max_chars=cfg.input.ui_tree_max_chars)
    assert body != full and body.split("\n")[0] == full.split("\n")[0]
    assert ctx.metrics.counters["budget.truncations.classify"] == 1
    # the whole prompt honours the throat invariant (same estimator, V16)
    prof = cfg.llm_profiles["default"]
    est = budget_mod.est_prompt(call[1], prof, None, image_cost=100)
    assert est <= budget_mod.input_budget(prof)

    # determinism: an identical re-run builds the identical prompt
    engine2 = MapEngine({rec.id: {"class": "qa"}})
    ctx2 = budget_ctx(cfg, engine2, image_cost=100)
    await ClassifyStage(cfg).run([PipelineItem(record=big_ui_record())], ctx2)
    assert engine2.calls[0][1] == call[1]


def test_budget_off_prompt_byte_identical():
    # cw == 0 → the packing layer is dead code: the assembled prompt equals the
    # frozen public builder's output byte-for-byte (§1 byte-equivalence anchor).
    rec = big_ui_record()
    off_cfg = budget_cfg(0, modality="ui")
    anchor = build_classify_prompt(rec, make_cfg(modality="ui"), with_reason=False)
    item = PipelineItem(record=rec)
    engine = MapEngine({rec.id: {"class": "qa"}})
    ctx = budget_ctx(off_cfg, engine)
    asyncio.run(ClassifyStage(off_cfg).run([item], ctx))
    assert engine.calls[0][1] == anchor
    assert not any(k.startswith("budget.") for k in ctx.metrics.counters)


def test_budget_sequence_digest_body_trims_same_family():
    cfg = budget_cfg(1000)                             # text modality, no images
    members = [text_record("步骤" + "字" * 120, rid=f"s{i}") for i in range(1, 9)]
    rec = seq_record(members)
    item = PipelineItem(record=rec)
    engine = MapEngine({rec.id: {"class": "qa"}})
    ctx = budget_ctx(cfg, engine)
    asyncio.run(ClassifyStage(cfg).run([item], ctx))
    assert item.status == "active"
    (call,) = engine.calls
    body = call[1].messages[-1].parts[0].text.removeprefix("[待分类数据·序列]\n")
    lines = body.split("\n")
    assert lines[0].startswith("1. ")                  # first member kept
    assert lines[-2].startswith("8. ")                 # last member kept
    assert lines[-1].startswith("…(truncated ") and lines[-1].endswith(" members)")
    assert ctx.metrics.counters["budget.truncations.classify"] == 1


def test_budget_minimal_unit_unfittable_fails_record_no_call():
    # V10: even the single record cannot fit (huge calibrated image cost) — the
    # doomed request is never sent; kind=context_overflow → rejects, counted in
    # budget.overflow_records; precheck never feeds the breaker.
    cfg = budget_cfg(1200, modality="ui")
    rec = big_ui_record()
    item = PipelineItem(record=rec)
    ctx = budget_ctx(cfg, ExplodingEngine(), image_cost=10_000)
    asyncio.run(ClassifyStage(cfg).run([item], ctx))
    assert item.status == "failed"
    assert item.errors[0].kind == "context_overflow"
    assert ctx.metrics.counters["budget.overflow_records"] == 1
    error_events = [e for e in ctx.metrics.events if e[0] == "error"]
    assert error_events and error_events[0][3]["kind"] == "context_overflow"


def test_classifier_branch_context_overflow_and_output_truncated():
    # V27①: the budget vocabulary routes FIRST in the per-record classifier —
    # never internal_error; overflow rejects count budget.overflow_records.
    from labelkit.common.errors import ContextOverflowError, OutputTruncatedError

    cfg = make_cfg()
    rec = text_record()
    item = PipelineItem(record=rec)
    exc = ContextOverflowError("over", phase="precheck")
    out, ctx = run_stage(cfg, [item], MapEngine({rec.id: exc}))
    assert item.status == "failed"
    assert item.errors[0].kind == "context_overflow"
    assert ctx.metrics.counters["budget.overflow_records"] == 1

    item2 = PipelineItem(record=rec)
    out, ctx2 = run_stage(cfg, [item2],
                          MapEngine({rec.id: OutputTruncatedError("cap")}))
    assert item2.status == "failed"
    assert item2.errors[0].kind == "output_truncated"
    assert "budget.overflow_records" not in ctx2.metrics.counters


def test_reactive_400_terminal_feeds_breaker_exactly_once():
    # A7/§7.8 matrix: reactive-400 → the owning operator feeds the fatal streak
    # exactly once; the 200-shaped finish oracle (origin="finish") never feeds.
    from labelkit.common.errors import ContextOverflowError

    class FeedMetrics(RecordingMetrics):
        def __init__(self):
            super().__init__()
            self.fed: list = []

        def record_provider_result(self, fatal, *, hard=False):
            self.fed.append((fatal, hard))

    cfg = make_cfg()
    rec = text_record()

    exc = ContextOverflowError("sniff hit", phase="reactive")   # origin defaults http_400
    item = PipelineItem(record=rec)
    ctx = SimpleNamespace(cfg=cfg, llm=None, schema_engine=MapEngine({rec.id: exc}),
                          metrics=FeedMetrics(), rng=None, batch_no=1)
    asyncio.run(ClassifyStage(cfg).run([item], ctx))
    assert item.errors[0].kind == "context_overflow"
    assert ctx.metrics.fed == [(True, False)]

    exc200 = ContextOverflowError("finish oracle", phase="reactive")
    exc200.origin = "finish"                                    # 200-shaped: never fed
    item2 = PipelineItem(record=rec)
    ctx2 = SimpleNamespace(cfg=cfg, llm=None, schema_engine=MapEngine({rec.id: exc200}),
                           metrics=FeedMetrics(), rng=None, batch_no=1)
    asyncio.run(ClassifyStage(cfg).run([item2], ctx2))
    assert item2.errors[0].kind == "context_overflow"
    assert ctx2.metrics.fed == []


# ── v1.12 帧级批量判决（SPEC-frame-annotation §3.2，CONTRACTS §10.12） ────────

FRAME_CLASSES = (
    ClassSpec(name="task_request", description="发起一项新任务的请求"),
    ClassSpec(name="followup", description="对进行中任务的补充或跟进"),
    ClassSpec(name="chitchat", description="与任务无关的闲聊"),
    ClassSpec(name="other", description="其余"),
)

FRAME_NAMES = [c.name for c in FRAME_CLASSES]


def frame_cfg(*, context_window=0, frame_enabled=True, vision_resolved=False,
              fallback_class="other", **kw) -> ResolvedConfig:
    """make_cfg/budget_cfg + 帧级分类配置（vision_resolved 为 M1 解析产物——
    单测按产物形态直接注入，segment.vision_resolved 测试同款手法）。"""
    from dataclasses import replace
    base = budget_cfg(context_window, **kw) if context_window else make_cfg(**kw)
    return replace(base, frame_classify=FrameClassifyConfig(
        enabled=frame_enabled, llm="default", fallback_class=fallback_class,
        classes=FRAME_CLASSES, vision_resolved=vision_resolved))


class FrameStageEngine:
    """帧级测试引擎：schema 带 labels 键 ⇒ 帧窗调用（frame_outcomes 队列优先出货/
    抛出，队列空则按窗长自动应答 frame_label × n），单独记入 frame_calls；
    其余 ⇒ 序列级判决按 record id 出货（MapEngine 形态）。"""

    def __init__(self, by_record, frame_label="task_request", frame_outcomes=None):
        self.by_record = dict(by_record)
        self.frame_label = frame_label
        self.frame_outcomes = list(frame_outcomes or [])
        self.calls: list = []              # (profile, prompt, schema, record_ids)
        self.frame_calls: list = []

    async def complete_validated(self, profile, prompt, schema=None, *, scope):
        record_ids = scope.record_ids
        self.calls.append((profile, prompt, schema, record_ids))
        if schema is not None and "labels" in schema.get("properties", {}):
            self.frame_calls.append((profile, prompt, schema, record_ids))
            if self.frame_outcomes:
                out = self.frame_outcomes.pop(0)
                if isinstance(out, Exception):
                    raise out
                return out, Usage(), 1, "glm-5.2"
            n = schema["properties"]["labels"]["minItems"]
            return {"labels": [self.frame_label] * n}, Usage(), 1, "glm-5.2"
        out = self.by_record[record_ids[0]]
        if isinstance(out, Exception):
            raise out
        return out, Usage(), 1, "glm-5.2"


def frame_members(n: int, text="步骤") -> list[Record]:
    return [text_record(f"{text}{i}", rid=f"fm{i}") for i in range(1, n + 1)]


# ── prompt 装配（§10.12：确定性模板 + vision 形态 + fit 尾参） ────────────────

def test_frame_prompt_verbatim_text():
    cfg = frame_cfg()
    members = [text_record("打开外卖应用", rid="fm1"), text_record("搜索奶茶", rid="fm2")]
    digests = [frame_digest(m, cfg.segment.digest_max_chars) for m in members]
    bundle = build_frame_classify_prompt(members, cfg, digests)
    assert [m.role for m in bundle.messages] == ["system", "user"]
    assert bundle.messages[0].parts[0].text == (
        "[任务]\n"
        "你是数据流的逐帧分类员。下面给出同一会话中按时间顺序排列的 2 帧成员摘要，"
        "对每一帧独立判断它属于以下类别中的哪一类，只能从以下封闭类别表中取恰一值。类别表：\n"
        "- task_request: 发起一项新任务的请求\n"
        "- followup: 对进行中任务的补充或跟进\n"
        "- chitchat: 与任务无关的闲聊\n"
        "- other: 其余\n"
        "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
        '{"labels": [<第 1 帧类名>, <第 2 帧类名>, ...]}（恰 2 项，按帧序与成员摘要行对齐）'
    )
    (part,) = bundle.messages[1].parts
    assert part.text == "[会话成员帧]\n1. 打开外卖应用\n2. 搜索奶茶"
    assert bundle.temperature is None


def test_frame_prompt_vision_appends_member_screenshot_parts():
    cfg = frame_cfg(modality="ui", vision_resolved=True)
    members = [seq_member_ui(1), seq_member_ui(2)]
    digests = [frame_digest(m, cfg.segment.digest_max_chars) for m in members]
    bundle = build_frame_classify_prompt(members, cfg, digests)
    msg = bundle.messages[-1]
    assert [p.kind for p in msg.parts] == ["text", "text", "image", "text", "image"]
    assert msg.parts[0].text.startswith("[会话成员帧]\n1. ")
    assert msg.parts[1].text == "[成员 1 截图]"
    assert msg.parts[2].image is members[0].image
    assert msg.parts[3].text == "[成员 2 截图]"
    assert msg.parts[4].image is members[1].image
    # vision_resolved=False（成本控制面 = 指向纯文本 profile）⇒ 摘要行 only
    off = frame_cfg(modality="ui", vision_resolved=False)
    plain = build_frame_classify_prompt(members, off, digests)
    assert [p.kind for p in plain.messages[-1].parts] == ["text"]


def test_frame_assemble_fit_checks_whole_prompt_and_keeps_bytes():
    from labelkit.operators.classify import _PromptFit, _assemble_frame_classify

    cfg = frame_cfg()
    members = [text_record("字" * 200, rid="fm1")]
    digests = ["字" * 200]
    tight = _PromptFit(input_budget=50, image_cost=0)
    bundle = _assemble_frame_classify(members, cfg, digests, fit=tight)
    assert tight.overflow is True                      # 最小单元不可装填 → precheck 跳过位
    roomy = _PromptFit(input_budget=100_000, image_cost=0)
    bundle2 = _assemble_frame_classify(members, cfg, digests, fit=roomy)
    assert roomy.overflow is False
    # fit 尾参不改字节：公开面与 fit 路径同篇（「公开面冻结 + 私有 fit 尾参」形态）
    assert bundle == bundle2 == build_frame_classify_prompt(members, cfg, digests)


# ── classify_frames：位次对齐后校验（缺项 fallback / 超长截断） ───────────────

def test_classify_frames_single_window_maps_labels_positionally():
    cfg = frame_cfg()
    members = frame_members(3)
    engine = QueueEngine([{"labels": ["task_request", "followup", "chitchat"]}])
    ctx = make_ctx(cfg, engine)
    result = asyncio.run(classify_frames(members, ctx))
    assert set(result) == {"fm1", "fm2", "fm3"}
    assert result["fm1"] == Classification(label="task_request",
                                           labels=("task_request",),
                                           source="llm", detail={})
    assert [result[f"fm{i}"].label for i in (1, 2, 3)] == [
        "task_request", "followup", "chitchat"]
    profile, _prompt, schema, record_ids = engine.calls[0]
    assert profile == "default"                        # cfg.frame_classify.llm
    assert schema == frame_classify_schema(FRAME_NAMES, 3)
    assert record_ids == ("fm1",)                      # 公开面缺省归属 = 首成员 id
    assert ctx.metrics.counters == {"frame_classify.calls": 1}


def test_classify_frames_short_array_pads_missing_with_fallback():
    cfg = frame_cfg()
    members = frame_members(3)
    engine = QueueEngine([{"labels": ["task_request", "followup"]}])   # 缺第 3 项
    ctx = make_ctx(cfg, engine)
    result = asyncio.run(classify_frames(members, ctx))
    assert result["fm1"].source == "llm" and result["fm2"].source == "llm"
    assert result["fm3"] == Classification(label="other", labels=("other",),
                                           source="fallback", detail={})
    assert ctx.metrics.counters == {"frame_classify.calls": 1,
                                    "frame_classify.fallback": 1}


def test_classify_frames_overlong_array_truncates_keeping_head():
    cfg = frame_cfg()
    members = frame_members(2)
    engine = QueueEngine([{"labels": ["followup", "chitchat", "task_request",
                                      "other"]}])
    ctx = make_ctx(cfg, engine)
    result = asyncio.run(classify_frames(members, ctx))
    assert [result[f"fm{i}"].label for i in (1, 2)] == ["followup", "chitchat"]
    assert all(c.source == "llm" for c in result.values())
    assert "frame_classify.fallback" not in ctx.metrics.counters


# ── classify_frames：全窗 fallback 语义（修复穷尽/不可恢复，永不 episode failed）─

def test_classify_frames_window_failure_falls_back_whole_window():
    cfg = frame_cfg()
    members = frame_members(3)
    engine = QueueEngine([SchemaViolation(["/labels: 枚举违规"], "{}")])
    ctx = make_ctx(cfg, engine)
    result = asyncio.run(classify_frames(members, ctx))
    assert all(c.label == "other" and c.labels == ("other",)
               and c.source == "fallback" for c in result.values())
    assert all(c.detail["kind"] == "classification_invalid"
               for c in result.values())               # R4 留痕：kind/message 入 detail
    assert ctx.metrics.counters == {"frame_classify.calls": 1,
                                    "frame_classify.window_failures": 1,
                                    "frame_classify.fallback": 3}


# ── classify_frames：分窗（预算开/关 + 零重叠） ──────────────────────────────

def test_classify_frames_budget_off_single_window_all_members():
    cfg = frame_cfg()                                  # llm_profiles={} ⇒ 预算关
    members = frame_members(8)
    engine = FrameStageEngine({})
    ctx = make_ctx(cfg, engine)
    result = asyncio.run(classify_frames(members, ctx))
    assert len(engine.frame_calls) == 1                # 单窗全成员
    assert engine.frame_calls[0][2]["properties"]["labels"]["minItems"] == 8
    assert len(result) == 8


def test_classify_frames_budget_packs_zero_overlap_windows():
    cfg = frame_cfg(context_window=2000)
    members = [text_record("字" * 400, rid=f"fm{i:02d}") for i in range(12)]
    engine = FrameStageEngine({})
    ctx = budget_ctx(cfg, engine)
    result = asyncio.run(classify_frames(members, ctx))
    assert len(engine.frame_calls) >= 2                # 预算确实切了窗
    # 零重叠去重叠形：各窗长度之和 == 成员总数（pack_windows 原生跨度带 1 帧重叠，
    # 帧分类调用形自第二窗起丢弃重叠首帧），拼接摘要行 == 全序列恰一次按序覆盖。
    sizes = [c[2]["properties"]["labels"]["minItems"] for c in engine.frame_calls]
    assert sum(sizes) == 12
    seen: list[str] = []
    for call in engine.frame_calls:
        body = call[1].messages[-1].parts[0].text.removeprefix("[会话成员帧]\n")
        lines = body.split("\n")
        assert lines[0].startswith("1. ")              # 窗内序号 1-based 重编
        assert len(lines) == call[2]["properties"]["labels"]["minItems"]
        seen.extend(line.split(". ", 1)[1] for line in lines)
    assert seen == ["字" * 400] * 12
    assert set(result) == {f"fm{i:02d}" for i in range(12)}
    assert all(c.source == "llm" for c in result.values())
    assert ctx.metrics.counters["frame_classify.calls"] == len(engine.frame_calls)


# ── classify_frames：反应式溢出对半降级（V20 镜像）与 precheck 纪律 ───────────

def test_classify_frames_reactive_overflow_halves_then_succeeds():
    from labelkit.common.errors import ContextOverflowError

    cfg = frame_cfg(context_window=100_000)
    overflow = ContextOverflowError("prompt too long", phase="reactive")
    overflow.origin = "finish"                         # 200 形终局：永不喂熔断
    engine = FrameStageEngine({}, frame_outcomes=[overflow])
    ctx = budget_ctx(cfg, engine)
    result = asyncio.run(classify_frames(frame_members(4), ctx))
    # 原窗 (0,4) 溢出 → 对半 (0,2)/(2,4) 各自成功；零成员落 fallback
    assert [c[2]["properties"]["labels"]["minItems"]
            for c in engine.frame_calls] == [4, 2, 2]
    assert all(c.source == "llm" for c in result.values())
    assert ctx.metrics.counters["budget.degrade_retries"] == 1
    assert ctx.metrics.counters["frame_classify.calls"] == 3
    assert "frame_classify.window_failures" not in ctx.metrics.counters


def test_classify_frames_degrade_exhausted_feeds_breaker_once_and_falls_back():
    from labelkit.common.errors import ContextOverflowError

    class FeedMetrics(RecordingMetrics):
        def __init__(self):
            super().__init__()
            self.fed: list = []

        def record_provider_result(self, fatal, *, hard=False):
            self.fed.append((fatal, hard))

    cfg = frame_cfg(context_window=100_000)
    outcomes = [ContextOverflowError("prompt too long", phase="reactive")
                for _ in range(3)]                     # (0,4) → (0,2) → (0,1) 终局
    engine = FrameStageEngine({}, frame_outcomes=outcomes)
    ctx = budget_ctx(cfg, engine)
    ctx.metrics = FeedMetrics()
    result = asyncio.run(classify_frames(frame_members(4), ctx))
    # 降级树终局失败 ⇒ 整个原始窗兜底；反应式 400 终局恰一次喂熔断（A7）
    assert all(c.label == "other" and c.source == "fallback"
               for c in result.values())
    assert all(c.detail["kind"] == "context_overflow" for c in result.values())
    assert ctx.metrics.counters["budget.degrade_retries"] == 2
    assert ctx.metrics.counters["frame_classify.window_failures"] == 1
    assert ctx.metrics.counters["frame_classify.fallback"] == 4
    assert ctx.metrics.fed == [(True, False)]


def test_classify_frames_precheck_overflow_skips_window_without_dispatch():
    # 校准后图像成本巨大 ⇒ 每窗（强制 2 帧兜底窗 + 去重叠单帧窗）皆 precheck 溢出：
    # 零 LLM 派发（若误派发 ExplodingEngine 会把 kind 打成 internal_error）、全员
    # fallback、precheck 永不喂熔断（RecordingMetrics 无 record_provider_result——
    # 被误喂将直接 AttributeError）、非 reject 不计 budget.overflow_records（V13②）。
    cfg = frame_cfg(context_window=2000, modality="ui", vision_resolved=True)
    members = [seq_member_ui(i) for i in range(1, 5)]
    ctx = budget_ctx(cfg, ExplodingEngine(), image_cost=10_000)
    result = asyncio.run(classify_frames(members, ctx))
    assert all(c.label == "other" and c.source == "fallback"
               for c in result.values())
    assert all(c.detail["kind"] == "context_overflow" for c in result.values())
    assert ctx.metrics.counters["frame_classify.fallback"] == 4
    assert ctx.metrics.counters["frame_classify.window_failures"] == 3
    assert ctx.metrics.counters["frame_classify.calls"] == 3
    assert "budget.overflow_records" not in ctx.metrics.counters


# ── stage 帧 pass：产物、事件、计数与执行门四条件 ────────────────────────────

def test_stage_frame_pass_writes_member_classifications_and_event():
    cfg = frame_cfg()
    members = frame_members(3)
    episode = seq_record(members, rid="ep0001aabbccdd00")
    item = PipelineItem(record=episode)
    single = PipelineItem(record=text_record(rid="solo", text="独行记录"))
    engine = FrameStageEngine({"ep0001aabbccdd00": {"class": "qa"},
                               "solo": {"class": "qa"}})
    out, ctx = run_stage(cfg, [item, single], engine)
    assert item.classification.label == "qa"           # 序列级判决先行写入
    assert set(item.member_classifications) == {"fm1", "fm2", "fm3"}
    assert all(c == Classification(label="task_request", labels=("task_request",),
                                   source="llm", detail={})
               for c in item.member_classifications.values())
    assert single.member_classifications is None       # 帧 pass 只作用于序列信封
    assert ctx.metrics.counters["frame_classify.calls"] == 1
    # 帧窗 record_ids 归属 = episode_id（SPEC §3.2 调用形态）
    assert engine.frame_calls[0][3] == ("ep0001aabbccdd00",)
    frame_events = [e for e in ctx.metrics.events if e[0] == "classify.frame"]
    assert frame_events == [("classify.frame", "classify", ("ep0001aabbccdd00",),
                             {"members": 3, "windows": 1, "fallback": 0})]


def test_stage_frame_window_failure_keeps_episode_active():
    cfg = frame_cfg()
    episode = seq_record(frame_members(2), rid="ep0002aabbccdd00")
    item = PipelineItem(record=episode)
    engine = FrameStageEngine(
        {"ep0002aabbccdd00": {"class": "qa"}},
        frame_outcomes=[SchemaViolation(["/labels: 枚举违规"], "{}")])
    out, ctx = run_stage(cfg, [item], engine)
    assert item.status == "active" and item.errors == []   # 永不 episode failed
    assert all(c.label == "other" and c.source == "fallback"
               for c in item.member_classifications.values())
    (ev,) = [e for e in ctx.metrics.events if e[0] == "classify.frame"]
    assert ev[3] == {"members": 2, "windows": 1, "fallback": 2}
    assert ctx.metrics.counters["frame_classify.window_failures"] == 1


def test_stage_frame_gate_skips_clone_envelopes():
    # 克隆判据 = classification.label != classification.labels[0]（verify S8 同款）
    cfg = frame_cfg()
    episode = seq_record([text_record("步骤", rid="fm1")], rid="ep0003aabbccdd00")
    clone = PipelineItem(record=episode,
                         classification=Classification(label="qa",
                                                       labels=("writing", "qa"),
                                                       source="llm", detail={}))
    out, ctx = run_stage(cfg, [clone], ExplodingEngine())   # 零调用（序列级也幂等跳过）
    assert clone.member_classifications is None
    assert ctx.metrics.counters == {} and ctx.metrics.events == []


def test_stage_frame_gate_skips_degraded_episode_with_counter():
    cfg = frame_cfg()
    episode = seq_record(frame_members(2), rid="ep0004aabbccdd00")
    item = PipelineItem(record=episode)
    item.segment_degraded = {"kind": "segmentation_invalid", "windows_failed": 1}
    engine = FrameStageEngine({"ep0004aabbccdd00": {"class": "qa"}})
    out, ctx = run_stage(cfg, [item], engine)
    assert item.classification is not None             # 序列级判决照常
    assert item.member_classifications is None         # 帧 pass 跳过（降格裁决）
    assert engine.frame_calls == []
    assert ctx.metrics.counters["frame_classify.skipped_degraded"] == 1
    assert not [e for e in ctx.metrics.events if e[0] == "classify.frame"]


def test_stage_frame_gate_idempotent_on_existing_dict():
    cfg = frame_cfg()
    episode = seq_record([text_record("步骤", rid="fm1")], rid="ep0005aabbccdd00")
    existing = {"fm1": Classification(label="chitchat", labels=("chitchat",),
                                      source="llm", detail={})}
    item = PipelineItem(record=episode, member_classifications=existing)
    engine = FrameStageEngine({"ep0005aabbccdd00": {"class": "qa"}})
    out, ctx = run_stage(cfg, [item], engine)
    assert item.member_classifications is existing     # 幂等门：原 dict 原样保留
    assert engine.frame_calls == []
    assert "frame_classify.calls" not in ctx.metrics.counters


def test_stage_frame_only_dual_gate_skips_sequence_classification():
    # v1.12 双门（SPEC §3.2）：classify.enabled=false ∧ frame.classify.enabled=true
    # 时序列级判决静默跳过、帧 pass 照常运行（组链或门由 test_cli 的 factory
    # 测试守护）；空 by_record 引擎保证序列级若被误调必挂（KeyError → failed）。
    from dataclasses import replace as _replace
    cfg = frame_cfg()
    cfg = _replace(cfg, classify=_replace(cfg.classify, enabled=False))
    episode = seq_record(frame_members(2), rid="ep0007aabbccdd00")
    item = PipelineItem(record=episode)
    engine = FrameStageEngine({})
    out, ctx = run_stage(cfg, [item], engine)
    assert item.status == "active" and item.classification is None
    assert set(item.member_classifications) == {"fm1", "fm2"}
    assert len(engine.frame_calls) == 1
    assert engine.calls == engine.frame_calls          # 序列级零调用
    assert ctx.metrics.counters["frame_classify.calls"] == 1


def test_stage_frame_pass_disabled_by_switch():
    cfg = frame_cfg(frame_enabled=False)
    episode = seq_record(frame_members(2), rid="ep0006aabbccdd00")
    item = PipelineItem(record=episode)
    engine = FrameStageEngine({"ep0006aabbccdd00": {"class": "qa"}})
    out, ctx = run_stage(cfg, [item], engine)
    assert item.classification is not None
    assert item.member_classifications is None
    assert engine.frame_calls == []
    assert not any(key.startswith("frame_classify.")
                   for key in ctx.metrics.counters)


# ── _fan_out：两 dict 按引用共享（扇出共享裁决） ─────────────────────────────

def test_fan_out_pins_shared_annotations_dict_when_frame_annotate_on():
    # 终审缺陷修复（扇出共享时序）：M5 尚未运行时克隆共享 None 将永久失联
    # （M5 的「None ⇒ 重绑新 dict」只作用于原信封）——扇出前对首标签序列信封
    # 钉住共享容器 {}，克隆与原信封持同一 dict 对象。
    from dataclasses import replace as _replace

    from labelkit.common.config.model import FrameAnnotateConfig
    cfg = frame_cfg(assignment="multi", classes=CLASSES4, max_labels=4)
    cfg = _replace(cfg, frame_annotate=FrameAnnotateConfig(
        enabled=True, llm="default", instruction="标注帧"))
    episode = seq_record(frame_members(2), rid="ep0008aabbccdd00")
    item = PipelineItem(record=episode)
    engine = FrameStageEngine({"ep0008aabbccdd00": {"classes": ["code", "qa"]}})
    out, ctx = run_stage(cfg, [item], engine)
    (clone,) = out[1:]
    assert item.member_annotations == {}                # 已钉住（非 None 空容器）
    assert clone.member_annotations is item.member_annotations


def test_fan_out_pin_respects_degraded_none_semantics():
    # 降格信封的帧 pass 恒跳过：钉住不得破坏「dict None = pass 未运行」语义。
    from dataclasses import replace as _replace

    from labelkit.common.config.model import FrameAnnotateConfig
    cfg = frame_cfg(assignment="multi", classes=CLASSES4, max_labels=4)
    cfg = _replace(cfg, frame_annotate=FrameAnnotateConfig(
        enabled=True, llm="default", instruction="标注帧"))
    episode = seq_record(frame_members(2), rid="ep0009aabbccdd00")
    item = PipelineItem(record=episode)
    item.segment_degraded = {"kind": "segmentation_invalid", "windows_failed": 1}
    engine = FrameStageEngine({"ep0009aabbccdd00": {"classes": ["code", "qa"]}})
    out, ctx = run_stage(cfg, [item], engine)
    (clone,) = out[1:]
    assert item.member_annotations is None
    assert clone.member_annotations is None


def test_classify_frames_duplicate_member_ids_first_wins():
    # 终审缺陷修复（内容哈希 id 碰撞）：同 id 成员 first-wins——后位次判决
    # 不覆盖首位次、不重复计数（温度 0 下同内容判决本就应一致）。
    cfg = frame_cfg()
    members = frame_members(2)
    dup_batch = [members[0], members[0], members[1]]    # 位次 0/1 同 id
    engine = FrameStageEngine(
        {}, frame_outcomes=[{"labels": ["task_request", "chitchat", "other"]}])
    ctx = make_ctx(cfg, engine)
    result = asyncio.run(classify_frames(dup_batch, ctx))
    assert set(result) == {members[0].id, members[1].id}
    assert result[members[0].id].label == "task_request"   # 首位次胜出
    assert result[members[1].id].label == "other"
    assert "frame_classify.fallback" not in ctx.metrics.counters


def test_stage_frame_fan_out_clones_share_member_dicts_by_reference():
    cfg = frame_cfg(assignment="multi", classes=CLASSES4, max_labels=4)
    episode = seq_record(frame_members(2), rid="ep0007aabbccdd00")
    item = PipelineItem(record=episode)
    sentinel_annotations = {"fm1": None}               # 后续 wave 的 M5 产物占位
    item.member_annotations = sentinel_annotations
    engine = FrameStageEngine({"ep0007aabbccdd00": {"classes": ["code", "qa"]}})
    out, ctx = run_stage(cfg, [item], engine)
    (clone,) = out[1:]
    assert clone.classification.label == "code"        # 克隆 = 非首标签信封
    assert item.member_classifications is not None
    assert clone.member_classifications is item.member_classifications
    assert clone.member_annotations is sentinel_annotations
    assert len(engine.frame_calls) == 1                # 帧 pass 只跑一次（扇出前）

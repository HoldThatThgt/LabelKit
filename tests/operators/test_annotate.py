"""Offline unit tests for M5 annotate: prompt assembly (spec 3.5.2 / CONTRACTS §10.1,
§10.5) and the self-consistency field-level majority vote. Pure logic only — no LLM.

v1.8 sequence annotation (S5/S6/S28, CONTRACTS §10.1 sequence variant): the deterministic
keyframe downsample formula, the ①②③ segment order with the ALWAYS-text final part
(repair suffix never swallows the last image), the transitions trailing kwarg threading
through the single/self-consistency/repair paths and the stage layer, and the
single-record default-kwarg regression anchor."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from labelkit.operators.annotate import (
    AnnotateStage,
    RepairContext,
    _keyframe_indexes,
    _majority_vote,
    _member_digest_lines,
    _voted_keys,
    annotate_member,
    annotate_record,
    build_annotate_prompt,
    build_frame_annotate_prompt,
)
from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ClassSpec,
    ClassView,
    ConsoleConfig,
    DedupConfig,
    ExtractConfig,
    FewShotExample,
    FrameAnnotateConfig,
    FrameClassView,
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
from labelkit.common.contracts.types import (
    Annotation,
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

USER_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string",
                   "enum": ["writing_assist", "qa", "translation", "chitchat", "other"]},
        "topic": {"type": "string"},
        "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]},
    },
    "required": ["intent", "topic", "difficulty"],
    "additionalProperties": False,
}

SCHEMA_TEXT = json.dumps(USER_SCHEMA, ensure_ascii=False, separators=(", ", ": "))


def make_cfg(*, modality="text", instruction="你是意图标注员。", examples=(),
             self_consistency=0, ui_tree_max_chars=30000,
             user_schema=USER_SCHEMA, trace=None,
             sequence_frames=20) -> ResolvedConfig:
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
        classify=ClassifyConfig(),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(enabled=True, llm="default", instruction=instruction,
                                examples=tuple(examples),
                                self_consistency=self_consistency,
                                sequence_frames=sequence_frames),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline=json.dumps(user_schema)),
        trace=trace or TraceConfig(),
        rubric=Rubric(name="default:text", criteria=()),
        class_views={},
        user_schema=user_schema,
        limit=None,
        strict=False,
        dry_run=False,
        config_path="config.toml",
        project_path="project.toml",
        config_digest="sha256:0",
        project_digest="sha256:0",
    )


def text_record(text="帮我写一条请假条，明天上午要去医院") -> Record:
    return Record(id="1cda030abc565f17", modality="text", text=text,
                  raw={"instruction": text}, ui_tree=None, image=None,
                  ref=RecordRef("data.jsonl", 1, None, ()))


def ui_record(tmp_path=None) -> Record:
    nodes = (
        UINode("1", None, 0, "FrameLayout", "", "", (0, 0, 1080, 1920), True, {}),
        UINode("2", "1", 1, "EditText", "请输入手机号", "", (72, 520, 1008, 664), True, {}),
        UINode("3", "1", 1, "Button", "登录", "", (72, 952, 1008, 1096), True, {}),
        UINode("4", "1", 1, "View", "", "", (0, 0, 0, 0), False, {}),
    )
    image = ImageRef(path=(tmp_path or __import__("pathlib").Path("image_2.png")),
                     format="png", size_bytes=1234)
    return Record(id="9f2c31ab52e08d17", modality="ui", text=None, raw=None,
                  ui_tree=UITree(nodes), image=image,
                  ref=RecordRef("b/uitree_2.jsonl", None, 2, ()))


# ── prompt assembly: text modality ──────────────────────────────────────────

def test_text_prompt_section_order_and_content():
    examples = (FewShotExample(input="NBA 总决赛什么时候开始",
                               output={"intent": "qa", "topic": "体育赛事时间查询",
                                       "difficulty": "easy"}),)
    cfg = make_cfg(examples=examples)
    rec = text_record()
    bundle = build_annotate_prompt(rec, cfg, SCHEMA_TEXT)

    # order: system, one user message per few-shot example, current record last
    assert [m.role for m in bundle.messages] == ["system", "user", "user"]

    system = bundle.messages[0]
    assert len(system.parts) == 1 and system.parts[0].kind == "text"
    assert system.parts[0].text == (
        "你是意图标注员。\n"
        "输出必须是符合以下 JSON Schema 的单个 JSON 对象，不输出任何其他内容：\n"
        + SCHEMA_TEXT
    )

    record_msg = bundle.messages[2]
    assert len(record_msg.parts) == 1
    assert record_msg.parts[0].text == "[待标注数据] 帮我写一条请假条，明天上午要去医院"

    # default temperature = None (profile default)
    assert bundle.temperature is None


def test_few_shot_rendering_exact_and_in_order():
    examples = (
        FewShotExample(input="第一个示例", output={"intent": "qa", "topic": "主题一",
                                                    "difficulty": "easy"}),
        FewShotExample(input="第二个示例", output={"intent": "chitchat", "topic": "主题二",
                                                    "difficulty": "hard"}),
    )
    cfg = make_cfg(examples=examples)
    bundle = build_annotate_prompt(text_record(), cfg, SCHEMA_TEXT)

    ex1 = bundle.messages[1].parts[0].text
    ex2 = bundle.messages[2].parts[0].text
    # exact labels, JSON via json.dumps(..., ensure_ascii=False) — Chinese unescaped
    assert ex1 == ('[示例输入] 第一个示例\n[示例输出] '
                   '{"intent": "qa", "topic": "主题一", "difficulty": "easy"}')
    assert ex2 == ('[示例输入] 第二个示例\n[示例输出] '
                   '{"intent": "chitchat", "topic": "主题二", "difficulty": "hard"}')
    # record message is last
    assert bundle.messages[3].parts[0].text.startswith("[待标注数据] ")


def test_no_examples_yields_system_plus_record_only():
    bundle = build_annotate_prompt(text_record(), make_cfg(), SCHEMA_TEXT)
    assert [m.role for m in bundle.messages] == ["system", "user"]


def test_temperature_passthrough():
    bundle = build_annotate_prompt(text_record(), make_cfg(), SCHEMA_TEXT, temperature=0.7)
    assert bundle.temperature == 0.7


# ── prompt assembly: UI modality ────────────────────────────────────────────

def test_ui_prompt_three_parts_in_one_user_message():
    cfg = make_cfg(modality="ui")
    rec = ui_record()
    bundle = build_annotate_prompt(rec, cfg, SCHEMA_TEXT)

    record_msg = bundle.messages[-1]
    assert record_msg.role == "user"
    assert [p.kind for p in record_msg.parts] == ["text", "image", "text"]

    assert record_msg.parts[0].text == "[屏幕截图]"
    assert record_msg.parts[1].image is rec.image

    expected_tree = rec.ui_tree.serialize(max_chars=cfg.input.ui_tree_max_chars)
    assert record_msg.parts[2].text == "[UI 控件树]\n" + expected_tree
    # serialized tree: visible nodes only, two-space indentation
    assert 'EditText "请输入手机号" [72,520,1008,664]' in expected_tree
    assert "View" not in expected_tree           # invisible node skipped


def test_ui_prompt_honors_ui_tree_max_chars():
    cfg = make_cfg(modality="ui", ui_tree_max_chars=40)
    rec = ui_record()
    bundle = build_annotate_prompt(rec, cfg, SCHEMA_TEXT)
    tree_part = bundle.messages[-1].parts[2].text
    body = tree_part.removeprefix("[UI 控件树]\n")
    assert len(body) <= 40
    assert "…(truncated" in body


# ── repair suffix (§10.5) ───────────────────────────────────────────────────

def test_repair_suffix_appended_to_final_text_part():
    repair = RepairContext(previous_output={"intent": "qa", "topic": "错", "difficulty": "easy"},
                           critiques_text="事实一致性: 主题字段与原始数据不符")
    bundle = build_annotate_prompt(text_record(), make_cfg(), SCHEMA_TEXT, repair=repair)
    final = bundle.messages[-1].parts[-1].text
    assert final == (
        "[待标注数据] 帮我写一条请假条，明天上午要去医院\n"
        '[上一版标注] {"intent": "qa", "topic": "错", "difficulty": "easy"}\n'
        "[审核意见] 事实一致性: 主题字段与原始数据不符\n"
        "请修正后重新输出"
    )


def test_repair_suffix_ui_modality_lands_on_tree_part():
    repair = RepairContext(previous_output={"a": 1}, critiques_text="x: y")
    cfg = make_cfg(modality="ui")
    bundle = build_annotate_prompt(ui_record(), cfg, SCHEMA_TEXT, repair=repair)
    parts = bundle.messages[-1].parts
    assert [p.kind for p in parts] == ["text", "image", "text"]
    assert parts[2].text.startswith("[UI 控件树]\n")
    assert parts[2].text.endswith('[上一版标注] {"a": 1}\n[审核意见] x: y\n请修正后重新输出')


# ── annotate.done excerpt payload — trace.content tier gating (§7.4) ────────

def trace_cfg(*, enabled=True, content="refs") -> ResolvedConfig:
    return make_cfg(trace=TraceConfig(
        enabled=enabled, channels=("quality", "verify", "schema", "annotate"),
        content=content))


def test_excerpt_payload_absent_at_none_and_refs_tiers():
    rec = text_record()
    assert AnnotateStage(trace_cfg(content="none"))._excerpt_payload(rec) is None
    assert AnnotateStage(trace_cfg(content="refs"))._excerpt_payload(rec) is None


def test_excerpt_payload_present_at_excerpt_tier():
    rec = text_record()
    stage = AnnotateStage(trace_cfg(content="excerpt"))
    assert stage._excerpt_payload(rec) == {rec.id: "帮我写一条请假条，明天上午要去医院"}


def test_excerpt_payload_present_at_full_tier():
    # §7.4: tiers are cumulative ("逐档递增") — full includes everything excerpt has.
    rec = text_record()
    stage = AnnotateStage(trace_cfg(content="full"))
    assert stage._excerpt_payload(rec) == {rec.id: "帮我写一条请假条，明天上午要去医院"}


def test_excerpt_payload_absent_when_trace_disabled():
    rec = text_record()
    stage = AnnotateStage(trace_cfg(enabled=False, content="full"))
    assert stage._excerpt_payload(rec) is None


def test_excerpt_truncates_to_200_chars_and_serializes_ui_tree():
    long_text = "长" * 500
    stage = AnnotateStage(trace_cfg(content="full"))
    payload = stage._excerpt_payload(text_record(long_text))
    (excerpt,) = payload.values()
    assert excerpt == long_text[:200]

    ui = ui_record()
    ui_payload = stage._excerpt_payload(ui)
    assert ui_payload == {ui.id: ui.ui_tree.serialize()[:200]}


# ── voted-key detection ─────────────────────────────────────────────────────

def test_voted_keys_enum_boolean_integer_only():
    schema = {"type": "object", "properties": {
        "e": {"type": "string", "enum": ["a", "b"]},
        "b": {"type": "boolean"},
        "i": {"type": "integer"},
        "free": {"type": "string"},
        "num": {"type": "number"},
        "arr": {"type": "array", "items": {"type": "string"}},
        "obj": {"type": "object"},
    }}
    assert _voted_keys(schema) == ("e", "b", "i")


# ── self-consistency majority vote ──────────────────────────────────────────

def test_vote_clear_majority():
    samples = [
        {"intent": "qa", "topic": "t1", "difficulty": "easy"},
        {"intent": "qa", "topic": "t2", "difficulty": "easy"},
        {"intent": "other", "topic": "t3", "difficulty": "hard"},
    ]
    chosen, matches, disagreed = _majority_vote(samples, USER_SCHEMA)
    assert not disagreed
    # modal combination (qa, easy); first matching sample wins → free text from sample 1
    assert chosen == samples[0]
    assert matches == 2


def test_vote_per_field_independence():
    # intent modes come from samples 1&2, difficulty mode from samples 2&3:
    # modal combination (qa, hard) only matches sample 2 — wholesale fields from there.
    samples = [
        {"intent": "qa", "topic": "t1", "difficulty": "easy"},
        {"intent": "qa", "topic": "t2", "difficulty": "hard"},
        {"intent": "other", "topic": "t3", "difficulty": "hard"},
    ]
    chosen, matches, disagreed = _majority_vote(samples, USER_SCHEMA)
    assert not disagreed
    assert chosen == samples[1]
    assert chosen["topic"] == "t2"
    assert matches == 1


def test_vote_full_disagreement_tie_falls_back_to_first_sample():
    samples = [
        {"intent": "qa", "topic": "t1", "difficulty": "easy"},
        {"intent": "other", "topic": "t2", "difficulty": "medium"},
        {"intent": "chitchat", "topic": "t3", "difficulty": "hard"},
    ]
    chosen, matches, disagreed = _majority_vote(samples, USER_SCHEMA)
    assert disagreed
    assert chosen == samples[0]
    assert matches == 1     # only sample #1 matches its own combination


def test_vote_modal_combination_matches_no_sample():
    schema = {"type": "object", "properties": {
        "f1": {"type": "string", "enum": ["a", "b"]},
        "f2": {"type": "string", "enum": ["x", "y"]},
        "f3": {"type": "string", "enum": ["p", "q"]},
    }}
    # per-field modes: f1=a, f2=x, f3=q — but no sample is (a, x, q)
    samples = [
        {"f1": "a", "f2": "x", "f3": "p"},
        {"f1": "a", "f2": "y", "f3": "q"},
        {"f1": "b", "f2": "x", "f3": "q"},
    ]
    chosen, matches, disagreed = _majority_vote(samples, schema)
    assert disagreed
    assert chosen == samples[0]
    assert matches == 1


def test_vote_boolean_and_integer_fields():
    schema = {"type": "object", "properties": {
        "flag": {"type": "boolean"},
        "level": {"type": "integer"},
        "note": {"type": "string"},
    }}
    samples = [
        {"flag": True, "level": 2, "note": "n1"},
        {"flag": True, "level": 3, "note": "n2"},
        {"flag": False, "level": 3, "note": "n3"},
    ]
    chosen, matches, disagreed = _majority_vote(samples, schema)
    assert not disagreed
    assert chosen == samples[1]                    # (True, 3) → first match = sample 2
    assert matches == 1


def test_vote_no_voted_fields_takes_first_sample_without_disagreement():
    schema = {"type": "object", "properties": {"summary": {"type": "string"}}}
    samples = [{"summary": "a"}, {"summary": "b"}, {"summary": "c"}]
    chosen, matches, disagreed = _majority_vote(samples, schema)
    assert not disagreed
    assert chosen == samples[0]
    assert matches == 3        # empty voted combination matches every sample


# ── v1.5 plan A: SchemaViolation(callback_only) → kind=callback_violation ────

def test_callback_violation_kind_mapping(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from labelkit.common.errors import SchemaViolation
    from labelkit.common.contracts.types import PipelineItem

    cfg = make_cfg()

    class RaisingEngine:
        user_schema_text = "{}"
        async def complete_validated(self, *a, **k):
            raise SchemaViolation(["(validator) topic 太长"], "{}", callback_only=True)

    class Metrics:
        def __init__(self):
            self.events = []
        def event(self, ev, **kw):
            self.events.append((ev, kw.get("payload") or {}))
        def count(self, key, n=1):
            pass

    ctx = SimpleNamespace(cfg=cfg, schema_engine=RaisingEngine(), metrics=Metrics(),
                          batch_no=1)
    stage = AnnotateStage(cfg)
    item = PipelineItem(record=text_record())
    asyncio.run(stage._annotate_item(item, ctx))
    assert item.status == "failed"
    assert item.errors and item.errors[0].kind == "callback_violation"


def test_schema_violation_kind_unchanged_without_flag(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from labelkit.common.errors import SchemaViolation
    from labelkit.common.contracts.types import PipelineItem

    cfg = make_cfg()

    class RaisingEngine:
        user_schema_text = "{}"
        async def complete_validated(self, *a, **k):
            raise SchemaViolation(["/intent: 枚举违规"], "{}")

    class Metrics:
        def event(self, ev, **kw): pass
        def count(self, key, n=1): pass

    ctx = SimpleNamespace(cfg=cfg, schema_engine=RaisingEngine(), metrics=Metrics(),
                          batch_no=1)
    stage = AnnotateStage(cfg)
    item = PipelineItem(record=text_record())
    asyncio.run(stage._annotate_item(item, ctx))
    assert item.status == "failed"
    assert item.errors and item.errors[0].kind == "schema_violation"


# ── v1.7 label threading (R2/R5): class-effective instruction/examples ───────

CLASS_INSTRUCTION = "你是写作类指令的意图标注员。"
CLASS_EXAMPLE = FewShotExample(
    input="以春天为题写一首短诗",
    output={"intent": "writing_assist", "topic": "诗歌创作", "difficulty": "medium"})


def make_classified_cfg(**kw) -> ResolvedConfig:
    """make_cfg + classify enabled + two class views: 'writing' overrides the
    annotate instruction/examples, 'qa' is a zero-override view (= global)."""
    base = make_cfg(**kw)
    class_annotate = replace(base.annotate, instruction=CLASS_INSTRUCTION,
                             examples=(CLASS_EXAMPLE,))
    views = {
        "writing": ClassView(name="writing", quality=base.quality, rubric=base.rubric,
                             annotate=class_annotate, generate=base.generate,
                             verify=base.verify, extract=ExtractConfig()),
        "qa": ClassView(name="qa", quality=base.quality, rubric=base.rubric,
                        annotate=base.annotate, generate=base.generate,
                        verify=base.verify, extract=ExtractConfig()),
    }
    classify = ClassifyConfig(
        enabled=True, fallback_class="qa", max_labels=None,
        classes=(ClassSpec(name="writing", description="写作协助类指令"),
                 ClassSpec(name="qa", description="知识问答类指令")))
    return replace(base, classify=classify, class_views=views)


def _classification(label="writing"):
    return Classification(label=label, labels=(label,), source="llm", detail={})


def test_label_takes_class_effective_instruction_and_examples():
    global_examples = (FewShotExample(input="全局示例",
                                      output={"intent": "qa", "topic": "全局主题",
                                              "difficulty": "easy"}),)
    cfg = make_classified_cfg(examples=global_examples)
    bundle = build_annotate_prompt(text_record(), cfg, SCHEMA_TEXT, label="writing")

    # template structure unchanged: system, one message per class example, record
    assert [m.role for m in bundle.messages] == ["system", "user", "user"]
    assert bundle.messages[0].parts[0].text == (
        CLASS_INSTRUCTION + "\n"
        "输出必须是符合以下 JSON Schema 的单个 JSON 对象，不输出任何其他内容：\n"
        + SCHEMA_TEXT
    )
    example_text = bundle.messages[1].parts[0].text
    assert example_text == (
        "[示例输入] 以春天为题写一首短诗\n[示例输出] "
        '{"intent": "writing_assist", "topic": "诗歌创作", "difficulty": "medium"}'
    )
    assert "全局示例" not in example_text


def test_label_none_falls_back_to_global_config():
    global_examples = (FewShotExample(input="全局示例",
                                      output={"intent": "qa", "topic": "全局主题",
                                              "difficulty": "easy"}),)
    cfg = make_classified_cfg(examples=global_examples)
    plain = make_cfg(examples=global_examples)
    # label omitted → byte-identical to the pre-v1.7 global assembly
    assert (build_annotate_prompt(text_record(), cfg, SCHEMA_TEXT)
            == build_annotate_prompt(text_record(), plain, SCHEMA_TEXT))
    # zero-override view ('qa') also equals the global assembly
    assert (build_annotate_prompt(text_record(), cfg, SCHEMA_TEXT, label="qa")
            == build_annotate_prompt(text_record(), plain, SCHEMA_TEXT))


class _CapturingEngine:
    user_schema_text = SCHEMA_TEXT

    def __init__(self):
        self.prompts = []

    async def complete_validated(self, profile, prompt, *, record_ids, batch_no,
                                 record=None):
        from labelkit.common.contracts.types import Usage
        self.prompts.append(prompt)
        return ({"intent": "writing_assist", "topic": "诗歌创作", "difficulty": "easy"},
                Usage(1, 1), 1, "m")


class _CapturingMetrics:
    def __init__(self):
        self.events = []

    def event(self, ev, **kw):
        self.events.append((ev, dict(kw.get("payload") or {})))

    def count(self, key, n=1):
        pass


def test_stage_passes_classification_label_and_event_carries_it():
    import asyncio
    from types import SimpleNamespace
    from labelkit.common.contracts.types import PipelineItem

    cfg = make_classified_cfg()
    engine, metrics = _CapturingEngine(), _CapturingMetrics()
    ctx = SimpleNamespace(cfg=cfg, schema_engine=engine, metrics=metrics, batch_no=1)
    item = PipelineItem(record=text_record(), classification=_classification("writing"))
    asyncio.run(AnnotateStage(cfg)._annotate_item(item, ctx))

    assert item.status == "active" and item.annotation is not None
    # the prompt reaching M8 was assembled from the class view
    assert engine.prompts[0].messages[0].parts[0].text.startswith(CLASS_INSTRUCTION + "\n")
    # annotate.done carries the label (R5: classify enabled + classified item)
    ((ev, payload),) = metrics.events
    assert ev == "annotate.done"
    assert payload["label"] == "writing"


def test_annotate_done_without_classification_has_no_label():
    import asyncio
    from types import SimpleNamespace
    from labelkit.common.contracts.types import PipelineItem

    cfg = make_classified_cfg()
    engine, metrics = _CapturingEngine(), _CapturingMetrics()
    ctx = SimpleNamespace(cfg=cfg, schema_engine=engine, metrics=metrics, batch_no=1)
    item = PipelineItem(record=text_record())          # classification is None
    asyncio.run(AnnotateStage(cfg)._annotate_item(item, ctx))

    # falls back to the global prompt and omits the payload field
    assert engine.prompts[0].messages[0].parts[0].text.startswith("你是意图标注员。\n")
    ((ev, payload),) = metrics.events
    assert ev == "annotate.done"
    assert "label" not in payload


def test_annotate_record_threads_label_through_sc_path():
    import asyncio
    from types import SimpleNamespace

    cfg = make_classified_cfg(self_consistency=3)
    engine, metrics = _CapturingEngine(), _CapturingMetrics()
    ctx = SimpleNamespace(cfg=cfg, schema_engine=engine, metrics=metrics, batch_no=1)
    ann = asyncio.run(annotate_record(text_record(), ctx, label="writing"))

    assert ann.sc == {"n": 3, "agreement_ratio": 1.0}
    assert len(engine.prompts) == 3
    for prompt in engine.prompts:                      # every sample uses the class view
        assert prompt.messages[0].parts[0].text.startswith(CLASS_INSTRUCTION + "\n")
        assert prompt.temperature == cfg.annotate.sc_temperature


# ── v1.8 sequence annotation (S5/S6/S28, CONTRACTS §10.1 sequence variant) ───

def ui_member(i: int) -> Record:
    nodes = (
        UINode("1", None, 0, "FrameLayout", "", "", (0, 0, 1080, 1920), True,
               {"package": "com.demo.app"}),
        UINode("2", "1", 1, "TextView", f"屏幕{i}", "", (0, 0, 1080, 200), True, {}),
        UINode("3", "1", 1, "Button", "下一步", "", (72, 952, 1008, 1096), True, {}),
    )
    return Record(id=f"frame{i:04d}", modality="ui", text=None, raw=None,
                  ui_tree=UITree(nodes),
                  image=ImageRef(path=Path(f"image_{i}.png"), format="png", size_bytes=1),
                  ref=RecordRef("frames/x.jsonl", None, i, ()))


def text_member(i: int) -> Record:
    return Record(id=f"line{i:04d}", modality="text", text=f"步骤文本{i}",
                  raw={"text": f"步骤文本{i}"}, ui_tree=None, image=None,
                  ref=RecordRef("frames.jsonl", i + 1, None, ()))


def make_episode(members: tuple[Record, ...], ep_id: str = "ep0001") -> Record:
    first = members[0]
    return Record(id=ep_id, modality=first.modality, text=None, raw=None, ui_tree=None,
                  image=None, ref=first.ref, kind="sequence", members=members)


def ui_episode(n: int = 3, ep_id: str = "ep0001") -> Record:
    return make_episode(tuple(ui_member(i) for i in range(n)), ep_id)


SEQ_TRANSITIONS = (
    Transition(index=0, action={"action_type": "click", "target": "登录", "value": None,
                                "description": "点击登录按钮"},
               model="m", attempts=1, detail={}),
    Transition(index=1, action={"action_type": "other", "target": None, "value": None,
                                "description": "两帧间变化无法归因"},
               model="m", attempts=2,
               detail={"kind": "extraction_invalid", "message": "repair exhausted"}),
)

# Annotation evidence renders WITHOUT the （摘取兜底） fallback suffix (S16 marks
# fallback steps only in M4's scoring sections).
ACTION_SECTION = ("[动作序列]\n"
                  "0. click（对象: 登录；值: —）点击登录按钮\n"
                  "1. other（对象: —；值: —）两帧间变化无法归因")


def digest_section(record: Record) -> str:
    return "[成员帧摘要]\n" + "\n".join(
        f"{m}. {frame_digest(member, 400)}"
        for m, member in enumerate(record.members, start=1))


# ── S28 keyframe downsample formula ──────────────────────────────────────────

def test_keyframe_downsample_formula_n25_k20():
    assert _keyframe_indexes(25, 20) == [0, 1, 2, 3, 5, 6, 7, 8, 10, 11, 12, 13,
                                         15, 16, 17, 18, 20, 21, 22, 24]


def test_keyframe_downsample_keeps_all_when_n_le_k():
    assert _keyframe_indexes(3, 20) == [0, 1, 2]
    assert _keyframe_indexes(20, 20) == list(range(20))
    assert _keyframe_indexes(1, 2) == [0]


def test_keyframe_downsample_endpoints_monotonic_no_rng():
    for n, k in ((100, 20), (21, 20), (50, 3), (2, 2), (101, 7), (1000, 100)):
        idx = _keyframe_indexes(n, k)
        assert idx[0] == 0                       # first ALWAYS kept
        assert idx[-1] == n - 1                  # last ALWAYS kept
        assert idx == sorted(set(idx))           # strictly increasing, no duplicates
        assert len(idx) == min(n, k)


# ── v1.9 (T14): per-fragment keyframe quota ─────────────────────────────────

def test_keyframe_quota_every_fragment_keeps_at_least_one():
    """The minor-8 counterexample: a small fragment that the uniform formula
    drains whole must keep ≥ 1 keyframe under the quota path."""
    # fragments 20 + 2 + 3 = 25 members, k = 4: uniform picks [0, 8, 16, 24]
    # — nothing from the 2-member middle fragment
    uniform = _keyframe_indexes(25, 4)
    assert not any(20 <= i < 22 for i in uniform)
    quota = _keyframe_indexes(25, 4, (20, 2, 3))
    assert len(quota) == 4
    assert any(20 <= i < 22 for i in quota)      # middle fragment survives
    assert quota[0] == 0 and quota[-1] == 24     # global first/last invariant
    assert quota == sorted(set(quota))


def test_keyframe_quota_largest_remainder_distribution():
    # n=10, k=5, fragments (6, 2, 2): surplus 2 over weights (5, 1, 1) →
    # base [1, 0, 0] + leftover 1 by largest remainder (3/7 vs 2/7) → frag 1
    idx = _keyframe_indexes(10, 5, (6, 2, 2))
    assert len(idx) == 5
    assert idx[0] == 0 and idx[-1] == 9
    per_fragment = [sum(1 for i in idx if lo <= i < hi)
                    for lo, hi in ((0, 6), (6, 8), (8, 10))]
    assert per_fragment == [3, 1, 1]             # every fragment ≥ 1
    # quota-1 middle fragment keeps its FIRST member; last fragment keeps LAST
    assert 6 in idx and 9 in idx


def test_keyframe_quota_degrades_to_uniform_when_infeasible_or_absent():
    uniform = _keyframe_indexes(25, 4)
    assert _keyframe_indexes(25, 4, None) == uniform
    assert _keyframe_indexes(25, 4, (25,)) == uniform          # single fragment
    assert _keyframe_indexes(25, 4, (10, 10)) == uniform       # sum mismatch
    assert _keyframe_indexes(25, 4, (5,) * 5) == uniform       # k < m infeasible
    # n <= k keeps everything regardless of fragments
    assert _keyframe_indexes(4, 20, (2, 2)) == [0, 1, 2, 3]


def test_keyframe_quota_invariants_across_shapes():
    for n, k, lens in ((25, 20, (20, 2, 3)), (30, 5, (1, 1, 27, 1)),
                       (12, 6, (4, 4, 4)), (9, 3, (3, 3, 3)),
                       (40, 7, (35, 2, 3))):
        idx = _keyframe_indexes(n, k, lens)
        assert len(idx) == k
        assert idx[0] == 0 and idx[-1] == n - 1
        assert idx == sorted(set(idx))
        bounds, start = [], 0
        for length in lens:
            bounds.append((start, start + length))
            start += length
        assert all(any(lo <= i < hi for i in idx) for lo, hi in bounds)


# ── sequence template: ①②③ order + text-final invariant (S6) ────────────────

def test_sequence_template_three_sections_in_order():
    cfg = make_cfg(modality="ui")
    ep = ui_episode(3)
    bundle = build_annotate_prompt(ep, cfg, SCHEMA_TEXT, transitions=SEQ_TRANSITIONS)
    parts = bundle.messages[-1].parts

    assert [p.kind for p in parts] == [
        "text", "text", "image", "text", "image", "text", "image", "text"]
    assert parts[0].text == ACTION_SECTION                     # ①
    assert "摘取兜底" not in parts[0].text
    assert parts[1].text == "[关键帧 1/3·成员 1]"               # ② labels: 1-based i/k, m
    assert parts[3].text == "[关键帧 2/3·成员 2]"
    assert parts[5].text == "[关键帧 3/3·成员 3]"
    assert parts[2].image is ep.members[0].image
    assert parts[4].image is ep.members[1].image
    assert parts[6].image is ep.members[2].image
    assert parts[-1].kind == "text"                            # ③ ALWAYS-text final part
    assert parts[-1].text == digest_section(ep)


def test_sequence_template_transitions_none_omits_action_section():
    cfg = make_cfg(modality="ui")
    ep = ui_episode(2)
    bundle = build_annotate_prompt(ep, cfg, SCHEMA_TEXT)       # transitions omitted
    parts = bundle.messages[-1].parts
    assert [p.kind for p in parts] == ["text", "image", "text", "image", "text"]
    assert "[动作序列]" not in "".join(p.text or "" for p in parts)
    assert parts[0].text == "[关键帧 1/2·成员 1]"
    assert parts[-1].kind == "text"                            # still closes with ③
    assert parts[-1].text == digest_section(ep)


def test_sequence_template_downsamples_25_members_to_20_keyframes():
    cfg = make_cfg(modality="ui")                              # sequence_frames default 20
    ep = ui_episode(25)
    bundle = build_annotate_prompt(ep, cfg, SCHEMA_TEXT, transitions=SEQ_TRANSITIONS)
    parts = bundle.messages[-1].parts

    images = [p for p in parts if p.kind == "image"]
    assert len(images) == 20
    kept = [0, 1, 2, 3, 5, 6, 7, 8, 10, 11, 12, 13, 15, 16, 17, 18, 20, 21, 22, 24]
    assert [p.image for p in images] == [ep.members[m].image for m in kept]
    labels = [p.text for p in parts[1:-1] if p.kind == "text"]
    assert labels == [f"[关键帧 {i}/20·成员 {m + 1}]"
                      for i, m in enumerate(kept, start=1)]
    assert parts[-1].kind == "text"
    # ③ still digests EVERY member, not just the kept keyframes
    assert parts[-1].text == digest_section(ep)


def test_sequence_template_fragment_lens_selects_quota_keyframes():
    """v1.9 (T14, third additive trailing kwarg): fragment_lens switches the ②
    keyframe selection to per-fragment quotas; None keeps the v1.8 uniform
    downsample byte-identical."""
    cfg = make_cfg(modality="ui", sequence_frames=4)
    ep = ui_episode(25)
    quota = build_annotate_prompt(ep, cfg, SCHEMA_TEXT,
                                  transitions=SEQ_TRANSITIONS,
                                  fragment_lens=(20, 2, 3))
    kept = _keyframe_indexes(25, 4, (20, 2, 3))
    images = [p for p in quota.messages[-1].parts if p.kind == "image"]
    assert [p.image for p in images] == [ep.members[m].image for m in kept]
    assert any(20 <= m < 22 for m in kept)                 # small fragment kept
    plain = build_annotate_prompt(ep, cfg, SCHEMA_TEXT,
                                  transitions=SEQ_TRANSITIONS)
    uniform = _keyframe_indexes(25, 4)
    images_plain = [p for p in plain.messages[-1].parts if p.kind == "image"]
    assert [p.image for p in images_plain] == [ep.members[m].image
                                               for m in uniform]


def test_stage_threads_fragment_lens_from_stitch_duck_mark():
    """v1.9 (T14 穿参义务, M5 main call site): the stage derives fragment_lens
    from the stitch_fragments duck mark and threads it into the prompt — the
    quota keyframe set reaches the request."""
    import asyncio
    from types import SimpleNamespace
    from labelkit.common.contracts.types import PipelineItem

    cfg = make_cfg(modality="ui", sequence_frames=4)
    ep = ui_episode(25)
    item = PipelineItem(record=ep)
    item.stitch_fragments = (
        {"order_span": [0, 19], "member_count": 20, "cause": "origin",
         "source_episode": ep.id},
        {"order_span": [25, 26], "member_count": 2, "cause": "resumed",
         "source_episode": "f" * 16},
        {"order_span": [30, 32], "member_count": 3, "cause": "rescued",
         "source_episode": None},
    )
    engine, metrics = _CapturingEngine(), _CapturingMetrics()
    ctx = SimpleNamespace(cfg=cfg, schema_engine=engine, metrics=metrics, batch_no=1)
    asyncio.run(AnnotateStage(cfg)._annotate_item(item, ctx))
    assert item.annotation is not None
    prompt = engine.prompts[0]
    images = [p for p in prompt.messages[-1].parts if p.kind == "image"]
    kept = _keyframe_indexes(25, 4, (20, 2, 3))
    assert [p.image for p in images] == [ep.members[m].image for m in kept]


def test_text_sequence_degrades_to_steps_plus_digest():
    cfg = make_cfg()
    ep = make_episode(tuple(text_member(i) for i in range(4)))
    with_steps = build_annotate_prompt(ep, cfg, SCHEMA_TEXT, transitions=SEQ_TRANSITIONS)
    parts = with_steps.messages[-1].parts
    assert [p.kind for p in parts] == ["text", "text"]         # ① + ③, no image
    assert parts[0].text == ACTION_SECTION
    assert parts[1].text == digest_section(ep)

    bare = build_annotate_prompt(ep, cfg, SCHEMA_TEXT)
    assert [p.kind for p in bare.messages[-1].parts] == ["text"]   # ③ alone
    assert bare.messages[-1].parts[0].text == digest_section(ep)


def test_member_digest_lines_bounded_first_last_kept():
    members = tuple(
        replace(text_member(i), text=f"帧{i:02d}" + "长" * 80) for i in range(8))
    full = _member_digest_lines(members, 100000)
    assert len(full) == 8

    bounded = _member_digest_lines(members, 350)
    assert len("\n".join(bounded)) <= 350
    assert bounded[0] == full[0]
    assert bounded[-1] == full[-1]
    dropped = 8 - (len(bounded) - 2) - 1
    assert dropped >= 1
    assert bounded[-2] == f"…(truncated {dropped} members)"


# ── repair suffix on the sequence branch (S6: never swallows the last image) ─

def test_sequence_repair_suffix_lands_on_digest_part_keeps_images():
    cfg = make_cfg(modality="ui")
    ep = ui_episode(3)
    repair = RepairContext(previous_output={"a": 1}, critiques_text="x: y")
    bundle = build_annotate_prompt(ep, cfg, SCHEMA_TEXT, repair=repair,
                                   transitions=SEQ_TRANSITIONS)
    parts = bundle.messages[-1].parts
    # every keyframe image survives; the suffix concatenates onto the ③ text part
    assert [p.kind for p in parts] == [
        "text", "text", "image", "text", "image", "text", "image", "text"]
    assert parts[-2].kind == "image"
    assert parts[-1].text == (digest_section(ep)
                              + '\n[上一版标注] {"a": 1}\n[审核意见] x: y\n请修正后重新输出')
    assert "None" not in parts[-1].text


# ── transitions kwarg threading (S5) ─────────────────────────────────────────

def test_annotate_record_threads_transitions_through_sc_path():
    import asyncio
    from types import SimpleNamespace

    cfg = make_cfg(modality="ui", self_consistency=3)
    engine, metrics = _CapturingEngine(), _CapturingMetrics()
    ctx = SimpleNamespace(cfg=cfg, schema_engine=engine, metrics=metrics, batch_no=1)
    ann = asyncio.run(annotate_record(ui_episode(3), ctx, transitions=SEQ_TRANSITIONS))

    assert ann.sc == {"n": 3, "agreement_ratio": 1.0}
    assert len(engine.prompts) == 3
    for prompt in engine.prompts:                      # every sample carries ① and ③
        parts = prompt.messages[-1].parts
        assert parts[0].text == ACTION_SECTION
        assert parts[-1].kind == "text"
        assert parts[-1].text.startswith("[成员帧摘要]\n")


def test_annotate_record_threads_transitions_through_repair_path():
    import asyncio
    from types import SimpleNamespace

    cfg = make_cfg(modality="ui")
    engine, metrics = _CapturingEngine(), _CapturingMetrics()
    ctx = SimpleNamespace(cfg=cfg, schema_engine=engine, metrics=metrics, batch_no=1)
    repair = RepairContext(previous_output={"intent": "qa"}, critiques_text="a: b")
    asyncio.run(annotate_record(ui_episode(2), ctx, repair=repair,
                                transitions=SEQ_TRANSITIONS))

    (prompt,) = engine.prompts                         # repair skips self-consistency
    parts = prompt.messages[-1].parts
    assert parts[0].text == ACTION_SECTION
    assert parts[-1].kind == "text"
    assert parts[-1].text.endswith(
        '[上一版标注] {"intent": "qa"}\n[审核意见] a: b\n请修正后重新输出')
    assert prompt.temperature is None                  # profile-default temperature


def test_stage_passes_item_transitions():
    import asyncio
    from types import SimpleNamespace
    from labelkit.common.contracts.types import PipelineItem

    cfg = make_cfg(modality="ui")
    engine, metrics = _CapturingEngine(), _CapturingMetrics()
    ctx = SimpleNamespace(cfg=cfg, schema_engine=engine, metrics=metrics, batch_no=1)
    item = PipelineItem(record=ui_episode(2), transitions=SEQ_TRANSITIONS)
    asyncio.run(AnnotateStage(cfg)._annotate_item(item, ctx))

    assert item.status == "active" and item.annotation is not None
    assert engine.prompts[0].messages[-1].parts[0].text == ACTION_SECTION
    ((ev, _payload),) = metrics.events
    assert ev == "annotate.done"


# ── single-record regression anchor (pre-v1.8 byte-identical) ────────────────

def test_single_record_transitions_default_none_regression():
    cfg = make_cfg()
    rec = text_record()
    assert (build_annotate_prompt(rec, cfg, SCHEMA_TEXT)
            == build_annotate_prompt(rec, cfg, SCHEMA_TEXT, transitions=None))
    # a single record IGNORES passed transitions — no sequence sections leak in
    with_steps = build_annotate_prompt(rec, cfg, SCHEMA_TEXT,
                                       transitions=SEQ_TRANSITIONS)
    assert with_steps == build_annotate_prompt(rec, cfg, SCHEMA_TEXT)
    joined = "".join(p.text or "" for m in with_steps.messages for p in m.parts)
    assert "[动作序列]" not in joined and "[成员帧摘要]" not in joined

    ui_cfg = make_cfg(modality="ui")
    ui_rec = ui_record()
    assert (build_annotate_prompt(ui_rec, ui_cfg, SCHEMA_TEXT)
            == build_annotate_prompt(ui_rec, ui_cfg, SCHEMA_TEXT, transitions=None))


# ── v1.11 context-budget packing (spec 3.5.2 v1.11 段, §3.3⑥/V20/V27①) ───────

import asyncio as _asyncio
from dataclasses import replace as _dc_replace
from types import SimpleNamespace as _NS

from labelkit.common.errors import ContextOverflowError, OutputTruncatedError
from labelkit.common.runtime import budget as budget_mod


def _budget_profile(context_window: int, name: str = "default"):
    from labelkit.common.config.model import LLMProfile
    return LLMProfile(name=name, provider="openai_compatible", base_url="http://x",
                      model="m", api_key_env="K", max_output_tokens=256,
                      context_window=context_window)


def budget_cfg(context_window: int, **kw) -> ResolvedConfig:
    return _dc_replace(make_cfg(**kw),
                       llm_profiles={"default": _budget_profile(context_window)})


class _FixedCalibrator:
    def __init__(self, value: int):
        self.value = value

    def cost(self, profile: str) -> int:
        return self.value


class _BudgetMetrics:
    def __init__(self):
        self.counters: dict[str, int] = {}
        self.events: list = []
        self.fed: list = []

    def count(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n

    def event(self, ev, **kw):
        self.events.append((ev, dict(kw.get("payload") or {})))

    def record_provider_result(self, fatal, *, hard=False):
        self.fed.append(fatal)


def budget_ctx(cfg, engine, image_cost: int = 300) -> _NS:
    return _NS(cfg=cfg, llm=_NS(calibrator=_FixedCalibrator(image_cost)),
               schema_engine=engine, metrics=_BudgetMetrics(), batch_no=1)


class _PromptEngine:
    """Captures prompts; optionally raises fresh reactive overflows first."""
    user_schema_text = SCHEMA_TEXT

    def __init__(self, n_overflows: int = 0):
        self.prompts = []
        self.n_overflows = n_overflows

    async def complete_validated(self, profile, prompt, *, record_ids, batch_no,
                                 record=None):
        from labelkit.common.contracts.types import Usage
        self.prompts.append(prompt)
        if self.n_overflows > 0:
            self.n_overflows -= 1
            raise ContextOverflowError("provider overflow", phase="reactive")
        return ({"intent": "qa", "topic": "t", "difficulty": "easy"},
                Usage(1, 1), 1, "m")


def _images(prompt) -> list:
    return [p.image for p in prompt.messages[-1].parts if p.kind == "image"]


def test_build_prompt_k_eff_caps_below_config_first_last_kept():
    cfg = make_cfg(modality="ui", sequence_frames=20)
    ep = ui_episode(25)
    bundle = build_annotate_prompt(ep, cfg, SCHEMA_TEXT, k_eff=6)
    kept = _keyframe_indexes(25, 6)
    assert [p for p in _images(bundle)] == [ep.members[m].image for m in kept]
    assert kept[0] == 0 and kept[-1] == 24         # first/last invariant
    # min with the config value: k_eff above the config cap is inert
    assert build_annotate_prompt(ep, cfg, SCHEMA_TEXT, k_eff=99) == (
        build_annotate_prompt(ep, cfg, SCHEMA_TEXT))


def test_build_prompt_image_px_rides_bundle():
    cfg = make_cfg(modality="ui")
    ep = ui_episode(3)
    assert build_annotate_prompt(ep, cfg, SCHEMA_TEXT).image_px is None
    assert build_annotate_prompt(ep, cfg, SCHEMA_TEXT, image_px=1536).image_px == 1536


def test_build_prompt_none_none_byte_identical():
    # None/None = pre-v1.11 byte-identical (frozen-signature anchor).
    cfg = make_cfg(modality="ui")
    ep = ui_episode(25)
    assert build_annotate_prompt(ep, cfg, SCHEMA_TEXT, transitions=SEQ_TRANSITIONS) == \
        build_annotate_prompt(ep, cfg, SCHEMA_TEXT, transitions=SEQ_TRANSITIONS,
                              k_eff=None, image_px=None)


def test_budget_k_eff_layer_images_eat_remainder():
    # §3.3⑥③: images eat what the counted static+text side leaves —
    # k_eff = min(cap, max(2, ⌊remaining/cost⌋)), first/last always kept.
    cfg = budget_cfg(4096, modality="ui", sequence_frames=20)
    ep = ui_episode(25)
    engine = _PromptEngine()
    ctx = budget_ctx(cfg, engine, image_cost=300)
    ann = _asyncio.run(annotate_record(ep, ctx))
    assert ann is not None
    (prompt,) = engine.prompts
    images = _images(prompt)
    assert 2 <= len(images) < 20                   # shrunk below the config cap
    assert images[0] is ep.members[0].image        # first member kept
    assert images[-1] is ep.members[24].image      # last member kept
    prof = cfg.llm_profiles["default"]
    est = budget_mod.est_prompt(prompt, prof, None, image_cost=300)
    assert est <= budget_mod.input_budget(prof)    # throat invariant honoured

    # determinism: identical re-run → identical prompt and k
    engine2 = _PromptEngine()
    _asyncio.run(annotate_record(ui_episode(25), budget_ctx(cfg, engine2,
                                                            image_cost=300)))
    assert engine2.prompts[0] == prompt


def test_budget_k_floor_then_text_blocks_trim():
    # §3.3⑥④: at the k=2 floor the text blocks trim (edges) — digests are the
    # last to yield; the assembled prompt then fits.
    cfg = budget_cfg(2000, modality="ui", sequence_frames=20)
    ep = ui_episode(25)
    engine = _PromptEngine()
    ctx = budget_ctx(cfg, engine, image_cost=500)
    _asyncio.run(annotate_record(ep, ctx))
    (prompt,) = engine.prompts
    assert len(_images(prompt)) == 2               # keyframe floor
    digest_part = prompt.messages[-1].parts[-1].text
    assert "…(truncated " in digest_part           # §3.3⑤ edges marker in place
    assert ctx.metrics.counters["budget.truncations.annotate"] >= 1
    prof = cfg.llm_profiles["default"]
    assert budget_mod.est_prompt(prompt, prof, None, image_cost=500) <= \
        budget_mod.input_budget(prof)


def test_budget_minimal_unit_unfittable_fails_record():
    # V10: a plain text record has no trimmable block — unfittable rejects the
    # record (kind=context_overflow, budget.overflow_records) with NO request.
    cfg = budget_cfg(600)                          # input_budget = 88
    from labelkit.common.contracts.types import PipelineItem
    item = PipelineItem(record=text_record("长" * 800))
    engine = _PromptEngine()
    ctx = budget_ctx(cfg, engine)
    _asyncio.run(AnnotateStage(cfg)._annotate_item(item, ctx))
    assert engine.prompts == []                    # doomed request never sent
    assert item.status == "failed"
    assert item.errors[0].kind == "context_overflow"
    assert ctx.metrics.counters["budget.overflow_records"] == 1


def test_v20_reactive_degrade_halves_keyframes():
    cfg = budget_cfg(8192, modality="ui", sequence_frames=8)
    ep = ui_episode(25)
    engine = _PromptEngine(n_overflows=1)
    ctx = budget_ctx(cfg, engine, image_cost=100)
    _asyncio.run(annotate_record(ep, ctx))
    assert len(engine.prompts) == 2
    k1 = len(_images(engine.prompts[0]))
    k2 = len(_images(engine.prompts[1]))
    assert k1 == 8                                 # roomy budget: config cap
    assert k2 == max(2, -(-k1 // 2))               # V20: k → max(2, ⌈k/2⌉)
    assert ctx.metrics.counters["budget.degrade_retries"] == 1
    assert ctx.metrics.fed == []                   # successful degrade: no feed


def test_v20_degrades_bounded_then_terminal_feeds_once():
    cfg = budget_cfg(8192, modality="ui", sequence_frames=8)
    ep = ui_episode(25)
    engine = _PromptEngine(n_overflows=99)         # never recovers
    ctx = budget_ctx(cfg, engine, image_cost=100)
    from labelkit.common.contracts.types import PipelineItem
    item = PipelineItem(record=ep)
    _asyncio.run(AnnotateStage(cfg)._annotate_item(item, ctx))
    assert len(engine.prompts) == 3                # initial + 2 bounded degrades
    assert [len(_images(p)) for p in engine.prompts] == [8, 4, 2]
    assert ctx.metrics.counters["budget.degrade_retries"] == 2
    assert ctx.metrics.fed == [True]               # A7: reactive-400 fed ONCE
    assert item.status == "failed"
    assert item.errors[0].kind == "context_overflow"
    assert ctx.metrics.counters["budget.overflow_records"] == 1


def test_budget_off_no_degrade_and_precise_kinds():
    # cw == 0: the packing/degrade machinery is dead code — a finish-oracle
    # overflow propagates once, unfed, with the precise kind at the stage.
    cfg = make_cfg()                               # llm_profiles={} → budget off

    class _Overflowing:
        user_schema_text = SCHEMA_TEXT
        def __init__(self):
            self.calls = 0
        async def complete_validated(self, *a, **k):
            self.calls += 1
            exc = ContextOverflowError("finish oracle", phase="reactive")
            exc.origin = "finish"
            raise exc

    engine = _Overflowing()
    ctx = _NS(cfg=cfg, llm=None, schema_engine=engine,
              metrics=_BudgetMetrics(), batch_no=1)
    from labelkit.common.contracts.types import PipelineItem
    item = PipelineItem(record=text_record())
    _asyncio.run(AnnotateStage(cfg)._annotate_item(item, ctx))
    assert engine.calls == 1                       # no degrade retry surface
    assert item.errors[0].kind == "context_overflow"
    assert ctx.metrics.fed == []                   # origin="finish": never fed
    assert "budget.degrade_retries" not in ctx.metrics.counters

    item2 = PipelineItem(record=text_record())

    class _Truncating:
        user_schema_text = SCHEMA_TEXT
        async def complete_validated(self, *a, **k):
            raise OutputTruncatedError("cap")

    ctx2 = _NS(cfg=cfg, llm=None, schema_engine=_Truncating(),
               metrics=_BudgetMetrics(), batch_no=1)
    _asyncio.run(AnnotateStage(cfg)._annotate_item(item2, ctx2))
    assert item2.errors[0].kind == "output_truncated"
    assert "budget.overflow_records" not in ctx2.metrics.counters


# ── v1.12 frame-level per-member annotation (SPEC-frame-annotation §3.3) ─────

from labelkit.common.errors import SchemaViolation

FRAME_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "entities": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["intent", "entities"],
    "additionalProperties": False,
}
FRAME_SCHEMA_TEXT = json.dumps(FRAME_SCHEMA, ensure_ascii=False,
                               separators=(", ", ": "))
FRAME_INSTRUCTION = "标注该帧的意图与实体。"
FRAME_OVERRIDE_INSTRUCTION = "标注任务请求帧：给出 intent 与 entities。"
FRAME_EXAMPLE = FewShotExample(
    input="帮我订明天去上海的高铁",
    output={"intent": "book_train", "entities": ["上海", "明天"]})


def make_frame_cfg(*, frame_examples=(), views=None, classified=False,
                   **kw) -> ResolvedConfig:
    """make_cfg（或 classified=True 时 make_classified_cfg）+ 帧粒度三件套：
    frame_annotate 开、frame_class_views、frame_schema 解析产物。"""
    base = make_classified_cfg(**kw) if classified else make_cfg(**kw)
    return _dc_replace(
        base,
        frame_annotate=FrameAnnotateConfig(
            enabled=True, llm="default", instruction=FRAME_INSTRUCTION,
            examples=tuple(frame_examples),
            schema_inline=json.dumps(FRAME_SCHEMA)),
        frame_class_views=views or {},
        frame_schema=FRAME_SCHEMA)


def frame_views(*, skip_chitchat=True):
    """三个帧类视图：task_request 覆盖指令 + few-shot；chitchat 默认跳过
    （enabled=false 省成本面）；other 零覆盖（继承全局）。"""
    return {
        "task_request": FrameClassView(instruction=FRAME_OVERRIDE_INSTRUCTION,
                                       examples=(FRAME_EXAMPLE,), enabled=True),
        "chitchat": FrameClassView(instruction=FRAME_INSTRUCTION, examples=(),
                                   enabled=not skip_chitchat),
        "other": FrameClassView(instruction=FRAME_INSTRUCTION, examples=(),
                                enabled=True),
    }


def member_cls(label: str) -> Classification:
    return Classification(label=label, labels=(label,), source="llm", detail={})


class _FrameEngine:
    """捕获全部调用（profile/prompt/schema/record_ids）；fail_ids 内的 id 抛
    SchemaViolation（修复穷尽形）。schema=None = 序列级用户 Schema 调用，
    非 None = 帧级显式 Schema 调用——两路由靠该参数区分。"""
    user_schema_text = SCHEMA_TEXT

    def __init__(self, fail_ids=(), frame_output=None):
        self.calls = []      # (profile, prompt, schema, record_ids)
        self.fail_ids = set(fail_ids)
        self.frame_output = frame_output or {"intent": "book_train",
                                             "entities": ["上海"]}

    async def complete_validated(self, profile, prompt, schema=None, *,
                                 record_ids, batch_no, record=None):
        self.calls.append((profile, prompt, schema, record_ids))
        if record_ids and record_ids[0] in self.fail_ids:
            raise SchemaViolation(["/intent: 枚举违规"], "{}")
        if schema is None:                   # sequence-level user-schema call
            return ({"intent": "qa", "topic": "t", "difficulty": "easy"},
                    Usage(1, 1), 1, "m")
        return (dict(self.frame_output), Usage(1, 1), 2, "m")

    def frame_calls(self):
        return [c for c in self.calls if c[2] is not None]


class _FrameMetrics:
    def __init__(self):
        self.events = []     # (ev, record_ids, payload)
        self.counters: dict[str, int] = {}

    def event(self, ev, **kw):
        self.events.append((ev, tuple(kw.get("record_ids") or ()),
                            dict(kw.get("payload") or {})))

    def count(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n


def run_annotate_item(cfg, item, engine=None, metrics=None):
    engine = engine or _FrameEngine()
    metrics = metrics or _FrameMetrics()
    ctx = _NS(cfg=cfg, schema_engine=engine, metrics=metrics, batch_no=1)
    _asyncio.run(AnnotateStage(cfg)._annotate_item(item, ctx))
    return engine, metrics


def frame_event_payloads(metrics):
    return [(ids, p) for ev, ids, p in metrics.events if ev == "annotate.frame"]


def text_episode(n: int = 3, ep_id: str = "ep0001") -> Record:
    return make_episode(tuple(text_member(i) for i in range(n)), ep_id)


# ── frame prompt assembly (§10.13 形态) ──────────────────────────────────────

def test_frame_prompt_text_sections_schema_embedding_and_few_shot():
    cfg = make_frame_cfg(frame_examples=(FRAME_EXAMPLE,))
    member = text_member(0)
    bundle = build_frame_annotate_prompt(member, cfg, FRAME_SCHEMA_TEXT)

    # 段序：system → few-shot（每条一条 user 消息）→ 成员内容
    assert [m.role for m in bundle.messages] == ["system", "user", "user"]
    assert bundle.messages[0].parts[0].text == (
        "[任务]\n标注该帧的意图与实体。\n"
        "输出必须是符合以下 JSON Schema 的单个 JSON 对象，不输出任何其他内容：\n"
        + FRAME_SCHEMA_TEXT
    )
    assert bundle.messages[1].parts[0].text == (
        "[示例输入] 帮我订明天去上海的高铁\n[示例输出] "
        '{"intent": "book_train", "entities": ["上海", "明天"]}'
    )
    (part,) = bundle.messages[2].parts
    assert part.text == "[成员帧] 步骤文本0"
    # 无自洽采样、无独立图像尺寸：profile 默认温度、工作点 px
    assert bundle.temperature is None and bundle.image_px is None


def test_frame_prompt_ui_three_parts_and_tree_cap():
    cfg = make_frame_cfg(modality="ui")
    member = ui_member(2)
    bundle = build_frame_annotate_prompt(member, cfg, FRAME_SCHEMA_TEXT)
    msg = bundle.messages[-1]
    assert [p.kind for p in msg.parts] == ["text", "image", "text"]
    assert msg.parts[0].text == "[屏幕截图]"
    assert msg.parts[1].image is member.image
    expected_tree = member.ui_tree.serialize(max_chars=cfg.input.ui_tree_max_chars)
    assert msg.parts[2].text == "[UI 控件树]\n" + expected_tree

    capped = make_frame_cfg(modality="ui", ui_tree_max_chars=40)
    body = (build_frame_annotate_prompt(member, capped, FRAME_SCHEMA_TEXT)
            .messages[-1].parts[2].text.removeprefix("[UI 控件树]\n"))
    assert len(body) <= 40
    assert "…(truncated" in body


def test_frame_prompt_class_view_override_and_global_fallback():
    cfg = make_frame_cfg(views=frame_views())
    member = text_member(1)
    over = build_frame_annotate_prompt(member, cfg, FRAME_SCHEMA_TEXT,
                                       label="task_request")
    assert over.messages[0].parts[0].text.startswith(
        "[任务]\n" + FRAME_OVERRIDE_INSTRUCTION + "\n")
    assert over.messages[1].parts[0].text.startswith(
        "[示例输入] 帮我订明天去上海的高铁")
    # label=None ⇒ 全局 [frame.annotate] 指令与 few-shot（此处为空）
    plain = build_frame_annotate_prompt(member, cfg, FRAME_SCHEMA_TEXT)
    assert [m.role for m in plain.messages] == ["system", "user"]
    assert plain.messages[0].parts[0].text.startswith(
        "[任务]\n" + FRAME_INSTRUCTION + "\n")


# ── annotate_member：显式 Schema 路由 + 失败语义 + 溢出纪律 ──────────────────

def test_annotate_member_routes_explicit_frame_schema():
    cfg = make_frame_cfg()
    engine, metrics = _FrameEngine(), _FrameMetrics()
    ctx = _NS(cfg=cfg, schema_engine=engine, metrics=metrics, batch_no=3)
    member = text_member(0)
    ann = _asyncio.run(annotate_member(member, ctx))
    assert ann is not None
    assert ann.output == {"intent": "book_train", "entities": ["上海"]}
    assert ann.attempts == 2 and ann.sc is None
    ((profile, _prompt, schema, record_ids),) = engine.calls
    assert profile == "default"
    assert schema == FRAME_SCHEMA          # 显式 schema= 内部 Schema 待遇，绝非 None
    assert record_ids == (member.id,)
    assert metrics.counters == {"frame_annotate.annotated": 1}


def test_annotate_member_failure_swallows_counts_and_returns_none():
    cfg = make_frame_cfg()
    member = text_member(0)
    engine = _FrameEngine(fail_ids={member.id})
    metrics = _FrameMetrics()
    ctx = _NS(cfg=cfg, schema_engine=engine, metrics=metrics, batch_no=1)
    ann = _asyncio.run(annotate_member(member, ctx))    # 不抛（成员级隔离）
    assert ann is None
    assert metrics.counters == {"frame_annotate.failed": 1}


def test_annotate_member_precheck_overflow_fails_member_never_sends():
    # 最小单元（text 成员无可裁块，无降级梯）：precheck 溢出 ⇒ 成员失败（None），
    # 请求不发、不喂熔断、不计 overflow_records（成员失败非记录 reject）。
    cfg = _dc_replace(make_frame_cfg(),
                      llm_profiles={"default": _budget_profile(600)})
    member = replace(text_member(0), text="长" * 800)
    engine = _FrameEngine()
    ctx = budget_ctx(cfg, engine)
    ann = _asyncio.run(annotate_member(member, ctx))
    assert ann is None
    assert engine.calls == []                       # doomed request never sent
    assert ctx.metrics.counters.get("frame_annotate.failed") == 1
    assert ctx.metrics.fed == []                    # precheck 永不喂熔断（A7）
    assert "budget.overflow_records" not in ctx.metrics.counters
    assert "budget.degrade_retries" not in ctx.metrics.counters


def test_annotate_member_reactive_overflow_feeds_terminal_once():
    cfg = _dc_replace(make_frame_cfg(),
                      llm_profiles={"default": _budget_profile(131072)})
    member = text_member(0)

    class _OverflowEngine:
        async def complete_validated(self, *a, **k):
            raise ContextOverflowError("provider overflow", phase="reactive")

    ctx = budget_ctx(cfg, _OverflowEngine())
    assert _asyncio.run(annotate_member(member, ctx)) is None
    assert ctx.metrics.fed == [True]                # 反应式 http_400 恰一次喂
    assert ctx.metrics.counters.get("frame_annotate.failed") == 1

    class _FinishEngine:
        async def complete_validated(self, *a, **k):
            exc = ContextOverflowError("finish oracle", phase="reactive")
            exc.origin = "finish"
            raise exc

    ctx2 = budget_ctx(cfg, _FinishEngine())
    assert _asyncio.run(annotate_member(member, ctx2)) is None
    assert ctx2.metrics.fed == []                   # 200 形终局 oracle 永不喂


def test_annotate_member_ui_tree_trims_under_budget():
    cfg = _dc_replace(make_frame_cfg(modality="ui"),
                      llm_profiles={"default": _budget_profile(2000)})
    nodes = [UINode("1", None, 0, "FrameLayout", "", "",
                    (0, 0, 1080, 1920), True, {})]
    nodes += [UINode(str(i), "1", 1, "TextView",
                     f"文本节点内容第{i}行" + "长" * 40, "",
                     (0, i, 100, i + 1), True, {}) for i in range(2, 120)]
    member = Record(id="fatframe", modality="ui", text=None, raw=None,
                    ui_tree=UITree(tuple(nodes)),
                    image=ImageRef(path=Path("f.png"), format="png",
                                   size_bytes=1),
                    ref=RecordRef("frames/x.jsonl", None, 9, ()))
    engine = _FrameEngine()
    ctx = budget_ctx(cfg, engine, image_cost=300)
    ann = _asyncio.run(annotate_member(member, ctx))
    assert ann is not None
    ((_, prompt, _, _),) = engine.calls
    tree_part = prompt.messages[-1].parts[2].text
    assert "…(truncated" in tree_part and "nodes)" in tree_part
    assert ctx.metrics.counters.get("budget.truncations.frame_annotate", 0) >= 1
    prof = cfg.llm_profiles["default"]
    assert budget_mod.est_prompt(prompt, prof, None, image_cost=300) <= \
        budget_mod.input_budget(prof)               # throat invariant honoured


# ── stage 帧 pass：占键语义 / 幂等 / 执行门 / 计数与事件 ─────────────────────

def test_frame_pass_annotates_members_events_and_counters():
    cfg = make_frame_cfg()
    ep = text_episode(3)
    item = PipelineItem(record=ep)                  # 无 classification = 首标签
    engine, metrics = run_annotate_item(cfg, item)

    assert item.status == "active" and item.annotation is not None   # 序列级照常
    assert set(item.member_annotations) == {m.id for m in ep.members}
    assert all(a is not None for a in item.member_annotations.values())
    assert metrics.counters.get("frame_annotate.annotated") == 3
    assert "frame_annotate.failed" not in metrics.counters
    # 帧调用走显式 frame_schema；每成员 record_ids=(member_id,)
    assert len(engine.frame_calls()) == 3
    assert all(c[2] == FRAME_SCHEMA for c in engine.frame_calls())
    # annotate.frame 每成员一发，ids=(episode_id,)，payload 三键闭集
    events = frame_event_payloads(metrics)
    assert len(events) == 3
    for ids, payload in events:
        assert ids == (ep.id,)
        assert payload["status"] == "annotated" and payload["attempts"] == 2
        assert set(payload) == {"member_id", "status", "attempts"}
    assert {p["member_id"] for _, p in events} == {m.id for m in ep.members}


def test_frame_pass_duplicate_member_ids_single_call():
    # 终审缺陷修复（内容哈希 id 碰撞）：同 id 成员只调用一次——防同键并发
    # 双付费与 annotated 计数虚高（first-wins，与 M13 帧判决落表同口径）。
    from dataclasses import replace as _replace
    cfg = make_frame_cfg()
    ep = text_episode(2)
    dup_ep = _replace(ep, members=(ep.members[0], ep.members[0], ep.members[1]))
    item = PipelineItem(record=dup_ep)
    engine, metrics = run_annotate_item(cfg, item)

    assert set(item.member_annotations) == {m.id for m in ep.members}
    assert len(engine.frame_calls()) == 2               # 唯一 id 各一次
    assert metrics.counters.get("frame_annotate.annotated") == 2


def test_frame_pass_class_skip_takes_no_key():
    cfg = make_frame_cfg(views=frame_views())
    ep = text_episode(3)
    item = PipelineItem(record=ep)
    item.member_classifications = {
        ep.members[0].id: member_cls("task_request"),
        ep.members[1].id: member_cls("chitchat"),   # 视图 enabled=false ⇒ 跳过
        ep.members[2].id: member_cls("other"),
    }
    engine, metrics = run_annotate_item(cfg, item)

    assert set(item.member_annotations) == {ep.members[0].id, ep.members[2].id}
    assert ep.members[1].id not in item.member_annotations   # skipped 不占键
    assert metrics.counters.get("frame_annotate.skipped") == 1
    assert metrics.counters.get("frame_annotate.annotated") == 2
    skipped = [p for _, p in frame_event_payloads(metrics)
               if p["status"] == "skipped"]
    assert skipped == [{"member_id": ep.members[1].id, "status": "skipped",
                        "attempts": 0}]
    # 类覆盖/全局指令都到达请求（task_request 覆盖指令；other 继承全局）
    sys_texts = [c[1].messages[0].parts[0].text for c in engine.frame_calls()]
    assert any(t.startswith("[任务]\n" + FRAME_OVERRIDE_INSTRUCTION)
               for t in sys_texts)
    assert any(t.startswith("[任务]\n" + FRAME_INSTRUCTION) for t in sys_texts)


def test_frame_pass_failure_occupies_key_with_none():
    cfg = make_frame_cfg()
    ep = text_episode(3)
    bad = ep.members[1].id
    item = PipelineItem(record=ep)
    engine, metrics = run_annotate_item(cfg, item,
                                        engine=_FrameEngine(fail_ids={bad}))

    assert set(item.member_annotations) == {m.id for m in ep.members}
    assert item.member_annotations[bad] is None          # failed 占键 None
    assert item.member_annotations[ep.members[0].id] is not None
    assert metrics.counters.get("frame_annotate.failed") == 1
    assert metrics.counters.get("frame_annotate.annotated") == 2
    failed = [p for _, p in frame_event_payloads(metrics)
              if p["status"] == "failed"]
    assert failed == [{"member_id": bad, "status": "failed", "attempts": 0}]
    # 成员失败非信封失败：episode 照常发射、item.errors 不写
    assert item.status == "active" and not item.errors


def test_frame_pass_idempotent_fills_missing_keys_only():
    cfg = make_frame_cfg()
    ep = text_episode(3)
    item = PipelineItem(record=ep)
    seeded = Annotation(output={"intent": "seed", "entities": []}, model="m0",
                        attempts=1, usage=Usage())
    existing = {ep.members[0].id: seeded}
    item.member_annotations = existing
    engine, metrics = run_annotate_item(cfg, item)

    assert item.member_annotations is existing      # 只补缺位，从不换 dict 对象
    assert item.member_annotations[ep.members[0].id] is seeded   # 已占键不重跑
    called = {c[3][0] for c in engine.frame_calls()}
    assert called == {ep.members[1].id, ep.members[2].id}
    assert metrics.counters.get("frame_annotate.annotated") == 2


def test_frame_pass_gate_first_label_clone_and_degraded():
    cfg = make_frame_cfg(classified=True)
    ep = text_episode(2)
    # 非首标签克隆（label != labels[0]）：帧 pass 不跑，dict 保持 None
    clone = PipelineItem(record=ep, classification=Classification(
        label="qa", labels=("writing", "qa"), source="llm", detail={}))
    engine, metrics = run_annotate_item(cfg, clone)
    assert clone.annotation is not None             # 序列级照常
    assert clone.member_annotations is None
    assert engine.frame_calls() == []
    assert frame_event_payloads(metrics) == []
    # 首标签信封照常执行
    first = PipelineItem(record=ep, classification=Classification(
        label="writing", labels=("writing", "qa"), source="llm", detail={}))
    engine2, _ = run_annotate_item(cfg, first)
    assert set(first.member_annotations) == {m.id for m in ep.members}
    assert len(engine2.frame_calls()) == 2
    # 降格会话（segment_degraded duck 标）：跳过，保持 None
    degraded = PipelineItem(record=ep)
    degraded.segment_degraded = {"stage": "segment", "kind": "provider_fatal",
                                 "message": "x"}
    engine3, metrics3 = run_annotate_item(cfg, degraded)
    assert degraded.annotation is not None
    assert degraded.member_annotations is None
    assert engine3.frame_calls() == []
    assert frame_event_payloads(metrics3) == []


def test_frame_pass_gate_switch_and_kind():
    # 开关关（默认 FrameAnnotateConfig()）：序列信封不跑帧 pass
    off = make_cfg()
    item = PipelineItem(record=text_episode(2))
    engine, metrics = run_annotate_item(off, item)
    assert item.annotation is not None
    assert item.member_annotations is None
    assert frame_event_payloads(metrics) == []
    # 开关开但非序列记录（kind=="single"）：同样不跑
    on = make_frame_cfg()
    single = PipelineItem(record=text_record())
    engine2, metrics2 = run_annotate_item(on, single)
    assert single.annotation is not None
    assert single.member_annotations is None
    assert engine2.frame_calls() == []
    assert frame_event_payloads(metrics2) == []


def test_frame_pass_not_run_when_sequence_annotation_fails():
    cfg = make_frame_cfg()
    ep = text_episode(2)
    item = PipelineItem(record=ep)
    engine, metrics = run_annotate_item(cfg, item,
                                        engine=_FrameEngine(fail_ids={ep.id}))
    assert item.status == "failed"                  # 序列级修复穷尽
    assert item.member_annotations is None          # 帧 pass 未运行
    assert len(engine.calls) == 1                   # 仅序列级一发，无帧调用
    assert frame_event_payloads(metrics) == []
    assert "frame_annotate.annotated" not in metrics.counters


def test_frame_pass_initializes_empty_dict_when_all_skipped():
    # dict 初始化语义：pass 运行即 {}（≠ 未运行的 None），全员跳过也如此。
    cfg = make_frame_cfg(views=frame_views())
    ep = text_episode(2)
    item = PipelineItem(record=ep)
    item.member_classifications = {m.id: member_cls("chitchat")
                                   for m in ep.members}
    engine, metrics = run_annotate_item(cfg, item)
    assert item.member_annotations == {}            # 已运行、全员 skipped 不占键
    assert item.member_annotations is not None
    assert engine.frame_calls() == []
    assert metrics.counters.get("frame_annotate.skipped") == 2


def test_frame_event_excerpt_discipline_by_tier():
    ep = text_episode(1)
    long_output = {"intent": "长" * 300, "entities": []}
    for content in ("none", "refs"):
        cfg = make_frame_cfg(trace=TraceConfig(
            enabled=True, channels=("annotate",), content=content))
        item = PipelineItem(record=ep)
        _, metrics = run_annotate_item(cfg, item)
        ((_, payload),) = frame_event_payloads(metrics)
        assert set(payload) == {"member_id", "status", "attempts"}   # 无 excerpt
    for content in ("excerpt", "full"):
        cfg = make_frame_cfg(trace=TraceConfig(
            enabled=True, channels=("annotate",), content=content))
        item = PipelineItem(record=ep)
        engine = _FrameEngine(frame_output=long_output)
        _, metrics = run_annotate_item(cfg, item, engine=engine)
        ((_, payload),) = frame_event_payloads(metrics)
        # 标注内容仅经既有 excerpt 键，200 字截断；无其他数据内容键
        assert set(payload) == {"member_id", "status", "attempts", "excerpt"}
        expected = json.dumps(long_output, ensure_ascii=False)[:200]
        assert payload["excerpt"] == {ep.members[0].id: expected}
        assert len(payload["excerpt"][ep.members[0].id]) == 200


# ── v1.13 按序列类标注 Schema（裁决·按类标注 Schema，SPEC §3.4 六消费点）─────

import pytest as _pytest

from labelkit.operators.annotate import (
    class_annotate_schema,
    class_effective_schema,
    class_schema_text,
)

CLASS_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string"},
        "kind": {"type": "string", "enum": ["book", "cancel"]},
    },
    "required": ["task", "kind"],
    "additionalProperties": False,
}

CLASS_SCHEMA_TEXT = json.dumps(CLASS_SCHEMA, ensure_ascii=False,
                               separators=(", ", ": "))


def schema_cfg(**kw) -> ResolvedConfig:
    """make_classified_cfg + 'writing' 类声明按类标注 Schema 覆盖；'qa' 类零
    覆盖（schema=None）——两类共同构成「按类 vs 回落全局」的对照面。"""
    base = make_classified_cfg(**kw)
    views = dict(base.class_views)
    views["writing"] = replace(views["writing"], schema=CLASS_SCHEMA)
    return replace(base, class_views=views)


class _SchemaRoutingEngine:
    """记录每次 M8 调用的 (schema, user_treatment, record)；序列级用户 Schema
    调用的既有形态是两者皆 None——v1.12 字节等价的判定面。"""

    user_schema_text = SCHEMA_TEXT

    def __init__(self, outputs=None):
        self.calls = []
        self._outputs = list(outputs or [])

    async def complete_validated(self, profile, prompt, schema=None, *,
                                 record_ids, batch_no, record=None,
                                 user_treatment=None):
        self.calls.append(_NS(profile=profile, prompt=prompt, schema=schema,
                              user_treatment=user_treatment, record=record))
        output = (self._outputs.pop(0) if self._outputs
                  else {"task": "订票", "kind": "book"})
        return output, Usage(1, 1), 1, "m"


def test_class_effective_schema_single_point_and_three_fallbacks():
    cfg = schema_cfg()
    assert class_annotate_schema(cfg, "writing") == CLASS_SCHEMA
    assert class_effective_schema(cfg, "writing") == CLASS_SCHEMA
    # 三反例：零覆盖类 / 类表外未知类 / label 缺失 —— 全部回落全局 user_schema
    for label in ("qa", "no_such_class", None):
        assert class_annotate_schema(cfg, label) is None
        assert class_effective_schema(cfg, label) is cfg.user_schema
    # classify 关闭（class_views == {}）：任何标签都回落全局
    plain = make_cfg()
    assert class_annotate_schema(plain, "writing") is None
    assert class_effective_schema(plain, "writing") is plain.user_schema


def test_class_schema_text_computed_per_class():
    cfg = schema_cfg()
    ctx = _NS(cfg=cfg, schema_engine=_NS(user_schema_text=SCHEMA_TEXT))
    # 按类现算（帧侧先例同款 canonical 单行 dump）
    assert class_schema_text(ctx, "writing") == CLASS_SCHEMA_TEXT
    # 无覆盖 ⇒ 沿用 M8 既有属性对象本身（字节等价）
    assert class_schema_text(ctx, "qa") is SCHEMA_TEXT
    assert class_schema_text(ctx, None) is SCHEMA_TEXT


def test_class_schema_call_routes_explicit_schema_with_user_treatment():
    cfg = schema_cfg()
    engine = _SchemaRoutingEngine()
    ctx = _NS(cfg=cfg, schema_engine=engine, metrics=_CapturingMetrics(),
              batch_no=1)
    record = text_record()
    item = PipelineItem(record=record,
                        classification=_classification("writing"))
    _asyncio.run(AnnotateStage(cfg)._annotate_item(item, ctx))

    (call,) = engine.calls
    assert item.status == "active" and item.annotation is not None
    assert call.schema == CLASS_SCHEMA
    assert call.schema is not cfg.class_views["writing"].schema   # 防御性拷贝
    assert call.user_treatment is True          # L2.5 与 resolved_at 记账保留
    assert call.record == record.raw            # L2.5 回调仍拿到原始映射
    # 提示词 system 段内嵌按类现算的 Schema 文本，而非全局文本
    system_text = call.prompt.messages[0].parts[0].text
    assert system_text.endswith(CLASS_SCHEMA_TEXT)
    assert SCHEMA_TEXT not in system_text


def test_class_without_schema_keeps_v112_call_shape():
    cfg = schema_cfg()
    engine = _SchemaRoutingEngine()
    ctx = _NS(cfg=cfg, schema_engine=engine, metrics=_CapturingMetrics(),
              batch_no=1)
    zero_override = PipelineItem(record=text_record(),
                                 classification=_classification("qa"))
    unclassified = PipelineItem(record=text_record())       # classification None
    _asyncio.run(AnnotateStage(cfg)._annotate_item(zero_override, ctx))
    _asyncio.run(AnnotateStage(cfg)._annotate_item(unclassified, ctx))

    for call in engine.calls:
        # 既有推断路径：schema 与 user_treatment 都不传（v1.12 调用形字节等价）
        assert (call.schema, call.user_treatment) == (None, None)
        assert call.prompt.messages[0].parts[0].text.endswith(SCHEMA_TEXT)


def test_self_consistency_vote_uses_class_effective_schema():
    # 样本在 kind（按类 Schema 的枚举字段）上 1:2 分歧；全局 Schema 的可投票
    # 字段（intent/difficulty 枚举）在样本中缺席 ⇒ 两条取值路线结论不同。
    samples = [{"task": "A", "kind": "book"},
               {"task": "B", "kind": "cancel"},
               {"task": "C", "kind": "cancel"}]
    cfg = schema_cfg(self_consistency=3)

    engine = _SchemaRoutingEngine(list(samples))
    ctx = _NS(cfg=cfg, schema_engine=engine, metrics=_CapturingMetrics(),
              batch_no=1)
    ann = _asyncio.run(annotate_record(text_record(), ctx, label="writing"))
    assert ann.output == {"task": "B", "kind": "cancel"}      # kind 多数派
    assert ann.sc == {"n": 3, "agreement_ratio": 2 / 3}

    engine2 = _SchemaRoutingEngine(list(samples))
    ctx2 = _NS(cfg=cfg, schema_engine=engine2, metrics=_CapturingMetrics(),
               batch_no=1)
    ann2 = _asyncio.run(annotate_record(text_record(), ctx2, label="qa"))
    assert ann2.output == {"task": "A", "kind": "book"}       # 无分歧，取样本 #1
    assert ann2.sc == {"n": 3, "agreement_ratio": 1.0}


def _structured_profile(context_window: int, name: str = "default"):
    from labelkit.common.config.model import LLMProfile
    return LLMProfile(name=name, provider="openai_compatible", base_url="http://x",
                      model="m", api_key_env="K", max_output_tokens=256,
                      context_window=context_window,
                      supports_structured_output=True)


def test_pack_prompt_schema_est_prices_class_effective_schema():
    # V13③ 计价对象必须与调用 Schema 同源：巨大的按类 Schema 抬高 schema_est ⇒
    # 单条文本记录在最小单元上 V10 溢出，注定失败的请求永不发出。
    big = {"type": "object",
           "properties": {"task": {"type": "string", "description": "凑" * 20000}},
           "required": ["task"]}
    base = make_classified_cfg()
    views = dict(base.class_views)
    views["writing"] = replace(views["writing"], schema=big)
    cfg = _dc_replace(base, class_views=views,
                      llm_profiles={"default": _structured_profile(30000)})
    engine = _SchemaRoutingEngine()
    ctx = budget_ctx(cfg, engine)

    _asyncio.run(annotate_record(text_record(), ctx, label="qa"))
    assert len(engine.calls) == 1                # 全局 Schema 计价 ⇒ 装填通过

    with _pytest.raises(ContextOverflowError) as excinfo:
        _asyncio.run(annotate_record(text_record(), ctx, label="writing"))
    assert excinfo.value.phase == "precheck"
    assert len(engine.calls) == 1                # 溢出发生在派发前

"""Offline unit tests for M5 annotate: prompt assembly (spec 3.5.2 / CONTRACTS §10.1,
§10.5) and the self-consistency field-level majority vote. Pure logic only — no LLM."""
from __future__ import annotations

import json

from labelkit.annotate import (
    AnnotateStage,
    RepairContext,
    _majority_vote,
    _voted_keys,
    build_annotate_prompt,
)
from labelkit.config.model import (
    AnnotateConfig,
    DedupConfig,
    FewShotExample,
    GenerateConfig,
    InputConfig,
    OutputConfig,
    QualityConfig,
    ResolvedConfig,
    Rubric,
    RunConfig,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)
from labelkit.types import ImageRef, Record, RecordRef, UINode, UITree

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
             user_schema=USER_SCHEMA, trace=None) -> ResolvedConfig:
    return ResolvedConfig(
        tool=ToolConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality=modality, input="in"),
        input=InputConfig(ui_tree_max_chars=ui_tree_max_chars),
        dedup=DedupConfig(),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(enabled=True, llm="default", instruction=instruction,
                                examples=tuple(examples),
                                self_consistency=self_consistency),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline=json.dumps(user_schema)),
        trace=trace or TraceConfig(),
        rubric=Rubric(name="default:text", criteria=()),
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
    from labelkit.errors import SchemaViolation
    from labelkit.types import PipelineItem

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
    from labelkit.errors import SchemaViolation
    from labelkit.types import PipelineItem

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

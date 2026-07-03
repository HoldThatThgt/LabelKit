"""Offline unit tests for M8 schema engine — pure logic only (no LLM anywhere).

Covers: L1 deterministic_repair exhaustively, L2 full-violation collection with JSON
Pointer paths, byte-exact L3 repair-prompt rendering vs the spec 3.8.4 worked example,
resolved_at bucket logic driven by synthetic layer outcomes, canonical user-schema
text, and the §10.7 internal schema constants.
"""
from types import SimpleNamespace

from jsonschema import Draft202012Validator

from labelkit.config.model import OutputConfig
from labelkit.schema_engine import (
    VERDICT_SCHEMA,
    SchemaEngine,
    _bucket_for,
    _build_repair_prompt,
    _extract_object,
    _first_balanced_braces,
    _strip_markdown_fences,
    deterministic_repair,
    judgment_schema,
    pointwise_schema,
    samples_schema,
)
from labelkit.types import Usage

# The spec 3.8.4 worked-example user schema.
SPEC_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
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


def make_engine(user_schema=None, cfg=None) -> SchemaEngine:
    # llm=None: these tests never trigger an LLM call (pure-logic paths only).
    return SchemaEngine(user_schema or SPEC_SCHEMA, llm=None, cfg=cfg or OutputConfig())


# ── L1: deterministic_repair, exhaustively ──────────────────────────────────

class TestDeterministicRepair:
    def test_clean_json_passes_through(self):
        assert deterministic_repair('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}

    def test_clean_json_with_whitespace(self):
        assert deterministic_repair('  \n {"a": 1}\n\n') == {"a": 1}

    def test_json_fence_with_language_tag(self):
        text = '```json\n{"intent": "qa"}\n```'
        assert deterministic_repair(text) == {"intent": "qa"}

    def test_fence_without_language_tag(self):
        assert deterministic_repair('```\n{"a": 1}\n```') == {"a": 1}

    def test_fence_with_prose_before_and_after(self):
        text = '好的，以下是结果：\n```json\n{"a": 1}\n```\n希望有帮助。'
        assert deterministic_repair(text) == {"a": 1}

    def test_unclosed_fence_truncated_output(self):
        # Truncation mid-generation: opening fence, no closing fence, cut-off JSON.
        text = '```json\n{"intent": "qa", "topic": "天气'
        assert deterministic_repair(text) == {"intent": "qa", "topic": "天气"}

    def test_prose_around_bare_json(self):
        text = 'Sure! Here is the object: {"a": 1, "b": 2} — let me know.'
        assert deterministic_repair(text) == {"a": 1, "b": 2}

    def test_single_quotes(self):
        assert deterministic_repair("{'intent': 'qa', 'n': 3}") == {"intent": "qa", "n": 3}

    def test_trailing_comma(self):
        assert deterministic_repair('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}

    def test_trailing_comma_in_array(self):
        assert deterministic_repair('{"a": [1, 2,],}') == {"a": [1, 2]}

    def test_truncated_object(self):
        assert deterministic_repair('{"a": 1, "b": {"c": 2') == {"a": 1, "b": {"c": 2}}

    def test_truncated_mid_string(self):
        assert deterministic_repair('{"a": "hello wor') == {"a": "hello wor"}

    def test_balanced_extraction_with_braces_inside_strings(self):
        text = 'prefix {"a": "he said {hi} and {bye}", "b": {"c": 1}} trailing } noise'
        assert deterministic_repair(text) == {"a": "he said {hi} and {bye}", "b": {"c": 1}}

    def test_balanced_extraction_with_escaped_quotes(self):
        text = '{"a": "quote \\" then {brace", "b": 1} extra'
        assert deterministic_repair(text) == {"a": 'quote " then {brace', "b": 1}

    def test_takes_first_balanced_object_not_later_ones(self):
        text = '{"first": 1} {"second": 2}'
        assert deterministic_repair(text) == {"first": 1}

    def test_all_fail_returns_none_for_garbage(self):
        assert deterministic_repair("I cannot answer that question.") is None

    def test_all_fail_returns_none_for_empty(self):
        assert deterministic_repair("") is None

    def test_non_object_json_returns_none(self):
        assert deterministic_repair("[1, 2, 3]") is None
        assert deterministic_repair('"just a string"') is None

    def test_fenced_string_value_with_embedded_backticks_survives_intact(self):
        # Regression: a non-greedy fence regex used to end the fenced block at the
        # ``` embedded in the string value, silently truncating the field content.
        text = ('```json\n{"intent": "qa", "difficulty": "easy", '
                '"topic": "use ``` fences for code"}\n```')
        assert deterministic_repair(text) == {
            "intent": "qa", "difficulty": "easy", "topic": "use ``` fences for code",
        }

    def test_embedded_backticks_in_middle_property_survive_intact(self):
        text = '```json\n{"a": "wrap in ``` marks", "b": 1}\n```'
        assert deterministic_repair(text) == {"a": "wrap in ``` marks", "b": 1}

    def test_inline_fence_in_prose_before_bare_json(self):
        # Regression: first-fenced-block-wins used to select the empty inline fence
        # content and discard the JSON that follows -> spurious L3 escalation.
        text = ('Note the ```code``` style.\n'
                '{"intent": "qa", "topic": "t", "difficulty": "easy"}')
        assert deterministic_repair(text) == {
            "intent": "qa", "topic": "t", "difficulty": "easy",
        }

    def test_json_in_second_fenced_block(self):
        # Regression: the non-JSON first fenced block used to win and L1 failed.
        text = ('Plan first:\n```text\nsome notes without JSON\n```\n'
                'Result:\n```json\n{"a": 1, "b": 2}\n```')
        assert deterministic_repair(text) == {"a": 1, "b": 2}

    def test_anchored_fence_with_prose_after_closing_fence(self):
        text = '```json\n{"a": 1}\n```\n希望有帮助。'
        assert deterministic_repair(text) == {"a": 1}

    def test_unclosed_anchored_fence_with_embedded_backticks_truncated(self):
        # Truncated output: opening fence, embedded ``` inside a string, no closing
        # fence and cut-off JSON — repair completes without cutting at the embedded ```.
        text = '```json\n{"a": "use ``` fences", "b": "天气'
        assert deterministic_repair(text) == {"a": "use ``` fences", "b": "天气"}

    def test_fence_strip_helper_keeps_non_fenced_text(self):
        assert _strip_markdown_fences('{"a": 1}') == '{"a": 1}'

    def test_fence_strip_helper_is_anchored(self):
        # Prose-leading text is NOT treated as fenced even if it contains fences.
        text = 'Note the ```code``` style.\n{"a": 1}'
        assert _strip_markdown_fences(text) == text

    def test_fence_strip_helper_takes_interior_up_to_trailing_fence(self):
        text = '```json\n{"a": "x ``` y"}\n```'
        assert _strip_markdown_fences(text) == '{"a": "x ``` y"}'

    def test_balanced_helper_returns_none_without_brace(self):
        assert _first_balanced_braces("no braces here") is None

    def test_balanced_helper_returns_suffix_when_unbalanced(self):
        assert _first_balanced_braces('x {"a": {"b": 1}') == '{"a": {"b": 1}'


# ── L2: full violation collection with JSON Pointer paths ───────────────────

class TestValidateOnly:
    def test_valid_object_yields_empty_list(self):
        engine = make_engine()
        obj = {"intent": "qa", "topic": "t", "difficulty": "easy"}
        assert engine.validate_only(obj) == []

    def test_collects_all_violations_not_just_first(self):
        engine = make_engine()
        obj = {"intent": "writing", "difficulty": 3, "extra": True}
        errors = engine.validate_only(obj)
        # intent enum, difficulty type AND enum, missing required, additionalProperties
        assert len(errors) == 5
        pointers = [e.split(":")[0] for e in errors]
        assert "/intent" in pointers
        assert "/difficulty" in pointers
        # Root-level violations (required / additionalProperties) anchor at "".
        assert pointers.count("") == 2
        assert any("'topic' is a required property" in e for e in errors)

    def test_nested_pointer_paths(self):
        schema = {"type": "object",
                  "properties": {"outer": {"type": "object",
                      "properties": {"inner": {"type": "integer"},
                                     "arr": {"type": "array",
                                             "items": {"type": "string"}}}}}}
        engine = make_engine(schema)
        errors = engine.validate_only({"outer": {"inner": "x", "arr": ["ok", 5]}})
        pointers = sorted(e.split(":")[0] for e in errors)
        assert pointers == ["/outer/arr/1", "/outer/inner"]

    def test_explicit_schema_argument_overrides_user_schema(self):
        engine = make_engine()
        assert engine.validate_only({"samples": ["a", "b"]}, samples_schema(2)) == []
        assert engine.validate_only({"samples": ["a"]}, samples_schema(2)) != []

    def test_enum_violation_rendered_in_spec_wording(self):
        engine = make_engine()
        errors = engine.validate_only(
            {"intent": "writing", "topic": "请假条写作", "difficulty": "easy"})
        assert errors == [
            '/intent: 期望为枚举 ["writing_assist", "qa", "translation", "chitchat", '
            '"other"] 之一，实际值为 "writing"'
        ]


# ── L3: repair prompt byte-exact vs spec 3.8.4 ──────────────────────────────

SPEC_RAW_OUTPUT = (
    '```json\n'
    '{\n'
    '  "intent": "writing",\n'
    '  "topic": "请假条写作",\n'
    '  "difficulty": "easy",\n'
    '}\n'
    '```'
)

SPEC_REPAIR_PROMPT = (
    '[原始输出]\n'
    '```json\n'
    '{\n'
    '  "intent": "writing",\n'
    '  "topic": "请假条写作",\n'
    '  "difficulty": "easy",\n'
    '}\n'
    '```\n'
    '\n'
    '[违规清单]\n'
    '1. /intent: 期望为枚举 ["writing_assist", "qa", "translation", "chitchat", '
    '"other"] 之一，实际值为 "writing"\n'
    '\n'
    '只输出修正后的 JSON。'
)


class TestRepairPrompt:
    def test_spec_worked_example_byte_exact(self):
        # L1 on the spec's raw output: fence stripped, trailing comma repaired,
        # enum violation survives untouched into L2.
        obj = deterministic_repair(SPEC_RAW_OUTPUT)
        assert obj == {"intent": "writing", "topic": "请假条写作", "difficulty": "easy"}
        violations = make_engine().validate_only(obj)
        prompt = _build_repair_prompt(SPEC_RAW_OUTPUT, violations)
        assert prompt == SPEC_REPAIR_PROMPT

    def test_numbered_list_is_one_based_one_per_line(self):
        prompt = _build_repair_prompt('{"x": 1}', ["/a: first", "/b: second", "/c: third"])
        assert "[违规清单]\n1. /a: first\n2. /b: second\n3. /c: third\n" in prompt
        assert prompt.endswith("只输出修正后的 JSON。")
        assert prompt.startswith('[原始输出]\n{"x": 1}\n')


# ── resolved_at bucket logic (synthetic layer outcomes, no LLM) ─────────────

class TestBucketing:
    def test_bucket_mapping(self):
        assert _bucket_for(False, 0) == "l0_or_clean"   # clean first response / L0
        assert _bucket_for(True, 0) == "l1"             # L1 had to fix something
        assert _bucket_for(False, 1) == "l3_1"          # passed after repair round 1
        assert _bucket_for(False, 2) == "l3_2"          # passed after repair round 2

    def test_stats_count_user_schema_calls_only(self):
        engine = make_engine()
        engine._resolve("l0_or_clean", is_user_schema=True, record_ids=(), batch_no=0,
                        violations=[])
        engine._resolve("l1", is_user_schema=True, record_ids=(), batch_no=0,
                        violations=[])
        engine._resolve("l3_1", is_user_schema=False, record_ids=(), batch_no=0,
                        violations=["/x: enum"])  # internal-schema call: not counted
        engine._resolve("rejected", is_user_schema=True, record_ids=(), batch_no=0,
                        violations=["/x: enum"])
        assert engine.stats == {"l0_or_clean": 1, "l1": 1, "l3_1": 0, "l3_2": 0,
                                "rejected": 1}

    def test_stats_starts_zeroed_with_all_five_buckets(self):
        assert make_engine().stats == {"l0_or_clean": 0, "l1": 0, "l3_1": 0,
                                       "l3_2": 0, "rejected": 0}

    def test_extract_object_synthetic_outcomes(self):
        # Native structured payload (L0 path) — no L1 fix.
        resp = SimpleNamespace(structured={"a": 1}, text="")
        assert _extract_object(resp) == ({"a": 1}, False, '{"a": 1}')
        # Clean text — trivially parsed, no L1 fix.
        resp = SimpleNamespace(structured=None, text='{"a": 1}')
        assert _extract_object(resp) == ({"a": 1}, False, '{"a": 1}')
        # Fenced text — L1 had to fix.
        resp = SimpleNamespace(structured=None, text='```json\n{"a": 1}\n```')
        obj, fixed, raw = _extract_object(resp)
        assert (obj, fixed) == ({"a": 1}, True)
        assert raw == '```json\n{"a": 1}\n```'   # raw text preserved for the repair prompt
        # Unparseable — all layers of L1 fail.
        resp = SimpleNamespace(structured=None, text="cannot comply")
        assert _extract_object(resp) == (None, False, "cannot comply")


# ── canonical user-schema text ───────────────────────────────────────────────

def test_user_schema_text_is_single_line_canonical():
    engine = make_engine({"type": "object", "properties": {"意图": {"type": "string"}}})
    text = engine.user_schema_text
    assert "\n" not in text
    assert text == '{"type": "object", "properties": {"意图": {"type": "string"}}}'


# ── internal schema constants (§10.7) ────────────────────────────────────────

class TestInternalSchemas:
    def test_judgment_schema_with_and_without_reason(self):
        s = judgment_schema(["accuracy", "clarity"], with_reason=True)
        Draft202012Validator.check_schema(s)
        item = s["properties"]["judgments"]["items"]
        assert item["required"] == ["criterion", "winner", "reason"]
        assert s["properties"]["judgments"]["minItems"] == 2
        assert s["properties"]["judgments"]["maxItems"] == 2
        s2 = judgment_schema(["accuracy"], with_reason=False)
        assert "reason" not in s2["properties"]["judgments"]["items"]["properties"]
        assert s2["properties"]["judgments"]["items"]["required"] == ["criterion", "winner"]

    def test_pointwise_schema(self):
        s = pointwise_schema("educational_value")
        Draft202012Validator.check_schema(s)
        v = Draft202012Validator(s)
        assert v.is_valid({"scores": [{"criterion": "educational_value",
                                       "reason": "两句理由。", "score": 4}]})
        assert not v.is_valid({"scores": [{"criterion": "other", "reason": "r", "score": 4}]})
        assert not v.is_valid({"scores": [{"criterion": "educational_value",
                                           "reason": "r", "score": 6}]})

    def test_verdict_schema(self):
        Draft202012Validator.check_schema(VERDICT_SCHEMA)
        v = Draft202012Validator(VERDICT_SCHEMA)
        assert list(VERDICT_SCHEMA["properties"]) == ["critiques", "verdict"]
        assert v.is_valid({"critiques": [{"aspect": "a", "opinion": "o"}], "verdict": "pass"})
        assert not v.is_valid({"critiques": [], "verdict": "maybe"})

    def test_samples_schema(self):
        s = samples_schema(4)
        Draft202012Validator.check_schema(s)
        v = Draft202012Validator(s)
        assert v.is_valid({"samples": ["a", "b", "c", "d"]})
        assert not v.is_valid({"samples": ["a", "b", "c"]})
        assert not v.is_valid({"samples": ["a", "b", "c", "d", "e"]})


def test_usage_summing_shape():
    # complete_validated sums first-call + repair usage via Usage.__add__.
    assert Usage(10, 5) + Usage(3, 2) == Usage(13, 7)


# ── P2-5: lossy-L1 heuristic (json_repair quote-truncation detection) ────────

def test_l1_lossy_flags_large_content_drop():
    from labelkit.schema_engine import l1_repair_is_lossy
    tail = "，这一段是被未转义引号截断后整体丢失的很长的批评意见文本" * 4
    raw = '{"aspect": "事实一致性", "opinion": "页面标题"' + tail + '"}'
    obj = {"aspect": "事实一致性", "opinion": "页面标题"}   # what json_repair keeps
    assert l1_repair_is_lossy(obj, raw) is True


def test_l1_lossy_not_flagged_for_small_fixes():
    from labelkit.schema_engine import l1_repair_is_lossy
    raw = '```json\n{"intent": "writing_assist", "topic": "请假条代写",}\n```'
    obj = {"intent": "writing_assist", "topic": "请假条代写"}
    assert l1_repair_is_lossy(obj, raw) is False


def test_l1_lossy_end_to_end_via_deterministic_repair():
    from labelkit.schema_engine import deterministic_repair, l1_repair_is_lossy
    tail = "x" * 120
    raw = '{"opinion": "标题"未转义' + tail + '"}'
    import json as _json
    obj = deterministic_repair(raw)
    assert isinstance(obj, dict)          # L1 salvages SOMETHING…
    if _json.dumps(obj, ensure_ascii=False).find(tail) < 0:
        # …and when this json_repair version drops the tail, the heuristic
        # must notice (a preserved tail is also acceptable).
        assert l1_repair_is_lossy(obj, raw) is True


def test_l1_lossy_not_flagged_for_fenced_pretty_json():
    # Review finding: fence + indent is the most common "clean" non-structured
    # output shape — zero content is lost, must not warn.
    import json as _json
    from labelkit.schema_engine import l1_repair_is_lossy
    obj = {"scores": [{"criterion": "educational_value",
                       "reason": "该指令是意图明确的写作示范任务，包含时间与事由等具体要素。"
                                 "但任务简单，不涉及推理或专业知识，可学习内容有限。",
                       "score": 3}]}
    raw = "```json\n" + _json.dumps(obj, ensure_ascii=False, indent=2) + "\n```"
    assert l1_repair_is_lossy(obj, raw) is False


def test_l1_lossy_not_flagged_for_ascii_escaped_json():
    import json as _json
    from labelkit.schema_engine import l1_repair_is_lossy
    obj = {"critiques": [{"aspect": "事实一致性",
                          "opinion": "标注结果与原始数据逐项一致，未见编造内容。"}],
           "verdict": "pass"}
    raw = "```json\n" + _json.dumps(obj, ensure_ascii=True) + "\n```"
    assert l1_repair_is_lossy(obj, raw) is False

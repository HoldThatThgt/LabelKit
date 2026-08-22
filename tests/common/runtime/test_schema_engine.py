"""Offline unit tests for M8 schema engine — pure logic only (no LLM anywhere).

Covers: L1 deterministic_repair exhaustively, L2 full-violation collection with JSON
Pointer paths, byte-exact L3 repair-prompt rendering vs the spec 3.8.4 worked example,
resolved_at bucket logic driven by synthetic layer outcomes, canonical user-schema
text, and the §10.7 internal schema constants.
"""
import asyncio
import json
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from labelkit.common.config.model import LLMProfile, OutputConfig
from labelkit.common.contracts.generation import (
    EventExecution,
    PostValidatedCallRequest,
    PostValidationResult,
)
from labelkit.common.errors import SchemaViolation
from labelkit.common.runtime.llm_client import (
    Message,
    Part,
    PromptBundle,
    _build_anthropic_body,
)
from labelkit.common.runtime.schema_engine import (
    VERDICT_SCHEMA,
    CallScope,
    SchemaEngine,
    _bucket_for,
    _CallContext,
    _build_repair_prompt,
    _build_post_repair_instruction,
    _extract_object,
    _first_balanced_braces,
    _render_error,
    _strip_markdown_fences,
    action_schema,
    classification_schema,
    defect_verdict_schema,
    deterministic_repair,
    event_plan_schema,
    judgment_schema,
    noise_semantic_evaluation_schema,
    pointwise_schema,
    scenario_seed_schema,
    semantic_evaluation_schema,
    samples_schema,
    segment_window_schema,
    stitch_schema,
)
from labelkit.common.contracts.types import Usage
from tests import hook_samples

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


def make_engine(user_schema=None, cfg=None, validator=None) -> SchemaEngine:
    # llm=None: these tests never trigger an LLM call (pure-logic paths only).
    # v1.17 Wave 2b: L2.5 回调以冻结 callable 传入（不再从 cfg.validator 字符串 resolve）。
    return SchemaEngine(user_schema or SPEC_SCHEMA, llm=None, cfg=cfg or OutputConfig(),
                        validator=validator)


def _ctx(*, user_treated: bool, schema=None, record=None) -> _CallContext:
    # The per-call context object _resolve/_inspect take (accounting + trace inputs).
    return _CallContext(active=schema or SPEC_SCHEMA, user_treated=user_treated,
                        record_ids=(), batch_no=0, record=record)


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

    def test_unrepairable_candidate_is_swallowed_and_returns_none(self):
        # 失控模型吐出的极深嵌套让 json_repair 自己抛（递归深度）：L1 只记 debug、
        # 换下一个来源继续，最终返回 None 而不是把异常甩给调用方。
        runaway = "{" + '"a": [' * 20000 + "]" * 20000 + "}"
        assert deterministic_repair(runaway) is None
        assert deterministic_repair(f"```json\n{runaway}\n```") is None

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
            '/intent: expected one of enum ["writing_assist", "qa", "translation", '
            '"chitchat", "other"], got "writing"'
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
    '1. /intent: expected one of enum ["writing_assist", "qa", "translation", '
    '"chitchat", "other"], got "writing"\n'
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
        user_ctx = _ctx(user_treated=True)
        internal_ctx = _ctx(user_treated=False)
        engine._resolve("l0_or_clean", user_ctx, violations=[])
        engine._resolve("l1", user_ctx, violations=[])
        # internal-schema call: not counted
        engine._resolve("l3_1", internal_ctx, violations=["/x: enum"])
        engine._resolve("rejected", user_ctx, violations=["/x: enum"])
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
        # Clean JSON that is not an OBJECT — L1 still gets a shot at it.
        resp = SimpleNamespace(structured=None, text='[{"a": 1}]')
        assert _extract_object(resp) == ({"a": 1}, True, '[{"a": 1}]')
        resp = SimpleNamespace(structured=None, text="42")
        assert _extract_object(resp) == (None, False, "42")


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

    def test_segment_window_schema_two_states(self):
        # v1.8 M14 (spec 3.14.3): minItems == maxItems == frame_count pins the
        # array length; index range pinned to the window; NO uniqueItems —
        # index de-duplication is judge_window's first-wins post-validation.
        s = segment_window_schema(3, with_reason=False)
        Draft202012Validator.check_schema(s)
        assert s["required"] == ["frames"] and s["additionalProperties"] is False
        arr = s["properties"]["frames"]
        assert arr["minItems"] == 3 and arr["maxItems"] == 3
        item = arr["items"]
        assert item["required"] == ["index", "relation"]
        assert item["additionalProperties"] is False
        assert item["properties"]["index"] == {"type": "integer",
                                               "minimum": 0, "maximum": 2}
        assert item["properties"]["relation"]["enum"] == [
            "continues", "advances", "returns_to_entry", "context_switch",
            "interruption"]
        assert "reason" not in item["properties"]
        assert "uniqueItems" not in _all_dict_keys(s)
        v = Draft202012Validator(s)
        rows = [{"index": 0, "relation": "continues"},
                {"index": 1, "relation": "advances"},
                {"index": 2, "relation": "interruption"}]
        assert v.is_valid({"frames": rows})
        assert not v.is_valid({"frames": rows[:2]})              # minItems pins N
        assert not v.is_valid({"frames": rows + rows[:1]})       # maxItems pins N
        assert not v.is_valid({"frames": rows[:2] + [{"index": 3,
                                                      "relation": "continues"}]})
        assert not v.is_valid({"frames": rows[:2] + [{"index": 2,
                                                      "relation": "boundary"}]})
        # duplicate indices are schema-legal (first-wins is code-side)
        assert v.is_valid({"frames": [rows[0]] * 3})
        s2 = segment_window_schema(3, with_reason=True)
        Draft202012Validator.check_schema(s2)
        item2 = s2["properties"]["frames"]["items"]
        assert item2["required"] == ["index", "relation", "reason"]
        assert item2["properties"]["reason"] == {"type": "string"}
        assert not Draft202012Validator(s2).is_valid({"frames": rows})  # reason required

    def test_action_schema_shape(self):
        # v1.8 M15 (S15/S7): frozen 11-value enum ORDER; all four keys required
        # with nullable unions (OpenAI strict rejects optional properties).
        s = action_schema()
        Draft202012Validator.check_schema(s)
        assert s["properties"]["action_type"]["enum"] == [
            "click", "long_press", "input_text", "scroll", "drag", "open_app",
            "app_switch", "navigate_back", "navigate_home", "wait", "other"]
        assert s["required"] == ["action_type", "target", "value", "description"]
        assert s["additionalProperties"] is False
        assert s["properties"]["target"]["type"] == ["string", "null"]
        assert s["properties"]["value"]["type"] == ["string", "null"]
        assert s["properties"]["description"] == {"type": "string"}
        assert "uniqueItems" not in _all_dict_keys(s)
        v = Draft202012Validator(s)
        assert v.is_valid({"action_type": "click", "target": "登录",
                           "value": None, "description": "点击登录按钮"})
        assert v.is_valid({"action_type": "wait", "target": None,
                           "value": None, "description": "等待加载"})
        assert not v.is_valid({"action_type": "tap", "target": None,
                               "value": None, "description": "d"})
        assert not v.is_valid({"action_type": "click", "value": None,
                               "description": "d"})     # target key required

    def test_defect_verdict_schema_shape(self):
        # v1.8 M7 stream variant (S7): all three top keys required; defect
        # members is a nullable STRING array; critiques byte-identical to
        # VERDICT_SCHEMA's (the feed-back/merge chain consumes them unchanged).
        # v1.9 (T15): six kinds — wrong_stitch appended.
        s = defect_verdict_schema()
        Draft202012Validator.check_schema(s)
        assert list(s["properties"]) == ["critiques", "defects", "verdict"]
        assert s["required"] == ["critiques", "defects", "verdict"]
        assert s["additionalProperties"] is False
        assert s["properties"]["critiques"] == VERDICT_SCHEMA["properties"]["critiques"]
        defect = s["properties"]["defects"]["items"]
        assert defect["required"] == ["kind", "members", "position", "detail"]
        assert defect["properties"]["kind"]["enum"] == [
            "label_mismatch", "off_task_members", "missing_head",
            "missing_tail", "missing_members", "wrong_stitch"]
        assert defect["properties"]["members"] == {"type": ["array", "null"],
                                                   "items": {"type": "string"}}
        assert defect["properties"]["position"]["type"] == ["string", "null"]
        assert s["properties"]["verdict"]["enum"] == ["pass", "fail"]
        assert "uniqueItems" not in _all_dict_keys(s)
        v = Draft202012Validator(s)
        assert v.is_valid({"critiques": [{"aspect": "边界", "opinion": "缺尾帧"}],
                           "defects": [{"kind": "missing_tail", "members": None,
                                        "position": "tail", "detail": "缺下单确认页"}],
                           "verdict": "fail"})
        assert v.is_valid({"critiques": [], "defects": [], "verdict": "pass"})
        assert not v.is_valid({"critiques": [], "verdict": "pass"})  # defects required
        assert not v.is_valid({"critiques": [],
                               "defects": [{"kind": "off_task_members",
                                            "members": [123], "position": None,
                                            "detail": "d"}],
                               "verdict": "fail"})       # members items are strings

    def test_stitch_schema_shape(self):
        # v1.9 M16 (spec 3.16 / §10.7): all five keys required; thread_ref is a
        # nullable integer (pool ordinal range-checked code-side); confidence is
        # the closed three-level enum (trace observation only, T9 — never a gate).
        s = stitch_schema()
        Draft202012Validator.check_schema(s)
        assert list(s["properties"]) == ["verdict", "thread_ref", "task_name",
                                         "reason", "confidence"]
        assert s["required"] == ["verdict", "thread_ref", "task_name",
                                 "reason", "confidence"]
        assert s["additionalProperties"] is False
        assert s["properties"]["verdict"]["enum"] == ["resume", "new"]
        assert s["properties"]["thread_ref"]["type"] == ["integer", "null"]
        assert s["properties"]["task_name"] == {"type": "string"}
        assert s["properties"]["reason"] == {"type": "string"}
        assert s["properties"]["confidence"]["enum"] == ["high", "medium", "low"]
        assert "uniqueItems" not in _all_dict_keys(s)
        v = Draft202012Validator(s)
        assert v.is_valid({"verdict": "resume", "thread_ref": 1,
                           "task_name": "点外卖", "reason": "订单实体延续",
                           "confidence": "high"})
        assert v.is_valid({"verdict": "new", "thread_ref": None,
                           "task_name": "打车", "reason": "新任务",
                           "confidence": "medium"})
        assert not v.is_valid({"verdict": "merge", "thread_ref": None,
                               "task_name": "t", "reason": "r",
                               "confidence": "low"})     # closed verdict enum
        assert not v.is_valid({"verdict": "new", "task_name": "t",
                               "reason": "r", "confidence": "low"})
                                                         # thread_ref key required

    def test_scenario_seed_schema_declared_and_instruction_only(self):
        state = {"type": "object", "properties": {"counter": {"type": "integer"}},
                 "required": ["counter"], "additionalProperties": False}
        declared = scenario_seed_schema(("requester", "system"), state)
        dynamic = scenario_seed_schema(None, state)
        Draft202012Validator.check_schema(declared)
        Draft202012Validator.check_schema(dynamic)
        actors = declared["properties"]["actors"]
        assert tuple(actors["properties"]) == ("requester", "system")
        assert actors["required"] == ["requester", "system"]
        assert actors["additionalProperties"] is False
        assert dynamic["properties"]["actors"]["minProperties"] == 1
        assert dynamic["properties"]["actors"]["maxProperties"] == 8
        assert dynamic["properties"]["actors"]["propertyNames"] == {
            "type": "string", "minLength": 1,
        }
        dynamic_validator = Draft202012Validator(dynamic)
        shell = {
            "initial_state": {"counter": 0},
            "actors": {"": {"goal": {}, "identity": {}, "style": {}}},
            "shared_facts": {"public": {}, "hidden": {}},
            "style": {}, "time_context": {},
        }
        assert not dynamic_validator.is_valid(shell)
        state_resource = declared["properties"]["initial_state"]
        resource_id = state_resource["$id"]
        assert resource_id.startswith("urn:labelkit:state-schema:")
        assert {key: value for key, value in state_resource.items() if key != "$id"} == state

    def test_scenario_seed_schema_preserves_embedded_resource_semantics(self):
        state = {
            "$defs": {"number": {"type": "integer"}},
            "allOf": [{
                "type": "object",
                "properties": {
                    "kind": {"const": "count"},
                    "value": {"$ref": "#/$defs/number"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["kind", "value"],
            }],
            "if": {"properties": {"kind": {"const": "count"}}},
            "then": {"dependentSchemas": {
                "value": {"required": ["enabled"]},
            }},
            "unevaluatedProperties": False,
        }
        schema = scenario_seed_schema(("requester",), state)
        validator = Draft202012Validator(schema)
        seed = {
            "initial_state": {"kind": "count", "value": 2, "enabled": True},
            "actors": {"requester": {"goal": {}, "identity": {}, "style": {}}},
            "shared_facts": {"public": {}, "hidden": {}},
            "style": {}, "time_context": {},
        }
        assert validator.is_valid(seed)
        assert not validator.is_valid({
            **seed, "initial_state": {"kind": "count", "value": "2", "enabled": True}})
        assert not validator.is_valid({
            **seed, "initial_state": {"kind": "count", "value": 2}})
        assert not validator.is_valid({
            **seed,
            "initial_state": {"kind": "count", "value": 2, "enabled": True, "extra": 1},
        })

    def test_scenario_seed_schema_reaches_anthropic_body_unchanged(self):
        state = {
            "$defs": {"value": {"type": "string"}},
            "type": "object",
            "properties": {"value": {"$ref": "#/$defs/value"}},
            "required": ["value"],
        }
        schema = scenario_seed_schema(("requester",), state)
        profile = LLMProfile(
            name="semantic", provider="anthropic", base_url="https://example.invalid",
            model="m", api_key_env="K", supports_structured_output=True,
        )
        prompt = PromptBundle(messages=(Message(
            role="user", parts=(Part(kind="text", text="生成场景"),)),))
        body = _build_anthropic_body(profile, prompt, schema)
        assert body["tools"][0]["input_schema"] == schema
        assert json.loads(json.dumps(body))["tools"][0]["input_schema"] == schema

    def test_scenario_seed_schema_reuses_absolute_state_resource_id(self):
        state = {
            "$id": "https://schemas.example.test/state",
            "$defs": {"value": {"type": "integer"}},
            "type": "object",
            "properties": {"value": {"$ref": "#/$defs/value"}},
            "required": ["value"],
        }
        schema = scenario_seed_schema(("requester",), state)
        resource = schema["properties"]["initial_state"]
        assert resource == state
        seed = {
            "initial_state": {"value": 1},
            "actors": {"requester": {"goal": {}, "identity": {}, "style": {}}},
            "shared_facts": {"public": {}, "hidden": {}},
            "style": {}, "time_context": {},
        }
        assert Draft202012Validator(schema).is_valid(seed)

    def test_event_plan_schema_pins_patch_and_closed_sets(self):
        schema = event_plan_schema(("request", "confirm"), ("user", "system"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        valid = {
            "frame_class": "request", "actor": "user", "intent": "ask",
            "patch": [
                {"op": "test", "path": "/status", "value": "new"},
                {"op": "replace", "path": "/status", "value": "pending"},
            ],
        }
        assert validator.is_valid(valid)
        assert not validator.is_valid({**valid, "actor": "ghost"})
        assert not validator.is_valid({**valid, "patch": valid["patch"][:1]})
        assert not validator.is_valid({
            **valid, "patch": [{"op": "remove", "path": "/x", "value": 1},
                                valid["patch"][1]]})

    def test_semantic_evaluation_schemas_are_closed(self):
        semantic = semantic_evaluation_schema()
        noise = noise_semantic_evaluation_schema()
        Draft202012Validator.check_schema(semantic)
        Draft202012Validator.check_schema(noise)
        assert semantic["required"] == [
            "causal_consistency", "actor_knowledge", "goal_consistency",
            "temporal_plausibility", "cross_frame_consistency", "realism",
            "reason_codes",
        ]
        assert noise["required"] == [
            "unrelated_to_declared_tasks", "no_executable_task", "realism",
            "matches_planned_topic",
            "reason_codes",
        ]
        assert semantic["additionalProperties"] is False
        assert noise["additionalProperties"] is False


# ── v1.7: classification_schema (§10.7 / spec 3.13, R1) ─────────────────────

NAMES = ["faq", "chitchat", "other"]

# The frozen keyword vocabulary of the internal schemas — classification_schema must
# not grow it (R1: strict-mode gateways hard-reject e.g. uniqueItems, L0 passes
# schemas through unconditionally).
ALLOWED_KEYWORDS = {"type", "properties", "required", "additionalProperties",
                    "enum", "items", "minItems", "maxItems"}


def _schema_keywords(schema: dict) -> set[str]:
    """All JSON-Schema keywords used anywhere in the tree (property NAMES excluded)."""
    kws = set(schema)
    for key, value in schema.items():
        if key == "properties":
            for sub in value.values():
                kws |= _schema_keywords(sub)
        elif key in ("items", "contains"):
            kws |= _schema_keywords(value)
        elif key == "allOf":                     # v1.14 cover_all 的逐类覆盖支
            for sub in value:
                kws |= _schema_keywords(sub)
    return kws


def _all_dict_keys(node) -> set[str]:
    """Every dict key in the full tree, property names included — the uniqueItems sweep."""
    keys: set[str] = set()
    if isinstance(node, dict):
        keys |= set(node)
        for value in node.values():
            keys |= _all_dict_keys(value)
    elif isinstance(node, list):
        for value in node:
            keys |= _all_dict_keys(value)
    return keys


class TestClassificationSchema:
    def test_single_shape(self):
        s = classification_schema(NAMES, "single", max_labels=3, with_reason=False)
        Draft202012Validator.check_schema(s)
        assert s["required"] == ["class"]
        assert s["additionalProperties"] is False
        assert list(s["properties"]) == ["class"]
        assert s["properties"]["class"] == {"type": "string", "enum": NAMES}
        v = Draft202012Validator(s)
        assert v.is_valid({"class": "faq"})
        assert not v.is_valid({"class": "unknown"})         # enum-locked to the class table
        assert not v.is_valid({"class": "faq", "reason": "r"})

    def test_single_with_reason(self):
        s = classification_schema(NAMES, "single", max_labels=3, with_reason=True)
        Draft202012Validator.check_schema(s)
        assert s["required"] == ["class", "reason"]
        assert s["additionalProperties"] is False
        assert s["properties"]["reason"] == {"type": "string"}
        v = Draft202012Validator(s)
        assert v.is_valid({"class": "faq", "reason": "一句话理由"})
        assert not v.is_valid({"class": "faq"})             # reason required when requested

    def test_multi_shape(self):
        s = classification_schema(NAMES, "multi", max_labels=2, with_reason=False)
        Draft202012Validator.check_schema(s)
        assert s["required"] == ["classes"]
        assert s["additionalProperties"] is False
        arr = s["properties"]["classes"]
        assert arr["items"] == {"type": "string", "enum": NAMES}
        assert arr["minItems"] == 1
        assert arr["maxItems"] == 2
        v = Draft202012Validator(s)
        assert v.is_valid({"classes": ["faq"]})
        assert v.is_valid({"classes": ["faq", "chitchat"]})
        assert not v.is_valid({"classes": []})                        # minItems 1
        assert not v.is_valid({"classes": ["faq", "chitchat", "other"]})  # maxItems
        assert not v.is_valid({"classes": ["unknown"]})

    def test_multi_with_reason(self):
        s = classification_schema(NAMES, "multi", max_labels=3, with_reason=True)
        assert s["required"] == ["classes", "reason"]
        assert s["additionalProperties"] is False
        assert s["properties"]["classes"]["minItems"] == 1
        assert s["properties"]["classes"]["maxItems"] == 3
        v = Draft202012Validator(s)
        assert v.is_valid({"classes": ["faq", "other"], "reason": "理由"})
        assert not v.is_valid({"classes": ["faq"]})

    def test_multi_accepts_duplicate_labels_at_schema_level(self):
        # R1: duplicates pass the SCHEMA (no uniqueItems); de-duplication is the
        # classify stage's post-validation normalization, not M8's job.
        s = classification_schema(NAMES, "multi", max_labels=3, with_reason=False)
        assert Draft202012Validator(s).is_valid({"classes": ["faq", "faq"]})

    def test_no_unique_items_anywhere_and_keyword_set_frozen(self):
        # R1 regression anchor over every mode combination.
        for assignment in ("single", "multi"):
            for with_reason in (False, True):
                s = classification_schema(NAMES, assignment, max_labels=2,
                                          with_reason=with_reason)
                assert "uniqueItems" not in _all_dict_keys(s)
                assert _schema_keywords(s) <= ALLOWED_KEYWORDS

    def test_enum_copies_input_list(self):
        names = ["a", "b"]
        s = classification_schema(names, "single", max_labels=2, with_reason=False)
        names.append("c")                    # caller mutation must not leak into the schema
        assert s["properties"]["class"]["enum"] == ["a", "b"]


def test_usage_summing_shape():
    # complete_validated sums first-call + repair usage via Usage.__add__.
    assert Usage(10, 5) + Usage(3, 2) == Usage(13, 7)


# ── P2-5: lossy-L1 heuristic (json_repair quote-truncation detection) ────────

def test_l1_lossy_flags_large_content_drop():
    from labelkit.common.runtime.schema_engine import l1_repair_is_lossy
    tail = "，这一段是被未转义引号截断后整体丢失的很长的批评意见文本" * 4
    raw = '{"aspect": "事实一致性", "opinion": "页面标题"' + tail + '"}'
    obj = {"aspect": "事实一致性", "opinion": "页面标题"}   # what json_repair keeps
    assert l1_repair_is_lossy(obj, raw) is True


def test_l1_lossy_not_flagged_for_small_fixes():
    from labelkit.common.runtime.schema_engine import l1_repair_is_lossy
    raw = '```json\n{"intent": "writing_assist", "topic": "请假条代写",}\n```'
    obj = {"intent": "writing_assist", "topic": "请假条代写"}
    assert l1_repair_is_lossy(obj, raw) is False


def test_l1_lossy_end_to_end_via_deterministic_repair():
    from labelkit.common.runtime.schema_engine import deterministic_repair, l1_repair_is_lossy
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
    from labelkit.common.runtime.schema_engine import l1_repair_is_lossy
    obj = {"scores": [{"criterion": "educational_value",
                       "reason": "该指令是意图明确的写作示范任务，包含时间与事由等具体要素。"
                                 "但任务简单，不涉及推理或专业知识，可学习内容有限。",
                       "score": 3}]}
    raw = "```json\n" + _json.dumps(obj, ensure_ascii=False, indent=2) + "\n```"
    assert l1_repair_is_lossy(obj, raw) is False


def test_l1_lossy_falls_back_to_the_length_heuristic_when_the_region_differs():
    # 花括号区段本身能干净解析、但解析结果与修复对象不是同一个（L1 真出手改了内容）
    # ⇒ 不能直接判无损，转由长度启发式裁定。
    from labelkit.common.runtime.schema_engine import l1_repair_is_lossy
    raw = '{"opinion": "' + "长" * 200 + '"}'
    assert l1_repair_is_lossy({"opinion": "短"}, raw) is True
    assert l1_repair_is_lossy({"opinion": "长" * 199}, raw) is False


def test_l1_lossy_is_false_without_a_brace_region():
    # 长度启发式的基线是"花括号区段"；原始文本里根本没有花括号时无从比较，
    # 按定义判无损（结构化输出直接给对象、原始文本为空即此形）。
    from labelkit.common.runtime.schema_engine import l1_repair_is_lossy
    assert l1_repair_is_lossy({"intent": "qa", "topic": "请假条"}, "") is False
    assert l1_repair_is_lossy({"intent": "qa"}, "抱歉，我无法完成该任务。") is False


def test_l1_lossy_not_flagged_for_ascii_escaped_json():
    import json as _json
    from labelkit.common.runtime.schema_engine import l1_repair_is_lossy
    obj = {"critiques": [{"aspect": "事实一致性",
                          "opinion": "标注结果与原始数据逐项一致，未见编造内容。"}],
           "verdict": "pass"}
    raw = "```json\n" + _json.dumps(obj, ensure_ascii=True) + "\n```"
    assert l1_repair_is_lossy(obj, raw) is False


# ── v1.5 plan A / v1.17 Wave 2b 载体化: L2.5 hook plumbing（纯路径；全环 → 集成）──
# v1.17 Wave 2b：L2.5 回调以冻结 callable 传入（装配方从
# ``ResolvedConfig.validation_hooks.output.target`` 取），引擎不再按字符串二次
# resolve——测试直接传 ``tests.hook_samples`` 的可调用对象。

class TestL25Hook:
    def _engine(self, hook=hook_samples.topic_max6):
        return make_engine(validator=hook)

    def test_hook_carried_in_and_renders_prefix(self):
        eng = self._engine()
        out = eng._callback_violations({"topic": "这是一个很长很长的主题"}, None)
        assert len(out) == 1 and out[0].startswith("(validator) ")

    def test_hook_pass_returns_empty(self):
        eng = self._engine()
        assert eng._callback_violations({"topic": "请假条"}, None) == []

    def test_hook_receives_record_context(self):
        eng = self._engine(hook_samples.needs_record)
        obj = {"topic": "帮我写一条请假条"}
        assert eng._callback_violations(obj, None) == ["(validator) record 缺失"]
        assert eng._callback_violations(
            obj, {"instruction": "帮我写一条请假条"}
        ) == ["(validator) topic 不得整句复述原文"]
        assert eng._callback_violations(obj, {"instruction": "其他原文"}) == []

    def test_hook_gets_defensive_copy(self):
        seen = {}

        def spy(obj, record):
            seen["obj"] = obj
            obj["mutated"] = True
            return []

        eng = make_engine(validator=hook_samples.ok)
        eng._validator = spy                     # direct injection for the copy check
        original = {"topic": "请假条"}
        eng._callback_violations(original, None)
        assert "mutated" not in original         # hook saw a copy, not the object

    def test_hook_exception_propagates(self):
        eng = self._engine(hook_samples.boom)
        import pytest as _pytest
        with _pytest.raises(RuntimeError, match="hook exploded"):
            eng._callback_violations({"topic": "x"}, None)

    def test_hook_bad_return_raises_type_error(self):
        eng = self._engine(hook_samples.bad_return)
        import pytest as _pytest
        with _pytest.raises(TypeError, match="must return list"):
            eng._callback_violations({"topic": "x"}, None)

    def test_no_hook_configured_attribute_is_none(self):
        assert make_engine()._validator is None


# ── v1.11 (V25①): L3 repair-call overflow short-circuits to exhaustion ──────
# The engine's LLM boundary is stubbed with in-process objects (the segment/
# classify QueueEngine 惯例 — never a mock server/transport): the FIRST call
# returns an invalid-but-parseable response, every repair call overflows.

class _StubResponse:
    def __init__(self, text: str):
        self.text = text
        self.structured = None
        self.usage = Usage(5, 2)
        self.model = "glm-5.2"
        self.latency_ms = 1


class _FirstBadThenOverflowLLM:
    def __init__(self):
        self.calls = 0

    async def complete(self, profile, prompt, response_schema=None):
        from labelkit.common.errors import ContextOverflowError
        self.calls += 1
        if self.calls == 1:
            return _StubResponse('{"intent": "nope"}')     # fails L2 (enum+required)
        raise ContextOverflowError("repair prompt over budget",
                                   phase="precheck", profile=profile)


class _AlwaysOverflowLLM:
    async def complete(self, profile, prompt, response_schema=None):
        from labelkit.common.errors import ContextOverflowError
        raise ContextOverflowError("initial call over budget",
                                   phase="precheck", profile=profile)


def test_l3_repair_overflow_short_circuits_to_exhaustion():
    """V25①: the repair prompt is constant — one overflowing repair round
    proves every remaining round fails identically, so the engine skips them
    and lands on the EXISTING exhaustion path; reject attribution stays
    schema_violation (never context_overflow, no overflow_records count)."""
    import asyncio

    import pytest
    from labelkit.common.errors import SchemaViolation

    llm = _FirstBadThenOverflowLLM()
    eng = SchemaEngine(SPEC_SCHEMA, llm=llm,
                       cfg=OutputConfig(max_repair_attempts=3))
    prompt = object()                    # never inspected by the stub
    with pytest.raises(SchemaViolation) as ei:
        asyncio.run(eng.complete_validated("default", prompt))
    assert ei.value.callback_only is False          # schema_violation attribution
    assert any("/intent" in v for v in ei.value.errors)   # original violations kept
    assert llm.calls == 2               # first call + ONE repair try — rounds
    assert eng.stats["rejected"] == 1   # 2..3 short-circuited


def test_generic_repair_context_byte_limit_accepts_exact_and_skips_overflow():
    """generic L3 对完整新增 user 正文执行 R/R-1 边界且超限零修复派发。"""
    schema = {
        "type": "object",
        "properties": {"ok": {"const": True}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    raw = '{"bad":"value"}'
    probe = SchemaEngine(schema, llm=None, cfg=OutputConfig())
    rendered = probe._validate_full({"bad": "value"}, schema)[0]
    size = len(_build_repair_prompt(raw, rendered).encode("utf-8"))

    llm = _QueueLLM(raw, '{"ok":true}')
    engine = SchemaEngine(schema, llm=llm, cfg=OutputConfig(max_repair_attempts=1))
    result = asyncio.run(engine.complete_validated(
        "default", object(), schema=schema,
        scope=CallScope(repair_context_bytes=size),
    ))
    assert result[0] == {"ok": True} and llm.calls == 2

    llm = _QueueLLM(raw, '{"ok":true}')
    engine = SchemaEngine(schema, llm=llm, cfg=OutputConfig(max_repair_attempts=1))
    with pytest.raises(SchemaViolation):
        asyncio.run(engine.complete_validated(
            "default", object(), schema=schema,
            scope=CallScope(repair_context_bytes=size - 1),
        ))
    assert llm.calls == 1


def test_initial_call_overflow_propagates_untouched():
    """v1.11: ContextOverflowError from the INITIAL complete() is NOT the
    engine's to classify — it propagates to the operator (V27①) with zero
    bucket accounting."""
    import asyncio

    import pytest
    from labelkit.common.errors import ContextOverflowError

    eng = SchemaEngine(SPEC_SCHEMA, llm=_AlwaysOverflowLLM(), cfg=OutputConfig())
    with pytest.raises(ContextOverflowError) as ei:
        asyncio.run(eng.complete_validated("default", object()))
    assert ei.value.phase == "precheck"
    assert eng.stats == {"l0_or_clean": 0, "l1": 0, "l3_1": 0, "l3_2": 0,
                         "rejected": 0}


# ── A7 blind-spot fix: the L3 swallow point owns the reactive-400 feed ───────
# The engine's short-circuit is the overflow exception's TERMINAL — the
# SchemaViolation raised at exhaustion never reaches an operator overflow
# reject site, so the exactly-once breaker feed settles at the swallow.

class _MetricsFeedSpy:
    def __init__(self):
        self.fed = []
        self.events = []
        self.payloads = []

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None):
        self.events.append(ev)
        self.payloads.append(payload or {})

    def record_provider_result(self, fatal, *, hard=False):
        self.fed.append(fatal)


class _FirstBadThenReactiveLLM:
    def __init__(self, origin: str):
        self.calls = 0
        self.origin = origin

    async def complete(self, profile, prompt, response_schema=None):
        from labelkit.common.errors import ContextOverflowError
        self.calls += 1
        if self.calls == 1:
            return _StubResponse('{"intent": "nope"}')     # fails L2
        raise ContextOverflowError("sniffed 400 overflow", phase="reactive",
                                   profile=profile, origin=self.origin)


def _run_repair_overflow(origin: str) -> tuple["_MetricsFeedSpy", int]:
    import asyncio

    import pytest
    from labelkit.common.errors import SchemaViolation

    llm = _FirstBadThenReactiveLLM(origin)
    metrics = _MetricsFeedSpy()
    eng = SchemaEngine(SPEC_SCHEMA, llm=llm,
                       cfg=OutputConfig(max_repair_attempts=3), metrics=metrics)
    with pytest.raises(SchemaViolation):
        asyncio.run(eng.complete_validated("default", object()))
    return metrics, llm.calls


def test_l3_repair_reactive_400_overflow_feeds_breaker_exactly_once():
    metrics, calls = _run_repair_overflow("http_400")
    assert calls == 2                    # first call + the ONE overflowing repair
    assert metrics.fed == [True]         # A7: fed exactly once at the swallow


def test_l3_repair_finish_origin_overflow_never_feeds():
    # The 200-shaped oracle rode a successful HTTP interaction (§7.8 matrix) —
    # the short-circuit semantics stay, the breaker stays untouched.
    metrics, calls = _run_repair_overflow("finish")
    assert calls == 2
    assert metrics.fed == []


# ── L1 截断嫌疑与不可解析首轮：两条走完 complete_validated 的定案路径 ─────────


class _QueueLLM:
    """In-process 桩（QueueEngine 惯例）：按队列依次返回文本响应。"""

    def __init__(self, *texts: str):
        self.texts = list(texts)
        self.calls = 0
        self.prompts = []

    async def complete(self, profile, prompt, response_schema=None):
        self.calls += 1
        self.prompts.append(prompt)
        return _StubResponse(self.texts[min(self.calls - 1, len(self.texts) - 1)])


def test_clean_settlement_flags_a_suspected_lossy_l1_repair(caplog):
    # 首轮就过校验，但对象是 L1 从一段"花括号里夹自由文字"的响应里救出来的，
    # 保留体量远小于原区段 ⇒ 运行态只报长度不报内容，trace 事件加 l1_lossy 标。
    import asyncio
    import logging

    junk = "这是一段模型自说自话的解释文字并没有任何键值结构" * 5
    raw = ('{"intent": "qa", "topic": "请假条", "difficulty": "easy", '
           + junk + "}")
    metrics = _MetricsFeedSpy()
    eng = SchemaEngine(SPEC_SCHEMA, llm=_QueueLLM(raw), cfg=OutputConfig(),
                       metrics=metrics)
    with caplog.at_level(logging.WARNING, logger="labelkit"):
        obj, _usage, attempts, _model = asyncio.run(
            eng.complete_validated("default", object()))
    assert obj == {"intent": "qa", "topic": "请假条", "difficulty": "easy"}
    assert attempts == 1 and eng.stats["l1"] == 1        # 首轮经 L1 定案
    assert metrics.events == ["schema.repair"]
    assert metrics.payloads[0]["l1_lossy"] is True
    assert metrics.payloads[0]["resolved_at"] == "l1"
    warns = [r for r in caplog.records if "L1 repair may have dropped content" in r.message]
    assert len(warns) == 1
    assert junk not in warns[0].getMessage()             # 只讲长度，绝不讲内容


def test_unparseable_first_round_is_repaired_and_bucketed_at_l3_1():
    # 首轮连 JSON 对象都产不出（纯散文）⇒ 违规清单只有那条"不可解析"，进入 L3；
    # 第一轮修复通过即定案 l3_1，attempts 计首调 + 修复调用。
    import asyncio

    llm = _QueueLLM("抱歉，我无法给出结构化结果。",
                    '{"intent": "qa", "topic": "请假条", "difficulty": "easy"}')
    metrics = _MetricsFeedSpy()
    eng = SchemaEngine(SPEC_SCHEMA, llm=llm, cfg=OutputConfig(max_repair_attempts=2),
                       metrics=metrics)
    obj, usage, attempts, model = asyncio.run(eng.complete_validated("default", object()))
    assert obj == {"intent": "qa", "topic": "请假条", "difficulty": "easy"}
    assert llm.calls == 2 and attempts == 2
    assert usage == Usage(10, 4)                         # 首调 + 修复调用逐字段相加
    assert model == "glm-5.2"                            # 首轮模型名
    assert eng.stats["l3_1"] == 1 and eng.stats["rejected"] == 0
    assert metrics.events == ["schema.repair"]
    assert metrics.payloads[0]["resolved_at"] == "l3_1"
    assert metrics.payloads[0]["violations"] == [": unparseable"]
    assert "l1_lossy" not in metrics.payloads[0]


# ── v1.13（裁决·M8 显式待遇参数）: user_treatment 显式门 ─────────────────────
# 「用户 Schema 待遇」= 计 resolved_at 记账 + 启 L2.5 回调。v1.13 前该待遇由
# `schema is None` 隐式推断，导致「按序列类标注 Schema」这类显式 Schema 的记录级
# 标注调用被误当内部调用；新增的 additive keyword 把待遇与 Schema 来源解耦。

class _FixedLLM:
    """In-process 桩（QueueEngine 惯例——绝不 mock 服务端/传输层）：每次调用返回
    同一段文本，并记录 L0 透传的 response_schema。"""

    def __init__(self, text: str):
        self.text = text
        self.calls = 0
        self.schemas: list = []

    async def complete(self, profile, prompt, response_schema=None):
        self.calls += 1
        self.schemas.append(response_schema)
        return _StubResponse(self.text)


CLASS_SCHEMA = {"type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"], "additionalProperties": False}

# v1.17 Wave 2b：L2.5 回调以冻结 callable（hook_samples.topic_max6）传入。
ZERO_STATS = {"l0_or_clean": 0, "l1": 0, "l3_1": 0, "l3_2": 0, "rejected": 0}


def _run_engine(engine, schema=None, **scope_kw):
    import asyncio

    return asyncio.run(engine.complete_validated("default", object(), schema,
                                                 scope=CallScope(**scope_kw)))


def test_user_treatment_default_keeps_the_schema_is_none_inference():
    # 显式 schema + 缺省 user_treatment ⇒ 内部待遇（15 个既有调用点零改动的语义）
    llm = _FixedLLM('{"topic": "这是一个很长很长的主题"}')
    eng = SchemaEngine(SPEC_SCHEMA, llm=llm, cfg=OutputConfig(), validator=hook_samples.topic_max6)
    obj, _usage, attempts, _model = _run_engine(eng, schema=CLASS_SCHEMA)
    assert obj == {"topic": "这是一个很长很长的主题"}   # hook 未跑，否则会被判违规
    assert attempts == 1 and llm.calls == 1
    assert eng.stats == ZERO_STATS                     # 内部调用不进 resolved_at


def test_user_treatment_true_counts_the_bucket_on_an_explicit_schema():
    llm = _FixedLLM('{"topic": "请假条"}')
    eng = SchemaEngine(SPEC_SCHEMA, llm=llm, cfg=OutputConfig(), validator=hook_samples.topic_max6)
    obj, _usage, attempts, _model = _run_engine(eng, schema=CLASS_SCHEMA,
                                                user_treatment=True)
    assert obj == {"topic": "请假条"} and attempts == 1
    assert eng.stats["l0_or_clean"] == 1               # 记录级标注调用，照常记账
    assert llm.schemas == [CLASS_SCHEMA]               # L0 透传的是显式 Schema


def test_complete_validated_recursively_thaws_structured_schema():
    """普通校验调用也必须把冻结 Schema 还原成 JSON 容器。"""
    frozen = MappingProxyType({
        "type": "object",
        "properties": MappingProxyType({
            "topic": MappingProxyType({"enum": ("qa", "chat")}),
        }),
        "required": ("topic",),
        "additionalProperties": False,
    })
    llm = _FixedLLM('{"topic":"qa"}')
    engine = SchemaEngine(SPEC_SCHEMA, llm=llm, cfg=OutputConfig())

    result = _run_engine(engine, schema=frozen)

    assert result[0] == {"topic": "qa"}
    assert type(llm.schemas[0]) is dict
    assert type(llm.schemas[0]["properties"]) is dict
    assert type(llm.schemas[0]["properties"]["topic"]["enum"]) is list
    assert json.loads(json.dumps(llm.schemas[0]))["required"] == ["topic"]


def test_user_treatment_true_also_runs_the_l25_hook():
    import pytest
    from labelkit.common.errors import SchemaViolation

    llm = _FixedLLM('{"topic": "这是一个很长很长的主题"}')     # 过 L2、被 hook 拒
    eng = SchemaEngine(SPEC_SCHEMA, llm=llm,
                       cfg=OutputConfig(max_repair_attempts=1),
                       validator=hook_samples.topic_max6)
    with pytest.raises(SchemaViolation) as ei:
        _run_engine(eng, schema=CLASS_SCHEMA, user_treatment=True)
    assert ei.value.callback_only is True              # 剩余违规全部来自 L2.5
    assert all(v.startswith("(validator) ") for v in ei.value.errors)
    assert eng.stats["rejected"] == 1
    assert llm.calls == 2                              # 首调 + 1 轮 L3 修复


def test_user_treatment_false_turns_a_user_schema_call_internal():
    llm = _FixedLLM('{"intent": "qa", "topic": "这是一个很长很长的主题", '
                    '"difficulty": "easy"}')
    eng = SchemaEngine(SPEC_SCHEMA, llm=llm, cfg=OutputConfig(), validator=hook_samples.topic_max6)
    obj, _usage, _attempts, _model = _run_engine(eng, user_treatment=False)
    assert obj["intent"] == "qa"                       # hook 未跑
    assert eng.stats == ZERO_STATS
    assert llm.schemas == [SPEC_SCHEMA]                # 仍按全局 Schema 校验


def test_plain_user_schema_call_is_byte_equivalent_to_v1_12():
    llm = _FixedLLM('{"intent": "qa", "topic": "请假条", "difficulty": "easy"}')
    eng = SchemaEngine(SPEC_SCHEMA, llm=llm, cfg=OutputConfig(), validator=hook_samples.topic_max6)
    _obj, _usage, _attempts, _model = _run_engine(eng)
    assert eng.stats["l0_or_clean"] == 1
    assert llm.schemas == [SPEC_SCHEMA]


# ── v1.18：request-local 可执行后置验证 ─────────────────────────────────────

POST_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
            "additionalProperties": False,
        },
    },
    "required": ["plan"],
    "additionalProperties": False,
}


def _execution(n: int) -> EventExecution:
    """构造能用身份断言钉住的执行证明。"""
    return EventExecution(
        state_before={"n": n - 1},
        state_after={"n": n},
        state_before_hash=f"before-{n}",
        state_after_hash=f"after-{n}",
        publish_snapshot={"nested": {"n": n}},
        normalized_patch=({"op": "replace", "path": "/n", "value": n},),
    )


def _post_request(llm, validator) -> tuple[SchemaEngine, PostValidatedCallRequest]:
    """构造一条完整 post-validated 调用。"""
    prompt = PromptBundle(messages=(Message(
        role="user", parts=(Part(kind="text", text="生成事件"),)),))
    request = PostValidatedCallRequest(
        profile="semantic", prompt=prompt, schema=POST_SCHEMA,
        scope=CallScope(record_ids=("record-1",), batch_no=2),
        post_validator=validator,
    )
    engine = SchemaEngine(SPEC_SCHEMA, llm=llm, cfg=OutputConfig())
    return engine, request


def test_complete_post_validated_returns_same_execution_instance():
    execution = _execution(1)
    seen = []

    def validate(candidate):
        seen.append(candidate)
        return PostValidationResult((), execution)

    llm = _FixedLLM('{"plan":{"n":1}}')
    engine, request = _post_request(llm, validate)
    result = asyncio.run(engine.complete_post_validated(request))
    assert result.object == {"plan": {"n": 1}}
    assert result.event_execution is execution
    assert result.resolved_at == "l0_or_clean"
    assert result.attempts == 1 and llm.calls == 1
    assert seen == [{"plan": {"n": 1}}]
    assert engine.stats == ZERO_STATS


def test_post_schema_recursively_thaws_for_anthropic_structured_body():
    execution = _execution(1)
    llm = _FixedLLM('{"plan":{"n":1}}')
    engine, request = _post_request(
        llm, lambda _candidate: PostValidationResult((), execution))
    assert isinstance(request.schema, MappingProxyType)
    assert isinstance(request.schema["properties"], MappingProxyType)
    assert type(request.schema["properties"]["plan"]["required"]) is tuple
    asyncio.run(engine.complete_post_validated(request))
    sent = llm.schemas[0]
    assert sent == POST_SCHEMA
    assert type(sent) is dict
    assert type(sent["properties"]) is dict
    assert type(sent["properties"]["plan"]["required"]) is list
    assert isinstance(request.schema["properties"], MappingProxyType)
    assert type(request.schema["properties"]["plan"]["required"]) is tuple
    profile = LLMProfile(
        name="semantic", provider="anthropic", base_url="https://example.invalid",
        model="m", api_key_env="K", supports_structured_output=True,
    )
    body = _build_anthropic_body(profile, request.prompt, sent)
    assert body["tools"][0]["input_schema"] == POST_SCHEMA
    assert json.loads(json.dumps(body))["tools"][0]["input_schema"] == POST_SCHEMA


def test_post_validator_runs_once_per_l2_candidate_across_repairs():
    calls = []
    executions = {1: _execution(1), 2: _execution(2)}

    def validate(candidate):
        n = candidate["plan"]["n"]
        calls.append(n)
        if n == 1:
            return PostValidationResult(("secret candidate detail",), None)
        return PostValidationResult((), executions[n])

    llm = _QueueLLM(
        '{"plan":{"n":"invalid"}}',
        '{"plan":{"n":1}}',
        '{"plan":{"n":2}}',
    )
    metrics = _MetricsFeedSpy()
    prompt = PromptBundle(messages=(Message(
        role="user", parts=(Part(kind="text", text="生成事件"),)),))
    request = PostValidatedCallRequest(
        "semantic", prompt, POST_SCHEMA, CallScope(), validate)
    engine = SchemaEngine(SPEC_SCHEMA, llm=llm,
                          cfg=OutputConfig(max_repair_attempts=2), metrics=metrics)
    result = asyncio.run(engine.complete_post_validated(request))
    assert calls == [1, 2]
    assert result.event_execution is executions[2]
    assert result.resolved_at == "l3_2" and result.attempts == 3
    assert metrics.payloads[0]["violations"] == ["post-validator"]
    assert "secret candidate detail" not in json.dumps(metrics.payloads)
    first_repair = llm.prompts[1].messages
    assert first_repair[0] is prompt.messages[0]
    assert first_repair[1].role == "assistant"
    assert first_repair[1].parts[0].text == '{"plan":{"n":"invalid"}}'
    assert first_repair[2].role == "user"
    assert first_repair[2].parts[0].text.startswith("[违规清单]\n1. /plan/n:")
    assert "[原始输出]" not in first_repair[2].parts[0].text
    second_repair = llm.prompts[2].messages
    assert second_repair[0] is prompt.messages[0]
    assert second_repair[1].parts[0].text == '{"plan":{"n":1}}'
    assert second_repair[2].parts[0].text == (
        "[违规清单]\n1. (post-validator) secret candidate detail"
        "\n\n只输出修正后的 JSON。"
    )


def test_post_repair_context_byte_limit_accepts_exact_and_skips_overflow():
    """post L3 对 assistant+user 新增正文执行 R/R-1 边界。"""
    raw = '{"plan":{"n":"invalid"}}'
    rendered = sorted(
        _render_error(error)
        for error in Draft202012Validator(POST_SCHEMA).iter_errors(json.loads(raw))
    )
    instruction = _build_post_repair_instruction(rendered)
    size = len(raw.encode("utf-8")) + len(instruction.encode("utf-8"))
    execution = _execution(2)

    def validate(candidate):
        return PostValidationResult((), execution)

    llm = _QueueLLM(raw, '{"plan":{"n":2}}')
    engine, request = _post_request(llm, validate)
    request = replace(
        request,
        scope=replace(request.scope, repair_context_bytes=size),
    )
    result = asyncio.run(engine.complete_post_validated(request))
    assert result.object == {"plan": {"n": 2}} and llm.calls == 2

    llm = _QueueLLM(raw, '{"plan":{"n":2}}')
    engine, request = _post_request(llm, validate)
    request = replace(
        request,
        scope=replace(request.scope, repair_context_bytes=size - 1),
    )
    with pytest.raises(SchemaViolation):
        asyncio.run(engine.complete_post_validated(request))
    assert llm.calls == 1


@pytest.mark.parametrize("returned", [
    object(),
    PostValidationResult((), None),
    PostValidationResult(("bad",), _execution(1)),
    PostValidationResult("bad", None),
    PostValidationResult((1,), None),
    PostValidationResult((), object()),
])
def test_post_validator_rejects_every_non_contract_shape(returned):
    llm = _FixedLLM('{"plan":{"n":1}}')
    engine, request = _post_request(llm, lambda _candidate: returned)
    with pytest.raises(SchemaViolation) as captured:
        asyncio.run(engine.complete_post_validated(request))
    assert captured.value.errors == ["post_validator_invalid"]
    assert llm.calls == 1


def test_post_validator_exception_is_terminal_and_data_free():
    def raises(_candidate):
        raise RuntimeError("secret candidate content")

    llm = _FixedLLM('{"plan":{"n":1}}')
    engine, request = _post_request(llm, raises)
    with pytest.raises(SchemaViolation) as captured:
        asyncio.run(engine.complete_post_validated(request))
    assert captured.value.errors == ["post_validator_exception"]
    assert "secret" not in str(captured.value)
    assert llm.calls == 1


def test_typed_post_validator_invalid_error_is_terminal_and_data_free():
    from labelkit.common.errors import PostValidatorInvalidError

    def invalid(_candidate):
        raise PostValidatorInvalidError("secret invalid return")

    llm = _FixedLLM('{"plan":{"n":1}}')
    engine, request = _post_request(llm, invalid)
    with pytest.raises(SchemaViolation) as captured:
        asyncio.run(engine.complete_post_validated(request))
    assert captured.value.errors == ["post_validator_invalid"]
    assert "secret" not in str(captured.value)
    assert llm.calls == 1

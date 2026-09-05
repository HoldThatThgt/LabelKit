"""工程后处理公共边界的纯逻辑契约测试；不调用 LLM。"""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import sys
from types import MappingProxyType

import pytest
from jsonschema import Draft202012Validator

from labelkit.common.config._temporal import freeze_json
from labelkit.common.errors import InternalError, PostprocessorError
from labelkit.common.extensions.hooks import ResolvedHook
from labelkit.common.extensions.postprocessing import (
    invoke_postprocessor,
    project_postprocessor_instance,
    project_postprocessor_schema,
    resolve_postprocessor,
)

MARKER = "x-labelkit-postprocessor"
TIME = "x-labelkit-business-time"


def _object(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def _schema():
    item = _object({"value": {"type": "string"}, "start": {"type": "integer", MARKER: True}},
                   ("value", "start"))
    return _object({"entities": {"type": "array", "items": item}, "text": {"type": "string"}},
                   ("entities", "text"))


def test_items_projection_removes_required_code_fields_and_rejects_provider_values():
    schema = _schema()
    source = deepcopy(schema)
    full = {"text": "hello", "entities": [{"value": "hello", "start": 0}]}
    model = project_postprocessor_schema(freeze_json(schema))
    projected = project_postprocessor_instance(full, schema)
    assert projected == {"text": "hello", "entities": [{"value": "hello"}]}
    assert Draft202012Validator(model).is_valid(projected)
    assert not Draft202012Validator(model).is_valid(full)
    assert model["required"] == ["entities", "text"]
    assert model["properties"]["entities"]["items"]["required"] == ["value"]
    for missing in ({}, {"entities": []}, {"text": "hello"}, {"text": "hello", "entities": [{}]}):
        assert not Draft202012Validator(model).is_valid(missing)
    assert Draft202012Validator(schema).is_valid(full)
    assert not Draft202012Validator(schema).is_valid(projected)
    assert schema == source
    assert full["entities"][0]["start"] == 0


def test_optional_owned_property_does_not_invent_a_required_array():
    schema = {"type": "object", "properties": {"code": {"type": "integer", MARKER: True}},
              "additionalProperties": False}
    model = project_postprocessor_schema(schema)
    assert model == {"type": "object", "properties": {}, "additionalProperties": False}
    assert project_postprocessor_instance({}, schema) == {}
    assert Draft202012Validator(schema).is_valid({})


def test_nested_arrays_optional_nullable_parents_and_empty_objects():
    item = _object({"value": {"type": "string"}, "computed": {"type": "integer", MARKER: True}}, ("computed",))
    item["type"] = ["object", "null"]
    array = {"type": ["array", "null"], "items": {"type": "array", "items": item}, "minItems": 0, "maxItems": 10}
    schema = _object({"groups": array, "empty": _object({"computed": {"type": "integer", MARKER: True}})})
    model = project_postprocessor_schema(schema)
    value = {"groups": [[None, {"computed": 1}, {"value": "x", "computed": 2}]], "empty": {"computed": 1}}
    assert project_postprocessor_instance(value, schema) == {"groups": [[None, {}, {"value": "x"}]], "empty": {}}
    projected = {"groups": [[None, {}, {"value": "x"}]], "empty": {}}
    assert Draft202012Validator(model).is_valid(projected)
    assert not Draft202012Validator(model).is_valid(value)
    inner = model["properties"]["groups"]["items"]["items"]
    assert inner["properties"] == {"value": {"type": "string"}}
    assert inner["required"] == []
    assert model["properties"]["groups"]["minItems"] == 0
    assert model["properties"]["groups"]["maxItems"] == 10
    assert not Draft202012Validator(model).is_valid({"groups": [[]] * 11})
    assert model["properties"]["empty"]["required"] == []
    for value in ({}, {"groups": None}, {"groups": "bad"}, {"groups": [None]}, {"empty": 1}):
        assert project_postprocessor_instance(value, schema) == value


def test_default_examples_projection_and_same_named_data_are_preserved():
    schema = _schema()
    schema["properties"]["metadata"] = {"type": "object"}
    value = {"text": "x", "entities": [{"value": "x", "start": 4}], "metadata": {MARKER: "business data"}}
    schema["default"] = value
    schema["examples"] = [value]
    schema["properties"]["entities"]["default"] = value["entities"]
    schema["properties"]["entities"]["examples"] = [value["entities"]]
    model = project_postprocessor_schema(schema)
    assert model["default"]["metadata"] == {MARKER: "business data"}
    assert model["default"]["entities"] == [{"value": "x"}]
    assert Draft202012Validator(model).is_valid(model["default"])
    assert model["examples"][0]["entities"] == [{"value": "x"}]
    assert model["properties"]["entities"]["default"] == [{"value": "x"}]
    assert model["properties"]["entities"]["examples"] == [[{"value": "x"}]]


def test_enum_data_key_named_marker_is_preserved_with_and_without_projection():
    enum_schema = {"type": "object", "enum": [{MARKER: True}, {"other": "value"}]}
    assert project_postprocessor_schema(enum_schema) == enum_schema
    schema = _schema()
    schema["properties"]["metadata"] = enum_schema
    value = {"text": "x", "entities": [{"value": "x", "start": 0}], "metadata": {MARKER: True}}
    model = project_postprocessor_schema(schema)
    projected = project_postprocessor_instance(value, schema)
    assert model["properties"]["metadata"] == enum_schema
    assert projected["metadata"] == {MARKER: True}
    assert Draft202012Validator(model).is_valid(projected)
    assert Draft202012Validator(schema).is_valid(value)


def test_unmarked_schemas_and_unaffected_siblings_remain_unchanged():
    untouched = {"anyOf": [{"type": "string"}, {"type": "null"}], "const": {MARKER: "data"}}
    schema = {"type": "object", "properties": {"data": untouched}, "additionalProperties": True}
    assert project_postprocessor_schema(schema) == schema
    schema = _schema()
    schema["properties"]["other"] = untouched
    schema["additionalProperties"] = True
    assert project_postprocessor_schema(schema)["properties"]["other"] == untouched


@pytest.mark.parametrize("target", [
    {"type": "string", "const": "x"},
    {"type": "array", "items": {"type": "integer"}, "enum": [[1], [2]]},
    {"$ref": "#/$defs/code", "allOf": [{"type": "object"}]},
])
def test_whole_marked_property_can_have_complex_final_schema(target):
    schema = _object({"code": {**target, MARKER: True}}, ("code",))
    schema["$defs"] = {"code": {"type": "object"}}
    model = project_postprocessor_schema(schema)
    assert model["properties"] == {}
    assert model["required"] == []
    assert schema["properties"]["code"] == {**target, MARKER: True}


@pytest.mark.parametrize("value", [None, True, {}])
def test_open_direct_object_is_rejected_without_closing_other_ancestors(value):
    schema = _object({"code": {"type": "integer", MARKER: True}})
    if value is None:
        del schema["additionalProperties"]
    else:
        schema["additionalProperties"] = value
    with pytest.raises(ValueError, match="additionalProperties"):
        project_postprocessor_schema(schema)


@pytest.mark.parametrize("keyword,value", [
    ("$ref", "#/$defs/part"), ("$dynamicRef", "#anchor"), ("allOf", [{}]), ("anyOf", [{}]),
    ("oneOf", [{}]), ("not", {}), ("if", {}), ("then", {}), ("else", {}),
    ("dependentRequired", {}), ("dependentSchemas", {}), ("patternProperties", {}),
    ("propertyNames", {}), ("unevaluatedProperties", False), ("const", {}), ("enum", [{}]),
    ("minProperties", 1), ("maxProperties", 5),
])
def test_affected_object_constraints_rejected(keyword, value):
    schema = _object({"code": {"type": "integer", MARKER: True}})
    schema[keyword] = value
    with pytest.raises(ValueError, match="forbid"):
        project_postprocessor_schema(schema)


@pytest.mark.parametrize("keyword,value", [
    ("prefixItems", [{}]), ("contains", {}), ("minContains", 0), ("maxContains", 2), ("uniqueItems", True),
])
def test_affected_array_constraints_rejected(keyword, value):
    schema = _schema()
    schema["properties"]["entities"][keyword] = value
    with pytest.raises(ValueError, match="array projection forbids"):
        project_postprocessor_schema(schema)


@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_marker_value_is_strict_json_true(value):
    schema = _object({"code": {"type": "integer", MARKER: value}})
    with pytest.raises(ValueError, match="must equal true"):
        project_postprocessor_schema(schema)


@pytest.mark.parametrize("schema", [
    {"type": "object", MARKER: True},
    {"type": "array", "items": {"type": "integer", MARKER: True}},
    {"type": "object", "$defs": {"item": _object({"code": {MARKER: True}})}},
    {"type": "object", "allOf": [_object({"code": {MARKER: True}})]},
    {"type": "object", "not": _object({"code": {MARKER: True}})},
])
def test_marker_requires_supported_property_route(schema):
    with pytest.raises(ValueError, match="explicit property"):
        project_postprocessor_schema(schema)


@pytest.mark.parametrize("schema", [
    _object({"code": {"type": "integer", MARKER: True, TIME: True}}),
    _object({"code": {**_object({"time": {"type": "integer", TIME: True}}), MARKER: True}}),
    _object({"time": {**_object({"code": {"type": "integer", MARKER: True}}), TIME: True}}),
])
def test_business_time_ownership_overlap_is_rejected(schema):
    with pytest.raises(ValueError, match="overlap business time"):
        project_postprocessor_schema(schema)


def test_nested_ownership_bad_ancestor_type_and_invalid_projected_schema():
    schema = _object({"code": {**_object({"nested": {MARKER: True}}), MARKER: True}})
    with pytest.raises(ValueError, match="must not be nested"):
        project_postprocessor_schema(schema)
    schema = _object({"code": {MARKER: True}})
    del schema["type"]
    with pytest.raises(ValueError, match="ancestor type"):
        project_postprocessor_schema(schema)
    schema["type"] = "object"
    schema["minLength"] = -1
    with pytest.raises(ValueError, match="projected model Schema is invalid"):
        project_postprocessor_schema(schema)


@pytest.mark.parametrize("schema", [
    {"type": "array", "properties": {"code": {MARKER: True}}, "additionalProperties": False},
    {"type": "object", "items": _object({"code": {MARKER: True}})},
])
def test_affected_edges_must_match_their_object_or_array_type(schema):
    with pytest.raises(ValueError, match="ancestor type must match"):
        project_postprocessor_schema(schema)


def test_unaffected_items_do_not_turn_an_object_into_an_array_ancestor():
    schema = _object({"code": {MARKER: True}, "value": {"type": "string"}})
    schema.update(items={"type": "integer"}, uniqueItems=True, contains={"minimum": 0})
    model = project_postprocessor_schema(schema)
    assert model["properties"] == {"value": {"type": "string"}}
    assert model["items"] == schema["items"] and model["uniqueItems"] is True


def test_boolean_schema_siblings_and_regular_key_named_marker():
    schema = _object({"code": {MARKER: True}, "untouched": True, MARKER: {"type": "string"}})
    model = project_postprocessor_schema(schema)
    assert model["properties"] == {"untouched": True, MARKER: {"type": "string"}}
    assert project_postprocessor_instance({"untouched": {"x": 1}, MARKER: "data"}, schema) == {
        "untouched": {"x": 1}, MARKER: "data"}


def test_invoke_copies_nested_inputs_and_result_without_changing_ordinary_fields_policy():
    retained = {}
    calls = []
    obj = {"value": "  hi ", "nested": {"values": [1]}}
    record = {"text": "source", "nested": [1]}
    def hook(candidate, raw):
        calls.append((candidate, raw))
        candidate["value"] = candidate["value"].strip()
        candidate["nested"]["values"].append(2)
        raw["nested"].append(3)
        retained["candidate"] = candidate
        retained["raw"] = raw
        return candidate
    result = invoke_postprocessor(ResolvedHook("hooks.py:normalise", hook), freeze_json(obj), freeze_json(record))
    assert len(calls) == 1
    assert result == {"value": "hi", "nested": {"values": [1, 2]}}
    retained["candidate"]["nested"]["values"].append(9)
    assert result["nested"]["values"] == [1, 2]
    assert obj["nested"]["values"] == record["nested"] == [1]


def test_returned_object_completely_replaces_candidate_without_implicit_merge():
    candidate = {"value": "original", "optional": "remove", "nested": {"items": [1]}}
    returned = {"value": "normalized", "nested": {"items": [2]}}
    def complete(obj, record):
        obj["temporary"] = "also removed"
        return returned
    result = invoke_postprocessor(ResolvedHook("hooks.py:complete", complete), candidate, None)
    assert result == {"value": "normalized", "nested": {"items": [2]}}
    returned["nested"]["items"].append(3)
    assert result["nested"]["items"] == [2]
    assert candidate == {"value": "original", "optional": "remove", "nested": {"items": [1]}}


@pytest.mark.parametrize("raw", [None, {}])
def test_missing_raw_remains_distinct_from_an_empty_raw_object(raw):
    seen = []
    def complete(obj, record):
        seen.append(record)
        return {"has_raw": record is not None}
    result = invoke_postprocessor(ResolvedHook("hooks.py:complete", complete), {}, raw)
    assert seen == [raw]
    assert result == {"has_raw": raw is not None}


@pytest.mark.parametrize("returned", [
    None, "text", 1, [], (), set(), MappingProxyType({}), {"bad": ()}, {"bad": {1}},
    {1: "bad"}, {"bad": object()}, {"bad": float("nan")}, {"bad": float("inf")}, {"bad": float("-inf")},
])
def test_invalid_returns_fail_with_fixed_internal_error(returned, caplog):
    def hook(obj, record):
        return returned
    with pytest.raises(PostprocessorError, match="^postprocessor_error$") as captured:
        invoke_postprocessor(ResolvedHook("hooks.py:invalid", hook), {}, None)
    assert isinstance(captured.value, InternalError)
    assert "Postprocessor failed" in caplog.text


def test_cycles_fail_but_shared_references_are_copied_independently():
    cycle = {}
    cycle["cycle"] = cycle
    def cyclic(obj, record):
        return cycle
    with pytest.raises(PostprocessorError):
        invoke_postprocessor(ResolvedHook("hooks.py:cyclic", cyclic), {}, None)
    shared = [1]
    def aliases(obj, record):
        return {"a": shared, "b": shared, "ratio": 0.5, "enabled": True, "empty": None}
    result = invoke_postprocessor(ResolvedHook("hooks.py:aliases", aliases), {}, None)
    result["a"].append(2)
    assert result["b"] == shared == [1]


def test_exception_and_bad_input_never_leak_business_data(caplog):
    def exploding(obj, record):
        raise RuntimeError("PRIVATE BUSINESS SECRET")
    with pytest.raises(PostprocessorError) as captured:
        invoke_postprocessor(ResolvedHook("hooks.py:explode", exploding), {"secret": "PRIVATE DATA"}, None)
    assert "PRIVATE" not in str(captured.value) + caplog.text
    with pytest.raises(PostprocessorError):
        invoke_postprocessor(ResolvedHook("hooks.py:explode", exploding), {"bad": object()}, None)


def _load(tmp_path, source, attribute="process"):
    (tmp_path / "hooks.py").write_text(source, encoding="utf-8")
    return resolve_postprocessor(f"hooks.py:{attribute}", tmp_path)


def test_resolver_checks_without_calling_function_and_supports_file_forms(tmp_path, monkeypatch):
    original_path = list(sys.path)
    hook = _load(tmp_path, "def process(obj, record=None):\n    raise AssertionError('must not run')\n")
    assert sys.path == original_path
    assert hook.reference.endswith("/hooks.py:process")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    same = resolve_postprocessor(hook.reference, elsewhere)
    assert same.reference == hook.reference
    _load(tmp_path, "class Gate:\n    @staticmethod\n    def process(obj, record, /):\n        return obj\n", "Gate.process")
    assert sys.path == original_path


def test_resolved_postprocessor_reference_and_callable_are_immutable(tmp_path):
    hook = _load(tmp_path, "def process(obj, record): return {'value': 'original'}")
    original_reference, original_target = hook.reference, hook.target
    with pytest.raises(FrozenInstanceError):
        hook.reference = "elsewhere.py:changed"
    with pytest.raises(FrozenInstanceError):
        hook.target = lambda obj, record: {"value": "changed"}
    assert hook.reference == original_reference and hook.target is original_target
    assert invoke_postprocessor(hook, {}, None) == {"value": "original"}


def test_two_projects_with_same_hook_filename_keep_distinct_frozen_callables(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    hook_a = _load(first, "def process(obj, record): return {'project': 'first'}")
    hook_b = _load(second, "def process(obj, record): return {'project': 'second'}")
    assert hook_a.target.__module__ != hook_b.target.__module__
    assert invoke_postprocessor(hook_a, {}, None) == {"project": "first"}
    assert invoke_postprocessor(hook_b, {}, None) == {"project": "second"}


@pytest.mark.parametrize("source", [
    "async def process(obj, record): pass",
    "def process(obj, record): yield obj",
    "async def process(obj, record): yield obj",
    "def process(obj, record, extra): pass",
    "def process(obj, record, **kwargs): pass",
    "def process(obj, *args): pass",
    "def process(obj, *, record): pass",
    "def process(obj=None, record=None): pass",
    "def process(obj, record=1): pass",
    "class Gate:\n    async def __call__(self, obj, record): pass\nprocess = Gate()",
    "class Gate:\n    def __call__(self, obj, record): yield obj\nprocess = Gate()",
    "def process(obj, record): pass\nprocess.__signature__ = 42",
])
def test_resolver_rejects_non_sync_or_non_exact_signatures(tmp_path, source):
    with pytest.raises(ValueError, match="synchronous with exactly two positional"):
        _load(tmp_path, source)


def test_resolver_load_failure_is_sanitized(tmp_path, caplog):
    with pytest.raises(ValueError, match="cannot load postprocessor") as captured:
        _load(tmp_path, "raise RuntimeError('PRIVATE MODULE ERROR')")
    assert "PRIVATE" not in str(captured.value) + caplog.text
    with pytest.raises(ValueError):
        _load(tmp_path, "process = 5")
    with pytest.raises(ValueError):
        resolve_postprocessor("missing.py:process", tmp_path)


def test_signature_introspection_exception_is_sanitized(tmp_path, caplog):
    source = """
class Gate:
    @property
    def __signature__(self):
        raise RuntimeError("PRIVATE SIGNATURE ERROR")
    def __call__(self, obj, record):
        return obj
process = Gate()
"""
    with pytest.raises(ValueError, match="synchronous with exactly two positional") as captured:
        _load(tmp_path, source)
    assert "PRIVATE" not in str(captured.value) + caplog.text


def test_record_default_must_be_none_by_identity_without_invoking_equality(tmp_path, caplog):
    source = """
class Default:
    def __eq__(self, other):
        raise RuntimeError("PRIVATE DEFAULT EQUALITY EXECUTED")
def process(obj, record=Default()):
    return obj
"""
    with pytest.raises(ValueError, match="synchronous with exactly two positional") as captured:
        _load(tmp_path, source)
    assert "PRIVATE" not in str(captured.value) + caplog.text

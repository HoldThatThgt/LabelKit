"""Offline unit tests for labelkit/common/extensions/hooks.py (v1.5 plan A). Pure logic — no LLM."""
from __future__ import annotations

import pytest

from labelkit.common.contracts.types import (
    SequenceValidationFrame,
    SequenceValidationInput,
)
from labelkit.common.extensions.hooks import (
    clone_sequence_input,
    invoke_sequence_hook,
    normalize_violations,
    resolve_hook,
    resolve_sequence_hook,
)


def test_resolve_hook_happy_path():
    fn = resolve_hook("tests.hook_samples:topic_max6")
    assert fn({"topic": "这是一个很长很长的主题"}, None)
    assert fn({"topic": "请假条"}, None) == []


def test_resolve_hook_bad_format():
    for ref in ("no-colon", ":fn", "mod:", "  :  "):
        with pytest.raises(ValueError, match="module:function"):
            resolve_hook(ref)


def test_resolve_hook_import_and_attr_errors():
    with pytest.raises(ValueError, match="cannot import module"):
        resolve_hook("no_such_module_xyz:fn")
    with pytest.raises(ValueError, match="not found"):
        resolve_hook("tests.hook_samples:missing_fn")


def test_resolve_hook_not_callable():
    with pytest.raises(ValueError, match="is not callable"):
        resolve_hook("tests.hook_samples:NOT_CALLABLE")


def test_normalize_violations():
    assert normalize_violations(None, "r") == []
    assert normalize_violations([], "r") == []
    assert normalize_violations(("a", 1), "r") == ["a", "1"]
    with pytest.raises(TypeError, match="must return list"):
        normalize_violations("nope", "r")


def _sequence_input() -> SequenceValidationInput:
    """构造序列级钩子测试输入。"""
    return SequenceValidationInput(
        sequence_class="booking",
        tier_rank=2,
        frames=(SequenceValidationFrame(
            position=0,
            frame_class="request",
            payload={"nested": {"value": "original"}, "items": [1]},
        ),),
    )


def test_sequence_hook_resolves_with_one_argument_contract():
    hook = resolve_sequence_hook("tests.hook_samples:sequence_ok")
    assert hook(_sequence_input()) == []


def test_clone_sequence_input_deep_copies_payload_and_preserves_metadata():
    original = _sequence_input()
    cloned = clone_sequence_input(original)
    assert cloned is not original
    assert cloned.sequence_class == "booking"
    assert cloned.tier_rank == 2
    assert cloned.frames[0] is not original.frames[0]
    cloned.frames[0].payload["nested"]["value"] = "changed"
    cloned.frames[0].payload["items"].append(2)
    assert original.frames[0].payload == {
        "nested": {"value": "original"}, "items": [1]}


def test_invoke_sequence_hook_normalizes_pass_and_violation():
    value = _sequence_input()
    assert invoke_sequence_hook("tests.hook_samples:sequence_ok", value) == []
    assert invoke_sequence_hook("tests.hook_samples:sequence_reject", value) == [
        "sequence rejected"]


def test_invoke_sequence_hook_isolates_user_mutation():
    value = _sequence_input()
    assert invoke_sequence_hook("tests.hook_samples:sequence_mutates", value) == []
    assert value.frames[0].payload["nested"]["value"] == "original"


def test_invoke_sequence_hook_propagates_exception_for_m6_isolation():
    with pytest.raises(RuntimeError, match="sequence hook exploded"):
        invoke_sequence_hook("tests.hook_samples:sequence_boom", _sequence_input())


def test_invoke_sequence_hook_rejects_invalid_return_type():
    with pytest.raises(TypeError, match="must return list"):
        invoke_sequence_hook("tests.hook_samples:sequence_bad_return", _sequence_input())

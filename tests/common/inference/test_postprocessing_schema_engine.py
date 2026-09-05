"""标注后处理接入结构引擎的控制流与隔离回归。"""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from labelkit.common.config.model import OutputConfig
from labelkit.common.contracts.types import Usage
from labelkit.common.errors import PostprocessorError
from labelkit.common.extensions.hooks import ResolvedHook
from labelkit.common.extensions.postprocessing import invoke_postprocessor
from labelkit.common.inference.llm_client import Message, Part, PromptBundle
from labelkit.common.inference.schema_engine import (
    CallScope,
    FinalizedCallRequest,
    SchemaEngine,
)


MODEL_SCHEMA = {
    "type": "object",
    "properties": {
        "payload": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
            "additionalProperties": False,
        },
    },
    "required": ["payload"],
    "additionalProperties": False,
}
FULL_SCHEMA = {
    "type": "object",
    "properties": {
        "payload": {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "derived": {"type": "integer", "minimum": 0},
            },
            "required": ["label", "derived"],
            "additionalProperties": False,
        },
    },
    "required": ["payload"],
    "additionalProperties": False,
}
ZERO_STATS = {"l0_or_clean": 0, "l1": 0, "l3_1": 0, "l3_2": 0, "rejected": 0}


def _prompt() -> PromptBundle:
    return PromptBundle(messages=(Message(
        role="user", parts=(Part(kind="text", text="Generate annotation"),)),))


class _QueueLLM:
    """以进程内对象驱动真实结构引擎，不模拟服务端或传输。"""

    def __init__(self, *values: object):
        self.values = values
        self.calls = 0
        self.schemas = []
        self.prompts = []

    async def complete(self, _profile, prompt, response_schema=None):
        if self.calls >= len(self.values):
            raise AssertionError("unexpected extra provider call")
        value = self.values[self.calls]
        self.calls += 1
        self.prompts.append(prompt)
        self.schemas.append(response_schema)
        structured = value if isinstance(value, dict) else None
        text = value if isinstance(value, str) else ""
        return SimpleNamespace(
            structured=structured,
            text=text,
            usage=Usage(5, 2),
            model="local-test-model",
        )


def _request(finalizer, *, scope=CallScope(), projector=lambda candidate: candidate):
    return FinalizedCallRequest(
        profile="semantic",
        prompt=_prompt(),
        model_schema=MODEL_SCHEMA,
        final_schema=FULL_SCHEMA,
        scope=scope,
        candidate_finalizer=finalizer,
        repair_projector=projector,
    )


def test_complete_validated_deeply_isolates_validator_candidate_and_record():
    source = {"payload": {"label": "source"}}
    record = {"nested": {"values": ["source"]}}

    def validator(candidate, raw):
        candidate["payload"]["label"] = "validator mutation"
        raw["nested"]["values"].append("validator mutation")
        return []

    llm = _QueueLLM(source)
    engine = SchemaEngine(MODEL_SCHEMA, llm, OutputConfig(), validator=validator)
    result = asyncio.run(engine.complete_validated(
        "semantic", _prompt(), scope=CallScope(record=record)))

    assert result[0] == {"payload": {"label": "source"}}
    assert source == {"payload": {"label": "source"}}
    assert record == {"nested": {"values": ["source"]}}
    assert llm.calls == 1


def test_complete_validated_l3_gives_each_validator_call_fresh_deep_copies():
    first = {"payload": {"label": "first"}}
    second = {"payload": {"label": "second"}}
    record = {"nested": {"value": "source"}}
    seen = []

    def validator(candidate, raw):
        seen.append((candidate["payload"]["label"], raw["nested"]["value"]))
        candidate["payload"]["label"] = "validator mutation"
        raw["nested"]["value"] = "validator mutation"
        return ["retry"] if seen[-1][0] == "first" else []

    llm = _QueueLLM(first, second)
    engine = SchemaEngine(
        MODEL_SCHEMA, llm, OutputConfig(max_repair_attempts=1), validator=validator)
    result = asyncio.run(engine.complete_validated(
        "semantic", _prompt(), scope=CallScope(record=record)))

    assert result == ({"payload": {"label": "second"}}, Usage(10, 4), 2,
                      "local-test-model")
    assert seen == [("first", "source"), ("second", "source")]
    assert first == {"payload": {"label": "first"}}
    assert second == {"payload": {"label": "second"}}
    assert record == {"nested": {"value": "source"}}
    assert engine.stats["l3_1"] == 1


def test_complete_finalized_orders_once_and_isolates_all_mutable_boundaries():
    source = {"payload": {"label": " source "}}
    record = {"nested": {"values": ["source"]}}
    retained = []
    order = []

    def finalizer(candidate):
        order.append("postprocessor")
        retained.append(candidate)
        candidate["payload"]["label"] = candidate["payload"]["label"].strip()
        candidate["payload"]["derived"] = len(candidate["payload"]["label"])
        return candidate

    def validator(candidate, raw):
        order.append("validator")
        assert candidate == {"payload": {"label": "source", "derived": 6}}
        candidate["payload"]["label"] = "validator mutation"
        raw["nested"]["values"].append("validator mutation")
        return []

    llm = _QueueLLM(source)
    engine = SchemaEngine(MODEL_SCHEMA, llm, OutputConfig(), validator=validator)
    request = _request(
        finalizer,
        scope=CallScope(record=record, user_treatment=True),
    )
    result = asyncio.run(engine.complete_finalized(request))

    assert result[0] == {"payload": {"label": "source", "derived": 6}}
    assert order == ["postprocessor", "validator"]
    assert source == {"payload": {"label": " source "}}
    assert record == {"nested": {"values": ["source"]}}
    retained[0]["payload"]["label"] = "late mutation"
    assert result[0]["payload"]["label"] == "source"
    assert llm.schemas == [MODEL_SCHEMA]
    assert engine.stats["l0_or_clean"] == 1


def test_complete_finalized_l3_processes_each_new_candidate_once():
    first = {"payload": {"label": "first"}}
    second = {"payload": {"label": "second"}}
    record = {"nested": {"value": "source"}}
    finalized = []
    projected = []
    validated = []

    def finalizer(candidate):
        finalized.append(candidate["payload"]["label"])
        candidate["payload"]["derived"] = len(candidate["payload"]["label"])
        return candidate

    def projector(candidate):
        projected.append(candidate["payload"]["label"])
        return candidate

    def validator(candidate, raw):
        validated.append((candidate["payload"]["label"], raw["nested"]["value"]))
        candidate["payload"]["label"] = "validator mutation"
        raw["nested"]["value"] = "validator mutation"
        return ["retry"] if validated[-1][0] == "first" else []

    llm = _QueueLLM(first, second)
    engine = SchemaEngine(
        MODEL_SCHEMA, llm, OutputConfig(max_repair_attempts=1), validator=validator)
    request = _request(
        finalizer,
        scope=CallScope(record=record, user_treatment=True),
        projector=projector,
    )
    result = asyncio.run(engine.complete_finalized(request))

    assert result == ({"payload": {"label": "second", "derived": 6}},
                      Usage(10, 4), 2, "local-test-model")
    assert finalized == ["first", "second"]
    assert projected == ["first"]
    assert validated == [("first", "source"), ("second", "source")]
    assert first == {"payload": {"label": "first"}}
    assert second == {"payload": {"label": "second"}}
    assert record == {"nested": {"value": "source"}}
    assert llm.schemas == [MODEL_SCHEMA, MODEL_SCHEMA]
    assert engine.stats["l3_1"] == 1


def test_postprocessor_error_on_repair_candidate_is_terminal_and_unwrapped(caplog):
    invalid = {"payload": {"label": 1}}
    valid = {"payload": {"label": "secret candidate"}}
    finalizer_calls = []
    validator_calls = []

    def failing_hook(candidate, _record):
        finalizer_calls.append(candidate["payload"]["label"])
        raise RuntimeError("secret exception text")

    hook = ResolvedHook(reference="/safe/hooks.py:complete", target=failing_hook)

    def finalizer(candidate):
        return invoke_postprocessor(hook, candidate, None)

    def validator(candidate, _raw):
        validator_calls.append(candidate)
        return []

    llm = _QueueLLM(invalid, valid)
    engine = SchemaEngine(
        MODEL_SCHEMA, llm, OutputConfig(max_repair_attempts=2), validator=validator)
    request = _request(finalizer, scope=CallScope(user_treatment=True))

    with caplog.at_level(logging.ERROR, logger="labelkit"):
        with pytest.raises(PostprocessorError) as captured:
            asyncio.run(engine.complete_finalized(request))

    assert str(captured.value) == "postprocessor_error"
    assert finalizer_calls == ["secret candidate"]
    assert validator_calls == []
    assert llm.calls == 2
    assert engine.stats == ZERO_STATS
    assert "secret candidate" not in caplog.text
    assert "secret exception text" not in caplog.text
    assert "reason=exception" in caplog.text
    assert "/safe/hooks.py:complete" in caplog.text

"""M8 通用后校验接缝的行为（SPEC-image-region-segmentation §10.4，AC-75、AC-127）。

只用 in-process 的 LLM 桩（QueueEngine 惯例），不 mock 服务端或传输层。
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from labelkit.common.config.model import OutputConfig
from labelkit.common.contracts.post_validation import (
    PostValidatedCallRequest,
    PostValidationResult,
    ValidatedPostCall,
)
from labelkit.common.contracts.regions import RegionOverlayRef, RegionSet
from labelkit.common.contracts.types import ImageRef, Usage
from labelkit.common.errors import ContextOverflowError, PostValidatorInvalidError, SchemaViolation
from labelkit.common.inference import schema_engine as schema_engine_module
from labelkit.common.inference.llm_client import Message, Part, PromptBundle, _render_trace_messages
from labelkit.common.inference.schema_engine import (
    CallScope,
    SchemaEngine,
    _build_post_repair_instruction,
    _render_error,
)

SCHEMA = {
    "type": "object",
    "properties": {"n": {"type": "integer"}},
    "required": ["n"],
    "additionalProperties": False,
}
PROOF = object()
IMAGE = Part(kind="image", image=ImageRef(Path("image_1.png"), "png", 1))
PROMPT = PromptBundle(
    messages=(
        Message(role="system", parts=(Part(kind="text", text="系统"),)),
        Message(role="user", parts=(Part(kind="text", text="[图片]"), IMAGE)),
    ),
    image_px=512,
)
OK = PostValidationResult((), PROOF)
BAD = PostValidationResult(("/regions/0: exactly one of box_id and polygons must be set",), None)
PREFIXED = "(post-validator) /regions/0: exactly one of box_id and polygons must be set"


class _Metrics:
    """记录 trace 事件与熔断喂入。"""

    def __init__(self):
        self.fed = []
        self.payloads = []

    def event(self, _event, *, stage, batch_no, record_ids=(), payload=None):
        self.payloads.append(payload or {})

    def record_provider_result(self, fatal, *, hard=False):
        self.fed.append(fatal)


class _QueueLLM:
    """按队列返回文本，队列项为异常时抛出；每轮用量固定 (5, 2)。"""

    def __init__(self, *items):
        self.items = list(items)
        self.calls = 0
        self.profiles = []
        self.prompts = []
        self.schemas = []

    async def complete(self, profile, prompt, response_schema=None):
        self.calls += 1
        self.profiles.append(profile)
        self.prompts.append(prompt)
        self.schemas.append(response_schema)
        item = self.items[min(self.calls - 1, len(self.items) - 1)]
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=item, structured=None, usage=Usage(5, 2), model=f"model-{self.calls}")


def _run(llm, validator, **options):
    """用真实 SchemaEngine 跑一次通用接缝。

    @param llm 引擎桩
    @param validator 后校验函数
    @param options 可选 cfg、scope、metrics、prompt
    @return 接缝返回的载体
    """
    cfg = options.get("cfg") or OutputConfig(max_repair_attempts=2)
    engine = SchemaEngine({}, llm=llm, cfg=cfg, metrics=options.get("metrics"))
    request = PostValidatedCallRequest("segment", options.get("prompt", PROMPT), SCHEMA,
                                       options.get("scope") or CallScope(), validator)
    return asyncio.run(engine.complete_post_validated(request))


@pytest.mark.parametrize("returned", [
    PostValidationResult((), PROOF),
    PostValidationResult((), RegionSet(10, 10, (), ())),
    PostValidationResult((), {"any": "object"}),
    PostValidationResult((), 0),
    PostValidationResult((), ""),
], ids=["object", "region_set", "mapping", "falsy_int", "falsy_str"])
def test_proof_shape_rules(returned):
    """成功形状只看「违规为空且 proof 非空」，接缝不判定 proof 类型或真假值。"""
    llm = _QueueLLM('{"n":1}')
    result = _run(llm, lambda _candidate: returned)
    assert isinstance(result, ValidatedPostCall)
    assert result.proof is returned.proof
    assert llm.calls == 1


@pytest.mark.parametrize("returned", [
    object(),
    None,
    PostValidationResult((), None),
    PostValidationResult(("bad",), PROOF),
    PostValidationResult("bad", None),
    PostValidationResult((1,), None),
    PostValidationResult(("",), None),
    PostValidationResult(("   ",), None),
], ids=["not_result", "none", "empty_both", "both_set", "str_violations", "non_str_item", "empty_item", "blank_item"])
def test_every_other_shape_is_post_validator_invalid(returned):
    llm = _QueueLLM('{"n":1}')
    with pytest.raises(SchemaViolation) as caught:
        _run(llm, lambda _candidate: returned)
    assert caught.value.errors == ["post_validator_invalid"]
    assert caught.value.raw_last_output == ""
    assert caught.value.usage is None
    assert llm.calls == 1


@pytest.mark.parametrize("rounds_before_success", [0, 1, 2])
def test_returns_same_proof_instance(rounds_before_success):
    """首轮与每个 L3 轮都原样返回校验器给出的同一实例，并报告对应解析路径与累计用量。"""
    proof = RegionSet(1080, 2340, (), ())
    calls = []

    def validate(candidate):
        calls.append(candidate["n"])
        return PostValidationResult((), proof) if candidate["n"] == rounds_before_success else BAD

    llm = _QueueLLM(*[json.dumps({"n": index}) for index in range(rounds_before_success + 1)])
    result = _run(llm, validate)
    assert result.proof is proof
    assert result.object == {"n": rounds_before_success}
    assert result.resolved_at == ("l0_or_clean", "l3_1", "l3_2")[rounds_before_success]
    assert result.attempts == rounds_before_success + 1
    assert result.usage == Usage(5 * (rounds_before_success + 1), 2 * (rounds_before_success + 1))
    assert result.model == "model-1"
    assert calls == list(range(rounds_before_success + 1))


def test_l1_repaired_first_response_resolves_as_l1():
    llm = _QueueLLM('```json\n{"n":1}\n```')
    result = _run(llm, lambda _candidate: OK)
    assert result.resolved_at == "l1"
    assert result.proof is PROOF


@pytest.mark.parametrize("retain", [False, True])
def test_repair_replays_first_prompt_with_images_and_image_px(retain):
    """修复轮恒重放首轮 prompt，图片与 image_px 都在，且不读取 retain_repair_prompt。"""
    llm = _QueueLLM('{"n":"bad"}', '{"n":1}', '{"n":2}')
    outcomes = iter((BAD, OK))
    result = _run(llm, lambda _candidate: next(outcomes),
                  scope=CallScope(retain_repair_prompt=retain))
    assert result.resolved_at == "l3_2"
    first = llm.prompts[0]
    assert first is PROMPT
    for raw, repair in (('{"n":"bad"}', llm.prompts[1]), ('{"n":1}', llm.prompts[2])):
        assert repair.image_px == 512
        assert repair.messages[:2] == PROMPT.messages
        assert repair.messages[0] is PROMPT.messages[0]
        assert repair.messages[1] is PROMPT.messages[1]
        assert repair.messages[1].parts[1] is IMAGE
        assert [message.role for message in repair.messages] == ["system", "user", "assistant", "user"]
        assert repair.messages[2].parts[0].text == raw
    assert llm.prompts[2].messages[3].parts[0].text == (
        f"[违规清单]\n1. {PREFIXED}\n\n只输出修正后的 JSON。")
    assert all(schema == SCHEMA and type(schema) is dict for schema in llm.schemas)


def test_repair_dialogue_does_not_depend_on_retain_repair_prompt():
    """两种 retain 取值产生完全相同的修复对话。"""
    dialogues = []
    for retain in (False, True):
        llm = _QueueLLM('{"n":1}', '{"n":2}')
        outcomes = iter((BAD, OK))
        _run(llm, lambda _candidate: next(outcomes), scope=CallScope(retain_repair_prompt=retain))
        dialogues.append(llm.prompts[1])
    assert dialogues[0] == dialogues[1]


def test_repair_llm_profile_is_used_for_repair_rounds_only():
    llm = _QueueLLM('{"n":1}', '{"n":2}')
    outcomes = iter((BAD, OK))
    _run(llm, lambda _candidate: next(outcomes), cfg=OutputConfig(repair_llm="repair", max_repair_attempts=2))
    assert llm.profiles == ["segment", "repair"]


@pytest.mark.parametrize(("configured", "calls"), [(0, 1), (1, 2), (2, 3), (3, 3), (9, 3)])
def test_l3_rounds_capped_at_two(configured, calls):
    """L3 轮数恒为 min(max_repair_attempts, 2)。"""
    llm = _QueueLLM('{"n":1}')
    seen = []

    def validate(candidate):
        seen.append(candidate)
        return BAD

    with pytest.raises(SchemaViolation) as caught:
        _run(llm, validate, cfg=OutputConfig(max_repair_attempts=configured))
    assert llm.calls == calls
    assert len(seen) == calls
    assert caught.value.errors == [PREFIXED]
    assert caught.value.usage == Usage(5 * calls, 2 * calls)


def test_repair_overflow_raises_schema_violation_with_usage():
    """修复轮 reactive-400 溢出不外抛 context_overflow：恰一次喂熔断，以 SchemaViolation 结束并带累计用量。"""
    overflow = ContextOverflowError("repair overflows", "reactive", "segment", "http_400")
    metrics = _Metrics()
    llm = _QueueLLM('{"n":1}', overflow, '{"n":2}')
    with pytest.raises(SchemaViolation) as caught:
        _run(llm, lambda _candidate: BAD, metrics=metrics)
    assert llm.calls == 2  # 溢出短路剩余修复预算
    assert caught.value.errors == [PREFIXED]
    assert caught.value.raw_last_output == '{"n":1}'
    assert caught.value.usage == Usage(5, 2)
    assert metrics.fed == [True]
    assert overflow._breaker_fed is True


def test_second_repair_overflow_keeps_cumulative_usage_of_completed_rounds():
    overflow = ContextOverflowError("repair overflows", "reactive", "segment", "finish")
    metrics = _Metrics()
    llm = _QueueLLM('{"n":1}', '{"n":2}', overflow)
    with pytest.raises(SchemaViolation) as caught:
        _run(llm, lambda _candidate: BAD, metrics=metrics)
    assert llm.calls == 3
    assert caught.value.raw_last_output == '{"n":2}'
    assert caught.value.usage == Usage(10, 4)
    assert metrics.fed == []  # 200 形态的 finish 判据不喂熔断


def test_first_round_overflow_stays_context_overflow():
    """首轮超限原样上抛，修复轮归因不影响首轮。"""
    overflow = ContextOverflowError("too big", "precheck", "segment")
    llm = _QueueLLM(overflow)
    with pytest.raises(ContextOverflowError) as caught:
        _run(llm, lambda _candidate: OK)
    assert caught.value is overflow
    assert llm.calls == 1


def test_repair_context_byte_limit_ends_in_schema_violation_with_usage():
    raw = '{"n":1}'
    instruction = _build_post_repair_instruction([PREFIXED])
    limit = len(raw.encode("utf-8")) + len(instruction.encode("utf-8")) - 1
    llm = _QueueLLM(raw)
    metrics = _Metrics()
    with pytest.raises(SchemaViolation) as caught:
        _run(llm, lambda _candidate: BAD, scope=CallScope(repair_context_bytes=limit), metrics=metrics)
    assert llm.calls == 1
    assert caught.value.usage == Usage(5, 2)
    assert metrics.fed == []


@pytest.mark.parametrize(("raised", "kind"), [
    (RuntimeError("secret candidate content"), "post_validator_exception"),
    (PostValidatorInvalidError("secret invalid return"), "post_validator_invalid"),
], ids=["exception", "typed_invalid"])
def test_validator_exception_is_terminal(raised, kind, caplog):
    """校验函数异常收敛为无数据终态，不进 L3，也不写 usage。"""
    def validate(_candidate):
        raise raised

    llm = _QueueLLM('{"n":1}')
    with pytest.raises(SchemaViolation) as caught:
        _run(llm, validate)
    assert caught.value.errors == [kind]
    assert caught.value.raw_last_output == ""
    assert caught.value.usage is None
    assert llm.calls == 1
    assert "secret" not in str(caught.value) and "secret" not in caplog.text


def test_validator_exception_during_repair_round_is_terminal():
    outcomes = iter((BAD,))

    def validate(_candidate):
        for outcome in outcomes:
            return outcome
        raise RuntimeError("secret repair detail")

    llm = _QueueLLM('{"n":1}', '{"n":2}', '{"n":3}')
    with pytest.raises(SchemaViolation) as caught:
        _run(llm, validate)
    assert caught.value.errors == ["post_validator_exception"]
    assert caught.value.usage is None
    assert llm.calls == 2


def test_validator_never_sees_l2_failures_and_runs_once_per_candidate():
    """L2 不通过的候选和不可解析的候选都不调用校验函数，L2 违规文本沿用 _render_error。"""
    seen = []

    def validate(candidate):
        seen.append(candidate)
        return PostValidationResult((), candidate)

    llm = _QueueLLM('{"n":"text"}', "not json at all", '{"n":3}')
    result = _run(llm, validate)
    assert seen == [{"n": 3}]
    assert result.resolved_at == "l3_2"
    expected = [_render_error(item) for item in Draft202012Validator(SCHEMA).iter_errors({"n": "text"})]
    assert llm.prompts[1].messages[-1].parts[0].text == (
        f"[违规清单]\n1. {expected[0]}\n\n只输出修正后的 JSON。")


def test_trace_summaries_redact_post_validator_text():
    metrics = _Metrics()
    llm = _QueueLLM('{"n":1}', '{"n":2}')
    outcomes = iter((PostValidationResult(("secret detail",), None), OK))
    result = _run(llm, lambda _candidate: next(outcomes), metrics=metrics)
    assert result.resolved_at == "l3_1"
    assert metrics.payloads == [{"resolved_at": "l3_1", "violations": ["post-validator"]}]


def test_full_trace_records_overlay_without_bytes(tmp_path):
    """AC-122：路径指向不存在的文件，渲染 trace 仍成功，证明这里不读字节、不渲染叠图。"""
    base = ImageRef(tmp_path / "image_1.png", "png", 1)
    overlay = RegionOverlayRef(base=base, regions=RegionSet(width=4, height=4, boxes=(), regions=()))
    prompt = PromptBundle(messages=(Message(role="user", parts=(
        Part(kind="image", image=base), Part(kind="image", image=overlay))),))
    rendered = _render_trace_messages(prompt)
    assert rendered == [{"role": "user", "content": [
        {"type": "image", "path": str(base.path)},
        {"type": "image", "path": str(base.path), "overlay": "regions"},
    ]}]
    assert list(rendered[0]["content"][1]) == ["type", "path", "overlay"]
    assert "base64" not in json.dumps(rendered)
    assert not base.path.exists()


def test_seam_source_carries_no_event_or_geometry_semantics():
    """接缝本身不判定 proof 类型，也不再按需导入 generation 载体。"""
    tree = ast.parse(inspect.getsource(schema_engine_module))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "labelkit.common.contracts.generation" not in imported
    assert "labelkit.common.contracts.regions" not in imported
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not names & {"EventExecution", "RegionSet", "ValidatedGenerationCall"}

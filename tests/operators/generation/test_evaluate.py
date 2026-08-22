"""PatternEvaluator 与 counterfactual coupling 的独立离线测试。"""

from __future__ import annotations

import asyncio
from dataclasses import fields, replace
from types import SimpleNamespace

import pytest

from labelkit.common.config.generation import GenerationLimits
from labelkit.common.contracts.generation import (
    ActorView,
    CouplingEvaluationRequest,
    EventTruth,
    GenerationServices,
    NoiseEvaluationRequest,
    ObservedEvent,
    ScenarioSeed,
    SemanticEvaluationRequest,
)
from labelkit.common.errors import ContextOverflowError, SchemaViolation
from labelkit.operators.generation import GenerationAttemptRejected
from labelkit.operators.generation.evaluate import (
    evaluate_coupling,
    evaluate_noise,
    evaluate_pattern,
    evaluate_semantics,
)
from labelkit.operators.generation.planner import compile_scenario_plan


def _branch(plan, slot_key: str, variant: str):
    """读取一个可见 branch。"""
    return next(block[(slot_key, variant)] for block in plan.blocks
                if (slot_key, variant) in block)


def _observed(pattern, events):
    """只以 frame 与实际 timestamp 构造盲结构输入。"""
    frames = {role.name: role.frame_class for role in pattern.roles}
    return tuple(ObservedEvent(f"event-{index}", frames[event.role], event.timestamp_us)
                 for index, event in enumerate(events))


@pytest.mark.parametrize("variant", (
    "positive",
    "missing_acknowledgement",
    "confirmation_before_acknowledgement",
    "confirmation_timeout",
))
def test_pattern_evaluator_finds_exact_single_target(declared_program, variant):
    plan = compile_scenario_plan(declared_program)
    slot = plan.delivery_slots[0]
    pattern = declared_program.patterns[slot.pattern_name]
    spec = next(item for item in declared_program.counterfactual_sets[0].variants
                if item.name == variant)
    observed = _observed(pattern, _branch(plan, slot.slot_key, variant))
    evaluation = evaluate_pattern(pattern, observed)
    expected = () if not spec.expected_violation else (dict(spec.expected_violation),)
    assert tuple(dict(item) for item in evaluation.actual_violations) == expected
    assert set(evaluation.actual_bindings) == {event.event_id for event in observed}


def _observed_word(pattern, timestamps):
    """按 pattern role word 构造不依赖 planner 的观察序列。"""
    roles = {role.name: role for role in pattern.roles}
    return tuple(
        ObservedEvent(f"manual-{index}", roles[role].frame_class, timestamp)
        for index, (role, timestamp) in enumerate(zip(pattern.order, timestamps))
    )


def test_pattern_evaluator_detects_extra_role_without_planner(declared_program):
    """同 frame 额外事件必须产生唯一 extra_role。"""
    pattern = declared_program.patterns["booking_success"]
    events = list(_observed_word(pattern, (0, 5_000_000, 35_000_000)))
    request_frame = next(role.frame_class for role in pattern.roles if role.name == "request")
    events.append(ObservedEvent("manual-extra", request_frame, 36_000_000))
    result = evaluate_pattern(pattern, tuple(events))
    assert tuple(map(dict, result.actual_violations)) == (
        {"kind": "extra_role", "target": request_frame},
    )


def test_pattern_evaluator_detects_gap_below_min_without_planner(declared_program):
    """非目标短间隔必须独立命中 gap_below_min。"""
    pattern = declared_program.patterns["booking_success"]
    events = _observed_word(pattern, (0, 4_999_999, 34_999_999))
    result = evaluate_pattern(pattern, events)
    assert tuple(map(dict, result.actual_violations)) == (
        {"kind": "gap_below_min", "target": "request_to_acknowledge"},
    )


def test_pattern_evaluator_detects_gap_and_span_in_dependency_order(declared_program):
    """完整 role word 超长时同时保留先 gap 后 span 的违规序。"""
    pattern = declared_program.patterns["booking_success"]
    events = _observed_word(pattern, (0, 5_000_000, pattern.max_span_us + 1))
    result = evaluate_pattern(pattern, events)
    assert tuple(map(dict, result.actual_violations)) == (
        {"kind": "gap_above_max", "target": "acknowledge_to_confirm"},
        {"kind": "max_span_exceeded", "target": pattern.name},
    )


def _truth(role: str, index: int, payload=None) -> EventTruth:
    """构造 coupling 只读字段完整的 EventTruth。"""
    view = ActorView("actor", {}, {}, (), index, index)
    return EventTruth(
        f"key-{role}", f"id-{index}", role, f"frame-{role}", "actor",
        index, 100 + index, view, f"intent-{role}",
        ({"op": "test", "path": "/x", "value": index},),
        f"before-{index}", f"after-{index}", {"/x": index},
        payload or {"value": index},
    )


@pytest.mark.parametrize("field", (
    "event_key",
    "role",
    "frame_class",
    "actor",
    "logical_time_us",
    "actor_view",
    "intent",
    "patch",
    "state_before_hash",
    "state_after_hash",
    "publish_snapshot",
    "payload",
))
def test_coupling_ignores_branch_ids_but_protects_every_prefix_field(
    declared_program,
    field,
):
    """除 branch id/time 外的十二个 protected 字段均独立强制。"""
    variant = next(item for item in declared_program.counterfactual_sets[0].variants
                   if item.name == "confirmation_before_acknowledgement")
    baseline = tuple(_truth(role, index) for index, role in enumerate(
        ("request", "acknowledge", "confirm")
    ))
    branch = tuple(replace(event, event_id=f"branch-{index}", timestamp_us=900 + index)
                   for index, event in enumerate(baseline))
    assert evaluate_coupling(CouplingEvaluationRequest(variant, baseline, branch))
    changes = {
        "event_key": "changed-key",
        "role": "changed-role",
        "frame_class": "changed-frame",
        "actor": "other",
        "logical_time_us": 99,
        "actor_view": replace(branch[0].actor_view, wait_since_previous_us=99),
        "intent": "changed",
        "patch": ({"op": "test", "path": "/other", "value": 0},),
        "state_before_hash": "changed-before",
        "state_after_hash": "changed-after",
        "publish_snapshot": {"/changed": True},
        "payload": {"value": "changed"},
    }
    tampered = (replace(branch[0], **{field: changes[field]}), *branch[1:])
    assert not evaluate_coupling(CouplingEvaluationRequest(variant, baseline, tampered))


class _Metrics:
    """记录 semantic family 逻辑调用数。"""

    def __init__(self):
        self.counters = {}

    def count(self, key: str, n: int = 1):
        self.counters[key] = self.counters.get(key, 0) + n


class _SemanticEngine:
    """返回一个指定独立语义判定并保留 prompt。"""

    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.prompt = None
        self.calls = 0

    async def complete_validated(self, _profile, prompt, *, schema, scope):
        del schema, scope
        self.calls += 1
        self.prompt = prompt
        if self.error is not None:
            raise self.error
        return (self.value,)


class _InlineTaskExecutor:
    """按声明序执行冻结叶任务并返回输入序结果。"""

    async def run_group(self, request):
        return tuple([await task.operation() for task in request.tasks])


def _semantic_request():
    """构造不携带任何结构目标的最小盲审请求。"""
    seed = ScenarioSeed({}, {"actor": {"goal": {}}}, {"public": {}, "hidden": {}}, {}, {})
    return SemanticEvaluationRequest(
        "judge", "declared", "ticket", "class description", "pattern description",
        seed, (), {}, 0, GenerationLimits(),
    )


def _semantic_services(value=None, error=None):
    """构造 semantic evaluator 离线服务。"""
    engine, metrics = _SemanticEngine(value, error), _Metrics()
    config = SimpleNamespace(
        sequence_generation=SimpleNamespace(limits=GenerationLimits())
    )
    services = GenerationServices(
        config,
        engine,
        object(),
        metrics,
        _InlineTaskExecutor(),
    )
    return services, engine, metrics


def test_semantic_request_and_prompt_are_blind_to_structural_truth():
    request = _semantic_request()
    names = {field.name for field in fields(request)}
    forbidden = {
        "trace", "variant", "target", "expected", "actual",
        "pattern_evaluation", "state_evaluation",
    }
    assert names.isdisjoint(forbidden)
    value = {
        "causal_consistency": True, "actor_knowledge": True,
        "goal_consistency": True, "temporal_plausibility": True,
        "cross_frame_consistency": True, "realism": True, "reason_codes": [],
    }
    services, engine, metrics = _semantic_services(value)
    result = asyncio.run(evaluate_semantics(request, services))
    assert result.causal_consistency and result.reason_codes == ()
    user = "".join(part.text or "" for part in engine.prompt.messages[1].parts)
    system = "".join(part.text or "" for part in engine.prompt.messages[0].parts)
    assert all(token not in user for token in (
        "EventTrace", "PatternEvaluation", "StateEvaluation", "expected_violation",
        "actual_violations", "variant=", "target=",
    ))
    assert "反例优先审查" in system
    assert "后续消息引用已有终态又用近义短语重述它" in system
    assert "语法主语直接是请求、消息或业务实体" in system
    assert "以过程作主语，不属于该缺陷" in system
    assert "发件者把自己正在发出的消息当成收到的对象" in system
    assert "不得用未提供的隐藏理由替候选补故事" in system
    assert "缺步骤、顺序异常或长等待本身不自动失败" in system
    assert metrics.counters == {"generate.sequence.calls.semantic_evaluation_calls": 1}


def test_semantic_evaluator_uses_program_bound_request_limits():
    """冲突的服务配置上限不能改变已编译请求的 prompt gate。"""
    value = {
        "causal_consistency": True, "actor_knowledge": True,
        "goal_consistency": True, "temporal_plausibility": True,
        "cross_frame_consistency": True, "realism": True, "reason_codes": [],
    }
    services, engine, _metrics = _semantic_services(value)
    services.config.sequence_generation.limits = replace(
        services.config.sequence_generation.limits, prompt_value_bytes=1
    )
    result = asyncio.run(evaluate_semantics(_semantic_request(), services))
    assert result.realism and engine.calls == 1


_SEMANTIC_FIELDS = (
    "causal_consistency",
    "actor_knowledge",
    "goal_consistency",
    "temporal_plausibility",
    "cross_frame_consistency",
    "realism",
)
_SEMANTIC_CODES = (
    "causal_inconsistency",
    "actor_knowledge_violation",
    "goal_inconsistency",
    "temporal_implausibility",
    "cross_frame_inconsistency",
    "unrealistic",
)


@pytest.mark.parametrize("field", _SEMANTIC_FIELDS)
def test_each_semantic_boolean_is_an_independent_rejection_gate(field):
    """六项任一 false 即使漏报 reason code 也必须拒绝。"""
    value = {name: True for name in _SEMANTIC_FIELDS}
    value.update({field: False, "reason_codes": []})
    services, _engine, _metrics = _semantic_services(value)
    with pytest.raises(GenerationAttemptRejected) as caught:
        asyncio.run(evaluate_semantics(_semantic_request(), services))
    assert caught.value.kind == "semantic_evaluation"


@pytest.mark.parametrize("reason_codes", (
    ("causal_inconsistency",),
    tuple(reversed(_SEMANTIC_CODES)),
    (*_SEMANTIC_CODES, "causal_inconsistency"),
))
def test_semantic_reason_codes_cannot_exist_when_all_booleans_pass(reason_codes):
    """全 true 与任何错误、乱序或多余 reason code 不一致。"""
    value = {name: True for name in _SEMANTIC_FIELDS}
    value["reason_codes"] = list(reason_codes)
    services, _engine, _metrics = _semantic_services(value)
    with pytest.raises(GenerationAttemptRejected) as caught:
        asyncio.run(evaluate_semantics(_semantic_request(), services))
    assert caught.value.kind == "semantic_evaluation"


@pytest.mark.parametrize("field, code", tuple(zip(_SEMANTIC_FIELDS, _SEMANTIC_CODES)))
def test_semantic_matching_false_reason_code_still_rejects_candidate(field, code):
    """任一 false 与对应 code 精确对齐仍不能被当成通过。"""
    value = {name: True for name in _SEMANTIC_FIELDS}
    value.update({field: False, "reason_codes": [code]})
    services, _engine, _metrics = _semantic_services(value)
    with pytest.raises(GenerationAttemptRejected):
        asyncio.run(evaluate_semantics(_semantic_request(), services))


def test_semantic_schema_exhaustion_rejects_current_attempt():
    services, _engine, _metrics = _semantic_services(
        error=SchemaViolation(["/causal_consistency: required"], "redacted")
    )
    with pytest.raises(GenerationAttemptRejected) as caught:
        asyncio.run(evaluate_semantics(_semantic_request(), services))
    assert caught.value.kind == "semantic_evaluation"


def test_semantic_prompt_value_limit_rejects_before_provider():
    """完整 review/final-state 动态值 D+1 在独立判定派发前终止。"""
    services, engine, _metrics = _semantic_services({})
    limit = services.config.sequence_generation.limits.prompt_value_bytes
    request = replace(_semantic_request(), final_state={"value": "x" * (limit + 1)})
    with pytest.raises(ContextOverflowError):
        asyncio.run(evaluate_semantics(request, services))
    assert engine.calls == 0


def _noise_request() -> NoiseEvaluationRequest:
    """构造不携带 primary 真值的 noise 盲审请求。"""
    return NoiseEvaluationRequest(
        "judge",
        {"utterance": "今天的云很好看"},
        "夜空中的月相观察",
        {"ticket": "booking"},
        {"noise": "small talk"},
        0,
        GenerationLimits(),
    )


@pytest.mark.parametrize("field", (
    "unrelated_to_declared_tasks",
    "no_executable_task",
    "realism",
    "matches_planned_topic",
))
def test_each_noise_boolean_is_an_independent_rejection_gate(field):
    """noise 四项任一 false 且缺 reason code 仍必须拒绝。"""
    value = {
        "unrelated_to_declared_tasks": True,
        "no_executable_task": True,
        "realism": True,
        "matches_planned_topic": True,
        "reason_codes": [],
    }
    value[field] = False
    services, _engine, _metrics = _semantic_services(value)
    with pytest.raises(GenerationAttemptRejected) as caught:
        asyncio.run(evaluate_noise(_noise_request(), services))
    assert caught.value.kind == "noise_semantic"


@pytest.mark.parametrize("field, code", (
    ("unrelated_to_declared_tasks", "related_to_declared_task"),
    ("no_executable_task", "executable_task_present"),
    ("realism", "unrealistic"),
    ("matches_planned_topic", "planned_noise_topic_mismatch"),
))
def test_noise_matching_false_reason_code_still_rejects_candidate(field, code):
    """noise 任一 false 与对应 code 精确对齐仍不能通过。"""
    value = {
        "unrelated_to_declared_tasks": True,
        "no_executable_task": True,
        "realism": True,
        "matches_planned_topic": True,
        "reason_codes": [code],
    }
    value[field] = False
    services, _engine, _metrics = _semantic_services(value)
    with pytest.raises(GenerationAttemptRejected):
        asyncio.run(evaluate_noise(_noise_request(), services))


@pytest.mark.parametrize("reason_codes", (
    ("related_to_declared_task",),
    ("unrealistic", "executable_task_present"),
    ("unrealistic", "unrealistic"),
    ("planned_noise_topic_mismatch",),
))
def test_noise_reason_codes_cannot_exist_when_all_booleans_pass(reason_codes):
    """noise 全 true 不得带错误、乱序或重复 reason code。"""
    value = {
        "unrelated_to_declared_tasks": True,
        "no_executable_task": True,
        "realism": True,
        "matches_planned_topic": True,
        "reason_codes": list(reason_codes),
    }
    services, _engine, _metrics = _semantic_services(value)
    with pytest.raises(GenerationAttemptRejected):
        asyncio.run(evaluate_noise(_noise_request(), services))


def test_noise_schema_exhaustion_rejects_current_attempt():
    """noise 内部 Schema 穷尽归当前 noise attempt 拒绝。"""
    services, _engine, _metrics = _semantic_services(
        error=SchemaViolation(["/realism: required"], "redacted")
    )
    with pytest.raises(GenerationAttemptRejected) as caught:
        asyncio.run(evaluate_noise(_noise_request(), services))
    assert caught.value.kind == "noise_semantic"

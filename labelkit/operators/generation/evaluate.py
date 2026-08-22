"""v1.18 pattern、state、coupling 与独立语义 oracle。"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from typing import Mapping, Sequence

import jsonpatch
from jsonpointer import JsonPointerException, resolve_pointer
from jsonschema import Draft202012Validator

from labelkit.common.config.generation import SequencePattern
from labelkit.common.contracts.generation import (
    CouplingEvaluationRequest,
    GenerationServices,
    NoiseEvaluationRequest,
    NoiseSemanticEvaluation,
    ObservedEvent,
    PatternEvaluation,
    SemanticEvaluation,
    SemanticEvaluationRequest,
    StateEvaluation,
    StateEvaluationRequest,
    StateTransitionInput,
)
from labelkit.common.errors import SchemaViolation
from labelkit.common.extensions.hooks import clone_state_input, normalize_state_violations
from labelkit.common.runtime.generation_prompts import (
    enforce_prompt_value_limit,
    noise_evaluation_prompt,
    semantic_evaluation_prompt,
)
from labelkit.common.runtime.schema_engine import (
    CallScope,
    noise_semantic_evaluation_schema,
    semantic_evaluation_schema,
)
from labelkit.operators.generation import GenerationAttemptRejected
from labelkit.operators.generation.project import canonical_json
from labelkit.operators.generation.state import binding_values


_log = logging.getLogger("labelkit.generation.evaluate")
def evaluate_pattern(
    pattern: SequencePattern,
    events: Sequence[ObservedEvent],
) -> PatternEvaluation:
    """不读取 planner witness 地绑定实际角色与违规。

    @param pattern 待独立判定的序列模式。
    @param events 按发生顺序排列的观察事件。
    @return 实际角色绑定与实际违规闭集。
    """
    roles_by_frame = defaultdict(list)
    role_specs = {item.name: item for item in pattern.roles}
    for role_name in pattern.order:
        roles_by_frame[role_specs[role_name].frame_class].append(role_name)
    events_by_frame = defaultdict(list)
    for event in events:
        events_by_frame[event.frame_class].append(event)
    bindings, violations = _bind_cardinality(roles_by_frame, events_by_frame)
    event_index = {event.event_id: event for event in events}
    role_events = {role: event_index[event_id] for event_id, role in bindings.items()}
    violations.extend(_order_violations(pattern, role_events))
    violations.extend(_gap_violations(pattern, role_events, violations))
    violations.extend(_span_violations(pattern, role_events))
    return PatternEvaluation(bindings, tuple(violations))


def evaluate_state(request: StateEvaluationRequest) -> StateEvaluation:
    """独立重放全部 patch，并校验状态、binding 与受保护前缀。

    @param request 状态判定请求。
    @return 独立状态判定结果。
    """
    state = _json_copy(request.scenario_seed.initial_state)
    bindings_valid = True
    replay_valid = True
    for event in request.events:
        state, step_valid, binding_valid = _replay_event(request, event, state)
        replay_valid = replay_valid and step_valid
        bindings_valid = bindings_valid and binding_valid
    final_hash = _state_hash(request.final_state)
    replay_hash = _state_hash(state)
    replay_valid = replay_valid and canonical_json(state) == canonical_json(request.final_state)
    outcome_valid = replay_valid and _outcome_valid(request, state)
    protected = _protected_prefix_valid(request.variant, request.baseline_events, request.events)
    return StateEvaluation(replay_hash, final_hash, bindings_valid, outcome_valid, protected)


def evaluate_coupling(request: CouplingEvaluationRequest) -> bool:
    """逐字节比较变体与基线的全部受保护前缀字段。

    @param request 基线与变体耦合判定请求。
    @return 全部受保护字段一致时为 true。
    """
    return _protected_prefix_valid(request.variant, request.baseline_events, request.events)


async def evaluate_semantics(
    request: SemanticEvaluationRequest,
    services: GenerationServices,
) -> SemanticEvaluation:
    """用 evaluation profile 判定完整且未裁剪的盲审语义输入。

    @param request 不含结构目标与既有判定的语义审查请求。
    @param services 生成服务根。
    @return 六项布尔语义判定与闭集 reason code。
    """
    services.metrics.count("generate.sequence.calls.semantic_evaluation_calls")
    try:
        result = await services.schema_engine.complete_validated(
            request.evaluation_profile,
            _semantic_prompt(request, request.limits),
            schema=semantic_evaluation_schema(),
            scope=CallScope(
                repair_context_bytes=(
                    request.limits.repair_context_bytes
                ),
            ),
        )
    except SchemaViolation:
        raise GenerationAttemptRejected("semantic_evaluation", request.sequence_class) from None
    value = SemanticEvaluation(**result[0])
    if not _semantic_consistent(value):
        raise GenerationAttemptRejected("semantic_evaluation", request.sequence_class)
    return value


async def evaluate_noise(
    request: NoiseEvaluationRequest,
    services: GenerationServices,
) -> NoiseSemanticEvaluation:
    """独立于全部 primary 内容判定一个 noise payload。

    @param request noise 语义判定请求。
    @param services 生成服务根。
    @return 四项布尔判定与闭集 reason code。
    """
    services.metrics.count("generate.sequence.calls.noise_evaluation_calls")
    slot_key = f"noise/{request.attempt_index:06d}"
    try:
        result = await services.schema_engine.complete_validated(
            request.evaluation_profile,
            _noise_prompt(request, request.limits),
            schema=noise_semantic_evaluation_schema(),
            scope=CallScope(
                repair_context_bytes=(
                    request.limits.repair_context_bytes
                ),
            ),
        )
    except SchemaViolation:
        raise GenerationAttemptRejected("noise_semantic", slot_key) from None
    value = NoiseSemanticEvaluation(**result[0])
    if not _noise_consistent(value):
        raise GenerationAttemptRejected("noise_semantic", slot_key)
    return value


def _bind_cardinality(roles_by_frame, events_by_frame):
    """按 frame 组的第 k 次出现绑定 event_id 到 role。"""
    bindings: dict[str, str] = {}
    violations: list[dict[str, str]] = []
    frames = tuple(dict.fromkeys((*roles_by_frame.keys(), *events_by_frame.keys())))
    for frame in frames:
        roles, events = roles_by_frame[frame], events_by_frame[frame]
        for role, event in zip(roles, events):
            bindings[event.event_id] = role
        for role in roles[len(events):]:
            violations.append({"kind": "missing_role", "target": role})
        for _event in events[len(roles):]:
            violations.append({"kind": "extra_role", "target": frame})
    return bindings, violations


def _order_violations(pattern, role_events):
    """按 pattern 相邻边声明序检查实际顺序。"""
    violations = []
    for before, after in zip(pattern.order, pattern.order[1:]):
        if before not in role_events or after not in role_events:
            continue
        if role_events[before].timestamp_us > role_events[after].timestamp_us:
            violations.append({"kind": "reordered", "before": before, "after": after})
    return violations


def _gap_violations(pattern, role_events, prior):
    """检查所有可用 gap，并抑制缺 role/逆序的依赖边。"""
    reordered = {(item.get("before"), item.get("after"))
                 for item in prior if item.get("kind") == "reordered"}
    violations = []
    for gap in pattern.gaps:
        if gap.before not in role_events or gap.after not in role_events:
            continue
        if (gap.before, gap.after) in reordered:
            continue
        delta = role_events[gap.after].timestamp_us - role_events[gap.before].timestamp_us
        if delta < gap.min_gap_us:
            violations.append({"kind": "gap_below_min", "target": gap.name})
        elif delta > gap.max_gap_us:
            violations.append({"kind": "gap_above_max", "target": gap.name})
    return violations


def _span_violations(pattern, role_events):
    """在全部 role 可绑定时检查 max span。"""
    if any(role not in role_events for role in pattern.order):
        return []
    times = [role_events[role].timestamp_us for role in pattern.order]
    if max(times) - min(times) <= pattern.max_span_us:
        return []
    return [{"kind": "max_span_exceeded", "target": pattern.name}]


def _replay_event(request, event, state):
    """不使用 StateExecutor cache 重放一个 EventTruth。"""
    role = _event_role(request, event.role)
    before = _json_copy(state)
    try:
        _validate_event_patch(event.patch, role)
        if role is not None and role.pre_state_schema is not None:
            _require_schema(before, role.pre_state_schema)
        after = jsonpatch.apply_patch(before, _json_copy(event.patch), in_place=False)
        _require_schema(after, _state_schema(request))
        _run_hook(request, role, before, after, event.patch)
        hashes = (_state_hash(before) == event.state_before_hash
                  and _state_hash(after) == event.state_after_hash)
        bindings = _bindings_match(role, before, after, event.payload)
        return after, hashes, bindings
    except Exception as exc:
        if isinstance(exc, RuntimeError) and str(exc) == "state evaluator hook failed":
            raise
        return before, False, False


def _event_role(request, role_name: str):
    """从 StateEvaluationRequest 唯一解析 declared role。"""
    if request.pattern is None:
        return None
    role = next((item for item in request.pattern.roles if item.name == role_name), None)
    if role is None:
        raise ValueError
    return role


def _validate_event_patch(patch, role) -> None:
    """独立检查 patch 闭集、test 前缀与权限。"""
    mutation, tests = False, 0
    for operation in patch:
        op, path = operation.get("op"), operation.get("path")
        if op not in {"test", "add", "remove", "replace"} or not isinstance(path, str):
            raise ValueError
        if op == "test":
            if mutation:
                raise ValueError
            tests += 1
        else:
            mutation = True
        if role is not None and not _covered(path, role.read_roots if op == "test" else role.write_roots):
            raise ValueError
    if tests == 0 or not mutation:
        raise ValueError


def _run_hook(request, role, before, after, patch) -> None:
    """以独立副本执行 program 中的 state validator。"""
    hook = request.program.state_validator
    if hook is None:
        return
    value = StateTransitionInput(
        request.slot.slot_key,
        None if request.variant is None else request.variant.name,
        None if role is None else role.name,
        before,
        after,
        tuple(patch),
    )
    try:
        result = hook.target(clone_state_input(value))
    except Exception:
        _log.error("state evaluator hook raised an exception")
        raise RuntimeError("state evaluator hook failed") from None
    try:
        violations = normalize_state_violations(result, hook.reference)
    except Exception:
        _log.error("state evaluator hook returned an invalid value")
        raise RuntimeError("state evaluator hook failed") from None
    if violations:
        raise ValueError


def _bindings_match(role, before, after, payload) -> bool:
    """独立比较 payload binding 与状态快照。"""
    if role is None:
        return True
    for item in role.payload_bindings:
        state = before if item.state_phase == "before" else after
        try:
            expected = resolve_pointer(state, item.state_path)
            actual = resolve_pointer(payload, item.payload_path)
        except (JsonPointerException, KeyError, TypeError):
            return False
        if canonical_json(expected) != canonical_json(actual):
            return False
    return True


def _outcome_valid(request, state) -> bool:
    """检查 variant outcome；instruction-only 只用基础 Schema。"""
    schema = _state_schema(request) if request.variant is None else request.variant.outcome_schema
    try:
        return next(Draft202012Validator(_json_copy(schema)).iter_errors(state), None) is None
    except Exception:
        return False


def _state_schema(request):
    """从 program/slot 选择 StateEvaluator 基础 Schema。"""
    if request.program.mode == "instruction_only":
        source = next(item for item in request.program.instruction_only
                      if item.name == request.slot.source_name)
        return source.state_schema
    config = request.program.class_views[request.slot.sequence_class].sequence_generation
    return config.state_schema


def _protected_prefix_valid(variant, baseline, events) -> bool:
    """比较 divergence role 之前的全部受保护语义字段。"""
    if variant is None or variant.divergence_role is None:
        count = min(len(baseline), len(events))
    else:
        count = next((index for index, event in enumerate(baseline)
                      if event.role == variant.divergence_role), 0)
    if len(events) < count or len(baseline) < count:
        return False
    return all(_protected_bytes(left) == _protected_bytes(right)
               for left, right in zip(baseline[:count], events[:count]))


def _protected_bytes(event) -> str:
    """投影 protected prefix 要求字节相同的字段。"""
    return canonical_json({
        "event_key": event.event_key,
        "role": event.role,
        "frame_class": event.frame_class,
        "actor": event.actor,
        "logical_time_us": event.logical_time_us,
        "actor_view": event.actor_view,
        "intent": event.intent,
        "patch": event.patch,
        "state_before_hash": event.state_before_hash,
        "state_after_hash": event.state_after_hash,
        "publish_snapshot": event.publish_snapshot,
        "payload": event.payload,
    })


def _semantic_prompt(request: SemanticEvaluationRequest, limits):
    """构造不含结构目标或既有 verdict 的盲审 prompt。"""
    enforce_prompt_value_limit(
        request.evaluation_profile,
        limits.scenario_seed_bytes,
        {"scenario_seed": request.scenario_seed},
    )
    enforce_prompt_value_limit(
        request.evaluation_profile,
        limits.prompt_value_bytes,
        {"review_events": request.review_events, "final_state": request.final_state},
    )
    return semantic_evaluation_prompt({
        "mode": request.mode, "sequence_class": request.sequence_class,
        "attempt_index": request.attempt_index,
        "class_description": request.class_description,
        "pattern_description": request.pattern_description,
        "scenario_seed": request.scenario_seed,
        "review_events": request.review_events, "final_state": request.final_state,
    })


def _noise_prompt(request: NoiseEvaluationRequest, limits):
    """构造独立 noise 语义判定 prompt。"""
    enforce_prompt_value_limit(
        request.evaluation_profile,
        limits.rendered_payload_bytes,
        {"payload": request.payload},
    )
    return noise_evaluation_prompt({
        "attempt_index": request.attempt_index,
        "class_descriptions": request.class_descriptions,
        "frame_descriptions": request.frame_descriptions,
        "planned_topic": request.planned_topic,
        "payload": request.payload,
    })


def _semantic_consistent(value: SemanticEvaluation) -> bool:
    """校验六项 boolean 与 reason code 的全等价关系。"""
    pairs = (
        (value.causal_consistency, "causal_inconsistency"),
        (value.actor_knowledge, "actor_knowledge_violation"),
        (value.goal_consistency, "goal_inconsistency"),
        (value.temporal_plausibility, "temporal_implausibility"),
        (value.cross_frame_consistency, "cross_frame_inconsistency"),
        (value.realism, "unrealistic"),
    )
    expected = tuple(code for passed, code in pairs if not passed)
    return tuple(value.reason_codes) == expected and not expected


def _noise_consistent(value: NoiseSemanticEvaluation) -> bool:
    """校验四项 noise boolean 与 reason code 的全等价关系。"""
    pairs = (
        (value.unrelated_to_declared_tasks, "related_to_declared_task"),
        (value.no_executable_task, "executable_task_present"),
        (value.realism, "unrealistic"),
        (value.matches_planned_topic, "planned_noise_topic_mismatch"),
    )
    expected = tuple(code for passed, code in pairs if not passed)
    return tuple(value.reason_codes) == expected and not expected


def _covered(path: str, roots) -> bool:
    """以 RFC 6901 token 而非字符串前缀判断权限。"""
    from jsonpointer import JsonPointer

    try:
        parts = tuple(JsonPointer(path).parts)
        return any(_prefix(tuple(JsonPointer(root).parts), parts) for root in roots)
    except JsonPointerException:
        return False


def _prefix(left, right) -> bool:
    """判断解码 token 的祖先或自身关系。"""
    return len(left) <= len(right) and left == right[:len(left)]


def _require_schema(value, schema) -> None:
    """要求完整对象通过 Draft 2020-12 Schema。"""
    if next(Draft202012Validator(_json_copy(schema)).iter_errors(value), None) is not None:
        raise ValueError


def _state_hash(value) -> str:
    """计算完整状态 canonical SHA-256。"""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _json_copy(value):
    """复制冻结或可变 JSON 值。"""
    return json.loads(canonical_json(value))

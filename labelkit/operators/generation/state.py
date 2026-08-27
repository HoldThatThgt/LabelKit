"""v1.20 事件状态执行、ActorView 投影与机械 binding。"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Mapping

import jsonpatch
from jsonpointer import JsonPointer, JsonPointerException, resolve_pointer
from jsonschema import Draft202012Validator

from labelkit.common.config.generation import RoleSpec, is_generation_frame_eligible
from labelkit.common.contracts.generation import (
    ActorView,
    EventDraft,
    EventExecution,
    EventExecutionContext,
    EventPlan,
    PostValidationResult,
    StateTransitionInput,
)
from labelkit.common.errors import InternalError, PostValidatorInvalidError
from labelkit.common.extensions.hooks import clone_state_input, normalize_state_violations
from labelkit.operators.generation.project import canonical_json, validate_planned_events


_log = logging.getLogger("labelkit.generation.state")
_OPS = frozenset({"test", "add", "remove", "replace"})
_DYNAMIC_SCHEMA_PATHS = frozenset({
    "additionalProperties", "patternProperties", "propertyNames",
    "unevaluatedProperties", "items", "prefixItems", "contains", "unevaluatedItems",
})


class _StateViolation(Exception):
    """一条可修复的状态转换违规。"""

    def __init__(self, violations: str | tuple[str, ...]):
        """保留机械违规或 state hook 返回的逐项文本。

        @param violations 单条机械 kind 或已规范化 hook 违规。
        """
        self.violations = (violations,) if isinstance(violations, str) else violations
        super().__init__("; ".join(self.violations))


@dataclass(frozen=True)
class _Transition:
    """一次状态执行的内部单根参数。"""

    context: EventExecutionContext  # 完整事件执行上下文。
    role: RoleSpec | None  # declared role；instruction-only 为 None。
    event_plan: EventPlan  # 待原子执行计划。
    state_schema: Mapping[str, object]  # 当前模式的基础状态 Schema。


def resolve_planned_events(context: EventExecutionContext):
    """校验唯一执行根并返回当前 branch 事件。

    @param context 唯一事件执行上下文。
    @return 当前 block key 的 PlannedEvent tuple。
    """
    if context.slot not in context.plan.delivery_slots:
        _contract_error("slot does not belong to the scenario plan")
    key = (context.slot.slot_key, context.variant_name)
    matches = [block[key] for block in context.plan.blocks if key in block]
    if len(matches) != 1:
        _contract_error("scenario block key is missing or duplicated")
    events = matches[0]
    validate_planned_events(context.program, context.slot, context.variant_name, events)
    if not 0 <= context.event_index < len(events):
        _contract_error("event index is outside the frozen scenario block")
    if len(context.history) != context.event_index:
        _contract_error("event history length does not match event index")
    return events


def resolve_role(context: EventExecutionContext) -> RoleSpec | None:
    """从 program/slot/plan 唯一解析当前 declared role。

    @param context 唯一事件执行上下文。
    @return declared RoleSpec；instruction-only 为 None。
    """
    events = resolve_planned_events(context)
    if context.program.mode == "instruction_only":
        if context.variant_name is not None or context.slot.pattern_name is not None:
            _contract_error("instruction-only context carries declared identity")
        return None
    pattern = context.program.patterns.get(context.slot.pattern_name)
    if pattern is None:
        _contract_error("declared context cannot resolve its pattern")
    planned = events[context.event_index]
    role = next((item for item in pattern.roles if item.name == planned.role), None)
    if role is None:
        _contract_error("planned role is absent from the declared pattern")
    return role


def state_schema_for(context: EventExecutionContext) -> Mapping[str, object]:
    """从执行根唯一选择基础状态 Schema。

    @param context 唯一事件执行上下文。
    @return 完整 Draft 2020-12 Schema。
    """
    if context.program.mode == "instruction_only":
        source = next(
            (item for item in context.program.instruction_only
             if item.name == context.slot.source_name),
            None,
        )
        if source is None:
            _contract_error("instruction-only slot source is absent from the program")
        return source.state_schema
    class_view = context.program.class_views.get(context.slot.sequence_class)
    config = None if class_view is None else class_view.sequence_generation
    if config is None:
        _contract_error("declared slot has no sequence generation state schema")
    return config.state_schema


def build_actor_view(context: EventExecutionContext, actor: str | None = None) -> ActorView:
    """从当前 state/history 机械构造 ActorView。

    @param context 唯一事件执行上下文。
    @param actor instruction-only 已选 actor；declared 不传。
    @return 当前事件前的 actor 知识视图。
    """
    events = resolve_planned_events(context)
    planned = events[context.event_index]
    role = resolve_role(context)
    selected = role.actor if role is not None else actor
    if selected not in context.scenario_seed.actors:
        _contract_error("selected actor is absent from ScenarioSeed")
    wait = _wait_since_previous(context, planned.logical_time_us)
    if role is None:
        read_state = _json_copy(context.current_state)
        observations = tuple(project_instruction_draft(item) for item in context.history)
    else:
        read_state = _project_paths(context.current_state, role.read_roots)
        observations = _declared_observations(context, selected)
    goal = context.scenario_seed.actors[selected]["goal"]
    return ActorView(selected, goal, read_state, observations, planned.logical_time_us, wait)


def execute_event(context: EventExecutionContext, event_plan: EventPlan) -> EventExecution:
    """在深拷贝状态上原子执行并校验一个事件。

    @param context 唯一事件执行上下文。
    @param event_plan 待执行的事件计划。
    @return 规范化 patch 及执行前后证明。
    """
    role = resolve_role(context)
    transition = _Transition(context, role, event_plan, state_schema_for(context))
    return _execute_transition(transition)


def post_validate_event_plan(
    candidate: Mapping[str, object],
    context: EventExecutionContext,
) -> PostValidationResult:
    """恰好一次后置校验一个 L2 候选并保留执行证明。

    @param candidate 已通过 L2 的候选对象。
    @param context 唯一事件执行上下文。
    @return 可修复违规或唯一成功执行证明。
    """
    try:
        event_plan = _event_plan(candidate)
        execution = execute_event(context, event_plan)
    except _StateViolation as exc:
        return PostValidationResult(exc.violations, None)
    return PostValidationResult((), execution)


def binding_values(role: RoleSpec | None, execution: EventExecution) -> dict[str, object]:
    """按 RoleSpec 声明序读取权威 payload binding 值。

    @param role declared role；instruction-only 为 None。
    @param execution 已执行状态证明。
    @return payload path 到深拷贝值的有序字典。
    """
    if role is None:
        return {}
    values: dict[str, object] = {}
    for item in role.payload_bindings:
        state = execution.state_before if item.state_phase == "before" else execution.state_after
        try:
            values[item.payload_path] = _json_copy(resolve_pointer(state, item.state_path))
        except (JsonPointerException, KeyError, TypeError):
            raise _StateViolation("payload_binding") from None
    return values


def _execute_transition(transition: _Transition) -> EventExecution:
    """执行一次已解析的状态转换。"""
    context, role, plan = transition.context, transition.role, transition.event_plan
    _validate_plan_identity(context, role, plan)
    patch = _normalize_patch(plan.patch, context.program.limits.event_patch_bytes)
    _validate_patch(patch, role)
    before = _json_copy(context.current_state)
    if role is not None and role.pre_state_schema is not None:
        _validate_schema(before, role.pre_state_schema, "pre_state_schema")
    after = _apply_patch(before, patch)
    _validate_schema(after, transition.state_schema, "state_schema")
    _run_state_hook(context, role, before, after, patch)
    outcome_schema = outcome_schema_for(context)
    if outcome_schema is not None:
        _validate_schema(after, outcome_schema, "outcome_schema")
    _validate_bindings(role, before, after)
    publish = {} if role is None else _project_paths(after, role.publish_roots)
    return EventExecution(
        before, after, _state_hash(before), _state_hash(after), publish, patch
    )


def _validate_plan_identity(context, role, plan) -> None:
    """校验 EventPlan 的 frame/actor 闭集。"""
    if role is not None and (plan.frame_class != role.frame_class or plan.actor != role.actor):
        raise _StateViolation("event_identity")
    frame = context.program.frame_classes.get(plan.frame_class)
    noise = context.program.noise
    invalid_frame = (
        frame is None
        or not is_generation_frame_eligible(frame)
        or (noise is not None and plan.frame_class == noise.frame_class)
    )
    if role is None and (invalid_frame or plan.actor not in context.scenario_seed.actors):
        raise _StateViolation("event_identity")


def _normalize_patch(patch, byte_limit: int) -> tuple[Mapping[str, object], ...]:
    """复制并限制单事件 patch byte 预算。"""
    value = tuple(_json_copy(item) for item in patch)
    if len(canonical_json(value).encode("utf-8")) > byte_limit:
        raise _StateViolation("event_patch_size")
    return value


def _validate_patch(patch, role: RoleSpec | None) -> None:
    """校验 operation 闭集、test 前缀与 declared 根权限。"""
    seen_mutation = False
    tests = 0
    for operation in patch:
        op, path = operation.get("op"), operation.get("path")
        if op not in _OPS or not isinstance(path, str):
            raise _StateViolation("patch_operation")
        if op == "test":
            if seen_mutation:
                raise _StateViolation("patch_test_order")
            tests += 1
        else:
            seen_mutation = True
        if role is not None:
            roots = role.read_roots if op == "test" else role.write_roots
            if not _path_covered(path, roots):
                raise _StateViolation("patch_permission")
    if tests == 0 or not seen_mutation:
        raise _StateViolation("patch_test_prefix")


def _apply_patch(before, patch):
    """在副本上原子应用 RFC 6902 patch。"""
    try:
        return jsonpatch.apply_patch(before, list(patch), in_place=False)
    except (jsonpatch.JsonPatchException, JsonPointerException, KeyError, TypeError, ValueError):
        raise _StateViolation("json_patch") from None


def _run_state_hook(context, role, before, after, patch) -> None:
    """用隔离副本执行冻结 state validator。"""
    hook = context.program.state_validator
    if hook is None:
        return
    value = StateTransitionInput(
        context.slot.slot_key,
        context.variant_name,
        None if role is None else role.name,
        before,
        after,
        patch,
    )
    try:
        result = hook.target(clone_state_input(value))
    except Exception:
        _log.error("state validator raised an exception")
        raise RuntimeError("state validator failed") from None
    try:
        violations = normalize_state_violations(result, hook.reference)
    except TypeError:
        _log.error("state validator returned an invalid value")
        raise PostValidatorInvalidError from None
    if violations:
        raise _StateViolation(violations)


def _validate_bindings(role: RoleSpec | None, before, after) -> None:
    """在 EventExecution 冻结前证明每个权威 binding 叶子存在。"""
    if role is None:
        return
    for item in role.payload_bindings:
        state = before if item.state_phase == "before" else after
        try:
            resolve_pointer(state, item.state_path)
        except (JsonPointerException, KeyError, TypeError):
            raise _StateViolation("payload_binding") from None


def _validate_schema(value, schema, kind: str) -> None:
    """对完整对象运行 Draft 2020-12 校验并生成无数据值违规。"""
    try:
        active = _json_copy(schema)
        errors = Draft202012Validator(active).iter_errors(value)
        violations = tuple(sorted({_schema_violation(kind, item, active) for item in errors}))
    except Exception:
        _contract_error("frozen state schema cannot be evaluated")
    if violations:
        raise _StateViolation(violations)


def outcome_schema_for(
    context: EventExecutionContext,
) -> Mapping[str, object] | None:
    """为 declared branch 的末事件选择唯一可选 outcome Schema。

    @param context 唯一事件执行上下文。
    @return 当前末事件的 outcome Schema；无需额外检查时为 None。
    """
    events = resolve_planned_events(context)
    if context.program.mode == "instruction_only" or context.event_index != len(events) - 1:
        return None
    source = next(
        (item for item in context.program.counterfactual_sets
         if item.name == context.slot.source_name),
        None,
    )
    if source is None:
        _contract_error("declared slot cannot resolve its counterfactual set")
    if context.variant_name is None:
        variant = next((item for item in source.variants if item.kind == "positive"), None)
        if variant is None:
            return None
    else:
        variant = next(
            (item for item in source.variants if item.name == context.variant_name),
            None,
        )
    if variant is None:
        _contract_error("declared branch cannot resolve its outcome schema")
    return variant.outcome_schema


def _schema_violation(kind: str, error, schema) -> str:
    """把 Schema 错误归一为 kind、实例 Pointer 与关键字。"""
    pointer = _safe_schema_pointer(schema, error.absolute_schema_path)
    return f"{kind}:{pointer}:{error.validator}"


def _safe_schema_pointer(schema, schema_path) -> str:
    """只用显式 properties 名称构造不含动态实例键的 Pointer。"""
    node, tokens, property_name = schema, [], False
    for part in schema_path:
        if not property_name and part in _DYNAMIC_SCHEMA_PATHS:
            break
        current_is_name = property_name
        if current_is_name:
            tokens.append(part)
            property_name = False
        try:
            node = node[part]
        except (KeyError, IndexError, TypeError):
            break
        if not current_is_name and part == "properties" and isinstance(node, Mapping):
            property_name = True
    pointer = "".join(
        "/" + str(token).replace("~", "~0").replace("/", "~1")
        for token in tokens
    )
    return pointer


def _project_paths(state, roots) -> dict[str, object]:
    """把 RFC 6901 roots 投影为 canonical path/value mapping。"""
    result = {}
    for root in roots:
        try:
            result[root] = _json_copy(resolve_pointer(state, root))
        except (JsonPointerException, KeyError, TypeError):
            raise _StateViolation("state_projection") from None
    return result


def _declared_observations(context, actor: str):
    """只投影历史 role 已发布给当前 actor 的事实。"""
    events = resolve_planned_events(context)
    pattern = context.program.patterns[context.slot.pattern_name]
    roles = {item.name: item for item in pattern.roles}
    observations = []
    for planned, draft in zip(events, context.history):
        if actor not in roles[planned.role].observers:
            continue
        observations.append({
            "source_event_key": draft.event_key,
            "logical_time_us": draft.logical_time_us,
            "values": _json_copy(draft.publish_snapshot),
        })
    return tuple(observations)


def project_instruction_draft(draft: EventDraft) -> dict[str, object]:
    """投影无递归 ActorView、工件 ID、timestamp 与 role 的 draft 语义。

    @param draft 已完成的 EventDraft
    @return 可安全重复嵌入 prompt 与 ActorView 的扁平语义投影
    """
    return {
        "event_key": draft.event_key,
        "logical_time_us": draft.logical_time_us,
        "frame_class": draft.frame_class,
        "actor": draft.actor,
        "intent": draft.intent,
        "patch": draft.patch,
        "state_before_hash": draft.state_before_hash,
        "state_after_hash": draft.state_after_hash,
        "publish_snapshot": _json_copy(draft.publish_snapshot),
        "payload": _json_copy(draft.payload),
    }


def _wait_since_previous(context, current: int) -> int:
    """计算当前事件的逻辑等待。"""
    return 0 if not context.history else current - context.history[-1].logical_time_us


def _path_covered(path: str, roots) -> bool:
    """按解码 token 而非字符串前缀校验路径根。"""
    try:
        parts = tuple(JsonPointer(path).parts)
        return any(_prefix(tuple(JsonPointer(root).parts), parts) for root in roots)
    except JsonPointerException:
        return False


def _prefix(left, right) -> bool:
    """判断解码 pointer token 的祖先或自身关系。"""
    return len(left) <= len(right) and left == right[:len(left)]


def _event_plan(candidate: Mapping[str, object]) -> EventPlan:
    """从 L2 候选构造唯一 EventPlan。"""
    try:
        return EventPlan(
            frame_class=candidate["frame_class"],
            actor=candidate["actor"],
            intent=candidate["intent"],
            patch=tuple(candidate["patch"]),
        )
    except (KeyError, TypeError, ValueError):
        raise _StateViolation("event_plan_candidate") from None


def _state_hash(state) -> str:
    """计算完整状态的 64 位 canonical SHA-256。"""
    return hashlib.sha256(canonical_json(state).encode("utf-8")).hexdigest()


def _json_copy(value):
    """通过 canonical JSON 复制冻结或可变 JSON 值。"""
    return json.loads(canonical_json(value))


def _contract_error(message: str):
    """记录并抛出不含数据的内部契约错误。"""
    _log.error("generation_downstream_contract: %s", message)
    raise InternalError(f"generation_downstream_contract: {message}")

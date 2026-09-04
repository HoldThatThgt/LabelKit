"""v1.20 ScenarioSeed、EventPlan 与完整 slot branch 生成。"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Mapping

from labelkit.common.contracts.generation import (
    ActorView,
    CouplingEvaluationRequest,
    DeliverySlot,
    EventDraft,
    EventExecution,
    EventExecutionContext,
    EventPlan,
    EventPlanRequest,
    EventTrace,
    EventTruth,
    GenerationProgram,
    GenerationServices,
    ObservedEvent,
    PostValidatedCallRequest,
    RenderEventRequest,
    ScenarioPlan,
    ScenarioSeed,
    ScenarioSeedRequest,
    SemanticEvaluationRequest,
    SemanticReviewEvent,
    StateEvaluationRequest,
)
from labelkit.common.config.generation import declared_actor_names, is_generation_frame_eligible
from labelkit.common.errors import (
    ContextOverflowError,
    InternalError,
    OutputTruncatedError,
    ProviderRetryableError,
    SchemaViolation,
)
from labelkit.common.inference.generation_prompts import (
    enforce_prompt_value_limit,
    event_plan_prompt,
    scenario_seed_prompt,
)
from labelkit.common.inference.schema_engine import CallScope, event_plan_schema, scenario_seed_schema
from labelkit.operators.generation import GenerationAttemptRejected
from labelkit.operators.generation.evaluate import (
    evaluate_coupling,
    evaluate_pattern,
    evaluate_semantics,
    evaluate_state,
)
from labelkit.operators.generation.project import (
    canonical_json,
    derive_generation_id,
    generation_random,
    validate_plan_identity,
)
from labelkit.operators.generation.render import (
    rebind_temporal_payload,
    render_event,
    time_binding_descriptor,
)
from labelkit.operators.generation.state import (
    binding_values,
    build_actor_view,
    execute_event,
    outcome_schema_for,
    post_validate_event_plan,
    project_instruction_draft,
    resolve_planned_events,
    resolve_role,
)


_log = logging.getLogger("labelkit.generation.scenario")


@dataclass(frozen=True)
class _SlotRun:
    """一次 slot attempt 的内部生成根。"""

    program: GenerationProgram  # 冻结生成程序。
    plan: ScenarioPlan  # 唯一冻结计划。
    slot: DeliverySlot  # 当前交付槽。
    seed: ScenarioSeed  # 当前 attempt 共享世界种子。
    attempt_index: int  # 零基尝试序号。
    services: GenerationServices  # 生成服务根。


@dataclass(frozen=True)
class _DraftRender:
    """一次 EventDraft 渲染的内部参数。"""

    actor_view: ActorView  # 当前 actor 知识视图。
    world_id: str  # 当前 branch 世界 ID。
    attempt_index: int  # 零基尝试序号。
    services: GenerationServices  # 生成服务根。


@dataclass(frozen=True)
class _VariantOutcome:
    """一条 counterfactual suffix 的声明序结果。"""

    ordinal: int  # variant 在 set 内的声明序号。
    trace: EventTrace | None  # 成功生成的 branch trace。
    error: Exception | None  # 可恢复失败；成功时为空。


class _VariantFatal(Exception):
    """在 TaskGroup 内绑定 fatal 的 variant 声明序。"""

    def __init__(self, ordinal: int, error: Exception):
        """保存原异常，供结构化清理后稳定还原。

        @param ordinal variant 声明序号。
        @param error 原 fatal/control/internal 异常。
        """
        self.ordinal = ordinal
        self.error = error
        super().__init__("counterfactual variant failed")


class _VariantCancellation(Exception):
    """把 suffix 主动取消转换为可触发 sibling cleanup 的内部信号。"""

    def __init__(self, ordinal: int, error: asyncio.CancelledError):
        """保存声明序与原始取消对象。

        @param ordinal variant 声明序号
        @param error 原始取消对象
        """
        self.ordinal = ordinal
        self.error = error
        super().__init__("counterfactual variant cancelled")


async def generate_scenario_seed(
    request: ScenarioSeedRequest,
    services: GenerationServices,
) -> ScenarioSeed:
    """从 catalog 或独立 LLM 调用生成事件发生前的完整世界快照。

    @param request 场景种子请求。
    @param services 生成服务根。
    @return 已通过 Schema 校验的场景种子。
    """
    source = _seed_source(request.program, request.slot)
    actors = _declared_actors(request.program, request.slot)
    state_schema = _seed_state_schema(request.program, request.slot)
    schema = scenario_seed_schema(actors, state_schema)
    if source is not None and source.initial_state_source == "catalog":
        value = source.initial_state_catalog[request.slot.catalog_row_index]
    else:
        services.metrics.count("generate.sequence.calls.scenario_seed_calls")
        try:
            result = await services.schema_engine.complete_validated(
                request.program.semantic_profile,
                _seed_prompt(request, actors, state_schema),
                schema=_json_copy(schema),
                scope=CallScope(
                    record_ids=(request.slot.slot_key,),
                    repair_context_bytes=request.program.limits.repair_context_bytes,
                ),
            )
        except SchemaViolation:
            raise GenerationAttemptRejected("scenario_schema", request.slot.slot_key) from None
        value = result[0]
    seed = _scenario_seed(value, request.slot.slot_key)
    _validate_seed(
        seed, schema, request.program.limits.scenario_seed_bytes,
        services, request.slot.slot_key,
    )
    return seed


def build_event_plan_request(
    context: EventExecutionContext,
    attempt_index: int,
    variation_nonce: str,
) -> EventPlanRequest:
    """从唯一执行上下文投影一个 prompt-safe 事件规划请求。

    @param context 唯一事件执行上下文。
    @param attempt_index 当前交付槽尝试序号。
    @param variation_nonce 当前事件变化 nonce。
    @return 不含隐藏或重复真值的事件规划请求。
    """
    validate_plan_identity(context.program, context.plan)
    return _build_validated_event_plan_request(context, attempt_index, variation_nonce)


def _build_validated_event_plan_request(
    context: EventExecutionContext,
    attempt_index: int,
    variation_nonce: str,
) -> EventPlanRequest:
    """在完整计划已经验根后机械投影事件规划请求。"""
    events = resolve_planned_events(context)
    planned = events[context.event_index]
    role = resolve_role(context)
    public = _public_facts(context)
    if context.program.mode == "declared":
        instruction = _seed_source(context.program, context.slot).instruction
        eligible_frames = OrderedDict(((role.frame_class, context.program.frame_classes[role.frame_class]),))
        actor_view = build_actor_view(context)
        return EventPlanRequest(
            "declared", context.program.semantic_profile, context.slot.slot_key, planned,
            role, instruction, len(events), eligible_frames, (role.actor,), actor_view,
            None, None, outcome_schema_for(context), None, None, public,
            attempt_index, variation_nonce,
        )
    source = _instruction_source(context.program, context.slot)
    noise_name = None if context.program.noise is None else context.program.noise.frame_class
    eligible_frames = OrderedDict(
        (name, frame) for name, frame in context.program.frame_classes.items()
        if name != noise_name and is_generation_frame_eligible(frame)
    )
    profiles = _instruction_actor_profiles(context.scenario_seed)
    return EventPlanRequest(
        "instruction_only", context.program.semantic_profile, context.slot.slot_key, planned,
        None, source.instruction, len(events), eligible_frames, tuple(profiles), None,
        context.current_state, source.state_schema, None, context.history, profiles, public,
        attempt_index, variation_nonce,
    )


async def plan_event(
    context: EventExecutionContext,
    attempt_index: int,
    variation_nonce: str,
    services: GenerationServices,
) -> tuple[EventPlan, EventExecution]:
    """规划一个冻结事件并返回其唯一缓存执行证明。

    @param context 唯一事件执行上下文。
    @param attempt_index 当前交付槽尝试序号。
    @param variation_nonce 当前事件变化 nonce。
    @param services 生成服务根。
    @return 事件计划与同一候选的执行证明。
    """
    validate_plan_identity(context.program, context.plan)
    return await _plan_validated_event(context, attempt_index, variation_nonce, services)


async def _plan_validated_event(
    context: EventExecutionContext,
    attempt_index: int,
    variation_nonce: str,
    services: GenerationServices,
) -> tuple[EventPlan, EventExecution]:
    """在完整计划已经验根后规划并执行一个事件。"""
    request = _build_validated_event_plan_request(context, attempt_index, variation_nonce)
    key = ("baseline_event_plan_calls" if context.variant_name is None
           else "variant_event_plan_calls")
    services.metrics.count(f"generate.sequence.calls.{key}")
    post_request = PostValidatedCallRequest(
        request.semantic_profile,
        _event_prompt(request, context.program.limits.prompt_value_bytes),
        _json_copy(event_plan_schema(tuple(request.eligible_frame_classes), request.eligible_actors)),
        CallScope(
            record_ids=(request.planned_event.event_key,),
            repair_context_bytes=context.program.limits.repair_context_bytes,
        ),
        lambda candidate: post_validate_event_plan(candidate, context),
    )
    try:
        result = await services.schema_engine.complete_post_validated(post_request)
    except SchemaViolation as exc:
        kind = _plan_rejection_kind(exc)
        raise GenerationAttemptRejected(kind, context.slot.slot_key) from None
    plan = EventPlan(
        result.object["frame_class"], result.object["actor"], result.object["intent"],
        tuple(result.object["patch"]),
    )
    return plan, result.event_execution


async def generate_slot_traces(
    program: GenerationProgram,
    plan: ScenarioPlan,
    slot: DeliverySlot,
    attempt_index: int,
    services: GenerationServices,
) -> tuple[EventTrace, ...]:
    """完整生成并判定一个 slot 的所有交付 branch。

    @param program 冻结生成程序。
    @param plan 唯一冻结 ScenarioPlan。
    @param slot 当前串行准入槽。
    @param attempt_index 零基 slot 尝试序号。
    @param services 生成服务根。
    @return 与 slot.variant_names 声明序完全对齐的 EventTrace。
    """
    validate_plan_identity(program, plan)
    _validate_slot_root(program, plan, slot)
    return await _generate_validated_slot_traces(
        program, plan, slot, attempt_index, services
    )


async def _generate_validated_slot_traces(
    program: GenerationProgram,
    plan: ScenarioPlan,
    slot: DeliverySlot,
    attempt_index: int,
    services: GenerationServices,
) -> tuple[EventTrace, ...]:
    """在上层已完整验证 plan 后执行一个槽，避免逐槽重复扫描计划。

    @param program 已与计划绑定的冻结程序
    @param plan 已完整验证的冻结计划
    @param slot 来自 plan.delivery_slots 的当前槽
    @param attempt_index 零基槽尝试序号
    @param services 生成服务根
    @return 与 slot.variant_names 声明序对齐的 EventTrace
    """
    try:
        return await _run_slot(program, plan, slot, attempt_index, services)
    except GenerationAttemptRejected as exc:
        if exc.slot_key == slot.slot_key:
            raise
        raise GenerationAttemptRejected(exc.kind, slot.slot_key) from None


async def _run_slot(program, plan, slot, attempt_index: int, services):
    """在已验证根引用下执行一次完整 slot attempt。"""
    random_seed = _attempt_random(
        program.planner_seed, slot.slot_key, attempt_index, "scenario_seed"
    )
    seed = await generate_scenario_seed(
        ScenarioSeedRequest(program, slot, attempt_index, random_seed), services
    )
    run = _SlotRun(program, plan, slot, seed, attempt_index, services)
    if program.mode == "instruction_only":
        trace = await _generate_instruction(run)
        return (trace,)
    return await _generate_declared(run)


async def _generate_declared(run: _SlotRun):
    """生成 hidden baseline，并发派生独立 suffix 后按声明序归并。"""
    source = next(
        item for item in run.program.counterfactual_sets
        if item.name == run.slot.source_name
    )
    positive = next((item for item in source.variants if item.kind == "positive"), None)
    baseline_name = None if positive is None else positive.name
    baseline = await _generate_fresh_branch(run, baseline_name, True)
    _require_expected(baseline, {})
    return await _generate_variant_set(run, baseline, source)


async def _generate_variant_set(run: _SlotRun, baseline, source):
    """结构化运行 sibling suffix，并按 variant 声明序选择结果。"""
    tasks: dict[int, asyncio.Task[_VariantOutcome]] = {}
    try:
        async with asyncio.TaskGroup() as group:
            for ordinal, variant in enumerate(source.variants):
                if variant.kind != "positive":
                    tasks[ordinal] = group.create_task(
                        _generate_variant_outcome(run, baseline, variant, ordinal)
                    )
    except BaseExceptionGroup as group:
        raise _unwrap_variant_group(group) from None
    outcomes = {ordinal: task.result() for ordinal, task in tasks.items()}
    traces: list[EventTrace] = []
    for ordinal, variant in enumerate(source.variants):
        outcome = outcomes.get(ordinal)
        if outcome is not None and outcome.error is not None:
            raise outcome.error
        trace = baseline if outcome is None else outcome.trace
        if trace is None:
            _log.error("counterfactual variant completed without a trace")
            raise InternalError("counterfactual variant completed without a trace")
        if outcome is None:
            _require_expected(trace, variant.expected_violation)
        traces.append(trace)
    return tuple(traces)


async def _generate_variant_outcome(run: _SlotRun, baseline, variant,
                                    ordinal: int) -> _VariantOutcome:
    """生成一条 suffix，把可恢复失败冻结为普通结果。"""
    try:
        operation_task = asyncio.create_task(_generate_variant(run, baseline, variant))
        trace = await operation_task
        _require_expected(trace, variant.expected_violation)
        return _VariantOutcome(ordinal, trace, None)
    except asyncio.CancelledError as exc:
        task = asyncio.current_task()
        if task is None or task.cancelling() == 0:
            raise _VariantCancellation(ordinal, exc) from None
        raise
    except (GenerationAttemptRejected, ProviderRetryableError,
            ContextOverflowError, OutputTruncatedError) as exc:
        return _VariantOutcome(ordinal, None, exc)
    except Exception as exc:
        raise _VariantFatal(ordinal, exc) from exc


def _unwrap_variant_group(group: BaseExceptionGroup) -> BaseException:
    """从 TaskGroup 异常树中按 variant 声明序还原原异常。"""
    leaves = _flatten_variant_group(group)
    cancellations = [leaf for leaf in leaves if isinstance(leaf, _VariantCancellation)]
    if cancellations:
        return min(cancellations, key=lambda failure: failure.ordinal).error
    failures = [leaf for leaf in leaves if isinstance(leaf, _VariantFatal)]
    if failures:
        return min(failures, key=lambda failure: failure.ordinal).error
    ordinary = [leaf for leaf in leaves if not isinstance(leaf, asyncio.CancelledError)]
    if ordinary:
        _log.error("counterfactual variant group raised an untracked exception")
        return ordinary[0]
    return asyncio.CancelledError()


def _flatten_variant_group(group: BaseExceptionGroup) -> list[BaseException]:
    """递归展开 branch-local TaskGroup 异常树。"""
    leaves: list[BaseException] = []
    for error in group.exceptions:
        if isinstance(error, BaseExceptionGroup):
            leaves.extend(_flatten_variant_group(error))
        else:
            leaves.append(error)
    return leaves


async def _generate_instruction(run: _SlotRun):
    """生成一条无 pattern 声称的 instruction-only branch。"""
    return await _generate_fresh_branch(run, None, False)


async def _generate_fresh_branch(run: _SlotRun, variant_name, baseline: bool):
    """从初始状态逐事件 plan/execute/render 一条 branch。"""
    state, drafts = _json_copy(run.seed.initial_state), []
    events = _block_events(run.plan, run.slot.slot_key, None if baseline else variant_name)
    world_id = _world_branch_id(run.program, run.slot, variant_name)
    for index, planned in enumerate(events):
        context = EventExecutionContext(
            run.program, run.plan, run.slot, None if baseline else variant_name,
            index, run.seed, state, tuple(drafts),
        )
        nonce = _variation_nonce(run, world_id, index)
        event_plan, execution = await _plan_validated_event(
            context, run.attempt_index, nonce, run.services
        )
        selected = event_plan.actor if run.program.mode == "instruction_only" else None
        render = _DraftRender(
            build_actor_view(context, selected), world_id, run.attempt_index, run.services
        )
        draft = await _render_draft(context, planned, event_plan, execution, render)
        drafts.append(draft)
        state = _json_copy(execution.state_after)
    return await _evaluate_trace(run, variant_name, tuple(drafts), tuple(drafts), state)


async def _generate_variant(run: _SlotRun, baseline, variant):
    """复用 protected prefix 并重规划 causal suffix。"""
    state, drafts = _json_copy(run.seed.initial_state), []
    events = _block_events(run.plan, run.slot.slot_key, variant.name)
    protected = _protected_count(baseline.events, variant.divergence_role)
    world_id = _world_branch_id(run.program, run.slot, variant.name)
    for index, planned in enumerate(events):
        context = EventExecutionContext(
            run.program, run.plan, run.slot, variant.name, index,
            run.seed, state, tuple(drafts),
        )
        if index < protected:
            draft, execution = _reuse_draft(context, planned, baseline.events[index], world_id)
        else:
            nonce = _variation_nonce(run, world_id, index)
            event_plan, execution = await _plan_validated_event(
                context, run.attempt_index, nonce, run.services
            )
            render = _DraftRender(
                build_actor_view(context), world_id, run.attempt_index, run.services
            )
            draft = await _render_draft(context, planned, event_plan, execution, render)
        drafts.append(draft)
        state = _json_copy(execution.state_after)
    return await _evaluate_trace(run, variant.name, tuple(drafts), baseline.events, state)


async def _render_draft(
    context, planned, event_plan, execution, render: _DraftRender,
):
    """渲染一个新事件并构造无 role 的 EventDraft。"""
    role = resolve_role(context)
    values = binding_values(role, execution)
    request = RenderEventRequest(
        context.program.semantic_profile, context.slot.slot_key, planned, event_plan,
        render.actor_view, execution.publish_snapshot, execution.state_before_hash,
        execution.state_after_hash, values, context.program.frame_classes[event_plan.frame_class],
        role, _public_facts(context), render.attempt_index,
        context.program.timeline.utc_offset_minutes, context.program.limits,
    )
    payload = await render_event(request, render.services)
    frame = context.program.frame_classes[event_plan.frame_class]
    event_id = derive_generation_id(
        "primary_event_id", [
            render.world_id, planned.event_key, planned.timestamp_us, planned.duration_us,
            planned.resources, time_binding_descriptor(frame), payload,
        ],
    )
    return EventDraft(
        planned.event_key, event_id, event_plan.frame_class, event_plan.actor,
        planned.logical_time_us, planned.timestamp_us, planned.duration_us,
        render.actor_view, event_plan.intent,
        execution.normalized_patch, execution.state_before_hash, execution.state_after_hash,
        execution.publish_snapshot, payload,
    )


def _reuse_draft(context, planned, baseline, world_id: str):
    """重执行 protected patch，并把 payload 重绑到当前 branch 时间。"""
    event_plan = EventPlan(baseline.frame_class, baseline.actor, baseline.intent, baseline.patch)
    execution = execute_event(context, event_plan)
    if (
        execution.state_before_hash != baseline.state_before_hash
        or execution.state_after_hash != baseline.state_after_hash
        or canonical_json(execution.publish_snapshot) != canonical_json(baseline.publish_snapshot)
        or planned.event_key != baseline.event_key
        or planned.logical_time_us != baseline.logical_time_us
    ):
        raise GenerationAttemptRejected("coupling_evaluation", context.slot.slot_key)
    frame = context.program.frame_classes[baseline.frame_class]
    payload = rebind_temporal_payload(
        baseline.payload, frame, planned, context.program.timeline.utc_offset_minutes,
    )
    event_id = derive_generation_id(
        "primary_event_id", [
            world_id, planned.event_key, planned.timestamp_us, planned.duration_us,
            planned.resources, time_binding_descriptor(frame), payload,
        ],
    )
    draft = EventDraft(
        planned.event_key, event_id, baseline.frame_class, baseline.actor,
        baseline.logical_time_us, planned.timestamp_us, planned.duration_us,
        baseline.actor_view, baseline.intent,
        baseline.patch, baseline.state_before_hash, baseline.state_after_hash,
        baseline.publish_snapshot, payload,
    )
    return draft, execution


async def _evaluate_trace(
    run: _SlotRun, variant_name, drafts, baseline_events, final_state,
):
    """按 pattern、state、coupling、semantic 顺序组装 EventTrace。"""
    variant = _variant(run.program, run.slot, variant_name)
    pattern = (None if run.slot.pattern_name is None
               else run.program.patterns[run.slot.pattern_name])
    pattern_evaluation, truths = _bind_truths(pattern, drafts)
    baseline_truths = _baseline_truths(baseline_events, pattern)
    try:
        state_evaluation = evaluate_state(StateEvaluationRequest(
            run.program, run.slot, pattern, variant, run.seed,
            truths, baseline_truths, final_state,
        ))
    except RuntimeError:
        raise GenerationAttemptRejected("state_evaluation", run.slot.slot_key) from None
    if not _state_passes(state_evaluation):
        raise GenerationAttemptRejected("state_evaluation", run.slot.slot_key)
    if variant is not None and not evaluate_coupling(
        CouplingEvaluationRequest(
            variant, baseline_truths, truths, run.program.frame_classes,
        )
    ):
        raise GenerationAttemptRejected("coupling_evaluation", run.slot.slot_key)
    semantic = await evaluate_semantics(
        _semantic_request(run, drafts, final_state), run.services
    )
    scenario_id = _scenario_id(run.program, run.slot)
    world_id = _world_branch_id(run.program, run.slot, variant_name)
    return EventTrace(
        scenario_id, world_id, run.slot.sequence_class, run.slot.pattern_name,
        variant_name, run.seed, truths, final_state, pattern_evaluation,
        state_evaluation, semantic,
    )


def _bind_truths(pattern, drafts):
    """独立 binding 通过后才把 EventDraft 转成 EventTruth。"""
    if pattern is None:
        truths = tuple(_truth(draft, f"position_{index:03d}")
                       for index, draft in enumerate(drafts))
        return None, truths
    observed = tuple(ObservedEvent(
        item.event_id, item.frame_class, item.timestamp_us, item.duration_us,
    ) for item in drafts)
    evaluation = evaluate_pattern(pattern, observed)
    if set(evaluation.actual_bindings) != {item.event_id for item in drafts}:
        raise GenerationAttemptRejected("pattern_evaluation", pattern.name)
    roles = tuple(evaluation.actual_bindings[item.event_id] for item in drafts)
    if len(set(roles)) != len(roles):
        raise GenerationAttemptRejected("pattern_evaluation", pattern.name)
    return evaluation, tuple(_truth(draft, role) for draft, role in zip(drafts, roles))


def _baseline_truths(events, pattern):
    """确保 State/Coupling oracle 获得已绑定的 baseline EventTruth。"""
    if not events:
        return ()
    if isinstance(events[0], EventTruth):
        return tuple(events)
    return _bind_truths(pattern, events)[1]


def _truth(draft: EventDraft, role: str) -> EventTruth:
    """仅在独立 binding 后为 draft 增加 role。"""
    return EventTruth(
        draft.event_key, draft.event_id, role, draft.frame_class, draft.actor,
        draft.logical_time_us, draft.timestamp_us, draft.duration_us,
        draft.actor_view, draft.intent,
        draft.patch, draft.state_before_hash, draft.state_after_hash,
        draft.publish_snapshot, draft.payload,
    )


def _semantic_request(run: _SlotRun, drafts, final_state):
    """直接从 EventDraft 构造不含结构 verdict 的盲审请求。"""
    reviews = tuple(SemanticReviewEvent(
        item.frame_class, item.actor, item.logical_time_us, item.duration_us,
        item.actor_view.wait_since_previous_us, item.actor_view, item.intent, item.patch,
        item.state_before_hash, item.state_after_hash, item.publish_snapshot, item.payload,
    ) for item in drafts)
    class_view = run.program.class_views[run.slot.sequence_class]
    description = (_instruction_source(run.program, run.slot).instruction
                   if run.program.mode == "instruction_only"
                   else run.program.patterns[run.slot.pattern_name].description)
    return SemanticEvaluationRequest(
        run.program.evaluation_profile, run.program.mode, run.slot.sequence_class,
        class_view.description, description, run.seed, reviews, final_state,
        run.attempt_index, run.program.limits,
    )


def _event_prompt(request: EventPlanRequest, prompt_value_bytes: int):
    """按冻结插值序构造 event-plan prompt。"""
    role_contract = None if request.role is None else _role_contract(request.role)
    frames = [{
        "name": name,
        "description": frame.description,
        "generation_instruction": frame.gen_instruction,
    } for name, frame in request.eligible_frame_classes.items()]
    wait = (request.actor_view.wait_since_previous_us if request.actor_view is not None
            else _instruction_wait(request))
    event = request.planned_event
    history = _history_projection(request.history)
    dynamic = {"public_facts": request.public_facts}
    if request.mode == "declared":
        dynamic["actor_view"] = request.actor_view
    else:
        dynamic.update({
            "eligible_actors": request.eligible_actors,
            "visible_state": request.visible_state,
            "history": history,
            "actor_profiles": request.actor_profiles,
        })
    enforce_prompt_value_limit(request.semantic_profile, prompt_value_bytes, dynamic)
    return event_plan_prompt({
        "mode": request.mode, "slot_key": request.slot_key,
        "attempt_index": request.attempt_index, "variation_nonce": request.variation_nonce,
        "event_key": event.event_key, "role": event.role, "position": event.position,
        "sequence_length": request.sequence_length, "logical_time_us": event.logical_time_us,
        "wait_since_previous_us": wait, "generation_instruction": request.generation_instruction,
        "role_contract": role_contract, "eligible_frame_classes": frames,
        "eligible_actors": request.eligible_actors, "actor_view": request.actor_view,
        "visible_state": request.visible_state, "state_schema": request.state_schema,
        "outcome_schema": request.outcome_schema,
        "history": history,
        "actor_profiles": request.actor_profiles, "public_facts": request.public_facts,
    })


def _seed_prompt(request, actors, state_schema):
    """按冻结插值序构造 ScenarioSeed prompt。"""
    class_view = request.program.class_views[request.slot.sequence_class]
    actor_contract = _seed_actor_contract(request.program.mode, actors)
    if request.program.mode == "declared":
        instruction = class_view.sequence_generation.instruction
    else:
        instruction = _instruction_source(request.program, request.slot).instruction
    slot = request.slot
    return scenario_seed_prompt({
        "mode": request.program.mode, "slot_key": slot.slot_key,
        "source_name": slot.source_name, "scenario_index": slot.scenario_index,
        "attempt_index": request.attempt_index, "sequence_class": slot.sequence_class,
        "class_description": class_view.description,
        "generation_instruction": instruction, "actor_contract": actor_contract,
        "state_schema": state_schema,
    })


def _seed_actor_contract(mode: str, actors) -> dict[str, object]:
    """构造 L0-off 也能读取的完整 actor profile 约束。

    @param mode declared 或 instruction_only。
    @param actors declared actor 名称序列。
    @return 可直接写入 prompt 的约束对象。
    """
    profile = {"required": ["goal", "identity", "style"], "each_value": "object"}
    if mode == "declared":
        return {"actor_names": list(actors), "actor_profile": profile}
    return {
        "minimum_actor_count": 1,
        "maximum_actor_count": 8,
        "actor_name": "non-empty string",
        "actor_profile": profile,
    }


def _history_projection(history):
    """移除 EventDraft 的递归 ActorView、ID、工件坐标与 role。"""
    if history is None:
        return None
    return [project_instruction_draft(item) for item in history]


def _role_contract(role):
    """投影不含 binding/calendar 的 prompt-safe RoleSpec。"""
    return {
        "name": role.name,
        "frame_class": role.frame_class,
        "actor": role.actor,
        "read_roots": role.read_roots,
        "write_roots": role.write_roots,
        "publish_roots": role.publish_roots,
        "observers": role.observers,
        "state_instruction": role.state_instruction,
        "pre_state_schema": role.pre_state_schema,
    }


def _validate_seed(seed, schema, byte_limit: int, services, slot_key: str) -> None:
    """复验 seed 闭集、Schema 与 canonical byte 上限。"""
    if services.schema_engine.validate_only(_json_copy(seed), schema=_json_copy(schema)):
        raise GenerationAttemptRejected("scenario_schema", slot_key)
    if len(canonical_json(seed).encode("utf-8")) > byte_limit:
        raise GenerationAttemptRejected("scenario_schema", slot_key)


def _scenario_seed(value, slot_key: str) -> ScenarioSeed:
    """从已验证 object 构造冻结 ScenarioSeed。"""
    try:
        return ScenarioSeed(
            value["initial_state"], value["actors"], value["shared_facts"],
            value["style"], value["time_context"],
        )
    except (KeyError, TypeError):
        raise GenerationAttemptRejected("scenario_schema", slot_key) from None


def _require_expected(trace, expected) -> None:
    """要求 declared 独立违规恰等于 variant 声明。"""
    evaluation = trace.pattern_evaluation
    actual = () if evaluation is None else tuple(dict(item) for item in evaluation.actual_violations)
    wanted = () if not expected else (dict(expected),)
    if actual != wanted:
        raise GenerationAttemptRejected("pattern_evaluation", trace.scenario_id)


def _state_passes(value) -> bool:
    """将 StateEvaluation 的全部独立证明合取。"""
    return (
        value.replay_hash == value.final_state_hash
        and value.bindings_valid
        and value.outcome_valid
        and value.protected_prefix_valid
    )


def _plan_rejection_kind(exc: SchemaViolation) -> str:
    """将 M8 event-plan 失败安全分类到 report 闭集。"""
    if exc.errors == ["post_validator_invalid"]:
        return "post_validator_invalid"
    if exc.errors == ["post_validator_exception"]:
        return "post_validator_exception"
    if exc.errors and all(item.startswith("(post-validator) ") for item in exc.errors):
        return "state_transition"
    return "event_schema"


def _validate_slot_root(program, plan, slot) -> None:
    """在零 LLM 调用前校验 program/plan/slot 单一属主。"""
    if slot not in plan.delivery_slots:
        _contract_error("slot does not belong to ScenarioPlan")
    if program.mode == "instruction_only" and slot.variant_names:
        _contract_error("instruction-only slot carries variants")
    if program.mode == "declared" and not slot.variant_names:
        _contract_error("declared slot has no variants")


def _block_events(plan, slot_key: str, variant_name: str | None):
    """从唯一 ScenarioBlock 解析 branch 事件。"""
    matches = [block[(slot_key, variant_name)] for block in plan.blocks
               if (slot_key, variant_name) in block]
    if len(matches) != 1:
        _contract_error("scenario block key is missing or duplicated")
    return matches[0]


def _variant(program, slot, variant_name):
    """按 slot 声明唯一解析 VariantSpec。"""
    if variant_name is None:
        return None
    source = next(item for item in program.counterfactual_sets if item.name == slot.source_name)
    return next(item for item in source.variants if item.name == variant_name)


def _protected_count(events, divergence_role: str | None) -> int:
    """计算 divergence role 之前的受保护事件数。"""
    if divergence_role is None:
        return len(events)
    return next(index for index, event in enumerate(events) if event.role == divergence_role)


def _seed_source(program, slot):
    """返回 declared 类世界生成配置。"""
    if program.mode != "declared":
        return None
    return program.class_views[slot.sequence_class].sequence_generation


def _instruction_source(program, slot):
    """返回 slot 唯一 instruction-only 声明。"""
    return next(item for item in program.instruction_only if item.name == slot.source_name)


def _seed_state_schema(program, slot):
    """从 program/slot 唯一选择 ScenarioSeed 状态 Schema。"""
    source = _seed_source(program, slot)
    return (source.state_schema if source is not None
            else _instruction_source(program, slot).state_schema)


def _declared_actors(program, slot):
    """从 declared pattern 按 role 声明序投影 actor 闭集。"""
    if program.mode == "instruction_only":
        return None
    pattern = program.patterns[slot.pattern_name]
    return declared_actor_names(pattern)


def _public_facts(context) -> Mapping[str, object]:
    """只返回 ScenarioSeed 明确标记的 public facts。"""
    public = context.scenario_seed.shared_facts.get("public")
    if not isinstance(public, Mapping):
        _contract_error("ScenarioSeed public facts are missing")
    return public


def _instruction_actor_profiles(seed: ScenarioSeed) -> OrderedDict:
    """校验并按声明序复制 instruction-only 动态 actor 闭集。

    @param seed 已冻结 ScenarioSeed
    @return 一至八个非空 actor profile 的有序表
    """
    profiles = OrderedDict(seed.actors.items())
    valid_names = all(isinstance(name, str) and bool(name) for name in profiles)
    if not 1 <= len(profiles) <= 8 or not valid_names:
        _contract_error("instruction-only actor registry is invalid")
    return profiles


def _instruction_wait(request: EventPlanRequest) -> int:
    """从完整 EventDraft history 派生 instruction-only 逻辑等待。"""
    if not request.history:
        return 0
    return request.planned_event.logical_time_us - request.history[-1].logical_time_us


def _scenario_id(program, slot) -> str:
    """按模式冻结域派生 scenario ID。"""
    domain = "declared_scenario_id" if program.mode == "declared" else "instruction_scenario_id"
    return derive_generation_id(domain, [program.digest, slot.source_name, slot.scenario_index])


def _world_branch_id(program, slot, variant_name) -> str:
    """按模式冻结域派生 world branch ID。"""
    scenario = _scenario_id(program, slot)
    if program.mode == "instruction_only":
        return derive_generation_id("instruction_world_branch_id", [scenario, "instruction_only"])
    if variant_name is None:
        return derive_generation_id("declared_hidden_baseline_world_branch_id", [scenario])
    return derive_generation_id("declared_world_branch_id", [scenario, variant_name])


def _attempt_random(seed: int, slot_identity: str, attempt: int, purpose: str) -> int:
    """按冻结 domain framing 派生完整尝试随机整数。"""
    return generation_random("attempt_random", [seed, slot_identity, attempt, purpose])


def _variation_nonce(run: _SlotRun, branch, index: int) -> str:
    """派生不携带结构目标的 prompt 变化 nonce。"""
    seed = _attempt_random(
        run.program.planner_seed,
        run.slot.slot_key,
        run.attempt_index,
        f"event:{branch}:{index}",
    )
    return f"{seed:064x}"


def _json_copy(value):
    """复制冻结或可变 JSON 值。"""
    return json.loads(canonical_json(value))


def _contract_error(message: str):
    """记录并抛出不含数据的下游契约错误。"""
    _log.error("generation_downstream_contract: %s", message)
    raise InternalError(f"generation_downstream_contract: {message}")

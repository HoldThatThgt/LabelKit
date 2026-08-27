"""ActorView、逐事件权限、状态 hook 与原子 patch 的离线测试。"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from labelkit.common.contracts.generation import (
    EventDraft,
    EventExecutionContext,
    ScenarioSeed,
    StateEvaluationRequest,
)
from labelkit.common.extensions.hooks import ResolvedHook
from labelkit.common.errors import PostValidatorInvalidError
from labelkit.operators.generation.evaluate import evaluate_state
from labelkit.operators.generation.planner import compile_scenario_plan
from labelkit.operators.generation.project import canonical_json
from labelkit.operators.generation.program import generation_program_digest
from labelkit.operators.generation.state import (
    binding_values,
    build_actor_view,
    outcome_schema_for,
    post_validate_event_plan,
    resolve_planned_events,
    resolve_role,
)


def _context(program, plan):
    """构造首槽 hidden baseline 的首事件执行根。"""
    slot = plan.delivery_slots[0]
    config = program.class_views[slot.sequence_class].sequence_generation
    seed = ScenarioSeed(**dict(config.initial_state_catalog[slot.catalog_row_index]))
    return EventExecutionContext(
        program, plan, slot, None, 0, seed, seed.initial_state, (),
    )


def _rehash(program):
    """为测试内协调修改后的程序重建权威摘要。"""
    return replace(program, digest=generation_program_digest(program))


def _request_candidate(patch):
    """构造首个 request role 的 EventPlan 候选。"""
    return {
        "frame_class": "task_request",
        "actor": "requester",
        "intent": "submit booking request",
        "patch": patch,
    }


def _request_transition(context):
    """执行教学配置的首个 request 转换。"""
    candidate = _request_candidate((
        {"op": "test", "path": "/request/status", "value": "new"},
        {"op": "replace", "path": "/request/status", "value": "pending"},
    ))
    return post_validate_event_plan(candidate, context).event_execution


def _ack_context(program, plan):
    """构造已完成 request 后的 acknowledgement 执行根。"""
    first = _context(program, plan)
    execution = _request_transition(first)
    planned = resolve_planned_events(first)[0]
    view = build_actor_view(first)
    draft = EventDraft(
        planned.event_key, "first-event", "task_request", "requester",
        planned.logical_time_us, planned.timestamp_us, planned.duration_us, view, "submit request",
        execution.normalized_patch, execution.state_before_hash, execution.state_after_hash,
        execution.publish_snapshot, {"request_id": "R-100", "status": "pending"},
    )
    return EventExecutionContext(
        program, plan, first.slot, None, 1, first.scenario_seed,
        execution.state_after, (draft,),
    )


def _confirm_context(program, plan):
    """构造已完成 request 与 acknowledgement 的末事件执行根。"""
    context = _ack_context(program, plan)
    candidate = {
        "frame_class": "acknowledgement",
        "actor": "system",
        "intent": "acknowledge request",
        "patch": (
            {"op": "test", "path": "/request/status", "value": "pending"},
            {"op": "replace", "path": "/request/acknowledged", "value": True},
            {"op": "replace", "path": "/request/status", "value": "acknowledged"},
        ),
    }
    execution = post_validate_event_plan(candidate, context).event_execution
    planned = resolve_planned_events(context)[1]
    draft = EventDraft(
        planned.event_key, "second-event", "acknowledgement", "system",
        planned.logical_time_us, planned.timestamp_us, planned.duration_us, build_actor_view(context),
        "acknowledge request", execution.normalized_patch, execution.state_before_hash,
        execution.state_after_hash, execution.publish_snapshot,
        {"request_id": "R-100", "status": "acknowledged"},
    )
    return EventExecutionContext(
        program, plan, context.slot, None, 2, context.scenario_seed,
        execution.state_after, (*context.history, draft),
    )


def test_actor_view_excludes_hidden_state_and_uses_declared_roots(declared_program):
    plan = compile_scenario_plan(declared_program)
    view = build_actor_view(_context(declared_program, plan))
    serialized = canonical_json(view)
    assert view.actor == "requester"
    assert "hidden_sentinel" not in serialized
    assert "risk_score" not in serialized
    assert set(view.read_state) == {"/public", "/goal", "/request", "/actors/requester"}


def test_published_fact_stays_hidden_from_non_observer(declared_program):
    pattern = declared_program.patterns["booking_success"]
    request_role = replace(pattern.roles[0], observers=("requester",))
    changed_pattern = replace(pattern, roles=(request_role, *pattern.roles[1:]))
    program = _rehash(replace(
        declared_program,
        patterns={**declared_program.patterns, pattern.name: changed_pattern},
    ))
    plan = compile_scenario_plan(program)
    context = _ack_context(program, plan)
    assert context.history[0].publish_snapshot
    assert build_actor_view(context).actor == "system"
    assert build_actor_view(context).observations == ()


def test_event_patch_executes_once_and_produces_authoritative_bindings(declared_program):
    plan = compile_scenario_plan(declared_program)
    context = _context(declared_program, plan)
    candidate = _request_candidate((
        {"op": "test", "path": "/request/status", "value": "new"},
        {"op": "replace", "path": "/request/status", "value": "pending"},
    ))
    result = post_validate_event_plan(candidate, context)
    assert result.violations == ()
    assert result.event_execution.state_before["request"]["status"] == "new"
    assert result.event_execution.state_after["request"]["status"] == "pending"
    values = binding_values(resolve_role(context), result.event_execution)
    assert values == {"/request_id": "R-100", "/status": "pending"}


@pytest.mark.parametrize("kind", ("ineligible", "noise", "unknown"))
def test_instruction_state_executor_rejects_non_generatable_frame(
    instruction_program, declared_program, kind,
):
    """StateExecutor 独立拒绝注册但不可生成、noise 与未知 frame class。"""
    program = instruction_program
    frames = dict(program.frame_classes)
    if kind == "ineligible":
        frames["reference"] = replace(
            frames["task_request"], gen_instruction=None, gen_schema=None,
        )
        invalid = "reference"
    elif kind == "noise":
        frames["noise"] = declared_program.frame_classes["noise"]
        program = replace(program, noise=declared_program.noise)
        invalid = "noise"
    else:
        invalid = "unknown"
    program = _rehash(replace(program, frame_classes=frames))
    plan = compile_scenario_plan(program)
    source = declared_program.class_views["ticket_booking"].sequence_generation
    seed = ScenarioSeed(**dict(source.initial_state_catalog[0]))
    context = EventExecutionContext(
        program, plan, plan.delivery_slots[0], None, 0, seed, seed.initial_state, (),
    )
    patch = (
        {"op": "test", "path": "/hidden_sentinel", "value": "catalog-secret-a"},
        {"op": "replace", "path": "/hidden_sentinel", "value": "catalog-secret-a"},
    )
    valid = {"frame_class": "task_request", "actor": "requester",
             "intent": "continue", "patch": patch}
    assert post_validate_event_plan(valid, context).violations == ()
    invalid_candidate = {**valid, "frame_class": invalid}
    assert post_validate_event_plan(invalid_candidate, context).violations == (
        "event_identity",
    )


def test_missing_binding_leaf_is_a_repairable_post_validation_violation(declared_program):
    """权限覆盖但运行态缺失的 binding 叶子在 EventExecution 前进入 L3。"""
    pattern_name = next(iter(declared_program.patterns))
    pattern = declared_program.patterns[pattern_name]
    role = pattern.roles[0]
    missing = replace(role.payload_bindings[0], state_path="/request/missing_leaf")
    changed_role = replace(role, payload_bindings=(missing, *role.payload_bindings[1:]))
    changed_pattern = replace(pattern, roles=(changed_role, *pattern.roles[1:]))
    patterns = dict(declared_program.patterns)
    patterns[pattern_name] = changed_pattern
    program = _rehash(replace(declared_program, patterns=patterns))
    plan = compile_scenario_plan(program)
    context = _context(program, plan)
    patch = (
        {"op": "test", "path": "/request/status", "value": "new"},
        {"op": "replace", "path": "/request/status", "value": "pending"},
    )
    result = post_validate_event_plan(_request_candidate(patch), context)
    assert result.violations == ("payload_binding",)
    assert result.event_execution is None


def test_token_prefix_permission_rejects_hidden_and_sibling_paths(declared_program):
    plan = compile_scenario_plan(declared_program)
    context = _context(declared_program, plan)
    for path in ("/hidden_sentinel", "/requester"):
        candidate = _request_candidate((
            {"op": "test", "path": "/request/status", "value": "new"},
            {"op": "replace", "path": path, "value": "tampered"},
        ))
        result = post_validate_event_plan(candidate, context)
        assert result.violations == ("patch_permission",)
        assert result.event_execution is None
        assert context.current_state["hidden_sentinel"] == "catalog-secret-a"


def test_state_hook_violations_are_preserved_verbatim(declared_program):
    def reject_transition(_value):
        return ["first violation", "second violation"]

    hook = ResolvedHook("offline.py:reject_transition", reject_transition)
    program = _rehash(replace(declared_program, state_validator=hook))
    plan = compile_scenario_plan(program)
    context = _context(program, plan)
    candidate = _request_candidate((
        {"op": "test", "path": "/request/status", "value": "new"},
        {"op": "replace", "path": "/request/status", "value": "pending"},
    ))
    result = post_validate_event_plan(candidate, context)
    assert result.violations == ("first violation", "second violation")
    assert result.event_execution is None


def test_rfc6902_test_add_replace_remove_succeeds_atomically(declared_program):
    plan = compile_scenario_plan(declared_program)
    context = _ack_context(declared_program, plan)
    candidate = {
        "frame_class": "acknowledgement",
        "actor": "system",
        "intent": "acknowledge request",
        "patch": (
            {"op": "test", "path": "/request/id", "value": "R-100"},
            {"op": "remove", "path": "/request/acknowledged"},
            {"op": "add", "path": "/request/acknowledged", "value": True},
            {"op": "replace", "path": "/request/status", "value": "acknowledged"},
        ),
    }
    result = post_validate_event_plan(candidate, context)
    assert result.violations == ()
    assert result.event_execution.state_after["request"]["acknowledged"] is True
    assert result.event_execution.state_after["request"]["status"] == "acknowledged"
    view = build_actor_view(context)
    assert view.observations[0]["source_event_key"] == context.history[0].event_key
    assert view.observations[0]["values"] == context.history[0].publish_snapshot


def test_move_copy_test_order_and_failed_test_are_rejected_without_mutation(declared_program):
    plan = compile_scenario_plan(declared_program)
    context = _context(declared_program, plan)
    candidates = (
        (({"op": "move", "from": "/request/id", "path": "/request/status"},),
         "patch_operation"),
        (({"op": "copy", "from": "/request/id", "path": "/request/status"},),
         "patch_operation"),
        (({"op": "replace", "path": "/request/status", "value": "pending"},
          {"op": "test", "path": "/request/status", "value": "pending"}),
         "patch_test_order"),
        (({"op": "test", "path": "/request/status", "value": "wrong"},
          {"op": "replace", "path": "/request/status", "value": "pending"}),
         "json_patch"),
        (({"op": "replace", "path": "/request/status", "value": "pending"},),
         "patch_test_prefix"),
        (({"op": "test", "path": "/request/status", "value": "new"},),
         "patch_test_prefix"),
    )
    before = canonical_json(context.current_state)
    for patch, expected in candidates:
        result = post_validate_event_plan(_request_candidate(patch), context)
        assert result.violations == (expected,)
        assert result.event_execution is None
        assert canonical_json(context.current_state) == before


def test_multiple_prefix_tests_are_allowed_before_first_mutation(declared_program):
    plan = compile_scenario_plan(declared_program)
    context = _context(declared_program, plan)
    patch = (
        {"op": "test", "path": "/request/status", "value": "new"},
        {"op": "test", "path": "/request/id", "value": "R-100"},
        {"op": "replace", "path": "/request/status", "value": "pending"},
    )
    result = post_validate_event_plan(_request_candidate(patch), context)
    assert result.violations == ()
    assert result.event_execution.state_after["request"]["status"] == "pending"


def test_patch_without_test_is_rejected_with_stable_violation(declared_program):
    plan = compile_scenario_plan(declared_program)
    context = _context(declared_program, plan)
    result = post_validate_event_plan(_request_candidate((
        {"op": "add", "path": "/request/status", "value": "pending"},
        {"op": "replace", "path": "/request/status", "value": "pending"},
    )), context)
    assert result.violations == ("patch_test_prefix",)
    assert result.event_execution is None


@pytest.mark.parametrize(("invalid_hook", "error", "message"), (
    (lambda _value: (_ for _ in ()).throw(ValueError("private data")),
     RuntimeError, "state validator failed"),
    (lambda _value: [1], PostValidatorInvalidError, "^$"),
))
def test_state_hook_exception_and_invalid_return_are_terminal(
    declared_program, invalid_hook, error, message,
):
    hook = ResolvedHook("offline.py:invalid_state", invalid_hook)
    program = _rehash(replace(declared_program, state_validator=hook))
    plan = compile_scenario_plan(program)
    context = _context(program, plan)
    patch = (
        {"op": "test", "path": "/request/status", "value": "new"},
        {"op": "replace", "path": "/request/status", "value": "pending"},
    )
    with pytest.raises(error, match=message):
        post_validate_event_plan(_request_candidate(patch), context)


def test_pre_state_base_state_and_positive_outcome_schemas_are_independent(declared_program):
    plan = compile_scenario_plan(declared_program)
    context = _context(declared_program, plan)
    wrong = json.loads(canonical_json(context.current_state))
    wrong["request"]["status"] = "pending"
    pre_result = post_validate_event_plan(
        _request_candidate((
            {"op": "test", "path": "/request/status", "value": "pending"},
            {"op": "replace", "path": "/request/status", "value": "pending"},
        )),
        replace(context, current_state=wrong),
    )
    assert pre_result.violations == ("pre_state_schema:/request/status:const",)
    base_result = post_validate_event_plan(_request_candidate((
        {"op": "test", "path": "/request/status", "value": "new"},
        {"op": "replace", "path": "/request/status", "value": "invalid"},
    )), context)
    assert base_result.violations == ("state_schema:/request/status:enum",)
    pattern = declared_program.patterns[context.slot.pattern_name]
    variant = declared_program.counterfactual_sets[0].variants[0]
    evaluation = evaluate_state(StateEvaluationRequest(
        declared_program, context.slot, pattern, variant, context.scenario_seed,
        (), (), context.scenario_seed.initial_state,
    ))
    assert evaluation.replay_hash == evaluation.final_state_hash
    assert not evaluation.outcome_valid


def test_state_schema_violations_are_complete_sorted_deduplicated_and_value_free(
    declared_program,
):
    schema = {
        "type": "object",
        "allOf": [
            {"properties": {"request": {"properties": {
                "status": {"enum": ["expected-secret"]},
                "id": {"type": "integer"},
            }}}},
            {"properties": {"request": {"properties": {
                "status": {"enum": ["expected-secret"]},
            }}}},
        ],
    }
    class_view = declared_program.class_views["ticket_booking"]
    generation = replace(class_view.sequence_generation, state_schema=schema)
    program = _rehash(replace(
        declared_program,
        class_views={**declared_program.class_views,
                     "ticket_booking": replace(class_view, sequence_generation=generation)},
    ))
    context = _context(program, compile_scenario_plan(program))
    result = post_validate_event_plan(_request_candidate((
        {"op": "test", "path": "/request/status", "value": "new"},
        {"op": "replace", "path": "/request/status", "value": "actual-secret"},
    )), context)
    assert result.violations == (
        "state_schema:/request/id:type",
        "state_schema:/request/status:enum",
    )
    rendered = "\n".join(result.violations)
    assert "expected-secret" not in rendered
    assert "actual-secret" not in rendered
    assert "R-100" not in rendered


def test_state_schema_violations_mask_dynamic_keys_and_array_indexes(declared_program):
    schema = {
        "type": "object",
        "patternProperties": {"^catalog-secret-root$": {"type": "integer"}},
        "allOf": [{"properties": {"a/b": {"type": "object", "properties": {
            "~key": {"type": "integer"},
        }}}}],
        "properties": {
            "public": {"type": "object", "patternProperties": {
                ".*": {"type": "integer"},
            }},
            "goal": {"type": "object", "additionalProperties": {"type": "integer"}},
            "audit": {"type": "array", "items": {"type": "integer"}},
        },
    }
    class_view = declared_program.class_views["ticket_booking"]
    generation = replace(class_view.sequence_generation, state_schema=schema)
    program = _rehash(replace(
        declared_program,
        class_views={**declared_program.class_views,
                     "ticket_booking": replace(class_view, sequence_generation=generation)},
    ))
    context = _context(program, compile_scenario_plan(program))
    current = json.loads(canonical_json(context.current_state))
    current["public"] = {"catalog-secret-key": "catalog-secret-value"}
    current["goal"]["catalog-secret-extra"] = "catalog-secret-goal"
    current["audit"] = ["catalog-secret-array"]
    current["catalog-secret-root"] = "catalog-secret-root-value"
    current["a/b"] = {"~key": "catalog-secret-escaped"}
    result = post_validate_event_plan(_request_candidate((
        {"op": "test", "path": "/request/status", "value": "new"},
        {"op": "replace", "path": "/request/status", "value": "pending"},
    )), replace(context, current_state=current))
    assert result.violations == tuple(sorted({
        "state_schema::type",
        "state_schema:/a~1b/~0key:type",
        "state_schema:/audit:type",
        "state_schema:/goal:type",
        "state_schema:/public:type",
    }))
    assert "catalog-secret" not in "\n".join(result.violations)
    assert "/0" not in "\n".join(result.violations)


def test_final_declared_event_repairs_hidden_baseline_and_variant_outcomes(declared_program):
    plan = compile_scenario_plan(declared_program)
    context = _confirm_context(declared_program, plan)
    blocked = {
        "frame_class": "confirmation",
        "actor": "system",
        "intent": "block request",
        "patch": (
            {"op": "test", "path": "/request/acknowledged", "value": True},
            {"op": "replace", "path": "/request/status", "value": "blocked"},
            {"op": "replace", "path": "/ticket/id", "value": None},
            {"op": "replace", "path": "/ticket/status", "value": "not_issued"},
        ),
    }
    baseline = post_validate_event_plan(blocked, context)
    assert baseline.violations == (
        "outcome_schema:/request/status:const",
        "outcome_schema:/ticket/id:type",
        "outcome_schema:/ticket/status:const",
    )
    ticketed = {
        **blocked,
        "intent": "issue ticket",
        "patch": (
            {"op": "test", "path": "/request/acknowledged", "value": True},
            {"op": "replace", "path": "/request/status", "value": "ticketed"},
            {"op": "replace", "path": "/ticket/id", "value": "T-100"},
            {"op": "replace", "path": "/ticket/status", "value": "issued"},
        ),
    }
    timeout = post_validate_event_plan(
        ticketed,
        replace(context, variant_name="confirmation_timeout"),
    )
    assert timeout.violations == (
        "outcome_schema:/request/status:const",
        "outcome_schema:/sla/expired:const",
        "outcome_schema:/ticket/id:type",
        "outcome_schema:/ticket/status:const",
    )


def test_hidden_baseline_without_optional_positive_has_no_outcome_schema(declared_program):
    source = declared_program.counterfactual_sets[0]
    without_positive = replace(
        source,
        variants=tuple(item for item in source.variants if item.kind != "positive"),
    )
    timeline = replace(declared_program.timeline, duplicate_sequences=0)
    program = _rehash(replace(
        declared_program, counterfactual_sets=(without_positive,), timeline=timeline,
    ))
    plan = compile_scenario_plan(program)
    context = _confirm_context(program, plan)
    assert outcome_schema_for(context) is None


def test_event_patch_byte_limit_accepts_exact_boundary(declared_program):
    plan = compile_scenario_plan(declared_program)
    context = _context(declared_program, plan)
    patch = (
        {"op": "test", "path": "/request/status", "value": "new"},
        {"op": "replace", "path": "/request/status", "value": "pending"},
    )
    size = len(canonical_json(patch).encode("utf-8"))
    exact = replace(
        declared_program,
        limits=replace(declared_program.limits, event_patch_bytes=size),
    )
    assert not post_validate_event_plan(_request_candidate(patch), replace(context, program=exact)).violations
    below = replace(exact, limits=replace(exact.limits, event_patch_bytes=size - 1))
    result = post_validate_event_plan(_request_candidate(patch), replace(context, program=below))
    assert result.violations == ("event_patch_size",)

"""slot 高层生成、instruction-only 保证与 noise 原语的离线测试。"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import jsonpatch
from jsonschema import Draft202012Validator

from labelkit.cli.parser import CliOverrides
from labelkit.common.config import load
from labelkit.common.contracts.generation import (
    EventDraft,
    EventExecutionContext,
    GenerationServices,
    NoiseEvaluationRequest,
    NoiseRenderRequest,
    PatternEvaluation,
    ProjectionRequest,
    ScenarioSeed,
    ScenarioSeedRequest,
    StateEvaluationRequest,
)
from labelkit.common.errors import ContextOverflowError, InternalError, SchemaViolation
from labelkit.common.extensions.hooks import ResolvedHook
from labelkit.common.inference.schema_engine import event_plan_schema
from labelkit.operators.generation import GenerationAttemptRejected
from labelkit.operators.generation.evaluate import evaluate_state
from labelkit.operators.generation.evaluate import evaluate_noise
from labelkit.operators.generation.planner import compile_scenario_plan
from labelkit.operators.generation.program import generation_program_digest
from labelkit.operators.generation.project import (
    canonical_json,
    derive_generation_id,
    project_trace,
    scenario_plan_digest,
)
from labelkit.operators.generation.render import render_noise
from labelkit.orchestration.sequence_workflow import estimate_sequence_products
from labelkit.operators.generation.scenario import (
    build_event_plan_request,
    generate_scenario_seed,
    generate_slot_traces,
    plan_event,
)
from labelkit.operators.generation import scenario as scenario_module


_CATALOG = Path(__file__).resolve().parents[3] / "examples/sequence-generation/catalogs/ticket-booking.jsonl"


def test_hidden_baseline_world_domain_cannot_collide_with_user_variant(declared_program):
    """用户 variant 名为旧 sentinel 时仍与不可见 baseline 使用不同 hash 域。"""
    plan = compile_scenario_plan(declared_program)
    slot = plan.delivery_slots[0]
    scenario_id = derive_generation_id(
        "declared_scenario_id",
        [declared_program.digest, slot.source_name, slot.scenario_index],
    )
    hidden = scenario_module._world_branch_id(declared_program, slot, None)
    visible = scenario_module._world_branch_id(declared_program, slot, "__baseline__")
    assert hidden == derive_generation_id(
        "declared_hidden_baseline_world_branch_id", [scenario_id]
    )
    assert visible == derive_generation_id(
        "declared_world_branch_id", [scenario_id, "__baseline__"]
    )
    assert hidden != visible


class _Metrics:
    """仅记录 generation family 逻辑入口计数。"""

    def __init__(self):
        self.counters = {}

    def count(self, key: str, n: int = 1):
        self.counters[key] = self.counters.get(key, 0) + n


class _OfflineSchemaEngine:
    """直接返回合约对象的离线 SchemaEngine 替身。"""

    def __init__(self):
        self.plan_requests = []
        self.validated_calls = []
        self.seed = json.loads(_CATALOG.read_text(encoding="utf-8").splitlines()[0])

    def validate_only(self, value, *, schema):
        return [error.message for error in Draft202012Validator(schema).iter_errors(value)]

    async def complete_validated(self, _profile, prompt, *, schema, scope):
        del scope
        self.validated_calls.append((_profile, prompt, schema))
        return (self._value_for_schema(schema),)

    async def complete_finalized(self, request):
        """用 model Schema 生成，再执行生产 finalizer 的离线替身。"""
        self.validated_calls.append((request.profile, request.prompt, request.model_schema))
        candidate = self._value_for_schema(request.model_schema)
        candidate = self._apply_state_bindings(candidate, request.prompt)
        value = request.candidate_finalizer(candidate)
        schema = json.loads(canonical_json(request.final_schema))
        errors = tuple(Draft202012Validator(schema).iter_errors(value))
        if errors:
            raise AssertionError(
                "offline finalized candidate failed full schema: "
                + "; ".join(error.message for error in errors)
            )
        return value, None, 1, "offline"

    @staticmethod
    def _apply_state_bindings(candidate, prompt):
        """让离线候选遵循 prompt 显式给出的非时间状态绑定。"""
        user = _user_text(SimpleNamespace(prompt=prompt))
        marker = "[非时间状态绑定值]\n"
        if marker not in user:
            return candidate
        bindings = json.loads(user.split(marker, 1)[1].splitlines()[0])
        value = candidate
        for binding in bindings:
            value = jsonpatch.apply_patch(value, [{
                "op": "add", "path": binding["payload_path"], "value": binding["value"],
            }], in_place=False)
        return value

    def _value_for_schema(self, schema):
        """按请求 Schema 形状生成一个离线候选。"""
        properties = schema.get("properties", {})
        if "initial_state" in properties:
            return self.seed
        if "causal_consistency" in properties:
            return {
                "causal_consistency": True,
                "actor_knowledge": True,
                "goal_consistency": True,
                "temporal_plausibility": True,
                "cross_frame_consistency": True,
                "realism": True,
                "reason_codes": [],
            }
        if "unrelated_to_declared_tasks" in properties:
            return {
                "unrelated_to_declared_tasks": True,
                "no_executable_task": True,
                "realism": True,
                "matches_planned_topic": True,
                "reason_codes": [],
            }
        if "utterance" in properties and "request_id" in properties:
            status = properties["status"]
            if status.get("const") == "pending":
                value = {"utterance": "请帮我订票", "request_id": "R-100", "status": "pending"}
            elif "acknowledged" in status.get("enum", ()):
                value = {
                    "utterance": "已收到 R-100 订票请求",
                    "request_id": "R-100",
                    "status": "acknowledged",
                }
            else:
                value = {
                    "utterance": "未能出票", "request_id": "R-100",
                    "ticket_id": None, "status": "blocked",
                }
            return value
        if "utterance" in properties:
            return {"utterance": "今天的云很好看"}
        raise AssertionError("unexpected schema")

    async def complete_post_validated(self, request):
        self.plan_requests.append(request)
        user = _user_text(request)
        candidates = self._plan_candidates(user)
        for candidate in candidates:
            checked = request.post_validator(candidate)
            if not checked.violations:
                return SimpleNamespace(object=candidate, event_execution=checked.event_execution)
        raise AssertionError("offline event candidates did not satisfy state contract")

    def _plan_candidates(self, user):
        """按 prompt-safe role/wait 生成可由后置验证器裁决的候选。"""
        if "mode=instruction_only" in user:
            return [self._instruction_candidate()]
        role = next(line.removeprefix("role=") for line in user.splitlines()
                    if line.startswith("role="))
        wait = int(next(line.removeprefix("wait_since_previous_us=")
                        for line in user.splitlines()
                        if line.startswith("wait_since_previous_us=")))
        if role == "request":
            return [self._request_candidate()]
        if role == "acknowledge":
            return [self._ack_candidate(False), self._ack_candidate(True)]
        if wait > 1_200_000_000:
            return [self._confirm_candidate("expired")]
        return [self._confirm_candidate("ticketed"), self._confirm_candidate("blocked")]

    @staticmethod
    def _instruction_candidate():
        """返回 instruction-only 的无状态变化候选。"""
        return {
            "frame_class": "task_request",
            "actor": "requester",
            "intent": "continue the booking conversation",
            "patch": (
                {"op": "test", "path": "/hidden_sentinel", "value": "catalog-secret-a"},
                {"op": "replace", "path": "/hidden_sentinel", "value": "catalog-secret-a"},
            ),
        }

    @staticmethod
    def _request_candidate():
        """返回 request role 候选。"""
        return {
            "frame_class": "task_request", "actor": "requester", "intent": "submit request",
            "patch": (
                {"op": "test", "path": "/request/status", "value": "new"},
                {"op": "replace", "path": "/request/status", "value": "pending"},
            ),
        }

    @staticmethod
    def _ack_candidate(late: bool):
        """返回正常或晚到的 acknowledgement 候选。"""
        before = "blocked" if late else "pending"
        patch = [
            {"op": "test", "path": "/request/status", "value": before},
            {"op": "replace", "path": "/request/acknowledged", "value": True},
        ]
        if not late:
            patch.append({
                "op": "replace", "path": "/request/status", "value": "acknowledged",
            })
        return {
            "frame_class": "acknowledgement", "actor": "system", "intent": "acknowledge",
            "patch": tuple(patch),
        }

    @staticmethod
    def _confirm_candidate(outcome: str):
        """返回出票、阻断或超时 confirmation 候选。"""
        acknowledged = outcome != "blocked"
        ticket_id = "T-100" if outcome == "ticketed" else None
        ticket_status = "issued" if outcome == "ticketed" else "not_issued"
        patch = [
            {"op": "test", "path": "/request/acknowledged", "value": acknowledged},
            {"op": "replace", "path": "/request/status", "value": outcome},
            {"op": "replace", "path": "/ticket/id", "value": ticket_id},
            {"op": "replace", "path": "/ticket/status", "value": ticket_status},
        ]
        if outcome == "expired":
            patch.append({"op": "replace", "path": "/sla/expired", "value": True})
        return {
            "frame_class": "confirmation", "actor": "system",
            "intent": f"confirm {outcome}", "patch": tuple(patch),
        }


class _InlineTaskExecutor:
    """按声明序执行冻结叶任务并返回输入序结果。"""

    async def run_group(self, request):
        return tuple([await task.operation() for task in request.tasks])


def _services(config):
    """构造不含网络调用的真实 GenerationServices carrier。"""
    engine, metrics = _OfflineSchemaEngine(), _Metrics()
    services = GenerationServices(
        config,
        engine,
        object(),
        metrics,
        _InlineTaskExecutor(),
    )
    return services, engine, metrics


def _user_text(request) -> str:
    """读取一次 EventPlan request 的 user prompt。"""
    return "".join(part.text or "" for part in request.prompt.messages[1].parts)


def _branch(plan, slot_key: str, variant_name):
    """读取一个唯一 planner branch。"""
    return next(block[(slot_key, variant_name)] for block in plan.blocks
                if (slot_key, variant_name) in block)


def _declared_context(program, plan):
    """构造首个 declared event 的唯一执行根。"""
    slot = plan.delivery_slots[0]
    source = program.class_views[slot.sequence_class].sequence_generation
    seed = ScenarioSeed(**dict(source.initial_state_catalog[slot.catalog_row_index]))
    return EventExecutionContext(program, plan, slot, None, 0, seed, seed.initial_state, ())


def _tampered_context(program, plan, kind: str):
    """构造摘要自洽或根摘要损坏的直接事件入口上下文。"""
    if kind == "program_digest":
        return _declared_context(replace(program, digest="0" * 64), plan)
    if kind == "plan_digest":
        return _declared_context(program, replace(plan, digest="0" * 64))
    blocks = [dict(block) for block in plan.blocks]
    slot = plan.delivery_slots[0]
    key = (slot.slot_key, None)
    events = list(next(block[key] for block in blocks if key in block))
    if kind == "logical_time":
        events[0] = replace(events[0], logical_time_us=events[0].logical_time_us + 999)
    elif kind == "timestamp":
        events[0] = replace(events[0], timestamp_us=events[0].timestamp_us + 999)
    else:
        events[0] = replace(events[0], session_id="forged-session")
    next(block for block in blocks if key in block)[key] = tuple(events)
    forged = replace(plan, blocks=tuple(blocks), digest="")
    forged = replace(forged, digest=scenario_plan_digest(forged))
    return _declared_context(program, forged)


def _draft(event):
    """从已绑定 EventTruth 移除 role，返回生成期 EventDraft。"""
    return EventDraft(
        event.event_key, event.event_id, event.frame_class, event.actor,
        event.logical_time_us, event.timestamp_us, event.duration_us,
        event.actor_view, event.intent,
        event.patch, event.state_before_hash, event.state_after_hash,
        event.publish_snapshot, event.payload,
    )


@pytest.mark.parametrize(
    "kind", ("program_digest", "plan_digest", "logical_time", "timestamp", "session"),
)
def test_public_request_builder_rejects_every_forged_root(declared_program, kind):
    """公开 prompt 投影入口必须先完整验证 program/plan 根。"""
    plan = compile_scenario_plan(declared_program)
    context = _tampered_context(declared_program, plan, kind)
    with pytest.raises(InternalError):
        build_event_plan_request(context, 0, "nonce")


@pytest.mark.parametrize(
    "kind", ("program_digest", "plan_digest", "logical_time", "timestamp", "session"),
)
def test_public_plan_event_rejects_every_forged_root_without_llm(
    declared_config, declared_program, kind,
):
    """公开事件规划入口必须在任何逻辑调用前拒绝伪造根。"""
    plan = compile_scenario_plan(declared_program)
    context = _tampered_context(declared_program, plan, kind)
    services, engine, metrics = _services(declared_config)
    with pytest.raises(InternalError):
        asyncio.run(plan_event(context, 0, "nonce", services))
    assert engine.plan_requests == [] and engine.validated_calls == []
    assert metrics.counters == {}


def test_instruction_only_high_level_api_has_frozen_shape_and_prompt(instruction_config,
                                                                     instruction_program):
    plan = compile_scenario_plan(instruction_program)
    services, engine, metrics = _services(instruction_config)
    traces = asyncio.run(generate_slot_traces(
        instruction_program, plan, plan.delivery_slots[0], 0, services,
    ))
    assert len(traces) == 1
    trace = traces[0]
    assert trace.pattern_name is None and trace.variant_name is None
    assert trace.pattern_evaluation is None
    assert [event.role for event in trace.events] == [
        f"position_{index:03d}" for index in range(len(trace.events))
    ]
    assert all(event.frame_class == "task_request" and event.actor == "requester"
               for event in trace.events)
    for request in engine.plan_requests:
        user = _user_text(request)
        assert "timestamp_us" not in user and "session_id" not in user
        assert all(event.event_id not in user for event in trace.events)
        assert "expected_violation" not in user and "actual_violations" not in user
        assert "variant=" not in user and "target=" not in user
    frame_prompts = [
        "".join(part.text or "" for part in prompt.messages[1].parts)
        for _profile, prompt, schema in engine.validated_calls
        if "utterance" in schema.get("properties", {})
    ]
    assert len(frame_prompts) == len(trace.events)
    assert '"intent":"continue the booking conversation"' in frame_prompts[1]
    assert '"utterance":"请帮我订票"' in frame_prompts[1]
    assert trace.events[0].event_id not in frame_prompts[1]
    assert str(trace.events[0].timestamp_us) not in frame_prompts[1]
    assert trace.events[0].actor_view.observations == ()
    witness = trace.events[1].actor_view.observations[0]
    assert set(witness) == {
        "event_key", "logical_time_us", "frame_class", "actor", "intent", "patch",
        "state_before_hash", "state_after_hash", "publish_snapshot", "payload",
    }
    assert "actor_view" not in witness
    assert canonical_json(witness) in _user_text(engine.plan_requests[1])
    prefix = "generate.sequence.calls."
    assert metrics.counters == {
        prefix + "scenario_seed_calls": 1,
        prefix + "baseline_event_plan_calls": len(trace.events),
        prefix + "frame_render_calls": len(trace.events),
        prefix + "semantic_evaluation_calls": 1,
    }
    slot = plan.delivery_slots[0]
    projection = project_trace(ProjectionRequest(
        instruction_program, plan, slot, trace
    ))
    truth = projection.main_record.raw["_meta"]["generation"]
    assert set(truth) == {
        "validation_mode", "actor_knowledge_validation", "instruction_slot",
        "scenario_index", "scenario_id", "world_branch_id", "sequence_class",
    }
    assert truth["validation_mode"] == "instruction_only"
    assert all(key not in truth for key in (
        "pattern", "variant", "expected_violation", "actual_violations",
    ))


def test_high_level_slot_generation_uses_program_seed_and_limits(
    monkeypatch, instruction_config, instruction_program,
):
    """编译后污染 ResolvedConfig 不能改变 attempt seed 或请求预算。"""
    poison_seed = instruction_program.planner_seed + 10_000
    poison_limits = replace(
        instruction_config.sequence_generation.limits,
        prompt_value_bytes=1,
        scenario_seed_bytes=1,
    )
    poisoned = replace(
        instruction_config,
        run=replace(instruction_config.run, seed=poison_seed),
        sequence_generation=replace(
            instruction_config.sequence_generation, limits=poison_limits,
        ),
    )
    services, _engine, _metrics = _services(poisoned)
    captured = []
    original = scenario_module.generate_scenario_seed

    async def capture(request, generation_services):
        captured.append(request)
        return await original(request, generation_services)

    monkeypatch.setattr(scenario_module, "generate_scenario_seed", capture)
    plan = compile_scenario_plan(instruction_program)
    traces = asyncio.run(generate_slot_traces(
        instruction_program, plan, plan.delivery_slots[0], 0, services,
    ))
    expected = scenario_module._attempt_random(
        instruction_program.planner_seed,
        plan.delivery_slots[0].slot_key,
        0,
        "scenario_seed",
    )
    assert traces and captured[0].random_seed == expected
    assert captured[0].program.limits == instruction_program.limits
    assert services.config.run.seed == poison_seed
    assert services.config.sequence_generation.limits == poison_limits


def test_instruction_only_maximum_8_event_trace_is_complete(instruction_config):
    """8-event 上限经过真实 planner 与高层生成入口后仍为完整 EventTrace。"""
    sequence = instruction_config.sequence_generation
    source = replace(sequence.instruction_only[0], len_range=(8, 8))
    timeline = replace(sequence.timeline, session_max_events=8)
    config = replace(
        instruction_config,
        sequence_generation=replace(
            sequence,
            instruction_only=(source,),
            timeline=timeline,
        ),
    )
    from labelkit.operators.generation.program import compile_generation_program

    program = compile_generation_program(config)
    plan = compile_scenario_plan(program)
    services, engine, metrics = _services(config)
    traces = asyncio.run(generate_slot_traces(
        program, plan, plan.delivery_slots[0], 0, services,
    ))
    assert len(traces) == 1 and len(traces[0].events) == 8
    assert len(engine.plan_requests) == 8
    assert all(event.payload for event in traces[0].events)
    assert project_trace(ProjectionRequest(
        program, plan, plan.delivery_slots[0], traces[0],
    )).main_record.members
    forged = replace(traces[0], pattern_evaluation=PatternEvaluation({}, ()))
    with pytest.raises(InternalError, match="pattern evaluation"):
        project_trace(ProjectionRequest(
            program, plan, plan.delivery_slots[0], forged,
        ))
    assert metrics.counters["generate.sequence.calls.baseline_event_plan_calls"] == 8
    assert metrics.counters["generate.sequence.calls.frame_render_calls"] == 8


@pytest.mark.parametrize("kind", ("ineligible", "noise", "unknown"))
def test_instruction_projector_rejects_non_generatable_frame_at_boundary(
    instruction_config, instruction_program, declared_program, kind,
):
    """instruction trace 即使已通过 gate，也只能投影 program 的可生成非 noise 帧。"""
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
    program = replace(program, frame_classes=frames, digest="")
    program = replace(program, digest=generation_program_digest(program))
    plan = compile_scenario_plan(program)
    services, _engine, _metrics = _services(instruction_config)
    trace = asyncio.run(generate_slot_traces(
        program, plan, plan.delivery_slots[0], 0, services,
    ))[0]
    assert project_trace(ProjectionRequest(
        program, plan, plan.delivery_slots[0], trace,
    )).main_record.members
    events = list(trace.events)
    events[0] = replace(events[0], frame_class=invalid)
    with pytest.raises(InternalError, match="frame class"):
        project_trace(ProjectionRequest(
            program, plan, plan.delivery_slots[0], replace(trace, events=tuple(events)),
        ))


def test_instruction_event_request_is_exact_projection(instruction_config,
                                                        instruction_program):
    plan = compile_scenario_plan(instruction_program)
    slot = plan.delivery_slots[0]
    services, engine, _metrics = _services(instruction_config)
    seed = ScenarioSeed(**engine.seed)
    context = EventExecutionContext(
        instruction_program, plan, slot, None, 0, seed, seed.initial_state, (),
    )
    request = build_event_plan_request(context, 3, "instruction-nonce")
    source = instruction_program.instruction_only[0]
    planned = _branch(plan, slot.slot_key, None)
    expected_frames = tuple(
        name for name in instruction_program.frame_classes
        if instruction_program.noise is None or name != instruction_program.noise.frame_class
    )
    assert tuple(field.name for field in fields(request)) == (
        "mode", "semantic_profile", "slot_key", "planned_event", "role",
        "generation_instruction", "sequence_length", "eligible_frame_classes",
        "eligible_actors", "actor_view", "visible_state", "state_schema", "outcome_schema",
        "history", "actor_profiles", "public_facts", "attempt_index", "variation_nonce",
    )
    assert request.mode == "instruction_only"
    assert request.semantic_profile == instruction_program.semantic_profile
    assert request.slot_key == slot.slot_key and request.planned_event == planned[0]
    assert request.role is None and request.generation_instruction == source.instruction
    assert request.sequence_length == len(planned)
    assert tuple(request.eligible_frame_classes) == expected_frames
    assert request.eligible_actors == tuple(seed.actors)
    assert request.actor_view is None
    assert canonical_json(request.visible_state) == canonical_json(seed.initial_state)
    assert canonical_json(request.state_schema) == canonical_json(source.state_schema)
    assert request.outcome_schema is None
    assert request.history == ()
    assert canonical_json(request.actor_profiles) == canonical_json(seed.actors)
    assert canonical_json(request.public_facts) == canonical_json(seed.shared_facts["public"])
    assert request.attempt_index == 3 and request.variation_nonce == "instruction-nonce"
    user = "".join(
        part.text or "" for part in scenario_module._event_prompt(
            request, instruction_program.limits.prompt_value_bytes
        ).messages[1].parts
    )
    assert f"[完整状态 Schema]\n{canonical_json(source.state_schema)}" in user
    assert "[末事件 Outcome Schema]\nnull" in user
    assert "\n\n[ActorView]" in user and "\n\n\n[ActorView]" not in user


def test_instruction_event_closed_set_excludes_non_generatable_frame(
    instruction_program,
):
    """普通帧可留在 registry，但不能进入 EventPlan 的生成闭集。"""
    ordinary = replace(
        instruction_program.frame_classes["task_request"],
        description="仅供既有数据分类的参考帧",
        gen_instruction=None,
        gen_schema=None,
    )
    program = replace(
        instruction_program,
        frame_classes={**dict(instruction_program.frame_classes), "reference": ordinary},
        digest="",
    )
    program = replace(program, digest=generation_program_digest(program))
    plan = compile_scenario_plan(program)
    raw = json.loads(_CATALOG.read_text(encoding="utf-8").splitlines()[0])
    seed = ScenarioSeed(**raw)
    context = EventExecutionContext(
        program, plan, plan.delivery_slots[0], None, 0, seed, seed.initial_state, (),
    )
    request = build_event_plan_request(context, 0, "nonce")
    assert "reference" not in request.eligible_frame_classes
    assert "reference" not in _user_text(SimpleNamespace(
        prompt=scenario_module._event_prompt(
            request, instruction_program.limits.prompt_value_bytes
        ),
    ))
    schema = event_plan_schema(tuple(request.eligible_frame_classes), request.eligible_actors)
    invalid = {
        "frame_class": "reference", "actor": request.eligible_actors[0],
        "intent": "use an ineligible frame",
        "patch": [
            {"op": "test", "path": "/hidden_sentinel", "value": "catalog-secret-a"},
            {"op": "replace", "path": "/hidden_sentinel", "value": "catalog-secret-a"},
        ],
    }
    assert not Draft202012Validator(schema).is_valid(invalid)


def test_instruction_event_prompt_value_limit_rejects_before_provider(
    instruction_config,
    instruction_program,
):
    """动态 actor 闭集 D+1 在 EventPlan 派发前终止。"""
    plan = compile_scenario_plan(instruction_program)
    slot = plan.delivery_slots[0]
    raw = json.loads(_CATALOG.read_text(encoding="utf-8").splitlines()[0])
    profile = raw["actors"]["requester"]
    limit = instruction_program.limits.prompt_value_bytes
    actor = "a" * (limit + 1)
    seed = ScenarioSeed(
        raw["initial_state"], {actor: profile}, raw["shared_facts"],
        raw["style"], raw["time_context"],
    )
    context = EventExecutionContext(
        instruction_program, plan, slot, None, 0, seed, seed.initial_state, (),
    )
    services, engine, _metrics = _services(instruction_config)
    with pytest.raises(ContextOverflowError):
        asyncio.run(plan_event(context, 0, "nonce", services))
    assert engine.plan_requests == []


def test_invalid_slot_is_zero_llm_and_terminal(instruction_config, instruction_program):
    plan = compile_scenario_plan(instruction_program)
    services, engine, metrics = _services(instruction_config)
    invalid = replace(plan.delivery_slots[0], slot_key="foreign/000000")
    with pytest.raises(InternalError, match="slot does not belong"):
        asyncio.run(generate_slot_traces(instruction_program, plan, invalid, 0, services))
    assert not engine.plan_requests
    assert metrics.counters == {}


@pytest.mark.parametrize("tamper", (
    "program_digest", "program_semantics", "plan_digest", "event_key",
))
def test_program_plan_identity_tamper_is_zero_llm(
    instruction_config, instruction_program, tamper,
):
    """program/plan 任一身份根漂移都必须先于 ScenarioSeed 调用终止。"""
    program = instruction_program
    plan = compile_scenario_plan(program)
    if tamper == "program_digest":
        program = replace(program, digest="f" * 64)
    elif tamper == "program_semantics":
        frames = dict(program.frame_classes)
        frames["task_request"] = replace(
            frames["task_request"], description="forged semantic description",
        )
        program = replace(program, frame_classes=frames)
    elif tamper == "plan_digest":
        plan = replace(plan, digest="f" * 64)
    else:
        blocks = [dict(block) for block in plan.blocks]
        key = next(iter(blocks[0]))
        blocks[0][key] = (replace(blocks[0][key][0], event_key="f" * 32),
                          *blocks[0][key][1:])
        plan = replace(plan, blocks=tuple(blocks))
    services, engine, metrics = _services(instruction_config)
    with pytest.raises(InternalError, match="generation_downstream_contract"):
        asyncio.run(generate_slot_traces(
            program, plan, plan.delivery_slots[0], 0, services,
        ))
    assert engine.validated_calls == [] and engine.plan_requests == []
    assert metrics.counters == {}


def test_scenario_block_entry_order_tamper_is_zero_llm(
    declared_config,
    declared_program,
):
    """Mapping equality 忽略的 block insertion order 仍由 plan digest 拒绝。"""
    plan = compile_scenario_plan(declared_program)
    blocks = list(plan.blocks)
    blocks[0] = dict(reversed(tuple(blocks[0].items())))
    tampered = replace(plan, blocks=tuple(blocks))
    assert tampered == plan and scenario_plan_digest(tampered) != tampered.digest
    services, engine, metrics = _services(declared_config)
    with pytest.raises(InternalError, match="scenario plan digest is invalid"):
        asyncio.run(generate_slot_traces(
            declared_program, tampered, tampered.delivery_slots[0], 0, services,
        ))
    assert engine.validated_calls == [] and engine.plan_requests == []
    assert metrics.counters == {}


@pytest.mark.parametrize("field", ("event_key", "topic", "timestamp_us"))
def test_noise_plan_identity_tamper_is_zero_llm(
    declared_config, declared_program, field,
):
    """即使重算 plan digest，伪造 noise key 或时间也必须在首调用前终止。"""
    plan = compile_scenario_plan(declared_program)
    values = {
        "event_key": "f" * 32,
        "topic": "未声明话题",
        "timestamp_us": plan.noise_slots[0].timestamp_us + 1,
    }
    value = values[field]
    noise = replace(plan.noise_slots[0], **{field: value})
    tampered = replace(plan, noise_slots=(noise, *plan.noise_slots[1:]), digest="")
    tampered = replace(tampered, digest=scenario_plan_digest(tampered))
    services, engine, metrics = _services(declared_config)
    with pytest.raises(InternalError, match="canonical planner output"):
        asyncio.run(generate_slot_traces(
            declared_program, tampered, tampered.delivery_slots[0], 0, services,
        ))
    assert engine.validated_calls == [] and engine.plan_requests == []
    assert metrics.counters == {}


def _assert_plan_tamper_is_zero_llm(config, program, plan):
    """断言协调重算摘要后的结构篡改仍在首个调用前终止。"""
    plan = replace(plan, digest=scenario_plan_digest(replace(plan, digest="")))
    services, engine, metrics = _services(config)
    with pytest.raises(InternalError, match="canonical planner output"):
        asyncio.run(generate_slot_traces(program, plan, plan.delivery_slots[0], 0, services))
    assert engine.validated_calls == [] and engine.plan_requests == []
    assert metrics.counters == {}


def test_declared_role_word_tamper_is_zero_llm(declared_config, declared_program):
    """协调重派 event key 也不能把 baseline role word 改成重复 role。"""
    plan = compile_scenario_plan(declared_program)
    blocks = [dict(block) for block in plan.blocks]
    slot = plan.delivery_slots[0]
    key = (slot.slot_key, None)
    events = blocks[0][key]
    scenario_id = derive_generation_id(
        "declared_scenario_id", [declared_program.digest, slot.source_name, slot.scenario_index]
    )
    forged = replace(
        events[0], role=events[1].role,
        event_key=derive_generation_id("declared_event_key", [scenario_id, events[1].role]),
    )
    blocks[0][key] = (forged, *events[1:])
    _assert_plan_tamper_is_zero_llm(
        declared_config, declared_program, replace(plan, blocks=tuple(blocks))
    )


@pytest.mark.parametrize("field", ("logical_time_us", "session_id"))
def test_primary_coordinate_tamper_is_zero_llm(
    declared_config, declared_program, field,
):
    """primary 逻辑时间或 session 漂移即使重算摘要也必须零调用终止。"""
    plan = compile_scenario_plan(declared_program)
    blocks = [dict(block) for block in plan.blocks]
    key = next(key for key in blocks[0] if key[1] is not None)
    events = blocks[0][key]
    value = events[0].logical_time_us + 999 if field == "logical_time_us" else "forged"
    blocks[0][key] = (replace(events[0], **{field: value}), *events[1:])
    _assert_plan_tamper_is_zero_llm(
        declared_config, declared_program, replace(plan, blocks=tuple(blocks)),
    )


@pytest.mark.parametrize("field", ("shift_us", "source"))
def test_replay_coordinate_or_source_tamper_is_zero_llm(
    declared_config, declared_program, field,
):
    """replay 时间或声明序 source 漂移必须在零调用边界终止。"""
    plan = compile_scenario_plan(declared_program)
    layout = plan.replay_layouts[0]
    if field == "shift_us":
        layout = replace(layout, shift_us=layout.shift_us + 1)
    else:
        layout = replace(layout, source_slot_key=plan.delivery_slots[1].slot_key)
    tampered = replace(plan, replay_layouts=(layout, *plan.replay_layouts[1:]))
    _assert_plan_tamper_is_zero_llm(declared_config, declared_program, tampered)


def test_hidden_baseline_only_block_tamper_is_zero_llm(
    declared_config, declared_program,
):
    """hidden baseline 不能被协调移动到 allocator 不会产生的专用 block。"""
    plan = compile_scenario_plan(declared_program)
    first = dict(plan.blocks[0])
    hidden_key = next(key for key in first if key[1] is None)
    hidden = first.pop(hidden_key)
    tampered = replace(plan, blocks=({hidden_key: hidden}, first, *plan.blocks[1:]))
    _assert_plan_tamper_is_zero_llm(declared_config, declared_program, tampered)


def test_instruction_non_optimal_length_tamper_is_zero_llm(
    instruction_config, instruction_program,
):
    """len_range 上界内但非 CP-SAT 最优的额外位置也不是第二份合法计划。"""
    plan = compile_scenario_plan(instruction_program)
    block = dict(plan.blocks[0])
    key = next(iter(block))
    events = block[key]
    position = len(events)
    scenario_id = derive_generation_id(
        "instruction_scenario_id",
        [instruction_program.digest, plan.delivery_slots[0].source_name, 0],
    )
    extra = replace(
        events[-1],
        event_key=derive_generation_id(
            "instruction_event_key",
            [scenario_id, plan.delivery_slots[0].source_name, 0, position],
        ),
        role=f"position_{position:03d}", position=position,
        logical_time_us=position * instruction_program.timeline.event_gap_us[0],
        timestamp_us=events[-1].timestamp_us + instruction_program.timeline.event_gap_us[0],
    )
    block[key] = (*events, extra)
    _assert_plan_tamper_is_zero_llm(
        instruction_config, instruction_program, replace(plan, blocks=(block,)),
    )


def test_estimate_counts_hidden_baseline_semantics_without_positive(
    tmp_path, monkeypatch,
):
    """缺省 positive 时，估算必须计入不交付但完整判定的 hidden baseline。"""
    root = tmp_path / "sequence-generation"
    shutil.copytree(_CATALOG.parents[1], root)
    project = root / "project.toml"
    text = project.read_text(encoding="utf-8")
    positive = '''[[generate.counterfactual_sets.variants]]
name = "positive"
kind = "positive"
outcome_schema_path = "schemas/outcome-positive.json"

'''
    assert positive in text
    text = text.replace(positive, "", 1)
    text = text.replace("duplicate_sequences = 1", "duplicate_sequences = 0", 1)
    project.write_text(text, encoding="utf-8")
    monkeypatch.setenv("LABELKIT_DEEPSEEK_KEY", "offline-test-key")
    config = load(root / "config.toml", project, CliOverrides())
    from labelkit.operators.generation.program import compile_generation_program

    program = compile_generation_program(config)
    plan = compile_scenario_plan(program)
    services, _engine, metrics = _services(config)
    for slot in plan.delivery_slots:
        asyncio.run(generate_slot_traces(program, plan, slot, 0, services))
    actual = metrics.counters["generate.sequence.calls.semantic_evaluation_calls"]
    estimate = estimate_sequence_products(config, program, plan)
    assert actual == 8
    assert estimate["sequence"]["sequence_calls"]["semantic_evaluation_calls"] == actual
    assert estimate["sequence"]["planned_sequences"] == 6


def test_instruction_empty_actor_carrier_is_zero_event_call(
    instruction_config, instruction_program,
):
    """内部 forged 空 actor 名不能进入 EventPlan 或 renderer。"""
    plan = compile_scenario_plan(instruction_program)
    services, engine, metrics = _services(instruction_config)
    raw = engine.seed
    seed = ScenarioSeed(
        raw["initial_state"], {"": next(iter(raw["actors"].values()))},
        raw["shared_facts"], raw["style"], raw["time_context"],
    )
    context = EventExecutionContext(
        instruction_program, plan, plan.delivery_slots[0], None, 0,
        seed, seed.initial_state, (),
    )
    with pytest.raises(InternalError, match="actor registry"):
        asyncio.run(plan_event(context, 0, "nonce", services))
    assert engine.plan_requests == [] and engine.validated_calls == []
    assert metrics.counters == {}


def test_declared_event_request_is_exact_projection_and_invalid_context_is_zero_call(
    declared_config, declared_program,
):
    plan = compile_scenario_plan(declared_program)
    slot = plan.delivery_slots[0]
    source = declared_program.class_views[slot.sequence_class].sequence_generation
    seed = ScenarioSeed(**dict(source.initial_state_catalog[slot.catalog_row_index]))
    context = EventExecutionContext(
        declared_program, plan, slot, None, 0, seed, seed.initial_state, (),
    )
    request = build_event_plan_request(context, 2, "nonce")
    assert request.mode == "declared"
    assert request.semantic_profile == declared_program.semantic_profile
    assert request.slot_key == slot.slot_key
    assert request.planned_event == _branch(plan, slot.slot_key, None)[0]
    assert request.role.name == "request"
    assert request.generation_instruction == source.instruction
    assert request.sequence_length == 3
    assert tuple(request.eligible_frame_classes) == ("task_request",)
    assert request.eligible_actors == ("requester",)
    assert request.visible_state is None and request.state_schema is None
    assert request.outcome_schema is None
    assert request.history is None
    assert request.actor_profiles is None
    assert "hidden_sentinel" not in canonical_json(request.actor_view.read_state)
    assert canonical_json(request.public_facts) == canonical_json(seed.shared_facts["public"])
    assert request.attempt_index == 2 and request.variation_nonce == "nonce"
    user = "".join(
        part.text or "" for part in scenario_module._event_prompt(
            request, declared_program.limits.prompt_value_bytes
        ).messages[1].parts
    )
    assert "[完整状态 Schema]\nnull" in user
    assert "[末事件 Outcome Schema]\nnull" in user
    assert "\n\n[ActorView]" in user and "\n\n\n[ActorView]" not in user
    services, engine, metrics = _services(declared_config)
    invalid = replace(context, event_index=99)
    with pytest.raises(InternalError, match="event index"):
        asyncio.run(plan_event(invalid, 0, "nonce", services))
    assert not engine.plan_requests
    assert metrics.counters == {}


def test_missing_block_key_is_zero_llm_and_terminal(declared_config, declared_program):
    plan = compile_scenario_plan(declared_program)
    context = _declared_context(declared_program, replace(plan, blocks=()))
    services, engine, metrics = _services(declared_config)
    with pytest.raises(InternalError, match="scenario plan digest is invalid"):
        asyncio.run(plan_event(context, 0, "nonce", services))
    assert not engine.plan_requests
    assert metrics.counters == {}


@pytest.mark.parametrize(("errors", "expected_kind"), (
    (["post_validator_invalid"], "post_validator_invalid"),
    (["post_validator_exception"], "post_validator_exception"),
    (["(post-validator) patch_permission"], "state_transition"),
    (["/patch: invalid"], "event_schema"),
))
def test_event_plan_schema_failures_use_stable_rejection_kinds(
    declared_config, declared_program, errors, expected_kind,
):
    plan = compile_scenario_plan(declared_program)
    context = _declared_context(declared_program, plan)
    services, engine, metrics = _services(declared_config)

    async def reject(_request):
        raise SchemaViolation(errors, "redacted")

    engine.complete_post_validated = reject
    with pytest.raises(GenerationAttemptRejected) as caught:
        asyncio.run(plan_event(context, 0, "nonce", services))
    assert caught.value.kind == expected_kind
    assert metrics.counters["generate.sequence.calls.baseline_event_plan_calls"] == 1


@pytest.mark.parametrize("binding_shape", ("missing", "duplicate", "extra"))
def test_pattern_binding_must_cover_each_draft_exactly_once(
    declared_config, declared_program, monkeypatch, binding_shape,
):
    plan = compile_scenario_plan(declared_program)
    services, _engine, _metrics = _services(declared_config)
    traces = asyncio.run(generate_slot_traces(
        declared_program, plan, plan.delivery_slots[0], 0, services,
    ))
    positive = next(trace for trace in traces if trace.variant_name == "positive")
    drafts = tuple(_draft(event) for event in positive.events)
    bindings = dict(positive.pattern_evaluation.actual_bindings)
    if binding_shape == "missing":
        bindings.pop(drafts[-1].event_id)
    elif binding_shape == "extra":
        bindings["f" * 32] = "confirm"
    else:
        bindings[drafts[-1].event_id] = bindings[drafts[0].event_id]
    evaluation = replace(positive.pattern_evaluation, actual_bindings=bindings)
    monkeypatch.setattr(scenario_module, "evaluate_pattern", lambda _pattern, _events: evaluation)
    pattern = declared_program.patterns[positive.pattern_name]
    with pytest.raises(GenerationAttemptRejected) as caught:
        scenario_module._bind_truths(pattern, drafts)
    assert caught.value.kind == "pattern_evaluation"


def test_declared_high_level_api_delivers_four_exact_variants(declared_config,
                                                               declared_program):
    plan = compile_scenario_plan(declared_program)
    services, engine, metrics = _services(declared_config)
    slot = plan.delivery_slots[0]
    traces = asyncio.run(generate_slot_traces(declared_program, plan, slot, 0, services))
    assert tuple(trace.variant_name for trace in traces) == slot.variant_names
    expected = {
        variant.name: (() if not variant.expected_violation
                       else (dict(variant.expected_violation),))
        for variant in declared_program.counterfactual_sets[0].variants
    }
    for trace in traces:
        assert tuple(dict(item) for item in trace.pattern_evaluation.actual_violations) == expected[
            trace.variant_name
        ]
        assert trace.state_evaluation.outcome_valid
        assert trace.state_evaluation.protected_prefix_valid
    reordered = next(
        trace for trace in traces
        if trace.variant_name == "confirmation_before_acknowledgement"
    )
    assert reordered.final_state["request"] == {
        "id": "R-100", "status": "blocked", "acknowledged": True,
    }
    assert reordered.events[-1].patch == (
        {"op": "test", "path": "/request/status", "value": "blocked"},
        {"op": "replace", "path": "/request/acknowledged", "value": True},
    )
    assert reordered.events[-1].payload["status"] == "blocked"
    prefix = "generate.sequence.calls."
    assert metrics.counters[prefix + "baseline_event_plan_calls"] == 3
    assert metrics.counters[prefix + "variant_event_plan_calls"] == 4
    assert metrics.counters[prefix + "frame_render_calls"] == 7
    assert metrics.counters[prefix + "semantic_evaluation_calls"] == 4
    assert prefix + "scenario_seed_calls" not in metrics.counters
    plan_users = [_user_text(item) for item in engine.plan_requests]
    for user in plan_users:
        assert "catalog-secret-a" not in user
        assert all(token not in user for token in (
            "expected_violation", "actual_violations", "variant=", "target=",
        ))
    source = declared_program.counterfactual_sets[0]
    for variant in source.variants:
        rendered = f"[末事件 Outcome Schema]\n{canonical_json(variant.outcome_schema)}"
        assert any(rendered in user for user in plan_users)
    assert sum("[末事件 Outcome Schema]\nnull" in user for user in plan_users) == 3
    frame_prompts = [
        prompt for _profile, prompt, schema in engine.validated_calls
        if "utterance" in schema.get("properties", {})
    ]
    assert frame_prompts
    for prompt in frame_prompts:
        user = "".join(part.text or "" for part in prompt.messages[1].parts)
        assert "catalog-secret-a" not in user


def test_declared_counterfactual_suffixes_overlap_after_baseline(
    declared_config, declared_program, monkeypatch,
):
    """三条 sibling suffix 必须同时到达屏障，串行实现会超时。"""
    plan = compile_scenario_plan(declared_program)
    services, _engine, _metrics = _services(declared_config)
    original = scenario_module._generate_variant
    all_started = asyncio.Event()
    active = high_water = 0

    async def guarded(run, baseline, variant):
        nonlocal active, high_water
        active += 1
        high_water = max(high_water, active)
        if active == 3:
            all_started.set()
        try:
            await asyncio.wait_for(all_started.wait(), timeout=1)
            return await original(run, baseline, variant)
        finally:
            active -= 1

    monkeypatch.setattr(scenario_module, "_generate_variant", guarded)
    traces = asyncio.run(generate_slot_traces(
        declared_program, plan, plan.delivery_slots[0], 0, services,
    ))

    assert high_water == 3
    assert tuple(trace.variant_name for trace in traces) == plan.delivery_slots[0].variant_names


def test_declared_suffix_fatal_cancels_and_settles_siblings(
    declared_config, declared_program, monkeypatch,
):
    """fatal 必须等全部 sibling cleanup 后以原异常身份离开。"""
    plan = compile_scenario_plan(declared_program)
    services, _engine, _metrics = _services(declared_config)
    all_started = asyncio.Event()
    never = asyncio.Event()
    started = cleaned = 0
    fatal = RuntimeError("suffix fatal")

    async def fail_one(_run, _baseline, variant):
        nonlocal started, cleaned
        started += 1
        if started == 3:
            all_started.set()
        try:
            await asyncio.wait_for(all_started.wait(), timeout=1)
            if variant.name == "missing_acknowledgement":
                raise fatal
            await never.wait()
        finally:
            cleaned += 1

    monkeypatch.setattr(scenario_module, "_generate_variant", fail_one)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(generate_slot_traces(
            declared_program, plan, plan.delivery_slots[0], 0, services,
        ))

    assert caught.value is fatal
    assert started == 3 and cleaned == 3


def test_declared_suffix_recoverable_uses_variant_declaration_order(
    declared_config, declared_program, monkeypatch,
):
    """多个可恢复失败同时完成时选择最早声明的 variant。"""
    plan = compile_scenario_plan(declared_program)
    services, _engine, _metrics = _services(declared_config)
    kinds = {
        "missing_acknowledgement": "event_schema",
        "confirmation_before_acknowledgement": "event_semantic",
        "confirmation_timeout": "state_transition",
    }

    async def reject(_run, _baseline, variant):
        raise GenerationAttemptRejected(kinds[variant.name], "ignored")

    monkeypatch.setattr(scenario_module, "_generate_variant", reject)
    with pytest.raises(GenerationAttemptRejected) as caught:
        asyncio.run(generate_slot_traces(
            declared_program, plan, plan.delivery_slots[0], 0, services,
        ))

    assert caught.value.kind == "event_schema"


def test_state_oracle_exposes_replay_binding_outcome_and_prefix_failures_independently(
    declared_config, declared_program,
):
    plan = compile_scenario_plan(declared_program)
    services, _engine, _metrics = _services(declared_config)
    slot = plan.delivery_slots[0]
    traces = asyncio.run(generate_slot_traces(declared_program, plan, slot, 0, services))
    by_name = {trace.variant_name: trace for trace in traces}
    variants = {
        variant.name: variant
        for variant in declared_program.counterfactual_sets[0].variants
    }
    pattern = declared_program.patterns[slot.pattern_name]
    positive = by_name["positive"]

    changed_final = json.loads(canonical_json(positive.final_state))
    changed_final["audit"].append("independent-final-state-change")
    replay = evaluate_state(StateEvaluationRequest(
        declared_program, slot, pattern, variants["positive"], positive.scenario_seed,
        positive.events, positive.events, changed_final,
    ))
    assert replay.replay_hash != replay.final_state_hash
    assert replay.bindings_valid and replay.protected_prefix_valid

    payload = json.loads(canonical_json(positive.events[0].payload))
    payload["request_id"] = "wrong"
    binding_events = (replace(positive.events[0], payload=payload), *positive.events[1:])
    binding = evaluate_state(StateEvaluationRequest(
        declared_program, slot, pattern, variants["positive"], positive.scenario_seed,
        binding_events, binding_events, positive.final_state,
    ))
    assert binding.replay_hash == binding.final_state_hash
    assert not binding.bindings_valid and binding.outcome_valid
    assert binding.protected_prefix_valid

    impossible = replace(variants["positive"], outcome_schema={"not": {}})
    outcome = evaluate_state(StateEvaluationRequest(
        declared_program, slot, pattern, impossible, positive.scenario_seed,
        positive.events, positive.events, positive.final_state,
    ))
    assert outcome.replay_hash == outcome.final_state_hash
    assert outcome.bindings_valid and not outcome.outcome_valid
    assert outcome.protected_prefix_valid

    reordered = by_name["confirmation_before_acknowledgement"]
    changed_prefix = (replace(reordered.events[0], intent="changed"), *reordered.events[1:])
    prefix = evaluate_state(StateEvaluationRequest(
        declared_program, slot, pattern,
        variants["confirmation_before_acknowledgement"], reordered.scenario_seed,
        changed_prefix, positive.events, reordered.final_state,
    ))
    assert prefix.replay_hash == prefix.final_state_hash
    assert prefix.bindings_valid and prefix.outcome_valid
    assert not prefix.protected_prefix_valid


def test_state_evaluator_rechecks_patch_schema_hash_and_hook_gates(
    declared_config,
    declared_program,
):
    """StateEvaluator 不借用 StateExecutor 的已通过结论。"""
    plan = compile_scenario_plan(declared_program)
    services, _engine, _metrics = _services(declared_config)
    slot = plan.delivery_slots[0]
    traces = asyncio.run(generate_slot_traces(
        declared_program, plan, slot, 0, services
    ))
    positive = next(trace for trace in traces if trace.variant_name == "positive")
    variant = next(
        item for item in declared_program.counterfactual_sets[0].variants
        if item.name == "positive"
    )
    pattern = declared_program.patterns[slot.pattern_name]

    def evaluate(events, *, program=declared_program, pattern_value=pattern):
        """用同一被测 events 作 baseline，仅聚焦独立状态 gate。"""
        return evaluate_state(StateEvaluationRequest(
            program,
            slot,
            pattern_value,
            variant,
            positive.scenario_seed,
            events,
            events,
            positive.final_state,
        ))

    first = positive.events[0]
    forbidden_noop = replace(first, patch=(
        {"op": "test", "path": "/hidden_sentinel", "value": "catalog-secret-a"},
        {"op": "test", "path": "/request/status", "value": "new"},
        {"op": "replace", "path": "/hidden_sentinel", "value": "catalog-secret-a"},
        {"op": "replace", "path": "/request/status", "value": "pending"},
    ))
    invalid_order = replace(first, patch=(
        {"op": "replace", "path": "/request/status", "value": "pending"},
        {"op": "test", "path": "/request/status", "value": "pending"},
    ))
    for changed in (forbidden_noop, invalid_order):
        result = evaluate((changed, *positive.events[1:]))
        assert not result.outcome_valid

    strict_role = replace(pattern.roles[0], pre_state_schema={"not": {}})
    strict_pattern = replace(pattern, roles=(strict_role, *pattern.roles[1:]))
    assert not evaluate(positive.events, pattern_value=strict_pattern).outcome_valid

    view = declared_program.class_views[slot.sequence_class]
    generation = replace(view.sequence_generation, state_schema={"not": {}})
    invalid_view = replace(view, sequence_generation=generation)
    invalid_program = replace(
        declared_program,
        class_views={**dict(declared_program.class_views), slot.sequence_class: invalid_view},
    )
    assert not evaluate(positive.events, program=invalid_program).outcome_valid

    def reject_transition(_value):
        """拒绝所有状态转换。"""
        return ["rejected"]

    hook_program = replace(
        declared_program,
        state_validator=ResolvedHook("offline.py:reject_transition", reject_transition),
    )
    assert not evaluate(positive.events, program=hook_program).outcome_valid

    for field in ("state_before_hash", "state_after_hash"):
        changed = replace(first, **{field: "0" * 64})
        result = evaluate((changed, *positive.events[1:]))
        assert result.replay_hash == result.final_state_hash
        assert not result.outcome_valid

    def raise_from_hook(_value):
        """模拟用户 hook 异常。"""
        raise RuntimeError("secret hook detail")

    raising_program = replace(
        declared_program,
        state_validator=ResolvedHook("offline.py:raise_from_hook", raise_from_hook),
    )
    with pytest.raises(RuntimeError, match="state evaluator hook failed"):
        evaluate(positive.events, program=raising_program)


def test_noise_render_and_blind_evaluation_use_two_counted_families(declared_config,
                                                                    declared_program):
    plan = compile_scenario_plan(declared_program)
    services, _engine, metrics = _services(declared_config)
    slot = plan.noise_slots[0]
    frame = declared_program.frame_classes[slot.frame_class]
    descriptions = {name: view.description for name, view in declared_program.class_views.items()}
    frames = {name: view.description for name, view in declared_program.frame_classes.items()}
    render_request = NoiseRenderRequest(
        declared_program.semantic_profile, slot, declared_program.noise, frame,
        descriptions, frames, 0, declared_program.timeline.utc_offset_minutes,
        declared_program.limits,
    )
    payload = asyncio.run(render_noise(render_request, services))
    evaluation_request = NoiseEvaluationRequest(
        declared_program.evaluation_profile, payload, slot.topic, descriptions, frames, 0,
        declared_program.limits,
    )
    evaluation = asyncio.run(evaluate_noise(evaluation_request, services))
    assert payload == {"utterance": "今天的云很好看"}
    assert evaluation.unrelated_to_declared_tasks and evaluation.no_executable_task
    assert evaluation.matches_planned_topic
    assert metrics.counters == {
        "generate.sequence.calls.noise_render_calls": 1,
        "generate.sequence.calls.noise_evaluation_calls": 1,
    }


def test_scenario_seed_byte_limit_accepts_exact_boundary(instruction_config,
                                                          instruction_program):
    plan = compile_scenario_plan(instruction_program)
    slot = plan.delivery_slots[0]
    services, engine, _metrics = _services(instruction_config)
    size = len(canonical_json(engine.seed).encode("utf-8"))
    exact = replace(
        instruction_program,
        limits=replace(instruction_program.limits, scenario_seed_bytes=size),
    )
    request = ScenarioSeedRequest(exact, slot, 0, 7)
    seed = asyncio.run(generate_scenario_seed(request, services))
    assert seed.initial_state["hidden_sentinel"] == "catalog-secret-a"
    seed_prompt = engine.validated_calls[0][1]
    user = "".join(part.text or "" for part in seed_prompt.messages[1].parts)
    assert '"actor_profile":{"each_value":"object","required":[' in user
    assert '"goal","identity","style"' in user
    assert (
        '{"initial_state":{},"actors":{"<actor_name>":{"goal":{},"identity":{},'
        '"style":{}}},"shared_facts":{"public":{},"hidden":{}},"style":{},'
        '"time_context":{}}'
    ) in user
    below = replace(exact, limits=replace(exact.limits, scenario_seed_bytes=size - 1))
    with pytest.raises(GenerationAttemptRejected) as caught:
        asyncio.run(generate_scenario_seed(ScenarioSeedRequest(below, slot, 0, 7), services))
    assert getattr(caught.value, "kind", None) == "scenario_schema"


def test_declared_scenario_seed_requires_observer_only_actor(
    declared_config, declared_program,
):
    pattern = declared_program.patterns["booking_success"]
    first = replace(pattern.roles[0], observers=(*pattern.roles[0].observers, "auditor"))
    changed_pattern = replace(pattern, roles=(first, *pattern.roles[1:]))
    view = declared_program.class_views["ticket_booking"]
    generation = view.sequence_generation
    rows = [json.loads(canonical_json(row)) for row in generation.initial_state_catalog]
    rows[0]["actors"]["auditor"] = {
        "goal": {"intent": "audit"}, "identity": {"role": "auditor"}, "style": {},
    }
    changed_generation = replace(generation, initial_state_catalog=tuple(rows))
    changed_view = replace(view, sequence_generation=changed_generation)
    program = replace(
        declared_program,
        patterns={**dict(declared_program.patterns), pattern.name: changed_pattern},
        class_views={**dict(declared_program.class_views), "ticket_booking": changed_view},
        digest="",
    )
    program = replace(program, digest=generation_program_digest(program))
    plan = compile_scenario_plan(program)
    services, _engine, _metrics = _services(declared_config)
    seed = asyncio.run(generate_scenario_seed(
        ScenarioSeedRequest(program, plan.delivery_slots[0], 0, 7), services,
    ))
    assert tuple(seed.actors) == ("requester", "system", "auditor")


def test_catalog_scenario_seed_retry_reuses_exact_planned_row(
    declared_config, declared_program,
):
    plan = compile_scenario_plan(declared_program)
    slot = plan.delivery_slots[0]
    services, engine, _metrics = _services(declared_config)
    first = asyncio.run(generate_scenario_seed(
        ScenarioSeedRequest(declared_program, slot, 0, 7), services,
    ))
    retry = asyncio.run(generate_scenario_seed(
        ScenarioSeedRequest(declared_program, slot, 1, 99), services,
    ))
    expected = ScenarioSeed(**json.loads(
        _CATALOG.read_text(encoding="utf-8").splitlines()[slot.catalog_row_index]
    ))
    assert first == retry == expected
    assert engine.validated_calls == []

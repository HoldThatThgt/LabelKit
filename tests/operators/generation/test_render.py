"""FrameRenderer 的 binding、完整 Schema 与预算边界测试。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator

from labelkit.common.config.generation import PayloadBindingSpec
from labelkit.common.contracts.generation import (
    ActorView,
    EventPlan,
    GenerationServices,
    RenderEventRequest,
)
from labelkit.common.errors import ContextOverflowError, InternalError
from labelkit.operators.generation import GenerationAttemptRejected
from labelkit.operators.generation.planner import compile_scenario_plan
from labelkit.operators.generation.project import canonical_json
from labelkit.operators.generation.render import render_event


class _Metrics:
    """记录 renderer 逻辑调用次数。"""

    def __init__(self):
        self.counters = {}

    def count(self, key: str, n: int = 1):
        self.counters[key] = self.counters.get(key, 0) + n


class _SchemaEngine:
    """返回指定 payload 并真实执行最终 jsonschema 复验。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.prompt = None

    async def complete_validated(self, *_args, **_kwargs):
        self.calls += 1
        self.prompt = _args[1]
        return (self.payload,)

    def validate_only(self, value, *, schema):
        return [error.message for error in Draft202012Validator(schema).iter_errors(value)]


class _InlineTaskExecutor:
    """按声明序执行冻结叶任务并返回输入序结果。"""

    async def run_group(self, request):
        return tuple([await task.operation() for task in request.tasks])


def _services(config, payload):
    """构造 renderer 专用离线服务。"""
    engine, metrics = _SchemaEngine(payload), _Metrics()
    services = GenerationServices(
        config,
        engine,
        object(),
        metrics,
        _InlineTaskExecutor(),
    )
    return services, engine, metrics


def _planned(plan, slot_key: str, variant: str, role: str):
    """按 branch 与 role 读取唯一 PlannedEvent。"""
    events = next(block[(slot_key, variant)] for block in plan.blocks
                  if (slot_key, variant) in block)
    return next(event for event in events if event.role == role)


def _request(program, plan, role_name: str, values, frame=None, role=None):
    """构造一个完整但不含状态正文的 RenderEventRequest。"""
    slot = plan.delivery_slots[0]
    pattern = program.patterns[slot.pattern_name]
    selected = role or next(item for item in pattern.roles if item.name == role_name)
    planned = _planned(plan, slot.slot_key, "positive", role_name)
    event_plan = EventPlan(selected.frame_class, selected.actor, "render intent", ())
    actor_view = ActorView(selected.actor, {}, {}, (), planned.logical_time_us, 0)
    frame_spec = frame or program.frame_classes[selected.frame_class]
    return RenderEventRequest(
        program.semantic_profile, slot.slot_key, planned, event_plan, actor_view,
        {}, "before", "after", values, frame_spec, selected, {}, 0, program.limits,
    )


def test_binding_overwrites_conflicting_llm_values_and_revalidates(declared_config,
                                                                    declared_program):
    plan = compile_scenario_plan(declared_program)
    values = {"/request_id": "R-100", "/status": "pending"}
    request = _request(declared_program, plan, "request", values)
    services, engine, _metrics = _services(
        declared_config,
        {"utterance": "请订票", "request_id": "WRONG", "status": "pending"},
    )
    payload = asyncio.run(render_event(request, services))
    assert payload == {"utterance": "请订票", "request_id": "R-100", "status": "pending"}
    assert engine.calls == 1
    system = "".join(part.text or "" for part in engine.prompt.messages[0].parts)
    assert "不得照抄状态枚举、内部指标或实现术语" in system
    assert "不得用两个同义短语机械复述一个结果" in system
    assert "真正经历等待的动作、阶段或参与方作主语" in system
    assert "从受理到确认的等待已超过可用时间" in system
    assert "引用先前通知即可" in system
    assert "正在发出的消息写成它收到的对象" in system


def test_teaching_confirmation_schema_rejects_unnatural_wait_subject(declared_program):
    """教学帧 Schema 拒绝把请求本身写成等待主体。"""
    schema = json.loads(canonical_json(
        declared_program.frame_classes["confirmation"].gen_schema,
    ))
    payload = {
        "utterance": "您的请求R-100等待已超过可用时间，本次未能出票。",
        "request_id": "R-100",
        "ticket_id": None,
        "status": "expired",
    }
    errors = tuple(Draft202012Validator(schema).iter_errors(payload))
    assert errors


@pytest.mark.parametrize("utterance", (
    "您的请求 R-1200 已确认出票，票号 T-1200，车票已成功出票。",
    "您的订票请求 R-300 已确认出票成功，票号 T-300，状态为已出票。",
))
def test_teaching_confirmation_schema_rejects_repeated_terminal_keyword(
    declared_program, utterance,
):
    """教学帧 Schema 拒绝盲审发现的同句终态重复。"""
    schema = json.loads(canonical_json(
        declared_program.frame_classes["confirmation"].gen_schema,
    ))
    payload = {
        "utterance": utterance,
        "request_id": "R-100",
        "ticket_id": "T-100",
        "status": "ticketed",
    }
    assert tuple(Draft202012Validator(schema).iter_errors(payload))


def test_teaching_confirmation_schema_accepts_single_terminal_keyword(declared_program):
    """教学帧 Schema 保留仅声明一次终态的自然表达。"""
    schema = json.loads(canonical_json(
        declared_program.frame_classes["confirmation"].gen_schema,
    ))
    payload = {
        "utterance": "请求 R-100 已出票，票号为 T-100。",
        "request_id": "R-100",
        "ticket_id": "T-100",
        "status": "ticketed",
    }
    assert not tuple(Draft202012Validator(schema).iter_errors(payload))


def test_binding_missing_parent_is_recoverable_frame_rejection(declared_config,
                                                                declared_program):
    plan = compile_scenario_plan(declared_program)
    original = declared_program.patterns["booking_success"].roles[0]
    role = replace(original, payload_bindings=(
        PayloadBindingSpec("/nested/request_id", "after", "/request/id"),
    ))
    schema = {
        "type": "object",
        "properties": {
            "utterance": {"type": "string"},
            "nested": {
                "type": "object",
                "properties": {"request_id": {"type": "string"}},
                "required": ["request_id"],
            },
        },
        "required": ["utterance", "nested"],
    }
    frame = replace(declared_program.frame_classes["task_request"], gen_schema=schema)
    request = _request(
        declared_program, plan, "request", {"/nested/request_id": "R-100"}, frame, role,
    )
    services, _engine, _metrics = _services(declared_config, {"utterance": "请订票"})
    with pytest.raises(GenerationAttemptRejected) as caught:
        asyncio.run(render_event(request, services))
    assert caught.value.kind == "frame_schema"


def test_full_confirmation_combination_schema_runs_after_binding(declared_config,
                                                                  declared_program):
    plan = compile_scenario_plan(declared_program)
    values = {"/request_id": "R-100", "/ticket_id": "T-100", "/status": "ticketed"}
    request = _request(declared_program, plan, "confirm", values)
    services, _engine, _metrics = _services(declared_config, {
        "utterance": "出票完成", "request_id": "R-100", "ticket_id": None,
        "status": "blocked",
    })
    payload = asyncio.run(render_event(request, services))
    assert payload["status"] == "ticketed" and payload["ticket_id"] == "T-100"


def test_authoritative_binding_failing_complete_schema_rejects_without_second_call(
    declared_config, declared_program,
):
    plan = compile_scenario_plan(declared_program)
    values = {"/request_id": "R-100", "/status": "not-a-declared-status"}
    request = _request(declared_program, plan, "request", values)
    services, engine, _metrics = _services(
        declared_config,
        {"utterance": "请订票", "request_id": "WRONG", "status": "pending"},
    )
    with pytest.raises(GenerationAttemptRejected) as caught:
        asyncio.run(render_event(request, services))
    assert caught.value.kind == "frame_schema"
    assert engine.calls == 1


def test_ancestor_binding_conflict_fails_mechanical_overwrite(declared_config,
                                                               declared_program):
    plan = compile_scenario_plan(declared_program)
    original = declared_program.patterns["booking_success"].roles[0]
    role = replace(original, payload_bindings=(
        PayloadBindingSpec("/nested", "after", "/request/id"),
        PayloadBindingSpec("/nested/request_id", "after", "/request/id"),
    ))
    schema = {
        "type": "object",
        "properties": {
            "utterance": {"type": "string"},
            "nested": {"type": ["object", "string"]},
        },
        "required": ["utterance", "nested"],
    }
    frame = replace(declared_program.frame_classes["task_request"], gen_schema=schema)
    request = _request(
        declared_program,
        plan,
        "request",
        {"/nested": "R-100", "/nested/request_id": "R-100"},
        frame,
        role,
    )
    services, engine, _metrics = _services(
        declared_config, {"utterance": "请订票", "nested": {}},
    )
    with pytest.raises(GenerationAttemptRejected) as caught:
        asyncio.run(render_event(request, services))
    assert caught.value.kind == "frame_schema"
    assert engine.calls == 1


def test_rendered_payload_byte_limit_accepts_exact_boundary(declared_config,
                                                             declared_program):
    plan = compile_scenario_plan(declared_program)
    values = {"/request_id": "R-100", "/status": "pending"}
    request = _request(declared_program, plan, "request", values)
    raw = {"utterance": "请订票", "request_id": "WRONG", "status": "pending"}
    final = {"utterance": "请订票", "request_id": "R-100", "status": "pending"}
    size = len(canonical_json(final).encode("utf-8"))
    exact_limits = replace(request.limits, rendered_payload_bytes=size)
    request = replace(request, limits=exact_limits)
    services, _engine, _metrics = _services(declared_config, raw)
    assert asyncio.run(render_event(request, services)) == final
    request = replace(request, limits=replace(exact_limits, rendered_payload_bytes=size - 1))
    with pytest.raises(GenerationAttemptRejected) as caught:
        asyncio.run(render_event(request, services))
    assert caught.value.kind == "frame_schema"


def test_render_uses_program_bound_request_limits_not_service_config(declared_config,
                                                                      declared_program):
    """服务配置中的冲突上限不能改变已编译程序请求的接受行为。"""
    plan = compile_scenario_plan(declared_program)
    values = {"/request_id": "R-100", "/status": "pending"}
    request = _request(declared_program, plan, "request", values)
    raw = {"utterance": "请订票", "request_id": "WRONG", "status": "pending"}
    poisoned_limits = replace(request.limits, rendered_payload_bytes=1)
    poisoned_sequence = replace(
        declared_config.sequence_generation, limits=poisoned_limits
    )
    poisoned = replace(declared_config, sequence_generation=poisoned_sequence)
    services, _engine, _metrics = _services(poisoned, raw)
    assert asyncio.run(render_event(request, services))["request_id"] == "R-100"


def test_frame_prompt_value_limit_rejects_before_provider(
    declared_config,
    declared_program,
):
    """完整动态提示值 D+1 在 renderer 派发前终止。"""
    plan = compile_scenario_plan(declared_program)
    values = {"/request_id": "R-100", "/status": "pending"}
    request = _request(declared_program, plan, "request", values)
    limit = declared_config.sequence_generation.limits.prompt_value_bytes
    request = replace(request, event_plan=replace(request.event_plan, intent="x" * (limit + 1)))
    services, engine, _metrics = _services(declared_config, {})
    with pytest.raises(ContextOverflowError):
        asyncio.run(render_event(request, services))
    assert engine.calls == 0


def test_missing_schema_and_binding_key_mismatch_are_zero_llm_internal_errors(
    declared_config, declared_program,
):
    plan = compile_scenario_plan(declared_program)
    values = {"/request_id": "R-100", "/status": "pending"}
    request = _request(declared_program, plan, "request", values)
    missing = replace(request, frame_spec=replace(request.frame_spec, gen_schema=None))
    services, engine, metrics = _services(declared_config, {})
    with pytest.raises(InternalError, match="frame schema is missing"):
        asyncio.run(render_event(missing, services))
    assert engine.calls == 0 and metrics.counters == {}
    mismatched = replace(request, binding_values={"/request_id": "R-100"})
    with pytest.raises(InternalError, match="binding keys"):
        asyncio.run(render_event(mismatched, services))
    assert engine.calls == 0 and metrics.counters == {}

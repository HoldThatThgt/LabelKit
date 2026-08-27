"""v1.20 帧 payload 与独立 noise 时间终验渲染器。"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Mapping

from jsonpointer import JsonPointerException, resolve_pointer
from jsonschema import Draft202012Validator

from labelkit.common.config._temporal import (
    inject_temporal_values,
    project_temporal_instance,
    resolve_frame_time_values,
)
from labelkit.common.config.model import FrameClassView
from labelkit.common.contracts.generation import (
    GenerationServices,
    NoiseRenderRequest,
    PlannedEvent,
    RenderEventRequest,
)
from labelkit.common.errors import InternalError, SchemaViolation
from labelkit.common.inference.generation_prompts import (
    enforce_prompt_value_limit,
    frame_render_prompt,
    noise_render_prompt,
)
from labelkit.common.inference.schema_engine import (
    CallScope,
    CandidateFinalizerContractError,
    FinalizedCallRequest,
)
from labelkit.operators.generation import GenerationAttemptRejected
from labelkit.operators.generation.project import canonical_json


_log = logging.getLogger("labelkit.generation.render")


@dataclass(frozen=True)
class _TemporalFinalizer:
    """一次帧候选的冻结时间注入器。"""

    paths: tuple[str, ...]                       # 完整 Schema 中的时间路径
    values: tuple[tuple[str, object], ...]        # 路径到 Planner 机械值

    def __call__(self, candidate: Mapping[str, object]) -> Mapping[str, object]:
        """注入时间值并证明非时间 payload 未改写。"""
        projected = project_temporal_instance(candidate, self.paths)
        if canonical_json(projected) != canonical_json(candidate):
            raise ValueError("model candidate contains a business time field")
        finalized = inject_temporal_values(candidate, dict(self.values))
        if canonical_json(project_temporal_instance(finalized, self.paths)) != canonical_json(candidate):
            raise ValueError("temporal finalizer changed a non-time field")
        return finalized


@dataclass(frozen=True)
class _TemporalProjector:
    """L3 只读可达时间叶子的 total 投影器。"""

    paths: tuple[str, ...]                       # 需从 previous_output 删除的路径

    def __call__(self, candidate: Mapping[str, object]) -> Mapping[str, object]:
        """@return 不创建或替换 parent 的 model-space 副本。"""
        return project_temporal_instance(candidate, self.paths)


async def render_event(
    request: RenderEventRequest,
    services: GenerationServices,
) -> Mapping[str, object]:
    """让模型只生成非时间 payload，然后机械注入 Planner 时间。

    @param request 帧渲染请求。
    @param services 生成服务根。
    @return 通过完整帧 Schema 复验的 payload。
    """
    full_schema, model_schema = _frame_schemas(request.frame_spec, request.slot_key)
    _validate_render_request(request)
    finalizer, projector = _temporal_transforms(
        request.frame_spec, request.planned_event, request.utc_offset_minutes,
    )
    services.metrics.count("generate.sequence.calls.frame_render_calls")
    try:
        result = await services.schema_engine.complete_finalized(FinalizedCallRequest(
            profile=request.semantic_profile,
            prompt=_frame_prompt(request, request.limits),
            model_schema=model_schema,
            final_schema=full_schema,
            scope=CallScope(
                record_ids=(request.planned_event.event_key,),
                repair_context_bytes=request.limits.repair_context_bytes,
            ),
            candidate_finalizer=finalizer,
            repair_projector=projector,
        ))
    except SchemaViolation:
        raise GenerationAttemptRejected("frame_schema", request.slot_key) from None
    except CandidateFinalizerContractError:
        _contract_error("frame candidate finalizer contract failed")
    payload = result[0]
    if not _state_bindings_match(payload, request):
        _reject_frame(request.slot_key, "state payload binding differs from its planned value")
    limit = request.limits.rendered_payload_bytes
    if len(canonical_json(payload).encode("utf-8")) > limit:
        _reject_frame(request.slot_key, "rendered payload exceeds the frozen byte limit")
    return payload


async def render_noise(
    request: NoiseRenderRequest,
    services: GenerationServices,
) -> Mapping[str, object]:
    """使用 semantic profile 渲染一个独立 noise 对象。

    @param request noise 渲染请求。
    @param services 生成服务根。
    @return 通过完整 noise 帧 Schema 的 payload。
    """
    slot_key = f"noise/{request.noise_slot.ordinal:06d}"
    full_schema, model_schema = _frame_schemas(request.frame_spec, slot_key)
    _validate_noise_request(request)
    planned = PlannedEvent(
        request.noise_slot.event_key, "noise", request.noise_slot.ordinal, 0,
        request.noise_slot.timestamp_us, request.noise_slot.duration_us,
        request.noise_slot.resources, request.noise_slot.session_id,
    )
    finalizer, projector = _temporal_transforms(
        request.frame_spec, planned, request.utc_offset_minutes,
    )
    services.metrics.count("generate.sequence.calls.noise_render_calls")
    try:
        result = await services.schema_engine.complete_finalized(FinalizedCallRequest(
            profile=request.semantic_profile,
            prompt=_noise_prompt(request),
            model_schema=model_schema,
            final_schema=full_schema,
            scope=CallScope(
                record_ids=(request.noise_slot.event_key,),
                repair_context_bytes=request.limits.repair_context_bytes,
            ),
            candidate_finalizer=finalizer,
            repair_projector=projector,
        ))
    except SchemaViolation:
        raise GenerationAttemptRejected("noise_schema", slot_key) from None
    except CandidateFinalizerContractError:
        _contract_error("noise candidate finalizer contract failed")
    payload = result[0]
    limit = request.limits.rendered_payload_bytes
    if len(canonical_json(payload).encode("utf-8")) > limit:
        raise GenerationAttemptRejected("noise_schema", slot_key)
    return payload


def _frame_prompt(request: RenderEventRequest, limits):
    """按冻结插值序构造 frame render prompt。"""
    event, plan = request.planned_event, request.event_plan
    bindings = [
        {"payload_path": item.payload_path, "value": request.binding_values[item.payload_path]}
        for item in (() if request.role is None else request.role.payload_bindings)
    ]
    dynamic = {
        "intent": plan.intent,
        "actor_view": request.actor_view,
        "public_facts": request.public_facts,
        "publish_snapshot": request.publish_snapshot,
        "binding_values": bindings,
    }
    if request.role is None:
        dynamic["actor"] = plan.actor
    enforce_prompt_value_limit(request.semantic_profile, limits.prompt_value_bytes, dynamic)
    enforce_prompt_value_limit(
        request.semantic_profile, limits.event_patch_bytes, {"patch": plan.patch}
    )
    return frame_render_prompt({
        "slot_key": request.slot_key, "attempt_index": request.attempt_index,
        "event_key": event.event_key, "role": event.role, "position": event.position,
        "frame_class": plan.frame_class, "actor": plan.actor,
        "logical_time_us": event.logical_time_us,
        "wait_since_previous_us": request.actor_view.wait_since_previous_us,
        "intent": plan.intent, "patch": plan.patch, "actor_view": request.actor_view,
        "public_facts": request.public_facts, "publish_snapshot": request.publish_snapshot,
        "state_before_hash": request.state_before_hash,
        "state_after_hash": request.state_after_hash,
        "frame_instruction": request.frame_spec.gen_instruction,
        "frame_description": request.frame_spec.description,
        "binding_values": bindings,
        "planned_start_us": event.timestamp_us,
        "planned_end_us": event.timestamp_us + event.duration_us,
        "planned_duration_us": event.duration_us,
        "frame_schema": request.frame_spec.model_gen_schema,
    })


def _noise_prompt(request: NoiseRenderRequest):
    """按冻结插值序构造 noise render prompt。"""
    slot = request.noise_slot
    return noise_render_prompt({
        "event_key": slot.event_key, "noise_ordinal": slot.ordinal,
        "attempt_index": request.attempt_index, "frame_class": slot.frame_class,
        "planned_start_us": slot.timestamp_us,
        "planned_end_us": slot.timestamp_us + slot.duration_us,
        "planned_duration_us": slot.duration_us,
        "session_id": slot.session_id,
        "class_descriptions": request.class_descriptions,
        "frame_descriptions": request.frame_descriptions,
        "planned_topic": slot.topic,
        "noise_instruction": request.noise_spec.instruction,
        "frame_instruction": request.frame_spec.gen_instruction,
        "frame_schema": request.frame_spec.model_gen_schema,
    })


def _validate_render_request(request: RenderEventRequest) -> None:
    """在零 LLM 交互前检查 renderer 的闭集身份与 binding key。"""
    role = request.role
    if role is not None and (
        request.event_plan.frame_class != role.frame_class
        or request.event_plan.actor != role.actor
    ):
        _contract_error("render request differs from its declared role")
    expected = [] if role is None else [item.payload_path for item in role.payload_bindings]
    if list(request.binding_values) != expected:
        _contract_error("render binding keys differ from RoleSpec")
    frame = request.frame_spec
    event = request.planned_event
    if event.duration_us != frame.duration_us or event.resources != frame.resources:
        _contract_error("planned event interval differs from its frame class")


def _state_bindings_match(payload: Mapping[str, object], request: RenderEventRequest) -> bool:
    """验证模型已按状态真值填写非时间 payload binding。"""
    role = request.role
    if role is None:
        return True
    for item in role.payload_bindings:
        try:
            actual = resolve_pointer(payload, item.payload_path)
            expected = request.binding_values[item.payload_path]
        except (JsonPointerException, KeyError, TypeError):
            return False
        if canonical_json(actual) != canonical_json(expected):
            return False
    return True


def _validate_noise_request(request: NoiseRenderRequest) -> None:
    """在零 LLM 交互前验证 noise 的点事件身份。"""
    slot = request.noise_slot
    if slot.duration_us or slot.resources or request.frame_spec.duration_us or request.frame_spec.resources:
        _contract_error("noise render requires a point frame class")


def _temporal_transforms(frame: FrameClassView, event: PlannedEvent,
                         utc_offset_minutes: int) -> tuple[_TemporalFinalizer, _TemporalProjector]:
    """从冻结帧类和计划事件构造唯一时间变换对。"""
    try:
        values = resolve_frame_time_values(
            frame.time_bindings, event.timestamp_us, event.duration_us, utc_offset_minutes,
        )
    except ValueError:
        _contract_error("planned frame time binding values are invalid")
    if tuple(values) != frame.business_time_paths:
        _contract_error("frame time binding order differs from its Schema paths")
    return (
        _TemporalFinalizer(frame.business_time_paths, tuple(values.items())),
        _TemporalProjector(frame.business_time_paths),
    )


def rebind_temporal_payload(payload: Mapping[str, object], frame: FrameClassView,
                            event: PlannedEvent, utc_offset_minutes: int) -> dict[str, object]:
    """把 protected payload 的时间叶子重绑到当前 branch 计划。

    @param payload baseline 最终 payload
    @param frame payload 对应的冻结帧类
    @param event 当前 branch 计划事件
    @param utc_offset_minutes timeline 固定 UTC offset
    @return 非时间内容不变且通过完整 Schema 的 payload
    """
    finalizer, _projector = _temporal_transforms(frame, event, utc_offset_minutes)
    model_payload = project_temporal_instance(payload, frame.business_time_paths)
    try:
        rebound = dict(finalizer(model_payload))
    except ValueError:
        _contract_error("protected payload temporal rebinding failed")
    schema = _frame_schema(frame.gen_schema, event.event_key)
    if next(Draft202012Validator(schema).iter_errors(rebound), None) is not None:
        _contract_error("protected payload failed the complete frame schema")
    return rebound


def time_binding_descriptor(frame: FrameClassView) -> tuple[dict[str, str], ...]:
    """投影帧类的规范 time binding descriptor。

    @param frame 冻结帧类
    @return 按工程声明序的可序列化 descriptor
    """
    return tuple({"payload_path": item.payload_path, "source": item.source}
                 for item in frame.time_bindings)


def _frame_schema(value, slot_key: str) -> dict[str, object]:
    """读取完整帧 Schema；缺失表示内部契约破坏。"""
    if value is None:
        _contract_error("frame schema is missing")
    return _json_copy(value)


def _frame_schemas(frame: FrameClassView,
                   slot_key: str) -> tuple[dict[str, object], dict[str, object]]:
    """读取一个帧类的完整/model Schema 对。"""
    full = _frame_schema(frame.gen_schema, slot_key)
    model = _frame_schema(frame.model_gen_schema, slot_key)
    return full, model


def _json_copy(value):
    """复制冻结或可变 JSON 值。"""
    return json.loads(canonical_json(value))


def _reject_frame(slot_key: str, reason: str):
    """记录无数据原因并拒绝当前帧。"""
    _log.warning("frame render rejected: %s", reason)
    raise GenerationAttemptRejected("frame_schema", slot_key)


def _contract_error(reason: str):
    """记录并抛出 renderer 内部契约错误。"""
    _log.error("generation_downstream_contract: %s", reason)
    raise InternalError(f"generation_downstream_contract: {reason}")

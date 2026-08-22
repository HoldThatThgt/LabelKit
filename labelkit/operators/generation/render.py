"""v1.18 帧 payload 与独立 noise 渲染器。"""
from __future__ import annotations

import json
import logging
from typing import Mapping

import jsonpatch
from jsonpointer import JsonPointerException

from labelkit.common.contracts.generation import (
    GenerationServices,
    NoiseRenderRequest,
    RenderEventRequest,
)
from labelkit.common.errors import InternalError, SchemaViolation
from labelkit.common.inference.generation_prompts import (
    enforce_prompt_value_limit,
    frame_render_prompt,
    noise_render_prompt,
)
from labelkit.common.inference.schema_engine import CallScope
from labelkit.operators.generation import GenerationAttemptRejected
from labelkit.operators.generation.project import canonical_json


_log = logging.getLogger("labelkit.generation.render")
async def render_event(
    request: RenderEventRequest,
    services: GenerationServices,
) -> Mapping[str, object]:
    """渲染对象 payload，并按声明序机械覆盖 payload binding。

    @param request 帧渲染请求。
    @param services 生成服务根。
    @return 通过完整帧 Schema 复验的 payload。
    """
    schema = _frame_schema(request.frame_spec.gen_schema, request.slot_key)
    _validate_render_request(request)
    services.metrics.count("generate.sequence.calls.frame_render_calls")
    try:
        result = await services.schema_engine.complete_validated(
            request.semantic_profile,
            _frame_prompt(request, request.limits),
            schema=schema,
            scope=CallScope(
                record_ids=(request.planned_event.event_key,),
                repair_context_bytes=(
                    request.limits.repair_context_bytes
                ),
            ),
        )
    except SchemaViolation:
        raise GenerationAttemptRejected("frame_schema", request.slot_key) from None
    payload = _apply_bindings(result[0], request)
    if services.schema_engine.validate_only(payload, schema=schema):
        _reject_frame(request.slot_key, "bound payload failed the complete frame schema")
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
    schema = _frame_schema(request.frame_spec.gen_schema, slot_key)
    services.metrics.count("generate.sequence.calls.noise_render_calls")
    try:
        result = await services.schema_engine.complete_validated(
            request.semantic_profile,
            _noise_prompt(request),
            schema=schema,
            scope=CallScope(
                record_ids=(request.noise_slot.event_key,),
                repair_context_bytes=(
                    request.limits.repair_context_bytes
                ),
            ),
        )
    except SchemaViolation:
        raise GenerationAttemptRejected("noise_schema", slot_key) from None
    payload = _json_copy(result[0])
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
        "binding_values": bindings, "frame_schema": request.frame_spec.gen_schema,
    })


def _noise_prompt(request: NoiseRenderRequest):
    """按冻结插值序构造 noise render prompt。"""
    slot = request.noise_slot
    return noise_render_prompt({
        "event_key": slot.event_key, "noise_ordinal": slot.ordinal,
        "attempt_index": request.attempt_index, "frame_class": slot.frame_class,
        "timestamp_us": slot.timestamp_us, "session_id": slot.session_id,
        "class_descriptions": request.class_descriptions,
        "frame_descriptions": request.frame_descriptions,
        "planned_topic": slot.topic,
        "noise_instruction": request.noise_spec.instruction,
        "frame_instruction": request.frame_spec.gen_instruction,
        "frame_schema": request.frame_spec.gen_schema,
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


def _apply_bindings(payload, request: RenderEventRequest) -> dict[str, object]:
    """按 RoleSpec 声明序以 RFC 6902 add 机械覆盖 payload。"""
    value = _json_copy(payload)
    role = request.role
    if role is None:
        return value
    try:
        for item in role.payload_bindings:
            operation = [{
                "op": "add",
                "path": item.payload_path,
                "value": _json_copy(request.binding_values[item.payload_path]),
            }]
            value = jsonpatch.apply_patch(value, operation, in_place=False)
    except (
        jsonpatch.JsonPatchException,
        JsonPointerException,
        KeyError,
        TypeError,
        ValueError,
    ):
        _reject_frame(request.slot_key, "mechanical payload binding failed")
    return value


def _frame_schema(value, slot_key: str) -> dict[str, object]:
    """读取完整帧 Schema；缺失表示内部契约破坏。"""
    if value is None:
        _contract_error("frame schema is missing")
    return _json_copy(value)


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

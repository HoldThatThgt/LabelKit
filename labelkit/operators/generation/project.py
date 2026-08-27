"""v1.20 generation ID、双视图投影与提交前机械时间对账。"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
from collections.abc import Mapping as MappingABC
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

from jsonpointer import JsonPointer, JsonPointerException
from jsonschema import Draft202012Validator

from labelkit.common.config._temporal import (
    inject_temporal_values,
    project_temporal_instance,
    resolve_frame_time_values,
)
from labelkit.common.config.generation import is_generation_frame_eligible
from labelkit.common.contracts.generation import (
    CrossViewDelta,
    DeliverySlot,
    GenerationProgram,
    NoiseCandidateReconcileRequest,
    NoiseProjectionRequest,
    PlannedEvent,
    PreparedCandidate,
    PreparedNoiseCandidate,
    PrimaryCandidateReconcileRequest,
    ProjectionWitness,
    ProjectedSequence,
    ProjectionRequest,
    ReconcileRequest,
    ResourceInterval,
    ReplayProjectionRequest,
    ReplayRows,
    ScenarioPlan,
)
from labelkit.common.contracts.types import Record, RecordRef
from labelkit.common.errors import GenerationProjectionMismatch, InternalError


_log = logging.getLogger("labelkit.generation.project")
_GENERATION_HEADER = "labelkit:v1.20"


def canonical_json(value: object) -> str:
    """返回 generation 使用的规范 JSON。

    @param value JSON-compatible 值。
    @return 键序稳定且无空白的 JSON。
    """
    return json.dumps(
        _thaw_json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def generation_digest(domain: str, value: object) -> str:
    """以 v1.20 域分离 canonical JSON 计算完整 SHA-256。

    @param domain 冻结 generation 域。
    @param value 当前域的规范材料。
    @return 64 位小写十六进制摘要。
    """
    material = canonical_json([_GENERATION_HEADER, domain, value])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def generation_random(domain: str, value: object) -> int:
    """从 v1.20 generation digest 派生确定性完整整数。

    @param domain 冻结随机域。
    @param value 当前域的规范材料。
    @return SHA-256 摘要对应的非负整数。
    """
    return int(generation_digest(domain, value), 16)


def _thaw_json(value: object) -> object:
    """把冻结 carrier 的 JSON 子树递归复制为可变容器。

    @param value MappingProxyType/tuple 或 JSON 标量。
    @return 不与输入共享容器的 JSON 值。
    """
    if dataclasses.is_dataclass(value):
        return {
            field.name: _thaw_json(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, MappingABC):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw_json(item) for item in value]
    return value


def derive_generation_id(domain: str, components: Sequence[object]) -> str:
    """以域分离 canonical JSON 组件派生 generation ID。

    @param domain 冻结 ID 域。
    @param components 按规范顺序排列的组件。
    @return 32 位小写十六进制 ID。
    """
    return generation_digest(domain, list(components))[:32]


def scenario_plan_digest(plan: ScenarioPlan) -> str:
    """计算排除 digest 自身的完整 ScenarioPlan 摘要。

    @param plan 待校验或尚未写入 digest 的冻结计划
    @return 64 位小写十六进制摘要
    """
    blocks = [[
        {
            "slot_key": key[0],
            "variant_name": key[1],
            "events": [dataclasses.asdict(event) for event in events],
        }
        for key, events in block.items()
    ] for block in plan.blocks]
    material = {
        "blocks": blocks,
        "delivery_slots": [dataclasses.asdict(item) for item in plan.delivery_slots],
        "noise_slots": [dataclasses.asdict(item) for item in plan.noise_slots],
        "replay_layouts": [dataclasses.asdict(item) for item in plan.replay_layouts],
        "primary_sessions": plan.primary_sessions,
    }
    return generation_digest("scenario_plan", material)


def validate_planned_events(
    program: GenerationProgram,
    slot: DeliverySlot,
    variant_name: str | None,
    events: Sequence[PlannedEvent],
) -> None:
    """把一个 branch 的位置、role 与 event key 重新绑定到程序。

    @param program 当前冻结生成程序
    @param slot 当前交付槽
    @param variant_name 当前 branch 变体名；hidden baseline 与 instruction-only 为 None
    @param events 当前 branch 的完整事件序列
    @return None；任何不一致均抛终态 InternalError
    """
    scenario_domain = (
        "declared_scenario_id" if program.mode == "declared" else "instruction_scenario_id"
    )
    scenario_id = derive_generation_id(
        scenario_domain, [program.digest, slot.source_name, slot.scenario_index]
    )
    expected_roles = _expected_branch_roles(program, slot, variant_name, len(events))
    seen_keys: set[str] = set()
    for position, (event, expected_role) in enumerate(zip(events, expected_roles)):
        if program.mode == "instruction_only":
            expected_key = derive_generation_id("instruction_event_key", [
                scenario_id, slot.source_name, slot.scenario_index, position,
            ])
        else:
            expected_key = derive_generation_id(
                "declared_event_key", [scenario_id, expected_role]
            )
        if event.position != position or event.role != expected_role:
            _contract_error("planned event position or role is not bound to the program")
        if event.event_key != expected_key:
            _contract_error("planned event key is not bound to the program")
        if event.event_key in seen_keys:
            _contract_error("planned event key is duplicated within a branch")
        seen_keys.add(event.event_key)


def _expected_branch_roles(program, slot, variant_name, event_count: int) -> tuple[str, ...]:
    """从程序独立重建一个 branch 的精确 role word。"""
    if program.mode == "instruction_only":
        source = next(
            (item for item in program.instruction_only if item.name == slot.source_name), None
        )
        if source is None or not source.len_range[0] <= event_count <= source.len_range[1]:
            _contract_error("instruction branch event count differs from the program")
        return tuple(f"position_{position:03d}" for position in range(event_count))
    pattern = program.patterns.get(slot.pattern_name)
    source = next(
        (item for item in program.counterfactual_sets if item.name == slot.source_name), None
    )
    if pattern is None or source is None:
        _contract_error("declared branch source is absent from the program")
    roles = list(pattern.order)
    if variant_name is not None:
        variant = next((item for item in source.variants if item.name == variant_name), None)
        if variant is None:
            _contract_error("declared branch variant is absent from the program")
        if variant.kind == "missing":
            roles.remove(variant.target["role"])
        elif variant.kind == "reordered":
            before = roles.index(variant.target["before"])
            after = roles.index(variant.target["after"])
            roles[before], roles[after] = roles[after], roles[before]
    if len(roles) != event_count:
        _contract_error("declared branch event count differs from the program")
    return tuple(roles)


def validate_plan_identity(program: GenerationProgram, plan: ScenarioPlan) -> None:
    """以 program-bound seed 重建并逐字段复验 canonical ScenarioPlan。

    @param program 当前冻结生成程序
    @param plan 待执行或最终对账的冻结计划
    @return None；任何不一致均抛终态 InternalError
    """
    from labelkit.operators.generation.planner import compile_scenario_plan
    from labelkit.operators.generation.program import generation_program_digest

    if program.digest != generation_program_digest(program):
        _contract_error("generation program digest is invalid")
    if plan.digest != scenario_plan_digest(plan):
        _contract_error("scenario plan digest is invalid")
    if plan != compile_scenario_plan(program):
        _contract_error("scenario plan differs from canonical planner output")


def _witness_digest(domain: str, value: object) -> str:
    """以冻结 domain 对 canonical source value 计算完整 SHA-256。"""
    return generation_digest(domain, value)


def noise_payload_digest(payload: Mapping[str, object]) -> str:
    """计算 post-gate noise payload 的 compact source witness。

    @param payload 已通过完整 Schema 与独立语义 gate 的 noise object。
    @return 不含源内容的完整 SHA-256。
    """
    return _witness_digest("noise_payload", payload)


def canonical_delivery_row(row: Mapping[str, object]) -> bytes:
    """移除发射期墙钟字段后返回 canonical UTF-8 行。

    @param row 待摘要的输出行。
    @return 规范行字节。
    """
    value = _thaw_json(row)
    meta = value.get("_meta")
    if isinstance(meta, dict):
        run = meta.get("run")
        if isinstance(run, dict):
            for key in ("started_at", "finished_at", "duration_ms"):
                run.pop(key, None)
    return canonical_json(value).encode("utf-8")


def project_trace(request: ProjectionRequest) -> ProjectedSequence:
    """把已接受 primary trace 投影为下游 Record 与基础 stream rows。

    @param request 当前 slot 与 EventTrace。
    @return 下游前双视图投影。
    """
    validate_plan_identity(request.program, request.plan)
    return _project_trace_from_validated_plan(request)


def _project_trace_from_validated_plan(request: ProjectionRequest) -> ProjectedSequence:
    """仅供已在运行边界验证过完整 plan 的控制器投影 trace。"""
    _validate_projection_request(request)
    generation = _generation_truth(request)
    event_ids = [event.event_id for event in request.trace.events]
    sequence_id = derive_generation_id(
        "sequence_id", [request.trace.world_branch_id, event_ids]
    )
    rows = tuple(
        _primary_row(event, sequence_id, generation, request.program)
        for event in request.trace.events
    )
    members = tuple(_member_record(row) for row in rows)
    record = Record(
        id=sequence_id,
        modality="text",
        text=None,
        raw={"_meta": {"generation": generation}},
        ui_tree=None,
        image=None,
        ref=_generated_ref(),
        kind="sequence",
        members=members,
    )
    return ProjectedSequence(main_record=record, primary_stream_rows=rows)


def projection_witness(projection: ProjectedSequence) -> ProjectionWitness:
    """把 attempt-local projector 内容压缩为 CrossView 源证明。

    @param projection 尚未释放的不可变 ProjectedSequence。
    @return 不含 payload、Record 或 row 的 full-SHA-256 witness。
    """
    generation = _projection_generation(projection)
    member_sources = [_member_source(member) for member in projection.main_record.members]
    base = tuple(_witness_digest("projection_primary_base", _primary_base(row))
                 for row in projection.primary_stream_rows)
    return ProjectionWitness(
        projection.main_record.id,
        _witness_digest("projection_main_generation", generation),
        _witness_digest("projection_member_sources", member_sources),
        base,
    )


def _primary_base(row) -> dict[str, object]:
    """提取 projector 与最终 primary 共享的三个基础字段。"""
    meta = row.get("_meta") if isinstance(row, MappingABC) else None
    if not isinstance(meta, MappingABC):
        _projection_mismatch("primary base metadata is missing")
    return {
        "payload": row.get("payload"),
        "event": meta.get("event"),
        "generation": meta.get("generation"),
    }


def _time_descriptor(frame) -> list[dict[str, str]]:
    """把帧类时间声明投影为 stream 自描述表。"""
    return [
        {"payload_path": binding.payload_path, "source": binding.source}
        for binding in frame.time_bindings
    ]


def _frame_for_event(program, frame_class: object):
    """读取一条 event 对应的生成帧类并拒绝未知名称。"""
    frame = program.frame_classes.get(frame_class) if isinstance(frame_class, str) else None
    if frame is None or not isinstance(frame.gen_schema, MappingABC):
        _contract_error("event frame class has no complete generation Schema")
    return frame


def _validate_payload_time(program, frame, payload, timestamp_us: int, duration_us: int) -> None:
    """独立复验 payload 机械时间、非时间闭包与完整 Schema。"""
    if not isinstance(payload, MappingABC):
        _temporal_contract_error("event payload is not an object")
    try:
        values = resolve_frame_time_values(
            frame.time_bindings,
            timestamp_us,
            duration_us,
            program.timeline.utc_offset_minutes,
        )
        projected = project_temporal_instance(payload, frame.business_time_paths)
        rebound = inject_temporal_values(projected, values)
    except ValueError:
        _temporal_contract_error("event payload time binding cannot be reconstructed")
    if canonical_json(rebound) != canonical_json(payload):
        _temporal_contract_error("event payload time differs from the plan")
    if any(Draft202012Validator(_thaw_json(frame.gen_schema)).iter_errors(_thaw_json(payload))):
        _temporal_contract_error("event payload fails its complete frame Schema")


def _rebound_payload(program, frame, payload, timestamp_us: int, duration_us: int) -> dict:
    """删除 source 时间叶并按 replay 起点机械注入完整 payload。"""
    try:
        model_payload = project_temporal_instance(payload, frame.business_time_paths)
        values = resolve_frame_time_values(
            frame.time_bindings,
            timestamp_us,
            duration_us,
            program.timeline.utc_offset_minutes,
        )
        rebound = inject_temporal_values(model_payload, values)
    except ValueError:
        _contract_error("replay payload time rebinding failed")
    if any(Draft202012Validator(_thaw_json(frame.gen_schema)).iter_errors(rebound)):
        _contract_error("replay payload fails its complete frame Schema")
    return rebound


def _validate_projection_request(request: ProjectionRequest) -> None:
    """校验 trace 与当前交付槽一致。

    @param request 当前投影请求。
    @return None。
    """
    trace = request.trace
    slot = request.slot
    if sum(item == slot for item in request.plan.delivery_slots) != 1:
        _contract_error("projection slot is absent from the scenario plan")
    valid_variant = trace.variant_name in slot.variant_names
    if request.program.mode == "instruction_only":
        valid_variant = trace.variant_name is None and slot.variant_names == ()
    if (
        trace.sequence_class != slot.sequence_class
        or trace.pattern_name != slot.pattern_name
        or not valid_variant
        or not trace.events
    ):
        _contract_error("trace does not match its delivery slot")
    _validate_trace_identity(request)
    _validate_trace_evaluations(request)


def _validate_trace_identity(request: ProjectionRequest) -> None:
    """从程序与已验证 branch 重派 trace、事件和时间身份。"""
    trace = request.trace
    planned = _projection_branch(request)
    if len(trace.events) != len(planned):
        _contract_error("trace event count differs from its planned branch")
    validate_planned_events(request.program, request.slot, trace.variant_name, planned)
    scenario_id, world_id = _branch_ids(
        request.program, request.slot, trace.variant_name
    )
    if trace.scenario_id != scenario_id or trace.world_branch_id != world_id:
        _contract_error("trace branch identity is not bound to the program")
    for event, witness in zip(trace.events, planned, strict=True):
        frame = _frame_for_event(request.program, event.frame_class)
        if request.program.mode == "instruction_only":
            noise = request.program.noise
            if (not is_generation_frame_eligible(frame)
                    or (noise is not None and event.frame_class == noise.frame_class)):
                _contract_error("instruction trace frame class is outside the registry")
            if event.actor not in trace.scenario_seed.actors:
                _contract_error("instruction trace actor is outside the scenario")
        planned_fields = (
            event.event_key == witness.event_key,
            event.role == witness.role,
            event.logical_time_us == witness.logical_time_us,
            event.timestamp_us == witness.timestamp_us,
            event.duration_us == witness.duration_us,
            frame.duration_us == witness.duration_us,
            tuple(frame.resources) == tuple(witness.resources),
        )
        descriptor = _time_descriptor(frame)
        expected_id = derive_generation_id(
            "primary_event_id",
            [
                world_id,
                witness.event_key,
                witness.timestamp_us,
                witness.duration_us,
                list(witness.resources),
                descriptor,
                event.payload,
            ],
        )
        if not all(planned_fields) or event.event_id != expected_id:
            _contract_error("trace event identity differs from its planned branch")
        _validate_payload_time(
            request.program, frame, event.payload, witness.timestamp_us, witness.duration_us
        )


def _projection_branch(request: ProjectionRequest) -> tuple[PlannedEvent, ...]:
    """从请求的唯一完整计划解析当前 branch。"""
    key = (request.slot.slot_key, request.trace.variant_name)
    branches = [block[key] for block in request.plan.blocks if key in block]
    if len(branches) != 1:
        _contract_error("trace planned branch is missing or duplicated")
    return branches[0]


def _validate_trace_evaluations(request: ProjectionRequest) -> None:
    """复验投影入口携带的独立 gate 结论与事件真值一致。"""
    trace = request.trace
    state = trace.state_evaluation
    semantic = trace.semantic_evaluation
    state_valid = (
        state.replay_hash == state.final_state_hash
        and state.bindings_valid
        and state.outcome_valid
        and state.protected_prefix_valid
    )
    semantic_valid = (
        semantic.causal_consistency
        and semantic.actor_knowledge
        and semantic.goal_consistency
        and semantic.temporal_plausibility
        and semantic.cross_frame_consistency
        and semantic.realism
        and not semantic.reason_codes
    )
    if not state_valid or not semantic_valid:
        _contract_error("trace carries a failed evaluation")
    if request.program.mode == "instruction_only":
        if trace.pattern_evaluation is not None:
            _contract_error("instruction-only trace carries a pattern evaluation")
        return
    _validate_declared_trace_truth(request)


def _validate_declared_trace_truth(request: ProjectionRequest) -> None:
    """复验 declared role word、绑定与预期违规。"""
    trace = request.trace
    evaluation = trace.pattern_evaluation
    variant = _variant_for(request)
    pattern = request.program.patterns.get(request.slot.pattern_name)
    if evaluation is None or pattern is None:
        _contract_error("declared trace truth is incomplete")
    roles = {item.name: item for item in pattern.roles}
    expected_word = _variant_role_word(pattern.order, variant)
    actual_word = tuple(event.role for event in trace.events)
    if actual_word != expected_word:
        _contract_error("declared trace role word differs from its variant")
    for event in trace.events:
        role = roles.get(event.role)
        if role is None or event.frame_class != role.frame_class or event.actor != role.actor:
            _contract_error("declared trace event differs from its role")
    expected_bindings = {event.event_id: event.role for event in trace.events}
    if len(expected_bindings) != len(trace.events):
        _contract_error("declared trace event ids are duplicated")
    if dict(evaluation.actual_bindings) != expected_bindings:
        _contract_error("declared trace bindings differ from event truth")
    expected = () if not variant.expected_violation else (dict(variant.expected_violation),)
    if tuple(dict(item) for item in evaluation.actual_violations) != expected:
        _contract_error("declared trace violations differ from expected truth")


def _variant_role_word(order, variant) -> tuple[str, ...]:
    """从声明 order 与 variant 机械派生实际 role word。"""
    roles = list(order)
    if variant.kind == "missing":
        roles.remove(variant.target["role"])
    elif variant.kind == "reordered":
        left = roles.index(variant.target["before"])
        right = roles.index(variant.target["after"])
        roles[left], roles[right] = roles[right], roles[left]
    return tuple(roles)


def _generation_truth(request: ProjectionRequest) -> dict[str, object]:
    """构造 primary 的 generation truth。

    @param request 当前投影请求。
    @return 按输出契约排序的字典。
    """
    trace, slot = request.trace, request.slot
    if request.program.mode == "instruction_only":
        return {
            "validation_mode": "instruction_only",
            "actor_knowledge_validation": "semantic",
            "instruction_slot": slot.source_name,
            "scenario_index": slot.scenario_index,
            "scenario_id": trace.scenario_id,
            "world_branch_id": trace.world_branch_id,
            "sequence_class": trace.sequence_class,
        }
    variant = _variant_for(request)
    return {
        "validation_mode": "declared",
        "actor_knowledge_validation": "mechanical_and_semantic",
        "scenario_set": slot.source_name,
        "scenario_index": slot.scenario_index,
        "scenario_id": trace.scenario_id,
        "world_branch_id": trace.world_branch_id,
        "sequence_class": trace.sequence_class,
        "pattern": trace.pattern_name,
        "variant": trace.variant_name,
        "expected_violation": dict(variant.expected_violation),
        "actual_violations": [dict(item) for item in trace.pattern_evaluation.actual_violations],
    }


def _variant_for(request: ProjectionRequest):
    """解析投影 branch 的 VariantSpec。

    @param request 当前投影请求。
    @return 唯一 VariantSpec。
    """
    source = next(
        (item for item in request.program.counterfactual_sets
         if item.name == request.slot.source_name),
        None,
    )
    variant = next(
        (item for item in source.variants if item.name == request.trace.variant_name),
        None,
    ) if source is not None else None
    if variant is None or request.trace.pattern_evaluation is None:
        _contract_error("declared projection cannot resolve variant truth")
    return variant


def _primary_row(event, sequence_id: str, generation: Mapping[str, object], program) -> dict:
    """构造一行 primary stream 数据。

    @param event 当前 EventTruth。
    @param sequence_id owner sequence ID。
    @param generation owner generation truth。
    @param program 冻结生成程序。
    @return 完整 primary row。
    """
    frame = _frame_for_event(program, event.frame_class)
    event_meta = {
        "event_id": event.event_id,
        "event_key": event.event_key,
        "owner_sequence_id": sequence_id,
        "role": event.role,
        "frame_class": event.frame_class,
        "actor": event.actor,
        "logical_time_us": event.logical_time_us,
        "timestamp": _timestamp_text(event.timestamp_us, program.timeline.utc_offset_minutes),
        "duration_us": event.duration_us,
        "resources": list(frame.resources),
        "time_bindings": _time_descriptor(frame),
    }
    stream_generation = dict(generation)
    stream_generation.pop("expected_violation", None)
    stream_generation.pop("actual_violations", None)
    return {
        "payload": event.payload,
        "_meta": {"event": event_meta, "generation": stream_generation},
    }


def _member_record(row: Mapping[str, object]) -> Record:
    """从 primary row 构造 sequence member Record。

    @param row 完整 primary stream row。
    @return member Record。
    """
    event = row["_meta"]["event"]
    payload = row["payload"]
    return Record(
        id=event["event_id"],
        modality="text",
        text=canonical_json(payload),
        raw=row,
        ui_tree=None,
        image=None,
        ref=_generated_ref(),
    )


def _generated_ref() -> RecordRef:
    """返回 sequence generation 的通用空输入溯源。

    @return 生成记录溯源。
    """
    return RecordRef(
        source_file="",
        line_no=None,
        pair_index=None,
        generated_from=(),
        generator=None,
    )


def project_noise(request: NoiseProjectionRequest) -> Mapping[str, object]:
    """把一个 NoiseSlot payload 投影为最终 stream row。

    @param request 已接受 noise payload 的投影请求。
    @return 完整 noise row。
    """
    slot = request.noise_slot
    frame = _frame_for_event(request.program, slot.frame_class)
    descriptor = _time_descriptor(frame)
    _validate_payload_time(request.program, frame, request.payload, slot.timestamp_us, 0)
    event_id = derive_generation_id(
        "noise_event_id",
        [
            request.run_id,
            slot.event_key,
            slot.timestamp_us,
            0,
            [],
            descriptor,
            request.payload,
        ],
    )
    event = {
        "event_id": event_id,
        "event_key": slot.event_key,
        "owner_sequence_id": None,
        "role": None,
        "frame_class": slot.frame_class,
        "actor": None,
        "logical_time_us": None,
        "timestamp": _timestamp_text(
            slot.timestamp_us, request.program.timeline.utc_offset_minutes
        ),
        "duration_us": 0,
        "resources": [],
        "time_bindings": descriptor,
        "noise": True,
    }
    return {"payload": request.payload, "_meta": {"event": event, "generation": None}}


def project_replay(request: ReplayProjectionRequest) -> ReplayRows:
    """从最终 source primary rows 投影一次完整 replay。

    @param request 冻结 replay 布局与最终 source rows。
    @return replay rows 与 retained-content 费用。
    """
    validate_plan_identity(request.program, request.plan)
    return _project_replay_from_validated_plan(request)


def _project_replay_from_validated_plan(request: ReplayProjectionRequest) -> ReplayRows:
    """仅供已在运行边界验证过完整 plan 的控制器投影 replay。"""
    _validate_replay_request(request)
    source_rows = request.source.primary_stream_rows
    planned = _branch_events(
        request.plan, request.layout.source_slot_key, request.layout.source_variant_name
    )
    if len(source_rows) != len(planned) or not source_rows:
        _contract_error("replay source row count does not match the planned branch")
    source_id = _single_owner(source_rows)
    replay_id = derive_generation_id(
        "replay_sequence_id", [source_id, request.layout.replay_ordinal]
    )
    rows = tuple(
        _replay_row(request, row, index, source_id, replay_id)
        for index, row in enumerate(source_rows)
    )
    retained = sum(len(canonical_delivery_row(row)) + 1 for row in rows)
    return ReplayRows(rows=rows, retained_content_bytes=retained)


def _validate_replay_request(request: ReplayProjectionRequest) -> None:
    """把 replay layout 与已验证计划、positive 声明和 source truth 精确绑定。"""
    if request.plan.digest != scenario_plan_digest(request.plan):
        _contract_error("replay plan digest is invalid")
    matches = [item for item in request.plan.replay_layouts if item == request.layout]
    if len(matches) != 1:
        _contract_error("replay layout is absent from the planned replay table")
    if request.layout.shift_us <= 0 or request.layout.shift_us % 1000:
        _contract_error("replay layout shift is not a positive millisecond value")
    slot = next(
        (item for item in request.plan.delivery_slots
         if item.slot_key == request.layout.source_slot_key),
        None,
    )
    source = next(
        (item for item in request.program.counterfactual_sets
         if slot is not None and item.name == slot.source_name),
        None,
    )
    variant = next(
        (item for item in source.variants
         if item.name == request.layout.source_variant_name),
        None,
    ) if source is not None else None
    if slot is None or variant is None or variant.kind != "positive":
        _contract_error("replay source is not a planned positive variant")
    generation = request.source.main_row.get("_meta", {}).get("generation", {})
    expected = {
        "scenario_set": slot.source_name,
        "scenario_index": slot.scenario_index,
        "pattern": slot.pattern_name,
        "variant": variant.name,
        "sequence_class": slot.sequence_class,
    }
    if any(generation.get(key) != value for key, value in expected.items()):
        _contract_error("replay source truth differs from its planned positive variant")


def _single_owner(rows: Sequence[Mapping[str, object]]) -> str:
    """读取并验证 source primary 的唯一 owner。

    @param rows source primary rows。
    @return owner sequence ID。
    """
    owners = {row["_meta"]["event"]["owner_sequence_id"] for row in rows}
    if len(owners) != 1 or not isinstance(next(iter(owners)), str):
        _contract_error("replay source has no unique primary owner")
    return next(iter(owners))


def _replay_row(
    request: ReplayProjectionRequest,
    source_row: Mapping[str, object],
    index: int,
    source_id: str,
    replay_id: str,
) -> Mapping[str, object]:
    """重写一行 source primary 成 replay row。

    @param request replay 投影请求。
    @param source_row source primary row。
    @param index source 事件位置。
    @param source_id source sequence ID。
    @param replay_id 新 replay sequence ID。
    @return 完整 replay row。
    """
    row = _thaw_json(source_row)
    source_event = source_row["_meta"]["event"]
    source_generation = source_row["_meta"]["generation"]
    payload, timestamp = _replay_temporal_projection(request, source_row, index)
    event = _replay_event(
        source_event,
        (source_id, replay_id, request.layout.replay_ordinal),
        timestamp,
        payload,
    )
    generation = {
        "validation_mode": "replay",
        "source_validation_mode": source_generation["validation_mode"],
        "sequence_class": source_generation["sequence_class"],
        "scenario_id": source_generation["scenario_id"],
        "source_pattern": source_generation.get("pattern"),
        "source_variant": source_generation.get("variant"),
        "duplicate_of_sequence_id": source_id,
    }
    row["payload"] = payload
    row["_meta"]["event"] = event
    row["_meta"]["generation"] = generation
    return row


def _replay_temporal_projection(request, source_row, index: int) -> tuple[dict, tuple[int, str]]:
    """复验 source 计划时间并机械构造 replay payload 与时间。

    @param request 当前 replay 投影请求。
    @param source_row 当前 source primary row。
    @param index 当前 source member 位置。
    @return rebound payload 与 replay 起点整数、文本。
    """
    source = source_row["_meta"]["event"]
    source_start = _timestamp_us(source["timestamp"])
    planned = _branch_events(
        request.plan, request.layout.source_slot_key, request.layout.source_variant_name
    )[index]
    frame = _frame_for_event(request.program, source.get("frame_class"))
    duration_us = source.get("duration_us")
    if not isinstance(duration_us, int) or isinstance(duration_us, bool):
        _contract_error("replay source duration is invalid")
    valid = (
        source_start == planned.timestamp_us,
        duration_us == planned.duration_us,
        tuple(source.get("resources", ())) == tuple(planned.resources),
        canonical_json(source.get("time_bindings")) == canonical_json(_time_descriptor(frame)),
    )
    if not all(valid):
        _contract_error("replay source temporal facts differ from the plan")
    payload = source_row.get("payload")
    _validate_payload_time(request.program, frame, payload, source_start, duration_us)
    replay_start = source_start + request.layout.shift_us
    rebound = _rebound_payload(request.program, frame, payload, replay_start, duration_us)
    timestamp = _timestamp_text(replay_start, request.program.timeline.utc_offset_minutes)
    return rebound, (replay_start, timestamp)


def _replay_event(
    source,
    identity: tuple[str, str, int],
    timestamp: tuple[int, str],
    payload: Mapping[str, object],
) -> dict:
    """构造 replay 的 event metadata。

    @param source source event metadata。
    @param identity source sequence、replay sequence 与 replay ordinal。
    @param timestamp replay 工件时间的整数与 ISO 文本。
    @param payload 已按 replay 起点重绑的最终 payload。
    @return replay event metadata。
    """
    source_id, replay_id, ordinal = identity
    timestamp_us, timestamp_text = timestamp
    duration_us = source["duration_us"]
    event_id = derive_generation_id("replay_event_id", [
        replay_id, source["event_id"], timestamp_us, duration_us, payload,
    ])
    return {
        "event_id": event_id,
        "event_key": source["event_key"],
        "owner_sequence_id": None,
        "role": source["role"],
        "frame_class": source["frame_class"],
        "actor": source["actor"],
        "logical_time_us": source["logical_time_us"],
        "timestamp": timestamp_text,
        "duration_us": duration_us,
        "resources": list(source["resources"]),
        "time_bindings": [dict(item) for item in source["time_bindings"]],
        "replay_sequence_id": replay_id,
        "replay_ordinal": ordinal,
        "duplicate_of_sequence_id": source_id,
        "duplicate_of_event_id": source["event_id"],
    }


def reconcile_views(request: ReconcileRequest) -> None:
    """机械对账 primary、noise、replay 与全局时间线。

    @param request 全量最终行对账请求。
    @return None；内容不一致时抛可恢复投影拒绝。
    """
    _reconcile_request(request)


def reconcile_primary_candidate(request: PrimaryCandidateReconcileRequest) -> None:
    """只对当前 primary 候选执行闭包与本地 CrossView 校验。

    @param request 不含已提交前缀的当前候选请求。
    @return None；候选内容不一致时抛可恢复投影拒绝。
    """
    try:
        _validate_candidate_slot(request.plan, request.slot)
        targets = _primary_slot_targets(request.plan, request.slot)
        sources = _reconcile_primary(
            request.program,
            request.projection_witnesses,
            request.sequences,
            targets,
        )
        expected_layouts = tuple(
            layout for layout in request.plan.replay_layouts
            if layout.source_slot_key == request.slot.slot_key
        )
        if request.replay_layouts != expected_layouts:
            _projection_mismatch("candidate replay layouts differ from the plan")
        _reconcile_replay(sources, request.replays, request.replay_layouts, request.program)
        replay_rows = [row for replay in request.replays for row in replay.rows]
        rows = [row for sequence in request.sequences for row in sequence.primary_stream_rows]
        rows.extend(replay_rows)
        _reconcile_timeline(rows)
        _reconcile_resource_rows(rows)
        _reconcile_candidate_bytes(request, replay_rows)
    except GenerationProjectionMismatch:
        raise
    except (KeyError, TypeError, ValueError, IndexError):
        _projection_mismatch("primary candidate rows are malformed")


def reconcile_noise_candidate(request: NoiseCandidateReconcileRequest) -> None:
    """只对当前 noise 候选执行 payload、身份、行闭包与字节校验。

    @param request 当前 NoiseSlot 的独立候选请求。
    @return None；候选内容不一致时抛可恢复投影拒绝。
    """
    try:
        _validate_noise_slot(request.program, request.noise_slot)
        _reconcile_noise_row(
            request.row,
            request.payload_digest,
            request.noise_slot,
            (request.program, request.run_id),
        )
        _reconcile_timeline((request.row,))
        if request.retained_content_bytes != _canonical_rows_bytes((request.row,)):
            _projection_mismatch("noise candidate retained-content bytes are invalid")
    except GenerationProjectionMismatch:
        raise
    except (KeyError, TypeError, ValueError, IndexError):
        _projection_mismatch("noise candidate row is malformed")


def _reconcile_request(request: ReconcileRequest) -> None:
    """执行最终全量逐行对账。"""
    validate_plan_identity(request.program, request.plan)
    targets = _primary_targets(request.plan)
    try:
        sources = _reconcile_primary(
            request.program, request.projection_witnesses, request.sequences, targets
        )
        _reconcile_noise(
            request.noise_rows,
            request.noise_payload_digests,
            request.plan.noise_slots,
            (request.program, request.run_id),
        )
        _reconcile_replay(
            sources, request.replays, request.plan.replay_layouts,
            request.program,
        )
        replay_rows = [row for replay in request.replays for row in replay.rows]
        rows = [row for sequence in request.sequences for row in sequence.primary_stream_rows]
        rows.extend(request.noise_rows)
        rows.extend(replay_rows)
        _reconcile_timeline(rows)
        _reconcile_resource_rows(rows)
        _reconcile_retained_bytes(request, replay_rows)
    except GenerationProjectionMismatch:
        raise
    except (KeyError, TypeError, ValueError, IndexError):
        _projection_mismatch("final rows are malformed")


def _validate_candidate_slot(plan: ScenarioPlan, slot: DeliverySlot) -> None:
    """要求候选槽与计划声明表中的唯一槽逐字段相等。"""
    matches = [item for item in plan.delivery_slots if item.slot_key == slot.slot_key]
    if len(matches) != 1 or matches[0] != slot:
        _projection_mismatch("candidate delivery slot differs from the plan")


def _primary_slot_targets(plan, slot) -> tuple[tuple[object, str | None, tuple], ...]:
    """只解析当前槽严格 variant 序的计划目标。"""
    return tuple(
        (slot, variant, _branch_events(plan, slot.slot_key, variant))
        for variant in slot.variant_names or (None,)
    )


def _validate_noise_slot(program: GenerationProgram, slot) -> None:
    """把 noise slot 的 topic、ordinal 与 frame 绑定回程序声明。"""
    noise = program.noise
    valid_ordinal = 0 <= slot.ordinal < len(noise.topics) if noise is not None else False
    if not valid_ordinal:
        _projection_mismatch("noise slot ordinal differs from the program")
    if slot.topic != noise.topics[slot.ordinal] or slot.frame_class != noise.frame_class:
        _projection_mismatch("noise slot topic or frame differs from the program")


def _reconcile_candidate_bytes(request, replay_rows) -> None:
    """从当前 primary 候选全部实际行独立复算 canonical bytes。"""
    rows = []
    for sequence in request.sequences:
        rows.append(sequence.main_row)
        rows.extend(sequence.primary_stream_rows)
    rows.extend(replay_rows)
    if request.retained_content_bytes != _canonical_rows_bytes(rows):
        _projection_mismatch("primary candidate retained-content bytes are invalid")


class CrossViewFrontier:
    """按声明序维护已提交身份与 Planner 权威资源区间。"""

    def __init__(self, program: GenerationProgram, plan: ScenarioPlan):
        """构造空 frontier，并验证当前运行的 canonical plan。

        @param program 当前冻结生成程序。
        @param plan 当前运行唯一冻结计划。
        @return None。
        """
        validate_plan_identity(program, plan)
        self._program = program
        self._plan = plan
        self._phase = "primary" if plan.delivery_slots else "noise"
        self._next_ordinal = 0
        self._event_ids: set[str] = set()
        self._timestamps_us: set[int] = set()
        self._source_keys: set[str] = set()
        self._resource_intervals: list[ResourceInterval] = []
        self._checked_delta: CrossViewDelta | None = None

    def check_primary(self, candidate: PreparedCandidate) -> CrossViewDelta:
        """对当前 primary head 生成零正式突变的冻结 delta。

        @param candidate 已通过 candidate-local 校验并深度冻结的当前候选。
        @return 只含当前候选新增事实的 delta。
        """
        if self._phase != "primary" or self._next_ordinal >= len(self._plan.delivery_slots):
            _frontier_contract_error("crossview_frontier: primary phase is closed")
        expected = self._plan.delivery_slots[self._next_ordinal]
        if candidate.slot != expected:
            _frontier_contract_error("crossview_frontier: primary ordinal is out of order")
        try:
            facts = _planned_primary_facts(self._plan, expected)
            rows = [
                row for sequence in candidate.sequences
                for row in sequence.primary_stream_rows
            ]
            rows.extend(row for replay in candidate.replays for row in replay.rows)
            delta = _frontier_delta("primary", self._next_ordinal, rows, facts)
            self._check_delta_conflicts(delta)
            self._checked_delta = delta
            return delta
        except GenerationProjectionMismatch:
            raise
        except (KeyError, TypeError, ValueError, IndexError):
            _projection_mismatch("primary frontier facts are malformed")

    def check_noise(self, candidate: PreparedNoiseCandidate) -> CrossViewDelta:
        """对当前 noise head 生成零正式突变的冻结 delta。

        @param candidate 已通过 noise-local 校验并深度冻结的当前候选。
        @return 只含当前候选新增事实的 delta。
        """
        if self._phase != "noise" or self._next_ordinal >= len(self._plan.noise_slots):
            _frontier_contract_error("crossview_frontier: noise phase is closed")
        expected = self._plan.noise_slots[self._next_ordinal]
        if candidate.noise_slot != expected:
            _frontier_contract_error("crossview_frontier: noise ordinal is out of order")
        try:
            fact = (
                f"noise:{expected.event_key}", expected.timestamp_us,
                expected.duration_us, expected.resources,
            )
            delta = _frontier_delta("noise", self._next_ordinal, [candidate.row], [fact])
            self._check_delta_conflicts(delta)
            self._checked_delta = delta
            return delta
        except GenerationProjectionMismatch:
            raise
        except (KeyError, TypeError, ValueError, IndexError):
            _projection_mismatch("noise frontier facts are malformed")

    def commit(self, delta: CrossViewDelta) -> None:
        """无 await 地消费已检查的当前 phase/ordinal delta。

        @param delta 当前 check_primary 或 check_noise 返回的冻结增量。
        @return None。
        """
        if delta is not self._checked_delta:
            _frontier_contract_error("crossview_frontier: delta was not checked by this frontier")
        if delta.phase != self._phase or delta.ordinal != self._next_ordinal:
            _frontier_contract_error("crossview_frontier: delta is out of order")
        if not _delta_is_well_formed(delta) or self._delta_conflicts(delta):
            _frontier_contract_error("crossview_frontier: unchecked conflicting delta")
        if not all(_is_id(value) for value in delta.event_ids):
            _frontier_contract_error("crossview_frontier: unchecked invalid event ID")
        self._event_ids.update(delta.event_ids)
        self._timestamps_us.update(delta.timestamps_us)
        self._source_keys.update(delta.source_keys)
        self._resource_intervals.extend(delta.resource_intervals)
        self._checked_delta = None
        self._next_ordinal += 1
        if self._phase == "primary" and self._next_ordinal == len(self._plan.delivery_slots):
            self._phase = "noise"
            self._next_ordinal = 0

    def _check_delta_conflicts(self, delta: CrossViewDelta) -> None:
        """把候选内或相对已提交集合的冲突转为当前 attempt 拒绝。"""
        if not _delta_is_well_formed(delta):
            _projection_mismatch("candidate CrossView delta is malformed")
        if self._delta_conflicts(delta):
            _temporal_contract_error("candidate conflicts with the committed temporal frontier")
        if not all(_is_id(value) for value in delta.event_ids):
            _projection_mismatch("candidate frontier event ID is invalid")

    def _delta_conflicts(self, delta: CrossViewDelta) -> bool:
        """判断增量是否与三个正式身份集合相交。"""
        identity = bool(
            self._event_ids.intersection(delta.event_ids)
            or self._timestamps_us.intersection(delta.timestamps_us)
            or self._source_keys.intersection(delta.source_keys)
        )
        return identity or _interval_sets_overlap(
            tuple(self._resource_intervals), delta.resource_intervals
        )


def _planned_primary_facts(plan, slot) -> tuple[tuple, ...]:
    """按 candidate 行顺序展开 primary 与同源 replay 计划事实。"""
    facts: list[tuple] = []
    for variant in slot.variant_names or (None,):
        for event in _branch_events(plan, slot.slot_key, variant):
            key = f"primary:{slot.slot_key}:{variant}:{event.event_key}"
            facts.append((key, event.timestamp_us, event.duration_us, event.resources))
    layouts = [item for item in plan.replay_layouts if item.source_slot_key == slot.slot_key]
    for layout in layouts:
        events = _branch_events(plan, slot.slot_key, layout.source_variant_name)
        for event in events:
            key = f"replay:{layout.replay_ordinal}:{event.event_key}"
            facts.append((
                key, event.timestamp_us + layout.shift_us, event.duration_us, event.resources,
            ))
    return tuple(facts)


def _frontier_delta(phase: str, ordinal: int, rows, facts) -> CrossViewDelta:
    """用计划事实绑定实际 event ID 并构造排序资源区间。"""
    if len(rows) != len(facts) or not rows:
        _temporal_contract_error("candidate row count differs from planned temporal facts")
    event_ids, timestamps, source_keys, intervals = [], [], [], []
    for row, fact in zip(rows, facts, strict=True):
        source_key, start_us, duration_us, resources = fact
        event = row["_meta"]["event"]
        actual_start = _timestamp_us(event["timestamp"])
        actual_resources = tuple(event.get("resources", ()))
        if (
            actual_start != start_us
            or event.get("duration_us") != duration_us
            or actual_resources != tuple(resources)
        ):
            _temporal_contract_error("candidate interval differs from the planned interval")
        event_id = event["event_id"]
        event_ids.append(event_id)
        timestamps.append(start_us)
        source_keys.append(source_key)
        if duration_us > 0:
            intervals.extend(
                ResourceInterval(resource, start_us, start_us + duration_us, event_id, source_key)
                for resource in resources
            )
    ordered = tuple(sorted(intervals, key=_interval_sort_key))
    if _resource_intervals_overlap(ordered):
        _temporal_contract_error("candidate resource intervals overlap")
    return CrossViewDelta(
        phase, ordinal, tuple(event_ids), tuple(timestamps), tuple(source_keys), ordered
    )


def _delta_is_well_formed(delta: CrossViewDelta) -> bool:
    """判断 frontier delta 非空、逐行对齐且三族身份各自唯一。"""
    return (
        bool(delta.event_ids and delta.timestamps_us and delta.source_keys)
        and len(delta.event_ids) == len(delta.timestamps_us)
        and len(delta.event_ids) == len(delta.source_keys)
        and len(delta.event_ids) == len(set(delta.event_ids))
        and len(delta.timestamps_us) == len(set(delta.timestamps_us))
        and len(delta.source_keys) == len(set(delta.source_keys))
        and delta.resource_intervals == tuple(sorted(
            delta.resource_intervals, key=_interval_sort_key
        ))
        and not _resource_intervals_overlap(delta.resource_intervals)
    )


def _interval_sort_key(interval: ResourceInterval) -> tuple:
    """返回 resource interval 的规范排序键。"""
    return (
        interval.resource, interval.start_us, interval.end_us,
        interval.event_id, interval.source_key,
    )


def _resource_intervals_overlap(intervals: Sequence[ResourceInterval]) -> bool:
    """以每资源 sort/sweep 判断候选内部半开区间冲突。"""
    previous: ResourceInterval | None = None
    for current in sorted(intervals, key=_interval_sort_key):
        if previous is not None and current.resource == previous.resource:
            if current.start_us < previous.end_us:
                return True
            if current.end_us > previous.end_us:
                previous = current
        else:
            previous = current
    return False


def _interval_sets_overlap(left, right) -> bool:
    """判断 candidate 资源区间是否与已提交前缀相交。"""
    return _resource_intervals_overlap((*left, *right))


def _frontier_contract_error(message: str) -> None:
    """记录并抛出 frontier 调用顺序或消费不变式错误。"""
    _log.error(message)
    raise InternalError(message)


def _primary_targets(plan) -> tuple[tuple[object, str | None, tuple], ...]:
    """按交付声明序解析每个可见 primary branch 的唯一计划。"""
    targets = []
    for slot in plan.delivery_slots:
        for variant in slot.variant_names or (None,):
            targets.append((slot, variant, _branch_events(plan, slot.slot_key, variant)))
    return tuple(targets)


def _branch_events(plan, slot_key: str, variant: str | None) -> tuple:
    """从 plan blocks 唯一解析一个可见 branch。"""
    matches = [block[(slot_key, variant)] for block in plan.blocks
               if (slot_key, variant) in block]
    if len(matches) != 1:
        _contract_error("ScenarioPlan branch is missing or duplicated")
    return tuple(matches[0])


def _reconcile_primary(program, witnesses, sequences, targets):
    """校验当前闭包的 main、primary、计划与 ID 双向关系。"""
    if len(sequences) != len(witnesses) or len(sequences) != len(targets):
        _projection_mismatch("primary sequence sources differ from final rows")
    sources = {}
    for witness, sequence, target in zip(witnesses, sequences, targets, strict=True):
        _reconcile_sequence(program, witness, sequence, target)
        slot, variant, _ = target
        sources[(slot.slot_key, variant)] = sequence
    return sources


@dataclasses.dataclass(frozen=True)
class _PrimaryIdentity:
    """一条 primary 分支对账使用的冻结身份根。"""

    program: object                               # 冻结 GenerationProgram
    slot: object                                  # 当前 DeliverySlot
    variant: str | None                           # 当前可见分支名
    owner: str                                    # 最终 sequence ID
    world_id: str                                 # 独立派生 world branch ID
    generation: Mapping[str, object]              # projector 生成真值


def _reconcile_sequence(program, witness, sequence, target) -> None:
    """校验一条 main 与其 primary rows 的全部机械身份。"""
    slot, variant, planned = target
    main = sequence.main_row
    meta = main.get("_meta")
    generation = meta.get("generation") if isinstance(meta, MappingABC) else None
    if not isinstance(meta, MappingABC) or not isinstance(generation, MappingABC):
        _projection_mismatch("primary main metadata is missing")
    _require_keys(meta, {
        "id", "source", "stream", "scores", "dedup", "classification",
        "annotation", "verification", "generation",
    }, "primary main metadata fields differ from contract")
    classification = {
        "label": slot.sequence_class,
        "labels": [slot.sequence_class],
        "source": "inherited",
    }
    if canonical_json(meta.get("classification")) != canonical_json(classification):
        _projection_mismatch("primary main classification is invalid")
    _reconcile_generation(generation, program, slot, variant)
    if _witness_digest("projection_main_generation", generation) != witness.generation_digest:
        _projection_mismatch("primary generation truth differs from projection")
    main_id, world_id = meta.get("id"), generation.get("world_branch_id")
    expected_scenario, expected_world = _branch_ids(program, slot, variant)
    if generation.get("scenario_id") != expected_scenario or world_id != expected_world:
        _projection_mismatch("primary scenario or world identity is invalid")
    if main_id != witness.main_record_id or not _is_id(main_id):
        _projection_mismatch("primary main identity differs from projection")
    rows = tuple(sequence.primary_stream_rows)
    if len(rows) != len(planned) or len(rows) != len(witness.primary_base_digests) or not rows:
        _projection_mismatch("primary event count differs from plan")
    identity = _PrimaryIdentity(program, slot, variant, main_id, world_id, generation)
    event_ids = _reconcile_primary_rows(
        rows, witness.primary_base_digests, planned, identity
    )
    if derive_generation_id("sequence_id", [world_id, event_ids]) != main_id:
        _projection_mismatch("sequence ID does not match ordered event IDs")
    _reconcile_main_stream(
        meta.get("stream"), rows, event_ids, planned, witness
    )
    _reconcile_sequence_temporal(program, slot, planned, main)
    retained = _canonical_rows_bytes((main, *rows))
    if sequence.retained_content_bytes != retained:
        _projection_mismatch("primary retained-content bytes are invalid")


def _projection_generation(projection) -> Mapping[str, object]:
    """读取不可变 projector main generation truth。"""
    raw = projection.main_record.raw
    meta = raw.get("_meta") if isinstance(raw, MappingABC) else None
    generation = meta.get("generation") if isinstance(meta, MappingABC) else None
    if not isinstance(generation, MappingABC):
        _projection_mismatch("projection generation truth is missing")
    return generation


def _branch_ids(program, slot, variant: str | None) -> tuple[str, str]:
    """从程序与交付槽独立派生可见分支身份。"""
    scenario_domain = (
        "declared_scenario_id" if program.mode == "declared" else "instruction_scenario_id"
    )
    scenario = derive_generation_id(
        scenario_domain, [program.digest, slot.source_name, slot.scenario_index]
    )
    if program.mode == "instruction_only":
        world = derive_generation_id("instruction_world_branch_id", [scenario, "instruction_only"])
    else:
        world = derive_generation_id("declared_world_branch_id", [scenario, variant])
    return scenario, world


def _reconcile_generation(generation, program, slot, variant) -> None:
    """校验 main generation truth 与声明序目标一致。"""
    common = {
        "scenario_index": slot.scenario_index,
        "sequence_class": slot.sequence_class,
    }
    if slot.variant_names:
        expected = {
            "validation_mode": "declared",
            "actor_knowledge_validation": "mechanical_and_semantic",
            "scenario_set": slot.source_name,
            "pattern": slot.pattern_name,
            "variant": variant,
            **common,
        }
        required = {"scenario_id", "world_branch_id", "expected_violation", "actual_violations"}
    else:
        expected = {
            "validation_mode": "instruction_only",
            "actor_knowledge_validation": "semantic",
            "instruction_slot": slot.source_name,
            **common,
        }
        required = {"scenario_id", "world_branch_id"}
    if any(generation.get(key) != value for key, value in expected.items()):
        _projection_mismatch("main generation truth differs from delivery slot")
    if set(generation) != set(expected) | required:
        _projection_mismatch("main generation truth has missing or extra fields")
    if not _is_id(generation.get("scenario_id")):
        _projection_mismatch("scenario ID is invalid")


def _reconcile_primary_rows(rows, source_digests, planned, identity) -> tuple[str, ...]:
    """逐位校验 primary row 并返回有序 event IDs。"""
    event_ids = []
    stream_generation = dict(identity.generation)
    stream_generation.pop("expected_violation", None)
    stream_generation.pop("actual_violations", None)
    entries = zip(rows, source_digests, planned, strict=True)
    for row, source_digest, event_plan in entries:
        event_ids.append(
            _reconcile_primary_row(
                row, source_digest, event_plan, identity, stream_generation
            )
        )
    if len(event_ids) != len(set(event_ids)):
        _projection_mismatch("primary event IDs are duplicated")
    return tuple(event_ids)


def _reconcile_primary_row(row, source_digest, planned, identity, generation) -> str:
    """校验一行 primary 的计划 witness、owner 与 canonical ID。"""
    _require_row_fields(row, "primary row fields differ from contract")
    meta, payload = row.get("_meta"), row.get("payload")
    event = meta.get("event") if isinstance(meta, MappingABC) else None
    if not isinstance(event, MappingABC) or not isinstance(payload, MappingABC):
        _projection_mismatch("primary row shape is invalid")
    _require_keys(event, {
        "event_id", "event_key", "owner_sequence_id", "role", "frame_class",
        "actor", "logical_time_us", "timestamp", "duration_us", "resources",
        "time_bindings",
    }, "primary event fields differ from contract")
    allowed = (
        {"event", "generation", "classification"},
        {"event", "generation", "classification", "annotation"},
    )
    if set(meta) not in allowed:
        _projection_mismatch("primary row metadata has extra fields")
    expected = {
        "event_key": planned.event_key,
        "owner_sequence_id": identity.owner,
        "role": planned.role,
        "logical_time_us": planned.logical_time_us,
    }
    if any(event.get(key) != value for key, value in expected.items()):
        _projection_mismatch("primary row differs from planned event")
    frame = _reconcile_primary_temporal(identity.program, event, payload, planned)
    event_id = derive_generation_id(
        "primary_event_id",
        [
            identity.world_id,
            planned.event_key,
            planned.timestamp_us,
            planned.duration_us,
            list(planned.resources),
            _time_descriptor(frame),
            payload,
        ],
    )
    if event.get("event_id") != event_id:
        _projection_mismatch("primary event ID is invalid")
    _reconcile_source_row(row, source_digest)
    _reconcile_declared_role(identity, planned, event)
    frame = event.get("frame_class")
    classification = {"label": frame, "labels": [frame], "source": "inherited"}
    if canonical_json(meta.get("classification")) != canonical_json(classification):
        _projection_mismatch("primary frame classification is invalid")
    if canonical_json(meta.get("generation")) != canonical_json(generation):
        _projection_mismatch("primary generation truth differs from main")
    return event_id


def _reconcile_primary_temporal(program, event, payload, planned):
    """复验 primary 外层区间、descriptor 与 payload 机械时间。

    @param program 当前冻结生成程序。
    @param event 当前 primary event metadata。
    @param payload 当前最终 payload。
    @param planned Planner 权威事件。
    @return 当前事件的冻结 frame class view。
    """
    if _timestamp_us(event.get("timestamp")) != planned.timestamp_us:
        _temporal_contract_error("primary timestamp differs from plan")
    frame = _frame_for_event(program, event.get("frame_class"))
    valid = (
        event.get("duration_us") == planned.duration_us,
        tuple(event.get("resources", ())) == tuple(planned.resources),
        canonical_json(event.get("time_bindings")) == canonical_json(_time_descriptor(frame)),
        tuple(frame.resources) == tuple(planned.resources),
        frame.duration_us == planned.duration_us,
    )
    if not all(valid):
        _temporal_contract_error("primary interval descriptor differs from plan")
    _validate_payload_time(program, frame, payload, planned.timestamp_us, planned.duration_us)
    return frame


def _reconcile_source_row(row, source_digest: str) -> None:
    """要求最终 primary 基础内容与不可变投影摘要相等。"""
    actual = _witness_digest("projection_primary_base", _primary_base(row))
    if actual != source_digest:
        _projection_mismatch("primary base content differs from projection witness")


def _reconcile_declared_role(identity, planned, event) -> None:
    """独立证明 declared role 的 frame class 与 actor 契约。"""
    if identity.program.mode == "instruction_only":
        if not _nonempty(event.get("frame_class")) or not _nonempty(event.get("actor")):
            _projection_mismatch("instruction primary frame or actor is invalid")
        return
    pattern = identity.program.patterns.get(identity.slot.pattern_name)
    role = None if pattern is None else next(
        (item for item in pattern.roles if item.name == planned.role), None
    )
    if role is None or (event.get("frame_class"), event.get("actor")) != (
        role.frame_class, role.actor
    ):
        _projection_mismatch("primary frame or actor differs from declared role")


def _reconcile_main_stream(stream, rows, event_ids, planned, witness) -> None:
    """校验 main stream 索引与 primary row 顺序、类别和 session。"""
    if not isinstance(stream, MappingABC):
        _projection_mismatch("main stream index is missing")
    _require_keys(stream, {
        "episode_id", "session_id", "member_count", "member_ids",
        "member_sources", "members",
    }, "main stream index fields differ from contract")
    expected = {
        "episode_id": rows[0]["_meta"]["event"]["owner_sequence_id"],
        "member_count": len(rows),
        "member_ids": list(event_ids),
    }
    if any(canonical_json(stream.get(key)) != canonical_json(value)
           for key, value in expected.items()):
        _projection_mismatch("main member index differs from primary rows")
    source_digest = _witness_digest(
        "projection_member_sources", stream.get("member_sources")
    )
    if source_digest != witness.member_sources_digest:
        _projection_mismatch("main member sources differ from projection witness")
    members = stream.get("members")
    if not isinstance(members, (tuple, list)) or len(members) != len(rows):
        _projection_mismatch("main member products are incomplete")
    for index, (member, row) in enumerate(zip(members, rows, strict=True)):
        _reconcile_member(member, row, event_ids[index], index)
    sessions = {item.session_id for item in planned}
    if len(sessions) != 1 or stream.get("session_id") != next(iter(sessions)):
        _projection_mismatch("main session differs from plan")


def _reconcile_sequence_temporal(program, slot, planned, main) -> None:
    """独立复验 main annotation Schema、机械时间与 containment。"""
    view = program.class_views.get(slot.sequence_class)
    if view is None:
        _contract_error("sequence class view is absent during temporal reconcile")
    user = _thaw_json({key: value for key, value in main.items() if key != "_meta"})
    if isinstance(view.schema, MappingABC):
        if any(Draft202012Validator(_thaw_json(view.schema)).iter_errors(user)):
            _temporal_contract_error("main annotation fails its complete class Schema")
    if view.time_bindings:
        _reconcile_annotation_bindings(view, planned, user)
    if program.mode == "declared":
        pattern = program.patterns.get(slot.pattern_name)
        if pattern is None:
            _contract_error("declared pattern is absent during temporal reconcile")
        _reconcile_containments(pattern, planned)


def _reconcile_annotation_bindings(view, planned, user) -> None:
    """要求 main annotation 时间等于目标资源的最早计划起点。"""
    for binding in view.time_bindings:
        starts = [
            event.timestamp_us for event in planned
            if event.duration_us > 0 and binding.resource in event.resources
        ]
        if binding.source != "first_resource_start_milliseconds" or not starts:
            _temporal_contract_error("annotation time binding has no planned resource interval")
        expected = min(starts) // 1000
        try:
            actual = JsonPointer(binding.payload_path).resolve(user)
        except (JsonPointerException, TypeError):
            _temporal_contract_error("annotation time binding path is missing")
        if actual != expected or isinstance(actual, bool):
            _temporal_contract_error("annotation time differs from planned resource start")


def _reconcile_containments(pattern, planned) -> None:
    """从计划 member 独立复验当前 branch 的严格区间包含。"""
    events = {event.role: event for event in planned}
    for relation in pattern.containments:
        container = events.get(relation.container)
        contained = events.get(relation.contained)
        if contained is None:
            continue
        if container is None:
            _temporal_contract_error("contained interval has no container")
        margin = container.timestamp_us + container.duration_us
        valid = (
            container.duration_us > 0
            and contained.duration_us > 0
            and container.timestamp_us <= contained.timestamp_us
            and contained.timestamp_us + contained.duration_us + 1000 <= margin
        )
        if not valid:
            _temporal_contract_error("planned interval containment is invalid")


def _member_source(member: Record) -> dict[str, object]:
    """从 projector member ref 重建唯一输出来源块。"""
    source: dict[str, object] = {"file": member.ref.source_file}
    if member.ref.line_no is not None:
        source["line_no"] = member.ref.line_no
    else:
        source["pair_index"] = member.ref.pair_index
    return source


def _reconcile_member(member, row, event_id: str, index: int) -> None:
    """校验 main member 与对应 primary frame product。"""
    if not isinstance(member, MappingABC):
        _projection_mismatch("main member product is malformed")
    has_annotation = "annotation" in row["_meta"]
    expected_fields = ({"index", "id", "label", "annotation", "status"}
                       if has_annotation else {"index", "id", "label"})
    if set(member) != expected_fields:
        _projection_mismatch("main member fields differ from contract")
    event = row["_meta"]["event"]
    if (member.get("index"), member.get("id"), member.get("label")) != (
        index, event_id, event.get("frame_class")
    ):
        _projection_mismatch("main member identity differs from primary row")
    if has_annotation:
        annotation = row["_meta"].get("annotation")
        if canonical_json(member.get("annotation")) != canonical_json(annotation):
            _projection_mismatch("main member annotation differs from primary row")
        valid_status = ({"annotated"} if annotation is not None else {"failed", "skipped"})
        if member.get("status") not in valid_status:
            _projection_mismatch("main member annotation status is invalid")


def _reconcile_noise(rows, payload_digests, slots, identity) -> None:
    """校验 noise rows 与 NoiseSlot 逐位闭合。"""
    if len(rows) != len(payload_digests) or len(rows) != len(slots):
        _projection_mismatch("noise sources differ from final rows")
    for row, payload_digest, slot in zip(rows, payload_digests, slots, strict=True):
        _reconcile_noise_row(row, payload_digest, slot, identity)


def _reconcile_noise_row(row, source_digest: str, slot, identity) -> None:
    """校验一行 noise 的专型身份与计划坐标。"""
    program, run_id = identity
    _require_row_fields(row, "noise row fields differ from contract")
    meta, payload = row.get("_meta"), row.get("payload")
    event = meta.get("event") if isinstance(meta, MappingABC) else None
    if not isinstance(event, MappingABC) or not isinstance(payload, MappingABC):
        _projection_mismatch("noise row shape is invalid")
    _require_keys(event, {
        "event_id", "event_key", "owner_sequence_id", "role", "frame_class",
        "actor", "logical_time_us", "timestamp", "duration_us", "resources",
        "time_bindings", "noise",
    }, "noise event fields differ from contract")
    _require_keys(meta, {"event", "generation"}, "noise metadata fields differ from contract")
    expected = {
        "event_key": slot.event_key,
        "owner_sequence_id": None,
        "role": None,
        "frame_class": slot.frame_class,
        "actor": None,
        "logical_time_us": None,
        "noise": True,
    }
    if any(event.get(key) != value for key, value in expected.items()):
        _projection_mismatch("noise row differs from planned slot")
    expected_timestamp = _timestamp_text(slot.timestamp_us, program.timeline.utc_offset_minutes)
    if event.get("timestamp") != expected_timestamp:
        _temporal_contract_error("noise timestamp differs from plan")
    frame = _frame_for_event(program, slot.frame_class)
    descriptor = _time_descriptor(frame)
    if (
        event.get("duration_us") != 0
        or tuple(event.get("resources", ())) != ()
        or canonical_json(event.get("time_bindings")) != canonical_json(descriptor)
        or frame.duration_us != 0
        or frame.resources
    ):
        _temporal_contract_error("noise interval descriptor differs from plan")
    _validate_payload_time(program, frame, payload, slot.timestamp_us, 0)
    event_key = derive_generation_id("noise_event_key", [program.digest, "noise", slot.ordinal])
    event_id = derive_generation_id(
        "noise_event_id", [run_id, event_key, slot.timestamp_us, 0, [], descriptor, payload]
    )
    if noise_payload_digest(payload) != source_digest:
        _projection_mismatch("noise payload differs from accepted source")
    if slot.event_key != event_key or event.get("event_id") != event_id:
        _projection_mismatch("noise artifact identity is invalid")
    if meta.get("generation") is not None:
        _projection_mismatch("noise row carries invalid identity truth")


def _reconcile_replay(sources, replays, layouts, program) -> None:
    """校验所有已具备 source 的 replay 分组、费用与逐位内容。"""
    expected = [layout for layout in layouts
                if (layout.source_slot_key, layout.source_variant_name) in sources]
    if len(replays) != len(expected):
        _projection_mismatch("replay group count differs from available layouts")
    for replay, layout in zip(replays, expected, strict=True):
        source = sources[(layout.source_slot_key, layout.source_variant_name)]
        rows = tuple(replay.rows)
        _reconcile_replay_group(
            rows, source, layout, program
        )
        if replay.retained_content_bytes != _canonical_rows_bytes(rows):
            _projection_mismatch("replay retained-content bytes are invalid")


def _reconcile_retained_bytes(request, replay_rows) -> None:
    """从最终 canonical rows 独立复算 retained-content 总费用。

    @param request 当前最终 CrossView 请求。
    @param replay_rows 按 ReplayRows 分组展平后的实际行。
    @return None；计费不一致时拒绝当前 attempt。
    """
    rows = []
    for sequence in request.sequences:
        rows.append(sequence.main_row)
        rows.extend(sequence.primary_stream_rows)
    rows.extend(request.noise_rows)
    rows.extend(replay_rows)
    if request.retained_content_bytes != _canonical_rows_bytes(rows):
        _projection_mismatch("retained-content total is invalid")


def _canonical_rows_bytes(rows) -> int:
    """计算一组实际交付行的 canonical JSONL byte 费用。

    @param rows 待写出的 JSON object 序列。
    @return 含每行一个换行符的 UTF-8 byte 总数。
    """
    return sum(len(canonical_delivery_row(row)) + 1 for row in rows)


def _reconcile_replay_group(rows, source, layout, program) -> None:
    """校验一个 replay group 的组身份与逐位 source 关系。"""
    source_id = source.main_row["_meta"]["id"]
    replay_id = derive_generation_id(
        "replay_sequence_id", [source_id, layout.replay_ordinal]
    )
    for index, (row, source_row) in enumerate(
        zip(rows, source.primary_stream_rows, strict=True)
    ):
        _reconcile_replay_row(
            row, source_row, layout, (index, source_id, replay_id), program
        )


def _reconcile_replay_provenance(event, source_event, layout, source_id, replay_id) -> None:
    """复验 replay provenance 与 source primary 逐字段同源。

    @param event 当前 replay event metadata。
    @param source_event 对应 source event metadata。
    @param layout Planner 权威 replay layout。
    @param source_id source sequence ID。
    @param replay_id replay sequence ID。
    @return None。
    """
    expected = {
        "event_key": source_event.get("event_key"),
        "owner_sequence_id": None,
        "role": source_event.get("role"),
        "frame_class": source_event.get("frame_class"),
        "actor": source_event.get("actor"),
        "logical_time_us": source_event.get("logical_time_us"),
        "replay_sequence_id": replay_id,
        "replay_ordinal": layout.replay_ordinal,
        "duplicate_of_sequence_id": source_id,
        "duplicate_of_event_id": source_event.get("event_id"),
    }
    if any(event.get(key) != value for key, value in expected.items()):
        _projection_mismatch("replay provenance differs from source")


def _reconcile_replay_row(row, source, layout, identity, program) -> None:
    """校验一行 replay 的新时间、新 ID 与 source 语义。"""
    index, source_id, replay_id = identity
    _require_row_fields(row, "replay row fields differ from contract")
    meta, source_meta = row.get("_meta"), source.get("_meta")
    event = meta.get("event") if isinstance(meta, MappingABC) else None
    source_event = source_meta.get("event") if isinstance(source_meta, MappingABC) else None
    if not isinstance(event, MappingABC) or not isinstance(source_event, MappingABC):
        _projection_mismatch("replay row shape is invalid")
    _require_keys(event, {
        "event_id", "event_key", "owner_sequence_id", "role", "frame_class",
        "actor", "logical_time_us", "timestamp", "duration_us", "resources",
        "time_bindings", "replay_sequence_id", "replay_ordinal",
        "duplicate_of_sequence_id", "duplicate_of_event_id",
    }, "replay event fields differ from contract")
    del index
    source_timestamp_us = _timestamp_us(source_event.get("timestamp"))
    timestamp_us = source_timestamp_us + layout.shift_us
    _reconcile_replay_provenance(event, source_event, layout, source_id, replay_id)
    if (
        layout.shift_us <= 0
        or layout.shift_us % 1000
        or event.get("duration_us") != source_event.get("duration_us")
        or tuple(event.get("resources", ())) != tuple(source_event.get("resources", ()))
        or canonical_json(event.get("time_bindings")) != canonical_json(
            source_event.get("time_bindings")
        )
    ):
        _temporal_contract_error("replay interval descriptor differs from source")
    duration_us = source_event.get("duration_us")
    if not isinstance(duration_us, int) or isinstance(duration_us, bool):
        _temporal_contract_error("replay source duration is invalid")
    frame = _frame_for_event(program, source_event.get("frame_class"))
    _reconcile_replay_content(row, source, source_id, frame)
    rebound = _rebound_payload(
        program, frame, source.get("payload"), timestamp_us, duration_us
    )
    if canonical_json(row.get("payload")) != canonical_json(rebound):
        _temporal_contract_error("replay payload time is not rebound from source")
    event_id = derive_generation_id(
        "replay_event_id",
        [replay_id, source_event.get("event_id"), timestamp_us, duration_us, rebound],
    )
    timestamp = _timestamp_text(timestamp_us, program.timeline.utc_offset_minutes)
    if event.get("event_id") != event_id:
        _projection_mismatch("replay artifact identity is invalid")
    if event.get("timestamp") != timestamp:
        _temporal_contract_error("replay timestamp text differs from the planned offset")


def _reconcile_replay_content(row, source, source_id: str, frame) -> None:
    """校验 replay payload、下游 frame 产物与 generation 同源。"""
    source_model = project_temporal_instance(source.get("payload"), frame.business_time_paths)
    replay_model = project_temporal_instance(row.get("payload"), frame.business_time_paths)
    if canonical_json(replay_model) != canonical_json(source_model):
        _projection_mismatch("replay non-time payload differs from source")
    row_extra = {key: value for key, value in row["_meta"].items()
                 if key not in {"event", "generation"}}
    source_extra = {key: value for key, value in source["_meta"].items()
                    if key not in {"event", "generation"}}
    if canonical_json(row_extra) != canonical_json(source_extra):
        _projection_mismatch("replay downstream products differ from source")
    source_generation = source["_meta"]["generation"]
    generation = {
        "validation_mode": "replay",
        "source_validation_mode": source_generation["validation_mode"],
        "sequence_class": source_generation["sequence_class"],
        "scenario_id": source_generation["scenario_id"],
        "source_pattern": source_generation.get("pattern"),
        "source_variant": source_generation.get("variant"),
        "duplicate_of_sequence_id": source_id,
    }
    if canonical_json(row["_meta"].get("generation")) != canonical_json(generation):
        _projection_mismatch("replay generation truth differs from source")


def _reconcile_timeline(rows: Sequence[Mapping[str, object]]) -> None:
    """校验所有工件时间和 event ID 全局唯一，不依赖 owner 分组顺序。"""
    timestamps = [_timestamp_us(row["_meta"]["event"]["timestamp"]) for row in rows]
    event_ids = [row["_meta"]["event"]["event_id"] for row in rows]
    if len(timestamps) != len(set(timestamps)):
        _temporal_contract_error("artifact timestamps are not globally unique")
    if len(event_ids) != len(set(event_ids)) or not all(_is_id(value) for value in event_ids):
        _projection_mismatch("event IDs are not globally unique")


def _reconcile_resource_rows(rows: Sequence[Mapping[str, object]]) -> None:
    """从最终 row 独立重建全局资源区间并执行 sort/sweep。"""
    intervals: list[ResourceInterval] = []
    for row in rows:
        event = row["_meta"]["event"]
        start_us = _timestamp_us(event.get("timestamp"))
        duration_us = event.get("duration_us")
        resources = event.get("resources")
        if (
            not isinstance(duration_us, int)
            or isinstance(duration_us, bool)
            or duration_us < 0
            or duration_us % 1000
            or not isinstance(resources, (tuple, list))
            or len(resources) != len(set(resources))
            or (duration_us == 0 and resources)
        ):
            _temporal_contract_error("final resource descriptor is invalid")
        source_key = _final_source_key(event)
        intervals.extend(
            ResourceInterval(resource, start_us, start_us + duration_us, event["event_id"], source_key)
            for resource in resources
        )
    if _resource_intervals_overlap(tuple(intervals)):
        _temporal_contract_error("final resource intervals overlap")


def _final_source_key(event) -> str:
    """从 final event metadata 构造仅供独立 sweep 的稳定 source key。"""
    if event.get("noise") is True:
        return f"noise:{event.get('event_key')}"
    replay = event.get("replay_sequence_id")
    if replay is not None:
        return f"replay:{replay}:{event.get('event_key')}"
    return f"primary:{event.get('owner_sequence_id')}:{event.get('event_key')}"


def _timestamp_us(value: object) -> int:
    """严格解析带六位微秒与数值 offset 的工件时间。"""
    pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}[+-]\d{2}:\d{2}$"
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        _projection_mismatch("artifact timestamp format is invalid")
    parsed = datetime.fromisoformat(value)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed.astimezone(timezone.utc) - epoch
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _is_id(value: object) -> bool:
    """判断值是否为规范 32 位 generation ID。"""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _nonempty(value: object) -> bool:
    """判断值是否为非空字符串。"""
    return isinstance(value, str) and bool(value)


def _require_keys(value, expected: set[str], reason: str) -> None:
    """要求机械投影对象字段集完全相等。"""
    if not isinstance(value, MappingABC) or set(value) != expected:
        _projection_mismatch(reason)


def _require_row_fields(value, reason: str) -> None:
    """要求 sequence stream 行顶层字段及声明序完全相等。"""
    if not isinstance(value, MappingABC) or tuple(value) != ("payload", "_meta"):
        _projection_mismatch(reason)


def _projection_mismatch(reason: str):
    """记录并抛出可恢复的双视图不一致。"""
    _log.warning("sequence_projection_mismatch: %s", reason)
    raise GenerationProjectionMismatch(reason)


def _timestamp_text(timestamp_us: int, offset_minutes: int) -> str:
    """把整数微秒渲染为固定 offset ISO8601。

    @param timestamp_us UTC epoch 微秒。
    @param offset_minutes 固定 offset 分钟。
    @return 带六位微秒的 ISO8601 文本。
    """
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    zone = timezone(timedelta(minutes=offset_minutes))
    value = (epoch + timedelta(microseconds=timestamp_us)).astimezone(zone)
    return value.isoformat(timespec="microseconds")


def _contract_error(message: str):
    """记录并抛出 generation downstream contract 错误。

    @param message 不含数据内容的英文原因。
    @return 不返回。
    """
    _log.error("generation_downstream_contract: %s", message)
    raise InternalError(f"generation_downstream_contract: {message}")


def _temporal_contract_error(message: str):
    """把固定计划时间不一致作为不消费 slot retry 的终态错误。

    @param message 不含 payload 与实际时间值的英文原因。
    @return 不返回。
    """
    _contract_error(message)

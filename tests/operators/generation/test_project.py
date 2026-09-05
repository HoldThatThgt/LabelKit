"""EventProjector、ReplayProjector 与 CrossViewReconciler 对抗测试。"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import importlib.util
import resource
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from labelkit.common.config._temporal import IntervalContainmentSpec
from labelkit.common.config.generation import InterleavingPatternSpec, InterleavingSpec
from labelkit.common.config.model import TimeBindingSpec
from labelkit.common.contracts.generation import (
    ActorView,
    DedupReservation,
    EventTrace,
    EventTruth,
    InterleavingLayout,
    NoiseCandidateReconcileRequest,
    NoiseProjectionRequest,
    PatternEvaluation,
    PreparedCandidate,
    PreparedNoiseCandidate,
    PrimaryCandidateReconcileRequest,
    ProjectedSequence,
    ProjectionRequest,
    ReconcileRequest,
    ResourceInterval,
    ReplayRows,
    ReplayProjectionRequest,
    ScenarioSeed,
    SemanticEvaluation,
    SequenceRows,
    StateEvaluation,
)
from labelkit.common.contracts.types import Record, RecordRef
from labelkit.common.errors import GenerationProjectionMismatch, InternalError
from labelkit.operators.generation.planner import compile_scenario_plan
from labelkit.operators.generation.program import generation_program_digest
from labelkit.operators.generation.project import (
    _timestamp_text,
    _timestamp_us,
    CrossViewFrontier,
    canonical_json,
    canonical_delivery_row,
    derive_generation_id,
    generation_digest,
    generation_random,
    noise_payload_digest,
    project_noise,
    project_replay,
    project_trace,
    projection_witness,
    _reconcile_declared_role,
    _reconcile_sequence_temporal,
    _resource_intervals_overlap,
    reconcile_noise_candidate,
    reconcile_primary_candidate,
    reconcile_views,
    scenario_plan_digest,
    validate_plan_identity,
)
_RETAINED_CAP = 536_870_912
_RSS_LIMIT_BYTES = 4 * 1024**3


def test_v120_generation_domain_fixed_vectors():
    assert generation_digest("fixed_vector", {"b": 2, "a": 1}) == (
        "22311a5d679e7837e3ae9b12f8072cc21a34f58e77d27ba76c67a83142479b2d"
    )
    assert generation_random("attempt_random", [7, "slot", 2, "scenario"]) == (
        64040523016585868640274058000328024092017509124317729899591492220036754143598
    )
    assert derive_generation_id(
        "primary_event_id", ["world", "event", 123, {"x": 1}]
    ) == "e08c2d306b8bdb97282d721f91d91116"


def _independent_semantic_value(value):
    """独立把 program carrier 转成不含 digest/callable 的语义树。"""
    if dataclasses.is_dataclass(value):
        if hasattr(value, "reference") and hasattr(value, "target"):
            return {"reference": value.reference}
        return {
            field.name: _independent_semantic_value(getattr(value, field.name))
            for field in dataclasses.fields(value)
            if field.name != "digest"
        }
    if isinstance(value, Mapping):
        return {str(key): _independent_semantic_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_independent_semantic_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return None if callable(value) else value


def _independent_domain_digest(domain: str, value) -> str:
    """不用 production digest helper 计算 v1.20 canonical SHA-256。"""
    encoded = json.dumps(
        ["labelkit:v1.20", domain, value],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_actual_program_and_plan_digest_fixed_vectors(declared_program):
    """教学工程实际 program/plan 必须命中独立重建的 v1.20 fixed vectors。"""
    declared_program = _stable_digest_program(declared_program)
    plan = compile_scenario_plan(declared_program)
    program_value = _independent_semantic_value(declared_program)
    plan_value = {
        "blocks": [[
            {
                "slot_key": key[0],
                "variant_name": key[1],
                "events": [dataclasses.asdict(event) for event in events],
            }
            for key, events in block.items()
        ] for block in plan.blocks],
        "delivery_slots": [dataclasses.asdict(item) for item in plan.delivery_slots],
        "noise_slots": [dataclasses.asdict(item) for item in plan.noise_slots],
        "replay_layouts": [dataclasses.asdict(item) for item in plan.replay_layouts],
        "interleaving_layouts": [
            dataclasses.asdict(item) for item in plan.interleaving_layouts
        ],
        "interleaving_opportunities": plan.interleaving_opportunities,
        "interleaving_pattern_opportunities": dict(
            plan.interleaving_pattern_opportunities
        ),
        "primary_sessions": plan.primary_sessions,
    }
    assert declared_program.digest == (
        "61c40c866a8d2b70625d0c0dba52a00f428711c3b13b97aea01e9cc78686baa4"
    )
    assert declared_program.digest == _independent_domain_digest(
        "generation_program", program_value,
    )
    assert plan.digest == "1c20498e62fe05e2ed0130457e43aec900d940002e0e6a1b95591f35abad80cf"
    assert plan.digest == _independent_domain_digest("scenario_plan", plan_value)


def _interleaving_digest_plan(plan):
    """为摘要篡改测试构造一个完整交织语义材料。"""
    trigger, partner = plan.delivery_slots[:2]
    layout = InterleavingLayout(
        "trigger_with_partner",
        trigger.slot_key,
        "positive",
        partner.slot_key,
        "positive",
    )
    return replace(
        plan,
        interleaving_layouts=(layout,),
        interleaving_opportunities=1,
        interleaving_pattern_opportunities={"trigger_with_partner": 1},
        primary_sessions=plan.primary_sessions - 1,
        digest="",
    )


def _stable_digest_program(program):
    """把 fixed-vector program 中的 checkout 路径替换为稳定语义值。"""
    class_views = {}
    for name, view in program.class_views.items():
        generation = view.sequence_generation
        if generation is not None:
            generation = replace(
                generation,
                initial_state_catalog_path=(
                    "/labelkit-test-fixture/catalogs/ticket-booking.jsonl"
                ),
            )
        class_views[name] = replace(view, sequence_generation=generation)
    state_validator = replace(
        program.state_validator,
        reference="/labelkit-test-fixture/hooks.py:validate_state",
    )
    stable = replace(
        program,
        class_views=MappingProxyType(class_views),
        state_validator=state_validator,
        digest="",
    )
    return replace(stable, digest=generation_program_digest(stable))


def test_stable_digest_program_is_checkout_independent(
    declared_program, instruction_program,
) -> None:
    """同一真实 program 的两个绝对根归一化为同一独立语义摘要。"""
    def reroot(program, root: str):
        views = {}
        for name, view in program.class_views.items():
            generation = view.sequence_generation
            if generation is not None:
                generation = replace(
                    generation,
                    initial_state_catalog_path=f"{root}/catalogs/ticket-booking.jsonl",
                )
            views[name] = replace(view, sequence_generation=generation)
        validator = replace(
            program.state_validator, reference=f"{root}/hooks.py:validate_state",
        )
        changed = replace(
            program, class_views=MappingProxyType(views),
            state_validator=validator, digest="",
        )
        return replace(changed, digest=generation_program_digest(changed))

    for program in (declared_program, instruction_program):
        first = reroot(program, "/checkout/a")
        second = reroot(program, "/different/checkout/b")
        assert first.digest != second.digest
        stable_first = _stable_digest_program(first)
        stable_second = _stable_digest_program(second)
        assert stable_first.digest == stable_second.digest
        assert stable_first.digest == _independent_domain_digest(
            "generation_program", _independent_semantic_value(stable_first),
        )


def _tampered_block_plans(plan):
    """返回 exact timestamp 与 session 的独立 block 变更。"""
    blocks = [dict(block) for block in plan.blocks]
    block = next(item for item in blocks if item)
    key, events = next(iter(block.items()))
    changed_time = replace(events[0], timestamp_us=events[0].timestamp_us + 1_000)
    block[key] = (changed_time, *events[1:])
    session_blocks = [dict(item) for item in plan.blocks]
    session_block = next(item for item in session_blocks if item)
    session_key, session_events = next(iter(session_block.items()))
    changed_session = replace(session_events[0], session_id="forged-session")
    session_block[session_key] = (changed_session, *session_events[1:])
    return replace(plan, blocks=tuple(blocks)), replace(plan, blocks=tuple(session_blocks))


def _tampered_interleaving_digest_plans(plan):
    """返回覆盖全部交织摘要材料的独立变更。"""
    layout = plan.interleaving_layouts[0]
    return (
        replace(plan, interleaving_opportunities=2),
        replace(plan, interleaving_pattern_opportunities={"trigger_with_partner": 2}),
        replace(plan, interleaving_pattern_opportunities={"forged_pattern": 1}),
        replace(
            plan,
            interleaving_layouts=(replace(layout, pattern_name="forged-pattern"),),
        ),
        replace(
            plan,
            interleaving_layouts=(replace(layout, trigger_slot_key="forged-trigger"),),
        ),
        replace(
            plan,
            interleaving_layouts=(replace(layout, trigger_variant_name="forged-trigger"),),
        ),
        replace(
            plan,
            interleaving_layouts=(replace(layout, partner_slot_key="forged-partner"),),
        ),
        replace(
            plan,
            interleaving_layouts=(replace(layout, partner_variant_name="forged-partner"),),
        ),
        *_tampered_block_plans(plan),
    )


def test_plan_digest_covers_interleaving_opportunities_pair_and_blocks(declared_program):
    """plan digest 显式覆盖机会统计、配对身份及 exact block 时间身份。"""
    program = _stable_digest_program(declared_program)
    base = _interleaving_digest_plan(compile_scenario_plan(program))
    digest = scenario_plan_digest(base)
    assert digest == (
        "c130f66209c5ff2165527673385ee53931d26299ce9574b773e80e39a03a5eae"
    )
    assert all(
        scenario_plan_digest(item) != digest
        for item in _tampered_interleaving_digest_plans(base)
    )


def test_plan_identity_rejects_coordinated_interleaving_tamper(declared_program):
    """协调重算摘要仍不能把交织载体伪装成 canonical plan。"""
    plan = compile_scenario_plan(declared_program)
    synthetic = _interleaving_digest_plan(plan)
    tampers = (
        replace(plan, interleaving_opportunities=1, digest=""),
        replace(
            plan,
            interleaving_pattern_opportunities={"trigger_with_partner": 1},
            digest="",
        ),
        replace(plan, interleaving_layouts=synthetic.interleaving_layouts, digest=""),
        *_tampered_block_plans(plan),
    )
    for item in tampers:
        forged = replace(item, digest=scenario_plan_digest(item))
        with pytest.raises(InternalError, match="canonical planner"):
            validate_plan_identity(declared_program, forged)


def test_far_future_timestamp_round_trips_without_float_drift(declared_program):
    """远期微秒在 planner、projection 与 ID 载体间必须保持整数精度。"""
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    target = datetime(2300, 1, 1, 0, 0, 0, 1000, tzinfo=timezone.utc)
    delta = target - epoch
    target_us = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    assert _timestamp_us(_timestamp_text(target_us, 480)) == target_us
    timeline = replace(declared_program.timeline, timestamp_start_us=target_us)
    program = replace(declared_program, timeline=timeline, digest="")
    program = replace(program, digest=generation_program_digest(program))
    plan = compile_scenario_plan(program)
    slot, trace = _trace(program, plan)
    projection = project_trace(ProjectionRequest(
        program, plan, slot, trace
    ))
    for event, row in zip(trace.events, projection.primary_stream_rows, strict=True):
        assert _timestamp_us(row["_meta"]["event"]["timestamp"]) == event.timestamp_us
        assert row["_meta"]["event"]["event_id"] == event.event_id


def test_instruction_reconcile_requires_nonempty_frame_and_actor():
    """instruction-only 的自由 role 仍必须带非空闭集 frame 与 seed actor。"""
    identity = SimpleNamespace(program=SimpleNamespace(mode="instruction_only"))
    planned = SimpleNamespace(role="position_000")
    event = {"frame_class": "message", "actor": "requester"}
    _reconcile_declared_role(identity, planned, event)
    for field in ("frame_class", "actor"):
        changed = dict(event)
        changed[field] = ""
        with pytest.raises(GenerationProjectionMismatch):
            _reconcile_declared_role(identity, planned, changed)


def _peak_rss_bytes() -> int:
    """把当前进程 peak RSS 统一为 byte。"""
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _independent_row_bytes(row) -> int:
    """不用生产 helper 独立计算无墙钟字段行的 JSONL byte。"""
    encoded = json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(encoded) + 1


def _branch(plan, slot_key: str, variant: str):
    """读取唯一可见 branch。"""
    return next(block[(slot_key, variant)] for block in plan.blocks
                if (slot_key, variant) in block)


def _payload(role: str) -> dict:
    """返回满足教学 frame Schema 的确定性 payload。"""
    if role == "request":
        return {"utterance": "请订票", "request_id": "R-100", "status": "pending"}
    if role == "acknowledge":
        return {
            "utterance": "已受理 R-100", "request_id": "R-100",
            "status": "acknowledged",
        }
    return {
        "utterance": "已出票", "request_id": "R-100",
        "ticket_id": "T-100", "status": "ticketed",
    }


def _trace(program, plan, variant_name: str = "positive", slot_index: int = 0):
    """从计划 witness 构造一个完整 declared EventTrace。"""
    slot = plan.delivery_slots[slot_index]
    planned = _branch(plan, slot.slot_key, variant_name)
    pattern = program.patterns[slot.pattern_name]
    source = next(item for item in program.counterfactual_sets if item.name == slot.source_name)
    variant = next(item for item in source.variants if item.name == variant_name)
    roles = {role.name: role for role in pattern.roles}
    scenario_id = derive_generation_id(
        "declared_scenario_id", [program.digest, slot.source_name, slot.scenario_index]
    )
    world_id = derive_generation_id("declared_world_branch_id", [scenario_id, variant_name])
    events = []
    bindings = {}
    for item in planned:
        role, payload = roles[item.role], _payload(item.role)
        frame = program.frame_classes[role.frame_class]
        descriptor = [
            {"payload_path": binding.payload_path, "source": binding.source}
            for binding in frame.time_bindings
        ]
        event_id = derive_generation_id(
            "primary_event_id",
            [
                world_id, item.event_key, item.timestamp_us, item.duration_us,
                list(item.resources), descriptor, payload,
            ],
        )
        view = ActorView(role.actor, {}, {}, (), item.logical_time_us, 0)
        events.append(EventTruth(
            item.event_key, event_id, item.role, role.frame_class, role.actor,
            item.logical_time_us, item.timestamp_us, item.duration_us,
            view, f"intent-{item.role}",
            ({"op": "test", "path": "/request/id", "value": "R-100"},),
            "before", "after", {}, payload,
        ))
        bindings[event_id] = item.role
    config = program.class_views[slot.sequence_class].sequence_generation
    seed = ScenarioSeed(**dict(config.initial_state_catalog[slot.catalog_row_index]))
    semantic = SemanticEvaluation(True, True, True, True, True, True, ())
    state = StateEvaluation("hash", "hash", True, True, True)
    violations = () if not variant.expected_violation else (variant.expected_violation,)
    return slot, EventTrace(
        scenario_id, world_id, slot.sequence_class, slot.pattern_name, variant_name,
        seed, tuple(events), seed.initial_state, PatternEvaluation(bindings, violations),
        state, semantic,
    )


def _paired_positive_program(program):
    """构造两个 positive 候选且强制配对的冻结程序。"""
    source = program.counterfactual_sets[0]
    positive = next(item for item in source.variants if item.kind == "positive")
    trigger = replace(
        source,
        name="projection_trigger",
        count=1,
        interleaving_candidate_set="projection_trigger",
        variants=(positive,),
    )
    partner = replace(
        source,
        name="projection_partner",
        count=1,
        interleaving_candidate_set="projection_partner",
        variants=(positive,),
    )
    pattern = InterleavingPatternSpec(
        "projection_pair", "projection_trigger", "projection_partner", 1,
    )
    timeline = replace(program.timeline, noise_events=0, duplicate_sequences=0)
    base = replace(
        program,
        counterfactual_sets=(trigger, partner),
        interleaving=InterleavingSpec(0, (pattern,)),
        timeline=timeline,
        noise=None,
        digest="",
    )
    return replace(base, digest=generation_program_digest(base))


def _sequence_rows(projection, planned) -> SequenceRows:
    """模拟 M11 从 ProjectedSequence 得到的最小合法最终双视图。"""
    sequence_id = projection.main_record.id
    event_ids = [member.id for member in projection.main_record.members]
    members = [
        {"index": index, "id": member.id,
         "label": row["_meta"]["event"]["frame_class"]}
        for index, (member, row) in enumerate(zip(
            projection.main_record.members, projection.primary_stream_rows, strict=True,
        ))
    ]
    generation = projection.main_record.raw["_meta"]["generation"]
    rows = []
    for source in projection.primary_stream_rows:
        row = _thaw(source)
        frame = row["_meta"]["event"]["frame_class"]
        row["_meta"]["classification"] = {
            "label": frame, "labels": [frame], "source": "inherited",
        }
        rows.append(row)
    main = {
        "intent": "book a ticket",
        "outcome": "ticketed",
        "request_id": "R-100",
        "ticket_id": "T-100",
        "summary": "ticket booking completed",
        "_meta": {
        "id": sequence_id,
        "source": {"file": "", "line": None, "pair_index": None},
        "stream": {
            "episode_id": sequence_id,
            "session_id": planned[0].session_id,
            "member_count": len(event_ids),
            "member_ids": event_ids,
            "member_sources": [
                {"file": member.ref.source_file, "pair_index": member.ref.pair_index}
                for member in projection.main_record.members
            ],
            "members": members,
        },
        "scores": {},
        "dedup": None,
        "classification": {
            "label": generation["sequence_class"],
            "labels": [generation["sequence_class"]],
            "source": "inherited",
        },
        "annotation": None,
        "verification": None,
        "generation": generation,
        },
    }
    final_rows = tuple(rows)
    retained = sum(
        len(canonical_delivery_row(row)) + 1 for row in (main, *final_rows)
    )
    return SequenceRows(main, final_rows, retained)


@pytest.fixture
def projected_set(declared_program):
    """返回一组 primary/noise/replay 的冻结合法投影。"""
    source = declared_program.counterfactual_sets[0]
    positive = next(variant for variant in source.variants if variant.kind == "positive")
    timeline = replace(
        declared_program.timeline,
        noise_events=2,
        duplicate_sequences=1,
    )
    base = replace(
        declared_program,
        counterfactual_sets=(replace(source, count=1, variants=(positive,)),),
        timeline=timeline,
        digest="",
    )
    program = replace(base, digest=generation_program_digest(base))
    plan = compile_scenario_plan(program)
    slot, trace = _trace(program, plan)
    projection = project_trace(ProjectionRequest(
        program, plan, slot, trace
    ))
    planned = _branch(plan, slot.slot_key, "positive")
    sequence = _sequence_rows(projection, planned)
    noise = project_noise(NoiseProjectionRequest(
        program, "a" * 32, plan.noise_slots[0], {"utterance": "天气很好"},
    ))
    replay = project_replay(ReplayProjectionRequest(
        program, plan, plan.replay_layouts[0], sequence,
    ))
    return program, plan, projection, sequence, noise, replay


def test_projection_and_reconcile_accept_shared_interleaving_session(declared_program):
    """两个 owner 的 positive branch 可投影并对账为同一个 primary session。"""
    program = _paired_positive_program(declared_program)
    plan = compile_scenario_plan(program)
    assert len(plan.interleaving_layouts) == 1
    assert plan.primary_sessions == 1
    witnesses = []
    sequences = []
    session_ids = set()
    for slot_index in range(2):
        slot, trace = _trace(program, plan, slot_index=slot_index)
        projection = project_trace(ProjectionRequest(program, plan, slot, trace))
        planned = _branch(plan, slot.slot_key, "positive")
        sequence = _sequence_rows(projection, planned)
        session_ids.add(planned[0].session_id)
        assert sequence.main_row["_meta"]["stream"]["session_id"] == planned[0].session_id
        witness = projection_witness(projection)
        reconcile_primary_candidate(PrimaryCandidateReconcileRequest(
            program,
            plan,
            "a" * 32,
            slot,
            (witness,),
            (sequence,),
            (),
            (),
            sequence.retained_content_bytes,
        ))
        witnesses.append(witness)
        sequences.append(sequence)
    assert len(session_ids) == 1
    retained = sum(item.retained_content_bytes for item in sequences)
    reconcile_views(ReconcileRequest(
        program,
        plan,
        "a" * 32,
        tuple(witnesses),
        tuple(sequences),
        (),
        (),
        (),
        retained,
    ))


def _thaw(value):
    """保留对象声明序地把冻结 JSON carrier 子树转回普通容器。"""
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    return value


def _event_start_frame(frame, duration_us: int, resources: tuple[str, ...]):
    """给教学 frame 增加一个由计划起点机械注入的业务时间叶。"""
    schema = _thaw(frame.gen_schema)
    schema["properties"]["timestamp"] = {
        "type": "integer", "x-labelkit-business-time": True,
    }
    schema["required"].append("timestamp")
    return replace(
        frame,
        gen_schema=schema,
        model_gen_schema=_thaw(frame.gen_schema),
        business_time_paths=("/timestamp",),
        time_bindings=(TimeBindingSpec(
            "/timestamp", "event_start_milliseconds", None,
        ),),
        duration_us=duration_us,
        resources=resources,
    )


def _trace_with_bound_times(program, trace):
    """把测试 trace 的模型 payload 转成 projector 接收的最终机械 payload。"""
    events = []
    for event in trace.events:
        frame = program.frame_classes[event.frame_class]
        payload = _thaw(event.payload)
        if frame.time_bindings:
            payload["timestamp"] = event.timestamp_us // 1000
        descriptor = [
            {"payload_path": binding.payload_path, "source": binding.source}
            for binding in frame.time_bindings
        ]
        event_id = derive_generation_id(
            "primary_event_id",
            [
                trace.world_branch_id, event.event_key, event.timestamp_us,
                event.duration_us, list(frame.resources), descriptor, payload,
            ],
        )
        events.append(replace(event, event_id=event_id, payload=payload))
    evaluation = replace(
        trace.pattern_evaluation,
        actual_bindings={event.event_id: event.role for event in events},
    )
    return replace(trace, events=tuple(events), pattern_evaluation=evaluation)


def _build_temporal_projected_set(declared_program):
    """返回同时覆盖 interval、annotation、noise 与 replay rebinding 的闭包。"""
    frames = dict(declared_program.frame_classes)
    frames["task_request"] = _event_start_frame(
        frames["task_request"], 10_000_000, ("foreground_app",),
    )
    frames["noise"] = _event_start_frame(frames["noise"], 0, ())
    views = dict(declared_program.class_views)
    view = views["ticket_booking"]
    schema = _thaw(view.schema)
    schema["properties"]["started_at"] = {
        "type": "integer", "x-labelkit-business-time": True,
    }
    schema["required"].append("started_at")
    views["ticket_booking"] = replace(
        view,
        schema=schema,
        model_schema=_thaw(view.schema),
        business_time_paths=("/started_at",),
        time_bindings=(TimeBindingSpec(
            "/started_at", "first_resource_start_milliseconds", "foreground_app",
        ),),
    )
    source = declared_program.counterfactual_sets[0]
    positive = next(item for item in source.variants if item.kind == "positive")
    timeline = replace(
        declared_program.timeline,
        noise_events=1,
        duplicate_sequences=1,
    )
    base = replace(
        declared_program,
        frame_classes=frames,
        class_views=views,
        counterfactual_sets=(replace(source, count=1, variants=(positive,)),),
        timeline=timeline,
        digest="",
    )
    program = replace(base, digest=generation_program_digest(base))
    plan = compile_scenario_plan(program)
    slot, trace = _trace(program, plan)
    trace = _trace_with_bound_times(program, trace)
    projection = project_trace(ProjectionRequest(program, plan, slot, trace))
    planned = _branch(plan, slot.slot_key, "positive")
    sequence = _sequence_rows(projection, planned)
    main = _thaw(sequence.main_row)
    main["started_at"] = min(
        event.timestamp_us for event in planned
        if "foreground_app" in event.resources
    ) // 1000
    rows = tuple(sequence.primary_stream_rows)
    retained = sum(len(canonical_delivery_row(row)) + 1 for row in (main, *rows))
    sequence = SequenceRows(main, rows, retained)
    noise_slot = plan.noise_slots[0]
    noise = project_noise(NoiseProjectionRequest(
        program,
        "a" * 32,
        noise_slot,
        {"utterance": "天气很好", "timestamp": noise_slot.timestamp_us // 1000},
    ))
    replay = project_replay(ReplayProjectionRequest(
        program, plan, plan.replay_layouts[0], sequence,
    ))
    return program, plan, projection, sequence, noise, replay


@pytest.fixture
def temporal_projected_set(declared_program):
    """向 pytest 暴露可复用的真实 temporal 投影闭包。"""
    return _build_temporal_projected_set(declared_program)


def _final_checker_rows(sequence):
    """把最小投影 fixture 补成教学 checker 接收的最终 M11 main 形状。"""
    main = _thaw(sequence.main_row)
    rows = _thaw(sequence.primary_stream_rows)
    truth = main["_meta"]["generation"]
    main["_meta"].update({
        "source": {"file": "", "line": None, "pair_index": None},
        "scores": {},
        "dedup": None,
        "classification": {
            "label": truth["sequence_class"],
            "labels": [truth["sequence_class"]],
            "source": "inherited",
        },
        "annotation": None,
        "verification": None,
    })
    return main, rows


def _example_checker():
    """从真实教学工程装载独立工件 checker。"""
    path = Path(__file__).resolve().parents[3] / "examples/sequence-generation/check_output.py"
    spec = importlib.util.spec_from_file_location("sequence_generation_checker", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rewrite_checker_owner(main, rows) -> None:
    """同步重算教学 checker 可见的 event 与 owner 身份。"""
    truth = main["_meta"]["generation"]
    world = truth["world_branch_id"]
    event_ids = []
    for row in rows:
        event = row["_meta"]["event"]
        timestamp = _timestamp_us(event["timestamp"])
        event["event_id"] = derive_generation_id(
            "primary_event_id",
            [
                world, event["event_key"], timestamp, event["duration_us"],
                event["resources"], event["time_bindings"], row["payload"],
            ],
        )
        event_ids.append(event["event_id"])
    owner = derive_generation_id("sequence_id", [world, event_ids])
    main["_meta"]["id"] = main["_meta"]["stream"]["episode_id"] = owner
    main["_meta"]["stream"]["member_ids"] = event_ids
    for index, row in enumerate(rows):
        row["_meta"]["event"]["owner_sequence_id"] = owner
        main["_meta"]["stream"]["members"][index]["id"] = event_ids[index]


def _request(program, plan, projection, sequence, noise, replay):
    """构造 partial-admission 合法 ReconcileRequest。"""
    retained = sequence.retained_content_bytes + replay.retained_content_bytes
    retained += len(canonical_delivery_row(noise)) + 1
    return ReconcileRequest(
        program, plan, "a" * 32, (projection_witness(projection),), (sequence,),
        (noise_payload_digest(noise["payload"]),), (noise,), (replay,), retained,
    )


def _primary_only_request(program, plan, projection, sequence):
    """构造不依赖 noise/replay 的 primary 对账请求。"""
    isolated = replace(plan, noise_slots=(), replay_layouts=())
    return ReconcileRequest(
        program, isolated, "a" * 32, (projection_witness(projection),),
        (sequence,), (), (), (), sequence.retained_content_bytes
    )


def _reconcile_candidate_bundle(request: ReconcileRequest) -> None:
    """把旧聚合测试夹具拆成 canonical primary/noise candidate-local 请求。"""
    noise_bytes = sum(len(canonical_delivery_row(row)) + 1 for row in request.noise_rows)
    has_primary = bool(
        request.projection_witnesses or request.sequences or request.replays
    )
    if has_primary:
        slot = request.plan.delivery_slots[0]
        layouts = tuple(
            item for item in request.plan.replay_layouts
            if item.source_slot_key == slot.slot_key
        )
        reconcile_primary_candidate(PrimaryCandidateReconcileRequest(
            request.program,
            request.plan,
            request.run_id,
            slot,
            request.projection_witnesses,
            request.sequences,
            layouts,
            request.replays,
            request.retained_content_bytes - noise_bytes,
        ))
    if len(request.noise_rows) != len(request.noise_payload_digests):
        raise GenerationProjectionMismatch("noise candidate source count differs")
    if len(request.noise_rows) > len(request.plan.noise_slots):
        raise GenerationProjectionMismatch("noise candidate slot count differs")
    for row, payload_digest, slot in zip(
        request.noise_rows,
        request.noise_payload_digests,
        request.plan.noise_slots[:len(request.noise_rows)],
        strict=True,
    ):
        reconcile_noise_candidate(NoiseCandidateReconcileRequest(
            request.program,
            request.run_id,
            slot,
            payload_digest,
            row,
            len(canonical_delivery_row(row)) + 1,
        ))


def _single_final_request(program):
    """从完整编译语义构造一槽、一 positive、一 noise、一 replay 最终计划。"""
    source = program.counterfactual_sets[0]
    positive = next(variant for variant in source.variants if variant.kind == "positive")
    timeline = replace(
        program.timeline,
        noise_events=1,
        duplicate_sequences=1,
    )
    base = replace(
        program,
        counterfactual_sets=(replace(source, count=1, variants=(positive,)),),
        timeline=timeline,
        digest="",
    )
    from labelkit.operators.generation.program import generation_program_digest

    reduced = replace(base, digest=generation_program_digest(base))
    complete = compile_scenario_plan(reduced)
    slot, trace = _trace(reduced, complete)
    projection = project_trace(ProjectionRequest(
        reduced, complete, slot, trace
    ))
    sequence = _sequence_rows(projection, _branch(complete, slot.slot_key, "positive"))
    noise = project_noise(NoiseProjectionRequest(
        reduced, "a" * 32, complete.noise_slots[0], {"utterance": "天气很好"},
    ))
    replay = project_replay(ReplayProjectionRequest(
        reduced, complete, complete.replay_layouts[0], sequence,
    ))
    return ReconcileRequest(
        reduced, complete, "a" * 32, (projection_witness(projection),), (sequence,),
        (noise_payload_digest(noise["payload"]),), (noise,), (replay,),
        sequence.retained_content_bytes + replay.retained_content_bytes
        + len(canonical_delivery_row(noise)) + 1,
    )


def test_projectors_round_trip_frozen_mappings_and_rederive_replay(projected_set):
    program, plan, projection, sequence, noise, replay = projected_set
    assert set(json.loads(canonical_json(plan.noise_slots[0]))) == {
        "event_key", "ordinal", "frame_class", "topic", "timestamp_us",
        "duration_us", "resources", "session_id",
    }
    replay_layout = json.loads(canonical_json(plan.replay_layouts[0]))
    assert set(replay_layout) == {
        "source_slot_key", "source_variant_name", "replay_ordinal", "session_id",
        "shift_us",
    }
    assert replay_layout["shift_us"] > 0
    assert json.loads(canonical_json(projection.primary_stream_rows[0]))["payload"]
    truth = projection.main_record.raw["_meta"]["generation"]
    assert truth["expected_violation"] == {}
    assert truth["actual_violations"] == ()
    witness = projection_witness(projection)
    assert witness.main_record_id == projection.main_record.id
    assert all(len(value) == 64 for value in (
        witness.generation_digest,
        witness.member_sources_digest,
        *witness.primary_base_digests,
    ))
    assert "请订票" not in canonical_json(witness)
    source_event = sequence.primary_stream_rows[0]["_meta"]["event"]
    replay_event = replay.rows[0]["_meta"]["event"]
    timestamp_us = _timestamp_us(source_event["timestamp"]) + plan.replay_layouts[0].shift_us
    expected = derive_generation_id(
        "replay_event_id",
        [
            replay_event["replay_sequence_id"], source_event["event_id"], timestamp_us,
            source_event["duration_us"], replay.rows[0]["payload"],
        ],
    )
    assert replay_event["event_id"] == expected
    assert replay_event["timestamp"] != source_event["timestamp"]
    noise_event = noise["_meta"]["event"]
    noise_slot = plan.noise_slots[0]
    assert noise_event["event_id"] == derive_generation_id(
        "noise_event_id",
        [
            "a" * 32, noise_slot.event_key, noise_slot.timestamp_us, 0, [],
            noise_event["time_bindings"], noise["payload"],
        ],
    )
    _reconcile_candidate_bundle(_request(program, plan, projection, sequence, noise, replay))


def test_temporal_projectors_emit_self_describing_rebound_rows(temporal_projected_set):
    """primary/noise/replay 都携带计划 interval 与 descriptor，replay 只平移时间叶。"""
    program, plan, projection, sequence, noise, replay = temporal_projected_set
    source = next(
        row for row in sequence.primary_stream_rows
        if row["_meta"]["event"]["time_bindings"]
    )
    rebound = next(
        row for row in replay.rows
        if row["_meta"]["event"]["time_bindings"]
    )
    source_event = source["_meta"]["event"]
    rebound_event = rebound["_meta"]["event"]
    assert source_event["duration_us"] == 10_000_000
    assert source_event["resources"] == ("foreground_app",)
    assert source_event["time_bindings"] == ({
        "payload_path": "/timestamp", "source": "event_start_milliseconds",
    },)
    assert rebound_event["duration_us"] == source_event["duration_us"]
    assert rebound_event["resources"] == source_event["resources"]
    assert rebound_event["time_bindings"] == source_event["time_bindings"]
    shift_ms = plan.replay_layouts[0].shift_us // 1000
    assert rebound["payload"]["timestamp"] == source["payload"]["timestamp"] + shift_ms
    assert {key: value for key, value in rebound["payload"].items() if key != "timestamp"} == {
        key: value for key, value in source["payload"].items() if key != "timestamp"
    }
    noise_event = noise["_meta"]["event"]
    assert noise_event["duration_us"] == 0 and tuple(noise_event["resources"]) == ()
    assert noise["payload"]["timestamp"] == plan.noise_slots[0].timestamp_us // 1000
    reconcile_views(_request(program, plan, projection, sequence, noise, replay))


def _tamper_temporal_row(row, mutation: str):
    """篡改一行时间事实且保留其余最终内容。"""
    changed = _thaw(row)
    event = changed["_meta"]["event"]
    if mutation == "duration":
        event["duration_us"] += 1000
    elif mutation == "resources":
        event["resources"] = ["forged_resource"]
    elif mutation == "descriptor":
        event["time_bindings"].append({
            "payload_path": "/extra", "source": "event_start_milliseconds",
        })
    else:
        changed["payload"]["timestamp"] += 1
    return changed


@pytest.mark.parametrize("surface", ("primary", "replay", "noise"))
@pytest.mark.parametrize("mutation", ("duration", "resources", "descriptor", "payload_time"))
def test_candidate_temporal_tamper_is_terminal_before_commit(
    temporal_projected_set,
    surface,
    mutation,
):
    """三类候选的固定时间篡改都走 terminal downstream contract。"""
    if surface == "noise":
        request = _noise_candidate_request(temporal_projected_set)
        request = replace(request, row=_tamper_temporal_row(request.row, mutation))
        reconcile = reconcile_noise_candidate
    else:
        request = _primary_candidate_request(temporal_projected_set)
        if surface == "primary":
            sequence = request.sequences[0]
            rows = list(sequence.primary_stream_rows)
            index = next(i for i, row in enumerate(rows)
                         if row["_meta"]["event"]["time_bindings"])
            rows[index] = _tamper_temporal_row(rows[index], mutation)
            changed = replace(sequence, primary_stream_rows=tuple(rows))
            request = replace(request, sequences=(changed,))
        else:
            replay = request.replays[0]
            rows = list(replay.rows)
            index = next(i for i, row in enumerate(rows)
                         if row["_meta"]["event"]["time_bindings"])
            rows[index] = _tamper_temporal_row(rows[index], mutation)
            request = replace(request, replays=(replace(replay, rows=tuple(rows)),))
        reconcile = reconcile_primary_candidate
    with pytest.raises(InternalError, match="generation_downstream_contract"):
        reconcile(request)


def test_candidate_main_annotation_tamper_is_terminal_before_commit(
    temporal_projected_set,
):
    """main annotation 时间篡改必须在 candidate-local gate 终态失败。"""
    request = _primary_candidate_request(temporal_projected_set)
    sequence = request.sequences[0]
    main = _thaw(sequence.main_row)
    main["started_at"] += 1
    changed = replace(sequence, main_row=main)
    with pytest.raises(InternalError, match="annotation time"):
        reconcile_primary_candidate(replace(request, sequences=(changed,)))


def test_temporal_reconcile_accepts_frozen_nested_annotation() -> None:
    """最终对账先解冻 annotation，不能把只读嵌套 object 误判为 Schema 违规。"""
    schema = {
        "type": "object",
        "properties": {
            "actionInfo": {
                "type": "object",
                "properties": {"timestamp": {"type": "integer"}},
                "required": ["timestamp"],
                "additionalProperties": False,
            },
        },
        "required": ["actionInfo"],
        "additionalProperties": False,
    }
    view = SimpleNamespace(schema=schema, time_bindings=())
    program = SimpleNamespace(
        class_views={"navigation": view},
        mode="instruction_only",
    )
    slot = SimpleNamespace(sequence_class="navigation")
    main = {
        "actionInfo": MappingProxyType({"timestamp": 1}),
        "_meta": MappingProxyType({}),
    }

    _reconcile_sequence_temporal(program, slot, (), main)


def test_candidate_infeasible_containment_is_terminal_before_commit(
    temporal_projected_set,
):
    """绕过 canonical identity 后，candidate-local 仍拒绝不可行 containment carrier。"""
    request = _primary_candidate_request(temporal_projected_set)
    pattern_name = request.slot.pattern_name
    pattern = request.program.patterns[pattern_name]
    patterns = dict(request.program.patterns)
    patterns[pattern_name] = replace(
        pattern,
        containments=(IntervalContainmentSpec("request", "acknowledge"),),
    )
    tampered = replace(request.program, patterns=patterns)
    with pytest.raises(InternalError, match="containment"):
        reconcile_primary_candidate(replace(request, program=tampered))


@pytest.mark.parametrize("surface", ("primary", "replay", "noise", "annotation"))
def test_final_reconcile_independently_rejects_temporal_bypass(
    temporal_projected_set,
    surface,
):
    """绕过 candidate/frontier 的每个最终时间面仍由 full reconcile 独立拒绝。"""
    program, plan, projection, sequence, noise, replay = temporal_projected_set
    request = _request(program, plan, projection, sequence, noise, replay)
    if surface == "primary":
        rows = list(sequence.primary_stream_rows)
        rows[0] = _tamper_temporal_row(rows[0], "duration")
        request = replace(request, sequences=(replace(sequence, primary_stream_rows=tuple(rows)),))
    elif surface == "replay":
        rows = list(replay.rows)
        rows[0] = _tamper_temporal_row(rows[0], "descriptor")
        request = replace(request, replays=(replace(replay, rows=tuple(rows)),))
    elif surface == "noise":
        request = replace(request, noise_rows=(_tamper_temporal_row(noise, "payload_time"),))
    else:
        main = _thaw(sequence.main_row)
        main["started_at"] += 1
        request = replace(request, sequences=(replace(sequence, main_row=main),))
    with pytest.raises(InternalError, match="generation_downstream_contract"):
        reconcile_views(request)


def test_final_reconcile_independently_rechecks_containment(
    monkeypatch,
    temporal_projected_set,
):
    """绕过 plan identity 后，final reconcile 仍独立执行 containment oracle。"""
    program, plan, projection, sequence, noise, replay = temporal_projected_set
    pattern_name = plan.delivery_slots[0].pattern_name
    pattern = program.patterns[pattern_name]
    patterns = dict(program.patterns)
    patterns[pattern_name] = replace(
        pattern,
        containments=(IntervalContainmentSpec("request", "acknowledge"),),
    )
    tampered = replace(program, patterns=patterns)
    from labelkit.operators.generation import project

    monkeypatch.setattr(project, "validate_plan_identity", lambda *_args: None)
    request = replace(
        _request(program, plan, projection, sequence, noise, replay),
        program=tampered,
    )
    with pytest.raises(InternalError, match="containment"):
        reconcile_views(request)


def test_final_reconcile_independently_rejects_global_resource_overlap(
    monkeypatch,
    temporal_projected_set,
):
    """即使绕过 Planner/frontier，final sort/sweep 仍拒绝 source/replay 全局重叠。"""
    program, plan, projection, sequence, noise, _replay = temporal_projected_set
    layout = replace(plan.replay_layouts[0], shift_us=1000)
    changed_plan = replace(plan, replay_layouts=(layout,), digest="")
    changed_plan = replace(changed_plan, digest=scenario_plan_digest(changed_plan))
    from labelkit.operators.generation import project

    monkeypatch.setattr(project, "validate_plan_identity", lambda *_args: None)
    replay = project_replay(ReplayProjectionRequest(
        program, changed_plan, layout, sequence,
    ))
    retained = sequence.retained_content_bytes + replay.retained_content_bytes
    retained += len(canonical_delivery_row(noise)) + 1
    request = ReconcileRequest(
        program,
        changed_plan,
        "a" * 32,
        (projection_witness(projection),),
        (sequence,),
        (noise_payload_digest(noise["payload"]),),
        (noise,),
        (replay,),
        retained,
    )
    with pytest.raises(InternalError, match="resource intervals overlap"):
        reconcile_views(request)


def test_final_reconcile_rejects_synchronized_replay_rewrite(temporal_projected_set):
    """整组同步改写 outer time、payload time 与 ID 仍不能替换冻结 replay layout。"""
    program, plan, projection, sequence, noise, replay = temporal_projected_set
    rows = _thaw(replay.rows)
    extra_shift_us = 10_000_000
    for row in rows:
        event = row["_meta"]["event"]
        timestamp_us = _timestamp_us(event["timestamp"]) + extra_shift_us
        event["timestamp"] = _timestamp_text(timestamp_us, program.timeline.utc_offset_minutes)
        if event["time_bindings"]:
            row["payload"]["timestamp"] += extra_shift_us // 1000
        event["event_id"] = derive_generation_id(
            "replay_event_id",
            [
                event["replay_sequence_id"], event["duplicate_of_event_id"],
                timestamp_us, event["duration_us"], row["payload"],
            ],
        )
    changed = ReplayRows(tuple(rows), sum(_independent_row_bytes(row) for row in rows))
    retained = sequence.retained_content_bytes + changed.retained_content_bytes
    retained += len(canonical_delivery_row(noise)) + 1
    request = replace(
        _request(program, plan, projection, sequence, noise, replay),
        replays=(changed,),
        retained_content_bytes=retained,
    )
    with pytest.raises(InternalError, match="generation_downstream_contract"):
        reconcile_views(request)


def test_frontier_resource_sweep_uses_half_open_intervals():
    """同 resource 相邻区间合法，一微秒交叠失败，多 resource 各自扫描。"""
    first = ResourceInterval("app", 0, 10_000, "1" * 32, "first")
    adjacent = ResourceInterval("app", 10_000, 15_000, "2" * 32, "second")
    overlap = ResourceInterval("app", 9_999, 15_000, "3" * 32, "third")
    other = ResourceInterval("screen", 1, 20_000, "4" * 32, "fourth")
    assert not _resource_intervals_overlap((first, adjacent, other))
    assert _resource_intervals_overlap((first, overlap, other))


def _primary_candidate_request(projected_set):
    """构造当前唯一 source 的 canonical primary candidate-local 请求。"""
    program, plan, projection, sequence, _noise, replay = projected_set
    slot = plan.delivery_slots[0]
    layouts = tuple(
        item for item in plan.replay_layouts if item.source_slot_key == slot.slot_key
    )
    retained = sequence.retained_content_bytes + replay.retained_content_bytes
    return PrimaryCandidateReconcileRequest(
        program,
        plan,
        "a" * 32,
        slot,
        (projection_witness(projection),),
        (sequence,),
        layouts,
        (replay,),
        retained,
    )


def _noise_candidate_request(projected_set, ordinal: int = 0):
    """构造指定 NoiseSlot 的 canonical noise candidate-local 请求。"""
    program, plan, _projection, _sequence, noise, _replay = projected_set
    slot = plan.noise_slots[ordinal]
    row = noise if ordinal == 0 else project_noise(NoiseProjectionRequest(
        program, "a" * 32, slot, {"utterance": "请忽略"},
    ))
    return NoiseCandidateReconcileRequest(
        program,
        "a" * 32,
        slot,
        noise_payload_digest(row["payload"]),
        row,
        len(canonical_delivery_row(row)) + 1,
    )


def _prepared_primary(request) -> PreparedCandidate:
    """把已通过 local gate 的 primary 请求冻结成 frontier carrier。"""
    reservation = DedupReservation("capability", 0, ("record",), ("cluster",))
    return PreparedCandidate(
        request.slot,
        1,
        request.projection_witnesses,
        request.sequences,
        request.replays,
        reservation,
        {"generated": 1},
        request.retained_content_bytes,
        "candidate-digest",
    )


def _prepared_noise(request) -> PreparedNoiseCandidate:
    """把已通过 local gate 的 noise 请求冻结成 frontier carrier。"""
    return PreparedNoiseCandidate(
        request.noise_slot,
        1,
        request.payload_digest,
        request.row,
        (1, 2, 3),
        {"generated": 1},
        request.retained_content_bytes,
        "noise-digest",
    )


def test_candidate_local_accepts_closed_primary_and_noise_requests(projected_set):
    """两个 candidate-local 入口各自只接收当前候选且不需要已提交前缀。"""
    reconcile_primary_candidate(_primary_candidate_request(projected_set))
    reconcile_noise_candidate(_noise_candidate_request(projected_set))


@pytest.mark.parametrize(
    "mutation", ("missing", "additional", "layout", "replay", "slot")
)
def test_primary_candidate_rejects_variant_and_replay_closure_mutations(
    projected_set,
    mutation,
):
    """variant 与 replay 任一遗漏、增加或 layout 错配都在缓冲前拒绝。"""
    request = _primary_candidate_request(projected_set)
    changes = {
        "missing": {"projection_witnesses": (), "sequences": ()},
        "additional": {
            "projection_witnesses": request.projection_witnesses * 2,
            "sequences": request.sequences * 2,
        },
        "layout": {"replay_layouts": ()},
        "replay": {"replays": ()},
        "slot": {"slot": replace(request.slot, scenario_index=99)},
    }
    with pytest.raises(GenerationProjectionMismatch):
        reconcile_primary_candidate(replace(request, **changes[mutation]))


def test_primary_candidate_requires_exact_declared_variant_order(declared_program):
    """即使 witness 与 sequence 成对交换，自洽内容也不能改变声明 variant 顺序。"""
    plan = compile_scenario_plan(declared_program)
    slot = plan.delivery_slots[0]
    projections = []
    sequences = []
    for variant_name in slot.variant_names:
        _slot, trace = _trace(declared_program, plan, variant_name)
        projection = project_trace(ProjectionRequest(
            declared_program, plan, slot, trace,
        ))
        projections.append(projection)
        sequences.append(_sequence_rows(
            projection, _branch(plan, slot.slot_key, variant_name)
        ))
    layouts = tuple(
        item for item in plan.replay_layouts if item.source_slot_key == slot.slot_key
    )
    source_by_variant = dict(zip(slot.variant_names, sequences, strict=True))
    replays = tuple(project_replay(ReplayProjectionRequest(
        declared_program,
        plan,
        layout,
        source_by_variant[layout.source_variant_name],
    )) for layout in layouts)
    retained = sum(item.retained_content_bytes for item in (*sequences, *replays))
    request = PrimaryCandidateReconcileRequest(
        declared_program,
        plan,
        "a" * 32,
        slot,
        tuple(projection_witness(item) for item in projections),
        tuple(sequences),
        layouts,
        replays,
        retained,
    )
    reconcile_primary_candidate(request)
    order = (1, 0, *range(2, len(sequences)))
    changed = replace(
        request,
        projection_witnesses=tuple(request.projection_witnesses[index] for index in order),
        sequences=tuple(request.sequences[index] for index in order),
    )
    with pytest.raises(GenerationProjectionMismatch, match="delivery slot"):
        reconcile_primary_candidate(changed)


@pytest.mark.parametrize("mutation", ("topic", "ordinal", "digest", "bytes"))
def test_noise_candidate_rejects_local_identity_and_accounting_mutations(
    projected_set,
    mutation,
):
    """noise topic/ordinal、payload source 与 canonical bytes 均独立闭合。"""
    request = _noise_candidate_request(projected_set)
    changes = {
        "topic": {"noise_slot": replace(request.noise_slot, topic="forged")},
        "ordinal": {"noise_slot": replace(request.noise_slot, ordinal=99)},
        "digest": {"payload_digest": "0" * 64},
        "bytes": {"retained_content_bytes": request.retained_content_bytes + 1},
    }
    with pytest.raises(GenerationProjectionMismatch):
        reconcile_noise_candidate(replace(request, **changes[mutation]))


def test_candidate_local_converts_malformed_plan_and_noise_slot_to_rejection(projected_set):
    """内部 carrier 的结构类型错误也只能成为当前候选的 reconcile rejection。"""
    primary = _primary_candidate_request(projected_set)
    malformed_plan = replace(primary.plan, blocks=({
        (primary.slot.slot_key, primary.slot.variant_names[0]): None,
    },))
    with pytest.raises(GenerationProjectionMismatch, match="primary candidate"):
        reconcile_primary_candidate(replace(primary, plan=malformed_plan))

    noise = _noise_candidate_request(projected_set)
    malformed_slot = replace(noise.noise_slot, timestamp_us="invalid")
    with pytest.raises(GenerationProjectionMismatch, match="noise candidate"):
        reconcile_noise_candidate(replace(noise, noise_slot=malformed_slot))


def test_frontier_check_is_non_mutating_and_commit_advances_phase(projected_set):
    """check 只返回 delta；commit 后才写集合并从 primary 切换到 noise。"""
    primary_request = _primary_candidate_request(projected_set)
    frontier = CrossViewFrontier(primary_request.program, primary_request.plan)
    first_noise = _noise_candidate_request(projected_set)
    second_noise = _noise_candidate_request(projected_set, 1)
    with pytest.raises(InternalError, match="noise phase is closed"):
        frontier.check_noise(_prepared_noise(first_noise))
    delta = frontier.check_primary(_prepared_primary(primary_request))

    assert delta.phase == "primary" and delta.ordinal == 0
    main_id = primary_request.sequences[0].main_row["_meta"]["id"]
    replay_id = primary_request.replays[0].rows[0]["_meta"]["event"]["replay_sequence_id"]
    del main_id, replay_id
    assert len(delta.source_keys) == len(delta.event_ids)
    assert any(key.startswith("primary:") for key in delta.source_keys)
    assert any(key.startswith("replay:") for key in delta.source_keys)
    assert frontier._event_ids == set()
    assert frontier._timestamps_us == set()
    assert frontier._source_keys == set()
    assert frontier._next_ordinal == 0

    frontier.commit(delta)
    assert frontier._phase == "noise"
    assert frontier._next_ordinal == 0
    assert frontier._event_ids == set(delta.event_ids)
    assert frontier._timestamps_us == set(delta.timestamps_us)
    assert frontier._source_keys == set(delta.source_keys)
    with pytest.raises(InternalError, match="primary phase is closed"):
        frontier.check_primary(_prepared_primary(primary_request))
    with pytest.raises(InternalError, match="noise ordinal is out of order"):
        frontier.check_noise(_prepared_noise(second_noise))

    noise_delta = frontier.check_noise(_prepared_noise(first_noise))
    assert noise_delta.source_keys == (f"noise:{first_noise.noise_slot.event_key}",)
    frontier.commit(noise_delta)
    assert frontier._next_ordinal == 1
    second_delta = frontier.check_noise(_prepared_noise(second_noise))
    frontier.commit(second_delta)
    with pytest.raises(InternalError, match="noise phase is closed"):
        frontier.check_noise(_prepared_noise(second_noise))


def test_frontier_rejects_out_of_order_and_cross_candidate_collision_without_mutation(
    projected_set,
):
    """scheduler 顺序错误是内部失败且不推进 frontier。"""
    request = _primary_candidate_request(projected_set)
    second_slot = replace(request.slot, slot_key="second/000000")
    frontier = CrossViewFrontier(request.program, request.plan)
    duplicate = replace(_prepared_primary(request), slot=second_slot)

    with pytest.raises(InternalError, match="out of order"):
        frontier.check_primary(duplicate)

    first_delta = frontier.check_primary(_prepared_primary(request))
    frontier.commit(first_delta)
    with pytest.raises(InternalError, match="primary phase is closed"):
        frontier.check_primary(duplicate)

    assert frontier._phase == "noise"
    assert frontier._event_ids == set(first_delta.event_ids)
    assert frontier._timestamps_us == set(first_delta.timestamps_us)
    assert frontier._source_keys == set(first_delta.source_keys)


def test_frontier_rejects_malformed_or_invalid_primary_facts(projected_set):
    """frontier 对绕过 local gate 的坏 carrier 仍 fail closed，且不推进状态。"""
    request = _primary_candidate_request(projected_set)
    prepared = _prepared_primary(request)
    sequence = prepared.sequences[0]

    malformed = replace(prepared, sequences=(replace(sequence, primary_stream_rows=({},)),))
    with pytest.raises(InternalError, match="row count"):
        CrossViewFrontier(request.program, request.plan).check_primary(malformed)

    rows = _thaw(sequence.primary_stream_rows)
    rows[0]["_meta"]["event"]["event_id"] = "invalid"
    invalid_id = replace(
        prepared,
        sequences=(replace(sequence, primary_stream_rows=tuple(rows)),),
    )
    with pytest.raises(GenerationProjectionMismatch, match="event ID is invalid"):
        CrossViewFrontier(request.program, request.plan).check_primary(invalid_id)

    rows = _thaw(sequence.primary_stream_rows)
    rows[0]["_meta"]["event"]["duration_us"] = 1000
    invalid_interval = replace(
        prepared, sequences=(replace(sequence, primary_stream_rows=tuple(rows)),)
    )
    with pytest.raises(InternalError, match="planned interval"):
        CrossViewFrontier(request.program, request.plan).check_primary(invalid_interval)


def test_frontier_rejects_malformed_or_invalid_noise_facts(projected_set):
    """noise frontier 对缺失行结构和伪造 source identity 都不正式突变。"""
    primary = _primary_candidate_request(projected_set)
    frontier = CrossViewFrontier(primary.program, primary.plan)
    frontier.commit(frontier.check_primary(_prepared_primary(primary)))
    noise = _prepared_noise(_noise_candidate_request(projected_set))

    with pytest.raises(GenerationProjectionMismatch, match="frontier facts are malformed"):
        frontier.check_noise(replace(noise, row={}))

    row = _thaw(noise.row)
    row["_meta"]["event"]["duration_us"] = 1000
    with pytest.raises(InternalError, match="planned interval"):
        frontier.check_noise(replace(noise, row=row))

    assert frontier._next_ordinal == 0


@pytest.mark.parametrize("mutation", ("duplicate", "empty", "invalid_id", "phase"))
def test_frontier_commit_rejects_a_forged_unchecked_delta(
    projected_set,
    mutation,
):
    """只有 check 产生的闭合当前 delta 可进入无 await commit 路径。"""
    request = _primary_candidate_request(projected_set)
    frontier = CrossViewFrontier(request.program, request.plan)
    checked = frontier.check_primary(_prepared_primary(request))
    changes = {
        "duplicate": {"event_ids": (*checked.event_ids, checked.event_ids[0])},
        "empty": {"event_ids": (), "timestamps_us": (), "source_keys": ()},
        "invalid_id": {"event_ids": ("invalid", *checked.event_ids[1:])},
        "phase": {"phase": "noise"},
    }
    forged = replace(checked, **changes[mutation])

    with pytest.raises(InternalError, match="was not checked"):
        frontier.commit(forged)

    assert frontier._next_ordinal == 0
    assert frontier._event_ids == set()


@pytest.mark.parametrize("delta", (-1, 1))
def test_reconcile_recomputes_sequence_and_total_retained_bytes(projected_set, delta):
    """同步篡改 sequence 字段与候选总数仍由 canonical rows 杀死。"""
    program, plan, projection, sequence, noise, replay = projected_set
    request = _request(program, plan, projection, sequence, noise, replay)
    tampered = replace(
        sequence,
        retained_content_bytes=sequence.retained_content_bytes + delta,
    )
    with pytest.raises(GenerationProjectionMismatch):
        _reconcile_candidate_bundle(replace(
            request,
            sequences=(tampered,),
            retained_content_bytes=request.retained_content_bytes + delta,
        ))


@pytest.mark.parametrize("delta", (-1, 1))
def test_reconcile_recomputes_replay_and_total_retained_bytes(projected_set, delta):
    """同步篡改 replay 字段与候选总数仍由分组 canonical rows 杀死。"""
    program, plan, projection, sequence, noise, replay = projected_set
    request = _request(program, plan, projection, sequence, noise, replay)
    tampered = replace(
        replay,
        retained_content_bytes=replay.retained_content_bytes + delta,
    )
    with pytest.raises(GenerationProjectionMismatch):
        _reconcile_candidate_bundle(replace(
            request,
            replays=(tampered,),
            retained_content_bytes=request.retained_content_bytes + delta,
        ))


def test_reconcile_recomputes_noise_and_global_retained_total(projected_set):
    """noise 没有自报费用时，CrossView 仍独立复算最终全局总数。"""
    program, plan, projection, sequence, noise, replay = projected_set
    request = _request(program, plan, projection, sequence, noise, replay)
    with pytest.raises(GenerationProjectionMismatch):
        _reconcile_candidate_bundle(replace(
            request, retained_content_bytes=request.retained_content_bytes + 1
        ))


def test_final_reconcile_requires_every_planned_primary_noise_and_replay(projected_set):
    """公开 final 入口拒绝全空及任一视图少一项，完整计划才通过。"""
    program, plan, projection, sequence, noise, replay = projected_set
    request = _single_final_request(program)
    reconcile_views(request)
    empty = replace(
        request,
        projection_witnesses=(),
        sequences=(),
        noise_payload_digests=(),
        noise_rows=(),
        replays=(),
        retained_content_bytes=0,
    )
    mutations = (
        empty,
        replace(request, projection_witnesses=(), sequences=()),
        replace(request, noise_payload_digests=(), noise_rows=()),
        replace(request, replays=()),
    )
    for incomplete in mutations:
        with pytest.raises(GenerationProjectionMismatch):
            reconcile_views(incomplete)


def test_projected_sequence_deep_freezes_record_and_row_source_truth():
    """CrossView 源投影不与调用方保留的可变 JSON 容器共享。"""
    raw = {"_meta": {"generation": {"scenario_id": "a" * 32}}}
    member_raw = {"payload": {"value": 1}}
    member = Record(
        "b" * 32, "text", "frame", member_raw, None, None,
        RecordRef("", None, None, ()),
    )
    record = Record(
        "c" * 32, "text", None, raw, None, None,
        RecordRef("", None, None, ()), kind="sequence", members=(member,),
    )
    row = {"payload": {"value": 1}, "_meta": {"event": {"event_id": "b" * 32}}}
    projection = ProjectedSequence(record, (row,))
    raw["_meta"]["generation"]["scenario_id"] = "d" * 32
    member_raw["payload"]["value"] = 2
    row["payload"]["value"] = 3
    assert projection.main_record.raw["_meta"]["generation"]["scenario_id"] == "a" * 32
    assert projection.main_record.members[0].raw["payload"]["value"] == 1
    assert projection.primary_stream_rows[0]["payload"]["value"] == 1


def test_canonical_delivery_row_ignores_only_wall_clock_observations():
    base = {"payload": {"value": 1}, "_meta": {"run": {"run_id": "stable"}}}
    observed = _thaw(base)
    observed["_meta"]["run"].update({
        "started_at": "now", "finished_at": "later", "duration_ms": 7,
    })
    assert canonical_delivery_row(observed) == canonical_delivery_row(base)
    changed = _thaw(base)
    changed["payload"]["value"] = 2
    assert canonical_delivery_row(changed) != canonical_delivery_row(base)


def test_example_checker_matches_delivery_canonicalization_and_rejects_forged_ids(
        projected_set):
    """教学 checker 独立重算身份，且不误删 manifest committed_at。"""
    program, plan, projection, sequence, _noise, _replay = projected_set
    checker = _example_checker()
    main, rows = _final_checker_rows(sequence)
    checker._assert_cross_view([main], rows, program.digest)

    committed = {"_meta": {"run": {"run_id": "stable", "committed_at": "frozen"}}}
    assert checker._canonical_row(committed) != checker._canonical_row({
        "_meta": {"run": {"run_id": "stable"}},
    })

    truth = main["_meta"]["generation"]
    truth["scenario_id"] = "1" * 32
    truth["world_branch_id"] = derive_generation_id(
        "declared_world_branch_id", [truth["scenario_id"], truth["variant"]]
    )
    planned = _branch(plan, plan.delivery_slots[0].slot_key, "positive")
    event_ids = []
    for index, row in enumerate(rows):
        row_truth = row["_meta"]["generation"]
        row_truth.update({key: truth[key] for key in ("scenario_id", "world_branch_id")})
        event = row["_meta"]["event"]
        event["event_key"] = derive_generation_id(
            "declared_event_key", [truth["scenario_id"], event["role"]]
        )
        event["event_id"] = derive_generation_id(
            "primary_event_id",
            [truth["world_branch_id"], event["event_key"],
             planned[index].timestamp_us, row["payload"]],
        )
        event_ids.append(event["event_id"])
    owner = derive_generation_id("sequence_id", [truth["world_branch_id"], event_ids])
    main["_meta"]["id"] = main["_meta"]["stream"]["episode_id"] = owner
    main["_meta"]["stream"]["member_ids"] = event_ids
    for index, row in enumerate(rows):
        row["_meta"]["event"]["owner_sequence_id"] = owner
        main["_meta"]["stream"]["members"][index]["id"] = event_ids[index]
    with pytest.raises(AssertionError):
        checker._assert_cross_view([main], rows, program.digest)


def test_example_checker_rejects_extra_or_wrong_declared_violation():
    """教学 checker 要求四个变体各自恰有其唯一目标违规。"""
    checker = _example_checker()
    rows = []
    for scenario_index in range(2):
        for variant, expected in checker.EXPECTED_VARIANT_VIOLATIONS.items():
            actual = [] if not expected else [expected]
            rows.append({"_meta": {"generation": {
                "scenario_index": scenario_index,
                "scenario_set": "booking_success_training",
                "pattern": "booking_success",
                "sequence_class": "ticket_booking",
                "variant": variant,
                "validation_mode": "declared",
                "actor_knowledge_validation": "mechanical_and_semantic",
                "expected_violation": expected,
                "actual_violations": list(actual),
                "scenario_id": "a" * 32,
                "world_branch_id": "b" * 32,
            }}})
    checker._assert_declared_main(rows)
    rows[1]["_meta"]["generation"]["actual_violations"].append({
        "kind": "gap_above_max", "target": "unexpected",
    })
    with pytest.raises(AssertionError, match="Actual violation set mismatch"):
        checker._assert_declared_main(rows)


def test_example_checker_rejects_self_consistent_below_min_gap(projected_set):
    """重算全部身份也不能掩盖可见事件违反教学 gap。"""
    program, _plan, _projection, sequence, _noise, _replay = projected_set
    checker = _example_checker()
    main, rows = _final_checker_rows(sequence)
    first_us = _timestamp_us(rows[0]["_meta"]["event"]["timestamp"])
    rows[1]["_meta"]["event"]["timestamp"] = _timestamp_text(first_us + 1_000_000, 480)
    rows[1]["_meta"]["event"]["logical_time_us"] = 1_000_000
    _rewrite_checker_owner(main, rows)
    checker._assert_cross_view([main], rows, program.digest)
    assert checker._visible_pattern_violations(rows) == [{
        "kind": "gap_below_min", "target": "request_to_acknowledge",
    }]
    with pytest.raises(AssertionError, match="logical layout|Visible pattern"):
        checker._assert_declared_patterns([main], rows)


@pytest.mark.parametrize("mutation", (
    "member_label", "member_index", "member_source", "sequence_label",
    "sequence_labels", "session_id", "primary_actor", "primary_meta_extra",
    "primary_top_extra", "stream_extra",
))
def test_example_checker_rejects_each_main_primary_contract_tamper(
        projected_set, mutation):
    """教学 checker 独立封闭 main/primary 的所有用户可见关联面。"""
    program, _plan, _projection, sequence, _noise, _replay = projected_set
    checker = _example_checker()
    main, rows = _final_checker_rows(sequence)
    stream = main["_meta"]["stream"]
    if mutation == "member_label":
        stream["members"][0]["label"] = "noise"
    elif mutation == "member_index":
        stream["members"][0]["index"] = 9
    elif mutation == "member_source":
        stream["member_sources"][0]["file"] = "forged.jsonl"
    elif mutation == "sequence_label":
        main["_meta"]["classification"]["label"] = "other"
    elif mutation == "sequence_labels":
        main["_meta"]["classification"]["labels"] = ["other"]
    elif mutation == "session_id":
        stream["session_id"] = "primary_999999"
    elif mutation == "primary_actor":
        rows[0]["_meta"]["event"]["actor"] = "forged"
    elif mutation == "primary_meta_extra":
        rows[0]["_meta"]["unexpected"] = True
    elif mutation == "primary_top_extra":
        rows[0]["unexpected"] = True
    else:
        stream["unexpected"] = True
    with pytest.raises(AssertionError):
        checker._assert_cross_view([main], rows, program.digest)


@pytest.mark.parametrize("mutation", (
    "classification", "generation", "meta_extra", "top_extra", "actor", "payload",
))
def test_example_checker_rejects_each_replay_contract_tamper(projected_set, mutation):
    """教学 checker 对 replay 下游元数据、来源真值和内容逐项对账。"""
    _program, _plan, _projection, sequence, noise, replay = projected_set
    checker = _example_checker()
    primary = _thaw(sequence.primary_stream_rows)
    replay_rows = _thaw(replay.rows)
    first_replay = _timestamp_us(replay_rows[0]["_meta"]["event"]["timestamp"])
    prior = _thaw(noise)
    prior["_meta"]["event"]["timestamp"] = _timestamp_text(
        first_replay - 3_600_000_000, 480
    )
    stream = [*primary, prior, *replay_rows]
    checker._assert_replay(primary, replay_rows, stream)
    if mutation == "classification":
        replay_rows[0]["_meta"]["classification"]["label"] = "other"
    elif mutation == "generation":
        replay_rows[0]["_meta"]["generation"]["source_variant"] = "other"
    elif mutation == "meta_extra":
        replay_rows[0]["_meta"]["unexpected"] = True
    elif mutation == "top_extra":
        replay_rows[0]["unexpected"] = True
    elif mutation == "actor":
        replay_rows[0]["_meta"]["event"]["actor"] = "other"
    else:
        replay_rows[0]["payload"]["request_id"] = "other"
    with pytest.raises(AssertionError):
        checker._assert_replay(primary, replay_rows, stream)


def test_example_checker_rejects_noise_top_level_tamper(projected_set):
    """教学 checker 要求 noise stream row 顶层字段闭集。"""
    program, plan, _projection, _sequence, noise, _replay = projected_set
    checker = _example_checker()
    rows = [_thaw(noise)]
    rows.append(_thaw(project_noise(NoiseProjectionRequest(
        program, "a" * 32, plan.noise_slots[1], {"utterance": "请忽略"},
    ))))
    checker._assert_noise(rows, program.digest, "a" * 32)
    rows[0]["unexpected"] = True
    with pytest.raises(AssertionError):
        checker._assert_noise(rows, program.digest, "a" * 32)


@pytest.mark.parametrize("mutation", ("top_order", "meta_extra", "event_extra"))
def test_example_checker_rejects_each_noise_shape_tamper(projected_set, mutation):
    """教学 checker 对 noise 顶层顺序、元数据与 event 字段集逐层封闭。"""
    program, plan, _projection, _sequence, noise, _replay = projected_set
    checker = _example_checker()
    rows = [_thaw(noise), _thaw(project_noise(NoiseProjectionRequest(
        program, "a" * 32, plan.noise_slots[1], {"utterance": "请忽略"},
    )))]
    checker._assert_noise(rows, program.digest, "a" * 32)
    if mutation == "top_order":
        rows[0] = {"_meta": rows[0]["_meta"], "payload": rows[0]["payload"]}
    elif mutation == "meta_extra":
        rows[0]["_meta"]["unexpected"] = True
    else:
        rows[0]["_meta"]["event"]["unexpected"] = True
    with pytest.raises(AssertionError):
        checker._assert_noise(rows, program.digest, "a" * 32)


def _checker_report_fixture(checker):
    """构造一个完整、零拒绝的教学 sequence report。"""
    expected = {
        "mode": "declared", "planned_sets": 2, "delivered_sets": 2,
        "planned_sequences": 8, "delivered_sequences": 8, "primary_events": 22,
        "interleaving_opportunities": 0, "primary_sessions": 8,
        "interleaved_primary_sessions": 0, "by_interleaving_pattern": {},
        "noise_events": 2,
        "replay_sequences": 1, "replay_events": 3, "replay_tail_sessions": 1,
        "stream_rows": 27,
    }
    calls = {
        "scenario_seed_calls": 0,
        "baseline_event_plan_calls": 6,
        "variant_event_plan_calls": 8,
        "frame_render_calls": 14,
        "semantic_evaluation_calls": 8,
        "noise_render_calls": 2,
        "noise_evaluation_calls": 2,
    }
    by_pattern = {"booking_success": {"positive": {"planned": 2, "delivered": 2}}}
    sequence = {
        "mode": expected["mode"],
        "run_attempt_id": "a" * 32,
        "run_id": "b" * 32,
        "delivery_digest": "c" * 64,
        "artifacts_committed": True,
        "program_digest": "d" * 64,
        "plan_digest": "e" * 64,
        **{key: value for key, value in expected.items() if key != "mode"},
        "sequence_slot_attempts": 2,
        "noise_slot_attempts": 2,
        "sequence_calls": _thaw(calls),
        "by_pattern": _thaw(by_pattern),
        "rejected_attempts": {key: 0 for key in checker.REJECTION_ORDER},
    }
    return sequence, expected, calls, by_pattern


@pytest.mark.parametrize("mutation", (
    "extra", "run_id", "count", "call_missing", "pattern", "reject_missing",
    "rejected", "committed", "count_float", "attempt_bool", "pattern_float",
))
def test_example_checker_rejects_each_sequence_report_tamper(mutation):
    """教学 checker 要求报告字段闭集、算术、调用与拒绝计数全部精确。"""
    checker = _example_checker()
    sequence, expected, calls, by_pattern = _checker_report_fixture(checker)
    checker._assert_sequence_report(sequence, expected, calls, by_pattern, 8)
    if mutation == "extra":
        sequence["unexpected"] = 1
    elif mutation == "run_id":
        sequence["run_id"] = "not-an-id"
    elif mutation == "count":
        sequence["delivered_sequences"] += 1
    elif mutation == "call_missing":
        sequence["sequence_calls"].pop("frame_render_calls")
    elif mutation == "pattern":
        sequence["by_pattern"]["booking_success"]["positive"]["delivered"] = 1
    elif mutation == "reject_missing":
        sequence["rejected_attempts"].pop("reconcile")
    elif mutation == "rejected":
        sequence["rejected_attempts"]["reconcile"] = 1
    elif mutation == "count_float":
        sequence["planned_sequences"] = 8.0
    elif mutation == "attempt_bool":
        sequence["sequence_slot_attempts"] = True
    elif mutation == "pattern_float":
        sequence["by_pattern"]["booking_success"]["positive"]["planned"] = 2.0
    else:
        sequence["artifacts_committed"] = False
    with pytest.raises(AssertionError):
        checker._assert_sequence_report(sequence, expected, calls, by_pattern, 8)


def test_example_checker_accepts_reported_natural_retries():
    """成功报告允许自然拒绝，但 attempt 必须与专用桶精确守恒。"""
    checker = _example_checker()
    sequence, expected, calls, by_pattern = _checker_report_fixture(checker)
    sequence["rejected_attempts"]["semantic_evaluation"] = 3
    sequence["rejected_attempts"]["noise_similarity"] = 2
    sequence["sequence_slot_attempts"] = 5
    sequence["noise_slot_attempts"] = 4
    sequence["sequence_calls"]["noise_render_calls"] = 4
    sequence["sequence_calls"]["noise_evaluation_calls"] = 4

    checker._assert_sequence_report(sequence, expected, calls, by_pattern, 8)


@pytest.mark.parametrize("target", ("sequence_attempts", "noise_attempts", "family_calls"))
def test_example_checker_rejects_coordinated_attempt_and_call_forgery(target):
    """协调抬高守恒字段也不能越过配置预算或单 attempt 调用上界。"""
    checker = _example_checker()
    sequence, expected, calls, by_pattern = _checker_report_fixture(checker)
    if target == "sequence_attempts":
        sequence["rejected_attempts"]["semantic_evaluation"] = 15
        sequence["sequence_slot_attempts"] = 17
    elif target == "noise_attempts":
        sequence["rejected_attempts"]["noise_similarity"] = 15
        sequence["noise_slot_attempts"] = 17
        sequence["sequence_calls"]["noise_render_calls"] = 17
        sequence["sequence_calls"]["noise_evaluation_calls"] = 17
    else:
        sequence["sequence_calls"]["frame_render_calls"] = 15
    with pytest.raises(AssertionError):
        checker._assert_sequence_report(sequence, expected, calls, by_pattern, 8)


def _checker_manifest_fixture(tmp_path, checker):
    """写出一个最小但完整的 manifest checker 工件组。"""
    paths = tuple(tmp_path / name for name in (
        "main.jsonl", "stream.jsonl", "report.json", "manifest.json",
    ))
    main = [{"_meta": {"id": "a" * 32}}]
    stream = [{"payload": {}, "_meta": {"event": {}}}]
    for path, rows in zip(paths[:2], (main, stream), strict=True):
        body = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
        path.write_text(body, encoding="utf-8")
    digest = checker._delivery_digest(main, stream)
    report = {"generate": {"sequence": {
        "run_id": "b" * 32, "delivery_digest": digest, "artifacts_committed": True,
    }}}
    paths[2].write_text(json.dumps(report, separators=(",", ":")) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": 1, "run_id": "b" * 32, "delivery_digest": digest,
        "artifacts_committed": True,
        "main": {"path": str(paths[0].resolve()), "sha256": checker._artifact_sha(paths[0]),
                 "rows": 1},
        "stream": {"path": str(paths[1].resolve()), "sha256": checker._artifact_sha(paths[1]),
                   "rows": 1},
        "report": {"path": str(paths[2].resolve()), "sha256": checker._artifact_sha(paths[2])},
        "committed_at": "2026-01-05T00:00:00.000000Z",
    }
    paths[3].write_text(json.dumps(manifest, separators=(",", ":")) + "\n", encoding="utf-8")
    return paths, (main, stream), manifest


@pytest.mark.parametrize("mutation", (
    "top_order", "nested_order", "rows", "sha", "path", "committed", "extra",
    "schema_float", "main_rows_float", "stream_rows_bool",
))
def test_example_checker_rejects_each_manifest_tamper(tmp_path, mutation):
    """教学 checker 独立验证 manifest 键序、身份、摘要、路径与行数。"""
    checker = _example_checker()
    paths, rows, manifest = _checker_manifest_fixture(tmp_path, checker)
    checker._assert_manifest(paths, rows)
    if mutation == "top_order":
        manifest = dict(reversed(tuple(manifest.items())))
    elif mutation == "nested_order":
        manifest["main"] = dict(reversed(tuple(manifest["main"].items())))
    elif mutation == "rows":
        manifest["stream"]["rows"] = 2
    elif mutation == "sha":
        manifest["main"]["sha256"] = "0" * 64
    elif mutation == "path":
        manifest["report"]["path"] = "/forged"
    elif mutation == "committed":
        manifest["committed_at"] = "invalid"
    elif mutation == "schema_float":
        manifest["schema_version"] = 1.0
    elif mutation == "main_rows_float":
        manifest["main"]["rows"] = 1.0
    elif mutation == "stream_rows_bool":
        manifest["stream"]["rows"] = True
    else:
        manifest["unexpected"] = True
    paths[3].write_text(json.dumps(manifest, separators=(",", ":")) + "\n", encoding="utf-8")
    with pytest.raises((AssertionError, ValueError)):
        checker._assert_manifest(paths, rows)


def _wire_declared_checker(monkeypatch, checker, *, forbidden: bool = False):
    """给 declared 顶层入口提供最小工件，并记录全部 oracle 调用。"""
    paths = checker._declared_paths()
    main = [{"main": index} for index in range(8)]
    if forbidden:
        main[0]["hidden"] = "catalog-secret-forbidden"
    stream = [{"stream": 0}]
    primary = [{"primary": index} for index in range(22)]
    noise = [{"noise": index} for index in range(2)]
    replay = [{"replay": index} for index in range(3)]
    sequence = {"program_digest": "d" * 64, "run_id": "a" * 32}
    calls = []

    monkeypatch.setattr(
        checker, "_load_jsonl", lambda path: main if path == paths[0] else stream,
    )
    monkeypatch.setattr(
        checker, "_load_json", lambda _path: {"generate": {"sequence": sequence}},
    )

    def split(_rows):
        calls.append("split_stream")
        return primary, noise, replay

    monkeypatch.setattr(checker, "_split_stream", split)
    for name in (
        "_assert_stream_timestamps", "_assert_declared_main", "_assert_cross_view",
        "_assert_declared_patterns", "_assert_noise", "_assert_replay",
        "_assert_sequence_report", "_assert_manifest",
    ):
        monkeypatch.setattr(
            checker, name,
            lambda *_args, _name=name, **_kwargs: calls.append(_name.removeprefix("_assert_")),
        )
    return calls


def test_declared_checker_invokes_every_required_oracle(monkeypatch):
    """declared 顶层入口必须按冻结顺序调用全部独立 oracle。"""
    checker = _example_checker()
    calls = _wire_declared_checker(monkeypatch, checker)

    checker.check_declared()

    assert calls == [
        "stream_timestamps", "split_stream", "declared_main", "cross_view",
        "declared_patterns", "noise", "replay", "sequence_report", "manifest",
    ]


def test_declared_checker_rejects_forbidden_catalog_sentinel(monkeypatch):
    """即使结构 oracle 通过，隐藏 catalog sentinel 也不得出现在交付内容。"""
    checker = _example_checker()
    calls = _wire_declared_checker(monkeypatch, checker, forbidden=True)

    with pytest.raises(AssertionError):
        checker.check_declared()

    assert "sequence_report" not in calls and "manifest" not in calls


def test_instruction_checker_invokes_every_required_oracle(monkeypatch):
    """instruction-only 顶层入口必须实际调用时间、交叉视图、报告与 manifest oracle。"""
    checker = _example_checker()
    truth = {
        "validation_mode": "instruction_only",
        "actor_knowledge_validation": "semantic",
    }
    main = [{"_meta": {"generation": truth}}]
    frames = ("task_request", "acknowledgement", "confirmation")
    stream = [{"_meta": {"event": {
        "role": f"position_{index:03d}", "frame_class": frame,
        "logical_time_us": index * 15_000_000,
    }}} for index, frame in enumerate(frames)]
    sequence = {"program_digest": "d" * 64}
    calls = []

    monkeypatch.setattr(
        checker, "_load_jsonl",
        lambda path: main if path.name == "instruction-only-labels.jsonl" else stream,
    )
    monkeypatch.setattr(
        checker, "_load_json", lambda _path: {"generate": {"sequence": sequence}},
    )
    for name in (
        "_assert_stream_timestamps", "_assert_owner_timeline", "_assert_cross_view",
        "_assert_sequence_report", "_assert_manifest",
    ):
        monkeypatch.setattr(
            checker, name,
            lambda *_args, _name=name, **_kwargs: calls.append(_name.removeprefix("_assert_")),
        )

    checker.check_instruction_only()

    assert calls == [
        "stream_timestamps", "owner_timeline", "cross_view",
        "sequence_report", "manifest",
    ]


@pytest.mark.parametrize("tamper", (
    "cardinality", "validation_mode", "actor_knowledge",
    "forbidden_scenario_set", "forbidden_pattern", "forbidden_variant",
    "forbidden_expected_violation", "forbidden_actual_violations",
    "role_0", "role_1", "role_2", "frame_class_0", "frame_class_1",
    "frame_class_2", "logical_time_0", "logical_time_1", "logical_time_2",
))
def test_instruction_checker_rejects_each_inline_truth_tamper(monkeypatch, tamper):
    """instruction-only 顶层入口的每簇直接真值断言都必须可观测。"""
    checker = _example_checker()
    truth = {
        "validation_mode": "instruction_only",
        "actor_knowledge_validation": "semantic",
    }
    main = [{"_meta": {"generation": truth}}]
    frames = ("task_request", "acknowledgement", "confirmation")
    stream = [{"_meta": {"event": {
        "role": f"position_{index:03d}", "frame_class": frame,
        "logical_time_us": index * 15_000_000,
    }}} for index, frame in enumerate(frames)]
    if tamper == "cardinality":
        stream.pop()
    elif tamper in ("validation_mode", "actor_knowledge"):
        key = "validation_mode" if tamper == "validation_mode" else "actor_knowledge_validation"
        truth[key] = "forged"
    elif tamper.startswith("forbidden_"):
        truth[tamper.removeprefix("forbidden_")] = "forged"
    else:
        field, index_text = tamper.rsplit("_", 1)
        key = "logical_time_us" if field == "logical_time" else field
        stream[int(index_text)]["_meta"]["event"][key] = (
            -1 if key == "logical_time_us" else "forged"
        )
    sequence = {"program_digest": "d" * 64}
    monkeypatch.setattr(
        checker, "_load_jsonl",
        lambda path: main if path.name == "instruction-only-labels.jsonl" else stream,
    )
    monkeypatch.setattr(
        checker, "_load_json", lambda _path: {"generate": {"sequence": sequence}},
    )
    for name in (
        "_assert_stream_timestamps", "_assert_owner_timeline", "_assert_cross_view",
        "_assert_sequence_report", "_assert_manifest",
    ):
        monkeypatch.setattr(checker, name, lambda *_args, **_kwargs: None)

    with pytest.raises(AssertionError):
        checker.check_instruction_only()


def test_frame_only_checker_invokes_every_required_oracle(monkeypatch):
    """frame-only 顶层入口必须校验帧对象、时间线、交叉视图、报告与 manifest。"""
    checker = _example_checker()
    main = [{"_meta": {"annotation": None, "scores": {"__aggregate__": 1.0}}}]
    stream = [{"frame": index} for index in range(3)]
    sequence = {"program_digest": "d" * 64, "rejected_attempts": {"annotate": 0}}
    report = {
        "schema_engine": {"resolved_at": {}},
        "generate": {"sequence": sequence},
        "counts": {},
    }
    calls = []

    monkeypatch.setattr(
        checker, "_frame_only_validator",
        lambda: calls.append("frame_validator") or object(),
    )
    monkeypatch.setattr(
        checker, "_load_jsonl",
        lambda path: main if path.name == "frame-only-labels.jsonl" else stream,
    )
    monkeypatch.setattr(checker, "_load_json", lambda _path: report)
    for name in (
        "_assert_stream_timestamps", "_assert_frame_only_annotations",
        "_assert_owner_timeline", "_assert_cross_view", "_assert_sequence_report",
        "_assert_manifest",
    ):
        monkeypatch.setattr(
            checker, name,
            lambda *_args, _name=name, **_kwargs: calls.append(_name.removeprefix("_assert_")),
        )

    checker.check_frame_only()

    assert calls == [
        "frame_validator", "stream_timestamps", "frame_only_annotations",
        "owner_timeline", "cross_view", "sequence_report", "manifest",
    ]


@pytest.mark.parametrize("tamper", (
    "cardinality", "sequence_annotation", "resolved_calls", "quality",
    "frame_annotate_failed", "frame_annotate_discarded", "annotate_rejection",
))
def test_frame_only_checker_rejects_each_inline_truth_tamper(monkeypatch, tamper):
    """frame-only 运行工件的每簇直接真值断言都必须可观测。"""
    checker = _example_checker()
    main = [{"_meta": {"annotation": None, "scores": {"__aggregate__": 1.0}}}]
    stream = [{"frame": index} for index in range(3)]
    sequence = {"program_digest": "d" * 64, "rejected_attempts": {"annotate": 0}}
    report = {
        "schema_engine": {"resolved_at": {}},
        "generate": {"sequence": sequence},
        "counts": {},
    }
    if tamper == "cardinality":
        stream.pop()
    elif tamper == "sequence_annotation":
        main[0]["_meta"]["annotation"] = {"forged": True}
    elif tamper == "resolved_calls":
        report["schema_engine"]["resolved_at"] = {"l0_or_clean": 1}
    elif tamper == "quality":
        main[0]["_meta"]["scores"] = {"other": 1.0}
    elif tamper.startswith("frame_annotate_"):
        suffix = tamper.removeprefix("frame_annotate_")
        report["counts"][f"frame_annotate.{suffix}"] = 1
    else:
        sequence["rejected_attempts"]["annotate"] = 1
    monkeypatch.setattr(checker, "_frame_only_validator", lambda: object())
    monkeypatch.setattr(
        checker, "_load_jsonl",
        lambda path: main if path.name == "frame-only-labels.jsonl" else stream,
    )
    monkeypatch.setattr(checker, "_load_json", lambda _path: report)
    for name in (
        "_assert_stream_timestamps", "_assert_frame_only_annotations",
        "_assert_owner_timeline", "_assert_cross_view", "_assert_sequence_report",
        "_assert_manifest",
    ):
        monkeypatch.setattr(checker, name, lambda *_args, **_kwargs: None)

    with pytest.raises(AssertionError):
        checker.check_frame_only()


@pytest.mark.parametrize("tamper", (
    "label", "status", "member_annotation", "primary_mismatch",
    "primary_validation", "id_mapping",
))
def test_frame_only_annotation_helper_rejects_each_cross_view_tamper(tamper):
    """帧标注 helper 独立锁定顺序、状态、Schema、同源与事件 ID 映射。"""
    from jsonschema import Draft202012Validator

    checker = _example_checker()
    labels = ("task_request", "acknowledgement", "confirmation")
    members = [{
        "id": f"event-{index}", "label": label, "status": "annotated",
        "annotation": {"value": label},
    } for index, label in enumerate(labels)]
    stream = [{
        "_meta": {
            "event": {"event_id": member["id"]},
            "annotation": dict(member["annotation"]),
        },
    } for member in members]
    row = {"_meta": {"stream": {"members": members}}}
    schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    validator = Draft202012Validator(schema)
    if tamper == "label":
        members[1]["label"] = "forged"
    elif tamper == "status":
        members[1]["status"] = "failed"
    elif tamper == "member_annotation":
        members[1]["annotation"] = {"value": 1}
    elif tamper == "primary_mismatch":
        stream[1]["_meta"]["annotation"] = {"value": "other"}
    elif tamper == "id_mapping":
        stream[1]["_meta"]["event"]["event_id"] = "forged"
    else:
        class PrimaryRejectingValidator:
            """只在三次 member 校验后的首个 primary 校验抛错。"""

            def __init__(self):
                self.calls = 0

            def validate(self, _value):
                self.calls += 1
                if self.calls == 4:
                    raise AssertionError("primary validation was reached")

        validator = PrimaryRejectingValidator()

    from jsonschema.exceptions import ValidationError

    with pytest.raises((AssertionError, KeyError, ValidationError)):
        checker._assert_frame_only_annotations(row, stream, validator)


@pytest.mark.parametrize("tamper", (
    "segment", "run_mode", "generate_form", "generate_mode", "slot_length",
    "slot_count", "len_range", "annotate", "frame_classify", "frame_annotate",
    "quality_enabled", "quality", "threshold", "output", "output_rejects",
    "schema_examples", "invalid_schema_example", "invalid_schema_meta",
))
def test_frame_only_static_checker_rejects_each_project_contract(
        monkeypatch, tmp_path, tamper):
    """frame-only 静态入口逐簇强制教学开关与根 Schema example。"""
    checker = _example_checker()
    root = tmp_path / "sequence-generation"
    source = Path(__file__).resolve().parents[3] / "examples" / "sequence-generation"
    shutil.copytree(source, root)
    project = root / "project-frame-only.toml"
    text = project.read_text(encoding="utf-8")
    replacements = {
        "run_mode": ('mode = "generate_only"', 'mode = "process"'),
        "generate_form": ('form = "sequence"', 'form = "standalone"'),
        "generate_mode": ('mode = "instruction_only"', 'mode = "declared"'),
        "slot_count": ("count = 1", "count = 2"),
        "len_range": ("len_range = [3, 3]", "len_range = [2, 3]"),
        "annotate": ("[annotate]\nenabled = false", "[annotate]\nenabled = true"),
        "frame_classify": (
            "[frame.classify]\nenabled = false", "[frame.classify]\nenabled = true",
        ),
        "frame_annotate": (
            "[frame.annotate]\nenabled = true", "[frame.annotate]\nenabled = false",
        ),
        "quality_enabled": ("[quality]\nenabled = true", "[quality]\nenabled = false"),
        "quality": ('mode = "pointwise"', 'mode = "pairwise_bt"'),
        "threshold": ("threshold = 0.0", "threshold = 0.5"),
        "output": ('meta_mode = "inline"', 'meta_mode = "sidecar"'),
        "output_rejects": ('rejects = "none"', 'rejects = "file"'),
    }
    if tamper == "segment":
        text += "\n[segment]\nenabled = false\n"
    elif tamper == "slot_length":
        text += '''
[[generate.instruction_only]]
name = "forged_second_slot"
sequence_class = "ticket_booking"
count = 1
len_range = [3, 3]
instruction = "forged"
'''
    elif tamper in ("schema_examples", "invalid_schema_example", "invalid_schema_meta"):
        schema_path = root / "schemas" / "frame-annotation.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        if tamper == "schema_examples":
            schema["examples"] = []
        elif tamper == "invalid_schema_example":
            schema["examples"][0]["observed_status"] = "forged"
        else:
            schema["properties"]["unused"] = "not-a-schema"
        schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
    else:
        old, new = replacements[tamper]
        assert old in text
        text = text.replace(old, new, 1)
    project.write_text(text, encoding="utf-8")
    monkeypatch.setattr(checker, "ROOT", root)
    monkeypatch.setattr(checker, "OUT", root / "out")

    from jsonschema.exceptions import SchemaError, ValidationError

    with pytest.raises((AssertionError, SchemaError, ValidationError)):
        checker.check_frame_only_static()


@pytest.mark.parametrize("tamper", (
    "scanned", "absorbed", "dropped_noise", "episodes", "dropped_dup",
    "emitted", "failed", "output_rows",
))
def test_replay_checker_rejects_each_count_or_output_tamper(monkeypatch, tamper):
    """replay 顶层入口逐项强制冻结计数与输出 cardinality。"""
    checker = _example_checker()
    counts = {
        "scanned": 27, "absorbed": 25, "dropped_noise": 2, "episodes": 9,
        "dropped_dup": 1, "emitted": 8, "failed": 0,
    }
    rows = [{} for _index in range(8)]
    if tamper == "output_rows":
        rows.pop()
    else:
        counts[tamper] += 1
    monkeypatch.setattr(checker, "_load_json", lambda _path: {"counts": counts})
    monkeypatch.setattr(checker, "_load_jsonl", lambda _path: rows)

    with pytest.raises(AssertionError):
        checker.check_replay()


@pytest.mark.parametrize(("args", "target"), (
    ((), "check_declared"),
    (("--instruction-only",), "check_instruction_only"),
    (("--frame-only",), "check_frame_only"),
    (("--frame-only", "--static"), "check_frame_only_static"),
    (("--replay",), "check_replay"),
))
def test_checker_main_dispatches_every_teaching_entry(monkeypatch, args, target):
    """CLI checker 的五个用户入口必须各自到达唯一对应检查函数。"""
    checker = _example_checker()
    calls = []
    for name in (
        "check_declared", "check_instruction_only", "check_frame_only",
        "check_frame_only_static", "check_replay",
    ):
        monkeypatch.setattr(
            checker, name,
            lambda _name=name: calls.append(_name),
        )
    monkeypatch.setattr(sys, "argv", ["check_output.py", *args])

    checker.main()

    assert calls == [target]


def _tamper_stream_top(row, mutation):
    """给 stream row 注入额外顶层字段或颠倒冻结键序。"""
    changed = _thaw(row)
    if mutation == "extra":
        changed["forged"] = True
        return changed
    return {"_meta": changed["_meta"], "payload": changed["payload"]}


@pytest.mark.parametrize("mutation", ("extra", "order"))
def test_reconcile_rejects_primary_top_level_contract_tamper(projected_set, mutation):
    """primary 顶层只接受按 payload、_meta 排列的两个字段。"""
    program, plan, projection, sequence, _noise, _replay = projected_set
    main, rows = _thaw(sequence.main_row), _thaw(sequence.primary_stream_rows)
    rows[0] = _tamper_stream_top(rows[0], mutation)
    retained = sum(_independent_row_bytes(row) for row in (main, *rows))
    tampered = SequenceRows(main, tuple(rows), retained)
    with pytest.raises(GenerationProjectionMismatch):
        _reconcile_candidate_bundle(_primary_only_request(
            program, plan, projection, tampered
        ))


@pytest.mark.parametrize("mutation", ("extra", "order"))
def test_reconcile_rejects_noise_top_level_contract_tamper(projected_set, mutation):
    """noise 顶层只接受按 payload、_meta 排列的两个字段。"""
    program, plan, projection, sequence, noise, replay = projected_set
    changed = _tamper_stream_top(noise, mutation)
    with pytest.raises(GenerationProjectionMismatch):
        _reconcile_candidate_bundle(_request(
            program, plan, projection, sequence, changed, replay
        ))


@pytest.mark.parametrize("mutation", ("extra", "order"))
def test_reconcile_rejects_replay_top_level_contract_tamper(projected_set, mutation):
    """replay 顶层只接受按 payload、_meta 排列的两个字段。"""
    program, plan, projection, sequence, noise, replay = projected_set
    rows = _thaw(replay.rows)
    rows[0] = _tamper_stream_top(rows[0], mutation)
    changed = ReplayRows(tuple(rows), sum(_independent_row_bytes(row) for row in rows))
    with pytest.raises(GenerationProjectionMismatch):
        _reconcile_candidate_bundle(_request(
            program, plan, projection, sequence, noise, changed
        ))


@pytest.mark.parametrize("mutation", ("meta_extra", "label", "labels"))
def test_reconcile_rejects_main_metadata_contract_tamper(projected_set, mutation):
    """main 元数据闭集与 inherited sequence classification 必须精确。"""
    program, plan, projection, sequence, _noise, _replay = projected_set
    main, rows = _thaw(sequence.main_row), _thaw(sequence.primary_stream_rows)
    if mutation == "meta_extra":
        main["_meta"]["forged"] = True
    else:
        main["_meta"]["classification"][mutation] = ["other"] if mutation == "labels" else "other"
    retained = sum(_independent_row_bytes(row) for row in (main, *rows))
    tampered = SequenceRows(main, tuple(rows), retained)
    with pytest.raises(GenerationProjectionMismatch):
        _reconcile_candidate_bundle(_primary_only_request(
            program, plan, projection, tampered
        ))


def test_example_checker_rejects_global_stream_reordering():
    """不同 owner 的行交换也必须被全文件时间序 oracle 拒绝。"""
    checker = _example_checker()
    expected = checker.INSTRUCTION_TIMESTAMPS
    rows = [{"_meta": {"event": {"timestamp": value}}} for value in expected]
    checker._assert_stream_timestamps(rows, expected)
    rows[0], rows[1] = rows[1], rows[0]
    with pytest.raises(AssertionError, match="globally strictly increasing"):
        checker._assert_stream_timestamps(rows, expected)


def test_example_checker_rejects_synchronized_replay_time_shift(projected_set):
    """replay 整组平移并重算 event ID 仍违反冻结 tail layout。"""
    _program, _plan, _projection, sequence, noise, replay = projected_set
    checker = _example_checker()
    primary = _thaw(sequence.primary_stream_rows)
    replay_rows = _thaw(replay.rows)
    first_replay = _timestamp_us(replay_rows[0]["_meta"]["event"]["timestamp"])
    prior = _thaw(noise)
    prior["_meta"]["event"]["timestamp"] = _timestamp_text(
        first_replay - 3_600_000_000, 480
    )
    stream = [*primary, prior, *replay_rows]
    checker._assert_replay(primary, replay_rows, stream)
    for row in replay_rows:
        event = row["_meta"]["event"]
        shifted = _timestamp_us(event["timestamp"]) + 10_000_000
        event["timestamp"] = _timestamp_text(shifted, 480)
        event["event_id"] = derive_generation_id(
            "replay_event_id",
            [
                event["replay_sequence_id"], event["duplicate_of_event_id"], shifted,
                event["duration_us"], row["payload"],
            ],
        )
    with pytest.raises(AssertionError, match="frozen tail layout"):
        checker._assert_replay(primary, replay_rows, stream)


@pytest.mark.parametrize("field,value", (
    ("event_key", "b" * 32),
    ("owner_sequence_id", "c" * 32),
    ("role", "confirm"),
    ("frame_class", "confirmation"),
    ("logical_time_us", -1),
    ("timestamp", "2026-01-05T00:00:00.000000+08:00"),
    ("event_id", "d" * 32),
))
def test_reconcile_rejects_each_primary_identity_tamper(projected_set, field, value):
    program, plan, projection, sequence, noise, replay = projected_set
    main, rows = _thaw(sequence.main_row), _thaw(sequence.primary_stream_rows)
    rows[0]["_meta"]["event"][field] = value
    tampered = SequenceRows(main, tuple(rows), 0)
    expected = InternalError if field in {"frame_class", "timestamp"} else GenerationProjectionMismatch
    with pytest.raises(expected):
        _reconcile_candidate_bundle(_primary_only_request(program, plan, projection, tampered))


def test_reconcile_rejects_primary_actor_tamper_against_replay_source(projected_set):
    program, plan, projection, sequence, noise, replay = projected_set
    main, rows = _thaw(sequence.main_row), _thaw(sequence.primary_stream_rows)
    rows[0]["_meta"]["event"]["actor"] = "system"
    tampered = SequenceRows(main, tuple(rows), 0)
    with pytest.raises(GenerationProjectionMismatch):
        _reconcile_candidate_bundle(_request(program, plan, projection, tampered, noise, replay))


def test_reconcile_rejects_payload_main_members_and_generation_tamper(projected_set):
    program, plan, projection, sequence, noise, replay = projected_set
    mutations = []
    main, rows = _thaw(sequence.main_row), _thaw(sequence.primary_stream_rows)
    rows[0]["payload"]["request_id"] = "changed"
    mutations.append(SequenceRows(main, tuple(rows), 0))
    main, rows = _thaw(sequence.main_row), _thaw(sequence.primary_stream_rows)
    main["_meta"]["stream"]["member_ids"].reverse()
    mutations.append(SequenceRows(main, tuple(rows), 0))
    main, rows = _thaw(sequence.main_row), _thaw(sequence.primary_stream_rows)
    main["_meta"]["generation"]["variant"] = "missing_acknowledgement"
    mutations.append(SequenceRows(main, tuple(rows), 0))
    main, rows = _thaw(sequence.main_row), _thaw(sequence.primary_stream_rows)
    main["_meta"]["id"] = "9" * 32
    mutations.append(SequenceRows(main, tuple(rows), 0))
    main, rows = _thaw(sequence.main_row), _thaw(sequence.primary_stream_rows)
    main["_meta"]["stream"]["members"][0]["label"] = "confirmation"
    mutations.append(SequenceRows(main, tuple(rows), 0))
    main, rows = _thaw(sequence.main_row), _thaw(sequence.primary_stream_rows)
    rows[0]["_meta"]["event"]["unexpected"] = True
    mutations.append(SequenceRows(main, tuple(rows), 0))
    for tampered in mutations:
        with pytest.raises(GenerationProjectionMismatch):
            _reconcile_candidate_bundle(
                _primary_only_request(program, plan, projection, tampered)
            )


def test_reconcile_rejects_synchronized_primary_payload_and_id_rewrite(projected_set):
    """最终行内部完全自洽也不能替换不可变 projector payload。"""
    program, plan, projection, sequence, _noise, _replay = projected_set
    main, rows = _thaw(sequence.main_row), _thaw(sequence.primary_stream_rows)
    rows[0]["payload"]["utterance"] = "同步改写"
    world = main["_meta"]["generation"]["world_branch_id"]
    planned = _branch(plan, plan.delivery_slots[0].slot_key, "positive")
    event_ids = [row["_meta"]["event"]["event_id"] for row in rows]
    event_ids[0] = derive_generation_id(
        "primary_event_id",
        [world, planned[0].event_key, planned[0].timestamp_us, rows[0]["payload"]],
    )
    sequence_id = derive_generation_id("sequence_id", [world, event_ids])
    for index, row in enumerate(rows):
        row["_meta"]["event"]["event_id"] = event_ids[index]
        row["_meta"]["event"]["owner_sequence_id"] = sequence_id
    stream = main["_meta"]["stream"]
    main["_meta"]["id"] = stream["episode_id"] = sequence_id
    stream["member_ids"] = event_ids
    for index, member in enumerate(stream["members"]):
        member["id"] = event_ids[index]
    with pytest.raises(GenerationProjectionMismatch):
        _reconcile_candidate_bundle(_primary_only_request(
            program, plan, projection, SequenceRows(main, tuple(rows), 0)
        ))


def test_reconcile_closes_member_sources_classification_and_annotation(projected_set):
    """primary 下游字段只允许生产契约的机械形状与双向同值。"""
    program, plan, projection, sequence, _noise, _replay = projected_set
    mutations = []
    main, rows = _thaw(sequence.main_row), _thaw(sequence.primary_stream_rows)
    main["_meta"]["stream"]["member_sources"][0] = {"file": "tampered", "line_no": 9}
    mutations.append(SequenceRows(main, tuple(rows), 0))
    main, rows = _thaw(sequence.main_row), _thaw(sequence.primary_stream_rows)
    rows[0]["_meta"].pop("classification")
    mutations.append(SequenceRows(main, tuple(rows), 0))
    main, rows = _thaw(sequence.main_row), _thaw(sequence.primary_stream_rows)
    rows[0]["_meta"]["classification"]["label"] = "wrong"
    mutations.append(SequenceRows(main, tuple(rows), 0))
    main, rows = _thaw(sequence.main_row), _thaw(sequence.primary_stream_rows)
    rows[0]["_meta"]["annotation"] = {"value": "orphan"}
    mutations.append(SequenceRows(main, tuple(rows), 0))
    for tampered in mutations:
        with pytest.raises(GenerationProjectionMismatch):
            _reconcile_candidate_bundle(
                _primary_only_request(program, plan, projection, tampered)
            )

    main, rows = _thaw(sequence.main_row), _thaw(sequence.primary_stream_rows)
    rows[0]["_meta"]["annotation"] = {"value": "accepted"}
    member = main["_meta"]["stream"]["members"][0]
    member.update({"annotation": {"value": "accepted"}, "status": "annotated"})
    final_rows = tuple(rows)
    retained = sum(
        len(canonical_delivery_row(row)) + 1 for row in (main, *final_rows)
    )
    _reconcile_candidate_bundle(_primary_only_request(
        program, plan, projection, SequenceRows(main, final_rows, retained)
    ))


@pytest.mark.parametrize("session_id", (None, "wrong-session"))
def test_reconcile_requires_exact_planned_main_session(projected_set, session_id):
    program, plan, projection, sequence, _noise, _replay = projected_set
    main = _thaw(sequence.main_row)
    main["_meta"]["stream"]["session_id"] = session_id
    tampered = SequenceRows(main, sequence.primary_stream_rows, 0)
    with pytest.raises(GenerationProjectionMismatch):
        _reconcile_candidate_bundle(_primary_only_request(
            program, plan, projection, tampered
        ))


@pytest.mark.parametrize("field,value", (
    ("duplicate_of_event_id", "e" * 32),
    ("duplicate_of_sequence_id", "f" * 32),
    ("replay_sequence_id", "1" * 32),
    ("replay_ordinal", 99),
    ("role", "wrong"),
    ("frame_class", "wrong"),
    ("actor", "wrong"),
    ("logical_time_us", -1),
    ("event_key", "8" * 32),
    ("timestamp", "2026-01-05T00:00:00.000000+08:00"),
    ("event_id", "2" * 32),
))
def test_reconcile_rejects_each_replay_identity_tamper(projected_set, field, value):
    program, plan, projection, sequence, noise, replay = projected_set
    rows = _thaw(replay.rows)
    rows[0]["_meta"]["event"][field] = value
    error = InternalError if field == "timestamp" else GenerationProjectionMismatch
    with pytest.raises(error):
        request = _request(program, plan, projection, sequence, noise, replay)
        _reconcile_candidate_bundle(replace(
            request, replays=(replace(replay, rows=tuple(rows)),)
        ))


def test_reconcile_rejects_replay_payload_generation_and_extra_field(projected_set):
    program, plan, projection, sequence, noise, replay = projected_set
    mutations = []
    rows = _thaw(replay.rows)
    rows[0]["payload"]["request_id"] = "changed"
    mutations.append(rows)
    rows = _thaw(replay.rows)
    rows[0]["_meta"]["generation"]["source_variant"] = "wrong"
    mutations.append(rows)
    rows = _thaw(replay.rows)
    rows[0]["_meta"]["event"]["noise"] = True
    mutations.append(rows)
    for rows in mutations:
        with pytest.raises(GenerationProjectionMismatch):
            request = _request(program, plan, projection, sequence, noise, replay)
            _reconcile_candidate_bundle(replace(
                request, replays=(replace(replay, rows=tuple(rows)),)
            ))


def test_reconcile_rejects_equivalent_replay_instant_with_wrong_offset(projected_set):
    """replay timestamp 文本必须使用 program 固定 offset，不能只比较 epoch。"""
    program, plan, projection, sequence, noise, replay = projected_set
    rows = _thaw(replay.rows)
    timestamp = rows[0]["_meta"]["event"]["timestamp"]
    rows[0]["_meta"]["event"]["timestamp"] = datetime.fromisoformat(timestamp).astimezone(
        timezone.utc
    ).isoformat(timespec="microseconds")
    request = _request(program, plan, projection, sequence, noise, replay)
    with pytest.raises(InternalError):
        _reconcile_candidate_bundle(replace(
            request, replays=(replace(replay, rows=tuple(rows)),)
        ))


@pytest.mark.parametrize("field,value", (
    ("event_key", "3" * 32),
    ("owner_sequence_id", "4" * 32),
    ("role", "request"),
    ("frame_class", "task_request"),
    ("noise", False),
    ("timestamp", "2026-01-05T00:00:00.000000+08:00"),
    ("event_id", "invalid"),
))
def test_reconcile_rejects_each_noise_identity_tamper(projected_set, field, value):
    program, plan, projection, sequence, noise, replay = projected_set
    row = _thaw(noise)
    row["_meta"]["event"][field] = value
    error = InternalError if field == "timestamp" else GenerationProjectionMismatch
    with pytest.raises(error):
        _reconcile_candidate_bundle(
            _request(program, plan, projection, sequence, row, replay)
        )


def test_reconcile_rejects_noise_generation_and_extra_branch_truth(projected_set):
    program, plan, projection, sequence, noise, replay = projected_set
    rows = []
    row = _thaw(noise)
    row["_meta"]["generation"] = {"variant": "positive"}
    rows.append(row)
    row = _thaw(noise)
    row["_meta"]["event"]["scenario_id"] = "7" * 32
    rows.append(row)
    for row in rows:
        with pytest.raises(GenerationProjectionMismatch):
            _reconcile_candidate_bundle(
                _request(program, plan, projection, sequence, row, replay)
            )


def test_reconcile_rejects_synchronized_noise_payload_id_and_offset_rewrite(projected_set):
    """noise 必须对照 post-gate payload，并使用 program 固定 offset 文本。"""
    program, plan, projection, sequence, noise, replay = projected_set
    request = _request(program, plan, projection, sequence, noise, replay)
    row = _thaw(noise)
    row["payload"]["utterance"] = "同步改写"
    slot = plan.noise_slots[0]
    row["_meta"]["event"]["event_id"] = derive_generation_id(
        "noise_event_id",
        [
            "a" * 32, slot.event_key, slot.timestamp_us, 0, [],
            row["_meta"]["event"]["time_bindings"], row["payload"],
        ],
    )
    with pytest.raises(GenerationProjectionMismatch):
        _reconcile_candidate_bundle(replace(request, noise_rows=(row,)))

    row = _thaw(noise)
    timestamp = row["_meta"]["event"]["timestamp"]
    row["_meta"]["event"]["timestamp"] = datetime.fromisoformat(timestamp).astimezone(
        timezone.utc
    ).isoformat(timespec="microseconds")
    with pytest.raises(InternalError):
        _reconcile_candidate_bundle(replace(request, noise_rows=(row,)))


def test_projection_contract_errors_are_terminal_not_reconcile_rejections(projected_set):
    program, plan, _projection, sequence, _noise, _replay = projected_set
    slot, trace = _trace(program, plan)
    wrong_trace = replace(trace, sequence_class="wrong")
    with pytest.raises(InternalError, match="trace does not match"):
        project_trace(ProjectionRequest(
            program, plan, slot, wrong_trace
        ))
    layout = replace(
        plan.replay_layouts[0],
        shift_us=plan.replay_layouts[0].shift_us + 1000,
    )
    with pytest.raises(InternalError, match="replay layout"):
        project_replay(ReplayProjectionRequest(program, plan, layout, sequence))


@pytest.mark.parametrize("tamper", (
    "bindings", "violations", "state_replay_hash", "state_bindings", "state_outcome",
    "state_prefix", "semantic", "semantic_reason", "role_word", "frame_class", "actor",
    "duplicate_event_id",
))
def test_projection_rejects_forged_gate_truth(declared_program, tamper):
    """projector 不接受与独立 gate 或事件真值矛盾的同步载体。"""
    plan = compile_scenario_plan(declared_program)
    slot, trace = _trace(declared_program, plan)
    if tamper == "bindings":
        evaluation = replace(trace.pattern_evaluation, actual_bindings={})
        trace = replace(trace, pattern_evaluation=evaluation)
    elif tamper == "violations":
        violation = ({"kind": "missing_role", "target": "request"},)
        evaluation = replace(trace.pattern_evaluation, actual_violations=violation)
        trace = replace(trace, pattern_evaluation=evaluation)
    elif tamper == "state_replay_hash":
        trace = replace(
            trace,
            state_evaluation=replace(trace.state_evaluation, replay_hash="forged"),
        )
    elif tamper == "state_bindings":
        trace = replace(
            trace,
            state_evaluation=replace(trace.state_evaluation, bindings_valid=False),
        )
    elif tamper == "state_outcome":
        trace = replace(
            trace,
            state_evaluation=replace(trace.state_evaluation, outcome_valid=False),
        )
    elif tamper == "state_prefix":
        trace = replace(
            trace,
            state_evaluation=replace(trace.state_evaluation, protected_prefix_valid=False),
        )
    elif tamper == "semantic":
        trace = replace(
            trace,
            semantic_evaluation=replace(trace.semantic_evaluation, realism=False),
        )
    elif tamper == "semantic_reason":
        trace = replace(
            trace,
            semantic_evaluation=replace(trace.semantic_evaluation, reason_codes=("unrealistic",)),
        )
    elif tamper == "role_word":
        events = list(trace.events)
        events[0] = replace(events[0], role="acknowledge")
        trace = replace(trace, events=tuple(events))
    elif tamper == "frame_class":
        events = list(trace.events)
        events[0] = replace(events[0], frame_class="confirmation")
        trace = replace(trace, events=tuple(events))
    elif tamper == "actor":
        events = list(trace.events)
        events[0] = replace(events[0], actor="system")
        trace = replace(trace, events=tuple(events))
    else:
        events = list(trace.events)
        events[1] = replace(events[1], event_id=events[0].event_id)
        trace = replace(trace, events=tuple(events))
    with pytest.raises(InternalError, match="generation_downstream_contract"):
        project_trace(ProjectionRequest(
            declared_program, plan, slot, trace,
        ))


def test_projection_rejects_synchronized_event_and_branch_identity_forgery(declared_program):
    """同步改 evaluator bindings 也不能伪造 canonical event 或 branch ID。"""
    plan = compile_scenario_plan(declared_program)
    slot, trace = _trace(declared_program, plan)
    events = list(trace.events)
    original = events[0]
    events[0] = replace(original, event_id="f" * 32)
    bindings = dict(trace.pattern_evaluation.actual_bindings)
    bindings.pop(original.event_id)
    bindings[events[0].event_id] = events[0].role
    forged = replace(
        trace,
        events=tuple(events),
        pattern_evaluation=replace(trace.pattern_evaluation, actual_bindings=bindings),
    )
    with pytest.raises(InternalError, match="trace event identity"):
        project_trace(ProjectionRequest(declared_program, plan, slot, forged))

    scenario_id, world_id = "e" * 32, "d" * 32
    events = tuple(replace(
        event,
        event_id=derive_generation_id(
            "primary_event_id",
            [world_id, event.event_key, event.timestamp_us, event.payload],
        ),
    ) for event in trace.events)
    evaluation = replace(
        trace.pattern_evaluation,
        actual_bindings={event.event_id: event.role for event in events},
    )
    forged = replace(
        trace, scenario_id=scenario_id, world_branch_id=world_id,
        events=events, pattern_evaluation=evaluation,
    )
    with pytest.raises(InternalError, match="trace branch identity"):
        project_trace(ProjectionRequest(declared_program, plan, slot, forged))


@pytest.mark.parametrize("field", (
    "source_name", "scenario_index", "sequence_class", "pattern_name",
    "catalog_row_index", "variant_names",
))
def test_projection_requires_exact_planned_delivery_slot(declared_program, field):
    """同步改 trace 身份也不能把独立 DeliverySlot 冒充计划成员。"""
    plan = compile_scenario_plan(declared_program)
    slot, trace = _trace(declared_program, plan)
    changes = {
        "source_name": {"source_name": "forged_source"},
        "scenario_index": {"scenario_index": slot.scenario_index + 1},
        "sequence_class": {"sequence_class": "forged_class"},
        "pattern_name": {"pattern_name": "forged_pattern"},
        "catalog_row_index": {"catalog_row_index": slot.catalog_row_index + 1},
        "variant_names": {"variant_names": (*slot.variant_names, "forged_variant")},
    }
    forged_slot = replace(slot, **changes[field])
    scenario_id = derive_generation_id(
        "declared_scenario_id",
        [declared_program.digest, forged_slot.source_name, forged_slot.scenario_index],
    )
    world_id = derive_generation_id(
        "declared_world_branch_id", [scenario_id, trace.variant_name]
    )
    events = tuple(replace(
        event,
        event_id=derive_generation_id(
            "primary_event_id",
            [world_id, event.event_key, event.timestamp_us, event.payload],
        ),
    ) for event in trace.events)
    evaluation = replace(
        trace.pattern_evaluation,
        actual_bindings={event.event_id: event.role for event in events},
    )
    forged_trace = replace(
        trace,
        scenario_id=scenario_id,
        world_branch_id=world_id,
        sequence_class=forged_slot.sequence_class,
        pattern_name=forged_slot.pattern_name,
        events=events,
        pattern_evaluation=evaluation,
    )
    with pytest.raises(InternalError, match="projection slot"):
        project_trace(ProjectionRequest(declared_program, plan, forged_slot, forged_trace))


@pytest.mark.parametrize("field", ("logical_time_us", "timestamp_us", "session_id"))
def test_projection_rejects_self_digesting_noncanonical_plan(declared_program, field):
    """即使 trace 同步，公开 projector 也只接受 planner 的 canonical plan。"""
    plan = compile_scenario_plan(declared_program)
    slot, trace = _trace(declared_program, plan)
    key = (slot.slot_key, trace.variant_name)
    blocks = [dict(block) for block in plan.blocks]
    branch = list(next(block[key] for block in blocks if key in block))
    value = "forged-session" if field == "session_id" else getattr(branch[0], field) + 1
    branch[0] = replace(branch[0], **{field: value})
    next(block for block in blocks if key in block)[key] = tuple(branch)
    forged_plan = replace(plan, blocks=tuple(blocks), digest="")
    forged_plan = replace(forged_plan, digest=scenario_plan_digest(forged_plan))
    if field != "session_id":
        events = list(trace.events)
        events[0] = replace(events[0], **{field: value})
        if field == "timestamp_us":
            event = events[0]
            events[0] = replace(event, event_id=derive_generation_id(
                "primary_event_id",
                [
                    trace.world_branch_id, event.event_key, value, event.duration_us,
                    list(branch[0].resources), [], event.payload,
                ],
            ))
        evaluation = replace(
            trace.pattern_evaluation,
            actual_bindings={event.event_id: event.role for event in events},
        )
        trace = replace(trace, events=tuple(events), pattern_evaluation=evaluation)
    with pytest.raises(InternalError, match="canonical planner"):
        project_trace(ProjectionRequest(declared_program, forged_plan, slot, trace))


@pytest.mark.parametrize("field", (
    "source_slot_key", "positive_variant", "unknown_variant", "replay_ordinal",
    "session_id", "shift_us",
))
def test_replay_projector_requires_exact_planned_positive_layout(projected_set, field):
    """ReplayProjector 在造行前拒绝任一伪造的来源或布局字段。"""
    program, plan, _projection, sequence, _noise, _replay = projected_set
    layout = plan.replay_layouts[0]
    changes = {
        "source_slot_key": {"source_slot_key": "forged"},
        "positive_variant": {"source_variant_name": "confirmation_timeout"},
        "unknown_variant": {"source_variant_name": "unknown"},
        "replay_ordinal": {"replay_ordinal": 99},
        "session_id": {"session_id": "forged"},
        "shift_us": {"shift_us": layout.shift_us + 1000},
    }
    forged = replace(layout, **changes[field])
    with pytest.raises(InternalError, match="replay layout"):
        project_replay(ReplayProjectionRequest(program, plan, forged, sequence))


@pytest.mark.parametrize("field", ("replay_ordinal", "session_id", "shift_us"))
def test_replay_rejects_self_digesting_noncanonical_plan(projected_set, field):
    """公开 replay projector 不接受自摘要但不是 planner 输出的布局。"""
    program, plan, _projection, sequence, _noise, _replay = projected_set
    layout = plan.replay_layouts[0]
    changes = {
        "replay_ordinal": {"replay_ordinal": layout.replay_ordinal + 1},
        "session_id": {"session_id": "forged-session"},
        "shift_us": {"shift_us": layout.shift_us + 1000},
    }
    forged_layout = replace(layout, **changes[field])
    layouts = tuple(
        forged_layout if item == layout else item for item in plan.replay_layouts
    )
    forged_plan = replace(plan, replay_layouts=layouts, digest="")
    forged_plan = replace(forged_plan, digest=scenario_plan_digest(forged_plan))
    with pytest.raises(InternalError, match="canonical planner"):
        project_replay(ReplayProjectionRequest(
            program, forged_plan, forged_layout, sequence,
        ))


def test_mixed_512_mib_accounting_oracle_is_compact_and_rss_bounded():
    """用真实 canonical 行费用与虚拟 multiplicity 覆盖接近 512 MiB 的混合包络。"""
    rows = (
        {"payload": {"text": "main"}, "_meta": {"generation": {"variant": "positive"}}},
        {"payload": {"text": "主事件"}, "_meta": {"event": {"role": "request"}}},
        {"payload": {"text": "noise"}, "_meta": {"event": {"noise": True}}},
        {
            "payload": {"text": "replay"},
            "_meta": {"event": {"duplicate_of_event_id": "a" * 32}},
        },
    )
    costs = tuple(_independent_row_bytes(row) for row in rows)
    assert costs == tuple(len(canonical_delivery_row(row)) + 1 for row in rows)

    rounds, remainder = divmod(_RETAINED_CAP, sum(costs))
    counts = [rounds] * len(costs)
    smallest = min(range(len(costs)), key=costs.__getitem__)
    extra, _unused = divmod(remainder, costs[smallest])
    counts[smallest] += extra
    retained = sum(cost * count for cost, count in zip(costs, counts, strict=True))
    assert 0 <= _RETAINED_CAP - retained < min(costs)
    assert len(set(costs)) > 1 and all(count > 0 for count in counts)
    assert _peak_rss_bytes() < _RSS_LIMIT_BYTES


def test_five_hundred_thousand_compact_witnesses_fit_rss_gate():
    """独立进程驻留完整规模 compact witness，且 peak RSS 小于 4 GiB。"""
    script = """
import resource
import sys
from labelkit.common.contracts.generation import ProjectionWitness

items = []
for index in range(500000):
    first = f"{index:064x}"
    second = f"{index + 500000:064x}"
    third = f"{index + 1000000:064x}"
    items.append(ProjectionWitness(first[:32], first, second, (third,)))
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
rss_bytes = int(rss if sys.platform == "darwin" else rss * 1024)
print(len(items), rss_bytes)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    count, rss_bytes = (int(value) for value in result.stdout.split())
    assert count == 500000
    assert rss_bytes < _RSS_LIMIT_BYTES

"""CP-SAT ScenarioPlan 的独立离线枚举 oracle。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from itertools import permutations
from types import SimpleNamespace

import pytest
from ortools.sat.python import cp_model

import labelkit.operators.generation.planner as planner_module
from labelkit.common.config.generation import (
    CalendarWindowSpec,
    CounterfactualSetSpec,
    GapSpec,
    InterleavingPatternSpec,
    InterleavingSpec,
)
from labelkit.common.config._temporal import IntervalContainmentSpec
from labelkit.common.config.model import TimeBindingSpec
from labelkit.common.errors import ConfigError, InternalError
from labelkit.orchestration.sequence_workflow import _DeliveryController
from labelkit.operators.generation.planner import (
    _add_containment_constraints,
    _bounded_random,
    _plan_budget,
    _plan_internal,
    _require_program_quantum,
    compile_scenario_plan,
)
from labelkit.operators.generation.program import generation_program_digest
from labelkit.operators.generation.project import scenario_plan_digest


def _rehash(program):
    """为测试内协调修改后的程序重建权威摘要。"""
    return replace(program, digest=generation_program_digest(program))


def _visible_branches(plan):
    """按 DeliverySlot 与 variant 声明序读取可见 branch。"""
    output = []
    for slot in plan.delivery_slots:
        for variant in slot.variant_names or (None,):
            matches = [block[(slot.slot_key, variant)] for block in plan.blocks
                       if (slot.slot_key, variant) in block]
            assert len(matches) == 1
            output.append((slot, variant, matches[0]))
    return output


def _declared_case(program, pattern, variants, frames, duplicate: int = 0):
    """构造一个只保留单 scenario 的区间计划用例。"""
    source = replace(
        program.counterfactual_sets[0], count=1, variants=variants,
    )
    timeline = replace(
        program.timeline,
        noise_events=0,
        duplicate_sequences=duplicate,
    )
    return _rehash(replace(
        program,
        frame_classes=frames,
        patterns={pattern.name: pattern},
        counterfactual_sets=(source,),
        timeline=timeline,
        noise=None,
    ))


def _resource_interval_case(program, first_gap_us: int):
    """构造两个同 resource 正区间与一个点事件。"""
    base = program.patterns["booking_success"]
    frames = dict(program.frame_classes)
    frames["task_request"] = replace(
        frames["task_request"], duration_us=10_000_000, resources=("app", "shared"),
    )
    frames["acknowledgement"] = replace(
        frames["acknowledgement"], duration_us=5_000_000, resources=("shared",),
    )
    frames["confirmation"] = replace(
        frames["confirmation"], duration_us=0, resources=(),
    )
    gaps = (
        GapSpec("request_to_acknowledge", "request", "acknowledge",
                first_gap_us, first_gap_us),
        GapSpec("acknowledge_to_confirm", "acknowledge", "confirm",
                5_000_000, 5_000_000),
    )
    pattern = replace(base, gaps=gaps, max_span_us=20_000_000, containments=())
    positive = next(item for item in program.counterfactual_sets[0].variants
                    if item.kind == "positive")
    return _declared_case(program, pattern, (positive,), frames)


def _interleaving_case(
    program,
    trigger_count: int = 1,
    partner_count: int = 1,
    none_weight: int = 0,
    partner_first: bool = False,
):
    """构造一个 positive-only 且可强制交织的冻结 program。"""
    source = program.counterfactual_sets[0]
    positive = next(item for item in source.variants if item.kind == "positive")
    trigger = replace(
        source, name="trigger_source", count=trigger_count,
        interleaving_candidate_set="trigger", variants=(positive,),
    )
    partner = replace(
        source, name="partner_source", count=partner_count,
        interleaving_candidate_set="partner", variants=(positive,),
    )
    interleaving = InterleavingSpec(none_weight, (
        InterleavingPatternSpec("trigger_with_partner", "trigger", "partner", 1),
    ))
    sources = (partner, trigger) if partner_first else (trigger, partner)
    timeline = replace(program.timeline, noise_events=0, duplicate_sequences=0)
    return _rehash(replace(
        program, counterfactual_sets=sources, interleaving=interleaving,
        timeline=timeline, noise=None,
    ))


def _offset_pattern(program, name: str, prefix: str, offsets: tuple[int, ...]):
    """从真实声明克隆一个固定 logical offset 的测试 pattern。"""
    base = program.patterns["booking_success"]
    roles = tuple(
        replace(
            base.roles[index % len(base.roles)],
            name=f"{prefix}{index + 1}",
            calendar_window=None,
        )
        for index in range(len(offsets))
    )
    order = tuple(item.name for item in roles)
    gaps = tuple(
        GapSpec(
            f"{left}_to_{right}", left, right,
            offsets[index + 1] - offsets[index],
            offsets[index + 1] - offsets[index],
        )
        for index, (left, right) in enumerate(zip(order, order[1:]))
    )
    return replace(
        base, name=name, roles=roles, order=order,
        gaps=gaps, max_span_us=offsets[-1], containments=(),
    )


def _offset_interleaving_case(program, trigger_offsets, partner_offsets):
    """构造两个独立固定 offset pattern 的强制交织用例。"""
    trigger_pattern = _offset_pattern(program, "trigger_pattern", "a", trigger_offsets)
    partner_pattern = _offset_pattern(program, "partner_pattern", "b", partner_offsets)
    source = program.counterfactual_sets[0]
    positive = next(item for item in source.variants if item.kind == "positive")
    sources = (
        replace(
            source, name="trigger_source", pattern=trigger_pattern.name, count=1,
            interleaving_candidate_set="trigger", variants=(positive,),
        ),
        replace(
            source, name="partner_source", pattern=partner_pattern.name, count=1,
            interleaving_candidate_set="partner", variants=(positive,),
        ),
    )
    interleaving = InterleavingSpec(0, (
        InterleavingPatternSpec("forced", "trigger", "partner", 1),
    ))
    timeline = replace(
        program.timeline,
        session_max_events=len(trigger_offsets) + len(partner_offsets),
        session_max_span_us=max((*trigger_offsets, *partner_offsets, 0)) + 60_000_000,
        noise_events=0,
        duplicate_sequences=0,
    )
    return _rehash(replace(
        program, patterns={trigger_pattern.name: trigger_pattern,
                           partner_pattern.name: partner_pattern},
        counterfactual_sets=sources, interleaving=interleaving,
        timeline=timeline, calendar_windows={}, noise=None,
    ))


def _independent_violations(pattern, events):
    """不导入 production evaluator 地计算本教学模式的结构违规。"""
    positions = {event.role: index for index, event in enumerate(events)}
    times = {event.role: event.logical_time_us for event in events}
    violations = []
    for role in pattern.order:
        if role not in positions:
            violations.append({"kind": "missing_role", "target": role})
    for before, after in zip(pattern.order, pattern.order[1:]):
        if before in positions and after in positions and positions[before] > positions[after]:
            violations.append({"kind": "reordered", "before": before, "after": after})
    for gap in pattern.gaps:
        if gap.before not in times or gap.after not in times:
            continue
        if positions[gap.before] > positions[gap.after]:
            continue
        delta = times[gap.after] - times[gap.before]
        if delta < gap.min_gap_us:
            violations.append({"kind": "gap_below_min", "target": gap.name})
        elif delta > gap.max_gap_us:
            violations.append({"kind": "gap_above_max", "target": gap.name})
    if len(times) == len(pattern.order) and max(times.values()) - min(times.values()) > pattern.max_span_us:
        violations.append({"kind": "max_span_exceeded", "target": pattern.name})
    return violations


def _has_alternation(owners) -> bool:
    """判断时间序 owner 中是否存在 A-B-A 或 B-A-B witness。"""
    return any(left == right and left != middle
               for left, middle, right in zip(owners, owners[1:], owners[2:]))


def _prefer_first_trigger_gap(domain, material):
    """固定选择 trigger 的第一个 alternation witness。"""
    if domain != "interleaving_witness_rank":
        return 0
    owner, gap_index = material[-2:]
    if (owner, gap_index) == ("trigger", 0):
        return 0
    return 10 + (0 if owner == "trigger" else 100) + gap_index


def test_plan_is_deterministic(declared_program):
    plan_a = compile_scenario_plan(declared_program)
    plan_b = compile_scenario_plan(declared_program)
    assert plan_a == plan_b
    assert plan_a.digest == plan_b.digest


def test_bounded_random_rejects_limit_and_advances_only_counter(monkeypatch):
    upper = 10
    maximum = 1 << 256
    limit = maximum - maximum % upper
    values = iter((limit, 17))
    calls = []

    def fake_random(domain, material):
        calls.append((domain, material))
        return next(values)

    monkeypatch.setattr(planner_module, "generation_random", fake_random)

    assert _bounded_random("interleaving_pattern_choice", ("program", 3), upper) == 7
    assert calls == [
        ("interleaving_pattern_choice", ["program", 3, 0]),
        ("interleaving_pattern_choice", ["program", 3, 1]),
    ]


def test_partner_rejection_uses_exact_domain_material_and_counter(
        declared_program, monkeypatch):
    program = _interleaving_case(declared_program, partner_count=3)
    maximum = 1 << 256
    limit = maximum - maximum % 3
    calls = []

    def fake_random(domain, material):
        if domain == "interleaving_partner_choice":
            calls.append(list(material))
            return limit if len(calls) == 1 else 0
        return 0

    monkeypatch.setattr(planner_module, "generation_random", fake_random)

    plan = compile_scenario_plan(program)

    assert len(plan.interleaving_layouts) == 1
    assert calls == [
        [program.digest, program.planner_seed, "trigger_source/000000", "positive",
         "trigger_with_partner", "partner", 0],
        [program.digest, program.planner_seed, "trigger_source/000000", "positive",
         "trigger_with_partner", "partner", 1],
    ]


@pytest.mark.parametrize(
    ("ticket", "selected"),
    tuple((ticket, None if ticket < 9 else "trigger_with_partner") for ticket in range(10)),
)
def test_nine_to_one_pattern_ticket_boundaries(declared_program, monkeypatch, ticket, selected):
    program = _interleaving_case(declared_program, none_weight=9)
    trigger = SimpleNamespace(
        slot=SimpleNamespace(slot_key="trigger_source/000000"),
        variant_name="positive",
    )
    monkeypatch.setattr(planner_module, "generation_random", lambda _domain, _value: ticket)

    pattern = planner_module._choose_interleaving_pattern(
        program, trigger, program.interleaving.patterns,
    )

    assert (None if pattern is None else pattern.name) == selected


def test_none_weight_occurs_once_before_multiple_pattern_ranges(declared_program, monkeypatch):
    base = _interleaving_case(declared_program, none_weight=9)
    patterns = (
        InterleavingPatternSpec("first", "trigger", "partner", 1),
        InterleavingPatternSpec("second", "trigger", "partner", 2),
    )
    program = _rehash(replace(base, interleaving=InterleavingSpec(9, patterns)))
    trigger = SimpleNamespace(
        slot=SimpleNamespace(slot_key="trigger_source/000000"),
        variant_name="positive",
    )
    tickets = iter(range(12))
    uppers = []

    def bounded(_domain, _material, upper):
        uppers.append(upper)
        return next(tickets)

    monkeypatch.setattr(planner_module, "_bounded_random", bounded)
    observed = []
    for _ticket in range(12):
        choice = planner_module._choose_interleaving_pattern(program, trigger, patterns)
        observed.append(None if choice is None else choice.name)

    assert observed == [None] * 9 + ["first", "second", "second"]
    assert uppers == [12] * 12


def test_six_hundred_slot_plan_digest_is_runtime_capacity_independent(
        declared_config, declared_program):
    pattern = declared_program.patterns["booking_success"]
    pattern = replace(
        pattern,
        roles=tuple(replace(role, calendar_window=None) for role in pattern.roles),
    )
    source = declared_program.counterfactual_sets[0]
    positive = next(item for item in source.variants if item.kind == "positive")
    source = replace(source, count=600, variants=(positive,))
    timeline = replace(
        declared_program.timeline,
        session_gap_us=1000,
        noise_events=0,
        duplicate_sequences=0,
    )
    program = _rehash(replace(
        declared_program,
        evaluation_profile=declared_program.semantic_profile,
        patterns={pattern.name: pattern},
        counterfactual_sets=(source,),
        timeline=timeline,
        noise=None,
    ))

    digests = {}
    for capacity in (1, 600):
        profiles = dict(declared_config.llm_profiles)
        profile = profiles[program.semantic_profile]
        profiles[program.semantic_profile] = replace(
            profile, max_concurrency=capacity,
        )
        cfg = replace(declared_config, llm_profiles=profiles)
        plan = compile_scenario_plan(program)
        request = SimpleNamespace(program=program, plan=plan)
        services = SimpleNamespace(generation=SimpleNamespace(config=cfg))
        controller = _DeliveryController(request, services)

        assert controller._candidate_capacity("primary", 600) == capacity
        assert len(controller.request.plan.delivery_slots) == 600
        assert controller.request.plan.digest == scenario_plan_digest(controller.request.plan)
        digests[capacity] = controller.request.plan.digest

    assert digests[1] == digests[600]


def test_six_hundred_positive_branches_compile_three_hundred_selected_pairs(
        declared_program, monkeypatch):
    pattern = declared_program.patterns["booking_success"]
    pattern = replace(
        pattern,
        roles=tuple(replace(role, calendar_window=None) for role in pattern.roles),
    )
    base = _rehash(replace(
        declared_program, patterns={pattern.name: pattern},
    ))
    program = _interleaving_case(base, trigger_count=300, partner_count=300)
    timeline = replace(program.timeline, session_gap_us=1000)
    program = _rehash(replace(program, timeline=timeline))
    solve = planner_module._solve_interleaving_layout
    calls = 0

    def counted_solve(model, request):
        nonlocal calls
        calls += 1
        return solve(model, request)

    monkeypatch.setattr(planner_module, "_solve_interleaving_layout", counted_solve)

    plan = compile_scenario_plan(program)

    assert len(plan.delivery_slots) == 600
    assert len(plan.interleaving_layouts) == 300
    assert plan.interleaving_opportunities == 300
    assert dict(plan.interleaving_pattern_opportunities) == {"trigger_with_partner": 300}
    assert plan.primary_sessions == 300
    assert calls == 1200
    assert plan.digest == scenario_plan_digest(plan)
    assert plan.digest == "59e11af8d22ecdf195409d6f2e242c11ba7a5d8f23e0315b4ae2013b41de8a89"


def test_same_resource_half_open_adjacency_and_multi_resource_carrier(declared_program):
    program = _resource_interval_case(declared_program, 10_000_000)

    plan = compile_scenario_plan(program)
    events = _visible_branches(plan)[0][2]

    assert [(item.duration_us, item.resources) for item in events] == [
        (10_000_000, ("app", "shared")),
        (5_000_000, ("shared",)),
        (0, ()),
    ]
    assert events[0].timestamp_us + events[0].duration_us == events[1].timestamp_us


def test_same_resource_strict_overlap_is_plan_infeasible(declared_program):
    program = _resource_interval_case(declared_program, 9_000_000)

    with pytest.raises(ConfigError, match="generation_plan_infeasible"):
        compile_scenario_plan(program)


def test_interleaved_owners_use_millisecond_starts_and_shared_resource_no_overlap(
        declared_program):
    program = _resource_interval_case(declared_program, 10_000_000)
    frames = dict(program.frame_classes)
    frames["task_request"] = replace(
        frames["task_request"], duration_us=2_000_000, resources=("shared",),
    )
    frames["acknowledgement"] = replace(
        frames["acknowledgement"], duration_us=0, resources=(),
    )
    program = _rehash(replace(program, frame_classes=frames))
    program = _interleaving_case(program)

    plan = compile_scenario_plan(program)
    request_events = [
        events[0] for _slot, _variant, events in _visible_branches(plan)
    ]
    intervals = sorted(
        (item.timestamp_us, item.timestamp_us + item.duration_us)
        for item in request_events
    )

    assert len({item.session_id for item in request_events}) == 1
    assert all(item.timestamp_us % 1000 == 0 for item in request_events)
    assert intervals[0][1] <= intervals[1][0]


def _interleaved_calendar_case(program, duration_us: int):
    """构造两个 owner 只可占用相邻毫秒起点的 calendar。"""
    base = program.patterns["booking_success"]
    roles = tuple(
        replace(role, calendar_window="tight" if role.name == "request" else None)
        for role in base.roles
    )
    pattern = replace(base, roles=roles)
    frames = dict(program.frame_classes)
    frames["task_request"] = replace(
        frames["task_request"], duration_us=duration_us, resources=(),
    )
    start = int(datetime(2026, 8, 24, 9, tzinfo=timezone.utc).timestamp() * 1_000_000)
    window_start = 9 * 3_600_000_000
    window = CalendarWindowSpec(
        "tight", 0, ("mon",), ((window_start, window_start + 11_000),),
    )
    timeline = replace(
        program.timeline,
        timestamp_start_us=start,
        utc_offset_minutes=0,
        session_max_events=6,
        noise_events=0,
        duplicate_sequences=0,
    )
    frozen = _rehash(replace(
        program,
        patterns={pattern.name: pattern},
        frame_classes=frames,
        timeline=timeline,
        calendar_windows={"tight": window},
        noise=None,
    ))
    return _interleaving_case(frozen), start + 11_000


def test_interleaved_calendar_end_uses_full_interval_boundary(declared_program):
    program, window_end = _interleaved_calendar_case(declared_program, 10_000)

    plan = compile_scenario_plan(program)
    requests = [events[0] for _slot, _variant, events in _visible_branches(plan)]

    assert len({event.session_id for event in requests}) == 1
    assert max(event.timestamp_us + event.duration_us for event in requests) == window_end

    impossible, _window_end = _interleaved_calendar_case(declared_program, 12_000)
    with pytest.raises(ConfigError, match="generation_plan_infeasible"):
        compile_scenario_plan(impossible)


def test_interleaved_session_starts_at_full_previous_tail_plus_session_gap(declared_program):
    pattern = declared_program.patterns["booking_success"]
    pattern = replace(
        pattern,
        roles=tuple(replace(role, calendar_window=None) for role in pattern.roles),
    )
    frames = dict(declared_program.frame_classes)
    frames["task_request"] = replace(
        frames["task_request"], duration_us=120_000_000, resources=(),
    )
    source = declared_program.counterfactual_sets[0]
    positive = next(item for item in source.variants if item.kind == "positive")
    sources = (
        replace(source, name="first", count=1, variants=(positive,)),
        replace(
            source, name="trigger_source", count=1,
            interleaving_candidate_set="trigger", variants=(positive,),
        ),
        replace(
            source, name="partner_source", count=1,
            interleaving_candidate_set="partner", variants=(positive,),
        ),
    )
    timeline = replace(
        declared_program.timeline,
        session_max_events=6,
        session_gap_us=1_000_000,
        noise_events=0,
        duplicate_sequences=0,
    )
    interleaving = InterleavingSpec(0, (
        InterleavingPatternSpec("trigger_with_partner", "trigger", "partner", 1),
    ))
    program = _rehash(replace(
        declared_program,
        patterns={pattern.name: pattern},
        frame_classes=frames,
        counterfactual_sets=sources,
        interleaving=interleaving,
        timeline=timeline,
        noise=None,
    ))

    branches = {
        slot.source_name: events
        for slot, _variant, events in _visible_branches(compile_scenario_plan(program))
    }
    first_tail = max(
        event.timestamp_us + max(event.duration_us, 1000)
        for event in branches["first"]
    )
    interleaved_start = min(
        events[0].timestamp_us
        for name, events in branches.items()
        if name in {"trigger_source", "partner_source"}
    )

    assert interleaved_start == first_tail + timeline.session_gap_us


def _containment_case(program, variants, gap_us: int = 1000):
    """构造有精确 1ms 包含余量的 declared program。"""
    base = program.patterns["booking_success"]
    frames = dict(program.frame_classes)
    frames["task_request"] = replace(
        frames["task_request"], duration_us=10_000_000, resources=("app",),
    )
    frames["acknowledgement"] = replace(
        frames["acknowledgement"], duration_us=9_998_000, resources=("screen",),
    )
    frames["confirmation"] = replace(
        frames["confirmation"], duration_us=0, resources=(),
    )
    gaps = (
        GapSpec("request_to_acknowledge", "request", "acknowledge", gap_us, gap_us),
        GapSpec("acknowledge_to_confirm", "acknowledge", "confirm",
                9_999_000, 9_999_000),
    )
    pattern = replace(
        base,
        gaps=gaps,
        max_span_us=10_000_000,
        containments=(IntervalContainmentSpec("request", "acknowledge"),),
    )
    return _declared_case(program, pattern, variants, frames)


def test_strict_containment_accepts_exactly_one_millisecond_margin(declared_program):
    positive = next(item for item in declared_program.counterfactual_sets[0].variants
                    if item.kind == "positive")
    program = _containment_case(declared_program, (positive,))

    events = _visible_branches(compile_scenario_plan(program))[0][2]
    by_role = {item.role: item for item in events}

    container = by_role["request"]
    contained = by_role["acknowledge"]
    assert contained.timestamp_us + contained.duration_us + 1000 == (
        container.timestamp_us + container.duration_us
    )


def test_non_millisecond_containment_attempt_fails_before_layout(declared_program):
    positive = next(item for item in declared_program.counterfactual_sets[0].variants
                    if item.kind == "positive")
    program = _containment_case(declared_program, (positive,), 999)

    with pytest.raises(ConfigError, match="millisecond quantum"):
        compile_scenario_plan(program)


def test_containment_solver_rejects_999_microsecond_margin_after_quantum_preflight(
        declared_program):
    """999us 向下量化为零余量后，生产 containment helper 必须直接判不可行。"""
    positive = next(item for item in declared_program.counterfactual_sets[0].variants
                    if item.kind == "positive")
    program = _containment_case(declared_program, (positive,))
    _require_program_quantum(program)
    model = cp_model.CpModel()
    starts = {
        "request": model.new_constant(0),
        "acknowledge": model.new_constant(1),
    }
    margin_us = 999
    container_duration = 10
    contained_duration = container_duration - 1 - margin_us // 1000
    pattern = SimpleNamespace(
        containments=(IntervalContainmentSpec("request", "acknowledge"),),
    )

    _add_containment_constraints(
        model, pattern, starts,
        {"request": container_duration, "acknowledge": contained_duration},
        frozenset(starts),
    )

    assert cp_model.CpSolver().solve(model) == cp_model.INFEASIBLE


def test_missing_contained_is_legal_but_missing_container_is_infeasible(declared_program):
    variants = declared_program.counterfactual_sets[0].variants
    missing = next(item for item in variants if item.kind == "missing")
    legal = _containment_case(declared_program, (missing,))
    assert len(_visible_branches(compile_scenario_plan(legal))[0][2]) == 2

    missing_container = replace(
        missing,
        target={"role": "request"},
        expected_violation={"kind": "missing_role", "target": "request"},
        divergence_role="request",
    )
    illegal = _containment_case(declared_program, (missing_container,))
    with pytest.raises(ConfigError, match="generation_plan_infeasible"):
        compile_scenario_plan(illegal)


def test_reordered_branch_cannot_break_containment(declared_program):
    reordered = next(
        item for item in declared_program.counterfactual_sets[0].variants
        if item.kind == "reordered"
    )
    program = _containment_case(declared_program, (reordered,))

    with pytest.raises(ConfigError, match="generation_plan_infeasible"):
        compile_scenario_plan(program)


def test_interval_exceeded_branch_cannot_break_containment(declared_program):
    variants = declared_program.counterfactual_sets[0].variants
    exceeded = next(item for item in variants if item.kind == "interval_exceeded")
    exceeded = replace(
        exceeded,
        target={
            "gap": "request_to_acknowledge",
            "min_excess_us": 1000,
            "max_excess_us": 1000,
        },
        expected_violation={
            "kind": "gap_above_max",
            "target": "request_to_acknowledge",
        },
        divergence_role="acknowledge",
    )
    program = _containment_case(declared_program, (exceeded,))
    pattern = program.patterns["booking_success"]
    pattern = replace(pattern, max_span_us=11_000_000)
    program = _rehash(replace(program, patterns={pattern.name: pattern}))

    with pytest.raises(ConfigError, match="generation_plan_infeasible"):
        compile_scenario_plan(program)


def test_interval_envelope_not_last_start_enforces_pattern_span(declared_program):
    program = _resource_interval_case(declared_program, 10_000_000)
    pattern = program.patterns["booking_success"]
    too_short = replace(pattern, max_span_us=14_999_000)
    program = _rehash(replace(program, patterns={too_short.name: too_short}))

    with pytest.raises(ConfigError, match="generation_plan_infeasible"):
        compile_scenario_plan(program)


def test_session_span_uses_interval_envelope(declared_program):
    program = _resource_interval_case(declared_program, 10_000_000)
    timeline = replace(program.timeline, session_max_span_us=14_999_000)
    program = _rehash(replace(program, timeline=timeline))

    with pytest.raises(ConfigError, match="generation_plan_infeasible"):
        compile_scenario_plan(program)


def test_calendar_window_contains_the_full_half_open_interval(declared_program):
    program = _resource_interval_case(declared_program, 10_000_000)
    start = int(datetime(2026, 8, 24, 9, tzinfo=timezone.utc).timestamp() * 1_000_000)
    window = CalendarWindowSpec(
        "short", 0, ("mon",), ((9 * 3_600_000_000, 9 * 3_600_000_000 + 10_000_000),),
    )
    pattern = program.patterns["booking_success"]
    roles = tuple(
        replace(role, calendar_window="short" if role.name == "request" else None)
        for role in pattern.roles
    )
    pattern = replace(pattern, roles=roles)
    timeline = replace(program.timeline, timestamp_start_us=start)
    program = _rehash(replace(
        program,
        patterns={pattern.name: pattern},
        calendar_windows={"short": window},
        timeline=timeline,
    ))

    events = _visible_branches(compile_scenario_plan(program))[0][2]
    assert events[0].timestamp_us == start
    assert events[0].timestamp_us + events[0].duration_us == start + 10_000_000

    frames = dict(program.frame_classes)
    frames["task_request"] = replace(
        frames["task_request"], duration_us=10_001_000,
    )
    impossible = _rehash(replace(program, frame_classes=frames))
    with pytest.raises(ConfigError, match="generation_plan_infeasible"):
        compile_scenario_plan(impossible)


def test_replay_layout_is_one_constant_shift_and_preserves_intervals(declared_program):
    program = _resource_interval_case(declared_program, 10_000_000)
    source = program.counterfactual_sets[0]
    timeline = replace(program.timeline, duplicate_sequences=1)
    program = _rehash(replace(program, counterfactual_sets=(source,), timeline=timeline))

    plan = compile_scenario_plan(program)
    layout = plan.replay_layouts[0]
    source_events = _visible_branches(plan)[0][2]
    replay_starts = tuple(item.timestamp_us + layout.shift_us for item in source_events)

    assert layout.shift_us > 0 and layout.shift_us % 1000 == 0
    assert tuple(right - left for left, right in zip(replay_starts, replay_starts[1:])) == (
        tuple(right.timestamp_us - left.timestamp_us
              for left, right in zip(source_events, source_events[1:]))
    )
    assert min(replay_starts) >= max(
        item.timestamp_us + max(item.duration_us, 1000) for item in source_events
    )


def test_plan_preflight_rejects_mechanical_leaf_constraint(declared_program):
    frames = dict(declared_program.frame_classes)
    frame = frames["task_request"]
    schema = {
        "type": "object",
        "properties": {"timestamp": {"type": "integer", "maximum": 0}},
        "required": ["timestamp"],
        "additionalProperties": False,
    }
    frames["task_request"] = replace(
        frame,
        gen_schema=schema,
        model_gen_schema={
            "type": "object", "properties": {}, "required": [],
            "additionalProperties": False,
        },
        business_time_paths=("/timestamp",),
        time_bindings=(TimeBindingSpec(
            "/timestamp", "event_start_milliseconds", None,
        ),),
    )
    program = _rehash(replace(declared_program, frame_classes=frames))

    with pytest.raises(ConfigError, match="mechanical value violates"):
        compile_scenario_plan(program)


def test_plan_preflight_rejects_annotation_resource_leaf_constraint(declared_program):
    program = _resource_interval_case(declared_program, 10_000_000)
    frames = dict(program.frame_classes)
    frames["task_request"] = replace(
        frames["task_request"], resources=("foreground_app",),
    )
    views = dict(program.class_views)
    view = views["ticket_booking"]
    schema = {
        "type": "object",
        "properties": {
            "timestamp": {"type": "integer", "maximum": 0},
            "text": {"type": "string"},
        },
        "required": ["timestamp", "text"],
        "additionalProperties": False,
    }
    views["ticket_booking"] = replace(
        view,
        schema=schema,
        model_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        business_time_paths=("/timestamp",),
        time_bindings=(TimeBindingSpec(
            "/timestamp", "first_resource_start_milliseconds", "foreground_app",
        ),),
    )
    program = _rehash(replace(program, frame_classes=frames, class_views=views))

    with pytest.raises(ConfigError, match="mechanical value violates"):
        compile_scenario_plan(program)


def test_noise_topics_preserve_nonlexical_declaration_order(declared_program):
    """NoiseSlot ordinal 严格沿用声明序，不做排序或规范化。"""
    topics = ("z-topic", "a-topic")
    noise = replace(declared_program.noise, topics=topics)
    program = _rehash(replace(declared_program, noise=noise))

    plan = compile_scenario_plan(program)

    assert tuple(slot.ordinal for slot in plan.noise_slots) == (0, 1)
    assert tuple(slot.topic for slot in plan.noise_slots) == topics


def test_example_exact_layout_has_independent_primary_sessions(declared_program):
    plan = compile_scenario_plan(declared_program)
    branches = _visible_branches(plan)
    assert len(plan.delivery_slots) == 2
    assert len(branches) == 8
    assert plan.primary_sessions == 8
    assert len(plan.noise_slots) == 2
    assert len(plan.replay_layouts) == 1
    sessions = defaultdict(list)
    for slot, variant, events in branches:
        owner = (slot.slot_key, variant)
        for event in events:
            sessions[event.session_id].append((event.timestamp_us, owner))
    assert len(sessions) == 8
    assert all(len({owner for _, owner in rows}) == 1 for rows in sessions.values())
    assert len({timestamp for rows in sessions.values() for timestamp, _ in rows}) == 22
    for slot, variant, events in branches:
        assert [event.position for event in events] == list(range(len(events)))
        assert len({event.session_id for event in events}) == 1
        assert len(events) <= declared_program.timeline.session_max_events
        assert events[-1].timestamp_us - events[0].timestamp_us <= (
            declared_program.timeline.session_max_span_us
        )
        if variant == "missing_acknowledgement":
            assert len(events) == 2
        else:
            assert len(events) == 3
    assert {slot.session_id for slot in plan.noise_slots} == {"noise_000000"}
    assert tuple(slot.topic for slot in plan.noise_slots) == declared_program.noise.topics
    assert plan.noise_slots[-1].timestamp_us - plan.noise_slots[0].timestamp_us <= (
        declared_program.timeline.session_max_span_us
    )
    assert plan.replay_layouts[0].shift_us > 0
    assert plan.replay_layouts[0].shift_us % 1000 == 0


def test_interleaving_layout_has_true_owner_alternation(declared_program):
    program = _interleaving_case(declared_program)
    plan = compile_scenario_plan(program)
    sessions = defaultdict(list)
    for slot, variant, events in _visible_branches(plan):
        owner = (slot.slot_key, variant)
        for event in events:
            sessions[event.session_id].append((event.timestamp_us, owner))
    interleaved = [rows for rows in sessions.values() if len({owner for _, owner in rows}) == 2]
    assert len(interleaved) == 1
    ordered_owners = [owner for _, owner in sorted(interleaved[0])]
    assert _has_alternation(ordered_owners)
    assert plan.interleaving_opportunities == 1
    assert dict(plan.interleaving_pattern_opportunities) == {"trigger_with_partner": 1}
    assert plan.primary_sessions == 1


def test_interleaving_preserves_each_owner_logical_and_artifact_offsets(
        declared_program):
    second = 1_000_000
    expected = {
        "trigger_source": (0, 10 * second, 20 * second),
        "partner_source": (0, 2 * second, 12 * second),
    }
    program = _offset_interleaving_case(
        declared_program,
        expected["trigger_source"],
        expected["partner_source"],
    )

    for slot, _variant, events in _visible_branches(compile_scenario_plan(program)):
        logical = tuple(
            event.logical_time_us - events[0].logical_time_us for event in events
        )
        artifact = tuple(
            event.timestamp_us - events[0].timestamp_us for event in events
        )
        assert logical == expected[slot.source_name]
        assert artifact == expected[slot.source_name]


def test_interleaving_enforces_cross_owner_resource_no_overlap(
        declared_program, monkeypatch):
    program = _offset_interleaving_case(
        declared_program,
        (0, 10_000_000, 20_000_000),
        (0, 2_000_000, 12_000_000),
    )
    frames = {
        name: replace(frame, duration_us=0, resources=())
        for name, frame in program.frame_classes.items()
    }
    frames["task_request"] = replace(
        frames["task_request"], duration_us=2_000_000, resources=("shared",),
    )
    program = _rehash(replace(program, frame_classes=frames))
    monkeypatch.setattr(
        planner_module, "generation_random", _prefer_first_trigger_gap,
    )

    branches = {
        slot.source_name: events
        for slot, _variant, events in _visible_branches(compile_scenario_plan(program))
    }
    trigger = branches["trigger_source"][0]
    partner = branches["partner_source"][0]

    assert partner.timestamp_us - trigger.timestamp_us == 2_000_000
    assert trigger.timestamp_us + trigger.duration_us <= partner.timestamp_us


def test_interleaving_enforces_cross_owner_start_uniqueness(
        declared_program, monkeypatch):
    program = _offset_interleaving_case(
        declared_program,
        (0, 10_000_000, 20_000_000),
        (0, 9_999_000, 12_000_000),
    )
    frames = {
        name: replace(frame, duration_us=0, resources=())
        for name, frame in program.frame_classes.items()
    }
    program = _rehash(replace(program, frame_classes=frames))
    monkeypatch.setattr(
        planner_module, "generation_random", _prefer_first_trigger_gap,
    )

    branches = {
        slot.source_name: events
        for slot, _variant, events in _visible_branches(compile_scenario_plan(program))
    }
    timestamps = [
        event.timestamp_us
        for events in branches.values()
        for event in events
    ]

    assert len(timestamps) == len(set(timestamps))
    assert (
        branches["partner_source"][0].timestamp_us
        - branches["trigger_source"][0].timestamp_us
    ) == 2_000


def test_interleaving_combined_interval_envelope_obeys_session_cap(
        declared_program, monkeypatch):
    program = _offset_interleaving_case(
        declared_program, (0, 10_000_000), (0, 10_000_000),
    )
    frames = {
        name: replace(frame, duration_us=0, resources=())
        for name, frame in program.frame_classes.items()
    }
    timeline = replace(
        program.timeline,
        session_max_events=4,
        session_max_span_us=10_000_000,
    )
    program = _rehash(replace(
        program, frame_classes=frames, timeline=timeline,
    ))
    monkeypatch.setattr(
        planner_module, "generation_random", _prefer_first_trigger_gap,
    )

    with pytest.raises(ConfigError, match="generation_plan_infeasible"):
        compile_scenario_plan(program)


def test_interleaving_combined_event_count_obeys_session_cap(declared_program):
    program = _offset_interleaving_case(
        declared_program,
        (0, 10_000_000),
        (0, 2_000_000, 12_000_000),
    )
    frames = {
        name: replace(frame, duration_us=0, resources=())
        for name, frame in program.frame_classes.items()
    }
    timeline = replace(
        program.timeline,
        session_max_events=4,
        session_max_span_us=60_000_000,
    )
    program = _rehash(replace(
        program, frame_classes=frames, timeline=timeline,
    ))

    with pytest.raises(ConfigError, match="generation_plan_infeasible"):
        compile_scenario_plan(program)


@pytest.mark.parametrize("partner_first", (False, True))
def test_partner_may_precede_or_follow_trigger(declared_program, partner_first):
    program = _interleaving_case(declared_program, partner_first=partner_first)

    plan = compile_scenario_plan(program)
    layout = plan.interleaving_layouts[0]
    branches = {
        slot.source_name: events
        for slot, _variant, events in _visible_branches(plan)
    }

    assert layout.trigger_slot_key == "trigger_source/000000"
    assert layout.partner_slot_key == "partner_source/000000"
    assert branches["trigger_source"][0].session_id == branches["partner_source"][0].session_id


def test_nonadjacent_pair_is_placed_at_earlier_branch_position(declared_program):
    source = declared_program.counterfactual_sets[0]
    positive = next(item for item in source.variants if item.kind == "positive")
    sources = (
        replace(
            source, name="trigger_source", count=1,
            interleaving_candidate_set="trigger", variants=(positive,),
        ),
        replace(source, name="middle", count=1, variants=(positive,)),
        replace(
            source, name="partner_source", count=1,
            interleaving_candidate_set="partner", variants=(positive,),
        ),
    )
    interleaving = InterleavingSpec(0, (
        InterleavingPatternSpec("forced", "trigger", "partner", 1),
    ))
    timeline = replace(
        declared_program.timeline, noise_events=0, duplicate_sequences=0,
    )
    program = _rehash(replace(
        declared_program, counterfactual_sets=sources,
        interleaving=interleaving, timeline=timeline, noise=None,
    ))

    branches = {
        slot.source_name: events
        for slot, _variant, events in _visible_branches(compile_scenario_plan(program))
    }

    assert branches["trigger_source"][0].session_id == "primary_000000"
    assert branches["partner_source"][0].session_id == "primary_000000"
    assert branches["middle"][0].session_id == "primary_000001"


def test_only_positive_variants_enter_interleaving_candidates(declared_program):
    source = declared_program.counterfactual_sets[0]
    sources = (
        replace(
            source, name="trigger_source", count=1,
            interleaving_candidate_set="trigger",
        ),
        replace(
            source, name="partner_source", count=1,
            interleaving_candidate_set="partner",
        ),
    )
    interleaving = InterleavingSpec(0, (
        InterleavingPatternSpec("forced", "trigger", "partner", 1),
    ))
    timeline = replace(
        declared_program.timeline, noise_events=0, duplicate_sequences=0,
    )
    program = _rehash(replace(
        declared_program, counterfactual_sets=sources,
        interleaving=interleaving, timeline=timeline, noise=None,
    ))

    plan = compile_scenario_plan(program)
    layout = plan.interleaving_layouts[0]
    sessions = defaultdict(set)
    for slot, variant, events in _visible_branches(plan):
        sessions[events[0].session_id].add((slot.source_name, variant))

    assert layout.trigger_variant_name == "positive"
    assert layout.partner_variant_name == "positive"
    assert plan.primary_sessions == 7
    assert sum(len(owners) == 2 for owners in sessions.values()) == 1
    assert all(
        variant == "positive"
        for owners in sessions.values() if len(owners) == 2
        for _source, variant in owners
    )


def test_none_does_not_consume_partner_pool(declared_program, monkeypatch):
    program = _interleaving_case(
        declared_program, trigger_count=2, partner_count=1, none_weight=1,
    )

    def fixed_random(domain, material):
        if domain == "interleaving_pattern_choice":
            return 0 if material[2].endswith("000000") else 1
        return 0

    monkeypatch.setattr(planner_module, "generation_random", fixed_random)

    plan = compile_scenario_plan(program)

    assert plan.interleaving_opportunities == 2
    assert len(plan.interleaving_layouts) == 1
    assert plan.interleaving_layouts[0].trigger_slot_key == "trigger_source/000001"
    assert plan.interleaving_layouts[0].partner_slot_key == "partner_source/000000"


def test_multiple_patterns_share_one_partner_pool_without_replacement(
        declared_program, monkeypatch):
    program = _interleaving_case(declared_program, trigger_count=2, partner_count=2)
    patterns = (
        InterleavingPatternSpec("first", "trigger", "partner", 1),
        InterleavingPatternSpec("second", "trigger", "partner", 1),
    )
    program = _rehash(replace(program, interleaving=InterleavingSpec(0, patterns)))

    def fixed_random(domain, material):
        if domain == "interleaving_pattern_choice":
            return 0 if material[2].endswith("000000") else 1
        return 0

    monkeypatch.setattr(planner_module, "generation_random", fixed_random)

    plan = compile_scenario_plan(program)
    layouts = plan.interleaving_layouts

    assert [item.pattern_name for item in layouts] == ["first", "second"]
    assert len({item.partner_slot_key for item in layouts}) == 2
    assert dict(plan.interleaving_pattern_opportunities) == {"first": 2, "second": 2}


def test_exhausted_partner_pool_removes_later_opportunities(declared_program):
    program = _interleaving_case(
        declared_program, trigger_count=3, partner_count=1,
    )

    plan = compile_scenario_plan(program)

    assert len(plan.interleaving_layouts) == 1
    assert plan.interleaving_opportunities == 1
    assert dict(plan.interleaving_pattern_opportunities) == {"trigger_with_partner": 1}


def test_only_selected_pair_runs_interleaving_solver(declared_program, monkeypatch):
    program = _interleaving_case(
        declared_program, trigger_count=50, partner_count=1,
    )
    solve = planner_module._solve_interleaving_layout
    calls = 0

    def counted_solve(model, request):
        nonlocal calls
        calls += 1
        return solve(model, request)

    monkeypatch.setattr(planner_module, "_solve_interleaving_layout", counted_solve)

    plan = compile_scenario_plan(program)

    assert len(plan.interleaving_layouts) == 1
    assert calls == 4


def test_replay_source_remains_positive_declaration_order(declared_program):
    source = declared_program.counterfactual_sets[0]
    positive = next(item for item in source.variants if item.kind == "positive")
    sources = (
        replace(
            source, name="trigger_source", count=1,
            interleaving_candidate_set="trigger", variants=(positive,),
        ),
        replace(source, name="middle", count=1, variants=(positive,)),
        replace(
            source, name="partner_source", count=1,
            interleaving_candidate_set="partner", variants=(positive,),
        ),
    )
    interleaving = InterleavingSpec(0, (
        InterleavingPatternSpec("forced", "trigger", "partner", 1),
    ))
    timeline = replace(
        declared_program.timeline,
        noise_events=0,
        duplicate_sequences=2,
    )
    program = _rehash(replace(
        declared_program,
        counterfactual_sets=sources,
        interleaving=interleaving,
        timeline=timeline,
        noise=None,
    ))

    plan = compile_scenario_plan(program)

    assert plan.interleaving_layouts[0].trigger_slot_key == "trigger_source/000000"
    assert tuple(item.source_slot_key for item in plan.replay_layouts) == (
        "trigger_source/000000", "middle/000000",
    )


def test_seed_ranked_layout_realizes_a_b_b_a_b_a_a(declared_program, monkeypatch):
    second = 1_000_000
    program = _offset_interleaving_case(
        declared_program,
        (0, 10 * second, 20 * second, 30 * second),
        (0, 2 * second, 12 * second),
    )

    def ranked_random(domain, material):
        if domain != "interleaving_witness_rank":
            return 0
        owner, gap_index = material[-2:]
        if owner == "trigger" and gap_index == 0:
            return 0
        return 100 + (0 if owner == "trigger" else 10) + gap_index

    monkeypatch.setattr(planner_module, "generation_random", ranked_random)

    rows = []
    for slot, _variant, events in _visible_branches(compile_scenario_plan(program)):
        owner = "A" if slot.source_name == "trigger_source" else "B"
        rows.extend((event.timestamp_us, owner) for event in events)

    assert [owner for _timestamp, owner in sorted(rows)] == ["A", "B", "B", "A", "B", "A", "A"]


def test_witness_hash_collision_uses_owner_and_gap_total_order(declared_program, monkeypatch):
    second = 1_000_000
    program = _offset_interleaving_case(
        declared_program,
        (0, 10 * second, 20 * second, 30 * second),
        (0, 2 * second, 12 * second),
    )
    monkeypatch.setattr(planner_module, "generation_random", lambda _domain, _value: 0)

    rows = []
    for slot, _variant, events in _visible_branches(compile_scenario_plan(program)):
        owner = "A" if slot.source_name == "trigger_source" else "B"
        rows.extend((event.timestamp_us, owner) for event in events)

    assert [owner for _timestamp, owner in sorted(rows)] == ["A", "B", "B", "A", "B", "A", "A"]


def test_interleaving_plan_is_deterministic_and_digest_covers_pair_facts(declared_program):
    program = _interleaving_case(declared_program)

    plan_a = compile_scenario_plan(program)
    plan_b = compile_scenario_plan(program)
    tampered_opportunities = replace(
        plan_a, interleaving_opportunities=plan_a.interleaving_opportunities + 1,
    )
    layout = plan_a.interleaving_layouts[0]
    tampered_layout = replace(layout, partner_slot_key="forged/000000")
    tampered_pair = replace(plan_a, interleaving_layouts=(tampered_layout,))

    assert plan_a == plan_b
    assert plan_a.digest == scenario_plan_digest(plan_a)
    assert scenario_plan_digest(tampered_opportunities) != plan_a.digest
    assert scenario_plan_digest(tampered_pair) != plan_a.digest


def test_multiple_witness_fixture_changes_owner_word_across_frozen_seeds(declared_program):
    second = 1_000_000
    base = _offset_interleaving_case(
        declared_program,
        (0, 10 * second, 20 * second, 30 * second),
        (0, 2 * second, 12 * second),
    )
    words = set()
    for seed in range(8):
        program = _rehash(replace(base, planner_seed=seed))
        rows = []
        for slot, _variant, events in _visible_branches(compile_scenario_plan(program)):
            owner = "A" if slot.source_name == "trigger_source" else "B"
            rows.extend((event.timestamp_us, owner) for event in events)
        words.add(tuple(owner for _timestamp, owner in sorted(rows)))

    assert len(words) > 1


def test_single_event_can_be_wrapped_by_multi_event_partner(declared_program):
    second = 1_000_000
    program = _offset_interleaving_case(
        declared_program, (0,), (0, 10 * second, 20 * second),
    )

    rows = []
    for slot, _variant, events in _visible_branches(compile_scenario_plan(program)):
        owner = "A" if slot.source_name == "trigger_source" else "B"
        rows.extend((event.timestamp_us, owner) for event in events)
    owners = [owner for _timestamp, owner in sorted(rows)]

    assert _has_alternation(owners)
    assert owners.count("A") == 1


def test_two_single_event_branches_cannot_interleave(declared_program):
    program = _offset_interleaving_case(declared_program, (0,), (0,))

    with pytest.raises(ConfigError, match="cannot form a true owner interleave"):
        compile_scenario_plan(program)


@pytest.mark.parametrize("trigger_first", (False, True))
def test_pure_serial_owner_words_are_rejected(declared_program, trigger_first):
    second = 1_000_000
    program = _offset_interleaving_case(
        declared_program, (0, second), (0, second),
    )
    start = int(datetime(2026, 8, 24, 9, tzinfo=timezone.utc).timestamp() * 1_000_000)
    hour = 3_600_000_000
    windows = {
        "early": CalendarWindowSpec("early", 0, ("mon",), ((9 * hour, 9 * hour + 1000),)),
        "late": CalendarWindowSpec("late", 0, ("mon",), ((10 * hour, 10 * hour + 1000),)),
    }
    names = ("early", "late") if trigger_first else ("late", "early")
    patterns = dict(program.patterns)
    for pattern_name, window_name in zip(patterns, names):
        pattern = patterns[pattern_name]
        roles = (replace(pattern.roles[0], calendar_window=window_name), *pattern.roles[1:])
        patterns[pattern_name] = replace(pattern, roles=roles)
    timeline = replace(
        program.timeline, timestamp_start_us=start, utc_offset_minutes=0,
        session_max_span_us=2 * hour,
    )
    program = _rehash(replace(
        program, patterns=patterns, timeline=timeline, calendar_windows=windows,
    ))

    with pytest.raises(ConfigError, match="selected interleaving pair has no feasible layout"):
        compile_scenario_plan(program)


def test_same_slot_variants_never_share_or_interleave_session(declared_program):
    plan = compile_scenario_plan(declared_program)
    for slot in plan.delivery_slots:
        sessions = {}
        for owner_slot, variant, events in _visible_branches(plan):
            if owner_slot.slot_key == slot.slot_key:
                sessions[variant] = {event.session_id for event in events}
        assert len(sessions) == len(slot.variant_names)
        assert all(len(value) == 1 for value in sessions.values())
        flattened = [next(iter(value)) for value in sessions.values()]
        assert len(flattened) == len(set(flattened))


def test_catalog_row_index_is_declaration_order_and_seed_independent(declared_program):
    left_program = _rehash(replace(declared_program, planner_seed=1))
    right_program = _rehash(replace(declared_program, planner_seed=999))
    left = compile_scenario_plan(left_program)
    right = compile_scenario_plan(right_program)
    assert [slot.catalog_row_index for slot in left.delivery_slots] == [0, 1]
    assert [slot.catalog_row_index for slot in right.delivery_slots] == [0, 1]


def test_small_vocabulary_oracle_matches_all_four_variants(declared_program):
    plan = compile_scenario_plan(declared_program)
    pattern = declared_program.patterns["booking_success"]
    variants = {item.name: item for item in declared_program.counterfactual_sets[0].variants}
    for _slot, name, events in _visible_branches(plan):
        assert _independent_violations(pattern, events) == (
            [] if not variants[name].expected_violation
            else [dict(variants[name].expected_violation)]
        )
    vocabulary = tuple(pattern.order)
    all_orders = {order for size in (2, 3) for order in permutations(vocabulary, size)}
    planned_orders = {tuple(event.role for event in events)
                      for _slot, _name, events in _visible_branches(plan)}
    assert planned_orders <= all_orders
    assert planned_orders == {
        ("request", "acknowledge", "confirm"),
        ("request", "confirm"),
        ("request", "confirm", "acknowledge"),
    }


def test_reordered_variant_with_impossible_non_target_gap_fails_planning(declared_program):
    base = declared_program.patterns["booking_success"]
    second = 1_000_000
    pattern = replace(
        base,
        gaps=(
            GapSpec("request_to_acknowledge", "request", "acknowledge", 5 * second, 5 * second),
            GapSpec("acknowledge_to_confirm", "acknowledge", "confirm", 5 * second, 5 * second),
        ),
        max_span_us=20 * second,
    )
    source = declared_program.counterfactual_sets[0]
    reordered = next(item for item in source.variants if item.kind == "reordered")
    source = replace(source, count=1, variants=(reordered,))
    timeline = replace(
        declared_program.timeline,
        noise_events=0,
        duplicate_sequences=0,
    )
    program = _rehash(replace(
        declared_program,
        patterns={pattern.name: pattern},
        counterfactual_sets=(source,),
        timeline=timeline,
    ))
    with pytest.raises(ConfigError, match="generation_plan_infeasible"):
        compile_scenario_plan(program)


def test_hidden_baseline_without_positive_has_independent_calendar_layout(declared_program):
    base = declared_program.patterns["booking_success"]
    hour = 3_600_000_000
    windows = {
        "morning": CalendarWindowSpec("morning", 480, ("mon",), ((9 * hour, 10 * hour),)),
        "afternoon": CalendarWindowSpec(
            "afternoon", 480, ("mon",), ((13 * hour, 14 * hour),),
        ),
    }
    roles = tuple(
        replace(role, calendar_window=("morning", "afternoon", None)[index])
        for index, role in enumerate(base.roles)
    )
    pattern = replace(
        base,
        roles=roles,
        gaps=(
            GapSpec("request_to_acknowledge", "request", "acknowledge", 4 * hour, 4 * hour),
            GapSpec("acknowledge_to_confirm", "acknowledge", "confirm", 1_000_000, 1_000_000),
        ),
        max_span_us=5 * hour,
    )
    source = declared_program.counterfactual_sets[0]
    missing = next(item for item in source.variants if item.kind == "missing")
    missing = replace(
        missing,
        target={"role": "request"},
        expected_violation={"kind": "missing_role", "target": "request"},
        divergence_role="request",
    )
    source = replace(source, count=1, variants=(missing,))
    timeline = replace(
        declared_program.timeline,
        noise_events=0,
        duplicate_sequences=0,
    )
    program = _rehash(replace(
        declared_program,
        patterns={pattern.name: pattern},
        counterfactual_sets=(source,),
        timeline=timeline,
        calendar_windows=windows,
    ))
    plan = compile_scenario_plan(program)
    slot = plan.delivery_slots[0]
    block = next(item for item in plan.blocks if (slot.slot_key, None) in item)
    hidden = block[(slot.slot_key, None)]
    visible = block[(slot.slot_key, missing.name)]
    start = program.timeline.timestamp_start_us
    assert [item.timestamp_us for item in hidden] == [start, start + 4 * hour, start + 4 * hour + 1_000_000]
    assert visible[0].timestamp_us == start + 4 * hour


def test_positive_branch_reuses_hidden_baseline_values(declared_program):
    plan = compile_scenario_plan(declared_program)
    for slot in plan.delivery_slots:
        block = next(item for item in plan.blocks if (slot.slot_key, None) in item)
        assert block[(slot.slot_key, None)] == block[(slot.slot_key, "positive")]


def test_counterfactual_logical_times_are_exact_mechanical_transforms(declared_program):
    plan = compile_scenario_plan(declared_program)
    slot = plan.delivery_slots[0]
    pattern = declared_program.patterns[slot.pattern_name]
    variants = {
        item.kind: item for item in declared_program.counterfactual_sets[0].variants
    }

    def branch(name):
        return next(block[(slot.slot_key, name)] for block in plan.blocks
                    if (slot.slot_key, name) in block)

    baseline = {event.role: event.logical_time_us for event in branch(None)}
    missing = {event.role: event.logical_time_us for event in branch(variants["missing"].name)}
    missing_target = variants["missing"].target["role"]
    assert missing == {role: value for role, value in baseline.items()
                       if role != missing_target}

    reordered = {
        event.role: event.logical_time_us for event in branch(variants["reordered"].name)
    }
    before = variants["reordered"].target["before"]
    after = variants["reordered"].target["after"]
    expected = dict(baseline)
    expected[before], expected[after] = expected[after], expected[before]
    assert reordered == expected

    exceeded_spec = variants["interval_exceeded"]
    exceeded = {
        event.role: event.logical_time_us for event in branch(exceeded_spec.name)
    }
    target = next(gap for gap in pattern.gaps if gap.name == exceeded_spec.target["gap"])
    after_index = pattern.order.index(target.after)
    shifts = {exceeded[role] - baseline[role] for role in pattern.order[after_index:]}
    assert len(shifts) == 1 and next(iter(shifts)) > 0
    assert all(exceeded[role] == baseline[role] for role in pattern.order[:after_index])
    target_gap = exceeded[target.after] - exceeded[target.before]
    assert target.max_gap_us + exceeded_spec.target["min_excess_us"] <= target_gap
    assert target_gap <= target.max_gap_us + exceeded_spec.target["max_excess_us"]


def test_instruction_only_freezes_positions_without_declared_truth(instruction_program):
    plan = compile_scenario_plan(instruction_program)
    assert len(plan.delivery_slots) == 1
    slot = plan.delivery_slots[0]
    events = next(block[(slot.slot_key, None)] for block in plan.blocks
                  if (slot.slot_key, None) in block)
    assert slot.pattern_name is None
    assert slot.variant_names == ()
    assert 3 <= len(events) <= 4
    assert [event.role for event in events] == [
        f"position_{index:03d}" for index in range(len(events))
    ]


def test_selected_infeasible_partner_is_not_rematched(declared_program, monkeypatch):
    base = declared_program.patterns["booking_success"]
    windows = {
        "morning": CalendarWindowSpec(
            "morning", 480, ("mon", "tue", "wed", "thu", "fri"),
            ((8 * 3_600_000_000, 9 * 3_600_000_000),),
        ),
        "afternoon": CalendarWindowSpec(
            "afternoon", 480, ("mon", "tue", "wed", "thu", "fri"),
            ((13 * 3_600_000_000, 14 * 3_600_000_000),),
        ),
    }

    def make_pattern(name: str, window: str):
        roles = tuple(replace(role, calendar_window=window if index == 0 else None)
                      for index, role in enumerate(base.roles))
        return replace(base, name=name, roles=roles)

    patterns = {name: make_pattern(name, window) for name, window in (
        ("afternoon_trigger", "afternoon"),
        ("morning_partner", "morning"),
        ("afternoon_partner", "afternoon"),
    )}
    positive = declared_program.counterfactual_sets[0].variants[:1]
    sources = (
        CounterfactualSetSpec(
            "trigger_source", "afternoon_trigger", 1, "trigger", positive,
        ),
        CounterfactualSetSpec(
            "bad_partner", "morning_partner", 1, "partner", positive,
        ),
        CounterfactualSetSpec(
            "good_partner", "afternoon_partner", 1, "partner", positive,
        ),
    )
    timeline = replace(
        declared_program.timeline,
        noise_events=0,
        duplicate_sequences=0,
    )
    interleaving = InterleavingSpec(0, (
        InterleavingPatternSpec("forced", "trigger", "partner", 1),
    ))
    program = _rehash(replace(
        declared_program,
        planner_seed=17,
        patterns=patterns,
        counterfactual_sets=sources,
        interleaving=interleaving,
        timeline=timeline,
        calendar_windows=windows,
        noise=None,
    ))
    calls = []

    def fixed_random(domain, material):
        calls.append((domain, tuple(material)))
        return 0

    monkeypatch.setattr("labelkit.operators.generation.planner.generation_random", fixed_random)
    with pytest.raises(ConfigError, match="selected interleaving pair"):
        compile_scenario_plan(program)
    partner_calls = [item for item in calls if item[0] == "interleaving_partner_choice"]
    assert len(partner_calls) == 1


def test_standalone_impossible_calendar_fails_without_retry_loop(declared_program):
    base = declared_program.patterns["booking_success"]
    roles = tuple(replace(role, calendar_window="never" if index == 0 else None)
                  for index, role in enumerate(base.roles))
    pattern = replace(base, roles=roles)
    source = replace(
        declared_program.counterfactual_sets[0],
        count=1,
        variants=declared_program.counterfactual_sets[0].variants[:1],
    )
    timeline = replace(
        declared_program.timeline,
        noise_events=0,
        duplicate_sequences=0,
    )
    program = _rehash(replace(
        declared_program,
        planner_seed=9,
        patterns={pattern.name: pattern},
        counterfactual_sets=(source,),
        timeline=timeline,
        calendar_windows={"never": CalendarWindowSpec("never", 480, (), ())},
    ))
    with pytest.raises(ConfigError, match="generation_plan_infeasible"):
        compile_scenario_plan(program)


def test_planner_rejects_timestamp_beyond_datetime_range(declared_program):
    """计划工件时间超出 ISO8601 可表达范围时必须在内容调用前拒绝。"""
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = datetime.max.replace(tzinfo=timezone.utc) - epoch
    start_us = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    start_us -= start_us % 1000
    timeline = replace(declared_program.timeline, timestamp_start_us=start_us)
    program = _rehash(replace(declared_program, timeline=timeline))
    with pytest.raises(ConfigError, match="supported datetime range"):
        compile_scenario_plan(program)


def test_planner_maps_tampered_empty_missing_branch_to_internal_error(declared_program):
    """绕过 M1 的非法空 missing branch 也不得泄漏 IndexError。"""
    pattern = next(iter(declared_program.patterns.values()))
    role = pattern.roles[0]
    single = replace(pattern, roles=(role,), order=(role.name,), gaps=())
    source = declared_program.counterfactual_sets[0]
    missing = next(variant for variant in source.variants if variant.kind == "missing")
    missing = replace(
        missing,
        target={"role": role.name},
        expected_violation={"kind": "missing_role", "target": role.name},
        divergence_role=role.name,
    )
    timeline = replace(
        declared_program.timeline,
        noise_events=0,
        duplicate_sequences=0,
    )
    program = _rehash(replace(
        declared_program,
        patterns={single.name: single},
        counterfactual_sets=(replace(source, count=1, variants=(missing,)),),
        timeline=timeline,
        noise=None,
    ))
    with pytest.raises(InternalError, match="missing variant produced an empty branch"):
        compile_scenario_plan(program)


def test_planner_internal_failure_vocabulary_is_stable():
    with pytest.raises(InternalError, match="generation_plan_budget: exhausted"):
        _plan_budget("exhausted")
    with pytest.raises(InternalError, match="generation_plan_internal: invalid witness"):
        _plan_internal("invalid witness")

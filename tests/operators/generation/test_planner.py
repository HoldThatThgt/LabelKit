"""CP-SAT ScenarioPlan 的独立离线枚举 oracle。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from itertools import permutations

import pytest

from labelkit.common.config.generation import (
    CalendarWindowSpec,
    CounterfactualSetSpec,
    GapSpec,
)
from labelkit.common.errors import ConfigError, InternalError
from labelkit.operators.generation.planner import (
    _plan_budget,
    _plan_internal,
    compile_scenario_plan,
)
from labelkit.operators.generation.program import generation_program_digest


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


def test_plan_is_deterministic(declared_program):
    plan_a = compile_scenario_plan(declared_program)
    plan_b = compile_scenario_plan(declared_program)
    assert plan_a == plan_b
    assert plan_a.digest == plan_b.digest


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
    assert len(plan.replay_layouts[0].timestamps_us) == 3


def test_crossing_layout_has_true_owner_alternation(declared_program):
    timeline = replace(
        declared_program.timeline,
        primary_sessions=7,
        crossed_primary_sessions=1,
    )
    program = _rehash(replace(declared_program, timeline=timeline))
    sessions = defaultdict(list)
    for slot, variant, events in _visible_branches(compile_scenario_plan(program)):
        owner = (slot.slot_key, variant)
        for event in events:
            sessions[event.session_id].append((event.timestamp_us, owner))
    crossed = [rows for rows in sessions.values() if len({owner for _, owner in rows}) == 2]
    assert len(crossed) == 1
    ordered_owners = [owner for _, owner in sorted(crossed[0])]
    assert _has_alternation(ordered_owners)


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
        primary_sessions=1,
        crossed_primary_sessions=0,
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
        primary_sessions=1,
        crossed_primary_sessions=0,
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


def test_infeasible_first_crossing_selection_tries_later_choice(declared_program):
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
        ("morning_pattern", "morning"),
        ("afternoon_left", "afternoon"),
        ("afternoon_right", "afternoon"),
    )}
    positive = declared_program.counterfactual_sets[0].variants[:1]
    sources = tuple(CounterfactualSetSpec(f"source_{index}", name, 1, positive)
                    for index, name in enumerate(patterns))
    timeline = replace(
        declared_program.timeline,
        primary_sessions=2,
        crossed_primary_sessions=1,
        noise_events=0,
        duplicate_sequences=0,
    )
    program = _rehash(replace(
        declared_program,
        planner_seed=17,
        patterns=patterns,
        counterfactual_sets=sources,
        timeline=timeline,
        calendar_windows=windows,
    ))
    plan = compile_scenario_plan(program)
    sessions = defaultdict(set)
    for slot, variant, events in _visible_branches(plan):
        sessions[events[0].session_id].add((slot.slot_key, variant))
    crossed = next(owners for owners in sessions.values() if len(owners) == 2)
    assert {owner[0] for owner in crossed} == {"source_1/000000", "source_2/000000"}


def test_zero_crossing_impossible_calendar_fails_without_retry_loop(declared_program):
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
        primary_sessions=1,
        crossed_primary_sessions=0,
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
        primary_sessions=1,
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

"""v1.18 确定性 CP-SAT ScenarioPlan 编译器。"""
from __future__ import annotations

import dataclasses
import hashlib
import heapq
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

from ortools.sat.python import cp_model

from labelkit.common.contracts.generation import (
    DeliverySlot,
    GenerationProgram,
    NoiseSlot,
    PlannedEvent,
    ReplayLayout,
    ScenarioPlan,
)
from labelkit.common.errors import ConfigError, InternalError
from labelkit.operators.generation.project import (
    canonical_json,
    derive_generation_id,
    scenario_plan_digest,
)


_log = logging.getLogger("labelkit.generation.planner")
_DAY_US = 86_400_000_000
_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)
_WEEKDAY = {"mon": 0, "tue": 1, "wed": 2, "thu": 3,
            "fri": 4, "sat": 5, "sun": 6}


@dataclass(frozen=True)
class _LogicalBranch:
    """一个尚未放入工件时间线的 branch。"""

    slot: DeliverySlot  # owning 交付槽。
    variant_name: str | None  # branch 名；hidden/instruction-only 为 None。
    roles: tuple[str, ...]  # 按实际发生顺序排列的 role/position 名。
    logical_times: tuple[int, ...]  # 与 roles 对位的逻辑微秒。


@dataclass(frozen=True)
class _PlacedBranch:
    """一个完成 session 与工件时间投影的 primary branch。"""

    logical: _LogicalBranch  # 原始逻辑 branch。
    events: tuple[PlannedEvent, ...]  # 完整工件事件计划。
    session_index: int  # primary session 序号。


@dataclass(frozen=True)
class _CrossRequest:
    """一个待真正交织的双 owner session。"""

    program: GenerationProgram  # 冻结生成程序。
    left: _LogicalBranch  # 声明序左 branch。
    right: _LogicalBranch  # 声明序右 branch。
    lower: int  # 该 session 全部事件的下界。
    seed: int  # 交织 CP-SAT 随机种子。


class _PrimaryLayoutInfeasible(Exception):
    """当前 crossing 选择的任一绝对 primary 布局不可行。"""


class _CrossingChoicesExhausted(Exception):
    """全部满足精确 crossing 数量的选择都已被 no-good 排除。"""


def compile_scenario_plan(program: GenerationProgram) -> ScenarioPlan:
    """求解并返回唯一可接受的 OPTIMAL 确定性计划。

    @param program 冻结生成程序。
    @return 完整冻结的场景计划。
    """
    from labelkit.operators.generation.program import generation_program_digest

    if program.digest != generation_program_digest(program):
        _plan_internal("generation program digest is invalid")
    seed = program.planner_seed
    slots = _delivery_slots(program)
    logical, baselines = _logical_branches(program, slots, seed)
    placed = _select_primary_layout(program, logical, seed)
    blocks = _allocate_blocks(program, placed, baselines)
    cursor = max((event.timestamp_us for item in placed for event in item.events),
                 default=program.timeline.timestamp_start_us - 1)
    noise_slots, cursor = _noise_slots(program, cursor)
    replay_layouts = _replay_layouts(program, placed, cursor)
    _validate_timestamp_range(program, placed, noise_slots, replay_layouts)
    base = ScenarioPlan(
        blocks=blocks,
        delivery_slots=slots,
        noise_slots=noise_slots,
        replay_layouts=replay_layouts,
        primary_sessions=program.timeline.primary_sessions,
        digest="",
    )
    return dataclasses.replace(base, digest=scenario_plan_digest(base))


def _delivery_slots(program: GenerationProgram) -> tuple[DeliverySlot, ...]:
    """按声明序冻结全部 DeliverySlot 与 catalog row。

    @param program 冻结生成程序。
    @return 精确交付槽。
    """
    if program.mode == "instruction_only":
        return _instruction_slots(program)
    catalog_indexes: dict[str, int] = {}
    slots: list[DeliverySlot] = []
    for source in program.counterfactual_sets:
        pattern = program.patterns[source.pattern]
        for scenario_index in range(source.count):
            catalog = _catalog_index(program, pattern.sequence_class, catalog_indexes)
            slots.append(DeliverySlot(
                slot_key=f"{source.name}/{scenario_index:06d}",
                source_name=source.name,
                scenario_index=scenario_index,
                sequence_class=pattern.sequence_class,
                pattern_name=pattern.name,
                variant_names=tuple(variant.name for variant in source.variants),
                catalog_row_index=catalog,
            ))
    return tuple(slots)


def _instruction_slots(program: GenerationProgram) -> tuple[DeliverySlot, ...]:
    """冻结 instruction-only 交付槽。

    @param program 冻结生成程序。
    @return instruction-only 槽。
    """
    return tuple(
        DeliverySlot(
            slot_key=f"{source.name}/{index:06d}",
            source_name=source.name,
            scenario_index=index,
            sequence_class=source.sequence_class,
            pattern_name=None,
            variant_names=(),
            catalog_row_index=None,
        )
        for source in program.instruction_only
        for index in range(source.count)
    )


def _catalog_index(program, class_name: str, indexes: dict[str, int]) -> int | None:
    """为一个 declared slot 分配稳定 catalog row index。

    @param program 冻结生成程序。
    @param class_name sequence class 名。
    @param indexes 各类已分配计数。
    @return catalog row index 或 None。
    """
    class_config = program.class_views[class_name].sequence_generation
    if class_config.initial_state_source != "catalog":
        return None
    index = indexes.get(class_name, 0)
    indexes[class_name] = index + 1
    return index


def _logical_branches(program, slots, seed: int):
    """求解全部 baseline 与 delivery branch 逻辑时间。

    @param program 冻结生成程序。
    @param slots 交付槽。
    @param seed 运行随机种子。
    @return delivery branches 与按 slot 索引的 baseline。
    """
    branches: list[_LogicalBranch] = []
    baselines: dict[str, _LogicalBranch] = {}
    for slot_index, slot in enumerate(slots):
        if program.mode == "instruction_only":
            branch = _instruction_branch(program, slot, seed, slot_index)
            branches.append(branch)
            baselines[slot.slot_key] = branch
            continue
        pattern = program.patterns[slot.pattern_name]
        source = _counterfactual_source(program, slot.source_name)
        times = _solve_pattern_times(
            pattern, source.variants, _solver_seed(seed, f"baseline:{slot.slot_key}"),
        )
        baseline = _LogicalBranch(slot, None, pattern.order, times)
        baselines[slot.slot_key] = baseline
        for variant_index, variant in enumerate(source.variants):
            branches.append(_variant_branch(
                baseline, pattern, variant, seed, variant_index,
            ))
    return tuple(branches), baselines


def _instruction_branch(program, slot, seed: int, slot_index: int) -> _LogicalBranch:
    """用 CP-SAT 冻结 instruction-only length 与位置时间。

    @param program 冻结生成程序。
    @param slot 当前交付槽。
    @param seed 运行随机种子。
    @param slot_index 槽声明序号。
    @return instruction-only 逻辑 branch。
    """
    source = next(item for item in program.instruction_only if item.name == slot.source_name)
    model = cp_model.CpModel()
    length = model.new_int_var(source.len_range[0], source.len_range[1], "length")
    model.minimize(length)
    solver = _solve(model, _solver_seed(seed, f"instruction:{slot_index}"), slot.slot_key)
    count = solver.value(length)
    gap = program.timeline.event_gap_us[0]
    times = tuple(position * gap for position in range(count))
    roles = tuple(f"position_{position:03d}" for position in range(count))
    return _LogicalBranch(slot, None, roles, times)


def _solve_pattern_times(pattern, variants, solver_seed: int) -> tuple[int, ...]:
    """求解 baseline role 时间与全部 gap/span 约束。

    @param pattern 当前精确 pattern。
    @param variants 当前 set 的完整变体声明。
    @param solver_seed 确定性 solver seed。
    @return 与 pattern.order 对位的逻辑微秒。
    """
    model = cp_model.CpModel()
    times = [model.new_int_var(0, pattern.max_span_us, f"time_{index}")
             for index in range(len(pattern.order))]
    model.add(times[0] == 0)
    role_indexes = {role: index for index, role in enumerate(pattern.order)}
    for left, right in zip(times, times[1:]):
        model.add(left < right)
    for gap in pattern.gaps:
        delta = times[role_indexes[gap.after]] - times[role_indexes[gap.before]]
        model.add(delta >= gap.min_gap_us)
        model.add(delta <= gap.max_gap_us)
    _add_reordered_constraints(model, pattern, times, variants)
    model.add(times[-1] - times[0] <= pattern.max_span_us)
    model.minimize(sum(times))
    solver = _solve(model, solver_seed, pattern.name)
    return tuple(solver.value(value) for value in times)


def _add_reordered_constraints(model, pattern, times, variants) -> None:
    """把每个错序分支的全部非目标结构约束并入 baseline 模型。

    @param model 当前 baseline CP-SAT 模型。
    @param pattern 当前精确 pattern。
    @param times 与 pattern.order 对位的 baseline 时间变量。
    @param variants 当前 set 的完整变体声明。
    """
    indexes = {role: index for index, role in enumerate(pattern.order)}
    for variant in variants:
        if variant.kind != "reordered":
            continue
        before, after = variant.target["before"], variant.target["after"]
        branch_times = {role: times[index] for role, index in indexes.items()}
        branch_times[before], branch_times[after] = branch_times[after], branch_times[before]
        for left, right in zip(pattern.order, pattern.order[1:]):
            if (left, right) != (before, after):
                model.add(branch_times[left] < branch_times[right])
        for gap in pattern.gaps:
            if (gap.before, gap.after) == (before, after):
                continue
            delta = branch_times[gap.after] - branch_times[gap.before]
            model.add(delta >= gap.min_gap_us)
            model.add(delta <= gap.max_gap_us)


def _variant_branch(baseline, pattern, variant, seed: int, ordinal: int) -> _LogicalBranch:
    """机械派生一个 positive/counterfactual branch。

    @param baseline 当前 slot baseline。
    @param pattern 当前 pattern。
    @param variant 当前变体声明。
    @param seed 运行随机种子。
    @param ordinal 变体声明序号。
    @return 派生逻辑 branch。
    """
    roles = list(baseline.roles)
    times = list(baseline.logical_times)
    if variant.kind == "missing":
        index = roles.index(variant.target["role"])
        roles.pop(index)
        times.pop(index)
        if not roles:
            _plan_internal("missing variant produced an empty branch")
    elif variant.kind == "reordered":
        before = roles.index(variant.target["before"])
        after = roles.index(variant.target["after"])
        roles[before], roles[after] = roles[after], roles[before]
    elif variant.kind == "interval_exceeded":
        times = _exceeded_times(
            pattern, baseline.logical_times, variant,
            _solver_seed(seed, f"variant:{baseline.slot.slot_key}:{ordinal}"),
        )
    return _LogicalBranch(baseline.slot, variant.name, tuple(roles), tuple(times))


def _exceeded_times(pattern, baseline_times, variant, solver_seed: int) -> list[int]:
    """求解 interval-exceeded 的唯一 suffix 平移量。

    @param pattern 当前 pattern。
    @param baseline_times baseline 逻辑时间。
    @param variant interval-exceeded 变体。
    @param solver_seed 确定性 solver seed。
    @return 平移后的逻辑时间。
    """
    target = next(gap for gap in pattern.gaps if gap.name == variant.target["gap"])
    indexes = {role: index for index, role in enumerate(pattern.order)}
    after_index = indexes[target.after]
    low = target.max_gap_us + int(variant.target["min_excess_us"])
    high = target.max_gap_us + int(variant.target["max_excess_us"])
    base_gap = baseline_times[after_index] - baseline_times[indexes[target.before]]
    model = cp_model.CpModel()
    shift = model.new_int_var(max(0, low - base_gap), high - base_gap, "suffix_shift")
    for gap in pattern.gaps:
        if gap.name == target.name:
            continue
        delta = _shifted_gap(pattern, baseline_times, gap, after_index, shift)
        model.add(delta >= gap.min_gap_us)
        model.add(delta <= gap.max_gap_us)
    model.add(baseline_times[-1] + shift <= pattern.max_span_us)
    model.minimize(shift)
    solver = _solve(model, solver_seed, variant.name)
    amount = solver.value(shift)
    return [value + amount if index >= after_index else value
            for index, value in enumerate(baseline_times)]


def _shifted_gap(pattern, times, gap, suffix_index: int, shift):
    """构造一个非目标 gap 的变体 CP 表达式。

    @param pattern 当前 pattern。
    @param times baseline 时间。
    @param gap 当前非目标 gap。
    @param suffix_index 后移后缀起点。
    @param shift CP-SAT 后移量。
    @return gap 微秒表达式。
    """
    indexes = {role: index for index, role in enumerate(pattern.order)}
    before, after = indexes[gap.before], indexes[gap.after]
    delta = times[after] - times[before]
    if before < suffix_index <= after:
        return delta + shift
    return delta


def _select_primary_layout(program, branches, seed: int):
    """用 no-good 闭包在全部 crossing 选择中求唯一可布局解。

    @param program 冻结生成程序。
    @param branches 声明序 primary branches。
    @param seed 运行随机种子。
    @return 已投影 primary branches。
    """
    forbidden: list[frozenset[int]] = []
    while True:
        paired = _solve_crossings(branches, program, seed, tuple(forbidden))
        try:
            return _place_primary(program, branches, paired, seed)
        except _PrimaryLayoutInfeasible:
            forbidden.append(paired)


def _solve_crossings(branches, program, seed: int, forbidden) -> frozenset[int]:
    """用 CP-SAT 选择不重叠的相邻 crossed-session 边界。

    @param branches 按交付声明序排列的 primary branch。
    @param program 冻结生成程序。
    @param seed 运行随机种子。
    @param forbidden 已证明绝对布局不可行的边界集。
    @return 被选边界左端序号集合。
    """
    target = program.timeline.crossed_primary_sessions
    if target == 0:
        if forbidden:
            _plan_infeasible("primary layout is infeasible")
        return frozenset()
    model = cp_model.CpModel()
    choices = []
    for index in range(len(branches) - 1):
        different = branches[index].slot.slot_key != branches[index + 1].slot.slot_key
        allowed = different and _relative_cross_possible(branches[index], branches[index + 1], program)
        choice = model.new_bool_var(f"cross_{index}")
        if not allowed:
            model.add(choice == 0)
        choices.append(choice)
    for left, right in zip(choices, choices[1:]):
        model.add(left + right <= 1)
    for selection in forbidden:
        model.add(sum(choices[index] for index in selection) <= target - 1)
    model.add(sum(choices) == target)
    model.minimize(sum((index + 1) * choice for index, choice in enumerate(choices)))
    try:
        solver = _solve_crossing_choice(model, _solver_seed(seed, "crossing"))
    except _CrossingChoicesExhausted:
        _plan_infeasible("no crossing selection has a feasible absolute primary layout")
    return frozenset(index for index, choice in enumerate(choices) if solver.value(choice))


def _solve_crossing_choice(model, seed: int):
    """求解 crossing choice，并把 no-good 穷尽与预算失败分开。"""
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = seed
    solver.parameters.max_deterministic_time = 10.0
    status = solver.solve(model)
    if status == cp_model.OPTIMAL:
        return solver
    if status == cp_model.INFEASIBLE:
        raise _CrossingChoicesExhausted
    if status in (cp_model.FEASIBLE, cp_model.UNKNOWN):
        _plan_budget("CP-SAT model did not prove OPTIMAL: crossing")
    _plan_internal("CP-SAT model is invalid: crossing")


def _relative_cross_possible(left, right, program) -> bool:
    """精确预判无日历平移下是否存在 A-B-A 或 B-A-B。

    @param left 声明序左 branch。
    @param right 声明序右 branch。
    @param program 冻结生成程序。
    @return 容量、跨度与交织同时可行时为 true。
    """
    left_offsets, right_offsets = _logical_offsets(left), _logical_offsets(right)
    timeline = program.timeline
    if len(left_offsets) + len(right_offsets) > timeline.session_max_events:
        return False
    cap = timeline.session_max_span_us
    if left_offsets[-1] > cap or right_offsets[-1] > cap:
        return False
    return (_can_insert(left_offsets, right_offsets, cap)
            or _can_insert(right_offsets, left_offsets, cap))


def _can_insert(host, guest, cap: int) -> bool:
    """判断 guest 首事件能否插入 host 的任一相邻空隙。

    @param host 包围 guest 首事件的 owner offsets。
    @param guest 待平移 owner offsets。
    @param cap 完整 session 跨度上限。
    @return 存在不碰撞整数平移时为 true。
    """
    forbidden = {left - right for left in host for right in guest}
    for before, after in zip(host, host[1:]):
        low, high = before + 1, min(after - 1, cap - guest[-1])
        if low > high:
            continue
        if high - low + 1 > len(forbidden):
            return True
        if any(value not in forbidden for value in range(low, high + 1)):
            return True
    return False


def _place_primary(program, branches, paired, seed: int) -> tuple[_PlacedBranch, ...]:
    """按声明序投影 primary session 与全局工件时间。

    @param program 冻结生成程序。
    @param branches primary 逻辑 branches。
    @param paired crossed 边界集合。
    @param seed 运行随机种子。
    @return 完整 primary branch 计划。
    """
    placed: list[_PlacedBranch] = []
    cursor = program.timeline.timestamp_start_us - 1
    session_index = 0
    index = 0
    while index < len(branches):
        lower = _branch_lower_bound(program, cursor, False)
        if index in paired:
            request = _CrossRequest(
                program, branches[index], branches[index + 1], lower,
                _solver_seed(seed, f"cross-layout:{index}"),
            )
            pair = _place_crossed_pair(request, session_index)
            placed.extend(pair)
            cursor = max(event.timestamp_us for item in pair for event in item.events)
            index += 2
        else:
            branch = branches[index]
            start = _earliest_calendar_start(program, branch, lower)
            events = _planned_events(program, branch, start, session_index)
            _check_session(program, branch.slot.slot_key, events, events[0].timestamp_us, len(events))
            placed.append(_PlacedBranch(branch, events, session_index))
            cursor = events[-1].timestamp_us
            index += 1
        session_index += 1
    if session_index != program.timeline.primary_sessions:
        _plan_infeasible("planned primary session count differs from timeline")
    return tuple(placed)


def _place_crossed_pair(request: _CrossRequest, session_index: int):
    """求解并投影一个真正 owner 交织的 session。

    @param request 双 branch 与时间下界。
    @param session_index primary session 序号。
    @return 声明序的两个 placed branch。
    """
    program, left, right = request.program, request.left, request.right
    left_start, right_start = _solve_crossed_starts(request)
    left_events = _planned_events(program, left, left_start, session_index)
    right_events = _planned_events(program, right, right_start, session_index)
    events = tuple(sorted((*left_events, *right_events), key=lambda item: item.timestamp_us))
    _check_session(program, left.slot.slot_key, events, events[0].timestamp_us, len(events))
    return (
        _PlacedBranch(left, left_events, session_index),
        _PlacedBranch(right, right_events, session_index),
    )


def _solve_crossed_starts(request: _CrossRequest) -> tuple[int, int]:
    """用 CP-SAT 联合冻结两个 branch 起点与 owner 交织。

    @param request 双 branch 交织请求。
    @return 左、右 branch 起点。
    """
    upper = request.lower + 21 * _DAY_US
    left_intervals = _branch_start_intervals(
        request.program, request.left, request.lower, upper
    )
    right_intervals = _branch_start_intervals(
        request.program, request.right, request.lower, upper
    )
    if not left_intervals or not right_intervals:
        raise _PrimaryLayoutInfeasible
    model = cp_model.CpModel()
    left = model.new_int_var_from_domain(
        cp_model.Domain.from_intervals(left_intervals), "left_start"
    )
    right = model.new_int_var_from_domain(
        cp_model.Domain.from_intervals(right_intervals), "right_start"
    )
    if not _constrain_crossed_session(model, request, left, right):
        raise _PrimaryLayoutInfeasible
    model.minimize(left + right)
    solver = _solve_cross_layout(model, request)
    return solver.value(left), solver.value(right)


def _constrain_crossed_session(model, request, left_start, right_start) -> bool:
    """加入时间唯一、session 容量与 owner 交织约束。

    @param model 当前 CP-SAT 模型。
    @param request 双 branch 交织请求。
    @param left_start 左 branch 起点变量。
    @param right_start 右 branch 起点变量。
    @return 存在基本交织证明形状时为 true。
    """
    left_offsets = _logical_offsets(request.left)
    right_offsets = _logical_offsets(request.right)
    if len(left_offsets) + len(right_offsets) > request.program.timeline.session_max_events:
        return False
    for left in left_offsets:
        for right in right_offsets:
            model.add(right_start + right != left_start + left)
    _constrain_session_span(model, request, left_start, right_start)
    witnesses = _alternation_witnesses(model, left_offsets, right_offsets, left_start, right_start)
    if not witnesses:
        return False
    model.add(sum(witnesses) >= 1)
    return True


def _constrain_session_span(model, request, left_start, right_start) -> None:
    """限制双 owner session 的完整跨度。

    @param model 当前 CP-SAT 模型。
    @param request 双 branch 交织请求。
    @param left_start 左 branch 起点。
    @param right_start 右 branch 起点变量。
    @return None。
    """
    cap = request.program.timeline.session_max_span_us
    lower = request.lower
    horizon = lower + 21 * _DAY_US + cap
    left_end = left_start + _logical_offsets(request.left)[-1]
    first = model.new_int_var(lower, horizon, "session_first")
    last = model.new_int_var(lower, horizon + cap, "session_last")
    model.add_min_equality(first, [left_start, right_start])
    model.add_max_equality(last, [
        left_end,
        right_start + _logical_offsets(request.right)[-1],
    ])
    model.add(last - first <= cap)


def _alternation_witnesses(model, left, right, left_start: int, right_start):
    """构造 A-B-A 或 B-A-B 的可重化证明。

    @param model 当前 CP-SAT 模型。
    @param left 左 branch 逻辑 offset。
    @param right 右 branch 逻辑 offset。
    @param left_start 左 branch 起点。
    @param right_start 右 branch 起点变量。
    @return 可选交织证明变量。
    """
    witnesses = []
    for index, (before, after) in enumerate(zip(left, left[1:])):
        flag = model.new_bool_var(f"left_wraps_{index}")
        model.add(right_start + right[0] > left_start + before).only_enforce_if(flag)
        model.add(right_start + right[0] < left_start + after).only_enforce_if(flag)
        witnesses.append(flag)
    for index, (before, after) in enumerate(zip(right, right[1:])):
        flag = model.new_bool_var(f"right_wraps_{index}")
        model.add(right_start + before < left_start + left[0]).only_enforce_if(flag)
        model.add(left_start + left[0] < right_start + after).only_enforce_if(flag)
        witnesses.append(flag)
    return witnesses


def _solve_cross_layout(model, request: _CrossRequest):
    """求解一个 crossing 绝对日历布局，区分可替换选择与运行终态。

    @param model 待求解 crossing CP-SAT 模型。
    @param request 双 branch 交织请求。
    @return 只有 OPTIMAL 时的 solver。
    """
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = request.seed
    solver.parameters.max_deterministic_time = 10.0
    status = solver.solve(model)
    if status == cp_model.OPTIMAL:
        return solver
    if status == cp_model.INFEASIBLE:
        raise _PrimaryLayoutInfeasible
    if status in (cp_model.FEASIBLE, cp_model.UNKNOWN):
        _plan_budget("crossed layout did not prove OPTIMAL")
    _plan_internal("crossed layout model is invalid")


def _logical_offsets(branch) -> tuple[int, ...]:
    """返回 branch 相对首事件的逻辑时间。

    @param branch 逻辑 branch。
    @return 从零开始的时间 offset。
    """
    first = branch.logical_times[0]
    return tuple(value - first for value in branch.logical_times)


def _branch_lower_bound(program, cursor: int, shares: bool) -> int:
    """计算下一个 branch 的最早工件起点。

    @param program 冻结生成程序。
    @param cursor 前一 branch 最后事件时间。
    @param shares 是否与前一 branch 共 session。
    @return 严格递增的最早起点。
    """
    if cursor < program.timeline.timestamp_start_us:
        return program.timeline.timestamp_start_us
    gap = (program.timeline.event_gap_us[0] if shares
           else program.timeline.session_gap_us)
    return cursor + max(gap, 1)


def _planned_events(program, branch, start: int, session_index: int):
    """把一个逻辑 branch 转换为 PlannedEvent tuple。

    @param program 冻结生成程序。
    @param branch 当前逻辑 branch。
    @param start branch 首事件工件时间。
    @param session_index primary session 序号。
    @return 完整 PlannedEvent tuple。
    """
    first = branch.logical_times[0]
    scenario_id = _scenario_id(program, branch.slot)
    events = []
    for position, (role, logical_time) in enumerate(
        zip(branch.roles, branch.logical_times)
    ):
        event_key = _event_key(program, branch.slot, scenario_id, role, position)
        events.append(PlannedEvent(
            event_key=event_key,
            role=role,
            position=position,
            logical_time_us=logical_time,
            timestamp_us=start + logical_time - first,
            session_id=f"primary_{session_index:06d}",
        ))
    return tuple(events)


def _earliest_calendar_start(program, branch, lower: int) -> int:
    """求满足全部 role calendar 的最早 branch start。

    @param program 冻结生成程序。
    @param branch 当前逻辑 branch。
    @param lower 起点下界。
    @return 最早合法 epoch 微秒。
    """
    if program.mode == "instruction_only":
        return lower
    pattern = program.patterns[branch.slot.pattern_name]
    roles = {role.name: role for role in pattern.roles}
    constrained = [
        (logical - branch.logical_times[0], roles[name].calendar_window)
        for name, logical in zip(branch.roles, branch.logical_times)
        if roles[name].calendar_window is not None
    ]
    if not constrained:
        return lower
    intervals = [(lower, lower + 21 * _DAY_US)]
    for offset, window_name in constrained:
        allowed = _calendar_start_intervals(program.calendar_windows[window_name], offset, lower)
        intervals = _intersect_intervals(intervals, allowed)
    if not intervals:
        raise _PrimaryLayoutInfeasible
    return intervals[0][0]


def _branch_start_intervals(program, branch, lower: int, upper: int):
    """返回一个 branch 在给定边界内的全部日历起点区间。

    @param program 冻结生成程序。
    @param branch 待放置逻辑 branch。
    @param lower 闭区间下界。
    @param upper 闭区间上界。
    @return 排序且不为空的闭区间表。
    """
    intervals = [(lower, upper)]
    if program.mode == "instruction_only":
        return intervals
    pattern = program.patterns[branch.slot.pattern_name]
    roles = {role.name: role for role in pattern.roles}
    for name, logical in zip(branch.roles, branch.logical_times):
        window_name = roles[name].calendar_window
        if window_name is None:
            continue
        offset = logical - branch.logical_times[0]
        allowed = _calendar_start_intervals(program.calendar_windows[window_name], offset, lower)
        intervals = _intersect_intervals(intervals, allowed)
    return [(start, end) for start, end in intervals if start <= upper and end >= lower]


def _calendar_start_intervals(window, logical_offset: int, lower: int):
    """展开 21 天内一个 role window 的可用 branch-start 区间。

    @param window 命名 calendar window。
    @param logical_offset role 相对 branch 起点偏移。
    @param lower branch start 下界。
    @return 闭区间整数微秒列表。
    """
    zone = timezone(timedelta(minutes=window.utc_offset_minutes))
    try:
        local = (_EPOCH_UTC + timedelta(microseconds=lower)).astimezone(zone)
    except (OverflowError, ValueError):
        _plan_infeasible("calendar timestamp exceeds supported datetime range")
    day = datetime(local.year, local.month, local.day, tzinfo=zone)
    intervals = []
    for day_offset in range(22):
        try:
            current = day + timedelta(days=day_offset)
        except OverflowError:
            _plan_infeasible("calendar horizon exceeds supported datetime range")
        weekday = current.weekday()
        if weekday not in {_WEEKDAY[name] for name in window.days}:
            continue
        delta = current.astimezone(timezone.utc) - _EPOCH_UTC
        base = delta.days * _DAY_US + delta.seconds * 1_000_000 + delta.microseconds
        intervals.extend(
            (base + start - logical_offset, base + end - logical_offset - 1)
            for start, end in window.intervals_us
        )
    return intervals


def _validate_timestamp_range(program, placed, noise_slots, replay_layouts) -> None:
    """在计划交付前验证全部工件时间可精确渲染为 ISO8601。"""
    primary = (event.timestamp_us for branch in placed for event in branch.events)
    replay = (value for layout in replay_layouts for value in layout.timestamps_us)
    timestamps = tuple((*primary, *(item.timestamp_us for item in noise_slots), *replay))
    zone = timezone(timedelta(minutes=program.timeline.utc_offset_minutes))
    try:
        for value in timestamps:
            (_EPOCH_UTC + timedelta(microseconds=value)).astimezone(zone)
    except (OverflowError, ValueError):
        _plan_infeasible("planned timestamp exceeds supported datetime range")


def _intersect_intervals(left, right):
    """求两个闭区间集合的规范交集。

    @param left 已排序闭区间。
    @param right 已排序闭区间。
    @return 非空交集区间。
    """
    result = []
    for left_start, left_end in left:
        for right_start, right_end in right:
            start, end = max(left_start, right_start), min(left_end, right_end)
            if start <= end:
                result.append((start, end))
    return sorted(result)


def _check_session(program, slot_key, events, start: int, count: int) -> None:
    """验证一个 primary session 的容量与跨度。

    @param program 冻结生成程序。
    @param slot_key 当前 slot identity。
    @param events 当前 branch 事件。
    @param start session 首事件时间。
    @param count session 累计事件数。
    @return None。
    """
    if count > program.timeline.session_max_events:
        raise _PrimaryLayoutInfeasible
    if events[-1].timestamp_us - start > program.timeline.session_max_span_us:
        raise _PrimaryLayoutInfeasible


def _allocate_blocks(program, placed, baselines):
    """按完整 primary session 分配不超过 4096 事件的 blocks。

    @param program 冻结生成程序。
    @param placed 已放置 primary branches。
    @param baselines 按 slot 索引的 baseline。
    @return ScenarioBlock tuple。
    """
    groups = _session_groups(placed)
    blocks: list[dict] = []
    current: dict = {}
    primary_events = 0
    included_baselines: set[str] = set()
    positives = {
        item.logical.slot.slot_key: item.events
        for item in placed
        if _is_positive(program, item.logical)
    }
    for group in groups:
        group_events = sum(len(item.events) for item in group)
        if current and primary_events + group_events > 4096:
            blocks.append(current)
            current, primary_events = {}, 0
        for item in group:
            _add_branch_plan(
                program, current, item, baselines, (included_baselines, positives)
            )
        primary_events += group_events
    if current:
        blocks.append(current)
    return tuple(MappingProxyType(block) for block in blocks)


def _session_groups(placed):
    """把连续 placed branches 按 session 分组。

    @param placed 已放置 primary branches。
    @return session group 列表。
    """
    groups = []
    for item in placed:
        if not groups or groups[-1][0].session_index != item.session_index:
            groups.append([item])
        else:
            groups[-1].append(item)
    return groups


def _add_branch_plan(program, block, item, baselines, indexes) -> None:
    """向 block 加入 branch 并在首次出现时加入 hidden baseline。

    @param program 冻结生成程序。
    @param block 当前可变 block。
    @param item 当前 placed branch。
    @param baselines baseline 索引。
    @param indexes 已加入 slot 集与 positive 事件索引。
    @return None。
    """
    included, positives = indexes
    slot_key = item.logical.slot.slot_key
    if program.mode == "declared" and slot_key not in included:
        baseline = baselines[slot_key]
        events = positives.get(slot_key)
        if events is None:
            events = _baseline_events(program, baseline, item.events)
        block[(slot_key, None)] = events
        included.add(slot_key)
    block[(slot_key, item.logical.variant_name)] = item.events


def _baseline_events(program, baseline, anchor_events):
    """为 hidden baseline 独立求解日历并复用 anchor 的内部 session 名。

    @param program 冻结生成程序。
    @param baseline hidden baseline 逻辑 branch。
    @param anchor_events 当前 slot 首个 delivery branch 事件。
    @return baseline PlannedEvent tuple。
    """
    start = _earliest_calendar_start(
        program, baseline, program.timeline.timestamp_start_us,
    )
    session = anchor_events[0].session_id
    scenario_id = _scenario_id(program, baseline.slot)
    return tuple(PlannedEvent(
        event_key=_event_key(program, baseline.slot, scenario_id, role, position),
        role=role,
        position=position,
        logical_time_us=logical,
        timestamp_us=start + logical - baseline.logical_times[0],
        session_id=session,
    ) for position, (role, logical) in enumerate(
        zip(baseline.roles, baseline.logical_times)
    ))


def _noise_slots(program, cursor: int) -> tuple[tuple[NoiseSlot, ...], int]:
    """在 primary 后冻结精确 NoiseSlot。

    @param program 冻结生成程序。
    @param cursor 最后 primary 时间。
    @return noise slots 与更新后的 cursor。
    """
    if program.timeline.noise_events == 0:
        return (), cursor
    slots = []
    session_index = 0
    session_start = None
    session_count = 0
    for ordinal in range(program.timeline.noise_events):
        cursor, session_index, session_start, session_count = _next_noise_time(
            program, cursor, session_index, session_start, session_count
        )
        event_key = derive_generation_id(
            "noise_event_key", [program.digest, "noise", ordinal]
        )
        slots.append(NoiseSlot(
            event_key=event_key,
            ordinal=ordinal,
            frame_class=program.noise.frame_class,
            topic=program.noise.topics[ordinal],
            timestamp_us=cursor,
            session_id=f"noise_{session_index:06d}",
        ))
    return tuple(slots), cursor


def _next_noise_time(program, cursor, session_index, session_start, session_count):
    """计算下一个 noise 的 session 与时间。

    @param program 冻结生成程序。
    @param cursor 前一全局事件时间。
    @param session_index 当前 noise session 序号。
    @param session_start 当前 session 首时间或 None。
    @param session_count 当前 session 事件数。
    @return 更新后的四元组。
    """
    gap = max(program.timeline.event_gap_us[0], 1)
    timestamp = cursor + gap
    if session_start is None:
        timestamp = cursor + max(program.timeline.session_gap_us, 1)
        session_start = timestamp
    exceeds = (session_count >= program.timeline.session_max_events
               or timestamp - session_start > program.timeline.session_max_span_us)
    if exceeds:
        session_index += 1
        timestamp = cursor + max(program.timeline.session_gap_us, 1)
        session_start, session_count = timestamp, 0
    return timestamp, session_index, session_start, session_count + 1


def _replay_layouts(program, placed, cursor: int) -> tuple[ReplayLayout, ...]:
    """从 declaration-order positive source 冻结完整 replay layouts。

    @param program 冻结生成程序。
    @param placed 已放置 primary branches。
    @param cursor primary/noise 最后时间。
    @return replay layouts。
    """
    positives = [item for item in placed if _is_positive(program, item.logical)]
    layouts = []
    for ordinal, source in enumerate(positives[:program.timeline.duplicate_sequences]):
        cursor += max(program.timeline.session_gap_us, 1)
        first = source.events[0].logical_time_us
        timestamps = tuple(
            cursor + event.logical_time_us - first for event in source.events
        )
        if len(timestamps) > program.timeline.session_max_events:
            _plan_infeasible("replay exceeds session event capacity")
        if timestamps[-1] - timestamps[0] > program.timeline.session_max_span_us:
            _plan_infeasible("replay exceeds session span")
        layouts.append(ReplayLayout(
            source_slot_key=source.logical.slot.slot_key,
            source_variant_name=source.logical.variant_name,
            replay_ordinal=ordinal,
            session_id=f"replay_{ordinal:06d}",
            timestamps_us=timestamps,
        ))
        cursor = timestamps[-1]
    return tuple(layouts)


def _is_positive(program, branch) -> bool:
    """判断一个 delivery branch 是否为 positive。

    @param program 冻结生成程序。
    @param branch 当前逻辑 branch。
    @return positive 时为 true。
    """
    if program.mode != "declared":
        return False
    source = _counterfactual_source(program, branch.slot.source_name)
    return any(item.name == branch.variant_name and item.kind == "positive"
               for item in source.variants)


def _scenario_id(program, slot) -> str:
    """派生 declared 或 instruction-only scenario ID。

    @param program 冻结生成程序。
    @param slot 当前交付槽。
    @return scenario ID。
    """
    domain = ("declared_scenario_id" if program.mode == "declared"
              else "instruction_scenario_id")
    return derive_generation_id(
        domain, [program.digest, slot.source_name, slot.scenario_index]
    )


def _event_key(program, slot, scenario_id: str, role: str, position: int) -> str:
    """派生 declared 或 instruction-only event key。

    @param program 冻结生成程序。
    @param slot 当前交付槽。
    @param scenario_id 当前 scenario ID。
    @param role baseline role 或 position label。
    @param position 事件位置。
    @return event key。
    """
    if program.mode == "declared":
        return derive_generation_id("declared_event_key", [scenario_id, role])
    return derive_generation_id("instruction_event_key", [
        scenario_id, slot.source_name, slot.scenario_index, position,
    ])


def _counterfactual_source(program, name: str):
    """按名称解析一个 CounterfactualSetSpec。

    @param program 冻结生成程序。
    @param name source name。
    @return 唯一 counterfactual set。
    """
    return next(item for item in program.counterfactual_sets if item.name == name)


def _solve(model: cp_model.CpModel, seed: int, identity: str) -> cp_model.CpSolver:
    """按冻结参数求解一个 CP-SAT 优化层。

    @param model 待求解模型。
    @param seed 确定性随机种子。
    @param identity 不含数据内容的模型身份。
    @return 仅 OPTIMAL 状态的 solver。
    """
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = seed
    solver.parameters.max_deterministic_time = 10.0
    status = solver.solve(model)
    if status == cp_model.OPTIMAL:
        return solver
    if status == cp_model.INFEASIBLE:
        _plan_infeasible(f"CP-SAT model is infeasible: {identity}")
    if status in (cp_model.FEASIBLE, cp_model.UNKNOWN):
        _plan_budget(f"CP-SAT model did not prove OPTIMAL: {identity}")
    _plan_internal(f"CP-SAT model is invalid: {identity}")


def _solver_seed(seed: int, identity: str) -> int:
    """为一个 solver layer 派生稳定 31-bit seed。

    @param seed 运行随机种子。
    @param identity layer 身份。
    @return OR-Tools 接受的非负整数 seed。
    """
    material = canonical_json(["labelkit:v1.18", "planner_seed", [seed, identity]])
    return int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:4], "big") & 0x7fffffff


def _plan_infeasible(message: str):
    """抛出冻结的 plan infeasible 配置错误。

    @param message 不含数据内容的英文原因。
    @return 不返回。
    """
    _log.error("generation_plan_infeasible: %s", message)
    raise ConfigError([f"generation_plan_infeasible: {message}"])


def _plan_budget(message: str):
    """抛出冻结的 planner budget 错误。

    @param message 不含数据内容的英文原因。
    @return 不返回。
    """
    _log.error("generation_plan_budget: %s", message)
    raise InternalError(f"generation_plan_budget: {message}")


def _plan_internal(message: str):
    """抛出冻结的 planner internal 错误。

    @param message 不含数据内容的英文原因。
    @return 不返回。
    """
    _log.error("generation_plan_internal: %s", message)
    raise InternalError(f"generation_plan_internal: {message}")

"""v1.21 确定性点、区间与交织 ScenarioPlan 编译器。"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

from jsonpointer import JsonPointer
from jsonschema import Draft202012Validator
from ortools.sat.python import cp_model

from labelkit.common.config._temporal import resolve_frame_time_values
from labelkit.common.config.generation import is_generation_frame_eligible
from labelkit.common.contracts.generation import (
    DeliverySlot,
    GenerationProgram,
    InterleavingLayout,
    NoiseSlot,
    PlannedEvent,
    ReplayLayout,
    ScenarioPlan,
)
from labelkit.common.errors import ConfigError, InternalError
from labelkit.operators.generation.project import (
    derive_generation_id,
    generation_digest,
    generation_random,
    scenario_plan_digest,
)


_log = logging.getLogger("labelkit.generation.planner")
_QUANTUM_US = 1000
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
class _InterleavingPair:
    """一个冻结的 trigger/partner 交织配对。"""

    layout: InterleavingLayout  # 对外冻结配对身份。
    trigger: _LogicalBranch  # trigger positive branch。
    partner: _LogicalBranch  # partner positive branch。


@dataclass(frozen=True)
class _InterleavingSelection:
    """一次精确抽取后的交织决策与机会统计。"""

    pairs: tuple[_InterleavingPair, ...]  # 按 trigger scan 顺序的配对。
    opportunities: int  # 全局抽取机会数。
    pattern_opportunities: tuple[tuple[str, int], ...]  # pattern 声明序机会数。


@dataclass(frozen=True)
class _InterleavingRequest:
    """一个只允许整体平移的双 branch 布局请求。"""

    program: GenerationProgram  # 冻结生成程序。
    pair: _InterleavingPair  # 已冻结且不可重抽的配对。
    lower: int  # 该 session 全部事件的下界。
    seed: int  # 布局 CP-SAT 确定性种子。


def compile_scenario_plan(program: GenerationProgram) -> ScenarioPlan:
    """求解并返回唯一可接受的 OPTIMAL 确定性计划。

    @param program 冻结生成程序。
    @return 完整冻结的场景计划。
    """
    from labelkit.operators.generation.program import generation_program_digest

    if program.digest != generation_program_digest(program):
        _plan_internal("generation program digest is invalid")
    _require_program_quantum(program)
    seed = program.planner_seed
    slots = _delivery_slots(program)
    logical, baselines = _logical_branches(program, slots, seed)
    selection = _select_interleaving(program, logical)
    placed = _place_primary(program, logical, selection)
    blocks = _allocate_blocks(program, placed, baselines)
    cursor = max((_events_tail(item.events) for item in placed),
                 default=program.timeline.timestamp_start_us)
    noise_slots, cursor = _noise_slots(program, cursor)
    replay_layouts = _replay_layouts(program, logical, placed, cursor)
    _validate_timestamp_range(program, placed, noise_slots, replay_layouts)
    base = ScenarioPlan(
        blocks=blocks,
        delivery_slots=slots,
        noise_slots=noise_slots,
        replay_layouts=replay_layouts,
        interleaving_layouts=tuple(pair.layout for pair in selection.pairs),
        interleaving_opportunities=selection.opportunities,
        interleaving_pattern_opportunities=MappingProxyType(
            dict(selection.pattern_opportunities)
        ),
        primary_sessions=len(logical) - len(selection.pairs),
        digest="",
    )
    _preflight_temporal_values(program, base)
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
            program, pattern, source.variants,
            _solver_seed(seed, f"baseline:{slot.slot_key}"),
        )
        baseline = _LogicalBranch(slot, None, pattern.order, times)
        baselines[slot.slot_key] = baseline
        for variant_index, variant in enumerate(source.variants):
            variant_seed = _solver_seed(seed, f"variant:{slot.slot_key}:{variant_index}")
            branches.append(_variant_branch(
                program, baseline, pattern, variant, variant_seed,
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


def _solve_pattern_times(program, pattern, variants, solver_seed: int) -> tuple[int, ...]:
    """求解 baseline role 时间与全部 gap/span 约束。

    @param program 冻结生成程序。
    @param pattern 当前精确 pattern。
    @param variants 当前 set 的完整变体声明。
    @param solver_seed 确定性 solver seed。
    @return 与 pattern.order 对位的逻辑微秒。
    """
    _require_pattern_quantum(pattern)
    model = cp_model.CpModel()
    span_ms = pattern.max_span_us // _QUANTUM_US
    times = [model.new_int_var(0, span_ms, f"time_{index}")
             for index in range(len(pattern.order))]
    model.add(times[0] == 0)
    role_indexes = {role: index for index, role in enumerate(pattern.order)}
    for left, right in zip(times, times[1:]):
        model.add(left + 1 <= right)
    for gap in pattern.gaps:
        delta = times[role_indexes[gap.after]] - times[role_indexes[gap.before]]
        model.add(delta >= gap.min_gap_us // _QUANTUM_US)
        model.add(delta <= gap.max_gap_us // _QUANTUM_US)
    starts = {role: times[index] for role, index in role_indexes.items()}
    _add_temporal_constraints(model, program, pattern, starts, pattern.order)
    _add_reordered_constraints(model, program, pattern, times, variants)
    _add_exceeded_feasibility(model, program, pattern, times, variants)
    solver = _solve_lexicographic(model, solver_seed, pattern.name, tuple(times))
    return tuple(solver.value(value) * _QUANTUM_US for value in times)


def _add_reordered_constraints(model, program, pattern, times, variants) -> None:
    """把每个错序分支的全部非目标结构约束并入 baseline 模型。

    @param model 当前 baseline CP-SAT 模型。
    @param program 冻结生成程序。
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
                model.add(branch_times[left] + 1 <= branch_times[right])
        for gap in pattern.gaps:
            if (gap.before, gap.after) == (before, after):
                continue
            delta = branch_times[gap.after] - branch_times[gap.before]
            model.add(delta >= gap.min_gap_us // _QUANTUM_US)
            model.add(delta <= gap.max_gap_us // _QUANTUM_US)
        ordered = list(pattern.order)
        before_index, after_index = ordered.index(before), ordered.index(after)
        ordered[before_index], ordered[after_index] = ordered[after_index], ordered[before_index]
        _add_temporal_constraints(model, program, pattern, branch_times, tuple(ordered))


def _add_exceeded_feasibility(model, program, pattern, times, variants) -> None:
    """把每个 interval-exceeded 分支的存在性并入 baseline 模型。"""
    indexes = {role: index for index, role in enumerate(pattern.order)}
    for variant in variants:
        if variant.kind != "interval_exceeded":
            continue
        target = next(item for item in pattern.gaps if item.name == variant.target["gap"])
        suffix = indexes[target.after]
        low = (target.max_gap_us + int(variant.target["min_excess_us"])) // _QUANTUM_US
        high = (target.max_gap_us + int(variant.target["max_excess_us"])) // _QUANTUM_US
        shift = model.new_int_var(0, pattern.max_span_us // _QUANTUM_US,
                                  f"exceeded_shift_{variant.name}")
        starts = _shifted_starts(model, pattern, times, suffix, shift)
        model.add(starts[target.after] - starts[target.before] >= low)
        model.add(starts[target.after] - starts[target.before] <= high)
        _add_non_target_gap_constraints(model, pattern, starts, target.name)
        _add_temporal_constraints(model, program, pattern, starts, pattern.order)


def _add_non_target_gap_constraints(model, pattern, starts, target_name: str) -> None:
    """向 interval-exceeded 模型加入所有非目标 gap。"""
    for gap in pattern.gaps:
        if gap.name == target_name:
            continue
        delta = starts[gap.after] - starts[gap.before]
        model.add(delta >= gap.min_gap_us // _QUANTUM_US)
        model.add(delta <= gap.max_gap_us // _QUANTUM_US)


def _shifted_starts(model, pattern, times, suffix_index: int, shift) -> dict:
    """构造可直接作为 fixed interval 起点的后缀平移变量。

    @param model 当前 CP-SAT 模型。
    @param pattern 当前 pattern。
    @param times baseline 起点变量。
    @param suffix_index 后缀首 role 序号。
    @param shift 后缀统一平移量。
    @return role 到可用起点变量的映射。
    """
    upper = 2 * pattern.max_span_us // _QUANTUM_US
    prefix = len(model.proto.variables)
    starts = {}
    for index, role in enumerate(pattern.order):
        if index < suffix_index:
            starts[role] = times[index]
            continue
        shifted = model.new_int_var(0, upper, f"shifted_{prefix}_{role}")
        model.add(shifted == times[index] + shift)
        starts[role] = shifted
    return starts


def _add_temporal_constraints(model, program, pattern, starts, roles) -> None:
    """把区间包络、资源互斥与严格包含加入一个 branch 模型。"""
    durations = _role_durations_ms(program, pattern)
    resources = _role_resources(program, pattern)
    present = tuple(role for role in roles if role in starts)
    _add_interval_envelope(model, pattern, starts, durations, present)
    by_resource: dict[str, list] = {}
    for role in present:
        duration = durations[role]
        if duration <= 0:
            continue
        interval = model.new_fixed_size_interval_var(
            starts[role], duration, f"{role}_interval")
        for resource in resources[role]:
            by_resource.setdefault(resource, []).append(interval)
    for intervals in by_resource.values():
        model.add_no_overlap(intervals)
    _add_containment_constraints(model, pattern, starts, durations, frozenset(present))


def _add_interval_envelope(model, pattern, starts, durations, roles) -> None:
    """以完整 interval envelope 限制 pattern span。"""
    cap = pattern.max_span_us // _QUANTUM_US
    upper = cap + max(durations.values(), default=0)
    first = model.new_int_var(0, upper, "interval_first")
    last = model.new_int_var(0, upper, "interval_last")
    model.add_min_equality(first, [starts[role] for role in roles])
    model.add_max_equality(last, [starts[role] + durations[role] for role in roles])
    model.add(last - first <= cap)


def _add_containment_constraints(model, pattern, starts, durations, present) -> None:
    """对仍同时在场的 role 加入一个 quantum 严格包含。"""
    for item in pattern.containments:
        if item.contained not in present:
            continue
        if item.container not in present:
            model.add(0 == 1)
            continue
        if durations[item.container] <= 0 or durations[item.contained] <= 0:
            model.add(0 == 1)
            continue
        model.add(starts[item.container] <= starts[item.contained])
        model.add(
            starts[item.contained] + durations[item.contained] + 1
            <= starts[item.container] + durations[item.container]
        )


def _role_durations_ms(program, pattern) -> dict[str, int]:
    """返回 pattern role 到固定毫秒时长的表。"""
    return {
        role.name: _duration_ms(program.frame_classes[role.frame_class].duration_us)
        for role in pattern.roles
    }


def _role_resources(program, pattern) -> dict[str, tuple[str, ...]]:
    """返回 pattern role 到声明序资源的表。"""
    return {
        role.name: program.frame_classes[role.frame_class].resources
        for role in pattern.roles
    }


def _require_pattern_quantum(pattern) -> None:
    """防止绕过 M1 的非毫秒 pattern 进入求解器。"""
    values = [pattern.max_span_us]
    values.extend(value for gap in pattern.gaps for value in (gap.min_gap_us, gap.max_gap_us))
    if any(value % _QUANTUM_US for value in values):
        _plan_infeasible("pattern timing must use the millisecond quantum")


def _duration_ms(duration_us: int) -> int:
    """校验并返回 Planner 毫秒 quantum 时长。"""
    if duration_us < 0 or duration_us % _QUANTUM_US:
        _plan_infeasible("frame duration must use the millisecond quantum")
    return duration_us // _QUANTUM_US


def _require_program_quantum(program) -> None:
    """在建模前拒绝绕过 M1 的非毫秒全局时间。"""
    timeline = program.timeline
    values = [
        timeline.timestamp_start_us, *timeline.event_gap_us,
        timeline.session_max_span_us, timeline.session_gap_us,
    ]
    values.extend(
        value for window in program.calendar_windows.values()
        for interval in window.intervals_us for value in interval
    )
    values.extend(
        int(variant.target[key])
        for source in program.counterfactual_sets for variant in source.variants
        if variant.kind == "interval_exceeded"
        for key in ("min_excess_us", "max_excess_us")
    )
    if any(value % _QUANTUM_US for value in values):
        _plan_infeasible("generation timeline must use the millisecond quantum")
    for frame in program.frame_classes.values():
        _duration_ms(frame.duration_us)


def _variant_branch(program, baseline, pattern, variant,
                    solver_seed: int) -> _LogicalBranch:
    """机械派生一个 positive/counterfactual branch。

    @param program 冻结生成程序。
    @param baseline 当前 slot baseline。
    @param pattern 当前 pattern。
    @param variant 当前变体声明。
    @param solver_seed 变体求解器随机种子。
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
            program, pattern, baseline.logical_times, variant,
            solver_seed,
        )
    branch = _LogicalBranch(baseline.slot, variant.name, tuple(roles), tuple(times))
    _validate_logical_branch(program, pattern, branch)
    return branch


def _exceeded_times(program, pattern, baseline_times, variant,
                    solver_seed: int) -> list[int]:
    """求解 interval-exceeded 的唯一 suffix 平移量。

    @param program 冻结生成程序。
    @param pattern 当前 pattern。
    @param baseline_times baseline 逻辑时间。
    @param variant interval-exceeded 变体。
    @param solver_seed 确定性 solver seed。
    @return 平移后的逻辑时间。
    """
    target = next(gap for gap in pattern.gaps if gap.name == variant.target["gap"])
    indexes = {role: index for index, role in enumerate(pattern.order)}
    after_index = indexes[target.after]
    low = (target.max_gap_us + int(variant.target["min_excess_us"])) // _QUANTUM_US
    high = (target.max_gap_us + int(variant.target["max_excess_us"])) // _QUANTUM_US
    base_times = [value // _QUANTUM_US for value in baseline_times]
    base_gap = base_times[after_index] - base_times[indexes[target.before]]
    model = cp_model.CpModel()
    shift = model.new_int_var(max(0, low - base_gap), high - base_gap, "suffix_shift")
    starts = _shifted_starts(model, pattern, base_times, after_index, shift)
    _add_non_target_gap_constraints(model, pattern, starts, target.name)
    _add_temporal_constraints(model, program, pattern, starts, pattern.order)
    model.minimize(shift)
    solver = _solve(model, solver_seed, variant.name)
    amount = solver.value(shift)
    return [value + amount * _QUANTUM_US if index >= after_index else value
            for index, value in enumerate(baseline_times)]


def _validate_logical_branch(program, pattern, branch) -> None:
    """用固定 CP-SAT 模型验证派生 branch 的全部区间约束。"""
    model = cp_model.CpModel()
    starts = {
        role: model.new_constant(value // _QUANTUM_US)
        for role, value in zip(branch.roles, branch.logical_times)
    }
    _add_temporal_constraints(model, program, pattern, starts, branch.roles)
    _solve(model, _solver_seed(program.planner_seed, f"fixed:{branch.slot.slot_key}"),
           branch.variant_name or "baseline")


def _select_interleaving(program, branches) -> _InterleavingSelection:
    """按 trigger 声明序冻结权重抽取与共享 partner pool。"""
    if program.interleaving is None:
        return _InterleavingSelection((), 0, ())
    patterns = program.interleaving.patterns
    candidates = _positive_candidates(program, branches)
    partner_names = {item.partner_candidate_set for item in patterns}
    pools = {name: [] for name in partner_names}
    for branch, candidate in candidates:
        if candidate in pools:
            pools[candidate].append(branch)
    counts = {item.name: 0 for item in patterns}
    pairs = []
    opportunities = 0
    for trigger, candidate in candidates:
        applicable = tuple(
            item for item in patterns
            if item.trigger_candidate_set == candidate and pools[item.partner_candidate_set]
        )
        if not applicable:
            continue
        opportunities += 1
        for item in applicable:
            counts[item.name] += 1
        pattern = _choose_interleaving_pattern(program, trigger, applicable)
        if pattern is None:
            continue
        pool = pools[pattern.partner_candidate_set]
        partner = _draw_partner(program, trigger, pattern, pool)
        pairs.append(_interleaving_pair(pattern, trigger, partner))
    ordered = tuple((item.name, counts[item.name]) for item in patterns)
    return _InterleavingSelection(tuple(pairs), opportunities, ordered)


def _positive_candidates(program, branches):
    """按 branch 声明序返回带标签的唯一 positive branch。"""
    sources = {item.name: item for item in program.counterfactual_sets}
    positive_names = {
        item.name: next(
            variant.name for variant in item.variants if variant.kind == "positive"
        )
        for item in program.counterfactual_sets
        if item.interleaving_candidate_set is not None
    }
    return tuple(
        (branch, sources[branch.slot.source_name].interleaving_candidate_set)
        for branch in branches
        if branch.slot.source_name in positive_names
        and branch.variant_name == positive_names[branch.slot.source_name]
    )


def _choose_interleaving_pattern(program, trigger, applicable):
    """以精确整数 ticket 选择 none 或一个声明序 pattern。"""
    none_weight = program.interleaving.no_interleaving_weight
    total = none_weight + sum(item.trigger_weight for item in applicable)
    material = (
        program.digest, program.planner_seed,
        trigger.slot.slot_key, trigger.variant_name,
    )
    ticket = _bounded_random("interleaving_pattern_choice", material, total)
    if ticket < none_weight:
        return None
    ticket -= none_weight
    for item in applicable:
        if ticket < item.trigger_weight:
            return item
        ticket -= item.trigger_weight
    _plan_internal("interleaving pattern ticket is outside its weight range")


def _draw_partner(program, trigger, pattern, pool):
    """从共享 pool 无偏抽取并 swap-delete 一个 partner。"""
    material = (
        program.digest, program.planner_seed,
        trigger.slot.slot_key, trigger.variant_name,
        pattern.name, pattern.partner_candidate_set,
    )
    index = _bounded_random("interleaving_partner_choice", material, len(pool))
    partner = pool[index]
    pool[index] = pool[-1]
    pool.pop()
    return partner


def _bounded_random(domain: str, material, upper: int) -> int:
    """以拒绝采样把完整 SHA-256 整数无偏投影到正范围。"""
    maximum = 1 << 256
    limit = maximum - maximum % upper
    counter = 0
    while True:
        value = generation_random(domain, [*material, counter])
        if value < limit:
            return value % upper
        counter += 1


def _interleaving_pair(pattern, trigger, partner) -> _InterleavingPair:
    """冻结一个命名 pattern 的精确 branch identity。"""
    layout = InterleavingLayout(
        pattern_name=pattern.name,
        trigger_slot_key=trigger.slot.slot_key,
        trigger_variant_name=trigger.variant_name,
        partner_slot_key=partner.slot.slot_key,
        partner_variant_name=partner.variant_name,
    )
    return _InterleavingPair(layout, trigger, partner)


def _branch_identity(branch) -> tuple[str, str | None]:
    """返回一个 logical branch 的冻结身份。"""
    return branch.slot.slot_key, branch.variant_name


def _place_primary(program, branches, selection) -> tuple[_PlacedBranch, ...]:
    """在较早 branch 位置投影 pair，其他 branch 保持声明序。"""
    positions = {_branch_identity(branch): index for index, branch in enumerate(branches)}
    by_branch = {
        _branch_identity(branch): pair
        for pair in selection.pairs for branch in (pair.trigger, pair.partner)
    }
    placed: list[_PlacedBranch] = []
    consumed: set[tuple[str, str | None]] = set()
    cursor: int | None = None
    session_index = 0
    for branch in branches:
        identity = _branch_identity(branch)
        if identity in consumed:
            continue
        lower = _branch_lower_bound(program, cursor, False)
        pair = by_branch.get(identity)
        if pair is None:
            current = _place_standalone(program, branch, lower, session_index)
        else:
            current = _place_selected_pair(program, pair, lower, session_index)
            current = tuple(sorted(
                current, key=lambda item: positions[_branch_identity(item.logical)]
            ))
            consumed.update((_branch_identity(pair.trigger), _branch_identity(pair.partner)))
        placed.extend(current)
        cursor = max(_events_tail(item.events) for item in current)
        session_index += 1
    expected = len(branches) - len(selection.pairs)
    if session_index != expected:
        _plan_internal("derived primary session count is inconsistent")
    return tuple(placed)


def _place_standalone(program, branch, lower: int, session_index: int):
    """以最早日历可行起点投影一个独立 branch。"""
    start = _earliest_calendar_start(program, branch, lower)
    events = _planned_events(program, branch, start, session_index)
    _check_session(program, branch.slot.slot_key, events, events[0].timestamp_us, len(events))
    return (_PlacedBranch(branch, events, session_index),)


def _place_selected_pair(program, pair, lower: int, session_index: int):
    """仅对已抽中 pair 求解一个真正 owner 交织 session。"""
    identity = f"{pair.layout.pattern_name}:{pair.layout.trigger_slot_key}:{pair.layout.partner_slot_key}"
    request = _InterleavingRequest(
        program, pair, lower,
        _solver_seed(program.planner_seed, f"interleaving-layout:{identity}"),
    )
    trigger_start, partner_start = _solve_interleaved_starts(request)
    trigger_events = _planned_events(program, pair.trigger, trigger_start, session_index)
    partner_events = _planned_events(program, pair.partner, partner_start, session_index)
    events = tuple(sorted((*trigger_events, *partner_events), key=lambda item: item.timestamp_us))
    _check_session(program, pair.trigger.slot.slot_key, events, events[0].timestamp_us, len(events))
    return (
        _PlacedBranch(pair.trigger, trigger_events, session_index),
        _PlacedBranch(pair.partner, partner_events, session_index),
    )


def _solve_interleaved_starts(request: _InterleavingRequest) -> tuple[int, int]:
    """用 CP-SAT 联合冻结 trigger/partner 起点与 witness。"""
    program, pair = request.program, request.pair
    upper = request.lower + 21 * _DAY_US
    trigger_intervals = _branch_start_intervals(program, pair.trigger, request.lower, upper)
    partner_intervals = _branch_start_intervals(program, pair.partner, request.lower, upper)
    if not trigger_intervals or not partner_intervals:
        _plan_infeasible("selected interleaving pair has no calendar layout")
    model = cp_model.CpModel()
    trigger = model.new_int_var_from_domain(
        cp_model.Domain.from_intervals(_millisecond_intervals(trigger_intervals)), "trigger_start"
    )
    partner = model.new_int_var_from_domain(
        cp_model.Domain.from_intervals(_millisecond_intervals(partner_intervals)), "partner_start"
    )
    witness_rank = _constrain_interleaved_session(model, request, trigger, partner)
    if witness_rank is None:
        _plan_infeasible("selected interleaving pair cannot form a true owner interleave")
    first = model.new_int_var(request.lower // _QUANTUM_US,
                              upper // _QUANTUM_US, "session_first")
    model.add_min_equality(first, [trigger, partner])
    objectives = (witness_rank, first, trigger, partner)
    solver = _solve_interleaving_lexicographic(model, request, objectives)
    return solver.value(trigger) * _QUANTUM_US, solver.value(partner) * _QUANTUM_US


def _constrain_interleaved_session(model, request, trigger_start, partner_start):
    """加入起点唯一、容量、资源、跨度与 witness 约束。"""
    trigger_offsets = _logical_offsets(request.pair.trigger)
    partner_offsets = _logical_offsets(request.pair.partner)
    count = len(trigger_offsets) + len(partner_offsets)
    if count > request.program.timeline.session_max_events:
        return None
    for trigger in trigger_offsets:
        for partner in partner_offsets:
            model.add(
                partner_start + partner // _QUANTUM_US
                != trigger_start + trigger // _QUANTUM_US
            )
    _constrain_interleaved_resources(model, request, trigger_start, partner_start)
    _constrain_interleaved_span(model, request, trigger_start, partner_start)
    return _add_alternation_witnesses(model, request, trigger_start, partner_start)


def _constrain_interleaved_span(model, request, trigger_start, partner_start) -> None:
    """限制双 owner session 的完整 interval envelope。"""
    cap = request.program.timeline.session_max_span_us // _QUANTUM_US
    lower = request.lower // _QUANTUM_US
    horizon = lower + 21 * _DAY_US // _QUANTUM_US + cap
    trigger_ends = _branch_end_expressions(
        request.program, request.pair.trigger, trigger_start
    )
    partner_ends = _branch_end_expressions(
        request.program, request.pair.partner, partner_start
    )
    first = model.new_int_var(lower, horizon, "interleaving_first")
    last = model.new_int_var(lower, horizon + cap, "interleaving_last")
    model.add_min_equality(first, [trigger_start, partner_start])
    model.add_max_equality(last, [*trigger_ends, *partner_ends])
    model.add(last - first <= cap)


def _add_alternation_witnesses(model, request, trigger_start, partner_start):
    """构造并唯一选择 seed rank 最小的 A-B-A/B-A-B witness。"""
    ranked = _ranked_witnesses(request)
    if not ranked:
        return None
    rank = model.new_int_var(0, len(ranked) - 1, "witness_rank")
    flags = []
    for value, (owner, gap_index) in enumerate(ranked):
        descriptor = (owner, gap_index, value)
        flag = _witness_flag(model, request, (trigger_start, partner_start), descriptor)
        model.add(rank == value).only_enforce_if(flag)
        flags.append(flag)
    model.add(sum(flags) == 1)
    return rank


def _ranked_witnesses(request):
    """以冻结哈希、owner 序与 gap 序产生唯一 witness rank。"""
    pair = request.pair
    layout = pair.layout
    prefix = [
        request.program.digest, request.program.planner_seed, layout.pattern_name,
        layout.trigger_slot_key, layout.trigger_variant_name,
        layout.partner_slot_key, layout.partner_variant_name,
    ]
    ranked = []
    owners = (("trigger", pair.trigger), ("partner", pair.partner))
    for owner_order, (owner, branch) in enumerate(owners):
        for gap_index in range(len(branch.roles) - 1):
            value = generation_random(
                "interleaving_witness_rank", [*prefix, owner, gap_index]
            )
            ranked.append((value, owner_order, gap_index, owner))
    ranked.sort()
    return tuple((owner, gap_index) for _value, _order, gap_index, owner in ranked)


def _witness_flag(model, request, starts, descriptor):
    """强制另一 owner 首事件严格位于一个 owner gap 中。"""
    trigger_start, partner_start = starts
    owner, gap_index, rank = descriptor
    trigger = tuple(value // _QUANTUM_US for value in _logical_offsets(request.pair.trigger))
    partner = tuple(value // _QUANTUM_US for value in _logical_offsets(request.pair.partner))
    flag = model.new_bool_var(f"{owner}_wraps_{gap_index}_rank_{rank}")
    if owner == "trigger":
        before, after = trigger[gap_index:gap_index + 2]
        model.add(partner_start + partner[0] >= trigger_start + before + 1).only_enforce_if(flag)
        model.add(partner_start + partner[0] <= trigger_start + after - 1).only_enforce_if(flag)
    else:
        before, after = partner[gap_index:gap_index + 2]
        model.add(trigger_start + trigger[0] >= partner_start + before + 1).only_enforce_if(flag)
        model.add(trigger_start + trigger[0] <= partner_start + after - 1).only_enforce_if(flag)
    return flag


def _solve_interleaving_layout(model, request: _InterleavingRequest):
    """求解一层 selected pair 绝对日历优化。"""
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = request.seed
    solver.parameters.max_deterministic_time = 10.0
    status = solver.solve(model)
    if status == cp_model.OPTIMAL:
        return solver
    if status == cp_model.INFEASIBLE:
        _plan_infeasible("selected interleaving pair has no feasible layout")
    if status in (cp_model.FEASIBLE, cp_model.UNKNOWN):
        _plan_budget("interleaving layout did not prove OPTIMAL")
    _plan_internal("interleaving layout model is invalid")


def _solve_interleaving_lexicographic(model, request, objectives):
    """依次冻结 witness rank、session 起点、trigger 与 partner 起点。"""
    solver = None
    for objective in objectives:
        model.minimize(objective)
        solver = _solve_interleaving_layout(model, request)
        model.add(objective == solver.value(objective))
    return solver


def _logical_offsets(branch) -> tuple[int, ...]:
    """返回 branch 相对首事件的逻辑时间。

    @param branch 逻辑 branch。
    @return 从零开始的时间 offset。
    """
    first = branch.logical_times[0]
    return tuple(value - first for value in branch.logical_times)


def _branch_metadata(program, branch):
    """返回 branch 声明序的 offset、duration 与 resources。"""
    offsets = _logical_offsets(branch)
    if program.mode == "instruction_only":
        return tuple((role, offset, 0, ()) for role, offset in zip(branch.roles, offsets))
    pattern = program.patterns[branch.slot.pattern_name]
    roles = {item.name: item for item in pattern.roles}
    return tuple(
        (role, offset, program.frame_classes[roles[role].frame_class].duration_us,
         program.frame_classes[roles[role].frame_class].resources)
        for role, offset in zip(branch.roles, offsets)
    )


def _branch_end_expressions(program, branch, start):
    """构造毫秒绝对 branch 的全部 end 表达式。"""
    return tuple(
        start + offset // _QUANTUM_US + duration // _QUANTUM_US
        for _role, offset, duration, _resources in _branch_metadata(program, branch)
    )


def _constrain_interleaved_resources(model, request, trigger_start, partner_start) -> None:
    """对交织 owner 合并同名 resource 并加入 AddNoOverlap。"""
    by_resource: dict[str, list] = {}
    owners = (
        ("trigger", request.pair.trigger, trigger_start),
        ("partner", request.pair.partner, partner_start),
    )
    for owner, branch, start in owners:
        for role, offset, duration, resources in _branch_metadata(request.program, branch):
            if duration <= 0:
                continue
            interval = model.new_fixed_size_interval_var(
                start + offset // _QUANTUM_US, duration // _QUANTUM_US,
                f"interleaving_{owner}_{role}_interval",
            )
            for resource in resources:
                by_resource.setdefault(resource, []).append(interval)
    for intervals in by_resource.values():
        model.add_no_overlap(intervals)


def _millisecond_intervals(intervals) -> list[list[int]]:
    """把已毫秒对齐的微秒闭区间转为 CP-SAT 毫秒域。"""
    result = []
    for start, end in intervals:
        aligned_start = (start + _QUANTUM_US - 1) // _QUANTUM_US
        aligned_end = end // _QUANTUM_US
        if aligned_start <= aligned_end:
            result.append([aligned_start, aligned_end])
    return result


def _branch_lower_bound(program, cursor: int | None, shares: bool) -> int:
    """计算下一个 branch 的最早工件起点。

    @param program 冻结生成程序。
    @param cursor 前缀事件的最大区间尾。
    @param shares 是否与前一 branch 共 session。
    @return 严格递增的最早起点。
    """
    if cursor is None:
        return _align_millisecond(program.timeline.timestamp_start_us)
    gap = (program.timeline.event_gap_us[0] if shares
           else program.timeline.session_gap_us)
    return _align_millisecond(cursor + max(gap, _QUANTUM_US))


def _align_millisecond(value: int) -> int:
    """向上量化 epoch 微秒到 Planner 毫秒 quantum。"""
    return ((value + _QUANTUM_US - 1) // _QUANTUM_US) * _QUANTUM_US


def _events_tail(events) -> int:
    """按区间 end 与点事件单 quantum 返回最大尾。"""
    return max(
        event.timestamp_us + max(event.duration_us, _QUANTUM_US)
        for event in events
    )


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
    metadata = _branch_metadata(program, branch)
    for position, ((role, logical_time), temporal) in enumerate(
        zip(zip(branch.roles, branch.logical_times), metadata)
    ):
        _name, _offset, duration_us, resources = temporal
        event_key = _event_key(program, branch.slot, scenario_id, role, position)
        events.append(PlannedEvent(
            event_key=event_key,
            role=role,
            position=position,
            logical_time_us=logical_time,
            timestamp_us=start + logical_time - first,
            duration_us=duration_us,
            resources=resources,
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
        return _align_millisecond(lower)
    pattern = program.patterns[branch.slot.pattern_name]
    roles = {role.name: role for role in pattern.roles}
    constrained = [
        (offset, duration, roles[name].calendar_window)
        for name, offset, duration, _resources in _branch_metadata(program, branch)
        if roles[name].calendar_window is not None
    ]
    if not constrained:
        return _align_millisecond(lower)
    intervals = [(_align_millisecond(lower), lower + 21 * _DAY_US)]
    for offset, duration, window_name in constrained:
        allowed = _calendar_start_intervals(
            program.calendar_windows[window_name], offset, duration, lower,
        )
        intervals = _intersect_intervals(intervals, allowed)
    if not intervals:
        _plan_infeasible("primary branch has no calendar layout")
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
        return [(_align_millisecond(lower), upper)]
    pattern = program.patterns[branch.slot.pattern_name]
    roles = {role.name: role for role in pattern.roles}
    for name, offset, duration, _resources in _branch_metadata(program, branch):
        window_name = roles[name].calendar_window
        if window_name is None:
            continue
        allowed = _calendar_start_intervals(
            program.calendar_windows[window_name], offset, duration, lower,
        )
        intervals = _intersect_intervals(intervals, allowed)
    return [(start, end) for start, end in intervals if start <= upper and end >= lower]


def _calendar_start_intervals(window, logical_offset: int, duration_us: int,
                              lower: int):
    """展开 21 天内一个 role window 的可用 branch-start 区间。

    @param window 命名 calendar window。
    @param logical_offset role 相对 branch 起点偏移。
    @param duration_us role 固定时长。
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
    occupied = max(duration_us, _QUANTUM_US)
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
            (base + start - logical_offset, base + end - logical_offset - occupied)
            for start, end in window.intervals_us
        )
    return intervals


def _validate_timestamp_range(program, placed, noise_slots, replay_layouts) -> None:
    """在计划交付前验证全部工件时间可精确渲染为 ISO8601。"""
    timestamps = []
    for branch in placed:
        for event in branch.events:
            timestamps.extend((event.timestamp_us, event.timestamp_us + event.duration_us))
    timestamps.extend(item.timestamp_us for item in noise_slots)
    sources = {
        (item.logical.slot.slot_key, item.logical.variant_name): item
        for item in placed
    }
    for layout in replay_layouts:
        source = sources[(layout.source_slot_key, layout.source_variant_name)]
        for event in source.events:
            start = event.timestamp_us + layout.shift_us
            timestamps.extend((start, start + event.duration_us))
    zone = timezone(timedelta(minutes=program.timeline.utc_offset_minutes))
    try:
        for value in timestamps:
            (_EPOCH_UTC + timedelta(microseconds=value)).astimezone(zone)
    except (OverflowError, ValueError):
        _plan_infeasible("planned timestamp exceeds supported datetime range")


def _preflight_temporal_values(program, plan: ScenarioPlan) -> None:
    """在任何 LLM 调用前验证全部机械时间叶值。"""
    slots = {item.slot_key: item for item in plan.delivery_slots}
    groups = {
        key: events for block in plan.blocks for key, events in block.items()
    }
    for (slot_key, _variant), events in groups.items():
        slot = slots[slot_key]
        for event in events:
            for frame_name in _event_frame_names(program, slot, event):
                _preflight_frame_event(program, frame_name, event.timestamp_us,
                                       event.duration_us, event.resources)
    for noise in plan.noise_slots:
        _preflight_frame_event(
            program, noise.frame_class, noise.timestamp_us, noise.duration_us,
            noise.resources,
        )
    _preflight_replays(program, plan, groups)
    _preflight_annotations(program, plan, groups)


def _event_frame_names(program, slot, event) -> tuple[str, ...]:
    """返回计划事件在内容生成时可用的帧类闭集。"""
    if program.mode == "declared":
        pattern = program.patterns[slot.pattern_name]
        role = next(item for item in pattern.roles if item.name == event.role)
        return (role.frame_class,)
    noise_name = None if program.noise is None else program.noise.frame_class
    return tuple(
        name for name, frame in program.frame_classes.items()
        if name != noise_name and is_generation_frame_eligible(frame)
    )


def _preflight_frame_event(program, frame_name: str, timestamp_us: int,
                           duration_us: int, resources) -> None:
    """验证一个帧类在固定计划时间上的全部 binding 叶子。"""
    frame = program.frame_classes[frame_name]
    if frame.duration_us != duration_us or frame.resources != tuple(resources):
        _plan_infeasible("planned event interval differs from frame declaration")
    try:
        values = resolve_frame_time_values(
            frame.time_bindings, timestamp_us, duration_us,
            program.timeline.utc_offset_minutes,
        )
    except ValueError:
        _plan_infeasible("planned frame time binding cannot be resolved")
    _validate_mechanical_leaf_values(frame.gen_schema, values)


def _preflight_replays(program, plan, groups) -> None:
    """在平移后的 replay 起点上重新验证 frame 时间叶子。"""
    slots = {item.slot_key: item for item in plan.delivery_slots}
    for layout in plan.replay_layouts:
        key = (layout.source_slot_key, layout.source_variant_name)
        source = groups[key]
        slot = slots[layout.source_slot_key]
        for event in source:
            frame_name = _event_frame_names(program, slot, event)[0]
            _preflight_frame_event(
                program, frame_name, event.timestamp_us + layout.shift_us,
                event.duration_us, event.resources,
            )


def _preflight_annotations(program, plan, groups) -> None:
    """验证每个可交付 branch 的 annotation 资源起点叶值。"""
    for slot in plan.delivery_slots:
        view = program.class_views[slot.sequence_class]
        if not view.time_bindings:
            continue
        for variant in slot.variant_names:
            events = groups[(slot.slot_key, variant)]
            values = {}
            for binding in view.time_bindings:
                starts = [event.timestamp_us for event in events
                          if event.duration_us > 0 and binding.resource in event.resources]
                if not starts:
                    _plan_infeasible("annotation resource is absent from a deliverable branch")
                values[binding.payload_path] = min(starts) // _QUANTUM_US
            _validate_mechanical_leaf_values(view.schema, values)


def _validate_mechanical_leaf_values(schema, values) -> None:
    """用完整 Schema 中的原始叶子约束验证机械值。"""
    if not values:
        return
    if schema is None:
        _plan_infeasible("temporal binding requires a full Schema")
    for path, value in values.items():
        leaf = schema
        for token in JsonPointer(path).parts:
            properties = leaf.get("properties") if isinstance(leaf, dict) else None
            if properties is None and hasattr(leaf, "get"):
                properties = leaf.get("properties")
            if not hasattr(properties, "get"):
                _plan_infeasible("temporal binding leaf is absent from full Schema")
            leaf = properties.get(token)
        if not hasattr(leaf, "items") or tuple(Draft202012Validator(leaf).iter_errors(value)):
            _plan_infeasible("planned mechanical value violates its full Schema leaf")


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
        _plan_infeasible("primary session exceeds event capacity")
    last = max(event.timestamp_us + event.duration_us for event in events)
    first = min(event.timestamp_us for event in events)
    if last - first > program.timeline.session_max_span_us:
        _plan_infeasible("primary session exceeds span capacity")


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
    events = _planned_events(program, baseline, start, 0)
    return tuple(dataclasses.replace(event, session_id=session) for event in events)


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
        timestamp, cursor, session_index, session_start, session_count = _next_noise_time(
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
            timestamp_us=timestamp,
            duration_us=0,
            resources=(),
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
    @return timestamp、新 tail、session 序号、起点与计数。
    """
    gap = max(program.timeline.event_gap_us[0], _QUANTUM_US)
    timestamp = cursor + max(gap - _QUANTUM_US, 0)
    if session_start is None:
        timestamp = cursor + max(program.timeline.session_gap_us, _QUANTUM_US)
        session_start = timestamp
    exceeds = (session_count >= program.timeline.session_max_events
               or timestamp - session_start > program.timeline.session_max_span_us)
    if exceeds:
        session_index += 1
        timestamp = cursor + max(program.timeline.session_gap_us, _QUANTUM_US)
        session_start, session_count = timestamp, 0
    return timestamp, timestamp + _QUANTUM_US, session_index, session_start, session_count + 1


def _replay_layouts(program, logical, placed, cursor: int) -> tuple[ReplayLayout, ...]:
    """从 declaration-order positive source 冻结完整 replay layouts。

    @param program 冻结生成程序。
    @param logical 原始 declaration-order primary branches。
    @param placed 已放置 primary branches。
    @param cursor primary/noise 最后时间。
    @return replay layouts。
    """
    placed_by_identity = {
        _branch_identity(item.logical): item
        for item in placed
    }
    positives = [
        placed_by_identity[_branch_identity(branch)]
        for branch in logical
        if _is_positive(program, branch)
    ]
    layouts = []
    for ordinal, source in enumerate(positives[:program.timeline.duplicate_sequences]):
        lower = cursor + max(program.timeline.session_gap_us, _QUANTUM_US)
        start = _earliest_calendar_start(program, source.logical, lower)
        shift = start - source.events[0].timestamp_us
        if shift <= 0 or shift % _QUANTUM_US:
            _plan_infeasible("replay shift must be positive and millisecond aligned")
        replay_events = tuple(
            dataclasses.replace(
                event,
                timestamp_us=event.timestamp_us + shift,
                session_id=f"replay_{ordinal:06d}",
            )
            for event in source.events
        )
        _check_session(
            program, source.logical.slot.slot_key, replay_events,
            replay_events[0].timestamp_us, len(replay_events),
        )
        layouts.append(ReplayLayout(
            source_slot_key=source.logical.slot.slot_key,
            source_variant_name=source.logical.variant_name,
            replay_ordinal=ordinal,
            session_id=f"replay_{ordinal:06d}",
            shift_us=shift,
        ))
        cursor = _events_tail(replay_events)
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


def _solve_lexicographic(model, seed: int, identity: str, objectives):
    """按声明序逐项冻结确定性最优值。"""
    solver = None
    for objective in objectives:
        model.minimize(objective)
        solver = _solve(model, seed, identity)
        model.add(objective == solver.value(objective))
    if solver is None:
        _plan_internal("lexicographic objective list is empty")
    return solver


def _solver_seed(seed: int, identity: str) -> int:
    """为一个 solver layer 派生稳定 31-bit seed。

    @param seed 运行随机种子。
    @param identity layer 身份。
    @return OR-Tools 接受的非负整数 seed。
    """
    return int(generation_digest("planner_seed", [seed, identity])[:8], 16) & 0x7fffffff


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

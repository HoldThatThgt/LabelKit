"""v1.17 compile_scenario：quota 见证模型 + ScenarioPlanner 时间轴模型（SPEC-SP §6/§7/§8）。

§6.2 编译顺序冻结：静态检查 → derive_stream_bounds → quota solve → slot specs →
timeline model → lexicographic solve → noise allocate → ScenarioPlan+digest。
§7 编码要点（以 /tmp/v117 探针为准）：length 冻结为 length_target（无变长/prefix
机制），全部帧是 always-present 1µs interval 进全局 ``AddNoOverlap``；frame gap
按闭区间量化 µs 双侧约束；slot anchor local date one-hot 承载 §7.5 quota bucket
汇总（同一 slot 可同时贡献 day/week/schedule quota）；duplicate 平移每帧
channeling IntVar、窗口/排程/唯一性按新时间重执行、不参与 quota/rule/noise-ratio；
三层目标（偏好偏差 [Wave 4a 只有 length 项且恒 0，Wave 5 加入 duration 偏差项]
→ calendar days spanned → timeline end）每层独立 solve 必须 OPTIMAL 并固化等式。
§8.2 family stats 按 builder 前后 proto 差分记录，quota/timeline 两模型各自
250k entry 上限独立判定；§8.4 quota/frame window 各建具名 assumption，
INFEASIBLE 时 core 具名集合进 PlannerInfeasibleError。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import date
from random import Random
from typing import Any

from ortools.sat.python import cp_model

from labelkit.common.config.model import apportion_tiers
from labelkit.common.runtime.scenario.calendar import (
    day_bounds_us,
    day_segment,
    expand_period_buckets,
    legal_dates,
    local_date,
    local_date_span,
)
from labelkit.common.runtime.scenario.diagnostics import (
    FamilyRecorder,
    PlannerBudgetError,
    PlannerInfeasibleError,
    PlannerInternalError,
    derive_stream_bounds,
    enforce_model_capacity,
    format_budget_message,
    format_infeasible_message,
    infeasible_core_names,
    make_planner_solver,
)
from labelkit.common.runtime.scenario.model import (
    DuplicateLayout,
    FrameClassDomain,
    FrameLayout,
    PlannerModelStats,
    PlannerObjectives,
    ScenarioConfig,
    ScenarioPlan,
    SequenceLayout,
    SequenceSlotSpec,
    SessionLayout,
)
from labelkit.common.runtime.scenario.noise import (
    NoiseAllocationSpec,
    NoiseSessionSpan,
    NoiseSlot,
    allocate_noise,
)
from labelkit.common.runtime.scenario.quota import (
    QuotaSolution,
    half_even_noise_target,
    quota_bucket_values,
    solve_quota_targets,
)
from labelkit.common.runtime.scenario.sessions import (
    NoiseReserve,
    NoiseReserveSpec,
    SessionBuildSpec,
    SessionLayer,
    SlotTimeline,
    build_crossing_witness,
    build_noise_reserve,
    build_session_layer,
)

_US_PER_DAY = 86_400_000_000

#: timeline 模型的全部约束族（§8.2；Wave 5 族以 0/0 占位在场）。
_TIMELINE_FAMILIES = ("frame_domain", "frame_rule", "frame_window", "session_slot",
                      "crossing", "quota_period", "sequence_rule", "resource",
                      "noise_reserve", "objective")


@dataclass
class _Timeline:
    """时间轴模型构建上下文（各 builder 共享的可变载体）。"""

    model: cp_model.CpModel
    config: ScenarioConfig
    slots: tuple[SequenceSlotSpec, ...]
    recorder: FamilyRecorder
    frame_names: tuple[str, ...]
    frame_domains: dict[str, FrameClassDomain]
    frame_index: dict[str, int]
    slot_domains: dict[str, tuple[int, ...]]
    slot_tiers: dict[str, Any]
    legal_days: tuple[date, ...]
    day_segments: tuple[tuple[int, int], ...]
    full_cover: bool
    dup_sources: tuple[int, ...]
    literal_names: dict[int, str] = field(default_factory=dict)
    grid_ts: list[list[Any]] = field(default_factory=list)
    grid_words: list[list[Any]] = field(default_factory=list)
    grid_durations: list[list[Any]] = field(default_factory=list)
    grid_ends: list[list[Any]] = field(default_factory=list)
    duration_targets: dict[tuple[str, int, str], int] = field(default_factory=dict)
    anchors: list[list[Any]] = field(default_factory=list)
    dup_offsets: list[Any] = field(default_factory=list)
    dup_ts: list[list[Any]] = field(default_factory=list)
    dup_ends: list[list[Any]] = field(default_factory=list)
    layer: SessionLayer | None = None
    reserve: NoiseReserve | None = None
    preference_terms: list[Any] = field(default_factory=list)
    solver: cp_model.CpSolver | None = None


@dataclass(frozen=True)
class _WindowContext:
    """一条 frame window 的模型上下文（绝对区段/类索引/assumption literal）。"""

    segments: tuple[tuple[int, int], ...]
    class_index: int
    literal: Any
    name: str


# ------------------------------------------------------ 唯一入口 ----

def compile_scenario(config: ScenarioConfig) -> ScenarioPlan:
    """编译 quota、求解时间布局并冻结 noise，不做网络调用（SPEC-SP §6.2）。

    执行顺序冻结：静态检查 → derive_stream_bounds → quota solve → slot specs →
    timeline model → lexicographic solve → noise allocate → ScenarioPlan+digest。
    静态派生错误以分号聚合成单条 PlannerInfeasibleError（§8.4：静态 arithmetic
    错误优先于 solver core）。

    @param config 冻结的 ``ScenarioConfig``
    @return ``ScenarioPlan``（models 含 quota/timeline 两模型规模统计）
    @raises PlannerInfeasibleError 派生检查失败或两模型任一证得 INFEASIBLE 时
    @raises PlannerCapacityError 任一模型超 250k entry 时
    @raises PlannerBudgetError 任一求解层非 OPTIMAL 时
    @raises PlannerInternalError 解码违反结构不变量时
    """
    errors = derive_stream_bounds(config)
    if errors:
        raise PlannerInfeasibleError("; ".join(errors))
    quota = solve_quota_targets(config.quotas, config.schedule, config.seed)
    targets = dict(quota.class_targets)
    for spec in config.sequence_classes:
        targets.setdefault(spec.name, 0)
    slots = _build_slot_specs(config, targets)
    return _plan_timeline(config, slots, quota)


def _plan_timeline(config: ScenarioConfig, slots: tuple[SequenceSlotSpec, ...],
                   quota: QuotaSolution) -> ScenarioPlan:
    """时间轴模型构建 → 三层字典序求解 → noise 分配 → 冻结计划与 digest。"""
    tl = _make_timeline(config, slots)
    _build_frame_domain(tl)
    _build_resources(tl)
    _build_quota_anchors(tl)
    _build_sequence_rules(tl)
    _build_frame_windows(tl)
    _build_session_and_crossing(tl)
    _build_noise_target(tl)
    _build_objectives(tl)
    enforce_model_capacity("timeline", tl.recorder)
    objectives = _lex_solve(tl)
    layouts, sessions = _decode_sequences(tl)
    noise_slots = _allocate_noise(tl, sessions, layouts)
    duplicates = _decode_duplicates(tl)
    variables, constraints = tl.recorder.totals()
    stats = PlannerModelStats(variables, constraints, tl.recorder.stats())
    plan = ScenarioPlan(
        slots=slots, layouts=layouts, sessions=sessions, noise_slots=noise_slots,
        duplicates=duplicates, quota_summary=quota.summary, objectives=objectives,
        models={"quota": quota.stats, "timeline": stats}, plan_digest="")
    return replace(plan, plan_digest=_plan_digest(plan))


# ------------------------------------------------------ slot 构建 ----

@dataclass(frozen=True)
class _ApportionTier:
    """``apportion_tiers`` 复用适配行：``TierDomain.rank`` → ``tier_rank``。"""

    weight: int
    tier_rank: int


def _build_slot_specs(config: ScenarioConfig,
                      targets: dict[str, int]) -> tuple[SequenceSlotSpec, ...]:
    """§6.2 slot 构建：声明序 × 类内 ordinal、tier 配分与 seeded length target。

    tier 分配复用 v1.15 的纯整数最大余额 ``apportion_tiers``（零 rng），类内
    ordinal 按 rank 升序占据连续分块；length target 从
    ``Random(f"{seed}:scenario.preference")`` 按 len_range 抽整数——种子流消费
    顺序冻结为 slot 声明序（测试钉死）。

    @param config 冻结配置
    @param targets 类名 → 配额 target（quota 模型解码）
    @return ``SequenceSlotSpec`` 元组
    """
    rng = Random(f"{config.seed}:scenario.preference")
    slots: list[SequenceSlotSpec] = []
    for spec in config.sequence_classes:
        target = targets.get(spec.name, 0)
        if target <= 0:
            continue
        tiers = tuple(sorted(spec.tiers, key=lambda tier: tier.rank))
        shares = apportion_tiers(target, tuple(
            _ApportionTier(tier.weight, tier.rank) for tier in tiers))
        for ordinal in range(target):
            slots.append(SequenceSlotSpec(
                key=f"sequence:{spec.name}:{ordinal}",
                sequence_class=spec.name, class_ordinal=ordinal,
                tier_rank=_tier_rank_for_ordinal(ordinal, tiers, shares),
                length_target=rng.randint(*spec.length_range),
                length_range=spec.length_range))
    return tuple(slots)


def _tier_rank_for_ordinal(ordinal: int, tiers: tuple[Any, ...],
                           shares: tuple[int, ...]) -> int | None:
    """类内 ordinal → tier rank（rank 升序连续分块；无 tier 表时 None）。

    @param ordinal 类内 0 基序数
    @param tiers rank 升序 tier 表
    @param shares ``apportion_tiers`` 的逐档配分
    @return tier rank 或 ``None``
    @raises PlannerInternalError 配分未覆盖序数时
    """
    if not tiers:
        return None
    cursor = 0
    for tier, share in zip(tiers, shares, strict=True):
        if cursor + share > ordinal:
            return tier.rank
        cursor += share
    raise PlannerInternalError("tier apportionment does not cover class ordinal")


def _select_duplicate_sources(seed: int, count: int,
                              slot_count: int) -> tuple[int, ...]:
    """§7.7：artifact.duplicate 流在建模前按 class/ordinal 稳定选 source。

    @param seed run seed
    @param duplicates 声明条数
    @param slot_count 全部 sequence slot 数
    @return source slot 下标元组（无放回、plan 序稳定）
    """
    rng = Random(f"{seed}:artifact.duplicate")
    return tuple(rng.sample(range(slot_count), count))


# ------------------------------------------------------ 构建上下文 ----

def _make_timeline(config: ScenarioConfig,
                   slots: tuple[SequenceSlotSpec, ...]) -> _Timeline:
    """组装构建上下文：任务帧类域、slot 词域、合法日段与 duplicate source。"""
    schedule = config.schedule
    noise_names = {spec.frame_class for spec in config.noise_classes}
    frame_names = tuple(spec.name for spec in config.frame_classes
                        if spec.name not in noise_names)
    frame_index = {name: position for position, name in enumerate(frame_names)}
    legal = legal_dates(schedule, ())
    segments = tuple(day_segment(schedule.start_us, schedule.end_us, day,
                                 schedule.utc_offset_minutes)
                     for day in legal)
    first, last = local_date_span(schedule)
    full_cover = not any(first <= date.fromisoformat(text) <= last
                         for text in schedule.exclude_dates)
    domains, tiers = _slot_word_domains(config, slots, frame_index)
    duration_targets = _duration_targets(config, slots)
    model = cp_model.CpModel()
    return _Timeline(
        model=model, config=config, slots=slots,
        recorder=FamilyRecorder(model, _TIMELINE_FAMILIES),
        frame_names=frame_names,
        frame_domains={spec.name: spec for spec in config.frame_classes},
        frame_index=frame_index, slot_domains=domains, slot_tiers=tiers,
        duration_targets=duration_targets,
        legal_days=legal, day_segments=segments, full_cover=full_cover,
        dup_sources=_select_duplicate_sources(config.seed, config.duplicates,
                                              len(slots)))


def _slot_word_domains(config: ScenarioConfig,
                       slots: tuple[SequenceSlotSpec, ...],
                       frame_index: dict[str, int]
                       ) -> tuple[dict[str, tuple[int, ...]], dict[str, Any]]:
    """逐 slot 的 word 域（tier 构成或任务帧类全集）与 tier 归属表。

    @param config 冻结配置
    @param slots 全部 sequence slot
    @param frame_index 任务帧类名 → 词下标
    @return ``(slot key → 词下标域, slot key → TierDomain|None)``
    """
    by_class = {spec.name: spec for spec in config.sequence_classes}
    domains: dict[str, tuple[int, ...]] = {}
    tiers: dict[str, Any] = {}
    for slot in slots:
        domain_spec = by_class[slot.sequence_class]
        tier = None
        if slot.tier_rank is not None:
            tier = next(item for item in domain_spec.tiers
                        if item.rank == slot.tier_rank)
        tiers[slot.key] = tier
        if tier is None:
            domains[slot.key] = tuple(range(len(frame_index)))
        else:
            domains[slot.key] = tuple(frame_index[name]
                                      for name in tier.frame_classes)
    return domains, tiers


def _duration_targets(config: ScenarioConfig,
                      slots: tuple[SequenceSlotSpec, ...]) -> dict[tuple[str, int, str], int]:
    """按稳定 slot/position/candidate-class 顺序冻结所有 duration preference 抽签。"""
    rng = Random(f"{config.seed}:scenario.preference")
    for spec in config.sequence_classes:
        target = sum(1 for slot in slots if slot.sequence_class == spec.name)
        for _ in range(target):
            rng.randint(*spec.length_range)
    targets: dict[tuple[str, int, str], int] = {}
    duration_domains = tuple(spec for spec in config.frame_classes
                             if spec.duration_us is not None)
    for slot in slots:
        for position in range(slot.length_target):
            for domain in duration_domains:
                targets[(slot.key, position, domain.name)] = rng.randint(
                    *domain.duration_us)
    return targets


def _build_frame_domain(tl: _Timeline) -> None:
    """§7.4 + 帧域：ts/word 变量、gap 链、tier 构成、日合法性与全局唯一。"""
    with tl.recorder.family("frame_domain"):
        _build_frame_vars(tl)
        _build_duplicate_channeling(tl)
        _build_uniqueness(tl)


def _build_resources(tl: _Timeline) -> None:
    """为每个声明 resource 的 duration 候选建立同资源 optional interval 互斥。"""
    with tl.recorder.family("resource"):
        by_resource: dict[str, list[Any]] = {}
        for slot_index, spec in enumerate(tl.slots):
            for position, (start, duration, end, word) in enumerate(zip(
                    tl.grid_ts[slot_index], tl.grid_durations[slot_index],
                    tl.grid_ends[slot_index], tl.grid_words[slot_index], strict=True)):
                _resource_intervals(tl, by_resource, spec.key, position,
                                    start, duration, end, word, "task")
        for duplicate_index, source in enumerate(tl.dup_sources):
            spec = tl.slots[source]
            for position, (start, duration, end, word) in enumerate(zip(
                    tl.dup_ts[duplicate_index], tl.grid_durations[source],
                    tl.dup_ends[duplicate_index], tl.grid_words[source], strict=True)):
                _resource_intervals(tl, by_resource, spec.key, position,
                                    start, duration, end, word, f"dup{duplicate_index}")
        for intervals in by_resource.values():
            tl.model.AddNoOverlap(intervals)


def _resource_intervals(tl: _Timeline, by_resource: dict[str, list[Any]],
                        slot_key: str, position: int, start: Any, duration: Any,
                        end: Any, word: Any, tag: str) -> None:
    """把一个 frame 的每个候选 duration class 加入其 resource optional interval。"""
    for name, domain in tl.frame_domains.items():
        if not domain.resources:
            continue
        index = tl.frame_index.get(name)
        if index is None or index not in tl.slot_domains[slot_key]:
            continue
        present = tl.model.NewBoolVar(f"resource_match_{tag}_{slot_key}_{position}_{name}")
        tl.model.Add(word == index).OnlyEnforceIf(present)
        tl.model.Add(word != index).OnlyEnforceIf(present.Not())
        interval = tl.model.NewOptionalIntervalVar(
            start, duration, end, present, f"resource_iv_{tag}_{slot_key}_{position}_{name}")
        for resource in domain.resources:
            by_resource.setdefault(resource, []).append(interval)



def _build_frame_vars(tl: _Timeline) -> None:
    """每 (slot, position) 的 start/end/word 变量、duration 域与 frame gap 链。"""
    cfg, model = tl.config, tl.model
    lo_us, hi_us = cfg.schedule.start_us, cfg.schedule.end_us - 1
    gap_lo, gap_hi = cfg.frame_gap_us
    max_duration = max((domain.duration_us[1] for domain in tl.frame_domains.values()
                        if domain.duration_us is not None), default=0)
    for spec in tl.slots:
        domain = tl.slot_domains[spec.key]
        row_ts = [model.NewIntVar(lo_us, hi_us, f"ts_{spec.key}_{p}")
                  for p in range(spec.length_target)]
        row_word = [model.NewIntVarFromDomain(
            cp_model.Domain.FromValues(domain), f"word_{spec.key}_{p}")
            for p in range(spec.length_target)]
        row_duration = [model.NewIntVar(0, max_duration, f"duration_{spec.key}_{p}")
                        for p in range(spec.length_target)]
        row_end = [model.NewIntVar(lo_us, cfg.schedule.end_us, f"end_{spec.key}_{p}")
                   for p in range(spec.length_target)]
        for p, (word, start, duration, end) in enumerate(zip(
                row_word, row_ts, row_duration, row_end, strict=True)):
            model.Add(end == start + duration)
            model.Add(end <= cfg.schedule.end_us)
            for name, class_domain in tl.frame_domains.items():
                index = tl.frame_index.get(name)
                if index is None or index not in domain:
                    continue
                match = model.NewBoolVar(f"duration_match_{spec.key}_{p}_{name}")
                model.Add(word == index).OnlyEnforceIf(match)
                model.Add(word != index).OnlyEnforceIf(match.Not())
                if class_domain.duration_us is None:
                    model.Add(duration == 0).OnlyEnforceIf(match)
                    continue
                duration_lo, duration_hi = class_domain.duration_us
                model.Add(duration >= duration_lo).OnlyEnforceIf(match)
                model.Add(duration <= duration_hi).OnlyEnforceIf(match)
                target = tl.duration_targets[(spec.key, p, name)]
                deviation = model.NewIntVar(0, duration_hi - duration_lo,
                                            f"duration_deviation_{spec.key}_{p}_{name}")
                model.AddAbsEquality(deviation, duration - target)
                selected = model.NewIntVar(0, duration_hi - duration_lo,
                                           f"duration_selected_deviation_{spec.key}_{p}_{name}")
                model.Add(selected == deviation).OnlyEnforceIf(match)
                model.Add(selected == 0).OnlyEnforceIf(match.Not())
                tl.preference_terms.append(selected)
        for p in range(spec.length_target - 1):
            model.Add(row_ts[p + 1] >= row_ts[p] + gap_lo)
            model.Add(row_ts[p + 1] <= row_ts[p] + gap_hi)
        tl.grid_ts.append(row_ts)
        tl.grid_words.append(row_word)
        tl.grid_durations.append(row_duration)
        tl.grid_ends.append(row_end)
        _tier_cover(tl, len(tl.grid_ts) - 1, spec.key)
        _row_day_legality(tl, row_ts, spec.key)


def _tier_cover(tl: _Timeline, position: int, slot_key: str) -> None:
    """v1.14 精确构成：档内每个帧类在该 slot 的 word 中至少出现一次。"""
    tier = tl.slot_tiers[slot_key]
    if tier is None:
        return
    model = tl.model
    for name in tier.frame_classes:
        index = tl.frame_index[name]
        flags = []
        for p, word in enumerate(tl.grid_words[position]):
            flag = model.NewBoolVar(f"cover_{slot_key}_{name}_{p}")
            model.Add(word == index).OnlyEnforceIf(flag)
            model.Add(word != index).OnlyEnforceIf(flag.Not())
            flags.append(flag)
        model.AddAtLeastOne(flags)


def _row_day_legality(tl: _Timeline, row_ts: list[Any], key: str) -> None:
    """帧时间戳不得占用排除日（§4.3；无排除日时 ts 域已蕴含，跳过）。"""
    if tl.full_cover:
        return
    model = tl.model
    for p, ts_var in enumerate(row_ts):
        flags = [model.NewBoolVar(f"legal_{key}_{p}_{j}")
                 for j in range(len(tl.day_segments))]
        model.AddExactlyOne(flags)
        for flag, (lo, hi) in zip(flags, tl.day_segments, strict=True):
            model.Add(ts_var >= lo).OnlyEnforceIf(flag)
            model.Add(ts_var <= hi - 1).OnlyEnforceIf(flag)


def _build_duplicate_channeling(tl: _Timeline) -> None:
    """§7.7：正 offset var + 每帧 channeling IntVar（interval 不接受双变量和）。"""
    cfg, model = tl.config, tl.model
    lo_us, hi_us = cfg.schedule.start_us, cfg.schedule.end_us - 1
    for j, source in enumerate(tl.dup_sources):
        offset = model.NewIntVar(1, hi_us - lo_us, f"dup_offset_{j}")
        tl.dup_offsets.append(offset)
        row = []
        ends = []
        for p, (src_ts, src_end) in enumerate(zip(tl.grid_ts[source],
                                                  tl.grid_ends[source], strict=True)):
            shifted = model.NewIntVar(lo_us, hi_us, f"dup_ts_{j}_{p}")
            shifted_end = model.NewIntVar(lo_us, cfg.schedule.end_us,
                                          f"dup_end_{j}_{p}")
            model.Add(shifted == src_ts + offset)
            model.Add(shifted_end == src_end + offset)
            row.append(shifted)
            ends.append(shifted_end)
        tl.dup_ts.append(row)
        tl.dup_ends.append(ends)
        _row_day_legality(tl, row, f"dup{j}")


def _build_uniqueness(tl: _Timeline) -> None:
    """§7.4：全部 task 与 duplicate 帧的 always-present 1µs start 唯一。"""
    model = tl.model
    rows = list(tl.grid_ts) + list(tl.dup_ts)
    intervals = [model.NewIntervalVar(ts, 1, ts + 1, f"iv_{i}_{p}")
                 for i, row in enumerate(rows) for p, ts in enumerate(row)]
    model.AddNoOverlap(intervals)


# ------------------------------------------------------ quota 锚点 ----

def _build_quota_anchors(tl: _Timeline) -> None:
    """§7.5：anchor one-hot + 每 quota 的 bucket equality（具名 assumption）。"""
    with tl.recorder.family("quota_period"):
        _build_anchor_selectors(tl)
        _build_quota_equalities(tl)


def _build_anchor_selectors(tl: _Timeline) -> None:
    """每 slot 的 anchor local date one-hot（sequence_start 所在日，合法日枚举）。"""
    model = tl.model
    for position, spec in enumerate(tl.slots):
        flags = [model.NewBoolVar(f"anchor_{spec.key}_{day.isoformat()}")
                 for day in tl.legal_days]
        tl.anchors.append(flags)
        model.AddExactlyOne(flags)
        first = tl.grid_ts[position][0]
        for flag, (lo, hi) in zip(flags, tl.day_segments, strict=True):
            model.Add(first >= lo).OnlyEnforceIf(flag)
            model.Add(first <= hi - 1).OnlyEnforceIf(flag)


def _build_quota_equalities(tl: _Timeline) -> None:
    """每张 quota 一个 literal；bucket equality 按 sequence class 汇总 date selector。"""
    model, cfg = tl.model, tl.config
    day_pos = {day: j for j, day in enumerate(tl.legal_days)}
    for quota in cfg.quotas:
        literal = model.NewBoolVar(f"quota_{quota.name}")
        model.AddAssumption(literal)
        tl.literal_names[literal.Index()] = quota.name
        values = quota_bucket_values(quota)
        buckets = expand_period_buckets(cfg.schedule, quota.period, quota.of_week)
        for name, value in values:
            for _, bucket_days in buckets:
                terms = [tl.anchors[i][day_pos[day]]
                         for i, spec in enumerate(tl.slots)
                         if spec.sequence_class == name
                         for day in bucket_days]
                model.Add(sum(terms) == value).OnlyEnforceIf(literal)


# ------------------------------------------------------ sequence rules ----

def _effective_sequence_rules(tl: _Timeline) -> tuple[Any, ...]:
    """收集规则；按类规则对相同 occurrence 对的全局规则做整表覆盖。"""
    local: list[Any] = []
    global_names = {rule.name for rule in tl.config.sequence_rules}
    for spec in tl.config.sequence_classes:
        local.extend(spec.sequence_rules)
    if local:
        return tuple(rule for rule in local if rule.name not in global_names)
    return tuple(tl.config.sequence_rules)


def _rule_buckets(tl: _Timeline, period: str) -> tuple[tuple[str, tuple[int, ...]], ...]:
    """把 period bucket 映射为合法日 anchor 下标。"""
    days = {day: index for index, day in enumerate(tl.legal_days)}
    return tuple((key, tuple(days[day] for day in bucket if day in days))
                 for key, bucket in expand_period_buckets(
                     tl.config.schedule, period, ()))


def _bucket_member(tl: _Timeline, slot: int, days: tuple[int, ...],
                   tag: str) -> Any:
    """把 occurrence anchor 映射为某个 period bucket 的成员布尔量。"""
    member = tl.model.NewBoolVar(tag)
    flags = [tl.anchors[slot][day] for day in days]
    if flags:
        tl.model.AddMaxEquality(member, flags)
    else:
        tl.model.Add(member == 0)
    return member


def _rule_witness(tl: _Timeline, source: int, target: int,
                  days: tuple[int, ...], rule: Any, tag: str) -> Any:
    """建立同 bucket、严格先后与半开 gap 的 occurrence witness。"""
    model = tl.model
    witness = model.NewBoolVar(tag)
    model.AddImplication(witness, _bucket_member(tl, source, days, f"{tag}_source"))
    model.AddImplication(witness, _bucket_member(tl, target, days, f"{tag}_target"))
    source_end = tl.grid_ends[source][-1]
    target_start = tl.grid_ts[target][0]
    lower = 1 if rule.gap_us is None else max(1, rule.gap_us[0])
    upper = None if rule.gap_us is None else rule.gap_us[1] - 1
    model.Add(source_end + lower <= target_start).OnlyEnforceIf(witness)
    if upper is not None:
        model.Add(target_start - source_end <= upper).OnlyEnforceIf(witness)
    return witness


def _rule_obligation(tl: _Timeline, rule: Any, obligated: str,
                     source_slots: tuple[int, ...], target_slots: tuple[int, ...],
                     buckets: tuple[tuple[str, tuple[int, ...]], ...],
                     literal: Any) -> None:
    """为每个 source/target occurrence 添加至少一个可复用 witness。"""
    if obligated == rule.source:
        obligations, witnesses = source_slots, target_slots
        earlier, later = "source", "target"
    else:
        obligations, witnesses = target_slots, source_slots
        earlier, later = "witness", "obligation"
    for obligation in obligations:
        candidates = []
        for witness in witnesses:
            for bucket_index, (_, days) in enumerate(buckets):
                if not days:
                    continue
                if earlier == "source":
                    source, target = obligation, witness
                else:
                    source, target = witness, obligation
                candidates.append(_rule_witness(
                    tl, source, target, days, rule,
                    f"seqrule_{rule.name}_{obligation}_{witness}_{bucket_index}"))
                for day in days[1:]:
                    candidates.append(_rule_witness(
                        tl, source, target, (day,), rule,
                        f"seqrule_{rule.name}_{obligation}_{witness}_{bucket_index}_{day}"))
        constraint = tl.model.AddBoolOr(candidates)
        constraint.OnlyEnforceIf(literal)


def _build_sequence_rules(tl: _Timeline) -> None:
    """§4.6/§7.5：四模板 occurrence bucket 规则 CP 编码。"""
    rules = _effective_sequence_rules(tl)
    if not rules:
        return
    by_class = {spec.name: tuple(i for i, slot in enumerate(tl.slots)
                                if slot.sequence_class == spec.name)
                for spec in tl.config.sequence_classes}
    with tl.recorder.family("sequence_rule"):
        for rule in rules:
            literal = tl.model.NewBoolVar(f"sequence_rule_{rule.name}")
            tl.model.AddAssumption(literal)
            tl.literal_names[literal.Index()] = rule.name
            source = by_class.get(rule.source, ())
            target = by_class.get(rule.target, ())
            buckets = _rule_buckets(tl, rule.period)
            if rule.template == "not_co_existence":
                for left in source:
                    for right in target:
                        for bucket_index, (_, days) in enumerate(buckets):
                            for day in days:
                                tl.model.AddBoolOr([
                                    _bucket_member(tl, left, (day,),
                                                   f"seqrule_{rule.name}_{left}_{day}_l").Not(),
                                    _bucket_member(tl, right, (day,),
                                                   f"seqrule_{rule.name}_{right}_{day}_r").Not()]).OnlyEnforceIf(literal)
                continue
            _rule_obligation(tl, rule, rule.target, source, target, buckets, literal)
            if rule.template in {"response", "succession"}:
                _rule_obligation(tl, rule, rule.source, source, target, buckets, literal)


# ------------------------------------------------------ frame window ----

def _build_frame_windows(tl: _Timeline) -> None:
    """§7.5 frame window：word==受约束类 ⇒ ts 落允许日段（dup 按新时间重执行）。"""
    with tl.recorder.family("frame_window"):
        contexts = _window_contexts(tl)
        for position, spec in enumerate(tl.slots):
            for context in contexts.get(spec.sequence_class, ()):
                if context.class_index in tl.slot_domains[spec.key]:
                    _apply_window_row(tl, tl.grid_words[position],
                                      tl.grid_ts[position], context,
                                      f"w{position}")
        for j, source in enumerate(tl.dup_sources):
            src = tl.slots[source]
            for context in contexts.get(src.sequence_class, ()):
                if context.class_index in tl.slot_domains[src.key]:
                    _apply_window_row(tl, tl.grid_words[source], tl.dup_ts[j],
                                      context, f"dw{j}")


def _window_contexts(tl: _Timeline) -> dict[str, tuple[_WindowContext, ...]]:
    """每序列类的窗口上下文（literal + 绝对区段 + 词下标）。"""
    model, cfg = tl.model, tl.config
    result: dict[str, tuple[_WindowContext, ...]] = {}
    for seq in cfg.sequence_classes:
        rows = []
        for window in seq.frame_windows:
            if window.frame_class not in tl.frame_index:
                continue
            literal = model.NewBoolVar(f"window_{window.name}")
            model.AddAssumption(literal)
            tl.literal_names[literal.Index()] = window.name
            rows.append(_WindowContext(
                _window_segments(tl, window),
                tl.frame_index[window.frame_class], literal, window.name))
        result[seq.name] = tuple(rows)
    return result


def _window_segments(tl: _Timeline,
                     window: Any) -> tuple[tuple[int, int], ...]:
    """窗口允许的绝对 µs 区段（合法日 × of_day_us，clip 到 schedule 半开区间）。"""
    schedule = tl.config.schedule
    effective = window.of_week or tuple(range(1, 8))
    segments = []
    for day in tl.legal_days:
        if day.isoweekday() not in effective:
            continue
        base, _ = day_bounds_us(day, schedule.utc_offset_minutes)
        for lo, hi in window.of_day_us:
            a = max(base + lo, schedule.start_us)
            b = min(base + hi, schedule.end_us)
            if a < b:
                segments.append((a, b))
    return tuple(segments)


def _apply_window_row(tl: _Timeline, words: list[Any], timestamps: list[Any],
                      context: _WindowContext, tag: str) -> None:
    """把一条窗口约束应用到一行帧（task 行或 duplicate 平移行）。"""
    if not context.segments:
        for word in words:
            tl.model.Add(word != context.class_index
                         ).OnlyEnforceIf(context.literal)
        return
    for p, (word, ts_var) in enumerate(zip(words, timestamps, strict=True)):
        _window_on_frame(tl.model, word, ts_var, context, f"{tag}_{p}")


def _window_on_frame(model: cp_model.CpModel, word: Any, ts_var: Any,
                     context: _WindowContext, tag: str) -> None:
    """word==受约束类 ⇒ ts 落允许区段（reified 匹配 + segment one-hot）。"""
    match = model.NewBoolVar(f"wmatch_{tag}")
    model.Add(word == context.class_index).OnlyEnforceIf(match)
    model.Add(word != context.class_index).OnlyEnforceIf(match.Not())
    flags = [model.NewBoolVar(f"wseg_{tag}_{j}")
             for j in range(len(context.segments))]
    model.Add(sum(flags) == 1).OnlyEnforceIf([context.literal, match])
    for flag, (lo, hi) in zip(flags, context.segments, strict=True):
        model.AddImplication(flag, match)
        model.Add(ts_var >= lo).OnlyEnforceIf(flag)
        model.Add(ts_var <= hi - 1).OnlyEnforceIf(flag)


# ------------------------------------------------------ session 层 ----

def _build_session_and_crossing(tl: _Timeline) -> None:
    """§7.1-§7.3：session 层 + 流尾 duplicate 排序 + crossing witness。"""
    cfg, model = tl.config, tl.model
    timeline = SlotTimeline(
        first_ts=tuple(row[0] for row in tl.grid_ts),
        last_ts=tuple(row[-1] for row in tl.grid_ts),
        end_ts=tuple(row[-1] for row in tl.grid_ends),
        middle_ts=tuple(row[min(1, len(row) - 1)] for row in tl.grid_ts),
        lengths=tuple(spec.length_target for spec in tl.slots))
    spec = SessionBuildSpec(
        model=model, timeline=timeline, crossed=cfg.crossed_sessions,
        session_gap_us=cfg.session_gap_us,
        session_max_span_us=cfg.session_max_span_us,
        ts_low_us=cfg.schedule.start_us, ts_high_us=cfg.schedule.end_us - 1)
    with tl.recorder.family("session_slot"):
        tl.layer = build_session_layer(spec)
        _build_duplicate_order(tl)
    with tl.recorder.family("crossing"):
        build_crossing_witness(model, tl.layer, (0, len(tl.slots)))


def _build_duplicate_order(tl: _Timeline) -> None:
    """duplicate 流尾链：每条接在前一 session last_point + gap + 1µs 之后。"""
    model, cfg = tl.model, tl.config
    previous = tl.layer.session_last_point[-1]
    for row in tl.dup_ts:
        model.Add(row[0] >= previous + cfg.session_gap_us + 1)
        previous = row[-1]


def _build_noise_target(tl: _Timeline) -> None:
    """§7.6：noise reserve（O(S) 编码）+ half-even 表约束 + 总 equality。"""
    cfg, model = tl.config, tl.model
    with tl.recorder.family("noise_reserve"):
        spec = NoiseReserveSpec(
            model=model, layer=tl.layer,
            lengths=tuple(slot.length_target for slot in tl.slots),
            segments=tl.day_segments, session_max_len=cfg.session_max_len,
            ts_low_us=cfg.schedule.start_us,
            ts_high_us=cfg.schedule.end_us - 1)
        tl.reserve = build_noise_reserve(spec)
        planned = sum(slot.length_target for slot in tl.slots)
        target_value = half_even_noise_target(cfg.noise_ratio, planned)
        planned_var = model.NewIntVar(planned, planned, "planned_task_frames")
        target_var = model.NewIntVar(0, target_value, "noise_target")
        model.AddAllowedAssignments(
            [planned_var, target_var], [(planned, target_value)])
        model.Add(sum(tl.reserve.noise_units) == target_var)


# ------------------------------------------------------ 目标层 ----

def _day_base_shift(tl: _Timeline) -> int:
    """本地日索引换算平移量 ``offset_us - floor((start+off)/DAY)*DAY`` ∈ [0, DAY)。"""
    schedule = tl.config.schedule
    offset_us = schedule.utc_offset_minutes * 60_000_000
    return offset_us - ((schedule.start_us + offset_us) // _US_PER_DAY) * _US_PER_DAY


def _build_objectives(tl: _Timeline) -> None:
    """§7.8：三层目标表达式与辅助变量（L1 偏好项 Wave 4a 恒 0，Wave 5 扩展点）。

    timeline_end 的"latest interval end"取 ``FrameLayout.end_us`` 语义
    （point 帧 end==start、duplicate 取平移后末帧；与 §7.4 的 1µs 唯一性
    interval 末点 ts+1 恰差 1µs，冻结选择前者并在此钉死）。
    """
    with tl.recorder.family("objective"):
        model, schedule = tl.model, tl.config.schedule
        occupied = [ts for row in tl.grid_ts for ts in row]
        occupied += [ts for row in tl.dup_ts for ts in row]
        occupied_last = [end - 1 for row, ends in zip(tl.grid_ts, tl.grid_ends,
                                                       strict=True)
                         for _, end in zip(row, ends, strict=True)]
        occupied_last += [end - 1 for ends in tl.dup_ends for end in ends]
        shift = _day_base_shift(tl)
        tl.earliest = model.NewIntVar(schedule.start_us + shift,
                                      schedule.end_us - 1 + shift, "occ_earliest")
        model.AddMinEquality(tl.earliest, [ts + shift for ts in occupied])
        tl.latest = model.NewIntVar(schedule.start_us + shift,
                                    schedule.end_us - 1 + shift, "occ_latest")
        model.AddMaxEquality(tl.latest, [point + shift for point in occupied_last])
        tl.latest_end = model.NewIntVar(schedule.start_us, schedule.end_us,
                                        "interval_latest_end")
        model.AddMaxEquality(tl.latest_end, [end for row in tl.grid_ends for end in row]
                             + [end for row in tl.dup_ends for end in row])
        bound = (schedule.end_us - schedule.start_us) // _US_PER_DAY + 2
        tl.earliest_day = model.NewIntVar(0, bound, "earliest_day")
        model.AddDivisionEquality(tl.earliest_day, tl.earliest, _US_PER_DAY)
        tl.latest_day = model.NewIntVar(0, bound, "latest_day")
        model.AddDivisionEquality(tl.latest_day, tl.latest, _US_PER_DAY)
        tl.span_days = model.NewIntVar(1, bound, "calendar_days_spanned")
        model.Add(tl.span_days == tl.latest_day - tl.earliest_day + 1)
        tl.timeline_end = model.NewIntVar(0, schedule.end_us - schedule.start_us,
                                          "timeline_end")
        model.Add(tl.timeline_end == tl.latest_end - schedule.start_us)
        _redundant_span_bound(tl)


def _redundant_span_bound(tl: _Timeline) -> None:
    """冗余直接下界（解析精确形）：帮助 timeline_end 的全局界传播（§7.8 第三层）。

    记 slot 链长 ``c_i = (L_i−1)×gap_lo``。session 时间有序且互不交，故
    ``latest − earliest ≥ Σ session span + (S−1)×(session_gap+1)``。零交叉时
    每 session 恰一个 owner，``Σ span = Σ c_i``（精确）。有交叉时 crossed
    session 的 span ≥ max(两链)，被共享吃掉的链至多是配对中较小侧——整体
    最大节省 ≤ D 条最小链之和（升序取前 D）。当两链差 ≤1µs 且双 owner 都
    ≥2 帧时，inside owner 的链被 1µs 边距顶出，span ≥ max+1；链差 ≥2µs 或
    len==1 的 slot 可以整段包进大链（span=max 恰达），每条这样的"可包进"
    小链至多供一个 crossed session 逃避 +1，故 +1 至少
    ``max(D − #{c_i ≤ max−2}, 0)`` 个。该式由逐链约束蕴含，但跨
    AddElement/置换间接层后求解器难以自行聚合；显式写出让最优性证明在
    冻结预算内可达（均匀链形下逐 µs 精确）。

    @param tl 构建上下文
    """
    cfg = tl.config
    if not tl.slots:
        return
    gap_lo = max(cfg.frame_gap_us[0], 1)
    crossed = cfg.crossed_sessions
    sessions = len(tl.slots) - crossed
    chains = sorted((slot.length_target - 1) * gap_lo for slot in tl.slots)
    savings = sum(chains[:crossed])
    top = chains[-1]
    dodgers = sum(1 for chain in chains if chain <= top - 2)
    interleave = max(crossed - dodgers, 0)
    span = sum(chains) - savings + interleave
    span += (sessions - 1) * (cfg.session_gap_us + 1)
    tl.model.Add(tl.latest - tl.earliest >= span)


def _lex_solve(tl: _Timeline) -> PlannerObjectives:
    """§7.8 三层字典序：每层独立 solve 必须 OPTIMAL，固化等式再下层。"""
    layers = (("preference_deviation", sum(tl.preference_terms)),
              ("calendar_days_spanned", tl.span_days),
              ("timeline_end", tl.timeline_end))
    values: list[int] = []
    for name, expression in layers:
        tl.model.Minimize(expression)
        solver = make_planner_solver(tl.config.seed)
        status = solver.Solve(tl.model)
        _require_layer_status(tl, solver, status, name)
        value = int(solver.ObjectiveValue())
        values.append(value)
        tl.model.Add(expression == value)
    tl.solver = solver
    return PlannerObjectives(*values)


def _require_layer_status(tl: _Timeline, solver: cp_model.CpSolver,
                          status: int, layer: str) -> None:
    """OPTIMAL 之外分流：INFEASIBLE→core 具名集合；UNKNOWN/FEASIBLE→budget。"""
    if status == cp_model.INFEASIBLE:
        raise PlannerInfeasibleError(format_infeasible_message(
            infeasible_core_names(solver, tl.literal_names)))
    if status != cp_model.OPTIMAL:
        raise PlannerBudgetError(format_budget_message("timeline", layer))


# ------------------------------------------------------ 解码 ----

def _decode_frame(tl: _Timeline, slot_position: int, frame_position: int,
                  ts_var: Any, end_var: Any, duration_var: Any) -> FrameLayout:
    """解码单帧：point 的 end 等于 start，duration frame 解出闭区间 target。"""
    word = tl.solver.Value(tl.grid_words[slot_position][frame_position])
    name = tl.frame_names[word]
    start = tl.solver.Value(ts_var)
    duration = tl.solver.Value(duration_var)
    return FrameLayout(
        position=frame_position, frame_class=name, start_us=start,
        end_us=tl.solver.Value(end_var),
        duration_target_us=(tl.duration_targets[(tl.slots[slot_position].key,
                                                 frame_position, name)]
                            if duration else None),
        resources=tl.frame_domains[name].resources)


def _decode_sequences(tl: _Timeline) -> tuple[tuple[SequenceLayout, ...],
                                              tuple[SessionLayout, ...]]:
    """解码全部 sequence layout 与 session layout 并核验结构不变量。"""
    solver, cfg = tl.solver, tl.config
    sessions = len(tl.slots) - cfg.crossed_sessions
    _verify_permutations(tl, sessions)
    layouts: list[SequenceLayout] = []
    for i, spec in enumerate(tl.slots):
        position = solver.Value(tl.layer.position_of_owner[i])
        primary = position < sessions
        session_index = position if primary else solver.Value(
            tl.layer.session_at_rank[position - sessions])
        row = tl.grid_ts[i]
        ends = tl.grid_ends[i]
        durations = tl.grid_durations[i]
        start, last = solver.Value(row[0]), solver.Value(row[-1])
        end = max(solver.Value(value) for value in ends)
        anchor = local_date(start, cfg.schedule.utc_offset_minutes).isoformat()
        frames = tuple(_decode_frame(tl, i, p, row[p], ends[p], durations[p])
                       for p in range(spec.length_target))
        layouts.append(SequenceLayout(
            slot_key=spec.key, session_index=session_index,
            owner_role="primary" if primary else "secondary",
            anchor_date=anchor, start_us=start, last_point_us=last,
            end_us=end, frames=frames))
    _verify_crossed_premise(tl)
    return tuple(layouts), _decode_sessions(tl, sessions)


def _verify_permutations(tl: _Timeline, sessions: int) -> None:
    """解码不变量：owner/rank 是置换、secondary 集合与 tail owner 一致。"""
    solver = tl.solver
    owners = [solver.Value(v) for v in tl.layer.owner_at_position]
    if sorted(owners) != list(range(len(owners))):
        raise PlannerInternalError("owner_at_position is not a permutation")
    if tl.layer.session_at_rank is None:
        return
    ranks = [solver.Value(v) for v in tl.layer.rank_of_session]
    if sorted(ranks) != list(range(sessions)):
        raise PlannerInternalError("rank_of_session is not a permutation")
    real = sorted(solver.Value(v) for v in tl.layer.secondary_owner
                  if solver.Value(v) != len(owners))
    if real != sorted(owners[sessions:]):
        raise PlannerInternalError(
            "secondary mapping does not match tail owner positions")


def _verify_crossed_premise(tl: _Timeline) -> None:
    """解码不变量：每个 crossed session 的实际交错满足 §7.3 双 orientation 前提。"""
    if tl.layer.session_at_rank is None:
        return
    solver = tl.solver
    for r in range(tl.config.crossed_sessions):
        session = solver.Value(tl.layer.session_at_rank[r])
        primary = solver.Value(tl.layer.owner_at_position[session])
        secondary = solver.Value(tl.layer.secondary_owner[session])
        prim, sec = tl.grid_ts[primary], tl.grid_ts[secondary]
        outside = (solver.Value(prim[0]) < solver.Value(sec[min(1, len(sec) - 1)])
                   < solver.Value(prim[-1]))
        inside = (solver.Value(sec[0]) < solver.Value(prim[min(1, len(prim) - 1)])
                  < solver.Value(sec[-1]))
        if not (outside or inside):
            raise PlannerInternalError(
                f"crossed session {session} violates the interleave premise")


def _decode_sessions(tl: _Timeline, sessions: int) -> tuple[SessionLayout, ...]:
    """解码 N-D 个 primary session（duplicate 流尾 session 不生成 SessionLayout）。"""
    solver = tl.solver
    count = len(tl.slots)
    rows = []
    for k in range(sessions):
        primary = solver.Value(tl.layer.owner_at_position[k])
        secondary = count
        if tl.layer.secondary_owner is not None:
            secondary = solver.Value(tl.layer.secondary_owner[k])
        rows.append(SessionLayout(
            index=k, primary_slot_key=tl.slots[primary].key,
            secondary_slot_key=(tl.slots[secondary].key
                                if secondary != count else None),
            start_us=solver.Value(tl.layer.session_start[k]),
            last_point_us=solver.Value(tl.layer.session_last_point[k]),
            end_us=solver.Value(tl.layer.session_end[k]),
            noise_count=solver.Value(tl.reserve.noise_count[k])))
    _verify_session_bounds(tl, rows)
    return tuple(rows)


def _verify_session_bounds(tl: _Timeline, rows: list[SessionLayout]) -> None:
    """解码不变量：session 有序、区间自洽且全部落在 schedule 半开区间内。"""
    schedule = tl.config.schedule
    previous: int | None = None
    for row in rows:
        if not (schedule.start_us <= row.start_us <= row.last_point_us
                <= row.end_us < schedule.end_us):
            raise PlannerInternalError(
                f"session {row.index} escapes the schedule bounds")
        if previous is not None and row.start_us <= previous:
            raise PlannerInternalError(f"session {row.index} breaks ordering")
        previous = row.last_point_us


def _allocate_noise(tl: _Timeline, sessions: tuple[SessionLayout, ...],
                    layouts: tuple[SequenceLayout, ...]) -> tuple[NoiseSlot, ...]:
    """解码 reserve 后按 scenario.noise 流确定性分配 noise slot（§7.6/§11）。"""
    cfg = tl.config
    counts = tuple(tl.solver.Value(v) for v in tl.reserve.noise_count)
    tasks: dict[int, list[int]] = {row.index: [] for row in sessions}
    for layout in layouts:
        tasks[layout.session_index].extend(
            frame.start_us for frame in layout.frames)
    spans = tuple(NoiseSessionSpan(
        row.index, row.start_us, row.last_point_us, tuple(sorted(tasks[row.index])))
        for row in sessions)
    spec = NoiseAllocationSpec(
        seed=cfg.seed, noise_classes=cfg.noise_classes, noise_counts=counts,
        session_spans=spans, segments=tl.day_segments)
    return allocate_noise(spec)


def _decode_duplicates(tl: _Timeline) -> tuple[DuplicateLayout, ...]:
    """解码 duplicate layout（session_index 从 S 起接续编号，§7.7）。"""
    sessions = len(tl.slots) - tl.config.crossed_sessions
    rows = []
    for j, source in enumerate(tl.dup_sources):
        row = tl.dup_ts[j]
        ends = tl.dup_ends[j]
        source_durations = tl.grid_durations[source]
        frames = tuple(_decode_frame(tl, source, p, row[p], ends[p],
                                     source_durations[p])
                       for p in range(len(row)))
        rows.append(DuplicateLayout(
            key=f"duplicate:{j}", ordinal=j,
            source_slot_key=tl.slots[source].key,
            session_index=sessions + j,
            offset_us=tl.solver.Value(tl.dup_offsets[j]), frames=frames))
    return tuple(rows)


# ------------------------------------------------------ digest ----

def _frame_entry(frame: FrameLayout) -> dict[str, Any]:
    """digest 的单帧 canonical 条目。"""
    return {"duration_target_us": frame.duration_target_us,
            "end_us": frame.end_us, "frame_class": frame.frame_class,
            "position": frame.position, "resources": list(frame.resources),
            "start_us": frame.start_us}


def _plan_digest(plan: ScenarioPlan) -> str:
    """§6.1：canonical object 的 UTF-8 SHA-256（``sha256:`` 前缀）。

    只覆盖 quota targets、slot key、frame word、start/end/duration、resource、
    session owner、noise slot、duplicate source/layout 与 objective values；
    不含 payload、callable、credential、report 字段或 ``ModelStats()`` 文本
    （稳定 family counts 只进 report，不参与 digest）。

    @param plan 已组装（digest 占位为空串）的冻结计划
    @return ``sha256:<hex>`` 摘要
    """
    value = {
        "duplicates": [{
            "key": row.key, "ordinal": row.ordinal,
            "source_slot_key": row.source_slot_key,
            "session_index": row.session_index, "offset_us": row.offset_us,
            "frames": [_frame_entry(frame) for frame in row.frames]}
            for row in plan.duplicates],
        "noise": [{
            "key": row.key, "frame_class": row.frame_class,
            "class_ordinal": row.class_ordinal,
            "session_index": row.session_index,
            "timestamp_us": row.timestamp_us} for row in plan.noise_slots],
        "objectives": {
            "preference_deviation": plan.objectives.preference_deviation,
            "calendar_days_spanned": plan.objectives.calendar_days_spanned,
            "timeline_end_us": plan.objectives.timeline_end_us},
        "quota": [{
            "name": row.name, "bucket": row.bucket,
            "sequence_class": row.sequence_class, "target": row.target}
            for row in plan.quota_summary],
        "sessions": [{
            "index": row.index, "primary_slot_key": row.primary_slot_key,
            "secondary_slot_key": row.secondary_slot_key,
            "start_us": row.start_us, "last_point_us": row.last_point_us,
            "end_us": row.end_us, "noise_count": row.noise_count}
            for row in plan.sessions],
        "slots": [{
            "slot_key": layout.slot_key, "owner_role": layout.owner_role,
            "session_index": layout.session_index,
            "anchor_date": layout.anchor_date, "start_us": layout.start_us,
            "last_point_us": layout.last_point_us, "end_us": layout.end_us,
            "frames": [_frame_entry(frame) for frame in layout.frames]}
            for layout in plan.layouts],
    }
    text = json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

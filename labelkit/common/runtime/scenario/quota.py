"""v1.17 场景配额：静态算术（两形态校验、bucket 展开与逐类静态 target）+
QuotaCompiler CP 见证模型（SPEC-SP §6.3）。

静态侧：SPEC-SP §4.4 counts 与 total/weights/allocation 互斥；weights 先 GCD 归一，
``exact`` 要求 total 是 minimum exact cohort 的整数倍；``largest_remainder``
复用 ``labelkit.common.config.model.apportion_tiers`` 的纯整数最大余额算法
（零浮点、零 rng，平票按表内声明序）。bucket 展开委托 calendar 纯函数。
模型侧：每 (sequence_class, local_date) 一个非负整数 count 变量，每张展开
bucket 一个 assumption literal，逐类 equality 仅在该 literal 下生效；
``Minimize(sum(counts))`` 单层必须 OPTIMAL；解码只取逐类 target 总数，
date assignment 是 quota-only witness 不进计划不进 RNG。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from math import gcd
from typing import Any

from ortools.sat.python import cp_model

from labelkit.common.config.model import apportion_tiers
from labelkit.common.runtime.scenario.calendar import (
    VALID_PERIODS,
    expand_period_buckets,
    legal_dates,
)
from labelkit.common.runtime.scenario.diagnostics import (
    FamilyRecorder,
    PlannerBudgetError,
    PlannerInfeasibleError,
    enforce_model_capacity,
    format_budget_message,
    format_infeasible_message,
    infeasible_core_names,
    make_planner_solver,
)
from labelkit.common.runtime.scenario.model import (
    PlannerModelStats,
    QuotaSpec,
    QuotaSummary,
    ScheduleSpec,
)
from labelkit.common.runtime.scenario.rules import is_valid_name


@dataclass(frozen=True)
class _ApportionRow:
    """apportion_tiers 复用适配行：weight 原样、rank 取表内声明序。"""

    weight: int
    tier_rank: int


def _gcd_of_weights(weights: tuple[tuple[str, int], ...]) -> int:
    """求全部权重的最大公约数（空表返回 0）。"""
    divisor = 0
    for _, weight in weights:
        divisor = gcd(divisor, weight)
    return divisor


def normalize_weights(weights: tuple[tuple[str, int], ...]) -> tuple[tuple[str, int], ...]:
    """weights 除以最大公约数得到最简整数比。

    @param weights ``(class, weight)`` 声明序元组
    @return 同序最简整数比元组
    """
    divisor = _gcd_of_weights(weights)
    if divisor == 0:
        return ()
    return tuple((name, weight // divisor) for name, weight in weights)


def minimum_exact_cohort(weights: tuple[tuple[str, int], ...]) -> int:
    """最简整数比之和 = exact allocation 的最小可交付 cohort。

    @param weights ``(class, weight)`` 声明序元组
    @return cohort 大小
    """
    return sum(weight for _, weight in normalize_weights(weights))


def nearest_exact_totals(total: int, cohort: int) -> tuple[int | None, int | None]:
    """最近可精确 total：小于者不存在时为 ``None``。

    @param total 声明的 total
    @param cohort 最小 exact cohort
    @return ``(小于 total 的最近正数倍, 大于 total 的最近倍)`` 二元组
    """
    below = ((total - 1) // cohort) * cohort if total > cohort else None
    above = (total // cohort + 1) * cohort
    return below, above


def allocate_weights(total: int, weights: tuple[tuple[str, int], ...],
                     allocation: str) -> tuple[tuple[str, int], ...]:
    """把 total 化成逐类整数值。

    - ``exact``：total 必须是 cohort 整数倍，按最简比放大；
    - ``largest_remainder``：纯整数最大余额（平票按声明序，复用
      ``apportion_tiers``）。

    @param total 每个 period bucket 的总交付数
    @param weights ``(class, weight)`` 声明序元组
    @param allocation ``exact`` | ``largest_remainder``
    @return 声明序逐类整数值元组
    @raises ValueError exact 不整除或 allocation 未知时
    """
    if allocation == "exact":
        cohort = minimum_exact_cohort(weights)
        if cohort == 0 or total % cohort != 0:
            raise ValueError(f"exact total must be a multiple of the minimum "
                             f"exact cohort {cohort}")
        factor = total // cohort
        return tuple((name, weight * factor)
                     for name, weight in normalize_weights(weights))
    if allocation == "largest_remainder":
        rows = tuple(_ApportionRow(weight, index)
                     for index, (_, weight) in enumerate(weights))
        shares = apportion_tiers(total, rows)
        return tuple((name, share)
                     for (name, _), share in zip(weights, shares, strict=True))
    raise ValueError(f"unknown allocation: {allocation!r}")


def validate_quota_spec(quota: QuotaSpec) -> None:
    """校验 quota 两形态互斥矩阵。

    @param quota 待校验的 quota
    @raises ValueError 名称、period、形态互斥或数值域非法时
    """
    if not is_valid_name(quota.name):
        raise ValueError(f"invalid quota name: {quota.name!r}")
    if quota.period not in VALID_PERIODS:
        raise ValueError(f"unknown quota period: {quota.period!r}")
    has_counts = bool(quota.counts)
    has_weights = bool(quota.weights) or quota.total is not None or quota.allocation
    if has_counts and has_weights:
        raise ValueError("quota counts and total/weights/allocation are mutually "
                         "exclusive")
    if quota.counts:
        for name, value in quota.counts:
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"quota {quota.name!r} count for {name!r} must be "
                                 f"a non-negative integer")
        return
    if not quota.weights:
        raise ValueError("quota requires either counts or weights form")
    if len(quota.weights) < 2:
        raise ValueError("quota weights require at least two classes")
    for name, weight in quota.weights:
        if not isinstance(weight, int) or isinstance(weight, bool) or weight <= 0:
            raise ValueError(f"quota {quota.name!r} weight for {name!r} must be a "
                             f"positive integer")
    if not isinstance(quota.total, int) or isinstance(quota.total, bool) \
            or quota.total < 1:
        raise ValueError("quota total must be an integer >= 1")
    if quota.allocation not in ("exact", "largest_remainder"):
        raise ValueError("quota weights form requires allocation "
                         "'exact' or 'largest_remainder'")


def quota_bucket_values(quota: QuotaSpec) -> tuple[tuple[str, int], ...]:
    """单个 period bucket 内的逐类值。

    @param quota 待展开值的 quota
    @return 声明序逐类整数值元组
    @raises ValueError 形态或数值非法时
    """
    validate_quota_spec(quota)
    if quota.counts:
        return tuple(quota.counts)
    return allocate_weights(quota.total or 0, quota.weights,
                            quota.allocation or "exact")


def quota_static_summary(quota: QuotaSpec,
                         schedule: ScheduleSpec) -> tuple[QuotaSummary, ...]:
    """展开 period bucket 并产出逐 ``(bucket, class)`` 的静态 target 行。

    @param quota 待展开的 quota
    @param schedule 冻结 schedule（提供本地日与排除日）
    @return ``QuotaSummary`` 行元组（bucket 升序、类声明序）
    @raises ValueError quota 形态或数值非法时
    """
    values = quota_bucket_values(quota)
    buckets = expand_period_buckets(schedule, quota.period, quota.of_week)
    summary: list[QuotaSummary] = []
    for bucket, _ in buckets:
        summary.extend(QuotaSummary(quota.name, quota.period, bucket, name, target)
                       for name, target in values)
    return tuple(summary)


def static_class_targets(quota: QuotaSpec,
                         schedule: ScheduleSpec) -> tuple[tuple[str, int], ...]:
    """逐类静态 target = Σ bucket 内逐类值（每个 bucket 值相同，类按声明序）。

    @param quota 待汇总的 quota
    @param schedule 冻结 schedule
    @return 声明序逐类求和元组
    @raises ValueError quota 形态或数值非法时
    """
    values = quota_bucket_values(quota)
    buckets = expand_period_buckets(schedule, quota.period, quota.of_week)
    return tuple((name, value * len(buckets)) for name, value in values)


def unsatisfiable_buckets(quota: QuotaSpec,
                          schedule: ScheduleSpec) -> tuple[str, ...]:
    """点名无合法日期且 target>0 的 bucket（§4.4 直接不可满足判定）。

    @param quota 待判定的 quota
    @param schedule 冻结 schedule
    @return 不可满足 bucket key 元组（bucket 升序）
    @raises ValueError quota 形态或数值非法时
    """
    values = quota_bucket_values(quota)
    bucket_total = sum(value for _, value in values)
    buckets = expand_period_buckets(schedule, quota.period, quota.of_week)
    if bucket_total <= 0:
        return ()
    return tuple(key for key, days in buckets if not days)


# ------------------------------------------------------ QuotaCompiler ----

def half_even_noise_target(ratio: Decimal, planned_task_frames: int) -> int:
    """ROUND_HALF_EVEN 的精确 noise target（§7.6，纯 Decimal 域零浮点）。

    @param ratio noise_ratio（Decimal 形态，满足 0 <= ratio < 1）
    @param planned_task_frames 全部 slot 的 length_target 之和（>= 0）
    @return ``round_half_even(ratio * planned_task_frames)``
    """
    return int((ratio * planned_task_frames).to_integral_value(
        rounding=ROUND_HALF_EVEN))


@dataclass(frozen=True)
class QuotaSolution:
    """QuotaCompiler 求解产物：逐类 target、bucket 静态行与模型规模。"""

    class_targets: tuple[tuple[str, int], ...]
    summary: tuple[QuotaSummary, ...]
    stats: PlannerModelStats


def _referenced_classes(quotas: tuple[QuotaSpec, ...]) -> list[str]:
    """按声明序收集全部 quota 引用的 sequence class（去重保持首现序）。

    @param quotas quota 表
    @return 类名列表
    @raises ValueError quota 形态非法时（调用方 derive 已先行拦截）
    """
    names: list[str] = []
    for quota in quotas:
        for name, _ in quota_bucket_values(quota):
            if name not in names:
                names.append(name)
    return names


def _class_upper_bounds(quotas: tuple[QuotaSpec, ...],
                        schedule: ScheduleSpec) -> dict[str, int]:
    """逐类 count 变量域上界 = 所有命中 quota target 之和（§6.3，非常数 horizon）。

    @param quotas quota 表
    @param schedule 冻结 schedule
    @return 类名 → 域上界
    """
    uppers: dict[str, int] = {}
    for quota in quotas:
        for name, value in static_class_targets(quota, schedule):
            uppers[name] = uppers.get(name, 0) + value
    return uppers


def _quota_literal_name(quota: QuotaSpec, bucket: str) -> str:
    """quota 模型 bucket literal 的 core 名称（quota:bucket 双段定位）。"""
    return f"quota:{quota.name}:{bucket}"


def _build_count_vars(model: cp_model.CpModel, classes: list[str],
                      days: tuple[Any, ...], uppers: dict[str, int]) -> dict:
    """quota_domain 族：每 (sequence_class, local_date) 一个非负整数 count 变量。

    @param model CP-SAT 模型
    @param classes 声明序类名列表
    @param days schedule 内合法本地日（升序）
    @param uppers 逐类域上界
    @return ``(class, date)`` → IntVar
    """
    counts: dict[tuple[str, Any], Any] = {}
    for name in classes:
        upper = uppers.get(name, 0)
        for day in days:
            counts[(name, day)] = model.NewIntVar(
                0, upper, f"count_{name}_{day.isoformat()}")
    return counts


def _build_bucket_rows(model: cp_model.CpModel,
                       quotas: tuple[QuotaSpec, ...], schedule: ScheduleSpec,
                       counts: dict) -> dict[int, str]:
    """quota_row 族：每张展开 bucket 一个 assumption literal + 逐类 equality。

    @param model CP-SAT 模型
    @param quotas quota 表
    @param schedule 冻结 schedule
    @param counts ``_build_count_vars`` 的变量表
    @return literal 下标 → core 名称（供 ``infeasible_core_names`` 映射）
    """
    literal_names: dict[int, str] = {}
    for quota in quotas:
        values = quota_bucket_values(quota)
        buckets = expand_period_buckets(schedule, quota.period, quota.of_week)
        for bucket, bucket_days in buckets:
            literal = model.NewBoolVar(f"quota_{quota.name}_{bucket}")
            model.AddAssumption(literal)
            literal_names[literal.Index()] = _quota_literal_name(quota, bucket)
            for name, value in values:
                terms = [counts[(name, day)] for day in bucket_days]
                model.Add(sum(terms) == value).OnlyEnforceIf(literal)
    return literal_names


def _build_quota_objective(model: cp_model.CpModel,
                           uppers: dict[str, int], counts: dict) -> None:
    """objective 族：``Minimize(sum(counts))`` 单层目标（显式 total 变量承载）。"""
    total = model.NewIntVar(0, sum(uppers.values()), "quota_total")
    model.Add(total == sum(counts.values()))
    model.Minimize(total)


def solve_quota_targets(quotas: tuple[QuotaSpec, ...], schedule: ScheduleSpec,
                        seed: int) -> QuotaSolution:
    """§6.3 QuotaCompiler：建立并求解 quota 见证模型，解码逐类 target。

    静态 target（``static_class_targets``）与模型最优 target 必须一致——多张
    quota 引用同一 class 时 occurrence 共享，模型给出满足全部 equality 的最小
    总数。求解参数冻结：1 worker、``seed & 0x7fffffff``、10.0 deterministic
    time；非 OPTIMAL 状态按 §8.3 分流（INFEASIBLE→core 具名集合，否则 budget）。

    @param quotas quota 表
    @param schedule 冻结 schedule
    @param seed run seed
    @return ``QuotaSolution``（class_targets 声明序、summary bucket 升序）
    @raises PlannerInfeasibleError bucket equality 不可满足时（具名 quota 集合）
    @raises PlannerBudgetError 非 OPTIMAL 时（model=quota, layer=quota）
    @raises PlannerCapacityError quota 模型超 250k entry 时
    @raises ValueError quota 形态非法时（derive 阶段应已拦截）
    """
    model = cp_model.CpModel()
    recorder = FamilyRecorder(model, ("quota_domain", "quota_row", "objective"))
    days = legal_dates(schedule, ())
    classes = _referenced_classes(quotas)
    uppers = _class_upper_bounds(quotas, schedule)
    with recorder.family("quota_domain"):
        counts = _build_count_vars(model, classes, days, uppers)
    with recorder.family("quota_row"):
        literal_names = _build_bucket_rows(model, quotas, schedule, counts)
    with recorder.family("objective"):
        _build_quota_objective(model, uppers, counts)
    enforce_model_capacity("quota", recorder)
    solver = make_planner_solver(seed)
    status = solver.Solve(model)
    if status == cp_model.INFEASIBLE:
        raise PlannerInfeasibleError(format_infeasible_message(
            infeasible_core_names(solver, literal_names)))
    if status != cp_model.OPTIMAL:
        raise PlannerBudgetError(format_budget_message("quota", "quota"))
    targets = tuple((name, sum(solver.Value(counts[(name, day)]) for day in days))
                    for name in classes)
    summary: list[QuotaSummary] = []
    for quota in quotas:
        summary.extend(quota_static_summary(quota, schedule))
    variables, constraints = recorder.totals()
    return QuotaSolution(targets, tuple(summary),
                         PlannerModelStats(variables, constraints,
                                           recorder.stats()))

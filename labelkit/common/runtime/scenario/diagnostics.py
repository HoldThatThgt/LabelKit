"""v1.17 planner 诊断：异常类（CONTRACTS §7.19.6 逐字）、稳定 message 模板、
求解器工厂、assumption core 映射、约束族差分记录与 §8.1 派生边界检查。

capacity message 使用 §8.3 冻结的英文稳定字段（不输出"减少 horizon"一类猜测）；
budget message 点名 ``model=quota|timeline`` 与超时 layer；infeasible message
拼出"足以导致不可行"的具名约束集合（§8.4）。``FamilyRecorder`` 按 §8.2 在每个
builder 前后读取 proto variable/constraint 差分；``derive_stream_bounds`` 是
§8.1 的一次性聚合纯函数，不因前一错误短路。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

from ortools.sat.python import cp_model

from labelkit.common.runtime.scenario.calendar import local_date_span
from labelkit.common.runtime.scenario.model import (
    PlannerFamilyStats,
    ScenarioConfig,
)
from labelkit.common.runtime.scenario.rules import is_valid_name

#: quota / timeline 两模型各自的 entries（variables + constraints）容量上限。
PLANNER_ENTRY_LIMIT = 250_000

#: §7.8 冻结的每层 deterministic budget（不是对所有组合已证明安全的界，
#: §13.2 的五点曲线与 crossing 组在实现门里证明该常数覆盖受测形态）。
SOLVE_DETERMINISTIC_TIME = 10.0


class PlannerInfeasibleError(ValueError):
    """用户硬约束没有共同解。"""


class PlannerCapacityError(RuntimeError):
    """模型在求解前超过实现容量。"""


class PlannerBudgetError(RuntimeError):
    """deterministic solve budget 内无法冻结最优计划。"""


class PlannerInternalError(RuntimeError):
    """solver 解码或冻结计划违反实现不变量。"""


def _families_text(families: Mapping[str, int]) -> tuple[str, str]:
    """按计数降序（平票名字升序）渲染 families 与 dominant。

    @param families 约束族名 → entry 数
    @return ``(families 文本, dominant 族名)``
    """
    ordered = sorted(families.items(), key=lambda item: (-item[1], item[0]))
    body = ",".join(f"{name}:{count}" for name, count in ordered)
    return "{" + body + "}", ordered[0][0]


def format_capacity_message(model: str, entries: int, families: Mapping[str, int],
                            limit: int = PLANNER_ENTRY_LIMIT) -> str:
    """组装 §8.3 冻结的 capacity 报错文本。

    @param model ``quota`` | ``timeline``
    @param entries 实际 variables + constraints 总数
    @param families 约束族名 → entry 数
    @param limit 容量上限（默认 250000）
    @return 稳定英文 message
    @raises ValueError families 为空时（无法判定 dominant）
    """
    families_text, dominant = _families_text(families)
    return (f"sequence planner capacity exceeded: model={model} entries={entries} "
            f"limit={limit} dominant={dominant} families={families_text}")


def format_budget_message(model: str, layer: str) -> str:
    """组装 budget 超时报错文本（点名 model 与超时 layer）。

    @param model ``quota`` | ``timeline``
    @param layer 超时的求解层名
    @return 稳定英文 message
    """
    return f"sequence planner budget exhausted: model={model} layer={layer}"


def format_infeasible_message(names: Iterable[str]) -> str:
    """组装 infeasible core 报错文本（§8.4 示例逐字节形态）。

    @param names "足以导致不可行"的具名约束集合（保持给定顺序）
    @return 稳定英文 message
    """
    joined = ",".join(names)
    return f"sequence planner infeasible: constraints=[{joined}]"


# ------------------------------------------------------ 求解器与 core ----

def make_planner_solver(seed: int,
                        dtime: float | None = None) -> cp_model.CpSolver:
    """按 §7.8 冻结参数面构造求解器（不声明手写 decision strategy）。

    @param seed run seed（random_seed 取 ``seed & 0x7fffffff``）
    @param dtime 覆盖用 deterministic budget（缺省取模块冻结常数）
    @return 单 worker CP-SAT 求解器
    """
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = seed & 0x7FFFFFFF
    solver.parameters.max_deterministic_time = (
        SOLVE_DETERMINISTIC_TIME if dtime is None else dtime)
    return solver


def infeasible_core_names(solver: cp_model.CpSolver,
                          literal_names: Mapping[int, str]) -> list[str]:
    """把 ``SufficientAssumptionsForInfeasibility`` 的字面量下标映射回具名约束。

    @param solver 已证 INFEASIBLE 的求解器
    @param literal_names 字面量下标 → 自然名称（quota/frame window/rule/resource）
    @return "足以导致不可行"的具名集合（保持 solver 返回顺序，不声称最小）
    """
    core = solver.SufficientAssumptionsForInfeasibility()
    return [literal_names[item] for item in core if item in literal_names]


def proto_entry_counts(model: cp_model.CpModel) -> tuple[int, int]:
    """读取 proto 的 (variables, constraints) 计数。

    @param model CP-SAT 模型
    @return 二元计数
    """
    proto = model.Proto()
    return len(proto.variables), len(proto.constraints)


class FamilyRecorder:
    """§8.2 约束族差分记录器：builder 前后 proto 计数之差按族累计。"""

    def __init__(self, model: cp_model.CpModel, names: tuple[str, ...]) -> None:
        """初始化全部族为零（未构建的族保持 0/0 在场）。

        @param model 被记录的 CP-SAT 模型
        @param names 全部约束族名（初始化即写入，占位族天然为 0）
        """
        self._model = model
        self.families: dict[str, list[int]] = {name: [0, 0] for name in names}

    @contextmanager
    def family(self, name: str):
        """把 with 块内的 proto 增量记入指定约束族。

        @param name 约束族名
        @yield None
        """
        before = proto_entry_counts(self._model)
        try:
            yield
        finally:
            after = proto_entry_counts(self._model)
            row = self.families[name]
            row[0] += after[0] - before[0]
            row[1] += after[1] - before[1]

    def stats(self) -> dict[str, PlannerFamilyStats]:
        """导出族名 → ``PlannerFamilyStats`` 的普通 dict（调用方负责冻结）。"""
        return {name: PlannerFamilyStats(row[0], row[1])
                for name, row in self.families.items()}

    def totals(self) -> tuple[int, int]:
        """全部族累计的 (variables, constraints)。"""
        first = sum(row[0] for row in self.families.values())
        second = sum(row[1] for row in self.families.values())
        return first, second


def enforce_model_capacity(model_kind: str, recorder: FamilyRecorder) -> None:
    """§8.2/§8.3：求解前的 250k entry 容量判定（两模型各自独立，不相加）。

    @param model_kind ``quota`` | ``timeline``
    @param recorder 该模型的约束族记录器
    @raises PlannerCapacityError entries 超过 ``PLANNER_ENTRY_LIMIT`` 时
    """
    variables, constraints = recorder.totals()
    entries = variables + constraints
    if entries > PLANNER_ENTRY_LIMIT:
        families = {name: row[0] + row[1]
                    for name, row in recorder.families.items()}
        raise PlannerCapacityError(
            format_capacity_message(model_kind, entries, families))


# ------------------------------------------------------ §8.1 派生检查 ----
# 本节的 quota 静态算术引用采用函数内延迟导入：quota.py 的模型段需要本模块的
# 异常类与 FamilyRecorder，顶层互相导入会成环（diagnostics ↔ quota）。

def derive_stream_bounds(config: ScenarioConfig) -> list[str]:
    """§8.1 一次性派生检查：聚合全部错误、不因前一错误短路。

    跨度检查使用实际 slot length domain 与 crossed count（§8.1 末段），因此
    ``session_max_len``、frame gap 与 session max span 的关联问题一次全部出现。
    在 §6.2 编译顺序里先于 quota solve 执行，target 数取逐类静态下界
    （多张 quota 引用同一 class 时 occurrence 共享，下界 = 各表对该类要量的
    最大值；精确值由 QuotaCompiler 模型决定）。

    @param config 冻结的 ``ScenarioConfig``
    @return 英文稳定错误串列表（供 M1 聚合）；干净时为空
    """
    errors: list[str] = []
    errors.extend(_quota_static_errors(config))
    targets = _lower_class_targets(config)
    errors.extend(_reference_errors(config, targets))
    errors.extend(_length_and_crossing_errors(config, targets))
    errors.extend(_capacity_errors(config, targets))
    errors.extend(_time_fit_errors(config, targets))
    errors.extend(_wave5_field_errors(config))
    return errors


def _quota_static_errors(config: ScenarioConfig) -> list[str]:
    """quota 静态算术：形态错误、exact cohort 兼容与无合法日 bucket。"""
    from labelkit.common.runtime.scenario.quota import (
        quota_bucket_values,
        unsatisfiable_buckets,
    )
    errors: list[str] = []
    for quota in config.quotas:
        errors.extend(_exact_cohort_errors(quota))
        try:
            values = quota_bucket_values(quota)
        except ValueError as exc:
            errors.append(f"quota {quota.name!r}: {exc}")
            continue
        bucket_total = sum(value for _, value in values)
        for bucket in unsatisfiable_buckets(quota, config.schedule):
            errors.append(f"quota {quota.name!r} bucket {bucket!r} has no legal "
                          f"schedule date but requires {bucket_total} sequence(s)")
    return errors


def _exact_cohort_errors(quota) -> list[str]:
    """exact weights 形态的 total 与最小 cohort 兼容检查（§13.3 黄金）。"""
    from labelkit.common.runtime.scenario.quota import (
        minimum_exact_cohort,
        nearest_exact_totals,
    )
    if not quota.weights or quota.allocation != "exact":
        return []
    cohort = minimum_exact_cohort(quota.weights)
    total = int(quota.total or 0)
    if total % cohort == 0:
        return []
    below, above = nearest_exact_totals(total, cohort)
    nearest = f"{below}/{above}" if below is not None else f"none/{above}"
    return [f"quota {quota.name!r}: exact total {total} is not a multiple of the "
            f"minimum exact cohort {cohort}; nearest multiples: {nearest}"]


def _lower_class_targets(config: ScenarioConfig) -> dict[str, int]:
    """逐类静态 target 下界 = 各 quota 对该类静态总量取最大（occurrence 共享）。"""
    from labelkit.common.runtime.scenario.quota import static_class_targets
    targets: dict[str, int] = {}
    for quota in config.quotas:
        try:
            rows = static_class_targets(quota, config.schedule)
        except ValueError:
            continue
        for name, value in rows:
            targets[name] = max(targets.get(name, 0), value)
    return targets


def _reference_errors(config: ScenarioConfig,
                      targets: Mapping[str, int]) -> list[str]:
    """quota/rule/window/noise 的名称引用域检查。"""
    from labelkit.common.runtime.scenario.quota import static_class_targets
    errors: list[str] = []
    class_names = {spec.name for spec in config.sequence_classes}
    for quota in config.quotas:
        try:
            rows = static_class_targets(quota, config.schedule)
        except ValueError:
            continue
        for name, _ in rows:
            if name not in class_names:
                errors.append(f"quota {quota.name!r} references unknown sequence "
                              f"class {name!r}")
    for rule in config.sequence_rules:
        for name in (rule.source, rule.target):
            if name not in class_names:
                errors.append(f"sequence rule {rule.name!r} references unknown "
                              f"sequence class {name!r}")
    errors.extend(_noise_reference_errors(config))
    return errors


def _noise_reference_errors(config: ScenarioConfig) -> list[str]:
    """noise 表引用、ratio 域与 task 候选域非空检查（§4.8）。"""
    errors: list[str] = []
    ratio = config.noise_ratio
    if not (Decimal(0) <= ratio < Decimal(1)):
        errors.append(f"noise_ratio must satisfy 0 <= value < 1, got {ratio}")
    frame_names = {spec.name for spec in config.frame_classes}
    noise_names = {spec.frame_class for spec in config.noise_classes}
    if ratio > 0 and not config.noise_classes:
        errors.append("noise_ratio > 0 requires a non-empty noise class table")
    for spec in config.noise_classes:
        if spec.frame_class not in frame_names:
            errors.append(f"noise class {spec.frame_class!r} is not in the frame "
                          f"class table")
        if spec.weight <= 0:
            errors.append(f"noise class {spec.frame_class!r} weight must be a "
                          f"positive integer")
    if frame_names and not (frame_names - noise_names):
        errors.append("task frame candidate domain is empty after excluding noise "
                      "classes")
    errors.extend(_noise_exclusion_errors(config, noise_names))
    return errors


def _noise_exclusion_errors(config: ScenarioConfig,
                            noise_names: set[str]) -> list[str]:
    """noise 帧类不得出现在任何生效 tier、frame window 或 frame rule（§4.8）。"""
    errors: list[str] = []
    for spec in config.sequence_classes:
        for tier in spec.tiers:
            for name in tier.frame_classes:
                if name in noise_names:
                    errors.append(f"noise frame class {name!r} must not appear in "
                                  f"tier compositions of {spec.name!r}")
        for window in spec.frame_windows:
            if window.frame_class in noise_names:
                errors.append(f"noise frame class {window.frame_class!r} must not "
                              f"appear in frame window {window.name!r}")
        for rule in spec.frame_rules:
            for name in filter(None, (rule.frame_class, rule.source, rule.target)):
                if name in noise_names:
                    errors.append(f"noise frame class {name!r} must not appear in "
                                  f"frame rule {rule.name!r}")
    return errors


def _class_length_range(config: ScenarioConfig, name: str) -> tuple[int, int]:
    """按类名取 length_range（未知类返回 (0, 0) 供上层跳过）。"""
    for spec in config.sequence_classes:
        if spec.name == name:
            return spec.length_range
    return (0, 0)


def _length_and_crossing_errors(config: ScenarioConfig,
                                targets: Mapping[str, int]) -> list[str]:
    """target/crossed/derived session 计数、length 域与 lo>=2 覆盖检查。"""
    errors: list[str] = []
    for spec in config.sequence_classes:
        lo, hi = spec.length_range
        if targets.get(spec.name, 0) > 0 and (lo < 1 or lo > hi):
            errors.append(f"sequence class {spec.name!r} length_range must satisfy "
                          f"1 <= lo <= hi, got [{lo}, {hi}]")
    n_total = sum(targets.values())
    crossed = config.crossed_sessions
    if n_total < 1:
        errors.append("quotas must require at least one sequence in total")
    if crossed < 0:
        errors.append(f"crossed_sessions must be >= 0, got {crossed}")
    elif n_total >= 1 and crossed > n_total // 2:
        errors.append(f"crossed_sessions {crossed} exceeds floor(target/2)="
                      f"{n_total // 2} for target {n_total}")
    if crossed > 0:
        covering = sum(value for name, value in targets.items()
                       if _class_length_range(config, name)[0] >= 2)
        if covering < crossed:
            errors.append(f"crossed_sessions {crossed} requires at least {crossed} "
                          f"slots with length lo >= 2; the length domain provides "
                          f"{covering}")
    if not 0 <= config.duplicates <= max(n_total, 0):
        errors.append(f"duplicates must satisfy 0 <= value <= {n_total}, got "
                      f"{config.duplicates}")
    return errors


def _capacity_errors(config: ScenarioConfig,
                     targets: Mapping[str, int]) -> list[str]:
    """session 容量：单 owner / 最小合法 pair 的最小容量与总容量 vs noise。"""
    from labelkit.common.runtime.scenario.quota import half_even_noise_target
    errors: list[str] = []
    active = [spec for spec in config.sequence_classes
              if targets.get(spec.name, 0) > 0]
    lo_total = sum(targets.get(spec.name, 0) * spec.length_range[0]
                   for spec in config.sequence_classes)
    lo_max = max((spec.length_range[0] for spec in active), default=0)
    lo_min = min((spec.length_range[0] for spec in active), default=0)
    n_total = sum(targets.values())
    sessions = n_total - config.crossed_sessions
    max_len = config.session_max_len
    if max_len < 1:
        errors.append(f"session_max_len must be >= 1, got {max_len}")
        return errors
    need = lo_max if config.crossed_sessions == 0 else max(lo_max, 2 * lo_min)
    if need > max_len:
        errors.append(f"session_max_len {max_len} cannot hold the minimum session "
                      f"load {need} (target={n_total}, crossed="
                      f"{config.crossed_sessions}, derived sessions={sessions})")
    noise_lb = half_even_noise_target(config.noise_ratio, lo_total)
    capacity = sessions * max_len
    if n_total >= 1 and capacity < lo_total + noise_lb:
        errors.append(f"total session frame capacity {sessions}x{max_len}="
                      f"{capacity} is below the minimum task frames {lo_total} plus "
                      f"exact noise target {noise_lb}")
    return errors


def _time_fit_errors(config: ScenarioConfig,
                     targets: Mapping[str, int]) -> list[str]:
    """frame gap/session gap/max span 数值关系与 schedule 可用微秒检查。"""
    errors: list[str] = []
    schedule = config.schedule
    if schedule.end_us <= schedule.start_us:
        errors.append("schedule end must be strictly greater than start")
        return errors
    first, last = local_date_span(schedule)
    for text in schedule.exclude_dates:
        if not first <= date.fromisoformat(text) <= last:
            errors.append(f"exclude date {text!r} is outside the schedule local "
                          f"date span [{first.isoformat()}, {last.isoformat()}]")
    gap_lo, gap_hi = config.frame_gap_us
    if gap_lo < 0 or gap_lo > gap_hi:
        errors.append(f"frame_gap_us must satisfy 0 <= lo <= hi, got "
                      f"[{gap_lo}, {gap_hi}]")
    if config.session_gap_us < 1:
        errors.append(f"session_gap_us must be >= 1, got {config.session_gap_us}")
    n_total = sum(targets.values())
    active = [spec for spec in config.sequence_classes
              if targets.get(spec.name, 0) > 0]
    if n_total < 1 or not active:
        return errors
    lo_total = sum(targets.get(spec.name, 0) * spec.length_range[0]
                   for spec in config.sequence_classes)
    lo_max = max(spec.length_range[0] for spec in active)
    sessions = n_total - config.crossed_sessions
    duration = schedule.end_us - schedule.start_us
    base = ((sessions - 1) * config.session_gap_us
            + max(lo_max - 1, 0) * max(gap_lo, 1))
    if base > duration:
        errors.append(f"schedule provides {duration} us but sessions require at "
                      f"least {base} us ({sessions} sessions, session_gap_us="
                      f"{config.session_gap_us}, frame_gap_us lo={gap_lo})")
    if config.crossed_sessions == 0:
        tight = ((lo_total - n_total) * max(gap_lo, 1)
                 + (n_total - 1) * config.session_gap_us)
        if tight > duration:
            errors.append(f"schedule provides {duration} us but the zero-crossing "
                          f"stream requires at least {tight} us for {lo_total} "
                          f"frames across {n_total} sessions")
    span = config.session_max_span_us
    if span is not None and span < (lo_max - 1) * max(gap_lo, 1):
        errors.append(f"session_max_span_us {span} is below the minimum span "
                      f"{(lo_max - 1) * max(gap_lo, 1)} of a {lo_max}-frame "
                      f"sequence at frame_gap_us lo={gap_lo}")
    return errors


def _wave5_field_errors(config: ScenarioConfig) -> list[str]:
    """duration/resource/contains 前置（字段在场则查，缺省跳过；Wave 5 建模）。"""
    errors: list[str] = []
    domains = {spec.name: spec for spec in config.frame_classes}
    for name, spec in domains.items():
        if spec.duration_us is not None:
            lo, hi = spec.duration_us
            if lo < 1 or lo > hi:
                errors.append(f"frame class {name!r} duration_us must satisfy "
                              f"1 <= lo <= hi, got [{lo}, {hi}]")
        if spec.resources and spec.duration_us is None:
            errors.append(f"frame class {name!r} declares resources without "
                          f"duration_us")
        for resource in spec.resources:
            if not is_valid_name(resource):
                errors.append(f"frame class {name!r} resource {resource!r} must "
                              f"match [a-z0-9_]+")
    for seq in config.sequence_classes:
        errors.extend(_window_field_errors(seq))
        for rule in seq.frame_rules:
            errors.extend(_frame_rule_field_errors(rule, domains))
    return errors


def _window_field_errors(seq) -> list[str]:
    """frame window 的帧类引用与日内段形状检查。"""
    errors: list[str] = []
    for window in seq.frame_windows:
        if not window.of_day_us:
            errors.append(f"frame window {window.name!r} must declare at least one "
                          f"of_day_us segment")
        for lo, hi in window.of_day_us:
            if not 0 <= lo < hi:
                errors.append(f"frame window {window.name!r} of_day_us must "
                              f"satisfy 0 <= lo < hi, got [{lo}, {hi}]")
    return errors


def _frame_rule_field_errors(rule, domains) -> list[str]:
    """frame rule 的帧类引用与 contains 前置（容器须声明 duration）。"""
    errors: list[str] = []
    for name in filter(None, (rule.frame_class, rule.source, rule.target)):
        if name not in domains:
            errors.append(f"frame rule {rule.name!r} references unknown frame "
                          f"class {name!r}")
    if rule.template == "contains" and rule.source in domains \
            and domains[rule.source].duration_us is None:
        errors.append(f"contains rule {rule.name!r} source {rule.source!r} must "
                      f"declare duration_us")
    return errors

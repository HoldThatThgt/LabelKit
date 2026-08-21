"""v1.17 场景规划层（SPEC-scenario-planning §6-§8，CONTRACTS §7.19）。

本包承载 dataclass 模型、fixed-offset 日历、quota 静态算术与 QuotaCompiler
见证模型、规则纯求值器、session/crossing/noise-reserve builder、确定性 noise
分配与诊断（异常/求解器工厂/约束族差分/§8.1 派生检查）——零网络、零 M1/M6
接线。``compile_scenario`` 是唯一编译入口（planner.py），time-stream 形态由
M1 生成 ``ScenarioPlan``、M6 只读消费（Wave 4b 接线）。
"""
from __future__ import annotations

from labelkit.common.runtime.scenario.calendar import (
    DEFAULT_OF_WEEK,
    WEEKDAY_WORDS,
    day_bounds_us,
    day_segment,
    expand_period_buckets,
    legal_dates,
    local_date,
    local_date_span,
    of_week_from_words,
    out_of_range_exclusions,
    parse_offset_datetime,
    parse_schedule_spec,
    week_monday,
)
from labelkit.common.runtime.scenario.diagnostics import (
    PLANNER_ENTRY_LIMIT,
    SOLVE_DETERMINISTIC_TIME,
    FamilyRecorder,
    PlannerBudgetError,
    PlannerCapacityError,
    PlannerInfeasibleError,
    PlannerInternalError,
    derive_stream_bounds,
    enforce_model_capacity,
    format_budget_message,
    format_capacity_message,
    format_infeasible_message,
    infeasible_core_names,
    make_planner_solver,
    proto_entry_counts,
)
from labelkit.common.runtime.scenario.model import (
    CorrelationSpec,
    DuplicateLayout,
    FrameClassDomain,
    FrameLayout,
    FrameRuleSpec,
    FrameWindowSpec,
    NoiseClassSpec,
    NoiseSlot,
    PlannerFamilyStats,
    PlannerModelStats,
    PlannerObjectives,
    QuotaSpec,
    QuotaSummary,
    ScenarioConfig,
    ScenarioPlan,
    ScheduleSpec,
    SequenceClassDomain,
    SequenceLayout,
    SequenceRuleSpec,
    SequenceSlotSpec,
    SessionLayout,
    TierDomain,
    frozen_sorted_mapping,
)
from labelkit.common.runtime.scenario.noise import (
    NoiseAllocationSpec,
    NoiseSessionSpan,
    allocate_noise,
    apportion_noise_classes,
)
from labelkit.common.runtime.scenario.planner import compile_scenario
from labelkit.common.runtime.scenario.quota import (
    QuotaSolution,
    allocate_weights,
    half_even_noise_target,
    minimum_exact_cohort,
    nearest_exact_totals,
    normalize_weights,
    quota_bucket_values,
    quota_static_summary,
    solve_quota_targets,
    static_class_targets,
    unsatisfiable_buckets,
    validate_quota_spec,
)
from labelkit.common.runtime.scenario.rules import (
    EvalFrame,
    EvalOccurrence,
    RuleVerdict,
    canonical_equal,
    evaluate_frame_rule,
    evaluate_sequence_rule,
    is_valid_name,
    name_domain_violations,
    validate_frame_rule,
    validate_sequence_rule,
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

__all__ = (
    # model（CONTRACTS §7.19.4 冻结 22 类）
    "CorrelationSpec", "DuplicateLayout", "FrameClassDomain", "FrameLayout",
    "FrameRuleSpec", "FrameWindowSpec", "NoiseClassSpec", "NoiseSlot",
    "PlannerFamilyStats", "PlannerModelStats", "PlannerObjectives", "QuotaSpec",
    "QuotaSummary", "ScenarioConfig", "ScenarioPlan", "ScheduleSpec",
    "SequenceClassDomain", "SequenceLayout", "SequenceRuleSpec", "SequenceSlotSpec",
    "SessionLayout", "TierDomain", "frozen_sorted_mapping",
    # calendar
    "DEFAULT_OF_WEEK", "WEEKDAY_WORDS", "day_bounds_us", "day_segment",
    "expand_period_buckets", "legal_dates", "local_date", "local_date_span",
    "of_week_from_words", "out_of_range_exclusions", "parse_offset_datetime",
    "parse_schedule_spec", "week_monday",
    # quota（静态算术 + QuotaCompiler 见证模型）
    "QuotaSolution", "allocate_weights", "half_even_noise_target",
    "minimum_exact_cohort", "nearest_exact_totals", "normalize_weights",
    "quota_bucket_values", "quota_static_summary", "solve_quota_targets",
    "static_class_targets", "unsatisfiable_buckets", "validate_quota_spec",
    # rules
    "EvalFrame", "EvalOccurrence", "RuleVerdict", "canonical_equal",
    "evaluate_frame_rule", "evaluate_sequence_rule", "is_valid_name",
    "name_domain_violations", "validate_frame_rule", "validate_sequence_rule",
    # sessions（owner permutation / bounds / crossing / noise reserve）
    "NoiseReserve", "NoiseReserveSpec", "SessionBuildSpec", "SessionLayer",
    "SlotTimeline", "build_crossing_witness", "build_noise_reserve",
    "build_session_layer",
    # noise（确定性分配）
    "NoiseAllocationSpec", "NoiseSessionSpan", "allocate_noise",
    "apportion_noise_classes",
    # planner（唯一编译入口）
    "compile_scenario",
    # diagnostics
    "PLANNER_ENTRY_LIMIT", "SOLVE_DETERMINISTIC_TIME", "FamilyRecorder",
    "PlannerBudgetError", "PlannerCapacityError", "PlannerInfeasibleError",
    "PlannerInternalError", "derive_stream_bounds", "enforce_model_capacity",
    "format_budget_message", "format_capacity_message", "format_infeasible_message",
    "infeasible_core_names", "make_planner_solver", "proto_entry_counts",
)

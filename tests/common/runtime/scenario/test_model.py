"""v1.17 scenario model 22 个冻结 dataclass 的结构测试（CONTRACTS §7.19.4）。"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
from types import MappingProxyType
from typing import get_type_hints

import pytest

from labelkit.common.runtime.scenario import (
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
)

# CONTRACTS §7.19.4 冻结字段序（逐字核对的自检锚）。
FIELD_ORDER = {
    "ScheduleSpec": ("start_us", "end_us", "utc_offset_minutes", "exclude_dates"),
    "QuotaSpec": ("name", "period", "of_week", "counts", "total", "weights", "allocation"),
    "CorrelationSpec": ("source_field", "target_field"),
    "FrameRuleSpec": ("name", "template", "frame_class", "source", "target", "count",
                      "time_us", "correlation"),
    "FrameWindowSpec": ("name", "frame_class", "of_day_us", "of_week"),
    "SequenceRuleSpec": ("name", "template", "source", "target", "period", "gap_us"),
    "TierDomain": ("rank", "weight", "frame_classes"),
    "SequenceClassDomain": ("name", "length_range", "tiers", "frame_rules", "frame_windows",
                            "sequence_rules"),
    "FrameClassDomain": ("name", "duration_us", "resources"),
    "NoiseClassSpec": ("frame_class", "weight"),
    "ScenarioConfig": ("seed", "schedule", "quotas", "sequence_classes", "frame_classes",
                       "sequence_rules", "crossed_sessions", "frame_gap_us", "session_gap_us",
                       "session_max_len", "session_max_span_us", "noise_ratio",
                       "noise_classes", "duplicates"),
    "SequenceSlotSpec": ("key", "sequence_class", "class_ordinal", "tier_rank",
                         "length_target", "length_range"),
    "FrameLayout": ("position", "frame_class", "start_us", "end_us", "duration_target_us",
                    "resources"),
    "SequenceLayout": ("slot_key", "session_index", "owner_role", "anchor_date", "start_us",
                       "last_point_us", "end_us", "frames"),
    "SessionLayout": ("index", "primary_slot_key", "secondary_slot_key", "start_us",
                      "last_point_us", "end_us", "noise_count"),
    "NoiseSlot": ("key", "frame_class", "class_ordinal", "session_index", "timestamp_us"),
    "DuplicateLayout": ("key", "ordinal", "source_slot_key", "session_index", "offset_us",
                        "frames"),
    "QuotaSummary": ("name", "period", "bucket", "sequence_class", "target"),
    "PlannerObjectives": ("preference_deviation", "calendar_days_spanned", "timeline_end_us"),
    "PlannerFamilyStats": ("variables", "constraints"),
    "PlannerModelStats": ("variables", "constraints", "families"),
    "ScenarioPlan": ("slots", "layouts", "sessions", "noise_slots", "duplicates",
                     "quota_summary", "objectives", "models", "plan_digest"),
}

ALL_CLASSES = (
    ScheduleSpec, QuotaSpec, CorrelationSpec, FrameRuleSpec, FrameWindowSpec,
    SequenceRuleSpec, TierDomain, SequenceClassDomain, FrameClassDomain, NoiseClassSpec,
    ScenarioConfig, SequenceSlotSpec, FrameLayout, SequenceLayout, SessionLayout, NoiseSlot,
    DuplicateLayout, QuotaSummary, PlannerObjectives, PlannerFamilyStats, PlannerModelStats,
    ScenarioPlan,
)


def test_exactly_twenty_two_classes_and_field_table_covers_all():
    """冻结块是 22 个 dataclass，字段表与类集合一一对应。"""
    assert len(ALL_CLASSES) == 22
    assert {cls.__name__ for cls in ALL_CLASSES} == set(FIELD_ORDER)
    assert len(FIELD_ORDER) == 22


@pytest.mark.parametrize("cls", ALL_CLASSES, ids=lambda cls: cls.__name__)
def test_dataclass_is_frozen_and_field_order_matches_contracts(cls):
    """每个 dataclass 冻结且字段名与顺序逐字等于 CONTRACTS §7.19.4。"""
    names = tuple(field.name for field in cls.__dataclass_fields__.values())
    assert names == FIELD_ORDER[cls.__name__]
    instance_names = tuple(f.name for f in getattr(cls, "__dataclass_fields__", {}).values())
    assert instance_names == FIELD_ORDER[cls.__name__]


@pytest.mark.parametrize("cls", ALL_CLASSES, ids=lambda cls: cls.__name__)
def test_docstrings_are_chinese_verbatim_carriers(cls):
    """每个冻结 dataclass 都携带中文 docstring（契约逐字载体）。"""
    assert cls.__doc__ and "。" in cls.__doc__


def test_optional_rule_fields_default_to_none():
    """FrameRuleSpec/SequenceRuleSpec 的可选字段缺省为 None。"""
    frame_rule = FrameRuleSpec("app_contains_screen", "contains",
                               source="app_usage", target="screen_evidence")
    assert frame_rule.frame_class is None
    assert frame_rule.count is None
    assert frame_rule.time_us is None
    assert frame_rule.correlation is None
    sequence_rule = SequenceRuleSpec("navigate_before_clock_out", "precedence",
                                     "navigate_home", "clock_out", "day")
    assert sequence_rule.gap_us is None


def test_frozen_assignment_raises():
    """冻结实例不可赋值。"""
    spec = ScheduleSpec(0, 1, 480, ())
    with pytest.raises(FrozenInstanceError):
        spec.start_us = 5  # type: ignore[misc]


def _plan_models() -> dict[str, PlannerModelStats]:
    """构造两个 model stats 供 Mapping 测试。"""
    return {
        "timeline": PlannerModelStats(3, 4, {"crossing": PlannerFamilyStats(1, 2)}),
        "quota": PlannerModelStats(1, 1, {}),
    }


def test_planner_model_stats_families_is_sorted_readonly_proxy():
    """families 构造点复制为按 key 排序的只读 mapping。"""
    stats = PlannerModelStats(3, 4, {"timeline": PlannerFamilyStats(1, 2),
                                     "quota": PlannerFamilyStats(1, 1)})
    assert isinstance(stats.families, MappingProxyType)
    assert tuple(stats.families) == ("quota", "timeline")
    with pytest.raises(TypeError):
        stats.families["new"] = PlannerFamilyStats(0, 0)  # type: ignore[index]


def test_scenario_plan_models_is_sorted_readonly_proxy():
    """ScenarioPlan.models 同样按 key 排序只读。"""
    plan = ScenarioPlan(
        slots=(), layouts=(), sessions=(), noise_slots=(), duplicates=(),
        quota_summary=(QuotaSummary("q", "schedule", "schedule", "mail", 1),),
        objectives=PlannerObjectives(0, 1, 2),
        models=_plan_models(),
        plan_digest="sha256:0",
    )
    assert isinstance(plan.models, MappingProxyType)
    assert tuple(plan.models) == ("quota", "timeline")
    with pytest.raises(TypeError):
        plan.models["extra"] = PlannerModelStats(0, 0, {})  # type: ignore[index]


def test_scenario_config_is_constructible_with_all_fourteen_fields():
    """ScenarioConfig 十四字段可整体构造（含 Decimal noise_ratio）。"""
    config = ScenarioConfig(
        seed=1,
        schedule=ScheduleSpec(0, 1, 0, ()),
        quotas=(QuotaSpec("q", "schedule", tuple(range(1, 8)), (("mail", 1),),
                          None, (), None),),
        sequence_classes=(SequenceClassDomain("mail", (2, 4), (), (), ()),),
        frame_classes=(FrameClassDomain("task_request", None, ()),),
        sequence_rules=(),
        crossed_sessions=0,
        frame_gap_us=(1, 2),
        session_gap_us=60_000_000,
        session_max_len=32,
        session_max_span_us=None,
        noise_ratio=Decimal("0.1"),
        noise_classes=(NoiseClassSpec("noise_frame", 1),),
        duplicates=0,
    )
    hints = get_type_hints(ScenarioConfig)
    assert len(hints) == 14
    assert config.noise_ratio == Decimal("0.1")


def test_layout_and_slot_types_carry_frozen_defaults_shape():
    """布局类字段形状（tier_rank 可空、frames 元组）与契约一致。"""
    slot = SequenceSlotSpec("sequence:mail:0", "mail", 0, None, 3, (2, 4))
    assert slot.tier_rank is None
    frame = FrameLayout(0, "task_request", 10, 10, None, ())
    assert frame.end_us == frame.start_us
    layout = SequenceLayout("sequence:mail:0", 0, "primary", "2026-01-05", 10, 20, 30,
                            (frame,))
    assert layout.owner_role == "primary"
    session = SessionLayout(0, "sequence:mail:0", None, 10, 20, 30, 0)
    assert session.secondary_slot_key is None
    noise = NoiseSlot("noise:noise_frame:0", "noise_frame", 0, 0, 10)
    duplicate = DuplicateLayout("duplicate:0", 0, "sequence:mail:0", 1, 5, (frame,))
    assert duplicate.frames == (frame,)
    assert noise.timestamp_us == 10


def test_correlation_spec_is_positional_two_field():
    """CorrelationSpec 只有 source_field/target_field（operator 已删除）。"""
    corr = CorrelationSpec("subject_id", "subject_id")
    assert corr == CorrelationSpec("subject_id", "subject_id")
    names = tuple(f.name for f in CorrelationSpec.__dataclass_fields__.values())
    assert names == ("source_field", "target_field")

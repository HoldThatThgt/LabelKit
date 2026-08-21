"""v1.17 场景模型 dataclass 层（CONTRACTS §7.19.4 逐字冻结）。

22 个 frozen dataclass 是 compile_scenario 的输入/输出载体；字段名、顺序、
默认值与中文 docstring 都以 CONTRACTS §7.19.4 为唯一真值，不得增删改序。
Mapping 字段在构造点复制为按 key 排序的只读 mapping（SPEC-SP §6.1 末段），
冻结 dataclass 内不得藏可变 dict/list。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal, Mapping


def frozen_sorted_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """构造点复制为按 key 排序的只读 mapping。

    @param value 任意 mapping 输入
    @return 键升序排列的 ``MappingProxyType`` 只读视图
    """
    return MappingProxyType({key: value[key] for key in sorted(value)})


@dataclass(frozen=True)
class ScheduleSpec:
    """planner 使用的有限 fixed-offset schedule。"""

    start_us: int
    end_us: int
    utc_offset_minutes: int
    exclude_dates: tuple[str, ...]


@dataclass(frozen=True)
class QuotaSpec:
    """一张尚待展开 period bucket 的 quota。"""

    name: str
    period: Literal["day", "week", "schedule"]
    of_week: tuple[int, ...]
    counts: tuple[tuple[str, int], ...]
    total: int | None
    weights: tuple[tuple[str, int], ...]
    allocation: Literal["exact", "largest_remainder"] | None


@dataclass(frozen=True)
class CorrelationSpec:
    """frame rule 的类型敏感顶层字段相等约束。"""

    source_field: str
    target_field: str


@dataclass(frozen=True)
class FrameRuleSpec:
    """一条带自然名称的同序列有限迹规则。"""

    name: str
    template: str
    frame_class: str | None = None
    source: str | None = None
    target: str | None = None
    count: int | None = None
    time_us: tuple[int, int] | None = None
    correlation: CorrelationSpec | None = None


@dataclass(frozen=True)
class FrameWindowSpec:
    """一条带自然名称的 frame class 本地日历窗口。"""

    name: str
    frame_class: str
    of_day_us: tuple[tuple[int, int], ...]
    of_week: tuple[int, ...]


@dataclass(frozen=True)
class SequenceRuleSpec:
    """一条跨 sequence occurrence 的周期规则。"""

    name: str
    template: Literal["precedence", "response", "succession", "not_co_existence"]
    source: str
    target: str
    period: Literal["day", "week", "schedule"]
    gap_us: tuple[int, int] | None = None


@dataclass(frozen=True)
class TierDomain:
    """一个 sequence class 的一档 frame class 构成。"""

    rank: int
    weight: int
    frame_classes: tuple[str, ...]


@dataclass(frozen=True)
class SequenceClassDomain:
    """一个 sequence class 的完整生效 planner 输入。"""

    name: str
    length_range: tuple[int, int]
    tiers: tuple[TierDomain, ...]
    frame_rules: tuple[FrameRuleSpec, ...]
    frame_windows: tuple[FrameWindowSpec, ...]
    # 按类生效的跨 sequence 规则；空元组表示该类不声明约束
    sequence_rules: tuple[SequenceRuleSpec, ...] = ()


@dataclass(frozen=True)
class FrameClassDomain:
    """一个 frame class 的时间与 resource 域。"""

    name: str
    duration_us: tuple[int, int] | None
    resources: tuple[str, ...]


@dataclass(frozen=True)
class NoiseClassSpec:
    """一个 structured noise frame class 及其整数权重。"""

    frame_class: str
    weight: int


@dataclass(frozen=True)
class ScenarioConfig:
    """compile_scenario 的唯一冻结参数对象。"""

    seed: int
    schedule: ScheduleSpec
    quotas: tuple[QuotaSpec, ...]
    sequence_classes: tuple[SequenceClassDomain, ...]
    frame_classes: tuple[FrameClassDomain, ...]
    sequence_rules: tuple[SequenceRuleSpec, ...]
    crossed_sessions: int
    frame_gap_us: tuple[int, int]
    session_gap_us: int
    session_max_len: int
    session_max_span_us: int | None
    noise_ratio: Decimal
    noise_classes: tuple[NoiseClassSpec, ...]
    duplicates: int


@dataclass(frozen=True)
class SequenceSlotSpec:
    """QuotaCompiler 冻结 target 后的一条稳定成功交付槽位。"""

    key: str
    sequence_class: str
    class_ordinal: int
    tier_rank: int | None
    length_target: int
    length_range: tuple[int, int]


@dataclass(frozen=True)
class FrameLayout:
    """一条已选中的 active frame occurrence 布局。"""

    position: int
    frame_class: str
    start_us: int
    end_us: int
    duration_target_us: int | None
    resources: tuple[str, ...]


@dataclass(frozen=True)
class SequenceLayout:
    """一条 sequence slot 的完整时间与 session 布局。"""

    slot_key: str
    session_index: int
    owner_role: Literal["primary", "secondary"]
    anchor_date: str
    start_us: int
    last_point_us: int
    end_us: int
    frames: tuple[FrameLayout, ...]


@dataclass(frozen=True)
class SessionLayout:
    """一个 replay session 的 owner、边界与 noise 数量。"""

    index: int
    primary_slot_key: str
    secondary_slot_key: str | None
    start_us: int
    last_point_us: int
    end_us: int
    noise_count: int


@dataclass(frozen=True)
class NoiseSlot:
    """一条已冻结 class、session 与时间的 noise 交付槽位。"""

    key: str
    frame_class: str
    class_ordinal: int
    session_index: int
    timestamp_us: int


@dataclass(frozen=True)
class DuplicateLayout:
    """一条已冻结 source 与平移后时间的流尾 duplicate。"""

    key: str
    ordinal: int
    source_slot_key: str
    session_index: int
    offset_us: int
    frames: tuple[FrameLayout, ...]


@dataclass(frozen=True)
class QuotaSummary:
    """一张 quota 展开后的一个 class/bucket target。"""

    name: str
    period: Literal["day", "week", "schedule"]
    bucket: str
    sequence_class: str
    target: int


@dataclass(frozen=True)
class PlannerObjectives:
    """三层字典序目标的冻结最优值。"""

    preference_deviation: int
    calendar_days_spanned: int
    timeline_end_us: int


@dataclass(frozen=True)
class PlannerFamilyStats:
    """一个约束族对模型规模的增量。"""

    variables: int
    constraints: int


@dataclass(frozen=True)
class PlannerModelStats:
    """quota 或 timeline 模型的稳定规模统计。"""

    variables: int
    constraints: int
    families: Mapping[str, PlannerFamilyStats]

    def __post_init__(self) -> None:
        """families 复制为按 key 排序的只读 mapping。"""
        object.__setattr__(self, "families",
                           frozen_sorted_mapping(self.families))


@dataclass(frozen=True)
class ScenarioPlan:
    """M1 唯一生成、estimate 与 M6 只读消费的冻结计划。"""

    slots: tuple[SequenceSlotSpec, ...]
    layouts: tuple[SequenceLayout, ...]
    sessions: tuple[SessionLayout, ...]
    noise_slots: tuple[NoiseSlot, ...]
    duplicates: tuple[DuplicateLayout, ...]
    quota_summary: tuple[QuotaSummary, ...]
    objectives: PlannerObjectives
    models: Mapping[str, PlannerModelStats]
    plan_digest: str

    def __post_init__(self) -> None:
        """models 复制为按 key 排序的只读 mapping。"""
        object.__setattr__(self, "models", frozen_sorted_mapping(self.models))

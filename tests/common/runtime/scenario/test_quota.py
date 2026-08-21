"""v1.17 scenario quota 静态算术测试（SPEC-SP §4.4 黄金数字）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from labelkit.common.runtime.scenario import (
    QuotaSpec,
    QuotaSummary,
    ScheduleSpec,
    allocate_weights,
    minimum_exact_cohort,
    nearest_exact_totals,
    normalize_weights,
    quota_bucket_values,
    quota_static_summary,
    static_class_targets,
    unsatisfiable_buckets,
    validate_quota_spec,
)

_WEIGHTS = (("shopping", 60), ("entertainment", 25), ("fitness", 15))
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _at(day: str, hour: int) -> int:
    """+08:00 本地时刻的绝对微秒。"""
    from datetime import date

    parsed = date.fromisoformat(day)
    moment = datetime(parsed.year, parsed.month, parsed.day, hour,
                      tzinfo=timezone(timedelta(hours=8)))
    return (moment.astimezone(timezone.utc) - _EPOCH) // timedelta(microseconds=1)


def _schedule(first: str, last: str) -> ScheduleSpec:
    """构造覆盖 first..last 本地日的 schedule（末日本地午夜排他）。"""
    from datetime import date

    final_day = date.fromisoformat(last) + timedelta(days=1)
    end_text = final_day.isoformat()
    return ScheduleSpec(_at(first, 0), _at(end_text, 0), 480, ())


def _counts_quota(name: str, period: str, counts: tuple[tuple[str, int], ...],
                  of_week: tuple[int, ...] = ()) -> QuotaSpec:
    """counts 形态 quota 构造助手。"""
    return QuotaSpec(name, period, of_week, counts, None, (), None)


def _weights_quota(name: str, period: str, total: int,
                   weights: tuple[tuple[str, int], ...],
                   allocation: str) -> QuotaSpec:
    """weights 形态 quota 构造助手。"""
    return QuotaSpec(name, period, (), (), total, weights, allocation)


def test_golden_gcd_normalize_and_cohort():
    """GCD(60,25,15)=5 ⇒ 最简比 12/5/3 ⇒ cohort 20。"""
    assert normalize_weights(_WEIGHTS) == (
        ("shopping", 12), ("entertainment", 5), ("fitness", 3))
    assert minimum_exact_cohort(_WEIGHTS) == 20


def test_golden_nearest_exact_totals():
    """total 9 ⇒ nearest null/20；total 21 ⇒ 20/40（§13.3）。"""
    assert nearest_exact_totals(9, 20) == (None, 20)
    assert nearest_exact_totals(21, 20) == (20, 40)
    assert nearest_exact_totals(20, 20) == (None, 40)


def test_exact_allocation_multiplies_normalized_ratio():
    """exact：total 是 cohort 整数倍时按最简比放大。"""
    assert allocate_weights(20, _WEIGHTS, "exact") == (
        ("shopping", 12), ("entertainment", 5), ("fitness", 3))
    assert allocate_weights(40, _WEIGHTS, "exact") == (
        ("shopping", 24), ("entertainment", 10), ("fitness", 6))
    with pytest.raises(ValueError, match="cohort"):
        allocate_weights(9, _WEIGHTS, "exact")


def test_largest_remainder_pure_integer_golden():
    """最大余额纯整数 golden：total 7、60/25/15 ⇒ 4/2/1。"""
    assert allocate_weights(7, _WEIGHTS, "largest_remainder") == (
        ("shopping", 4), ("entertainment", 2), ("fitness", 1))


def test_largest_remainder_tie_breaks_by_declaration_order():
    """平票按表内 class 声明序（a 先声明得多余 1）。"""
    weights = (("a", 1), ("b", 1))
    assert allocate_weights(3, weights, "largest_remainder") == (("a", 2), ("b", 1))


def test_validate_quota_spec_form_exclusivity():
    """counts 与 total/weights/allocation 互斥；weights ≥2 类正整数；total ≥1。"""
    validate_quota_spec(_counts_quota("weekday", "day", (("mail", 1),)))
    validate_quota_spec(_weights_quota("mix", "schedule", 20, _WEIGHTS, "exact"))
    with pytest.raises(ValueError, match="exclusive"):
        validate_quota_spec(QuotaSpec("bad", "day", (), (("mail", 1),), 5, (), None))
    with pytest.raises(ValueError, match="exclusive"):
        validate_quota_spec(QuotaSpec("bad", "day", (), (("mail", 1),), None,
                                      (("a", 1),), None))
    with pytest.raises(ValueError, match="two"):
        validate_quota_spec(_weights_quota("bad", "schedule", 5, (("a", 1),), "exact"))
    with pytest.raises(ValueError, match="positive"):
        validate_quota_spec(_weights_quota("bad", "schedule", 5,
                                           (("a", 1), ("b", 0)), "exact"))
    with pytest.raises(ValueError, match="total"):
        validate_quota_spec(_weights_quota("bad", "schedule", 0, _WEIGHTS, "exact"))
    with pytest.raises(ValueError, match="allocation"):
        validate_quota_spec(_weights_quota("bad", "schedule", 20, _WEIGHTS, None))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="name"):
        validate_quota_spec(_counts_quota("Bad-Name", "day", (("mail", 1),)))
    with pytest.raises(ValueError, match="period"):
        validate_quota_spec(_counts_quota("q", "month", (("mail", 1),)))


def test_quota_bucket_values_counts_direct_and_weights_allocated():
    """bucket 内逐类值：counts 直取；weights 按 allocation 化逐类整数。"""
    assert quota_bucket_values(_counts_quota("weekday", "day", (("mail", 1),))) == (
        ("mail", 1),)
    assert quota_bucket_values(
        _weights_quota("mix", "schedule", 20, _WEIGHTS, "exact")) == (
        ("shopping", 12), ("entertainment", 5), ("fitness", 3))


def test_day_period_summary_sums_per_bucket_counts():
    """day quota：三日 schedule 每日一行，逐类静态 target = Σ bucket 值。"""
    schedule = _schedule("2026-01-05", "2026-01-07")
    quota = _counts_quota("weekday", "day", (("mail", 1), ("calendar", 2)))
    summary = quota_static_summary(quota, schedule)
    assert [(row.bucket, row.sequence_class, row.target) for row in summary] == [
        ("2026-01-05", "mail", 1), ("2026-01-05", "calendar", 2),
        ("2026-01-06", "mail", 1), ("2026-01-06", "calendar", 2),
        ("2026-01-07", "mail", 1), ("2026-01-07", "calendar", 2),
    ]
    assert all(isinstance(row, QuotaSummary) and row.name == "weekday"
               and row.period == "day" for row in summary)
    assert static_class_targets(quota, schedule) == (("mail", 3), ("calendar", 6))


def test_week_and_schedule_period_bucket_counts():
    """week：跨两周两个 bucket；schedule：单 bucket。"""
    two_weeks = _schedule("2026-01-07", "2026-01-13")
    week_quota = _counts_quota("weekly", "week", (("mail", 1),))
    assert len(quota_static_summary(week_quota, two_weeks)) == 2
    schedule_quota = _weights_quota("mix", "schedule", 20, _WEIGHTS, "exact")
    summary = quota_static_summary(schedule_quota, two_weeks)
    assert [(row.bucket, row.sequence_class, row.target) for row in summary] == [
        ("schedule", "shopping", 12), ("schedule", "entertainment", 5),
        ("schedule", "fitness", 3)]
    assert static_class_targets(schedule_quota, two_weeks) == (
        ("shopping", 12), ("entertainment", 5), ("fitness", 3))


def test_unsatisfiable_when_bucket_has_no_legal_date_and_target_positive():
    """无合法日期且 target>0 ⇒ 不可满足判定；全零 target 不判不可满足。"""
    schedule = _schedule("2026-01-05", "2026-01-07")  # 周一..周三
    sunday_only = _counts_quota("sunday", "schedule", (("mail", 1),), of_week=(7,))
    assert unsatisfiable_buckets(sunday_only, schedule) == ("schedule",)
    zero_quota = _counts_quota("zero", "schedule", (("mail", 0),), of_week=(7,))
    assert unsatisfiable_buckets(zero_quota, schedule) == ()
    excluded_week = ScheduleSpec(_at("2026-01-05", 0), _at("2026-01-08", 0), 480,
                                 ("2026-01-05", "2026-01-06", "2026-01-07"))
    monday_only = _counts_quota("monday", "week", (("mail", 1),), of_week=(1,))
    assert unsatisfiable_buckets(monday_only, excluded_week) == ("2026-01-05",)

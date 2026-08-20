"""时间半开/闭区间、fixed-offset 日历与 duplicate 平移测试。"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from labelkit.common.runtime.temporal import (
    CalendarWindow,
    DayWindow,
    MICROSECONDS_PER_DAY,
    MICROSECONDS_PER_WEEK,
    TimeInterval,
    fixed_offset,
    in_calendar_window,
    minimal_duplicate_shift,
    normalize_calendar_window,
    parse_local_time,
    quantize_frame_gap,
    quantize_time_s,
    replay_guard,
    timestamp_datetime,
    timestamp_us,
)


def test_time_s_is_half_open_and_frame_gap_is_closed():
    """time_s 只开右端，frame_gap 两端均可取。"""
    assert quantize_time_s((Decimal("0.000001"), Decimal("2"))) == (1, 2_000_000)
    gap = quantize_frame_gap((Decimal("1.0000001"), Decimal("2.0000001")))
    assert gap.lo_us == 1_000_001 and gap.hi_us == 2_000_000
    assert gap.contains(gap.lo_us) and gap.contains(gap.hi_us)
    assert not TimeInterval(1, 2).contains(3)


def test_replay_guard_preserves_closed_endpoint_intersection():
    """显式 time_s 与 replay guard 的交集不误删可闭合端点。"""
    result = replay_guard(TimeInterval(1, 10, closed=True), 5)
    assert result.lo_us == 1 and result.hi_us == 5
    assert result.contains(5)


def test_integer_timestamp_conversion_preserves_negative_and_microseconds():
    """datetime 转换只使用整数 timedelta，不经过 float。"""
    value = -1
    decoded = timestamp_datetime(value)
    assert timestamp_us(decoded) == value
    exact = datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=timezone.utc)
    assert timestamp_datetime(timestamp_us(exact)) == exact


@pytest.mark.parametrize("value", ["-1:00", "01:-1", "01:00:00.1234567", "01:00:00.1234560"])
def test_local_time_rejects_negative_or_excess_precision(value: str):
    """日内时间不接受负字段或超过六位小数。"""
    with pytest.raises(ValueError):
        parse_local_time(value)


def test_weekday_and_multiple_same_day_windows():
    """窗口支持多段日内并集与 weekday 限制。"""
    window = normalize_calendar_window({
        "frame_class": "task",
        "of_day": [["08:00", "09:00"], ["14:00", "15:00"]],
        "of_week": ["mon", "fri"],
    })
    monday = datetime(2026, 1, 5, 8, 30, tzinfo=timezone.utc)
    tuesday = datetime(2026, 1, 6, 8, 30, tzinfo=timezone.utc)
    assert in_calendar_window(monday, window)
    assert not in_calendar_window(tuesday, window)


def test_explicit_empty_weekday_and_overlapping_windows_are_invalid():
    """显式空 weekday 与重叠窗口都属于配置错误。"""
    with pytest.raises(ValueError):
        normalize_calendar_window({"frame_class": "x", "of_day": [["00:00", "01:00"]], "of_week": []})
    with pytest.raises(ValueError):
        normalize_calendar_window({"frame_class": "x", "of_day": [["00:00", "02:00"], ["01:00", "03:00"]]})


def test_single_calendar_window_cannot_cross_local_midnight():
    """单个日内窗口不得用 start >= end 表达跨午夜。"""
    with pytest.raises(ValueError):
        normalize_calendar_window({"frame_class": "x", "of_day": [["23:00", "01:00"]]})


def test_non_fixed_timezone_is_rejected():
    """日历只接受带固定 offset 的 timestamp。"""
    with pytest.raises(ValueError):
        fixed_offset(datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Shanghai")))


def test_duplicate_shift_without_window_is_minimal_microsecond():
    """无窗口 duplicate 只满足尾部 gap 的最小正微秒平移。"""
    assert minimal_duplicate_shift((1_000_000,), 5_000_000, 2_000_000) == 6_000_001


def test_duplicate_shift_with_any_window_is_minimal_positive_week():
    """窗口表非空时即使 source class 未命中也使用整周平移。"""
    window = CalendarWindow("other", (DayWindow(0, MICROSECONDS_PER_DAY),))
    shift = minimal_duplicate_shift(((0, "task"),), 1, 1, {"other": window})
    assert shift == MICROSECONDS_PER_WEEK


def test_duplicate_shift_checks_only_matching_frame_class_windows():
    """source frame class 只匹配自身窗口，不借用其他帧类窗口。"""
    window = CalendarWindow("task", (DayWindow(0, MICROSECONDS_PER_DAY),))
    shift = minimal_duplicate_shift(((0, "task"), (1, "free")), 1, 1, {"task": window})
    assert shift == MICROSECONDS_PER_WEEK

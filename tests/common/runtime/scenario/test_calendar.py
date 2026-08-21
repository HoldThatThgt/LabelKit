"""v1.17 scenario calendar：fixed-offset schedule 解析与 period bucket 纯函数测试。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from labelkit.common.runtime.scenario import (
    ScheduleSpec,
    day_bounds_us,
    day_segment,
    expand_period_buckets,
    local_date,
    legal_dates,
    out_of_range_exclusions,
    parse_offset_datetime,
    parse_schedule_spec,
    week_monday,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _us(moment: datetime) -> int:
    """把 aware datetime 精确转成整数微秒。"""
    return (moment.astimezone(timezone.utc) - _EPOCH) // timedelta(microseconds=1)


def _at(day: str, hour: int, minute: int = 0, second: int = 0, micro: int = 0) -> datetime:
    """构造 +08:00 本地时刻。"""
    parsed = date.fromisoformat(day)
    return datetime(parsed.year, parsed.month, parsed.day, hour, minute, second, micro,
                    tzinfo=timezone(timedelta(hours=8)))


def test_parse_offset_datetime_accepts_z_and_returns_exact_microseconds():
    """Z 结尾合法且 µs 换算精确到微秒。"""
    value = _us(datetime(2026, 1, 5, 0, 0, 1, 1, tzinfo=timezone.utc))
    assert parse_offset_datetime("2026-01-05T00:00:01.000001Z") == (value, 0)
    assert parse_offset_datetime("2026-01-05T08:00:01.000001+08:00") == (value, 480)


def test_parse_offset_datetime_rejects_naive_and_bad_text():
    """naive datetime 与不可解析文本都被拒。"""
    with pytest.raises(ValueError, match="offset"):
        parse_offset_datetime("2026-01-05T14:00:00")
    with pytest.raises(ValueError):
        parse_offset_datetime("not-a-datetime")


def test_parse_schedule_spec_three_day_with_middle_exclude():
    """三日 schedule + 中间排除日：µs、offset、exclude 原样保留。"""
    spec = parse_schedule_spec(
        "2026-01-05T14:00:00+08:00", "2026-01-08T00:00:00+08:00", ("2026-01-07",))
    assert spec.start_us == _us(_at("2026-01-05", 14))
    assert spec.end_us == _us(_at("2026-01-08", 0))
    assert spec.utc_offset_minutes == 480
    assert spec.exclude_dates == ("2026-01-07",)


def test_parse_schedule_spec_rejects_equal_bounds_and_offset_mismatch():
    """end==start 与不同 offset 都拒收。"""
    with pytest.raises(ValueError, match="end"):
        parse_schedule_spec("2026-01-05T14:00:00+08:00", "2026-01-05T14:00:00+08:00")
    with pytest.raises(ValueError, match="offset"):
        parse_schedule_spec("2026-01-05T14:00:00+08:00", "2026-01-06T14:00:00+09:00")


def test_parse_schedule_spec_rejects_duplicate_and_malformed_exclude_dates():
    """重复排除日与非法日期文本拒收。"""
    with pytest.raises(ValueError, match="duplicate"):
        parse_schedule_spec("2026-01-05T00:00:00Z", "2026-01-08T00:00:00Z",
                            ("2026-01-06", "2026-01-06"))
    with pytest.raises(ValueError):
        parse_schedule_spec("2026-01-05T00:00:00Z", "2026-01-08T00:00:00Z", ("01-06",))


def test_local_date_and_day_bounds_use_fixed_offset():
    """本地日界按固定 offset 换算：+08:00 的午夜是 UTC 前一日 16:00。"""
    midnight = _us(datetime(2026, 1, 4, 16, 0, tzinfo=timezone.utc))
    assert day_bounds_us(date(2026, 1, 5), 480) == (midnight, midnight + 86_400_000_000)
    assert local_date(midnight + 1, 480) == date(2026, 1, 5)


def test_day_segment_clips_schedule_to_local_day():
    """本地日区段 = schedule 半开区间 ∩ 本地日；不相交返回 None。"""
    start = _us(_at("2026-01-05", 14))
    end = _us(_at("2026-01-06", 1))
    assert day_segment(start, end, date(2026, 1, 5), 480) == (
        _us(_at("2026-01-05", 14)), _us(_at("2026-01-06", 0)))
    assert day_segment(start, end, date(2026, 1, 6), 480) == (
        _us(_at("2026-01-06", 0)), _us(_at("2026-01-06", 1)))
    assert day_segment(start, end, date(2026, 1, 4), 480) is None


def test_day_period_buckets_honor_of_week_and_exclude():
    """day bucket：schedule 相交、未排除、of_week 命中的 local date 各一。"""
    spec = parse_schedule_spec(
        "2026-01-05T14:00:00+08:00", "2026-01-08T00:00:00+08:00", ("2026-01-07",))
    buckets = expand_period_buckets(spec, "day", tuple(range(1, 8)))
    assert [key for key, _ in buckets] == ["2026-01-05", "2026-01-06"]
    monday_only = expand_period_buckets(spec, "day", (1,))
    assert [key for key, _ in monday_only] == ["2026-01-05"]


def test_cross_midnight_schedule_touches_both_local_dates():
    """跨午夜 schedule 两天都相交。"""
    spec = parse_schedule_spec(
        "2026-01-05T23:00:00+08:00", "2026-01-06T01:00:00+08:00")
    buckets = expand_period_buckets(spec, "day", tuple(range(1, 8)))
    assert [key for key, _ in buckets] == ["2026-01-05", "2026-01-06"]


def test_week_buckets_are_iso_monday_keyed_and_span_two_weeks():
    """week bucket 按 ISO Monday 记 key，跨周 schedule 各周一一个 bucket。"""
    spec = parse_schedule_spec(
        "2026-01-07T00:00:00+08:00", "2026-01-13T00:00:00+08:00")
    buckets = expand_period_buckets(spec, "week", tuple(range(1, 8)))
    assert [key for key, _ in buckets] == ["2026-01-05", "2026-01-12"]
    first_week_dates = tuple(day.isoformat() for day in buckets[0][1])
    assert list(first_week_dates) == ["2026-01-07", "2026-01-08", "2026-01-09",
                                      "2026-01-10", "2026-01-11"]
    assert [day.isoformat() for day in buckets[1][1]] == ["2026-01-12"]


def test_week_bucket_counts_only_of_week_hits_and_exclude_does_not_cancel_week():
    """周内只统计 of_week 命中合法日；排除日不取消整周。"""
    spec = parse_schedule_spec(
        "2026-01-05T00:00:00+08:00", "2026-01-08T00:00:00+08:00", ("2026-01-05",))
    buckets = expand_period_buckets(spec, "week", (1,))
    assert len(buckets) == 1
    key, days = buckets[0]
    assert key == "2026-01-05"
    assert days == ()  # 周一被排除，bucket 仍在但合法日为空


def test_schedule_period_is_single_bucket_over_legal_dates():
    """schedule 周期整个 schedule 一次，合法日受 of_week 过滤。"""
    spec = parse_schedule_spec(
        "2026-01-05T14:00:00+08:00", "2026-01-08T00:00:00+08:00", ("2026-01-07",))
    buckets = expand_period_buckets(spec, "schedule", (2,))
    assert len(buckets) == 1
    key, days = buckets[0]
    assert key == "schedule"
    assert days == (date(2026, 1, 6),)
    assert legal_dates(spec, (2,)) == (date(2026, 1, 6),)


def test_empty_of_week_defaults_to_all_seven_days():
    """空 of_week 按『缺省周一至周日』处理。"""
    spec = parse_schedule_spec(
        "2026-01-05T00:00:00+08:00", "2026-01-07T00:00:00+08:00")
    buckets = expand_period_buckets(spec, "day", ())
    assert [key for key, _ in buckets] == ["2026-01-05", "2026-01-06"]


def test_unknown_period_rejected():
    """未知 period 拒收。"""
    spec = parse_schedule_spec("2026-01-05T00:00:00Z", "2026-01-06T00:00:00Z")
    with pytest.raises(ValueError, match="period"):
        expand_period_buckets(spec, "month", tuple(range(1, 8)))


def test_out_of_range_exclusions_flag_only_outside_local_span():
    """区间外 exclude 由判定函数点名；区间内返回空。"""
    spec = parse_schedule_spec(
        "2026-01-05T00:00:00+08:00", "2026-01-07T00:00:00+08:00",
        ("2026-01-04", "2026-01-06", "2026-01-07"))
    assert out_of_range_exclusions(spec) == ("2026-01-04", "2026-01-07")


def test_week_monday_maps_iso_weekday():
    """week_monday 返回所在 ISO 周的周一。"""
    assert week_monday(date(2026, 1, 7)) == date(2026, 1, 5)
    assert week_monday(date(2026, 1, 11)) == date(2026, 1, 5)
    assert week_monday(date(2026, 1, 12)) == date(2026, 1, 12)


def test_half_open_end_excludes_day_starting_at_end():
    """end 恰为本地午夜时该日不相交（半开区间）。"""
    spec = ScheduleSpec(
        _us(_at("2026-01-05", 0)), _us(_at("2026-01-06", 0)), 480, ())
    buckets = expand_period_buckets(spec, "day", tuple(range(1, 8)))
    assert [key for key, _ in buckets] == ["2026-01-05"]

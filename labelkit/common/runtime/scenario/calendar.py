"""v1.17 场景日历纯函数：fixed-offset schedule 解析、本地日界与 period bucket 展开。

全部零 IO、µs 域：所有 ``*_us`` 是 Unix epoch 绝对整数微秒；本地日期通过
schedule 的固定 offset 换算，绝不混入 naive datetime（SPEC-SP §4.3/§4.4）。
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone

from labelkit.common.runtime.scenario.model import ScheduleSpec

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_US_PER_DAY = 86_400_000_000
_US_PER_MINUTE = 60_000_000
#: 星期词（mon..sun）→ ISO weekday 整数（Monday=1..Sunday=7）。
WEEKDAY_WORDS: dict[str, int] = {
    "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7,
}
#: 缺省 of_week = 周一至周日（SPEC-SP §4.4）。
DEFAULT_OF_WEEK = tuple(range(1, 8))
VALID_PERIODS = ("day", "week", "schedule")


def parse_offset_datetime(text: str) -> tuple[int, int]:
    """解析必带 ``Z`` 或 numeric offset 的 ISO datetime 为 (绝对µs, offset分钟)。

    @param text ISO-8601 datetime 文本
    @return ``(绝对微秒, utc_offset_minutes)`` 二元组
    @raises ValueError 文本不可解析、naive（无 offset）、offset 不是整分钟时
    """
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid ISO datetime: {text!r}") from exc
    offset = moment.utcoffset()
    if offset is None:
        raise ValueError(f"schedule datetime must carry Z or a numeric offset: {text!r}")
    total_us = ((offset.days * 86_400 + offset.seconds) * 1_000_000
                + offset.microseconds)
    if total_us % _US_PER_MINUTE != 0:
        raise ValueError(f"schedule offset must be whole minutes: {text!r}")
    absolute = (moment - _EPOCH) // timedelta(microseconds=1)
    return absolute, total_us // _US_PER_MINUTE


def parse_schedule_spec(start_text: str, end_text: str,
                        exclude_texts: Sequence[str] = ()) -> ScheduleSpec:
    """解析有限半开 schedule 为 ``ScheduleSpec``（µs 域纯函数）。

    @param start_text 起点ISO datetime（必带 ``Z`` 或 numeric offset）
    @param end_text 终点 ISO datetime（与 start 同 offset 且严格大于 start）
    @param exclude_texts 本地自然日 ISO date 文本数组（不得重复）
    @return 冻结的 ``ScheduleSpec``
    @raises ValueError 解析失败、offset 不一致、end<=start、排除日重复或非法时
    """
    start_us, start_minutes = parse_offset_datetime(start_text)
    end_us, end_minutes = parse_offset_datetime(end_text)
    if start_minutes != end_minutes:
        raise ValueError("schedule start and end must use the same utc offset")
    if end_us <= start_us:
        raise ValueError("schedule end must be strictly greater than start")
    parsed: list[date] = []
    for text in exclude_texts:
        try:
            parsed.append(date.fromisoformat(text))
        except ValueError as exc:
            raise ValueError(f"invalid exclude date: {text!r}") from exc
    if len(set(parsed)) != len(parsed):
        raise ValueError("schedule exclude_dates must not contain duplicates")
    return ScheduleSpec(start_us, end_us, start_minutes, tuple(exclude_texts))


def _offset_timezone(utc_offset_minutes: int) -> timezone:
    """固定 offset 分钟 → timezone。"""
    return timezone(timedelta(minutes=utc_offset_minutes))


def local_date(us: int, utc_offset_minutes: int) -> date:
    """绝对微秒 → schedule 本地自然日。

    @param us 绝对微秒
    @param utc_offset_minutes 固定 offset 分钟
    @return 本地 ``date``
    """
    shifted = _EPOCH + timedelta(microseconds=us + utc_offset_minutes * _US_PER_MINUTE)
    return shifted.date()


def day_bounds_us(day: date, utc_offset_minutes: int) -> tuple[int, int]:
    """本地自然日的 [起点µs, 日界µs) 半开区间。

    @param day 本地 ``date``
    @param utc_offset_minutes 固定 offset 分钟
    @return ``(起点微秒, 终点微秒)``，终点 = 次日本地午夜
    """
    midnight = datetime(day.year, day.month, day.day,
                        tzinfo=_offset_timezone(utc_offset_minutes))
    start = (midnight - _EPOCH) // timedelta(microseconds=1)
    return start, start + _US_PER_DAY


def day_segment(start_us: int, end_us: int, day: date,
                utc_offset_minutes: int) -> tuple[int, int] | None:
    """schedule 半开区间与本地自然日的交集区段。

    @param start_us schedule 起点微秒
    @param end_us schedule 终点微秒（排他）
    @param day 本地 ``date``
    @param utc_offset_minutes 固定 offset 分钟
    @return 非空交集 ``(lo, hi)``；不相交时 ``None``
    """
    day_start, day_end = day_bounds_us(day, utc_offset_minutes)
    lo, hi = max(start_us, day_start), min(end_us, day_end)
    return (lo, hi) if lo < hi else None


def local_date_span(spec: ScheduleSpec) -> tuple[date, date]:
    """schedule 半开区间触及的首末本地自然日（end 取 end-1µs）。

    @param spec 冻结 schedule
    @return ``(首日, 末日)``；区间恒非空
    """
    return (local_date(spec.start_us, spec.utc_offset_minutes),
            local_date(spec.end_us - 1, spec.utc_offset_minutes))


def out_of_range_exclusions(spec: ScheduleSpec) -> tuple[str, ...]:
    """点名落在 schedule 本地日范围之外的排除日（报错定位由 M1 决定）。

    @param spec 冻结 schedule
    @return 越界排除日文本元组（按声明序）
    """
    first, last = local_date_span(spec)
    flagged = [text for text in spec.exclude_dates
               if not first <= date.fromisoformat(text) <= last]
    return tuple(flagged)


def week_monday(day: date) -> date:
    """返回 ``day`` 所在 ISO 周的周一。

    @param day 本地 ``date``
    @return 该周的周一 ``date``
    """
    return day - timedelta(days=day.isoweekday() - 1)


def of_week_from_words(words: Sequence[str]) -> tuple[int, ...]:
    """星期词序列 → ISO weekday 整数元组。

    @param words ``mon``..``sun`` 星期词序列
    @return 对应 ISO weekday 整数元组
    @raises ValueError 出现未知星期词时
    """
    try:
        return tuple(WEEKDAY_WORDS[word] for word in words)
    except KeyError as exc:
        raise ValueError(f"unknown weekday word: {exc.args[0]!r}") from exc


def legal_dates(spec: ScheduleSpec, of_week: tuple[int, ...]) -> tuple[date, ...]:
    """schedule 内、未排除、of_week 命中的本地自然日（升序）。

    @param spec 冻结 schedule
    @param of_week ISO weekday 集合；空元组按缺省周一至周日处理
    @return 合法日 ``date`` 元组
    """
    effective = of_week or DEFAULT_OF_WEEK
    first, last = local_date_span(spec)
    excluded = {date.fromisoformat(text) for text in spec.exclude_dates}
    days: list[date] = []
    cursor = first
    while cursor <= last:
        if cursor not in excluded and cursor.isoweekday() in effective:
            days.append(cursor)
        cursor += timedelta(days=1)
    return tuple(days)


def expand_period_buckets(spec: ScheduleSpec, period: str,
                         of_week: tuple[int, ...]) -> tuple[tuple[str, tuple[date, ...]], ...]:
    """按 period 展开 quota bucket 及各 bucket 的合法日。

    - ``day``：每个合法日各一 bucket（key=ISO 日期）；
    - ``week``：每个与 schedule 相交的 ISO Monday week 各一 bucket（key=周一
      ISO 日期），只统计周内 of_week 命中的合法日，排除日不取消整周；
    - ``schedule``：整个 schedule 一次（key=``"schedule"``）。

    @param spec 冻结 schedule
    @param period ``day`` | ``week`` | ``schedule``
    @param of_week ISO weekday 集合；空元组按缺省周一至周日处理
    @return ``(bucket key, 合法日元组)`` 序列
    @raises ValueError period 未知时
    """
    if period not in VALID_PERIODS:
        raise ValueError(f"unknown quota period: {period!r}")
    if period == "schedule":
        return (("schedule", legal_dates(spec, of_week)),)
    legal = legal_dates(spec, of_week)
    if period == "day":
        return tuple((day.isoformat(), (day,)) for day in legal)
    first, last = local_date_span(spec)
    buckets = []
    cursor = week_monday(first)
    final_monday = week_monday(last)
    while cursor <= final_monday:
        week_days = tuple(day for day in legal if week_monday(day) == cursor)
        buckets.append((cursor.isoformat(), week_days))
        cursor += timedelta(days=7)
    return tuple(buckets)

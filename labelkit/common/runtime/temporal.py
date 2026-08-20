"""v1.16 时间量化、fixed-offset 日历窗口与重复平移纯函数。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import logging
from collections.abc import Mapping, Sequence
from typing import Any

MICROSECONDS_PER_SECOND = 1_000_000
MICROSECONDS_PER_DAY = 86_400 * MICROSECONDS_PER_SECOND
MICROSECONDS_PER_WEEK = 7 * MICROSECONDS_PER_DAY
WEEKDAYS = {name: index for index, name in enumerate(
    ("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}
_log = logging.getLogger("labelkit.sequence_rules.temporal")


def _intersection_left(left: "TimeInterval", right: "TimeInterval", value: int) -> bool:
    """计算交集左端是否闭合。"""
    if left.lo_us != value:
        return bool(right.left_closed)
    if right.lo_us != value:
        return bool(left.left_closed)
    return bool(left.left_closed and right.left_closed)


def _intersection_right(left: "TimeInterval", right: "TimeInterval", value: int) -> bool:
    """计算交集右端是否闭合。"""
    if left.hi_us != value:
        return bool(right.right_closed)
    if right.hi_us != value:
        return bool(left.right_closed)
    return bool(left.right_closed and right.right_closed)


@dataclass(frozen=True)
class TimeInterval:
    """整数微秒区间。``closed`` 表示两端都可取，否则右端半开。"""

    lo_us: int
    hi_us: int
    closed: bool = False
    left_closed: bool | None = None
    right_closed: bool | None = None

    def __post_init__(self) -> None:
        """把简写 closed 展开为两端包含性。"""
        if self.left_closed is None:
            object.__setattr__(self, "left_closed", True)
        if self.right_closed is None:
            object.__setattr__(self, "right_closed", self.closed)

    def contains(self, value_us: int) -> bool:
        """判断整数微秒是否落在区间。

        @param value_us 待判断的整数微秒值
        @return 值位于区间内时为 ``True``
        """
        left = value_us > self.lo_us or (value_us == self.lo_us and bool(self.left_closed))
        right = value_us < self.hi_us or (value_us == self.hi_us and bool(self.right_closed))
        return left and right

    def intersect(self, other: "TimeInterval") -> "TimeInterval | None":
        """求两个同语义区间的交集。

        @param other 参与求交的另一个整数微秒区间
        @return 非空交集区间；无交集时返回 ``None``
        """
        lo, hi = max(self.lo_us, other.lo_us), min(self.hi_us, other.hi_us)
        left_closed = _intersection_left(self, other, lo)
        right_closed = _intersection_right(self, other, hi)
        if lo > hi or (lo == hi and not (left_closed and right_closed)):
            return None
        return TimeInterval(lo, hi, left_closed=left_closed, right_closed=right_closed)


@dataclass(frozen=True)
class DayWindow:
    """本地自然日内半开窗口。"""

    start_us: int
    end_us: int

    def contains(self, local_us: int) -> bool:
        """判断本地日内微秒是否落窗。

        @param local_us 待判断的本地日内整数微秒值
        @return 值位于半开日内窗时为 ``True``
        """
        return self.start_us <= local_us < self.end_us


@dataclass(frozen=True)
class CalendarWindow:
    """帧类对应的多日内窗与星期集合。"""

    frame_class: str
    of_day: tuple[DayWindow, ...]
    of_week: frozenset[int] = frozenset(range(7))


def _decimal(value: Any) -> Decimal:
    """用字符串保持配置小数的精确语义。"""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _exact_us(value: Any, rounding: str) -> int:
    """把秒数转微秒并按指定方向取整。"""
    scaled = _decimal(value) * MICROSECONDS_PER_SECOND
    integral = scaled.to_integral_value(rounding=rounding)
    if scaled != integral:
        raise ValueError("time_s endpoint must be exactly representable in microseconds")
    return int(integral)


def quantize_time_s(bounds: Sequence[Any]) -> tuple[int, int]:
    """量化半开 ``time_s``，要求 1µs <= lo < hi。

    @param bounds 含两个秒数端点的序列
    @return 精确可表示的整数微秒半开端点 ``(lo_us, hi_us)``
    """
    if len(bounds) != 2:
        raise ValueError("time_s must contain exactly two endpoints")
    lo = _exact_us(bounds[0], ROUND_CEILING)
    hi = _exact_us(bounds[1], ROUND_FLOOR)
    if lo < 1 or lo >= hi:
        raise ValueError("time_s must satisfy 1us <= lo < hi")
    return lo, hi


def quantize_frame_gap(bounds: Sequence[Any]) -> TimeInterval:
    """量化 frame_gap_s 的实数闭区间，使用 ceil(lo)/floor(hi)。

    @param bounds 含两个秒数端点的序列
    @return 量化后的整数微秒闭区间
    """
    if len(bounds) != 2:
        raise ValueError("frame_gap_s must contain exactly two endpoints")
    lo_dec, hi_dec = _decimal(bounds[0]), _decimal(bounds[1])
    lo = int((lo_dec * MICROSECONDS_PER_SECOND).to_integral_value(rounding=ROUND_CEILING))
    hi = int((hi_dec * MICROSECONDS_PER_SECOND).to_integral_value(rounding=ROUND_FLOOR))
    if lo > hi:
        raise ValueError("frame_gap_s has no representable microsecond value")
    if lo < 0:
        raise ValueError("frame_gap_s must be non-negative")
    return TimeInterval(lo, hi, closed=True)


def timestamp_us(value: Any) -> int:
    """把 ISO timestamp、datetime 或数值秒转换为整数微秒。

    @param value ISO 时间文本、datetime 或以秒表示的数值
    @return 从 Unix epoch 起算的整数微秒
    """
    if isinstance(value, datetime):
        item = value
        if item.tzinfo is None:
            item = item.replace(tzinfo=timezone.utc)
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        delta = item.astimezone(timezone.utc) - epoch
        return ((delta.days * 86_400 + delta.seconds) * MICROSECONDS_PER_SECOND
                + delta.microseconds)
    if isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        return timestamp_us(datetime.fromisoformat(text))
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, (float, Decimal)):
        scaled = _decimal(value) * MICROSECONDS_PER_SECOND
        if scaled != scaled.to_integral_value():
            raise ValueError("timestamp must be representable in microseconds")
        return int(scaled)
    raise TypeError("unsupported timestamp value")


def timestamp_datetime(value: Any) -> datetime:
    """解析 timestamp 并保留固定 offset；naive 值按 UTC。

    @param value ISO 时间文本、datetime 或以秒表示的数值
    @return 带固定 offset 的 datetime
    """
    if isinstance(value, datetime):
        item = value
    elif isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        item = datetime.fromisoformat(text)
    else:
        total = timestamp_us(value)
        days, remainder = divmod(total, MICROSECONDS_PER_DAY)
        seconds, micros = divmod(remainder, MICROSECONDS_PER_SECOND)
        item = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
            days=days, seconds=seconds, microseconds=micros)
    return item if item.tzinfo is not None else item.replace(tzinfo=timezone.utc)


def fixed_offset(value: Any) -> timezone:
    """返回 timestamp 的固定 ISO offset。

    @param value 用于提取 offset 的 timestamp 值
    @return timestamp 携带的固定 timezone offset
    """
    item = timestamp_datetime(value)
    if not isinstance(item.tzinfo, timezone):
        _log.error("calendar timestamp must use a fixed UTC offset")
        raise ValueError("calendar timestamp must use a fixed UTC offset")
    return item.tzinfo


def parse_local_time(value: str) -> int:
    """解析 HH:MM、HH:MM:SS 或微秒精度日内时间。

    @param value 日内时间文本
    @return 自当日 00:00 起算的整数微秒
    """
    parts = value.split(":")
    if len(parts) not in {2, 3}:
        raise ValueError("calendar time must use HH:MM[:SS[.ffffff]]")
    hour, minute = int(parts[0]), int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("calendar time is out of range")
    seconds_text = parts[2] if len(parts) == 3 else "0"
    if seconds_text.startswith("-"):
        raise ValueError("calendar seconds are out of range")
    whole, dot, fraction = seconds_text.partition(".")
    if not whole.isdigit() or (dot and not fraction.isdigit()) or len(fraction) > 6:
        raise ValueError("calendar time must have at most microsecond precision")
    second_value = int(whole)
    if second_value >= 60:
        raise ValueError("calendar seconds are out of range")
    micros = int((fraction + "000000")[:6]) if fraction else 0
    return ((hour * 3600 + minute * 60 + second_value) * MICROSECONDS_PER_SECOND
            + micros)


def _normalize_day_windows(values: Sequence[Sequence[str]]) -> tuple[DayWindow, ...]:
    """规范化并检查同日窗口不重叠。"""
    result = tuple(pair if isinstance(pair, DayWindow) else
                   DayWindow(parse_local_time(pair[0]), parse_local_time(pair[1]))
                   for pair in values)
    if not result or any(item.start_us >= item.end_us for item in result):
        raise ValueError("of_day must contain non-empty same-day windows")
    ordered = sorted(result, key=lambda item: item.start_us)
    if any(left.end_us > right.start_us for left, right in zip(ordered, ordered[1:])):
        raise ValueError("of_day windows must not overlap")
    return tuple(ordered)


def _normalize_week(values: Sequence[str] | None) -> frozenset[int]:
    """规范化星期枚举并拒绝重复。"""
    if values is None:
        return frozenset(range(7))
    if all(isinstance(value, int) and not isinstance(value, bool) for value in values):
        indexes = tuple(int(value) for value in values)
        if not indexes or len(set(indexes)) != len(indexes) or any(index not in range(7) for index in indexes):
            raise ValueError("of_week must contain unique weekday indexes")
        return frozenset(indexes)
    names = tuple(str(value).lower() for value in values)
    if not names:
        raise ValueError("of_week must contain at least one weekday")
    if len(set(names)) != len(names) or any(name not in WEEKDAYS for name in names):
        raise ValueError("of_week must contain unique mon-sun values")
    return frozenset(WEEKDAYS[name] for name in names)


def normalize_calendar_window(value: CalendarWindow | Mapping[str, Any] | Any) -> CalendarWindow:
    """规范化一行窗口声明。

    @param value 日历窗口 dataclass、配置映射或等价属性对象
    @return 规范化的不可变日历窗口
    """
    if isinstance(value, CalendarWindow):
        return CalendarWindow(value.frame_class, _normalize_day_windows(value.of_day),
                              _normalize_week(value.of_week))
    if hasattr(value, "frame_class") and hasattr(value, "of_day"):
        return CalendarWindow(str(value.frame_class), _normalize_day_windows(value.of_day),
                              _normalize_week(getattr(value, "of_week", None)))
    if not isinstance(value, Mapping):
        raise TypeError("calendar window must be a mapping")
    frame_class = str(value.get("frame_class", ""))
    if not frame_class:
        raise ValueError("calendar window requires frame_class")
    return CalendarWindow(frame_class, _normalize_day_windows(value.get("of_day", ())),
                          _normalize_week(value.get("of_week")))


def normalize_calendar_windows(values: Sequence[CalendarWindow | Mapping[str, Any]],
                               ) -> tuple[CalendarWindow, ...]:
    """规范化窗口表并拒绝同一帧类重复声明。

    @param values 按配置声明顺序排列的窗口序列
    @return 规范化窗口元组
    """
    result = tuple(normalize_calendar_window(value) for value in values)
    names = [item.frame_class for item in result]
    if len(set(names)) != len(names):
        raise ValueError("each frame_class may have at most one calendar window")
    return result


def _local_parts(value: Any, offset: timezone | None = None) -> tuple[date, int, int]:
    """返回 fixed offset 下的日期、星期与日内微秒。"""
    item = timestamp_datetime(value)
    if offset is not None:
        item = item.astimezone(offset)
    day_start = item.replace(hour=0, minute=0, second=0, microsecond=0)
    delta = item - day_start
    local_us = ((delta.days * 86_400 + delta.seconds) * MICROSECONDS_PER_SECOND
                + delta.microseconds)
    return item.date(), item.weekday(), local_us


def in_calendar_window(value: Any, window: CalendarWindow | Mapping[str, Any],
                       offset: timezone | None = None) -> bool:
    """判断 timestamp 是否落入帧类的日历并集。

    @param value 待判断的 timestamp 值
    @param window 帧类日历窗口 dataclass 或配置映射
    @param offset 可选的固定 timezone offset；省略时使用 value 的 offset
    @return timestamp 落在允许星期及任一日内窗时为 ``True``
    """
    item = normalize_calendar_window(window)
    _, weekday, local_us = _local_parts(value, offset)
    return weekday in item.of_week and any(day.contains(local_us) for day in item.of_day)


def calendar_day_bounds(day: date, day_window: DayWindow, offset: timezone) -> tuple[int, int]:
    """把某一 fixed-offset 自然日窗口转换成绝对微秒半开区间。

    @param day 目标自然日
    @param day_window 目标日内半开窗口
    @param offset 用于解释自然日的固定 timezone offset
    @return 绝对整数微秒半开端点 ``(start_us, end_us)``
    """
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=offset)
    base = timestamp_us(midnight)
    return base + day_window.start_us, base + day_window.end_us


def window_day_options(ts_start: Any, window: CalendarWindow | Mapping[str, Any],
                       day_count: int = 8, offset: timezone | None = None) -> tuple[tuple[int, int], ...]:
    """列出从 ts_start 起固定天数内的合法日内窗。

    @param ts_start 搜索起点 timestamp
    @param window 帧类日历窗口 dataclass 或配置映射
    @param day_count 从起点起检查的自然日数量
    @param offset 可选的固定 timezone offset；省略时使用 ts_start 的 offset
    @return 按日期和日内窗口顺序排列的绝对微秒半开区间
    """
    item = normalize_calendar_window(window)
    start = timestamp_datetime(ts_start)
    offset = offset or fixed_offset(start)
    first = start.astimezone(offset).date()
    options: list[tuple[int, int]] = []
    for delta in range(max(0, day_count)):
        day = first + timedelta(days=delta)
        if day.weekday() not in item.of_week:
            continue
        options.extend(calendar_day_bounds(day, current, offset) for current in item.of_day)
    return tuple(options)


def minimal_duplicate_shift(source_timestamps: Sequence[Any], tail_end_us: int,
                            stream_gap_us: int,
                            windows: Mapping[str, CalendarWindow] | None = None,
                            ts_start: Any | None = None) -> int:
    """求 duplicate 源到流尾后的最小合法正平移。

    @param source_timestamps 源序列 timestamp 或 ``(timestamp, frame_class)`` 序列
    @param tail_end_us 当前流尾的绝对整数微秒
    @param stream_gap_us duplicate session 与流尾之间要求的 gap 微秒数
    @param windows 按帧类索引的有效日历窗口；无窗口时使用精确平移
    @param ts_start 用于 fixed offset 的起始 timestamp；省略时使用源首帧
    @return 满足 session gap 和窗口约束的最小正整数微秒平移
    """
    if not source_timestamps:
        raise ValueError("duplicate source must contain timestamps")
    decorated = tuple(_source_time_class(value) for value in source_timestamps)
    source = tuple(item[0] for item in decorated)
    start = source[0]
    lower = max(1, tail_end_us + stream_gap_us + 1 - start)
    if windows is None:
        return lower
    if not isinstance(windows, Mapping):
        raise ValueError("duplicate windows must map frame_class to CalendarWindow")
    if not windows:
        return lower
    original_offset_source = (source_timestamps[0][0]
                              if isinstance(source_timestamps[0], tuple) else source_timestamps[0])
    offset_source = ts_start if ts_start is not None else original_offset_source
    offset = fixed_offset(offset_source)
    weeks = (lower + MICROSECONDS_PER_WEEK - 1) // MICROSECONDS_PER_WEEK
    shift = max(1, weeks) * MICROSECONDS_PER_WEEK
    source_windows = tuple(windows.get(item[1]) for item in decorated)
    constrained = tuple((value, item) for value, item in zip(source, source_windows) if item is not None)
    if not constrained or all(in_calendar_window(value + shift, window, offset)
                              for value, window in constrained):
        return shift
    raise ValueError("duplicate weekly shift cannot preserve the declared calendar windows")


def _source_time_class(value: Any) -> tuple[int, str | None]:
    """读取普通 timestamp 或 ``(timestamp, frame_class)``。"""
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], str):
        return timestamp_us(value[0]), value[1]
    return timestamp_us(value), None


def replay_guard(frame_gap: TimeInterval, stream_gap_us: int) -> TimeInterval:
    """返回默认相邻 owner edge 与 stream gap 的闭区间交集。

    @param frame_gap 默认 frame gap 的整数微秒区间
    @param stream_gap_us stream gap 的整数微秒上界
    @return 与闭区间 ``[1, stream_gap_us]`` 的非空交集
    """
    guard = TimeInterval(1, stream_gap_us, closed=True)
    result = frame_gap.intersect(guard)
    if result is None:
        raise ValueError("frame_gap_s does not intersect the replay guard")
    return result

"""sequence timeline 与 interleaving 配置的解析和闭包校验。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

from labelkit.common.config._collect import _fmt
from labelkit.common.config.generation import (
    CounterfactualSetSpec,
    InterleavingPatternSpec,
    InterleavingSpec,
    SequenceGenerationConfig,
    SequencePattern,
    TimelineSpec,
    _ParseState,
    _check_keys,
    _error,
    _integer,
    _name,
    _seconds_us,
    _table,
)

_INT64_MAX = (1 << 63) - 1
_TIMELINE_KEYS = frozenset({
    "timestamp_start", "event_gap_s", "session_max_events", "session_max_span_s",
    "session_gap_s", "noise_events", "duplicate_sequences",
})
_DELETED_TIMELINE_KEYS = ("primary_sessions", "crossed_primary_sessions")


def _timestamp(state: _ParseState, value: object, location: str) -> tuple[int, int]:
    """解析带固定 offset 的 ISO-8601 时间。

    @param state 当前解析状态
    @param value 原始值
    @param location 键定位
    @return epoch 微秒与 offset 分钟
    """
    if not isinstance(value, str):
        _error(state, location, f"expected ISO-8601 string with offset, got {_fmt(value)}")
        return 0, 0
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _error(state, location, f"expected ISO-8601 string with offset, got {_fmt(value)}")
        return 0, 0
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None:
        _error(state, location, "timestamp must include a fixed UTC offset")
        return 0, 0
    return _epoch_microseconds(state, parsed, offset.total_seconds(), location)


def _epoch_microseconds(
    state: _ParseState,
    parsed: datetime,
    offset_seconds: float,
    location: str,
) -> tuple[int, int]:
    """把已解析时间转换为整数 epoch 微秒。

    @param state 当前解析状态
    @param parsed 带时区时间
    @param offset_seconds 固定 offset 秒数
    @param location 键定位
    @return epoch 微秒与 offset 分钟
    """
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        delta = parsed.astimezone(timezone.utc) - epoch
    except (OverflowError, ValueError):
        _error(state, location, "timestamp is outside the supported UTC range")
        return 0, 0
    micros = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    return micros, int(offset_seconds // 60)


def _pair_us(state: _ParseState, value: object, location: str) -> tuple[int, int]:
    """解析非负秒数闭区间。

    @param state 当前解析状态
    @param value 原始二元素数组
    @param location 键定位
    @return 整数微秒闭区间
    """
    if not isinstance(value, list) or len(value) != 2:
        _error(state, location, f"expected two-number array, got {_fmt(value)}")
        return 0, 0
    low = _seconds_us(state, value[0], f"{location}[1]", False)
    high = _seconds_us(state, value[1], f"{location}[2]", False)
    if low > high:
        _error(state, location, "range lower bound must be <= upper bound")
    return low, high


def _reject_deleted_timeline_keys(state: _ParseState, row: Mapping[str, object]) -> None:
    """对已删除 timeline 键给出定向配置错误。

    @param state 当前解析状态
    @param row timeline 原始表
    """
    for key in _DELETED_TIMELINE_KEYS:
        if key in row:
            _error(
                state,
                f"[generate.timeline].{key}",
                "generation_config_invalid: deleted timeline key",
            )


def parse_timeline(state: _ParseState) -> TimelineSpec:
    """解析不含用户 session 基数的 timeline 表。

    @param state 当前解析状态
    @return 冻结时间线载体
    """
    row = _table(state, state.generate, "timeline", "[generate.timeline]")
    _check_keys(
        state,
        row,
        _TIMELINE_KEYS | frozenset(_DELETED_TIMELINE_KEYS),
        "[generate.timeline]",
    )
    _reject_deleted_timeline_keys(state, row)
    timestamp, offset = _timestamp(
        state, row.get("timestamp_start"), "[generate.timeline].timestamp_start",
    )
    gap = _pair_us(state, row.get("event_gap_s"), "[generate.timeline].event_gap_s")
    max_events = _integer(
        state, row, "session_max_events", "[generate.timeline].session_max_events", 1,
    )
    max_span = _seconds_us(
        state, row.get("session_max_span_s"), "[generate.timeline].session_max_span_s", True,
    )
    session_gap = _seconds_us(
        state, row.get("session_gap_s"), "[generate.timeline].session_gap_s", True,
    )
    noise = _integer(state, row, "noise_events", "[generate.timeline].noise_events", 0)
    duplicate = _integer(
        state, row, "duplicate_sequences", "[generate.timeline].duplicate_sequences", 0,
    )
    return TimelineSpec(timestamp, offset, gap, max_events, max_span, session_gap, noise, duplicate)


def _int64(
    state: _ParseState,
    row: Mapping[str, object],
    key: str,
    location: str,
    minimum: int,
) -> int:
    """读取有下界的 TOML int64。

    @param state 当前解析状态
    @param row 来源表
    @param key 键名
    @param location 键定位
    @param minimum 闭区间下界
    @return 合法整数或下界
    """
    value = row.get(key)
    valid = isinstance(value, int) and not isinstance(value, bool)
    if not valid or not minimum <= value <= _INT64_MAX:
        _error(
            state,
            location,
            f"expected TOML int64 in {minimum}..{_INT64_MAX}, got {_fmt(value)}",
        )
        return minimum
    return value


def _parse_interleaving_pattern(
    state: _ParseState,
    raw_name: object,
    raw_row: object,
) -> InterleavingPatternSpec | None:
    """解析一个命名 interleaving pattern。

    @param state 当前解析状态
    @param raw_name TOML 子表名
    @param raw_row pattern 原始表
    @return 冻结 pattern；表类型非法时为 None
    """
    name = _name(state, raw_name, "[generate.interleaving.pattern].name")
    location = f"[generate.interleaving.pattern.{name or raw_name}]"
    if not isinstance(raw_row, Mapping):
        _error(state, location, f"expected table, got {_fmt(raw_row)}")
        return None
    _check_keys(
        state,
        raw_row,
        frozenset({"trigger_candidate_set", "partner_candidate_set", "trigger_weight"}),
        location,
    )
    trigger = _name(
        state, raw_row.get("trigger_candidate_set"), f"{location}.trigger_candidate_set",
    )
    partner = _name(
        state, raw_row.get("partner_candidate_set"), f"{location}.partner_candidate_set",
    )
    weight = _int64(state, raw_row, "trigger_weight", f"{location}.trigger_weight", 1)
    return InterleavingPatternSpec(name, trigger, partner, weight)


def parse_interleaving(state: _ParseState) -> InterleavingSpec | None:
    """解析独立 interleaving 章节及声明序 pattern。

    @param state 当前解析状态
    @return 冻结交织配置；章节缺失时为 None
    """
    if "interleaving" not in state.generate:
        return None
    row = _table(state, state.generate, "interleaving", "[generate.interleaving]")
    _check_keys(
        state, row, frozenset({"no_interleaving_weight", "pattern"}),
        "[generate.interleaving]",
    )
    weight = _int64(
        state,
        row,
        "no_interleaving_weight",
        "[generate.interleaving].no_interleaving_weight",
        0,
    )
    pattern_rows = _table(state, row, "pattern", "[generate.interleaving.pattern]")
    patterns = tuple(
        parsed
        for name, pattern_row in pattern_rows.items()
        if (parsed := _parse_interleaving_pattern(state, name, pattern_row)) is not None
    )
    if not patterns:
        _error(state, "[generate.interleaving.pattern]", "at least one named pattern is required")
    return InterleavingSpec(weight, patterns)


def _candidate_labels(config: SequenceGenerationConfig) -> tuple[str, ...]:
    """按 set 首次声明序返回有效候选集标签。

    @param config 完整 sequence 配置
    @return 去重后的候选集标签
    """
    labels: list[str] = []
    for group in config.counterfactual_sets:
        label = group.interleaving_candidate_set
        if label and label not in labels:
            labels.append(label)
    return tuple(labels)


def _check_candidate_variants(state: _ParseState, config: SequenceGenerationConfig) -> None:
    """要求每个候选声明恰有一个 positive variant。

    @param state 当前解析状态
    @param config 完整 sequence 配置
    """
    for group in config.counterfactual_sets:
        if group.interleaving_candidate_set is None:
            continue
        positive = sum(item.kind == "positive" for item in group.variants)
        if positive != 1:
            _error(
                state,
                f"[[generate.counterfactual_sets]].{group.name}.interleaving_candidate_set",
                "interleaving candidate set requires exactly one positive variant",
            )


def _check_interleaving_presence(state: _ParseState, config: SequenceGenerationConfig) -> None:
    """校验章节、候选标签和 sequence mode 的开关闭包。

    @param state 当前解析状态
    @param config 完整 sequence 配置
    """
    has_candidates = any(
        item.interleaving_candidate_set is not None for item in config.counterfactual_sets
    )
    has_section = config.interleaving is not None
    if has_candidates and not has_section:
        _error(
            state,
            "[generate.interleaving]",
            "required when interleaving_candidate_set is declared",
        )
    if has_section and not has_candidates:
        _error(
            state,
            "[[generate.counterfactual_sets]].interleaving_candidate_set",
            "required when generate.interleaving is declared",
        )
    if config.mode == "instruction_only" and (has_candidates or has_section):
        _error(state, "[generate.interleaving]", "forbidden in instruction_only mode")


def _check_pattern_references(state: _ParseState, config: SequenceGenerationConfig) -> None:
    """校验 pattern 引用、角色分离和候选全集引用。

    @param state 当前解析状态
    @param config 完整 sequence 配置
    """
    spec = config.interleaving
    if spec is None:
        return
    labels = _candidate_labels(config)
    trigger_labels = {item.trigger_candidate_set for item in spec.patterns}
    partner_labels = {item.partner_candidate_set for item in spec.patterns}
    for pattern in spec.patterns:
        _check_pattern_reference(state, pattern, frozenset(labels))
    for label in labels:
        if label not in trigger_labels and label not in partner_labels:
            _error(
                state,
                f"[[generate.counterfactual_sets]].interleaving_candidate_set.{label}",
                "declared candidate set is not referenced by an interleaving pattern",
            )
    conflicts = sorted(trigger_labels & partner_labels)
    if conflicts:
        _error(
            state,
            "[generate.interleaving]",
            f"candidate sets cannot serve both trigger and partner roles: {conflicts!r}",
        )


def _check_pattern_reference(
    state: _ParseState,
    pattern: InterleavingPatternSpec,
    labels: frozenset[str],
) -> None:
    """校验一个 pattern 的两端候选引用。

    @param state 当前解析状态
    @param pattern 当前交织 pattern
    @param labels 已声明候选集标签
    """
    location = f"[generate.interleaving.pattern.{pattern.name}]"
    if pattern.trigger_candidate_set == pattern.partner_candidate_set:
        _error(state, location, "trigger_candidate_set must differ from partner_candidate_set")
    for field, label in (
        ("trigger_candidate_set", pattern.trigger_candidate_set),
        ("partner_candidate_set", pattern.partner_candidate_set),
    ):
        if label not in labels:
            _error(state, f"{location}.{field}", "unknown or empty interleaving candidate set")


def _check_weight_totals(state: _ParseState, config: SequenceGenerationConfig) -> None:
    """校验每种 trigger opportunity 的累计权重不越界。

    @param state 当前解析状态
    @param config 完整 sequence 配置
    """
    spec = config.interleaving
    if spec is None:
        return
    triggers: list[str] = []
    for pattern in spec.patterns:
        if pattern.trigger_candidate_set not in triggers:
            triggers.append(pattern.trigger_candidate_set)
    for trigger in triggers:
        total = spec.no_interleaving_weight + sum(
            pattern.trigger_weight
            for pattern in spec.patterns
            if pattern.trigger_candidate_set == trigger
        )
        if total > _INT64_MAX:
            _error(
                state,
                "[generate.interleaving]",
                f"opportunity weight total exceeds TOML int64 for trigger {trigger!r}",
            )


def _declared_counts(
    patterns: tuple[SequencePattern, ...],
    sets: tuple[CounterfactualSetSpec, ...],
    duplicates: int,
) -> tuple[int, int, int]:
    """计算 declared primary sequence/event 与 replay event 数。

    @param patterns pattern 表
    @param sets counterfactual set 表
    @param duplicates replay sequence 数
    @return primary sequence、primary event 与 replay event 数
    """
    pattern_map = {item.name: item for item in patterns}
    sequences = 0
    events = 0
    positive_lengths: list[int] = []
    for group in sets:
        pattern = pattern_map.get(group.pattern)
        if pattern is None:
            continue
        sequences += group.count * len(group.variants)
        for variant in group.variants:
            length = len(pattern.roles) - (1 if variant.kind == "missing" else 0)
            events += group.count * length
            if variant.kind == "positive":
                positive_lengths.extend([length] * group.count)
    return sequences, events, sum(positive_lengths[:duplicates])


def _sequence_counts(
    state: _ParseState,
    config: SequenceGenerationConfig,
) -> tuple[int, int, int]:
    """按 mode 计算可见序列、事件与 replay event 数。

    @param state 当前解析状态
    @param config 完整 sequence 配置
    @return 可见序列、事件与 replay event 数
    """
    if config.mode == "declared":
        counts = _declared_counts(
            config.patterns,
            config.counterfactual_sets,
            config.timeline.duplicate_sequences,
        )
        positive = sum(
            group.count
            for group in config.counterfactual_sets
            if any(item.kind == "positive" for item in group.variants)
        )
        if config.timeline.duplicate_sequences > positive:
            _error(
                state,
                "[generate.timeline].duplicate_sequences",
                "not enough positive primary sources for replay",
            )
        return counts
    if config.timeline.duplicate_sequences != 0:
        _error(
            state,
            "[generate.timeline].duplicate_sequences",
            "instruction_only requires zero duplicate sequences",
        )
    sequences = sum(item.count for item in config.instruction_only)
    events = sum(item.count * item.len_range[0] for item in config.instruction_only)
    return sequences, events, 0


def _check_noise_counts(state: _ParseState, config: SequenceGenerationConfig) -> None:
    """校验 noise 表与 timeline 精确数量相互闭合。

    @param state 当前解析状态
    @param config 完整 sequence 配置
    """
    timeline = config.timeline
    if (timeline.noise_events > 0) != (config.noise is not None):
        _error(state, "[generate.noise]", "noise table must be present iff noise_events > 0")
    if config.noise is not None and len(config.noise.topics) != timeline.noise_events:
        _error(
            state,
            "[generate.noise].topics",
            "topic count must equal generate.timeline.noise_events",
        )


def _check_derived_limits(
    state: _ParseState,
    config: SequenceGenerationConfig,
    sequences: int,
    events: int,
    replay: int,
) -> None:
    """校验派生 stream row 与 record unit 上限。

    @param state 当前解析状态
    @param config 完整 sequence 配置
    @param sequences 可见 primary sequence 数
    @param events primary event 数
    @param replay replay event 数
    """
    stream_rows = events + config.timeline.noise_events + replay
    record_units = sequences + stream_rows
    if not 1 <= stream_rows <= config.limits.stream_rows:
        _error(state, "[generate]", "derived stream_rows must be in 1..500000")
    if not 1 <= record_units <= config.limits.record_units:
        _error(state, "[generate]", "derived record_units must be in 1..500000")


def validate_sequence_layout(state: _ParseState, config: SequenceGenerationConfig) -> None:
    """校验 interleaving 闭包及 timeline 派生数量。

    @param state 当前解析状态
    @param config 完整 sequence 配置
    """
    _check_interleaving_presence(state, config)
    _check_candidate_variants(state, config)
    _check_pattern_references(state, config)
    _check_weight_totals(state, config)
    sequences, events, replay = _sequence_counts(state, config)
    _check_noise_counts(state, config)
    _check_derived_limits(state, config, sequences, events, replay)

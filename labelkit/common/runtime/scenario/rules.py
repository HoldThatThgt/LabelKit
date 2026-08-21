"""v1.17 场景规则纯语义：FrameRuleSpec 15+1 模板与 SequenceRuleSpec 四模板求值器。

FrameRuleSpec 前 15 个模板沿用 v1.16 DECLARE 有限迹语义（时间改为整数微秒半开
区间，correlation 去掉 operator 只余双字段）；新增 ``contains`` 严格 interval
包含（SPEC-SP §4.5）。SequenceRuleSpec 按 period bucket 独立执行 precedence/
response/succession/not_co_existence（SPEC-SP §4.6）。本模块只做纯求值与名称域
判定，不做 IO、不建 CP 模型；供 M1 few-shot 干跑、planner 校验与单元测试共用。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from labelkit.common.runtime.scenario.calendar import (
    VALID_PERIODS,
    local_date,
    week_monday,
)
from labelkit.common.runtime.scenario.model import (
    CorrelationSpec,
    FrameRuleSpec,
    ScheduleSpec,
    SequenceRuleSpec,
)

_log = logging.getLogger("labelkit.scenario.rules")

#: frame rule 的 15 个 v1.16 模板 + 新增 contains。
FRAME_TEMPLATES = frozenset({
    "existence", "absence", "exactly", "init", "end",
    "responded_existence", "co_existence", "response", "precedence",
    "succession", "alternate_response", "chain_response",
    "chain_precedence", "not_co_existence", "not_succession", "contains",
})
SEQUENCE_TEMPLATES = frozenset({"precedence", "response", "succession",
                                "not_co_existence"})
_UNARY = frozenset({"existence", "absence", "exactly", "init", "end"})
_COUNTED = frozenset({"existence", "absence", "exactly"})
_DIRECTIONAL = frozenset({
    "response", "precedence", "succession", "alternate_response",
    "chain_response", "chain_precedence", "not_succession",
})
_ABSOLUTE = frozenset({"responded_existence", "co_existence", "not_co_existence"})
_NAME_RE = re.compile(r"[a-z0-9_]+\Z")


@dataclass(frozen=True)
class EvalFrame:
    """frame rule 求值输入的单帧 occurrence。

    ``end_us`` 为 ``None`` 表示 point frame（end == start）；``payload`` 是
    correlation 检查用的 JSON-compatible 顶层对象，可缺省。
    """

    frame_class: str
    start_us: int
    end_us: int | None = None
    payload: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class EvalOccurrence:
    """sequence rule 求值输入的单条 sequence occurrence。

    ``end_us`` 为 ``None`` 表示点状 sequence（end == start）；bucket 归属按
    ``start_us`` 的本地自然日判定（SPEC-SP §4.6）。
    """

    sequence_class: str
    start_us: int
    end_us: int | None = None


@dataclass(frozen=True)
class RuleVerdict:
    """单条规则的纯求值结果。"""

    rule_name: str
    valid: bool
    failure_index: int | None = None
    reason: str | None = None


# --------------------------------------------------------------- 名称域 ----

def is_valid_name(name: str) -> bool:
    """判断名称是否匹配 ``[a-z0-9_]+`` 全串。

    @param name 待判定的自然名称
    @return 全串命中 ``[a-z0-9_]+`` 时为 ``True``
    """
    return bool(_NAME_RE.match(name))


def name_domain_violations(quotas: Sequence[Any], frame_rules: Sequence[Any],
                           frame_windows: Sequence[Any],
                           sequence_rules: Sequence[Any]) -> tuple[str, ...]:
    """quota/frame rule/frame window/sequence rule 的 name 全局唯一域校验。

    @param quotas 带 ``name`` 的 quota 对象序列
    @param frame_rules 带 ``name`` 的 frame rule 对象序列
    @param frame_windows 带 ``name`` 的 frame window 对象序列
    @param sequence_rules 带 ``name`` 的 sequence rule 对象序列
    @return 排序后的违规描述元组；干净时为空元组
    """
    groups = (("quota", quotas), ("frame_rule", frame_rules),
              ("frame_window", frame_windows), ("sequence_rule", sequence_rules))
    violations: list[str] = []
    seen: dict[str, str] = {}
    for kind, items in groups:
        for item in items:
            name = item.name
            if not is_valid_name(name):
                violations.append(f"invalid {kind} name: {name!r}")
            elif name in seen:
                violations.append(f"duplicate name in shared name domain: "
                                  f"{name!r} ({seen[name]} vs {kind})")
            else:
                seen[name] = kind
    return tuple(sorted(violations))


# ------------------------------------------------- JSON 类型敏感相等 ----

def _json_type(value: Any) -> str:
    """返回 JSON 运行时类型，避免 bool 与 number 相等。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def canonical_equal(left: Any, right: Any) -> bool:
    """按 JSON 类型后 canonical bytes 比较两个 payload 值。

    @param left 第一个 JSON-compatible 值
    @param right 第二个 JSON-compatible 值
    @return 两值类型敏感且 canonical bytes 相等时为 ``True``
    """
    if _json_type(left) != _json_type(right):
        return False
    try:
        dumps = json.dumps(left, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
        return dumps == json.dumps(right, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        _log.error("canonical equality failed: %s", type(exc).__name__)
        return False


def _field(payload: Any, name: str) -> Any:
    """从结构化 payload 读取一个顶层字段。"""
    return payload.get(name) if isinstance(payload, Mapping) else None


def _end_us(start_us: int, end_us: int | None) -> int:
    """point occurrence 的 end 折叠为 start。"""
    return start_us if end_us is None else end_us


# ------------------------------------------------- frame rule 形状校验 ----

def _require_half_open_us(label: str, window: tuple[int, int]) -> None:
    """校验半开整数微秒区间 ``0 <= lo < hi``。

    @param label 报错用字段名
    @param window 二元区间
    @raises ValueError 元素非整数或区间非法时
    """
    if len(window) != 2 or any(not isinstance(value, int) or isinstance(value, bool)
                               for value in window):
        raise ValueError(f"{label} must contain two integers")
    if not 0 <= window[0] < window[1]:
        raise ValueError(f"{label} must satisfy 0 <= lo < hi")


def _validate_unary(rule: FrameRuleSpec) -> None:
    """一元模板参数矩阵校验。"""
    if not rule.frame_class:
        raise ValueError(f"{rule.template} requires frame_class")
    if any(value is not None for value in (rule.source, rule.target,
                                           rule.correlation, rule.time_us)):
        raise ValueError(f"{rule.template} does not accept binary modifiers")
    if rule.template in _COUNTED:
        if (not isinstance(rule.count, int) or isinstance(rule.count, bool)
                or rule.count <= 0):
            raise ValueError(f"{rule.template}.count must be a positive integer")
    elif rule.count is not None:
        raise ValueError(f"{rule.template} does not accept count")


def _validate_binary(rule: FrameRuleSpec) -> None:
    """二值模板（含 contains）参数矩阵校验。"""
    if rule.count is not None or rule.frame_class is not None:
        raise ValueError(f"{rule.template} requires source and target only")
    if not rule.source or not rule.target:
        raise ValueError(f"{rule.template} requires source and target")
    if rule.source == rule.target:
        raise ValueError("frame rule source and target must differ")
    if rule.correlation is not None and (not rule.correlation.source_field
                                         or not rule.correlation.target_field):
        raise ValueError("correlation must name two fields")
    if rule.time_us is None:
        return
    if rule.template == "contains":
        raise ValueError("contains does not accept time_us")
    _require_half_open_us("time_us", rule.time_us)


def validate_frame_rule(rule: FrameRuleSpec) -> FrameRuleSpec:
    """校验 frame rule 参数矩阵并原样返回。

    @param rule 待校验的 frame rule
    @return 通过校验的同一对象
    @raises ValueError 名称、模板或参数矩阵非法时
    """
    if not is_valid_name(rule.name):
        raise ValueError(f"invalid frame rule name: {rule.name!r}")
    if rule.template not in FRAME_TEMPLATES:
        raise ValueError(f"unknown frame rule template: {rule.template}")
    if rule.template in _UNARY:
        _validate_unary(rule)
    else:
        _validate_binary(rule)
    return rule


def validate_sequence_rule(rule: SequenceRuleSpec) -> SequenceRuleSpec:
    """校验 sequence rule 参数矩阵并原样返回。

    @param rule 待校验的 sequence rule
    @return 通过校验的同一对象
    @raises ValueError 名称、模板、方向或区间非法时
    """
    if not is_valid_name(rule.name):
        raise ValueError(f"invalid sequence rule name: {rule.name!r}")
    if rule.template not in SEQUENCE_TEMPLATES:
        raise ValueError(f"unknown sequence rule template: {rule.template}")
    if not rule.source or not rule.target:
        raise ValueError(f"{rule.template} requires source and target")
    if rule.source == rule.target:
        raise ValueError("sequence rule source and target must differ")
    if rule.period not in VALID_PERIODS:
        raise ValueError(f"unknown sequence rule period: {rule.period!r}")
    if rule.gap_us is not None:
        if rule.template == "not_co_existence":
            raise ValueError("not_co_existence does not accept gap_us")
        _require_half_open_us("gap_us", rule.gap_us)
    return rule


# ------------------------------------------------- frame rule 纯求值 ----

def _activation_positions(rule: FrameRuleSpec, word: Sequence[str]) -> tuple[int, ...]:
    """返回一条二值规则的标准 activation 位置（v1.16 语义）。"""
    if rule.template in {"precedence", "chain_precedence"}:
        return tuple(i for i, value in enumerate(word) if value == rule.target)
    if rule.template in {"succession", "co_existence"}:
        sources = tuple(i for i, value in enumerate(word) if value == rule.source)
        return sources + tuple(i for i, value in enumerate(word)
                               if value == rule.target)
    return tuple(i for i, value in enumerate(word) if value == rule.source)


def _next_occurrence(word: Sequence[str], label: str, position: int) -> int | None:
    """查找 position 之后的下一 occurrence。"""
    return next((i for i in range(position + 1, len(word)) if word[i] == label), None)


def _pair_for(rule: FrameRuleSpec, word: Sequence[str],
              activation: int) -> tuple[tuple[int, int, int], ...]:
    """为一个 activation 枚举结构候选对 ``(activation, source, target)``。"""
    template, source, target = rule.template, rule.source, rule.target
    if template == "co_existence" and word[activation] == target:
        return tuple((activation, index, activation)
                     for index, value in enumerate(word)
                     if index != activation and value == source)
    if template in {"precedence", "chain_precedence"}:
        indexes = [i for i in range(activation) if word[i] == source]
        if template == "chain_precedence":
            indexes = [i for i in indexes if i == activation - 1]
        return tuple((activation, i, activation) for i in indexes)
    if template == "succession" and word[activation] == target:
        return tuple((activation, i, activation) for i in range(activation)
                     if word[i] == source)
    start = activation + 1 if template in _DIRECTIONAL else 0
    indexes = [i for i in range(start, len(word))
               if i != activation and word[i] == target]
    if template == "chain_response":
        indexes = [i for i in indexes if i == activation + 1]
    elif template == "alternate_response":
        boundary = _next_occurrence(word, source or "", activation)
        indexes = [i for i in indexes if boundary is None or i < boundary]
    return tuple((activation, activation, i) for i in indexes)


def _negative_pairs(rule: FrameRuleSpec,
                    word: Sequence[str]) -> tuple[tuple[int, int, int], ...]:
    """负规则的全部结构 occurrence 对。"""
    if rule.template == "not_co_existence":
        return tuple((i, i, j) for i, value in enumerate(word)
                     if value == rule.source
                     for j, other in enumerate(word)
                     if j != i and other == rule.target)
    return tuple((i, i, j) for i, value in enumerate(word)
                 if value == rule.source
                 for j, other in enumerate(word)
                 if j > i and other == rule.target)


def _time_pair_ok(rule: FrameRuleSpec, pair: tuple[int, int, int],
                  frames: Sequence[EvalFrame]) -> bool:
    """判定 occurrence pair 的 start 差是否落入半开 time_us。"""
    if rule.time_us is None:
        return True
    delta = frames[pair[2]].start_us - frames[pair[1]].start_us
    if rule.template in _ABSOLUTE:
        delta = abs(delta)
    lo, hi = rule.time_us
    return lo <= delta < hi


def _correlation_pair_ok(rule: FrameRuleSpec, source: EvalFrame,
                         target: EvalFrame) -> bool:
    """判定 pair 的类型敏感字段 equality。"""
    corr: CorrelationSpec | None = rule.correlation
    if corr is None:
        return True
    return canonical_equal(_field(source.payload, corr.source_field),
                           _field(target.payload, corr.target_field))


def _pair_matched(rule: FrameRuleSpec, pair: tuple[int, int, int],
                  frames: Sequence[EvalFrame]) -> bool:
    """结构候选对同时通过 correlation 与时间窗。"""
    return (_correlation_pair_ok(rule, frames[pair[1]], frames[pair[2]])
            and _time_pair_ok(rule, pair, frames))


def _unary_verdict(rule: FrameRuleSpec, word: Sequence[str]) -> RuleVerdict:
    """求值一元模板。"""
    count = sum(value == rule.frame_class for value in word)
    if rule.template == "existence":
        valid = count >= int(rule.count or 0)
    elif rule.template == "absence":
        valid = count < int(rule.count or 0)
    elif rule.template == "exactly":
        valid = count == int(rule.count or 0)
    else:
        valid = bool(word) and (word[0] if rule.template == "init"
                                else word[-1]) == rule.frame_class
    return RuleVerdict(rule.name, valid, None, None if valid else "control_flow")


def _contains_verdict(rule: FrameRuleSpec, frames: Sequence[EvalFrame]) -> RuleVerdict:
    """求值严格 interval 包含：相等边界不通过，point target end==start。"""
    for index, frame in enumerate(frames):
        if frame.frame_class != rule.target:
            continue
        target_end = _end_us(frame.start_us, frame.end_us)
        contained = any(source.frame_class == rule.source
                        and source.start_us < frame.start_us
                        and target_end < _end_us(source.start_us, source.end_us)
                        and _correlation_pair_ok(rule, source, frame)
                        for source in frames)
        if not contained:
            return RuleVerdict(rule.name, False, index, "missing_containment")
    return RuleVerdict(rule.name, True)


def _negative_verdict(rule: FrameRuleSpec, frames: Sequence[EvalFrame],
                      word: Sequence[str]) -> RuleVerdict:
    """负规则：任一实际 occurrence 对命中即失败。"""
    for pair in _negative_pairs(rule, word):
        if _pair_matched(rule, pair, frames):
            return RuleVerdict(rule.name, False, pair[1], "negative_match")
    return RuleVerdict(rule.name, True)


def _positive_verdict(rule: FrameRuleSpec, frames: Sequence[EvalFrame],
                      word: Sequence[str]) -> RuleVerdict:
    """正规则：每个 activation 至少一个匹配对，无 activation 即 vacuity。"""
    for activation in _activation_positions(rule, word):
        local = _pair_for(rule, word, activation)
        if not any(_pair_matched(rule, pair, frames) for pair in local):
            return RuleVerdict(rule.name, False, activation, "missing_target")
    return RuleVerdict(rule.name, True)


def evaluate_frame_rule(rule: FrameRuleSpec,
                        frames: Sequence[EvalFrame]) -> RuleVerdict:
    """按有限迹语义纯求值一条 frame rule（先做形状校验）。

    @param rule 待求值的 frame rule
    @param frames 与该 sequence 对齐的帧 occurrence 序列
    @return 冻结 ``RuleVerdict``
    @raises ValueError 规则参数矩阵非法时
    """
    item = validate_frame_rule(rule)
    word = tuple(frame.frame_class for frame in frames)
    if item.template in _UNARY:
        return _unary_verdict(item, word)
    if item.template == "contains":
        return _contains_verdict(item, frames)
    if item.template in {"not_co_existence", "not_succession"}:
        return _negative_verdict(item, frames, word)
    return _positive_verdict(item, frames, word)


# ---------------------------------------------- sequence rule 纯求值 ----

def _occurrence_bucket_key(period: str, occurrence: EvalOccurrence,
                           schedule: ScheduleSpec) -> str:
    """sequence occurrence 按 period 归属的 bucket key。"""
    day = local_date(occurrence.start_us, schedule.utc_offset_minutes)
    if period == "day":
        return day.isoformat()
    if period == "week":
        return week_monday(day).isoformat()
    return "schedule"


def _witness_gap_ok(rule: SequenceRuleSpec, earlier: EvalOccurrence,
                    later: EvalOccurrence) -> bool:
    """判定 witness 对：earlier.end 严格早于 later.start 且 gap 落入半开区间。"""
    earlier_end = _end_us(earlier.start_us, earlier.end_us)
    if earlier_end >= later.start_us:
        return False
    if rule.gap_us is None:
        return True
    lo, hi = rule.gap_us
    return lo <= later.start_us - earlier_end < hi


def _obligation_verdict(rule: SequenceRuleSpec, occurrences: Sequence[EvalOccurrence],
                        keys: Sequence[str], buckets: Mapping[str, tuple[int, ...]],
                        obligated_class: str) -> RuleVerdict:
    """precedence/response 共用：义务方向由 obligated_class 决定。"""
    witness_class = rule.target if obligated_class == rule.source else rule.source
    reason = ("missing_target_witness" if obligated_class == rule.source
              else "missing_source_witness")
    for index, occurrence in enumerate(occurrences):
        if occurrence.sequence_class != obligated_class:
            continue
        found = False
        for j in buckets[keys[index]]:
            other = occurrences[j]
            if other.sequence_class != witness_class:
                continue
            earlier, later = ((other, occurrence)
                              if obligated_class == rule.target
                              else (occurrence, other))
            if _witness_gap_ok(rule, earlier, later):
                found = True
                break
        if not found:
            return RuleVerdict(rule.name, False, index, reason)
    return RuleVerdict(rule.name, True)


def _not_co_existence_verdict(rule: SequenceRuleSpec,
                              occurrences: Sequence[EvalOccurrence],
                              buckets: Mapping[str, tuple[int, ...]]) -> RuleVerdict:
    """同一 bucket 不得同时出现 source 与 target。"""
    for indexes in buckets.values():
        classes = {occurrences[i].sequence_class for i in indexes}
        if rule.source in classes and rule.target in classes:
            first = min(i for i in indexes if occurrences[i].sequence_class
                        in (rule.source, rule.target))
            return RuleVerdict(rule.name, False, first, "co_existence")
    return RuleVerdict(rule.name, True)


def evaluate_sequence_rule(rule: SequenceRuleSpec,
                           occurrences: Sequence[EvalOccurrence],
                           schedule: ScheduleSpec) -> RuleVerdict:
    """按 period bucket 独立纯求值一条 sequence rule（先做形状校验）。

    @param rule 待求值的 sequence rule
    @param occurrences sequence occurrence 序列（bucket 按 start 本地日归属）
    @param schedule 提供 fixed offset 的冻结 schedule
    @return 冻结 ``RuleVerdict``
    @raises ValueError 规则参数矩阵非法时
    """
    item = validate_sequence_rule(rule)
    keys = tuple(_occurrence_bucket_key(item.period, occurrence, schedule)
                 for occurrence in occurrences)
    buckets: dict[str, tuple[int, ...]] = {}
    for index, key in enumerate(keys):
        buckets.setdefault(key, ())
        buckets[key] = buckets[key] + (index,)
    if item.template == "not_co_existence":
        return _not_co_existence_verdict(item, occurrences, buckets)
    if item.template in {"precedence", "succession"}:
        verdict = _obligation_verdict(item, occurrences, keys, buckets, item.target)
        if not verdict.valid:
            return verdict
    if item.template in {"response", "succession"}:
        return _obligation_verdict(item, occurrences, keys, buckets, item.source)
    return RuleVerdict(item.name, True)

"""v1.16 DECLARE 规则与冻结 skeleton 的运行期判定。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

_log = logging.getLogger("labelkit.sequence_rules")

TEMPLATES = frozenset({
    "existence", "absence", "exactly", "init", "end",
    "responded_existence", "co_existence", "response", "precedence",
    "succession", "alternate_response", "chain_response",
    "chain_precedence", "not_co_existence", "not_succession",
})
_UNARY = frozenset({"existence", "absence", "exactly", "init", "end"})
_BINARY = TEMPLATES - _UNARY
_COUNTED = frozenset({"existence", "absence", "exactly"})
_DIRECTIONAL = frozenset({
    "response", "precedence", "succession", "alternate_response",
    "chain_response", "chain_precedence", "not_succession",
})
_ABSOLUTE = frozenset({"responded_existence", "co_existence", "not_co_existence"})
_POSITIVE = frozenset(TEMPLATES - {"absence", "not_co_existence", "not_succession"})


@dataclass(frozen=True)
class CorrelationSpec:
    """类型敏感的 payload equality 声明。"""

    operator: str = "equal"
    source_field: str = ""
    target_field: str = ""


@dataclass(frozen=True)
class RuleSpec:
    """一条已规范化的序列规则。"""

    template: str
    frame_class: str | None = None
    source: str | None = None
    target: str | None = None
    count: int | None = None
    time_s: tuple[Decimal, Decimal] | None = None
    correlation: CorrelationSpec | None = None


@dataclass(frozen=True)
class CandidatePair:
    """一个标准 occurrence 候选对，位置均为序列内零基索引。"""

    activation: int
    source: int
    target: int


@dataclass(frozen=True)
class RuleFailure:
    """一条规则的首个失败义务。"""

    activation: int | None
    reason: str
    candidates: tuple[CandidatePair, ...]


@dataclass(frozen=True)
class RuleEvaluation:
    """直接 evaluator 的单规则结果。"""

    rule: RuleSpec
    valid: bool
    activations: tuple[int, ...]
    candidates: tuple[CandidatePair, ...]
    matches: tuple[CandidatePair, ...]
    failure: RuleFailure | None = None


@dataclass(frozen=True)
class PayloadEvaluation:
    """冻结 skeleton 上的 C0/Ce/Ct 逐规则判定结果。"""

    valid: bool
    evaluations: tuple[RuleEvaluation, ...]
    validator_scrapped: int = 0
    correlation_scrapped: int = 0
    temporal_scrapped: int = 0
    sequence_validator_scrapped: int = 0
    failure_rule: RuleSpec | None = None
    failure_reason: str | None = None


@dataclass(frozen=True)
class _PayloadFailureSpec:
    """作废归因的冻结输入。"""

    previous: tuple[RuleEvaluation, ...]
    rule: RuleSpec
    word: Sequence[str]
    reason: str
    correlation: int
    temporal: int
    failure: RuleFailure | None = None


def render_constraint_text(rules: Sequence[Any], windows: Sequence[Any]) -> str:
    """把生效规则与窗口渲染为内容模型可读的稳定约束文本。

    @param rules 生效的 DECLARE 规则序列
    @param windows 生效的日历窗口序列
    @return 稳定约束文本；无规则和窗口时返回 ``"none"``
    """
    lines: list[str] = []
    for rule in rules:
        fields = [f"template={rule.template}"]
        fields.extend(f"{name}={value}" for name, value in (
            ("frame_class", rule.frame_class), ("source", rule.source),
            ("target", rule.target), ("count", rule.count),
        ) if value is not None)
        if rule.time_s is not None:
            fields.append(f"time_s=[{rule.time_s[0]}, {rule.time_s[1]}) 秒")
        line = "规则：" + "；".join(fields)
        if rule.correlation is not None:
            corr = rule.correlation
            if rule.template in {"not_co_existence", "not_succession"}:
                line += (f"；correlation={corr.operator}，禁止 source.{corr.source_field} 与 "
                         f"target.{corr.target_field} 的 JSON 类型及值相同；若该 occurrence "
                         "对同时满足其他结构与时间条件，则该对违规")
            else:
                line += (f"；correlation={corr.operator}，source.{corr.source_field} 与 "
                         f"target.{corr.target_field} 的 JSON 类型及值必须相同")
        lines.append(line)
    for window in windows:
        lines.append(f"窗口：frame_class={window.frame_class}；of_day={window.of_day}；"
                     f"of_week={window.of_week}")
    return "\n".join(lines) if lines else "none"


def normalize_rule(value: RuleSpec | Mapping[str, Any]) -> RuleSpec:
    """把配置映射转换成稳定的规则对象。

    @param value 规则 dataclass、配置映射或等价属性对象
    @return 规范化的不可变规则对象
    """
    if isinstance(value, RuleSpec):
        return value
    if not isinstance(value, Mapping) and hasattr(value, "template"):
        correlation = getattr(value, "correlation", None)
        if correlation is not None and not isinstance(correlation, CorrelationSpec):
            correlation = CorrelationSpec(
                operator=str(getattr(correlation, "operator", "")),
                source_field=str(getattr(correlation, "source_field", "")),
                target_field=str(getattr(correlation, "target_field", "")),
            )
        time_s = getattr(value, "time_s", None)
        return RuleSpec(
            template=str(getattr(value, "template")),
            frame_class=getattr(value, "frame_class", None),
            source=getattr(value, "source", None), target=getattr(value, "target", None),
            count=getattr(value, "count", None),
            time_s=(Decimal(str(time_s[0])), Decimal(str(time_s[1]))) if time_s is not None else None,
            correlation=correlation,
        )
    if not isinstance(value, Mapping):
        raise TypeError("sequence rule must be a mapping")
    correlation = value.get("correlation")
    if correlation is not None and not isinstance(correlation, CorrelationSpec):
        if not isinstance(correlation, Mapping):
            raise TypeError("correlation must be a mapping")
        correlation = CorrelationSpec(
            operator=str(correlation.get("operator", "")),
            source_field=str(correlation.get("source_field", "")),
            target_field=str(correlation.get("target_field", "")))
    time_s = value.get("time_s")
    if time_s is not None:
        if not isinstance(time_s, Sequence) or isinstance(time_s, (str, bytes)):
            raise TypeError("time_s must contain two numbers")
        if len(time_s) != 2:
            raise ValueError("time_s must contain exactly two numbers")
        time_s = (Decimal(str(time_s[0])), Decimal(str(time_s[1])))
    return RuleSpec(template=str(value.get("template", "")), frame_class=value.get("frame_class"),
                    source=value.get("source"), target=value.get("target"),
                    count=value.get("count"), time_s=time_s, correlation=correlation)


def validate_rule_shape(rule: RuleSpec | Mapping[str, Any]) -> RuleSpec:
    """校验规则参数矩阵并返回规范化对象。

    @param rule 待校验的规则 dataclass 或配置映射
    @return 通过参数矩阵校验的规范化规则
    """
    item = normalize_rule(rule)
    if item.template not in TEMPLATES:
        raise ValueError(f"unknown sequence rule template: {item.template}")
    if item.template in _UNARY:
        if not item.frame_class:
            raise ValueError(f"{item.template} requires frame_class")
        if any(v is not None for v in (item.source, item.target, item.correlation, item.time_s)):
            raise ValueError(f"{item.template} does not accept binary modifiers")
        if item.template in _COUNTED:
            if not isinstance(item.count, int) or isinstance(item.count, bool) or item.count <= 0:
                raise ValueError(f"{item.template}.count must be a positive integer")
        elif item.count is not None:
            raise ValueError(f"{item.template} does not accept count")
        return item
    if item.count is not None or item.frame_class is not None:
        raise ValueError(f"{item.template} requires source and target only")
    if not item.source or not item.target:
        raise ValueError(f"{item.template} requires source and target")
    if item.source == item.target:
        raise ValueError("binary sequence rule source and target must differ")
    if item.correlation is not None:
        if (item.correlation.operator != "equal" or not item.correlation.source_field
                or not item.correlation.target_field):
            raise ValueError("correlation must be an equal predicate with two fields")
    if item.time_s is not None:
        from .temporal import quantize_time_s

        quantize_time_s(item.time_s)
    return item


def validate_rules(rules: Sequence[RuleSpec | Mapping[str, Any]]) -> tuple[RuleSpec, ...]:
    """校验规则并拒绝完全重复声明。

    @param rules 按声明顺序排列的规则序列
    @return 按原声明顺序排列的规范化规则元组
    """
    normalized = tuple(validate_rule_shape(rule) for rule in rules)
    seen: set[tuple[Any, ...]] = set()
    for rule in normalized:
        key = (rule.template, rule.frame_class, rule.source, rule.target,
               rule.count, rule.time_s, rule.correlation)
        if key in seen:
            raise ValueError("duplicate sequence rule declaration")
        seen.add(key)
    return normalized


def activation_positions(rule: RuleSpec | Mapping[str, Any], word: Sequence[str]) -> tuple[int, ...]:
    """返回一条规则的标准 activation 位置，缺席时为空即 vacuity。

    @param rule 待求值的规则 dataclass 或配置映射
    @param word 冻结的帧类词
    @return 按位置升序排列的 activation 索引元组
    """
    item = normalize_rule(rule)
    if item.template in _UNARY:
        return ()
    if item.template in {"precedence", "chain_precedence"}:
        return tuple(i for i, value in enumerate(word) if value == item.target)
    if item.template == "succession":
        sources = tuple(i for i, value in enumerate(word) if value == item.source)
        targets = tuple(i for i, value in enumerate(word) if value == item.target)
        return sources + targets
    if item.template == "co_existence":
        sources = tuple(i for i, value in enumerate(word) if value == item.source)
        targets = tuple(i for i, value in enumerate(word) if value == item.target)
        return sources + targets
    return tuple(i for i, value in enumerate(word) if value == item.source)


def _next_occurrence(word: Sequence[str], label: str, position: int) -> int | None:
    """查找 position 之后的下一 occurrence。"""
    return next((i for i in range(position + 1, len(word)) if word[i] == label), None)


def _pair_for(rule: RuleSpec, word: Sequence[str], activation: int) -> tuple[CandidatePair, ...]:
    """为一个 activation 枚举标准候选对。"""
    template = rule.template
    source, target = rule.source, rule.target
    if template == "co_existence" and word[activation] == target:
        return tuple(CandidatePair(activation, index, activation)
                     for index, value in enumerate(word)
                     if index != activation and value == source)
    if template in {"precedence", "chain_precedence"}:
        pairs = ((activation, i, activation) for i in range(activation))
        pairs = (CandidatePair(activation, i, activation) for _, i, _ in pairs
                 if word[i] == source)
        if template == "chain_precedence":
            pairs = tuple(pair for pair in pairs if pair.source == activation - 1)
        else:
            pairs = tuple(pairs)
        return pairs
    if template == "succession" and word[activation] == target:
        pairs = (CandidatePair(activation, i, activation)
                 for i in range(activation) if word[i] == source)
        return tuple(pairs)
    start = activation + 1 if template in _DIRECTIONAL else 0
    indexes = range(start, len(word)) if template in _DIRECTIONAL else range(len(word))
    pairs = [CandidatePair(activation, activation, i) for i in indexes
             if i != activation and word[i] == target]
    if template == "chain_response":
        pairs = [pair for pair in pairs if pair.target == activation + 1]
    elif template == "alternate_response":
        boundary = _next_occurrence(word, source or "", activation)
        pairs = [pair for pair in pairs if boundary is None or pair.target < boundary]
    return tuple(pairs)


def candidate_pairs(rule: RuleSpec | Mapping[str, Any], word: Sequence[str],
                    activation: int | None = None) -> tuple[CandidatePair, ...]:
    """枚举规则的全部结构候选对，不套 correlation/time。

    @param rule 待枚举的规则 dataclass 或配置映射
    @param word 冻结的帧类词
    @param activation 可选的单个 activation 索引；省略时枚举全部 activation
    @return 按标准 occurrence 语义排列的候选对元组
    """
    item = normalize_rule(rule)
    if item.template in _UNARY:
        return ()
    if item.template == "not_co_existence":
        return tuple(CandidatePair(i, i, j) for i, value in enumerate(word)
                     if value == item.source
                     for j, other in enumerate(word)
                     if j != i and other == item.target)
    if item.template == "not_succession":
        return tuple(CandidatePair(i, i, j) for i, value in enumerate(word)
                     if value == item.source
                     for j, other in enumerate(word)
                     if j > i and other == item.target)
    if item.template == "succession":
        acts = activation_positions(item, word)
        result: list[CandidatePair] = []
        for pos in acts:
            result.extend(_pair_for(item, word, pos))
        return tuple(result)
    acts = activation_positions(item, word)
    selected = acts if activation is None else tuple(i for i in acts if i == activation)
    return tuple(pair for pos in selected for pair in _pair_for(item, word, pos))


def _timestamp_us(value: Any) -> int:
    """转换单个 timestamp 为整数微秒。"""
    from .temporal import timestamp_us

    return timestamp_us(value)


def _time_match(rule: RuleSpec, pair: CandidatePair,
                timestamps: Sequence[Any] | None) -> bool:
    """判定 occurrence pair 是否落入半开 time_s。"""
    if rule.time_s is None or timestamps is None:
        return True
    from .temporal import quantize_time_s

    lo, hi = quantize_time_s(rule.time_s)
    delta = _timestamp_us(timestamps[pair.target]) - _timestamp_us(timestamps[pair.source])
    if rule.template in _ABSOLUTE:
        delta = abs(delta)
    return lo <= delta < hi


def _unary_valid(rule: RuleSpec, word: Sequence[str]) -> bool:
    """求值一元模板。"""
    count = sum(value == rule.frame_class for value in word)
    if rule.template == "existence":
        return count >= int(rule.count or 0)
    if rule.template == "absence":
        return count < int(rule.count or 0)
    if rule.template == "exactly":
        return count == int(rule.count or 0)
    if not word:
        return False
    return word[0] == rule.frame_class if rule.template == "init" else word[-1] == rule.frame_class


def evaluate_rule(rule: RuleSpec | Mapping[str, Any], word: Sequence[str],
                  timestamps: Sequence[Any] | None = None) -> RuleEvaluation:
    """按有限迹 DECLARE 语义求值一条规则，蕴含型 activation 遵循 vacuity。

    @param rule 待求值的规则 dataclass 或配置映射
    @param word 冻结的帧类词
    @param timestamps 与 word 对齐的时间戳；无时间约束时可省略
    @return 单条规则的有效性、候选、匹配和首错信息
    """
    item = validate_rule_shape(rule)
    if item.template in _UNARY:
        valid = _unary_valid(item, word)
        failure = None if valid else RuleFailure(None, "control_flow", ())
        return RuleEvaluation(item, valid, (), (), (), failure)
    activations = activation_positions(item, word)
    if item.template in {"not_co_existence", "not_succession"}:
        candidates = candidate_pairs(item, word)
        matches = tuple(pair for pair in candidates if _time_match(item, pair, timestamps))
        valid = not matches
        failure = None if valid else RuleFailure(matches[0].activation, "negative_match", matches)
        return RuleEvaluation(item, valid, activations, candidates, matches, failure)
    candidates: list[CandidatePair] = []
    matches: list[CandidatePair] = []
    for activation in activations:
        local = _pair_for(item, word, activation)
        candidates.extend(local)
        matches.extend(pair for pair in local if _time_match(item, pair, timestamps))
        if not any(pair.activation == activation for pair in matches):
            failure = RuleFailure(activation, "missing_target", local)
            return RuleEvaluation(item, False, activations, tuple(candidates), tuple(matches), failure)
    return RuleEvaluation(item, True, activations, tuple(candidates), tuple(matches), None)


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
        return json.dumps(left, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode() == json.dumps(
                              right, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        _log.error("payload canonical equality failed: %s", type(exc).__name__)
        return False


def _field(payload: Any, name: str) -> Any:
    """从结构化 payload 读取一个顶层字段。"""
    return payload.get(name) if isinstance(payload, Mapping) else None


def _correlation_match(rule: RuleSpec, pair: CandidatePair,
                       payloads: Sequence[Any]) -> bool:
    """判定 pair 的类型敏感字段 equality。"""
    corr = rule.correlation
    if corr is None:
        return True
    return canonical_equal(_field(payloads[pair.source], corr.source_field),
                           _field(payloads[pair.target], corr.target_field))


def evaluate_payload_rules(rules: Sequence[RuleSpec | Mapping[str, Any]], word: Sequence[str],
                           payloads: Sequence[Any], timestamps: Sequence[Any] | None = None,
                           ) -> PayloadEvaluation:
    """按 C0→Ce→Ct 顺序判定 payload 规则并返回精确失败归因。

    @param rules 生效规则，按声明顺序
    @param word 冻结帧类词
    @param payloads 与 word 等长的 JSON-compatible payload
    @param timestamps 与 word 等长的时间戳；无 time_s 时可省略
    @return 按规则顺序汇总的 C0/Ce/Ct 判定与单点作废归因
    """
    normalized = validate_rules(rules)
    if len(word) != len(payloads) or (timestamps is not None and len(word) != len(timestamps)):
        raise ValueError("word, payloads and timestamps must have equal lengths")
    evaluations: list[RuleEvaluation] = []
    for rule in normalized:
        base = evaluate_rule(rule, word)
        if rule.template in _UNARY:
            if not base.valid:
                return _internal_payload_failure(
                    rule, "unary control-flow rule failed after planner validation")
            evaluations.append(base)
            continue
        c0 = candidate_pairs(rule, word)
        ce = tuple(pair for pair in c0 if _correlation_match(rule, pair, payloads))
        ct = tuple(pair for pair in ce if _time_match(rule, pair, timestamps))
        if rule.template in {"not_co_existence", "not_succession"}:
            if not ct:
                evaluations.append(RuleEvaluation(rule, True, (), c0, ct, None))
                continue
            if rule.correlation is not None:
                return _payload_failure(_PayloadFailureSpec(
                    tuple(evaluations), rule, word, "correlation_scrapped", 1, 0))
            return _internal_payload_failure(rule, "negative rule violated without correlation")
        failure, reason = _positive_failure(rule, word, c0, ce, ct)
        if failure is None:
            evaluations.append(RuleEvaluation(rule, True, activation_positions(rule, word),
                                              c0, ct, None))
            continue
        if reason == "correlation_scrapped":
            return _payload_failure(_PayloadFailureSpec(
                tuple(evaluations), rule, word, reason, 1, 0, failure))
        if reason == "temporal_scrapped":
            return _payload_failure(_PayloadFailureSpec(
                tuple(evaluations), rule, word, reason, 0, 1, failure))
        return _internal_payload_failure(rule, "planner potential witness was not realized")
    return PayloadEvaluation(True, tuple(evaluations))


def _positive_failure(rule: RuleSpec, word: Sequence[str], c0: tuple[CandidatePair, ...],
                      ce: tuple[CandidatePair, ...], ct: tuple[CandidatePair, ...],
                      ) -> tuple[RuleFailure | None, str | None]:
    """定位正规则首个 activation 的 C0/Ce/Ct 失败层。"""
    for activation in activation_positions(rule, word):
        base = tuple(pair for pair in c0 if pair.activation == activation)
        equal = tuple(pair for pair in ce if pair.activation == activation)
        timed = tuple(pair for pair in ct if pair.activation == activation)
        if timed:
            continue
        if rule.correlation is not None and not equal:
            return RuleFailure(activation, "correlation_scrapped", base), "correlation_scrapped"
        if rule.correlation is not None and rule.time_s is not None and equal:
            return RuleFailure(activation, "temporal_scrapped", equal), "temporal_scrapped"
        return RuleFailure(activation, "planner_invariant", equal or base), "internal"
    return None, None


def _payload_failure(spec: _PayloadFailureSpec) -> PayloadEvaluation:
    """构造作废结果并保证首错只有一个计数。"""
    candidates = spec.failure.candidates if spec.failure is not None else ()
    result = RuleEvaluation(spec.rule, False, activation_positions(spec.rule, spec.word),
                            candidates, (), spec.failure)
    return PayloadEvaluation(False, spec.previous + (result,), 1, spec.correlation,
                             spec.temporal, 0, spec.rule, spec.reason)


def _internal_payload_failure(rule: RuleSpec, message: str) -> PayloadEvaluation:
    """记录 invariant 破坏并抛出统一内部异常。"""
    _log.error("sequence rule invariant failed: %s", message,
               extra={"stage": "generate", "rule": rule.template})
    from labelkit.common.errors import InternalError

    raise InternalError(message)

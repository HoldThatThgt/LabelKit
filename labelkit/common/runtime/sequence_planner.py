"""v1.16 时间流序列规则的唯一 CP-SAT 联合问题入口。"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from enum import Enum
import itertools
import logging
import random
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any

from labelkit.common.errors import InternalError
from .declare import RuleSpec, normalize_rule, validate_rules
from .temporal import (
    CalendarWindow,
    TimeInterval,
    fixed_offset,
    normalize_calendar_windows,
    quantize_frame_gap,
    replay_guard,
    timestamp_us,
    window_day_options,
)

_log = logging.getLogger("labelkit.sequence_planner")
MAX_PROTO_ENTRIES = 250_000
DETERMINISTIC_TIME_LIMIT = 10.0
_WEEK_US = 7 * 86_400 * 1_000_000
_DAY_US = 86_400 * 1_000_000


class PlannerStatus(str, Enum):
    """CP-SAT 状态的稳定英文映射。"""

    OPTIMAL = "OPTIMAL"
    FEASIBLE = "FEASIBLE"
    INFEASIBLE = "INFEASIBLE"
    UNKNOWN = "UNKNOWN"
    MODEL_INVALID = "MODEL_INVALID"


class PlannerConfigError(ValueError):
    """联合问题在求解前发现不可用配置。"""


class PlannerInternalError(InternalError):
    """求解器或已通过预检的问题违反内部不变量。"""


@dataclass(frozen=True)
class AttemptSpec:
    """一条待规划 attempt 的冻结元数据。"""

    index: int
    class_name: str
    length: int
    allowed_classes: tuple[str, ...] = ()
    rules: tuple[RuleSpec, ...] | None = None
    windows: tuple[CalendarWindow, ...] | None = None
    length_range: tuple[int, int] | None = None
    tier_rank: int | None = None


@dataclass(frozen=True)
class PlannerQuestion:
    """M1、estimate 与 M6 共用的冻结问题输入。"""

    frame_classes: tuple[str, ...]
    attempts: tuple[AttemptSpec, ...]
    sessions: int
    stream_gap_us: int
    frame_gap: TimeInterval
    ts_start_us: int
    session_max_len: int
    ts_offset_s: int = 0
    session_max_span_us: int = 0
    noise_target: int = 0
    rules: tuple[RuleSpec, ...] = ()
    windows: tuple[CalendarWindow, ...] = ()
    solver_seed: int = 0
    length_domains: tuple[tuple[int, int] | None, ...] = ()
    noise_ratio: Decimal = Decimal(0)

    def with_lengths(self, lengths: Sequence[int]) -> "PlannerQuestion":
        """返回只替换 attempt 长度的新问题。

        @param lengths 按 attempt 顺序排列的最终长度
        @return 仅替换长度和 noise target 的新问题对象
        """
        if len(lengths) != len(self.attempts):
            raise ValueError("length vector must cover all attempts")
        return replace(self, attempts=tuple(replace(item, length=int(length))
                                            for item, length in zip(self.attempts, lengths)),
                       length_domains=tuple(None for _ in lengths),
                       noise_target=int((self.noise_ratio * sum(lengths)).to_integral_value(
                           rounding=ROUND_HALF_EVEN))
                       if self.noise_ratio else self.noise_target)

@dataclass(frozen=True)
class TimelineFrame:
    """解码后的一帧任务或噪音槽。"""

    owner: int | None
    position: int | None
    frame_class: str | None
    timestamp_us: int
    noise: bool
    session_index: int | None = None


@dataclass(frozen=True)
class SessionLayout:
    """一个冻结 session 的 owner、任务帧和噪音槽。"""

    index: int
    owners: tuple[int, ...]
    frames: tuple[TimelineFrame, ...]
    start_us: int
    end_us: int


@dataclass(frozen=True)
class PlannerLayout:
    """CP-SAT 解码后的完整 skeleton。"""

    words: tuple[tuple[str, ...], ...]
    timestamps_us: tuple[tuple[int, ...], ...]
    sessions: tuple[SessionLayout, ...]
    noise_slots: tuple[TimelineFrame, ...]
    planned_noise_slots: int
    status: PlannerStatus
    objective_value: int = 0


@dataclass(frozen=True)
class PlannerResult:
    """一次求解的状态、规模和可选 layout。"""

    status: PlannerStatus
    layout: PlannerLayout | None
    proto_entries: int
    objective_value: int = 0


@dataclass(frozen=True)
class LocalCandidateResult:
    """单类、单档位、单长度的局部潜在可行状态。"""

    class_name: str
    tier_rank: int | None
    length: int
    status: PlannerStatus


@dataclass(frozen=True)
class ProjectedLayout:
    """LLM 作废后的固定 skeleton 投影。"""

    sessions: tuple[SessionLayout, ...]
    noise_slots: tuple[TimelineFrame, ...]
    crossed_sessions: int


def _cp_model() -> Any:
    """延迟导入固定版本 OR-Tools。"""
    try:
        from ortools.sat.python import cp_model

        return cp_model
    except ImportError as exc:
        _log.error("OR-Tools CP-SAT dependency is unavailable")
        raise PlannerInternalError("OR-Tools CP-SAT dependency is unavailable") from exc


def _attr(value: Any, name: str, default: Any = None) -> Any:
    """从 mapping 或冻结 dataclass 读取字段。"""
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _rules_for(question: PlannerQuestion, attempt: AttemptSpec) -> tuple[RuleSpec, ...]:
    """取 attempt 的按类覆盖规则。"""
    return question.rules if attempt.rules is None else attempt.rules


def _windows_for(question: PlannerQuestion, attempt: AttemptSpec) -> tuple[CalendarWindow, ...]:
    """取 attempt 的按类覆盖窗口。"""
    return question.windows if attempt.windows is None else attempt.windows


def _normalize_attempt(value: AttemptSpec | Mapping[str, Any], index: int) -> AttemptSpec:
    """把外部 attempt 适配为稳定 dataclass。"""
    if isinstance(value, AttemptSpec):
        return value
    length_range = _attr(value, "length_range", _attr(value, "len_range", None))
    raw_rules = _attr(value, "rules", None)
    raw_windows = _attr(value, "windows", None)
    rules = None if raw_rules is None else tuple(normalize_rule(item) for item in raw_rules)
    windows = None if raw_windows is None else normalize_calendar_windows(raw_windows)
    return AttemptSpec(
        index=int(_attr(value, "index", index)),
        class_name=str(_attr(value, "class_name", _attr(value, "sequence_class", ""))),
        length=int(_attr(value, "length", _attr(value, "len", 0))),
        allowed_classes=tuple(_attr(value, "allowed_classes", ()) or ()),
        rules=rules, windows=windows,
        length_range=tuple(length_range) if length_range is not None else None,
        tier_rank=_attr(value, "tier_rank", None),
    )


def build_question(frame_classes: Sequence[str], attempts: Sequence[AttemptSpec | Mapping[str, Any]],
                   stream: Any, generate_stream: Any, solver_seed: int = 0) -> PlannerQuestion:
    """构造唯一联合问题，M1/estimate/M6 必须共享此入口。

    @param frame_classes 全部可用帧类，顺序影响 CP-SAT 的固定决策顺序
    @param attempts 已按配额顺序冻结的 attempt 元数据
    @param stream 读取 gap_s/session_max_len/session_max_span_s
    @param generate_stream 读取 frame_gap_s/ts_start/rules/windows/noise_target
    @param solver_seed CP-SAT 固定随机种子
    @return 通过基本校验的不可变联合规划问题
    """
    classes = tuple(dict.fromkeys(str(name) for name in frame_classes))
    if not classes:
        raise PlannerConfigError("sequence planner requires at least one frame class")
    normalized_attempts = tuple(_normalize_attempt(item, index)
                               for index, item in enumerate(attempts))
    global_rules = validate_rules(_attr(generate_stream, "rules", ()) or ())
    global_windows = normalize_calendar_windows(_attr(generate_stream, "windows", ()) or ())
    frame_gap = quantize_frame_gap(_attr(generate_stream, "frame_gap_s", (0, 0)))
    gap = _attr(stream, "gap_s", 0)
    ts_start = _attr(generate_stream, "ts_start", 0)
    offset = fixed_offset(ts_start).utcoffset(None)
    offset_s = int(offset.total_seconds()) if offset is not None else 0
    question = PlannerQuestion(
        frame_classes=classes, attempts=normalized_attempts,
        sessions=min(int(_attr(generate_stream, "sessions", 0)), len(normalized_attempts)),
        stream_gap_us=int(Decimal(str(gap)) * 1_000_000),
        frame_gap=frame_gap,
        ts_start_us=timestamp_us(ts_start), ts_offset_s=offset_s,
        session_max_len=int(_attr(stream, "session_max_len", 0)),
        session_max_span_us=int(Decimal(str(_attr(stream, "session_max_span_s", 0))) * 1_000_000),
        noise_target=_noise_target(generate_stream, normalized_attempts),
        rules=global_rules, windows=global_windows,
        solver_seed=int(solver_seed),
        noise_ratio=Decimal(str(_attr(generate_stream, "noise_ratio", 0))),
    )
    _validate_question(question)
    return question


def question_from_config(config: Any, attempts: Sequence[AttemptSpec | Mapping[str, Any]] | None = None,
                         solver_seed: int = 0) -> PlannerQuestion:
    """从 ResolvedConfig 或等价 frozen adapter 构造问题。

    @param config 已解析配置或等价属性适配器
    @param attempts 可选的已冻结 attempt；省略时从配置展开
    @param solver_seed CP-SAT 固定随机种子
    @return 通过基本校验的不可变联合规划问题
    """
    frame_views = _attr(config, "frame_class_views", {}) or {}
    classes = tuple(frame_views) if isinstance(frame_views, Mapping) else tuple(
        _attr(config, "frame_classes", ()) or ())
    gs = _attr(config, "generate_stream", config)
    stream = _attr(config, "stream", object())
    if attempts is None:
        attempts = _attempts_from_config(config, classes)
    else:
        attempts = _apply_limit(config, tuple(attempts))
    return build_question(classes, attempts, stream, gs, solver_seed)


def _attempts_from_config(config: Any, frame_classes: Sequence[str]) -> tuple[AttemptSpec, ...]:
    """为 estimate 适配当前配置的 attempt 元数据。"""
    views = _attr(config, "class_views", {}) or {}
    result: list[AttemptSpec] = []
    gs = _attr(config, "generate_stream", config)
    global_tiers = tuple(_attr(gs, "tiers", ()) or ())
    from labelkit.common.config.model import apportion_tiers, effective_tiers

    for class_name in sorted(views):
        view = views[class_name]
        generate = _attr(view, "generate", view)
        count = int(_attr(generate, "sequences", 0))
        bounds = tuple(_attr(generate, "len_range", (1, 1)))
        class_tiers = _attr(view, "tiers", None)
        tiers = effective_tiers(class_tiers, global_tiers)
        quotas = apportion_tiers(count, tiers)
        rules, windows = _class_rules_windows(view)
        for tier, quota in zip(tiers, quotas):
            for _ in range(quota):
                result.append(AttemptSpec(
                    index=len(result), class_name=class_name, length=bounds[0],
                    allowed_classes=tuple(tier.frame_classes), length_range=(int(bounds[0]), int(bounds[1])),
                    rules=rules, windows=windows, tier_rank=tier.tier_rank))
        if not tiers:
            for _ in range(count):
                result.append(AttemptSpec(
                    index=len(result), class_name=class_name, length=bounds[0],
                    length_range=(int(bounds[0]), int(bounds[1])), rules=rules, windows=windows))
    return _apply_limit(config, tuple(result))


def _apply_limit(config: Any, attempts: tuple[AttemptSpec, ...]) -> tuple[AttemptSpec, ...]:
    """按冻结类名字典序后的全流配额前缀应用 limit。"""
    limit = _attr(config, "limit", None)
    if limit is None:
        return attempts
    if int(limit) < 0:
        raise PlannerConfigError("limit must be non-negative")
    return attempts[:int(limit)]


def check_local_candidates(config: Any) -> tuple[LocalCandidateResult, ...]:
    """检查每个生效类、档位与长度的单序列结构/时间 potential。

    @param config 冻结 ResolvedConfig 或等价属性适配器
    @return 按类名字典序、档位序、长度升序排列的 solver 状态
    """
    frame_views = _attr(config, "frame_class_views", {}) or {}
    classes = tuple(frame_views) if isinstance(frame_views, Mapping) else tuple(
        _attr(config, "frame_classes", ()) or ())
    views = _attr(config, "class_views", {}) or {}
    gs = _attr(config, "generate_stream", config)
    stream = _attr(config, "stream", object())
    local_stream = SimpleNamespace(
        gap_s=_attr(stream, "gap_s", 0),
        session_max_len=_attr(stream, "session_max_len", 0),
        session_max_span_s=_attr(stream, "session_max_span_s", 0),
    )
    local_generate = SimpleNamespace(
        frame_gap_s=_attr(gs, "frame_gap_s", (0, 0)), ts_start=_attr(gs, "ts_start", 0),
        sessions=1, rules=(), windows=(), noise_ratio=0,
    )
    results: list[LocalCandidateResult] = []
    from labelkit.common.config.model import effective_tiers

    global_tiers = tuple(_attr(gs, "tiers", ()) or ())
    for class_name in sorted(views):
        view = views[class_name]
        generate = _attr(view, "generate", view)
        bounds = tuple(_attr(generate, "len_range", (1, 1)))
        tiers = effective_tiers(_attr(view, "tiers", None), global_tiers)
        table = tiers or (None,)
        rules, windows = _class_rules_windows(view)
        for tier in table:
            for length in range(int(bounds[0]), int(bounds[1]) + 1):
                attempt = AttemptSpec(
                    0, class_name, length,
                    tuple(tier.frame_classes) if tier is not None else (),
                    rules, windows, (length, length),
                    tier.tier_rank if tier is not None else None,
                )
                question = build_question(classes, (attempt,), local_stream, local_generate)
                status = solve_question(question).status
                results.append(LocalCandidateResult(class_name, attempt.tier_rank, length, status))
    return tuple(results)


def _class_rules_windows(view: Any) -> tuple[tuple[RuleSpec, ...] | None,
                                                   tuple[CalendarWindow, ...] | None]:
    """读取 ClassView 顶层三态 rules/windows。"""
    generate = _attr(view, "generate", view)
    raw_rules = _attr(view, "rules", _attr(generate, "rules", None))
    raw_windows = _attr(view, "windows", _attr(generate, "windows", None))
    rules = None if raw_rules is None else tuple(normalize_rule(item) for item in raw_rules)
    windows = None if raw_windows is None else normalize_calendar_windows(raw_windows)
    return rules, windows


def _noise_target(generate_stream: Any, attempts: Sequence[AttemptSpec]) -> int:
    """按冻结长度计算 noise target。"""
    explicit = _attr(generate_stream, "noise_target", None)
    if explicit is not None:
        return int(explicit)
    ratio = Decimal(str(_attr(generate_stream, "noise_ratio", 0)))
    value = ratio * sum(item.length for item in attempts)
    return int(value.to_integral_value(rounding=ROUND_HALF_EVEN))


def _max_noise_target(ratio: Decimal, attempts: Sequence[AttemptSpec]) -> int:
    """按可变长度上界计算联合模型需要创建的 noise 槽数。"""
    if not ratio:
        return 0
    value = ratio * sum((item.length_range[1] if item.length_range else item.length)
                        for item in attempts)
    return int(value.to_integral_value(rounding=ROUND_HALF_EVEN))


def _validate_question(question: PlannerQuestion) -> None:
    """检查模型输入的基本不变量。"""
    n = len(question.attempts)
    if n == 0:
        raise PlannerConfigError("sequence planner requires at least one attempt")
    if not 1 <= question.sessions <= n <= 2 * question.sessions:
        raise PlannerConfigError("sessions must satisfy sessions <= attempts <= 2*sessions")
    if question.session_max_len <= 0 or question.stream_gap_us <= 0:
        raise PlannerConfigError("stream session limits must be positive")
    guard = replay_guard(question.frame_gap, question.stream_gap_us)
    if guard.lo_us < 1 or guard.hi_us < guard.lo_us:
        raise PlannerConfigError("frame_gap_s does not satisfy the replay guard")
    for attempt in question.attempts:
        if attempt.length < 1:
            raise PlannerConfigError("attempt length must be positive")
        allowed = attempt.allowed_classes or question.frame_classes
        if not set(allowed) <= set(question.frame_classes):
            raise PlannerConfigError("attempt allowed_classes contains an unknown frame class")
        rules = validate_rules(_rules_for(question, attempt))
        windows = normalize_calendar_windows(_windows_for(question, attempt))
        if any(not _rule_references_known_classes(rule, question.frame_classes) for rule in rules):
            raise PlannerConfigError("sequence rule references an unknown frame class")
        if any(item.frame_class not in question.frame_classes for item in windows):
            raise PlannerConfigError("calendar window references an unknown frame class")
    if question.noise_target < 0:
        raise PlannerConfigError("noise_target must be non-negative")
    if tuple(item.index for item in question.attempts) != tuple(range(n)):
        raise PlannerConfigError("attempt indexes must be contiguous and zero based")
    if question.length_domains and len(question.length_domains) != n:
        raise PlannerConfigError("length_domains must cover all attempts")
    for domain in question.length_domains:
        if domain is not None and (domain[0] < 1 or domain[0] > domain[1]):
            raise PlannerConfigError("invalid variable length domain")


def _rule_references_known_classes(rule: RuleSpec, classes: Sequence[str]) -> bool:
    """判断规则中的每个帧类引用都存在。"""
    known = set(classes)
    values = (rule.frame_class,) if rule.template in {
        "existence", "absence", "exactly", "init", "end"
    } else (rule.source, rule.target)
    return all(value in known for value in values)


@dataclass
class _ModelContext:
    """CP-SAT 变量集合，避免跨函数传递大量参数。"""

    cp: Any
    model: Any
    class_index: dict[str, int]
    words: list[list[Any]]
    classes: list[list[dict[str, Any]]]
    timestamps: list[list[Any]]
    active: list[list[Any]]
    assignments: list[list[Any]]
    noise_presence: list[Any]
    noise_assignments: list[list[Any]]
    noise_timestamps: list[Any]
    session_starts: list[Any]
    session_ends: list[Any]
    horizon: int
    covered: list[dict[tuple[int, int], list[Any]]]


def _horizon(question: PlannerQuestion) -> int:
    """按七日 start window 与 owner span 递推 timestamp 上界。"""
    owner_spans = tuple(_attempt_span(question, item) for item in question.attempts)
    pair_bound = max(owner_spans, default=0)
    pair_bound = max(pair_bound, max((left + right for left, right in itertools.combinations(
        owner_spans, 2)), default=0))
    session_span = min(question.session_max_span_us, pair_bound) if question.session_max_span_us else pair_bound
    return question.ts_start_us + question.sessions * (_WEEK_US + session_span + question.stream_gap_us) - 1


def _attempt_span(question: PlannerQuestion, attempt: AttemptSpec) -> int:
    """返回 attempt 在当前模型中的最大 owner span。"""
    if question.length_domains and question.length_domains[attempt.index] is not None:
        length = question.length_domains[attempt.index][1]
    else:
        length = attempt.length
    return max(0, int(length) - 1) * question.stream_gap_us


def _session_span(question: PlannerQuestion) -> int:
    """返回受 owner 长度与配置上限共同限制的 session span。"""
    owner_spans = tuple(_attempt_span(question, item) for item in question.attempts)
    bound = max(owner_spans, default=0)
    bound = max(bound, max((left + right for left, right in itertools.combinations(
        owner_spans, 2)), default=0))
    configured = question.session_max_span_us
    return min(configured, bound) if configured else bound


def _new_context(question: PlannerQuestion) -> _ModelContext:
    """创建 CP-SAT 模型与基础变量。"""
    cp = _cp_model()
    model = cp.CpModel()
    classes = {name: index for index, name in enumerate(question.frame_classes)}
    horizon = _horizon(question)
    ctx = _ModelContext(
        cp=cp, model=model, class_index=classes, words=[], classes=[], timestamps=[],
        active=[], assignments=[], noise_presence=[], noise_assignments=[],
        noise_timestamps=[], session_starts=[], session_ends=[], horizon=horizon, covered=[],
    )
    for attempt in question.attempts:
        _new_attempt_vars(ctx, question, attempt)
    _add_primary_uniqueness(ctx, question)
    _new_session_vars(ctx, question)
    _new_noise_vars(ctx, question)
    _minimize_timeline_end(ctx, question)
    return ctx


def _minimize_timeline_end(ctx: _ModelContext, question: PlannerQuestion) -> None:
    """无 noise 目标时选择最早结束的可行布局。"""
    if ctx.noise_presence:
        return
    end = ctx.model.NewIntVar(question.ts_start_us, ctx.horizon, "timeline_end")
    for index, row in enumerate(ctx.timestamps):
        for position, timestamp in enumerate(row):
            ctx.model.Add(end >= timestamp).OnlyEnforceIf(ctx.active[index][position])
    ctx.model.Minimize(end)


def _add_primary_uniqueness(ctx: _ModelContext, question: PlannerQuestion) -> None:
    """约束 active primary timestamp 全局唯一。"""
    variables = [(index, pos, value) for index, row in enumerate(ctx.timestamps)
                 for pos, value in enumerate(row)]
    if not question.length_domains:
        ctx.model.AddAllDifferent([value for _, _, value in variables])
        return
    for left, right in itertools.combinations(variables, 2):
        ctx.model.Add(left[2] != right[2]).OnlyEnforceIf(
            [ctx.active[left[0]][left[1]], ctx.active[right[0]][right[1]]])


def _new_attempt_vars(ctx: _ModelContext, question: PlannerQuestion, attempt: AttemptSpec) -> None:
    """创建单 attempt 的 word、one-hot 与 timestamp 变量。"""
    model = ctx.model
    domain = question.length_domains[attempt.index] if question.length_domains else None
    max_length = domain[1] if domain is not None else attempt.length
    words = [model.NewIntVar(0, len(question.frame_classes) - 1,
                             f"word_{attempt.index}_{pos}") for pos in range(max_length)]
    active = _active_positions(ctx, attempt, max_length, domain)
    class_rows: list[dict[str, Any]] = []
    for pos, word in enumerate(words):
        row = {name: model.NewBoolVar(f"class_{attempt.index}_{pos}_{name}")
               for name in question.frame_classes}
        model.Add(sum(row.values()) == active[pos])
        for name, flag in row.items():
            model.Add(word == ctx.class_index[name]).OnlyEnforceIf(flag)
            model.Add(word != ctx.class_index[name]).OnlyEnforceIf([flag.Not(), active[pos]])
        class_rows.append(row)
    if attempt.allowed_classes:
        allowed = attempt.allowed_classes
        for name in question.frame_classes:
            if name not in allowed:
                for row in class_rows:
                    model.Add(row[name] == 0)
        for name in allowed:
            model.Add(sum(row[name] for row in class_rows) >= 1)
    times = [model.NewIntVar(question.ts_start_us, ctx.horizon,
                             f"ts_{attempt.index}_{pos}") for pos in range(max_length)]
    ctx.words.append(words)
    ctx.classes.append(class_rows)
    ctx.active.append(active)
    ctx.timestamps.append(times)
    ctx.covered.append({})
    _add_attempt_rules(ctx, question, attempt)


def _active_positions(ctx: _ModelContext, attempt: AttemptSpec, max_length: int,
                      domain: tuple[int, int] | None) -> list[Any]:
    """创建固定或可变长度的前缀 active 变量。"""
    model = ctx.model
    if domain is None:
        result = [model.NewBoolVar(f"active_{attempt.index}_{pos}") for pos in range(max_length)]
        for item in result:
            model.Add(item == 1)
        return result
    result = [model.NewBoolVar(f"active_{attempt.index}_{pos}") for pos in range(max_length)]
    for left, right in zip(result, result[1:]):
        model.Add(left >= right)
    model.Add(sum(result) >= domain[0])
    model.Add(sum(result) <= domain[1])
    return result


def _last_active_flags(ctx: _ModelContext, attempt: AttemptSpec) -> tuple[tuple[int, Any], ...]:
    """为可变长度前缀创建 last-active 标记。"""
    model = ctx.model
    active = ctx.active[attempt.index]
    result = []
    for position, current in enumerate(active):
        marker = model.NewBoolVar(f"last_active_{attempt.index}_{position}")
        model.AddImplication(marker, current)
        if position + 1 < len(active):
            model.AddImplication(marker, active[position + 1].Not())
            model.AddBoolOr([marker, current.Not(), active[position + 1]])
        else:
            model.AddBoolOr([marker, current.Not()])
        result.append((position, marker))
    return tuple(result)


@dataclass(frozen=True)
class _PairSpec:
    """规则候选对的结构位置。"""

    activation: int
    source: int
    target: int
    forbidden_source_positions: tuple[int, ...] = ()
    source_label: str = ""
    target_label: str = ""


@dataclass(frozen=True)
class _PatternSpec:
    """owner 交替三点模式。"""

    pair: Any
    session: int
    first: int
    first_pos: int
    middle: int
    middle_pos: int
    last: int
    last_pos: int


@dataclass(frozen=True)
class _DecodeSpec:
    """解码 session 所需的冻结变量与结果。"""

    ctx: _ModelContext
    question: PlannerQuestion
    solver: Any
    words: tuple[tuple[str, ...], ...]
    stamps: tuple[tuple[int, ...], ...]
    owners: tuple[int, ...]
    noise: tuple[TimelineFrame, ...]


def _add_attempt_rules(ctx: _ModelContext, question: PlannerQuestion, attempt: AttemptSpec) -> None:
    """把一个 attempt 的规则、窗口与默认相邻边加入模型。"""
    rules = _rules_for(question, attempt)
    for rule in rules:
        _add_rule_constraints(ctx, question, attempt, rule)
    _add_calendar_constraints(ctx, question, attempt)
    _add_default_edges(ctx, question, attempt)


def _add_rule_constraints(ctx: _ModelContext, question: PlannerQuestion,
                          attempt: AttemptSpec, rule: RuleSpec) -> None:
    """分派一元、正规则与负规则。"""
    model = ctx.model
    rows = ctx.classes[attempt.index]
    if rule.template in {"existence", "absence", "exactly"}:
        total = sum(row[rule.frame_class] for row in rows)
        bound = int(rule.count or 0)
        model.Add(total >= bound if rule.template == "existence" else
                  total < bound if rule.template == "absence" else total == bound)
        return
    if rule.template in {"init", "end"}:
        if rule.template == "init":
            model.Add(rows[0][rule.frame_class] == 1)
        else:
            for position, marker in _last_active_flags(ctx, attempt):
                model.Add(rows[position][rule.frame_class] == 1).OnlyEnforceIf(marker)
        return
    if rule.template in {"not_co_existence", "not_succession"}:
        if rule.correlation is None:
            _add_negative_rule(ctx, attempt, rule)
        return
    if rule.template == "co_existence":
        _add_obligation(ctx, attempt, rule, "responded")
        _add_obligation(ctx, attempt, rule, "reverse_responded")
    elif rule.template == "succession":
        _add_obligation(ctx, attempt, rule, "response")
        _add_obligation(ctx, attempt, rule, "precedence")
    else:
        _add_obligation(ctx, attempt, rule, rule.template)


def _add_obligation(ctx: _ModelContext, attempt: AttemptSpec, rule: RuleSpec,
                    mode: str) -> None:
    """为每个 activation 强制恰选一个 structural/time potential witness。"""
    rows = ctx.classes[attempt.index]
    source_label, target_label, activation_label = _labels(rule, mode)
    capacity = len(rows)
    for activation in range(capacity):
        candidates = _candidate_specs(capacity, activation, source_label,
                                      target_label, mode)
        active = rows[activation][activation_label]
        witnesses = [_make_pair(ctx, attempt, rule, replace(item,
                                                           source_label=source_label,
                                                           target_label=target_label))
                     for item in candidates]
        if not witnesses:
            ctx.model.Add(active == 0)
            continue
        ctx.model.Add(sum(witnesses) == 1).OnlyEnforceIf(active)
        ctx.model.Add(sum(witnesses) == 0).OnlyEnforceIf(active.Not())


def _labels(rule: RuleSpec, mode: str) -> tuple[str, str, str]:
    """返回 witness 的 source、target 与 activation 标签。"""
    if mode == "reverse_responded":
        return str(rule.target), str(rule.source), str(rule.target)
    if mode in {"precedence", "chain_precedence"}:
        return str(rule.source), str(rule.target), str(rule.target)
    return str(rule.source), str(rule.target), str(rule.source)


def _candidate_specs(length: int, activation: int, source: str, target: str,
                     mode: str) -> tuple[_PairSpec, ...]:
    """枚举不含 payload 的标准 occurrence 候选位置。"""
    if mode == "precedence":
        return tuple(_PairSpec(activation, source_pos, activation)
                     for source_pos in range(activation))
    if mode == "chain_precedence":
        return ((_PairSpec(activation, activation - 1, activation),)
                if activation else ())
    if mode == "chain_response":
        return ((_PairSpec(activation, activation, activation + 1),)
                if activation + 1 < length else ())
    if mode == "reverse_responded":
        return tuple(_PairSpec(activation, activation, target_pos)
                     for target_pos in range(length) if target_pos != activation)
    if mode == "response":
        return tuple(_PairSpec(activation, activation, target_pos)
                     for target_pos in range(activation + 1, length))
    if mode == "alternate_response":
        result = []
        for target_pos in range(activation + 1, length):
            blocked = tuple(range(activation + 1, target_pos))
            result.append(_PairSpec(activation, activation, target_pos, blocked))
        return tuple(result)
    return tuple(_PairSpec(activation, activation, target_pos)
                 for target_pos in range(length) if target_pos != activation)


def _make_pair(ctx: _ModelContext, attempt: AttemptSpec, rule: RuleSpec,
               spec: _PairSpec) -> Any:
    """创建结构候选对并附加显式时间区间。"""
    model = ctx.model
    rows = ctx.classes[attempt.index]
    source = spec.source_label or str(rule.source)
    target = spec.target_label or str(rule.target)
    literals = [rows[spec.source][source], rows[spec.target][target]]
    literals.extend(rows[pos][source].Not() for pos in spec.forbidden_source_positions)
    pair = model.NewBoolVar(f"witness_{attempt.index}_{spec.activation}_{spec.source}_{spec.target}")
    for literal in literals:
        model.AddImplication(pair, literal)
    if rule.time_s is not None:
        _add_time_constraint(ctx, attempt, rule, pair, spec)
        if abs(spec.source - spec.target) == 1:
            edge = tuple(sorted((spec.source, spec.target)))
            ctx.covered[attempt.index].setdefault(edge, []).append(pair)
    return pair


def _add_time_constraint(ctx: _ModelContext, attempt: AttemptSpec, rule: RuleSpec,
                         pair: Any, spec: _PairSpec) -> None:
    """把 time_s 半开区间约束到 witness。"""
    lo, hi = _time_bounds(rule)
    source = ctx.timestamps[attempt.index][spec.source]
    target = ctx.timestamps[attempt.index][spec.target]
    delta = target - source
    if rule.template in {"responded_existence", "co_existence", "not_co_existence"}:
        before = ctx.model.NewBoolVar(f"time_before_{attempt.index}_{spec.source}_{spec.target}")
        after = ctx.model.NewBoolVar(f"time_after_{attempt.index}_{spec.source}_{spec.target}")
        ctx.model.AddBoolOr([before, after]).OnlyEnforceIf(pair)
        ctx.model.AddImplication(before, pair)
        ctx.model.AddImplication(after, pair)
        ctx.model.Add(delta >= lo).OnlyEnforceIf(before)
        ctx.model.Add(delta <= -lo).OnlyEnforceIf(after)
        ctx.model.Add(delta <= hi - 1).OnlyEnforceIf(before)
        ctx.model.Add(delta >= -(hi - 1)).OnlyEnforceIf(after)
        return
    ctx.model.Add(delta >= lo).OnlyEnforceIf(pair)
    ctx.model.Add(delta <= hi - 1).OnlyEnforceIf(pair)


def _time_bounds(rule: RuleSpec) -> tuple[int, int]:
    """量化已校验规则的 time_s。"""
    from .temporal import quantize_time_s

    if rule.time_s is None:
        raise PlannerInternalError("time witness lacks time_s")
    return quantize_time_s(rule.time_s)


def _add_negative_rule(ctx: _ModelContext, attempt: AttemptSpec, rule: RuleSpec) -> None:
    """施加无 correlation 的负规则禁止对。"""
    capacity = len(ctx.classes[attempt.index])
    for source_pos in range(capacity):
        targets = (range(source_pos + 1, capacity)
                   if rule.template == "not_succession" else range(capacity))
        for target_pos in targets:
            if target_pos == source_pos:
                continue
            spec = _PairSpec(source_pos, source_pos, target_pos)
            _forbid_pair(ctx, attempt, rule, spec)


def _forbid_pair(ctx: _ModelContext, attempt: AttemptSpec, rule: RuleSpec,
                 spec: _PairSpec) -> None:
    """禁止结构与 time_s 同时成立的 occurrence pair。"""
    model = ctx.model
    rows = ctx.classes[attempt.index]
    source = spec.source_label or str(rule.source)
    target = spec.target_label or str(rule.target)
    literals = [rows[spec.source][source], rows[spec.target][target]]
    literals.extend(rows[pos][source].Not() for pos in spec.forbidden_source_positions)
    outside = _time_outside_literals(ctx, attempt, rule, spec)
    model.AddBoolOr([*(literal.Not() for literal in literals), *outside])


def _time_outside_literals(ctx: _ModelContext, attempt: AttemptSpec, rule: RuleSpec,
                           spec: _PairSpec) -> list[Any]:
    """返回 time_s 之外的可满足分支；无 time_s 时为空。"""
    if rule.time_s is None:
        return []
    lo, hi = _time_bounds(rule)
    delta = ctx.timestamps[attempt.index][spec.target] - ctx.timestamps[attempt.index][spec.source]
    if rule.template == "not_co_existence":
        branches = [ctx.model.NewBoolVar(f"outside_far_low_{attempt.index}_{spec.source}_{spec.target}"),
                    ctx.model.NewBoolVar(f"outside_middle_{attempt.index}_{spec.source}_{spec.target}"),
                    ctx.model.NewBoolVar(f"outside_far_high_{attempt.index}_{spec.source}_{spec.target}")]
        ctx.model.Add(delta <= -hi).OnlyEnforceIf(branches[0])
        ctx.model.Add(delta >= -lo + 1).OnlyEnforceIf(branches[1])
        ctx.model.Add(delta <= lo - 1).OnlyEnforceIf(branches[1])
        ctx.model.Add(delta >= hi).OnlyEnforceIf(branches[2])
        return branches
    low = ctx.model.NewBoolVar(f"outside_low_{attempt.index}_{spec.source}_{spec.target}")
    high = ctx.model.NewBoolVar(f"outside_high_{attempt.index}_{spec.source}_{spec.target}")
    ctx.model.Add(delta <= lo - 1).OnlyEnforceIf(low)
    ctx.model.Add(delta >= hi).OnlyEnforceIf(high)
    return [low, high]


def _add_calendar_constraints(ctx: _ModelContext, question: PlannerQuestion,
                              attempt: AttemptSpec) -> None:
    """把帧类窗口的日内×星期并集编码为析取域。"""
    by_class = {item.frame_class: item for item in _windows_for(question, attempt)}
    offset = timezone(timedelta(seconds=question.ts_offset_s))
    days = max(8, (ctx.horizon - question.ts_start_us) // _DAY_US + 9)
    rows = ctx.classes[attempt.index]
    for position, timestamp in enumerate(ctx.timestamps[attempt.index]):
        for name, window in by_class.items():
            branches = []
            for lower, upper in window_day_options(question.ts_start_us, window, days, offset):
                branch = ctx.model.NewBoolVar(f"window_{attempt.index}_{position}_{name}_{lower}")
                branches.append(branch)
                ctx.model.Add(timestamp >= lower).OnlyEnforceIf(branch)
                ctx.model.Add(timestamp < upper).OnlyEnforceIf(branch)
            if not branches:
                ctx.model.Add(rows[position][name] == 0)
                continue
            ctx.model.Add(sum(branches) == rows[position][name])


def _add_default_edges(ctx: _ModelContext, question: PlannerQuestion,
                       attempt: AttemptSpec) -> None:
    """为未被 time_s witness 覆盖的相邻 owner 对施加默认 gap。"""
    guard = replay_guard(question.frame_gap, question.stream_gap_us)
    for position in range(len(ctx.active[attempt.index]) - 1):
        witness = ctx.covered[attempt.index].get((position, position + 1), [])
        delta = ctx.timestamps[attempt.index][position + 1] - ctx.timestamps[attempt.index][position]
        active = [ctx.active[attempt.index][position], ctx.active[attempt.index][position + 1]]
        ctx.model.Add(delta >= 1).OnlyEnforceIf(active)
        ctx.model.Add(delta <= question.stream_gap_us).OnlyEnforceIf(active)
        if not witness:
            ctx.model.Add(delta >= guard.lo_us).OnlyEnforceIf(active)
            ctx.model.Add(delta <= guard.hi_us).OnlyEnforceIf(active)
            continue
        covered = ctx.model.NewBoolVar(f"covered_{attempt.index}_{position}")
        for item in witness:
            ctx.model.AddImplication(item, covered)
        ctx.model.AddBoolOr([covered.Not(), *witness])
        ctx.model.Add(delta >= guard.lo_us).OnlyEnforceIf([covered.Not(), *active])
        ctx.model.Add(delta <= guard.hi_us).OnlyEnforceIf([covered.Not(), *active])


def _proto_entries(ctx: _ModelContext) -> int:
    """读取模型 proto 的变量与约束规模。"""
    proto = ctx.model.Proto()
    return len(proto.variables) + len(proto.constraints)


def _status_name(cp: Any, status: int) -> PlannerStatus:
    """映射 OR-Tools solver status。"""
    table = {
        cp.OPTIMAL: PlannerStatus.OPTIMAL,
        cp.FEASIBLE: PlannerStatus.FEASIBLE,
        cp.INFEASIBLE: PlannerStatus.INFEASIBLE,
        cp.UNKNOWN: PlannerStatus.UNKNOWN,
        cp.MODEL_INVALID: PlannerStatus.MODEL_INVALID,
    }
    return table.get(status, PlannerStatus.UNKNOWN)


def _configure_solver(cp: Any, seed: int) -> Any:
    """冻结 CP-SAT 的单线程与 deterministic budget。"""
    solver = cp.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.max_deterministic_time = DETERMINISTIC_TIME_LIMIT
    solver.parameters.random_seed = int(seed) & 0x7FFFFFFF
    return solver


def solve_question(question: PlannerQuestion) -> PlannerResult:
    """构造并求解完整联合问题，不改变 question 或外部 RNG。

    @param question M1、estimate 或 M6 构造的不可变联合规划问题
    @return CP-SAT 状态、模型规模和可选冻结 layout
    """
    _validate_question(question)
    ctx = _new_context(question)
    size = _proto_entries(ctx)
    if size > MAX_PROTO_ENTRIES:
        _log.error("sequence planner model exceeds the 250000 proto entry limit")
        raise PlannerConfigError("sequence planner model exceeds 250000 proto entries")
    cp = ctx.cp
    solver = _configure_solver(cp, question.solver_seed)
    status = _status_name(cp, solver.Solve(ctx.model))
    if status == PlannerStatus.MODEL_INVALID:
        _log.error("CP-SAT returned MODEL_INVALID for a validated sequence planner model")
        raise PlannerInternalError("CP-SAT returned MODEL_INVALID")
    if status == PlannerStatus.UNKNOWN:
        _log.error("CP-SAT deterministic budget ended in UNKNOWN")
        return PlannerResult(status, None, size)
    if status == PlannerStatus.INFEASIBLE:
        return PlannerResult(status, None, size)
    objective = int(round(solver.ObjectiveValue())) if ctx.noise_presence and status in {
        PlannerStatus.OPTIMAL, PlannerStatus.FEASIBLE
    } else 0
    if question.noise_target and status != PlannerStatus.OPTIMAL:
        _log.error("noise objective did not reach OPTIMAL status")
        return PlannerResult(status, None, size, objective)
    layout = _decode_layout(ctx, question, solver, status, objective)
    return PlannerResult(status, layout, size, objective)


def _decode_layout(ctx: _ModelContext, question: PlannerQuestion, solver: Any,
                   status: PlannerStatus, objective: int) -> PlannerLayout:
    """解码 word、timestamp、session 与 noise。"""
    words = tuple(tuple(question.frame_classes[solver.Value(var)] for pos, var in enumerate(row)
                         if solver.Value(ctx.active[index][pos]))
                  for index, row in enumerate(ctx.words))
    stamps = tuple(tuple(solver.Value(var) for pos, var in enumerate(row)
                         if solver.Value(ctx.active[index][pos]))
                   for index, row in enumerate(ctx.timestamps))
    owners = _decode_owners(ctx, question, solver)
    noise = _decode_noise(ctx, question, solver)
    sessions = _decode_sessions(_DecodeSpec(ctx, question, solver, words, stamps, owners, noise))
    return PlannerLayout(words, stamps, sessions, noise, len(noise), status, objective)


def _decode_owners(ctx: _ModelContext, question: PlannerQuestion, solver: Any,
                   ) -> tuple[int, ...]:
    """解码每条 attempt 的 session。"""
    return tuple(next(session for session, flag in enumerate(row) if solver.Value(flag))
                 for row in ctx.assignments)


def _decode_sessions(spec: _DecodeSpec) -> tuple[SessionLayout, ...]:
    """按 session 时间顺序组装任务帧。"""
    result: list[SessionLayout] = []
    for session in range(spec.question.sessions):
        indexes = tuple(index for index, owner in enumerate(spec.owners) if owner == session)
        tasks = tuple(replace(frame, session_index=session)
                      for index in indexes for frame in _task_frames(index, spec.words, spec.stamps))
        extras = tuple(item for item in spec.noise if item.session_index == session)
        frames = tasks + extras
        ordered = tuple(sorted(frames, key=lambda item: item.timestamp_us))
        result.append(SessionLayout(session, indexes, ordered,
                                    min(item.timestamp_us for item in ordered),
                                    max(item.timestamp_us for item in ordered)))
    return tuple(result)


def _task_frames(index: int, words: tuple[tuple[str, ...], ...],
                 stamps: tuple[tuple[int, ...], ...]) -> tuple[TimelineFrame, ...]:
    """构造一条 attempt 的任务帧。"""
    return tuple(TimelineFrame(index, position, words[index][position], stamps[index][position], False)
                 for position in range(len(words[index])))


def _decode_noise(ctx: _ModelContext, question: PlannerQuestion,
                  solver: Any) -> tuple[TimelineFrame, ...]:
    """只保留 objective 选中的噪音槽。"""
    result: list[TimelineFrame] = []
    for index, present in enumerate(ctx.noise_presence):
        if not solver.Value(present):
            continue
        session = next(session for session, flag in enumerate(ctx.noise_assignments[index])
                       if solver.Value(flag))
        result.append(TimelineFrame(None, None, None, solver.Value(ctx.noise_timestamps[index]), True, session))
    return tuple(sorted(result, key=lambda item: item.timestamp_us))


def plan_question(question: PlannerQuestion) -> PlannerLayout:
    """要求问题得到可用 layout，严格映射状态语义。

    @param question 待求解的不可变联合规划问题
    @return 已解码且可用于生成的冻结 skeleton layout
    """
    result = solve_question(question)
    if result.status == PlannerStatus.INFEASIBLE:
        raise PlannerConfigError("sequence planner model is infeasible")
    if result.status == PlannerStatus.UNKNOWN:
        raise PlannerConfigError("sequence planner could not be verified within deterministic budget")
    if result.layout is None:
        raise PlannerInternalError("CP-SAT produced no layout for an accepted status")
    return result.layout


def check_question(question: PlannerQuestion) -> PlannerStatus:
    """只运行联合可行性检查，供 M1 每个固定长度的局部矩阵调用。

    @param question 待检查的不可变联合规划问题
    @return CP-SAT 稳定状态枚举
    """
    return solve_question(question).status


def select_feasible_plan(question: PlannerQuestion,
                         rng: random.Random) -> tuple[PlannerQuestion, PlannerLayout]:
    """一次联合求解选择可行长度向量并冻结完整布局。

    @param question 含长度区间的不可变联合规划问题
    @param rng 用于每个 attempt 长度偏好的一次性随机源
    @return 选定长度后的问题与对应冻结 skeleton layout
    """
    _validate_question(question)
    sampling = _free_length_question(question)
    ctx = _new_context(sampling)
    ctx.model.ClearObjective()
    costs = _add_length_preferences(ctx, sampling, rng)
    noise = sum(ctx.noise_presence)
    ctx.model.Minimize(sum(costs) * (len(ctx.noise_presence) + 1) - noise)
    size = _proto_entries(ctx)
    if size > MAX_PROTO_ENTRIES:
        _log.error("length selection model exceeds the 250000 proto entry limit")
        raise PlannerConfigError("length selection model exceeds 250000 proto entries")
    solver = _configure_solver(ctx.cp, sampling.solver_seed)
    status = _status_name(ctx.cp, solver.Solve(ctx.model))
    if status == PlannerStatus.MODEL_INVALID:
        _log.error("CP-SAT returned MODEL_INVALID for the length selection model")
        raise PlannerInternalError("CP-SAT returned MODEL_INVALID")
    if status == PlannerStatus.UNKNOWN:
        _log.error("length selection ended in UNKNOWN")
        raise PlannerConfigError("length selection could not be verified")
    if status == PlannerStatus.INFEASIBLE:
        _log.error("no complete feasible length vector exists")
        raise PlannerConfigError("no complete feasible length vector exists")
    if ctx.noise_presence and status != PlannerStatus.OPTIMAL:
        _log.error("joint length and noise objective did not reach OPTIMAL status")
        raise PlannerConfigError("joint length and noise objective could not be verified")
    lengths = [sum(solver.Value(item) for item in row) for row in ctx.active]
    selected = question.with_lengths(lengths)
    noise_count = sum(solver.Value(item) for item in ctx.noise_presence)
    layout = _decode_layout(ctx, sampling, solver, status, noise_count)
    return selected, layout


def _free_length_question(question: PlannerQuestion) -> PlannerQuestion:
    """把每个 attempt 的声明长度区间装入 active 前缀域。"""
    domains = tuple(tuple(map(int, item.length_range)) if item.length_range is not None
                    else (int(item.length), int(item.length)) for item in question.attempts)
    attempts = tuple(replace(item, length=domain[1])
                     for item, domain in zip(question.attempts, domains))
    target = _max_noise_target(question.noise_ratio, attempts)
    if not question.noise_ratio:
        target = question.noise_target
    return replace(question, attempts=attempts, length_domains=domains,
                   noise_target=target)


def _add_length_preferences(ctx: _ModelContext, question: PlannerQuestion,
                            rng: random.Random) -> list[Any]:
    """为每个长度域增加一次随机旋转得到的整数偏好成本。"""
    costs: list[Any] = []
    for index, domain in enumerate(question.length_domains):
        if domain is None:
            raise PlannerInternalError("length selection requires a domain per attempt")
        lo, hi = domain
        width = hi - lo + 1
        offset = rng.randrange(width)
        length = ctx.model.NewIntVar(lo, hi, f"selected_length_{index}")
        cost = ctx.model.NewIntVar(0, width - 1, f"length_cost_{index}")
        ctx.model.Add(length == sum(ctx.active[index]))
        ctx.model.AddAllowedAssignments(
            [length, cost], [(value, (value - lo - offset) % width)
                             for value in range(lo, hi + 1)])
        preferred = lo + offset
        for position, active in enumerate(ctx.active[index]):
            ctx.model.AddHint(active, int(position < preferred))
        costs.append(cost)
    return costs


def owner_alternates(frames: Sequence[TimelineFrame], owners: Sequence[int]) -> bool:
    """判断双 owner 是否存在真实三点 A-B-A 或 B-A-B。

    @param frames 按 timestamp 可排序的 session 帧
    @param owners 需要检查的两个 owner 索引
    @return 存在真实 owner 交替三点模式时为 ``True``
    """
    if len(set(owners)) != 2:
        return False
    sequence = [frame.owner for frame in sorted(frames, key=lambda item: item.timestamp_us)
                if frame.owner is not None]
    left, right = tuple(owners)
    return any(sequence[i] == left and sequence[j] == right and sequence[k] == left
               for i, j, k in itertools.combinations(range(len(sequence)), 3)) or any(
                   sequence[i] == right and sequence[j] == left and sequence[k] == right
                   for i, j, k in itertools.combinations(range(len(sequence)), 3))


def project_survivors(layout: PlannerLayout, survivors: Sequence[bool] | set[int],
                      ) -> ProjectedLayout:
    """删除作废 attempt 并重编号 session，不移动任一 survivor timestamp。

    @param layout LLM 调用前冻结的完整 skeleton layout
    @param survivors 存活标记序列或存活 attempt 索引集合
    @return 删除作废 owner、空 session 和边界外 noise 后的投影 layout
    """
    alive = (survivors if isinstance(survivors, set)
             else {index for index, value in enumerate(survivors) if value})
    sessions: list[SessionLayout] = []
    for session in layout.sessions:
        frames = tuple(frame for frame in session.frames
                       if frame.owner is None or frame.owner in alive)
        task_frames = tuple(frame for frame in frames if frame.owner is not None)
        if not task_frames:
            continue
        task_owners = tuple(sorted({frame.owner for frame in task_frames if frame.owner is not None}))
        lo, hi = min(frame.timestamp_us for frame in task_frames), max(frame.timestamp_us for frame in task_frames)
        noise = tuple(frame for frame in frames if frame.noise and lo < frame.timestamp_us < hi)
        new_index = len(sessions)
        kept = tuple(replace(item, session_index=new_index)
                     for item in sorted(task_frames + noise, key=lambda item: item.timestamp_us))
        sessions.append(SessionLayout(new_index, task_owners, kept, lo, hi))
    crossed = sum(1 for session in sessions if len(session.owners) == 2
                  and owner_alternates(session.frames, session.owners))
    noise = tuple(frame for session in sessions for frame in session.frames if frame.noise)
    return ProjectedLayout(tuple(sessions), noise, crossed)


def _new_session_vars(ctx: _ModelContext, question: PlannerQuestion) -> None:
    """创建 session owner 分配与 session 边界变量。"""
    model = ctx.model
    for attempt in question.attempts:
        row = [model.NewBoolVar(f"owner_{attempt.index}_{session}")
               for session in range(question.sessions)]
        model.Add(sum(row) == 1)
        ctx.assignments.append(row)
    # session 序数由下方跨 session 的严格时间顺序定义，不能再按 attempt 序号固定；
    # 否则较小 attempt 的晚日历窗口会迫使较大 attempt 错误地延到下一周期。
    _hint_session_assignments(ctx, question)
    doubles = [model.NewBoolVar(f"double_{session}") for session in range(question.sessions)]
    for session, double in enumerate(doubles):
        count = sum(row[session] for row in ctx.assignments)
        model.Add(count == 1 + double)
    model.Add(sum(doubles) == len(question.attempts) - question.sessions)
    span = _session_span(question)
    members = _session_members(ctx, question)
    _add_session_boundaries(ctx, question, members)
    for session in range(question.sessions):
        current = members[session]
        for left, right in itertools.combinations(current, 2):
            literals = [left[0], left[1], right[0], right[1]]
            model.Add(left[2] - right[2] <= span).OnlyEnforceIf(literals)
            model.Add(right[2] - left[2] <= span).OnlyEnforceIf(literals)
        if session:
            previous = members[session - 1]
            for left in current:
                for right in previous:
                    literals = [left[0], left[1], right[0], right[1]]
                    model.Add(left[2] >= right[2] + question.stream_gap_us + 1).OnlyEnforceIf(literals)
    _add_crossing_constraints(ctx, question)


def _add_session_boundaries(ctx: _ModelContext, question: PlannerQuestion,
                            members: Sequence[Sequence[tuple[Any, Any, Any]]]) -> None:
    """建立 session 首尾和相邻 session 的七日递推上界。"""
    model = ctx.model
    for session, current in enumerate(members):
        start = model.NewIntVar(question.ts_start_us, ctx.horizon,
                                f"session_start_{session}")
        end = model.NewIntVar(question.ts_start_us, ctx.horizon,
                              f"session_end_{session}")
        starts, ends = [], []
        for index, (owner, active, timestamp) in enumerate(current):
            member = _member_flag(model, owner, active,
                                  f"session_member_{session}_{index}")
            start_flag = model.NewBoolVar(f"session_start_member_{session}_{index}")
            end_flag = model.NewBoolVar(f"session_end_member_{session}_{index}")
            model.AddImplication(start_flag, member)
            model.AddImplication(end_flag, member)
            model.Add(start == timestamp).OnlyEnforceIf(start_flag)
            model.Add(end == timestamp).OnlyEnforceIf(end_flag)
            model.Add(timestamp >= start).OnlyEnforceIf(member)
            model.Add(timestamp <= end).OnlyEnforceIf(member)
            starts.append(start_flag)
            ends.append(end_flag)
        model.Add(sum(starts) == 1)
        model.Add(sum(ends) == 1)
        lower = (question.ts_start_us if session == 0
                 else ctx.session_ends[session - 1] + question.stream_gap_us + 1)
        model.Add(start >= lower)
        model.Add(start <= lower + _WEEK_US - 1)
        ctx.session_starts.append(start)
        ctx.session_ends.append(end)


def _hint_session_assignments(ctx: _ModelContext, question: PlannerQuestion) -> None:
    """用相邻 attempt 装箱作为软提示，不限制日历要求的反向时间顺序。"""
    paired = len(question.attempts) - question.sessions
    for index, row in enumerate(ctx.assignments):
        preferred = index // 2 if index < 2 * paired else index - paired
        for session, variable in enumerate(row):
            ctx.model.AddHint(variable, int(session == preferred))


def _session_members(ctx: _ModelContext, question: PlannerQuestion,
                     ) -> list[list[tuple[Any, Any, Any]]]:
    """返回每个 session 的 owner、active、timestamp 三元组。"""
    result: list[list[tuple[Any, Any, Any]]] = [[] for _ in range(question.sessions)]
    for attempt in question.attempts:
        for session, owner in enumerate(ctx.assignments[attempt.index]):
            result[session].extend(
                (owner, active, ctx.timestamps[attempt.index][position])
                for position, active in enumerate(ctx.active[attempt.index]))
    return result


def _member_flag(model: Any, owner: Any, active: Any, name: str) -> Any:
    """创建 owner 与 active 的等价合取标记。"""
    member = model.NewBoolVar(name)
    model.AddImplication(member, owner)
    model.AddImplication(member, active)
    model.AddBoolOr([member, owner.Not(), active.Not()])
    return member


def _add_crossing_constraints(ctx: _ModelContext, question: PlannerQuestion) -> None:
    """要求每个双 owner session 存在真实三点 owner 交替。"""
    cp, model = ctx.cp, ctx.model
    for session in range(question.sessions):
        for left, right in itertools.combinations(range(len(question.attempts)), 2):
            pair = model.NewBoolVar(f"pair_{session}_{left}_{right}")
            model.AddImplication(pair, ctx.assignments[left][session])
            model.AddImplication(pair, ctx.assignments[right][session])
            model.AddBoolOr([pair, ctx.assignments[left][session].Not(),
                             ctx.assignments[right][session].Not()])
            patterns = _alternation_patterns(ctx, left, right, pair, session)
            model.AddBoolOr([pair.Not(), *patterns])


def _alternation_patterns(ctx: _ModelContext, left: int, right: int,
                          pair: Any, session: int) -> list[Any]:
    """创建 A-B-A 与 B-A-B 的可行三点布尔模式。"""
    model = ctx.model
    patterns: list[Any] = []
    for k in range(1, len(ctx.timestamps[left])):
        for j in range(len(ctx.timestamps[right])):
            patterns.append(_pattern(ctx, _PatternSpec(pair, session, left, 0, right, j, left, k)))
    for k in range(1, len(ctx.timestamps[right])):
        for j in range(len(ctx.timestamps[left])):
            patterns.append(_pattern(ctx, _PatternSpec(pair, session, right, 0, left, j, right, k)))
    return patterns


def _pattern(ctx: _ModelContext, spec: _PatternSpec) -> Any:
    """创建一个 owner alternation 模式。"""
    model = ctx.model
    suffix = (spec.session, spec.first, spec.first_pos, spec.middle,
              spec.middle_pos, spec.last, spec.last_pos)
    value = model.NewBoolVar("alternate_" + "_".join(map(str, suffix)))
    model.AddImplication(value, spec.pair)
    model.AddImplication(value, ctx.active[spec.first][spec.first_pos])
    model.AddImplication(value, ctx.active[spec.middle][spec.middle_pos])
    model.AddImplication(value, ctx.active[spec.last][spec.last_pos])
    middle_time = ctx.timestamps[spec.middle][spec.middle_pos]
    first_time = ctx.timestamps[spec.first][spec.first_pos]
    last_time = ctx.timestamps[spec.last][spec.last_pos]
    model.Add(middle_time >= first_time + 1).OnlyEnforceIf(value)
    model.Add(last_time >= middle_time + 1).OnlyEnforceIf(value)
    return value


def _new_noise_vars(ctx: _ModelContext, question: PlannerQuestion) -> None:
    """创建噪音 presence、session 归属与时间槽。"""
    model = ctx.model
    for index in range(question.noise_target):
        presence = model.NewBoolVar(f"noise_present_{index}")
        assigns = [model.NewBoolVar(f"noise_owner_{index}_{session}")
                   for session in range(question.sessions)]
        model.Add(sum(assigns) == presence)
        stamp = model.NewIntVar(question.ts_start_us, ctx.horizon, f"noise_ts_{index}")
        ctx.noise_presence.append(presence)
        ctx.noise_assignments.append(assigns)
        ctx.noise_timestamps.append(stamp)
        _add_noise_interior(ctx, question, index, stamp, assigns)
    for session in range(question.sessions):
        counts = []
        for attempt in question.attempts:
            owner = ctx.assignments[attempt.index][session]
            count = model.NewIntVar(0, len(ctx.active[attempt.index]),
                                    f"task_count_{attempt.index}_{session}")
            model.Add(count == sum(ctx.active[attempt.index])).OnlyEnforceIf(owner)
            model.Add(count == 0).OnlyEnforceIf(owner.Not())
            counts.append(count)
        task_count = sum(counts)
        noise_count = sum(assign[session] for assign in ctx.noise_assignments)
        model.Add(task_count + noise_count <= question.session_max_len)
    for noise_a, noise_b in itertools.combinations(range(question.noise_target), 2):
        model.Add(ctx.noise_timestamps[noise_a] != ctx.noise_timestamps[noise_b]).OnlyEnforceIf(
            [ctx.noise_presence[noise_a], ctx.noise_presence[noise_b]])
    for noise_a, noise_b in zip(range(question.noise_target - 1),
                                range(1, question.noise_target)):
        model.Add(ctx.noise_presence[noise_a] >= ctx.noise_presence[noise_b])
        model.Add(ctx.noise_timestamps[noise_a] < ctx.noise_timestamps[noise_b]).OnlyEnforceIf(
            [ctx.noise_presence[noise_a], ctx.noise_presence[noise_b]])
    for noise_index, stamp in enumerate(ctx.noise_timestamps):
        for attempt in question.attempts:
            for position, primary in enumerate(ctx.timestamps[attempt.index]):
                active = ctx.active[attempt.index][position]
                model.Add(stamp != primary).OnlyEnforceIf(
                    [ctx.noise_presence[noise_index], active])
    _add_dynamic_noise_cap(ctx, question)
    if ctx.noise_presence:
        model.Maximize(sum(ctx.noise_presence))


def _add_dynamic_noise_cap(ctx: _ModelContext, question: PlannerQuestion) -> None:
    """可变长度模型中让 noise 上限随选中任务帧总数按 half-even 变化。"""
    if not ctx.noise_presence or not question.length_domains or not question.noise_ratio:
        return
    lower = sum(domain[0] for domain in question.length_domains if domain is not None)
    upper = sum(domain[1] for domain in question.length_domains if domain is not None)
    total = ctx.model.NewIntVar(lower, upper, "selected_frame_count")
    cap = ctx.model.NewIntVar(0, len(ctx.noise_presence), "selected_noise_cap")
    ctx.model.Add(total == sum(item for row in ctx.active for item in row))
    pairs = [(value, int((question.noise_ratio * value).to_integral_value(
        rounding=ROUND_HALF_EVEN))) for value in range(lower, upper + 1)]
    ctx.model.AddAllowedAssignments([total, cap], pairs)
    ctx.model.Add(sum(ctx.noise_presence) <= cap)


def _add_noise_interior(ctx: _ModelContext, question: PlannerQuestion, index: int,
                        stamp: Any, assigns: Sequence[Any]) -> None:
    """要求在场 noise 严格位于实际 session 首尾任务帧之间。"""
    model = ctx.model
    for session, assignment in enumerate(assigns):
        lower_flags, upper_flags = [], []
        for attempt in question.attempts:
            owner = ctx.assignments[attempt.index][session]
            for position, active in enumerate(ctx.active[attempt.index]):
                timestamp = ctx.timestamps[attempt.index][position]
                lower = model.NewBoolVar(f"noise_lower_{index}_{session}_{attempt.index}_{position}")
                upper = model.NewBoolVar(f"noise_upper_{index}_{session}_{attempt.index}_{position}")
                for flag in (lower, upper):
                    model.AddImplication(flag, assignment)
                    model.AddImplication(flag, owner)
                    model.AddImplication(flag, active)
                model.Add(stamp > timestamp).OnlyEnforceIf(lower)
                model.Add(stamp < timestamp).OnlyEnforceIf(upper)
                lower_flags.append(lower)
                upper_flags.append(upper)
        model.Add(sum(lower_flags) >= assignment)
        model.Add(sum(upper_flags) >= assignment)

"""联合 planner 的规则、session、noise、长度条件与投影测试。"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from itertools import product
import random
from types import SimpleNamespace

import pytest

from labelkit.common.config.model import TierSpec
from labelkit.common.errors import InternalError
from labelkit.common.runtime.declare import RuleSpec, evaluate_rule
from labelkit.common.runtime.sequence_planner import (
    AttemptSpec,
    PlannerLayout,
    PlannerInternalError,
    PlannerQuestion,
    SessionLayout,
    PlannerStatus,
    TimelineFrame,
    _horizon,
    check_local_candidates,
    owner_alternates,
    plan_question,
    project_survivors,
    question_from_config,
    select_feasible_plan,
    solve_question,
    _new_context,
    _status_name,
    _configure_solver,
)
from labelkit.common.runtime.temporal import TimeInterval
from labelkit.common.runtime.temporal import CalendarWindow, DayWindow, in_calendar_window, timestamp_us


def _question(
    attempts: tuple[AttemptSpec, ...],
    *,
    sessions: int = 1,
    max_len: int = 10,
    noise_target: int = 0,
    stream_gap_us: int = 3_000_000,
) -> PlannerQuestion:
    """构造小型 deterministic planner 问题。"""
    return PlannerQuestion(
        ("A", "B", "C"), attempts, sessions, stream_gap_us,
        TimeInterval(1_000_000, 3_000_000, closed=True), 0, max_len,
        noise_target=noise_target, solver_seed=17,
    )


def test_joint_layout_has_exact_owner_counts_unique_times_and_noise_interior():
    """联合模型冻结一/二 owner session、真实交替与 noise 开区间。"""
    question = _question((AttemptSpec(0, "x", 2), AttemptSpec(1, "y", 1)), max_len=4, noise_target=1)
    layout = plan_question(question)
    assert layout.status is PlannerStatus.OPTIMAL
    assert len(layout.sessions) == 1 and len(layout.sessions[0].owners) == 2
    assert owner_alternates(layout.sessions[0].frames, layout.sessions[0].owners)
    assert len({stamp for row in layout.timestamps_us for stamp in row}) == 3
    noise = layout.noise_slots[0]
    assert layout.sessions[0].start_us < noise.timestamp_us < layout.sessions[0].end_us
    assert noise.session_index == 0


def test_same_seed_replays_identical_joint_layout():
    """相同 question 与 solver seed 必须冻结相同 layout。"""
    question = _question((AttemptSpec(0, "x", 2), AttemptSpec(1, "y", 1)),
                         max_len=4, noise_target=1)
    first = plan_question(question)
    second = plan_question(question)
    assert first == second


def test_cross_class_windows_allow_reverse_attempt_time_order():
    """不同类窗口可使较大 attempt index 先于 attempt zero。"""
    monday = DayWindow(9 * 3_600 * 1_000_000, 10 * 3_600 * 1_000_000)
    tuesday = DayWindow(9 * 3_600 * 1_000_000, 10 * 3_600 * 1_000_000)
    attempt_zero = AttemptSpec(
        0, "tuesday", 1,
        rules=(RuleSpec("exactly", frame_class="A", count=1),),
        windows=(CalendarWindow("A", (tuesday,), frozenset({1})),),
    )
    later_index = AttemptSpec(
        1, "monday", 1,
        rules=(RuleSpec("exactly", frame_class="B", count=1),),
        windows=(CalendarWindow("B", (monday,), frozenset({0})),),
    )
    question = PlannerQuestion(
        ("A", "B", "C"), (attempt_zero, later_index), 2,
        3_600_000_000, TimeInterval(1_000_000, 3_600_000_000, closed=True),
        0, 2,
    )
    layout = plan_question(question)
    assert layout.timestamps_us[1][0] < layout.timestamps_us[0][0]
    assert in_calendar_window(layout.timestamps_us[0][0], attempt_zero.windows[0])
    assert in_calendar_window(layout.timestamps_us[1][0], later_index.windows[0])


def test_noise_objective_returns_maximum_feasible_presence():
    """noise target 超过容量时只冻结可实现的最大 presence。"""
    question = _question((AttemptSpec(0, "x", 2), AttemptSpec(1, "y", 1)),
                         max_len=4, noise_target=3)
    result = solve_question(question)
    assert result.status is PlannerStatus.OPTIMAL
    assert result.layout is not None
    assert result.objective_value == 1
    assert result.layout.planned_noise_slots == 1


def test_crossing_uses_first_and_last_owner_frames_for_variable_capacity():
    """owner 内严格递增时 first/middle/last 模式仍覆盖 crossing。"""
    question = _question((
        AttemptSpec(0, "long", 3), AttemptSpec(1, "middle", 1),
    ), max_len=4)
    layout = plan_question(question)
    owner_sequence = [frame.owner for frame in layout.sessions[0].frames if not frame.noise]
    assert owner_sequence[0] == owner_sequence[-1]
    assert owner_sequence[0] != owner_sequence[1]
    assert owner_alternates(layout.sessions[0].frames, layout.sessions[0].owners)


def test_status_mapping_distinguishes_solver_terminal_states():
    """求解器五种状态映射保持精确且无 fallback。"""
    fake = SimpleNamespace(OPTIMAL=1, FEASIBLE=2, INFEASIBLE=3, UNKNOWN=4, MODEL_INVALID=5)
    assert _status_name(fake, 1) is PlannerStatus.OPTIMAL
    assert _status_name(fake, 2) is PlannerStatus.FEASIBLE
    assert _status_name(fake, 3) is PlannerStatus.INFEASIBLE
    assert _status_name(fake, 4) is PlannerStatus.UNKNOWN
    assert _status_name(fake, 5) is PlannerStatus.MODEL_INVALID
    assert _status_name(fake, 99) is PlannerStatus.UNKNOWN


def test_model_invalid_raises_public_internal_error(monkeypatch):
    """MODEL_INVALID 必须映射到 common InternalError，且异常文本不含用户值。"""
    import labelkit.common.runtime.sequence_planner as planner

    monkeypatch.setattr(planner, "_status_name",
                        lambda *_args: planner.PlannerStatus.MODEL_INVALID)
    with pytest.raises(InternalError) as caught:
        solve_question(_question((AttemptSpec(0, "x", 1),)))
    assert isinstance(caught.value, InternalError)
    assert isinstance(caught.value, PlannerInternalError)
    assert str(caught.value) == "CP-SAT returned MODEL_INVALID"
    assert "payload" not in str(caught.value)


def test_explicit_timed_witness_replaces_default_gap_but_keeps_guard():
    """声明 time_s 的相邻 witness 可超过默认 frame_gap。"""
    rule = RuleSpec(
        "chain_response", source="A", target="B",
        time_s=(Decimal("1200"), Decimal("2400")),
    )
    attempt = AttemptSpec(
        0, "x", 2,
        rules=(RuleSpec("existence", frame_class="A", count=1),
               RuleSpec("existence", frame_class="B", count=1), rule),
    )
    layout = plan_question(_question((attempt,), stream_gap_us=3_000_000_000))
    assert layout.words == (("A", "B"),)
    delta = layout.timestamps_us[0][1] - layout.timestamps_us[0][0]
    assert 1_200_000_000 <= delta < 2_400_000_000


def test_multiple_time_rules_constrain_one_witness_to_interval_intersection():
    """同一 occurrence witness 的多条 time_s 必须同时落在交集内。"""
    attempt = AttemptSpec(
        0, "intersection", 2, allowed_classes=("A", "B"),
        rules=(
            RuleSpec("init", frame_class="A"),
            RuleSpec("end", frame_class="B"),
            RuleSpec("chain_response", source="A", target="B",
                     time_s=(Decimal("10"), Decimal("20"))),
            RuleSpec("chain_response", source="A", target="B",
                     time_s=(Decimal("15"), Decimal("25"))),
        ),
    )
    layout = plan_question(_question((attempt,), stream_gap_us=30_000_000))
    delta = layout.timestamps_us[0][1] - layout.timestamps_us[0][0]
    assert 15_000_000 <= delta < 20_000_000


def test_non_adjacent_time_rule_measures_total_wall_clock_difference():
    """非相邻 witness 的 time_s 约束总墙钟差，沿途边仍遵守默认 gap。"""
    attempt = AttemptSpec(
        0, "non_adjacent", 3, allowed_classes=("A", "B", "C"),
        rules=(
            RuleSpec("init", frame_class="A"),
            RuleSpec("end", frame_class="C"),
            RuleSpec("response", source="A", target="C",
                     time_s=(Decimal("5"), Decimal("6"))),
        ),
    )
    layout = plan_question(_question((attempt,), stream_gap_us=10_000_000))
    assert layout.words == (("A", "B", "C"),)
    first, middle, last = layout.timestamps_us[0]
    assert 5_000_000 <= last - first < 6_000_000
    assert 1_000_000 <= middle - first <= 3_000_000
    assert 1_000_000 <= last - middle <= 3_000_000


def test_one_session_can_cross_local_midnight_with_each_window_occurrence_valid():
    """序列和 session 可跨本地午夜，而每个 occurrence 仍落在同日窗口。"""
    day = 86_400 * 1_000_000
    late = CalendarWindow("A", (DayWindow(23 * 3_600 * 1_000_000, day),),
                          frozenset({0}))
    early = CalendarWindow("B", (DayWindow(0, 3_600 * 1_000_000),), frozenset({1}))
    attempt = AttemptSpec(
        0, "overnight", 2, allowed_classes=("A", "B"),
        rules=(
            RuleSpec("init", frame_class="A"), RuleSpec("end", frame_class="B"),
            RuleSpec("chain_response", source="A", target="B",
                     time_s=(Decimal("1800"), Decimal("5400"))),
        ), windows=(late, early),
    )
    question = PlannerQuestion(
        frame_classes=("A", "B"), attempts=(attempt,), sessions=1,
        stream_gap_us=7_200 * 1_000_000,
        frame_gap=TimeInterval(1_000_000, 7_200 * 1_000_000, closed=True),
        ts_start_us=timestamp_us("2026-01-05T23:30:00+08:00"),
        ts_offset_s=8 * 3_600, session_max_len=2,
    )
    layout = plan_question(question)
    first, last = layout.timestamps_us[0]
    offset = timezone(timedelta(hours=8))
    assert last > first
    first_day = datetime.fromtimestamp(first / 1_000_000, offset).date()
    last_day = datetime.fromtimestamp(last / 1_000_000, offset).date()
    assert first_day != last_day
    assert in_calendar_window(first, late, offset)
    assert in_calendar_window(last, early, offset)
    assert len(layout.sessions) == 1


def test_projection_deletes_empty_sessions_renumbers_by_time_and_recomputes_crossing():
    """投影只删除内容，保留 timestamp/noise 并按 survivor 重算 session 与 crossing。"""
    def task(owner: int, position: int, stamp: int, session: int, name: str) -> TimelineFrame:
        return TimelineFrame(owner, position, name, stamp, False, session)

    def noise(stamp: int, session: int) -> TimelineFrame:
        return TimelineFrame(None, None, None, stamp, True, session)

    first = SessionLayout(0, (0,), (task(0, 0, 100, 0, "gone"),), 100, 100)
    crossed = SessionLayout(
        1, (1, 2),
        (task(1, 0, 1_000, 1, "A"), noise(1_500, 1),
         task(2, 0, 2_000, 1, "B"), task(1, 1, 3_000, 1, "A")),
        1_000, 3_000,
    )
    becomes_single = SessionLayout(
        2, (2, 3),
        (task(2, 0, 10_000, 2, "A"), noise(10_500, 2),
         task(3, 0, 11_000, 2, "B"), task(2, 1, 12_000, 2, "A")),
        10_000, 12_000,
    )
    layout = PlannerLayout(
        words=(("gone",), ("A", "B", "A"), ("A", "B", "A")),
        timestamps_us=((100,), (1_000, 2_000, 3_000), (10_000, 11_000, 12_000)),
        sessions=(first, crossed, becomes_single),
        noise_slots=(noise(1_500, 1), noise(10_500, 2)),
        planned_noise_slots=2, status=PlannerStatus.OPTIMAL,
    )
    projected = project_survivors(layout, {1, 2})
    assert [item.index for item in projected.sessions] == [0, 1]
    assert projected.sessions[0].owners == (1, 2)
    assert projected.sessions[1].owners == (2,)
    assert projected.crossed_sessions == 1
    assert [(item.start_us, item.end_us) for item in projected.sessions] == [
        (1_000, 3_000), (10_000, 12_000)]
    retained = [frame for session in projected.sessions for frame in session.frames]
    assert {(frame.owner, frame.position, frame.noise, frame.timestamp_us)
            for frame in retained} == {
        (None, None, True, 1_500), (None, None, True, 10_500),
        (1, 0, False, 1_000), (1, 1, False, 3_000),
        (2, 0, False, 2_000), (2, 0, False, 10_000), (2, 1, False, 12_000),
    }
    assert [frame.session_index for frame in retained] == [0, 0, 0, 0, 1, 1, 1]


def test_negative_timed_rule_does_not_prevent_out_of_window_structure():
    """负 time/correlation 规则只禁止落窗对，窗外结构仍可规划。"""
    rules = (
        RuleSpec("existence", frame_class="A", count=1),
        RuleSpec("existence", frame_class="B", count=1),
        RuleSpec("init", frame_class="A"),
        RuleSpec("end", frame_class="B"),
        RuleSpec("not_succession", source="A", target="B",
                 time_s=(Decimal("10"), Decimal("20"))),
    )
    attempt = AttemptSpec(0, "x", 2, rules=rules)
    layout = plan_question(_question((attempt,)))
    assert layout.words[0] == ("A", "B")
    assert layout.timestamps_us[0][1] - layout.timestamps_us[0][0] < 10_000_000


def test_infeasible_status_is_preserved_and_proto_is_bounded():
    """INFEASIBLE 不伪装成 budget failure，模型规模有明确上限。"""
    rules = (RuleSpec("init", frame_class="A"), RuleSpec("end", frame_class="B"))
    result = solve_question(_question((AttemptSpec(0, "x", 1, rules=rules),)))
    assert result.status is PlannerStatus.INFEASIBLE
    assert result.layout is None and result.proto_entries < 250_000


def test_horizon_uses_per_owner_span_and_seven_day_recursion():
    """horizon 按每个 session 的 owner span 上界递推。"""
    gap = 3_000_000
    question = _question((AttemptSpec(0, "x", 2), AttemptSpec(1, "y", 3)), sessions=2)
    expected_span = (1 + 2) * gap
    expected = question.sessions * (7 * 86_400 * 1_000_000 + expected_span + gap) - 1
    assert _horizon(question) == expected


def test_session_start_recursion_caps_each_start_to_one_week():
    """每个 session start 都只能在递推下界后的七日窗口内。"""
    monday = timestamp_us("1970-01-05T00:00:00Z")
    window = CalendarWindow("A", (DayWindow(0, 1),), frozenset({0}))
    attempt_rules = (RuleSpec("exactly", frame_class="A", count=1),)
    question = PlannerQuestion(
        ("A",),
        (AttemptSpec(0, "first", 1, rules=attempt_rules, windows=(window,)),
         AttemptSpec(1, "second", 1, rules=attempt_rules, windows=(window,))),
        2, 3_600_000_000, TimeInterval(1_000_000, 3_600_000_000, closed=True),
        monday, 1,
    )
    starts = set()
    for seed in range(100):
        layout = plan_question(replace(question, solver_seed=seed))
        starts.add(tuple((item.start_us - monday) // 86_400_000_000
                         for item in layout.sessions))
    assert starts == {(0, 7)}


def test_joint_length_preference_is_complete_and_deterministic():
    """一次联合偏好求解返回完整可行长度向量，并按 attempt 各消费一次偏移。"""
    attempts = (
        AttemptSpec(0, "x", 1, length_range=(1, 2)),
        AttemptSpec(1, "y", 1, length_range=(1, 3)),
    )
    question = _question(attempts)
    first_rng = random.Random(9)
    first, first_layout = select_feasible_plan(question, first_rng)
    second, second_layout = select_feasible_plan(question, random.Random(9))
    assert first.attempts == second.attempts
    assert first_layout == second_layout
    assert first.length_domains == (None, None)
    assert solve_question(first).layout is not None
    control = random.Random(9)
    control.randrange(2)
    control.randrange(3)
    assert first_rng.getstate() == control.getstate()


def test_projection_removes_voided_owner_and_recomputes_noise_interior():
    """作废投影删除 owner、保留 timestamp，并按真实 session_index 解码 noise。"""
    question = _question((AttemptSpec(0, "x", 2), AttemptSpec(1, "y", 1)), max_len=4, noise_target=1)
    layout = plan_question(question)
    projected = project_survivors(layout, {0})
    assert len(projected.sessions) == 1
    assert projected.sessions[0].owners == (0,)
    assert all(frame.owner in {0, None} for frame in projected.sessions[0].frames)
    assert all(projected.sessions[0].start_us < frame.timestamp_us < projected.sessions[0].end_us
               for frame in projected.noise_slots)


def test_config_adapter_applies_limit_after_full_tier_apportionment():
    """limit 切完整配分后的类名字典序前缀，不复制配额算法。"""
    tier1 = TierSpec(1, 1, ("A",))
    tier2 = TierSpec(2, 2, ("A", "B"))
    view = lambda count: SimpleNamespace(
        generate=SimpleNamespace(sequences=count, len_range=(1, 2)),
        tiers=None, rules=None, windows=None,
    )
    config = SimpleNamespace(
        frame_class_views={"A": object(), "B": object()},
        class_views={"z": view(2), "a": view(2)},
        generate_stream=SimpleNamespace(
            tiers=(tier1, tier2), sessions=2, noise_ratio=0,
            frame_gap_s=(1, 3), ts_start="2026-01-01T00:00:00Z",
        ),
        stream=SimpleNamespace(gap_s=3, session_max_len=6, session_max_span_s=0),
        limit=3,
    )
    question = question_from_config(config)
    assert [item.class_name for item in question.attempts] == ["a", "a", "z"]
    assert [item.tier_rank for item in question.attempts] == [1, 2, 1]


def test_local_candidate_check_includes_zero_quota_class_and_each_length():
    """局部检查覆盖零配额类、生效档位和区间内每个长度。"""
    tier = TierSpec(1, 1, ("A",))
    config = SimpleNamespace(
        frame_class_views={"A": object(), "B": object()},
        class_views={
            "zero": SimpleNamespace(
                generate=SimpleNamespace(sequences=0, len_range=(1, 2)),
                tiers=(tier,), rules=None, windows=None,
            )
        },
        generate_stream=SimpleNamespace(
            tiers=(), frame_gap_s=(1, 3), ts_start="2026-01-01T00:00:00Z",
        ),
        stream=SimpleNamespace(gap_s=3, session_max_len=4, session_max_span_s=0),
    )
    results = check_local_candidates(config)
    assert [(item.class_name, item.tier_rank, item.length) for item in results] == [
        ("zero", 1, 1), ("zero", 1, 2)
    ]
    assert all(item.status in {PlannerStatus.OPTIMAL, PlannerStatus.INFEASIBLE} for item in results)


def test_direct_evaluator_exhaustive_small_alphabet_lengths_one_to_six():
    """对三帧类长度一至六穷举直接 evaluator 的独立有限迹 oracle。"""
    templates = (
        RuleSpec("existence", frame_class="A", count=1),
        RuleSpec("absence", frame_class="A", count=1),
        RuleSpec("exactly", frame_class="A", count=1),
        RuleSpec("init", frame_class="A"),
        RuleSpec("end", frame_class="A"),
        RuleSpec("responded_existence", source="A", target="B"),
        RuleSpec("co_existence", source="A", target="B"),
        RuleSpec("response", source="A", target="B"),
        RuleSpec("precedence", source="A", target="B"),
        RuleSpec("succession", source="A", target="B"),
        RuleSpec("alternate_response", source="A", target="B"),
        RuleSpec("chain_response", source="A", target="B"),
        RuleSpec("chain_precedence", source="A", target="B"),
        RuleSpec("not_co_existence", source="A", target="B"),
        RuleSpec("not_succession", source="A", target="B"),
    )
    for length in range(1, 7):
        for word in product(("A", "B", "C"), repeat=length):
            for rule in templates:
                result = evaluate_rule(rule, word)
                assert result.valid == _reference_valid(rule, word)


def test_cp_sat_agrees_with_reference_for_each_template_and_length():
    """每个模板每个长度一次全解枚举后与独立 oracle 全量对账。"""
    templates = (
        RuleSpec("existence", frame_class="A", count=1),
        RuleSpec("absence", frame_class="A", count=1),
        RuleSpec("exactly", frame_class="A", count=1),
        RuleSpec("init", frame_class="A"), RuleSpec("end", frame_class="A"),
        RuleSpec("responded_existence", source="A", target="B"),
        RuleSpec("co_existence", source="A", target="B"),
        RuleSpec("response", source="A", target="B"),
        RuleSpec("precedence", source="A", target="B"),
        RuleSpec("succession", source="A", target="B"),
        RuleSpec("alternate_response", source="A", target="B"),
        RuleSpec("chain_response", source="A", target="B"),
        RuleSpec("chain_precedence", source="A", target="B"),
        RuleSpec("not_co_existence", source="A", target="B"),
        RuleSpec("not_succession", source="A", target="B"),
    )
    for length in range(1, 7):
        for rule in templates:
            expected = {
                word for word in product(("A", "B", "C"), repeat=length)
                if _reference_valid(rule, word)
            }
            actual = _cp_words_for_length(rule, length, len(expected))
            assert actual == expected


def _reference_valid(rule: RuleSpec, word: tuple[str, ...]) -> bool:
    """用独立 occurrence 枚举实现有限迹 oracle。"""
    if rule.template == "existence":
        return word.count(rule.frame_class) >= int(rule.count or 0)
    if rule.template == "absence":
        return word.count(rule.frame_class) < int(rule.count or 0)
    if rule.template == "exactly":
        return word.count(rule.frame_class) == int(rule.count or 0)
    if rule.template == "init":
        return bool(word) and word[0] == rule.frame_class
    if rule.template == "end":
        return bool(word) and word[-1] == rule.frame_class
    sources = tuple(index for index, value in enumerate(word) if value == rule.source)
    targets = tuple(index for index, value in enumerate(word) if value == rule.target)
    if rule.template == "responded_existence":
        return all(any(index != other for other in targets) for index in sources)
    if rule.template == "co_existence":
        return all(any(index != other for other in targets) for index in sources) and all(
            any(index != other for other in sources) for index in targets)
    if rule.template == "response":
        return all(any(other > index for other in targets) for index in sources)
    if rule.template == "precedence":
        return all(any(other < index for other in sources) for index in targets)
    if rule.template == "succession":
        response = RuleSpec("response", source=rule.source, target=rule.target)
        precedence = RuleSpec("precedence", source=rule.source, target=rule.target)
        return _reference_valid(response, word) and _reference_valid(precedence, word)
    if rule.template == "alternate_response":
        for index in sources:
            next_source = next((other for other in sources if other > index), len(word))
            if not any(index < target < next_source for target in targets):
                return False
        return True
    if rule.template == "chain_response":
        return all(index + 1 < len(word) and word[index + 1] == rule.target for index in sources)
    if rule.template == "chain_precedence":
        return all(index > 0 and word[index - 1] == rule.source for index in targets)
    if rule.template == "not_co_existence":
        return not any(source != target for source in sources for target in targets)
    return not any(source < target for source in sources for target in targets)


def _cp_words_for_length(rule: RuleSpec, length: int,
                         expected_count: int) -> set[tuple[str, ...]]:
    """固定 timestamp/session 后枚举一个模板长度的全部可行 word。"""
    attempt = AttemptSpec(0, "fixed", length, rules=(rule,))
    question = replace(
        _question((attempt,), max_len=max(6, length), stream_gap_us=10),
        frame_gap=TimeInterval(1, 10, closed=True),
    )
    context = _new_context(question)
    context.model.ClearObjective()
    for position, timestamp in enumerate(context.timestamps[0]):
        context.model.Add(timestamp == position * 2)
    for index, variable in enumerate(context.model.Proto().variables):
        if variable.name == "timeline_end":
            end = context.model.GetIntVarFromProtoIndex(index)
            context.model.Add(end == max(0, length - 1) * 2)

    class _Collector(context.cp.CpSolverSolutionCallback):
        """收集 word 解并丢弃 witness 的多余排列。"""

        def __init__(self) -> None:
            super().__init__()
            self.words: set[tuple[str, ...]] = set()

        def on_solution_callback(self) -> None:
            """记录当前固定长度 word。"""
            self.words.add(tuple(
                question.frame_classes[self.Value(var)] for var in context.words[0]
            ))
            if len(self.words) == expected_count:
                self.StopSearch()

    collector = _Collector()
    solver = _configure_solver(context.cp, question.solver_seed)
    status = _status_name(context.cp, solver.SearchForAllSolutions(context.model, collector))
    assert status in {PlannerStatus.OPTIMAL, PlannerStatus.FEASIBLE}
    return collector.words

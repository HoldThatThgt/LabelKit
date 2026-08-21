"""v1.17 scenario 规则纯求值器测试：15+1 frame 模板与 4 sequence 模板。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from labelkit.common.runtime.scenario import (
    CorrelationSpec,
    EvalFrame,
    EvalOccurrence,
    FrameRuleSpec,
    FrameWindowSpec,
    QuotaSpec,
    RuleVerdict,
    ScheduleSpec,
    SequenceRuleSpec,
    evaluate_frame_rule,
    evaluate_sequence_rule,
    is_valid_name,
    name_domain_violations,
    validate_frame_rule,
    validate_sequence_rule,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_OFFSET = timezone(timedelta(hours=8))
_M = 1_000_000


def _at(day: str, hour: int, minute: int = 0) -> int:
    """+08:00 本地时刻的绝对微秒。"""
    moment = datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00+08:00")
    return (moment.astimezone(timezone.utc) - _EPOCH) // timedelta(microseconds=1)


def _schedule() -> ScheduleSpec:
    """覆盖 2026-01-05..2026-01-06 两本地日的 schedule。"""
    return ScheduleSpec(_at("2026-01-05", 0), _at("2026-01-07", 0), 480, ())


def _frames(word: tuple[str, ...], gap_us: int = _M) -> tuple[EvalFrame, ...]:
    """按位置等距 point frame 构造求值输入。"""
    return tuple(EvalFrame(label, index * gap_us) for index, label in enumerate(word))


# ---------------------------------------------------------------- contains ----

def _contains_rule() -> FrameRuleSpec:
    """contains 测试规则。"""
    return FrameRuleSpec("app_contains_screen", "contains",
                         source="app_usage", target="screen_evidence")


def test_contains_strict_containment_accepts_interval_and_point_target():
    """严格包含成立：区间 target 与 point target 都通过。"""
    interval = (EvalFrame("app_usage", 10, 50), EvalFrame("screen_evidence", 20, 30))
    assert evaluate_frame_rule(_contains_rule(), interval).valid
    point = (EvalFrame("app_usage", 10, 50), EvalFrame("screen_evidence", 20))
    assert evaluate_frame_rule(_contains_rule(), point).valid


def test_contains_rejects_equal_boundaries():
    """相等边界不通过：start 相等或 end 相等都失败。"""
    same_start = (EvalFrame("app_usage", 10, 50), EvalFrame("screen_evidence", 10, 20))
    same_end = (EvalFrame("app_usage", 10, 50), EvalFrame("screen_evidence", 20, 50))
    assert not evaluate_frame_rule(_contains_rule(), same_start).valid
    assert not evaluate_frame_rule(_contains_rule(), same_end).valid


def test_contains_rejects_reversed_and_crossing():
    """逆序与交叉都不成立。"""
    reversed_pair = (EvalFrame("app_usage", 30, 40), EvalFrame("screen_evidence", 10, 50))
    crossing = (EvalFrame("app_usage", 10, 30), EvalFrame("screen_evidence", 20, 40))
    assert not evaluate_frame_rule(_contains_rule(), reversed_pair).valid
    assert not evaluate_frame_rule(_contains_rule(), crossing).valid


def test_contains_witness_must_be_same_sequence():
    """跨序列不成立：target 所在序列没有 source occurrence 时失败。"""
    target_only = (EvalFrame("screen_evidence", 20, 30),)
    assert not evaluate_frame_rule(_contains_rule(), target_only).valid


# ------------------------------------------------- 15 个 frame 模板冒烟 ----

@pytest.mark.parametrize(
    ("rule", "word"),
    [
        (FrameRuleSpec("r", "existence", frame_class="A", count=2), ("A", "B", "A")),
        (FrameRuleSpec("r", "absence", frame_class="B", count=1), ("A", "A")),
        (FrameRuleSpec("r", "exactly", frame_class="A", count=2), ("A", "B", "A")),
        (FrameRuleSpec("r", "init", frame_class="A"), ("A", "B")),
        (FrameRuleSpec("r", "end", frame_class="B"), ("A", "B")),
        (FrameRuleSpec("r", "responded_existence", source="A", target="B"), ("B", "A")),
        (FrameRuleSpec("r", "co_existence", source="A", target="B"), ("B", "A", "B")),
        (FrameRuleSpec("r", "response", source="A", target="B"), ("A", "B", "A", "B")),
        (FrameRuleSpec("r", "precedence", source="A", target="B"), ("A", "B", "B")),
        (FrameRuleSpec("r", "succession", source="A", target="B"), ("A", "B", "A", "B")),
        (FrameRuleSpec("r", "alternate_response", source="A", target="B"),
         ("A", "B", "A", "B")),
        (FrameRuleSpec("r", "chain_response", source="A", target="B"), ("A", "B")),
        (FrameRuleSpec("r", "chain_precedence", source="A", target="B"), ("A", "B")),
        (FrameRuleSpec("r", "not_co_existence", source="A", target="B"), ("A", "A")),
        (FrameRuleSpec("r", "not_succession", source="A", target="B"), ("B", "A")),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_fifteen_frame_templates_accept_golden_words(rule, word):
    """十五模板黄金通过形态（v1.16 test_declare.py 同形）。"""
    verdict = evaluate_frame_rule(rule, _frames(word))
    assert isinstance(verdict, RuleVerdict)
    assert verdict.valid


def test_frame_template_reject_anchors():
    """拒绝锚：末位未服务、负规则命中、一元计数不足。"""
    unserved = evaluate_frame_rule(
        FrameRuleSpec("r", "response", source="A", target="B"), _frames(("A", "B", "A")))
    assert not unserved.valid and unserved.failure_index == 2
    co_exist = evaluate_frame_rule(
        FrameRuleSpec("r", "not_co_existence", source="A", target="B"), _frames(("A", "B")))
    assert not co_exist.valid
    counted = evaluate_frame_rule(
        FrameRuleSpec("r", "existence", frame_class="A", count=2), _frames(("A", "B")))
    assert not counted.valid


def test_frame_time_window_is_half_open_in_microseconds():
    """time_us 半开 [lo,hi)：delta==hi 拒、hi-1 过。"""
    rule = FrameRuleSpec("r", "response", source="A", target="B",
                         time_us=(_M, 2 * _M))
    at_upper = (EvalFrame("A", 0), EvalFrame("B", 2 * _M))
    assert not evaluate_frame_rule(rule, at_upper).valid
    inside = (EvalFrame("A", 0), EvalFrame("B", 2 * _M - 1))
    assert evaluate_frame_rule(rule, inside).valid


def test_frame_correlation_is_type_sensitive():
    """correlation 先比 JSON 类型：1 与 1.0 不相等。"""
    rule = FrameRuleSpec("r", "response", source="A", target="B",
                         correlation=CorrelationSpec("id", "id"))
    bad = (EvalFrame("A", 0, payload={"id": 1}),
           EvalFrame("B", _M, payload={"id": 1.0}))
    assert not evaluate_frame_rule(rule, bad).valid
    good = (EvalFrame("A", 0, payload={"id": 1}),
            EvalFrame("B", _M, payload={"id": 1}))
    assert evaluate_frame_rule(rule, good).valid


def test_validate_frame_rule_shape_matrix():
    """形状矩阵：未知模板、一元带 source、contains 带 time_us 都拒。"""
    with pytest.raises(ValueError, match="template"):
        validate_frame_rule(FrameRuleSpec("r", "nonsense", frame_class="A"))
    with pytest.raises(ValueError, match="binary modifiers"):
        validate_frame_rule(FrameRuleSpec("r", "init", frame_class="A", source="B"))
    with pytest.raises(ValueError, match="time"):
        validate_frame_rule(FrameRuleSpec("r", "contains", source="A", target="B",
                                          time_us=(0, 1)))
    assert validate_frame_rule(_contains_rule()) == _contains_rule()


# ------------------------------------------------- sequence 四模板求值 ----

def test_sequence_precedence_half_open_gap_witness():
    """precedence：gap 半开 [lo,hi)；lo 命中、hi 拒、跨 bucket 无 witness。"""
    rule = SequenceRuleSpec("nav", "precedence", "navigate_home", "clock_out", "day",
                            gap_us=(60 * _M, 120 * _M))
    day_one = _at("2026-01-05", 10)
    good = (EvalOccurrence("clock_out", day_one + 90 * _M),
            EvalOccurrence("navigate_home", day_one))
    assert evaluate_sequence_rule(rule, good, _schedule()).valid
    at_hi = (EvalOccurrence("clock_out", day_one + 120 * _M),
             EvalOccurrence("navigate_home", day_one))
    assert not evaluate_sequence_rule(rule, at_hi, _schedule()).valid
    at_lo = (EvalOccurrence("clock_out", day_one + 60 * _M),
             EvalOccurrence("navigate_home", day_one))
    assert evaluate_sequence_rule(rule, at_lo, _schedule()).valid


def test_sequence_witness_reuse_and_cross_bucket_isolation():
    """一个 source witness 可服务多个 target；跨日 bucket 不互证。"""
    rule = SequenceRuleSpec("nav", "precedence", "navigate_home", "clock_out", "day",
                            gap_us=(0, 3600 * _M))
    day_one = _at("2026-01-05", 10)
    reused = (EvalOccurrence("clock_out", day_one + 10 * _M),
              EvalOccurrence("clock_out", day_one + 20 * _M),
              EvalOccurrence("navigate_home", day_one))
    assert evaluate_sequence_rule(rule, reused, _schedule()).valid
    isolated = (EvalOccurrence("clock_out", _at("2026-01-06", 10)),
                EvalOccurrence("navigate_home", day_one))
    verdict = evaluate_sequence_rule(rule, isolated, _schedule())
    assert not verdict.valid and verdict.failure_index == 0


def test_sequence_response_and_succession():
    """response：每个 source 有更晚 target；succession 双向都要。"""
    response = SequenceRuleSpec("resp", "response", "navigate_home", "clock_out", "day")
    day_one = _at("2026-01-05", 10)
    ok = (EvalOccurrence("navigate_home", day_one),
          EvalOccurrence("clock_out", day_one + 10 * _M))
    assert evaluate_sequence_rule(response, ok, _schedule()).valid
    missing = (EvalOccurrence("navigate_home", day_one + 10 * _M),
               EvalOccurrence("clock_out", day_one))
    assert not evaluate_sequence_rule(response, missing, _schedule()).valid
    succession = SequenceRuleSpec("succ", "succession", "navigate_home", "clock_out",
                                  "schedule")
    assert evaluate_sequence_rule(succession, ok, _schedule()).valid
    assert not evaluate_sequence_rule(succession, missing, _schedule()).valid


def test_sequence_not_co_existence_per_bucket():
    """not_co_existence：同 bucket 同时出现 source 与 target 才失败。"""
    rule = SequenceRuleSpec("excl", "not_co_existence", "food_delivery", "grocery", "day")
    clash = (EvalOccurrence("food_delivery", _at("2026-01-05", 9)),
             EvalOccurrence("grocery", _at("2026-01-05", 18)))
    assert not evaluate_sequence_rule(rule, clash, _schedule()).valid
    split = (EvalOccurrence("food_delivery", _at("2026-01-05", 9)),
             EvalOccurrence("grocery", _at("2026-01-06", 9)))
    assert evaluate_sequence_rule(rule, split, _schedule()).valid
    with pytest.raises(ValueError, match="gap"):
        validate_sequence_rule(SequenceRuleSpec(
            "bad", "not_co_existence", "a", "b", "day", gap_us=(0, 1)))


def test_sequence_week_period_buckets_by_iso_week():
    """week 周期按 ISO 周分桶：跨周不冲突。"""
    rule = SequenceRuleSpec("weekly", "not_co_existence", "a", "b", "week")
    occurrences = (EvalOccurrence("a", _at("2026-01-05", 9)),
                   EvalOccurrence("b", _at("2026-01-12", 9)))
    assert evaluate_sequence_rule(rule, occurrences, _schedule()).valid


def test_sequence_rule_shape_validation():
    """source==target、未知模板、非法 period、坏 gap 都拒。"""
    with pytest.raises(ValueError, match="differ"):
        validate_sequence_rule(SequenceRuleSpec("bad", "response", "a", "a", "day"))
    with pytest.raises(ValueError, match="template"):
        validate_sequence_rule(SequenceRuleSpec("bad", "contains", "a", "b", "day"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="period"):
        validate_sequence_rule(SequenceRuleSpec("bad", "response", "a", "b", "month"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="gap"):
        validate_sequence_rule(SequenceRuleSpec("bad", "response", "a", "b", "day",
                                                gap_us=(2 * _M, _M)))


# ------------------------------------------------------------- 名称域 ----

def test_name_pattern():
    """[a-z0-9_]+ 全串匹配。"""
    assert is_valid_name("week_day_1")
    assert not is_valid_name("Bad")
    assert not is_valid_name("")
    assert not is_valid_name("has space")
    assert not is_valid_name("中文")


def test_name_domain_violations_flags_invalid_and_duplicate():
    """quota/rule/window 全局唯一域：坏模式与跨表重名都被点名。"""
    quotas = (QuotaSpec("weekday_coverage", "day", (), (("mail", 1),), None, (), None),)
    frame_rules = (FrameRuleSpec("app_contains_screen", "contains",
                                 source="a", target="b"),)
    windows = (FrameWindowSpec("app_contains_screen", "task_request", (), ()),)
    sequence_rules = (SequenceRuleSpec("Bad-Name", "response", "a", "b", "day"),)
    violations = name_domain_violations(quotas, frame_rules, windows, sequence_rules)
    assert any("duplicate" in text and "app_contains_screen" in text
               for text in violations)
    assert any("Bad-Name" in text for text in violations)


def test_name_domain_clean_returns_empty():
    """合法且互不重名时返回空元组。"""
    quotas = (QuotaSpec("weekday_coverage", "day", (), (("mail", 1),), None, (), None),)
    frame_rules = (FrameRuleSpec("app_contains_screen", "contains",
                                 source="a", target="b"),)
    windows = (FrameWindowSpec("work_hours", "task_request", (), ()),)
    sequence_rules = (SequenceRuleSpec("navigate_before_clock_out", "response",
                                       "a", "b", "day"),)
    assert name_domain_violations(quotas, frame_rules, windows, sequence_rules) == ()

"""DECLARE 十五模板、重复 occurrence 与 C0/Ce/Ct 归因测试。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from labelkit.common.errors import InternalError
from labelkit.common.runtime.declare import (
    CorrelationSpec,
    RuleSpec,
    activation_positions,
    candidate_pairs,
    evaluate_payload_rules,
    evaluate_rule,
    render_constraint_text,
)


def _rule(template: str, **values: object) -> RuleSpec:
    """构造测试规则。"""
    return RuleSpec(template, **values)


@pytest.mark.parametrize(
    ("rule", "word"),
    [
        (_rule("existence", frame_class="A", count=2), ("A", "B", "A")),
        (_rule("absence", frame_class="B", count=1), ("A", "A")),
        (_rule("exactly", frame_class="A", count=2), ("A", "B", "A")),
        (_rule("init", frame_class="A"), ("A", "B")),
        (_rule("end", frame_class="B"), ("A", "B")),
        (_rule("responded_existence", source="A", target="B"), ("B", "A")),
        (_rule("co_existence", source="A", target="B"), ("B", "A", "B")),
        (_rule("response", source="A", target="B"), ("A", "B", "A", "B")),
        (_rule("precedence", source="A", target="B"), ("A", "B", "B")),
        (_rule("succession", source="A", target="B"), ("A", "B", "A", "B")),
        (_rule("alternate_response", source="A", target="B"), ("A", "B", "A", "B")),
        (_rule("chain_response", source="A", target="B"), ("A", "B")),
        (_rule("chain_precedence", source="A", target="B"), ("A", "B")),
        (_rule("not_co_existence", source="A", target="B"), ("A", "A")),
        (_rule("not_succession", source="A", target="B"), ("B", "A")),
    ],
)
def test_all_declare_templates_accept_or_vacuously_accept(rule: RuleSpec, word: tuple[str, ...]):
    """十五个模板均由同一个直接 evaluator 覆盖。"""
    assert evaluate_rule(rule, word).valid


def test_repeated_occurrences_reuse_one_target_and_preserve_activation_order():
    """重复 source 的 response 与 succession 义务按 occurrence 处理。"""
    word = ("A", "A", "B")
    response = _rule("response", source="A", target="B")
    succession = _rule("succession", source="A", target="B")
    assert activation_positions(response, word) == (0, 1)
    assert activation_positions(succession, word) == (0, 1, 2)
    assert evaluate_rule(response, word).valid
    assert evaluate_rule(succession, word).valid


def test_repeated_response_reports_the_last_unserved_activation():
    """最后一个 source occurrence 没有 target 时必须失败。"""
    result = evaluate_rule(_rule("response", source="A", target="B"), ("A", "B", "A"))
    assert not result.valid
    assert result.failure is not None and result.failure.activation == 2


def test_co_existence_activation_order_is_grouped_by_direction():
    """双向义务先完成 source 组，再完成 target 组。"""
    rule = _rule("co_existence", source="A", target="B")
    assert activation_positions(rule, ("B", "A", "B", "A")) == (1, 3, 0, 2)


def test_vacuity_and_negative_templates():
    """蕴含模板在无 activation 时真，负规则只拒绝实际 occurrence 对。"""
    assert evaluate_rule(_rule("response", source="A", target="B"), ("B",)).valid
    assert evaluate_rule(_rule("not_co_existence", source="A", target="B"), ("A", "B")).valid is False
    assert evaluate_rule(_rule("not_succession", source="A", target="B"), ("A", "B")).valid is False


def test_payload_correlation_is_type_sensitive_and_temporal_is_half_open():
    """payload equality 先比较 JSON 类型，time_s 右端严格半开。"""
    rule = _rule(
        "response",
        source="A",
        target="B",
        correlation=CorrelationSpec("equal", "id", "id"),
        time_s=(Decimal("1"), Decimal("2")),
    )
    bad_type = evaluate_payload_rules(rule_tuple(rule), ("A", "B"), ({"id": 1}, {"id": 1.0}), (0, 1_000_000))
    assert not bad_type.valid
    assert bad_type.correlation_scrapped == 1
    assert bad_type.temporal_scrapped == 0
    at_upper = evaluate_payload_rules(rule_tuple(rule), ("A", "B"), ({"id": 1}, {"id": 1}), (0, 2_000_000))
    assert not at_upper.valid
    assert at_upper.temporal_scrapped == 1
    assert at_upper.evaluations[-1].matches == ()


def test_negative_correlation_is_checked_at_runtime_without_preban():
    """带 correlation 的负规则只在最终 payload 上归因。"""
    rule = _rule(
        "not_succession",
        source="A",
        target="B",
        correlation=CorrelationSpec("equal", "id", "id"),
    )
    result = evaluate_payload_rules(rule_tuple(rule), ("A", "B"), ({"id": 1}, {"id": 1}), (0, 1_000_000))
    assert not result.valid
    assert result.correlation_scrapped == 1
    unequal = evaluate_payload_rules(
        rule_tuple(rule), ("A", "B"), ({"id": 1}, {"id": 2}), (0, 1_000_000))
    assert unequal.valid


def test_negative_and_positive_correlation_prompts_have_opposite_polarity():
    """负规则把 equality 作为禁配，正规则仍要求 equality。"""
    positive = render_constraint_text((
        _rule("response", source="A", target="B",
              correlation=CorrelationSpec("equal", "id", "id")),), ())
    negative = render_constraint_text((
        _rule("not_succession", source="A", target="B",
              correlation=CorrelationSpec("equal", "id", "id")),), ())
    assert "source.id 与 target.id 的 JSON 类型及值必须相同" in positive
    assert "禁止 source.id 与 target.id 的 JSON 类型及值相同" in negative
    assert "必须相同" not in negative


def test_no_correlation_runtime_failure_is_internal_error():
    """无 correlation 的失败只能说明 planner invariant 被破坏。"""
    rule = _rule("response", source="A", target="B")
    with pytest.raises(InternalError):
        evaluate_payload_rules(rule_tuple(rule), ("A",), ({},), (0,))


def test_no_correlation_positive_time_failure_is_internal_error(caplog):
    """无 correlation 的 Ct 失败必须走 value-free InternalError。"""
    rule = _rule("response", source="A", target="B", time_s=(Decimal("1"), Decimal("2")))
    with caplog.at_level("ERROR", logger="labelkit.sequence_rules"):
        with pytest.raises(InternalError):
            evaluate_payload_rules(rule_tuple(rule), ("A", "B"), ({}, {}), (0, 2_000_000))
    assert "temporal_scrapped" not in caplog.text
    assert "2_000_000" not in caplog.text


def rule_tuple(rule: RuleSpec) -> tuple[RuleSpec, ...]:
    """把单条规则装入公共序列接口。"""
    return (rule,)

"""v1.17 planner 诊断异常层级与 message 模板逐字节测试。"""
from __future__ import annotations

import pytest

from labelkit.common.runtime.scenario import (
    PLANNER_ENTRY_LIMIT,
    PlannerBudgetError,
    PlannerCapacityError,
    PlannerInfeasibleError,
    PlannerInternalError,
    format_budget_message,
    format_capacity_message,
    format_infeasible_message,
)


def test_exception_hierarchy_matches_contracts():
    """四异常层级：Infeasible 是 ValueError，其余三个是 RuntimeError。"""
    assert issubclass(PlannerInfeasibleError, ValueError)
    assert issubclass(PlannerCapacityError, RuntimeError)
    assert issubclass(PlannerBudgetError, RuntimeError)
    assert issubclass(PlannerInternalError, RuntimeError)
    assert not issubclass(PlannerCapacityError, ValueError)
    assert not issubclass(PlannerBudgetError, ValueError)
    assert not issubclass(PlannerInternalError, ValueError)


def test_capacity_message_bytes_match_spec_example():
    """capacity message 与 §8.3 示例逐字节一致。"""
    message = format_capacity_message(
        "timeline", 251_891, {"crossing": 170_000, "session_slot": 60_000})
    assert message == ("sequence planner capacity exceeded: model=timeline "
                       "entries=251891 limit=250000 dominant=crossing "
                       "families={crossing:170000,session_slot:60000}")


def test_capacity_message_dominant_tie_breaks_by_name():
    """dominant 平票按名字升序，families 按计数降序。"""
    message = format_capacity_message("quota", 300, {"b": 40, "a": 40, "c": 10})
    assert message == ("sequence planner capacity exceeded: model=quota entries=300 "
                       "limit=250000 dominant=a families={a:40,b:40,c:10}")


def test_capacity_limit_constant_is_250k():
    """默认 limit 常量 = 250000。"""
    assert PLANNER_ENTRY_LIMIT == 250_000


def test_budget_message_names_model_and_layer():
    """budget message 点名 model=quota|timeline 与超时 layer。"""
    assert format_budget_message("timeline", "crossing") == (
        "sequence planner budget exhausted: model=timeline layer=crossing")
    assert format_budget_message("quota", "quota_row") == (
        "sequence planner budget exhausted: model=quota layer=quota_row")


def test_infeasible_message_joins_named_constraints():
    """infeasible message：具名约束集合逗号连接（§8.4 示例逐字节）。"""
    message = format_infeasible_message(
        ("weekday_coverage", "navigate_before_clock_out", "ticket_request_work_hours"))
    assert message == ("sequence planner infeasible: constraints=[weekday_coverage,"
                       "navigate_before_clock_out,ticket_request_work_hours]")
    assert format_infeasible_message(()) == (
        "sequence planner infeasible: constraints=[]")


def test_exceptions_carry_docstrings_verbatim():
    """四类异常 docstring 为契约中文原文。"""
    assert PlannerInfeasibleError.__doc__ == "用户硬约束没有共同解。"
    assert PlannerCapacityError.__doc__ == "模型在求解前超过实现容量。"
    assert PlannerBudgetError.__doc__ == "deterministic solve budget 内无法冻结最优计划。"
    assert PlannerInternalError.__doc__ == "solver 解码或冻结计划违反实现不变量。"


def test_exceptions_are_raisable_with_message_helpers():
    """异常可携带模板 message 抛出并被捕获。"""
    with pytest.raises(PlannerCapacityError, match="dominant=crossing"):
        raise PlannerCapacityError(format_capacity_message(
            "timeline", 251_891, {"crossing": 170_000}))

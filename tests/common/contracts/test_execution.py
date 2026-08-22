from __future__ import annotations

import importlib
import inspect
from dataclasses import FrozenInstanceError, fields

import pytest


def _execution_module():
    """延迟导入使缺失的新生产模块表现为测试失败，而不是 collection error。"""
    return importlib.import_module("labelkit.common.contracts.execution")


def test_task_spec_and_group_request_freeze_the_exact_contract_fields():
    execution = _execution_module()

    assert [field.name for field in fields(execution.TaskSpec)] == [
        "task_id",
        "declaration_key",
        "stage",
        "resource_key",
        "operation",
    ]
    assert [field.name for field in fields(execution.TaskGroupRequest)] == ["tasks"]
    assert execution.TaskSpec.__dataclass_params__.frozen is True
    assert execution.TaskGroupRequest.__dataclass_params__.frozen is True


def test_task_operation_is_neither_compared_nor_represented():
    execution = _execution_module()

    async def first_operation():
        return "first-secret-operation"

    async def second_operation():
        return "second-secret-operation"

    common = {
        "task_id": "run:batch:quality:0",
        "declaration_key": (0, 0, 0),
        "stage": "quality",
        "resource_key": ("llm", "judge"),
    }
    first = execution.TaskSpec(operation=first_operation, **common)
    second = execution.TaskSpec(operation=second_operation, **common)

    assert first == second
    assert "secret-operation" not in repr(first)
    with pytest.raises(FrozenInstanceError):
        first.stage = "verify"


def test_execution_protocols_expose_only_the_frozen_methods():
    execution = _execution_module()

    assert getattr(execution.TaskExecutor, "_is_protocol", False) is True
    assert list(inspect.signature(execution.TaskExecutor.run_group).parameters) == [
        "self",
        "request",
    ]
    assert inspect.iscoroutinefunction(execution.TaskExecutor.run_group)
    assert getattr(execution.ResourceLimiter, "_is_protocol", False) is True
    assert {
        "resource_limit",
        "origin_for",
        "origin_limit",
        "http_connection_capacity",
    }.issubset(vars(execution.ResourceLimiter))


def test_run_context_has_eight_required_fields_in_contract_order():
    stage = importlib.import_module("labelkit.common.contracts.stage")

    assert [field.name for field in fields(stage.RunContext)] == [
        "cfg",
        "llm",
        "schema_engine",
        "rng",
        "batch_no",
        "metrics",
        "tasks",
        "task_namespace",
    ]
    assert all(
        parameter.default is inspect.Parameter.empty
        for parameter in inspect.signature(stage.RunContext).parameters.values()
    )


def test_run_services_and_generation_services_require_the_same_task_identity():
    process = importlib.import_module("labelkit.orchestration.process_workflow")
    generation = importlib.import_module("labelkit.common.contracts.generation")
    tasks = object()

    run_services = process.RunServices(
        llm=object(),
        schema_engine=object(),
        metrics=object(),
        tasks=tasks,
        run_id="run",
        run_started_at=object(),
    )
    generation_services = generation.GenerationServices(
        config=object(),
        schema_engine=object(),
        llm=object(),
        metrics=object(),
        tasks=tasks,
    )

    assert [field.name for field in fields(process.RunServices)] == [
        "llm",
        "schema_engine",
        "metrics",
        "tasks",
        "run_id",
        "run_started_at",
    ]
    assert [field.name for field in fields(generation.GenerationServices)] == [
        "config",
        "schema_engine",
        "llm",
        "metrics",
        "tasks",
    ]
    assert run_services.tasks is generation_services.tasks is tasks

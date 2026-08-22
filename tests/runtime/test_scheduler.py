from __future__ import annotations

import asyncio
import contextvars
import importlib
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import pytest


class _Metrics:
    def __init__(self) -> None:
        self.high_water: dict[str, int] = {}
        self.totals: dict[str, int] = {}

    def observe_runtime_high_water(self, key: str, value: int) -> None:
        self.high_water[key] = max(self.high_water.get(key, 0), value)

    def add_runtime_total(self, key: str, value: int) -> None:
        self.totals[key] = self.totals.get(key, 0) + value


def _modules():
    execution = importlib.import_module("labelkit.common.contracts.execution")
    scheduler = importlib.import_module("labelkit.runtime.scheduler")
    resources = importlib.import_module("labelkit.runtime.resources")
    errors = importlib.import_module("labelkit.common.errors")
    return execution, scheduler, resources, errors


def _runtime(
    capacities: Mapping[tuple[str, str], int],
) -> tuple[Any, Any, _Metrics]:
    _execution, scheduler, resources, _errors = _modules()
    origins = {
        key: ("https", f"{key[0]}-{key[1]}.example", 443)
        for key in capacities
    }
    metrics = _Metrics()
    manager = resources.ResourceManager(capacities, origins, metrics)
    return scheduler.ExecutionRuntime(manager, metrics), manager, metrics


def _task(
    task_id: str,
    declaration_key: tuple[Any, ...],
    resource_key: tuple[str, str],
    operation: Callable[[], Awaitable[Any]],
):
    execution, _scheduler, _resources, _errors = _modules()
    return execution.TaskSpec(
        task_id=task_id,
        declaration_key=declaration_key,
        stage="test",
        resource_key=resource_key,
        operation=operation,
    )


def _request(*tasks):
    execution, _scheduler, _resources, _errors = _modules()
    return execution.TaskGroupRequest(tasks=tuple(tasks))


async def test_empty_group_returns_without_materializing_an_operation():
    runtime, _resources, _metrics = _runtime({("llm", "a"): 1})

    async def workflow():
        return await runtime.run_group(_request())

    assert await runtime.run(workflow) == ()


async def test_six_hundred_tasks_return_in_input_order_after_reverse_completion():
    key = ("llm", "wide")
    runtime, _resources, metrics = _runtime({key: 600})
    condition = asyncio.Condition()
    all_started = asyncio.Event()
    started = 0
    turn = 599
    completion_order: list[int] = []

    def operation(index: int):
        async def run() -> int:
            nonlocal started, turn
            started += 1
            if started == 600:
                all_started.set()
            await all_started.wait()
            async with condition:
                await condition.wait_for(lambda: turn == index)
                completion_order.append(index)
                turn -= 1
                condition.notify_all()
            return index

        return run

    tasks = tuple(
        _task(f"run:wide:{index}", (0, index), key, operation(index))
        for index in range(600)
    )

    async def workflow():
        return await runtime.run_group(_request(*tasks))

    result = await asyncio.wait_for(runtime.run(workflow), timeout=15)

    assert result == tuple(range(600))
    assert completion_order == list(range(599, -1, -1))
    assert metrics.high_water["running"] == 600


async def test_low_capacity_profile_cannot_head_of_line_block_an_independent_profile():
    slow_key = ("llm", "slow")
    wide_key = ("llm", "wide")
    runtime, _resources, _metrics = _runtime({slow_key: 1, wide_key: 599})
    slow_release = asyncio.Event()
    wide_release = asyncio.Event()
    wide_all_started = asyncio.Event()
    wide_started = 0

    async def slow(index: int):
        await slow_release.wait()
        return ("slow", index)

    def wide(index: int):
        async def run():
            nonlocal wide_started
            wide_started += 1
            if wide_started == 599:
                wide_all_started.set()
            await wide_release.wait()
            return ("wide", index)

        return run

    tasks = [
        _task(f"run:slow:{index}", (0, index), slow_key,
              lambda index=index: slow(index))
        for index in range(600)
    ]
    tasks.extend(
        _task(f"run:wide:{index}", (1, index), wide_key, wide(index))
        for index in range(599)
    )

    async def workflow():
        group = asyncio.create_task(runtime.run_group(_request(*tasks)))
        await asyncio.wait_for(wide_all_started.wait(), timeout=10)
        assert wide_started == 599
        wide_release.set()
        slow_release.set()
        return await group

    result = await asyncio.wait_for(runtime.run(workflow), timeout=15)

    assert result[:600] == tuple(("slow", index) for index in range(600))
    assert result[600:] == tuple(("wide", index) for index in range(599))


async def test_admission_capacity_is_global_across_concurrent_groups():
    key = ("embedding", "shared")
    runtime, _resources, metrics = _runtime({key: 7})
    release = asyncio.Event()
    saturated = asyncio.Event()
    active = 0
    maximum = 0

    def operation(index: int):
        async def run() -> int:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            if active == 7:
                saturated.set()
            try:
                await release.wait()
                return index
            finally:
                active -= 1

        return run

    async def workflow():
        groups = [
            asyncio.create_task(runtime.run_group(_request(
                _task(f"run:group:{index}", (0, index), key, operation(index)),
            )))
            for index in range(70)
        ]
        await asyncio.wait_for(saturated.wait(), timeout=5)
        await asyncio.sleep(0)
        assert active == 7
        release.set()
        return await asyncio.gather(*groups)

    results = await asyncio.wait_for(runtime.run(workflow), timeout=10)

    assert maximum == 7
    assert metrics.high_water["running"] == 7
    assert tuple(result[0] for result in results) == tuple(range(70))


class _LowFatal(RuntimeError):
    pass


class _HighFatal(RuntimeError):
    pass


async def test_cross_group_fatal_selects_smallest_key_after_all_cleanup():
    key = ("llm", "fatal")
    runtime, _resources, metrics = _runtime({key: 4})
    ready = asyncio.Event()
    release = asyncio.Event()
    ready_count = 0
    cleaned: set[str] = set()
    low = _LowFatal("low")
    high = _HighFatal("high")

    def fatal(name: str, error: Exception):
        async def run():
            nonlocal ready_count
            ready_count += 1
            if ready_count == 4:
                ready.set()
            await release.wait()
            raise error

        return run

    def blocker(name: str):
        async def run():
            nonlocal ready_count
            ready_count += 1
            if ready_count == 4:
                ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.add(name)

        return run

    low_group = _request(
        _task("run:fatal:low", (0, 0), key, fatal("low", low)),
        _task("run:block:low", (0, 1), key, blocker("low")),
    )
    high_group = _request(
        _task("run:fatal:high", (1, 0), key, fatal("high", high)),
        _task("run:block:high", (1, 1), key, blocker("high")),
    )

    async def workflow():
        async with asyncio.TaskGroup() as group:
            group.create_task(runtime.run_group(low_group))
            group.create_task(runtime.run_group(high_group))
            await ready.wait()
            release.set()

    with pytest.raises(_LowFatal) as caught:
        await asyncio.wait_for(runtime.run(workflow), timeout=5)

    assert caught.value is low
    assert cleaned == {"low", "high"}
    assert metrics.totals["cancelled_tasks"] >= 2


async def test_leaf_fatal_aborts_domain_even_when_workflow_catches_group_error():
    key = ("llm", "fatal")
    runtime, _resources, metrics = _runtime({key: 2})
    blocker_started = asyncio.Event()
    blocker_cleaned = asyncio.Event()
    error = _LowFatal("caught leaf fatal")

    async def blocker():
        blocker_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            blocker_cleaned.set()

    async def fatal():
        await blocker_started.wait()
        raise error

    async def workflow():
        blocker_group = asyncio.create_task(runtime.run_group(_request(
            _task("run:blocker", (1, 0), key, blocker),
        )))
        try:
            await runtime.run_group(_request(
                _task("run:fatal", (0, 0), key, fatal),
            ))
        except _LowFatal:
            await asyncio.Event().wait()
        return await blocker_group

    with pytest.raises(_LowFatal) as caught:
        await asyncio.wait_for(runtime.run(workflow), timeout=2)

    assert caught.value is error
    assert blocker_cleaned.is_set()
    assert metrics.totals["cancelled_tasks"] >= 1


async def test_circuit_breaker_cleans_group_then_returns_control_to_workflow():
    key = ("llm", "breaker")
    runtime, resources, metrics = _runtime({key: 2})
    _execution, _scheduler, _resource_module, errors = _modules()
    origin = resources.origin_for(key)
    both_started = asyncio.Event()
    cleaned = asyncio.Event()
    started = 0
    breaker = errors.CircuitBreakerTripped("breaker open")

    async def mark_started():
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await both_started.wait()

    async def trip():
        async with resources.resource_limit(key):
            async with resources.origin_limit(origin):
                await mark_started()
                raise breaker

    async def blocker():
        async with resources.resource_limit(key):
            async with resources.origin_limit(origin):
                await mark_started()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()

    async def reacquire_all_permits():
        async with resources.resource_limit(key):
            async with resources.resource_limit(key):
                async with resources.origin_limit(origin):
                    async with resources.origin_limit(origin):
                        return True

    request = _request(
        _task("run:breaker:trip", (0, 0), key, trip),
        _task("run:breaker:blocker", (0, 1), key, blocker),
    )

    async def workflow():
        try:
            await runtime.run_group(request)
        except errors.CircuitBreakerTripped as caught:
            assert caught is breaker
            assert runtime._admission[key]._value == 2
            assert await asyncio.wait_for(reacquire_all_permits(), timeout=1)
            return 4
        raise AssertionError("breaker must reach workflow")

    assert await runtime.run(workflow) == 4
    assert cleaned.is_set()
    assert metrics.totals["cancelled_tasks"] == 1


_marker = contextvars.ContextVar("scheduler_test_marker", default="unset")
_capture = contextvars.ContextVar("scheduler_test_capture", default=None)


async def test_each_leaf_gets_a_context_copy_but_shares_attempt_capture_object(monkeypatch):
    key = ("llm", "context")
    runtime, _resources, _metrics = _runtime({key: 2})
    both_started = asyncio.Event()
    started = 0
    shared: dict[str, int] = {}
    leaf_contexts = []
    original_create_task = asyncio.TaskGroup.create_task

    def create_task(group, coro, *args, **kwargs):
        if getattr(coro, "cr_code", None).co_name == "_run_leaf":
            leaf_contexts.append(kwargs["context"])
        return original_create_task(group, coro, *args, **kwargs)

    monkeypatch.setattr(asyncio.TaskGroup, "create_task", create_task)

    def operation(name: str):
        async def run():
            nonlocal started
            before = _marker.get()
            captured = _capture.get()
            _marker.set(name)
            captured[name] = len(captured)
            started += 1
            if started == 2:
                both_started.set()
            await both_started.wait()
            return before, _marker.get(), id(captured)

        return run

    async def workflow():
        marker_token = _marker.set("parent")
        capture_token = _capture.set(shared)
        try:
            return await runtime.run_group(_request(
                _task("run:context:left", (0, 0), key, operation("left")),
                _task("run:context:right", (0, 1), key, operation("right")),
            ))
        finally:
            _capture.reset(capture_token)
            _marker.reset(marker_token)

    result = await runtime.run(workflow)

    assert result == (
        ("parent", "left", id(shared)),
        ("parent", "right", id(shared)),
    )
    assert shared == {"left": 0, "right": 1}
    assert _marker.get() == "unset"
    assert len(leaf_contexts) == 2 and leaf_contexts[0] is not leaf_contexts[1]


@pytest.mark.parametrize("case", ["duplicate", "unknown", "unorderable"])
async def test_invalid_group_fails_before_any_leaf_starts(case: str):
    key = ("llm", "known")
    runtime, _resources, _metrics = _runtime({key: 2})
    _execution, _scheduler, _resource_module, errors = _modules()
    started: list[str] = []

    async def operation():
        started.append("started")
        return None

    if case == "duplicate":
        tasks = (
            _task("same", (0,), key, operation),
            _task("same", (1,), key, operation),
        )
    elif case == "unknown":
        tasks = (_task("unknown", (0,), ("llm", "missing"), operation),)
    else:
        tasks = (
            _task("integer", (0,), key, operation),
            _task("string", ("not-comparable",), key, operation),
        )

    async def workflow():
        return await runtime.run_group(_request(*tasks))

    with pytest.raises(errors.InternalError):
        await runtime.run(workflow)
    assert started == []


async def test_domain_outside_leaf_nested_and_after_shutdown_fail_closed():
    key = ("llm", "domain")
    runtime, _resources, _metrics = _runtime({key: 2})
    _execution, _scheduler, _resource_module, errors = _modules()

    async def value():
        return 1

    request = _request(_task("run:domain:0", (0,), key, value))

    with pytest.raises(errors.InternalError):
        await runtime.run_group(request)

    async def nested_group():
        return await runtime.run_group(request)

    async def group_workflow():
        return await runtime.run_group(_request(
            _task("run:domain:nested", (1,), key, nested_group),
        ))

    with pytest.raises(errors.InternalError):
        await runtime.run(group_workflow)

    second, _manager, _metrics = _runtime({key: 1})

    async def nested_run_workflow():
        return await second.run(value)

    with pytest.raises(errors.InternalError):
        await second.run(nested_run_workflow)

    third, _manager, _metrics = _runtime({key: 1})
    assert await third.run(value) == 1
    with pytest.raises(errors.InternalError):
        await third.run_group(request)


async def test_external_cancellation_waits_for_cleanup_and_returns_resource_permits():
    key = ("llm", "cancel")
    runtime, resources, metrics = _runtime({key: 5})
    all_started = asyncio.Event()
    started = 0
    cleaned = 0

    async def operation():
        nonlocal started, cleaned
        async with resources.resource_limit(key):
            started += 1
            if started == 5:
                all_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned += 1

    request = _request(*(
        _task(f"run:cancel:{index}", (0, index), key, operation)
        for index in range(5)
    ))

    async def workflow():
        return await runtime.run_group(request)

    root = asyncio.create_task(runtime.run(workflow))
    await asyncio.wait_for(all_started.wait(), timeout=5)
    root.cancel()
    with pytest.raises(asyncio.CancelledError):
        await root

    assert cleaned == 5
    assert metrics.totals["cancelled_tasks"] == 5
    async with asyncio.timeout(1):
        async with resources.resource_limit(key):
            pass


async def test_external_cancellation_before_leaf_first_step_releases_admission(monkeypatch):
    key = ("llm", "cancel-before-start")
    runtime, resources, metrics = _runtime({key: 1})
    original = asyncio.TaskGroup.create_task
    root_holder: dict[str, asyncio.Task] = {}
    intercepted = False
    ran: list[str] = []

    def create_task(group, coro, *args, **kwargs):
        nonlocal intercepted
        task = original(group, coro, *args, **kwargs)
        if not intercepted and getattr(coro, "cr_code", None).co_name == "_run_leaf":
            intercepted = True
            task.cancel()
            root_holder["task"].cancel()
        return task

    monkeypatch.setattr(asyncio.TaskGroup, "create_task", create_task)

    async def operation():
        ran.append("started")

    request = _request(
        _task("run:cancel-before-start:0", (0, 0), key, operation),
        _task("run:cancel-before-start:1", (0, 1), key, operation),
    )
    root = asyncio.create_task(runtime.run(lambda: runtime.run_group(request)))
    root_holder["task"] = root

    with pytest.raises(asyncio.CancelledError):
        await root

    assert intercepted is True and ran == []
    assert runtime._admission[key]._value == 1
    assert metrics.totals["cancelled_tasks"] == 1
    async with asyncio.timeout(1):
        await runtime._admission[key].acquire()
        runtime._admission[key].release()
        async with resources.resource_limit(key):
            pass


async def test_leaf_cancelled_error_is_restored_after_sibling_cleanup():
    key = ("llm", "leaf-cancel")
    runtime, resources, metrics = _runtime({key: 2})
    both_started = asyncio.Event()
    cleaned = asyncio.Event()
    started = 0
    error = asyncio.CancelledError("leaf cancelled")

    async def mark_started():
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await both_started.wait()

    async def cancel_leaf():
        await mark_started()
        raise error

    async def blocker():
        await mark_started()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    request = _request(
        _task("run:cancel:self", (0, 0), key, cancel_leaf),
        _task("run:cancel:blocker", (0, 1), key, blocker),
    )

    with pytest.raises(asyncio.CancelledError) as caught:
        await runtime.run(lambda: runtime.run_group(request))

    assert caught.value is error
    assert cleaned.is_set()
    assert metrics.totals["cancelled_tasks"] == 2
    async with asyncio.timeout(1):
        async with resources.resource_limit(key):
            pass


async def test_leaf_task_self_cancel_is_not_converted_to_internal_error():
    key = ("llm", "self-cancel")
    runtime, resources, metrics = _runtime({key: 2})
    both_started = asyncio.Event()
    cleaned = asyncio.Event()
    started = 0

    async def mark_started():
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await both_started.wait()

    async def self_cancel():
        await mark_started()
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)

    async def blocker():
        await mark_started()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    request = _request(
        _task("run:self-cancel", (0, 0), key, self_cancel),
        _task("run:self-cancel:blocker", (0, 1), key, blocker),
    )

    with pytest.raises(asyncio.CancelledError):
        await runtime.run(lambda: runtime.run_group(request))

    assert cleaned.is_set()
    assert metrics.totals["cancelled_tasks"] == 2
    async with asyncio.timeout(1):
        async with resources.resource_limit(key):
            pass

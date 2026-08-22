from __future__ import annotations

import asyncio
import ast
import importlib
import inspect
import textwrap
from collections.abc import Mapping
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


def _manager(
    capacities: Mapping[tuple[str, str], int],
    origins: Mapping[tuple[str, str], tuple[str, str, int]],
    metrics: _Metrics | None = None,
) -> tuple[Any, _Metrics]:
    module = importlib.import_module("labelkit.runtime.resources")
    sink = metrics or _Metrics()
    return module.ResourceManager(capacities, origins, sink), sink


def test_capacities_origins_and_connection_capacity_are_exact():
    same = ("https", "shared.example", 443)
    other = ("http", "other.example", 80)
    capacities = {
        ("llm", "a"): 2,
        ("embedding", "a"): 3,
        ("llm", "b"): 7,
    }
    origins = {
        ("llm", "a"): same,
        ("embedding", "a"): same,
        ("llm", "b"): other,
    }
    manager, _metrics = _manager(capacities, origins)

    assert manager.admission_capacity(("llm", "a")) == 2
    assert manager.admission_capacity(("embedding", "a")) == 3
    assert manager.origin_for(("llm", "a")) == same
    assert manager.origin_for(("embedding", "a")) == same
    assert manager.origin_for(("llm", "b")) == other
    assert manager.http_connection_capacity == 12


@pytest.mark.parametrize(
    ("capacities", "origins"),
    [
        ({("llm", "a"): 0}, {("llm", "a"): ("https", "a.example", 443)}),
        ({("llm", "a"): -1}, {("llm", "a"): ("https", "a.example", 443)}),
        ({("llm", "a"): 1}, {}),
        ({}, {("llm", "a"): ("https", "a.example", 443)}),
        ({("llm", "a"): 1}, {("llm", "a"): ("http", "a.example", 0)}),
    ],
)
def test_invalid_capacity_or_origin_maps_fail_closed(capacities, origins):
    errors = importlib.import_module("labelkit.common.errors")
    with pytest.raises(errors.InternalError):
        _manager(capacities, origins)


async def test_resource_limit_is_shared_by_all_logical_calls_for_one_profile():
    key = ("llm", "shared")
    manager, metrics = _manager({key: 3}, {key: ("https", "a.example", 443)})
    release = asyncio.Event()
    saturated = asyncio.Event()
    active = 0
    maximum = 0

    async def call() -> None:
        nonlocal active, maximum
        async with manager.resource_limit(key):
            active += 1
            maximum = max(maximum, active)
            if active == 3:
                saturated.set()
            try:
                await release.wait()
            finally:
                active -= 1

    calls = [asyncio.create_task(call()) for _ in range(12)]
    await asyncio.wait_for(saturated.wait(), timeout=3)
    await asyncio.sleep(0.01)
    assert active == 3
    release.set()
    await asyncio.gather(*calls)

    assert maximum == 3
    assert metrics.high_water["resource_wait"] >= 1
    assert metrics.totals["resource_wait_ms"] > 0


async def test_same_origin_aggregates_capacities_while_other_origin_is_independent():
    first = ("llm", "first")
    second = ("embedding", "second")
    third = ("llm", "third")
    shared = ("https", "shared.example", 443)
    independent = ("https", "independent.example", 443)
    manager, _metrics = _manager(
        {first: 2, second: 3, third: 1},
        {first: shared, second: shared, third: independent},
    )
    release = asyncio.Event()
    shared_full = asyncio.Event()
    independent_started = asyncio.Event()
    shared_active = 0
    shared_maximum = 0

    async def shared_call() -> None:
        nonlocal shared_active, shared_maximum
        async with manager.origin_limit(shared):
            shared_active += 1
            shared_maximum = max(shared_maximum, shared_active)
            if shared_active == 5:
                shared_full.set()
            try:
                await release.wait()
            finally:
                shared_active -= 1

    async def independent_call() -> None:
        async with manager.origin_limit(independent):
            independent_started.set()
            await release.wait()

    calls = [asyncio.create_task(shared_call()) for _ in range(10)]
    calls.append(asyncio.create_task(independent_call()))
    await asyncio.wait_for(shared_full.wait(), timeout=3)
    await asyncio.wait_for(independent_started.wait(), timeout=1)
    assert shared_active == 5
    release.set()
    await asyncio.gather(*calls)

    assert shared_maximum == 5


async def test_repair_profile_switch_uses_its_own_single_logical_permit():
    main = ("llm", "main")
    repair = ("llm", "repair")
    manager, _metrics = _manager(
        {main: 3, repair: 1},
        {
            main: ("https", "main.example", 443),
            repair: ("https", "repair.example", 443),
        },
    )
    all_main_started = asyncio.Event()
    release_repair = asyncio.Event()
    main_active = 0
    repair_active = 0
    repair_maximum = 0

    async def main_then_repair() -> None:
        nonlocal main_active, repair_active, repair_maximum
        async with manager.resource_limit(main):
            main_active += 1
            if main_active == 3:
                all_main_started.set()
            try:
                async with manager.resource_limit(repair):
                    repair_active += 1
                    repair_maximum = max(repair_maximum, repair_active)
                    try:
                        await release_repair.wait()
                    finally:
                        repair_active -= 1
            finally:
                main_active -= 1

    calls = [asyncio.create_task(main_then_repair()) for _ in range(3)]
    await asyncio.wait_for(all_main_started.wait(), timeout=3)
    await asyncio.sleep(0.01)
    assert main_active == 3
    assert repair_active == 1
    release_repair.set()
    await asyncio.gather(*calls)

    assert repair_maximum == 1


async def test_cancelled_waiter_does_not_consume_or_leak_resource_and_origin_permits():
    key = ("llm", "cancel")
    origin = ("https", "cancel.example", 443)
    manager, _metrics = _manager({key: 1}, {key: origin})

    async with manager.resource_limit(key):
        waiter = asyncio.create_task(_take_resource(manager, key))
        await asyncio.sleep(0.02)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    assert _metrics.totals["resource_wait_ms"] > 0
    async with asyncio.timeout(1):
        async with manager.resource_limit(key):
            pass

    async with manager.origin_limit(origin):
        waiter = asyncio.create_task(_take_origin(manager, origin))
        await asyncio.sleep(0.02)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    assert _metrics.totals["http_pool_wait_ms"] > 0
    async with asyncio.timeout(1):
        async with manager.origin_limit(origin):
            pass


async def _take_resource(manager, key) -> None:
    async with manager.resource_limit(key):
        pass


async def _take_origin(manager, origin) -> None:
    async with manager.origin_limit(origin):
        pass


def _except_names(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return set()
    nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    return names


def test_pool_timeout_is_fail_closed_before_retryable_transport_handling_without_mock_transport():
    llm_client = importlib.import_module("labelkit.common.inference.llm_client")
    source = textwrap.dedent(inspect.getsource(llm_client.LLMClient._dispatch_attempt))
    tree = ast.parse(source)
    handlers = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
    pool_handlers = [handler for handler in handlers if "PoolTimeout" in _except_names(handler)]
    retry_handlers = [
        handler
        for handler in handlers
        if _except_names(handler) & {"TimeoutException", "TransportError"}
    ]

    assert len(pool_handlers) == 1
    assert retry_handlers
    assert pool_handlers[0].lineno < min(handler.lineno for handler in retry_handlers)
    assert "InternalError" in ast.unparse(pool_handlers[0])


def test_llm_client_has_no_private_profile_semaphore_fallback():
    llm_client = importlib.import_module("labelkit.common.inference.llm_client")
    source = inspect.getsource(llm_client.LLMClient)

    assert "_semaphores" not in source
    assert "def _semaphore" not in source
    assert "asyncio.Semaphore" not in source

from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@dataclass
class _GraphState:
    resources: list[Any]
    runtimes: list[Any]
    clients: list[Any]
    services: list[dict[str, Any]]
    workflow_loops: list[asyncio.AbstractEventLoop]
    close_loops: list[asyncio.AbstractEventLoop]
    event_log_closes: int = 0


def _profile(name: str, base_url: str, capacity: int):
    return SimpleNamespace(
        name=name,
        base_url=base_url,
        max_concurrency=capacity,
        provider="openai_compatible",
        model=f"model-{name}",
    )


def _config(*, dry_run: bool = False):
    return SimpleNamespace(
        dry_run=dry_run,
        llm_profiles={
            "a": _profile("a", "HTTPS://SHARED.example/v1", 2),
            "b": _profile("b", "http://other.example/v1", 7),
        },
        embedding_profiles={
            "e": _profile("e", "https://shared.example:443/embed", 3),
        },
        user_schema={},
        output=object(),
        run=SimpleNamespace(mode="process"),
        generate=SimpleNamespace(form="flat"),
        trace=SimpleNamespace(enabled=False, path=None),
        paths=SimpleNamespace(trace=None),
    )


def _install_graph(
    monkeypatch,
    *,
    workflow_result: Any = None,
    workflow_error: BaseException | None = None,
    workflow_init_error: BaseException | None = None,
    credentials_error: BaseException | None = None,
    close_error: BaseException | None = None,
    dry_run: bool = False,
):
    application = importlib.import_module("labelkit.orchestration.application")
    state = _GraphState([], [], [], [], [], [])
    cfg = _config(dry_run=dry_run)
    summary = workflow_result or SimpleNamespace(exit_code=0)

    class EventLog:
        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            state.event_log_closes += 1

    class Metrics:
        def __init__(self, *_args, **_kwargs):
            self.counters = {}
            self.fatal_streak = 0

    class Resources:
        def __init__(self, capacities, origins, metrics):
            self.capacities = dict(capacities)
            self.origins = dict(origins)
            self.metrics = metrics
            state.resources.append(self)

    class Client:
        def __init__(self, *_args, **_kwargs):
            self.close_calls = 0
            self.probe_calls: list[tuple[str, str]] = []
            state.clients.append(self)

        async def aclose(self):
            self.close_calls += 1
            state.close_loops.append(asyncio.get_running_loop())
            if close_error is not None:
                raise close_error

        async def probe_all(self, resource_key):
            self.probe_calls.append(resource_key)
            state.workflow_loops.append(asyncio.get_running_loop())
            if workflow_error is not None:
                raise workflow_error
            return [f"probe:{resource_key[0]}:{resource_key[1]}"]

        def snapshot(self):
            return ()

    class Runtime:
        def __init__(self, resources, metrics):
            self.resources = resources
            self.metrics = metrics
            self.run_calls = 0
            state.runtimes.append(self)

        async def run(self, workflow):
            self.run_calls += 1
            state.workflow_loops.append(asyncio.get_running_loop())
            if workflow_error is not None:
                raise workflow_error
            value = workflow() if callable(workflow) else workflow
            return await value

    class Workflow:
        def __init__(self, *_args, **_kwargs):
            if workflow_init_error is not None:
                raise workflow_init_error

        async def run(self):
            state.workflow_loops.append(asyncio.get_running_loop())
            if workflow_error is not None:
                raise workflow_error
            return summary

    def run_services(**kwargs):
        state.services.append(dict(kwargs))
        return SimpleNamespace(**kwargs)

    def run_credentials(_cfg):
        if credentials_error is not None:
            raise credentials_error
        return object()

    monkeypatch.setattr(application, "load", lambda *_args, **_kwargs: cfg)
    monkeypatch.setattr(application, "_sequence_plan_for_run", lambda _cfg: None)
    monkeypatch.setattr(application, "_compile_sequence_plan", lambda _cfg: None)
    monkeypatch.setattr(application, "setup_logging", lambda _cfg: None)
    monkeypatch.setattr(application, "_runtime_run_id", lambda *_args: "run")
    monkeypatch.setattr(application, "_trace_config", lambda _cfg: object())
    monkeypatch.setattr(application, "_run_credentials", run_credentials)
    monkeypatch.setattr(application, "resolve_credentials", lambda _cfg: object())
    monkeypatch.setattr(application, "referenced_profiles", lambda _cfg: (("a", "b"), ("e",)))
    monkeypatch.setattr(application, "EventLog", EventLog)
    monkeypatch.setattr(application, "MetricsSink", Metrics)
    monkeypatch.setattr(application, "ResourceManager", Resources, raising=False)
    monkeypatch.setattr(application, "ExecutionRuntime", Runtime, raising=False)
    monkeypatch.setattr(application, "LLMClient", Client)
    monkeypatch.setattr(application, "SchemaEngine", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(application, "RunServices", run_services)
    monkeypatch.setattr(application, "ProcessWorkflow", Workflow)
    monkeypatch.setattr(application, "build_stages", lambda _cfg: [])
    monkeypatch.setattr(application, "_build_ingestor", lambda *_args: None)
    monkeypatch.setattr(application, "Emitter", lambda *_args, **_kwargs: object())
    return application, state, cfg


def _execute(application):
    return application.execute_run(
        Path("config.toml"),
        Path("project.toml"),
        SimpleNamespace(),
    )


def test_static_validate_never_constructs_runtime_or_network_client(monkeypatch):
    application, state, _cfg = _install_graph(monkeypatch)

    result = application.validate_project(
        Path("config.toml"),
        Path("project.toml"),
        SimpleNamespace(),
    )

    assert result is _cfg
    assert state.resources == []
    assert state.runtimes == []
    assert state.clients == []


def test_static_dry_run_builds_runtime_without_running_it(monkeypatch):
    application, state, _cfg = _install_graph(monkeypatch, dry_run=True)

    assert _execute(application) == 0
    assert len(state.runtimes) == 1
    assert state.runtimes[0].run_calls == 0
    assert all(client.probe_calls == [] for client in state.clients)


def test_live_run_uses_one_runtime_identity_and_closes_client_once(monkeypatch):
    application, state, _cfg = _install_graph(monkeypatch)

    assert _execute(application) == 0

    assert len(state.resources) == 1
    assert len(state.runtimes) == 1
    assert state.runtimes[0].run_calls == 1
    assert len(state.clients) == 1
    assert state.clients[0].close_calls == 1
    assert state.services[0]["tasks"] is state.runtimes[0]
    assert state.workflow_loops[-1] is state.close_loops[0]


def test_workflow_construction_failure_closes_client_and_event_log(monkeypatch):
    primary = _PrimaryFailure("workflow-construction")
    application, state, _cfg = _install_graph(
        monkeypatch,
        workflow_init_error=primary,
    )

    with pytest.raises(_PrimaryFailure) as caught:
        _execute(application)

    assert caught.value is primary
    assert state.clients[0].close_calls == 1
    assert state.event_log_closes == 1


def test_credentials_failure_closes_event_log_without_constructing_client(monkeypatch):
    primary = _PrimaryFailure("credentials")
    application, state, _cfg = _install_graph(monkeypatch, credentials_error=primary)

    with pytest.raises(_PrimaryFailure) as caught:
        _execute(application)

    assert caught.value is primary
    assert state.clients == []
    assert state.event_log_closes == 1


def test_application_normalizes_and_aggregates_referenced_profile_origins(monkeypatch):
    application, state, _cfg = _install_graph(monkeypatch)

    assert _execute(application) == 0
    manager = state.resources[0]

    assert manager.capacities == {
        ("llm", "a"): 2,
        ("llm", "b"): 7,
        ("embedding", "e"): 3,
    }
    assert manager.origins == {
        ("llm", "a"): ("https", "shared.example", 443),
        ("llm", "b"): ("http", "other.example", 80),
        ("embedding", "e"): ("https", "shared.example", 443),
    }


def test_explicit_zero_port_is_not_folded_into_default_origin():
    application = importlib.import_module("labelkit.orchestration.application")

    assert application._normalize_origin("http://example.test:0/v1") == (
        "http", "example.test", 0,
    )


class _PrimaryFailure(RuntimeError):
    pass


class _CloseFailure(RuntimeError):
    pass


def test_primary_run_failure_survives_logged_close_failure(monkeypatch, caplog):
    primary = _PrimaryFailure("primary")
    close = _CloseFailure("close")
    application, state, _cfg = _install_graph(
        monkeypatch,
        workflow_error=primary,
        close_error=close,
    )

    with pytest.raises(_PrimaryFailure) as caught:
        _execute(application)

    assert caught.value is primary
    assert state.clients[0].close_calls == 1
    assert "close" in caplog.text.lower()


def test_close_failure_without_primary_becomes_internal_error(monkeypatch):
    close = _CloseFailure("close")
    application, state, _cfg = _install_graph(monkeypatch, close_error=close)
    errors = importlib.import_module("labelkit.common.errors")

    with pytest.raises(errors.InternalError):
        _execute(application)

    assert state.clients[0].close_calls == 1


def test_cancelled_live_run_closes_once_and_preserves_cancelled_error(monkeypatch):
    cancelled = asyncio.CancelledError()
    application, state, _cfg = _install_graph(monkeypatch, workflow_error=cancelled)

    with pytest.raises(asyncio.CancelledError) as caught:
        _execute(application)

    assert caught.value is cancelled
    assert state.clients[0].close_calls == 1


def test_probe_uses_resources_and_closes_in_the_same_event_loop(monkeypatch):
    application, state, cfg = _install_graph(monkeypatch)

    result = application.probe_referenced_profiles(cfg)

    assert result == ("probe:llm:a", "probe:llm:b", "probe:embedding:e")
    assert len(state.resources) == 1
    assert len(state.clients) == 1
    assert state.clients[0].probe_calls == [
        ("llm", "a"),
        ("llm", "b"),
        ("embedding", "e"),
    ]
    assert state.clients[0].close_calls == 1
    assert all(loop is state.close_loops[0] for loop in state.workflow_loops)


def test_probe_distinguishes_same_named_llm_and_embedding_profiles(monkeypatch):
    application, state, cfg = _install_graph(monkeypatch)
    cfg.embedding_profiles["a"] = _profile("a", "https://embed.example/v1", 3)
    monkeypatch.setattr(application, "referenced_profiles", lambda _cfg: (("a",), ("a",)))

    result = application.probe_referenced_profiles(cfg)

    assert result == ("probe:llm:a", "probe:embedding:a")
    assert state.clients[0].probe_calls == [("llm", "a"), ("embedding", "a")]


def test_probe_primary_failure_survives_close_failure(monkeypatch, caplog):
    primary = _PrimaryFailure("probe-primary")
    close = _CloseFailure("probe-close")
    application, state, cfg = _install_graph(
        monkeypatch,
        workflow_error=primary,
        close_error=close,
    )

    with pytest.raises(_PrimaryFailure) as caught:
        application.probe_referenced_profiles(cfg)

    assert caught.value is primary
    assert state.clients[0].close_calls == 1
    assert "close" in caplog.text.lower()

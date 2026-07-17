"""Runtime object-graph assembly for run and validation commands.

This module owns composition only. It does not parse argparse namespaces, print
user-facing text, map exceptions to exit codes, or implement operator behavior.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from labelkit.common.config import load
from labelkit.common.config.model import CliOverrides, ResolvedConfig
from labelkit.common.observability.obslog import EventLog, MetricsSink, setup_logging
from labelkit.common.runtime.llm_client import LLMClient
from labelkit.common.runtime.schema_engine import SchemaEngine
from labelkit.orchestration.factory import build_stages
from labelkit.orchestration.orchestrator import Orchestrator
from labelkit.orchestration.profile_usage import referenced_profiles
from labelkit.operators.emitter import Emitter

if TYPE_CHECKING:
    from labelkit.common.observability.obslog import ProgressListener
    from labelkit.common.runtime.llm_client import ProbeResult

__all__ = ["execute_run", "probe_referenced_profiles", "validate_project"]

_log = logging.getLogger("labelkit.runtime")


def _activate_listener(listener: "ProgressListener", cfg: ResolvedConfig,
                       llm: LLMClient, metrics: MetricsSink) -> None:
    """v1.10 (U19 调用时序): activate the lazy-shell renderer once — after full
    assembly, before ``asyncio.run`` — handing it the ResolvedConfig plus the
    three read-only pull closures for its render tick (``LLMClient.snapshot``,
    the MetricsSink counters, the breaker streak).

    U23 discipline on failure: one WARN, then the sink's listener reference is
    set to None for the whole run (``_listener`` is the documented Wave-1
    storage — ``MetricsSink._forward`` nulls the same attribute on its own
    forward failures). A listener bug never affects exit codes or output."""
    try:
        listener.on_run_context(cfg, llm.snapshot,
                                lambda: dict(metrics.counters),
                                lambda: metrics.fatal_streak)
    except Exception as exc:  # noqa: BLE001 — bypass isolation (U7/U23)
        metrics._listener = None
        _log.warning("console listener 异常，已停用面板旁路: %s", exc,
                     extra={"stage": "run", "batch": 0})


def execute_run(
    config_path: str | Path,
    project_path: str | Path,
    overrides: CliOverrides,
    listener: "ProgressListener | None" = None,
) -> int:
    """Load configuration, assemble the runtime graph, and execute one run.

    v1.10 (U19): ``listener`` is the console panel's in-process bypass — wired
    into the MetricsSink at construction and activated via ``on_run_context``
    right before the event loop starts; None (every pre-v1.10 caller) is
    byte-identical to v1.9."""
    cfg = load(Path(config_path), Path(project_path), overrides)
    setup_logging(cfg)
    run_id = secrets.token_hex(6)
    run_started_at = datetime.now().astimezone()

    trace_cfg = cfg.trace
    if cfg.dry_run and trace_cfg.enabled and trace_cfg.path:
        path = Path(trace_cfg.path)
        trace_cfg = replace(
            trace_cfg,
            path=str(path.with_name(path.stem + ".dryrun" + path.suffix)),
        )

    event_log = EventLog(trace_cfg, run_id)
    metrics = MetricsSink(cfg, run_id, event_log, listener=listener)
    llm = LLMClient(cfg.llm_profiles, cfg.embedding_profiles, metrics)
    schema_engine = SchemaEngine(dict(cfg.user_schema), llm, cfg.output, metrics)
    stages = build_stages(cfg)

    ingestor = None
    if cfg.run.mode == "process":
        from labelkit.operators.ingest import Ingestor

        ingestor = Ingestor(cfg)
        ingestor.metrics = metrics

    emitter = Emitter(cfg, schema_engine, run_id, run_started_at)
    orchestrator = Orchestrator(
        cfg,
        stages,
        ingestor,
        emitter,
        llm,
        schema_engine,
        metrics,
        run_id,
        run_started_at,
    )
    if listener is not None:
        _activate_listener(listener, cfg, llm, metrics)
    try:
        summary = asyncio.run(orchestrator.run())
    finally:
        event_log.close()
    return summary.exit_code


def validate_project(
    config_path: str | Path,
    project_path: str | Path,
    overrides: CliOverrides = CliOverrides(),
) -> ResolvedConfig:
    """Load and fully validate one tool/project configuration pair.

    v1.10 (U27): the CLI passes its parsed overrides through so ``--console``
    reaches M1 on the validate path too (the jsonl × explicit-rich WARN
    fires here as well); existing callers keep the zero-override default."""
    return load(Path(config_path), Path(project_path), overrides)


def probe_referenced_profiles(cfg: ResolvedConfig) -> tuple["ProbeResult", ...]:
    """Probe every profile actually referenced by enabled stages."""
    llm_names, emb_names = referenced_profiles(cfg)
    client = LLMClient(cfg.llm_profiles, cfg.embedding_profiles, None)

    async def _probe_all() -> list[ProbeResult]:
        results: list[ProbeResult] = []
        for name in (*llm_names, *emb_names):
            results.extend(await client.probe_all(name))
        return results

    return tuple(asyncio.run(_probe_all()))

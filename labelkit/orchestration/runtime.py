"""Runtime object-graph assembly for run and validation commands.

This module owns composition only. It does not parse argparse namespaces, print
user-facing text, map exceptions to exit codes, or implement operator behavior.
"""
from __future__ import annotations

import asyncio
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
    from labelkit.common.runtime.llm_client import ProbeResult

__all__ = ["execute_run", "probe_referenced_profiles", "validate_project"]


def execute_run(
    config_path: str | Path,
    project_path: str | Path,
    overrides: CliOverrides,
) -> int:
    """Load configuration, assemble the runtime graph, and execute one run."""
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
    metrics = MetricsSink(cfg, run_id, event_log)
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
    try:
        summary = asyncio.run(orchestrator.run())
    finally:
        event_log.close()
    return summary.exit_code


def validate_project(
    config_path: str | Path,
    project_path: str | Path,
) -> ResolvedConfig:
    """Load and fully validate one tool/project configuration pair."""
    return load(Path(config_path), Path(project_path), CliOverrides())


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

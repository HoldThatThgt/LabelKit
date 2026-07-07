"""Entry layer: ``labelkit run | validate | rubric`` (spec §2.4, CONTRACTS.md §7.12).

Owns argument parsing, module wiring for a run, and the ONLY exception → exit-code
mapping in the tool (no other module calls ``sys.exit``).

Imports of the not-yet-guaranteed sibling modules (loader, ingest, orchestrator, ...)
are performed lazily inside the subcommand handlers so that the pure-logic surface of
this module (argument parsing, exit-code mapping, rubric printing) stays importable
and unit-testable on its own.
"""
from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from labelkit.config.model import CliOverrides
from labelkit.errors import (
    EXIT_CONFIG,
    EXIT_FATAL,
    EXIT_INPUT,
    EXIT_OK,
    EXIT_STRICT,
    CircuitBreakerTripped,
    ConfigError,
    InputError,
    LabelKitError,
    ProviderFatalError,
)

if TYPE_CHECKING:
    from labelkit.config.model import ResolvedConfig
    from labelkit.stage import Stage

# rubric selector → packaged file (labelkit/data/rubrics/*)
_RUBRIC_FILES: dict[str, str] = {
    "default:text": "default_text.toml",
    "default:ui": "default_ui.toml",
}

# The one exit-1 LabelKitError, per CONTRACTS.md §7.10 (Emitter.finalize contract).
_REPORT_WRITE_FAILED_MSG = "report write failed"


# ── exception → exit-code mapping (spec §2.4; lives ONLY here) ─────────────

def exit_code_for(exc: BaseException) -> int:
    """Map an escaped exception to the spec §2.4 exit code.

    ConfigError → 2; InputError → 3; provider-fatal / circuit break → 4;
    report-write failure (LabelKitError('report write failed'), frozen in
    CONTRACTS §7.10) → 1; any other LabelKitError (e.g. unwritable output from
    Emitter.open) or unexpected exception → 4.
    """
    if isinstance(exc, ConfigError):
        return EXIT_CONFIG
    if isinstance(exc, InputError):
        return EXIT_INPUT
    if isinstance(exc, (ProviderFatalError, CircuitBreakerTripped)):
        return EXIT_FATAL
    if isinstance(exc, LabelKitError) and str(exc) == _REPORT_WRITE_FAILED_MSG:
        return EXIT_STRICT
    return EXIT_FATAL


def _print_exception(exc: BaseException) -> None:
    """Render an escaped exception on stderr (never data content, never keys)."""
    if isinstance(exc, ConfigError):
        # Aggregated feedback format per spec 3.1.5 / 3.1.6 example ②.
        print(f"ConfigError: {len(exc.errors)} 个配置错误（全量聚合反馈）", file=sys.stderr)
        for line in exc.errors:
            print(line, file=sys.stderr)
    else:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)


# ── argparse ───────────────────────────────────────────────────────────────

def _positive_int(value: str) -> int:
    """argparse type for ``--limit``: integer ≥ 1.

    ``--limit`` is part of the CLI 参数字典 that M1 validates (spec 3.1.2); a
    zero/negative value is a usage/config error and must surface as exit 2
    (spec §2.4), not as a runtime ``ValueError`` deep inside the orchestrator
    (spec 3.1.5: no runtime config errors). argparse errors exit with code 2,
    matching EXIT_CONFIG.
    """
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"期望 ≥ 1 的整数，得到 {value!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"期望 ≥ 1 的整数，得到 {n}")
    return n


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="labelkit",
        description="LLM-powered stateless batch pipeline: dedup / quality / annotate / generate / verify.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="execute the pipeline")
    p_run.add_argument("--config", required=True, help="path to config.toml")
    p_run.add_argument("--project", required=True, help="path to project.toml")
    p_run.add_argument("--input", default=None, help="override project.toml run.input")
    p_run.add_argument("--output", default=None, help="override project.toml run.output")
    p_run.add_argument("--limit", type=_positive_int, default=None, metavar="N",
                       help="process only the first N records (trial run)")
    p_run.add_argument("--dry-run", action="store_true",
                       help="M1/M2 validation + cost estimate only; no LLM calls")
    p_run.add_argument("--strict", action="store_true",
                       help="exit 1 if any record is rejected")
    p_run.add_argument("--log-level", default=None,
                       choices=("debug", "info", "warn", "error"),
                       help="stderr log level (default: info)")

    p_val = sub.add_parser("validate", help="M1 full validation only (no run)")
    p_val.add_argument("--config", required=True, help="path to config.toml")
    p_val.add_argument("--project", required=True, help="path to project.toml")
    p_val.add_argument("--probe", action="store_true",
                       help="also probe connectivity of every referenced profile")

    p_rub = sub.add_parser("rubric", help="print / list the packaged default rubrics")
    p_rub.add_argument("--show", default=None, choices=sorted(_RUBRIC_FILES),
                       help="print the named default rubric TOML verbatim to stdout")

    return parser


def _overrides_from_args(args: argparse.Namespace) -> CliOverrides:
    return CliOverrides(
        input=args.input,
        output=args.output,
        limit=args.limit,
        dry_run=args.dry_run,
        strict=args.strict,
        log_level=args.log_level,
    )


# ── wiring helpers (pure logic, unit-testable) ─────────────────────────────

def referenced_profiles(cfg: "ResolvedConfig") -> tuple[list[str], list[str]]:
    """(llm profile names, embedding profile names) actually referenced by the
    enabled stages, order-preserving and deduplicated. Used by ``validate --probe``.

    "Referenced" follows M1's definition (spec 3.1.4 API-Key row, 3.1.6 example
    ①: an unreferenced profile needs no key and is never probed):

    * classify — referenced iff enabled (v1.7, R24 reference-set point ③;
      guard mirrors loader rule 12).
    * quality — in ``pointwise`` mode every scoring call uses ``quality.llm``
      and the judges panel is never consulted (spec §3.4.4; judges are defined
      over pairwise comparisons only); in ``pairwise`` mode a non-empty
      ``judges`` panel *replaces* ``quality.llm``.
    * verify — a non-empty ``judges`` panel replaces ``verify.llm``.
    """
    llm_names: list[str] = []
    if cfg.classify.enabled:
        llm_names.append(cfg.classify.llm)
    if cfg.quality.enabled:
        if cfg.quality.mode == "pointwise" or not cfg.quality.judges:
            llm_names.append(cfg.quality.llm)
        else:
            llm_names.extend(cfg.quality.judges)
    if cfg.annotate.enabled:
        llm_names.append(cfg.annotate.llm)
    if cfg.generate.enabled:
        llm_names.extend(cfg.generate.llms)
    if cfg.verify.enabled:
        if cfg.verify.judges:
            llm_names.extend(cfg.verify.judges)
        else:
            llm_names.append(cfg.verify.llm)
    if cfg.output.repair_llm:
        llm_names.append(cfg.output.repair_llm)
    emb_names: list[str] = []
    if cfg.dedup.enabled and cfg.dedup.semantic and cfg.dedup.semantic_embedding:
        emb_names.append(cfg.dedup.semantic_embedding)
    return list(dict.fromkeys(llm_names)), list(dict.fromkeys(emb_names))


def _build_stages(cfg: "ResolvedConfig") -> list["Stage"]:
    """Instantiate enabled operator stages in pipeline order (CONTRACTS §2)."""
    from labelkit.annotate import AnnotateStage
    from labelkit.classify import ClassifyStage
    from labelkit.dedup import DedupIndex, DedupStage
    from labelkit.generate import GenerateStage
    from labelkit.quality import QualityStage
    from labelkit.verify import VerifyStage

    stages: list[Stage] = []
    if cfg.dedup.enabled:
        stages.append(DedupStage(cfg.dedup, DedupIndex(cfg.dedup, cfg.run.modality)))
    if cfg.classify.enabled:
        stages.append(ClassifyStage(cfg))
    if cfg.quality.enabled:
        stages.append(QualityStage(cfg))
    if cfg.generate.enabled:
        stages.append(GenerateStage(cfg))
    if cfg.annotate.enabled:
        stages.append(AnnotateStage(cfg))
    if cfg.verify.enabled:
        stages.append(VerifyStage(cfg))
    return stages


# ── subcommands ────────────────────────────────────────────────────────────

def _cmd_run(args: argparse.Namespace) -> int:
    from labelkit.config import load

    cfg = load(Path(args.config), Path(args.project), _overrides_from_args(args))

    from labelkit.emitter import Emitter
    from labelkit.llm_client import LLMClient
    from labelkit.obslog import EventLog, MetricsSink, setup_logging
    from labelkit.orchestrator import Orchestrator
    from labelkit.schema_engine import SchemaEngine

    setup_logging(cfg)
    run_id = secrets.token_hex(6)
    run_started_at = datetime.now().astimezone()

    trace_cfg = cfg.trace
    if cfg.dry_run and trace_cfg.enabled and trace_cfg.path:
        # A rehearsal must never truncate the previous real run's trace
        # (E2E finding P2-4): divert to "<name>.dryrun<suffix>".
        from dataclasses import replace as _dc_replace
        p = Path(trace_cfg.path)
        trace_cfg = _dc_replace(trace_cfg, path=str(p.with_name(p.stem + ".dryrun" + p.suffix)))
    event_log = EventLog(trace_cfg, run_id)
    metrics = MetricsSink(cfg, run_id, event_log)
    llm = LLMClient(cfg.llm_profiles, cfg.embedding_profiles, metrics)
    schema_engine = SchemaEngine(dict(cfg.user_schema), llm, cfg.output, metrics)
    stages = _build_stages(cfg)

    ingestor = None
    if cfg.run.mode == "process":
        from labelkit.ingest import Ingestor
        ingestor = Ingestor(cfg)
        ingestor.metrics = metrics  # trace wiring, CONTRACTS §7.1

    emitter = Emitter(cfg, schema_engine, run_id, run_started_at)
    orchestrator = Orchestrator(cfg, stages, ingestor, emitter, llm,
                                schema_engine, metrics, run_id, run_started_at)
    try:
        summary = asyncio.run(orchestrator.run())
    finally:
        event_log.close()
    return summary.exit_code


def _cmd_validate(args: argparse.Namespace) -> int:
    from labelkit.config import load

    cfg = load(Path(args.config), Path(args.project), CliOverrides())
    print("配置校验通过", file=sys.stderr)

    if args.probe:
        from labelkit.llm_client import LLMClient

        llm_names, emb_names = referenced_profiles(cfg)
        client = LLMClient(cfg.llm_profiles, cfg.embedding_profiles, None)

        async def _probe_all() -> list:
            # v1.6: one probe per pool key (spec 3.9.2 probe_all) — pooled
            # profiles print one line per key; single-key lines are unchanged.
            results: list = []
            for name in (*llm_names, *emb_names):
                results.extend(await client.probe_all(name))
            return results

        for result in asyncio.run(_probe_all()):
            label = (f"{result.profile}[{result.key_env}]"
                     if result.key_env else result.profile)
            if result.ok:
                print(f"probe {label}: ok model={result.model} "
                      f"latency_ms={result.latency_ms}")
            else:
                print(f"probe {label}: FAIL {result.error}")
        # Probe failures do not change the exit code (CONTRACTS §7.12, frozen).
    return EXIT_OK


def _cmd_rubric(args: argparse.Namespace) -> int:
    if args.show is None:
        for name in _RUBRIC_FILES:
            print(name)
        return EXIT_OK
    text = (
        resources.files("labelkit")
        .joinpath("data", "rubrics", _RUBRIC_FILES[args.show])
        .read_text(encoding="utf-8")
    )
    sys.stdout.write(text)  # verbatim — no added newline, no reformatting
    return EXIT_OK


# ── entry point ────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"run": _cmd_run, "validate": _cmd_validate, "rubric": _cmd_rubric}
    try:
        return handlers[args.command](args)
    except LabelKitError as exc:
        _print_exception(exc)
        return exit_code_for(exc)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_FATAL
    except Exception as exc:  # unexpected → fatal runtime error
        _print_exception(exc)
        return exit_code_for(exc)


if __name__ == "__main__":
    sys.exit(main())

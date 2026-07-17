"""User-facing command handlers for run, validate, and rubric."""
from __future__ import annotations

import argparse
import sys
from importlib import resources

from labelkit.common.config.model import CliOverrides
from labelkit.common.errors import EXIT_OK
from labelkit.orchestration.runtime import (
    execute_run,
    probe_referenced_profiles,
    validate_project,
)

from .console import ConsoleRenderer
from .parser import _RUBRIC_FILES, _overrides_from_args


def _cmd_run(args: argparse.Namespace) -> int:
    # v1.10 (U19): the renderer is ALWAYS passed as a lazy shell — it
    # self-configures on on_run_context (rich → panel; plain ∧ heartbeat>0 ∧
    # non-TTY → heartbeat; anything else stays inert, byte-identical to v1.9).
    renderer = ConsoleRenderer()
    return execute_run(args.config, args.project, _overrides_from_args(args),
                       listener=renderer)


def _cmd_validate(args: argparse.Namespace) -> int:
    # v1.10 (U27): --console reaches M1 on the validate path too (the
    # jsonl × explicit-rich WARN fires here as well). The validate namespace
    # has no run-only fields, so the overrides are built inline.
    cfg = validate_project(args.config, args.project,
                           overrides=CliOverrides(console=args.console))
    print("配置校验通过", file=sys.stderr)

    if args.probe:
        results = probe_referenced_profiles(cfg)
        # v1.10 (U13/U27): the probe TABLE renders only under rich mode AND a
        # stdout TTY — script consumers keep the byte-identical line format
        # (stdout channel responsibility unchanged).
        if cfg.console.mode_resolved == "rich" and sys.stdout.isatty():
            if _print_probe_table(results):
                return EXIT_OK
        for result in results:
            label = (
                f"{result.profile}[{result.key_env}]"
                if result.key_env
                else result.profile
            )
            if result.ok:
                print(
                    f"probe {label}: ok model={result.model} "
                    f"latency_ms={result.latency_ms}"
                )
            else:
                print(f"probe {label}: FAIL {result.error}")
    return EXIT_OK


def _print_probe_table(results) -> bool:
    """Render the rich probe table to stdout; False (→ plain lines) when rich
    cannot actually be imported (mode_resolved probed find_spec only, U21)."""
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        return False
    table = Table(title="validate --probe")
    table.add_column("profile[key]")
    table.add_column("status")
    table.add_column("model")
    table.add_column("latency_ms", justify="right")
    table.add_column("error")
    for result in results:
        label = (f"{result.profile}[{result.key_env}]" if result.key_env
                 else result.profile)
        status = "[green]ok[/green]" if result.ok else "[red]FAIL[/red]"
        table.add_row(label, status, result.model, str(result.latency_ms),
                      result.error or "")
    Console().print(table)
    return True


def _cmd_rubric(args: argparse.Namespace) -> int:
    # `rubric --show` is machine-consumed stdout — ALWAYS plain (U13).
    if args.show is None:
        for name in _RUBRIC_FILES:
            print(name)
        return EXIT_OK
    text = (
        resources.files("labelkit")
        .joinpath("data", "rubrics", _RUBRIC_FILES[args.show])
        .read_text(encoding="utf-8")
    )
    sys.stdout.write(text)
    return EXIT_OK

"""User-facing command handlers for run, validate, and rubric."""
from __future__ import annotations

import argparse
import sys
from importlib import resources

from labelkit.common.errors import EXIT_OK
from labelkit.orchestration.runtime import (
    execute_run,
    probe_referenced_profiles,
    validate_project,
)

from .parser import _RUBRIC_FILES, _overrides_from_args


def _cmd_run(args: argparse.Namespace) -> int:
    return execute_run(args.config, args.project, _overrides_from_args(args))


def _cmd_validate(args: argparse.Namespace) -> int:
    cfg = validate_project(args.config, args.project)
    print("配置校验通过", file=sys.stderr)

    if args.probe:
        for result in probe_referenced_profiles(cfg):
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
    sys.stdout.write(text)
    return EXIT_OK

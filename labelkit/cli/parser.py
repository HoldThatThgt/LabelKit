"""Argument parsing and CLI override conversion."""
from __future__ import annotations

import argparse

from labelkit.common.config.model import CliOverrides

_RUBRIC_FILES: dict[str, str] = {
    "default:text": "default_text.toml",
    "default:ui": "default_ui.toml",
    "default:trajectory": "default_trajectory.toml",
}

__all__ = ["build_parser"]


def _positive_int(value: str) -> int:
    """Parse ``--limit`` as an integer greater than or equal to one."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"期望 ≥ 1 的整数，得到 {value!r}"
        ) from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"期望 ≥ 1 的整数，得到 {number}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="labelkit",
        description=(
            "LLM-powered stateless batch pipeline: segment / stitch / dedup / "
            "classify / extract / quality / generate / annotate / verify."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="execute the pipeline")
    run.add_argument("--config", required=True, help="path to config.toml")
    run.add_argument("--project", required=True, help="path to project.toml")
    run.add_argument("--input", default=None, help="override project.toml run.input")
    run.add_argument("--output", default=None, help="override project.toml run.output")
    run.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        metavar="N",
        help="process only the first N records (trial run)",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="M1/M2 validation + cost estimate only; no LLM calls",
    )
    run.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 if any record is rejected",
    )
    run.add_argument(
        "--log-level",
        default=None,
        choices=("debug", "info", "warn", "error"),
        help="stderr log level (default: info)",
    )
    run.add_argument(
        "--console",
        default=None,
        choices=("auto", "rich", "plain"),
        help="progress face: live panel / v1.9 plain lines (default: auto)",
    )

    validate = sub.add_parser("validate", help="M1 full validation only (no run)")
    validate.add_argument("--config", required=True, help="path to config.toml")
    validate.add_argument("--project", required=True, help="path to project.toml")
    validate.add_argument(
        "--probe",
        action="store_true",
        help="also probe connectivity of every referenced profile",
    )
    validate.add_argument(
        "--console",
        default=None,
        choices=("auto", "rich", "plain"),
        help="progress face: live panel / v1.9 plain lines (default: auto)",
    )

    rubric = sub.add_parser("rubric", help="print / list the packaged default rubrics")
    rubric.add_argument(
        "--show",
        default=None,
        choices=sorted(_RUBRIC_FILES),
        help="print the named default rubric TOML verbatim to stdout",
    )
    return parser


def _overrides_from_args(args: argparse.Namespace) -> CliOverrides:
    """run-namespace → CliOverrides. The validate namespace lacks the run-only
    fields, so `_cmd_validate` builds its CliOverrides(console=...) inline."""
    return CliOverrides(
        input=args.input,
        output=args.output,
        limit=args.limit,
        dry_run=args.dry_run,
        strict=args.strict,
        log_level=args.log_level,
        console=args.console,
    )

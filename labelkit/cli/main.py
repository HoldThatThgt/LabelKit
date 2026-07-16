"""CLI process entry point and the sole exception-to-exit-code mapping."""
from __future__ import annotations

import sys

from labelkit.common.errors import (
    EXIT_CONFIG,
    EXIT_FATAL,
    EXIT_INPUT,
    EXIT_STRICT,
    CircuitBreakerTripped,
    ConfigError,
    InputError,
    LabelKitError,
    ProviderFatalError,
)

from .commands import _cmd_rubric, _cmd_run, _cmd_validate
from .parser import build_parser

_REPORT_WRITE_FAILED_MSG = "report write failed"

__all__ = ["exit_code_for", "main"]


def exit_code_for(exc: BaseException) -> int:
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
    if isinstance(exc, ConfigError):
        print(
            f"ConfigError: {len(exc.errors)} 个配置错误（全量聚合反馈）",
            file=sys.stderr,
        )
        for line in exc.errors:
            print(line, file=sys.stderr)
    else:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)


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
    except Exception as exc:
        _print_exception(exc)
        return exit_code_for(exc)


if __name__ == "__main__":
    sys.exit(main())

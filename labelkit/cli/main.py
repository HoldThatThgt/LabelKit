"""CLI 进程入口，以及唯一的「异常 → 退出码」映射点。"""
from __future__ import annotations

import logging
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

_logger = logging.getLogger("labelkit.cli")

__all__ = ["exit_code_for", "main"]


def exit_code_for(exc: BaseException) -> int:
    """按异常类型映射退出码（spec §2.4：唯一映射点）。

    @param exc 逃逸到 CLI 顶层的异常实例。
    @return 退出码：2 配置错误 / 3 输入错误 / 1 报告写失败 / 其余 4。
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
    """把异常打到 stderr：ConfigError 展开全量聚合反馈，其余单行。

    @param exc 待打印的异常实例。
    @return 无。
    """
    if isinstance(exc, ConfigError):
        print(
            f"ConfigError: {len(exc.errors)} config error(s) (all aggregated)",
            file=sys.stderr,
        )
        for line in exc.errors:
            print(line, file=sys.stderr)
    else:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口：解析参数、分派子命令、把逃逸异常翻译成退出码。

    @param argv 参数向量；None 表示取 sys.argv[1:]。
    @return 进程退出码（spec §2.4）。
    """
    args = build_parser().parse_args(argv)
    handlers = {"run": _cmd_run, "validate": _cmd_validate, "rubric": _cmd_rubric}
    try:
        return handlers[args.command](args)
    except LabelKitError as exc:
        code = exit_code_for(exc)
        _logger.error("command %s aborted: %s (exit %d)", args.command,
                      type(exc).__name__, code)
        _print_exception(exc)
        return code
    except KeyboardInterrupt:
        # Ctrl-C 语义不变：仍按 EXIT_FATAL 退出，仅补一条分类错误日志。
        _logger.error("command %s interrupted by user (exit %d)", args.command,
                      EXIT_FATAL)
        print("interrupted", file=sys.stderr)
        return EXIT_FATAL
    except Exception as exc:
        code = exit_code_for(exc)
        _logger.error("command %s failed: %s (exit %d)", args.command,
                      type(exc).__name__, code)
        _print_exception(exc)
        return code


if __name__ == "__main__":
    sys.exit(main())

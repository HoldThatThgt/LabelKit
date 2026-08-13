"""命令行参数解析与 CLI 覆盖项转换。"""
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
    """将 ``--limit`` 解析为 ≥ 1 的整数。

    @param value 命令行原始字符串。
    @return 解析出的正整数。
    @raises argparse.ArgumentTypeError 非整数或小于 1 时抛出（argparse 落到退出码 2）。
    """
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected an integer >= 1, got {value!r}"
        ) from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"expected an integer >= 1, got {number}")
    return number


def _add_run_parser(sub: argparse._SubParsersAction) -> None:
    """挂载 ``run`` 子命令及其全部开关（spec §2.4 CLI 表面）。

    @param sub build_parser 创建的子命令容器。
    @return 无。
    """
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


def _add_validate_parser(sub: argparse._SubParsersAction) -> None:
    """挂载 ``validate`` 子命令（仅 M1 全量校验，不运行流水线）。

    @param sub build_parser 创建的子命令容器。
    @return 无。
    """
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


def _add_rubric_parser(sub: argparse._SubParsersAction) -> None:
    """挂载 ``rubric`` 子命令（打印 / 列出随包发布的默认 rubric）。

    @param sub build_parser 创建的子命令容器。
    @return 无。
    """
    rubric = sub.add_parser("rubric", help="print / list the packaged default rubrics")
    rubric.add_argument(
        "--show",
        default=None,
        choices=sorted(_RUBRIC_FILES),
        help="print the named default rubric TOML verbatim to stdout",
    )


def build_parser() -> argparse.ArgumentParser:
    """构建 labelkit 顶层 argparse 解析器（run / validate / rubric 三子命令）。

    @return 已挂载全部子命令的解析器。
    """
    parser = argparse.ArgumentParser(
        prog="labelkit",
        description=(
            "LLM-powered stateless batch pipeline: segment / stitch / dedup / "
            "classify / extract / quality / generate / annotate / verify."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    _add_run_parser(sub)
    _add_validate_parser(sub)
    _add_rubric_parser(sub)
    return parser


def _overrides_from_args(args: argparse.Namespace) -> CliOverrides:
    """run 命名空间 → CliOverrides。

    validate 命名空间没有 run 专属字段，故 `_cmd_validate` 自行内联构造
    CliOverrides(console=...)。

    @param args run 子命令解析出的命名空间。
    @return 由命令行覆盖项组成的 CliOverrides。
    """
    return CliOverrides(
        input=args.input,
        output=args.output,
        limit=args.limit,
        dry_run=args.dry_run,
        strict=args.strict,
        log_level=args.log_level,
        console=args.console,
    )

"""run / validate / rubric 三个子命令的用户交互处理器。"""
from __future__ import annotations

import argparse
import logging
import sys
from importlib import resources

from labelkit.common.config.model import CliOverrides
from labelkit.common.errors import EXIT_OK
from labelkit.orchestration.application import (
    execute_run,
    probe_referenced_profiles,
    validate_project,
)

from .console import ConsoleRenderer
from .parser import _RUBRIC_FILES, _overrides_from_args

_logger = logging.getLogger("labelkit.cli")


def _cmd_run(args: argparse.Namespace) -> int:
    """执行 ``run`` 子命令：装配惰性壳渲染器并把控制权交给编排层。

    @param args run 子命令解析出的命名空间。
    @return 运行退出码（由 execute_run 决定）。
    """
    # v1.10（U19）：渲染器**恒**以惰性壳传入——它在 on_run_context 自配置
    # （rich → 面板；plain ∧ heartbeat>0 ∧ 非 TTY → 心跳；其余保持惰性，
    # 与 v1.9 逐字节等价）。
    renderer = ConsoleRenderer()
    return execute_run(args.config, args.project, _overrides_from_args(args),
                       listener=renderer)


def _cmd_validate(args: argparse.Namespace) -> int:
    """执行 ``validate`` 子命令：M1 全量校验，可选逐 profile 连通性探测。

    @param args validate 子命令解析出的命名空间。
    @return 校验通过时的 EXIT_OK（失败以 ConfigError 抛出，由 main 映射退出码）。
    """
    # v1.10（U27）：--console 在 validate 路径同样抵达 M1（jsonl × 显式 rich
    # 的 WARN 也在这里触发）。validate 命名空间没有 run 专属字段，覆盖项内联构造。
    cfg = validate_project(args.config, args.project,
                           overrides=CliOverrides(console=args.console))
    print("configuration valid", file=sys.stderr)

    if args.probe:
        results = probe_referenced_profiles(cfg)
        # v1.10（U13/U27）：探测**表格**仅在 rich 档 且 stdout 为 TTY 时渲染——
        # 脚本消费方保持逐字节一致的行格式（stdout 通道职责不变）。
        if cfg.console.mode_resolved == "rich" and sys.stdout.isatty():
            if _print_probe_table(results):
                return EXIT_OK
        for result in results:
            identity = f"{result.kind}.{result.profile}"
            label = f"{identity}[{result.key_env}]" if result.key_env else identity
            if result.ok:
                print(
                    f"probe {label}: ok model={result.model} "
                    f"latency_ms={result.latency_ms}"
                )
            else:
                print(f"probe {label}: FAIL {result.error}")
    return EXIT_OK


def _print_probe_table(results) -> bool:
    """把探测结果渲染成 rich 表格输出到 stdout。

    字面文本一律以 ``Text`` 直传、着色改走显式 style：rich 的控制台 markup 会把
    ``[key]`` 这类小写方括号片段当成样式标签整段吃掉（rich 官方处置即「改用 Text
    实例」），而表头与 ``profile[key_env]`` 标签都带方括号——U13 要求表格与 plain
    行式逐项一致，被吃掉就是信息丢失。

    @param results probe_referenced_profiles 返回的探测结果序列。
    @return True 已渲染表格；False 表示 rich 实际不可导入（mode_resolved 只做过
        find_spec 探测，U21），调用方回落逐行 plain 输出。
    """
    try:
        from rich.console import Console
        from rich.table import Table
        from rich.text import Text
    except ImportError as exc:
        _logger.debug("cli: rich unavailable for the probe table: %s", exc)
        return False
    table = Table(title="validate --probe")
    for column in ("kind.profile[key]", "status", "model", "latency_ms", "error"):
        table.add_column(Text(column),
                         justify="right" if column == "latency_ms" else "left")
    for result in results:
        identity = f"{result.kind}.{result.profile}"
        label = f"{identity}[{result.key_env}]" if result.key_env else identity
        status = (Text("ok", style="green") if result.ok
                  else Text("FAIL", style="red"))
        table.add_row(Text(label), status, Text(result.model),
                      Text(str(result.latency_ms)), Text(result.error or ""))
    Console().print(table)
    return True


def _cmd_rubric(args: argparse.Namespace) -> int:
    """执行 ``rubric`` 子命令：列出或原样打印随包发布的默认 rubric。

    @param args rubric 子命令解析出的命名空间。
    @return EXIT_OK。
    """
    # `rubric --show` 的 stdout 面向机器消费——**恒** plain（U13）。
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

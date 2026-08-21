"""M1 配置装载器(spec 3.1, CONTRACTS.md §6.2/§6.3)。

``load()``: 三源合并——CLI 覆盖 > project.toml > config.toml/内建默认——外加**完整**的
启动期校验。每一条校验错误都聚合进同一个 ConfigError(绝不首错即抛); 未知键只出 stderr
警告(前向兼容), 唯二例外是 v1.7 的 ``[class.*]`` 覆盖命名空间与 v1.12 的
``[frame.class.*]`` 命名空间——它们由 M1 显名拥有, 白名单外的键/节是 CONFIG_ERROR(R25)。

``default_rubric()``: 从 labelkit/data/rubrics/ 装载一份打包默认准则。

错误消息格式(spec 3.1.5): ``"<file>:[section].key: <expected>, got <actual>"``, 其中
``"<file>:[section].key:"`` 是机器稳定的定位前缀; 表数组元素以 ``"[[section.key]][N]"``
定位, N 从 1 起。

各节解析在 ``_sections``, Schema 与 few-shot 干跑在 ``_schemas``, 准则解析在
``_rubrics``, 按类视图在 ``_classviews``, 跨节组合约束在 ``_constraints``——本文件只保留
公开入口、控制台模式裁定与 ResolvedConfig 装配。
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from labelkit.common.config._collect import _Collector, _flush_warnings
from labelkit.common.config._constraints import _LoadCtx, _Products, validate
from labelkit.common.config._rubrics import default_rubric
from labelkit.common.config._sections import (
    _TRACE_CHANNELS,        # 追踪通道枚举: 实现落在 _sections, 公开导入面留在本模块
    _auto_console_mode,     # v1.10 auto 决策链: 同上, 公开导入面留在本模块
    _parse_config_file,
    _parse_project_file,
    _ToolSide,
)
from labelkit.common.config.model import (
    CliOverrides,
    ConsoleConfig,
    ResolvedConfig,
    ResolvedPaths,
    RunConfig,
    ToolConfig,
)
from labelkit.common.errors import ConfigError
from labelkit.common.extensions.hooks import ValidationHooks

__all__ = ["load", "default_rubric"]

# v1.10 (spec 3.1.4 console 行 / §7.7): rich 可导入性探针**只**用 find_spec——装载器
# 从不真正 import rich(惰性导入是 CLI 层的事, U4/U21)。模块级别名以便离线测试注入探针。
_find_spec = importlib.util.find_spec


@dataclass(frozen=True)
class _ConsoleVerdict:
    """v1.10 控制台模式的两个裁定值(spec 3.1.4 console 行 / §7.7, U21/U25)。"""

    effective_mode: str   # CLI > config 合并后的用户意图("auto" | "rich" | "plain")
    mode_resolved: str    # M1 冻结的最终裁定("rich" | "plain")


def _read_toml(col: _Collector, path: Path, label: str) -> tuple[bytes | None, dict | None]:
    """读一份 TOML 文件(尽力而为: 读失败与解析失败都只记账, 不抛)。

    @param col 错误聚合器
    @param path 文件路径
    @param label 报错定位用的路径字符串
    @return (原始字节, 解析后的字典); 任一环节失败时对应位置为 None
    """
    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        col.error(f"{label}: cannot read config file: {e}")
        return None, None
    try:
        return raw, tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        col.error(f"{label}: TOML parse failed: {e}")
        return raw, None


def _resolve_console(ctx: _LoadCtx, head: _ToolSide) -> _ConsoleVerdict:
    """v1.10 控制台: CLI 优先级 + ``mode_resolved`` 冻结(spec 3.1.4 console 行 / §7.7)。

    ``--console`` 的取值已由 argparse choices 预校验; "显式 rich" = CLI --console rich
    或 TOML 里字面写了 ``[console].mode = "rich"``。

    @param ctx 校验上下文
    @param head config.toml 一侧的解析产物
    @return 控制台两个裁定值
    """
    effective_mode = ctx.cli.console if ctx.cli.console is not None else head.console.mode
    explicit_rich = ctx.cli.console == "rich" or head.console_rich_explicit
    if head.tool.log_format == "jsonl":
        # §7.7 铁律: stderr 逐行可 json.loads——jsonl 强制 plain 且**不可**被显式 rich
        # 覆盖; 显式冲突恰好告警一次。
        if explicit_rich:
            ctx.col.warn('console: log_format="jsonl" forces plain - an explicit rich has '
                         "no effect (the line-parseable stderr invariant, 7.7)")
        return _ConsoleVerdict(effective_mode=effective_mode, mode_resolved="plain")
    if effective_mode == "plain":
        return _ConsoleVerdict(effective_mode=effective_mode, mode_resolved="plain")
    if effective_mode == "rich":
        # 显式 rich 即便没有 TTY 也照办(CI 录制 ANSI 的场景, §7.7)——只有可导入性能降级它。
        if _find_spec("rich") is not None:
            return _ConsoleVerdict(effective_mode=effective_mode, mode_resolved="rich")
        ctx.col.warn("console: rich is not importable, demoted to plain")
        return _ConsoleVerdict(effective_mode=effective_mode, mode_resolved="plain")
    # auto —— §7.7 的决策链, 全部走终端能力探针。v1.17 secret-free（SPEC-SP §5.2）:
    # TERM 的非秘密读取改用 membership/indexing——不得经 os.environ.get 成为偷读
    # credential 的旁路（validate/dry-run 的静态 face 全程零 value reader）。
    resolved = _auto_console_mode(
        isatty=sys.stderr.isatty(),
        log_format=head.tool.log_format,
        term=(os.environ["TERM"] if "TERM" in os.environ else None),
        rich_importable=_find_spec("rich") is not None,
    )
    return _ConsoleVerdict(effective_mode=effective_mode, mode_resolved=resolved)


def _assemble_run(ctx: _LoadCtx, products: _Products) -> RunConfig:
    """装配 ``RunConfig``(CLI 覆盖已生效; generate_only 下输入恒为 None)。

    @param ctx 校验上下文
    @param products 产物累加器
    @return ``RunConfig``
    """
    run = ctx.p.run
    return RunConfig(
        output=products.eff_output,
        modality=ctx.modality,      # type: ignore[arg-type]
        input=None if ctx.mode == "generate_only" else products.eff_input,
        mode=ctx.mode,              # type: ignore[arg-type]
        batch_size=run["batch_size"],
        seed=run["seed"],
        fatal_error_threshold=run["fatal_error_threshold"],
        max_park_s=run["max_park_s"],
    )


def _assemble_console(head: _ToolSide, verdict: _ConsoleVerdict) -> ConsoleConfig:
    """装配 ``ConsoleConfig``(CLI > config 的 mode 与 M1 冻结的 mode_resolved)。

    @param head config.toml 一侧的解析产物
    @param verdict 控制台两个裁定值
    @return ``ConsoleConfig``
    """
    return replace(
        head.console,
        mode=verdict.effective_mode,          # type: ignore[arg-type] # CLI > config (2.5)
        mode_resolved=verdict.mode_resolved,  # type: ignore[arg-type] # 冻结裁定 (U21)
    )


def _trace_path(ctx: _LoadCtx, products: _Products) -> str:
    """未显式声明 trace.path 时, 由输出路径推出默认追踪文件名。

    v1.17: 显式声明的相对路径已在解析期按 project root 绝对化; 默认名由**绝对**
    生效输出派生, 故返回值恒为绝对路径。

    @param ctx 校验上下文
    @param products 产物累加器
    @return 追踪文件路径
    """
    path = ctx.p.trace.path
    if not path and products.eff_output:
        return str(Path(products.eff_output).with_suffix("")) + ".trace.jsonl"
    return path


def _resolved_paths(ctx: _LoadCtx, products: _Products) -> ResolvedPaths:
    """v1.17(SPEC-SP §5.1): 一次性派生全部绝对路径(未启用通道为 None)。

    live report 固定 ``<stem>.report.json``, dry-run report 固定
    ``<stem>.dryrun.report.json``——由 M1 写入 ``ResolvedPaths.report``; rejects/
    sidecar/trace/stream artifact 也只在 M1 派生一次。派生公式与 M11 落盘面一致。

    @param ctx 校验上下文(其 ``root`` = project root)
    @param products 产物累加器(生效输入/输出已绝对化)
    @return 冻结的 ``ResolvedPaths``
    """
    p = ctx.p
    stem = str(Path(products.eff_output).with_suffix(""))
    suffix = ".dryrun.report.json" if ctx.cli.dry_run else ".report.json"
    return ResolvedPaths(
        project=str(Path(ctx.fp).resolve()),
        project_root=str(ctx.root),
        input=None if ctx.mode == "generate_only" else products.eff_input,
        output=products.eff_output,
        report=stem + suffix,
        rejects=None if p.output.rejects == "none" else stem + ".rejects.jsonl",
        sidecar=stem + ".meta.jsonl" if p.output.meta_mode == "sidecar" else None,
        trace=_trace_path(ctx, products) if p.trace.enabled else None,
        stream_artifact=stem + ".stream.jsonl" if p.generate_stream.enabled else None,
    )


def _log_paths(paths: ResolvedPaths) -> None:
    """启动 INFO: 打印一次 output/report 及实际启用的 side channel 绝对路径。

    装载期 M12 日志尚未配置(与 ``_flush_warnings`` 同款处境), 故直接写 stderr;
    刻意不使用 ``dry-run`` 前缀——七个 plain golden 按行前缀 ``dry-run`` 比对,
    新增前缀行会砸 golden; 也刻意不走 logging(经已关闭的 handler 发射会把调用方
    源码行以 Logging-error 回溯泄进 stderr)。

    @param paths 已冻结的绝对路径载体
    """
    sides = [f"{name}={value}" for name, value in (
        ("rejects", paths.rejects), ("sidecar", paths.sidecar),
        ("trace", paths.trace), ("stream_artifact", paths.stream_artifact)) if value]
    suffix = (" " + " ".join(sides)) if sides else ""
    print(f"run paths: output={paths.output} report={paths.report}{suffix}",
          file=sys.stderr)


def _validation_hooks(products: _Products) -> ValidationHooks:
    """把 M1 解析期冻结的四个钩子载体装配成 ``ValidationHooks``。

    @param products 产物累加器(其 ``hooks`` 键 = output/sample/sequence/scenario)
    @return 冻结的 ``ValidationHooks``(未声明/未通过双检的键为 None)
    """
    return ValidationHooks(
        output=products.hooks.get("output"),
        sample=products.hooks.get("sample"),
        sequence=products.hooks.get("sequence"),
        scenario=products.hooks.get("scenario"),
    )


def _assemble(ctx: _LoadCtx, head: _ToolSide, products: _Products,
              verdict: _ConsoleVerdict, digests: tuple[bytes | None, bytes | None]
              ) -> ResolvedConfig:
    """装配冻结的 ``ResolvedConfig``。

    @param ctx 校验上下文(其 profile 表已回填密钥, vision 产物已冻结)
    @param head config.toml 一侧的解析产物
    @param products 校验阶段的产物累加器
    @param verdict 控制台两个裁定值
    @param digests (config.toml 原始字节, project.toml 原始字节)
    @return ``ResolvedConfig``
    """
    p, cli = ctx.p, ctx.cli
    config_raw, project_raw = digests
    paths = _resolved_paths(ctx, products)
    return ResolvedConfig(
        tool=ToolConfig(
            log_level=cli.log_level if cli.log_level is not None else head.tool.log_level,
            log_format=head.tool.log_format,
        ),
        console=_assemble_console(head, verdict),
        llm_profiles=ctx.llm_profiles,
        embedding_profiles=ctx.embedding_profiles,
        run=_assemble_run(ctx, products),
        input=p.input, stream=p.stream, dedup=p.dedup, segment=p.segment,
        stitch=p.stitch, extract=p.extract,
        classify=p.classify,             # 启用时 max_labels 已回填
        quality=replace(p.quality, rubric=products.selector),
        generate=p.generate, annotate=p.annotate, verify=p.verify, output=p.output,
        trace=replace(p.trace, path=_trace_path(ctx, products)),
        rubric=products.rubric,
        class_views=products.class_views,
        user_schema=products.user_schema,
        frame_classify=p.frame_classify,   # v1.12; vision_resolved 已冻结
        frame_annotate=p.frame_annotate,
        frame_class_views=products.frame_class_views,
        frame_schema=products.frame_schema,
        generate_stream=p.generate_stream,   # v1.13
        limit=cli.limit, strict=cli.strict, dry_run=cli.dry_run,
        config_path=ctx.fc, project_path=ctx.fp,
        config_digest="sha256:" + hashlib.sha256(config_raw or b"").hexdigest(),
        project_digest="sha256:" + hashlib.sha256(project_raw or b"").hexdigest(),
        paths=paths,
        validation_hooks=_validation_hooks(products),
        scenario_plan=products.scenario_plan,
    )


def load(config_path: Path, project_path: Path,
         cli_overrides: CliOverrides) -> ResolvedConfig:
    """三源合并 + 完整校验(M1 的唯一公开入口)。

    @param config_path config.toml 路径
    @param project_path project.toml 路径
    @param cli_overrides CLI 覆盖值(优先级最高)
    @return 冻结的 ``ResolvedConfig``
    @raises ConfigError 携带**全部**错误(绝非首错即抛); CLI 据此退出码 2
    """
    col = _Collector()
    fc, fp = str(config_path), str(project_path)
    config_raw, config_data = _read_toml(col, Path(config_path), fc)
    project_raw, project_data = _read_toml(col, Path(project_path), fp)
    # v1.17(SPEC-SP §5.1): project TOML 读取成功后立即冻结 project root——project
    # 侧全部相对路径(输入/输出/四类 schema_path/trace.path/四个钩子文件)的解析基点。
    project_root = Path(fp).resolve().parent
    head = (_parse_config_file(col, fc, config_data) if config_data is not None
            else _ToolSide(tool=ToolConfig(), console=ConsoleConfig(),
                           console_rich_explicit=False, llm_profiles={},
                           embedding_profiles={}))
    project = (_parse_project_file(col, fp, project_data, project_root)
               if project_data is not None else None)
    if project is None:
        _flush_warnings(col)
        raise ConfigError(col.errors or [f"{fp}: config load failed"])
    ctx = _LoadCtx(col=col, fc=fc, fp=fp, cli=cli_overrides,
                   config_ok=config_data is not None, llm_profiles=head.llm_profiles,
                   embedding_profiles=head.embedding_profiles, p=project,
                   modality=project.run["modality"] or "text",
                   mode=project.run["mode"] or "process", root=project_root)
    products = _Products()
    ctx = validate(ctx, products)
    verdict = _resolve_console(ctx, head)
    _flush_warnings(col)
    if col.errors:
        raise ConfigError(col.errors)
    cfg = _assemble(ctx, head, products, verdict, (config_raw, project_raw))
    _log_paths(cfg.paths)
    return cfg

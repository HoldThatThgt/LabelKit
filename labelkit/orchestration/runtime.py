"""run 与 validate 两条命令的运行期对象图装配。

本模块只管装配：不解析 argparse 命名空间、不打印面向用户的文本、不把异常映射为退出码、
也不实现任何算子行为。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from labelkit.common.config import load
from labelkit.common.config.model import CliOverrides, ResolvedConfig
from labelkit.common.observability.obslog import EventLog, MetricsSink, setup_logging
from labelkit.common.runtime.credentials import (
    RuntimeCredentials,
    referenced_profiles,
    resolve_credentials,
)
from labelkit.common.runtime.llm_client import LLMClient
from labelkit.common.runtime.schema_engine import SchemaEngine
from labelkit.orchestration.factory import build_stages
from labelkit.orchestration.orchestrator import Orchestrator, RunServices
from labelkit.operators.emitter import Emitter

if TYPE_CHECKING:
    from labelkit.common.observability.obslog import ProgressListener
    from labelkit.common.runtime.llm_client import ProbeResult
    from labelkit.common.config.model import TraceConfig
    from labelkit.operators.ingest import Ingestor

__all__ = ["execute_run", "probe_referenced_profiles", "validate_project"]

_log = logging.getLogger("labelkit.runtime")


def _activate_listener(listener: "ProgressListener", cfg: ResolvedConfig,
                       llm: LLMClient, metrics: MetricsSink) -> None:
    """v1.10（U19 调用时序）：把懒壳渲染器激活恰一次——在对象图装配完成之后、
    ``asyncio.run`` 之前，交给它 ResolvedConfig 与渲染 tick 用的三个只读拉取闭包
    （``LLMClient.snapshot``、MetricsSink 计数器、熔断连击数）。

    U23 的失败纪律：一条 WARN，然后把汇上的 listener 引用整轮置 None（``_listener``
    是既定的 Wave-1 存放位——``MetricsSink._forward`` 在自己转发失败时清的也是同一属性）。
    渲染器的 bug 永远影响不到退出码与输出。

    @param listener: 控制台面板的进度监听器
    @param cfg: 已解析配置
    @param llm: M9 LLM 客户端（提供 snapshot 拉取面）
    @param metrics: M12 计数与事件汇
    """
    try:
        listener.on_run_context(cfg, llm.snapshot,
                                lambda: dict(metrics.counters),
                                lambda: metrics.fatal_streak)
    except Exception as exc:  # noqa: BLE001 — bypass isolation (U7/U23)
        metrics._listener = None
        _log.warning("console listener failed, panel bypass disabled: %s", exc,
                     extra={"stage": "run", "batch": 0})


def _trace_config(cfg: ResolvedConfig) -> "TraceConfig":
    """取本轮实际使用的 trace 配置。

    v1.17（Wave 2b）：路径改从 ``cfg.paths.trace`` 消费——装载期一次派生的绝对
    路径（显式相对路径按 project root 绝对化），不再从字符串按 cwd 推导。
    dry-run 时仍改道到 ``<name>.dryrun<suffix>``（P2-4——空跑绝不覆盖上一轮的
    trace 文件）。

    @param cfg 已解析配置
    @return trace 配置（通道未启用时原样返回）
    """
    trace_cfg = cfg.trace
    paths = getattr(cfg, "paths", None)
    resolved = paths.trace if paths is not None else None
    if not (trace_cfg.enabled and resolved):
        return trace_cfg
    path = Path(resolved)
    if cfg.dry_run:
        path = path.with_name(path.stem + ".dryrun" + path.suffix)
    return replace(trace_cfg, path=str(path))


def _l25_validator(cfg: ResolvedConfig):
    """从 M1 冻结载体取 ``output.validator`` 的 L2.5 冻结 callable。

    v1.17（Wave 2b）：schema engine 的 L2.5 腿载体化——装配方在此读
    ``ResolvedConfig.validation_hooks.output.target`` 传入，引擎内不再按
    字符串二次 resolve（CONTRACTS §7.19.3 / rule 70）。

    @param cfg 已解析配置
    @return 冻结 callable；未配置 output.validator 时为 None
    """
    hooks = getattr(cfg, "validation_hooks", None)
    if hooks is not None and hooks.output is not None:
        return hooks.output.target
    return None


def _run_credentials(cfg: ResolvedConfig) -> RuntimeCredentials:
    """按命令分流物化凭据（SPEC-SP §5.2 命令路径 mermaid）。

    ``run``（真实网络）聚合解析所有被引用 profile 的密钥值，任一缺失即
    ConfigError（exit 2 面）；``run --dry-run`` 是纯静态面——传**空**凭据对象，
    零环境变量 value 读取、零密钥驻留（若空跑意外派发，池物化会 fail-closed）。

    @param cfg 已解析配置
    @return 运行期凭据
    @raises ConfigError 真实运行且任一被引用密钥缺失（聚合全部缺失项）
    """
    if cfg.dry_run:
        return RuntimeCredentials(llm={}, embedding={})
    return resolve_credentials(cfg)


def _build_ingestor(cfg: ResolvedConfig, metrics: MetricsSink) -> "Ingestor | None":
    """构造 M2 摄取器并接上 trace 通路；generate_only 模式没有摄取器。

    @param cfg: 已解析配置
    @param metrics: M12 计数与事件汇
    @return: 摄取器实例；generate_only 模式返回 None
    """
    if cfg.run.mode != "process":
        return None
    from labelkit.operators.ingest import Ingestor

    ingestor = Ingestor(cfg)
    ingestor.metrics = metrics
    return ingestor


def execute_run(
    config_path: str | Path,
    project_path: str | Path,
    overrides: CliOverrides,
    listener: "ProgressListener | None" = None,
) -> int:
    """加载配置、装配运行期对象图、执行一轮运行。

    v1.10（U19）：``listener`` 是控制台面板的进程内旁路——构造 MetricsSink 时接入，事件
    循环启动前经 ``on_run_context`` 激活；传 None（v1.10 之前的全部调用方）与 v1.9 逐字节
    一致。

    @param config_path: 工具级 config.toml 路径
    @param project_path: 工程级 project.toml 路径
    @param overrides: CLI 覆盖项
    @param listener: 控制台面板进度监听器；None 表示不挂面板
    @return: 进程退出码
    """
    cfg = load(Path(config_path), Path(project_path), overrides)
    setup_logging(cfg)
    run_id = secrets.token_hex(6)
    run_started_at = datetime.now().astimezone()

    event_log = EventLog(_trace_config(cfg), run_id)
    metrics = MetricsSink(cfg, run_id, event_log, listener=listener)
    llm = LLMClient(cfg.llm_profiles, cfg.embedding_profiles,
                    _run_credentials(cfg), metrics)
    schema_engine = SchemaEngine(dict(cfg.user_schema), llm, cfg.output, metrics,
                                 validator=_l25_validator(cfg))
    services = RunServices(llm=llm, schema_engine=schema_engine, metrics=metrics,
                           run_id=run_id, run_started_at=run_started_at)
    orchestrator = Orchestrator(
        cfg,
        build_stages(cfg),
        _build_ingestor(cfg, metrics),
        Emitter(cfg, schema_engine, run_id, run_started_at),
        services,
    )
    if listener is not None:
        _activate_listener(listener, cfg, llm, metrics)
    try:
        summary = asyncio.run(orchestrator.run())
    finally:
        event_log.close()
    return summary.exit_code


def validate_project(
    config_path: str | Path,
    project_path: str | Path,
    overrides: CliOverrides = CliOverrides(),
) -> ResolvedConfig:
    """加载并完整校验一对工具/工程配置。

    v1.10（U27）：CLI 把它解析出的覆盖项一并传进来，好让 ``--console`` 在 validate 路径上
    也抵达 M1（jsonl × 显式 rich 的 WARN 在这里同样会响）；既有调用方保持零覆盖的默认值。

    @param config_path: 工具级 config.toml 路径
    @param project_path: 工程级 project.toml 路径
    @param overrides: CLI 覆盖项
    @return: 已解析配置
    """
    return load(Path(config_path), Path(project_path), overrides)


def probe_referenced_profiles(cfg: ResolvedConfig) -> tuple["ProbeResult", ...]:
    """探测被启用阶段实际引用的每一个 profile。

    v1.17（Wave 2b，SPEC-SP §5.2）：``validate --probe`` 属真实网络路径——探测前
    先对所有被引用 profile 聚合解析密钥值，任一缺失即 ConfigError（exit 2 面，
    绝不拿空密钥去撞端点）。

    @param cfg 已解析配置
    @return 按 (LLM, embedding) 顺序排布的探测结果元组
    @raises ConfigError 任一被引用密钥缺失（聚合全部缺失项）
    """
    llm_names, emb_names = referenced_profiles(cfg)
    client = LLMClient(cfg.llm_profiles, cfg.embedding_profiles,
                       resolve_credentials(cfg), None)

    async def _probe_all() -> list[ProbeResult]:
        """依次探测全部被引用 profile。

        @return: 探测结果列表
        """
        results: list[ProbeResult] = []
        for name in (*llm_names, *emb_names):
            results.extend(await client.probe_all(name))
        return results

    return tuple(asyncio.run(_probe_all()))

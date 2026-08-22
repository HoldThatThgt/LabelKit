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
from typing import TYPE_CHECKING, Awaitable, Callable, TypeVar, cast

import httpx

from labelkit.common.config import load
from labelkit.common.config.model import CliOverrides, ResolvedConfig
from labelkit.common.errors import InternalError, LabelKitError
from labelkit.common.observability.obslog import EventLog, MetricsSink, setup_logging
from labelkit.common.inference.credentials import (
    RuntimeCredentials,
    referenced_profiles,
    resolve_credentials,
)
from labelkit.common.inference.llm_client import LLMClient
from labelkit.common.inference.schema_engine import SchemaEngine
from labelkit.orchestration.factory import build_stages
from labelkit.orchestration.process_workflow import ProcessWorkflow, RunServices
from labelkit.operators.emitter import Emitter
from labelkit.runtime import ExecutionRuntime, ResourceManager

if TYPE_CHECKING:
    from labelkit.common.observability.obslog import ProgressListener
    from labelkit.common.inference.llm_client import ProbeResult
    from labelkit.common.config.model import TraceConfig
    from labelkit.common.contracts.generation import GenerationProgram, ScenarioPlan
    from labelkit.operators.ingest import Ingestor

__all__ = ["execute_run", "probe_referenced_profiles", "validate_project"]

_log = logging.getLogger("labelkit.application")
_T = TypeVar("_T")
_MISSING = object()


class _SequencePlanFailure(Exception):
    """保留已编译 program，使 live run 能写无内容 plan failed report。"""

    def __init__(self, program: "GenerationProgram", cause: LabelKitError):
        """保存 planner 失败边界。

        @param program 已成功编译的程序。
        @param cause planner 原始冻结异常。
        """
        super().__init__(type(cause).__name__)
        self.program = program
        self.cause = cause


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


def _compile_sequence_plan(
    cfg: ResolvedConfig,
) -> "tuple[GenerationProgram, ScenarioPlan] | None":
    """在凭据、EventLog 与输出通道之前运行唯一 sequence compiler/planner。

    @param cfg 已解析配置。
    @return sequence 形态返回唯一 program/plan，flat 返回 None。
    """
    if cfg.generate.form != "sequence":
        return None
    from labelkit.operators.generation.planner import compile_scenario_plan
    from labelkit.operators.generation.program import compile_generation_program

    program = compile_generation_program(cfg)
    try:
        plan = compile_scenario_plan(program)
    except LabelKitError as exc:
        raise _SequencePlanFailure(program, exc) from exc
    return program, plan


def _sequence_plan_for_run(
    cfg: ResolvedConfig,
) -> "tuple[GenerationProgram, ScenarioPlan] | None":
    """编译 live/dry-run 计划并仅在 live planner 失败时写诊断。

    @param cfg 已通过 M1 的配置。
    @return sequence program/plan；flat 返回 None。
    """
    try:
        return _compile_sequence_plan(cfg)
    except _SequencePlanFailure as failure:
        if not cfg.dry_run:
            from labelkit.orchestration.sequence_workflow import _write_plan_failed_report

            _write_plan_failed_report(cfg, failure.program, failure.cause)
        raise failure.cause from failure


def _runtime_run_id(
    cfg: ResolvedConfig,
    sequence_plan: "tuple[GenerationProgram, ScenarioPlan] | None",
) -> str:
    """为普通运行生成临时 ID，为 sequence 派生冻结 run ID。

    @param cfg 已解析配置。
    @param sequence_plan 可选的唯一 sequence program/plan。
    @return 本次 EventLog、MetricsSink 与交付共用 run ID。
    """
    if sequence_plan is None:
        return secrets.token_hex(6)
    from labelkit.operators.generation.project import derive_generation_id

    program, plan = sequence_plan
    attempt_id = derive_generation_id("run_attempt_id", [program.digest, program.planner_seed])
    return derive_generation_id("run_id", [attempt_id, plan.digest])


def _normalize_origin(base_url: str) -> tuple[str, str, int]:
    """使用 HTTPX 规则冻结一个 profile base URL 的 origin。

    @param base_url 已通过配置解析的端点根地址
    @return 小写 scheme、IDNA host 与有效端口
    @raises InternalError URL 不含受支持的 HTTP origin
    """
    try:
        url = httpx.URL(base_url)
        scheme = url.scheme.lower()
        host = url.raw_host.decode("ascii").lower()
    except (AttributeError, UnicodeError, httpx.InvalidURL) as exc:
        _log.error("invalid profile HTTP origin")
        raise InternalError("invalid profile HTTP origin") from exc
    if scheme not in {"http", "https"} or not host:
        _log.error("invalid profile HTTP origin")
        raise InternalError("invalid profile HTTP origin")
    port = url.port if url.port is not None else (443 if scheme == "https" else 80)
    return scheme, host, port


def _resource_maps(cfg: ResolvedConfig) -> tuple[dict, dict]:
    """收集本轮唯一引用的逻辑容量与规范化 origin。

    @param cfg 冻结运行配置
    @return capacities 与 origins 映射
    @raises InternalError 引用的 profile 不存在
    """
    llm_names, embedding_names = referenced_profiles(cfg)
    capacities: dict = {}
    origins: dict = {}
    groups = (("llm", llm_names, cfg.llm_profiles),
              ("embedding", embedding_names, cfg.embedding_profiles))
    for kind, names, profiles in groups:
        for name in names:
            profile = profiles.get(name)
            if profile is None:
                _log.error("referenced runtime profile is missing")
                raise InternalError("referenced runtime profile is missing")
            key = (kind, name)
            capacities[key] = profile.max_concurrency
            origins[key] = _normalize_origin(profile.base_url)
    return capacities, origins


def _build_resources(cfg: ResolvedConfig, metrics: MetricsSink | None) -> ResourceManager:
    """按冻结引用集构造唯一 ResourceManager。

    @param cfg 冻结运行配置
    @param metrics 可选运行观测汇
    @return 本轮资源所有者
    """
    capacities, origins = _resource_maps(cfg)
    return ResourceManager(capacities, origins, metrics)


async def _run_and_close(client: LLMClient, workflow: Callable[[], Awaitable[_T]],
                         runtime: ExecutionRuntime | None) -> _T:
    """在同一事件循环运行工作流并保持主异常高于关闭失败。

    @param client Application 拥有的根 LLMClient
    @param workflow live 或静态工作流
    @param runtime live 时的唯一 execution runtime；静态路径为 None
    @return 工作流结果
    """
    result: object = _MISSING
    primary: BaseException | None = None
    try:
        result = await (runtime.run(workflow) if runtime is not None else workflow())
    except BaseException as exc:
        primary = exc
    try:
        await client.aclose()
    except Exception as exc:  # 关闭失败不能覆盖既有主异常或 cancellation
        if primary is None:
            _log.error("LLM client close failed")
            raise InternalError("LLM client close failed") from exc
        _log.error("LLM client close failed while preserving primary error: %s", type(exc).__name__)
    if primary is not None:
        raise primary
    if result is _MISSING:
        _log.error("application workflow ended without a result")
        raise InternalError("application workflow ended without a result")
    return cast(_T, result)


async def _close_after_setup_failure(client: LLMClient, primary: BaseException) -> None:
    """对象图装配失败时关闭根客户端，且不覆盖原异常。

    @param client Application 已取得所有权的根客户端
    @param primary 对象图装配原异常
    @return None
    """
    try:
        await client.aclose()
    except Exception as exc:  # 关闭失败不能覆盖装配异常
        _log.error("LLM client close failed while preserving setup error: %s", type(exc).__name__)
    raise primary


def _compose_process_workflow(
    cfg: ResolvedConfig,
    sequence_plan: "tuple[GenerationProgram, ScenarioPlan] | None",
    services: RunServices,
    schema_engine: SchemaEngine,
    listener: "ProgressListener | None",
) -> ProcessWorkflow:
    """装配普通与序列运行共用的唯一工作流。

    @param cfg 冻结运行配置
    @param sequence_plan 可选序列程序与计划
    @param services 本轮共享服务
    @param schema_engine 唯一 Schema 引擎
    @param listener 可选进度监听器
    @return 已绑定计划与监听器的工作流
    """
    workflow = ProcessWorkflow(
        cfg, build_stages(cfg),
        _build_ingestor(cfg, services.metrics),
        Emitter(cfg, schema_engine, services.run_id, services.run_started_at),
        services,
    )
    if sequence_plan is not None:
        workflow._bind_sequence_plan(*sequence_plan)
    if listener is not None:
        _activate_listener(listener, cfg, services.llm, services.metrics)
    return workflow


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
    sequence_plan = _sequence_plan_for_run(cfg)
    setup_logging(cfg)
    run_id = _runtime_run_id(cfg, sequence_plan)
    run_started_at = datetime.now().astimezone()

    event_log = EventLog(_trace_config(cfg), run_id)
    try:
        metrics = MetricsSink(cfg, run_id, event_log, listener=listener)
        resources = _build_resources(cfg, metrics)
        runtime = ExecutionRuntime(resources, metrics)
        llm = LLMClient(cfg.llm_profiles, cfg.embedding_profiles,
                        _run_credentials(cfg), resources, metrics)
        try:
            schema_engine = SchemaEngine(dict(cfg.user_schema), llm, cfg.output, metrics,
                                         validator=_l25_validator(cfg))
            services = RunServices(
                llm=llm, schema_engine=schema_engine, metrics=metrics, tasks=runtime,
                run_id=run_id, run_started_at=run_started_at,
            )
            process_workflow = _compose_process_workflow(
                cfg, sequence_plan, services, schema_engine, listener,
            )
        except BaseException as primary:
            asyncio.run(_close_after_setup_failure(llm, primary))
        active_runtime = None if cfg.dry_run else runtime
        summary = asyncio.run(_run_and_close(llm, process_workflow.run, active_runtime))
        return summary.exit_code
    finally:
        event_log.close()


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
    cfg = load(Path(config_path), Path(project_path), overrides)
    try:
        _compile_sequence_plan(cfg)
    except _SequencePlanFailure as failure:
        raise failure.cause from failure
    return cfg


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
    resources = _build_resources(cfg, None)
    client = LLMClient(cfg.llm_profiles, cfg.embedding_profiles,
                       resolve_credentials(cfg), resources, None)

    async def _probe_all() -> tuple[ProbeResult, ...]:
        """依次探测全部被引用 profile。

        @return 探测结果 tuple
        """
        results: list[ProbeResult] = []
        for name in llm_names:
            results.extend(await client.probe_all(("llm", name)))
        for name in emb_names:
            results.extend(await client.probe_all(("embedding", name)))
        return tuple(results)

    return asyncio.run(_run_and_close(client, _probe_all, None))

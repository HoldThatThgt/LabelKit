"""v1.19 sequence 有界并发准备与声明序精确交付控制器。"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import datetime
from itertools import combinations
from typing import TYPE_CHECKING, Any, Mapping, NoReturn

from labelkit import __version__
from labelkit.common.contracts.generation import (
    AttemptTransaction,
    DedupGroupRequest,
    DedupReservation,
    DeliveryRequest,
    DeliveryServices,
    DownstreamAttemptRequest,
    GenerationProduct,
    NoiseCandidateReconcileRequest,
    NoiseEvaluationRequest,
    NoiseProjectionRequest,
    NoiseRenderRequest,
    PreparedCandidate,
    PreparedNoiseCandidate,
    PrimaryCandidateReconcileRequest,
    ProjectionRequest,
    ReconcileRequest,
    ReplayProjectionRequest,
    SequenceAssemblyRequest,
    SequenceRows,
)
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import Classification, DedupInfo, PipelineItem
from labelkit.common.errors import (
    CircuitBreakerTripped,
    ConfigError,
    ContextOverflowError,
    DeliveryError,
    GenerationProjectionMismatch,
    InternalError,
    LabelKitError,
    OutputTruncatedError,
    ProviderFatalError,
    ProviderRetryableError,
)
from labelkit.operators.dedup import DedupGroupRejected
from labelkit.operators.generation import GenerationAttemptRejected
from labelkit.operators.generation.flat import SimilarityFilter
from labelkit.operators.generation.project import canonical_delivery_row, canonical_json

if TYPE_CHECKING:
    from labelkit.common.contracts.generation import (
        DeliverySlot,
        ProjectedSequence,
        ProjectionWitness,
        ReplayRows,
    )


_log = logging.getLogger("labelkit.sequence_workflow")

_SEQUENCE_CALL_KEYS = (
    "scenario_seed_calls",
    "baseline_event_plan_calls",
    "variant_event_plan_calls",
    "frame_render_calls",
    "semantic_evaluation_calls",
    "noise_render_calls",
    "noise_evaluation_calls",
)

_ESTIMATE_CALL_ORDER = (
    "generate_calls",
    "segment_calls",
    "stitch_calls",
    "classify_calls",
    "frame_classify_calls",
    "extract_calls",
    "quality_calls",
    "annotate_calls",
    "frame_annotate_calls",
    "verify_calls",
)

_REJECTION_KEYS = (
    "scenario_schema",
    "event_schema",
    "post_validator_invalid",
    "post_validator_exception",
    "state_transition",
    "frame_schema",
    "coupling_evaluation",
    "pattern_evaluation",
    "state_evaluation",
    "semantic_evaluation",
    "sequence_memory_budget",
    "context_overflow",
    "output_truncated",
    "provider_retryable_exhausted",
    "dedup",
    "quality",
    "annotate",
    "verify",
    "reconcile",
    "noise_schema",
    "noise_semantic",
    "noise_similarity",
    "noise_memory_budget",
    "noise_context_overflow",
    "noise_output_truncated",
    "noise_provider_retryable_exhausted",
    "noise_reconcile",
)

_ZERO_RUNTIME_REPORT = {
    "queue_high_water": 0,
    "running_high_water": 0,
    "resource_wait_high_water": 0,
    "commit_waiting_high_water": 0,
    "candidate_bytes_high_water": 0,
    "cancelled_tasks": 0,
    "resource_wait_ms": 0,
    "http_pool_wait_ms": 0,
    "commit_ms": 0,
}


def _delivery_contract_error(message: str) -> NoReturn:
    """记录并抛出 generation delivery 契约错误。

    @param message 固定英文错误文本。
    @return 不返回。
    """
    _log.error(message, extra={"stage": "generate", "batch": 0})
    raise InternalError(message)


def _write_plan_failed_report(cfg, program, exc: BaseException) -> None:
    """为 live planner 终态写入无内容 failed report。

    @param cfg 已通过 M1 且路径已冻结的 sequence 配置。
    @param program 已成功编译、用于派生 run_attempt_id 的程序。
    @param exc planner 原始终态异常。
    @return None；failed-report I/O 不遮蔽 planner 主异常。
    """
    from labelkit.operators.emitter import SequenceDeliveryEmitter
    from labelkit.operators.generation.project import derive_generation_id

    attempt_id = derive_generation_id("run_attempt_id", [program.digest, program.planner_seed])
    report = {
        "run_attempt_id": attempt_id,
        "run_id": None,
        "artifacts_committed": False,
        "failed_slot": None,
        "attempts_used": 0,
        "terminal_error_kind": _planning_terminal_kind(exc),
        "llm_usage": {},
        "rejected_attempts": {key: 0 for key in _REJECTION_KEYS},
        "runtime": dict(_ZERO_RUNTIME_REPORT),
    }
    try:
        SequenceDeliveryEmitter(cfg.paths).write_failed_report(report)
    except LabelKitError:
        _log.error("generation_failed_report_io", extra={"stage": "run", "batch": 0})


def _planning_terminal_kind(exc: BaseException) -> str:
    """把 planner 终态映射到冻结 failed-report kind。

    @param exc planner 原始终态异常。
    @return generation_plan_infeasible、budget 或 internal。
    """
    if isinstance(exc, ConfigError):
        return "generation_plan_infeasible"
    prefix = str(exc).partition(":")[0]
    if prefix in {"generation_plan_budget", "generation_plan_internal"}:
        return prefix
    return "generation_plan_internal"


@dataclass
class _DeliveryState:
    """一次运行内只驻留内存的已接受产品状态。"""

    sequences: list[SequenceRows] = field(default_factory=list)  # 声明序主序列
    witnesses: list["ProjectionWitness"] = field(default_factory=list)  # compact 源证明
    noise_payload_digests: list[str] = field(default_factory=list)  # noise 源摘要
    noise_rows: list[Mapping[str, object]] = field(default_factory=list)  # NoiseSlot 序
    replays: list["ReplayRows"] = field(default_factory=list)  # ReplayLayout 顺序分组
    sources: dict[tuple[str, str | None], SequenceRows] = field(default_factory=dict)  # replay 源
    retained_bytes: int = 0                       # 已接受 canonical JSONL bytes
    sequence_attempts: int = 0                    # 已消费主槽尝试数
    noise_attempts: int = 0                       # 已消费 noise 尝试数
    rejected: dict[str, int] = field(             # 闭集拒绝账本
        default_factory=lambda: {key: 0 for key in _REJECTION_KEYS}
    )
    failed_slot: str | None = None                # 当前终态槽
    attempts_used: int = 0                        # 当前终态槽已消费尝试数


@dataclass(frozen=True)
class _AttemptOutcome:
    """一个 coordinator 已完成、等待声明序裁决的 attempt。"""

    attempt_index: int                            # 零基 attempt 序号
    candidate: PreparedCandidate | PreparedNoiseCandidate | None  # 成功候选
    reservation: DedupReservation | None          # downstream 失败仍保留的 reservation
    error: BaseException | None                   # recoverable 或 terminal 原异常


@dataclass(frozen=True)
class _OutcomeNotice:
    """coordinator 向唯一提交协调器转移 outcome 所有权的通知。"""

    ordinal: int                                  # 当前 phase 声明序 ordinal
    outcome: _AttemptOutcome                      # 完整 attempt outcome
    decision: asyncio.Future[str]                 # retry 或 done 控制面


@dataclass(frozen=True)
class _ControlNotice:
    """coordinator 用于原样穿透语言级控制异常的通知。"""

    ordinal: int                                  # 当前 phase 声明序 ordinal
    error: BaseException                          # 不进入业务 outcome 的原异常


@dataclass
class _PhaseRun:
    """一个 phase 的连续窗口控制面。"""

    phase: str                                    # primary 或 noise
    slots: tuple                                  # 当前 phase 冻结槽表
    task_group: asyncio.TaskGroup                 # 唯一 coordinator TaskGroup
    permits: asyncio.Semaphore                    # 跨 coordinator 全寿命 permit
    tasks: dict[int, asyncio.Task[None]]          # ordinal 到 coordinator task


@dataclass(frozen=True)
class _PrimaryBuild:
    """primary candidate 装配所需的 attempt-local 闭包。"""

    slot: object                                  # 当前 DeliverySlot
    batch_no: int                                 # 一基声明序批号
    attempt_index: int                            # 零基 attempt
    transaction: AttemptTransaction               # 下游完成后的唯一事务
    projections: tuple                            # variant 序投影
    witnesses: tuple                              # variant 序 compact witness
    reservation: DedupReservation                 # 当前唯一 reservation
    counters: Mapping[str, int]                   # attempt-local dataset delta


@dataclass(frozen=True)
class _PrimaryClosure:
    """candidate-local CrossView 的当前 primary 闭包。"""

    slot: object                                  # 当前 DeliverySlot
    witnesses: tuple                              # variant 序 compact witness
    rows: tuple[SequenceRows, ...]                # variant 序最终 rows
    replays: tuple                                # 当前 source 全部 replay
    retained: int                                 # 当前候选实际 canonical bytes


def _plan_events(plan, slot_key: str, variant: str | None):
    """从唯一 plan block 表解析一条 branch。"""
    found = [block[(slot_key, variant)] for block in plan.blocks
             if (slot_key, variant) in block]
    if len(found) != 1:
        _delivery_contract_error("generation_downstream_contract: plan branch lookup failed")
    return found[0]


def _slot_seed_calls(program, slot) -> int:
    """判断一个槽的 ScenarioSeed 是否需要真实调用。"""
    if program.mode == "instruction_only":
        return 1
    source = program.class_views[slot.sequence_class].sequence_generation
    return 0 if source is not None and source.initial_state_source == "catalog" else 1


def _slot_variant_calls(program, plan, slot) -> int:
    """计算一个 declared set 的 causal suffix 新事件数。"""
    if program.mode == "instruction_only":
        return 0
    source = next(item for item in program.counterfactual_sets if item.name == slot.source_name)
    baseline = _plan_events(plan, slot.slot_key, None)
    role_indexes = {event.role: index for index, event in enumerate(baseline)}
    total = 0
    for variant in source.variants:
        if variant.kind == "positive":
            continue
        branch = _plan_events(plan, slot.slot_key, variant.name)
        total += len(branch) - role_indexes.get(variant.divergence_role, 0)
    return total


def _slot_semantic_calls(program, slot) -> int:
    """计算一个槽实际执行的独立语义判定次数。"""
    if program.mode == "instruction_only":
        return 1
    source = next(item for item in program.counterfactual_sets if item.name == slot.source_name)
    has_positive = any(variant.kind == "positive" for variant in source.variants)
    return len(source.variants) + int(not has_positive)


def _sequence_generation_calls(program, plan) -> dict[str, int]:
    """按真实 branch 与 protected-prefix 计算一次成功交付的七类调用。"""
    seed_calls = sum(_slot_seed_calls(program, slot) for slot in plan.delivery_slots)
    baseline_calls = sum(len(_plan_events(plan, slot.slot_key, None))
                         for slot in plan.delivery_slots)
    variant_calls = sum(_slot_variant_calls(program, plan, slot)
                        for slot in plan.delivery_slots)
    semantic_calls = sum(_slot_semantic_calls(program, slot)
                         for slot in plan.delivery_slots)
    return {
        "scenario_seed_calls": seed_calls,
        "baseline_event_plan_calls": baseline_calls,
        "variant_event_plan_calls": variant_calls,
        "frame_render_calls": baseline_calls + variant_calls,
        "semantic_evaluation_calls": semantic_calls,
        "noise_render_calls": len(plan.noise_slots),
        "noise_evaluation_calls": len(plan.noise_slots),
    }


def _sequence_primary_events(plan) -> int:
    """计算全部交付 branch 的精确 primary event 数。"""
    return sum(
        len(_plan_events(plan, slot.slot_key, variant))
        for slot in plan.delivery_slots
        for variant in (slot.variant_names or (None,))
    )


def _sequence_downstream_calls(cfg, program, plan) -> dict[str, int]:
    """按生效 ClassView 计算一次成功 attempt 的下游调用。"""
    quality = annotate = verify = 0
    for slot in plan.delivery_slots:
        count = len(slot.variant_names) if slot.variant_names else 1
        view = program.class_views[slot.sequence_class]
        if cfg.quality.enabled:
            quality += count * len(view.rubric.criteria)
        if cfg.annotate.enabled:
            annotate += count * max(1, view.annotate.self_consistency)
        if cfg.verify.enabled:
            verify += count * max(1, len(view.verify.judges))
    frame = _sequence_primary_events(plan) if cfg.frame_annotate.enabled else 0
    return {
        "quality_calls": quality,
        "annotate_calls": annotate,
        "frame_annotate_calls": frame,
        "verify_calls": verify,
    }


def estimate_sequence_products(cfg, program, plan) -> dict:
    """从同一已冻结 compiler/planner 产物构造精确 sequence 估算。"""
    sequence_calls = _sequence_generation_calls(program, plan)
    downstream = _sequence_downstream_calls(cfg, program, plan)
    primary_sequences = sum(len(slot.variant_names) if slot.variant_names else 1
                            for slot in plan.delivery_slots)
    calls = dict.fromkeys(_ESTIMATE_CALL_ORDER, 0)
    calls["generate_calls"] = sum(sequence_calls.values())
    calls.update(downstream)
    estimate = {"records": primary_sequences, "batches": len(plan.delivery_slots)}
    estimate.update({key: calls[key] for key in _ESTIMATE_CALL_ORDER})
    estimate["total_calls"] = sum(calls.values())
    estimate["sequence"] = _sequence_estimate_block(program, plan, calls, sequence_calls)
    return estimate


def estimate_sequence(cfg) -> dict:
    """使用生产 compiler/planner 构造独立调用的 sequence 估算。"""
    from labelkit.operators.generation.planner import compile_scenario_plan
    from labelkit.operators.generation.program import compile_generation_program

    program = compile_generation_program(cfg)
    plan = compile_scenario_plan(program)
    return estimate_sequence_products(cfg, program, plan)


def _sequence_estimate_block(program, plan, calls, sequence_calls) -> dict:
    """组装 estimate.sequence 的精确数量与尝试边界。"""
    primary_sequences = sum(len(slot.variant_names) if slot.variant_names else 1
                            for slot in plan.delivery_slots)
    primary_events = _sequence_primary_events(plan)
    replay_events = sum(len(layout.timestamps_us) for layout in plan.replay_layouts)
    lower = sum(calls.values())
    return {
        "mode": program.mode,
        "program_digest": program.digest,
        "plan_digest": plan.digest,
        "planned_sets": len(plan.delivery_slots),
        "planned_sequences": primary_sequences,
        "primary_events": primary_events,
        "primary_sessions": plan.primary_sessions,
        "crossed_primary_sessions": program.timeline.crossed_primary_sessions,
        "noise_events": len(plan.noise_slots),
        "replay_sequences": len(plan.replay_layouts),
        "replay_events": replay_events,
        "stream_rows": primary_events + len(plan.noise_slots) + replay_events,
        "successful_attempt_lower_bound": lower,
        "max_slot_attempts_upper_bound": lower * program.max_slot_attempts,
        "sequence_calls": sequence_calls,
    }


async def deliver_generation(
    request: DeliveryRequest,
    services: DeliveryServices,
) -> GenerationProduct:
    """并发准备全部 slot，并只按声明序提交完整产品。

    @param request 冻结 program、plan、路径与运行身份。
    @param services 唯一生成服务根与下游协作者。
    @return 已经 manifest-last 提交的完整产品。
    """
    controller = _DeliveryController(request, services)
    try:
        return await controller.deliver()
    except BaseException as exc:
        controller.write_failed_report(exc)
        raise


class _DeliveryController:
    """拥有唯一 coordinator TaskGroup、候选缓冲与声明序提交器。"""

    def __init__(self, request: DeliveryRequest, services: DeliveryServices):
        """保存冻结输入并初始化无内容运行账本。"""
        self.request = request
        self.services = services
        self.generation = services.generation
        self.state = _DeliveryState()
        self.started_at = datetime.now().astimezone()
        self.started_perf = time.perf_counter()
        self._frontier: Any = None
        self._notices: asyncio.Queue[_OutcomeNotice | _ControlNotice] = asyncio.Queue()
        self._buffer: dict[int, _OutcomeNotice | _ControlNotice] = {}
        self._waiting = 0
        self._candidate_bytes = 0
        self._attempts_by_slot: dict[tuple[str, int], int] = {}
        self._noise_similarity: SimilarityFilter | None = None

    async def deliver(self) -> GenerationProduct:
        """完成有界准备、最终独立对账与 manifest-last commit。"""
        from labelkit.operators.generation.project import CrossViewFrontier, validate_plan_identity

        validate_plan_identity(self.request.program, self.request.plan)
        self._frontier = CrossViewFrontier(self.request.plan)
        try:
            await self._deliver_slot_phases()
            self._clear_failure_ledger()
            self._final_reconcile()
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit):
            self._clear_failure_ledger()
            raise
        stream_rows = self._ordered_stream_rows()
        report = self._success_report(stream_rows)
        product = self.services.emitter.prepare_product(
            [row.main_row for row in self.state.sequences], stream_rows, report
        )
        self.services.emitter.commit(product)
        return product

    async def _deliver_slot_phases(self) -> None:
        """用唯一 TaskGroup 依次运行 primary 与 noise phase。"""
        terminal: BaseException | None = None
        async with asyncio.TaskGroup() as task_group:
            terminal = await self._run_phase(
                "primary", tuple(self.request.plan.delivery_slots), task_group
            )
            if terminal is None:
                terminal = await self._run_phase(
                    "noise", tuple(self.request.plan.noise_slots), task_group
                )
        if terminal is not None:
            raise terminal

    async def _run_phase(self, phase: str, slots: tuple, task_group) -> BaseException | None:
        """运行一个连续候选窗口并返回 cleanup 后的终态。"""
        if not slots:
            return None
        self._reset_phase_buffer()
        capacity = self._candidate_capacity(phase, len(slots))
        permits = asyncio.Semaphore(capacity)
        tasks: dict[int, asyncio.Task[None]] = {}
        run = _PhaseRun(phase, slots, task_group, permits, tasks)
        if phase == "noise":
            self._noise_similarity = self._initial_similarity_filter()
        next_launch = await self._launch_initial(run)
        next_commit = 0
        try:
            while next_commit < len(slots):
                notice, fatal = await self._wait_head_or_fatal(next_commit, phase)
                if fatal is not None:
                    self._set_fatal_ledger(phase, slots, fatal)
                    await self._cancel_phase(tasks)
                    return fatal.error if isinstance(fatal, _ControlNotice) else fatal.outcome.error
                if notice is None:
                    _delivery_contract_error("generation_downstream_contract: missing head outcome")
                action, terminal = self._resolve_head(
                    phase, slots[next_commit], next_commit, notice
                )
                if terminal is not None:
                    await self._cancel_phase(tasks)
                    return terminal
                notice.decision.set_result(action)
                if action == "retry":
                    continue
                next_commit += 1
                next_launch = await self._launch_tail(run, next_launch)
            await self._settle_phase(tasks)
            return None
        except asyncio.CancelledError:
            await self._cancel_phase(tasks)
            self._clear_failure_ledger()
            raise
        finally:
            self._cleanup_phase_buffer()

    async def _launch_initial(self, run: _PhaseRun) -> int:
        """在创建 coordinator 前取得初始连续窗口 permit。"""
        count = min(self._candidate_capacity(run.phase, len(run.slots)), len(run.slots))
        for ordinal in range(count):
            await self._launch_coordinator(run, run.slots[ordinal], ordinal)
        return count

    async def _launch_tail(self, run: _PhaseRun, ordinal: int) -> int:
        """只在 head 成功提交后把连续窗口向右移动一格。"""
        if ordinal >= len(run.slots):
            return ordinal
        await self._launch_coordinator(run, run.slots[ordinal], ordinal)
        return ordinal + 1

    async def _launch_coordinator(self, run: _PhaseRun, slot, ordinal: int) -> None:
        """先取得 phase permit，再创建一个全寿命 coordinator。"""
        await run.permits.acquire()
        operation = self._coordinate_primary if run.phase == "primary" else self._coordinate_noise
        try:
            task = run.task_group.create_task(operation(slot, ordinal, run.permits))
        except BaseException:
            run.permits.release()
            _log.error("sequence coordinator creation failed", extra={"stage": "generate", "batch": 0})
            raise
        run.tasks[ordinal] = task

    async def _coordinate_primary(self, slot, ordinal: int, permits) -> None:
        """同一 primary slot 内串行尝试，并把重试决定交给 head。"""
        await self._coordinate_slot("primary", slot, ordinal, permits)

    async def _coordinate_noise(self, slot, ordinal: int, permits) -> None:
        """同一 noise slot 内串行尝试，并把重试决定交给 head。"""
        await self._coordinate_slot("noise", slot, ordinal, permits)

    async def _coordinate_slot(self, phase: str, slot, ordinal: int, permits) -> None:
        """保持窗口 permit，直到该 ordinal commit 或 cleanup。"""
        attempt_index = 0
        try:
            while True:
                owned_reservation = None
                try:
                    outcome = await self._capture_attempt(phase, slot, ordinal, attempt_index)
                    owned_reservation = outcome.reservation
                    decision = asyncio.get_running_loop().create_future()
                    notice = _OutcomeNotice(ordinal, outcome, decision)
                    self._notices.put_nowait(notice)
                    owned_reservation = None
                    action = await decision
                finally:
                    if owned_reservation is not None:
                        self.services.dedup.group_discard(owned_reservation)
                if action == "done":
                    return
                attempt_index += 1
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
            current = asyncio.current_task()
            if isinstance(exc, asyncio.CancelledError) and current is not None and current.cancelling():
                raise
            self._notices.put_nowait(_ControlNotice(ordinal, exc))
        finally:
            permits.release()

    async def _capture_attempt(self, phase, slot, ordinal, attempt_index) -> _AttemptOutcome:
        """把非取消异常冻结为不改变报告的 coordinator outcome。"""
        try:
            if phase == "primary":
                return await self._prepare_primary(slot, ordinal, attempt_index)
            return await self._prepare_noise(slot, attempt_index)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _AttemptOutcome(attempt_index, None, None, exc)

    async def _prepare_primary(self, slot, ordinal: int, attempt_index: int) -> _AttemptOutcome:
        """准备一个 primary attempt，并精确转移 reservation 所有权。"""
        traces = await self._generate_traces(slot, attempt_index)
        projections = self._project_traces(slot, traces)
        witnesses = self._projection_witnesses(projections)
        transaction = self._transaction(slot, projections)
        context = self._context(slot.slot_key, attempt_index, "dedup", ordinal + 1)
        reservation = await self._dedup_reserve(transaction, context)
        try:
            counters = await self._run_downstream(transaction, slot, attempt_index, ordinal + 1)
            build = _PrimaryBuild(
                slot, ordinal + 1, attempt_index, transaction, projections,
                witnesses, reservation, counters,
            )
            candidate = self._assemble_primary_candidate(build)
            return _AttemptOutcome(attempt_index, candidate, reservation, None)
        except BaseException as exc:
            try:
                recoverable = self._recoverable_kind(exc, "primary")
            except BaseException:
                self.services.dedup.group_discard(reservation)
                raise
            if recoverable is not None:
                return _AttemptOutcome(attempt_index, None, reservation, exc)
            self.services.dedup.group_discard(reservation)
            raise

    async def _prepare_noise(self, slot, attempt_index: int) -> _AttemptOutcome:
        """准备并深冻结一个不读取正式相似度前缀的 noise attempt。"""
        payload = await self._render_and_evaluate_noise(slot, attempt_index)
        candidate = self._assemble_noise_candidate(slot, payload, attempt_index)
        return _AttemptOutcome(attempt_index, candidate, None, None)

    async def _render_and_evaluate_noise(self, slot, attempt_index: int):
        """执行 noise 渲染与独立盲审。"""
        from labelkit.operators.generation.evaluate import evaluate_noise
        from labelkit.operators.generation.render import render_noise

        started = time.perf_counter()
        try:
            payload = await render_noise(
                self._noise_render_request(slot, attempt_index), self.generation
            )
            evaluation = await evaluate_noise(
                self._noise_evaluation_request(payload, slot, attempt_index), self.generation
            )
        finally:
            self.generation.metrics.add_stage_time("generate", time.perf_counter() - started)
        if not self._noise_accepted(evaluation):
            raise GenerationAttemptRejected("noise_semantic", self._noise_slot_key(slot))
        return payload

    def _assemble_primary_candidate(self, build: _PrimaryBuild) -> PreparedCandidate:
        """装配、局部对账并深冻结一个 primary candidate。"""
        try:
            rows = tuple(
                self.services.emitter.assemble_sequence(SequenceAssemblyRequest(
                    self.request.program, self.generation.schema_engine,
                    item, projection, build.batch_no,
                ))
                for item, projection in zip(
                    build.transaction.items, build.projections, strict=True
                )
            )
        except GenerationProjectionMismatch:
            raise GenerationAttemptRejected("reconcile", build.slot.slot_key) from None
        replays = self._project_replays(build.slot, rows)
        retained = self._primary_retained(rows, replays)
        closure = _PrimaryClosure(build.slot, build.witnesses, rows, replays, retained)
        self._reconcile_primary_candidate(closure)
        candidate = PreparedCandidate(
            build.slot, build.attempt_index + 1, build.witnesses, rows, replays,
            build.reservation, self._validated_counters(build.counters), retained, "",
        )
        return replace(candidate, digest=_prepared_digest("primary", candidate))

    def _assemble_noise_candidate(self, slot, payload, attempt_index) -> PreparedNoiseCandidate:
        """投影、局部对账并深冻结一个 noise candidate。"""
        from labelkit.operators.generation.project import noise_payload_digest, project_noise

        projection = NoiseProjectionRequest(self.request.program, self.request.run_id, slot, payload)
        row = project_noise(projection)
        payload_digest = noise_payload_digest(payload)
        retained = len(canonical_delivery_row(row)) + 1
        self._reconcile_noise_candidate(slot, payload_digest, row, retained)
        signature = self._noise_signature(canonical_json(row["payload"]))
        candidate = PreparedNoiseCandidate(
            slot, attempt_index + 1, payload_digest, row, signature, {}, retained, "",
        )
        return replace(candidate, digest=_prepared_digest("noise", candidate))

    def _reconcile_primary_candidate(self, closure: _PrimaryClosure) -> None:
        """把当前 primary 闭包 mismatch 映射为当前 attempt 拒绝。"""
        from labelkit.operators.generation.project import reconcile_primary_candidate

        layouts = tuple(
            layout for layout in self.request.plan.replay_layouts
            if layout.source_slot_key == closure.slot.slot_key
        )
        request = PrimaryCandidateReconcileRequest(
            self.request.program, self.request.plan, self.request.run_id, closure.slot,
            closure.witnesses, closure.rows, layouts, closure.replays, closure.retained,
        )
        try:
            reconcile_primary_candidate(request)
        except GenerationProjectionMismatch:
            raise GenerationAttemptRejected("reconcile", closure.slot.slot_key) from None

    def _reconcile_noise_candidate(self, slot, payload_digest, row, retained) -> None:
        """把当前 noise 闭包 mismatch 映射为当前 attempt 拒绝。"""
        from labelkit.operators.generation.project import reconcile_noise_candidate

        request = NoiseCandidateReconcileRequest(
            self.request.program, self.request.run_id, slot, payload_digest, row, retained
        )
        try:
            reconcile_noise_candidate(request)
        except GenerationProjectionMismatch:
            raise GenerationAttemptRejected("reconcile", self._noise_slot_key(slot)) from None

    async def _wait_head_or_fatal(self, head: int, phase: str):
        """等待 head 或任一 terminal，并稳定收集同一事件循环批次。"""
        while True:
            self._drain_notices()
            fatal = self._stable_fatal(phase)
            if fatal is not None:
                await asyncio.sleep(0)
                self._drain_notices()
                fatal = self._stable_fatal(phase)
                return None, fatal
            if head in self._buffer:
                await asyncio.sleep(0)
                self._drain_notices()
                fatal = self._stable_fatal(phase)
                if fatal is not None:
                    return None, fatal
                return self._take_notice(head), None
            self._register_notice(await self._notices.get())

    def _drain_notices(self) -> None:
        """把已经完成的 coordinator outcomes 全部转入候选缓冲。"""
        while True:
            try:
                notice = self._notices.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._register_notice(notice)

    def _register_notice(self, notice: _OutcomeNotice | _ControlNotice) -> None:
        """转移一个 outcome，并更新等待数量与实际字节高水位。"""
        if notice.ordinal in self._buffer:
            _delivery_contract_error("generation_downstream_contract: duplicate slot outcome")
        self._buffer[notice.ordinal] = notice
        if isinstance(notice, _ControlNotice):
            return
        self._waiting += 1
        self._candidate_bytes += _outcome_bytes(notice.outcome)
        metrics = self.generation.metrics
        metrics.observe_runtime_high_water("commit_waiting", self._waiting)
        metrics.observe_runtime_high_water("candidate_bytes", self._candidate_bytes)

    def _take_notice(self, ordinal: int) -> _OutcomeNotice:
        """从缓冲取出 head 并更新当前驻留量。"""
        notice = self._buffer.pop(ordinal)
        if isinstance(notice, _ControlNotice):
            _delivery_contract_error("generation_downstream_contract: control reached head reduce")
        self._waiting -= 1
        self._candidate_bytes -= _outcome_bytes(notice.outcome)
        return notice

    def _stable_fatal(self, phase: str) -> _OutcomeNotice | _ControlNotice | None:
        """按 phase ordinal 选择已观察到的最小 terminal outcome。"""
        values: list[_OutcomeNotice | _ControlNotice] = []
        for notice in self._buffer.values():
            if isinstance(notice, _ControlNotice):
                values.append(notice)
                continue
            if (
                notice.outcome.error is not None
                and self._recoverable_kind(notice.outcome.error, phase) is None
            ):
                values.append(notice)
        return min(values, key=lambda value: value.ordinal) if values else None

    def _resolve_head(self, phase, slot, ordinal, notice) -> tuple[str, BaseException | None]:
        """无 await 裁决当前 head，并显式返回 retry 或 done。"""
        outcome = notice.outcome
        try:
            rejection = (
                self._commit_primary_head(outcome)
                if phase == "primary" else self._commit_noise_head(outcome)
            )
        except Exception as exc:
            self._set_slot_ledger(phase, slot, outcome.attempt_index)
            return "terminal", exc
        self._consume_attempt(outcome.attempt_index, phase == "noise", ordinal)
        if rejection is None:
            return "done", None
        self._set_slot_ledger(phase, slot, outcome.attempt_index + 1)
        self._reject(rejection)
        if outcome.attempt_index + 1 < self.request.program.max_slot_attempts:
            return "retry", None
        return (
            "terminal",
            DeliveryError(
                "sequence_delivery_exhausted", self._slot_identity(phase, slot),
                self.request.program.max_slot_attempts,
            ),
        )

    def _commit_primary_head(self, outcome: _AttemptOutcome) -> str | None:
        """按冻结顺序执行 primary head 的无 await 临界区。"""
        started = time.perf_counter()
        reservation = outcome.reservation
        try:
            if reservation is None:
                if outcome.error is None:
                    _delivery_contract_error(
                        "generation_downstream_contract: primary reservation is absent"
                    )
                return self._recoverable_kind(outcome.error, "primary")
            try:
                self.services.dedup.group_revalidate(reservation)
            except DedupGroupRejected:
                self.services.dedup.group_discard(reservation)
                return "dedup"
            except BaseException:
                self.services.dedup.group_discard(reservation)
                raise
            if outcome.error is not None:
                self.services.dedup.group_discard(reservation)
                return self._recoverable_kind(outcome.error, "primary")
            return self._commit_primary_candidate(outcome, reservation)
        finally:
            self._record_commit_time(started)

    def _commit_primary_candidate(self, outcome, reservation) -> str | None:
        """校验冻结候选并在全部可恢复 gate 后原子交换状态。"""
        candidate = outcome.candidate
        try:
            if not isinstance(candidate, PreparedCandidate):
                _delivery_contract_error("generation_downstream_contract: invalid primary outcome")
            if candidate.digest != _prepared_digest("primary", candidate):
                raise GenerationProjectionMismatch("prepared candidate digest mismatch")
            delta = self._frontier.check_primary(candidate)
            if self.state.retained_bytes + candidate.retained_content_bytes > (
                self.request.program.limits.retained_content_bytes
            ):
                self.services.dedup.group_discard(reservation)
                return "sequence_memory_budget"
            counters = self._validated_counters(candidate.dataset_counters)
            entries = self._validated_source_entries(candidate)
        except GenerationProjectionMismatch:
            self.services.dedup.group_discard(reservation)
            return "reconcile"
        except BaseException:
            self.services.dedup.group_discard(reservation)
            raise
        try:
            self.services.dedup.group_commit(reservation)
        except BaseException:
            self.services.dedup.group_discard(reservation)
            raise
        self._frontier.commit(delta)
        self.generation.metrics.merge_counts(counters)
        self._commit_primary_state(candidate, entries)
        return None

    def _commit_noise_head(self, outcome: _AttemptOutcome) -> str | None:
        """按最新正式相似度前缀执行 noise head 的无 await 临界区。"""
        started = time.perf_counter()
        try:
            if outcome.error is not None:
                return self._recoverable_kind(outcome.error, "noise")
            candidate = outcome.candidate
            if not isinstance(candidate, PreparedNoiseCandidate):
                _delivery_contract_error("generation_downstream_contract: invalid noise outcome")
            similarity = self._noise_similarity
            if similarity is None:
                _delivery_contract_error("generation_downstream_contract: noise filter is absent")
            novel, signature = similarity.probe(canonical_json(candidate.row["payload"]))
            if not novel:
                return "noise_similarity"
            return self._commit_noise_candidate(candidate, similarity, signature)
        finally:
            self._record_commit_time(started)

    def _commit_noise_candidate(self, candidate, similarity, signature) -> str | None:
        """校验冻结 noise 候选并在全部 gate 后提交相似度与状态。"""
        try:
            if candidate.digest != _prepared_digest("noise", candidate):
                raise GenerationProjectionMismatch("prepared noise digest mismatch")
            if candidate.similarity_signature != _signature_tuple(signature):
                raise GenerationProjectionMismatch("prepared noise signature mismatch")
            delta = self._frontier.check_noise(candidate)
            if self.state.retained_bytes + candidate.retained_content_bytes > (
                self.request.program.limits.retained_content_bytes
            ):
                return "noise_memory_budget"
            counters = self._validated_counters(candidate.dataset_counters)
        except GenerationProjectionMismatch:
            return "noise_reconcile"
        similarity.commit(signature)
        self._frontier.commit(delta)
        self.generation.metrics.merge_counts(counters)
        self.state.noise_payload_digests.append(candidate.payload_digest)
        self.state.noise_rows.append(candidate.row)
        self.state.retained_bytes += candidate.retained_content_bytes
        return None

    async def _cancel_phase(self, tasks: Mapping[int, asyncio.Task[None]]) -> None:
        """取消并等待当前 phase 全部 coordinator。"""
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await self._settle_phase(tasks)

    async def _settle_phase(self, tasks: Mapping[int, asyncio.Task[None]]) -> None:
        """逐个等待 coordinator，保持 TaskGroup 内结构化终止。"""
        for task in tasks.values():
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _cleanup_phase_buffer(self) -> None:
        """discard 队列和缓冲拥有的全部 reservation，并清零观测状态。"""
        self._drain_notices()
        for notice in tuple(self._buffer.values()):
            if isinstance(notice, _ControlNotice):
                continue
            reservation = notice.outcome.reservation
            if reservation is not None:
                self.services.dedup.group_discard(reservation)
            if not notice.decision.done():
                notice.decision.cancel()
        self._reset_phase_buffer()

    def _reset_phase_buffer(self) -> None:
        """重置 phase-local 队列、缓冲与当前驻留量。"""
        self._notices = asyncio.Queue()
        self._buffer = {}
        self._waiting = 0
        self._candidate_bytes = 0

    def _candidate_capacity(self, phase: str, remaining: int) -> int:
        """从当前 phase 不同 ResourceKey 的冻结容量求候选窗口。"""
        keys = self._phase_resource_keys(phase)
        cfg = self.generation.config
        capacity = sum(
            cfg.llm_profiles[name].max_concurrency if kind == "llm"
            else cfg.embedding_profiles[name].max_concurrency
            for kind, name in keys
        )
        return min(capacity, remaining)

    def _phase_resource_keys(self, phase: str) -> tuple[tuple[str, str], ...]:
        """按首次引用顺序返回 phase 的不同资源键。"""
        program, cfg = self.request.program, self.generation.config
        keys = [("llm", program.semantic_profile), ("llm", program.evaluation_profile)]
        if phase == "primary":
            keys.extend(self._primary_downstream_resource_keys())
            if cfg.dedup.semantic:
                keys.append(("embedding", cfg.dedup.semantic_embedding))
        if cfg.output.repair_llm is not None:
            keys.append(("llm", cfg.output.repair_llm))
        return tuple(dict.fromkeys(keys))

    def _primary_downstream_resource_keys(self) -> list[tuple[str, str]]:
        """收集 primary quality、annotate、frame 与 verify 资源。"""
        cfg = self.generation.config
        class_names = dict.fromkeys(
            slot.sequence_class for slot in self.request.plan.delivery_slots
        )
        views = tuple(self.request.program.class_views[name] for name in class_names)
        keys: list[tuple[str, str]] = []
        if cfg.quality.enabled:
            keys.extend(("llm", view.quality.llm) for view in views if view.quality.enabled)
        if cfg.annotate.enabled:
            keys.extend(("llm", view.annotate.llm) for view in views if view.annotate.enabled)
        frame_enabled = any(view.enabled for view in self.request.program.frame_classes.values())
        if cfg.frame_annotate.enabled and frame_enabled:
            keys.append(("llm", cfg.frame_annotate.llm))
        if cfg.verify.enabled:
            for view in views:
                if view.verify.enabled:
                    keys.extend(
                        ("llm", name) for name in (view.verify.judges or (view.verify.llm,))
                    )
        return keys

    async def _generate_traces(self, slot: "DeliverySlot", attempt_index: int):
        """调用 generation 高层槽入口并记录累计阶段时间。"""
        from labelkit.operators.generation.scenario import _generate_validated_slot_traces

        started = time.perf_counter()
        try:
            return await _generate_validated_slot_traces(
                self.request.program, self.request.plan, slot, attempt_index, self.generation
            )
        finally:
            self.generation.metrics.add_stage_time("generate", time.perf_counter() - started)

    def _project_traces(self, slot: "DeliverySlot", traces) -> tuple["ProjectedSequence", ...]:
        """按槽声明序投影并验证 trace cardinality。"""
        from labelkit.operators.generation.project import _project_trace_from_validated_plan

        expected = len(slot.variant_names) if slot.variant_names else 1
        if len(traces) != expected:
            _delivery_contract_error("generation_downstream_contract: trace count mismatch")
        return tuple(
            _project_trace_from_validated_plan(ProjectionRequest(
                self.request.program, self.request.plan, slot, trace
            ))
            for trace in traces
        )

    @staticmethod
    def _projection_witnesses(projections) -> tuple["ProjectionWitness", ...]:
        """在 projector 内容释放前冻结 compact witness。"""
        from labelkit.operators.generation.project import projection_witness

        return tuple(projection_witness(item) for item in projections)

    def _transaction(self, slot: "DeliverySlot", projections) -> AttemptTransaction:
        """构造含 inherited sequence/frame class 的唯一 items。"""
        variants = slot.variant_names or (None,)
        if len(variants) != len(projections):
            _delivery_contract_error("generation_downstream_contract: projection count mismatch")
        items = tuple(
            self._item(slot, variant, projection)
            for variant, projection in zip(variants, projections, strict=True)
        )
        return AttemptTransaction(items, self.request.program.class_views, projections)

    def _item(self, slot, variant_name, projection) -> PipelineItem:
        """从 pre-downstream projection 构造 attempt-local 信封。"""
        classification = Classification(
            label=slot.sequence_class, labels=(slot.sequence_class,), source="inherited", detail={}
        )
        member_classes = {
            member.id: Classification(
                label=row["_meta"]["event"]["frame_class"],
                labels=(row["_meta"]["event"]["frame_class"],), source="inherited", detail={},
            )
            for member, row in zip(
                projection.main_record.members, projection.primary_stream_rows, strict=True
            )
        }
        return PipelineItem(
            record=projection.main_record,
            classification=classification,
            session_id=self._planned_session_id(slot, variant_name),
            member_classifications=member_classes,
        )

    def _planned_session_id(self, slot, variant_name: str | None) -> str:
        """从唯一冻结 plan branch 取得信封 session 身份。"""
        key = (slot.slot_key, variant_name)
        branches = [block[key] for block in self.request.plan.blocks if key in block]
        sessions = {event.session_id for event in branches[0]} if len(branches) == 1 else set()
        if len(branches) != 1 or not branches[0] or len(sessions) != 1:
            _delivery_contract_error("generation_downstream_contract: invalid planned session")
        return next(iter(sessions))

    async def _dedup_reserve(self, transaction, context) -> DedupReservation:
        """创建零正式索引突变的 whole-set reservation。"""
        records = tuple(item.record for item in transaction.items)
        exempt = frozenset((left.id, right.id) for left, right in combinations(records, 2))
        cfg = self.generation.config.dedup
        profile = cfg.semantic_embedding if cfg.semantic else None
        started = time.perf_counter()
        try:
            reservation = await self.services.dedup.group_reserve(
                DedupGroupRequest(records, exempt, profile), context
            )
        finally:
            self.generation.metrics.add_stage_time("dedup", time.perf_counter() - started)
        for item, cluster_key in zip(
            transaction.items, reservation.exact_cluster_keys, strict=True
        ):
            item.dedup = DedupInfo(kind="unique", cluster_key=cluster_key, kept_id=None)
        return reservation

    async def _run_downstream(self, transaction, slot, attempt_index, batch_no):
        """保持 quality、annotate、verify 屏障并汇总 attempt-local counters。"""
        counters: dict[str, int] = {}
        cfg = replace(
            self.generation.config,
            class_views=transaction.class_views,
            frame_class_views=self.request.program.frame_classes,
            frame_schema=self.request.program.frame_schema,
        )
        collaborators = (
            ("quality", self.services.quality),
            ("annotate", self.services.annotate),
            ("verify", self.services.verify),
        )
        for stage, collaborator in collaborators:
            if collaborator is None:
                continue
            context = self._context(slot.slot_key, attempt_index, stage, batch_no)
            context.cfg = cfg
            result = await self._run_collaborator(
                stage, collaborator, DownstreamAttemptRequest(transaction, context)
            )
            self._merge_local(counters, result.dataset_counters)
            expected = None if result.accepted else stage
            if not result.accepted or result.rejected_stage != expected:
                raise GenerationAttemptRejected(stage, slot.slot_key)
        return counters

    async def _run_collaborator(self, stage: str, collaborator, request):
        """计时执行一个 attempt-local 下游协作者。"""
        started = time.perf_counter()
        try:
            return await collaborator.run_attempt(request)
        finally:
            self.generation.metrics.add_stage_time(stage, time.perf_counter() - started)

    def _project_replays(self, slot, rows) -> tuple["ReplayRows", ...]:
        """只从当前 source 最终 rows 派生匹配 replay。"""
        from labelkit.operators.generation.project import _project_replay_from_validated_plan

        variants = slot.variant_names or (None,)
        sources = dict(zip(
            ((slot.slot_key, variant) for variant in variants), rows, strict=True
        ))
        return tuple(
            _project_replay_from_validated_plan(ReplayProjectionRequest(
                self.request.program, self.request.plan, layout,
                sources[(layout.source_slot_key, layout.source_variant_name)],
            ))
            for layout in self.request.plan.replay_layouts
            if (layout.source_slot_key, layout.source_variant_name) in sources
        )

    @staticmethod
    def _primary_retained(rows, replays) -> int:
        """计算一个 primary candidate 的全部 canonical 行费用。"""
        return sum(row.retained_content_bytes for row in rows) + sum(
            row.retained_content_bytes for row in replays
        )

    def _validated_source_entries(self, candidate) -> tuple:
        """在 group_commit 前验证当前 source 不覆盖已提交前缀。"""
        variants = candidate.slot.variant_names or (None,)
        if len(variants) != len(candidate.sequences):
            raise GenerationProjectionMismatch("prepared candidate source count mismatch")
        entries = tuple(zip(
            ((candidate.slot.slot_key, variant) for variant in variants),
            candidate.sequences, strict=True,
        ))
        if any(key in self.state.sources for key, _row in entries):
            raise GenerationProjectionMismatch("prepared candidate source is duplicated")
        return entries

    def _commit_primary_state(self, candidate, entries) -> None:
        """在 dedup/frontier commit 后执行不可失败的状态交换。"""
        self.state.witnesses.extend(candidate.projection_witnesses)
        self.state.sequences.extend(candidate.sequences)
        for key, row in entries:
            self.state.sources[key] = row
        self.state.replays.extend(candidate.replays)
        self.state.retained_bytes += candidate.retained_content_bytes

    def _initial_similarity_filter(self) -> SimilarityFilter:
        """在 noise phase 入口一次装入全部正式 primary payload。"""
        cfg = self.generation.config.dedup
        similarity = SimilarityFilter(cfg.minhash_threshold, cfg.minhash_num_perm, cfg.ngram)
        for sequence in self.state.sequences:
            for row in sequence.primary_stream_rows:
                similarity.add(canonical_json(row["payload"]))
        return similarity

    def _noise_signature(self, text: str) -> tuple[int, ...]:
        """计算不读取正式前缀的 deterministic noise MinHash。"""
        cfg = self.generation.config.dedup
        probe = SimilarityFilter(cfg.minhash_threshold, cfg.minhash_num_perm, cfg.ngram)
        _novel, signature = probe.probe(text)
        return _signature_tuple(signature)

    def _final_reconcile(self) -> None:
        """从最终行独立重建一次全量 CrossView，不映射为 attempt 拒绝。"""
        from labelkit.operators.generation.project import reconcile_views

        request = ReconcileRequest(
            self.request.program, self.request.plan, self.request.run_id,
            tuple(self.state.witnesses), tuple(self.state.sequences),
            tuple(self.state.noise_payload_digests), tuple(self.state.noise_rows),
            tuple(self.state.replays), self.state.retained_bytes,
        )
        try:
            reconcile_views(request)
        except GenerationProjectionMismatch:
            self._clear_failure_ledger()
            _log.error("generation final CrossView invariant failed", extra={"stage": "generate", "batch": 0})
            raise InternalError("generation final CrossView invariant failed") from None

    def _ordered_stream_rows(self) -> tuple[Mapping[str, object], ...]:
        """按冻结 artifact timestamp 全局稳定排序最终 stream 行。"""
        rows = [row for sequence in self.state.sequences for row in sequence.primary_stream_rows]
        rows.extend(self.state.noise_rows)
        rows.extend(row for replay in self.state.replays for row in replay.rows)
        return tuple(sorted(rows, key=lambda row: row["_meta"]["event"]["timestamp"]))

    def _noise_render_request(self, slot, attempt_index: int) -> NoiseRenderRequest:
        """构造 noise 渲染请求且不暴露 primary 内容。"""
        spec = self.request.program.noise
        frame = self.request.program.frame_classes.get(slot.frame_class)
        if spec is None or frame is None:
            _delivery_contract_error("generation_downstream_contract: invalid noise slot")
        return NoiseRenderRequest(
            self.request.program.semantic_profile, slot, spec, frame,
            self._class_descriptions(), self._frame_descriptions(), attempt_index,
            self.request.program.limits,
        )

    def _noise_evaluation_request(self, payload, slot, attempt_index) -> NoiseEvaluationRequest:
        """构造与结构判定独立的 noise 盲审请求。"""
        return NoiseEvaluationRequest(
            self.request.program.evaluation_profile, payload, slot.topic,
            self._class_descriptions(), self._frame_descriptions(), attempt_index,
            self.request.program.limits,
        )

    def _context(self, slot_identity, attempt_index, purpose, batch_no) -> RunContext:
        """派生只更换 rng、batch 与 namespace 的身份一致 RunContext。"""
        return RunContext(
            cfg=self.generation.config,
            llm=self.generation.llm,
            schema_engine=self.generation.schema_engine,
            rng=random.Random(self._attempt_seed(slot_identity, attempt_index, purpose)),
            batch_no=batch_no,
            metrics=self.generation.metrics,
            tasks=self.generation.tasks,
            task_namespace=(
                f"{self.request.run_id}:sequence:{slot_identity}:"
                f"attempt:{attempt_index}:stage:{purpose}"
            ),
        )

    def _attempt_seed(self, slot_identity: str, attempt_index: int, purpose: str) -> int:
        """按冻结 canonical 材料派生完整整数随机种子。"""
        material = canonical_json([
            "labelkit:v1.19", "attempt_random",
            [self.request.program.planner_seed, slot_identity, attempt_index, purpose],
        ])
        return int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest(), "big")

    def _success_report(self, stream_rows) -> dict:
        """组装成功 report 与同形状 runtime block。"""
        return {
            "run": self._run_report(exit_code=0),
            "counts": self._counts_report(),
            "schema_engine": self._schema_report(),
            "generate": {"sequence": self._sequence_report(stream_rows)},
            "runtime": dict(self.generation.metrics.runtime_report),
            "trace": self._trace_report(),
            "llm_usage": self._usage_report(),
            "timing": self._timing_report(),
        }

    def _sequence_report(self, stream_rows) -> dict:
        """按冻结键序组装 report.generate.sequence。"""
        plan, timeline = self.request.plan, self.request.program.timeline
        primary_events = sum(len(row.primary_stream_rows) for row in self.state.sequences)
        return {
            "mode": self.request.program.mode,
            "run_attempt_id": self.request.run_attempt_id,
            "run_id": self.request.run_id,
            "delivery_digest": None,
            "artifacts_committed": False,
            "program_digest": self.request.program.digest,
            "planned_sets": len(plan.delivery_slots),
            "delivered_sets": len(plan.delivery_slots),
            "planned_sequences": self._planned_sequences(),
            "delivered_sequences": len(self.state.sequences),
            "primary_events": primary_events,
            "primary_sessions": plan.primary_sessions,
            "crossed_primary_sessions": timeline.crossed_primary_sessions,
            "noise_events": len(self.state.noise_rows),
            "replay_sequences": len(plan.replay_layouts),
            "replay_events": sum(len(replay.rows) for replay in self.state.replays),
            "replay_tail_sessions": len(plan.replay_layouts),
            "stream_rows": len(stream_rows),
            "sequence_slot_attempts": self.state.sequence_attempts,
            "noise_slot_attempts": self.state.noise_attempts,
            "sequence_calls": self._sequence_calls(),
            "by_pattern": self._by_pattern(),
            "rejected_attempts": dict(self.state.rejected),
        }

    def _run_report(self, exit_code: int) -> dict:
        """组装无 partial-delivery 的 sequence run block。"""
        cfg, paths = self.generation.config, self.request.paths
        return {
            "tool_version": __version__,
            "started_at": self.started_at.isoformat(),
            "finished_at": datetime.now().astimezone().isoformat(),
            "interrupted": False,
            "circuit_broken": False,
            "exit_code": exit_code,
            "modality": cfg.run.modality,
            "seed": self.request.program.planner_seed,
            "config_digest": cfg.config_digest,
            "project_digest": cfg.project_digest,
            "paths": {
                "project": paths.project,
                "project_root": paths.project_root,
                "input": paths.input,
                "output": paths.output,
                "report": paths.report,
                "rejects": paths.rejects,
                "sidecar": paths.sidecar,
                "trace": paths.trace,
                "stream": paths.stream,
                "manifest": paths.manifest,
                "failed_report": paths.failed_report,
            },
        }

    def _counts_report(self) -> dict:
        """组装 sequence generate-only 的守恒计数。"""
        delivered = len(self.state.sequences)
        return {
            "scanned": 0, "ingested": 0, "bad_input": 0, "dropped_dup": 0,
            "dropped_lowq": 0, "dropped_verify": 0, "failed": 0,
            "generated": delivered, "emitted": delivered,
        }

    def _schema_report(self) -> dict:
        """读取用户 Schema 调用的 resolved-at 账本。"""
        zero = {"l0_or_clean": 0, "l1": 0, "l3_1": 0, "l3_2": 0, "rejected": 0}
        stats = self.generation.schema_engine.stats
        return {"resolved_at": dict(stats) if stats else zero}

    def _sequence_calls(self) -> dict[str, int]:
        """读取七个 generation family 的唯一逻辑入口计数。"""
        counters = self.generation.metrics.counters
        return {
            key: int(counters.get(f"generate.sequence.calls.{key}", 0))
            for key in _SEQUENCE_CALL_KEYS
        }

    def _planned_sequences(self) -> int:
        """计算声明序精确 primary sequence 数。"""
        return sum(len(slot.variant_names) if slot.variant_names else 1
                   for slot in self.request.plan.delivery_slots)

    def _by_pattern(self) -> dict:
        """按 pattern 与 variant 声明序组装计划/交付计数。"""
        delivered: dict[tuple[str, str], int] = {}
        for sequence in self.state.sequences:
            truth = sequence.main_row.get("_meta", {}).get("generation", {})
            key = (truth.get("pattern"), truth.get("variant"))
            if all(isinstance(part, str) for part in key):
                delivered[key] = delivered.get(key, 0) + 1
        output = self._planned_by_pattern()
        for (pattern_name, variant_name), count in delivered.items():
            pattern = output.get(pattern_name)
            if pattern is None or variant_name not in pattern:
                _delivery_contract_error("generation_downstream_contract: unknown report row")
            pattern[variant_name]["delivered"] += count
        return output

    def _planned_by_pattern(self) -> dict:
        """构造声明序 planned/delivered 零基字典。"""
        output: dict = {}
        for source in self.request.program.counterfactual_sets:
            pattern = output.setdefault(source.pattern, {})
            for variant in source.variants:
                current = pattern.setdefault(variant.name, {"planned": 0, "delivered": 0})
                current["planned"] += source.count
        return output

    def _trace_report(self) -> dict:
        """读取现有 EventLog 的运行计数，不打开新通道。"""
        event_log = self.generation.metrics.event_log
        events = int(getattr(event_log, "events_written", 0) or 0)
        dropped = int(getattr(event_log, "dropped_events", 0) or 0)
        if self.generation.config.trace.enabled:
            if getattr(event_log, "closed", False):
                dropped += 1
            else:
                events += 1
        return {
            "enabled": self.generation.config.trace.enabled,
            "path": getattr(getattr(event_log, "cfg", None), "path", None),
            "events": events,
            "dropped_events": dropped,
        }

    def _usage_report(self) -> dict:
        """组装不含凭据值的按 profile 用量。"""
        report: dict = {}
        for name, value in self.generation.llm.usage_by_profile.items():
            entry = self._usage_entry(value)
            if entry is not None:
                report[name] = entry
        return report

    @staticmethod
    def _usage_entry(usage) -> dict | None:
        """把一个运行期 profile 累加器投影为 secret-safe 对象。"""
        keys = usage.keys or {}
        active = any((
            usage.calls, usage.retries, usage.prompt_tokens, usage.completion_tokens,
            usage.est_cost_usd is not None, len(keys) > 1, usage.parked_calls, usage.parked_ms,
        ))
        if not active:
            return None
        entry = {
            "calls": usage.calls, "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens, "retries": usage.retries,
        }
        if usage.est_cost_usd is not None:
            entry["est_cost_usd"] = usage.est_cost_usd
        if len(keys) > 1:
            entry["keys"] = {
                name: {"calls": row.calls, "rate_limited": row.rate_limited,
                       "disabled": row.disabled}
                for name, row in sorted(keys.items())
            }
        if len(keys) > 1 or usage.parked_calls or usage.parked_ms:
            entry.update({"parked_calls": usage.parked_calls, "parked_ms": usage.parked_ms})
        return entry

    def _timing_report(self) -> dict:
        """组装运行墙钟与 MetricsSink 阶段累计。"""
        return {
            "wall_s": round(time.perf_counter() - self.started_perf, 3),
            "per_stage_s": {
                name: round(seconds, 3)
                for name, seconds in self.generation.metrics.stage_times.items()
            },
        }

    def write_failed_report(self, exc: BaseException) -> None:
        """cleanup 完成后 best-effort 写 data-free failed report。"""
        report = {
            "run_attempt_id": self.request.run_attempt_id,
            "run_id": self.request.run_id,
            "artifacts_committed": False,
            "failed_slot": self.state.failed_slot,
            "attempts_used": self.state.attempts_used,
            "terminal_error_kind": self._terminal_kind(exc),
            "llm_usage": self._usage_report(),
            "rejected_attempts": dict(self.state.rejected),
            "runtime": dict(self.generation.metrics.runtime_report),
        }
        try:
            self.services.emitter.write_failed_report(report)
        except LabelKitError:
            _log.error("generation_failed_report_io", extra={"stage": "run", "batch": 0})

    def _set_fatal_ledger(
        self, phase: str, slots: tuple, notice: _OutcomeNotice | _ControlNotice,
    ) -> None:
        """按稳定 coordinator ordinal 冻结 fatal failed-report 身份。"""
        if isinstance(notice, _ControlNotice):
            self._clear_failure_ledger()
            return
        slot = slots[notice.ordinal]
        self.state.failed_slot = self._slot_identity(phase, slot)
        self.state.attempts_used = self._attempts_by_slot.get((phase, notice.ordinal), 0)

    def _set_slot_ledger(self, phase: str, slot, attempts: int) -> None:
        """冻结当前 head 的 failed-report 身份与尝试数。"""
        self.state.failed_slot = self._slot_identity(phase, slot)
        self.state.attempts_used = attempts

    def _clear_failure_ledger(self) -> None:
        """为成功、外部取消或最终不变式失败清空槽级账本。"""
        self.state.failed_slot = None
        self.state.attempts_used = 0

    def _consume_attempt(self, attempt_index: int, noise: bool, ordinal: int) -> None:
        """仅在 head 的成功或 recoverable 裁决后落账一次。"""
        phase = "noise" if noise else "primary"
        self._attempts_by_slot[(phase, ordinal)] = attempt_index + 1
        if noise:
            self.state.noise_attempts += 1
        else:
            self.state.sequence_attempts += 1

    def _reject(self, kind: str) -> None:
        """向闭集拒绝账本增加一次并记录 secret-safe 日志。"""
        if kind not in self.state.rejected:
            _delivery_contract_error("generation_downstream_contract: unknown rejection kind")
        self.state.rejected[kind] += 1
        _log.info(
            "sequence attempt rejected: slot=%s attempts=%s kind=%s",
            self.state.failed_slot, self.state.attempts_used, kind,
            extra={"stage": "generate", "batch": 0},
        )

    def _recoverable_kind(self, exc: BaseException | None, phase: str) -> str | None:
        """把显式 retryable 异常映射到当前 phase 闭集桶。"""
        if exc is None:
            return None
        if isinstance(exc, GenerationAttemptRejected):
            return self._generation_rejection_kind(exc.kind, phase)
        if isinstance(exc, DedupGroupRejected) and phase == "primary":
            return "dedup"
        if isinstance(exc, ProviderRetryableError):
            return "noise_provider_retryable_exhausted" if phase == "noise" else (
                "provider_retryable_exhausted"
            )
        if isinstance(exc, ContextOverflowError):
            return "noise_context_overflow" if phase == "noise" else "context_overflow"
        if isinstance(exc, OutputTruncatedError):
            return "noise_output_truncated" if phase == "noise" else "output_truncated"
        return None

    def _generation_rejection_kind(self, kind: str, phase: str) -> str:
        """验证 generation rejection 只落入当前 phase 的冻结桶。"""
        if phase == "noise":
            if kind == "reconcile":
                return "noise_reconcile"
            if kind in {
                "noise_schema", "noise_semantic", "noise_similarity",
                "noise_memory_budget", "noise_context_overflow",
                "noise_output_truncated", "noise_provider_retryable_exhausted",
            }:
                return kind
            _delivery_contract_error("generation_downstream_contract: invalid noise rejection")
        if kind == "sequence_projection_mismatch":
            return "reconcile"
        if kind not in _REJECTION_KEYS or kind.startswith("noise_"):
            _delivery_contract_error("generation_downstream_contract: invalid sequence rejection")
        return kind

    @staticmethod
    def _validated_counters(source: Mapping[str, int]) -> Mapping[str, int]:
        """预验证并复制一个 candidate dataset counter delta。"""
        result: dict[str, int] = {}
        for key, value in source.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                _delivery_contract_error("generation_downstream_contract: invalid dataset counter")
            result[key] = value
        return result

    @staticmethod
    def _merge_local(target: dict[str, int], source: Mapping[str, int]) -> None:
        """合并一个协作者返回的非负 dataset delta。"""
        for key, value in _DeliveryController._validated_counters(source).items():
            target[key] = target.get(key, 0) + value

    def _record_commit_time(self, started: float) -> None:
        """累计一次无 await 提交临界区的整数毫秒。"""
        elapsed = max(0, round((time.perf_counter() - started) * 1000))
        self.generation.metrics.add_runtime_total("commit_ms", elapsed)

    def _class_descriptions(self) -> dict[str, str]:
        """返回仅含闭集名称与描述的 class registry。"""
        return {name: view.description for name, view in self.request.program.class_views.items()}

    def _frame_descriptions(self) -> dict[str, str]:
        """返回仅含闭集名称与描述的 frame registry。"""
        return {name: view.description for name, view in self.request.program.frame_classes.items()}

    @staticmethod
    def _noise_accepted(evaluation) -> bool:
        """判定 noise 四项盲审全部通过且无原因码。"""
        return bool(
            evaluation.unrelated_to_declared_tasks
            and evaluation.no_executable_task
            and evaluation.realism
            and evaluation.matches_planned_topic
            and not evaluation.reason_codes
        )

    @staticmethod
    def _noise_slot_key(slot) -> str:
        """返回冻结 NoiseSlot 报告身份。"""
        return f"noise/{slot.ordinal:06d}"

    def _slot_identity(self, phase: str, slot) -> str:
        """返回当前 phase 槽的稳定报告身份。"""
        return slot.slot_key if phase == "primary" else self._noise_slot_key(slot)

    @staticmethod
    def _terminal_kind(exc: BaseException) -> str:
        """把运行终态折成不含异常正文的固定 kind。"""
        if isinstance(exc, DeliveryError):
            return exc.kind
        if isinstance(exc, ProviderFatalError):
            return "provider_fatal"
        if isinstance(exc, CircuitBreakerTripped):
            return "circuit_breaker_tripped"
        if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
            return "interrupted"
        if isinstance(exc, InternalError):
            return "generation_downstream_contract"
        if isinstance(exc, LabelKitError):
            return "generation_commit_io"
        return "internal_error"


def _prepared_digest(domain: str, candidate) -> str:
    """计算排除 digest 自身的 PreparedCandidate 规范摘要。"""
    material = {
        item.name: _canonical_value(getattr(candidate, item.name))
        for item in fields(candidate)
        if item.name != "digest"
    }
    encoded = canonical_json(["labelkit:v1.19", f"prepared_{domain}", material])
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical_value(value: object) -> object:
    """把冻结 carrier 递归转换为 canonical_json 可接受的树。"""
    if is_dataclass(value):
        return {item.name: _canonical_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, MappingABC):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_canonical_value(item) for item in value]
        return sorted(items, key=canonical_json)
    return value


def _signature_tuple(signature) -> tuple[int, ...]:
    """把 datasketch MinHash 转成小型不可变签名。"""
    return tuple(int(value) for value in signature.hashvalues)


def _outcome_bytes(outcome: _AttemptOutcome) -> int:
    """返回已完成候选实际 canonical bytes，失败 outcome 为零。"""
    candidate = outcome.candidate
    return int(candidate.retained_content_bytes) if candidate is not None else 0

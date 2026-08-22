"""v1.18 sequence whole-set 精确交付控制器。"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from itertools import combinations
from typing import TYPE_CHECKING, Mapping, NoReturn

from labelkit import __version__
from labelkit.common.contracts.generation import (
    AttemptTransaction,
    DedupGroupRequest,
    DeliveryRequest,
    DeliveryServices,
    DownstreamAttemptRequest,
    GenerationProduct,
    NoiseEvaluationRequest,
    NoiseProjectionRequest,
    NoiseRenderRequest,
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


_log = logging.getLogger("labelkit.generation_delivery")


def _delivery_contract_error(message: str) -> NoReturn:
    """记录并抛出 generation delivery 契约错误。

    @param message 固定英文错误文本。
    @return 不返回。
    """
    _log.error(message, extra={"stage": "generate", "batch": 0})
    raise InternalError(message)

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
    failed_slot: str | None = None                # 当前或最后失败槽
    attempts_used: int = 0                        # 当前槽已消费尝试数


@dataclass(frozen=True)
class _AcceptedSequenceAttempt:
    """通过全部 prospective gate、尚未提交的 set。"""

    rows: tuple[SequenceRows, ...]                 # 当前 set 的最终 rows
    witnesses: tuple["ProjectionWitness", ...]    # 当前 set 的 compact 源证明
    replays: tuple["ReplayRows", ...]             # 当前 source 同临界区 replay
    source_entries: tuple[tuple[tuple[str, str | None], SequenceRows], ...]  # replay 来源索引
    dedup_token: object                            # 一次性 group commit capability
    dataset_counters: Mapping[str, int]            # 下游 attempt-local 计数
    retained_bytes: int                            # rows 与 replays 的合计费用


@dataclass(frozen=True)
class _ReconcileOverride:
    """一次 prospective CrossView 调用覆盖的局部前缀。"""

    projection_witnesses: tuple | None = None      # 可选源证明前缀
    sequences: tuple | None = None                 # 可选最终 sequence 前缀
    replays: tuple | None = None                   # 可选 replay 分组前缀
    noise_payload_digests: tuple | None = None     # 可选 noise 源摘要前缀
    noise_rows: tuple | None = None                # 可选 noise 行前缀
    retained_content_bytes: int | None = None      # 可选 prospective 总费用


def _plan_events(plan, slot_key: str, variant: str | None):
    """从唯一 plan block 表解析一条 branch。

    @param plan 冻结 ScenarioPlan。
    @param slot_key DeliverySlot 身份。
    @param variant branch 名；baseline/instruction 为 None。
    @return PlannedEvent tuple。
    """
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
        protected = role_indexes.get(variant.divergence_role, 0)
        total += len(branch) - protected
    return total


def _slot_semantic_calls(program, slot) -> int:
    """计算一个槽实际执行的独立语义判定次数。

    @param program 冻结生成程序
    @param slot 当前交付槽
    @return hidden baseline 与可见反事实合计调用数
    """
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
    """从同一已冻结 compiler/planner 产物构造精确 sequence 估算。

    @param cfg 已解析 sequence 配置。
    @param program 唯一 GenerationProgram。
    @param plan 唯一 ScenarioPlan。
    @return 既有顶层键加 sequence 精确子块。
    """
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
    """使用生产 compiler/planner 构造独立调用的 sequence 估算。

    @param cfg 已解析 sequence 配置。
    @return 既有顶层键加 sequence 精确子块。
    """
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
    """精确交付全部 sequence/noise slot，并提交一次完整产品。

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
    """串行驱动 slot admission、事务提交与最终发射。"""

    def __init__(self, request: DeliveryRequest, services: DeliveryServices):
        """保存冻结输入并初始化无内容运行账本。

        @param request 本次交付请求。
        @param services 唯一协作者根。
        """
        self.request = request
        self.services = services
        self.generation = services.generation
        self.state = _DeliveryState()
        self.started_at = datetime.now().astimezone()
        self.started_perf = time.perf_counter()

    async def deliver(self) -> GenerationProduct:
        """交付全部槽、准备产品并执行 manifest-last commit。

        @return 已提交的 GenerationProduct。
        """
        from labelkit.operators.generation.project import validate_plan_identity

        validate_plan_identity(self.request.program, self.request.plan)
        await self._deliver_primary_slots()
        await self._deliver_noise_slots()
        self.state.failed_slot = None
        self.state.attempts_used = 0
        self._reconcile()
        stream_rows = self._ordered_stream_rows()
        report = self._success_report(stream_rows)
        product = self.services.emitter.prepare_product(
            [row.main_row for row in self.state.sequences], stream_rows, report
        )
        self.services.emitter.commit(product)
        return product

    async def _deliver_primary_slots(self) -> None:
        """按声明序逐槽接受并提交一个 whole set。"""
        for batch_no, slot in enumerate(self.request.plan.delivery_slots, start=1):
            accepted = await self._accept_sequence_slot(slot, batch_no)
            self._commit_sequence_attempt(accepted)

    async def _accept_sequence_slot(
        self,
        slot: "DeliverySlot",
        batch_no: int,
    ) -> _AcceptedSequenceAttempt:
        """有界重试一个 sequence slot，直到接受或耗尽。

        @param slot 当前声明序交付槽。
        @param batch_no 固定的一基 declaration ordinal。
        @return 通过全部 prospective gate 的事务。
        """
        self.state.failed_slot = slot.slot_key
        self.state.attempts_used = 0
        for attempt_index in range(self.request.program.max_slot_attempts):
            try:
                accepted = await self._try_sequence_attempt(slot, batch_no, attempt_index)
            except GenerationAttemptRejected as exc:
                self._consume_attempt(attempt_index, noise=False)
                self._reject(self._sequence_rejection_kind(exc.kind))
            except DedupGroupRejected:
                self._consume_attempt(attempt_index, noise=False)
                self._reject("dedup")
            except ProviderRetryableError:
                self._consume_attempt(attempt_index, noise=False)
                self._reject("provider_retryable_exhausted")
            except ContextOverflowError:
                self._consume_attempt(attempt_index, noise=False)
                self._reject("context_overflow")
            except OutputTruncatedError:
                self._consume_attempt(attempt_index, noise=False)
                self._reject("output_truncated")
            else:
                self._consume_attempt(attempt_index, noise=False)
                return accepted
        raise DeliveryError(
            "sequence_delivery_exhausted",
            slot.slot_key,
            self.request.program.max_slot_attempts,
        )

    async def _try_sequence_attempt(
        self,
        slot: "DeliverySlot",
        batch_no: int,
        attempt_index: int,
    ) -> _AcceptedSequenceAttempt:
        """执行一个 sequence attempt 的完整 prospective 链。

        @param slot 当前交付槽。
        @param batch_no 固定声明序批号。
        @param attempt_index 零基重试序号。
        @return 尚未提交的接受事务。
        """
        traces = await self._generate_traces(slot, attempt_index)
        projections = self._project_traces(slot, traces)
        witnesses = self._projection_witnesses(projections)
        transaction = self._transaction(slot, projections)
        context = self._context(slot.slot_key, attempt_index, "dedup", batch_no)
        token = await self._dedup_probe(transaction, context)
        counters = await self._run_downstream(transaction, slot, attempt_index, batch_no)
        try:
            rows = tuple(
                self.services.emitter.assemble_sequence(SequenceAssemblyRequest(
                    program=self.request.program,
                    schema_engine=self.generation.schema_engine,
                    item=item,
                    projection=projection,
                    batch_no=batch_no,
                ))
                for item, projection in zip(transaction.items, projections, strict=True)
            )
        except GenerationProjectionMismatch:
            raise GenerationAttemptRejected(
                "sequence_projection_mismatch", slot.slot_key
            ) from None
        replays, entries = self._project_replays(slot, rows)
        retained = sum(row.retained_content_bytes for row in rows)
        retained += sum(row.retained_content_bytes for row in replays)
        self._prospective_sequence(witnesses, rows, replays, retained)
        return _AcceptedSequenceAttempt(
            rows, witnesses, replays, entries, token, counters, retained
        )

    @staticmethod
    def _projection_witnesses(projections) -> tuple["ProjectionWitness", ...]:
        """在 attempt-local projector 内容释放前冻结 compact witness。

        @param projections 当前 set 的 ProjectedSequence。
        @return 与声明分支逐位对齐的 ProjectionWitness。
        """
        from labelkit.operators.generation.project import projection_witness

        return tuple(projection_witness(item) for item in projections)

    async def _generate_traces(self, slot: "DeliverySlot", attempt_index: int):
        """调用 generation 高层槽入口。

        @param slot 当前交付槽。
        @param attempt_index 零基尝试序号。
        @return 与交付 variant 声明序一致的 EventTrace tuple。
        """
        from labelkit.operators.generation.scenario import _generate_validated_slot_traces

        started = time.perf_counter()
        try:
            return await _generate_validated_slot_traces(
                self.request.program,
                self.request.plan,
                slot,
                attempt_index,
                self.generation,
            )
        finally:
            self.generation.metrics.add_stage_time("generate", time.perf_counter() - started)

    def _project_traces(self, slot: "DeliverySlot", traces) -> tuple["ProjectedSequence", ...]:
        """按槽声明序投影并验证 trace cardinality。

        @param slot 当前槽。
        @param traces generation 高层入口产物。
        @return ProjectedSequence tuple。
        """
        from labelkit.operators.generation.project import _project_trace_from_validated_plan

        expected = len(slot.variant_names) if slot.variant_names else 1
        if len(traces) != expected:
            _delivery_contract_error("generation_downstream_contract: trace count mismatch")
        projections = tuple(
            _project_trace_from_validated_plan(ProjectionRequest(
                program=self.request.program,
                plan=self.request.plan,
                slot=slot,
                trace=trace,
            ))
            for trace in traces
        )
        return projections

    def _transaction(self, slot: "DeliverySlot", projections) -> AttemptTransaction:
        """构造含 inherited sequence/frame class 的唯一 items。

        @param slot 当前槽。
        @param projections 按 variant 声明序的投影。
        @return AttemptTransaction。
        """
        variants = slot.variant_names or (None,)
        if len(variants) != len(projections):
            _log.error("generation_downstream_contract",
                       extra={"stage": "generate", "batch": slot.scenario_index})
            raise InternalError("generation_downstream_contract: projection count mismatch")
        items = tuple(
            self._item(slot, variant_name, projection)
            for variant_name, projection in zip(variants, projections, strict=True)
        )
        return AttemptTransaction(
            items=items,
            class_views=self.request.program.class_views,
            projected_sequences=projections,
        )

    def _item(
        self,
        slot: "DeliverySlot",
        variant_name: str | None,
        projection: "ProjectedSequence",
    ) -> PipelineItem:
        """从 pre-downstream projection 构造一个 attempt-local 信封。

        @param slot 当前交付槽。
        @param variant_name 当前声明分支；instruction-only 为 None。
        @param projection 当前分支投影。
        @return inherited classification 已冻结的 PipelineItem。
        """
        classification = Classification(
            label=slot.sequence_class,
            labels=(slot.sequence_class,),
            source="inherited",
            detail={},
        )
        member_classes = {
            member.id: Classification(
                label=row["_meta"]["event"]["frame_class"],
                labels=(row["_meta"]["event"]["frame_class"],),
                source="inherited",
                detail={},
            )
            for member, row in zip(
                projection.main_record.members,
                projection.primary_stream_rows,
                strict=True,
            )
        }
        return PipelineItem(
            record=projection.main_record,
            classification=classification,
            session_id=self._planned_session_id(slot, variant_name),
            member_classifications=member_classes,
        )

    def _planned_session_id(
        self,
        slot: "DeliverySlot",
        variant_name: str | None,
    ) -> str:
        """从唯一冻结 plan branch 取得信封 session 身份。

        @param slot 当前交付槽。
        @param variant_name 当前声明分支；instruction-only 为 None。
        @return 该 owner branch 的唯一计划 session id。
        """
        key = (slot.slot_key, variant_name)
        branches = [block[key] for block in self.request.plan.blocks if key in block]
        sessions = {event.session_id for event in branches[0]} if len(branches) == 1 else set()
        if len(branches) != 1 or not branches[0] or len(sessions) != 1:
            _log.error("generation_downstream_contract",
                       extra={"stage": "generate", "batch": slot.scenario_index})
            raise InternalError("generation_downstream_contract: invalid planned session")
        return next(iter(sessions))

    async def _dedup_probe(self, transaction: AttemptTransaction, context: RunContext):
        """无突变地探测当前 whole set 并给 items 写 prospective unique 结论。

        @param transaction 当前 attempt 唯一信封真值。
        @param context dedup 专用稳定 RunContext。
        @return 一次性 group commit token。
        """
        records = tuple(item.record for item in transaction.items)
        exempt = frozenset((left.id, right.id) for left, right in combinations(records, 2))
        profile = (
            self.generation.config.dedup.semantic_embedding
            if self.generation.config.dedup.semantic
            else None
        )
        started = time.perf_counter()
        try:
            token = await self.services.dedup.group_probe(
                DedupGroupRequest(records=records, exempt_pairs=exempt, embedding_profile=profile),
                context,
            )
        finally:
            self.generation.metrics.add_stage_time("dedup", time.perf_counter() - started)
        for item, exact in zip(transaction.items, token.exact_features, strict=True):
            item.dedup = DedupInfo(kind="unique", cluster_key=exact[:16], kept_id=None)
        return token

    async def _run_downstream(
        self,
        transaction: AttemptTransaction,
        slot: "DeliverySlot",
        attempt_index: int,
        batch_no: int,
    ) -> dict[str, int]:
        """顺序试算全部开启协作者并累计可回滚 dataset counters。

        @param transaction 当前 attempt 唯一信封真值。
        @param slot 当前交付槽。
        @param attempt_index 零基尝试序号。
        @param batch_no 固定声明序批号。
        @return 只有 whole set commit 后才可合并的计数。
        """
        counters: dict[str, int] = {}
        attempt_config = replace(
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
            context.cfg = attempt_config
            request = DownstreamAttemptRequest(transaction=transaction, run_context=context)
            result = await self._run_collaborator(stage, collaborator, request)
            self._merge_local(counters, result.dataset_counters)
            if not result.accepted or result.rejected_stage != (None if result.accepted else stage):
                raise GenerationAttemptRejected(stage, slot.slot_key)
        return counters

    async def _run_collaborator(self, stage: str, collaborator, request):
        """计时执行一个 attempt-local 下游协作者。

        @param stage 冻结阶段名。
        @param collaborator 下游协作者。
        @param request 当前 attempt 请求。
        @return DownstreamAttemptResult。
        """
        started = time.perf_counter()
        try:
            return await collaborator.run_attempt(request)
        finally:
            self.generation.metrics.add_stage_time(stage, time.perf_counter() - started)

    def _project_replays(
        self,
        slot: "DeliverySlot",
        rows: tuple[SequenceRows, ...],
    ) -> tuple[tuple["ReplayRows", ...], tuple]:
        """只从当前最终 source rows 派生匹配的计划 replay。

        @param slot 当前槽。
        @param rows 当前槽最终 SequenceRows。
        @return ReplayRows tuple 与 source lookup 新条目。
        """
        from labelkit.operators.generation.project import _project_replay_from_validated_plan

        variants = slot.variant_names or (None,)
        entries = tuple(((slot.slot_key, variant), row)
                        for variant, row in zip(variants, rows, strict=True))
        sources = dict(entries)
        projected = []
        for layout in self.request.plan.replay_layouts:
            key = (layout.source_slot_key, layout.source_variant_name)
            if key not in sources:
                continue
            projected.append(_project_replay_from_validated_plan(ReplayProjectionRequest(
                program=self.request.program,
                plan=self.request.plan,
                layout=layout,
                source=sources[key],
            )))
        return tuple(projected), entries

    def _prospective_sequence(self, witnesses, rows, replays, retained: int) -> None:
        """在任何 commit 前执行 CrossView 与 retained-content gate。

        @param witnesses 当前 set 的 compact 源证明。
        @param rows 当前 set 的最终 rows。
        @param replays 当前 source 的最终 replay。
        @param retained 当前 set 与 replay 合计费用。
        @return None。
        """
        prospective = self.state.retained_bytes + retained
        source = tuple((*self.state.witnesses, *witnesses))
        self._reconcile(_ReconcileOverride(
            projection_witnesses=source,
            sequences=tuple((*self.state.sequences, *rows)),
            replays=tuple((*self.state.replays, *replays)),
            retained_content_bytes=prospective,
        ))
        if prospective > self.request.program.limits.retained_content_bytes:
            raise GenerationAttemptRejected("sequence_memory_budget", self.state.failed_slot or "")

    def _commit_sequence_attempt(self, accepted: _AcceptedSequenceAttempt) -> None:
        """在无 await 临界区提交 dedup、dataset、rows 与 replay。

        @param accepted 已通过全部 prospective gate 的事务。
        @return None。
        """
        self.services.dedup.group_commit(accepted.dedup_token)
        self.generation.metrics.merge_counts(accepted.dataset_counters)
        self.state.witnesses.extend(accepted.witnesses)
        self.state.sequences.extend(accepted.rows)
        for key, row in accepted.source_entries:
            self.state.sources[key] = row
        self.state.replays.extend(accepted.replays)
        self.state.retained_bytes += accepted.retained_bytes

    async def _deliver_noise_slots(self) -> None:
        """在全部 primary 接受后按 NoiseSlot 序精确交付 noise。"""
        dedup = self.generation.config.dedup
        similarity = SimilarityFilter(
            threshold=dedup.minhash_threshold,
            num_perm=dedup.minhash_num_perm,
            ngram=dedup.ngram,
        )
        for sequence in self.state.sequences:
            for row in sequence.primary_stream_rows:
                similarity.add(canonical_json(row["payload"]))
        for slot in self.request.plan.noise_slots:
            await self._accept_noise_slot(slot, similarity)

    async def _accept_noise_slot(self, slot, similarity: SimilarityFilter) -> None:
        """有界重试一个 noise slot。

        @param slot 当前 NoiseSlot。
        @param similarity attempt-local noise 相似度索引。
        @return None。
        """
        slot_key = f"noise/{slot.ordinal:06d}"
        self.state.failed_slot = slot_key
        self.state.attempts_used = 0
        for attempt_index in range(self.request.program.max_slot_attempts):
            try:
                row, payload_digest, signature, retained = await self._try_noise(
                    slot, attempt_index, similarity
                )
            except GenerationAttemptRejected as exc:
                self._consume_attempt(attempt_index, noise=True)
                self._reject(self._noise_rejection_kind(exc.kind))
                continue
            except ProviderRetryableError:
                self._consume_attempt(attempt_index, noise=True)
                self._reject("noise_provider_retryable_exhausted")
                continue
            except ContextOverflowError:
                self._consume_attempt(attempt_index, noise=True)
                self._reject("noise_context_overflow")
                continue
            except OutputTruncatedError:
                self._consume_attempt(attempt_index, noise=True)
                self._reject("noise_output_truncated")
                continue
            self._consume_attempt(attempt_index, noise=True)
            similarity.commit(signature)
            self.state.noise_payload_digests.append(payload_digest)
            self.state.noise_rows.append(row)
            self.state.retained_bytes += retained
            return
        raise DeliveryError(
            "sequence_delivery_exhausted",
            slot_key,
            self.request.program.max_slot_attempts,
        )

    async def _try_noise(self, slot, attempt_index: int, similarity: SimilarityFilter):
        """渲染、盲审、去重并 prospective 对账一条 noise。

        @param slot 当前 NoiseSlot。
        @param attempt_index 零基尝试序号。
        @param similarity 已预载 primary 与已接受 noise 的过滤器。
        @return 最终 row、冻结源 payload、待提交签名与 retained bytes。
        """
        from labelkit.operators.generation.evaluate import evaluate_noise
        from labelkit.operators.generation.project import noise_payload_digest, project_noise
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
            raise GenerationAttemptRejected("noise_semantic", f"noise/{slot.ordinal:06d}")
        novel, signature = similarity.probe(canonical_json(payload))
        if not novel:
            raise GenerationAttemptRejected("noise_similarity", f"noise/{slot.ordinal:06d}")
        projection = NoiseProjectionRequest(
            program=self.request.program,
            run_id=self.request.run_id,
            noise_slot=slot,
            payload=payload,
        )
        row = project_noise(projection)
        source_digest = noise_payload_digest(projection.payload)
        retained = len(canonical_delivery_row(row)) + 1
        self._prospective_noise(row, source_digest, retained)
        return row, source_digest, signature, retained

    def _noise_render_request(self, slot, attempt_index: int) -> NoiseRenderRequest:
        """构造 noise 渲染请求且不暴露 primary 内容。

        @param slot 当前 NoiseSlot。
        @param attempt_index 零基尝试序号。
        @return NoiseRenderRequest。
        """
        spec = self.request.program.noise
        frame = self.request.program.frame_classes.get(slot.frame_class)
        if spec is None or frame is None:
            _delivery_contract_error("generation_downstream_contract: invalid noise slot")
        return NoiseRenderRequest(
            semantic_profile=self.request.program.semantic_profile,
            noise_slot=slot,
            noise_spec=spec,
            frame_spec=frame,
            class_descriptions=self._class_descriptions(),
            frame_descriptions=self._frame_descriptions(),
            attempt_index=attempt_index,
            limits=self.request.program.limits,
        )

    def _noise_evaluation_request(self, payload, slot, attempt_index: int) -> NoiseEvaluationRequest:
        """构造与结构判定独立的 noise 盲审请求。

        @param payload 已通过完整 frame Schema 的对象。
        @param slot 当前 NoiseSlot。
        @param attempt_index 零基尝试序号。
        @return NoiseEvaluationRequest。
        """
        return NoiseEvaluationRequest(
            evaluation_profile=self.request.program.evaluation_profile,
            payload=payload,
            planned_topic=slot.topic,
            class_descriptions=self._class_descriptions(),
            frame_descriptions=self._frame_descriptions(),
            attempt_index=attempt_index,
            limits=self.request.program.limits,
        )

    def _prospective_noise(self, row, payload_digest: str, retained: int) -> None:
        """在 similarity commit 前验证 noise 内存与 CrossView。

        @param row 当前最终 noise row。
        @param payload_digest 当前已通过语义 gate 的 compact 源摘要。
        @param retained 当前行 canonical JSONL 费用。
        @return None。
        """
        prospective = self.state.retained_bytes + retained
        self._reconcile(_ReconcileOverride(
            noise_payload_digests=tuple((*self.state.noise_payload_digests, payload_digest)),
            noise_rows=tuple((*self.state.noise_rows, row)),
            retained_content_bytes=prospective,
        ))
        if prospective > self.request.program.limits.retained_content_bytes:
            raise GenerationAttemptRejected("noise_memory_budget", self.state.failed_slot or "")

    def _reconcile(self, override: _ReconcileOverride | None = None) -> None:
        """调用唯一 CrossViewReconciler，并把 mismatch 归当前 attempt。

        @param override 可选 prospective 前缀覆盖；None 使用已提交状态。
        @return None。
        """
        from labelkit.operators.generation.project import (
            reconcile_prospective_views,
            reconcile_views,
        )

        active = override or _ReconcileOverride()
        request = ReconcileRequest(
            program=self.request.program,
            plan=self.request.plan,
            run_id=self.request.run_id,
            projection_witnesses=(tuple(self.state.witnesses)
                                  if active.projection_witnesses is None
                                  else active.projection_witnesses),
            sequences=(tuple(self.state.sequences)
                       if active.sequences is None else active.sequences),
            noise_payload_digests=(tuple(self.state.noise_payload_digests)
                                   if active.noise_payload_digests is None
                                   else active.noise_payload_digests),
            noise_rows=(tuple(self.state.noise_rows)
                        if active.noise_rows is None else active.noise_rows),
            replays=(tuple(self.state.replays)
                     if active.replays is None else active.replays),
            retained_content_bytes=(self.state.retained_bytes
                                    if active.retained_content_bytes is None
                                    else active.retained_content_bytes),
        )
        try:
            if override is None:
                reconcile_views(request)
            else:
                reconcile_prospective_views(request)
        except GenerationProjectionMismatch:
            raise GenerationAttemptRejected("reconcile", self.state.failed_slot or "") from None

    def _ordered_stream_rows(self) -> tuple[Mapping[str, object], ...]:
        """按冻结 artifact timestamp 全局稳定排序最终 stream 行。

        @return primary、noise、replay 的唯一最终行序。
        """
        rows = [row for sequence in self.state.sequences for row in sequence.primary_stream_rows]
        rows.extend(self.state.noise_rows)
        rows.extend(row for replay in self.state.replays for row in replay.rows)
        return tuple(sorted(rows, key=lambda row: row["_meta"]["event"]["timestamp"]))

    def _success_report(self, stream_rows) -> dict:
        """组装成功 report；digest 与 commit 标志仍是 M11 占位。

        @param stream_rows 最终全局时间序 stream rows。
        @return 顶层 report 对象。
        """
        sequence = self._sequence_report(stream_rows)
        return {
            "run": self._run_report(exit_code=0),
            "counts": self._counts_report(),
            "schema_engine": self._schema_report(),
            "generate": {"sequence": sequence},
            "trace": self._trace_report(),
            "llm_usage": self._usage_report(),
            "timing": self._timing_report(),
        }

    def _sequence_report(self, stream_rows) -> dict:
        """按冻结键序组装 report.generate.sequence。

        @param stream_rows 最终时间序行。
        @return sequence report block。
        """
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
        """组装无 partial-delivery 的 sequence run block。

        @param exit_code 本次终态退出码。
        @return run report block。
        """
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
        """组装 sequence generate-only 的守恒计数。

        @return 与已提交 main rows 对齐的 counts block。
        """
        delivered = len(self.state.sequences)
        return {
            "scanned": 0,
            "ingested": 0,
            "bad_input": 0,
            "dropped_dup": 0,
            "dropped_lowq": 0,
            "dropped_verify": 0,
            "failed": 0,
            "generated": delivered,
            "emitted": delivered,
        }

    def _schema_report(self) -> dict:
        """读取用户 Schema 调用的既有 resolved-at 账本。

        @return schema_engine report block。
        """
        zero = {"l0_or_clean": 0, "l1": 0, "l3_1": 0, "l3_2": 0, "rejected": 0}
        stats = getattr(self.generation.schema_engine, "stats", None)
        return {"resolved_at": dict(stats) if stats else zero}

    def _sequence_calls(self) -> dict[str, int]:
        """读取七个 generation family 的唯一逻辑入口计数。

        @return 冻结键序的 sequence_calls。
        """
        counters = self.generation.metrics.counters
        return {
            key: int(counters.get(f"generate.sequence.calls.{key}", 0))
            for key in _SEQUENCE_CALL_KEYS
        }

    def _planned_sequences(self) -> int:
        """计算声明序精确 primary sequence 数。

        @return declared variant 总数或 instruction slot 数。
        """
        return sum(len(slot.variant_names) if slot.variant_names else 1
                   for slot in self.request.plan.delivery_slots)

    def _by_pattern(self) -> dict:
        """按 pattern 与 variant 声明序组装计划/交付计数。

        @return instruction-only 为空，declared 为完整零基字典。
        """
        delivered: dict[tuple[str, str], int] = {}
        for sequence in self.state.sequences:
            truth = sequence.main_row.get("_meta", {}).get("generation", {})
            key = (truth.get("pattern"), truth.get("variant"))
            if all(isinstance(part, str) for part in key):
                delivered[key] = delivered.get(key, 0) + 1
        output: dict = {}
        for source in self.request.program.counterfactual_sets:
            pattern = output.setdefault(source.pattern, {})
            for variant in source.variants:
                current = pattern.setdefault(variant.name, {"planned": 0, "delivered": 0})
                current["planned"] += source.count
        for (pattern_name, variant_name), count in delivered.items():
            pattern = output.get(pattern_name)
            if pattern is None or variant_name not in pattern:
                _delivery_contract_error("generation_downstream_contract: unknown report row")
            pattern[variant_name]["delivered"] += count
        return output

    def _trace_report(self) -> dict:
        """读取现有 EventLog 的运行计数，不打开新通道。

        @return trace report block。
        """
        event_log = getattr(self.generation.metrics, "event_log", None)
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
        """组装不含凭据值的按 profile 用量。

        @return 活跃 profile 的 llm_usage block。
        """
        usage = getattr(self.generation.llm, "usage_by_profile", {}) or {}
        report: dict = {}
        for name, value in usage.items():
            entry = self._usage_entry(value)
            if entry is not None:
                report[name] = entry
        return report

    @staticmethod
    def _usage_entry(usage) -> dict | None:
        """把一个运行期 profile 累加器投影为 secret-safe 对象。

        @param usage M9 ProfileUsage。
        @return 零活动时 None，否则用量对象。
        """
        keys = getattr(usage, "keys", None) or {}
        parked_calls = getattr(usage, "parked_calls", 0)
        parked_ms = getattr(usage, "parked_ms", 0)
        active = any((usage.calls, usage.retries, usage.prompt_tokens,
                      usage.completion_tokens, usage.est_cost_usd is not None,
                      len(keys) > 1, parked_calls, parked_ms))
        if not active:
            return None
        entry = {
            "calls": usage.calls,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "retries": usage.retries,
        }
        if usage.est_cost_usd is not None:
            entry["est_cost_usd"] = usage.est_cost_usd
        if len(keys) > 1:
            entry["keys"] = {
                name: {"calls": row.calls, "rate_limited": row.rate_limited,
                       "disabled": row.disabled}
                for name, row in sorted(keys.items())
            }
        if len(keys) > 1 or parked_calls or parked_ms:
            entry.update({"parked_calls": parked_calls, "parked_ms": parked_ms})
        return entry

    def _timing_report(self) -> dict:
        """组装运行墙钟与 MetricsSink 阶段累计。

        @return timing report block。
        """
        return {
            "wall_s": round(time.perf_counter() - self.started_perf, 3),
            "per_stage_s": {
                name: round(seconds, 3)
                for name, seconds in self.generation.metrics.stage_times.items()
            },
        }

    def write_failed_report(self, exc: BaseException) -> None:
        """best-effort 写 data-free failed report 且不遮蔽主异常。

        @param exc 触发终态的原始异常。
        @return None。
        """
        report = {
            "run_attempt_id": self.request.run_attempt_id,
            "run_id": self.request.run_id,
            "artifacts_committed": False,
            "failed_slot": self.state.failed_slot,
            "attempts_used": self.state.attempts_used,
            "terminal_error_kind": self._terminal_kind(exc),
            "llm_usage": self._usage_report(),
            "rejected_attempts": dict(self.state.rejected),
        }
        try:
            self.services.emitter.write_failed_report(report)
        except LabelKitError:
            _log.error("generation_failed_report_io", extra={"stage": "run", "batch": 0})

    def _context(
        self,
        slot_identity: str,
        attempt_index: int,
        purpose: str,
        batch_no: int,
    ) -> RunContext:
        """派生只更换 rng 与 batch_no 的身份一致 RunContext。

        @param slot_identity 当前 slot 身份。
        @param attempt_index 零基 attempt。
        @param purpose 随机流目的域。
        @param batch_no 固定 declaration ordinal。
        @return 与 GenerationServices 根身份一致的 RunContext。
        """
        return RunContext(
            cfg=self.generation.config,
            llm=self.generation.llm,
            schema_engine=self.generation.schema_engine,
            metrics=self.generation.metrics,
            rng=random.Random(self._attempt_seed(slot_identity, attempt_index, purpose)),
            batch_no=batch_no,
        )

    def _attempt_seed(self, slot_identity: str, attempt_index: int, purpose: str) -> int:
        """按冻结 canonical 材料派生完整整数随机种子。

        @param slot_identity 当前 slot 身份。
        @param attempt_index 零基 attempt。
        @param purpose 随机流目的域。
        @return SHA-256 全宽整数。
        """
        material = canonical_json([
            "labelkit:v1.18",
            "attempt_random",
            [self.request.program.planner_seed, slot_identity, attempt_index, purpose],
        ])
        return int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest(), "big")

    def _class_descriptions(self) -> dict[str, str]:
        """返回仅含闭集名称与描述的 class registry。"""
        return {name: view.description for name, view in self.request.program.class_views.items()}

    def _frame_descriptions(self) -> dict[str, str]:
        """返回仅含闭集名称与描述的 frame registry。"""
        return {name: view.description for name, view in self.request.program.frame_classes.items()}

    @staticmethod
    def _noise_accepted(evaluation) -> bool:
        """判定 noise 四项盲审全部通过且无原因码。

        @param evaluation NoiseSemanticEvaluation。
        @return 全部布尔为真且原因码为空时为 True。
        """
        return bool(
            evaluation.unrelated_to_declared_tasks
            and evaluation.no_executable_task
            and evaluation.realism
            and evaluation.matches_planned_topic
            and not evaluation.reason_codes
        )

    def _reject(self, kind: str) -> None:
        """向闭集拒绝账本增加一次且记录 secret-safe 日志。

        @param kind 冻结 rejected_attempts 键。
        @return None。
        """
        if kind not in self.state.rejected:
            _delivery_contract_error("generation_downstream_contract: unknown rejection kind")
        self.state.rejected[kind] += 1
        _log.info("sequence attempt rejected: slot=%s attempt=%s kind=%s",
                  self.state.failed_slot, self.state.attempts_used - 1, kind,
                  extra={"stage": "generate", "batch": 0})

    def _consume_attempt(self, attempt_index: int, noise: bool) -> None:
        """仅在接受或可恢复拒绝后落账一次 attempt。

        @param attempt_index 当前零基 attempt。
        @param noise 是否为 noise slot。
        @return None。
        """
        self.state.attempts_used = attempt_index + 1
        if noise:
            self.state.noise_attempts += 1
        else:
            self.state.sequence_attempts += 1

    @staticmethod
    def _merge_local(target: dict[str, int], source: Mapping[str, int]) -> None:
        """合并一个协作者返回的非负整数 dataset delta。

        @param target 当前 attempt 局部账本。
        @param source 协作者返回的计数。
        @return None。
        """
        for key, value in source.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                _delivery_contract_error(
                    "generation_downstream_contract: invalid dataset counter"
                )
            target[key] = target.get(key, 0) + value

    @staticmethod
    def _sequence_rejection_kind(kind: str) -> str:
        """把 generation 内部 mismatch 分类映射到 report 闭集。

        @param kind GenerationAttemptRejected.kind。
        @return sequence rejected_attempts 键。
        """
        if kind == "sequence_projection_mismatch":
            return "reconcile"
        if kind not in _REJECTION_KEYS or kind.startswith("noise_"):
            _delivery_contract_error(
                "generation_downstream_contract: invalid sequence rejection"
            )
        return kind

    @staticmethod
    def _noise_rejection_kind(kind: str) -> str:
        """验证 noise rejection 只使用专用桶。

        @param kind GenerationAttemptRejected.kind。
        @return noise rejected_attempts 键。
        """
        allowed = {
            "noise_schema",
            "noise_semantic",
            "noise_similarity",
            "noise_memory_budget",
            "noise_context_overflow",
            "noise_output_truncated",
            "noise_provider_retryable_exhausted",
        }
        if kind == "reconcile":
            return "noise_reconcile"
        if kind not in allowed:
            _delivery_contract_error("generation_downstream_contract: invalid noise rejection")
        return kind

    @staticmethod
    def _terminal_kind(exc: BaseException) -> str:
        """把运行终态折成不含异常正文的固定 kind。

        @param exc 原始终态异常。
        @return failed report 分类。
        """
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

"""v1.18 whole-set delivery 的离线事务、计数与身份测试。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from labelkit.cli.parser import CliOverrides
from labelkit.common.config import load
from labelkit.common.contracts.generation import (
    AttemptTransaction,
    DedupProbeToken,
    DownstreamAttemptRequest,
    DownstreamAttemptResult,
    ProjectedSequence,
    ReplayRows,
    SequenceAssemblyRequest,
    SequenceRows,
)
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import (
    Annotation,
    Classification,
    PipelineItem,
    QualityScore,
    Record,
    RecordRef,
    Usage,
    VerificationResult,
)
from labelkit.common.errors import (
    CircuitBreakerTripped,
    ConfigError,
    ContextOverflowError,
    DeliveryError,
    InternalError,
    LabelKitError,
    OutputTruncatedError,
    ProviderFatalError,
    ProviderRetryableError,
)
from labelkit.operators.dedup import DedupGroupRejected
from labelkit.common.errors import GenerationProjectionMismatch
from labelkit.operators.generation import GenerationAttemptRejected
from labelkit.operators.generation.flat import SimilarityFilter
from labelkit.operators.generation.planner import compile_scenario_plan
from labelkit.operators.generation.project import canonical_json
from labelkit.operators.generation.program import compile_generation_program
from labelkit.operators.annotate import AnnotateStage, class_effective_schema
from labelkit.operators.emitter import SequenceDeliveryEmitter
from labelkit.operators.quality import QualityStage
from labelkit.operators.verify import VerifyStage
import labelkit.orchestration.generation_delivery as delivery_mod
from labelkit.orchestration.generation_delivery import (
    _AcceptedSequenceAttempt,
    _DeliveryController,
    deliver_generation,
    estimate_sequence,
    estimate_sequence_products,
)
from labelkit.orchestration import runtime


_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE = _ROOT / "examples" / "sequence-generation"


class _Metrics:
    """记录 attempt commit 与运行事实的最小观测实现。"""

    def __init__(self):
        self.counters: dict[str, int] = {}
        self.stage_times: dict[str, float] = {}
        self.merged: list[dict[str, int]] = []
        self.event_log = None
        self._captured: dict[str, int] | None = None

    def add_stage_time(self, stage: str, seconds: float) -> None:
        """累计阶段时间。"""
        self.stage_times[stage] = self.stage_times.get(stage, 0.0) + seconds

    def merge_counts(self, counts) -> None:
        """只在正式 commit 时合并 dataset counter。"""
        copied = dict(counts)
        self.merged.append(copied)
        for key, value in copied.items():
            self.counters[key] = self.counters.get(key, 0) + value

    @contextmanager
    def capture_counts(self):
        """隔离 attempt-local dataset counter。"""
        assert self._captured is None
        captured: dict[str, int] = {}
        self._captured = captured
        try:
            yield captured
        finally:
            self._captured = None

    def count(self, key: str, value: int = 1) -> None:
        """budget 运行事实直写，其余计数进入当前 attempt。"""
        target = self.counters if key.startswith("budget.") else self._captured
        target = self.counters if target is None else target
        target[key] = target.get(key, 0) + value

    def event(self, *_args, **_kwargs) -> None:
        """接收真实算子事件且不把数据写出测试进程。"""


class _Dedup:
    """记录 group commit 的事务协作者。"""

    def __init__(self):
        self.commits: list[object] = []

    def group_commit(self, token) -> None:
        """记录已消费 token。"""
        self.commits.append(token)


class _Emitter:
    """只记录 failed report 的延迟输出协作者。"""

    def __init__(self):
        self.failed: list[dict] = []

    def write_failed_report(self, report) -> None:
        """保存无内容失败报告。"""
        self.failed.append(dict(report))


@dataclass
class _Config:
    """覆盖 DeliveryController 所需的 ResolvedConfig 形状。"""

    run: object
    trace: object
    dedup: object
    config_digest: str = "c"
    project_digest: str = "p"
    class_views: dict = field(default_factory=dict)
    frame_class_views: dict = field(default_factory=dict)
    frame_schema: object | None = None


def _controller(*, attempts: int = 2, retained: int = 100):
    """构造不接触 LLM 或文件的 DeliveryController。"""
    metrics = _Metrics()
    dedup = _Dedup()
    emitter = _Emitter()
    limits = SimpleNamespace(retained_content_bytes=retained)
    program = SimpleNamespace(
        max_slot_attempts=attempts,
        limits=limits,
        timeline=SimpleNamespace(crossed_primary_sessions=0),
        counterfactual_sets=(),
        class_views={},
        frame_classes={},
        frame_schema=None,
        mode="instruction_only",
        digest="d" * 64,
        planner_seed=7,
    )
    plan = SimpleNamespace(
        blocks=(), delivery_slots=(), noise_slots=(), replay_layouts=(),
        primary_sessions=0, digest="p" * 64,
    )
    paths = SimpleNamespace(
        project="project.toml", project_root="/tmp", input=None, output="out.jsonl",
        report="out.report.json", rejects=None, sidecar=None, trace=None,
        stream="out.stream.jsonl", manifest="out.manifest.json",
        failed_report="out.failed.report.json",
    )
    request = SimpleNamespace(
        program=program, plan=plan, paths=paths,
        run_attempt_id="a" * 32, run_id="b" * 32,
    )
    cfg = _Config(
        run=SimpleNamespace(seed=7, modality="text"),
        trace=SimpleNamespace(enabled=False),
        dedup=SimpleNamespace(
            minhash_threshold=0.85,
            minhash_num_perm=128,
            ngram=5,
        ),
    )
    generation = SimpleNamespace(
        config=cfg, metrics=metrics, schema_engine=SimpleNamespace(stats={}),
        llm=SimpleNamespace(usage_by_profile={}),
    )
    services = SimpleNamespace(
        generation=generation, dedup=dedup, quality=None, annotate=None,
        verify=None, emitter=emitter,
    )
    return _DeliveryController(request, services), metrics, dedup, emitter


def _accepted(*, counters=None, retained: int = 0):
    """返回尚未 commit 的空 set 测试载体。"""
    return _AcceptedSequenceAttempt(
        rows=(), witnesses=(), replays=(), source_entries=(), dedup_token="token",
        dataset_counters=counters or {}, retained_bytes=retained,
    )


def _slot():
    """返回稳定 sequence slot。"""
    return SimpleNamespace(slot_key="set/000000", scenario_index=0)


def _retryable_cases():
    """返回 sequence 可恢复失败与唯一 report 桶。"""
    return (
        (GenerationAttemptRejected("scenario_schema", "slot"), "scenario_schema"),
        (GenerationAttemptRejected("event_schema", "slot"), "event_schema"),
        (GenerationAttemptRejected("post_validator_invalid", "slot"),
         "post_validator_invalid"),
        (GenerationAttemptRejected("post_validator_exception", "slot"),
         "post_validator_exception"),
        (GenerationAttemptRejected("state_transition", "slot"), "state_transition"),
        (GenerationAttemptRejected("frame_schema", "slot"), "frame_schema"),
        (GenerationAttemptRejected("coupling_evaluation", "slot"),
         "coupling_evaluation"),
        (GenerationAttemptRejected("pattern_evaluation", "slot"),
         "pattern_evaluation"),
        (GenerationAttemptRejected("state_evaluation", "slot"), "state_evaluation"),
        (GenerationAttemptRejected("semantic_evaluation", "slot"),
         "semantic_evaluation"),
        (GenerationAttemptRejected("sequence_memory_budget", "slot"),
         "sequence_memory_budget"),
        (GenerationAttemptRejected("quality", "slot"), "quality"),
        (GenerationAttemptRejected("annotate", "slot"), "annotate"),
        (GenerationAttemptRejected("verify", "slot"), "verify"),
        (GenerationAttemptRejected("reconcile", "slot"), "reconcile"),
        (DedupGroupRejected("duplicate"), "dedup"),
        (ProviderRetryableError("retry", "default", 0),
         "provider_retryable_exhausted"),
        (ContextOverflowError("overflow", phase="precheck"), "context_overflow"),
        (OutputTruncatedError("truncated"), "output_truncated"),
    )


@pytest.mark.parametrize(("failure", "bucket"), _retryable_cases())
async def test_each_sequence_failure_retries_whole_set_once(monkeypatch, failure, bucket):
    """每个可恢复边界只消费当前 attempt，下一次从 whole set 重来。"""
    controller, metrics, dedup, _emitter = _controller()
    calls = 0

    async def attempt(slot, batch_no, attempt_index):
        nonlocal calls
        calls += 1
        if attempt_index == 0:
            raise failure
        return _accepted(counters={"generated": 4})

    monkeypatch.setattr(controller, "_try_sequence_attempt", attempt)
    accepted = await controller._accept_sequence_slot(_slot(), 1)

    assert accepted.dataset_counters == {"generated": 4}
    assert calls == 2
    assert controller.state.sequence_attempts == 2
    assert controller.state.attempts_used == 2
    assert controller.state.rejected[bucket] == 1
    assert metrics.counters == {}
    assert dedup.commits == []


@pytest.mark.parametrize("failure", (
    ProviderFatalError("fatal", "default"),
    CircuitBreakerTripped("broken"),
    InternalError("generation_downstream_contract"),
    asyncio.CancelledError(),
))
async def test_terminal_sequence_failure_does_not_consume_or_retry(monkeypatch, failure):
    """run-terminal 异常原样穿透且不消耗 slot attempt。"""
    controller, metrics, dedup, _emitter = _controller()
    calls = 0

    async def attempt(slot, batch_no, attempt_index):
        nonlocal calls
        calls += 1
        raise failure

    monkeypatch.setattr(controller, "_try_sequence_attempt", attempt)
    with pytest.raises(type(failure)) as caught:
        await controller._accept_sequence_slot(_slot(), 1)
    assert caught.value is failure
    assert calls == 1
    assert controller.state.sequence_attempts == 0
    assert controller.state.attempts_used == 0
    assert metrics.counters == {}
    assert dedup.commits == []


class _ScriptedCollaborator:
    """先抛指定异常，随后返回 accepted 的下游 seam。"""

    def __init__(self, failure):
        self.failure = failure
        self.calls = 0

    async def run_attempt(self, request):
        """按调用序执行脚本。"""
        self.calls += 1
        if self.calls == 1:
            raise self.failure
        return DownstreamAttemptResult(True, None, {"accepted": 1})


@pytest.mark.parametrize("stage", ("quality", "annotate", "verify"))
@pytest.mark.parametrize("fatal", (False, True))
async def test_controller_applies_terminal_matrix_to_each_downstream_collaborator(
        monkeypatch, stage, fatal):
    """每个 concrete seam 后的 fatal 零 attempt，retryable 恰消费一次再重试。"""
    failure = (ProviderFatalError("fatal", "default") if fatal else
               ProviderRetryableError("retry", "default", 0))
    controller, metrics, dedup, _emitter = _controller()
    collaborator = _ScriptedCollaborator(failure)
    setattr(controller.services, stage, collaborator)
    transaction = AttemptTransaction((), {}, ())

    async def attempt(slot, batch_no, attempt_index):
        counters = await controller._run_downstream(
            transaction, slot, attempt_index, batch_no
        )
        return _accepted(counters=counters)

    monkeypatch.setattr(controller, "_try_sequence_attempt", attempt)
    if fatal:
        with pytest.raises(ProviderFatalError) as caught:
            await controller._accept_sequence_slot(_slot(), 1)
        assert caught.value is failure
        assert collaborator.calls == 1
        assert controller.state.sequence_attempts == 0
    else:
        accepted = await controller._accept_sequence_slot(_slot(), 1)
        assert accepted.dataset_counters == {"accepted": 1}
        assert collaborator.calls == 2
        assert controller.state.sequence_attempts == 2
        assert controller.state.rejected["provider_retryable_exhausted"] == 1
    assert metrics.counters == {}
    assert dedup.commits == []


def test_controller_attempt_seed_ignores_mutated_resolved_config_seed():
    """DeliveryController 的所有目的域随机流只以 program.planner_seed 为根。"""
    controller, _metrics, _dedup, _emitter = _controller()
    controller.generation.config.run.seed = 999_999
    material = canonical_json([
        "labelkit:v1.18",
        "attempt_random",
        [controller.request.program.planner_seed, "slot", 2, "annotate"],
    ])
    expected = int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest(), "big")
    assert controller._attempt_seed("slot", 2, "annotate") == expected


class _ScriptedSemanticDedup:
    """先失败后返回合法 group token 的 semantic dedup seam。"""

    def __init__(self, failure):
        self.failure = failure
        self.calls = 0
        self.commits: list[object] = []

    async def group_probe(self, request, context):
        """首调抛错，次调返回与 records 对齐的 token。"""
        self.calls += 1
        if self.calls == 1:
            raise self.failure
        size = len(request.records)
        return DedupProbeToken(
            "capability", 0, tuple("d" * 64 for _ in range(size)),
            tuple("e" * 64 for _ in range(size)), tuple(None for _ in range(size)), (),
        )

    def group_commit(self, token) -> None:
        """记录正式 commit。"""
        self.commits.append(token)


@pytest.mark.parametrize("fatal", (False, True))
async def test_controller_applies_terminal_matrix_to_semantic_dedup(monkeypatch, fatal):
    """semantic group probe 的 fatal 零 attempt，retryable 消耗一次后 whole-set retry。"""
    failure = (ProviderFatalError("fatal", "embedding") if fatal else
               ProviderRetryableError("retry", "embedding", 0))
    controller, metrics, _old_dedup, _emitter = _controller()
    controller.generation.config.dedup = SimpleNamespace(
        semantic=True, semantic_embedding="embedding"
    )
    dedup = _ScriptedSemanticDedup(failure)
    controller.services.dedup = dedup
    record = Record(
        "f" * 32, "text", "unique", {"text": "unique"}, None, None,
        RecordRef("", None, None, ()),
    )
    transaction = AttemptTransaction((PipelineItem(record),), {}, ())

    async def attempt(slot, batch_no, attempt_index):
        context = controller._context(slot.slot_key, attempt_index, "dedup", batch_no)
        token = await controller._dedup_probe(transaction, context)
        return _AcceptedSequenceAttempt((), (), (), (), token, {"generated": 1}, 0)

    monkeypatch.setattr(controller, "_try_sequence_attempt", attempt)
    if fatal:
        with pytest.raises(ProviderFatalError) as caught:
            await controller._accept_sequence_slot(_slot(), 1)
        assert caught.value is failure
        assert controller.state.sequence_attempts == 0
        assert dedup.calls == 1
    else:
        accepted = await controller._accept_sequence_slot(_slot(), 1)
        assert accepted.dataset_counters == {"generated": 1}
        assert controller.state.sequence_attempts == 2
        assert dedup.calls == 2
    assert dedup.commits == []
    assert metrics.counters == {}


@pytest.mark.parametrize(("failure", "bucket"), (
    (GenerationAttemptRejected("noise_schema", "noise"), "noise_schema"),
    (GenerationAttemptRejected("noise_semantic", "noise"), "noise_semantic"),
    (GenerationAttemptRejected("noise_similarity", "noise"), "noise_similarity"),
    (GenerationAttemptRejected("noise_memory_budget", "noise"), "noise_memory_budget"),
    (ProviderRetryableError("retry", "default", 0),
     "noise_provider_retryable_exhausted"),
    (ContextOverflowError("overflow", phase="precheck"), "noise_context_overflow"),
    (OutputTruncatedError("truncated"), "noise_output_truncated"),
    (GenerationAttemptRejected("reconcile", "noise"), "noise_reconcile"),
))
async def test_each_noise_failure_retries_without_similarity_commit(
        monkeypatch, failure, bucket):
    """noise 的专用桶、attempt 与 similarity commit 保持同一边界。"""
    controller, _metrics, _dedup, _emitter = _controller()
    similarity = SimilarityFilter()
    signature = similarity.probe("unique noise")[1]
    calls = 0

    async def attempt(slot, attempt_index, current):
        nonlocal calls
        calls += 1
        if attempt_index == 0:
            raise failure
        payload = {"value": "unique noise"}
        return {"payload": payload}, "d" * 64, signature, 1

    monkeypatch.setattr(controller, "_try_noise", attempt)
    await controller._accept_noise_slot(
        SimpleNamespace(ordinal=0, session_id="noise_000000"), similarity,
    )
    assert calls == 2
    assert controller.state.noise_attempts == 2
    assert controller.state.rejected[bucket] == 1
    assert len(controller.state.noise_rows) == 1
    assert similarity.probe("unique noise")[0] is False


def test_commit_is_the_only_dedup_dataset_and_row_mutation_boundary():
    """accepted carrier 在显式 commit 前不改变任一正式状态。"""
    controller, metrics, dedup, _emitter = _controller()
    rows = SequenceRows({"row": 1}, (), 7)
    replay = ReplayRows(({"replay": 1},), 5)
    accepted = _AcceptedSequenceAttempt(
        rows=(rows,), witnesses=(), replays=(replay,),
        source_entries=((('slot', None), rows),), dedup_token="capability",
        dataset_counters={"generated": 1}, retained_bytes=12,
    )
    assert metrics.counters == {}
    assert dedup.commits == []
    assert controller.state.sequences == []

    controller._commit_sequence_attempt(accepted)

    assert dedup.commits == ["capability"]
    assert metrics.counters == {"generated": 1}
    assert controller.state.sequences == [rows]
    assert controller.state.replays == [replay]
    assert controller.state.retained_bytes == 12


async def test_m11_projection_mismatch_retries_whole_set_before_any_commit(monkeypatch):
    """M11 首次终检失败映射 reconcile，第二次成功才提交唯一 token 与 rows。"""
    controller, metrics, dedup, _emitter = _controller(attempts=2)
    record = Record(
        "f" * 32, "text", "value", {"text": "value"}, None, None,
        RecordRef("", None, None, ()),
    )
    projection = SimpleNamespace()
    transaction = AttemptTransaction(
        (PipelineItem(record),), {}, (projection,)
    )

    class FlakyEmitter:
        def __init__(self):
            self.calls = 0

        def assemble_sequence(self, request):
            self.calls += 1
            if self.calls == 1:
                raise GenerationProjectionMismatch("schema")
            return SequenceRows({"attempt": self.calls}, (), 1)

    emitter = FlakyEmitter()
    controller.services.emitter = emitter
    probes = 0

    async def probe(_transaction, _context):
        nonlocal probes
        probes += 1
        return f"token-{probes}"

    async def generate(*_args):
        return (1,)

    async def downstream(*_args):
        return {"generated": 1}

    monkeypatch.setattr(controller, "_generate_traces", generate)
    monkeypatch.setattr(controller, "_project_traces", lambda *_args: (projection,))
    monkeypatch.setattr(controller, "_projection_witnesses", lambda *_args: ("witness",))
    monkeypatch.setattr(controller, "_transaction", lambda *_args: transaction)
    monkeypatch.setattr(controller, "_dedup_probe", probe)
    monkeypatch.setattr(controller, "_run_downstream", downstream)
    monkeypatch.setattr(controller, "_project_replays", lambda *_args: ((), ()))
    monkeypatch.setattr(controller, "_prospective_sequence", lambda *_args: None)
    slot = SimpleNamespace(
        slot_key="set/000000", scenario_index=0, variant_names=()
    )
    accepted = await controller._accept_sequence_slot(slot, 1)

    assert emitter.calls == probes == 2
    assert controller.state.sequence_attempts == 2
    assert controller.state.rejected["reconcile"] == 1
    assert dedup.commits == [] and metrics.merged == []
    assert controller.state.sequences == []
    controller._commit_sequence_attempt(accepted)
    assert dedup.commits == ["token-2"]
    assert metrics.merged == [{"generated": 1}]
    assert [row.main_row for row in controller.state.sequences] == [{"attempt": 2}]


async def test_failed_attempt_keeps_runtime_facts_but_rolls_back_all_dataset_state(
        monkeypatch):
    """Schema/usage/retry/trace 累积；item、annotation、token、rows 与计数只提交末次。"""
    controller, metrics, dedup, _emitter = _controller()
    controller.generation.schema_engine.stats = {
        "l0_or_clean": 0, "l1": 0, "l3_1": 0, "l3_2": 0, "rejected": 0,
    }
    usage = SimpleNamespace(
        calls=0, retries=0, prompt_tokens=0, completion_tokens=0,
        est_cost_usd=None, keys={}, parked_calls=0, parked_ms=0,
    )
    controller.generation.llm.usage_by_profile = {"default": usage}
    metrics.event_log = SimpleNamespace(events_written=0, dropped_events=0, closed=False)
    failed_items: list[PipelineItem] = []
    failed_rows: list[SequenceRows] = []

    async def attempt(slot, batch_no, attempt_index):
        controller.generation.schema_engine.stats["l3_1"] += 1
        usage.calls += 1
        usage.retries += 1
        usage.prompt_tokens += 10
        usage.completion_tokens += 3
        metrics.event_log.events_written += 1
        metrics.count("budget.truncations.annotate")
        if attempt_index == 0:
            item = PipelineItem(Record(
                "0" * 32, "text", "failed", {"text": "failed"}, None, None,
                RecordRef("", None, None, ()),
            ))
            item.annotation = Annotation({"attempt": 0}, "model", 1, Usage())
            item.status = "failed"
            failed_items.append(item)
            failed_rows.append(SequenceRows({"attempt": 0}, (), 1))
            with metrics.capture_counts():
                metrics.count("generated", 99)
                raise GenerationAttemptRejected("quality", slot.slot_key)
        rows = SequenceRows({"attempt": 1}, (), 1)
        return _AcceptedSequenceAttempt(
            (rows,), (), (), (((slot.slot_key, None), rows),),
            "accepted-token", {"generated": 1}, 1,
        )

    monkeypatch.setattr(controller, "_try_sequence_attempt", attempt)
    accepted = await controller._accept_sequence_slot(_slot(), 1)
    assert controller.state.sequences == []
    assert controller.state.sources == {}
    assert dedup.commits == []
    assert metrics.counters == {"budget.truncations.annotate": 2}
    assert controller.generation.schema_engine.stats["l3_1"] == 2
    assert (usage.calls, usage.retries, usage.prompt_tokens, usage.completion_tokens) == (
        2, 2, 20, 6,
    )
    assert metrics.event_log.events_written == 2
    assert failed_items[0].annotation.output == {"attempt": 0}
    assert failed_rows[0] not in controller.state.sequences

    controller._commit_sequence_attempt(accepted)
    assert dedup.commits == ["accepted-token"]
    assert metrics.counters == {
        "budget.truncations.annotate": 2, "generated": 1,
    }
    assert [row.main_row["attempt"] for row in controller.state.sequences] == [1]
    assert controller.state.sources == {("set/000000", None): accepted.rows[0]}


def test_retained_cap_accepts_exact_limit_and_rejects_one_utf8_byte(monkeypatch):
    """retained-content gate 使用完整 source 加 replay byte 费用且不裁剪。"""
    controller, metrics, dedup, _emitter = _controller(retained=100)
    reconciled: list[tuple] = []
    monkeypatch.setattr(controller, "_reconcile", lambda *args, **kwargs: reconciled.append(args))

    controller._prospective_sequence((), (), (), 100)
    with pytest.raises(GenerationAttemptRejected) as caught:
        controller._prospective_sequence((), (), (), 101)

    assert caught.value.kind == "sequence_memory_budget"
    assert len(reconciled) == 2
    assert metrics.counters == {}
    assert dedup.commits == []
    assert controller.state.retained_bytes == 0
    assert controller.state.replays == []


def test_reconcile_maps_only_typed_projection_mismatch(monkeypatch):
    """内容投影 mismatch 可恢复，plan/contract InternalError 保持 terminal。"""
    controller, _metrics, _dedup, _emitter = _controller()
    from labelkit.operators.generation import project

    def mismatch(request):
        raise GenerationProjectionMismatch("tampered")

    monkeypatch.setattr(project, "reconcile_views", mismatch)
    with pytest.raises(GenerationAttemptRejected) as caught:
        controller._reconcile()
    assert caught.value.kind == "reconcile"

    terminal = InternalError("generation_downstream_contract: broken plan")
    monkeypatch.setattr(project, "reconcile_views", lambda request: (_ for _ in ()).throw(terminal))
    with pytest.raises(InternalError) as escaped:
        controller._reconcile()
    assert escaped.value is terminal


def test_failed_report_has_exact_secret_free_keyset():
    """failed report 使用 llm_usage 权威键且不包含数据或旧 usage 键。"""
    controller, _metrics, _dedup, emitter = _controller()
    controller.state.failed_slot = "set/000000"
    controller.state.attempts_used = 2
    controller.state.rejected["quality"] = 1
    controller.write_failed_report(ProviderFatalError("sensitive body", "default"))

    report = emitter.failed[0]
    assert tuple(report) == (
        "run_attempt_id", "run_id", "artifacts_committed", "failed_slot",
        "attempts_used", "terminal_error_kind", "llm_usage", "rejected_attempts",
    )
    assert report["terminal_error_kind"] == "provider_fatal"
    assert report["artifacts_committed"] is False
    assert report["rejected_attempts"]["quality"] == 1
    assert "usage" not in report
    assert "sensitive body" not in repr(report)


@pytest.mark.parametrize(("closed", "expected"), (
    (False, {"events": 4, "dropped_events": 1}),
    (True, {"events": 3, "dropped_events": 2}),
))
def test_sequence_trace_report_accounts_for_pending_terminal_event(closed, expected):
    """成功报告预记稍后由 M10 发出的最终 run.end。"""
    controller, _metrics, _dedup, _emitter = _controller()
    controller.generation.config.trace.enabled = True
    controller.generation.metrics.event_log = SimpleNamespace(
        events_written=3, dropped_events=1, closed=closed,
        cfg=SimpleNamespace(path="trace.jsonl"),
    )
    report = controller._trace_report()
    assert report == {
        "enabled": True, "path": "trace.jsonl", **expected,
    }


def _load_products(monkeypatch, project_name: str):
    """经真实 M1、compiler、planner 装载一个教学项目。"""
    monkeypatch.setenv("LABELKIT_DEEPSEEK_KEY", "offline-test-key")
    cfg = load(_EXAMPLE / "config.toml", _EXAMPLE / project_name, CliOverrides())
    program = compile_generation_program(cfg)
    return cfg, program, compile_scenario_plan(program)


def _projection_for_branch(slot, events, ordinal: int) -> ProjectedSequence:
    """构造只供信封身份投影使用的无内容 primary carrier。"""
    members = tuple(
        Record(
            id=f"{ordinal:08x}{index:024x}", modality="text", text="frame",
            raw={"utterance": "frame"}, ui_tree=None, image=None,
            ref=RecordRef("", None, None, ()),
        )
        for index, _event in enumerate(events)
    )
    main = Record(
        id=f"{ordinal:032x}", modality="text", text=None,
        raw={"_meta": {"generation": {"validation_mode": "declared"}}},
        ui_tree=None, image=None, ref=RecordRef("", None, None, ()),
        kind="sequence", members=members,
    )
    rows = tuple(
        {"_meta": {"event": {
            "event_id": member.id, "frame_class": "teaching_frame",
        }}, "payload": {}}
        for member in members
    )
    return ProjectedSequence(main, rows)


def _instruction_projection(plan, ordinal: int = 1):
    """构造 instruction-only 单槽且成员继承 task_request 的投影。"""
    slot = plan.delivery_slots[0]
    events = next(
        block[(slot.slot_key, None)] for block in plan.blocks
        if (slot.slot_key, None) in block
    )
    base = _projection_for_branch(slot, events, ordinal)
    rows = tuple(
        {
            "_meta": {"event": {
                "event_id": member.id,
                "frame_class": "task_request",
            }},
            "payload": {},
        }
        for member in base.main_record.members
    )
    return slot, events, ProjectedSequence(base.main_record, rows)


def _unbudgeted_profiles(cfg):
    """关闭测试 profile 预算，使 oracle 只观察配置消费而不依赖校准器。"""
    return {
        name: replace(profile, context_window=0)
        for name, profile in cfg.llm_profiles.items()
    }


class _HappyDedup:
    """为成功控制器路径提供可提交的 whole-group token。"""

    def __init__(self):
        self.commits = []

    async def group_probe(self, request, context):
        """返回与当前 records 严格对齐的冻结特征。"""
        size = len(request.records)
        return DedupProbeToken(
            f"capability-{size}", len(self.commits),
            tuple(f"digest-{index}" for index in range(size)),
            tuple(f"exact-{index}" for index in range(size)),
            tuple(None for _index in range(size)), (),
        )

    def group_commit(self, token):
        """记录唯一正式提交边界。"""
        self.commits.append(token)


class _HappyCollaborator:
    """返回 accepted 与 attempt-local counter 的下游协作者。"""

    def __init__(self, name):
        self.name = name

    async def run_attempt(self, request):
        """接受 whole set，且不直接修改全局计数。"""
        return DownstreamAttemptResult(
            True, None, {f"{self.name}.accepted": len(request.transaction.items)}
        )


class _HappyEmitter:
    """保留控制器最终产品而不接触文件的 M11 边界。"""

    def __init__(self):
        self.product = None
        self.committed = None
        self.assembled = []

    def assemble_sequence(self, request):
        """把 generation truth 与计划事件投影成最终测试 rows。"""
        projection = request.projection
        truth = dict(projection.main_record.raw["_meta"]["generation"])
        ordinal = len(self.assembled)
        primary = tuple(
            {
                **dict(row),
                "_meta": {
                    **dict(row["_meta"]),
                    "annotation": {"downstream_marker": ordinal},
                },
            }
            for row in projection.primary_stream_rows
        )
        rows = SequenceRows(
            {"_meta": {
                "generation": truth,
                "annotation": {"downstream_marker": ordinal},
            }},
            primary,
            1,
        )
        self.assembled.append(rows)
        return rows

    def prepare_product(self, main_rows, stream_rows, report):
        """冻结控制器交来的唯一成功视图。"""
        self.product = SimpleNamespace(
            main_rows=tuple(main_rows), stream_rows=tuple(stream_rows), report=report
        )
        return self.product

    def commit(self, product):
        """记录 manifest-last commit 调用。"""
        self.committed = product

    def write_failed_report(self, report):
        """成功路径不得写 failed report。"""
        raise AssertionError("unexpected failed report")


async def test_slot_retry_reuses_exact_precompiled_program_and_plan(monkeypatch):
    """真实 attempt 链重试时不得重新编译 program 或重新规划。"""
    cfg, program, plan = _load_products(monkeypatch, "project.toml")
    controller, _metrics, _dedup, _emitter = _controller()
    controller.request.program = program
    controller.request.plan = plan
    controller.generation.config = cfg
    slot = plan.delivery_slots[0]
    observed = []

    async def traces(current_program, current_plan, current_slot, attempt_index, services):
        assert current_program is program and current_plan is plan
        assert current_slot is slot and services is controller.generation
        observed.append((current_program, current_plan, attempt_index))
        return ()

    async def dedup_probe(_transaction, _context):
        if len(observed) == 1:
            raise DedupGroupRejected("retry after generation")
        return "token"

    async def downstream(_transaction, _slot, _attempt_index, _batch_no):
        return {}

    def forbidden(*_args, **_kwargs):
        raise AssertionError("generation retry recompiled or replanned")

    from labelkit.operators.generation import planner as planner_module
    from labelkit.operators.generation import program as program_module
    from labelkit.operators.generation import scenario

    monkeypatch.setattr(scenario, "_generate_validated_slot_traces", traces)
    monkeypatch.setattr(program_module, "compile_generation_program", forbidden)
    monkeypatch.setattr(planner_module, "compile_scenario_plan", forbidden)
    monkeypatch.setattr(delivery_mod, "compile_generation_program", forbidden, raising=False)
    monkeypatch.setattr(delivery_mod, "compile_scenario_plan", forbidden, raising=False)
    monkeypatch.setattr(controller, "_project_traces", lambda _slot, _traces: ())
    monkeypatch.setattr(controller, "_projection_witnesses", lambda _projections: ())
    monkeypatch.setattr(controller, "_transaction", lambda _slot, _projections: AttemptTransaction((), {}, ()))
    monkeypatch.setattr(controller, "_dedup_probe", dedup_probe)
    monkeypatch.setattr(controller, "_run_downstream", downstream)
    monkeypatch.setattr(controller, "_project_replays", lambda _slot, _rows: ((), ()))
    monkeypatch.setattr(
        controller, "_prospective_sequence",
        lambda _witnesses, _rows, _replays, _retained: None,
    )

    accepted = await controller._accept_sequence_slot(slot, 1)

    assert accepted.rows == ()
    assert [(item[0] is program, item[1] is plan, item[2]) for item in observed] == [
        (True, True, 0), (True, True, 1),
    ]
    assert controller.state.sequence_attempts == 2
    assert controller.state.rejected["dedup"] == 1


async def test_controller_happy_path_drives_all_primary_noise_replay_and_reports(
        monkeypatch):
    """从冻结 plan 驱动全部 prospective seam，最终只 commit 一个 8/27 产品。"""
    cfg, program, plan = _load_products(monkeypatch, "project.toml")
    controller, metrics, _old_dedup, _old_emitter = _controller()
    controller.request.program = program
    controller.request.plan = plan
    controller.generation.config = cfg
    dedup = _HappyDedup()
    emitter = _HappyEmitter()
    controller.services.dedup = dedup
    controller.services.quality = _HappyCollaborator("quality")
    controller.services.annotate = _HappyCollaborator("annotate")
    controller.services.verify = _HappyCollaborator("verify")
    controller.services.emitter = emitter
    from labelkit.operators.generation import evaluate, project, render, scenario

    async def traces(current_program, current_plan, slot, attempt_index, services):
        assert current_program is program and current_plan is plan
        assert attempt_index == 0 and services is controller.generation
        return tuple(SimpleNamespace(variant_name=name) for name in slot.variant_names)

    ordinal = 0

    def projection(request):
        nonlocal ordinal
        variant = request.trace.variant_name
        events = next(
            block[(request.slot.slot_key, variant)] for block in plan.blocks
            if (request.slot.slot_key, variant) in block
        )
        ordinal += 1
        base = _projection_for_branch(request.slot, events, ordinal)
        frame_class = next(iter(program.frame_classes))
        members = tuple(
            replace(member, raw={"utterance": f"primary-{ordinal}-{index}"})
            for index, member in enumerate(base.main_record.members)
        )
        main = replace(
            base.main_record,
            raw={"_meta": {"generation": {
                "pattern": request.slot.pattern_name, "variant": variant,
            }}},
            members=members,
        )
        rows = tuple(
            {
                "payload": dict(member.raw),
                "_meta": {"event": {
                    "event_id": member.id, "frame_class": frame_class,
                    "timestamp": f"{event.timestamp_us:020d}",
                }},
            }
            for member, event in zip(members, events, strict=True)
        )
        return ProjectedSequence(main, rows)

    replay_requests = []

    def replay(request):
        replay_requests.append(request)
        rows = tuple(
            {
                "payload": {"replay": request.layout.replay_ordinal, "index": index},
                "_meta": {"event": {"timestamp": f"{timestamp:020d}"}},
            }
            for index, timestamp in enumerate(request.layout.timestamps_us)
        )
        return ReplayRows(rows, len(rows))

    async def render_noise(request, services):
        return {"noise": request.noise_slot.ordinal}

    async def evaluate_noise(request, services):
        return SimpleNamespace(
            unrelated_to_declared_tasks=True,
            no_executable_task=True,
            realism=True,
            matches_planned_topic=True,
            reason_codes=(),
        )

    def project_noise(request):
        return {
            "payload": dict(request.payload),
            "_meta": {"event": {
                "timestamp": f"{request.noise_slot.timestamp_us:020d}",
            }},
        }

    monkeypatch.setattr(scenario, "_generate_validated_slot_traces", traces)
    monkeypatch.setattr(project, "_project_trace_from_validated_plan", projection)
    monkeypatch.setattr(project, "_project_replay_from_validated_plan", replay)
    monkeypatch.setattr(project, "project_noise", project_noise)
    monkeypatch.setattr(project, "reconcile_views", lambda request: None)
    monkeypatch.setattr(project, "reconcile_prospective_views", lambda request: None)
    monkeypatch.setattr(render, "render_noise", render_noise)
    monkeypatch.setattr(evaluate, "evaluate_noise", evaluate_noise)

    product = await controller.deliver()

    assert product is emitter.committed is emitter.product
    assert len(product.main_rows) == 8
    assert len(product.stream_rows) == 27
    assert product.report["counts"]["generated"] == 8
    assert product.report["generate"]["sequence"]["delivered_sets"] == 2
    assert product.report["generate"]["sequence"]["replay_events"] == 3
    assert len(dedup.commits) == 2
    assert len(replay_requests) == 1
    replay_source = replay_requests[0].source
    assert any(replay_source is rows for rows in emitter.assembled)
    assert replay_source.main_row["_meta"]["annotation"] == {
        "downstream_marker": 0,
    }
    assert replay_source.primary_stream_rows[0]["_meta"]["annotation"] == {
        "downstream_marker": 0,
    }
    assert metrics.counters == {
        "quality.accepted": 8, "annotate.accepted": 8, "verify.accepted": 8,
    }


@pytest.mark.parametrize("project_name", ("project.toml", "project-instruction-only.toml"))
def test_items_take_session_from_unique_planned_owner_branch(monkeypatch, project_name):
    """declared crossing 与 instruction-only 都不依赖 projector 的 session 字段。"""
    _cfg, program, plan = _load_products(monkeypatch, project_name)
    controller, _metrics, _dedup, _emitter = _controller()
    controller.request.program = program
    controller.request.plan = plan
    ordinal = 1
    for slot in plan.delivery_slots:
        variants = slot.variant_names or (None,)
        projections = []
        expected = []
        for variant_name in variants:
            events = next(
                block[(slot.slot_key, variant_name)] for block in plan.blocks
                if (slot.slot_key, variant_name) in block
            )
            projections.append(_projection_for_branch(slot, events, ordinal))
            expected.append(events[0].session_id)
            ordinal += 1
        transaction = controller._transaction(slot, tuple(projections))
        assert [item.session_id for item in transaction.items] == expected
        assert all(item.classification.source == "inherited" for item in transaction.items)


async def test_concrete_attempt_collaborators_feed_the_only_final_sequence_rows(
        monkeypatch, tmp_path):
    """真实 run_attempt 包装后的 final item 含全部下游产物并由 M11 一次装配。"""
    cfg, program, plan = _load_products(monkeypatch, "project.toml")
    cfg = replace(
        cfg,
        quality=replace(cfg.quality, enabled=True),
        frame_annotate=replace(cfg.frame_annotate, enabled=True),
        verify=replace(cfg.verify, enabled=True),
    )
    program = replace(
        program,
        frame_schema={"type": "object"},
        frame_classes={
            name: replace(view, enabled=True)
            for name, view in program.frame_classes.items()
        },
    )
    controller, metrics, _dedup, _emitter = _controller()
    controller.request.program = program
    controller.request.plan = plan
    controller.generation.config = cfg
    slot = plan.delivery_slots[0]
    branches = tuple(
        next(
            block[(slot.slot_key, variant)] for block in plan.blocks
            if (slot.slot_key, variant) in block
        )
        for variant in slot.variant_names
    )
    projections = tuple(
        _projection_for_branch(slot, events, 99 + index)
        for index, events in enumerate(branches)
    )
    transaction = controller._transaction(slot, projections)
    item = transaction.items[0]
    context = RunContext(cfg, None, None, metrics, random.Random(7), 1)
    request = DownstreamAttemptRequest(transaction, context)
    quality, annotate, verify = QualityStage(cfg), AnnotateStage(cfg), VerifyStage(cfg)

    async def score(batch, run_context):
        for current in batch:
            current.scores = {
                "clarity": QualityScore("clarity", 0.8, "pointwise", {}),
                "__aggregate__": QualityScore("__aggregate__", 0.8, "pointwise", {}),
            }
        run_context.metrics.count("quality.kept", len(batch))
        return batch

    async def label(batch, run_context):
        for current in batch:
            current.annotation = Annotation(
                {"intent": "book_ticket"}, "deepseek-v4-flash", 1, Usage(10, 4)
            )
            current.member_annotations = {
                member.id: Annotation(
                    {"frame": index}, "deepseek-v4-flash", 1, Usage(3, 1)
                )
                for index, member in enumerate(current.record.members)
            }
        run_context.metrics.count("annotate.succeeded", len(batch))
        return batch

    async def judge(batch, run_context):
        for current in batch:
            current.verification = VerificationResult("pass", 1, ())
        run_context.metrics.count("verify.passed", len(batch))
        return batch

    monkeypatch.setattr(quality, "run", score)
    monkeypatch.setattr(annotate, "run", label)
    monkeypatch.setattr(verify, "run", judge)
    results = tuple(
        [await quality.run_attempt(request), await annotate.run_attempt(request),
         await verify.run_attempt(request)]
    )
    assert all(result.accepted for result in results)
    assert metrics.counters == {}
    counters = {}
    for result in results:
        counters.update(result.dataset_counters)
    assert counters == {
        "quality.kept": 4, "annotate.succeeded": 4, "verify.passed": 4,
    }

    frame_view = next(iter(program.frame_classes.values()))
    assembly_program = replace(
        program,
        frame_classes={
            classification.label: replace(frame_view, enabled=True)
            for classification in item.member_classifications.values()
        },
    )
    assembled = SequenceDeliveryEmitter(cfg.paths).assemble_sequence(
        SequenceAssemblyRequest(
            assembly_program,
            SimpleNamespace(validate_only=lambda *_args, **_kwargs: []),
            item, projections[0], 1,
        )
    )
    main = json.loads(canonical_json(assembled.main_row))
    assert main["_meta"]["classification"]["source"] == "inherited"
    assert main["_meta"]["scores"]["__aggregate__"] == 0.8
    assert main["_meta"]["annotation"]["model"] == "deepseek-v4-flash"
    assert [member["annotation"] for member in main["_meta"]["stream"]["members"]] == [
        {"frame": index} for index in range(len(branches[0]))
    ]
    assert main["_meta"]["verification"] == {"verdict": "pass", "rounds": 1}


async def test_downstream_annotate_consumes_program_views_not_source_config(
    monkeypatch,
):
    """真实 AnnotateStage 的 schema 与 prompt 只消费 program-bound 视图。"""
    cfg, _old_program, _old_plan = _load_products(
        monkeypatch, "project-instruction-only.toml"
    )
    class_name = "ticket_booking"
    program_schema = {
        "type": "object",
        "properties": {"program_annotation": {"const": "ok"}},
        "required": ["program_annotation"],
        "additionalProperties": False,
    }
    frame_schema = {
        "type": "object",
        "properties": {"frame_annotation": {"const": "ok"}},
        "required": ["frame_annotation"],
        "additionalProperties": False,
    }
    program_class = replace(
        cfg.class_views[class_name],
        schema=program_schema,
        annotate=replace(
            cfg.class_views[class_name].annotate,
            instruction="PROGRAM CLASS INSTRUCTION",
        ),
    )
    program_frames = {
        name: replace(
            view,
            instruction=("PROGRAM FRAME INSTRUCTION" if name == "task_request"
                         else view.instruction),
        )
        for name, view in cfg.frame_class_views.items()
    }
    program_cfg = replace(
        cfg,
        class_views={**dict(cfg.class_views), class_name: program_class},
        frame_class_views=program_frames,
        frame_annotate=replace(cfg.frame_annotate, enabled=True),
        frame_schema=frame_schema,
        llm_profiles=_unbudgeted_profiles(cfg),
    )
    program = compile_generation_program(program_cfg)
    plan = compile_scenario_plan(program)
    poison_schema = {
        "type": "object", "properties": {"poison": {"type": "string"}},
    }
    poison_class = replace(
        program_cfg.class_views[class_name],
        schema=poison_schema,
        annotate=replace(
            program_cfg.class_views[class_name].annotate,
            instruction="POISON CLASS INSTRUCTION",
        ),
    )
    poison_frames = {
        **dict(program_cfg.frame_class_views),
        "task_request": replace(
            program_cfg.frame_class_views["task_request"],
            instruction="POISON FRAME INSTRUCTION",
        ),
    }
    poisoned = replace(
        program_cfg,
        class_views={**dict(program_cfg.class_views), class_name: poison_class},
        frame_class_views=poison_frames,
    )

    class CaptureSchemaEngine:
        """只替代 M8 端点边界，保留 M5 的真实 schema/prompt 消费。"""

        def __init__(self):
            self.calls = []
            self.stats = {}
            self.user_schema_text = "POISON GLOBAL SCHEMA"

        async def complete_validated(self, profile, prompt, schema=None, *, scope):
            """记录实际 M5 请求并返回符合相应 Schema 的对象。"""
            del profile, scope
            self.calls.append((prompt, schema))
            if "program_annotation" in schema.get("properties", {}):
                return {"program_annotation": "ok"}, Usage(), 1, "offline"
            return {"frame_annotation": "ok"}, Usage(), 1, "offline"

    engine = CaptureSchemaEngine()
    controller, metrics, _dedup, _emitter = _controller()
    controller.request.program = program
    controller.request.plan = plan
    controller.generation.config = poisoned
    controller.generation.schema_engine = engine
    slot, events, projection = _instruction_projection(plan)
    transaction = controller._transaction(
        slot, (projection,),
    )
    stage = AnnotateStage(poisoned)
    controller.services.annotate = stage
    counters = await controller._run_downstream(transaction, slot, 0, 1)
    assert counters == {"frame_annotate.annotated": len(events)}
    assert len(engine.calls) == len(events) + 1
    sequence_prompt, sequence_schema = engine.calls[0]
    sequence_system = sequence_prompt.messages[0].parts[0].text
    assert canonical_json(sequence_schema) == canonical_json(program_schema)
    assert "PROGRAM CLASS INSTRUCTION" in sequence_system
    assert "POISON CLASS INSTRUCTION" not in sequence_system
    for frame_prompt, actual_schema in engine.calls[1:]:
        frame_system = frame_prompt.messages[0].parts[0].text
        assert canonical_json(actual_schema) == canonical_json(frame_schema)
        assert "PROGRAM FRAME INSTRUCTION" in frame_system
        assert "POISON FRAME INSTRUCTION" not in frame_system
    assert canonical_json(class_effective_schema(
        program_cfg, class_name,
    )) == canonical_json(program_schema)
    assert controller.generation.config is poisoned and stage.cfg is poisoned
    assert poisoned.class_views[class_name].schema == poison_schema
    assert poisoned.frame_class_views["task_request"].instruction == (
        "POISON FRAME INSTRUCTION"
    )
    assert metrics.counters == {}


async def test_frame_only_downstream_has_zero_sequence_calls_and_uses_program_views(
    monkeypatch,
):
    """frame-only attempt 只消费 program 中的 frame 指令与 Schema。"""
    cfg, _old_program, _old_plan = _load_products(
        monkeypatch, "project-instruction-only.toml"
    )
    frame_schema = {
        "type": "object",
        "properties": {"frame_annotation": {"const": "ok"}},
        "required": ["frame_annotation"],
        "additionalProperties": False,
    }
    program_frames = {
        name: replace(
            view,
            instruction=(
                "PROGRAM FRAME INSTRUCTION"
                if name == "task_request"
                else view.instruction
            ),
        )
        for name, view in cfg.frame_class_views.items()
    }
    program_cfg = replace(
        cfg,
        annotate=replace(cfg.annotate, enabled=False),
        frame_annotate=replace(cfg.frame_annotate, enabled=True),
        frame_class_views=program_frames,
        frame_schema=frame_schema,
        llm_profiles=_unbudgeted_profiles(cfg),
    )
    program = compile_generation_program(program_cfg)
    plan = compile_scenario_plan(program)
    poison_schema = {
        "type": "object",
        "properties": {"poison": {"type": "string"}},
    }
    poisoned_frames = {
        **dict(program_cfg.frame_class_views),
        "task_request": replace(
            program_cfg.frame_class_views["task_request"],
            instruction="POISON FRAME INSTRUCTION",
        ),
    }
    poisoned = replace(
        program_cfg,
        frame_class_views=poisoned_frames,
        frame_schema=poison_schema,
    )

    class CaptureSchemaEngine:
        """记录 frame-only M5 请求并返回 program Schema 合法对象。"""

        def __init__(self):
            self.calls = []
            self.stats = {}

        async def complete_validated(self, profile, prompt, schema=None, *, scope):
            """记录请求并返回帧标注对象。"""
            del profile, scope
            self.calls.append((prompt, schema))
            return {"frame_annotation": "ok"}, Usage(), 1, "offline"

    engine = CaptureSchemaEngine()
    controller, metrics, _dedup, _emitter = _controller()
    controller.request.program = program
    controller.request.plan = plan
    controller.generation.config = poisoned
    controller.generation.schema_engine = engine
    slot, events, projection = _instruction_projection(plan)
    transaction = controller._transaction(slot, (projection,))
    stage = AnnotateStage(poisoned)
    controller.services.annotate = stage
    counters = await controller._run_downstream(transaction, slot, 0, 1)

    assert counters == {"frame_annotate.annotated": len(events)}
    assert len(engine.calls) == len(events)
    for prompt, actual_schema in engine.calls:
        system = prompt.messages[0].parts[0].text
        assert canonical_json(actual_schema) == canonical_json(frame_schema)
        assert "POISON FRAME INSTRUCTION" not in system
    task_request_calls = [
        prompt for prompt, _schema in engine.calls
        if "PROGRAM FRAME INSTRUCTION" in prompt.messages[0].parts[0].text
    ]
    assert task_request_calls
    assert all(item.annotation is None for item in transaction.items)
    assert all(
        len(item.member_annotations or {}) == len(item.record.members)
        for item in transaction.items
    )
    assert controller.generation.config is poisoned and stage.cfg is poisoned
    assert metrics.counters == {}


async def test_downstream_quality_consumes_program_rubric_not_source_config(monkeypatch):
    """真实 QualityStage 的分池、Schema、prompt 与 gate 只消费 program-bound 视图。"""
    cfg, _old_program, _old_plan = _load_products(
        monkeypatch, "project-instruction-only.toml"
    )
    class_name = "ticket_booking"
    source = cfg.class_views[class_name]
    program_criterion = replace(
        source.rubric.criteria[0],
        key="program_criterion",
        description="PROGRAM RUBRIC DESCRIPTION",
    )
    quality = replace(
        source.quality,
        enabled=True,
        mode="pointwise",
        selection="threshold",
        threshold=0.0,
        top_ratio=None,
    )
    program_class = replace(
        source,
        quality=quality,
        rubric=replace(source.rubric, criteria=(program_criterion,)),
    )
    program_cfg = replace(
        cfg,
        class_views={**dict(cfg.class_views), class_name: program_class},
        llm_profiles=_unbudgeted_profiles(cfg),
    )
    program = compile_generation_program(program_cfg)
    plan = compile_scenario_plan(program)
    poison_criterion = replace(
        program_criterion,
        key="poison_criterion",
        description="POISON RUBRIC DESCRIPTION",
    )
    poison_class = replace(
        program_cfg.class_views[class_name],
        quality=replace(
            program_cfg.class_views[class_name].quality,
            threshold=1.0,
        ),
        rubric=replace(source.rubric, criteria=(poison_criterion,)),
    )
    poisoned = replace(
        program_cfg,
        class_views={**dict(program_cfg.class_views), class_name: poison_class},
    )

    class CaptureQualityEngine:
        """只替代 M8 调用，记录真实 M4 schema 与 prompt。"""

        def __init__(self):
            self.calls = []
            self.stats = {}

        async def complete_validated(self, profile, prompt, schema=None, *, scope):
            """按实际 Schema 的 criterion enum 返回四分。"""
            del profile, scope
            key = schema["properties"]["scores"]["items"]["properties"][
                "criterion"
            ]["enum"][0]
            self.calls.append((prompt, schema, key))
            value = {"scores": [{"criterion": key, "reason": "ok", "score": 4}]}
            return value, Usage(), 1, "offline"

    engine = CaptureQualityEngine()
    controller, metrics, _dedup, _emitter = _controller()
    controller.request.program = program
    controller.request.plan = plan
    controller.generation.config = poisoned
    controller.generation.schema_engine = engine
    slot, _events, projection = _instruction_projection(plan)
    transaction = controller._transaction(slot, (projection,))
    stage = QualityStage(poisoned)
    controller.services.quality = stage
    counters = await controller._run_downstream(transaction, slot, 0, 1)
    item = transaction.items[0]
    system = engine.calls[0][0].messages[0].parts[0].text
    assert engine.calls[0][2] == "program_criterion"
    assert "PROGRAM RUBRIC DESCRIPTION" in system
    assert "POISON RUBRIC DESCRIPTION" not in system
    assert set(item.scores) == {"program_criterion", "__aggregate__"}
    assert item.scores["__aggregate__"].score == pytest.approx(0.8)
    assert item.status == "active"
    assert counters == {}
    assert stage.cfg is poisoned and controller.generation.config is poisoned
    assert poisoned.class_views[class_name].rubric.criteria[0].key == "poison_criterion"
    assert poisoned.class_views[class_name].quality.threshold == 1.0
    assert metrics.counters == {}


async def test_downstream_verify_consumes_program_class_texts_not_source_config(monkeypatch):
    """真实 VerifyStage 的判决与修复重标注只消费 program-bound 类视图。"""
    cfg, _old_program, _old_plan = _load_products(
        monkeypatch, "project-instruction-only.toml"
    )
    class_name = "ticket_booking"
    source = cfg.class_views[class_name]
    program_schema = {
        "type": "object",
        "properties": {"program_repair": {"const": "ok"}},
        "required": ["program_repair"],
        "additionalProperties": False,
    }
    program_class = replace(
        source,
        schema=program_schema,
        annotate=replace(source.annotate, instruction="PROGRAM VERIFY TASK"),
        verify=replace(source.verify, extra_criteria="PROGRAM VERIFY CRITERIA"),
    )
    program_cfg = replace(
        cfg,
        class_views={**dict(cfg.class_views), class_name: program_class},
        verify=replace(
            cfg.verify,
            enabled=True,
            policy="repair",
            max_repair_rounds=1,
        ),
        llm_profiles=_unbudgeted_profiles(cfg),
    )
    program = compile_generation_program(program_cfg)
    plan = compile_scenario_plan(program)
    poison_schema = {
        "type": "object",
        "properties": {"poison_repair": {"const": "wrong"}},
        "required": ["poison_repair"],
        "additionalProperties": False,
    }
    poison_class = replace(
        program_cfg.class_views[class_name],
        schema=poison_schema,
        annotate=replace(source.annotate, instruction="POISON VERIFY TASK"),
        verify=replace(source.verify, extra_criteria="POISON VERIFY CRITERIA"),
    )
    poisoned = replace(
        program_cfg,
        class_views={**dict(program_cfg.class_views), class_name: poison_class},
    )

    class CaptureVerifyEngine:
        """只替代 M8 调用，记录真实 M7 判决与 M5 修复请求。"""

        def __init__(self):
            self.verdict_calls = []
            self.reannotation_calls = []
            self.stats = {}
            self.verdict_index = 0
            self.user_schema_text = "POISON GLOBAL SCHEMA"

        async def complete_validated(self, profile, prompt, schema=None, *, scope):
            """首轮判 fail，真实修复重标注后次轮判 pass。"""
            del profile
            if "verdict" in schema.get("properties", {}):
                self.verdict_calls.append((prompt, schema, scope))
                self.verdict_index += 1
                if self.verdict_index == 1:
                    result = {
                        "critiques": [{"aspect": "schema", "opinion": "repair it"}],
                        "verdict": "fail",
                    }
                else:
                    result = {"critiques": [], "verdict": "pass"}
                return result, Usage(), 1, "offline"
            self.reannotation_calls.append((prompt, schema, scope))
            return {"program_repair": "ok"}, Usage(), 1, "offline"

    engine = CaptureVerifyEngine()
    controller, metrics, _dedup, _emitter = _controller()
    controller.request.program = program
    controller.request.plan = plan
    controller.generation.config = poisoned
    controller.generation.schema_engine = engine
    slot, _events, projection = _instruction_projection(plan)
    transaction = controller._transaction(slot, (projection,))
    item = transaction.items[0]
    item.annotation = Annotation({"program_repair": "before"}, "offline", 1, Usage())
    stage = VerifyStage(poisoned)
    controller.services.verify = stage
    counters = await controller._run_downstream(transaction, slot, 0, 1)
    assert len(engine.verdict_calls) == 2
    assert len(engine.reannotation_calls) == 1
    prompt = engine.verdict_calls[0][0]
    rendered = "\n".join(
        part.text or "" for message in prompt.messages for part in message.parts
    )
    assert "PROGRAM VERIFY TASK" in rendered
    assert "PROGRAM VERIFY CRITERIA" in rendered
    assert "POISON VERIFY TASK" not in rendered
    assert "POISON VERIFY CRITERIA" not in rendered
    repair_prompt, repair_schema, _scope = engine.reannotation_calls[0]
    repair_system = repair_prompt.messages[0].parts[0].text
    assert canonical_json(repair_schema) == canonical_json(program_schema)
    assert "PROGRAM VERIFY TASK" in repair_system
    assert "program_repair" in repair_system
    assert "POISON VERIFY TASK" not in repair_system
    assert "poison_repair" not in repair_system
    assert item.annotation.output == {"program_repair": "ok"}
    assert item.verification == VerificationResult(
        "pass", 2, ({"aspect": "schema", "opinion": "repair it"},)
    )
    assert counters == {}
    assert stage.cfg is poisoned and controller.generation.config is poisoned
    assert poisoned.class_views[class_name].schema == poison_schema
    assert poisoned.class_views[class_name].verify.extra_criteria == (
        "POISON VERIFY CRITERIA"
    )
    assert metrics.counters == {}


def test_teaching_example_plan_and_replay_arithmetic_is_exact(monkeypatch):
    """同一冻结 plan 唯一实现 2/8/22 + 2 noise + 3 replay = 27。"""
    cfg, program, plan = _load_products(monkeypatch, "project.toml")
    estimate = estimate_sequence_products(cfg, program, plan)["sequence"]
    assert estimate_sequence(cfg)["sequence"] == estimate
    assert estimate == {
        "mode": "declared",
        "program_digest": program.digest,
        "plan_digest": plan.digest,
        "planned_sets": 2,
        "planned_sequences": 8,
        "primary_events": 22,
        "primary_sessions": 8,
        "crossed_primary_sessions": 0,
        "noise_events": 2,
        "replay_sequences": 1,
        "replay_events": 3,
        "stream_rows": 27,
        "successful_attempt_lower_bound": 48,
        "max_slot_attempts_upper_bound": 384,
        "sequence_calls": {
            "scenario_seed_calls": 0,
            "baseline_event_plan_calls": 6,
            "variant_event_plan_calls": 8,
            "frame_render_calls": 14,
            "semantic_evaluation_calls": 8,
            "noise_render_calls": 2,
            "noise_evaluation_calls": 2,
        },
    }

    controller, _metrics, _dedup, _emitter = _controller()
    controller.request.program = program
    controller.request.plan = plan
    for slot in plan.delivery_slots:
        for variant_name in slot.variant_names or (None,):
            events = next(
                block[(slot.slot_key, variant_name)] for block in plan.blocks
                if (slot.slot_key, variant_name) in block
            )
            main = {
                "_meta": {"generation": {
                    "pattern": slot.pattern_name, "variant": variant_name,
                }},
            }
            controller.state.sequences.append(
                SequenceRows(main, tuple({} for _event in events), 0)
            )
    controller.state.noise_rows.extend(({}, {}))
    controller.state.replays.append(ReplayRows(({}, {}, {}), 0))
    controller.state.sequence_attempts = 2
    controller.state.noise_attempts = 2
    report = controller._sequence_report(tuple({} for _index in range(27)))
    assert {
        key: report[key] for key in (
            "planned_sets", "delivered_sets", "planned_sequences",
            "delivered_sequences", "primary_events", "noise_events",
            "replay_sequences", "replay_events", "stream_rows",
            "sequence_slot_attempts", "noise_slot_attempts",
        )
    } == {
        "planned_sets": 2,
        "delivered_sets": 2,
        "planned_sequences": 8,
        "delivered_sequences": 8,
        "primary_events": 22,
        "noise_events": 2,
        "replay_sequences": 1,
        "replay_events": 3,
        "stream_rows": 27,
        "sequence_slot_attempts": 2,
        "noise_slot_attempts": 2,
    }
    assert all(
        counts == {"planned": 2, "delivered": 2}
        for variants in report["by_pattern"].values()
        for counts in variants.values()
    )

    no_downstream = replace(
        cfg,
        quality=replace(cfg.quality, enabled=False),
        annotate=replace(cfg.annotate, enabled=False),
        frame_annotate=replace(cfg.frame_annotate, enabled=False),
        verify=replace(cfg.verify, enabled=False),
    )
    disabled = estimate_sequence_products(no_downstream, program, plan)
    assert {
        key: disabled[key] for key in (
            "quality_calls", "annotate_calls", "frame_annotate_calls", "verify_calls",
        )
    } == {
        "quality_calls": 0, "annotate_calls": 0,
        "frame_annotate_calls": 0, "verify_calls": 0,
    }
    instruction_cfg, instruction_program, instruction_plan = _load_products(
        monkeypatch, "project-instruction-only.toml"
    )
    instruction = estimate_sequence_products(
        instruction_cfg, instruction_program, instruction_plan
    )["sequence"]
    assert instruction["mode"] == "instruction_only"
    assert instruction["sequence_calls"]["scenario_seed_calls"] == 1


def test_by_pattern_counts_shared_pattern_variants_once_across_sources():
    """多个 source 复用同一 pattern/variant 时 delivered 不按 source 重复累计。"""
    controller, _metrics, _dedup, _emitter = _controller()
    positive = SimpleNamespace(name="positive")
    missing = SimpleNamespace(name="missing")
    timeout = SimpleNamespace(name="timeout")
    controller.request.program.counterfactual_sets = (
        SimpleNamespace(pattern="shared", count=1, variants=(positive, missing)),
        SimpleNamespace(pattern="shared", count=1, variants=(positive, timeout)),
    )
    for variant in ("positive", "missing", "positive", "timeout"):
        controller.state.sequences.append(SequenceRows(
            {"_meta": {"generation": {"pattern": "shared", "variant": variant}}},
            (), 0,
        ))
    assert controller._by_pattern() == {
        "shared": {
            "positive": {"planned": 2, "delivered": 2},
            "missing": {"planned": 1, "delivered": 1},
            "timeout": {"planned": 1, "delivered": 1},
        }
    }


async def test_commit_failure_report_clears_last_successful_slot_identity(monkeypatch):
    """finalization I/O 失败属于 run terminal，failed report 不冒充最后一个 slot。"""
    controller, _metrics, _dedup, _old_emitter = _controller()
    slot = SimpleNamespace(
        slot_key="set/000000", scenario_index=0, variant_names=()
    )
    controller.request.plan.delivery_slots = (slot,)
    controller.request.plan.primary_sessions = 1
    controller.request.program.instruction_only = (SimpleNamespace(count=1),)
    row = SequenceRows({"_meta": {"generation": {}}}, (), 1)
    accepted = _AcceptedSequenceAttempt(
        (row,), (), (), (), "token", {"generated": 1}, 1
    )

    class CommitFailEmitter:
        def __init__(self):
            self.failed_reports = []
            self.manifest = {"delivery_digest": "old"}

        def prepare_product(self, main_rows, stream_rows, report):
            return SimpleNamespace(
                main_rows=tuple(main_rows), stream_rows=tuple(stream_rows), report=report
            )

        def commit(self, product):
            raise LabelKitError("generation_commit_io")

        def write_failed_report(self, report):
            self.failed_reports.append(dict(report))

    emitter = CommitFailEmitter()
    controller.services.emitter = emitter

    async def accept(current, _slot, _batch_no):
        current.state.failed_slot = slot.slot_key
        current.state.attempts_used = 1
        current.state.sequence_attempts = 1
        return accepted

    monkeypatch.setattr(_DeliveryController, "_accept_sequence_slot", accept)
    from labelkit.operators.generation import project

    monkeypatch.setattr(project, "validate_plan_identity", lambda *_args: None)
    monkeypatch.setattr(project, "reconcile_views", lambda *_args: None)
    with pytest.raises(LabelKitError, match="generation_commit_io"):
        await deliver_generation(controller.request, controller.services)

    assert emitter.manifest == {"delivery_digest": "old"}
    assert len(emitter.failed_reports) == 1
    failed = emitter.failed_reports[0]
    assert failed["failed_slot"] is None and failed["attempts_used"] == 0
    assert failed["terminal_error_kind"] == "generation_commit_io"
    assert not any(failed["rejected_attempts"].values())


def test_stream_rows_are_globally_stable_timestamp_sorted():
    """最终 stream 形成跨 owner A-B-A，而非按 sequence group 拼接。"""
    controller, _metrics, _dedup, _emitter = _controller()
    first = SequenceRows({}, (
        {"name": "a1", "_meta": {"event": {"timestamp": "2026-01-01T00:00:01.000000Z"}}},
        {"name": "a2", "_meta": {"event": {"timestamp": "2026-01-01T00:00:03.000000Z"}}},
    ), 0)
    second = SequenceRows({}, (
        {"name": "b1", "_meta": {"event": {"timestamp": "2026-01-01T00:00:02.000000Z"}}},
    ), 0)
    controller.state.sequences.extend((first, second))
    controller.state.noise_rows.append(
        {"name": "noise", "_meta": {"event": {"timestamp": "2026-01-01T00:00:04.000000Z"}}}
    )
    assert [row["name"] for row in controller._ordered_stream_rows()] == [
        "a1", "b1", "a2", "noise",
    ]


async def test_exhaustion_consumes_exact_attempts_without_commit(monkeypatch):
    """耗尽后 planned slot 不产生任何 partial delivery。"""
    controller, metrics, dedup, _emitter = _controller(attempts=2)

    async def rejected(slot, batch_no, attempt_index):
        raise GenerationAttemptRejected("quality", slot.slot_key)

    monkeypatch.setattr(controller, "_try_sequence_attempt", rejected)
    with pytest.raises(DeliveryError) as caught:
        await controller._accept_sequence_slot(_slot(), 1)
    assert caught.value.kind == "sequence_delivery_exhausted"
    assert controller.state.sequence_attempts == 2
    assert controller.state.rejected["quality"] == 2
    assert metrics.counters == {}
    assert dedup.commits == []
    assert controller.state.sequences == []


async def test_public_exhaustion_preserves_success_artifacts_and_writes_only_failure(
        monkeypatch, tmp_path):
    """public delivery 耗尽时四件旧成功工件不变，只原子替换 failed report。"""
    controller, _metrics, _dedup, _emitter = _controller(attempts=2)
    paths = SimpleNamespace(
        project=str(tmp_path / "project.toml"), project_root=str(tmp_path), input=None,
        output=str(tmp_path / "labels.jsonl"), report=str(tmp_path / "labels.report.json"),
        rejects=None, sidecar=None, trace=None,
        stream=str(tmp_path / "labels.stream.jsonl"),
        manifest=str(tmp_path / "labels.manifest.json"),
        failed_report=str(tmp_path / "labels.failed.report.json"),
    )
    controller.request.paths = paths
    controller.request.plan.delivery_slots = (_slot(),)
    controller.services.emitter = SequenceDeliveryEmitter(paths)
    successful = tuple(
        Path(value) for value in (
            paths.output, paths.stream, paths.report, paths.manifest,
        )
    )
    for index, path in enumerate(successful):
        path.write_bytes(f"old-{index}".encode())

    async def rejected(self, slot, batch_no, attempt_index):
        raise GenerationAttemptRejected("quality", slot.slot_key)

    from labelkit.operators.generation import project

    monkeypatch.setattr(project, "validate_plan_identity", lambda _program, _plan: None)
    monkeypatch.setattr(_DeliveryController, "_try_sequence_attempt", rejected)
    with pytest.raises(DeliveryError) as caught:
        await deliver_generation(controller.request, controller.services)

    assert caught.value.kind == "sequence_delivery_exhausted"
    assert [path.read_bytes() for path in successful] == [
        f"old-{index}".encode() for index in range(4)
    ]
    failed = json.loads(Path(paths.failed_report).read_text(encoding="utf-8"))
    assert set(failed) == {
        "run_attempt_id", "run_id", "artifacts_committed", "failed_slot",
        "attempts_used", "terminal_error_kind", "llm_usage", "rejected_attempts",
    }
    assert failed["artifacts_committed"] is False
    assert failed["failed_slot"] == "set/000000"
    assert failed["attempts_used"] == 2
    assert failed["terminal_error_kind"] == "sequence_delivery_exhausted"
    assert failed["rejected_attempts"]["quality"] == 2
    assert not tuple(tmp_path.glob("*.part"))


def test_secret_safe_usage_and_terminal_kind_cover_every_report_shape():
    """usage 只投影计数，终态闭集不读取异常正文。"""
    controller, _metrics, _dedup, _emitter = _controller()
    key = lambda calls, limited, disabled: SimpleNamespace(
        calls=calls, rate_limited=limited, disabled=disabled
    )
    controller.generation.llm.usage_by_profile = {
        "empty": SimpleNamespace(
            calls=0, retries=0, prompt_tokens=0, completion_tokens=0,
            est_cost_usd=None, keys={}, parked_calls=0, parked_ms=0,
        ),
        "basic": SimpleNamespace(
            calls=1, retries=0, prompt_tokens=10, completion_tokens=4,
            est_cost_usd=None, keys={}, parked_calls=0, parked_ms=0,
        ),
        "pooled": SimpleNamespace(
            calls=2, retries=1, prompt_tokens=20, completion_tokens=8,
            est_cost_usd=0.01,
            keys={"key_1": key(1, 0, False), "key_2": key(1, 1, True)},
            parked_calls=1, parked_ms=25,
        ),
    }
    assert controller._usage_report() == {
        "basic": {
            "calls": 1, "prompt_tokens": 10, "completion_tokens": 4, "retries": 0,
        },
        "pooled": {
            "calls": 2, "prompt_tokens": 20, "completion_tokens": 8, "retries": 1,
            "est_cost_usd": 0.01,
            "keys": {
                "key_1": {"calls": 1, "rate_limited": 0, "disabled": False},
                "key_2": {"calls": 1, "rate_limited": 1, "disabled": True},
            },
            "parked_calls": 1, "parked_ms": 25,
        },
    }
    cases = (
        (DeliveryError("sequence_delivery_exhausted", "slot", 2),
         "sequence_delivery_exhausted"),
        (ProviderFatalError("secret", "default"), "provider_fatal"),
        (CircuitBreakerTripped("secret"), "circuit_breaker_tripped"),
        (KeyboardInterrupt(), "interrupted"),
        (InternalError("secret"), "generation_downstream_contract"),
        (ConfigError(["secret"]), "generation_commit_io"),
        (ValueError("secret"), "internal_error"),
    )
    assert [controller._terminal_kind(error) for error, _expected in cases] == [
        expected for _error, expected in cases
    ]


def test_delivery_invariant_errors_fail_closed_before_mutation(monkeypatch):
    """错 cardinality、plan branch、counter 与 rejection kind 均是 terminal contract。"""
    controller, metrics, dedup, _emitter = _controller(retained=0)
    slot = SimpleNamespace(
        slot_key="set/000000", scenario_index=0, variant_names=("positive",)
    )
    controller.request.program.noise = None
    with pytest.raises(InternalError):
        controller._project_traces(slot, ())
    with pytest.raises(InternalError):
        controller._transaction(slot, ())
    with pytest.raises(InternalError):
        controller._planned_session_id(slot, None)
    with pytest.raises(InternalError):
        controller._noise_render_request(SimpleNamespace(frame_class="missing"), 0)
    monkeypatch.setattr(controller, "_reconcile", lambda *_args, **_kwargs: None)
    with pytest.raises(GenerationAttemptRejected) as memory:
        controller._prospective_noise({}, "d" * 64, 1)
    assert memory.value.kind == "noise_memory_budget"
    assert controller.state.noise_rows == []
    assert controller.state.noise_payload_digests == []
    assert controller.state.retained_bytes == 0
    with pytest.raises(InternalError):
        controller._reject("not-a-bucket")
    with pytest.raises(InternalError):
        controller._merge_local({}, {"generated": True})
    assert controller._sequence_rejection_kind("sequence_projection_mismatch") == "reconcile"
    with pytest.raises(InternalError):
        controller._sequence_rejection_kind("noise_schema")
    with pytest.raises(InternalError):
        controller._noise_rejection_kind("quality")
    with pytest.raises(InternalError):
        delivery_mod._plan_events(SimpleNamespace(blocks=()), "missing", None)
    assert delivery_mod._planning_terminal_kind(ValueError("secret")) == (
        "generation_plan_internal"
    )
    assert metrics.counters == {}
    assert dedup.commits == []


@pytest.mark.parametrize("noise", (False, True))
def test_reconcile_rejection_precedes_simultaneous_memory_overflow(monkeypatch, noise):
    """同一 prospective 产品双故障时，CrossView 桶稳定先于 byte cap。"""
    controller, _metrics, _dedup, _emitter = _controller(retained=0)

    def reject(_override=None):
        raise GenerationAttemptRejected("reconcile", "slot")

    monkeypatch.setattr(controller, "_reconcile", reject)
    with pytest.raises(GenerationAttemptRejected) as caught:
        if noise:
            controller._prospective_noise({}, "d" * 64, 1)
        else:
            controller._prospective_sequence((), (), (), 1)
    assert caught.value.kind == "reconcile"
    if noise:
        assert controller.state.noise_rows == []
        assert controller.state.noise_payload_digests == []
        assert controller.state.retained_bytes == 0


async def test_noise_exhaustion_consumes_exact_bound_without_commit(monkeypatch):
    """noise 重试耗尽覆盖独立 loop 终态，similarity 与 rows 均不提交。"""
    controller, _metrics, _dedup, _emitter = _controller(attempts=1)
    similarity = SimilarityFilter()

    async def rejected(slot, attempt_index, current):
        raise GenerationAttemptRejected("noise_semantic", "noise/000000")

    monkeypatch.setattr(controller, "_try_noise", rejected)
    with pytest.raises(DeliveryError) as caught:
        await controller._accept_noise_slot(SimpleNamespace(ordinal=0), similarity)
    assert caught.value.kind == "sequence_delivery_exhausted"
    assert controller.state.noise_attempts == 1
    assert controller.state.noise_rows == []


async def test_noise_similarity_uses_resolved_dedup_parameters(monkeypatch):
    """noise attempt-local MinHash 索引逐字段读取冻结 dedup 配置。"""
    controller, _metrics, _dedup, _emitter = _controller()
    controller.generation.config.dedup = SimpleNamespace(
        minhash_threshold=0.99,
        minhash_num_perm=64,
        ngram=3,
    )
    captured = {}

    class _SpySimilarity:
        """只记录生产构造参数的轻量 spy。"""

        def __init__(self, *, threshold, num_perm, ngram):
            captured.update(
                threshold=threshold,
                num_perm=num_perm,
                ngram=ngram,
            )

        def add(self, _text):
            """空输入计划下不会调用。"""

    monkeypatch.setattr(delivery_mod, "SimilarityFilter", _SpySimilarity)
    await controller._deliver_noise_slots()
    assert captured == {"threshold": 0.99, "num_perm": 64, "ngram": 3}


def test_noise_requests_and_prompts_share_the_exact_planned_topic(monkeypatch):
    """renderer 与独立 evaluator 只共享 NoiseSlot 冻结的话题。"""
    _cfg, program, plan = _load_products(monkeypatch, "project.toml")
    controller, _metrics, _dedup, _emitter = _controller()
    controller.request.program = program
    controller.request.plan = plan
    slot = plan.noise_slots[1]

    render_request = controller._noise_render_request(slot, 3)
    evaluation_request = controller._noise_evaluation_request(
        {"utterance": "面包刚出炉时闻起来很香。"}, slot, 3,
    )

    assert render_request.noise_slot.topic == slot.topic
    assert evaluation_request.planned_topic == slot.topic
    assert evaluation_request.attempt_index == render_request.attempt_index == 3
    assert render_request.semantic_profile == program.semantic_profile
    assert evaluation_request.evaluation_profile == program.evaluation_profile
    assert render_request.semantic_profile != evaluation_request.evaluation_profile

    from labelkit.operators.generation.evaluate import _noise_prompt as evaluation_prompt
    from labelkit.operators.generation.render import _noise_prompt as render_prompt

    render_text = render_prompt(render_request).messages[1].parts[0].text
    evaluation_text = evaluation_prompt(
        evaluation_request, program.limits,
    ).messages[1].parts[0].text
    other_topic = plan.noise_slots[0].topic
    assert slot.topic in render_text and slot.topic in evaluation_text
    assert other_topic not in render_text and other_topic not in evaluation_text


async def test_rejected_prospective_noise_does_not_pollute_similarity_or_state(
        monkeypatch):
    """真实 noise 链首次 prospective 失败后，同一 payload 仍可在重试中提交。"""
    cfg, program, plan = _load_products(monkeypatch, "project.toml")
    controller, _metrics, _dedup, _emitter = _controller()
    controller.request.program = program
    controller.request.plan = plan
    controller.generation.config = cfg
    slot = plan.noise_slots[0]
    payload = {"utterance": "今晚可以观察到清晰的月相。"}
    similarity = SimilarityFilter()
    prospective_calls = 0

    async def render_noise(_request, _services):
        return payload

    async def evaluate_noise(_request, _services):
        return SimpleNamespace(
            unrelated_to_declared_tasks=True,
            no_executable_task=True,
            realism=True,
            matches_planned_topic=True,
            reason_codes=(),
        )

    def prospective(_row, _digest, _retained):
        nonlocal prospective_calls
        prospective_calls += 1
        assert similarity.probe(canonical_json(payload))[0] is True
        assert controller.state.noise_rows == []
        assert controller.state.noise_payload_digests == []
        assert controller.state.retained_bytes == 0
        if prospective_calls == 1:
            raise GenerationAttemptRejected("reconcile", "noise/000000")

    from labelkit.operators.generation import evaluate, render

    monkeypatch.setattr(render, "render_noise", render_noise)
    monkeypatch.setattr(evaluate, "evaluate_noise", evaluate_noise)
    monkeypatch.setattr(controller, "_prospective_noise", prospective)
    await controller._accept_noise_slot(slot, similarity)

    assert prospective_calls == 2
    assert controller.state.noise_attempts == 2
    assert controller.state.rejected["noise_reconcile"] == 1
    assert len(controller.state.noise_rows) == 1
    assert len(controller.state.noise_payload_digests) == 1
    assert controller.state.retained_bytes > 0
    assert similarity.probe(canonical_json(payload))[0] is False


def _load_example_config(monkeypatch, tmp_path: Path, *, dry_run: bool = False):
    """用真实 M1 路径裁决装载临时输出位置。"""
    monkeypatch.setenv("LABELKIT_DEEPSEEK_KEY", "offline-test-key")
    return load(
        _EXAMPLE / "config.toml",
        _EXAMPLE / "project.toml",
        CliOverrides(output=str(tmp_path / "labels.jsonl"), dry_run=dry_run),
    )


def _fixed_sequence_paths(cfg):
    """返回 sequence 五个禁止被 dry-run 覆盖的固定路径。"""
    assert cfg.paths is not None
    return tuple(
        Path(value) for value in (
            cfg.paths.output, cfg.paths.stream, cfg.paths.report,
            cfg.paths.manifest, cfg.paths.failed_report,
        )
    )


def test_sequence_dry_run_preserves_every_fixed_output_sentinel(monkeypatch, tmp_path):
    """成功规划的 sequence dry-run 只打印 estimate，不写五个固定工件。"""
    cfg = _load_example_config(monkeypatch, tmp_path, dry_run=True)
    sentinels = _fixed_sequence_paths(cfg)
    for index, path in enumerate(sentinels):
        path.write_bytes(f"sentinel-{index}".encode())
    monkeypatch.setattr(runtime, "load", lambda *args, **kwargs: cfg)

    assert runtime.execute_run("config.toml", "project.toml", CliOverrides()) == 0
    assert [path.read_bytes() for path in sentinels] == [
        f"sentinel-{index}".encode() for index in range(5)
    ]


@pytest.mark.parametrize(("cause", "terminal_kind"), (
    (ConfigError(["generation_plan_infeasible: witness"]), "generation_plan_infeasible"),
    (InternalError("generation_plan_budget: witness"), "generation_plan_budget"),
    (InternalError("generation_plan_internal: witness"), "generation_plan_internal"),
))
def test_live_plan_failure_precedes_secrets_and_writes_exact_failed_report(
        monkeypatch, tmp_path, cause, terminal_kind):
    """program 后 planner 终态零密钥/日志对象，且只替换独立 failed report。"""
    cfg = _load_example_config(monkeypatch, tmp_path)
    fixed = _fixed_sequence_paths(cfg)
    for index, path in enumerate(fixed[:-1]):
        path.write_bytes(f"old-{index}".encode())
    monkeypatch.setattr(runtime, "load", lambda *args, **kwargs: cfg)
    from labelkit.operators.generation import planner

    monkeypatch.setattr(planner, "compile_scenario_plan",
                        lambda program: (_ for _ in ()).throw(cause))
    touched: list[str] = []
    monkeypatch.setattr(runtime, "setup_logging", lambda current: touched.append("logging"))
    monkeypatch.setattr(runtime, "_run_credentials",
                        lambda current: touched.append("credentials"))
    monkeypatch.setattr(runtime, "EventLog", lambda *args, **kwargs: touched.append("eventlog"))

    with pytest.raises(type(cause)) as caught:
        runtime.execute_run("config.toml", "project.toml", CliOverrides())

    assert caught.value is cause
    assert touched == []
    assert [path.read_bytes() for path in fixed[:-1]] == [
        f"old-{index}".encode() for index in range(4)
    ]
    failed = json.loads(fixed[-1].read_text(encoding="utf-8"))
    assert set(failed) == {
        "run_attempt_id", "run_id", "artifacts_committed", "failed_slot",
        "attempts_used", "terminal_error_kind", "llm_usage", "rejected_attempts",
    }
    assert failed["run_id"] is None
    assert failed["failed_slot"] is None
    assert failed["attempts_used"] == 0
    assert failed["terminal_error_kind"] == terminal_kind
    assert failed["llm_usage"] == {}
    assert sum(failed["rejected_attempts"].values()) == 0


@pytest.mark.parametrize("entrypoint", ("validate", "dry-run"))
def test_non_live_plan_failure_reads_no_secret_and_writes_no_failed_report(
        monkeypatch, tmp_path, entrypoint):
    """validate 与 dry-run 共享 planner error，但绝不产生 failed report。"""
    cfg = _load_example_config(monkeypatch, tmp_path, dry_run=entrypoint == "dry-run")
    monkeypatch.setattr(runtime, "load", lambda *args, **kwargs: cfg)
    from labelkit.operators.generation import planner

    cause = ConfigError(["generation_plan_infeasible: witness"])
    monkeypatch.setattr(planner, "compile_scenario_plan",
                        lambda program: (_ for _ in ()).throw(cause))
    monkeypatch.setattr(runtime, "_run_credentials",
                        lambda current: (_ for _ in ()).throw(AssertionError("secret read")))

    with pytest.raises(ConfigError) as caught:
        if entrypoint == "validate":
            runtime.validate_project("config.toml", "project.toml")
        else:
            runtime.execute_run("config.toml", "project.toml", CliOverrides())
    assert caught.value is cause
    assert not Path(cfg.paths.failed_report).exists()


class _RuntimeOrchestrator:
    """只记录 runtime 绑定 program/plan 的零网络装配器。"""

    bound: list[tuple] = []

    def __init__(self, cfg, stages, ingestor, emitter, services):
        """保存配置供 run 返回。"""
        self.cfg = cfg

    def _bind_sequence_plan(self, program, plan) -> None:
        """记录 runtime 传入的同一冻结产品。"""
        self.bound.append((program, plan))

    async def run(self):
        """不执行 LLM 的最小运行终态。"""
        return SimpleNamespace(exit_code=0)


def _plan_witness(program, plan):
    """提取 digest、slots 与 calendar-constrained 时间 witness。"""
    blocks = tuple(
        (
            key,
            tuple((event.role, event.logical_time_us, event.timestamp_us, event.session_id)
                  for event in events),
        )
        for block in plan.blocks for key, events in block.items()
    )
    slots = tuple(
        (slot.slot_key, slot.variant_names, slot.catalog_row_index)
        for slot in plan.delivery_slots
    )
    return program.digest, plan.digest, slots, blocks


def test_validate_dry_run_and_live_bind_identical_plan_witness(monkeypatch, tmp_path):
    """三条命令路径调用同一 compiler/planner 并得到完全相同 witness。"""
    live_cfg = _load_example_config(monkeypatch, tmp_path)
    dry_cfg = replace(live_cfg, dry_run=True)
    configs = iter((live_cfg, dry_cfg, live_cfg))
    monkeypatch.setattr(runtime, "load", lambda *args, **kwargs: next(configs))
    original = runtime._compile_sequence_plan
    witnesses: list[tuple] = []

    def compile_and_record(cfg):
        result = original(cfg)
        assert result is not None
        witnesses.append(_plan_witness(*result))
        return result

    monkeypatch.setattr(runtime, "_compile_sequence_plan", compile_and_record)
    monkeypatch.setattr(runtime, "Orchestrator", _RuntimeOrchestrator)
    monkeypatch.setattr(runtime, "build_stages", lambda cfg: [])
    monkeypatch.setattr(runtime, "_run_credentials",
                        lambda cfg: SimpleNamespace(llm={}, embedding={}))
    _RuntimeOrchestrator.bound.clear()

    runtime.validate_project("config.toml", "project.toml")
    assert runtime.execute_run("config.toml", "project.toml", CliOverrides()) == 0
    assert runtime.execute_run("config.toml", "project.toml", CliOverrides()) == 0

    assert len(witnesses) == 3
    assert witnesses[0] == witnesses[1] == witnesses[2]
    assert [_plan_witness(*pair) for pair in _RuntimeOrchestrator.bound] == witnesses[1:]

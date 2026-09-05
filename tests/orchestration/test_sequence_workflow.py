"""v1.21 sequence 候选窗口、交织计划、声明序提交与终态账本测试。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Mapping as MappingABC
from contextlib import contextmanager
from dataclasses import fields, is_dataclass, replace
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from labelkit.cli.parser import CliOverrides
from labelkit.common.config import load
from labelkit.common.contracts.generation import (
    DedupReservation,
    DeliverySlot,
    NoiseSlot,
    PreparedCandidate,
    PreparedNoiseCandidate,
    PlannedEvent,
    SequenceRows,
)
from labelkit.common.errors import (
    CircuitBreakerTripped,
    ConfigError,
    ContextOverflowError,
    DeliveryError,
    GenerationProjectionMismatch,
    InternalError,
    LabelKitError,
    OutputTruncatedError,
    PostprocessorError,
    ProviderFatalError,
    ProviderRetryableError,
)
from labelkit.common.inference.llm_client import ProfileUsage
from labelkit.operators.dedup import DedupGroupRejected
from labelkit.operators.generation import GenerationAttemptRejected
from labelkit.operators.generation.project import canonical_delivery_row, canonical_json
from labelkit.orchestration.sequence_workflow import (
    _AttemptOutcome,
    _DeliveryController,
    _PrimaryBuild,
    _prepared_digest,
    deliver_generation,
    estimate_sequence,
    estimate_sequence_products,
)
from labelkit.orchestration import application


_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE = _ROOT / "examples" / "sequence-generation"
_RUNTIME_KEYS = (
    "queue_high_water",
    "running_high_water",
    "resource_wait_high_water",
    "commit_waiting_high_water",
    "candidate_bytes_high_water",
    "cancelled_tasks",
    "resource_wait_ms",
    "http_pool_wait_ms",
    "commit_ms",
)


class _Metrics:
    """记录候选、提交与报告观测的最小 MetricsSink。"""

    def __init__(self):
        self.counters: dict[str, int] = {}
        self.stage_times: dict[str, float] = {}
        self.high = {key.removesuffix("_high_water"): 0 for key in _RUNTIME_KEYS[:5]}
        self.totals = {key: 0 for key in _RUNTIME_KEYS[5:]}
        self.event_log = SimpleNamespace(events_written=0, dropped_events=0, closed=False)
        self.merged: list[dict[str, int]] = []

    def observe_runtime_high_water(self, key: str, value: int) -> None:
        """更新候选高水位。"""
        self.high[key] = max(self.high[key], value)

    def add_runtime_total(self, key: str, value: int) -> None:
        """累计运行时整数指标。"""
        self.totals[key] += value

    def add_stage_time(self, stage: str, value: float) -> None:
        """累计阶段时间。"""
        self.stage_times[stage] = self.stage_times.get(stage, 0.0) + value

    def merge_counts(self, counters) -> None:
        """只在正式 commit 后合并 dataset counters。"""
        copied = dict(counters)
        self.merged.append(copied)
        for key, value in copied.items():
            self.counters[key] = self.counters.get(key, 0) + value

    @contextmanager
    def capture_counts(self):
        """提供 attempt-local 测试捕获区。"""
        captured: dict[str, int] = {}
        yield captured

    @property
    def runtime_report(self) -> dict[str, int]:
        """返回冻结九字段 runtime block。"""
        return {
            "queue_high_water": self.high["queue"],
            "running_high_water": self.high["running"],
            "resource_wait_high_water": self.high["resource_wait"],
            "commit_waiting_high_water": self.high["commit_waiting"],
            "candidate_bytes_high_water": self.high["candidate_bytes"],
            **self.totals,
        }


class _Dedup:
    """验证 reservation 精确一次消费的内存协作者。"""

    def __init__(self):
        self.states: dict[str, str] = {}
        self.commits: list[str] = []
        self.discards: list[str] = []
        self.duplicates: set[str] = set()

    def add(self, reservation: DedupReservation) -> None:
        """注册一个 Reserved capability。"""
        self.states[reservation.capability_id] = "reserved"

    def group_revalidate(self, reservation: DedupReservation) -> None:
        """按最新测试前缀重验证。"""
        key = reservation.capability_id
        assert self.states[key] == "reserved"
        if key in self.duplicates:
            raise DedupGroupRejected("duplicate")
        self.states[key] = "validated"

    def group_commit(self, reservation: DedupReservation) -> None:
        """提交并消费 Validated capability。"""
        key = reservation.capability_id
        assert self.states.pop(key) == "validated"
        self.commits.append(key)

    def group_discard(self, reservation: DedupReservation) -> None:
        """丢弃并消费 Reserved 或 Validated capability。"""
        key = reservation.capability_id
        assert self.states.pop(key) in {"reserved", "validated"}
        self.discards.append(key)


class _Frontier:
    """记录声明序 check/commit 的测试 frontier。"""

    def __init__(self):
        self.checked: list[object] = []
        self.committed: list[object] = []
        self.error: Exception | None = None

    def check_primary(self, candidate):
        """返回当前 primary delta 或测试终态。"""
        if self.error is not None:
            raise self.error
        self.checked.append(candidate.slot)
        return ("primary", candidate.slot.slot_key)

    def check_noise(self, candidate):
        """返回当前 noise delta。"""
        if self.error is not None:
            raise self.error
        self.checked.append(candidate.noise_slot)
        return ("noise", candidate.noise_slot.ordinal)

    def commit(self, delta) -> None:
        """记录不可失败的 delta 消费。"""
        self.committed.append(delta)


class _Emitter:
    """记录 failed report 的延迟 emitter。"""

    def __init__(self):
        self.failed: list[dict] = []
        self.committed: list[object] = []

    def write_failed_report(self, report) -> None:
        """保存无内容 failed report。"""
        self.failed.append(dict(report))

    def commit(self, product) -> None:
        """记录测试中不应发生的 manifest-last 提交。"""
        self.committed.append(product)


def _view(*, enabled: bool = False, profile: str = "unused"):
    """构造 sequence class 的最小下游视图。"""
    stage = SimpleNamespace(enabled=enabled, llm=profile, judges=())
    return SimpleNamespace(
        description="class", quality=stage, annotate=stage, verify=stage,
        rubric=SimpleNamespace(criteria=()),
    )


def _slot(ordinal: int) -> DeliverySlot:
    """构造一个稳定 instruction-only DeliverySlot。"""
    return DeliverySlot(
        slot_key=f"set/{ordinal:06d}", source_name="source", scenario_index=ordinal,
        sequence_class="cls", pattern_name=None, variant_names=(), catalog_row_index=None,
    )


def _controller(
    count: int = 2,
    *,
    capacity: int = 2,
    attempts: int = 2,
    dedup: _Dedup | None = None,
) -> tuple[_DeliveryController, _Metrics, _Dedup]:
    """构造不接触 LLM 或文件的 coordinator 控制器。"""
    metrics = _Metrics()
    dedup = dedup or _Dedup()
    profiles = {"model": SimpleNamespace(max_concurrency=capacity)}
    cfg = SimpleNamespace(
        llm_profiles=profiles,
        embedding_profiles={},
        output=SimpleNamespace(repair_llm=None),
        quality=SimpleNamespace(enabled=False),
        annotate=SimpleNamespace(enabled=False),
        verify=SimpleNamespace(enabled=False),
        frame_annotate=SimpleNamespace(enabled=False, llm="frame"),
        dedup=SimpleNamespace(
            semantic=False, semantic_embedding=None, minhash_threshold=0.85,
            minhash_num_perm=16, ngram=3,
        ),
        run=SimpleNamespace(modality="text"),
        trace=SimpleNamespace(enabled=False),
        config_digest="c",
        project_digest="p",
    )
    program = SimpleNamespace(
        semantic_profile="model", evaluation_profile="model", max_slot_attempts=attempts,
        class_views={"cls": _view()}, frame_classes={"frame": SimpleNamespace(enabled=True)},
        limits=SimpleNamespace(retained_content_bytes=10**9),
        planner_seed=7, timeline=SimpleNamespace(utc_offset_minutes=480),
        counterfactual_sets=(), interleaving=None, mode="instruction_only",
        digest="d" * 64, noise=None,
    )
    plan = SimpleNamespace(
        delivery_slots=tuple(_slot(index) for index in range(count)), noise_slots=(),
        replay_layouts=(), interleaving_layouts=(), interleaving_opportunities=0,
        interleaving_pattern_opportunities={}, blocks=(), primary_sessions=count,
        digest="p" * 64,
    )
    paths = SimpleNamespace(
        project="project.toml", project_root="/tmp", input=None, output="out.jsonl",
        report="out.report.json", rejects=None, sidecar=None, trace=None,
        stream="out.stream.jsonl", manifest="out.manifest.json",
        failed_report="out.failed.report.json",
    )
    generation = SimpleNamespace(
        config=cfg, metrics=metrics, schema_engine=SimpleNamespace(stats={}),
        llm=SimpleNamespace(usage_by_profile={}), tasks=object(),
    )
    services = SimpleNamespace(
        generation=generation, dedup=dedup, quality=None, annotate=None,
        verify=None, emitter=_Emitter(),
    )
    request = SimpleNamespace(
        program=program, plan=plan, paths=paths,
        run_attempt_id="a" * 32, run_id="b" * 32,
    )
    controller = _DeliveryController(request, services)
    controller._frontier = _Frontier()
    return controller, metrics, dedup


def _synthetic_candidate(ordinal: int, retained: int = 1):
    """构造只用于 coordinator 顺序测试的完成候选。"""
    return SimpleNamespace(ordinal=ordinal, retained_content_bytes=retained)


async def _wait_until(predicate, turns: int = 5000) -> None:
    """在固定事件循环轮数内等待测试谓词成立。"""
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


def _local_interleaving_products():
    """编译不物化凭据的本地四槽强制交织 program/plan。"""
    cfg = load(
        _EXAMPLE / "config-local-4b.toml",
        _EXAMPLE / "project-runtime-four-slot.toml",
        CliOverrides(),
    )
    from labelkit.operators.generation.planner import compile_scenario_plan
    from labelkit.operators.generation.program import compile_generation_program

    program = compile_generation_program(cfg)
    return cfg, program, compile_scenario_plan(program)


def _sequence_report_for_products(cfg, program, plan) -> dict:
    """为离线 exact-shape 测试填入一轮完整成功交付账本。"""
    controller, _metrics, _dedup = _controller(count=len(plan.delivery_slots))
    controller.generation.config = cfg
    controller.request.program = program
    controller.request.plan = plan
    for slot in plan.delivery_slots:
        main = {"_meta": {"generation": {
            "pattern": slot.pattern_name,
            "variant": slot.variant_names[0],
        }}}
        controller.state.sequences.append(SequenceRows(main, ({}, {}, {}), 0))
    controller.state.noise_rows.append({})
    controller.state.replays.append(SimpleNamespace(rows=({}, {}, {})))
    controller.state.sequence_attempts = 4
    controller.state.noise_attempts = 1
    return controller._sequence_report([{} for _ in range(16)])


def test_estimate_uses_the_frozen_example_program_and_plan() -> None:
    """估算继续绑定生产 compiler/planner 的 8/27 教学计划。"""
    cfg = load(
        _EXAMPLE / "config.toml", _EXAMPLE / "project.toml", CliOverrides(dry_run=True)
    )
    from labelkit.operators.generation.planner import compile_scenario_plan
    from labelkit.operators.generation.program import compile_generation_program

    program = compile_generation_program(cfg)
    plan = compile_scenario_plan(program)
    estimate = estimate_sequence_products(cfg, program, plan)
    assert estimate["records"] == 8
    assert estimate["batches"] == 2
    assert estimate["sequence"]["stream_rows"] == 27
    assert estimate["sequence"]["successful_attempt_lower_bound"] == estimate["total_calls"]


def test_selected_pair_estimate_and_success_report_have_exact_shape() -> None:
    """强制 pair 的 estimate/report 共享冻结交织统计与键序。"""
    cfg, program, plan = _local_interleaving_products()
    estimate = estimate_sequence_products(cfg, program, plan)["sequence"]
    report = _sequence_report_for_products(cfg, program, plan)
    assert list(report) == [
        "mode", "run_attempt_id", "run_id", "delivery_digest", "artifacts_committed",
        "program_digest", "plan_digest", "planned_sets", "delivered_sets",
        "planned_sequences", "delivered_sequences", "primary_events",
        "interleaving_opportunities", "primary_sessions", "interleaved_primary_sessions",
        "by_interleaving_pattern", "noise_events", "replay_sequences", "replay_events",
        "replay_tail_sessions", "stream_rows", "sequence_slot_attempts",
        "noise_slot_attempts", "sequence_calls", "by_pattern", "rejected_attempts",
    ]
    expected = {
        "plan_digest": plan.digest,
        "interleaving_opportunities": 2,
        "primary_sessions": 2,
        "interleaved_primary_sessions": 2,
        "by_interleaving_pattern": {
            "runtime_pair": {"eligible_opportunities": 2, "selected_sessions": 2},
        },
    }
    assert {key: report[key] for key in expected} == expected
    assert {key: estimate[key] for key in expected} == expected
    assert report["primary_events"] == 12 and report["stream_rows"] == 16


def test_selected_none_estimate_and_success_report_have_exact_shape(monkeypatch) -> None:
    """启用配置但抽中 none 时保留机会并输出零 selected session。"""
    cfg, program, _forced_plan = _local_interleaving_products()
    from labelkit.operators.generation import planner
    from labelkit.operators.generation.program import generation_program_digest

    original_random = planner.generation_random
    interleaving = replace(program.interleaving, no_interleaving_weight=1)
    program = replace(program, interleaving=interleaving, digest="")
    program = replace(program, digest=generation_program_digest(program))
    monkeypatch.setattr(
        planner,
        "generation_random",
        lambda domain, value: (
            0 if domain == "interleaving_pattern_choice" else original_random(domain, value)
        ),
    )
    plan = planner.compile_scenario_plan(program)
    estimate = estimate_sequence_products(cfg, program, plan)["sequence"]
    report = _sequence_report_for_products(cfg, program, plan)
    expected = {
        "interleaving_opportunities": 2,
        "primary_sessions": 4,
        "interleaved_primary_sessions": 0,
        "by_interleaving_pattern": {
            "runtime_pair": {"eligible_opportunities": 2, "selected_sessions": 0},
        },
    }
    assert {key: report[key] for key in expected} == expected
    assert {key: estimate[key] for key in expected} == expected


def test_candidate_capacity_sums_distinct_enabled_resource_keys_only() -> None:
    """禁用 class view 不扩张候选窗口，启用资源按不同 key 求和。"""
    controller, _metrics, _dedup = _controller(count=20, capacity=2)
    cfg = controller.generation.config
    cfg.llm_profiles.update({
        "evaluation": SimpleNamespace(max_concurrency=3),
        "disabled": SimpleNamespace(max_concurrency=100),
    })
    controller.request.program.evaluation_profile = "evaluation"
    controller.request.program.class_views["cls"] = _view(enabled=False, profile="disabled")
    assert controller._candidate_capacity("primary", 20) == 5
    controller.request.program.class_views["cls"] = _view(enabled=True, profile="disabled")
    cfg.quality.enabled = True
    assert controller._candidate_capacity("primary", 200) == 105
    cfg.quality.enabled = False
    cfg.verify.enabled = True
    controller.request.program.class_views["cls"] = _view(enabled=False, profile="disabled")
    assert controller._candidate_capacity("primary", 200) == 5
    controller.request.program.class_views["cls"] = _view(enabled=True, profile="disabled")
    assert controller._candidate_capacity("primary", 200) == 105
    cfg.quality.enabled = cfg.annotate.enabled = True
    assert controller._candidate_capacity("primary", 200) == 105
    cfg.llm_profiles["frame"] = SimpleNamespace(max_concurrency=11)
    cfg.frame_annotate.enabled = True
    controller.request.program.frame_classes["frame"].enabled = False
    assert controller._candidate_capacity("primary", 200) == 105
    controller.request.program.frame_classes["frame"].enabled = True
    assert controller._candidate_capacity("primary", 200) == 116


async def test_downstream_barriers_bind_only_frozen_program_views() -> None:
    """workflow 按 quality→annotate→verify 屏障传递 program-bound 类与帧视图。"""
    cfg = load(_EXAMPLE / "config.toml", _EXAMPLE / "project.toml", CliOverrides())
    frame_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}, "derived": {"type": "integer"}},
    }
    model_frame_schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    cfg = replace(cfg, frame_schema=frame_schema, model_frame_schema=model_frame_schema)
    from labelkit.common.extensions.hooks import ResolvedHook
    from labelkit.operators.generation.planner import compile_scenario_plan
    from labelkit.operators.generation.program import compile_generation_program

    hook = ResolvedHook("hooks.py:complete", lambda obj, record: {"frozen": True})
    views = dict(cfg.class_views)
    views["ticket_booking"] = replace(views["ticket_booking"], annotate=replace(
        views["ticket_booking"].annotate, postprocessor=hook.reference, resolved_postprocessor=hook,
    ))
    frame_views = dict(cfg.frame_class_views)
    frame_views["task_request"] = replace(frame_views["task_request"], resolved_postprocessor=hook)
    cfg = replace(cfg, class_views=views, frame_class_views=frame_views)
    program = compile_generation_program(cfg)
    plan = compile_scenario_plan(program)
    controller, metrics, _dedup = _controller(count=1)
    controller.generation.config = replace(
        cfg, frame_schema={"type": "integer"}, model_frame_schema={"type": "integer"},
        class_views={}, frame_class_views={},
    )
    controller.request.program = program
    controller.request.plan = plan
    order: list[str] = []

    class _Stage:
        def __init__(self, name: str):
            self.name = name

        async def run_attempt(self, request):
            assert request.run_context.cfg.class_views is program.class_views
            assert request.run_context.cfg.frame_class_views is program.frame_classes
            assert request.run_context.cfg.frame_schema is program.frame_schema
            assert request.run_context.cfg.frame_schema["type"] == "object"
            assert request.run_context.cfg.model_frame_schema is program.model_frame_schema
            assert request.run_context.cfg.model_frame_schema["type"] == "object"
            frozen_cfg = request.run_context.cfg
            assert frozen_cfg.class_views["ticket_booking"].annotate.resolved_postprocessor.target is hook.target
            assert frozen_cfg.frame_class_views["task_request"].resolved_postprocessor.target is hook.target
            assert order == ["quality", "annotate", "verify"][:len(order)]
            order.append(self.name)
            return SimpleNamespace(
                accepted=True, rejected_stage=None,
                dataset_counters={f"{self.name}.accepted": 1},
            )

    controller.services.quality = _Stage("quality")
    controller.services.annotate = _Stage("annotate")
    controller.services.verify = _Stage("verify")
    transaction = SimpleNamespace(class_views=program.class_views)
    counters = await controller._run_downstream(
        transaction, plan.delivery_slots[0], 0, 1,
    )
    assert order == ["quality", "annotate", "verify"]
    assert counters == {
        "quality.accepted": 1, "annotate.accepted": 1, "verify.accepted": 1,
    }
    assert metrics.counters == {}


async def test_six_hundred_reverse_completion_commits_zero_to_five_ninety_nine() -> None:
    """六百个固定大小候选反序完成仍有界，并且按声明序快速 commit。"""
    total = 600
    candidate_size = 65536
    controller, metrics, _dedup = _controller(count=total, capacity=total)
    gates = [asyncio.Event() for _ in range(total)]
    finished = [asyncio.Event() for _ in range(total)]
    completion: list[int] = []
    commits: list[int] = []
    commit_times: list[int] = []

    async def capture(_self, _phase, _slot_value, ordinal, attempt_index):
        await gates[ordinal].wait()
        completion.append(ordinal)
        finished[ordinal].set()
        payload = f"{ordinal:06d}".ljust(candidate_size, "x")
        candidate = _synthetic_candidate(ordinal, candidate_size)
        candidate.canonical_payload = payload
        return _AttemptOutcome(attempt_index, candidate, None, None)

    def commit(_self, outcome):
        commits.append(outcome.candidate.ordinal)
        commit_times.append(time.perf_counter_ns())
        return None

    controller._capture_attempt = MethodType(capture, controller)
    controller._commit_primary_head = MethodType(commit, controller)
    run = asyncio.create_task(controller._deliver_slot_phases())
    arrival_started = time.perf_counter_ns()
    for ordinal in reversed(range(total)):
        await asyncio.sleep(0.0001)
        gates[ordinal].set()
        await finished[ordinal].wait()
    arrival_elapsed = time.perf_counter_ns() - arrival_started
    await asyncio.wait_for(run, timeout=5)
    commit_elapsed = max(1, commit_times[-1] - commit_times[0])
    arrival_rate = total * 1_000_000_000 / arrival_elapsed
    commit_rate = total * 1_000_000_000 / commit_elapsed
    print(json.dumps({
        "candidate_bytes_high_water": metrics.high["candidate_bytes"],
        "commit_service_rate_s": round(commit_rate, 3),
        "prepared_candidate_arrival_rate_s": round(arrival_rate, 3),
    }, sort_keys=True))
    assert completion == list(reversed(range(total)))
    assert commits == list(range(total))
    assert metrics.high["commit_waiting"] == total
    assert metrics.high["candidate_bytes"] == total * candidate_size
    assert commit_rate > arrival_rate


async def test_six_hundred_pending_reservations_coexist_and_commit_independently() -> None:
    """六百 pending capability 同时存在，低槽 commit 不使其余 reservation stale。"""
    total = 600
    dedup = _Dedup()
    controller, _metrics, _ = _controller(count=total, capacity=total, dedup=dedup)
    gates = [asyncio.Event() for _ in range(total)]
    finished = [asyncio.Event() for _ in range(total)]

    async def capture(_self, _phase, _slot_value, ordinal, attempt_index):
        reservation = _reservation(f"pending-{ordinal}")
        dedup.add(reservation)
        candidate = _prepared_primary(controller, reservation, slot=_slot(ordinal))
        await gates[ordinal].wait()
        finished[ordinal].set()
        return _AttemptOutcome(attempt_index, candidate, reservation, None)

    controller._capture_attempt = MethodType(capture, controller)
    run = asyncio.create_task(controller._deliver_slot_phases())
    await _wait_until(lambda: len(dedup.states) == total)
    assert set(dedup.states.values()) == {"reserved"}
    for ordinal in reversed(range(total)):
        gates[ordinal].set()
        await finished[ordinal].wait()
    await run
    assert dedup.states == {}
    assert dedup.discards == []
    assert dedup.commits == [f"pending-{ordinal}" for ordinal in range(total)]


async def test_capacity_one_and_six_hundred_freeze_identical_commit_digest() -> None:
    """容量只影响调度，不改变 deterministic provider 产物的声明序摘要。"""

    async def run(capacity: int) -> str:
        controller, _metrics, _dedup = _controller(count=600, capacity=capacity)
        committed: list[int] = []

        async def capture(_self, _phase, _slot_value, ordinal, attempt_index):
            await asyncio.sleep(0)
            return _AttemptOutcome(attempt_index, _synthetic_candidate(ordinal), None, None)

        def commit(_self, outcome):
            committed.append(outcome.candidate.ordinal)
            return None

        controller._capture_attempt = MethodType(capture, controller)
        controller._commit_primary_head = MethodType(commit, controller)
        await controller._deliver_slot_phases()
        assert committed == list(range(600))
        return hashlib.sha256(canonical_json(committed).encode("utf-8")).hexdigest()

    assert await run(1) == await run(600)


async def test_completed_high_slot_does_not_admit_tail_outside_window() -> None:
    """高槽完成仍占位置，只有 head commit 后才接纳窗口外 tail。"""
    controller, _metrics, _dedup = _controller(count=8, capacity=4)
    gates = [asyncio.Event() for _ in range(8)]
    started: set[int] = set()
    finished: set[int] = set()

    async def capture(_self, _phase, _slot_value, ordinal, attempt_index):
        started.add(ordinal)
        await gates[ordinal].wait()
        finished.add(ordinal)
        return _AttemptOutcome(attempt_index, _synthetic_candidate(ordinal), None, None)

    controller._capture_attempt = MethodType(capture, controller)
    controller._commit_primary_head = MethodType(lambda _self, _outcome: None, controller)
    run = asyncio.create_task(controller._deliver_slot_phases())
    await _wait_until(lambda: started == {0, 1, 2, 3})
    gates[3].set()
    await _wait_until(lambda: 3 in finished)
    assert started == {0, 1, 2, 3}
    gates[0].set()
    await _wait_until(lambda: 4 in started)
    for gate in gates:
        gate.set()
    await run


async def test_expensive_primary_chain_overlaps_across_distinct_slots() -> None:
    """不同 primary coordinator 可同时穿过完整昂贵链的每道屏障。"""
    controller, _metrics, _dedup = _controller(count=4, capacity=4)
    stages = (
        "generation", "evaluation", "reserve", "quality", "annotate",
        "verify", "assemble", "candidate_reconcile",
    )
    arrivals = {stage: [] for stage in stages}
    barriers = {stage: asyncio.Event() for stage in stages}

    async def prepare(_slot_value, ordinal, _attempt_index):
        for stage in stages:
            arrivals[stage].append(ordinal)
            if len(arrivals[stage]) == 4:
                barriers[stage].set()
            await barriers[stage].wait()
        return _AttemptOutcome(0, _synthetic_candidate(ordinal), None, None)

    controller._prepare_primary = prepare
    controller._commit_primary_head = MethodType(lambda _self, _outcome: None, controller)
    await asyncio.wait_for(controller._deliver_slot_phases(), timeout=5)
    assert all(sorted(values) == [0, 1, 2, 3] for values in arrivals.values())


@pytest.mark.parametrize(
    "rejection", ["dedup", "reconcile", "sequence_memory_budget"]
)
async def test_commit_time_rejection_retries_same_head_before_advancing(rejection: str) -> None:
    """commit-time rejection 显式 retry，同槽下一 attempt 成功后才推进。"""
    controller, _metrics, _dedup = _controller(count=2, capacity=2, attempts=2)
    captures: list[tuple[int, int]] = []
    commits: list[tuple[int, int]] = []

    async def capture(_self, _phase, _slot_value, ordinal, attempt_index):
        captures.append((ordinal, attempt_index))
        candidate = SimpleNamespace(
            ordinal=ordinal, attempt_index=attempt_index, retained_content_bytes=1
        )
        return _AttemptOutcome(attempt_index, candidate, None, None)

    def commit(_self, outcome):
        key = (outcome.candidate.ordinal, outcome.attempt_index)
        commits.append(key)
        return rejection if key == (0, 0) else None

    controller._capture_attempt = MethodType(capture, controller)
    controller._commit_primary_head = MethodType(commit, controller)
    await controller._deliver_slot_phases()
    assert captures.count((0, 0)) == captures.count((0, 1)) == 1
    assert commits[:2] == [(0, 0), (0, 1)]
    assert controller.state.rejected[rejection] == 1


async def test_high_recoverable_is_not_counted_when_low_head_exhausts() -> None:
    """高槽提前拒绝只缓存；低槽耗尽后不泄漏 attempt 或 rejection。"""
    controller, _metrics, _dedup = _controller(count=2, capacity=2, attempts=1)
    high_done = asyncio.Event()

    async def capture(_self, _phase, slot, ordinal, attempt_index):
        if ordinal == 0:
            await high_done.wait()
            error = GenerationAttemptRejected("quality", slot.slot_key)
        else:
            high_done.set()
            error = GenerationAttemptRejected("annotate", slot.slot_key)
        return _AttemptOutcome(attempt_index, None, None, error)

    controller._capture_attempt = MethodType(capture, controller)
    with pytest.raises(DeliveryError):
        await controller._deliver_slot_phases()
    assert controller.state.sequence_attempts == 1
    assert controller.state.rejected["quality"] == 1
    assert controller.state.rejected["annotate"] == 0
    assert controller.state.failed_slot == "set/000000"
    assert controller.state.attempts_used == 1


async def test_low_exhaustion_discards_buffer_owned_high_reservation() -> None:
    """低槽耗尽后清空高槽 reservation、候选缓冲与局部计数。"""
    dedup = _Dedup()
    controller, metrics, _ = _controller(count=2, capacity=2, attempts=1, dedup=dedup)
    high_ready = asyncio.Event()
    reservation = _reservation("buffer-owned")
    dedup.add(reservation)
    candidate = _prepared_primary(
        controller, reservation, counters={"annotate.accepted": 7}, slot=_slot(1),
    )

    async def capture(_self, _phase, slot, ordinal, attempt_index):
        if ordinal == 1:
            high_ready.set()
            return _AttemptOutcome(attempt_index, candidate, reservation, None)
        await high_ready.wait()
        error = GenerationAttemptRejected("quality", slot.slot_key)
        return _AttemptOutcome(attempt_index, None, None, error)

    controller._capture_attempt = MethodType(capture, controller)
    with pytest.raises(DeliveryError):
        await controller._deliver_slot_phases()
    assert dedup.states == {}
    assert dedup.discards == ["buffer-owned"]
    assert metrics.merged == []
    assert controller._buffer == {}
    assert controller._waiting == controller._candidate_bytes == 0


async def test_fatal_after_previous_slot_success_has_zero_attempts_for_fatal_slot() -> None:
    """前槽成功不能污染下一槽首次 fatal 的 attempts_used。"""
    controller, _metrics, _dedup = _controller(count=2, capacity=2, attempts=2)
    first_committed = asyncio.Event()

    async def capture(_self, _phase, _slot_value, ordinal, attempt_index):
        if ordinal == 1:
            await first_committed.wait()
            return _AttemptOutcome(
                attempt_index, None, None, ProviderFatalError("fatal", "model", 401)
            )
        return _AttemptOutcome(attempt_index, _synthetic_candidate(ordinal), None, None)

    def commit(_self, outcome):
        first_committed.set()
        return None

    controller._capture_attempt = MethodType(capture, controller)
    controller._commit_primary_head = MethodType(commit, controller)
    with pytest.raises(ProviderFatalError):
        await controller._deliver_slot_phases()
    assert controller.state.failed_slot == "set/000001"
    assert controller.state.attempts_used == 0
    assert controller.state.sequence_attempts == 1


async def test_simultaneous_fatals_choose_smallest_coordinator_ordinal() -> None:
    """同一事件循环批次的多个 fatal 稳定选择更小声明序身份。"""
    controller, _metrics, _dedup = _controller(count=3, capacity=3)
    release = asyncio.Event()
    never = asyncio.Event()

    async def capture(_self, _phase, _slot_value, ordinal, attempt_index):
        await release.wait()
        if ordinal == 0:
            await never.wait()
        if ordinal == 1:
            await asyncio.sleep(0)
        return _AttemptOutcome(
            attempt_index, None, None,
            ProviderFatalError(f"fatal-{ordinal}", "model", 401),
        )

    controller._capture_attempt = MethodType(capture, controller)
    run = asyncio.create_task(controller._deliver_slot_phases())
    release.set()
    with pytest.raises(ProviderFatalError, match="fatal-1"):
        await run
    assert controller.state.failed_slot == "set/000001"
    assert controller.state.attempts_used == 0


async def test_external_cancellation_waits_cleanup_and_clears_slot_ledger() -> None:
    """外部取消等待全部 coordinator finally，并使用 null/zero failed ledger。"""
    controller, _metrics, _dedup = _controller(count=4, capacity=4)
    started: set[int] = set()
    blocker = asyncio.Event()
    controller.state.failed_slot = "set/000003"
    controller.state.attempts_used = 2

    async def capture(_self, _phase, _slot_value, ordinal, _attempt_index):
        started.add(ordinal)
        await blocker.wait()

    controller._capture_attempt = MethodType(capture, controller)
    run = asyncio.create_task(controller._deliver_slot_phases())
    await _wait_until(lambda: started == {0, 1, 2, 3})
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert controller.state.failed_slot is None
    assert controller.state.attempts_used == 0
    assert controller._buffer == {}
    assert controller._waiting == controller._candidate_bytes == 0


async def test_recoverable_then_fatal_preserves_only_consumed_attempt_count() -> None:
    """同槽一次 recoverable 后 fatal 的账本精确为一。"""
    controller, _metrics, _dedup = _controller(count=1, capacity=1, attempts=3)

    async def capture(_self, _phase, slot, _ordinal, attempt_index):
        if attempt_index == 0:
            error = GenerationAttemptRejected("quality", slot.slot_key)
        else:
            error = ProviderFatalError("fatal", "model", 401)
        return _AttemptOutcome(attempt_index, None, None, error)

    controller._capture_attempt = MethodType(capture, controller)
    with pytest.raises(ProviderFatalError):
        await controller._deliver_slot_phases()
    assert controller.state.rejected["quality"] == 1
    assert controller.state.sequence_attempts == 1
    assert controller.state.failed_slot == "set/000000"
    assert controller.state.attempts_used == 1


@pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit, GeneratorExit])
async def test_control_exceptions_are_never_frozen_as_business_outcomes(control) -> None:
    """控制异常从 attempt capture 原样穿透。"""
    controller, _metrics, _dedup = _controller(count=1)

    async def prepare(_slot_value, _ordinal, _attempt_index):
        raise control()

    controller._prepare_primary = prepare
    with pytest.raises(control):
        await controller._capture_attempt("primary", _slot(0), 0, 0)


@pytest.mark.parametrize(
    "control", [asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit],
)
async def test_coordinator_control_exception_cancels_siblings_and_escapes_original(
    control,
) -> None:
    """coordinator control 使用独立通知完成 cleanup，调用方不见 BaseExceptionGroup。"""
    controller, _metrics, _dedup = _controller(count=2, capacity=2)
    error = control()
    sibling_started = asyncio.Event()
    sibling_cleaned = asyncio.Event()

    async def capture(_self, _phase, _slot_value, ordinal, _attempt_index):
        if ordinal == 0:
            await sibling_started.wait()
            raise error
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cleaned.set()

    controller._capture_attempt = MethodType(capture, controller)
    with pytest.raises(control) as caught:
        await controller._deliver_slot_phases()
    assert caught.value is error
    assert sibling_cleaned.is_set()
    assert controller.state.failed_slot is None
    assert controller.state.attempts_used == 0


@pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("boundary", ["revalidate", "commit"])
def test_control_exception_discards_unconsumed_primary_reservation(
    control, boundary: str,
) -> None:
    """head 控制异常原样穿透，并精确丢弃尚未消费的 reservation。"""

    class _ControlDedup(_Dedup):
        def group_revalidate(self, reservation: DedupReservation) -> None:
            if boundary == "revalidate":
                raise control()
            super().group_revalidate(reservation)

        def group_commit(self, reservation: DedupReservation) -> None:
            if boundary == "commit":
                raise control()
            super().group_commit(reservation)

    dedup = _ControlDedup()
    controller, _metrics, _ = _controller(count=1, dedup=dedup)
    reservation = _reservation(f"control-{boundary}")
    dedup.add(reservation)
    candidate = _prepared_primary(controller, reservation)
    with pytest.raises(control):
        controller._commit_primary_head(_AttemptOutcome(0, candidate, reservation, None))
    assert dedup.states == {}
    assert dedup.discards == [f"control-{boundary}"]
    assert dedup.commits == []


def _reservation(name: str) -> DedupReservation:
    """构造一个稳定测试 reservation。"""
    return DedupReservation(name, 0, ("record",), ("cluster",))


def _prepared_primary(
    controller, reservation, *, counters=None, slot=None,
) -> PreparedCandidate:
    """构造可直接进入 head 临界区的冻结 primary candidate。"""
    active_slot = slot or controller.request.plan.delivery_slots[0]
    row = SequenceRows({"_meta": {"id": f"main-{active_slot.slot_key}"}}, (), 0)
    candidate = PreparedCandidate(
        active_slot, 1, (), (row,), (), reservation, counters or {}, 0, "",
    )
    return replace(candidate, digest=_prepared_digest("primary", candidate))


def _independent_prepared_value(value):
    """不用 workflow helper 把 prepared carrier 转成 canonical 树。"""
    if is_dataclass(value):
        return {
            field.name: _independent_prepared_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, MappingABC):
        return {str(key): _independent_prepared_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_independent_prepared_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_independent_prepared_value(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    return value


def _independent_prepared_digest(domain: str, candidate) -> str:
    """独立执行 v1.20 prepared domain、排除自身与 canonical SHA-256。"""
    material = {
        field.name: _independent_prepared_value(getattr(candidate, field.name))
        for field in fields(candidate)
        if field.name != "digest"
    }
    payload = json.dumps(
        ["labelkit:v1.20", f"prepared_{domain}", material],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def test_actual_prepared_primary_and_noise_domain_fixed_vectors() -> None:
    """实际 carrier 必须命中互不混域的独立 prepared fixed vectors。"""
    reservation = DedupReservation(
        "fixed-capability", 0, ("record-a", "record-b"), ("cluster-a",),
    )
    row = SequenceRows({"_meta": {"id": "main-set/000000"}}, (), 0)
    primary = PreparedCandidate(
        _slot(0), 1, (), (row,), (), reservation, {"generated": 1}, 0, "",
    )
    noise = PreparedNoiseCandidate(
        NoiseSlot("event-key", 0, "frame", "topic", 1000, 0, (), "noise-session"),
        1, "p" * 64, {"payload": {"text": "fixed"}}, (1, 2, 3),
        {"generated": 1}, 37, "",
    )
    assert _prepared_digest("primary", primary) == (
        "01d5b7329a511dd1ae2542b603c9ada16871cd8976f7e126f0aa7b5a82147018"
    )
    assert _prepared_digest("primary", primary) == _independent_prepared_digest(
        "primary", primary,
    )
    assert _prepared_digest("noise", noise) == (
        "ba1228faa47a11daa8475f74d3cc80bece93e6111adc1f8a7420bda7844fecf2"
    )
    assert _prepared_digest("noise", noise) == _independent_prepared_digest("noise", noise)
    assert _prepared_digest("primary", primary) != _prepared_digest("noise", noise)


@pytest.mark.parametrize("surface", ["record", "frame_member", "frame_primary"])
def test_prepared_digest_changes_with_final_postprocessor_fields(surface) -> None:
    """等长代码字段变化必须改变真实 prepared 摘要，JSON 键序变化必须保持摘要。"""
    main = {
        "derived_code": "code-a",
        "_meta": {"stream": {"members": [{
            "index": 0, "id": "event-a", "annotation": {"derived_code": "code-a"},
        }]}},
    }
    primary = {"payload": {"text": "event"}, "_meta": {"annotation": {"derived_code": "code-a"}}}

    def prepare(main_row, primary_row):
        retained = sum(len(canonical_delivery_row(row)) + 1 for row in (main_row, primary_row))
        rows = SequenceRows(main_row, (primary_row,), retained)
        candidate = PreparedCandidate(
            _slot(0), 1, (), (rows,), (), _reservation("final-annotation"), {"annotated": 1}, retained, "",
        )
        return replace(candidate, digest=_prepared_digest("primary", candidate))

    original = prepare(main, primary)
    changed_main, changed_primary = json.loads(json.dumps([main, primary]))
    if surface == "record":
        changed_main["derived_code"] = "code-b"
    elif surface == "frame_member":
        changed_main["_meta"]["stream"]["members"][0]["annotation"]["derived_code"] = "code-b"
    else:
        changed_primary["_meta"]["annotation"]["derived_code"] = "code-b"
    changed = prepare(changed_main, changed_primary)
    assert original.retained_content_bytes == changed.retained_content_bytes
    assert original.sequences[0].retained_content_bytes == changed.sequences[0].retained_content_bytes
    assert replace(original, sequences=changed.sequences, digest="") == replace(changed, digest="")
    assert original.digest != changed.digest
    reordered = prepare(dict(reversed(list(main.items()))), dict(reversed(list(primary.items()))))
    assert original.digest == reordered.digest


async def test_coordinator_discards_reservation_if_buffer_transfer_fails() -> None:
    """候选插入缓冲失败时 reservation 仍由 coordinator finally 路径消费。"""
    dedup = _Dedup()
    controller, _metrics, _ = _controller(count=1, dedup=dedup)
    reservation = _reservation("transfer-failure")
    dedup.add(reservation)
    candidate = _prepared_primary(controller, reservation)

    async def capture(_phase, _slot_value, _ordinal, attempt_index):
        return _AttemptOutcome(attempt_index, candidate, reservation, None)

    class _FailingQueue:
        def put_nowait(self, _notice):
            raise InternalError("buffer transfer failed")

    controller._capture_attempt = capture
    controller._notices = _FailingQueue()
    with pytest.raises(InternalError, match="buffer transfer failed"):
        await controller._coordinate_primary(_slot(0), 0, asyncio.Semaphore(0))
    assert dedup.states == {}
    assert dedup.discards == ["transfer-failure"]


def test_invalid_candidate_discards_validated_reservation_once() -> None:
    """candidate 类型不变式失败仍在 group_commit 前消费 reservation。"""
    dedup = _Dedup()
    controller, _metrics, _ = _controller(count=1, dedup=dedup)
    reservation = _reservation("invalid-candidate")
    dedup.add(reservation)
    outcome = _AttemptOutcome(0, SimpleNamespace(), reservation, None)
    with pytest.raises(InternalError, match="invalid primary outcome"):
        controller._commit_primary_head(outcome)
    assert dedup.states == {}
    assert dedup.discards == ["invalid-candidate"]
    assert dedup.commits == []


def test_precommit_internal_error_discards_validated_reservation_once() -> None:
    """counter 不变式失败发生在 group_commit 前且清空 pending registry。"""
    dedup = _Dedup()
    controller, _metrics, _ = _controller(count=1, dedup=dedup)
    reservation = _reservation("bad-counter")
    dedup.add(reservation)
    candidate = _prepared_primary(controller, reservation, counters={"bad": -1})
    outcome = _AttemptOutcome(0, candidate, reservation, None)
    with pytest.raises(InternalError):
        controller._commit_primary_head(outcome)
    assert dedup.states == {}
    assert dedup.discards == ["bad-counter"]
    assert dedup.commits == []


def test_frontier_internal_error_discards_without_group_commit() -> None:
    """frontier internal 失败仍精确消费未提交 reservation。"""
    dedup = _Dedup()
    controller, _metrics, _ = _controller(count=1, dedup=dedup)
    reservation = _reservation("frontier")
    dedup.add(reservation)
    controller._frontier.error = InternalError("frontier internal")
    candidate = _prepared_primary(controller, reservation)
    with pytest.raises(InternalError):
        controller._commit_primary_head(_AttemptOutcome(0, candidate, reservation, None))
    assert dedup.states == {}
    assert dedup.discards == ["frontier"]
    assert dedup.commits == []


def test_successful_group_commit_never_discards_consumed_reservation() -> None:
    """group_commit 正常返回后只提交一次，不执行二次 discard。"""
    dedup = _Dedup()
    controller, _metrics, _ = _controller(count=1, dedup=dedup)
    reservation = _reservation("success")
    dedup.add(reservation)
    candidate = _prepared_primary(controller, reservation, counters={"generated": 1})
    result = controller._commit_primary_head(
        _AttemptOutcome(0, candidate, reservation, None)
    )
    assert result is None
    assert dedup.states == {}
    assert dedup.commits == ["success"]
    assert dedup.discards == []


def test_post_group_commit_internal_failure_never_discards_consumed_reservation() -> None:
    """group_commit 返回后的内部失败保留终态，且不二次消费 capability。"""
    dedup = _Dedup()
    controller, _metrics, _ = _controller(count=1, dedup=dedup)
    reservation = _reservation("post-commit")
    dedup.add(reservation)
    candidate = _prepared_primary(controller, reservation)

    def fail(_delta):
        raise InternalError("frontier commit failed")

    controller._frontier.commit = fail
    with pytest.raises(InternalError, match="frontier commit failed"):
        controller._commit_primary_head(_AttemptOutcome(0, candidate, reservation, None))
    assert dedup.states == {}
    assert dedup.commits == ["post-commit"]
    assert dedup.discards == []


@pytest.mark.parametrize("total", [100, 300, 600])
def test_primary_frontier_and_commit_work_are_linear_per_candidate(total: int) -> None:
    """primary head 每槽恰执行一次 frontier check/commit，不重扫前缀。"""
    dedup = _Dedup()
    controller, _metrics, _ = _controller(count=total, capacity=total, dedup=dedup)
    for ordinal, slot in enumerate(controller.request.plan.delivery_slots):
        reservation = _reservation(f"linear-{ordinal}")
        dedup.add(reservation)
        candidate = _prepared_primary(controller, reservation, slot=slot)
        outcome = _AttemptOutcome(0, candidate, reservation, None)
        assert controller._commit_primary_head(outcome) is None
    assert len(controller._frontier.checked) == total
    assert len(controller._frontier.committed) == total
    assert len(dedup.commits) == total
    assert dedup.states == {}


def test_frozen_candidate_digest_tamper_is_reconcile_rejection() -> None:
    """篡改 frozen digest 在正式 dedup commit 前拒绝并清空 reservation。"""
    dedup = _Dedup()
    controller, _metrics, _ = _controller(count=1, dedup=dedup)
    reservation = _reservation("tampered")
    dedup.add(reservation)
    candidate = replace(_prepared_primary(controller, reservation), digest="0" * 64)
    result = controller._commit_primary_head(
        _AttemptOutcome(0, candidate, reservation, None)
    )
    assert result == "reconcile"
    assert dedup.states == {}
    assert dedup.commits == []


def test_dedup_revalidation_precedes_saved_downstream_failure() -> None:
    """同一高槽同时 duplicate 与 quality failure 时固定记录 dedup。"""
    dedup = _Dedup()
    controller, _metrics, _ = _controller(count=1, dedup=dedup)
    reservation = _reservation("duplicate")
    dedup.add(reservation)
    dedup.duplicates.add("duplicate")
    error = GenerationAttemptRejected("quality", "set/000000")
    result = controller._commit_primary_head(_AttemptOutcome(0, None, reservation, error))
    assert result == "dedup"
    assert dedup.states == {}
    assert dedup.discards == ["duplicate"]


def test_same_speculative_candidates_keep_lowest_ordinal_first_writer() -> None:
    """两个 pending 候选只允许较低 ordinal 成为正式 first writer。"""
    dedup = _Dedup()
    controller, _metrics, _ = _controller(count=2, dedup=dedup)
    lower = _reservation("same-lower")
    higher = _reservation("same-higher")
    dedup.add(lower)
    dedup.add(higher)
    lower_candidate = _prepared_primary(controller, lower, slot=_slot(0))
    higher_candidate = _prepared_primary(controller, higher, slot=_slot(1))
    assert controller._commit_primary_head(
        _AttemptOutcome(0, lower_candidate, lower, None)
    ) is None
    dedup.duplicates.add("same-higher")
    assert controller._commit_primary_head(
        _AttemptOutcome(0, higher_candidate, higher, None)
    ) == "dedup"
    assert dedup.commits == ["same-lower"]
    assert dedup.discards == ["same-higher"]
    assert dedup.states == {}


@pytest.mark.parametrize("total", [100, 300, 600])
def test_noise_similarity_commit_path_is_linear(monkeypatch, total: int) -> None:
    """noise head 只对持久 phase filter 做一次 probe 与一次 commit。"""
    controller, _metrics, _dedup = _controller(count=0, capacity=total)
    controller._noise_similarity = controller._initial_similarity_filter()
    probes = commits = 0
    filter_type = type(controller._noise_similarity)
    original_probe = filter_type.probe
    original_commit = filter_type.commit

    def probe(instance, text):
        nonlocal probes
        probes += 1
        return original_probe(instance, text)

    def commit(instance, signature):
        nonlocal commits
        commits += 1
        return original_commit(instance, signature)

    candidates = []
    for ordinal in range(total):
        slot = NoiseSlot(
            f"event-{ordinal}", ordinal, "frame", f"topic-{ordinal}", ordinal * 1000, 0, (),
            f"session-{ordinal}",
        )
        unique = hashlib.sha256(f"noise-{ordinal}".encode()).hexdigest()
        row = {"payload": {"text": unique}}
        signature = controller._noise_signature(canonical_json(row["payload"]))
        candidate = PreparedNoiseCandidate(slot, 1, f"d{ordinal}", row, signature, {}, 1, "")
        candidates.append(replace(candidate, digest=_prepared_digest("noise", candidate)))
    monkeypatch.setattr(filter_type, "probe", probe)
    monkeypatch.setattr(filter_type, "commit", commit)
    for candidate in candidates:
        result = controller._commit_noise_head(
            _AttemptOutcome(0, candidate, None, None)
        )
        assert result is None
    assert probes == total
    assert commits == total


def test_final_full_reconcile_failure_is_internal_and_clears_slot_ledger(monkeypatch) -> None:
    """最终独立 CrossView 失败不消费 attempt，也不保留 failed_slot。"""
    controller, _metrics, _dedup = _controller(count=0)
    controller.state.failed_slot = "set/000003"
    controller.state.attempts_used = 2
    from labelkit.operators.generation import project

    calls = 0

    def mismatch(_request):
        nonlocal calls
        calls += 1
        raise GenerationProjectionMismatch("mismatch")

    monkeypatch.setattr(project, "reconcile_views", mismatch)
    with pytest.raises(InternalError, match="final CrossView invariant"):
        controller._final_reconcile()
    assert calls == 1
    assert controller.state.failed_slot is None
    assert controller.state.attempts_used == 0


async def test_deliver_runs_final_crossview_before_manifest_commit(monkeypatch) -> None:
    """成功交付必须在工件准备与 commit 前执行最终独立 CrossView。"""
    controller, _metrics, _dedup = _controller(count=0)
    order: list[str] = []

    async def phases(_self) -> None:
        order.append("phases")

    class Emitter:
        def prepare_product(self, _main, _stream, _report):
            order.append("prepare")
            return "product"

        def commit(self, product) -> None:
            assert product == "product"
            order.append("commit")

    from labelkit.operators.generation import project
    monkeypatch.setattr(project, "validate_plan_identity", lambda *_args: None)
    monkeypatch.setattr(project, "CrossViewFrontier", lambda _program, _plan: object())
    controller._deliver_slot_phases = MethodType(phases, controller)
    controller._final_reconcile = MethodType(lambda _self: order.append("crossview"), controller)
    controller._ordered_stream_rows = MethodType(lambda _self: [], controller)
    controller._success_report = MethodType(lambda _self, _rows: {}, controller)
    controller.services.emitter = Emitter()

    assert await controller.deliver() == "product"
    assert order == ["phases", "crossview", "prepare", "commit"]


def test_success_and_failed_reports_use_frozen_runtime_key_order() -> None:
    """成功顶层与 failed report 都遵守 CONTRACTS 的 runtime 位置和九键顺序。"""
    controller, _metrics, _dedup = _controller(count=0)
    report = controller._success_report(())
    assert list(report) == [
        "run", "counts", "schema_engine", "generate", "runtime",
        "trace", "llm_usage", "timing",
    ]
    assert list(report["runtime"]) == list(_RUNTIME_KEYS)
    error = ProviderFatalError("fatal", "model", 401)
    controller.write_failed_report(error)
    failed = controller.services.emitter.failed[-1]
    assert list(failed) == [
        "run_attempt_id", "run_id", "artifacts_committed", "failed_slot",
        "attempts_used", "terminal_error_kind", "llm_usage", "rejected_attempts", "runtime",
    ]
    assert list(failed["runtime"]) == list(_RUNTIME_KEYS)


def test_instruction_only_success_report_has_exact_sequence_shape() -> None:
    """instruction-only 成功路径输出冻结 sequence 闭集与零交织统计。"""
    controller, _metrics, _dedup = _controller(count=1)
    controller.state.sequences.append(SequenceRows(
        {"_meta": {}}, ({"_meta": {"event": {"event_id": "event"}}},), 0,
    ))
    controller.state.sequence_attempts = 1

    sequence = controller._success_report(({},))["generate"]["sequence"]

    assert tuple(sequence) == (
        "mode", "run_attempt_id", "run_id", "delivery_digest", "artifacts_committed",
        "program_digest", "plan_digest", "planned_sets", "delivered_sets",
        "planned_sequences", "delivered_sequences", "primary_events",
        "interleaving_opportunities", "primary_sessions", "interleaved_primary_sessions",
        "by_interleaving_pattern", "noise_events", "replay_sequences", "replay_events",
        "replay_tail_sessions", "stream_rows", "sequence_slot_attempts",
        "noise_slot_attempts", "sequence_calls", "by_pattern", "rejected_attempts",
    )
    assert tuple(sequence["sequence_calls"]) == (
        "scenario_seed_calls", "baseline_event_plan_calls", "variant_event_plan_calls",
        "frame_render_calls", "semantic_evaluation_calls", "noise_render_calls",
        "noise_evaluation_calls",
    )
    assert tuple(sequence["rejected_attempts"]) == (
        "scenario_schema", "event_schema", "post_validator_invalid",
        "post_validator_exception", "state_transition", "frame_schema",
        "coupling_evaluation", "pattern_evaluation", "state_evaluation",
        "semantic_evaluation", "sequence_memory_budget", "context_overflow",
        "output_truncated", "provider_retryable_exhausted", "dedup", "quality",
        "annotate", "verify", "reconcile", "noise_schema", "noise_semantic",
        "noise_similarity", "noise_memory_budget", "noise_context_overflow",
        "noise_output_truncated", "noise_provider_retryable_exhausted", "noise_reconcile",
    )
    assert sequence | {
        "run_attempt_id": "a" * 32,
        "run_id": "b" * 32,
        "program_digest": "d" * 64,
        "plan_digest": "p" * 64,
        "sequence_calls": {key: 0 for key in sequence["sequence_calls"]},
        "rejected_attempts": {key: 0 for key in sequence["rejected_attempts"]},
    } == sequence
    assert {
        key: sequence[key]
        for key in (
            "mode", "planned_sets", "delivered_sets", "planned_sequences",
            "delivered_sequences", "primary_events", "interleaving_opportunities",
            "primary_sessions", "interleaved_primary_sessions", "by_interleaving_pattern",
            "noise_events", "replay_sequences", "replay_events", "replay_tail_sessions",
            "stream_rows", "sequence_slot_attempts", "noise_slot_attempts", "by_pattern",
        )
    } == {
        "mode": "instruction_only", "planned_sets": 1, "delivered_sets": 1,
        "planned_sequences": 1, "delivered_sequences": 1, "primary_events": 1,
        "interleaving_opportunities": 0, "primary_sessions": 1,
        "interleaved_primary_sessions": 0, "by_interleaving_pattern": {},
        "noise_events": 0, "replay_sequences": 0, "replay_events": 0,
        "replay_tail_sessions": 0, "stream_rows": 1, "sequence_slot_attempts": 1,
        "noise_slot_attempts": 0, "by_pattern": {},
    }


_SEQUENCE_RETRY_CASES = (
    (GenerationAttemptRejected("scenario_schema", "slot"), "scenario_schema"),
    (GenerationAttemptRejected("event_schema", "slot"), "event_schema"),
    (GenerationAttemptRejected("post_validator_invalid", "slot"), "post_validator_invalid"),
    (GenerationAttemptRejected("post_validator_exception", "slot"), "post_validator_exception"),
    (GenerationAttemptRejected("state_transition", "slot"), "state_transition"),
    (GenerationAttemptRejected("frame_schema", "slot"), "frame_schema"),
    (GenerationAttemptRejected("coupling_evaluation", "slot"), "coupling_evaluation"),
    (GenerationAttemptRejected("pattern_evaluation", "slot"), "pattern_evaluation"),
    (GenerationAttemptRejected("state_evaluation", "slot"), "state_evaluation"),
    (GenerationAttemptRejected("semantic_evaluation", "slot"), "semantic_evaluation"),
    (GenerationAttemptRejected("sequence_memory_budget", "slot"), "sequence_memory_budget"),
    (GenerationAttemptRejected("quality", "slot"), "quality"),
    (GenerationAttemptRejected("annotate", "slot"), "annotate"),
    (GenerationAttemptRejected("verify", "slot"), "verify"),
    (GenerationAttemptRejected("reconcile", "slot"), "reconcile"),
    (DedupGroupRejected("duplicate"), "dedup"),
    (ProviderRetryableError("retry", "model", 1), "provider_retryable_exhausted"),
    (ContextOverflowError("overflow", "precheck"), "context_overflow"),
    (OutputTruncatedError("truncated"), "output_truncated"),
)


@pytest.mark.parametrize(("failure", "bucket"), _SEQUENCE_RETRY_CASES)
async def test_every_primary_recoverable_boundary_retries_whole_slot_once(
    failure, bucket,
) -> None:
    """旧 primary retry 矩阵完整迁移到 coordinator outcome seam。"""
    controller, _metrics, _dedup = _controller(count=1, capacity=1, attempts=2)
    attempts: list[int] = []
    original = controller._commit_primary_head

    async def capture(_self, _phase, _slot_value, ordinal, attempt_index):
        attempts.append(attempt_index)
        if attempt_index == 0:
            return _AttemptOutcome(attempt_index, None, None, failure)
        return _AttemptOutcome(attempt_index, _synthetic_candidate(ordinal), None, None)

    def commit(_self, outcome):
        return original(outcome) if outcome.error is not None else None

    controller._capture_attempt = MethodType(capture, controller)
    controller._commit_primary_head = MethodType(commit, controller)
    await controller._deliver_slot_phases()
    assert attempts == [0, 1]
    assert controller.state.sequence_attempts == 2
    assert controller.state.rejected[bucket] == 1
    assert sum(controller.state.rejected.values()) == 1


_NOISE_RETRY_CASES = (
    (GenerationAttemptRejected("noise_schema", "noise"), "noise_schema"),
    (GenerationAttemptRejected("noise_semantic", "noise"), "noise_semantic"),
    (GenerationAttemptRejected("noise_similarity", "noise"), "noise_similarity"),
    (GenerationAttemptRejected("noise_memory_budget", "noise"), "noise_memory_budget"),
    (GenerationAttemptRejected("reconcile", "noise"), "noise_reconcile"),
    (ProviderRetryableError("retry", "model", 1), "noise_provider_retryable_exhausted"),
    (ContextOverflowError("overflow", "precheck"), "noise_context_overflow"),
    (OutputTruncatedError("truncated"), "noise_output_truncated"),
)


@pytest.mark.parametrize(("failure", "bucket"), _NOISE_RETRY_CASES)
async def test_every_noise_recoverable_boundary_retries_without_early_commit(
    failure, bucket,
) -> None:
    """旧 noise retry 矩阵完整迁移且只有第二次成功推进。"""
    controller, _metrics, _dedup = _controller(count=0, capacity=1, attempts=2)
    slot = NoiseSlot("event", 0, "frame", "topic", 1000, 0, (), "session")
    controller.request.plan.noise_slots = (slot,)
    attempts: list[int] = []
    original = controller._commit_noise_head

    async def capture(_self, _phase, _slot_value, ordinal, attempt_index):
        attempts.append(attempt_index)
        if attempt_index == 0:
            return _AttemptOutcome(attempt_index, None, None, failure)
        return _AttemptOutcome(attempt_index, _synthetic_candidate(ordinal), None, None)

    def commit(_self, outcome):
        return original(outcome) if outcome.error is not None else None

    controller._capture_attempt = MethodType(capture, controller)
    controller._commit_noise_head = MethodType(commit, controller)
    await controller._deliver_slot_phases()
    assert attempts == [0, 1]
    assert controller.state.noise_attempts == 2
    assert controller.state.rejected[bucket] == 1
    assert sum(controller.state.rejected.values()) == 1


@pytest.mark.parametrize(
    "failure",
    (
        ProviderFatalError("fatal", "model", 401),
        CircuitBreakerTripped("breaker"),
        InternalError("internal"),
        PostprocessorError(),
    ),
)
async def test_terminal_primary_failure_never_consumes_or_retries(failure) -> None:
    """sequence terminal 矩阵立即取消且 attempt/rejection 均为零。"""
    controller, _metrics, _dedup = _controller(count=3, capacity=3, attempts=3)
    calls = 0

    async def capture(_self, _phase, _slot_value, ordinal, attempt_index):
        nonlocal calls
        calls += 1
        if ordinal == 0:
            return _AttemptOutcome(attempt_index, None, None, failure)
        await asyncio.Event().wait()

    controller._capture_attempt = MethodType(capture, controller)
    with pytest.raises(type(failure)) as caught:
        await controller._deliver_slot_phases()
    assert caught.value is failure
    assert 1 <= calls <= 3
    assert controller.state.sequence_attempts == 0
    assert sum(controller.state.rejected.values()) == 0
    assert controller.state.failed_slot == "set/000000"
    assert controller.state.attempts_used == 0


def test_attempt_seed_uses_program_seed_not_mutated_run_config() -> None:
    """attempt RNG 身份只绑定冻结 program，不读取后来变化的 run.seed。"""
    controller, _metrics, _dedup = _controller(count=1)
    before = controller._attempt_seed("set/000000", 1, "quality")
    controller.generation.config.run.seed = 999999
    after = controller._attempt_seed("set/000000", 1, "quality")
    assert before == after
    assert before != controller._attempt_seed("set/000000", 2, "quality")


async def test_retry_reuses_exact_frozen_program_and_plan_objects() -> None:
    """同槽重试不重新编译或替换 Application 绑定的 program/plan。"""
    controller, _metrics, _dedup = _controller(count=1, capacity=1, attempts=2)
    expected = (id(controller.request.program), id(controller.request.plan))
    observed: list[tuple[int, int]] = []

    async def capture(_self, _phase, slot, ordinal, attempt_index):
        observed.append((id(controller.request.program), id(controller.request.plan)))
        if attempt_index == 0:
            error = GenerationAttemptRejected("quality", slot.slot_key)
            return _AttemptOutcome(attempt_index, None, None, error)
        return _AttemptOutcome(attempt_index, _synthetic_candidate(ordinal), None, None)

    controller._capture_attempt = MethodType(capture, controller)
    controller._commit_primary_head = MethodType(
        lambda _self, outcome: "quality" if outcome.error is not None else None,
        controller,
    )
    await controller._deliver_slot_phases()
    assert observed == [expected, expected]


async def test_interleaving_retry_and_reverse_completion_reuse_frozen_plan() -> None:
    """启用交织后 recoverable retry 与逆序完成不替换 pair/layout/timestamp。"""
    cfg, program, plan = _local_interleaving_products()
    controller, _metrics, _dedup = _controller(count=4, capacity=4, attempts=2)
    controller.generation.config = cfg
    controller.request.program = program
    controller.request.plan = plan
    gates: dict[tuple[int, int], asyncio.Event] = {}
    started: set[tuple[int, int]] = set()
    completion: list[tuple[int, int]] = []
    observed = []
    commits: list[int] = []
    expected = (
        id(program), id(plan), _plan_witness(program, plan),
        plan.interleaving_layouts, plan.interleaving_opportunities,
    )
    original_commit = controller._commit_primary_head

    async def capture(_self, _phase, slot, ordinal, attempt_index):
        if _phase == "noise":
            return _AttemptOutcome(
                attempt_index, _synthetic_candidate(ordinal), None, None,
            )
        key = (ordinal, attempt_index)
        gate = gates.setdefault(key, asyncio.Event())
        started.add(key)
        observed.append((
            id(controller.request.program), id(controller.request.plan),
            _plan_witness(controller.request.program, controller.request.plan),
            controller.request.plan.interleaving_layouts,
            controller.request.plan.interleaving_opportunities,
        ))
        await gate.wait()
        completion.append(key)
        if key == (0, 0):
            error = GenerationAttemptRejected("quality", slot.slot_key)
            return _AttemptOutcome(attempt_index, None, None, error)
        return _AttemptOutcome(attempt_index, _synthetic_candidate(ordinal), None, None)

    def commit(_self, outcome):
        if outcome.error is not None:
            return original_commit(outcome)
        commits.append(outcome.candidate.ordinal)
        return None

    controller._capture_attempt = MethodType(capture, controller)
    controller._commit_primary_head = MethodType(commit, controller)
    controller._commit_noise_head = MethodType(
        lambda _self, _outcome: None,
        controller,
    )
    run = asyncio.create_task(controller._deliver_slot_phases())
    await _wait_until(lambda: {(index, 0) for index in range(4)} <= started)
    for ordinal in reversed(range(4)):
        gates[(ordinal, 0)].set()
        await _wait_until(lambda value=ordinal: (value, 0) in completion)
    await _wait_until(lambda: (0, 1) in started)
    gates[(0, 1)].set()
    await run
    assert completion == [(3, 0), (2, 0), (1, 0), (0, 0), (0, 1)]
    assert commits == [0, 1, 2, 3]
    assert observed == [expected] * 5
    assert controller.state.sequence_attempts == 5
    assert controller.state.rejected["quality"] == 1


@pytest.mark.parametrize("domain", ("primary", "noise"))
@pytest.mark.parametrize(
    ("error_type", "mapped"),
    ((GenerationProjectionMismatch, True), (InternalError, False)),
)
def test_candidate_reconcile_maps_only_typed_projection_mismatch(
    monkeypatch, domain: str, error_type, mapped: bool,
) -> None:
    """candidate-local 只把 typed projection mismatch 转成当前 attempt 拒绝。"""
    controller, _metrics, _dedup = _controller(count=1)
    from labelkit.operators.generation import project

    error = error_type("injected reconcile failure")
    target = (
        "reconcile_primary_candidate" if domain == "primary"
        else "reconcile_noise_candidate"
    )
    monkeypatch.setattr(project, target, lambda _request: (_ for _ in ()).throw(error))
    with pytest.raises(GenerationAttemptRejected if mapped else error_type) as caught:
        if domain == "primary":
            closure = SimpleNamespace(
                slot=_slot(0), witnesses=(), rows=(), replays=(), retained=0,
            )
            controller._reconcile_primary_candidate(closure)
        else:
            slot = NoiseSlot("event", 0, "frame", "topic", 1000, 0, (), "session")
            controller._reconcile_noise_candidate(slot, "digest", {"payload": {}}, 0)
    if mapped:
        assert caught.value.kind == "reconcile"
    else:
        assert caught.value is error


def _real_temporal_bundle():
    """从真实教学 program 构造带 business-time/resource/replay 的闭包。"""
    cfg = load(_EXAMPLE / "config.toml", _EXAMPLE / "project.toml", CliOverrides())
    from labelkit.operators.generation.program import compile_generation_program
    from tests.operators.generation.test_project import _build_temporal_projected_set

    return _build_temporal_projected_set(compile_generation_program(cfg))


def test_primary_candidate_and_capacity_include_projected_replay_bytes() -> None:
    """真实 candidate 装配与容量门都计入非空 replay canonical bytes。"""
    program, plan, projection, sequence, _noise, expected_replay = _real_temporal_bundle()
    controller, metrics, dedup = _controller(count=1)
    controller.request.program = program
    controller.request.plan = plan
    controller.request.run_id = "a" * 32
    slot = plan.delivery_slots[0]

    class _FinalEmitter(_Emitter):
        """把既有最终 sequence rows 送入真实 replay 装配入口。"""

        def assemble_sequence(self, request):
            assert request.projection == projection
            return sequence

    controller.services.emitter = _FinalEmitter()
    transaction = controller._transaction(slot, (projection,))
    reservation = _reservation("replay-retained")
    build = _PrimaryBuild(
        slot, 1, 0, transaction, (projection,),
        controller._projection_witnesses((projection,)), reservation, {"annotated": 1},
    )
    candidate = controller._assemble_primary_candidate(build)

    sequence_bytes = sum(
        len(canonical_delivery_row(row)) + 1
        for row in (sequence.main_row, *sequence.primary_stream_rows)
    )
    replay_bytes = sum(
        len(canonical_delivery_row(row)) + 1 for row in expected_replay.rows
    )
    assert replay_bytes > 0
    assert candidate.replays == (expected_replay,)
    assert candidate.retained_content_bytes == sequence_bytes + replay_bytes
    assert candidate.sequences[0].retained_content_bytes == sequence_bytes
    assert candidate.replays[0].retained_content_bytes == replay_bytes

    controller.request.program = replace(
        program,
        limits=replace(program.limits, retained_content_bytes=sequence_bytes + replay_bytes - 1),
    )
    dedup.add(reservation)
    result = controller._commit_primary_head(
        _AttemptOutcome(0, candidate, reservation, None),
    )
    assert result == "sequence_memory_budget"
    assert dedup.discards == ["replay-retained"]
    assert metrics.merged == []
    assert controller.state.sequences == []
    assert controller.state.replays == []
    assert controller.state.retained_bytes == 0


def _assert_precommit_state_is_empty(controller, metrics, dedup, discarded) -> None:
    """统一证明 terminal temporal attempt 没有任何正式提交。"""
    assert dedup.states == {}
    assert dedup.commits == []
    assert dedup.discards == discarded
    assert metrics.merged == [] and metrics.counters == {}
    assert controller.state.sequences == []
    assert controller.state.noise_rows == [] and controller.state.replays == []
    assert controller.state.sources == {} and controller.state.retained_bytes == 0
    assert controller.state.sequence_attempts == controller.state.noise_attempts == 0
    assert controller.services.emitter.committed == []
    assert controller.services.emitter.failed == []


@pytest.mark.parametrize(
    "surface",
    ("primary", "replay", "annotation", "containment", "replay_construction"),
)
async def test_controller_primary_precommit_temporal_tamper_is_terminal_and_atomic(
    surface: str,
) -> None:
    """实际 primary/replay 篡改经 controller capture 后 terminal 且 reservation 全回滚。"""
    program, plan, projection, sequence, _noise, replay = _real_temporal_bundle()
    from labelkit.common.config._temporal import IntervalContainmentSpec
    from labelkit.operators.generation.project import projection_witness
    from tests.operators.generation.test_project import _tamper_temporal_row, _thaw

    if surface == "primary":
        rows = list(sequence.primary_stream_rows)
        index = next(i for i, row in enumerate(rows)
                     if row["_meta"]["event"]["time_bindings"])
        rows[index] = _tamper_temporal_row(rows[index], "payload_time")
        sequence = replace(sequence, primary_stream_rows=tuple(rows))
    elif surface == "replay":
        rows = list(replay.rows)
        index = next(i for i, row in enumerate(rows)
                     if row["_meta"]["event"]["time_bindings"])
        rows[index] = _tamper_temporal_row(rows[index], "descriptor")
        replay = replace(replay, rows=tuple(rows))
    elif surface == "annotation":
        main = _thaw(sequence.main_row)
        main["started_at"] += 1
        sequence = replace(sequence, main_row=main)
    elif surface == "containment":
        pattern_name = plan.delivery_slots[0].pattern_name
        pattern = program.patterns[pattern_name]
        patterns = dict(program.patterns)
        patterns[pattern_name] = replace(
            pattern,
            containments=(IntervalContainmentSpec("request", "acknowledge"),),
        )
        program = replace(program, patterns=patterns)
    else:
        rows = list(sequence.primary_stream_rows)
        rows[0] = _tamper_temporal_row(rows[0], "duration")
        sequence = replace(sequence, primary_stream_rows=tuple(rows))
    dedup = _Dedup()
    controller, metrics, _ = _controller(count=1, dedup=dedup)
    controller.request.program = program
    controller.request.plan = plan
    controller.request.run_id = "a" * 32
    controller._candidate_capacity = lambda _phase, _total: 1
    slot = plan.delivery_slots[0]
    reservation = _reservation(f"temporal-{surface}")
    closure = SimpleNamespace(
        slot=slot,
        witnesses=(projection_witness(projection),),
        rows=(sequence,),
        replays=(replay,),
        retained=sequence.retained_content_bytes + replay.retained_content_bytes,
    )

    async def reserve(_transaction, _context):
        dedup.add(reservation)
        return reservation

    async def downstream(_transaction, _slot_value, _attempt_index, _batch_no):
        return {"generated": 1}

    def assemble(build):
        assert build.reservation is reservation
        if surface == "replay_construction":
            controller._project_replays(build.slot, closure.rows)
        else:
            controller._reconcile_primary_candidate(closure)
        raise AssertionError("temporal reconcile unexpectedly accepted")

    async def generate(_slot_value: object, _attempt_index: int) -> tuple[()]:
        return ()

    controller._generate_traces = generate
    controller._project_traces = lambda *_args: (projection,)
    controller._projection_witnesses = lambda _items: closure.witnesses
    controller._transaction = lambda *_args: SimpleNamespace()
    controller._dedup_reserve = reserve
    controller._run_downstream = downstream
    controller._assemble_primary_candidate = assemble
    async with asyncio.TaskGroup() as task_group:
        terminal = await controller._run_phase("primary", (slot,), task_group)
    assert isinstance(terminal, InternalError)
    expected_reason = {
        "annotation": "annotation time",
        "containment": "containment",
        "replay_construction": "replay source temporal facts",
    }.get(surface)
    if expected_reason is not None:
        assert expected_reason in str(terminal)
    assert controller._recoverable_kind(terminal, "primary") is None
    assert controller._terminal_kind(terminal) == "generation_downstream_contract"
    _assert_precommit_state_is_empty(
        controller, metrics, dedup, [f"temporal-{surface}"],
    )


async def test_controller_noise_precommit_temporal_tamper_is_terminal_and_atomic() -> None:
    """实际 noise payload-time 篡改经 controller capture 后 terminal 且零正式提交。"""
    program, plan, _projection, _sequence, noise, _replay = _real_temporal_bundle()
    controller, metrics, dedup = _controller(count=0)
    controller.request.program = program
    controller.request.plan = plan
    controller.request.run_id = "a" * 32
    controller._candidate_capacity = lambda _phase, _total: 1
    slot = plan.noise_slots[0]
    payload = dict(noise["payload"])
    payload["timestamp"] += 1

    async def render(_slot_value, _attempt_index):
        return payload

    controller._render_and_evaluate_noise = render
    async with asyncio.TaskGroup() as task_group:
        terminal = await controller._run_phase("noise", (slot,), task_group)
    assert isinstance(terminal, InternalError)
    assert controller._recoverable_kind(terminal, "noise") is None
    assert controller._terminal_kind(terminal) == "generation_downstream_contract"
    _assert_precommit_state_is_empty(controller, metrics, dedup, [])


@pytest.mark.parametrize(("retained", "limit", "expected"), ((10, 10, None), (11, 10, "sequence_memory_budget")))
def test_retained_cap_accepts_exact_limit_and_rejects_one_byte_over(
    retained: int, limit: int, expected: str | None,
) -> None:
    """retained gate 恰上限接受，超一 byte 在 group_commit 前拒绝。"""
    dedup = _Dedup()
    controller, _metrics, _ = _controller(count=1, dedup=dedup)
    controller.request.program.limits.retained_content_bytes = limit
    reservation = _reservation(f"retained-{retained}")
    dedup.add(reservation)
    base = _prepared_primary(controller, reservation)
    candidate = replace(base, retained_content_bytes=retained, digest="")
    candidate = replace(candidate, digest=_prepared_digest("primary", candidate))
    result = controller._commit_primary_head(_AttemptOutcome(0, candidate, reservation, None))
    assert result == expected
    assert dedup.commits == ([] if expected else [f"retained-{retained}"])
    assert dedup.states == {}


def test_reconcile_rejection_precedes_simultaneous_retained_overflow() -> None:
    """同一候选 CrossView 与 retained 双故障时先落 reconcile。"""
    dedup = _Dedup()
    controller, _metrics, _ = _controller(count=1, dedup=dedup)
    controller.request.program.limits.retained_content_bytes = 0
    reservation = _reservation("double-failure")
    dedup.add(reservation)
    candidate = _prepared_primary(controller, reservation)
    candidate = replace(candidate, retained_content_bytes=1, digest="")
    candidate = replace(candidate, digest=_prepared_digest("primary", candidate))
    controller._frontier.error = GenerationProjectionMismatch("collision")
    result = controller._commit_primary_head(_AttemptOutcome(0, candidate, reservation, None))
    assert result == "reconcile"
    assert dedup.states == {}


def test_noise_reconcile_rejection_precedes_simultaneous_retained_overflow() -> None:
    """noise candidate 的 frontier mismatch 同样先于 retained 溢出。"""
    controller, _metrics, _dedup = _controller(count=0)
    controller.request.program.limits.retained_content_bytes = 0
    controller._noise_similarity = controller._initial_similarity_filter()
    controller._frontier.error = GenerationProjectionMismatch("noise collision")
    slot = NoiseSlot("event", 0, "frame", "topic", 1000, 0, (), "session")
    row = {"payload": {"text": "novel noise"}}
    signature = controller._noise_signature(canonical_json(row["payload"]))
    candidate = PreparedNoiseCandidate(slot, 1, "payload-digest", row, signature, {}, 1, "")
    candidate = replace(candidate, digest=_prepared_digest("noise", candidate))
    result = controller._commit_noise_head(_AttemptOutcome(0, candidate, None, None))
    assert result == "noise_reconcile"
    assert controller.state.noise_rows == []
    assert controller.state.retained_bytes == 0


@pytest.mark.parametrize(
    ("closed", "expected"),
    ((False, {"events": 4, "dropped_events": 1}),
     (True, {"events": 3, "dropped_events": 2})),
)
def test_trace_report_accounts_for_pending_terminal_event(closed, expected) -> None:
    """成功报告预记稍后由运行汇发出的最终 run.end。"""
    controller, _metrics, _dedup = _controller(count=0)
    controller.generation.config.trace.enabled = True
    controller.generation.metrics.event_log = SimpleNamespace(
        events_written=3, dropped_events=1, closed=closed,
        cfg=SimpleNamespace(path="trace.jsonl"),
    )
    assert controller._trace_report() == {
        "enabled": True, "path": "trace.jsonl", **expected,
    }


def test_planned_session_id_comes_from_unique_plan_branch() -> None:
    """信封 session 身份只取唯一冻结 branch，重复或缺失 fail closed。"""
    controller, _metrics, _dedup = _controller(count=1)
    slot = controller.request.plan.delivery_slots[0]
    event = PlannedEvent("event", "position_000", 0, 0, 1000, 0, (), "planned-session")
    controller.request.plan.blocks = ({(slot.slot_key, None): (event,)},)
    assert controller._planned_session_id(slot, None) == "planned-session"
    controller.request.plan.blocks = ()
    with pytest.raises(InternalError, match="invalid planned session"):
        controller._planned_session_id(slot, None)


def test_attempt_item_carries_the_frozen_temporal_context() -> None:
    """attempt 信封从唯一计划分支冻结 member 时间与资源。"""
    controller, _metrics, _dedup = _controller(count=1)
    slot = controller.request.plan.delivery_slots[0]
    event = PlannedEvent(
        "event", "position_000", 0, 0, 1000, 2000,
        ("foreground_app",), "planned-session",
    )
    controller.request.plan.blocks = ({(slot.slot_key, None): (event,)},)
    row = {
        "_meta": {
            "event": {"event_id": "event-id", "frame_class": "frame"},
        },
    }
    projection = SimpleNamespace(
        main_record=SimpleNamespace(members=(SimpleNamespace(id="event-id"),)),
        primary_stream_rows=(row,),
    )

    item = controller._item(slot, None, projection)

    assert item.session_id == "planned-session"
    assert item.classification.label == "cls"
    assert item.member_classifications["event-id"].label == "frame"
    member = item.temporal_context.members[0]
    assert member.event_id == "event-id"
    assert (member.timestamp_us, member.duration_us) == (1000, 2000)
    assert member.resources == ("foreground_app",)


def test_teaching_example_arithmetic_and_call_families_are_exact() -> None:
    """教学计划继续严格满足 2/8/22 + 2 noise + 3 replay = 27。"""
    cfg = load(_EXAMPLE / "config.toml", _EXAMPLE / "project.toml", CliOverrides())
    from labelkit.operators.generation.planner import compile_scenario_plan
    from labelkit.operators.generation.program import compile_generation_program

    program = compile_generation_program(cfg)
    plan = compile_scenario_plan(program)
    estimate = estimate_sequence_products(cfg, program, plan)["sequence"]
    assert estimate_sequence(cfg)["sequence"] == estimate
    assert estimate == {
        "mode": "declared",
        "program_digest": program.digest,
        "plan_digest": plan.digest,
        "planned_sets": 2,
        "planned_sequences": 8,
        "primary_events": 22,
        "interleaving_opportunities": 0,
        "primary_sessions": 8,
        "interleaved_primary_sessions": 0,
        "by_interleaving_pattern": {},
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


def test_by_pattern_counts_shared_variants_once_across_sources() -> None:
    """多个 source 复用 pattern/variant 时按真实 delivered rows 汇总。"""
    controller, _metrics, _dedup = _controller(count=0)
    positive = SimpleNamespace(name="positive")
    missing = SimpleNamespace(name="missing")
    timeout = SimpleNamespace(name="timeout")
    controller.request.program.counterfactual_sets = (
        SimpleNamespace(pattern="shared", count=1, variants=(positive, missing)),
        SimpleNamespace(pattern="shared", count=1, variants=(positive, timeout)),
    )
    for variant in ("positive", "missing", "positive", "timeout"):
        controller.state.sequences.append(SequenceRows(
            {"_meta": {"generation": {"pattern": "shared", "variant": variant}}}, (), 0
        ))
    assert controller._by_pattern() == {
        "shared": {
            "positive": {"planned": 2, "delivered": 2},
            "missing": {"planned": 1, "delivered": 1},
            "timeout": {"planned": 1, "delivered": 1},
        }
    }


def test_stream_rows_are_globally_stable_timestamp_sorted() -> None:
    """最终 stream 可形成跨 owner A-B-A，而不是按 sequence 分组拼接。"""
    controller, _metrics, _dedup = _controller(count=0)

    def row(timestamp: str, name: str) -> dict:
        return {"name": name, "_meta": {"event": {"timestamp": timestamp}}}

    controller.state.sequences.extend((
        SequenceRows({}, (row("2026-01-01T00:00:01Z", "a1"),
                          row("2026-01-01T00:00:03Z", "a2")), 0),
        SequenceRows({}, (row("2026-01-01T00:00:02Z", "b1"),), 0),
    ))
    assert [item["name"] for item in controller._ordered_stream_rows()] == ["a1", "b1", "a2"]


async def test_exhaustion_consumes_exact_bound_without_commit() -> None:
    """head 恰消费 max_slot_attempts，零正式 commit。"""
    controller, _metrics, _dedup = _controller(count=1, capacity=1, attempts=3)

    async def capture(_self, _phase, slot, _ordinal, attempt_index):
        error = GenerationAttemptRejected("quality", slot.slot_key)
        return _AttemptOutcome(attempt_index, None, None, error)

    controller._capture_attempt = MethodType(capture, controller)
    with pytest.raises(DeliveryError) as caught:
        await controller._deliver_slot_phases()
    assert caught.value.attempts_used == 3
    assert controller.state.sequence_attempts == 3
    assert controller.state.rejected["quality"] == 3
    assert controller.state.sequences == []


async def test_noise_exhaustion_consumes_exact_bound_without_commit() -> None:
    """noise head 恰消费上限且不提交 similarity、row 或 retained state。"""
    controller, _metrics, _dedup = _controller(count=0, capacity=1, attempts=3)
    slot = NoiseSlot("event", 0, "frame", "topic", 1000, 0, (), "session")
    controller.request.plan.noise_slots = (slot,)

    async def capture(_self, _phase, _slot_value, _ordinal, attempt_index):
        error = GenerationAttemptRejected("noise_semantic", "noise/000000")
        return _AttemptOutcome(attempt_index, None, None, error)

    controller._capture_attempt = MethodType(capture, controller)
    with pytest.raises(DeliveryError) as caught:
        await controller._deliver_slot_phases()
    assert caught.value.attempts_used == 3
    assert controller.state.noise_attempts == 3
    assert controller.state.rejected["noise_semantic"] == 3
    assert controller.state.noise_rows == []
    assert controller.state.noise_payload_digests == []
    assert controller.state.retained_bytes == 0


def test_usage_and_terminal_kinds_are_secret_safe() -> None:
    """usage 只投影冻结统计，terminal kind 不包含异常正文。"""
    controller, _metrics, _dedup = _controller(count=0)
    usage = SimpleNamespace(
        calls=2, retries=1, prompt_tokens=10, completion_tokens=5,
        est_cost_usd=0.25, keys={}, parked_calls=0, parked_ms=0,
    )
    controller.generation.llm.usage_by_profile = {"model": usage}
    report = controller._usage_report()
    assert report == {
        "model": {
            "calls": 2, "prompt_tokens": 10, "completion_tokens": 5,
            "retries": 1, "est_cost_usd": 0.25,
        }
    }
    secret = "provider-sensitive-body"
    assert controller._terminal_kind(ProviderFatalError(secret, "model")) == "provider_fatal"
    controller.write_failed_report(ProviderFatalError(secret, "model"))
    assert secret not in repr(controller.services.emitter.failed[-1])


def test_delivery_invariant_errors_fail_closed_before_mutation() -> None:
    """cardinality、noise、counter 与 rejection kind 异常均不改变正式状态。"""
    from labelkit.orchestration import sequence_workflow as workflow

    controller, metrics, dedup = _controller(count=1)
    slot = controller.request.plan.delivery_slots[0]
    with pytest.raises(InternalError):
        controller._project_traces(slot, ())
    with pytest.raises(InternalError):
        controller._transaction(slot, ())
    controller.request.program.noise = None
    with pytest.raises(InternalError):
        controller._noise_render_request(NoiseSlot("event", 0, "missing", "topic", 1000, 0, (), "s"), 0)
    with pytest.raises(InternalError):
        controller._reject("not-a-bucket")
    with pytest.raises(InternalError):
        controller._merge_local({}, {"generated": True})
    assert controller._generation_rejection_kind("sequence_projection_mismatch", "primary") == "reconcile"
    with pytest.raises(InternalError):
        controller._generation_rejection_kind("noise_schema", "primary")
    with pytest.raises(InternalError):
        controller._generation_rejection_kind("quality", "noise")
    with pytest.raises(InternalError):
        workflow._plan_events(SimpleNamespace(blocks=()), "missing", None)
    assert workflow._planning_terminal_kind(ValueError("secret")) == "generation_plan_internal"
    assert metrics.merged == []
    assert dedup.commits == []
    assert controller.state.sequences == controller.state.noise_rows == []


def test_noise_filter_uses_resolved_dedup_parameters() -> None:
    """noise phase filter 直接消费 frozen dedup threshold/num_perm/ngram。"""
    controller, _metrics, _dedup = _controller(count=0)
    cfg = controller.generation.config.dedup
    cfg.minhash_threshold = 0.73
    cfg.minhash_num_perm = 32
    cfg.ngram = 7
    similarity = controller._initial_similarity_filter()
    assert similarity._threshold == 0.73
    assert similarity._num_perm == 32
    assert similarity._ngram == 7


def test_noise_requests_share_exact_planned_topic_and_profiles() -> None:
    """noise render/evaluation 请求共享计划 topic 且 profile 分离。"""
    controller, _metrics, _dedup = _controller(count=0)
    slot = NoiseSlot("event", 0, "frame", "planned-topic", 1000, 0, (), "session")
    controller.request.program.semantic_profile = "semantic"
    controller.request.program.evaluation_profile = "evaluation"
    controller.request.program.noise = SimpleNamespace(instruction="noise", topics=("planned-topic",))
    controller.request.program.frame_classes = {
        "frame": SimpleNamespace(description="frame description")
    }
    render = controller._noise_render_request(slot, 2)
    evaluation = controller._noise_evaluation_request({"text": "noise"}, slot, 2)
    assert render.semantic_profile == "semantic"
    assert render.utc_offset_minutes == 480
    assert render.noise_slot.topic == "planned-topic"
    assert evaluation.evaluation_profile == "evaluation"
    assert evaluation.planned_topic == "planned-topic"


def test_rejected_noise_similarity_does_not_mutate_state_or_filter() -> None:
    """相似度拒绝不提交 signature、row、payload digest 或 retained bytes。"""
    controller, _metrics, _dedup = _controller(count=0)
    payload = {"text": "same primary payload"}
    controller.state.sequences.append(SequenceRows(
        {}, ({"payload": payload},), 0
    ))
    controller._noise_similarity = controller._initial_similarity_filter()
    slot = NoiseSlot("event", 0, "frame", "topic", 1000, 0, (), "session")
    signature = controller._noise_signature(canonical_json(payload))
    candidate = PreparedNoiseCandidate(slot, 1, "digest", {"payload": payload}, signature, {}, 1, "")
    candidate = replace(candidate, digest=_prepared_digest("noise", candidate))
    before = len(controller._noise_similarity._signatures)
    result = controller._commit_noise_head(_AttemptOutcome(0, candidate, None, None))
    assert result == "noise_similarity"
    assert len(controller._noise_similarity._signatures) == before
    assert controller.state.noise_rows == []
    assert controller.state.noise_payload_digests == []
    assert controller.state.retained_bytes == 0


async def test_commit_failure_clears_last_slot_and_preserves_old_manifest(monkeypatch) -> None:
    """finalization I/O 失败不冒充最后成功槽，也不改旧 manifest 真值。"""
    controller, _metrics, _dedup = _controller(count=0)

    class CommitFailEmitter(_Emitter):
        def __init__(self):
            super().__init__()
            self.manifest = {"delivery_digest": "old"}

        def prepare_product(self, main_rows, stream_rows, report):
            return SimpleNamespace(main_rows=tuple(main_rows), stream_rows=tuple(stream_rows), report=report)

        def commit(self, _product):
            raise LabelKitError("generation_commit_io")

    emitter = CommitFailEmitter()
    controller.services.emitter = emitter
    from labelkit.operators.generation import project

    monkeypatch.setattr(project, "validate_plan_identity", lambda *_args: None)

    async def phases(self):
        self.state.failed_slot = "set/000000"
        self.state.attempts_used = 1
        self.state.sequence_attempts = 1

    monkeypatch.setattr(_DeliveryController, "_deliver_slot_phases", phases)
    monkeypatch.setattr(_DeliveryController, "_final_reconcile", lambda _self: None)
    with pytest.raises(LabelKitError, match="generation_commit_io"):
        await deliver_generation(controller.request, controller.services)
    assert emitter.manifest == {"delivery_digest": "old"}
    failed = emitter.failed[-1]
    assert failed["failed_slot"] is None
    assert failed["attempts_used"] == 0
    assert failed["terminal_error_kind"] == "generation_commit_io"


@pytest.mark.parametrize("failure,attempts_used", [
    (DeliveryError("sequence_delivery_exhausted", "set/000000", 2), 2),
    (PostprocessorError(), 0),
])
async def test_public_terminal_failure_preserves_success_artifacts_and_writes_only_failure(
    monkeypatch, tmp_path, failure, attempts_used,
) -> None:
    """耗尽或后处理内部错误只原子替换独立 failed report，保留成功工件。"""
    from labelkit.operators.emitter import SequenceDeliveryEmitter

    controller, _metrics, _dedup = _controller(count=1, attempts=2)
    paths = controller.request.paths
    paths.output = str(tmp_path / "main.jsonl")
    paths.stream = str(tmp_path / "stream.jsonl")
    paths.report = str(tmp_path / "report.json")
    paths.manifest = str(tmp_path / "manifest.json")
    paths.failed_report = str(tmp_path / "failed.report.json")
    success_paths = tuple(Path(value) for value in (
        paths.output, paths.stream, paths.report, paths.manifest,
    ))
    for index, path in enumerate(success_paths):
        path.write_text(f"old-{index}", encoding="utf-8")
    controller.services.emitter = SequenceDeliveryEmitter(paths)
    async def exhaust(self):
        self.generation.llm.usage_by_profile["model"] = ProfileUsage(
            calls=2, prompt_tokens=17, completion_tokens=9, retries=1, est_cost_usd=0.25,
        )
        self.state.failed_slot = "set/000000"
        self.state.attempts_used = attempts_used
        self.state.sequence_attempts = attempts_used
        self.state.rejected["quality"] = attempts_used
        raise failure

    monkeypatch.setattr(_DeliveryController, "deliver", exhaust)
    with pytest.raises(type(failure)) as caught:
        await deliver_generation(controller.request, controller.services)
    assert caught.value is failure
    assert [path.read_text(encoding="utf-8") for path in success_paths] == [
        f"old-{index}" for index in range(4)
    ]
    failed = json.loads(Path(paths.failed_report).read_text(encoding="utf-8"))
    assert failed["failed_slot"] == "set/000000"
    assert failed["attempts_used"] == attempts_used
    assert failed["rejected_attempts"]["quality"] == attempts_used
    assert failed["llm_usage"] == {
        "model": {"calls": 2, "prompt_tokens": 17, "completion_tokens": 9, "retries": 1, "est_cost_usd": 0.25},
    }
    assert failed["runtime"] == {key: 0 for key in _RUNTIME_KEYS}


def _load_example_config(monkeypatch, tmp_path: Path, *, dry_run: bool = False):
    """用真实 M1 路径裁决装载临时 sequence 输出位置。"""
    monkeypatch.setenv("LABELKIT_DEEPSEEK_KEY", "offline-test-key")
    return load(
        _EXAMPLE / "config.toml",
        _EXAMPLE / "project.toml",
        CliOverrides(output=str(tmp_path / "labels.jsonl"), dry_run=dry_run),
    )


def _fixed_sequence_paths(cfg) -> tuple[Path, ...]:
    """返回 sequence 五个禁止被 dry-run 或 plan failure 覆盖的路径。"""
    return tuple(Path(value) for value in (
        cfg.paths.output, cfg.paths.stream, cfg.paths.report,
        cfg.paths.manifest, cfg.paths.failed_report,
    ))


def test_sequence_dry_run_preserves_every_fixed_output_sentinel(monkeypatch, tmp_path) -> None:
    """成功规划的 dry-run 只估算，不覆盖五个固定工件。"""
    cfg = _load_example_config(monkeypatch, tmp_path, dry_run=True)
    sentinels = _fixed_sequence_paths(cfg)
    for index, path in enumerate(sentinels):
        path.write_bytes(f"sentinel-{index}".encode())
    monkeypatch.setattr(application, "load", lambda *_args, **_kwargs: cfg)
    assert application.execute_run("config.toml", "project.toml", CliOverrides()) == 0
    assert [path.read_bytes() for path in sentinels] == [
        f"sentinel-{index}".encode() for index in range(5)
    ]


@pytest.mark.parametrize(
    ("cause", "terminal_kind"),
    (
        (ConfigError(["generation_plan_infeasible: witness"]), "generation_plan_infeasible"),
        (InternalError("generation_plan_budget: witness"), "generation_plan_budget"),
        (InternalError("generation_plan_internal: witness"), "generation_plan_internal"),
    ),
)
def test_live_plan_failure_precedes_secrets_and_writes_runtime_report(
    monkeypatch, tmp_path, cause, terminal_kind,
) -> None:
    """live planner terminal 先于凭据/日志对象，仅替换独立 failed report。"""
    cfg = _load_example_config(monkeypatch, tmp_path)
    fixed = _fixed_sequence_paths(cfg)
    for index, path in enumerate(fixed[:-1]):
        path.write_bytes(f"old-{index}".encode())
    monkeypatch.setattr(application, "load", lambda *_args, **_kwargs: cfg)
    from labelkit.operators.generation import planner

    monkeypatch.setattr(
        planner, "compile_scenario_plan", lambda _program: (_ for _ in ()).throw(cause)
    )
    touched: list[str] = []
    monkeypatch.setattr(application, "setup_logging", lambda _cfg: touched.append("logging"))
    monkeypatch.setattr(application, "_run_credentials", lambda _cfg: touched.append("credentials"))
    monkeypatch.setattr(application, "EventLog", lambda *_args: touched.append("eventlog"))
    with pytest.raises(type(cause)) as caught:
        application.execute_run("config.toml", "project.toml", CliOverrides())
    assert caught.value is cause
    assert touched == []
    assert [path.read_bytes() for path in fixed[:-1]] == [
        f"old-{index}".encode() for index in range(4)
    ]
    failed = json.loads(fixed[-1].read_text(encoding="utf-8"))
    assert failed["run_id"] is None
    assert failed["failed_slot"] is None
    assert failed["attempts_used"] == 0
    assert failed["terminal_error_kind"] == terminal_kind
    assert failed["runtime"] == {key: 0 for key in _RUNTIME_KEYS}
    assert sum(failed["rejected_attempts"].values()) == 0


@pytest.mark.parametrize("entrypoint", ("validate", "dry-run"))
def test_non_live_plan_failure_reads_no_secret_and_writes_no_failed_report(
    monkeypatch, tmp_path, entrypoint,
) -> None:
    """validate 与 dry-run 共享 planner error，但绝不产生 failed report。"""
    cfg = _load_example_config(monkeypatch, tmp_path, dry_run=entrypoint == "dry-run")
    monkeypatch.setattr(application, "load", lambda *_args, **_kwargs: cfg)
    from labelkit.operators.generation import planner

    cause = ConfigError(["generation_plan_infeasible: witness"])
    monkeypatch.setattr(
        planner, "compile_scenario_plan", lambda _program: (_ for _ in ()).throw(cause)
    )
    monkeypatch.setattr(
        application, "_run_credentials",
        lambda _cfg: (_ for _ in ()).throw(AssertionError("secret read")),
    )
    with pytest.raises(ConfigError) as caught:
        if entrypoint == "validate":
            application.validate_project("config.toml", "project.toml")
        else:
            application.execute_run("config.toml", "project.toml", CliOverrides())
    assert caught.value is cause
    assert not Path(cfg.paths.failed_report).exists()


def _plan_witness(program, plan) -> tuple:
    """提取 digest、slots 与计划事件时间 witness。"""
    blocks = tuple(
        (key, tuple((event.role, event.logical_time_us, event.timestamp_us, event.session_id)
                    for event in events))
        for block in plan.blocks for key, events in block.items()
    )
    slots = tuple(
        (slot.slot_key, slot.variant_names, slot.catalog_row_index)
        for slot in plan.delivery_slots
    )
    return program.digest, plan.digest, slots, blocks


class _PlanWorkflow:
    """只记录 Application 绑定 program/plan 的零网络 workflow。"""

    bound: list[tuple] = []

    def __init__(self, _cfg, _stages, _ingestor, _emitter, _services):
        pass

    def _bind_sequence_plan(self, program, plan) -> None:
        """记录 Application 传入的同一冻结产品。"""
        self.bound.append((program, plan))

    async def run(self):
        """返回最小成功运行终态。"""
        return SimpleNamespace(exit_code=0)


def test_validate_dry_run_and_live_bind_identical_plan_witness(monkeypatch, tmp_path) -> None:
    """三条命令路径调用同一 compiler/planner 并冻结相同 witness。"""
    live = _load_example_config(monkeypatch, tmp_path)
    dry = replace(live, dry_run=True)
    configs = iter((live, dry, live))
    monkeypatch.setattr(application, "load", lambda *_args, **_kwargs: next(configs))
    original = application._compile_sequence_plan
    witnesses: list[tuple] = []

    def compile_and_record(cfg):
        result = original(cfg)
        assert result is not None
        witnesses.append(_plan_witness(*result))
        return result

    monkeypatch.setattr(application, "_compile_sequence_plan", compile_and_record)
    monkeypatch.setattr(application, "ProcessWorkflow", _PlanWorkflow)
    monkeypatch.setattr(application, "build_stages", lambda _cfg: [])
    monkeypatch.setattr(
        application, "_run_credentials", lambda _cfg: SimpleNamespace(llm={}, embedding={})
    )
    _PlanWorkflow.bound.clear()
    application.validate_project("config.toml", "project.toml")
    assert application.execute_run("config.toml", "project.toml", CliOverrides()) == 0
    assert application.execute_run("config.toml", "project.toml", CliOverrides()) == 0
    assert witnesses[0] == witnesses[1] == witnesses[2]
    assert [_plan_witness(*pair) for pair in _PlanWorkflow.bound] == witnesses[1:]

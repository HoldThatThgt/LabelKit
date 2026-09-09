"""容量边界与真实 SchemaEngine 的离线组合契约；不创建服务或网络 transport。"""
from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from types import SimpleNamespace

import pytest

import test_segment as segment_tests
import test_stitch as stitch_tests
import test_verify as verify_tests
from labelkit.common.config.model import OutputConfig
from labelkit.common.contracts.types import SequenceBounds, Usage
from labelkit.common.errors import ContextOverflowError, SessionCapacityError
from labelkit.common.inference import budget
from labelkit.common.inference.schema_engine import SchemaEngine
from labelkit.common.inference.sequence_evidence import request_overflow
from labelkit.operators.annotate import AnnotatePromptOptions, AnnotateStage, class_schema_text
from labelkit.operators.annotate_capacity import sequence_request
from labelkit.operators.segment import SegmentStage, _call_window
from labelkit.operators.segment_capacity import episode_envelope
from labelkit.operators.stitch import StitchStage, judge_stitch
from labelkit.operators.stream_verify import StreamVerifyDriver
from labelkit.operators.verify import VerifyStage, _EpisodeReview
from labelkit.orchestration.session_workflow import SessionCapacityChecker


MODEL_SCHEMA = {
    "type": "object", "properties": {"proof": {"type": "string"}},
    "required": ["proof"], "additionalProperties": False,
}
IMAGE_COST = 73


def _profiles(cfg):
    """为实际成员请求和 Schema 修复提供明确且可装的部署工作点。"""
    profile = replace(segment_tests.llm_profile(context_window=1000000),
                      supports_structured_output=True, supports_vision=True)
    return {name: replace(profile, name=name) for name in ("default", "judge", "repair-profile")}


def _complete_cfg(cfg):
    return replace(cfg, llm_profiles=_profiles(cfg), user_schema=MODEL_SCHEMA, model_user_schema=MODEL_SCHEMA,
                   annotate=replace(cfg.annotate, enabled=True),
                   output=OutputConfig(max_repair_attempts=3, repair_llm="repair-profile"))


def _exact_annotation_budget(cfg, frames, ctx):
    """用真实标注请求、Schema 和图片估算选择恰好等于输入预算的窗口。"""
    ctx.cfg = cfg
    ctx.llm = SimpleNamespace(calibrator=SimpleNamespace(cost=lambda profile: IMAGE_COST))
    ctx.schema_engine.user_schema_text = SchemaEngine(MODEL_SCHEMA, llm=None, cfg=cfg.output).user_schema_text
    item = episode_envelope("s1", frames, SequenceBounds(0, 10000))
    request = sequence_request(item.record, ctx, class_schema_text(ctx, None), AnnotatePromptOptions())
    profile = cfg.llm_profiles[request.profile]
    estimate = budget.est_prompt(request.prompt, profile, request.schema, image_cost=IMAGE_COST)
    window = next(window for window in range(estimate + profile.max_output_tokens,
                                              2 * (estimate + profile.max_output_tokens) + 1000)
                  if budget.input_budget(replace(profile, context_window=window)) == estimate)
    profiles = {**cfg.llm_profiles, request.profile: replace(profile, context_window=window)}
    cfg = replace(cfg, llm_profiles=profiles)
    ctx.cfg = cfg
    ctx.capacity_checker = SessionCapacityChecker((AnnotateStage(cfg),))
    assert request_overflow(request, ctx) is None
    assert estimate == budget.input_budget(profiles[request.profile])
    return cfg, item


@pytest.mark.parametrize("strategy", ["rules", "hybrid"])
async def test_rules_and_keep_paths_partition_full_evidence_after_semantic_fate(strategy):
    cfg = _complete_cfg(segment_tests.make_cfg(strategy=strategy, on_error="keep", min_len=9))
    frames = [segment_tests.envelope(segment_tests.ui_frame(f"f{index}", index), sid="s1")
              for index in range(5)]
    failure = ValueError("ordinary segmentation result failure")
    engine = segment_tests.QueueEngine([failure]) if strategy == "hybrid" else segment_tests.ExplodingEngine()
    ctx = segment_tests.make_ctx(cfg, engine)
    cfg, _ = _exact_annotation_budget(cfg, frames[:2], ctx)
    # segment 继续使用足够大的独立 profile；容量约束只来自下游实际标注请求。
    cfg = replace(cfg, segment=replace(cfg.segment, llm="judge"))
    ctx.cfg = cfg
    batch = list(frames)
    await SegmentStage(cfg).run(batch, ctx)
    episodes = batch[len(frames):]
    assert [item.member_positions for item in episodes] == [(0, 1), (2, 3), (4,)]
    assert [item.capacity.sealed for item in episodes] == [True, True, False]
    assert all(item.status == "active" and not item.errors for item in episodes)
    assert all(frame.status == "absorbed" for frame in frames)
    assert ctx.metrics.counters["capacity.splits"] == 2
    assert "segment.below_min_len" not in ctx.metrics.counters
    if strategy == "hybrid":
        assert len(engine.calls) == 1
        assert all(item.segment_degraded == {"kind": "segmentation_invalid", "windows_failed": 1}
                   for item in episodes)
        assert ctx.metrics.counters["segment.failures"] == 1
    else:
        assert all(not hasattr(item, "segment_degraded") for item in episodes)


@pytest.mark.parametrize("batch_size", [1, 2, 100])
async def test_group_edges_share_noise_and_short_segment_decisions_before_final_partition(batch_size):
    cfg = segment_tests.make_cfg(window=2, min_len=2, noise_filter=True)
    cfg = replace(cfg, run=replace(cfg.run, batch_size=batch_size))
    frames = [segment_tests.envelope(segment_tests.ui_frame(f"f{index}", index)) for index in range(7)]
    # 第二窗撤销位置1的旧噪声判断；第三窗确认位置2噪声；位置3在全轮完成后才成为短段。
    relations = [
        ("continues", "interruption"), ("continues", "continues"),
        ("interruption", "context_switch"), ("context_switch", "continues"),
        ("returns_to_entry", "continues"), ("continues", "continues"),
    ]
    engine = segment_tests.MapEngine({f"f{index}": segment_tests.window_obj(*values)
                                      for index, values in enumerate(relations)})
    ctx = segment_tests.make_ctx(cfg, engine)
    ctx.tasks = stitch_tests.TaskRunner()
    batch = list(frames)
    await SegmentStage(cfg).run(batch, ctx)
    assert [item.member_positions for item in batch[7:]] == [(0, 1), (4, 5, 6)]
    assert [frame.status for frame in frames] == [
        "absorbed", "absorbed", "dropped_noise", "dropped_noise", "absorbed", "absorbed", "absorbed"]
    assert frames[2].noise_attribution == ("segment", "noise")
    assert frames[3].noise_attribution == ("segment", "below_min_len")
    assert ctx.metrics.counters["segment.below_min_len"] == 1
    assert [len(request.tasks) for request in ctx.tasks.requests] == (
        [1] * 6 if batch_size == 1 else [2] * 3 if batch_size == 2 else [6])
    expected_ids = [episode_envelope(frames[0].session_id, [frames[index] for index in positions],
                                     SequenceBounds(0, 7)).record.id for positions in ((0, 1), (4, 5, 6))]
    assert [item.record.id for item in batch[7:]] == expected_ids


@pytest.mark.parametrize("entry", ["pass1", "rescue", "repass"])
@pytest.mark.parametrize("extra_member", [False, True])
async def test_each_stitch_entry_prices_exact_complete_request_and_next_member(entry, extra_member):
    cfg = _complete_cfg(stitch_tests.make_cfg(bias="llm", repass=entry == "repass"))
    frames = [stitch_tests.envelope(stitch_tests.ui_frame(f"f{index}", index)) for index in range(4)]
    left = stitch_tests.episode_of(frames[:2])
    candidates = frames[2:4 if extra_member else 3]
    if entry == "rescue":
        stitch_tests.short_run(candidates)
        episodes = [left]
    else:
        episodes = [left, stitch_tests.episode_of(candidates)]
    outcomes = [stitch_tests.obj(), stitch_tests.obj("resume", 1)]
    if entry == "repass":
        outcomes = [stitch_tests.obj(), stitch_tests.obj(), stitch_tests.obj("resume", 1)]
    engine = stitch_tests.QueueEngine(outcomes)
    ctx = stitch_tests.make_ctx(cfg, engine)
    cfg, exact = _exact_annotation_budget(cfg, frames[:3], ctx)
    assert ctx.capacity_checker.preview(exact, ctx) is None
    too_large = episode_envelope("s1", frames, SequenceBounds(0, 10000))
    overflow = ctx.capacity_checker.preview(too_large, ctx)
    assert overflow is not None and overflow.unit == "sequence" and overflow.stage == "annotate"
    batch = [*frames[:4 if extra_member else 3], *episodes]
    await StitchStage(cfg).run(batch, ctx)
    if extra_member:
        assert left.member_positions == (0, 1) and left.capacity.sealed
        assert left.capacity.bounds.upper == 2
        assert ctx.metrics.counters["capacity.sealed"] == 1
        if entry == "rescue":
            assert all(frame.status == "dropped_noise" for frame in candidates)
            assert "stitch.rescued_short" not in ctx.metrics.counters
        else:
            assert episodes[1].member_positions == (2, 3) and episodes[1].status == "active"
            assert episodes[1].capacity.bounds.lower == 2
    else:
        survivor = episodes[-1] if entry == "repass" else left
        assert survivor.member_positions == (0, 1, 2) and not survivor.capacity.sealed
        assert all(frame.status == "absorbed" for frame in batch if frame.record.kind == "single")
        assert "capacity.sealed" not in ctx.metrics.counters
        if entry == "rescue":
            assert ctx.metrics.counters["stitch.rescued_short"] == 1
        else:
            shell = left if entry == "repass" else episodes[-1]
            assert shell.status == "stitched"
    assert len(engine.calls) == (3 if entry == "repass" else 2)


class _RepairOverflowLLM:
    """直接实现进程内 LLM 对象面；真实 SchemaEngine 负责验证、L3 构造及错误传播。"""

    def __init__(self):
        self.requests = []
        self.errors = []
        self.calibrator = SimpleNamespace(cost=lambda profile: IMAGE_COST)

    async def complete(self, profile, prompt, response_schema=None):
        self.requests.append((profile, prompt, response_schema))
        if profile == "repair-profile":
            error = ContextOverflowError("full repair evidence exceeds context", "reactive", profile, "http_400")
            self.errors.append(error)
            raise error
        return SimpleNamespace(text="{}", structured=None, usage=Usage(7, 2), model="logic-unit", latency_ms=1)


def _l3_case():
    cfg = _complete_cfg(verify_tests._frame_stream_cfg())
    cfg = replace(cfg, model_frame_schema=MODEL_SCHEMA, frame_schema=MODEL_SCHEMA)
    records = [segment_tests.ui_frame(f"f{index}", index, texts=("full-visible-" + "x" * 500,
                                                               f"critical-tail-{index}")) for index in range(4)]
    frames = [verify_tests._env(record) for record in records]
    frames[-1].status = "dropped_noise"
    frames[-1].noise_attribution = ("segment", "noise")
    episode = verify_tests._episode(records[:3], transitions=(verify_tests._transition(0),
                                                             verify_tests._transition(1)))
    llm = _RepairOverflowLLM()
    engine = SchemaEngine(MODEL_SCHEMA, llm=llm, cfg=cfg.output)
    ctx = verify_tests._task_context(cfg=cfg, llm=llm, schema_engine=engine)
    ctx.capacity_checker = SessionCapacityChecker((AnnotateStage(cfg),))
    return cfg, [*frames, episode], episode, ctx, llm


def _l3_operation(phase, driver, state, batch, ctx):
    """调用真实阶段或真实 repair 波次，独立叶只在最底层 LLM 对象处返回未通过 Schema 的数据。"""
    if phase == "stitch":
        return judge_stitch(["complete existing task card"], "complete candidate card", ctx, ("f0",), (0, 0, 0))
    if phase == "segment":
        return _call_window(state.working_members, ctx, span=(0, 3))
    if phase == "review":
        return driver._review_round([state], batch, ctx)
    if phase == "claim":
        state.working_members.pop(1)
        state.working_positions.pop(1)
        batch[1].status = "dropped_noise"
        state.claims = [driver._make_claim(state, batch[1], 1)]
        return driver._resolve_claims([state], ctx)
    if phase == "reseam":
        state.working_members.pop(1)
        state.working_positions.pop(1)
        state.surgical = True
        return driver._reseam_episodes([state], ctx)
    if phase == "reannotate":
        state.fail_critiques = [{"aspect": "complete evidence", "opinion": "full-critique-" + "x" * 500}]
        return driver._reannotate_round([state], ctx)
    if phase == "frame_classify":
        state.item.member_classifications = {}
        return driver._backfill_frame_classify([state], ctx)
    state.item.member_annotations = {}
    return driver._backfill_frame_annotate([state], ctx)


@pytest.mark.parametrize("phase", ["stitch", "segment", "review", "claim", "reseam", "reannotate",
                                   "frame_classify", "frame_annotate"])
async def test_every_stream_call_enters_real_l3_with_complete_evidence_and_original_capacity_error(phase):
    cfg, batch, episode, ctx, llm = _l3_case()
    driver = StreamVerifyDriver(VerifyStage(cfg))
    state = _EpisodeReview(episode, 0)
    if phase in ("stitch", "segment"):
        ctx.session_attempt = replace(ctx.session_attempt, stage=phase)
    operation = _l3_operation(phase, driver, state, batch, ctx)
    error_type = ContextOverflowError if phase in ("stitch", "segment") else SessionCapacityError
    with pytest.raises(error_type) as caught:
        await operation
    initial = [request for request in llm.requests if request[0] != "repair-profile"]
    repairs = [request for request in llm.requests if request[0] == "repair-profile"]
    assert len(initial) == len(repairs) == (3 if phase.startswith("frame_") else 1)
    assert ctx.schema_engine.stats["rejected"] == 0
    observed_errors = [caught.value] if error_type is ContextOverflowError else [
        failure.error for failure in caught.value.failures]
    assert observed_errors == llm.errors
    assert all(error.profile == "repair-profile" and error.phase == "reactive" and error.origin == "http_400"
               and not getattr(error, "_breaker_fed", False) for error in observed_errors)
    if error_type is SessionCapacityError:
        assert all(failure.stage == "verify" for failure in caught.value.failures)
    for initial_request, repaired_request in zip(initial, repairs, strict=True):
        _, original, schema = initial_request
        profile, repaired, repaired_schema = repaired_request
        assert profile == "repair-profile" and repaired_schema == schema
        assert repaired.messages[:-2] == original.messages and repaired.image_px == original.image_px
        assert repaired.messages[-2].role == "assistant" and repaired.messages[-1].role == "user"
        assert "[违规清单]" in repaired.messages[-1].parts[0].text
        assert any("required" in part.text for part in repaired.messages[-1].parts if part.kind == "text")
    evidence = [part for _, prompt, _ in initial for message in prompt.messages for part in message.parts]
    images = [part.image for part in evidence if part.kind == "image"]
    expected = [] if phase == "stitch" else [frame.record.image for frame in batch[:4 if phase == "review" else 3]
                                             if phase != "reseam" or frame.session_position != 1]
    assert images == expected
    text = "\n".join(part.text for part in evidence if part.kind == "text")
    for frame in batch[:4]:
        if frame.record.image in expected:
            assert frame.record.ui_tree.serialize(max_chars=None) in text
    if phase == "reannotate":
        assert "full-critique-" + "x" * 500 in text
        assert "task_label" in text
    assert episode.status == "active" and episode.verification is None
    assert "budget.overflow_records" not in ctx.metrics.counters


@pytest.mark.parametrize("batch_size", [1, 3])
@pytest.mark.parametrize("reverse_completion", [False, True])
async def test_real_workflow_freezes_calibration_only_after_session_recomputation(
        tmp_path, batch_size, reverse_completion):
    from labelkit.common.contracts.execution import TaskGroupRequest, TaskSpec
    from labelkit.common.contracts.sequence_capacity import capacity_target, raise_session_capacity
    from labelkit.orchestration.process_workflow import ProcessWorkflow
    from tests.orchestration.test_process_workflow import (
        FakeEmitter, FakeMetrics, FakeSessionIngestor, services, stream_cfg,
    )

    cfg = _complete_cfg(stream_cfg(tmp_path, batch_size=batch_size, annotate=True, dedup=False, modality="ui",
                                   segment=replace(segment_tests.make_cfg().segment, strategy="rules")))
    calibrator = budget.ImageCostCalibrator({name: (profile.provider, 512)
                                           for name, profile in cfg.llm_profiles.items()})
    prior = calibrator.cost("default")
    llm = SimpleNamespace(calibrator=calibrator)
    metrics = FakeMetrics()
    engine = SchemaEngine(MODEL_SCHEMA, llm=None, cfg=cfg.output)
    previews, reads, random_values, completed, groups = [], [], [], [], []

    class CompletionExecutor:
        async def run_group(self, request):
            groups.append(tuple(task.declaration_key[-1] for task in request.tasks))
            order = list(range(len(request.tasks)))
            if reverse_completion:
                order.reverse()
            gates = [asyncio.Event() for _ in order]
            gates[0].set()

            async def execute(index, task):
                position = order.index(index)
                await gates[position].wait()
                try:
                    return await task.operation()
                finally:
                    if position + 1 < len(gates):
                        gates[position + 1].set()

            return tuple(await asyncio.gather(*(execute(index, task) for index, task in enumerate(request.tasks))))

    class CalibrationStage:
        name = "annotate"

        def preview_capacity(self, item, ctx):
            previews.append((ctx.session_attempt.session_id, ctx.session_attempt.attempt,
                             ctx.llm.calibrator.cost("default")))
            return AnnotateStage(cfg).preview_capacity(item, ctx)

        async def run(self, batch, ctx):
            scope = ctx.session_attempt
            random_values.append((scope.session_id, scope.attempt, ctx.rng.random()))
            items = [item for item in batch if item.status == "active"]

            async def observe(index):
                await asyncio.sleep(0)
                cost = ctx.llm.calibrator.cost("default")
                # 直接提供已完成带图响应的统计事实；这里没有模型或 transport。
                residual = (2000 if scope.session_id == "first" else 1000) + index * 10
                ctx.llm.calibrator.observe("default", 123 + residual * 2, 123, 2)
                reads.append((scope.session_id, scope.attempt, cost, ctx.llm.calibrator.cost("default")))
                completed.append((scope.session_id, scope.attempt, index))
                return index

            tasks = tuple(TaskSpec(f"{ctx.task_namespace}:sample:{index}", (ctx.batch_no, index),
                                   self.name, ("llm", "default"), lambda index=index: observe(index))
                          for index in range(8))
            assert await ctx.run_group(TaskGroupRequest(tasks)) == tuple(range(8))
            if scope.session_id == "first" and scope.attempt == 1:
                raise_session_capacity(ctx, (capacity_target(items[0]),),
                                       ContextOverflowError("capacity recomputation", "reactive", "default"),
                                       "sequence")
            ctx.metrics.count("annotate.annotated", len(items))
            return batch

    sessions = [SimpleNamespace(session_id=name, cause="eof", records=tuple(
        segment_tests.ui_frame(f"{name}-{position}", position) for position in range(length)))
        for name, length in (("first", 4), ("second", 2))]
    runtime = replace(services(metrics, llm=llm, schema_engine=engine), tasks=CompletionExecutor())
    emitter = FakeEmitter(cfg)
    driver = ProcessWorkflow(cfg, [SegmentStage(cfg), CalibrationStage()],
                             FakeSessionIngestor(sessions), emitter, runtime)
    summary = await driver.run()
    learned = math.ceil(2070 / budget.CALIBRATION_SAFETY)
    assert learned != prior
    assert {(sid, attempt) for sid, attempt, _, _ in reads} == {("first", 1), ("first", 2), ("second", 1)}
    assert all(before == after == (prior if sid == "first" else learned)
               for sid, _, before, after in reads)
    assert all(cost == (prior if sid == "first" else learned) for sid, _, cost in previews)
    assert random_values[0][2] == random_values[1][2]
    assert calibrator._frozen_total["default"] == 24 and calibrator._current == {}
    assert calibrator.cost("default") == emitter.report["budget"]["image_cost"]["default"] == learned
    assert metrics.counters["capacity.recomputations"] == 1
    assert metrics.counters["annotate.annotated"] == summary.counts["emitted"] == 3
    assert summary.counts["absorbed"] == 6
    assert all(len(group) <= batch_size for group in groups)
    assert len(groups) == (24 if batch_size == 1 else 9)
    first_wave = [index for sid, attempt, index in completed if sid == "first" and attempt == 1]
    assert first_wave == ([2, 1, 0, 5, 4, 3, 7, 6] if reverse_completion and batch_size == 3 else list(range(8)))


async def test_empty_session_iteration_finishes_with_an_empty_delivery(tmp_path):
    from labelkit.orchestration.process_workflow import ProcessWorkflow
    from tests.orchestration.test_process_workflow import (
        FakeEmitter, FakeMetrics, FakeSessionIngestor, services, stream_cfg,
    )

    cfg = stream_cfg(tmp_path, dedup=False, segment=replace(segment_tests.make_cfg().segment, strategy="rules"))
    metrics, ingestor, emitter = FakeMetrics(), FakeSessionIngestor([]), FakeEmitter(cfg)
    engine = SchemaEngine(cfg.user_schema, llm=None, cfg=cfg.output)
    driver = ProcessWorkflow(cfg, [SegmentStage(cfg)], ingestor, emitter, services(metrics, schema_engine=engine))
    summary = await driver.run()
    assert summary.exit_code == 0 and not summary.interrupted
    assert summary.counts["scanned"] == summary.counts["episodes"] == summary.counts["emitted"] == 0
    assert emitter.report["stream"]["sessions"] == 0 and emitter.batches == []
    assert emitter.output.exists() and emitter.output.read_text() == ""
    assert not metrics.stage_begins


async def test_second_session_cancellation_preserves_first_real_dedup_and_emitter_commit(tmp_path):
    import json
    from datetime import datetime, timezone
    from labelkit.common.config.model import ResolvedPaths
    from labelkit.operators.dedup import DedupIndex, DedupStage
    from labelkit.operators.emitter import Emitter
    from labelkit.orchestration.process_workflow import ProcessWorkflow
    from tests.orchestration.test_process_workflow import FakeMetrics, FakeSessionIngestor, services, sess, stream_cfg

    cfg = _complete_cfg(stream_cfg(tmp_path, annotate=True, batch_size=1,
                                   segment=replace(segment_tests.make_cfg().segment, strategy="rules")))
    output = cfg.run.output
    cfg = replace(cfg, output=replace(cfg.output, rejects="none"), paths=ResolvedPaths(
        str(tmp_path / "project.toml"), str(tmp_path), str(tmp_path / "input.jsonl"), output,
        str(tmp_path / "report.json"), None, None, None, None, None, None))
    metrics = FakeMetrics()
    engine = SchemaEngine(MODEL_SCHEMA, llm=None, cfg=cfg.output)
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    emitter = Emitter(cfg, engine, "123456abcdef", started)
    dedup = DedupStage(cfg.dedup, DedupIndex(cfg.dedup, "text"))
    identities, observed_prefixes = [], []

    class CancelSecondSession:
        name = "annotate"

        def preview_capacity(self, item, ctx):
            return None

        async def run(self, batch, ctx):
            item, = [item for item in batch if item.status == "active"]
            identities.append(item.record.id)
            observed_prefixes.append(set(dedup.index._digest_by_id))
            item.annotation = verify_tests._annotation({"proof": ctx.session_attempt.session_id})
            ctx.metrics.count("annotate.annotated")
            if ctx.session_attempt.session_id == "second":
                driver._request_stop()
                driver._current_task.cancel()
                await asyncio.sleep(0)
                raise AssertionError("cancellation must interrupt the uncommitted session")
            return batch

    runtime = replace(services(metrics, schema_engine=engine), run_id="123456abcdef", run_started_at=started)
    ingestor = FakeSessionIngestor([sess("first", 1, 2), sess("second", 11, 3), sess("buffered", 21, 4)])
    driver = ProcessWorkflow(cfg, [SegmentStage(cfg), dedup, CancelSecondSession()], ingestor, emitter, runtime)
    summary = await driver.run()
    rows = [json.loads(line) for line in (tmp_path / "out.jsonl").read_text().splitlines()]
    report = json.loads((tmp_path / "report.json").read_text())
    assert summary.interrupted and summary.exit_code == 0
    assert len(rows) == 1 and rows[0]["proof"] == "first"
    assert rows[0]["_meta"]["stream"]["member_positions"] == [0, 1]
    assert len(identities) == 2 and observed_prefixes == [set(), {identities[0]}]
    assert set(dedup.index._digest_by_id) == {identities[0]} and identities[1] not in dedup.index._digest_by_id
    assert metrics.counters["annotate.annotated"] == summary.counts["emitted"] == summary.counts["episodes"] == 1
    # 既有 for-session 迭代在停止检查前读出下一会话；全部已摄取而未提交帧进入残差。
    assert summary.counts["scanned"] == 9 and summary.counts["absorbed"] == 2
    assert summary.counts["unprocessed"] == 7
    assert report["run"]["interrupted"] is True and report["counts"] == summary.counts

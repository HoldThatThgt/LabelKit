"""Finite session coordination tested with real dedup and pure stage reducers, no model substitutes."""
from __future__ import annotations

import asyncio
import gc
import json
import weakref
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from labelkit.common.contracts.execution import TaskGroupRequest, TaskSpec
from labelkit.common.contracts.sequence_capacity import (
    CapacityTarget, SessionAttemptScope, SessionCapacityFailure, capacity_target,
    process_sequence_id, raise_session_capacities, raise_session_capacity, terminal_capacity_failure,
)
from labelkit.common.contracts.types import Classification, PipelineItem, SequenceBounds, SequenceCapacity
from labelkit.common.errors import ContextOverflowError, InternalError, ProviderFatalError
from labelkit.operators.dedup import DedupIndex, DedupStage
from labelkit.orchestration.process_workflow import ProcessWorkflow
from labelkit.orchestration.session_capacity import SessionPartition, clone_item, project_terminals, validate_session
from labelkit.orchestration.session_workflow import SessionCapacityChecker, SessionWorkflow, _project_failure_targets
from tests.orchestration.test_process_workflow import (
    FakeEmitter, FakeMetrics, FakeSessionIngestor, rec, services, sess, stream_cfg, stream_counts_invariant,
)


async def test_real_ingest_segment_dedup_emitter_preserve_full_session_across_compute_sizes(tmp_path):
    from labelkit.common.config.model import AnnotateConfig, ResolvedPaths, SegmentConfig
    from labelkit.common.inference.schema_engine import SchemaEngine
    from labelkit.common.observability.obslog import EventLog, MetricsSink
    from labelkit.operators.emitter import Emitter
    from labelkit.operators.ingest import Ingestor
    from labelkit.orchestration.factory import build_stages

    source = tmp_path / "input.jsonl"
    source.write_text(''.join(json.dumps({"text": "same complete frame"}) + '\n' for _ in range(6)))
    products = []
    for size in (1, 3):
        cfg = stream_cfg(tmp_path, batch_size=size, segment=SegmentConfig(enabled=True, strategy="rules"))
        output = str(tmp_path / f"result-{size}.jsonl")
        paths = ResolvedPaths(str(tmp_path / "project.toml"), str(tmp_path), str(source), output,
                              str(tmp_path / f"report-{size}.json"), None, None, None, None, None, None)
        cfg = replace(cfg, paths=paths, run=replace(cfg.run, input=str(source), output=output),
                      annotate=AnnotateConfig(enabled=False), output=replace(cfg.output, rejects="none"))
        metrics = MetricsSink(cfg, "123456abcdef", EventLog(cfg.trace, "123456abcdef"))
        engine = SchemaEngine(cfg.user_schema, None, cfg.output, metrics)
        started = datetime(2026, 1, 1, tzinfo=timezone.utc)
        emitter = Emitter(cfg, engine, "123456abcdef", started)
        services_value = replace(services(metrics, schema_engine=engine), run_id="123456abcdef", run_started_at=started)
        driver = ProcessWorkflow(cfg, build_stages(cfg), Ingestor(cfg), emitter, services_value)
        summary = await driver.run()
        rows = [json.loads(line) for line in (tmp_path / f"result-{size}.jsonl").read_text().splitlines()]
        assert summary.counts["episodes"] == summary.counts["emitted"] == 1
        assert summary.counts["absorbed"] == 6 and summary.counts["failed"] == 0
        assert len(rows) == 1
        assert rows[0]["_meta"]["stream"]["member_positions"] == list(range(6))
        assert len(set(rows[0]["_meta"]["stream"]["member_ids"])) == 1
        products.append(rows)
    assert products[0] == products[1]


class PartitionStage:
    name = "segment"

    def __init__(self, sizes=()):
        self.sizes = sizes
        self.calls = 0

    async def run(self, batch, ctx):
        self.calls += 1
        frames = tuple(batch)
        sizes = self.sizes or (len(frames),)
        cursor = 0
        for size in sizes:
            chosen = frames[cursor:cursor + size]
            cursor += size
            positions = tuple(item.session_position for item in chosen)
            members = tuple(item.record for item in chosen)
            identity = process_sequence_id(chosen[0].session_id, positions, tuple(member.id for member in members))
            sequence = replace(members[0], id=identity, kind="sequence", members=members)
            batch.append(PipelineItem(sequence, session_id=chosen[0].session_id, member_positions=positions))
            for item in chosen:
                item.status = "absorbed"
        return batch


class CapacityStage:
    name = "annotate"

    def __init__(self, limit, unit="sequence"):
        self.limit = limit
        self.unit = unit
        self.seen = []
        self.random_values = []
        self.envelopes = []

    def preview_capacity(self, item, ctx):
        return None

    async def run(self, batch, ctx):
        self.random_values.append(ctx.rng.random())
        self.envelopes.append(tuple(batch))
        for item in batch:
            if item.status != "active":
                continue
            self.seen.append((ctx.session_attempt.attempt, item.record.id, item.member_positions))
            ctx.metrics.count("annotate.annotated")
            ctx.metrics.count("llm.capacity_test_calls")
            if len(item.record.members) > self.limit:
                error = ContextOverflowError("complete evidence exceeds context", phase="reactive", profile="default")
                raise_session_capacity(ctx, (capacity_target(item),), error, self.unit)
        return batch


class CapturingEmitter(FakeEmitter):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.products = []

    def emit_batch(self, batch, batch_no):
        self.products.append(tuple(batch))
        return super().emit_batch(batch, batch_no)


def workflow(tmp_path, stages, length=8, batch_size=2):
    cfg = stream_cfg(tmp_path, batch_size=batch_size, annotate=True)
    cfg = replace(cfg, dedup=replace(cfg.dedup, minhash_threshold=0.95))
    index = DedupIndex(cfg.dedup, "text")
    dedup = DedupStage(cfg.dedup, index)
    metrics = FakeMetrics()
    emitter = CapturingEmitter(cfg)
    ingestor = FakeSessionIngestor([sess("source", 1, length)])
    result = ProcessWorkflow(cfg, [*stages, dedup], ingestor, emitter, services(metrics))
    return result, dedup, metrics, emitter


async def test_complete_session_runs_upstream_once_and_ignores_physical_group_size(tmp_path):
    segment = PartitionStage()
    annotate = CapacityStage(100)
    driver, dedup, metrics, emitter = workflow(tmp_path, [segment, annotate])
    summary = await driver.run()
    assert segment.calls == 1
    assert len(emitter.products) == 1
    sequence = [item for item in emitter.products[0] if item.record.kind == "sequence"][0]
    assert sequence.member_positions == tuple(range(8))
    assert len(sequence.record.members) > driver.cfg.run.batch_size
    assert set(dedup.index._digest_by_id) == {sequence.record.id}
    assert summary.counts["episodes"] == 1
    assert summary.counts["absorbed"] == 8
    assert stream_counts_invariant(summary.counts)
    assert metrics.counters["capacity.retained_frames_high_water"] == 8


async def test_reactive_splits_rebuild_whole_downstream_without_leaking_dedup_or_counts(tmp_path):
    segment = PartitionStage()
    annotate = CapacityStage(2)
    driver, dedup, metrics, emitter = workflow(tmp_path, [segment, annotate])
    summary = await driver.run()
    assert segment.calls == 1
    assert len(emitter.products) == 1
    sequences = [item for item in emitter.products[0] if item.record.kind == "sequence"]
    assert [item.member_positions for item in sequences] == [(0, 1), (2, 3), (4, 5), (6, 7)]
    assert all(item.capacity.sealed for item in sequences)
    assert set(dedup.index._digest_by_id) == {item.record.id for item in sequences}
    assert metrics.counters["annotate.annotated"] == 4
    assert metrics.counters["llm.capacity_test_calls"] > 4
    assert metrics.counters["capacity.splits"] == 3
    assert metrics.counters["capacity.recomputations"] == 3
    assert metrics.counters["capacity.sealed"] == 6
    assert len(set(annotate.random_values)) == 1
    assert len({id(batch[0]) for batch in annotate.envelopes}) == 4
    assert summary.counts["episodes"] == 4
    assert summary.counts["emitted"] == 4
    assert stream_counts_invariant(summary.counts)
    assert emitter.report["stream"]["capacity"]["splits"] == 3
    first = next(event[-1] for event in metrics.events
                 if event[0] == "sequence.capacity" and event[-1]["action"] == "split")
    assert first["targets"][0]["member_positions"] == tuple(range(8))
    assert first["cut"]["left_position"] == 3 and first["cut"]["right_position"] == 4


async def test_minimum_sequence_failure_is_projected_once_at_owner_gate(tmp_path):
    segment = PartitionStage()
    annotate = CapacityStage(0)
    driver, dedup, metrics, emitter = workflow(tmp_path, [segment, annotate], length=1)
    summary = await driver.run()
    assert len(annotate.seen) == 1
    assert metrics.counters["capacity.minimum_failures"] == 1
    assert metrics.counters["budget.overflow_records"] == 1
    assert summary.counts["failed"] == 1 and summary.counts["emitted"] == 0
    assert len(dedup.index._digest_by_id) == 1
    item = next(item for item in emitter.products[0] if item.record.kind == "sequence")
    assert item.errors[0].stage == "annotate" and item.errors[0].kind == "context_overflow"
    assert stream_counts_invariant(summary.counts)


async def test_fixed_failure_does_not_split_a_large_sequence(tmp_path):
    annotate = CapacityStage(0, unit="fixed")
    driver, _, metrics, _ = workflow(tmp_path, [PartitionStage(), annotate])
    summary = await driver.run()
    assert len(annotate.seen) == 1
    assert metrics.counters.get("capacity.splits", 0) == 0
    assert summary.counts["episodes"] == 1 and summary.counts["failed"] == 1


@pytest.mark.parametrize("error", [ProviderFatalError("denied", "default", 401), asyncio.CancelledError()])
async def test_control_failure_discards_attempt_and_keeps_formal_state(tmp_path, error):
    class ControlStage(CapacityStage):
        async def run(self, batch, ctx):
            ctx.metrics.count("annotate.annotated", 90)
            raise error

    driver, dedup, metrics, emitter = workflow(tmp_path, [PartitionStage(), ControlStage(1)])
    with pytest.raises(type(error)):
        await driver.run()
    assert not dedup.index._exact
    assert "annotate.annotated" not in metrics.counters
    assert "counts.episodes" not in metrics.counters
    assert emitter.products == []


def partition(sizes=(4, 2)):
    frames = [PipelineItem(rec(index + 1), session_id="session", session_position=index, status="absorbed")
              for index in range(sum(sizes))]
    items = list(frames)
    start = 0
    for ordinal, size in enumerate(sizes):
        positions = tuple(range(start, start + size))
        members = tuple(frames[position].record for position in positions)
        items.append(PipelineItem(replace(members[0], id=f"root-{ordinal}", kind="sequence", members=members),
                                  session_id="session", member_positions=positions))
        start += size
    return SessionPartition(items, len(frames))


def overflow(items, unit="sequence", stage="annotate"):
    return SessionCapacityFailure(stage, tuple(capacity_target(item) for item in items), unit,
                                  ContextOverflowError("too long", "reactive", "default"))


def test_pairwise_split_selects_larger_then_earliest_and_preserves_nonempty_members():
    plan = partition((2, 4))
    sequences = [item for item in plan.items if item.record.kind == "sequence"]
    children = plan.split(overflow(sequences, "pairwise", "quality"))
    assert [item.member_positions for item in children] == [(2, 3), (4, 5)]
    assert children[0].capacity.root_id == "root-1"
    assert children[0].capacity.bounds.upper == 4
    assert children[1].capacity.bounds.lower == 4
    tied = partition((4, 4))
    children = tied.split(overflow([item for item in tied.items if item.record.kind == "sequence"], "pairwise"))
    assert children[0].capacity.root_id == "root-0"


def test_child_ids_depend_on_final_members_not_split_tree_and_clones_share_only_evidence():
    first = partition((8,))
    root = first.items[-1]
    root.scores["mutable"] = {"value": []}
    clone = clone_item(root)
    clone.scores["mutable"]["value"].append(1)
    assert root.scores["mutable"]["value"] == []
    assert clone.record is root.record
    left, _ = first.split(overflow([root]))
    final, _ = first.split(overflow([left]))
    second = partition((8,))
    second.split(overflow([second.items[-1]]))
    final2, _ = second.split(overflow([second.items[-2]]))
    assert final.record.id == final2.record.id
    assert final.capacity.parent_id == left.record.id


def test_verify_expansion_failure_splits_frozen_baseline_and_minimum_stays_terminal():
    plan = partition((2,))
    base = plan.items[-1]
    expanded = replace(capacity_target(base), member_positions=(0, 1, 9))
    failure = replace(overflow([base], stage="verify"), targets=(expanded,))
    children = plan.split(failure)
    assert [child.member_positions for child in children] == [(0,), (1,)]
    minimum = replace(failure, targets=(replace(expanded, record_id=children[0].record.id),))
    assert plan.split(minimum) is None


def test_conservation_rejects_cross_cut_claims_and_duplicate_occurrences():
    plan = partition((4,))
    children = plan.split(overflow([plan.items[-1]]))
    validate_session(plan.fresh())
    children[0].capacity = replace(children[0].capacity, bounds=SequenceBounds(0, 1))
    with pytest.raises(InternalError, match="occurrence"):
        validate_session(plan.fresh())


def test_existing_fixed_terminal_projects_to_child_views_without_reopening_requests():
    plan = partition((4,))
    root = plan.items[-1]
    failure = overflow([root], unit="fixed")
    children = plan.split(overflow([root]))
    projected = _project_failure_targets((failure,), children)
    assert projected[0].error is failure.error
    assert [target.record_id for target in projected[0].targets] == [child.record.id for child in children]


def test_interleaved_fragments_and_tuple_seam_owners_project_by_occurrence():
    plan = partition((4,))
    root = plan.items[-1]
    root.thread_id = root.record.id
    root.stitch_task_name = "Frozen task"
    root.stitch_fragments = (
        {"member_positions": [0, 3], "member_count": 2, "order_span": ["old", "old"],
         "source_episode": "first", "cause": "resume"},
        {"member_positions": [1, 2], "member_count": 2, "order_span": ["old", "old"],
         "source_episode": "second", "cause": "merge"},
    )
    root.seam_indexes = (0, 2)
    root.seam_interrupted_by = (("other-a",), ("other-b",))
    left, right = plan.split(overflow([root]))
    assert [f["member_positions"] for f in left.stitch_fragments] == [(0,), (1,)]
    assert [f["member_positions"] for f in right.stitch_fragments] == [(2,), (3,)]
    assert all(f["member_count"] == 1 for child in (left, right) for f in child.stitch_fragments)
    assert left.seam_indexes == right.seam_indexes == (0,)
    assert left.seam_interrupted_by == (("other-a",),)
    assert right.seam_interrupted_by == (("other-b",),)
    assert left.thread_id == left.record.id != root.record.id
    assert left.stitch_task_name == right.stitch_task_name == "Frozen task"


def test_capacity_child_omits_fragments_with_no_remaining_occurrences():
    plan = partition((4,))
    root = plan.items[-1]
    root.stitch_fragments = tuple(
        {"member_positions": positions, "member_count": 2, "order_span": ["old", "old"],
         "source_episode": name, "cause": "resume"}
        for positions, name in (((0, 1), "left"), ((2, 3), "right")))
    root.seam_indexes = (1,)
    root.seam_interrupted_by = (("other",),)
    left, right = plan.split(overflow([root]))
    assert [fragment["source_episode"] for fragment in left.stitch_fragments] == ["left"]
    assert [fragment["source_episode"] for fragment in right.stitch_fragments] == ["right"]
    assert left.seam_indexes == right.seam_indexes == ()


def test_capacity_checker_sets_owner_scope_for_each_pure_preview(tmp_path):
    seen = []

    class Preview:
        name = "quality"

        def preview_capacity(self, item, ctx):
            seen.append(ctx.session_attempt.stage)
            return overflow([item], stage=ctx.session_attempt.stage)

    driver, _, _, _ = workflow(tmp_path, [])
    ctx = driver._make_ctx(1, "segment")
    ctx.session_attempt = SessionAttemptScope("session", 1, 0, "segment")
    result = SessionCapacityChecker([Preview()]).preview(partition((2,)).items[-1], ctx)
    assert seen == ["quality"] and result.stage == "quality"
    assert ctx.session_attempt.stage == "segment"


def test_real_multilabel_fanout_and_verify_shrink_preserve_single_member_owner(tmp_path):
    from labelkit.operators.classify import ClassifyStage
    from labelkit.operators.stream_verify import StreamVerifyDriver
    from labelkit.operators.verify import VerifyStage, _EpisodeReview

    plan = partition((3,))
    batch = plan.fresh()
    owner = batch[-1]
    owner.classification = Classification("first", ("first", "second"), "inherited", {})
    ClassifyStage._fan_out(batch, [owner])
    sibling = batch[-1]
    driver, _, _, _ = workflow(tmp_path, [])
    verifier = StreamVerifyDriver(VerifyStage(driver.cfg))
    state = _EpisodeReview(owner, 0)
    state.defects = [{"kind": "off_task_members", "members": [1]}]
    verifier._route_defects(state, batch, set(), driver._make_ctx(1, "verify"))
    verifier._rebuild_episode(state)
    assert owner.member_positions == (0, 2)
    assert sibling.member_positions == (0, 1, 2)
    assert batch[1].status == "dropped_noise"
    validate_session(batch)
    impostor = clone_item(sibling)
    impostor.classification = None
    impostor.record = replace(impostor.record, id="unrelated-root")
    impostor.capacity = replace(impostor.capacity, root_id="unrelated-root")
    with pytest.raises(InternalError, match="overlapping"):
        validate_session([*batch, impostor])
    with pytest.raises(InternalError, match="no member owner"):
        validate_session([*batch[:3], sibling])


def test_terminal_frame_gate_stays_at_leaf_and_known_repeat_fails_fast(tmp_path):
    driver, _, metrics, _ = workflow(tmp_path, [])
    session = SessionWorkflow(driver, "session", 1)
    plan = partition((1,))
    failure = overflow([plan.items[-1]], unit="frame")
    session._advance(plan, (failure,))
    batch = plan.fresh()
    project_terminals(batch, session._context("annotate", 2))
    assert batch[-1].status == "active"
    assert terminal_capacity_failure(session._context("annotate", 2), capacity_target(batch[-1]),
                                     "frame", "default") is failure
    with pytest.raises(InternalError, match="no partition or terminal progress"):
        session._advance(plan, (replace(failure, error=ContextOverflowError("same", "reactive", "default")),))
    assert metrics.counters["capacity.minimum_failures"] == 1


def test_terminal_transition_projection_does_not_poison_different_child_request():
    plan = partition((4,))
    root = plan.items[-1]
    across = replace(overflow([root], unit="transition"),
                     targets=(replace(capacity_target(root), member_positions=(1, 2)),))
    within = replace(across, targets=(replace(capacity_target(root), member_positions=(0, 1)),))
    unrelated = replace(across, targets=(CapacityTarget("other", "other", None, (9, 10)),))
    children = plan.split(overflow([root]))
    assert _project_failure_targets((across,), children) == ()
    projected = _project_failure_targets((within, unrelated), children)
    assert projected[0].targets[0].member_positions == (0, 1)
    assert projected[1] is not None and projected[1].targets == unrelated.targets


def test_conservation_rejects_missing_absorbed_claims():
    plan = partition((2,))
    batch = plan.fresh()
    batch[0].status = "dropped_noise"
    with pytest.raises(InternalError, match="conserve"):
        validate_session(batch)


async def test_terminal_traceback_does_not_retain_discarded_attempt(tmp_path):
    class Evidence:
        pass

    class TerminalStage(CapacityStage):
        def __init__(self):
            super().__init__(0)
            self.ref = None
            self.failure = None

        async def run(self, batch, ctx):
            if self.ref is not None:
                gc.collect()
                assert self.ref() is None
                assert self.failure.__traceback__ is self.failure.__cause__ is self.failure.__context__ is None
                return batch
            local_evidence = Evidence()
            self.ref = weakref.ref(local_evidence)
            self.failure = ContextOverflowError("terminal complete evidence", "reactive", "default", "http_400")
            try:
                raise self.failure
            except ContextOverflowError as error:
                raise_session_capacity(ctx, (capacity_target(batch[-1]),), error, "fixed")

    stage = TerminalStage()
    driver, _, metrics, _ = workflow(tmp_path, [PartitionStage(), stage])
    summary = await driver.run()
    assert summary.counts["failed"] == 1
    assert stage.failure._breaker_fed and metrics.fatal_streak == 1
    assert metrics.counters["budget.overflow_records"] == 1


async def test_upstream_envelopes_and_attempts_release_before_next_session(tmp_path):
    class ObservingSegment(PartitionStage):
        def __init__(self):
            super().__init__()
            self.refs = []
            self.source = None

        async def run(self, batch, ctx):
            gc.collect()
            assert all(ref() is None for ref in self.refs)
            await super().run(batch, ctx)
            self.refs = [weakref.ref(item) for item in batch]
            self.source = batch

    class ObservingStage(CapacityStage):
        async def run(self, batch, ctx):
            assert segment.source == []
            assert all(ref() is not None for ref in segment.refs)
            assert all(item is not ref() for item, ref in zip(batch, segment.refs))
            return batch

    segment = ObservingSegment()
    driver, _, _, _ = workflow(tmp_path, [segment, ObservingStage(99)], length=2)
    driver.ingestor = FakeSessionIngestor([sess("first", 1, 2), sess("second", 10, 2)])
    driver.emitter = FakeEmitter(driver.cfg)
    summary = await driver.run()
    gc.collect()
    assert all(ref() is None for ref in segment.refs)
    assert summary.counts["emitted"] == 2


async def test_quality_rebuilds_complete_session_pool_after_each_repartition(tmp_path):
    from labelkit.operators.quality import QualityStage

    class PoolObserver(CapacityStage):
        name = "quality"

        def __init__(self):
            super().__init__(99)
            self.pools = []

        async def run(self, batch, ctx):
            stage = QualityStage(ctx.cfg)
            pools = stage._build_pools([item for item in batch if item.status == "active"])
            stage._predraw_plans(pools, ctx)
            self.pools.append(tuple(tuple(item.member_positions for item in pool.items) for pool in pools))
            ctx.metrics.count("quality.judgments", sum(len(pool.items) for pool in pools))
            return batch

    observer = PoolObserver()
    driver, _, metrics, _ = workflow(tmp_path, [PartitionStage((6, 2)), observer, CapacityStage(2)], batch_size=1)
    driver.cfg = replace(driver.cfg, quality=replace(driver.cfg.quality, enabled=True))
    summary = await driver.run()
    assert [len(pools[0]) for pools in observer.pools] == [2, 3, 4, 5]
    assert all(len(pools) == 1 for pools in observer.pools)
    assert observer.pools[-1][0] == ((0,), (1, 2), (3,), (4, 5), (6, 7))
    assert metrics.counters["quality.judgments"] == 5
    assert summary.counts["episodes"] == 5


async def test_minimum_pair_marks_both_views_without_repeating_pair_request(tmp_path):
    class MinimumPair(CapacityStage):
        name = "quality"

        async def run(self, batch, ctx):
            active = [item for item in batch if item.status == "active"]
            if active:
                self.seen.append(tuple(item.member_positions for item in active))
                raise_session_capacity(ctx, tuple(capacity_target(item) for item in active),
                                       ContextOverflowError("pair exceeds capacity", "reactive", "default"), "pairwise")
            return batch

    quality = MinimumPair(0)
    driver, dedup, metrics, _ = workflow(tmp_path, [PartitionStage((1, 1)), quality], length=2, batch_size=1)
    driver.cfg = replace(driver.cfg, quality=replace(driver.cfg.quality, enabled=True))
    summary = await driver.run()
    assert quality.seen == [((0,), (1,))]
    assert summary.counts["failed"] == metrics.counters["budget.overflow_records"] == 2
    assert metrics.counters["capacity.minimum_failures"] == 1
    assert len(dedup.index._digest_by_id) == 2


async def test_emit_rejection_is_counted_after_commit_without_reopening_capacity_attempt(tmp_path):
    class RejectingEmitter(CapturingEmitter):
        def emit_batch(self, batch, batch_no):
            for item in batch:
                if item.status == "active":
                    item.status = "failed"
            return super().emit_batch(batch, batch_no)

    annotate = CapacityStage(99)
    driver, dedup, _, _ = workflow(tmp_path, [PartitionStage(), annotate], length=2)
    driver.emitter = RejectingEmitter(driver.cfg)
    summary = await driver.run()
    assert summary.counts["emitted"] == 0 and summary.counts["failed"] == 1
    assert len(annotate.seen) == 1 and len(dedup.index._digest_by_id) == 1


async def test_repartition_counts_classification_fanout_separately_with_dedup_disabled(tmp_path):
    from labelkit.operators.classify import ClassifyStage

    class FanoutStage(CapacityStage):
        name = "classify"

        async def run(self, batch, ctx):
            processed = [item for item in batch if item.status == "active"]
            for item in processed:
                item.classification = Classification("first", ("first", "second"), "inherited", {})
            ClassifyStage._fan_out(batch, processed)
            return batch

    driver, _, _, _ = workflow(tmp_path, [PartitionStage(), FanoutStage(99), CapacityStage(1)], length=2)
    driver.cfg = replace(driver.cfg, dedup=replace(driver.cfg.dedup, enabled=False),
                         classify=replace(driver.cfg.classify, enabled=True, assignment="multi"))
    summary = await driver.run()
    assert summary.counts["episodes"] == summary.counts["fanout"] == 2
    assert summary.counts["emitted"] == 4
    assert stream_counts_invariant(summary.counts)


@pytest.mark.parametrize("completion_order", [(0, 1), (1, 0)])
async def test_all_completed_wave_overflows_split_before_one_recomputation(tmp_path, completion_order):
    class CompleteWave(CapacityStage):
        async def run(self, batch, ctx):
            active = [item for item in batch if item.status == "active"]
            failures = {}
            for ordinal in completion_order:
                if ordinal >= len(active) or len(active[ordinal].member_positions) <= 1:
                    continue
                item = active[ordinal]
                self.seen.append(item.record.id)
                failures[ordinal] = overflow([item])
            raise_session_capacities(ctx, tuple(failures[index] for index in sorted(failures)))
            return batch

    stage = CompleteWave(1)
    driver, dedup, metrics, emitter = workflow(tmp_path, [PartitionStage((2, 2)), stage], length=4)
    summary = await driver.run()
    assert len(stage.seen) == len(set(stage.seen)) == 2
    assert metrics.counters["capacity.splits"] == 2
    assert metrics.counters["capacity.recomputations"] == 1
    assert summary.counts["episodes"] == summary.counts["emitted"] == 4
    assert len(dedup.index._digest_by_id) == 4
    assert len(emitter.products) == 1


def test_wave_overlapping_sequence_and_pair_targets_do_not_create_false_terminals(tmp_path):
    driver, _, metrics, _ = workflow(tmp_path, [])
    session = SessionWorkflow(driver, "session", 1)
    plan = partition((2, 2))
    a, b = [item for item in plan.items if item.record.kind == "sequence"]
    session._advance(plan, (overflow([a]), overflow([a, b], "pairwise"), overflow([a]), overflow([b])))
    assert metrics.counters["capacity.splits"] == 2
    assert metrics.counters["capacity.recomputations"] == 1
    assert session.terminals == ()
    assert [len(item.member_positions) for item in plan.items if item.record.kind == "sequence"] == [1] * 4


def test_wave_discards_unreachable_lineage_without_marking_minimum_failure(tmp_path):
    driver, _, metrics, _ = workflow(tmp_path, [])
    session = SessionWorkflow(driver, "session", 1)
    plan = partition((2,))
    item = plan.items[-1]
    wrong_lineage = replace(overflow([item]), targets=(replace(capacity_target(item), root_id="old-root"),))
    session._advance(plan, (wrong_lineage, overflow([item])))
    assert metrics.counters["capacity.splits"] == 1
    assert session.terminals == ()


def test_wave_fixed_frame_and_transition_failures_follow_allowed_noise_positions(tmp_path):
    driver, _, metrics, _ = workflow(tmp_path, [])
    session = SessionWorkflow(driver, "session", 1)
    plan = partition((6,))
    root = plan.items[-1]
    root.member_positions = (0, 2, 4, 5)
    root.record = replace(root.record, members=tuple(plan.items[position].record for position in root.member_positions))
    frame = replace(overflow([root], "frame", "verify"),
                    targets=(replace(capacity_target(root), member_positions=(3,)),))
    transition = replace(overflow([root], "transition", "verify"),
                         targets=(replace(capacity_target(root), member_positions=(2, 3)),))
    fixed = overflow([root], "fixed", "quality")
    across = replace(transition, targets=(replace(capacity_target(root), member_positions=(3, 4)),))
    session._advance(plan, (overflow([root]), frame, frame, transition, fixed, across))
    assert metrics.counters["capacity.splits"] == metrics.counters["capacity.recomputations"] == 1
    assert metrics.counters["capacity.minimum_failures"] == 3
    assert [failure.unit for failure in session.terminals] == ["frame", "transition", "fixed"]
    children = [item for item in plan.items if item.record.kind == "sequence"]
    assert session.terminals[0].targets[0].record_id == children[0].record.id
    assert session.terminals[0].targets[0].member_positions == (3,)
    assert session.terminals[1].targets[0].member_positions == (2, 3)
    assert [target.record_id for target in session.terminals[2].targets] == [item.record.id for item in children]


def test_every_error_in_capacity_wave_releases_old_traceback_even_when_superseded(tmp_path):
    class Evidence:
        pass

    refs = []

    def failure_with_frame(item):
        local = Evidence()
        refs.append(weakref.ref(local))
        try:
            raise ContextOverflowError("completed wave overflow", "reactive", "default")
        except ContextOverflowError as error:
            return replace(overflow([item]), error=error)

    driver, _, _, _ = workflow(tmp_path, [])
    session = SessionWorkflow(driver, "session", 1)
    plan = partition((2,))
    failures = (failure_with_frame(plan.items[-1]), failure_with_frame(plan.items[-1]))
    assert all(ref() is not None for ref in refs)
    session._advance(plan, failures)
    gc.collect()
    assert all(ref() is None for ref in refs)
    assert all(failure.error.__traceback__ is None for failure in failures)

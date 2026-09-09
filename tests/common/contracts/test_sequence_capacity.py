from __future__ import annotations

import asyncio
import random
from dataclasses import replace
from types import SimpleNamespace

import pytest

from labelkit.common.contracts.execution import TaskGroupRequest, TaskSpec
from labelkit.common.contracts.sequence_capacity import (
    CapacityTarget, SessionAttemptScope, SessionCapacityFailure, capacity_sequence_id,
    capacity_failures, capacity_target, member_key, process_sequence_id, raise_session_capacities,
    raise_session_capacity, terminal_capacity_failure,
)
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import (
    Classification, PipelineItem, Record, RecordRef, SequenceBounds, SequenceCapacity,
)
from labelkit.common.errors import ContextOverflowError, OutputTruncatedError, SessionCapacityError


def _item():
    member = Record("same", "text", "same text", {}, None, None, RecordRef("a.jsonl", 1, None, ()))
    record = replace(member, id="child", kind="sequence", members=(member, member), text=None)
    return PipelineItem(record, session_id="session", member_positions=(3, 7),
                        capacity=SequenceCapacity(SequenceBounds(0, 10), root_id="root"))


def _context(scope=None, *, size=2, stream=True, executor=None):
    cfg = SimpleNamespace(segment=SimpleNamespace(enabled=stream), run=SimpleNamespace(batch_size=size))
    return RunContext(cfg, None, None, random.Random(7), 1, None, executor, "namespace", session_attempt=scope)


def test_occurrences_keep_repeated_content_independently_and_generation_keeps_its_id():
    item = _item()
    assert item.record.members[0].id == item.record.members[1].id
    assert [member_key(item, index) for index in (0, 1)] == [3, 7]
    item.capacity = None
    assert [member_key(item, index) for index in (0, 1)] == [3, 7]
    item.member_positions = ()
    assert member_key(item, 0) == "same"
    item.capacity = SequenceCapacity(SequenceBounds(0, 10))
    with pytest.raises(ValueError, match="positions"):
        member_key(item, 0)


def test_child_identity_cannot_self_reference_a_stitched_single_member_founder():
    founder = process_sequence_id("session", (0,), ("same",))
    other_occurrence = process_sequence_id("session", (1,), ("same",))
    child = capacity_sequence_id(founder, (0,), ("same",))
    assert len({founder, other_occurrence, child}) == 3
    assert child == capacity_sequence_id(founder, [0], ["same"])
    assert child != capacity_sequence_id("another_root", (0,), ("same",))


def test_sequence_identity_matches_frozen_canonical_domain_vectors():
    # 固定向量由规范中的规范化 JSON 独立求 SHA-256，不复用生产身份辅助函数。
    # ["process_sequence","session",[0],["same"]]
    assert process_sequence_id("session", (0,), ("same",)) == "37f801eeb038c37c"
    # ["process_sequence_capacity","frozen-root",[3,7],["same","same"]]
    assert capacity_sequence_id("frozen-root", (3, 7), ("same", "same")) == "199c3c87fe05cc95"
    # 非 ASCII 身份按原始 UTF-8 编码，不能先变成 JSON 转义序列。
    assert process_sequence_id("会话", (0,), ("重复",)) == "603c8d69bf956353"
    assert capacity_sequence_id("根", (3, 7), ("重复", "重复")) == "1537a23486cfd8a8"


@pytest.mark.parametrize("positions,ids", [((), ()), ((0,), ()), ((1, 0), ("a", "b")),
                                             ((0, 0), ("a", "b")), ((-1,), ("a",)), ((True,), ("a",))])
def test_invalid_occurrence_partition_cannot_produce_sequence_identity(positions, ids):
    with pytest.raises(ValueError, match="ordered, nonempty"):
        process_sequence_id("session", positions, ids)


def test_terminal_frame_request_is_isolated_by_stage_profile_label_and_occurrence():
    target = CapacityTarget("root", "child", "alpha", (7,))
    error = ContextOverflowError("oversized frame", "reactive", "local", origin="finish")
    failure = SessionCapacityFailure("annotate", (target,), "frame", error)
    scope = SessionAttemptScope("session", 1, 2, "annotate", (failure,))
    ctx = _context(scope)
    assert terminal_capacity_failure(ctx, target, "frame", "local") is failure
    for changed in (replace(target, label="beta"), replace(target, member_positions=(3,)),
                    replace(target, root_id="different")):
        assert terminal_capacity_failure(ctx, changed, "frame", "local") is None
    assert terminal_capacity_failure(ctx, target, "frame", "other") is None
    assert terminal_capacity_failure(ctx, target, "sequence", "local") is None
    ctx.session_attempt = replace(scope, stage="verify")
    assert terminal_capacity_failure(ctx, target, "frame", "local") is None


def test_verify_expansion_failure_projects_to_original_view_without_repeating_expansion():
    item = _item()
    item.classification = Classification("alpha", ("alpha",), "llm", {})
    target = capacity_target(item, (2, 3, 7))
    error = ContextOverflowError("expanded request", "precheck", "local")
    failure = SessionCapacityFailure("verify", (target,), "sequence", error)
    ctx = _context(SessionAttemptScope("session", 1, 3, "verify", (failure,)))
    baseline = capacity_target(item)
    assert baseline.member_positions == (3, 7)
    assert terminal_capacity_failure(ctx, baseline, "sequence", "local") is failure
    assert terminal_capacity_failure(ctx, replace(baseline, record_id="other_child"), "sequence", "local") is None


def test_terminal_transition_blocks_its_current_view_across_distinct_pairs_only():
    first = CapacityTarget("root", "child", "alpha", (2, 3))
    second = replace(first, member_positions=(3, 7))
    error = ContextOverflowError("unbreakable pair", "reactive", "local")
    failure = SessionCapacityFailure("verify", (first,), "transition", error)
    ctx = _context(SessionAttemptScope("session", 1, 2, "verify", (failure,)))
    assert terminal_capacity_failure(ctx, first, "transition", "local") is failure
    assert terminal_capacity_failure(ctx, second, "transition", "local") is failure
    assert failure.targets == (first,)
    for changed in (replace(second, record_id="other_child"), replace(second, root_id="other_root"),
                    replace(second, label="beta")):
        assert terminal_capacity_failure(ctx, changed, "transition", "local") is None
    assert terminal_capacity_failure(ctx, second, "transition", "other") is None
    assert terminal_capacity_failure(ctx, second, "frame", "local") is None
    ctx.session_attempt = replace(ctx.session_attempt, stage="extract")
    assert terminal_capacity_failure(ctx, second, "transition", "local") is None


def test_capacity_control_preserves_error_origin_and_does_not_reclassify_other_errors():
    target = capacity_target(_item())
    ctx = _context(SessionAttemptScope("session", 1, 1, "verify"))
    error = ContextOverflowError("actual endpoint overflow", "reactive", "local", origin="finish")
    with pytest.raises(SessionCapacityError) as caught:
        raise_session_capacity(ctx, (target,), error, "sequence")
    failure, = caught.value.failures
    assert failure.error is error
    assert failure.stage == "verify"
    assert failure.targets == (target,)
    assert caught.value.__cause__ is error
    ctx.session_attempt = replace(ctx.session_attempt, terminal_failures=(failure,))
    raise_session_capacity(ctx, (target,), error, "sequence")
    raise_session_capacity(ctx, (target,), OutputTruncatedError("output cap"), "sequence")
    raise_session_capacity(_context(), (target,), error, "sequence")
    assert terminal_capacity_failure(_context(), target, "sequence", "local") is None
    ctx.session_attempt = replace(ctx.session_attempt, terminal_failures=())
    with pytest.raises(ValueError, match="explicit request target"):
        raise_session_capacity(ctx, (), error, "fixed")


def test_complete_wave_preserves_all_failures_in_declaration_order_and_filters_only_known_terminals():
    first = CapacityTarget("root", "first", "alpha", (3,))
    second = CapacityTarget("root", "second", "alpha", (7,))
    ctx = _context(SessionAttemptScope("session", 1, 2, "verify"))
    errors = [ContextOverflowError(f"overflow {i}", "reactive", "local") for i in (1, 2)]
    failures = tuple(failure for target, error in zip((first, second), errors, strict=True)
                     for failure in capacity_failures(ctx, (target,), error, "frame"))
    with pytest.raises(SessionCapacityError) as caught:
        raise_session_capacities(ctx, failures)
    assert caught.value.failures == failures
    assert [failure.error for failure in caught.value.failures] == errors
    assert capacity_failures(ctx, (), caught.value, "sequence") == failures
    assert capacity_failures(_context(), (), caught.value, "sequence") == ()
    ctx.session_attempt = replace(ctx.session_attempt, terminal_failures=(failures[0],))
    with pytest.raises(SessionCapacityError) as remaining:
        raise_session_capacities(ctx, failures)
    assert remaining.value.failures == (failures[1],)
    ctx.session_attempt = replace(ctx.session_attempt, terminal_failures=failures)
    assert raise_session_capacities(ctx, failures) is None
    assert raise_session_capacities(_context(), failures) is None
    assert not hasattr(caught.value, "failure")


def test_capacity_signal_requires_nonempty_wave():
    with pytest.raises(ValueError, match="at least one failure"):
        SessionCapacityError(())


class _Executor:
    def __init__(self):
        self.groups = []

    async def run_group(self, request):
        self.groups.append(tuple(task.task_id for task in request.tasks))
        return tuple(await asyncio.gather(*(task.operation() for task in request.tasks)))


@pytest.mark.asyncio
@pytest.mark.parametrize("size,expected", [(1, [1] * 5), (2, [2, 2, 1]), (8, [5])])
async def test_physical_group_size_changes_without_changing_frozen_task_order(size, expected):
    async def operation(value):
        await asyncio.sleep((5 - value) * 0.001)
        return value

    tasks = tuple(TaskSpec(str(i), (1, i), "segment", ("llm", "local"),
                           lambda i=i: operation(i)) for i in range(5))
    executor = _Executor()
    ctx = _context(SessionAttemptScope("session", 1, 0, "segment"), size=size, executor=executor)
    assert await ctx.run_group(TaskGroupRequest(tasks)) == tuple(range(5))
    assert [len(group) for group in executor.groups] == expected
    assert tuple(key for group in executor.groups for key in group) == tuple(str(i) for i in range(5))
    before = list(executor.groups)
    assert await ctx.run_group(TaskGroupRequest(())) == ()
    assert executor.groups == before
    executor.groups.clear()
    ctx.session_attempt = None
    assert await ctx.run_group(TaskGroupRequest(tasks)) == tuple(range(5))
    assert len(executor.groups) == 1

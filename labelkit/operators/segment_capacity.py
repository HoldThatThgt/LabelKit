"""分段完成后的完整成员容量分区与信封构造。"""
from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Sequence

from labelkit.common.contracts.sequence_capacity import process_sequence_id
from labelkit.common.contracts.types import (
    CapacityCut,
    PipelineItem,
    Record,
    RecordRef,
    SequenceBounds,
    SequenceCapacity,
    StageError,
)
from labelkit.common.errors import InternalError

if TYPE_CHECKING:
    from labelkit.common.contracts.sequence_capacity import SessionCapacityFailure
    from labelkit.common.contracts.stage import RunContext

_log = logging.getLogger("labelkit.segment")


@dataclasses.dataclass(frozen=True)
class _Partition:
    """一个容量分区及导致该分区结束的容量证据。"""

    frames: tuple[PipelineItem, ...]  # 按会话出现位置排列的成员信封。
    failure: SessionCapacityFailure | None  # 终止增长或最小成员失败的证据。
    failed: bool  # 单个完整成员仍无法进入下游请求。


def episode_envelope(sid: str, frames: Sequence[PipelineItem], bounds: SequenceBounds) -> PipelineItem:
    """以最终成员出现位置构造尚未提交的序列信封。

    @param sid 原会话身份。
    @param frames 按出现位置排列的完整帧信封。
    @param bounds 当前序列允许的成员位置范围。
    @return 无副作用的新序列信封。
    """
    positions = tuple(frame.session_position for frame in frames)
    if not positions or any(position is None for position in positions):
        _log.error("segment requires explicit member occurrence positions")
        raise InternalError("segment requires explicit member occurrence positions")
    records = tuple(frame.record for frame in frames)
    first = records[0]
    record = Record(
        id=process_sequence_id(sid, positions, tuple(member.id for member in records)),
        modality=first.modality, text=None, raw=None, ui_tree=None, image=None,
        ref=RecordRef(source_file=first.ref.source_file, line_no=first.ref.line_no,
                      pair_index=first.ref.pair_index, generated_from=(), generator=None),
        kind="sequence", members=records,
    )
    return PipelineItem(record=record, session_id=sid, member_positions=positions,
                        capacity=SequenceCapacity(bounds))


def _partition_members(sid: str, frames: Sequence[PipelineItem],
                       bounds: SequenceBounds, ctx: RunContext) -> list[_Partition]:
    """用真实下游预览贪心确定完整成员分区。

    @param sid 原会话身份。
    @param frames 已通过语义短段判定的完整成员。
    @param bounds 当前会话的完整位置范围。
    @param ctx 本会话分段上下文。
    @return 不重叠且完整覆盖输入成员的分区。
    """
    if ctx.capacity_checker is None:
        _log.error("segment requires a sequence capacity checker")
        raise InternalError("segment requires a sequence capacity checker")
    partitions: list[_Partition] = []
    start = 0
    while start < len(frames):
        end = start
        failure = None
        while end < len(frames):
            preview = episode_envelope(sid, frames[start:end + 1], bounds)
            failure = ctx.capacity_checker.preview(preview, ctx)
            if failure is not None:
                if failure.unit != "sequence":
                    # 不可缩请求归原下游阶段处置，不能删掉相邻对或提前误杀帧级产物。
                    return [_Partition(tuple(frames), None, False)]
                break
            end += 1
        failed = end == start
        stop = end + 1 if failed else end
        partitions.append(_Partition(tuple(frames[start:stop]), failure, failed))
        start = stop
    return partitions


def _partition_cuts(partitions: Sequence[_Partition]) -> list[CapacityCut]:
    """从增长失败证据生成相邻分区的永久容量切点。

    @param partitions 按输入顺序确定的完整分区。
    @return 与相邻分区一一对应的切点。
    """
    cuts: list[CapacityCut] = []
    for left, right in zip(partitions, partitions[1:]):
        failure = left.failure
        if failure is None:
            _log.error("capacity partition is missing its boundary evidence")
            raise InternalError("capacity partition is missing its boundary evidence")
        cuts.append(CapacityCut(
            left_position=left.frames[-1].session_position,
            right_position=right.frames[0].session_position,
            stage=failure.stage, profile=failure.error.profile, phase=failure.error.phase,
        ))
    return cuts


def _emit_capacity_evidence(cuts: Sequence[CapacityCut], ctx: RunContext) -> None:
    """只记录分区位置与预算归因，不记录成员数据。

    @param cuts 本次新产生的切点。
    @param ctx 当前会话上下文。
    """
    for cut in cuts:
        ctx.metrics.count("capacity.splits")
        ctx.metrics.count("capacity.sealed")
        ctx.metrics.event("sequence.capacity", stage="segment", batch_no=ctx.batch_no,
                          payload={"action": "split", **dataclasses.asdict(cut)})


def capacity_episodes(sid: str, frames: Sequence[PipelineItem],
                      bounds: SequenceBounds, ctx: RunContext) -> list[PipelineItem]:
    """将语义片段分为容量内子序列，最小成员不可装时留下明确失败。

    @param sid 原会话身份。
    @param frames 已通过语义短段判定的完整成员。
    @param bounds 原会话允许范围。
    @param ctx 当前会话上下文。
    @return 最终可提交到分段列表的序列信封。
    """
    partitions = _partition_members(sid, frames, bounds, ctx)
    cuts = _partition_cuts(partitions)
    result: list[PipelineItem] = []
    for index, partition in enumerate(partitions):
        item = episode_envelope(sid, partition.frames, bounds)
        before = cuts[index - 1] if index else bounds.before
        after = cuts[index] if index < len(cuts) else bounds.after
        if before is not None or after is not None:
            item.capacity = SequenceCapacity(
                SequenceBounds(before.right_position if before else bounds.lower,
                               after.right_position if after else bounds.upper, before, after),
                sealed=after is not None,
            )
        if partition.failed:
            _fail_episode(item, partition.failure, ctx)
        result.append(item)
    _emit_capacity_evidence(cuts, ctx)
    return result


def _fail_episode(item: PipelineItem, failure: SessionCapacityFailure,
                  ctx: RunContext) -> None:
    """最小完整帧预算失败保留序列身份及精确阶段归因。

    @param item 已构造的单成员序列。
    @param failure 不可恢复的完整证据预算失败。
    @param ctx 当前会话上下文。
    """
    item.status = "failed"
    item.errors.append(StageError(stage=failure.stage, kind="context_overflow",
                                  message=str(failure.error), retryable=False))
    _log.error("minimal sequence capacity failed: stage=%s profile=%s",
               failure.stage, failure.error.profile)
    ctx.metrics.count("capacity.minimum_failures")
    ctx.metrics.count("budget.overflow_records")
    ctx.metrics.event("sequence.capacity", stage="segment", batch_no=ctx.batch_no,
                      record_ids=(item.record.id,),
                      payload={"action": "minimum_failure", "stage": failure.stage,
                               "profile": failure.error.profile, "phase": failure.error.phase,
                               "member_positions": list(item.member_positions)})

"""完整会话的冻结分区、容量切点与最小失败执行门。"""
from __future__ import annotations

import copy
import logging
from dataclasses import replace

from labelkit.common.contracts.sequence_capacity import (
    capacity_sequence_id, capacity_target, terminal_capacity_failure,
)
from labelkit.common.contracts.types import (
    CapacityCut, PipelineItem, SequenceBounds, SequenceCapacity, StageError,
)
from labelkit.common.errors import InternalError

_log = logging.getLogger(__name__)


def clone_item(item: PipelineItem) -> PipelineItem:
    """复制全部可变信封产物并共享原始证据。@param item 已冻结信封。@return 独立信封。"""
    memo = {id(item.record): item.record}
    memo.update({id(member): member for member in item.record.members})
    return copy.deepcopy(item, memo)


class SessionPartition:
    """只允许增加切点的上游结果快照，重算不再次分段或缝合。"""

    def __init__(self, batch: list[PipelineItem], frame_count: int):
        """接管上游信封并冻结认领范围。@param batch 调用方随后释放的上游列表。@param frame_count 原始帧数。"""
        self.items = list(batch)
        self.net_episodes = 0
        for item in self.items:
            if item.record.kind != "sequence":
                continue
            capacity = item.capacity or SequenceCapacity(SequenceBounds(0, frame_count))
            item.capacity = replace(capacity, root_id=item.record.id)

    def fresh(self) -> list[PipelineItem]:
        """为一次下游重算提供新信封。@return 不共享可变产物的会话列表。"""
        return [clone_item(item) for item in self.items]

    def split(self, failure) -> tuple[PipelineItem, PipelineItem] | None:
        """按完整成员中点严格缩小一个基线序列。@param failure 实际容量归属。@return 两个子序列或空。"""
        if failure.unit not in ("sequence", "pairwise"):
            return None
        ids = {target.record_id for target in failure.targets}
        candidates = [item for item in self.items if item.record.id in ids
                      and item.status == "active" and len(item.member_positions) > 1]
        if not candidates:
            return None
        parent = min(candidates, key=lambda item: (-len(item.member_positions), item.member_positions[0]))
        middle = len(parent.member_positions) // 2
        cut = CapacityCut(parent.member_positions[middle - 1], parent.member_positions[middle],
                          failure.stage, failure.error.profile, failure.error.phase)
        children = (_child(parent, slice(0, middle), cut), _child(parent, slice(middle, None), cut))
        index = self.items.index(parent)
        self.items[index:index + 1] = children
        self.net_episodes += 1
        return children


def _child(parent: PipelineItem, selection: slice, cut: CapacityCut) -> PipelineItem:
    """构造有独立身份和不可跨越范围的完整成员子序列。@param parent 父项。@param selection 成员切片。@param cut 切点。"""
    child = clone_item(parent)
    positions = parent.member_positions[selection]
    members = parent.record.members[selection]
    capacity = parent.capacity
    root = capacity.root_id
    identity = capacity_sequence_id(root, positions, tuple(member.id for member in members))
    child.record = replace(parent.record, id=identity, members=members)
    child.member_positions = positions
    bounds = capacity.bounds
    if positions[0] >= cut.right_position:
        bounds = replace(bounds, lower=cut.right_position, before=cut)
    else:
        bounds = replace(bounds, upper=cut.right_position, after=cut)
    child.capacity = SequenceCapacity(bounds, sealed=True, root_id=root, parent_id=parent.record.id)
    if child.thread_id is not None:
        child.thread_id = identity
    _project_fragments(parent, child)
    return child


def _project_fragments(parent: PipelineItem, child: PipelineItem) -> None:
    """按出现位置保留碎片与接缝的原始归属。@param parent 父项。@param child 新子项。"""
    if not hasattr(parent, "stitch_fragments"):
        return
    members = dict(zip(child.member_positions, child.record.members, strict=True))
    fragments = []
    for fragment in parent.stitch_fragments:
        positions = tuple(position for position in fragment["member_positions"] if position in members)
        if positions:
            from labelkit.operators.stitch import _order_key_repr
            fragments.append({**fragment, "member_positions": positions, "member_count": len(positions),
                              "order_span": [_order_key_repr(members[positions[0]]),
                                             _order_key_repr(members[positions[-1]])]})
    child.stitch_fragments = tuple(sorted(fragments, key=lambda fragment: fragment["member_positions"][0]))
    offset = parent.member_positions.index(child.member_positions[0])
    seams = tuple(index - offset for index in parent.seam_indexes
                  if offset <= index < offset + len(child.member_positions) - 1)
    child.seam_indexes = seams
    child.seam_interrupted_by = tuple(
        value for index, value in zip(parent.seam_indexes, parent.seam_interrupted_by, strict=True)
        if index - offset in seams)


def project_terminals(batch, ctx) -> None:
    """在所属阶段执行门重放非帧最小失败，不跳过前置阶段。@param batch 新尝试。@param ctx 当前阶段上下文。"""
    for item in batch:
        if item.status != "active" or item.record.kind != "sequence":
            continue
        target = capacity_target(item)
        for failure in ctx.session_attempt.terminal_failures:
            if failure.unit == "frame":
                continue
            known = terminal_capacity_failure(ctx, target, failure.unit, failure.error.profile)
            if known is not None:
                _terminal_item(item, known, ctx)
                break


def _terminal_item(item, failure, ctx) -> None:
    """投影已确立最小失败的产品与终局预算计数。@param item 序列视图。@param failure 原始失败。@param ctx 上下文。"""
    error = failure.error
    _log.error("sequence reached minimum context capacity", extra={"stage": failure.stage, "batch": ctx.batch_no})
    item.status = "failed"
    item.errors.append(StageError(stage=failure.stage, kind="context_overflow", message=str(error), retryable=False))
    ctx.metrics.count("budget.overflow_records")
    if error.phase == "reactive" and error.origin == "http_400" and not getattr(error, "_breaker_fed", False):
        error._breaker_fed = True
        ctx.metrics.record_provider_result(fatal=True)
    ctx.metrics.event("error", stage=failure.stage, batch_no=ctx.batch_no, record_ids=(item.record.id,),
                      payload={"stage": failure.stage, "kind": "context_overflow",
                               "message": str(error), "retryable": False})


def validate_session(batch: list[PipelineItem]) -> None:
    """正式提交前核对原始出现位置、认领范围与帧状态守恒。@param batch 最终尝试产品。"""
    frames = {item.session_position: item for item in batch if item.record.kind != "sequence"}
    claimed = set()
    owners = set()
    clones = set()
    for item in batch:
        if item.record.kind != "sequence" or item.status == "stitched":
            continue
        positions = item.member_positions
        valid = bool(positions) and len(positions) == len(item.record.members)
        valid = valid and all(a < b for a, b in zip(positions, positions[1:]))
        bounds = item.capacity.bounds
        valid = valid and all(position in frames and bounds.lower <= position < bounds.upper for position in positions)
        valid = valid and all(frames[position].record is member
                              for position, member in zip(positions, item.record.members))
        if not valid:
            _log.error("session sequence membership violates the frozen occurrence contract")
            raise InternalError("session sequence membership violates the frozen occurrence contract")
        identity = (item.capacity.root_id, item.record.id)
        classification = item.classification
        if classification is not None and classification.labels and classification.label != classification.labels[0]:
            clones.add(identity)
            continue
        if claimed.intersection(positions):
            _log.error("independent session sequences claim overlapping occurrences")
            raise InternalError("independent session sequences claim overlapping occurrences")
        owners.add(identity)
        claimed.update(positions)
    if not clones.issubset(owners):
        _log.error("session classification clone has no member owner")
        raise InternalError("session classification clone has no member owner")
    absorbed = {position for position, frame in frames.items() if frame.status == "absorbed"}
    if absorbed != claimed:
        _log.error("session frame claims do not conserve the frozen occurrences")
        raise InternalError("session frame claims do not conserve the frozen occurrences")

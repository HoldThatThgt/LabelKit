"""序列评审的完整请求预算、人工边界与手术回滚。"""
from __future__ import annotations

import copy
import dataclasses
import logging
from typing import TYPE_CHECKING, Mapping, Sequence

from labelkit.common.contracts.sequence_capacity import (
    capacity_failures, capacity_target, raise_session_capacities, raise_session_capacity,
)
from labelkit.common.errors import ContextOverflowError, InternalError, SessionCapacityError
from labelkit.common.inference.sequence_evidence import CapacityRequest, preview_failure, terminal_error

if TYPE_CHECKING:
    from labelkit.common.contracts.stage import RunContext
    from labelkit.common.contracts.types import PipelineItem
    from labelkit.operators.verify import VerifyStage

_log = logging.getLogger("labelkit.verify.capacity")


def review_request(stage: VerifyStage, item: PipelineItem, ctx: RunContext,
                   options, profile: str) -> CapacityRequest:
    """按当前已有完整成果构造评审请求及固定开销。

    @param stage 当前评审阶段。
    @param item 当前完整工作序列。
    @param ctx 冻结会话上下文。
    @param options 当前实际评审参数。
    @param profile 当前评委名称。
    @return 与实际调用同源的完整请求。
    """
    from labelkit.common.inference.schema_engine import defect_verdict_schema
    from labelkit.operators.verify import build_verify_prompt

    output = item.annotation.output if item.annotation is not None else {}
    prompt = build_verify_prompt(item.record, output, stage.cfg, options)
    empty = dataclasses.replace(item.record, members=())
    fixed_options = dataclasses.replace(options, member_positions=(), transitions=None,
                                        boundary_margin="", fragment_structure="", boundary_records=())
    fixed = build_verify_prompt(empty, {}, stage.cfg, fixed_options)
    return CapacityRequest(profile, prompt, defect_verdict_schema(), fixed)


def preview_capacity(stage: VerifyStage, item: PipelineItem, ctx: RunContext):
    """预览全部可达分类视图的完整已知评审证据，不伪造未来标注。

    @param stage 当前评审阶段。
    @param item 候选完整序列。
    @param ctx 冻结会话上下文。
    @return 首个明确的完整请求容量问题。
    """
    from labelkit.operators.verify import VerifyPromptOptions, fragment_structure_text

    labels = ((item.classification.label,) if item.classification else
              tuple(stage.cfg.class_views) if stage.cfg.classify.enabled else (None,))
    judges, _ = stage._judge_panel()
    structure = fragment_structure_text(item) if stage.cfg.stitch.enabled else ""
    for label in labels:
        options = VerifyPromptOptions(label=label, transitions=item.transitions,
                                      fragment_structure=structure, member_positions=item.member_positions)
        for profile in judges:
            target = dataclasses.replace(capacity_target(item), label=label)
            request = review_request(stage, item, ctx, options, profile)
            failure = preview_failure(ctx, (target,), "sequence", request)
            if failure is not None:
                return failure
    return None


def check_review(item: PipelineItem, request: CapacityRequest, ctx: RunContext) -> None:
    """在实际评审发送前消费终态或转交新容量问题。

    @param item 本次工作序列。
    @param request 实际完整评委请求。
    @param ctx 所属 verify 会话尝试。
    """
    target = capacity_target(item)
    for unit in ("sequence", "fixed"):
        known = terminal_error(ctx, target, unit, request.profile)
        if known is not None:
            raise known
    failure = preview_failure(ctx, (target,), "sequence", request)
    if failure is not None:
        raise_session_capacity(ctx, failure.targets, failure.error, failure.unit)
        raise failure.error


def check_reviews(item: PipelineItem, requests: Sequence[CapacityRequest], ctx: RunContext) -> None:
    """收齐整评审团同步预算问题后统一上抛。

    @param item 当前完整工作序列。
    @param requests 全部评委完整请求。
    @param ctx 当前会话尝试。
    """
    errors = []
    for request in requests:
        try:
            check_review(item, request, ctx)
        except (ContextOverflowError, SessionCapacityError) as exc:
            errors.append(exc)
    failures = tuple(failure for error in errors
                     for failure in capacity_failures(ctx, (capacity_target(item),), error, "sequence"))
    raise_session_capacities(ctx, failures)
    if errors:
        raise errors[0]


def allows_position(item: PipelineItem, position: int) -> bool:
    """检查回收位置是否位于明确允许区间。

    @param item 当前序列。
    @param position 拟认领的输入出现位置。
    @return 是否允许纳入该序列。
    """
    if item.capacity is None:
        _log.error("stream verification requires explicit capacity bounds")
        raise InternalError("stream verification requires explicit capacity bounds")
    bounds = item.capacity.bounds
    return bounds.lower <= position < bounds.upper


def boundary_suspicion(item: PipelineItem, defect: Mapping) -> bool:
    """只识别准确贴合本序列人工切点的缺头或缺尾。

    @param item 当前完整序列。
    @param defect 当前缺陷。
    @return 该缺陷是否只反映已知人工容量边界。
    """
    bounds = item.capacity.bounds
    named = set(defect.get("members") or ())
    if defect["kind"] == "missing_head":
        cut = bounds.before
        return (cut is not None and item.member_positions[0] == cut.right_position
                and (not named or named == {cut.left_position}))
    if defect["kind"] == "missing_tail":
        cut = bounds.after
        return (cut is not None and item.member_positions[-1] == cut.left_position
                and (not named or named == {cut.right_position}))
    return False


def snapshot_items(items: Sequence[PipelineItem]) -> tuple[dict, ...]:
    """保留阶段开始时全部可变产物，共享原始不可变证据。

    @param items 当前完整会话信封。
    @return 按输入序排列的独立状态字典。
    """
    memo = {id(item.record): item.record for item in items}
    memo.update({id(member): member for item in items for member in item.record.members})
    return tuple(copy.deepcopy(item.__dict__, memo) for item in items)


def restore_items(items: Sequence[PipelineItem], snapshots: Sequence[dict]) -> None:
    """容量信号逃逸前恢复所有手术、认领与已重建产物。

    @param items 同一次调用的原始信封列表。
    @param snapshots 阶段开始时的状态字典。
    """
    for item, snapshot in zip(items, snapshots, strict=True):
        item.__dict__.clear()
        item.__dict__.update(snapshot)


def working_item(state):
    """构造尚未提交手术的完整工作信封。@param state 评审台账。@return 无副作用的工作序列。"""
    return dataclasses.replace(state.item,
                               record=dataclasses.replace(state.item.record, members=tuple(state.working_members)),
                               member_positions=tuple(state.working_positions))


def working_failures(state, outcome: object, ctx: RunContext,
                      unit: str = "sequence", positions=None) -> tuple:
    """归集属于完整工作目标的容量问题，供整波次一次上抛。

    @param state 当前评审台账。
    @param outcome 已收齐的叶任务结果。
    @param ctx 当前 verify 会话尝试。
    @param unit 请求最小单位。
    @param positions 实际最小请求位置；为空时使用全部工作成员。
    @return 保留声明序的容量失败元组。
    """
    return capacity_failures(ctx, (capacity_target(working_item(state), positions),), outcome, unit)


def project_fragments(item: PipelineItem, previous_positions: Sequence[int]) -> None:
    """按出现位置投影碎片，回收成员归入前邻原碎片，段首回收归入后邻。

    @param item 已重绑完整成员的序列。
    @param previous_positions 手术前成员的明确位置。
    """
    from labelkit.operators.stitch import _order_key_repr

    if not hasattr(item, "stitch_fragments"):
        return
    fragments = item.stitch_fragments
    owner = {position: index for index, fragment in enumerate(fragments)
             for position in fragment["member_positions"]}
    members = dict(zip(item.member_positions, item.record.members, strict=True))
    assigned = [[] for _ in fragments]
    for position in item.member_positions:
        if position in owner:
            index = owner[position]
        else:
            previous = [old for old in previous_positions if old < position]
            neighbor = previous[-1] if previous else next(old for old in previous_positions if old > position)
            index = owner[neighbor]
        assigned[index].append(position)
    projected = (
        {**fragment, "order_span": [_order_key_repr(members[positions[0]]),
                                    _order_key_repr(members[positions[-1]])],
         "member_count": len(positions), "member_positions": positions}
        for fragment, positions in zip(fragments, assigned, strict=True) if positions
    )
    item.stitch_fragments = tuple(sorted(projected, key=lambda fragment: fragment["member_positions"][0]))


def plan_seams(states, batch: Sequence[PipelineItem]) -> None:
    """以本轮完整工作成员归属预先计算手术后的接缝。

    @param states 本轮需要修复的序列台账。
    @param batch 完整会话信封。
    """
    working = {id(state.item): tuple(state.working_positions) for state in states}
    projected = [dataclasses.replace(item, member_positions=working[id(item)])
                 if id(item) in working else item for item in batch]
    for original, projection in zip(batch, projected, strict=True):
        if hasattr(original, "stitch_task_name"):
            projection.stitch_task_name = original.stitch_task_name
    for state in states:
        state.seams = current_seams(working_item(state), projected)


def current_seams(item: PipelineItem, batch: Sequence[PipelineItem]) -> dict[int, tuple[str, ...]]:
    """按同会话最终已提交成员归属计算接缝和真实中断名。

    @param item 当前完整序列。
    @param batch 完整会话信封。
    @return 接缝下标到中断名的映射。
    """
    from labelkit.operators.stitch import compute_seams

    # 首标签拥有真实成员；同序列其他标签只是消费视图，不能形成自身中断。
    owners = {position: owner.stitch_task_name for owner in batch
              if owner.record.kind == "sequence" and owner.status != "stitched" and owner.record.id != item.record.id
              and owner.session_id == item.session_id and hasattr(owner, "stitch_task_name")
              and (owner.classification is None or owner.classification.label == owner.classification.labels[0])
              for position in owner.member_positions}
    indexes, names = compute_seams(item.member_positions, owners)
    return dict(zip(indexes, names, strict=True))

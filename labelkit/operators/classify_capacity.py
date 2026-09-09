"""分类完整执行波次的容量事实收集，不提交任何业务结果。"""
from __future__ import annotations

from dataclasses import replace

from labelkit.common.contracts.sequence_capacity import capacity_failures, capacity_target
from labelkit.common.errors import ContextOverflowError
from labelkit.common.inference.schema_engine import classification_schema, frame_classify_schema
from labelkit.common.inference.sequence_evidence import CapacityRequest, record_evidence, request_unit


def sequence_request(record, cfg):
    """构造完整分类请求与保留全部固定包络的空成员请求。

    @param record 当前完整序列。
    @param cfg 冻结配置。
    @return 同源的实际请求、固定请求和模型 Schema。
    """
    from labelkit.operators.classify import _reason_requested, build_classify_prompt

    reason = _reason_requested(cfg)
    schema = classification_schema([spec.name for spec in cfg.classify.classes],
                                   cfg.classify.assignment, cfg.classify.max_labels, reason)
    prompt = build_classify_prompt(record, cfg, reason)
    fixed = build_classify_prompt(replace(record, members=()), cfg, reason)
    return CapacityRequest(cfg.classify.llm, prompt, schema, fixed)


def frame_request(members, cfg):
    """构造整段帧分类请求；固定包络使用同一构造器的空成员输入。

    @param members 当前请求的全部成员。
    @param cfg 冻结配置。
    @return 保留实际定长 Schema 的完整请求和固定请求。
    """
    from labelkit.operators.classify import build_frame_classify_prompt

    prompt = build_frame_classify_prompt(members, cfg, tuple(record_evidence(member) for member in members))
    fixed = build_frame_classify_prompt((), cfg, ())
    schema = frame_classify_schema([spec.name for spec in cfg.frame_classify.classes], len(members))
    return CapacityRequest(cfg.frame_classify.llm, prompt, schema, fixed)


def sequence_failures(stage, items, plans, outcomes, ctx):
    """收集计划异常与全部分类样本的容量事实。

    @param stage 当前分类阶段。
    @param items 信封声明序。
    @param plans 与信封对齐的计划或同步异常。
    @param outcomes 全部叶结果，保持声明序。
    @param ctx 当前会话尝试。
    @return 按 item/sample 顺序的容量失败元组。
    """
    failures = []
    offset = 0
    for item, plan in zip(items, plans):
        errors = (plan,) if isinstance(plan, BaseException) else tuple(
            result.value for result in outcomes[offset:offset + plan.sample_count])
        if not isinstance(plan, BaseException):
            offset += plan.sample_count
        for error in errors:
            unit = "sequence"
            if ctx.session_attempt is not None and isinstance(error, ContextOverflowError):
                preview = stage.preview_capacity(item, ctx)
                unit = "fixed" if preview is not None and preview.unit == "fixed" else unit
            failures.extend(capacity_failures(ctx, (capacity_target(item),), error, unit))
    return tuple(failures)


def frame_plan_failures(plan, outcomes, ctx):
    """收集一条帧分类计划的所有原始窗和内部叶容量结果。

    @param plan 包含实际成员位置的完整计划。
    @param outcomes 按原始窗声明序返回的纯结果。
    @param ctx 当前 owning stage 上下文。
    @return 保留原错误及目标的失败元组。
    """
    if plan.target is None:
        return ()
    failures = []
    for outcome in outcomes:
        for (start, end), result in outcome.leaves:
            target = replace(plan.target, member_positions=plan.target.member_positions[start:end])
            unit = "sequence" if len(target.member_positions) > 1 else "frame"
            if isinstance(result, ContextOverflowError) and unit == "sequence":
                unit = request_unit(frame_request(plan.members[start:end], ctx.cfg), ctx, unit)
            failures.extend(capacity_failures(ctx, (target,), result, unit))
    return tuple(failures)


def frame_wave_failures(plans, outcomes, ctx):
    """在任何成员字典归并前收齐完整帧波次。

    @param plans 各信封与其帧计划。
    @param outcomes 扁平原始窗结果。
    @param ctx 当前会话尝试。
    @return 按信封、窗口与成员顺序排列的失败。
    """
    failures = []
    offset = 0
    for _item, plan in plans:
        selected = outcomes[offset:offset + len(plan.spans)]
        offset += len(plan.spans)
        failures.extend(frame_plan_failures(plan, selected, ctx))
    return tuple(failures)

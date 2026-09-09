"""质量评估的完整序列证据和会话容量路径。"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from labelkit.common.contracts.sequence_capacity import capacity_failures, capacity_target
from labelkit.common.errors import CircuitBreakerTripped, ContextOverflowError, ProviderFatalError
from labelkit.common.inference.llm_client import Part
from labelkit.common.inference.schema_engine import CallScope, judgment_schema, pointwise_schema
from labelkit.common.inference.sequence_evidence import (
    CapacityRequest, preview_failure, request_overflow, request_unit, sequence_parts,
)
from labelkit.operators.quality_calls import PairwiseQualityCall, PointwiseQualityOutcome, QualityCallFailure

if TYPE_CHECKING:
    from labelkit.common.contracts.stage import RunContext
    from labelkit.common.contracts.types import PipelineItem, Record, Transition
    from labelkit.operators.quality import QualityStage

_logger = logging.getLogger("labelkit.quality")


def complete_parts(record: Record, label: str, transitions: tuple[Transition, ...] | None) -> list[Part]:
    """渲染完整动作、成员文本、可见树与所有图片。

    @param record 序列记录
    @param label 当前比较侧标签
    @param transitions 已产生的全部动作
    @return 当前比较侧完整证据部件
    """
    from labelkit.operators.quality import _step_line

    parts = [Part(kind="text", text=f"[{label}·操作序列]")]
    if transitions is not None:
        parts.append(Part(kind="text", text="[步骤序列]\n" + "\n".join(_step_line(t) for t in transitions)))
    parts.extend(sequence_parts(record))
    return parts


def _call_request(stage: QualityStage, call) -> CapacityRequest:
    """从冻结实际调用构造完整提示词，不使用记录槽位裁剪。

    @param stage 质量阶段实例
    @param call 冻结的成对或逐条调用
    @return 与实际发送同源的请求
    """
    from labelkit.operators.quality import _build_pointwise_prompt

    if isinstance(call, PairwiseQualityCall):
        prompt = stage._pairwise_prompt(call, None)
        empty_call = replace(call, record_a=replace(call.record_a, members=()),
                             record_b=replace(call.record_b, members=()),
                             transitions_a=() if call.transitions_a is not None else None,
                             transitions_b=() if call.transitions_b is not None else None)
        fixed = stage._pairwise_prompt(empty_call, None)
        schema = judgment_schema([criterion.key for criterion in call.criteria], call.with_reason)
    else:
        prompt = _build_pointwise_prompt(call.record, call.criterion, None, call.transitions)
        fixed = _build_pointwise_prompt(replace(call.record, members=()), call.criterion, None,
                                        () if call.transitions is not None else None)
        schema = pointwise_schema(call.criterion.key)
    return CapacityRequest(call.profile, prompt, schema, fixed)


async def run_complete_call(stage: QualityStage, call, ctx: RunContext):
    """完整请求只能成功或返回原始失败，不执行图像、文本或步骤降级。

    @param stage 质量阶段实例
    @param call 冻结调用
    @param ctx 当前会话尝试
    @return 成功评估或待声明序归并的原始失败
    """
    request = _call_request(stage, call)
    error = request_overflow(request, ctx)
    if error is not None:
        return QualityCallFailure(error, "call", precheck=True)
    ids = stage._pairwise_ids(call) if isinstance(call, PairwiseQualityCall) else (call.record.id,)
    try:
        obj, _usage, _attempts, model = await ctx.schema_engine.complete_validated(
            request.profile, request.prompt, request.schema,
            scope=CallScope(record_ids=ids, batch_no=ctx.batch_no, complete_evidence=ctx.session_attempt is not None),
        )
    except (CircuitBreakerTripped, ProviderFatalError, KeyboardInterrupt, asyncio.CancelledError):
        raise
    except Exception as exc:
        _logger.warning("complete quality request failed: profile=%s error=%s", request.profile, type(exc).__name__)
        return QualityCallFailure(exc, "call")
    if isinstance(call, PairwiseQualityCall):
        return stage._pairwise_success(obj, [criterion.key for criterion in call.criteria], model)
    entry = obj["scores"][0]
    return PointwiseQualityOutcome(int(entry["score"]), entry.get("reason", ""))


def wave_failures(stage, pools, planned, outcomes, ctx):
    """收集完整质量波次，计划错误与调用错误按池声明序合并。

    @param stage 当前质量阶段。
    @param pools 冻结比较池。
    @param planned 调用计划与同步计划异常。
    @param outcomes 全部模型叶结果。
    @param ctx 当前会话尝试。
    @return 保留实际双方目标的容量事实元组。
    """
    if ctx.session_attempt is None:
        return ()
    plans, errors = planned
    failures = []
    for pool_index, pool in enumerate(pools):
        if pool_index in errors:
            targets = tuple(capacity_target(item) for item in pool.items)
            failures.extend(capacity_failures(ctx, targets, errors[pool_index], "fixed"))
        for plan, outcome in zip(plans, outcomes):
            if plan.pool_ordinal != pool_index or not isinstance(outcome, QualityCallFailure):
                continue
            indices = ((plan.a_item_ordinal, plan.b_item_ordinal) if isinstance(plan, PairwiseQualityCall)
                       else (plan.item_ordinal,))
            targets = tuple(capacity_target(pool.items[index]) for index in indices)
            unit = capacity_unit(stage, plan, ctx) if isinstance(outcome.error, ContextOverflowError) else "sequence"
            failures.extend(capacity_failures(ctx, targets, outcome.error, unit))
    return tuple(failures)


def capacity_unit(stage: QualityStage, call, ctx: RunContext) -> str:
    """@param stage 质量阶段。@param call 实际调用。@param ctx 冻结预算。@return 准确最小单位。"""
    unit = "pairwise" if isinstance(call, PairwiseQualityCall) else "sequence"
    return request_unit(_call_request(stage, call), ctx, unit)


def preview_capacity(stage: QualityStage, item: PipelineItem, ctx: RunContext):
    """对全部可达类别和评委预览完整请求，未知比较对象以同等成员量计量。

    @param stage 质量阶段实例
    @param item 候选序列
    @param ctx 冻结会话上下文
    @return 首个容量失败，不抽随机数、不建立比较池
    """
    cfg = stage.cfg
    labels = ((item.classification.label,) if item.classification else
              tuple(cfg.class_views) if cfg.classify.enabled else (None,))
    for label in labels:
        view = cfg.class_views[label] if label is not None else cfg
        if not view.quality.enabled:
            continue
        target = replace(capacity_target(item), label=label)
        for request in _preview_requests(stage, item, view):
            failure = preview_failure(ctx, (target,), "sequence", request)
            if failure is not None:
                return failure
    return None


def _preview_requests(stage: QualityStage, item: PipelineItem, view):
    """保持配置声明序产出实际完整模板的预算请求。

    @param stage 质量阶段实例
    @param item 候选完整序列
    @param view 当前类别有效配置
    @return 完整请求迭代器
    """
    from labelkit.operators.quality import _Comparison, _build_pairwise_prompt, _build_pointwise_prompt

    criteria = view.rubric.criteria
    quality = view.quality
    if quality.mode == "pairwise":
        reason = stage._reasons_effective()
        pair = _Comparison(item.record, item.record, item.transitions, item.transitions)
        empty_pair = _Comparison(replace(item.record, members=()), replace(item.record, members=()),
                                 () if item.transitions is not None else None,
                                 () if item.transitions is not None else None)
        groups = (criteria,) if quality.criteria_per_call == "all" else tuple((criterion,) for criterion in criteria)
        for group in groups:
            prompt = _build_pairwise_prompt(pair, group, reason, None)
            fixed = _build_pairwise_prompt(empty_pair, group, reason, None)
            schema = judgment_schema([criterion.key for criterion in group], reason)
            for profile in quality.judges or (quality.llm,):
                yield CapacityRequest(profile, prompt, schema, fixed)
    else:
        for criterion in criteria:
            prompt = _build_pointwise_prompt(item.record, criterion, None, item.transitions)
            fixed = _build_pointwise_prompt(replace(item.record, members=()), criterion, None,
                                            () if item.transitions is not None else None)
            yield CapacityRequest(quality.llm, prompt, pointwise_schema(criterion.key), fixed)

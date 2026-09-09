"""标注组件的完整序列证据与纯请求容量预览。"""
from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

from labelkit.common.contracts.sequence_capacity import capacity_failures, capacity_target, member_key
from labelkit.common.errors import ContextOverflowError
from labelkit.common.inference.llm_client import Part
from labelkit.common.inference.schema_engine import _thaw_json
from labelkit.common.inference.sequence_evidence import CapacityRequest, preview_failure, request_unit, sequence_parts

if TYPE_CHECKING:
    from labelkit.common.contracts.stage import RunContext
    from labelkit.common.contracts.types import PipelineItem, Record
    from labelkit.operators.annotate import AnnotatePromptOptions, AnnotateStage


def sequence_wave_failures(spans, outcomes, ctx):
    """在任何标注归并前收齐同步计划错误和整波样本错误。

    @param spans 信封声明序切片。
    @param outcomes 扁平样本结果。
    @param ctx 当前 owning stage。
    @return 保留声明序的容量事实元组。
    """
    from labelkit.operators.annotation_finalization import class_schema_text

    failures = []
    for span in spans:
        errors = (span.error,) if span.error is not None else tuple(
            result.error for result in outcomes[span.start:span.start + span.count])
        for error in errors:
            unit = "sequence"
            if ctx.session_attempt is not None and isinstance(error, ContextOverflowError):
                request = sequence_request(span.item.record, ctx, class_schema_text(ctx, span.options.label),
                                           span.options)
                unit = request_unit(request, ctx, unit)
            failures.extend(capacity_failures(ctx, (capacity_target(span.item),), error, unit))
    return tuple(failures)


def frame_wave_failures(stage, plans, outcomes, ctx):
    """在任何成员字典写入前收齐本波全部帧容量失败。

    @param stage 帧目标工厂所属阶段。
    @param plans 含跳过成员的声明序计划。
    @param outcomes 实际帧调用结果。
    @param ctx 当前 owning stage。
    @return 全部帧容量事实。
    """
    failures = []
    for plan in plans:
        if not plan.skipped:
            error = outcomes[plan.call_index].error
            failures.extend(capacity_failures(ctx, (stage._frame_target(plan),), error, "frame"))
    return tuple(failures)


def complete_parts(record: Record, options: AnnotatePromptOptions) -> tuple[Part, ...]:
    """保留全部已有动作和所有成员证据，不提供关键帧或文本裁剪档位。

    @param record 当前完整序列
    @param options 当前标注的既有派生产物
    @return 末部件为文本的完整证据部件
    """
    from labelkit.operators.annotate import _step_line

    parts = []
    if options.transitions is not None:
        text = "[动作序列]\n" + "\n".join(_step_line(transition) for transition in options.transitions)
        parts.append(Part(kind="text", text=text))
    parts.extend(sequence_parts(record))
    return tuple(parts)


def sequence_request(record: Record, ctx: RunContext, schema_text: str,
                     options: AnnotatePromptOptions) -> CapacityRequest:
    """构造与实际标注完全相同的模型 Schema 和完整提示词。

    @param record 当前完整序列
    @param ctx 当前尝试上下文
    @param schema_text 已投影的模型 Schema 文本
    @param options 当前标注与修复参数
    @return 完整实际请求及其固定开销
    """
    from labelkit.operators.annotate import build_annotate_prompt, class_effective_model_schema

    prompt = build_annotate_prompt(record, ctx.cfg, schema_text, options)
    schema = class_effective_model_schema(ctx.cfg, options.label)
    fixed_options = replace(options, transitions=() if options.transitions is not None else None)
    fixed = build_annotate_prompt(replace(record, members=()), ctx.cfg, schema_text, fixed_options)
    return CapacityRequest(ctx.cfg.annotate.llm, prompt, schema, fixed)


def preview_capacity(stage: AnnotateStage, item: PipelineItem, ctx: RunContext):
    """预览全部可达标注类别和帧类别，不执行后处理或访问模型。

    @param stage 当前标注阶段实例
    @param item 候选完整序列
    @param ctx 冻结会话上下文
    @return 首个完整请求容量失败
    """
    from labelkit.operators.annotate import AnnotatePromptOptions, class_schema_text

    cfg = stage.cfg
    if cfg.annotate.enabled:
        labels = ((item.classification.label,) if item.classification else
                  tuple(cfg.class_views) if cfg.classify.enabled else (None,))
        for label in labels:
            options = AnnotatePromptOptions(label=label, transitions=item.transitions)
            request = sequence_request(item.record, ctx, class_schema_text(ctx, label), options)
            target = replace(capacity_target(item), label=label)
            failure = preview_failure(ctx, (target,), "sequence", request)
            if failure is not None:
                return failure
    return _preview_frames(item, ctx) if cfg.frame_annotate.enabled else None


def _preview_frames(item: PipelineItem, ctx: RunContext):
    """按完整成员出现位置和可达帧类预览最小帧请求。

    @param item 当前完整序列
    @param ctx 冻结会话上下文
    @return 首个帧请求容量失败
    """
    from labelkit.operators.annotate import build_frame_annotate_prompt

    cfg = ctx.cfg
    schema = _thaw_json(cfg.model_frame_schema)
    schema_text = json.dumps(schema, ensure_ascii=False, separators=(", ", ": "))
    for index, member in enumerate(item.record.members):
        classification = (item.member_classifications or {}).get(member_key(item, index))
        labels = ((classification.label,) if classification else
                  tuple(cfg.frame_class_views) if cfg.frame_classify.enabled else (None,))
        for label in labels:
            if label is not None and not cfg.frame_class_views[label].enabled:
                continue
            prompt = build_frame_annotate_prompt(member, cfg, schema_text, label)
            request = CapacityRequest(cfg.frame_annotate.llm, prompt, schema)
            target = replace(capacity_target(item, item.member_positions[index:index + 1]), label=label)
            failure = preview_failure(ctx, (target,), "frame", request)
            if failure is not None:
                return failure
    return None

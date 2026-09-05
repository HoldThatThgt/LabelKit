"""标注候选的模型 Schema 取值、工程后处理与框架时间定稿。"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from labelkit.common.config._temporal import inject_temporal_values, project_temporal_instance
from labelkit.common.contracts.generation import SequenceTemporalContext
from labelkit.common.contracts.types import Record
from labelkit.common.errors import InternalError
from labelkit.common.extensions.postprocessing import (
    invoke_postprocessor,
    project_postprocessor_instance,
)
from labelkit.common.inference.schema_engine import _thaw_json

if TYPE_CHECKING:
    from labelkit.common.config.model import ResolvedConfig, TimeBindingSpec
    from labelkit.common.contracts.stage import RunContext
    from labelkit.common.extensions.hooks import ResolvedHook


_logger = logging.getLogger("labelkit.annotate")


@dataclass(frozen=True)
class AnnotationFinalizer:
    """一次标注候选的工程后处理与框架时间定稿器。"""

    postprocessor: "ResolvedHook | None"            # 当前类生效的工程后处理函数
    record: Mapping | None                          # 工程函数收到的原始记录；sequence 为 None
    time_paths: tuple[str, ...]                     # 完整 Schema 中的框架时间路径
    time_values: tuple[tuple[str, object], ...]     # 路径到权威框架时间值
    temporal_context: SequenceTemporalContext | None  # 本次调用冻结的时间上下文

    def __call__(self, candidate: Mapping[str, object]) -> Mapping[str, object]:
        """运行工程函数后注入框架时间，并拒绝工程函数生成时间字段。

        @param candidate 已通过模型 Schema 的候选副本。
        @return 完成工程字段、普通字段规范化与框架时间注入的对象。
        """
        finalized = candidate
        if self.postprocessor is not None:
            finalized = invoke_postprocessor(self.postprocessor, candidate, self.record)
        projected = project_temporal_instance(finalized, self.time_paths)
        if _canonical_mapping(projected) != _canonical_mapping(finalized):
            raise ValueError("annotation candidate contains a business time field")
        output = inject_temporal_values(finalized, dict(self.time_values))
        if _canonical_mapping(project_temporal_instance(output, self.time_paths)) \
                != _canonical_mapping(finalized):
            raise ValueError("annotation finalizer changed a non-time field")
        return output


@dataclass(frozen=True)
class AnnotationProjector:
    """把完整标注投影回模型可生成的候选空间。"""

    schema: Mapping                                # 含代码负责字段的完整标注 Schema
    time_paths: tuple[str, ...]                    # 需删除的框架时间路径

    def __call__(self, candidate: Mapping[str, object]) -> Mapping[str, object]:
        """删除代码负责字段与框架时间，不创建或替换父节点。

        @param candidate 完整或部分完成的标注对象。
        @return 与模型 Schema 对齐的独立副本。
        """
        projected = project_temporal_instance(candidate, self.time_paths)
        return project_postprocessor_instance(projected, self.schema)


def class_annotate_schema(cfg: "ResolvedConfig", label: str | None) -> Mapping | None:
    """读取记录类覆盖的完整标注 Schema。

    @param cfg 已解析配置。
    @param label 记录分类标签。
    @return 类 Schema；无标签、未知类或无覆盖时为 None。
    """
    if label is None:
        return None
    view = cfg.class_views.get(label)
    return None if view is None else view.schema


def class_effective_schema(cfg: "ResolvedConfig", label: str | None) -> Mapping:
    """读取记录类最终交付使用的完整标注 Schema。

    @param cfg 已解析配置。
    @param label 记录分类标签。
    @return 类覆盖或全局完整 Schema。
    """
    override = class_annotate_schema(cfg, label)
    return cfg.user_schema if override is None else override


def class_effective_model_schema(cfg: "ResolvedConfig", label: str | None) -> Mapping:
    """读取记录类发送给模型的标注 Schema。

    @param cfg 已解析配置。
    @param label 记录分类标签。
    @return 排除代码负责字段与框架时间字段的有效 Schema。
    """
    view = _class_view(cfg, label)
    if view is not None:
        if view.model_schema is None:
            annotation_contract_error("annotation model schema is missing")
        return view.model_schema
    return cfg.model_user_schema


def class_schema_text(ctx: "RunContext", label: str | None) -> str:
    """渲染提示词使用的记录类模型 Schema。

    @param ctx 运行上下文。
    @param label 记录分类标签。
    @return provider-facing Schema 文本。
    """
    model_schema = class_effective_model_schema(ctx.cfg, label)
    override = class_annotate_schema(ctx.cfg, label)
    if override is None and model_schema == ctx.cfg.user_schema:
        return ctx.schema_engine.user_schema_text
    return json.dumps(_thaw_json(model_schema), ensure_ascii=False, separators=(", ", ": "))


def record_postprocessor(cfg: "ResolvedConfig", label: str | None) -> "ResolvedHook | None":
    """读取记录类继承完成后的工程后处理函数。

    @param cfg 已解析配置。
    @param label 记录分类标签。
    @return 类或全局生效的冻结函数。
    """
    view = _class_view(cfg, label)
    return cfg.annotate.resolved_postprocessor if view is None else view.annotate.resolved_postprocessor


def frame_postprocessor(cfg: "ResolvedConfig", label: str | None) -> "ResolvedHook | None":
    """读取帧类继承完成后的工程后处理函数。

    @param cfg 已解析配置。
    @param label 成员帧分类标签。
    @return 帧类或全局生效的冻结函数。
    """
    view = cfg.frame_class_views.get(label) if label is not None else None
    return cfg.frame_annotate.resolved_postprocessor if view is None else view.resolved_postprocessor


def annotation_record_context(record: Record) -> Mapping | None:
    """取得工程函数与记录级 validator 可见的原始记录。

    @param record 当前记录或成员帧。
    @return 普通记录的 raw；sequence 恒为 None。
    """
    return None if record.kind == "sequence" else record.raw


def annotation_transforms(
    record: Record,
    cfg: "ResolvedConfig",
    label: str | None,
    temporal_context: SequenceTemporalContext | None,
) -> tuple[AnnotationFinalizer, AnnotationProjector]:
    """构造一条记录调用共享的定稿器与模型空间投影器。

    @param record 当前记录。
    @param cfg 已解析配置。
    @param label 记录分类标签。
    @param temporal_context sequence 的冻结时间上下文。
    @return 使用同一完整 Schema 与时间路径的变换对。
    """
    view = _class_view(cfg, label)
    paths = () if view is None else view.business_time_paths
    context, values = _annotation_time_values(record, cfg, label, temporal_context)
    finalizer = AnnotationFinalizer(
        record_postprocessor(cfg, label), annotation_record_context(record),
        paths, tuple(values.items()), context,
    )
    return finalizer, AnnotationProjector(class_effective_schema(cfg, label), paths)


def frame_annotation_transforms(
    member: Record, cfg: "ResolvedConfig", label: str | None,
) -> tuple[AnnotationFinalizer, AnnotationProjector]:
    """构造一次成员帧标注的后处理变换对。

    @param member 当前成员帧。
    @param cfg 已解析配置。
    @param label 成员帧分类标签。
    @return 无框架时间路径的定稿器与投影器。
    """
    if cfg.frame_schema is None:
        annotation_contract_error("frame annotation schema is missing")
    finalizer = AnnotationFinalizer(
        frame_postprocessor(cfg, label), annotation_record_context(member), (), (), None,
    )
    return finalizer, AnnotationProjector(cfg.frame_schema, ())


def project_annotation_instance(
    value: Mapping, cfg: "ResolvedConfig", label: str | None,
) -> dict:
    """把已有完整标注投影为 verify 修复提示词的模型对象。

    @param value 已有完整标注对象。
    @param cfg 已解析配置。
    @param label 记录分类标签。
    @return 删除代码负责字段与框架时间的独立副本。
    """
    view = _class_view(cfg, label)
    paths = () if view is None else view.business_time_paths
    projected = project_temporal_instance(value, paths)
    return project_postprocessor_instance(projected, class_effective_schema(cfg, label))


def verify_temporal_annotation(
    output: Mapping[str, object], record: Record, cfg: "ResolvedConfig",
    label: str | None, temporal_context: SequenceTemporalContext | None,
) -> None:
    """确认自洽选择没有替换框架时间上下文。

    @param output 已完整验证的候选。
    @param record 当前 sequence 记录。
    @param cfg 已解析配置。
    @param label 记录分类标签。
    @param temporal_context 本次调用冻结的时间上下文。
    """
    view = _class_view(cfg, label)
    if view is None or not view.time_bindings:
        return
    context, values = _annotation_time_values(record, cfg, label, temporal_context)
    model = project_temporal_instance(output, view.business_time_paths)
    finalizer = AnnotationFinalizer(None, None, view.business_time_paths,
                                    tuple(values.items()), context)
    try:
        expected = finalizer(model)
    except ValueError:
        annotation_contract_error("annotation vote temporal finalization failed")
    if _canonical_mapping(expected) != _canonical_mapping(output):
        annotation_contract_error("annotation vote replaced its temporal context")


def annotation_contract_error(reason: str) -> None:
    """把标注定稿不变量破坏升级为脱敏内部错误。

    @param reason 固定、无业务数据的错误原因。
    @raises InternalError 始终抛出。
    """
    _logger.error("generation_downstream_contract: %s", reason)
    raise InternalError(f"generation_downstream_contract: {reason}")


def _class_view(cfg: "ResolvedConfig", label: str | None):
    """读取标签的冻结类视图；非法标签不猜测。"""
    return None if label is None else cfg.class_views.get(label)


def _annotation_time_values(
    record: Record, cfg: "ResolvedConfig", label: str | None,
    context: SequenceTemporalContext | None,
) -> tuple[SequenceTemporalContext | None, dict[str, object]]:
    """从同一冻结时间上下文解析 annotation 机械值。"""
    view = _class_view(cfg, label)
    if view is None or not view.time_bindings:
        return None, {}
    if not isinstance(context, SequenceTemporalContext) or record.kind != "sequence":
        annotation_contract_error("annotation temporal context is missing")
    expected = tuple(member.id for member in record.members)
    if tuple(member.event_id for member in context.members) != expected:
        annotation_contract_error("annotation temporal context differs from sequence members")
    values = {
        binding.payload_path: _first_resource_start(context, binding)
        for binding in view.time_bindings
    }
    if tuple(values) != view.business_time_paths:
        annotation_contract_error("annotation binding order differs from its Schema paths")
    return context, values


def _first_resource_start(context: SequenceTemporalContext, binding: "TimeBindingSpec") -> int:
    """读取一个资源的最早正区间毫秒起点。"""
    if binding.source != "first_resource_start_milliseconds" or not binding.resource:
        annotation_contract_error("annotation time binding source is invalid")
    starts: list[int] = []
    for member in context.members:
        valid = (isinstance(member.timestamp_us, int) and not isinstance(member.timestamp_us, bool)
                 and isinstance(member.duration_us, int) and not isinstance(member.duration_us, bool)
                 and member.timestamp_us % 1000 == 0 and member.duration_us >= 0
                 and member.duration_us % 1000 == 0)
        if not valid:
            annotation_contract_error("annotation temporal context contains an invalid interval")
        if binding.resource in member.resources:
            if member.duration_us == 0:
                annotation_contract_error("annotation resource interval is not positive")
            starts.append(member.timestamp_us)
    if not starts:
        annotation_contract_error("annotation temporal context lacks its resource interval")
    return min(starts) // 1000


def _canonical_mapping(value: Mapping[str, object]) -> str:
    """把 mapping 转为稳定字节等价文本。"""
    return json.dumps(_thaw_json(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))

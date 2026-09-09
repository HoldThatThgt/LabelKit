"""处理模式序列的完整证据、无副作用预算预览与容量控制信号。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

from labelkit.common.contracts.sequence_capacity import (
    CapacityTarget, SessionCapacityFailure, terminal_capacity_failure,
)
from labelkit.common.contracts.types import Record
from labelkit.common.errors import ContextOverflowError
from labelkit.common.inference import budget
from labelkit.common.inference.llm_client import Part, PromptBundle
from labelkit.common.inference.schema_engine import _thaw_json

if TYPE_CHECKING:
    from labelkit.common.contracts.stage import RunContext

@dataclass(frozen=True)
class CapacityRequest:
    """完整实际请求及可独立检查的固定开销。"""

    profile: str                         # 本次请求使用的配置名称
    prompt: PromptBundle                 # 含完整成员证据的实际提示词
    schema: Mapping | None = None        # 实际上行的模型 Schema
    fixed_prompt: PromptBundle | None = None  # 不含可切分成员证据的固定提示词


def record_evidence(record: Record) -> str:
    """渲染一个成员的完整文本或完整可见规范化树。

    @param record 不可变成员记录
    @return 未经摘要或字符裁剪的正文
    """
    if record.modality == "text":
        return record.text or ""
    return record.ui_tree.serialize(max_chars=None) if record.ui_tree is not None else ""


def sequence_parts(record: Record, heading: str = "[序列成员]") -> tuple[Part, ...]:
    """按成员顺序渲染全部证据，图片保持惰性引用，末部件恒为文本。

    @param record 序列记录
    @param heading 当前记录的段落标签
    @return 完整成员文字与全部图片部件
    """
    parts = [Part(kind="text", text=heading)]
    for index, member in enumerate(record.members, start=1):
        if member.image is not None:
            parts.append(Part(kind="text", text=f"[成员 {index} 截图]"))
            parts.append(Part(kind="image", image=member.image))
        parts.append(Part(kind="text", text=f"[成员 {index}]\n{record_evidence(member)}"))
    return tuple(parts)


def request_overflow(request: CapacityRequest, ctx: RunContext) -> ContextOverflowError | None:
    """用真实请求同源的估算器和会话校准快照做纯预算检查。

    @param request 待检查的完整请求
    @param ctx 当前运行上下文
    @return 超限异常或空值；不发送请求、不加载图片、不改指标
    """
    profile = ctx.cfg.llm_profiles[request.profile]
    if profile.context_window <= 0:
        return None
    has_images = any(part.kind == "image" for message in request.prompt.messages for part in message.parts)
    image_cost = ctx.llm.calibrator.cost(profile.name) if has_images else 0
    schema = _thaw_json(request.schema) if profile.supports_structured_output and request.schema is not None else None
    estimate = budget.est_prompt(request.prompt, profile, schema, image_cost=image_cost)
    if estimate <= budget.input_budget(profile):
        return None
    return ContextOverflowError(
        "complete sequence evidence exceeds the input context budget",
        phase="precheck", profile=request.profile,
    )


def preview_failure(ctx: RunContext, targets: tuple[CapacityTarget, ...], unit: str,
                    request: CapacityRequest) -> SessionCapacityFailure | None:
    """检查完整请求并把无法通过成员拆分减少的开销归为固定请求。

    @param ctx 当前运行上下文，scope.stage 是实际所有者
    @param targets 请求涉及的成员和分类视图
    @param unit 请求最小单位
    @param request 完整提示词、固定提示词与 Schema
    @return 首个发送前容量失败；可装入时为空值
    """
    error = request_overflow(request, ctx)
    if error is None:
        return None
    unit = request_unit(request, ctx, unit)
    scope = ctx.session_attempt
    return SessionCapacityFailure(stage=scope.stage, targets=targets, unit=unit, error=error)


def request_unit(request: CapacityRequest, ctx: RunContext, unit: str) -> str:
    """区分序列固定开销，保留帧和相邻对已有的最小失败归属。

    @param request 完整实际请求及固定开销
    @param ctx 当前冻结预算上下文
    @param unit 原请求的完整证据单位
    @return 不可依靠成员分区减少的序列开销归固定，其余保持原单位
    """
    if unit in ("sequence", "pairwise") and request.fixed_prompt is not None:
        fixed = CapacityRequest(request.profile, request.fixed_prompt, request.schema)
        if request_overflow(fixed, ctx) is not None:
            return "fixed"
    return unit


def terminal_error(ctx: RunContext, target: CapacityTarget, unit: str,
                   profile: str) -> ContextOverflowError | None:
    """在实际请求发出前按阶段、配置、视图、血缘和出现位置重放最小失败。

    @param ctx 当前会话尝试
    @param target 当前实际请求的归属
    @param unit 当前请求最小单位
    @param profile 当前模型配置名称
    @return 已冻结的原始异常；未命中时为空值
    """
    failure = terminal_capacity_failure(ctx, target, unit, profile)
    return failure.error if failure is not None else None

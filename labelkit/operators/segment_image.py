"""M14 图片模式：逐样本生成候选区域（CONTRACTS §7.20，SPEC-image-region-segmentation §10）。

每个样本先在阶段内同步读取画布、调用一次 box_parser 并冻结参考框。写了 instruction 时，其余 active
记录各提交一个模型任务；没写时（D15）参考框直接成为矩形候选，这条路径完全不碰 runtime。失败都是记录级，
没有 on_error 键，也不沿用流模式「非预算错误一律 segmentation_invalid」的回落。
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import warnings
from typing import TYPE_CHECKING

from labelkit.common.contracts.execution import TaskGroupRequest, TaskSpec
from labelkit.common.contracts.post_validation import PostValidatedCallRequest, ValidatedPostCall
from labelkit.common.contracts.regions import ReferenceBox, RegionSet, render_reference_boxes
from labelkit.common.contracts.types import ImageRef, PipelineItem, Record, StageError, Usage
from labelkit.common.errors import (
    CircuitBreakerTripped,
    ErrorKind,
    InternalError,
    ProviderFatalError,
    ProviderRetryableError,
    SchemaViolation,
)
from labelkit.common.extensions.box_parser import invoke_box_parser
from labelkit.common.inference import budget as budget_mod
from labelkit.common.inference.llm_client import Message, Part, PromptBundle
from labelkit.common.inference.schema_engine import CallScope, segment_image_schema
from labelkit.common.inference.sequence_evidence import CapacityRequest, request_overflow
from labelkit.common.observability.obslog import EV_ERROR, EV_SEGMENT_REGIONS
from labelkit.operators.segment_geometry import boxes_as_regions, validate_regions

if TYPE_CHECKING:
    from labelkit.common.config.model import ResolvedConfig
    from labelkit.common.contracts.stage import RunContext

_logger = logging.getLogger("labelkit.segment")

# 中文提示词逐字取自 CONTRACTS §10.20；TEMPLATE_HEAD_TOKENS["segment_image"] == est_text(_SYSTEM_STATIC)。
_SYSTEM_STATIC = "\n".join((
    "你负责为图片生成候选区域。坐标使用 0–1000 整数网格：x 是相对图片宽度的千分位，y 是相对图片高度的千分位，原点在左上角。",
    "每个区域由一个或多个多边形组成；每个多边形给出闭合的外环（至少 4 个点，最后一个点与第一个点相同），"
    "可以给出同样闭合的内环表示孔洞。同一区域的多个多边形之间不能重叠，也不能共用边；相邻的部分请合并成一个多边形。"
    "孔洞不能与外环共用边。不要输出区域编号。",
    "提供了参考框时，每个区域二选一：复用一个参考框时只填 box_id，polygons 与 source_box_ids 都填 null；"
    "画新多边形时 box_id 填 null，可以在 source_box_ids 中列出参考了哪些框（不要重复）。同一个参考框最多复用一次。"
    "未提供参考框时 box_id 与 source_box_ids 都填 null。没有符合指令的区域时输出空列表。",
    "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：",
    '{"regions": [{"box_id": <参考框 box_id|null>, "polygons": [{"exterior": [[x, y], ...], '
    '"holes": [[[x, y], ...], ...]}, ...]|null, "source_box_ids": [<参考框 box_id>, ...]|null}, ...]}',
    "[分割指令]",
))
_LABEL_IMAGE = "[图片]"
_LABEL_BOXES = "[参考框]"
_NO_BOXES = "本样本没有参考框。"
_COMPANION_REJECTED = "box_parser rejected companion"
_TERMINAL_VALIDATOR_ERRORS = (["post_validator_invalid"], ["post_validator_exception"])

# M14 自有的计数器键（CONTRACTS §9.3，report.segment_image）。
_COUNTER_SAMPLES = "segment_image.samples"
_COUNTER_CALLS = "segment_image.calls"
_COUNTER_BOXES = "segment_image.boxes"
_COUNTER_REGIONS = "segment_image.regions"
_COUNTER_EMPTY = "segment_image.empty"
_COUNTER_FAILED_PREFIX = "segment_image.failed."
_COUNTER_PROMPT_TOKENS = "segment_image.prompt_tokens"
_COUNTER_COMPLETION_TOKENS = "segment_image.completion_tokens"


def build_segment_image_prompt(record: Record, canvas: RegionSet, instruction: str) -> PromptBundle:
    """确定性组装 CONTRACTS §10.20 的提示词；图片只放 user 消息。

    @param record 当前样本
    @param canvas 携带画布尺寸与冻结参考框的未分割载体
    @param instruction segment.instruction
    @return 可直接交给 M8 的提示词包
    """
    system = Message(role="system", parts=(Part(kind="text", text=f"{_SYSTEM_STATIC}\n{instruction}"),))
    boxes_text = f"{_LABEL_BOXES}\n{render_reference_boxes(canvas)}" if canvas.boxes else _NO_BOXES
    user = Message(role="user", parts=(Part(kind="text", text=_LABEL_IMAGE),
                                       Part(kind="image", image=record.image),
                                       Part(kind="text", text=boxes_text)))
    return PromptBundle(messages=(system, user))


def _read_canvas(image: ImageRef) -> tuple[int, int]:
    """读取图片文件头得到存储像素宽高，不解码像素，也不看方向标签（D4）。

    @param image 原图引用
    @return (width, height)
    @raises OSError 文件不可读；超限与 Pillow 的其他文件头异常都转成它，由 image_decode_error 归类
    """
    from PIL import Image  # 局部导入：与 ImageRef.load_base64 同一惯例

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(image.path) as im:
                return im.size
    except OSError:
        raise
    except Exception as exc:  # DecompressionBombError 不是 OSError 子类；残缺文件头的异常也不止 OSError
        raise OSError(f"image header unreadable: {type(exc).__name__}") from exc


def _classify_failure(exc: BaseException) -> tuple[str, str, bool]:
    """把分割异常映射为 §7.6 错误分类；预算词表先行。

    @param exc 捕获的异常
    @return (kind, message, retryable)
    """
    kind = budget_mod.classify_stage_error(exc)
    if kind is not None:
        return kind, str(exc), False
    if isinstance(exc, SchemaViolation):
        if exc.errors in _TERMINAL_VALIDATOR_ERRORS:
            return ErrorKind.INTERNAL_ERROR.value, exc.errors[0], False
        return ErrorKind.SEGMENTATION_INVALID.value, str(exc), False
    if isinstance(exc, ProviderRetryableError):
        return ErrorKind.PROVIDER_RETRYABLE_EXHAUSTED.value, str(exc), True
    if isinstance(exc, ProviderFatalError):
        return ErrorKind.PROVIDER_FATAL.value, str(exc), False
    if isinstance(exc, OSError):
        return ErrorKind.IMAGE_DECODE_ERROR.value, f"{type(exc).__name__}: {exc}", False
    return ErrorKind.INTERNAL_ERROR.value, f"{type(exc).__name__}: {exc}", False


@dataclasses.dataclass(frozen=True, slots=True)
class _Sample:
    """一个已冻结参考框、等待候选的样本。"""

    item: PipelineItem                     # 样本信封
    canvas: RegionSet                      # 画布尺寸与冻结参考框，regions 为空元组


class SegmentImageStage:
    """M14 图片模式算子：只写 item.regions，可置 failed，不重绑 Record、不追加信封。"""

    name = "segment"

    def __init__(self, cfg: "ResolvedConfig"):
        """构造图片分割阶段。

        @param cfg 已解析的不可变配置
        """
        self.cfg = cfg

    async def run(self, batch: list[PipelineItem], ctx: "RunContext") -> list[PipelineItem]:
        """为全部 active 记录生成候选。

        @param batch 本批次信封列表（只改信封字段与状态）
        @param ctx 运行上下文
        @return 与入参同一个列表对象
        """
        samples = self._prepare(batch, ctx)
        if not self.cfg.segment.instruction:
            for sample in samples:
                canvas = sample.canvas
                self._commit(sample, boxes_as_regions(canvas.boxes, canvas.width, canvas.height), 0, ctx)
        elif samples:
            await self._segment_with_model(samples, ctx)
        return batch

    def _prepare(self, batch: list[PipelineItem], ctx: "RunContext") -> list[_Sample]:
        """同步读取画布并调用钩子；失败的记录直接落 failed，不会进入任何模型任务。

        @param batch 本批次信封列表
        @param ctx 运行上下文
        @return 按批内顺序排列的待分割样本
        """
        samples: list[_Sample] = []
        for item in batch:
            if item.status != "active":
                continue
            ctx.metrics.count(_COUNTER_SAMPLES)
            try:
                width, height = _read_canvas(item.record.image)
                boxes = self._frozen_boxes(item.record, width, height)
            except Exception as exc:  # noqa: BLE001 — 记录级隔离绝对优先
                self._fail(item, ctx, _classify_failure(exc))
                continue
            if boxes is None:
                self._fail(item, ctx, (ErrorKind.COMPANION_INVALID.value, _COMPANION_REJECTED, False))
                continue
            samples.append(_Sample(item, RegionSet(width, height, boxes, ())))
        return samples

    def _frozen_boxes(self, record: Record, width: int, height: int) -> tuple[ReferenceBox, ...] | None:
        """调用一次钩子（若注册且有伴随文件）并冻结参考框。

        @param record 当前样本
        @param width 原图存储像素宽
        @param height 原图存储像素高
        @return 冻结参考框；没有钩子或伴随文件时为空元组；钩子拒收时为 None
        @raises BoxParserError 钩子缺陷，日志已由 invoke_box_parser 写出
        """
        hook = self.cfg.segment.resolved_box_parser
        if hook is None or record.companion is None:
            return ()
        return invoke_box_parser(hook, record.companion, width, height)

    async def _segment_with_model(self, samples: list[_Sample], ctx: "RunContext") -> None:
        """每个样本提交一个模型任务，结果按声明序归并。

        @param samples 待分割样本
        @param ctx 运行上下文
        """
        profile = self.cfg.segment.llm
        specs = tuple(
            TaskSpec(
                task_id=f"{ctx.task_namespace}:segment:{ordinal}",
                declaration_key=(ctx.batch_no, 0, ordinal),
                stage=self.name,
                resource_key=("llm", profile),
                operation=lambda sample=sample: self._call_model(sample, ctx),
            )
            for ordinal, sample in enumerate(samples)
        )
        ctx.metrics.count(_COUNTER_CALLS, len(specs))
        results = await ctx.run_group(TaskGroupRequest(specs))
        for sample, result in zip(samples, results):
            if isinstance(result, BaseException):
                self._settle_failure(sample.item, result, ctx)
            else:
                self._note_usage(result.usage, ctx)
                self._commit(sample, result.proof, result.attempts, ctx)

    async def _call_model(self, sample: _Sample, ctx: "RunContext") -> ValidatedPostCall | Exception:
        """叶任务：首轮预检后经通用后校验接缝调用模型，记录级错误作为值返回。

        @param sample 当前样本
        @param ctx 运行上下文
        @return 证明类型已确认的调用结果，或捕获到的失败异常
        """
        profile = self.cfg.segment.llm
        record, canvas = sample.item.record, sample.canvas
        prompt = build_segment_image_prompt(record, canvas, self.cfg.segment.instruction)
        schema = segment_image_schema()
        try:
            overflow = request_overflow(CapacityRequest(profile, prompt, schema), ctx)
            if overflow is not None:
                raise overflow
            result = await ctx.schema_engine.complete_post_validated(PostValidatedCallRequest(
                profile, prompt, schema, CallScope(record_ids=(record.id,), batch_no=ctx.batch_no),
                lambda candidate: validate_regions(candidate, canvas.boxes, canvas.width, canvas.height)))
            if not isinstance(result.proof, RegionSet):
                _logger.error("segment post-validation returned a proof that is not a RegionSet")
                raise InternalError("segment post-validation returned a proof that is not a RegionSet")
            return result
        except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 — 记录级隔离绝对优先
            return exc

    def _settle_failure(self, item: PipelineItem, exc: BaseException, ctx: "RunContext") -> None:
        """结算一次模型调用失败：恰一次喂熔断、计入已发生的用量，再落记录级失败。

        @param item 失败的信封
        @param exc 叶任务捕获的异常
        @param ctx 运行上下文
        """
        budget_mod.feed_reactive_terminal(exc, ctx.metrics)
        if isinstance(exc, SchemaViolation) and exc.usage is not None:
            self._note_usage(exc.usage, ctx)
        self._fail(item, ctx, _classify_failure(exc))

    def _commit(self, sample: _Sample, regions: RegionSet, attempts: int, ctx: "RunContext") -> None:
        """把候选写入信封并发出只含计数的 segment.regions 事件。

        @param sample 当前样本
        @param regions 同一冻结候选对象，后续阶段共用
        @param attempts 模型调用总次数；D15 路径为 0
        @param ctx 运行上下文
        """
        item = sample.item
        item.regions = regions
        total = len(regions.regions)
        reused = sum(1 for region in regions.regions if region.box_id is not None)
        ctx.metrics.count(_COUNTER_BOXES, len(regions.boxes))
        ctx.metrics.count(_COUNTER_REGIONS, total)
        if not regions.regions:
            ctx.metrics.count(_COUNTER_EMPTY)
        ctx.metrics.event(EV_SEGMENT_REGIONS, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(item.record.id,),
                          payload={"regions": total, "reused": reused, "drawn": total - reused,
                                   "width": regions.width, "height": regions.height, "attempts": attempts})

    @staticmethod
    def _note_usage(usage: Usage, ctx: "RunContext") -> None:
        """累加分割调用用量，用来拆分 segment 与 annotate 共用同一 profile 时的成本。

        @param usage 本样本的累计用量
        @param ctx 运行上下文
        """
        ctx.metrics.count(_COUNTER_PROMPT_TOKENS, usage.prompt_tokens)
        ctx.metrics.count(_COUNTER_COMPLETION_TOKENS, usage.completion_tokens)

    def _fail(self, item: PipelineItem, ctx: "RunContext", failure: tuple[str, str, bool]) -> None:
        """把信封落为记录级失败（写 item.errors、置 failed、发 error 事件）。

        @param item 失败的信封
        @param ctx 运行上下文
        @param failure (kind, message, retryable)
        """
        kind, message, retryable = failure
        _logger.warning("image segmentation failed: record=%s kind=%s", item.record.id, kind,
                        extra={"stage": self.name, "batch": ctx.batch_no})
        item.errors.append(StageError(stage=self.name, kind=kind, message=message, retryable=retryable))
        item.status = "failed"
        if kind == ErrorKind.CONTEXT_OVERFLOW.value:
            ctx.metrics.count("budget.overflow_records")  # V13②：每个拒收各计一次
        ctx.metrics.count(_COUNTER_FAILED_PREFIX + kind)
        ctx.metrics.event(EV_ERROR, stage=self.name, batch_no=ctx.batch_no, record_ids=(item.record.id,),
                          payload={"stage": self.name, "kind": kind, "message": message, "retryable": retryable})

"""M1 的 segment.mode 键面与图片模式组合规则（CONTRACTS §6.3 rule 72–80）。

只往 ``_Collector`` 记账，从不提前抛出。规则 77a 由 ``_warn_undeclared_windows`` 发出，77b、77c
由 ``_check_vision_profiles`` 发出；D9 对 rule 17、29 的放宽写在 ``_constraints.py`` 各自的簇里。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from labelkit.common.config._collect import _fmt
from labelkit.common.config.model import SegmentConfig
from labelkit.common.extensions.box_parser import resolve_box_parser

if TYPE_CHECKING:
    from labelkit.common.config._constraints import _LoadCtx, _Products

_SEQUENCE_KEYS = ("strategy", "window", "noise_filter", "min_len", "context", "on_error")
_IMAGE_KEYS = ("instruction", "box_parser", "companion_suffix")
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})


def image_mode(segment: SegmentConfig) -> bool:
    """判定图片模式是否生效；``enabled = false`` 时其余键都是停放配置。

    @param segment 已解析的 ``[segment]`` 节
    @return 开关开启且 ``mode = "image"`` 时为 True
    """
    return segment.enabled and segment.mode == "image"


def check_segment_image(ctx: _LoadCtx, products: _Products) -> None:
    """``segment.enabled = true`` 时按 mode 运行键面与组合规则。

    @param ctx 校验上下文
    @param products 产物累加器(解析成功的钩子写入 ``hooks["box_parser"]``)
    """
    segment = ctx.p.segment
    if segment.mode == "sequence":
        _check_sequence_keys(ctx)
    elif segment.mode == "image":
        _check_image_keys(ctx)
        _check_companion_keys(ctx)
        _resolve_hook(ctx, products)
        _check_image_stages(ctx)
        _check_image_sections(ctx)
    elif not ctx.p.segment_provided["mode"]:
        # 写了枚举外的取值时解析层已报错，这里不再报「缺键」。
        ctx.col.error(f'{ctx.fp}:[segment].mode: required when segment.enabled = true; '
                      f'expected "image" or "sequence"')


def _check_sequence_keys(ctx: _LoadCtx) -> None:
    """rule 73b：流模式显式书写图片模式键。

    @param ctx 校验上下文
    """
    for key in _IMAGE_KEYS:
        if ctx.p.segment_provided[key]:
            ctx.col.error(f'{ctx.fp}:[segment].{key}: not allowed when segment.mode = "sequence" '
                          f"(image segmentation key)")


def _check_image_keys(ctx: _LoadCtx) -> None:
    """rule 73a、73c、73d、74：图片模式的键面与候选来源。

    @param ctx 校验上下文
    """
    col, fp, provided = ctx.col, ctx.fp, ctx.p.segment_provided
    for key in _SEQUENCE_KEYS:
        if provided[key]:
            col.error(f'{fp}:[segment].{key}: not allowed when segment.mode = "image" '
                      f"(sequence segmentation key)")
    if provided["instruction"] and not ctx.p.segment.instruction.strip():
        col.error(f"{fp}:[segment].instruction: must be non-empty when set")
    if provided["llm"] and not provided["instruction"]:
        col.error(f'{fp}:[segment].llm: not allowed when segment.mode = "image" has no instruction '
                  f"(no segment model call is made)")
    if not provided["instruction"] and not provided["box_parser"]:
        col.error(f'{fp}:[segment]: segment.mode = "image" requires at least one of instruction '
                  f"and box_parser")


def _check_companion_keys(ctx: _LoadCtx) -> None:
    """rule 73e、73f：伴随文件后缀的成对出现与形态。

    @param ctx 校验上下文
    """
    col, fp, provided = ctx.col, ctx.fp, ctx.p.segment_provided
    suffix = ctx.p.segment.companion_suffix
    if provided["box_parser"] != provided["companion_suffix"]:
        col.error(f"{fp}:[segment].companion_suffix: box_parser and companion_suffix must be set together")
    if suffix is not None and not _suffix_ok(suffix):
        col.error(f'{fp}:[segment].companion_suffix: expected a suffix starting with "." that is '
                  f"not an image extension, got {_fmt(suffix)}")


def _suffix_ok(suffix: str) -> bool:
    """判定后缀形态：以点开头、不含路径分隔符、不是图片扩展名。

    @param suffix 配置中的后缀
    @return 形态合法时为 True；``.ocr.json`` 这样的多段后缀合法
    """
    return (suffix.startswith(".") and "/" not in suffix and "\\" not in suffix
            and suffix.lower() not in _IMAGE_SUFFIXES)


def _resolve_hook(ctx: _LoadCtx, products: _Products) -> None:
    """rule 73g、73h：加载并静态检查钩子，绝不在启动期调用它。

    @param ctx 校验上下文
    @param products 产物累加器
    """
    reference = ctx.p.segment.box_parser
    if reference is None:
        return
    try:
        products.hooks["box_parser"] = resolve_box_parser(reference, ctx.root)
    except ValueError as error:
        ctx.col.error(f"{ctx.fp}:[segment].box_parser: {error}")


def _check_image_stages(ctx: _LoadCtx) -> None:
    """rule 75、76：图片模式要求 UI 模态，并排除流家族阶段与 ``[stream]`` 节。

    @param ctx 校验上下文
    """
    col, fp, p = ctx.col, ctx.fp, ctx.p
    if ctx.modality != "ui":
        col.error(f'{fp}:[segment].mode: segment.mode = "image" requires run.modality = "ui", '
                  f"got {_fmt(ctx.modality)}")
    for section, enabled in (("stitch", p.stitch.enabled), ("extract", p.extract.enabled),
                             ("frame.classify", p.frame_classify.enabled),
                             ("frame.annotate", p.frame_annotate.enabled)):
        if enabled:
            col.error(f'{fp}:[{section}].enabled: {section}.enabled = true is not available when '
                      f'segment.mode = "image"')
    if p.stream_provided["section"]:
        col.error(f'{fp}:[stream]: the [stream] section is not available when segment.mode = "image"')


def _check_image_sections(ctx: _LoadCtx) -> None:
    """rule 78、79、80 与修复轮数告警：其他节在图片模式下的护栏。

    @param ctx 校验上下文
    """
    col, fp, p = ctx.col, ctx.fp, ctx.p
    if p.dedup.semantic:
        col.error(f'{fp}:[dedup].semantic: not available when segment.mode = "image" '
                  f"(image samples have no text to embed)")
    if p.input_provided["ui_tree_max_chars"]:
        col.error(f'{fp}:[input].ui_tree_max_chars: not allowed when segment.mode = "image" '
                  f"(image mode builds no UI tree)")
    if p.output.meta_mode == "none":
        col.error(f'{fp}:[output].meta_mode: must not be "none" when segment.mode = "image" - '
                  f"candidates and original polygons are carried only by _meta.regions "
                  f'(sidecar is legal), got "none"')
    if p.quality.enabled and not p.quality.rubric:
        col.error(f'{fp}:[quality].rubric: segment.mode = "image" has no default rubric; set '
                  f"quality.rubric or quality.enabled = false")
    if p.segment.llm_referenced and p.output.max_repair_attempts > 2:
        col.warn(f'{fp}:[output].max_repair_attempts: segment.mode = "image" repairs segmentation '
                 f"at most 2 rounds; larger values do not extend it")

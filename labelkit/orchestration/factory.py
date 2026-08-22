"""按配置装配 LabelKit 流水线的算子实例。"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from labelkit.common.config.model import ResolvedConfig
    from labelkit.common.contracts.stage import Stage

__all__ = ["build_stages"]


def build_stages(cfg: "ResolvedConfig") -> list["Stage"]:
    """按冻结的超集链序实例化已启用的算子。

    @param cfg: 已解析配置
    @return: 已启用的算子实例列表（链序由 M10 组链时再行确认）
    """
    from labelkit.operators.annotate import AnnotateStage
    from labelkit.operators.classify import ClassifyStage
    from labelkit.operators.dedup import DedupIndex, DedupStage
    from labelkit.operators.quality import QualityStage
    from labelkit.operators.verify import VerifyStage

    stages: list[Stage] = []
    if cfg.segment.enabled:
        from labelkit.operators.segment import SegmentStage

        stages.append(SegmentStage(cfg))
    if cfg.stitch.enabled:
        from labelkit.operators.stitch import StitchStage

        stages.append(StitchStage(cfg))
    if cfg.dedup.enabled:
        stages.append(DedupStage(cfg.dedup, DedupIndex(cfg.dedup, cfg.run.modality)))
    if cfg.generate.form != "sequence" and (cfg.classify.enabled or cfg.frame_classify.enabled):
        # v1.12 或门：仅帧级分类开启时 ClassifyStage 仍须进链承载帧 pass，
        # 序列级判决由 stage 内 classify.enabled 门静默跳过（SPEC §3.2）。
        stages.append(ClassifyStage(cfg))
    if cfg.extract.enabled:
        from labelkit.operators.extract import ExtractStage

        stages.append(ExtractStage(cfg))
    if cfg.quality.enabled:
        stages.append(QualityStage(cfg))
    if cfg.generate.enabled and cfg.generate.form != "sequence":
        from labelkit.operators.generate import GenerateStage

        stages.append(GenerateStage(cfg))
    if cfg.annotate.enabled or cfg.frame_annotate.enabled:
        stages.append(AnnotateStage(cfg))
    if cfg.verify.enabled:
        stages.append(VerifyStage(cfg))
    return stages

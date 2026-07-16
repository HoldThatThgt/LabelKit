"""Stage construction for the configured LabelKit pipeline."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from labelkit.common.config.model import ResolvedConfig
    from labelkit.common.contracts.stage import Stage

__all__ = ["build_stages"]


def build_stages(cfg: "ResolvedConfig") -> list["Stage"]:
    """Instantiate enabled operators in the frozen superset-chain order."""
    from labelkit.operators.annotate import AnnotateStage
    from labelkit.operators.classify import ClassifyStage
    from labelkit.operators.dedup import DedupIndex, DedupStage
    from labelkit.operators.generate import GenerateStage
    from labelkit.operators.quality import QualityStage
    from labelkit.operators.verify import VerifyStage

    stages: list[Stage] = []
    if cfg.segment.enabled:
        from labelkit.operators.segment import SegmentStage

        stages.append(SegmentStage(cfg))
    if cfg.dedup.enabled:
        stages.append(DedupStage(cfg.dedup, DedupIndex(cfg.dedup, cfg.run.modality)))
    if cfg.classify.enabled:
        stages.append(ClassifyStage(cfg))
    if cfg.extract.enabled:
        from labelkit.operators.extract import ExtractStage

        stages.append(ExtractStage(cfg))
    if cfg.quality.enabled:
        stages.append(QualityStage(cfg))
    if cfg.generate.enabled:
        stages.append(GenerateStage(cfg))
    if cfg.annotate.enabled:
        stages.append(AnnotateStage(cfg))
    if cfg.verify.enabled:
        stages.append(VerifyStage(cfg))
    return stages

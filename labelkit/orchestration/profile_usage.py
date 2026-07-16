"""Referenced-profile discovery for ``labelkit validate --probe``."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from labelkit.common.config.model import ResolvedConfig

__all__ = ["referenced_profiles"]


def referenced_profiles(cfg: "ResolvedConfig") -> tuple[list[str], list[str]]:
    """Return order-preserving, deduplicated LLM and embedding profile names."""
    llm_names: list[str] = []
    if cfg.segment.enabled and cfg.segment.strategy in ("llm", "hybrid"):
        llm_names.append(cfg.segment.llm)
    if cfg.classify.enabled:
        llm_names.append(cfg.classify.llm)
    if cfg.extract.enabled:
        llm_names.append(cfg.extract.llm)
    if cfg.quality.enabled:
        if cfg.quality.mode == "pointwise" or not cfg.quality.judges:
            llm_names.append(cfg.quality.llm)
        else:
            llm_names.extend(cfg.quality.judges)
    if cfg.annotate.enabled:
        llm_names.append(cfg.annotate.llm)
    if cfg.generate.enabled:
        llm_names.extend(cfg.generate.llms)
    if cfg.verify.enabled:
        if cfg.verify.judges:
            llm_names.extend(cfg.verify.judges)
        else:
            llm_names.append(cfg.verify.llm)
    if cfg.output.repair_llm:
        llm_names.append(cfg.output.repair_llm)

    emb_names: list[str] = []
    if cfg.dedup.enabled and cfg.dedup.semantic and cfg.dedup.semantic_embedding:
        emb_names.append(cfg.dedup.semantic_embedding)
    return list(dict.fromkeys(llm_names)), list(dict.fromkeys(emb_names))

"""Canonical shared contracts.

The implementation lives in the sibling ``types`` and ``stage`` modules; this
package-level export keeps the common contract namespace convenient to import.
"""
from .stage import RunContext, Stage
from .types import (
    Annotation,
    Classification,
    DedupInfo,
    ImageRef,
    PipelineItem,
    QualityScore,
    Record,
    RecordRef,
    StageError,
    Status,
    Transition,
    UINode,
    UITree,
    Usage,
    VerificationResult,
    digest_is_poor,
    frame_digest,
    tree_diff,
)

__all__ = [
    "Annotation",
    "Classification",
    "DedupInfo",
    "ImageRef",
    "PipelineItem",
    "QualityScore",
    "Record",
    "RecordRef",
    "RunContext",
    "Stage",
    "StageError",
    "Status",
    "Transition",
    "UINode",
    "UITree",
    "Usage",
    "VerificationResult",
    "digest_is_poor",
    "frame_digest",
    "tree_diff",
]

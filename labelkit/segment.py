"""Compatibility re-exports for :mod:`labelkit.operators.segment`."""

from labelkit.operators.segment import (
    SegmentStage,
    _logger,
    _reason_requested,
    _window_spans,
    build_segment_prompt,
    judge_window,
    render_tree_diff,
)

__all__ = [
    "SegmentStage",
    "build_segment_prompt",
    "judge_window",
    "render_tree_diff",
]

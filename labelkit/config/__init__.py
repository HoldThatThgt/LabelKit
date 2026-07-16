"""Compatibility exports for the canonical M1 configuration service."""
from __future__ import annotations

from typing import Any

from labelkit.common.config.model import ResolvedConfig

__all__ = ["load", "default_rubric", "ResolvedConfig"]


def __getattr__(name: str) -> Any:
    if name in ("load", "default_rubric"):
        from labelkit.common.config import loader
        return getattr(loader, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

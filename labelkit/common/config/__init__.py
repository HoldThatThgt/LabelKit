"""Config service (M1). Re-exports per CONTRACTS.md §1: load, default_rubric, ResolvedConfig.

`load` / `default_rubric` live in `labelkit.common.config.loader` (owned by M1) and are
re-exported lazily (PEP 562) so that importing `labelkit.common.config.model` never
requires loader.py — keeping the import graph acyclic.
"""
from __future__ import annotations

from typing import Any

from labelkit.common.config.model import ResolvedConfig

__all__ = ["load", "default_rubric", "ResolvedConfig"]


def __getattr__(name: str) -> Any:
    if name in ("load", "default_rubric"):
        from labelkit.common.config import loader
        return getattr(loader, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

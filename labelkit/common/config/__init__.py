"""配置服务（M1）。按 CONTRACTS.md §1 再导出：load、default_rubric、ResolvedConfig。

`load` / `default_rubric` 实现在 `labelkit.common.config.loader`（归 M1 所有），此处以
PEP 562 惰性再导出——这样导入 `labelkit.common.config.model` 永不牵连 loader.py，
从而保持导入图无环。
"""
from __future__ import annotations

from typing import Any

from labelkit.common.config.model import (
    CorrelationSpec,
    ResolvedConfig,
    SequenceRuleSpec,
    SequenceWindowSpec,
    effective_rules,
    effective_windows,
)

__all__ = ["load", "default_rubric", "ResolvedConfig", "CorrelationSpec",
           "SequenceRuleSpec", "SequenceWindowSpec", "effective_rules",
           "effective_windows"]


def __getattr__(name: str) -> Any:
    """惰性再导出 loader 侧符号（PEP 562），保持导入图无环。

    @param name 被访问的模块属性名
    @return `loader.load` / `loader.default_rubric` 对应的可调用对象
    @raises AttributeError 名字不在再导出白名单内
    """
    if name in ("load", "default_rubric"):
        from labelkit.common.config import loader
        return getattr(loader, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

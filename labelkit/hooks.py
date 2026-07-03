"""User validation hooks (spec 3.8.2 L2.5 / 3.6.2, v1.5 plan A).

A hook reference is a ``"module:function"`` string resolved with importlib.
Hooks run arbitrary user code with the same privileges as LabelKit itself —
the trust boundary is identical to the config files that name them.

Two hook shapes exist (both return a list of violation strings, empty = pass):

- ``output.validator``      — ``fn(obj: dict, record: Mapping | None) -> list[str]``
  wired into the schema engine as L2.5 for user-schema annotate calls.
- ``generate.sample_validator`` — ``fn(text: str) -> list[str]``
  a per-sample filter applied before the similarity filter.
"""
from __future__ import annotations

from importlib import import_module
from typing import Any, Callable


def resolve_hook(ref: str) -> Callable[..., Any]:
    """Resolve ``"module:function"`` to the callable. Raises ValueError with a
    user-facing message on bad format / import failure / non-callable — M1
    turns these into aggregated ConfigError lines."""
    module_name, sep, attr_path = ref.partition(":")
    if not sep or not module_name.strip() or not attr_path.strip():
        raise ValueError(f'期望 "module:function" 形式，得到 {ref!r}')
    try:
        obj: Any = import_module(module_name.strip())
    except Exception as exc:  # ImportError and anything the module raises on import
        raise ValueError(f"无法导入模块 {module_name.strip()!r}：{exc}") from exc
    for part in attr_path.strip().split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError:
            raise ValueError(
                f"模块 {module_name.strip()!r} 中找不到 {attr_path.strip()!r}") from None
    if not callable(obj):
        raise ValueError(f"{ref!r} 不是可调用对象（得到 {type(obj).__name__}）")
    return obj


def normalize_violations(result: Any, ref: str) -> list[str]:
    """Coerce a hook's return value into list[str]; a non-conforming return is
    itself a hook bug and must not pass silently."""
    if result is None:
        return []
    if isinstance(result, (list, tuple)):
        return [str(v) for v in result]
    raise TypeError(f"校验回调 {ref!r} 应返回 list[str]（空 = 通过），"
                    f"得到 {type(result).__name__}")

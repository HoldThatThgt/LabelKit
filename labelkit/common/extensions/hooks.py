"""用户校验回调钩子（spec 3.8.2 L2.5 / 3.6.2，v1.5 方案 A）。

钩子引用是一个 ``"module:function"`` 字符串，由 importlib 解析。钩子以运行者同权限
执行任意用户代码——信任边界与「配置文件里写下它」完全一致。

共两种钩子形态（均返回违规描述字符串列表，空列表 = 通过）：

- ``output.validator``          —— ``fn(obj: dict, record: Mapping | None) -> list[str]``，
  挂接为结构引擎的 L2.5，仅作用于用户 Schema 的标注调用。
- ``generate.sample_validator`` —— ``fn(text: str) -> list[str]``，
  在相似度过滤之前逐条过滤生成样本。
"""
from __future__ import annotations

from importlib import import_module
from typing import Any, Callable


def resolve_hook(ref: str) -> Callable[..., Any]:
    """把 ``"module:function"`` 引用解析成可调用对象。

    @param ref 钩子引用字符串；冒号右侧允许点号属性路径（如 ``pkg.mod:Cls.fn``）。
    @return 解析得到的可调用对象。
    @raises ValueError 格式非法 / 模块导入失败 / 属性不存在 / 目标不可调用；
            消息面向用户，M1 将其汇总成 ConfigError 行。
    """
    module_name, sep, attr_path = ref.partition(":")
    if not sep or not module_name.strip() or not attr_path.strip():
        raise ValueError(f'expected "module:function" form, got {ref!r}')
    try:
        obj: Any = import_module(module_name.strip())
    except Exception as exc:  # ImportError 以及模块导入期自身抛出的任何异常
        raise ValueError(f"cannot import module {module_name.strip()!r}: {exc}") from exc
    for part in attr_path.strip().split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError:
            raise ValueError(
                f"attribute {attr_path.strip()!r} not found in module "
                f"{module_name.strip()!r}") from None
    if not callable(obj):
        raise ValueError(f"{ref!r} is not callable (got {type(obj).__name__})")
    return obj


def normalize_violations(result: Any, ref: str) -> list[str]:
    """把钩子返回值规整成 ``list[str]``；不合约的返回值本身即钩子缺陷，不得静默放过。

    @param result 钩子的原始返回值（None 与序列均可接受）。
    @param ref 钩子引用字符串，仅用于错误消息定位。
    @return 违规描述列表；空列表 = 通过。
    @raises TypeError 返回值既不是 None 也不是列表/元组。
    """
    if result is None:
        return []
    if isinstance(result, (list, tuple)):
        return [str(v) for v in result]
    raise TypeError(f"validator hook {ref!r} must return list[str] (empty = pass), "
                    f"got {type(result).__name__}")

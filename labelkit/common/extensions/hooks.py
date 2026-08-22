"""用户校验回调钩子（spec 3.8.2 L2.5 / 3.6.2 / v1.18 状态转换）。

钩子引用统一为 ``"<python-file>:<attribute-path>"``（如 ``hooks.py:validate_output``）：
相对 python-file 按 **project root** 解析，绝对路径允许；文件必须是 ``.py`` 普通文件。
M1 经 ``importlib.util.spec_from_file_location`` 装载，module name 由绝对路径 hash 生成
（防两个工程同名 ``hooks.py`` 污染 ``sys.modules``），不改 ``sys.path``、不依赖 cwd、
不做自动发现。M1 解析一次并把 callable 冻结进 :class:`ResolvedHook`；``invoke`` 面不再
按字符串二次 resolve。钩子以运行者同权限执行任意用户代码——信任边界与「配置文件里
写下它」完全一致。

三种钩子形态（均返回违规描述字符串列表，空列表 = 通过）：

- ``output.validator``            —— ``fn(obj: dict, record: Mapping | None) -> list[str]``，
  挂接为结构引擎的 L2.5，仅作用于用户 Schema 的标注调用。
- ``generate.sample_validator``   —— ``fn(text: str) -> list[str]``，
  在相似度过滤之前逐条过滤生成样本。
- ``generate.state_validator`` —— ``fn(value: StateTransitionInput) -> list[str]``，
  在事件计划的后置验证阶段检查一次冻结状态转换。

旧 ``sequence_validator``、``scenario_validator`` 与 ``"module:function"`` 解析面已删除。
"""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Callable

# 旧形态定向错误的固定文案（rule 62：只指引新表达，不读旧值、不转换）。
_OLD_FORM_HINT = ('hook references use the "<python-file>:<attribute-path>" form '
                  '(e.g. hooks.py:validate_output); the v1.16 "module:function" '
                  "form is deleted")


@dataclass(frozen=True)
class ResolvedHook:
    """一个已按工程根目录解析并通过 synthetic probe 的校验器。"""

    reference: str                               # 绝对文件路径与属性路径
    target: Callable[..., list[str]] = field(    # 已解析且不参与显示或相等的 callable
        repr=False, compare=False)


@dataclass(frozen=True)
class ValidationHooks:
    """运行内三个校验阶段唯一使用的冻结 callable 集。"""

    output: ResolvedHook | None = None            # 用户输出 Schema 后置钩子
    sample: ResolvedHook | None = None            # flat 生成样本钩子
    state: ResolvedHook | None = None             # sequence 状态转换钩子


def load_hook(ref: str, project_root: Path) -> ResolvedHook:
    """把 ``<python-file>:<attribute-path>`` 引用解析成冻结载体。

    相对文件按 project root 解析、绝对路径允许；文件必须是 ``.py`` 普通文件。经
    ``spec_from_file_location`` + 绝对路径 hash 生成唯一 module name 装载——不改
    ``sys.path``、不依赖 cwd、不做自动发现。

    @param ref 钩子引用字符串；冒号右侧允许点号属性路径（如 ``hooks.py:Cls.fn``）。
    @param project_root 工程根目录（相对文件的解析基点）。
    @return ``ResolvedHook``（reference = 绝对规范化文件路径 + 属性路径）。
    @raises ValueError 格式非法 / 旧形态 / 文件不存在或非 .py / 模块执行失败 /
            属性不存在 / 目标不可调用；消息面向用户，M1 汇总成 ConfigError 行。
    """
    file_part, sep, attr_path = ref.partition(":")
    if not sep or not file_part.strip() or not attr_path.strip():
        raise ValueError(f"expected \"<python-file>:<attribute-path>\", got {ref!r} - "
                         f"{_OLD_FORM_HINT}")
    attr_path = attr_path.strip()
    path = Path(file_part.strip())
    if not path.is_absolute():
        path = project_root / path
    if path.suffix != ".py":
        raise ValueError(f"hook file must be a .py regular file, got {file_part!r} - "
                         f"{_OLD_FORM_HINT}")
    try:
        abs_path = path.resolve()
    except OSError as exc:
        raise ValueError(f"cannot resolve hook file {file_part!r}: {exc}") from exc
    if not abs_path.is_file():
        raise ValueError(f"hook file does not exist: {abs_path}")
    module = _load_module(abs_path)
    obj: Any = module
    for part in attr_path.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError:
            raise ValueError(
                f"attribute {attr_path!r} not found in hook file {abs_path}") from None
    if not callable(obj):
        raise ValueError(f"{ref!r} is not callable (got {type(obj).__name__})")
    return ResolvedHook(reference=f"{abs_path}:{attr_path}", target=obj)


def _load_module(path: Path):
    """按绝对文件路径装载隔离的用户模块。

    @param path 已验证存在的绝对 Python 文件
    @return 已执行并注册的模块对象
    """
    name = "labelkit_user_hook_" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot create an import spec for hook file {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # 模块执行期自身抛出的任何异常
        raise ValueError(f"cannot execute hook file {path}: {exc}") from exc
    sys.modules[name] = module
    return module


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


def clone_state_input(value: "StateTransitionInput") -> "StateTransitionInput":
    """深拷贝状态钩子输入，隔离用户函数对嵌套对象的修改。

    @param value 待复制的冻结状态转换
    @return 所有 JSON 内容均独立深拷贝的新输入
    """
    from labelkit.common.contracts.generation import StateTransitionInput

    return StateTransitionInput(
        slot_key=value.slot_key,
        variant=value.variant,
        role=value.role,
        state_before=_thaw_json(value.state_before),
        state_after=_thaw_json(value.state_after),
        patch=tuple(_thaw_json(operation) for operation in value.patch),
    )


def _thaw_json(value: Any) -> Any:
    """把深冻结 JSON 树递归复制为普通容器。

    @param value 深冻结 JSON 值
    @return 不共享任何容器的普通 JSON 树
    """
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw_json(item) for item in value]
    return value


def state_probe_input() -> "StateTransitionInput":
    """构造不含用户数据的少数分支状态转换探针。

    @return 可用于两次独立深拷贝确定性检查的冻结输入
    """
    from labelkit.common.contracts.generation import StateTransitionInput

    return StateTransitionInput(
        slot_key="__m1_probe__",
        variant="__minority__",
        role=None,
        state_before={"counter": 0, "nested": {"values": [1, 2]}},
        state_after={"counter": 1, "nested": {"values": [1, 3]}},
        patch=({"op": "replace", "path": "/nested/values/1",
                "value": {"result": [3]}},),
    )


def normalize_state_violations(result: Any, ref: str) -> tuple[str, ...]:
    """严格规整状态钩子的 ``list[str]`` 返回值。

    @param result 钩子的原始返回值
    @param ref 钩子引用，仅用于错误定位
    @return 保持声明顺序的不可变违规字符串
    @raises TypeError 返回值不是字符串列表或含空字符串
    """
    if not isinstance(result, list) or any(
            not isinstance(item, str) or not item.strip() for item in result):
        raise TypeError(
            f"state validator hook {ref!r} must return list[str] with non-empty strings"
        )
    return tuple(result)


def check_hook_arity(hook: ResolvedHook, expected: int) -> str | None:
    """检查钩子声明的位置参数数量（SPEC-SP §4.9 / rule 70 的启动期检查）。

    @param hook 已解析的冻结载体
    @param expected 期望的位置参数个数（output.validator = 2，其余钩子 = 1）
    @return None 表示通过；否则为面向用户的错误描述（M1 汇总成 ConfigError 行）
    """
    try:
        params = tuple(inspect.signature(hook.target).parameters.values())
    except (TypeError, ValueError):
        return "cannot inspect hook signature"
    positional = [p for p in params if p.kind in (
        inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    if len(positional) != expected:
        return (f"hook must accept exactly {expected} positional "
                f"parameter{'s' if expected > 1 else ''}, got {len(positional)}")
    return None


def probe_hook(hook: ResolvedHook, args: tuple) -> str | None:
    """用不含用户数据的 synthetic 输入干跑一次钩子并规整返回值（rule 70）。

    异常与非法返回值都在 validate 阶段聚合上报，绝不等到首条真实 content。

    @param hook 已解析的冻结载体
    @param args 以位置参数形式传给 target 的合成输入
    @return None 表示通过；否则为面向用户的错误描述
    """
    try:
        result = hook.target(*args)
    except Exception as exc:  # 用户钩子干跑期抛出的任何异常
        return f"synthetic dry-run raised {type(exc).__name__}"
    try:
        normalize_violations(result, hook.reference)
    except TypeError as exc:
        return f"synthetic dry-run returned an invalid value: {exc}"
    return None

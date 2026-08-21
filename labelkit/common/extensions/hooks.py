"""用户校验回调钩子（spec 3.8.2 L2.5 / 3.6.2，v1.17 SPEC-SP §4.9）。

钩子引用统一为 ``"<python-file>:<attribute-path>"``（如 ``hooks.py:validate_output``）：
相对 python-file 按 **project root** 解析，绝对路径允许；文件必须是 ``.py`` 普通文件。
M1 经 ``importlib.util.spec_from_file_location`` 装载，module name 由绝对路径 hash 生成
（防两个工程同名 ``hooks.py`` 污染 ``sys.modules``），不改 ``sys.path``、不依赖 cwd、
不做自动发现。M1 解析一次并把 callable 冻结进 :class:`ResolvedHook`；``invoke`` 面不再
按字符串二次 resolve。钩子以运行者同权限执行任意用户代码——信任边界与「配置文件里
写下它」完全一致。

四种钩子形态（均返回违规描述字符串列表，空列表 = 通过）：

- ``output.validator``            —— ``fn(obj: dict, record: Mapping | None) -> list[str]``，
  挂接为结构引擎的 L2.5，仅作用于用户 Schema 的标注调用。
- ``generate.sample_validator``   —— ``fn(text: str) -> list[str]``，
  在相似度过滤之前逐条过滤生成样本。
- ``generate.sequence_validator`` —— ``fn(value: SequenceValidationInput) -> list[str]``，
  在声明规则之后、序列相似度之前检查一条冻结序列。
- ``generate.scenario_validator`` —— ``fn(value: ScenarioValidationInput) -> list[str]``，
  场景交付时增量校验（candidate 是本次唯一可拒绝项）。

v1.16 的 ``"module:function"`` 形态已删除（rule 62/70）——M1 对旧形态报定向错误；
本模块暂留的 :func:`resolve_hook` 旧解析器仅供 M6/M8 的既有字符串 ``invoke`` 面过渡使用
（v1.17 后续 wave 把它们改读冻结载体后即删）。
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import inspect
import sys
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Callable

from labelkit.common.contracts.types import (
    ScenarioSequence,
    ScenarioValidationInput,
    SequenceValidationFrame,
    SequenceValidationInput,
)

# 旧形态定向错误的固定文案（rule 62：只指引新表达，不读旧值、不转换）。
_OLD_FORM_HINT = ('hook references use the "<python-file>:<attribute-path>" form '
                  '(e.g. hooks.py:validate_output); the v1.16 "module:function" '
                  "form is deleted")


@dataclass(frozen=True)
class ResolvedHook:
    """一个已按工程根目录解析并通过 synthetic probe 的校验器。"""

    reference: str
    target: Callable[..., list[str]] = field(repr=False, compare=False)


@dataclass(frozen=True)
class ValidationHooks:
    """运行内四个校验阶段唯一使用的冻结 callable 集。"""

    output: ResolvedHook | None = None
    sample: ResolvedHook | None = None
    sequence: ResolvedHook | None = None
    scenario: ResolvedHook | None = None


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
    module_name = ("labelkit_user_hook_"
                   + hashlib.sha256(str(abs_path).encode("utf-8")).hexdigest()[:16])
    spec = importlib.util.spec_from_file_location(module_name, abs_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot create an import spec for hook file {abs_path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # 模块执行期自身抛出的任何异常
        raise ValueError(f"cannot execute hook file {abs_path}: {exc}") from exc
    sys.modules[module_name] = module
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


def clone_sequence_input(value: SequenceValidationInput) -> SequenceValidationInput:
    """深拷贝序列钩子输入，隔离用户函数对 payload 的修改。

    @param value M6 组装的冻结序列视图
    @return 元数据不变、payload 完全深拷贝的新序列视图
    """
    frames = tuple(SequenceValidationFrame(
        position=frame.position,
        frame_class=frame.frame_class,
        payload=copy.deepcopy(frame.payload),
    ) for frame in value.frames)
    return SequenceValidationInput(
        sequence_class=value.sequence_class,
        tier_rank=value.tier_rank,
        frames=frames,
    )


def scenario_probe_input() -> ScenarioValidationInput:
    """构造 scenario validator 的最小合成输入（不含任何用户数据）。

    @return ``accepted = ()`` 与一条最小 candidate 的 ``ScenarioValidationInput``
    """
    candidate = ScenarioSequence(
        slot_key="__m1_probe__",
        sequence_class="__m1_probe__",
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T00:00:01Z",
        frames=(SequenceValidationFrame(0, "__m1_probe__", {}),),
    )
    return ScenarioValidationInput(accepted=(), candidate=candidate)


def check_hook_arity(hook: ResolvedHook, expected: int) -> str | None:
    """检查钩子声明的位置参数数量（SPEC-SP §4.9 / rule 70 的启动期检查）。

    @param hook 已解析的冻结载体
    @param expected 期望的位置参数个数（output.validator = 2，其余三个钩子 = 1）
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


def resolve_hook(ref: str) -> Callable[..., Any]:
    """【临时接线·v1.17 后续 wave 删除】旧 ``"module:function"`` 字符串解析器。

    仅剩 M6（generate 的逐帧/序列 ``invoke`` 面）与 M8（schema engine L2.5）在运行期
    按字符串二次 resolve 时使用；它们改读 ``ResolvedConfig.validation_hooks`` 冻结
    载体后，本函数与旧形态一并删除。M1 的配置解析面**不再**走这里（rule 70）。

    @param ref 钩子引用字符串；冒号右侧允许点号属性路径（如 ``pkg.mod:Cls.fn``）。
    @return 解析得到的可调用对象。
    @raises ValueError 格式非法 / 模块导入失败 / 属性不存在 / 目标不可调用。
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

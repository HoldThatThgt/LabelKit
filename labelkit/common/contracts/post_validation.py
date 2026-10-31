"""M8 通用后校验接缝的中立载体（CONTRACTS §7.7，SPEC-image-region-segmentation §10.4）。

接缝不含任何事件或几何语义：成功证明的类型由调用方判定。本模块只在 ``TYPE_CHECKING``
下引用 ``PromptBundle``、``CallScope`` 与 ``Usage``，运行期不导入 ``llm_client`` 与
``schema_engine``，因此 M8 与任一调用方都可以直接导入它而不形成导入环。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, TypeAlias

if TYPE_CHECKING:
    from labelkit.common.contracts.types import Usage
    from labelkit.common.inference.llm_client import PromptBundle
    from labelkit.common.inference.schema_engine import CallScope


def _freeze_json(value: object) -> object:
    """递归复制并冻结 JSON 容器，规则与 ``generation._freeze_value`` 相同。

    @param value 待冻结字段值
    @return 不可变 Mapping/tuple/frozenset 或原对象
    """
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_json(item) for item in value)
    return value


@dataclass(frozen=True)
class PostValidationResult:
    """M8 单次后置校验产生的违规或成功证明。"""

    violations: tuple[str, ...]                   # 可修复违规；成功时为空
    proof: object | None                          # 成功时非空的证明对象

    def __post_init__(self) -> None:
        """把违规序列归一为元组；``proof`` 不复制，同一实例原样保留。"""
        object.__setattr__(self, "violations", _freeze_json(self.violations))


CallPostValidator: TypeAlias = Callable[[Mapping[str, object]], PostValidationResult]


@dataclass(frozen=True)
class PostValidatedCallRequest:
    """一次带 request-local 后置校验器的 M8 调用。"""

    profile: str                                  # 调用 profile
    prompt: PromptBundle                          # 完整提示包
    schema: Mapping[str, object]                  # 完整内部 Schema（构造时深冻结）
    scope: CallScope                              # 记账与 trace 范围
    post_validator: CallPostValidator             # 仅本请求生效的后置验证器

    def __post_init__(self) -> None:
        """深冻结内部 Schema，使请求与调用方的可变容器隔离。"""
        object.__setattr__(self, "schema", _freeze_json(self.schema))


@dataclass(frozen=True)
class ValidatedPostCall:
    """M8 后置验证成功且保留同一证明对象的结果。"""

    object: Mapping[str, object]                  # 成功候选对象
    proof: object                                 # 同一候选的成功证明
    resolved_at: Literal["l0_or_clean", "l1", "l3_1", "l3_2"]  # 内部解析路径
    usage: Usage                                  # 累计 token 用量
    attempts: int                                 # 总 LLM 调用次数
    model: str                                    # 首轮实际模型名

    def __post_init__(self) -> None:
        """深冻结成功候选对象；``proof`` 不复制，同一实例原样保留。"""
        object.__setattr__(self, "object", _freeze_json(self.object))

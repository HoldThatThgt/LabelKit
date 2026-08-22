"""quality 阶段纯叶调用的冻结计划与结果载体。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias

from labelkit.common.contracts.types import Record, Transition

if TYPE_CHECKING:
    from labelkit.common.config.model import Criterion


@dataclass(frozen=True, slots=True)
class PairwiseQualityCall:
    """一次成对质量判定的不可变调用计划。

    @param ordinal 全批 quality 叶任务声明序
    @param pool_ordinal 打分池声明序
    @param comparison_ordinal 池内比较声明序
    @param first_item_ordinal 抽样序第一条记录的池内下标
    @param second_item_ordinal 抽样序第二条记录的池内下标
    @param a_item_ordinal 呈现序 A 位记录的池内下标
    @param b_item_ordinal 呈现序 B 位记录的池内下标
    @param record_a 呈现序 A 位记录快照
    @param record_b 呈现序 B 位记录快照
    @param transitions_a A 位记录的步骤快照
    @param transitions_b B 位记录的步骤快照
    @param profile 首轮判定使用的 LLM profile
    @param flipped 是否为双顺序的翻转调用
    @param criteria 本调用覆盖的 rubric 准则组
    @param with_reason 是否要求理由
    @param multi_judge 是否为多评委配置
    @param pool 可选分类池名
    """

    ordinal: int
    pool_ordinal: int
    comparison_ordinal: int
    first_item_ordinal: int
    second_item_ordinal: int
    a_item_ordinal: int
    b_item_ordinal: int
    record_a: Record
    record_b: Record
    transitions_a: tuple[Transition, ...] | None
    transitions_b: tuple[Transition, ...] | None
    profile: str
    flipped: bool
    criteria: tuple[Criterion, ...]
    with_reason: bool
    multi_judge: bool
    pool: str | None


@dataclass(frozen=True, slots=True)
class PointwiseQualityCall:
    """一次逐记录逐准则质量判定的不可变调用计划。

    @param ordinal 全批 quality 叶任务声明序
    @param pool_ordinal 打分池声明序
    @param item_ordinal 记录在池内的声明序
    @param record 只读记录快照
    @param transitions 记录的步骤快照
    @param criterion 本调用覆盖的唯一准则
    @param profile 首轮判定使用的 LLM profile
    @param pool 可选分类池名
    """

    ordinal: int
    pool_ordinal: int
    item_ordinal: int
    record: Record
    transitions: tuple[Transition, ...] | None
    criterion: Criterion
    profile: str
    pool: str | None


QualityCall: TypeAlias = PairwiseQualityCall | PointwiseQualityCall


@dataclass(frozen=True, slots=True)
class QualityFitRequest:
    """一次上下文预算装填所需的冻结静态输入。

    @param profile 当前调用的 LLM profile
    @param system_text 当前调用的系统提示词
    @param schema 当前调用的结构化输出 Schema
    @param records 当前调用的只读记录快照
    """

    profile: str
    system_text: str
    schema: dict
    records: tuple[Record, ...]


@dataclass(frozen=True, slots=True)
class QualityJudgment:
    """一次已校验成对判定中的冻结准则裁决。

    @param criterion rubric 准则键
    @param winner 模型返回的呈现位胜者
    @param reason 可选判定理由
    """

    criterion: str
    winner: Literal["A", "B", "tie"]
    reason: str | None


@dataclass(frozen=True, slots=True)
class PairwiseQualityOutcome:
    """一次成功成对判定的纯结果。

    @param judgments 首次命中的准则裁决，保持 schema 数组序
    @param model 实际应答模型名
    """

    judgments: tuple[QualityJudgment, ...]
    model: str


@dataclass(frozen=True, slots=True)
class PointwiseQualityOutcome:
    """一次成功逐条判定的纯结果。

    @param raw_score 原始零至五分
    @param reason 模型给出的理由
    """

    raw_score: int
    reason: str


@dataclass(frozen=True, slots=True)
class QualityCallFailure:
    """一次叶调用的冻结失败结果。

    @param error 原始异常；只在声明序 reducer 中转成业务错误
    @param scope call 表示既有比较或记录失败，pool 表示池内不变式破损
    @param precheck 是否在最小单元发送前预检终结
    """

    error: Exception
    scope: Literal["call", "pool"]
    precheck: bool = False


QualityCallOutcome: TypeAlias = (
    PairwiseQualityOutcome | PointwiseQualityOutcome | QualityCallFailure
)

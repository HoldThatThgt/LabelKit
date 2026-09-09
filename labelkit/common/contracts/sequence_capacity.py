"""普通会话容量、出现位置和错误归属的公共契约。"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, Sequence

from labelkit.common.errors import ContextOverflowError, SessionCapacityError

if TYPE_CHECKING:
    from labelkit.common.contracts.stage import RunContext
    from labelkit.common.contracts.types import PipelineItem

_log = logging.getLogger(__name__)
CapacityUnit = Literal["sequence", "pairwise", "frame", "transition", "fixed", "stitch_pool"]


@dataclass(frozen=True)
class CapacityTarget:
    """定位容量请求中的一个序列视图。

    @param root_id 已冻结的上游序列身份。
    @param record_id 当前容量子序列身份。
    @param label 当前分类视图，未分类时为空。
    @param member_positions 请求实际使用的成员出现位置。
    """

    root_id: str                           # 冻结上游序列的根身份
    record_id: str                         # 当前序列或容量子序列身份
    label: str | None                      # 请求所属的分类视图
    member_positions: tuple[int, ...]      # 工作成员位置，可能包含验证临时回收的帧


@dataclass(frozen=True)
class SessionCapacityFailure:
    """把真实容量错误和可确定归属的请求单位交给会话编排。

    @param stage 拥有该操作的编排阶段，验证内的子调用归验证。
    @param targets 按请求声明顺序排列的受影响序列视图。
    @param unit 请求的最小完整证据单位。
    @param error 保留原始档案、相位和来源的容量错误。
    """

    stage: str                            # 编排阶段归属
    targets: tuple[CapacityTarget, ...]    # 请求关联的序列视图
    unit: CapacityUnit                    # 是否可通过序列分区缩短该请求
    error: ContextOverflowError           # 未被改写为普通失败的原始异常


@dataclass(frozen=True)
class SessionAttemptScope:
    """固定会话尝试身份与此前已确立的最小失败。

    @param session_id 摄取器生成的完整会话身份。
    @param ordinal 输入会话声明序，从一开始。
    @param attempt 上游为零，下游尝试从一开始。
    @param stage 当前编排阶段。
    @param terminal_failures 不得再次发送的已知最小失败请求。
    """

    session_id: str                        # 完整会话身份
    ordinal: int                          # 输入声明序，不受计算分组影响
    attempt: int                          # 可审计的尝试序号
    stage: str                            # 当前调用所属编排阶段
    terminal_failures: tuple[SessionCapacityFailure, ...] = ()
                                          # 固定失败列表，普通请求不得猜测其归属


class SequenceCapacityChecker(Protocol):
    """供分段和缝合使用的完整已知请求预览接口。"""

    def preview(self, item: PipelineItem, ctx: RunContext) -> SessionCapacityFailure | None:
        """检查完整已知请求，不调用模型或修改信封。

        @param item 候选完整序列视图。
        @param ctx 当前冻结会话上下文。
        @return 首个声明序容量问题；全部可装时为空。
        """
        ...


def process_sequence_id(session_id: str, positions: Sequence[int], member_ids: Sequence[str]) -> str:
    """推导普通流初始序列身份。

    @param session_id 完整会话身份。
    @param positions 有序输入出现位置。
    @param member_ids 与位置一一对应的内容身份。
    @return 域分离的十六位十六进制身份。
    """
    return _sequence_id("process_sequence", session_id, positions, member_ids)


def capacity_sequence_id(root_id: str, positions: Sequence[int], member_ids: Sequence[str]) -> str:
    """推导与拆分路径无关的容量子序列身份。

    @param root_id 冻结的上游根序列身份。
    @param positions 最终子序列成员出现位置。
    @param member_ids 与位置一一对应的内容身份。
    @return 与上游身份域分离的子序列身份。
    """
    return _sequence_id("process_sequence_capacity", root_id, positions, member_ids)


def _sequence_id(domain: str, owner: str, positions: Sequence[int], member_ids: Sequence[str]) -> str:
    """用唯一的规范序列化公式生成身份。

    @param domain 初始序列或容量子序列身份域。
    @param owner 会话或上游根身份。
    @param positions 严格递增的非空成员位置。
    @param member_ids 与位置对齐的内容身份。
    @return 截取的 SHA-256 十六进制身份。
    @raises ValueError 成员位置不构成非空有序的一一映射。
    """
    valid = len(positions) == len(member_ids) and bool(positions)
    valid = valid and all(type(p) is int and p >= 0 for p in positions)
    valid = valid and all(left < right for left, right in zip(positions, positions[1:]))
    if not valid:
        _log.error("sequence identity requires ordered, nonempty member positions")
        raise ValueError("sequence identity requires ordered, nonempty member positions")
    value = [domain, owner, list(positions), list(member_ids)]
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def member_key(item: PipelineItem, index: int) -> int | str:
    """读取已声明的成员产物键，不以内容身份猜测流输入位置。

    @param item 序列信封。
    @param index 零起始序列成员索引。
    @return 普通流的位置整数，或生成路径的既有成员身份。
    @raises ValueError 普通流成员位置未与成员对齐。
    """
    if item.member_positions or item.capacity is not None:
        if len(item.member_positions) != len(item.record.members):
            _log.error("stream member positions do not match sequence members")
            raise ValueError("stream member positions do not match sequence members")
        return item.member_positions[index]
    return item.record.members[index].id


def capacity_target(item: PipelineItem, positions: Sequence[int] | None = None) -> CapacityTarget:
    """从信封构造请求归属，保留工作成员出现位置。

    @param item 当前序列分类视图。
    @param positions 请求实际涉及的位置；未指定时使用完整工作成员。
    @return 不可变容量请求目标。
    """
    capacity = item.capacity
    root_id = capacity.root_id if capacity is not None and capacity.root_id is not None else item.record.id
    label = item.classification.label if item.classification is not None else None
    working = item.member_positions if positions is None else tuple(positions)
    return CapacityTarget(root_id, item.record.id, label, tuple(working))


def terminal_capacity_failure(ctx: RunContext, target: CapacityTarget, unit: CapacityUnit,
                              profile: str | None) -> SessionCapacityFailure | None:
    """定位当前执行门不得重发的最小请求。

    @param ctx 当前会话尝试上下文。
    @param target 明确包含出现位置的请求目标。
    @param unit 请求最小证据单位。
    @param profile 当前模型档案。
    @return 已冻结失败；没有匹配项或非会话尝试时为空。
    """
    scope = ctx.session_attempt
    if scope is None:
        return None
    for failure in scope.terminal_failures:
        if failure.stage != scope.stage or failure.unit != unit or failure.error.profile != profile:
            continue
        if any(_same_terminal_target(old, target, unit) for old in failure.targets):
            return failure
    return None


def _same_terminal_target(old: CapacityTarget, current: CapacityTarget, unit: CapacityUnit) -> bool:
    """按最小请求或序列执行门匹配终态归属。

    @param old 已失败请求目标。
    @param current 当前执行门目标。
    @param unit 请求证据单位。
    @return 是否属于同一不可重发请求。
    """
    if (old.root_id, old.label) != (current.root_id, current.label):
        return False
    if unit == "frame":
        return old.member_positions == current.member_positions
    return old.record_id == current.record_id


def raise_session_capacity(ctx: RunContext, targets: Sequence[CapacityTarget], error: BaseException,
                           unit: CapacityUnit) -> None:
    """在普通记录错误归并前转交可重算的会话容量信号。

    @param ctx 当前编排阶段上下文。
    @param targets 按请求顺序排列的明确目标。
    @param error 真实请求异常，非容量异常不改变既有路由。
    @param unit 请求最小证据单位。
    @raises SessionCapacityError 尚未确立终态的会话容量问题。
    """
    raise_session_capacities(ctx, capacity_failures(ctx, targets, error, unit))


def capacity_failures(ctx: RunContext, targets: Sequence[CapacityTarget], error: BaseException,
                      unit: CapacityUnit) -> tuple[SessionCapacityFailure, ...]:
    """纯转换原始或嵌套容量错误，供完整判决轮声明序扫描。

    @param ctx 当前编排阶段上下文。
    @param targets 原始请求归属，嵌套信号保留其已有精确归属。
    @param error 实际叶结果中的异常。
    @param unit 原始请求的最小完整证据单位。
    @return 容量事实元组；非会话或非容量错误为空。
    """
    scope = ctx.session_attempt
    if scope is None:
        return ()
    if isinstance(error, SessionCapacityError):
        return error.failures
    if not isinstance(error, ContextOverflowError):
        return ()
    targets = tuple(targets)
    if not targets:
        _log.error("session capacity signal requires an explicit request target")
        raise ValueError("session capacity signal requires an explicit request target")
    return (SessionCapacityFailure(scope.stage, targets, unit, error),)


def raise_session_capacities(ctx: RunContext, failures: Sequence[SessionCapacityFailure]) -> None:
    """归并整轮已收齐的新容量事实，禁止首错隐藏其余已失败请求。

    @param ctx 当前编排阶段上下文。
    @param failures 完整判决轮中按任务声明序排列的容量事实。
    @raises SessionCapacityError 至少一个请求尚未确立终态。
    """
    if ctx.session_attempt is None:
        return
    pending = tuple(failure for failure in failures if not all(
        terminal_capacity_failure(ctx, target, failure.unit, failure.error.profile) is not None
        for target in failure.targets))
    if not pending:
        return
    for failure in pending:
        _log.error("session request exceeds context capacity: stage=%s profile=%s phase=%s",
                   failure.stage, failure.error.profile, failure.error.phase)
    raise SessionCapacityError(pending) from pending[0].error

"""资源感知、结构化终止且按输入序归并的异步执行运行时。"""
from __future__ import annotations

import asyncio
import logging
from contextvars import Context, ContextVar, Token, copy_context
from dataclasses import dataclass
from typing import Awaitable, Callable, Generic, TypeVar, cast

from labelkit.common.contracts.execution import ResourceKey, TaskGroupRequest, TaskSpec
from labelkit.common.errors import CircuitBreakerTripped, InternalError
from labelkit.common.observability.obslog import MetricsSink
from labelkit.runtime.resources import ResourceManager


T = TypeVar("T")
_DOMAIN: ContextVar[ExecutionRuntime | None] = ContextVar("labelkit_execution_domain", default=None)
_IN_LEAF: ContextVar[bool] = ContextVar("labelkit_execution_leaf", default=False)
_MISSING = object()
_logger = logging.getLogger("labelkit.runtime.scheduler")


@dataclass(frozen=True)
class _FatalEntry:
    """一次叶任务 fatal 的稳定身份与原异常。"""

    declaration_key: tuple[int, ...]
    error: Exception


@dataclass(frozen=True)
class _CancellationEntry:
    """一个叶任务主动传播的取消与稳定声明序。"""

    declaration_key: tuple[int, ...]
    error: asyncio.CancelledError


@dataclass(frozen=True)
class _ControlEntry:
    """一个交回工作流属主的局部结构化控制信号。"""

    declaration_key: tuple[int, ...]
    error: CircuitBreakerTripped


class _LeafFailure(Exception):
    """只在 TaskGroup 内传播、永不越过 runtime 边界的包装。"""

    def __init__(self, entry: _FatalEntry):
        """绑定稳定 fatal 条目。

        @param entry 原异常与声明序身份
        """
        self.entry = entry
        super().__init__("runtime leaf failed")


class _LeafCancellation(Exception):
    """让 TaskGroup 结构化取消 siblings 的内部取消载体。"""

    def __init__(self, entry: _CancellationEntry):
        """绑定待还原的语言原语取消。

        @param entry 原取消与声明序身份
        """
        self.entry = entry
        super().__init__("runtime leaf cancelled itself")


class _LeafControl(Exception):
    """让当前 TaskGroup 清理 siblings 的内部控制载体。"""

    def __init__(self, entry: _ControlEntry):
        """绑定待交回工作流的控制信号。

        @param entry 原控制异常与声明序身份
        """
        self.entry = entry
        super().__init__("runtime leaf raised workflow control")


class _DomainAbort(Exception):
    """只用于唤醒根 TaskGroup 并取消整个执行域。"""


async def _invoke_leaf(operation: Callable[[], Awaitable[T]]) -> T:
    """在独立子任务中调用叶操作，使自取消与结构取消可区分。

    @param operation 叶任务纯异步调用
    @return 叶操作结果
    """
    return await operation()


@dataclass
class _GroupState(Generic[T]):
    """一个 run_group 作用域内的私有可变状态。"""

    request: TaskGroupRequest[T]
    results: list[object]
    pending: set[int]
    captured: Context
    task_group: asyncio.TaskGroup


class ExecutionRuntime:
    """一次 live run 唯一的 TaskExecutor 与 execution domain。"""

    def __init__(self, resources: ResourceManager, metrics: MetricsSink):
        """创建尚未进入执行域的运行时。

        @param resources profile 与 origin 资源所有者
        @param metrics 运行观测汇
        """
        self._resources = resources
        self._metrics = metrics
        self._admission: dict[ResourceKey, asyncio.Semaphore] = {}
        self._task_ids: set[str] = set()
        self._fatal_entries: list[_FatalEntry] = []
        self._cancellation_entries: list[_CancellationEntry] = []
        self._group_tasks: set[asyncio.Task[object]] = set()
        self._abort = asyncio.Event()
        self._state = "new"
        self._queued = 0
        self._running = 0

    async def run(self, workflow: Callable[[], Awaitable[T]]) -> T:
        """建立唯一 execution domain 并等待其中全部工作结束。

        @param workflow composition root 提供的完整工作流
        @return 工作流结果
        @raises InternalError 重复或嵌套进入执行域
        """
        if self._state != "new" or _DOMAIN.get() is not None:
            _logger.error("execution runtime cannot enter a nested or reused domain")
            raise InternalError("execution runtime cannot enter a nested or reused domain")
        self._state = "running"
        token = _DOMAIN.set(self)
        result: object = _MISSING
        primary: BaseException | None = None
        try:
            result = await self._run_domain(workflow)
        except BaseException as exc:  # control 异常必须在 cleanup 后保持原语义
            primary = exc
        finally:
            self._state = "stopping"
            await self._settle_unmanaged_groups()
            _DOMAIN.reset(token)
            self._state = "closed"
        self._raise_terminal(primary)
        if result is _MISSING:
            _logger.error("execution workflow ended without a result")
            raise InternalError("execution workflow ended without a result")
        return cast(T, result)

    async def _run_domain(self, workflow: Callable[[], Awaitable[T]]) -> T:
        """以根 TaskGroup 绑定 workflow 与 fatal abort watcher。

        @param workflow composition root 提供的完整工作流
        @return 工作流结果
        """
        result: object = _MISSING
        async with asyncio.TaskGroup() as task_group:
            workflow_task = task_group.create_task(workflow())
            watcher = task_group.create_task(self._watch_abort())
            result = await workflow_task
            watcher.cancel()
        return cast(T, result)

    async def _watch_abort(self) -> None:
        """在任一叶 fatal 后打断根 TaskGroup。"""
        await self._abort.wait()
        raise _DomainAbort("execution domain aborted")

    async def run_group(self, request: TaskGroupRequest[T]) -> tuple[T, ...]:
        """按资源独立接纳叶任务并按请求输入序返回结果。

        @param request 冻结任务组
        @return 输入序结果 tuple
        @raises InternalError 域外、叶内、重复身份或非法计划
        """
        self._require_group_domain()
        if not request.tasks:
            return ()
        lanes = self._validate_and_group(request)
        caller = cast(asyncio.Task[object] | None, asyncio.current_task())
        if caller is None:
            _logger.error("runtime task group has no owning asyncio task")
            raise InternalError("runtime task group has no owning asyncio task")
        self._group_tasks.add(caller)
        self._change_queue(len(request.tasks))
        pending = set(range(len(request.tasks)))
        try:
            return await self._run_group_scope(request, lanes, pending)
        finally:
            if pending:
                self._change_queue(-len(pending))
            self._group_tasks.discard(caller)

    def _require_group_domain(self) -> None:
        """验证 run_group 调用位于当前协调域且不在叶任务内。

        @raises InternalError 调用边界非法
        """
        if self._state != "running" or _DOMAIN.get() is not self:
            _logger.error("task group submitted outside its execution domain")
            raise InternalError("task group submitted outside its execution domain")
        if _IN_LEAF.get():
            _logger.error("runtime leaf cannot submit a nested task group")
            raise InternalError("runtime leaf cannot submit a nested task group")

    def _validate_and_group(self, request: TaskGroupRequest[T]) -> dict[ResourceKey, list[int]]:
        """在任何阻塞或叶创建前验证整组并按资源分道。

        @param request 冻结任务组
        @return 资源键到输入位置的映射
        @raises InternalError 计划字段非法或身份重复
        """
        lanes: dict[ResourceKey, list[int]] = {}
        ids: set[str] = set()
        for index, spec in enumerate(request.tasks):
            self._validate_spec(spec)
            if spec.task_id in ids or spec.task_id in self._task_ids:
                _logger.error("duplicate runtime task id")
                raise InternalError("duplicate runtime task id")
            ids.add(spec.task_id)
            lanes.setdefault(spec.resource_key, []).append(index)
        self._task_ids.update(ids)
        return lanes

    def _validate_spec(self, spec: TaskSpec[object]) -> None:
        """验证单个任务的稳定身份和可调用面。

        @param spec 待验证任务计划
        @raises InternalError 字段非法或资源未知
        """
        valid_key = (isinstance(spec.declaration_key, tuple) and bool(spec.declaration_key)
                     and all(isinstance(value, int) and not isinstance(value, bool)
                             for value in spec.declaration_key))
        if not spec.task_id or not spec.stage or not valid_key or not callable(spec.operation):
            _logger.error("invalid runtime task specification")
            raise InternalError("invalid runtime task specification")
        capacity = self._resources.admission_capacity(spec.resource_key)
        self._admission.setdefault(spec.resource_key, asyncio.Semaphore(capacity))

    async def _run_group_scope(self, request: TaskGroupRequest[T], lanes: dict[ResourceKey, list[int]],
                               pending: set[int]) -> tuple[T, ...]:
        """运行资源生产协程与取得接纳后的叶任务。

        @param request 冻结任务组
        @param lanes 资源分道后的输入位置
        @param pending 尚未取得接纳的位置集合
        @return 输入序结果 tuple
        """
        results: list[object] = [_MISSING] * len(request.tasks)
        captured = copy_context()
        try:
            async with asyncio.TaskGroup() as task_group:
                state = _GroupState(request, results, pending, captured, task_group)
                for resource_key, indices in lanes.items():
                    task_group.create_task(self._produce_lane(resource_key, indices, state))
        except BaseExceptionGroup as group:
            raise self._unwrap_group(group) from None
        if any(value is _MISSING for value in results):
            _logger.error("runtime task group completed with missing results")
            raise InternalError("runtime task group completed with missing results")
        return cast(tuple[T, ...], tuple(results))

    async def _produce_lane(self, resource_key: ResourceKey, indices: list[int],
                            state: _GroupState[T]) -> None:
        """按一个资源通道的输入序接纳并创建叶任务。

        @param resource_key 当前资源通道
        @param indices 该通道的请求输入位置
        @param state 任务组共享状态
        """
        admission = self._admission[resource_key]
        for index in indices:
            await admission.acquire()
            state.pending.remove(index)
            self._change_queue(-1)
            try:
                leaf_context = state.captured.copy()
                task = state.task_group.create_task(self._run_leaf(index, state), context=leaf_context)
                task.add_done_callback(
                    lambda done, spec=state.request.tasks[index]: self._finish_leaf(spec, done),
                )
            except BaseException:
                admission.release()
                _logger.error("runtime failed to create an admitted leaf task")
                raise

    def _finish_leaf(self, spec: TaskSpec[object], task: asyncio.Task[None]) -> None:
        """归还已转交给叶任务的接纳名额。

        @param spec 已接纳的叶任务计划
        @param task 已终结的叶任务
        """
        self._admission[spec.resource_key].release()
        if task.cancelled():
            self._metrics.add_runtime_total("cancelled_tasks", 1)

    async def _run_leaf(self, index: int, state: _GroupState[T]) -> None:
        """执行已接纳叶任务，并在 finally 清理叶上下文与运行计数。

        @param index 请求输入位置
        @param state 任务组共享状态
        """
        spec = state.request.tasks[index]
        marker: Token[bool] = _IN_LEAF.set(True)
        self_cancelled = False
        self._change_running(1)
        try:
            operation_task = asyncio.create_task(_invoke_leaf(spec.operation))
            state.results[index] = await operation_task
        except asyncio.CancelledError as exc:
            _logger.debug("runtime leaf cancelled: task_id=%s", spec.task_id)
            task = asyncio.current_task()
            if task is None or task.cancelling() == 0:
                self_cancelled = True
                entry = _CancellationEntry(spec.declaration_key, exc)
                self._cancellation_entries.append(entry)
                self._abort.set()
                raise _LeafCancellation(entry) from None
            raise
        except CircuitBreakerTripped as exc:
            entry = _ControlEntry(spec.declaration_key, exc)
            raise _LeafControl(entry) from exc
        except Exception as exc:
            entry = _FatalEntry(spec.declaration_key, exc)
            self._fatal_entries.append(entry)
            self._abort.set()
            _logger.error("runtime leaf failed: task_id=%s stage=%s", spec.task_id, spec.stage)
            raise _LeafFailure(entry) from exc
        finally:
            self._change_running(-1)
            _IN_LEAF.reset(marker)
            if self_cancelled:
                self._metrics.add_runtime_total("cancelled_tasks", 1)

    def _unwrap_group(self, group: BaseExceptionGroup) -> BaseException:
        """把 TaskGroup 异常树还原为稳定原异常。

        @param group TaskGroup 抛出的异常树
        @return 应越过 run_group 边界的单个原异常
        """
        entries: list[_FatalEntry] = []
        cancellations: list[_CancellationEntry] = []
        controls: list[_ControlEntry] = []
        ordinary: list[BaseException] = []
        for error in _flatten_group(group):
            if isinstance(error, _LeafFailure):
                entries.append(error.entry)
            elif isinstance(error, _LeafCancellation):
                cancellations.append(error.entry)
            elif isinstance(error, _LeafControl):
                controls.append(error.entry)
            elif not isinstance(error, asyncio.CancelledError):
                ordinary.append(error)
        if cancellations:
            return min(cancellations, key=lambda value: value.declaration_key).error
        if entries:
            return min(entries, key=lambda value: value.declaration_key).error
        if controls:
            return min(controls, key=lambda value: value.declaration_key).error
        if ordinary:
            _logger.error("runtime task group raised an untracked exception")
            return ordinary[0]
        return asyncio.CancelledError()

    async def _settle_unmanaged_groups(self) -> None:
        """取消并回收工作流未结构化等待的 task group 调用者。"""
        current = asyncio.current_task()
        tasks = {task for task in self._group_tasks if task is not current and not task.done()}
        if not tasks:
            return
        _logger.error("execution workflow returned with active task groups")
        for task in tasks:
            task.cancel()
        done, _ = await asyncio.wait(tasks)
        for task in done:
            try:
                task.exception()
            except asyncio.CancelledError:
                pass

    def _raise_terminal(self, primary: BaseException | None) -> None:
        """按 control 优先、随后稳定 fatal 的规则结束执行域。

        @param primary workflow 边界观察到的主异常
        """
        if isinstance(primary, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise primary
        if self._cancellation_entries:
            entry = min(self._cancellation_entries, key=lambda value: value.declaration_key)
            raise entry.error
        if self._fatal_entries:
            entry = min(self._fatal_entries, key=lambda value: value.declaration_key)
            raise entry.error
        if primary is not None:
            if isinstance(primary, BaseExceptionGroup):
                errors = [error for error in _flatten_group(primary)
                          if not isinstance(error, asyncio.CancelledError)]
                if errors:
                    raise errors[0]
                raise asyncio.CancelledError()
            raise primary

    def _change_queue(self, delta: int) -> None:
        """更新全域等待接纳数及其高水位。

        @param delta 等待任务数增量
        """
        self._queued += delta
        self._metrics.observe_runtime_high_water("queue", self._queued)

    def _change_running(self, delta: int) -> None:
        """更新全域运行叶任务数及其高水位。

        @param delta 运行叶任务数增量
        """
        self._running += delta
        self._metrics.observe_runtime_high_water("running", self._running)


def _flatten_group(group: BaseExceptionGroup) -> list[BaseException]:
    """把 ExceptionGroup 递归展开成叶异常。

    @param group 待展开异常树
    @return 原顺序叶异常
    """
    result: list[BaseException] = []
    for error in group.exceptions:
        if isinstance(error, BaseExceptionGroup):
            result.extend(_flatten_group(error))
        else:
            result.append(error)
    return result

"""v1.19 execution runtime 的跨层冻结契约。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncContextManager, Awaitable, Callable, Generic, Literal, Protocol, TypeVar


T = TypeVar("T")
ResourceKey = tuple[Literal["llm", "embedding"], str]
HttpOrigin = tuple[str, str, int]


@dataclass(frozen=True)
class TaskSpec(Generic[T]):
    """一次纯异步叶调用的不可变计划。

    @param task_id execution domain 内唯一且不含数据内容的任务标识
    @param declaration_key 用于同时 fatal 稳定选取的声明序键
    @param stage 现有业务阶段名
    @param resource_key 首轮模型调用的资源接纳身份
    @param operation 不进入表示、比较或持久化的纯异步调用
    """

    task_id: str
    declaration_key: tuple[int, ...]
    stage: str
    resource_key: ResourceKey
    operation: Callable[[], Awaitable[T]] = field(repr=False, compare=False)


@dataclass(frozen=True)
class TaskGroupRequest(Generic[T]):
    """一个按输入序归并的冻结任务组。

    @param tasks 叶任务计划；空 tuple 是合法空操作
    """

    tasks: tuple[TaskSpec[T], ...]


class TaskExecutor(Protocol):
    """业务算子唯一可见的异步任务执行接口。"""

    async def run_group(self, request: TaskGroupRequest[T]) -> tuple[T, ...]:
        """执行任务组并按请求输入序返回结果。

        @param request 冻结任务组
        @return 与 request.tasks 一一对应的结果 tuple
        """
        ...


class ResourceLimiter(Protocol):
    """逻辑 profile 与 HTTP origin 的运行期容量接口。"""

    def resource_limit(self, resource_key: ResourceKey) -> AsyncContextManager[None]:
        """取得一次完整逻辑调用的 profile 许可。

        @param resource_key 逻辑资源身份
        @return 自动归还许可的异步上下文管理器
        """
        ...

    def origin_for(self, resource_key: ResourceKey) -> HttpOrigin:
        """查询逻辑资源对应的规范化 HTTP origin。

        @param resource_key 逻辑资源身份
        @return 规范化 origin
        """
        ...

    def origin_limit(self, origin: HttpOrigin) -> AsyncContextManager[None]:
        """取得一次 HTTP attempt 的 origin 许可。

        @param origin 规范化 HTTP origin
        @return 自动归还许可的异步上下文管理器
        """
        ...

    @property
    def http_connection_capacity(self) -> int:
        """返回共享 HTTPX 连接池的精确连接容量。

        @return 全部 origin 容量之和
        """
        ...

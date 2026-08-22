"""逻辑 profile 与 HTTP origin 的运行期容量所有权。"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from types import MappingProxyType
from typing import TYPE_CHECKING, AsyncIterator, Mapping

from labelkit.common.contracts.execution import HttpOrigin, ResourceKey
from labelkit.common.errors import InternalError

if TYPE_CHECKING:
    from labelkit.common.observability.obslog import MetricsSink


_logger = logging.getLogger("labelkit.runtime.resources")


class ResourceManager:
    """持有一次执行域内全部 profile 与 origin 许可。"""

    def __init__(self, capacities: Mapping[ResourceKey, int], origins: Mapping[ResourceKey, HttpOrigin],
                 metrics: MetricsSink | None):
        """冻结容量和映射，并创建共享许可。

        @param capacities 每个逻辑资源的正整数容量
        @param origins 每个逻辑资源对应的规范化 HTTP origin
        @param metrics 可选运行指标汇
        """
        frozen_capacities = dict(capacities)
        frozen_origins = dict(origins)
        self._validate(frozen_capacities, frozen_origins)
        self._capacities = MappingProxyType(frozen_capacities)
        self._origins = MappingProxyType(frozen_origins)
        self._resource_limits = {key: asyncio.Semaphore(value) for key, value in frozen_capacities.items()}
        origin_capacities = self._sum_origin_capacities(frozen_capacities, frozen_origins)
        self._origin_capacities = MappingProxyType(origin_capacities)
        self._origin_limits = {key: asyncio.Semaphore(value) for key, value in origin_capacities.items()}
        self._metrics = metrics
        self._resource_waiting = 0
        self._origin_waiting = 0

    @staticmethod
    def _validate(capacities: Mapping[ResourceKey, int], origins: Mapping[ResourceKey, HttpOrigin]) -> None:
        """验证映射在创建任何许可前完整且可用。

        @param capacities 资源容量映射
        @param origins 资源 origin 映射
        @raises InternalError 映射不完整或字段非法
        """
        if capacities.keys() != origins.keys():
            _logger.error("resource capacities and origins must have identical keys")
            raise InternalError("resource capacities and origins must have identical keys")
        for resource_key, capacity in capacities.items():
            if not _valid_resource_key(resource_key) or not _positive_int(capacity):
                _logger.error("invalid runtime resource declaration")
                raise InternalError("invalid runtime resource declaration")
            if not _valid_origin(origins[resource_key]):
                _logger.error("invalid runtime HTTP origin")
                raise InternalError("invalid runtime HTTP origin")

    @staticmethod
    def _sum_origin_capacities(capacities: Mapping[ResourceKey, int],
                               origins: Mapping[ResourceKey, HttpOrigin]) -> dict[HttpOrigin, int]:
        """按 origin 汇总不同逻辑资源的容量。

        @param capacities 资源容量映射
        @param origins 资源 origin 映射
        @return 每个 origin 的容量
        """
        result: dict[HttpOrigin, int] = {}
        for resource_key, capacity in capacities.items():
            origin = origins[resource_key]
            result[origin] = result.get(origin, 0) + capacity
        return result

    def admission_capacity(self, resource_key: ResourceKey) -> int:
        """返回 scheduler 对该资源使用的接纳上界。

        @param resource_key 逻辑资源身份
        @return 正整数接纳上界
        @raises InternalError 资源身份未知
        """
        capacity = self._capacities.get(resource_key)
        if capacity is None:
            _logger.error("unknown runtime resource key")
            raise InternalError("unknown runtime resource key")
        return capacity

    @asynccontextmanager
    async def resource_limit(self, resource_key: ResourceKey) -> AsyncIterator[None]:
        """取得并最终归还完整逻辑调用的 profile 许可。

        @param resource_key 逻辑资源身份
        @return 许可作用域
        """
        limit = self._resource_limits.get(resource_key)
        if limit is None:
            _logger.error("unknown runtime resource key")
            raise InternalError("unknown runtime resource key")
        async with self._measured_limit(limit, "resource"):
            yield

    def origin_for(self, resource_key: ResourceKey) -> HttpOrigin:
        """查询资源对应的 HTTP origin。

        @param resource_key 逻辑资源身份
        @return 规范化 HTTP origin
        @raises InternalError 资源身份未知
        """
        origin = self._origins.get(resource_key)
        if origin is None:
            _logger.error("unknown runtime resource key")
            raise InternalError("unknown runtime resource key")
        return origin

    @asynccontextmanager
    async def origin_limit(self, origin: HttpOrigin) -> AsyncIterator[None]:
        """取得并最终归还一次 HTTP attempt 的 origin 许可。

        @param origin 规范化 HTTP origin
        @return 许可作用域
        """
        limit = self._origin_limits.get(origin)
        if limit is None:
            _logger.error("unknown runtime HTTP origin")
            raise InternalError("unknown runtime HTTP origin")
        async with self._measured_limit(limit, "origin"):
            yield

    @property
    def http_connection_capacity(self) -> int:
        """返回共享 HTTPX 连接池容量。

        @return 全部 origin 容量之和
        """
        return sum(self._origin_capacities.values())

    @asynccontextmanager
    async def _measured_limit(self, limit: asyncio.Semaphore, kind: str) -> AsyncIterator[None]:
        """取得许可并记录对应等待事实。

        @param limit 目标许可
        @param kind resource 或 origin
        @return 许可作用域
        """
        waiting = limit.locked()
        started = time.perf_counter() if waiting else 0.0
        if waiting:
            self._change_waiting(kind, 1)
        try:
            await limit.acquire()
        finally:
            if waiting:
                self._change_waiting(kind, -1)
                self._record_wait(kind, started)
        try:
            yield
        finally:
            limit.release()

    def _change_waiting(self, kind: str, delta: int) -> None:
        """更新等待数和 profile 等待高水位。

        @param kind resource 或 origin
        @param delta 等待数增量
        """
        if kind == "resource":
            self._resource_waiting += delta
            if self._metrics is not None:
                self._metrics.observe_runtime_high_water("resource_wait", self._resource_waiting)
            return
        self._origin_waiting += delta

    def _record_wait(self, kind: str, started: float) -> None:
        """把许可等待耗时写入运行事实。

        @param kind resource 或 origin
        @param started 等待开始的单调时钟读数
        """
        if self._metrics is None:
            return
        elapsed_ms = max(0, round((time.perf_counter() - started) * 1000))
        key = "resource_wait_ms" if kind == "resource" else "http_pool_wait_ms"
        self._metrics.add_runtime_total(key, elapsed_ms)


def _positive_int(value: object) -> bool:
    """判断值是否是严格正整数。

    @param value 待检查值
    @return 是否为严格正整数且非 bool
    """
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_resource_key(value: object) -> bool:
    """判断逻辑资源键是否符合冻结形状。

    @param value 待检查值
    @return 是否为合法 ResourceKey
    """
    return (isinstance(value, tuple) and len(value) == 2 and value[0] in {"llm", "embedding"}
            and isinstance(value[1], str) and bool(value[1]))


def _valid_origin(value: object) -> bool:
    """判断 HTTP origin 是否已规范化且字段合法。

    @param value 待检查值
    @return 是否为合法 HttpOrigin
    """
    if not isinstance(value, tuple) or len(value) != 3:
        return False
    scheme, host, port = value
    return (scheme in {"http", "https"} and isinstance(host, str) and bool(host)
            and host == host.lower() and _positive_int(port) and port <= 65535)

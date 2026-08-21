"""v1.17 frozen layout 上的确定性 noise 分配（SPEC-SP §7.6 末段/§9.3/§11）。

NoiseAllocator 解码每个 session 的 reserve count 后，从 task timestamp 之间的
空微秒点确定性选取具体位置——只有位置选取消费 ``scenario.noise`` 随机流；
noise class 分派是纯整数最大余额（零 rng、平票按声明序）。候选空位按
(session×合法日段) 的交集剔除内部 task 点后以子区间表达，不物化逐点列表。
若模型声称有 reserve 而 allocator 找不到位置，属于 ``PlannerInternalError``。
"""
from __future__ import annotations

from dataclasses import dataclass
from random import Random

from labelkit.common.runtime.scenario.diagnostics import PlannerInternalError
from labelkit.common.runtime.scenario.model import NoiseClassSpec, NoiseSlot
from labelkit.common.runtime.scenario.quota import allocate_weights


@dataclass(frozen=True)
class NoiseSessionSpan:
    """一个 session 的 noise 空位选择输入（task 时间戳为已解码常量）。"""

    session_index: int
    start_us: int
    last_point_us: int
    task_timestamps: tuple[int, ...]


@dataclass(frozen=True)
class NoiseAllocationSpec:
    """``allocate_noise`` 的冻结参数对象。"""

    seed: int
    noise_classes: tuple[NoiseClassSpec, ...]
    noise_counts: tuple[int, ...]
    session_spans: tuple[NoiseSessionSpan, ...]
    segments: tuple[tuple[int, int], ...]


def apportion_noise_classes(total: int,
                            noise_classes: tuple[NoiseClassSpec, ...]
                            ) -> tuple[tuple[str, int], ...]:
    """按整数权重最大余额法把 noise 总数分到各 noise class（零 rng）。

    @param total noise 总数（>= 0）
    @param noise_classes noise 表（声明序）
    @return 声明序逐类计数元组（复用 quota 的 ``allocate_weights``）
    """
    weights = tuple((spec.frame_class, spec.weight) for spec in noise_classes)
    return allocate_weights(total, weights, "largest_remainder")


def _candidate_ranges(span: NoiseSessionSpan,
                      segments: tuple[tuple[int, int], ...]
                      ) -> tuple[tuple[int, int], ...]:
    """``(start, last)`` 开区间与合法日区段的交集子区间（半开）。"""
    ranges = []
    for seg_lo, seg_hi in segments:
        lo = max(seg_lo, span.start_us + 1)
        hi = min(seg_hi, span.last_point_us)
        if lo < hi:
            ranges.append((lo, hi))
    return tuple(ranges)


def _split_by_tasks(ranges: tuple[tuple[int, int], ...],
                    tasks: tuple[int, ...]) -> tuple[tuple[int, int], ...]:
    """从子区间中剔除内部 task 点，得到纯空位子区间（升序）。"""
    blocked = sorted(ts for ts in tasks)
    pieces: list[tuple[int, int]] = []
    for lo, hi in ranges:
        cursor = lo
        for ts in blocked:
            if lo <= ts < hi:
                if ts > cursor:
                    pieces.append((cursor, ts))
                cursor = ts + 1
        if cursor < hi:
            pieces.append((cursor, hi))
    return tuple(pieces)


def _point_at(pieces: tuple[tuple[int, int], ...], index: int) -> int:
    """把空位序号映射回具体微秒点（不物化逐点列表）。

    @param pieces 升序空位子区间
    @param index 空位序号
    @return 对应微秒点
    @raises PlannerInternalError 序号越界时
    """
    for lo, hi in pieces:
        size = hi - lo
        if index < size:
            return lo + index
        index -= size
    raise PlannerInternalError("noise candidate index out of range")


def _assign_noise_classes(placed: list[tuple[int, int]],
                          noise_classes: tuple[NoiseClassSpec, ...]
                          ) -> tuple[NoiseSlot, ...]:
    """类分派按权重最大余额、声明序分块（零 rng，§4.8）。

    @param placed ``(session_index, timestamp)`` 升序列表
    @param noise_classes noise 表（声明序）
    @return ``NoiseSlot`` 元组（key = ``noise:<class>:<ordinal>``）
    """
    if not placed:
        return ()
    counts = apportion_noise_classes(len(placed), noise_classes)
    slots: list[NoiseSlot] = []
    cursor = 0
    for name, count in counts:
        for ordinal in range(count):
            session_index, timestamp = placed[cursor]
            slots.append(NoiseSlot(
                key=f"noise:{name}:{ordinal}", frame_class=name,
                class_ordinal=ordinal, session_index=session_index,
                timestamp_us=timestamp))
            cursor += 1
    return tuple(slots)


def allocate_noise(spec: NoiseAllocationSpec) -> tuple[NoiseSlot, ...]:
    """§7.6/§11：确定性选取每 session 的 noise 位置并按权重分派类。

    位置选取消费 ``Random(f"{seed}:scenario.noise")``：按 session 升序对每个
    count>0 的 session 从空位索引域 ``range(total)`` 无放回抽样后排序落点；
    类分派零 rng。找不到足够空位时抛 ``PlannerInternalError``（模型声称有
    reserve 而实际无位置是实现不变量违反，不是配置错误）。

    @param spec 冻结分配参数
    @return ``NoiseSlot`` 元组
    @raises PlannerInternalError 空位不足时
    """
    rng = Random(f"{spec.seed}:scenario.noise")
    placed: list[tuple[int, int]] = []
    pairs = zip(spec.session_spans, spec.noise_counts, strict=True)
    for span, count in pairs:
        if count == 0:
            continue
        ranges = _candidate_ranges(span, spec.segments)
        pieces = _split_by_tasks(ranges, span.task_timestamps)
        total = sum(hi - lo for lo, hi in pieces)
        if total < count:
            raise PlannerInternalError(
                f"noise reserve claimed {count} slots in session "
                f"{span.session_index} but only {total} free microsecond "
                f"points exist")
        picks = sorted(_point_at(pieces, index)
                       for index in rng.sample(range(total), count))
        placed.extend((span.session_index, timestamp) for timestamp in picks)
    return _assign_noise_classes(placed, spec.noise_classes)

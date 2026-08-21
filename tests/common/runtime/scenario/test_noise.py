"""v1.17 noise 分配测试（SPEC-SP §7.6 half-even 黄金 / §11 scenario.noise 流）。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from labelkit.common.runtime.scenario import (
    NoiseAllocationSpec,
    NoiseClassSpec,
    NoiseSessionSpan,
    PlannerInternalError,
    allocate_noise,
    apportion_noise_classes,
    half_even_noise_target,
)

_DAY = 86_400_000_000


def test_half_even_goldens():
    """§7.6 黄金：0.1×15→2、半点平票取偶（2.5→2、3.5→4）、ratio 0→0。"""
    assert half_even_noise_target(Decimal("0.1"), 15) == 2
    assert half_even_noise_target(Decimal("0.1"), 25) == 2
    assert half_even_noise_target(Decimal("0.1"), 35) == 4
    assert half_even_noise_target(Decimal("0.5"), 5) == 2
    assert half_even_noise_target(Decimal("0.5"), 7) == 4
    assert half_even_noise_target(Decimal("0"), 100) == 0
    assert half_even_noise_target(Decimal("0.1"), 0) == 0


def test_apportion_noise_classes_largest_remainder_zero_rng():
    """类分派纯整数最大余额（3:1、total 10 → 8:2）、平票按声明序、零 rng。"""
    table = (NoiseClassSpec("notify", 3), NoiseClassSpec("ping", 1))
    assert apportion_noise_classes(10, table) == (("notify", 8), ("ping", 2))
    assert apportion_noise_classes(2, table) == (("notify", 2), ("ping", 0))
    single = (NoiseClassSpec("only", 1),)
    assert apportion_noise_classes(5, single) == (("only", 5),)
    assert apportion_noise_classes(0, table) == (("notify", 0), ("ping", 0))


def _alloc(seed: int, spans, counts, segments, classes):
    """allocate_noise 的直接封装。"""
    spec = NoiseAllocationSpec(
        seed=seed, noise_classes=classes, noise_counts=counts,
        session_spans=spans, segments=segments)
    return allocate_noise(spec)


def test_allocate_noise_positions_deterministic_and_interior():
    """同 seed 同位置；noise 严格在 task 首尾之间且避开 task 时间戳。"""
    spans = (NoiseSessionSpan(0, 1_000, 90_000, (1_000, 20_000, 50_000, 90_000)),)
    segments = ((0, _DAY),)
    classes = (NoiseClassSpec("notify", 1),)
    first = _alloc(7, spans, (2,), segments, classes)
    second = _alloc(7, spans, (2,), segments, classes)
    assert [slot.timestamp_us for slot in first] == [slot.timestamp_us
                                                     for slot in second]
    stamps = [slot.timestamp_us for slot in first]
    assert all(1_000 < ts < 90_000 for ts in stamps)
    assert all(ts not in (20_000, 50_000) for ts in stamps)
    assert len(set(stamps)) == len(stamps)
    assert first[0].key == "noise:notify:0"
    assert first[0].session_index == 0
    # 类分派零 rng：位置流种子不同，但分派仍是声明序最大余额
    both = _alloc(9, spans, (2,), segments,
                  (NoiseClassSpec("notify", 3), NoiseClassSpec("ping", 1)))
    assert [slot.frame_class for slot in both] == ["notify", "notify"]


def test_allocate_noise_excluded_day_hole():
    """排除日空洞不虚报：候选空位只落在合法日区段内。"""
    spans = (NoiseSessionSpan(0, 0, 3 * _DAY // 2, (0, 3 * _DAY // 2)),)
    segments = ((0, _DAY), (2 * _DAY, 3 * _DAY))
    slots = _alloc(3, spans, (4,), segments, (NoiseClassSpec("n", 1),))
    stamps = [slot.timestamp_us for slot in slots]
    assert len(stamps) == 4
    assert all(ts < _DAY for ts in stamps)


def test_allocate_noise_single_frame_session_has_no_room():
    """单帧 session 的 reserve 为 0：开区间为空，不产生 noise slot。"""
    spans = (NoiseSessionSpan(0, 5_000, 5_000, (5_000,)),)
    slots = _alloc(3, spans, (0,), ((0, _DAY),), (NoiseClassSpec("n", 1),))
    assert slots == ()


def test_allocate_noise_insufficient_points_raises_internal():
    """模型声称的 reserve 超过实际空位 → PlannerInternalError。"""
    spans = (NoiseSessionSpan(0, 100, 102, (100,)),)
    with pytest.raises(PlannerInternalError):
        _alloc(3, spans, (2,), ((0, _DAY),), (NoiseClassSpec("n", 1),))


def test_allocate_noise_multisession_order_and_class_blocks():
    """多 session：位置按 session 升序汇总，类按声明序整块分派。"""
    spans = (NoiseSessionSpan(0, 0, 10_000, (0, 5_000, 10_000)),
             NoiseSessionSpan(1, 100_000, 110_000, (100_000, 105_000, 110_000)))
    slots = _alloc(4, spans, (2, 2), ((0, _DAY),),
                   (NoiseClassSpec("a", 1), NoiseClassSpec("b", 3)))
    sessions = [slot.session_index for slot in slots]
    assert sessions == sorted(sessions)
    classes = [slot.frame_class for slot in slots]
    # 3:1 → b:3、a:1，声明序分块
    assert classes == ["a", "b", "b", "b"]
    assert [slot.key for slot in slots] == [
        "noise:a:0", "noise:b:0", "noise:b:1", "noise:b:2"]

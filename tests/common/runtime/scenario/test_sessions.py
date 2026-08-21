"""v1.17 session 层 builder 测试（SPEC-SP §7.1-§7.3；对照 /tmp/v117 探针黄金）。"""
from __future__ import annotations

from ortools.sat.python import cp_model

from labelkit.common.runtime.scenario import (
    SessionBuildSpec,
    SlotTimeline,
    build_crossing_witness,
    build_session_layer,
    proto_entry_counts,
)

_BIG = 10**7


def _synthetic(model: cp_model.CpModel, lengths: tuple[int, ...],
               spacing: int = 1000) -> tuple[list[list], SlotTimeline]:
    """构造带递增链的合成 slot 时间变量（形态对照 probe-inverse-element）。"""
    rows = []
    for i, length in enumerate(lengths):
        row = [model.NewIntVar(0, _BIG, f"ts_{i}_{p}") for p in range(length)]
        for p in range(length - 1):
            model.Add(row[p + 1] >= row[p] + spacing)
        rows.append(row)
    timeline = SlotTimeline(
        first_ts=tuple(row[0] for row in rows),
        last_ts=tuple(row[-1] for row in rows),
        end_ts=tuple(row[-1] for row in rows),
        middle_ts=tuple(row[min(1, len(row) - 1)] for row in rows),
        lengths=lengths)
    return rows, timeline


def _spec(model: cp_model.CpModel, timeline: SlotTimeline, crossed: int,
          max_span: int | None = None) -> SessionBuildSpec:
    """组装 SessionBuildSpec（ts 域 [0, _BIG]）。"""
    return SessionBuildSpec(model=model, timeline=timeline, crossed=crossed,
                            session_gap_us=500, session_max_span_us=max_span,
                            ts_low_us=0, ts_high_us=_BIG)


def test_owner_secondary_rank_consistency():
    """N=12 D=3 探针黄金：owner/rank 是置换、secondary 集合与 tail 一致。"""
    model = cp_model.CpModel()
    _, timeline = _synthetic(model, (4, 3, 3, 2, 2, 2, 2, 2, 1, 1, 1, 1))
    layer = build_session_layer(_spec(model, timeline, crossed=3))
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    assert solver.StatusName(solver.Solve(model)) == "OPTIMAL"
    owners = [solver.Value(v) for v in layer.owner_at_position]
    ranks = [solver.Value(v) for v in layer.rank_of_session]
    secondary = [solver.Value(v) for v in layer.secondary_owner]
    assert sorted(owners) == list(range(12))
    assert sorted(ranks) == list(range(9))
    assert sum(1 for v in secondary if v == 12) == 6
    real = sorted(v for v in secondary if v != 12)
    assert real == sorted(owners[9:]) == [9, 10, 11]
    assert len(set(real)) == len(real)


def test_zero_crossed_builds_no_secondary_vars():
    """D=0：不建 session permutation / secondary；crossing builder 零增量。"""
    model = cp_model.CpModel()
    _, timeline = _synthetic(model, (3, 2, 2, 1))
    layer = build_session_layer(_spec(model, timeline, crossed=0))
    assert layer.session_at_rank is None
    assert layer.rank_of_session is None
    assert layer.secondary_owner is None
    before = proto_entry_counts(model)
    build_crossing_witness(model, layer, (0, 4))
    assert proto_entry_counts(model) == before


def test_crossing_witness_orientation_primary_outside():
    """orientation A-B-A：secondary 的中帧严格落在 primary 首尾之间。"""
    model = cp_model.CpModel()
    rows, timeline = _synthetic(model, (2, 1))
    model.Add(rows[0][0] == 100)
    model.Add(rows[0][1] == 5000)
    model.Add(rows[1][0] == 3000)
    layer = build_session_layer(_spec(model, timeline, crossed=1))
    build_crossing_witness(model, layer, (0, 2))
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    assert solver.StatusName(solver.Solve(model)) == "OPTIMAL"
    assert solver.Value(layer.owner_at_position[0]) == 0
    assert solver.Value(layer.secondary_owner[0]) == 1


def test_crossing_witness_orientation_secondary_outside():
    """orientation B-A-B：单帧 primary 的中帧严格落在 secondary 首尾之间。

    固定 owner 置换使 secondary-outside 成为唯一可行 orientation（单帧
    primary 的区间为空，A-B-A 侧不可行）。
    """
    model = cp_model.CpModel()
    rows, timeline = _synthetic(model, (1, 2))
    model.Add(rows[1][0] == 100)
    model.Add(rows[1][1] == 5000)
    model.Add(rows[0][0] == 3000)
    layer = build_session_layer(_spec(model, timeline, crossed=1))
    build_crossing_witness(model, layer, (0, 2))
    model.Add(layer.owner_at_position[0] == 0)
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    assert solver.StatusName(solver.Solve(model)) == "OPTIMAL"
    assert solver.Value(layer.secondary_owner[0]) == 1


def test_session_bounds_sentinel_neutral():
    """未被 secondary 选中的 session：bounds 退化为 primary 侧（sentinel 中性值）。"""
    model = cp_model.CpModel()
    rows, timeline = _synthetic(model, (3, 2, 2))
    model.Add(rows[0][0] == 100)
    model.Add(rows[0][2] == 9000)
    model.Add(rows[1][0] == 20000)
    model.Add(rows[1][1] == 21000)
    model.Add(rows[2][0] == 30000)
    model.Add(rows[2][1] == 31000)
    layer = build_session_layer(_spec(model, timeline, crossed=1))
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    assert solver.StatusName(solver.Solve(model)) == "OPTIMAL"
    solo = [k for k in range(2)
            if solver.Value(layer.secondary_owner[k]) == 3]
    assert len(solo) == 1
    k = solo[0]
    assert solver.Value(layer.session_start[k]) == solver.Value(
        layer.primary_first[k])
    assert solver.Value(layer.session_last_point[k]) == solver.Value(
        layer.primary_last[k])
    assert solver.Value(layer.session_end[k]) == solver.Value(
        layer.primary_end[k])


def test_session_ordering_and_span():
    """相邻 session 顺序（start ≥ prev.last + gap + 1µs）与 max span 约束。"""
    model = cp_model.CpModel()
    rows, timeline = _synthetic(model, (2, 2, 2))
    for row in rows:
        model.Add(row[0] == 1000)
        model.Add(row[1] == 3000)
    layer = build_session_layer(_spec(model, timeline, crossed=0,
                                      max_span=2500))
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    status = solver.Solve(model)
    assert solver.StatusName(status) == "INFEASIBLE"
    model2 = cp_model.CpModel()
    rows2, timeline2 = _synthetic(model2, (2, 2, 2))
    layer2 = build_session_layer(_spec(model2, timeline2, crossed=0,
                                       max_span=2600))
    solver2 = cp_model.CpSolver()
    solver2.parameters.num_search_workers = 1
    assert solver2.StatusName(solver2.Solve(model2)) == "OPTIMAL"
    starts = [solver2.Value(v) for v in layer2.session_start]
    lasts = [solver2.Value(v) for v in layer2.session_last_point]
    assert all(starts[k + 1] >= lasts[k] + 501 for k in range(2))

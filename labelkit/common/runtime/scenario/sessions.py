"""v1.17 session 层 builder：owner permutation、session bounds、crossing witness
与 aggregate noise reserve（SPEC-SP §7.1-§7.3/§7.6）。

编码以 /tmp/v117 已验证探针为准：``AddInverse`` 承载 owner/session 双置换；
sentinel N 进 ``AddElement`` 用 N+1 长数组 + 中性常量（进 min 的数组填上界、
进 max 的数组填下界）；secondary 映射 O(S)，禁止 D×S bool 矩阵；crossing
witness 只为实际 secondary mapping 建立，first/middle/last 经两级 AddElement
选扁平 (slot,position) 常量偏移；D=0 时 permutation/secondary/crossing 变量
一概不建（crossing family 恒 0）。noise reserve 是 O(S) 编码：
``occupied = max(0, task_count - 2)``、``legal_open`` 按 (session×合法日段)
clip 钳非负，禁止逐 (session, frame) reified 计数。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ortools.sat.python import cp_model


@dataclass(frozen=True)
class SlotTimeline:
    """session builder 的 per-slot 时间变量与冻结长度输入。

    ``first_ts``/``last_ts``/``middle_ts`` 分别取 (slot, 0)、(slot, L-1)、
    ``(slot, min(1, L-1))`` 的扁平常量偏移（长度已冻结为 length_target）；
    point 帧 ``end_ts == last_ts``，duration 帧在 Wave 5 区分。
    """

    first_ts: tuple[Any, ...]
    last_ts: tuple[Any, ...]
    end_ts: tuple[Any, ...]
    middle_ts: tuple[Any, ...]
    lengths: tuple[int, ...]


@dataclass(frozen=True)
class SessionBuildSpec:
    """``build_session_layer`` 的冻结参数对象（规约 ≤5 参数的载体）。"""

    model: cp_model.CpModel
    timeline: SlotTimeline
    crossed: int
    session_gap_us: int
    session_max_span_us: int | None
    ts_low_us: int
    ts_high_us: int


@dataclass(frozen=True)
class _ExtendedArrays:
    """sentinel N 扩展后的被选数组（中性常量按 min/max 方向区分）。"""

    first: list[Any]
    last: list[Any]
    end: list[Any]
    middle: list[Any]
    first_bounds: tuple[int, int]
    last_bounds: tuple[int, int]


@dataclass
class SessionLayer:
    """session 层的全部求解变量（解码与 crossing/noise builder 共用）。"""

    owner_at_position: list[Any]
    position_of_owner: list[Any]
    extended: _ExtendedArrays = field(init=False)
    primary_first: list[Any] = field(default_factory=list)
    primary_last: list[Any] = field(default_factory=list)
    primary_end: list[Any] = field(default_factory=list)
    session_at_rank: list[Any] | None = None
    rank_of_session: list[Any] | None = None
    secondary_owner: list[Any] | None = None
    session_start: list[Any] = field(default_factory=list)
    session_last_point: list[Any] = field(default_factory=list)
    session_end: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class NoiseReserveSpec:
    """``build_noise_reserve`` 的冻结参数对象。"""

    model: cp_model.CpModel
    layer: SessionLayer
    lengths: tuple[int, ...]
    segments: tuple[tuple[int, int], ...]
    session_max_len: int
    ts_low_us: int
    ts_high_us: int


@dataclass
class NoiseReserve:
    """noise reserve 的求解变量（planner 加总约束、allocator 解码）。

    ``noise_units`` 是参与 ``Σ == noise_target`` 总约束的原始变量（D=0 时
    per-slot、D>0 时 per-session）；``noise_count`` 恒为 per-session 视图
    （D=0 时是仅解码用的 element 镜像）。
    """

    noise_count: list[Any] = field(default_factory=list)
    noise_units: list[Any] = field(default_factory=list)
    task_count: list[Any] = field(default_factory=list)
    legal_open: list[Any] = field(default_factory=list)


def _segments_cover_domain(spec: NoiseReserveSpec) -> bool:
    """合法日区段的并是否覆盖整个 ts 域（单日无排除日的常见形）。"""
    cursor = spec.ts_low_us
    for seg_lo, seg_hi in sorted(spec.segments):
        if seg_lo > cursor:
            return False
        cursor = max(cursor, seg_hi)
    return cursor >= spec.ts_high_us + 1


def _element(model: cp_model.CpModel, index: Any, bounds: tuple[int, int],
             array: list[Any], name: str) -> Any:
    """``AddElement`` 包装：目标域 ``[lo, hi]``，数组可混排变量与常量。

    @param model CP-SAT 模型
    @param index 下标变量（可取 sentinel N）
    @param bounds 目标变量的域上下界
    @param array 被选数组（N+1 长，末位为中性常量）
    @param name 目标变量名
    @return 目标 IntVar
    """
    target = model.NewIntVar(bounds[0], bounds[1], name)
    model.AddElement(index, array, target)
    return target


def _neutral_extensions(model: cp_model.CpModel,
                        spec: SessionBuildSpec) -> _ExtendedArrays:
    """按 min/max 方向物化 N+1 长被选数组：min 填上界、max 填下界。

    @param model CP-SAT 模型
    @param spec session 构建参数（提供 ts 域与 per-slot 变量）
    @return 扩展数组组（含 element 目标域）
    """
    neutral_high = spec.ts_high_us + 1
    neutral_low = spec.ts_low_us - 1
    timeline = spec.timeline
    return _ExtendedArrays(
        first=list(timeline.first_ts) + [model.NewConstant(neutral_high)],
        last=list(timeline.last_ts) + [model.NewConstant(neutral_low)],
        end=list(timeline.end_ts) + [model.NewConstant(neutral_low)],
        middle=list(timeline.middle_ts) + [model.NewConstant(neutral_low)],
        first_bounds=(spec.ts_low_us, neutral_high),
        last_bounds=(neutral_low, spec.ts_high_us))


def _select_primary(model: cp_model.CpModel, layer: SessionLayer,
                    ext: _ExtendedArrays, k: int) -> None:
    """session k 的 primary 量：owner index 经 AddElement 选 sequence 量。"""
    owner = layer.owner_at_position[k]
    layer.primary_first.append(_element(model, owner, ext.first_bounds,
                                        ext.first, f"pfirst_{k}"))
    layer.primary_last.append(_element(model, owner, ext.last_bounds,
                                       ext.last, f"plast_{k}"))
    layer.primary_end.append(_element(model, owner, ext.last_bounds,
                                      ext.end, f"pend_{k}"))


def _bound_var(model: cp_model.CpModel, bounds: tuple[int, int], kind: str,
               parts: list[Any], name: str) -> Any:
    """min/max 合成 session 起止（sentinel 中性值使其退化为 primary 侧）。

    @param model CP-SAT 模型
    @param bounds 目标域（min 用 first_bounds、max 用 last_bounds）
    @param kind ``min`` | ``max``
    @param parts 参与 min/max 的变量列表
    @param name 目标变量名
    @return 目标 IntVar
    """
    target = model.NewIntVar(bounds[0], bounds[1], name)
    if kind == "min":
        model.AddMinEquality(target, parts)
    else:
        model.AddMaxEquality(target, parts)
    return target


def _build_secondary_mapping(model: cp_model.CpModel, layer: SessionLayer,
                              ext: _ExtendedArrays, crossed: int) -> None:
    """§7.1 后半：session_at_rank permutation + 每 session 一次 AddElement。"""
    sessions = len(layer.primary_first)
    n = len(layer.owner_at_position)
    layer.session_at_rank = [model.NewIntVar(0, sessions - 1, f"rank2sess_{r}")
                             for r in range(sessions)]
    layer.rank_of_session = [model.NewIntVar(0, sessions - 1, f"sess2rank_{k}")
                             for k in range(sessions)]
    model.AddInverse(layer.session_at_rank, layer.rank_of_session)
    owners = layer.owner_at_position
    by_rank = [owners[sessions + r] if r < crossed else model.NewConstant(n)
               for r in range(sessions)]
    layer.secondary_owner = [
        _element(model, layer.rank_of_session[k], (0, n), by_rank,
                 f"secondary_{k}") for k in range(sessions)]
    for k in range(sessions):
        secondary = layer.secondary_owner[k]
        sec_first = _element(model, secondary, ext.first_bounds, ext.first,
                             f"sfirst_{k}")
        sec_last = _element(model, secondary, ext.last_bounds, ext.last,
                            f"slast_{k}")
        sec_end = _element(model, secondary, ext.last_bounds, ext.end,
                           f"send_{k}")
        layer.session_start.append(_bound_var(
            model, ext.first_bounds, "min",
            [layer.primary_first[k], sec_first], f"sstart_{k}"))
        layer.session_last_point.append(_bound_var(
            model, ext.last_bounds, "max",
            [layer.primary_last[k], sec_last], f"slastp_{k}"))
        layer.session_end.append(_bound_var(
            model, ext.last_bounds, "max",
            [layer.primary_end[k], sec_end], f"sendp_{k}"))


def _build_session_order(model: cp_model.CpModel, layer: SessionLayer,
                         spec: SessionBuildSpec) -> None:
    """§7.2 相邻 session 顺序与 session_max_span_us 约束。"""
    for k in range(1, len(layer.session_start)):
        model.Add(layer.session_start[k] >= layer.session_last_point[k - 1]
                  + spec.session_gap_us + 1)
    if spec.session_max_span_us is not None:
        for k in range(len(layer.session_start)):
            model.Add(layer.session_end[k] - layer.session_start[k]
                      <= spec.session_max_span_us)


def build_session_layer(spec: SessionBuildSpec) -> SessionLayer:
    """§7.1/§7.2：owner permutation、secondary 映射与 session 起止/顺序/span。

    前 S 个 owner position 是对应 session index 的 primary，后 D 位是
    secondary；D=0 时不创建 session permutation、inverse、secondary 或
    orientation 变量（session bounds 直接取 primary 侧）。

    @param spec 冻结构建参数
    @return ``SessionLayer``（含全部解码所需变量）
    """
    model, timeline = spec.model, spec.timeline
    n = len(timeline.lengths)
    sessions = n - spec.crossed
    layer = SessionLayer(
        owner_at_position=[model.NewIntVar(0, n - 1, f"owner_{p}")
                           for p in range(n)],
        position_of_owner=[model.NewIntVar(0, n - 1, f"position_{q}")
                           for q in range(n)])
    model.AddInverse(layer.owner_at_position, layer.position_of_owner)
    layer.extended = _neutral_extensions(model, spec)
    ext = layer.extended
    for k in range(sessions):
        _select_primary(model, layer, ext, k)
    if spec.crossed:
        _build_secondary_mapping(model, layer, ext, spec.crossed)
    else:
        layer.session_start = list(layer.primary_first)
        layer.session_last_point = list(layer.primary_last)
        layer.session_end = list(layer.primary_end)
    _build_session_order(model, layer, spec)
    return layer


def build_crossing_witness(model: cp_model.CpModel, layer: SessionLayer,
                           bounds: tuple[int, int]) -> None:
    """§7.3 crossing witness：只为实际 secondary mapping 建立，两 orientation 择一。

    每 crossed session（rank < D）一个 orientation bool：primary 在外时
    ``primary.first < secondary.middle < primary.last``，secondary 在外时反向。
    长度前提由严格不等式与帧链自动承载（first/last 相异需 ≥2 帧），域级前提
    由 ``derive_stream_bounds`` 检查；D=0 直接返回（family 恒 0）。

    @param model CP-SAT 模型
    @param layer 已建 session 层
    @param bounds secondary owner 的 element 目标域
    """
    if layer.session_at_rank is None:
        return
    crossed = len(layer.owner_at_position) - len(layer.session_start)
    ext = layer.extended
    for r in range(crossed):
        sess = layer.session_at_rank[r]
        owner_bounds = (0, len(layer.owner_at_position) - 1)
        primary = _element(model, sess, owner_bounds, layer.owner_at_position,
                           f"cross_owner_{r}")
        pfirst = _element(model, primary, ext.first_bounds, ext.first,
                          f"cp_first_{r}")
        plast = _element(model, primary, ext.last_bounds, ext.last,
                         f"cp_last_{r}")
        pmid = _element(model, primary, ext.last_bounds, ext.middle,
                        f"cp_mid_{r}")
        secondary = _element(model, sess, bounds, layer.secondary_owner,
                             f"cross_sec_{r}")
        sfirst = _element(model, secondary, ext.first_bounds, ext.first,
                          f"cs_first_{r}")
        slast = _element(model, secondary, ext.last_bounds, ext.last,
                         f"cs_last_{r}")
        smid = _element(model, secondary, ext.last_bounds, ext.middle,
                        f"cs_mid_{r}")
        outside = model.NewBoolVar(f"cross_orient_{r}")
        model.Add(smid >= pfirst + 1).OnlyEnforceIf(outside)
        model.Add(smid <= plast - 1).OnlyEnforceIf(outside)
        model.Add(pmid >= sfirst + 1).OnlyEnforceIf(outside.Not())
        model.Add(pmid <= slast - 1).OnlyEnforceIf(outside.Not())


def _session_task_count(model: cp_model.CpModel, layer: SessionLayer,
                        extended: list[int], k: int) -> Any:
    """session k 的 task_count：primary 长度 + secondary 长度（sentinel 取 0）。

    @param model CP-SAT 模型
    @param layer 已建 session 层
    @param extended 冻结长度数组（末位补 0 作 sentinel 中性值）
    @param k session 下标
    @return task_count IntVar
    """
    upper = max(extended)
    primary = _element(model, layer.owner_at_position[k], (0, upper),
                       extended, f"task_prim_{k}")
    if layer.secondary_owner is None:
        return primary
    secondary = _element(model, layer.secondary_owner[k], (0, upper),
                         extended, f"task_sec_{k}")
    total = model.NewIntVar(0, 2 * upper, f"task_count_{k}")
    model.Add(total == primary + secondary)
    return total


def _clip_open(spec: NoiseReserveSpec, start: Any, last: Any,
               seg: tuple[int, int], tag: str) -> Any:
    """``max(0, min(seg_hi, last) - max(seg_lo, start+1))`` 的钳非负编码（带折叠）。

    段界在 session ts 域之外的 min/max 直接折叠为仿射表达式（单日全覆盖段
    折叠后每对仅一个 clip 变量），段与 ts 域不相交时贡献常量 0——同一段数下
    中间大域变量最少，§7.6 的 O(S) 规模界不变。

    @param spec reserve 构建参数（提供模型与 ts 域）
    @param start session_start 变量
    @param last session_last_point 变量
    @param seg 合法日区段 ``(lo, hi)``
    @param tag 命名前缀（session×区段下标）
    @return 钳位后的开区间点数（IntVar 或常量 0）
    """
    model = spec.model
    ts_low, ts_high = spec.ts_low_us, spec.ts_high_us
    seg_lo, seg_hi = seg
    if seg_hi <= ts_low + 1 or seg_lo >= ts_high:
        return 0
    wide = (ts_low - 1, ts_high + 1)
    if seg_hi > ts_high:
        hi_expr: Any = last
    else:
        hi_expr = model.NewIntVar(*wide, f"open_hi_{tag}")
        model.AddMinEquality(hi_expr, [last, model.NewConstant(seg_hi)])
    if seg_lo <= ts_low:
        lo_expr: Any = start + 1
    else:
        lo_expr = model.NewIntVar(*wide, f"open_lo_{tag}")
        model.AddMaxEquality(lo_expr, [start + 1, model.NewConstant(seg_lo)])
    clip = model.NewIntVar(0, ts_high + 1 - ts_low, f"open_clip_{tag}")
    model.AddMaxEquality(clip, [hi_expr - lo_expr, model.NewConstant(0)])
    return clip


def build_noise_reserve(spec: NoiseReserveSpec) -> NoiseReserve:
    """§7.6 aggregate noise reserve 的 O(S) 编码。

    D=0（无 secondary）时按 **per-slot** 形式建模：session 就是其唯一 owner，
    cap 直接落在 slot 自身的 ts 变量上（``n_i + L_i ≤ last − first + 1``、
    ``L_i + n_i ≤ session_max_len``），完全绕开 AddElement 间接层——大规模
    单 owner 形态的默认搜索在 element 耦合的 session 级 cap 上会退化。
    D>0 时按 session 级建模：``task + noise ≤ session_max_len``、
    ``noise + occupied ≤ legal_open``（occupied = max(0, task_count-2)，
    legal_open 为 (session×合法日段) clip 之和；相邻 session 顺序约束使
    墙钟区间两两不交、端点被半开区间排除，故 occupied 不需逐帧计数）。
    区段并集覆盖整个 ts 域时 legal_open 走纯线性等价形
    ``noise + task ≤ last - start + 1``（max 两侧钳位逐点重合）。
    ``Σ noise_units == noise_target`` 的总约束与 half-even 表由 planner 追加。

    @param spec 冻结构建参数
    @return ``NoiseReserve``
    """
    model, layer = spec.model, spec.layer
    reserve = NoiseReserve()
    extended = list(spec.lengths) + [0]
    if layer.secondary_owner is None:
        _build_slot_reserve(spec, reserve)
        return reserve
    linear = _segments_cover_domain(spec)
    for k in range(len(layer.session_start)):
        task = _session_task_count(model, layer, extended, k)
        reserve.task_count.append(task)
        noise = model.NewIntVar(0, spec.session_max_len, f"noise_count_{k}")
        reserve.noise_count.append(noise)
        reserve.noise_units.append(noise)
        model.Add(task + noise <= spec.session_max_len)
        if linear:
            model.Add(noise + task <= layer.session_last_point[k]
                      - layer.session_start[k] + 1)
            continue
        pieces = [_clip_open(spec, layer.session_start[k],
                             layer.session_last_point[k], seg, f"{k}_{j}")
                  for j, seg in enumerate(spec.segments)]
        legal = model.NewIntVar(0, spec.ts_high_us + 1 - spec.ts_low_us,
                                f"legal_open_{k}")
        model.Add(legal == sum(pieces))
        reserve.legal_open.append(legal)
        occupied = model.NewIntVar(0, max(extended), f"occupied_{k}")
        model.AddMaxEquality(occupied, [task - 2, model.NewConstant(0)])
        model.Add(noise + occupied <= legal)
    return reserve


def _build_slot_reserve(spec: NoiseReserveSpec, reserve: NoiseReserve) -> None:
    """D=0 的 per-slot reserve：cap 直接落在 slot ts 变量上（零 element 耦合）。

    session k 的 noise_count 是 ``owner_at_position[k]`` 对 slot 变量的 element
    镜像（仅解码用，不进任何约束）；总约束走 ``noise_units``。
    """
    model, layer = spec.model, spec.layer
    timeline = spec.layer.extended
    for i, length in enumerate(spec.lengths):
        noise = model.NewIntVar(0, spec.session_max_len, f"noise_slot_{i}")
        reserve.noise_units.append(noise)
        model.Add(length + noise <= spec.session_max_len)
        first, last = timeline.first[i], timeline.last[i]
        model.Add(noise + length <= last - first + 1)
    for k in range(len(layer.session_start)):
        mirror = _element(model, layer.owner_at_position[k],
                          (0, spec.session_max_len), list(reserve.noise_units),
                          f"noise_count_{k}")
        reserve.noise_count.append(mirror)

"""M14 图片模式的几何检查与候选构造（CONTRACTS §7.20，SPEC-image-region-segmentation §10.5）。

这是生产代码中 Shapely 的唯一导入点。合法性在模型给出的 0–1000 网格整数上判定，通过后才投影为
原图像素真值；框架只判定、不修正。违规文本不回显坐标或模型给出的值。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

import shapely
from shapely.geometry import MultiPolygon, Polygon

from labelkit.common.contracts.post_validation import PostValidationResult
from labelkit.common.contracts.regions import CandidateRegion, ReferenceBox, RegionPolygon, RegionSet


def boxes_as_regions(boxes: tuple[ReferenceBox, ...], width: int, height: int) -> RegionSet:
    """把参考框按返回顺序直接变成矩形候选（D15，不经模型、不经网格）。

    @param boxes 已校验并冻结的参考框
    @param width 原图存储像素宽
    @param height 原图存储像素高
    @return 候选与参考框一一对应的分割结果；没有框时是合法空候选
    """
    return RegionSet(width, height, boxes, tuple(_box_candidate(box) for box in boxes))


def validate_regions(obj: Mapping[str, object], boxes: tuple[ReferenceBox, ...],
                     width: int, height: int) -> PostValidationResult:
    """按冻结顺序逐区域检查模型输出，全部通过后构造 ``RegionSet`` 作为成功证明。

    一个区域某步违规后跳过该区域其余步骤，后续区域照常检查；一步内的全部违规都报告。

    @param obj 已通过 ``segment_image_schema()`` 的模型候选
    @param boxes 本样本冻结的参考框
    @param width 原图存储像素宽
    @param height 原图存储像素高
    @return 按输出顺序排列的可修复违规，或成功证明
    """
    known = {box.box_id: box for box in boxes}
    reused: set[str] = set()
    violations: list[str] = []
    for index, region in enumerate(obj["regions"]):
        violations.extend(_region_violations(index, region, known, reused))
    if violations:
        return PostValidationResult(tuple(violations), None)
    return PostValidationResult((), RegionSet(width, height, boxes, _candidates(obj["regions"], known, width, height)))


def _region_violations(index: int, region: Mapping, known: Mapping[str, ReferenceBox],
                       reused: set[str]) -> list[str]:
    """检查一个区域的步骤 1 至 6，遇到第一个违规的步骤即停。

    @param index 区域在模型输出中的序号
    @param region 待检查的区域
    @param known 按框名索引的本样本参考框
    @param reused 已被前面的区域复用的框名；本步通过的复用写入其中
    @return 该区域第一个违规步骤的全部违规；合法时为空
    """
    prefix = f"/regions/{index}"
    box_id, polygons = region["box_id"], region["polygons"]
    if (box_id is None) == (polygons is None):
        return [f"{prefix}: exactly one of box_id and polygons must be set"]
    if box_id is not None:
        problem = _reference_violation(prefix, box_id, known, reused)
        if problem is not None:
            return [problem]
    else:
        problems = _closure_violations(prefix, polygons) or _validity_violations(prefix, polygons)
        if problems:
            return problems
    return _source_violations(prefix, region, known)


def _reference_violation(prefix: str, box_id: str, known: Mapping[str, ReferenceBox],
                         reused: set[str]) -> str | None:
    """步骤 2：``box_id`` 必须指向本样本的参考框，且同一个框最多复用一次。

    @param prefix 区域的 JSON 指针
    @param box_id 模型给出的框名，不进入违规文本
    @param known 按框名索引的本样本参考框
    @param reused 已复用的框名；通过时写入
    @return 违规文本；合法时为 None
    """
    if not known:
        return f"{prefix}/box_id: must be null when no reference boxes are provided"
    if box_id not in known:
        return f"{prefix}/box_id: unknown reference box"
    if box_id in reused:
        return f"{prefix}/box_id: reference box reused more than once"
    reused.add(box_id)
    return None


def _closure_violations(prefix: str, polygons: Sequence[Mapping]) -> list[str]:
    """步骤 3：每个环必须首末相同；未闭合进入修复，框架不代为闭合。

    @param prefix 区域的 JSON 指针
    @param polygons 模型给出的多边形部件
    @return 全部未闭合环的违规
    """
    problems: list[str] = []
    for part, polygon in enumerate(polygons):
        if polygon["exterior"][0] != polygon["exterior"][-1]:
            problems.append(f"{prefix}/polygons/{part}/exterior: ring is not closed")
        for hole_index, hole in enumerate(polygon["holes"]):
            if hole[0] != hole[-1]:
                problems.append(f"{prefix}/polygons/{part}/holes/{hole_index}: ring is not closed")
    return problems


def _validity_violations(prefix: str, polygons: Sequence[Mapping]) -> list[str]:
    """步骤 4 与 5：逐部件 OGC 合法性，再查多部件整体。

    类别取 ``is_valid_reason`` 第一个 ``[`` 之前的部分，它之后是坐标。部件之间单点相接合法，
    共边或内部相交非法，孔中岛合法。

    @param prefix 区域的 JSON 指针
    @param polygons 环已闭合的多边形部件
    @return 全部非法部件的违规；部件都合法时为整体违规或空
    """
    parts = [_part(polygon) for polygon in polygons]
    problems = [f"{prefix}/polygons/{index}: invalid geometry ({shapely.is_valid_reason(part).split('[', 1)[0]})"
                for index, part in enumerate(parts) if not part.is_valid]
    if not problems and len(parts) > 1 and not MultiPolygon(parts).is_valid:
        problems.append(f"{prefix}: parts overlap or share an edge")
    return problems


def _source_violations(prefix: str, region: Mapping, known: Mapping[str, ReferenceBox]) -> list[str]:
    """步骤 6：``source_box_ids`` 与 ``box_id`` 及本样本参考框的关系。

    @param prefix 区域的 JSON 指针
    @param region 待检查的区域
    @param known 按框名索引的本样本参考框
    @return 违规文本，不回显 id
    """
    sources = region["source_box_ids"] or ()
    if not sources:
        return []
    if region["box_id"] is not None:
        return [f"{prefix}/source_box_ids: must be null when box_id is set"]
    if not known:
        return [f"{prefix}/source_box_ids: must be null or empty when no reference boxes are provided"]
    return [f"{prefix}/source_box_ids/{index}: unknown reference box"
            for index, value in enumerate(sources) if value not in known]


def _part(polygon: Mapping) -> Polygon:
    """在网格整数坐标上构造一个部件。

    @param polygon 模型给出的部件
    @return 未经任何修正的 Shapely 多边形
    """
    return Polygon(polygon["exterior"], polygon["holes"])


def _candidates(regions: Sequence[Mapping], known: Mapping[str, ReferenceBox], width: int,
                height: int) -> tuple[CandidateRegion, ...]:
    """步骤 7：按模型输出顺序构造候选；复用区域不占 ``r<n>`` 编号。

    @param regions 全部检查通过的模型区域
    @param known 按框名索引的本样本参考框
    @param width 原图存储像素宽
    @param height 原图存储像素高
    @return 与模型输出同序的候选
    """
    candidates: list[CandidateRegion] = []
    drawn = 0
    for region in regions:
        if region["box_id"] is not None:
            candidates.append(_box_candidate(known[region["box_id"]]))
        else:
            drawn += 1
            candidates.append(_drawn_candidate(f"r{drawn}", region, width, height))
    return tuple(candidates)


def _box_candidate(box: ReferenceBox) -> CandidateRegion:
    """由参考框构造矩形候选；几何直接取框的像素矩形，保留钩子给出的数值类型。

    @param box 参考框
    @return 顶点顺序冻结为 (l,t)→(r,t)→(r,b)→(l,b)→(l,t) 的候选，标注点取框中心
    """
    left, top, right, bottom = box.bbox
    exterior = ((left, top), (right, top), (right, bottom), (left, bottom), (left, top))
    return CandidateRegion(region_id=box.box_id, polygons=(RegionPolygon(exterior, ()),), box_id=box.box_id,
                           source_box_ids=(box.box_id,), label_anchor=((left + right) / 2, (top + bottom) / 2))


def _drawn_candidate(region_id: str, region: Mapping, width: int, height: int) -> CandidateRegion:
    """由模型多边形构造候选：投影一次为像素真值，来源框按首次出现顺序去重。

    @param region_id 框架编号 ``r<n>``
    @param region 检查通过的模型区域
    @param width 原图存储像素宽
    @param height 原图存储像素高
    @return 顶点顺序不变、坐标不取整的候选
    """
    polygons = tuple(
        RegionPolygon(_project_ring(polygon["exterior"], width, height),
                      tuple(_project_ring(hole, width, height) for hole in polygon["holes"]))
        for polygon in region["polygons"])
    parts = [_part(polygon) for polygon in region["polygons"]]
    point = (parts[0] if len(parts) == 1 else MultiPolygon(parts)).point_on_surface()
    return CandidateRegion(region_id=region_id, polygons=polygons, box_id=None,
                           source_box_ids=tuple(dict.fromkeys(region["source_box_ids"] or ())),
                           label_anchor=(point.x * width / 1000, point.y * height / 1000))


def _project_ring(ring: Sequence[Sequence[int]], width: int, height: int) -> tuple[tuple[float, float], ...]:
    """把网格环投影为原图像素：``x * width / 1000``、``y * height / 1000``。

    @param ring 网格整数环
    @param width 原图存储像素宽
    @param height 原图存储像素高
    @return 同序、不取整的像素环
    """
    return tuple((x * width / 1000, y * height / 1000) for x, y in ring)

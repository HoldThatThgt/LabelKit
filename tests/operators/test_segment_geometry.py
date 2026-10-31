"""图片模式几何检查、投影与候选构造（SPEC-image-region-segmentation §10.3、§10.5）。纯逻辑，没有 LLM。"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from labelkit.common.contracts.regions import ReferenceBox, RegionSet
from labelkit.common.inference.schema_engine import segment_image_schema
from labelkit.operators.segment_geometry import boxes_as_regions, validate_regions

ROOT = Path(__file__).resolve().parents[2]
WIDTH, HEIGHT = 1080, 2340
BOXES = (ReferenceBox("n3", (0, 0, 10, 10), "甲"),
         ReferenceBox("n5", (20, 20, 40.5, 40.5), "乙"),
         ReferenceBox("n7", (50, 50, 60, 60), "丙"))


def rect(left: int, top: int, right: int, bottom: int) -> list[list[int]]:
    """闭合矩形环。"""
    return [[left, top], [right, top], [right, bottom], [left, bottom], [left, top]]


def poly(exterior: list[list[int]], *holes: list[list[int]]) -> dict:
    return {"exterior": exterior, "holes": list(holes)}


def drawn(*polygons: dict, sources: list[str] | None = None) -> dict:
    return {"box_id": None, "polygons": list(polygons), "source_box_ids": sources}


def reuse(box_id: str, sources: list[str] | None = None) -> dict:
    return {"box_id": box_id, "polygons": None, "source_box_ids": sources}


def violations(*regions: dict, boxes: tuple[ReferenceBox, ...] = BOXES) -> tuple[str, ...]:
    result = validate_regions({"regions": list(regions)}, boxes, WIDTH, HEIGHT)
    assert (result.proof is None) == bool(result.violations)
    return result.violations


def accepted(*regions: dict, boxes: tuple[ReferenceBox, ...] = BOXES) -> RegionSet:
    result = validate_regions({"regions": list(regions)}, boxes, WIDTH, HEIGHT)
    assert result.violations == ()
    assert isinstance(result.proof, RegionSet)
    return result.proof


def inside(point: tuple[float, float], ring: tuple[tuple[float, float], ...]) -> bool:
    """射线法判定点是否在环内，测试不依赖 Shapely。"""
    x, y = point
    hit = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


# ── 内部 Schema（§10.3）──────────────────────────────────────────────────────

def test_schema_matches_frozen_json():
    """Schema 与 SPEC §10.3 的冻结 JSON 相等，环只构造一次。"""
    spec = (ROOT / "docs/dev/SPEC-image-region-segmentation.md").read_text(encoding="utf-8")
    block = spec.split("### 10.3", 1)[1].split("```json", 1)[1].split("```", 1)[0]
    schema = segment_image_schema()
    assert schema == json.loads(block)
    Draft202012Validator.check_schema(schema)
    item = schema["properties"]["regions"]["items"]["properties"]["polygons"]["items"]["properties"]
    assert item["exterior"] is item["holes"]["items"]


@pytest.mark.parametrize("region", [
    drawn(poly([[0, 0], [10, 0], [10, 1000], [0, 0.5]])),
    drawn(poly([[0, 0], [10, 0], [10, 1001], [0, 0]])),
    drawn(poly([[0, 0], [10, 0], [0, 0]])),
    {"box_id": None, "polygons": [], "source_box_ids": None},
], ids=["float", "out_of_range", "too_few_vertices", "empty_polygons"])
def test_schema_decides_integer_range_and_vertex_count(region):
    errors = list(Draft202012Validator(segment_image_schema()).iter_errors({"regions": [region]}))
    assert errors


# ── 步骤 1、2：互斥与框引用 ──────────────────────────────────────────────────

def test_exactly_one_of_box_id_and_polygons():
    both_null = {"box_id": None, "polygons": None, "source_box_ids": None}
    both_set = {"box_id": "n3", "polygons": [poly(rect(0, 0, 9, 9))], "source_box_ids": None}
    assert violations(both_null, both_set) == (
        "/regions/0: exactly one of box_id and polygons must be set",
        "/regions/1: exactly one of box_id and polygons must be set",
    )


def test_box_id_reference_rules():
    assert violations(reuse("n3"), boxes=()) == (
        "/regions/0/box_id: must be null when no reference boxes are provided",)
    assert violations(reuse("secret-name")) == ("/regions/0/box_id: unknown reference box",)
    assert violations(reuse("n3"), reuse("n3")) == (
        "/regions/1/box_id: reference box reused more than once",)
    assert [region.region_id for region in accepted(reuse("n3"), reuse("n5")).regions] == ["n3", "n5"]


# ── 步骤 3–5：闭环、合法性、多部件 ──────────────────────────────────────────

def test_unclosed_ring_is_repairable():
    open_ring = [[10, 10], [90, 10], [90, 90], [10, 90]]
    assert violations(drawn(poly(open_ring))) == ("/regions/0/polygons/0/exterior: ring is not closed",)
    assert violations(drawn(poly(rect(0, 0, 900, 900), [[100, 100], [200, 100], [200, 200], [100, 200]]))) == (
        "/regions/0/polygons/0/holes/0: ring is not closed",)
    assert violations(drawn(poly(rect(0, 0, 9, 9)), poly(open_ring))) == (
        "/regions/0/polygons/1/exterior: ring is not closed",)


def test_self_intersection_reason_has_no_coordinates():
    bowtie = [[111, 222], [333, 444], [333, 222], [111, 444], [111, 222]]
    assert violations(drawn(poly(bowtie))) == ("/regions/0/polygons/0: invalid geometry (Self-intersection)",)


@pytest.mark.parametrize("ring", [
    [[5, 5], [5, 5], [5, 5], [5, 5]],
    [[0, 0], [10, 0], [0, 0], [0, 0]],
    [[0, 0], [5, 5], [10, 10], [0, 0]],
], ids=["single_point", "back_and_forth", "collinear"])
def test_zero_area_rings_are_repairable_not_exceptions(ring):
    only = violations(drawn(poly(ring)))
    assert len(only) == 1
    assert only[0].startswith("/regions/0/polygons/0: invalid geometry (")
    assert "[" not in only[0]


def test_hole_sharing_shell_edge_is_repairable():
    shell = rect(0, 0, 100, 100)
    assert violations(drawn(poly(shell, [[0, 20], [0, 80], [50, 80], [50, 20], [0, 20]]))) == (
        "/regions/0/polygons/0: invalid geometry (Self-intersection)",)
    assert violations(drawn(poly(shell, rect(200, 200, 300, 300)))) == (
        "/regions/0/polygons/0: invalid geometry (Hole lies outside shell)",)
    assert accepted(drawn(poly(shell, [[0, 50], [50, 80], [50, 20], [0, 50]]))).regions[0].region_id == "r1"


@pytest.mark.parametrize("right", [rect(100, 0, 200, 100), rect(100, 20, 200, 80)],
                         ids=["whole_edge", "partial_edge"])
def test_shared_edge_parts_are_repairable(right):
    assert violations(drawn(poly(rect(0, 0, 100, 100)), poly(right))) == (
        "/regions/0: parts overlap or share an edge",)


def test_single_point_touching_parts_accepted():
    regions = accepted(drawn(poly(rect(0, 0, 100, 100)), poly(rect(100, 100, 200, 200))))
    assert len(regions.regions[0].polygons) == 2


def test_overlapping_parts_are_repairable():
    assert violations(drawn(poly(rect(0, 0, 100, 100)), poly(rect(50, 50, 150, 150)))) == (
        "/regions/0: parts overlap or share an edge",)
    assert violations(drawn(poly(rect(0, 0, 100, 100)), poly(rect(0, 0, 100, 100)))) == (
        "/regions/0: parts overlap or share an edge",)


@pytest.mark.parametrize("flip", [False, True], ids=["shell_first", "island_first"])
def test_island_in_hole_accepted(flip):
    shell = poly(rect(0, 0, 1000, 1000), rect(100, 100, 900, 900))
    island = poly(rect(400, 400, 600, 600))
    regions = accepted(drawn(*((island, shell) if flip else (shell, island))))
    region = regions.regions[0]
    assert len(region.polygons) == 2
    assert [len(polygon.holes) for polygon in region.polygons] == ([0, 1] if flip else [1, 0])


# ── 步骤 6：source_box_ids ──────────────────────────────────────────────────

@pytest.mark.parametrize("sources", [None, []], ids=["null", "empty"])
def test_source_box_ids_null_accepted_on_reuse(sources):
    region = accepted(reuse("n5", sources)).regions[0]
    assert region.box_id == "n5"
    assert region.source_box_ids == ("n5",)


def test_source_box_ids_nonempty_on_reuse_is_repairable():
    assert violations(reuse("n5", ["n5"])) == ("/regions/0/source_box_ids: must be null when box_id is set",)


def test_source_box_ids_rules():
    assert violations(drawn(poly(rect(0, 0, 9, 9)), sources=["n3"]), boxes=()) == (
        "/regions/0/source_box_ids: must be null or empty when no reference boxes are provided",)
    assert violations(drawn(poly(rect(0, 0, 9, 9)), sources=["n3", "secret-a", "n5", "secret-b"])) == (
        "/regions/0/source_box_ids/1: unknown reference box",
        "/regions/0/source_box_ids/3: unknown reference box",
    )
    for sources in (None, []):
        assert accepted(drawn(poly(rect(0, 0, 9, 9)), sources=sources), boxes=()).regions[0].source_box_ids == ()


def test_source_box_ids_deduplicated_in_first_seen_order():
    region = accepted(drawn(poly(rect(0, 0, 9, 9)), sources=["n5", "n5", "n3"])).regions[0]
    assert region.source_box_ids == ("n5", "n3")
    assert region.box_id is None


def test_violations_collected_per_region_in_output_order():
    """一个区域某步违规后跳过其余步骤；后续区域照常检查；违规不含坐标或模型值。"""
    found = violations(
        drawn(poly(rect(0, 0, 9, 9))),                                          # 合法
        reuse("secret-name", ["also-secret"]),                                  # 2b 之后不再报 6a
        drawn(poly([[123, 456], [789, 456], [789, 987]]), sources=["secret"]),  # 3 之后不再报 6c
        drawn(poly([[1, 1], [9, 9], [9, 1], [1, 9], [1, 1]]),
              poly([[20, 20], [29, 29], [29, 20], [20, 29], [20, 20]])),        # 同一步的两个部件都报
        {"box_id": None, "polygons": None, "source_box_ids": None},
    )
    assert found == (
        "/regions/1/box_id: unknown reference box",
        "/regions/2/polygons/0/exterior: ring is not closed",
        "/regions/3/polygons/0: invalid geometry (Self-intersection)",
        "/regions/3/polygons/1: invalid geometry (Self-intersection)",
        "/regions/4: exactly one of box_id and polygons must be set",
    )
    text = "\n".join(found)
    assert "secret" not in text
    for value in ("123", "456", "789", "987"):
        assert value not in text


# ── 第 7 步：构造 ───────────────────────────────────────────────────────────

def test_region_ids_use_box_name_and_r_numbering_skips_reused():
    regions = accepted(drawn(poly(rect(0, 0, 9, 9))), reuse("n5"), drawn(poly(rect(20, 20, 29, 29))), reuse("n3"))
    assert [region.region_id for region in regions.regions] == ["r1", "n5", "r2", "n3"]
    assert [region.box_id for region in regions.regions] == [None, "n5", None, "n3"]
    assert [region.source_box_ids for region in regions.regions] == [(), ("n5",), (), ("n3",)]


def test_region_order_follows_model_output():
    regions = accepted(reuse("n7"), reuse("n3"), drawn(poly(rect(0, 0, 9, 9))), reuse("n5"))
    assert [region.region_id for region in regions.regions] == ["n7", "n3", "r1", "n5"]
    assert regions.boxes is BOXES
    assert (regions.width, regions.height) == (WIDTH, HEIGHT)


def test_grid_projection_formula_without_rounding():
    exterior = [[333, 7], [1000, 7], [1000, 1000], [333, 7]]
    region = accepted(drawn(poly(exterior))).regions[0]
    projected = region.polygons[0].exterior
    assert projected == tuple((x * WIDTH / 1000, y * HEIGHT / 1000) for x, y in exterior)
    assert projected[0] == (359.64, 16.38)
    assert all(type(value) is float for vertex in projected for value in vertex)
    assert region.polygons[0].holes == ()


def test_holes_are_projected_with_the_same_formula():
    region = accepted(drawn(poly(rect(0, 0, 1000, 1000), rect(100, 200, 300, 400)))).regions[0]
    assert region.polygons[0].holes == (
        ((108.0, 468.0), (324.0, 468.0), (324.0, 936.0), (108.0, 936.0), (108.0, 468.0)),)


@pytest.mark.parametrize("region, part", [
    (drawn(poly([[0, 0], [1000, 0], [1000, 100], [100, 100], [100, 1000], [0, 1000], [0, 0]])), 0),
    (drawn(poly(rect(0, 0, 1000, 1000), rect(50, 50, 950, 950))), 0),
    (drawn(poly(rect(0, 0, 10, 10)), poly(rect(100, 100, 900, 900))), 1),
], ids=["concave_l_shape", "ring_with_big_hole", "multipolygon_widest_part"])
def test_label_anchor_inside_region(region, part):
    """标注点落在区域内（不在孔里），在网格上取点后投影，是浮点。"""
    candidate = accepted(region).regions[0]
    polygon = candidate.polygons[part]
    assert all(type(value) is float for value in candidate.label_anchor)
    assert inside(candidate.label_anchor, polygon.exterior)
    assert not any(inside(candidate.label_anchor, hole) for hole in polygon.holes)


def test_reused_box_anchor_is_box_center_and_keeps_numeric_types():
    region = accepted(reuse("n5")).regions[0]
    assert region.label_anchor == (30.25, 30.25)
    assert region.polygons[0].exterior == ((20, 20), (40.5, 20), (40.5, 40.5), (20, 40.5), (20, 20))
    assert [type(value) for value in region.polygons[0].exterior[0]] == [int, int]
    assert region.polygons[0].holes == ()


def test_duplicate_adjacent_vertices_preserved():
    exterior = [[0, 0], [0, 0], [100, 0], [100, 100], [100, 100], [0, 100], [0, 0]]
    projected = accepted(drawn(poly(exterior))).regions[0].polygons[0].exterior
    assert len(projected) == len(exterior)
    assert projected[0] == projected[1]
    assert projected[3] == projected[4]


def test_empty_model_list_builds_legal_empty_region_set():
    regions = accepted()
    assert regions.regions == ()
    assert regions.boxes is BOXES


def test_boxes_as_regions_is_hook_order_rectangles():
    regions = boxes_as_regions(BOXES, WIDTH, HEIGHT)
    assert [region.region_id for region in regions.regions] == ["n3", "n5", "n7"]
    assert [region.box_id for region in regions.regions] == ["n3", "n5", "n7"]
    assert [region.source_box_ids for region in regions.regions] == [("n3",), ("n5",), ("n7",)]
    assert regions.boxes is BOXES
    assert boxes_as_regions((), WIDTH, HEIGHT).regions == ()


# ── 静态守护 ────────────────────────────────────────────────────────────────

def _production_trees():
    for path in sorted((ROOT / "labelkit").rglob("*.py")):
        yield path.relative_to(ROOT).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def test_shapely_imported_only_in_segment_geometry():
    importers = set()
    for relative, tree in _production_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == "shapely" or name.startswith("shapely.") for name in names):
                importers.add(relative)
    assert importers == {"labelkit/operators/segment_geometry.py"}


def test_no_automatic_geometry_repair_calls():
    """全仓不调用 make_valid、buffer(0)、orient_polygons、set_precision，也不引用这些名字。"""
    forbidden = {"make_valid", "orient_polygons", "set_precision"}
    hits = []
    for relative, tree in _production_trees():
        for node in ast.walk(tree):
            name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
            if name in forbidden:
                hits.append((relative, name))
            if isinstance(node, ast.ImportFrom) and any(alias.name in forbidden for alias in node.names):
                hits.append((relative, node.module))
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "buffer"):
                hits.append((relative, "buffer"))
    assert hits == []

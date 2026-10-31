"""图片模式输出：``_meta.regions`` 与记录载荷（SPEC-image-region-segmentation §16.1、§16.2）。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from labelkit import TOOL_VERSION
from labelkit.common.config.model import SegmentConfig
from labelkit.common.contracts.regions import CandidateRegion, ReferenceBox, RegionPolygon, RegionSet
from labelkit.common.contracts.types import ImageRef, PipelineItem, Record, RecordRef, StageError
from labelkit.operators.annotation_finalization import annotation_record_context
from labelkit.operators.segment_geometry import boxes_as_regions
from tests.operators.test_emitter import (
    make_cfg, make_item, make_record, make_seq_record, make_ui_record, read_jsonl, run_emitter,
)

SEGMENT = SegmentConfig(enabled=True, mode="image", instruction="分割")
BOX = ReferenceBox("n12", (43, 140, 821, 1264), "Button 登录")
OUTER = ((10.5, 20.25), (900.0, 20.25), (900.0, 800.75), (10.5, 800.75), (10.5, 20.25))
HOLE = ((100.0, 100.0), (100.0, 200.0), (200.0, 200.0), (100.0, 100.0))
ISLAND = ((120.0, 110.0), (140.0, 110.0), (140.0, 150.0), (120.0, 110.0))
DRAWN = CandidateRegion("r1", (RegionPolygon(OUTER, (HOLE,)),), None, ("n12",), (50.0, 60.0))
MULTI = CandidateRegion("r2", (RegionPolygon(OUTER, (HOLE,)), RegionPolygon(ISLAND, ())), None, (), (50.0, 60.0))
REUSED = boxes_as_regions((BOX,), 1080, 2340).regions[0]
REGIONS = RegionSet(1080, 2340, (BOX,), (DRAWN, REUSED, MULTI))


def image_record(*, rec_id: str = "d" * 16, companion: str | None = None) -> Record:
    image = ImageRef(path=Path("data/image_7.png"), format="png", size_bytes=1)
    return Record(id=rec_id, modality="ui", text=None, raw=None, ui_tree=None, image=image,
                  ref=RecordRef("image_7.png", None, 7, ()), companion=b"RAW-COMPANION" if companion else None,
                  companion_path=companion, image_sha256="a" * 64, companion_sha256="b" * 64 if companion else None)


def image_item(regions: RegionSet = REGIONS, **kw) -> PipelineItem:
    item = make_item(record=image_record(**kw), annotated=kw.pop("annotated", True))
    item.regions = regions
    return item


def main_rows(cfg) -> list[dict]:
    return read_jsonl(Path(cfg.run.output))


def cfg_for(tmp_path, **kw):
    return make_cfg(tmp_path, modality="ui", segment=SEGMENT, quality_rubric="inline", **kw)


def test_meta_key_order_in_image_mode(tmp_path):
    cfg = cfg_for(tmp_path)
    run_emitter(cfg, [image_item()])
    meta = main_rows(cfg)[0]["_meta"]
    assert list(meta) == ["id", "run", "source", "stream", "regions", "scores", "dedup", "classification",
                          "annotation", "verification"]
    assert meta["stream"] is None
    assert list(meta["regions"]) == ["image", "boxes", "items"]
    assert meta["regions"]["image"] == {"width": 1080, "height": 2340}
    assert meta["regions"]["boxes"] == [{"id": "n12", "bbox": [43, 140, 821, 1264], "text": "Button 登录"}]
    assert list(meta["regions"]["items"][0]) == ["region_id", "geometry", "box_id", "source_box_ids"]


def test_fixture_pixel_geometry_preserved_in_meta_regions(tmp_path):
    """固定候选夹具：像素几何、环序与顶点顺序逐值不变，不取整。"""
    cfg = cfg_for(tmp_path)
    run_emitter(cfg, [image_item()])
    items = main_rows(cfg)[0]["_meta"]["regions"]["items"]
    assert [item["region_id"] for item in items] == ["r1", "n12", "r2"]
    assert items[0]["geometry"]["coordinates"] == [[list(v) for v in OUTER], [list(v) for v in HOLE]]
    assert items[1]["geometry"]["coordinates"] == [
        [[43, 140], [821, 140], [821, 1264], [43, 1264], [43, 140]]]
    assert all(type(value) is int for vertex in items[1]["geometry"]["coordinates"][0] for value in vertex)
    assert items[2]["geometry"]["coordinates"] == [
        [[list(v) for v in OUTER], [list(v) for v in HOLE]], [[list(v) for v in ISLAND]]]
    assert (items[0]["box_id"], items[0]["source_box_ids"]) == (None, ["n12"])
    assert (items[1]["box_id"], items[1]["source_box_ids"]) == ("n12", ["n12"])


def test_polygon_and_multipolygon_geometry_shape(tmp_path):
    cfg = cfg_for(tmp_path)
    run_emitter(cfg, [image_item()])
    items = main_rows(cfg)[0]["_meta"]["regions"]["items"]
    assert [item["geometry"]["type"] for item in items] == ["Polygon", "Polygon", "MultiPolygon"]
    assert list(items[0]["geometry"]) == ["type", "coordinates"]


def test_rectangle_output_keeps_original_polygon_in_meta(tmp_path):
    """用户对象转成矩形时，``_meta.regions`` 仍留存转换前的原多边形。"""
    cfg = cfg_for(tmp_path)
    item = image_item()
    item.annotation.output["topic"] = "10.5,20.25,900.0,800.75"   # 后处理钩子写入的外接矩形
    run_emitter(cfg, [item])
    row = main_rows(cfg)[0]
    assert row["topic"] == "10.5,20.25,900.0,800.75"
    assert row["_meta"]["regions"]["items"][0]["geometry"]["coordinates"][0][1] == [900.0, 20.25]


def test_boxes_only_item_ids_align_with_boxes(tmp_path):
    cfg = cfg_for(tmp_path)
    boxes = (ReferenceBox("1", (0, 0, 1, 1), ""), ReferenceBox("2", (0.5, 0.5, 2, 2), "t"))
    run_emitter(cfg, [image_item(boxes_as_regions(boxes, 100, 100))])
    regions = main_rows(cfg)[0]["_meta"]["regions"]
    assert [box["id"] for box in regions["boxes"]] == ["1", "2"]
    for item, box in zip(regions["items"], regions["boxes"], strict=True):
        assert item["region_id"] == item["box_id"] == box["id"]
        assert item["source_box_ids"] == [box["id"]]


def test_empty_candidates_still_carry_the_block(tmp_path):
    cfg = cfg_for(tmp_path)
    run_emitter(cfg, [image_item(RegionSet(100, 200, (), ()))])
    assert main_rows(cfg)[0]["_meta"]["regions"] == {"image": {"width": 100, "height": 200}, "boxes": [], "items": []}


def test_sidecar_carries_regions(tmp_path):
    cfg = cfg_for(tmp_path, meta_mode="sidecar")
    run_emitter(cfg, [image_item()])
    assert "_meta" not in main_rows(cfg)[0]
    sidecar = read_jsonl(Path(cfg.paths.sidecar))[0]["_meta"]
    assert list(sidecar)[3:6] == ["stream", "regions", "scores"]
    assert [item["region_id"] for item in sidecar["regions"]["items"]] == ["r1", "n12", "r2"]


def test_non_image_output_has_no_regions_key(tmp_path):
    text_cfg = make_cfg(tmp_path)
    run_emitter(text_cfg, [make_item(record=make_record())])
    assert list(main_rows(text_cfg)[0]["_meta"]) == ["id", "run", "source", "stream", "scores", "dedup",
                                                     "classification", "annotation", "verification"]
    ui_dir = tmp_path / "ui"
    ui_dir.mkdir()
    ui_cfg = make_cfg(ui_dir, modality="ui", quality_rubric="default:ui")
    run_emitter(ui_cfg, [make_item(record=make_ui_record())])
    assert "regions" not in main_rows(ui_cfg)[0]["_meta"]


def test_annotate_off_payload_has_image_and_companion_paths(tmp_path):
    cfg = cfg_for(tmp_path, annotate_enabled=False)
    companion_path = str(Path("data/image_8.xml"))
    plain = make_item(record=image_record(), annotated=False)
    plain.regions = REGIONS
    with_companion = make_item(record=image_record(rec_id="e" * 16, companion=companion_path), annotated=False)
    with_companion.regions = REGIONS
    run_emitter(cfg, [plain, with_companion])
    first, second = main_rows(cfg)
    assert list(first) == ["image_path", "_meta"]
    assert first["image_path"] == str(Path("data/image_7.png"))
    assert list(second) == ["image_path", "companion_path", "_meta"]
    assert second["companion_path"] == companion_path
    assert "RAW-COMPANION" not in json.dumps(second, ensure_ascii=False)
    assert first["_meta"]["annotation"] is None
    assert first["_meta"]["regions"]["items"][0]["region_id"] == "r1"


@pytest.mark.parametrize("tier", ["refs", "full"])
def test_rejects_full_payload_in_image_mode(tmp_path, tier):
    cfg = cfg_for(tmp_path, rejects=tier)
    companion_path = str(Path("data/image_7.xml"))
    item = make_item(record=image_record(companion=companion_path), annotated=False, status="failed", errors=(
        StageError(stage="segment", kind="companion_invalid", message="box_parser rejected companion",
                   retryable=False),))
    run_emitter(cfg, [item])
    row = read_jsonl(Path(cfg.paths.rejects))[0]
    assert row["_meta"]["stage"] == "segment"
    assert row["_meta"]["reason"] == "companion_invalid"
    assert row["_meta"]["errors"] == ["box_parser rejected companion"]
    assert "regions" not in row["_meta"]
    if tier == "full":
        assert row["record"] == {"image_path": str(Path("data/image_7.png")), "companion_path": companion_path}
    else:
        assert "record" not in row


def test_rejects_segmentation_invalid_attribution(tmp_path):
    cfg = cfg_for(tmp_path, rejects="full")
    item = make_item(record=image_record(), annotated=False, status="failed", errors=(
        StageError(stage="segment", kind="segmentation_invalid", message="m", retryable=False),))
    run_emitter(cfg, [item])
    row = read_jsonl(Path(cfg.paths.rejects))[0]
    assert (row["_meta"]["stage"], row["_meta"]["reason"]) == ("segment", "segmentation_invalid")
    assert row["record"] == {"image_path": str(Path("data/image_7.png"))}
    assert "raw_last_output" not in row


def test_non_image_payload_unchanged(tmp_path):
    cfg = make_cfg(tmp_path, modality="ui", quality_rubric="default:ui", annotate_enabled=False)
    record = make_ui_record()
    run_emitter(cfg, [make_item(record=record, annotated=False)])
    row = main_rows(cfg)[0]
    assert list(row) == ["ui_tree", "image_path", "_meta"]
    assert row["ui_tree"] == record.ui_tree.serialize()
    assert row["image_path"] == str(record.image.path)


# 字面量取自图片分割改动前提交 c42fef3 的 emitter 实跑结果，钉住未改动的基线。
# 工具版本与随操作系统变化的路径分隔符用占位符，换行符按 ``os.linesep`` 还原。
FROZEN_TEXT_ROW = (
    '{"intent": "writing_assist", "topic": "请假条", "difficulty": "easy", "_meta": {"id": "aaaaaaaaaaaaaaaa", '
    '"run": {"tool": "@TOOL@", "started_at": "2026-07-02T10:27:41+00:00", "project_file": "project.toml", '
    '"rubric": "default:text", "seed": 7}, '
    '"source": {"file": "ime-2026-06.jsonl", "line_no": 1, "generated_from": [], "fields": {}, "generator": null}, '
    '"stream": null, "scores": null, "dedup": {"kind": "unique"}, "classification": null, '
    '"annotation": {"model": "glm-5.2", "attempts": 1}, "verification": null}}')
FROZEN_UI_ROW = (
    '{"ui_tree": "Button \\"登录\\" [0,0,10,10]", "image_path": @IMAGE_PATH@, "_meta": {"id": "cccccccccccccccc", '
    '"run": {"tool": "@TOOL@", "started_at": "2026-07-02T10:27:41+00:00", "project_file": "project.toml", '
    '"rubric": "default:ui", "seed": 7}, '
    '"source": {"file": "b/uitree_2.jsonl", "pair_index": 2, "generated_from": [], "fields": {}, "generator": null}, '
    '"stream": null, "scores": null, "dedup": {"kind": "unique"}, "classification": null, '
    '"annotation": null, "verification": null}}')
FROZEN_SEQUENCE_ROW = (
    '{"intent": "writing_assist", "topic": "请假条", "difficulty": "easy", "_meta": {"id": "eeeeeeeeeeeeeeee", '
    '"run": {"tool": "@TOOL@", "started_at": "2026-07-02T10:27:41+00:00", "project_file": "project.toml", '
    '"rubric": "default:trajectory", "seed": 7}, '
    '"source": {"file": "ime-2026-06.jsonl", "line_no": 3, "generated_from": [], "fields": {}, "generator": null}, '
    '"stream": {"episode_id": "eeeeeeeeeeeeeeee", "session_id": null, '
    '"order_span": ["ime-2026-06.jsonl:3", "ime-2026-06.jsonl:5"], "member_count": 2, '
    '"member_ids": ["1111111111111111", "2222222222222222"], "member_positions": [], '
    '"member_sources": [{"file": "ime-2026-06.jsonl", "line_no": 3}, {"file": "ime-2026-06.jsonl", "line_no": 5}], '
    '"capacity": null, "repaired": false, "degraded": null, "steps": null}, '
    '"scores": null, "dedup": {"kind": "unique"}, "classification": null, '
    '"annotation": {"model": "glm-5.2", "attempts": 1}, '
    '"verification": {"verdict": "pass", "rounds": 1, "defects": []}}}')


def frozen_rows() -> dict[str, tuple[dict, PipelineItem, str]]:
    """@return 行名 → （make_cfg 覆盖项、信封、冻结行文本）；序列行走 ``sequence_resolved`` 的三处分支。"""
    members = [make_record("1" * 16, 3), make_record("2" * 16, 5)]
    return {
        "text": ({}, make_item(record=make_record()), FROZEN_TEXT_ROW),
        "ui-annotate-off": (
            {"modality": "ui", "quality_rubric": "default:ui", "annotate_enabled": False},
            make_item(record=make_ui_record(), annotated=False), FROZEN_UI_ROW),
        "sequence": (
            {"segment": SegmentConfig(enabled=True, mode="sequence", sequence_resolved=True), "quality_rubric": ""},
            make_item(record=make_seq_record(members), verified=True), FROZEN_SEQUENCE_ROW),
    }


@pytest.mark.parametrize("name", ["text", "ui-annotate-off", "sequence"])
def test_non_image_rows_match_frozen_bytes(tmp_path, name):
    """非图片行与改动前的基线逐字节相等：键序、取值、分隔符、非 ASCII 与换行符都算在内。"""
    overrides, item, row = frozen_rows()[name]
    cfg = make_cfg(tmp_path, **overrides)
    run_emitter(cfg, [item])
    expected = row.replace("@TOOL@", TOOL_VERSION).replace("@IMAGE_PATH@", json.dumps(str(Path("b/image_2.png"))))
    assert Path(cfg.run.output).read_bytes() == (expected + os.linesep).encode("utf-8")


def test_stage_output_round_trips_through_emitter(tmp_path):
    """阶段产物直接进 emitter：D15 候选的 ``_meta.regions`` 与钩子框逐值相等。"""
    from tests.operators.test_segment_image import make_cfg as stage_cfg, make_item as stage_item, run as run_stage

    box = {"id": "n12", "bbox": [43, 140.5, 821, 1264], "text": "登录"}
    item = stage_item(tmp_path, companion=b"x")
    run_stage(stage_cfg(instruction="", hook=lambda companion, image: [box]), [item])
    item.dedup = None
    cfg = cfg_for(tmp_path, annotate_enabled=False)
    run_emitter(cfg, [item])
    regions = main_rows(cfg)[0]["_meta"]["regions"]
    assert regions["boxes"] == [box]
    assert regions["items"] == [{
        "region_id": "n12",
        "geometry": {"type": "Polygon",
                     "coordinates": [[[43, 140.5], [821, 140.5], [821, 1264], [43, 1264], [43, 140.5]]]},
        "box_id": "n12", "source_box_ids": ["n12"]}]


def test_items_equal_context_regions(tmp_path):
    """``items[]`` 与样本上下文的 ``regions[]`` 逐元素相同，``boxes``、``image`` 同形（同一投影）。"""
    cfg = cfg_for(tmp_path)
    item = image_item()
    run_emitter(cfg, [item])
    regions = main_rows(cfg)[0]["_meta"]["regions"]
    context = annotation_record_context(item.record, item.regions)
    assert regions["items"] == context["regions"]
    assert regions["boxes"] == context["boxes"]
    assert regions["image"] == context["image"]
    assert context["sample_id"] == item.record.id == main_rows(cfg)[0]["_meta"]["id"]
    assert context["source"]["file"] == main_rows(cfg)[0]["_meta"]["source"]["file"]
    assert context["source"]["pair_index"] == main_rows(cfg)[0]["_meta"]["source"]["pair_index"]

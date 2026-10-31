"""图片样本的后处理与校验器第二入参（SPEC-image-region-segmentation §13.1，AC-07、AC-134、AC-135）。"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
from types import SimpleNamespace

import pytest

from labelkit.common.contracts.regions import ReferenceBox, RegionSet, project_regions
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import PipelineItem, Record, RecordRef
from labelkit.common.extensions.hooks import ResolvedHook
from labelkit.common.inference.schema_engine import SchemaEngine
from labelkit.operators.annotate import AnnotateStage, annotate_record
from labelkit.operators.annotation_finalization import annotation_record_context, annotation_transforms
from labelkit.operators.segment_geometry import boxes_as_regions, validate_regions
from tests.common.config.test_segment_mode_config import Env
from tests.operators.test_annotate_image import (
    GOOD,
    RECORD,
    SCHEMA,
    _Calibrator,
    _Metrics,
    _QueueLLM,
    _SerialTasks,
    make_cfg,
)
from tests.operators.test_emitter_image import DRAWN, HOLE, ISLAND, MULTI, OUTER, REGIONS, image_item, cfg_for, main_rows
from tests.operators.test_emitter import run_emitter

BOX = ReferenceBox("n12", (43, 140, 821, 1264), "Button 登录")
SPEC_REGIONS = boxes_as_regions((BOX,), 1080, 2340)
SPEC_RECORD = dataclasses.replace(RECORD, id="8f3c2a91d0b47e65", ref=RecordRef("image_7.png", None, 7, ()))


def run_annotate(cfg, llm, regions=SPEC_REGIONS, validator=None) -> PipelineItem:
    metrics = _Metrics()
    engine = SchemaEngine(SCHEMA, llm, cfg.output, metrics, validator=validator)
    ctx = RunContext(cfg=cfg, llm=SimpleNamespace(calibrator=_Calibrator(10)), schema_engine=engine, rng=None,
                     batch_no=1, metrics=metrics, tasks=_SerialTasks(), task_namespace="t:run:batch:1:stage:annotate")
    item = PipelineItem(record=SPEC_RECORD, regions=regions)
    asyncio.run(AnnotateStage(cfg).run([item], ctx))
    return item


def hooked_cfg(function) -> object:
    base = make_cfg()
    return dataclasses.replace(base, annotate=dataclasses.replace(
        base.annotate, resolved_postprocessor=ResolvedHook("hooks.py:fn", function)))


def test_context_shape_matches_spec():
    """逐值与键序都等于 SPEC §13.1 的例子。"""
    context = annotation_record_context(SPEC_RECORD, SPEC_REGIONS)
    assert context == {
        "sample_id": "8f3c2a91d0b47e65",
        "source": {"file": "image_7.png", "pair_index": 7},
        "image": {"width": 1080, "height": 2340},
        "boxes": [{"id": "n12", "bbox": [43, 140, 821, 1264], "text": "Button 登录"}],
        "regions": [{
            "region_id": "n12",
            "geometry": {"type": "Polygon",
                         "coordinates": [[[43, 140], [821, 140], [821, 1264], [43, 1264], [43, 140]]]},
            "box_id": "n12",
            "source_box_ids": ["n12"],
        }],
    }
    assert list(context) == ["sample_id", "source", "image", "boxes", "regions"]
    assert list(context["source"]) == ["file", "pair_index"]
    assert list(context["boxes"][0]) == ["id", "bbox", "text"]
    assert list(context["regions"][0]) == ["region_id", "geometry", "box_id", "source_box_ids"]
    # 上下文只有 JSON，没有路径对象、句柄或像素数据。
    json.dumps(context, ensure_ascii=False)


def test_empty_candidates_still_carry_the_sample():
    context = annotation_record_context(SPEC_RECORD, RegionSet(1080, 2340, (), ()))
    assert context["boxes"] == [] and context["regions"] == []
    assert context["image"] == {"width": 1080, "height": 2340}


def test_postprocessor_and_validator_receive_same_context():
    """两者同值；工程函数改动第二入参不会污染校验器，也不会改动 RegionSet。"""
    received = {}

    def postprocess(candidate, context):
        received["hook"] = copy.deepcopy(context)
        context["regions"][0]["geometry"]["coordinates"][0][0][0] = -1
        context["boxes"].clear()
        return candidate

    def validator(obj, context):
        received["validator"] = copy.deepcopy(context)
        return []

    item = run_annotate(hooked_cfg(postprocess), _QueueLLM(GOOD), validator=validator)
    assert item.status == "active"
    expected = annotation_record_context(SPEC_RECORD, SPEC_REGIONS)
    assert received["hook"] == expected
    assert received["validator"] == expected
    assert annotation_record_context(SPEC_RECORD, SPEC_REGIONS) == expected


def test_validator_receives_context_without_postprocessor():
    received = []
    item = run_annotate(make_cfg(), _QueueLLM(GOOD),
                        validator=lambda obj, context: received.append(context) or [])
    assert item.status == "active"
    assert received == [annotation_record_context(SPEC_RECORD, SPEC_REGIONS)]


def test_non_image_context_unchanged():
    """文本与成员帧仍是 raw 本身，序列恒为 None，ui 记录 raw 为 None；帧路径传 None。"""
    raw = {"text": "正文", "case": 1}
    text = Record(id="t", modality="text", text="正文", raw=raw, ui_tree=None, image=None,
                  ref=RecordRef("in.jsonl", 3, None, ()))
    assert annotation_record_context(text, None) is raw
    sequence = dataclasses.replace(text, kind="sequence", raw=None, members=(text,))
    assert annotation_record_context(sequence, None) is None
    assert annotation_record_context(dataclasses.replace(SPEC_RECORD, image=None), None) is None
    finalizer, _projector = annotation_transforms(text, make_cfg(), None, None, None)
    assert finalizer.record is raw


def test_transforms_bind_the_image_context():
    finalizer, _projector = annotation_transforms(SPEC_RECORD, make_cfg(), None, None, SPEC_REGIONS)
    assert finalizer.record == annotation_record_context(SPEC_RECORD, SPEC_REGIONS)


def test_context_is_fresh_deep_copy():
    """每次构造都不共享容器；每个合格候选的工程函数也各拿一份新副本。"""
    first = annotation_record_context(SPEC_RECORD, REGIONS)
    second = annotation_record_context(SPEC_RECORD, REGIONS)
    assert first == second and first is not second
    first["regions"][0]["geometry"]["coordinates"][0][0][0] = -1
    first["boxes"][0]["bbox"][0] = -1
    first["image"]["width"] = -1
    first["source"]["file"] = "x"
    assert second == annotation_record_context(SPEC_RECORD, REGIONS)
    assert second["regions"][0]["geometry"]["coordinates"][0][0][0] == OUTER[0][0]
    assert REGIONS.regions[0].polygons[0].exterior == OUTER

    seen = []

    def postprocess(candidate, context):
        seen.append(context)
        if len(seen) == 1:
            context["regions"].clear()
        return candidate

    cfg = hooked_cfg(postprocess)
    # 校验器打回第一轮：同一请求的第二个候选仍拿到完整上下文。
    verdicts = iter((["again"], []))
    item = run_annotate(cfg, _QueueLLM(GOOD, GOOD), validator=lambda obj, context: next(verdicts))
    assert item.status == "active"
    assert len(seen) == 2 and seen[0] is not seen[1]
    assert seen[1]["regions"] != []


def test_context_geometry_equals_fixture():
    """固定候选夹具：孔洞、多部件、环序与顶点顺序逐值不变，不取整，与 _meta.regions 同投影。"""
    context = annotation_record_context(SPEC_RECORD, REGIONS)
    by_id = {region["region_id"]: region for region in context["regions"]}
    assert by_id["r1"]["geometry"] == {"type": "Polygon",
                                       "coordinates": [[list(v) for v in OUTER], [list(v) for v in HOLE]]}
    assert by_id["r1"]["box_id"] is None and by_id["r1"]["source_box_ids"] == list(DRAWN.source_box_ids)
    assert by_id["r2"]["geometry"] == {"type": "MultiPolygon",
                                       "coordinates": [[[list(v) for v in OUTER], [list(v) for v in HOLE]],
                                                       [[list(v) for v in ISLAND]]]}
    assert by_id["r2"]["source_box_ids"] == list(MULTI.source_box_ids)
    assert context["regions"] == project_regions(REGIONS)["items"]


def test_autonumbered_box_names_are_strings_in_context():
    boxes = (ReferenceBox("1", (0, 0, 10, 10), ""), ReferenceBox("2", (0.5, 0.5, 20, 20), "t"))
    context = annotation_record_context(SPEC_RECORD, boxes_as_regions(boxes, 100, 100))
    assert [box["id"] for box in context["boxes"]] == ["1", "2"]
    assert [region["region_id"] for region in context["regions"]] == ["1", "2"]
    assert context["boxes"][1]["bbox"] == [0.5, 0.5, 20, 20]


VALIDATOR_HOOKS = '''\
CALLS = []


def parse(companion, image):
    raise AssertionError("M1 must never call box_parser")


def check(obj, record):
    CALLS.append(record)
    return []
'''
MARKED_SCHEMA = json.dumps({
    "type": "object", "additionalProperties": False, "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["region_id"],
        "properties": {"region_id": {"type": "string", "x-labelkit-region-ref": True}}}}},
})


def image_project(env: Env, examples: str = "") -> str:
    env.tmp.joinpath("hooks.py").write_text(VALIDATOR_HOOKS, encoding="utf-8")
    text = env.project(annotate='instruction = "标注"\n' + examples, output='validator = "hooks.py:check"\n')
    return text.replace(text[text.index("schema_inline"):], f"schema_inline = '''{MARKED_SCHEMA}'''\n")


def test_probe_and_few_shot_call_validator_with_none(tmp_path):
    """启动探针与 few-shot 干跑都以 None 调用校验器，从不虚构样本上下文。"""
    env = Env(tmp_path)
    examples = ('[[annotate.examples]]\ninput = "样例"\n\n[annotate.examples.output]\n'
                'items = [{region_id = "n1"}]\n')
    cfg = env.load(image_project(env, examples))
    calls = cfg.validation_hooks.output.target.__globals__["CALLS"]
    assert len(calls) >= 2
    assert all(record is None for record in calls)


def test_few_shot_skips_region_reference_check(tmp_path):
    """few-shot 里的 region_id 只过 Schema：任意字符串合法，类型错误是 CONFIG_ERROR。"""
    env = Env(tmp_path)
    examples = ('[[annotate.examples]]\ninput = "样例"\n\n[annotate.examples.output]\n'
                'items = [{region_id = "no-such-region"}]\n')
    cfg = env.load(image_project(env, examples))
    assert cfg.region_ref_paths == ("/items/*/region_id",)
    assert cfg.annotate.examples[0].output["items"][0]["region_id"] == "no-such-region"
    wrong = examples.replace('"no-such-region"', "7")
    errors = env.errors(image_project(env, wrong))
    assert any("annotate.examples" in error for error in errors)


def test_annotate_record_uses_regions_from_options(tmp_path):
    """公开入口 annotate_record 同样绑定样本上下文（verify 修复走这条路径）。"""
    from labelkit.operators.annotate import AnnotatePromptOptions

    received = []

    def postprocess(candidate, context):
        received.append(context)
        return candidate

    cfg = hooked_cfg(postprocess)
    metrics = _Metrics()
    llm = _QueueLLM(GOOD)
    ctx = RunContext(cfg=cfg, llm=SimpleNamespace(calibrator=_Calibrator(10)),
                     schema_engine=SchemaEngine(SCHEMA, llm, cfg.output, metrics), rng=None, batch_no=1,
                     metrics=metrics, tasks=_SerialTasks(), task_namespace="t:run:batch:1:stage:annotate")
    asyncio.run(annotate_record(SPEC_RECORD, ctx, AnnotatePromptOptions(regions=SPEC_REGIONS)))
    assert received == [annotation_record_context(SPEC_RECORD, SPEC_REGIONS)]


@pytest.mark.parametrize("raw, expected", [
    (None, {}),
    ({"case_id": "c7", "other": 1}, {"case_id": "c7"}),
], ids=["image-record-has-no-raw", "raw-wins"])
def test_passthrough_fields_still_read_raw(tmp_path, raw, expected):
    """passthrough_fields 只读 Record.raw：样本上下文里的键不会被透传。"""
    cfg = cfg_for(tmp_path, passthrough_fields=("case_id", "sample_id", "boxes", "image"))
    item = image_item()
    item.record = dataclasses.replace(item.record, raw=raw)
    run_emitter(cfg, [item])
    assert main_rows(cfg)[0]["_meta"]["source"]["fields"] == expected


def test_real_geometry_validation_reaches_the_hook():
    """模型画的多边形经真实校验投影后，钩子看到的是原图像素浮点。"""
    model = {"regions": [{"box_id": None, "source_box_ids": None,
                          "polygons": [{"exterior": [[100, 100], [500, 100], [500, 500], [100, 100]], "holes": []}]}]}
    regions = validate_regions(model, (), 1081, 2341).proof
    received = []

    def postprocess(candidate, context):
        received.append(context["regions"][0]["geometry"]["coordinates"][0][:2])
        return candidate

    item = run_annotate(hooked_cfg(postprocess), _QueueLLM('{"kind": "a", "items": [{"region_id": "r1"}]}'), regions)
    assert item.status == "active"
    assert received == [[[108.1, 234.1], [540.5, 234.1]]]

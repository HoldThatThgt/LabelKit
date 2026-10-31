"""区域引用检查的引擎层位与修复语义（SPEC-image-region-segmentation §13.2，AC-11、AC-18、AC-135）。

只在 ``complete`` 接缝上替身，SchemaEngine、AnnotateStage、M1 投影都是真实的。
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from types import SimpleNamespace

import pytest

from labelkit.common.config._collect import _Collector
from labelkit.common.config._postprocessing import compile_postprocessor_model, region_ref_paths
from labelkit.common.config.model import OutputConfig
from labelkit.common.contracts.regions import ReferenceBox
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import PipelineItem, Usage
from labelkit.common.errors import SchemaViolation
from labelkit.common.extensions.hooks import ResolvedHook
from labelkit.common.inference.llm_client import Message, Part, PromptBundle
from labelkit.common.inference.schema_engine import (
    CallScope,
    FinalizedCallRequest,
    SchemaEngine,
    _thaw_json,
)
from labelkit.operators.annotate import AnnotatePromptOptions, AnnotateStage, annotate_record
from labelkit.operators.annotation_finalization import class_region_ref_paths
from labelkit.operators.segment_geometry import validate_regions
from tests.common.config.test_segment_mode_config import Env
from tests.operators.test_annotate_image import RECORD, _Calibrator, _Metrics, _SerialTasks, make_cfg

SECRET = "ghost-value-7"
MARKED = {
    "type": "object", "additionalProperties": False, "required": ["items"],
    "properties": {
        "primary": {"type": ["string", "null"], "x-labelkit-region-ref": True},
        "items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["region_id"],
            "properties": {
                "region_id": {"type": "string", "x-labelkit-region-ref": True},
                "others": {"type": "array", "items": {"type": "string", "x-labelkit-region-ref": True}},
                "note": {"type": "string"},
            }}},
    },
}
PATHS = region_ref_paths(MARKED)
MODEL = _thaw_json(compile_postprocessor_model(_Collector(), "p", "[output]", MARKED))
SCOPE = CallScope(record_ids=("a",), region_ids=frozenset({"r1", "n12"}), region_ref_paths=PATHS)


def good(*ids: str) -> str:
    return json.dumps({"items": [{"region_id": region_id} for region_id in ids]})


class QueueLLM:
    """按队列返回文本，记录每轮的 Schema 与提示词。"""

    def __init__(self, *texts):
        self.texts = list(texts)
        self.schemas = []
        self.prompts = []

    async def complete(self, _profile, prompt, response_schema=None):
        index = len(self.prompts)
        self.prompts.append(prompt)
        self.schemas.append(response_schema)
        if index >= len(self.texts):
            raise AssertionError("unexpected extra provider call")
        return SimpleNamespace(text=self.texts[index], structured=None, usage=Usage(5, 2), model="m")


class Metrics:
    def __init__(self):
        self.events = []

    def event(self, name, **kwargs):
        self.events.append((name, kwargs))


def prompt() -> PromptBundle:
    return PromptBundle(messages=(Message(role="user", parts=(Part(kind="text", text="标注"),)),))


def engine_for(llm, *, attempts=2, validator=None, metrics=None) -> SchemaEngine:
    return SchemaEngine(MODEL, llm, OutputConfig(max_repair_attempts=attempts), metrics, validator=validator)


def finalized(finalizer, scope=SCOPE) -> FinalizedCallRequest:
    return FinalizedCallRequest(profile="m", prompt=prompt(), model_schema=MODEL, final_schema=MODEL, scope=scope,
                                candidate_finalizer=finalizer, repair_projector=lambda value: value)


def run_stage(cfg, llm: QueueLLM, regions) -> PipelineItem:
    """用真实 SchemaEngine 跑一次 AnnotateStage。"""
    metrics = _Metrics()
    engine = SchemaEngine(dict(cfg.user_schema), llm, cfg.output, metrics)
    ctx = RunContext(cfg=cfg, llm=SimpleNamespace(calibrator=_Calibrator(10)), schema_engine=engine, rng=None,
                     batch_no=1, metrics=metrics, tasks=_SerialTasks(), task_namespace="t:run:batch:1:stage:annotate")
    item = PipelineItem(record=RECORD, regions=regions)
    asyncio.run(AnnotateStage(cfg).run([item], ctx))
    return item


def repair_text(llm: QueueLLM, index: int) -> str:
    return "\n".join(part.text for message in llm.prompts[index].messages for part in message.parts
                     if part.kind == "text")


def test_paths_use_json_pointer_with_star_for_array_items():
    assert PATHS == ("/primary", "/items/*/region_id", "/items/*/others/*")
    schema = {"type": "object", "properties": {"a/b~c": {"type": "string", "x-labelkit-region-ref": True}}}
    assert region_ref_paths(schema) == ("/a~1b~0c",)


def test_marker_removed_from_model_schema():
    """注解不上行：L0 Schema 与 L2 用的模型 Schema 都没有它，完整 Schema 保留，其余键不变。"""
    assert "x-labelkit-region-ref" not in json.dumps(MODEL)
    assert "x-labelkit-region-ref" in json.dumps(MARKED)
    expected = json.loads(json.dumps(MARKED).replace(', "x-labelkit-region-ref": true', ""))
    assert MODEL == expected
    assert list(MODEL["properties"]["items"]["items"]["properties"]) == ["region_id", "others", "note"]
    llm = QueueLLM(good("r1"))
    asyncio.run(engine_for(llm).complete_validated("m", prompt(), MODEL, scope=SCOPE))
    assert "x-labelkit-region-ref" not in json.dumps(llm.schemas[0])


@pytest.mark.parametrize("hooked", [False, True], ids=["validated", "finalized"])
def test_stage_sends_model_schema_without_marker(hooked):
    """真实 AnnotateStage：有无后处理函数，发给端点的 Schema 都没有注解，引用检查都生效。"""
    box = ReferenceBox("n12", (43, 140, 821, 1264), "")
    regions = validate_regions({"regions": [{"box_id": "n12", "polygons": None, "source_box_ids": None}]},
                               (box,), 1080, 2340).proof
    base = make_cfg()
    annotate = base.annotate
    if hooked:
        annotate = dataclasses.replace(annotate, resolved_postprocessor=ResolvedHook(
            "hooks.py:fn", lambda candidate, context: candidate))
    cfg = dataclasses.replace(base, annotate=annotate, user_schema=MARKED, model_user_schema=MODEL,
                              region_ref_paths=PATHS)
    llm = QueueLLM(good(SECRET), good("n12"))
    item = run_stage(cfg, llm, regions)
    assert item.status == "active"
    assert item.annotation.output == {"items": [{"region_id": "n12"}]}
    assert len(llm.prompts) == 2
    assert all("x-labelkit-region-ref" not in json.dumps(schema) for schema in llm.schemas)
    assert all("x-labelkit-region-ref" not in repair_text(llm, index) for index in range(2))
    assert "/items/0/region_id: unknown region reference" in repair_text(llm, 1)


def test_check_runs_before_finalizer():
    """未知引用的候选不调用 finalizer；模型 Schema 违规也不走引用检查。"""
    seen = []

    def finalizer(candidate):
        seen.append(candidate["items"][0]["region_id"])
        return candidate

    llm = QueueLLM('{"items": [{"region_id": 7}]}', good(SECRET), good("r1"))
    obj, _usage, attempts, _model = asyncio.run(engine_for(llm).complete_finalized(finalized(finalizer)))
    assert obj == {"items": [{"region_id": "r1"}]} and attempts == 3
    assert seen == ["r1"]
    assert "unknown region reference" not in repair_text(llm, 1)
    assert "/items/0/region_id: unknown region reference" in repair_text(llm, 2)


def test_check_runs_before_validator_without_finalizer():
    """complete_validated：未知引用不调用 L2.5；已知引用才到校验器，校验器违规照常修复。"""
    seen = []

    def validator(obj, record):
        seen.append((obj["items"][0]["region_id"], record))
        return ["must select r1"] if obj["items"][0]["region_id"] != "r1" else []

    llm = QueueLLM(good(SECRET), good("n12"), good("r1"))
    scope = dataclasses.replace(SCOPE, user_treatment=True, record={"sample_id": "s"})
    obj, _usage, attempts, _model = asyncio.run(
        engine_for(llm, validator=validator).complete_validated("m", prompt(), MODEL, scope=scope))
    assert obj == {"items": [{"region_id": "r1"}]} and attempts == 3
    assert seen == [("n12", {"sample_id": "s"}), ("r1", {"sample_id": "s"})]
    assert "/items/0/region_id: unknown region reference" in repair_text(llm, 1)
    assert "(validator) must select r1" in repair_text(llm, 2)


def test_user_schema_call_runs_the_check_when_schema_is_none():
    llm = QueueLLM(good(SECRET), good("r1"))
    obj, *_ = asyncio.run(engine_for(llm).complete_validated("m", prompt(), scope=SCOPE))
    assert obj == {"items": [{"region_id": "r1"}]}
    assert len(llm.prompts) == 2


def test_null_value_skipped():
    """null 与缺失的可选字段跳过；空数组没有值可查。"""
    llm = QueueLLM(json.dumps({"primary": None, "items": [{"region_id": "r1", "others": []}, {"region_id": "n12"}]}))
    obj, _usage, attempts, _model = asyncio.run(engine_for(llm).complete_validated("m", prompt(), MODEL, scope=SCOPE))
    assert attempts == 1 and obj["primary"] is None
    llm = QueueLLM(json.dumps({"primary": SECRET, "items": []}), good("r1"))
    asyncio.run(engine_for(llm).complete_validated("m", prompt(), MODEL, scope=SCOPE))
    assert "/primary: unknown region reference" in repair_text(llm, 1)


def test_items_path_reports_concrete_pointer():
    """数组项路径报具体下标，字符串 items 也逐项检查；每个未知值恰一条。"""
    payload = {"primary": "nope", "items": [{"region_id": "r1", "others": ["n12", "bad"]}, {"region_id": "zz"}]}
    llm = QueueLLM(json.dumps(payload))
    with pytest.raises(SchemaViolation) as caught:
        asyncio.run(engine_for(llm, attempts=0).complete_validated("m", prompt(), MODEL, scope=SCOPE))
    assert caught.value.errors == [
        "/items/0/others/1: unknown region reference",
        "/items/1/region_id: unknown region reference",
        "/primary: unknown region reference",
    ]


def test_property_name_with_pointer_characters_is_escaped():
    schema = {"type": "object", "properties": {"a/b": {"type": "string"}}, "additionalProperties": False}
    llm = QueueLLM(json.dumps({"a/b": SECRET}))
    scope = CallScope(region_ids=frozenset({"r1"}), region_ref_paths=("/a~1b",))
    with pytest.raises(SchemaViolation) as caught:
        asyncio.run(engine_for(llm, attempts=0).complete_validated("m", prompt(), schema, scope=scope))
    assert caught.value.errors == ["/a~1b: unknown region reference"]


def test_unknown_region_reference_enters_l3_without_echo():
    """未知值进入 L3；违规文本、异常与 trace 摘要都不回显模型给出的值。"""
    metrics = Metrics()
    llm = QueueLLM(good(SECRET), good(SECRET), good(SECRET))
    with pytest.raises(SchemaViolation) as caught:
        asyncio.run(engine_for(llm, metrics=metrics).complete_validated("m", prompt(), MODEL, scope=SCOPE))
    assert len(llm.prompts) == 3
    assert caught.value.errors == ["/items/0/region_id: unknown region reference"]
    assert caught.value.callback_only is False
    assert SECRET not in str(caught.value)
    assert SECRET not in json.dumps(caught.value.errors)
    payloads = [kwargs["payload"] for name, kwargs in metrics.events if name == "schema.repair"]
    assert payloads[-1]["violations"] == ["/items/0/region_id: unknown region reference"]
    assert SECRET not in json.dumps(payloads)


def test_box_name_not_a_candidate_is_repairable_without_echo():
    """框名没有成为候选：同一 RegionSet 派生的 id 不含它，走可修复违规，违规不回显该框名。"""
    boxes = (ReferenceBox("n12", (43, 140, 821, 1264), "登录"), ReferenceBox("n13", (43, 1300, 821, 1400), "注册"))
    regions = validate_regions({"regions": [{"box_id": "n12", "polygons": None, "source_box_ids": None}]},
                               boxes, 1080, 2340).proof
    assert [box.box_id for box in regions.boxes] == ["n12", "n13"]
    base = make_cfg()
    cfg = dataclasses.replace(base, user_schema=MARKED, model_user_schema=MODEL, region_ref_paths=PATHS)
    llm = QueueLLM(good("n13"), good("n12"))
    item = run_stage(cfg, llm, regions)
    assert item.status == "active" and item.annotation.output == {"items": [{"region_id": "n12"}]}
    violations = [line for line in repair_text(llm, 1).split("\n") if "unknown region reference" in line]
    assert violations == ["1. /items/0/region_id: unknown region reference"]

    # 穷尽修复时记录失败，错误文本同样不含 n13。
    item = run_stage(cfg, QueueLLM(good("n13"), good("n13"), good("n13")), regions)
    assert item.status == "failed" and item.errors[-1].kind == "schema_violation"
    assert "n13" not in item.errors[-1].message
    assert "unknown region reference" in item.errors[-1].message


def test_no_region_ids_means_no_check():
    """region_ids 为 None（所有非图片调用）时路径不起作用。"""
    llm = QueueLLM(good(SECRET))
    scope = CallScope(region_ref_paths=PATHS)
    obj, _usage, attempts, _model = asyncio.run(engine_for(llm).complete_validated("m", prompt(), MODEL, scope=scope))
    assert attempts == 1 and obj["items"][0]["region_id"] == SECRET


def test_class_view_paths_and_model_schema_win_over_global(tmp_path):
    """有类视图取视图：类 Schema 的路径生效，全局路径不生效，端点收到的也是类的模型 Schema。"""
    item = {"type": "object", "additionalProperties": False, "required": ["target"],
            "properties": {"target": {"type": "string", "x-labelkit-region-ref": True}}}
    own = {"type": "object", "additionalProperties": False, "required": ["items"],
           "properties": {"items": {"type": "array", "items": item}}}
    body = ('[classify]\nenabled = true\nllm = "default"\nassignment = "single"\nfallback_class = "other"\n\n'
            '[[classify.classes]]\nname = "own"\ndescription = "d"\n\n'
            '[[classify.classes]]\nname = "other"\ndescription = "d"\n\n'
            f"[class.own.annotate]\nschema_inline = '''{json.dumps(own)}'''\n")
    env = Env(tmp_path)
    project = env.project(body=body)
    cfg = env.load(project[:project.rindex("schema_inline")] + f"schema_inline = '''{json.dumps(MARKED)}'''\n")
    assert class_region_ref_paths(cfg, None) == PATHS
    assert class_region_ref_paths(cfg, "other") == PATHS
    assert class_region_ref_paths(cfg, "own") == ("/items/*/target",)

    regions = validate_regions({"regions": [{"box_id": "n12", "polygons": None, "source_box_ids": None}]},
                               (ReferenceBox("n12", (43, 140, 821, 1264), ""),), 1080, 2340).proof
    llm = QueueLLM(json.dumps({"items": [{"target": SECRET}]}), json.dumps({"items": [{"target": "n12"}]}))
    metrics = _Metrics()
    ctx = RunContext(cfg=cfg, llm=SimpleNamespace(calibrator=_Calibrator(10)),
                     schema_engine=SchemaEngine(dict(cfg.user_schema), llm, cfg.output, metrics), rng=None, batch_no=1,
                     metrics=metrics, tasks=_SerialTasks(), task_namespace="t:run:batch:1:stage:annotate")
    result = asyncio.run(annotate_record(RECORD, ctx, AnnotatePromptOptions(label="own", regions=regions)))
    assert result.output == {"items": [{"target": "n12"}]}
    assert len(llm.prompts) == 2
    assert "/items/0/target: unknown region reference" in repair_text(llm, 1)
    assert list(llm.schemas[0]["properties"]["items"]["items"]["properties"]) == ["target"]
    assert "x-labelkit-region-ref" not in json.dumps(llm.schemas[0])

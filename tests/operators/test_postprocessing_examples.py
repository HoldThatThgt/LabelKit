"""标注后处理教学工程的离线边界与独立 oracle 测试。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from labelkit.cli.parser import CliOverrides
from labelkit.common.config import load
from labelkit.common.errors import PostprocessorError
from labelkit.common.extensions.postprocessing import invoke_postprocessor
from labelkit.operators.generation.planner import compile_scenario_plan
from labelkit.operators.generation.program import compile_generation_program


_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE = _ROOT / "examples" / "annotation-postprocessing"


def _load_example_module(filename: str, name: str):
    """从教学工程路径装载一个独立 Python 模块。"""
    path = _EXAMPLE / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def hooks():
    """装载生产式教学钩子。"""
    return _load_example_module("hooks.py", "annotation_postprocessing_example_hooks")


@pytest.fixture(scope="module")
def checker():
    """装载不依赖教学钩子的工件检查器。"""
    return _load_example_module("check_output.py", "annotation_postprocessing_example_checker")


@pytest.mark.parametrize(("text", "values", "expected"), (
    (
        "值班员记录：粤B·12345和京A12345已进入园区。",
        ["京a12345", "粤b-12345"],
        [("粤B12345", "粤B·12345"), ("京A12345", "京A12345")],
    ),
    (
        "入口京A12345，出口京A12345。",
        ["京A12345", "京A12345"],
        [("京A12345", "京A12345"), ("京A12345", "京A12345")],
    ),
    (
        "摄像头识别到沪C-88888，随后车辆驶离。",
        ["沪c 88888"],
        [("沪C88888", "沪C-88888")],
    ),
))
def test_plate_hook_normalizes_and_derives_unicode_offsets(hooks, text, values, expected):
    """多实体、同值歧义与分隔符均按原文位置确定性完成。"""
    candidate = {"entities": [{"value": value} for value in values]}

    completed = hooks.complete_plate_annotation(candidate, {"text": text})

    assert completed["entity_count"] == len(expected)
    assert [(item["value"], text[item["start"]:item["end"]])
            for item in completed["entities"]] == expected
    assert candidate == {"entities": [{"value": value} for value in values]}


@pytest.mark.parametrize(
    ("candidate", "record"),
    (
        ({"entities": [{"value": "粤B12345"}]}, {"text": "这里只有京A12345。"}),
        ({"entities": [{"value": "京A12345"}, {"value": "京A12345"}]},
         {"text": "这里只有京A12345。"}),
        ({"entities": [{"value": "京A12345"}]},
         {"text": "入口京A12345，出口京A12345。"}),
        ({"entities": []}, None),
    ),
    ids=("unmatched", "duplicate-overflow", "ambiguous-missing-duplicate", "missing-record"),
)
def test_plate_hook_rejects_missing_or_unmatched_context(hooks, candidate, record):
    """缺失原文、幻觉实体和重复过量实体都硬失败。"""
    with pytest.raises(ValueError):
        hooks.complete_plate_annotation(candidate, record)


def test_runtime_boundary_redacts_plate_hook_failure(tmp_path):
    """真实装载的工程异常在调用边界转成固定脱敏错误。"""
    cfg = load(
        _EXAMPLE / "config-local-4b.toml",
        _EXAMPLE / "project.toml",
        CliOverrides(output=str(tmp_path / "plate-labels.jsonl"), console="plain"),
    )
    candidate = {"entities": [{"value": "粤B12345"}]}
    record = {"text": "这里只有京A12345。", "private": {"value": "secret"}}

    with pytest.raises(PostprocessorError, match="^postprocessor_error$"):
        invoke_postprocessor(cfg.annotate.resolved_postprocessor, candidate, record)

    assert candidate == {"entities": [{"value": "粤B12345"}]}
    assert record["private"] == {"value": "secret"}


def test_sequence_hooks_use_the_declared_record_boundaries(hooks):
    """序列主标注只接收 None，帧标注只从真实成员 raw 派生。"""
    sequence = hooks.complete_sequence_annotation({"summary": "  订票成功🙂。  "}, None)
    row = {
        "payload": {
            "request_id": "R-测试-7",
            "status": "acknowledged",
            "utterance": "系统已受理 R-测试-7，请稍候。",
        },
        "_meta": {"event": {"role": "acknowledge"}},
    }
    frame = hooks.complete_frame_annotation({
        "request_id": "R-测试-7",
        "observed_status": "acknowledged",
        "summary": "  已受理🙂  ",
    }, row)

    assert sequence == {"summary": "订票成功🙂。", "summary_length": 6}
    assert frame["summary"] == "已受理🙂"
    assert frame["summary_length"] == 4
    assert frame["request_id_length"] == len(row["payload"]["request_id"])
    assert frame["utterance_length"] == len(row["payload"]["utterance"])
    with pytest.raises(ValueError, match="record must be None"):
        hooks.complete_sequence_annotation({"summary": "成功"}, {})
    with pytest.raises(ValueError, match="differs from record.payload"):
        hooks.complete_frame_annotation({
            "request_id": "R-WRONG", "observed_status": "acknowledged", "summary": "受理",
        }, row)
    with pytest.raises(ValueError, match="observed_status differs"):
        hooks.complete_frame_annotation({
            "request_id": "R-测试-7", "observed_status": "pending", "summary": "受理",
        }, row)
    duplicate = {"payload": dict(row["payload"]), "_meta": row["_meta"]}
    duplicate["payload"]["utterance"] = "R-测试-7 已受理，请记录 R-测试-7。"
    located = hooks.complete_frame_annotation({
        "request_id": "R-测试-7", "observed_status": "acknowledged", "summary": "受理",
    }, duplicate)
    assert located["request_id_length"] == len(duplicate["payload"]["request_id"])


def test_projects_freeze_model_projection_and_single_sequence_plan(tmp_path):
    """M1 与 M2 冻结代码字段投影及一条三事件正例和 replay。"""
    overrides = CliOverrides(output=str(tmp_path / "labels.jsonl"), console="plain")
    ordinary = load(_EXAMPLE / "config-local-4b.toml", _EXAMPLE / "project.toml", overrides)
    sequence = load(
        _EXAMPLE / "config-local-4b.toml", _EXAMPLE / "project-sequence.toml", overrides,
    )
    assert set(ordinary.user_schema["properties"]) == {"entities", "entity_count"}
    assert set(ordinary.model_user_schema["properties"]) == {"entities"}
    assert set(ordinary.model_user_schema["properties"]["entities"]["items"]["properties"]) == {"value"}
    assert set(ordinary.annotate.examples[0].output) == {"entities"}
    assert set(ordinary.annotate.examples[0].output["entities"][0]) == {"value"}
    assert ordinary.annotate.resolved_postprocessor is not None
    assert "summary_length" not in sequence.model_user_schema["properties"]
    assert set(sequence.model_frame_schema["properties"]) == {
        "request_id", "observed_status", "summary",
    }
    assert all(view.resolved_postprocessor is not None for view in sequence.frame_class_views.values())
    program = compile_generation_program(sequence)
    plan = compile_scenario_plan(program)
    visible = [events for block in plan.blocks for (_slot, variant), events in block.items()
               if variant is not None]
    assert len(plan.delivery_slots) == 1
    assert [len(events) for events in visible] == [3]
    assert len(plan.noise_slots) == 0
    assert len(plan.replay_layouts) == 1


def test_independent_checker_rejects_forged_plate_offset_and_frame_length(checker):
    """独立 oracle 不复用钩子，并能杀死伪造位置与派生长度。"""
    plate_schema = checker._load_json(_EXAMPLE / "schemas" / "plate-annotation.json")
    source = {"case_id": "one", "text": "车辆粤B·12345已到。"}
    start = source["text"].index("粤")
    plate = {
        "entities": [{"value": "粤B12345", "start": start, "end": start + 8}],
        "entity_count": 1,
    }
    assert checker._check_plate_row(plate, source, ["粤B12345"], plate_schema)["entity_count"] == 1
    plate["entities"][0]["start"] += 1
    with pytest.raises(AssertionError, match="do not select"):
        checker._check_plate_row(plate, source, ["粤B12345"], plate_schema)

    frame_schema = checker._load_json(_EXAMPLE / "schemas" / "frame-annotation.json")
    row = {
        "payload": {"request_id": "R-7", "status": "pending", "utterance": "提交 R-7 请求"},
        "_meta": {"annotation": {
            "request_id": "R-7", "observed_status": "pending", "summary": "已提交",
            "summary_length": 3, "utterance_length": 9,
            "request_id_length": 3,
        }},
    }
    checker._check_frame(row, frame_schema)
    row["_meta"]["annotation"]["request_id_length"] = 2
    with pytest.raises(AssertionError, match="request_id_length differs"):
        checker._check_frame(row, frame_schema)


def test_checker_ordinary_entry_uses_input_oracle(tmp_path, checker):
    """公开检查入口按输入期望和原文切片验收，不调用钩子。"""
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    source = {"case_id": "one", "text": "车辆粤B·12345已到。"}
    oracle_path = tmp_path / "oracle.json"
    start = source["text"].index("粤")
    output = {
        "entities": [{"value": "粤B12345", "start": start, "end": start + 8}],
        "entity_count": 1,
        "_meta": {"source": {"fields": {"case_id": "one"}}},
    }
    input_path.write_text(json.dumps(source, ensure_ascii=False) + "\n", encoding="utf-8")
    output_path.write_text(json.dumps(output, ensure_ascii=False) + "\n", encoding="utf-8")
    oracle_path.write_text(json.dumps({"one": ["粤B12345"]}, ensure_ascii=False), encoding="utf-8")

    result = checker.check_ordinary(output_path, input_path, oracle_path)

    assert result == {
        "rows": 1,
        "cases": {"one": {"entities": ["粤B12345"], "spans": [[2, 10]], "entity_count": 1}},
    }

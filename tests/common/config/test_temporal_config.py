"""v1.20 sequence 时间配置的公开 loader 契约。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from labelkit.common.config import load
from labelkit.common.config.model import CliOverrides
from labelkit.common.errors import ConfigError


EXAMPLE_ROOT = Path(__file__).resolve().parents[3] / "examples" / "sequence-generation"


def _copy_project(tmp_path: Path, project_name: str = "project.toml") -> tuple[Path, Path]:
    """复制教学工程并返回根目录与待测 project 路径。"""
    root = tmp_path / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    return root, root / project_name


def _errors(root: Path, project: Path) -> list[str]:
    """返回公开 loader 的全部聚合错误。"""
    with pytest.raises(ConfigError) as captured:
        load(root / "config.toml", project, CliOverrides())
    return captured.value.errors


def _has(errors: list[str], text: str) -> None:
    """断言聚合错误中至少一条包含目标文本。"""
    assert any(text in item for item in errors), errors


def _add_schema_time(root: Path, schema_name: str, path: str = "timestamp",
                     value_type: str = "integer") -> None:
    """向已关闭的根 object Schema 添加 required 业务时间叶子。"""
    target = root / "schemas" / schema_name
    schema = json.loads(target.read_text(encoding="utf-8"))
    schema["properties"][path] = {
        "type": value_type,
        "x-labelkit-business-time": True,
    }
    schema["required"].append(path)
    for example in schema.get("examples", []):
        example[path] = 0 if value_type == "integer" else "1970-01-01T00:00:00.000000+00:00"
    target.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")


def _insert_after(text: str, needle: str, addition: str, last: bool = False) -> str:
    """在精确锨点的首个或最后一个出现之后插入 TOML。"""
    position = text.rfind(needle) if last else text.find(needle)
    assert position >= 0
    end = position + len(needle)
    return text[:end] + addition + text[end:]


def _add_frame_contract(project: Path, schema_name: str, source: str,
                        duration_s: str | None, resources: tuple[str, ...]) -> None:
    """在引用指定 Schema 的 frame generate 节声明区间与时间 binding。"""
    text = project.read_text(encoding="utf-8")
    needle = f'schema_path = "schemas/{schema_name}"'
    rows = [f'  {{ payload_path = "/timestamp", source = "{source}" }}']
    addition = "\n"
    if duration_s is not None:
        addition += f"duration_s = {duration_s}\n"
    if resources:
        rendered = ", ".join(f'"{item}"' for item in resources)
        addition += f"resources = [{rendered}]\n"
    addition += "time_bindings = [\n" + ",\n".join(rows) + "\n]"
    project.write_text(_insert_after(text, needle, addition), encoding="utf-8")


def _add_duration_resource(project: Path, schema_name: str, duration_s: str,
                           resources: tuple[str, ...]) -> None:
    """在指定 frame generate 节只声明时长与资源。"""
    text = project.read_text(encoding="utf-8")
    rendered = ", ".join(f'"{item}"' for item in resources)
    addition = f"\nduration_s = {duration_s}\nresources = [{rendered}]"
    needle = f'schema_path = "schemas/{schema_name}"'
    project.write_text(_insert_after(text, needle, addition), encoding="utf-8")


def _add_annotation_contract(root: Path, project: Path, resource: str) -> None:
    """声明 sequence annotation 的 Schema marker 与 resource binding。"""
    _add_schema_time(root, "annotation.json")
    text = project.read_text(encoding="utf-8")
    addition = (
        "\ntime_bindings = [\n"
        f'  {{ payload_path = "/timestamp", source = '
        f'"first_resource_start_milliseconds", resource = "{resource}" }}\n]'
    )
    project.write_text(
        _insert_after(text, 'schema_path = "schemas/annotation.json"', addition, last=True),
        encoding="utf-8",
    )


def _add_containment(project: Path, container: str, contained: str) -> None:
    """在教学 pattern 中声明一条 containment。"""
    text = project.read_text(encoding="utf-8")
    block = (
        "[[generate.pattern.booking_success.containments]]\n"
        f'container = "{container}"\ncontained = "{contained}"\n\n'
    )
    project.write_text(text.replace("[generate.timeline]\n", block + "[generate.timeline]\n"),
                       encoding="utf-8")


def _complete_temporal_project(tmp_path: Path, annotation_resource: str = "foreground_app",
                               ack_resource: str = "screen") -> tuple[Path, Path]:
    """构造 marker/binding/resource/containment 全部闭合的 declared 工程。"""
    root, project = _copy_project(tmp_path)
    _add_schema_time(root, "frame-request.json")
    _add_frame_contract(
        project, "frame-request.json", "event_start_milliseconds", "120", ("foreground_app",),
    )
    _add_duration_resource(project, "frame-acknowledgement.json", "10", (ack_resource,))
    _add_annotation_contract(root, project, annotation_resource)
    _add_containment(project, "request", "acknowledge")
    return root, project


def test_temporal_config_compiles_model_schemas_and_missing_contained_branch(tmp_path):
    """有效工程冻结 full/model Schema、区间资源与 missing-contained 关系。"""
    root, project = _complete_temporal_project(tmp_path)

    cfg = load(root / "config.toml", project, CliOverrides())

    frame = cfg.frame_class_views["task_request"]
    assert frame.business_time_paths == ("/timestamp",)
    assert frame.duration_us == 120_000_000 and frame.resources == ("foreground_app",)
    assert "timestamp" in frame.gen_schema["properties"]
    assert "timestamp" not in frame.model_gen_schema["properties"]
    assert "timestamp" not in frame.model_gen_schema["examples"][0]
    view = cfg.class_views["ticket_booking"]
    assert view.business_time_paths == ("/timestamp",)
    assert "timestamp" not in view.model_schema["properties"]
    assert cfg.sequence_generation.patterns[0].containments[0].contained == "acknowledge"
    with pytest.raises(TypeError):
        frame.model_gen_schema["properties"]["utterance"]["minLength"] = 0


@pytest.mark.parametrize("marker,binding", ((True, False), (False, True)))
def test_schema_marker_and_binding_paths_must_be_exactly_equal(tmp_path, marker, binding):
    """Schema marker 或 binding 任一单边缺失都在 M1 失败。"""
    root, project = _copy_project(tmp_path)
    if marker:
        _add_schema_time(root, "frame-request.json")
    if binding:
        _add_frame_contract(
            project, "frame-request.json", "event_start_milliseconds", None, (),
        )

    _has(_errors(root, project), "must exactly match time bindings")


def test_time_binding_source_must_match_marked_leaf_type(tmp_path):
    """ISO source 不能绑定 integer 标记叶子。"""
    root, project = _copy_project(tmp_path)
    _add_schema_time(root, "frame-request.json")
    _add_frame_contract(project, "frame-request.json", "event_start_iso8601", None, ())

    _has(_errors(root, project), "time binding requires Schema type string")


def test_duration_and_resource_contracts_use_positive_millisecond_intervals(tmp_path):
    """duration 必须正整数毫秒，resource 不能占用点事件。"""
    root, project = _copy_project(tmp_path / "precision")
    _add_duration_resource(project, "frame-request.json", "0.0005", ("foreground_app",))
    _has(_errors(root, project), "positive seconds at millisecond precision")

    root, project = _copy_project(tmp_path / "point")
    text = project.read_text(encoding="utf-8")
    project.write_text(
        _insert_after(text, 'schema_path = "schemas/frame-request.json"',
                      '\nresources = ["foreground_app"]'),
        encoding="utf-8",
    )
    _has(_errors(root, project), "duration_s is required")


def test_role_state_binding_cannot_overlap_frame_time_binding(tmp_path):
    """role state payload binding 不能覆盖业务时间叶子。"""
    root, project = _complete_temporal_project(tmp_path)
    text = project.read_text(encoding="utf-8")
    old = '  { payload_path = "/status", state_phase = "after", state_path = "/request/status" }\n]'
    new = old[:-2] + ',\n  { payload_path = "/timestamp", state_phase = "after", '
    new += 'state_path = "/request/id" }\n]'
    assert old in text
    project.write_text(text.replace(old, new, 1), encoding="utf-8")

    _has(_errors(root, project), "payload binding conflicts with frame time binding")


def test_containment_rejects_shared_resource_and_missing_container(tmp_path):
    """containment 两端不得共享互斥资源，missing 不得只删 container。"""
    root, project = _complete_temporal_project(tmp_path / "shared", ack_resource="foreground_app")
    _has(_errors(root, project), "must not share an exclusive resource")

    root, project = _copy_project(tmp_path / "missing")
    _add_duration_resource(project, "frame-request.json", "120", ("foreground_app",))
    _add_duration_resource(project, "frame-acknowledgement.json", "10", ("screen",))
    _add_containment(project, "acknowledge", "request")
    _has(_errors(root, project), "missing container cannot retain its contained role")


def test_annotation_resource_must_survive_every_deliverable_branch(tmp_path):
    """annotation resource 若只由被 missing 的 role 提供，M1 必须拒绝。"""
    root, project = _complete_temporal_project(tmp_path, annotation_resource="screen")

    _has(_errors(root, project), "is absent from branch 'missing_acknowledgement'")


def test_instruction_only_rejects_sequence_annotation_time_binding(tmp_path):
    """instruction-only 不得声明只属于 declared member resource 的 annotation 时间。"""
    root, project = _copy_project(tmp_path, "project-instruction-only.toml")
    _add_annotation_contract(root, project, "foreground_app")

    _has(_errors(root, project), "annotation time bindings require declared mode")


def test_disabled_annotate_rejects_sequence_annotation_time_binding(tmp_path):
    """关闭 annotation 时不得保留机械时间 binding。"""
    root, project = _complete_temporal_project(tmp_path)
    text = project.read_text(encoding="utf-8")
    project.write_text(
        text.replace("[annotate]\nenabled = true", "[annotate]\nenabled = false", 1),
        encoding="utf-8",
    )

    _has(_errors(root, project), "annotate must be enabled")


def test_process_form_rejects_sequence_annotation_time_binding(tmp_path):
    """普通 process/flat 工程不得声明 sequence annotation 时间 binding。"""
    root, project = _complete_temporal_project(tmp_path)
    text = project.read_text(encoding="utf-8")
    text = text.replace('mode = "generate_only"', 'mode = "process"', 1)
    text = text.replace('form = "sequence"', 'form = "flat"', 1)
    project.write_text(text, encoding="utf-8")

    _has(_errors(root, project), "only legal in sequence generation")


def test_class_few_shot_is_projected_to_model_space(tmp_path):
    """class few-shot 可以按 full Schema 作者化，冻结视图只暴露 model-space output。"""
    root, project = _complete_temporal_project(tmp_path)
    text = project.read_text(encoding="utf-8")
    block = '''
[[class.ticket_booking.annotate.examples]]
input = "sample"

[class.ticket_booking.annotate.examples.output]
intent = "book"
outcome = "ticketed"
request_id = "R-1"
ticket_id = "T-1"
summary = "done"
timestamp = 123
'''
    project.write_text(text.replace("[frame.class.task_request]\n", block + "\n[frame.class.task_request]\n"),
                       encoding="utf-8")

    cfg = load(root / "config.toml", project, CliOverrides())

    output = cfg.class_views["ticket_booking"].annotate.examples[0].output
    assert output == {
        "intent": "book", "outcome": "ticketed", "request_id": "R-1",
        "ticket_id": "T-1", "summary": "done",
    }


def test_projected_class_few_shot_array_uses_json_shape_for_dryrun(tmp_path):
    """冻结后的 tuple 必须在 Schema dry-run 前恢复为 JSON array。"""
    root, project = _complete_temporal_project(tmp_path)
    schema_path = root / "schemas" / "annotation.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"]["items"] = {
        "type": "array", "items": {"type": "string"}, "minItems": 1,
    }
    schema["required"].append("items")
    schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    text = project.read_text(encoding="utf-8")
    block = '''
[[class.ticket_booking.annotate.examples]]
input = "sample"

[class.ticket_booking.annotate.examples.output]
intent = "book"
outcome = "ticketed"
request_id = "R-1"
ticket_id = "T-1"
summary = "done"
timestamp = 123
items = ["a", "b"]
'''
    project.write_text(text.replace("[frame.class.task_request]\n", block + "\n[frame.class.task_request]\n"),
                       encoding="utf-8")

    cfg = load(root / "config.toml", project, CliOverrides())

    assert cfg.class_views["ticket_booking"].annotate.examples[0].output["items"] == ("a", "b")

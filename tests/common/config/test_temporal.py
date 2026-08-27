"""v1.20 业务时间 Schema 投影与机械写入契约。"""
from __future__ import annotations

from copy import deepcopy

import pytest

from labelkit.common.config._collect import _Collector
from labelkit.common.config._temporal import (
    compile_temporal_schema,
    inject_temporal_values,
    parse_resources,
    parse_time_bindings,
    project_temporal_instance,
    resolve_frame_time_values,
)
from labelkit.common.config.model import TimeBindingSpec


def _schema() -> dict:
    """构造一份带嵌套时间叶子的完整 Schema。"""
    return {
        "type": "object",
        "properties": {
            "meta": {
                "type": "object",
                "properties": {
                    "timestamp": {
                        "type": "integer",
                        "minimum": 0,
                        "x-labelkit-business-time": True,
                    },
                    "label": {"type": "string", "minLength": 1},
                },
                "required": ["timestamp", "label"],
                "additionalProperties": False,
                "default": {"timestamp": 7, "label": "default"},
                "examples": [{"timestamp": 8, "label": "nested"}],
            },
            "value": {"type": "integer", "minimum": 1},
        },
        "required": ["meta", "value"],
        "additionalProperties": False,
        "examples": [{"meta": {"timestamp": 9, "label": "root"}, "value": 2}],
    }


def test_compile_temporal_schema_projects_only_business_time_values():
    """model Schema 删除时间叶子与样例值，但保留必需 parent 与非时间约束。"""
    collector = _Collector()
    source = _schema()
    compiled = compile_temporal_schema(collector, "project.toml", "[frame.generate]", source)

    assert collector.errors == []
    assert compiled.business_time_paths == ("/meta/timestamp",)
    assert compiled.leaf_types == {"/meta/timestamp": "integer"}
    assert compiled.full_schema["properties"]["meta"]["properties"]["timestamp"][
        "x-labelkit-business-time"
    ] is True
    assert source == _schema()
    model = compiled.model_schema
    assert model["required"] == ("meta", "value")
    assert model["properties"]["meta"]["required"] == ("label",)
    assert set(model["properties"]["meta"]["properties"]) == {"label"}
    assert model["properties"]["meta"]["default"] == {"label": "default"}
    assert model["properties"]["meta"]["examples"] == ({"label": "nested"},)
    assert model["examples"] == ({"meta": {"label": "root"}, "value": 2},)
    assert model["properties"]["value"] == {"type": "integer", "minimum": 1}
    with pytest.raises(TypeError):
        model["properties"]["value"]["minimum"] = 0
    with pytest.raises(TypeError):
        compiled.full_schema["properties"]["value"]["minimum"] = 0


def test_model_schema_keeps_required_empty_temporal_parent():
    """只有时间叶子的 object 在 model Schema 中仍保留必需空 parent。"""
    schema = {
        "type": "object",
        "properties": {
            "meta": {
                "type": "object",
                "properties": {
                    "timestamp": {
                        "type": "integer",
                        "x-labelkit-business-time": True,
                    },
                },
                "required": ["timestamp"],
                "additionalProperties": False,
            },
        },
        "required": ["meta"],
        "additionalProperties": False,
    }
    collector = _Collector()

    compiled = compile_temporal_schema(collector, "project.toml", "[frame.generate]", schema)

    assert collector.errors == []
    meta = compiled.model_schema["properties"]["meta"]
    assert meta["properties"] == {}
    assert meta["required"] == ()
    assert compiled.model_schema["required"] == ("meta",)


@pytest.mark.parametrize("keyword,value", (
    ("$ref", "#/$defs/time"),
    ("$dynamicRef", "#anchor"),
    ("allOf", []),
    ("anyOf", []),
    ("oneOf", []),
    ("not", {}),
    ("if", {}),
    ("then", {}),
    ("else", {}),
    ("dependentRequired", {}),
    ("dependentSchemas", {}),
    ("patternProperties", {}),
    ("propertyNames", {}),
    ("unevaluatedProperties", False),
    ("minProperties", 1),
    ("maxProperties", 3),
))
def test_temporal_path_rejects_each_closed_projection_keyword(keyword, value):
    """时间路径 ancestor 上的每个封闭关键字都独立失败。"""
    schema = _schema()
    schema[keyword] = value
    collector = _Collector()

    compile_temporal_schema(collector, "project.toml", "[frame.generate]", schema)

    assert any(f"forbids keyword {keyword!r}" in item for item in collector.errors)


@pytest.mark.parametrize("required,additional,message", (
    (["value"], False, "must require property 'meta'"),
    (["meta", "value"], True, "requires additionalProperties = false"),
))
def test_temporal_path_requires_closed_required_object_chain(required, additional, message):
    """时间叶子的每级 object parent 都必须 required 且关闭额外属性。"""
    schema = _schema()
    schema["required"] = required
    schema["additionalProperties"] = additional
    collector = _Collector()

    compile_temporal_schema(collector, "project.toml", "[frame.generate]", schema)

    assert any(message in item for item in collector.errors)


@pytest.mark.parametrize("keyword,value", (("const", {}), ("enum", ({},))))
def test_temporal_path_rejects_ancestor_value_constraints(keyword, value):
    """ancestor object 的 const/enum 不参与一般 Schema 等价性证明。"""
    schema = _schema()
    schema["properties"]["meta"][keyword] = value
    collector = _Collector()

    compile_temporal_schema(collector, "project.toml", "[frame.generate]", schema)

    assert any(f"forbids keyword {keyword!r}" in item for item in collector.errors)


def test_temporal_path_rejects_schema_shaped_additional_properties():
    """时间 ancestor 的 Schema 形态 additionalProperties 独立聚合失败。"""
    schema = _schema()
    schema["additionalProperties"] = {"type": "string"}
    collector = _Collector()

    compile_temporal_schema(collector, "project.toml", "[frame.generate]", schema)

    assert any("forbids keyword 'additionalProperties'" in item for item in collector.errors)


@pytest.mark.parametrize("mutation,message", (
    ("nested_required", "must require property 'timestamp'"),
    ("nested_type", "business time parent type must be object"),
))
def test_temporal_path_validates_each_nested_parent(mutation, message):
    """闭包校验覆盖时间叶子的直接 object parent。"""
    schema = _schema()
    meta = schema["properties"]["meta"]
    if mutation == "nested_required":
        meta["required"] = ["label"]
    else:
        meta["type"] = "array"
    collector = _Collector()

    compile_temporal_schema(collector, "project.toml", "[frame.generate]", schema)

    assert any(message in item for item in collector.errors)


def test_temporal_marker_false_and_marker_outside_properties_are_rejected():
    """false 标记与不在显式 properties 路径下的标记都不会被忽略。"""
    schema = _schema()
    schema["properties"]["meta"]["properties"]["timestamp"][
        "x-labelkit-business-time"
    ] = False
    schema["items"] = {"type": "integer", "x-labelkit-business-time": True}
    collector = _Collector()

    compile_temporal_schema(collector, "project.toml", "[frame.generate]", schema)

    assert any("must equal true" in item for item in collector.errors)
    assert any("must be an explicit properties leaf" in item for item in collector.errors)


@pytest.mark.parametrize("keyword", ("items", "patternProperties"))
def test_temporal_marker_rejects_array_and_dynamic_property_paths(keyword):
    """array item 与 dynamic property 下的标记不是显式 properties instance path。"""
    schema = _schema()
    schema[keyword] = {
        "dynamic": {"type": "integer", "x-labelkit-business-time": True}
    }
    collector = _Collector()

    compile_temporal_schema(collector, "project.toml", "[frame.generate]", schema)

    assert any("must be an explicit properties leaf" in item for item in collector.errors)


def test_temporal_instance_projection_is_total_for_missing_or_wrong_parents():
    """修复投影只删除仍可达时间叶子，不创建或替换错误 parent。"""
    original = {"meta": "wrong", "other": {"timestamp": 3}, "value": 2}
    projected = project_temporal_instance(
        original, ("/meta/timestamp", "/missing/timestamp")
    )

    assert projected == original
    assert projected is not original


def test_temporal_injection_requires_existing_parent_and_absent_leaf():
    """机械写入只处理已存在 object parent，不覆盖模型自带时间。"""
    candidate = {"meta": {"label": "ok"}, "value": 2}
    before = deepcopy(candidate)

    injected = inject_temporal_values(candidate, {"/meta/timestamp": 123})

    assert injected == {"meta": {"label": "ok", "timestamp": 123}, "value": 2}
    assert candidate == before
    with pytest.raises(ValueError, match="already exists"):
        inject_temporal_values(injected, {"/meta/timestamp": 456})
    with pytest.raises(ValueError, match="missing object parent"):
        inject_temporal_values(candidate, {"/missing/timestamp": 456})


@pytest.mark.parametrize("rows,annotation,message", (
    ([
        {"payload_path": "/time", "source": "event_start_milliseconds"},
        {"payload_path": "/time", "source": "event_start_milliseconds"},
    ], False, "conflicting paths"),
    ([
        {"payload_path": "/meta", "source": "event_start_milliseconds"},
        {"payload_path": "/meta/time", "source": "event_start_milliseconds"},
    ], False, "conflicting paths"),
    ([{"payload_path": "/time", "source": "wall_clock"}], False, "expected one of"),
    ([{
        "payload_path": "/time",
        "source": "first_resource_start_milliseconds",
        "resource": "Foreground-App",
    }], True, "expected [a-z0-9_]+ resource name"),
))
def test_time_binding_parser_rejects_duplicate_prefix_source_and_resource(
        rows, annotation, message):
    """binding path、source 与 annotation resource 都使用封闭声明域。"""
    collector = _Collector()

    parse_time_bindings(
        collector, "project.toml", "[frame.generate].time_bindings", rows, annotation,
    )

    assert any(message in item for item in collector.errors)


@pytest.mark.parametrize("resources", (
    ["foreground_app", "foreground_app"],
    ["Foreground-App"],
))
def test_resource_parser_requires_unique_declared_names(resources):
    """exclusive resource 使用声明序唯一的小写下划线名称。"""
    collector = _Collector()

    parse_resources(collector, "project.toml", "[frame.generate].resources", resources)

    assert any("unique [a-z0-9_]+ resource names" in item for item in collector.errors)


def test_resolve_frame_time_values_uses_integer_end_and_fixed_offset_iso8601():
    """五种 frame source 共用同一整数起点、终点、时长与 fixed offset。"""
    bindings = tuple(
        TimeBindingSpec(f"/{source}", source)
        for source in (
            "event_start_milliseconds",
            "event_end_milliseconds",
            "event_duration_milliseconds",
            "event_start_iso8601",
            "event_end_iso8601",
        )
    )

    values = resolve_frame_time_values(bindings, 0, 1_500_000, 480)

    assert values == {
        "/event_start_milliseconds": 0,
        "/event_end_milliseconds": 1500,
        "/event_duration_milliseconds": 1500,
        "/event_start_iso8601": "1970-01-01T08:00:00.000000+08:00",
        "/event_end_iso8601": "1970-01-01T08:00:01.500000+08:00",
    }


def test_resolve_frame_time_values_rejects_non_quantized_or_point_end_source():
    """runtime descriptor 不能绕过毫秒 quantum 或 end source 的正时长要求。"""
    start = (TimeBindingSpec("/timestamp", "event_start_milliseconds"),)
    end = (TimeBindingSpec("/end", "event_end_milliseconds"),)

    with pytest.raises(ValueError, match="millisecond quantum"):
        resolve_frame_time_values(start, 1, 0, 0)
    with pytest.raises(ValueError, match="requires positive duration"):
        resolve_frame_time_values(end, 0, 0, 0)

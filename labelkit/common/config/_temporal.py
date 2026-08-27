"""v1.20 业务时间 Schema 投影、配置解析与 sequence 交叉校验。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping

from jsonpointer import JsonPointer, JsonPointerException
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from labelkit.common.config._collect import _Collector, _fmt
if TYPE_CHECKING:
    from labelkit.common.config.model import TimeBindingSpec


_MARKER = "x-labelkit-business-time"
_NAME_RE = re.compile(r"[a-z0-9_]+")
_FRAME_SOURCES = frozenset({
    "event_start_milliseconds",
    "event_end_milliseconds",
    "event_duration_milliseconds",
    "event_start_iso8601",
    "event_end_iso8601",
})
_ANNOTATION_SOURCES = frozenset({"first_resource_start_milliseconds"})
_CLOSED_KEYWORDS = frozenset({
    "$ref", "$dynamicRef", "allOf", "anyOf", "oneOf", "not", "if", "then", "else",
    "dependentRequired", "dependentSchemas", "patternProperties", "propertyNames",
    "unevaluatedProperties", "minProperties", "maxProperties",
})
_DATA_KEYWORDS = frozenset({"const", "enum", "default", "examples"})


class _FrozenJsonDict(dict):
    """保留 JSON/dict 协议且拒绝所有就地修改的深冻结 mapping。"""

    def _blocked(self, *_args, **_kwargs) -> None:
        """拒绝对冻结 JSON mapping 的修改。"""
        raise TypeError("frozen JSON mapping does not support mutation")

    __setitem__ = _blocked
    __delitem__ = _blocked
    clear = _blocked
    pop = _blocked
    popitem = _blocked
    setdefault = _blocked
    update = _blocked
    __ior__ = _blocked


@dataclass(frozen=True)
class TemporalSchemaProjection:
    """M1 对一份完整 Schema 的冻结时间投影。"""

    full_schema: Mapping[str, object] | None       # 工程权威 Schema 的深冻结副本
    model_schema: Mapping[str, object] | None      # 删除业务时间值后的 provider Schema
    business_time_paths: tuple[str, ...]           # Schema properties 声明序 instance path
    leaf_types: Mapping[str, str]                  # path 到 integer/string 叶子类型


@dataclass(frozen=True)
class IntervalContainmentSpec:
    """一条 declared role 正区间严格包含关系。"""

    container: str                                # 外层正时长 role
    contained: str                                # 被包含的正时长 role


@dataclass
class _ScanState:
    """Schema 时间标记遍历的可变结果。"""

    paths: list[str]                               # 已发现的 instance path
    leaf_types: dict[str, str]                     # 已验证叶子类型
    explicit_marker_nodes: set[int]               # 显式 properties child 节点标识
    violations: list[str]                         # 未加定位前缀的稳定英文错误


def freeze_json(value: object) -> object:
    """递归复制并冻结 JSON 树。

    @param value 待冻结 JSON 值
    @return JSON-compatible frozen dict/tuple 组成的不可变树
    """
    if isinstance(value, Mapping):
        return _FrozenJsonDict({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(freeze_json(item) for item in value)
    return value


def compile_temporal_schema(col: _Collector, file: str, location: str,
                            schema: Mapping[str, object] | None) -> TemporalSchemaProjection:
    """收集业务时间叶子并一次性派生 model Schema。

    @param col M1 错误聚合器
    @param file project.toml 定位字符串
    @param location Schema 所属配置节
    @param schema 完整 Draft 2020-12 object Schema
    @return 深冻结 full/model Schema、路径与叶子类型
    """
    if schema is None:
        return TemporalSchemaProjection(None, None, (), MappingProxyType({}))
    plain = _plain_json(schema)
    state = _ScanState([], {}, set(), [])
    _scan_properties(plain, (), (), state)
    _scan_stray_markers(plain, state, "$")
    for message in dict.fromkeys(state.violations):
        col.error(f"{file}:{location}: {message}")
    parts = tuple(tuple(JsonPointer(path).parts) for path in state.paths)
    model = _project_schema(plain, (), frozenset(parts))
    try:
        Draft202012Validator.check_schema(model)
    except SchemaError as error:
        col.error(f"{file}:{location}: projected model Schema is invalid: {error.message}")
    return TemporalSchemaProjection(
        freeze_json(plain), freeze_json(model), tuple(state.paths),
        MappingProxyType(dict(state.leaf_types)),
    )


def _plain_json(value: object) -> object:
    """把冻结 JSON 树复制回标准可变容器。"""
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    return value


def _escape_token(value: str) -> str:
    """把一个 property 名编码为 RFC 6901 token。"""
    return value.replace("~", "~0").replace("/", "~1")


def _path_text(parts: tuple[str, ...]) -> str:
    """把显式 properties token 渲染为 RFC 6901 instance path。"""
    return "/" + "/".join(_escape_token(part) for part in parts)


def _scan_properties(node: object, path: tuple[str, ...], parents: tuple,
                     state: _ScanState) -> None:
    """只沿显式 properties 边收集合法时间叶子。"""
    if not isinstance(node, Mapping):
        return
    properties = node.get("properties")
    if not isinstance(properties, Mapping):
        return
    for name, child in properties.items():
        if not isinstance(name, str) or not isinstance(child, Mapping):
            continue
        child_path = (*path, name)
        child_parents = (*parents, (node, name, path))
        if _MARKER in child:
            state.explicit_marker_nodes.add(id(child))
            _collect_marker(child, child_path, child_parents, state)
        _scan_properties(child, child_path, child_parents, state)


def _collect_marker(node: Mapping, path: tuple[str, ...], parents: tuple,
                    state: _ScanState) -> None:
    """校验一个显式 property 上的标记与完整 ancestor 链。"""
    text = _path_text(path)
    if node.get(_MARKER) is not True:
        state.violations.append(f"{text}: {_MARKER} must equal true")
        return
    if "-" in path:
        state.violations.append(f"{text}: business time path must not contain '-' token")
    for parent, property_name, parent_path in parents:
        _check_parent(parent, property_name, parent_path, state)
    _check_closed_keywords(node, text, False, state)
    value_type = node.get("type")
    if value_type not in ("integer", "string"):
        state.violations.append(f"{text}: business time leaf type must be integer or string")
        return
    state.paths.append(text)
    state.leaf_types[text] = value_type


def _check_parent(parent: Mapping, child_name: str, path: tuple[str, ...],
                  state: _ScanState) -> None:
    """校验时间路径上一级 object parent 的闭包。"""
    text = "$" if not path else _path_text(path)
    if parent.get("type") != "object":
        state.violations.append(f"{text}: business time parent type must be object")
    required = parent.get("required")
    if not isinstance(required, list) or child_name not in required:
        state.violations.append(f"{text}: business time parent must require property {child_name!r}")
    if parent.get("additionalProperties") is not False:
        state.violations.append(f"{text}: business time parent requires additionalProperties = false")
    _check_closed_keywords(parent, text, True, state)


def _check_closed_keywords(node: Mapping, text: str, parent: bool,
                           state: _ScanState) -> None:
    """拒绝会让机械投影需要一般 Schema 证明的关键字。"""
    for keyword in _CLOSED_KEYWORDS:
        if keyword in node:
            state.violations.append(f"{text}: temporal projection forbids keyword {keyword!r}")
    if isinstance(node.get("additionalProperties"), Mapping):
        state.violations.append(f"{text}: temporal projection forbids keyword 'additionalProperties'")
    if parent:
        for keyword in ("const", "enum"):
            if keyword in node:
                state.violations.append(f"{text}: temporal projection forbids keyword {keyword!r}")


def _scan_stray_markers(node: object, state: _ScanState, location: str) -> None:
    """拒绝不在显式 properties child 上的时间标记。"""
    if isinstance(node, Mapping):
        if _MARKER in node and id(node) not in state.explicit_marker_nodes:
            state.violations.append(
                f"{location}: {_MARKER} must be an explicit properties leaf"
            )
        for key, value in node.items():
            if key not in _DATA_KEYWORDS:
                _scan_stray_markers(value, state, f"{location}/{_escape_token(str(key))}")
    elif isinstance(node, (tuple, list)):
        for index, value in enumerate(node):
            _scan_stray_markers(value, state, f"{location}/{index}")


def _project_schema(node: object, path: tuple[str, ...],
                    time_paths: frozenset[tuple[str, ...]]) -> object:
    """在 Schema 位置删除时间叶子、required 成员与数据样例。"""
    if not isinstance(node, Mapping):
        return _plain_json(node)
    out: dict[str, object] = {}
    direct = {parts[-1] for parts in time_paths if parts[:-1] == path}
    for key, value in node.items():
        if key == _MARKER:
            continue
        if key == "properties" and isinstance(value, Mapping):
            out[key] = _project_properties(value, path, time_paths)
        elif key == "required" and isinstance(value, (tuple, list)):
            out[key] = [item for item in value if item not in direct]
        elif key in ("default", "examples"):
            out[key] = _project_schema_data(value, path, time_paths)
        else:
            out[key] = _strip_markers(value)
    return out


def _project_properties(properties: Mapping, path: tuple[str, ...],
                        time_paths: frozenset[tuple[str, ...]]) -> dict[str, object]:
    """投影一个 properties 表并保留空 object parent。"""
    out: dict[str, object] = {}
    for name, child in properties.items():
        child_path = (*path, name)
        if child_path in time_paths:
            continue
        out[name] = _project_schema(child, child_path, time_paths)
    return out


def _project_schema_data(value: object, prefix: tuple[str, ...],
                         time_paths: frozenset[tuple[str, ...]]) -> object:
    """从 mapping 形态 default/examples 中删除当前 instance 子树的时间值。"""
    tails = tuple(parts[len(prefix):] for parts in time_paths if parts[:len(prefix)] == prefix)
    if isinstance(value, Mapping):
        return _project_data_mapping(value, tails)
    if isinstance(value, (tuple, list)):
        return [(_project_data_mapping(item, tails) if isinstance(item, Mapping)
                 else _plain_json(item)) for item in value]
    return value


def _project_data_mapping(value: Mapping, tails: tuple[tuple[str, ...], ...]) -> dict:
    """对一个 Schema 数据 mapping 执行 total 叶子删除。"""
    out = _plain_json(value)
    for parts in tails:
        _delete_parts(out, parts)
    return out


def _strip_markers(value: object) -> object:
    """递归删除 provider-facing Schema 中的所有 LabelKit 标记。"""
    if isinstance(value, Mapping):
        return {key: _strip_markers(item) for key, item in value.items() if key != _MARKER}
    if isinstance(value, (tuple, list)):
        return [_strip_markers(item) for item in value]
    return value


def project_temporal_instance(value: Mapping[str, object],
                              paths: tuple[str, ...]) -> dict[str, object]:
    """在深拷贝上 total 删除仍可达的业务时间叶子。

    @param value 完整或不完整候选 object
    @param paths 冻结业务时间 instance path
    @return 不创建或替换 parent 的可变深拷贝
    """
    out = _plain_json(value)
    for path in paths:
        try:
            parts = tuple(JsonPointer(path).parts)
        except JsonPointerException:
            continue
        _delete_parts(out, parts)
    return out


def _delete_parts(root: object, parts: tuple[str, ...]) -> None:
    """若路径的 object parent 仍可达，就只删除最后叶子。"""
    if not parts or not isinstance(root, dict):
        return
    parent = root
    for token in parts[:-1]:
        child = parent.get(token)
        if not isinstance(child, dict):
            return
        parent = child
    parent.pop(parts[-1], None)


def inject_temporal_values(candidate: Mapping[str, object],
                           values: Mapping[str, object]) -> dict[str, object]:
    """把机械值写入已存在 object parent，且拒绝覆盖候选值。

    @param candidate 已通过 model Schema 的候选 object
    @param values path 到权威机械值的声明序 mapping
    @return 写入机械值的可变深拷贝
    @raises ValueError path 非法、parent 缺失或叶子已存在
    """
    out = _plain_json(candidate)
    for path, value in values.items():
        parts = _binding_parts(path)
        parent = out
        for token in parts[:-1]:
            child = parent.get(token)
            if not isinstance(child, dict):
                raise ValueError(f"temporal path {path!r} has a missing object parent")
            parent = child
        if parts[-1] in parent:
            raise ValueError(f"temporal path {path!r} already exists in the candidate")
        parent[parts[-1]] = deepcopy(value)
    return out


def _binding_parts(path: object) -> tuple[str, ...]:
    """解析非根、非 array-append 的 RFC 6901 binding path。"""
    try:
        parts = tuple(JsonPointer(path).parts) if isinstance(path, str) else ()
    except JsonPointerException as error:
        raise ValueError(f"invalid temporal path {path!r}") from error
    if not parts or "-" in parts:
        raise ValueError(f"invalid temporal path {path!r}")
    return parts


def parse_time_bindings(col: _Collector, file: str, location: str, value: object,
                        annotation: bool) -> tuple["TimeBindingSpec", ...]:
    """解析 frame 或 sequence annotation 的封闭时间 binding 表。

    @param col M1 错误聚合器
    @param file project.toml 定位字符串
    @param location time_bindings 键定位
    @param value TOML 原始值
    @param annotation 是否解析 sequence annotation source
    @return 声明序合法 binding
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        col.error(f"{file}:{location}: expected array of tables, got {_fmt(value)}")
        return ()
    bindings = tuple(item for index, row in enumerate(value, 1)
                     if (item := _parse_binding_row(
                         col, file, f"{location}[{index}]", row, annotation)) is not None)
    _check_binding_paths(col, file, location, bindings)
    return bindings


def _parse_binding_row(col: _Collector, file: str, location: str, row: object,
                       annotation: bool) -> "TimeBindingSpec | None":
    """解析一条时间 binding 并聚合未知键。"""
    if not isinstance(row, dict):
        col.error(f"{file}:{location}: expected table, got {_fmt(row)}")
        return None
    allowed = {"payload_path", "source", "resource"} if annotation else {
        "payload_path", "source"}
    for key in sorted(set(row) - allowed):
        col.error(f"{file}:{location}.{key}: unknown time binding key")
    path = row.get("payload_path")
    try:
        _binding_parts(path)
    except ValueError:
        col.error(f"{file}:{location}.payload_path: expected non-root RFC 6901 path")
        return None
    sources = _ANNOTATION_SOURCES if annotation else _FRAME_SOURCES
    source = row.get("source")
    if source not in sources:
        col.error(f"{file}:{location}.source: expected one of {', '.join(sorted(sources))}")
        return None
    resource = row.get("resource")
    if annotation and (not isinstance(resource, str) or _NAME_RE.fullmatch(resource) is None):
        col.error(f"{file}:{location}.resource: expected [a-z0-9_]+ resource name")
        return None
    from labelkit.common.config.model import TimeBindingSpec
    return TimeBindingSpec(path, source, resource if annotation else None)


def _check_binding_paths(col: _Collector, file: str, location: str,
                         bindings: tuple["TimeBindingSpec", ...]) -> None:
    """拒绝重复或互为 ancestor/descendant 的 binding path。"""
    parsed = [(item.payload_path, tuple(JsonPointer(item.payload_path).parts))
              for item in bindings]
    for index, (left, left_parts) in enumerate(parsed):
        for right, right_parts in parsed[index + 1:]:
            if _parts_overlap(left_parts, right_parts):
                col.error(f"{file}:{location}: conflicting paths {left!r} and {right!r}")


def _parts_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """判断两组 pointer token 是否相等或互为前缀。"""
    return left == right[:len(left)] or right == left[:len(right)]


def paths_overlap(left: str, right: str) -> bool:
    """判断两个合法 JSON Pointer 是否互相覆盖。

    @param left 左侧 RFC 6901 path
    @param right 右侧 RFC 6901 path
    @return 相等或互为 ancestor/descendant 时为 true
    """
    return _parts_overlap(tuple(JsonPointer(left).parts), tuple(JsonPointer(right).parts))


def check_sequence_temporal_contract(state: object, patterns: tuple,
                                     config: object) -> None:
    """聚合 sequence 时间 binding、containment、resource 与毫秒量化约束。

    @param state generation 解析状态
    @param patterns 已解析 declared pattern 表
    @param config 完整 sequence generation 配置
    """
    _check_state_binding_conflicts(state, patterns)
    for pattern in patterns:
        _check_pattern_containments(state, pattern, config.counterfactual_sets)
    _check_annotation_contract(state, patterns, config)
    _check_point_event_frames(state, config)
    _check_quantum(state, config)


def parse_containment_spec(state: object, row: Mapping[str, object],
                           location: str) -> IntervalContainmentSpec:
    """解析一条 containment 并把全部错误并入 M1。

    @param state generation 解析状态
    @param row containment TOML 表
    @param location containment 定位
    @return 冻结 containment；错误名回落空字符串
    """
    for key in row:
        if key not in {"container", "contained"}:
            _sequence_error(state, f"{location}.{key}",
                            "generation_config_invalid: unknown or deleted sequence key")
    container = _containment_name(state, row.get("container"), f"{location}.container")
    contained = _containment_name(state, row.get("contained"), f"{location}.contained")
    if container == contained:
        _sequence_error(state, location, "container and contained must differ")
    return IntervalContainmentSpec(container, contained)


def _containment_name(state: object, value: object, location: str) -> str:
    """解析 containment 的小写下划线 role 名。"""
    if not isinstance(value, str) or _NAME_RE.fullmatch(value) is None:
        _sequence_error(state, location,
                        f"expected name matching [a-z0-9_]+, got {_fmt(value)}")
        return ""
    return value


def _sequence_error(state: object, location: str, message: str) -> None:
    """向 generation M1 解析状态追加稳定时间错误。"""
    project = state.context.project_root / "project.toml"
    state.context.collector.error(f"{project}:{location}: {message}")


def _check_state_binding_conflicts(state: object, patterns: tuple) -> None:
    """拒绝 role state binding 与 frame time binding 争用 payload 子树。"""
    for pattern in patterns:
        for role in pattern.roles:
            frame = state.context.frame_classes.get(role.frame_class)
            if frame is None:
                continue
            for state_binding in role.payload_bindings:
                if any(paths_overlap(state_binding.payload_path, item.payload_path)
                       for item in frame.time_bindings):
                    location = f"[generate.pattern.{pattern.name}].roles.{role.name}"
                    _sequence_error(state, location,
                                    "payload binding conflicts with frame time binding")


def _check_pattern_containments(state: object, pattern: object, groups: tuple) -> None:
    """校验 containment 端点时长、资源独立性与 missing 闭包。"""
    roles = {item.name: item for item in pattern.roles}
    for relation in pattern.containments:
        container = _frame_for_role(state, roles.get(relation.container))
        contained = _frame_for_role(state, roles.get(relation.contained))
        location = f"[generate.pattern.{pattern.name}].containments"
        if container is None or contained is None:
            continue
        if container.duration_us <= 0 or contained.duration_us <= 0:
            _sequence_error(state, location, "containment roles require positive duration_s")
        if set(container.resources) & set(contained.resources):
            _sequence_error(state, location,
                            "containment roles must not share an exclusive resource")
        _check_missing_container(state, pattern, relation, groups)


def _frame_for_role(state: object, role: object) -> object | None:
    """返回一个 role 引用的 frame class 视图。"""
    if role is None:
        return None
    return state.context.frame_classes.get(role.frame_class)


def _check_missing_container(state: object, pattern: object, relation: object,
                             groups: tuple) -> None:
    """允许删除 contained，但拒绝只删除 container 的 branch。"""
    for group in groups:
        if group.pattern != pattern.name:
            continue
        for variant in group.variants:
            target = variant.target.get("role") if variant.kind == "missing" else None
            if target == relation.container:
                location = f"[[generate.counterfactual_sets]].{variant.name}"
                _sequence_error(state, location,
                                "missing container cannot retain its contained role")


def _check_annotation_contract(state: object, patterns: tuple, config: object) -> None:
    """证明 annotation binding 只用于 declared 且每个可交付 branch 都有资源。"""
    views = state.context.class_views
    bound = [(name, view) for name, view in views.items() if view.time_bindings]
    if config.mode != "declared":
        for name, _view in bound:
            _sequence_error(state, f"[class.{name}.annotate].time_bindings",
                            "annotation time bindings require declared mode")
        return
    for name, view in bound:
        class_patterns = [item for item in patterns if item.sequence_class == name]
        if not class_patterns:
            _sequence_error(state, f"[class.{name}.annotate].time_bindings",
                            "annotation time bindings require a deliverable pattern")
        for pattern in class_patterns:
            _check_pattern_annotation_resources(state, pattern, view, config)


def _check_pattern_annotation_resources(state: object, pattern: object, view: object,
                                        config: object) -> None:
    """逐个 counterfactual branch 检查 annotation 目标资源可达。"""
    groups = [item for item in config.counterfactual_sets if item.pattern == pattern.name]
    for group in groups:
        for variant in group.variants:
            roles = _variant_roles(pattern, variant)
            available = _role_resources(state, pattern, roles)
            for binding in view.time_bindings:
                if binding.resource not in available:
                    location = f"[class.{pattern.sequence_class}.annotate].time_bindings"
                    _sequence_error(
                        state, location,
                        f"resource {binding.resource!r} is absent from branch {variant.name!r}",
                    )


def _variant_roles(pattern: object, variant: object) -> tuple[str, ...]:
    """重建一个已规范化 variant 的实际 role word。"""
    roles = list(pattern.order)
    if variant.kind == "missing" and variant.target.get("role") in roles:
        roles.remove(variant.target["role"])
    elif variant.kind == "reordered":
        before, after = variant.target.get("before"), variant.target.get("after")
        if before in roles and after in roles:
            left, right = roles.index(before), roles.index(after)
            roles[left], roles[right] = roles[right], roles[left]
    return tuple(roles)


def _role_resources(state: object, pattern: object, role_names: tuple[str, ...]) -> set[str]:
    """返回一个 branch role word 中全部正区间资源。"""
    roles = {item.name: item for item in pattern.roles}
    resources: set[str] = set()
    for name in role_names:
        frame = _frame_for_role(state, roles.get(name))
        if frame is not None and frame.duration_us > 0:
            resources.update(frame.resources)
    return resources


def _check_point_event_frames(state: object, config: object) -> None:
    """限制 noise 与 instruction-only 只使用点事件 frame class。"""
    if config.noise is not None:
        frame = state.context.frame_classes.get(config.noise.frame_class)
        if frame is not None and frame.duration_us:
            _sequence_error(state, "[generate.noise].frame_class",
                            "noise frame class must be a point event")
    if config.mode != "instruction_only":
        return
    for name, frame in state.context.frame_classes.items():
        eligible = bool(frame.description.strip() and frame.gen_instruction and frame.gen_schema)
        if eligible and frame.duration_us:
            _sequence_error(state, f"[frame.class.{name}.generate].duration_s",
                            "instruction_only frame class must be a point event")


def _check_quantum(state: object, config: object) -> None:
    """要求 Planner 全部配置时间域使用固定 1000 微秒 quantum。"""
    values = _quantized_values(state, config)
    if any(value % 1000 for value in values):
        _sequence_error(state, "[generate]", "planner time values must align to milliseconds")


def _quantized_values(state: object, config: object) -> list[int]:
    """收集所有应在 M1 定型的 Planner 时间整数。"""
    timeline = config.timeline
    values = [timeline.timestamp_start_us, *timeline.event_gap_us,
              timeline.session_gap_us, timeline.session_max_span_us]
    values.extend(frame.duration_us for frame in state.context.frame_classes.values())
    for pattern in config.patterns:
        values.append(pattern.max_span_us)
        values.extend(value for gap in pattern.gaps
                      for value in (gap.min_gap_us, gap.max_gap_us))
    for group in config.counterfactual_sets:
        for variant in group.variants:
            values.extend(value for key, value in variant.target.items()
                          if key in {"min_excess_us", "max_excess_us"})
    values.extend(bound for window in config.calendar_windows.values()
                  for interval in window.intervals_us for bound in interval)
    return values


def check_binding_schema(col: _Collector, file: str, location: str,
                         compiled: TemporalSchemaProjection,
                         bindings: tuple["TimeBindingSpec", ...]) -> None:
    """证明 Schema 标记路径与 binding 路径精确等集且类型一致。

    @param col M1 错误聚合器
    @param file project.toml 定位字符串
    @param location Schema/binding 所属配置节
    @param compiled 已编译时间 Schema
    @param bindings 已解析 binding 表
    """
    paths = tuple(item.payload_path for item in bindings)
    if set(paths) != set(compiled.business_time_paths) or len(paths) != len(compiled.business_time_paths):
        col.error(f"{file}:{location}: Schema business time paths must exactly match time bindings")
    for binding in bindings:
        expected = "string" if binding.source.endswith("iso8601") else "integer"
        if compiled.leaf_types.get(binding.payload_path) != expected:
            col.error(f"{file}:{location}{binding.payload_path}: time binding requires Schema type {expected}")


def parse_duration_us(col: _Collector, file: str, location: str, value: object) -> int:
    """把可选正秒数无损量化为正整数毫秒的微秒值。

    @param col M1 错误聚合器
    @param file project.toml 定位字符串
    @param location duration_s 键定位
    @param value TOML 原始值；None 表示点事件
    @return 正时长微秒或零
    """
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        col.error(f"{file}:{location}: expected positive seconds at millisecond precision")
        return 0
    try:
        milliseconds = Decimal(str(value)) * 1000
    except InvalidOperation:
        milliseconds = Decimal(0)
    if (not milliseconds.is_finite() or milliseconds <= 0
            or milliseconds != milliseconds.to_integral_value()):
        col.error(f"{file}:{location}: expected positive seconds at millisecond precision")
        return 0
    return int(milliseconds) * 1000


def parse_resources(col: _Collector, file: str, location: str,
                    value: object) -> tuple[str, ...]:
    """解析声明序唯一的容量一资源名。

    @param col M1 错误聚合器
    @param file project.toml 定位字符串
    @param location resources 键定位
    @param value TOML 原始值
    @return 合法资源元组
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        col.error(f"{file}:{location}: expected unique resource name array")
        return ()
    result = tuple(item for item in value if isinstance(item, str))
    valid = len(result) == len(value) and len(set(result)) == len(result)
    valid = valid and all(_NAME_RE.fullmatch(item) is not None for item in result)
    if not valid:
        col.error(f"{file}:{location}: expected unique [a-z0-9_]+ resource names")
        return ()
    return result


def resolve_frame_time_values(bindings: tuple["TimeBindingSpec", ...], timestamp_us: int,
                              duration_us: int, utc_offset_minutes: int) -> dict[str, object]:
    """把 frame binding 机械解析为 path 到权威时间值的声明序映射。

    @param bindings frame class 的冻结时间 binding
    @param timestamp_us 计划事件起点 epoch 微秒
    @param duration_us 计划事件非负时长微秒
    @param utc_offset_minutes timeline 固定 UTC offset 分钟
    @return binding 声明序 path 到 integer/string 机械值
    @raises ValueError 计划量化、source 或 descriptor 契约非法
    """
    _check_frame_time_inputs(timestamp_us, duration_us, utc_offset_minutes)
    end_us = timestamp_us + duration_us
    values: dict[str, object] = {}
    for binding in bindings:
        if binding.payload_path in values:
            raise ValueError("duplicate frame time binding path")
        values[binding.payload_path] = _resolve_frame_time_source(
            binding.source, timestamp_us, end_us, duration_us, utc_offset_minutes
        )
    return values


def _check_frame_time_inputs(timestamp_us: int, duration_us: int,
                             utc_offset_minutes: int) -> None:
    """拒绝非整数、非毫秒量化或越界的计划时间。"""
    values = (timestamp_us, duration_us, utc_offset_minutes)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise ValueError("frame time inputs must be integers")
    if timestamp_us % 1000 or duration_us < 0 or duration_us % 1000:
        raise ValueError("frame time inputs must use the millisecond quantum")
    if not -1439 <= utc_offset_minutes <= 1439:
        raise ValueError("frame time offset is outside the supported range")


def _resolve_frame_time_source(source: str, start_us: int, end_us: int,
                               duration_us: int, offset_minutes: int) -> object:
    """按封闭 source 集计算单个 frame 机械时间值。"""
    if source == "event_start_milliseconds":
        return start_us // 1000
    if source == "event_end_milliseconds":
        _require_positive_duration(duration_us)
        return end_us // 1000
    if source == "event_duration_milliseconds":
        _require_positive_duration(duration_us)
        return duration_us // 1000
    if source == "event_start_iso8601":
        return _format_iso8601(start_us, offset_minutes)
    if source == "event_end_iso8601":
        _require_positive_duration(duration_us)
        return _format_iso8601(end_us, offset_minutes)
    raise ValueError("unsupported frame time binding source")


def _require_positive_duration(duration_us: int) -> None:
    """要求 end/duration source 使用正时长事件。"""
    if duration_us <= 0:
        raise ValueError("frame time binding source requires positive duration")


def _format_iso8601(epoch_us: int, offset_minutes: int) -> str:
    """以整数 epoch 运算与固定 offset 渲染六位微秒 ISO-8601。"""
    try:
        instant = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=epoch_us)
        fixed = timezone(timedelta(minutes=offset_minutes))
        return instant.astimezone(fixed).isoformat(timespec="microseconds")
    except (OverflowError, ValueError) as error:
        raise ValueError("frame time value is outside the supported range") from error

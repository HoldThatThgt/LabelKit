"""工程标注后处理：同步函数边界及 Schema、示例和修复候选投影。"""
from __future__ import annotations

import inspect
import logging
import math
from pathlib import Path
from typing import Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from labelkit.common.errors import PostprocessorError
from labelkit.common.extensions.hooks import ResolvedHook, load_hook

_log = logging.getLogger("labelkit.postprocessing")
_MARKER = "x-labelkit-postprocessor"
_TIME_MARKER = "x-labelkit-business-time"
_SCHEMA_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"})
_SCHEMA_LISTS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SCHEMA_VALUES = frozenset({
    "items", "additionalProperties", "unevaluatedProperties", "contains", "not", "if", "then", "else",
    "propertyNames", "additionalItems", "unevaluatedItems", "contentSchema",
})
_ANCESTOR_FORBIDDEN = frozenset({
    "$ref", "$dynamicRef", "allOf", "anyOf", "oneOf", "not", "if", "then", "else",
    "dependentRequired", "dependentSchemas", "patternProperties", "propertyNames", "unevaluatedProperties",
    "const", "enum",
})
_ARRAY_FORBIDDEN = frozenset({"prefixItems", "contains", "minContains", "maxContains"})


def _failure(reason: str, reference: str) -> None:
    """记录固定原因并抛出脱敏程序错误。

    @param reason 固定英文原因
    @param reference 工程函数位置
    @raises PostprocessorError 始终抛出
    """
    _log.error("Postprocessor failed: reason=%s hook=%s", reason, reference)
    raise PostprocessorError() from None


def _copy_json(value: object, strict: bool, ancestors: set[int]) -> object:
    """复制 JSON 树，严格返回边界拒绝非 JSON 容器和循环引用。

    @param value 待复制值
    @param strict 是否仅接受标准 JSON Python 类型
    @param ancestors 当前递归祖先对象标识
    @return 无共享容器的 JSON 值
    @raises ValueError 值不属于 JSON 或存在循环
    """
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    mapping = type(value) is dict if strict else isinstance(value, Mapping)
    sequence = type(value) is list if strict else isinstance(value, (list, tuple))
    if not (mapping or sequence) or id(value) in ancestors:
        raise ValueError("invalid JSON value")
    ancestors.add(id(value))
    try:
        if mapping:
            if any(type(key) is not str for key in value):
                raise ValueError("JSON object keys must be strings")
            return {key: _copy_json(item, strict, ancestors) for key, item in value.items()}
        return [_copy_json(item, strict, ancestors) for item in value]
    finally:
        ancestors.remove(id(value))


def resolve_postprocessor(reference: str, project_root: Path) -> ResolvedHook:
    """加载并静态检查工程函数，绝不使用虚构业务输入调用它。

    @param reference 文件与属性引用
    @param project_root 工程根目录
    @return 已装载且签名合格的冻结函数
    @raises ValueError 装载或同步签名检查失败
    """
    try:
        hook = load_hook(reference, project_root)
    except Exception:
        _log.error("Cannot load postprocessor: hook=%s", reference)
        raise ValueError(f"cannot load postprocessor {reference!r}") from None
    target = hook.target
    checks = (inspect.iscoroutinefunction, inspect.isgeneratorfunction, inspect.isasyncgenfunction)
    try:
        non_sync = any(check(candidate) for candidate in (target, getattr(target, "__call__", None))
                       for check in checks)
        parameters = tuple(inspect.signature(target).parameters.values())
    except Exception:
        _signature_error(reference)
    kinds = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    if (non_sync or len(parameters) != 2 or any(item.kind not in kinds for item in parameters)
            or parameters[0].default is not inspect.Parameter.empty
            or (parameters[1].default is not inspect.Parameter.empty and parameters[1].default is not None)):
        _signature_error(reference)
    return hook


def _signature_error(reference: str) -> None:
    """拒绝不满足两个位置参数的同步函数。

    @param reference 工程函数位置
    @raises ValueError 始终抛出
    """
    _log.error("Invalid postprocessor signature: hook=%s", reference)
    raise ValueError("postprocessor must be synchronous with exactly two positional parameters")


def invoke_postprocessor(hook: ResolvedHook, obj: Mapping, record: Mapping | None) -> dict:
    """在独立输入副本上调用一次，并复制严格 JSON 返回值。

    @param hook 已解析的工程函数
    @param obj 通过模型 Schema 的候选
    @param record 原始行映射或 None
    @return 不与工程函数共享任何容器的完整标注
    @raises PostprocessorError 函数异常或返回契约违规
    """
    try:
        result = hook.target(_copy_json(obj, False, set()), _copy_json(record, False, set()))
    except Exception:
        _failure("exception", hook.reference)
    if type(result) is not dict:
        _failure("return_type", hook.reference)
    try:
        return _copy_json(result, True, set())
    except Exception:
        _failure("invalid_json", hook.reference)


def _schema_error(location: str, reason: str) -> None:
    """拒绝无法保真投影的 Schema。

    @param location Schema 位置
    @param reason 固定英文约束说明
    @raises ValueError 始终抛出
    """
    _log.error("Invalid postprocessor schema: path=%s reason=%s", location, reason)
    raise ValueError(f"{location}: {reason}")


def _children(node: Mapping):
    """只遍历 Schema 位置，跳过 default、examples、const 与 enum 数据。

    @param node 当前 Schema
    @return 子节点、定位后缀与进入边类型的迭代器
    """
    for key, value in node.items():
        if key in _SCHEMA_MAPS and isinstance(value, Mapping):
            for name, child in value.items():
                yield child, f"/{key}/{name}", key
        elif key in _SCHEMA_LISTS and isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                yield child, f"/{key}/{index}", key
        elif key in _SCHEMA_VALUES:
            yield value, f"/{key}", key


def _contains_annotation(node: object, marker: str) -> bool:
    """检查 Schema 子树是否包含指定注解，不检查数据值。

    @param node Schema 子树
    @param marker 注解名称
    @return 是否存在注解
    """
    return isinstance(node, Mapping) and (
        marker in node or any(_contains_annotation(child, marker) for child, _, _ in _children(node)))


def _inspect_schema(node: object, location: str, route: str, time_owned: bool) -> bool:
    """检查标记位置与祖先约束，返回子树是否需要投影。

    @param node 当前 Schema
    @param location Schema 路径
    @param route properties、items、root 或 unsupported
    @param time_owned 是否位于业务时间字段内
    @return 当前子树是否包含代码负责字段
    """
    if not isinstance(node, Mapping):
        return False
    if _MARKER in node:
        _check_target(node, location, route, time_owned)
        return True
    affected = set()
    for child, suffix, edge in _children(node):
        child_route = edge if route != "unsupported" and edge in ("properties", "items") else "unsupported"
        if _inspect_schema(child, location + suffix, child_route, time_owned or _TIME_MARKER in node):
            affected.add(edge)
    if affected:
        _check_ancestor(node, location, affected)
    return bool(affected)


def _check_target(node: Mapping, location: str, route: str, time_owned: bool) -> None:
    """检查整个由代码负责的 property，保留其完整约束供终验。

    @param node 标记目标
    @param location Schema 路径
    @param route 进入目标的边类型
    @param time_owned 是否已位于时间字段内
    @raises ValueError 标记位置、取值或归属冲突
    """
    if route != "properties" or node[_MARKER] is not True:
        _schema_error(location, "x-labelkit-postprocessor must equal true on an explicit property")
    if time_owned or _contains_annotation(node, _TIME_MARKER):
        _schema_error(location, "postprocessor fields must not overlap business time fields")
    if any(_contains_annotation(child, _MARKER) for child, _, _ in _children(node)):
        _schema_error(location, "postprocessor ownership markers must not be nested")


def _check_ancestor(node: Mapping, location: str, affected: set[str]) -> None:
    """检查投影会改变其后代的对象或数组约束。

    @param node 受影响祖先 Schema
    @param location Schema 路径
    @param affected 通向代码字段的 Schema 边
    @raises ValueError 类型或关键字不属于保真投影域
    """
    for keyword in sorted(_ANCESTOR_FORBIDDEN):
        if keyword in node:
            _schema_error(location, f"postprocessor projection forbids keyword {keyword!r}")
    properties = node.get("properties", {})
    direct = isinstance(properties, Mapping) and any(
        isinstance(child, Mapping) and _MARKER in child for child in properties.values())
    if direct and node.get("additionalProperties") is not False:
        _schema_error(location, "postprocessor property owner requires additionalProperties = false")
    if direct and any(key in node for key in ("minProperties", "maxProperties")):
        _schema_error(location, "postprocessor property owner forbids minProperties and maxProperties")
    for edge, kind in (("properties", "object"), ("items", "array")):
        if edge in affected and node.get("type") not in (kind, [kind, "null"], ["null", kind]):
            _schema_error(location, f"postprocessor ancestor type must match {edge!r} or its nullable form")
    if "items" in affected:
        for keyword in sorted(_ARRAY_FORBIDDEN):
            if keyword in node:
                _schema_error(location, f"postprocessor array projection forbids keyword {keyword!r}")
        if node.get("uniqueItems") is True:
            _schema_error(location, "postprocessor array projection forbids uniqueItems = true")


def project_postprocessor_schema(schema: Mapping) -> dict:
    """保留完整 Schema，派生删除代码负责字段的模型 Schema。

    @param schema 完整标注 Schema
    @return 独立的模型 Schema
    @raises ValueError 标记结构不能保真投影
    """
    plain = _copy_json(schema, False, set())
    if not _inspect_schema(plain, "$", "root", False):
        return plain
    projected = _project_schema_node(plain)
    try:
        Draft202012Validator.check_schema(projected)
    except SchemaError:
        _schema_error("$", "projected model Schema is invalid")
    return projected


def _project_schema_node(node: Mapping) -> dict:
    """按原 Schema 的 properties/items 结构投影 Schema 及数据示例。

    @param node 当前 Schema
    @return 对应模型 Schema
    """
    out = _copy_json(node, False, set())
    properties = node.get("properties")
    if isinstance(properties, Mapping):
        removed = {name for name, child in properties.items() if isinstance(child, Mapping) and _MARKER in child}
        out["properties"] = {name: (_project_schema_node(child) if isinstance(child, Mapping) else child)
                             for name, child in properties.items() if name not in removed}
        if "required" in node:
            out["required"] = [name for name in node["required"] if name not in removed]
    if isinstance(node.get("items"), Mapping):
        out["items"] = _project_schema_node(node["items"])
    if "default" in node:
        out["default"] = _project_instance(node["default"], node)
    if isinstance(node.get("examples"), (tuple, list)):
        out["examples"] = [_project_instance(item, node) for item in node["examples"]]
    return out


def _project_instance(value: object, schema: object) -> object:
    """仅沿明确的对象/数组 Schema 删除可达字段，保持其他值和数组顺序。

    @param value 候选或 Schema 示例值
    @param schema 对应的完整 Schema
    @return 深复制后的投影值
    """
    out = _copy_json(value, False, set())
    if not isinstance(schema, Mapping):
        return out
    if isinstance(out, dict) and isinstance(schema.get("properties"), Mapping):
        for name, child in schema["properties"].items():
            if name not in out:
                continue
            if isinstance(child, Mapping) and _MARKER in child:
                del out[name]
            else:
                out[name] = _project_instance(out[name], child)
    elif isinstance(out, list) and isinstance(schema.get("items"), Mapping):
        out = [_project_instance(item, schema["items"]) for item in out]
    return out


def project_postprocessor_instance(value: Mapping, schema: Mapping) -> dict:
    """投影完整或不完整候选，供 few-shot 与修复请求使用。

    @param value 候选对象
    @param schema M1 已检查的完整 Schema
    @return 删除代码负责字段后的独立对象
    """
    return _project_instance(value, schema)

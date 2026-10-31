"""M1 标注后处理的引用解析、模型 Schema 冻结与示例投影，以及区域引用注解（rule 81）。"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from labelkit.common.config._collect import _Collector
from labelkit.common.config._temporal import freeze_json
from labelkit.common.extensions.hooks import ResolvedHook
from labelkit.common.extensions.postprocessing import (
    _children,
    _contains_annotation,
    _copy_json,
    project_postprocessor_instance,
    project_postprocessor_schema,
    resolve_postprocessor,
)

_INTERNAL_KEYS = frozenset({"resolved_postprocessor", "model_user_schema", "model_frame_schema"})
_POSTPROCESSOR_MARKER = "x-labelkit-postprocessor"
_REGION_MARKER = "x-labelkit-region-ref"
_REGION_TYPES = ("string", ["string", "null"])


@dataclass(frozen=True)
class _RegionSite:
    """Schema 中一处区域引用注解。"""

    pointer: str   # 实例 JSON Pointer，数组项写 *
    owned: bool    # 位于代码负责字段或其子树内
    legal: bool    # 取值、类型与所在位置都合规


def reject_internal_postprocessing_fields(col: _Collector, file: str, data: dict) -> None:
    """只检查配置命名空间的内部字段，不扫描 few-shot 业务数据。

    @param col 错误聚合器
    @param file 工程文件位置
    @param data 原始工程配置
    """
    sites = [("", data)]
    for prefix, root in (("", data), ("frame.", data.get("frame", {}))):
        if not isinstance(root, dict):
            continue
        if prefix:
            sites.append(("[frame]", root))
        for section in ("annotate", "output"):
            sites.append((f"[{prefix}{section}]", root.get(section, {})))
        classes = root.get("class", {})
        if isinstance(classes, dict):
            sites.extend((f"[{prefix}class.{name}.annotate]", value.get("annotate", {}))
                         for name, value in classes.items() if isinstance(value, dict))
    for location, table in sites:
        if isinstance(table, dict):
            for key in sorted(_INTERNAL_KEYS.intersection(table)):
                col.error(f"{file}:{location}.{key}: internal field cannot be configured")
    output = data.get("output")
    if isinstance(output, dict) and "postprocessor" in output:
        col.error(f"{file}:[output].postprocessor: use annotate.postprocessor for annotation processing")


def resolve_config_postprocessor(col: _Collector, file: str, location: str,
                                 reference: str | None, root: Path) -> ResolvedHook | None:
    """把工程引用转换成冻结函数，聚合静态错误而不执行函数。

    @param col 错误聚合器
    @param file 工程文件位置
    @param location 配置节位置
    @param reference 原始文件与属性引用
    @param root 工程根目录
    @return 已解析函数或 None
    """
    if reference is None:
        return None
    try:
        return resolve_postprocessor(reference, root)
    except ValueError as error:
        col.error(f"{file}:{location}.postprocessor: {error}")
        return None


def check_parked_postprocessor_references(col: _Collector, file: str, raw: object, prefix: str,
                                         materialized: Mapping | None = None) -> None:
    """检查未物化类的显式后处理引用，不重复加载已生效的函数。

    @param col 错误聚合器
    @param file 工程文件位置
    @param raw 原始类配置表
    @param prefix class 或 frame.class
    @param materialized 已物化且已经检查引用的类视图
    """
    if not isinstance(raw, dict):
        return
    for name, sections in raw.items():
        if materialized is not None and name in materialized:
            continue
        section = sections.get("annotate") if isinstance(sections, dict) else None
        if not isinstance(section, dict) or "postprocessor" not in section:
            continue
        location = f"[{prefix}.{name}.annotate]"
        reference = section["postprocessor"]
        if not isinstance(reference, str) or not reference.strip():
            col.error(f"{file}:{location}.postprocessor: expected non-empty string")
            continue
        resolve_config_postprocessor(col, file, location, reference, Path(file).resolve().parent)


def compile_postprocessor_model(col: _Collector, file: str, location: str,
                                schema: Mapping | None) -> Mapping | None:
    """派生并深冻结模型 Schema，投影失败由 M1 聚合。

    @param col 错误聚合器
    @param file 工程文件位置
    @param location Schema 所属节
    @param schema 完整或已去时间的 Schema
    @return 冻结模型 Schema；输入缺失或投影失败时 None
    """
    if schema is None:
        return None
    try:
        return freeze_json(_strip_region_refs(project_postprocessor_schema(schema)))
    except ValueError as error:
        col.error(f"{file}:{location}: {error}")
        return None


def require_postprocessor(col: _Collector, file: str, location: str,
                          schema: Mapping | None, hook: ResolvedHook | None) -> None:
    """启用的有效 Schema 含代码负责字段时必须声明有效工程函数。

    @param col 错误聚合器
    @param file 工程文件位置
    @param location 生效标注节
    @param schema 生效 Schema
    @param hook 已解析工程函数
    """
    if hook is None and _contains_annotation(schema, "x-labelkit-postprocessor"):
        col.error(f"{file}:{location}.postprocessor: required for code-owned annotation fields")


def project_postprocessor_examples(examples: tuple, schema: Mapping | None) -> tuple:
    """在最终约束检查之后投影示例，并冻结每个模型输出。

    @param examples 通过最终约束的示例
    @param schema 完整或已去时间的有效 Schema
    @return 保留 input 的模型示例
    """
    if schema is None or not _contains_annotation(schema, "x-labelkit-postprocessor"):
        return examples
    return tuple(replace(item, output=freeze_json(project_postprocessor_instance(item.output, schema)))
                 for item in examples)


def check_region_refs(col: _Collector, file: str, location: str, schema: Mapping | None, image: bool) -> None:
    """rule 81：注解只能出现在图片模式、模型可见的字符串属性或字符串 items 上。

    @param col 错误聚合器
    @param file 工程文件位置
    @param location Schema 所属节
    @param schema 完整 Schema；缺失时不检查
    @param image 当前工程是否图片模式
    """
    if schema is None or not _contains_annotation(schema, _REGION_MARKER):
        return
    if not image:
        col.error(f'{file}:{location}: {_REGION_MARKER} requires segment.mode = "image"')
        return
    sites = tuple(_region_sites(_copy_json(schema, False, set()), "", "root", False))
    if any(site.owned for site in sites):
        col.error(f"{file}:{location}: {_REGION_MARKER} must not be used inside a {_POSTPROCESSOR_MARKER} field")
    if any(not site.owned and not site.legal for site in sites):
        col.error(f"{file}:{location}: {_REGION_MARKER} must equal true on a string property or string items")


def region_ref_paths(schema: Mapping | None) -> tuple[str, ...]:
    """解析被标记的实例路径；非法位置已由 ``check_region_refs`` 聚合报错。

    @param schema 完整 Schema
    @return JSON Pointer 元组，数组项写 ``*``，例如 ``/items/*/region_id``
    """
    if schema is None or not _contains_annotation(schema, _REGION_MARKER):
        return ()
    sites = _region_sites(_copy_json(schema, False, set()), "", "root", False)
    return tuple(site.pointer for site in sites if site.legal and not site.owned)


def _region_sites(node: object, pointer: str, route: str, owned: bool) -> Iterator[_RegionSite]:
    """按 Schema 位置遍历注解；default、examples、const 与 enum 数据不是 Schema。

    @param node 当前 Schema
    @param pointer 当前节点的实例路径
    @param route properties、items、root 或 unsupported
    @param owned 是否已在代码负责字段内
    @return 注解位置的迭代器
    """
    if not isinstance(node, Mapping):
        return
    owned = owned or _POSTPROCESSOR_MARKER in node
    if _REGION_MARKER in node:
        legal = (route in ("properties", "items") and node[_REGION_MARKER] is True
                 and node.get("type") in _REGION_TYPES)
        yield _RegionSite(pointer, owned, legal)
    for child, suffix, edge in _children(node):
        child_route = edge if route != "unsupported" and edge in ("properties", "items") else "unsupported"
        yield from _region_sites(child, pointer + _instance_token(suffix, edge), child_route, owned)


def _instance_token(suffix: str, edge: str) -> str:
    """把 Schema 位置后缀换成实例路径片段。

    @param suffix ``_children`` 给出的 ``/properties/<name>`` 或 ``/items``
    @param edge 进入子节点的 Schema 关键字
    @return RFC 6901 转义后的属性片段，或数组项的 ``/*``
    """
    if edge != "properties":
        return "/*"
    name = suffix[len("/properties/"):]
    return "/" + name.replace("~", "~0").replace("/", "~1")


def _strip_region_refs(schema: dict) -> dict:
    """从模型 Schema 删除注解；没有注解时原样返回，字节不变。

    @param schema 已投影的独立模型 Schema
    @return 发给端点与 L2 使用的模型 Schema
    """
    return _strip_region_node(schema) if _contains_annotation(schema, _REGION_MARKER) else schema


def _strip_region_node(node: Mapping) -> dict:
    """沿 properties 与 items 递归删除注解，其余键与键序不变；rule 81 已排除其他位置。

    @param node 当前 Schema
    @return 删除注解后的节点
    """
    out = {key: value for key, value in node.items() if key != _REGION_MARKER}
    properties = node.get("properties")
    if isinstance(properties, Mapping):
        out["properties"] = {name: _strip_region_node(child) if isinstance(child, Mapping) else child
                             for name, child in properties.items()}
    if isinstance(node.get("items"), Mapping):
        out["items"] = _strip_region_node(node["items"])
    return out

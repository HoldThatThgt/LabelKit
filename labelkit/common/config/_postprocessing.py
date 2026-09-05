"""M1 标注后处理的引用解析、模型 Schema 冻结与示例投影。"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Mapping

from labelkit.common.config._collect import _Collector
from labelkit.common.config._temporal import freeze_json
from labelkit.common.extensions.hooks import ResolvedHook
from labelkit.common.extensions.postprocessing import (
    _contains_annotation,
    project_postprocessor_instance,
    project_postprocessor_schema,
    resolve_postprocessor,
)

_INTERNAL_KEYS = frozenset({"resolved_postprocessor", "model_user_schema", "model_frame_schema"})


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
        return freeze_json(project_postprocessor_schema(schema))
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

"""M1 的 JSON Schema 装载/元校验与 few-shot 示例干跑(CONTRACTS §6.3 规则 13/14/17)。

普通输出、帧标注与按类标注 Schema，以及 v1.18 状态、结果和帧生成 Schema，共用
``_load_schema_pair`` 装载主体；各自的专属分支（byte 上限、保留键禁令与本地
``$ref`` 可解析性遍历）留在包装函数中，以保持稳定报错定位。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import urljoin

from jsonschema.exceptions import SchemaError
from jsonschema.validators import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from labelkit.common.config._collect import _Collector, _fmt, _Tbl
from labelkit.common.extensions.hooks import _thaw_json, normalize_violations

if TYPE_CHECKING:
    from labelkit.common.config.model import FewShotExample, FrameAnnotateConfig, OutputConfig

_logger = logging.getLogger("labelkit.config")

# 这些关键字位置上的值是数据而非子 Schema——其中形如 "$ref" 的字符串是字面内容,
# 不参与可解析性检查。
_SCHEMA_DATA_KEYS = frozenset({"const", "enum", "default", "examples"})


@dataclass(frozen=True)
class _SchemaSite:
    """一次 Schema 装载的位点捆包(报错定位前缀 + 名词)。"""

    file: str      # 报错定位用的配置文件路径字符串
    section: str   # 节名, 如 "output" / "frame.annotate" / "class.<name>.annotate"
    noun: str      # 该 Schema 在报错文案里的名词, 如 "user schema"


@dataclass(frozen=True)
class _GenerationSchemaRequest:
    """一份 v1.18 生成 Schema 的装载请求。"""

    file: str                                      # project.toml 定位路径
    section: str                                   # 不带方括号的节定位
    path: object                                   # schema_path 原始值
    inline: object                                 # schema_inline 原始值
    project_root: Path                             # 相对路径解析根
    max_bytes: int                                 # 原始 UTF-8 byte 上限


@dataclass(frozen=True)
class _DryRun:
    """一次 few-shot 干跑的调用面捆包(Schema 侧 + L2.5 回调侧)。"""

    file: str                     # 报错定位用的 project.toml 路径字符串
    elem_label: str               # 示例表数组定位标签, 如 "annotate.examples"
    validator: Any = None         # Draft202012Validator; None 表示跳过 Schema 侧干跑
    schema_key: str = ""          # 该 Schema 的源键名("schema_path" | "schema_inline")
    schema_section: str = "output"        # Schema 侧报错定位的节名
    schema_noun: str = "user schema"      # Schema 侧报错文案里的名词
    hook: Any = None              # L2.5 用户回调; None 表示跳过回调侧干跑
    hook_ref: str | None = None   # 回调引用串("module:function"), 用于违规归一化


def _collect_schema_refs(node: Any, base: str, out: list[tuple[str, str]]) -> None:
    """遍历 Schema 文档收集 ``(base_uri, $ref)`` 对。

    过程中跟踪嵌套 ``$id`` 引起的基 URI 变化(RFC 3986 join), 并跳过数据位置。

    @param node 当前遍历到的节点
    @param base 当前生效的基 URI
    @param out 结果累加列表(就地追加)
    """
    if isinstance(node, dict):
        nid = node.get("$id")
        if isinstance(nid, str) and nid:
            base = urljoin(base, nid)
        ref = node.get("$ref")
        if isinstance(ref, str):
            out.append((base, ref))
        for k, v in node.items():
            if k in _SCHEMA_DATA_KEYS:
                continue
            _collect_schema_refs(v, base, out)
    elif isinstance(node, list):
        for v in node:
            _collect_schema_refs(v, base, out)


def _unresolvable_refs(schema: dict) -> list[tuple[str, str]]:
    """CONTRACTS §6.3 规则 13(``$ref`` 可解析性, §12 #23)。

    每个 ``$ref`` 都须能在文档自身内解析——工具运行期从不取外部资源, 因此此处解析
    不了的引用必然让 M8 在每条记录上炸(spec 3.1 M1 契约: 不存在运行期配置错误)。
    尽力而为: 若引用机制本身无法摄入该文档则返回空表(规则 15 的运行期兜底仍在)。

    @param schema 已解析的 Schema 文档
    @return 按 ref 去重且确定性排序的 ``[(ref, reason)]``
    """
    try:
        resource = Resource.from_contents(schema, default_specification=DRAFT202012)
        root_uri = resource.id() or ""
        registry = Registry().with_resource(root_uri, resource).crawl()
    except Exception as e:
        # 尽力而为分支: 装载期 stderr 只承载聚合报告, 故用 debug 级别记录原因。
        _logger.debug("schema registry could not ingest the document: %s", e)
        return []
    pairs: list[tuple[str, str]] = []
    _collect_schema_refs(schema, root_uri, pairs)
    bad: dict[str, str] = {}
    for base, ref in pairs:
        if ref in bad:
            continue
        try:
            registry.resolver(base).lookup(ref)
        except Exception as e:
            # 异常即"引用不可解析"这一信号本身, 原因串随后由调用方聚合上报。
            _logger.debug("unresolvable schema reference: %s", ref)
            bad[ref] = str(e)
    return sorted(bad.items())


def _load_schema_pair(col: _Collector, site: _SchemaSite, sp: str | None,
                      si: str | None) -> tuple[dict, bool, str]:
    """通用 Schema 装载主体（§6.3 规则 13/14 与 v1.18 生成面共用）。

    依次执行: 恰一约束 → 读文件 → JSON 解析 → 顶层对象 → draft 2020-12 元校验 →
    顶层 ``type = "object"``。专属分支(``"_meta"`` 保留键、``$ref`` 遍历)留在包装函数。

    @param col 错误聚合器
    @param site 位点捆包(file/section/noun)
    @param sp ``schema_path`` 的取值
    @param si ``schema_inline`` 的取值
    @return (schema 字典, 是否可用, 生效源键名)
    """
    if sp is not None and si is not None:
        col.error(f"{site.file}:[{site.section}].schema_inline: exactly one of schema_path / "
                  f"schema_inline must be provided (mutually exclusive), got both set")
        return {}, False, "schema_inline"
    if sp is None and si is None:
        col.error(f"{site.file}:[{site.section}].schema_path: exactly one of schema_path or "
                  f"schema_inline must be provided, got neither")
        return {}, False, "schema_path"
    key = "schema_inline" if si is not None else "schema_path"
    text = si
    if sp is not None:
        try:
            text = Path(sp).read_text(encoding="utf-8")
        except OSError as e:
            col.error(f"{site.file}:[{site.section}].schema_path: cannot read schema file "
                      f"{_fmt(sp)}: {e}")
            return {}, False, key
    try:
        schema = json.loads(text)  # type: ignore[arg-type]
    except json.JSONDecodeError as e:
        col.error(f"{site.file}:[{site.section}].{key}: expected valid JSON, got a JSON "
                  f"parse error: {e}")
        return {}, False, key
    if not isinstance(schema, dict):
        col.error(f"{site.file}:[{site.section}].{key}: {site.noun} must be a JSON object at "
                  f"the top level, got {_fmt(schema)}")
        return {}, False, key
    return schema, _check_schema_shape(col, site, key, schema), key


def _load_generation_schema(
    col: _Collector,
    request: _GenerationSchemaRequest,
) -> Mapping[str, object] | None:
    """装载一份 v1.18 object-root Draft 2020-12 Schema。

    @param col 错误聚合器
    @param request 路径、定位与上限请求
    @return 通过元校验、本地引用与 byte 上限的 Schema，失败为 None
    """
    if request.path is None and request.inline is None:
        col.error(f"{request.file}:[{request.section}].schema_path: exactly one of "
                  "schema_path or schema_inline must be provided, got neither")
        return None
    path, inline = _generation_schema_sources(col, request)
    if path is None and inline is None:
        return None
    site = _SchemaSite(request.file, request.section, "generation schema")
    schema, ok, key = _load_schema_pair(col, site, path, inline)
    if schema and _schema_bytes(schema) > request.max_bytes:
        col.error(f"{request.file}:[{request.section}].{key}: generation schema exceeds "
                  f"{request.max_bytes} canonical UTF-8 bytes")
        ok = False
    if ok:
        ok = _check_schema_refs(col, site, key, schema)
    return schema if ok else None


def _generation_schema_sources(
    col: _Collector,
    request: _GenerationSchemaRequest,
) -> tuple[str | None, str | None]:
    """校验并规范化生成 Schema 的 path/inline 原始值。

    @param col 错误聚合器
    @param request Schema 请求
    @return 绝对 path 与 inline 文本
    """
    path = request.path
    inline = request.inline
    if path is not None and (not isinstance(path, str) or not path.strip()):
        col.error(f"{request.file}:[{request.section}].schema_path: expected non-empty string, "
                  f"got {_fmt(path)}")
        path = None
    if inline is not None and (not isinstance(inline, str) or not inline.strip()):
        col.error(f"{request.file}:[{request.section}].schema_inline: expected non-empty string, "
                  f"got {_fmt(inline)}")
        inline = None
    if isinstance(path, str):
        candidate = Path(path)
        path = str(candidate if candidate.is_absolute() else request.project_root / candidate)
        try:
            if Path(path).stat().st_size > request.max_bytes:
                col.error(f"{request.file}:[{request.section}].schema_path: schema file exceeds "
                          f"{request.max_bytes} bytes")
                return None, None
        except OSError:
            pass
    if isinstance(inline, str) and len(inline.encode("utf-8")) > request.max_bytes:
        col.error(f"{request.file}:[{request.section}].schema_inline: schema text exceeds "
                  f"{request.max_bytes} UTF-8 bytes")
        return None, None
    return path, inline


def _schema_bytes(schema: Mapping[str, object]) -> int:
    """计算 Schema 的 canonical UTF-8 byte 数。

    @param schema 已解析 Schema
    @return canonical JSON byte 数
    """
    text = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return len(text.encode("utf-8"))


def _check_schema_shape(col: _Collector, site: _SchemaSite, key: str,
                        schema: dict) -> bool:
    """对已解析的 Schema 做元校验与顶层 ``type`` 检查。

    @param col 错误聚合器
    @param site 位点捆包
    @param key 生效源键名
    @param schema 已解析的 Schema 字典
    @return 两项检查全过为 True
    """
    ok = True
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as e:
        col.error(f"{site.file}:[{site.section}].{key}: failed JSON Schema draft 2020-12 "
                  f"meta-schema validation: {e.message}")
        ok = False
    if schema.get("type") != "object":
        col.error(f'{site.file}:[{site.section}].{key}: {site.noun} top-level type must be '
                  f'"object", got {_fmt(schema.get("type"))}')
        ok = False
    return ok


def _check_schema_refs(col: _Collector, site: _SchemaSite, key: str,
                       schema: dict) -> bool:
    """对一份 Schema 做 ``$ref`` 可解析性遍历并逐条上报。

    @param col 错误聚合器
    @param site 位点捆包
    @param key 生效源键名
    @param schema 已解析的 Schema 字典
    @return 无悬空引用为 True
    """
    ok = True
    for ref, why in _unresolvable_refs(schema):
        col.error(f"{site.file}:[{site.section}].{key}: {site.noun} has an unresolvable "
                  f"reference ($ref {_fmt(ref)}): {why}")
        ok = False
    return ok


def _check_no_meta_key(col: _Collector, site: _SchemaSite, key: str,
                       schema: dict) -> bool:
    """禁止在顶层 ``properties`` 里声明保留键 ``"_meta"``(§6.3 信封字段由工具写入)。

    @param col 错误聚合器
    @param site 位点捆包
    @param key 生效源键名
    @param schema 已解析的 Schema 字典
    @return 未声明保留键为 True
    """
    props = schema.get("properties")
    if isinstance(props, dict) and "_meta" in props:
        col.error(f'{site.file}:[{site.section}].{key}: {site.noun} must not declare the '
                  f'reserved top-level key "_meta" (the 6.3 envelope fields are written by '
                  f'the tool), got properties containing "_meta"')
        return False
    return True


def _load_user_schema(col: _Collector, file: str,
                      output: OutputConfig) -> tuple[dict, bool]:
    """装载 ``[output]`` 用户 Schema(CONTRACTS §6.3 规则 13/14)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param output 已解析的 ``[output]`` 节
    @return (schema 字典, 是否可用)
    """
    site = _SchemaSite(file=file, section="output", noun="user schema")
    schema, ok, key = _load_schema_pair(col, site, output.schema_path, output.schema_inline)
    if not schema and not ok:
        return schema, ok        # 硬解析失败已上报(与抽取前的提前返回等价)
    ok = _check_no_meta_key(col, site, key, schema) and ok
    if ok:
        ok = _check_schema_refs(col, site, key, schema)
    return schema, ok


def _load_class_schema(col: _Collector, file: str, cname: str, sp: str | None,
                       si: str | None) -> dict | None:
    """装载 ``[class.<name>.annotate]`` 的按类标注 Schema。

    语义是**至多其一**(两者均缺 = 覆盖未声明, 回落全局 output.schema); 声明了就走
    ``_load_schema_pair`` 全套, 再加 output.schema 同款的 ``"_meta"`` 保留键禁令与
    ``$ref`` 可解析性遍历(运行期不取外部资源, 悬空引用必然每条记录都炸)。产物进入
    ``ClassView.schema``；None 表示未声明并回落全局 Schema。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param cname 序列类名
    @param sp ``schema_path`` 的取值
    @param si ``schema_inline`` 的取值
    @return 解析后的 Schema; 未声明或不可用(错误已聚合上报)时返回 None
    """
    if sp is None and si is None:
        return None
    site = _SchemaSite(file=file, section=f"class.{cname}.annotate",
                       noun="per-class annotation schema")
    schema, ok, key = _load_schema_pair(col, site, sp, si)
    ok = _check_no_meta_key(col, site, key, schema) and ok
    if ok:
        ok = _check_schema_refs(col, site, key, schema)
    return schema if ok else None


def _load_frame_gen(col: _Collector, file: str, cname: str,
                    sections: dict) -> tuple[str | None, Mapping[str, object] | None]:
    """解析 v1.18 frame class 的对象生成契约。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param cname 帧类名
    @param sections 该帧类的原始覆盖节字典
    @return 生成指令与完整对象 Schema
    """
    sub = sections.get("generate")
    if not isinstance(sub, dict):
        return None, None            # 缺节 / 非表(非表已由白名单校验定位上报)
    section = f"frame.class.{cname}.generate"
    t = _Tbl(col, file, f"[{section}]", sub)
    instruction = t.get_str("instruction", None, nonempty=True)
    path = t.get_str("schema_path", None, nonempty=True)
    inline = t.get_str("schema_inline", None, nonempty=True)
    schema = _load_generation_schema(col, _GenerationSchemaRequest(
        file=file, section=section, path=path, inline=inline,
        project_root=Path(file).resolve().parent, max_bytes=65536,
    ))
    return instruction, schema


def _load_frame_schema(col: _Collector, file: str,
                       fa: FrameAnnotateConfig) -> tuple[dict, bool]:
    """v1.12(SPEC-frame-annotation §3.1 帧 Schema 恰一行): 装载帧级输出 Schema。

    镜像 output.schema 全套分支(恰一 / 读取 / JSON 解析 / 顶层对象 / 元校验 /
    ``$ref`` 可解析性 + 调用方的 examples 干跑)。唯一不镜像的分支是 ``"_meta"``
    保留键检查——帧标注对象落于 ``_meta.stream.members[].annotation`` 内部, 与 §6.3
    信封字段无冲突面。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param fa 已解析的 ``[frame.annotate]`` 节
    @return (schema 字典, 是否可用)
    """
    site = _SchemaSite(file=file, section="frame.annotate", noun="frame schema")
    schema, ok, key = _load_schema_pair(col, site, fa.schema_path, fa.schema_inline)
    if ok:
        ok = _check_schema_refs(col, site, key, schema)
    return schema, ok


def _dryrun_fewshot(col: _Collector, examples: tuple[FewShotExample, ...],
                    target: _DryRun) -> tuple[bool, bool]:
    """把 few-shot 示例输出过一遍用户 Schema(规则 14)与 output.validator 回调(规则 17)。

    全局 ``[[annotate.examples]]``、v1.7 按类 ``[[class.<name>.annotate.examples]]`` 与
    v1.12 帧级两族示例集共用本函数(``target.elem_label`` 承载定位)。任一侧的
    ``validator`` / ``hook`` 为 None 即跳过该侧。

    @param col 错误聚合器
    @param examples 待干跑的示例集
    @param target 调用面捆包
    @return (schema_alive, hook_alive): False 表示该层的后续示例集不必再跑——病因在
            Schema 或回调自身(悬空 ``$ref`` / 回调抛异常), 一条错误已足够
    """
    schema_alive = _dryrun_schema(col, examples, target)
    hook_alive = _dryrun_hook(col, examples, target)
    return schema_alive, hook_alive


def _dryrun_schema(col: _Collector, examples: tuple[FewShotExample, ...],
                   target: _DryRun) -> bool:
    """Schema 侧干跑: 逐条示例输出过 jsonschema 校验。

    @param col 错误聚合器
    @param examples 待干跑的示例集
    @param target 调用面捆包
    @return Schema 是否仍可用(``$ref`` 解析兜底未触发)
    """
    if target.validator is None:
        return True
    for i, ex in enumerate(examples, 1):
        try:
            errs = sorted(target.validator.iter_errors(ex.output),
                          key=lambda e: list(e.absolute_path))
        except Exception as e:
            # 规则 13 静态遍历看不见的解析失败兜底(如 $dynamicRef): iter_errors 会抛
            # referencing 异常。按 spec 3.1.5 它必须并入聚合 ConfigError(退出码 2),
            # 绝不能作为未捕获崩溃逃逸(退出码 4)。一条错误足矣——病因是 Schema 自身。
            col.error(f"{target.file}:[{target.schema_section}].{target.schema_key}: "
                      f"{target.schema_noun} has an unresolvable reference, cannot validate "
                      f"the [[{target.elem_label}]] example outputs: {e}")
            return False
        if errs:
            e0 = errs[0]
            ptr = "/" + "/".join(str(x) for x in e0.absolute_path)
            col.error(f"{target.file}:[[{target.elem_label}]][{i}].output: failed "
                      f"{target.schema_noun} validation: {ptr}: {e0.message}")
    return True


def _dryrun_hook(col: _Collector, examples: tuple[FewShotExample, ...],
                 target: _DryRun) -> bool:
    """L2.5 回调侧干跑: 用户自己的校验器拒掉的示例是配置错误, 启动期就揪出。

    @param col 错误聚合器
    @param examples 待干跑的示例集
    @param target 调用面捆包
    @return 回调是否仍可用(未抛异常)
    """
    if target.hook is None:
        return True
    for i, ex in enumerate(examples, 1):
        try:
            violations = normalize_violations(target.hook(_thaw_json(ex.output), None),
                                              target.hook_ref)
        except Exception as e:   # 回调自身有 bug——按配置错误上报, 而非退出码 4
            col.error(f"{target.file}:[output].validator: the callback raised while "
                      f"dry-running few-shot example {i}: {type(e).__name__}: {e}")
            return False
        if violations:
            col.error(f"{target.file}:[[{target.elem_label}]][{i}].output: failed the "
                      f"output.validator callback: {violations[0]}")
    return True

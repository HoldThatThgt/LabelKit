"""M1 的评分准则(rubric)解析与打包默认准则装载(spec 3.1.4 rubric 行 / 附录 A)。

一份"生效准则"由两部分决定: 已回落默认值的非空选择器(``default:*`` 或
``"inline"``), 以及可选的内联表 ``[rubric]`` / ``[class.<name>.rubric]``。全局面
与按类视图(R7)共用同一套解析逻辑, 仅由 ``_RubricSite.scope`` 平移报错定位。
"""
from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from importlib import resources
from typing import Any, Literal

from labelkit.common.config._collect import (
    _MISSING,
    _KEY_RE,
    _Collector,
    _fmt,
    _GT0,
    _Tbl,
)
from labelkit.common.config.model import Criterion, Rubric

_logger = logging.getLogger("labelkit.config")

# 打包默认准则的选择器 → 包内文件名(labelkit/data/rubrics/)。
_RUBRIC_PKG_FILES: dict[str, str] = {
    "default:text": "default_text.toml",
    "default:ui": "default_ui.toml",
    "default:trajectory": "default_trajectory.toml",   # v1.8 (S29)
}

# 两个选择器位点(全局 [quality].rubric 与按类 [class.<name>.quality].rubric)共同
# 接受的取值: 三份打包默认准则 + "inline"。
_RUBRIC_SELECTORS = ("default:text", "default:ui", "default:trajectory", "inline")


@dataclass(frozen=True)
class _RubricSite:
    """一次准则解析/校验的位点捆包(把 file/selector/modality/scope 收成一个参数)。"""

    file: str            # 报错定位用的 project.toml 路径字符串
    selector: str        # 准则选择器("inline" | "default:*"), 或校验时的报错定位键名
    modality: str = ""   # 运行模态(内联缺表时决定回落哪份打包默认准则)
    scope: str = ""      # "" = 全局 [rubric]; "class.<name>" = 按类视图(R7)


def default_rubric(
        name: Literal["default:text", "default:ui", "default:trajectory"]) -> Rubric:
    """装载一份打包默认准则(labelkit/data/rubrics/*.toml, 经 importlib.resources)。

    @param name 打包准则选择器
    @return 解析后的 ``Rubric``
    @raises ValueError 选择器不在三份打包准则之内
    """
    try:
        fname = _RUBRIC_PKG_FILES[name]
    except KeyError:
        raise ValueError(
            f'unknown default rubric {name!r}; expected "default:text", '
            f'"default:ui" or "default:trajectory"'
        ) from None
    text = (resources.files("labelkit") / "data" / "rubrics" / fname).read_text(encoding="utf-8")
    data = tomllib.loads(text)
    criteria = tuple(
        Criterion(
            key=c["key"],
            description=c["description"],
            pairwise_prompt=c["pairwise_prompt"],
            weight=float(c.get("weight", 1.0)),
            pointwise_levels=tuple(c.get("pointwise_levels", ())),
        )
        for c in data.get("criteria", ())
    )
    return Rubric(name=data["name"], criteria=criteria)


def _fallback_default_rubric(modality: str) -> Rubric:
    """内联准则不可用时按模态回落到打包默认准则。

    @param modality 运行模态
    @return 打包默认准则; 连打包文件都装不上时返回空准则
    """
    try:
        return default_rubric("default:ui" if modality == "ui" else "default:text")
    except Exception as e:  # pragma: no cover — 打包文件出厂即合法
        _logger.warning("packaged default rubric failed to load: %s", e)
        return Rubric(name="inline", criteria=())


def _parse_criteria(col: _Collector, file: str, raw: Any,
                    label: str = "rubric.criteria") -> tuple[Criterion, ...]:
    """解析一个 ``[[<label>]]`` 表数组: 键模式/唯一性、必填字段与 weight > 0。

    (spec 3.1.4 rubric 行; 逐元素定位报错。)

    @param col 错误聚合器
    @param file 报错定位用的配置文件路径字符串
    @param raw 原始表数组
    @param label 表数组名, 按类视图时平移为 ``class.<name>.rubric.criteria``
    @return 解析出的 ``Criterion`` 元组
    """
    if not isinstance(raw, list):
        col.error(f"{file}:[[{label}]]: expected array of tables, got {_fmt(raw)}")
        return ()
    criteria: list[Criterion] = []
    seen_keys: set[str] = set()
    for i, sub in enumerate(raw, 1):
        elem_label = f"[[{label}]][{i}]"
        if not isinstance(sub, dict):
            col.error(f"{file}:{elem_label}: expected table, got {_fmt(sub)}")
            continue
        t = _Tbl(col, file, elem_label, sub)
        key = _take_criterion_key(col, file, elem_label, t, seen_keys)
        criteria.append(_read_criterion_body(t, key or f"criterion_{i}"))
    return tuple(criteria)


def _take_criterion_key(col: _Collector, file: str, elem_label: str, t: _Tbl,
                        seen_keys: set[str]) -> str | None:
    """读取并校验一条准则的 ``key``: 必填非空、匹配 ``[a-z0-9_]+``、表内唯一。

    @param col 错误聚合器
    @param file 报错定位用的配置文件路径字符串
    @param elem_label 该元素的定位标签(含 1 起序号)
    @param t 该元素表的读取器
    @param seen_keys 已出现过的键集合(就地更新)
    @return 合法键名; 缺失或违规时返回 None(调用方用序号占位)
    """
    key = t.get_str("key", None, required=True, nonempty=True)
    if key is not None and not _KEY_RE.fullmatch(key):
        col.error(f"{file}:{elem_label}.key: expected a match of [a-z0-9_]+, got {_fmt(key)}")
        key = None
    if key is not None:
        if key in seen_keys:
            col.error(f"{file}:{elem_label}.key: key must be unique, got duplicate {_fmt(key)}")
        seen_keys.add(key)
    return key


def _read_criterion_body(t: _Tbl, key: str) -> Criterion:
    """读取一条准则的其余字段并物化 ``Criterion``。

    @param t 该元素表的读取器
    @param key 已定名的准则键(违规时为调用方给的占位名)
    @return ``Criterion``
    """
    description = t.get_str("description", "", required=True, nonempty=True) or ""
    pairwise_prompt = t.get_str("pairwise_prompt", "", required=True, nonempty=True) or ""
    weight = t.get_float("weight", 1.0, bound=_GT0)
    pointwise_levels = t.get_str_tuple("pointwise_levels", ())
    t.finish()
    return Criterion(
        key=key,
        description=description,
        pairwise_prompt=pairwise_prompt,
        weight=weight,
        pointwise_levels=pointwise_levels,
    )


def _resolve_rubric(col: _Collector, site: _RubricSite,
                    raw: Any) -> tuple[Rubric, bool]:
    """由(已回落默认值的非空)选择器与可选内联表解析出一份生效准则。

    load() 收尾的内联准则逻辑抽出成本函数, 使按类视图能用合并后的选择器复跑
    (R7); ``site.scope`` 只平移报错/告警定位([rubric] 与 [class.<name>.rubric])。

    @param col 错误聚合器
    @param site 位点捆包(file/selector/modality/scope)
    @param raw 内联准则表; 缺省为 None
    @return (生效准则, 是否来自内联表)
    """
    prefix = f"{site.scope}." if site.scope else ""
    if site.selector == "inline":
        return _resolve_inline_rubric(col, site, raw, prefix)
    try:
        rubric = default_rubric(site.selector)  # type: ignore[arg-type]
    except Exception as e:  # pragma: no cover — 打包文件出厂即合法
        col.error(f"{site.selector}: failed to load default rubric: {e}")
        rubric = Rubric(name=site.selector, criteria=())
    if raw is not None:
        col.warn(f"{site.file}:[[{prefix}rubric.criteria]]: quality.rubric = "
                 f"{_fmt(site.selector)}, the inline rubric has no effect and is ignored")
    return rubric, False


def _resolve_inline_rubric(col: _Collector, site: _RubricSite, raw: Any,
                           prefix: str) -> tuple[Rubric, bool]:
    """解析 ``rubric = "inline"`` 分支的内联准则表。

    @param col 错误聚合器
    @param site 位点捆包
    @param raw 内联准则表; None 表示未提供(硬错误)
    @param prefix 定位前缀("" 或 "class.<name>.")
    @return (生效准则, 是否来自内联表)
    """
    if raw is None:
        col.error(f'{site.file}:[{prefix}quality].rubric: rubric = "inline" but '
                  f"[[{prefix}rubric.criteria]] is not provided")
        return _fallback_default_rubric(site.modality), False
    t = _Tbl(col, site.file, f"[{prefix}rubric]", raw)
    name = t.get_str("name", None, required=True, nonempty=True)
    raw_criteria = t.take("criteria")
    t.finish()
    if raw_criteria is _MISSING or (isinstance(raw_criteria, list) and not raw_criteria):
        col.error(f"{site.file}:[{prefix}rubric].criteria: criteria must not be empty, "
                  f"expected a non-empty array of tables")
        criteria: tuple[Criterion, ...] = ()
    else:
        criteria = _parse_criteria(col, site.file, raw_criteria,
                                   label=f"{prefix}rubric.criteria")
    return Rubric(name=name or "inline", criteria=criteria), True


def _check_pointwise_rubric(col: _Collector, site: _RubricSite, rubric: Rubric,
                            is_inline: bool) -> None:
    """pointwise 模式要求每条准则恰好 6 个等级(spec 3.1.4 rubric 行)。

    v1.7 起对每个不同的(生效模式 × 生效准则)组合各跑一次——全局与按类(R7);
    调用方对已检查过的准则去重, 使共享表只被报一次。

    @param col 错误聚合器
    @param site 位点捆包(此处 ``selector`` 承载报错定位键名)
    @param rubric 待检查的生效准则
    @param is_inline 该准则是否来自内联表(决定定位前缀形态)
    """
    prefix = f"{site.scope}." if site.scope else ""
    for i, c in enumerate(rubric.criteria, 1):
        if len(c.pointwise_levels) != 6:
            loc = (f"{site.file}:[[{prefix}rubric.criteria]][{i}].pointwise_levels" if is_inline
                   else f"{site.selector}:criteria[{i}].pointwise_levels")
            col.error(f"{loc}: pointwise mode requires exactly 6 levels (0-5), "
                      f"got {len(c.pointwise_levels)}")

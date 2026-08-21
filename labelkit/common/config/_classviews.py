"""M1 的按类视图物化: ``[class.<name>.*]``(v1.7) 与 ``[frame.class.<name>.*]``(v1.12)。

两个命名空间都由 M1 **显名拥有**(R25 家族): 白名单外的节/键是 CONFIG_ERROR, 不走
前向兼容 WARN。每个已声明的类都会得到一份合并视图(零覆盖的类也有), 使下游算子在
运行期永不回退。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any

from jsonschema.validators import Draft202012Validator

from labelkit.common.config._collect import (
    _GE0,
    _UNIT_CLOSED,
    _UNIT_HALF_OPEN,
    _Collector,
    _fmt,
    _int_pair,
    _Tbl,
)
from labelkit.common.config._rubrics import (
    _RUBRIC_SELECTORS,
    _RubricSite,
    _check_pointwise_rubric,
    _fallback_default_rubric,
    _resolve_rubric,
    default_rubric,
)
from labelkit.common.config._schemas import (
    _DryRun,
    _dryrun_fewshot,
    _load_class_schema,
    _load_frame_gen,
)
from labelkit.common.config._sections import (
    _parse_examples,
    _parse_frame_rules,
    _parse_frame_windows,
    _parse_sequence_rules,
    _parse_styles,
    _parse_tiers,
    _parse_time_fields,
    _parse_duration_resources,
)
from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ClassView,
    ExtractConfig,
    FrameAnnotateConfig,
    FrameClassifyConfig,
    FrameClassView,
    GenerateConfig,
    QualityConfig,
    Rubric,
    TierSpec,
    VerifyConfig,
)

# v1.7 [class.<name>.<section>] 覆盖白名单(spec 5.2 / R25): 本表之外的节与键是
# CONFIG_ERROR 而非前向兼容警告——[class.*] 命名空间由 M1 显名拥有。"rubric" 是
# quality.rubric = "inline" 的按类内联准则子表伙伴(R7)。v1.8 增 "extract"(仅
# instruction, S2); segment 不在白名单内——它跑在 classify 之前, 类标签尚不存在
# (链序因果)。
# v1.13 增两族键: annotate 的 schema_path/schema_inline(裁决·按类标注 Schema,
# 覆盖语义、缺省回落全局 output.schema)与 generate 的 sequences/len_range
# (时间流形态的按类配额与序列长度区间载体)。
# v1.15(裁决·表级原子覆盖)增 generate 第七键 tiers(按类档位表, 整表取代全局表;
# 未声明即回落全局表)。
_CLASS_SECTION_KEYS: dict[str, tuple[str, ...]] = {
    "quality": ("mode", "rounds", "rubric", "threshold", "selection", "top_ratio"),
    "annotate": ("instruction", "examples", "schema_path", "schema_inline"),
    "generate": ("instruction", "styles", "num_per_record", "temperature",
                 "len_range", "tiers", "frame_rules", "frame_windows", "sequence_rules"),
    "verify": ("extra_criteria",),
    "extract": ("instruction",),
}
_CLASS_SECTIONS = ("quality", "rubric", "annotate", "generate", "verify", "extract")

# 质量选择组(R6): 提供其中**任一**键的类接管整组——全局侧的取值在类覆盖生效前先退回
# 内建默认, 于是"全局 threshold + 类 top_ratio"(或反之)绝不会伪共存。
_SELECTION_GROUP = ("selection", "threshold", "top_ratio")

# v1.12 [frame.class.<name>.<section>] 覆盖白名单(SPEC-frame-annotation §3.1): 帧类
# 命名空间与 [class.*] 同为 M1 显名拥有(R25 家族)。annotate 节三键:
# instruction / examples / enabled。
# v1.13(裁决·帧类生成面)增 generate 节三键: instruction(时间流生成形态下每个帧类
# 必填)+ schema_path/schema_inline(至多其一; 均缺 = 纯文本帧)——该节**仅时间流生成
# 形态合法**, 非本形态出现是定向 CONFIG_ERROR(在约束簇上报)。
# v1.14(裁决·绑定即剔除)增第四键 time_fields(时间语义字段绑定子表)——不入白名单的话
# 该子表会被下面的白名单循环判成未知键 CONFIG_ERROR。
_FRAME_CLASS_SECTION_KEYS: dict[str, tuple[str, ...]] = {
    "annotate": ("instruction", "examples", "enabled"),
    "generate": ("instruction", "schema_path", "schema_inline", "time_fields",
                  "duration_s", "resources"),
}
_FRAME_CLASS_SECTIONS = tuple(_FRAME_CLASS_SECTION_KEYS)


def _avail(names: tuple[str, ...] | dict[str, Any]) -> str:
    """把一组可用名字渲染成报错文案里的清单。

    @param names 可用名字集合
    @return 逗号分隔的清单; 空集渲染为 "(none)"
    """
    return ", ".join(names) if names else "(none)"


@dataclass(frozen=True)
class _ClassBases:
    """按类覆盖的全局基线捆包(把五个基线节收成一个参数对象)。"""

    quality: QualityConfig     # 全局 [quality](rubric 字段已回填生效选择器)
    annotate: AnnotateConfig   # 全局 [annotate]
    generate: GenerateConfig   # 全局 [generate]
    verify: VerifyConfig       # 全局 [verify]
    extract: ExtractConfig     # 全局 [extract]


@dataclass(frozen=True)
class _MergedClass:
    """一个序列类的覆盖合并结果(准则与 Schema 的装载留给调用方)。"""

    quality: QualityConfig      # 合并后的质量配置
    annotate: AnnotateConfig    # 合并后的标注配置
    generate: GenerateConfig    # 合并后的生成配置
    verify: VerifyConfig        # 合并后的复核配置
    extract: ExtractConfig      # 合并后的摘取配置
    rubric_raw: dict | None     # [class.<name>.rubric] 原始表(R7 需要合并后的选择器)
    examples_provided: bool     # 该类是否自带 few-shot 示例(决定是否干跑)
    schema_path: str | None     # v1.13 按类标注 Schema 的文件源
    schema_inline: str | None   # v1.13 按类标注 Schema 的内联源
    tiers: tuple[TierSpec, ...] | None = None
                                # v1.15 按类档位表; None = 未声明(回落全局表)
    frame_rules: tuple[Any, ...] | None = None
                                # v1.17 按类 frame_rules; None = 继承全局, 空元组 = 清空
    frame_windows: tuple[Any, ...] | None = None
                                # v1.17 按类 frame_windows; 三态同 frame_rules
    sequence_rules: tuple[Any, ...] | None = None
                                # v1.17 按类 sequence_rules; 三态整表


@dataclass
class _GlobalDryRun:
    """全局 Schema 与 L2.5 回调的干跑面(两个存活标志在按类循环中就地更新)。"""

    validator: Any = None         # 全局用户 Schema 的校验器; None = 跳过 Schema 侧
    schema_key: str = ""          # 全局 Schema 的源键名
    schema_ok: bool = False       # 全局 Schema 是否通过装载
    schema_alive: bool = True     # 全局 Schema 的 $ref 解析兜底是否仍未触发
    hook: Any = None              # output.validator 回调
    hook_ref: str | None = None   # 回调引用串("module:function")
    hook_alive: bool = True       # 回调是否仍未抛异常


@dataclass(frozen=True)
class _ViewInputs:
    """物化按类视图所需的全局取值捆包。"""

    file: str                # 报错定位用的 project.toml 路径字符串
    modality: str            # 运行模态(内联准则缺表时的回落依据)
    selector: str            # 全局生效的准则选择器
    rubric: Rubric           # 全局生效准则
    rubric_is_inline: bool   # 全局准则是否来自内联表
    bases: _ClassBases       # 各节全局基线
    dryrun: _GlobalDryRun    # 全局 Schema/回调干跑面(可变)


@dataclass
class _FrameViewCtx:
    """物化帧类视图所需的上下文(``schema_alive`` 在循环中就地更新)。"""

    file: str                          # 报错定位用的 project.toml 路径字符串
    classify: FrameClassifyConfig      # [frame.classify] 节(承载帧类表)
    annotate: FrameAnnotateConfig      # [frame.annotate] 节(作为覆盖基线)
    class_raw: Any                     # [frame.class.<name>.*] 原始节
    validator: Any = None              # 帧级 Schema 校验器; None = 跳过干跑
    schema_key: str = ""               # 帧级 Schema 的源键名
    schema_alive: bool = True          # 帧级 Schema 的 $ref 解析兜底是否仍未触发


# ── 序列类覆盖合并 ──────────────────────────────────────────────────────────


def _check_class_whitelist(col: _Collector, file: str, cname: str,
                           sections: dict) -> None:
    """R25 白名单校验: ``[class.*]`` 之外的节名与键名都是 CONFIG_ERROR。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param cname 序列类名
    @param sections 该类的原始覆盖节字典
    """
    for sect, sub in sections.items():
        if sect not in _CLASS_SECTIONS:
            col.error(f"{file}:[class.{cname}.{sect}]: section is not in the [class.*] "
                      f"override whitelist (available: {_avail(_CLASS_SECTIONS)})")
            continue
        if not isinstance(sub, dict):
            col.error(f"{file}:[class.{cname}.{sect}]: expected table, got {_fmt(sub)}")
            continue
        if sect == "rubric":
            continue   # 结构由 _resolve_rubric 校验(与全局 [rubric] 同款)
        allowed = _CLASS_SECTION_KEYS[sect]
        directed = {"sequences": "[[generate.stream.quotas]]",
                    "rules": f"[[class.{cname}.generate.frame_rules]]",
                    "windows": f"[[class.{cname}.generate.frame_windows]]"}
        for k in sub:
            if k not in allowed:
                if sect == "generate" and k in directed:
                    col.error(f"{file}:[class.{cname}.generate].{k}: this key was "
                              f"removed in v1.17 - use {directed[k]} instead (rule 62)")
                    continue
                col.error(f"{file}:[class.{cname}.{sect}].{k}: [class.*.{sect}] cannot "
                          f"override this key (whitelist: {_avail(allowed)})")


def _merge_class_sections(col: _Collector, file: str, cname: str, sections: dict,
                          bases: _ClassBases) -> _MergedClass:
    """把一个类的 ``[class.<name>.*]`` 覆盖节合并到已解析的全局配置上。

    (spec 5.2 v1.7; v1.8 增 extract 节, S2。)按键溯源: 类提供的键覆盖全局值, 其余
    继承。``bases.quality`` 的 ``rubric`` 字段承载已回落默认值的全局准则选择器。
    ``[class.<name>.rubric]`` 表不在此消费: R7 的准则复解析需要合并后的选择器, 故
    原样回传。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param cname 序列类名
    @param sections 该类的原始覆盖节字典
    @param bases 各节全局基线
    @return ``_MergedClass``
    """
    _check_class_whitelist(col, file, cname, sections)

    def _sect(name: str) -> dict:
        """取一个覆盖节, 缺节或非表都退化为空表。

        @param name 节名
        @return 覆盖节字典
        """
        sub = sections.get(name)
        return sub if isinstance(sub, dict) else {}

    quality = _merge_class_quality(col, file, cname, _sect("quality"), bases.quality)
    annotate, examples_provided, schema_path, schema_inline = _merge_class_annotate(
        col, file, cname, _sect("annotate"), bases.annotate)
    generate, tiers, frame_rules, frame_windows, sequence_rules = _merge_class_generate(
        col, file, cname, _sect("generate"), bases.generate)
    return _MergedClass(
        quality=quality, annotate=annotate, generate=generate,
        verify=_merge_class_verify(col, file, cname, _sect("verify"), bases.verify),
        extract=_merge_class_extract(col, file, cname, _sect("extract"), bases.extract),
        rubric_raw=(sections.get("rubric")
                    if isinstance(sections.get("rubric"), dict) else None),
        examples_provided=examples_provided,
        schema_path=schema_path, schema_inline=schema_inline, tiers=tiers,
        frame_rules=frame_rules, frame_windows=frame_windows,
        sequence_rules=sequence_rules,
    )


def _merge_class_annotate(col: _Collector, file: str, cname: str, a_over: dict,
                          base: AnnotateConfig) -> tuple[AnnotateConfig, bool,
                                                         str | None, str | None]:
    """``[class.<name>.annotate]`` 的合并, 外加 v1.13 按类标注 Schema 的两个源键取值。

    Schema 的装载与元校验由视图物化侧的 ``_load_class_schema`` 统一执行(干跑要与全局
    hook 一起做), 此处只取值。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param cname 序列类名
    @param a_over 该类的 annotate 覆盖表
    @param base 全局标注基线
    @return (合并后的标注配置, 是否自带示例, schema_path 取值, schema_inline 取值)
    """
    t = _Tbl(col, file, f"[class.{cname}.annotate]", a_over)
    examples_provided = "examples" in a_over
    annotate = replace(
        base,
        instruction=t.get_str("instruction", base.instruction, nonempty=True),
        examples=(_parse_examples(col, file, t.take("examples"),
                                  section=f"class.{cname}.annotate")
                  if examples_provided else base.examples),
    )
    schema_path = t.get_str("schema_path", None, nonempty=True)
    schema_inline = t.get_str("schema_inline", None, nonempty=True)
    return annotate, examples_provided, schema_path, schema_inline


def _merge_class_quality(col: _Collector, file: str, cname: str, q_over: dict,
                         base_quality: QualityConfig) -> QualityConfig:
    """``[class.<name>.quality]`` 的合并(R6 选择组接管 + 合并视图上的规则 6 家族)。

    选择组语义: 类提供 selection/threshold/top_ratio 任一 ⇒ 接管整组, 未提供的组内键
    从内建默认重启(而非全局值), 全局 threshold 与类 top_ratio 不会伪共存。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param cname 序列类名
    @param q_over 该类的 quality 覆盖表
    @param base_quality 全局质量基线
    @return 合并后的 ``QualityConfig``
    """
    group_taken = any(k in q_over for k in _SELECTION_GROUP)
    base_q = (replace(base_quality, selection="threshold", threshold=None, top_ratio=None)
              if group_taken else base_quality)
    t = _Tbl(col, file, f"[class.{cname}.quality]", q_over)
    quality = replace(
        base_q,
        mode=t.get_str("mode", base_q.mode, enum=("pairwise", "pointwise")),
        rounds=t.get_int("rounds", base_q.rounds, minimum=1),
        rubric=t.get_str("rubric", base_q.rubric, enum=_RUBRIC_SELECTORS),
        threshold=t.get_float("threshold", base_q.threshold, bound=_UNIT_CLOSED),
        selection=t.get_str("selection", base_q.selection, enum=("threshold", "top_ratio")),
        top_ratio=t.get_float("top_ratio", base_q.top_ratio, bound=_UNIT_HALF_OPEN),
    )
    if not group_taken:
        # 未被接管的组已在全局侧校验过, 重复检查只会造成重复报错。
        return quality
    _check_class_selection_group(col, file, cname, q_over, quality)
    return quality


def _check_class_selection_group(col: _Collector, file: str, cname: str, q_over: dict,
                                 quality: QualityConfig) -> None:
    """对被类接管的选择组跑规则 6 家族(必填/互斥/no-op 警告)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param cname 序列类名
    @param q_over 该类的 quality 覆盖表
    @param quality 合并后的质量配置
    """
    if quality.selection == "top_ratio":
        if quality.top_ratio is None and "top_ratio" not in q_over:
            col.error(f'{file}:[class.{cname}.quality].top_ratio: required when selection = '
                      f'"top_ratio", expected a number in (0,1]')
        if quality.threshold is not None:
            col.error(f'{file}:[class.{cname}.quality].threshold: mutually exclusive with '
                      f'quality.top_ratio (must not be set when selection = "top_ratio")')
    elif "top_ratio" in q_over:
        # 与全局 P3-7 警告同款的静默陷阱护栏。
        col.warn(f'{file}:[class.{cname}.quality].top_ratio: selection is still the default '
                 f'"threshold", so this key has no effect - to keep a fixed ratio also set '
                 f'selection = "top_ratio"')


def _merge_class_generate(col: _Collector, file: str, cname: str, g_over: dict,
                          base: GenerateConfig,
                          ) -> tuple[GenerateConfig, tuple[TierSpec, ...] | None,
                                     tuple[Any, ...] | None,
                                     tuple[Any, ...] | None,
                                     tuple[Any, ...] | None]:
    """``[class.<name>.generate]`` 的合并(按键溯源)。

    v1.15(裁决·载体 ClassView 顶层字段): 第七键 ``tiers`` 与其余六键分道——它**不落**
    ``GenerateConfig``(否则纯档位覆盖会被编排器误判为"估算失真型按类覆盖"), 而是随
    返回值另交给视图物化侧挂在 ``ClassView.tiers`` 上。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param cname 序列类名
    @param g_over 该类的 generate 覆盖表
    @param base 全局生成基线
    @return (合并后的生成配置, 按类档位表, 按类规则表, 按类窗口表)
    """
    t = _Tbl(col, file, f"[class.{cname}.generate]", g_over)
    merged = replace(
        base,
        instruction=t.get_str("instruction", base.instruction, nonempty=True),
        styles=(_parse_styles(col, file, t.take("styles"), section=f"class.{cname}.generate")
                if "styles" in g_over else base.styles),
        num_per_record=t.get_int("num_per_record", base.num_per_record, minimum=1),
        temperature=t.get_float("temperature", base.temperature, bound=_GE0),
        len_range=_int_pair(t, "len_range", base.len_range),
    )
    tiers = frame_rules = frame_windows = sequence_rules = None
    if "tiers" in g_over:
        raw = t.take("tiers")
        parsed = _parse_tiers(col, file, raw, f"[[class.{cname}.generate.tiers]]")
        # 形状错误(键值非数组)已在解析层报出且修复动作明确(改写成数组表), 按未声明
        # 落库——不再叠报 rule 61 的空表/锚错(同一个键一条错误一个修复动作, 互斥推论)。
        tiers = parsed if isinstance(raw, list) else None
    if "frame_rules" in g_over:
        frame_rules = _parse_frame_rules(
            col, file, t.take("frame_rules"),
            f"[[class.{cname}.generate.frame_rules]]")
    if "frame_windows" in g_over:
        frame_windows = _parse_frame_windows(
            col, file, t.take("frame_windows"),
            f"[[class.{cname}.generate.frame_windows]]")
    if "sequence_rules" in g_over:
        sequence_rules = _parse_sequence_rules(
            col, file, t.take("sequence_rules"),
            f"[[class.{cname}.generate.sequence_rules]]")
    return merged, tiers, frame_rules, frame_windows, sequence_rules


def _merge_class_verify(col: _Collector, file: str, cname: str, v_over: dict,
                        base: VerifyConfig) -> VerifyConfig:
    """``[class.<name>.verify]`` 的合并(白名单仅 extra_criteria)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param cname 序列类名
    @param v_over 该类的 verify 覆盖表
    @param base 全局复核基线
    @return 合并后的 ``VerifyConfig``
    """
    t = _Tbl(col, file, f"[class.{cname}.verify]", v_over)
    return replace(base, extra_criteria=t.get_str("extra_criteria", base.extra_criteria))


def _merge_class_extract(col: _Collector, file: str, cname: str, e_over: dict,
                         base: ExtractConfig) -> ExtractConfig:
    """``[class.<name>.extract]`` 的合并(v1.8, S2: 白名单只有 instruction)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param cname 序列类名
    @param e_over 该类的 extract 覆盖表
    @param base 全局摘取基线
    @return 合并后的 ``ExtractConfig``
    """
    t = _Tbl(col, file, f"[class.{cname}.extract]", e_over)
    return replace(base, instruction=t.get_str("instruction", base.instruction))


# ── 序列类视图物化 ──────────────────────────────────────────────────────────


def _inherit_class(bases: _ClassBases) -> _MergedClass:
    """零覆盖的类: 全盘继承全局基线(v1.15: 档位表得 None ⇒ 回落全局表)。

    @param bases 各节全局基线
    @return ``_MergedClass``
    """
    return _MergedClass(quality=bases.quality, annotate=bases.annotate,
                        generate=bases.generate, verify=bases.verify,
                        extract=bases.extract, rubric_raw=None,
                        examples_provided=False, schema_path=None,
                        schema_inline=None, tiers=None, frame_rules=None,
                        frame_windows=None, sequence_rules=None)


def _class_sections(col: _Collector, file: str, cname: str, class_raw: Any) -> dict | None:
    """取一个类的原始覆盖节(非表即定位报错并按"无覆盖"处理)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param cname 序列类名
    @param class_raw ``[class]`` 原始节
    @return 该类的覆盖节字典或 None
    """
    sections = class_raw.get(cname) if isinstance(class_raw, dict) else None
    if sections is not None and not isinstance(sections, dict):
        col.error(f"{file}:[class.{cname}]: expected table, got {_fmt(sections)}")
        return None
    return sections


def _resolve_class_rubric(col: _Collector, cname: str, merged: _MergedClass,
                          inputs: _ViewInputs) -> tuple[Rubric, bool, str | None, str]:
    """R7: 用合并后的选择器复解析该类的生效准则(按键溯源到内联表)。

    @param col 错误聚合器
    @param cname 序列类名
    @param merged 该类的覆盖合并结果
    @param inputs 全局取值捆包
    @return (生效准则, 是否内联, pointwise 去重键 | None, 报错定位 scope)
    """
    global_key = "[[rubric.criteria]]" if inputs.rubric_is_inline else inputs.selector
    if merged.quality.rubric != "inline":
        return _resolve_class_named_rubric(col, cname, merged, inputs)
    if merged.rubric_raw is not None:
        site = _RubricSite(file=inputs.file, selector="inline", modality=inputs.modality,
                           scope=f"class.{cname}")
        rubric_c, inline_c = _resolve_rubric(col, site, merged.rubric_raw)
        return rubric_c, inline_c, f"[[class.{cname}.rubric.criteria]]", f"class.{cname}"
    if inputs.selector == "inline":
        # 继承全局内联产物(含其回落路径)
        return inputs.rubric, inputs.rubric_is_inline, global_key, ""
    # 类切到 inline 却没给表——与全局同规则: inline 必须有伙伴表
    col.error(f'{inputs.file}:[class.{cname}.quality].rubric: rubric = "inline" but '
              f"[[class.{cname}.rubric.criteria]] is not provided")
    return _fallback_default_rubric(inputs.modality), False, None, ""


def _resolve_class_named_rubric(col: _Collector, cname: str, merged: _MergedClass,
                                inputs: _ViewInputs) -> tuple[Rubric, bool, str | None, str]:
    """类选择器指向打包默认准则时的解析分支(内联表在场则告警忽略)。

    @param col 错误聚合器
    @param cname 序列类名
    @param merged 该类的覆盖合并结果
    @param inputs 全局取值捆包
    @return (生效准则, 恒 False, pointwise 去重键, 恒 "")
    """
    if merged.rubric_raw is not None:
        col.warn(f"{inputs.file}:[[class.{cname}.rubric.criteria]]: quality.rubric = "
                 f"{_fmt(merged.quality.rubric)}, the inline rubric has no effect and "
                 f"is ignored")
    if merged.quality.rubric == inputs.selector and not inputs.rubric_is_inline:
        rubric_c = inputs.rubric        # 与全局同一份打包默认准则
    else:
        try:
            rubric_c = default_rubric(merged.quality.rubric)  # type: ignore[arg-type]
        except Exception as e:  # pragma: no cover — 打包文件出厂即合法
            col.error(f"{merged.quality.rubric}: failed to load default rubric: {e}")
            rubric_c = Rubric(name=merged.quality.rubric, criteria=())
    return rubric_c, False, merged.quality.rubric, ""


def _dryrun_class_examples(col: _Collector, cname: str, merged: _MergedClass,
                           schema_c: dict | None, inputs: _ViewInputs) -> None:
    """few-shot 干跑: 过**类有效 Schema** + 全局 hook。

    v1.13 修正——此前恒过全局 Schema, 类自带 Schema 时会误判。类自带 Schema 时, 继承
    来的全局示例也要按类 Schema 复跑一遍(运行期就是按类 Schema 发出去的)。

    @param col 错误聚合器
    @param cname 序列类名
    @param merged 该类的覆盖合并结果
    @param schema_c 该类的标注 Schema; None = 回落全局
    @param inputs 全局取值捆包(``dryrun`` 的两个存活标志就地更新)
    """
    dr = inputs.dryrun
    if not (merged.annotate.examples and (merged.examples_provided or schema_c is not None)):
        return
    own = schema_c is not None
    v_arg = (Draft202012Validator(schema_c) if own
             else (dr.validator if dr.schema_alive else None))
    key_c = (("schema_inline" if merged.schema_inline is not None else "schema_path")
             if own else dr.schema_key)
    s_ok, h_ok = _dryrun_fewshot(col, merged.annotate.examples, _DryRun(
        file=inputs.file, elem_label=f"class.{cname}.annotate.examples",
        validator=v_arg, schema_key=key_c,
        schema_section=f"class.{cname}.annotate" if own else "output",
        schema_noun="per-class annotation schema" if own else "user schema",
        hook=dr.hook if (dr.hook_alive and dr.schema_ok) else None,
        hook_ref=dr.hook_ref))
    if not own:
        # 全局 Schema 的 $ref 解析死了才停后续类的干跑; 类自带 Schema 的失败只属于
        # 该类, 不牵连全局层。
        dr.schema_alive = dr.schema_alive and s_ok
    dr.hook_alive = dr.hook_alive and h_ok


def _build_class_views(col: _Collector, classify: ClassifyConfig, class_raw: Any,
                       inputs: _ViewInputs) -> dict[str, ClassView]:
    """为**每个已声明的序列类**物化一份合并视图(零覆盖的类也有), 使下游永不回退。

    @param col 错误聚合器
    @param classify 已解析的 ``[classify]`` 节
    @param class_raw ``[class]`` 原始节
    @param inputs 全局取值捆包
    @return 类名 → ``ClassView``
    """
    views: dict[str, ClassView] = {}
    global_key = "[[rubric.criteria]]" if inputs.rubric_is_inline else inputs.selector
    pointwise_checked: set[str] = (
        {global_key} if inputs.bases.quality.mode == "pointwise" else set())
    for cspec in classify.classes:
        cname = cspec.name
        sections = _class_sections(col, inputs.file, cname, class_raw)
        merged = (_merge_class_sections(col, inputs.file, cname, sections, inputs.bases)
                  if sections else _inherit_class(inputs.bases))
        rubric_c, inline_c, rkey, rscope = _resolve_class_rubric(col, cname, merged, inputs)
        # (类模式 × 类准则)组合上的 pointwise 六级检查; 已检查过的准则跳过(去重)。
        if merged.quality.mode == "pointwise" and rkey is not None \
                and rkey not in pointwise_checked:
            pointwise_checked.add(rkey)
            _check_pointwise_rubric(col, _RubricSite(file=inputs.file, selector=rkey,
                                                     modality=inputs.modality, scope=rscope),
                                    rubric_c, inline_c)
        # v1.13(裁决·按类标注 Schema): 装载该类的标注 Schema(覆盖语义, 未声明 =
        # None = 回落全局 output.schema)。
        schema_c = _load_class_schema(col, inputs.file, cname, merged.schema_path,
                                      merged.schema_inline)
        _dryrun_class_examples(col, cname, merged, schema_c, inputs)
        views[cname] = ClassView(name=cname, quality=merged.quality, rubric=rubric_c,
                                 annotate=merged.annotate, generate=merged.generate,
                                 verify=merged.verify, extract=merged.extract,
                                 schema=schema_c, tiers=merged.tiers,
                                 frame_rules=merged.frame_rules,
                                 frame_windows=merged.frame_windows,
                                 sequence_rules=merged.sequence_rules)
    return views


# ── 帧类覆盖合并与视图物化 ──────────────────────────────────────────────────


def _check_frame_class_whitelist(col: _Collector, file: str, cname: str,
                                 sections: dict) -> None:
    """R25 家族的帧类白名单校验: 节名与键名都必须在白名单内。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param cname 帧类名
    @param sections 该帧类的原始覆盖节字典
    """
    for sect, sub in sections.items():
        if sect not in _FRAME_CLASS_SECTIONS:
            col.error(f"{file}:[frame.class.{cname}.{sect}]: section is not in the "
                      f"[frame.class.*] override whitelist "
                      f"(available: {_avail(_FRAME_CLASS_SECTIONS)})")
            continue
        if not isinstance(sub, dict):
            col.error(f"{file}:[frame.class.{cname}.{sect}]: expected table, got {_fmt(sub)}")
            continue
        allowed = _FRAME_CLASS_SECTION_KEYS[sect]
        for k in sub:
            if k not in allowed:
                col.error(f"{file}:[frame.class.{cname}.{sect}].{k}: "
                          f"[frame.class.*.{sect}] cannot override this key "
                          f"(whitelist: {_avail(allowed)})")


def _merge_frame_class(col: _Collector, file: str, cname: str, sections: dict,
                       base: FrameAnnotateConfig) -> tuple[FrameClassView, bool]:
    """v1.12: 把一个帧类的覆盖节合并到全局 ``[frame.annotate]``。

    (SPEC-frame-annotation §3.1「帧类覆盖」行; R25 家族。)按键溯源: 类提供的键覆盖
    全局值, 其余继承; ``enabled`` 缺省 true(= 该类照常标注; false = 跳过该类成员的帧
    标注, 省成本面)。v1.13: 同时物化该帧类的生成面(``[frame.class.<name>.generate]``
    四键白名单)——时间流生成形态的帧内容契约。v1.14: 生成面第四键 ``time_fields``
    (时间语义字段绑定表)在此并入视图。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param cname 帧类名
    @param sections 该帧类的原始覆盖节字典
    @param base 全局 ``[frame.annotate]`` 基线
    @return (视图, 该类是否自带示例)——后者供调用方决定是否做帧级 Schema 干跑
    """
    _check_frame_class_whitelist(col, file, cname, sections)
    a_over = sections.get("annotate")
    a_over = a_over if isinstance(a_over, dict) else {}
    t = _Tbl(col, file, f"[frame.class.{cname}.annotate]", a_over)
    examples_provided = "examples" in a_over
    gen_instruction, gen_schema = _load_frame_gen(col, file, cname, sections)
    duration_us, resources = _parse_duration_resources(
        col, file, cname, sections.get("generate"))
    view = FrameClassView(
        instruction=t.get_str("instruction", base.instruction, nonempty=True),
        examples=(_parse_examples(col, file, t.take("examples"),
                                  section=f"frame.class.{cname}.annotate")
                  if examples_provided else base.examples),
        enabled=t.get_bool("enabled", True),
        gen_instruction=gen_instruction,
        gen_schema=gen_schema,
        time_fields=_parse_time_fields(col, file, cname, sections.get("generate")),
        duration_us=duration_us,
        resources=resources,
    )
    return view, examples_provided


def _build_frame_class_views(col: _Collector,
                             ctx: _FrameViewCtx) -> dict[str, FrameClassView]:
    """为每个已声明的帧类物化一份视图(零覆盖的类也有, class_views 同款)。

    @param col 错误聚合器
    @param ctx 帧类视图上下文(``schema_alive`` 就地更新)
    @return 帧类名 → ``FrameClassView``
    """
    names = tuple(c.name for c in ctx.classify.classes)
    if isinstance(ctx.class_raw, dict):
        for cname in ctx.class_raw:
            if cname not in names:
                col.error(f"{ctx.file}:[frame.class.{cname}]: class name {_fmt(cname)} is "
                          f"not in [[frame.classify.classes]], available: {_avail(names)}")
    views: dict[str, FrameClassView] = {}
    for cspec in ctx.classify.classes:
        sections = (ctx.class_raw.get(cspec.name)
                    if isinstance(ctx.class_raw, dict) else None)
        if sections is not None and not isinstance(sections, dict):
            col.error(f"{ctx.file}:[frame.class.{cspec.name}]: expected table, "
                      f"got {_fmt(sections)}")
            sections = None
        if sections:
            view, examples_provided = _merge_frame_class(col, ctx.file, cspec.name,
                                                         sections, ctx.annotate)
        else:
            view = FrameClassView(instruction=ctx.annotate.instruction,
                                  examples=ctx.annotate.examples, enabled=True)
            examples_provided = False
        if examples_provided and view.examples:
            _dryrun_frame_examples(col, cspec.name, view, ctx)
        views[cspec.name] = view
    return views


def _dryrun_frame_examples(col: _Collector, cname: str, view: FrameClassView,
                           ctx: _FrameViewCtx) -> None:
    """把类提供的示例对帧级 Schema 干跑(规则 28 的帧级镜像; 帧级无 L2.5 hook)。

    @param col 错误聚合器
    @param cname 帧类名
    @param view 该帧类的合并视图
    @param ctx 帧类视图上下文(``schema_alive`` 就地更新)
    """
    if ctx.validator is None or not ctx.schema_alive:
        return
    alive, _ = _dryrun_fewshot(col, view.examples, _DryRun(
        file=ctx.file, elem_label=f"frame.class.{cname}.annotate.examples",
        validator=ctx.validator, schema_key=ctx.schema_key,
        schema_section="frame.annotate", schema_noun="frame schema"))
    ctx.schema_alive = ctx.schema_alive and alive

"""M1 时间流生成约束。

本模块承载 v1.13--v1.15 的时间流形态、档位与时间字段约束，并承载 v1.16 的规则、窗口
语法与结构前提检查。完整可行性由运行期规划器提供同源的 M1 入口，配置层不依赖
算子。
"""
from __future__ import annotations

import json
from datetime import datetime
from dataclasses import replace
import inspect
import random
from types import SimpleNamespace
from typing import Any

from labelkit.common.config._collect import _Collector, _fmt
from labelkit.common.config._sections import (
    _SEQUENCE_TEMPLATES,
    _STREAM_FORBIDDEN_CLASS_GEN_KEYS,
    _STREAM_FORBIDDEN_GEN_KEYS,
)
from labelkit.common.config.model import (
    GenerateStreamConfig,
    SequenceRuleSpec,
    SequenceWindowSpec,
    TierSpec,
    apportion_tiers,
    effective_rules,
    effective_tiers,
    effective_windows,
)
from labelkit.common.contracts.types import (
    SequenceValidationFrame,
    SequenceValidationInput,
)
from labelkit.common.extensions.hooks import normalize_violations, resolve_hook
from labelkit.common.runtime.temporal import quantize_frame_gap


_TIME_FIELD_TERMS: dict[str, str] = {
    "ts": "string",
    "gap_prev_s": "number",
    "gap_next_s": "number",
    "elapsed_s": "number",
}
_FRAME_GAP_FLOOR_S = 1e-6
_UNARY_TEMPLATES = {"existence", "absence", "exactly", "init", "end"}
_COUNT_TEMPLATES = {"existence", "absence", "exactly"}
_BINARY_TEMPLATES = set(_SEQUENCE_TEMPLATES) - _UNARY_TEMPLATES


def check_generate_stream_form(ctx: Any, products: Any) -> tuple[int, int]:
    """校验时间流形态并返回序列总配额与最长长度。

    @param ctx 当前配置收集上下文。
    @param products 已解析的类视图与帧类视图产品。
    @return 序列总配额与所有类的最长序列长度。
    """
    views = products.class_views
    seq_total = sum(view.generate.sequences for view in views.values())
    len_max = max([1] + [view.generate.len_range[1] for view in views.values()])
    if not ctx.p.generate_stream.enabled:
        check_parked_stream_keys(ctx)
        return seq_total, len_max
    check_stream_constraints(ctx.col, ctx.fp, ctx.p.generate_stream, SimpleNamespace(
        mode=ctx.mode, modality=ctx.modality, generate=ctx.p.generate,
        classify=ctx.p.classify, class_views=views, stream=ctx.p.stream,
        meta_mode=ctx.p.output.meta_mode, frame_classify=ctx.p.frame_classify,
        frame_annotate=ctx.p.frame_annotate,
        frame_class_views=products.frame_class_views,
        gen_provided=ctx.p.gen_provided,
        class_raw=ctx.p.class_raw if isinstance(ctx.p.class_raw, dict) else {},
        seq_total=seq_total, len_max=len_max, text_field=ctx.p.input.text_field,
        generate_stream=ctx.p.generate_stream, run_seed=ctx.p.run.get("seed", 0),
        limit=ctx.cli.limit,
        tiers=ctx.p.generate_stream.tiers,
        tier_domain={name for view in views.values() if view.generate.sequences >= 1
                     for spec in effective_tiers(view.tiers, ctx.p.generate_stream.tiers)
                     for name in spec.frame_classes},
        frame_gen_schema_declared=_frame_gen_schema_declared(ctx.p.frame_class_raw),
    ))
    return seq_total, len_max


def check_stream_constraints(col: _Collector, fp: str, gs: GenerateStreamConfig,
                             values: SimpleNamespace) -> None:
    """校验时间流形态的跨节组合约束。

    @param col 配置错误与警告收集器。
    @param fp 当前配置文件路径前缀。
    @param gs 时间流生成配置。
    @param values 跨节约束所需的解析配置视图。
    """
    _stream_form_premise(col, fp, values)
    _stream_form_probes(col, fp, values)
    _stream_form_quota(col, fp, values)
    _stream_form_packing(col, fp, gs, values)
    _stream_form_weaving(col, fp, gs, values)
    _check_tier_table(col, fp, values)
    _check_time_fields(col, fp, values)
    _check_rule_window_tables(col, fp, gs, values)


def _stream_form_premise(col: _Collector, fp: str, values: SimpleNamespace) -> None:
    """校验时间流形态的开关合取和工件字段。"""
    loc = f"{fp}:[generate.stream].enabled"
    if values.mode != "generate_only":
        col.error(f'{loc}: the time-stream form requires run.mode = "generate_only", got '
                  f"{_fmt(values.mode)} - this form synthesizes a time stream from scratch "
                  f"and consumes no input data")
    if values.modality != "text":
        col.error(f'{loc}: the time-stream form requires run.modality = "text", got '
                  f"{_fmt(values.modality)} (UI-modality time-stream generation is a v1.13 "
                  f"non-goal)")
    if not values.generate.enabled:
        col.error(f"{loc}: the time-stream form requires generate.enabled = true")
    if not values.classify.enabled:
        col.error(f"{loc}: the time-stream form requires classify.enabled = true - the "
                  f"sequence class table carries the quota and per-class conditioning, and "
                  f"generation-side labels are inherited (zero verdict calls)")
    _stream_form_artifact_keys(col, fp, values)
    if values.meta_mode == "none":
        col.error(f'{fp}:[output].meta_mode: must not be "none" in the time-stream form - '
                  f"frame-class ground truth and member reconciliation are carried only by "
                  f"_meta.stream (sidecar is legal)")


def _stream_form_artifact_keys(col: _Collector, fp: str, values: SimpleNamespace) -> None:
    """校验时间流工件行的三个顶层键互斥且可往返。"""
    order_by = values.stream.order_by
    if not (order_by.startswith("meta:") and order_by[len("meta:"):]):
        col.error(f'{fp}:[stream].order_by: the time-stream form requires "meta:<field>" '
                  f"(that field is the timestamp key of the artifact row, replayable on the "
                  f"ingest side under the same declaration), got {_fmt(order_by)}")
    elif "." in order_by[len("meta:"):]:
        col.error(f'{fp}:[stream].order_by: the timestamp field name of the time-stream '
                  f"form must not contain \".\" (the artifact row writes it as a literal "
                  f"top-level key and a dotted path cannot round-trip on replay ingest), got "
                  f"{_fmt(order_by)}")
    if "." in values.text_field:
        col.error(f'{fp}:[input].text_field: the text field name of the time-stream form '
                  f"must not contain \".\" (the artifact row writes it as a literal "
                  f"top-level key and a dotted path cannot round-trip on replay ingest), got "
                  f"{_fmt(values.text_field)}")
    ts_field = order_by[len("meta:"):] if order_by.startswith("meta:") else ""
    if ts_field and ts_field == values.text_field:
        col.error(f"{fp}:[input].text_field: must not have the same name as the timestamp "
                  f"field of [stream].order_by in the time-stream form (the two artifact-row "
                  f"keys would collide), got {_fmt(values.text_field)}")
    for owner, field_name in (("[input].text_field", values.text_field),
                              ("[stream].order_by", ts_field)):
        if field_name == "truth":
            col.error(f'{fp}:{owner}: the field name must not be "truth" in the time-stream '
                      f"form (it would collide with the ground-truth key of the artifact row)")


def _stream_form_probes(col: _Collector, fp: str, values: SimpleNamespace) -> None:
    """校验时间流形态下的定向禁设键。"""
    for key in _STREAM_FORBIDDEN_GEN_KEYS:
        if values.gen_provided.get(key):
            col.error(f"{fp}:[generate].{key}: the time-stream form does not provide this "
                      f"key - sequence quotas are carried by [class.<name>.generate].sequences, "
                      f"sequence length by len_range and noise batching by num_per_call; "
                      f"remove this key")
    for cname, sections in values.class_raw.items():
        g_over = sections.get("generate") if isinstance(sections, dict) else None
        if not isinstance(g_over, dict):
            continue
        for key in _STREAM_FORBIDDEN_CLASS_GEN_KEYS:
            if key in g_over:
                col.error(f"{fp}:[class.{cname}.generate].{key}: the time-stream form does "
                          f"not provide this key (per-record expansion / seeds per call "
                          f"belong to the flat generation forms); use sequences / len_range "
                          f"instead")
    for name, on in (("frame.classify", values.frame_classify.enabled),
                     ("frame.annotate", values.frame_annotate.enabled)):
        if on:
            col.error(f"{fp}:[{name}].enabled: mutually exclusive with the time-stream form "
                      f"- frame-class ground truth is known at generation time (the blueprint "
                      f"is the truth), so no frame-level verdict is needed; write frame "
                      f"content contracts in [frame.class.<name>.generate]")


def _stream_form_quota(col: _Collector, fp: str, values: SimpleNamespace) -> None:
    """校验序列类配额和帧类生成面。"""
    if values.seq_total < 1:
        col.error(f"{fp}:[class.<name>.generate].sequences: the time-stream form requires "
                  f"at least one sequence class with an effective sequences >= 1 (the "
                  f"global [generate].sequences sets a default that classes may override), "
                  f"got a total of {values.seq_total} across all classes")
    for name, view in values.class_views.items():
        if view.generate.sequences >= 1 and not view.generate.instruction.strip():
            col.error(f"{fp}:[class.{name}.generate].instruction: a participating sequence "
                      f"class (effective sequences = {view.generate.sequences}) must provide "
                      f"a non-empty generation instruction (the global [generate].instruction "
                      f"sets a default)")
    if not values.frame_classify.classes:
        col.error(f"{fp}:[[frame.classify.classes]]: the time-stream form requires a non-empty "
                  f"frame class table (the blueprint picks each step from that closed set; "
                  f"frame.classify.enabled stays false)")
    _check_frame_gen_instructions(col, fp, values)


def _check_frame_gen_instructions(col: _Collector, fp: str, values: SimpleNamespace) -> None:
    """校验所有有效档位构成中的帧类生成指令。"""
    if values.tiers:
        domain = values.tier_domain
        reason = ("the blueprint enum covers the union of the tier compositions, so any "
                  "frame class of a tier may be picked")
    else:
        domain = {spec.name for spec in values.frame_classify.classes}
        reason = "the blueprint enum covers the whole table, so any frame class may be picked"
    for spec in values.frame_classify.classes:
        if spec.name not in domain:
            continue
        view = values.frame_class_views.get(spec.name)
        if view is None or not (view.gen_instruction or "").strip():
            col.error(f"{fp}:[frame.class.{spec.name}.generate].instruction: every frame "
                      f"class must provide a non-empty generation instruction ({reason}), "
                      f"expected a non-empty string")


def _stream_form_packing(col: _Collector, fp: str, gs: GenerateStreamConfig,
                         values: SimpleNamespace) -> None:
    """校验会话数、噪音、重复和帧间隔。"""
    total = values.seq_total
    if gs.sessions < 1:
        col.error(f"{fp}:[generate.stream].sessions: expected an integer >= 1 (number of "
                  f"sessions), got {gs.sessions}")
    elif not gs.sessions <= total <= 2 * gs.sessions:
        col.error(f"{fp}:[generate.stream].sessions: expected sessions <= Σsequences <= "
                  f"2 * sessions (crossed sessions = Σsequences - sessions, crossing "
                  f"concurrency is always k in 1,2), got sessions = {gs.sessions}, "
                  f"Σsequences = {total}")
    if gs.duplicates > total:
        col.error(f"{fp}:[generate.stream].duplicates: expected an integer in [0, Σsequences] "
                  f"(re-sent sequences are drawn from the surviving ones), got {gs.duplicates}, "
                  f"Σsequences = {total}")
    if not 0 <= gs.noise_ratio < 1:
        col.error(f"{fp}:[generate.stream].noise_ratio: expected a number in [0,1) (noise "
                  f"frames / task frames ratio), got {_fmt(gs.noise_ratio)}")
    elif gs.noise_ratio > 0 and not gs.noise_instruction.strip():
        col.error(f"{fp}:[generate.stream].noise_instruction: required when noise_ratio > 0, "
                  f"expected a non-empty string (the noise-frame generation instruction)")
    if gs.frame_gap_s[0] < _FRAME_GAP_FLOOR_S:
        col.error(f"{fp}:[generate.stream].frame_gap_s: the lower bound must be >= "
                  f"{_FRAME_GAP_FLOOR_S:g} s (one microsecond) - the laid-out timestamps "
                  f"must be strictly increasing and a sub-microsecond gap rounds to a zero "
                  f"timedelta, and the time vocabulary uses 0.0 as its first/last frame "
                  f"boundary sentinel, got lower bound {_fmt(gs.frame_gap_s[0])}")
    _check_frame_gap_quantization(col, fp, gs)
    constrained_prefix = _has_nonzero_constraints(gs, values)
    at_or_above_gap = gs.frame_gap_s[1] > values.stream.gap_s
    if not constrained_prefix:
        at_or_above_gap = gs.frame_gap_s[1] >= values.stream.gap_s
    if at_or_above_gap:
        relation = "<=" if constrained_prefix else "<"
        explanation = ("the constrained v1.16 planner permits equality at the replay boundary"
                       if constrained_prefix else
                       "the default v1.15 path requires a strict boundary")
        col.error(f"{fp}:[generate.stream].frame_gap_s: the upper bound must be {relation} "
                  f"stream.gap_s (= {values.stream.gap_s}; {explanation}), got "
                  f"{_fmt(gs.frame_gap_s[1])}")


def _check_frame_gap_quantization(col: _Collector, fp: str,
                                  gs: GenerateStreamConfig) -> None:
    """把 frame_gap_s 的整数微秒可表示性纳入 M1 聚合。"""
    try:
        quantize_frame_gap(gs.frame_gap_s)
    except ValueError as exc:
        col.error(f"{fp}:[generate.stream].frame_gap_s: {exc} - widen the range so that "
                  "at least one integer microsecond is representable")


def _stream_form_weaving(col: _Collector, fp: str, gs: GenerateStreamConfig,
                         values: SimpleNamespace) -> None:
    """校验会话装载上限、分区键、跨度和起始时间。"""
    if 2 * values.len_max > values.stream.session_max_len:
        col.error(f"{fp}:[stream].session_max_len: the time-stream form requires >= 2 * "
                  f"max(len_range upper bound) (a crossed session always packs two "
                  f"sequences), got {values.stream.session_max_len} < {2 * values.len_max}")
    if values.stream.key:
        col.error(f"{fp}:[stream].key: the time-stream form requires an empty array - sessions "
                  f"are laid out directly by the weaver and partition keys do not participate, "
                  f"got {_fmt(list(values.stream.key))}")
    if values.stream.gap_steps:
        col.error(f"{fp}:[stream].gap_steps: the time-stream form requires 0 - session "
                  f"boundaries are laid out directly by the weaver (inter-session gaps are "
                  f"always > gap_s) and step-gap splitting does not participate, got "
                  f"{values.stream.gap_steps}")
    span = values.stream.session_max_span_s
    worst = (values.stream.session_max_len - 1) * gs.frame_gap_s[1]
    if span > 0 and worst > span:
        col.error(f"{fp}:[stream].session_max_span_s: worst-case session span "
                  f"(session_max_len - 1) * frame_gap_s upper bound = {worst:g} s > {span} s - "
                  f"the laid-out sessions would be hard-cut by span on the ingest side; raise "
                  f"session_max_span_s, lower the frame_gap_s upper bound or lower "
                  f"session_max_len")
    try:
        datetime.fromisoformat(gs.ts_start)
    except (TypeError, ValueError):
        col.error(f'{fp}:[generate.stream].ts_start: expected a parseable ISO-8601 instant '
                  f'(e.g. "2026-01-01T09:00:00+08:00"; a missing timezone is treated as UTC, '
                  f"matching the meta:<field> ingest rule), got {_fmt(gs.ts_start)}")


def _check_tier_table(col: _Collector, fp: str, values: SimpleNamespace) -> None:
    """校验全局与按类档位表及其配额对。"""
    tiers = values.tiers
    frame_names = tuple(spec.name for spec in values.frame_classify.classes)
    _check_tier_source(col, f"{fp}:[[generate.stream.tiers]]", tiers, frame_names)
    for cname, view in values.class_views.items():
        if view.tiers is None:
            continue
        loc = f"{fp}:[class.{cname}.generate].tiers"
        if not view.tiers:
            col.error(f"{loc}: expected a non-empty array of tier tables - omit the key to "
                      f"fall back to the global [[generate.stream.tiers]] table")
        elif not tiers:
            col.error(f"{loc}: a per-class tier table overrides the global "
                      f"[[generate.stream.tiers]] table, which is absent - declare the global "
                      f"table (it is the fallback for classes without their own table and the "
                      f"switch of the whole tier face)")
        _check_tier_source(col, loc, view.tiers, frame_names)
    if tiers:
        _check_tier_quota_pairs(col, fp, values)
        _warn_frame_classes_without_tier(col, fp, values.tier_domain, frame_names)


def _check_tier_source(col: _Collector, loc: str, tiers: tuple[TierSpec, ...],
                       frame_names: tuple[str, ...]) -> None:
    """校验一张档位来源表的身份和构成。"""
    ranks = sorted(spec.tier_rank for spec in tiers)
    if ranks != list(range(1, len(ranks) + 1)):
        col.error(f"{loc}.tier_rank: tier ranks must be unique and cover 1..N contiguously "
                  f"(N = {len(ranks)} = the number of tiers; the rank is the identity of a "
                  f"tier, there is no name key), got {_fmt(ranks)}")
    owners: dict[tuple[str, ...], int] = {}
    for spec in tiers:
        at = f"{loc}(tier_rank = {spec.tier_rank}).frame_classes"
        if not spec.frame_classes:
            col.error(f"{at}: expected a non-empty array of frame class names (a tier IS its "
                      f"frame-class composition)")
            continue
        for index, name in enumerate(spec.frame_classes):
            if name in spec.frame_classes[:index]:
                col.error(f"{at}: frame class names must be distinct within a tier (the "
                          f"composition is a set), got duplicate {_fmt(name)}")
            elif name not in frame_names:
                col.error(f"{at}: frame class name {_fmt(name)} is not in "
                          f"[[frame.classify.classes]], available: "
                          f"{', '.join(frame_names) if frame_names else '(none)'}")
        key = tuple(sorted(set(spec.frame_classes)))
        if key in owners:
            col.error(f"{at}: the composition is identical to the one of tier_rank = "
                      f"{owners[key]} - two tiers with the same frame-class set are semantically "
                      f"duplicates, got {_fmt(list(spec.frame_classes))}")
        else:
            owners[key] = spec.tier_rank


def _check_tier_quota_pairs(col: _Collector, fp: str, values: SimpleNamespace) -> None:
    """校验非零档位配额的长度下界并报告零配额警告。"""
    for cname, view in values.class_views.items():
        if view.generate.sequences < 1:
            continue
        table = effective_tiers(view.tiers, values.tiers)
        weights = ", ".join(f"tier_rank {spec.tier_rank}: weight {spec.weight}"
                             for spec in table)
        for spec, quota in zip(table, apportion_tiers(view.generate.sequences, table)):
            if quota < 1:
                col.warn(f"{fp}:[[generate.stream.tiers]]: class {_fmt(cname)} apportions 0 "
                         f"sequences to tier_rank = {spec.tier_rank} (largest-remainder "
                         f"apportionment of {view.generate.sequences} sequences over weights "
                         f"{weights}), so that tier is never exercised for this class - raise "
                         f"sequences or rebalance the weights")
            elif view.generate.len_range[0] < len(spec.frame_classes):
                col.error(f"{fp}:[class.{cname}.generate].len_range: the lower bound must be "
                          f">= the composition size of every tier this class draws from "
                          f"(tier_rank = {spec.tier_rank} declares {len(spec.frame_classes)} "
                          f"frame classes and is apportioned {quota} of the "
                          f"{view.generate.sequences} sequences, and each of them must appear "
                          f"at least once), got lower bound {view.generate.len_range[0]}")


def _warn_frame_classes_without_tier(col: _Collector, fp: str, covered: set[str],
                                     frame_names: tuple[str, ...]) -> None:
    """报告不在任何有效档位构成中的帧类。"""
    for name in frame_names:
        if name not in covered:
            col.warn(f"{fp}:[frame.class.{name}.generate]: frame class {_fmt(name)} is in no "
                     f"tier composition, so it can never be picked by a blueprint - its "
                     f"whole generate face (instruction, schema, time_fields) is dead config "
                     f"(the generation instruction is not required for it either)")


def check_parked_stream_keys(ctx: Any) -> None:
    """形态关闭时拒绝时间流专属的显式配置。

    @param ctx 当前配置收集上下文。
    """
    if ctx.p.gen_provided.get("stream_tiers"):
        ctx.col.error(f"{ctx.fp}:[[generate.stream.tiers]]: the tier table is only legal in "
                      f"the time-stream generation form ([generate.stream].enabled = true) - "
                      f"a tier declares the frame-class composition of the sequences drawn "
                      f"from it, and only that form plans sequences from a frame class table")
    if ctx.p.gen_provided.get("stream_rules"):
        ctx.col.error(f"{ctx.fp}:[[generate.stream.rules]]: sequence rules are only legal in "
                      f"the time-stream generation form ([generate.stream].enabled = true)")
    if ctx.p.gen_provided.get("stream_windows"):
        ctx.col.error(f"{ctx.fp}:[[generate.stream.windows]]: sequence windows are only legal "
                      f"in the time-stream generation form ([generate.stream].enabled = true)")
    if ctx.p.generate.sequence_validator is not None:
        ctx.col.error(f"{ctx.fp}:[generate].sequence_validator: sequence_validator is only "
                      f"legal in the time-stream generation form ([generate.stream].enabled = true)")
    for cname, sections in (ctx.p.class_raw or {}).items():
        g_over = sections.get("generate") if isinstance(sections, dict) else None
        if not isinstance(g_over, dict):
            continue
        if "tiers" in g_over:
            ctx.col.error(f"{ctx.fp}:[class.{cname}.generate].tiers: the per-class tier table "
                          f"is only legal in the time-stream generation form "
                          f"([generate.stream].enabled = true) - it overrides the global "
                          f"[[generate.stream.tiers]] table for sequences of this class")
        for key in ("rules", "windows"):
            if key in g_over:
                ctx.col.error(f"{ctx.fp}:[class.{cname}.generate].{key}: sequence {key} are "
                              f"only legal in the time-stream generation form "
                              f"([generate.stream].enabled = true)")


def _check_time_fields(col: _Collector, fp: str, values: SimpleNamespace) -> None:
    """校验 v1.14 时间字段绑定面。"""
    for name, view in values.frame_class_views.items():
        if view.time_fields is None:
            continue
        loc = f"{fp}:[frame.class.{name}.generate.time_fields]"
        if view.gen_schema is None:
            if name not in values.frame_gen_schema_declared:
                col.error(f"{loc}: a time-field binding is only legal on a structured frame "
                          f"class - declare schema_path / schema_inline for "
                          f"[frame.class.{name}.generate] first (the backfill writes the "
                          f"computed value into the frame payload in place, so the payload "
                          f"must always be a JSON object; a plain-text frame has no field to "
                          f"bind)")
            continue
        props = view.gen_schema.get("properties")
        props = props if isinstance(props, dict) else {}
        _check_binding_pairs(col, loc, view.time_fields, props)
        bound = sum(1 for key in view.time_fields if key in props)
        if len(props) - bound < 1:
            col.error(f"{loc}: the bindings would remove every top-level property of the "
                      f"frame-class generation schema (top-level properties: {len(props)}, "
                      f"bound: {bound}) - a bound field is stripped from the per-position "
                      f"schema, so leave at least one property for the LLM to generate")


def _check_binding_pairs(col: _Collector, loc: str, bindings: dict[str, str],
                         props: dict[str, Any]) -> None:
    """校验绑定字段、语义词与 JSON Schema 字面类型。"""
    for key, term in bindings.items():
        want = _TIME_FIELD_TERMS.get(term)
        prop = props.get(key)
        declared = prop.get("type") if isinstance(prop, dict) else prop
        if want is None:
            col.error(f"{loc}.{key}: expected one of the time vocabulary terms "
                      f"{', '.join(_TIME_FIELD_TERMS)} (a frozen closed set), got {_fmt(term)}")
        elif key not in props:
            col.error(f"{loc}.{key}: {_fmt(key)} is not a top-level property of the "
                      f"frame-class generation schema, available: "
                      f"{', '.join(props) if props else '(none)'}")
        elif declared != want:
            col.error(f'{loc}.{key}: the bound property must declare "type": '
                      f'{json.dumps(want)} literally for the term {_fmt(term)} (a union type '
                      f"array, a missing type and an indirect declaration through $ref or a "
                      f"combining keyword all count as a mismatch), got {_fmt(declared)}")
        else:
            _warn_binding_extra_keywords(col, loc, key, prop)


def _warn_binding_extra_keywords(col: _Collector, loc: str, key: str,
                                 prop: dict[str, Any]) -> None:
    """报告绑定字段上不会被执行的额外 Schema 关键字。"""
    for keyword in prop:
        if keyword != "type":
            col.warn(f"{loc}.{key}: the bound field carries the keyword {_fmt(keyword)}, "
                     f"which is neither sent to the LLM nor enforced - a bound field is "
                     f"removed from the per-position schema and its value is computed from "
                     f"the laid-out timeline (only the declared type is guaranteed)")


def _frame_gen_schema_declared(raw: object) -> frozenset[str]:
    """返回声明过生成 Schema 源键的帧类名。"""
    if not isinstance(raw, dict):
        return frozenset()
    return frozenset(cname for cname, sections in raw.items()
                     if isinstance(sections, dict)
                     and isinstance(sections.get("generate"), dict)
                     and any(key in sections["generate"]
                             for key in ("schema_path", "schema_inline")))


def _check_rule_window_tables(col: _Collector, fp: str, gs: GenerateStreamConfig,
                              values: SimpleNamespace) -> None:
    """校验 v1.16 生效 rules/windows 的语法与 Schema 前提。"""
    frame_names = {spec.name for spec in values.frame_classify.classes}
    _check_rules(col, f"{fp}:[[generate.stream.rules]]", gs.rules, frame_names, values)
    _check_windows(col, f"{fp}:[[generate.stream.windows]]", gs.windows, frame_names)
    for cname, view in values.class_views.items():
        if view.rules is not None:
            _check_rules(col, f"{fp}:[[class.{cname}.generate.rules]]", view.rules,
                         frame_names, values)
        if view.windows is not None:
            _check_windows(col, f"{fp}:[[class.{cname}.generate.windows]]", view.windows,
                           frame_names)
    _check_sequence_validator(col, fp, values.generate.sequence_validator, values)
    if not len(col.errors):
        _check_local_potential(col, fp, gs, values)
        _check_full_potential(col, fp, gs, values)


def _check_sequence_validator(col: _Collector, fp: str, ref: str | None,
                              values: SimpleNamespace) -> None:
    """解析并干跑序列钩子，冻结其单位置参数与返回值契约。"""
    if ref is None:
        return
    try:
        hook = resolve_hook(ref)
    except ValueError as exc:
        col.error(f"{fp}:[generate].sequence_validator: {exc}")
        return
    try:
        params = tuple(inspect.signature(hook).parameters.values())
    except (TypeError, ValueError) as exc:
        col.error(f"{fp}:[generate].sequence_validator: cannot inspect hook signature "
                  f"({type(exc).__name__})")
        return
    if len(params) != 1 or params[0].kind not in (
            inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
        col.error(f"{fp}:[generate].sequence_validator: hook must accept exactly one "
                  "positional SequenceValidationInput parameter")
        return
    probe = _sequence_validator_probe(values)
    try:
        result = hook(probe)
    except Exception as exc:
        col.error(f"{fp}:[generate].sequence_validator: dry-run raised "
                  f"{type(exc).__name__}")
        return
    try:
        normalize_violations(result, ref)
    except (TypeError, ValueError) as exc:
        col.error(f"{fp}:[generate].sequence_validator: dry-run returned an invalid value: "
                  f"{exc}")


def _sequence_validator_probe(values: SimpleNamespace) -> SequenceValidationInput:
    """构造不含用户数据的代表性序列钩子输入。"""
    participating = sorted(
        (name, view) for name, view in values.class_views.items()
        if view.generate.sequences > 0)
    if not participating:
        return SequenceValidationInput(
            sequence_class="__m1_probe__", tier_rank=None,
            frames=(SequenceValidationFrame(0, "__m1_probe__", {}),))
    class_name, view = participating[0]
    table = effective_tiers(view.tiers, values.tiers)
    tier = table[0] if table else None
    frame_classes = tuple(tier.frame_classes) if tier else tuple(
        spec.name for spec in values.frame_classify.classes)
    if not frame_classes:
        frame_classes = ("__m1_probe__",)
    length = max(view.generate.len_range[0], 1)
    selected = tuple(frame_classes[index % len(frame_classes)] for index in range(length))
    return SequenceValidationInput(
        sequence_class=class_name,
        tier_rank=tier.tier_rank if tier else None,
        frames=tuple(SequenceValidationFrame(
            position=index, frame_class=frame_class,
            payload={"nested": {"value": "m1-probe"}})
            for index, frame_class in enumerate(selected)))


def _check_local_potential(col: _Collector, fp: str, gs: GenerateStreamConfig,
                           values: SimpleNamespace) -> None:
    """用联合 planner 检查每个类、档位和每个长度候选的局部可行性。"""
    from labelkit.common.runtime.sequence_planner import (
        AttemptSpec,
        PlannerConfigError,
        PlannerInternalError,
        PlannerStatus,
        check_question,
        question_from_config,
    )

    for cname in sorted(values.class_views):
        view = values.class_views[cname]
        rules = effective_rules(view.rules, gs.rules)
        windows = effective_windows(view.windows, gs.windows)
        if not rules and not windows:
            continue
        tiers = effective_tiers(view.tiers, gs.tiers) or (None,)
        for tier in tiers:
            for length in range(view.generate.len_range[0], view.generate.len_range[1] + 1):
                attempt = AttemptSpec(
                    index=0, class_name=cname, length=length,
                    allowed_classes=tuple(tier.frame_classes) if tier else (),
                    length_range=(length, length),
                    tier_rank=tier.tier_rank if tier else None,
                )
                try:
                    question = question_from_config(
                        _local_config(values, gs, rules, windows), attempts=(attempt,))
                    status = check_question(question)
                except PlannerInternalError:
                    raise
                except PlannerConfigError as exc:
                    _planner_config_error(
                        col, f"{fp}:[class.{cname}.generate].len_range",
                        tier, length, str(exc))
                    continue
                except ValueError as exc:
                    _planner_config_error(
                        col, f"{fp}:[class.{cname}.generate].len_range",
                        tier, length, str(exc))
                    continue
                if status in {PlannerStatus.INFEASIBLE, PlannerStatus.UNKNOWN}:
                    _planner_config_error(
                        col, f"{fp}:[class.{cname}.generate].len_range",
                        tier, length, status.value)


def _local_config(values: SimpleNamespace, gs: GenerateStreamConfig,
                  rules: tuple[SequenceRuleSpec, ...],
                  windows: tuple[SequenceWindowSpec, ...]) -> SimpleNamespace:
    """构造单 attempt 局部检查所需的 planner 适配器。"""
    local_stream = SimpleNamespace(
        sessions=1, frame_gap_s=gs.frame_gap_s, ts_start=gs.ts_start,
        noise_ratio=0, rules=rules, windows=windows,
    )
    return SimpleNamespace(
        frame_class_views=values.frame_class_views,
        generate_stream=local_stream,
        stream=values.stream,
    )


def _planner_config_error(col: _Collector, path: str, tier: TierSpec | None,
                          length: int, detail: str) -> None:
    """记录单个类、档位和长度的 planner 状态错误。"""
    rank = "implicit" if tier is None else str(tier.tier_rank)
    if _planner_error_is_unknown(detail):
        col.error(f"{path}: sequence planner could not verify this potential within the "
                  f"deterministic budget (status = UNKNOWN; tier_rank = {rank}, "
                  f"length = {length})")
        return
    col.error(f"{path}: sequence planner found no feasible potential for tier_rank = "
              f"{rank}, length = {length}: {detail}")


def _planner_error_is_unknown(detail: str) -> bool:
    """识别 planner 的 UNKNOWN 文案，避免把预算未验证写成不可满足。"""
    text = detail.lower()
    return "unknown" in text or "could not be verified" in text or "deterministic budget" in text


def _check_full_potential(col: _Collector, fp: str, gs: GenerateStreamConfig,
                          values: SimpleNamespace) -> None:
    """对存在非零约束配额的流执行一次完整联合规划。"""
    if not _has_nonzero_constraints(gs, values):
        return
    from labelkit.common.runtime.sequence_planner import (
        PlannerConfigError,
        PlannerInternalError,
        question_from_config,
        select_feasible_plan,
    )

    try:
        rng = random.Random(f"{getattr(values, 'run_seed', 0)}:0:generate")
        question = question_from_config(values)
        question = replace(question, solver_seed=rng.getrandbits(31))
        select_feasible_plan(question, rng)
    except PlannerInternalError:
        raise
    except PlannerConfigError as exc:
        if _planner_error_is_unknown(str(exc)):
            col.error(f"{fp}:[generate.stream]: sequence planner could not verify the full "
                      "prefix within the deterministic budget (status = UNKNOWN)")
        else:
            col.error(f"{fp}:[generate.stream]: sequence planner found no feasible full "
                      f"prefix (status = INFEASIBLE): {exc}")
        return
    except ValueError as exc:
        col.error(f"{fp}:[generate.stream]: sequence planner configuration could not be "
                  f"constructed: {exc}")
        return


def _has_nonzero_constraints(gs: GenerateStreamConfig, values: SimpleNamespace) -> bool:
    """判断实际配额前缀是否需要激活全流 planner。"""
    remaining = getattr(values, "limit", None)
    for cname in sorted(values.class_views):
        view = values.class_views[cname]
        count = view.generate.sequences
        if remaining is not None:
            count = min(count, max(remaining, 0))
            remaining -= count
        constrained = effective_rules(view.rules, gs.rules) or effective_windows(
            view.windows, gs.windows)
        if count > 0 and constrained:
            return True
        if remaining == 0:
            break
    return False


def _check_rules(col: _Collector, prefix: str, rules: tuple[SequenceRuleSpec, ...],
                 frame_names: set[str], values: SimpleNamespace) -> None:
    """校验规则模板参数、引用、重复和 correlation Schema 前提。"""
    seen: set[tuple[Any, ...]] = set()
    for index, rule in enumerate(rules, 1):
        loc = f"{prefix}[{index}]"
        _check_rule_shape(col, loc, rule)
        _check_rule_refs(col, loc, rule, frame_names)
        _check_rule_schema(col, loc, rule, values)
        _check_rule_replay_guard(col, loc, rule, values)
        identity = (rule.template, rule.frame_class, rule.source, rule.target,
                    rule.count, rule.time_s, rule.correlation)
        if identity in seen:
            col.error(f"{loc}: duplicate sequence rule declaration; the same rule is already "
                      f"declared in this effective table")
        seen.add(identity)


def _check_rule_replay_guard(col: _Collector, loc: str, rule: SequenceRuleSpec,
                             values: SimpleNamespace) -> None:
    """拒绝与相邻帧 replay guard 明显没有交集的有序时间规则。"""
    if rule.time_s is None or rule.template not in {"chain_response", "chain_precedence"}:
        return
    lo, hi = rule.time_s
    if lo > values.stream.gap_s or hi <= _FRAME_GAP_FLOOR_S:
        col.error(f"{loc}.time_s: the declared half-open interval has no intersection with "
                  f"the adjacent replay guard [1us, stream.gap_s = {values.stream.gap_s}s]")


def _check_rule_shape(col: _Collector, loc: str, rule: SequenceRuleSpec) -> None:
    """校验一条规则的模板参数矩阵。"""
    template = rule.template
    if template in _UNARY_TEMPLATES:
        if rule.frame_class is None:
            col.error(f"{loc}.frame_class: required for unary template {template}")
        if any(value is not None for value in
               (rule.source, rule.target, rule.time_s, rule.correlation)):
            col.error(f"{loc}: source, target, time_s and correlation are only legal for "
                      f"binary templates, got template {template}")
    elif template in _BINARY_TEMPLATES:
        if rule.source is None or rule.target is None:
            col.error(f"{loc}: source and target are required for binary template {template}")
        if rule.frame_class is not None:
            col.error(f"{loc}: frame_class is only legal for unary templates, got template "
                      f"{template}")
    if template in _COUNT_TEMPLATES:
        if rule.count is None:
            col.error(f"{loc}.count: required for template {template}, expected a positive "
                      f"integer")
    elif rule.count is not None:
        col.error(f"{loc}.count: forbidden for template {template}")
    if template not in _BINARY_TEMPLATES and rule.correlation is not None:
        col.error(f"{loc}.correlation: only legal for binary templates, got template "
                  f"{template}")
    if rule.source is not None and rule.target is not None and rule.source == rule.target:
        col.error(f"{loc}.source: source and target must name different frame classes, got "
                  f"{_fmt(rule.source)}")


def _check_rule_refs(col: _Collector, loc: str, rule: SequenceRuleSpec,
                     frame_names: set[str]) -> None:
    """校验规则引用的帧类属于闭集。"""
    for key, name in (("frame_class", rule.frame_class), ("source", rule.source),
                      ("target", rule.target)):
        if name is not None and name not in frame_names:
            col.error(f"{loc}.{key}: frame class {_fmt(name)} is not in "
                      f"[[frame.classify.classes]], available: "
                      f"{', '.join(sorted(frame_names)) if frame_names else '(none)'}")


def _check_rule_schema(col: _Collector, loc: str, rule: SequenceRuleSpec,
                       values: SimpleNamespace) -> None:
    """校验 correlation 两侧结构化 Schema 的字段、required、同型和绑定排除。"""
    corr = rule.correlation
    if corr is None or rule.source is None or rule.target is None:
        return
    if (rule.source not in values.frame_class_views
            or rule.target not in values.frame_class_views):
        return
    source = values.frame_class_views.get(rule.source)
    target = values.frame_class_views.get(rule.target)
    if source is None or target is None:
        return
    if source.gen_schema is None or target.gen_schema is None:
        invalid_schema = ((source.gen_schema is None
                           and rule.source in values.frame_gen_schema_declared)
                          or (target.gen_schema is None
                              and rule.target in values.frame_gen_schema_declared))
        plain_schema = ((source.gen_schema is None
                         and rule.source not in values.frame_gen_schema_declared)
                        or (target.gen_schema is None
                            and rule.target not in values.frame_gen_schema_declared))
        if invalid_schema and not plain_schema:
            return
        col.error(f"{loc}.correlation: source and target frame classes must both use a "
                  f"structured generation schema")
        return
    source_type, target_type, source_present, target_present = _check_correlation_fields(
        col, loc, corr, source, target)
    if source_present and target_present and (source_type != target_type or source_type is None):
        col.error(f"{loc}.correlation: source_field and target_field must declare the same "
                  f"JSON Schema type literally, got source {_fmt(source_type)}, target "
                  f"{_fmt(target_type)}")


def _check_correlation_fields(col: _Collector, loc: str, correlation: Any,
                              source: Any, target: Any) -> tuple[Any, Any, bool, bool]:
    """校验两侧 correlation 字段的结构、required 与 time_fields 排除。"""
    result: list[tuple[Any, bool]] = []
    for side, field, view in (("source", correlation.source_field, source),
                              ("target", correlation.target_field, target)):
        schema = view.gen_schema
        props = schema.get("properties") if isinstance(schema, dict) else None
        required = schema.get("required") if isinstance(schema, dict) else None
        if not isinstance(props, dict) or field not in props:
            col.error(f"{loc}.correlation.{side}_field: {_fmt(field)} must be a top-level "
                      f"property of the {side} frame-class generation schema")
        if not isinstance(required, list) or field not in required:
            col.error(f"{loc}.correlation.{side}_field: {_fmt(field)} must be listed in the "
                      f"required array of the {side} frame-class generation schema")
        if field in (view.time_fields or {}):
            col.error(f"{loc}.correlation.{side}_field: {_fmt(field)} must not be a bound "
                      f"time_fields property")
        present = isinstance(props, dict) and field in props
        prop = props.get(field, {}) if isinstance(props, dict) else {}
        result.append((prop.get("type") if isinstance(prop, dict) else None, present))
    return result[0][0], result[1][0], result[0][1], result[1][1]


def _check_windows(col: _Collector, prefix: str,
                   windows: tuple[SequenceWindowSpec, ...], frame_names: set[str]) -> None:
    """校验窗口引用、星期、同日格式、重叠和跨午夜。"""
    seen: set[str] = set()
    for index, window in enumerate(windows, 1):
        loc = f"{prefix}[{index}]"
        if window.frame_class in seen:
            col.error(f"{loc}.frame_class: duplicate window declaration for frame class "
                      f"{_fmt(window.frame_class)}")
        seen.add(window.frame_class)
        if window.frame_class not in frame_names:
            col.error(f"{loc}.frame_class: frame class {_fmt(window.frame_class)} is not in "
                      f"[[frame.classify.classes]]")
        if not window.of_day:
            col.error(f"{loc}.of_day: expected a non-empty array of same-day half-open "
                      f"windows")
        intervals = []
        for branch, (start, end) in enumerate(window.of_day, 1):
            start_us = _clock_us(col, f"{loc}.of_day[{branch}][1]", start)
            end_us = _clock_us(col, f"{loc}.of_day[{branch}][2]", end)
            if start_us is not None and end_us is not None:
                if start_us >= end_us:
                    col.error(f"{loc}.of_day[{branch}]: window must satisfy start < end "
                              f"within one natural day; cross-midnight windows are not legal")
                intervals.append((start_us, end_us))
        for left, right in zip(sorted(intervals), sorted(intervals)[1:]):
            if left[1] > right[0]:
                col.error(f"{loc}.of_day: branches must not overlap, got {_fmt(list(window.of_day))}")
        if len(set(window.of_week)) != len(window.of_week):
            col.error(f"{loc}.of_week: weekday values must be distinct, got "
                      f"{_fmt(list(window.of_week))}")
        if not window.of_week:
            col.error(f"{loc}.of_week: expected a non-empty array of weekday names")


def _clock_us(col: _Collector, loc: str, value: str) -> int | None:
    """把一个 HH:MM[:SS[.ffffff]] 墙钟字符串转换为微秒。"""
    import re
    match = re.fullmatch(r"(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?", value)
    if match is None:
        col.error(f"{loc}: expected HH:MM, HH:MM:SS or microsecond-precision wall-clock "
                  f"time, got {_fmt(value)}")
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    second = int(match.group(3) or 0)
    micros = int((match.group(4) or "").ljust(6, "0") or 0)
    if hour > 23 or minute > 59 or second > 59:
        col.error(f"{loc}: expected a valid same-day wall-clock time, got {_fmt(value)}")
        return None
    return ((hour * 60 + minute) * 60 + second) * 1_000_000 + micros

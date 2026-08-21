"""M1 时间流生成约束（v1.17 场景规划形态，SPEC-SP §4/§6.2/§8.3）。

本模块承载 v1.13–v1.15 的形态门、档位与时间字段约束；v1.17 起 quota 域、noise 域、
schedule 前提与名称域在此裁定，并**装配 ``ScenarioConfig`` 调用 ``compile_scenario``
一次**——成功 ⇒ ``ResolvedConfig.scenario_plan``；``PlannerInfeasibleError`` 汇入
ConfigError（exit 2），capacity/budget/internal 映射 exit 4（沿 §8.3）。v1.16 的
``_check_local_potential``/``_check_full_potential`` 与 ``select_feasible_plan`` 调用面
已删除。
"""
from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any

from labelkit.common.config._collect import _Collector, _fmt
from labelkit.common.config._sections import (
    _STREAM_FORBIDDEN_CLASS_GEN_KEYS,
    _STREAM_FORBIDDEN_GEN_KEYS,
)
from labelkit.common.config.model import (
    GenerateStreamConfig,
    TierSpec,
    effective_frame_rules,
    effective_frame_windows,
    effective_tiers,
    effective_sequence_rules,
)
from labelkit.common.errors import InternalError
from labelkit.common.contracts.types import (
    SequenceValidationFrame,
    SequenceValidationInput,
)
from labelkit.common.extensions.hooks import (
    ResolvedHook,
    check_hook_arity,
    load_hook,
    probe_hook,
)
from labelkit.common.runtime.scenario.diagnostics import (
    PlannerBudgetError,
    PlannerCapacityError,
    PlannerInfeasibleError,
)
from labelkit.common.runtime.scenario.model import (
    FrameClassDomain,
    ScenarioConfig,
    SequenceClassDomain,
    TierDomain,
)
from labelkit.common.runtime.scenario.planner import compile_scenario
from labelkit.common.runtime.scenario.rules import name_domain_violations


_TIME_FIELD_TERMS: dict[str, str] = {
    "ts": "string",
    "end_ts": "string",
    "gap_prev_s": "number",
    "gap_next_s": "number",
    "elapsed_s": "number",
    "duration_s": "number",
}
_UNARY_TEMPLATES = {"existence", "absence", "exactly", "init", "end"}
_COUNT_TEMPLATES = {"existence", "absence", "exactly"}
_BINARY_TEMPLATES = {"responded_existence", "co_existence", "response", "precedence",
                     "succession", "alternate_response", "chain_response",
                     "chain_precedence", "not_co_existence", "not_succession",
                     "contains"}


def check_generate_stream_form(ctx: Any, products: Any) -> tuple[int, int]:
    """校验时间流形态并编译场景计划，返回序列目标总数与最长长度。

    @param ctx 当前配置收集上下文。
    @param products 已解析的类视图与帧类视图产品（成功时写入 ``scenario_plan``）。
    @return 序列目标总数（形态关闭时 0）与所有类的最长序列长度。
    """
    views = products.class_views
    len_max = max([1] + [view.generate.len_range[1] for view in views.values()])
    if not ctx.p.generate_stream.enabled:
        check_parked_stream_keys(ctx)
        return 0, len_max
    values = SimpleNamespace(
        mode=ctx.mode, modality=ctx.modality, generate=ctx.p.generate,
        classify=ctx.p.classify, class_views=views, stream=ctx.p.stream,
        meta_mode=ctx.p.output.meta_mode, frame_classify=ctx.p.frame_classify,
        frame_annotate=ctx.p.frame_annotate,
        frame_class_views=products.frame_class_views,
        gen_provided=ctx.p.gen_provided,
        class_raw=ctx.p.class_raw if isinstance(ctx.p.class_raw, dict) else {},
        len_max=len_max, text_field=ctx.p.input.text_field,
        generate_stream=ctx.p.generate_stream, run_seed=ctx.p.run.get("seed", 0),
        limit=ctx.cli.limit,
        tiers=ctx.p.generate_stream.tiers,
        frame_gen_schema_declared=_frame_gen_schema_declared(ctx.p.frame_class_raw),
        root=ctx.root, hooks=products.hooks,
    )
    check_stream_constraints(ctx.col, ctx.fp, ctx.p.generate_stream, values)
    _compile_scenario_plan(ctx.col, ctx.fp, ctx.p.generate_stream, values, products)
    return len(getattr(products.scenario_plan, "slots", ()) or ()), len_max


def check_stream_constraints(col: _Collector, fp: str, gs: GenerateStreamConfig,
                             values: SimpleNamespace) -> None:
    """校验时间流形态的跨节组合约束（编译前的静态门）。

    @param col 配置错误与警告收集器。
    @param fp 当前配置文件路径前缀。
    @param gs 时间流生成配置。
    @param values 跨节约束所需的解析配置视图。
    """
    _stream_form_premise(col, fp, values)
    _stream_form_probes(col, fp, values)
    _stream_form_limit(col, fp, values)
    _stream_form_quota(col, fp, values)
    _stream_form_schedule(col, fp, gs)
    _stream_form_noise(col, fp, gs, values)
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
            col.error(f"{fp}:{owner}: the field name must not be \"truth\" in the time-stream "
                      f"form (it would collide with the ground-truth key of the artifact row)")


def _stream_form_probes(col: _Collector, fp: str, values: SimpleNamespace) -> None:
    """校验时间流形态下的定向禁设键与 scenario_validator 形态门。"""
    for key in _STREAM_FORBIDDEN_GEN_KEYS:
        if values.gen_provided.get(key):
            col.error(f"{fp}:[generate].{key}: the time-stream form does not provide this "
                      f"key - sequence quotas are carried by "
                      f"[[generate.stream.quotas]], sequence length by len_range and "
                      f"noise batching is per noise slot; remove this key")
    for cname, sections in values.class_raw.items():
        g_over = sections.get("generate") if isinstance(sections, dict) else None
        if not isinstance(g_over, dict):
            continue
        for key in _STREAM_FORBIDDEN_CLASS_GEN_KEYS:
            if key in g_over:
                col.error(f"{fp}:[class.{cname}.generate].{key}: the time-stream form does "
                          f"not provide this key (per-record expansion / seeds per call "
                          f"belong to the flat generation forms); use quotas / len_range "
                          f"instead")
    for name, on in (("frame.classify", values.frame_classify.enabled),
                     ("frame.annotate", values.frame_annotate.enabled)):
        if on:
            col.error(f"{fp}:[{name}].enabled: mutually exclusive with the time-stream form "
                      f"- frame-class ground truth is known at generation time (the planner "
                      f"word is the truth), so no frame-level verdict is needed; write frame "
                      f"content contracts in [frame.class.<name>.generate]")


def _stream_form_limit(col: _Collector, fp: str, values: SimpleNamespace) -> None:
    """``--limit`` 与时间流形态互斥（rule 62 尾注：quota 是整体契约，截断前缀不再声称满足）。"""
    if values.limit is not None:
        col.error(f"{fp}:[generate.stream].enabled: mutually exclusive with --limit - the "
                  f"quota is a whole contract and a truncated prefix no longer claims quota "
                  f"satisfaction; remove --limit (rule 62)")


def _stream_form_quota(col: _Collector, fp: str, values: SimpleNamespace) -> None:
    """校验 quota 表在场性、类域与帧类表。"""
    gs: GenerateStreamConfig = values.generate_stream
    if not gs.quotas:
        col.error(f"{fp}:[[generate.stream.quotas]]: the time-stream form requires at "
                  f"least one quota table and a compiled sequence-target total >= 1 (a "
                  f"zero-sequence project has only noise and sourceless duplicates, "
                  f"nothing deliverable)")
    class_names = {spec.name for spec in values.classify.classes}
    for quota in gs.quotas:
        for name in _quota_class_names(quota):
            if name not in class_names:
                col.error(f"{fp}:[[generate.stream.quotas]] (name = {quota.name!r}): "
                          f"sequence class {_fmt(name)} is not in [[classify.classes]] "
                          f"(rule 71 - this domain check runs before quota-form "
                          f"arithmetic so a typo never surfaces as a solver "
                          f"infeasibility)")
    if not values.frame_classify.classes:
        col.error(f"{fp}:[[frame.classify.classes]]: the time-stream form requires a non-empty "
                  f"frame class table (the planner picks each frame from that closed set; "
                  f"frame.classify.enabled stays false)")


def _quota_class_names(quota: Any) -> tuple[str, ...]:
    """列出一张 quota 表引用的全部 sequence class 名。"""
    names = [name for name, _ in (quota.counts or ())]
    names.extend(name for name, _ in (quota.weights or ()))
    return tuple(names)


def _stream_form_schedule(col: _Collector, fp: str, gs: GenerateStreamConfig) -> None:
    """schedule 必填门（形态开启时；形状与区间已在解析层裁定）。"""
    if gs.schedule is None:
        col.error(f"{fp}:[generate.stream.schedule]: required in the time-stream form - "
                  f"declare start / end (both ISO-8601 with an explicit Z or numeric "
                  f"offset, same offset, end > start) and optional exclude_dates; the "
                  f"schedule is the only time boundary (the v1.16 per-session one-week "
                  f"horizon recursion is deleted)")


def _stream_form_noise(col: _Collector, fp: str, gs: GenerateStreamConfig,
                       values: SimpleNamespace) -> None:
    """noise 域检查（rule 69 的 M1 侧：ratio↔表双向、类域、指令、排除域）。"""
    if not 0 <= gs.noise_ratio < 1:
        col.error(f"{fp}:[generate.stream].noise_ratio: expected a number in [0,1) (noise "
                  f"frames / task frames ratio), got {_fmt(gs.noise_ratio)}")
    elif gs.noise_ratio > 0 and not gs.noise_classes:
        col.error(f"{fp}:[[generate.stream.noise]]: required when noise_ratio > 0 - "
                  f"declare at least one noise frame class with a positive integer "
                  f"weight")
    elif gs.noise_ratio == 0 and gs.noise_classes:
        col.error(f"{fp}:[[generate.stream.noise]]: a noise table at noise_ratio = 0 is "
                  f"a directed config error - raise noise_ratio above 0 or omit the "
                  f"table")
    frame_names = {spec.name for spec in values.frame_classify.classes}
    noise_names = {spec.frame_class for spec in gs.noise_classes}
    for spec in gs.noise_classes:
        if spec.frame_class not in frame_names:
            col.error(f"{fp}:[[generate.stream.noise]]: frame class {_fmt(spec.frame_class)}"
                      f" is not in [[frame.classify.classes]]")
            continue
        view = values.frame_class_views.get(spec.frame_class)
        if view is None or not (view.gen_instruction or "").strip():
            col.error(f"{fp}:[frame.class.{spec.frame_class}.generate].instruction: a "
                      f"noise frame class must provide a non-empty generation "
                      f"instruction (schema parsing, budget checks and realization "
                      f"reuse the task-frame path)")
        if view is not None and (view.duration_us is not None or view.resources):
            col.error(f"{fp}:[frame.class.{spec.frame_class}.generate]: noise frame class "
                      f"must not declare duration_s or resources (noise occurrences are "
                      f"point frames)")
    if frame_names and noise_names and not (frame_names - noise_names):
        col.error(f"{fp}:[[generate.stream.noise]]: the task-frame candidate domain is "
                  f"empty after excluding the noise classes - at least one non-noise "
                  f"frame class must remain for task positions")
    _check_noise_exclusions(col, fp, gs, values, noise_names)


def _check_noise_exclusions(col: _Collector, fp: str, gs: GenerateStreamConfig,
                            values: SimpleNamespace, noise_names: set[str]) -> None:
    """noise 帧类不得出现在任何生效 tier、frame rule 或 frame window。"""
    for cname, view in values.class_views.items():
        for spec in effective_tiers(view.tiers, gs.tiers):
            for name in spec.frame_classes:
                if name in noise_names:
                    col.error(f"{fp}:[[generate.stream.tiers]]: noise frame class "
                              f"{_fmt(name)} must not appear in a tier composition "
                              f"(class {_fmt(cname)})")
        for rule in effective_frame_rules(view.frame_rules, gs.frame_rules):
            for name in filter(None, (rule.frame_class, rule.source, rule.target)):
                if name in noise_names:
                    col.error(f"{fp}:[[generate.stream.frame_rules]] (name = "
                              f"{rule.name!r}): noise frame class {_fmt(name)} must "
                              f"not appear in a frame rule")
        for window in effective_frame_windows(view.frame_windows, gs.frame_windows):
            if window.frame_class in noise_names:
                col.error(f"{fp}:[[generate.stream.frame_windows]] (name = "
                          f"{window.name!r}): noise frame class "
                          f"{_fmt(window.frame_class)} must not appear in a frame window")


def check_parked_stream_keys(ctx: Any) -> None:
    """形态关闭时拒绝时间流专属的显式配置。

    @param ctx 当前配置收集上下文。
    """
    if ctx.p.gen_provided.get("stream_tiers"):
        ctx.col.error(f"{ctx.fp}:[[generate.stream.tiers]]: the tier table is only legal in "
                      f"the time-stream generation form ([generate.stream].enabled = true) - "
                      f"a tier declares the frame-class composition of the sequences drawn "
                      f"from it, and only that form plans sequences from a frame class table")
    if ctx.p.gen_provided.get("stream_section"):
        ctx.col.error(f"{ctx.fp}:[generate.stream]: the stream sub-table is only legal in "
                      f"the time-stream generation form ([generate.stream].enabled = "
                      f"true) - quotas, schedule, noise, crossed_sessions, frame rules "
                      f"and frame windows are planning inputs of that form")
    for key in ("sequence_validator", "scenario_validator"):
        if getattr(ctx.p.generate, key) is not None:
            ctx.col.error(f"{ctx.fp}:[generate].{key}: {key} is only "
                          f"legal in the time-stream generation form "
                          f"([generate.stream].enabled = true)")
    for cname, sections in (ctx.p.class_raw or {}).items():
        g_over = sections.get("generate") if isinstance(sections, dict) else None
        if not isinstance(g_over, dict):
            continue
        if "tiers" in g_over:
            ctx.col.error(f"{ctx.fp}:[class.{cname}.generate].tiers: the per-class tier table "
                          f"is only legal in the time-stream generation form "
                          f"([generate.stream].enabled = true) - it overrides the global "
                          f"[[generate.stream.tiers]] table for sequences of this class")
        for key in ("frame_rules", "frame_windows"):
            if key in g_over:
                ctx.col.error(f"{ctx.fp}:[class.{cname}.generate].{key}: the per-class "
                              f"{key} table is only legal in the time-stream generation "
                              f"form ([generate.stream].enabled = true)")


# ── 档位与时间字段（v1.14/v1.15 面，quota 对检查后移到计划期）────────────────


def _check_tier_table(col: _Collector, fp: str, values: SimpleNamespace) -> None:
    """校验全局与按类档位表的结构；配额对（长度下界）改由计划期按 slot 检查。"""
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


# ── frame rule / frame window（v1.16 语义换名 + name 域）─────────────────────


def _check_rule_window_tables(col: _Collector, fp: str, gs: GenerateStreamConfig,
                              values: SimpleNamespace) -> None:
    """校验生效 frame_rules/frame_windows 的语法、Schema 前提与全局名称域。"""
    frame_names = {spec.name for spec in values.frame_classify.classes}
    _check_rules(col, f"{fp}:[[generate.stream.frame_rules]]", gs.frame_rules,
                 frame_names, values)
    _check_windows(col, f"{fp}:[[generate.stream.frame_windows]]", gs.frame_windows)
    for cname, view in values.class_views.items():
        if view.frame_rules is not None:
            _check_rules(col, f"{fp}:[[class.{cname}.generate.frame_rules]]",
                         view.frame_rules, frame_names, values)
        if view.frame_windows is not None:
            _check_windows(col, f"{fp}:[[class.{cname}.generate.frame_windows]]",
                           view.frame_windows)
    _check_name_domain(col, fp, gs, values)
    _check_sequence_validator(col, fp, values.generate.sequence_validator, values)


def _check_name_domain(col: _Collector, fp: str, gs: GenerateStreamConfig,
                       values: SimpleNamespace) -> None:
    """quota/frame rule/frame window（与 Wave 5 sequence rule）共享的全局唯一名称域。"""
    # 名称域按**来源表**分别检查（全局表 + 每张按类声明表）——按类覆盖只换生效表，
    # 不复制名称；用生效并集会把继承的全局行数两遍。
    source_rules: list[Any] = list(gs.frame_rules)
    source_windows: list[Any] = list(gs.frame_windows)
    for view in values.class_views.values():
        if view.frame_rules is not None:
            source_rules.extend(view.frame_rules)
        if view.frame_windows is not None:
            source_windows.extend(view.frame_windows)
    for problem in name_domain_violations(gs.quotas, source_rules,
                                          source_windows, ()):
        col.error(f"{fp}:[[generate.stream.quotas]]: shared name domain violation - "
                  f"{problem} (quota, frame rule and frame window names share one "
                  f"global-unique domain; a per-class override changes the effective "
                  f"table, it never copies or renames natural names)")


def _check_sequence_validator(col: _Collector, fp: str, ref: str | None,
                              values: SimpleNamespace) -> None:
    """解析并干跑序列钩子，冻结其单位置参数与返回值契约。

    v1.17(SPEC-SP §4.9)：引用统一 ``<python-file>:<attribute-path>``，按 project root
    解析为 ``ResolvedHook`` 冻结载体（存入 ``values.hooks["sequence"]``，由 loader 冻结
    进 ``ResolvedConfig.validation_hooks``）；M6 的 invoke 面不再按字符串二次 resolve。
    """
    if ref is None:
        return
    loc = f"{fp}:[generate].sequence_validator"
    try:
        hook: ResolvedHook = load_hook(ref, values.root)
    except ValueError as exc:
        col.error(f"{loc}: {exc}")
        return
    problem = check_hook_arity(hook, 1)
    if problem is None:
        problem = probe_hook(hook, (_sequence_validator_probe(values),))
    if problem is not None:
        col.error(f"{loc}: {problem}")
        return
    values.hooks["sequence"] = hook


def _sequence_validator_probe(values: SimpleNamespace) -> SequenceValidationInput:
    """构造不含用户数据的代表性序列钩子输入。"""
    participating = sorted(
        (name, view) for name, view in values.class_views.items())
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


def _check_rules(col: _Collector, prefix: str, rules: tuple[Any, ...],
                 frame_names: set[str], values: SimpleNamespace) -> None:
    """校验规则模板参数、引用、重复和 correlation Schema 前提。"""
    seen: set[tuple[Any, ...]] = set()
    for index, rule in enumerate(rules, 1):
        loc = f"{prefix}[{index}]"
        _check_rule_shape(col, loc, rule)
        _check_rule_refs(col, loc, rule, frame_names)
        _check_rule_schema(col, loc, rule, values)
        identity = (rule.name, rule.template, rule.frame_class, rule.source,
                    rule.target, rule.count, rule.time_us, rule.correlation)
        if identity in seen:
            col.error(f"{loc}: duplicate frame rule declaration; the same rule is already "
                      f"declared in this effective table")
        seen.add(identity)


def _check_rule_shape(col: _Collector, loc: str, rule: Any) -> None:
    """校验一条规则的模板参数矩阵（v1.16 语义 + contains）。"""
    template = rule.template
    if template in _UNARY_TEMPLATES:
        if rule.frame_class is None:
            col.error(f"{loc}.frame_class: required for unary template {template}")
        if any(value is not None for value in
               (rule.source, rule.target, rule.time_us, rule.correlation)):
            col.error(f"{loc}: source, target, time_s and correlation are only legal for "
                      f"binary templates, got template {template}")
    elif template in _BINARY_TEMPLATES:
        if rule.source is None or rule.target is None:
            col.error(f"{loc}: source and target are required for binary template {template}")
        if rule.frame_class is not None:
            col.error(f"{loc}.frame_class: only legal for unary templates, got template "
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


def _check_rule_refs(col: _Collector, loc: str, rule: Any,
                     frame_names: set[str]) -> None:
    """校验规则引用的帧类属于闭集。"""
    for key, name in (("frame_class", rule.frame_class), ("source", rule.source),
                      ("target", rule.target)):
        if name is not None and name not in frame_names:
            col.error(f"{loc}.{key}: frame class {_fmt(name)} is not in "
                      f"[[frame.classify.classes]], available: "
                      f"{', '.join(sorted(frame_names)) if frame_names else '(none)'}")


def _check_rule_schema(col: _Collector, loc: str, rule: Any,
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


def _check_windows(col: _Collector, prefix: str, windows: tuple[Any, ...]) -> None:
    """校验窗口引用、星期、同日区间与重叠。"""
    seen: set[str] = set()
    for index, window in enumerate(windows, 1):
        loc = f"{prefix}[{index}]"
        if window.frame_class in seen:
            col.error(f"{loc}.frame_class: duplicate window declaration for frame class "
                      f"{_fmt(window.frame_class)}")
        seen.add(window.frame_class)
        intervals = []
        for branch, (start_us, end_us) in enumerate(window.of_day_us, 1):
            if start_us >= end_us:
                col.error(f"{loc}.of_day[{branch}]: window must satisfy start < end "
                          f"within one natural day; cross-midnight windows are not legal")
            else:
                intervals.append((start_us, end_us))
        for left, right in zip(sorted(intervals), sorted(intervals)[1:]):
            if left[1] > right[0]:
                col.error(f"{loc}.of_day: branches must not overlap")
        if not window.of_week:
            col.error(f"{loc}.of_week: expected a non-empty array of weekday names")


# ── ScenarioConfig 装配与唯一编译入口 ───────────────────────────────────────


def _quantize_frame_gap(col: _Collector, fp: str,
                        gs: GenerateStreamConfig) -> tuple[int, int] | None:
    """frame_gap_s 闭区间的 Decimal 量化（[ceil(lo×1e6), floor(hi×1e6)]）。"""
    try:
        lo = Decimal(str(gs.frame_gap_s[0])) * Decimal(1_000_000)
        hi = Decimal(str(gs.frame_gap_s[1])) * Decimal(1_000_000)
        lo_us = math.ceil(lo)
        hi_us = math.floor(hi)
    except (InvalidOperation, ValueError):
        col.error(f"{fp}:[generate.stream].frame_gap_s: expected finite numbers")
        return None
    if lo < Decimal(1) or lo_us < 1:
        col.error(f"{fp}:[generate.stream].frame_gap_s: the lower bound must be >= "
                  f"1e-6 s (one microsecond) - the laid-out timestamps must be strictly "
                  f"increasing and a sub-microsecond gap rounds to a zero timedelta, got "
                  f"lower bound {_fmt(gs.frame_gap_s[0])}")
        return None
    if lo_us > hi_us:
        col.error(f"{fp}:[generate.stream].frame_gap_s: the range quantizes to an empty "
                  f"integer-microsecond interval - widen it so that at least one "
                  f"microsecond is representable, got {_fmt(list(gs.frame_gap_s))}")
        return None
    return lo_us, hi_us


def _scenario_config(col: _Collector, fp: str, gs: GenerateStreamConfig,
                     values: SimpleNamespace) -> "ScenarioConfig | None":
    """把已通过静态门的配置装配成 ``compile_scenario`` 的冻结参数对象。"""
    gap = _quantize_frame_gap(col, fp, gs)
    if gap is None:
        return None
    sequence_classes = tuple(
        SequenceClassDomain(
            name=spec.name,
            length_range=(view.generate.len_range[0], view.generate.len_range[1]),
            tiers=tuple(TierDomain(rank=tier.tier_rank, weight=tier.weight,
                                   frame_classes=tuple(tier.frame_classes))
                        for tier in effective_tiers(view.tiers, values.tiers)),
            frame_rules=effective_frame_rules(view.frame_rules, gs.frame_rules),
            frame_windows=effective_frame_windows(view.frame_windows, gs.frame_windows),
            sequence_rules=effective_sequence_rules(view.sequence_rules, gs.sequence_rules),
        )
        for spec, view in ((spec, values.class_views[spec.name])
                           for spec in values.classify.classes))
    frame_classes = tuple(
        FrameClassDomain(name=spec.name, duration_us=products_view.duration_us,
                         resources=products_view.resources)
        for spec, products_view in ((spec, values.frame_class_views[spec.name])
                                    for spec in values.frame_classify.classes))
    span_s = values.stream.session_max_span_s
    return ScenarioConfig(
        seed=values.run_seed,
        schedule=gs.schedule,           # type: ignore[arg-type]
        quotas=gs.quotas,
        sequence_classes=sequence_classes,
        frame_classes=frame_classes,
        sequence_rules=gs.sequence_rules,
        crossed_sessions=gs.crossed_sessions,
        frame_gap_us=gap,
        session_gap_us=int(values.stream.gap_s) * 1_000_000,
        session_max_len=values.stream.session_max_len,
        session_max_span_us=span_s * 1_000_000 if span_s > 0 else None,
        noise_ratio=Decimal(str(gs.noise_ratio)),
        noise_classes=gs.noise_classes,
        duplicates=gs.duplicates,
    )


def _compile_scenario_plan(col: _Collector, fp: str, gs: GenerateStreamConfig,
                           values: SimpleNamespace, products: Any) -> None:
    """装配 ``ScenarioConfig`` 并调用 ``compile_scenario`` 一次（§6.2 唯一入口）。

    静态门有错时不进入求解（避免对已判非法的配置输出 solver 噪声）；
    ``PlannerInfeasibleError`` 汇入 ConfigError（exit 2）；capacity/budget/internal
    映射 exit 4（§8.3，经 ``InternalError`` 通道上抛）。
    """
    if col.errors or gs.schedule is None:
        return
    config = _scenario_config(col, fp, gs, values)
    if config is None:
        return
    try:
        plan = compile_scenario(config)
    except PlannerInfeasibleError as exc:
        col.error(f"{fp}:[generate.stream]: scenario planning found no feasible plan "
                  f"(status = INFEASIBLE): {exc}")
        return
    except (PlannerCapacityError, PlannerBudgetError) as exc:
        raise InternalError(str(exc)) from exc
    except RuntimeError as exc:            # PlannerInternalError 及解码不变量
        raise InternalError(str(exc)) from exc
    products.scenario_plan = plan
    _check_plan_tier_pairs(col, fp, gs, values, plan)
    _check_plan_instruction_domain(col, fp, gs, values, plan)


def _check_plan_tier_pairs(col: _Collector, fp: str, gs: GenerateStreamConfig,
                           values: SimpleNamespace, plan: Any) -> None:
    """计划期配额对检查：每个非零 target 类的 len_range 下界 ≥ 生效档构成大小。"""
    targets = {slot.sequence_class for slot in plan.slots}
    for cname in targets:
        view = values.class_views[cname]
        table = effective_tiers(view.tiers, gs.tiers)
        for spec in table:
            if view.generate.len_range[0] < len(spec.frame_classes):
                col.error(f"{fp}:[class.{cname}.generate].len_range: the lower bound must "
                          f"be >= the composition size of every tier this class draws from "
                          f"(tier_rank = {spec.tier_rank} declares "
                          f"{len(spec.frame_classes)} frame classes and each of them must "
                          f"appear at least once), got lower bound "
                          f"{view.generate.len_range[0]}")


def _check_plan_instruction_domain(col: _Collector, fp: str, gs: GenerateStreamConfig,
                                   values: SimpleNamespace, plan: Any) -> None:
    """计划期帧类指令域：参与类生效档构成 ∪（无档 = 非噪音全表）内指令必填。"""
    participating = {slot.sequence_class for slot in plan.slots}
    noise_names = {spec.frame_class for spec in gs.noise_classes}
    domain: set[str] = set()
    tiered = False
    for cname in participating:
        view = values.class_views[cname]
        table = effective_tiers(view.tiers, gs.tiers)
        if table:
            tiered = True
            domain.update(name for spec in table for name in spec.frame_classes)
    if not tiered:
        domain = {spec.name for spec in values.frame_classify.classes} - noise_names
    domain -= noise_names
    reason = ("the planner word covers the union of the participating classes' effective "
              "tier compositions, so any frame class of a tier may be picked"
              if tiered else
              "the planner word spans the whole non-noise table, so any task frame "
              "class may be picked")
    for name in sorted(domain):
        view = values.frame_class_views.get(name)
        if view is None or not (view.gen_instruction or "").strip():
            col.error(f"{fp}:[frame.class.{name}.generate].instruction: every task frame "
                      f"class must provide a non-empty generation instruction ({reason}), "
                      f"expected a non-empty string")
    for cname, view in values.class_views.items():
        if cname in participating and not view.generate.instruction.strip():
            col.error(f"{fp}:[class.{cname}.generate].instruction: a participating "
                      f"sequence class must provide a non-empty generation instruction "
                      f"(the global [generate].instruction sets a default)")
    if gs.tiers and not participating:
        return
    if gs.tiers:
        covered = set()
        for cname in participating:
            covered.update(name for spec in effective_tiers(
                values.class_views[cname].tiers, gs.tiers)
                for name in spec.frame_classes)
        for spec in values.frame_classify.classes:
            if spec.name not in covered and spec.name not in noise_names:
                col.warn(f"{fp}:[frame.class.{spec.name}.generate]: frame class "
                         f"{_fmt(spec.name)} is in no effective tier composition of a "
                         f"participating class, so it can never be picked - its whole "
                         f"generate face (instruction, schema, time_fields) is dead "
                         f"config")

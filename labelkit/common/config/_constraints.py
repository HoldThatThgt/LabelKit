"""M1 的跨节组合约束簇(CONTRACTS §6.3 规则 2–21 与 spec 2.3.1 阶段组合矩阵)。

每个函数负责一簇约束, 调用次序即报错聚合次序——不得随意调整。所有函数只往
``_Collector`` 里记账, 从不提前抛出; 少数函数会回传被"回填/冻结"过的配置对象
(classify.max_labels 回填、segment/frame_classify 的 vision_resolved 冻结)。

拆分预案(≤ 2000 行硬约束的余量已很紧): 下次增簇时把 v1.13/v1.14 的生成形态簇
(``_check_generate_stream`` 起至绑定簇止)整体迁往 ``_genstream.py``, ``validate``
侧只留一次调用——切口沿形态边界, 与 2026-08-14 的 M1 拆分同款。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from jsonschema.validators import Draft202012Validator

from labelkit.common.config._classviews import (
    _ClassBases,
    _FrameViewCtx,
    _GlobalDryRun,
    _ViewInputs,
    _avail,
    _build_class_views,
    _build_frame_class_views,
)
from labelkit.common.config._collect import _Collector, _fmt
from labelkit.common.config._rubrics import _RubricSite, _check_pointwise_rubric, _resolve_rubric
from labelkit.common.config._schemas import (
    _DryRun,
    _dryrun_fewshot,
    _load_frame_schema,
    _load_user_schema,
)
from labelkit.common.config._sections import (
    _STREAM_FORBIDDEN_CLASS_GEN_KEYS,
    _STREAM_FORBIDDEN_GEN_KEYS,
    _Project,
)
from labelkit.common.config.model import (
    ClassView,
    CliOverrides,
    EmbeddingProfile,
    FewShotExample,
    FrameClassView,
    GenerateStreamConfig,
    LLMProfile,
    Rubric,
    TierSpec,
    apportion_tiers,
)
from labelkit.common.extensions.hooks import resolve_hook
from labelkit.common.runtime import budget


@dataclass(frozen=True)
class _LoadCtx:
    """校验各阶段共享的上下文(两份配置的解析产物 + CLI 覆盖)。"""

    col: _Collector                                    # 全轮错误/警告聚合器
    fc: str                                            # config.toml 路径字符串
    fp: str                                            # project.toml 路径字符串
    cli: CliOverrides                                  # CLI 覆盖值(优先级最高)
    config_ok: bool                                    # config.toml 是否解析成功
    llm_profiles: dict[str, LLMProfile]                # LLM profile 表(密钥回填就地改)
    embedding_profiles: dict[str, EmbeddingProfile]    # 嵌入 profile 表
    p: _Project                                        # project.toml 的解析产物
    modality: str                                      # 生效模态
    mode: str                                          # 生效运行模式


@dataclass
class _Products:
    """校验阶段依次填充的产物累加器(最终交给装配阶段)。"""

    referenced: set[str] = field(default_factory=set)   # 被启用阶段引用的 profile 名集合
    user_schema: dict = field(default_factory=dict)     # [output] 用户 Schema
    dryrun: _GlobalDryRun = field(default_factory=_GlobalDryRun)   # 全局 Schema/回调干跑面
    selector: str = ""                                  # 全局生效的准则选择器
    rubric: Rubric | None = None                        # 全局生效准则
    rubric_is_inline: bool = False                      # 全局准则是否来自内联表
    class_views: dict[str, ClassView] = field(default_factory=dict)          # 序列类视图
    frame_schema: dict | None = None                    # 帧级输出 Schema
    frame_class_views: dict[str, FrameClassView] = field(default_factory=dict)  # 帧类视图
    eff_input: str | None = None                        # 生效输入路径(CLI > project)
    eff_output: str | None = None                       # 生效输出路径(CLI > project)


def _check_llm_ref(ctx: _LoadCtx, loc: str, name: str) -> None:
    """规则 2–5: 一个 profile 引用必须存在于 config.toml 的 ``[llm.*]``。

    @param ctx 校验上下文
    @param loc 报错定位前缀
    @param name 被引用的 profile 名
    """
    if ctx.config_ok and name and name not in ctx.llm_profiles:
        ctx.col.error(f"{loc}: referenced profile {_fmt(name)} does not exist in "
                      f"config.toml [llm.*], available: {_avail(ctx.llm_profiles)}")


def _check_cli_overrides(ctx: _LoadCtx) -> None:
    """校验 CLI 覆盖里唯一需要 M1 裁定的取值(``--log-level`` 枚举)。

    @param ctx 校验上下文
    """
    level = ctx.cli.log_level
    if level is not None and level not in ("debug", "info", "warn", "error"):
        ctx.col.error(f'cli:--log-level: expected "debug" | "info" | "warn" | "error", '
                      f"got {_fmt(level)}")


def _check_profile_refs(ctx: _LoadCtx) -> None:
    """规则 2/3/4/5(§6.3): 逐个阶段检查其 profile 引用的存在性。

    @param ctx 校验上下文
    """
    p, fp = ctx.p, ctx.fp
    if p.segment.enabled and p.segment.strategy in ("llm", "hybrid"):
        # v1.8 S30: rules 策略零 LLM 调用——只有真正会拨号的策略才让 segment.llm 入集
        _check_llm_ref(ctx, f"{fp}:[segment].llm", p.segment.llm)
    if p.stitch.enabled:
        # v1.9 T16/T17: 纯文本判定——启用即引用, 但永不加入下面的 vision 必需集
        _check_llm_ref(ctx, f"{fp}:[stitch].llm", p.stitch.llm)
    if p.classify.enabled:
        # 与下面的 verify 同理: 阶段关闭时默认引用("default")不必存在(v1.7, R24 ①)
        _check_llm_ref(ctx, f"{fp}:[classify].llm", p.classify.llm)
    if p.frame_classify.enabled:
        # v1.12: enabled 即入存在性引用集; 永不入下方 vision 必需集(vision 语义分列
        # 裁决——vision_resolved 自适应推导, segment V3 同款)
        _check_llm_ref(ctx, f"{fp}:[frame.classify].llm", p.frame_classify.llm)
    if p.frame_annotate.enabled:
        # v1.12: enabled 即入存在性引用集(vision 登记见下方 ui 分支)
        _check_llm_ref(ctx, f"{fp}:[frame.annotate].llm", p.frame_annotate.llm)
    if p.extract.enabled:
        _check_llm_ref(ctx, f"{fp}:[extract].llm", p.extract.llm)   # v1.8 S30: 启用即引用
    _check_llm_ref(ctx, f"{fp}:[quality].llm", p.quality.llm)
    _check_llm_ref(ctx, f"{fp}:[annotate].llm", p.annotate.llm)
    for i, name in enumerate(p.generate.llms, 1):
        _check_llm_ref(ctx, f"{fp}:[generate].llms[{i}]", name)
    if p.verify.enabled and not p.verify.judges:
        # spec §5.2 脚注 †: 关闭时不要求默认 "judge" 存在; 非空评审团在运行期**取代**
        # verify.llm(3.7.2), 故其存在性也不作要求(E2E 发现 P3-8)——评审团成员在下面查
        _check_llm_ref(ctx, f"{fp}:[verify].llm", p.verify.llm)
    if p.output.repair_llm is not None:
        _check_llm_ref(ctx, f"{fp}:[output].repair_llm", p.output.repair_llm)
    _check_judge_panels(ctx)


def _check_judge_panels(ctx: _LoadCtx) -> None:
    """评审团面板: 成员逐个查存在性, 非空时长度须为奇数。

    @param ctx 校验上下文
    """
    for section, judges in (("quality", ctx.p.quality.judges),
                            ("verify", ctx.p.verify.judges)):
        for i, name in enumerate(judges, 1):
            _check_llm_ref(ctx, f"{ctx.fp}:[{section}].judges[{i}]", name)
        if judges and len(judges) % 2 == 0:
            ctx.col.error(f"{ctx.fp}:[{section}].judges: must have an odd length when "
                          f"non-empty, got {len(judges)}")


def _vision_users(ctx: _LoadCtx) -> dict[str, set[str]]:
    """收集 UI 模态下必须具备 vision 能力的 profile → 引用它的阶段名集合。

    v1.11 (V3): segment 对 vision 是**自适应**的(vision_resolved 解析产物), 永不入本
    集合——原先受 use_vision 把守的分支已失去可失败性。

    @param ctx 校验上下文
    @return profile 名 → 阶段名集合
    """
    p = ctx.p
    users: dict[str, set[str]] = {}
    if p.classify.enabled:
        users.setdefault(p.classify.llm, set()).add("classify")
    if p.extract.enabled:
        users.setdefault(p.extract.llm, set()).add("extract")   # v1.8 S30: 恒读相邻截图
    if p.quality.enabled and not p.segment.enabled:
        # v1.8 S30 放宽: 流模式的质量打分把序列当纯文本评(转移 + 帧摘要, 不附图)
        refs = (p.quality.judges if p.quality.judges and p.quality.mode == "pairwise"
                else (p.quality.llm,))
        for name in refs:
            users.setdefault(name, set()).add("quality")
    if p.annotate.enabled:
        users.setdefault(p.annotate.llm, set()).add("annotate")
    if p.frame_annotate.enabled:
        # v1.12(vision 语义分列): frame.annotate.llm 在 ui ∧ enabled 时无条件入 vision
        # 必需集(截图是帧标注主证据, 镜像序列级 annotate); frame.classify.llm 永不入
        # 此集——附图与否由 vision_resolved 解析产物自适应决定。
        users.setdefault(p.frame_annotate.llm, set()).add("frame.annotate")
    if p.verify.enabled:
        for name in (p.verify.judges or (p.verify.llm,)):
            users.setdefault(name, set()).add("verify")
    return users


def _check_vision_profiles(ctx: _LoadCtx) -> None:
    """UI 模态下, 被视觉必需阶段引用的 profile 须 ``supports_vision = true``。

    @param ctx 校验上下文
    """
    if ctx.modality != "ui":
        return
    for name, stages in _vision_users(ctx).items():
        prof = ctx.llm_profiles.get(name)
        if prof is not None and not prof.supports_vision:
            ctx.col.error(f"{ctx.fc}:[llm.{name}].supports_vision: a profile referenced by "
                          f"the {'/'.join(sorted(stages))} stage(s) in UI modality must have "
                          f"supports_vision = true, got false")


def _check_dedup_semantic(ctx: _LoadCtx) -> None:
    """语义去重开启时必须指向一个存在的 ``[embedding.*]`` profile。

    @param ctx 校验上下文
    """
    dedup = ctx.p.dedup
    if not dedup.semantic:
        return
    if dedup.semantic_embedding is None:
        ctx.col.error(f"{ctx.fp}:[dedup].semantic_embedding: required when dedup.semantic = "
                      f"true, expected an [embedding.*] profile name from config.toml")
    elif ctx.config_ok and dedup.semantic_embedding not in ctx.embedding_profiles:
        ctx.col.error(f"{ctx.fp}:[dedup].semantic_embedding: referenced profile "
                      f"{_fmt(dedup.semantic_embedding)} does not exist in config.toml "
                      f"[embedding.*], available: {_avail(ctx.embedding_profiles)}")


def _check_cross_field(ctx: _LoadCtx) -> None:
    """规则 6–9(v1.2): 质量选择组、评审团 no-op、自洽采样奇偶、生成混合权重。

    @param ctx 校验上下文
    """
    col, fp, p = ctx.col, ctx.fp, ctx.p
    quality = p.quality
    if quality.selection == "top_ratio":
        if quality.top_ratio is None and not p.top_ratio_provided:
            col.error(f'{fp}:[quality].top_ratio: required when selection = "top_ratio", '
                      f"expected a number in (0,1]")
        if quality.threshold is not None:
            col.error(f'{fp}:[quality].threshold: mutually exclusive with quality.top_ratio '
                      f'(must not be set when selection = "top_ratio")')
    elif quality.top_ratio is not None or p.top_ratio_provided:
        # 静默陷阱护栏(E2E 发现 P3-7): selection 仍是 "threshold" 时设 top_ratio 合法
        # 但是空转——要大声说出来。
        col.warn(f'{fp}:[quality].top_ratio: selection is still the default "threshold", so '
                 f'this key has no effect - to keep a fixed ratio also set '
                 f'selection = "top_ratio"')
    if quality.enabled and quality.judges and quality.mode == "pointwise":
        # 同族的空转护栏: 评审团只定义在成对比较上(spec 3.4.4)——逐条打分恒用 quality.llm。
        col.warn(f'{fp}:[quality].judges: the judges panel has no effect in pointwise mode '
                 f'(pointwise scoring always uses quality.llm) - to use a judges panel '
                 f'switch mode = "pairwise"')
    sc = p.annotate.self_consistency
    if sc != 0 and (sc < 3 or sc % 2 == 0):
        col.error(f"{fp}:[annotate].self_consistency: expected 0 or an odd number >= 3, got {sc}")
    _check_generate_weights(ctx)


def _check_generate_weights(ctx: _LoadCtx) -> None:
    """``mixture = "weighted"`` 时的权重数组约束(长度对齐 + 逐元素为正)。

    风格名唯一性与提示非空已在解析期强制。

    @param ctx 校验上下文
    """
    col, fp, generate = ctx.col, ctx.fp, ctx.p.generate
    if generate.mixture != "weighted":
        return
    if not generate.weights:
        col.error(f'{fp}:[generate].weights: required when mixture = "weighted", expected an '
                  f"array of positive numbers (length = generate.llms)")
        return
    if len(generate.weights) != len(generate.llms):
        col.error(f"{fp}:[generate].weights: expected length {len(generate.llms)} "
                  f"(= generate.llms), got length {len(generate.weights)}")
    for i, w in enumerate(generate.weights, 1):
        if not w > 0:
            col.error(f"{fp}:[generate].weights[{i}]: expected positive number, got {_fmt(w)}")


def _check_run_mode(ctx: _LoadCtx) -> None:
    """规则 10/11(v1.4; = 阶段约束④): generate_only 与 process 两种模式的键面。

    @param ctx 校验上下文
    """
    if ctx.mode == "generate_only":
        _check_generate_only_mode(ctx)
        return
    col, fp, provided = ctx.col, ctx.fp, ctx.p.gen_provided
    for key in ("seed_examples", "standalone_count"):
        if provided[key]:
            col.error(f'{fp}:[generate].{key}: can only be set when run.mode = '
                      f'"generate_only" (must not be set in process mode)')


def _check_generate_only_mode(ctx: _LoadCtx) -> None:
    """``run.mode = "generate_only"`` 分支的键面约束。

    @param ctx 校验上下文
    """
    col, fp, p = ctx.col, ctx.fp, ctx.p
    if p.run["input"] is not None:
        col.error(f'{fp}:[run].input: must be absent when run.mode = "generate_only", '
                  f"got {_fmt(p.run['input'])}")
    if ctx.cli.input is not None:
        col.error(f'cli:--input: must not provide an input path when run.mode = '
                  f'"generate_only", got {_fmt(ctx.cli.input)}')
    if ctx.modality != "text":
        col.error(f'{fp}:[run].modality: run.mode = "generate_only" requires "text", '
                  f"got {_fmt(ctx.modality)}")
    if not p.generate.enabled:
        col.error(f'{fp}:[generate].enabled: run.mode = "generate_only" requires '
                  f"generate.enabled = true")
    # v1.13: 时间流形态自带配额面(按类 sequences × len_range)——种子池与独立计数两族键
    # 都不适用(显式书写由形态约束簇定向报错), 互斥校验仅平面形态执行。
    if not p.generate_stream.enabled:
        _check_flat_generate_only(ctx)


def _check_flat_generate_only(ctx: _LoadCtx) -> None:
    """平面 generate_only 形态: ``seed_examples`` 与 ``standalone_count`` 恰一。

    @param ctx 校验上下文
    """
    col, fp, p = ctx.col, ctx.fp, ctx.p
    seed_set = p.gen_provided["seed_examples"]
    standalone_set = p.gen_provided["standalone_count"]
    if seed_set and standalone_set:
        col.error(f"{fp}:[generate].seed_examples: mutually exclusive with standalone_count, "
                  f"exactly one of them must be provided")
    elif not seed_set and not standalone_set:
        col.error(f"{fp}:[generate].seed_examples: generate_only mode requires either "
                  f"seed_examples (a non-empty string array) or standalone_count (>= 1)")
    elif seed_set:
        if not p.generate.seed_examples:
            col.error(f"{fp}:[generate].seed_examples: expected a non-empty string array, "
                      f"got an empty array")
        for i, s in enumerate(p.generate.seed_examples, 1):
            if not s.strip():
                col.error(f"{fp}:[generate].seed_examples[{i}]: expected non-empty string, "
                          f"got {_fmt(s)}")
    # standalone_count >= 1 已在解析期强制


def _collect_referenced(ctx: _LoadCtx) -> set[str]:
    """规则 12: 汇总"被启用阶段真正会拨号"的 profile 集合。

    评审团只在 PAIRWISE 模式取代 quality.llm(spec 3.4.4: 逐条打分恒用 quality.llm;
    另见 cli.referenced_profiles)——引用集必须与运行期一致。

    @param ctx 校验上下文
    @return profile 名集合
    """
    p = ctx.p
    judges_active = bool(p.quality.judges) and p.quality.mode == "pairwise"
    referenced: set[str] = set()
    if p.segment.enabled and p.segment.strategy in ("llm", "hybrid"):
        referenced.add(p.segment.llm)      # v1.8, S30 引用集要点②
    if p.stitch.enabled:
        referenced.add(p.stitch.llm)       # v1.9, T17 引用集要点
    if p.classify.enabled and not p.generate_stream.enabled:
        # v1.7, R24 引用集要点②; v1.13: 时间流形态的序列标签直接继承(inherited,
        # classify 零判决调用)⇒ 援引 S30 先例豁免密钥引用集——存在性检查照旧
        # (拼错 profile 名仍要在启动期揪出, 不需要活密钥)
        referenced.add(p.classify.llm)
    if p.frame_classify.enabled:
        referenced.add(p.frame_classify.llm)   # v1.12 帧级分类引用集要点
    if p.frame_annotate.enabled:
        referenced.add(p.frame_annotate.llm)   # v1.12 帧级标注引用集要点
    if p.extract.enabled:
        referenced.add(p.extract.llm)      # v1.8, S30 引用集要点②
    if p.quality.enabled:
        referenced |= set(p.quality.judges) if judges_active else {p.quality.llm}
    if p.annotate.enabled:
        referenced.add(p.annotate.llm)
    if p.generate.enabled:
        referenced |= set(p.generate.llms)
    if p.verify.enabled:
        referenced |= set(p.verify.judges) if p.verify.judges else {p.verify.llm}
    if p.output.repair_llm is not None:
        referenced.add(p.output.repair_llm)
    return referenced


def _resolve_keys(ctx: _LoadCtx, kind: str, prof_name: str,
                  envs: tuple[str, ...]) -> tuple[str, ...] | None:
    """解析一个被引用 profile 的**全部**环境变量(v1.6 密钥池)。

    每个缺失变量各出一条聚合错误。

    @param ctx 校验上下文
    @param kind profile 族("llm" | "embedding")
    @param prof_name profile 名
    @param envs 声明的环境变量名元组
    @return 对齐的密钥元组; 任一变量缺失/为空时返回 None
    """
    pooled = len(envs) > 1
    keys: list[str] = []
    ok = True
    for i, env in enumerate(envs, 1):
        key = os.environ.get(env, "")
        if not key:
            loc = (f"{ctx.fc}:[{kind}.{prof_name}].api_key_envs[{i}]" if pooled
                   else f"{ctx.fc}:[{kind}.{prof_name}].api_key_env")
            ctx.col.error(f"{loc}: environment variable {_fmt(env)} is not set or empty")
            ok = False
        keys.append(key)
    return tuple(keys) if ok else None


def _resolve_api_keys(ctx: _LoadCtx, products: _Products) -> None:
    """规则 12: 只为被引用的 profile 解析 API 密钥, 并就地回填到 profile 表。

    @param ctx 校验上下文
    @param products 产物累加器(填充 ``referenced``)
    """
    products.referenced = _collect_referenced(ctx)
    for name in sorted(products.referenced):
        prof = ctx.llm_profiles.get(name)
        if prof is None or not prof.api_key_envs:
            continue    # profile 缺失 / 密钥声明非法都已在别处报过
        keys = _resolve_keys(ctx, "llm", name, prof.api_key_envs)
        if keys is not None:
            ctx.llm_profiles[name] = replace(prof, api_key=keys[0], api_keys=keys)
    dedup = ctx.p.dedup
    if dedup.semantic and dedup.semantic_embedding in ctx.embedding_profiles:
        prof_e = ctx.embedding_profiles[dedup.semantic_embedding]
        if prof_e.api_key_envs:
            keys = _resolve_keys(ctx, "embedding", prof_e.name, prof_e.api_key_envs)
            if keys is not None:
                ctx.embedding_profiles[prof_e.name] = replace(
                    prof_e, api_key=keys[0], api_keys=keys)


def _load_schema_and_hooks(ctx: _LoadCtx, products: _Products) -> None:
    """规则 13–15 与规则 17: 用户 Schema、few-shot 干跑与两个用户校验钩子。

    @param ctx 校验上下文
    @param products 产物累加器(填充 ``user_schema`` 与 ``dryrun``)
    """
    col, fp, p = ctx.col, ctx.fp, ctx.p
    user_schema, schema_ok = _load_user_schema(col, fp, p.output)
    skey = "schema_inline" if p.output.schema_inline is not None else "schema_path"
    dr = products.dryrun
    dr.validator = Draft202012Validator(user_schema) if schema_ok else None
    dr.schema_key = skey
    dr.schema_ok = schema_ok
    products.user_schema = user_schema
    if dr.validator is not None and p.annotate.examples:
        dr.schema_alive, _ = _dryrun_fewshot(col, p.annotate.examples, _DryRun(
            file=fp, elem_label="annotate.examples", validator=dr.validator,
            schema_key=skey))
    # 规则 17 — 校验钩子(v1.5 方案 A, spec 3.8.2/3.6.2)
    if p.output.validator is not None:
        try:
            dr.hook = resolve_hook(p.output.validator)
        except ValueError as e:
            col.error(f"{fp}:[output].validator: {e}")
    dr.hook_ref = p.output.validator
    if p.generate.enabled and p.generate.sample_validator is not None:
        try:
            resolve_hook(p.generate.sample_validator)
        except ValueError as e:
            col.error(f"{fp}:[generate].sample_validator: {e}")
    if dr.hook is not None and schema_ok and p.annotate.examples:
        _, dr.hook_alive = _dryrun_fewshot(col, p.annotate.examples, _DryRun(
            file=fp, elem_label="annotate.examples", schema_key=skey,
            hook=dr.hook, hook_ref=dr.hook_ref))


def _resolve_global_rubric(ctx: _LoadCtx, products: _Products) -> None:
    """规则 16: 解析全局生效准则并跑 pointwise 六级检查。

    v1.8 S29: 流模式下空选择器对**两种模态**都解析为轨迹准则(逐帧的 default:ui 准则
    对无图序列打分没有意义); 显式选择器恒胜出, 按类视图经回填的基线选择器继承。
    v1.13(裁决·轨迹准则自动解析扩展): 空选择器条件扩为
    ``segment.enabled ∨ generate_stream.enabled``——时间流形态打的也是序列/轨迹分。

    @param ctx 校验上下文
    @param products 产物累加器(填充 ``selector`` / ``rubric`` / ``rubric_is_inline``)
    """
    p = ctx.p
    if p.quality.rubric:
        selector = p.quality.rubric
    elif p.segment.enabled or p.generate_stream.enabled:
        selector = "default:trajectory"
    else:
        selector = "default:ui" if ctx.modality == "ui" else "default:text"
    site = _RubricSite(file=ctx.fp, selector=selector, modality=ctx.modality)
    rubric, is_inline = _resolve_rubric(ctx.col, site, p.rubric_raw)
    products.selector, products.rubric, products.rubric_is_inline = selector, rubric, is_inline
    if p.quality.mode == "pointwise":
        _check_pointwise_rubric(ctx.col, site, rubric, is_inline)


def _check_classify_table(ctx: _LoadCtx) -> None:
    """v1.7: ``classify.enabled = true`` 时的类表约束(类数、fallback、max_labels)。

    v1.13(裁决·序列类约束按形态放宽): 时间流形态无判决路径(标签 inherited),
    「≥ 2 类」与「fallback_class 必填」两条规则的保护对象不存在 ⇒ 放宽为 ≥ 1 类、
    fallback 免填(写了仍须 ∈ 类表)。

    @param ctx 校验上下文
    """
    col, fp, p = ctx.col, ctx.fp, ctx.p
    classify = p.classify
    class_names = tuple(c.name for c in classify.classes)
    min_classes = 1 if p.generate_stream.enabled else 2
    if len(classify.classes) < min_classes:
        col.error(f"{fp}:[classify].classes: classify.enabled = true requires >= "
                  f"{min_classes} declared classes (the [[classify.classes]] array of "
                  f"tables), got {len(classify.classes)}")
    if not classify.fallback_class and not p.generate_stream.enabled:
        col.error(f"{fp}:[classify].fallback_class: required when classify.enabled = true, "
                  f"expected a class name from [[classify.classes]]")
    elif classify.fallback_class and class_names \
            and classify.fallback_class not in class_names:
        col.error(f"{fp}:[classify].fallback_class: referenced class name "
                  f"{_fmt(classify.fallback_class)} is not in [[classify.classes]], "
                  f"available: {_avail(class_names)}")
    if (classify.max_labels is not None and len(class_names) >= 2
            and not 2 <= classify.max_labels <= len(class_names)):
        col.error(f"{fp}:[classify].max_labels: expected an integer in [2, "
                  f"{len(class_names)}] (upper bound = number of classes), "
                  f"got {classify.max_labels}")
    if isinstance(p.class_raw, dict):
        for cname in p.class_raw:
            if cname not in class_names:
                col.error(f"{fp}:[class.{cname}]: class name {_fmt(cname)} is not in "
                          f"[[classify.classes]], available: {_avail(class_names)}")


def _warn_parked_classify(ctx: _LoadCtx) -> None:
    """R8: classify 关闭时, 停放的类配置合法——一次性告警并点名被忽略的表。

    @param ctx 校验上下文
    """
    p = ctx.p
    ignored = ["[[classify.classes]]"] if p.classify_provided["classes"] else []
    if isinstance(p.class_raw, dict):
        ignored += [f"[class.{n}]" for n in p.class_raw]
    if ignored:
        ctx.col.warn(f"{ctx.fp}:[classify].enabled: classify.enabled = false, "
                     f"{_avail(tuple(ignored))} will have no effect and is ignored "
                     f"(keeping the config with the switch off is legal)")


def _check_classify_and_views(ctx: _LoadCtx, products: _Products) -> _LoadCtx:
    """v1.7: classify 自洽采样/多标签键面 + 类表约束 + 按类视图物化(spec 5.2)。

    @param ctx 校验上下文
    @param products 产物累加器(填充 ``class_views``)
    @return 可能回填了 ``classify.max_labels`` 的新上下文
    """
    col, fp, p = ctx.col, ctx.fp, ctx.p
    sc_c = p.classify.self_consistency
    if sc_c != 0 and (sc_c < 3 or sc_c % 2 == 0):
        col.error(f"{fp}:[classify].self_consistency: expected 0 or an odd number >= 3, "
                  f"got {sc_c}")
    if p.classify_provided["max_labels"] and p.classify.assignment != "multi":
        col.error(f'{fp}:[classify].max_labels: can only be set when assignment = "multi"')
    if not p.classify.enabled:
        _warn_parked_classify(ctx)
        return ctx
    _check_classify_table(ctx)
    classify = p.classify
    if classify.max_labels is None:
        classify = replace(classify, max_labels=len(classify.classes))   # spec 5.2 回填
        ctx = replace(ctx, p=replace(p, classify=classify))
    inputs = _ViewInputs(
        file=fp, modality=ctx.modality, selector=products.selector,
        rubric=products.rubric, rubric_is_inline=products.rubric_is_inline,
        bases=_ClassBases(quality=replace(p.quality, rubric=products.selector),
                          annotate=p.annotate, generate=p.generate,
                          verify=p.verify, extract=p.extract),
        dryrun=products.dryrun)
    products.class_views = _build_class_views(col, classify, p.class_raw, inputs)
    return ctx


def _check_stage_matrix(ctx: _LoadCtx) -> None:
    """规则 17–19: 阶段组合矩阵(spec 2.3.1 约束①–③)。

    @param ctx 校验上下文
    """
    col, fp, p = ctx.col, ctx.fp, ctx.p
    if not p.annotate.enabled and not p.quality.enabled:
        col.error(f"{fp}:[quality].enabled: quality and annotate must not both be disabled "
                  f"(at least one must be enabled, 2.3.1 constraint ①)")
    if p.verify.enabled and not p.annotate.enabled:
        col.error(f"{fp}:[verify].enabled: verify.enabled = true requires "
                  f"annotate.enabled = true (2.3.1 constraint ②)")
    if p.generate.enabled:
        if ctx.modality != "text":
            col.error(f'{fp}:[generate].enabled: generate.enabled = true requires '
                      f'run.modality = "text", got {_fmt(ctx.modality)} '
                      f"(2.3.1 constraint ③)")
        if ctx.mode == "process" and not p.quality.enabled:
            col.error(f"{fp}:[generate].enabled: in process mode generate.enabled = true "
                      f"requires quality.enabled = true (seeds come from the quality gate, "
                      f"2.3.1 constraint ③)")
    # 约束④ 就是上面的 generate_only 簇(规则 10)


# ── v1.8 §3.6 — stream / segment / extract 约束 ──────────────────────────────


def _check_stage_switches(ctx: _LoadCtx) -> None:
    """v1.8/v1.9: segment / stitch / extract 三个开关的前置条件与互斥。

    @param ctx 校验上下文
    """
    col, fp, p = ctx.col, ctx.fp, ctx.p
    if p.segment.enabled:
        if ctx.mode != "process":
            col.error(f'{fp}:[segment].enabled: segment.enabled = true requires '
                      f'run.mode = "process", got {_fmt(ctx.mode)}')
        if p.generate.enabled:
            col.error(f"{fp}:[segment].enabled: segment.enabled = true and "
                      f"generate.enabled = true are mutually exclusive (stream mode does "
                      f"no generation)")
        if not p.annotate.enabled:
            col.error(f"{fp}:[segment].enabled: segment.enabled = true requires "
                      f"annotate.enabled = true (constraint ⑭: episodes must be annotated "
                      f"into the user schema)")
    if p.stitch.enabled and not p.segment.enabled:
        # v1.9 T17: stitch 只消费 segment 的产物
        col.error(f"{fp}:[stitch].enabled: stitch.enabled = true requires "
                  f"segment.enabled = true (thread stitching only applies to segmentation "
                  f"products)")
    if p.stitch.votes % 2 == 0:
        # v1.9 T18/M-4: 严格多数决需要奇数样本(judges / classify.self_consistency 先例)
        col.error(f"{fp}:[stitch].votes: expected an odd number >= 1 (strict majority over "
                  f"(verdict, thread_ref)), got {p.stitch.votes}")
    if p.extract.enabled:
        if not p.segment.enabled:
            col.error(f"{fp}:[extract].enabled: extract.enabled = true requires "
                      f"segment.enabled = true (transition extraction only applies to "
                      f"sequence records)")
        if ctx.modality != "ui":
            col.error(f'{fp}:[extract].enabled: extract.enabled = true requires '
                      f'run.modality = "ui", got {_fmt(ctx.modality)} (text sequences are '
                      f"out of scope in v1)")


def _check_stream_keys(ctx: _LoadCtx) -> None:
    """``[stream]`` 的时间序键、分区键与两个窗口尺寸键的约束。

    @param ctx 校验上下文
    """
    col, fp, p = ctx.col, ctx.fp, ctx.p
    stream = p.stream
    order_is_meta = stream.order_by.startswith("meta:")
    if stream.order_by != "input_order" and not (order_is_meta
                                                 and stream.order_by[len("meta:"):]):
        col.error(f'{fp}:[stream].order_by: expected "input_order" | "meta:<field>", '
                  f"got {_fmt(stream.order_by)}")
    elif order_is_meta and ctx.modality != "text":
        col.error(f'{fp}:[stream].order_by: "meta:<field>" is only available in text '
                  f'modality (run.modality = "text"), got modality {_fmt(ctx.modality)}')
    if stream.session_max_span_s > 0 and not order_is_meta:
        # 必然是显式书写(默认 0 即关闭)——硬错误
        col.error(f'{fp}:[stream].session_max_span_s: > 0 requires order_by = '
                  f'"meta:<field>" (a time span needs a time-ordered key), got order_by '
                  f"{_fmt(stream.order_by)}")
    if p.stream_provided["gap_s"] and not order_is_meta:
        # gap_s 带非零默认值(300)——只有显式取值才表达用户意图, 且这属提示而非致命
        # (spec §3.6)
        col.warn(f'{fp}:[stream].gap_s: gap_s is set explicitly but order_by is not '
                 f'"meta:<field>", so time-gap splitting has no effect - to split by time '
                 f'set order_by = "meta:<field>"')
    for i, k in enumerate(stream.key, 1):
        if k == "source_dir":
            continue
        if k.startswith("meta:") and k[len("meta:"):]:
            if ctx.modality != "text":
                col.error(f'{fp}:[stream].key[{i}]: a "meta:<field>" partition key is only '
                          f"available in text modality, got {_fmt(k)}")
        else:
            col.error(f'{fp}:[stream].key[{i}]: expected "meta:<field>" (text only) | '
                      f'"source_dir", got {_fmt(k)}')
    if p.segment.window < 2:
        col.error(f"{fp}:[segment].window: expected an integer >= 2 (a sliding window must "
                  f"contain at least one adjacent frame pair), got {p.segment.window}")
    if not 2 <= p.annotate.sequence_frames <= 100:
        col.error(f"{fp}:[annotate].sequence_frames: expected an integer in [2, 100], "
                  f"got {p.annotate.sequence_frames}")


def _warn_segment_on(ctx: _LoadCtx, selector: str) -> None:
    """segment 开启时的四条提示性告警(S21/S28/S29 家族与 stitch 停放键)。

    @param ctx 校验上下文
    @param selector 全局生效的准则选择器
    """
    col, fp, p = ctx.col, ctx.fp, ctx.p
    if p.annotate.sequence_frames > 20:
        prof_a = ctx.llm_profiles.get(p.annotate.llm)
        if prof_a is not None and prof_a.max_image_px > 2000:
            # S28: Anthropic 对 >20 图请求硬拒任一边 >2000px 的图(400, 非自动缩放)
            col.warn(f"{fp}:[annotate].sequence_frames: sequence_frames = "
                     f"{p.annotate.sequence_frames} > 20 and the profile referenced by "
                     f"annotate [llm.{p.annotate.llm}] has max_image_px = "
                     f"{prof_a.max_image_px} > 2000 - Anthropic hard-rejects >20-image "
                     f"requests carrying any image with an edge > 2000px (400, not "
                     f"auto-downscaled); set max_image_px <= 2000 or lower "
                     f"sequence_frames back to <= 20")
    if p.stream.session_max_len > p.run["batch_size"]:
        col.warn(f"{fp}:[stream].session_max_len: session_max_len = "
                 f"{p.stream.session_max_len} > run.batch_size = {p.run['batch_size']}; "
                 f"over-long sessions will be hard-cut by M10 and marked "
                 f"session_split (S21)")
    _warn_segment_strategy(ctx)
    # S29 组合提示: 仅当**生效准则**就是轨迹准则时才提示(含空选择器的流模式解析)——
    # 显式选了 default:text/ui/inline 的用户是按自己的准则打分, 不该被告知在打轨迹分。
    if p.quality.enabled and not p.extract.enabled and selector == "default:trajectory":
        col.warn(f"{fp}:[quality].enabled: segment.enabled = true and "
                 f"extract.enabled = false, so trajectory scoring (default:trajectory) "
                 f"evaluates \"frame-to-frame change\" from frame digests instead of a "
                 f"structured action sequence - enable [extract] to score by action sequence")


def _warn_segment_strategy(ctx: _LoadCtx) -> None:
    """segment 策略相关的两条告警 + stitch 停放键告警。

    @param ctx 校验上下文
    """
    col, fp, p = ctx.col, ctx.fp, ctx.p
    if p.segment.strategy == "rules" and p.segment.noise_filter:
        col.warn(f'{fp}:[segment].noise_filter: noise_filter has no effect when '
                 f'strategy = "rules" (noise marking and min_len only apply to the '
                 f"llm/hybrid strategies) - switch strategy to filter noise frames")
    if p.stitch.enabled and p.segment.strategy == "rules":
        # v1.9 T17 提示: rules 分段把整会话粗段喂进缝合池——合法但通常非本意
        col.warn(f'{fp}:[stitch].enabled: with segment.strategy = "rules" there is no LLM '
                 f"refinement, so stitching consumes coarse whole-session segments - "
                 f'switch strategy to "llm" or "hybrid" to stitch at task granularity')
    if p.stitch_provided["non_switch_keys"] and not p.stitch.enabled:
        # v1.9 T17: 停放清单告警在 segment-off 分支——本组合(stitch 关、segment 开且带
        # 载荷)自成一条告警(sequence_frames 先例)
        col.warn(f"{fp}:[stitch].enabled: stitch.enabled = false, the remaining [stitch] "
                 f"keys will have no effect and are ignored (keeping the config with the "
                 f"switch off is legal)")


def _warn_segment_off(ctx: _LoadCtx) -> None:
    """segment 关闭时的 R8 停放清单告警(留配置、关开关合法)。

    @param ctx 校验上下文
    """
    col, fp, p = ctx.col, ctx.fp, ctx.p
    parked = []
    if p.stream_provided["section"] and not p.generate_stream.enabled:
        # v1.13(裁决·停放豁免精确化): 时间流形态复用摄取侧词汇——[stream] 是生成侧的
        # 铺设契约(order_by 声明工件时间戳字段、gap_s 定会话间隔下界), 此时不是停放配置
        parked.append("[stream]")
    if p.segment_provided["non_switch_keys"]:
        parked.append("[segment]")
    if p.stitch_provided["non_switch_keys"] and not p.stitch.enabled:
        parked.append("[stitch]")                  # v1.9 (T17)
    if p.extract_provided["non_switch_keys"] and not p.extract.enabled:
        parked.append("[extract]")
    if (p.frame_provided["section"] and not p.frame_classify.enabled
            and not p.frame_annotate.enabled and not p.generate_stream.enabled):
        # v1.12 no-op 约束: [frame.*] 节在场 ∧ 均未启用 ∧ segment off ⇒ 入 R8 停放清单
        # (任一帧开关启用时由「帧粒度要求流模式」CONFIG_ERROR 接管, 不再重复告警)。
        # v1.13: 时间流形态下帧类表与 [frame.class.*.generate] 是生效面, 不入停放清单
        parked.append("[frame]")
    if parked:
        col.warn(f"{fp}:[segment].enabled: segment.enabled = false, "
                 f"{_avail(tuple(parked))} will have no effect and is ignored (keeping the "
                 f"config with the switch off is legal)")
    if p.sequence_frames_provided:
        col.warn(f"{fp}:[annotate].sequence_frames: segment.enabled = false; "
                 f"sequence_frames only applies to sequence annotation (stream mode), so "
                 f"it has no effect")


def _check_stream_family(ctx: _LoadCtx, products: _Products) -> None:
    """v1.8 流家族约束簇的驱动器(开关前置条件 → 键面 → 开/关两侧的提示)。

    @param ctx 校验上下文
    @param products 产物累加器(读取全局准则选择器)
    """
    _check_stage_switches(ctx)
    _check_stream_keys(ctx)
    if ctx.p.segment.enabled:
        _warn_segment_on(ctx, products.selector)
    else:
        _warn_segment_off(ctx)


# ── v1.12 — 帧粒度 [frame.*] 组合约束(SPEC-frame-annotation §3.1 七条) ────────


def _check_frame_switches(ctx: _LoadCtx) -> None:
    """约束·帧粒度要求流模式 + 两条定向探针 + meta_mode 护栏。

    @param ctx 校验上下文
    """
    col, fp, p = ctx.col, ctx.fp, ctx.p
    for fname, fon in (("frame.classify", p.frame_classify.enabled),
                       ("frame.annotate", p.frame_annotate.enabled)):
        if fon and not p.segment.enabled:
            col.error(f"{fp}:[{fname}].enabled: {fname}.enabled = true requires "
                      f"segment.enabled = true (frame granularity is stream-mode only) - "
                      f"outside stream mode use classify + [class.<name>.annotate] "
                      f"per-class annotation")
    # 约束·定向探针(v1.11 use_vision 原始节探针同款机制): 帧级无多标签、无自洽采样。
    if p.frame_provided["classify_assignment"]:
        col.error(f"{fp}:[frame.classify].assignment: frame classification does not provide "
                  f"assignment - frame classes are always single-assignment (frame-level "
                  f"multi-label / fan-out is a v1.12 non-goal); remove this key and use the "
                  f"sequence-level [classify].assignment for multi-label fan-out")
    if p.frame_provided["annotate_self_consistency"]:
        col.error(f"{fp}:[frame.annotate].self_consistency: frame annotation does not "
                  f"provide self_consistency - self-consistency sampling costs n times more "
                  f"and the voting key would have to come from the frame schema (a v1.12 "
                  f"non-goal); remove this key and use the sequence-level "
                  f"[annotate].self_consistency")
    # 约束·meta_mode 护栏: 帧产物仅经 _meta.stream.members 承载(sidecar 合法)。
    if ((p.frame_classify.enabled or p.frame_annotate.enabled)
            and p.output.meta_mode == "none"):
        col.error(f'{fp}:[output].meta_mode: must not be "none" when frame granularity '
                  f"(frame.classify / frame.annotate) is enabled - frame products are "
                  f"carried only by _meta.stream.members and meta_mode = \"none\" would "
                  f'drop all of them (sidecar is legal), got "none"')


def _check_frame_class_table(ctx: _LoadCtx) -> None:
    """约束·fallback 合法 + 帧标注 instruction 必填(§5.2 † 家族的帧级镜像)。

    帧类表与序列类表相互独立、允许重名、互不约束。

    @param ctx 校验上下文
    """
    col, fp, p = ctx.col, ctx.fp, ctx.p
    frame_names = tuple(c.name for c in p.frame_classify.classes)
    if p.frame_classify.enabled:
        if any(c.examples for c in p.frame_classify.classes):
            # 帧级批量判决模板不渲染类别示例(§10.12, 与序列级 §10.8 的 few-shot 渲染
            # 有意不同)——显名提示避免"配置了但静默无效"的锐边。
            col.warn(f"{fp}:[frame.classify].classes: class examples are not rendered by "
                     f"the batched frame-verdict template (§10.12), so this key is ignored")
        if not p.frame_classify.fallback_class:
            col.error(f"{fp}:[frame.classify].fallback_class: required when "
                      f"frame.classify.enabled = true, expected a class name from "
                      f"[[frame.classify.classes]]")
        elif p.frame_classify.fallback_class not in frame_names:
            # 空类表不放行(available: (none))——fallback ∈ 帧类表 传递性地要求类表非空
            # (v1.12 约束表无独立的 ≥N 类数规则, 与 [classify] 的 ≥2 规则有意不同)。
            col.error(f"{fp}:[frame.classify].fallback_class: referenced class name "
                      f"{_fmt(p.frame_classify.fallback_class)} is not in "
                      f"[[frame.classify.classes]], available: {_avail(frame_names)}")
    if p.frame_annotate.enabled and not p.frame_annotate.instruction.strip():
        col.error(f"{fp}:[frame.annotate].instruction: required when "
                  f"frame.annotate.enabled = true, expected a non-empty string")


def _load_frame_annotate_schema(ctx: _LoadCtx, products: _Products) -> _FrameViewCtx:
    """约束·帧 Schema 恰一 + 元校验 + examples 干跑(镜像 output.schema 全套分支)。

    仅 enabled 时执行——留配置、关开关合法, 帧 Schema 不做停放校验。

    @param ctx 校验上下文
    @param products 产物累加器(填充 ``frame_schema``)
    @return 帧类视图上下文(带校验器与存活标志)
    """
    col, fp, p = ctx.col, ctx.fp, ctx.p
    fskey = ("schema_inline" if p.frame_annotate.schema_inline is not None
             else "schema_path")
    fctx = _FrameViewCtx(file=fp, classify=p.frame_classify, annotate=p.frame_annotate,
                         class_raw=p.frame_class_raw, schema_key=fskey)
    if not p.frame_annotate.enabled:
        return fctx
    fschema, fs_ok = _load_frame_schema(col, fp, p.frame_annotate)
    if fs_ok:
        products.frame_schema = fschema
        fctx.validator = Draft202012Validator(fschema)
    if fctx.validator is not None and p.frame_annotate.examples:
        fctx.schema_alive, _ = _dryrun_fewshot(col, p.frame_annotate.examples, _DryRun(
            file=fp, elem_label="frame.annotate.examples", validator=fctx.validator,
            schema_key=fskey, schema_section="frame.annotate", schema_noun="frame schema"))
    return fctx


def _check_frame_namespace(ctx: _LoadCtx, products: _Products) -> None:
    """约束·帧类覆盖: 在场前提、生成节的形态限定, 以及帧类视图物化。

    零覆盖的帧类也各得一份视图(class_views 同款——下游运行期永不回退)。

    @param ctx 校验上下文
    @param products 产物累加器(填充 ``frame_class_views``)
    """
    col, fp, p = ctx.col, ctx.fp, ctx.p
    fctx = _load_frame_annotate_schema(ctx, products)
    frame_ns_live = p.frame_classify.enabled or p.generate_stream.enabled
    raw = p.frame_class_raw
    if isinstance(raw, dict) and raw and not frame_ns_live:
        for cname in raw:
            col.error(f"{fp}:[frame.class.{cname}]: the presence of [frame.class.*] "
                      f"requires frame.classify.enabled = true or "
                      f"generate.stream.enabled = true (frame-class overrides depend on "
                      f"frame classification producing the labels; the time-stream "
                      f"generation form declares frame content contracts through "
                      f"[frame.class.*.generate])")
    if isinstance(raw, dict) and not p.generate_stream.enabled:
        # v1.13(裁决·帧类生成面): generate 节仅时间流生成形态合法——反向定向
        # CONFIG_ERROR(白名单接纳该节名, 故此处必须显名拦截, 否则会静默无效)
        for cname, sections_g in raw.items():
            if isinstance(sections_g, dict) and "generate" in sections_g:
                col.error(f"{fp}:[frame.class.{cname}.generate]: this section is only legal "
                          f"in the time-stream generation form "
                          f"([generate.stream].enabled = true) - write "
                          f"[frame.class.{cname}.annotate] for frame annotation")
    if frame_ns_live:
        products.frame_class_views = _build_frame_class_views(col, fctx)


def _check_frame_family(ctx: _LoadCtx, products: _Products) -> None:
    """v1.12 帧粒度约束簇的驱动器。

    @param ctx 校验上下文
    @param products 产物累加器
    """
    _check_frame_switches(ctx)
    _check_frame_class_table(ctx)
    _check_frame_namespace(ctx, products)


# ── v1.13 时间流生成形态的组合约束(SPEC-stream-generation §3.1 约束表) ────────

# v1.14(裁决·语义词表四值): 时间语义词表是**冻结闭集**(扩词走 spec 修订)——键 = 词,
# 值 = 该词要求绑定属性字面声明的 JSON 类型(ts 是 ISO 串, 其余是 round(ts 差秒, 6))。
_TIME_FIELD_TERMS: dict[str, str] = {
    "ts": "string",           # 本帧已铺时间戳(ISO 串; 重发帧承源值)
    "gap_prev_s": "number",   # 与本序列上一帧的间隔秒(首帧 0.0)
    "gap_next_s": "number",   # 与本序列下一帧的间隔秒(末帧 0.0)
    "elapsed_s": "number",    # 距本序列首帧秒(首帧 0.0)
}

# v1.14(裁决·微秒地板): 帧间隔下界的分辨率地板——isoformat 精度与 round(·, 6) 的下界。
_FRAME_GAP_FLOOR_S = 1e-6


def _check_generate_stream(col: _Collector, fp: str, gs: GenerateStreamConfig,
                           v: SimpleNamespace) -> None:
    """时间流形态([generate.stream].enabled = true)的 M1 约束簇驱动器。

    ``v`` 是调用方组装的取值捆包(mode / modality / generate / classify / class_views /
    stream / meta_mode / frame_classify / frame_annotate / frame_class_views /
    gen_provided / class_raw / seq_total / len_max / text_field, v1.14 增 tiers /
    frame_gen_schema_declared)——形态约束横跨十余个节, 逐参传递会把签名撑爆。形态关闭
    时调用方不进入本簇: 相关键退化为停放配置, 全系统与 v1.12 字节等价。

    @param col 错误聚合器
    @param fp 报错定位用的 project.toml 路径字符串
    @param gs 已解析的 [generate.stream] 节
    @param v 跨节取值捆包
    """
    _stream_form_premise(col, fp, v)
    _stream_form_probes(col, fp, v)
    _stream_form_quota(col, fp, v)
    _stream_form_packing(col, fp, gs, v)
    _stream_form_weaving(col, fp, gs, v)
    _check_tier_table(col, fp, gs.tiers, v)      # v1.14 档位簇
    _check_time_fields(col, fp, v)               # v1.14 绑定簇


def _stream_form_premise(col: _Collector, fp: str, v: SimpleNamespace) -> None:
    """形态前提合取 + 工件键守卫。

    合取项: generate_only ∧ text ∧ generate.enabled ∧ classify.enabled ∧
    stream.order_by = "meta:<字段>" ∧ output.meta_mode != "none"——缺一即 CONFIG_ERROR,
    报错文案给出形态语义指引。工件键守卫: ts 字段与文本字段不得含点(字面顶层键 vs
    点路径解析, 往返不成立)、互不同名、均不得为 "truth"(工件行三个顶层键互斥)。

    @param col 错误聚合器
    @param fp 报错定位用的 project.toml 路径字符串
    @param v 跨节取值捆包
    """
    loc = f"{fp}:[generate.stream].enabled"
    if v.mode != "generate_only":
        col.error(f'{loc}: the time-stream form requires run.mode = "generate_only", got '
                  f"{_fmt(v.mode)} - this form synthesizes a time stream from scratch and "
                  f"consumes no input data")
    if v.modality != "text":
        col.error(f'{loc}: the time-stream form requires run.modality = "text", got '
                  f"{_fmt(v.modality)} (UI-modality time-stream generation is a v1.13 "
                  f"non-goal)")
    if not v.generate.enabled:
        col.error(f"{loc}: the time-stream form requires generate.enabled = true")
    if not v.classify.enabled:
        col.error(f"{loc}: the time-stream form requires classify.enabled = true - the "
                  f"sequence class table carries the quota and per-class conditioning, and "
                  f"generation-side labels are inherited (zero verdict calls)")
    _stream_form_artifact_keys(col, fp, v)
    if v.meta_mode == "none":
        col.error(f'{fp}:[output].meta_mode: must not be "none" in the time-stream form - '
                  f"frame-class ground truth and member reconciliation are carried only by "
                  f"_meta.stream (sidecar is legal)")


def _stream_form_artifact_keys(col: _Collector, fp: str, v: SimpleNamespace) -> None:
    """工件行三个顶层键(时间戳字段 / 文本字段 / truth)的形状与互斥守卫。

    @param col 错误聚合器
    @param fp 报错定位用的 project.toml 路径字符串
    @param v 跨节取值捆包
    """
    order_by = v.stream.order_by
    if not (order_by.startswith("meta:") and order_by[len("meta:"):]):
        col.error(f'{fp}:[stream].order_by: the time-stream form requires "meta:<field>" '
                  f"(that field is the timestamp key of the artifact row, replayable on the "
                  f"ingest side under the same declaration), got {_fmt(order_by)}")
    elif "." in order_by[len("meta:"):]:
        # 工件行把 ts 字段名当字面顶层键写, 而 M2 按点路径解析——带点的字段名无法往返
        # (重放侧整份判坏行), 本形态定向封死。
        col.error(f'{fp}:[stream].order_by: the timestamp field name of the time-stream '
                  f'form must not contain "." (the artifact row writes it as a literal '
                  f"top-level key and a dotted path cannot round-trip on replay ingest), "
                  f"got {_fmt(order_by)}")
    if "." in v.text_field:
        col.error(f'{fp}:[input].text_field: the text field name of the time-stream form '
                  f'must not contain "." (the artifact row writes it as a literal '
                  f"top-level key and a dotted path cannot round-trip on replay ingest), "
                  f"got {_fmt(v.text_field)}")
    ts_field = order_by[len("meta:"):] if order_by.startswith("meta:") else ""
    # 工件行的三个顶层键(ts 字段、文本字段、truth)互斥——同名即键冲突, 行不成立。
    if ts_field and ts_field == v.text_field:
        col.error(f"{fp}:[input].text_field: must not have the same name as the timestamp "
                  f"field of [stream].order_by in the time-stream form (the two artifact-row "
                  f"keys would collide), got {_fmt(v.text_field)}")
    for owner, field_name in (("[input].text_field", v.text_field),
                              ("[stream].order_by", ts_field)):
        if field_name == "truth":
            col.error(f'{fp}:{owner}: the field name must not be "truth" in the time-stream '
                      f"form (it would collide with the ground-truth key of the artifact row)")


def _stream_form_probes(col: _Collector, fp: str, v: SimpleNamespace) -> None:
    """定向禁设键探针(v1.11 原始节探针机制)。

    [generate] 的四个「另一形态」键、[class.*.generate] 的两个同族键、以及帧粒度两
    开关——本形态下显式书写均为 CONFIG_ERROR, 不走白名单外键的前向兼容 WARN, 报错
    指明替代面。

    @param col 错误聚合器
    @param fp 报错定位用的 project.toml 路径字符串
    @param v 跨节取值捆包
    """
    for key in _STREAM_FORBIDDEN_GEN_KEYS:
        if v.gen_provided.get(key):
            col.error(f"{fp}:[generate].{key}: the time-stream form does not provide this "
                      f"key - sequence quotas are carried by "
                      f"[class.<name>.generate].sequences, sequence length by len_range and "
                      f"noise batching by num_per_call; remove this key")
    for cname, sections in (v.class_raw or {}).items():
        g_over = sections.get("generate") if isinstance(sections, dict) else None
        if not isinstance(g_over, dict):
            continue
        for key in _STREAM_FORBIDDEN_CLASS_GEN_KEYS:
            if key in g_over:
                col.error(f"{fp}:[class.{cname}.generate].{key}: the time-stream form does "
                          f"not provide this key (per-record expansion / seeds per call "
                          f"belong to the flat generation forms); use sequences / len_range "
                          f"instead")
    for name, on in (("frame.classify", v.frame_classify.enabled),
                     ("frame.annotate", v.frame_annotate.enabled)):
        if on:
            col.error(f"{fp}:[{name}].enabled: mutually exclusive with the time-stream form "
                      f"- frame-class ground truth is known at generation time (the "
                      f"blueprint is the truth), so no frame-level verdict is needed; write "
                      f"frame content contracts in [frame.class.<name>.generate]")


def _stream_form_quota(col: _Collector, fp: str, v: SimpleNamespace) -> None:
    """类表与配额约束。

    至少一个序列类的有效 sequences ≥ 1; 参与类(有效 sequences ≥ 1)的有效生成指令
    非空; 帧类表非空(帧类的生成指令必填域见 ``_check_frame_gen_instructions``)。

    @param col 错误聚合器
    @param fp 报错定位用的 project.toml 路径字符串
    @param v 跨节取值捆包
    """
    if v.seq_total < 1:
        col.error(f"{fp}:[class.<name>.generate].sequences: the time-stream form requires "
                  f"at least one sequence class with an effective sequences >= 1 (the "
                  f"global [generate].sequences sets a default that classes may override), "
                  f"got a total of {v.seq_total} across all classes")
    for name, view in v.class_views.items():
        if view.generate.sequences >= 1 and not view.generate.instruction.strip():
            col.error(f"{fp}:[class.{name}.generate].instruction: a participating sequence "
                      f"class (effective sequences = {view.generate.sequences}) must "
                      f"provide a non-empty generation instruction (the global "
                      f"[generate].instruction sets a default)")
    if not v.frame_classify.classes:
        col.error(f"{fp}:[[frame.classify.classes]]: the time-stream form requires a "
                  f"non-empty frame class table (the blueprint picks each step from that "
                  f"closed set; frame.classify.enabled stays false)")
    _check_frame_gen_instructions(col, fp, v)


def _check_frame_gen_instructions(col: _Collector, fp: str, v: SimpleNamespace) -> None:
    """每帧类的 ``[frame.class.<name>.generate].instruction`` 必填(及其检查域)。

    v1.14(裁决·指令必填域收窄): 档位表在场时检查域收窄为 **∪各档 frame_classes**——
    蓝图 enum 只在档内子集上取值, 未入档的帧类永不被选中(另有一条 WARN 点名其生成面
    整体为死配置), 逼用户为它写死指令违反"禁止多此一举的配置"纪律。

    @param col 错误聚合器
    @param fp 报错定位用的 project.toml 路径字符串
    @param v 跨节取值捆包
    """
    if v.tiers:
        domain = {name for spec in v.tiers for name in spec.frame_classes}
        reason = ("the blueprint enum covers the union of the tier compositions, so any "
                  "frame class of a tier may be picked")
    else:
        domain = {spec.name for spec in v.frame_classify.classes}
        reason = "the blueprint enum covers the whole table, so any frame class may be picked"
    for spec in v.frame_classify.classes:
        if spec.name not in domain:
            continue
        view = v.frame_class_views.get(spec.name)
        if view is None or not (view.gen_instruction or "").strip():
            col.error(f"{fp}:[frame.class.{spec.name}.generate].instruction: every frame "
                      f"class must provide a non-empty generation instruction ({reason}), "
                      f"expected a non-empty string")


def _stream_form_packing(col: _Collector, fp: str, gs: GenerateStreamConfig,
                         v: SimpleNamespace) -> None:
    """装箱一致性约束。

    sessions ≥ 1 ∧ sessions ≤ Σsequences ≤ 2 × sessions(交叉并发度恒 k ∈ {1, 2},
    交叉会话数 = Σsequences − sessions); duplicates ∈ [0, Σsequences];
    noise_ratio ∈ [0,1) 且 > 0 时 noise_instruction 必填; frame_gap_s 下界 ≥ 微秒地板
    (v1.14 裁决·微秒地板)且上界 < stream.gap_s。

    @param col 错误聚合器
    @param fp 报错定位用的 project.toml 路径字符串
    @param gs 已解析的 [generate.stream] 节
    @param v 跨节取值捆包
    """
    total = v.seq_total
    if gs.sessions < 1:
        col.error(f"{fp}:[generate.stream].sessions: expected an integer >= 1 (number of "
                  f"sessions), got {gs.sessions}")
    elif not gs.sessions <= total <= 2 * gs.sessions:
        col.error(f"{fp}:[generate.stream].sessions: expected sessions <= Σsequences <= "
                  f"2 * sessions (crossed sessions = Σsequences - sessions, crossing "
                  f"concurrency is always k in 1,2), got sessions = {gs.sessions}, "
                  f"Σsequences = {total}")
    if gs.duplicates > total:
        col.error(f"{fp}:[generate.stream].duplicates: expected an integer in "
                  f"[0, Σsequences] (re-sent sequences are drawn from the surviving ones), "
                  f"got {gs.duplicates}, Σsequences = {total}")
    if not 0 <= gs.noise_ratio < 1:
        col.error(f"{fp}:[generate.stream].noise_ratio: expected a number in [0,1) (noise "
                  f"frames / task frames ratio), got {_fmt(gs.noise_ratio)}")
    elif gs.noise_ratio > 0 and not gs.noise_instruction.strip():
        col.error(f"{fp}:[generate.stream].noise_instruction: required when "
                  f"noise_ratio > 0, expected a non-empty string (the noise-frame "
                  f"generation instruction)")
    if gs.frame_gap_s[0] < _FRAME_GAP_FLOOR_S:
        col.error(f"{fp}:[generate.stream].frame_gap_s: the lower bound must be >= "
                  f"{_FRAME_GAP_FLOOR_S:g} s (one microsecond) - the laid-out timestamps "
                  f"must be strictly increasing and a sub-microsecond gap rounds to a zero "
                  f"timedelta, and the time vocabulary uses 0.0 as its first/last frame "
                  f"boundary sentinel, got lower bound {_fmt(gs.frame_gap_s[0])}")
    if gs.frame_gap_s[1] >= v.stream.gap_s:
        col.error(f"{fp}:[generate.stream].frame_gap_s: the upper bound must be < "
                  f"stream.gap_s (= {v.stream.gap_s}; otherwise the in-session frame gap "
                  f"itself would trigger a session split), got upper bound "
                  f"{_fmt(gs.frame_gap_s[1])}")


def _stream_form_weaving(col: _Collector, fp: str, gs: GenerateStreamConfig,
                         v: SimpleNamespace) -> None:
    """织造上限与铺设契约约束。

    2 × max(各类 len_range 上界) ≤ stream.session_max_len(交叉会话恒装两条序列);
    stream.key 须为空数组、stream.gap_steps 须为 0(分区键与序差断开同生成侧的铺设
    契约冲突); session_max_span_s > 0 时按最坏帧间隔做静态跨度校验; ts_start 须可
    解析为 ISO-8601 时刻。

    @param col 错误聚合器
    @param fp 报错定位用的 project.toml 路径字符串
    @param gs 已解析的 [generate.stream] 节
    @param v 跨节取值捆包
    """
    if 2 * v.len_max > v.stream.session_max_len:
        col.error(f"{fp}:[stream].session_max_len: the time-stream form requires >= 2 * "
                  f"max(len_range upper bound) (a crossed session always packs two "
                  f"sequences), got {v.stream.session_max_len} < {2 * v.len_max}")
    if v.stream.key:
        col.error(f"{fp}:[stream].key: the time-stream form requires an empty array - "
                  f"sessions are laid out directly by the weaver and partition keys do not "
                  f"participate, got {_fmt(list(v.stream.key))}")
    if v.stream.gap_steps:
        col.error(f"{fp}:[stream].gap_steps: the time-stream form requires 0 - session "
                  f"boundaries are laid out directly by the weaver (inter-session gaps are "
                  f"always > gap_s) and step-gap splitting does not participate, got "
                  f"{v.stream.gap_steps}")
    span = v.stream.session_max_span_s
    worst = (v.stream.session_max_len - 1) * gs.frame_gap_s[1]
    if span > 0 and worst > span:
        col.error(f"{fp}:[stream].session_max_span_s: worst-case session span "
                  f"(session_max_len - 1) * frame_gap_s upper bound = {worst:g} s > "
                  f"{span} s - the laid-out sessions would be hard-cut by span on the "
                  f"ingest side; raise session_max_span_s, lower the frame_gap_s upper "
                  f"bound or lower session_max_len")
    try:
        datetime.fromisoformat(gs.ts_start)
    except ValueError:
        col.error(f'{fp}:[generate.stream].ts_start: expected a parseable ISO-8601 instant '
                  f'(e.g. "2026-01-01T09:00:00+08:00"; a missing timezone is treated as '
                  f"UTC, matching the meta:<field> ingest rule), got {_fmt(gs.ts_start)}")


# ── v1.14 档位面(SPEC-generation-tiers §3.1 档位表三行 + 两条 WARN) ───────────


def _check_tier_table(col: _Collector, fp: str, tiers: tuple[TierSpec, ...],
                      v: SimpleNamespace) -> None:
    """v1.14 档位簇驱动器: 身份 → 构成 → 逐非零配额对 → 未入档 WARN。

    档位表缺省 ⇒ 零执行(档位面整体不在场, 与 v1.13 字节等价)。

    @param col 错误聚合器
    @param fp 报错定位用的 project.toml 路径字符串
    @param tiers 已按 tier_rank 升序解析的档位表
    @param v 跨节取值捆包
    """
    if not tiers:
        return
    frame_names = tuple(spec.name for spec in v.frame_classify.classes)
    _check_tier_identity(col, fp, tiers)
    _check_tier_composition(col, fp, tiers, frame_names)
    _check_tier_quota_pairs(col, fp, tiers, v.class_views)
    _warn_frame_classes_without_tier(col, fp, tiers, frame_names)


def _check_tier_identity(col: _Collector, fp: str, tiers: tuple[TierSpec, ...]) -> None:
    """档位身份(裁决·tier_rank 即档位身份): 表内唯一且连续覆盖 1..N。

    正整数与 ``weight >= 1`` 已在解析期强制; 此处只裁定全表形状——缺号/重号都会让
    "第几档"失去身份语义(它同时是配分平票依据与类内序数分块依据)。

    @param col 错误聚合器
    @param fp 报错定位用的 project.toml 路径字符串
    @param tiers 档位表
    """
    ranks = sorted(spec.tier_rank for spec in tiers)
    if ranks != list(range(1, len(ranks) + 1)):
        col.error(f"{fp}:[[generate.stream.tiers]].tier_rank: tier ranks must be unique and "
                  f"cover 1..N contiguously (N = {len(ranks)} = the number of tiers; the "
                  f"rank is the identity of a tier, there is no name key), "
                  f"got {_fmt(ranks)}")


def _check_tier_composition(col: _Collector, fp: str, tiers: tuple[TierSpec, ...],
                            frame_names: tuple[str, ...]) -> None:
    """档位构成(裁决·构成恰等): 非空、档内互异、名 ∈ 帧类表、各档构成两两互异。

    定位按 tier_rank 而非下标(档位身份即 tier_rank, 且存放序已按 rank 重排)。

    @param col 错误聚合器
    @param fp 报错定位用的 project.toml 路径字符串
    @param tiers 档位表
    @param frame_names 帧类表的名集(声明序)
    """
    owners: dict[tuple[str, ...], int] = {}
    for spec in tiers:
        loc = (f"{fp}:[[generate.stream.tiers]](tier_rank = {spec.tier_rank})"
               f".frame_classes")
        if not spec.frame_classes:
            col.error(f"{loc}: expected a non-empty array of frame class names (a tier IS "
                      f"its frame-class composition)")
            continue
        for i, name in enumerate(spec.frame_classes):
            if name in spec.frame_classes[:i]:
                col.error(f"{loc}: frame class names must be distinct within a tier (the "
                          f"composition is a set), got duplicate {_fmt(name)}")
            elif name not in frame_names:
                col.error(f"{loc}: frame class name {_fmt(name)} is not in "
                          f"[[frame.classify.classes]], available: {_avail(frame_names)}")
        key = tuple(sorted(set(spec.frame_classes)))
        if key in owners:
            col.error(f"{loc}: the composition is identical to the one of tier_rank = "
                      f"{owners[key]} - two tiers with the same frame-class set are "
                      f"semantically duplicates, got {_fmt(list(spec.frame_classes))}")
        else:
            owners[key] = spec.tier_rank


def _check_tier_quota_pairs(col: _Collector, fp: str, tiers: tuple[TierSpec, ...],
                            class_views: dict) -> None:
    """长度可覆盖 + 配分零额告警: 逐 (参与类, 档) 配额对裁定。

    配分是 ``(sequences, tiers)`` 的纯函数, M1 期可算(裁决·零抽签配分)。配额 >= 1 的
    每一对须满足该类 ``len_range`` 下界 >= 该档构成大小(构成恰等要求每类至少出现一
    次); **零额对豁免**——不为永不尝试的组合抬高下界, 与零额 WARN 语义对齐。

    @param col 错误聚合器
    @param fp 报错定位用的 project.toml 路径字符串
    @param tiers 档位表
    @param class_views 序列类视图表(承载有效 sequences 与 len_range)
    """
    weights = ", ".join(f"tier_rank {spec.tier_rank}: weight {spec.weight}"
                        for spec in tiers)
    for cname, view in class_views.items():
        if view.generate.sequences < 1:
            continue        # 不参与生成的类没有配额对
        for spec, quota in zip(tiers, apportion_tiers(view.generate.sequences, tiers)):
            if quota < 1:
                col.warn(f"{fp}:[[generate.stream.tiers]]: class {_fmt(cname)} apportions 0 "
                         f"sequences to tier_rank = {spec.tier_rank} (largest-remainder "
                         f"apportionment of {view.generate.sequences} sequences over "
                         f"weights {weights}), so that tier is never exercised for this "
                         f"class - raise sequences or rebalance the weights")
            elif view.generate.len_range[0] < len(spec.frame_classes):
                col.error(f"{fp}:[class.{cname}.generate].len_range: the lower bound must be "
                          f">= the composition size of every tier this class draws from "
                          f"(tier_rank = {spec.tier_rank} declares "
                          f"{len(spec.frame_classes)} frame classes and is apportioned "
                          f"{quota} of the {view.generate.sequences} sequences, and each of "
                          f"them must appear at least once), got lower bound "
                          f"{view.generate.len_range[0]}")


def _warn_frame_classes_without_tier(col: _Collector, fp: str, tiers: tuple[TierSpec, ...],
                                     frame_names: tuple[str, ...]) -> None:
    """帧类未入档: 该帧类不会出现在任何蓝图中, 其生成面整体是死配置(WARN)。

    @param col 错误聚合器
    @param fp 报错定位用的 project.toml 路径字符串
    @param tiers 档位表
    @param frame_names 帧类表的名集(声明序)
    """
    covered = {name for spec in tiers for name in spec.frame_classes}
    for name in frame_names:
        if name not in covered:
            col.warn(f"{fp}:[frame.class.{name}.generate]: frame class {_fmt(name)} is in "
                     f"no tier composition, so it can never be picked by a blueprint - its "
                     f"whole generate face (instruction, schema, time_fields) is dead "
                     f"config (the generation instruction is not required for it either)")


def _check_tiers_parked(ctx: _LoadCtx) -> None:
    """档位表前提(v1.11 原始节探针机制): 档位表**仅**时间流形态合法。

    在场性取自原始节探针而非解析产物——表内容非法(解析产物为空)时也要照发。

    @param ctx 校验上下文
    """
    if ctx.p.gen_provided.get("stream_tiers"):
        ctx.col.error(f"{ctx.fp}:[[generate.stream.tiers]]: the tier table is only legal in "
                      f"the time-stream generation form ([generate.stream].enabled = true) "
                      f"- a tier declares the frame-class composition of the sequences "
                      f"drawn from it, and only that form plans sequences from a frame "
                      f"class table")


# ── v1.14 时间字段绑定面(SPEC-generation-tiers §3.1 绑定表三行) ───────────────


def _check_time_fields(col: _Collector, fp: str, v: SimpleNamespace) -> None:
    """v1.14 绑定簇驱动器(裁决·绑定即剔除): 前提 → 键与类型 → 剔除余量。

    绑定表仅结构化帧合法: 回填就地写入共享载荷对象, 要求载荷恒为 JSON 对象(生成
    Schema 顶层 ``"type"`` 字面恰等 ``"object"`` 由 Schema 装载期强制, 联合类型与缺失
    都在那里就地报错并使该帧类退化为"Schema 不可用"——此处不叠加误导性第二错)。

    @param col 错误聚合器
    @param fp 报错定位用的 project.toml 路径字符串
    @param v 跨节取值捆包
    """
    for name, view in v.frame_class_views.items():
        if view.time_fields is None:
            continue
        loc = f"{fp}:[frame.class.{name}.generate.time_fields]"
        if view.gen_schema is None:
            if name not in v.frame_gen_schema_declared:
                col.error(f"{loc}: a time-field binding is only legal on a structured frame "
                          f"class - declare schema_path / schema_inline for "
                          f"[frame.class.{name}.generate] first (the backfill writes the "
                          f"computed value into the frame payload in place, so the payload "
                          f"must always be a JSON object; a plain-text frame has no field "
                          f"to bind)")
            continue        # 已声明但装载失败: 病因与报错都属该 Schema 自身
        props = view.gen_schema.get("properties")
        props = props if isinstance(props, dict) else {}
        _check_binding_pairs(col, loc, view.time_fields, props)
        # 剔除余量: 绑定字段整体从逐位 Schema 中剔除, 至少得给 LLM 剩一个字段可生成。
        bound = sum(1 for key in view.time_fields if key in props)
        if len(props) - bound < 1:
            col.error(f"{loc}: the bindings would remove every top-level property of the "
                      f"frame-class generation schema (top-level properties: {len(props)}, "
                      f"bound: {bound}) - a bound field is stripped from the per-position "
                      f"schema, so leave at least one property for the LLM to generate")


def _check_binding_pairs(col: _Collector, loc: str, bindings: dict, props: dict) -> None:
    """逐个绑定对: 键 ∈ 顶层 properties、值 ∈ 语义词表、属性 ``type`` 字面恰等。

    字面恰等意味着联合类型数组、缺失的 ``type`` 与经 ``$ref``/组合关键字的间接声明
    一律判不匹配(CONFIG_ERROR)——类型层满足是工具对用户完整生成 Schema 的唯一静态
    保证; ``type`` 以外的关键字既不上行也不被强制(字段整体从 LLM 面向的逐位 Schema
    中剔除), 逐个发一条值-free WARN。

    @param col 错误聚合器
    @param loc 该帧类绑定表的报错定位前缀
    @param bindings 该帧类的绑定映射(字段名 → 语义词)
    @param props 该帧类生成 Schema 的顶层 ``properties``
    """
    for key, term in bindings.items():
        want = _TIME_FIELD_TERMS.get(term)
        prop = props.get(key)
        declared = prop.get("type") if isinstance(prop, dict) else prop
        if want is None:
            col.error(f"{loc}.{key}: expected one of the time vocabulary terms "
                      f"{_avail(tuple(_TIME_FIELD_TERMS))} (a frozen closed set), "
                      f"got {_fmt(term)}")
        elif key not in props:
            col.error(f"{loc}.{key}: {_fmt(key)} is not a top-level property of the "
                      f"frame-class generation schema, available: {_avail(tuple(props))}")
        elif declared != want:
            col.error(f'{loc}.{key}: the bound property must declare "type": '
                      f'{json.dumps(want)} literally for the term {_fmt(term)} (a union '
                      f"type array, a missing type and an indirect declaration through "
                      f"$ref or a combining keyword all count as a mismatch), got "
                      f"{_fmt(declared)}")
        else:
            _warn_binding_extra_keywords(col, loc, key, prop)


def _warn_binding_extra_keywords(col: _Collector, loc: str, key: str,
                                 prop: dict) -> None:
    """绑定字段上 ``type`` 以外的关键字: 逐个一条值-free WARN(帧类名 + 字段名 + 关键字名)。

    @param col 错误聚合器
    @param loc 该帧类绑定表的报错定位前缀
    @param key 绑定的字段名
    @param prop 该字段的属性 Schema
    """
    for keyword in prop:
        if keyword != "type":
            col.warn(f"{loc}.{key}: the bound field carries the keyword {_fmt(keyword)}, "
                     f"which is neither sent to the LLM nor enforced - a bound field is "
                     f"removed from the per-position schema and its value is computed from "
                     f"the laid-out timeline (only the declared type is guaranteed)")


def _frame_gen_schema_declared(raw: object) -> frozenset[str]:
    """哪些帧类**声明过**生成 Schema 源键(与"装载是否成功"无关)——绑定表前提据此区分
    "纯文本帧带绑定表"(定向 CONFIG_ERROR)与"Schema 自身装载失败"(不叠加第二条)。

    @param raw ``[frame.class.<name>.*]`` 原始节
    @return 声明过 ``schema_path``/``schema_inline`` 的帧类名集合
    """
    if not isinstance(raw, dict):
        return frozenset()
    return frozenset(
        cname for cname, sections in raw.items()
        if isinstance(sections, dict) and isinstance(sections.get("generate"), dict)
        and any(k in sections["generate"] for k in ("schema_path", "schema_inline")))


def _check_generate_stream_form(ctx: _LoadCtx, products: _Products) -> tuple[int, int]:
    """v1.13 形态约束簇的入口: 组装跨节取值捆包并一次性裁定。

    类视图与帧类视图都已物化后才调用; 形态关闭 ⇒ 除 v1.14 档位表前提外零执行、零行为
    差异。Σsequences / max(len_range 上界)取自按类生效视图。

    @param ctx 校验上下文
    @param products 产物累加器
    @return (Σsequences, max(len_range 上界)) —— 后续预算静态检查复用
    """
    views = products.class_views
    seq_total = sum(cv.generate.sequences for cv in views.values())
    len_max = max([1] + [cv.generate.len_range[1] for cv in views.values()])
    p = ctx.p
    if not p.generate_stream.enabled:
        _check_tiers_parked(ctx)        # v1.14 档位表前提(形态关闭侧的唯一一条)
        return seq_total, len_max
    _check_generate_stream(ctx.col, ctx.fp, p.generate_stream, SimpleNamespace(
        mode=ctx.mode, modality=ctx.modality, generate=p.generate,
        classify=p.classify, class_views=views, stream=p.stream,
        meta_mode=p.output.meta_mode, frame_classify=p.frame_classify,
        frame_annotate=p.frame_annotate, frame_class_views=products.frame_class_views,
        gen_provided=p.gen_provided,
        class_raw=p.class_raw if isinstance(p.class_raw, dict) else {},
        seq_total=seq_total, len_max=len_max, text_field=p.input.text_field,
        tiers=p.generate_stream.tiers,                                  # v1.14 档位面
        frame_gen_schema_declared=_frame_gen_schema_declared(p.frame_class_raw)))
    return seq_total, len_max


# ── v1.11 — 上下文预算与 vision 推导(spec 3.1.4 上下文预算行) ────────────────


def _check_removed_use_vision(ctx: _LoadCtx) -> None:
    """V2(V27② 原始节探针): 已移除的键给出**定向**错误与迁移指引, 而非未知键 WARN。

    @param ctx 校验上下文
    """
    if ctx.p.segment_provided["use_vision"]:
        ctx.col.error(f"{ctx.fp}:[segment].use_vision: segment.use_vision was removed in "
                      f"v1.11: whether a window carries images is derived automatically "
                      f"from supports_vision of the profile named by segment.llm; point "
                      f"segment.llm at a text-only profile for text-only judgments (V2)")


def _freeze_vision(ctx: _LoadCtx) -> _LoadCtx:
    """V1 与 v1.12: 冻结 segment / frame.classify 两个 ``vision_resolved`` 解析产物。

    (mode_resolved 先例——下游每个消费者与最终 ResolvedConfig 读的都是冻结值。)

    @param ctx 校验上下文
    @return 冻结后的上下文
    """
    p = ctx.p
    prof_seg = ctx.llm_profiles.get(p.segment.llm)
    segment = replace(p.segment, vision_resolved=(
        ctx.modality == "ui" and p.segment.enabled
        and p.segment.strategy in ("llm", "hybrid")
        and prof_seg is not None and prof_seg.supports_vision))
    prof_fc = ctx.llm_profiles.get(p.frame_classify.llm)
    frame_classify = replace(p.frame_classify, vision_resolved=(
        ctx.modality == "ui" and p.frame_classify.enabled
        and prof_fc is not None and prof_fc.supports_vision))
    return replace(ctx, p=replace(p, segment=segment, frame_classify=frame_classify))


def _warn_segment_image_limit(ctx: _LoadCtx) -> None:
    """V5(S28 姊妹条): segment 多图窗口触及 Anthropic 的 ">20 图 ∧ 任一边 >2000px" 硬拒域。

    默认 window = 20 恰好卡在边界内侧——不动配置就永不触发。

    @param ctx 校验上下文
    """
    p = ctx.p
    prof_seg = ctx.llm_profiles.get(p.segment.llm)
    if (p.segment.vision_resolved and p.segment.window > 20
            and prof_seg is not None and prof_seg.max_image_px > 2000):
        ctx.col.warn(f"{ctx.fp}:[segment].window: window = {p.segment.window} > 20 with "
                     f"vision_resolved in effect and the profile referenced by segment "
                     f"[llm.{p.segment.llm}] has max_image_px = {prof_seg.max_image_px} > "
                     f"2000 - Anthropic hard-rejects >20-image requests carrying any image "
                     f"with an edge > 2000px (400, not auto-downscaled); set max_image_px "
                     f"<= 2000 or lower window back to <= 20 (V5)")


def _warn_undeclared_windows(ctx: _LoadCtx, products: _Products) -> None:
    """V6: 被启用阶段引用却未声明上下文窗口的 profile 各出一条 WARN(非阻断, 带声明建议)。

    @param ctx 校验上下文
    @param products 产物累加器(读取引用集)
    """
    for name in sorted(products.referenced):
        prof_r = ctx.llm_profiles.get(name)
        if prof_r is not None and prof_r.context_window == 0:
            ctx.col.warn(f"{ctx.fc}:[llm.{name}].context_window: referenced by an enabled "
                         f"stage but not declared (0 = context budget off for this profile) "
                         f"- declare the deployment-effective window (e.g. context_window = "
                         f"131072; under-declaring is always safe, it only trims more and "
                         f"never overflows, V6/V26)")
    dedup = ctx.p.dedup
    if dedup.semantic and dedup.semantic_embedding in ctx.embedding_profiles:
        prof_e = ctx.embedding_profiles[dedup.semantic_embedding]
        if prof_e.context_window == 0:
            ctx.col.warn(f"{ctx.fc}:[embedding.{prof_e.name}].context_window: referenced by "
                         f"an enabled stage but not declared (0 = embedding budget off for "
                         f"this profile) - declare the deployment-effective window "
                         f"(under-declaring is always safe, V6/V15)")


def _check_min_window(ctx: _LoadCtx) -> None:
    """V9 静态护栏: 声明预算下, segment 的最坏保证装填量须容得下地板帧数。

    verify repair 下地板为 3(固定的三帧成员复裁窗, F14: policy="drop" 不建复裁窗,
    地板保持 2)。

    @param ctx 校验上下文
    """
    p = ctx.p
    prof_seg = ctx.llm_profiles.get(p.segment.llm)
    if not (p.segment.enabled and p.segment.strategy in ("llm", "hybrid")
            and prof_seg is not None and prof_seg.context_window > 0):
        return
    w_min = budget.min_window(
        SimpleNamespace(segment=p.segment, llm_profiles=ctx.llm_profiles))
    floor = 3 if (p.verify.enabled and p.verify.policy == "repair"
                  and p.segment.enabled) else 2
    if w_min < floor:
        ctx.col.error(f"{ctx.fp}:[segment].window: worst-case guaranteed packing size "
                      f"w_min = {w_min} < floor = {floor} (profile [llm.{p.segment.llm}], "
                      f"context_window = {prof_seg.context_window}) - any frame must "
                      f"statically fit into a {floor}-frame window (the verify repair "
                      f"re-judgment window is always 3 frames); raise context_window, lower "
                      f"segment.digest_max_chars or switch profile (V9)")
    elif w_min == floor:
        ctx.col.warn(f"{ctx.fp}:[segment].window: worst-case guaranteed packing size "
                     f"w_min = {w_min} == floor - degenerate shape: every frame is a seam "
                     f"and every frame is judged twice, so the window count explodes (a "
                     f"full 200-frame session takes up to 199 windows, roughly 18x the call "
                     f"volume of the default 20-frame shape, V9)")


def _rubric_est(rub: Rubric, mode: str) -> int:
    """估算一份准则渲染进提示后的 token 数。

    @param rub 生效准则
    @param mode 打分模式("pairwise" | "pointwise"; 后者多渲染等级文本)
    @return token 估算值
    """
    return budget.est_text("\n".join(
        f"{c.key}\n{c.description}\n{c.pairwise_prompt}"
        + ("\n" + "\n".join(c.pointwise_levels) if mode == "pointwise" else "")
        for c in rub.criteria))


def _fewshot_est(examples: tuple[FewShotExample, ...]) -> int:
    """估算一组 few-shot 示例渲染进提示后的 token 数。

    @param examples 示例集
    @return token 估算值
    """
    return budget.est_text("\n".join(
        f"{ex.input}\n{json.dumps(ex.output, ensure_ascii=False)}" for ex in examples))


def _class_schema_est(view: ClassView, schema_text: str) -> int:
    """估算一个类视图的**有效**标注 Schema token 数(未声明按类 Schema 则回落全局)。

    @param view 类视图
    @param schema_text 全局用户 Schema 的 JSON 文本
    @return token 估算值
    """
    if view.schema is None:
        return budget.est_text(schema_text)
    return budget.est_text(json.dumps(view.schema, ensure_ascii=False))


def _static_checks_prefix(ctx: _LoadCtx,
                          views: tuple[ClassView, ...]) -> list[tuple[str, tuple, int]]:
    """V13③ 静态部件: segment / stitch / classify / extract 四段。

    @param ctx 校验上下文
    @param views 全部类视图
    @return (阶段名, profile 名元组, 静态估算)三元组列表
    """
    p = ctx.p
    checks: list[tuple[str, tuple, int]] = []
    if p.segment.enabled and p.segment.strategy in ("llm", "hybrid"):
        checks.append(("segment", (p.segment.llm,),
                       budget.TEMPLATE_HEAD_TOKENS["segment"]
                       + budget.est_text(p.segment.context)))
    if p.stitch.enabled:
        checks.append(("stitch", (p.stitch.llm,),
                       budget.TEMPLATE_HEAD_TOKENS["stitch"]
                       + budget.est_text(p.stitch.context)))
    if p.classify.enabled:
        class_table_text = "\n".join(f"{c.name}\n{c.description}\n" + "\n".join(c.examples)
                                     for c in p.classify.classes)
        checks.append(("classify", (p.classify.llm,),
                       budget.TEMPLATE_HEAD_TOKENS["classify"]
                       + budget.est_text(p.classify.instruction)
                       + budget.est_text(class_table_text)))
    if p.extract.enabled:
        checks.append(("extract", (p.extract.llm,),
                       budget.TEMPLATE_HEAD_TOKENS["extract"]
                       + max([budget.est_text(p.extract.instruction)]
                             + [budget.est_text(v.extract.instruction) for v in views])))
    return checks


def _static_checks_scoring(ctx: _LoadCtx, views: tuple[ClassView, ...],
                           products: _Products) -> list[tuple[str, tuple, int]]:
    """V13③ 静态部件: quality 与 annotate 两段(逐池取最大)。

    v1.13: annotate 的 schema 项现在是按类的(裁决·按类标注 Schema)——最大值跑在**整
    池之和**(schema + instruction + few-shot)上; 未声明按类 Schema 时每个视图都解析到
    全局那份, 取值与 v1.12 逐字节一致。

    @param ctx 校验上下文
    @param views 全部类视图
    @param products 产物累加器(取全局准则与用户 Schema)
    @return (阶段名, profile 名元组, 静态估算)三元组列表
    """
    p = ctx.p
    schema_text = (json.dumps(products.user_schema, ensure_ascii=False)
                   if products.user_schema else "")
    checks: list[tuple[str, tuple, int]] = []
    if p.quality.enabled:
        judges_active = bool(p.quality.judges) and p.quality.mode == "pairwise"
        q_profiles = p.quality.judges if judges_active else (p.quality.llm,)
        checks.append(("quality", tuple(q_profiles),
                       budget.TEMPLATE_HEAD_TOKENS["quality"]
                       + max([_rubric_est(products.rubric, p.quality.mode)]
                             + [_rubric_est(v.rubric, v.quality.mode) for v in views])))
    if p.annotate.enabled:
        checks.append(("annotate", (p.annotate.llm,),
                       budget.TEMPLATE_HEAD_TOKENS["annotate"]
                       + max([budget.est_text(schema_text)
                              + budget.est_text(p.annotate.instruction)
                              + _fewshot_est(p.annotate.examples)]
                             + [_class_schema_est(v, schema_text)
                                + budget.est_text(v.annotate.instruction)
                                + _fewshot_est(v.annotate.examples) for v in views])))
    return checks


def _static_checks_generate(ctx: _LoadCtx, products: _Products,
                            len_max: int) -> list[tuple[str, tuple, int]]:
    """V13③ 静态部件: generate 段与 v1.13 的蓝图 / 帧实现两段(裁决·预算头两键)。

    蓝图调用 = 冻结模板头 + 类有效 instruction + 全帧类表; 帧实现调用 = 冻结模板头 +
    类有效 instruction + 最坏 L_max × max(帧类生成 Schema)(逐位契约把 Schema 文本重复
    L 次)。噪音批量实现复用 generate 段。

    @param ctx 校验上下文
    @param products 产物累加器
    @param len_max 各类 len_range 上界的最大值
    @return (阶段名, profile 名元组, 静态估算)三元组列表
    """
    p = ctx.p
    views = tuple(products.class_views.values())
    gen_instruction_est = max([budget.est_text(p.generate.instruction)]
                              + [budget.est_text(v.generate.instruction) for v in views])
    checks: list[tuple[str, tuple, int]] = []
    if p.generate.enabled:
        checks.append(("generate", tuple(p.generate.llms),
                       budget.TEMPLATE_HEAD_TOKENS["generate"] + gen_instruction_est))
    if not p.generate_stream.enabled:
        return checks
    frame_gen_table_text = "\n".join(f"{c.name}\n{c.description}"
                                     for c in p.frame_classify.classes)
    gen_schema_max = max([0] + [
        budget.est_text(json.dumps(fv.gen_schema, ensure_ascii=False))
        for fv in products.frame_class_views.values() if fv.gen_schema])
    checks.append(("generate.stream.plan", tuple(p.generate.llms),
                   budget.TEMPLATE_HEAD_TOKENS["generate_plan"] + gen_instruction_est
                   + budget.est_text(frame_gen_table_text)))
    checks.append(("generate.stream.realize", tuple(p.generate.llms),
                   budget.TEMPLATE_HEAD_TOKENS["generate_realize"] + gen_instruction_est
                   + len_max * gen_schema_max))
    return checks


def _static_checks_tail(ctx: _LoadCtx, products: _Products) -> list[tuple[str, tuple, int]]:
    """V13③ 静态部件: verify 段与 v1.12 的帧级分类 / 帧级标注两段。

    帧级分类的静态部件口径与渲染事实对齐: 帧模板不渲染类别示例(§10.12), examples 不
    计入——多算会误触发 V13③ 启动预检。

    @param ctx 校验上下文
    @param products 产物累加器
    @return (阶段名, profile 名元组, 静态估算)三元组列表
    """
    p = ctx.p
    views = tuple(products.class_views.values())
    checks: list[tuple[str, tuple, int]] = []
    if p.verify.enabled:
        v_profiles = p.verify.judges if p.verify.judges else (p.verify.llm,)
        checks.append(("verify", tuple(v_profiles),
                       budget.TEMPLATE_HEAD_TOKENS["verify"]
                       + max([budget.est_text(p.verify.extra_criteria)
                              + budget.est_text(p.annotate.instruction)]
                             + [budget.est_text(v.verify.extra_criteria)
                                + budget.est_text(v.annotate.instruction) for v in views])))
    if p.frame_classify.enabled:
        frame_table_text = "\n".join(f"{c.name}\n{c.description}"
                                     for c in p.frame_classify.classes)
        checks.append(("frame.classify", (p.frame_classify.llm,),
                       budget.TEMPLATE_HEAD_TOKENS["frame_classify"]
                       + budget.est_text(frame_table_text)))
    if p.frame_annotate.enabled:
        frame_schema_text = (json.dumps(products.frame_schema, ensure_ascii=False)
                             if products.frame_schema else "")
        checks.append(("frame.annotate", (p.frame_annotate.llm,),
                       budget.TEMPLATE_HEAD_TOKENS["frame_annotate"]
                       + budget.est_text(frame_schema_text)
                       + max([budget.est_text(p.frame_annotate.instruction)
                              + _fewshot_est(p.frame_annotate.examples)]
                             + [budget.est_text(v.instruction) + _fewshot_est(v.examples)
                                for v in products.frame_class_views.values()])))
    return checks


def _check_static_budget(ctx: _LoadCtx, products: _Products, len_max: int) -> None:
    """V13③ 静态系统侧预检: 不可裁剪的提示部件必须给每条记录留出空间。

    对每个启用阶段在已声明预算的 profile 上, 冻结模板头 + instruction/rubric/类表/
    schema/few-shot 之和若 ≥ 输入预算, 就是数学上必然失败(CONFIG_ERROR); 超过 50%
    则把单记录份额腰斩(WARN, A5)。按类覆盖能逐池换掉 instruction/rubric/few-shot/
    extra_criteria, 故每个阶段的静态和取"全局视图与各类视图的最大值"——否则超大的按类
    面会溜过启动期, 在运行期让该池每条记录都失败。

    @param ctx 校验上下文
    @param products 产物累加器
    @param len_max 各类 len_range 上界的最大值
    """
    views = tuple(products.class_views.values())
    static_checks = (_static_checks_prefix(ctx, views)
                     + _static_checks_scoring(ctx, views, products)
                     + _static_checks_generate(ctx, products, len_max)
                     + _static_checks_tail(ctx, products))
    for sect, prof_names, est_static in static_checks:
        for name in prof_names:
            prof_s = ctx.llm_profiles.get(name)
            if prof_s is None or prof_s.context_window <= 0:
                continue
            ib = budget.input_budget(prof_s)
            if est_static >= ib:
                ctx.col.error(f"{ctx.fp}:[{sect}]: static system-side prompt parts estimated "
                              f"at {est_static} tokens >= the input budget of {ib} tokens "
                              f"(profile [llm.{name}], context_window = "
                              f"{prof_s.context_window}) - no record can fit (V13③); trim "
                              f"instruction/rubric/class table/schema/few-shot or switch to "
                              f"a profile with a larger window")
            elif est_static * 2 > ib:
                ctx.col.warn(f"{ctx.fp}:[{sect}]: static system-side prompt parts estimated "
                             f"at {est_static} tokens exceed 50% of the input budget of "
                             f"{ib} tokens (profile [llm.{name}]) - less than half the space "
                             f"is left per record, which may degrade quality (V13③)")


def _warn_stitch_card_pool(ctx: _LoadCtx) -> None:
    """v1.11 缝合卡池最坏预检(spec 3.16.5 上下文预算行)。

    缝合判定提示是静态有界的——≤ max_open + 1 张卡, 每张带**两段**受 stitch
    .digest_max_chars 约束的帧摘要(首帧/尾帧摘要, §10.11 卡结构)——因此运行期没有裁剪
    余地; 于是 M1 在最坏估算装不进输入预算时告警。**绝不**自动缩 max_open(改语义属于
    用户: 调大 context_window / 调小 digest_max_chars / 调小 max_open); 运行期兜底是
    M9 的入口检查加 on_error="keep"。

    @param ctx 校验上下文
    """
    p = ctx.p
    prof_st = ctx.llm_profiles.get(p.stitch.llm) if p.stitch.enabled else None
    if prof_st is None or prof_st.context_window <= 0:
        return
    card_worst = 2 * budget.est_text("\u597d" * p.stitch.digest_max_chars)
    stitch_worst = (budget.TEMPLATE_HEAD_TOKENS["stitch"]
                    + budget.est_text(p.stitch.context)
                    + (p.stitch.max_open + 1) * card_worst)
    ib_st = budget.input_budget(prof_st)
    if stitch_worst > ib_st:
        ctx.col.warn(f"{ctx.fp}:[stitch].max_open: worst-case stitch card-pool estimate "
                     f"{stitch_worst} tokens > the input budget of {ib_st} tokens (profile "
                     f"[llm.{p.stitch.llm}], (max_open + 1) = {p.stitch.max_open + 1} cards "
                     f"x 2 frame digests x digest_max_chars = {p.stitch.digest_max_chars}) - "
                     f"max_open is never auto-shrunk (a semantics change belongs to the "
                     f"user): raise context_window, lower stitch.digest_max_chars or lower "
                     f"stitch.max_open (3.16.5)")


def _check_budget_and_vision(ctx: _LoadCtx, products: _Products, len_max: int) -> _LoadCtx:
    """v1.11 预算与 vision 推导簇的驱动器。

    @param ctx 校验上下文
    @param products 产物累加器
    @param len_max 各类 len_range 上界的最大值
    @return 冻结了两个 vision 解析产物的新上下文
    """
    _check_removed_use_vision(ctx)
    ctx = _freeze_vision(ctx)
    _warn_segment_image_limit(ctx)
    _warn_undeclared_windows(ctx, products)
    _check_min_window(ctx)
    _check_static_budget(ctx, products, len_max)
    _warn_stitch_card_pool(ctx)
    return ctx


# ── 收尾: 必填指令 / 路径 / 自增强偏差 ──────────────────────────────────────


def _check_required_instructions(ctx: _LoadCtx) -> None:
    """启用即必填的两条指令(spec §5.2 †)。

    v1.13: 时间流形态把任务描述放在按类生成指令上(全局键退化为可选默认),
    「参与类 instruction 非空」由形态约束簇按类裁定。

    @param ctx 校验上下文
    """
    col, fp, p = ctx.col, ctx.fp, ctx.p
    if p.annotate.enabled and not p.annotate.instruction.strip():
        col.error(f"{fp}:[annotate].instruction: required when annotate.enabled = true, "
                  f"expected a non-empty string")
    if (p.generate.enabled and not p.generate.instruction.strip()
            and not p.generate_stream.enabled):
        col.error(f"{fp}:[generate].instruction: required when generate.enabled = true, "
                  f"expected a non-empty string")


def _check_paths(ctx: _LoadCtx, products: _Products) -> None:
    """规则 21: 输入/输出路径的存在性关系与输出父目录可写性。

    输入路径的**存在性/可读性**在此故意不校验: 按 spec §2.4(路径缺失 → 退出码 3,
    仅 process 模式)与冻结的 InputError 契约("运行起点处路径缺失"), 那项检查属于 M2
    的 Ingestor.scan()/records()。M1 只查输出与输入的路径关系(输入不存在时尽力而为:
    is_dir()/is_file() 都为 False)。

    @param ctx 校验上下文
    @param products 产物累加器(填充生效输入/输出路径)
    """
    col, fp, p = ctx.col, ctx.fp, ctx.p
    eff_input = ctx.cli.input if ctx.cli.input is not None else p.run["input"]
    eff_output = ctx.cli.output if ctx.cli.output is not None else p.run["output"]
    products.eff_input, products.eff_output = eff_input, eff_output
    if eff_output is None:
        col.error(f"{fp}:[run].output: missing required key, expected string (may be "
                  f"supplied by CLI --output)")
    input_path = Path(eff_input) if eff_input else None
    if ctx.mode == "process":
        if eff_input is None:
            col.error(f"{fp}:[run].input: required in process mode (may be supplied by "
                      f"CLI --input)")
        elif eff_output is not None:
            out_res = Path(eff_output).resolve()
            in_res = input_path.resolve()
            if input_path.is_dir() and out_res.is_relative_to(in_res):
                col.error(f"{fp}:[run].output: must not be inside the input directory "
                          f"(self-ingestion guard), got {_fmt(eff_output)}")
            elif input_path.is_file() and out_res == in_res:
                col.error(f"{fp}:[run].output: must not be the same as the input file, "
                          f"got {_fmt(eff_output)}")
    if eff_output is not None:
        parent = Path(eff_output).resolve().parent
        if not (parent.is_dir() and os.access(parent, os.W_OK)):
            col.error(f"{fp}:[run].output: output parent directory does not exist or is "
                      f"not writable, got {_fmt(eff_output)}")


def _warn_self_enhancement(ctx: _LoadCtx) -> None:
    """非阻断告警: 复核与标注用同一模型存在自增强偏差风险(spec 3.7.2)。

    @param ctx 校验上下文
    """
    p = ctx.p
    if not (p.verify.enabled and p.annotate.enabled):
        return
    a_prof = ctx.llm_profiles.get(p.annotate.llm)
    v_prof = ctx.llm_profiles.get(p.verify.llm) if not p.verify.judges else None
    if a_prof is not None and v_prof is not None and a_prof.model == v_prof.model:
        ctx.col.warn(f"{ctx.fp}:[verify].llm: verify.llm and annotate.llm use the same "
                     f"model {_fmt(a_prof.model)}, which risks self-enhancement bias (3.7.2)")


def validate(ctx: _LoadCtx, products: _Products) -> _LoadCtx:
    """按 spec 3.1.4 的表行次序跑完全部约束簇(次序即报错聚合次序)。

    @param ctx 校验上下文
    @param products 产物累加器(逐阶段填充)
    @return 完成回填与冻结后的上下文
    """
    _check_cli_overrides(ctx)
    _check_profile_refs(ctx)
    _check_vision_profiles(ctx)
    _check_dedup_semantic(ctx)
    _check_cross_field(ctx)
    _check_run_mode(ctx)
    _resolve_api_keys(ctx, products)
    _load_schema_and_hooks(ctx, products)
    _resolve_global_rubric(ctx, products)
    ctx = _check_classify_and_views(ctx, products)
    _check_stage_matrix(ctx)
    _check_stream_family(ctx, products)
    _check_frame_family(ctx, products)
    _, len_max = _check_generate_stream_form(ctx, products)
    ctx = _check_budget_and_vision(ctx, products, len_max)
    _check_required_instructions(ctx)
    _check_paths(ctx, products)
    _warn_self_enhancement(ctx)
    return ctx

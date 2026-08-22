"""M6 平面生成的确定性计划、过滤与记录装配。"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import logging
import math
import re
import statistics
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from datasketch import MinHash, MinHashLSH

from labelkit.common.contracts.types import PipelineItem, Record, RecordRef
from labelkit.common.errors import (
    ErrorKind,
    LabelKitError,
    ProviderFatalError,
    ProviderRetryableError,
    SchemaViolation,
    InternalError,
)
from labelkit.common.inference import budget

if TYPE_CHECKING:
    import random
    from collections.abc import Mapping

    from labelkit.common.config.model import GenerateConfig, GenerateStyle, ResolvedConfig
    from labelkit.common.inference.llm_client import PromptBundle


_log = logging.getLogger("labelkit.generate")


def canonical_json(obj: object) -> str:
    """返回平面生成使用的规范 JSON。

    @param obj 待序列化对象。
    @return 键序稳定且无冗余空白的 JSON 文本。
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def make_generated_record(
    sample: str,
    text_field: str,
    seed_ids: Sequence[str],
    llm: str,
    style: str | None,
) -> Record:
    """从一个平面样本构造冻结记录。

    @param sample LLM 产出的文本。
    @param text_field 原始对象中的文本字段。
    @param seed_ids 实际发送的种子记录标识。
    @param llm 生成 profile 名。
    @param style 风格名或 None。
    @return 带生成溯源的记录。
    """
    raw = {text_field: sample}
    record_id = hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()[:16]
    return Record(
        id=record_id,
        modality="text",
        text=sample,
        raw=raw,
        ui_tree=None,
        image=None,
        ref=RecordRef(
            source_file="",
            line_no=None,
            pair_index=None,
            generated_from=tuple(seed_ids),
            generator={"llm": llm, "style": style},
        ),
    )


def bucket_key(llm: str, style: str | None, class_name: str | None = None) -> str:
    """构造平面生成报告桶键。

    @param llm 生成 profile 名。
    @param style 风格名或 None。
    @param class_name owning 类名或 None。
    @return 冻结格式的桶键。
    """
    tail = f"{llm}×{style if style is not None else 'null'}"
    return tail if class_name is None else f"{class_name}×{tail}"


def render_prompt_texts(
    instruction: str,
    style_prompt: str | None,
    num_per_call: int,
    seed_texts: Sequence[str],
) -> tuple[str, str]:
    """装配平面生成的固定提示词文本。

    @param instruction 类有效生成指令。
    @param style_prompt 风格提示或 None。
    @param num_per_call 单次要求样本数。
    @param seed_texts 实际发送的种子文本。
    @return system 与 user 文本。
    """
    system_lines = [instruction]
    if style_prompt is not None:
        system_lines.append(f"[风格要求] {style_prompt}")
    system_lines.append("输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：")
    system_lines.append('{"samples": [<新样本文本>, ...]}' + f"（恰 {num_per_call} 条）")
    user_lines = [f"[种子示例 {index}] {text}" for index, text in enumerate(seed_texts, 1)]
    user_lines.append(f"请生成 {num_per_call} 条全新样本。")
    return "\n".join(system_lines), "\n".join(user_lines)


def build_generate_prompt(
    instruction: str,
    style_prompt: str | None,
    num_per_call: int,
    seed_texts: Sequence[str],
    temperature: float,
) -> "PromptBundle":
    """构造平面生成的 PromptBundle。

    @param instruction 类有效生成指令。
    @param style_prompt 风格提示或 None。
    @param num_per_call 单次要求样本数。
    @param seed_texts 实际发送的种子文本。
    @param temperature 类有效温度。
    @return 可直接发送的提示对象。
    """
    from labelkit.common.inference.llm_client import Message, Part, PromptBundle

    system_text, user_text = render_prompt_texts(
        instruction, style_prompt, num_per_call, seed_texts
    )
    return PromptBundle(
        messages=(
            Message(role="system", parts=(Part(kind="text", text=system_text),)),
            Message(role="user", parts=(Part(kind="text", text=user_text),)),
        ),
        temperature=temperature,
    )


def samples_schema(num_per_call: int) -> dict:
    """返回定长 samples 内部 Schema。

    @param num_per_call 单次要求样本数。
    @return Draft 2020-12 Schema。
    """
    from labelkit.common.inference.schema_engine import samples_schema as build_schema

    return build_schema(num_per_call)


def predraw_llm_style(
    generate: "GenerateConfig",
    num_calls: int,
    rng: "random.Random",
    styles_by_index: Sequence[tuple["GenerateStyle", ...]] | None = None,
) -> list[tuple[str, "GenerateStyle | None"]]:
    """按冻结消费顺序预抽 profile 与风格。

    @param generate 平面生成配置。
    @param num_calls 调用总数。
    @param rng 单流随机数生成器。
    @param styles_by_index 各调用的风格池或 None。
    @return 按调用序排列的 profile 与风格对。
    """
    pairs: list[tuple[str, "GenerateStyle | None"]] = []
    for index in range(num_calls):
        if generate.mixture == "weighted":
            llm = rng.choices(list(generate.llms), weights=list(generate.weights), k=1)[0]
        else:
            llm = generate.llms[index % len(generate.llms)]
        styles = generate.styles if styles_by_index is None else styles_by_index[index]
        pairs.append((llm, rng.choice(styles) if styles else None))
    return pairs


@dataclass(frozen=True)
class CallPlan:
    """一次平面生成调用的派发前计划。"""

    index: int  # 全局调用序号。
    llm: str  # 生成 profile 名。
    style_name: str | None  # 风格名或 None。
    style_prompt: str | None  # 风格提示或 None。
    seed_ids: tuple[str, ...]  # 实际发送的种子标识。
    seed_texts: tuple[str, ...]  # 实际发送的种子文本。
    class_name: str | None = None  # owning 类名或 None。


@dataclass(frozen=True)
class ClassSegment:
    """一个按类归属的平面调用段。"""

    class_name: str | None  # owning 类名或 None。
    seeds: tuple[tuple[str | None, str], ...]  # 标识与文本组成的种子池。
    num_calls: int  # 本段调用预算。
    styles: tuple["GenerateStyle", ...]  # 类有效风格池。


def build_segment_plans(
    generate: "GenerateConfig",
    segments: Sequence[ClassSegment],
    rng: "random.Random",
    exec_calls: int | None = None,
) -> list[CallPlan]:
    """预抽跨类段的完整平面调用计划。

    @param generate 全局平面生成配置。
    @param segments 已排序的类段。
    @param rng 单流随机数生成器。
    @param exec_calls 实际执行的调用数或 None。
    @return 按全局调用序排列的计划。
    """
    total_calls = sum(segment.num_calls for segment in segments)
    selected_calls = total_calls if exec_calls is None else min(exec_calls, total_calls)
    owners = [segment for segment in segments for _ in range(segment.num_calls)]
    pairs = predraw_llm_style(
        generate, total_calls, rng, styles_by_index=[owner.styles for owner in owners]
    )
    plans: list[CallPlan] = []
    for index in range(selected_calls):
        owner = owners[index]
        drawn = _draw_seeds(generate, owner, rng)
        llm, style = pairs[index]
        plans.append(_make_call_plan(index, llm, style, drawn, owner.class_name))
    return plans


def _draw_seeds(
    generate: "GenerateConfig",
    owner: ClassSegment,
    rng: "random.Random",
) -> list[tuple[str | None, str]]:
    """按既有抽签规则抽取一个调用的种子。

    @param generate 全局平面生成配置。
    @param owner 当前调用所属类段。
    @param rng 单流随机数生成器。
    @return 无放回抽取的种子。
    """
    if not owner.seeds:
        return []
    count = min(generate.seeds_per_call, len(owner.seeds))
    return rng.sample(list(owner.seeds), count)


def _make_call_plan(
    index: int,
    llm: str,
    style: "GenerateStyle | None",
    drawn: Sequence[tuple[str | None, str]],
    class_name: str | None,
) -> CallPlan:
    """从一次预抽结果构造调用计划。

    @param index 全局调用序号。
    @param llm 生成 profile 名。
    @param style 预抽风格或 None。
    @param drawn 预抽种子。
    @param class_name owning 类名或 None。
    @return 冻结调用计划。
    """
    return CallPlan(
        index=index,
        llm=llm,
        style_name=style.name if style else None,
        style_prompt=style.prompt if style else None,
        seed_ids=tuple(seed_id for seed_id, _ in drawn if seed_id is not None),
        seed_texts=tuple(text for _, text in drawn),
        class_name=class_name,
    )


def build_call_plans(
    generate: "GenerateConfig",
    seeds: Sequence[tuple[str | None, str]],
    num_calls: int,
    rng: "random.Random",
    exec_calls: int | None = None,
) -> list[CallPlan]:
    """构造单匿名段的平面调用计划。

    @param generate 全局平面生成配置。
    @param seeds 匿名种子池。
    @param num_calls 调用预算。
    @param rng 单流随机数生成器。
    @param exec_calls 实际执行调用数或 None。
    @return 按调用序排列的计划。
    """
    segment = ClassSegment(None, tuple(seeds), num_calls, generate.styles)
    return build_segment_plans(generate, [segment], rng, exec_calls)


def _normalize(text: str) -> str:
    """归一化相似度文本。

    @param text 原始文本。
    @return NFC 且空白折叠后的文本。
    """
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


class SimilarityFilter:
    """生成样本的 MinHash-LSH 近重过滤器。"""

    def __init__(self, threshold: float = 0.85, num_perm: int = 128, ngram: int = 5):
        """构造过滤器。

        @param threshold Jaccard 阈值。
        @param num_perm MinHash 置换数。
        @param ngram 字符 shingle 长度。
        """
        self._threshold = threshold
        self._num_perm = num_perm
        self._ngram = ngram
        self._lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
        self._signatures: dict[str, MinHash] = {}

    def _minhash(self, text: str) -> MinHash:
        """计算文本的 MinHash。

        @param text 待签名文本。
        @return MinHash 签名。
        """
        normalized = _normalize(text)
        if len(normalized) >= self._ngram:
            shingles = {
                normalized[index:index + self._ngram]
                for index in range(len(normalized) - self._ngram + 1)
            }
        else:
            shingles = {normalized}
        signature = MinHash(num_perm=self._num_perm)
        for shingle in shingles:
            signature.update(shingle.encode("utf-8"))
        return signature

    def _is_duplicate(self, signature: MinHash) -> bool:
        """精确复核 LSH 候选。

        @param signature 待判定签名。
        @return 是否命中近重。
        """
        return any(
            signature.jaccard(self._signatures[key]) >= self._threshold
            for key in self._lsh.query(signature)
        )

    def add(self, text: str) -> None:
        """无条件加入一段文本。

        @param text 待加入文本。
        @return None。
        """
        self.commit(self._minhash(text))

    def probe_and_add(self, text: str) -> bool:
        """探测文本并在新颖时加入。

        @param text 待判定文本。
        @return 新颖时为 true。
        """
        novel, signature = self.probe(text)
        if novel:
            self.commit(signature)
        return novel

    def probe(self, text: str) -> tuple[bool, MinHash]:
        """只读探测一段文本。

        @param text 待判定文本。
        @return 新颖标志与签名。
        """
        signature = self._minhash(text)
        return not self._is_duplicate(signature), signature

    def commit(self, signature: MinHash) -> None:
        """提交已通过探测的签名。

        @param signature 待提交签名。
        @return None。
        """
        key = f"s{len(self._signatures)}"
        self._signatures[key] = signature
        self._lsh.insert(key, signature)


def select_seeds(
    batch: Sequence[PipelineItem],
    config: "ResolvedConfig",
) -> dict[str | None, list[tuple[str, str]]]:
    """按类和质量阈值选取平面生成种子。

    @param batch 当前批。
    @param config 已解析配置。
    @return 按类分组的种子池。
    """
    pools: dict[str | None, list[tuple[PipelineItem, float]]] = {}
    for item in batch:
        score = item.scores.get("__aggregate__")
        if item.status != "active" or score is None or score.score is None:
            continue
        label = _item_class(item, config)
        pools.setdefault(label, []).append((item, score.score))
    return {
        label: selected
        for label in sorted(pools, key=lambda value: value or "")
        if (selected := _select_pool(label, pools[label], config))
    }


def _item_class(item: PipelineItem, config: "ResolvedConfig") -> str | None:
    """解析一个种子的类归属。

    @param item 当前流水线项。
    @param config 已解析配置。
    @return 类名或 None。
    """
    if config.classify.enabled and item.classification is not None:
        return item.classification.label
    return None


def _select_pool(
    label: str | None,
    scored: Sequence[tuple[PipelineItem, float]],
    config: "ResolvedConfig",
) -> list[tuple[str, str]]:
    """按类有效阈值过滤一个种子池。

    @param label 类名或 None。
    @param scored 带聚合分的流水线项。
    @param config 已解析配置。
    @return 通过阈值的标识与文本。
    """
    threshold = config.generate.seed_min_score
    if threshold is None:
        quality = config.class_views[label].quality if label is not None else config.quality
        threshold = quality.threshold
    if threshold is None:
        threshold = statistics.median(score for _, score in scored)
    return [
        (item.record.id, item.record.text or "")
        for item, score in scored
        if score >= threshold
    ]


def effective_generate(config: "ResolvedConfig", class_name: str | None) -> "GenerateConfig":
    """返回类有效平面生成配置。

    @param config 已解析配置。
    @param class_name 类名或 None。
    @return 生效的 GenerateConfig。
    """
    return config.generate if class_name is None else config.class_views[class_name].generate


def build_class_segments(
    pools: "Mapping[str | None, list[tuple[str, str]]]",
    config: "ResolvedConfig",
) -> list[ClassSegment]:
    """把种子池转换为按声明规则排序的类段。

    @param pools 按类分组的种子池。
    @param config 已解析配置。
    @return 按类名字典序排列的类段。
    """
    segments: list[ClassSegment] = []
    for label in sorted(pools, key=lambda value: value or ""):
        generate = effective_generate(config, label)
        num_calls = math.ceil(
            len(pools[label]) * generate.num_per_record / config.generate.num_per_call
        )
        segments.append(ClassSegment(label, tuple(pools[label]), num_calls, generate.styles))
    return segments


class _SampleGate:
    """平面样本用户回调闸门。"""

    def __init__(self, hook_ref: str | None, target=None):
        """构造回调闸门。

        @param hook_ref 回调引用或 None。
        @param target M1 冻结 callable 或 None。
        """
        self._hook_ref = hook_ref
        self._hook = target
        if self._hook is None and hook_ref:
            _log.error("generate sample hook was not frozen by M1")
            raise InternalError("generate sample hook was not frozen by M1")
        self._warned = False

    @property
    def enabled(self) -> bool:
        """@return 是否启用回调闸门。"""
        return self._hook is not None

    def violates(self, sample: str) -> bool:
        """判定一个样本是否违反用户回调。

        @param sample 待判定文本。
        @return 违规时为 true。
        """
        from labelkit.common.extensions.hooks import normalize_violations

        try:
            violations = normalize_violations(self._hook(sample), self._hook_ref)
        except Exception as exc:
            self._warn_once(exc)
            violations = ["callback raised"]
        return bool(violations)

    def _warn_once(self, exc: Exception) -> None:
        """只记录一次回调异常。

        @param exc 回调异常。
        @return None。
        """
        if self._warned:
            return
        self._warned = True
        _log.warning(
            "generate.sample_validator raised; the offending sample is dropped as a violation "
            "(warned once): %s: %s",
            type(exc).__name__,
            exc,
            extra={"stage": "generate", "batch": 0},
        )


@dataclass(frozen=True)
class _PostprocessContext:
    """平面生成后处理的共享上下文。"""

    gate: _SampleGate  # 样本回调闸门。
    similarity: SimilarityFilter  # 已注入种子的相似度索引。
    config: "ResolvedConfig"  # 已解析配置。
    metrics: object  # MetricsSink 鸭子面。


def postprocess_samples(
    plans: Sequence[CallPlan],
    results: Sequence[list[str] | None],
    seed_texts: Sequence[str],
    config: "ResolvedConfig",
    metrics,
) -> list[tuple[Record, str | None]]:
    """按调用序过滤并装配平面生成结果。

    @param plans 与结果对位的调用计划。
    @param results 逐调用结果或 None。
    @param seed_texts 全部种子文本。
    @param config 已解析配置。
    @param metrics MetricsSink 鸭子面。
    @return 记录与 owning 类名对。
    """
    similarity = SimilarityFilter(
        threshold=config.dedup.minhash_threshold,
        num_perm=config.dedup.minhash_num_perm,
        ngram=config.dedup.ngram,
    )
    for text in seed_texts:
        similarity.add(text)
    frozen = config.validation_hooks.sample if config.validation_hooks else None
    context = _PostprocessContext(
        _SampleGate(config.generate.sample_validator, frozen.target if frozen else None),
        similarity,
        config,
        metrics,
    )
    records: list[tuple[Record, str | None]] = []
    for plan, samples in zip(plans, results):
        records.extend(_process_call(plan, samples, context))
    return records


def _process_call(
    plan: CallPlan,
    samples: Sequence[str] | None,
    context: _PostprocessContext,
) -> list[tuple[Record, str | None]]:
    """处理一次调用的返回样本。

    @param plan 当前调用计划。
    @param samples 样本序列或 None。
    @param context 后处理上下文。
    @return 通过全部闸门的记录。
    """
    key = bucket_key(plan.llm, plan.style_name, plan.class_name)
    context.metrics.count(f"generate.buckets.{key}.calls")
    if context.gate.enabled:
        context.metrics.count(f"generate.buckets.{key}.rejected_by_validator", 0)
    if samples is None:
        return []
    context.metrics.count(f"generate.buckets.{key}.produced", len(samples))
    return _accept_samples(plan, samples, context)


def _accept_samples(
    plan: CallPlan,
    samples: Sequence[str],
    context: _PostprocessContext,
) -> list[tuple[Record, str | None]]:
    """执行回调、相似度与记录装配。

    @param plan 当前调用计划。
    @param samples LLM 返回的样本。
    @param context 后处理上下文。
    @return 通过闸门的记录。
    """
    key = bucket_key(plan.llm, plan.style_name, plan.class_name)
    accepted: list[tuple[Record, str | None]] = []
    for sample in samples:
        if context.gate.enabled and context.gate.violates(sample):
            context.metrics.count(f"generate.buckets.{key}.rejected_by_validator")
            continue
        if not context.similarity.probe_and_add(sample):
            continue
        record = make_generated_record(
            sample,
            context.config.input.text_field,
            plan.seed_ids,
            plan.llm,
            plan.style_name,
        )
        context.metrics.count(f"generate.buckets.{key}.survived_dedup")
        accepted.append((record, plan.class_name))
    return accepted


def error_kind(exc: LabelKitError) -> str:
    """把平面生成异常映射到冻结错误种类。

    @param exc LabelKitError。
    @return 错误种类字符串。
    """
    kind = budget.classify_stage_error(exc)
    if kind is not None:
        return kind
    if isinstance(exc, SchemaViolation):
        return ErrorKind.SCHEMA_VIOLATION.value
    if isinstance(exc, ProviderRetryableError):
        return ErrorKind.PROVIDER_RETRYABLE_EXHAUSTED.value
    if isinstance(exc, ProviderFatalError):
        return ErrorKind.PROVIDER_FATAL.value
    return ErrorKind.INTERNAL_ERROR.value


def fit_plan_seeds(
    plan: CallPlan,
    config: "ResolvedConfig",
) -> tuple[CallPlan, bool, bool]:
    """按上下文预算从尾部裁剪平面种子。

    @param plan 待装填调用计划。
    @param config 已解析配置。
    @return 计划、是否裁剪、是否不可装填。
    """
    profile = config.llm_profiles.get(plan.llm)
    if profile is None or profile.context_window <= 0:
        return plan, False, False
    generate = config.generate
    effective = effective_generate(config, plan.class_name)
    available = _generate_input_budget(profile, generate.num_per_call)

    def fits(seed_texts: Sequence[str]) -> bool:
        """判断当前种子前缀是否落入完整调用输入预算。"""
        system, user = render_prompt_texts(
            effective.instruction, plan.style_prompt, generate.num_per_call, seed_texts
        )
        estimate = budget.est_text(system) + budget.est_text(user)
        return estimate + 2 * budget.MSG_OVERHEAD_TOKENS <= available

    return _tail_drop_seeds(plan, fits)


def _generate_input_budget(profile, num_per_call: int) -> int:
    """计算平面调用可用的输入预算。

    @param profile 目标 LLM profile。
    @param num_per_call 单次样本数。
    @return 可用输入 token 数。
    """
    available = budget.input_budget(profile)
    if profile.supports_structured_output:
        available -= budget.est_text(json.dumps(samples_schema(num_per_call), ensure_ascii=False))
    return available


def _tail_drop_seeds(plan: CallPlan, fits) -> tuple[CallPlan, bool, bool]:
    """按抽取序尾删种子直到提示词可装填。

    @param plan 待裁剪计划。
    @param fits 种子前缀预算谓词。
    @return 计划、是否裁剪、是否不可装填。
    """
    count = len(plan.seed_texts)
    for keep in range(count, 0, -1):
        if not fits(plan.seed_texts[:keep]):
            continue
        if keep == count:
            return plan, False, False
        seed_ids = plan.seed_ids[:keep] if len(plan.seed_ids) == count else plan.seed_ids
        return dataclasses.replace(
            plan, seed_ids=seed_ids, seed_texts=plan.seed_texts[:keep]
        ), True, False
    if count == 0 and fits(()):
        return plan, False, False
    return plan, False, True


def void_log_message(plan: CallPlan, exc: LabelKitError) -> str:
    """构造不含数据值的作废调用日志。

    @param plan 作废调用计划。
    @param exc 触发作废的异常。
    @return 英文单行摘要。
    """
    message = (
        f"generate call voided: call={plan.index} llm={plan.llm} "
        f"style={plan.style_name if plan.style_name is not None else 'null'} "
        f"kind={error_kind(exc)}"
    )
    if isinstance(exc, SchemaViolation):
        message += f" violations={len(exc.errors)}"
    return message


def deepcopy_json(value: object) -> object:
    """返回平面辅助测试使用的 JSON 深拷贝。

    @param value JSON-compatible 值。
    @return 深拷贝值。
    """
    return copy.deepcopy(value)

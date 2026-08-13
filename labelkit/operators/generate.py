"""M6 generate — synthesize new text records from seeds (spec 3.6, CONTRACTS §7.5).

Process mode: seeds are the current batch's quality-gate survivors; ``run()`` returns a
sub-batch of new PipelineItems (the input batch is never touched). generate_only mode
(v1.4): ``generate_all()`` produces every Record up front from the ``generate.seed_examples``
pool or, seedless, from ``generate.instruction`` × styles with a ``standalone_count`` target.

v1.7 per-class seed pools (classify enabled, process mode; spec 3.6.2 按类种子池,
R17–R19): seeds are grouped by ``item.classification.label``; participating classes occupy
consecutive global call-index ranges in class-name lexicographic order; each call uses the
class-effective instruction/styles/num_per_record/temperature while llms/mixture/weights/
seeds_per_call/num_per_call stay global. New records inherit the seed class
(``Classification(label, (label,), "inherited", {})``). Classify disabled ⇒ one anonymous
segment = the pre-v1.7 behavior, byte-identical draw stream included. The generate_only
``generate_all`` path stays flat (global instruction, no class segments).

All randomness comes from ``ctx.rng``; the full (llm, style) assignment and the per-call
seed draws are made in call-index order BEFORE any dispatch so results are independent of
concurrency scheduling (spec 3.6.2). New samples pass a MinHash similarity filter against
the seeds and against each other (Self-Instruct filter, threshold = dedup.minhash_threshold).

v1.13 时间流形态（SPEC-stream-generation §3.2，``generate_stream.enabled``）：generate_only
的第三形态——LLM 只做两类内容调用（一序列一次蓝图、一次帧实现，噪音帧批量实现复用平面
模板），装箱/交叉/噪音/重复/时间戳全部由机械交织器完成；单流 ``Random(f"{seed}:0:generate")``
按裁决·抽签消费顺序表三段消费（计划期①②③、派发期零消费、交织期④–⑨）。产物一式两份：
可重放的时间流工件行（工件行即 raw——重放同 id；真值不携最终 id——循环依赖封死）与直装
序列信封（两级 inherited 标签 + session_id）。``generate_all`` 平面路径零改动。
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import math
import re
import statistics
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from datasketch import MinHash, MinHashLSH

from labelkit.common.errors import (
    CircuitBreakerTripped,
    ContextOverflowError,
    ErrorKind,
    LabelKitError,
    ProviderFatalError,
    ProviderRetryableError,
    SchemaViolation,
)
from labelkit.common.contracts.types import Classification, PipelineItem, Record, RecordRef
from labelkit.common.runtime import budget

if TYPE_CHECKING:
    import random
    from typing import Mapping

    from labelkit.common.config.model import GenerateConfig, GenerateStyle, ResolvedConfig
    from labelkit.common.contracts.stage import RunContext
    from labelkit.common.runtime.llm_client import PromptBundle

# M6 observability is the report.generate.buckets counters only (spec 3.6.2 溯源与可观测,
# CONTRACTS §7.5). No M6-specific trace events: the §8.1 catalog defines none for generate,
# and "generate" is not a legal trace.channels value. Voided calls remain observable through
# the catalogued llm.call / schema.repair events (M9/M8) plus the value-free stderr log below.
_log = logging.getLogger("labelkit.generate")


# ── canonical helpers ──────────────────────────────────────────────────────

def canonical_json(obj) -> str:
    """M2's canonical JSON used for generated-record ids (CONTRACTS §3)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def make_generated_record(sample: str, text_field: str, seed_ids: Sequence[str],
                          llm: str, style: str | None) -> Record:
    """Construct a new generated Record per spec 3.6.2 新记录构造."""
    raw = {text_field: sample}
    rec_id = hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()[:16]
    return Record(
        id=rec_id,
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
    """Report bucket key ``<llm>×<style|null>`` (CONTRACTS §7.5 [FROZEN]).

    v1.7: calls that belong to a class segment (classify enabled, process mode) gain a
    class prefix — ``<class>×<llm>×<style|null>``, same literal ``×``. class_name=None
    (classify disabled, and the flat generate_only path) keeps the two-segment form
    byte-identical."""
    tail = f"{llm}×{style if style is not None else 'null'}"
    return tail if class_name is None else f"{class_name}×{tail}"


# ── prompt assembly (§10.4, deterministic template) ────────────────────────

def render_prompt_texts(instruction: str, style_prompt: str | None,
                        num_per_call: int, seed_texts: Sequence[str]) -> tuple[str, str]:
    """Pure text assembly of the generation prompt: returns (system_text, user_text)."""
    system_lines = [instruction]
    if style_prompt is not None:
        system_lines.append(f"[风格要求] {style_prompt}")
    system_lines.append("输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：")
    system_lines.append('{"samples": [<新样本文本>, ...]}' + f"（恰 {num_per_call} 条）")
    user_lines = [f"[种子示例 {i}] {text}" for i, text in enumerate(seed_texts, start=1)]
    user_lines.append(f"请生成 {num_per_call} 条全新样本。")
    return "\n".join(system_lines), "\n".join(user_lines)


def build_generate_prompt(instruction: str, style_prompt: str | None, num_per_call: int,
                          seed_texts: Sequence[str], temperature: float) -> "PromptBundle":
    # Imported lazily so this module's pure logic stays importable before M9 lands.
    from labelkit.common.runtime.llm_client import Message, Part, PromptBundle

    system_text, user_text = render_prompt_texts(instruction, style_prompt,
                                                 num_per_call, seed_texts)
    return PromptBundle(
        messages=(
            Message(role="system", parts=(Part(kind="text", text=system_text),)),
            Message(role="user", parts=(Part(kind="text", text=user_text),)),
        ),
        temperature=temperature,
    )


def _samples_schema(num_per_call: int) -> dict:
    # Lazy import: the schema constant is owned by M8 (CONTRACTS §7.7/§10.7).
    from labelkit.common.runtime.schema_engine import samples_schema

    return samples_schema(num_per_call)


# ── pre-drawn call plan (spec 3.6.2 多模型混合 / 风格条件化 / v1.7 类段) ────

@dataclass(frozen=True)
class CallPlan:
    index: int                          # GLOBAL call index 0..C-1 (across class segments)
    llm: str                            # [llm.*] profile name
    style_name: str | None
    style_prompt: str | None
    seed_ids: tuple[str, ...]           # process mode: sampled seed record ids; else ()
    seed_texts: tuple[str, ...]         # sampled seed texts ((), seedless form)
    class_name: str | None = None       # v1.7 (R17): owning class segment; None = the
                                        # anonymous segment (classify disabled / generate_only)


@dataclass(frozen=True)
class ClassSegment:
    """Planning input for one class segment (v1.7, R18) — or the single anonymous
    segment (class_name=None) that reproduces the pre-v1.7 behavior."""
    class_name: str | None
    seeds: tuple[tuple[str | None, str], ...]   # (record_id_or_None, text); () = seedless
    num_calls: int                              # segment budget C_c
    styles: tuple["GenerateStyle", ...]         # class-effective styles ((), no styles)


def predraw_llm_style(
    g: "GenerateConfig", num_calls: int, rng: "random.Random",
    styles_by_index: Sequence[tuple["GenerateStyle", ...]] | None = None,
) -> list[tuple[str, "GenerateStyle | None"]]:
    """Pre-draw the (llm, style) pair for every call index 0..num_calls-1 with ctx.rng.

    round_robin: llms[i % len(llms)] (no RNG consumed for the llm);
    weighted: rng.choices per index; style: uniform rng.choice per index when styles set.
    v1.7 (R18): ``styles_by_index`` supplies the effective styles of the class OWNING each
    global index; None means uniform g.styles everywhere (identical draw stream).
    """
    pairs: list[tuple[str, "GenerateStyle | None"]] = []
    for i in range(num_calls):
        if g.mixture == "weighted":
            llm = rng.choices(list(g.llms), weights=list(g.weights), k=1)[0]
        else:
            llm = g.llms[i % len(g.llms)]
        styles = g.styles if styles_by_index is None else styles_by_index[i]
        style = rng.choice(styles) if styles else None
        pairs.append((llm, style))
    return pairs


def build_segment_plans(g: "GenerateConfig", segments: Sequence[ClassSegment],
                        rng: "random.Random",
                        exec_calls: int | None = None) -> list[CallPlan]:
    """Full pre-dispatch plan over the concatenated class segments (v1.7, R18).

    Segments occupy consecutive global call-index ranges in the given order (the caller
    sorts participating classes lexicographically). One pass pre-draws (llm, style) for
    ALL indexes — llm by global index exactly as before, style from the owning segment's
    styles — so --limit truncation does not disturb the draw stream; then seed draws run
    per executed call in ascending global index order from the owning segment's pool.
    A single anonymous segment reproduces the pre-v1.7 plan byte-for-byte."""
    total_calls = sum(seg.num_calls for seg in segments)
    if exec_calls is None:
        exec_calls = total_calls
    exec_calls = min(exec_calls, total_calls)
    owner: list[ClassSegment] = []
    for seg in segments:
        owner.extend([seg] * seg.num_calls)
    pairs = predraw_llm_style(g, total_calls, rng,
                              styles_by_index=[seg.styles for seg in owner])
    plans: list[CallPlan] = []
    for i in range(exec_calls):
        seg = owner[i]
        llm, style = pairs[i]
        if seg.seeds:
            k = min(g.seeds_per_call, len(seg.seeds))
            drawn = rng.sample(list(seg.seeds), k)
        else:
            drawn = []
        plans.append(CallPlan(
            index=i,
            llm=llm,
            style_name=style.name if style else None,
            style_prompt=style.prompt if style else None,
            seed_ids=tuple(sid for sid, _ in drawn if sid is not None),
            seed_texts=tuple(text for _, text in drawn),
            class_name=seg.class_name,
        ))
    return plans


def build_call_plans(g: "GenerateConfig", seeds: Sequence[tuple[str | None, str]],
                     num_calls: int, rng: "random.Random",
                     exec_calls: int | None = None) -> list[CallPlan]:
    """Pre-v1.7 flat plan: one anonymous segment with the global styles. Kept as the
    zero-change regression anchor — the draw stream of the segmented planner with a
    single anonymous segment is identical to the pre-v1.7 implementation."""
    segment = ClassSegment(class_name=None, seeds=tuple(seeds),
                           num_calls=num_calls, styles=g.styles)
    return build_segment_plans(g, [segment], rng, exec_calls=exec_calls)


# ── MinHash similarity filter (Self-Instruct, spec 3.6.2 回流 / 3.3.3) ──────

def _normalize(text: str) -> str:
    """Same text normalization as M3 dedup: NFC + whitespace-run collapse + strip."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


class SimilarityFilter:
    """MinHash-LSH near-duplicate filter for generated samples vs seeds and each other.

    Character n-gram shingles over normalized text; a probe whose estimated Jaccard vs any
    stored text is >= threshold is a duplicate. Threshold defaults to the spec's 0.85
    (dedup.minhash_threshold)."""

    def __init__(self, threshold: float = 0.85, num_perm: int = 128, ngram: int = 5):
        self._threshold = threshold
        self._num_perm = num_perm
        self._ngram = ngram
        self._lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
        self._sigs: dict[str, MinHash] = {}

    def _minhash(self, text: str) -> MinHash:
        norm = _normalize(text)
        if len(norm) >= self._ngram:
            shingles = {norm[i:i + self._ngram] for i in range(len(norm) - self._ngram + 1)}
        else:
            shingles = {norm}
        m = MinHash(num_perm=self._num_perm)
        for s in shingles:
            m.update(s.encode("utf-8"))
        return m

    def _is_duplicate(self, m: MinHash) -> bool:
        for key in self._lsh.query(m):
            if m.jaccard(self._sigs[key]) >= self._threshold:
                return True
        return False

    def add(self, text: str) -> None:
        m = self._minhash(text)
        key = f"s{len(self._sigs)}"
        self._sigs[key] = m
        self._lsh.insert(key, m)

    def probe_and_add(self, text: str) -> bool:
        """True = novel (and added to the index); False = near-duplicate (not added)."""
        m = self._minhash(text)
        if self._is_duplicate(m):
            return False
        key = f"s{len(self._sigs)}"
        self._sigs[key] = m
        self._lsh.insert(key, m)
        return True


# ── seed selection (process mode, spec 3.6.2 种子选取 / v1.7 按类种子池) ────

def select_seeds(batch: Sequence[PipelineItem],
                 cfg: "ResolvedConfig") -> dict[str | None, list[tuple[str, str]]]:
    """Group the seed pool by class (v1.7, R19): classify enabled ⇒ key =
    ``item.classification.label``; disabled ⇒ a single anonymous group (key None) with
    exactly the pre-v1.7 selection. Per-group threshold chain: global
    ``generate.seed_min_score`` → absent: the CLASS-effective ``quality.threshold``
    (global one for the anonymous group) → absent: the median aggregate of that group's
    own scored pool. Unscored items never seed; groups where nothing passes are omitted.
    Keys are sorted (class-name lexicographic) so iteration order is the segment order."""
    pools: dict[str | None, list[tuple[PipelineItem, float]]] = {}
    for item in batch:
        if item.status != "active":
            continue
        agg = item.scores.get("__aggregate__")
        if agg is None or agg.score is None:
            continue
        if cfg.classify.enabled and item.classification is not None:
            label: str | None = item.classification.label
        else:
            label = None
        pools.setdefault(label, []).append((item, agg.score))
    selected: dict[str | None, list[tuple[str, str]]] = {}
    for label in sorted(pools, key=lambda l: l or ""):
        scored = pools[label]
        threshold = cfg.generate.seed_min_score
        if threshold is None:
            effective_quality = (cfg.class_views[label].quality if label is not None
                                 else cfg.quality)
            threshold = effective_quality.threshold
        if threshold is None:
            threshold = statistics.median(s for _, s in scored)
        seeds = [(item.record.id, item.record.text or "")
                 for item, score in scored if score >= threshold]
        if seeds:
            selected[label] = seeds
    return selected


# ── per-class effective config + segment assembly (v1.7) ───────────────────

def effective_generate(cfg: "ResolvedConfig", class_name: str | None) -> "GenerateConfig":
    """The class-effective [generate] section (R17): ``class_views[class].generate`` for a
    class segment, the global section for the anonymous one. Only instruction / styles /
    num_per_record / temperature may differ per class (5.2 whitelist); llms / mixture /
    weights / seeds_per_call / num_per_call are read from the GLOBAL section by callers."""
    if class_name is None:
        return cfg.generate
    return cfg.class_views[class_name].generate


def build_class_segments(pools: "Mapping[str | None, list[tuple[str, str]]]",
                         cfg: "ResolvedConfig") -> list[ClassSegment]:
    """Segment the grouped seed pools in class-name lexicographic order (R18). Budget
    per segment: C_c = ceil(len(seeds_c) × num_per_record_c / num_per_call) with the
    class-effective num_per_record and the GLOBAL num_per_call."""
    segments: list[ClassSegment] = []
    for label in sorted(pools, key=lambda l: l or ""):
        seeds_c = pools[label]
        gen_c = effective_generate(cfg, label)
        segments.append(ClassSegment(
            class_name=label,
            seeds=tuple(seeds_c),
            num_calls=math.ceil(len(seeds_c) * gen_c.num_per_record
                                / cfg.generate.num_per_call),
            styles=gen_c.styles,
        ))
    return segments


# ── post-processing: filter + record construction + bucket stats ───────────

def postprocess_samples(plans: Sequence[CallPlan],
                        results: Sequence[list[str] | None],
                        seed_texts: Sequence[str],
                        cfg: "ResolvedConfig",
                        metrics) -> list[tuple[Record, str | None]]:
    """Deterministic post-dispatch assembly, processed in call-index order.

    ``results[i]`` is the sample list of call i, or None for a voided call (invalid after
    M8 repair / retries exhausted): its bucket counts ``calls`` with ``produced`` 0 and no
    failed record is created (spec 3.6.3). Bucket counters (CONTRACTS §9.3):
    calls = dispatched calls; produced = samples returned by the LLM; survived_dedup =
    samples surviving the MinHash similarity filter (only those become Records).
    v1.7 (R17): returns (record, class) pairs — class = the producing plan's class_name
    (None on the anonymous segment) — and class-segment calls use three-segment bucket
    keys ``<class>×<llm>×<style|null>``."""
    d = cfg.dedup
    filt = SimilarityFilter(threshold=d.minhash_threshold,
                            num_perm=d.minhash_num_perm, ngram=d.ngram)
    for text in seed_texts:
        filt.add(text)
    # v1.5 plan A (spec 3.6.2): optional per-sample user hook, applied BEFORE
    # the similarity filter. Filter semantics: a violating sample is dropped
    # (no retry, no failed record), counted per bucket.
    sample_hook = None
    hook_ref = cfg.generate.sample_validator
    if hook_ref:
        from labelkit.common.extensions.hooks import resolve_hook
        sample_hook = resolve_hook(hook_ref)
    hook_error_warned = False
    records: list[tuple[Record, str | None]] = []
    for plan, samples in zip(plans, results):
        key = bucket_key(plan.llm, plan.style_name, plan.class_name)
        metrics.count(f"generate.buckets.{key}.calls")
        if sample_hook is not None:
            metrics.count(f"generate.buckets.{key}.rejected_by_validator", 0)
        if samples is None:
            continue
        metrics.count(f"generate.buckets.{key}.produced", len(samples))
        for sample in samples:
            if sample_hook is not None:
                from labelkit.common.extensions.hooks import normalize_violations
                try:
                    violations = normalize_violations(sample_hook(sample), hook_ref)
                except Exception as exc:  # hook bug: drop the sample, never the run
                    if not hook_error_warned:
                        hook_error_warned = True
                        logging.getLogger("labelkit.generate").warning(
                            "generate.sample_validator 回调抛出异常，命中样本按违规剔除"
                            "（本条提示仅打印一次）：%s: %s",
                            type(exc).__name__, exc,
                            extra={"stage": "generate", "batch": 0})
                    violations = ["callback raised"]
                if violations:
                    metrics.count(f"generate.buckets.{key}.rejected_by_validator")
                    continue
            if not filt.probe_and_add(sample):
                continue
            rec = make_generated_record(sample, cfg.input.text_field,
                                        plan.seed_ids, plan.llm, plan.style_name)
            metrics.count(f"generate.buckets.{key}.survived_dedup")
            # NOTE: counts.generated is owned by M10 (orchestrator), which counts
            # the records it receives from generate_all/GenerateStage. Incrementing
            # it here as well would double-count in report.counts (§9.3 invariant).
            records.append((rec, plan.class_name))
    return records


def _error_kind(exc: LabelKitError) -> str:
    # v1.11 (V27①): the budget vocabulary routes FIRST — a context_overflow /
    # output_truncated void must not surface as internal_error in the stderr line.
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


# ── v1.11 seed packing (spec 3.6.2 上下文预算装填 row / §3.3⑦) ──────────────

def _fit_plan_seeds(plan: CallPlan, cfg: "ResolvedConfig") -> tuple[CallPlan, bool, bool]:
    """seeds_per_call demoted to an UPPER BOUND under a declared budget: seeds are
    dropped FROM THE TAIL of the rng-drawn order (never re-drawn — determinism)
    until the call's prompt est fits the TARGET profile's input budget; min 1
    seed. The (llm, style) pre-draw and rotation order are untouched, so the
    trimmed plan stays call-by-call reproducible (llms mixture included). System
    side (instruction / style / output-structure sentence) is static — V13③ M1
    precheck territory, never trimmed here. Returns (plan', truncated,
    unfittable); unfittable=True ⇒ not even 1 seed (or the seedless prompt)
    fits — the CALL is disposed per V10 by the dispatcher (voided, kind
    context_overflow). Budget off (profile missing / cw == 0) → (plan, False,
    False) byte-identically."""
    prof = cfg.llm_profiles.get(plan.llm)
    if prof is None or prof.context_window <= 0:
        return plan, False, False
    g = cfg.generate
    gen_c = effective_generate(cfg, plan.class_name)
    b = budget.input_budget(prof)
    if prof.supports_structured_output:
        b -= budget.est_text(json.dumps(_samples_schema(g.num_per_call),
                                        ensure_ascii=False))

    def fits(seed_texts: Sequence[str]) -> bool:
        system_text, user_text = render_prompt_texts(
            gen_c.instruction, plan.style_prompt, g.num_per_call, seed_texts)
        est = (budget.est_text(system_text) + budget.est_text(user_text)
               + 2 * budget.MSG_OVERHEAD_TOKENS)
        return est <= b

    n = len(plan.seed_texts)
    for keep in range(n, 0, -1):                # tail drop: prefix of the drawn order
        if fits(plan.seed_texts[:keep]):
            if keep == n:
                return plan, False, False
            # seed_ids align positionally with seed_texts in process mode; the
            # generate_only pool carries no ids (empty tuple stays empty).
            ids = (plan.seed_ids[:keep] if len(plan.seed_ids) == n
                   else plan.seed_ids)
            return (dataclasses.replace(plan, seed_ids=ids,
                                        seed_texts=plan.seed_texts[:keep]),
                    True, False)
    if n == 0 and fits(()):                     # seedless form: nothing to drop
        return plan, False, False
    return plan, False, True                    # V10: voided whole, nothing trimmed


def void_log_message(plan: CallPlan, exc: LabelKitError) -> str:
    """Value-free stderr summary of a voided generation call (spec 3.6.3).

    Structural fields only — call index, config identifiers (llm profile / style name),
    error kind, violation count. NEVER str(exc): SchemaViolation's rendered violations
    embed LLM-generated sample text, and stderr must not carry data content or prompts
    (CONTRACTS §8.4, §11.7; spec ch.7)."""
    msg = (f"生成调用作废 call={plan.index} llm={plan.llm} "
           f"style={plan.style_name if plan.style_name is not None else 'null'} "
           f"kind={_error_kind(exc)}")
    if isinstance(exc, SchemaViolation):
        msg += f" violations={len(exc.errors)}"
    return msg


# ── v1.13 时间流形态：模板（§10.14/§10.15，实现即冻结面）────────────────────

# 蓝图模板静态脚手架：budget.TEMPLATE_HEAD_TOKENS["generate_plan"] 钉住
# est_text(_PLAN_SYSTEM_STATIC)（V22 家族跨层等式，tests/common/runtime/
# test_budget.py 守护两侧同步）；类生成指令与帧类表是配置量，在 M1 静态预算
# 预检（V13③）各自计量。
_PLAN_SYSTEM_HEAD = ("你是时间流数据规划器。给定任务描述与帧类表，为一条序列规划逐步蓝图："
                     "每一步选定一个帧类，并用一句话写明该步内容要点。")
_PLAN_LABEL_TASK = "[任务]"
_PLAN_LABEL_FRAME_TABLE = "[帧类表]"
_PLAN_STRUCTURE = (
    "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
    '{"steps": [{"frame_class": <帧类名>, "brief": <一句话要点>}, ...]}\n'
    "字段说明：steps 恰为要求的步数，一步一项，按时间顺序排列；frame_class 必须取自 "
    "[帧类表] 中的帧类名；brief 用一句话写明该步内容要点，供逐帧实现展开。")
_PLAN_SYSTEM_STATIC = "\n".join((_PLAN_SYSTEM_HEAD, _PLAN_LABEL_TASK,
                                 _PLAN_LABEL_FRAME_TABLE, _PLAN_STRUCTURE))

# 帧实现模板静态脚手架：TEMPLATE_HEAD_TOKENS["generate_realize"] 钉住
# est_text(_REALIZE_SYSTEM_STATIC)。逐位契约行把帧类生成 Schema 文本按步重复——
# L0 关端点（DeepSeek anthropic 路由硬拒强制 tool call）上结构服从性靠该契约。
_REALIZE_LABEL_TASK = "[任务]"
_REALIZE_LABEL_STYLE = "[风格要求]"
_REALIZE_STRUCTURE = (
    "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
    '{"frames": [<第 1 帧内容>, <第 2 帧内容>, ...]}\n'
    "字段说明：frames 恰为蓝图步数，一帧一项，与蓝图步序逐位对应；逐帧内容契约如下：")
_REALIZE_FREE_TEXT = "自由文本一段"
_REALIZE_SYSTEM_STATIC = "\n".join((_REALIZE_LABEL_TASK, _REALIZE_LABEL_STYLE,
                                    _REALIZE_STRUCTURE))

_MAX_STREAM_DEGRADE_LEVELS = 2   # 实现调用对半降级级数上限（裁决·预算头两键，AIMD ≤2）


def render_plan_prompt_texts(instruction: str, frame_classes: Sequence,
                             class_name: str, length: int) -> tuple[str, str]:
    """蓝图调用的纯文本装配（§10.14）：返回 (system_text, user_text)。

    :param instruction: 类有效生成指令（[class.<name>.generate].instruction）。
    :param frame_classes: 全帧类表（[[frame.classify.classes]] 的 ClassSpec 序列）。
    :param class_name: 序列类名（user 段引用）。
    :param length: 步数 L（与 plan_schema 的 minItems=maxItems 同源）。
    """
    table = "\n".join(f"{c.name}: {c.description}" for c in frame_classes)
    system = "\n".join((_PLAN_SYSTEM_HEAD,
                        f"{_PLAN_LABEL_TASK} {instruction}",
                        f"{_PLAN_LABEL_FRAME_TABLE}\n{table}",
                        _PLAN_STRUCTURE))
    return system, f"请为一条「{class_name}」序列产出 {length} 步蓝图。"


def render_realize_prompt_texts(instruction: str, style_prompt: str | None,
                                steps: Sequence[tuple[str, str]],
                                contracts: Sequence[str]) -> tuple[str, str]:
    """帧实现调用的纯文本装配（§10.15）：返回 (system_text, user_text)。

    :param instruction: 类有效生成指令。
    :param style_prompt: 预抽风格提示；None = 无风格段（蓝图不带风格，实现才带）。
    :param steps: 蓝图步序列 [(frame_class, brief), ...]（对半降级时为切片，局部重编号）。
    :param contracts: 与 steps 对位的逐帧内容契约文本（Schema 单行 dump 或自由文本句）。
    """
    lines = [f"{_REALIZE_LABEL_TASK} {instruction}"]
    if style_prompt is not None:
        lines.append(f"{_REALIZE_LABEL_STYLE} {style_prompt}")
    lines.append(_REALIZE_STRUCTURE)
    for i, ((frame_class, _brief), contract) in enumerate(zip(steps, contracts), 1):
        lines.append(f"第 {i} 帧（{frame_class}）须符合：{contract}")
    user_lines = [f"{i}. [{frame_class}] {brief}"
                  for i, (frame_class, brief) in enumerate(steps, 1)]
    user_lines.append(f"请实现全部 {len(steps)} 帧内容。")
    return "\n".join(lines), "\n".join(user_lines)


def _plan_schema(names: Sequence[str], length: int) -> dict:
    # 懒导入：内部 Schema 构造器归 M8（CONTRACTS §7.7/§10.7）。
    from labelkit.common.runtime.schema_engine import plan_schema

    return plan_schema(names, length)


def _realize_schema(step_schemas: Sequence[dict]) -> dict:
    # 懒导入：同上。
    from labelkit.common.runtime.schema_engine import realize_schema

    return realize_schema(step_schemas)


def _text_bundle(system_text: str, user_text: str,
                 temperature: float) -> "PromptBundle":
    """单 system + 单 user 的纯文本 PromptBundle（蓝图/实现两模板共用装配尾）。"""
    from labelkit.common.runtime.llm_client import Message, Part, PromptBundle

    return PromptBundle(
        messages=(Message(role="system", parts=(Part(kind="text", text=system_text),)),
                  Message(role="user", parts=(Part(kind="text", text=user_text),))),
        temperature=temperature)


# ── v1.13 时间流形态：计划期纯函数（estimate_run 精确复演共用）──────────────

@dataclass(frozen=True)
class SequencePlan:
    index: int                  # 计划序全局序号 0 基（配额展开序）
    class_name: str             # 所属序列类
    ordinal: int                # 类内序数 0 基（= 工件 truth.sequence）
    length: int                 # 步数 L（rng.randint(类有效 len_range)）
    llm: str                    # 预抽 profile——蓝图+实现绑定同一 profile
    style_name: str | None      # 预抽风格名（实现才生效，蓝图不带风格）
    style_prompt: str | None    # 预抽风格提示词


@dataclass(frozen=True)
class NoiseCallPlan:
    index: int                  # 噪音批调用序号 0 基
    llm: str                    # 独立预抽 profile（裁决·生成键效力矩阵）
    style_name: str | None      # 预抽风格名（全局 styles 池）
    style_prompt: str | None    # 预抽风格提示词


@dataclass(frozen=True)
class StreamPlan:
    sequences: tuple[SequencePlan, ...]     # 计划序（类字典序 × 类内序数）
    noise_target: int                       # round(noise_ratio × Σ length)
    noise_plans: tuple[NoiseCallPlan, ...]  # ⌈noise_target / num_per_call⌉ 个


@dataclass(frozen=True)
class RealizedSequence:
    plan: SequencePlan
    frame_classes: tuple[str, ...]          # 蓝图逐步帧类（帧级真值）
    payloads: tuple = ()                    # 逐帧 text_field 值（str 或结构化帧对象）


@dataclass(frozen=True)
class StreamGenerateProduct:
    """``generate_stream_all`` 的富返回（裁决·时间流入口与配额截断）——
    ``PipelineItem(record=r)`` 裸构造无法携带 session_id/classification/
    member_classifications，故必须整信封交付。"""
    envelopes: list[PipelineItem]           # 直装序列信封（计划序）
    artifact_lines: list[str]               # 工件行（交织序定稿；行号 = 列表序 + 1）


def expand_stream_quota(cfg: "ResolvedConfig") -> list[tuple[str, int]]:
    """计划期第①步（零 rng）：类按类名字典序展开配额为 (类名, 类内序数) 列表；
    ``--limit`` 在此做前缀截断（配额层截断 ⇒ 作废序列不再生成、不进交织，工件与
    主输出覆盖面恒一致）。"""
    entries: list[tuple[str, int]] = []
    for name in sorted(cfg.class_views):
        for ordinal in range(cfg.class_views[name].generate.sequences):
            entries.append((name, ordinal))
    if cfg.limit is not None:
        entries = entries[: cfg.limit]
    return entries


def plan_stream(cfg: "ResolvedConfig", rng: "random.Random") -> StreamPlan:
    """计划期纯函数（M10 estimate_run 精确复演共用，裁决·估算精确复演）。

    抽签消费顺序冻结（裁决·抽签消费顺序表，测试钉住）：①配额展开（截断，零 rng）
    ②逐序列 L = rng.randint(类有效 len_range) ③逐序列 (llm, style) 预抽——噪音批
    调用独立预抽，紧随序列预抽在同一 predraw 流内消费（round_robin 不耗 rng、
    weighted 逐位 rng.choices、styles 非空逐位 rng.choice；噪音批取全局 styles）。
    """
    entries = expand_stream_quota(cfg)
    lengths: list[int] = []
    for name, _ in entries:
        lo, hi = cfg.class_views[name].generate.len_range
        lengths.append(rng.randint(lo, hi))
    g = cfg.generate
    noise_target = round(cfg.generate_stream.noise_ratio * sum(lengths))
    n_noise = math.ceil(noise_target / g.num_per_call) if noise_target > 0 else 0
    styles_by_index = ([cfg.class_views[name].generate.styles for name, _ in entries]
                       + [g.styles] * n_noise)
    pairs = predraw_llm_style(g, len(entries) + n_noise, rng,
                              styles_by_index=styles_by_index)
    sequences = tuple(
        SequencePlan(index=i, class_name=name, ordinal=ordinal, length=lengths[i],
                     llm=pairs[i][0],
                     style_name=pairs[i][1].name if pairs[i][1] else None,
                     style_prompt=pairs[i][1].prompt if pairs[i][1] else None)
        for i, (name, ordinal) in enumerate(entries))
    offset = len(entries)
    noise_plans = tuple(
        NoiseCallPlan(index=j, llm=pairs[offset + j][0],
                      style_name=(pairs[offset + j][1].name
                                  if pairs[offset + j][1] else None),
                      style_prompt=(pairs[offset + j][1].prompt
                                    if pairs[offset + j][1] else None))
        for j in range(n_noise))
    return StreamPlan(sequences=sequences, noise_target=noise_target,
                      noise_plans=noise_plans)


# ── v1.13 时间流形态：机械交织器（纯函数族，零 LLM 零 IO）────────────────────

@dataclass
class _StreamSlot:
    """交织后的一帧槽位（工件行装配前形态；仅本模块内部可变）。"""
    payload: "str | Mapping"    # text_field 值（结构化帧 = 行内对象）
    truth: dict                 # 冻结键集 truth（session 值交织尾声回填）
    owner: int | None           # 幸存序列下标（任务帧）；噪音/重复帧 = None
    ts: str = ""                # ⑨ 铺设的 ISO-8601 时间戳


def _sequence_slots(index: int, seq: RealizedSequence) -> list[_StreamSlot]:
    """一条幸存序列的任务帧槽位（truth.session 占位 −1，交织尾声回填）。"""
    return [_StreamSlot(payload=seq.payloads[i],
                        truth={"session": -1, "sequence_class": seq.plan.class_name,
                               "sequence": seq.plan.ordinal,
                               "frame_class": seq.frame_classes[i], "noise": False},
                        owner=index)
            for i in range(len(seq.payloads))]


def _duplicate_slots(seq: RealizedSequence) -> list[_StreamSlot]:
    """⑧ 一条重复序列的流尾新会话槽位：帧 text_field 值逐字节同源（同对象再序列
    化），truth 带 duplicate_of = 原序列类内序数、sequence = null（重发副本无自身
    计划期身份，归属经 duplicate_of 对账——裁决·工件行真值字段集）。"""
    return [_StreamSlot(payload=seq.payloads[i],
                        truth={"session": -1, "sequence_class": seq.plan.class_name,
                               "sequence": None, "frame_class": seq.frame_classes[i],
                               "noise": False, "duplicate_of": seq.plan.ordinal},
                        owner=None)
            for i in range(len(seq.payloads))]


def _noise_slot(payload: str) -> _StreamSlot:
    """一帧插入噪音的槽位（真值三 null + noise=true）。"""
    return _StreamSlot(payload=payload,
                       truth={"session": -1, "sequence_class": None, "sequence": None,
                              "frame_class": None, "noise": True},
                       owner=None)


def _cross_session(slots_a: list[_StreamSlot], slots_b: list[_StreamSlot],
                   rng: "random.Random") -> list[_StreamSlot]:
    """⑥ 单个交叉会话的切换点掷签：形态 A 段+B 段+A 余段[+B 余段]（裁决·会话装箱
    定容）——cut_a ∈ [1, len(A)−1] 保证真交叉（A 必在 B 头部之后回续），cut_b ∈
    [1, len(B)]（= len(B) 时无 B 余段）。A 不足 2 帧时与 B 互换；两者都不足 ⇒
    真交叉不可构造，退化为顺次拼接（纯长度条件，确定性，零 rng 消费）。"""
    if len(slots_a) < 2 <= len(slots_b):
        slots_a, slots_b = slots_b, slots_a
    if len(slots_a) < 2:
        return slots_a + slots_b
    cut_a = rng.randint(1, len(slots_a) - 1)
    cut_b = rng.randint(1, len(slots_b))
    return slots_a[:cut_a] + slots_b[:cut_b] + slots_a[cut_a:] + slots_b[cut_b:]


def _pack_sessions(survivors: Sequence[RealizedSequence], declared: int,
                   rng: "random.Random") -> tuple[list[list[_StreamSlot]], int]:
    """⑤ 装箱定容：洗牌后前 Σ幸存 − sessions_eff 对成对交叉（sessions_eff =
    min(sessions, Σ幸存)），其余单序列会话；会话序 = 洗牌序（交叉会话在前）。"""
    order = list(range(len(survivors)))
    rng.shuffle(order)
    sessions_eff = min(declared, len(order))
    n_cross = len(order) - sessions_eff
    sessions: list[list[_StreamSlot]] = []
    for pair in range(n_cross):
        a, b = order[2 * pair], order[2 * pair + 1]
        sessions.append(_cross_session(_sequence_slots(a, survivors[a]),
                                       _sequence_slots(b, survivors[b]), rng))
    for index in order[2 * n_cross:]:
        sessions.append(_sequence_slots(index, survivors[index]))
    return sessions, n_cross


def _insert_noise(sessions: list[list[_StreamSlot]], payloads: Sequence[str],
                  session_max_len: int, rng: "random.Random") -> int:
    """⑦ 逐噪音帧 (会话, 槽位) 掷签：满员会话（len ≥ session_max_len）退出签池；
    签池耗尽 ⇒ 余帧从交织缺席（不补生成）。返回实际织入帧数。"""
    woven = 0
    for payload in payloads:
        pool = [session for session in sessions if len(session) < session_max_len]
        if not pool:
            _log.warning("noise weaving stopped: every session is at "
                         "stream.session_max_len; %d noise frame(s) dropped",
                         len(payloads) - woven,
                         extra={"stage": "generate", "batch": 0})
            break
        target = rng.choice(pool)
        target.insert(rng.randint(0, len(target)), _noise_slot(payload))
        woven += 1
    return woven


def _lay_timestamps(sessions: list[list[_StreamSlot]], cfg: "ResolvedConfig",
                    rng: "random.Random") -> None:
    """⑨ ts 铺设：起点 ts_start（流首帧零消费）；帧间隔 uniform(frame_gap_s)、会话
    间隔 uniform(gap_s + lo, gap_s + hi)（恒 > stream.gap_s ⇒ 摄取侧按同一 gap_s
    复演出相同会话切分）；datetime + timedelta 正间隔累加 ⇒ 严格递增；isoformat
    微秒精度写出。"""
    lo, hi = cfg.generate_stream.frame_gap_s
    gap = float(cfg.stream.gap_s)
    current = datetime.fromisoformat(cfg.generate_stream.ts_start)
    first = True
    for session in sessions:
        for position, slot in enumerate(session):
            if first:
                first = False
            elif position == 0:
                current += timedelta(seconds=rng.uniform(gap + lo, gap + hi))
            else:
                current += timedelta(seconds=rng.uniform(lo, hi))
            slot.ts = current.isoformat(timespec="microseconds")


def weave_stream(survivors: Sequence[RealizedSequence], noise_payloads: Sequence[str],
                 cfg: "ResolvedConfig", rng: "random.Random",
                 ) -> tuple[list[list[_StreamSlot]], dict]:
    """机械交织器入口（纯函数族，零 LLM 零 IO；裁决·抽签消费顺序表④–⑨单流顺序
    消费）：④重复选取 rng.sample ⑤装箱洗牌+成对交叉 ⑥逐交叉会话切换点 ⑦逐噪音帧
    掷签 ⑧重复序列成流尾新会话（零 rng）⑨ts 铺设；尾声回填 truth.session 全流会话
    序数。返回 (会话列表, counts-only 统计——sessions 不含重复尾会话)。"""
    gs = cfg.generate_stream
    dup_k = min(gs.duplicates, len(survivors))
    if dup_k < gs.duplicates:
        _log.warning("duplicates clamped to the surviving sequence count: %d -> %d",
                     gs.duplicates, dup_k, extra={"stage": "generate", "batch": 0})
    chosen = rng.sample(list(survivors), dup_k) if dup_k else []           # ④
    sessions, crossed = _pack_sessions(survivors, gs.sessions, rng)        # ⑤⑥
    woven_noise = _insert_noise(sessions, noise_payloads,
                                cfg.stream.session_max_len, rng)           # ⑦
    for source in chosen:                                                  # ⑧
        sessions.append(_duplicate_slots(source))
    for session_no, session in enumerate(sessions):
        for slot in session:
            slot.truth["session"] = session_no
    _lay_timestamps(sessions, cfg, rng)                                    # ⑨
    stats = {"sessions": len(sessions) - dup_k, "crossed_sessions": crossed,
             "frames": sum(len(seq.payloads) for seq in survivors),
             "noise_frames": woven_noise, "duplicates": dup_k}
    return sessions, stats


# ── v1.13 时间流形态：直装组装 ───────────────────────────────────────────────

def stream_artifact_path(cfg: "ResolvedConfig") -> str:
    """工件路径推导：输出路径去末级后缀 + ".stream.jsonl"。M11 Emitter 的工件通道
    用同一规则各自推导（算子间不互导，两侧等式由测试钉住）。"""
    return str(Path(cfg.run.output).with_suffix("")) + ".stream.jsonl"


def _payload_text(payload: "str | Mapping") -> str:
    """text_field 值的 M2 语义投影：字符串直取、对象 canonical JSON（重放时 M2
    的 dotted-path 提取产出同一投影——裁决·工件行即 raw）。"""
    return payload if isinstance(payload, str) else canonical_json(payload)


def _stream_envelope(seq: RealizedSequence, records: tuple[Record, ...],
                     session_id: str) -> PipelineItem:
    """一条幸存序列的直装信封：sequence Record（S24 字段惯例、ref = 首成员 ref、
    id = M14 公式 sha256("\\n".join(member ids))[:16]）+ session_id + 序列级/帧级
    两级 inherited 标签（帧级真值随 member_classifications 落 members[]）。"""
    joined = "\n".join(record.id for record in records)
    label = seq.plan.class_name
    sequence_record = Record(
        id=hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16],
        modality="text", text=None, raw=None, ui_tree=None, image=None,
        ref=records[0].ref, kind="sequence", members=records)
    member_classifications = {
        record.id: Classification(label=frame_class, labels=(frame_class,),
                                  source="inherited", detail={})
        for record, frame_class in zip(records, seq.frame_classes)}
    return PipelineItem(
        record=sequence_record, session_id=session_id,
        classification=Classification(label=label, labels=(label,),
                                      source="inherited", detail={}),
        member_classifications=member_classifications)


def assemble_stream(sessions: list[list[_StreamSlot]],
                    survivors: Sequence[RealizedSequence],
                    cfg: "ResolvedConfig") -> tuple[list[str], list[PipelineItem]]:
    """直装组装（裁决·工件行即 raw / 真值不携最终 id）：逐行构造工件行对象
    ``{<ts字段>: …, <text_field>: …, "truth": {…}}``（行序列化 json.dumps
    ensure_ascii=False 族；canonical_json 只用于 id 计算）与成员 Record（id =
    M2 公式、行号 = 列表序 + 1）；session_id = M2 公式（含噪音帧与重复帧）；
    噪音/重复帧只活在工件。信封按计划序返回。"""
    ts_field = cfg.stream.order_by[len("meta:"):]
    text_field = cfg.input.text_field
    path = stream_artifact_path(cfg)
    lines: list[str] = []
    session_ids: list[str] = []
    members: dict[int, list[Record]] = {}
    owner_session: dict[int, int] = {}
    for session_no, session in enumerate(sessions):
        frame_ids: list[str] = []
        for slot in session:
            row = {ts_field: slot.ts, text_field: slot.payload, "truth": slot.truth}
            rec_id = hashlib.sha256(
                canonical_json(row).encode("utf-8")).hexdigest()[:16]
            frame_ids.append(rec_id)
            lines.append(json.dumps(row, ensure_ascii=False))
            if slot.owner is None:
                continue                   # 噪音/重复帧不构造信封
            plan = survivors[slot.owner].plan
            members.setdefault(slot.owner, []).append(Record(
                id=rec_id, modality="text", text=_payload_text(slot.payload),
                raw=row, ui_tree=None, image=None,
                ref=RecordRef(source_file=path, line_no=len(lines), pair_index=None,
                              generated_from=(),
                              generator={"llm": plan.llm, "style": plan.style_name})))
            owner_session.setdefault(slot.owner, session_no)
        session_ids.append(hashlib.sha256(
            "\n".join(frame_ids).encode("utf-8")).hexdigest()[:16])
    envelopes = [_stream_envelope(survivors[owner], tuple(members[owner]),
                                  session_ids[owner_session[owner]])
                 for owner in sorted(members)]
    return lines, envelopes


async def _realize_degrading(realize, span: tuple[int, int], ctx: "RunContext",
                             level: int = 0) -> list[list]:
    """帧实现的反应式对半降级（classify._judge_frames_degrading 零重叠版同型）：
    reactive ContextOverflowError ⇒ [s, m) / [m, e) 顺序重试（每次对半计
    budget.degrade_retries，≤ _MAX_STREAM_DEGRADE_LEVELS 级；schema 与蓝图概要
    随切片同步减半）；precheck 相位、单步跨度或级数耗尽 ⇒ 原样上抛由调用方作废
    序列。返回跨度序的叶结果列表（帧载荷列表）。"""
    try:
        return [await realize(span)]
    except ContextOverflowError as exc:
        start, end = span
        if (exc.phase != "reactive" or end - start < 2
                or level >= _MAX_STREAM_DEGRADE_LEVELS):
            raise
        ctx.metrics.count("budget.degrade_retries")
        middle = (start + end) // 2
        leaves = await _realize_degrading(realize, (start, middle), ctx, level + 1)
        leaves.extend(await _realize_degrading(realize, (middle, end), ctx, level + 1))
        return leaves


# ── the stage ──────────────────────────────────────────────────────────────

class GenerateStage:
    name = "generate"

    def __init__(self, cfg: "ResolvedConfig"):
        self._cfg = cfg

    async def run(self, batch: list[PipelineItem], ctx: "RunContext") -> list[PipelineItem]:
        """PROCESS MODE. Returns the sub-batch of NEW PipelineItems (input batch untouched).
        A generation call that is invalid after M8 repair or exhausts retries is voided
        (bucket ``calls`` counted, ``produced`` 0); no failed records are created; seed
        records are unaffected. v1.7: seeds are grouped per class (classify enabled) and
        new records inherit the seed class (``source="inherited"``, R17)."""
        pools = select_seeds(batch, self._cfg)
        if not pools:
            return []
        segments = build_class_segments(pools, self._cfg)
        records = await self._generate(segments, ctx, limit=None)
        return [
            PipelineItem(record=rec) if cls is None else PipelineItem(
                record=rec,
                classification=Classification(label=cls, labels=(cls,),
                                              source="inherited", detail={}))
            for rec, cls in records
        ]

    async def generate_all(self, ctx: "RunContext") -> list[Record]:
        """GENERATE_ONLY MODE entry (called once by M10 before batching; ctx.batch_no == 0,
        ctx.rng == Random(f"{seed}:0:generate")). Executes all calls per the 3.6.2 count
        formulas; --limit truncates to the first ceil(limit / num_per_call) calls in
        pre-drawn order and then to limit records. v1.7: the flat path is UNCHANGED —
        one anonymous segment, global instruction, no class labels (spec 3.6.2)."""
        g = self._cfg.generate
        if g.seed_examples:
            seeds: list[tuple[str | None, str]] = [(None, s) for s in g.seed_examples]
            num_calls = math.ceil(len(seeds) * g.num_per_record / g.num_per_call)
        else:
            seeds = []
            num_calls = math.ceil((g.standalone_count or 0) / g.num_per_call)
        segment = ClassSegment(class_name=None, seeds=tuple(seeds),
                               num_calls=num_calls, styles=g.styles)
        records = await self._generate([segment], ctx, limit=self._cfg.limit)
        return [rec for rec, _ in records]

    async def _generate(self, segments: Sequence[ClassSegment], ctx: "RunContext",
                        limit: int | None) -> list[tuple[Record, str | None]]:
        g = self._cfg.generate
        num_calls = sum(seg.num_calls for seg in segments)
        exec_calls = num_calls
        if limit is not None:
            exec_calls = min(num_calls, math.ceil(limit / g.num_per_call))
        # All draws happen in global call-index order before dispatch (spec 3.6.2).
        plans = build_segment_plans(g, segments, ctx.rng, exec_calls=exec_calls)
        schema = _samples_schema(g.num_per_call)

        # v1.11 (§3.3⑦): per-call seed packing BEFORE dispatch — deterministic
        # (content + pre-drawn plan only), so the fitted plans drive dispatch AND
        # post-processing (records inherit the actually-sent seed provenance).
        fitted: list[tuple[CallPlan, bool]] = []
        for plan in plans:
            plan, truncated, unfittable = _fit_plan_seeds(plan, self._cfg)
            if truncated:
                ctx.metrics.count("budget.truncations.generate")
            fitted.append((plan, unfittable))
        plans = [plan for plan, _ in fitted]

        async def one_call(plan: CallPlan, unfittable: bool) -> list[str] | None:
            if unfittable:
                # V10: not even 1 seed fits — never send the doomed request; the
                # CALL is voided under the existing failure semantics (bucket
                # `calls` counted, produced 0, no failed record) with the precise
                # kind in the stderr line. phase=precheck never feeds the breaker.
                _log.warning(void_log_message(plan, ContextOverflowError(
                    "generation call unfittable at 1 seed", phase="precheck",
                    profile=plan.llm)),
                    extra={"stage": self.name, "batch": ctx.batch_no})
                return None
            # R17: instruction/temperature are class-effective; num_per_call stays global.
            gen_c = effective_generate(self._cfg, plan.class_name)
            prompt = build_generate_prompt(gen_c.instruction, plan.style_prompt,
                                           g.num_per_call, plan.seed_texts,
                                           gen_c.temperature)
            try:
                obj, _usage, _attempts, _model = await ctx.schema_engine.complete_validated(
                    plan.llm, prompt, schema=schema,
                    record_ids=plan.seed_ids, batch_no=ctx.batch_no)
                return list(obj["samples"])
            except CircuitBreakerTripped:
                raise
            except LabelKitError as exc:
                # Voided call: only this call's samples are lost (record-level isolation).
                # Spec 3.6.3: no failed record and no StageError, hence no `error` trace
                # event either (§8.1 ties it to StageError construction) — the void shows
                # up in report.generate.buckets (calls counted, produced 0) and in M8/M9's
                # own schema.repair / llm.call events. Stderr gets a value-free one-liner.
                # v1.11: a reactive-400 overflow terminal (no degrade face here)
                # feeds the breaker exactly once (A7); precheck/finish never do.
                if (isinstance(exc, ContextOverflowError) and exc.phase == "reactive"
                        and getattr(exc, "origin", "http_400") == "http_400"
                        and not getattr(exc, "_breaker_fed", False)):
                    exc._breaker_fed = True  # type: ignore[attr-defined]
                    ctx.metrics.record_provider_result(fatal=True)
                _log.warning(void_log_message(plan, exc),
                             extra={"stage": self.name, "batch": ctx.batch_no})
                return None

        results = await asyncio.gather(*(one_call(p, u) for p, u in fitted))
        seed_texts = [text for seg in segments for _, text in seg.seeds]
        records = postprocess_samples(plans, list(results), seed_texts,
                                      self._cfg, ctx.metrics)
        if limit is not None:
            records = records[:limit]
        return records

    # ── v1.13 时间流形态（SPEC-stream-generation §3.2）──────────────────────

    async def generate_stream_all(self, ctx: "RunContext") -> StreamGenerateProduct:
        """GENERATE_ONLY 时间流形态入口（M10 分支调用一次；ctx.batch_no == 0，
        ctx.rng == Random(f"{seed}:0:generate")）。计划期抽签（①②③）→ 派发
        （零 rng：逐序列蓝图→实现作业与噪音批并发）→ 逐帧钩子与序列相似度过滤 →
        机械交织（④–⑨）→ 直装组装。作废序列只缺席，不产 failed 记录。"""
        plan = plan_stream(self._cfg, ctx.rng)
        for seq_plan in plan.sequences:
            ctx.metrics.count(
                f"generate.stream.sequences.{seq_plan.class_name}.planned")
        results = await asyncio.gather(
            *(self._stream_sequence_job(p, ctx) for p in plan.sequences),
            *(self._stream_noise_call(p, ctx) for p in plan.noise_plans))
        realized = [r for r in results[: len(plan.sequences)] if r is not None]
        noise: list[str] = []
        for samples in results[len(plan.sequences):]:
            noise.extend(samples or ())
        survivors = self._filter_stream_sequences(realized, ctx)
        sessions, stats = weave_stream(survivors, noise[: plan.noise_target],
                                       self._cfg, ctx.rng)
        lines, envelopes = assemble_stream(sessions, survivors, self._cfg)
        self._count_stream_product(survivors, stats, ctx)
        return StreamGenerateProduct(envelopes=envelopes, artifact_lines=lines)

    async def _stream_sequence_job(self, plan: SequencePlan,
                                   ctx: "RunContext") -> RealizedSequence | None:
        """一条序列的蓝图 → 帧实现 → 逐帧钩子作业；任一环节作废 ⇒ None（作废语义
        同平面路径 3.6.3：计数 + 值-free 日志，不产 failed 记录）。"""
        steps = await self._stream_plan_call(plan, ctx)
        if steps is None:
            return None
        payloads = await self._stream_realize_call(plan, steps, ctx)
        if payloads is None:
            return None
        bucket = bucket_key(plan.llm, plan.style_name, plan.class_name)
        ctx.metrics.count(f"generate.buckets.{bucket}.produced")
        if not self._stream_frames_valid(plan, payloads, ctx):
            return None
        return RealizedSequence(plan=plan,
                                frame_classes=tuple(fc for fc, _ in steps),
                                payloads=tuple(payloads))

    async def _stream_plan_call(self, plan: SequencePlan,
                                ctx: "RunContext") -> list[tuple[str, str]] | None:
        """蓝图调用（一序列一次；§10.14 模板 + plan_schema 内部待遇）：修复穷尽/
        不可装填 ⇒ 序列作废计 plan_failures（不产 failed 记录）。"""
        cfg = self._cfg
        gen_c = cfg.class_views[plan.class_name].generate
        classes = cfg.frame_classify.classes
        system_text, user_text = render_plan_prompt_texts(
            gen_c.instruction, classes, plan.class_name, plan.length)
        schema = _plan_schema([c.name for c in classes], plan.length)
        bucket = bucket_key(plan.llm, plan.style_name, plan.class_name)
        ctx.metrics.count(f"generate.buckets.{bucket}.calls")
        ctx.metrics.count("generate.stream.plan_calls")
        if cfg.generate.sample_validator:
            ctx.metrics.count(f"generate.buckets.{bucket}.rejected_by_validator", 0)
        if not self._stream_fits((system_text, user_text), plan.llm, schema):
            # V10 先例：最小单元不可装填——从不发出注定失败的请求；precheck 不喂熔断
            self._void_stream_sequence(plan, ContextOverflowError(
                "plan call unfittable under the input budget", phase="precheck",
                profile=plan.llm), "plan", ctx)
            return None
        prompt = _text_bundle(system_text, user_text, gen_c.temperature)
        try:
            obj, _usage, _attempts, _model = await ctx.schema_engine.complete_validated(
                plan.llm, prompt, schema=schema, batch_no=ctx.batch_no)
            return [(step["frame_class"], str(step["brief"]))
                    for step in obj["steps"]]
        except CircuitBreakerTripped:
            raise
        except LabelKitError as exc:
            self._void_stream_sequence(plan, exc, "plan", ctx)
            return None

    async def _stream_realize_call(self, plan: SequencePlan,
                                   steps: Sequence[tuple[str, str]],
                                   ctx: "RunContext") -> list | None:
        """帧实现调用（一蓝图一次；§10.15 逐位契约 + realize_schema）：反应式溢出
        ⇒ 序列对半分（schema 与蓝图概要同步减半，≤2 级，计 budget.degrade_retries
        既有通道）；穷尽/其余不可修复 ⇒ 序列作废计 realize_failures。"""
        cfg = self._cfg
        gen_c = cfg.class_views[plan.class_name].generate
        views = cfg.frame_class_views
        schemas = [(dict(views[fc].gen_schema) if views[fc].gen_schema is not None
                    else {"type": "string"}) for fc, _ in steps]
        contracts = [(json.dumps(views[fc].gen_schema, ensure_ascii=False,
                                 separators=(", ", ": "))
                      if views[fc].gen_schema is not None else _REALIZE_FREE_TEXT)
                     for fc, _ in steps]
        bucket = bucket_key(plan.llm, plan.style_name, plan.class_name)

        async def realize(span: tuple[int, int]) -> list:
            start, end = span
            system_text, user_text = render_realize_prompt_texts(
                gen_c.instruction, plan.style_prompt, steps[start:end],
                contracts[start:end])
            schema = _realize_schema(schemas[start:end])
            ctx.metrics.count(f"generate.buckets.{bucket}.calls")
            ctx.metrics.count("generate.stream.realize_calls")
            if not self._stream_fits((system_text, user_text), plan.llm, schema):
                raise ContextOverflowError(
                    "realize call unfittable under the input budget",
                    phase="precheck", profile=plan.llm)
            obj, _usage, _attempts, _model = await ctx.schema_engine.complete_validated(
                plan.llm, _text_bundle(system_text, user_text, gen_c.temperature),
                schema=schema, batch_no=ctx.batch_no)
            return list(obj["frames"])

        try:
            leaves = await _realize_degrading(realize, (0, len(steps)), ctx)
            return [frame for leaf in leaves for frame in leaf]
        except CircuitBreakerTripped:
            raise
        except LabelKitError as exc:
            self._void_stream_sequence(plan, exc, "realize", ctx)
            return None

    async def _stream_noise_call(self, plan: NoiseCallPlan,
                                 ctx: "RunContext") -> list[str] | None:
        """噪音批量实现：复用平面生成模板与 samples_schema（裁决·噪音只做插入与
        重复）；作废 ⇒ None，缺额帧从交织缺席（不补生成）。"""
        g = self._cfg.generate
        instruction = self._cfg.generate_stream.noise_instruction
        system_text, user_text = render_prompt_texts(instruction, plan.style_prompt,
                                                     g.num_per_call, ())
        schema = _samples_schema(g.num_per_call)
        bucket = bucket_key(plan.llm, plan.style_name)
        ctx.metrics.count(f"generate.buckets.{bucket}.calls")
        ctx.metrics.count("generate.stream.noise_calls")
        if not self._stream_fits((system_text, user_text), plan.llm, schema):
            _log.warning("noise call voided: unfittable under the input budget "
                         "call=%d llm=%s", plan.index, plan.llm,
                         extra={"stage": self.name, "batch": ctx.batch_no})
            return None
        prompt = build_generate_prompt(instruction, plan.style_prompt,
                                       g.num_per_call, (), g.temperature)
        try:
            obj, _usage, _attempts, _model = await ctx.schema_engine.complete_validated(
                plan.llm, prompt, schema=schema, batch_no=ctx.batch_no)
            samples = [str(sample) for sample in obj["samples"]]
            ctx.metrics.count(f"generate.buckets.{bucket}.produced", len(samples))
            return samples
        except CircuitBreakerTripped:
            raise
        except LabelKitError as exc:
            budget.feed_reactive_terminal(exc, ctx.metrics)
            _log.warning("noise call voided: call=%d llm=%s kind=%s", plan.index,
                         plan.llm, _error_kind(exc),
                         extra={"stage": self.name, "batch": ctx.batch_no})
            return None

    def _stream_fits(self, texts: tuple[str, str], llm: str, schema: dict) -> bool:
        """``_fit_plan_seeds`` 先例的时间流预检：est(system) + est(user) + 2×消息
        包封 ≤ 输入预算；supports_structured_output 时 response_schema 文本另计
        （L0 上 schema 随请求上行；提示词内嵌 schema 文本已在 est(system) 内恒计）。
        预算未声明（profile 缺失 / cw == 0）恒可装填。"""
        prof = self._cfg.llm_profiles.get(llm)
        if prof is None or prof.context_window <= 0:
            return True
        available = budget.input_budget(prof)
        if prof.supports_structured_output:
            available -= budget.est_text(json.dumps(schema, ensure_ascii=False))
        system_text, user_text = texts
        est = (budget.est_text(system_text) + budget.est_text(user_text)
               + 2 * budget.MSG_OVERHEAD_TOKENS)
        return est <= available

    def _void_stream_sequence(self, plan: SequencePlan, exc: LabelKitError,
                              call_kind: str, ctx: "RunContext") -> None:
        """作废一条序列（蓝图/实现失败语义，平面路径作废同款）：计
        generate.stream.<call_kind>_failures、A7 恰一次熔断喂给（仅 reactive-400
        终局；precheck 与 200 形终局永不喂）、值-free stderr 一行；不产 failed
        记录、不写 StageError。"""
        ctx.metrics.count(f"generate.stream.{call_kind}_failures")
        budget.feed_reactive_terminal(exc, ctx.metrics)
        message = (f"stream sequence voided: seq={plan.index} "
                   f"class={plan.class_name} llm={plan.llm} call={call_kind} "
                   f"kind={_error_kind(exc)}")
        if isinstance(exc, SchemaViolation):
            message += f" violations={len(exc.errors)}"
        _log.warning(message, extra={"stage": self.name, "batch": ctx.batch_no})

    def _stream_frames_valid(self, plan: SequencePlan, payloads: Sequence,
                             ctx: "RunContext") -> bool:
        """``sample_validator`` 逐帧执行（裁决·生成键效力矩阵）：任一帧违规 ⇒ 整
        序列作废（蓝图定长不可剔单帧，拒绝采样语义）计 validator_scrapped 与桶
        rejected_by_validator；回调抛异常视同违规（平面路径同款兜底）。未配置钩子
        恒 True。"""
        hook_ref = self._cfg.generate.sample_validator
        if not hook_ref:
            return True
        from labelkit.common.extensions.hooks import normalize_violations, resolve_hook

        hook = resolve_hook(hook_ref)
        for position, payload in enumerate(payloads):
            try:
                violations = normalize_violations(hook(_payload_text(payload)),
                                                  hook_ref)
            except Exception as exc:  # 钩子缺陷：按违规作废本序列，绝不逸出批级
                _log.warning(
                    "generate.sample_validator raised on a stream frame; the "
                    "sequence is scrapped: %s: %s", type(exc).__name__, exc,
                    extra={"stage": self.name, "batch": ctx.batch_no})
                violations = ["callback raised"]
            if violations:
                bucket = bucket_key(plan.llm, plan.style_name, plan.class_name)
                ctx.metrics.count(f"generate.buckets.{bucket}.rejected_by_validator")
                ctx.metrics.count("generate.stream.validator_scrapped")
                _log.warning(
                    "stream sequence scrapped by sample_validator: seq=%d "
                    "class=%s frame=%d violations=%d", plan.index,
                    plan.class_name, position, len(violations),
                    extra={"stage": self.name, "batch": ctx.batch_no})
                return False
        return True

    def _filter_stream_sequences(self, realized: list[RealizedSequence],
                                 ctx: "RunContext") -> list[RealizedSequence]:
        """序列级相似度过滤（裁决·序列相似度过滤）：判重文本 = 成员 text 按序
        "\\x1e" 拼接（M3 序列配方同式）、比对面 = 兄弟序列（无种子）、参数取
        [dedup] 三键；淘汰以 survived_dedup 桶差呈现。幸存序列保持计划序。"""
        d = self._cfg.dedup
        filt = SimilarityFilter(threshold=d.minhash_threshold,
                                num_perm=d.minhash_num_perm, ngram=d.ngram)
        survivors: list[RealizedSequence] = []
        for seq in realized:
            probe = "\x1e".join(_payload_text(p) for p in seq.payloads)
            bucket = bucket_key(seq.plan.llm, seq.plan.style_name,
                                seq.plan.class_name)
            if not filt.probe_and_add(probe):
                _log.info("stream sequence eliminated by the similarity filter: "
                          "seq=%d class=%s", seq.plan.index, seq.plan.class_name,
                          extra={"stage": self.name, "batch": ctx.batch_no})
                continue
            ctx.metrics.count(f"generate.buckets.{bucket}.survived_dedup")
            survivors.append(seq)
        return survivors

    def _count_stream_product(self, survivors: Sequence[RealizedSequence],
                              stats: "Mapping", ctx: "RunContext") -> None:
        """report.generate.stream 供数（counts-only；键集 = 裁决·观测面；planned
        已在计划期计数，本处补交织统计与按类 produced）。"""
        for key in ("sessions", "crossed_sessions", "frames", "noise_frames",
                    "duplicates"):
            if stats[key]:
                ctx.metrics.count(f"generate.stream.{key}", stats[key])
        for seq in survivors:
            ctx.metrics.count(
                f"generate.stream.sequences.{seq.plan.class_name}.produced")

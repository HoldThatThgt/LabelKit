"""M6 generate — synthesize new text records from seeds (spec 3.6, CONTRACTS §7.5).

Process mode: seeds are the current batch's quality-gate survivors; ``run()`` returns a
sub-batch of new PipelineItems (the input batch is never touched). generate_only mode
(v1.4): ``generate_all()`` produces every Record up front from the ``generate.seed_examples``
pool or, seedless, from ``generate.instruction`` × styles with a ``standalone_count`` target.

All randomness comes from ``ctx.rng``; the full (llm, style) assignment and the per-call
seed draws are made in call-index order BEFORE any dispatch so results are independent of
concurrency scheduling (spec 3.6.2). New samples pass a MinHash similarity filter against
the seeds and against each other (Self-Instruct filter, threshold = dedup.minhash_threshold).
"""
from __future__ import annotations

import asyncio
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

from labelkit.errors import (
    CircuitBreakerTripped,
    ErrorKind,
    LabelKitError,
    ProviderFatalError,
    ProviderRetryableError,
    SchemaViolation,
)
from labelkit.types import PipelineItem, Record, RecordRef

if TYPE_CHECKING:
    import random

    from labelkit.config.model import GenerateConfig, GenerateStyle, ResolvedConfig
    from labelkit.llm_client import PromptBundle
    from labelkit.stage import RunContext

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


def bucket_key(llm: str, style: str | None) -> str:
    """Report bucket key ``<llm>×<style|null>`` (CONTRACTS §7.5 [FROZEN])."""
    return f"{llm}×{style if style is not None else 'null'}"


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
    from labelkit.llm_client import Message, Part, PromptBundle

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
    from labelkit.schema_engine import samples_schema

    return samples_schema(num_per_call)


# ── pre-drawn call plan (spec 3.6.2 多模型混合 / 风格条件化) ────────────────

@dataclass(frozen=True)
class CallPlan:
    index: int                          # call index 0..C-1
    llm: str                            # [llm.*] profile name
    style_name: str | None
    style_prompt: str | None
    seed_ids: tuple[str, ...]           # process mode: sampled seed record ids; else ()
    seed_texts: tuple[str, ...]         # sampled seed texts ((), seedless form)


def predraw_llm_style(g: "GenerateConfig", num_calls: int,
                      rng: "random.Random") -> list[tuple[str, "GenerateStyle | None"]]:
    """Pre-draw the (llm, style) pair for every call index 0..num_calls-1 with ctx.rng.

    round_robin: llms[i % len(llms)] (no RNG consumed for the llm);
    weighted: rng.choices per index; style: uniform rng.choice per index when styles set.
    """
    pairs: list[tuple[str, "GenerateStyle | None"]] = []
    for i in range(num_calls):
        if g.mixture == "weighted":
            llm = rng.choices(list(g.llms), weights=list(g.weights), k=1)[0]
        else:
            llm = g.llms[i % len(g.llms)]
        style = rng.choice(g.styles) if g.styles else None
        pairs.append((llm, style))
    return pairs


def build_call_plans(g: "GenerateConfig", seeds: Sequence[tuple[str | None, str]],
                     num_calls: int, rng: "random.Random",
                     exec_calls: int | None = None) -> list[CallPlan]:
    """Full pre-dispatch plan: (llm, style) for ALL num_calls indexes first (so --limit
    truncation does not disturb the draw stream), then seed draws per executed call in
    call-index order. seeds = (record_id_or_None, text) pairs; empty = seedless form."""
    if exec_calls is None:
        exec_calls = num_calls
    exec_calls = min(exec_calls, num_calls)
    pairs = predraw_llm_style(g, num_calls, rng)
    plans: list[CallPlan] = []
    for i in range(exec_calls):
        llm, style = pairs[i]
        if seeds:
            k = min(g.seeds_per_call, len(seeds))
            drawn = rng.sample(list(seeds), k)
        else:
            drawn = []
        plans.append(CallPlan(
            index=i,
            llm=llm,
            style_name=style.name if style else None,
            style_prompt=style.prompt if style else None,
            seed_ids=tuple(sid for sid, _ in drawn if sid is not None),
            seed_texts=tuple(text for _, text in drawn),
        ))
    return plans


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


# ── seed selection (process mode, spec 3.6.2 种子选取) ─────────────────────

def select_seeds(batch: Sequence[PipelineItem], cfg: "ResolvedConfig") -> list[tuple[str, str]]:
    """Active items whose aggregate score >= generate.seed_min_score (default:
    quality.threshold, else the batch median aggregate). Unscored items never seed."""
    scored: list[tuple[PipelineItem, float]] = []
    for item in batch:
        if item.status != "active":
            continue
        agg = item.scores.get("__aggregate__")
        if agg is None or agg.score is None:
            continue
        scored.append((item, agg.score))
    if not scored:
        return []
    threshold = cfg.generate.seed_min_score
    if threshold is None:
        threshold = cfg.quality.threshold
    if threshold is None:
        threshold = statistics.median(s for _, s in scored)
    return [(item.record.id, item.record.text or "")
            for item, score in scored if score >= threshold]


# ── post-processing: filter + record construction + bucket stats ───────────

def postprocess_samples(plans: Sequence[CallPlan],
                        results: Sequence[list[str] | None],
                        seed_texts: Sequence[str],
                        cfg: "ResolvedConfig",
                        metrics) -> list[Record]:
    """Deterministic post-dispatch assembly, processed in call-index order.

    ``results[i]`` is the sample list of call i, or None for a voided call (invalid after
    M8 repair / retries exhausted): its bucket counts ``calls`` with ``produced`` 0 and no
    failed record is created (spec 3.6.3). Bucket counters (CONTRACTS §9.3):
    calls = dispatched calls; produced = samples returned by the LLM; survived_dedup =
    samples surviving the MinHash similarity filter (only those become Records)."""
    d = cfg.dedup
    filt = SimilarityFilter(threshold=d.minhash_threshold,
                            num_perm=d.minhash_num_perm, ngram=d.ngram)
    for text in seed_texts:
        filt.add(text)
    records: list[Record] = []
    for plan, samples in zip(plans, results):
        key = bucket_key(plan.llm, plan.style_name)
        metrics.count(f"generate.buckets.{key}.calls")
        if samples is None:
            continue
        metrics.count(f"generate.buckets.{key}.produced", len(samples))
        for sample in samples:
            if not filt.probe_and_add(sample):
                continue
            rec = make_generated_record(sample, cfg.input.text_field,
                                        plan.seed_ids, plan.llm, plan.style_name)
            metrics.count(f"generate.buckets.{key}.survived_dedup")
            # NOTE: counts.generated is owned by M10 (orchestrator), which counts
            # the records it receives from generate_all/GenerateStage. Incrementing
            # it here as well would double-count in report.counts (§9.3 invariant).
            records.append(rec)
    return records


def _error_kind(exc: LabelKitError) -> str:
    if isinstance(exc, SchemaViolation):
        return ErrorKind.SCHEMA_VIOLATION.value
    if isinstance(exc, ProviderRetryableError):
        return ErrorKind.PROVIDER_RETRYABLE_EXHAUSTED.value
    if isinstance(exc, ProviderFatalError):
        return ErrorKind.PROVIDER_FATAL.value
    return ErrorKind.INTERNAL_ERROR.value


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


# ── the stage ──────────────────────────────────────────────────────────────

class GenerateStage:
    name = "generate"

    def __init__(self, cfg: "ResolvedConfig"):
        self._cfg = cfg

    async def run(self, batch: list[PipelineItem], ctx: "RunContext") -> list[PipelineItem]:
        """PROCESS MODE. Returns the sub-batch of NEW PipelineItems (input batch untouched).
        A generation call that is invalid after M8 repair or exhausts retries is voided
        (bucket ``calls`` counted, ``produced`` 0); no failed records are created; seed
        records are unaffected."""
        g = self._cfg.generate
        seeds = select_seeds(batch, self._cfg)
        if not seeds:
            return []
        num_calls = math.ceil(len(seeds) * g.num_per_record / g.num_per_call)
        records = await self._generate(
            [(sid, text) for sid, text in seeds], num_calls, ctx, limit=None)
        return [PipelineItem(record=rec) for rec in records]

    async def generate_all(self, ctx: "RunContext") -> list[Record]:
        """GENERATE_ONLY MODE entry (called once by M10 before batching; ctx.batch_no == 0,
        ctx.rng == Random(f"{seed}:0:generate")). Executes all calls per the 3.6.2 count
        formulas; --limit truncates to the first ceil(limit / num_per_call) calls in
        pre-drawn order and then to limit records."""
        g = self._cfg.generate
        if g.seed_examples:
            seeds: list[tuple[str | None, str]] = [(None, s) for s in g.seed_examples]
            num_calls = math.ceil(len(seeds) * g.num_per_record / g.num_per_call)
        else:
            seeds = []
            num_calls = math.ceil((g.standalone_count or 0) / g.num_per_call)
        return await self._generate(seeds, num_calls, ctx, limit=self._cfg.limit)

    async def _generate(self, seeds: Sequence[tuple[str | None, str]], num_calls: int,
                        ctx: "RunContext", limit: int | None) -> list[Record]:
        g = self._cfg.generate
        exec_calls = num_calls
        if limit is not None:
            exec_calls = min(num_calls, math.ceil(limit / g.num_per_call))
        # All draws happen in call-index order before dispatch (spec 3.6.2).
        plans = build_call_plans(g, seeds, num_calls, ctx.rng, exec_calls=exec_calls)
        schema = _samples_schema(g.num_per_call)

        async def one_call(plan: CallPlan) -> list[str] | None:
            prompt = build_generate_prompt(g.instruction, plan.style_prompt,
                                           g.num_per_call, plan.seed_texts, g.temperature)
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
                _log.warning(void_log_message(plan, exc),
                             extra={"stage": self.name, "batch": ctx.batch_no})
                return None

        results = await asyncio.gather(*(one_call(p) for p in plans))
        seed_texts = [text for _, text in seeds]
        records = postprocess_samples(plans, list(results), seed_texts,
                                      self._cfg, ctx.metrics)
        if limit is not None:
            records = records[:limit]
        return records

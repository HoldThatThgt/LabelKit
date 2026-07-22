"""M13 classify stage (spec 3.13, CONTRACTS.md §7.13).

Closed-set LLM classification of active, not-yet-classified items against the user
class table: deterministic prompt assembly (CONTRACTS §10.8), the M8 internal-schema
guarantee (schema_engine.classification_schema — no resolved_at counting, no L2.5),
deterministic post-validation label normalization, optional self-consistency voting
(own voting rules — NOT annotate._majority_vote, R26), the on_error fallback/fail
policy (R4: fallback evidence lives in Classification.detail, never in item.errors),
and multi-assignment sibling fan-out appended in place to the batch tail (Stage
contract ②a). Chain position: dedup → classify → quality.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Mapping, Sequence

from labelkit.common.errors import (
    CircuitBreakerTripped,
    ContextOverflowError,
    ErrorKind,
    OutputTruncatedError,
    ProviderFatalError,
    ProviderRetryableError,
    SchemaViolation,
)
from labelkit.common.contracts.types import (
    Classification,
    PipelineItem,
    Record,
    StageError,
    Usage,
    frame_digest,
)

from labelkit.common.runtime import budget
from labelkit.common.runtime.llm_client import Message, Part, PromptBundle
from labelkit.common.runtime.schema_engine import classification_schema

if TYPE_CHECKING:
    from labelkit.common.config.model import ClassifyConfig, ResolvedConfig
    from labelkit.common.contracts.stage import RunContext


# Event names (exact strings per CONTRACTS.md §7.13 / §8.1).
_EV_DECISION = "classify.decision"
_EV_ERROR = "error"

# Counter keys owned by M13 (CONTRACTS.md §9.3; counts.fanout is metered by M10).
_COUNTER_CLASSES_PREFIX = "classify.classes."
_COUNTER_FALLBACK = "classify.fallback"
_COUNTER_FAILURES = "classify.failures"
_COUNTER_MULTI_LABEL = "classify.multi_label_records"

# Chinese prompt fragments — verbatim from CONTRACTS.md §10.8 (spec 3.13.3).
_SYSTEM_HEAD_SINGLE = "你是数据分类员。阅读待分类数据，判断它属于以下类别中的哪一类。类别表："
_SYSTEM_HEAD_MULTI = ("你是数据分类员。阅读待分类数据，判断它适用于以下哪些类别"
                      "（至少 1 类，至多 {max_labels} 类）。类别表：")
_STRUCTURE_SENTENCE = "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容："
_STRUCTURE_SINGLE = '{"class": <类名>}'
_STRUCTURE_SINGLE_REASON = '{"class": <类名>, "reason": <一句话理由>}'
_STRUCTURE_MULTI = '{"classes": [<类名>, ...]}'
_STRUCTURE_MULTI_REASON = '{"classes": [<类名>, ...], "reason": <一句话理由>}'
_LABEL_EXAMPLE_TMPL = "[类别示例·{name}] {example}"
_LABEL_RECORD = "[待分类数据]"
_LABEL_SCREENSHOT = "[屏幕截图]"
_LABEL_UI_TREE = "[UI 控件树]"
# v1.8 sequence variant labels + truncation marker (CONTRACTS §10.8 [FROZEN HERE]).
_LABEL_RECORD_SEQ = "[待分类数据·序列]"
_LABEL_FIRST_FRAME = "[首帧截图]"
_SEQ_TRUNCATION_MARKER = "…(truncated {n} members)"


def _reason_requested(cfg: "ResolvedConfig") -> bool:
    """R29: reason is requested iff trace.enabled and "classify" ∈ trace.channels."""
    return cfg.trace.enabled and "classify" in cfg.trace.channels


# ── v1.11 context-budget packing (spec 3.13.4 上下文预算装填 row) ────────────

_TREE_MARKER_RE = re.compile(r"^…\(truncated (\d+) nodes\)$")


@dataclass
class _PromptFit:
    """Record-side packing state for ONE classification call (spec 3.13.4 v1.11
    row). ``input_budget`` = input_budget(classify profile) minus the
    structured-output schema est when the profile rides it; ``image_cost`` = the
    calibrated per-image readout (batch-frozen, V19) — 0 when the prompt carries
    no image. The single trimmable slot is the current-record tree render
    (single UI) or the episode digest body (sequence, same-family cap); class
    table / instruction / class examples are static user semantic assets — V13③
    M1-precheck territory, NEVER trimmed dynamically."""
    input_budget: int
    image_cost: int
    truncations: int = 0
    overflow: bool = False


def _fit_tree_text(rendered: str, budget_tokens: int) -> tuple[str, bool]:
    """§3.3③ dynamic cap on a serialized UI tree: the render (already under the
    absolute input.ui_tree_max_chars cap) is re-checked with est_text; over the
    share, trailing NODE lines are dropped and the serialize-family marker
    "…(truncated N nodes)" closes the text — N accumulates onto an existing
    marker's count, so the marker semantics stay UITree.serialize's own.
    Deterministic; est_text is prefix-monotone so the largest fitting prefix is
    found by bisection. Returns (text, trimmed)."""
    if budget.est_text(rendered) <= budget_tokens:
        return rendered, False
    lines = rendered.split("\n")
    base = 0
    m = _TREE_MARKER_RE.match(lines[-1])
    if m is not None:
        base = int(m.group(1))
        lines = lines[:-1]
    total = len(lines)

    def candidate(keep: int) -> str:
        marker = f"…(truncated {base + total - keep} nodes)"
        return "\n".join(lines[:keep] + [marker])

    lo, hi = 0, total - 1                        # keep == total is known not to fit
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if budget.est_text(candidate(mid)) <= budget_tokens:
            lo = mid
        else:
            hi = mid - 1
    return candidate(lo), True


def _fit_digest_body(body: str, budget_tokens: int) -> tuple[str, bool]:
    """§3.3③ same-family cap on the sequence digest body: the char-capped
    _sequence_digest_block output is re-checked with est_text; over the share,
    MIDDLE member lines are dropped (first/last always kept) and the frozen
    "…(truncated N members)" marker closes the block — the block's own §10.8
    truncation convention (marker after the last member line), with N
    accumulating onto an existing marker's count. Returns (text, trimmed)."""
    if budget.est_text(body) <= budget_tokens:
        return body, False
    lines = body.split("\n")
    base = 0
    m = re.match(r"^…\(truncated (\d+) members\)$", lines[-1])
    if m is not None:
        base = int(m.group(1))
        lines = lines[:-1]
    n = len(lines)
    if n > 2:
        for keep_middle in range(n - 3, -1, -1):
            marker = _SEQ_TRUNCATION_MARKER.format(n=base + n - 2 - keep_middle)
            cand = "\n".join(lines[: 1 + keep_middle] + [lines[-1], marker])
            if budget.est_text(cand) <= budget_tokens:
                return cand, True
    return _SEQ_TRUNCATION_MARKER.format(n=base + n), True


def _feed_reactive_terminal(exc: BaseException, metrics) -> None:
    """A7/§7.8 breaker matrix: ONLY the reactive-400 (body-sniff) overflow
    terminal feeds the fatal streak — exactly once per exception object (the
    duck flag guards double-feeds when one exception crosses operators);
    precheck and the 200-shaped finish oracle never feed. ``origin`` is read
    defensively pending the errors.py revision (default "http_400")."""
    if (isinstance(exc, ContextOverflowError) and exc.phase == "reactive"
            and getattr(exc, "origin", "http_400") == "http_400"
            and not getattr(exc, "_breaker_fed", False)):
        exc._breaker_fed = True  # type: ignore[attr-defined]
        metrics.record_provider_result(fatal=True)


def _prompt_fit(record: Record, cfg: "ResolvedConfig", ctx: "RunContext",
                schema: dict) -> _PromptFit | None:
    """None = budget OFF (profile missing or context_window == 0 — v1.10
    behavior byte-identical). The schema est is charged only when the profile
    declares supports_structured_output (the M9 throat sends it only then)."""
    prof = cfg.llm_profiles.get(cfg.classify.llm)
    if prof is None or prof.context_window <= 0:
        return None
    b = budget.input_budget(prof)
    if prof.supports_structured_output:
        b -= budget.est_text(json.dumps(schema, ensure_ascii=False))
    # Both the single-UI record and the UI episode carry exactly ONE image
    # ([屏幕截图] / [首帧截图]); text modality carries none.
    cost = ctx.llm.calibrator.cost(prof.name) if record.modality == "ui" else 0
    return _PromptFit(input_budget=b, image_cost=cost)


def _sequence_digest_block(record: Record, cfg: "ResolvedConfig") -> str:
    """Episode digest body of the §10.8 sequence variant (spec 3.13.3 sequence row).

    One line per member in member order — "{m}. {frame_digest(member,
    segment.digest_max_chars)}" with a 1-based ordinal — TOTAL capped at
    input.ui_tree_max_chars. Over the cap, whole MIDDLE lines are dropped (first and
    last members always kept, the surviving ordinals expose the gap) and the capped
    output ends with the frozen marker line "…(truncated N members)" where N = number
    of member lines omitted (UITree.serialize truncation convention)."""
    max_chars = cfg.input.ui_tree_max_chars
    lines = [f"{m}. {frame_digest(member, cfg.segment.digest_max_chars)}"
             for m, member in enumerate(record.members, start=1)]
    full = "\n".join(lines)
    if len(full) <= max_chars:
        return full

    n = len(lines)
    # prefix_len[k] = len("\n".join(lines[:k])) — serialize's prefix-sum scheme.
    prefix_len = [0] * (n + 1)
    for i, line in enumerate(lines):
        prefix_len[i + 1] = prefix_len[i] + (1 if i else 0) + len(line)
    last_len = len(lines[-1])
    # Keep the first line, the longest possible prefix of middle lines, and the last
    # line; at least one middle line must go (we are over the cap), so the kept middle
    # count ranges over [0, n-3] and the marker always closes the block.
    for keep_middle in range(n - 3, -1, -1):
        marker = _SEQ_TRUNCATION_MARKER.format(n=n - 2 - keep_middle)
        total = (prefix_len[1 + keep_middle] + 1 + last_len + 1 + len(marker))
        if total <= max_chars:
            return "\n".join(lines[: 1 + keep_middle] + [lines[-1], marker])
    # Degenerate cap (not even first + last + marker fits, or n <= 2): serialize's
    # final tier — the marker alone stands in for every member.
    return _SEQ_TRUNCATION_MARKER.format(n=n)


def build_classify_prompt(record: Record, cfg: "ResolvedConfig",
                          with_reason: bool) -> PromptBundle:
    """Deterministic assembly of the CONTRACTS §10.8 template.

    system (single/multi variant head, class table in [[classify.classes]]
    declaration order, optional classify.instruction line, structure line with or
    without the reason fragment), one user message per configured class example
    (class declaration order, then array order), then the current-record user
    message — text part, or the §10.1-shaped three-part screenshot + tree form (R27).

    v1.8 sequence records (record.kind == "sequence", spec 3.13.3 sequence row):
    system and few-shot messages unchanged; the current-record message becomes the
    §10.8 sequence variant — the [待分类数据·序列] episode digest block, plus (UI
    modality only — classify stays in the vision reference set) the [首帧截图] label
    and the first member's screenshot image part.

    v1.11: the frozen signature stays intact — the budget path enters through the
    private assembler's trailing ``fit`` parameter (classify_record), never here.
    """
    return _assemble_classify(record, cfg, with_reason)


def _assemble_classify(record: Record, cfg: "ResolvedConfig", with_reason: bool,
                       fit: _PromptFit | None = None) -> PromptBundle:
    """The §10.8 assembly body; ``fit`` non-None applies the spec 3.13.4 v1.11
    packing — the current-record slot budget = fit.input_budget − (system +
    class-example texts + record-part label overheads + message envelopes +
    image cost), spent on the ONE trimmable block; the closing whole-prompt est
    re-check sets fit.overflow (V10 — the caller records the reject). fit=None
    is the byte-identical v1.10 path."""
    c = cfg.classify
    lines: list[str] = []
    if c.assignment == "single":
        lines.append(_SYSTEM_HEAD_SINGLE)
    else:
        lines.append(_SYSTEM_HEAD_MULTI.format(max_labels=c.max_labels))
    for spec in c.classes:
        lines.append(f"- {spec.name}: {spec.description}")
    if c.instruction:
        lines.append(c.instruction)
    lines.append(_STRUCTURE_SENTENCE)
    if c.assignment == "single":
        lines.append(_STRUCTURE_SINGLE_REASON if with_reason else _STRUCTURE_SINGLE)
    else:
        lines.append(_STRUCTURE_MULTI_REASON if with_reason else _STRUCTURE_MULTI)

    messages: list[Message] = [
        Message(role="system", parts=(Part(kind="text", text="\n".join(lines)),))]

    for spec in c.classes:
        for example in spec.examples:
            text = _LABEL_EXAMPLE_TMPL.format(name=spec.name, example=example)
            messages.append(Message(role="user", parts=(Part(kind="text", text=text),)))

    # v1.11 slot budget: everything OUTSIDE the trimmable block is counted first
    # (static system side + label overheads + envelopes + calibrated image cost).
    slot_budget: int | None = None
    if fit is not None:
        n_messages = len(messages) + 1
        n_images = 1 if record.modality == "ui" else 0
        static_est = (sum(budget.est_text(m.parts[0].text or "") for m in messages)
                      + budget.MSG_OVERHEAD_TOKENS * n_messages
                      + n_images * fit.image_cost)
        slot_budget = fit.input_budget - static_est

    if record.kind == "sequence":
        # v1.8 sequence variant (§10.8): digest text part first; UI modality appends
        # the [首帧截图] label + the FIRST member's image (encoded by M9 at call time).
        # Text-modality sequences carry the digest part only.
        digest_block = _sequence_digest_block(record, cfg)
        if slot_budget is not None:
            head_est = budget.est_text(f"{_LABEL_RECORD_SEQ}\n")
            if record.modality == "ui":
                head_est += budget.est_text(_LABEL_FIRST_FRAME)
            digest_block, trimmed = _fit_digest_body(
                digest_block, max(0, slot_budget - head_est))
            if trimmed:
                fit.truncations += 1
        parts: tuple[Part, ...] = (
            Part(kind="text", text=f"{_LABEL_RECORD_SEQ}\n{digest_block}"),
        )
        if record.modality == "ui":
            parts += (
                Part(kind="text", text=_LABEL_FIRST_FRAME),
                Part(kind="image", image=record.members[0].image),
            )
    elif record.modality == "text":
        parts = (
            Part(kind="text", text=f"{_LABEL_RECORD} {record.text}"),
        )
    else:  # UI modality: three parts in one user message (same shape as §10.1, R27)
        tree_text = record.ui_tree.serialize(max_chars=cfg.input.ui_tree_max_chars)
        if slot_budget is not None:
            label_est = (budget.est_text(_LABEL_SCREENSHOT)
                         + budget.est_text(f"{_LABEL_UI_TREE}\n"))
            tree_text, trimmed = _fit_tree_text(
                tree_text, max(0, slot_budget - label_est))
            if trimmed:
                fit.truncations += 1
        parts = (
            Part(kind="text", text=_LABEL_SCREENSHOT),
            Part(kind="image", image=record.image),
            Part(kind="text", text=f"{_LABEL_UI_TREE}\n{tree_text}"),
        )
    messages.append(Message(role="user", parts=parts))
    bundle = PromptBundle(messages=tuple(messages))
    if fit is not None:
        # Whole-prompt re-check with the SAME estimator the M9 throat runs (the
        # schema est is already folded into fit.input_budget): over ⇒ even the
        # minimal unit (one record) is unfittable — V10, caller rejects.
        est = budget.est_prompt(bundle, None, None, image_cost=fit.image_cost)
        fit.overflow = est > fit.input_budget
    return bundle


# ── post-M8 normalization (deterministic, fixed order) ──────────────────────

def _hit_labels(obj: Mapping, assignment: str) -> tuple[str, ...]:
    """Raw hit set of one M8-validated classification object."""
    if assignment == "single":
        return (obj["class"],)
    return tuple(obj["classes"])


def _normalize_labels(raw: Sequence[str], c: "ClassifyConfig") -> tuple[str, ...]:
    """Spec 3.13.4 normalization: ① map onto class-table declaration order and
    de-duplicate; ② the fallback class co-occurring with concrete classes is
    dropped (a pure-fallback hit is kept). Only narrows an already-validated set
    (schema-side uniqueItems is deliberately absent, R1)."""
    hit = set(raw)
    ordered = [spec.name for spec in c.classes if spec.name in hit]
    if len(ordered) > 1 and c.fallback_class in ordered:
        ordered = [name for name in ordered if name != c.fallback_class]
    return tuple(ordered)


# ── record-level classification path ────────────────────────────────────────

async def classify_record(record: Record, ctx: "RunContext") -> Classification:
    """One record's full classification path incl. self-consistency voting and
    normalization; the on_error policy is applied by the stage layer.
    Raises SchemaViolation / ProviderRetryableError / ProviderFatalError."""
    cfg = ctx.cfg
    c = cfg.classify
    with_reason = _reason_requested(cfg)
    names = [spec.name for spec in c.classes]
    schema = classification_schema(names, c.assignment, c.max_labels, with_reason)
    # v1.11 (spec 3.13.4 v1.11 row): budget-declared profile → the current-record
    # slot packs under the derived share (fit=None keeps v1.10 byte-identical).
    fit = _prompt_fit(record, cfg, ctx, schema)
    prompt = _assemble_classify(record, cfg, with_reason, fit=fit)
    if fit is not None:
        if fit.truncations:
            ctx.metrics.count("budget.truncations.classify", fit.truncations)
        if fit.overflow:
            # V10: even the minimal unit (one record) is unfittable — never send
            # a request doomed to fail; the record goes to rejects (spec 3.13.4:
            # overflow bypasses the fallback class), phase=precheck never feeds
            # the breaker.
            raise ContextOverflowError(
                "classification prompt exceeds the input budget at the minimal "
                "unit (single record)", phase="precheck", profile=c.llm)
    n = c.self_consistency

    if n == 0:
        obj, _usage, _attempts, _model = await ctx.schema_engine.complete_validated(
            c.llm, prompt, schema, record_ids=(record.id,), batch_no=ctx.batch_no)
        labels = _normalize_labels(_hit_labels(obj, c.assignment), c)
        detail: dict = {}
        if with_reason:
            detail["reason"] = obj["reason"]
        return Classification(label=labels[0], labels=labels, source="llm",
                              detail=detail)

    # Self-consistency: n independent samples at classify.sc_temperature, each
    # through the full M8 guarantee; a SchemaViolation sample abstains — the
    # voting denominator stays n (spec 3.13.4).
    sc_prompt = replace(prompt, temperature=c.sc_temperature)

    async def one_sample() -> tuple[dict, Usage, int, str]:
        return await ctx.schema_engine.complete_validated(
            c.llm, sc_prompt, schema, record_ids=(record.id,), batch_no=ctx.batch_no)

    results = await asyncio.gather(*(one_sample() for _ in range(n)),
                                   return_exceptions=True)

    sample_sets: list[tuple[str, ...]] = []
    reasons: list[str] = []
    last_violation: SchemaViolation | None = None
    for res in results:
        if isinstance(res, SchemaViolation):
            last_violation = res                   # this sample abstains
        elif isinstance(res, BaseException):
            raise res                              # provider/internal errors escalate
        else:
            obj = res[0]
            sample_sets.append(_normalize_labels(_hit_labels(obj, c.assignment), c))
            if with_reason:
                reasons.append(obj["reason"])

    if not sample_sets:
        raise last_violation if last_violation is not None else SchemaViolation(
            ["self-consistency: all samples failed"], "")

    # Own voting rules (R26): per-label membership count over the normalized
    # sample sets; keep labels appearing in > n/2 sets. single assignment is the
    # same rule — each sample contributes exactly one label, so "> n/2 sets" is
    # precisely the majority vote; no majority ⇒ fallback class (never "take the
    # first sample" as annotate's field vote does).
    votes = {name: 0 for name in names}
    for labels_ in sample_sets:
        for label in labels_:
            votes[label] += 1
    kept = tuple(name for name in names if votes[name] * 2 > n)
    final = kept if kept else (c.fallback_class,)

    detail = {}
    if with_reason:
        detail["reason"] = reasons[0]              # first valid sample (gather order)
    detail["sc"] = {"n": n,
                    "agreement_ratio": min(votes[label] for label in final) / n}
    return Classification(label=final[0], labels=final, source="llm", detail=detail)


# ── stage ────────────────────────────────────────────────────────────────────

class ClassifyStage:
    name = "classify"

    def __init__(self, cfg: "ResolvedConfig"):
        self.cfg = cfg

    async def run(self, batch: list[PipelineItem], ctx: "RunContext") -> list[PipelineItem]:
        # Idempotency: classification is not None (e.g. generate's "inherited"
        # records on re-flow) is skipped — zero extra calls (spec 3.13.4).
        todo = [item for item in batch
                if item.status == "active" and item.classification is None]
        if todo:
            await asyncio.gather(*(self._classify_item(item, ctx) for item in todo))
            if self.cfg.classify.assignment == "multi":
                # Deterministic fan-out: one synchronous pass AFTER the gather
                # (never inside the coroutines), batch position order → label
                # declaration order (spec 3.13.4 multi 扇出).
                self._fan_out(batch, todo)
        return batch                               # the SAME list object (contract ②a)

    async def _classify_item(self, item: PipelineItem, ctx: "RunContext") -> None:
        record = item.record
        try:
            classification = await classify_record(record, ctx)
        except SchemaViolation as e:
            if self.cfg.classify.on_error == "fail":
                # Transport the raw last model output to M11 for the rejects
                # "full" tier (§9.2) via the duck-typed channel the emitter reads.
                item.raw_last_output = e.raw_last_output  # type: ignore[attr-defined]
                self._fail(item, ctx, ErrorKind.CLASSIFICATION_INVALID.value, str(e),
                           retryable=False)
                return
            classification = self._fallback(item, ctx, str(e))
        except (ContextOverflowError, OutputTruncatedError) as e:
            # v1.11 (V27①): the budget vocabulary routes FIRST — precise kinds,
            # record-level failed → rejects (spec 3.13.4 v1.11 row: overflow
            # bypasses the on_error fallback class). The reactive-400 terminal
            # feeds the breaker exactly once (A7, _feed_reactive_terminal).
            _feed_reactive_terminal(e, ctx.metrics)
            self._fail(item, ctx, budget.classify_stage_error(e), str(e),
                       retryable=False)
            return
        except ProviderRetryableError as e:
            self._fail(item, ctx, ErrorKind.PROVIDER_RETRYABLE_EXHAUSTED.value, str(e),
                       retryable=True)
            return
        except ProviderFatalError as e:
            self._fail(item, ctx, ErrorKind.PROVIDER_FATAL.value, str(e), retryable=False)
            return
        except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:  # noqa: BLE001 — record-level isolation is absolute
            if record.modality == "ui" and isinstance(e, OSError):
                kind = ErrorKind.IMAGE_DECODE_ERROR.value
            else:
                kind = ErrorKind.INTERNAL_ERROR.value
            self._fail(item, ctx, kind, f"{type(e).__name__}: {e}", retryable=False)
            return
        item.classification = classification
        self._register(item, ctx, classification)

    def _fallback(self, item: PipelineItem, ctx: "RunContext",
                  message: str) -> Classification:
        """on_error="fallback" (R4): the record survives on the fallback class —
        evidence goes into Classification.detail, NEVER into item.errors (rejects
        attribution reads errors[0]); plus the error trace event and counter."""
        kind = ErrorKind.CLASSIFICATION_INVALID.value
        ctx.metrics.count(_COUNTER_FALLBACK)
        ctx.metrics.event(_EV_ERROR, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(item.record.id,),
                          payload={"stage": self.name, "kind": kind,
                                   "message": message, "retryable": False})
        fallback = self.cfg.classify.fallback_class
        return Classification(label=fallback, labels=(fallback,), source="fallback",
                              detail={"kind": kind, "message": message})

    def _register(self, item: PipelineItem, ctx: "RunContext",
                  classification: Classification) -> None:
        """Counters + the per-record classify.decision event (fallback included —
        the decision event fires for every classified record, §7.13)."""
        for label in classification.labels:        # counted per label (multi: all)
            ctx.metrics.count(_COUNTER_CLASSES_PREFIX + label)
        if len(classification.labels) >= 2:
            ctx.metrics.count(_COUNTER_MULTI_LABEL)
        payload: dict = {"label": classification.label}
        if self.cfg.classify.assignment == "multi":
            payload["labels"] = list(classification.labels)
        payload["source"] = classification.source
        if "reason" in classification.detail:
            payload["reason"] = classification.detail["reason"]
        if "sc" in classification.detail:
            payload["sc"] = dict(classification.detail["sc"])
        ctx.metrics.event(_EV_DECISION, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(item.record.id,), payload=payload)

    def _fail(self, item: PipelineItem, ctx: "RunContext", kind: str, message: str,
              retryable: bool) -> None:
        err = StageError(stage=self.name, kind=kind, message=message, retryable=retryable)
        item.errors.append(err)
        item.status = "failed"
        if kind == ErrorKind.CONTEXT_OVERFLOW.value:
            ctx.metrics.count("budget.overflow_records")  # V13②: rejected, all phases
        ctx.metrics.count(_COUNTER_FAILURES)
        ctx.metrics.event(_EV_ERROR, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(item.record.id,),
                          payload={"stage": self.name, "kind": kind,
                                   "message": message, "retryable": retryable})

    @staticmethod
    def _fan_out(batch: list[PipelineItem], processed: list[PipelineItem]) -> None:
        """Normalized hit set of k ≥ 2: the original envelope already carries the
        first label; each remaining label clones one sibling appended to the batch
        tail. Clones share record and dedup BY REFERENCE and inherit session_id
        (v1.8: sibling episodes stay addressable for the M7 boundary-margin /
        neighborhood queries, spec 3.13.4) and thread_id (v1.9 T14: a real field —
        thread identity belongs to the record, not the envelope); classification
        swaps label (labels = same full set); scores/annotation/verification/errors
        are fresh default containers (spec 3.13.4)."""
        for item in processed:
            classification = item.classification
            if classification is None or len(classification.labels) < 2:
                continue
            for label in classification.labels[1:]:
                clone = PipelineItem(
                    record=item.record,
                    status="active",
                    classification=replace(classification, label=label),
                    dedup=item.dedup,
                    session_id=item.session_id,
                    thread_id=item.thread_id,
                )
                # v1.8 (D6): session_split / segment_degraded describe the
                # EPISODE's session and segmentation, not the envelope —
                # sibling rows must not contradict the original's _meta.stream.
                # v1.9 (T14): the M16 marks join the loop — seam_indexes drives
                # the sibling's own extract pass, seam_interrupted_by its
                # placeholder text, stitch_fragments its _meta.stream.fragments
                # and annotate quota.
                for mark in ("session_split", "segment_degraded", "seam_indexes",
                             "seam_interrupted_by", "stitch_fragments"):
                    value = getattr(item, mark, None)
                    if value is not None:
                        setattr(clone, mark, value)
                batch.append(clone)

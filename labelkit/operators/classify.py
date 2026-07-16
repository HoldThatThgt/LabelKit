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
from dataclasses import replace
from typing import TYPE_CHECKING, Mapping, Sequence

from labelkit.common.errors import (
    CircuitBreakerTripped,
    ErrorKind,
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
    """
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

    if record.kind == "sequence":
        # v1.8 sequence variant (§10.8): digest text part first; UI modality appends
        # the [首帧截图] label + the FIRST member's image (encoded by M9 at call time).
        # Text-modality sequences carry the digest part only.
        digest_block = _sequence_digest_block(record, cfg)
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
        parts = (
            Part(kind="text", text=_LABEL_SCREENSHOT),
            Part(kind="image", image=record.image),
            Part(kind="text", text=f"{_LABEL_UI_TREE}\n{tree_text}"),
        )
    messages.append(Message(role="user", parts=parts))
    return PromptBundle(messages=tuple(messages))


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
    prompt = build_classify_prompt(record, cfg, with_reason)
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

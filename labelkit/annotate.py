"""M5 annotate stage (spec 3.5, CONTRACTS.md §7.4).

Deterministic prompt assembly (task instruction + few-shot + record content; UI modality
adds screenshot + serialized UI tree), delegation of the structure guarantee to M8
(SchemaEngine.complete_validated), optional self-consistency sampling with field-level
majority vote (spec 3.5.2), and the public repair hooks used by M7 verify.

v1.8 sequence annotation (S5/S6/S28, CONTRACTS §10.1 sequence variant): episode envelopes
(record.kind == "sequence") swap the current-record user message for ① [动作序列] step
lines (omitted entirely when transitions is None) → ② per kept keyframe
[关键帧 {i}/{k}·成员 {m}] text + image (deterministic uniform downsample to
annotate.sequence_frames; text-modality sequences skip ②) → ③ the ALWAYS-PRESENT closing
[成员帧摘要] text part. Template invariant (S6): the final part is ALWAYS the ③ text
section — the repair suffix concatenates onto parts[-1].text with zero repair-code
changes. transitions is the second additive trailing kwarg on the two frozen signatures
(after v1.7's label); None keeps every pre-v1.8 call site byte-identical.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, Sequence

from labelkit.errors import (
    CircuitBreakerTripped,
    ErrorKind,
    ProviderFatalError,
    ProviderRetryableError,
    SchemaViolation,
)
from labelkit.types import (
    Annotation,
    PipelineItem,
    Record,
    StageError,
    Transition,
    Usage,
    frame_digest,
)

from labelkit.llm_client import Message, Part, PromptBundle

if TYPE_CHECKING:
    from labelkit.config.model import ResolvedConfig
    from labelkit.stage import RunContext


EV_ANNOTATE_DONE = "annotate.done"
EV_ERROR = "error"

# Chinese prompt fragments — verbatim from CONTRACTS.md §10.1/§10.5 (spec 3.5.2/3.7.3).
_SCHEMA_SENTENCE = "输出必须是符合以下 JSON Schema 的单个 JSON 对象，不输出任何其他内容："
_LABEL_EXAMPLE_IN = "[示例输入]"
_LABEL_EXAMPLE_OUT = "[示例输出]"
_LABEL_TEXT_RECORD = "[待标注数据]"
_LABEL_SCREENSHOT = "[屏幕截图]"
_LABEL_UI_TREE = "[UI 控件树]"
_LABEL_PREV_OUTPUT = "[上一版标注]"
_LABEL_CRITIQUES = "[审核意见]"
_REPAIR_TAIL = "请修正后重新输出"

# v1.8 sequence-variant fragments (CONTRACTS §10.1 sequence variant, S5/S6).
_LABEL_ACTION_SEQUENCE = "[动作序列]"
_LABEL_MEMBER_DIGESTS = "[成员帧摘要]"

# Operator modules never depend on each other (spec §2.2): M4 quality carries its own
# same-format step-line template (plus the （摘取兜底） fallback suffix); this copy is M5's.
_MEMBER_DIGEST_MAX_CHARS = 400   # per-member frame_digest cap (segment.digest_max_chars default)


def _step_line(transition: Transition) -> str:
    """One [动作序列] line in the §10.1 frozen format
    `{index}. {action_type}（对象: {target|—}；值: {value|—}）{description}`; null
    target/value render as "—". Annotation evidence does NOT carry the （摘取兜底）
    fallback suffix — that S16 separation marker belongs to M4's scoring sections only."""
    action = transition.action
    target = action.get("target")
    value = action.get("value")
    return (f"{transition.index}. {action.get('action_type')}"
            f"（对象: {'—' if target is None else target}；"
            f"值: {'—' if value is None else value}）"
            f"{action.get('description')}")


def _keyframe_indexes(n: int, k: int) -> list[int]:
    """S28 deterministic uniform downsample over n members with cap k
    (annotate.sequence_frames): n <= k keeps every member; otherwise
    idx_i = i*(n-1)//(k-1) for i = 0..k-1 — pure integer arithmetic, zero rng, first and
    last always kept, strictly increasing (no duplicates for n > k)."""
    if n <= k:
        return list(range(n))
    return [i * (n - 1) // (k - 1) for i in range(k)]


def _member_digest_lines(members: tuple[Record, ...], max_total_chars: int) -> list[str]:
    """[成员帧摘要] lines — per member `{m}. {frame_digest(member, 400)}` (m 1-based, member
    order). Total bounded by max_total_chars (input.ui_tree_max_chars): the first and last
    lines are ALWAYS kept; middle entries are dropped WHOLE and replaced in place by one
    `…(truncated N members)` marker line (serialize/§10.8 truncation convention)."""
    lines = [f"{m}. {frame_digest(member, _MEMBER_DIGEST_MAX_CHARS)}"
             for m, member in enumerate(members, start=1)]
    if len(lines) <= 2 or len("\n".join(lines)) <= max_total_chars:
        return lines
    last = lines[-1]
    keep = 1                 # first line survives even if the floor exceeds the budget
    for k in range(len(lines) - 2, 0, -1):
        marker = f"…(truncated {len(lines) - k - 1} members)"
        if len("\n".join(lines[:k] + [marker, last])) <= max_total_chars:
            keep = k
            break
    marker = f"…(truncated {len(lines) - keep - 1} members)"
    return lines[:keep] + [marker, last]


@dataclass(frozen=True)
class RepairContext:
    previous_output: Mapping                       # last annotation object
    critiques_text: str                            # rendered lines "aspect: opinion"
                                                   # (multi-judge: "judge_name/aspect: opinion")


def _dumps(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False)


def build_annotate_prompt(record: Record, cfg: "ResolvedConfig", schema_text: str,
                          repair: RepairContext | None = None,
                          temperature: float | None = None,
                          label: str | None = None,
                          transitions: tuple[Transition, ...] | None = None) -> PromptBundle:
    """Deterministic template assembly per CONTRACTS.md §10.1 (+ §10.5 repair suffix).

    schema_text = SchemaEngine.user_schema_text. Section order is fixed: system (task
    instruction + schema constraint), one user message per few-shot example in configured
    order, then the current-record user message (text part, or UI screenshot + tree parts).
    v1.7 (R2): label non-None → instruction/examples come from
    cfg.class_views[label].annotate; None = global config (pre-v1.7 behavior).
    v1.8 (S5, second additive trailing-kwarg revision of this frozen signature):
    transitions non-None → the §10.1 sequence variant renders the [动作序列] section from
    it; None = section omitted / pre-v1.8 behavior byte-identical. Sequence records
    (record.kind == "sequence") follow the S6 segment order ① [动作序列] → ② kept
    keyframes (text label + image; S28 downsample to annotate.sequence_frames; skipped in
    text modality) → ③ ALWAYS-PRESENT closing [成员帧摘要] text part, so parts[-1] is
    guaranteed text and the repair concatenation below needs zero changes.
    """
    acfg = cfg.class_views[label].annotate if label is not None else cfg.annotate
    messages: list[Message] = []

    system_text = (f"{acfg.instruction}\n"
                   f"{_SCHEMA_SENTENCE}\n"
                   f"{schema_text}")
    messages.append(Message(role="system", parts=(Part(kind="text", text=system_text),)))

    for example in acfg.examples:
        example_text = (f"{_LABEL_EXAMPLE_IN} {example.input}\n"
                        f"{_LABEL_EXAMPLE_OUT} {_dumps(example.output)}")
        messages.append(Message(role="user", parts=(Part(kind="text", text=example_text),)))

    if record.kind == "sequence":  # v1.8 sequence variant (checked BEFORE modality)
        seq_parts: list[Part] = []
        if transitions is not None:  # ① omitted entirely when transitions is None
            steps = "\n".join(_step_line(t) for t in transitions)
            seq_parts.append(Part(kind="text", text=f"{_LABEL_ACTION_SEQUENCE}\n{steps}"))
        if record.modality == "ui":  # ② text sequences degrade to ① + ③
            kept = _keyframe_indexes(len(record.members), cfg.annotate.sequence_frames)
            k = len(kept)
            for i, m_idx in enumerate(kept, start=1):
                member = record.members[m_idx]
                seq_parts.append(Part(kind="text",
                                      text=f"[关键帧 {i}/{k}·成员 {m_idx + 1}]"))
                seq_parts.append(Part(kind="image", image=member.image))
        digests = "\n".join(
            _member_digest_lines(record.members, cfg.input.ui_tree_max_chars))
        seq_parts.append(Part(kind="text", text=f"{_LABEL_MEMBER_DIGESTS}\n{digests}"))
        parts: tuple[Part, ...] = tuple(seq_parts)
    elif record.modality == "text":
        parts = (
            Part(kind="text", text=f"{_LABEL_TEXT_RECORD} {record.text}"),
        )
    else:  # UI modality: three parts in one user message
        tree_text = record.ui_tree.serialize(max_chars=cfg.input.ui_tree_max_chars)
        parts = (
            Part(kind="text", text=_LABEL_SCREENSHOT),
            Part(kind="image", image=record.image),
            Part(kind="text", text=f"{_LABEL_UI_TREE}\n{tree_text}"),
        )

    if repair is not None:
        suffix = (f"{_LABEL_PREV_OUTPUT} {_dumps(repair.previous_output)}\n"
                  f"{_LABEL_CRITIQUES} {repair.critiques_text}\n"
                  f"{_REPAIR_TAIL}")
        last = parts[-1]
        parts = parts[:-1] + (Part(kind="text", text=f"{last.text}\n{suffix}"),)

    messages.append(Message(role="user", parts=parts))
    return PromptBundle(messages=tuple(messages), temperature=temperature)


# ── self-consistency field-level majority vote (spec 3.5.2) ─────────────────

_MISSING = object()          # sentinel: voted property absent from a sample


def _voted_keys(user_schema: Mapping) -> tuple[str, ...]:
    """Top-level properties subject to per-field voting: enum / boolean / integer."""
    keys: list[str] = []
    for key, prop in (user_schema.get("properties") or {}).items():
        if not isinstance(prop, Mapping):
            continue
        if "enum" in prop:
            keys.append(key)
            continue
        t = prop.get("type")
        types = {t} if isinstance(t, str) else set(t or ())
        if types and types <= {"boolean", "integer"}:
            keys.append(key)
    return tuple(keys)


def _field_value(sample: Mapping, key: str) -> object:
    return sample[key] if key in sample else _MISSING


def _freeze(value: object) -> object:
    """Hashable identity for vote counting (enum values are JSON scalars in practice,
    but arbitrary JSON is tolerated)."""
    if value is _MISSING:
        return ("__missing__",)
    if isinstance(value, bool):                    # keep True distinct from 1
        return ("bool", value)
    try:
        hash(value)
        return ("v", value)
    except TypeError:
        return ("json", json.dumps(value, sort_keys=True, ensure_ascii=False))


def _majority_vote(samples: Sequence[Mapping],
                   user_schema: Mapping) -> tuple[Mapping, int, bool]:
    """Field-level majority vote over schema-valid samples (spec 3.5.2).

    enum/boolean/integer top-level properties vote independently (per-field strict mode:
    a value whose count strictly exceeds every other's); all other fields are taken
    wholesale from the FIRST sample whose voted fields all equal the modal combination.
    No strict per-field mode, or no sample matching the modal combination → full
    disagreement: sample #1 is taken entirely.

    Returns (chosen_output, matches, disagreed) where matches = number of samples whose
    voted fields fully equal the FINAL combination (the chosen sample's voted fields) and
    disagreed = True on the full-disagreement fallback path.
    """
    if not samples:
        raise ValueError("_majority_vote requires at least one sample")

    voted = _voted_keys(user_schema)

    def combo(sample: Mapping) -> tuple:
        return tuple(_freeze(_field_value(sample, k)) for k in voted)

    modal: list[object] = []
    disagreed = False
    for key in voted:
        counts: dict[object, int] = {}
        order: list[object] = []
        for sample in samples:
            fv = _freeze(_field_value(sample, key))
            if fv not in counts:
                order.append(fv)
            counts[fv] = counts.get(fv, 0) + 1
        best = max(counts.values())
        winners = [fv for fv in order if counts[fv] == best]
        if len(winners) != 1:                      # tie → no modal combination
            disagreed = True
            break
        modal.append(winners[0])

    chosen: Mapping | None = None
    if not disagreed:
        target = tuple(modal)
        for sample in samples:
            if combo(sample) == target:
                chosen = sample
                break
        if chosen is None:                         # modal combination matches no sample
            disagreed = True

    if disagreed:
        chosen = samples[0]

    final_combo = combo(chosen)
    matches = sum(1 for sample in samples if combo(sample) == final_combo)
    return chosen, matches, disagreed


# ── record-level annotation path (public repair hook for M7) ────────────────

async def annotate_record(record: Record, ctx: "RunContext",
                          repair: RepairContext | None = None,
                          label: str | None = None,
                          transitions: tuple[Transition, ...] | None = None) -> Annotation:
    """One record's full annotation path incl. self-consistency (skipped when repair is
    not None: repair re-annotation is always a single call at profile-default temperature).
    Raises SchemaViolation / ProviderRetryableError / ProviderFatalError.
    v1.7 (R2): label is passed through to build_annotate_prompt (class-effective
    instruction/examples); llm/self_consistency/sc_temperature stay global (whitelist).
    v1.8 (S5, additive trailing kwarg): transitions is passed through to
    build_annotate_prompt on every path (single call, each self-consistency sample, and
    repair re-annotation — M7 threads the REBUILT value after member surgery); None =
    pre-v1.8 behavior. Sequence records carry raw = None, so the L2.5 callback receives
    record=None (documented limitation)."""
    cfg = ctx.cfg
    profile = cfg.annotate.llm
    schema_text = ctx.schema_engine.user_schema_text
    n = cfg.annotate.self_consistency

    if repair is not None or n == 0:
        prompt = build_annotate_prompt(record, cfg, schema_text, repair=repair,
                                       temperature=None, label=label,
                                       transitions=transitions)
        obj, usage, attempts, model = await ctx.schema_engine.complete_validated(
            profile, prompt, record_ids=(record.id,), batch_no=ctx.batch_no,
            record=record.raw)
        return Annotation(output=obj, model=model, attempts=attempts, usage=usage)

    # Self-consistency: n independent samples at sc_temperature, each through the full
    # M8 guarantee; a SchemaViolation sample abstains (denominator stays n).
    async def one_sample() -> tuple[dict, Usage, int, str]:
        prompt = build_annotate_prompt(record, cfg, schema_text, repair=None,
                                       temperature=cfg.annotate.sc_temperature,
                                       label=label, transitions=transitions)
        return await ctx.schema_engine.complete_validated(
            profile, prompt, record_ids=(record.id,), batch_no=ctx.batch_no,
            record=record.raw)

    results = await asyncio.gather(*(one_sample() for _ in range(n)),
                                   return_exceptions=True)

    valid: list[tuple[dict, Usage, int, str]] = []
    last_violation: SchemaViolation | None = None
    for res in results:
        if isinstance(res, SchemaViolation):
            last_violation = res                   # this sample abstains
        elif isinstance(res, BaseException):
            raise res                              # provider/internal errors escalate
        else:
            valid.append(res)

    if not valid:
        raise last_violation if last_violation is not None else SchemaViolation(
            ["self-consistency: all samples failed"], "")

    outputs = [obj for obj, _, _, _ in valid]
    chosen, matches, disagreed = _majority_vote(outputs, cfg.user_schema)
    if disagreed:
        ctx.metrics.count("annotate.sc_disagreements")

    total_usage = sum((usage for _, usage, _, _ in valid), Usage())
    total_attempts = sum(attempts for _, _, attempts, _ in valid)
    model = valid[0][3]
    return Annotation(output=chosen, model=model, attempts=total_attempts,
                      usage=total_usage,
                      sc={"n": n, "agreement_ratio": matches / n})


# ── stage ────────────────────────────────────────────────────────────────────

class AnnotateStage:
    name = "annotate"

    def __init__(self, cfg: "ResolvedConfig"):
        self.cfg = cfg

    async def run(self, batch: list[PipelineItem], ctx: "RunContext") -> list[PipelineItem]:
        active = [item for item in batch if item.status == "active"]
        if active:
            await asyncio.gather(*(self._annotate_item(item, ctx) for item in active))
        return batch

    async def _annotate_item(self, item: PipelineItem, ctx: "RunContext") -> None:
        record = item.record
        label = item.classification.label if item.classification else None
        try:
            item.annotation = await annotate_record(record, ctx, label=label,
                                                    transitions=item.transitions)
        except SchemaViolation as e:
            # Transport the raw last model output to M11 for the rejects "full"
            # tier (§9.2) via the duck-typed channel the emitter reads.
            item.raw_last_output = e.raw_last_output  # type: ignore[attr-defined]
            kind = (ErrorKind.CALLBACK_VIOLATION if getattr(e, "callback_only", False)
                    else ErrorKind.SCHEMA_VIOLATION)
            self._fail(item, ctx, kind.value, str(e), retryable=False)
        except ProviderRetryableError as e:
            self._fail(item, ctx, ErrorKind.PROVIDER_RETRYABLE_EXHAUSTED.value, str(e),
                       retryable=True)
        except ProviderFatalError as e:
            self._fail(item, ctx, ErrorKind.PROVIDER_FATAL.value, str(e), retryable=False)
        except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:  # noqa: BLE001 — record-level isolation is absolute
            if record.modality == "ui" and isinstance(e, OSError):
                kind = ErrorKind.IMAGE_DECODE_ERROR.value
            else:
                kind = ErrorKind.INTERNAL_ERROR.value
            self._fail(item, ctx, kind, f"{type(e).__name__}: {e}", retryable=False)
        else:
            payload: dict = {"attempts": item.annotation.attempts}
            if item.annotation.sc is not None:
                payload["sc"] = dict(item.annotation.sc)
            if self.cfg.classify.enabled and label is not None:  # v1.7 R5
                payload["label"] = label
            excerpt = self._excerpt_payload(record)
            if excerpt is not None:
                payload["excerpt"] = excerpt
            ctx.metrics.event(EV_ANNOTATE_DONE, stage=self.name, batch_no=ctx.batch_no,
                              record_ids=(record.id,), payload=payload)

    def _excerpt_payload(self, record: Record) -> dict | None:
        """`excerpt` payload addition for the annotate.done event. §7.4: the four
        trace.content tiers are cumulative ("逐档递增") — "full" includes everything
        from "excerpt", so the excerpt is attached at both tiers."""
        if not (self.cfg.trace.enabled and self.cfg.trace.content in ("excerpt", "full")):
            return None
        return {record.id: self._excerpt(record)}

    @staticmethod
    def _excerpt(record: Record) -> str:
        content = record.text if record.modality == "text" else (
            record.ui_tree.serialize() if record.ui_tree is not None else "")
        return (content or "")[:200]

    def _fail(self, item: PipelineItem, ctx: "RunContext", kind: str, message: str,
              retryable: bool) -> None:
        err = StageError(stage=self.name, kind=kind, message=message, retryable=retryable)
        item.errors.append(err)
        item.status = "failed"
        ctx.metrics.event(EV_ERROR, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(item.record.id,),
                          payload={"stage": self.name, "kind": kind,
                                   "message": message, "retryable": retryable})

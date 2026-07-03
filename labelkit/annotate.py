"""M5 annotate stage (spec 3.5, CONTRACTS.md §7.4).

Deterministic prompt assembly (task instruction + few-shot + record content; UI modality
adds screenshot + serialized UI tree), delegation of the structure guarantee to M8
(SchemaEngine.complete_validated), optional self-consistency sampling with field-level
majority vote (spec 3.5.2), and the public repair hooks used by M7 verify.
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
from labelkit.types import Annotation, PipelineItem, Record, StageError, Usage

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


@dataclass(frozen=True)
class RepairContext:
    previous_output: Mapping                       # last annotation object
    critiques_text: str                            # rendered lines "aspect: opinion"
                                                   # (multi-judge: "judge_name/aspect: opinion")


def _dumps(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False)


def build_annotate_prompt(record: Record, cfg: "ResolvedConfig", schema_text: str,
                          repair: RepairContext | None = None,
                          temperature: float | None = None) -> PromptBundle:
    """Deterministic template assembly per CONTRACTS.md §10.1 (+ §10.5 repair suffix).

    schema_text = SchemaEngine.user_schema_text. Section order is fixed: system (task
    instruction + schema constraint), one user message per few-shot example in configured
    order, then the current-record user message (text part, or UI screenshot + tree parts).
    """
    messages: list[Message] = []

    system_text = (f"{cfg.annotate.instruction}\n"
                   f"{_SCHEMA_SENTENCE}\n"
                   f"{schema_text}")
    messages.append(Message(role="system", parts=(Part(kind="text", text=system_text),)))

    for example in cfg.annotate.examples:
        example_text = (f"{_LABEL_EXAMPLE_IN} {example.input}\n"
                        f"{_LABEL_EXAMPLE_OUT} {_dumps(example.output)}")
        messages.append(Message(role="user", parts=(Part(kind="text", text=example_text),)))

    if record.modality == "text":
        parts: tuple[Part, ...] = (
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
                          repair: RepairContext | None = None) -> Annotation:
    """One record's full annotation path incl. self-consistency (skipped when repair is
    not None: repair re-annotation is always a single call at profile-default temperature).
    Raises SchemaViolation / ProviderRetryableError / ProviderFatalError."""
    cfg = ctx.cfg
    profile = cfg.annotate.llm
    schema_text = ctx.schema_engine.user_schema_text
    n = cfg.annotate.self_consistency

    if repair is not None or n == 0:
        prompt = build_annotate_prompt(record, cfg, schema_text, repair=repair,
                                       temperature=None)
        obj, usage, attempts, model = await ctx.schema_engine.complete_validated(
            profile, prompt, record_ids=(record.id,), batch_no=ctx.batch_no)
        return Annotation(output=obj, model=model, attempts=attempts, usage=usage)

    # Self-consistency: n independent samples at sc_temperature, each through the full
    # M8 guarantee; a SchemaViolation sample abstains (denominator stays n).
    async def one_sample() -> tuple[dict, Usage, int, str]:
        prompt = build_annotate_prompt(record, cfg, schema_text, repair=None,
                                       temperature=cfg.annotate.sc_temperature)
        return await ctx.schema_engine.complete_validated(
            profile, prompt, record_ids=(record.id,), batch_no=ctx.batch_no)

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
        try:
            item.annotation = await annotate_record(record, ctx)
        except SchemaViolation as e:
            # Transport the raw last model output to M11 for the rejects "full"
            # tier (§9.2) via the duck-typed channel the emitter reads.
            item.raw_last_output = e.raw_last_output  # type: ignore[attr-defined]
            self._fail(item, ctx, ErrorKind.SCHEMA_VIOLATION.value, str(e), retryable=False)
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

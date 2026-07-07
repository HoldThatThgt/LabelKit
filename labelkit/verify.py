"""M7 verify stage — LLM-as-a-Judge review of (record, annotation) pairs (spec 3.7).

Per CONTRACTS.md §7.6: each active item with an annotation is reviewed with the §10.5
verify prompt against ``VERDICT_SCHEMA`` (§10.7). Optional judges panel (odd count,
majority verdict, critiques merged with a ``judge`` field). Policy ``drop`` drops on
fail; policy ``repair`` feeds the failing judges' critiques back into M5's annotator
(``annotate_record`` + ``RepairContext``) for up to ``verify.max_repair_rounds``
re-annotations, then drops (``dropped_verify``). One ``verify.verdict`` trace event per
judge per round.

Import note: the service/sibling modules this stage composes (``labelkit.llm_client``,
``labelkit.schema_engine``, ``labelkit.annotate``) are imported lazily inside the
functions that need them, so importing ``labelkit.verify`` (and unit-testing its pure
logic) never requires those files to exist yet. The imported names and their use match
CONTRACTS.md exactly.
"""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Mapping, Sequence

from labelkit.errors import (
    CircuitBreakerTripped,
    ErrorKind,
    ProviderFatalError,
    ProviderRetryableError,
    SchemaViolation,
)
from labelkit.types import Annotation, PipelineItem, Record, StageError, VerificationResult

if TYPE_CHECKING:
    from labelkit.config.model import ResolvedConfig
    from labelkit.llm_client import PromptBundle
    from labelkit.stage import RunContext

EV_VERIFY_VERDICT = "verify.verdict"
EV_ERROR = "error"

# §10.5 verify prompt, fixed Chinese text (spec 3.7.2, verbatim).
_SYSTEM_HEAD = "你是标注质量审核员。给定任务指令、原始数据与标注结果，独立判断标注是否合格。"
_SYSTEM_DIMS = "评审维度: ① 是否遵循任务指令 ② 与原始数据的事实一致性 ③ 字段语义是否正确填写"
_SYSTEM_TAIL = "先逐维度给出简短意见，再给结论。"


# ── pure prompt-text assembly (unit-testable, no service imports) ──────────

def verify_system_text(extra_criteria: str) -> str:
    """System message of the §10.5 verify prompt; the extra-criteria line is omitted
    entirely when ``verify.extra_criteria`` is empty."""
    lines = [_SYSTEM_HEAD, _SYSTEM_DIMS]
    if extra_criteria:
        lines.append(extra_criteria)
    lines.append(_SYSTEM_TAIL)
    return "\n".join(lines)


def verify_user_text(instruction: str, record_text: str, output: Mapping) -> str:
    """User message of the §10.5 verify prompt, text modality."""
    return (
        f"[任务指令] {instruction}\n"
        f"[原始数据] {record_text}\n"
        f"[标注结果] {json.dumps(output, ensure_ascii=False)}"
    )


def majority_verdict(verdicts: Sequence[str]) -> Literal["pass", "fail"]:
    """Majority vote over an odd number of pass/fail verdicts (single judge = len 1)."""
    fails = sum(1 for v in verdicts if v == "fail")
    return "fail" if fails * 2 > len(verdicts) else "pass"


def render_critiques_text(critiques: Sequence[Mapping]) -> str:
    """Render critique entries for the M5 repair suffix (§10.5): one per line
    ``aspect: opinion``; entries carrying a ``judge`` key (multi-judge) render as
    ``judge_name/aspect: opinion``."""
    lines = []
    for c in critiques:
        prefix = f"{c['judge']}/" if "judge" in c else ""
        lines.append(f"{prefix}{c['aspect']}: {c['opinion']}")
    return "\n".join(lines)


def build_verify_prompt(record: Record, output: Mapping, cfg: "ResolvedConfig") -> "PromptBundle":
    """Assemble the §10.5 judge prompt for one (record, annotation-output) pair.
    UI modality carries screenshot + serialized tree parts as in §10.1/§10.2."""
    from labelkit.llm_client import Message, Part, PromptBundle

    system = Message(
        role="system",
        parts=(Part(kind="text", text=verify_system_text(cfg.verify.extra_criteria)),),
    )
    if record.modality == "text":
        user = Message(
            role="user",
            parts=(
                Part(
                    kind="text",
                    text=verify_user_text(cfg.annotate.instruction, record.text or "", output),
                ),
            ),
        )
    else:
        head = f"[任务指令] {cfg.annotate.instruction}\n[原始数据]\n[屏幕截图]"
        tree = record.ui_tree.serialize(max_chars=cfg.input.ui_tree_max_chars)
        tail = f"[UI 控件树]\n{tree}\n[标注结果] {json.dumps(output, ensure_ascii=False)}"
        user = Message(
            role="user",
            parts=(
                Part(kind="text", text=head),
                Part(kind="image", image=record.image),
                Part(kind="text", text=tail),
            ),
        )
    return PromptBundle(messages=(system, user))


# ── policy state machine (pure control flow; judge/repair injected) ────────

JudgeRound = Callable[[Annotation, int], Awaitable[tuple[str, list[dict], list[dict]]]]
Reannotate = Callable[[Annotation, list[dict]], Awaitable[Annotation]]


async def run_verify_loop(
    annotation: Annotation,
    judge_round: JudgeRound,
    reannotate: Reannotate,
    policy: Literal["drop", "repair"],
    max_repair_rounds: int,
) -> tuple[Literal["pass", "fail"], int, list[dict], Annotation]:
    """Drive the review/repair loop (spec 3.7.3).

    ``judge_round(annotation, round_no)`` returns ``(verdict, merged_critiques,
    fail_critiques)`` for one review round; ``reannotate(annotation, fail_critiques)``
    returns the repaired annotation (M5 hook). Returns ``(verdict, rounds,
    accumulated_critiques, final_annotation)`` where ``rounds`` counts judged rounds
    including the first (pass on first review = 1)."""
    critiques_all: list[dict] = []
    rounds = 0
    while True:
        rounds += 1
        verdict, merged, fail_critiques = await judge_round(annotation, rounds)
        critiques_all.extend(merged)
        if verdict == "pass":
            return "pass", rounds, critiques_all, annotation
        repairs_done = rounds - 1
        if policy != "repair" or repairs_done >= max_repair_rounds:
            return "fail", rounds, critiques_all, annotation
        annotation = await reannotate(annotation, fail_critiques)


def _classify_error(exc: Exception, modality: str) -> tuple[str, bool]:
    """Map a per-record exception to (StageError.kind, retryable)."""
    if isinstance(exc, SchemaViolation):
        return ErrorKind.SCHEMA_VIOLATION.value, False
    if isinstance(exc, ProviderRetryableError):
        return ErrorKind.PROVIDER_RETRYABLE_EXHAUSTED.value, True
    if isinstance(exc, ProviderFatalError):
        return ErrorKind.PROVIDER_FATAL.value, False
    if modality == "ui" and isinstance(exc, OSError):
        # Image bytes are loaded lazily at call time; Pillow decode/read failures
        # surface as OSError (spec §7.6: M7 → failed, kind=image_decode_error).
        return ErrorKind.IMAGE_DECODE_ERROR.value, False
    return ErrorKind.INTERNAL_ERROR.value, False


class VerifyStage:
    name = "verify"

    def __init__(self, cfg: "ResolvedConfig"):
        self.cfg = cfg

    async def run(self, batch: list[PipelineItem], ctx: "RunContext") -> list[PipelineItem]:
        eligible = [it for it in batch if it.status == "active" and it.annotation is not None]
        if eligible:
            await asyncio.gather(*(self._verify_item(item, ctx) for item in eligible))
        return batch

    # ── per-item driver ────────────────────────────────────────────────────

    async def _verify_item(self, item: PipelineItem, ctx: "RunContext") -> None:
        vcfg = self.cfg.verify
        try:
            verdict, rounds, critiques, annotation = await run_verify_loop(
                item.annotation,
                judge_round=lambda ann, rnd: self._judge_round(item.record, ann, rnd, ctx),
                reannotate=lambda ann, fc: self._reannotate(item.record, ann, fc, ctx),
                policy=vcfg.policy,
                max_repair_rounds=vcfg.max_repair_rounds,
            )
        except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as exc:  # single-record failures never escape (stage contract)
            kind, retryable = _classify_error(exc, item.record.modality)
            if isinstance(exc, SchemaViolation):
                # Duck-typed channel read by M11 for the rejects "full" tier (§9.2).
                item.raw_last_output = exc.raw_last_output  # type: ignore[attr-defined]
            err = StageError(stage=self.name, kind=kind, message=str(exc), retryable=retryable)
            item.errors.append(err)
            item.status = "failed"
            ctx.metrics.event(
                EV_ERROR,
                stage=self.name,
                batch_no=ctx.batch_no,
                record_ids=(item.record.id,),
                payload={
                    "stage": self.name,
                    "kind": kind,
                    "message": str(exc),
                    "retryable": retryable,
                },
            )
            return
        item.annotation = annotation  # repaired annotation replaces the original (§7.6)
        item.verification = VerificationResult(
            verdict=verdict, rounds=rounds, critiques=tuple(critiques)
        )
        if verdict == "fail":
            item.status = "dropped_verify"

    # ── one review round (all judges, majority) ────────────────────────────

    async def _judge_round(
        self, record: Record, annotation: Annotation, round_no: int, ctx: "RunContext"
    ) -> tuple[str, list[dict], list[dict]]:
        from labelkit.schema_engine import VERDICT_SCHEMA

        vcfg = self.cfg.verify
        judges = list(vcfg.judges) or [vcfg.llm]
        multi = bool(vcfg.judges)
        prompt = build_verify_prompt(record, annotation.output, ctx.cfg)
        results = await asyncio.gather(
            *(
                ctx.schema_engine.complete_validated(
                    judge,
                    prompt,
                    schema=VERDICT_SCHEMA,
                    record_ids=(record.id,),
                    batch_no=ctx.batch_no,
                )
                for judge in judges
            ),
            return_exceptions=True,
        )
        merged: list[dict] = []
        fail_critiques: list[dict] = []
        verdicts: list[str] = []
        for judge, result in zip(judges, results):
            if isinstance(result, BaseException):
                # 单个 judge 崩溃（SchemaViolation / ProviderError 等）→ 视为 fail，
                # 记入 fail_critiques，不丢失其他 judge 的裁决（对标 quality.py:503-518）
                if isinstance(result, (KeyboardInterrupt, asyncio.CancelledError,
                                       CircuitBreakerTripped)):
                    raise result
                if not multi:
                    raise result  # 单 judge：让 _verify_item 的 _classify_error 分类错误类型
                verdicts.append("fail")
                entry = {"aspect": "judge_error", "opinion": str(result)}
                if multi:
                    entry["judge"] = judge
                merged.append(entry)
                fail_critiques.append(entry)
                continue
            obj, _usage, _attempts, _model = result
            verdict = obj["verdict"]
            verdicts.append(verdict)
            entries = []
            for c in obj["critiques"]:
                entry = {"aspect": c["aspect"], "opinion": c["opinion"]}
                if multi:
                    entry["judge"] = judge
                entries.append(entry)
            merged.extend(entries)
            if verdict == "fail":
                fail_critiques.extend(entries)
            self._emit_verdict_event(record, verdict, round_no, obj["critiques"],
                                     judge if multi else None, ctx)
        return majority_verdict(verdicts), merged, fail_critiques

    def _emit_verdict_event(
        self,
        record: Record,
        verdict: str,
        round_no: int,
        critiques: Sequence[Mapping],
        judge: str | None,
        ctx: "RunContext",
    ) -> None:
        content = ctx.cfg.trace.content
        payload: dict = {"verdict": verdict, "round": round_no}
        if content != "none":  # §8.3: tier "none" carries no LLM-produced free text
            payload["critiques"] = [
                {"aspect": c["aspect"], "opinion": c["opinion"]} for c in critiques
            ]
        if judge is not None:
            payload["judge"] = judge
        if ctx.cfg.trace.enabled and content in ("excerpt", "full"):
            # §7.4: the four trace.content tiers are cumulative ("逐档递增") — "full"
            # includes everything from "excerpt", so the excerpt is attached at both
            # tiers (CONTRACTS.md §8.3). Gated on trace.enabled so ui_tree.serialize()
            # is never computed when tracing is off.
            src = record.text if record.modality == "text" else record.ui_tree.serialize()
            payload["excerpt"] = {record.id: (src or "")[:200]}
        ctx.metrics.event(
            EV_VERIFY_VERDICT,
            stage=self.name,
            batch_no=ctx.batch_no,
            record_ids=(record.id,),
            payload=payload,
        )

    # ── repair hook into M5 (sanctioned cross-operator import, §7.4/§7.6) ──

    async def _reannotate(
        self, record: Record, annotation: Annotation, fail_critiques: list[dict], ctx: "RunContext"
    ) -> Annotation:
        from labelkit.annotate import RepairContext, annotate_record

        repair = RepairContext(
            previous_output=annotation.output,
            critiques_text=render_critiques_text(fail_critiques),
        )
        return await annotate_record(record, ctx, repair)

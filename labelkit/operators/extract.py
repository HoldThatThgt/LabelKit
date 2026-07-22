"""M15 extract stage (spec 3.15, CONTRACTS.md §7.15).

Transition/action extraction for sequence envelopes (episodes): one structured
action per adjacent member pair ⟨s_i, s_{i+1}⟩ via LLM — deterministic prompt
assembly (CONTRACTS §10.10: two images + optional tree-diff evidence + the
always-present frame-digest tail), the M8 internal-schema guarantee
(schema_engine.action_schema — no resolved_at counting, no L2.5), and the
on_error fallback/fail policy (S16: fallback evidence lives in Transition.detail,
never in item.errors — R4 family). Transition count == member count − 1, always
(a failed step becomes a fallback placeholder, never a hole). v1.9 (T10/T20):
pairs at M16 ``seam_indexes`` never reach the LLM — they take the mechanical
thread-seam placeholder (detail.kind == "thread_seam") and stay OUT of the
extract counters; adjacent-rescue splices are real transitions and extract
normally. Chain position:
classify → extract → quality (labels are in place so [class.<label>.extract]
per-class instructions apply); multi fan-out siblings each extract independently
under their own label (S9 — never de-duplicated by record id). UI-modality
sequences only (M1 enforces; re-checked defensively here).

Concurrency: ALL transitions across ALL episodes of the batch join ONE
asyncio.gather (M4 pairwise phase-2 skeleton); results are written back by
(episode batch position, pair ordinal) — schedule-independent, zero rng.

``extract_transition`` is a PUBLIC DIRECT-CALL SURFACE: M7's post-surgery seam
re-extraction calls it directly (1–2 calls per surgery; the stage itself is
never re-run — ``transitions is not None`` skips, so re-entry costs zero calls).
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Mapping

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
    PipelineItem,
    Record,
    StageError,
    Transition,
    frame_digest,
    tree_diff,
)
from labelkit.common.runtime import budget

from labelkit.common.runtime.llm_client import Message, Part, PromptBundle
from labelkit.common.runtime.schema_engine import action_schema

if TYPE_CHECKING:
    from labelkit.common.config.model import ResolvedConfig
    from labelkit.common.contracts.stage import RunContext


_STAGE_NAME = "extract"

# Event names (exact strings per CONTRACTS.md §7.15 / §8.1).
_EV_STEP = "extract.step"
_EV_ERROR = "error"

# Counter keys owned by M15 (CONTRACTS.md §9.3; report.stream.extract).
_COUNTER_TRANSITIONS = "extract.transitions"        # total steps incl. fallback
_COUNTER_FALLBACK_STEPS = "extract.fallback_steps"
_COUNTER_FAILURES = "extract.failures"              # failed episodes
_COUNTER_BY_TYPE_PREFIX = "extract.by_type."        # per action_type, incl. fallback "other"

# Chinese prompt fragments — verbatim from CONTRACTS.md §10.10 (spec 3.15.4),
# including the documented line breaks. The vocabulary bullets and the OpenCUA
# anchoring sentence are frozen template text and never vary with configuration.
_SYSTEM_HEAD = (
    "你是屏幕操作流的动作摘取员。给定同一操作流中相邻的前后两帧屏幕状态，推断用户在两帧之间\n"
    "执行的动作。action_type 只能取以下值：\n"
    "- click / long_press / drag: 点击 / 长按 / 拖拽某控件\n"
    "- input_text: 在输入框键入文本\n"
    "- scroll: 滚动屏幕或列表\n"
    "- open_app: 打开一个应用；app_switch: 切换到另一已打开的应用\n"
    "- navigate_back / navigate_home: 系统返回 / 回到桌面\n"
    "- wait: 无用户交互，仅等待界面加载或变化\n"
    "- other: 无法归入以上任何一类（把语义写进 description）\n"
    "锚定约定：前一帧是动作发生前最后一个稳定状态，后一帧是动作完成后的首个稳定状态；推断\n"
    "二者之间发生的单个语义动作；若变化由多个低层事件构成（连续滚动、连续键入），归并为一个\n"
    "语义动作。"
)
_STRUCTURE_SENTENCE = "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容："
_STRUCTURE_SHAPE = ('{"action_type": <词表值>, "target": <目标控件文本引用或 null>,\n'
                    ' "value": <动作参数或 null>, "description": <一句话动作描述>}')
_LABEL_PREV = "[前一帧截图]"
_LABEL_NEXT = "[后一帧截图]"
_LABEL_DIFF = "[树变更摘要]"
_LABEL_DIGESTS = "[前后帧树摘要]"

# Per-frame digest cap for the [前后帧树摘要] tail. The [extract] table carries no
# digest key; 400 = segment.digest_max_chars default — the same resolution M4's
# sequence branch made for its member digests (quality._MEMBER_DIGEST_MAX_CHARS).
_DIGEST_MAX_CHARS = 400

# Code-side fallback action (S16): distinguishable from an LLM-confirmed "other"
# by the presence of Transition.detail["kind"] == "extraction_invalid".
_FALLBACK_ACTION: Mapping = {"action_type": "other", "target": None,
                             "value": None, "description": ""}


def _seam_placeholder(index: int, interrupted_by: tuple[str, ...]) -> Transition:
    """v1.9 (T10, four keys pinned): the zero-LLM mechanical placeholder written
    at a thread-seam step. action_type="app_switch" promises NO semantics (M-1
    备注: same-app interleavings land here too) — downstream discriminates by
    detail.kind == "thread_seam". interrupted_by = the distinct interrupting
    threads' task_names in gap order (M-1 guarantees ≥ 1); model=""/attempts=0:
    no call was ever made."""
    names = "、".join(interrupted_by)
    action = {"action_type": "app_switch", "target": None, "value": None,
              "description": f"线索接缝：被{names}打断后恢复"}
    return Transition(index=index, action=action, model="", attempts=0,
                      detail={"kind": "thread_seam",
                              "interrupted_by": list(interrupted_by)})


def _diff_text(diff: Mapping) -> str:
    """Deterministic textualization of a tree_diff mapping for [树变更摘要]
    (spec 3.15.4: added/removed/text-changed node counts, change ratio, App/title
    changed). Same fixed form as M14's §10.9 [帧 {i} 变更] rendering — operator
    modules never depend on each other (spec §2.2), so this is M15's own copy of
    the shared format (the quality/annotate step-line precedent)."""
    text = (f"新增 {diff['added']} 节点，移除 {diff['removed']} 节点，"
            f"文本变化 {diff['text_changed']} 处，"
            f"变更比例 {diff['change_ratio']:.0%}")
    if diff["app_changed"]:
        text += "，应用切换"
    if diff["title_changed"]:
        text += "，标题变化"
    return text


def build_extract_prompt(prev: Record, curr: Record, cfg: "ResolvedConfig",
                         label: str | None) -> PromptBundle:
    """Deterministic assembly of the §10.10 template.

    system: extraction instruction + the 11-value vocabulary bullets + the
    OpenCUA anchoring sentence + the optional instruction line (label non-None →
    class_views[label].extract's effective value, the only whitelisted per-class
    key; omitted entirely when empty) + the structure sentence and shape.
    user: ONE message, five parts — text [前一帧截图], image s_i, text
    [后一帧截图], image s_{i+1}, and the always-present closing text part:
    the [树变更摘要] line (include_diff=true only; structural tree diff, S14)
    plus the ALWAYS-present [前后帧树摘要] line (S6: the final part is text).
    tree_diff quantization reuses dedup.bounds_quantize_px — the config's single
    coordinate-quantization notion, absorbing the same capture-side bounds
    jitter it exists for (spec §4.3).
    """
    ecfg = cfg.class_views[label].extract if label is not None else cfg.extract
    lines = [_SYSTEM_HEAD]
    if ecfg.instruction:
        lines.append(ecfg.instruction)
    lines.append(_STRUCTURE_SENTENCE)
    lines.append(_STRUCTURE_SHAPE)
    system = Message(role="system", parts=(Part(kind="text", text="\n".join(lines)),))

    tail_lines: list[str] = []
    if cfg.extract.include_diff:
        diff = tree_diff(prev.ui_tree, curr.ui_tree, cfg.dedup.bounds_quantize_px)
        tail_lines.append(f"{_LABEL_DIFF} {_diff_text(diff)}")
    tail_lines.append(f"{_LABEL_DIGESTS} {frame_digest(prev, _DIGEST_MAX_CHARS)}"
                      f" → {frame_digest(curr, _DIGEST_MAX_CHARS)}")
    user = Message(role="user", parts=(
        Part(kind="text", text=_LABEL_PREV),
        Part(kind="image", image=prev.image),
        Part(kind="text", text=_LABEL_NEXT),
        Part(kind="image", image=curr.image),
        Part(kind="text", text="\n".join(tail_lines)),
    ))
    return PromptBundle(messages=(system, user))


async def extract_transition(prev: Record, curr: Record, index: int,
                             ctx: "RunContext", label: str | None = None) -> Transition:
    """One transition, one call — through complete_validated(schema=action_schema()).

    Repair exhaustion follows extract.on_error (S16): "fallback" (default)
    returns the code-side fallback Transition — action_type="other",
    detail={kind: "extraction_invalid", message} — plus the extract.fallback_steps
    counter and the error trace event; NEVER item.errors (rejects attribution
    reads errors[0], R4). "fail" re-raises the SchemaViolation (the stage layer
    fails the episode). Provider/internal errors always propagate to the caller.
    Post-validation normalization: a scroll direction value is lowercased
    code-side (spec 3.15.4 field-semantics table).

    PUBLIC DIRECT-CALL SURFACE: M7's post-surgery seam re-extraction calls this
    function directly (CONTRACTS §7.15) — the sanctioned import exception.
    """
    cfg = ctx.cfg
    prompt = build_extract_prompt(prev, curr, cfg, label)
    try:
        obj, _usage, attempts, model = await ctx.schema_engine.complete_validated(
            cfg.extract.llm, prompt, action_schema(),
            record_ids=(prev.id, curr.id), batch_no=ctx.batch_no)
    except SchemaViolation as e:
        if cfg.extract.on_error == "fail":
            raise
        kind = ErrorKind.EXTRACTION_INVALID.value
        message = str(e)
        ctx.metrics.count(_COUNTER_FALLBACK_STEPS)
        ctx.metrics.event(_EV_ERROR, stage=_STAGE_NAME, batch_no=ctx.batch_no,
                          record_ids=(prev.id, curr.id),
                          payload={"stage": _STAGE_NAME, "kind": kind,
                                   "message": message, "retryable": False})
        # model=""/attempts=1+L3 budget: no validated output exists — the action
        # is a code-side construct; attempts reflects the exhausted call budget.
        return Transition(index=index, action=dict(_FALLBACK_ACTION), model="",
                          attempts=1 + cfg.output.max_repair_attempts,
                          detail={"kind": kind, "message": message})
    except (ContextOverflowError, OutputTruncatedError) as e:
        # v1.11 (spec 3.15.4 上下文预算 row): the constant 2-frame/2-image call has
        # nothing to shrink — no packing, no degrade face; the M9 throat/finish
        # disposition backstops (V16). Overflow/truncation rides the EXISTING
        # mechanical-fallback semantics UNCHANGED: the fallback Transition keeps
        # the extraction_invalid trace shape (detail.kind drives the downstream
        # （摘取兜底） suffix and report attribution — S16); "fail" re-raises and
        # the stage classifier records the precise §7.6 kind (V27①).
        if cfg.extract.on_error == "fail":
            raise
        # A7 terminal settlement: a reactive-400 overflow disposed here (the
        # fallback IS this call's terminal — no degrade face) feeds the fatal
        # streak exactly once; precheck / the 200-shaped finish oracle never do.
        if (isinstance(e, ContextOverflowError) and e.phase == "reactive"
                and getattr(e, "origin", "http_400") == "http_400"
                and not getattr(e, "_breaker_fed", False)):
            e._breaker_fed = True  # type: ignore[attr-defined]
            ctx.metrics.record_provider_result(fatal=True)
        kind = ErrorKind.EXTRACTION_INVALID.value
        message = str(e)
        ctx.metrics.count(_COUNTER_FALLBACK_STEPS)
        ctx.metrics.event(_EV_ERROR, stage=_STAGE_NAME, batch_no=ctx.batch_no,
                          record_ids=(prev.id, curr.id),
                          payload={"stage": _STAGE_NAME, "kind": kind,
                                   "message": message, "retryable": False})
        # attempts=1: overflow/truncation finalizes the call before any L3
        # repair round runs (V11/V25① — never "repaired").
        return Transition(index=index, action=dict(_FALLBACK_ACTION), model="",
                          attempts=1,
                          detail={"kind": kind, "message": message})
    if obj.get("action_type") == "scroll" and isinstance(obj.get("value"), str):
        obj = {**obj, "value": obj["value"].lower()}
    return Transition(index=index, action=obj, model=model, attempts=attempts,
                      detail={})


class ExtractStage:
    name = "extract"

    def __init__(self, cfg: "ResolvedConfig"):
        self.cfg = cfg

    async def run(self, batch: list[PipelineItem], ctx: "RunContext") -> list[PipelineItem]:
        # Selection & idempotency (spec 3.15.2): active sequence envelopes not
        # yet extracted; transitions is not None skips (any re-entry costs zero
        # calls — M7 repair uses the extract_transition direct call instead).
        # Modality is M1-guaranteed "ui"; re-checked defensively. Multi fan-out
        # siblings are NOT de-duplicated by record id — each envelope extracts
        # under its own label (S9).
        todo = [item for item in batch
                if item.status == "active" and item.record.kind == "sequence"
                and item.transitions is None and item.record.modality == "ui"]
        if not todo:
            return batch

        # ONE flat gather over every JUDGED (episode, adjacent pair) of the
        # batch; coroutine order = (episode batch position, pair ordinal), and
        # gather preserves it, so the write-back below is schedule-independent.
        # v1.9 (T20): pairs at seam indexes are SKIPPED — they never join the
        # gather (zero LLM) and take the mechanical T10 placeholder in the
        # finalize below; the pre-v1.9 one-coroutine-per-pair slicing is
        # replaced by per-episode judged-pair accounting for exactly this.
        spans: list[tuple[PipelineItem, int, frozenset[int]]] = []
        coros = []
        for item in todo:
            members = item.record.members
            label = item.classification.label if item.classification else None
            pairs = max(0, len(members) - 1)
            seams = frozenset(i for i in getattr(item, "seam_indexes", ()) or ()
                              if 0 <= i < pairs)
            spans.append((item, pairs, seams))
            for i in range(pairs):
                if i in seams:
                    continue
                coros.append(extract_transition(members[i], members[i + 1], i,
                                                ctx, label=label))
        results = await asyncio.gather(*coros, return_exceptions=True)

        for res in results:
            if isinstance(res, (CircuitBreakerTripped, KeyboardInterrupt,
                                asyncio.CancelledError)):
                raise res

        # Synchronous finalize in batch position order: an episode with any
        # escaped step exception fails whole (its other step results are
        # discarded — the invariant tuple is all-or-nothing); otherwise
        # len(transitions) == len(members) − 1 with fallback placeholders and,
        # v1.9, the seam placeholders spliced in at their pinned indexes.
        pos = 0
        for item, pairs, seams in spans:
            judged = pairs - len(seams)
            row = results[pos:pos + judged]
            pos += judged
            exc = next((r for r in row if isinstance(r, BaseException)), None)
            if exc is not None:
                self._fail(item, ctx, exc)
                continue
            interrupted = tuple(getattr(item, "seam_interrupted_by", ()) or ())
            seam_order = sorted(seams)
            transitions: list[Transition] = []
            judged_iter = iter(row)
            for i in range(pairs):
                if i in seams:
                    names = (interrupted[seam_order.index(i)]
                             if seam_order.index(i) < len(interrupted) else ())
                    transitions.append(_seam_placeholder(i, tuple(names)))
                else:
                    transitions.append(next(judged_iter))
            item.transitions = tuple(transitions)
            self._register(item, ctx)
        return batch                            # the SAME list object (contract ②)

    def _register(self, item: PipelineItem, ctx: "RunContext") -> None:
        """Counters + one extract.step event per finalized step, fallback steps
        included (§8.1): extract.transitions / extract.by_type.<action_type>
        count EVERY final step (fallback lands in by_type.other). Payload fields
        go in raw — the S27 redaction (_DATA_KEYS/_FREE_TEXT_KEYS) is M12's.
        v1.9 (T20 计数器口径): thread-seam placeholders are NOT extraction
        products — they skip the counters (their zero-LLM app_switch must not
        pollute by_type) and the extract.step event alike; the seam's single
        metering point is stream.stitch.seams."""
        members = item.record.members
        for t in item.transitions:
            if t.detail.get("kind") == "thread_seam":
                continue
            action_type = t.action["action_type"]
            ctx.metrics.count(_COUNTER_TRANSITIONS)
            ctx.metrics.count(_COUNTER_BY_TYPE_PREFIX + action_type)
            ctx.metrics.event(_EV_STEP, stage=self.name, batch_no=ctx.batch_no,
                              record_ids=(members[t.index].id,
                                          members[t.index + 1].id),
                              payload={"episode_id": item.record.id,
                                       "index": t.index,
                                       "action_type": action_type,
                                       "description": t.action["description"],
                                       "target": t.action["target"],
                                       "value": t.action["value"]})

    def _fail(self, item: PipelineItem, ctx: "RunContext", exc: BaseException) -> None:
        """Episode-level failure: on_error="fail" schema exhaustion, or any
        provider/internal error of a step call (kinds classified as in
        classify._classify_item; extract records are always UI modality, so an
        OSError is an image-decode failure surfacing from M9's lazy load).
        v1.11 (V27①): the budget vocabulary routes FIRST — context_overflow /
        output_truncated precisely (never internal_error); an overflow reject
        counts budget.overflow_records and the reactive-400 terminal feeds the
        breaker exactly once (A7 — duck flag guards double-feeds)."""
        budget_kind = budget.classify_stage_error(exc)
        if budget_kind is not None:
            kind, retryable = budget_kind, False
            message = str(exc)
            if kind == ErrorKind.CONTEXT_OVERFLOW.value:
                ctx.metrics.count("budget.overflow_records")
                if (isinstance(exc, ContextOverflowError)
                        and exc.phase == "reactive"
                        and getattr(exc, "origin", "http_400") == "http_400"
                        and not getattr(exc, "_breaker_fed", False)):
                    exc._breaker_fed = True  # type: ignore[attr-defined]
                    ctx.metrics.record_provider_result(fatal=True)
        elif isinstance(exc, SchemaViolation):
            kind, retryable = ErrorKind.EXTRACTION_INVALID.value, False
            message = str(exc)
        elif isinstance(exc, ProviderRetryableError):
            kind, retryable = ErrorKind.PROVIDER_RETRYABLE_EXHAUSTED.value, True
            message = str(exc)
        elif isinstance(exc, ProviderFatalError):
            kind, retryable = ErrorKind.PROVIDER_FATAL.value, False
            message = str(exc)
        elif isinstance(exc, OSError):
            kind, retryable = ErrorKind.IMAGE_DECODE_ERROR.value, False
            message = f"{type(exc).__name__}: {exc}"
        else:
            kind, retryable = ErrorKind.INTERNAL_ERROR.value, False
            message = f"{type(exc).__name__}: {exc}"
        err = StageError(stage=self.name, kind=kind, message=message,
                         retryable=retryable)
        item.errors.append(err)
        item.status = "failed"
        ctx.metrics.count(_COUNTER_FAILURES)
        ctx.metrics.event(_EV_ERROR, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(item.record.id,),
                          payload={"stage": self.name, "kind": kind,
                                   "message": message, "retryable": retryable})

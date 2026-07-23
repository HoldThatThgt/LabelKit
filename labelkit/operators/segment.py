"""M14 segment stage (spec 3.14, CONTRACTS.md §7.14) — v1.8 stream-mode operator.

Refines the batch's candidate sessions into episodes: regroups active frame
envelopes (record.kind == "single") by PipelineItem.session_id (batch position
order = session order, guaranteed by M10's whole-session packing), runs the
optional LLM sliding-window boundary verdicts (deterministic §10.9 prompt, the
M8 internal-schema guarantee via schema_engine.segment_window_schema, code-side
first-wins stitching), then deterministically assembles segments: noise frames
→ ``dropped_noise`` (duck-typed reason "noise"), boundary split, min_len check
(LLM-refined segments only, S11 — reason "below_min_len"), members → ``absorbed``
and one sequence envelope per segment tail-appended to the SAME batch list
(Stage contract ②b). Chain position: HEAD of the chain, before dedup. Failure
policy segment.on_error: "keep" degrades the whole session to ONE episode with
the S26 evidence triple (duck-typed ``segment_degraded`` → _meta.stream.degraded
+ error event + segment.failures counter, never item.errors); "fail" fails all
session members. ``judge_window`` is a PUBLIC direct-call surface for M7's
member-reclaim re-judgment (the sanctioned import exception).

v1.11 (context budget, SPEC-context-budget V4/V9/V13④/V20/V24/V27①): frame
digests are precomputed ONCE per session BEFORE windowing and shared by the
packing costs and every window prompt; when the segment profile declares
``context_window > 0`` the window cut switches from fixed spans to the greedy
budget packer ``_pack_windows`` (window = pure upper cap; 1-frame overlap and
later-window seam ownership preserved), undeclared budget keeps the v1.10
fixed-window cut byte-identically; a window call raising the reactive
``ContextOverflowError`` is re-cut in half and retried (bounded, ≤ 2 halvings
deep — the 400-sniffed terminal feeds the breaker exactly once, A7); window
failures classify through ``budget.classify_stage_error`` before falling back
to segmentation_invalid; every dispatched window counts ``segment.windows``
(→ report.stream.windows).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import TYPE_CHECKING, Mapping, Sequence

from labelkit.common.errors import (
    CircuitBreakerTripped,
    ContextOverflowError,
    ErrorKind,
)
from labelkit.common.contracts.types import (
    PipelineItem,
    Record,
    RecordRef,
    StageError,
    digest_is_poor,
    frame_digest,
    tree_diff,
)

from labelkit.common.runtime import budget as budget_mod
from labelkit.common.runtime.llm_client import Message, Part, PromptBundle
from labelkit.common.runtime.schema_engine import segment_window_schema

if TYPE_CHECKING:
    from labelkit.common.config.model import ResolvedConfig
    from labelkit.common.contracts.stage import RunContext

_logger = logging.getLogger("labelkit.segment")

# Event names (exact strings per CONTRACTS.md §7.14 / §8.1).
_EV_BOUNDARY = "segment.boundary"
_EV_ERROR = "error"

# Counter keys owned by M14 (CONTRACTS.md §9.3; counts.episodes / absorbed /
# dropped_noise are metered by M10). v1.11: segment.windows = ACTUAL windows
# dispatched incl. V20 split sub-windows (→ report.stream.windows, V13④ —
# reconciles estimate_run's w_min upper bound); budget.degrade_retries counts
# every V20 halving (→ report.budget.degrade_retries, V13⑤).
_COUNTER_FAILURES = "segment.failures"
_COUNTER_BELOW_MIN_LEN = "segment.below_min_len"
_COUNTER_DIGEST_POOR = "segment.digest_poor_frames"
_COUNTER_WINDOWS = "segment.windows"
_COUNTER_DEGRADE_RETRIES = "budget.degrade_retries"

# V20 degrade bound: at most 2 degrade levels per original window (one further
# halving of a half) — multiplicative decrease, bounded (AIMD family, spec
# 3.14.4 溢出降级重试).
_MAX_DEGRADE_LEVELS = 2

# Deductive mapping (spec 3.14.4, code-side lookup — the LLM never answers the
# boundary question): continues/advances → non-boundary; returns_to_entry/
# context_switch → boundary (that frame heads a new segment); interruption →
# noise. The session's first frame is always a segment head.
_BOUNDARY_RELATIONS = frozenset({"returns_to_entry", "context_switch"})
_NOISE_RELATION = "interruption"

# Chinese prompt fragments — verbatim from CONTRACTS.md §10.9 (spec 3.14.4).
# "{N}" is substituted with the window frame count at assembly time.
_SYSTEM_HEAD = (
    "你是屏幕操作流的分段审核员。下面给出同一会话中按时间顺序排列的 {N} 帧状态摘要\n"
    "（含相邻帧的确定性变更提示）。按三步作业：\n"
    "一、双向上下文概括：通读全窗，把握每帧之前若干帧正在进行的活动与之后若干帧的走向，再判断该帧。\n"
    "二、逐帧关系分类：对每一帧，判断它相对进行中活动的功能角色，只能从以下封闭词表中取恰一值：\n"
    "- continues: 同一流程的推进。\n"
    "- advances: 屏幕或 App 变了，但可见的任务实体延续（验证码、订单号、餐厅名等跨屏出现）——\n"
    "  跨 App 的同一任务属此值，不是边界。\n"
    "- returns_to_entry: 回到入口/搜索/桌面后开启新流程（同 App 背靠背任务的断点）。\n"
    "- context_switch: 交互对象与环境不连续且无实体延续——相关但无实体延续的新流程也取此值。\n"
    "- interruption: 与前后活动均无关的短暂插入（通知、弹窗、误触）。\n"
    "三、只输出逐帧关系，不判断边界（边界由既定规则从关系推导）。\n"
    "锚定约定：分段粒度取「完整任务」层级（整段录屏之下一层）；只看前台 App/前台窗口，\n"
    "忽略状态栏、后台通知等背景变化。"
)
_STRUCTURE_SENTENCE = "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容："
_STRUCTURE_PLAIN = ('{"frames": [{"index": <窗内帧序号>, "relation": <词表值>}, ...]}'
                    "（恰 {N} 项）")
_STRUCTURE_REASON = ('{"frames": [{"index": <窗内帧序号>, "relation": <词表值>, '
                     '"reason": <一句话理由>}, ...]}（恰 {N} 项）')
_FRAME_LABEL_TMPL = "[帧 {i}] {digest}"
_DIFF_LABEL_TMPL = "[帧 {i} 变更] {diff}"

# Once-per-run stderr WARN for the digest-poverty guard (S12) — data-independent.
# v1.11 (V4): guidance points at profile capability — the former use_vision key
# is removed, choosing the profile IS choosing the capability (V1).
_DIGEST_POOR_WARNING = ("帧摘要贫瘠（可见文本节点为零）：纯文本边界裁决依据不足，"
                        "建议为 segment.llm 配置 supports_vision=true 的 profile "
                        "附帧截图补偿")


def _reason_requested(cfg: "ResolvedConfig") -> bool:
    """with_reason iff trace.enabled and "segment" ∈ trace.channels (§8.1 †,
    the classify R29 construction — zero extra tokens otherwise)."""
    return cfg.trace.enabled and "segment" in cfg.trace.channels


def render_tree_diff(diff: Mapping) -> str:
    """Fixed textualization of a tree_diff mapping for the §10.9 [帧 {i} 变更]
    line: counts + change ratio, with the app/title flags appended only when
    set. Pure deterministic string assembly."""
    text = (f"新增 {diff['added']} 节点，移除 {diff['removed']} 节点，"
            f"文本变化 {diff['text_changed']} 处，"
            f"变更比例 {diff['change_ratio']:.0%}")
    if diff["app_changed"]:
        text += "，应用切换"
    if diff["title_changed"]:
        text += "，标题变化"
    return text


def build_segment_prompt(frames: Sequence[Record], diffs: Sequence[Mapping | None],
                         cfg: "ResolvedConfig", with_reason: bool,
                         digests: Sequence[str]) -> PromptBundle:
    """Deterministic assembly of the CONTRACTS §10.9 template — TEMPLATE BYTES
    UNCHANGED by v1.11.

    system: the frozen three-step deductive criteria with the window frame
    count substituted, the optional segment.context line (omitted when empty),
    and the structure line with or without the reason fragment. user: ONE
    message, one text part per frame — "[帧 {i}] {digest}" plus, from the
    second frame on and when the caller supplied a diff, the "[帧 {i} 变更]"
    line; under segment.vision_resolved (v1.11 V1 parse product) each frame's
    digest part is preceded by that frame's image part (§10.1/§10.10
    single-message multi-part shape).

    ``digests`` (v1.11 V9 frozen-signature revision, CONTRACTS §7.14): the
    per-frame digest strings ALIGNED with ``frames``, precomputed once per
    session BEFORE window packing — the packer prices frames off the same
    vector and seam frames are no longer digested twice; the builder never
    computes digests itself.
    """
    seg = cfg.segment
    n = str(len(frames))
    lines = [_SYSTEM_HEAD.replace("{N}", n)]
    if seg.context:
        lines.append(seg.context)
    lines.append(_STRUCTURE_SENTENCE)
    structure = _STRUCTURE_REASON if with_reason else _STRUCTURE_PLAIN
    lines.append(structure.replace("{N}", n))
    messages: list[Message] = [
        Message(role="system", parts=(Part(kind="text", text="\n".join(lines)),))]

    parts: list[Part] = []
    for i, frame in enumerate(frames):
        if seg.vision_resolved and frame.image is not None:
            parts.append(Part(kind="image", image=frame.image))
        text = _FRAME_LABEL_TMPL.format(i=i, digest=digests[i])
        diff = diffs[i] if i < len(diffs) else None
        if i >= 1 and diff is not None:            # 窗首帧无此行
            text += "\n" + _DIFF_LABEL_TMPL.format(i=i, diff=render_tree_diff(diff))
        parts.append(Part(kind="text", text=text))
    messages.append(Message(role="user", parts=tuple(parts)))
    return PromptBundle(messages=tuple(messages))


def _static_prompt_est(cfg: "ResolvedConfig") -> int:
    """est_text of the §10.9 prompt's static (record-independent) parts — the
    packing condition's est_static_system term (SPEC-context-budget §3.3-1).

    Enumerated from build_segment_prompt above: the system message is exactly
    "\\n".join(_SYSTEM_HEAD, [segment.context], _STRUCTURE_SENTENCE, structure
    line) — evaluated here in constant form ({N} unsubstituted; the 1–2 char
    frame-count substitution is margin headroom by design, V7) with the
    with_reason structure variant resolved deterministically from cfg — plus
    MSG_OVERHEAD_TOKENS for each of the two messages (system + user). The
    per-frame "[帧 {i}] " label and the "[帧 {i} 变更] {rendered diff}" line
    are frame scaffolding covered by the per-frame DIFF_MAX_TOKENS worst
    constant (V9: the diff is computed only after windowing; its rendered line
    is structurally bounded ≪ 128 tokens incl. the labels)."""
    seg = cfg.segment
    lines = [_SYSTEM_HEAD]
    if seg.context:
        lines.append(seg.context)
    lines.append(_STRUCTURE_SENTENCE)
    lines.append(_STRUCTURE_REASON if _reason_requested(cfg) else _STRUCTURE_PLAIN)
    return (budget_mod.est_text("\n".join(lines))
            + 2 * budget_mod.MSG_OVERHEAD_TOKENS)


# ── window verdict (one window, one call) ────────────────────────────────────

async def judge_window(frames: Sequence[Record], ctx: "RunContext") -> list[str]:
    """One window, one call — through complete_validated(schema=
    segment_window_schema(len(frames), with_reason)). Post-validation is INSIDE
    this function: index table built FIRST-WINS (a duplicate index keeps the
    first occurrence), absent frames default to "continues" (conservative-
    neutral, the quality "absent criterion → tie" precedent); returns the
    per-frame relation list ALIGNED with ``frames``. Emits one segment.boundary
    event per window. PUBLIC DIRECT-CALL SURFACE: M7's member-reclaim
    re-judgment calls this function directly (CONTRACTS §7.14) — the sanctioned
    import exception registered in the ground rules."""
    return await _judge_window(frames, ctx, session_id=None,
                               span=(0, len(frames)))


async def _judge_window(frames: Sequence[Record], ctx: "RunContext", *,
                        session_id: str | None,
                        span: tuple[int, int],
                        digests: Sequence[str] | None = None) -> list[str]:
    """Shared implementation behind ``judge_window`` and the stage's window
    calls — the keyword-only extras carry the event-payload context (session_id
    + window span) that the frozen public signature cannot, plus the v1.11
    session-precomputed ``digests`` slice (V9). ``digests=None`` = the public
    judge_window path (M7's ≤3-frame re-judgment tables): the digest table is
    self-computed here, keeping the frozen public signature unchanged
    (CONTRACTS §7.14)."""
    cfg = ctx.cfg
    with_reason = _reason_requested(cfg)
    if digests is None:
        digests = [frame_digest(frame, cfg.segment.digest_max_chars)
                   for frame in frames]
    # Adjacent-frame diffs are pre-assembled code-side; the window's first
    # frame carries none. Bounds quantization reuses the tool's single
    # quantization knob (dedup.bounds_quantize_px — the M3 serialize precedent)
    # so pixel jitter never floods added/removed.
    quantize = cfg.dedup.bounds_quantize_px
    diffs: list[Mapping | None] = [None]
    for i in range(1, len(frames)):
        diffs.append(tree_diff(frames[i - 1].ui_tree, frames[i].ui_tree, quantize))
    prompt = build_segment_prompt(frames, diffs, cfg, with_reason, digests)
    schema = segment_window_schema(len(frames), with_reason)
    obj, _usage, _attempts, model = await ctx.schema_engine.complete_validated(
        cfg.segment.llm, prompt, schema, record_ids=(frames[0].id,),
        batch_no=ctx.batch_no)

    # First-wins index table; absent frames default to "continues".
    table: dict[int, Mapping] = {}
    for entry in obj["frames"]:
        table.setdefault(entry["index"], entry)
    verdicts: list[str] = []
    for i in range(len(frames)):
        entry = table.get(i)
        verdicts.append("continues" if entry is None else entry["relation"])

    payload: dict = {
        "session_id": session_id,
        "window": [span[0], span[1]],
        "member_ids": [frame.id for frame in frames],
        "relations": [{"index": i, "relation": relation}
                      for i, relation in enumerate(verdicts)],
        "model": model,
    }
    if with_reason:
        reasons = [table[i]["reason"] for i in range(len(frames))
                   if i in table and "reason" in table[i]]
        if reasons:
            payload["reason"] = reasons
    ctx.metrics.event(_EV_BOUNDARY, stage="segment", batch_no=ctx.batch_no,
                      record_ids=(), payload=payload)
    return verdicts


def _window_spans(n: int, window: int) -> list[tuple[int, int]]:
    """Fixed sliding-window spans over a session of n frames (spec 3.14.4
    pseudocode): window = [start, end), stride = window − 1 (1-frame overlap —
    the seam frame's whole verdict belongs to the later window during
    stitching). v1.11: the BUDGET-UNDECLARED cut (segment profile missing or
    context_window == 0) — kept verbatim so the degradation is byte-identical
    to v1.10 by construction (V9 regression anchor); budget-declared sessions
    cut through _pack_windows below."""
    spans: list[tuple[int, int]] = []
    start = 0
    while start < n:
        end = min(start + window, n)
        spans.append((start, end))
        if end == n:
            break
        start += window - 1
    return spans


def _pack_windows(costs: list[int], budget: int, cap: int) -> list[tuple[int, int]]:
    """Greedy budget packer (v1.11 V9, spec 3.14.4 装填伪代码; M14-owned
    operator logic per CONTRACTS §7.17 — budget.py supplies only the
    estimation primitives). ``costs[i]`` = per-frame cost c_i, ``budget`` =
    input_budget − est_static_system (the caller subtracts the static term, so
    the packing condition Σ c_j ≤ budget here IS the spec's est_static_system
    + Σ c_i ≤ input_budget), ``cap`` = segment.window as pure upper cap.

    Windows = [start, end): the first starts at 0, every subsequent one at the
    previous window's end − 1 — the 1-frame overlap and later-window seam
    ownership are PRESERVED (the rel[] assembly overwrite order relies on it);
    a frame joins the open window while both the budget and the frame-count
    cap hold, overflow closes the window. Every window carries ≥ 2 frames —
    the V10 semantic minimum: the M1 w_min ≥ floor guard promises any two
    worst-case frames fit under PRIOR image pricing (spec 3.1.4), but the
    packer prices off the calibrator, which past CALIBRATION_MIN_SAMPLES may
    legitimately exceed prior × PRIOR_INFLATION (no clamp, by design). A
    window the budget would close below 2 frames is therefore FORCE-PACKED at
    2 regardless of cost: if its true est really exceeds the budget, the M9
    pre-dispatch check owns it record-level (ContextOverflowError(
    phase="precheck") through the per-window failure path) — never a run-kill,
    and the forced advance keeps the loop terminating. Pure function of
    (costs, budget, cap) ⇒ deterministic rerun."""
    spans: list[tuple[int, int]] = []
    n = len(costs)
    start = 0
    while start < n:
        end = start
        total = 0
        while end < n and end - start < cap and total + costs[end] <= budget:
            total += costs[end]
            end += 1
        if end - start < 2:
            end = min(start + 2, n)                # forced semantic minimum
        spans.append((start, end))
        if end == n:
            break
        start = end - 1
    return spans


async def _judge_span_degrading(judge, span: tuple[int, int],
                                ctx: "RunContext", *,
                                level: int = 0) -> list[tuple[tuple[int, int], list[str]]]:
    """V20 window-split degrade-retry (spec 3.14.4 溢出降级重试; SPEC-context-
    budget V20/V24/A7). ``judge`` = async callable(span) -> per-frame verdicts
    for that span (injected — the seam that keeps this pure logic offline-
    testable). Returns the leaf results as [(sub-span, verdicts), ...] in
    ascending span order, ready for the schedule-independent rel[] overwrite
    (the later sub-window still owns its seam frame).

    A window call raising the reactive ContextOverflowError is re-cut in half:
    [s, m+1) and [m, e) with m = midpoint — the 1-frame overlap and the
    seam-owned-by-the-later-window semantics survive the split and no frame is
    lost. Multiplicative decrease, bounded: at most _MAX_DEGRADE_LEVELS (2)
    degrade levels per original window; each halving counts
    budget.degrade_retries. The halves run SEQUENTIALLY — deterministic
    breaker accounting (the first terminal stops the tree; degrade traffic is
    reactive-only and rare, concurrency is not worth losing determinism).

    Terminal (no further split): non-reactive phases (precheck = packing-layer
    bug caught defensively — never degradable), a minimal 2-frame window
    (< 3 frames cannot split into two ≥ 2-frame halves), or the level bound.
    Per SPEC §3.5's breaker matrix, ONLY the 400-sniffed reactive terminal
    (origin="http_400") feeds the breaker — exactly once, here, at the leaf
    (A7: M9 deliberately skipped the feed when raising); the 200-shaped
    origin="finish" oracle rode a successful HTTP interaction whose ok already
    cleared the streak, and precheck had no provider interaction at all. The
    exception then re-raises into the session's on_error disposition (the
    parent recursion levels never re-settle it — only the direct judge() call
    sits inside the try)."""
    start, end = span
    try:
        return [(span, await judge(span))]
    except ContextOverflowError as exc:
        if exc.phase != "reactive" or end - start < 3 or level >= _MAX_DEGRADE_LEVELS:
            if exc.phase == "reactive" and exc.origin == "http_400":
                ctx.metrics.record_provider_result(fatal=True)
            raise
        ctx.metrics.count(_COUNTER_DEGRADE_RETRIES)
        mid = (start + end) // 2
        results = await _judge_span_degrading(judge, (start, mid + 1), ctx,
                                              level=level + 1)
        results.extend(await _judge_span_degrading(judge, (mid, end), ctx,
                                                   level=level + 1))
        return results


# ── stage ────────────────────────────────────────────────────────────────────

class SegmentStage:
    name = "segment"

    def __init__(self, cfg: "ResolvedConfig"):
        self.cfg = cfg
        self._digest_poor_warned = False           # once-per-run WARN (S12)

    async def run(self, batch: list[PipelineItem],
                  ctx: "RunContext") -> list[PipelineItem]:
        seg = self.cfg.segment
        # Regroup by session_id, batch position order = session order (M10
        # whole-session packing). kind == "sequence" envelopes never enter the
        # processing face — naturally idempotent; unstamped frames (session_id
        # None) are ignored defensively.
        sessions: dict[str, list[PipelineItem]] = {}
        for item in batch:
            if item.status != "active" or item.record.kind != "single":
                continue
            if item.session_id is None:
                continue
            sessions.setdefault(item.session_id, []).append(item)
        if not sessions:
            return batch

        refine = seg.strategy in ("llm", "hybrid")

        # v1.11 (V9): the window cut is budget-packed iff the segment profile
        # declares a context window; undeclared → the v1.10 fixed cut,
        # byte-identical. Both the static prompt estimate and the per-image
        # cost are config/batch-frozen values — computed once up front, so the
        # packing stays a pure function of (input, config).
        prof = self.cfg.llm_profiles.get(seg.llm)
        budget_on = refine and prof is not None and prof.context_window > 0
        pack_budget = (budget_mod.input_budget(prof) - _static_prompt_est(self.cfg)
                       if budget_on else 0)

        # Phase 1: every window of every refined session joins ONE gather
        # (M4 phase-2 skeleton); stitching is a synchronous pass afterwards,
        # positioned by window span — schedule-independent, zero rng.
        jobs_meta: list[tuple[str, tuple[int, int]]] = []
        jobs = []
        for sid, items in sessions.items():
            if not refine or len(items) == 1:      # rules / lone-frame: zero LLM
                continue
            for item in items:                     # digest-poverty guard (S12)
                if digest_is_poor(item.record):
                    ctx.metrics.count(_COUNTER_DIGEST_POOR)
                    if not self._digest_poor_warned:
                        self._digest_poor_warned = True
                        _logger.warning(_DIGEST_POOR_WARNING,
                                        extra={"stage": self.name,
                                               "batch": ctx.batch_no})
            # V9 session-level digest precompute — ONCE per frame per session,
            # BEFORE windowing; the packing costs and every window prompt
            # (incl. V20 sub-windows) share this vector, so seam frames are no
            # longer digested twice. The poverty guard above stays an
            # independent computation path (digest_is_poor's own hardcoded
            # cap), untouched by design.
            records = [item.record for item in items]
            digests = [frame_digest(record, seg.digest_max_chars)
                       for record in records]
            if budget_on:
                # Per-image cost: ONE calibrator read per session — the
                # snapshot is batch-frozen (V19), so the read is deterministic
                # wherever it happens inside the batch.
                image_cost = (ctx.llm.calibrator.cost(seg.llm)
                              if seg.vision_resolved else 0)
                costs = [budget_mod.est_text(digest) + budget_mod.DIFF_MAX_TOKENS
                         + image_cost for digest in digests]
                spans = _pack_windows(costs, pack_budget, seg.window)
            else:
                spans = _window_spans(len(items), seg.window)
            for span in spans:
                jobs_meta.append((sid, span))
                jobs.append(self._run_window(records, digests, ctx, sid, span,
                                             degrade=budget_on))

        outcomes: dict[str, list[tuple[tuple[int, int], object]]] = {}
        if jobs:
            results = await asyncio.gather(*jobs)
            for (sid, span), result in zip(jobs_meta, results):
                bucket = outcomes.setdefault(sid, [])
                if isinstance(result, BaseException):
                    bucket.append((span, result))
                else:
                    bucket.extend(result)          # V20 leaf results, span order

        # Phase 2: synchronous, deterministic per-session pass in batch order.
        for sid, items in sessions.items():
            split = any(getattr(item, "session_split", False) for item in items)
            if not refine or len(items) == 1:
                # rules / lone-frame degradation: the session becomes one
                # episode as-is; noise_filter / min_len do not apply (S11).
                self._emit_episode(batch, sid, items, split=split)
                continue
            failures = [result for _, result in outcomes[sid]
                        if isinstance(result, BaseException)]
            if failures:
                self._dispose_failed(batch, ctx, sid, items, split=split,
                                     failures=failures)
                continue
            rel: list[str | None] = [None] * len(items)
            for (start, end), verdicts in outcomes[sid]:
                for i in range(end - start):       # unconditional overwrite ⇒
                    rel[start + i] = verdicts[i]   # seam frame goes to the
                                                   # later window
            self._assemble(batch, ctx, sid, items, rel, split=split)
        return batch                               # the SAME list object (②b)

    async def _run_window(self, records: list[Record], digests: list[str],
                          ctx: "RunContext", sid: str, span: tuple[int, int],
                          *, degrade: bool):
        """One original window with per-window error capture: only the big
        three escape (contract ④ — everything else becomes the session-level
        on_error disposition, S26). ``records``/``digests`` are the SESSION
        vectors — sub-spans slice them, so V20 splits re-dispatch without
        re-digesting. Returns the leaf results [(sub-span, verdicts), ...] or
        the failure exception. ``degrade`` = budget on (V20's split-retry is a
        budget-mode reaction; budget-off overflow signals — the unconditional
        200-shaped oracle — take the plain failure path below and classify in
        _dispose_failed). Every actually dispatched window, split sub-windows
        included, counts segment.windows (V13④)."""
        async def judge(sub: tuple[int, int]) -> list[str]:
            ctx.metrics.count(_COUNTER_WINDOWS)
            return await _judge_window(records[sub[0]:sub[1]], ctx,
                                       session_id=sid, span=sub,
                                       digests=digests[sub[0]:sub[1]])

        try:
            if degrade:
                return await _judge_span_degrading(judge, span, ctx)
            return [(span, await judge(span))]
        except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:  # noqa: BLE001 — record-level isolation is absolute
            return e

    def _dispose_failed(self, batch: list[PipelineItem], ctx: "RunContext",
                        sid: str, items: list[PipelineItem], *, split: bool,
                        failures: list[BaseException]) -> None:
        """Two-form window-failure disposition (spec 3.14.6). "keep" (default):
        the session abandons ALL window verdicts and survives as ONE whole
        episode — evidence triple = duck-typed segment_degraded (→
        _meta.stream.degraded) + error event + segment.failures counter, never
        item.errors (S26). "fail": every session member fails → rejects.
        v1.11 (V27①): the kind routes through budget.classify_stage_error
        FIRST — ContextOverflowError → "context_overflow" (every rejected
        member counts budget.overflow_records at this reject site, the V13②
        convention shared with annotate/quality/verify — the report reads the
        counter, never rejects), OutputTruncatedError → "output_truncated" —
        falling back to the existing segmentation_invalid; an imprecise
        vocabulary here would break the §3.5 attribution. The first failure
        keys the classification and the message (the pre-v1.11 message
        semantics)."""
        first = failures[0]
        kind = (budget_mod.classify_stage_error(first)
                or ErrorKind.SEGMENTATION_INVALID.value)
        windows_failed = len(failures)
        message = str(first)
        if self.cfg.segment.on_error == "fail":
            error = StageError(stage=self.name, kind=kind, message=message,
                               retryable=False)
            for item in items:
                item.errors.append(error)
                item.status = "failed"
                if kind == ErrorKind.CONTEXT_OVERFLOW.value:
                    ctx.metrics.count("budget.overflow_records")  # V13②: per reject
        else:                                      # "keep"
            self._emit_episode(batch, sid, items, split=split,
                               degraded={"kind": kind,
                                         "windows_failed": windows_failed})
        ctx.metrics.count(_COUNTER_FAILURES)
        ctx.metrics.event(_EV_ERROR, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(),
                          payload={"stage": self.name, "kind": kind,
                                   "message": message, "retryable": False})

    def _assemble(self, batch: list[PipelineItem], ctx: "RunContext", sid: str,
                  items: list[PipelineItem], rel: list[str | None], *,
                  split: bool) -> None:
        """Deterministic segment assembly (spec 3.14.4 成段流程): ① noise
        removal (noise_filter=true; false keeps interruption frames as
        non-boundary members), ② boundary split (session first frame is always
        a segment head), ③ min_len check on the LLM-refined segments (S11),
        ④ one episode per surviving segment."""
        seg = self.cfg.segment
        kept: list[tuple[int, PipelineItem]] = []
        for idx, item in enumerate(items):
            if seg.noise_filter and rel[idx] == _NOISE_RELATION:   # incl. frame 0
                item.status = "dropped_noise"
                item.noise_attribution = ("segment", "noise")  # type: ignore[attr-defined]
            else:
                kept.append((idx, item))

        segments: list[list[PipelineItem]] = []
        current: list[PipelineItem] = []
        for idx, item in kept:
            # rel[0]'s boundary value never splits (the session's first frame
            # is always a segment head).
            if current and idx != 0 and rel[idx] in _BOUNDARY_RELATIONS:
                segments.append(current)
                current = []
            current.append(item)
        if current:
            segments.append(current)

        for members in segments:
            if len(members) < seg.min_len:         # S11: LLM-refined cuts only
                for item in members:
                    item.status = "dropped_noise"
                    item.noise_attribution = ("segment", "below_min_len")  # type: ignore[attr-defined]
                    ctx.metrics.count(_COUNTER_BELOW_MIN_LEN)
                continue
            self._emit_episode(batch, sid, members, split=split)

    @staticmethod
    def _emit_episode(batch: list[PipelineItem], sid: str,
                      members: list[PipelineItem], *, split: bool,
                      degraded: Mapping | None = None) -> None:
        """Absorb the member envelopes and tail-append one sequence envelope
        (contract ②b): id = sha256("\\n".join(member ids))[:16] fixed at
        formation; text/raw/ui_tree/image = None; ref inherits the first member
        (S24); session_id stamped; session_split / segment_degraded travel as
        duck-typed attributes for M11's _meta.stream."""
        records = tuple(item.record for item in members)
        joined = "\n".join(record.id for record in records)
        first = records[0]
        episode_record = Record(
            id=hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16],
            modality=first.modality,
            text=None, raw=None, ui_tree=None, image=None,
            ref=RecordRef(source_file=first.ref.source_file,
                          line_no=first.ref.line_no,
                          pair_index=first.ref.pair_index,
                          generated_from=(), generator=None),
            kind="sequence", members=records)
        for item in members:
            item.status = "absorbed"
        episode = PipelineItem(record=episode_record, session_id=sid)
        if split:
            episode.session_split = True  # type: ignore[attr-defined]
        if degraded is not None:
            episode.segment_degraded = dict(degraded)  # type: ignore[attr-defined]
        batch.append(episode)

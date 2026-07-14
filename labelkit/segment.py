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
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import TYPE_CHECKING, Mapping, Sequence

from labelkit.errors import CircuitBreakerTripped, ErrorKind
from labelkit.types import (
    PipelineItem,
    Record,
    RecordRef,
    StageError,
    digest_is_poor,
    frame_digest,
    tree_diff,
)

from labelkit.llm_client import Message, Part, PromptBundle
from labelkit.schema_engine import segment_window_schema

if TYPE_CHECKING:
    from labelkit.config.model import ResolvedConfig
    from labelkit.stage import RunContext

_logger = logging.getLogger("labelkit.segment")

# Event names (exact strings per CONTRACTS.md §7.14 / §8.1).
_EV_BOUNDARY = "segment.boundary"
_EV_ERROR = "error"

# Counter keys owned by M14 (CONTRACTS.md §9.3; counts.episodes / absorbed /
# dropped_noise are metered by M10).
_COUNTER_FAILURES = "segment.failures"
_COUNTER_BELOW_MIN_LEN = "segment.below_min_len"
_COUNTER_DIGEST_POOR = "segment.digest_poor_frames"

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
_DIGEST_POOR_WARNING = ("帧摘要贫瘠（可见文本节点为零）：纯文本边界裁决依据不足，"
                        "建议开启 segment.use_vision 附帧截图补偿")


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
                         cfg: "ResolvedConfig", with_reason: bool) -> PromptBundle:
    """Deterministic assembly of the CONTRACTS §10.9 template.

    system: the frozen three-step deductive criteria with the window frame
    count substituted, the optional segment.context line (omitted when empty),
    and the structure line with or without the reason fragment. user: ONE
    message, one text part per frame — "[帧 {i}] {digest}" plus, from the
    second frame on and when the caller supplied a diff, the "[帧 {i} 变更]"
    line; under segment.use_vision each frame's digest part is preceded by
    that frame's image part (§10.1/§10.10 single-message multi-part shape).
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
        if seg.use_vision and frame.image is not None:
            parts.append(Part(kind="image", image=frame.image))
        text = _FRAME_LABEL_TMPL.format(
            i=i, digest=frame_digest(frame, seg.digest_max_chars))
        diff = diffs[i] if i < len(diffs) else None
        if i >= 1 and diff is not None:            # 窗首帧无此行
            text += "\n" + _DIFF_LABEL_TMPL.format(i=i, diff=render_tree_diff(diff))
        parts.append(Part(kind="text", text=text))
    messages.append(Message(role="user", parts=tuple(parts)))
    return PromptBundle(messages=tuple(messages))


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
                        span: tuple[int, int]) -> list[str]:
    """Shared implementation behind ``judge_window`` and the stage's window
    calls — the keyword-only extras carry the event-payload context (session_id
    + window span) that the frozen public signature cannot."""
    cfg = ctx.cfg
    with_reason = _reason_requested(cfg)
    # Adjacent-frame diffs are pre-assembled code-side; the window's first
    # frame carries none. Bounds quantization reuses the tool's single
    # quantization knob (dedup.bounds_quantize_px — the M3 serialize precedent)
    # so pixel jitter never floods added/removed.
    quantize = cfg.dedup.bounds_quantize_px
    diffs: list[Mapping | None] = [None]
    for i in range(1, len(frames)):
        diffs.append(tree_diff(frames[i - 1].ui_tree, frames[i].ui_tree, quantize))
    prompt = build_segment_prompt(frames, diffs, cfg, with_reason)
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
    """Sliding-window spans over a session of n frames (spec 3.14.4 pseudocode):
    window = [start, end), stride = window − 1 (1-frame overlap — the seam
    frame's whole verdict belongs to the later window during stitching)."""
    spans: list[tuple[int, int]] = []
    start = 0
    while start < n:
        end = min(start + window, n)
        spans.append((start, end))
        if end == n:
            break
        start += window - 1
    return spans


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
            for span in _window_spans(len(items), seg.window):
                frames = [item.record for item in items[span[0]:span[1]]]
                jobs_meta.append((sid, span))
                jobs.append(self._run_window(frames, ctx, sid, span))

        outcomes: dict[str, list[tuple[tuple[int, int], object]]] = {}
        if jobs:
            results = await asyncio.gather(*jobs)
            for (sid, span), result in zip(jobs_meta, results):
                outcomes.setdefault(sid, []).append((span, result))

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
                                     windows_failed=len(failures),
                                     message=str(failures[0]))
                continue
            rel: list[str | None] = [None] * len(items)
            for (start, end), verdicts in outcomes[sid]:
                for i in range(end - start):       # unconditional overwrite ⇒
                    rel[start + i] = verdicts[i]   # seam frame goes to the
                                                   # later window
            self._assemble(batch, ctx, sid, items, rel, split=split)
        return batch                               # the SAME list object (②b)

    async def _run_window(self, frames: list[Record], ctx: "RunContext",
                          sid: str, span: tuple[int, int]):
        """One window call with per-window error capture: only the big three
        escape (contract ④ — everything else becomes the session-level
        on_error disposition, S26)."""
        try:
            return await _judge_window(frames, ctx, session_id=sid, span=span)
        except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:  # noqa: BLE001 — record-level isolation is absolute
            return e

    def _dispose_failed(self, batch: list[PipelineItem], ctx: "RunContext",
                        sid: str, items: list[PipelineItem], *, split: bool,
                        windows_failed: int, message: str) -> None:
        """segmentation_invalid two-form disposition (spec 3.14.6). "keep"
        (default): the session abandons ALL window verdicts and survives as ONE
        whole episode — evidence triple = duck-typed segment_degraded (→
        _meta.stream.degraded) + error event + segment.failures counter, never
        item.errors (S26). "fail": every session member fails → rejects."""
        kind = ErrorKind.SEGMENTATION_INVALID.value
        if self.cfg.segment.on_error == "fail":
            error = StageError(stage=self.name, kind=kind, message=message,
                               retryable=False)
            for item in items:
                item.errors.append(error)
                item.status = "failed"
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

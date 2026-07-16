"""M16 stitch stage (spec 3.16, CONTRACTS.md §7.16) — v1.9 thread stitching.

Conservatively stitches same-session fragments back into threads: per session
(batch position order = session order, M10 whole-session packing) the segment
products — active episode envelopes plus rescue candidates re-formed from
contiguous runs of ``below_min_len``-dropped frames (T11) — walk a MONOTONIC
selection pool in session order. One LLM judgment per candidate (§10.11 prompt:
open-thread summary cards most-recently-active first + the candidate's card,
validated against ``schema_engine.stitch_schema()``), gated by the T9
conservative conjunction: a merge requires the LLM ``resume`` verdict AND a
mechanical-prior whitelist hit (app-set intersection / digest entity overlap /
return-to-same-page). Merging rebinds the surviving envelope's Record with the
member union (session-order ascending; record.id is NEVER recomputed — the M7
surgery precedent) and marks the merged episode envelope ``stitched`` — the
contract-②c shape; rescue hits additionally flip their member frames
dropped_noise → absorbed (②c③). A bounded second pass (T19) re-judges the
single-fragment threads left by pass 1 against all other session threads with
the survivor direction REVERSED (candidate becomes the shell). Multi-fragment
threads finally get ``seam_indexes`` / ``seam_interrupted_by`` duck marks
(T20/M-1: a splice pair is a seam iff its session-order gap holds ≥ 1 frame
absorbed by a DIFFERENT thread) plus the ``stitch_fragments`` metadata M11
renders into ``_meta.stream.fragments``. Chain position: segment → stitch →
dedup. Failure policy ``stitch.on_error``: "keep" (default) lets an episode
candidate open its own thread with the evidence pair error-event +
``stitch.failures`` (never item.errors, S26 form); "fail" fails ONLY the
episode-candidate envelope (kind=stitch_invalid; member frames stay absorbed);
rescue candidates never take the fail path — a failed rescue judgment is a
miss (B-2).

``stitch.votes`` > 1 (T18) draws n samples per judgment at the profile default
temperature and requires a STRICT majority on the complete (verdict,
thread_ref) pair (M-4); any split falls back to the conservative outcome
(episode → new, rescue → miss).

The app/activity/title/entity extraction loops below are M16's OWN COPY of the
``types.frame_digest`` internals (T9 feasibility ruling — the extract._diff_text
precedent: operator modules never depend on each other, and rendered digest
strings are never re-parsed).
"""
from __future__ import annotations

import asyncio
import dataclasses
from typing import TYPE_CHECKING, Mapping, Sequence

from labelkit.common.errors import (
    CircuitBreakerTripped,
    ErrorKind,
    SchemaViolation,
)
from labelkit.common.contracts.types import (
    PipelineItem,
    Record,
    StageError,
    frame_digest,
    tree_diff,
)

from labelkit.common.runtime.llm_client import Message, Part, PromptBundle
from labelkit.common.runtime.schema_engine import stitch_schema

if TYPE_CHECKING:
    from labelkit.common.config.model import ResolvedConfig
    from labelkit.common.contracts.stage import RunContext

_STAGE_NAME = "stitch"

# Event names (exact strings per CONTRACTS.md §7.16 / §8.1).
_EV_JUDGE = "stitch.judge"
_EV_THREAD = "stitch.thread"
_EV_ERROR = "error"

# Counter keys owned by M16 (CONTRACTS.md §9.3 → report.stream.stitch;
# counts.stitched / counts.threads are metered/derived by M10).
_COUNTER_JUDGMENTS = "stitch.judgments"            # pass-1 judgments (episode + rescue)
_COUNTER_REPASS_JUDGMENTS = "stitch.repass_judgments"
_COUNTER_RESCUED_SHORT = "stitch.rescued_short"    # unit = FRAMES flipped (m-10)
_COUNTER_SEAMS = "stitch.seams"
_COUNTER_FAILURES = "stitch.failures"

# Prior whitelist leg names (T9, disjunction; trace payload vocabulary).
_PRIOR_APP = "app_overlap"
_PRIOR_ENTITY = "entity_overlap"
_PRIOR_PAGE = "same_page"

# M16's own copy of the frame_digest extraction keys (T9 ruling — see module
# docstring; types.py stays untouched).
_APP_KEYS = ("package", "package_name", "pkg")
_ACTIVITY_KEYS = ("activity", "activity_name", "window_title")

# Chinese prompt fragments — the §10.11 stitch-judgment template (frozen once
# CONTRACTS.md lands; mirror of segment.py's §10.9 builder conventions).
_SYSTEM_HEAD = (
    "你是屏幕操作流的线索缝合审核员。下面给出当前会话中 {P} 条开放线索的摘要卡"
    "（按最近活跃降序排列）与一张候选碎片摘要卡。\n"
    "判断该候选碎片是恢复其中某条线索（用户切回了之前挂起的同一任务），还是开启一个新任务：\n"
    "- resume: 候选与某条线索是同一任务的延续——任务实体跨碎片延续（订单号、地点、商品、"
    "联系人等再次出现）、返回同一页面继续操作、或 App 与操作语境明确承接；给出该线索编号。\n"
    "- new: 候选是一个新任务。\n"
    "保守偏置：仅在证据明确指向同一任务时判 resume；证据不足、模糊或仅有表面相似"
    "（同 App 不同任务、同类页面不同对象）时一律判 new——错缝的代价高于漏缝。\n"
    "若当前无开放线索，恒判 new。\n"
    "task_name 用一句话概括任务：resume 时给出该线索合并候选后的任务名（滚动更新），"
    "new 时给出新任务名。"
)
_STRUCTURE_SENTENCE = "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容："
_STRUCTURE_SHAPE = ('{"verdict": "resume"|"new", "thread_ref": <线索编号|null>,\n'
                    ' "task_name": <一句话任务名>, "reason": <一句话理由>,\n'
                    ' "confidence": "high"|"medium"|"low"}')
_EMPTY_POOL_LINE = "（当前无开放线索）"
_THREAD_CARD_HEAD = "[线索 {i}] 任务名: {task_name}"
_CANDIDATE_CARD_HEAD = "[候选碎片] 类型: {kind}"
_CANDIDATE_KIND_EPISODE = "分段产出"
_CANDIDATE_KIND_RESCUE = "短段救援"
_SEAM_PAIR_LABEL = "接续对（线索尾帧 → 候选首帧）变更: "


# ── pure evidence extraction (M16's own copies, T9) ─────────────────────────

def record_app(record: Record) -> str | None:
    """First non-empty app value among package/package_name/pkg over visible
    nodes in DFS order — byte-identical to the frame_digest head rule."""
    if record.ui_tree is None:
        return None
    for node in record.ui_tree.nodes:
        if not node.visible:
            continue
        for key in _APP_KEYS:
            value = node.extra.get(key)
            if value:
                return value
    return None


def app_set(records: Sequence[Record]) -> frozenset[str]:
    """Distinct app values over a member sequence (prior leg ①). Text-modality
    frames carry no tree → empty set (the leg silently never fires)."""
    return frozenset(app for app in (record_app(r) for r in records) if app)


def entity_set(record: Record) -> frozenset[str]:
    """Salient entity pieces of one frame — the frame_digest salient rule
    (ordered de-dup non-empty text/content_desc of visible nodes) as a SET for
    the prior-leg-② overlap judgment. Text modality → empty set."""
    if record.ui_tree is None:
        return frozenset()
    pieces: set[str] = set()
    for node in record.ui_tree.nodes:
        if not node.visible:
            continue
        for piece in (node.text, node.content_desc):
            if piece:
                pieces.add(piece)
    return frozenset(pieces)


def page_identity(record: Record) -> tuple[str, str, str | None] | None:
    """Prior leg ③ page identity = app + activity (+ DFS-first visible title).
    Requires BOTH app and activity — capture-side dumps often omit activity
    ("often absent", types.py note), in which case the leg silently fails
    (acceptable disjunction downgrade, T9 data-dependency clause)."""
    if record.ui_tree is None:
        return None
    app = activity = title = None
    for node in record.ui_tree.nodes:
        if not node.visible:
            continue
        if app is None:
            for key in _APP_KEYS:
                value = node.extra.get(key)
                if value:
                    app = value
                    break
        if activity is None:
            for key in _ACTIVITY_KEYS:
                value = node.extra.get(key)
                if value:
                    activity = value
                    break
        if title is None and node.text:
            title = node.text
    if app is None or activity is None:
        return None
    return (app, activity, title)


def prior_hits(thread_members: Sequence[Record],
               fragment_tails: Sequence[Record],
               candidate_members: Sequence[Record]) -> list[str]:
    """The T9 mechanical-prior whitelist — which of the three disjunctive legs
    hit between a thread and a candidate. Deterministic, zero LLM:
      ① app_overlap:    thread app set ∩ candidate app set ≠ ∅
      ② entity_overlap: thread TAIL frame entities ∩ candidate HEAD frame
                        entities ≠ ∅ (E5 pair: 挂起尾 × 恢复首)
      ③ same_page:      candidate head frame's page identity equals SOME
                        fragment-tail frame's page identity (E6 cue-guided
                        resumption; leg dead when activity is absent)."""
    hits: list[str] = []
    if app_set(thread_members) & app_set(candidate_members):
        hits.append(_PRIOR_APP)
    if thread_members and candidate_members and (
            entity_set(thread_members[-1]) & entity_set(candidate_members[0])):
        hits.append(_PRIOR_ENTITY)
    cand_page = page_identity(candidate_members[0]) if candidate_members else None
    if cand_page is not None and any(
            page_identity(tail) == cand_page for tail in fragment_tails):
        hits.append(_PRIOR_PAGE)
    return hits


# ── pure card / prompt assembly (§10.11) ────────────────────────────────────

def _diff_text(diff: Mapping) -> str:
    """Fixed textualization of a tree_diff mapping for the card's 接续对 line —
    same fixed form as M14's §10.9 rendering; this is M16's own copy (operator
    modules never depend on each other, spec §2.2)."""
    text = (f"新增 {diff['added']} 节点，移除 {diff['removed']} 节点，"
            f"文本变化 {diff['text_changed']} 处，"
            f"变更比例 {diff['change_ratio']:.0%}")
    if diff["app_changed"]:
        text += "，应用切换"
    if diff["title_changed"]:
        text += "，标题变化"
    return text


def render_thread_card(index: int, task_name: str, members: Sequence[Record],
                       span: tuple[int, int], fragment_count: int,
                       candidate_head: Record | None,
                       cfg: "ResolvedConfig") -> str:
    """One open-thread summary card (T8 evidence face): app set, session-order
    span + member/fragment counts, first/last frame digests, and — when a
    candidate head is supplied — the E5 resumption pair (thread tail × candidate
    head) with its deterministic tree_diff change evidence. Frame digests are
    truncated to stitch.digest_max_chars (m-9)."""
    st = cfg.stitch
    apps = "、".join(sorted(app_set(members))) or "（未知）"
    lines = [
        _THREAD_CARD_HEAD.format(i=index, task_name=task_name or "（未命名）"),
        f"App 集合: {apps}",
        f"序号跨度: [{span[0]}, {span[1]}]｜帧数 {len(members)}｜碎片数 {fragment_count}",
        f"首帧摘要: {frame_digest(members[0], st.digest_max_chars)}",
        f"尾帧摘要: {frame_digest(members[-1], st.digest_max_chars)}",
    ]
    if candidate_head is not None:
        diff = tree_diff(members[-1].ui_tree, candidate_head.ui_tree,
                         cfg.dedup.bounds_quantize_px)
        lines.append(_SEAM_PAIR_LABEL + _diff_text(diff))
    return "\n".join(lines)


def render_candidate_card(kind: str, members: Sequence[Record],
                          span: tuple[int, int], cfg: "ResolvedConfig") -> str:
    """The candidate fragment's summary card. ``kind`` ∈ {"episode", "rescue"}
    renders as 分段产出 / 短段救援."""
    st = cfg.stitch
    kind_text = (_CANDIDATE_KIND_RESCUE if kind == "rescue"
                 else _CANDIDATE_KIND_EPISODE)
    apps = "、".join(sorted(app_set(members))) or "（未知）"
    return "\n".join([
        _CANDIDATE_CARD_HEAD.format(kind=kind_text),
        f"App 集合: {apps}",
        f"序号跨度: [{span[0]}, {span[1]}]｜帧数 {len(members)}",
        f"首帧摘要: {frame_digest(members[0], st.digest_max_chars)}",
        f"末帧摘要: {frame_digest(members[-1], st.digest_max_chars)}",
    ])


def build_stitch_prompt(thread_cards: Sequence[str], candidate_card: str,
                        cfg: "ResolvedConfig") -> PromptBundle:
    """Deterministic assembly of the §10.11 template. system: the frozen
    conservative-bias instruction with the pool card count substituted, the
    optional stitch.context line (omitted when empty), the structure sentence
    and shape. user: ONE message — one text part per thread card (already
    ordered most-recently-active first by the caller, T8 position-bias
    mitigation; an empty pool renders the fixed 零卡 line) and the candidate
    card as the final text part. Pure text: stitch never attaches images."""
    st = cfg.stitch
    lines = [_SYSTEM_HEAD.replace("{P}", str(len(thread_cards)))]
    if st.context:
        lines.append(st.context)
    lines.append(_STRUCTURE_SENTENCE)
    lines.append(_STRUCTURE_SHAPE)
    system = Message(role="system", parts=(Part(kind="text", text="\n".join(lines)),))

    parts: list[Part] = [Part(kind="text", text=card) for card in thread_cards]
    if not thread_cards:
        parts.append(Part(kind="text", text=_EMPTY_POOL_LINE))
    parts.append(Part(kind="text", text=candidate_card))
    return PromptBundle(messages=(system, Message(role="user", parts=tuple(parts))))


# ── votes aggregation (T18/M-4, pure) ───────────────────────────────────────

def aggregate_votes(samples: Sequence[Mapping]) -> Mapping | None:
    """Strict majority over the COMPLETE (verdict, thread_ref) judgment key
    (M-4): a pair whose count strictly exceeds n/2 wins and the first sample of
    the majority cluster is returned whole (task_name/reason travel with it).
    Any split short of a strict majority — including a verdict majority whose
    thread_ref splits — returns None (the caller falls back to the conservative
    outcome: episode → new, rescue → miss)."""
    if not samples:
        return None
    counts: dict[tuple, int] = {}
    first: dict[tuple, Mapping] = {}
    for sample in samples:
        key = (sample["verdict"], sample["thread_ref"])
        counts[key] = counts.get(key, 0) + 1
        first.setdefault(key, sample)
    best_key, best_n = max(counts.items(), key=lambda kv: kv[1])
    if best_n * 2 > len(samples):
        return first[best_key]
    return None


# ── one judgment (votes-aware) ──────────────────────────────────────────────

async def judge_stitch(thread_cards: Sequence[str], candidate_card: str,
                       ctx: "RunContext",
                       record_ids: tuple[str, ...] = ()) -> Mapping | None:
    """One candidate judgment through complete_validated(schema=stitch_schema()).
    votes == 1 (default): a single call at the profile default temperature.
    votes > 1 (T18): n concurrent samples of the SAME prompt, aggregated by the
    M-4 strict majority; a SchemaViolation sample abstains while provider/
    internal errors escalate (the classify self-consistency discipline) — zero
    surviving samples re-raise the last violation so the stage's on_error
    disposition applies. Returns the winning judgment object, or None when
    votes split short of a strict majority."""
    cfg = ctx.cfg
    prompt = build_stitch_prompt(thread_cards, candidate_card, cfg)
    schema = stitch_schema()
    n = cfg.stitch.votes
    if n == 1:
        obj, _usage, _attempts, _model = await ctx.schema_engine.complete_validated(
            cfg.stitch.llm, prompt, schema, record_ids=record_ids,
            batch_no=ctx.batch_no)
        return obj

    results = await asyncio.gather(
        *(ctx.schema_engine.complete_validated(
            cfg.stitch.llm, prompt, schema, record_ids=record_ids,
            batch_no=ctx.batch_no) for _ in range(n)),
        return_exceptions=True)
    samples: list[Mapping] = []
    last_violation: SchemaViolation | None = None
    for res in results:
        if isinstance(res, SchemaViolation):
            last_violation = res                   # this sample abstains
        elif isinstance(res, BaseException):
            raise res                              # provider/internal errors escalate
        else:
            obj, _usage, _attempts, _model = res
            samples.append(obj)
    if not samples:
        raise last_violation if last_violation is not None else SchemaViolation(
            ["stitch votes: all samples failed"], "")
    return aggregate_votes(samples)


# ── session-local state ─────────────────────────────────────────────────────

class _Fragment:
    """One thread fragment: a contiguous session-order block of own members."""

    __slots__ = ("first_pos", "last_pos", "member_count", "cause",
                 "source_episode", "first", "last")

    def __init__(self, members: Sequence[Record], first_pos: int, last_pos: int,
                 cause: str, source_episode: str | None):
        self.first_pos = first_pos
        self.last_pos = last_pos
        self.member_count = len(members)
        self.cause = cause                      # "origin" | "resumed" | "rescued"
        self.source_episode = source_episode    # original episode_id; rescue → None
        self.first = members[0]
        self.last = members[-1]


class _Thread:
    """One open/closed thread: the surviving envelope plus rolling card state."""

    __slots__ = ("envelope", "fragments", "task_name", "last_active", "alive",
                 "head_pos", "tail_pos")

    def __init__(self, envelope: PipelineItem, fragment: _Fragment,
                 task_name: str, clock: int):
        self.envelope = envelope
        self.fragments: list[_Fragment] = [fragment]
        self.task_name = task_name
        self.last_active = clock
        self.alive = True
        self.head_pos = fragment.first_pos
        self.tail_pos = fragment.last_pos

    @property
    def members(self) -> tuple[Record, ...]:
        return self.envelope.record.members

    def fragment_tails(self) -> list[Record]:
        return [fragment.last for fragment in self.fragments]


class _Candidate:
    """One pass-1 candidate: an episode envelope, or a rescue run of frames."""

    __slots__ = ("kind", "envelope", "frames", "members", "first_pos", "last_pos")

    def __init__(self, kind: str, members: Sequence[Record],
                 first_pos: int, last_pos: int,
                 envelope: PipelineItem | None = None,
                 frames: Sequence[PipelineItem] = ()):
        self.kind = kind                        # "episode" | "rescue"
        self.envelope = envelope                # episode candidates only
        self.frames = list(frames)              # rescue candidates only
        self.members = tuple(members)
        self.first_pos = first_pos
        self.last_pos = last_pos


def select_eviction(pool: Sequence[_Thread], candidate_pos: int,
                    stale_gap_steps: int) -> _Thread:
    """Pool-full eviction priority (T8/M-3): ① threads whose suspension span
    (candidate position − thread tail position) exceeds stale_gap_steps first
    (0 = leg off), LRU among the stale ones; ② plain LRU fallback. Deterministic
    over pool insertion order (ties keep the earlier thread)."""
    if stale_gap_steps > 0:
        stale = [t for t in pool
                 if candidate_pos - t.tail_pos > stale_gap_steps]
        if stale:
            return min(stale, key=lambda t: t.last_active)
    return min(pool, key=lambda t: t.last_active)


def span_distance(a_head: int, a_tail: int, b_head: int, b_tail: int) -> int:
    """Session-order distance between two spans: 0 when they overlap, else the
    gap between the nearer edges — the T19 pool-truncation metric."""
    if a_tail < b_head:
        return b_head - a_tail
    if b_tail < a_head:
        return a_head - b_tail
    return 0


def compute_seams(members: Sequence[Record], position_of: Mapping[str, int],
                  owner_task: Mapping[str, str],
                  own_ids: frozenset[str],
                  frame_ids_by_pos: Sequence[str],
) -> tuple[tuple[int, ...], tuple[tuple[str, ...], ...]]:
    """Seam determination (T20/M-1): an adjacent member pair ⟨i, i+1⟩ is a seam
    iff the session-order gap between the two members contains ≥ 1 frame
    absorbed by a DIFFERENT thread. Noise-only gaps (and gaps of frames owned
    by no thread) are NOT seams — extract judges those pairs normally, matching
    the v1.8 剔噪 convention. Returns (seam_indexes, interrupted_by) where
    seam_indexes are LEFT-member indexes in the rebound member tuple (m-8:
    same coordinate as Transition.index, range [0, len(members)−2]) and
    interrupted_by lists the distinct interrupting threads' task_names in gap
    order (M-1: never empty for a seam)."""
    seams: list[int] = []
    interrupted: list[tuple[str, ...]] = []
    for i in range(len(members) - 1):
        left = position_of.get(members[i].id)
        right = position_of.get(members[i + 1].id)
        if left is None or right is None:
            continue
        names: list[str] = []
        for pos in range(left + 1, right):
            frame_id = frame_ids_by_pos[pos]
            if frame_id in own_ids:
                continue
            task = owner_task.get(frame_id)
            if task is not None and task not in names:
                names.append(task)
        if names:
            seams.append(i)
            interrupted.append(tuple(names))
    return tuple(seams), tuple(interrupted)


# ── stage ────────────────────────────────────────────────────────────────────

class StitchStage:
    name = "stitch"

    def __init__(self, cfg: "ResolvedConfig"):
        self.cfg = cfg

    async def run(self, batch: list[PipelineItem],
                  ctx: "RunContext") -> list[PipelineItem]:
        # Selection & idempotency: sessions are processed strictly in batch
        # position order (= session order); episode candidates are the active
        # sequence envelopes not yet stitched (thread_id is stamped at thread
        # opening, so re-entry costs zero calls). Sessions with zero episode
        # candidates are skipped whole — rescue candidates alone can never
        # merge into an empty pool (B-2).
        frames_by_sid: dict[str, list[PipelineItem]] = {}
        episodes_by_sid: dict[str, list[PipelineItem]] = {}
        order: list[str] = []
        for item in batch:
            if item.session_id is None:
                continue
            if item.session_id not in frames_by_sid:
                frames_by_sid[item.session_id] = []
                episodes_by_sid[item.session_id] = []
                order.append(item.session_id)
            if item.record.kind == "single":
                frames_by_sid[item.session_id].append(item)
            elif (item.record.kind == "sequence" and item.status == "active"
                    and item.thread_id is None):
                episodes_by_sid[item.session_id].append(item)

        # Sessions run SEQUENTIALLY: the pool is a serial decision process and
        # thread state is session-local — deterministic event/judgment order,
        # zero rng (calls per session are few by the §3.5 cost model).
        for sid in order:
            if not episodes_by_sid[sid]:
                continue
            await self._run_session(sid, frames_by_sid[sid],
                                    episodes_by_sid[sid], ctx)
        return batch                            # the SAME list object (②c)

    # ── per-session driver ───────────────────────────────────────────────────

    async def _run_session(self, sid: str, frames: list[PipelineItem],
                           episodes: list[PipelineItem],
                           ctx: "RunContext") -> None:
        st = self.cfg.stitch
        position_of: dict[str, int] = {}
        for i, frame in enumerate(frames):
            position_of.setdefault(frame.record.id, i)

        candidates = self._assemble_candidates(frames, episodes, position_of)
        threads: list[_Thread] = []             # creation order; incl. evicted
        pool: list[_Thread] = []                # open threads only
        clock = 0

        for cand in candidates:
            if cand.kind == "rescue" and not pool:
                continue                        # B-2: zero calls, stays dropped
            pool_view = sorted(pool, key=lambda t: t.last_active, reverse=True)
            cards = [render_thread_card(i, t.task_name, t.members,
                                        (t.head_pos, t.tail_pos),
                                        len(t.fragments), cand.members[0],
                                        self.cfg)
                     for i, t in enumerate(pool_view, start=1)]
            cand_card = render_candidate_card(cand.kind, cand.members,
                                              (cand.first_pos, cand.last_pos),
                                              self.cfg)
            try:
                outcome = await judge_stitch(cards, cand_card, ctx,
                                             record_ids=(cand.members[0].id,))
            except (CircuitBreakerTripped, KeyboardInterrupt,
                    asyncio.CancelledError):
                raise
            except Exception as e:  # noqa: BLE001 — record-level isolation is absolute
                self._dispose_failure(cand, e, threads, pool, clock, ctx)
                clock += 1
                continue
            ctx.metrics.count(_COUNTER_JUDGMENTS)
            target, hits = self._resolve_merge(outcome, pool_view, cand)
            if cand.kind == "episode":
                if target is None:
                    task_name = outcome["task_name"] if outcome else ""
                    self._open_thread(cand, task_name, threads, pool, clock)
                else:
                    self._merge_pass1(target, cand, outcome, position_of, clock)
            elif target is not None:            # rescue hit
                self._merge_rescue(target, cand, outcome, position_of, clock,
                                   ctx)
            self._emit_judge(cand, outcome, target, hits, sid, ctx,
                             repass=False)
            clock += 1

        if st.repass:
            clock = await self._repass(sid, threads, position_of, clock, ctx)

        self._finalize_session(sid, threads, frames, position_of, ctx)

    def _assemble_candidates(self, frames: list[PipelineItem],
                             episodes: list[PipelineItem],
                             position_of: Mapping[str, int]) -> list[_Candidate]:
        """Candidate stream in session order (T11): one candidate per episode
        envelope, plus — when stitch.rescue_short — one rescue candidate per
        CONTIGUOUS session-order run of below_min_len-dropped frames (no other
        frame in between; the run re-forming deliberately ignores the original
        segment cuts — adjacent short segments fuse into one candidate).
        reason="noise" frames never enter the candidate stream (V4 closure)."""
        candidates: list[_Candidate] = []
        for episode in episodes:
            members = episode.record.members
            positions = [position_of[m.id] for m in members
                         if m.id in position_of]
            first = min(positions) if positions else 0
            last = max(positions) if positions else 0
            candidates.append(_Candidate("episode", members, first, last,
                                         envelope=episode))
        if self.cfg.stitch.rescue_short:
            run: list[PipelineItem] = []
            for frame in frames + [None]:       # sentinel flushes the tail run
                rescueable = (
                    frame is not None
                    and frame.status == "dropped_noise"
                    and getattr(frame, "noise_attribution", None)
                    == ("segment", "below_min_len"))
                if rescueable:
                    run.append(frame)
                    continue
                if run:
                    members = [f.record for f in run]
                    candidates.append(_Candidate(
                        "rescue", members,
                        position_of[members[0].id], position_of[members[-1].id],
                        frames=run))
                    run = []
        candidates.sort(key=lambda c: c.first_pos)
        return candidates

    # ── merge gate (T8 verdict × T9 priors) ─────────────────────────────────

    def _resolve_merge(self, outcome: Mapping | None, pool_view: list[_Thread],
                       cand: _Candidate) -> tuple[_Thread | None, list[str]]:
        """Map one judgment outcome onto a merge target. None (votes split) and
        non-resume verdicts resolve to no target; a resume must name a valid
        1-based pool-card ordinal, and under bias="conservative" additionally
        clear the T9 prior conjunction — beyond stale_gap_steps the prior
        downgrades to TWO legs (E7 time decay)."""
        st = self.cfg.stitch
        if outcome is None or outcome.get("verdict") != "resume":
            return None, []
        ref = outcome.get("thread_ref")
        if not isinstance(ref, int) or not 1 <= ref <= len(pool_view):
            return None, []                     # conservative: invalid ref = new
        thread = pool_view[ref - 1]
        hits = prior_hits(thread.members, thread.fragment_tails(), cand.members)
        if st.bias == "llm":
            return thread, hits
        required = 1
        if st.stale_gap_steps > 0 and (
                cand.first_pos - thread.tail_pos > st.stale_gap_steps):
            required = 2
        if len(hits) >= required:
            return thread, hits
        return None, hits

    # ── state transitions (contract ②c) ─────────────────────────────────────

    def _open_thread(self, cand: _Candidate, task_name: str,
                     threads: list[_Thread], pool: list[_Thread],
                     clock: int) -> None:
        """Episode candidate opens a thread (rescue candidates NEVER reach
        here, B-2). Pool-full → evict one open thread first (M-3: closure
        happens ONLY here; the evicted thread stays a pass-2 target and a
        normal product)."""
        if len(pool) >= self.cfg.stitch.max_open:
            evicted = select_eviction(pool, cand.first_pos,
                                      self.cfg.stitch.stale_gap_steps)
            pool.remove(evicted)
        envelope = cand.envelope
        assert envelope is not None
        envelope.thread_id = envelope.record.id     # T22 identity chain
        fragment = _Fragment(cand.members, cand.first_pos, cand.last_pos,
                             cause="origin", source_episode=envelope.record.id)
        thread = _Thread(envelope, fragment, task_name, clock)
        threads.append(thread)
        pool.append(thread)

    def _merge_pass1(self, target: _Thread, cand: _Candidate, outcome: Mapping,
                     position_of: Mapping[str, int], clock: int) -> None:
        """Pass-1 merge: the thread-founding envelope survives (m-7), the
        candidate envelope becomes a stitched shell (②c①/②)."""
        assert cand.envelope is not None
        self._rebind(target, cand.members, position_of)
        cand.envelope.status = "stitched"
        target.fragments.append(_Fragment(
            cand.members, cand.first_pos, cand.last_pos,
            cause="resumed", source_episode=cand.envelope.record.id))
        self._touch(target, outcome, clock)

    def _merge_rescue(self, target: _Thread, cand: _Candidate, outcome: Mapping,
                      position_of: Mapping[str, int], clock: int,
                      ctx: "RunContext") -> None:
        """Rescue hit: member frames flip dropped_noise → absorbed (②c③) with
        the rescued_by audit mark; rescued_short counts FRAMES (m-10). No shell
        is produced — rescue candidates have no envelope form (T7 scope rule)."""
        self._rebind(target, cand.members, position_of)
        for frame in cand.frames:
            frame.status = "absorbed"
            frame.rescued_by = target.envelope.record.id  # type: ignore[attr-defined]
        ctx.metrics.count(_COUNTER_RESCUED_SHORT, len(cand.frames))
        target.fragments.append(_Fragment(
            cand.members, cand.first_pos, cand.last_pos,
            cause="rescued", source_episode=None))
        self._touch(target, outcome, clock)

    def _rebind(self, target: _Thread, new_members: Sequence[Record],
                position_of: Mapping[str, int]) -> None:
        """Record rebinding (②c②): member union in session-order ascending;
        record.id is NEVER recomputed (T6/T22 — the M7 surgery precedent)."""
        merged = sorted(
            (*target.members, *new_members),
            key=lambda record: position_of.get(record.id, 0))
        target.envelope.record = dataclasses.replace(
            target.envelope.record, members=tuple(merged))

    @staticmethod
    def _touch(target: _Thread, outcome: Mapping, clock: int) -> None:
        """Rolling card state after a merge: task_name updates from the hit
        judgment (M-6), span widens, recency advances."""
        task_name = outcome.get("task_name")
        if task_name:
            target.task_name = task_name
        first = min(fragment.first_pos for fragment in target.fragments)
        last = max(fragment.last_pos for fragment in target.fragments)
        target.head_pos, target.tail_pos = first, last
        target.last_active = clock

    def _dispose_failure(self, cand: _Candidate, exc: Exception,
                         threads: list[_Thread], pool: list[_Thread],
                         clock: int, ctx: "RunContext") -> None:
        """stitch_invalid two-form disposition (§3.16 失败语义). "keep"
        (default): an episode candidate opens its own thread — evidence pair =
        error event + stitch.failures counter, never item.errors (S26 form);
        a rescue candidate stays dropped_noise with the same evidence. "fail":
        ONLY the episode-candidate envelope fails (member frames stay absorbed
        — ②c grants no absorbed→failed migration); rescue candidates never
        take the fail path — the failure is a miss (B-2)."""
        kind = ErrorKind.STITCH_INVALID.value
        message = str(exc)
        if cand.kind == "episode":
            if self.cfg.stitch.on_error == "fail":
                assert cand.envelope is not None
                cand.envelope.errors.append(StageError(
                    stage=self.name, kind=kind, message=message,
                    retryable=False))
                cand.envelope.status = "failed"
            else:                               # "keep": bootstrap without a name
                self._open_thread(cand, "", threads, pool, clock)
        ctx.metrics.count(_COUNTER_FAILURES)
        ctx.metrics.event(_EV_ERROR, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(cand.members[0].id,),
                          payload={"stage": self.name, "kind": kind,
                                   "message": message, "retryable": False})

    # ── pass 2 (T19/M-2) ─────────────────────────────────────────────────────

    async def _repass(self, sid: str, threads: list[_Thread],
                      position_of: Mapping[str, int], clock: int,
                      ctx: "RunContext") -> int:
        """Bounded second pass: candidates = the single-fragment threads AT THE
        END of pass 1 (session order of their fragment); pool = all other alive
        session threads presented most-recently-active first, truncated to the
        max_open nearest-by-span when over (M-2: NOT interval intersection).
        The target set is a LIVE VIEW — a merge immediately updates spans and
        cards; merged-away candidates are consumed. Merge direction is REVERSED
        (T6 survivor rule): the candidate envelope becomes the shell, the
        target thread survives with the fragments re-sorted in session order."""
        st = self.cfg.stitch
        snapshot = [t for t in threads if t.alive and len(t.fragments) == 1]
        snapshot.sort(key=lambda t: t.fragments[0].first_pos)
        for cand_thread in snapshot:
            if not cand_thread.alive:
                continue                        # merged away earlier in pass 2
            others = [t for t in threads if t.alive and t is not cand_thread]
            if not others:
                continue                        # zero calls
            if len(others) > st.max_open:
                others = sorted(
                    others,
                    key=lambda t: (span_distance(cand_thread.head_pos,
                                                 cand_thread.tail_pos,
                                                 t.head_pos, t.tail_pos),
                                   t.head_pos))[:st.max_open]
            pool_view = sorted(others, key=lambda t: t.last_active, reverse=True)
            cards = [render_thread_card(i, t.task_name, t.members,
                                        (t.head_pos, t.tail_pos),
                                        len(t.fragments),
                                        cand_thread.members[0], self.cfg)
                     for i, t in enumerate(pool_view, start=1)]
            cand = _Candidate("episode", cand_thread.members,
                              cand_thread.head_pos, cand_thread.tail_pos,
                              envelope=cand_thread.envelope)
            cand_card = render_candidate_card("episode", cand.members,
                                              (cand.first_pos, cand.last_pos),
                                              self.cfg)
            try:
                outcome = await judge_stitch(cards, cand_card, ctx,
                                             record_ids=(cand.members[0].id,))
            except (CircuitBreakerTripped, KeyboardInterrupt,
                    asyncio.CancelledError):
                raise
            except Exception as e:  # noqa: BLE001 — record-level isolation
                # Pass-2 failure never fails an already-opened thread: the
                # candidate simply stays its own thread (keep-equivalent).
                ctx.metrics.count(_COUNTER_FAILURES)
                ctx.metrics.event(
                    _EV_ERROR, stage=self.name, batch_no=ctx.batch_no,
                    record_ids=(cand.members[0].id,),
                    payload={"stage": self.name,
                             "kind": ErrorKind.STITCH_INVALID.value,
                             "message": str(e), "retryable": False})
                clock += 1
                continue
            ctx.metrics.count(_COUNTER_REPASS_JUDGMENTS)
            target, hits = self._resolve_merge(outcome, pool_view, cand)
            if target is not None:
                self._merge_pass2(target, cand_thread, outcome, position_of,
                                  clock)
            self._emit_judge(cand, outcome, target, hits, sid, ctx,
                             repass=True)
            clock += 1
        return clock

    def _merge_pass2(self, target: _Thread, cand_thread: _Thread,
                     outcome: Mapping, position_of: Mapping[str, int],
                     clock: int) -> None:
        """Pass-2 merge, direction reversed (T6 survivor rule): the candidate
        thread's envelope becomes the shell, the target thread survives.
        Transferred fragments keep their member blocks; the candidate's origin
        fragment re-causes to "resumed" (it joined the target via a resume
        judgment; rescued fragments keep "rescued"); episode_id/thread_id stay
        with the surviving envelope (T22)."""
        self._rebind(target, cand_thread.members, position_of)
        cand_thread.envelope.status = "stitched"
        cand_thread.alive = False
        for fragment in cand_thread.fragments:
            if fragment.cause == "origin":
                fragment.cause = "resumed"
            target.fragments.append(fragment)
        target.fragments.sort(key=lambda fragment: fragment.first_pos)
        self._touch(target, outcome, clock)

    # ── finalization: seams + fragments metadata + events ────────────────────

    def _finalize_session(self, sid: str, threads: list[_Thread],
                          frames: list[PipelineItem],
                          position_of: Mapping[str, int],
                          ctx: "RunContext") -> None:
        """Stamp every surviving thread with its duck marks: stitch_fragments
        (session-order fragment table → _meta.stream.fragments), seam_indexes +
        seam_interrupted_by (T20/M-1), and emit one stitch.thread event per
        thread. The envelope-level order_span stays M11's (envelope semantics:
        a multi-fragment thread's span may contain other threads' frames)."""
        alive = [t for t in threads if t.alive]
        owner_task: dict[str, str] = {}
        for thread in alive:
            for member in thread.members:
                owner_task.setdefault(member.id, thread.task_name)
        frame_ids_by_pos = [frame.record.id for frame in frames]

        for thread in alive:
            thread.fragments.sort(key=lambda fragment: fragment.first_pos)
            envelope = thread.envelope
            envelope.stitch_fragments = tuple(  # type: ignore[attr-defined]
                {"order_span": [_order_key_repr(fragment.first),
                                _order_key_repr(fragment.last)],
                 "member_count": fragment.member_count,
                 "cause": fragment.cause,
                 "source_episode": fragment.source_episode}
                for fragment in thread.fragments)
            own_ids = frozenset(member.id for member in thread.members)
            seams, interrupted = compute_seams(
                thread.members, position_of, owner_task, own_ids,
                frame_ids_by_pos)
            envelope.seam_indexes = seams  # type: ignore[attr-defined]
            envelope.seam_interrupted_by = interrupted  # type: ignore[attr-defined]
            if seams:
                ctx.metrics.count(_COUNTER_SEAMS, len(seams))
            ctx.metrics.event(
                _EV_THREAD, stage=self.name, batch_no=ctx.batch_no,
                record_ids=(envelope.record.id,),
                payload={"session_id": sid,
                         "thread_id": envelope.record.id,
                         "task_name": thread.task_name,
                         "fragments": [dict(f) for f
                                       in envelope.stitch_fragments],  # type: ignore[attr-defined]
                         "seam_indexes": list(seams)})

    # ── trace event (stitch.judge) ───────────────────────────────────────────

    def _emit_judge(self, cand: _Candidate, outcome: Mapping | None,
                    target: _Thread | None, hits: list[str], sid: str,
                    ctx: "RunContext", *, repass: bool) -> None:
        """One stitch.judge event per judgment (T16): record_ids = the
        candidate fragment's first member id; verdict/thread_ref/confidence +
        the prior-leg hits; outcome None = a votes split (recorded as the
        conservative fallback verdict)."""
        payload: dict = {
            "session_id": sid,
            "candidate": cand.kind,
            "repass": repass,
            "verdict": outcome.get("verdict") if outcome else "new",
            "thread_ref": outcome.get("thread_ref") if outcome else None,
            "confidence": outcome.get("confidence") if outcome else None,
            "priors": list(hits),
            "merged": target is not None,
        }
        if outcome is None:
            payload["votes_split"] = True       # M-4 fallback left its trace
        else:
            payload["task_name"] = outcome.get("task_name")
            payload["reason"] = outcome.get("reason")
        if target is not None:
            payload["target_thread_id"] = target.envelope.record.id
        ctx.metrics.event(_EV_JUDGE, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(cand.members[0].id,), payload=payload)


def _order_key_repr(member: Record) -> str | int | None:
    """`fragments[].order_span` element — the member's order-key presentation
    (text = "file:line_no", UI = pair_index): M16's own copy of M11's
    rendering (operator modules never depend on each other, spec §2.2)."""
    ref = member.ref
    if ref.line_no is not None:
        return f"{ref.source_file}:{ref.line_no}"
    return ref.pair_index

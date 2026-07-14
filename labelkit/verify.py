"""M7 verify stage — LLM-as-a-Judge review of (record, annotation) pairs (spec 3.7).

Per CONTRACTS.md §7.6: each active item with an annotation is reviewed with the §10.5
verify prompt against ``VERDICT_SCHEMA`` (§10.7). Optional judges panel (odd count,
majority verdict, critiques merged with a ``judge`` field). Policy ``drop`` drops on
fail; policy ``repair`` feeds the failing judges' critiques back into M5's annotator
(``annotate_record`` + ``RepairContext``) for up to ``verify.max_repair_rounds``
re-annotations, then drops (``dropped_verify``). One ``verify.verdict`` trace event per
judge per round.

v1.8 stream branch (S7/S8/S31, spec 3.7 stream branch): sequence envelopes
(``record.kind == "sequence"``) under ``segment.enabled`` bypass into a stage-layer
driver — the §10.5 sequence-variant review (defect-table system text, six-section user
evidence incl. the ``[边界余量]`` boundary-margin section, validated against
``defect_verdict_schema()``) plus, under ``policy = "repair"``, two-phase batch-level
member surgery per round: concurrent review → synchronous defect routing in batch
position order (shrink / reclaim-claim / mark-only) → concurrent reclaim re-judgment
(``segment.judge_window`` direct calls) → concurrent seam re-extraction
(``extract.extract_transition`` direct calls) → synchronous record/transitions rebuild
→ concurrent re-annotation → next-round re-review. The non-stream path is a REGRESSION
ANCHOR: ``run_verify_loop``, ``VERDICT_SCHEMA`` usage and the classic prompt are
byte-unchanged.

Import note: the service/sibling modules this stage composes (``labelkit.llm_client``,
``labelkit.schema_engine``, ``labelkit.annotate``, and for the stream repair path the
sanctioned direct-call surfaces ``labelkit.segment.judge_window`` /
``labelkit.extract.extract_transition``) are imported lazily inside the functions that
need them, so importing ``labelkit.verify`` (and unit-testing its pure logic) never
requires those files to exist yet. The imported names and their use match CONTRACTS.md
exactly.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Mapping, Sequence

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
    VerificationResult,
    frame_digest,
)

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

# §10.5 v1.8 sequence variant, fixed Chinese text (CONTRACTS §10.5, frozen there).
_SEQ_SYSTEM_HEAD = ("你是标注质量审核员。给定任务指令、动作序列、边界余量与首末帧截图，独立判断该序列\n"
                    "（episode）的标注是否合格。")
_SEQ_SYSTEM_DIMS = ("评审维度: ① 是否遵循任务指令 ② 与动作序列及首末帧证据的事实一致性 ③ 字段语义是否正确填写\n"
                    "④ 段边界与成员构成是否成立（对照下列缺陷类型）")
_SEQ_SYSTEM_DEFECT_TYPES = (
    "缺陷类型（发现即列入 defects，可为空数组）:\n"
    "- label_mismatch: 标注的任务标签与序列证据不符\n"
    "- off_task_members: 段内混入与任务无关的成员帧（members 列出这些成员帧 id）\n"
    "- missing_head: 段首缺少任务起点帧（结合边界余量判断）\n"
    "- missing_tail: 段尾缺少任务终点帧（结合边界余量判断）\n"
    "- missing_members: 段中缺失成员帧（members 列出可指认的帧 id，无从指认则为 null）")
_SEQ_SYSTEM_TAIL = "先逐维度给出简短意见，再列缺陷表，最后给结论。"
_SEQ_SYSTEM_STRUCTURE = (
    "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
    '{"critiques": [{"aspect": <维度>, "opinion": <一句话意见>}, ...],\n'
    ' "defects": [{"kind": <缺陷类型>, "members": <帧 id 数组|null>,\n'
    '              "position": <位置说明|null>, "detail": <一句话>}, ...],\n'
    ' "verdict": "pass"|"fail"}')

# Sequence-evidence section labels + the §10.1 frozen step-line format. Operator
# modules never depend on each other (spec §2.2): M5/M4 carry their own copies of
# the same-format line template; this copy is M7's.
_LABEL_ACTION_SEQUENCE = "[动作序列]"
_LABEL_BOUNDARY_MARGIN = "[边界余量]"
_LABEL_FIRST_FRAME = "[首帧截图]"
_LABEL_LAST_FRAME = "[末帧截图]"
_MEMBER_DIGEST_MAX_CHARS = 400   # sequence excerpt-tier digest cap (M4 §7.3 mirror)

# Defect kind closed vocabulary in schema/enum order — the FIRST component of the
# S31 deterministic de-dup sort key.
DEFECT_KINDS = ("label_mismatch", "off_task_members", "missing_head",
                "missing_tail", "missing_members")
_MISSING_KINDS = frozenset({"missing_head", "missing_tail", "missing_members"})
# judge_window relations that accept a reclaim candidate back into the segment
# (spec 3.7.3 stream repair routing / §7.14 deductive mapping: non-boundary values).
_RECLAIM_RELATIONS = frozenset({"continues", "advances"})
# S7: a fail verdict with an empty defects array is normalized code-side to one
# default label_mismatch entry (repair routing is built on the defect table).
_DEFAULT_FAIL_DEFECT: Mapping = {
    "kind": "label_mismatch", "members": None, "position": None,
    "detail": "评审判 fail 但未指认缺陷，默认视同标签不符",
}
# k = 2 frames beyond each segment boundary feed the [边界余量] evidence section
# (spec 3.7.2 — the VAD hangover convention transplant, zero extra LLM calls).
_BOUNDARY_MARGIN_K = 2

# M7-owned stream counters (CONTRACTS §9.3, S31 → report.stream.verify).
_COUNTER_MEMBERSHIP_REPAIRS = "verify.membership_repairs"
_COUNTER_BOUNDARY_FLAGS = "verify.boundary_flags"
_COUNTER_DEFECTS_PREFIX = "verify.defects."


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


def verify_sequence_system_text(extra_criteria: str) -> str:
    """System message of the §10.5 v1.8 sequence variant: review dimensions, the
    five-kind defect-type explanation, then the structure sentence; the
    extra-criteria line is omitted entirely when empty (non-stream rule)."""
    lines = [_SEQ_SYSTEM_HEAD, _SEQ_SYSTEM_DIMS]
    if extra_criteria:
        lines.append(extra_criteria)
    lines.extend((_SEQ_SYSTEM_DEFECT_TYPES, _SEQ_SYSTEM_TAIL, _SEQ_SYSTEM_STRUCTURE))
    return "\n".join(lines)


def sequence_step_line(transition: Transition) -> str:
    """One [动作序列] line in the §10.1 frozen format
    `{index}. {action_type}（对象: {target|—}；值: {value|—}）{description}`; null
    target/value render as "—". Review evidence carries no （摘取兜底） suffix —
    that S16 marker belongs to M4's scoring sections only (M5 rule, mirrored)."""
    action = transition.action
    target = action.get("target")
    value = action.get("value")
    return (f"{transition.index}. {action.get('action_type')}"
            f"（对象: {'—' if target is None else target}；"
            f"值: {'—' if value is None else value}）"
            f"{action.get('description')}")


def normalize_defects(entries: Sequence[Mapping]) -> list[dict]:
    """S31 deterministic normalization of a (multi-judge union) defect list:
    sorted and de-duplicated by the key (kind enum order, position or "",
    members tuple or ()). ``sorted`` is stable, so among same-key entries the
    union order (judge config order, then entry order) picks the survivor —
    schedule-independent. Entries are shallow-copied (routing later annotates
    copies, never the judges' raw payloads)."""
    def key(entry: Mapping) -> tuple:
        return (DEFECT_KINDS.index(entry["kind"]),
                entry.get("position") or "",
                tuple(entry.get("members") or ()))

    seen: set[tuple] = set()
    out: list[dict] = []
    for entry in sorted(entries, key=key):
        k = key(entry)
        if k in seen:
            continue
        seen.add(k)
        out.append(dict(entry))
    return out


def _session_frame_envelopes(batch: Sequence[PipelineItem],
                             session_id: str | None) -> list[PipelineItem]:
    """The session's frame envelopes in batch position order (= session order,
    M10 whole-session packing; S4 — neighborhood queries are session_id filter +
    batch list position order)."""
    return [it for it in batch
            if it.record.kind == "single" and it.session_id == session_id]


def _session_episodes(batch: Sequence[PipelineItem],
                      session_id: str | None) -> list[PipelineItem]:
    """The session's episode envelopes in batch position order, unique by record
    id — multi fan-out clones share the record id and collapse onto the original
    (first in batch order), so segment ordinals stay stable under fan-out."""
    seen: set[str] = set()
    out: list[PipelineItem] = []
    for it in batch:
        if (it.record.kind == "sequence" and it.session_id == session_id
                and it.record.id not in seen):
            seen.add(it.record.id)
            out.append(it)
    return out


def boundary_margin_text(item: PipelineItem, batch: Sequence[PipelineItem],
                         digest_max_chars: int) -> str:
    """[边界余量] section body (spec 3.7.2): the k = 2 frames beyond each segment
    boundary (before the head member / after the tail member, taken from the same
    session_id in batch position order), each line carrying the frame_digest and
    the frame's fate — "noise" (dropped_noise) / "第 n 段" (member of the n-th
    same-session episode, batch order, 1-based) / "无" (an existing frame with
    neither fate). A position beyond the session's extent renders as a bare "无"
    line. Chronological line order: 段首前 2, 段首前 1, 段尾后 1, 段尾后 2.
    Pure code over batch state — zero LLM calls, zero rng."""
    frames = _session_frame_envelopes(batch, item.session_id)
    position_of: dict[str, int] = {}
    for i, frame in enumerate(frames):
        position_of.setdefault(frame.record.id, i)
    members = item.record.members
    head = position_of.get(members[0].id) if members else None
    tail = position_of.get(members[-1].id) if members else None

    membership: dict[str, int] = {}
    for ordinal, episode in enumerate(_session_episodes(batch, item.session_id), 1):
        for member in episode.record.members:
            membership.setdefault(member.id, ordinal)

    def fate(frame: PipelineItem) -> str:
        if frame.status == "dropped_noise":
            return "noise"
        ordinal = membership.get(frame.record.id)
        return f"第 {ordinal} 段" if ordinal is not None else "无"

    lines: list[str] = []
    for label, base, offsets in (("段首前", head, (-_BOUNDARY_MARGIN_K, -1)),
                                 ("段尾后", tail, (1, _BOUNDARY_MARGIN_K))):
        for offset in offsets:
            distance = abs(offset)
            pos = None if base is None else base + offset
            if pos is None or not 0 <= pos < len(frames):
                lines.append(f"{label} {distance}: 无")
            else:
                frame = frames[pos]
                digest = frame_digest(frame.record, digest_max_chars)
                lines.append(f"{label} {distance}: {digest}（去向: {fate(frame)}）")
    return "\n".join(lines)


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


def build_verify_prompt(record: Record, output: Mapping, cfg: "ResolvedConfig",
                        label: str | None = None,
                        transitions: tuple[Transition, ...] | None = None,
                        boundary_margin: str = "") -> "PromptBundle":
    """Assemble the §10.5 judge prompt for one (record, annotation-output) pair.
    UI modality carries screenshot + serialized tree parts as in §10.1/§10.2.
    v1.7 (R3): label non-None → the [任务指令] section and extra_criteria take the
    class-effective values (class_views[label].annotate.instruction /
    class_views[label].verify.extra_criteria); None = global config.
    v1.8 (S7, additive trailing kwargs — non-sequence callers byte-unchanged):
    ``record.kind == "sequence"`` selects the §10.5 sequence variant — one user
    message, six sections in order: [任务指令] → [动作序列] (omitted entirely when
    transitions is None) → [边界余量] (pre-rendered by the stage driver, which
    holds the batch context) → [首帧截图] + image → [末帧截图] + image → [标注结果];
    text-modality sequences degrade without the screenshot sections (the M5 S6
    precedent — frames carry no images)."""
    from labelkit.llm_client import Message, Part, PromptBundle

    if label is not None:
        view = cfg.class_views[label]
        instruction = view.annotate.instruction
        extra_criteria = view.verify.extra_criteria
    else:
        instruction = cfg.annotate.instruction
        extra_criteria = cfg.verify.extra_criteria

    if record.kind == "sequence":  # v1.8 sequence variant (checked BEFORE modality)
        seq_system = Message(
            role="system",
            parts=(Part(kind="text", text=verify_sequence_system_text(extra_criteria)),),
        )
        parts: list[Part] = [Part(kind="text", text=f"[任务指令] {instruction}")]
        if transitions is not None:  # section omitted entirely when None
            steps = "\n".join(sequence_step_line(t) for t in transitions)
            parts.append(Part(kind="text", text=f"{_LABEL_ACTION_SEQUENCE}\n{steps}"))
        parts.append(Part(kind="text",
                          text=f"{_LABEL_BOUNDARY_MARGIN}\n{boundary_margin}"))
        if record.modality == "ui":
            parts.append(Part(kind="text", text=_LABEL_FIRST_FRAME))
            parts.append(Part(kind="image", image=record.members[0].image))
            parts.append(Part(kind="text", text=_LABEL_LAST_FRAME))
            parts.append(Part(kind="image", image=record.members[-1].image))
        parts.append(Part(kind="text",
                          text=f"[标注结果] {json.dumps(output, ensure_ascii=False)}"))
        return PromptBundle(messages=(seq_system,
                                      Message(role="user", parts=tuple(parts))))

    system = Message(
        role="system",
        parts=(Part(kind="text", text=verify_system_text(extra_criteria)),),
    )
    if record.modality == "text":
        user = Message(
            role="user",
            parts=(
                Part(
                    kind="text",
                    text=verify_user_text(instruction, record.text or "", output),
                ),
            ),
        )
    else:
        head = f"[任务指令] {instruction}\n[原始数据]\n[屏幕截图]"
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


class _EpisodeReview:
    """Per-episode bookkeeping for the stream driver (one instance per sequence
    envelope, alive across driver rounds). Round-scoped surgery fields are reset
    by ``begin_round``."""

    __slots__ = ("item", "label", "rounds", "critiques", "verdict", "fail_critiques",
                 "defects", "orig_members", "working_members", "session_positions",
                 "needs_reannotate", "surgical", "claims", "reseams")

    def __init__(self, item: PipelineItem):
        self.item = item
        self.label = item.classification.label if item.classification else None
        self.rounds = 0
        self.critiques: list[dict] = []
        self.verdict: str = ""
        self.fail_critiques: list[dict] = []
        self.defects: list[dict] = []
        self.begin_round()

    def begin_round(self) -> None:
        self.orig_members: tuple[Record, ...] = self.item.record.members
        self.working_members: list[Record] = list(self.item.record.members)
        self.session_positions: dict[str, int] = {}
        self.needs_reannotate = False
        self.surgical = False
        self.claims: list["_ReclaimClaim"] = []
        self.reseams: dict[int, Transition] = {}


class _ReclaimClaim:
    """One reserved reclaim candidate: the noise envelope, its session position,
    and the [prev member, candidate, next member] re-judgment window."""

    __slots__ = ("envelope", "position", "window", "candidate_index")

    def __init__(self, envelope: PipelineItem, position: int,
                 window: list[Record], candidate_index: int):
        self.envelope = envelope
        self.position = position
        self.window = window
        self.candidate_index = candidate_index


_BIG_THREE = (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError)


class VerifyStage:
    name = "verify"

    def __init__(self, cfg: "ResolvedConfig"):
        self.cfg = cfg

    async def run(self, batch: list[PipelineItem], ctx: "RunContext") -> list[PipelineItem]:
        eligible = [it for it in batch if it.status == "active" and it.annotation is not None]
        if not eligible:
            return batch
        # v1.8: sequence envelopes under stream mode bypass into the driver; any
        # single-record stragglers in the same batch keep the classic per-item
        # path. Without sequences (or with segment off) the pre-v1.8 line below
        # runs byte-identically (regression anchor).
        episodes = [it for it in eligible if it.record.kind == "sequence"]
        if episodes and self.cfg.segment.enabled:
            singles = [it for it in eligible if it.record.kind != "sequence"]
            if singles:
                await asyncio.gather(*(self._verify_item(item, ctx) for item in singles))
            await self._run_stream_driver(batch, episodes, ctx)
            return batch
        await asyncio.gather(*(self._verify_item(item, ctx) for item in eligible))
        return batch

    # ── per-item driver ────────────────────────────────────────────────────

    async def _verify_item(self, item: PipelineItem, ctx: "RunContext") -> None:
        vcfg = self.cfg.verify
        label = item.classification.label if item.classification else None  # v1.7 R3
        try:
            verdict, rounds, critiques, annotation = await run_verify_loop(
                item.annotation,
                judge_round=lambda ann, rnd: self._judge_round(
                    item.record, ann, rnd, ctx, label=label),
                reannotate=lambda ann, fc: self._reannotate(
                    item.record, ann, fc, ctx, label=label),
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
        self, record: Record, annotation: Annotation, round_no: int, ctx: "RunContext",
        label: str | None = None,
    ) -> tuple[str, list[dict], list[dict]]:
        from labelkit.schema_engine import VERDICT_SCHEMA

        vcfg = self.cfg.verify
        judges = list(vcfg.judges) or [vcfg.llm]
        multi = bool(vcfg.judges)
        prompt = build_verify_prompt(record, annotation.output, ctx.cfg, label=label)
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
                                     judge if multi else None, ctx, label=label)
        return majority_verdict(verdicts), merged, fail_critiques

    def _emit_verdict_event(
        self,
        record: Record,
        verdict: str,
        round_no: int,
        critiques: Sequence[Mapping],
        judge: str | None,
        ctx: "RunContext",
        label: str | None = None,
        defects: Sequence[Mapping] | None = None,
    ) -> None:
        content = ctx.cfg.trace.content
        payload: dict = {"verdict": verdict, "round": round_no}
        if content != "none":  # §8.3: tier "none" carries no LLM-produced free text
            payload["critiques"] = [
                {"aspect": c["aspect"], "opinion": c["opinion"]} for c in critiques
            ]
        if judge is not None:
            payload["judge"] = judge
        if ctx.cfg.classify.enabled and label is not None:  # v1.7 R5
            payload["label"] = label
        if defects is not None:
            # v1.8 stream sequence review only: the judge's defect table rides the
            # payload as-is — tier redaction is M12's job ("defects" ∈ obslog's
            # free-text key set, S27/S31), not the stage's.
            payload["defects"] = [dict(d) for d in defects]
        if ctx.cfg.trace.enabled and content in ("excerpt", "full"):
            # §7.4: the four trace.content tiers are cumulative ("逐档递增") — "full"
            # includes everything from "excerpt", so the excerpt is attached at both
            # tiers (CONTRACTS.md §8.3). Gated on trace.enabled so ui_tree.serialize()
            # is never computed when tracing is off.
            if record.kind == "sequence":
                # Sequence records carry text/ui_tree = None; the excerpt is the
                # first member's frame_digest head (the M4 §7.3 sequence rule).
                src = (frame_digest(record.members[0], _MEMBER_DIGEST_MAX_CHARS)
                       if record.members else "")
            elif record.modality == "text":
                src = record.text
            else:
                src = record.ui_tree.serialize()
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
        self, record: Record, annotation: Annotation, fail_critiques: list[dict],
        ctx: "RunContext", label: str | None = None,
    ) -> Annotation:
        from labelkit.annotate import RepairContext, annotate_record

        repair = RepairContext(
            previous_output=annotation.output,
            critiques_text=render_critiques_text(fail_critiques),
        )
        return await annotate_record(record, ctx, repair, label=label)

    # ── v1.8 stream driver: sequence review + two-phase batch repair (S7/S8) ──
    #
    # Determinism contract (S8): zero rng; LLM calls happen ONLY inside the four
    # gathers — (a) review, (c) reclaim re-judgment, (d) seam re-extraction,
    # (f) re-annotation; every state write happens in the synchronous passes
    # between them, in batch position order (the classify fan-out precedent).

    async def _run_stream_driver(self, batch: list[PipelineItem],
                                 episodes: list[PipelineItem],
                                 ctx: "RunContext") -> None:
        vcfg = self.cfg.verify
        pending = [_EpisodeReview(item) for item in episodes]  # batch position order
        while pending:
            # ── (a) concurrent review of ALL pending episodes ──────────────
            results = await asyncio.gather(
                *(self._review_episode(state, batch, ctx) for state in pending),
                return_exceptions=True)
            reviewed: list[_EpisodeReview] = []
            for state, result in zip(pending, results):
                if isinstance(result, BaseException):
                    if isinstance(result, _BIG_THREE):
                        raise result
                    self._fail_item(state.item, result, ctx)
                    continue
                verdict, merged, fail_critiques, defects = result
                state.rounds += 1
                state.critiques.extend(merged)
                state.verdict = verdict
                state.fail_critiques = fail_critiques
                state.defects = defects
                # Count per adjudicated defect AT REVIEW TIME (D4): the report
                # histogram surfaces every defect the judges called — incl.
                # ones a later repair round fixes away — matching the
                # routing-time semantics of membership_repairs/boundary_flags.
                for defect in defects:
                    ctx.metrics.count(
                        f"{_COUNTER_DEFECTS_PREFIX}{defect['kind']}")
                reviewed.append(state)

            # ── (b) synchronous routing + member surgery, batch position order
            # ("first-come" becomes deterministic "position-come", S8).
            finalize: list[_EpisodeReview] = []
            routed: list[_EpisodeReview] = []
            claimed: set[int] = set()      # id() of noise envelopes reserved this round
            for state in reviewed:
                state.begin_round()
                if state.verdict == "pass":
                    finalize.append(state)
                    continue
                repairs_done = state.rounds - 1
                if vcfg.policy != "repair" or repairs_done >= vcfg.max_repair_rounds:
                    finalize.append(state)  # budget/policy: fail stands (现行语义)
                    continue
                self._route_defects(state, batch, claimed, ctx)
                routed.append(state)

            # ── (c) concurrent reclaim re-judgment via segment.judge_window ──
            claims = [(state, claim) for state in routed for claim in state.claims]
            if claims:
                outcomes = await asyncio.gather(
                    *(self._rejudge_claim(claim, ctx) for _, claim in claims),
                    return_exceptions=True)
                # Synchronous application in claim order (== batch position order).
                for (state, claim), outcome in zip(claims, outcomes):
                    if isinstance(outcome, BaseException):
                        if isinstance(outcome, _BIG_THREE):
                            raise outcome
                        # Re-judgment failure degrades to mark-only (record-level
                        # isolation: a broken window call never fails the episode).
                        ctx.metrics.count(_COUNTER_BOUNDARY_FLAGS)
                        continue
                    if outcome in _RECLAIM_RELATIONS:
                        self._apply_reclaim(state, claim, ctx)
                    else:
                        ctx.metrics.count(_COUNTER_BOUNDARY_FLAGS)

            repairing: list[_EpisodeReview] = []
            for state in routed:
                if state.surgical or state.needs_reannotate:
                    repairing.append(state)
                else:
                    finalize.append(state)  # nothing repairable — fail stands

            # ── (d) concurrent seam re-extraction (extract.enabled only) ────
            dead = await self._reseam_episodes(repairing, ctx)

            # ── (e) synchronous rebuild: record members + transitions ───────
            for state in repairing:
                if id(state) in dead or not state.surgical:
                    continue
                self._rebuild_episode(state)

            # ── (f) concurrent re-annotation of surgical/label_mismatch ─────
            jobs = [state for state in repairing if id(state) not in dead]
            next_pending: list[_EpisodeReview] = []
            if jobs:
                outcomes = await asyncio.gather(
                    *(self._reannotate_episode(state, ctx) for state in jobs),
                    return_exceptions=True)
                for state, outcome in zip(jobs, outcomes):
                    if isinstance(outcome, BaseException):
                        if isinstance(outcome, _BIG_THREE):
                            raise outcome
                        self._fail_item(state.item, outcome, ctx)
                        continue
                    state.item.annotation = outcome
                    next_pending.append(state)

            for state in finalize:
                self._finalize_episode(state, ctx)
            pending = next_pending                 # ── (g) next-round re-review

    async def _review_episode(self, state: _EpisodeReview,
                              batch: list[PipelineItem], ctx: "RunContext"):
        margin = boundary_margin_text(state.item, batch,
                                      self.cfg.segment.digest_max_chars)
        return await self._judge_round_sequence(
            state.item, state.item.annotation, state.rounds + 1, ctx,
            label=state.label, boundary_margin=margin)

    async def _judge_round_sequence(
        self, item: PipelineItem, annotation: Annotation, round_no: int,
        ctx: "RunContext", label: str | None = None, boundary_margin: str = "",
    ) -> tuple[str, list[dict], list[dict], list[dict]]:
        """One sequence review round: the _judge_round skeleton with the schema
        swapped to defect_verdict_schema(), critiques collected unchanged, and
        defects newly collected — the union over judges that voted fail,
        deterministically normalized (S31); final fail with an empty table gets
        the default label_mismatch entry (S7)."""
        from labelkit.schema_engine import defect_verdict_schema

        vcfg = self.cfg.verify
        judges = list(vcfg.judges) or [vcfg.llm]
        multi = bool(vcfg.judges)
        record = item.record
        prompt = build_verify_prompt(record, annotation.output, ctx.cfg, label=label,
                                     transitions=item.transitions,
                                     boundary_margin=boundary_margin)
        schema = defect_verdict_schema()
        results = await asyncio.gather(
            *(
                ctx.schema_engine.complete_validated(
                    judge,
                    prompt,
                    schema=schema,
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
        defects_union: list[Mapping] = []
        for judge, result in zip(judges, results):
            if isinstance(result, BaseException):
                if isinstance(result, _BIG_THREE):
                    raise result
                if not multi:
                    raise result  # single judge: _run_stream_driver classifies it
                verdicts.append("fail")
                entry = {"aspect": "judge_error", "opinion": str(result),
                         "judge": judge}
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
                defects_union.extend(obj["defects"])
            self._emit_verdict_event(record, verdict, round_no, obj["critiques"],
                                     judge if multi else None, ctx, label=label,
                                     defects=obj["defects"])
        final = majority_verdict(verdicts)
        defects = normalize_defects(defects_union)
        if final == "fail" and not defects:
            defects = [dict(_DEFAULT_FAIL_DEFECT)]
        return final, merged, fail_critiques, defects

    # ── (b) defect routing (synchronous; shrink executes here, reclaims are
    #        reserved as claims for the (c) gather) ───────────────────────────

    def _route_defects(self, state: _EpisodeReview, batch: list[PipelineItem],
                       claimed: set[int], ctx: "RunContext") -> None:
        item = state.item
        frames = _session_frame_envelopes(batch, item.session_id)
        positions: dict[str, int] = {}
        for i, frame in enumerate(frames):
            positions.setdefault(frame.record.id, i)
        state.session_positions = positions
        # S8: multi fan-out clone siblings (classification carries a label other
        # than the hit set's first) may never execute membership surgery — the
        # shared member frames belong to the original envelope.
        classification = item.classification
        clone = bool(classification is not None and classification.labels
                     and classification.label != classification.labels[0])
        split = bool(getattr(item, "session_split", False))

        for idx, defect in enumerate(state.defects):
            kind = defect["kind"]
            if kind == "label_mismatch":
                state.needs_reannotate = True
                continue
            if clone:
                # Mark-only downgrade; the missing_* kinds count as mark-only
                # boundary determinations, off_task shrink downgrades carry no
                # counter (boundary_flags counts boundary kinds only).
                if kind in _MISSING_KINDS:
                    ctx.metrics.count(_COUNTER_BOUNDARY_FLAGS)
                continue
            if kind == "off_task_members":
                named = set(defect.get("members") or ())
                shrink_ids = {m.id for m in state.working_members if m.id in named}
                if not shrink_ids or len(shrink_ids) == len(state.working_members):
                    # Nothing identifiable to shrink, or the judge named EVERY
                    # member — an empty episode cannot exist; the fail verdict
                    # stands and drops the episode whole instead.
                    continue
                state.working_members = [m for m in state.working_members
                                         if m.id not in shrink_ids]
                for frame in frames:
                    if frame.status == "absorbed" and frame.record.id in shrink_ids:
                        frame.status = "dropped_noise"     # ②b M7 exemption
                        frame.noise_attribution = ("verify", "off_task_member")  # type: ignore[attr-defined]
                state.surgical = True
                ctx.metrics.count(_COUNTER_MEMBERSHIP_REPAIRS)
                continue
            # missing_head / missing_tail / missing_members — three-level
            # reclaim determination (noise pool → neighbor episode → nowhere).
            if split:
                # Session was hard-split at batch_size (S21): the missing frame
                # may live in another batch — reclaim degrades to mark-only.
                state.defects[idx] = {**defect, "suspected": "session_split"}
                ctx.metrics.count(_COUNTER_BOUNDARY_FLAGS)
                continue
            found = self._find_reclaim_candidate(kind, defect, state, frames,
                                                 claimed)
            if isinstance(found, _ReclaimClaim):
                claimed.add(id(found.envelope))
                state.claims.append(found)
            elif found == "neighbor":
                # The adjacent frame is absorbed by another episode: mark only,
                # never cross-episode theft (S8).
                ctx.metrics.count(_COUNTER_BOUNDARY_FLAGS)
            else:
                state.defects[idx] = {**defect, "suspected": "capture_gap"}
                ctx.metrics.count(_COUNTER_BOUNDARY_FLAGS)

    def _find_reclaim_candidate(
        self, kind: str, defect: Mapping, state: _EpisodeReview,
        frames: list[PipelineItem], claimed: set[int],
    ) -> "_ReclaimClaim | str | None":
        """Deterministic candidate lookup in the defect's neighborhood (batch
        position order): head = the frame immediately before the head member,
        tail = immediately after the tail member (contiguity — reclaiming past a
        non-member frame would punch a hole), members = the first interior noise
        frame between head and tail (restricted to defect.members ids when the
        judge named some). Returns a claim, "neighbor" (frame held by another
        episode), or None (no candidate)."""
        positions = state.session_positions
        member_positions = sorted(positions[m.id] for m in state.working_members
                                  if m.id in positions)
        if not member_positions:
            return None
        head, tail = member_positions[0], member_positions[-1]

        def qualifies(frame: PipelineItem) -> bool:
            if frame.status != "dropped_noise" or id(frame) in claimed:
                return False
            attribution = getattr(frame, "noise_attribution", None)
            # Frames the verify stage itself dropped (off_task shrink) never
            # re-enter — the shrink↔reclaim ping-pong guard.
            return not (attribution and attribution[0] == "verify")

        def edge(adjacent_pos: int) -> "_ReclaimClaim | str | None":
            if not 0 <= adjacent_pos < len(frames):
                return None
            frame = frames[adjacent_pos]
            if qualifies(frame):
                return self._make_claim(state, frame, adjacent_pos)
            if frame.status == "absorbed" or id(frame) in claimed:
                # Held by another episode — either already absorbed or claimed
                # earlier in THIS synchronous pass (position-order priority,
                # S8): level-2 "neighbor", never a capture gap (D5).
                return "neighbor"
            return None

        if kind == "missing_head":
            return edge(head - 1)
        if kind == "missing_tail":
            return edge(tail + 1)
        named = set(defect.get("members") or ())
        contended = False
        for pos in range(head + 1, tail):
            frame = frames[pos]
            if not qualifies(frame):
                if id(frame) in claimed:
                    contended = True       # claimed this pass by an earlier episode
                continue
            if named and frame.record.id not in named:
                continue
            return self._make_claim(state, frame, pos)
        return "neighbor" if contended else None

    @staticmethod
    def _make_claim(state: _EpisodeReview, frame: PipelineItem,
                    position: int) -> _ReclaimClaim:
        """Window = [prev member, candidate, next member] Records around the
        candidate's session position (edge claims have no prev/next member)."""
        positions = state.session_positions
        prev_member = next_member = None
        for member in state.working_members:
            member_pos = positions.get(member.id)
            if member_pos is None:
                continue
            if member_pos < position:
                prev_member = member                 # last one below wins
            elif member_pos > position and next_member is None:
                next_member = member
        window: list[Record] = []
        if prev_member is not None:
            window.append(prev_member)
        candidate_index = len(window)
        window.append(frame.record)
        if next_member is not None:
            window.append(next_member)
        return _ReclaimClaim(frame, position, window, candidate_index)

    async def _rejudge_claim(self, claim: _ReclaimClaim, ctx: "RunContext") -> str:
        """Direct call into M14's public re-judgment surface (CONTRACTS §7.14 —
        the sanctioned import exception); returns the CANDIDATE frame's relation."""
        from labelkit.segment import judge_window

        relations = await judge_window(claim.window, ctx)
        return relations[claim.candidate_index]

    def _apply_reclaim(self, state: _EpisodeReview, claim: _ReclaimClaim,
                       ctx: "RunContext") -> None:
        """Accepted reclaim: flip the noise envelope back to absorbed (②b M7
        exemption — never back to active) and insert the record into the working
        member list at its batch-position rank."""
        claim.envelope.status = "absorbed"
        positions = state.session_positions
        insert_at = 0
        for i, member in enumerate(state.working_members):
            if positions.get(member.id, -1) < claim.position:
                insert_at = i + 1
        state.working_members.insert(insert_at, claim.envelope.record)
        state.surgical = True
        ctx.metrics.count(_COUNTER_MEMBERSHIP_REPAIRS)

    # ── (d)/(e) seam re-extraction + rebuild ────────────────────────────────

    def _affected_pairs(self, state: _EpisodeReview) -> list[tuple[int, Record, Record]]:
        """Rebuilt adjacent pairs that did not exist in the pre-surgery member
        list — the surgery touchpoints needing seam re-extraction (1–2 per
        surgery). Index = the pair's REBUILT ordinal."""
        old_adjacent = {(a.id, b.id)
                        for a, b in zip(state.orig_members, state.orig_members[1:])}
        return [(j, a, b)
                for j, (a, b) in enumerate(zip(state.working_members,
                                               state.working_members[1:]))
                if (a.id, b.id) not in old_adjacent]

    async def _reseam_episodes(self, repairing: list[_EpisodeReview],
                               ctx: "RunContext") -> set[int]:
        """Concurrent seam re-extraction via M15's public direct-call surface
        (CONTRACTS §7.15; extract.enabled only). Returns the states (by id) that
        died on a re-extraction error — record-level isolation."""
        jobs: list[tuple[_EpisodeReview, int, Record, Record]] = []
        for state in repairing:
            if not state.surgical:
                continue
            if not (self.cfg.extract.enabled and state.item.transitions is not None):
                continue
            for j, a, b in self._affected_pairs(state):
                jobs.append((state, j, a, b))
        dead: set[int] = set()
        if not jobs:
            return dead
        from labelkit.extract import extract_transition

        outcomes = await asyncio.gather(
            *(extract_transition(a, b, j, ctx, label=state.label)
              for state, j, a, b in jobs),
            return_exceptions=True)
        for (state, j, _a, _b), outcome in zip(jobs, outcomes):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, _BIG_THREE):
                    raise outcome
                if id(state) not in dead:
                    dead.add(id(state))
                    self._fail_item(state.item, outcome, ctx)
                continue
            state.reseams[j] = outcome
        return dead

    def _rebuild_episode(self, state: _EpisodeReview) -> None:
        """Synchronous rebuild after all shrinks/reclaims: the record is rebuilt
        with the new member tuple (the sequence id is NEVER recomputed, spec
        3.14.4); transitions are fully renumbered — untouched steps keep their
        Transition with the index rewritten to the rebuilt ordinal, surgery
        touchpoints take the fresh extraction with {"reseamed": True} merged into
        detail. Invariant: len(transitions) == len(members) − 1."""
        item = state.item
        new_members = tuple(state.working_members)
        item.record = dataclasses.replace(item.record, members=new_members)
        if item.transitions is not None:
            old_by_pair = {
                (a.id, b.id): t
                for (a, b), t in zip(zip(state.orig_members, state.orig_members[1:]),
                                     item.transitions)
            }
            rebuilt: list[Transition] = []
            for j, (a, b) in enumerate(zip(new_members, new_members[1:])):
                if j in state.reseams:
                    fresh = state.reseams[j]
                    rebuilt.append(dataclasses.replace(
                        fresh, index=j,
                        detail={**dict(fresh.detail), "reseamed": True}))
                else:
                    rebuilt.append(dataclasses.replace(old_by_pair[(a.id, b.id)],
                                                       index=j))
            item.transitions = tuple(rebuilt)
        item.stream_repaired = True  # type: ignore[attr-defined]  # → _meta.stream.repaired

    # ── (f) re-annotation + finalization ────────────────────────────────────

    async def _reannotate_episode(self, state: _EpisodeReview,
                                  ctx: "RunContext") -> Annotation:
        from labelkit.annotate import RepairContext, annotate_record

        repair = RepairContext(
            previous_output=state.item.annotation.output,
            critiques_text=render_critiques_text(state.fail_critiques),
        )
        return await annotate_record(state.item.record, ctx, repair,
                                     label=state.label,
                                     transitions=state.item.transitions)

    def _finalize_episode(self, state: _EpisodeReview, ctx: "RunContext") -> None:
        # verify.defects.<kind> is counted at review time (D4), not here —
        # a repaired-away defect must still reach the report histogram.
        item = state.item
        item.verification = VerificationResult(
            verdict=state.verdict, rounds=state.rounds,
            critiques=tuple(state.critiques), defects=tuple(state.defects))
        if state.verdict == "fail":
            item.status = "dropped_verify"

    def _fail_item(self, item: PipelineItem, exc: BaseException,
                   ctx: "RunContext") -> None:
        """Stream-driver counterpart of _verify_item's except block (stage
        contract ④: single-record failures never escape to batch level)."""
        kind, retryable = _classify_error(exc, item.record.modality)
        if isinstance(exc, SchemaViolation):
            item.raw_last_output = exc.raw_last_output  # type: ignore[attr-defined]
        item.errors.append(StageError(stage=self.name, kind=kind,
                                      message=str(exc), retryable=retryable))
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

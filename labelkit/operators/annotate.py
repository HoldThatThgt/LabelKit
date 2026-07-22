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
(after v1.7's label); None keeps every pre-v1.8 call site byte-identical. v1.9 (T14)
appends the third: fragment_lens — per-fragment keyframe quotas for stitched threads
(every fragment keeps ≥ 1 keyframe); None keeps the v1.8 uniform downsample.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
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
    Annotation,
    PipelineItem,
    Record,
    StageError,
    Transition,
    Usage,
    frame_digest,
)

from labelkit.common.runtime import budget
from labelkit.common.runtime.llm_client import Message, Part, PromptBundle

if TYPE_CHECKING:
    from labelkit.common.config.model import LLMProfile, ResolvedConfig
    from labelkit.common.contracts.stage import RunContext


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


def _keyframe_indexes(n: int, k: int,
                      fragment_lens: Sequence[int] | None = None) -> list[int]:
    """S28 deterministic downsample over n members with cap k
    (annotate.sequence_frames): n <= k keeps every member; otherwise
    idx_i = i*(n-1)//(k-1) for i = 0..k-1 — pure integer arithmetic, zero rng, first and
    last always kept, strictly increasing (no duplicates for n > k).

    v1.9 (T14, per-fragment quota): fragment_lens — the thread's per-fragment
    member counts in member-tuple order (fragments are contiguous session-order
    blocks) — upgrades the downsample so EVERY fragment keeps ≥ 1 keyframe
    (uniform sampling would drain small fragments whole, minor-8). Quota:
    each of the m fragments gets 1 + a largest-remainder share of the k − m
    surplus weighted by (Lᵢ − 1) (ties → lower fragment index); inside a
    fragment the S28 uniform formula runs locally (quota 1 keeps the fragment's
    FIRST member — last fragment keeps its LAST, so the global first/last
    invariant holds). Degrades to the v1.8 uniform path when fragment_lens is
    absent/single/inconsistent or k < m (the ≥ 1 guarantee is infeasible)."""
    if n <= k:
        return list(range(n))
    if (not fragment_lens or len(fragment_lens) <= 1
            or sum(fragment_lens) != n or len(fragment_lens) > k):
        return [i * (n - 1) // (k - 1) for i in range(k)]
    m = len(fragment_lens)
    extra_total = k - m
    weight_total = n - m                       # Σ (Lᵢ − 1) ≥ 1 since n > k ≥ m
    base = [(length - 1) * extra_total // weight_total for length in fragment_lens]
    remainders = [(length - 1) * extra_total % weight_total
                  for length in fragment_lens]
    leftover = extra_total - sum(base)
    granted = set(sorted(range(m), key=lambda i: (-remainders[i], i))[:leftover])
    out: list[int] = []
    start = 0
    for i, length in enumerate(fragment_lens):
        quota = 1 + base[i] + (1 if i in granted else 0)
        if quota == 1:
            picks = [length - 1] if i == m - 1 else [0]
        else:
            picks = [j * (length - 1) // (quota - 1) for j in range(quota)]
        out.extend(start + p for p in picks)
        start += length
    return out


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


# ── v1.11 context-budget packing (spec 3.5.2 上下文预算装填与修复升级换档) ────

_TREE_MARKER_RE = re.compile(r"^…\(truncated (\d+) nodes\)$")


@dataclass
class _PackState:
    """Per-build trim directives for _assemble_prompt (spec 3.5.2 v1.11 段,
    share order ④): token budgets for the trimmable text blocks — None = that
    block is not trimmed on this build. The V25③ untrimmable blocks (the repair
    [上一版标注]/[审核意见] suffix) and the ① static system side (instruction /
    user schema / few-shot) never appear here — they are COUNTED by the packer,
    never cut."""
    step_budget: int | None = None     # [动作序列] body (edges trim, §3.3⑤)
    digest_budget: int | None = None   # [成员帧摘要] body (edges trim — same family)
    tree_budget: int | None = None     # single-record UI tree render (§3.3③)
    truncations: int = 0


def _feed_reactive_terminal(exc: BaseException, metrics) -> None:
    """A7/§7.8 breaker matrix: ONLY the reactive-400 (body-sniff) overflow
    terminal feeds the fatal streak — exactly once per exception object (the
    duck flag guards double-feeds when one exception crosses operators, e.g.
    the M7→M5 repair chain); precheck and the 200-shaped finish oracle never
    feed. ``origin`` is read defensively pending the errors.py revision
    (default "http_400")."""
    if (isinstance(exc, ContextOverflowError) and exc.phase == "reactive"
            and getattr(exc, "origin", "http_400") == "http_400"
            and not getattr(exc, "_breaker_fed", False)):
        exc._breaker_fed = True  # type: ignore[attr-defined]
        metrics.record_provider_result(fatal=True)


def _fit_tree_text(rendered: str, budget_tokens: int) -> tuple[str, bool]:
    """§3.3③ dynamic cap on a serialized UI tree: the render (already under the
    absolute input.ui_tree_max_chars cap) is re-checked with est_text; over the
    share, trailing NODE lines are dropped and the serialize-family marker
    "…(truncated N nodes)" closes the text — N accumulates onto an existing
    marker's count. est_text is prefix-monotone ⇒ bisection. Returns
    (text, trimmed)."""
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


def _fit_block(body: str, share: int | None) -> tuple[str, int]:
    """One §3.3⑤ edges trim: first/last lines kept, whole middle lines out,
    in-place "…(truncated N lines)" marker. Returns (body', trims)."""
    if share is None or budget.est_text(body) <= share:
        return body, 0
    return budget.fit_text(body, max(0, share), keep="edges"), 1


def build_annotate_prompt(record: Record, cfg: "ResolvedConfig", schema_text: str,
                          repair: RepairContext | None = None,
                          temperature: float | None = None,
                          label: str | None = None,
                          transitions: tuple[Transition, ...] | None = None,
                          fragment_lens: tuple[int, ...] | None = None,
                          k_eff: int | None = None,
                          image_px: int | None = None) -> PromptBundle:
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
    v1.9 (T14, THIRD additive trailing kwarg — the S5 form): fragment_lens non-None →
    the ② keyframe downsample runs per-fragment quotas (every fragment keeps ≥ 1
    keyframe); None = the v1.8 uniform downsample byte-identical.
    v1.11 (V21 ladder / F3, FOURTH additive trailing-kwarg revision — CONTRACTS §7.4):
    k_eff non-None → EFFECTIVE KEYFRAME CAP — the ② downsample runs with
    k = min(annotate.sequence_frames, k_eff) (carrier of the V20 frame-halving retry
    and the V21 repair-ladder k → max(2, ⌈k/2⌉); per-fragment quotas degrade per the
    existing T14 rule when the quota becomes infeasible); image_px non-None →
    ESCALATED RESOLUTION, carried into PromptBundle.image_px (V23① — the M9 builder
    computes effective px = image_px or profile.default_image_px or
    profile.max_image_px, clamped to min(·, max_image_px)); None/None = pre-v1.11
    behavior byte-identical. The budget packing itself enters through the private
    assembler's trailing ``fit`` parameter (annotate_record), never here.
    """
    return _assemble_prompt(record, cfg, schema_text, repair, temperature, label,
                            transitions, fragment_lens, k_eff, image_px)


def _assemble_prompt(record: Record, cfg: "ResolvedConfig", schema_text: str,
                     repair: RepairContext | None, temperature: float | None,
                     label: str | None,
                     transitions: tuple[Transition, ...] | None,
                     fragment_lens: tuple[int, ...] | None,
                     k_eff: int | None, image_px: int | None,
                     fit: _PackState | None = None) -> PromptBundle:
    """The §10.1 assembly body; ``fit`` non-None applies the spec 3.5.2 v1.11 ④
    text-block trims (steps/digests edges, single-record tree dynamic cap) —
    directives computed by _pack_prompt, which owns the share ordering. fit=None
    is the byte-identical pre-pack path (budget off, or a build that fits)."""
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

    k_cap = cfg.annotate.sequence_frames
    if k_eff is not None:
        # External cap, min-ed with the config value (§7.4); floored at the V10
        # minimal unit of 2 — every sanctioned carrier (V20 halving, V21 ladder,
        # §3.3⑥③ packing) floors there already, and k=1 has no downsample form.
        k_cap = min(k_cap, max(2, k_eff))

    if record.kind == "sequence":  # v1.8 sequence variant (checked BEFORE modality)
        seq_parts: list[Part] = []
        if transitions is not None:  # ① omitted entirely when transitions is None
            steps = "\n".join(_step_line(t) for t in transitions)
            if fit is not None:
                steps, trims = _fit_block(steps, fit.step_budget)
                fit.truncations += trims
            seq_parts.append(Part(kind="text", text=f"{_LABEL_ACTION_SEQUENCE}\n{steps}"))
        if record.modality == "ui":  # ② text sequences degrade to ① + ③
            kept = _keyframe_indexes(len(record.members), k_cap, fragment_lens)
            k = len(kept)
            for i, m_idx in enumerate(kept, start=1):
                member = record.members[m_idx]
                seq_parts.append(Part(kind="text",
                                      text=f"[关键帧 {i}/{k}·成员 {m_idx + 1}]"))
                seq_parts.append(Part(kind="image", image=member.image))
        digests = "\n".join(
            _member_digest_lines(record.members, cfg.input.ui_tree_max_chars))
        if fit is not None:
            digests, trims = _fit_block(digests, fit.digest_budget)
            fit.truncations += trims
        seq_parts.append(Part(kind="text", text=f"{_LABEL_MEMBER_DIGESTS}\n{digests}"))
        parts: tuple[Part, ...] = tuple(seq_parts)
    elif record.modality == "text":
        parts = (
            Part(kind="text", text=f"{_LABEL_TEXT_RECORD} {record.text}"),
        )
    else:  # UI modality: three parts in one user message
        tree_text = record.ui_tree.serialize(max_chars=cfg.input.ui_tree_max_chars)
        if fit is not None and fit.tree_budget is not None:
            tree_text, trimmed = _fit_tree_text(tree_text, max(0, fit.tree_budget))
            if trimmed:
                fit.truncations += 1
        parts = (
            Part(kind="text", text=_LABEL_SCREENSHOT),
            Part(kind="image", image=record.image),
            Part(kind="text", text=f"{_LABEL_UI_TREE}\n{tree_text}"),
        )

    if repair is not None:
        # V25③: the repair suffix is a per-record semantic asset — COUNTED into
        # the est by the packer, NEVER trimmed (appended after all trims).
        suffix = (f"{_LABEL_PREV_OUTPUT} {_dumps(repair.previous_output)}\n"
                  f"{_LABEL_CRITIQUES} {repair.critiques_text}\n"
                  f"{_REPAIR_TAIL}")
        last = parts[-1]
        parts = parts[:-1] + (Part(kind="text", text=f"{last.text}\n{suffix}"),)

    messages.append(Message(role="user", parts=parts))
    return PromptBundle(messages=tuple(messages), temperature=temperature,
                        image_px=image_px)


# ── v1.11 packing driver (spec 3.5.2 v1.11 段, deterministic share order) ────

def _image_unit_cost(prof: "LLMProfile", ctx: "RunContext",
                     image_px: int | None) -> int:
    """Per-image est for the packing: the batch-frozen calibrated readout at the
    working point; a V21-escalated px additionally floors at the provider prior
    @ that px × PRIOR_INFLATION (the calibrator knows only the working point —
    the max keeps the escalated est honest and errs conservative)."""
    cost = ctx.llm.calibrator.cost(prof.name)
    if image_px is not None:
        cost = max(cost, math.ceil(budget.est_image_prior(prof, image_px)
                                   * budget.PRIOR_INFLATION))
    return cost


def _prompt_text_est(bundle: PromptBundle, schema_est: int) -> tuple[int, int]:
    """(text-side est, image count) of an assembled bundle — the est_prompt
    formula at image_cost=0 (identical accounting to the M9 throat)."""
    return (budget.est_prompt(bundle, None, None, image_cost=0) + schema_est,
            sum(1 for m in bundle.messages for p in m.parts if p.kind == "image"))


def _pack_prompt(record: Record, cfg: "ResolvedConfig", ctx: "RunContext",
                 prof: "LLMProfile", schema_text: str,
                 repair: RepairContext | None, temperature: float | None,
                 label: str | None, transitions: tuple[Transition, ...] | None,
                 fragment_lens: tuple[int, ...] | None,
                 k_eff: int | None, image_px: int | None
                 ) -> tuple[PromptBundle, int]:
    """spec 3.5.2 v1.11 份额定序 (deterministic): ① the static system side
    (instruction / user schema / few-shot) is COUNTED, never trimmed (V13③ M1
    precheck territory); ② the text blocks (step lines + member digests; the
    single-record tree) render at their existing absolute caps and are counted;
    ③ images eat the remainder — k_eff = min(cap, max(2, ⌊remaining/cost⌋)),
    first/last keyframes always kept, middle uniformly downsampled (only k
    shrinks; the T14 per-fragment quotas degrade per their documented rule);
    ④ k = 2 still over → the text blocks trim (edges; digests — the fallback
    adjudication evidence — yield LAST); ⑤ still over → V10:
    ContextOverflowError(phase="precheck") — the record is rejected by the
    stage layer, the doomed request is never sent. Returns (bundle, k_used —
    the image count actually packed, 0 when imageless)."""
    b = budget.input_budget(prof)
    schema_est = (budget.est_text(json.dumps(cfg.user_schema, ensure_ascii=False))
                  if prof.supports_structured_output else 0)

    def assemble(k: int | None, fit: _PackState | None) -> PromptBundle:
        return _assemble_prompt(record, cfg, schema_text, repair, temperature,
                                label, transitions, fragment_lens, k, image_px,
                                fit=fit)

    is_ui_sequence = record.kind == "sequence" and record.modality == "ui"
    # ①② count everything at the requested cap (k_eff already min-ed inside).
    bundle = assemble(k_eff, None)
    text_est, n_images = _prompt_text_est(bundle, schema_est)
    if n_images == 0:
        if text_est <= b:
            return bundle, 0
        k_fin = 0
        image_cost = 0
    else:
        image_cost = _image_unit_cost(prof, ctx, image_px)
        remaining = b - text_est
        k_budget = max(2, remaining // image_cost)
        k_fin = min(n_images, k_budget)
        if k_fin < n_images:
            bundle = assemble(k_fin, None)
            text_est, n_images = _prompt_text_est(bundle, schema_est)
        if text_est + n_images * image_cost <= b:
            return bundle, n_images

    # ④ trim the text blocks at the keyframe floor. Shares are derived from the
    # UNtrimmed block bodies of THIS build: everything outside the two blocks
    # (incl. the V25③ repair suffix and the single-record labels) is fixed.
    steps_body = ""
    if record.kind == "sequence" and transitions is not None:
        steps_body = "\n".join(_step_line(t) for t in transitions)
    if record.kind == "sequence":
        digest_body = "\n".join(
            _member_digest_lines(record.members, cfg.input.ui_tree_max_chars))
        # fixed keeps the section headers (they live inside the parts' est);
        # the shares below are for the block BODIES the ⑤-family trims cut.
        fixed = (text_est - budget.est_text(steps_body)
                 - budget.est_text(digest_body))
        avail = b - fixed - k_fin * image_cost
        digest_share = min(budget.est_text(digest_body), max(0, avail))
        step_share = max(0, avail - digest_share)
        fit = _PackState(step_budget=step_share if transitions is not None else None,
                         digest_budget=digest_share)
    elif record.modality == "ui":
        # §3.3③ single-record family: the tree render is the ONE trimmable slot.
        tree_body = (record.ui_tree.serialize(max_chars=cfg.input.ui_tree_max_chars)
                     if record.ui_tree else "")
        fixed = text_est - budget.est_text(tree_body)
        fit = _PackState(tree_budget=b - fixed - k_fin * image_cost)
    else:
        # Plain record text is not a trim class (§3.3 vocabulary) → V10.
        raise ContextOverflowError(
            "annotation prompt exceeds the input budget at the minimal unit "
            "(single text record — no trimmable block)", phase="precheck",
            profile=prof.name)

    bundle = assemble(k_fin if is_ui_sequence else k_eff, fit)
    if fit.truncations:
        ctx.metrics.count("budget.truncations.annotate", fit.truncations)
    text_est, n_images = _prompt_text_est(bundle, schema_est)
    if text_est + n_images * image_cost > b:
        # ⑤ every trimmable share exhausted and the untrimmable floor (static
        # side + V25③ suffix + 2 keyframes) still exceeds the budget → V10.
        raise ContextOverflowError(
            "annotation prompt exceeds the input budget at the minimal unit "
            f"(k={n_images}, text floor untrimmable)", phase="precheck",
            profile=prof.name)
    return bundle, n_images


async def _budgeted_call(record: Record, ctx: "RunContext",
                         schema_text: str, repair: RepairContext | None,
                         temperature: float | None, label: str | None,
                         transitions: tuple[Transition, ...] | None,
                         fragment_lens: tuple[int, ...] | None,
                         k_eff: int | None, image_px: int | None
                         ) -> tuple[dict, Usage, int, str]:
    """One annotation call through the M8 guarantee. Budget-declared profile →
    the §3.3⑥ packing above plus the V20 reactive degrade — keyframes halve
    (k → max(2, ⌈k/2⌉), ≤ 2 degrades, budget.degrade_retries counted) and the
    terminal follows the §3.5 matrix (reactive-400 feeds the breaker exactly
    once). Budget off (cw == 0) → the pre-v1.11 build/call path byte-identically
    (the finish-oracle overflow can still surface; it propagates unfed)."""
    cfg = ctx.cfg
    profile = cfg.annotate.llm
    prof = cfg.llm_profiles.get(profile)
    if prof is None or prof.context_window <= 0:
        prompt = build_annotate_prompt(record, cfg, schema_text, repair=repair,
                                       temperature=temperature, label=label,
                                       transitions=transitions,
                                       fragment_lens=fragment_lens,
                                       k_eff=k_eff, image_px=image_px)
        return await ctx.schema_engine.complete_validated(
            profile, prompt, record_ids=(record.id,), batch_no=ctx.batch_no,
            record=record.raw)

    k_current = k_eff
    degrades = 0
    pending: ContextOverflowError | None = None
    while True:
        try:
            prompt, k_used = _pack_prompt(record, cfg, ctx, prof, schema_text,
                                          repair, temperature, label, transitions,
                                          fragment_lens, k_current, image_px)
        except ContextOverflowError:
            # V10 from the packer: the reactive overflow (if any) that drove the
            # degrade settles its terminal here (A7); the precheck raise itself
            # never feeds.
            if pending is not None:
                _feed_reactive_terminal(pending, ctx.metrics)
            raise
        try:
            return await ctx.schema_engine.complete_validated(
                profile, prompt, record_ids=(record.id,), batch_no=ctx.batch_no,
                record=record.raw)
        except ContextOverflowError as exc:
            if k_used > 2 and degrades < 2:
                degrades += 1
                pending = exc
                ctx.metrics.count("budget.degrade_retries")
                k_current = max(2, math.ceil(k_used / 2))   # V20 frame halving
                continue
            _feed_reactive_terminal(exc, ctx.metrics)
            raise


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
                          transitions: tuple[Transition, ...] | None = None,
                          fragment_lens: tuple[int, ...] | None = None,
                          k_eff: int | None = None,
                          image_px: int | None = None) -> Annotation:
    """One record's full annotation path incl. self-consistency (skipped when repair is
    not None: repair re-annotation is always a single call at profile-default temperature).
    Raises SchemaViolation / ProviderRetryableError / ProviderFatalError.
    v1.7 (R2): label is passed through to build_annotate_prompt (class-effective
    instruction/examples); llm/self_consistency/sc_temperature stay global (whitelist).
    v1.8 (S5, additive trailing kwarg): transitions is passed through to
    build_annotate_prompt on every path (single call, each self-consistency sample, and
    repair re-annotation — M7 threads the REBUILT value after member surgery); None =
    pre-v1.8 behavior. Sequence records carry raw = None, so the L2.5 callback receives
    record=None (documented limitation).
    v1.9 (T14, third additive trailing kwarg): fragment_lens is passed through on every
    path the same way — both call sites (M5 main, M7 repair re-annotation) thread it
    from the M16 stitch_fragments duck mark; None = pre-v1.9 behavior.
    v1.11 (V21 ladder / F3, additive trailing kwargs — CONTRACTS §7.4): the M7 repair
    driver passes the quality-ladder step on verify-fail re-annotation (k_eff = the
    keyframe cap halved to max(2, ⌈k/2⌉), image_px = one rung up at 1.5×/dim ≤
    max_image_px, budget re-checked by M7 against the calibrated estimate); M5's own
    V20 overflow degrade passes k_eff alone (inside _budgeted_call). Both ride
    build_annotate_prompt on EVERY path — None/None = pre-v1.11 byte-identical."""
    cfg = ctx.cfg
    schema_text = ctx.schema_engine.user_schema_text
    n = cfg.annotate.self_consistency

    if repair is not None or n == 0:
        obj, usage, attempts, model = await _budgeted_call(
            record, ctx, schema_text, repair, None, label, transitions,
            fragment_lens, k_eff, image_px)
        return Annotation(output=obj, model=model, attempts=attempts, usage=usage)

    # Self-consistency: n independent samples at sc_temperature, each through the full
    # M8 guarantee; a SchemaViolation sample abstains (denominator stays n).
    async def one_sample() -> tuple[dict, Usage, int, str]:
        return await _budgeted_call(
            record, ctx, schema_text, None, cfg.annotate.sc_temperature, label,
            transitions, fragment_lens, k_eff, image_px)

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
        # v1.9 (T14): the per-fragment keyframe quota rides the M16 duck mark.
        fragments = getattr(item, "stitch_fragments", None)
        fragment_lens = (tuple(int(f["member_count"]) for f in fragments)
                         if fragments else None)
        try:
            item.annotation = await annotate_record(record, ctx, label=label,
                                                    transitions=item.transitions,
                                                    fragment_lens=fragment_lens)
        except SchemaViolation as e:
            # Transport the raw last model output to M11 for the rejects "full"
            # tier (§9.2) via the duck-typed channel the emitter reads.
            item.raw_last_output = e.raw_last_output  # type: ignore[attr-defined]
            kind = (ErrorKind.CALLBACK_VIOLATION if getattr(e, "callback_only", False)
                    else ErrorKind.SCHEMA_VIOLATION)
            self._fail(item, ctx, kind.value, str(e), retryable=False)
        except (ContextOverflowError, OutputTruncatedError) as e:
            # v1.11 (V27①): the budget vocabulary routes FIRST — precise kinds,
            # record-level failed → rejects. Terminal breaker feeds already
            # happened inside _budgeted_call (A7 — duck-flag idempotent).
            self._fail(item, ctx, budget.classify_stage_error(e), str(e),
                       retryable=False)
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
        if kind == ErrorKind.CONTEXT_OVERFLOW.value:
            ctx.metrics.count("budget.overflow_records")  # V13②: rejected, all phases
        ctx.metrics.event(EV_ERROR, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(item.record.id,),
                          payload={"stage": self.name, "kind": kind,
                                   "message": message, "retryable": retryable})

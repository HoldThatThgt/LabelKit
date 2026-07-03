"""M4 quality scoring (QuRating) — spec 3.4, CONTRACTS.md §7.3 / §10.2 / §10.3.

pairwise mode: k rounds of seeded random perfect matchings within the batch, LLM pairwise
judgments (optionally double-order and/or multi-judge with per-criterion majority vote),
Bradley-Terry fit via the MM algorithm (Hunter 2004), per-batch log-theta percentile
normalization to [0, 1].

pointwise mode: 0-5 additive rubric scoring per record per criterion, normalized /5.

Aggregate = weighted mean over non-null criterion scores per rubric weights. Gate: threshold
or top_ratio selection; unscored records handled per quality.on_unscored.
"""
from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING, Literal, Mapping, Sequence

import numpy as np

from labelkit.errors import (
    CircuitBreakerTripped,
    ErrorKind,
    ProviderFatalError,
    ProviderRetryableError,
    SchemaViolation,
)
from labelkit.types import PipelineItem, QualityScore, Record, StageError

if TYPE_CHECKING:
    from labelkit.config.model import Criterion, ResolvedConfig
    from labelkit.stage import RunContext

# M9 (llm_client) / M8 (schema_engine) public surface per CONTRACTS.md §7.8 / §7.7.
from labelkit.llm_client import Message, Part, PromptBundle
from labelkit.schema_engine import judgment_schema, pointwise_schema


AGGREGATE_KEY = "__aggregate__"

# Event names (exact strings per CONTRACTS.md §7.11 / §8.1).
_EV_JUDGMENT = "quality.judgment"
_EV_POINTWISE = "quality.pointwise"
_EV_BT_FIT = "quality.bt_fit"
_EV_GATE = "quality.gate"
_EV_ERROR = "error"

_COUNTER_JUDGMENT_FAILURES = "quality.judgment_failures"


# ── Bradley-Terry fit (MM algorithm, Hunter 2004) ─────────────────────────────

def fit_bradley_terry(n_items: int, comparisons: list[tuple[int, int, float]],
                      l2_pseudo: float = 0.1, tol: float = 1e-6,
                      max_iter: int = 200) -> np.ndarray:
    """comparisons: (winner_idx, loser_idx, weight); a tie is split into two entries with
    weight=0.5 each. MM iteration (Hunter 2004) with lambda=l2_pseudo pseudo-matches
    (half-win/half-loss vs a virtual opponent theta=1), renormalized to prod(theta)=1 per
    iteration; stops at max|delta log theta| < tol or max_iter. Returns log-theta array of
    length n_items."""
    log_theta, _, _ = _fit_bradley_terry_details(n_items, comparisons, l2_pseudo, tol, max_iter)
    return log_theta


def _fit_bradley_terry_details(n_items: int, comparisons: list[tuple[int, int, float]],
                               l2_pseudo: float = 0.1, tol: float = 1e-6,
                               max_iter: int = 200) -> tuple[np.ndarray, int, bool]:
    """As fit_bradley_terry, plus (iterations, converged) for the quality.bt_fit event."""
    if n_items == 0:
        return np.zeros(0), 0, True
    # W[i] = win total incl. the lambda/2 pseudo half-win vs the virtual opponent.
    w = np.full(n_items, l2_pseudo / 2.0, dtype=float)
    # n[i][j] = total comparison weight between i and j (symmetric).
    n = np.zeros((n_items, n_items), dtype=float)
    for winner, loser, weight in comparisons:
        w[winner] += weight
        n[winner, loser] += weight
        n[loser, winner] += weight

    theta = np.ones(n_items, dtype=float)
    iterations = 0
    converged = False
    for iterations in range(1, max_iter + 1):
        # denom_i = sum_j n_ij/(theta_i+theta_j) + lambda/(theta_i + 1)   (virtual opponent)
        pair_sums = theta[:, None] + theta[None, :]
        denom = (n / pair_sums).sum(axis=1) + l2_pseudo / (theta + 1.0)
        new_theta = w / denom
        # Renormalize to prod(theta) = 1 (divide by the geometric mean).
        new_theta = new_theta / np.exp(np.mean(np.log(new_theta)))
        delta = float(np.max(np.abs(np.log(new_theta) - np.log(theta))))
        theta = new_theta
        if delta < tol:
            converged = True
            break
    return np.log(theta), iterations, converged


# ── pure helpers (unit-tested directly) ───────────────────────────────────────

def _pairing_plan(n_items: int, rounds: int, rng) -> list[tuple[int, int, int, bool]]:
    """k rounds of random perfect matchings: shuffle indexes, pair adjacent; an odd leftover
    sits the round out. Returns (round_no 1-based, first_idx, second_idx, first_is_a) in a
    deterministic draw order — all randomness comes from `rng` (ctx.rng), pre-drawn before
    any LLM dispatch. (first_idx, second_idx) is the SAMPLING order; presentation order is
    given by first_is_a (possibly flipped again by both_orders)."""
    plan: list[tuple[int, int, int, bool]] = []
    for round_no in range(1, rounds + 1):
        order = list(range(n_items))
        rng.shuffle(order)
        for k in range(n_items // 2):
            i, j = order[2 * k], order[2 * k + 1]
            first_is_a = rng.random() < 0.5
            plan.append((round_no, i, j, first_is_a))
    return plan


def _average_ranks(values: Sequence[float]) -> list[float]:
    """Ascending 1-based ranks; exact ties get the average rank."""
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    pos = 0
    while pos < n:
        end = pos
        while end + 1 < n and values[order[end + 1]] == values[order[pos]]:
            end += 1
        avg = (pos + end) / 2.0 + 1.0
        for k in range(pos, end + 1):
            ranks[order[k]] = avg
        pos = end + 1
    return ranks


def _percentile_scores(values: Sequence[float]) -> list[float]:
    """score = (rank-1)/(N-1) over ascending average ranks; N == 1 -> [0.5]."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.5]
    return [(rank - 1.0) / (n - 1.0) for rank in _average_ranks(values)]


def _weighted_aggregate(criteria: Sequence["Criterion"],
                        scores: Mapping[str, float | None]) -> float | None:
    """Sum(w_i * s_i) / Sum(w_i) over criteria whose score is non-null; all-null -> None."""
    num = 0.0
    den = 0.0
    for crit in criteria:
        s = scores.get(crit.key)
        if s is None:
            continue
        num += crit.weight * s
        den += crit.weight
    if den == 0.0:
        return None
    return num / den


def _top_ratio_selection(scored: Sequence[tuple[str, float]],
                         top_ratio: float) -> tuple[set[str], dict[str, int]]:
    """scored: (record_id, aggregate). Keeps the top ceil(top_ratio * len(scored)) by
    (aggregate desc, id asc). Returns (kept ids, 1-based rank per id). Unscored records are
    NOT in `scored` (they occupy no quota slot, spec 3.4.3)."""
    ranked = sorted(scored, key=lambda t: (-t[1], t[0]))
    quota = math.ceil(top_ratio * len(ranked))
    ranks = {rec_id: pos + 1 for pos, (rec_id, _) in enumerate(ranked)}
    kept = {rec_id for rec_id, _ in ranked[:quota]}
    return kept, ranks


def _pointwise_label(description: str) -> str:
    """{label} = description up to (excluding) its first '：', or the whole description."""
    return description.split("：", 1)[0]


def _criterion_percentiles(log_theta: Sequence[float],
                           unscored: set[int]) -> list[float | None]:
    """Spec 3.4.3 normalization: percentile-rank ALL batch log θ values (『将批内全部
    log θ 升序排名』, CONTRACTS.md §7.3), then null out the unscored records' own scores —
    their exclusion must not shift any other record's rank."""
    pct = _percentile_scores([float(v) for v in log_theta])
    return [None if k in unscored else pct[k] for k in range(len(pct))]


def _classify_call_error(exc: Exception) -> tuple[str, bool]:
    """(StageError.kind, retryable) for a judging-call failure that is NOT a schema-invalid
    judgment (spec 7.6): M9 retry exhaustion is record-level provider_retryable_exhausted;
    M9 auth/4xx is run-level provider_fatal (obslog mirrors that kind to stderr at ERROR);
    anything else is invariant breakage."""
    if isinstance(exc, ProviderRetryableError):
        return ErrorKind.PROVIDER_RETRYABLE_EXHAUSTED.value, True
    if isinstance(exc, ProviderFatalError):
        return ErrorKind.PROVIDER_FATAL.value, False
    return ErrorKind.INTERNAL_ERROR.value, False


def _violation_summary(exc: SchemaViolation) -> str:
    """Data-free rendering of a SchemaViolation for the error-event message. exc.errors are
    '<json-pointer>: <description>' strings whose description part embeds instance values
    from LLM output — which may quote record content — and the error event's message is
    mirrored to the stderr run log, which must never carry data content (spec 7.1,
    CONTRACTS.md §8.4). Only the JSON Pointers (schema-defined keys / array indices)
    survive here."""
    pointers = ", ".join(
        dict.fromkeys(v.split(":", 1)[0] or "<root>" for v in exc.errors))
    return f"{len(exc.errors)} violation(s) at {pointers}"


# ── prompt assembly (CONTRACTS.md §10.2 / §10.3, byte-exact Chinese) ──────────

def _record_parts(record: Record, label: str, ui_tree_max_chars: int) -> list[Part]:
    """Text modality: one '[label] text' line; UI modality: three parts per §10.2."""
    if record.modality == "text":
        return [Part(kind="text", text=f"[{label}] {record.text}")]
    tree = record.ui_tree.serialize(max_chars=ui_tree_max_chars) if record.ui_tree else ""
    return [Part(kind="text", text=f"[{label} 屏幕截图]"),
            Part(kind="image", image=record.image),
            Part(kind="text", text=f"[{label} UI 控件树]\n{tree}")]


def _build_pairwise_prompt(rec_a: Record, rec_b: Record, criteria: Sequence["Criterion"],
                           with_reason: bool, ui_tree_max_chars: int) -> PromptBundle:
    lines = ["你将对两条记录进行成对质量比较。准则如下："]
    for crit in criteria:
        lines.append(f"- {crit.key}: {crit.description}")
        lines.append(f"  {crit.pairwise_prompt}")
    lines.append("对每条准则给出裁决。输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：")
    if with_reason:
        lines.append('{"judgments": [{"criterion": <准则 key>, "winner": "A"|"B"|"tie", '
                     '"reason": <一句话理由>}]}')
    else:
        lines.append('{"judgments": [{"criterion": <准则 key>, "winner": "A"|"B"|"tie"}]}')
    system = Message(role="system", parts=(Part(kind="text", text="\n".join(lines)),))

    if rec_a.modality == "text":
        user_parts = [Part(kind="text",
                           text=f"[记录 A] {rec_a.text}\n[记录 B] {rec_b.text}")]
    else:
        user_parts = (_record_parts(rec_a, "记录 A", ui_tree_max_chars)
                      + _record_parts(rec_b, "记录 B", ui_tree_max_chars))
    user = Message(role="user", parts=tuple(user_parts))
    return PromptBundle(messages=(system, user))


def _build_pointwise_prompt(record: Record, criterion: "Criterion",
                            ui_tree_max_chars: int) -> PromptBundle:
    label = _pointwise_label(criterion.description)
    lines = [f"按以下 0–5 加性量表为记录的 {criterion.key}（{label}）打分，"
             "先给两句理由再给整数分："]
    lines.extend(criterion.pointwise_levels)
    lines.append('输出 JSON：{"scores": [{"criterion": <准则 key>, "reason": <两句理由>, '
                 '"score": 0..5}]}')
    system = Message(role="system", parts=(Part(kind="text", text="\n".join(lines)),))
    user = Message(role="user", parts=tuple(_record_parts(record, "记录内容", ui_tree_max_chars)))
    return PromptBundle(messages=(system, user))


# ── the stage ─────────────────────────────────────────────────────────────────

class QualityStage:
    name = "quality"

    def __init__(self, cfg: "ResolvedConfig"):
        self.cfg = cfg

    async def run(self, batch: list[PipelineItem], ctx: "RunContext") -> list[PipelineItem]:
        items = [it for it in batch if it.status == "active"]
        if not items:
            return batch
        mode: Literal["pairwise_bt", "pointwise"] = (
            "pairwise_bt" if self.cfg.quality.mode == "pairwise" else "pointwise")
        try:
            if mode == "pairwise_bt":
                await self._run_pairwise(items, ctx)
            else:
                await self._run_pointwise(items, ctx)
        except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as exc:  # invariant breakage — fail the batch's records, not the run
            for it in items:
                if it.status == "active":
                    err = StageError(stage=self.name, kind=ErrorKind.INTERNAL_ERROR.value,
                                     message=f"quality stage internal error: {exc}",
                                     retryable=False)
                    it.errors.append(err)
                    it.status = "failed"
                    self._emit_error(ctx, (it.record.id,), err)
            return batch
        self._apply_gate(items, ctx)
        return batch

    # ── shared plumbing ────────────────────────────────────────────────────

    def _reasons_effective(self) -> bool:
        jr = self.cfg.quality.judgment_reasons
        if jr == "auto":
            return self.cfg.trace.enabled and "quality" in self.cfg.trace.channels
        return bool(jr)

    def _excerpt_payload(self, records: Sequence[Record]) -> dict | None:
        """`excerpt` payload addition for the excerpt/full trace.content tiers (§8.3)."""
        if not (self.cfg.trace.enabled and self.cfg.trace.content in ("excerpt", "full")):
            return None
        out: dict[str, str] = {}
        for rec in records:
            content = rec.text if rec.modality == "text" else (
                rec.ui_tree.serialize() if rec.ui_tree else "")
            out[rec.id] = (content or "")[:200]
        return out

    def _emit_error(self, ctx: "RunContext", record_ids: tuple[str, ...],
                    err: StageError) -> None:
        ctx.metrics.event(_EV_ERROR, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=record_ids,
                          payload={"stage": err.stage, "kind": err.kind,
                                   "message": err.message, "retryable": err.retryable})

    def _record_judgment_failure(self, ctx: "RunContext", items: Sequence[PipelineItem],
                                 message: str) -> None:
        """Judgment still schema-invalid after M8 repair (spec 3.4.3 裁决失败):
        comparison-level, counts as tie (BT-neutral), items stay active, and it is the ONLY
        path that increments quality.judgment_failures (the §7.5 rubric diagnostic)."""
        ctx.metrics.count(_COUNTER_JUDGMENT_FAILURES)
        err = StageError(stage=self.name, kind=ErrorKind.JUDGMENT_INVALID.value,
                         message=message, retryable=False)
        for it in items:
            it.errors.append(err)
        self._emit_error(ctx, tuple(it.record.id for it in items), err)

    def _record_call_failure(self, ctx: "RunContext", items: Sequence[PipelineItem],
                             exc: Exception, what: str) -> None:
        """Provider/internal failure of a judging call. Unlike a schema-invalid judgment
        this is not a rubric problem: per spec 7.6 the involved records fail (no tie
        fallback) and quality.judgment_failures is NOT incremented."""
        kind, retryable = _classify_call_error(exc)
        err = StageError(stage=self.name, kind=kind,
                         message=f"{what} ({type(exc).__name__}): {exc}",
                         retryable=retryable)
        for it in items:
            it.errors.append(err)
            it.status = "failed"
        self._emit_error(ctx, tuple(it.record.id for it in items), err)

    # ── pairwise mode ──────────────────────────────────────────────────────

    async def _run_pairwise(self, items: list[PipelineItem], ctx: "RunContext") -> None:
        q = self.cfg.quality
        criteria = self.cfg.rubric.criteria
        n = len(items)

        if n == 1:  # batch of 1: no judging calls, every criterion score fixed 0.5
            item = items[0]
            for crit in criteria:
                item.scores[crit.key] = QualityScore(
                    criterion=crit.key, score=0.5, mode="pairwise_bt",
                    detail={"comparisons": 0, "wins": 0, "ties": 0, "log_theta": 0.0})
            item.scores[AGGREGATE_KEY] = QualityScore(
                criterion=AGGREGATE_KEY, score=0.5, mode="pairwise_bt", detail={})
            return

        with_reason = self._reasons_effective()
        judges: tuple[str, ...] = q.judges if q.judges else (q.llm,)
        orders = (False, True) if q.both_orders else (False,)
        if q.criteria_per_call == "all":
            crit_groups: list[tuple["Criterion", ...]] = [tuple(criteria)]
        else:
            crit_groups = [(c,) for c in criteria]

        # Pre-draw the full pairing + presentation plan before any dispatch (determinism).
        plan = _pairing_plan(n, q.rounds, ctx.rng)

        # One judgment call per (comparison, judge, order, criterion-group).
        calls = []
        for comp_idx, (_round_no, i, j, first_is_a) in enumerate(plan):
            for judge in judges:
                for flipped in orders:
                    a_idx, b_idx = (i, j) if (first_is_a != flipped) else (j, i)
                    for group in crit_groups:
                        calls.append(self._judge_once(
                            ctx, items, comp_idx, i, j, a_idx, b_idx,
                            judge, flipped, group, with_reason,
                            multi_judge=len(judges) > 1))
        raw_results = await asyncio.gather(*calls)

        # results[comp_idx][criterion][judge][flipped] = winner idx | "tie" | None (failed)
        results: dict[int, dict[str, dict[str, dict[bool, int | str | None]]]] = {}
        for comp_idx, judge, flipped, verdicts in raw_results:
            comp = results.setdefault(comp_idx, {})
            for crit_key, outcome in verdicts.items():
                comp.setdefault(crit_key, {}).setdefault(judge, {})[flipped] = outcome

        # Compose per criterion: per-judge both-orders consistency first, then majority vote.
        for crit in criteria:
            entries: list[tuple[int, int, float]] = []
            comp_count = [0] * n
            wins = [0] * n
            ties = [0] * n
            success = [0] * n
            n_judged = 0        # comparisons with at least one successful judgment
            n_tie_judged = 0    # …of those, the ones that resolved to a tie
            for comp_idx, (_round_no, i, j, _first_is_a) in enumerate(plan):
                per_judge = results.get(comp_idx, {}).get(crit.key, {})
                votes: list[int | str | None] = []
                for judge in judges:
                    outcomes = per_judge.get(judge, {})
                    votes.append(self._compose_orders(
                        [outcomes.get(flipped) for flipped in orders]))
                outcome = self._majority(votes, i, j)
                comp_count[i] += 1
                comp_count[j] += 1
                if outcome is not None:  # at least one judgment call succeeded
                    success[i] += 1
                    success[j] += 1
                    n_judged += 1
                    if outcome == "tie":
                        n_tie_judged += 1
                verdict: int | str = "tie" if outcome is None else outcome
                if verdict == "tie":
                    ties[i] += 1
                    ties[j] += 1
                    entries.append((i, j, 0.5))
                    entries.append((j, i, 0.5))
                else:
                    winner = int(verdict)
                    loser = j if winner == i else i
                    wins[winner] += 1
                    entries.append((winner, loser, 1.0))

            # Per-criterion tie tally for report.quality.per_criterion_tie_rate
            # (E2E finding P4-9): pairwise percentile means are 0.5 by
            # construction, so the report carries the discriminative signal.
            # Only comparisons that produced a verdict are counted (review
            # finding): folding provider failures in would inflate the tie
            # rate and send the user chasing rubric wording when the endpoint
            # is the culprit — call failures show up in counts.failed and
            # judgment_failures instead.
            ctx.metrics.count(f"quality.tie_outcomes.{crit.key}", n_tie_judged)
            ctx.metrics.count(f"quality.tie_comparisons.{crit.key}", n_judged)

            log_theta, iterations, converged = _fit_bradley_terry_details(n, entries)
            ctx.metrics.event(_EV_BT_FIT, stage=self.name, batch_no=ctx.batch_no,
                              payload={"criterion": crit.key, "iterations": iterations,
                                       "converged": converged, "comparisons": len(plan)})

            # A record is unscored on this criterion iff it participated in >= 1 comparison
            # and every one of them failed; zero-participation records are covered by the
            # BT regularization pseudo-counts (spec 3.4.3) and stay scored. The percentile
            # ranking spans ALL n batch records (spec 3.4.3『将批内全部 log θ 升序排名』);
            # unscored records only get their OWN score nulled.
            unscored = {k for k in range(n) if comp_count[k] > 0 and success[k] == 0}
            scores = _criterion_percentiles([float(v) for v in log_theta], unscored)
            for k, item in enumerate(items):
                item.scores[crit.key] = QualityScore(
                    criterion=crit.key, score=scores[k], mode="pairwise_bt",
                    detail={"comparisons": comp_count[k], "wins": wins[k],
                            "ties": ties[k], "log_theta": float(log_theta[k])})

        self._set_aggregates(items, "pairwise_bt")

    @staticmethod
    def _compose_orders(outcomes: list[int | str | None]) -> int | str | None:
        """Per-judge composition. Single order: pass through. both_orders: two outcomes,
        consistent (same record, or both tie) -> that result, inconsistent -> tie; a failed
        order counts as tie (spec 3.4.3 判定失败); both failed -> None (failed)."""
        if len(outcomes) == 1:
            return outcomes[0]
        o1, o2 = outcomes
        if o1 is None and o2 is None:
            return None
        v1: int | str = "tie" if o1 is None else o1
        v2: int | str = "tie" if o2 is None else o2
        return v1 if v1 == v2 else "tie"

    @staticmethod
    def _majority(votes: list[int | str | None], i: int, j: int) -> int | str | None:
        """Across judges: strict majority over the three classes {i wins, j wins, tie};
        no majority -> tie. Failed judges count as tie unless ALL failed -> None."""
        if all(v is None for v in votes):
            return None
        counted = ["tie" if v is None else v for v in votes]
        total = len(counted)
        for cls in (i, j, "tie"):
            if counted.count(cls) * 2 > total:
                return cls
        return "tie"

    async def _judge_once(self, ctx: "RunContext", items: list[PipelineItem], comp_idx: int,
                          first_idx: int, second_idx: int, a_idx: int, b_idx: int,
                          judge: str, flipped: bool, group: tuple["Criterion", ...],
                          with_reason: bool, multi_judge: bool
                          ) -> tuple[int, str, bool, dict[str, int | str | None]]:
        """One LLM judgment call. Returns (comp_idx, judge, flipped,
        {criterion: winner idx | 'tie' | None-on-failure})."""
        rec_first = items[first_idx].record
        rec_second = items[second_idx].record
        rec_a = items[a_idx].record
        rec_b = items[b_idx].record
        keys = [c.key for c in group]
        prompt = _build_pairwise_prompt(rec_a, rec_b, group, with_reason,
                                        self.cfg.input.ui_tree_max_chars)
        schema = judgment_schema(keys, with_reason)
        try:
            obj, _usage, _attempts, model = await ctx.schema_engine.complete_validated(
                judge, prompt, schema,
                record_ids=(rec_first.id, rec_second.id), batch_no=ctx.batch_no)
        except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
            raise
        except SchemaViolation as exc:
            # Still invalid after M8 repair -> this comparison counts as tie (spec 3.4.3).
            self._record_judgment_failure(
                ctx, (items[first_idx], items[second_idx]),
                f"pairwise judgment failed (SchemaViolation): {_violation_summary(exc)}")
            return comp_idx, judge, flipped, {k: None for k in keys}
        except Exception as exc:
            self._record_call_failure(ctx, (items[first_idx], items[second_idx]), exc,
                                      "pairwise judgment call failed")
            return comp_idx, judge, flipped, {k: None for k in keys}

        by_key: dict[str, Mapping] = {}
        for entry in obj.get("judgments", []):
            by_key.setdefault(entry["criterion"], entry)

        payload: dict = {"order": {"A": rec_a.id, "B": rec_b.id}, "model": model,
                         "judgments": [dict(by_key[k]) for k in keys if k in by_key]}
        if multi_judge:
            payload["judge"] = judge
        excerpt = self._excerpt_payload((rec_first, rec_second))
        if excerpt is not None:
            payload["excerpt"] = excerpt
        # record_ids in SAMPLING order, not presented A/B order (§8.1).
        ctx.metrics.event(_EV_JUDGMENT, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(rec_first.id, rec_second.id), payload=payload)

        verdicts: dict[str, int | str | None] = {}
        for key in keys:
            entry = by_key.get(key)
            winner = entry["winner"] if entry else "tie"  # uncovered criterion -> tie
            if winner == "A":
                verdicts[key] = a_idx
            elif winner == "B":
                verdicts[key] = b_idx
            else:
                verdicts[key] = "tie"
        return comp_idx, judge, flipped, verdicts

    # ── pointwise mode ─────────────────────────────────────────────────────

    async def _run_pointwise(self, items: list[PipelineItem], ctx: "RunContext") -> None:
        criteria = self.cfg.rubric.criteria
        calls = [self._pointwise_once(ctx, item, crit)
                 for item in items for crit in criteria]
        await asyncio.gather(*calls)
        self._set_aggregates(items, "pointwise")

    async def _pointwise_once(self, ctx: "RunContext", item: PipelineItem,
                              criterion: "Criterion") -> None:
        rec = item.record
        prompt = _build_pointwise_prompt(rec, criterion, self.cfg.input.ui_tree_max_chars)
        schema = pointwise_schema(criterion.key)
        try:
            obj, _usage, _attempts, _model = await ctx.schema_engine.complete_validated(
                self.cfg.quality.llm, prompt, schema,
                record_ids=(rec.id,), batch_no=ctx.batch_no)
        except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
            raise
        except SchemaViolation as exc:
            # Still invalid after M8 repair -> null score, on_unscored decides (spec 3.4.3).
            self._record_judgment_failure(
                ctx, (item,),
                f"pointwise scoring failed for criterion {criterion.key} "
                f"(SchemaViolation): {_violation_summary(exc)}")
            item.scores[criterion.key] = QualityScore(
                criterion=criterion.key, score=None, mode="pointwise", detail={})
            return
        except Exception as exc:
            self._record_call_failure(
                ctx, (item,), exc,
                f"pointwise scoring call failed for criterion {criterion.key}")
            return

        entry = obj["scores"][0]
        raw = int(entry["score"])
        reason = entry.get("reason", "")
        item.scores[criterion.key] = QualityScore(
            criterion=criterion.key, score=raw / 5.0, mode="pointwise",
            detail={"raw_score": raw, "reason": reason})

        payload: dict = {"criterion": criterion.key, "score": raw}
        if self._reasons_effective():
            payload["reason"] = reason
        excerpt = self._excerpt_payload((rec,))
        if excerpt is not None:
            payload["excerpt"] = excerpt
        ctx.metrics.event(_EV_POINTWISE, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(rec.id,), payload=payload)

    # ── aggregation + gate ─────────────────────────────────────────────────

    def _set_aggregates(self, items: Sequence[PipelineItem],
                        mode: Literal["pairwise_bt", "pointwise"]) -> None:
        criteria = self.cfg.rubric.criteria
        for item in items:
            per_crit = {key: qs.score for key, qs in item.scores.items()
                        if key != AGGREGATE_KEY}
            agg = _weighted_aggregate(criteria, per_crit)
            item.scores[AGGREGATE_KEY] = QualityScore(
                criterion=AGGREGATE_KEY, score=agg, mode=mode, detail={})

    def _apply_gate(self, items: Sequence[PipelineItem], ctx: "RunContext") -> None:
        q = self.cfg.quality
        active = [it for it in items if it.status == "active"]

        def agg_of(it: PipelineItem) -> float | None:
            qs = it.scores.get(AGGREGATE_KEY)
            return qs.score if qs is not None else None

        scored = [(it, agg_of(it)) for it in active if agg_of(it) is not None]
        unscored = [it for it in active if agg_of(it) is None]
        gating = q.selection == "top_ratio" or q.threshold is not None

        if q.selection == "top_ratio":
            kept, ranks = _top_ratio_selection(
                [(it.record.id, agg) for it, agg in scored], q.top_ratio)
            for it, agg in scored:
                keep = it.record.id in kept
                ctx.metrics.event(_EV_GATE, stage=self.name, batch_no=ctx.batch_no,
                                  record_ids=(it.record.id,),
                                  payload={"aggregate": agg,
                                           "decision": "keep" if keep else "drop",
                                           "selection": "top_ratio",
                                           "top_ratio": q.top_ratio,
                                           "rank": ranks[it.record.id]})
                if not keep:
                    it.status = "dropped_lowq"
        elif q.threshold is not None:
            for it, agg in scored:
                keep = agg >= q.threshold
                ctx.metrics.event(_EV_GATE, stage=self.name, batch_no=ctx.batch_no,
                                  record_ids=(it.record.id,),
                                  payload={"aggregate": agg,
                                           "decision": "keep" if keep else "drop",
                                           "threshold": q.threshold})
                if not keep:
                    it.status = "dropped_lowq"

        # Unscored records: on_unscored applies regardless of gating mode (spec 3.4.3
        # 判定失败 row); "keep" -> stays active with null scores, occupies no top_ratio slot.
        for it in unscored:
            keep = q.on_unscored == "keep"
            if gating or not keep:
                payload: dict = {"aggregate": None, "decision": "keep" if keep else "drop"}
                if q.selection == "top_ratio":
                    payload["selection"] = "top_ratio"
                    payload["top_ratio"] = q.top_ratio
                elif q.threshold is not None:
                    payload["threshold"] = q.threshold
                ctx.metrics.event(_EV_GATE, stage=self.name, batch_no=ctx.batch_no,
                                  record_ids=(it.record.id,), payload=payload)
            if not keep:
                it.status = "dropped_lowq"

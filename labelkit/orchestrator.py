"""M10 orchestrator (spec 3.10, CONTRACTS.md §7.9).

Pure composition/scheduling — zero business logic, no direct LLM calls, no file
writes (M11 owns the output channels). Responsibilities:

- slice the M2 record stream (or the M6 generate_all output in generate_only
  mode) into batches of ``run.batch_size``; in stream mode (v1.8,
  ``segment.enabled``) consume the M2 session-stream view instead and pack
  WHOLE sessions per batch by next-fit — one open bin, oversized sessions
  hard-split (S21) — stamping ``session_id`` on frame envelopes (S4);
- compose the per-batch stage chain from the config switches (2.3.1 matrix) in
  the canonical order segment → dedup → classify → extract → quality →
  generate → annotate → verify (the v1.8 single superset tuple; segment and
  extract default off, degrading byte-identically to the v1.7 chain);
- meter ``counts.fanout`` / ``counts.episodes`` as the len-delta across the
  classify / segment stage (v1.7 R9 / v1.8 §7.9 — ``counts.*`` ownership stays
  with M10; M13/M14 append siblings/episodes in place);
- schedule the single-round generation re-flow (sub-batches re-enter at M3,
  never at M6 — no recursion);
- drive per-batch lifecycle: fresh RunContext per (batch, stage), emit via M11,
  flush trace, then drop every batch intermediate (memory release);
- aggregate run-level stats into the §9.3 report structure and hand it to
  ``Emitter.finalize``;
- circuit-breaker handling (``CircuitBreakerTripped`` → exit code 4, report
  written, ``.part`` NOT renamed) and SIGINT/SIGTERM graceful interruption;
- ``--limit`` truncation and ``--dry-run`` (M1/M2 + static call/cost estimate
  on stderr, no LLM calls, no main output/rejects — report and, when
  ``trace.enabled``, the trace channel only).
"""
from __future__ import annotations

import asyncio
import logging
import random
import signal as _signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from typing import TYPE_CHECKING, Mapping, Sequence

from labelkit import TOOL_VERSION, __version__
from labelkit.errors import CircuitBreakerTripped, InternalError
from labelkit.stage import RunContext, Stage
from labelkit.types import PipelineItem, Record

if TYPE_CHECKING:
    from labelkit.config.model import ResolvedConfig
    from labelkit.emitter import Emitter
    from labelkit.ingest import Ingestor
    from labelkit.llm_client import LLMClient
    from labelkit.obslog import MetricsSink
    from labelkit.schema_engine import SchemaEngine

# Event names — exact strings per CONTRACTS.md §7.11/§8.1 (mirrors the
# labelkit.obslog constants; literals here keep this module importable before
# obslog.py lands and are test-asserted against the contract).
_EV_RUN_START = "run.start"
_EV_RUN_END = "run.end"
_EV_BATCH_START = "batch.start"
_EV_BATCH_END = "batch.end"

# Canonical pipeline order (spec §2.2 / CONTRACTS.md §2/§7.9): the v1.8 SINGLE
# SUPERSET TUPLE — v1.7 inserted classify right after dedup; v1.8 prepends
# segment and slots extract between classify and quality. segment/extract are
# default OFF, so the effective chain degrades byte-identically to the v1.7
# six-name form (generate and segment are mutually exclusive per M1, so the two
# never co-occupy the chain).
_CHAIN_ORDER = ("segment", "dedup", "classify", "extract", "quality", "generate",
                "annotate", "verify")

# v1.8 closed vocabularies (§9.3 report zero-based histograms) — must equal the
# schema_engine enums: action_schema() action_type 11 values (S15) and
# defect_verdict_schema() defect kind 5 values (S31).
_ACTION_TYPES = ("click", "long_press", "input_text", "scroll", "drag", "open_app",
                 "app_switch", "navigate_back", "navigate_home", "wait", "other")
_DEFECT_KINDS = ("label_mismatch", "off_task_members", "missing_head",
                 "missing_tail", "missing_members")

_log = logging.getLogger("labelkit.orchestrator")

# report.json quality.aggregate_histogram bucket labels — frozen (§9.3).
_HIST_LABELS = tuple(f"{i / 10:.1f}-{(i + 1) / 10:.1f}" for i in range(10))

_SCHEMA_STATS_ZERO = {"l0_or_clean": 0, "l1": 0, "l3_1": 0, "l3_2": 0, "rejected": 0}


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b) if b > 0 else 0


def _pack_next_fit(session_lens: Sequence[int],
                   batch_size: int) -> tuple[list[int], list[int]]:
    """Next-fit packing simulation over session lengths — mirrors
    ``_run_process_stream`` exactly, so the dry-run batch count is EXACT
    (S21/S22). Returns (frames per batch, session pieces per batch); an
    oversized session hard-splits into ``batch_size`` slices, each its own
    batch."""
    frames: list[int] = []
    pieces: list[int] = []
    open_frames = open_pieces = 0
    for length in session_lens:
        if length > batch_size:
            if open_frames:
                frames.append(open_frames)
                pieces.append(open_pieces)
                open_frames = open_pieces = 0
            full, rest = divmod(length, batch_size)
            frames.extend([batch_size] * full)
            pieces.extend([1] * full)
            if rest:
                frames.append(rest)
                pieces.append(1)
            continue
        if open_frames and open_frames + length > batch_size:
            frames.append(open_frames)
            pieces.append(open_pieces)
            open_frames = open_pieces = 0
        open_frames += length
        open_pieces += 1
    if open_frames:
        frames.append(open_frames)
        pieces.append(open_pieces)
    return frames, pieces


@dataclass(frozen=True)                            # [FROZEN in CONTRACTS.md §7.9]
class RunSummary:
    counts: Mapping                                # same keys as report.json "counts" (§9.3)
    interrupted: bool
    exit_code: int                                 # 4 (circuit break) | 1 (strict + rejects) | 0
    wall_s: float
    output_lines: int
    rejects_lines: int


class Orchestrator:
    """Drives the whole run. Constructor signature frozen in CONTRACTS.md §7.9."""

    def __init__(self, cfg: ResolvedConfig, stages: list[Stage],
                 ingestor: Ingestor | None, emitter: Emitter, llm: LLMClient,
                 schema_engine: SchemaEngine, metrics: MetricsSink,
                 run_id: str, run_started_at: datetime):
        self.cfg = cfg
        self.stages = stages
        self.ingestor = ingestor
        self.emitter = emitter
        self.llm = llm
        self.schema_engine = schema_engine
        self.metrics = metrics
        self.run_id = run_id
        self.run_started_at = run_started_at

        # Run-level aggregation state (content-free, spec 3.10.3).
        self._stage_time: dict[str, float] = {}
        self._agg_hist = [0] * 10
        self._crit_sum: dict[str, float] = {}
        self._crit_n: dict[str, int] = {}
        # v1.7 R12: pool-dimensioned mirrors of the three accumulators above,
        # fed only when classify is enabled (pool = item.classification.label).
        self._pool_agg_hist: dict[str, list[int]] = {}
        self._pool_crit_sum: dict[str, dict[str, float]] = {}
        self._pool_crit_n: dict[str, dict[str, int]] = {}
        self._output_lines = 0
        self._rejects_lines = 0
        self._batch_no = 0
        self._pending: deque[list[PipelineItem]] = deque()  # generation re-flow queue

        # Control flow.
        self._stop = False
        self._interrupted = False
        self._circuit_broken = False
        self._current_task: asyncio.Task | None = None
        self._installed_signals: list[int] = []
        self._timer_handles: list[asyncio.TimerHandle] = []
        self._t0 = 0.0

    # ── public entry ───────────────────────────────────────────────────────

    async def run(self) -> RunSummary:
        self._t0 = time.perf_counter()
        if self.cfg.dry_run:
            return self._run_dry()

        if self.cfg.run.mode == "process" and self.ingestor is not None:
            # Fail fast on path/candidate/pairing errors BEFORE the first trace
            # emit — which is what opens (and truncates) the trace file — so a
            # dead-on-arrival run never destroys the previous run's trace
            # (E2E finding P2-4). Detach metrics so the rehearsal scan emits
            # nothing; the real records() pass re-emits everything in order.
            saved_metrics = getattr(self.ingestor, "metrics", None)
            self.ingestor.metrics = None
            try:
                # estimate=False: the fail-fast checks only — the text-modality
                # line count reads every input byte and its result is unused
                # here (that would double the input I/O of every run).
                self.ingestor.scan(estimate=False)
            finally:
                self.ingestor.metrics = saved_metrics

        self._install_signal_handlers()
        try:
            self.metrics.event(_EV_RUN_START, stage="run", batch_no=0,
                               payload={"tool_version": TOOL_VERSION,
                                        "config_digest": self.cfg.config_digest,
                                        "project_digest": self.cfg.project_digest,
                                        "trace_schema_version": 1})
            self.emitter.open()
            try:
                if self.cfg.run.mode == "generate_only":
                    await self._run_generate_only()
                else:
                    await self._run_process()
            except CircuitBreakerTripped:
                # Fatal-error streak >= run.fatal_error_threshold: remaining
                # work abandoned, report still written, .part NOT renamed.
                self._circuit_broken = True
        finally:
            self._remove_signal_handlers()
        return self._finalize()

    # ── mode drivers ───────────────────────────────────────────────────────

    async def _run_process(self) -> None:
        assert self.ingestor is not None, "process mode requires an Ingestor"
        if getattr(self.ingestor, "metrics", None) is None:
            self.ingestor.metrics = self.metrics  # trace wiring (CONTRACTS §7.1)
        if self.cfg.segment.enabled:
            # v1.8 stream mode: whole-session next-fit packing over the M2
            # session-stream view (generate is mutually exclusive with segment
            # per M1, so the re-flow queue can never fill here).
            await self._run_process_stream()
            return
        stream = iter(self.ingestor.records())
        if self.cfg.limit is not None:
            stream = islice(stream, self.cfg.limit)

        main_chain = self._compose_chain(include_generate=True)
        reflow_chain = self._compose_chain(include_generate=False)

        while not self._stop:
            if self._pending:
                # Generation sub-batches run right after their parent batch,
                # with consecutive batch numbers, and never re-generate.
                batch = self._pending.popleft()
                chain = reflow_chain
            else:
                records = list(islice(stream, self.cfg.run.batch_size))
                if not records:
                    break
                batch = [PipelineItem(record=r) for r in records]
                chain = main_chain
            self._batch_no += 1
            await self._guarded_batch(batch, self._batch_no, chain)
            del batch  # per-batch memory lifecycle: no reference survives emit

    async def _run_process_stream(self) -> None:
        """v1.8 stream batching (S21/S4, CONTRACTS §7.9): consume
        ``ingestor.sessions()`` (the --limit islice lives INSIDE M2, between
        parse stream and assembler — S17) and pack WHOLE sessions into batches
        by next-fit: exactly one open bin; a session that no longer fits closes
        the current batch and opens the next. Batch capacity = run.batch_size
        FRAMES. A session longer than batch_size is HARD-SPLIT into
        batch_size slices, each dispatched as its own batch, with ONE WARN per
        run and a duck-typed ``session_split`` mark on the split session's
        frame envelopes (M7's missing-frame downgrade evidence,
        ``_meta.stream.session_split``). M10 stamps ``PipelineItem.session_id``
        at envelope construction (S4); the residual open bin ships as-is once
        the session stream is exhausted. On SIGINT/SIGTERM no NEW batch is
        dispatched — buffered frames strand into the interrupted residual
        (S18)."""
        assert self.ingestor is not None, "process mode requires an Ingestor"
        chain = self._compose_chain(include_generate=True)
        bs = self.cfg.run.batch_size
        open_batch: list[PipelineItem] = []
        split_warned = False

        async def dispatch(batch: list[PipelineItem]) -> None:
            self._batch_no += 1
            await self._guarded_batch(batch, self._batch_no, chain)

        for sess in self.ingestor.sessions():
            if self._stop:
                break
            frames = [PipelineItem(record=r, session_id=sess.session_id)
                      for r in sess.records]
            if len(frames) > bs:
                # Hard split (S21): slice at batch_size, each slice its own
                # batch, dispatched in order. Mark every frame of the session.
                if not split_warned:
                    split_warned = True
                    _log.warning("会话超 batch_size 被硬切（本条提示仅打印一次）",
                                 extra={"stage": "run", "batch": self._batch_no})
                for item in frames:
                    item.session_split = True      # duck-typed mark (S21)
                if open_batch:
                    await dispatch(open_batch)
                    open_batch = []
                for i in range(0, len(frames), bs):
                    if self._stop:
                        break
                    await dispatch(frames[i:i + bs])
                continue
            if open_batch and len(open_batch) + len(frames) > bs:
                # The one pending overflow session — the only new cross-batch
                # survivor (§11 ⑤), released as soon as it is packed.
                await dispatch(open_batch)
                open_batch = []
            open_batch.extend(frames)
        if open_batch and not self._stop:
            await dispatch(open_batch)             # residual open bin ships as-is

    async def _run_generate_only(self) -> None:
        gen = next((s for s in self.stages if s.name == "generate"), None)
        if gen is None:
            raise InternalError("generate_only mode requires a generate stage")
        # Pre-draw PRNG fixed at batch_no=0 (spec 3.10.3): Random(f"{seed}:0:generate").
        ctx0 = self._make_ctx(0, "generate")
        # The generation phase runs as a guarded task — same pattern as
        # _guarded_batch — so a SIGINT/SIGTERM can stop it: _request_stop's
        # 30 s timer cancels `self._current_task` (spec 3.10.3 中断 row;
        # CONTRACTS §7.9 "wait current batch ≤ 30 s then cancel"). Its
        # wall-clock feeds report.timing.per_stage_s like any enabled stage.
        task = asyncio.ensure_future(gen.generate_all(ctx0))
        self._current_task = task
        t_gen = time.perf_counter()
        records: list[Record] = []
        try:
            records = list(await task)
        except asyncio.CancelledError:
            if not self._stop:
                raise                              # external cancellation, not ours
            # interrupted mid-generation: a cancelled generate_all yields no
            # records; finalize still runs normally (interrupted=true).
        finally:
            self._current_task = None
            elapsed = time.perf_counter() - t_gen
            self._stage_time["generate"] = self._stage_time.get("generate", 0.0) + elapsed
            self.metrics.add_stage_time("generate", elapsed)
        if self.cfg.limit is not None:
            records = records[: self.cfg.limit]  # generate_all already truncates; belt & braces
        if records:
            self.metrics.count("counts.generated", len(records))
        # 0 generated → loop is a no-op → normal finalize, exit 0.
        chain = self._compose_chain(include_generate=False)
        bs = self.cfg.run.batch_size
        for i in range(0, len(records), bs):
            if self._stop:
                break
            batch = [PipelineItem(record=r) for r in records[i:i + bs]]
            self._batch_no += 1
            await self._guarded_batch(batch, self._batch_no, chain)
            del batch

    # ── batch lifecycle ────────────────────────────────────────────────────

    async def _guarded_batch(self, batch: list[PipelineItem], batch_no: int,
                             chain: Sequence[Stage]) -> None:
        """Run one batch as a task so a SIGINT 30 s timeout can cancel it."""
        task = asyncio.ensure_future(self._process_batch(batch, batch_no, chain))
        self._current_task = task
        try:
            await task
        except asyncio.CancelledError:
            if not self._stop:
                raise                              # external cancellation, not ours
            # interrupted mid-batch: already-flushed lines stay valid
        finally:
            self._current_task = None

    async def _process_batch(self, batch: list[PipelineItem], batch_no: int,
                             chain: Sequence[Stage]) -> None:
        t_batch = time.perf_counter()
        self.metrics.event(_EV_BATCH_START, stage="run", batch_no=batch_no,
                           payload={"size": len(batch)})
        batch_fanout = 0
        batch_episodes = 0
        for stage in chain:
            ctx = self._make_ctx(batch_no, stage.name)
            size_before = len(batch)
            t_stage = time.perf_counter()
            try:
                result = await stage.run(batch, ctx)
            finally:
                elapsed = time.perf_counter() - t_stage
                self._stage_time[stage.name] = self._stage_time.get(stage.name, 0.0) + elapsed
                self.metrics.add_stage_time(stage.name, elapsed)
            if stage.name == "segment":
                # v1.8 §7.9: counts.episodes is METERED HERE — the len-delta
                # across the segment invocation (M14 tail-appends episode
                # envelopes in place and never touches counts.*; fanout-
                # isomorphic R9 construction).
                delta = len(batch) - size_before
                if delta > 0:
                    self.metrics.count("counts.episodes", delta)
                    batch_episodes += delta
            if stage.name == "classify":
                # v1.7 R9: counts.fanout is METERED HERE — the len-delta across
                # the classify invocation (M13 tail-appends siblings in place
                # and never touches counts.*; same construction as deriving
                # counts.generated from generate's return value).
                delta = len(batch) - size_before
                if delta > 0:
                    self.metrics.count("counts.fanout", delta)
                    batch_fanout += delta
            if stage.name == "generate":
                # Off-path stage: returns a NEW sub-batch; enqueue it split at
                # batch_size. Sub-batches re-enter at M3 (single round).
                sub = list(result) if result is not None else []
                if sub:
                    self.metrics.count("counts.generated", len(sub))
                    bs = self.cfg.run.batch_size
                    for i in range(0, len(sub), bs):
                        self._pending.append(sub[i:i + bs])

        if self.cfg.quality.enabled:
            self._collect_quality_stats(batch)

        emit = self.emitter.emit_batch(batch, batch_no)
        self._output_lines += emit.emitted
        self._rejects_lines += emit.rejected

        # Status tally (post-emit; emitter may have diverted internal errors).
        tally: dict[str, int] = {}
        for item in batch:
            tally[item.status] = tally.get(item.status, 0) + 1
        dropped_dup = tally.get("dropped_dup", 0)
        dropped_lowq = tally.get("dropped_lowq", 0)
        dropped_verify = tally.get("dropped_verify", 0)
        absorbed = tally.get("absorbed", 0)        # v1.8: episode members (third route)
        dropped_noise = tally.get("dropped_noise", 0)
        # counts invariant: whatever was neither emitted nor dropped is failed
        # (covers emitter-diverted internal_error items too). v1.8: without the
        # absorbed/dropped_noise terms, episode members would be miscounted
        # as failed (§7.9).
        failed = max(len(batch) - emit.emitted - dropped_dup - dropped_lowq
                     - dropped_verify - absorbed - dropped_noise, 0)
        self.metrics.count("counts.emitted", emit.emitted)
        self.metrics.count("counts.dropped_dup", dropped_dup)
        self.metrics.count("counts.dropped_lowq", dropped_lowq)
        self.metrics.count("counts.dropped_verify", dropped_verify)
        self.metrics.count("counts.absorbed", absorbed)
        self.metrics.count("counts.dropped_noise", dropped_noise)
        self.metrics.count("counts.failed", failed)

        end_payload: dict = {"active": tally.get("active", 0),
                             "dropped_dup": dropped_dup,
                             "dropped_lowq": dropped_lowq,
                             "dropped_verify": dropped_verify,
                             "failed": tally.get("failed", 0),
                             "duration_ms": int((time.perf_counter() - t_batch) * 1000)}
        if self.cfg.classify.enabled:
            # v1.7 R20 (§8.1): batch.start.size stays the batch-ENTRY envelope
            # count; batch.end carries the fan-out delta (classify enabled only).
            end_payload["fanout"] = batch_fanout
        if self.cfg.segment.enabled:
            # v1.8 (same R20 form): carried only when segment is enabled; the
            # stderr progress/summary line gains NO new keys (§7.9).
            end_payload["episodes"] = batch_episodes
            end_payload["absorbed"] = absorbed
            end_payload["dropped_noise"] = dropped_noise
        self.metrics.event(_EV_BATCH_END, stage="run", batch_no=batch_no,
                           payload=end_payload)
        self.metrics.flush()                       # trace flush follows output flush

    def _make_ctx(self, batch_no: int, stage_name: str) -> RunContext:
        """Fresh RunContext per (batch, stage); rng derivation frozen (spec 3.10.3)."""
        return RunContext(cfg=self.cfg, llm=self.llm, schema_engine=self.schema_engine,
                          metrics=self.metrics,
                          rng=random.Random(f"{self.cfg.run.seed}:{batch_no}:{stage_name}"),
                          batch_no=batch_no)

    def _compose_chain(self, include_generate: bool) -> list[Stage]:
        """Stage composition per the 2.3.1 switch matrix, canonical order.

        The CLI hands in the constructed enabled stages; composition here
        re-orders them canonically and drops anything the config disables.
        `generate` only ever runs on main process-mode batches (never on
        re-flow sub-batches, never in generate_only where it is the chain head).
        `classify` is included in the main, re-flow AND generate_only chains
        (v1.7, §7.9) — already-classified items rely on M13's idempotent skip.
        """
        cfg = self.cfg
        enabled = {
            "segment": cfg.segment.enabled,
            "dedup": cfg.dedup.enabled,
            "classify": cfg.classify.enabled,
            "extract": cfg.extract.enabled,
            "quality": cfg.quality.enabled,
            "generate": (cfg.generate.enabled and include_generate
                         and cfg.run.mode == "process"),
            "annotate": cfg.annotate.enabled,
            "verify": cfg.verify.enabled,
        }
        by_name = {s.name: s for s in self.stages}
        return [by_name[n] for n in _CHAIN_ORDER if enabled[n] and n in by_name]

    # ── run-level stats aggregation ────────────────────────────────────────

    def _collect_quality_stats(self, batch: list[PipelineItem]) -> None:
        """Aggregate quality scores into the report histogram/means (M10 owns
        report assembly; only counts, never data content). v1.7 R12: with
        classify enabled the accumulators additionally split by pool
        (= item.classification.label); classify disabled is byte-identical
        to the flat v1.6 path."""
        classify_on = self.cfg.classify.enabled
        for item in batch:
            pool = (item.classification.label
                    if classify_on and item.classification is not None else None)
            agg = item.scores.get("__aggregate__")
            if agg is not None and agg.score is not None:
                bucket = min(int(agg.score * 10), 9)
                self._agg_hist[bucket] += 1
                if pool is not None:
                    self._pool_agg_hist.setdefault(pool, [0] * 10)[bucket] += 1
            for key, qs in item.scores.items():
                if key == "__aggregate__" or qs.score is None:
                    continue
                self._crit_sum[key] = self._crit_sum.get(key, 0.0) + qs.score
                self._crit_n[key] = self._crit_n.get(key, 0) + 1
                if pool is not None:
                    psum = self._pool_crit_sum.setdefault(pool, {})
                    pn = self._pool_crit_n.setdefault(pool, {})
                    psum[key] = psum.get(key, 0.0) + qs.score
                    pn[key] = pn.get(key, 0) + 1

    # ── finalize / report ──────────────────────────────────────────────────

    def _finalize(self) -> RunSummary:
        wall_s = time.perf_counter() - self._t0
        # Source of truth is the MetricsSink flag: the breaker can open on the
        # tail calls of a batch without CircuitBreakerTripped ever escaping a
        # stage (every in-flight call fails record-level first) — the run must
        # still end 4 / undelivered.
        self._circuit_broken = (self._circuit_broken
                                or bool(getattr(self.metrics, "circuit_broken", False)))
        if self._circuit_broken:
            exit_code = 4
        elif self.cfg.strict and self._rejects_lines > 0:
            exit_code = 1
        else:
            exit_code = 0
        report = self._build_report(exit_code=exit_code, wall_s=wall_s)
        # v1.6 熔断交付 (spec 3.10.3, stakeholder decision 1.6 ②): a circuit
        # break ALSO delivers the completed batches — fsync + atomic rename,
        # report marked run.partial_delivery=true with counts.unprocessed as
        # the balancing residual. deliver=True here is unconditional; the
        # emitter still refuses to rename after a channel-write failure
        # (_undeliverable), and dry-run passes deliver=False elsewhere.
        self.emitter.finalize(report, deliver=True)
        self.metrics.event(_EV_RUN_END, stage="run", batch_no=0,
                           payload={"counts": report["counts"], "exit_code": exit_code})
        self.metrics.flush()
        return RunSummary(counts=report["counts"], interrupted=self._interrupted,
                          exit_code=exit_code, wall_s=wall_s,
                          output_lines=self._output_lines,
                          rejects_lines=self._rejects_lines)

    def _build_report(self, exit_code: int, wall_s: float) -> dict:
        """Assemble the §9.3 report dict from ingestor.report, metrics counters,
        schema_engine.stats, llm.usage_by_profile and stage timing."""
        cfg = self.cfg
        counters = dict(getattr(self.metrics, "counters", {}) or {})

        def c(key: str) -> int:
            return int(counters.get(key, 0))

        ingest_report = getattr(self.ingestor, "report", None) if self.ingestor else None
        counts = {
            "scanned": int(getattr(ingest_report, "scanned", 0)),
            "ingested": int(getattr(ingest_report, "ingested", 0)),
            "bad_input": int(getattr(ingest_report, "bad_input", 0)),
            "dropped_dup": c("counts.dropped_dup"),
            "dropped_lowq": c("counts.dropped_lowq"),
            "dropped_verify": c("counts.dropped_verify"),
            "failed": c("counts.failed"),
            "generated": c("counts.generated"),
            "emitted": c("counts.emitted"),
        }
        if cfg.classify.enabled and cfg.classify.assignment == "multi":
            # v1.7 R9/R10: the fanout key appears only under multi assignment
            # (§9.3) — single assignment never fans out; the counter is fed by
            # the _process_batch len-delta metering (M10-owned).
            counts["fanout"] = c("counts.fanout")
        if cfg.segment.enabled:
            # v1.8 只增 (§9.3): the three stream counts appear only when
            # segment is enabled — episodes from the len-delta metering,
            # absorbed/dropped_noise from the post-emit tallies (all M10-owned).
            counts["episodes"] = c("counts.episodes")
            counts["absorbed"] = c("counts.absorbed")
            counts["dropped_noise"] = c("counts.dropped_noise")

        run_block: dict = {
            "tool_version": __version__,
            "started_at": self.run_started_at.isoformat(),
            "finished_at": datetime.now().astimezone().isoformat(),
            "interrupted": self._interrupted,
            # Explicit breaker flag (E2E finding P4-10): interrupted stays
            # false on a circuit break — exit_code=4 alone was easy to misread.
            "circuit_broken": self._circuit_broken,
            "exit_code": exit_code,
            "modality": cfg.run.modality,
            "seed": cfg.run.seed,
            "config_digest": cfg.config_digest,
            "project_digest": cfg.project_digest,
        }
        if self._circuit_broken:
            # v1.6 熔断交付 (spec 6.4, 只增): partial_delivery present only on
            # breaker-trip delivery.
            run_block["partial_delivery"] = True
        if self._circuit_broken or (cfg.segment.enabled and self._interrupted):
            # counts.unprocessed = balancing residual so the invariant extends
            # to emitted + dropped_* + failed + bad_input + unprocessed =
            # scanned + generated [+ fanout] [+ episodes] (records that entered
            # the pipeline but reached no terminal count, incl. generated-but-
            # never-batched records in generate_only; the fanout term is v1.7
            # R10 — fanned-out siblings are envelopes too). v1.8 (S18): in
            # STREAM MODE the key also appears on interrupted runs (SIGINT over
            # the session buffer strands in-flight records) with the expanded
            # sides — + episodes on the source side, + absorbed + dropped_noise
            # among the terminal counts. Non-stream interrupted runs keep a
            # provably zero residual and never emit the key (regression anchor).
            residual = (counts["scanned"] + counts["generated"]
                        + counts.get("fanout", 0)
                        + counts.get("episodes", 0)
                        - counts["emitted"] - counts["dropped_dup"]
                        - counts["dropped_lowq"] - counts["dropped_verify"]
                        - counts["failed"] - counts["bad_input"]
                        - counts.get("absorbed", 0)
                        - counts.get("dropped_noise", 0))
            counts["unprocessed"] = max(0, residual)
        report: dict = {
            "run": run_block,
            "counts": counts,
        }

        if cfg.segment.enabled:
            # v1.8 stream block (§9.3/spec §6.4: placed right after counts).
            # sessions data source = IngestReport (M2 owner, §7.1);
            # below_min_len / digest_poor_frames / segment_failures surface the
            # M14 counters; the sub-blocks surface the M15/M7 counters over the
            # closed vocabularies (zero-based like report.classify.classes).
            episodes = counts["episodes"]
            absorbed = counts["absorbed"]
            stream_block: dict = {
                "sessions": int(getattr(ingest_report, "sessions", 0)),
                "episodes": episodes,
                "mean_episode_len": (round(absorbed / episodes, 2)
                                     if episodes else 0.0),
                "absorbed": absorbed,
                "dropped_noise": counts["dropped_noise"],
                "below_min_len": c("segment.below_min_len"),
                "digest_poor_frames": c("segment.digest_poor_frames"),
                "segment_failures": c("segment.failures"),
            }
            if cfg.extract.enabled:
                stream_block["extract"] = {
                    "transitions": c("extract.transitions"),
                    "fallback_steps": c("extract.fallback_steps"),
                    "failures": c("extract.failures"),
                    "by_type": {t: c(f"extract.by_type.{t}")
                                for t in _ACTION_TYPES},
                }
            if cfg.verify.enabled:
                stream_block["verify"] = {
                    "membership_repairs": c("verify.membership_repairs"),
                    "boundary_flags": c("verify.boundary_flags"),
                    "defects": {k: c(f"verify.defects.{k}")
                                for k in _DEFECT_KINDS},
                }
            report["stream"] = stream_block

        if cfg.dedup.enabled:
            dedup_block = {
                "exact": c("dedup.exact"),
                "near_text": c("dedup.near_text"),
                "near_image": c("dedup.near_image"),
                "near_both": c("dedup.near_both"),
                "clusters": c("dedup.clusters"),
                "image_decode_failures": c("dedup.image_decode_failures"),
            }
            if cfg.dedup.semantic:
                dedup_block["near_semantic"] = c("dedup.near_semantic")
                dedup_block["embedding_failures"] = c("dedup.embedding_failures")
            report["dedup"] = dedup_block

        if cfg.quality.enabled:
            # Top-level mode/rounds keep the globally-inherited base values
            # even under per-class overrides (v1.7 R14); by_class carries each
            # pool's effective values.
            report["quality"] = {
                "mode": "pairwise_bt" if cfg.quality.mode == "pairwise" else "pointwise",
                "rounds": cfg.quality.rounds,
                "judgment_failures": c("quality.judgment_failures"),
                "aggregate_histogram": {label: self._agg_hist[i]
                                        for i, label in enumerate(_HIST_LABELS)},
                "per_criterion_mean": {key: self._crit_sum[key] / self._crit_n[key]
                                       for key in sorted(self._crit_sum)
                                       if self._crit_n.get(key)},
            }
            # v1.7 R12: with classify enabled the tie counters are
            # pool-dimensioned (quality.tie_outcomes.<pool>.<crit>); parse them
            # once for both the per-pool rates and the cross-pool aggregate.
            prefix = "quality.tie_outcomes."
            tie_rate: dict[str, float] = {}
            pool_tie_rate: dict[str, dict[str, float]] = {}
            if cfg.classify.enabled:
                crit_ties: dict[str, int] = {}
                crit_comps: dict[str, int] = {}
                for key, ties in sorted(self.metrics.counters.items()):
                    if not key.startswith(prefix):
                        continue
                    rest = key[len(prefix):]
                    if "." not in rest:            # malformed / flat key: skip
                        continue
                    pool, _, crit = rest.partition(".")
                    comps = self.metrics.counters.get(
                        f"quality.tie_comparisons.{pool}.{crit}", 0)
                    if comps:
                        pool_tie_rate.setdefault(pool, {})[crit] = ties / comps
                        crit_ties[crit] = crit_ties.get(crit, 0) + ties
                        crit_comps[crit] = crit_comps.get(crit, 0) + comps
                tie_rate = {crit: crit_ties[crit] / crit_comps[crit]
                            for crit in sorted(crit_ties) if crit_comps.get(crit)}
            else:
                for key, ties in sorted(self.metrics.counters.items()):
                    if not key.startswith(prefix):
                        continue
                    crit = key[len(prefix):]
                    comps = self.metrics.counters.get(
                        f"quality.tie_comparisons.{crit}", 0)
                    if comps:
                        tie_rate[crit] = ties / comps
            # Tie-rate emission gate (v1.7 R14): global pairwise, or — classify
            # enabled — at least one pairwise pool exists. Pairwise percentile
            # means are ~0.5 by construction; the tie rate is the
            # discriminative per-criterion signal (E2E P4-9).
            any_pairwise_pool = cfg.classify.enabled and any(
                view.quality.mode == "pairwise" for view in cfg.class_views.values())
            if cfg.quality.mode == "pairwise" or any_pairwise_pool:
                report["quality"]["per_criterion_tie_rate"] = tie_rate
            if cfg.classify.enabled:
                # v1.7 R12/R14 quality.by_class: one entry per DECLARED class
                # (zero-count based, like report.classify.classes); mode/rounds
                # are the pool's EFFECTIVE values from cfg.class_views.
                by_class: dict[str, dict] = {}
                for pool in sorted(cfg.class_views):
                    view_q = cfg.class_views[pool].quality
                    hist = self._pool_agg_hist.get(pool, [0] * 10)
                    psum = self._pool_crit_sum.get(pool, {})
                    pn = self._pool_crit_n.get(pool, {})
                    by_class[pool] = {
                        "mode": ("pairwise_bt" if view_q.mode == "pairwise"
                                 else "pointwise"),
                        "rounds": view_q.rounds,
                        "aggregate_histogram": {label: hist[i]
                                                for i, label in enumerate(_HIST_LABELS)},
                        "per_criterion_mean": {key: psum[key] / pn[key]
                                               for key in sorted(psum)
                                               if pn.get(key)},
                        "per_criterion_tie_rate": pool_tie_rate.get(pool, {}),
                    }
                report["quality"]["by_class"] = by_class

        stats = getattr(self.schema_engine, "stats", None) if self.schema_engine else None
        report["schema_engine"] = {"resolved_at": dict(stats) if stats else dict(_SCHEMA_STATS_ZERO)}

        if cfg.annotate.enabled and cfg.annotate.self_consistency >= 3:
            report["annotate"] = {"sc_disagreements": c("annotate.sc_disagreements")}

        if cfg.generate.enabled:
            buckets: dict[str, dict] = {}
            prefix = "generate.buckets."
            # rejected_by_validator joined the whitelist (bug fix, spec v1.7 §6:
            # M6 counts it since v1.5 but the report parse silently dropped it);
            # zero-init keeps the three always-present fields — the fourth is
            # written only when its counter appears (validator configured).
            for key, value in counters.items():
                if not key.startswith(prefix):
                    continue
                bucket, _, field_name = key[len(prefix):].rpartition(".")
                if not bucket or field_name not in ("calls", "produced", "survived_dedup",
                                                    "rejected_by_validator"):
                    continue
                buckets.setdefault(bucket, {"calls": 0, "produced": 0,
                                            "survived_dedup": 0})[field_name] = int(value)
            report["generate"] = {"buckets": buckets}

        if cfg.classify.enabled:
            # v1.7 §9.3 classify block: the classes histogram is zero-based
            # over ALL declared classes (declaration order); counters are
            # M13-owned (classify.fallback surfaces as fallback_count).
            classify_block: dict = {
                "assignment": cfg.classify.assignment,
                "classes": {spec.name: c(f"classify.classes.{spec.name}")
                            for spec in cfg.classify.classes},
                "fallback_count": c("classify.fallback"),
                "failures": c("classify.failures"),
            }
            if cfg.classify.assignment == "multi":
                classify_block["multi_label_records"] = c("classify.multi_label_records")
            report["classify"] = classify_block

        event_log = (getattr(self.metrics, "event_log", None)
                     or getattr(self.metrics, "_event_log", None))
        trace_events = int(getattr(event_log, "events_written", 0) or 0)
        trace_dropped = int(getattr(event_log, "dropped_events", 0) or 0)
        if cfg.trace.enabled:
            # The terminal run.end event is emitted only after this report is
            # assembled (its payload carries the report counts, and §8.1 makes
            # it the trace's last line, written after finalize). Account for it
            # here so report.trace matches the final trace file: one more
            # written line while the channel is open, one more dropped event
            # once a write failure closed it.
            if getattr(event_log, "closed", False):
                trace_dropped += 1
            else:
                trace_events += 1
        report["trace"] = {
            "enabled": cfg.trace.enabled,
            # The EventLog may be writing to a diverted path (dry-run uses
            # "<name>.dryrun<suffix>", P2-4) — report the ACTUAL file.
            "path": (getattr(getattr(event_log, "cfg", None), "path", None)
                     or cfg.trace.path),
            "events": trace_events,
            "dropped_events": trace_dropped,
        }

        usage_by_profile = getattr(self.llm, "usage_by_profile", None) if self.llm else None
        llm_usage: dict[str, dict] = {}
        for name, usage in (usage_by_profile or {}).items():
            # v1.6 key pool (spec 6.4, 只增): keys sub-object only for pools > 1
            # (M9 pre-seeds every member, so len == pool size; key identity =
            # env-var NAME, decision 1.6 ⑤); parked stats for pools > 1 or
            # whenever nonzero — single-key parking must leave report evidence.
            key_usages = getattr(usage, "keys", None) or {}
            parked_calls = getattr(usage, "parked_calls", 0)
            parked_ms = getattr(usage, "parked_ms", 0)
            emit_keys = len(key_usages) > 1
            emit_parked = emit_keys or bool(parked_calls) or bool(parked_ms)
            if (usage.calls == 0 and usage.retries == 0
                    and usage.prompt_tokens == 0 and usage.completion_tokens == 0
                    and usage.est_cost_usd is None
                    and not emit_keys and not emit_parked):
                # Zero-activity profile (e.g. its only call was breaker-aborted
                # before any attempt): keep the v1.5 report shape — omit.
                continue
            entry = {
                "calls": usage.calls,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "retries": usage.retries,
            }
            if usage.est_cost_usd is not None:
                entry["est_cost_usd"] = usage.est_cost_usd
            if emit_keys:
                entry["keys"] = {
                    env: {"calls": ku.calls, "rate_limited": ku.rate_limited,
                          "disabled": ku.disabled}
                    for env, ku in sorted(key_usages.items())
                }
            if emit_parked:
                entry["parked_calls"] = parked_calls
                entry["parked_ms"] = parked_ms
            llm_usage[name] = entry
        report["llm_usage"] = llm_usage

        report["timing"] = {
            "wall_s": round(wall_s, 3),
            "per_stage_s": {name: round(seconds, 3)
                            for name, seconds in self._stage_time.items()},
        }
        return report

    # ── dry-run ────────────────────────────────────────────────────────────

    def _run_dry(self) -> RunSummary:
        """--dry-run: M1 already passed; run the M2 scan (process mode) or the
        3.6.2 static call-count formula (generate_only), print the call/cost
        estimate to stderr, write the report, make NO LLM calls, produce NO
        main output/rejects (Emitter.open is never called). The trace channel
        — an opt-in first-class output channel (spec 2.6) that carries only
        operational events, never data content — still receives its run.start
        / run.end lifecycle events when trace.enabled."""
        cfg = self.cfg
        self.metrics.event(_EV_RUN_START, stage="run", batch_no=0,
                           payload={"tool_version": TOOL_VERSION,
                                    "config_digest": cfg.config_digest,
                                    "project_digest": cfg.project_digest,
                                    "trace_schema_version": 1})
        est = self._estimate()
        print(f"dry-run: mode={cfg.run.mode} estimated_records={est['records']} "
              f"batches={est['batches']}", file=sys.stderr)
        print(f"dry-run: estimated LLM calls — generate_calls={est['generate_calls']} "
              f"segment_calls={est['segment_calls']} "
              f"classify_calls={est['classify_calls']} "
              f"extract_calls={est['extract_calls']} "
              f"quality_calls={est['quality_calls']} annotate_calls={est['annotate_calls']} "
              f"verify_calls={est['verify_calls']} total={est['total_calls']} "
              f"(excludes retries and repair calls)", file=sys.stderr)
        if cfg.classify.enabled and (cfg.classify.assignment == "multi"
                                     or self._class_overrides_exist()):
            # v1.7 R28: per-class overrides make the static estimate inexact,
            # and multi fan-out multiplies downstream calls by the (unknowable)
            # label count — flag both with the fixed wording.
            print("dry-run: 注：按全局配置估算 / multi 按标签乘数 1 报下界",
                  file=sys.stderr)
        if cfg.segment.enabled and cfg.segment.strategy in ("llm", "hybrid"):
            # v1.8 S22 (R28-style note): downstream estimates use
            # episodes ≈ sessions — LLM boundary refinement can only ADD
            # segments, so the numbers are a lower bound.
            print("dry-run: 注：stream 估算：下游按 episodes≈sessions 报下界"
                  "（LLM 精化只增段数）", file=sys.stderr)
        side_channels = "report and trace only" if cfg.trace.enabled else "report only"
        print(f"dry-run: no LLM calls made, no output written ({side_channels})",
              file=sys.stderr)

        wall_s = time.perf_counter() - self._t0
        report = self._build_report(exit_code=0, wall_s=wall_s)
        self.emitter.finalize(report, deliver=False)   # report only; no .part exists
        self.metrics.event(_EV_RUN_END, stage="run", batch_no=0,
                           payload={"counts": report["counts"], "exit_code": 0})
        self.metrics.flush()
        return RunSummary(counts=report["counts"], interrupted=False, exit_code=0,
                          wall_s=wall_s, output_lines=0, rejects_lines=0)

    def _class_overrides_exist(self) -> bool:
        """True when at least one [class.*] override diverges from the global
        sections (class_views holds one merged view per DECLARED class, so
        non-emptiness alone says nothing — compare against the global base)."""
        cfg = self.cfg
        return any(view.quality != cfg.quality or view.rubric != cfg.rubric
                   or view.annotate != cfg.annotate or view.generate != cfg.generate
                   or view.verify != cfg.verify or view.extract != cfg.extract
                   for view in cfg.class_views.values())

    def _estimate(self) -> dict:
        """Static record / LLM-call estimate. generate_only follows the 3.6.2
        call-count formulas; process mode uses the M2 scan estimate. All
        estimates assume no drops (upper bound) and exclude retries/repairs.
        v1.7 R11: classify_calls = ingested × max(1, sc) in process mode
        (re-flow sub-batches inherit their classification and skip M13),
        <generated records> × max(1, sc) in generate_only.
        v1.8 S22 (stream mode): the session table comes from
        ``plan.session_lens``; ``segment_calls = Σ ceil((L−1)/(window−1))``
        over sessions of length L ≥ 2 (L = 1 or strategy="rules" counts 0);
        ``extract_calls = Σ (L−1)`` (extract enabled; UPPER bound); the
        classify/quality/annotate/verify record base is ``len(session_lens)``
        (episodes ≈ sessions, LOWER bound — stderr note in _run_dry); the batch
        count comes EXACTLY from a next-fit packing simulation of the session
        sizes, and the pairwise-quality per-batch pool sizes are the packed
        sessions per batch. The non-stream branch is unchanged."""
        cfg = self.cfg
        g = cfg.generate
        if cfg.run.mode == "generate_only":
            n_ingested = 0
            plan = None
            if g.seed_examples:
                gen_calls = _ceil_div(len(g.seed_examples) * g.num_per_record, g.num_per_call)
            else:
                gen_calls = _ceil_div(g.standalone_count or 0, g.num_per_call)
            if cfg.limit is not None:
                gen_calls = min(gen_calls, _ceil_div(cfg.limit, g.num_per_call))
            gen_records = gen_calls * g.num_per_call
            if cfg.limit is not None:
                gen_records = min(gen_records, cfg.limit)
        else:
            assert self.ingestor is not None, "process mode requires an Ingestor"
            plan = self.ingestor.scan()
            n_ingested = plan.estimated_records
            if cfg.limit is not None:
                n_ingested = min(n_ingested, cfg.limit)
            if g.enabled:
                gen_calls = _ceil_div(n_ingested * g.num_per_record, g.num_per_call)
                gen_records = gen_calls * g.num_per_call
            else:
                gen_calls = gen_records = 0

        total_records = n_ingested + gen_records
        bs = cfg.run.batch_size
        sizes = [bs] * (total_records // bs)
        if total_records % bs:
            sizes.append(total_records % bs)

        # v1.8 stream mode (segment × generate_only is M1-forbidden, so plan
        # is always the process-mode scan here): sessions → exact batches,
        # episodes ≈ sessions as the downstream record base.
        segment_calls = 0
        extract_calls = 0
        stream = cfg.segment.enabled and cfg.run.mode == "process"
        if stream:
            session_lens = tuple(getattr(plan, "session_lens", ()) or ())
            frame_sizes, piece_sizes = _pack_next_fit(session_lens, bs)
            n_batches = len(frame_sizes)
            sizes = piece_sizes                    # pairwise pools are episodes
            downstream_base = len(session_lens)
            if cfg.segment.strategy in ("llm", "hybrid"):
                w = cfg.segment.window
                segment_calls = sum(_ceil_div(length - 1, w - 1)
                                    for length in session_lens if length >= 2)
            if cfg.extract.enabled:
                extract_calls = sum(length - 1 for length in session_lens)
        else:
            n_batches = len(sizes)
            downstream_base = total_records

        classify_calls = 0
        if cfg.classify.enabled:
            sc_c = cfg.classify.self_consistency
            if cfg.run.mode == "generate_only":
                base = gen_records
            else:
                base = downstream_base if stream else n_ingested
            classify_calls = base * max(1, sc_c)

        quality_calls = 0
        if cfg.quality.enabled:
            n_criteria = len(cfg.rubric.criteria)
            if cfg.quality.mode == "pairwise":
                judges = max(1, len(cfg.quality.judges))
                orders = 2 if cfg.quality.both_orders else 1
                per_call = n_criteria if cfg.quality.criteria_per_call == "single" else 1
                quality_calls = (sum(cfg.quality.rounds * (b // 2) for b in sizes)
                                 * per_call * judges * orders)
            else:
                quality_calls = downstream_base * n_criteria

        annotate_calls = 0
        if cfg.annotate.enabled:
            sc = cfg.annotate.self_consistency
            annotate_calls = downstream_base * (sc if sc >= 3 else 1)

        verify_calls = 0
        if cfg.verify.enabled:
            verify_calls = downstream_base * max(1, len(cfg.verify.judges))

        return {
            "records": total_records,
            "batches": n_batches,
            "generate_calls": gen_calls,
            "segment_calls": segment_calls,
            "classify_calls": classify_calls,
            "extract_calls": extract_calls,
            "quality_calls": quality_calls,
            "annotate_calls": annotate_calls,
            "verify_calls": verify_calls,
            "total_calls": (gen_calls + segment_calls + classify_calls
                            + extract_calls + quality_calls
                            + annotate_calls + verify_calls),
        }

    # ── signals ────────────────────────────────────────────────────────────

    def _install_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for sig in (_signal.SIGINT, _signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_stop)
                self._installed_signals.append(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass

    def _remove_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for sig in self._installed_signals:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        self._installed_signals.clear()
        for handle in self._timer_handles:
            handle.cancel()
        self._timer_handles.clear()

    def _request_stop(self) -> None:
        """SIGINT/SIGTERM: stop taking new batches; give the in-flight batch
        30 s before cancelling it. Finalize still runs normally (rename happens,
        report carries interrupted=true)."""
        self._stop = True
        self._interrupted = True
        task = self._current_task
        if task is not None and not task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._timer_handles.append(loop.call_later(30.0, task.cancel))

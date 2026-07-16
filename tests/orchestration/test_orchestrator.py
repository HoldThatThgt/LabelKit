"""M10 orchestrator offline tests (CONTRACTS.md §7.9, spec 3.10).

No mock LLMs anywhere. The stages used here are tiny REAL Stage
implementations of pure logic (exact-text dedup, deterministic failure,
deterministic sample generation) — unit fixtures for orchestration scheduling,
not fake model clients. Configurations keep quality/annotate/verify pointed at
pure stages or disabled so no LLM is ever needed. Test doubles for the
observability (MetricsSink) and output (Emitter) surfaces implement the real
contract semantics (fatal-streak breaker, .part + rename, report file).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from labelkit.common.config.model import (
    AnnotateConfig, ClassifyConfig, ClassSpec, ClassView, Criterion, DedupConfig,
    ExtractConfig, GenerateConfig, InputConfig, OutputConfig, QualityConfig,
    ResolvedConfig, Rubric, RunConfig, SegmentConfig, StreamConfig, ToolConfig,
    TraceConfig, VerifyConfig,
)
from labelkit.common.errors import CircuitBreakerTripped
from labelkit.common.observability.obslog import EventLog, MetricsSink, TraceEvent
from labelkit.orchestration.orchestrator import Orchestrator, RunSummary
from labelkit.common.contracts.types import (
    Classification, DedupInfo, PipelineItem, QualityScore, Record, RecordRef,
    StageError,
)

RUN_ID = "abcdef012345"


# ── helpers: real Records / ResolvedConfig built directly ───────────────────

def rec(i: int, text: str | None = None) -> Record:
    t = text if text is not None else f"sample text {i}"
    return Record(id=f"{i:016x}", modality="text", text=t, raw={"text": t},
                  ui_tree=None, image=None,
                  ref=RecordRef(source_file="in.jsonl", line_no=i,
                                pair_index=None, generated_from=()))


def gen_rec(i: int, seed_ids: tuple[str, ...] = ()) -> Record:
    t = f"generated text {i}"
    return Record(id=f"{i + 10_000:016x}", modality="text", text=t, raw={"text": t},
                  ui_tree=None, image=None,
                  ref=RecordRef(source_file="", line_no=None, pair_index=None,
                                generated_from=seed_ids,
                                generator={"llm": "default", "style": None}))


def make_cfg(tmp_path: Path, *, mode: str = "process", batch_size: int = 4,
             seed: int = 0, limit: int | None = None, strict: bool = False,
             dry_run: bool = False, fatal_threshold: int = 20,
             dedup: bool = True, quality: bool = False, annotate: bool = False,
             verify: bool = False, generate: GenerateConfig | None = None,
             quality_cfg: QualityConfig | None = None,
             classify: ClassifyConfig | None = None,
             segment: SegmentConfig | None = None,
             extract: ExtractConfig | None = None,
             trace: TraceConfig | None = None) -> ResolvedConfig:
    return ResolvedConfig(
        tool=ToolConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output=str(tmp_path / "out.jsonl"), modality="text",
                      input=None if mode == "generate_only" else str(tmp_path / "in"),
                      mode=mode, batch_size=batch_size, seed=seed,
                      fatal_error_threshold=fatal_threshold),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(enabled=dedup),
        segment=segment if segment is not None else SegmentConfig(),
        extract=extract if extract is not None else ExtractConfig(),
        classify=classify if classify is not None else ClassifyConfig(),
        quality=quality_cfg if quality_cfg is not None else QualityConfig(enabled=quality),
        generate=generate if generate is not None else GenerateConfig(),
        annotate=AnnotateConfig(enabled=annotate, instruction="标注" if annotate else ""),
        verify=VerifyConfig(enabled=verify),
        output=OutputConfig(schema_inline="{}"),
        trace=trace if trace is not None else TraceConfig(),
        rubric=Rubric(name="t", criteria=(
            Criterion(key="clarity", description="d", pairwise_prompt="p"),
            Criterion(key="usefulness", description="d", pairwise_prompt="p"),
        )),
        class_views={},
        user_schema={"type": "object"},
        limit=limit,
        strict=strict,
        dry_run=dry_run,
        config_path="config.toml",
        project_path="project.toml",
        config_digest="sha256:c",
        project_digest="sha256:p",
    )


def classify_cfg(assignment: str = "single", classes: tuple[str, ...] = ("faq", "chat"),
                 sc: int = 0) -> ClassifyConfig:
    """Enabled ClassifyConfig with a declared class table (M1-shaped: max_labels
    backfilled under multi, fallback = last declared class)."""
    specs = tuple(ClassSpec(name=n, description="d") for n in classes)
    return ClassifyConfig(enabled=True, assignment=assignment,
                          max_labels=len(specs) if assignment == "multi" else None,
                          fallback_class=classes[-1], self_consistency=sc,
                          classes=specs)


def with_views(cfg: ResolvedConfig, overrides: dict[str, dict] | None = None) -> ResolvedConfig:
    """Attach class_views like M1 does: one merged view PER DECLARED CLASS
    (zero-override classes mirror the global sections); `overrides` swaps
    individual ClassView fields per class name."""
    views = {}
    for spec in cfg.classify.classes:
        kw = dict(quality=cfg.quality, rubric=cfg.rubric, annotate=cfg.annotate,
                  generate=cfg.generate, verify=cfg.verify, extract=cfg.extract)
        kw.update((overrides or {}).get(spec.name, {}))
        views[spec.name] = ClassView(name=spec.name, **kw)
    return replace(cfg, class_views=views)


# ── contract-shaped test doubles (observability / IO, not LLMs) ─────────────

class FakeMetrics:
    """MetricsSink stand-in with the real fatal-streak breaker semantics."""

    def __init__(self, threshold: int = 20):
        self.counters: dict[str, int] = {}
        self.events: list[tuple] = []          # (ev, stage, batch_no, record_ids, payload)
        self.stage_times: dict[str, float] = {}
        self.flushes = 0
        self._threshold = threshold
        self._fatal_streak = 0
        self.event_log = SimpleNamespace(events_written=0, dropped_events=0)

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None):
        self.events.append((ev, stage, batch_no, tuple(record_ids), dict(payload or {})))
        self.event_log.events_written += 1

    def count(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n

    def add_stage_time(self, stage, seconds):
        self.stage_times[stage] = self.stage_times.get(stage, 0.0) + seconds

    def record_provider_result(self, fatal):
        self._fatal_streak = self._fatal_streak + 1 if fatal else 0

    @property
    def circuit_broken(self):
        return self._fatal_streak >= self._threshold

    def flush(self):
        self.flushes += 1


class FakeEmitResult(SimpleNamespace):
    pass


class FakeEmitter:
    """Emitter stand-in with real file semantics: .part + rename, report file."""

    def __init__(self, cfg: ResolvedConfig):
        self.output = Path(cfg.run.output)
        self.part = Path(str(self.output) + ".part")
        stem = str(self.output)[: -len(self.output.suffix)] if self.output.suffix else str(self.output)
        self.report_path = Path(stem + ".report.json")
        self.opened = False
        self.batches: list[tuple[int, int, int]] = []
        self.report = None
        self.deliver = None

    def open(self):
        self.opened = True
        self.part.write_text("", encoding="utf-8")

    def emit_batch(self, batch, batch_no):
        emitted = rejected = 0
        with self.part.open("a", encoding="utf-8") as fh:
            for item in batch:
                if item.status == "active":
                    fh.write(json.dumps(item.record.raw, ensure_ascii=False) + "\n")
                    emitted += 1
                elif item.status == "absorbed":
                    continue                       # v1.8 third route: counted only
                else:
                    rejected += 1
        self.batches.append((batch_no, emitted, rejected))
        return FakeEmitResult(emitted=emitted, rejected=rejected)

    def finalize(self, report, deliver=True):
        self.report = report
        self.deliver = deliver
        if deliver and self.part.exists():
            self.part.rename(self.output)
        self.report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")


class FakeIngestor:
    """Ingestor stand-in yielding pre-built Records lazily."""

    def __init__(self, records):
        self._records = list(records)
        self.metrics = None
        self.scan_called = False
        self.records_called = False
        self.report = SimpleNamespace(scanned=0, ingested=0, bad_input=0)

    def scan(self, *, estimate=True):
        self.scan_called = True
        return SimpleNamespace(files=("in.jsonl",), pairs=(),
                               estimated_records=len(self._records))

    def records(self):
        self.records_called = True
        for r in self._records:
            self.report.scanned += 1
            self.report.ingested += 1
            yield r


def sess(sid: str, start: int, n: int, cause: str = "eof") -> SimpleNamespace:
    """CONTRACTS §7.1 Session shape stand-in: {session_id, records, cause}."""
    return SimpleNamespace(session_id=sid,
                           records=tuple(rec(start + j) for j in range(n)),
                           cause=cause)


class FakeSessionIngestor:
    """Ingestor stand-in exposing the v1.8 session-stream view (the real M2
    sessions() belongs to a parallel work order — consumption side written
    against the CONTRACTS §7.1 frozen shape: Session {session_id, records,
    cause}, --limit applied INSIDE M2, IngestPlan.session_lens for dry-run)."""

    def __init__(self, sessions=(), session_lens=None):
        self._sessions = list(sessions)
        self.metrics = None
        self.scan_called = False
        self.report = SimpleNamespace(scanned=0, ingested=0, bad_input=0,
                                      sessions=0)
        self._session_lens = (tuple(session_lens) if session_lens is not None
                              else tuple(len(s.records) for s in self._sessions))

    def scan(self, *, estimate=True):
        self.scan_called = True
        return SimpleNamespace(files=("in.jsonl",), pairs=(),
                               estimated_records=sum(self._session_lens),
                               session_lens=self._session_lens)

    def records(self):
        raise AssertionError("stream mode must consume sessions(), not records()")

    def sessions(self):
        for s in self._sessions:
            for _ in s.records:
                self.report.scanned += 1
                self.report.ingested += 1
            self.report.sessions += 1
            yield s


# ── tiny REAL stages (pure logic; unit fixtures for scheduling) ─────────────

class RecordingStage:
    """Pass-through stage that records every invocation and rng draw."""

    def __init__(self, name: str):
        self.name = name
        self.calls: list[tuple[int, tuple[str, ...]]] = []
        self.rng_first: list[float] = []

    async def run(self, batch, ctx):
        self.calls.append((ctx.batch_no, tuple(it.record.id for it in batch)))
        self.rng_first.append(ctx.rng.random())
        return batch


class ExactDedupStage:
    """Real cross-batch exact-text dedup (pure, first-writer-wins)."""

    name = "dedup"

    def __init__(self):
        self._seen: dict[str, str] = {}
        self.calls: list[int] = []

    async def run(self, batch, ctx):
        self.calls.append(ctx.batch_no)
        for item in batch:
            if item.status != "active":
                continue
            key = item.record.text or ""
            kept = self._seen.get(key)
            if kept is not None:
                item.status = "dropped_dup"
                item.dedup = DedupInfo(kind="exact", cluster_key=key[:16], kept_id=kept)
                ctx.metrics.count("dedup.exact")
            else:
                self._seen[key] = item.record.id
                item.dedup = DedupInfo(kind="unique", cluster_key=key[:16], kept_id=None)
        return batch


class ScoreStage:
    """Pure quality stand-in: deterministic scores, no gating, no LLM."""

    name = "quality"

    def __init__(self, scores: dict[str, float]):
        self._scores = scores            # record id -> aggregate score

    async def run(self, batch, ctx):
        for item in batch:
            if item.status != "active":
                continue
            agg = self._scores.get(item.record.id, 0.5)
            item.scores["clarity"] = QualityScore(criterion="clarity", score=agg,
                                                  mode="pairwise_bt", detail={})
            item.scores["__aggregate__"] = QualityScore(criterion="__aggregate__",
                                                        score=agg, mode="pairwise_bt",
                                                        detail={})
        return batch


class FailEveryNth:
    """Marks every n-th active item failed with a REAL StageError."""

    name = "annotate"

    def __init__(self, n: int):
        self._n = n
        self._i = 0

    async def run(self, batch, ctx):
        for item in batch:
            if item.status != "active":
                continue
            self._i += 1
            if self._i % self._n == 0:
                item.errors.append(StageError(stage=self.name, kind="schema_violation",
                                              message="L3 exhausted", retryable=False))
                item.status = "failed"
        return batch


class PureGenerateStage:
    """Deterministic generation fixture: returns `per_batch` new items per
    invocation, no LLM. Also implements generate_all for generate_only mode."""

    name = "generate"

    def __init__(self, per_batch: int = 3, total: int = 0):
        self.per_batch = per_batch
        self.total = total
        self.run_batch_nos: list[int] = []
        self.generate_all_ctx: list[tuple[int, float]] = []
        self._counter = 0

    async def run(self, batch, ctx):
        self.run_batch_nos.append(ctx.batch_no)
        seeds = tuple(it.record.id for it in batch if it.status == "active")[:2]
        sub = []
        for _ in range(self.per_batch):
            self._counter += 1
            sub.append(PipelineItem(record=gen_rec(self._counter, seeds)))
        return sub

    async def generate_all(self, ctx):
        self.generate_all_ctx.append((ctx.batch_no, ctx.rng.random()))
        return [gen_rec(i) for i in range(1, self.total + 1)]


class BlockingGenerateStage:
    """Generation fixture for interruption tests: generate_all parks on an
    event so the test can deliver SIGINT semantics mid-generation (pure
    asyncio, no LLM). `release` lets it finish and return `total` records."""

    name = "generate"

    def __init__(self, total: int = 5):
        self.total = total
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, batch, ctx):               # never used in generate_only
        return []

    async def generate_all(self, ctx):
        self.started.set()
        await self.release.wait()
        return [gen_rec(i) for i in range(1, self.total + 1)]


class StubClassifyStage:
    """Pure classify stand-in (labelkit.operators.classify belongs to a parallel work
    order — deliberately NOT imported). Labels active, still-unclassified items
    round-robin over `labels`, feeds the M13-owned per-label counters, and —
    for record ids listed in `fan` — appends sibling clones IN PLACE to the
    tail of the passed-in list (contract ②a shape: same list object returned,
    clones share record/dedup by reference, fresh default containers)."""

    name = "classify"

    def __init__(self, labels: tuple[str, ...] = ("faq",),
                 fan: dict[str, tuple[str, ...]] | None = None):
        self._labels = labels
        self._fan = fan or {}
        self._i = 0
        self.calls: list[tuple[int, int]] = []     # (batch_no, entry size)

    async def run(self, batch, ctx):
        self.calls.append((ctx.batch_no, len(batch)))
        appended = []
        for item in batch:
            if item.status != "active" or item.classification is not None:
                continue                            # idempotent skip (M13 rule)
            label = self._labels[self._i % len(self._labels)]
            self._i += 1
            extra = self._fan.get(item.record.id, ())
            all_labels = (label, *extra)
            item.classification = Classification(
                label=label, labels=all_labels, source="llm", detail={})
            for lbl in all_labels:
                ctx.metrics.count(f"classify.classes.{lbl}")
            if len(all_labels) >= 2:
                ctx.metrics.count("classify.multi_label_records")
            for sib_label in extra:
                appended.append(PipelineItem(
                    record=item.record, status="active",
                    classification=Classification(
                        label=sib_label, labels=all_labels, source="llm",
                        detail={}),
                    dedup=item.dedup))
        batch.extend(appended)
        return batch


class StubSegmentStage:
    """Pure segment stand-in (labelkit.operators.segment belongs to a parallel work
    order — deliberately NOT imported). Groups the batch's active frames by
    session_id, absorbs the members (record ids listed in `noise` become
    dropped_noise instead) and tail-appends ONE episode envelope per session —
    the contract ②b shape: same list object returned, sequence Record over the
    member records, session_id stamped on the episode envelope."""

    name = "segment"

    def __init__(self, noise: tuple[str, ...] = ()):
        self._noise = set(noise)
        self.calls: list[tuple[int, tuple[str | None, ...]]] = []

    async def run(self, batch, ctx):
        self.calls.append((ctx.batch_no, tuple(it.session_id for it in batch)))
        groups: dict[str | None, list[PipelineItem]] = {}
        for item in batch:
            if item.status != "active":
                continue
            if item.record.id in self._noise:
                item.status = "dropped_noise"
                continue
            groups.setdefault(item.session_id, []).append(item)
        for sid, members in groups.items():
            for m in members:
                m.status = "absorbed"
            episode = Record(id=f"ep:{sid}:{ctx.batch_no}"[:16], modality="text",
                             text=None, raw={"episode": sid}, ui_tree=None,
                             image=None, ref=members[0].record.ref,
                             kind="sequence",
                             members=tuple(m.record for m in members))
            batch.append(PipelineItem(record=episode, session_id=sid))
        return batch


class BreakerStage:
    """Deterministically feeds the breaker with REAL ProviderFatalError
    semantics: per active item a fatal provider result; raises the real
    CircuitBreakerTripped once the sink reports the breaker open — exactly the
    only exception a stage may let escape (CONTRACTS §5)."""

    name = "annotate"

    async def run(self, batch, ctx):
        for item in batch:
            if item.status != "active":
                continue
            ctx.metrics.record_provider_result(fatal=True)
            item.errors.append(StageError(stage=self.name, kind="provider_fatal",
                                          message="401 unauthorized", retryable=False))
            item.status = "failed"
            if ctx.metrics.circuit_broken:
                raise CircuitBreakerTripped("fatal-error threshold reached")
        return batch


# ── wiring helper ───────────────────────────────────────────────────────────

def build(cfg, stages, records=None, *, ingestor=None, llm=None, schema_engine=None):
    metrics = FakeMetrics(threshold=cfg.run.fatal_error_threshold)
    emitter = FakeEmitter(cfg)
    if ingestor is None and cfg.run.mode == "process":
        ingestor = FakeIngestor(records or [])
    orch = Orchestrator(cfg, stages, ingestor, emitter, llm, schema_engine,
                        metrics, RUN_ID, datetime.now().astimezone())
    return orch, metrics, emitter, ingestor


def counts_invariant(counts):
    # v1.7: the fanout term joins the source side under multi assignment
    # (absent key = 0 keeps the pre-classify form).
    lhs = (counts["emitted"] + counts["dropped_dup"] + counts["dropped_lowq"]
           + counts["dropped_verify"] + counts["failed"] + counts["bad_input"])
    rhs = counts["scanned"] + counts["generated"] + counts.get("fanout", 0)
    return lhs == rhs


# ── tests: batching / ordering / events ─────────────────────────────────────

async def test_batching_sizes_and_order(tmp_path):
    cfg = make_cfg(tmp_path, batch_size=4)
    stage = RecordingStage("dedup")
    orch, metrics, emitter, _ = build(cfg, [stage], [rec(i) for i in range(1, 11)])
    summary = await orch.run()

    assert [bn for bn, _ in stage.calls] == [1, 2, 3]
    assert [len(ids) for _, ids in stage.calls] == [4, 4, 2]   # tail batch as-is
    # ingest order preserved
    assert stage.calls[0][1] == tuple(f"{i:016x}" for i in range(1, 5))
    assert summary.exit_code == 0
    assert summary.output_lines == 10
    assert emitter.output.exists() and not emitter.part.exists()
    assert len(emitter.output.read_text().splitlines()) == 10


async def test_run_and_batch_events(tmp_path):
    cfg = make_cfg(tmp_path, batch_size=4)
    orch, metrics, emitter, _ = build(cfg, [RecordingStage("dedup")],
                                      [rec(i) for i in range(1, 6)])
    await orch.run()

    names = [e[0] for e in metrics.events]
    assert names[0] == "run.start"
    assert names[-1] == "run.end"
    assert names.count("batch.start") == 2 and names.count("batch.end") == 2
    run_start = metrics.events[0]
    assert run_start[1] == "run" and run_start[2] == 0
    assert run_start[4]["trace_schema_version"] == 1
    assert run_start[4]["config_digest"] == "sha256:c"
    starts = [e for e in metrics.events if e[0] == "batch.start"]
    assert [e[2] for e in starts] == [1, 2]
    assert [e[4]["size"] for e in starts] == [4, 1]
    ends = [e for e in metrics.events if e[0] == "batch.end"]
    for e in ends:
        assert set(e[4]) == {"active", "dropped_dup", "dropped_lowq",
                             "dropped_verify", "failed", "duration_ms"}
    run_end = metrics.events[-1]
    assert run_end[4]["exit_code"] == 0
    assert run_end[4]["counts"]["emitted"] == 5
    # trace flush follows each batch emit + one final flush
    assert metrics.flushes == 3


async def test_rng_derivation_per_batch_and_stage(tmp_path):
    cfg = make_cfg(tmp_path, batch_size=4, seed=7)
    stage = RecordingStage("dedup")
    orch, *_ = build(cfg, [stage], [rec(i) for i in range(1, 9)])
    await orch.run()

    assert stage.rng_first[0] == random.Random("7:1:dedup").random()
    assert stage.rng_first[1] == random.Random("7:2:dedup").random()


async def test_limit_truncates_stream(tmp_path):
    cfg = make_cfg(tmp_path, batch_size=4, limit=5)
    stage = RecordingStage("dedup")
    orch, _, emitter, ingestor = build(cfg, [stage], [rec(i) for i in range(1, 11)])
    summary = await orch.run()

    assert summary.output_lines == 5
    assert [len(ids) for _, ids in stage.calls] == [4, 1]
    assert ingestor.report.scanned == 5           # lazy stream: only 5 consumed


# ── tests: counts invariant with real pure stages ───────────────────────────

async def test_counts_invariant_dedup_and_failures(tmp_path):
    # 12 records with 3 duplicate texts; every 4th surviving record fails.
    records = [rec(i) for i in range(1, 10)] + [
        rec(10, "sample text 1"), rec(11, "sample text 2"), rec(12, "sample text 3")]
    cfg = make_cfg(tmp_path, batch_size=5, annotate=True)
    orch, metrics, emitter, _ = build(cfg, [ExactDedupStage(), FailEveryNth(4)], records)
    summary = await orch.run()

    counts = emitter.report["counts"]
    assert counts["scanned"] == 12
    assert counts["dropped_dup"] == 3
    assert counts["failed"] == 2                  # 9 survivors, every 4th fails
    assert counts["emitted"] == 7
    assert counts_invariant(counts)
    assert summary.counts == counts
    assert summary.rejects_lines == 5
    # dedup stage counter feeds report block
    assert emitter.report["dedup"]["exact"] == 3


async def test_dedup_index_survives_across_batches(tmp_path):
    # duplicate text appears in a LATER batch — global cross-batch dedup state.
    records = [rec(i) for i in range(1, 5)] + [rec(5, "sample text 1")]
    cfg = make_cfg(tmp_path, batch_size=4)
    orch, _, emitter, _ = build(cfg, [ExactDedupStage()], records)
    await orch.run()
    assert emitter.report["counts"]["dropped_dup"] == 1
    assert emitter.report["counts"]["emitted"] == 4


# ── tests: generation re-flow (process mode) ────────────────────────────────

async def test_generate_reflow_single_round(tmp_path):
    gen_cfg = GenerateConfig(enabled=True, instruction="生成")
    cfg = make_cfg(tmp_path, batch_size=4, quality=True, generate=gen_cfg,
                   quality_cfg=QualityConfig(enabled=True))
    dedup = ExactDedupStage()
    quality = ScoreStage({})                       # scores everything 0.5, no gate
    gen = PureGenerateStage(per_batch=3)
    orch, metrics, emitter, _ = build(cfg, [dedup, quality, gen],
                                      [rec(i) for i in range(1, 9)])
    summary = await orch.run()

    # generate ran once per MAIN batch only — never on re-flow sub-batches.
    assert gen.run_batch_nos == [1, 3]
    # sub-batch re-enters at dedup right after its parent, consecutive numbering:
    # batch1=main(4), batch2=sub(3), batch3=main(4), batch4=sub(3)
    assert dedup.calls == [1, 2, 3, 4]
    starts = [e for e in metrics.events if e[0] == "batch.start"]
    assert [(e[2], e[4]["size"]) for e in starts] == [(1, 4), (2, 3), (3, 4), (4, 3)]

    counts = emitter.report["counts"]
    assert counts["generated"] == 6
    assert counts["scanned"] == 8
    assert counts["emitted"] == 14
    assert counts_invariant(counts)
    assert summary.output_lines == 14
    assert emitter.report["generate"] == {"buckets": {}}


async def test_generate_sub_batch_split_at_batch_size(tmp_path):
    gen_cfg = GenerateConfig(enabled=True, instruction="生成")
    cfg = make_cfg(tmp_path, batch_size=4, quality=True, generate=gen_cfg,
                   quality_cfg=QualityConfig(enabled=True))
    gen = PureGenerateStage(per_batch=6)           # sub-batch > batch_size → split
    orch, metrics, emitter, _ = build(cfg, [ExactDedupStage(), ScoreStage({}), gen],
                                      [rec(i) for i in range(1, 5)])
    await orch.run()

    starts = [e for e in metrics.events if e[0] == "batch.start"]
    assert [(e[2], e[4]["size"]) for e in starts] == [(1, 4), (2, 4), (3, 2)]
    assert gen.run_batch_nos == [1]                # single round, no recursion
    assert emitter.report["counts"]["generated"] == 6


# ── tests: generate_only mode ───────────────────────────────────────────────

async def test_generate_only_batches_from_dedup_onward(tmp_path):
    gen_cfg = GenerateConfig(enabled=True, instruction="生成",
                             seed_examples=("a", "b", "c"))
    cfg = make_cfg(tmp_path, mode="generate_only", batch_size=4, seed=3,
                   generate=gen_cfg)
    gen = PureGenerateStage(total=10)
    dedup = ExactDedupStage()
    orch, metrics, emitter, _ = build(cfg, [dedup, gen])
    summary = await orch.run()

    # generate_all called once, batch_no fixed 0, pre-draw rng Random(f"{seed}:0:generate")
    assert len(gen.generate_all_ctx) == 1
    bno, first_draw = gen.generate_all_ctx[0]
    assert bno == 0
    assert first_draw == random.Random("3:0:generate").random()
    assert gen.run_batch_nos == []                 # M6 never runs as a chain stage

    assert dedup.calls == [1, 2, 3]                # 10 records → 4/4/2 from M3 onward
    counts = emitter.report["counts"]
    assert counts == {"scanned": 0, "ingested": 0, "bad_input": 0,
                      "dropped_dup": 0, "dropped_lowq": 0, "dropped_verify": 0,
                      "failed": 0, "generated": 10, "emitted": 10}
    assert counts_invariant(counts)
    assert summary.exit_code == 0
    assert emitter.output.exists()


async def test_generate_only_zero_generated_finalizes_ok(tmp_path):
    gen_cfg = GenerateConfig(enabled=True, instruction="生成", standalone_count=5)
    cfg = make_cfg(tmp_path, mode="generate_only", batch_size=4, generate=gen_cfg)
    gen = PureGenerateStage(total=0)
    orch, metrics, emitter, _ = build(cfg, [ExactDedupStage(), gen])
    summary = await orch.run()

    assert summary.exit_code == 0
    assert summary.output_lines == 0
    assert emitter.deliver is True
    assert emitter.output.exists()                 # empty but delivered
    assert emitter.report["counts"]["generated"] == 0
    names = [e[0] for e in metrics.events]
    assert names == ["run.start", "run.end"]


async def test_generate_only_limit_truncates_records(tmp_path):
    gen_cfg = GenerateConfig(enabled=True, instruction="生成", standalone_count=10)
    cfg = make_cfg(tmp_path, mode="generate_only", batch_size=4, limit=5,
                   generate=gen_cfg)
    gen = PureGenerateStage(total=10)              # fixture ignores limit; M10 truncates
    orch, _, emitter, _ = build(cfg, [ExactDedupStage(), gen])
    summary = await orch.run()
    assert summary.output_lines == 5
    assert emitter.report["counts"]["generated"] == 5


async def test_generate_only_generate_stage_time_in_report(tmp_path):
    """generate_only: the generation phase is an enabled stage — its wall-clock
    must land in metrics.add_stage_time and report.timing.per_stage_s (§9.3,
    CONTRACTS §7.9 stage timing)."""
    gen_cfg = GenerateConfig(enabled=True, instruction="生成", standalone_count=6)
    cfg = make_cfg(tmp_path, mode="generate_only", batch_size=4, generate=gen_cfg)
    gen = PureGenerateStage(total=6)
    orch, metrics, emitter, _ = build(cfg, [ExactDedupStage(), gen])
    await orch.run()

    assert "generate" in metrics.stage_times
    assert metrics.stage_times["generate"] >= 0.0
    per_stage = emitter.report["timing"]["per_stage_s"]
    assert set(per_stage) == {"generate", "dedup"}
    assert per_stage["generate"] >= 0.0


# ── tests: interruption (SIGINT/SIGTERM semantics via _request_stop) ────────

async def test_generate_only_interrupt_during_generation_is_cancellable(tmp_path):
    """SIGINT during the generation phase: generate_all runs as the guarded
    current task, so the 30 s timer can cancel it; the run finalizes normally
    with interrupted=true and no records (spec 3.10.3 中断; CONTRACTS §7.9)."""
    gen_cfg = GenerateConfig(enabled=True, instruction="生成", standalone_count=5)
    cfg = make_cfg(tmp_path, mode="generate_only", batch_size=4, generate=gen_cfg)
    gen = BlockingGenerateStage(total=5)
    orch, metrics, emitter, _ = build(cfg, [ExactDedupStage(), gen])

    run_task = asyncio.ensure_future(orch.run())
    await gen.started.wait()
    # The generation phase must be the cancellable current task.
    assert orch._current_task is not None and not orch._current_task.done()
    orch._request_stop()
    assert orch._timer_handles                     # 30 s cancel timer scheduled
    orch._current_task.cancel()                    # fire the timeout now, not in 30 s

    summary = await run_task
    assert summary.interrupted is True
    assert summary.exit_code == 0                  # graceful: finalize + rename
    assert summary.output_lines == 0
    assert emitter.deliver is True
    assert emitter.output.exists() and not emitter.part.exists()
    assert emitter.report["run"]["interrupted"] is True
    assert emitter.report["counts"]["generated"] == 0
    assert emitter.report["counts"]["emitted"] == 0
    # cancelled or not, the generation phase wall-clock is recorded
    assert "generate" in emitter.report["timing"]["per_stage_s"]


async def test_generate_only_interrupt_stops_taking_batches_after_generation(tmp_path):
    """SIGINT received while generation is in flight, generation then completes
    within the 30 s window: no NEW batches are taken (停止取新批) and the run
    finalizes with interrupted=true; generated calls are still accounted."""
    gen_cfg = GenerateConfig(enabled=True, instruction="生成", standalone_count=5)
    cfg = make_cfg(tmp_path, mode="generate_only", batch_size=4, generate=gen_cfg)
    gen = BlockingGenerateStage(total=5)
    dedup = ExactDedupStage()
    orch, metrics, emitter, _ = build(cfg, [dedup, gen])

    run_task = asyncio.ensure_future(orch.run())
    await gen.started.wait()
    orch._request_stop()
    gen.release.set()                              # generation finishes gracefully

    summary = await run_task
    assert summary.interrupted is True
    assert summary.exit_code == 0
    assert dedup.calls == []                       # no new batch entered the chain
    assert summary.output_lines == 0
    assert emitter.deliver is True
    assert emitter.report["counts"]["generated"] == 5
    assert emitter.report["run"]["interrupted"] is True


async def test_process_interrupt_stops_taking_new_batches(tmp_path):
    """Process mode regression guard: stop requested during batch 1 → batch 1
    completes, no further batch is taken, finalize delivers normally."""

    class StopDuringFirstBatch:
        name = "dedup"

        def __init__(self, orch_ref):
            self._orch_ref = orch_ref
            self.calls: list[int] = []

        async def run(self, batch, ctx):
            self.calls.append(ctx.batch_no)
            if ctx.batch_no == 1:
                self._orch_ref[0]._request_stop()
            return batch

    cfg = make_cfg(tmp_path, batch_size=4)
    orch_ref: list = [None]
    stage = StopDuringFirstBatch(orch_ref)
    orch, _, emitter, _ = build(cfg, [stage], [rec(i) for i in range(1, 11)])
    orch_ref[0] = orch
    summary = await orch.run()

    assert stage.calls == [1]                      # batch 2/3 never taken
    assert summary.interrupted is True
    assert summary.exit_code == 0
    assert summary.output_lines == 4               # batch 1 flushed lines stay valid
    assert emitter.deliver is True
    assert emitter.report["run"]["interrupted"] is True


# ── tests: circuit breaker ──────────────────────────────────────────────────

async def test_circuit_breaker_exit_4_partial_delivery(tmp_path):
    """v1.6 熔断交付 (spec 3.10.3, decision 1.6 ②): a circuit break DELIVERS
    the completed batches — .part renamed, report marks partial_delivery=true,
    counts gains the balancing residual `unprocessed`. Exit code stays 4."""
    cfg = make_cfg(tmp_path, batch_size=4, fatal_threshold=3, annotate=True)
    orch, metrics, emitter, _ = build(cfg, [BreakerStage()],
                                      [rec(i) for i in range(1, 11)])
    summary = await orch.run()

    assert summary.exit_code == 4
    assert summary.interrupted is False
    assert emitter.deliver is True
    assert emitter.output.exists() and not emitter.part.exists()
    assert emitter.report is not None              # report still written
    assert emitter.report["run"]["exit_code"] == 4
    assert emitter.report["run"]["circuit_broken"] is True
    assert emitter.report["run"]["partial_delivery"] is True
    counts = emitter.report["counts"]
    # Invariant extension (spec 6.4): emitted + dropped_* + failed + bad_input
    # + unprocessed = scanned + generated.
    assert "unprocessed" in counts
    assert (counts["emitted"] + counts["dropped_dup"] + counts["dropped_lowq"]
            + counts["dropped_verify"] + counts["failed"] + counts["bad_input"]
            + counts["unprocessed"]) == counts["scanned"] + counts["generated"]
    assert emitter.report_path.exists()
    run_end = metrics.events[-1]
    assert run_end[0] == "run.end" and run_end[4]["exit_code"] == 4


async def test_clean_run_report_has_no_partial_delivery_fields(tmp_path):
    """partial_delivery / counts.unprocessed are 只增 fields present ONLY on
    breaker-trip runs (spec 6.4) — healthy runs keep the v1.5 report shape."""
    cfg = make_cfg(tmp_path, batch_size=4)
    orch, _, emitter, _ = build(cfg, [], [rec(1), rec(2)])
    summary = await orch.run()
    assert summary.exit_code == 0
    assert "partial_delivery" not in emitter.report["run"]
    assert "unprocessed" not in emitter.report["counts"]


async def test_breaker_streak_resets_on_success(tmp_path):
    metrics = FakeMetrics(threshold=3)
    metrics.record_provider_result(fatal=True)
    metrics.record_provider_result(fatal=True)
    metrics.record_provider_result(fatal=False)
    metrics.record_provider_result(fatal=True)
    assert metrics.circuit_broken is False
    metrics.record_provider_result(fatal=True)
    metrics.record_provider_result(fatal=True)
    assert metrics.circuit_broken is True


# ── tests: strict escalation ────────────────────────────────────────────────

async def test_strict_with_rejects_exit_1(tmp_path):
    records = [rec(1), rec(2), rec(3, "sample text 1")]
    cfg = make_cfg(tmp_path, batch_size=4, strict=True)
    orch, _, emitter, _ = build(cfg, [ExactDedupStage()], records)
    summary = await orch.run()
    assert summary.exit_code == 1
    assert emitter.report["run"]["exit_code"] == 1
    assert emitter.deliver is True                 # strict still delivers output
    assert emitter.output.exists()


async def test_strict_without_rejects_exit_0(tmp_path):
    cfg = make_cfg(tmp_path, batch_size=4, strict=True)
    orch, _, _, _ = build(cfg, [ExactDedupStage()], [rec(1), rec(2)])
    summary = await orch.run()
    assert summary.exit_code == 0


# ── tests: dry-run ──────────────────────────────────────────────────────────

async def test_dry_run_process_no_output_but_report(tmp_path, capsys):
    cfg = make_cfg(tmp_path, batch_size=4, dry_run=True, quality=True,
                   annotate=True, quality_cfg=QualityConfig(enabled=True))
    orch, metrics, emitter, ingestor = build(cfg, [], [rec(i) for i in range(1, 11)])
    summary = await orch.run()

    assert summary.exit_code == 0
    assert ingestor.scan_called and not ingestor.records_called
    assert emitter.opened is False                 # no .part, no main output
    assert not emitter.part.exists() and not emitter.output.exists()
    assert emitter.deliver is False
    assert emitter.report_path.exists()            # ...but a report
    assert emitter.report["run"]["exit_code"] == 0
    assert emitter.report["counts"]["emitted"] == 0

    err = capsys.readouterr().err
    assert "dry-run" in err
    assert "(report only)" in err                  # trace disabled → not mentioned
    assert "estimated_records=10" in err
    # pairwise: k*floor(b/2) per batch → 4*2 + 4*2 + 4*1 = 20; annotate = 10
    assert "quality_calls=20" in err
    assert "annotate_calls=10" in err
    assert "total=30" in err


async def test_dry_run_trace_enabled_message_and_lifecycle_events(tmp_path, capsys):
    """Dry-run with trace.enabled: the opt-in trace channel (a first-class
    output channel, spec 2.6) still records run.start/run.end, the stderr
    message says so, and report.trace.events matches the trace file."""
    trace_path = tmp_path / "dry.trace.jsonl"
    cfg = make_cfg(tmp_path, dry_run=True, annotate=True,
                   trace=TraceConfig(enabled=True, path=str(trace_path)))
    event_log = EventLog(cfg.trace, RUN_ID)
    metrics = MetricsSink(cfg, RUN_ID, event_log)
    emitter = FakeEmitter(cfg)
    ingestor = FakeIngestor([rec(1)])
    orch = Orchestrator(cfg, [], ingestor, emitter, None, None,
                        metrics, RUN_ID, datetime.now().astimezone())
    summary = await orch.run()
    event_log.close()

    assert summary.exit_code == 0
    assert emitter.opened is False                 # still no main output
    err = capsys.readouterr().err
    assert "(report and trace only)" in err
    lines = trace_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["ev"] for line in lines] == ["run.start", "run.end"]
    assert emitter.report["trace"]["events"] == 2
    assert emitter.report["trace"]["dropped_events"] == 0


async def test_dry_run_generate_only_static_formula(tmp_path, capsys):
    # seed-pool form: C = ceil(len(seeds) * num_per_record / num_per_call) = ceil(3*2/4) = 2
    gen_cfg = GenerateConfig(enabled=True, instruction="生成",
                             seed_examples=("a", "b", "c"),
                             num_per_record=2, num_per_call=4)
    cfg = make_cfg(tmp_path, mode="generate_only", batch_size=4, dry_run=True,
                   annotate=True, generate=gen_cfg)
    orch, metrics, emitter, _ = build(cfg, [])
    summary = await orch.run()

    assert summary.exit_code == 0
    err = capsys.readouterr().err
    assert "generate_calls=2" in err
    assert "estimated_records=8" in err            # 2 calls × 4 per call
    assert "annotate_calls=8" in err
    assert not emitter.opened
    assert emitter.report_path.exists()


async def test_dry_run_generate_only_standalone_with_limit(tmp_path, capsys):
    # seedless form: C = ceil(standalone_count/num_per_call) = ceil(500/4) = 125;
    # --limit 10 → C = ceil(10/4) = 3, records min(12, 10) = 10.
    gen_cfg = GenerateConfig(enabled=True, instruction="生成",
                             standalone_count=500, num_per_call=4)
    cfg = make_cfg(tmp_path, mode="generate_only", dry_run=True, limit=10,
                   annotate=True, generate=gen_cfg)
    orch, _, _, _ = build(cfg, [])
    await orch.run()
    err = capsys.readouterr().err
    assert "generate_calls=3" in err
    assert "estimated_records=10" in err


# ── tests: report assembly ──────────────────────────────────────────────────

async def test_report_quality_histogram_and_means(tmp_path):
    scores = {f"{i:016x}": s for i, s in
              [(1, 0.05), (2, 0.95), (3, 1.0), (4, 0.55)]}
    cfg = make_cfg(tmp_path, batch_size=4, dedup=False,
                   quality_cfg=QualityConfig(enabled=True, mode="pairwise", rounds=4))
    orch, _, emitter, _ = build(cfg, [ScoreStage(scores)], [rec(i) for i in range(1, 5)])
    await orch.run()

    q = emitter.report["quality"]
    assert q["mode"] == "pairwise_bt" and q["rounds"] == 4
    hist = q["aggregate_histogram"]
    assert list(hist) == ["0.0-0.1", "0.1-0.2", "0.2-0.3", "0.3-0.4", "0.4-0.5",
                          "0.5-0.6", "0.6-0.7", "0.7-0.8", "0.8-0.9", "0.9-1.0"]
    assert hist["0.0-0.1"] == 1
    assert hist["0.5-0.6"] == 1
    assert hist["0.9-1.0"] == 2                    # 0.95 and 1.0 (upper-inclusive last)
    assert q["per_criterion_mean"]["clarity"] == pytest.approx((0.05 + 0.95 + 1.0 + 0.55) / 4)
    assert "dedup" not in emitter.report           # disabled stage → no block


async def test_report_shape_and_llm_usage(tmp_path):
    class FakeLLM:
        usage_by_profile = {
            "default": SimpleNamespace(calls=3, prompt_tokens=100,
                                       completion_tokens=40, retries=1,
                                       est_cost_usd=None),
            "judge": SimpleNamespace(calls=2, prompt_tokens=50,
                                     completion_tokens=10, retries=0,
                                     est_cost_usd=0.5),
        }

    class FakeEngine:
        stats = {"l0_or_clean": 4, "l1": 1, "l3_1": 0, "l3_2": 0, "rejected": 0}

    cfg = make_cfg(tmp_path, batch_size=4)
    orch, metrics, emitter, _ = build(cfg, [ExactDedupStage()], [rec(1), rec(2)],
                                      llm=FakeLLM(), schema_engine=FakeEngine())
    summary = await orch.run()

    report = emitter.report
    assert set(report["run"]) == {"tool_version", "started_at", "finished_at",
                                  "interrupted", "circuit_broken", "exit_code",
                                  "modality", "seed",
                                  "config_digest", "project_digest"}
    assert report["run"]["tool_version"] == "1.0.0"
    assert report["run"]["interrupted"] is False
    assert report["schema_engine"]["resolved_at"]["l0_or_clean"] == 4
    assert report["llm_usage"]["default"] == {"calls": 3, "prompt_tokens": 100,
                                              "completion_tokens": 40, "retries": 1}
    assert report["llm_usage"]["judge"]["est_cost_usd"] == 0.5
    assert report["trace"]["enabled"] is False
    # trace disabled → no terminal-event accounting: the report snapshots the
    # counter as-is (run.start + batch.start + batch.end at build time; the
    # stub counts run.end too, after finalize). With trace ENABLED the report
    # adds the pending run.end so it matches the trace file (see
    # test_report_trace_events_counts_run_end).
    assert report["trace"]["events"] == 3
    assert metrics.event_log.events_written == 4   # + run.end after finalize
    assert "dedup" in report and "quality" not in report
    assert report["timing"]["per_stage_s"].keys() == {"dedup"}
    assert report["timing"]["wall_s"] == pytest.approx(summary.wall_s, abs=0.01)
    assert isinstance(summary, RunSummary)


async def test_report_trace_events_counts_run_end(tmp_path):
    """report.trace must describe the FINAL trace file: the terminal run.end
    line is written only after the report is assembled (§8.1 — run.end is the
    trace's last line, after finalize), so the orchestrator pre-counts it."""
    trace_path = tmp_path / "t.trace.jsonl"
    cfg = make_cfg(tmp_path, batch_size=4,
                   trace=TraceConfig(enabled=True, path=str(trace_path)))
    event_log = EventLog(cfg.trace, RUN_ID)
    metrics = MetricsSink(cfg, RUN_ID, event_log)
    emitter = FakeEmitter(cfg)
    ingestor = FakeIngestor([rec(1), rec(2)])
    orch = Orchestrator(cfg, [ExactDedupStage()], ingestor, emitter, None, None,
                        metrics, RUN_ID, datetime.now().astimezone())
    summary = await orch.run()
    event_log.close()

    assert summary.exit_code == 0
    lines = trace_path.read_text(encoding="utf-8").splitlines()
    # run.start, batch.start, batch.end, run.end
    assert [json.loads(line)["ev"] for line in lines] == [
        "run.start", "batch.start", "batch.end", "run.end"]
    assert emitter.report["trace"]["events"] == len(lines)
    assert emitter.report["trace"]["dropped_events"] == 0
    assert event_log.events_written == len(lines)


async def test_report_trace_run_end_counted_dropped_when_channel_closed(tmp_path):
    """When a write failure closed the channel, the pending run.end will be
    dropped too — report.trace.dropped_events must include it (§7 test
    invariant: dropped_events 计数正确)."""
    cfg = make_cfg(tmp_path, batch_size=4,
                   trace=TraceConfig(enabled=True,
                                     path=str(tmp_path / "no_dir" / "t.jsonl")))
    event_log = EventLog(cfg.trace, RUN_ID)
    assert event_log.closed is False             # lazy open: untouched so far
    event_log.emit(TraceEvent(ts="t", run_id=RUN_ID, batch_no=0, stage="run",
                              ev="run.probe", record_ids=(), payload={}))
    assert event_log.closed is True              # first emit opened → failed → closed
    metrics = MetricsSink(cfg, RUN_ID, event_log)
    emitter = FakeEmitter(cfg)
    ingestor = FakeIngestor([rec(1), rec(2)])
    orch = Orchestrator(cfg, [ExactDedupStage()], ingestor, emitter, None, None,
                        metrics, RUN_ID, datetime.now().astimezone())
    await orch.run()

    # probe + run.start + batch.start + batch.end dropped before report
    # assembly; the pending run.end is pre-counted, then actually dropped.
    assert emitter.report["trace"]["events"] == 0
    assert emitter.report["trace"]["dropped_events"] == 5
    assert event_log.dropped_events == 5


async def test_generate_buckets_from_counters(tmp_path):
    """M6 feeds generate.buckets.* counters; M10 folds them into the report."""

    class BucketGenStage(PureGenerateStage):
        async def run(self, batch, ctx):
            ctx.metrics.count("generate.buckets.default×concise.calls", 1)
            ctx.metrics.count("generate.buckets.default×concise.produced", 3)
            ctx.metrics.count("generate.buckets.default×concise.survived_dedup", 3)
            return await super().run(batch, ctx)

    gen_cfg = GenerateConfig(enabled=True, instruction="生成")
    cfg = make_cfg(tmp_path, batch_size=8, quality=True, generate=gen_cfg,
                   quality_cfg=QualityConfig(enabled=True))
    orch, _, emitter, _ = build(cfg, [ExactDedupStage(), ScoreStage({}),
                                      BucketGenStage(per_batch=3)],
                                [rec(1), rec(2)])
    await orch.run()
    assert emitter.report["generate"]["buckets"] == {
        "default×concise": {"calls": 1, "produced": 3, "survived_dedup": 3}}


# ── tests: stage chain composition per switches ─────────────────────────────

async def test_chain_composition_canonical_order_and_switches(tmp_path):
    """Stages run in canonical order dedup→quality→annotate→verify regardless
    of list order; disabled switches exclude a supplied stage."""
    order: list[str] = []

    class Probe:
        def __init__(self, name):
            self.name = name

        async def run(self, batch, ctx):
            order.append(self.name)
            return batch

    cfg = make_cfg(tmp_path, batch_size=8, dedup=True, annotate=True, verify=True,
                   quality_cfg=QualityConfig(enabled=True))
    stages = [Probe("verify"), Probe("annotate"), Probe("quality"), Probe("dedup")]
    orch, _, _, _ = build(cfg, stages, [rec(1), rec(2)])
    await orch.run()
    assert order == ["dedup", "quality", "annotate", "verify"]

    order.clear()
    cfg2 = make_cfg(tmp_path, batch_size=8, dedup=False, annotate=True, verify=False)
    orch2, _, _, _ = build(cfg2, stages, [rec(1)])
    await orch2.run()
    assert order == ["annotate"]                   # switched-off stages skipped


async def test_stage_timing_recorded(tmp_path):
    cfg = make_cfg(tmp_path, batch_size=4)
    orch, metrics, emitter, _ = build(cfg, [ExactDedupStage()],
                                      [rec(i) for i in range(1, 6)])
    await orch.run()
    assert "dedup" in metrics.stage_times
    assert metrics.stage_times["dedup"] >= 0.0
    assert "dedup" in emitter.report["timing"]["per_stage_s"]


# ── tests: v1.7 classify orchestration (chain / fanout / report / dry-run) ──

async def test_chain_order_includes_classify_after_dedup(tmp_path):
    """v1.7 canonical order: dedup → classify → quality → annotate → verify
    regardless of the supplied stage list order."""
    order: list[str] = []

    class Probe:
        def __init__(self, name):
            self.name = name

        async def run(self, batch, ctx):
            order.append(self.name)
            return batch

    cfg = make_cfg(tmp_path, batch_size=8, dedup=True, annotate=True, verify=True,
                   quality_cfg=QualityConfig(enabled=True), classify=classify_cfg())
    stages = [Probe("verify"), Probe("classify"), Probe("annotate"),
              Probe("quality"), Probe("dedup")]
    orch, _, _, _ = build(cfg, stages, [rec(1), rec(2)])
    await orch.run()
    assert order == ["dedup", "classify", "quality", "annotate", "verify"]


async def test_classify_runs_on_reflow_sub_batches(tmp_path):
    """The re-flow chain includes classify (§7.9): generation sub-batches pass
    through it (already-classified inherited items rely on M13's idempotent
    skip — exercised here via the stub's classification-is-None guard)."""
    gen_cfg = GenerateConfig(enabled=True, instruction="生成")
    cfg = make_cfg(tmp_path, batch_size=4, quality=True, generate=gen_cfg,
                   quality_cfg=QualityConfig(enabled=True), classify=classify_cfg())
    stub = StubClassifyStage()
    gen = PureGenerateStage(per_batch=3)
    orch, metrics, emitter, _ = build(
        cfg, [ExactDedupStage(), stub, ScoreStage({}), gen],
        [rec(i) for i in range(1, 5)])
    await orch.run()

    # batch 1 = main, batch 2 = re-flow sub-batch — classify saw BOTH.
    assert [bn for bn, _ in stub.calls] == [1, 2]
    # pre-classified items are skipped on re-entry, unclassified ones labeled:
    # 4 main + 3 generated all emitted with a classification counter fed.
    assert metrics.counters["classify.classes.faq"] == 7


async def test_generate_only_chain_includes_classify(tmp_path):
    gen_cfg = GenerateConfig(enabled=True, instruction="生成", standalone_count=5)
    cfg = make_cfg(tmp_path, mode="generate_only", batch_size=4, generate=gen_cfg,
                   classify=classify_cfg())
    gen = PureGenerateStage(total=5)
    stub = StubClassifyStage()
    orch, metrics, emitter, _ = build(cfg, [ExactDedupStage(), stub, gen])
    summary = await orch.run()

    assert summary.exit_code == 0
    assert [bn for bn, _ in stub.calls] == [1, 2]  # 5 records → batches of 4 + 1
    assert metrics.counters["classify.classes.faq"] == 5


async def test_fanout_metered_counts_key_and_extended_invariant(tmp_path):
    """R9/R20: M10 meters counts.fanout as the len-delta across classify;
    batch.start.size stays the batch-ENTRY count; batch.end carries the
    per-batch fanout; the counts invariant gains the fanout term."""
    ccfg = classify_cfg(assignment="multi", classes=("faq", "chat", "other"))
    fan = {f"{1:016x}": ("chat", "other"), f"{3:016x}": ("chat",)}
    cfg = make_cfg(tmp_path, batch_size=4, classify=ccfg)
    stub = StubClassifyStage(labels=("faq",), fan=fan)
    orch, metrics, emitter, _ = build(cfg, [ExactDedupStage(), stub],
                                      [rec(i) for i in range(1, 5)])
    summary = await orch.run()

    assert metrics.counters["counts.fanout"] == 3
    counts = emitter.report["counts"]
    assert counts["fanout"] == 3
    assert counts["scanned"] == 4
    assert counts["emitted"] == 7                  # 4 originals + 3 siblings
    assert counts_invariant(counts)
    assert summary.output_lines == 7

    starts = [e for e in metrics.events if e[0] == "batch.start"]
    assert [e[4]["size"] for e in starts] == [4]   # entry size, pre-fan-out
    ends = [e for e in metrics.events if e[0] == "batch.end"]
    assert [e[4]["fanout"] for e in ends] == [3]
    assert metrics.counters["classify.multi_label_records"] == 2


async def test_single_assignment_no_fanout_counts_key_but_batch_end_zero(tmp_path):
    """counts.fanout appears ONLY under multi (§9.3); batch.end carries fanout
    whenever classify is enabled (0 under single)."""
    cfg = make_cfg(tmp_path, batch_size=4, classify=classify_cfg())
    orch, metrics, emitter, _ = build(cfg, [ExactDedupStage(), StubClassifyStage()],
                                      [rec(1), rec(2)])
    await orch.run()

    assert "fanout" not in emitter.report["counts"]
    assert "counts.fanout" not in metrics.counters
    ends = [e for e in metrics.events if e[0] == "batch.end"]
    assert all(e[4]["fanout"] == 0 for e in ends)
    assert counts_invariant(emitter.report["counts"])


async def test_classify_disabled_batch_end_has_no_fanout_key(tmp_path):
    cfg = make_cfg(tmp_path, batch_size=4)
    orch, metrics, emitter, _ = build(cfg, [ExactDedupStage()], [rec(1)])
    await orch.run()
    ends = [e for e in metrics.events if e[0] == "batch.end"]
    assert ends and all("fanout" not in e[4] for e in ends)
    assert "classify" not in emitter.report


async def test_breaker_residual_includes_fanout(tmp_path):
    """R10: the unprocessed balancing residual adds + fanout to its source
    side — fanned-out siblings are envelopes that must be accounted for."""
    ccfg = classify_cfg(assignment="multi", classes=("faq", "chat"))
    fan = {f"{1:016x}": ("chat",)}
    cfg = make_cfg(tmp_path, batch_size=2, fatal_threshold=1, annotate=True,
                   classify=ccfg)
    stub = StubClassifyStage(labels=("faq",), fan=fan)
    orch, metrics, emitter, _ = build(cfg, [stub, BreakerStage()],
                                      [rec(i) for i in range(1, 7)])
    summary = await orch.run()

    assert summary.exit_code == 4
    assert emitter.report["run"]["partial_delivery"] is True
    counts = emitter.report["counts"]
    assert counts["fanout"] == 1
    assert "unprocessed" in counts
    lhs = (counts["emitted"] + counts["dropped_dup"] + counts["dropped_lowq"]
           + counts["dropped_verify"] + counts["failed"] + counts["bad_input"]
           + counts["unprocessed"])
    assert lhs == counts["scanned"] + counts["generated"] + counts["fanout"]


async def test_report_classify_block_shape_zero_based_classes(tmp_path):
    """§9.3 classify block: classes histogram zero-based over ALL declared
    classes; single assignment carries no multi_label_records key."""
    ccfg = classify_cfg(classes=("faq", "chat", "other"))
    cfg = make_cfg(tmp_path, batch_size=4, classify=ccfg)
    stub = StubClassifyStage(labels=("faq",))      # nothing ever hits chat/other
    orch, metrics, emitter, _ = build(cfg, [ExactDedupStage(), stub],
                                      [rec(i) for i in range(1, 5)])
    metrics.counters["classify.fallback"] = 2      # M13-owned counters, pre-fed
    metrics.counters["classify.failures"] = 1
    await orch.run()

    block = emitter.report["classify"]
    assert block == {"assignment": "single",
                     "classes": {"faq": 4, "chat": 0, "other": 0},
                     "fallback_count": 2, "failures": 1}
    assert "multi_label_records" not in block


async def test_report_classify_block_multi_has_multi_label_records(tmp_path):
    ccfg = classify_cfg(assignment="multi", classes=("faq", "chat"))
    fan = {f"{2:016x}": ("chat",)}
    cfg = make_cfg(tmp_path, batch_size=4, classify=ccfg)
    orch, _, emitter, _ = build(cfg, [StubClassifyStage(labels=("faq",), fan=fan)],
                                [rec(1), rec(2)])
    await orch.run()
    block = emitter.report["classify"]
    assert block["assignment"] == "multi"
    assert block["multi_label_records"] == 1
    assert block["classes"] == {"faq": 2, "chat": 1}


class PoolTieScoreStage(ScoreStage):
    """ScoreStage that also feeds the POOL-DIMENSIONED tie counters the real
    per-pool pairwise composition uses when classify is enabled (R12)."""

    async def run(self, batch, ctx):
        ctx.metrics.count("quality.tie_outcomes.faq.clarity", 2)
        ctx.metrics.count("quality.tie_comparisons.faq.clarity", 8)
        return await super().run(batch, ctx)


async def test_report_quality_by_class_shape_and_top_level_preserved(tmp_path):
    """R12/R14: by_class carries per-pool effective mode/rounds + stats keyed
    by the pool-dimensioned counters; the TOP-LEVEL mode/rounds keep the
    globally-inherited base values and tie_rate aggregates across pools."""
    ccfg = classify_cfg(classes=("chat", "faq"))
    cfg = make_cfg(tmp_path, batch_size=4, dedup=False,
                   quality_cfg=QualityConfig(enabled=True, mode="pairwise", rounds=4),
                   classify=ccfg)
    cfg = with_views(cfg, overrides={
        "chat": {"quality": QualityConfig(enabled=True, mode="pointwise", rounds=2)}})
    # round-robin faq/chat → records 1,3 → faq; 2,4 → chat
    stub = StubClassifyStage(labels=("faq", "chat"))
    scores = {f"{1:016x}": 0.05, f"{2:016x}": 0.95,
              f"{3:016x}": 0.55, f"{4:016x}": 0.75}
    orch, _, emitter, _ = build(cfg, [stub, PoolTieScoreStage(scores)],
                                [rec(i) for i in range(1, 5)])
    await orch.run()

    q = emitter.report["quality"]
    # top level: globally-inherited base values, cross-pool tie aggregate
    assert q["mode"] == "pairwise_bt" and q["rounds"] == 4
    assert q["per_criterion_tie_rate"] == {"clarity": 0.25}
    assert q["aggregate_histogram"]["0.0-0.1"] == 1     # flat totals unchanged
    assert q["aggregate_histogram"]["0.9-1.0"] == 1

    by_class = q["by_class"]
    assert set(by_class) == {"faq", "chat"}
    for pool_block in by_class.values():
        assert set(pool_block) == {"mode", "rounds", "aggregate_histogram",
                                   "per_criterion_mean", "per_criterion_tie_rate"}
    assert by_class["faq"]["mode"] == "pairwise_bt" and by_class["faq"]["rounds"] == 4
    assert by_class["chat"]["mode"] == "pointwise" and by_class["chat"]["rounds"] == 2
    assert by_class["faq"]["aggregate_histogram"]["0.0-0.1"] == 1
    assert by_class["faq"]["aggregate_histogram"]["0.5-0.6"] == 1
    assert by_class["chat"]["aggregate_histogram"]["0.9-1.0"] == 1
    assert by_class["chat"]["aggregate_histogram"]["0.7-0.8"] == 1
    assert by_class["faq"]["per_criterion_mean"]["clarity"] == pytest.approx(0.3)
    assert by_class["chat"]["per_criterion_mean"]["clarity"] == pytest.approx(0.85)
    assert by_class["faq"]["per_criterion_tie_rate"] == {"clarity": 0.25}
    assert by_class["chat"]["per_criterion_tie_rate"] == {}


async def test_tie_rate_gate_any_pairwise_pool_with_global_pointwise(tmp_path):
    """R14: tie_rate emission is gated on 'a pairwise pool exists OR the global
    mode is pairwise' — a pointwise global with one pairwise pool still emits."""
    ccfg = classify_cfg(classes=("chat", "faq"))
    cfg = make_cfg(tmp_path, batch_size=4, dedup=False,
                   quality_cfg=QualityConfig(enabled=True, mode="pointwise", rounds=1),
                   classify=ccfg)
    cfg = with_views(cfg, overrides={
        "faq": {"quality": QualityConfig(enabled=True, mode="pairwise", rounds=4)}})
    stub = StubClassifyStage(labels=("faq",))
    orch, _, emitter, _ = build(cfg, [stub, PoolTieScoreStage({})],
                                [rec(1), rec(2)])
    await orch.run()

    q = emitter.report["quality"]
    assert q["mode"] == "pointwise"                # top level keeps the global
    assert q["per_criterion_tie_rate"] == {"clarity": 0.25}
    assert q["by_class"]["faq"]["per_criterion_tie_rate"] == {"clarity": 0.25}


async def test_classify_disabled_quality_report_shape_unchanged(tmp_path):
    """Zero-change anchor: classify off keeps the flat tie counters, no
    by_class key, and the pointwise mode emits no tie_rate at all."""
    cfg = make_cfg(tmp_path, batch_size=4, dedup=False,
                   quality_cfg=QualityConfig(enabled=True, mode="pointwise"))
    orch, _, emitter, _ = build(cfg, [ScoreStage({})], [rec(1)])
    await orch.run()
    assert "by_class" not in emitter.report["quality"]
    assert "per_criterion_tie_rate" not in emitter.report["quality"]


async def test_generate_bucket_whitelist_includes_rejected_by_validator(tmp_path):
    """Bug fix regression (spec v1.7 §6): the report bucket field whitelist
    dropped rejected_by_validator — the M6 counter must now reach report.json;
    zero-init keeps three fields, the fourth appears only when counted."""

    class BucketGenStage(PureGenerateStage):
        async def run(self, batch, ctx):
            ctx.metrics.count("generate.buckets.default×concise.calls", 1)
            ctx.metrics.count("generate.buckets.default×concise.produced", 3)
            ctx.metrics.count("generate.buckets.default×concise.survived_dedup", 2)
            ctx.metrics.count("generate.buckets.default×concise.rejected_by_validator", 1)
            ctx.metrics.count("generate.buckets.default×plain.calls", 1)
            ctx.metrics.count("generate.buckets.default×plain.produced", 2)
            ctx.metrics.count("generate.buckets.default×plain.survived_dedup", 2)
            return await super().run(batch, ctx)

    gen_cfg = GenerateConfig(enabled=True, instruction="生成")
    cfg = make_cfg(tmp_path, batch_size=8, quality=True, generate=gen_cfg,
                   quality_cfg=QualityConfig(enabled=True))
    orch, _, emitter, _ = build(cfg, [ExactDedupStage(), ScoreStage({}),
                                      BucketGenStage(per_batch=1)],
                                [rec(1), rec(2)])
    await orch.run()

    buckets = emitter.report["generate"]["buckets"]
    assert buckets["default×concise"] == {"calls": 1, "produced": 3,
                                          "survived_dedup": 2,
                                          "rejected_by_validator": 1}
    # validator not configured for this bucket → three-field zero-init shape
    assert buckets["default×plain"] == {"calls": 1, "produced": 2,
                                        "survived_dedup": 2}


async def test_dry_run_process_classify_calls_formula(tmp_path, capsys):
    """R11 process mode: classify_calls = ingested × max(1, sc); single
    assignment with zero-override views prints NO R28 note."""
    cfg = make_cfg(tmp_path, batch_size=4, dry_run=True, annotate=True,
                   classify=classify_cfg(sc=3))
    cfg = with_views(cfg)                          # zero-override views
    orch, _, _, _ = build(cfg, [], [rec(i) for i in range(1, 11)])
    summary = await orch.run()

    assert summary.exit_code == 0
    err = capsys.readouterr().err
    assert "classify_calls=30" in err              # 10 ingested × sc 3
    assert "annotate_calls=10" in err
    assert "total=40" in err                       # classify counted into total
    assert "按全局配置估算" not in err               # no overrides, no multi


async def test_dry_run_generate_only_classify_calls_and_multi_note(tmp_path, capsys):
    """R11 generate_only: classify_calls = <generated records> × max(1, sc);
    multi assignment triggers the R28 lower-bound note."""
    gen_cfg = GenerateConfig(enabled=True, instruction="生成",
                             seed_examples=("a", "b", "c"),
                             num_per_record=2, num_per_call=4)
    cfg = make_cfg(tmp_path, mode="generate_only", batch_size=4, dry_run=True,
                   annotate=True, generate=gen_cfg,
                   classify=classify_cfg(assignment="multi"))
    cfg = with_views(cfg)
    orch, _, _, _ = build(cfg, [])
    await orch.run()

    err = capsys.readouterr().err
    assert "generate_calls=2" in err               # ceil(3×2/4)
    assert "classify_calls=8" in err               # 8 generated × max(1, 0)
    assert "annotate_calls=8" in err
    assert "total=18" in err
    assert "按全局配置估算 / multi 按标签乘数 1 报下界" in err


async def test_dry_run_class_override_triggers_note_without_multi(tmp_path, capsys):
    """R28: a [class.*] override alone (single assignment) also flags the
    estimate as computed on the global config."""
    cfg = make_cfg(tmp_path, batch_size=4, dry_run=True, annotate=True,
                   classify=classify_cfg())
    cfg = with_views(cfg, overrides={
        "chat": {"annotate": AnnotateConfig(enabled=True, instruction="按类标注")}})
    orch, _, _, _ = build(cfg, [], [rec(1), rec(2)])
    await orch.run()
    err = capsys.readouterr().err
    assert "classify_calls=2" in err
    assert "按全局配置估算 / multi 按标签乘数 1 报下界" in err


# ── E2E-finding fixes: P4-9 tie rate / P4-10 circuit_broken flag ─────────────

class TieCountingStage(ScoreStage):
    """ScoreStage that also feeds the per-criterion tie counters like the real
    pairwise composition loop does."""

    async def run(self, batch, ctx):
        ctx.metrics.count("quality.tie_outcomes.clarity", 2)
        ctx.metrics.count("quality.tie_comparisons.clarity", 8)
        return await super().run(batch, ctx)


async def test_report_pairwise_tie_rate_and_breaker_flag(tmp_path):
    cfg = make_cfg(tmp_path, batch_size=4, dedup=False,
                   quality_cfg=QualityConfig(enabled=True, mode="pairwise"))
    orch, _, emitter, _ = build(cfg, [TieCountingStage({})],
                                [rec(i) for i in range(1, 5)])
    await orch.run()
    q = emitter.report["quality"]
    assert q["per_criterion_tie_rate"] == {"clarity": 0.25}
    assert emitter.report["run"]["circuit_broken"] is False
    assert emitter.report["run"]["interrupted"] is False


async def test_report_pointwise_has_no_tie_rate(tmp_path):
    cfg = make_cfg(tmp_path, batch_size=4, dedup=False,
                   quality_cfg=QualityConfig(enabled=True, mode="pointwise"))
    orch, _, emitter, _ = build(cfg, [ScoreStage({})], [rec(1)])
    await orch.run()
    assert "per_criterion_tie_rate" not in emitter.report["quality"]


async def test_process_prescan_runs_before_run_start(tmp_path):
    # P2-4: the input scan must happen before the first trace emit so a bad
    # input path can never truncate the previous run's trace.
    cfg = make_cfg(tmp_path, dedup=False,
                   quality_cfg=QualityConfig(enabled=True, mode="pointwise"))
    orch, metrics, _, ingestor = build(cfg, [ScoreStage({})], [rec(1)])
    await orch.run()
    assert ingestor.scan_called
    assert metrics.events[0][0] == "run.start"


async def test_finalize_honors_sink_breaker_even_without_escape(tmp_path):
    """The breaker can open on a batch's tail calls while every coroutine fails
    record-level (queued calls never re-enter complete()) — CircuitBreakerTripped
    then never escapes a stage. finalize must still read the sink flag: exit 4,
    circuit_broken=true — and (v1.6 熔断交付) still deliver completed batches
    with the partial_delivery marker."""
    cfg = make_cfg(tmp_path, batch_size=4, dedup=False, fatal_threshold=1,
                   quality_cfg=QualityConfig(enabled=True, mode="pointwise"))

    class TrippingStage(ScoreStage):
        async def run(self, batch, ctx):
            ctx.metrics.record_provider_result(fatal=True)   # opens the breaker
            return await super().run(batch, ctx)

    orch, metrics, emitter, _ = build(cfg, [TrippingStage({})], [rec(1)])
    summary = await orch.run()
    assert metrics.circuit_broken
    assert summary.exit_code == 4
    assert emitter.report["run"]["circuit_broken"] is True
    assert emitter.deliver is True                 # v1.6: trip delivers
    assert emitter.report["run"]["partial_delivery"] is True
    assert "unprocessed" in emitter.report["counts"]


# ── tests: v1.8 stream orchestration (chain / packing / metering / report) ──

def stream_counts_invariant(counts):
    # v1.8 fully expanded form (spec 6.4): emitted + dropped_dup + dropped_lowq
    # + dropped_verify + dropped_noise + failed + bad_input + absorbed
    # [+ unprocessed] = scanned + generated [+ fanout] + episodes.
    lhs = (counts["emitted"] + counts["dropped_dup"] + counts["dropped_lowq"]
           + counts["dropped_verify"] + counts["dropped_noise"] + counts["failed"]
           + counts["bad_input"] + counts["absorbed"]
           + counts.get("unprocessed", 0))
    rhs = (counts["scanned"] + counts["generated"] + counts.get("fanout", 0)
           + counts["episodes"])
    return lhs == rhs


def stream_cfg(tmp_path, **kw):
    """make_cfg with segment enabled (defaults: hybrid strategy, window 20)."""
    kw.setdefault("segment", SegmentConfig(enabled=True))
    return make_cfg(tmp_path, **kw)


class Probe:
    """Named pass-through stage recording execution order."""

    def __init__(self, name, order):
        self.name = name
        self._order = order

    async def run(self, batch, ctx):
        self._order.append(self.name)
        return batch


async def test_chain_order_v18_superset_segment_and_extract_enabled(tmp_path):
    """v1.8 canonical order: segment → dedup → classify → extract → quality →
    annotate → verify regardless of the supplied stage list order (generate is
    mutually exclusive with segment and never co-occupies the chain)."""
    order: list[str] = []
    cfg = stream_cfg(tmp_path, batch_size=8, dedup=True, annotate=True,
                     verify=True, quality_cfg=QualityConfig(enabled=True),
                     classify=classify_cfg(),
                     extract=ExtractConfig(enabled=True))
    stages = [Probe(n, order) for n in ("verify", "extract", "annotate",
                                        "quality", "classify", "dedup",
                                        "segment")]
    ingestor = FakeSessionIngestor([sess("s1", 1, 2)])
    orch, _, _, _ = build(cfg, stages, ingestor=ingestor)
    await orch.run()
    assert order == ["segment", "dedup", "classify", "extract", "quality",
                     "annotate", "verify"]


async def test_chain_order_v18_degrades_to_v17_when_disabled(tmp_path):
    """segment/extract off: even with segment/extract stages SUPPLIED, the
    composed chain is the byte-identical v1.7 six-name form (minus generate)."""
    order: list[str] = []
    cfg = make_cfg(tmp_path, batch_size=8, dedup=True, annotate=True,
                   verify=True, quality_cfg=QualityConfig(enabled=True),
                   classify=classify_cfg())
    stages = [Probe(n, order) for n in ("verify", "extract", "annotate",
                                        "quality", "classify", "dedup",
                                        "segment")]
    orch, _, _, _ = build(cfg, stages, [rec(1), rec(2)])
    await orch.run()
    assert order == ["dedup", "classify", "quality", "annotate", "verify"]


async def test_stream_next_fit_packing_whole_sessions(tmp_path):
    """S21 next-fit: sessions [5, 3, 4] frames at batch_size=8 pack as
    [5+3][4] — one open bin, a session that no longer fits closes the batch."""
    cfg = stream_cfg(tmp_path, batch_size=8)
    stage = RecordingStage("dedup")
    ingestor = FakeSessionIngestor([sess("s1", 1, 5), sess("s2", 11, 3),
                                    sess("s3", 21, 4)])
    orch, metrics, emitter, _ = build(cfg, [stage], ingestor=ingestor)
    summary = await orch.run()

    assert [len(ids) for _, ids in stage.calls] == [8, 4]
    # whole sessions, arrival order: batch 1 = s1 then s2 frames, batch 2 = s3
    assert stage.calls[0][1] == tuple(f"{i:016x}" for i in (1, 2, 3, 4, 5,
                                                            11, 12, 13))
    assert stage.calls[1][1] == tuple(f"{i:016x}" for i in (21, 22, 23, 24))
    starts = [e for e in metrics.events if e[0] == "batch.start"]
    assert [e[4]["size"] for e in starts] == [8, 4]
    assert summary.exit_code == 0
    assert stream_counts_invariant(emitter.report["counts"])


async def test_stream_oversized_session_hard_split_and_marks(tmp_path, caplog):
    """S21 hard split: a session longer than batch_size is sliced at
    batch_size, each slice its own batch; every frame envelope of the split
    session carries the duck-typed session_split mark; ONE WARN per run even
    with several oversized sessions."""

    class SplitProbe:
        name = "dedup"

        def __init__(self):
            self.batches: list[list[tuple[str | None, bool]]] = []

        async def run(self, batch, ctx):
            self.batches.append([(it.session_id,
                                  getattr(it, "session_split", False))
                                 for it in batch])
            return batch

    cfg = stream_cfg(tmp_path, batch_size=8)
    probe = SplitProbe()
    ingestor = FakeSessionIngestor([sess("s1", 1, 10), sess("s2", 21, 9)])
    with caplog.at_level(logging.WARNING, logger=".".join(("labelkit", "orchestrator"))):
        orch, _, emitter, _ = build(cfg, [probe], ingestor=ingestor)
        summary = await orch.run()

    assert [len(b) for b in probe.batches] == [8, 2, 8, 1]
    for b in probe.batches:
        assert all(split is True for _, split in b)
    assert probe.batches[0][0][0] == "s1" and probe.batches[3][0][0] == "s2"
    assert sum("硬切" in r.message for r in caplog.records) == 1
    assert summary.output_lines == 19
    assert stream_counts_invariant(emitter.report["counts"])


async def test_stream_no_split_mark_without_oversize(tmp_path):
    """Sessions within batch_size never carry the session_split mark."""

    class MarkProbe:
        name = "dedup"

        def __init__(self):
            self.marks: list[bool] = []

        async def run(self, batch, ctx):
            self.marks.extend(getattr(it, "session_split", False)
                              for it in batch)
            return batch

    cfg = stream_cfg(tmp_path, batch_size=8)
    probe = MarkProbe()
    ingestor = FakeSessionIngestor([sess("s1", 1, 3), sess("s2", 11, 5)])
    orch, _, _, _ = build(cfg, [probe], ingestor=ingestor)
    await orch.run()
    assert probe.marks == [False] * 8


async def test_stream_session_id_stamped_on_frame_envelopes(tmp_path):
    """S4: M10 stamps PipelineItem.session_id at envelope construction."""
    cfg = stream_cfg(tmp_path, batch_size=8)
    stub = StubSegmentStage()
    ingestor = FakeSessionIngestor([sess("s1", 1, 2), sess("s2", 11, 1)])
    orch, _, _, _ = build(cfg, [stub], ingestor=ingestor)
    await orch.run()
    # captured at segment ENTRY: the three frame envelopes, stamped in order
    assert stub.calls == [(1, ("s1", "s1", "s2"))]


async def test_stream_episodes_metered_as_segment_len_delta(tmp_path):
    """§7.9: counts.episodes = len(batch) delta across the segment stage
    (fanout-isomorphic R9 construction) — the stub absorbs two sessions'
    frames and tail-appends 2 episode envelopes."""
    cfg = stream_cfg(tmp_path, batch_size=8)
    ingestor = FakeSessionIngestor([sess("s1", 1, 2), sess("s2", 11, 2)])
    orch, metrics, emitter, _ = build(cfg, [StubSegmentStage()],
                                      ingestor=ingestor)
    summary = await orch.run()

    assert metrics.counters["counts.episodes"] == 2
    counts = emitter.report["counts"]
    assert counts["episodes"] == 2
    assert counts["absorbed"] == 4
    assert counts["dropped_noise"] == 0
    assert counts["emitted"] == 2                  # the two episode envelopes
    assert summary.output_lines == 2
    assert stream_counts_invariant(counts)


async def test_stream_failed_fallback_formula_excludes_absorbed_and_noise(tmp_path):
    """§7.9 failed fallback: without the absorbed/dropped_noise terms the
    absorbed members would be miscounted as failed."""
    cfg = stream_cfg(tmp_path, batch_size=8)
    noise_id = f"{3:016x}"
    ingestor = FakeSessionIngestor([sess("s1", 1, 4)])
    orch, _, emitter, _ = build(cfg, [StubSegmentStage(noise=(noise_id,))],
                                ingestor=ingestor)
    await orch.run()

    counts = emitter.report["counts"]
    assert counts["failed"] == 0                   # not 4 (absorbed misread)
    assert counts["absorbed"] == 3
    assert counts["dropped_noise"] == 1
    assert counts["episodes"] == 1
    assert counts["emitted"] == 1
    assert stream_counts_invariant(counts)


async def test_stream_batch_end_carries_three_keys_only_when_enabled(tmp_path):
    """batch.end gains episodes/absorbed/dropped_noise ONLY when segment is
    enabled (R20 form) — the disabled path keeps the v1.7 payload byte-shape."""
    cfg = stream_cfg(tmp_path, batch_size=8)
    noise_id = f"{3:016x}"
    ingestor = FakeSessionIngestor([sess("s1", 1, 4)])
    orch, metrics, _, _ = build(cfg, [StubSegmentStage(noise=(noise_id,))],
                                ingestor=ingestor)
    await orch.run()
    ends = [e for e in metrics.events if e[0] == "batch.end"]
    assert [(e[4]["episodes"], e[4]["absorbed"], e[4]["dropped_noise"])
            for e in ends] == [(1, 3, 1)]

    cfg_off = make_cfg(tmp_path, batch_size=8)
    orch2, metrics2, _, _ = build(cfg_off, [ExactDedupStage()], [rec(1)])
    await orch2.run()
    ends2 = [e for e in metrics2.events if e[0] == "batch.end"]
    assert ends2 and all(
        k not in e[4] for e in ends2
        for k in ("episodes", "absorbed", "dropped_noise"))


async def test_stream_report_block_shape_and_counts_gating(tmp_path):
    """§9.3: with segment enabled counts gains the three keys and the report
    gains the stream block right after counts — base eight keys, extract/verify
    sub-blocks only when those stages are enabled, zero-based closed
    vocabularies fed from the M15/M7 counters."""
    cfg = stream_cfg(tmp_path, batch_size=8, verify=True,
                     extract=ExtractConfig(enabled=True))
    ingestor = FakeSessionIngestor([sess("s1", 1, 3), sess("s2", 11, 2)])
    orch, metrics, emitter, _ = build(cfg, [StubSegmentStage()],
                                      ingestor=ingestor)
    # stage-owned counters (M14/M15/M7), pre-fed like the classify block test
    metrics.counters["segment.below_min_len"] = 2
    metrics.counters["segment.digest_poor_frames"] = 1
    metrics.counters["segment.failures"] = 1
    metrics.counters["extract.transitions"] = 3
    metrics.counters["extract.fallback_steps"] = 1
    metrics.counters["extract.failures"] = 0
    metrics.counters["extract.by_type.click"] = 2
    metrics.counters["extract.by_type.scroll"] = 1
    metrics.counters["verify.membership_repairs"] = 1
    metrics.counters["verify.boundary_flags"] = 2
    metrics.counters["verify.defects.off_task_members"] = 1
    await orch.run()

    report = emitter.report
    counts = report["counts"]
    assert counts["episodes"] == 2 and counts["absorbed"] == 5
    assert counts["dropped_noise"] == 0
    keys = list(report)
    assert keys.index("stream") == keys.index("counts") + 1
    stream = report["stream"]
    assert set(stream) == {"sessions", "episodes", "mean_episode_len",
                           "absorbed", "dropped_noise", "below_min_len",
                           "digest_poor_frames", "segment_failures",
                           "extract", "verify"}
    assert stream["sessions"] == 2
    assert stream["episodes"] == 2
    assert stream["mean_episode_len"] == 2.5       # absorbed/episodes, round 2
    assert stream["absorbed"] == 5
    assert stream["dropped_noise"] == 0
    assert stream["below_min_len"] == 2
    assert stream["digest_poor_frames"] == 1
    assert stream["segment_failures"] == 1
    assert stream["extract"] == {
        "transitions": 3, "fallback_steps": 1, "failures": 0,
        "by_type": {"click": 2, "long_press": 0, "input_text": 0, "scroll": 1,
                    "drag": 0, "open_app": 0, "app_switch": 0,
                    "navigate_back": 0, "navigate_home": 0, "wait": 0,
                    "other": 0}}
    assert stream["verify"] == {
        "membership_repairs": 1, "boundary_flags": 2,
        "defects": {"label_mismatch": 0, "off_task_members": 1,
                    "missing_head": 0, "missing_tail": 0,
                    "missing_members": 0}}


async def test_stream_report_block_base_form_and_disabled_gating(tmp_path):
    """extract/verify off → no sub-blocks (eight base keys exactly);
    segment off → no stream block, no counts trio (regression anchor)."""
    cfg = stream_cfg(tmp_path, batch_size=8)
    ingestor = FakeSessionIngestor([sess("s1", 1, 2)])
    orch, _, emitter, _ = build(cfg, [StubSegmentStage()], ingestor=ingestor)
    await orch.run()
    stream = emitter.report["stream"]
    assert set(stream) == {"sessions", "episodes", "mean_episode_len",
                           "absorbed", "dropped_noise", "below_min_len",
                           "digest_poor_frames", "segment_failures"}
    assert stream["mean_episode_len"] == 2.0

    cfg_off = make_cfg(tmp_path, batch_size=8)
    orch2, _, emitter2, _ = build(cfg_off, [ExactDedupStage()], [rec(1)])
    await orch2.run()
    assert "stream" not in emitter2.report
    for key in ("episodes", "absorbed", "dropped_noise"):
        assert key not in emitter2.report["counts"]


async def test_stream_breaker_residual_includes_episodes_and_absorbed(tmp_path):
    """S18/R10: the breaker residual carries the expanded sides — + episodes
    on the source side, + absorbed + dropped_noise among the terminal counts.
    Batch 1 completes (absorbed tallied), batch 2 trips before emit."""
    cfg = stream_cfg(tmp_path, batch_size=4, fatal_threshold=2, annotate=True)
    ingestor = FakeSessionIngestor([sess("s1", 1, 3), sess("s2", 11, 2)])
    orch, _, emitter, _ = build(cfg, [StubSegmentStage(), BreakerStage()],
                                ingestor=ingestor)
    summary = await orch.run()

    assert summary.exit_code == 4
    assert emitter.report["run"]["partial_delivery"] is True
    counts = emitter.report["counts"]
    assert counts["episodes"] == 2                 # metered in BOTH batches
    assert counts["absorbed"] == 3                 # batch 1 only (batch 2 no emit)
    assert counts["failed"] == 1                   # batch 1's failed episode
    assert "unprocessed" in counts
    assert counts["unprocessed"] == 3              # s2's 2 frames + its episode
    lhs = (counts["emitted"] + counts["dropped_dup"] + counts["dropped_lowq"]
           + counts["dropped_verify"] + counts["dropped_noise"]
           + counts["failed"] + counts["bad_input"] + counts["absorbed"]
           + counts["unprocessed"])
    assert lhs == (counts["scanned"] + counts["generated"]
                   + counts["episodes"])


async def test_stream_interrupted_run_gains_unprocessed(tmp_path):
    """S18: in stream mode counts.unprocessed appears on 'breaker trip OR
    interrupted' — SIGINT over the session buffer strands in-flight records."""

    class StopDuringFirstBatch:
        name = "dedup"

        def __init__(self, orch_ref):
            self._orch_ref = orch_ref

        async def run(self, batch, ctx):
            if ctx.batch_no == 1:
                self._orch_ref[0]._request_stop()
            return batch

    cfg = stream_cfg(tmp_path, batch_size=2)
    orch_ref: list = [None]
    ingestor = FakeSessionIngestor([sess("s1", 1, 2), sess("s2", 11, 2),
                                    sess("s3", 21, 2)])
    orch, _, emitter, _ = build(cfg, [StopDuringFirstBatch(orch_ref)],
                                ingestor=ingestor)
    orch_ref[0] = orch
    summary = await orch.run()

    assert summary.interrupted is True
    assert summary.exit_code == 0                  # graceful: finalize + rename
    assert emitter.report["run"]["interrupted"] is True
    assert "partial_delivery" not in emitter.report["run"]
    counts = emitter.report["counts"]
    assert counts["emitted"] == 2                  # batch 1 only
    # s2 (buffered in the open bin) + s3 (pulled, never packed) stranded
    assert counts["unprocessed"] == 4
    assert stream_counts_invariant(counts)


async def test_non_stream_interrupted_run_has_no_unprocessed(tmp_path):
    """Regression anchor (S18): non-stream interrupted runs keep a zero
    residual and never emit the unprocessed key."""

    class StopDuringFirstBatch:
        name = "dedup"

        def __init__(self, orch_ref):
            self._orch_ref = orch_ref

        async def run(self, batch, ctx):
            if ctx.batch_no == 1:
                self._orch_ref[0]._request_stop()
            return batch

    cfg = make_cfg(tmp_path, batch_size=4)
    orch_ref: list = [None]
    orch, _, emitter, _ = build(cfg, [StopDuringFirstBatch(orch_ref)],
                                [rec(i) for i in range(1, 11)])
    orch_ref[0] = orch
    summary = await orch.run()

    assert summary.interrupted is True
    assert emitter.report["run"]["interrupted"] is True
    assert "unprocessed" not in emitter.report["counts"]
    assert "stream" not in emitter.report


async def test_dry_run_stream_estimate_formulas_and_note(tmp_path, capsys):
    """S22: segment_calls = Σ ceil((L−1)/(window−1)) over sessions with L ≥ 2;
    extract_calls = Σ (L−1); downstream bases use len(session_lens); the batch
    count comes from the exact next-fit packing simulation; the lower-bound
    note prints for the LLM-refining strategies."""
    cfg = stream_cfg(tmp_path, batch_size=8, dry_run=True, annotate=True,
                     segment=SegmentConfig(enabled=True, strategy="hybrid",
                                           window=20),
                     extract=ExtractConfig(enabled=True))
    ingestor = FakeSessionIngestor(session_lens=(21, 5))
    orch, _, emitter, _ = build(cfg, [], ingestor=ingestor)
    summary = await orch.run()

    assert summary.exit_code == 0
    err = capsys.readouterr().err
    assert "estimated_records=26" in err
    # next-fit simulation: 21 hard-splits to [8][8][5], then [5] → 4 batches
    assert "batches=4" in err
    assert "segment_calls=3" in err                # ceil(20/19) + ceil(4/19)
    assert "extract_calls=24" in err               # 20 + 4 (upper bound)
    assert "annotate_calls=2" in err               # episodes ≈ sessions
    assert "total=29" in err
    assert "stream 估算：下游按 episodes≈sessions 报下界（LLM 精化只增段数）" in err


async def test_dry_run_stream_rules_strategy_zero_segment_calls(tmp_path, capsys):
    """strategy='rules' costs zero segment calls and prints no lower-bound
    note (episodes == sessions exactly under rules)."""
    cfg = stream_cfg(tmp_path, batch_size=8, dry_run=True, annotate=True,
                     segment=SegmentConfig(enabled=True, strategy="rules"))
    ingestor = FakeSessionIngestor(session_lens=(21, 5))
    orch, _, _, _ = build(cfg, [], ingestor=ingestor)
    await orch.run()
    err = capsys.readouterr().err
    assert "segment_calls=0" in err
    assert "extract_calls=0" in err                # extract disabled
    assert "报下界（LLM 精化只增段数）" not in err


async def test_dry_run_non_stream_prints_zero_segment_and_extract_calls(tmp_path, capsys):
    """The two new estimate keys print unconditionally (classify precedent):
    0 with segment/extract disabled, non-stream branch otherwise unchanged."""
    cfg = make_cfg(tmp_path, batch_size=4, dry_run=True, annotate=True)
    orch, _, _, _ = build(cfg, [], [rec(i) for i in range(1, 11)])
    await orch.run()
    err = capsys.readouterr().err
    assert "segment_calls=0" in err
    assert "extract_calls=0" in err
    assert "annotate_calls=10" in err
    assert "total=10" in err


async def test_dry_run_extract_class_override_triggers_note(tmp_path, capsys):
    """S2 tie-in: a [class.<name>.extract] override alone counts as a per-class
    override — _class_overrides_exist compares view.extract too."""
    cfg = make_cfg(tmp_path, batch_size=4, dry_run=True, annotate=True,
                   classify=classify_cfg())
    cfg = with_views(cfg, overrides={
        "chat": {"extract": ExtractConfig(instruction="按类摘取")}})
    orch, _, _, _ = build(cfg, [], [rec(1), rec(2)])
    await orch.run()
    err = capsys.readouterr().err
    assert "按全局配置估算 / multi 按标签乘数 1 报下界" in err

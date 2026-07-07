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
import random
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from labelkit.config.model import (
    AnnotateConfig, ClassifyConfig, Criterion, DedupConfig, GenerateConfig,
    InputConfig, OutputConfig, QualityConfig, ResolvedConfig, Rubric, RunConfig,
    ToolConfig, TraceConfig, VerifyConfig,
)
from labelkit.errors import CircuitBreakerTripped
from labelkit.obslog import EventLog, MetricsSink, TraceEvent
from labelkit.orchestrator import Orchestrator, RunSummary
from labelkit.types import DedupInfo, PipelineItem, QualityScore, Record, RecordRef, StageError

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
        dedup=DedupConfig(enabled=dedup),
        classify=ClassifyConfig(),
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
    lhs = (counts["emitted"] + counts["dropped_dup"] + counts["dropped_lowq"]
           + counts["dropped_verify"] + counts["failed"] + counts["bad_input"])
    rhs = counts["scanned"] + counts["generated"]
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

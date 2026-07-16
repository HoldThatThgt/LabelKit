"""Offline unit tests for M2 stream-mode sessionization (spec 3.2.8, v1.8).

Covers S20 timestamp parsing, the S19 per-partition-key monotonicity check
with stream.on_disorder, the rule-layer session assembler (all close causes),
the S17 frame-level --limit, IngestReport.sessions/disorder, the S23 scan
fusion (IngestPlan.session_lens) and the segment.session / ingest.disorder
trace event payload shapes. Pure I/O + logic — no LLM.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from labelkit.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    DedupConfig,
    ExtractConfig,
    GenerateConfig,
    InputConfig,
    OutputConfig,
    QualityConfig,
    ResolvedConfig,
    Rubric,
    RunConfig,
    SegmentConfig,
    StreamConfig,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)
from labelkit.errors import InputError
from labelkit.ingest import Ingestor, Session, _parse_order_key

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

MINI_TREE = '{"id": "0", "class": "FrameLayout", "visible": true}\n'


def make_cfg(tmp_path: Path, *, modality: str = "text",
             stream: StreamConfig | None = None,
             segment: SegmentConfig | None = None,
             limit: int | None = None, **input_kw) -> ResolvedConfig:
    return ResolvedConfig(
        tool=ToolConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output=str(tmp_path / "out.jsonl"), modality=modality,
                      input=str(tmp_path / "in")),
        input=InputConfig(**input_kw),
        stream=stream if stream is not None else StreamConfig(),
        dedup=DedupConfig(),
        segment=segment if segment is not None else SegmentConfig(enabled=True),
        extract=ExtractConfig(),
        classify=ClassifyConfig(),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=TraceConfig(),
        rubric=Rubric(name="t", criteria=()),
        class_views={},
        user_schema={"type": "object"},
        limit=limit,
        strict=False,
        dry_run=False,
        config_path="config.toml",
        project_path="project.toml",
        config_digest="sha256:0",
        project_digest="sha256:0",
    )


class EventRecorder:
    """Same style as test_ingest.py: records emitted trace events."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None):
        assert stage == "ingest"
        assert batch_no == 0
        assert record_ids == ()
        self.events.append((ev, dict(payload or {})))


def write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ts_lines(*ts_values, device: str | None = None) -> list[str]:
    lines = []
    for ts in ts_values:
        obj = {"text": f"t-{ts}", "ts": ts}
        if device is not None:
            obj["device"] = device
        lines.append(json.dumps(obj, ensure_ascii=False))
    return lines


def run_sessions(cfg) -> tuple[list[Session], Ingestor, EventRecorder]:
    ing = Ingestor(cfg)
    rec = EventRecorder()
    ing.metrics = rec
    return list(ing.sessions()), ing, rec


# ── S20 timestamp parsing, all branches ──────────────────────────────────────

def test_parse_numeric_epoch_seconds():
    assert _parse_order_key(1_719_742_320) == 1_719_742_320.0
    assert _parse_order_key(0) == 0.0
    assert _parse_order_key(1_719_742_320.5) == 1_719_742_320.5
    # everything below 1e11 is seconds, as-is
    assert _parse_order_key(99_999_999_999.9) == 99_999_999_999.9


def test_parse_numeric_epoch_milliseconds():
    assert _parse_order_key(1_719_742_320_000) == 1_719_742_320.0
    # boundary: exactly 1e11 is the first millisecond value
    assert _parse_order_key(10**11) == 10**8


def test_parse_numeric_rejections():
    assert _parse_order_key(-1) is None                  # v < 0
    assert _parse_order_key(-0.5) is None
    assert _parse_order_key(10**14) is None              # v >= 1e14
    assert _parse_order_key(float("inf")) is None
    assert _parse_order_key(float("nan")) is None        # fits no S20 bucket
    assert _parse_order_key(True) is None                # bool is not a timestamp
    assert _parse_order_key(None) is None
    assert _parse_order_key([1]) is None
    assert _parse_order_key({"t": 1}) is None


def test_parse_string_numeric_follows_numeric_rules():
    assert _parse_order_key("1719742320") == 1_719_742_320.0
    assert _parse_order_key("1719742320000") == 1_719_742_320.0   # ms
    assert _parse_order_key("-5") is None
    assert _parse_order_key("100000000000000") is None            # 1e14
    assert _parse_order_key("nan") is None
    assert _parse_order_key("inf") is None


def test_parse_iso_with_z_suffix():
    expected = datetime(2026, 6, 30, 10, 12, 0, tzinfo=timezone.utc).timestamp()
    assert _parse_order_key("2026-06-30T10:12:00Z") == expected


def test_parse_iso_aware_offset():
    v = _parse_order_key("2026-06-30T18:12:00+08:00")
    assert v == datetime(2026, 6, 30, 10, 12, 0, tzinfo=timezone.utc).timestamp()


def test_parse_iso_naive_interpreted_as_utc():
    assert (_parse_order_key("2026-06-30T10:12:00")
            == _parse_order_key("2026-06-30T10:12:00Z"))


def test_parse_invalid_strings():
    assert _parse_order_key("not-a-time") is None
    assert _parse_order_key("") is None
    assert _parse_order_key("2026-13-45T99:99:99") is None


# ── S19 per-partition-key cursors & on_disorder ──────────────────────────────

def meta_stream_cfg(tmp_path, *, key=("meta:device",), on_disorder="skip",
                    gap_s=300, gap_steps=0, session_max_len=200,
                    session_max_span_s=0, limit=None):
    return make_cfg(
        tmp_path,
        stream=StreamConfig(order_by="meta:ts", on_disorder=on_disorder,
                            key=tuple(key), gap_s=gap_s, gap_steps=gap_steps,
                            session_max_len=session_max_len,
                            session_max_span_s=session_max_span_s),
        limit=limit,
    )


def test_per_key_cursors_grouped_input_each_monotonic(tmp_path):
    """Two keys, grouped: b restarts at a LOWER timestamp than a's cursor —
    per-partition cursors must not flag it; a's later group continues from
    a's own persistent cursor."""
    cfg = meta_stream_cfg(tmp_path)
    write_lines(tmp_path / "in" / "d.jsonl",
                ts_lines(100, 110, device="a")
                + ts_lines(5, 15, device="b")          # lower than a's 110 — fine
                + ts_lines(120, 130, device="a"))      # a resumes above its cursor
    sessions, ing, _ = run_sessions(cfg)
    assert ing.report.disorder == 0
    assert [s.cause for s in sessions] == ["key", "key", "eof"]
    assert [len(s.records) for s in sessions] == [2, 2, 2]
    assert ing.report.sessions == 3


def test_same_key_disorder_skipped_and_counted(tmp_path):
    """Same-key out-of-order record is skipped under the default skip policy:
    bad_input + disorder + bad_locations + per-record event; the session flow
    continues without it. Cursors persist across sessions (grouped input)."""
    cfg = meta_stream_cfg(tmp_path)
    write_lines(tmp_path / "in" / "d.jsonl",
                ts_lines(100, 110, device="a")
                + ts_lines(105, device="a")            # 105 < cursor 110 → disorder
                + ts_lines(50, 60, device="b")
                + ts_lines(90, device="a"))            # a cursor is STILL 110 → disorder
    sessions, ing, rec = run_sessions(cfg)
    rep = ing.report
    assert rep.disorder == 2
    assert rep.bad_input == 2                          # disorder ⊂ bad_input
    assert rep.sessions == 2
    assert [s.cause for s in sessions] == ["key", "eof"]
    assert [len(s.records) for s in sessions] == [2, 2]
    assert [loc["line_no"] for loc in rep.bad_locations] == [3, 6]
    assert all(loc["reason"].startswith("乱序：") for loc in rep.bad_locations)
    disorder_events = [p for ev, p in rec.events if ev == "ingest.disorder"]
    assert len(disorder_events) == 2


def test_timestamp_parse_failure_walks_disorder_path(tmp_path):
    cfg = meta_stream_cfg(tmp_path)
    lines = ts_lines(100, device="a") + [
        json.dumps({"text": "no-ts", "device": "a"}),               # field missing
        json.dumps({"text": "bad-ts", "ts": "next tuesday", "device": "a"}),
    ] + ts_lines(110, device="a")
    write_lines(tmp_path / "in" / "d.jsonl", lines)
    sessions, ing, rec = run_sessions(cfg)
    assert ing.report.disorder == 2
    assert [len(s.records) for s in sessions] == [2]
    reasons = [loc["reason"] for loc in ing.report.bad_locations]
    assert all(r.startswith("时间戳解析失败：") for r in reasons)
    assert "字段缺失" in reasons[0]
    assert "next tuesday" in reasons[1]                # timestamp value may enter reason


def test_on_disorder_fail_raises_input_error(tmp_path):
    cfg = meta_stream_cfg(tmp_path, on_disorder="fail")
    write_lines(tmp_path / "in" / "d.jsonl",
                ts_lines(100, 90, device="a"))
    ing = Ingestor(cfg)
    with pytest.raises(InputError, match=r'stream\.on_disorder = "fail"') as exc_info:
        list(ing.sessions())
    assert "d.jsonl:2" in str(exc_info.value)          # message carries the location
    assert ing.report.disorder == 1


def test_disorder_stderr_warn_only_once_per_run(tmp_path, caplog):
    cfg = meta_stream_cfg(tmp_path)
    write_lines(tmp_path / "in" / "d.jsonl",
                ts_lines(100, 90, 80, 110, device="a"))   # two disorder records
    ing = Ingestor(cfg)
    rec = EventRecorder()
    ing.metrics = rec
    with caplog.at_level(logging.WARNING, logger="labelkit.ingest"):
        sessions = list(ing.sessions())
    assert ing.report.disorder == 2
    assert len([ev for ev, _ in rec.events if ev == "ingest.disorder"]) == 2
    warns = [r for r in caplog.records if "ingest.disorder" in r.message]
    assert len(warns) == 1                             # ONE stderr WARN per run
    assert [len(s.records) for s in sessions] == [2]


# ── session close causes ─────────────────────────────────────────────────────

def test_cause_gap_s(tmp_path):
    cfg = meta_stream_cfg(tmp_path, key=(), gap_s=300)
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(0, 100, 500))
    sessions, ing, _ = run_sessions(cfg)               # 500-100=400 > 300 → gap
    assert [(s.cause, len(s.records)) for s in sessions] == [("gap", 2), ("eof", 1)]
    assert ing.report.sessions == 2


def test_cause_gap_steps_text_line_no(tmp_path):
    # blank lines advance line_no without being scanned: records sit on
    # lines 1 and 5 → step gap 4 > 3 → gap close
    cfg = make_cfg(tmp_path, stream=StreamConfig(gap_steps=3))
    write_lines(tmp_path / "in" / "d.jsonl",
                ['{"text": "a"}', "", "", "", '{"text": "b"}'])
    sessions, _, _ = run_sessions(cfg)
    assert [(s.cause, len(s.records)) for s in sessions] == [("gap", 1), ("eof", 1)]


def test_cause_gap_steps_ui_pair_index(tmp_path):
    root = tmp_path / "in"
    for index in (1, 2, 7):                            # 7-2=5 > 3 → gap
        root.mkdir(parents=True, exist_ok=True)
        (root / f"uitree_{index}.jsonl").write_text(MINI_TREE, encoding="utf-8")
        (root / f"image_{index}.png").write_bytes(PNG_MAGIC + b"x")
    cfg = make_cfg(tmp_path, modality="ui", stream=StreamConfig(gap_steps=3))
    sessions, _, _ = run_sessions(cfg)
    assert [(s.cause, len(s.records)) for s in sessions] == [("gap", 2), ("eof", 1)]
    assert [r.ref.pair_index for r in sessions[0].records] == [1, 2]


def test_cause_key_change(tmp_path):
    cfg = meta_stream_cfg(tmp_path, key=("meta:device",))
    write_lines(tmp_path / "in" / "d.jsonl",
                ts_lines(1, 2, device="a") + ts_lines(3, 4, device="b"))
    sessions, _, _ = run_sessions(cfg)
    assert [s.cause for s in sessions] == ["key", "eof"]


def test_cause_key_change_source_dir_ui(tmp_path):
    root = tmp_path / "in"
    for sub, index in (("x", 1), ("x", 2), ("y", 3)):
        d = root / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / f"uitree_{index}.jsonl").write_text(MINI_TREE, encoding="utf-8")
        (d / f"image_{index}.png").write_bytes(PNG_MAGIC + b"x")
    cfg = make_cfg(tmp_path, modality="ui",
                   stream=StreamConfig(key=("source_dir",)))
    sessions, _, _ = run_sessions(cfg)
    assert [(s.cause, len(s.records)) for s in sessions] == [("key", 2), ("eof", 1)]
    assert [r.ref.pair_index for r in sessions[1].records] == [3]


def test_cause_max_len_hard_close(tmp_path):
    cfg = meta_stream_cfg(tmp_path, key=(), session_max_len=2)
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(1, 2, 3, 4, 5))
    sessions, _, _ = run_sessions(cfg)
    assert [(s.cause, len(s.records)) for s in sessions] == \
        [("max_len", 2), ("max_len", 2), ("eof", 1)]


def test_cause_max_span(tmp_path):
    # adjacent gaps stay under gap_s; the span cap closes BEFORE the frame
    # that would push first→last beyond the limit, so emitted spans honor it
    cfg = meta_stream_cfg(tmp_path, key=(), gap_s=1000, session_max_span_s=100)
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(0, 90, 180, 270))
    sessions, _, _ = run_sessions(cfg)
    assert [(s.cause, len(s.records)) for s in sessions] == \
        [("max_span", 2), ("eof", 2)]


def test_cause_eof(tmp_path):
    cfg = meta_stream_cfg(tmp_path, key=())
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(1, 2))
    sessions, _, _ = run_sessions(cfg)
    assert [s.cause for s in sessions] == ["eof"]


def test_cause_limit_flushes_tail_and_warns_once(tmp_path, caplog):
    cfg = meta_stream_cfg(tmp_path, key=(), limit=3)
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(1, 2, 3, 4, 5))
    ing = Ingestor(cfg)
    with caplog.at_level(logging.WARNING, logger="labelkit.ingest"):
        sessions = list(ing.sessions())
    assert [(s.cause, len(s.records)) for s in sessions] == [("limit", 3)]
    assert ing.report.sessions == 1
    # D3: the WARN states budget exhaustion, never claims truncation (exact
    # exhaustion at EOF is indistinguishable without an extra pull).
    warns = [r for r in caplog.records if "--limit 预算耗尽处闭合" in r.message]
    assert len(warns) == 1
    # frame-level limit: only 3 records were ever consumed from the parse stream
    assert ing.report.ingested == 3


def test_limit_not_reached_is_plain_eof(tmp_path, caplog):
    cfg = meta_stream_cfg(tmp_path, key=(), limit=10)
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(1, 2))
    ing = Ingestor(cfg)
    with caplog.at_level(logging.WARNING, logger="labelkit.ingest"):
        sessions = list(ing.sessions())
    assert [s.cause for s in sessions] == ["eof"]
    assert not [r for r in caplog.records if "截断" in r.message]


def test_text_input_order_file_boundary_closes_session(tmp_path):
    """input_order text: a source-file change always closes the session
    (cause="key") — no timestamp can bridge the file boundary."""
    cfg = make_cfg(tmp_path, stream=StreamConfig())    # order_by="input_order"
    write_lines(tmp_path / "in" / "a.jsonl", ['{"text": "a1"}', '{"text": "a2"}'])
    write_lines(tmp_path / "in" / "b.jsonl", ['{"text": "b1"}'])
    sessions, ing, _ = run_sessions(cfg)
    assert [(s.cause, len(s.records)) for s in sessions] == [("key", 2), ("eof", 1)]
    assert [r.ref.source_file for r in sessions[0].records] == ["a.jsonl", "a.jsonl"]
    assert sessions[1].records[0].ref.source_file == "b.jsonl"


def test_meta_order_file_boundary_is_transparent(tmp_path):
    """meta:* ordering: file boundaries do NOT close sessions (rotated logs)."""
    cfg = meta_stream_cfg(tmp_path, key=())
    write_lines(tmp_path / "in" / "a.jsonl", ts_lines(1, 2))
    write_lines(tmp_path / "in" / "b.jsonl", ts_lines(3, 4))
    sessions, _, _ = run_sessions(cfg)
    assert [(s.cause, len(s.records)) for s in sessions] == [("eof", 4)]


# ── Session object invariants ────────────────────────────────────────────────

def test_session_id_deterministic_and_matches_formula(tmp_path):
    cfg = meta_stream_cfg(tmp_path, key=())
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(1, 2, 3))
    sessions_a, _, _ = run_sessions(cfg)
    sessions_b, _, _ = run_sessions(make_cfg(
        tmp_path, stream=StreamConfig(order_by="meta:ts", key=())))
    assert [s.session_id for s in sessions_a] == [s.session_id for s in sessions_b]
    s = sessions_a[0]
    expected = hashlib.sha256(
        "\n".join(r.id for r in s.records).encode("utf-8")).hexdigest()[:16]
    assert s.session_id == expected
    assert len(s.session_id) == 16


def test_identical_content_sessions_get_distinct_ids(tmp_path):
    """D2 collision guard: record ids are content hashes, so two sessions with
    byte-identical members would derive the same session_id and be silently
    MERGED by M14's session_id regrouping once packed into one batch. The
    first occurrence keeps the plain derivation (id stability); repeats fold a
    per-run ordinal into the hash."""
    cfg = make_cfg(tmp_path, stream=StreamConfig(
        order_by="input_order", key=(), session_max_len=2))
    # 4 byte-identical lines → max_len closes two sessions with identical
    # member id sequences.
    write_lines(tmp_path / "in" / "d.jsonl",
                ['{"text": "同一行"}'] * 4)
    sessions, _, _ = run_sessions(cfg)
    assert len(sessions) == 2
    assert sessions[0].session_id != sessions[1].session_id
    # first occurrence keeps the plain content-derived id
    expected = hashlib.sha256(
        "\n".join(r.id for r in sessions[0].records).encode("utf-8")
    ).hexdigest()[:16]
    assert sessions[0].session_id == expected
    # deterministic across runs
    rerun, _, _ = run_sessions(cfg)
    assert [s.session_id for s in rerun] == [s.session_id for s in sessions]


def test_session_preserves_frame_order(tmp_path):
    cfg = meta_stream_cfg(tmp_path, key=())
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(10, 20, 30, 40))
    sessions, _, _ = run_sessions(cfg)
    (s,) = sessions
    assert [r.ref.line_no for r in s.records] == [1, 2, 3, 4]
    assert [json.loads(json.dumps(r.raw))["ts"] for r in s.records] == [10, 20, 30, 40]
    assert isinstance(s.records, tuple)


def test_session_is_frozen(tmp_path):
    import dataclasses
    cfg = meta_stream_cfg(tmp_path, key=())
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(1))
    sessions, _, _ = run_sessions(cfg)
    with pytest.raises(dataclasses.FrozenInstanceError):
        sessions[0].cause = "gap"  # type: ignore[misc]


def test_mixed_second_ms_iso_timestamps_one_stream(tmp_path):
    """S20 end to end: seconds, milliseconds and ISO forms interleave freely
    as long as the decoded epochs stay monotonic."""
    base = datetime(2026, 6, 30, 10, 12, 0, tzinfo=timezone.utc).timestamp()
    lines = [
        json.dumps({"text": "1", "ts": int(base)}),
        json.dumps({"text": "2", "ts": int((base + 10) * 1000)}),   # ms
        json.dumps({"text": "3", "ts": "2026-06-30T10:12:20Z"}),
        json.dumps({"text": "4", "ts": "2026-06-30T10:12:30"}),     # naive = UTC
    ]
    cfg = meta_stream_cfg(tmp_path, key=(), gap_s=60)
    write_lines(tmp_path / "in" / "d.jsonl", lines)
    sessions, ing, _ = run_sessions(cfg)
    assert ing.report.disorder == 0
    assert [(s.cause, len(s.records)) for s in sessions] == [("eof", 4)]


# ── report counters ──────────────────────────────────────────────────────────

def test_report_sessions_and_disorder_counters(tmp_path):
    cfg = meta_stream_cfg(tmp_path, key=("meta:device",), session_max_len=2)
    write_lines(tmp_path / "in" / "d.jsonl",
                ts_lines(1, 2, 3, device="a")          # max_len 2 → [1,2] + [3
                + ts_lines(2, device="a")              # disorder (2 < 3)
                + ts_lines(7, device="b"))             # key → 3] closes, then [7
    sessions, ing, _ = run_sessions(cfg)
    rep = ing.report
    assert rep.sessions == 3
    assert rep.disorder == 1
    assert rep.bad_input == 1
    assert rep.ingested == 5                           # parse-level count unchanged
    assert [s.cause for s in sessions] == ["max_len", "key", "eof"]
    assert rep.sessions == len(sessions)


# ── S23 scan fusion: IngestPlan.session_lens ────────────────────────────────

def test_scan_session_lens_stream_off_is_empty(tmp_path):
    cfg = make_cfg(tmp_path, segment=SegmentConfig(enabled=False))
    write_lines(tmp_path / "in" / "d.jsonl", ['{"text": "a"}', '{"text": "b"}'])
    plan = Ingestor(cfg).scan()
    assert plan.session_lens == ()
    assert plan.estimated_records == 2


def test_scan_session_lens_text_matches_real_run(tmp_path):
    """The fused single-pass rehearsal (line count + session dry-run) must
    agree with the real sessions() lengths, skipping bad lines and disorder
    records the same way; estimated_records still counts every non-blank line."""
    lines = (ts_lines(0, 100, device="a")
             + ["{not json"]                            # bad line — skipped
             + ts_lines(50, device="a")                 # disorder — skipped
             + ts_lines(600, device="a")                # gap (600-100 > 300)
             + ts_lines(610, device="b"))               # key change
    cfg = meta_stream_cfg(tmp_path)
    write_lines(tmp_path / "in" / "d.jsonl", lines)
    plan = Ingestor(cfg).scan()
    assert plan.estimated_records == 6                  # all non-blank lines
    sessions, _, _ = run_sessions(cfg)
    assert plan.session_lens == tuple(len(s.records) for s in sessions)
    assert plan.session_lens == (2, 1, 1)


def test_scan_session_lens_estimate_false_is_empty(tmp_path):
    cfg = meta_stream_cfg(tmp_path)
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(1, 2))
    plan = Ingestor(cfg).scan(estimate=False)
    assert plan.session_lens == ()
    assert plan.estimated_records == 0


def test_scan_session_lens_ui_from_pairing_table(tmp_path):
    root = tmp_path / "in"
    for index in (1, 2, 3, 10):                        # 10-3=7 > 4 → gap
        root.mkdir(parents=True, exist_ok=True)
        (root / f"uitree_{index}.jsonl").write_text(MINI_TREE, encoding="utf-8")
        (root / f"image_{index}.png").write_bytes(PNG_MAGIC + b"x")
    cfg = make_cfg(tmp_path, modality="ui", stream=StreamConfig(gap_steps=4))
    plan = Ingestor(cfg).scan()
    assert plan.session_lens == (3, 1)
    sessions, _, _ = run_sessions(cfg)
    assert plan.session_lens == tuple(len(s.records) for s in sessions)


def test_scan_session_lens_ui_source_dir_key(tmp_path):
    root = tmp_path / "in"
    for sub, index in (("x", 1), ("x", 2), ("y", 3)):
        d = root / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / f"uitree_{index}.jsonl").write_text(MINI_TREE, encoding="utf-8")
        (d / f"image_{index}.png").write_bytes(PNG_MAGIC + b"x")
    cfg = make_cfg(tmp_path, modality="ui",
                   stream=StreamConfig(key=("source_dir",)))
    plan = Ingestor(cfg).scan()
    assert plan.session_lens == (2, 1)


def test_scan_session_lens_text_max_len_and_multi_file(tmp_path):
    # input_order: file boundary closes; max_len splits inside a file
    cfg = make_cfg(tmp_path,
                   stream=StreamConfig(session_max_len=2))
    write_lines(tmp_path / "in" / "a.jsonl",
                ['{"text": "1"}', '{"text": "2"}', '{"text": "3"}'])
    write_lines(tmp_path / "in" / "b.jsonl", ['{"text": "4"}'])
    plan = Ingestor(cfg).scan()
    assert plan.session_lens == (2, 1, 1)
    sessions, _, _ = run_sessions(cfg)
    assert [(s.cause, len(s.records)) for s in sessions] == \
        [("max_len", 2), ("key", 1), ("eof", 1)]


# ── trace event payload shapes ───────────────────────────────────────────────

def test_segment_session_event_payload_meta_order(tmp_path):
    cfg = meta_stream_cfg(tmp_path, key=(), gap_s=300)
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(10, 20, 900))
    sessions, _, rec = run_sessions(cfg)
    evs = [p for ev, p in rec.events if ev == "segment.session"]
    assert len(evs) == 2
    first_ev = evs[0]
    assert set(first_ev) == {"session_id", "first", "last", "len", "cause"}
    assert first_ev["session_id"] == sessions[0].session_id
    assert first_ev["first"] == 10.0 and first_ev["last"] == 20.0   # epoch floats
    assert first_ev["len"] == 2 and first_ev["cause"] == "gap"
    assert evs[1]["first"] == 900.0 and evs[1]["cause"] == "eof"


def test_segment_session_event_payload_input_order_text(tmp_path):
    cfg = make_cfg(tmp_path, stream=StreamConfig())
    write_lines(tmp_path / "in" / "a.jsonl", ['{"text": "1"}', '{"text": "2"}'])
    _, _, rec = run_sessions(cfg)
    (payload,) = [p for ev, p in rec.events if ev == "segment.session"]
    assert payload["first"] == "a.jsonl:1"             # "file:line_no" presentation
    assert payload["last"] == "a.jsonl:2"
    assert payload["cause"] == "eof"


def test_segment_session_event_payload_ui(tmp_path):
    root = tmp_path / "in"
    for index in (4, 6):
        root.mkdir(parents=True, exist_ok=True)
        (root / f"uitree_{index}.jsonl").write_text(MINI_TREE, encoding="utf-8")
        (root / f"image_{index}.png").write_bytes(PNG_MAGIC + b"x")
    cfg = make_cfg(tmp_path, modality="ui", stream=StreamConfig())
    _, _, rec = run_sessions(cfg)
    (payload,) = [p for ev, p in rec.events if ev == "segment.session"]
    assert payload["first"] == 4 and payload["last"] == 6   # pair_index presentation
    assert payload["len"] == 2


def test_ingest_disorder_event_payload_text(tmp_path):
    cfg = meta_stream_cfg(tmp_path, key=())
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(100, 90))
    _, _, rec = run_sessions(cfg)
    (payload,) = [p for ev, p in rec.events if ev == "ingest.disorder"]
    assert set(payload) == {"file", "line_no", "reason"}
    assert payload["file"] == "d.jsonl" and payload["line_no"] == 2
    assert payload["reason"].startswith("乱序：")
    assert "90" in payload["reason"]                   # timestamp values may appear


def test_disorder_event_emitted_before_fail_raise(tmp_path):
    cfg = meta_stream_cfg(tmp_path, key=(), on_disorder="fail")
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(100, 90))
    ing = Ingestor(cfg)
    rec = EventRecorder()
    ing.metrics = rec
    with pytest.raises(InputError):
        list(ing.sessions())
    assert [ev for ev, _ in rec.events if ev == "ingest.disorder"] == ["ingest.disorder"]


# ── regression anchors ───────────────────────────────────────────────────────

def test_records_path_untouched_by_stream_fields(tmp_path):
    """records() is the non-stream regression anchor: stream config in place
    must not alter it; the new report fields default to zero there."""
    cfg = meta_stream_cfg(tmp_path)
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(100, 90, 80))  # "disorder" if streamed
    ing = Ingestor(cfg)
    recs = list(ing.records())
    assert len(recs) == 3                              # no monotonicity check here
    assert ing.report.sessions == 0
    assert ing.report.disorder == 0


def test_sessions_reuses_records_semantics_for_bad_lines(tmp_path):
    """Bad lines are handled by the records() layer (bad_line events/counts);
    the disorder layer only sees valid records."""
    cfg = meta_stream_cfg(tmp_path, key=())
    write_lines(tmp_path / "in" / "d.jsonl",
                ts_lines(1) + ["{broken"] + ts_lines(2))
    sessions, ing, rec = run_sessions(cfg)
    assert ing.report.bad_input == 1
    assert ing.report.disorder == 0
    assert [ev for ev, _ in rec.events] == \
        ["ingest.bad_line", "segment.session"]
    assert [len(s.records) for s in sessions] == [2]

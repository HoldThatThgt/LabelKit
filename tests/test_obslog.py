"""Offline unit tests for M12 obslog (pure logic: formats, filtering, redaction,
write-failure resilience). No LLM involved."""
from __future__ import annotations

import json
import logging
import re

import pytest

from labelkit import obslog
from labelkit.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    Criterion,
    DedupConfig,
    GenerateConfig,
    InputConfig,
    OutputConfig,
    QualityConfig,
    ResolvedConfig,
    Rubric,
    RunConfig,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)
from labelkit.obslog import (
    EV_CLASSIFY_DECISION,
    EV_ERROR,
    EV_RUN_START,
    EventLog,
    MetricsSink,
    TraceEvent,
    redact_payload,
    setup_logging,
)

# ── helpers ─────────────────────────────────────────────────────────────────


def make_cfg(tmp_path, *, tool: ToolConfig | None = None,
             trace: TraceConfig | None = None,
             input_cfg: InputConfig | None = None) -> ResolvedConfig:
    return ResolvedConfig(
        tool=tool or ToolConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output=str(tmp_path / "out.jsonl"), modality="text",
                      input=str(tmp_path), fatal_error_threshold=3),
        input=input_cfg or InputConfig(),
        dedup=DedupConfig(),
        classify=ClassifyConfig(),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=trace or TraceConfig(),
        rubric=Rubric(name="t", criteria=(
            Criterion(key="clarity", description="d", pairwise_prompt="p"),)),
        class_views={},
        user_schema={"type": "object"},
        limit=None,
        strict=False,
        dry_run=False,
        config_path="config.toml",
        project_path="project.toml",
        config_digest="sha256:0",
        project_digest="sha256:0",
    )


def ev(name: str, *, stage: str = "quality", batch_no: int = 1,
       record_ids: tuple[str, ...] = (), payload=None) -> TraceEvent:
    return TraceEvent(
        ts="2026-07-02T09:31:04.482+08:00",
        run_id="f3a9c04b7d21",
        batch_no=batch_no,
        stage=stage,
        ev=name,
        record_ids=record_ids,
        payload=payload or {},
    )


def open_log(tmp_path, **kw) -> tuple[EventLog, "pathlib.Path"]:
    path = tmp_path / "run.trace.jsonl"
    cfg = TraceConfig(enabled=True, path=str(path), **kw)
    return EventLog(cfg, "f3a9c04b7d21"), path


LONG_TEXT = "帮我写一条请假条，明天上午要去医院。" * 40   # well over 200 chars

JUDGMENT_PAYLOAD = {
    "order": {"A": "d5ad41d6357f8a55", "B": "1cda030abc565f17"},
    "model": "glm-5.2",
    "judgments": [
        {"criterion": "clarity", "winner": "B", "reason": "B 含明确时间与事由。" * 30},
        {"criterion": "style", "winner": "tie", "reason": "两者水平相当。"},
    ],
    "excerpt": {"1cda030abc565f17": LONG_TEXT, "d5ad41d6357f8a55": LONG_TEXT},
}

LLM_PAYLOAD = {
    "profile": "default",
    "gen_ai.request.model": "glm-5.2",
    "latency_ms": 812,
    "gen_ai.usage.input_tokens": 100,
    "gen_ai.usage.output_tokens": 50,
    "retries": 0,
    "status": "ok",
    "gen_ai.input.messages": [{"role": "user", "content": LONG_TEXT}],
    "gen_ai.output.messages": [{"role": "assistant", "content": "ok"}],
}


@pytest.fixture(autouse=True)
def _reset_labelkit_logger():
    """setup_logging sets propagate=False and adds handlers on the 'labelkit'
    logger; restore the pristine state so caplog keeps working across tests."""
    logger = logging.getLogger("labelkit")
    saved = (list(logger.handlers), logger.propagate, logger.level)
    yield
    logger.handlers[:] = saved[0]
    logger.propagate = saved[1]
    logger.setLevel(saved[2])


# ── trace line shape (§8.2) ─────────────────────────────────────────────────


def test_trace_line_has_exactly_seven_fields_in_order(tmp_path):
    log, path = open_log(tmp_path)
    log.emit(ev("quality.judgment", record_ids=("a", "b"),
                payload={"model": "glm-5.2"}))
    log.close()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert list(obj) == ["ts", "run_id", "batch_no", "stage", "ev",
                         "record_ids", "payload"]
    assert obj["record_ids"] == ["a", "b"]
    assert obj["batch_no"] == 1
    assert obj["run_id"] == "f3a9c04b7d21"
    assert log.events_written == 1
    assert log.dropped_events == 0


def test_run_start_header_carries_trace_schema_version(tmp_path):
    log, path = open_log(tmp_path)
    log.emit(ev(EV_RUN_START, stage="run", batch_no=0,
                payload={"tool_version": "labelkit/1.0.0",
                         "config_digest": "sha256:0",
                         "project_digest": "sha256:0",
                         "trace_schema_version": 1}))
    log.emit(ev("quality.gate", record_ids=("a",),
                payload={"aggregate": 0.7, "decision": "keep"}))
    log.close()
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["ev"] == "run.start"
    assert lines[0]["payload"]["trace_schema_version"] == 1
    assert "trace_schema_version" not in lines[1]["payload"]


def test_existing_trace_file_truncated_with_one_warning(tmp_path, caplog):
    path = tmp_path / "run.trace.jsonl"
    path.write_text("old content\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="labelkit"):
        log, _ = open_log(tmp_path)
    log.emit(ev("quality.gate", record_ids=("a",), payload={"decision": "keep"}))
    log.close()
    assert "old content" not in path.read_text(encoding="utf-8")
    assert sum("truncating" in r.message for r in caplog.records) == 1


def test_disabled_trace_is_noop(tmp_path):
    cfg = TraceConfig(enabled=False, path=str(tmp_path / "t.jsonl"))
    log = EventLog(cfg, "abc")
    log.emit(ev("quality.gate"))
    log.flush()
    log.close()
    assert log.events_written == 0
    assert log.dropped_events == 0
    assert not (tmp_path / "t.jsonl").exists()


# ── redaction tiers (§8.3) ──────────────────────────────────────────────────


def test_tier_none_drops_all_llm_free_text():
    out = redact_payload(JUDGMENT_PAYLOAD, "none")
    assert out["order"] == JUDGMENT_PAYLOAD["order"]
    assert out["model"] == "glm-5.2"
    assert [j["winner"] for j in out["judgments"]] == ["B", "tie"]
    assert all("reason" not in j for j in out["judgments"])
    assert "excerpt" not in out
    # critiques / violations dropped too
    assert "critiques" not in redact_payload(
        {"verdict": "pass", "round": 1,
         "critiques": [{"aspect": "a", "opinion": "o"}]}, "none")
    assert "violations" not in redact_payload(
        {"resolved_at": "l1", "violations": ["/x: type"]}, "none")


def test_tier_refs_keeps_llm_text_but_no_input_content():
    out = redact_payload(JUDGMENT_PAYLOAD, "refs")
    assert out["judgments"][0]["reason"].startswith("B 含明确时间与事由。")
    assert "excerpt" not in out
    llm_out = redact_payload(LLM_PAYLOAD, "refs")
    assert "gen_ai.input.messages" not in llm_out
    assert "gen_ai.output.messages" not in llm_out
    assert LONG_TEXT not in json.dumps(out, ensure_ascii=False)


def test_tier_excerpt_truncates_to_200_chars():
    out = redact_payload(JUDGMENT_PAYLOAD, "excerpt")
    assert set(out["excerpt"]) == set(JUDGMENT_PAYLOAD["excerpt"])
    for rid, text in out["excerpt"].items():
        assert len(text) == 200
        assert text == JUDGMENT_PAYLOAD["excerpt"][rid][:200]
    # gen_ai messages still absent at this tier
    assert "gen_ai.input.messages" not in redact_payload(LLM_PAYLOAD, "excerpt")


def test_tier_full_passes_messages_through():
    out = redact_payload(LLM_PAYLOAD, "full")
    assert out == LLM_PAYLOAD
    assert out["gen_ai.input.messages"][0]["content"] == LONG_TEXT


def test_tier_full_is_cumulative_keeps_truncated_excerpt():
    """§7.4 tiers are cumulative ("逐档递增"): "full" contains everything
    "excerpt" contains — the excerpt field survives, still 200-char truncated."""
    out = redact_payload(JUDGMENT_PAYLOAD, "full")
    assert set(out["excerpt"]) == set(JUDGMENT_PAYLOAD["excerpt"])
    for rid, text in out["excerpt"].items():
        assert len(text) == 200
        assert text == JUDGMENT_PAYLOAD["excerpt"][rid][:200]
    # reason free text kept too (refs ⊂ full)
    assert out["judgments"][0]["reason"].startswith("B 含明确时间与事由。")


def test_redaction_is_deterministic_and_nondestructive():
    a = redact_payload(JUDGMENT_PAYLOAD, "none")
    b = redact_payload(JUDGMENT_PAYLOAD, "none")
    assert a == b
    # original payload untouched
    assert "reason" in JUDGMENT_PAYLOAD["judgments"][0]
    assert "excerpt" in JUDGMENT_PAYLOAD


def test_refs_trace_file_contains_no_input_content(tmp_path):
    log, path = open_log(tmp_path, content="refs", channels=("quality",))
    log.emit(ev("quality.judgment", record_ids=("a", "b"),
                payload=JUDGMENT_PAYLOAD))
    log.close()
    data = path.read_text(encoding="utf-8")
    assert "帮我写一条请假条" not in data     # input excerpt never leaks at refs


# ── channel filtering (§8.1 rules) ─────────────────────────────────────────


def test_channel_filter_and_lifecycle_bypass(tmp_path):
    log, path = open_log(tmp_path, channels=("quality",))
    log.emit(ev(EV_RUN_START, stage="run", batch_no=0))          # bypass
    log.emit(ev("batch.start", stage="run", payload={"size": 4}))  # bypass
    log.emit(ev("dedup.duplicate", stage="dedup", record_ids=("x",),
                payload={"kind": "exact"}))                       # filtered
    log.emit(ev("quality.gate", record_ids=("a",),
                payload={"decision": "keep"}))                    # written
    log.close()
    events = [json.loads(l)["ev"] for l in path.read_text().splitlines()]
    assert events == ["run.start", "batch.start", "quality.gate"]
    assert log.dropped_events == 0    # filtered events are not "dropped"


def test_error_event_channel_is_the_producing_stage(tmp_path):
    log, path = open_log(tmp_path, channels=("quality",))
    log.emit(ev(EV_ERROR, stage="dedup",
                payload={"stage": "dedup", "kind": "image_decode_error",
                         "message": "m", "retryable": False}))    # filtered
    log.emit(ev(EV_ERROR, stage="quality",
                payload={"stage": "quality", "kind": "judgment_invalid",
                         "message": "m", "retryable": False}))    # written
    log.close()
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["stage"] == "quality"


# ── v1.7 classify channel (spec 7.2 / CONTRACTS §7.11, §8.1) ────────────────


def test_classify_decision_routes_through_classify_channel(tmp_path):
    """classify.decision follows the prefix rule: written only when "classify"
    is among trace.channels (NOT in the default set — user opt-in, R29)."""
    assert EV_CLASSIFY_DECISION == "classify.decision"    # §7.11 exact string
    # default channels (quality/verify/schema): filtered out
    log, path = open_log(tmp_path)
    log.emit(ev(EV_CLASSIFY_DECISION, stage="classify", record_ids=("r1",),
                payload={"label": "faq", "source": "llm"}))
    log.close()
    assert not path.exists()                    # nothing ever written
    assert log.dropped_events == 0              # filtered ≠ dropped

    # explicit "classify" channel: written with the payload intact
    log2, path2 = open_log(tmp_path, channels=("classify",))
    log2.emit(ev(EV_CLASSIFY_DECISION, stage="classify", record_ids=("r1",),
                 payload={"label": "faq", "labels": ["faq", "chat"],
                          "source": "llm", "sc": {"n": 3, "agreement_ratio": 1.0}}))
    log2.close()
    lines = [json.loads(l) for l in path2.read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["ev"] == "classify.decision"
    assert lines[0]["stage"] == "classify"
    assert lines[0]["payload"]["label"] == "faq"
    assert lines[0]["payload"]["labels"] == ["faq", "chat"]


def test_classify_stage_error_event_belongs_to_classify_channel(tmp_path):
    """error events route by PRODUCING STAGE: a classify-stage error passes a
    channels=("classify",) filter and is excluded by a quality-only filter."""
    payload = {"stage": "classify", "kind": "classification_invalid",
               "message": "m", "retryable": False}
    log, path = open_log(tmp_path, channels=("classify",))
    log.emit(ev(EV_ERROR, stage="classify", record_ids=("r1",), payload=payload))
    log.emit(ev(EV_ERROR, stage="quality",
                payload={"stage": "quality", "kind": "judgment_invalid",
                         "message": "m", "retryable": False}))    # filtered
    log.close()
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["stage"] == "classify"
    assert lines[0]["payload"]["kind"] == "classification_invalid"

    path2 = tmp_path / "quality_only.trace.jsonl"
    log2 = EventLog(TraceConfig(enabled=True, path=str(path2),
                                channels=("quality",)), "f3a9c04b7d21")
    log2.emit(ev(EV_ERROR, stage="classify", record_ids=("r1",), payload=payload))
    log2.close()
    assert not path2.exists()                   # filtered by the quality-only set


def test_classify_decision_is_trace_only_no_stderr_mirror(tmp_path, capsys):
    """R29: classify.decision has no stderr level (like quality.judgment) —
    the sink writes it to the trace but never mirrors it to the run log."""
    cfg = make_cfg(tmp_path, tool=ToolConfig(log_level="debug"),
                   trace=TraceConfig(enabled=True,
                                     path=str(tmp_path / "t.trace.jsonl"),
                                     channels=("classify",)))
    setup_logging(cfg)
    log = EventLog(cfg.trace, "abc")
    sink = MetricsSink(cfg, "abc", log)
    sink.event(EV_CLASSIFY_DECISION, stage="classify", batch_no=1,
               record_ids=("r1",), payload={"label": "faq", "source": "llm"})
    sink.flush()
    log.close()
    assert capsys.readouterr().err == ""        # no mirror line at any level
    lines = (tmp_path / "t.trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(l)["ev"] for l in lines] == ["classify.decision"]


# ── write-failure policy ───────────────────────────────────────────────────


def test_unwritable_path_warns_once_disables_and_never_raises(tmp_path, caplog):
    bad = tmp_path / "no_such_dir" / "t.trace.jsonl"
    cfg = TraceConfig(enabled=True, path=str(bad), channels=("quality",))
    with caplog.at_level(logging.WARNING, logger="labelkit"):
        log = EventLog(cfg, "abc")                      # open fails, no raise
        log.emit(ev("quality.gate", record_ids=("a",)))
        log.emit(ev("quality.gate", record_ids=("b",)))
        log.emit(ev("dedup.duplicate", stage="dedup"))  # filtered, not dropped
        log.flush()
        log.close()
    assert log.events_written == 0
    assert log.dropped_events == 2
    warns = [r for r in caplog.records if "trace channel disabled" in r.message]
    assert len(warns) == 1


class _BrokenFile:
    def write(self, s):
        raise OSError("disk full")

    def flush(self):
        raise OSError("disk full")

    def close(self):
        pass


def test_midrun_write_failure_warns_once_and_counts_drops(tmp_path, caplog):
    log, _ = open_log(tmp_path, channels=("quality",))
    log.emit(ev("quality.gate", record_ids=("ok",)))
    log._fh.close()
    log._fh = _BrokenFile()                             # simulate I/O failure
    with caplog.at_level(logging.WARNING, logger="labelkit"):
        log.emit(ev("quality.gate", record_ids=("a",)))
        log.emit(ev("quality.gate", record_ids=("b",)))
        log.flush()                                     # no raise after close
        log.close()
    assert log.events_written == 1
    assert log.dropped_events == 2
    warns = [r for r in caplog.records if "trace channel disabled" in r.message]
    assert len(warns) == 1


# ── MetricsSink ────────────────────────────────────────────────────────────


def test_metrics_sink_builds_event_with_iso_ms_ts(tmp_path):
    cfg = make_cfg(tmp_path, trace=TraceConfig(
        enabled=True, path=str(tmp_path / "t.trace.jsonl"), channels=("annotate",)))
    log = EventLog(cfg.trace, "0123456789ab")
    sink = MetricsSink(cfg, "0123456789ab", log)
    sink.event("annotate.done", stage="annotate", batch_no=2,
               record_ids=("r1",), payload={"attempts": 1})
    sink.flush()
    log.close()
    obj = json.loads((tmp_path / "t.trace.jsonl").read_text().splitlines()[0])
    assert obj["run_id"] == "0123456789ab"
    assert obj["batch_no"] == 2
    assert obj["record_ids"] == ["r1"]
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}", obj["ts"])


def test_metrics_sink_counters_and_stage_times(tmp_path):
    cfg = make_cfg(tmp_path)
    sink = MetricsSink(cfg, "abc", EventLog(cfg.trace, "abc"))
    sink.count("dropped_dup")
    sink.count("dropped_dup", 2)
    sink.add_stage_time("dedup", 0.5)
    sink.add_stage_time("dedup", 0.25)
    assert sink.counters == {"dropped_dup": 3}
    assert sink.stage_times == {"dedup": 0.75}


def test_circuit_breaker_streak_and_reset(tmp_path):
    cfg = make_cfg(tmp_path)     # fatal_error_threshold = 3
    sink = MetricsSink(cfg, "abc", EventLog(cfg.trace, "abc"))
    sink.record_provider_result(fatal=True)
    sink.record_provider_result(fatal=True)
    sink.record_provider_result(fatal=False)     # streak resets
    sink.record_provider_result(fatal=True)
    sink.record_provider_result(fatal=True)
    assert not sink.circuit_broken
    sink.record_provider_result(fatal=True)
    assert sink.circuit_broken
    sink.record_provider_result(fatal=False)     # broken stays broken
    assert sink.circuit_broken


# ── stderr run-log formats (§8.4) ──────────────────────────────────────────


def _emit_stderr_line(tmp_path, capsys, fmt: str) -> str:
    cfg = make_cfg(tmp_path, tool=ToolConfig(log_level="debug", log_format=fmt))
    setup_logging(cfg)
    logging.getLogger("labelkit.quality").info(
        "pairwise 完成 items=128 comparisons=256 judgment_failures=1",
        extra={"stage": "quality", "batch": 3})
    return capsys.readouterr().err.strip().splitlines()[-1]


def test_stderr_text_format(tmp_path, capsys):
    line = _emit_stderr_line(tmp_path, capsys, "text")
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2} INFO  quality "
        r"batch=3 pairwise 完成 items=128 comparisons=256 judgment_failures=1",
        line)


def test_stderr_text_format_dash_for_missing_extras(tmp_path, capsys):
    cfg = make_cfg(tmp_path, tool=ToolConfig(log_format="text"))
    setup_logging(cfg)
    logging.getLogger("labelkit.cli").warning("something happened")
    line = capsys.readouterr().err.strip()
    assert " WARN  " in line
    assert " batch=- " in line
    assert re.search(r" -\s+batch=-", line)


def test_stderr_jsonl_lines_parse_as_json(tmp_path, capsys):
    line = _emit_stderr_line(tmp_path, capsys, "jsonl")
    obj = json.loads(line)
    assert list(obj) == ["ts", "level", "stage", "batch", "msg"]
    assert obj["level"] == "info"
    assert obj["stage"] == "quality"
    assert obj["batch"] == 3
    assert obj["msg"].startswith("pairwise 完成")


def test_setup_logging_respects_level_and_is_idempotent(tmp_path, capsys):
    cfg = make_cfg(tmp_path, tool=ToolConfig(log_level="warn", log_format="jsonl"))
    setup_logging(cfg)
    setup_logging(cfg)     # second call must not duplicate handlers
    lg = logging.getLogger("labelkit.dedup")
    lg.info("hidden", extra={"stage": "dedup", "batch": 1})
    lg.warning("shown", extra={"stage": "dedup", "batch": 1})
    lines = [l for l in capsys.readouterr().err.splitlines() if l.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["level"] == "warn"


def test_mirror_never_leaks_free_text_or_nested_payload(tmp_path, capsys):
    cfg = make_cfg(tmp_path, tool=ToolConfig(log_level="debug"))
    setup_logging(cfg)
    sink = MetricsSink(cfg, "abc", EventLog(cfg.trace, "abc"))
    # quality.judgment has no stderr level → no mirror line at all
    sink.event("quality.judgment", stage="quality", batch_no=1,
               record_ids=("a", "b"), payload=JUDGMENT_PAYLOAD)
    assert capsys.readouterr().err == ""
    # llm.call mirrors at debug with scalar fields only
    sink.event("llm.call", stage="llm", batch_no=1, payload=LLM_PAYLOAD)
    err = capsys.readouterr().err
    assert "profile=default" in err
    assert "status=ok" in err
    assert LONG_TEXT[:20] not in err


def test_mirror_bad_line_carries_structural_skip_reason(tmp_path, capsys):
    """spec 7.3 normative stderr example: the ingest.bad_line WARN line carries
    reason=missing_text_field — a structural enum, not LLM text. With default
    config (trace off, ingest not in trace.channels) this line is the ONLY
    place the skip reason surfaces."""
    cfg = make_cfg(tmp_path, tool=ToolConfig(log_level="debug"))
    setup_logging(cfg)
    sink = MetricsSink(cfg, "abc", EventLog(cfg.trace, "abc"))
    sink.event("ingest.bad_line", stage="ingest", batch_no=4,
               payload={"file": "ime-2026-06-30.jsonl", "line_no": 217,
                        "reason": "missing_text_field"})
    err = capsys.readouterr().err
    assert " WARN  " in err
    assert "ingest.bad_line" in err
    assert "file=ime-2026-06-30.jsonl" in err
    assert "line_no=217" in err
    assert "reason=missing_text_field" in err


def test_mirror_index_conflict_level_follows_policy(tmp_path, capsys):
    """spec 7.2 / CONTRACTS §8.1: ingest.index_conflict mirrors at warn, but at
    error when input.on_index_conflict="fail"."""
    payload = {"index": "00042"}
    cfg_fail = make_cfg(tmp_path, tool=ToolConfig(log_format="jsonl"),
                        input_cfg=InputConfig(on_index_conflict="fail"))
    setup_logging(cfg_fail)
    sink = MetricsSink(cfg_fail, "abc", EventLog(cfg_fail.trace, "abc"))
    sink.event("ingest.index_conflict", stage="ingest", batch_no=1, payload=payload)
    line = json.loads(capsys.readouterr().err.strip())
    assert line["level"] == "error"

    cfg_skip = make_cfg(tmp_path, tool=ToolConfig(log_format="jsonl"),
                        input_cfg=InputConfig(on_index_conflict="skip"))
    setup_logging(cfg_skip)
    sink = MetricsSink(cfg_skip, "abc", EventLog(cfg_skip.trace, "abc"))
    sink.event("ingest.index_conflict", stage="ingest", batch_no=1, payload=payload)
    line = json.loads(capsys.readouterr().err.strip())
    assert line["level"] == "warn"


def test_error_event_mirrors_warn_or_error_level(tmp_path, capsys):
    cfg = make_cfg(tmp_path, tool=ToolConfig(log_level="debug", log_format="jsonl"))
    setup_logging(cfg)
    sink = MetricsSink(cfg, "abc", EventLog(cfg.trace, "abc"))
    sink.event("error", stage="annotate", batch_no=1, record_ids=("r",),
               payload={"stage": "annotate", "kind": "schema_violation",
                        "message": "L3 exhausted", "retryable": False})
    sink.event("error", stage="llm", batch_no=0,
               payload={"stage": "llm", "kind": "provider_fatal",
                        "message": "401", "retryable": False})
    lines = [json.loads(l) for l in capsys.readouterr().err.splitlines()]
    assert [l["level"] for l in lines] == ["warn", "error"]


# ── E2E-finding fixes: lazy trace open (P2-4) & breaker hard trip (P2-3) ─────

def test_trace_file_lazy_open_untouched_until_first_emit(tmp_path):
    path = tmp_path / "run.trace.jsonl"
    path.write_text("precious previous run\n", encoding="utf-8")
    log, _ = open_log(tmp_path)
    # Construction must NOT touch the file (a run dying in config/input
    # validation before run.start leaves the previous trace intact).
    assert path.read_text(encoding="utf-8") == "precious previous run\n"
    log.emit(ev("quality.gate", record_ids=("a",), payload={"decision": "keep"}))
    log.close()
    text = path.read_text(encoding="utf-8")
    assert "precious previous run" not in text        # truncated at first emit
    assert '"quality.gate"' in text


def test_circuit_breaker_hard_trip_is_immediate(tmp_path):
    cfg = make_cfg(tmp_path)     # fatal_error_threshold = 3
    sink = MetricsSink(cfg, "abc", EventLog(cfg.trace, "abc"))
    # One auth-class fatal (401/403) opens the breaker at once — no streak.
    sink.record_provider_result(fatal=True, hard=True)
    assert sink.circuit_broken

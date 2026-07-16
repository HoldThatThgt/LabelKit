"""M12 — observability (spec ch.7, 3.12).

Two independent channels:

1. stderr run log — stdlib ``logging`` on logger ``labelkit``; text | jsonl line
   formats per CONTRACTS.md §8.4. NEVER contains data content, prompts, or API keys.
2. trace event log — opt-in JSONL file (``trace.path``), one :class:`TraceEvent`
   per line, line-buffered, flushed per batch in sync with M11. Payloads are
   redacted per the four ``trace.content`` tiers (§8.3).

Write failures never interrupt the run: the first OSError warns once on stderr,
closes the channel, and every subsequent event counts into
``report.trace.dropped_events``.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import IO, Mapping

from labelkit.config.model import ResolvedConfig, TraceConfig

# ── Event-name constants (§7.11, exact strings) ────────────────────────────

EV_RUN_START = "run.start"
EV_RUN_END = "run.end"
EV_BATCH_START = "batch.start"
EV_BATCH_END = "batch.end"
EV_INGEST_BAD_LINE = "ingest.bad_line"
EV_INGEST_MISSING_PAIR = "ingest.missing_pair"
EV_INGEST_INDEX_CONFLICT = "ingest.index_conflict"
EV_INGEST_DISORDER = "ingest.disorder"       # v1.8 M2 stream monotonicity (spec 7.2)
EV_SEGMENT_SESSION = "segment.session"       # v1.8 M2 session close (spec 7.2); trace-only
EV_SEGMENT_BOUNDARY = "segment.boundary"     # v1.8 M14 window verdict (spec 7.2); trace-only
EV_DEDUP_DUPLICATE = "dedup.duplicate"
EV_CLASSIFY_DECISION = "classify.decision"   # v1.7 M13 (spec 7.2); trace-only, R29
EV_EXTRACT_STEP = "extract.step"             # v1.8 M15 (spec 7.2); trace-only, S27
EV_QUALITY_JUDGMENT = "quality.judgment"
EV_QUALITY_POINTWISE = "quality.pointwise"
EV_QUALITY_BT_FIT = "quality.bt_fit"
EV_QUALITY_GATE = "quality.gate"
EV_ANNOTATE_DONE = "annotate.done"
EV_VERIFY_VERDICT = "verify.verdict"
EV_SCHEMA_REPAIR = "schema.repair"
EV_LLM_CALL = "llm.call"
EV_LLM_KEY_COOLDOWN = "llm.key_cooldown"     # v1.6 key pool (spec 7.2)
EV_LLM_KEY_DISABLED = "llm.key_disabled"     # v1.6
EV_LLM_POOL_PARKED = "llm.pool_parked"       # v1.6
EV_ERROR = "error"

TRACE_SCHEMA_VERSION = 1

_logger = logging.getLogger("labelkit.obslog")


# ── TraceEvent ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TraceEvent:
    ts: str                        # ISO8601 milliseconds with timezone offset
    run_id: str                    # secrets.token_hex(6) — 12 hex chars per run
    batch_no: int                  # 0 for run-level events
    stage: str                     # emitting stage name; run.*/batch.* use "run"
    ev: str                        # event name (§8.1)
    record_ids: tuple[str, ...]    # 0/1/2 record ids
    payload: Mapping               # per-event fields (§8.1), redacted per trace.content (§8.3)


# ── Redaction (§8.3) ───────────────────────────────────────────────────────

# LLM-produced free text, dropped at tier "none". v1.8 (S27, §8.3):
# + "description" (extract.step LLM text, same tier as reason/critiques) and
# + "defects" (the verify.verdict stream defect table carries LLM free text in
#   `detail` — dropped whole-key at "none", critiques level).
_FREE_TEXT_KEYS = frozenset({"reason", "critiques", "violations",
                             "description", "defects"})
# v1.8 (S27, §8.3): INPUT-DATA-DERIVED payload fields (extract.step's widget
# text reference / typed-in text) — stripped at BOTH "none" and "refs" (the
# refs tier's "no input data content" red line), carried from "excerpt".
_DATA_KEYS = frozenset({"target", "value"})
# Full prompt/response messages, present only at tier "full".
_MESSAGE_KEYS = frozenset({"gen_ai.input.messages", "gen_ai.output.messages"})
_EXCERPT_MAX_CHARS = 200


def _strip(value, drop: frozenset[str] | set[str]):
    """Recursively remove ``drop`` keys from nested mappings/sequences."""
    if isinstance(value, Mapping):
        return {k: _strip(v, drop) for k, v in value.items() if k not in drop}
    if isinstance(value, (list, tuple)):
        return [_strip(v, drop) for v in value]
    return value


def redact_payload(payload: Mapping, content: str) -> Mapping:
    """Apply the trace.content tier (§8.3) to an event payload.

    - "none":    ids/enums/numbers only — reason/critiques/violations/
                 description/defects dropped, no excerpt, no target/value,
                 no gen_ai messages.
    - "refs":    + LLM-produced text (reason/critiques/violations/description/
                 defects); still no input data content (no excerpt, no
                 target/value — _DATA_KEYS, v1.8 S27 —, no gen_ai messages).
    - "excerpt": + ``excerpt`` field, each value truncated to its first
                 200 characters; + the _DATA_KEYS fields (target/value).
    - "full":    + gen_ai.input.messages / gen_ai.output.messages verbatim.
                 Tiers are cumulative — "full" keeps the (truncated) excerpt too.
    """
    drop: set[str] = set()
    if content != "full":
        drop |= _MESSAGE_KEYS
    if content in ("none", "refs"):
        drop.add("excerpt")
        # v1.8 (S27, §8.3): input-data-derived fields never leak below the
        # excerpt tier — the refs tier carries LLM text but NO input content.
        drop |= _DATA_KEYS
    if content == "none":
        drop |= _FREE_TEXT_KEYS
    out = _strip(payload, drop)
    # Tiers are cumulative (spec 7.4 "逐档递增"): "full" contains everything
    # "excerpt" does, so the 200-char truncation applies at both tiers.
    if content in ("excerpt", "full") and isinstance(out.get("excerpt"), Mapping):
        out["excerpt"] = {
            rid: (text[:_EXCERPT_MAX_CHARS] if isinstance(text, str) else text)
            for rid, text in out["excerpt"].items()
        }
    return out


# ── EventLog (trace channel) ───────────────────────────────────────────────

class EventLog:
    """JSONL trace writer. Never raises to callers; write failure warns once,
    closes the channel, and counts subsequent events as dropped."""

    def __init__(self, cfg: TraceConfig, run_id: str):
        self.cfg = cfg
        self.run_id = run_id
        self.dropped_events: int = 0
        self.events_written: int = 0
        self._fh: IO[str] | None = None
        self._closed = False           # closed due to write failure
        self._opened = False           # lazy open: file is touched on FIRST emit
        self._channels = frozenset(cfg.channels)
        # The trace file is deliberately NOT opened here (E2E finding P2-4):
        # opening (and truncating a previous run's file) waits until the first
        # emitted event, so a run that dies in config/input validation before
        # run.start never destroys the previous run's trace.

    def _open(self) -> None:
        self._opened = True
        try:
            if self.cfg.path and os.path.exists(self.cfg.path):
                _logger.warning(
                    "trace file %s already exists — truncating (rename it or set "
                    "trace.path to keep history)", self.cfg.path,
                    extra={"stage": "run", "batch": 0},
                )
            # buffering=1 → line-buffered text stream
            self._fh = open(self.cfg.path, "w", encoding="utf-8", buffering=1)
        except OSError as exc:
            self._fail(exc)

    # internal ---------------------------------------------------------------

    def _fail(self, exc: OSError) -> None:
        """First OSError: warn once on stderr, close the channel."""
        if not self._closed:
            self._closed = True
            _logger.warning(
                "trace channel disabled after write failure: %s — subsequent "
                "events are dropped and counted", exc,
                extra={"stage": "run", "batch": 0},
            )
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def _channel(self, ev: TraceEvent) -> str:
        # Channel = event-name prefix before the first '.', EXCEPT ev == "error",
        # whose channel is the producing stage (spec 7.2).
        if ev.ev == EV_ERROR:
            return ev.stage
        return ev.ev.split(".", 1)[0]

    def _passes_filter(self, ev: TraceEvent) -> bool:
        channel = self._channel(ev)
        if channel in ("run", "batch"):    # lifecycle events bypass the filter
            return True
        return channel in self._channels

    # public -----------------------------------------------------------------

    @property
    def closed(self) -> bool:
        """True once a write failure shut the channel (3.12.4). The
        orchestrator reads this when assembling ``report.trace`` to account
        for the terminal ``run.end`` event, which is emitted after the report
        is built (§9.3)."""
        return self._closed

    def emit(self, ev: TraceEvent) -> None:
        """Line-buffered JSONL write. No-op when the channel is disabled,
        filtered out, or closed after a write failure (callers never check)."""
        if not self.cfg.enabled:
            return
        if not self._passes_filter(ev):
            return
        if not self._opened and not self._closed:
            self._open()
        if self._closed or self._fh is None:
            self.dropped_events += 1
            return
        payload = redact_payload(ev.payload, self.cfg.content)
        if ev.ev == EV_RUN_START and "trace_schema_version" not in payload:
            payload = {**payload, "trace_schema_version": TRACE_SCHEMA_VERSION}
        line = json.dumps(
            {
                "ts": ev.ts,
                "run_id": ev.run_id,
                "batch_no": ev.batch_no,
                "stage": ev.stage,
                "ev": ev.ev,
                "record_ids": list(ev.record_ids),
                "payload": payload,
            },
            ensure_ascii=False,
        )
        try:
            self._fh.write(line + "\n")
        except OSError as exc:
            self._fail(exc)
            self.dropped_events += 1
            return
        self.events_written += 1

    def flush(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.flush()
        except OSError as exc:
            self._fail(exc)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


# ── MetricsSink ────────────────────────────────────────────────────────────

# stderr mirror levels per §8.1 (events with "—" are trace-only, never mirrored).
_STDERR_LEVELS: dict[str, int] = {
    EV_RUN_START: logging.INFO,
    EV_RUN_END: logging.INFO,
    EV_BATCH_START: logging.DEBUG,
    EV_BATCH_END: logging.INFO,
    EV_INGEST_BAD_LINE: logging.WARNING,
    EV_INGEST_MISSING_PAIR: logging.WARNING,
    # EV_INGEST_INDEX_CONFLICT: warn, but error when input.on_index_conflict="fail"
    # (spec 7.2 / CONTRACTS §8.1) — resolved dynamically in _mirror().
    # v1.8 ingest.disorder is trace-only here (D1): its reason embeds
    # timestamp/cursor values and fires once PER RECORD — mirroring would both
    # break the "one stderr WARN per run" contract and flood stderr with
    # input-derived values under a systematically bad timestamp field. M2
    # itself logs the single data-free WARN per run (spec 7.2); the fail
    # policy surfaces through InputError (exit 3). The three segment/extract
    # events (segment.session / segment.boundary / extract.step) are likewise
    # trace-only ("—", §8.1) and stay out of this table.
    EV_LLM_CALL: logging.DEBUG,
    # v1.6 key-pool events (spec 7.2): key_cooldown is trace-only ("—"),
    # key_disabled / pool_parked mirror at warn.
    EV_LLM_KEY_DISABLED: logging.WARNING,
    EV_LLM_POOL_PARKED: logging.WARNING,
    # EV_ERROR: warn (record-level) / error (run-level) — resolved in event()
}


class MetricsSink:
    """Holds the EventLog + run counters. All stages emit through RunContext.metrics."""

    def __init__(self, cfg: ResolvedConfig, run_id: str, event_log: EventLog):
        self.cfg = cfg
        self.run_id = run_id
        self.event_log = event_log
        self.counters: dict[str, int] = {}
        self.stage_times: dict[str, float] = {}
        self._fatal_streak = 0
        self._circuit_broken = False

    def event(self, ev: str, *, stage: str, batch_no: int,
              record_ids: tuple[str, ...] = (), payload: Mapping | None = None) -> None:
        """Builds the TraceEvent (ts=now local ISO8601 ms, run_id) and forwards to
        EventLog; also mirrors to the stderr logger at the §8.1 level when one is
        defined."""
        payload = payload or {}
        trace_ev = TraceEvent(
            ts=datetime.now().astimezone().isoformat(timespec="milliseconds"),
            run_id=self.run_id,
            batch_no=batch_no,
            stage=stage,
            ev=ev,
            record_ids=tuple(record_ids),
            payload=payload,
        )
        self.event_log.emit(trace_ev)
        self._mirror(ev, stage, batch_no, payload)

    def _mirror(self, ev: str, stage: str, batch_no: int, payload: Mapping) -> None:
        if ev == EV_ERROR:
            # run-level provider_fatal → error; record-level → warn (§8.1)
            level = logging.ERROR if payload.get("kind") == "provider_fatal" else logging.WARNING
        elif ev == EV_INGEST_INDEX_CONFLICT:
            # spec 7.2: warn, but error under the fail policy (§8.1)
            level = (logging.ERROR if self.cfg.input.on_index_conflict == "fail"
                     else logging.WARNING)
        else:
            level = _STDERR_LEVELS.get(ev)
            if level is None:
                return
        # Operational summary only: scalar payload fields; never nested content
        # (counts objects, judgments, messages ...). No data content, no prompts.
        # Scalars in mirrored events are structural (e.g. ingest.bad_line's
        # skip-reason enum, spec 7.3 normative example) — LLM free text only ever
        # appears in non-mirrored events or as nested lists, which the isinstance
        # filter already excludes.
        parts = [
            f"{k}={v}" for k, v in payload.items()
            if isinstance(v, (str, int, float, bool))
        ]
        msg = ev if not parts else ev + " " + " ".join(parts)
        logging.getLogger("labelkit." + (stage or "run")).log(
            level, msg, extra={"stage": stage, "batch": batch_no},
        )

    def count(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def add_stage_time(self, stage: str, seconds: float) -> None:
        self.stage_times[stage] = self.stage_times.get(stage, 0.0) + seconds

    def record_provider_result(self, fatal: bool, *, hard: bool = False) -> None:
        """Feed the circuit breaker. ``hard=True`` (auth-class 401/403 fatals)
        opens the breaker immediately — credential/permission failures never
        self-heal, so counting a streak would only burn money (spec 3.9.3)."""
        if fatal:
            self._fatal_streak += 1
            if hard or self._fatal_streak >= self.cfg.run.fatal_error_threshold:
                self._circuit_broken = True
        else:
            self._fatal_streak = 0

    @property
    def circuit_broken(self) -> bool:
        return self._circuit_broken

    def flush(self) -> None:
        self.event_log.flush()


# ── stderr run log (§8.4) ──────────────────────────────────────────────────

_TEXT_LEVEL_NAMES = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARN",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "ERROR",
}
_JSONL_LEVEL_NAMES = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}
_LOG_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO,
               "warn": logging.WARNING, "error": logging.ERROR}


def _record_ts(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds")


class _TextFormatter(logging.Formatter):
    """'{ts} {LEVEL:<5} {stage:<7} batch={n|-} {msg}' — stage/batch from record
    extras, '-' when absent."""

    def format(self, record: logging.LogRecord) -> str:
        level = _TEXT_LEVEL_NAMES.get(record.levelno, record.levelname[:5])
        stage = getattr(record, "stage", None) or "-"
        batch = getattr(record, "batch", None)
        batch_s = "-" if batch is None else str(batch)
        return f"{_record_ts(record)} {level:<5} {stage:<7} batch={batch_s} {record.getMessage()}"


class _JsonlFormatter(logging.Formatter):
    """One JSON object per line: {"ts","level","stage","batch","msg"}."""

    def format(self, record: logging.LogRecord) -> str:
        batch = getattr(record, "batch", None)
        return json.dumps(
            {
                "ts": _record_ts(record),
                "level": _JSONL_LEVEL_NAMES.get(record.levelno, record.levelname.lower()),
                "stage": getattr(record, "stage", None) or "-",
                "batch": batch,
                "msg": record.getMessage(),
            },
            ensure_ascii=False,
        )


def setup_logging(cfg: ResolvedConfig) -> None:
    """Installs the stderr handler on logger 'labelkit' per tool.log_format /
    tool.log_level. Modules log via logging.getLogger('labelkit.<module>') with
    extra={'stage': ..., 'batch': ...}."""
    logger = logging.getLogger("labelkit")
    logger.setLevel(_LOG_LEVELS.get(cfg.tool.log_level, logging.INFO))
    logger.propagate = False
    for handler in list(logger.handlers):    # idempotent re-setup
        if getattr(handler, "_labelkit_handler", False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler._labelkit_handler = True         # type: ignore[attr-defined]
    handler.setFormatter(
        _JsonlFormatter() if cfg.tool.log_format == "jsonl" else _TextFormatter()
    )
    logger.addHandler(handler)

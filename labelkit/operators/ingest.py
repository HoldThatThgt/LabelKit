"""M2 — data ingest (spec 3.2, 6.1/6.2; CONTRACTS.md §7.1).

Materializes ``run.input`` into a lazy ``Record`` iterator:

- text modality: line-by-line JSONL parsing, ``input.text_field`` dotted-path
  extraction, deterministic id = sha256(canonical_json(raw))[:16];
- UI modality: recursive scan, ``uitree_<index>.jsonl`` / ``image_<index>.*``
  pairing across subdirectories (one shared index namespace), UI-tree node
  parsing per the §6.2 field mapping, lazy ``ImageRef`` (magic number + size
  check only — no pixel decode), id = sha256(tree_bytes + image_bytes)[:16].

Bad data follows input.on_bad_line / on_missing_pair / on_index_conflict
("skip" → count + trace event; "fail" → InputError, CLI exit 3).

v1.8 (stream mode, spec 3.2.8): ``sessions()`` exposes the session-stream
view consumed by M10 — input-side ordering per ``[stream]`` (S20 timestamp
parsing), the per-partition-key monotonicity check with ``stream.on_disorder``
(S19), and the rule-layer session assembler (key change / gap_s / gap_steps /
session_max_len / session_max_span_s). ``scan()`` fuses the text line count
with a session dry-run in a single pass (S23, ``IngestPlan.session_lens``).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Literal, Mapping

from labelkit.common.config.model import ResolvedConfig, StreamConfig
from labelkit.common.errors import InputError
from labelkit.common.contracts.types import ImageRef, Record, RecordRef, UINode, UITree

__all__ = ["IngestPlan", "IngestReport", "Ingestor", "Session"]


# ── filename patterns (spec 3.2.4; extension match case-insensitive) ───────
_TREE_RE = re.compile(r"^uitree_(\d+)\.(?i:jsonl)$")
_IMAGE_RE = re.compile(r"^image_(\d+)\.(?i:png|jpg|jpeg)$")

# image magic numbers (spec 3.2.4: 仅校验魔数与尺寸，不解码全图)
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"

# ── §6.2 field mapping: accepted source names, in precedence order ─────────
_NODE_ID_KEYS = ("id", "node_id")
_PARENT_KEYS = ("parent", "parent_id")
_ROLE_KEYS = ("class", "className", "type", "role")
_TEXT_KEYS = ("text", "label")
_DESC_KEYS = ("content_desc", "contentDescription", "desc")
_BOUNDS_KEYS = ("bounds",)
_VISIBLE_KEYS = ("visible", "visible_to_user")

_BOUNDS_STR_RE = re.compile(
    r"^\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\s*$"
)


def _canonical_json(obj: Any) -> str:
    """Canonical JSON per spec 3.2.5 (sort_keys, ensure_ascii=False, compact)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _text_record_id(raw: Mapping) -> str:
    return hashlib.sha256(_canonical_json(raw).encode("utf-8")).hexdigest()[:16]


def _extract_text_field(obj: Mapping, dotted_path: str) -> str | None:
    """Dotted-path extraction (spec 3.2.5). Returns None on a miss.

    String hit → used as-is; array/object (or any other non-null JSON value)
    hit → canonical JSON serialization; missing key / null / non-mapping
    intermediate → miss.
    """
    cur: Any = obj
    for part in dotted_path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    if cur is None:
        return None
    if isinstance(cur, str):
        return cur
    return _canonical_json(cur)


# ── v1.8 stream mode: timestamp parsing (S20, spec 6.1) ─────────────────────

_MISS = object()  # raw-lookup miss marker (a literal None value is also a parse failure)


def _lookup_raw(obj: Mapping | None, dotted_path: str) -> Any:
    """Raw dotted-path lookup on Record.raw (same path semantics as
    input.text_field, spec 3.2.8): returns the raw value, or _MISS."""
    cur: Any = obj
    for part in dotted_path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return _MISS
        cur = cur[part]
    return cur


def _numeric_order_key(v: float) -> float | None:
    """S20 numeric rules: v<0 ∨ v≥1e14 → failure; v<1e11 → epoch seconds;
    1e11≤v<1e14 → epoch milliseconds (÷1000). NaN fits no bucket → failure."""
    if math.isnan(v) or v < 0 or v >= 1e14:
        return None
    if v < 1e11:
        return float(v)
    return v / 1000.0


def _parse_order_key(value: Any) -> float | None:
    """S20: parse a stream.order_by="meta:<field>" value into the internal
    order key (float epoch seconds). Returns None on parse failure.

    Numbers (bool excluded — JSON true/false is not a timestamp) follow the
    numeric rules; strings first try float() under the numeric rules, then
    datetime.fromisoformat (Python 3.11+ accepts the Z suffix): aware →
    .timestamp(), naive → interpreted as UTC."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _numeric_order_key(float(value))
    if isinstance(value, str):
        try:
            return _numeric_order_key(float(value))
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def _clip(value: Any, limit: int = 120) -> str:
    """Bounded string form of a timestamp source value for reason texts
    (the timestamp's own value is sanctioned in reasons — S20/spec 7.2)."""
    s = str(value)
    return s if len(s) <= limit else s[:limit] + "…"


@dataclass(frozen=True)
class IngestPlan:
    files: tuple[str, ...]                     # text: .jsonl files (lexicographic by name);
                                               # UI: all matched files, tree then image per
                                               # pair, pairs ascending. Paths relative to
                                               # run.input (as RecordRef.source_file)
    pairs: tuple[tuple[int, str, str], ...]    # UI pairing table (spec 3.2.3 配对表):
                                               # (index, tree_path, image_path), ascending
                                               # by index; text modality: ()
    estimated_records: int                     # text: total lines (cheap count); UI: len(pairs)
    session_lens: tuple[int, ...] = ()         # v1.8 (S23): session dry-run lengths for the
                                               # dry-run next-fit packing; () when
                                               # estimate=False or segment.enabled=False


@dataclass
class IngestReport:
    scanned: int = 0                           # lines seen / pair indexes seen
    ingested: int = 0
    bad_input: int = 0                         # bad lines + skipped conflicts + missing pairs
                                               # (v1.8: + skipped disorder records)
    missing_pair: int = 0                      # UI only
    index_conflict: int = 0                    # UI only
    sessions: int = 0                          # v1.8: candidate sessions closed by the
                                               # assembler (stream mode only)
    disorder: int = 0                          # v1.8: records skipped by the monotonicity
                                               # check (out-of-order or timestamp parse
                                               # failure; a SUBSET of bad_input, S20)
    bad_locations: list[dict] = field(default_factory=list)
                                               # {"file": str, "line_no": int|None,
                                               #  "index": int|None, "reason": str}


@dataclass(frozen=True)
class Session:                                 # v1.8 (CONTRACTS §7.1) [FROZEN THERE]
    session_id: str                            # sha256("\n".join(record ids))[:16] over the
                                               # session's records in session order
    records: tuple[Record, ...]                # session members in session (order-key) order
    cause: Literal["gap", "key", "max_len", "max_span", "eof", "limit"]
                                               # what closed the session (spec 3.2.8/S17
                                               # vocabulary; = segment.session payload cause)


@dataclass(frozen=True)
class _UIScan:
    """Full UI scan result (internal): matched pairs + anomalies, all by index."""
    pairs: tuple[tuple[int, str, str], ...]            # ascending by index
    conflicts: tuple[tuple[int, tuple[str, ...]], ...]  # (index, offending files) ascending
    missing: tuple[tuple[int, str, str], ...]           # (index, present "tree"|"image", file)


class _Assembler:
    """Rule-layer session-closing state machine (spec 3.2.8), shared by
    sessions() (the real run) and scan() (the S23 dry-run rehearsal). Tracks
    rule state only — no Records; the caller owns any buffering.

    Per frame the caller invokes pre_close() (a cause closing the OPEN session
    before this frame joins, or None) and then feed() (appends the frame;
    True = session_max_len reached, hard close cause="max_len")."""

    def __init__(self, scfg: StreamConfig, *, text: bool, meta: bool):
        self._scfg = scfg
        self._text = text                     # text modality (gap_steps = same-file line_no)
        self._meta = meta                     # order_by = "meta:<field>" (gap_s / max_span live)
        self.length = 0                       # frames in the open session
        self._boundary: tuple | None = None
        self._first_key: float | None = None
        self._prev_key: float | None = None
        self._prev_step: int | None = None    # line_no (text) / pair_index (UI)
        self._prev_file: str | None = None

    def pre_close(self, boundary: tuple, order_key: float | None,
                  step: int | None, source_file: str) -> str | None:
        if self.length == 0:
            return None
        if boundary != self._boundary:
            return "key"
        s = self._scfg
        if (self._meta and order_key is not None and self._prev_key is not None
                and order_key - self._prev_key > s.gap_s):
            return "gap"
        # gap_steps: UI = pair_index difference; text = line_no difference
        # WITHIN the same file (line numbers reset per file — meta:* keeps
        # file boundaries transparent, so cross-file adjacency skips the check)
        if (s.gap_steps > 0 and step is not None and self._prev_step is not None
                and (not self._text or source_file == self._prev_file)
                and step - self._prev_step > s.gap_steps):
            return "gap"
        if (self._meta and s.session_max_span_s > 0 and order_key is not None
                and self._first_key is not None
                and order_key - self._first_key > s.session_max_span_s):
            return "max_span"
        return None

    def feed(self, boundary: tuple, order_key: float | None,
             step: int | None, source_file: str) -> bool:
        if self.length == 0:
            self._boundary = boundary
            self._first_key = order_key
        self.length += 1
        self._prev_key = order_key
        self._prev_step = step
        self._prev_file = source_file
        return 0 < self._scfg.session_max_len <= self.length

    def reset(self) -> None:
        self.length = 0
        self._boundary = None
        self._first_key = None


class Ingestor:
    """M2 ingest. Not a Stage — has no ctx; the CLI/orchestrator sets
    ``ingestor.metrics`` (public attribute, default None) before calling
    ``records()`` so ingest trace events are emitted with batch_no=0."""

    def __init__(self, cfg: ResolvedConfig):
        self._cfg = cfg
        self._root = Path(cfg.run.input) if cfg.run.input else None
        self._report = IngestReport()
        self.metrics = None  # MetricsSink | None, wired externally (CONTRACTS §7.1)
        self._disorder_warned = False  # ONE stderr WARN per run (spec 7.2, S19)
        self._session_id_seen: dict[str, int] = {}  # D2: per-run collision guard

    @property
    def report(self) -> IngestReport:
        return self._report

    # ── scan ────────────────────────────────────────────────────────────────

    def scan(self, *, estimate: bool = True) -> IngestPlan:
        """Scan only, no parsing: file list, pairing table, estimated record count.
        Used by --dry-run, `validate` and the orchestrator's P2-4 pre-scan.
        Raises InputError if run.input is missing/unreadable or a UI pairing
        problem hits a 'fail' policy. ``estimate=False`` skips the text-modality
        line count (which reads every input byte) — the pre-scan needs only the
        fail-fast checks, not the estimate, and must not double the input I/O.

        v1.8 (S23): with segment.enabled and estimate=True the plan also carries
        ``session_lens`` — a session dry-run over the same single read pass
        (text) or the pairing table (UI, zero extra I/O). Pure counting
        rehearsal: no Records, no events, no report mutation; bad lines and
        disorder records are skipped exactly like the real skip-policy run."""
        root = self._require_root()
        stream_mode = self._cfg.segment.enabled
        if self._cfg.run.modality == "text":
            files = self._text_files(root)
            estimated = 0
            session_lens: tuple[int, ...] = ()
            if estimate and stream_mode:
                estimated, session_lens = self._fused_text_scan(root, files)
            elif estimate:
                for rel in files:
                    path = root / rel if root.is_dir() else root
                    try:
                        with path.open("rb") as fh:
                            estimated += sum(1 for line in fh if line.strip())
                    except OSError as exc:
                        raise InputError(f"无法读取输入文件 {path}: {exc}") from exc
            return IngestPlan(files=tuple(files), pairs=(), estimated_records=estimated,
                              session_lens=session_lens)

        ui = self._scan_ui(root)
        if ui.conflicts and self._cfg.input.on_index_conflict == "fail":
            index, files_ = ui.conflicts[0]
            self._emit("ingest.index_conflict", {"index": index, "files": list(files_)})
            self._stderr_fallback(
                "ingest.index_conflict index=%s files=%s", index, list(files_))
            raise InputError(
                f"UI index 冲突: index={index} 匹配多个文件 {list(files_)}"
                f"（input.on_index_conflict = \"fail\"）"
            )
        if ui.missing and self._cfg.input.on_missing_pair == "fail":
            # Same fail-fast contract as the conflict branch (P2-4 review):
            # a missing-pair 'fail' run must die HERE, before run.start ever
            # opens (and truncates) the previous run's trace file.
            index, present, file_ = ui.missing[0]
            self._emit("ingest.missing_pair",
                       {"index": index, "present": present, "file": file_})
            self._stderr_fallback(
                "ingest.missing_pair index=%s present=%s file=%s", index, present, file_)
            raise InputError(
                f"UI 文件缺对: index={index} 仅有 {present} 侧（{file_}）"
                f"（input.on_missing_pair = \"fail\"）"
            )
        plan_files: list[str] = []
        for _, tree, image in ui.pairs:
            plan_files.append(tree)
            plan_files.append(image)
        session_lens = (self._rehearse_ui_sessions(ui.pairs)
                        if estimate and stream_mode else ())
        return IngestPlan(files=tuple(plan_files), pairs=ui.pairs,
                          estimated_records=len(ui.pairs),
                          session_lens=session_lens)

    # ── record stream ───────────────────────────────────────────────────────

    def records(self) -> Iterator[Record]:
        """Lazy Record stream. Parse errors follow input.on_bad_line /
        on_missing_pair / on_index_conflict ('skip' → count + trace event;
        'fail' → raise InputError). A stream that exhausts with ZERO valid
        records raises InputError（「无任何合法记录」, spec §2.4 → exit 3）—
        a run that would produce nothing is an input error, not a success."""
        root = self._require_root()
        if self._cfg.run.modality == "text":
            yield from self._text_records(root)
        else:
            yield from self._ui_records(root)
        if self._report.ingested == 0:
            r = self._report
            raise InputError(
                f"无任何合法记录: {root}（scanned={r.scanned} bad_input={r.bad_input}"
                f" missing_pair={r.missing_pair} index_conflict={r.index_conflict}）"
            )

    # ── v1.8 stream mode: session-stream view (spec 3.2.8, CONTRACTS §7.1) ──

    def sessions(self) -> Iterator[Session]:
        """v1.8 (stream mode): the SESSION-STREAM VIEW consumed by M10 instead
        of records(). Pipeline: parse stream (= records() semantics, incl.
        ordering per stream.order_by and the per-partition-key monotonicity
        check with stream.on_disorder, S19/S20) → frame-level --limit islice
        HERE, between the parse stream and the assembler (S17; the limit unit
        stays FRAMES, never sessions) → rule-layer session assembler
        (stream.key change / gap_s / gap_steps / session_max_len /
        session_max_span_s — any trigger closes the session). Emits one
        `segment.session` trace event per closed session (owner M2; the
        segment.* prefix routes it to the segment channel, S1) and counts
        IngestReport.sessions. --limit truncation is treated as EOF: the
        unclosed tail session is flushed with cause="limit" + ONE stderr WARN
        ("尾会话被 --limit 截断", S17)."""
        meta_field = self._meta_field()
        modality = self._cfg.run.modality
        asm = _Assembler(self._cfg.stream, text=modality == "text",
                         meta=meta_field is not None)
        cursors: dict[tuple, float] = {}   # per-partition monotonicity cursors (S19)
        buf: list[tuple[Record, float | None]] = []   # (record, order key)

        stream: Iterator[Record] = self.records()
        limit = self._cfg.limit
        if limit is not None:
            stream = islice(stream, limit)

        consumed = 0
        for rec in stream:
            consumed += 1
            order_key: float | None = None
            if meta_field is not None:
                raw_value = _lookup_raw(rec.raw, meta_field)
                order_key = (None if raw_value is _MISS
                             else _parse_order_key(raw_value))
                if order_key is None:
                    detail = ("字段缺失" if raw_value is _MISS
                              else f"值 {_clip(raw_value)} 无法解析")
                    self._disorder(rec, f"时间戳解析失败：meta:{meta_field} {detail}")
                    continue
            part_key, boundary = self._stream_keys(rec.raw, rec.ref.source_file)
            if meta_field is not None:
                cursor = cursors.get(part_key)
                if cursor is not None and order_key < cursor:
                    self._disorder(
                        rec, f"乱序：时间戳 {order_key} 小于分区游标 {cursor}")
                    continue
                cursors[part_key] = order_key
            step = rec.ref.pair_index if modality == "ui" else rec.ref.line_no
            cause = asm.pre_close(boundary, order_key, step, rec.ref.source_file)
            if cause is not None:
                yield self._close_session(buf, cause)
                buf = []
                asm.reset()
            buf.append((rec, order_key))
            if asm.feed(boundary, order_key, step, rec.ref.source_file):
                yield self._close_session(buf, "max_len")
                buf = []
                asm.reset()

        if buf:
            # cause="limit" states a FACT (the --limit budget was exhausted at
            # this closure point); whether more input existed behind it is
            # unknowable without pulling — and parsing — one extra record,
            # which would perturb the scanned/bad_input ledger (D3). The WARN
            # therefore reports budget exhaustion, not claimed truncation.
            at_budget = limit is not None and consumed == limit
            session = self._close_session(buf, "limit" if at_budget else "eof")
            if at_budget:
                logging.getLogger("labelkit.ingest").warning(
                    "尾会话在 --limit 预算耗尽处闭合（cause=limit；其后是否还有"
                    "输入未知）session_id=%s len=%s",
                    session.session_id, len(session.records),
                    extra={"stage": "ingest", "batch": 0})
            yield session

    def _meta_field(self) -> str | None:
        order_by = self._cfg.stream.order_by
        return order_by[len("meta:"):] if order_by.startswith("meta:") else None

    def _stream_keys(self, raw: Mapping | None, source_file: str) -> tuple[tuple, tuple]:
        """(partition_key, boundary_key) per spec 3.2.8. partition_key =
        stream.key components — "meta:<field>" dotted-path values (text) or
        "source_dir" (the ref.source_file parent directory). boundary_key adds
        the source file under text input_order ordering: a file change always
        closes the session there (no timestamp can bridge the file boundary),
        while meta:* keeps file boundaries transparent (rotated-log case)."""
        parts: list = []
        for key in self._cfg.stream.key:
            if key == "source_dir":
                parts.append(PurePosixPath(source_file).parent.as_posix())
            else:  # "meta:<field>" — shape M1-validated (text modality only)
                parts.append(_extract_text_field(raw or {}, key[len("meta:"):]))
        part_key = tuple(parts)
        if self._cfg.run.modality == "text" and self._meta_field() is None:
            return part_key, part_key + (source_file,)
        return part_key, part_key

    def _close_session(self, buf: list[tuple[Record, float | None]],
                       cause: str) -> Session:
        records = tuple(rec for rec, _ in buf)
        joined = "\n".join(r.id for r in records)
        session_id = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
        # D2 uniqueness guard: record ids are content hashes, so two sessions
        # with byte-identical members (repeated log lines split by max_len,
        # identical idle-screen lone frames ...) would collide and be silently
        # MERGED by M14's session_id regrouping once packed into one batch.
        # Fold a deterministic per-run repeat ordinal into the hash on
        # collision — first occurrence keeps the plain derivation, so ids are
        # stable for normal streams.
        repeat = self._session_id_seen.get(session_id, 0)
        self._session_id_seen[session_id] = repeat + 1
        if repeat:
            session_id = hashlib.sha256(
                f"{joined}\n#repeat:{repeat}".encode("utf-8")).hexdigest()[:16]
        self._report.sessions += 1
        self._emit("segment.session", {
            "session_id": session_id,
            "first": self._order_repr(buf[0]),
            "last": self._order_repr(buf[-1]),
            "len": len(records),
            "cause": cause,
        })
        return Session(session_id=session_id, records=records, cause=cause)

    def _order_repr(self, entry: tuple[Record, float | None]) -> float | int | str:
        """Order-key presentation for segment.session first/last: meta:* →
        epoch float; input_order text → "file:line_no"; UI → pair_index."""
        rec, key = entry
        if key is not None:
            return key
        if self._cfg.run.modality == "ui":
            return rec.ref.pair_index
        return f"{rec.ref.source_file}:{rec.ref.line_no}"

    def _disorder(self, rec: Record, reason: str) -> None:
        """S19/S20: a record rejected by the monotonicity check (out-of-order
        or timestamp parse failure) follows stream.on_disorder. skip: not fed
        to the session flow — bad_input + disorder counts + bad_locations +
        one ingest.disorder trace event per record + ONE data-free stderr WARN
        per run, logged HERE (the event has no obslog mirror row — mirroring
        would fire per record and carry the reason's timestamp values, D1).
        fail: InputError (exit 3)."""
        ref = rec.ref
        text = self._cfg.run.modality == "text"
        line_no = ref.line_no if text else None
        index = None if text else ref.pair_index
        self._report.disorder += 1
        self._bad(file=ref.source_file, line_no=line_no, index=index, reason=reason)
        payload: dict = {"file": ref.source_file}
        if text:
            payload["line_no"] = line_no
        else:
            payload["index"] = index
        payload["reason"] = reason
        self._emit("ingest.disorder", payload)
        if self._cfg.stream.on_disorder == "fail":
            loc = (f"{ref.source_file}:{line_no}" if text
                   else f"{ref.source_file} index={index}")
            raise InputError(f"{loc}: {reason}（stream.on_disorder = \"fail\"）")
        if not self._disorder_warned:
            self._disorder_warned = True
            # Data-free by design (spec §7.1 ①): the per-record reason embeds
            # timestamp/cursor values and stays in the trace channel only.
            logging.getLogger("labelkit.ingest").warning(
                "检测到乱序/时间戳解析失败记录，已按 stream.on_disorder = \"skip\" "
                "跳过（本警告全运行仅一次；逐条明细见 trace ingest.disorder 事件与 "
                "report.counts.bad_input）",
                extra={"stage": "ingest", "batch": 0})

    def _fused_text_scan(self, root: Path,
                         files: list[str]) -> tuple[int, tuple[int, ...]]:
        """S23: ONE read pass producing both the line-count estimate and the
        session dry-run lengths. Pure counting rehearsal of sessions(): no
        Records, no events, no report mutation, never raises for bad/disorder
        lines — those are skipped exactly like the real skip-policy run. The
        frame-level --limit applies between parsing and assembly (S17) while
        the line count still covers every line (estimated_records semantics
        unchanged)."""
        scfg = self._cfg.stream
        meta_field = self._meta_field()
        text_field = self._cfg.input.text_field
        asm = _Assembler(scfg, text=True, meta=meta_field is not None)
        cursors: dict[tuple, float] = {}
        lens: list[int] = []
        estimated = 0
        frames = 0
        limit = self._cfg.limit
        for rel in files:
            path = root / rel if root.is_dir() else root
            try:
                with path.open("rb") as fh:
                    for line_no, line_bytes in enumerate(fh, 1):
                        if not line_bytes.strip():
                            continue
                        estimated += 1
                        if limit is not None and frames >= limit:
                            continue   # assembler saw its last frame; keep counting lines
                        try:
                            raw = json.loads(line_bytes.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if not isinstance(raw, dict):
                            continue
                        if _extract_text_field(raw, text_field) is None:
                            continue
                        frames += 1    # a frame records() would yield (= islice unit)
                        order_key: float | None = None
                        if meta_field is not None:
                            raw_value = _lookup_raw(raw, meta_field)
                            order_key = (None if raw_value is _MISS
                                         else _parse_order_key(raw_value))
                            if order_key is None:
                                continue   # timestamp parse failure → skipped
                        part_key, boundary = self._stream_keys(raw, rel)
                        if meta_field is not None:
                            cursor = cursors.get(part_key)
                            if cursor is not None and order_key < cursor:
                                continue   # out of order → skipped
                            cursors[part_key] = order_key
                        if asm.pre_close(boundary, order_key, line_no, rel) is not None:
                            lens.append(asm.length)
                            asm.reset()
                        if asm.feed(boundary, order_key, line_no, rel):
                            lens.append(asm.length)
                            asm.reset()
            except OSError as exc:
                raise InputError(f"无法读取输入文件 {path}: {exc}") from exc
        if asm.length:
            lens.append(asm.length)
        return estimated, tuple(lens)

    def _rehearse_ui_sessions(
            self, pairs: tuple[tuple[int, str, str], ...]) -> tuple[int, ...]:
        """S23 (UI): session dry-run from the pairing table alone — index
        order + gap_steps / session_max_len / source_dir-key rules, zero extra
        I/O (meta:* ordering is text-only, so gap_s/max_span never apply).
        Pairs that the real run would skip as bad records are approximated as
        present, same as estimated_records = len(pairs)."""
        asm = _Assembler(self._cfg.stream, text=False, meta=False)
        lens: list[int] = []
        limit = self._cfg.limit
        for n, (index, tree_rel, _image_rel) in enumerate(pairs):
            if limit is not None and n >= limit:
                break
            _part_key, boundary = self._stream_keys(None, tree_rel)
            if asm.pre_close(boundary, None, index, tree_rel) is not None:
                lens.append(asm.length)
                asm.reset()
            if asm.feed(boundary, None, index, tree_rel):
                lens.append(asm.length)
                asm.reset()
        if asm.length:
            lens.append(asm.length)
        return tuple(lens)

    # ── shared helpers ──────────────────────────────────────────────────────

    def _require_root(self) -> Path:
        if self._root is None:
            raise InputError("run.input 未设置（process 模式必需）")
        if not self._root.exists():
            raise InputError(f"run.input 路径不存在: {self._root}")
        return self._root

    def _stderr_fallback(self, msg: str, *args) -> None:
        """ERROR-level stderr line for scan-time 'fail' policies when metrics is
        detached (the orchestrator pre-scan runs with metrics=None so trace
        stays untouched) — log-pipeline consumers matching ingest.* event names
        must still see the structured line (spec §7.2 fail 策略 error 级)."""
        if self.metrics is None:
            logging.getLogger("labelkit.ingest").error(
                msg, *args, extra={"stage": "ingest", "batch": 0})

    def _emit(self, ev: str, payload: dict) -> None:
        if self.metrics is not None:
            self.metrics.event(ev, stage="ingest", batch_no=0, payload=payload)

    def _bad(self, *, file: str, line_no: int | None, index: int | None,
             reason: str) -> None:
        self._report.bad_input += 1
        self._report.bad_locations.append(
            {"file": file, "line_no": line_no, "index": index, "reason": reason})

    # ── text modality ───────────────────────────────────────────────────────

    def _text_files(self, root: Path) -> list[str]:
        """Relative .jsonl file list, lexicographic by name (spec 3.2.2)."""
        if root.is_file():
            return [root.name]
        if not root.is_dir():
            raise InputError(f"run.input 不是文件也不是目录: {root}")
        files = sorted(p.name for p in root.iterdir()
                       if p.is_file() and p.suffix == ".jsonl")
        if not files:
            raise InputError(f"run.input 目录下没有 .jsonl 文件: {root}")
        return files

    def _text_records(self, root: Path) -> Iterator[Record]:
        on_bad = self._cfg.input.on_bad_line
        text_field = self._cfg.input.text_field
        for rel in self._text_files(root):
            path = root / rel if root.is_dir() else root
            # Binary read + strict per-line decode: spec 6.1 mandates UTF-8
            # JSONL and 3.2.1 mandates 原样保留 — invalid bytes must become a
            # bad line, never be silently replaced (errors="replace") and
            # ingested as altered data.
            with path.open("rb") as fh:
                for line_no, line_bytes in enumerate(fh, 1):
                    if not line_bytes.strip():
                        continue  # empty lines skipped silently (spec 6.1)
                    self._report.scanned += 1
                    reason: str | None = None
                    raw: Any = None
                    try:
                        line = line_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        reason = "行不是合法 UTF-8"
                    if reason is None:
                        try:
                            raw = json.loads(line)
                        except json.JSONDecodeError as exc:
                            reason = f"JSON 解析失败: {exc.msg}"
                    if reason is None and not isinstance(raw, dict):
                        reason = "JSON 行不是 object"
                    text: str | None = None
                    if reason is None:
                        text = _extract_text_field(raw, text_field)
                        if text is None:
                            reason = f'input.text_field "{text_field}" 未命中'
                    if reason is not None:
                        self._bad(file=rel, line_no=line_no, index=None, reason=reason)
                        self._emit("ingest.bad_line",
                                   {"file": rel, "line_no": line_no, "reason": reason})
                        if on_bad == "fail":
                            raise InputError(f"{rel}:{line_no}: {reason}"
                                             f"（input.on_bad_line = \"fail\"）")
                        continue
                    self._report.ingested += 1
                    yield Record(
                        id=_text_record_id(raw),
                        modality="text",
                        text=text,
                        raw=raw,
                        ui_tree=None,
                        image=None,
                        ref=RecordRef(source_file=rel, line_no=line_no,
                                      pair_index=None, generated_from=()),
                    )

    # ── UI modality: scan & pairing (spec 3.2.4) ────────────────────────────

    def _scan_ui(self, root: Path) -> _UIScan:
        if not root.is_dir():
            raise InputError(f"UI 模态 run.input 必须是目录: {root}")
        trees: dict[int, list[str]] = {}
        images: dict[int, list[str]] = {}
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            m = _TREE_RE.match(path.name)
            if m:
                trees.setdefault(int(m.group(1), 10), []).append(rel)
                continue
            m = _IMAGE_RE.match(path.name)
            if m:
                images.setdefault(int(m.group(1), 10), []).append(rel)
        if not trees and not images:
            raise InputError(
                f"UI 模态目录下未找到 uitree_<index>.jsonl / image_<index>.(png|jpg|jpeg) 文件: {root}")

        pairs: list[tuple[int, str, str]] = []
        conflicts: list[tuple[int, tuple[str, ...]]] = []
        missing: list[tuple[int, str, str]] = []
        for index in sorted(set(trees) | set(images)):
            t = trees.get(index, [])
            i = images.get(index, [])
            if len(t) >= 2 or len(i) >= 2:
                conflicts.append((index, tuple(t + i)))
            elif not i:
                missing.append((index, "tree", t[0]))
            elif not t:
                missing.append((index, "image", i[0]))
            else:
                pairs.append((index, t[0], i[0]))
        return _UIScan(pairs=tuple(pairs), conflicts=tuple(conflicts),
                       missing=tuple(missing))

    def _ui_records(self, root: Path) -> Iterator[Record]:
        icfg = self._cfg.input
        ui = self._scan_ui(root)
        # `scanned` is counted per index as each index is actually handled
        # (not eagerly for the whole scan), so a partially consumed stream
        # (--limit, circuit breaker, SIGINT) keeps the §6.4 report invariant
        # emitted + dropped_* + failed + bad_input = scanned + generated.

        # Anomalies are reported in ascending index order, before pair parsing.
        for index, files in ui.conflicts:
            self._report.scanned += 1
            self._report.index_conflict += 1
            self._emit("ingest.index_conflict", {"index": index, "files": list(files)})
            if icfg.on_index_conflict == "fail":
                raise InputError(f"UI index 冲突: index={index} 匹配多个文件 "
                                 f"{list(files)}（input.on_index_conflict = \"fail\"）")
            self._bad(file=files[0], line_no=None, index=index,
                      reason=f"index 冲突: {list(files)}")
        for index, present, rel in ui.missing:
            self._report.scanned += 1
            self._report.missing_pair += 1
            self._emit("ingest.missing_pair",
                       {"index": index, "present": present, "file": rel})
            if icfg.on_missing_pair == "fail":
                raise InputError(f"UI 文件缺对: index={index} 仅有 {present} 侧文件 "
                                 f"{rel}（input.on_missing_pair = \"fail\"）")
            self._bad(file=rel, line_no=None, index=index,
                      reason=f"缺对: 仅有 {present} 侧文件")

        max_bytes = icfg.max_image_mb * 1024 * 1024
        for index, tree_rel, image_rel in ui.pairs:
            self._report.scanned += 1
            tree_path = root / tree_rel
            image_path = root / image_rel
            reason = self._check_image(image_path, max_bytes)
            bad_file = image_rel
            ui_tree: UITree | None = None
            tree_bytes = b""
            if reason is None:
                bad_file = tree_rel
                try:
                    tree_bytes = tree_path.read_bytes()
                except OSError as exc:
                    reason = f"无法读取 UI 树文件: {exc}"
                else:
                    ui_tree, reason = _parse_ui_tree(tree_bytes)
            if reason is not None:
                self._bad(file=bad_file, line_no=None, index=index, reason=reason)
                self._emit("ingest.bad_line",
                           {"file": bad_file, "line_no": None, "reason": reason})
                if icfg.on_bad_line == "fail":
                    raise InputError(
                        f"{bad_file}: {reason}（input.on_bad_line = \"fail\"）")
                continue

            image_bytes = image_path.read_bytes()
            rec_id = hashlib.sha256(tree_bytes + image_bytes).hexdigest()[:16]
            ext = image_path.suffix.lower().lstrip(".")
            image_ref = ImageRef(
                path=image_path,
                format="png" if ext == "png" else "jpeg",
                size_bytes=len(image_bytes),
            )
            del image_bytes  # only hashed — pixels stay lazy (spec §2.6)
            self._report.ingested += 1
            yield Record(
                id=rec_id,
                modality="ui",
                text=None,
                raw=None,
                ui_tree=ui_tree,
                image=image_ref,
                ref=RecordRef(source_file=tree_rel, line_no=None,
                              pair_index=index, generated_from=()),
            )

    @staticmethod
    def _check_image(path: Path, max_bytes: int) -> str | None:
        """Magic-number + size check only, no full decode (spec 3.2.4).
        Returns a reason string when the image is bad, else None."""
        try:
            size = path.stat().st_size
            with path.open("rb") as fh:
                head = fh.read(8)
        except OSError as exc:
            return f"无法读取图像文件: {exc}"
        if size > max_bytes:
            return (f"图像大小 {size} 字节超出 input.max_image_mb = "
                    f"{max_bytes // (1024 * 1024)} 上限")
        ext = path.suffix.lower().lstrip(".")
        if ext == "png":
            if not head.startswith(_PNG_MAGIC):
                return "图像魔数与 .png 扩展名不符"
        else:
            if not head.startswith(_JPEG_MAGIC):
                return f"图像魔数与 .{ext} 扩展名不符"
        return None


# ── UI tree parsing (spec 3.2.4 + §6.2 field mapping) ───────────────────────

def _parse_ui_tree(data: bytes) -> tuple[UITree | None, str | None]:
    """Parse a uitree_<index>.jsonl file. Returns (tree, None) on success or
    (None, reason) when the file is empty or every line is bad (spec 3.2.4:
    空文件或全坏行 ⇒ 该记录按坏记录跳过). Individual bad node lines are skipped."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "UI 树文件不是合法 UTF-8"
    lines = [(no, ln) for no, ln in enumerate(text.splitlines(), 1) if ln.strip()]
    if not lines:
        return None, "UI 树文件为空"

    # First-line probe: object containing a `children` array → nested style.
    nested = False
    try:
        first = json.loads(lines[0][1])
        nested = isinstance(first, dict) and isinstance(first.get("children"), list)
    except json.JSONDecodeError:
        pass

    nodes: list[UINode] = []
    if nested:
        counter = [0]
        for _, ln in lines:
            try:
                obj = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            _walk_nested(obj, parent_id=None, depth=0, counter=counter, out=nodes)
    else:
        flat: list[UINode] = []
        for line_no, ln in lines:
            try:
                obj = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            flat.append(_normalize_node(obj, default_node_id=str(line_no),
                                        structural_parent=None))
        nodes = _flat_to_dfs(flat)
    if not nodes:
        return None, "UI 树文件全为坏行"
    return UITree(nodes=tuple(nodes)), None


def _flat_to_dfs(flat: list[UINode]) -> list[UINode]:
    """Rebuild flat-style nodes into depth-first order with depths derived
    from the parent_id graph (spec 4.1: ``UITree.nodes # 深度优先序`` — a type
    contract that must hold regardless of file order, e.g. BFS-ordered
    accessibility dumps). Roots = nodes whose parent_id is None or unknown;
    children keep file order; any node unreachable from a root (parent-id
    cycle) falls back to a depth-0 root, preserving file order."""
    known_ids = {n.node_id for n in flat}
    roots: list[int] = []
    children: dict[str, list[int]] = {}
    for i, node in enumerate(flat):
        if node.parent_id is None or node.parent_id not in known_ids:
            roots.append(i)
        else:
            children.setdefault(node.parent_id, []).append(i)

    out: list[UINode] = []
    visited: set[int] = set()

    def _visit(i: int, depth: int) -> None:
        if i in visited:
            return
        visited.add(i)
        node = flat[i]
        out.append(_with_depth(node, depth))
        for child in children.get(node.node_id, ()):
            _visit(child, depth + 1)

    for i in roots:
        _visit(i, 0)
    for i in range(len(flat)):  # cycle members unreachable from any root
        _visit(i, 0)
    return out


def _walk_nested(obj: dict, *, parent_id: str | None, depth: int,
                 counter: list[int], out: list[UINode]) -> None:
    """Depth-first traversal of a nested-style tree (spec 3.2.4)."""
    counter[0] += 1
    node = _normalize_node(obj, default_node_id=str(counter[0]),
                           structural_parent=parent_id, consume_children=True)
    out.append(_with_depth(node, depth))
    children = obj.get("children")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                _walk_nested(child, parent_id=node.node_id, depth=depth + 1,
                             counter=counter, out=out)


def _with_depth(node: UINode, depth: int) -> UINode:
    if node.depth == depth:
        return node
    return UINode(node_id=node.node_id, parent_id=node.parent_id, depth=depth,
                  role=node.role, text=node.text, content_desc=node.content_desc,
                  bounds=node.bounds, visible=node.visible, extra=node.extra)


def _first_present(obj: dict, keys: tuple[str, ...]) -> tuple[str | None, Any]:
    for key in keys:
        if key in obj:
            return key, obj[key]
    return None, None


def _parse_bounds(value: Any) -> tuple[int, int, int, int] | None:
    """Accepts [l,t,r,b] arrays and "[l,t][r,b]" strings (spec §6.2)."""
    if isinstance(value, list) and len(value) == 4:
        try:
            return tuple(int(v) for v in value)  # type: ignore[return-value]
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        m = _BOUNDS_STR_RE.match(value)
        if m:
            return tuple(int(g) for g in m.groups())  # type: ignore[return-value]
    return None


def _coerce_visible(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "")
    return bool(value)


def _stringify_extra(value: Any) -> str:
    if isinstance(value, str):
        return value
    return _canonical_json(value)


def _normalize_node(obj: dict, *, default_node_id: str,
                    structural_parent: str | None,
                    consume_children: bool = False) -> UINode:
    """§6.2 field mapping: first present source field per target, per-field
    defaults, remaining fields stringified into `extra` (insertion order).
    `children` is structural only in the nested style (consume_children=True);
    a flat-style row carrying a `children` field keeps it in `extra` per the
    §6.2 extra row (其余全部字段，值转字符串)."""
    consumed: set[str] = {"children"} if consume_children else set()

    key, value = _first_present(obj, _NODE_ID_KEYS)
    if key is not None:
        consumed.add(key)
    node_id = str(value) if key is not None and value is not None else default_node_id

    key, value = _first_present(obj, _PARENT_KEYS)
    if key is not None:
        consumed.add(key)
        parent_id = str(value) if value is not None else None
    else:
        parent_id = structural_parent

    key, value = _first_present(obj, _ROLE_KEYS)
    if key is not None:
        consumed.add(key)
    role = str(value) if key is not None and value is not None else "unknown"

    key, value = _first_present(obj, _TEXT_KEYS)
    if key is not None:
        consumed.add(key)
    text = str(value) if key is not None and value is not None else ""

    key, value = _first_present(obj, _DESC_KEYS)
    if key is not None:
        consumed.add(key)
    content_desc = str(value) if key is not None and value is not None else ""

    key, value = _first_present(obj, _BOUNDS_KEYS)
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    if key is not None:
        consumed.add(key)
        parsed = _parse_bounds(value)
        if parsed is not None:
            bounds = parsed

    key, value = _first_present(obj, _VISIBLE_KEYS)
    if key is not None:
        consumed.add(key)
    visible = _coerce_visible(value) if key is not None and value is not None else True

    extra = {k: _stringify_extra(v) for k, v in obj.items() if k not in consumed}
    return UINode(node_id=node_id, parent_id=parent_id, depth=0, role=role,
                  text=text, content_desc=content_desc, bounds=bounds,
                  visible=visible, extra=extra)

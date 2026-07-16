"""M11 — output emitter (spec 3.11, ch.6; CONTRACTS.md §7.10, §9).

Three channels:
- main output JSONL: appended to ``{output}.part`` with per-batch flush, delivered by
  fsync + atomic rename on finalize;
- rejects channel ``{output_stem}.rejects.jsonl`` (streamed append log, no ``.part``);
- ``{output_stem}.report.json`` (always written on finalize).

Distribution by status (v1.8 three routes, spec 3.11.2): ``active`` → main output;
``absorbed`` → NEITHER channel, counted only (the member content lives inside its
episode's sequence record); every other non-active status → rejects.

The emitter never crashes on a bad record: a failed pre-write ``validate_only`` check
(an internal invariant break) diverts the item to rejects with kind ``internal_error``
and the run continues. Record-level isolation covers meta assembly / serialization
only — an ``OSError`` on a channel write is a run-level failure (the ``.part`` file may
hold a truncated line): it propagates as ``LabelKitError`` (CLI exit 4) and marks the
run undeliverable so ``finalize`` can never rename a corrupted ``.part`` (spec 3.11.3 ④).
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from labelkit import TOOL_VERSION
from labelkit.common.errors import ErrorKind, LabelKitError
from labelkit.common.contracts.types import PipelineItem, Record, StageError

if TYPE_CHECKING:  # pragma: no cover — service modules may not exist yet at import time
    from labelkit.common.config.model import ResolvedConfig
    from labelkit.common.runtime.schema_engine import SchemaEngine

_log = logging.getLogger("labelkit.emitter")


@dataclass(frozen=True)                            # [FROZEN in CONTRACTS.md §7.10]
class EmitResult:
    emitted: int
    rejected: int


def _dumps(obj: Any) -> str:
    """Compact single-line JSON, non-ASCII preserved (CONTRACTS.md §9.1)."""
    return json.dumps(obj, ensure_ascii=False)


class Emitter:
    """Signatures frozen in CONTRACTS.md §7.10."""

    def __init__(self, cfg: "ResolvedConfig", engine: "SchemaEngine",
                 run_id: str, run_started_at: datetime):
        self._cfg = cfg
        self._engine = engine
        self._run_id = run_id
        self._run_started_at = run_started_at

        output = Path(cfg.run.output)
        self._output_path = output
        self._output_part = Path(str(output) + ".part")
        stem = output.with_suffix("")  # output path minus final suffix
        self._sidecar_path = Path(str(stem) + ".meta.jsonl")
        self._sidecar_part = Path(str(self._sidecar_path) + ".part")
        self._rejects_path = Path(str(stem) + ".rejects.jsonl")
        # Dry runs write their report to a separate name so a rehearsal never
        # clobbers the ledger of the last real run (E2E finding P2-4).
        self._report_path = Path(str(stem) + (".dryrun.report.json" if cfg.dry_run
                                              else ".report.json"))

        self._main_fh = None
        self._sidecar_fh = None
        self._rejects_fh = None

        self._emitted_total = 0
        self._rejected_total = 0
        self._status_totals: dict[str, int] = {}
        self._reject_lines_written = 0     # lines actually in the rejects FILE
        self._rejects_opened = False
        self._undeliverable = False        # a channel write failed: never rename .part
        self._progress_active = False

    # ── channel lifecycle ─────────────────────────────────────────────────

    def open(self) -> None:
        """Create/truncate the output channels. Unwritable → LabelKitError (CLI exit 4)."""
        try:
            self._main_fh = open(self._output_part, "w", encoding="utf-8")
            if self._cfg.output.meta_mode == "sidecar":
                self._sidecar_fh = open(self._sidecar_part, "w", encoding="utf-8")
            if self._cfg.output.rejects != "none":
                self._rejects_fh = open(self._rejects_path, "w", encoding="utf-8")
                self._rejects_opened = True
        except OSError as exc:
            self._close_all()
            raise LabelKitError(f"output path unwritable: {exc}") from exc

    def emit_batch(self, batch: list[PipelineItem], batch_no: int) -> EmitResult:
        """Distribute the batch by status — three routes (v1.8, spec 3.11.2):
        active → main output; absorbed → counted only (neither channel); every
        other non-active status → rejects. Appends + flush. Never raises for a
        record — but a channel-write OSError is a run-level failure and propagates
        as LabelKitError (spec 3.11.3 ④: the .part may now hold a truncated line)."""
        emitted = 0
        rejected = 0
        annotate_on = self._cfg.annotate.enabled
        for item in batch:
            try:
                if item.status == "active":
                    if annotate_on and item.annotation is None:
                        # Invariant break: active item without annotation.
                        self._divert_internal(item, batch_no,
                                              ["active item has no annotation"],
                                              "active item has no annotation")
                        self._write_reject(item, batch_no)
                        rejected += 1
                        continue
                    user_obj = self._user_object(item)
                    if annotate_on:
                        violations = self._engine.validate_only(dict(user_obj))
                        if violations:
                            # Violation text may embed data values: it goes only to
                            # the rejects channel (one array element per violation,
                            # §9.2); stderr gets a data-free summary (spec §7.1 ①).
                            self._divert_internal(
                                item, batch_no, list(violations),
                                "final validate_only failed: record "
                                f"{item.record.id}: {len(violations)} violation(s)",
                            )
                            self._write_reject(item, batch_no)
                            rejected += 1
                            continue
                    self._write_main(item, user_obj, batch_no)
                    emitted += 1
                elif item.status == "absorbed":
                    # v1.8 third route (spec 3.11.2 / §7.10): the member content
                    # lives inside its episode's sequence record — neither main
                    # output nor rejects; the generic _status_totals tally below
                    # covers the count.
                    continue
                else:
                    self._write_reject(item, batch_no)
                    rejected += 1
            except LabelKitError:
                raise  # channel write failure — run-level, never record-level
            except Exception as exc:  # noqa: BLE001 — record-level isolation is absolute
                # str(exc) may embed record content → rejects channel only; the
                # stderr log gets the exception type, the stack goes to debug (§7.6).
                self._divert_internal(item, batch_no, [f"emitter failure: {exc}"],
                                      f"emitter failure: {type(exc).__name__}", exc=exc)
                try:
                    self._write_reject(item, batch_no)
                except LabelKitError:
                    raise
                except Exception:  # reject-line assembly itself failed; count, continue
                    pass
                rejected += 1

        self._flush()
        self._emitted_total += emitted
        self._rejected_total += rejected
        for item in batch:
            self._status_totals[item.status] = self._status_totals.get(item.status, 0) + 1
        _log.info(
            "批 %d 落盘：主输出 +%d 行（累计 %d），rejects +%d（累计 %d）",
            batch_no, emitted, self._emitted_total, rejected, self._rejected_total,
            extra={"stage": "emitter", "batch": batch_no},
        )
        self._progress(batch_no)
        return EmitResult(emitted=emitted, rejected=rejected)

    def finalize(self, report: Mapping, deliver: bool = True) -> None:
        """fsync + atomic rename when deliver=True; always write report.json.
        deliver=False is dry-run-only (no .part was ever opened); v1.6: a
        circuit-break finalize passes deliver=True — completed batches ARE
        delivered, the report marking run.partial_delivery (spec 3.10.3 熔断交付).
        A prior channel-write failure forces deliver=False: a possibly-corrupted
        .part is never renamed to the final name (spec 3.11.3 ④)."""
        self._end_progress()
        deliver = deliver and not self._undeliverable
        try:
            self._deliver(self._main_fh, self._output_part, self._output_path, deliver)
            self._main_fh = None
            if self._sidecar_fh is not None:
                self._deliver(self._sidecar_fh, self._sidecar_part, self._sidecar_path, deliver)
                self._sidecar_fh = None
            if self._rejects_fh is not None:
                self._rejects_fh.flush()
                self._rejects_fh.close()
                self._rejects_fh = None
        except OSError as exc:
            raise LabelKitError(f"output delivery failed: {exc}") from exc
        finally:
            self._close_all()

        if deliver:
            _log.info(
                "finalize：fsync + rename  %s → %s（%d 行）",
                self._output_part, self._output_path, self._emitted_total,
                extra={"stage": "emitter", "batch": "-"},
            )

        try:
            self._report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            raise LabelKitError("report write failed") from exc

        # Spec 3.11.3 ③ verbatim run-tail line: rejects file (actual line count,
        # only when the channel was opened) + report path.
        if self._rejects_opened:
            _log.info(
                "已写出 %s（%d 行）与 %s",
                self._rejects_path, self._reject_lines_written, self._report_path,
                extra={"stage": "emitter", "batch": "-"},
            )
        else:
            _log.info("已写出 %s", self._report_path,
                      extra={"stage": "emitter", "batch": "-"})

        self._print_summary(report)

    # ── main channel ──────────────────────────────────────────────────────

    def _user_object(self, item: PipelineItem) -> Mapping:
        if self._cfg.annotate.enabled:
            return item.annotation.output  # type: ignore[union-attr]
        return _raw_payload(item.record)

    def _write_main(self, item: PipelineItem, user_obj: Mapping, batch_no: int) -> None:
        # Assemble + serialize EVERY line first (record-level failures stay
        # record-level and cannot leave a partial line or desynchronize the
        # sidecar's frozen line alignment, spec 3.11.3 ①); only then write.
        mode = self._cfg.output.meta_mode
        sidecar_line: str | None = None
        if mode == "inline":
            line_obj = dict(user_obj)
            line_obj["_meta"] = self._assemble_meta(item, batch_no)
            main_line = _dumps(line_obj) + "\n"
        elif mode == "sidecar":
            main_line = _dumps(dict(user_obj)) + "\n"
            sidecar_line = _dumps({"_meta": self._assemble_meta(item, batch_no)}) + "\n"
        else:  # "none"
            main_line = _dumps(dict(user_obj)) + "\n"
        self._channel_write(self._main_fh, main_line, "main output")
        if sidecar_line is not None:
            self._channel_write(self._sidecar_fh, sidecar_line, "sidecar")

    def _channel_write(self, fh, line: str, channel: str) -> None:
        """A failed write may leave a truncated line in the channel file — run-level
        failure: mark the run undeliverable and raise LabelKitError (CLI exit 4)."""
        try:
            fh.write(line)
        except OSError as exc:
            self._undeliverable = True
            raise LabelKitError(f"{channel} channel write failed: {exc}") from exc

    def _assemble_meta(self, item: PipelineItem, batch_no: int) -> dict:
        """The §6.3 `_meta` object — all keys always present; disabled stages → null."""
        rec = item.record
        return {
            "id": rec.id,
            "run": {
                "tool": TOOL_VERSION,
                "started_at": self._run_started_at.isoformat(),
                "project_file": self._cfg.project_path,
                "rubric": self._rubric_selector(),
                "seed": self._cfg.run.seed,
            },
            "source": self._source_block(rec, with_fields=True),
            # v1.8 ALWAYS-PRESENT key (§9.1): null whenever segment is disabled;
            # position source → scores mirrors the chain order.
            "stream": self._stream_block(item),
            "scores": self._scores_block(item, batch_no),
            "dedup": {"kind": item.dedup.kind} if item.dedup is not None else None,
            # v1.7 ALWAYS-PRESENT key (§9.1): null when the item carries no
            # classification (classify disabled, or never reached) — same
            # convention as the other stage keys; position dedup → annotation
            # mirrors the chain order.
            "classification": (
                {"label": item.classification.label,
                 "labels": list(item.classification.labels),
                 "source": item.classification.source}
                if item.classification is not None else None
            ),
            "annotation": self._annotation_block(item),
            "verification": self._verification_block(item),
        }

    def _rubric_selector(self) -> str:
        sel = self._cfg.quality.rubric
        if sel == "inline":
            return self._cfg.rubric.name
        if sel in ("default:text", "default:ui", "default:trajectory"):
            return sel
        # "" should have been resolved by M1; mirror the loader's resolution
        # rule (v1.8 S29: stream mode resolves the empty selector to the
        # trajectory rubric for both modalities).
        if self._cfg.segment.enabled:
            return "default:trajectory"
        return f"default:{self._cfg.run.modality}"

    def _source_block(self, rec: Record, *, with_fields: bool) -> dict:
        ref = rec.ref
        src: dict = {"file": ref.source_file}
        # Exactly one of line_no / pair_index (§9.1); generated records (both null)
        # emit "pair_index": null (CONTRACTS.md §12.20).
        if ref.line_no is not None:
            src["line_no"] = ref.line_no
        else:
            src["pair_index"] = ref.pair_index
        src["generated_from"] = list(ref.generated_from)
        if with_fields:
            src["fields"] = self._passthrough(rec)
            src["generator"] = dict(ref.generator) if ref.generator is not None else None
        elif ref.generator is not None:  # rejects: generator only when present, no fields
            src["generator"] = dict(ref.generator)
        return src

    def _passthrough(self, rec: Record) -> dict:
        raw = rec.raw or {}
        return {
            f: raw[f] for f in self._cfg.output.passthrough_fields if f in raw
        }

    def _scores_block(self, item: PipelineItem, batch_no: int) -> dict | None:
        if not item.scores:
            return None
        block: dict = {}
        mode: str | None = None
        for key, qs in item.scores.items():
            if key == "__aggregate__":
                continue
            block[key] = qs.score
            if mode is None:
                mode = qs.mode
        agg = item.scores.get("__aggregate__")
        block["__aggregate__"] = agg.score if agg is not None else None
        if agg is not None:
            mode = agg.mode
        block["mode"] = mode or (
            "pairwise_bt" if self._cfg.quality.mode == "pairwise" else "pointwise"
        )
        block["batch_no"] = batch_no
        if self._cfg.classify.enabled and item.classification is not None:
            # v1.7 (§9.1): the scoring pool this envelope was ranked in —
            # present only when classify is enabled.
            block["pool"] = item.classification.label
        return block

    def _stream_block(self, item: PipelineItem) -> dict | None:
        """The v1.8 `_meta.stream` value (§9.1 / spec §6.3): null whenever segment
        is disabled. In stream mode every main-output row is an episode (sequence
        record) — a non-sequence record here is defensive and also yields null.
        session_split / stream_repaired / segment_degraded travel as duck-typed
        envelope marks written by M10/M7/M14 (S21/S26, §7.6)."""
        rec = item.record
        if not self._cfg.segment.enabled or rec.kind != "sequence":
            return None
        members = rec.members
        return {
            "episode_id": rec.id,
            "session_id": item.session_id,
            "order_span": [_order_key_repr(members[0]), _order_key_repr(members[-1])],
            "member_count": len(members),
            "member_ids": [m.id for m in members],
            "member_sources": [_member_source(m) for m in members],
            "session_split": bool(getattr(item, "session_split", False)),
            "repaired": bool(getattr(item, "stream_repaired", False)),
            "degraded": getattr(item, "segment_degraded", None),
            "steps": (None if item.transitions is None
                      else [{"index": t.index, **t.action} for t in item.transitions]),
        }

    def _annotation_block(self, item: PipelineItem) -> dict | None:
        ann = item.annotation
        if ann is None:
            return None
        block: dict = {"model": ann.model, "attempts": ann.attempts}
        if ann.sc is not None:
            block["sc"] = dict(ann.sc)
        return block

    def _verification_block(self, item: PipelineItem) -> dict | None:
        ver = item.verification
        if ver is None:
            return None
        block: dict = {"verdict": ver.verdict, "rounds": ver.rounds}
        if self._cfg.segment.enabled:
            # v1.8 (§9.1): stream mode carries the ALWAYS-PRESENT defects key
            # ([] when none); non-stream verification blocks never carry it.
            block["defects"] = list(ver.defects)
        return block

    # ── rejects channel ───────────────────────────────────────────────────

    def _divert_internal(self, item: PipelineItem, batch_no: int, errors: list[str],
                         log_message: str, exc: BaseException | None = None) -> None:
        """Fail loudly, keep running: mark the item failed with kind internal_error.

        ``errors`` (full text, may embed data values) goes onto the item — one
        StageError per violation, so the rejects ``errors`` array keeps one element
        per violation (spec 3.11.3 ②). ``log_message`` MUST be data-free: the stderr
        run log never carries data content (spec §7.1 ①); any stack goes to debug
        level per §7.6."""
        for message in errors:
            item.errors.append(StageError(
                stage="emitter",
                kind=ErrorKind.INTERNAL_ERROR.value,
                message=message,
                retryable=False,
            ))
        item.status = "failed"
        _log.warning("internal_error: %s", log_message,
                     extra={"stage": "emitter", "batch": batch_no})
        if exc is not None:
            _log.debug("internal_error stack (record %s)", item.record.id,
                       exc_info=exc, extra={"stage": "emitter", "batch": batch_no})

    def _write_reject(self, item: PipelineItem, batch_no: int) -> None:
        if self._rejects_fh is None:
            return
        stage, reason = self._reject_stage_reason(item)
        meta: dict = {
            "id": item.record.id,
            "source": self._source_block(item.record, with_fields=False),
            "stage": stage,
            "reason": reason,
            "errors": [e.message for e in item.errors],  # [] when none (frozen)
        }
        if self._cfg.classify.enabled:
            # v1.7 R5 (§9.2): the closed five-key enumeration becomes SIX keys
            # when classify is enabled — `label` disambiguates fanned-out
            # siblings sharing a record id; null when the item was rejected
            # before ever being classified. Both refs and full tiers carry it
            # (full extends refs). Classify disabled keeps the five-key form
            # byte-identical.
            meta["label"] = (item.classification.label
                             if item.classification is not None else None)
        row: dict = {"_meta": meta}
        if self._cfg.output.rejects == "full":
            row["record"] = _raw_payload(item.record)
            if reason == ErrorKind.SCHEMA_VIOLATION.value:
                # raw_last_output travels on the item when the failing stage attached
                # it (SchemaViolation.raw_last_output); absent → null.
                row["raw_last_output"] = getattr(item, "raw_last_output", None)
        self._channel_write(self._rejects_fh, _dumps(row) + "\n", "rejects")
        self._reject_lines_written += 1

    def _reject_stage_reason(self, item: PipelineItem) -> tuple[str, str]:
        if item.status == "dropped_dup":
            kind = item.dedup.kind if item.dedup is not None else "exact"
            return "dedup", kind
        if item.status == "dropped_lowq":
            reason = ("top_ratio" if self._cfg.quality.selection == "top_ratio"
                      else "below_threshold")
            return "quality", reason
        if item.status == "dropped_verify":
            return "verify", "verify_fail"
        if item.status == "dropped_noise":
            # v1.8 (§9.2): these frames carry no item.errors entry — attribution
            # reads the duck-typed mark left by the flipping stage (M14/M7):
            # ("segment", "noise") | ("segment", "below_min_len") |
            # ("verify", "off_task_member").
            attribution = getattr(item, "noise_attribution", None)
            return attribution if attribution else ("segment", "noise")
        # failed (incl. emitter-diverted internal errors)
        if item.errors:
            first = item.errors[0]
            return first.stage, first.kind
        return "emitter", ErrorKind.INTERNAL_ERROR.value

    # ── plumbing ──────────────────────────────────────────────────────────

    def _flush(self) -> None:
        try:
            for fh in (self._main_fh, self._sidecar_fh, self._rejects_fh):
                if fh is not None:
                    fh.flush()
        except OSError as exc:
            # Buffered data may be partially on disk → same as a failed write.
            self._undeliverable = True
            raise LabelKitError(f"output flush failed: {exc}") from exc

    @staticmethod
    def _deliver(fh, part: Path, target: Path, deliver: bool) -> None:
        if fh is None:
            return
        fh.flush()
        if deliver:
            os.fsync(fh.fileno())
        fh.close()
        if deliver:
            os.rename(part, target)

    def _close_all(self) -> None:
        for fh in (self._main_fh, self._sidecar_fh, self._rejects_fh):
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
        self._main_fh = self._sidecar_fh = self._rejects_fh = None

    # ── stderr progress + summary (display, not logging — spec §7.7) ─────

    def _progress(self, batch_no: int) -> None:
        """TTY batch-level progress (spec §7.7): current batch number + cumulative
        per-status counts. Total-batch count and running cost are known only to
        M10/M9 and are not plumbed into the emitter (accepted reduction)."""
        if not sys.stderr.isatty() or self._cfg.tool.log_format == "jsonl":
            return
        t = self._status_totals
        sys.stderr.write(
            f"\rlabelkit: 批 {batch_no}"
            f"  emitted={self._emitted_total}"
            f"  dropped_dup={t.get('dropped_dup', 0)}"
            f"  dropped_lowq={t.get('dropped_lowq', 0)}"
            f"  dropped_verify={t.get('dropped_verify', 0)}"
            f"  failed={t.get('failed', 0)}"
        )
        sys.stderr.flush()
        self._progress_active = True

    def _end_progress(self) -> None:
        if self._progress_active:
            sys.stderr.write("\n")
            sys.stderr.flush()
            self._progress_active = False

    def _print_summary(self, report: Mapping) -> None:
        counts = dict(report.get("counts", {}))
        line1 = "  ".join(
            f"{k}={counts.get(k, 0)}" for k in ("scanned", "ingested", "bad_input", "generated")
        )
        line2 = "  ".join(
            f"{k}={counts.get(k, 0)}"
            for k in ("dropped_dup", "dropped_lowq", "dropped_verify", "failed", "emitted")
        )
        sys.stderr.write(
            "   ── 终版摘要（与 report.counts 逐项一致）──\n"
            f"   {line1}\n"
            f"   {line2}\n"
        )
        sys.stderr.flush()


def _raw_payload(rec: Record) -> Mapping:
    """Record content payload: text → Record.raw; UI → serialized tree + image path;
    v1.8 sequence records (S25, §9.2) → member id/source references (the frozen
    single-record shapes stay for kind="single"). Shared by the annotate-disabled
    main output and the rejects `full` tier (§9.1/§9.2)."""
    if rec.kind == "sequence":
        return {
            "kind": "sequence",
            "member_ids": [m.id for m in rec.members],
            "member_sources": [_member_source(m) for m in rec.members],
        }
    if rec.modality == "text":
        return rec.raw or {}
    return {
        "ui_tree": rec.ui_tree.serialize() if rec.ui_tree is not None else "",
        "image_path": str(rec.image.path) if rec.image is not None else "",
    }


def _member_source(member: Record) -> dict:
    """One `_meta.stream.member_sources` entry (§9.1): {"file", ...} plus exactly
    one of line_no / pair_index — the §9.1 source-block convention per member."""
    src: dict = {"file": member.ref.source_file}
    if member.ref.line_no is not None:
        src["line_no"] = member.ref.line_no
    else:
        src["pair_index"] = member.ref.pair_index
    return src


def _order_key_repr(member: Record) -> str | int | None:
    """`_meta.stream.order_span` element (spec §6.3): the member's order-key
    presentation — text = "file:line_no", UI = pair_index."""
    ref = member.ref
    if ref.line_no is not None:
        return f"{ref.source_file}:{ref.line_no}"
    return ref.pair_index

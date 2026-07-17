"""v1.10 — plain-console line formats (spec §7.7 / 3.12.3, SPEC-tui-console U21).

The plain progress line and the text final-summary lines, factored out of M11
emitter's hardcoded strings as pure functions: the SINGLE SOURCE OF TRUTH shared
by the M11 emitter and the CLI ConsoleRenderer (both import from here), so the
mid-run rich→plain handover renders byte-identical output without breaking the
cli ↛ operators dependency-direction rule. Output is pinned byte-for-byte to the
v1.9 strings by golden-snapshot tests (regression anchor layer ①, U24).

No I/O here — callers own the stderr writes.
"""
from __future__ import annotations

from typing import Mapping

__all__ = ["format_progress_line", "format_summary_lines"]

# Fixed key sets of the two plain faces (spec §7.7 plain 档 / v1.9 T16: the
# plain progress line and the text summary keep their frozen key sets —
# stitched/threads appear on the rich panel only, U18 bounded revision).
_PROGRESS_KEYS = ("dropped_dup", "dropped_lowq", "dropped_verify", "failed")
_SUMMARY_KEYS_LINE1 = ("scanned", "ingested", "bad_input", "generated")
_SUMMARY_KEYS_LINE2 = ("dropped_dup", "dropped_lowq", "dropped_verify",
                       "failed", "emitted")


def format_progress_line(batch_no: int, emitted_total: int,
                         totals: Mapping[str, int]) -> str:
    """The TTY single-line ``\\r`` batch progress (spec §7.7): batch number +
    cumulative per-status counts. Byte-identical to the v1.9 emitter string;
    missing ``totals`` keys render as 0."""
    counts = "".join(f"  {k}={totals.get(k, 0)}" for k in _PROGRESS_KEYS)
    return f"\rlabelkit: 批 {batch_no}  emitted={emitted_total}{counts}"


def format_summary_lines(counts: Mapping[str, int]) -> list[str]:
    """The three text final-summary lines (spec §7.7: 与 report.counts 逐项一致),
    newline-free — exactly what the v1.9 ``_print_summary`` writes; missing
    ``counts`` keys render as 0."""
    line1 = "  ".join(f"{k}={counts.get(k, 0)}" for k in _SUMMARY_KEYS_LINE1)
    line2 = "  ".join(f"{k}={counts.get(k, 0)}" for k in _SUMMARY_KEYS_LINE2)
    return [
        "   ── 终版摘要（与 report.counts 逐项一致）──",
        f"   {line1}",
        f"   {line2}",
    ]

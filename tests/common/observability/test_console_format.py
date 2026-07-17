"""Offline golden-snapshot tests for console_format (v1.10, SPEC-tui-console U21/U24 ①).

These byte-exact literals ARE the regression anchor's unit layer: the plain
progress line and text final-summary lines must stay byte-identical to the v1.9
emitter hardcoded strings — the emitter and the CLI renderer both import these
pure functions (single source of truth), so any drift here is a spec break.
"""
from __future__ import annotations

from labelkit.common.observability.console_format import (
    format_progress_line,
    format_summary_lines,
)

# ── format_progress_line (spec §7.7 plain 档; emitter._progress parity) ─────


def test_progress_line_golden_byte_exact():
    totals = {"dropped_dup": 3, "dropped_lowq": 5, "dropped_verify": 1,
              "failed": 0}
    line = format_progress_line(3, 41, totals)
    assert line == ("\rlabelkit: 批 3  emitted=41  dropped_dup=3"
                    "  dropped_lowq=5  dropped_verify=1  failed=0")


def test_progress_line_missing_keys_zero_fill():
    assert format_progress_line(1, 0, {}) == (
        "\rlabelkit: 批 1  emitted=0  dropped_dup=0  dropped_lowq=0"
        "  dropped_verify=0  failed=0")


def test_progress_line_fixed_key_set_ignores_extras():
    """v1.9 T16 (U18 bounded revision): the plain progress line keeps its FROZEN
    key set — stitched/threads (rich-panel-only keys) never enter this line."""
    totals = {"dropped_dup": 2, "stitched": 7, "threads": 4, "absorbed": 88}
    line = format_progress_line(2, 10, totals)
    assert line == ("\rlabelkit: 批 2  emitted=10  dropped_dup=2"
                    "  dropped_lowq=0  dropped_verify=0  failed=0")
    assert "stitched" not in line and "threads" not in line


def test_progress_line_starts_with_carriage_return_no_newline():
    line = format_progress_line(9, 9, {})
    assert line.startswith("\r")
    assert "\n" not in line


# ── format_summary_lines (spec §7.7 文本版终版摘要; _print_summary parity) ──


def test_summary_lines_golden_byte_exact():
    counts = {"scanned": 60, "ingested": 58, "bad_input": 2, "generated": 12,
              "dropped_dup": 5, "dropped_lowq": 6, "dropped_verify": 1,
              "failed": 0, "emitted": 41}
    assert format_summary_lines(counts) == [
        "   ── 终版摘要（与 report.counts 逐项一致）──",
        "   scanned=60  ingested=58  bad_input=2  generated=12",
        "   dropped_dup=5  dropped_lowq=6  dropped_verify=1  failed=0  emitted=41",
    ]


def test_summary_lines_missing_keys_zero_fill():
    assert format_summary_lines({}) == [
        "   ── 终版摘要（与 report.counts 逐项一致）──",
        "   scanned=0  ingested=0  bad_input=0  generated=0",
        "   dropped_dup=0  dropped_lowq=0  dropped_verify=0  failed=0  emitted=0",
    ]


def test_summary_lines_are_newline_free():
    """The functions return LINES; callers own the "\\n" joins (the emitter
    writes each with a trailing newline — byte parity holds after the join)."""
    for line in format_summary_lines({"scanned": 1}):
        assert "\n" not in line


def test_summary_lines_fixed_key_set_ignores_extras():
    """v1.9 T16: the text summary's two count rows keep their frozen key sets —
    extra report.counts keys (episodes/stitched/threads/...) never enter."""
    counts = {"scanned": 9, "emitted": 3, "episodes": 5, "stitched": 2,
              "threads": 3, "unprocessed": 1}
    lines = format_summary_lines(counts)
    joined = "\n".join(lines)
    assert "episodes" not in joined and "stitched" not in joined
    assert "threads" not in joined and "unprocessed" not in joined

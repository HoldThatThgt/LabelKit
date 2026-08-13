"""v1.10 —— plain 档控制台行格式（spec §7.7 / 3.12.3，SPEC-tui-console U21）。

把 M11 emitter 里硬编码的 plain 进度行与文本终版摘要行抽成纯函数：M11 emitter
与 CLI ConsoleRenderer **共用的单一真源**（两侧都从这里导入），使运行中途的
rich → plain 交接输出逐字节一致，同时不破坏 cli ↛ operators 的依赖方向规则。
输出由 golden 快照测试逐字节钉死（回归锚第 ① 层，U24）；2026-08-14 全库英文化
把这两串重冻结到英文文案上——键集、行结构与信息集不变，仅语言变。

本模块不做任何 I/O —— stderr 写入由调用方负责。
"""
from __future__ import annotations

from typing import Mapping

__all__ = ["format_progress_line", "format_summary_lines"]

# 两个 plain 面的固定键集（spec §7.7 plain 档 / v1.9 T16：plain 进度行与文本
# 摘要保持冻结键集——stitched/threads 只出现在 rich 面板，U18 有界修订）。
_PROGRESS_KEYS = ("dropped_dup", "dropped_lowq", "dropped_verify", "failed")
_SUMMARY_KEYS_LINE1 = ("scanned", "ingested", "bad_input", "generated")
_SUMMARY_KEYS_LINE2 = ("dropped_dup", "dropped_lowq", "dropped_verify",
                       "failed", "emitted")


def format_progress_line(batch_no: int, emitted_total: int,
                         totals: Mapping[str, int]) -> str:
    """渲染 TTY 单行 ``\\r`` 批进度（spec §7.7）：批号 + 各状态累计计数。

    与 emitter 的串逐字节一致；``totals`` 缺键按 0 渲染。

    @param batch_no 当前批号。
    @param emitted_total 累计已产出（emitted）条数。
    @param totals 各丢弃/失败状态的累计计数映射。
    @return 以 ``\\r`` 开头的单行进度串（不含换行）。
    """
    counts = "".join(f"  {k}={totals.get(k, 0)}" for k in _PROGRESS_KEYS)
    return f"\rlabelkit: batch {batch_no}  emitted={emitted_total}{counts}"


def format_summary_lines(counts: Mapping[str, int]) -> list[str]:
    """渲染三行文本终版摘要（spec §7.7：与 report.counts 逐项一致）。

    返回值不含换行符——与 ``_print_summary`` 写出的内容完全一致；
    ``counts`` 缺键按 0 渲染。

    @param counts 终版计数映射（= report.counts）。
    @return 三行摘要文本组成的列表。
    """
    line1 = "  ".join(f"{k}={counts.get(k, 0)}" for k in _SUMMARY_KEYS_LINE1)
    line2 = "  ".join(f"{k}={counts.get(k, 0)}" for k in _SUMMARY_KEYS_LINE2)
    return [
        "   ── final summary (matches report.counts item by item) ──",
        f"   {line1}",
        f"   {line2}",
    ]

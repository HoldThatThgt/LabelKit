"""v1.11 context-budget primitives (spec 3.9.5, CONTRACTS.md §7.17).

Margin/budget arithmetic, the zero-dependency text/image token estimators,
deterministic text fitting, the static minimum-window guarantee (w_min), the
V27① stage-error classification helper, and the ``ImageCostCalibrator`` (V19
online per-image cost calibration). Pure functions + one in-memory class; zero
third-party dependencies; zero persistence.

Layering: llm_client imports this module at runtime, so this module must never
import llm_client (or operators) at runtime — profile/bundle/config types enter
as duck-typed values (TYPE_CHECKING-only imports below). The data-adaptive
greedy window packer is OPERATOR logic and lives in segment.py (spec §3.2) —
this module supplies only the estimation/budget primitives + the calibrator.
"""
from __future__ import annotations

import json
import math
from collections import deque
from typing import TYPE_CHECKING, Literal, Mapping

from labelkit.common.errors import ContextOverflowError, OutputTruncatedError

if TYPE_CHECKING:
    from labelkit.common.config.model import (
        EmbeddingProfile,
        LLMProfile,
        ResolvedConfig,
    )
    from labelkit.common.runtime.llm_client import PromptBundle

# ── frozen constants (V7/V8/V22 — changing any value is a spec revision) ────

MARGIN_FLOOR = 256            # token
MARGIN_RATIO = 0.10           # [C-15] 量级锚定
ASCII_PER_TOKEN = 3.0         # /4 的 JSON 保守化 [C-24][C-26]
CJK_TOKEN_PER_CHAR = 1.0      # 覆盖 GLM/o200k/Qwen [C-25][C-73]；cl100k 局限见 spec
OTHER_PER_TOKEN = 2.0
MSG_OVERHEAD_TOKENS = 4       # [C-7][C-76] 3+1 保守化
DIFF_MAX_TOKENS = 128         # segment 窗内单帧 diff 行最坏常数（输出结构有界，V9）
CALIBRATION_SAFETY = 0.85     # V19 装填折扣 [C-32][C-37][C-33]
CALIBRATION_MIN_SAMPLES = 8   # 样本不足不升档 [C-32]
CALIBRATION_WINDOW_BATCHES = 8  # 批最大值窗口深度（F8：窗口单位=批，序无关）
PRIOR_INFLATION = 1.2         # 首批先验保守放大（V17）

# V22 (cross-layer dependency waiver): common may not import operators, so the
# per-stage frozen prompt-template heads enter the M1 static precheck (V13③)
# and the V9 guard as FROZEN INTEGER CONSTANTS here. Each value = est_text of
# the LARGEST frozen system/template head constant among that stage's operator
# templates (CONTRACTS §10 frozen texts); tests/common/runtime/test_budget.py
# asserts est_text(operator constant) == this dict cross-layer — revising a §10
# template turns the test red and the constant follows the CONTRACTS revision.
TEMPLATE_HEAD_TOKENS: dict[str, int] = {
    "segment": 415,   # segment._SYSTEM_HEAD (§10.9)
    "classify": 48,   # classify._SYSTEM_HEAD_MULTI (§10.8)
    "quality": 39,    # §10.2 pairwise verdict/structure sentence (inline literal)
    "annotate": 32,   # annotate._SCHEMA_SENTENCE (§10.1)
    "verify": 192,    # verify._SEQ_SYSTEM_DEFECT_TYPES (§10.5 stream variant)
    "generate": 29,   # §10.4 structure sentence (inline literal)
    "stitch": 325,    # stitch._SYSTEM_HEAD (§10.11)
    "extract": 286,   # extract._SYSTEM_HEAD (§10.10)
}

# CJK determination (V8): Unicode block CJK Unified Ideographs + its
# extensions + fullwidth punctuation — ranges enumerated (inclusive), tests pin
# exact samples. Kana/other scripts deliberately fall to the OTHER bucket.
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3000, 0x303F),    # CJK Symbols and Punctuation（、。「」等全角标点）
    (0x3400, 0x4DBF),    # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0xFF00, 0xFF60),    # Fullwidth ASCII variants（！（）：？等全角标点）
    (0xFFE0, 0xFFE6),    # Fullwidth signs（￠￡￥￦等）
    (0x20000, 0x2A6DF),  # Extension B
    (0x2A700, 0x2B73F),  # Extension C
    (0x2B740, 0x2B81F),  # Extension D
    (0x2B820, 0x2CEAF),  # Extension E
    (0x2CEB0, 0x2EBEF),  # Extension F
    (0x2EBF0, 0x2EE5F),  # Extension I
    (0x30000, 0x3134F),  # Extension G
    (0x31350, 0x323AF),  # Extension H
)

# Anthropic 28px-patch billing cap (standard tier, [C-11][C-47]); openai tile
# constants ([C-9][C-60]): 2048-square fit → shortest-side-768 normalization →
# 512px tiles → 85 base + 170 per tile.
_ANTHROPIC_PATCH_PX = 28
_ANTHROPIC_TOKEN_CAP = 1568
_OPENAI_FIT_SQUARE_PX = 2048
_OPENAI_SHORT_SIDE_PX = 768
_OPENAI_TILE_PX = 512
_OPENAI_BASE_TOKENS = 85
_OPENAI_TILE_TOKENS = 170

# Truncation marker family (classify.py member-line semantics, V9): whole
# middle lines dropped, the marker closes the gap in place.
_FIT_MARKER = "…(truncated {n} lines)"


# ── budget arithmetic (V7/V15) ───────────────────────────────────────────────

def margin(context_window: int) -> int:
    """max(256, ceil(0.10 × context_window)) — carries estimator residue,
    message envelope overhead and provider-side counting drift (V7)."""
    return max(MARGIN_FLOOR, math.ceil(MARGIN_RATIO * context_window))


def input_budget(profile: "LLMProfile") -> int:
    """context_window − max_output_tokens − margin; cw == 0 → 0 (budget OFF)."""
    cw = profile.context_window
    if cw <= 0:
        return 0
    return cw - profile.max_output_tokens - margin(cw)


def embed_budget(profile: "EmbeddingProfile") -> int:
    """context_window − margin (no output reservation, V15); cw == 0 → 0."""
    cw = profile.context_window
    if cw <= 0:
        return 0
    return cw - margin(cw)


# ── zero-dependency text estimator (V8) ─────────────────────────────────────

def _is_cjk(cp: int) -> bool:
    for lo, hi in _CJK_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def est_text(s: str) -> int:
    """ceil(ascii/3 + cjk×1.0 + other/2). Monotone over prefixes (⇒ fit_text
    may bisect on line boundaries). Known limit: cl100k-family Chinese
    (1.25–1.4 t/字) is NOT covered — recorded in spec, margin absorbs (V8)."""
    ascii_n = cjk_n = other_n = 0
    for ch in s:
        cp = ord(ch)
        if cp < 128:
            ascii_n += 1
        elif _is_cjk(cp):
            cjk_n += 1
        else:
            other_n += 1
    return math.ceil(ascii_n / ASCII_PER_TOKEN
                     + cjk_n * CJK_TOKEN_PER_CHAR
                     + other_n / OTHER_PER_TOKEN)


# ── image cost prior (V8 v3 / V17: first-batch seed, correctness lives in the
#    calibrator + the V20 overflow reaction) ────────────────────────────────

def _image_prior(provider: str, px: int) -> int:
    """Provider formula evaluated at the WORST aspect ratio for long edge px."""
    if provider == "anthropic":
        # 28px patch billing, worst square, standard-tier cap ([C-47][C-69]).
        return min(math.ceil(px / _ANTHROPIC_PATCH_PX) ** 2, _ANTHROPIC_TOKEN_CAP)
    # openai_compatible tile billing: fit into the 2048 square, then normalize
    # the shortest side to 768, then count 512px tiles. Worst aspect maximizes
    # ceil(short/512) × ceil(long/512) — short caps at 768 ([C-60]: @2048 the
    # worst portrait yields 85 + 8×170 = 1445; the square 765 is a special case).
    long_edge = min(px, _OPENAI_FIT_SQUARE_PX)
    tiles = (math.ceil(min(_OPENAI_SHORT_SIDE_PX, long_edge) / _OPENAI_TILE_PX)
             * math.ceil(long_edge / _OPENAI_TILE_PX))
    return _OPENAI_BASE_TOKENS + _OPENAI_TILE_TOKENS * tiles


def est_image_prior(profile: "LLMProfile", px: int) -> int:
    """Document-formula prior @ effective px; the calibrator's seed is this
    value × PRIOR_INFLATION (V17/V19)."""
    return _image_prior(profile.provider, px)


# ── whole-prompt estimate (V8/V16) ──────────────────────────────────────────

def est_prompt(bundle: "PromptBundle", profile: "LLMProfile",
               schema: dict | None, image_cost: int) -> int:
    """Σ est_text(text parts) + n_images × image_cost + MSG_OVERHEAD × message
    count + est_text(schema JSON — it rides the request when structured output
    is active; callers pass None otherwise). image_cost is read from the
    calibrator by the CALLER (M9 final check and the packing layers share the
    same batch-frozen source). ``profile`` is part of the frozen signature
    (§7.17) — reserved for provider-specific envelope terms."""
    del profile
    est = 0
    n_images = 0
    for message in bundle.messages:
        for part in message.parts:
            if part.kind == "text":
                est += est_text(part.text or "")
            else:
                n_images += 1
    est += n_images * image_cost
    est += MSG_OVERHEAD_TOKENS * len(bundle.messages)
    if schema is not None:
        est += est_text(json.dumps(schema, ensure_ascii=False))
    return est


# ── deterministic text fitting (V9/V15) ─────────────────────────────────────

def fit_text(s: str, budget_tokens: int,
             keep: Literal["head", "edges"]) -> str:
    """Line-boundary truncation, deterministic and idempotent (a fitting
    output refits to itself). ``head`` keeps the longest line prefix that fits
    (embed truncation, V15 — the semantic head of an embedding input is its
    front). ``edges`` keeps the first and last lines and drops middle lines,
    closing the gap with the in-place marker "…(truncated N lines)" — the
    classify.py member-line family semantics (V9); the degenerate floor is the
    marker alone standing in for every line."""
    if est_text(s) <= budget_tokens:
        return s
    lines = s.split("\n")
    n = len(lines)
    if keep == "head":
        # est_text is monotone over prefixes → bisect the largest fitting k.
        lo, hi = 0, n - 1                    # k == n is known not to fit
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if est_text("\n".join(lines[:mid])) <= budget_tokens:
                lo = mid
            else:
                hi = mid - 1
        return "\n".join(lines[:lo])
    # edges: first line + longest middle prefix + marker + last line (the
    # classify.py:92-108 scheme); at least one middle line must go.
    for keep_middle in range(n - 3, -1, -1):
        marker = _FIT_MARKER.format(n=n - 2 - keep_middle)
        candidate = "\n".join(lines[:1 + keep_middle] + [marker, lines[-1]])
        if est_text(candidate) <= budget_tokens:
            return candidate
    return _FIT_MARKER.format(n=n)


# ── static minimum-window guarantee (V9/V12) ────────────────────────────────

def min_window(cfg: "ResolvedConfig") -> int:
    """Worst-case guaranteed packing size w_min — shared by the M1 V9 guard and
    the V12 estimate upper bound. Budget undeclared (segment profile missing or
    context_window == 0) → cfg.segment.window unchanged. Declared →
    ⌊(input_budget − est_static_system) / per_frame_max⌋ (≥ 0), prior-based:
    per_frame_max = est_text(worst all-CJK digest of digest_max_chars)
    + DIFF_MAX_TOKENS + (image prior × PRIOR_INFLATION @ the working px, only
    under vision_resolved); est_static_system = the V22 frozen segment template
    head + segment.context + the two message envelopes (finer frozen template
    body lines are absorbed by margin/DIFF_MAX_TOKENS headroom by design, V7).
    NOTE: the return is NOT capped at window — w_min may exceed the cap (the
    guard needs the budget-derived value; estimate consumers clamp per V12/V26).
    Duck-typed: reads only cfg.segment and cfg.llm_profiles (M1 calls this
    before ResolvedConfig assembly)."""
    seg = cfg.segment
    prof = cfg.llm_profiles.get(seg.llm)
    if prof is None or prof.context_window <= 0:
        return seg.window
    est_static = (TEMPLATE_HEAD_TOKENS["segment"] + est_text(seg.context)
                  + 2 * MSG_OVERHEAD_TOKENS)
    per_frame = est_text("\u597d" * seg.digest_max_chars) + DIFF_MAX_TOKENS
    if seg.vision_resolved:
        px = prof.default_image_px or prof.max_image_px
        per_frame += math.ceil(est_image_prior(prof, px) * PRIOR_INFLATION)
    return max(0, (input_budget(prof) - est_static) // per_frame)


# ── V27① shared stage-error classifier ──────────────────────────────────────

def classify_stage_error(exc: BaseException) -> str | None:
    """ContextOverflowError → "context_overflow"; OutputTruncatedError →
    "output_truncated"; anything else → None (operators call this FIRST in
    their per-record error classifiers — an imprecise vocabulary would land in
    internal_error and break §3.5 attribution / overflow_records counting)."""
    if isinstance(exc, ContextOverflowError):
        return "context_overflow"
    if isinstance(exc, OutputTruncatedError):
        return "output_truncated"
    return None


# ── V19 online per-image cost calibration ───────────────────────────────────

class ImageCostCalibrator:
    """Per-profile per-image online cost calibration (run memory only, zero
    persistence — cross-run cold start is the stateless constraint's cost).
    Self-held by LLMClient (V23②), public face ``llm.calibrator``.

    Determinism guard (F8): the readable snapshot is FROZEN PER BATCH — batch
    N's packing reads only the < N batches' aggregate. Samples arrive in
    asyncio completion order, so ``freeze_batch()`` aggregates the batch max
    over the UNORDERED sample set (order-free) into the deque(maxlen=8)
    batch-max window; per-response ``observe()`` during batch N never affects
    batch N's own ``cost()`` reads (the cumulative sample count freezes too —
    otherwise the 8th mid-batch sample would flip cost() mid-batch)."""

    def __init__(self, profiles: Mapping[str, tuple[str, int]]):
        # profile name → (provider, working px): what the prior needs, derived
        # by LLMClient from its profile table (working px = default_image_px
        # or max_image_px, V18).
        self._profiles: dict[str, tuple[str, int]] = dict(profiles)
        self._current: dict[str, list[int]] = {}      # batch-open sample bucket
        self._windows: dict[str, deque[int]] = {}     # frozen batch maxes
        self._frozen_total: dict[str, int] = {}       # frozen sample count
        self._snapshot: dict[str, int] = {}           # frozen cost() readouts

    def observe(self, profile: str, prompt_tokens: int,
                text_est: int, n_images: int) -> None:
        """One sample per image-carrying response: (prompt_tokens − text_est)
        / n_images into the CURRENT batch bucket. Calls without images never
        sample; degenerate non-positive residues clamp to ≥ 1 (a max filter
        must never be poisoned by a zero/negative artifact)."""
        if n_images < 1:
            return
        sample = max(1, math.ceil((prompt_tokens - text_est) / n_images))
        self._current.setdefault(profile, []).append(sample)

    def freeze_batch(self) -> None:
        """M10 batch boundary: fold each profile's current-bucket max into its
        deque(maxlen=CALIBRATION_WINDOW_BATCHES) and refresh the readable
        snapshot (batch N+1 reads ≤ N aggregates only)."""
        for profile, samples in self._current.items():
            if not samples:
                continue
            window = self._windows.setdefault(
                profile, deque(maxlen=CALIBRATION_WINDOW_BATCHES))
            window.append(max(samples))               # order-free aggregate
            self._frozen_total[profile] = (self._frozen_total.get(profile, 0)
                                           + len(samples))
            self._snapshot[profile] = math.ceil(max(window)
                                                / CALIBRATION_SAFETY)
        self._current.clear()

    def cost(self, profile: str) -> int:
        """Packing readout — frozen snapshot ONLY: below
        CALIBRATION_MIN_SAMPLES cumulative (frozen) samples → prior ×
        PRIOR_INFLATION; else max(batch-max window) ÷ CALIBRATION_SAFETY,
        rounded up (precomputed at freeze)."""
        if self._frozen_total.get(profile, 0) < CALIBRATION_MIN_SAMPLES:
            provider, px = self._profiles[profile]
            return math.ceil(_image_prior(provider, px) * PRIOR_INFLATION)
        return self._snapshot[profile]

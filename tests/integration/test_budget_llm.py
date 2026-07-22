"""v1.11 context-budget integration tests — REAL endpoint (glm-5.2 via api.z.ai,
anthropic protocol). No mocks (project policy); auto-skipped by tests/conftest.py
when LABELKIT_ZAI_KEY is absent.

P6 evidence base (captured 2026-07-23, recorded in docs/dev/E2E-FINDINGS.md):

- Prompt-too-long on this endpoint is NEVER an HTTP 400: it is a **200-shaped
  oracle** — `{"stop_reason": "model_context_window_exceeded", "usage":
  {"input_tokens": 0, "output_tokens": 0, ...}, "content": [{"text": ""}]}` —
  exactly the [C-57] form the V24 finish-disposition handles (raises
  ContextOverflowError(phase="reactive", origin="finish")). The V20 400-body
  sniff therefore has no trigger surface on z.ai; the frozen pattern set
  nonetheless already matches the observed body ("context_window_exceeded" is
  a substring of the stop_reason riding in it), so no pattern growth was
  needed ([C-75]/[C-81] closure).
- The un-suffixed glm-5.2 effective window measured EXACTLY 2^20 = 1,048,576
  tokens, judged as input_tokens + max_tokens ≤ 1,048,576 (accepted at
  1,048,570; rejected at 1,048,582).
- Model name "glm-5.2[1m]" is REJECTED: HTTP 400, business code 1211
  "Unknown Model" ([C-59]'s 1M-suffix claim does not hold on this endpoint).
- The A7 reactive-400 exactly-once breaker feed is UNREACHABLE against this
  endpoint (no 400-shaped overflow exists here); it stays pinned offline
  (tests/operators/test_annotate.py::test_v20_degrades_bounded_then_terminal_feeds_once).

Spend discipline: overflow-rejected calls bill ZERO input tokens (usage comes
back all-zero) and return in a few seconds; the two accepted ladder rungs bill
~87k + ~267k input tokens with max_output_tokens=16; the vision calls use a
64×64 PNG. No unbounded loops.
"""
from __future__ import annotations

import math
import os

import httpx
import pytest

from labelkit.common.config.model import LLMProfile
from labelkit.common.errors import (
    ContextOverflowError,
    OutputTruncatedError,
    ProviderFatalError,
)
from labelkit.common.observability.obslog import EventLog, MetricsSink
from labelkit.common.runtime import budget
from labelkit.common.runtime.llm_client import (
    LLMClient,
    Message,
    Part,
    PromptBundle,
    overflow_body_matches,
)
from labelkit.common.contracts.types import ImageRef
from tests.conftest import ZAI_BASE_URL, ZAI_KEY_ENV, ZAI_MODEL
from tests.common.observability.test_obslog import make_cfg as obslog_cfg

pytestmark = pytest.mark.integration

# CJK+ASCII repeat unit: 15 CJK-bucket chars (incl. fullwidth ：。) + 7 ASCII
# → est_text = 15 + 7/3 ≈ 17.334 t/unit; glm-5.2 measured 12 t/unit (est is a
# ~1.44× conservative overestimate on this mix, the V8 design direction).
UNIT = "上下文预算实测：填充数据段落。abc123 "
_UNIT_EST = 15 + 7 / 3

# est-target sizes: two fit rungs (the V26 anchor 131072 and a mid rung) plus
# one rung far above the measured 2^20 window (real ≈ 1.18M tokens).
EST_ANCHOR = 131_072
EST_MID = 400_000
EST_OVERFLOW = 1_700_000


def _text_of_est(target_est: int) -> str:
    """Smallest UNIT repetition whose est_text is ≥ target_est."""
    return UNIT * math.ceil(target_est / _UNIT_EST)


def _profile(**over) -> LLMProfile:
    defaults = dict(
        name="default",
        provider="anthropic",
        base_url=ZAI_BASE_URL,
        model=ZAI_MODEL,
        api_key_env=ZAI_KEY_ENV,
        max_concurrency=2,
        timeout_s=300,
        max_retries=1,
        retry_base_delay_s=1.0,
        max_output_tokens=16,
        temperature=0.0,
        api_key=os.environ.get(ZAI_KEY_ENV, ""),
    )
    defaults.update(over)
    return LLMProfile(**defaults)


def _prompt(text: str) -> PromptBundle:
    return PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="text", text=text),)),))


def _sink(tmp_path) -> MetricsSink:
    cfg = obslog_cfg(tmp_path)
    return MetricsSink(cfg, "itest", EventLog(cfg.trace, "itest"))


# ── 1. z.ai overflow error-body capture pin (V20/[C-81] closure) ────────────

def test_zai_overflow_shape_is_200_model_context_window_exceeded():
    """Wire-level pin of the captured overflow shape: HTTP 200 +
    stop_reason="model_context_window_exceeded" + all-zero usage + empty text.
    If z.ai ever moves to a 400-shaped overflow this test flags it — the V20
    pattern set must then be re-checked against the new body."""
    text = _text_of_est(EST_OVERFLOW)
    body = {
        "model": ZAI_MODEL,
        "max_tokens": 16,
        "temperature": 0.0,
        "messages": [{"role": "user",
                      "content": [{"type": "text", "text": text}]}],
    }
    headers = {"x-api-key": os.environ[ZAI_KEY_ENV],
               "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    with httpx.Client(timeout=300.0) as client:
        resp = client.post(ZAI_BASE_URL + "/v1/messages",
                           headers=headers, json=body)

    assert resp.status_code == 200, resp.text[:500]
    data = resp.json()
    assert data["stop_reason"] == "model_context_window_exceeded", data
    usage = data["usage"]
    assert usage["input_tokens"] == 0 and usage["output_tokens"] == 0, usage
    assert all(not block.get("text") for block in data["content"]), data
    # [C-75] seed adequacy: had this body arrived on a 400, the frozen pattern
    # set would already match it ("context_window_exceeded" substring) — the
    # evidence-driven growth clause (V20) required NO addition.
    assert overflow_body_matches(resp.text)


async def test_overflow_classified_reactive_budget_off():
    """The finish-oracle disposition is NOT budget-gated (V11/V24 终局化,
    declared no-switch in SPEC §1): even a context_window=0 profile classifies
    the 200-shaped overflow as ContextOverflowError(phase="reactive")."""
    client = LLMClient({"default": _profile()}, {})
    try:
        with pytest.raises(ContextOverflowError) as ei:
            await client.complete("default", _prompt(_text_of_est(EST_OVERFLOW)))
    finally:
        await client.aclose()
    assert ei.value.phase == "reactive"
    assert ei.value.origin == "finish"
    assert ei.value.profile == "default"
    # The HTTP interaction happened (usage accumulated, zero tokens billed).
    acc = client.usage_by_profile["default"]
    assert acc.calls == 1 and acc.prompt_tokens == 0


async def test_overflow_with_declared_budget_passes_precheck_and_streak_untouched(tmp_path):
    """A large-but-wrong declared window (10M) passes the V16 precheck (est
    ~1.7M + 16 + 1M margin ≤ 10M) so the request truly dispatches; reality
    overflows → reactive ContextOverflowError. Breaker matrix (§7.8): the
    200-shaped form NEVER feeds the streak — the interaction's own ok already
    cleared it (llm.call stays status="ok", F9)."""
    sink = _sink(tmp_path)
    prof = _profile(context_window=10_000_000)
    client = LLMClient({"default": prof}, {}, sink)
    text = _text_of_est(EST_OVERFLOW)
    # Precheck arithmetic (same source complete() uses): must pass statically.
    est = budget.est_prompt(_prompt(text), prof, None,
                            image_cost=client.calibrator.cost("default"))
    assert est + prof.max_output_tokens + budget.margin(prof.context_window) \
        <= prof.context_window
    try:
        with pytest.raises(ContextOverflowError) as ei:
            await client.complete("default", _prompt(text))
    finally:
        await client.aclose()
    assert ei.value.phase == "reactive" and ei.value.origin == "finish"
    assert client.usage_by_profile["default"].calls == 1   # real dispatch
    assert sink.fatal_streak == 0                          # never fed (F9)
    assert sink.circuit_broken is False


# ── 2. effective-window measurement (V26/[C-81]) ────────────────────────────

async def test_effective_window_ladder_and_1m_suffix_probe():
    """Coarse accept/reject ladder for un-suffixed glm-5.2 (documented
    precisely in E2E-FINDINGS: window = 2^20, input+max_tokens judged):
    the 131072-est V26 anchor and a 400k-est rung FIT; a 1.7M-est rung
    (real ≈ 1.18M tokens) OVERFLOWS. Plus the [1m] model-name probe.
    Assertions are structural (fit/overflow/monotone), not content."""
    # Budget OFF so reality — not our precheck — judges every rung.
    client = LLMClient({"default": _profile()}, {})
    outcomes: dict[int, str] = {}
    billed: dict[int, int] = {}
    try:
        prev_tokens = 0
        for target in (EST_ANCHOR, EST_MID, EST_OVERFLOW):
            text = _text_of_est(target)
            assert budget.est_text(text) >= target      # rung really est-sized
            try:
                await client.complete("default", _prompt(text))
                outcomes[target] = "fit"
            except OutputTruncatedError:
                outcomes[target] = "fit"                # wrote max_tokens full
            except ContextOverflowError as exc:
                assert exc.phase == "reactive"
                outcomes[target] = "overflow"
            acc = client.usage_by_profile["default"]
            billed[target] = acc.prompt_tokens - prev_tokens
            prev_tokens = acc.prompt_tokens
    finally:
        await client.aclose()

    # V26 anchor: anything sent under the examples' declared 131072 budget
    # fits the real window with room to spare.
    assert outcomes[EST_ANCHOR] == "fit", outcomes
    assert outcomes[EST_MID] == "fit", outcomes
    # Measured window is 2^20 ≈ 1.05M; a real ~1.18M-token prompt overflows.
    # (If this ever flips to "fit" the endpoint window GREW — re-measure and
    # revisit the examples/config.toml declaration per V26.)
    assert outcomes[EST_OVERFLOW] == "overflow", outcomes
    # Monotone: no fit above the first overflow (single-threshold behavior).
    seen_overflow = False
    for target in (EST_ANCHOR, EST_MID, EST_OVERFLOW):
        if outcomes[target] == "overflow":
            seen_overflow = True
        else:
            assert not seen_overflow, outcomes
    # V8 conservatism pin on this text mix: est_text is an OVERestimate of the
    # billed prompt_tokens for fit rungs; overflow rungs bill zero.
    assert 0 < billed[EST_ANCHOR] <= EST_ANCHOR
    assert 0 < billed[EST_MID] <= EST_MID
    assert billed[EST_OVERFLOW] == 0

    # [1m] suffix probe (V26): one tiny call. Measured 2026-07-23: HTTP 400
    # business code 1211 "Unknown Model". Accepted-someday is tolerated (the
    # endpoint adding the alias is not a defect) — but it must be one of the
    # two shapes, never e.g. a silent fallback to a different model error.
    suffix_client = LLMClient(
        {"1m": _profile(name="1m", model=ZAI_MODEL + "[1m]",
                        max_output_tokens=16)}, {})
    try:
        try:
            await suffix_client.complete("1m", _prompt("1+1=?"))
            suffix_outcome = "accepted"
        except (ProviderFatalError, OutputTruncatedError) as exc:
            if isinstance(exc, OutputTruncatedError):
                suffix_outcome = "accepted"
            else:
                assert exc.status_code == 400, exc
                suffix_outcome = "rejected_unknown_model"
    finally:
        await suffix_client.aclose()
    assert suffix_outcome in {"rejected_unknown_model", "accepted"}


# ── 3. small-declared-window run semantics ──────────────────────────────────

async def test_small_window_precheck_fires_before_network_and_never_feeds_breaker(tmp_path):
    """context_window=2048 against the real endpoint: a modest prompt passes
    the precheck and succeeds for real; an oversized prompt raises
    ContextOverflowError(phase="precheck") with ZERO provider interaction
    (usage untouched); neither path feeds the breaker streak (V16)."""
    sink = _sink(tmp_path)
    prof = _profile(context_window=2048, max_output_tokens=128)
    client = LLMClient({"default": prof}, {}, sink)
    try:
        resp = await client.complete("default", _prompt("1+1 等于几？只回答数字。"))
        assert resp.text.strip()
        assert sink.fatal_streak == 0                  # success cleared/kept 0
        assert client.usage_by_profile["default"].calls == 1

        # input_budget = 2048 − 128 − 256 = 1664; est ≈ 3467 → must precheck.
        oversized = UNIT * 200
        assert budget.est_text(oversized) > budget.input_budget(prof)
        with pytest.raises(ContextOverflowError) as ei:
            await client.complete("default", _prompt(oversized))
    finally:
        await client.aclose()
    assert ei.value.phase == "precheck"
    assert ei.value.profile == "default"
    # BEFORE any network: the accumulator never saw a second call.
    assert client.usage_by_profile["default"].calls == 1
    # Breaker untouched by the precheck overflow (V16/§7.8 matrix). The
    # reactive-400 exactly-once terminal feed (A7) is unreachable against this
    # endpoint (overflow is 200-shaped here) and stays pinned offline.
    assert sink.fatal_streak == 0
    assert sink.circuit_broken is False


# ── 4. calibration convergence (V19) ────────────────────────────────────────

async def test_image_cost_calibration_converges_from_real_usage(tmp_path):
    """Two REAL vision calls with a runtime-generated 64×64 PNG through
    complete() under a declared window; freeze_batch(); then verify the V19
    sample math end-to-end: batch-max window and snapshot are usage-derived
    (sample = ceil((prompt_tokens − text_est)/n_images), snapshot =
    ceil(max/0.85)), cost() holds the prior×1.2 below CALIBRATION_MIN_SAMPLES
    (8), and crossing the threshold by REPLAYING the real measured samples
    (no synthetic numbers) flips cost() to the usage-derived readout."""
    from PIL import Image

    png_path = tmp_path / "tiny.png"
    img = Image.new("RGB", (64, 64), (200, 30, 30))
    for x in range(16, 48):
        for y in range(16, 48):
            img.putpixel((x, y), (30, 30, 200))
    img.save(png_path)
    image_ref = ImageRef(path=png_path, format="png",
                         size_bytes=png_path.stat().st_size)

    prof = _profile(name="vision", context_window=131_072,
                    max_output_tokens=64, supports_vision=True)
    client = LLMClient({"vision": prof}, {})
    calibrator = client.calibrator

    prior_readout = calibrator.cost("vision")
    # anthropic prior @ working px 2048 = 1568; ×1.2 inflation = 1882 (V8/V19).
    assert prior_readout == math.ceil(
        budget.est_image_prior(prof, prof.max_image_px)
        * budget.PRIOR_INFLATION) == 1882

    questions = ("这张图片的主色是什么？用一个词回答。",
                 "图片中央的方块是什么颜色？用一个词回答。")
    samples: list[int] = []
    prev_tokens = 0
    try:
        for question in questions:
            bundle = PromptBundle(messages=(
                Message(role="user", parts=(
                    Part(kind="text", text=question),
                    Part(kind="image", image=image_ref),
                )),))
            text_est = budget.est_prompt(bundle, prof, None, image_cost=0)
            try:
                await client.complete("vision", bundle)
            except OutputTruncatedError:
                pass                       # usage + calibration already fed
            acc = client.usage_by_profile["vision"]
            prompt_tokens = acc.prompt_tokens - prev_tokens
            prev_tokens = acc.prompt_tokens
            assert prompt_tokens > 0       # usage-bearing response (no [C-64])
            samples.append(max(1, math.ceil(prompt_tokens - text_est)))
    finally:
        await client.aclose()

    # Mid-batch: observe() fed both samples into the OPEN bucket — the frozen
    # snapshot (and therefore packing reads) must not move yet (F8).
    assert calibrator._current["vision"] == samples
    assert calibrator.cost("vision") == prior_readout

    calibrator.freeze_batch()
    expected_max = max(samples)
    expected_snapshot = math.ceil(expected_max / budget.CALIBRATION_SAFETY)
    # The frozen window/snapshot are usage-derived (the V19 sample math)…
    assert list(calibrator._windows["vision"]) == [expected_max]
    assert calibrator._snapshot["vision"] == expected_snapshot
    assert 1 <= expected_max <= 5000, samples
    # …but cost() still reads the prior below 8 cumulative samples ([C-32]).
    assert calibrator._frozen_total["vision"] == 2
    assert calibrator.cost("vision") == prior_readout

    # Cross CALIBRATION_MIN_SAMPLES by replaying the REAL measured pairs (the
    # replay re-observes endpoint-measured values, adding no synthetic data),
    # emulating the ≥8 image responses a real run accumulates.
    for i in range(budget.CALIBRATION_MIN_SAMPLES - len(samples)):
        sample = samples[i % len(samples)]
        # observe() recomputes ceil((pt − est)/n): feed (sample + 0, 0, 1).
        calibrator.observe("vision", prompt_tokens=sample, text_est=0,
                           n_images=1)
    calibrator.freeze_batch()
    assert calibrator._frozen_total["vision"] == budget.CALIBRATION_MIN_SAMPLES
    converged = calibrator.cost("vision")
    assert converged == expected_snapshot              # usage-derived readout
    if expected_snapshot != prior_readout:             # 64×64 ≪ 2048² prior —
        assert converged != prior_readout              # the delta says differ
    # Report-facing read is stable: repeated reads and mid-batch observes
    # never move the frozen snapshot (report.budget.image_cost reads cost()).
    assert calibrator.cost("vision") == converged
    calibrator.observe("vision", prompt_tokens=10 * expected_max,
                       text_est=0, n_images=1)
    assert calibrator.cost("vision") == converged

    report_style = {name: int(calibrator.cost(name))
                    for name, total in calibrator._frozen_total.items()
                    if total > 0}
    assert report_style == {"vision": converged}

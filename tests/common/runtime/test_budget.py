"""Offline unit tests for the v1.11 budget module (labelkit/common/runtime/budget.py,
CONTRACTS §7.17 / dev spec SPEC-context-budget.md §3.2) — pure logic only:
est_text pinned samples, est_image_prior for both providers, margin/input_budget
boundaries, fit_text both modes, the min_window matrix, the V22 cross-layer
TEMPLATE_HEAD_TOKENS equality, the V27① error-classification vocabulary, and the
ImageCostCalibrator batch-frozen semantics (F8)."""
from __future__ import annotations

from types import SimpleNamespace

from labelkit.common.config.model import (
    EmbeddingProfile,
    LLMProfile,
    SegmentConfig,
)
from labelkit.common.errors import (
    ContextOverflowError,
    OutputTruncatedError,
    SchemaViolation,
)
from labelkit.common.runtime import budget
from labelkit.common.runtime.budget import (
    CALIBRATION_MIN_SAMPLES,
    CALIBRATION_WINDOW_BATCHES,
    DIFF_MAX_TOKENS,
    MSG_OVERHEAD_TOKENS,
    TEMPLATE_HEAD_TOKENS,
    ImageCostCalibrator,
    classify_stage_error,
    embed_budget,
    est_image_prior,
    est_prompt,
    est_text,
    fit_text,
    input_budget,
    margin,
    min_window,
)
from labelkit.common.runtime.llm_client import Message, Part, PromptBundle


def _llm(**over) -> LLMProfile:
    defaults = dict(
        name="default", provider="openai_compatible",
        base_url="https://llm.example.com/v1", model="m",
        api_key_env="K", max_output_tokens=4096, max_image_px=2048)
    defaults.update(over)
    return LLMProfile(**defaults)


def _emb(**over) -> EmbeddingProfile:
    defaults = dict(name="emb", base_url="https://emb.example.com/v1",
                    model="e", api_key_env="K")
    defaults.update(over)
    return EmbeddingProfile(**defaults)


# ── est_text: pinned samples (spec 3.9.5 估算器行) ──────────────────────────

def test_est_text_pure_ascii():
    assert est_text("hello world") == 4          # ceil(11/3)


def test_est_text_pure_cjk():
    assert est_text("你好世界") == 4              # 4 × 1.0


def test_est_text_mixed():
    assert est_text("你好, world") == 5           # ceil(2 + 7/3)


def test_est_text_jsonish():
    assert est_text('{"intent": "写作", "n": 3}') == 10   # ceil(24/3 + 2)


def test_est_text_fullwidth_punctuation_counts_as_cjk():
    assert est_text("！？：（）") == 5             # FF00–FF60 block
    assert est_text("、。「」") == 4               # 3000–303F block


def test_est_text_other_scripts_take_half_rate():
    assert est_text("こんにちは") == 3             # kana = OTHER: ceil(5/2)


def test_est_text_empty_and_prefix_monotone():
    assert est_text("") == 0
    s = "abc\n你好\ndef"
    assert est_text(s[:4]) <= est_text(s[:7]) <= est_text(s)


# ── est_image_prior: both providers, both px tiers (V8 v3) ──────────────────

def test_anthropic_prior_hits_the_standard_tier_cap_at_2048():
    # ⌈2048/28⌉² = 5476 → capped at 1568 ([C-47][C-69])
    assert est_image_prior(_llm(provider="anthropic"), 2048) == 1568


def test_anthropic_prior_below_cap():
    assert est_image_prior(_llm(provider="anthropic"), 1092) == 39 ** 2  # 1521
    assert est_image_prior(_llm(provider="anthropic"), 28) == 1


def test_openai_prior_worst_aspect_at_2048_is_1445():
    # [C-60] audit-mandated pin: the worst PORTRAIT at long edge 2048 costs
    # 85 + 8×170 = 1445 — the square 765 is a special case, not the worst.
    assert est_image_prior(_llm(), 2048) == 1445


def test_openai_prior_smaller_px_tiers():
    assert est_image_prior(_llm(), 1024) == 85 + 4 * 170   # 765
    assert est_image_prior(_llm(), 512) == 85 + 1 * 170    # 255
    # the 2048-square fit clamps larger declarations
    assert est_image_prior(_llm(), 4096) == 1445


# ── margin / input_budget / embed_budget boundaries (V7/V15) ────────────────

def test_margin_floor_and_ratio():
    assert margin(1000) == 256            # floor wins on small windows
    assert margin(2560) == 256            # exactly the crossover
    assert margin(2570) == 257            # ceil(0.10 × cw) past the floor
    assert margin(131072) == 13108


def test_input_budget_matches_v26_worked_number():
    # V26: 131072 window, 4096 output, margin 13108 → 113868
    assert input_budget(_llm(context_window=131072)) == 113868


def test_input_budget_zero_window_means_budget_off():
    assert input_budget(_llm()) == 0                      # cw defaults to 0


def test_input_budget_non_positive_shape():
    # cw == max_output_tokens leaves no room — M1 rejects this shape (V6)
    assert input_budget(_llm(context_window=4096)) <= 0


def test_embed_budget_boundaries():
    assert embed_budget(_emb()) == 0                      # undeclared
    assert embed_budget(_emb(context_window=8192)) == 8192 - 820
    assert embed_budget(_emb(context_window=256)) <= 0    # swallowed by margin


# ── est_prompt (V8/V16) ─────────────────────────────────────────────────────

def test_est_prompt_sums_text_images_overhead_and_schema():
    bundle = PromptBundle(messages=(
        Message(role="system", parts=(Part(kind="text", text="你好世界"),)),
        Message(role="user", parts=(
            Part(kind="text", text="hello world"),
            Part(kind="image", image=None),
            Part(kind="image", image=None),
        )),
    ))
    schema = {"type": "object"}
    schema_est = est_text('{"type": "object"}')
    expected = (4 + 4                      # text parts
                + 2 * 100                  # n_images × image_cost
                + 2 * MSG_OVERHEAD_TOKENS  # message envelopes
                + schema_est)              # schema rides the request
    assert est_prompt(bundle, _llm(), schema, image_cost=100) == expected
    # schema=None (not sent) drops exactly the schema term
    assert est_prompt(bundle, _llm(), None, image_cost=100) == expected - schema_est


# ── fit_text: both modes (V9/V15) ───────────────────────────────────────────

def test_fit_text_returns_unchanged_when_it_fits():
    s = "aaaa\nbbbb"
    assert fit_text(s, 100, keep="head") is s
    assert fit_text(s, 100, keep="edges") is s


def test_fit_text_head_cuts_on_line_boundary_and_is_idempotent():
    s = "\n".join(["aaaa"] * 10)           # est = ceil(49/3) = 17
    out = fit_text(s, 8, keep="head")
    assert out == "\n".join(["aaaa"] * 5)  # largest fitting prefix
    assert est_text(out) <= 8
    assert fit_text(out, 8, keep="head") == out            # idempotent
    assert fit_text(s, 8, keep="head") == out              # deterministic


def test_fit_text_edges_keeps_first_and_last_with_marker():
    lines = [f"line-{i:02d}-xxxxxxxxxx" for i in range(12)]
    s = "\n".join(lines)
    out = fit_text(s, 30, keep="edges")
    out_lines = out.split("\n")
    assert out_lines[0] == lines[0]                        # first kept
    assert out_lines[-1] == lines[-1]                      # last kept
    assert any(l.startswith("…(truncated ") and l.endswith(" lines)")
               for l in out_lines)                         # in-place marker
    assert est_text(out) <= 30
    assert fit_text(out, 30, keep="edges") == out          # idempotent


def test_fit_text_edges_degenerate_floor_is_the_marker():
    s = "\n".join(["好" * 50 for _ in range(3)])
    assert fit_text(s, 5, keep="edges") == "…(truncated 3 lines)"


# ── min_window matrix (V9/V12/V26) ──────────────────────────────────────────

def _cfg(prof: LLMProfile, *, window=20, digest_max_chars=400,
         vision_resolved=False, context="") -> SimpleNamespace:
    # min_window reads only cfg.segment + cfg.llm_profiles (duck-typed —
    # M1 calls it before ResolvedConfig assembly).
    return SimpleNamespace(
        segment=SegmentConfig(enabled=True, llm=prof.name, window=window,
                              digest_max_chars=digest_max_chars,
                              context=context, vision_resolved=vision_resolved),
        llm_profiles={prof.name: prof})


def test_min_window_undeclared_budget_keeps_window():
    assert min_window(_cfg(_llm(), window=20)) == 20
    assert min_window(_cfg(_llm(), window=7)) == 7
    # missing profile → same degradation (existence errors are M1's job)
    cfg = _cfg(_llm(), window=20)
    cfg.llm_profiles = {}
    assert min_window(cfg) == 20


def test_min_window_large_window_exceeds_cap():
    # per-frame worst = 400 (all-CJK digest) + 128 (diff) = 528;
    # static = 415 (V22 head) + 0 (context) + 8 (envelopes) = 423
    prof = _llm(context_window=131072)
    assert min_window(_cfg(prof)) == (113868 - 423) // 528  # 214, ≥ window
    assert min_window(_cfg(prof)) >= 20


def test_min_window_small_window_yields_guard_values():
    prof = _llm(context_window=3200, max_output_tokens=1024)  # ib = 1856
    assert min_window(_cfg(prof)) == 2
    prof = _llm(context_window=3712, max_output_tokens=1024)  # ib = 2316
    assert min_window(_cfg(prof)) == 3


def test_min_window_vision_adds_the_inflated_image_prior():
    prof = _llm(provider="anthropic", context_window=131072)
    text_only = min_window(_cfg(prof))
    vision = min_window(_cfg(prof, vision_resolved=True))
    # per-frame gains ceil(1568 × 1.2) = 1882 → 528 + 1882 = 2410
    assert vision == (113868 - 423) // 2410                # 47
    assert vision < text_only


def test_min_window_vision_uses_the_working_point_px():
    prof = _llm(provider="anthropic", context_window=131072,
                default_image_px=1092)
    # prior @1092 = 1521 → ×1.2 → 1826 → per-frame 2354
    assert min_window(_cfg(prof, vision_resolved=True)) == (113868 - 423) // 2354


def test_min_window_context_eats_static_budget():
    prof = _llm(context_window=3200, max_output_tokens=1024)
    ctx = "外" * 600                                        # +600 static tokens
    assert min_window(_cfg(prof, context=ctx)) == (1856 - 423 - 600) // 528


# ── TEMPLATE_HEAD_TOKENS cross-layer equality (V22) ─────────────────────────

def test_template_head_tokens_match_operator_constants():
    """The V22 sync anchor: each budget constant equals est_text of the LARGEST
    frozen system/template head constant among that stage's operator templates
    (CONTRACTS §10). Revising a §10 template turns this red — the constant then
    follows the CONTRACTS revision (test layer may import both directions)."""
    from labelkit.operators import annotate, classify, extract, segment, stitch, verify

    heads = {
        "segment": (segment._SYSTEM_HEAD,),
        "classify": (classify._SYSTEM_HEAD_SINGLE, classify._SYSTEM_HEAD_MULTI),
        "annotate": (annotate._SCHEMA_SENTENCE,),
        "verify": (verify._SYSTEM_HEAD, verify._SYSTEM_DIMS, verify._SYSTEM_TAIL,
                   verify._SEQ_SYSTEM_HEAD, verify._SEQ_SYSTEM_DIMS,
                   verify._SEQ_SYSTEM_DEFECT_TYPES, verify._SEQ_SYSTEM_TAIL,
                   verify._SEQ_SYSTEM_STRUCTURE),
        "stitch": (stitch._SYSTEM_HEAD,),
        "extract": (extract._SYSTEM_HEAD,),
    }
    for stage, texts in heads.items():
        assert TEMPLATE_HEAD_TOKENS[stage] == max(est_text(t) for t in texts), stage


def test_template_head_tokens_quality_and_generate_inline_literals():
    """quality/generate carry their frozen heads as inline literals (§10.2 /
    §10.4) — pin the literals AND prove them live in the operators' assembly."""
    from labelkit.common.contracts.types import Record, RecordRef
    from labelkit.operators.generate import render_prompt_texts
    from labelkit.operators.quality import _build_pairwise_prompt

    quality_close = "对每条准则给出裁决。输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容："
    generate_sentence = "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容："
    assert TEMPLATE_HEAD_TOKENS["quality"] == est_text(quality_close)
    assert TEMPLATE_HEAD_TOKENS["generate"] == est_text(generate_sentence)

    rec = Record(id="r1", modality="text", text="样例", raw=None, ui_tree=None,
                 image=None, ref=RecordRef("f.jsonl", 1, None, ()))
    bundle = _build_pairwise_prompt(rec, rec, (), with_reason=False,
                                    ui_tree_max_chars=1000)
    assert quality_close in bundle.messages[0].parts[0].text
    system_text, _user = render_prompt_texts("指令", None, 4, ())
    assert generate_sentence in system_text


def test_template_head_tokens_covers_all_eight_stages():
    assert set(TEMPLATE_HEAD_TOKENS) == {"segment", "classify", "quality",
                                         "annotate", "verify", "generate",
                                         "stitch", "extract"}
    assert all(v > 0 for v in TEMPLATE_HEAD_TOKENS.values())


# ── classify_stage_error vocabulary (V27①) ──────────────────────────────────

def test_classify_stage_error_vocabulary():
    assert classify_stage_error(
        ContextOverflowError("x", phase="precheck")) == "context_overflow"
    assert classify_stage_error(
        ContextOverflowError("x", phase="reactive")) == "context_overflow"
    assert classify_stage_error(OutputTruncatedError("x")) == "output_truncated"
    assert classify_stage_error(ValueError("x")) is None
    assert classify_stage_error(SchemaViolation(["/x: bad"], "{}")) is None


# ── ImageCostCalibrator (V19/F8) ────────────────────────────────────────────

def _calibrator() -> ImageCostCalibrator:
    return ImageCostCalibrator({"p": ("anthropic", 2048)})


ANTHROPIC_PRIOR_READOUT = 1882            # ceil(1568 × 1.2)


def test_cost_before_any_sample_is_the_inflated_prior():
    assert _calibrator().cost("p") == ANTHROPIC_PRIOR_READOUT


def test_observe_freeze_cost_applies_the_085_division():
    cal = _calibrator()
    for sample_cost in (40, 55, 100, 70, 61, 88, 93, 77):   # 8 samples, max 100
        cal.observe("p", prompt_tokens=sample_cost + 10, text_est=10, n_images=1)
    cal.freeze_batch()
    assert cal.cost("p") == 118           # ceil(100 / 0.85)


def test_below_min_samples_stays_on_the_prior():
    cal = _calibrator()
    for _ in range(CALIBRATION_MIN_SAMPLES - 1):             # 7 < 8
        cal.observe("p", prompt_tokens=510, text_est=10, n_images=1)
    cal.freeze_batch()
    assert cal.cost("p") == ANTHROPIC_PRIOR_READOUT


def test_in_batch_observe_never_affects_current_batch_cost():
    cal = _calibrator()
    for _ in range(CALIBRATION_MIN_SAMPLES):
        cal.observe("p", prompt_tokens=510, text_est=10, n_images=1)
    # batch not frozen yet → batch N's own reads stay on the prior (F8)
    assert cal.cost("p") == ANTHROPIC_PRIOR_READOUT
    cal.freeze_batch()
    assert cal.cost("p") == 589           # ceil(500 / 0.85)


def test_batch_max_window_ages_out_old_batches():
    cal = _calibrator()
    for _ in range(CALIBRATION_MIN_SAMPLES):                 # batch 1: max 1000
        cal.observe("p", prompt_tokens=1010, text_est=10, n_images=1)
    cal.freeze_batch()
    assert cal.cost("p") == 1177          # ceil(1000 / 0.85)
    for _ in range(CALIBRATION_WINDOW_BATCHES):              # 8 batches of max 1
        cal.observe("p", prompt_tokens=11, text_est=10, n_images=1)
        cal.freeze_batch()
    assert cal.cost("p") == 2             # the 1000 batch fell out of deque(8)


def test_sample_is_averaged_over_images_and_clamped_positive():
    cal = _calibrator()
    cal.observe("p", prompt_tokens=210, text_est=10, n_images=2)   # → 100 each
    for _ in range(CALIBRATION_MIN_SAMPLES - 1):
        cal.observe("p", prompt_tokens=20, text_est=10, n_images=1)
    cal.freeze_batch()
    assert cal.cost("p") == 118           # max sample is (210−10)/2 = 100
    # degenerate negative residue clamps to 1, never poisons the max filter
    cal2 = _calibrator()
    for _ in range(CALIBRATION_MIN_SAMPLES):
        cal2.observe("p", prompt_tokens=10, text_est=100, n_images=2)
    cal2.freeze_batch()
    assert cal2.cost("p") == 2            # ceil(1 / 0.85)


def test_observe_without_images_is_a_no_op():
    cal = _calibrator()
    for _ in range(CALIBRATION_MIN_SAMPLES * 2):
        cal.observe("p", prompt_tokens=510, text_est=10, n_images=0)
    cal.freeze_batch()
    assert cal.cost("p") == ANTHROPIC_PRIOR_READOUT          # zero samples took


def test_batch_frozen_determinism_is_order_free():
    samples = [40, 100, 55, 88, 61, 93, 70, 77]
    cal_a, cal_b = _calibrator(), _calibrator()
    for s in samples:                                        # arrival order A
        cal_a.observe("p", prompt_tokens=s + 10, text_est=10, n_images=1)
    for s in reversed(samples):                              # arrival order B
        cal_b.observe("p", prompt_tokens=s + 10, text_est=10, n_images=1)
    cal_a.freeze_batch()
    cal_b.freeze_batch()
    assert cal_a.cost("p") == cal_b.cost("p") == 118
    assert cal_a._snapshot == cal_b._snapshot                # identical snapshots
    assert cal_a._frozen_total == cal_b._frozen_total


def test_calibrator_profiles_are_independent():
    cal = ImageCostCalibrator({"a": ("anthropic", 2048),
                               "o": ("openai_compatible", 2048)})
    assert cal.cost("a") == 1882          # ceil(1568 × 1.2)
    assert cal.cost("o") == 1734          # ceil(1445 × 1.2)
    for _ in range(CALIBRATION_MIN_SAMPLES):
        cal.observe("a", prompt_tokens=510, text_est=10, n_images=1)
    cal.freeze_batch()
    assert cal.cost("a") == 589
    assert cal.cost("o") == 1734          # untouched profile keeps its prior


def test_diff_constant_matches_spec():
    assert DIFF_MAX_TOKENS == 128

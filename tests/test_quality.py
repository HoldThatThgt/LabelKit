"""Offline unit tests for M4 quality (pure logic only — no LLM, per project policy).

Covers: Bradley-Terry MM fit on synthetic win matrices, percentile normalization,
pairing determinism under a fixed seed, top_ratio arithmetic (ties, on_unscored),
weighted aggregation, gate behavior, batch-of-1, byte-exact prompt assembly, and
judging-call failure classification / data-free failure messages (spec 7.6 / 7.1).

v1.7 per-class pooling (spec 3.4.3, CONTRACTS §7.3): pool lexicographic pre-draw and
rng-consumption determinism, per-pool top_ratio quotas, mixed per-pool modes, the N=1
pool rule, pool-dimensioned tie-counter keys, "pool" payload fields on the four quality
events, pool-level failure isolation (R15), and the classify-disabled single-pool
zero-change regression anchor.

v1.8 sequence scoring (spec 3.4.3 sequence row, CONTRACTS §10.2/§10.3): pure-text episode
rendering (no image parts), the frozen step-line format with the （摘取兜底） fallback
suffix listed separately from LLM-confirmed "other" (S16), bounded member-digest lines
(first/last always kept, in-place middle truncation marker), transitions threading through
the pairwise/pointwise judging calls, the _excerpt_payload sequence branch, and the
single-record default-kwarg regression anchor.
"""
from __future__ import annotations

import math
import random
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from labelkit.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ClassSpec,
    ClassView,
    Criterion,
    DedupConfig,
    ExtractConfig,
    GenerateConfig,
    InputConfig,
    OutputConfig,
    QualityConfig,
    ResolvedConfig,
    Rubric,
    RunConfig,
    SegmentConfig,
    StreamConfig,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)
from labelkit.errors import ProviderFatalError, ProviderRetryableError, SchemaViolation
from labelkit.quality import (
    AGGREGATE_KEY,
    QualityStage,
    _build_pairwise_prompt,
    _build_pointwise_prompt,
    _classify_call_error,
    _criterion_percentiles,
    _fit_bradley_terry_details,
    _member_digest_lines,
    _pairing_plan,
    _percentile_scores,
    _pointwise_label,
    _record_parts,
    _top_ratio_selection,
    _violation_summary,
    _weighted_aggregate,
    fit_bradley_terry,
)
from labelkit.stage import RunContext
from labelkit.types import (
    Classification,
    ImageRef,
    PipelineItem,
    QualityScore,
    Record,
    RecordRef,
    Transition,
    UINode,
    UITree,
    frame_digest,
)


# ── helpers ───────────────────────────────────────────────────────────────────

EDU = Criterion(
    key="educational_value",
    description="教育/训练价值：作为模型训练数据能带来多少可学习的能力。",
    pairwise_prompt="比较两段文本，哪一段更有教育价值、更值得用于训练语言模型？",
    weight=1.0,
    pointwise_levels=(
        "0: 无学习价值（噪声、纯广告、无意义重复）。",
        "1: 学习价值极低，内容浅表。",
        "2: 在 1 的基础上，有一定可学习内容但组织松散。",
        "3: 在 2 的基础上，内容系统、有清晰的知识或任务示范价值。",
        "4: 在 3 的基础上，示范性强，覆盖推理/解释/结构化表达等能力。",
        "5: 在 4 的基础上，训练价值突出，属稀缺的高质量样本。"),
)


def make_cfg(quality: QualityConfig, criteria: tuple[Criterion, ...] = (EDU,),
             trace: TraceConfig | None = None) -> ResolvedConfig:
    return ResolvedConfig(
        tool=ToolConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality="text"),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(),
        quality=quality,
        generate=GenerateConfig(),
        annotate=AnnotateConfig(instruction="x"),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=trace or TraceConfig(),
        rubric=Rubric(name="test-rubric", criteria=criteria),
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


class Recorder:
    """Minimal MetricsSink stand-in (records events/counters; not an LLM)."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self.counters: dict[str, int] = {}

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None):
        self.events.append((ev, {"stage": stage, "batch_no": batch_no,
                                 "record_ids": tuple(record_ids),
                                 "payload": dict(payload or {})}))

    def count(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n

    def of(self, ev_name):
        return [p for ev, p in self.events if ev == ev_name]


def make_record(rec_id: str, text: str = "样例文本") -> Record:
    return Record(id=rec_id, modality="text", text=text, raw={"text": text},
                  ui_tree=None, image=None,
                  ref=RecordRef(source_file="a.jsonl", line_no=1, pair_index=None,
                                generated_from=()))


def make_item(rec_id: str, aggregate: float | None,
              mode: str = "pairwise_bt") -> PipelineItem:
    item = PipelineItem(record=make_record(rec_id))
    item.scores[AGGREGATE_KEY] = QualityScore(
        criterion=AGGREGATE_KEY, score=aggregate, mode=mode, detail={})
    return item


def make_ctx(cfg: ResolvedConfig, metrics: Recorder | None = None,
             seed: str = "0:1:quality", engine=None) -> RunContext:
    return RunContext(cfg=cfg, llm=None, schema_engine=engine,
                      metrics=metrics or Recorder(),
                      rng=random.Random(seed), batch_no=1)


# ── Bradley-Terry fit ─────────────────────────────────────────────────────────

def test_bt_recovers_known_ordering():
    # Full round-robin where higher index always beats lower index.
    n = 5
    comparisons = [(i, j, 1.0) for i in range(n) for j in range(n) if i > j]
    log_theta = fit_bradley_terry(n, comparisons)
    assert log_theta.shape == (n,)
    for k in range(n - 1):
        assert log_theta[k] < log_theta[k + 1]
    # Renormalized to prod(theta)=1 <=> sum(log theta)=0.
    assert abs(float(np.sum(log_theta))) < 1e-9


def test_bt_pure_ties_give_equal_strengths():
    comparisons = [(0, 1, 0.5), (1, 0, 0.5), (0, 1, 0.5), (1, 0, 0.5)]
    log_theta = fit_bradley_terry(2, comparisons)
    assert log_theta[0] == pytest.approx(log_theta[1], abs=1e-9)
    assert log_theta[0] == pytest.approx(0.0, abs=1e-9)


def test_bt_disconnected_components_stay_finite():
    # {0,1} and {2,3} never compared across; regularization pseudo-counts keep theta
    # finite and unique, winners above losers within each component.
    comparisons = [(0, 1, 1.0), (0, 1, 1.0), (2, 3, 1.0), (2, 3, 1.0)]
    log_theta = fit_bradley_terry(4, comparisons)
    assert np.all(np.isfinite(log_theta))
    assert log_theta[0] > log_theta[1]
    assert log_theta[2] > log_theta[3]
    # Symmetric components produce symmetric strengths.
    assert log_theta[0] == pytest.approx(log_theta[2], abs=1e-6)


def test_bt_all_wins_finite_via_regularization():
    # 0 wins every game — without pseudo-matches theta would diverge.
    comparisons = [(0, 1, 1.0), (0, 2, 1.0), (0, 1, 1.0), (0, 2, 1.0)]
    log_theta = fit_bradley_terry(3, comparisons)
    assert np.all(np.isfinite(log_theta))
    assert log_theta[0] > max(log_theta[1], log_theta[2])


def test_bt_spec_worked_example():
    # Spec 3.4.6: r1..r4 -> idx 0..3; c1 r3>r1, c2 r4>r2, c3 r3>r2, c4 r1-r4 tie.
    comparisons = [(2, 0, 1.0), (3, 1, 1.0), (2, 1, 1.0), (0, 3, 0.5), (3, 0, 0.5)]
    log_theta, iterations, converged = _fit_bradley_terry_details(4, comparisons)
    assert log_theta[1] == pytest.approx(-3.021, abs=5e-3)   # r2
    assert log_theta[0] == pytest.approx(-0.082, abs=5e-3)   # r1
    assert log_theta[3] == pytest.approx(0.082, abs=5e-3)    # r4
    assert log_theta[2] == pytest.approx(3.021, abs=5e-3)    # r3
    # Spec: this example hits the 200-iteration cap without reaching tol=1e-6.
    assert iterations == 200
    assert not converged


def test_bt_empty_and_single():
    assert fit_bradley_terry(0, []).shape == (0,)
    single = fit_bradley_terry(1, [])
    assert single.shape == (1,)
    assert single[0] == pytest.approx(0.0, abs=1e-9)


# ── percentile normalization ──────────────────────────────────────────────────

def test_percentile_basic():
    assert _percentile_scores([-3.0, -0.1, 0.1, 3.0]) == pytest.approx(
        [0.0, 1 / 3, 2 / 3, 1.0])


def test_percentile_ties_average_rank():
    # values [1.0, 1.0, 2.0] -> ranks [1.5, 1.5, 3] -> scores [(0.5)/2, (0.5)/2, 1.0]
    assert _percentile_scores([1.0, 1.0, 2.0]) == pytest.approx([0.25, 0.25, 1.0])


def test_percentile_all_equal():
    scores = _percentile_scores([0.0, 0.0, 0.0])
    assert scores == pytest.approx([0.5, 0.5, 0.5])


def test_percentile_edges():
    assert _percentile_scores([]) == []
    assert _percentile_scores([7.0]) == [0.5]


# ── pairing determinism ───────────────────────────────────────────────────────

def test_pairing_plan_deterministic_under_fixed_seed():
    plan1 = _pairing_plan(9, 4, random.Random("42:1:quality"))
    plan2 = _pairing_plan(9, 4, random.Random("42:1:quality"))
    assert plan1 == plan2
    plan3 = _pairing_plan(9, 4, random.Random("43:1:quality"))
    assert plan1 != plan3


def test_pairing_plan_structure():
    n, rounds = 9, 4  # odd: one record sits out per round
    plan = _pairing_plan(n, rounds, random.Random("0:1:quality"))
    assert len(plan) == rounds * (n // 2)
    for round_no in range(1, rounds + 1):
        pairs = [(i, j) for r, i, j, _ in plan if r == round_no]
        participants = [k for pair in pairs for k in pair]
        assert len(participants) == len(set(participants))  # perfect matching
        assert len(participants) == 2 * (n // 2)


def test_pairing_plan_even_everyone_plays_each_round():
    n, rounds = 6, 3
    plan = _pairing_plan(n, rounds, random.Random("7:2:quality"))
    for round_no in range(1, rounds + 1):
        participants = {k for r, i, j, _ in plan if r == round_no for k in (i, j)}
        assert participants == set(range(n))


def test_pairing_plan_presentation_order_varies():
    plan = _pairing_plan(64, 4, random.Random("1:1:quality"))
    flags = {first_is_a for _, _, _, first_is_a in plan}
    assert flags == {True, False}


# ── top_ratio arithmetic ──────────────────────────────────────────────────────

def test_top_ratio_ceil_and_id_tiebreak():
    scored = [("b", 0.5), ("a", 0.5), ("c", 0.9)]
    kept, ranks = _top_ratio_selection(scored, 0.5)
    # quota = ceil(0.5 * 3) = 2; tie at 0.5 broken by id ascending -> "a" kept over "b"
    assert kept == {"c", "a"}
    assert ranks == {"c": 1, "a": 2, "b": 3}


def test_top_ratio_one_keeps_all():
    scored = [("a", 0.1), ("b", 0.9)]
    kept, _ = _top_ratio_selection(scored, 1.0)
    assert kept == {"a", "b"}


def test_top_ratio_small_ratio_keeps_at_least_one():
    kept, _ = _top_ratio_selection([("a", 0.1), ("b", 0.9), ("c", 0.5)], 0.01)
    assert kept == {"b"}  # ceil(0.03) = 1


def test_top_ratio_gate_excludes_unscored_from_quota():
    q = QualityConfig(selection="top_ratio", top_ratio=0.5, on_unscored="keep")
    cfg = make_cfg(q)
    stage = QualityStage(cfg)
    rec = Recorder()
    items = [make_item("a", 0.9), make_item("b", 0.6), make_item("c", 0.3),
             make_item("d", 0.1), make_item("u", None)]
    stage._apply_gate(items, make_ctx(cfg, rec))
    # quota = ceil(0.5 * 4 scored) = 2 -> a, b kept; unscored "u" kept without a slot.
    assert [it.status for it in items] == [
        "active", "active", "dropped_lowq", "dropped_lowq", "active"]
    gate = rec.of("quality.gate")
    assert len(gate) == 5
    by_id = {p["record_ids"][0]: p["payload"] for p in gate}
    assert by_id["a"]["decision"] == "keep" and by_id["a"]["rank"] == 1
    assert by_id["c"]["decision"] == "drop" and by_id["c"]["rank"] == 3
    assert by_id["u"]["decision"] == "keep" and by_id["u"]["aggregate"] is None
    assert "rank" not in by_id["u"]


def test_top_ratio_gate_on_unscored_drop():
    q = QualityConfig(selection="top_ratio", top_ratio=1.0, on_unscored="drop")
    cfg = make_cfg(q)
    items = [make_item("a", 0.5), make_item("u", None)]
    QualityStage(cfg)._apply_gate(items, make_ctx(cfg))
    assert items[0].status == "active"
    assert items[1].status == "dropped_lowq"


def test_threshold_gate():
    q = QualityConfig(threshold=0.3)
    cfg = make_cfg(q)
    rec = Recorder()
    items = [make_item("a", 0.0), make_item("b", 0.3), make_item("c", 0.9)]
    QualityStage(cfg)._apply_gate(items, make_ctx(cfg, rec))
    # aggregate < threshold drops; equal keeps (spec: 聚合分 < threshold ⇒ dropped_lowq)
    assert [it.status for it in items] == ["dropped_lowq", "active", "active"]
    gate = rec.of("quality.gate")
    assert {p["record_ids"][0]: p["payload"]["decision"] for p in gate} == {
        "a": "drop", "b": "keep", "c": "keep"}
    assert all(p["payload"]["threshold"] == 0.3 for p in gate)


def test_no_threshold_no_gate_events_for_scored():
    q = QualityConfig()  # threshold None, selection threshold -> score only
    cfg = make_cfg(q)
    rec = Recorder()
    items = [make_item("a", 0.1), make_item("u", None)]
    QualityStage(cfg)._apply_gate(items, make_ctx(cfg, rec))
    assert items[0].status == "active"
    assert items[1].status == "active"  # on_unscored default keep
    assert rec.of("quality.gate") == []


def test_unscored_dropped_even_without_threshold_when_on_unscored_drop():
    q = QualityConfig(on_unscored="drop")
    cfg = make_cfg(q)
    items = [make_item("a", 0.1), make_item("u", None)]
    QualityStage(cfg)._apply_gate(items, make_ctx(cfg))
    assert items[0].status == "active"
    assert items[1].status == "dropped_lowq"


# ── aggregation weights ───────────────────────────────────────────────────────

C1 = Criterion(key="c_one", description="a：b", pairwise_prompt="p", weight=1.0)
C3 = Criterion(key="c_three", description="a：b", pairwise_prompt="p", weight=3.0)


def test_weighted_aggregate():
    agg = _weighted_aggregate((C1, C3), {"c_one": 0.4, "c_three": 0.8})
    assert agg == pytest.approx((1.0 * 0.4 + 3.0 * 0.8) / 4.0)


def test_weighted_aggregate_skips_null_criteria():
    agg = _weighted_aggregate((C1, C3), {"c_one": 0.4, "c_three": None})
    assert agg == pytest.approx(0.4)  # only c_one counts in numerator AND denominator


def test_weighted_aggregate_all_null_is_none():
    assert _weighted_aggregate((C1, C3), {"c_one": None, "c_three": None}) is None


# ── batch of 1 (pairwise) ─────────────────────────────────────────────────────

async def test_pairwise_batch_of_one_fixed_half():
    q = QualityConfig(mode="pairwise", threshold=0.3)
    cfg = make_cfg(q)
    rec = Recorder()
    batch = [PipelineItem(record=make_record("solo"))]
    out = await QualityStage(cfg).run(batch, make_ctx(cfg, rec))
    assert out is batch
    item = batch[0]
    assert item.status == "active"  # 0.5 >= 0.3
    assert item.scores["educational_value"].score == 0.5
    assert item.scores["educational_value"].mode == "pairwise_bt"
    assert item.scores["educational_value"].detail == {
        "comparisons": 0, "wins": 0, "ties": 0, "log_theta": 0.0}
    assert item.scores[AGGREGATE_KEY].score == 0.5
    assert item.scores[AGGREGATE_KEY].detail == {}


async def test_stage_skips_non_active_items():
    q = QualityConfig(mode="pairwise")
    cfg = make_cfg(q)
    batch = [PipelineItem(record=make_record("dup"), status="dropped_dup")]
    out = await QualityStage(cfg).run(batch, make_ctx(cfg))
    assert out is batch
    assert batch[0].scores == {}
    assert batch[0].status == "dropped_dup"


# ── prompt assembly (byte-exact per CONTRACTS.md §10.2/§10.3, spec 3.4.6) ────

def test_pairwise_prompt_matches_spec_worked_example():
    rec_a = make_record("a1", "解释一下二分查找为什么是 O(log n)，能不能举个在通讯录里找人的例子")
    rec_b = make_record("b1", "帮我写一条请假条，明天上午要去医院")
    bundle = _build_pairwise_prompt(rec_a, rec_b, (EDU,), with_reason=True,
                                    ui_tree_max_chars=30000)
    assert bundle.temperature is None
    system, user = bundle.messages
    assert system.role == "system"
    assert system.parts[0].text == (
        "你将对两条记录进行成对质量比较。准则如下：\n"
        "- educational_value: 教育/训练价值：作为模型训练数据能带来多少可学习的能力。\n"
        "  比较两段文本，哪一段更有教育价值、更值得用于训练语言模型？\n"
        "对每条准则给出裁决。输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
        '{"judgments": [{"criterion": <准则 key>, "winner": "A"|"B"|"tie", '
        '"reason": <一句话理由>}]}')
    assert user.role == "user"
    assert user.parts[0].text == (
        "[记录 A] 解释一下二分查找为什么是 O(log n)，能不能举个在通讯录里找人的例子\n"
        "[记录 B] 帮我写一条请假条，明天上午要去医院")


def test_pairwise_prompt_without_reason():
    bundle = _build_pairwise_prompt(make_record("a"), make_record("b"), (EDU,),
                                    with_reason=False, ui_tree_max_chars=30000)
    text = bundle.messages[0].parts[0].text
    assert text.endswith('{"judgments": [{"criterion": <准则 key>, "winner": "A"|"B"|"tie"}]}')
    assert "reason" not in text


def test_pointwise_prompt_matches_spec_worked_example():
    record = make_record("r1", "帮我写一条请假条，明天上午要去医院")
    bundle = _build_pointwise_prompt(record, EDU, ui_tree_max_chars=30000)
    system, user = bundle.messages
    assert system.parts[0].text == (
        "按以下 0–5 加性量表为记录的 educational_value（教育/训练价值）打分，"
        "先给两句理由再给整数分：\n"
        "0: 无学习价值（噪声、纯广告、无意义重复）。\n"
        "1: 学习价值极低，内容浅表。\n"
        "2: 在 1 的基础上，有一定可学习内容但组织松散。\n"
        "3: 在 2 的基础上，内容系统、有清晰的知识或任务示范价值。\n"
        "4: 在 3 的基础上，示范性强，覆盖推理/解释/结构化表达等能力。\n"
        "5: 在 4 的基础上，训练价值突出，属稀缺的高质量样本。\n"
        '输出 JSON：{"scores": [{"criterion": <准则 key>, "reason": <两句理由>, '
        '"score": 0..5}]}')
    assert user.parts[0].text == "[记录内容] 帮我写一条请假条，明天上午要去医院"


def test_pointwise_label_extraction():
    assert _pointwise_label("教育/训练价值：作为模型训练数据……") == "教育/训练价值"
    assert _pointwise_label("无冒号描述") == "无冒号描述"


# ── criterion percentile normalization over the FULL batch (spec 3.4.3) ──────

def test_criterion_percentiles_rank_full_batch():
    # Ranking spans all N log θ values; an unscored record nulls only its own score
    # without shifting anyone else's rank (『将批内全部 log θ 升序排名』).
    log_theta = [0.4, -0.2, 0.1, -0.5]
    scores = _criterion_percentiles(log_theta, unscored={2})
    assert scores[2] is None
    assert scores[0] == pytest.approx(1.0)        # rank 4 of N=4
    assert scores[1] == pytest.approx(1.0 / 3.0)  # rank 2 of N=4, NOT re-ranked among 3
    assert scores[3] == pytest.approx(0.0)


def test_criterion_percentiles_no_unscored_matches_plain_percentiles():
    vals = [1.0, -1.0, 0.0]
    assert _criterion_percentiles(vals, set()) == _percentile_scores(vals)


# ── judging-call failure classification (spec 7.6 / CONTRACTS §8.1) ──────────

def test_classify_call_error():
    assert _classify_call_error(ProviderRetryableError("x", "p", 5)) == (
        "provider_retryable_exhausted", True)
    assert _classify_call_error(ProviderFatalError("x", "p", 401)) == (
        "provider_fatal", False)
    assert _classify_call_error(ValueError("boom")) == ("internal_error", False)


def test_violation_summary_is_data_free():
    # SchemaViolation's rendered violations embed instance values from LLM output, which
    # can quote record content; the summary must keep only pointers (spec 7.1, §8.4).
    secret = "身份证号 110101199003077777"
    exc = SchemaViolation(
        [f'/judgments/0/winner: 期望为枚举 ["A", "B", "tie"] 之一，实际值为 "{secret}"',
         "/judgments/0: 'reason' is a required property"],
        raw_last_output=secret)
    summary = _violation_summary(exc)
    assert secret not in summary and "110101" not in summary
    assert "/judgments/0/winner" in summary
    assert summary.startswith("2 violation(s) at ")


def test_violation_summary_root_pointer():
    assert _violation_summary(SchemaViolation([": 输出无法解析为 JSON 对象"], "raw")) == (
        "1 violation(s) at <root>")


def test_judgment_invalid_counts_tie_and_stays_active():
    cfg = make_cfg(QualityConfig(mode="pairwise"))
    stage = QualityStage(cfg)
    rec = Recorder()
    a = PipelineItem(record=make_record("a"))
    b = PipelineItem(record=make_record("b"))
    stage._record_judgment_failure(make_ctx(cfg, rec), (a, b),
                                   "pairwise judgment failed (SchemaViolation): "
                                   "1 violation(s) at <root>")
    assert rec.counters["quality.judgment_failures"] == 1
    for it in (a, b):
        assert it.status == "active"  # comparison-level: tie fallback, record survives
        assert it.errors[0].kind == "judgment_invalid"
        assert it.errors[0].retryable is False
    errs = rec.of("error")
    assert len(errs) == 1
    assert errs[0]["record_ids"] == ("a", "b")
    assert errs[0]["payload"]["kind"] == "judgment_invalid"


def test_provider_retryable_fails_records_without_judgment_failures():
    cfg = make_cfg(QualityConfig(mode="pairwise"))
    stage = QualityStage(cfg)
    rec = Recorder()
    a = PipelineItem(record=make_record("a"))
    b = PipelineItem(record=make_record("b"))
    stage._record_call_failure(make_ctx(cfg, rec), (a, b),
                               ProviderRetryableError("timeout", "default", 5),
                               "pairwise judgment call failed")
    # Provider outage is NOT a rubric diagnostic (§7.5): judgment_failures untouched.
    assert rec.counters.get("quality.judgment_failures", 0) == 0
    for it in (a, b):
        assert it.status == "failed"  # record-level per §7.6
        assert it.errors[0].kind == "provider_retryable_exhausted"
        assert it.errors[0].retryable is True
    assert rec.of("error")[0]["payload"]["kind"] == "provider_retryable_exhausted"


def test_provider_fatal_kind_reaches_error_event():
    # obslog mirrors error events at ERROR level iff payload.kind == "provider_fatal"
    # (CONTRACTS §8.1) — the kind must survive verbatim into the event payload.
    cfg = make_cfg(QualityConfig(mode="pointwise"))
    stage = QualityStage(cfg)
    rec = Recorder()
    item = PipelineItem(record=make_record("solo"))
    stage._record_call_failure(
        make_ctx(cfg, rec), (item,), ProviderFatalError("401 unauthorized", "default", 401),
        "pointwise scoring call failed for criterion educational_value")
    assert item.status == "failed"
    assert item.errors[0].kind == "provider_fatal"
    assert item.errors[0].retryable is False
    assert rec.of("error")[0]["payload"]["kind"] == "provider_fatal"


# ── judgment composition helpers ──────────────────────────────────────────────

def test_compose_orders_both_orders():
    compose = QualityStage._compose_orders
    assert compose([1, 1]) == 1            # consistent winner
    assert compose(["tie", "tie"]) == "tie"
    assert compose([1, 2]) == "tie"        # inconsistent -> tie
    assert compose([1, "tie"]) == "tie"
    assert compose([None, None]) is None   # both orders failed -> comparison failed
    assert compose([None, 1]) == "tie"     # failed order counts as tie -> inconsistent
    assert compose([None, "tie"]) == "tie"
    assert compose([1]) == 1               # single-order pass-through
    assert compose([None]) is None


def test_majority_vote():
    majority = QualityStage._majority
    assert majority([1, 1, 2], 1, 2) == 1
    assert majority([1, 2, "tie"], 1, 2) == "tie"      # no class > half
    assert majority(["tie", "tie", 1], 1, 2) == "tie"
    assert majority([None, None, None], 1, 2) is None  # all judges failed
    assert majority([None, 1, 1], 1, 2) == 1           # failed judge counts as tie
    assert majority([None, None, 1], 1, 2) == "tie"
    assert majority([2], 1, 2) == 2


# ── v1.7 per-class pooling (spec 3.4.3 按类分池, CONTRACTS §7.3) ──────────────

class StubEngine:
    """Offline complete_validated stand-in — same convention as the stub engine objects
    in test_verify.py (MockEngine) / test_annotate.py (RaisingEngine): a contract-shaped
    SchemaEngine surface, not a mock LLM server/transport. Pairwise judgments: presented
    A wins; pairs touching `tie_ids` tie. Pointwise: per-record raw score from
    `pointwise_scores` (default 3). Ids in `poison_ids` get a schema-shaped but broken
    object, driving an internal error PAST the per-call handlers (the R15 pool-poisoning
    path — the per-call try only wraps the engine await)."""

    def __init__(self, pointwise_scores: dict[str, int] | None = None,
                 tie_ids: frozenset[str] = frozenset(),
                 poison_ids: frozenset[str] = frozenset()):
        self.pointwise_scores = dict(pointwise_scores or {})
        self.tie_ids = tie_ids
        self.poison_ids = poison_ids

    async def complete_validated(self, profile, prompt, schema=None, *,
                                 record_ids=(), batch_no=0):
        props = schema["properties"]
        if "judgments" in props:  # pairwise judgment call
            if set(record_ids) & self.poison_ids:
                return {"judgments": [{}]}, None, 1, "stub-model"
            keys = props["judgments"]["items"]["properties"]["criterion"]["enum"]
            winner = "tie" if set(record_ids) & self.tie_ids else "A"
            return ({"judgments": [{"criterion": k, "winner": winner} for k in keys]},
                    None, 1, "stub-model")
        # pointwise scoring call
        if set(record_ids) & self.poison_ids:
            return {"scores": []}, None, 1, "stub-model"
        crit = props["scores"]["items"]["properties"]["criterion"]["enum"][0]
        raw = self.pointwise_scores.get(record_ids[0], 3)
        return ({"scores": [{"criterion": crit, "reason": "理由", "score": raw}]},
                None, 1, "stub-model")


def make_pooled_cfg(views: dict[str, QualityConfig],
                    rubrics: dict[str, Rubric] | None = None,
                    quality: QualityConfig | None = None) -> ResolvedConfig:
    """classify-enabled ResolvedConfig with one ClassView per label. Direct dataclass
    construction in make_cfg style — the cheapest form of the wave-1 loader-built views
    exercised in test_config.py: the stage only reads classify.enabled and
    class_views[label].(quality, rubric)."""
    base = make_cfg(quality or QualityConfig())
    class_views = {
        label: ClassView(name=label, quality=q,
                         rubric=(rubrics or {}).get(label, base.rubric),
                         annotate=base.annotate, generate=base.generate,
                         verify=base.verify, extract=ExtractConfig())
        for label, q in views.items()}
    classify = ClassifyConfig(
        enabled=True, fallback_class=sorted(views)[0], max_labels=len(views),
        classes=tuple(ClassSpec(name=n, description=f"{n} 类") for n in sorted(views)))
    return replace(base, classify=classify, class_views=class_views)


def make_classified(rec_id: str, label: str, text: str = "样例文本") -> PipelineItem:
    return PipelineItem(record=make_record(rec_id, text),
                        classification=Classification(label=label, labels=(label,),
                                                      source="llm", detail={}))


async def test_pool_lexicographic_predraw_and_rng_determinism():
    # R13 phase 1: pairing plans are pre-drawn per pool in class-name LEXICOGRAPHIC
    # order (regardless of batch order), consuming ctx.rng synchronously — so the draw
    # sequence is reproducible on an independent rng consumed pool-by-pool in that
    # order, and two runs under the same seed produce identical plans.
    cfg = make_pooled_cfg({"b": QualityConfig(mode="pairwise", rounds=1),
                           "a": QualityConfig(mode="pairwise", rounds=2)})

    async def run_once():
        rec = Recorder()
        batch = [make_classified("b1", "b"), make_classified("a1", "a"),
                 make_classified("b2", "b"), make_classified("a2", "a"),
                 make_classified("a3", "a")]
        await QualityStage(cfg).run(batch, make_ctx(cfg, rec, engine=StubEngine()))
        by_pool: dict[str, list] = {"a": [], "b": []}
        for p in rec.of("quality.judgment"):
            by_pool[p["payload"]["pool"]].append(p["payload"]["order"])
        return by_pool

    first = await run_once()
    second = await run_once()
    assert first == second  # same seed -> identical pairing + presentation plans

    # Replicate the draws: pool "a" (3 items, rounds=2) FIRST, then "b" (2 items,
    # rounds=1) on the SAME rng stream. A "b"-first order would consume differently.
    rng = random.Random("0:1:quality")
    ids_a, ids_b = ["a1", "a2", "a3"], ["b1", "b2"]
    exp_a = [{"A": ids_a[i] if f else ids_a[j], "B": ids_a[j] if f else ids_a[i]}
             for _r, i, j, f in _pairing_plan(3, 2, rng)]
    exp_b = [{"A": ids_b[i] if f else ids_b[j], "B": ids_b[j] if f else ids_b[i]}
             for _r, i, j, f in _pairing_plan(2, 1, rng)]
    assert first["a"] == exp_a
    assert first["b"] == exp_b


async def test_top_ratio_quota_is_pool_internal():
    q = QualityConfig(mode="pointwise", selection="top_ratio", top_ratio=0.5)
    cfg = make_pooled_cfg({"a": q, "b": q})
    scores = {"a1": 5, "a2": 4, "a3": 3,
              "b1": 5, "b2": 4, "b3": 3, "b4": 2, "b5": 1}
    batch = [make_classified(rec_id, rec_id[0]) for rec_id in scores]
    rec = Recorder()
    await QualityStage(cfg).run(
        batch, make_ctx(cfg, rec, engine=StubEngine(pointwise_scores=scores)))
    # Quota base = the POOL's scored survivors: a keeps ceil(0.5*3)=2, b keeps
    # ceil(0.5*5)=3 — five keeps in total, where a merged base of 8 would allow 4.
    assert {it.record.id: it.status for it in batch} == {
        "a1": "active", "a2": "active", "a3": "dropped_lowq",
        "b1": "active", "b2": "active", "b3": "active",
        "b4": "dropped_lowq", "b5": "dropped_lowq"}
    # gate ranks restart at 1 within each pool
    ranks = {p["record_ids"][0]: p["payload"]["rank"] for p in rec.of("quality.gate")}
    assert ranks == {"a1": 1, "a2": 2, "a3": 3,
                     "b1": 1, "b2": 2, "b3": 3, "b4": 4, "b5": 5}


async def test_mixed_pool_modes_coexist():
    cfg = make_pooled_cfg({"a": QualityConfig(mode="pairwise", rounds=1, threshold=0.4),
                           "b": QualityConfig(mode="pointwise", threshold=0.5)})
    batch = [make_classified("a1", "a"), make_classified("a2", "a"),
             make_classified("b1", "b"), make_classified("b2", "b")]
    engine = StubEngine(pointwise_scores={"b1": 4, "b2": 1})
    await QualityStage(cfg).run(batch, make_ctx(cfg, engine=engine))
    a1, a2, b1, b2 = batch
    # pairwise pool: BT percentiles under mode pairwise_bt, gated at ITS threshold
    assert {a1.scores["educational_value"].mode,
            a2.scores["educational_value"].mode} == {"pairwise_bt"}
    assert sorted([a1.scores[AGGREGATE_KEY].score,
                   a2.scores[AGGREGATE_KEY].score]) == [0.0, 1.0]
    assert sorted([a1.status, a2.status]) == ["active", "dropped_lowq"]
    # pointwise pool: absolute /5 scores under mode pointwise, gated at ITS threshold
    assert b1.scores["educational_value"].mode == "pointwise"
    assert b1.scores[AGGREGATE_KEY].score == pytest.approx(0.8)
    assert b2.scores[AGGREGATE_KEY].score == pytest.approx(0.2)
    assert b1.status == "active" and b2.status == "dropped_lowq"


async def test_pool_of_one_scores_fixed_half_with_no_calls():
    cfg = make_pooled_cfg({"a": QualityConfig(mode="pairwise", threshold=0.6),
                           "b": QualityConfig(mode="pairwise", rounds=1, threshold=0.3)})
    solo = make_classified("solo", "a")
    batch = [solo, make_classified("b1", "b"), make_classified("b2", "b")]
    rec = Recorder()
    await QualityStage(cfg).run(batch, make_ctx(cfg, rec, engine=StubEngine()))
    # N=1 rule applies PER POOL: the solo pool makes no judging calls, fixed 0.5
    assert solo.scores["educational_value"].score == 0.5
    assert solo.scores["educational_value"].detail == {
        "comparisons": 0, "wins": 0, "ties": 0, "log_theta": 0.0}
    assert solo.scores[AGGREGATE_KEY].score == 0.5
    assert solo.status == "dropped_lowq"  # gated by ITS pool's threshold 0.6
    judgments = rec.of("quality.judgment")
    assert len(judgments) == 1  # only pool b judged
    assert all("solo" not in p["record_ids"] for p in judgments)
    assert sorted(it.status for it in batch[1:]) == ["active", "dropped_lowq"]


async def test_tie_counters_pool_dimensioned_keys():
    # R12: the same criterion key lives in both pools' rubrics — tallies must not mix.
    q = QualityConfig(mode="pairwise", rounds=1)
    cfg = make_pooled_cfg({"a": q, "b": q})
    batch = [make_classified("a1", "a"), make_classified("a2", "a"),
             make_classified("b1", "b"), make_classified("b2", "b")]
    rec = Recorder()
    engine = StubEngine(tie_ids=frozenset({"a1", "a2"}))
    await QualityStage(cfg).run(batch, make_ctx(cfg, rec, engine=engine))
    assert rec.counters == {
        "quality.tie_outcomes.a.educational_value": 1,
        "quality.tie_comparisons.a.educational_value": 1,
        "quality.tie_outcomes.b.educational_value": 0,
        "quality.tie_comparisons.b.educational_value": 1,
    }


async def test_events_carry_pool_when_classify_enabled():
    # R16: quality.judgment / quality.pointwise / quality.bt_fit / quality.gate all
    # carry their pool label when classify is enabled.
    cfg = make_pooled_cfg({"a": QualityConfig(mode="pairwise", rounds=1, threshold=0.4),
                           "b": QualityConfig(mode="pointwise", threshold=0.4)})
    batch = [make_classified("a1", "a"), make_classified("a2", "a"),
             make_classified("b1", "b"), make_classified("b2", "b")]
    rec = Recorder()
    await QualityStage(cfg).run(batch, make_ctx(cfg, rec, engine=StubEngine()))
    assert [p["payload"]["pool"] for p in rec.of("quality.judgment")] == ["a"]
    assert [p["payload"]["pool"] for p in rec.of("quality.bt_fit")] == ["a"]
    assert {p["payload"]["pool"] for p in rec.of("quality.pointwise")} == {"b"}
    assert {p["record_ids"][0]: p["payload"]["pool"] for p in rec.of("quality.gate")} == {
        "a1": "a", "a2": "a", "b1": "b", "b2": "b"}


async def test_pool_isolation_one_pool_poisoned_other_survives():
    # R15: an internal error inside pool a (escaping the per-call handlers) fails ONLY
    # pool a's active items; pool b scores and gates as if a never existed.
    cfg = make_pooled_cfg({"a": QualityConfig(mode="pointwise", threshold=0.4),
                           "b": QualityConfig(mode="pointwise", threshold=0.4)})
    batch = [make_classified("a1", "a"), make_classified("a2", "a"),
             make_classified("b1", "b"), make_classified("b2", "b")]
    rec = Recorder()
    engine = StubEngine(pointwise_scores={"b1": 4, "b2": 1},
                        poison_ids=frozenset({"a1", "a2"}))
    await QualityStage(cfg).run(batch, make_ctx(cfg, rec, engine=engine))
    a1, a2, b1, b2 = batch
    for it in (a1, a2):
        assert it.status == "failed"
        assert it.errors[0].kind == "internal_error"
        assert "quality stage internal error" in it.errors[0].message
        assert AGGREGATE_KEY not in it.scores
    assert {rid for p in rec.of("error") for rid in p["record_ids"]} == {"a1", "a2"}
    assert b1.status == "active"
    assert b1.scores[AGGREGATE_KEY].score == pytest.approx(0.8)
    assert b2.status == "dropped_lowq"
    assert b2.scores[AGGREGATE_KEY].score == pytest.approx(0.2)


async def test_classify_disabled_single_pool_zero_change_regression():
    # Zero-change anchor (spec 3.4.3): classify disabled -> ONE anonymous pool whose
    # observable surface is field-for-field the pre-v1.7 shape — flat tie-counter keys,
    # no "pool" key in any event payload, identical score/gate structures.
    q = QualityConfig(mode="pairwise", rounds=1, threshold=0.4)
    cfg = make_cfg(q)
    rec = Recorder()
    batch = [PipelineItem(record=make_record("r1")),
             PipelineItem(record=make_record("r2"))]
    await QualityStage(cfg).run(batch, make_ctx(cfg, rec, engine=StubEngine()))

    # replicate the anonymous pool's single draw to resolve the presented order
    (_round, i, j, first_is_a), = _pairing_plan(2, 1, random.Random("0:1:quality"))
    ids = ["r1", "r2"]
    a_id, b_id = (ids[i], ids[j]) if first_is_a else (ids[j], ids[i])

    judgment, = rec.of("quality.judgment")
    assert judgment["payload"] == {  # exact payload: no pool / judge / excerpt keys
        "order": {"A": a_id, "B": b_id}, "model": "stub-model",
        "judgments": [{"criterion": "educational_value", "winner": "A"}]}
    assert judgment["record_ids"] == (ids[i], ids[j])  # sampling order

    bt_fit, = rec.of("quality.bt_fit")
    assert set(bt_fit["payload"]) == {"criterion", "iterations", "converged",
                                      "comparisons"}
    assert bt_fit["payload"]["criterion"] == "educational_value"
    assert bt_fit["payload"]["comparisons"] == 1

    assert rec.counters == {"quality.tie_outcomes.educational_value": 0,
                            "quality.tie_comparisons.educational_value": 1}

    winner = next(it for it in batch if it.record.id == a_id)
    loser = next(it for it in batch if it.record.id == b_id)
    assert winner.scores["educational_value"].score == 1.0
    assert winner.scores["educational_value"].mode == "pairwise_bt"
    assert winner.scores["educational_value"].detail["comparisons"] == 1
    assert winner.scores["educational_value"].detail["wins"] == 1
    assert winner.scores["educational_value"].detail["ties"] == 0
    assert winner.scores[AGGREGATE_KEY].score == 1.0
    assert winner.status == "active"
    assert loser.scores["educational_value"].score == 0.0
    assert loser.scores[AGGREGATE_KEY].score == 0.0
    assert loser.status == "dropped_lowq"
    gate_by_id = {p["record_ids"][0]: p["payload"] for p in rec.of("quality.gate")}
    assert gate_by_id[a_id] == {"aggregate": 1.0, "decision": "keep", "threshold": 0.4}
    assert gate_by_id[b_id] == {"aggregate": 0.0, "decision": "drop", "threshold": 0.4}


async def test_classify_enabled_unclassified_items_form_anonymous_pool():
    # Defensive path (spec: classify 关闭或项无分类 ⇒ 单一匿名池): classify enabled but
    # items carry no classification -> ONE anonymous pool under the GLOBAL config with
    # the pre-v1.7 byte shape (flat counter keys, no pool payload field).
    cfg = make_pooled_cfg({"a": QualityConfig(mode="pairwise", rounds=1)},
                          quality=QualityConfig(mode="pairwise", rounds=1))
    batch = [PipelineItem(record=make_record("r1")),
             PipelineItem(record=make_record("r2"))]
    rec = Recorder()
    await QualityStage(cfg).run(batch, make_ctx(cfg, rec, engine=StubEngine()))
    judgment, = rec.of("quality.judgment")
    assert "pool" not in judgment["payload"]
    assert set(rec.counters) == {"quality.tie_outcomes.educational_value",
                                 "quality.tie_comparisons.educational_value"}
    assert all(it.scores[AGGREGATE_KEY].score is not None for it in batch)


# ── v1.8 sequence scoring (spec 3.4.3 sequence row, CONTRACTS §10.2/§10.3) ────

def make_ui_frame(rec_id: str, title: str) -> Record:
    nodes = (
        UINode("1", None, 0, "FrameLayout", "", "", (0, 0, 1080, 1920), True,
               {"package": "com.demo.app"}),
        UINode("2", "1", 1, "TextView", title, "", (0, 0, 1080, 200), True, {}),
        UINode("3", "1", 1, "Button", "下一步", "", (72, 952, 1008, 1096), True, {}),
    )
    return Record(id=rec_id, modality="ui", text=None, raw=None, ui_tree=UITree(nodes),
                  image=ImageRef(path=Path(f"{rec_id}.png"), format="png", size_bytes=1),
                  ref=RecordRef("frames/x.jsonl", None, 1, ()))


def make_text_frame(rec_id: str, text: str) -> Record:
    return Record(id=rec_id, modality="text", text=text, raw={"text": text},
                  ui_tree=None, image=None,
                  ref=RecordRef("frames.jsonl", 1, None, ()))


def make_episode(members: tuple[Record, ...], ep_id: str = "ep0001") -> Record:
    first = members[0]
    return Record(id=ep_id, modality=first.modality, text=None, raw=None, ui_tree=None,
                  image=None, ref=first.ref, kind="sequence", members=members)


def make_ui_episode(n: int = 5, ep_id: str = "ep0001", title: str = "屏幕") -> Record:
    return make_episode(tuple(make_ui_frame(f"{ep_id}f{i}", f"{title}{i}")
                              for i in range(n)), ep_id)


SEQ_TRANSITIONS = (
    Transition(index=0, action={"action_type": "click", "target": "下一步", "value": None,
                                "description": "点击下一步按钮"},
               model="m", attempts=1, detail={}),
    Transition(index=1, action={"action_type": "input_text", "target": "搜索框",
                                "value": "咖啡", "description": "输入搜索词"},
               model="m", attempts=1, detail={}),
    Transition(index=2, action={"action_type": "other", "target": None, "value": None,
                                "description": "两帧间变化无法归因"},
               model="m", attempts=2,
               detail={"kind": "extraction_invalid", "message": "repair exhausted"}),
    Transition(index=3, action={"action_type": "other", "target": None, "value": None,
                                "description": "用户在等待加载"},
               model="m", attempts=1, detail={}),
)

STEP_LINES = (
    "0. click（对象: 下一步；值: —）点击下一步按钮",
    "1. input_text（对象: 搜索框；值: 咖啡）输入搜索词",
    "2. other（对象: —；值: —）两帧间变化无法归因（摘取兜底）",
    "3. other（对象: —；值: —）用户在等待加载",
)


class CapturingStubEngine(StubEngine):
    """StubEngine that additionally records every PromptBundle it receives."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.prompts = []

    async def complete_validated(self, profile, prompt, schema=None, *,
                                 record_ids=(), batch_no=0):
        self.prompts.append(prompt)
        return await super().complete_validated(profile, prompt, schema,
                                                record_ids=record_ids, batch_no=batch_no)


def test_sequence_record_parts_pure_text_sections_and_step_lines():
    # §10.2 sequence sections in ONE text part, no image even in UI modality (S30);
    # step lines in the frozen §10.1 format; the fallback step (detail.kind ==
    # "extraction_invalid") carries the （摘取兜底） suffix while the LLM-confirmed
    # "other" does not (S16 separate listing).
    ep = make_ui_episode(5)
    parts = _record_parts(ep, "记录 A", 30000, SEQ_TRANSITIONS)
    assert [p.kind for p in parts] == ["text"]
    lines = parts[0].text.split("\n")
    assert lines[0] == "[记录 A·操作序列]"
    assert lines[1] == "[步骤序列]"
    assert tuple(lines[2:6]) == STEP_LINES
    assert lines[6] == "[成员帧摘要]"
    assert lines[7:] == [f"{m}. {frame_digest(member, 400)}"
                         for m, member in enumerate(ep.members, start=1)]


def test_sequence_record_parts_omit_steps_when_transitions_none():
    ep = make_ui_episode(3)
    parts = _record_parts(ep, "记录 B", 30000)
    assert [p.kind for p in parts] == ["text"]
    lines = parts[0].text.split("\n")
    assert lines[0] == "[记录 B·操作序列]"
    assert lines[1] == "[成员帧摘要]"          # [步骤序列] omitted ENTIRELY
    assert "[步骤序列]" not in parts[0].text


def test_member_digest_lines_bounded_with_middle_truncation():
    members = tuple(make_text_frame(f"f{i}", f"帧{i:02d}" + "内" * 60)
                    for i in range(10))
    full = _member_digest_lines(members, 100000)
    assert len(full) == 10
    assert full[0] == f"1. {members[0].text}"
    assert full[-1] == f"10. {members[9].text}"

    bounded = _member_digest_lines(members, 300)
    assert len("\n".join(bounded)) <= 300
    assert bounded[0] == full[0]               # first ALWAYS kept
    assert bounded[-1] == full[-1]             # last ALWAYS kept
    dropped = 10 - (len(bounded) - 2) - 1
    assert dropped >= 1
    assert bounded[-2] == f"…(truncated {dropped} members)"  # in-place middle marker
    assert bounded[:-2] == full[:len(bounded) - 2]           # kept prefix untruncated


async def test_pairwise_sequence_prompt_pure_text_and_transitions_threaded():
    # Stage-level: both envelopes' item.transitions reach the pairwise prompt (their
    # step lines appear under the respective [记录 X] slots) and the user message is
    # pure text — no image part despite UI modality.
    cfg = make_cfg(QualityConfig(mode="pairwise", rounds=1))
    other = (Transition(index=0, action={"action_type": "open_app", "target": None,
                                         "value": "打车", "description": "打开打车应用"},
                        model="m", attempts=1, detail={}),)
    batch = [PipelineItem(record=make_ui_episode(5, "ep0001"),
                          transitions=SEQ_TRANSITIONS),
             PipelineItem(record=make_ui_episode(3, "ep0002", "行程"),
                          transitions=other)]
    engine = CapturingStubEngine()
    await QualityStage(cfg).run(batch, make_ctx(cfg, engine=engine))
    (prompt,) = engine.prompts
    user = prompt.messages[1]
    assert all(p.kind == "text" for p in user.parts)
    joined = "\n".join(p.text for p in user.parts)
    assert "[记录 A·操作序列]" in joined and "[记录 B·操作序列]" in joined
    for line in STEP_LINES:
        assert line in joined
    assert "0. open_app（对象: —；值: 打车）打开打车应用" in joined
    assert all(it.scores[AGGREGATE_KEY].score is not None for it in batch)


def test_text_modality_sequence_skips_text_fast_path():
    # A text-modality episode must NOT take the "[记录 A] {record.text}" fast path
    # (record.text is None for sequences).
    ep_a = make_episode(tuple(make_text_frame(f"a{i}", f"甲步骤{i}") for i in range(3)),
                        "ep000a")
    ep_b = make_episode(tuple(make_text_frame(f"b{i}", f"乙步骤{i}") for i in range(2)),
                        "ep000b")
    bundle = _build_pairwise_prompt(ep_a, ep_b, (EDU,), with_reason=False,
                                    ui_tree_max_chars=30000,
                                    transitions_a=SEQ_TRANSITIONS, transitions_b=None)
    user = bundle.messages[1]
    assert [p.kind for p in user.parts] == ["text", "text"]
    assert user.parts[0].text.startswith("[记录 A·操作序列]\n[步骤序列]\n")
    assert user.parts[1].text.startswith("[记录 B·操作序列]\n[成员帧摘要]\n")
    assert "None" not in user.parts[0].text and "None" not in user.parts[1].text


async def test_pointwise_sequence_prompt_transitions_threaded():
    cfg = make_cfg(QualityConfig(mode="pointwise"))
    item = PipelineItem(record=make_ui_episode(4), transitions=SEQ_TRANSITIONS)
    engine = CapturingStubEngine(pointwise_scores={"ep0001": 4})
    await QualityStage(cfg).run([item], make_ctx(cfg, engine=engine))
    (prompt,) = engine.prompts
    user = prompt.messages[1]
    assert [p.kind for p in user.parts] == ["text"]
    lines = user.parts[0].text.split("\n")
    assert lines[0] == "[记录内容·操作序列]"
    assert lines[1] == "[步骤序列]"
    assert tuple(lines[2:6]) == STEP_LINES
    assert lines[6] == "[成员帧摘要]"
    assert item.scores[AGGREGATE_KEY].score == pytest.approx(0.8)


def test_excerpt_payload_sequence_branch_first_member_digest():
    trace = TraceConfig(enabled=True, channels=("quality",), content="excerpt")
    stage = QualityStage(make_cfg(QualityConfig(), trace=trace))
    long_first = make_text_frame("f0", "长" * 500)
    ep = make_episode((long_first, make_text_frame("f1", "短")), "ep0001")
    payload = stage._excerpt_payload((ep,))
    assert payload == {"ep0001": frame_digest(long_first, 400)[:200]}
    assert len(payload["ep0001"]) == 200

    ui_ep = make_ui_episode(2, "ep0002")
    assert stage._excerpt_payload((ui_ep,)) == {
        "ep0002": frame_digest(ui_ep.members[0], 400)[:200]}


def test_single_record_paths_unchanged_by_default_kwargs():
    # Regression anchor: the trailing transitions kwargs default to None and leave the
    # single-record prompts byte-identical; a single record IGNORES passed transitions.
    rec_a, rec_b = make_record("a1", "文本甲"), make_record("b1", "文本乙")
    assert _build_pairwise_prompt(rec_a, rec_b, (EDU,), True, 30000) == (
        _build_pairwise_prompt(rec_a, rec_b, (EDU,), True, 30000,
                               transitions_a=None, transitions_b=None))
    assert _build_pointwise_prompt(rec_a, EDU, 30000) == (
        _build_pointwise_prompt(rec_a, EDU, 30000, transitions=None))
    with_steps = _build_pointwise_prompt(rec_a, EDU, 30000, transitions=SEQ_TRANSITIONS)
    assert with_steps == _build_pointwise_prompt(rec_a, EDU, 30000)
    assert "[步骤序列]" not in with_steps.messages[1].parts[0].text

    ui_single = make_ui_frame("u1", "登录页")
    parts = _record_parts(ui_single, "记录 A", 30000, transitions=None)
    assert [p.kind for p in parts] == ["text", "image", "text"]
    assert parts[1].image is ui_single.image

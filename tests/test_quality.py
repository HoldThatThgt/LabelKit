"""Offline unit tests for M4 quality (pure logic only — no LLM, per project policy).

Covers: Bradley-Terry MM fit on synthetic win matrices, percentile normalization,
pairing determinism under a fixed seed, top_ratio arithmetic (ties, on_unscored),
weighted aggregation, gate behavior, batch-of-1, byte-exact prompt assembly, and
judging-call failure classification / data-free failure messages (spec 7.6 / 7.1).
"""
from __future__ import annotations

import math
import random

import numpy as np
import pytest

from labelkit.config.model import (
    AnnotateConfig,
    Criterion,
    DedupConfig,
    GenerateConfig,
    InputConfig,
    OutputConfig,
    QualityConfig,
    ResolvedConfig,
    Rubric,
    RunConfig,
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
    _pairing_plan,
    _percentile_scores,
    _pointwise_label,
    _top_ratio_selection,
    _violation_summary,
    _weighted_aggregate,
    fit_bradley_terry,
)
from labelkit.stage import RunContext
from labelkit.types import PipelineItem, QualityScore, Record, RecordRef


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
        dedup=DedupConfig(),
        quality=quality,
        generate=GenerateConfig(),
        annotate=AnnotateConfig(instruction="x"),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=trace or TraceConfig(),
        rubric=Rubric(name="test-rubric", criteria=criteria),
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
             seed: str = "0:1:quality") -> RunContext:
    return RunContext(cfg=cfg, llm=None, schema_engine=None,
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

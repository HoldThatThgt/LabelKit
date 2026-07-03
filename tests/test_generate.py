"""Offline unit tests for M6 generate — pure logic only (no LLM, per project policy)."""
import hashlib
import json
import random

import pytest

from labelkit.config.model import (
    AnnotateConfig,
    DedupConfig,
    GenerateConfig,
    GenerateStyle,
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
from labelkit.errors import ProviderRetryableError, SchemaViolation
from labelkit.generate import (
    CallPlan,
    SimilarityFilter,
    bucket_key,
    build_call_plans,
    canonical_json,
    make_generated_record,
    postprocess_samples,
    predraw_llm_style,
    render_prompt_texts,
    select_seeds,
    void_log_message,
)
from labelkit.types import PipelineItem, QualityScore, Record, RecordRef


# ── helpers ────────────────────────────────────────────────────────────────

class Recorder:
    """Duck-typed MetricsSink stand-in: counters + captured events (no LLM involved)."""

    def __init__(self):
        self.counters: dict[str, int] = {}
        self.events: list[dict] = []

    def count(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None) -> None:
        self.events.append({"ev": ev, "stage": stage, "batch_no": batch_no,
                            "record_ids": tuple(record_ids), "payload": payload or {}})


def mk_cfg(generate: GenerateConfig | None = None, quality: QualityConfig | None = None,
           dedup: DedupConfig | None = None, mode: str = "process",
           limit: int | None = None) -> ResolvedConfig:
    return ResolvedConfig(
        tool=ToolConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality="text", mode=mode),
        input=InputConfig(),
        dedup=dedup or DedupConfig(),
        quality=quality or QualityConfig(),
        generate=generate or GenerateConfig(enabled=True, instruction="gen"),
        annotate=AnnotateConfig(),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=TraceConfig(),
        rubric=Rubric(name="r", criteria=()),
        user_schema={"type": "object"},
        limit=limit,
        strict=False,
        dry_run=False,
        config_path="config.toml",
        project_path="project.toml",
        config_digest="sha256:0",
        project_digest="sha256:0",
    )


def mk_item(rec_id: str, text: str, aggregate: float | None,
            status: str = "active") -> PipelineItem:
    rec = Record(id=rec_id, modality="text", text=text, raw={"text": text},
                 ui_tree=None, image=None,
                 ref=RecordRef(source_file="a.jsonl", line_no=1, pair_index=None,
                               generated_from=(), generator=None))
    item = PipelineItem(record=rec, status=status)
    if aggregate is not None:
        item.scores["__aggregate__"] = QualityScore(
            criterion="__aggregate__", score=aggregate, mode="pairwise_bt", detail={})
    return item


# ── mixture pre-draw ───────────────────────────────────────────────────────

def test_round_robin_rotation():
    g = GenerateConfig(enabled=True, instruction="i", llms=("a", "b", "c"))
    pairs = predraw_llm_style(g, 7, random.Random("seed"))
    assert [llm for llm, _ in pairs] == ["a", "b", "c", "a", "b", "c", "a"]
    assert all(style is None for _, style in pairs)


def test_round_robin_deterministic_and_rng_independent():
    # No styles, round_robin: rotation must not depend on the RNG state at all.
    g = GenerateConfig(enabled=True, instruction="i", llms=("x", "y"))
    p1 = predraw_llm_style(g, 5, random.Random(1))
    p2 = predraw_llm_style(g, 5, random.Random(999))
    assert [l for l, _ in p1] == [l for l, _ in p2] == ["x", "y", "x", "y", "x"]


def test_weighted_sampling_seeded_deterministic():
    g = GenerateConfig(enabled=True, instruction="i", llms=("a", "b"),
                       mixture="weighted", weights=(1.0, 3.0))
    draws1 = [llm for llm, _ in predraw_llm_style(g, 50, random.Random("0:1:generate"))]
    draws2 = [llm for llm, _ in predraw_llm_style(g, 50, random.Random("0:1:generate"))]
    assert draws1 == draws2                      # same seed → identical plan
    # Matches a manual replication of the exact draw sequence.
    rng = random.Random("0:1:generate")
    expected = [rng.choices(["a", "b"], weights=[1.0, 3.0], k=1)[0] for _ in range(50)]
    assert draws1 == expected
    # Weight 3:1 → "b" must dominate.
    assert draws1.count("b") > draws1.count("a")


def test_style_drawn_uniformly_per_call_with_ctx_rng():
    styles = (GenerateStyle(name="s1", prompt="p1"), GenerateStyle(name="s2", prompt="p2"))
    g = GenerateConfig(enabled=True, instruction="i", llms=("a",), styles=styles)
    rng = random.Random(7)
    pairs = predraw_llm_style(g, 20, rng)
    expected_rng = random.Random(7)
    expected = [expected_rng.choice(styles).name for _ in range(20)]
    assert [s.name for _, s in pairs] == expected
    assert {s.name for _, s in pairs} == {"s1", "s2"}


def test_build_call_plans_seed_draws_without_replacement():
    g = GenerateConfig(enabled=True, instruction="i", llms=("a",), seeds_per_call=3)
    seeds = [(f"id{i}", f"seed text {i}") for i in range(5)]
    plans = build_call_plans(g, seeds, 4, random.Random(3))
    for plan in plans:
        assert len(plan.seed_ids) == 3
        assert len(set(plan.seed_ids)) == 3                    # without replacement
        assert set(plan.seed_ids) <= {f"id{i}" for i in range(5)}
        assert plan.seed_texts == tuple(f"seed text {i[2:]}" for i in
                                        (sid for sid in plan.seed_ids))


def test_build_call_plans_small_pool_takes_whole_pool():
    g = GenerateConfig(enabled=True, instruction="i", llms=("a",), seeds_per_call=3)
    seeds = [("id0", "t0"), ("id1", "t1")]
    plans = build_call_plans(g, seeds, 2, random.Random(0))
    for plan in plans:
        assert sorted(plan.seed_ids) == ["id0", "id1"]


def test_limit_truncation_keeps_full_predraw_prefix():
    # (llm, style) pre-draw covers ALL calls; --limit only truncates executed calls,
    # so the executed prefix must equal the untruncated plan's prefix.
    styles = (GenerateStyle(name="s1", prompt="p1"), GenerateStyle(name="s2", prompt="p2"))
    g = GenerateConfig(enabled=True, instruction="i", llms=("a", "b"), styles=styles)
    full = build_call_plans(g, [], 10, random.Random(42))
    truncated = build_call_plans(g, [], 10, random.Random(42), exec_calls=3)
    assert len(truncated) == 3
    assert [(p.llm, p.style_name) for p in truncated] == \
           [(p.llm, p.style_name) for p in full[:3]]


# ── prompt rendering (§10.4) ───────────────────────────────────────────────

def test_render_prompt_texts_with_style_and_seeds():
    system, user = render_prompt_texts("生成中文指令。", "务求简短。", 4, ["种子一", "种子二"])
    assert system == ("生成中文指令。\n"
                      "[风格要求] 务求简短。\n"
                      "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
                      '{"samples": [<新样本文本>, ...]}（恰 4 条）')
    assert user == ("[种子示例 1] 种子一\n"
                    "[种子示例 2] 种子二\n"
                    "请生成 4 条全新样本。")


def test_render_prompt_texts_seedless_no_style():
    system, user = render_prompt_texts("指令", None, 2, [])
    assert "[风格要求]" not in system
    assert user == "请生成 2 条全新样本。"


# ── similarity filter (real datasketch) ────────────────────────────────────

def test_similarity_filter_drops_near_identical_strings():
    filt = SimilarityFilter(threshold=0.85, num_perm=128, ngram=5)
    base = ("帮我写一条请假条，明天上午要去医院看牙医，下午两点左右回来继续上班，"
            "请领导批准一下这半天的事假，我会提前把今天手头的报表和会议纪要整理完，"
            "并把未完成的工作交接给同组的小王，确保项目进度不受影响，谢谢理解与支持")
    assert filt.probe_and_add(base) is True
    # Near-identical variant (single character changed in a long text) → filtered.
    near = base.replace("牙医", "牙科", 1)
    assert filt.probe_and_add(near) is False
    # Genuinely different text → kept.
    other = "把这段会议纪要翻译成英文并整理成要点列表发给项目组的同事们参考使用"
    assert filt.probe_and_add(other) is True


def test_similarity_filter_exact_duplicate_and_vs_seeds():
    filt = SimilarityFilter()
    seed = "写一份本周工作总结的周报模板，包含进展、风险与下周计划三个部分"
    filt.add(seed)                                   # seed text seeds the index
    assert filt.probe_and_add(seed) is False         # sample identical to a seed
    assert filt.probe_and_add(seed + " ") is False   # whitespace-normalized duplicate
    assert filt.probe_and_add("帮我预订明天下午三点飞往上海虹桥机场的航班机票") is True


# ── record construction ────────────────────────────────────────────────────

def test_make_generated_record_process_mode():
    rec = make_generated_record("帮我写一段道歉话术", "instruction",
                                ("1cda030abc565f17", "d5ad41d6357f8a55"),
                                "default", "concise")
    raw = {"instruction": "帮我写一段道歉话术"}
    expected_id = hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()[:16]
    assert rec.id == expected_id
    assert rec.modality == "text"
    assert rec.text == "帮我写一段道歉话术"
    assert rec.raw == raw
    assert rec.ui_tree is None and rec.image is None
    assert rec.ref.source_file == ""
    assert rec.ref.line_no is None and rec.ref.pair_index is None
    assert rec.ref.generated_from == ("1cda030abc565f17", "d5ad41d6357f8a55")
    assert rec.ref.generator == {"llm": "default", "style": "concise"}


def test_make_generated_record_generate_only_mode():
    rec = make_generated_record("样本", "text", (), "glm", None)
    assert rec.ref.generated_from == ()              # seeds are not records (v1.4)
    assert rec.ref.generator == {"llm": "glm", "style": None}


def test_canonical_json_matches_m2_rule():
    assert canonical_json({"b": 1, "a": "中"}) == '{"a":"中","b":1}'


# ── bucket accounting + events (pure postprocess) ──────────────────────────

def test_bucket_key_format():
    assert bucket_key("default", "concise") == "default×concise"
    assert bucket_key("glm", None) == "glm×null"


def _plan(i, llm, style, seed_ids=(), seed_texts=()):
    return CallPlan(index=i, llm=llm, style_name=style,
                    style_prompt=None if style is None else "p",
                    seed_ids=tuple(seed_ids), seed_texts=tuple(seed_texts))


def test_postprocess_bucket_accounting_and_voided_call():
    cfg = mk_cfg()
    metrics = Recorder()
    plans = [_plan(0, "a", "s1"), _plan(1, "b", None), _plan(2, "a", "s1")]
    results = [
        ["第一条全新的中文样本，讲的是给客户写道歉信的场景", "第二条样本，请帮忙翻译一段会议通知到英文"],
        None,                                        # voided call: calls=1, produced=0
        ["第三条样本，编一条晒周末爬山照片的朋友圈文案"],
    ]
    records = postprocess_samples(plans, results, [], cfg, metrics)
    assert len(records) == 3
    assert metrics.counters["generate.buckets.a×s1.calls"] == 2
    assert metrics.counters["generate.buckets.a×s1.produced"] == 3
    assert metrics.counters["generate.buckets.a×s1.survived_dedup"] == 3
    assert metrics.counters["generate.buckets.b×null.calls"] == 1
    assert "generate.buckets.b×null.produced" not in metrics.counters  # voided → no produced
    # counts.generated is owned by M10 (orchestrator); M6 must not touch counts.*
    assert "counts.generated" not in metrics.counters
    # M6 emits NO trace events: the §8.1 catalog defines none for generate, and a voided
    # call produces no StageError (spec 3.6.3) — bucket counters only.
    assert metrics.events == []


def test_postprocess_filters_vs_seeds_and_between_samples():
    cfg = mk_cfg()
    metrics = Recorder()
    seed = "帮我写一条请假条，明天上午要去医院看牙医，请领导批准"
    novel = "把这份季度销售数据整理成图表并写两句总结发给经理审阅"
    plans = [_plan(0, "a", None, seed_ids=("sid1",), seed_texts=(seed,))]
    results = [[seed, novel, novel + "。"]]          # dup-of-seed, novel, dup-of-sample
    records = postprocess_samples(plans, results, [seed], cfg, metrics)
    assert [r.text for r in records] == [novel]
    assert metrics.counters["generate.buckets.a×null.produced"] == 3
    assert metrics.counters["generate.buckets.a×null.survived_dedup"] == 1
    assert metrics.events == []                      # filter outcomes are counters, not events
    # process-mode provenance carried through
    assert records[0].ref.generated_from == ("sid1",)


# ── voided-call stderr message (value-free, CONTRACTS §8.4 / spec 3.6.3) ───

def test_void_log_message_schema_violation_carries_no_data_values():
    sample = "帮我写一段给客户的道歉话术，快递发错货了"
    exc = SchemaViolation(
        errors=[f"/samples: ['{sample}'] is too short",
                f"/samples/0: 实际值为 {sample}"],
        raw_last_output=json.dumps({"samples": [sample]}, ensure_ascii=False))
    plan = _plan(3, "default", "concise")
    msg = void_log_message(plan, exc)
    # structural fields only
    assert msg == "生成调用作废 call=3 llm=default style=concise kind=schema_violation violations=2"
    # NEVER the rendered violations / raw output (they embed generated sample text)
    assert sample not in msg
    assert str(exc) not in msg


def test_void_log_message_provider_error_kind_and_null_style():
    exc = ProviderRetryableError("429 rate limited", profile="glm", retries=3)
    msg = void_log_message(_plan(0, "glm", None), exc)
    assert msg == "生成调用作废 call=0 llm=glm style=null kind=provider_retryable_exhausted"


# ── seed selection (process mode) ──────────────────────────────────────────

def test_select_seeds_uses_quality_threshold_by_default():
    cfg = mk_cfg(quality=QualityConfig(threshold=0.5))
    batch = [mk_item("id1", "t1", 0.8), mk_item("id2", "t2", 0.4),
             mk_item("id3", "t3", 0.5), mk_item("id4", "t4", 0.9, status="dropped_lowq"),
             mk_item("id5", "t5", None)]             # unscored → never seeds
    assert select_seeds(batch, cfg) == [("id1", "t1"), ("id3", "t3")]


def test_select_seeds_explicit_seed_min_score_wins():
    cfg = mk_cfg(generate=GenerateConfig(enabled=True, instruction="i", seed_min_score=0.85),
                 quality=QualityConfig(threshold=0.5))
    batch = [mk_item("id1", "t1", 0.8), mk_item("id2", "t2", 0.9)]
    assert select_seeds(batch, cfg) == [("id2", "t2")]


def test_select_seeds_median_fallback_when_no_threshold():
    cfg = mk_cfg(quality=QualityConfig(threshold=None))
    batch = [mk_item("id1", "t1", 0.2), mk_item("id2", "t2", 0.6),
             mk_item("id3", "t3", 0.9)]
    # median = 0.6; keep >= median
    assert select_seeds(batch, cfg) == [("id2", "t2"), ("id3", "t3")]


def test_select_seeds_empty_when_nothing_scored():
    cfg = mk_cfg()
    batch = [mk_item("id1", "t1", None), mk_item("id2", "t2", 0.9, status="failed")]
    assert select_seeds(batch, cfg) == []


def test_postprocess_sample_validator_filters_and_counts():
    from dataclasses import replace
    cfg = mk_cfg()
    cfg = replace(cfg, generate=replace(
        cfg.generate, sample_validator="tests.hook_samples:sample_min10"))
    metrics = Recorder()
    plans = [_plan(0, "a", "s1")]
    results = [["太短", "这一条样本足够长可以通过回调校验器的长度要求"]]
    records = postprocess_samples(plans, results, [], cfg, metrics)
    assert len(records) == 1                                   # 短样本被回调剔除
    assert metrics.counters["generate.buckets.a×s1.rejected_by_validator"] == 1
    assert metrics.counters["generate.buckets.a×s1.produced"] == 2
    assert metrics.counters["generate.buckets.a×s1.survived_dedup"] == 1


def test_postprocess_sample_validator_zero_touch_and_exception(caplog):
    import logging
    from dataclasses import replace
    cfg = mk_cfg()
    cfg = replace(cfg, generate=replace(
        cfg.generate, sample_validator="tests.hook_samples:boom"))
    metrics = Recorder()
    plans = [_plan(0, "a", None)]
    results = [["这一条样本足够长但回调会爆炸把它当违规剔除"]]
    with caplog.at_level(logging.WARNING, logger="labelkit"):
        records = postprocess_samples(plans, results, [], cfg, metrics)
    assert records == []                                        # 异常 ⇒ 按违规剔除
    assert metrics.counters["generate.buckets.a×null.rejected_by_validator"] == 1
    assert sum("sample_validator 回调抛出异常" in r.message for r in caplog.records) == 1


def test_postprocess_without_validator_has_no_bucket_field():
    cfg = mk_cfg()
    metrics = Recorder()
    records = postprocess_samples([_plan(0, "a", None)],
                                  [["这一条样本足够长可以通过所有过滤器"]], [], cfg, metrics)
    assert len(records) == 1
    assert not any("rejected_by_validator" in k for k in metrics.counters)

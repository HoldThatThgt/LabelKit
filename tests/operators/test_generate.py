"""Offline unit tests for M6 generate — pure logic only (no LLM, per project policy).

v1.7 stage-level tests (per-class instruction/temperature, inherited Classification)
follow the test_annotate.py precedent: a duck-typed SchemaEngine stand-in + asyncio.run
— no mock LLM server/transport is involved.
"""
import asyncio
import hashlib
import json
import random
from types import SimpleNamespace

from labelkit.common.config.model import (
    AnnotateConfig,
    ClassSpec,
    ClassView,
    ClassifyConfig,
    ConsoleConfig,
    DedupConfig,
    ExtractConfig,
    GenerateConfig,
    GenerateStyle,
    InputConfig,
    OutputConfig,
    QualityConfig,
    ResolvedConfig,
    Rubric,
    RunConfig,
    SegmentConfig,
    StitchConfig,
    StreamConfig,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)
from labelkit.common.errors import ProviderRetryableError, SchemaViolation
from labelkit.operators.generate import (
    CallPlan,
    ClassSegment,
    GenerateStage,
    SimilarityFilter,
    bucket_key,
    build_call_plans,
    build_class_segments,
    build_segment_plans,
    canonical_json,
    effective_generate,
    make_generated_record,
    postprocess_samples,
    predraw_llm_style,
    render_prompt_texts,
    select_seeds,
    void_log_message,
)
from labelkit.common.contracts.types import Classification, PipelineItem, QualityScore, Record, RecordRef


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
           limit: int | None = None, classify: ClassifyConfig | None = None,
           class_views: dict[str, ClassView] | None = None) -> ResolvedConfig:
    return ResolvedConfig(
        tool=ToolConfig(),
        console=ConsoleConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality="text", mode=mode),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=dedup or DedupConfig(),
        segment=SegmentConfig(),
        stitch=StitchConfig(),
        extract=ExtractConfig(),
        classify=classify or ClassifyConfig(),
        quality=quality or QualityConfig(),
        generate=generate or GenerateConfig(enabled=True, instruction="gen"),
        annotate=AnnotateConfig(),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=TraceConfig(),
        rubric=Rubric(name="r", criteria=()),
        class_views=class_views or {},
        user_schema={"type": "object"},
        limit=limit,
        strict=False,
        dry_run=False,
        config_path="config.toml",
        project_path="project.toml",
        config_digest="sha256:0",
        project_digest="sha256:0",
    )


def mk_view(name: str, *, quality: QualityConfig | None = None,
            generate: GenerateConfig | None = None) -> ClassView:
    """Minimal effective view for direct-construction tests (loader not in play)."""
    return ClassView(
        name=name,
        quality=quality or QualityConfig(),
        rubric=Rubric(name="r", criteria=()),
        annotate=AnnotateConfig(),
        generate=generate or GenerateConfig(enabled=True, instruction="gen"),
        verify=VerifyConfig(),
        extract=ExtractConfig(),
    )


def mk_classify(*names: str, fallback: str = "other") -> ClassifyConfig:
    return ClassifyConfig(
        enabled=True, fallback_class=fallback,
        classes=tuple(ClassSpec(name=n, description=f"{n} 类") for n in names))


def mk_item(rec_id: str, text: str, aggregate: float | None,
            status: str = "active", label: str | None = None) -> PipelineItem:
    rec = Record(id=rec_id, modality="text", text=text, raw={"text": text},
                 ui_tree=None, image=None,
                 ref=RecordRef(source_file="a.jsonl", line_no=1, pair_index=None,
                               generated_from=(), generator=None))
    item = PipelineItem(record=rec, status=status)
    if label is not None:
        item.classification = Classification(label=label, labels=(label,),
                                             source="llm", detail={})
    if aggregate is not None:
        item.scores["__aggregate__"] = QualityScore(
            criterion="__aggregate__", score=aggregate, mode="pairwise_bt", detail={})
    return item


# Pairwise-distinct sentences so the MinHash filter never collides across calls.
DISTINCT_SAMPLES = [
    "帮我写一封给房东的续租申请邮件，语气礼貌一些",
    "把下周一的项目评审会议纪要提纲列出来",
    "翻译这句话到英文：合同今天下午必须寄出",
    "编一条给同事庆祝升职的祝福短信",
    "写一个周五团建活动的报名通知",
    "帮我拟一份笔记本电脑采购的申请理由",
    "把这段口语化的反馈改写成正式的客服回复",
    "给产品发布会写三条备选的宣传口号",
    "帮我列一个搬家前一周的待办清单",
    "写一段介绍公司年假制度的问答话术",
    "把季度销售亮点整理成三句话的汇报开场白",
    "帮我起草一条召集校友聚会的群公告",
    "写一封感谢面试官抽空面试的跟进邮件",
    "给新人入职第一天准备一份欢迎词",
    "把这份菜谱的步骤改写得更简洁易懂",
    "帮我写一条提醒大家更新密码的安全通知",
    "起草一段婉拒供应商报价的回复",
    "写一个亲子读书会的活动流程安排",
    "帮我把演讲稿的结尾改得更有感染力",
    "给客户写一条物流延误的致歉说明",
    "列出预订会议室时需要确认的五个事项",
    "帮我写一段介绍新功能上线的推送文案",
    "把这周的健身计划整理成表格说明",
    "写一条邀请邻居参加社区义卖的短信",
]


class SamplesEngine:
    """Duck-typed SchemaEngine stand-in (test_annotate.py precedent): serves distinct
    canned samples per call and captures (profile, system, user, temperature)."""

    def __init__(self, num_per_call: int):
        self.calls: list[tuple] = []
        self._n = num_per_call
        self._served = 0

    async def complete_validated(self, profile, prompt, schema=None, *,
                                 record_ids=(), batch_no=0, record=None):
        system = prompt.messages[0].parts[0].text
        user = prompt.messages[1].parts[0].text
        self.calls.append((profile, system, user, prompt.temperature))
        samples = DISTINCT_SAMPLES[self._served:self._served + self._n]
        self._served += self._n
        return {"samples": samples}, None, 1, "m"


def run_stage(cfg: ResolvedConfig, batch: list[PipelineItem], *, rng_seed="0:1:generate",
              num_per_call: int | None = None) -> tuple[list[PipelineItem], SamplesEngine, Recorder]:
    engine = SamplesEngine(num_per_call or cfg.generate.num_per_call)
    metrics = Recorder()
    ctx = SimpleNamespace(cfg=cfg, llm=None, schema_engine=engine, metrics=metrics,
                          rng=random.Random(rng_seed), batch_no=1)
    items = asyncio.run(GenerateStage(cfg).run(batch, ctx))
    return items, engine, metrics


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


# ── v1.7 class segments: budgets, order, global-index draws (R18) ──────────

def test_build_class_segments_budgets_and_lexicographic_order():
    g = GenerateConfig(enabled=True, instruction="全局", num_per_call=4)
    styles_w = (GenerateStyle(name="w1", prompt="wp1"),)
    cfg = mk_cfg(generate=g, classify=mk_classify("writing", "qa", "other"),
                 class_views={
                     "qa": mk_view("qa", generate=GenerateConfig(
                         enabled=True, instruction="问答", num_per_record=3)),
                     "writing": mk_view("writing", generate=GenerateConfig(
                         enabled=True, instruction="写作", styles=styles_w)),
                     "other": mk_view("other"),
                 })
    # insertion order deliberately non-lexicographic to prove the sort
    pools = {"writing": [(f"w{i}", f"wt{i}") for i in range(3)],
             "qa": [(f"q{i}", f"qt{i}") for i in range(5)]}
    segments = build_class_segments(pools, cfg)
    assert [s.class_name for s in segments] == ["qa", "writing"]
    # C_c = ceil(len(seeds_c) × num_per_record_c / num_per_call): qa 5×3/4→4, writing 3×2/4→2
    assert [s.num_calls for s in segments] == [4, 2]
    assert segments[0].seeds == tuple((f"q{i}", f"qt{i}") for i in range(5))
    assert segments[0].styles == ()                      # class-effective styles
    assert segments[1].styles == styles_w


def test_build_class_segments_anonymous_uses_global_section():
    g = GenerateConfig(enabled=True, instruction="全局", num_per_record=2, num_per_call=4,
                       styles=(GenerateStyle(name="s1", prompt="p1"),))
    cfg = mk_cfg(generate=g)                             # classify disabled, no views
    segments = build_class_segments({None: [("a", "ta"), ("b", "tb"), ("c", "tc")]}, cfg)
    assert len(segments) == 1
    seg = segments[0]
    assert seg.class_name is None
    assert seg.num_calls == 2                            # ceil(3×2/4)
    assert seg.styles == g.styles


def test_round_robin_uses_global_index_across_segments():
    g = GenerateConfig(enabled=True, instruction="i", llms=("x", "y"))
    segs = [ClassSegment("a", (("a1", "at"),), 3, ()),
            ClassSegment("b", (("b1", "bt"),), 2, ())]
    plans = build_segment_plans(g, segs, random.Random(0))
    assert [p.index for p in plans] == [0, 1, 2, 3, 4]   # consecutive global indexes
    assert [p.class_name for p in plans] == ["a", "a", "a", "b", "b"]
    # rotation keyed on the GLOBAL index — it crosses the segment boundary unbroken
    assert [p.llm for p in plans] == ["x", "y", "x", "y", "x"]


def test_weighted_consumes_one_choices_per_global_index_across_segments():
    styles_a = (GenerateStyle(name="s1", prompt="p1"), GenerateStyle(name="s2", prompt="p2"))
    g = GenerateConfig(enabled=True, instruction="i", llms=("a", "b"),
                       mixture="weighted", weights=(1.0, 3.0), seeds_per_call=2)
    seeds_alpha = (("a1", "at1"), ("a2", "at2"), ("a3", "at3"))
    seeds_beta = (("b1", "bt1"), ("b2", "bt2"))
    segs = [ClassSegment("alpha", seeds_alpha, 2, styles_a),
            ClassSegment("beta", seeds_beta, 2, ())]
    plans = build_segment_plans(g, segs, random.Random(11))
    # Manual replication of the exact consumption order: per index — one choices for
    # the llm, then one choice for the style IF the owning class has styles; the seed
    # draws all happen afterwards, in ascending global index order.
    ref = random.Random(11)
    expected_pairs = []
    for i in range(4):
        llm = ref.choices(["a", "b"], weights=[1.0, 3.0], k=1)[0]
        style = ref.choice(styles_a).name if i < 2 else None
        expected_pairs.append((llm, style))
    assert [(p.llm, p.style_name) for p in plans] == expected_pairs
    expected_draws = [tuple(ref.sample(list(seeds_alpha if i < 2 else seeds_beta), 2))
                      for i in range(4)]
    assert [tuple(zip(p.seed_ids, p.seed_texts)) for p in plans] == expected_draws


def test_style_drawn_from_owning_class_styles_and_reproducible():
    styles_w = (GenerateStyle(name="w1", prompt="wp1"), GenerateStyle(name="w2", prompt="wp2"))
    styles_z = (GenerateStyle(name="z1", prompt="zp1"),)
    g = GenerateConfig(enabled=True, instruction="i", llms=("only",))
    segs = [ClassSegment("qa", (("q1", "qt1"),), 2, ()),         # no styles ⇒ None, zero rng
            ClassSegment("writing", (("w1s", "wt1"),), 2, styles_w),
            ClassSegment("zeta", (("z1s", "zt1"),), 1, styles_z)]
    p1 = build_segment_plans(g, segs, random.Random(5))
    p2 = build_segment_plans(g, segs, random.Random(5))
    assert p1 == p2                                      # same seed → identical plan
    ref = random.Random(5)
    expected_styles = [None, None,
                       ref.choice(styles_w).name, ref.choice(styles_w).name,
                       ref.choice(styles_z).name]
    assert [p.style_name for p in p1] == expected_styles
    assert [p.class_name for p in p1] == ["qa", "qa", "writing", "writing", "zeta"]


def test_single_class_segment_plan_matches_anonymous_plan():
    # 单类退化：one participating class with the global styles draws the exact same
    # (llm, style, seeds) stream as the anonymous pre-v1.7 plan; only class_name differs.
    styles = (GenerateStyle(name="s1", prompt="p1"), GenerateStyle(name="s2", prompt="p2"))
    g = GenerateConfig(enabled=True, instruction="i", llms=("x", "y"), styles=styles)
    seeds = [(f"id{i}", f"t{i}") for i in range(5)]
    solo = build_segment_plans(g, [ClassSegment("solo", tuple(seeds), 4, styles)],
                               random.Random(9))
    anon = build_call_plans(g, seeds, 4, random.Random(9))

    def key(p):
        return (p.index, p.llm, p.style_name, p.style_prompt, p.seed_ids, p.seed_texts)

    assert [key(p) for p in solo] == [key(p) for p in anon]
    assert {p.class_name for p in solo} == {"solo"}
    assert {p.class_name for p in anon} == {None}


def test_effective_generate_resolution():
    view_gen = GenerateConfig(enabled=True, instruction="类指令", temperature=0.3)
    cfg = mk_cfg(classify=mk_classify("qa", "other"),
                 class_views={"qa": mk_view("qa", generate=view_gen),
                              "other": mk_view("other")})
    assert effective_generate(cfg, None) is cfg.generate
    assert effective_generate(cfg, "qa") is view_gen


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


def test_bucket_key_class_prefix_only_with_class():
    # v1.7: three segments only for class-owned calls; None keeps the two-segment
    # form byte-identical (CONTRACTS §7.5).
    assert bucket_key("default", "concise", "qa") == "qa×default×concise"
    assert bucket_key("glm", None, "writing") == "writing×glm×null"
    assert bucket_key("glm", None, None) == "glm×null"


def _plan(i, llm, style, seed_ids=(), seed_texts=(), class_name=None):
    return CallPlan(index=i, llm=llm, style_name=style,
                    style_prompt=None if style is None else "p",
                    seed_ids=tuple(seed_ids), seed_texts=tuple(seed_texts),
                    class_name=class_name)


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
    assert all(cls is None for _, cls in records)    # anonymous plans carry no class
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
    assert [r.text for r, _ in records] == [novel]
    assert metrics.counters["generate.buckets.a×null.produced"] == 3
    assert metrics.counters["generate.buckets.a×null.survived_dedup"] == 1
    assert metrics.events == []                      # filter outcomes are counters, not events
    # process-mode provenance carried through
    assert records[0][0].ref.generated_from == ("sid1",)


def test_postprocess_class_bucket_keys_and_class_attribution():
    # v1.7 (R17): the bucket key gains the class prefix for class-owned plans, and each
    # produced record carries the producing plan's class.
    cfg = mk_cfg()
    metrics = Recorder()
    plans = [_plan(0, "a", "s1", class_name="qa"),
             _plan(1, "b", None, class_name="writing"),
             _plan(2, "a", None)]                    # anonymous plan in the same pass
    results = [[DISTINCT_SAMPLES[0], DISTINCT_SAMPLES[1]],
               [DISTINCT_SAMPLES[2]],
               [DISTINCT_SAMPLES[3]]]
    records = postprocess_samples(plans, results, [], cfg, metrics)
    assert [cls for _, cls in records] == ["qa", "qa", "writing", None]
    assert metrics.counters["generate.buckets.qa×a×s1.calls"] == 1
    assert metrics.counters["generate.buckets.qa×a×s1.produced"] == 2
    assert metrics.counters["generate.buckets.qa×a×s1.survived_dedup"] == 2
    assert metrics.counters["generate.buckets.writing×b×null.calls"] == 1
    assert metrics.counters["generate.buckets.a×null.calls"] == 1   # two-segment untouched
    assert not any(k.startswith("generate.buckets.None") for k in metrics.counters)


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
    assert select_seeds(batch, cfg) == {None: [("id1", "t1"), ("id3", "t3")]}


def test_select_seeds_explicit_seed_min_score_wins():
    cfg = mk_cfg(generate=GenerateConfig(enabled=True, instruction="i", seed_min_score=0.85),
                 quality=QualityConfig(threshold=0.5))
    batch = [mk_item("id1", "t1", 0.8), mk_item("id2", "t2", 0.9)]
    assert select_seeds(batch, cfg) == {None: [("id2", "t2")]}


def test_select_seeds_median_fallback_when_no_threshold():
    cfg = mk_cfg(quality=QualityConfig(threshold=None))
    batch = [mk_item("id1", "t1", 0.2), mk_item("id2", "t2", 0.6),
             mk_item("id3", "t3", 0.9)]
    # median = 0.6; keep >= median
    assert select_seeds(batch, cfg) == {None: [("id2", "t2"), ("id3", "t3")]}


def test_select_seeds_empty_when_nothing_scored():
    cfg = mk_cfg()
    batch = [mk_item("id1", "t1", None), mk_item("id2", "t2", 0.9, status="failed")]
    assert select_seeds(batch, cfg) == {}


# ── v1.7 seed selection: per-class grouping + threshold chain (R19) ────────

def test_select_seeds_groups_by_label_sorted_keeping_batch_order():
    # Views mirror loader inheritance of the global quality.threshold = 0.5.
    inherited = QualityConfig(threshold=0.5)
    cfg = mk_cfg(quality=QualityConfig(threshold=0.5),
                 classify=mk_classify("w", "q", "other"),
                 class_views={"w": mk_view("w", quality=inherited),
                              "q": mk_view("q", quality=inherited),
                              "other": mk_view("other", quality=inherited)})
    batch = [mk_item("id1", "t1", 0.8, label="w"), mk_item("id2", "t2", 0.9, label="q"),
             mk_item("id3", "t3", 0.7, label="w"), mk_item("id4", "t4", 0.6, label="q")]
    pools = select_seeds(batch, cfg)
    assert list(pools) == ["q", "w"]                 # lexicographic key order
    assert pools["q"] == [("id2", "t2"), ("id4", "t4")]   # batch order within a group
    assert pools["w"] == [("id1", "t1"), ("id3", "t3")]


def test_select_seeds_global_seed_min_score_wins_over_class_threshold():
    cfg = mk_cfg(generate=GenerateConfig(enabled=True, instruction="i", seed_min_score=0.85),
                 classify=mk_classify("a", "b", "other"),
                 class_views={"a": mk_view("a", quality=QualityConfig(threshold=0.5)),
                              "b": mk_view("b"), "other": mk_view("other")})
    batch = [mk_item("a1", "at1", 0.8, label="a"), mk_item("a2", "at2", 0.9, label="a"),
             mk_item("b1", "bt1", 0.84, label="b"), mk_item("b2", "bt2", 0.86, label="b")]
    assert select_seeds(batch, cfg) == {"a": [("a2", "at2")], "b": [("b2", "bt2")]}


def test_select_seeds_class_effective_threshold_then_group_median():
    # a has an effective class threshold; b has none anywhere → falls to ITS OWN
    # group median (not the batch-wide one).
    cfg = mk_cfg(quality=QualityConfig(threshold=None),
                 classify=mk_classify("a", "b", "other"),
                 class_views={"a": mk_view("a", quality=QualityConfig(threshold=0.7)),
                              "b": mk_view("b", quality=QualityConfig(threshold=None)),
                              "other": mk_view("other")})
    batch = [mk_item("a1", "at1", 0.6, label="a"), mk_item("a2", "at2", 0.8, label="a"),
             mk_item("b1", "bt1", 0.2, label="b"), mk_item("b2", "bt2", 0.6, label="b"),
             mk_item("b3", "bt3", 0.9, label="b")]
    # b's median = 0.6 (batch-wide median would be 0.6 too — so differentiate: a's items
    # must not shift b's cut, checked via a second config below)
    assert select_seeds(batch, cfg) == {"a": [("a2", "at2")],
                                        "b": [("b2", "bt2"), ("b3", "bt3")]}


def test_select_seeds_group_median_is_per_group_not_batch_wide():
    cfg = mk_cfg(quality=QualityConfig(threshold=None),
                 classify=mk_classify("a", "b", "other"),
                 class_views={"a": mk_view("a"), "b": mk_view("b"),
                              "other": mk_view("other")})
    batch = [mk_item("a1", "at1", 0.1, label="a"), mk_item("a2", "at2", 0.2, label="a"),
             mk_item("a3", "at3", 0.3, label="a"),
             mk_item("b1", "bt1", 0.7, label="b"), mk_item("b2", "bt2", 0.8, label="b")]
    # a's median = 0.2 → a2, a3 pass; b's median = 0.75 → only b2 passes.
    # (a batch-wide median of 0.3 would instead drop a2 and keep b1.)
    assert select_seeds(batch, cfg) == {"a": [("a2", "at2"), ("a3", "at3")],
                                        "b": [("b2", "bt2")]}


def test_select_seeds_omits_groups_where_nothing_passes():
    cfg = mk_cfg(generate=GenerateConfig(enabled=True, instruction="i", seed_min_score=0.95),
                 classify=mk_classify("a", "b", "other"),
                 class_views={"a": mk_view("a"), "b": mk_view("b"),
                              "other": mk_view("other")})
    batch = [mk_item("a1", "at1", 0.5, label="a"), mk_item("b1", "bt1", 0.96, label="b")]
    assert select_seeds(batch, cfg) == {"b": [("b1", "bt1")]}


def test_select_seeds_ignores_labels_when_classify_disabled():
    # Zero-change gate: grouping keys on cfg.classify.enabled, not on stray labels.
    cfg = mk_cfg(quality=QualityConfig(threshold=0.5))
    batch = [mk_item("id1", "t1", 0.8, label="w"), mk_item("id2", "t2", 0.9, label="q")]
    assert select_seeds(batch, cfg) == {None: [("id1", "t1"), ("id2", "t2")]}


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


# ── v1.7 stage level: class-effective calls, inheritance, degradation ──────

def test_run_per_class_instruction_temperature_and_inherited_classification():
    styles_w = (GenerateStyle(name="w1", prompt="写作风格一"),
                GenerateStyle(name="w2", prompt="写作风格二"))
    g = GenerateConfig(enabled=True, instruction="全局生成指令", llms=("x", "y"),
                       temperature=0.9)
    qa_gen = GenerateConfig(enabled=True, instruction="问答类生成指令", temperature=0.7)
    writing_gen = GenerateConfig(enabled=True, instruction="写作类生成指令",
                                 temperature=0.3, styles=styles_w)
    inherited = QualityConfig(threshold=0.5)      # loader inherits the global threshold
    cfg = mk_cfg(generate=g, quality=QualityConfig(threshold=0.5),
                 classify=mk_classify("writing", "qa", "other"),
                 class_views={"qa": mk_view("qa", generate=qa_gen, quality=inherited),
                              "writing": mk_view("writing", generate=writing_gen,
                                                 quality=inherited),
                              "other": mk_view("other", quality=inherited)})
    batch = [mk_item("q1", "问答种子一", 0.8, label="qa"),
             mk_item("w1", "写作种子一", 0.9, label="writing"),
             mk_item("q2", "问答种子二", 0.7, label="qa"),
             mk_item("w2", "写作种子二", 0.6, label="writing"),
             mk_item("w3", "写作种子三", 0.7, label="writing")]
    items, engine, metrics = run_stage(cfg, batch, rng_seed=42)

    # segment budgets: qa ceil(2×2/4)=1, writing ceil(3×2/4)=2; lexicographic → qa first
    assert len(engine.calls) == 3
    # round_robin over GLOBAL indexes crosses the qa→writing boundary: x, y, x
    assert [c[0] for c in engine.calls] == ["x", "y", "x"]
    # one_call takes the CLASS-effective instruction/temperature (R17)
    assert engine.calls[0][1].startswith("问答类生成指令\n")
    assert engine.calls[0][3] == 0.7
    for _, system, _, temp in engine.calls[1:]:
        assert system.startswith("写作类生成指令\n")
        assert "[风格要求] 写作风格" in system        # style drawn from writing's own styles
        assert temp == 0.3
    assert "[风格要求]" not in engine.calls[0][1]     # qa has no effective styles

    # the exact plan is reproducible from the same rng seed via the public planner
    expected_plans = build_segment_plans(
        g, build_class_segments(select_seeds(batch, cfg), cfg), random.Random(42))
    assert [p.class_name for p in expected_plans] == ["qa", "writing", "writing"]
    expected_keys = {f"generate.buckets.{bucket_key(p.llm, p.style_name, p.class_name)}.calls"
                     for p in expected_plans}
    assert expected_keys <= set(metrics.counters)
    assert all(k.split(".")[2].count("×") == 2       # three-segment bucket keys
               for k in metrics.counters if k.startswith("generate.buckets."))

    # inherited Classification on every new item; seeds drawn from the owning segment
    assert len(items) == 12                          # 3 calls × 4 samples, all distinct
    qa_ids, writing_ids = {"q1", "q2"}, {"w1", "w2", "w3"}
    for item in items:
        cls = item.classification
        assert cls is not None and cls.source == "inherited"
        assert cls.labels == (cls.label,) and cls.detail == {}
        pool = qa_ids if cls.label == "qa" else writing_ids
        assert cls.label in ("qa", "writing")
        assert set(item.record.ref.generated_from) <= pool
    assert [it.classification.label for it in items] == ["qa"] * 4 + ["writing"] * 8


def test_run_disabled_matches_pre_v17_flow_exactly():
    # 关闭退化（硬要求）：classify off ⇒ single anonymous segment; the rng consumption,
    # prompts, temperature and outputs are identical to the pre-v1.7 flat flow, which is
    # replicated here via build_call_plans (whose draw stream the mixture/style/seed
    # tests above lock against manual rng replication).
    styles = (GenerateStyle(name="s1", prompt="p1"), GenerateStyle(name="s2", prompt="p2"))
    g = GenerateConfig(enabled=True, instruction="全局生成指令", llms=("x", "y"),
                       styles=styles, temperature=0.4)
    cfg = mk_cfg(generate=g, quality=QualityConfig(threshold=0.5))
    batch = [mk_item("id1", "种子文本甲", 0.8), mk_item("id2", "种子文本乙", 0.9),
             mk_item("id3", "种子文本丙", 0.6), mk_item("id4", "种子文本丁", 0.4)]
    items, engine, metrics = run_stage(cfg, batch, rng_seed="0:1:generate")

    seeds = [("id1", "种子文本甲"), ("id2", "种子文本乙"), ("id3", "种子文本丙")]
    ref_plans = build_call_plans(g, seeds, 2, random.Random("0:1:generate"))  # ceil(3×2/4)=2
    expected_calls = []
    for p in ref_plans:
        system, user = render_prompt_texts(g.instruction, p.style_prompt, 4, p.seed_texts)
        expected_calls.append((p.llm, system, user, g.temperature))
    assert engine.calls == expected_calls
    assert [it.record.text for it in items] == DISTINCT_SAMPLES[:8]
    assert all(it.classification is None for it in items)
    for key in metrics.counters:
        if key.startswith("generate.buckets."):
            assert key.split(".")[2].count("×") == 1  # two-segment keys, byte-identical


def test_run_single_class_matches_disabled_run_stream():
    # 单类退化：one participating class with zero generate overrides consumes the rng
    # and builds prompts exactly like the disabled run; only classification and the
    # bucket-key prefix differ.
    styles = (GenerateStyle(name="s1", prompt="p1"), GenerateStyle(name="s2", prompt="p2"))
    g = GenerateConfig(enabled=True, instruction="全局生成指令", llms=("x", "y"),
                       styles=styles, temperature=0.4)
    base = dict(generate=g, quality=QualityConfig(threshold=0.5))
    inherited = QualityConfig(threshold=0.5)      # loader inherits the global threshold
    cfg_off = mk_cfg(**base)
    cfg_on = mk_cfg(**base, classify=mk_classify("solo", "other"),
                    class_views={"solo": mk_view("solo", generate=g, quality=inherited),
                                 "other": mk_view("other", generate=g, quality=inherited)})
    def make_batch(label):
        return [mk_item("id1", "种子文本甲", 0.8, label=label),
                mk_item("id2", "种子文本乙", 0.9, label=label),
                mk_item("id3", "种子文本丙", 0.6, label=label)]

    items_off, engine_off, metrics_off = run_stage(cfg_off, make_batch(None), rng_seed=7)
    items_on, engine_on, metrics_on = run_stage(cfg_on, make_batch("solo"), rng_seed=7)

    assert engine_on.calls == engine_off.calls        # identical prompts + temperature
    assert [it.record.text for it in items_on] == [it.record.text for it in items_off]
    assert [it.record.ref.generated_from for it in items_on] == \
           [it.record.ref.generated_from for it in items_off]
    assert all(it.classification == Classification("solo", ("solo",), "inherited", {})
               for it in items_on)
    assert set(metrics_on.counters) == {f"generate.buckets.solo×{k.split('.', 2)[2]}"
                                        if k.startswith("generate.buckets.") else k
                                        for k in metrics_off.counters}


def test_generate_all_flat_path_ignores_classes():
    # generate_only stays flat (spec 3.6.2): global instruction, no labels, two-segment
    # bucket keys — even with classify enabled and class views present.
    g = GenerateConfig(enabled=True, instruction="全局生成指令", temperature=0.6,
                       seed_examples=("种子例一", "种子例二", "种子例三"))
    cfg = mk_cfg(generate=g, mode="generate_only",
                 classify=mk_classify("qa", "other"),
                 class_views={"qa": mk_view("qa", generate=GenerateConfig(
                                  enabled=True, instruction="问答类生成指令")),
                              "other": mk_view("other")})
    engine = SamplesEngine(g.num_per_call)
    metrics = Recorder()
    ctx = SimpleNamespace(cfg=cfg, llm=None, schema_engine=engine, metrics=metrics,
                          rng=random.Random("0:0:generate"), batch_no=0)
    records = asyncio.run(GenerateStage(cfg).generate_all(ctx))

    assert len(engine.calls) == 2                     # ceil(3×2/4)
    for _, system, _, temp in engine.calls:
        assert system.startswith("全局生成指令\n")     # never the per-class instruction
        assert temp == 0.6
    assert all(isinstance(rec, Record) for rec in records)   # bare Records, no class
    assert [rec.text for rec in records] == DISTINCT_SAMPLES[:8]
    assert all(rec.ref.generated_from == () for rec in records)
    for key in metrics.counters:
        if key.startswith("generate.buckets."):
            assert key.split(".")[2].count("×") == 1  # two-segment keys


# ── v1.11 seed packing (spec 3.6.2 上下文预算装填 row / §3.3⑦, V27①) ─────────

def _budget_profile(context_window: int, name: str = "default"):
    from labelkit.common.config.model import LLMProfile
    return LLMProfile(name=name, provider="openai_compatible", base_url="http://x",
                      model="m", api_key_env="K", max_output_tokens=256,
                      context_window=context_window)


def _budget_cfg(context_window: int, generate: GenerateConfig | None = None,
                **kw) -> ResolvedConfig:
    import dataclasses
    cfg = mk_cfg(generate=generate, **kw)
    return dataclasses.replace(
        cfg, llm_profiles={"default": _budget_profile(context_window)})


LONG_SEEDS = [(f"id{i}", f"种子{i}" + "字" * 100) for i in range(4)]


def _seed_plan(seeds=None, llm="default") -> CallPlan:
    seeds = LONG_SEEDS if seeds is None else seeds
    return CallPlan(index=0, llm=llm, style_name=None, style_prompt=None,
                    seed_ids=tuple(sid for sid, _ in seeds),
                    seed_texts=tuple(text for _, text in seeds))


def test_fit_plan_seeds_tail_drop_deterministic():
    from labelkit.common.runtime import budget as budget_mod
    from labelkit.operators.generate import _fit_plan_seeds

    cfg = _budget_cfg(700)
    fitted, truncated, unfittable = _fit_plan_seeds(_seed_plan(), cfg)
    assert not unfittable and truncated
    kept = len(fitted.seed_texts)
    assert 1 <= kept < 4
    # tail drop: the kept seeds are exactly the PREFIX of the drawn order,
    # ids stay positionally aligned with texts
    assert fitted.seed_texts == _seed_plan().seed_texts[:kept]
    assert fitted.seed_ids == _seed_plan().seed_ids[:kept]
    # the fitted prompt honours the budget; one more seed would not
    g, prof = cfg.generate, cfg.llm_profiles["default"]

    def est_for(texts):
        s, u = render_prompt_texts(g.instruction, None, g.num_per_call, texts)
        return (budget_mod.est_text(s) + budget_mod.est_text(u)
                + 2 * budget_mod.MSG_OVERHEAD_TOKENS)

    assert est_for(fitted.seed_texts) <= budget_mod.input_budget(prof)
    assert est_for(_seed_plan().seed_texts[:kept + 1]) > budget_mod.input_budget(prof)
    # deterministic: same inputs → identical trim
    again, _, _ = _fit_plan_seeds(_seed_plan(), cfg)
    assert again == fitted


def test_fit_plan_seeds_budget_off_and_fitting_identity():
    from labelkit.operators.generate import _fit_plan_seeds

    # cw == 0 → byte-identical pass-through (dead code anchor)
    plan = _seed_plan()
    assert _fit_plan_seeds(plan, _budget_cfg(0)) == (plan, False, False)
    # unknown profile (llms mixture pointing elsewhere) → pass-through
    assert _fit_plan_seeds(_seed_plan(llm="other"), _budget_cfg(700)) == (
        _seed_plan(llm="other"), False, False)
    # roomy window → untouched plan, no truncation
    assert _fit_plan_seeds(plan, _budget_cfg(100_000)) == (plan, False, False)


def test_fit_plan_seeds_unfittable_at_one_seed():
    from labelkit.operators.generate import _fit_plan_seeds

    # cw=530 → input_budget = 530 − 256 − 256 = 18: not even one 100-char seed fits
    fitted, truncated, unfittable = _fit_plan_seeds(_seed_plan(), _budget_cfg(530))
    assert unfittable and not truncated
    assert fitted == _seed_plan()                       # plan untouched (call voided whole)


def test_stage_trims_seeds_and_counts_truncations():
    # Stage-level: the dispatched prompt carries only the fitted seed prefix and
    # the produced records inherit the TRIMMED provenance (generated_from).
    g = GenerateConfig(enabled=True, instruction="gen", seeds_per_call=4,
                       num_per_record=4, num_per_call=4)
    cfg = _budget_cfg(700, generate=g, quality=QualityConfig(threshold=0.5))
    batch = [mk_item(f"id{i}", f"种子{i}" + "字" * 100, 0.9) for i in range(4)]
    items, engine, metrics = run_stage(cfg, batch)
    assert metrics.counters["budget.truncations.generate"] >= 1
    for _, _, user, _ in engine.calls:
        n_seed_lines = sum(1 for line in user.split("\n")
                           if line.startswith("[种子示例 "))
        assert 1 <= n_seed_lines < 4                # tail-dropped below the cap
    for item in items:
        assert 1 <= len(item.record.ref.generated_from) < 4


def test_stage_unfittable_call_voided_with_context_overflow_kind(caplog):
    import logging

    g = GenerateConfig(enabled=True, instruction="gen", seeds_per_call=4,
                       num_per_record=4, num_per_call=4)
    cfg = _budget_cfg(530, generate=g, quality=QualityConfig(threshold=0.5))
    batch = [mk_item(f"id{i}", f"种子{i}" + "字" * 100, 0.9) for i in range(4)]
    with caplog.at_level(logging.WARNING, logger="labelkit.generate"):
        items, engine, metrics = run_stage(cfg, batch)
    assert items == []                             # every call voided, no records
    assert engine.calls == []                      # doomed requests never sent
    # existing void semantics: bucket calls counted, produced 0, precise kind
    assert metrics.counters["generate.buckets.default×null.calls"] == 4
    assert "generate.buckets.default×null.produced" not in metrics.counters
    assert "budget.truncations.generate" not in metrics.counters
    assert any("kind=context_overflow" in r.message for r in caplog.records)


def test_error_kind_routes_budget_vocabulary_first():
    from labelkit.common.errors import ContextOverflowError, OutputTruncatedError
    from labelkit.operators.generate import _error_kind

    assert _error_kind(ContextOverflowError("x", phase="reactive")) == "context_overflow"
    assert _error_kind(OutputTruncatedError("x")) == "output_truncated"
    assert _error_kind(SchemaViolation(["v"], "raw")) == "schema_violation"
    # the void stderr line carries the precise kind (V27①)
    msg = void_log_message(_seed_plan(), ContextOverflowError("x", phase="precheck"))
    assert "kind=context_overflow" in msg

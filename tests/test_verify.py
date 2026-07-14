"""Offline unit tests for M7 verify: pure logic only (no LLM, no mocks of LLM).

Covers prompt-text assembly, majority vote, critique rendering, the policy state
machine on synthetic verdict sequences, rounds accounting, error classification,
stage item-selection behavior and verify.verdict trace-event payload tier gating.

v1.8 stream branch (S7/S8/S31): the sequence-variant review prompt (six-section
order, boundary-margin fate states, [动作序列] omission), defect-table collection
and normalization, and the two-phase batch-level member surgery — driven through
in-process complete_validated stubs (test_segment 惯例) with
``segment.judge_window`` / ``extract.extract_transition`` /
``annotate.annotate_record`` monkeypatched at their direct-call surfaces.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from labelkit.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ClassSpec,
    ClassView,
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
from labelkit.errors import (
    ProviderFatalError,
    ProviderRetryableError,
    SchemaViolation,
)
from labelkit.schema_engine import VERDICT_SCHEMA, defect_verdict_schema
from labelkit.types import (
    Annotation,
    Classification,
    ImageRef,
    PipelineItem,
    Record,
    RecordRef,
    Transition,
    UINode,
    UITree,
    Usage,
    VerificationResult,
    frame_digest,
)
from labelkit.verify import (
    _DEFAULT_FAIL_DEFECT,
    VerifyStage,
    _classify_error,
    boundary_margin_text,
    build_verify_prompt,
    majority_verdict,
    normalize_defects,
    render_critiques_text,
    run_verify_loop,
    sequence_step_line,
    verify_sequence_system_text,
    verify_system_text,
    verify_user_text,
)


def _annotation(output=None, model="m", attempts=1) -> Annotation:
    return Annotation(output=output or {"intent": "x"}, model=model, attempts=attempts,
                      usage=Usage(10, 5))


def _record(rec_id="a" * 16, text="hello") -> Record:
    return Record(
        id=rec_id, modality="text", text=text, raw={"text": text}, ui_tree=None,
        image=None,
        ref=RecordRef(source_file="f.jsonl", line_no=1, pair_index=None, generated_from=()),
    )


# ── prompt text ─────────────────────────────────────────────────────────────

def test_system_text_without_extra_criteria():
    assert verify_system_text("") == (
        "你是标注质量审核员。给定任务指令、原始数据与标注结果，独立判断标注是否合格。\n"
        "评审维度: ① 是否遵循任务指令 ② 与原始数据的事实一致性 ③ 字段语义是否正确填写\n"
        "先逐维度给出简短意见，再给结论。"
    )


def test_system_text_with_extra_criteria():
    assert verify_system_text("④ 语言风格是否得体") == (
        "你是标注质量审核员。给定任务指令、原始数据与标注结果，独立判断标注是否合格。\n"
        "评审维度: ① 是否遵循任务指令 ② 与原始数据的事实一致性 ③ 字段语义是否正确填写\n"
        "④ 语言风格是否得体\n"
        "先逐维度给出简短意见，再给结论。"
    )


def test_user_text_assembly():
    text = verify_user_text("给指令分类。", "帮我写请假条", {"intent": "写作", "difficulty": "easy"})
    assert text == (
        "[任务指令] 给指令分类。\n"
        "[原始数据] 帮我写请假条\n"
        '[标注结果] {"intent": "写作", "difficulty": "easy"}'
    )


# ── majority vote ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "verdicts, expected",
    [
        (["pass"], "pass"),
        (["fail"], "fail"),
        (["pass", "pass", "fail"], "pass"),
        (["pass", "fail", "fail"], "fail"),
        (["fail", "fail", "fail"], "fail"),
        (["pass", "pass", "pass", "fail", "fail"], "pass"),
        (["pass", "fail", "fail", "fail", "pass"], "fail"),
    ],
)
def test_majority_verdict(verdicts, expected):
    assert majority_verdict(verdicts) == expected


# ── critique rendering ──────────────────────────────────────────────────────

def test_render_critiques_single_judge():
    text = render_critiques_text(
        [
            {"aspect": "字段语义", "opinion": "difficulty 应为 medium"},
            {"aspect": "事实一致性", "opinion": "topic 与原文相符"},
        ]
    )
    assert text == "字段语义: difficulty 应为 medium\n事实一致性: topic 与原文相符"


def test_render_critiques_multi_judge_prefix():
    text = render_critiques_text(
        [
            {"aspect": "字段语义", "opinion": "有误", "judge": "judge_a"},
            {"aspect": "指令遵循", "opinion": "偏离", "judge": "judge_b"},
        ]
    )
    assert text == "judge_a/字段语义: 有误\njudge_b/指令遵循: 偏离"


# ── policy state machine on synthetic verdict sequences ─────────────────────

def _scripted_judge(script):
    """script: list of (verdict, critiques) per round; fail critiques = all critiques."""
    calls = []

    async def judge(annotation, round_no):
        calls.append((annotation, round_no))
        verdict, critiques = script[round_no - 1]
        fails = critiques if verdict == "fail" else []
        return verdict, list(critiques), list(fails)

    judge.calls = calls
    return judge


def _scripted_repair(new_annotations):
    calls = []
    it = iter(new_annotations)

    async def repair(annotation, fail_critiques):
        calls.append((annotation, fail_critiques))
        return next(it)

    repair.calls = calls
    return repair


C1 = {"aspect": "字段语义", "opinion": "difficulty 应为 medium"}
C2 = {"aspect": "字段语义", "opinion": "修正正确"}


def test_pass_on_first_round():
    ann = _annotation()
    judge = _scripted_judge([("pass", [C2])])
    repair = _scripted_repair([])
    verdict, rounds, critiques, final = asyncio.run(
        run_verify_loop(ann, judge, repair, policy="repair", max_repair_rounds=1)
    )
    assert (verdict, rounds) == ("pass", 1)
    assert critiques == [C2]
    assert final is ann
    assert repair.calls == []


def test_fail_then_repair_then_pass():
    ann0, ann1 = _annotation({"d": "easy"}), _annotation({"d": "medium"})
    judge = _scripted_judge([("fail", [C1]), ("pass", [C2])])
    repair = _scripted_repair([ann1])
    verdict, rounds, critiques, final = asyncio.run(
        run_verify_loop(ann0, judge, repair, policy="repair", max_repair_rounds=1)
    )
    assert (verdict, rounds) == ("pass", 2)
    assert critiques == [C1, C2]          # accumulated in round order
    assert final is ann1                  # repaired annotation replaces the original
    # repair got the previous annotation and the failing critiques
    assert repair.calls == [(ann0, [C1])]
    # round 2 judged the repaired annotation
    assert judge.calls == [(ann0, 1), (ann1, 2)]


def test_fail_all_rounds_exhausts_repair_budget():
    ann0, ann1 = _annotation({"d": "easy"}), _annotation({"d": "hard"})
    judge = _scripted_judge([("fail", [C1]), ("fail", [C1])])
    repair = _scripted_repair([ann1])
    verdict, rounds, critiques, final = asyncio.run(
        run_verify_loop(ann0, judge, repair, policy="repair", max_repair_rounds=1)
    )
    assert (verdict, rounds) == ("fail", 2)
    assert critiques == [C1, C1]
    assert final is ann1
    assert len(repair.calls) == 1         # budget respected


def test_drop_policy_fails_immediately_without_repair():
    ann = _annotation()
    judge = _scripted_judge([("fail", [C1])])
    repair = _scripted_repair([])
    verdict, rounds, critiques, final = asyncio.run(
        run_verify_loop(ann, judge, repair, policy="drop", max_repair_rounds=1)
    )
    assert (verdict, rounds) == ("fail", 1)
    assert critiques == [C1]
    assert repair.calls == []             # drop never repairs


def test_two_repair_rounds_then_pass():
    anns = [_annotation({"v": i}) for i in range(3)]
    judge = _scripted_judge([("fail", [C1]), ("fail", [C1]), ("pass", [C2])])
    repair = _scripted_repair(anns[1:])
    verdict, rounds, critiques, final = asyncio.run(
        run_verify_loop(anns[0], judge, repair, policy="repair", max_repair_rounds=2)
    )
    assert (verdict, rounds) == ("pass", 3)
    assert critiques == [C1, C1, C2]
    assert final is anns[2]
    assert len(repair.calls) == 2


def test_zero_repair_rounds_behaves_like_drop():
    ann = _annotation()
    judge = _scripted_judge([("fail", [C1])])
    repair = _scripted_repair([])
    verdict, rounds, _, _ = asyncio.run(
        run_verify_loop(ann, judge, repair, policy="repair", max_repair_rounds=0)
    )
    assert (verdict, rounds) == ("fail", 1)
    assert repair.calls == []


# ── error classification ────────────────────────────────────────────────────

def test_classify_errors():
    assert _classify_error(SchemaViolation(["/a: bad"], "{}"), "text") == ("schema_violation", False)
    assert _classify_error(ProviderRetryableError("x", "p", 5), "text") == (
        "provider_retryable_exhausted", True)
    assert _classify_error(ProviderFatalError("x", "p", 401), "text") == ("provider_fatal", False)
    assert _classify_error(OSError("bad image"), "ui") == ("image_decode_error", False)
    assert _classify_error(OSError("disk"), "text") == ("internal_error", False)
    assert _classify_error(ValueError("?"), "ui") == ("internal_error", False)


# ── stage item selection (no LLM needed: no eligible items) ─────────────────

def test_stage_skips_non_active_and_unannotated_items():
    stage = VerifyStage(cfg=None)  # cfg untouched when nothing is eligible
    dropped = PipelineItem(record=_record("b" * 16), status="dropped_dup",
                           annotation=_annotation())
    unannotated = PipelineItem(record=_record("c" * 16), status="active", annotation=None)
    failed = PipelineItem(record=_record("d" * 16), status="failed")
    batch = [dropped, unannotated, failed]
    out = asyncio.run(stage.run(batch, ctx=None))
    assert out is batch                   # same list object (stage contract)
    assert [it.status for it in out] == ["dropped_dup", "active", "failed"]
    assert all(it.verification is None for it in out)


def test_verification_result_shape():
    vr = VerificationResult(verdict="pass", rounds=2, critiques=(C1, C2))
    assert vr.verdict == "pass" and vr.rounds == 2 and vr.critiques == (C1, C2)


# ── verify.verdict trace event — content tier gating (§7.4 / CONTRACTS §8.3) ─

USER_SCHEMA = {"type": "object", "properties": {"intent": {"type": "string"}},
               "required": ["intent"], "additionalProperties": False}


def trace_cfg(*, enabled=True, content="refs") -> ResolvedConfig:
    return ResolvedConfig(
        tool=ToolConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality="text", input="in"),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(enabled=True, llm="default", instruction="给指令分类。"),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline=json.dumps(USER_SCHEMA)),
        trace=TraceConfig(enabled=enabled,
                          channels=("quality", "verify", "schema"), content=content),
        rubric=Rubric(name="default:text", criteria=()),
        class_views={},
        user_schema=USER_SCHEMA,
        limit=None,
        strict=False,
        dry_run=False,
        config_path="config.toml",
        project_path="project.toml",
        config_digest="sha256:0",
        project_digest="sha256:0",
    )


class _CapturingMetrics:
    """Event + counter capture stand-in for MetricsSink (no LLM involved)."""

    def __init__(self):
        self.events = []
        self.counters = {}

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None):
        self.events.append((ev, stage, batch_no, tuple(record_ids), dict(payload or {})))

    def count(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n


def _emit(*, enabled=True, content="refs", verdict="pass", judge=None, text="hello"):
    cfg = trace_cfg(enabled=enabled, content=content)
    metrics = _CapturingMetrics()
    ctx = SimpleNamespace(cfg=cfg, metrics=metrics, batch_no=3)
    rec = _record(text=text)
    VerifyStage(cfg)._emit_verdict_event(rec, verdict, 1, [C1], judge, ctx)
    (event,) = metrics.events
    return rec, event


def test_verdict_event_shape_and_critiques_at_refs_tier():
    rec, (ev, stage, batch_no, record_ids, payload) = _emit(content="refs")
    assert (ev, stage, batch_no, record_ids) == ("verify.verdict", "verify", 3, (rec.id,))
    assert payload["verdict"] == "pass" and payload["round"] == 1
    assert payload["critiques"] == [C1]
    assert "excerpt" not in payload
    assert "judge" not in payload


def test_verdict_event_none_tier_drops_llm_free_text():
    _, (_, _, _, _, payload) = _emit(content="none")
    assert "critiques" not in payload and "excerpt" not in payload


def test_verdict_event_excerpt_present_at_excerpt_tier():
    rec, (_, _, _, _, payload) = _emit(content="excerpt")
    assert payload["excerpt"] == {rec.id: "hello"}


def test_verdict_event_excerpt_present_at_full_tier():
    # §7.4: tiers are cumulative ("逐档递增") — "full" carries everything "excerpt" has.
    rec, (_, _, _, _, payload) = _emit(content="full")
    assert payload["excerpt"] == {rec.id: "hello"}
    assert payload["critiques"] == [C1]


def test_verdict_event_excerpt_absent_when_trace_disabled():
    _, (_, _, _, _, payload) = _emit(enabled=False, content="full")
    assert "excerpt" not in payload


def test_verdict_event_excerpt_truncated_to_200_chars():
    long_text = "长" * 500
    rec, (_, _, _, _, payload) = _emit(content="full", text=long_text)
    assert payload["excerpt"] == {rec.id: long_text[:200]}


def test_verdict_event_judge_field_for_panel():
    _, (_, _, _, _, payload) = _emit(content="refs", verdict="fail", judge="judge_a")
    assert payload["judge"] == "judge_a" and payload["verdict"] == "fail"


# ── multi-judge gather exception safety (§7.6 / asyncio.gather bug) ──────────

def test_multi_judge_schema_violation_preserves_majority():
    """When one judge in a 3-judge panel raises SchemaViolation before returning,
    the other two judges' majority verdict (pass) must be preserved — the gather
    must NOT discard sibling verdicts when one judge fails."""
    PASS_OBJ = {
        "verdict": "pass",
        "critiques": [{"aspect": "正确性", "opinion": "标注正确"}],
    }
    USAGE = Usage(10, 5)

    cfg = ResolvedConfig(
        tool=ToolConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality="text", input="in"),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(enabled=True, llm="default", instruction="测试指令"),
        verify=VerifyConfig(judges=("j1", "j2", "j3")),
        output=OutputConfig(schema_inline=json.dumps(USER_SCHEMA)),
        trace=TraceConfig(enabled=False),
        rubric=Rubric(name="default:text", criteria=()),
        class_views={},
        user_schema=USER_SCHEMA,
        limit=None, strict=False, dry_run=False,
        config_path="config.toml", project_path="project.toml",
        config_digest="sha256:0", project_digest="sha256:0",
    )

    stage = VerifyStage(cfg)
    rec = _record(text="你好")
    ann = _annotation()

    call_idx = [0]  # mutable counter for closure
    async def mock_complete_validated(judge, prompt, *, schema, record_ids,
                                      batch_no):
        idx = call_idx[0]
        call_idx[0] += 1
        if idx == 1:  # judge 2 (j2) raises SchemaViolation
            raise SchemaViolation(["违反 schema 约束"], '{"raw": "bad"}')
        return (PASS_OBJ, USAGE, 1, "mock-model")

    class MockEngine:
        pass
    engine = MockEngine()
    engine.complete_validated = mock_complete_validated

    ctx = SimpleNamespace(
        cfg=cfg,
        schema_engine=engine,
        batch_no=1,
        metrics=_CapturingMetrics(),
    )

    verdict, merged, fail_critiques = asyncio.run(
        stage._judge_round(rec, ann, 1, ctx)
    )
    assert verdict == "pass", f"Expected pass (majority 2/3), got {verdict}"
    # j2's exception should appear as a fail critique with aspect="judge_error"
    judge_errors = [c for c in fail_critiques if c.get("aspect") == "judge_error"]
    assert len(judge_errors) >= 1, (
        f"Expected at least one judge_error critique, got fail_critiques={fail_critiques}"
    )


def test_single_judge_schema_violation_propagates_to_classify():
    """In single-judge mode, SchemaViolation (and other non-critical exceptions)
    must be re-raised from _judge_round so that _verify_item's _classify_error
    can map it to the correct ErrorKind (schema_violation, not judge_error)."""
    cfg = ResolvedConfig(
        tool=ToolConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality="text", input="in"),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(enabled=True, llm="default", instruction="测试指令"),
        verify=VerifyConfig(),  # no judges → single-judge mode
        output=OutputConfig(schema_inline=json.dumps(USER_SCHEMA)),
        trace=TraceConfig(enabled=False),
        rubric=Rubric(name="default:text", criteria=()),
        class_views={},
        user_schema=USER_SCHEMA,
        limit=None, strict=False, dry_run=False,
        config_path="config.toml", project_path="project.toml",
        config_digest="sha256:0", project_digest="sha256:0",
    )

    stage = VerifyStage(cfg)
    rec = _record(text="你好")
    ann = _annotation()

    async def mock_complete_validated(judge, prompt, *, schema, record_ids,
                                      batch_no):
        raise SchemaViolation(["违反 schema 约束"], '{"raw": "bad"}')

    class MockEngine:
        pass
    engine = MockEngine()
    engine.complete_validated = mock_complete_validated

    ctx = SimpleNamespace(
        cfg=cfg,
        schema_engine=engine,
        batch_no=1,
        metrics=_CapturingMetrics(),
    )

    with pytest.raises(SchemaViolation):
        asyncio.run(stage._judge_round(rec, ann, 1, ctx))


# ── v1.7 label threading (R3/R5): class-effective [任务指令] + extra_criteria ─

CLASS_INSTRUCTION = "写作类专用标注指令。"
CLASS_EXTRA = "④ 写作风格是否得体"


def _classified_cfg(*, policy="drop", max_repair_rounds=1) -> ResolvedConfig:
    """trace_cfg + classify enabled + two class views: 'writing' overrides the
    annotate instruction and verify extra_criteria, 'qa' is zero-override."""
    base = trace_cfg()
    gverify = VerifyConfig(enabled=True, policy=policy,
                           max_repair_rounds=max_repair_rounds)
    views = {
        "writing": ClassView(
            name="writing", quality=base.quality, rubric=base.rubric,
            annotate=replace(base.annotate, instruction=CLASS_INSTRUCTION),
            generate=base.generate,
            verify=replace(gverify, extra_criteria=CLASS_EXTRA),
            extract=ExtractConfig()),
        "qa": ClassView(
            name="qa", quality=base.quality, rubric=base.rubric,
            annotate=base.annotate, generate=base.generate, verify=gverify,
            extract=ExtractConfig()),
    }
    classify = ClassifyConfig(
        enabled=True, fallback_class="qa",
        classes=(ClassSpec(name="writing", description="写作协助类指令"),
                 ClassSpec(name="qa", description="知识问答类指令")))
    return replace(base, classify=classify, class_views=views, verify=gverify)


def test_verify_prompt_label_takes_class_effective_values():
    cfg = _classified_cfg()
    bundle = build_verify_prompt(_record(text="写一首诗"), {"intent": "写作"}, cfg,
                                 label="writing")
    assert bundle.messages[0].parts[0].text == (
        "你是标注质量审核员。给定任务指令、原始数据与标注结果，独立判断标注是否合格。\n"
        "评审维度: ① 是否遵循任务指令 ② 与原始数据的事实一致性 ③ 字段语义是否正确填写\n"
        f"{CLASS_EXTRA}\n"
        "先逐维度给出简短意见，再给结论。"
    )
    assert bundle.messages[1].parts[0].text == (
        f"[任务指令] {CLASS_INSTRUCTION}\n"
        "[原始数据] 写一首诗\n"
        '[标注结果] {"intent": "写作"}'
    )


def test_verify_prompt_label_none_falls_back_to_global():
    cfg = _classified_cfg()
    bundle = build_verify_prompt(_record(text="hello"), {"intent": "x"}, cfg)
    # global verify.extra_criteria is empty → the line is omitted entirely
    assert CLASS_EXTRA not in bundle.messages[0].parts[0].text
    # global annotate.instruction (trace_cfg) fills [任务指令]
    assert bundle.messages[1].parts[0].text.startswith("[任务指令] 给指令分类。\n")
    # zero-override view behaves exactly like the global config
    qa_bundle = build_verify_prompt(_record(text="hello"), {"intent": "x"}, cfg,
                                    label="qa")
    assert qa_bundle == bundle


def test_verify_prompt_ui_branch_head_uses_class_instruction():
    from pathlib import Path
    from labelkit.types import ImageRef, UINode, UITree

    nodes = (
        UINode("1", None, 0, "FrameLayout", "", "", (0, 0, 1080, 1920), True, {}),
        UINode("2", "1", 1, "Button", "登录", "", (72, 952, 1008, 1096), True, {}),
    )
    rec = Record(id="9" * 16, modality="ui", text=None, raw=None,
                 ui_tree=UITree(nodes),
                 image=ImageRef(path=Path("image_1.png"), format="png", size_bytes=1),
                 ref=RecordRef("a/uitree_1.jsonl", None, 1, ()))
    cfg = _classified_cfg()
    bundle = build_verify_prompt(rec, {"intent": "x"}, cfg, label="writing")
    assert CLASS_EXTRA in bundle.messages[0].parts[0].text
    head = bundle.messages[1].parts[0].text
    assert head == f"[任务指令] {CLASS_INSTRUCTION}\n[原始数据]\n[屏幕截图]"


def test_verdict_event_label_only_when_classify_enabled():
    # classify disabled: a label arg must NOT surface in the payload
    cfg = trace_cfg()
    metrics = _CapturingMetrics()
    ctx = SimpleNamespace(cfg=cfg, metrics=metrics, batch_no=1)
    VerifyStage(cfg)._emit_verdict_event(_record(), "pass", 1, [C1], None, ctx,
                                         label="writing")
    (_, _, _, _, payload) = metrics.events[0]
    assert "label" not in payload

    # classify enabled + labeled item → payload carries it (R5)
    cfg2 = _classified_cfg()
    metrics2 = _CapturingMetrics()
    ctx2 = SimpleNamespace(cfg=cfg2, metrics=metrics2, batch_no=1)
    VerifyStage(cfg2)._emit_verdict_event(_record(), "pass", 1, [C1], None, ctx2,
                                          label="writing")
    (_, _, _, _, payload2) = metrics2.events[0]
    assert payload2["label"] == "writing"


def test_verify_item_threads_label_through_judges_and_repair(monkeypatch):
    """_verify_item injects item.classification.label into both closures: every
    judge prompt is class-effective, repair re-annotation gets label=..., and
    verify.verdict events carry the label."""
    cfg = _classified_cfg(policy="repair", max_repair_rounds=1)
    stage = VerifyStage(cfg)
    item = PipelineItem(
        record=_record(text="帮我写一首诗"),
        annotation=_annotation({"intent": "写作"}),
        classification=Classification(label="writing", labels=("writing",),
                                      source="llm", detail={}),
    )

    prompts = []
    script = iter([
        {"verdict": "fail", "critiques": [{"aspect": "字段语义", "opinion": "有误"}]},
        {"verdict": "pass", "critiques": [{"aspect": "字段语义", "opinion": "已修正"}]},
    ])

    async def mock_complete_validated(judge, prompt, *, schema, record_ids, batch_no):
        prompts.append(prompt)
        return (next(script), Usage(1, 1), 1, "m")

    engine = SimpleNamespace(complete_validated=mock_complete_validated)

    captured = {}

    async def fake_annotate_record(record, ctx, repair=None, label=None):
        captured["label"] = label
        captured["repair"] = repair
        return _annotation({"intent": "修正后"})

    monkeypatch.setattr("labelkit.annotate.annotate_record", fake_annotate_record)

    metrics = _CapturingMetrics()
    ctx = SimpleNamespace(cfg=cfg, schema_engine=engine, metrics=metrics, batch_no=1)
    asyncio.run(stage._verify_item(item, ctx))

    assert item.status == "active"
    assert item.verification.verdict == "pass" and item.verification.rounds == 2
    # repair path threaded the label into annotate_record (R3)
    assert captured["label"] == "writing"
    assert captured["repair"].critiques_text == "字段语义: 有误"
    # both judge rounds used the class-effective prompt
    assert len(prompts) == 2
    for prompt in prompts:
        assert CLASS_EXTRA in prompt.messages[0].parts[0].text
        assert prompt.messages[1].parts[0].text.startswith(
            f"[任务指令] {CLASS_INSTRUCTION}\n")
    # every verify.verdict event carries the label
    verdict_payloads = [p for (ev, _, _, _, p) in metrics.events
                        if ev == "verify.verdict"]
    assert len(verdict_payloads) == 2
    assert all(p["label"] == "writing" for p in verdict_payloads)


# ═════════════════════════════════════════════════════════════════════════════
# v1.8 stream branch — sequence review (S7) + two-phase member surgery (S8/S31)
# ═════════════════════════════════════════════════════════════════════════════

SEQ_INSTRUCTION = "标注任务标签。"


def _stream_cfg(*, policy="repair", max_repair_rounds=1, judges=(),
                extract_enabled=True) -> ResolvedConfig:
    base = trace_cfg(enabled=False, content="refs")
    return replace(
        base,
        run=replace(base.run, modality="ui"),
        segment=SegmentConfig(enabled=True),
        extract=ExtractConfig(enabled=extract_enabled),
        verify=VerifyConfig(enabled=True, llm="judge", judges=tuple(judges),
                            policy=policy, max_repair_rounds=max_repair_rounds),
        annotate=AnnotateConfig(enabled=True, llm="default",
                                instruction=SEQ_INSTRUCTION),
    )


def _frame(rid, pair_index=0) -> Record:
    """Bare UI frame (no tree — digests render empty, logic unaffected)."""
    return Record(id=rid, modality="ui", text=None, raw=None, ui_tree=None,
                  image=ImageRef(path=Path(f"{rid}.png"), format="png",
                                 size_bytes=1),
                  ref=RecordRef("a/uitree_0.jsonl", None, pair_index, ()))


def _ui_frame(rid, *texts) -> Record:
    """UI frame with a visible tree — for digest-bearing margin assertions."""
    nodes = [UINode("1", None, 0, "FrameLayout", "", "", (0, 0, 1080, 1920),
                    True, {})]
    for j, text in enumerate(texts):
        nodes.append(UINode(str(j + 2), "1", 1, "TextView", text, "",
                            (0, j * 100, 1080, (j + 1) * 100), True, {}))
    return Record(id=rid, modality="ui", text=None, raw=None,
                  ui_tree=UITree(tuple(nodes)),
                  image=ImageRef(path=Path(f"{rid}.png"), format="png",
                                 size_bytes=1),
                  ref=RecordRef("a/uitree_0.jsonl", None, 0, ()))


def _env(record, *, sid="s1", status="absorbed") -> PipelineItem:
    return PipelineItem(record=record, status=status, session_id=sid)


def _episode(members, *, sid="s1", eid="e" * 16, transitions=None,
             annotation=None, classification=None) -> PipelineItem:
    first = members[0]
    record = Record(id=eid, modality=first.modality, text=None, raw=None,
                    ui_tree=None, image=None,
                    ref=RecordRef(first.ref.source_file, first.ref.line_no,
                                  first.ref.pair_index, ()),
                    kind="sequence", members=tuple(members))
    return PipelineItem(record=record, session_id=sid,
                        annotation=annotation or _annotation({"task_label": "外卖"}),
                        classification=classification,
                        transitions=transitions)


def _transition(index, *, action_type="click", target="按钮", value=None,
                description="步骤", detail=None) -> Transition:
    return Transition(index=index,
                      action={"action_type": action_type, "target": target,
                              "value": value, "description": description},
                      model="m", attempts=1, detail=detail or {})


def _defect(kind, *, members=None, position=None, detail="缺陷") -> dict:
    return {"kind": kind, "members": members, "position": position,
            "detail": detail}


SEQ_C = {"aspect": "边界", "opinion": "证据一致"}


def _seq_obj(verdict, *, critiques=None, defects=None) -> dict:
    return {"critiques": [SEQ_C] if critiques is None else critiques,
            "defects": defects or [],
            "verdict": verdict}


class SeqJudgeEngine:
    """Pops per-record queued outcomes in call order (record_ids[0] keyed)."""

    def __init__(self, scripts):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls: list = []              # (profile, prompt, schema, record_ids)

    async def complete_validated(self, profile, prompt, schema=None, *,
                                 record_ids=(), batch_no=0, record=None):
        self.calls.append((profile, prompt, schema, record_ids))
        out = self.scripts[record_ids[0]].pop(0)
        if isinstance(out, Exception):
            raise out
        return out, Usage(), 1, "glm-5.2"


def _stub_judge_window(monkeypatch, relation="continues"):
    calls = []

    async def fake(frames, ctx):
        calls.append([f.id for f in frames])
        return [relation] * len(frames)

    monkeypatch.setattr("labelkit.segment.judge_window", fake)
    return calls


def _stub_extract(monkeypatch):
    calls = []

    async def fake(prev, curr, index, ctx, label=None):
        calls.append((prev.id, curr.id, index, label))
        return Transition(index=index,
                          action={"action_type": "other", "target": None,
                                  "value": None,
                                  "description": f"{prev.id}->{curr.id}"},
                          model="stub", attempts=1, detail={})

    monkeypatch.setattr("labelkit.extract.extract_transition", fake)
    return calls


def _stub_annotate(monkeypatch, output=None):
    calls = []

    async def fake(record, ctx, repair=None, label=None, transitions=None):
        calls.append(SimpleNamespace(record=record, repair=repair, label=label,
                                     transitions=transitions))
        return _annotation(output or {"task_label": "修正"})

    monkeypatch.setattr("labelkit.annotate.annotate_record", fake)
    return calls


def _run_verify(cfg, batch, engine):
    metrics = _CapturingMetrics()
    ctx = SimpleNamespace(cfg=cfg, llm=None, schema_engine=engine,
                          metrics=metrics, rng=None, batch_no=1)
    out = asyncio.run(VerifyStage(cfg).run(batch, ctx))
    assert out is batch                    # stage contract: same list object
    return metrics


# ── sequence review prompt: system text + six-section user order (§10.5) ────

def test_sequence_system_text_verbatim_without_extra_criteria():
    assert verify_sequence_system_text("") == (
        "你是标注质量审核员。给定任务指令、动作序列、边界余量与首末帧截图，独立判断该序列\n"
        "（episode）的标注是否合格。\n"
        "评审维度: ① 是否遵循任务指令 ② 与动作序列及首末帧证据的事实一致性 ③ 字段语义是否正确填写\n"
        "④ 段边界与成员构成是否成立（对照下列缺陷类型）\n"
        "缺陷类型（发现即列入 defects，可为空数组）:\n"
        "- label_mismatch: 标注的任务标签与序列证据不符\n"
        "- off_task_members: 段内混入与任务无关的成员帧（members 列出这些成员帧 id）\n"
        "- missing_head: 段首缺少任务起点帧（结合边界余量判断）\n"
        "- missing_tail: 段尾缺少任务终点帧（结合边界余量判断）\n"
        "- missing_members: 段中缺失成员帧（members 列出可指认的帧 id，无从指认则为 null）\n"
        "先逐维度给出简短意见，再列缺陷表，最后给结论。\n"
        "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
        '{"critiques": [{"aspect": <维度>, "opinion": <一句话意见>}, ...],\n'
        ' "defects": [{"kind": <缺陷类型>, "members": <帧 id 数组|null>,\n'
        '              "position": <位置说明|null>, "detail": <一句话>}, ...],\n'
        ' "verdict": "pass"|"fail"}'
    )


def test_sequence_system_text_extra_criteria_line_position():
    text = verify_sequence_system_text("⑤ 领域合规")
    assert ("④ 段边界与成员构成是否成立（对照下列缺陷类型）\n"
            "⑤ 领域合规\n"
            "缺陷类型（发现即列入 defects，可为空数组）:") in text


def test_sequence_prompt_six_section_order_with_transitions():
    cfg = _stream_cfg()
    m0, m1, m2 = _frame("f0"), _frame("f1"), _frame("f2")
    episode = _episode([m0, m1, m2])
    transitions = (_transition(0, description="点击登录"),
                   _transition(1, action_type="scroll", target=None,
                               value="down", description="向下滚动"))
    margin = "段首前 2: 无\n段首前 1: 无\n段尾后 1: 无\n段尾后 2: 无"
    bundle = build_verify_prompt(episode.record, {"task_label": "外卖"}, cfg,
                                 transitions=transitions, boundary_margin=margin)
    assert bundle.messages[0].parts[0].text == verify_sequence_system_text("")
    parts = bundle.messages[1].parts
    assert [p.kind for p in parts] == [
        "text", "text", "text", "text", "image", "text", "image", "text"]
    assert parts[0].text == f"[任务指令] {SEQ_INSTRUCTION}"
    assert parts[1].text == (
        "[动作序列]\n"
        "0. click（对象: 按钮；值: —）点击登录\n"
        "1. scroll（对象: —；值: down）向下滚动"
    )
    assert parts[2].text == f"[边界余量]\n{margin}"
    assert parts[3].text == "[首帧截图]" and parts[4].image is m0.image
    assert parts[5].text == "[末帧截图]" and parts[6].image is m2.image
    assert parts[7].text == '[标注结果] {"task_label": "外卖"}'


def test_sequence_prompt_action_section_omitted_when_transitions_none():
    cfg = _stream_cfg()
    episode = _episode([_frame("f0"), _frame("f1")])
    bundle = build_verify_prompt(episode.record, {"task_label": "外卖"}, cfg,
                                 transitions=None, boundary_margin="段首前 2: 无")
    parts = bundle.messages[1].parts
    assert [p.kind for p in parts] == [
        "text", "text", "text", "image", "text", "image", "text"]
    assert parts[1].text.startswith("[边界余量]\n")
    assert all("[动作序列]" not in p.text for p in parts if p.kind == "text")


def test_sequence_step_line_frozen_format():
    assert sequence_step_line(_transition(
        3, action_type="input_text", target="搜索框", value="奶茶",
        description="输入关键词")) == "3. input_text（对象: 搜索框；值: 奶茶）输入关键词"
    assert sequence_step_line(_transition(
        0, action_type="navigate_back", target=None, value=None,
        description="返回")) == "0. navigate_back（对象: —；值: —）返回"


# ── boundary margin: three fate states (spec 3.7.2) ─────────────────────────

def test_boundary_margin_three_fate_states():
    f0, f1, f2, f3 = (_ui_frame("f0", "首页"), _ui_frame("f1", "弹窗"),
                      _ui_frame("f2", "搜索"), _ui_frame("f3", "下单"))
    e0, e1, e2, e3 = _env(f0), _env(f1, status="dropped_noise"), _env(f2), _env(f3)
    e1.noise_attribution = ("segment", "noise")
    ep1 = _episode([f0], eid="a" * 16)             # 第 1 段 (batch order)
    ep2 = _episode([f2, f3], eid="b" * 16)         # under review = 第 2 段
    batch = [e0, e1, e2, e3, ep1, ep2]
    text = boundary_margin_text(ep2, batch, digest_max_chars=400)
    assert text == (
        f"段首前 2: {frame_digest(f0, 400)}（去向: 第 1 段）\n"
        f"段首前 1: {frame_digest(f1, 400)}（去向: noise）\n"
        "段尾后 1: 无\n"
        "段尾后 2: 无"
    )


def test_boundary_margin_frame_with_no_fate_renders_none():
    f0, f1 = _ui_frame("f0", "残留"), _ui_frame("f1", "搜索")
    e0 = _env(f0, status="failed")                 # exists, neither noise nor member
    e1 = _env(f1)
    ep = _episode([f1], eid="c" * 16)
    text = boundary_margin_text(ep, [e0, e1, ep], digest_max_chars=400)
    assert text == (
        "段首前 2: 无\n"
        f"段首前 1: {frame_digest(f0, 400)}（去向: 无）\n"
        "段尾后 1: 无\n"
        "段尾后 2: 无"
    )


# ── defect normalization (S31) + default routing entry (S7) ─────────────────

def test_normalize_defects_deterministic_union_dedup():
    entries = [
        _defect("missing_tail", position="段尾", detail="评审员甲"),
        _defect("off_task_members", members=["b", "a"]),
        _defect("missing_tail", position="段尾", detail="评审员乙"),   # same key → dropped
        _defect("label_mismatch"),
        _defect("missing_tail", position="中段"),
    ]
    out = normalize_defects(entries)
    assert [(d["kind"], d["position"], tuple(d["members"] or ())) for d in out] == [
        ("label_mismatch", None, ()),
        ("off_task_members", None, ("b", "a")),
        ("missing_tail", "中段", ()),
        ("missing_tail", "段尾", ()),
    ]
    # first occurrence (union order) survives de-dup
    assert out[3]["detail"] == "评审员甲"
    # input order does not matter: reversed input gives the same table
    assert normalize_defects(list(reversed(entries)))[:3] == out[:3]


def test_multi_judge_defects_union_over_fail_voters_only():
    cfg = _stream_cfg(policy="drop", judges=("j1", "j2", "j3"))
    f0, f1 = _frame("f0"), _frame("f1")
    ep = _episode([f0, f1])
    batch = [_env(f0), _env(f1), ep]
    d_tail = _defect("missing_tail", position="尾")
    d_off = _defect("off_task_members", members=["f1"])
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[d_tail]),                  # j1 fail
        _seq_obj("pass", defects=[_defect("missing_head")]), # j2 pass → excluded
        _seq_obj("fail", defects=[d_off, d_tail]),           # j3 fail (dup tail)
    ]})
    metrics = _run_verify(cfg, batch, engine)
    assert ep.status == "dropped_verify"
    assert [d["kind"] for d in ep.verification.defects] == [
        "off_task_members", "missing_tail"]                  # kind enum order
    assert metrics.counters["verify.defects.off_task_members"] == 1
    assert metrics.counters["verify.defects.missing_tail"] == 1
    assert "verify.defects.missing_head" not in metrics.counters


def test_fail_with_empty_defects_normalized_to_default_label_mismatch_drop():
    cfg = _stream_cfg(policy="drop")
    f0 = _frame("f0")
    ep = _episode([f0])
    engine = SeqJudgeEngine({ep.record.id: [_seq_obj("fail", defects=[])]})
    metrics = _run_verify(cfg, [_env(f0), ep], engine)
    assert ep.status == "dropped_verify"
    assert ep.verification.defects == (dict(_DEFAULT_FAIL_DEFECT),)
    assert metrics.counters["verify.defects.label_mismatch"] == 1


def test_fail_with_empty_defects_routes_repair_reannotation(monkeypatch):
    cfg = _stream_cfg()
    annotate_calls = _stub_annotate(monkeypatch)
    f0 = _frame("f0")
    ep = _episode([f0])
    engine = SeqJudgeEngine({ep.record.id: [_seq_obj("fail", defects=[],
                                                     critiques=[C1]),
                                            _seq_obj("pass")]})
    metrics = _run_verify(cfg, [_env(f0), ep], engine)
    assert ep.status == "active"
    assert (ep.verification.verdict, ep.verification.rounds) == ("pass", 2)
    assert ep.verification.defects == ()               # last round's (pass) table
    (call,) = annotate_calls
    assert call.repair.critiques_text == "字段语义: difficulty 应为 medium"
    assert not hasattr(ep, "stream_repaired")          # no member surgery happened
    assert "verify.membership_repairs" not in metrics.counters


# ── sequence review plumbing: schema + verdict events with defects ──────────

def test_sequence_review_uses_defect_schema_and_event_carries_defects():
    cfg = _stream_cfg(policy="drop")
    f0 = _frame("f0")
    ep = _episode([f0])
    raw = [_defect("missing_tail", detail="尾帧缺失")]
    engine = SeqJudgeEngine({ep.record.id: [_seq_obj("fail", defects=raw)]})
    metrics = _run_verify(cfg, [_env(f0), ep], engine)
    (call,) = engine.calls
    assert call[2] == defect_verdict_schema()          # NOT the frozen VERDICT_SCHEMA
    ((_, _, _, record_ids, payload),) = [e for e in metrics.events
                                         if e[0] == "verify.verdict"]
    assert record_ids == (ep.record.id,)
    assert payload["verdict"] == "fail" and payload["defects"] == raw
    assert payload["critiques"] == [SEQ_C]


def test_stream_batch_singles_keep_classic_path():
    cfg = _stream_cfg(policy="drop")
    single = PipelineItem(record=_record("5" * 16, text="你好"),
                          annotation=_annotation())
    f0 = _frame("f0")
    ep = _episode([f0])
    engine = SeqJudgeEngine({
        "5" * 16: [{"critiques": [C2], "verdict": "pass"}],
        ep.record.id: [_seq_obj("pass")],
    })
    _run_verify(cfg, [single, _env(f0), ep], engine)
    assert single.verification.verdict == "pass"
    assert single.verification.defects == ()           # non-stream: always empty
    assert ep.verification.verdict == "pass"
    schema_by_id = {ids[0]: schema for _, _, schema, ids in engine.calls}
    assert schema_by_id["5" * 16] == VERDICT_SCHEMA    # regression anchor
    assert schema_by_id[ep.record.id] == defect_verdict_schema()


# ── off_task_members shrink: the full surgery chain ─────────────────────────

def test_off_task_members_shrink_full_chain(monkeypatch):
    cfg = _stream_cfg()
    extract_calls = _stub_extract(monkeypatch)
    annotate_calls = _stub_annotate(monkeypatch)
    f0, f1, f2 = _frame("f0"), _frame("f1"), _frame("f2")
    e0, e1, e2 = _env(f0), _env(f1), _env(f2)
    ep = _episode([f0, f1, f2],
                  transitions=(_transition(0, description="步一"),
                               _transition(1, description="步二")))
    ann0 = ep.annotation
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", critiques=[C1],
                 defects=[_defect("off_task_members", members=["f1"])]),
        _seq_obj("pass"),
    ]})
    metrics = _run_verify(cfg, [e0, e1, e2, ep], engine)

    # members shrank; the record id did NOT change (never recomputed)
    assert [m.id for m in ep.record.members] == ["f0", "f2"]
    assert ep.record.id == "e" * 16
    # the shrunk frame envelope flipped with the verify attribution
    assert e1.status == "dropped_noise"
    assert e1.noise_attribution == ("verify", "off_task_member")
    assert e0.status == "absorbed" and e2.status == "absorbed"
    # seam re-extraction on the new adjacency, rebuilt ordinal
    assert extract_calls == [("f0", "f2", 0, None)]
    (t,) = ep.transitions                              # fully renumbered
    assert t.index == 0 and t.detail["reseamed"] is True
    assert t.action["description"] == "f0->f2"
    assert ep.stream_repaired is True
    # re-annotation received the rebuilt record + transitions and the critiques
    (call,) = annotate_calls
    assert call.record is ep.record
    assert call.transitions == ep.transitions
    assert call.repair.previous_output == ann0.output
    assert call.repair.critiques_text == "字段语义: difficulty 应为 medium"
    # second-round re-review passed
    assert ep.status == "active"
    assert (ep.verification.verdict, ep.verification.rounds) == ("pass", 2)
    assert ep.annotation.output == {"task_label": "修正"}
    assert metrics.counters["verify.membership_repairs"] == 1
    assert "verify.boundary_flags" not in metrics.counters


def test_off_task_naming_every_member_downgrades_to_fail(monkeypatch):
    """The full-shrink guard: a defect naming EVERY member cannot empty the
    episode — no surgery happens and the fail verdict drops it whole."""
    cfg = _stream_cfg()
    _stub_extract(monkeypatch)
    annotate_calls = _stub_annotate(monkeypatch)
    f0, f1 = _frame("f0"), _frame("f1")
    e0, e1 = _env(f0), _env(f1)
    ep = _episode([f0, f1])
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[_defect("off_task_members",
                                          members=["f0", "f1"])]),
    ]})
    metrics = _run_verify(cfg, [e0, e1, ep], engine)
    assert ep.status == "dropped_verify"
    assert [m.id for m in ep.record.members] == ["f0", "f1"]
    assert e0.status == "absorbed" and e1.status == "absorbed"
    assert annotate_calls == []
    assert "verify.membership_repairs" not in metrics.counters


# ── missing_tail reclaim: the full recovery chain ────────────────────────────

def test_missing_tail_reclaim_full_chain(monkeypatch):
    cfg = _stream_cfg()
    jw_calls = _stub_judge_window(monkeypatch, relation="continues")
    extract_calls = _stub_extract(monkeypatch)
    annotate_calls = _stub_annotate(monkeypatch)
    f0, f1, f2 = _frame("f0"), _frame("f1"), _frame("f2")
    e0, e1 = _env(f0), _env(f1)
    e2 = _env(f2, status="dropped_noise")              # segment noise pool
    e2.noise_attribution = ("segment", "noise")
    ep = _episode([f0, f1], transitions=(_transition(0, description="步一"),))
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[_defect("missing_tail")]),
        _seq_obj("pass"),
    ]})
    metrics = _run_verify(cfg, [e0, e1, e2, ep], engine)

    # the noise frame was re-judged in a [prev member, candidate] window and
    # reclaimed: dropped_noise → absorbed, inserted at its position rank
    assert jw_calls == [["f1", "f2"]]
    assert e2.status == "absorbed"
    assert [m.id for m in ep.record.members] == ["f0", "f1", "f2"]
    # seam re-extraction only for the NEW pair; untouched step kept, renumbered
    assert extract_calls == [("f1", "f2", 1, None)]
    assert [t.index for t in ep.transitions] == [0, 1]
    assert ep.transitions[0].action["description"] == "步一"
    assert "reseamed" not in ep.transitions[0].detail
    assert ep.transitions[1].detail["reseamed"] is True
    assert ep.stream_repaired is True
    assert len(annotate_calls) == 1
    assert ep.status == "active"
    assert (ep.verification.verdict, ep.verification.rounds) == ("pass", 2)
    assert metrics.counters["verify.membership_repairs"] == 1
    assert "verify.boundary_flags" not in metrics.counters


def test_reclaim_rejected_by_rejudgment_marks_boundary_flag(monkeypatch):
    cfg = _stream_cfg()
    jw_calls = _stub_judge_window(monkeypatch, relation="context_switch")
    _stub_extract(monkeypatch)
    annotate_calls = _stub_annotate(monkeypatch)
    f0, f1, f2 = _frame("f0"), _frame("f1"), _frame("f2")
    e2 = _env(f2, status="dropped_noise")
    e2.noise_attribution = ("segment", "noise")
    ep = _episode([f0, f1], transitions=(_transition(0),))
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[_defect("missing_tail")]),
    ]})
    metrics = _run_verify(cfg, [_env(f0), _env(f1), e2, ep], engine)
    assert jw_calls == [["f1", "f2"]]                  # re-judgment did happen
    assert e2.status == "dropped_noise"                # rejected: stays noise
    assert [m.id for m in ep.record.members] == ["f0", "f1"]
    assert annotate_calls == []                        # nothing repairable
    assert ep.status == "dropped_verify"
    assert ep.verification.rounds == 1
    (d,) = ep.verification.defects
    assert d["kind"] == "missing_tail" and "suspected" not in d
    assert metrics.counters["verify.boundary_flags"] == 1
    assert "verify.membership_repairs" not in metrics.counters
    assert not hasattr(ep, "stream_repaired")


# ── mark-only downgrades: session_split / neighbor-held / capture_gap ───────

def test_session_split_episode_downgrades_reclaim_with_suspected(monkeypatch):
    cfg = _stream_cfg()
    jw_calls = _stub_judge_window(monkeypatch)
    _stub_annotate(monkeypatch)
    f0, f1, f2 = _frame("f0"), _frame("f1"), _frame("f2")
    e2 = _env(f2, status="dropped_noise")              # candidate DOES exist
    e2.noise_attribution = ("segment", "noise")
    ep = _episode([f0, f1])
    ep.session_split = True                            # M10's hard-split mark (S21)
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[_defect("missing_tail")]),
    ]})
    metrics = _run_verify(cfg, [_env(f0), _env(f1), e2, ep], engine)
    assert jw_calls == []                              # never re-judged
    assert e2.status == "dropped_noise"
    assert ep.status == "dropped_verify"
    (d,) = ep.verification.defects
    assert d["suspected"] == "session_split"
    assert metrics.counters["verify.boundary_flags"] == 1


def test_candidate_held_by_neighbor_episode_marks_only(monkeypatch):
    cfg = _stream_cfg()
    jw_calls = _stub_judge_window(monkeypatch)
    _stub_annotate(monkeypatch)
    f0, f1, f2, f3 = (_frame("f0"), _frame("f1"), _frame("f2"), _frame("f3"))
    ep1 = _episode([f0, f1], eid="a" * 16)
    ep2 = _episode([f2, f3], eid="b" * 16)
    engine = SeqJudgeEngine({
        ep1.record.id: [_seq_obj("fail", defects=[_defect("missing_tail")])],
        ep2.record.id: [_seq_obj("pass")],
    })
    metrics = _run_verify(
        cfg, [_env(f0), _env(f1), _env(f2), _env(f3), ep1, ep2], engine)
    assert jw_calls == []                              # no cross-episode theft
    assert ep1.status == "dropped_verify"
    (d,) = ep1.verification.defects
    assert d["kind"] == "missing_tail" and "suspected" not in d
    assert ep2.status == "active"                      # neighbor untouched
    assert [m.id for m in ep2.record.members] == ["f2", "f3"]
    assert metrics.counters["verify.boundary_flags"] == 1


def test_no_candidate_anywhere_marks_capture_gap(monkeypatch):
    cfg = _stream_cfg()
    jw_calls = _stub_judge_window(monkeypatch)
    _stub_annotate(monkeypatch)
    f0, f1 = _frame("f0"), _frame("f1")
    ep = _episode([f0, f1])                            # session ends at the tail
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[_defect("missing_tail")]),
    ]})
    metrics = _run_verify(cfg, [_env(f0), _env(f1), ep], engine)
    assert jw_calls == []
    (d,) = ep.verification.defects
    assert d["suspected"] == "capture_gap"
    assert metrics.counters["verify.boundary_flags"] == 1


def test_verify_dropped_frames_never_reclaimed(monkeypatch):
    """The shrink↔reclaim ping-pong guard: a frame verify itself dropped
    (attribution stage 'verify') is not a candidate — capture_gap instead."""
    cfg = _stream_cfg()
    jw_calls = _stub_judge_window(monkeypatch)
    _stub_annotate(monkeypatch)
    f0, f1, f2 = _frame("f0"), _frame("f1"), _frame("f2")
    e2 = _env(f2, status="dropped_noise")
    e2.noise_attribution = ("verify", "off_task_member")
    ep = _episode([f0, f1])
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[_defect("missing_tail")]),
    ]})
    _run_verify(cfg, [_env(f0), _env(f1), e2, ep], engine)
    assert jw_calls == []
    assert e2.status == "dropped_noise"
    (d,) = ep.verification.defects
    assert d["suspected"] == "capture_gap"


# ── multi fan-out clones: membership surgery downgrades to mark-only (S8) ───

def _stream_classified_cfg() -> ResolvedConfig:
    """_stream_cfg + classify(multi) enabled with two zero-override class views
    ('a' = the hit set's first label → original envelope, 'b' → clone)."""
    base = _stream_cfg()
    views = {name: ClassView(name=name, quality=base.quality, rubric=base.rubric,
                             annotate=base.annotate, generate=base.generate,
                             verify=base.verify, extract=base.extract)
             for name in ("a", "b")}
    classify = ClassifyConfig(
        enabled=True, assignment="multi", max_labels=2, fallback_class="a",
        classes=(ClassSpec(name="a", description="甲类"),
                 ClassSpec(name="b", description="乙类")))
    return replace(base, classify=classify, class_views=views)


def test_multi_clone_membership_defects_mark_only(monkeypatch):
    cfg = _stream_classified_cfg()
    jw_calls = _stub_judge_window(monkeypatch)
    _stub_extract(monkeypatch)
    annotate_calls = _stub_annotate(monkeypatch)
    f0, f1, f2 = _frame("f0"), _frame("f1"), _frame("f2")
    e0, e1 = _env(f0), _env(f1)
    e2 = _env(f2, status="dropped_noise")              # a real candidate exists
    e2.noise_attribution = ("segment", "noise")
    original = _episode([f0, f1], classification=Classification(
        label="a", labels=("a", "b"), source="llm", detail={}))
    clone = PipelineItem(record=original.record, session_id="s1",
                         annotation=_annotation({"task_label": "外卖"}),
                         classification=Classification(
                             label="b", labels=("a", "b"), source="llm",
                             detail={}))
    engine = SeqJudgeEngine({original.record.id: [
        _seq_obj("pass"),                              # original reviewed first
        _seq_obj("fail", defects=[_defect("off_task_members", members=["f1"]),
                                  _defect("missing_tail")]),
    ]})
    metrics = _run_verify(cfg, [e0, e1, e2, original, clone], engine)
    # NO surgery executed on the shared member set
    assert e1.status == "absorbed" and e2.status == "dropped_noise"
    assert [m.id for m in clone.record.members] == ["f0", "f1"]
    assert jw_calls == [] and annotate_calls == []
    assert original.status == "active"
    assert clone.status == "dropped_verify"
    kinds = [d["kind"] for d in clone.verification.defects]
    assert kinds == ["off_task_members", "missing_tail"]
    assert all("suspected" not in d for d in clone.verification.defects)
    # only the missing_* downgrade counts as a mark-only boundary determination
    assert metrics.counters["verify.boundary_flags"] == 1
    assert "verify.membership_repairs" not in metrics.counters


# ── contention: two episodes claim the same noise frame (determinism) ───────

def _contention_scenario(monkeypatch):
    cfg = _stream_cfg(extract_enabled=False)
    jw_calls = _stub_judge_window(monkeypatch, relation="continues")
    annotate_calls = _stub_annotate(monkeypatch)
    f0, f1, f2, f3, f4 = (_frame("f0"), _frame("f1"), _frame("f2"),
                          _frame("f3"), _frame("f4"))
    e2 = _env(f2, status="dropped_noise")
    e2.noise_attribution = ("segment", "noise")
    ep1 = _episode([f0, f1], eid="a" * 16)             # wants f2 as its tail
    ep2 = _episode([f3, f4], eid="b" * 16)             # wants f2 as its head
    batch = [_env(f0), _env(f1), e2, _env(f3), _env(f4), ep1, ep2]
    engine = SeqJudgeEngine({
        ep1.record.id: [_seq_obj("fail", defects=[_defect("missing_tail")]),
                        _seq_obj("pass")],
        ep2.record.id: [_seq_obj("fail", defects=[_defect("missing_head")])],
    })
    metrics = _run_verify(cfg, batch, engine)
    return {
        "ep1_members": [m.id for m in ep1.record.members],
        "ep1_status": ep1.status,
        "ep2_members": [m.id for m in ep2.record.members],
        "ep2_status": ep2.status,
        "ep2_defects": ep2.verification.defects,
        "noise_status": e2.status,
        "jw_calls": jw_calls,
        "annotate_count": len(annotate_calls),
        "counters": metrics.counters,
    }


def test_noise_frame_contention_batch_position_order_wins(monkeypatch):
    first = _contention_scenario(monkeypatch)
    # ep1 sits earlier in the batch → deterministic "position-come" claim
    assert first["ep1_members"] == ["f0", "f1", "f2"]
    assert first["ep1_status"] == "active"
    assert first["noise_status"] == "absorbed"
    assert first["jw_calls"] == [["f1", "f2"]]         # ONE re-judgment (ep1's)
    # ep2 lost the claim: the frame is held by ep1 this round → level-2
    # "neighbor" mark-only, NEVER capture_gap (D5 — the frame demonstrably
    # exists and was reclaimed by the adjacent episode).
    assert first["ep2_members"] == ["f3", "f4"]
    assert first["ep2_status"] == "dropped_verify"
    (d,) = first["ep2_defects"]
    assert d["kind"] == "missing_head" and "suspected" not in d
    assert first["counters"]["verify.membership_repairs"] == 1
    assert first["counters"]["verify.boundary_flags"] == 1
    assert first["annotate_count"] == 1                # only ep1 re-annotated
    # determinism: a second identical run produces the identical outcome
    second = _contention_scenario(monkeypatch)
    assert second == first


# ── repair budget + rounds semantics (unchanged from the non-stream loop) ───

def test_repair_budget_exhausted_drops_episode(monkeypatch):
    cfg = _stream_cfg(max_repair_rounds=1)
    annotate_calls = _stub_annotate(monkeypatch)
    f0 = _frame("f0")
    ep = _episode([f0])
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", critiques=[C1], defects=[_defect("label_mismatch")]),
        _seq_obj("fail", critiques=[C2], defects=[]),
    ]})
    metrics = _run_verify(cfg, [_env(f0), ep], engine)
    assert len(annotate_calls) == 1                    # budget respected
    assert ep.status == "dropped_verify"
    assert (ep.verification.verdict, ep.verification.rounds) == ("fail", 2)
    assert ep.verification.critiques == (C1, C2)       # accumulated over rounds
    # final round's fail+empty table normalized to the default entry
    assert ep.verification.defects == (dict(_DEFAULT_FAIL_DEFECT),)
    # D4: defects are counted at REVIEW time, one per adjudicated defect —
    # both fail rounds contributed a (normalized) label_mismatch entry.
    assert metrics.counters["verify.defects.label_mismatch"] == 2


def test_extract_disabled_shrink_keeps_transitions_none(monkeypatch):
    """extract off → transitions stay None end-to-end; the shrink chain still
    runs (members, envelope flip, re-annotation, re-review)."""
    cfg = _stream_cfg(extract_enabled=False)
    annotate_calls = _stub_annotate(monkeypatch)
    f0, f1 = _frame("f0"), _frame("f1")
    e0, e1 = _env(f0), _env(f1)
    ep = _episode([f0, f1])                            # transitions=None
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[_defect("off_task_members", members=["f1"])]),
        _seq_obj("pass"),
    ]})
    metrics = _run_verify(cfg, [e0, e1, ep], engine)
    assert [m.id for m in ep.record.members] == ["f0"]
    assert e1.status == "dropped_noise"
    assert ep.transitions is None
    assert ep.stream_repaired is True
    (call,) = annotate_calls
    assert call.transitions is None
    assert ep.status == "active"
    assert metrics.counters["verify.membership_repairs"] == 1

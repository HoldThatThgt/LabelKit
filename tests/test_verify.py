"""Offline unit tests for M7 verify: pure logic only (no LLM, no mocks of LLM).

Covers prompt-text assembly, majority vote, critique rendering, the policy state
machine on synthetic verdict sequences, rounds accounting, error classification,
stage item-selection behavior and verify.verdict trace-event payload tier gating.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from labelkit.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ClassSpec,
    ClassView,
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
from labelkit.errors import (
    ProviderFatalError,
    ProviderRetryableError,
    SchemaViolation,
)
from labelkit.types import (
    Annotation,
    Classification,
    PipelineItem,
    Record,
    RecordRef,
    Usage,
    VerificationResult,
)
from labelkit.verify import (
    VerifyStage,
    _classify_error,
    build_verify_prompt,
    majority_verdict,
    render_critiques_text,
    run_verify_loop,
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
        dedup=DedupConfig(),
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
    """Event capture stand-in for MetricsSink (no LLM involved)."""

    def __init__(self):
        self.events = []

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None):
        self.events.append((ev, stage, batch_no, tuple(record_ids), dict(payload or {})))


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
        dedup=DedupConfig(),
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
        dedup=DedupConfig(),
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
            verify=replace(gverify, extra_criteria=CLASS_EXTRA)),
        "qa": ClassView(
            name="qa", quality=base.quality, rubric=base.rubric,
            annotate=base.annotate, generate=base.generate, verify=gverify),
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

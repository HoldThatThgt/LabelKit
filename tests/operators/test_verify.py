"""Offline unit tests for M7 verify: pure logic only (no LLM, no mocks of LLM).

Covers prompt-text assembly, majority vote, critique rendering, the policy state
machine on synthetic verdict sequences, rounds accounting, error classification,
stage item-selection behavior and verify.verdict trace-event payload tier gating.

v1.8 stream branch (S7/S8/S31): the sequence-variant review prompt (six-section
order, boundary-margin fate states, [动作序列] omission), defect-table collection
and normalization, and the two-phase batch-level member surgery — driven through
in-process complete_validated stubs (test_segment 惯例) with the segment,
extract, classify and annotate pure-leaf seams monkeypatched directly.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ClassSpec,
    ClassView,
    ConsoleConfig,
    DedupConfig,
    ExtractConfig,
    FrameAnnotateConfig,
    FrameClassifyConfig,
    FrameClassView,
    GenerateConfig,
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
from labelkit.common.errors import (
    CircuitBreakerTripped,
    ContextOverflowError,
    OutputTruncatedError,
    ProviderFatalError,
    ProviderRetryableError,
    SchemaViolation,
    InternalError,
    PostprocessorError,
)
from labelkit.common.extensions.hooks import ResolvedHook
from labelkit.common.contracts.generation import SequenceTemporalContext
from labelkit.common.inference import budget as budget_mod
from labelkit.common.inference.schema_engine import (
    CandidateFinalizerContractError,
    VERDICT_SCHEMA,
    defect_verdict_schema,
)
from labelkit.common.contracts.types import (
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
from labelkit.operators.verify import (
    _DEFAULT_FAIL_DEFECT,
    _EpisodeReview,
    DEFECT_KINDS,
    VerifyPromptOptions,
    VerifyStage,
    _PromptFit,
    _VerdictEvent,
    _classify_error,
    boundary_margin_text,
    build_verify_prompt,
    fragment_structure_text,
    majority_verdict,
    normalize_defects,
    render_critiques_text,
    run_verify_loop,
    sequence_step_line,
    verify_sequence_system_text,
    verify_system_text,
    verify_user_text,
    verify_verdict_sequence_system_text,
)
from labelkit.operators.stream_verify import StreamVerifyDriver
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.sequence_capacity import SessionAttemptScope
from labelkit.common.contracts.types import SequenceBounds, SequenceCapacity, CapacityCut



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
        console=ConsoleConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality="text", input="in"),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(),
        stitch=StitchConfig(),
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
        model_user_schema=USER_SCHEMA,
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


class _TaskRunner:
    """按输入序返回结果的结构化单元测试执行器。"""

    def __init__(self):
        self.groups = []

    async def run_group(self, request):
        self.groups.append(request.tasks)
        results = [None] * len(request.tasks)

        async def run_one(index, spec):
            results[index] = await spec.operation()

        async with asyncio.TaskGroup() as group:
            for index, spec in enumerate(request.tasks):
                group.create_task(run_one(index, spec))
        return tuple(results)


class _ReverseTaskRunner:
    """让叶任务逆声明序完成，并在返回 reducer 前校验业务快照不变。"""

    def __init__(self, snapshot=None):
        self.groups = []
        self.completion_orders = []
        self.snapshot = snapshot

    async def run_group(self, request):
        before = self.snapshot() if self.snapshot is not None else None
        self.groups.append(tuple(spec.task_id for spec in request.tasks))
        results = [None] * len(request.tasks)
        completion_order = []
        condition = asyncio.Condition()
        ready = 0
        turn = len(request.tasks) - 1

        async def run_one(index, spec):
            nonlocal ready, turn
            results[index] = await spec.operation()
            async with condition:
                ready += 1
                condition.notify_all()
                await condition.wait_for(
                    lambda: ready == len(request.tasks) and turn == index,
                )
                completion_order.append(index)
                turn -= 1
                condition.notify_all()

        async with asyncio.TaskGroup() as group:
            for index, spec in enumerate(request.tasks):
                group.create_task(run_one(index, spec))
        if self.snapshot is not None:
            assert self.snapshot() == before
        self.completion_orders.append(completion_order)
        return tuple(results)


class _SerialTaskRunner:
    """原样传播控制异常的最小 TaskExecutor。"""

    def __init__(self):
        self.groups = []

    async def run_group(self, request):
        self.groups.append(request.tasks)
        return tuple([await spec.operation() for spec in request.tasks])


class _RejectTaskRunner:
    """任何任务组提交都会令测试失败。"""

    async def run_group(self, request):
        raise AssertionError("zero-call verify submitted a task group")


def _task_context(**fields):
    """提供实际 RunContext 协议；模型行为仍为纯离线函数。"""
    tasks = fields.pop("tasks", _TaskRunner())
    cfg = fields.setdefault("cfg", trace_cfg(enabled=False))
    fields.setdefault("metrics", _CapturingMetrics())
    fields.setdefault("rng", None)
    fields.setdefault("batch_no", 1)
    fields.setdefault("schema_engine", None)
    fields.setdefault("llm", None)
    if cfg.segment.enabled:
        fields["session_attempt"] = SessionAttemptScope("s1", 1, 1, "verify")
        if fields["llm"] is None:
            fields["llm"] = SimpleNamespace(calibrator=_FixedCalibrator(0))
    return RunContext(tasks=tasks, task_namespace="test:session:1:stage:verify", **fields)


def _emit(*, enabled=True, content="refs", verdict="pass", judge=None, text="hello"):
    cfg = trace_cfg(enabled=enabled, content=content)
    metrics = _CapturingMetrics()
    ctx = SimpleNamespace(cfg=cfg, metrics=metrics, batch_no=3)
    rec = _record(text=text)
    VerifyStage(cfg)._emit_verdict_event(
        _VerdictEvent(record=rec, verdict=verdict, round_no=1, critiques=[C1],
                      judge=judge), ctx)
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
        console=ConsoleConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality="text", input="in"),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(),
        stitch=StitchConfig(),
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
        model_user_schema=USER_SCHEMA,
        limit=None, strict=False, dry_run=False,
        config_path="config.toml", project_path="project.toml",
        config_digest="sha256:0", project_digest="sha256:0",
    )

    stage = VerifyStage(cfg)
    rec = _record(text="你好")
    ann = _annotation()

    call_idx = [0]  # mutable counter for closure
    async def mock_complete_validated(judge, prompt, *, schema, scope):
        idx = call_idx[0]
        call_idx[0] += 1
        if idx == 1:  # judge 2 (j2) raises SchemaViolation
            raise SchemaViolation(["违反 schema 约束"], '{"raw": "bad"}')
        return (PASS_OBJ, USAGE, 1, "mock-model")

    class MockEngine:
        pass
    engine = MockEngine()
    engine.complete_validated = mock_complete_validated

    ctx = _task_context(
        cfg=cfg,
        schema_engine=engine,
        batch_no=1,
        metrics=_CapturingMetrics(),
    )
    item = PipelineItem(record=rec, annotation=ann)
    asyncio.run(stage.run([item], ctx))
    assert item.verification.verdict == "pass"
    # j2's exception should appear as a fail critique with aspect="judge_error"
    judge_errors = [c for c in item.verification.critiques
                    if c.get("aspect") == "judge_error"]
    assert len(judge_errors) >= 1, (
        f"Expected at least one judge_error critique, got {item.verification.critiques}"
    )


def test_single_judge_schema_violation_becomes_record_outcome():
    """单评委 SchemaViolation 由 reducer 精确归类为记录级失败。"""
    cfg = ResolvedConfig(
        tool=ToolConfig(),
        console=ConsoleConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality="text", input="in"),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(),
        stitch=StitchConfig(),
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
        model_user_schema=USER_SCHEMA,
        limit=None, strict=False, dry_run=False,
        config_path="config.toml", project_path="project.toml",
        config_digest="sha256:0", project_digest="sha256:0",
    )

    stage = VerifyStage(cfg)
    rec = _record(text="你好")
    ann = _annotation()

    async def mock_complete_validated(judge, prompt, *, schema, scope):
        raise SchemaViolation(["违反 schema 约束"], '{"raw": "bad"}')

    class MockEngine:
        pass
    engine = MockEngine()
    engine.complete_validated = mock_complete_validated

    ctx = _task_context(
        cfg=cfg,
        schema_engine=engine,
        batch_no=1,
        metrics=_CapturingMetrics(),
    )
    item = PipelineItem(record=rec, annotation=ann)
    asyncio.run(stage.run([item], ctx))
    assert item.status == "failed"
    assert [error.kind for error in item.errors] == ["schema_violation"]


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
            extract=ExtractConfig(), model_schema=base.model_user_schema),
        "qa": ClassView(
            name="qa", quality=base.quality, rubric=base.rubric,
            annotate=base.annotate, generate=base.generate, verify=gverify,
            extract=ExtractConfig(), model_schema=base.model_user_schema),
    }
    classify = ClassifyConfig(
        enabled=True, fallback_class="qa",
        classes=(ClassSpec(name="writing", description="写作协助类指令"),
                 ClassSpec(name="qa", description="知识问答类指令")))
    return replace(base, classify=classify, class_views=views, verify=gverify)


def test_verify_prompt_label_takes_class_effective_values():
    cfg = _classified_cfg()
    bundle = build_verify_prompt(_record(text="写一首诗"), {"intent": "写作"}, cfg,
                                 VerifyPromptOptions(label="writing"))
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
                                    VerifyPromptOptions(label="qa"))
    assert qa_bundle == bundle


def test_verify_prompt_ui_branch_head_uses_class_instruction():
    from pathlib import Path
    from labelkit.common.contracts.types import ImageRef, UINode, UITree

    nodes = (
        UINode("1", None, 0, "FrameLayout", "", "", (0, 0, 1080, 1920), True, {}),
        UINode("2", "1", 1, "Button", "登录", "", (72, 952, 1008, 1096), True, {}),
    )
    rec = Record(id="9" * 16, modality="ui", text=None, raw=None,
                 ui_tree=UITree(nodes),
                 image=ImageRef(path=Path("image_1.png"), format="png", size_bytes=1),
                 ref=RecordRef("a/uitree_1.jsonl", None, 1, ()))
    cfg = _classified_cfg()
    bundle = build_verify_prompt(rec, {"intent": "x"}, cfg,
                                 VerifyPromptOptions(label="writing"))
    assert CLASS_EXTRA in bundle.messages[0].parts[0].text
    head = bundle.messages[1].parts[0].text
    assert head == f"[任务指令] {CLASS_INSTRUCTION}\n[原始数据]\n[屏幕截图]"


def test_verdict_event_label_only_when_classify_enabled():
    # classify disabled: a label arg must NOT surface in the payload
    cfg = trace_cfg()
    metrics = _CapturingMetrics()
    ctx = SimpleNamespace(cfg=cfg, metrics=metrics, batch_no=1)
    VerifyStage(cfg)._emit_verdict_event(
        _VerdictEvent(record=_record(), verdict="pass", round_no=1, critiques=[C1],
                      judge=None, label="writing"), ctx)
    (_, _, _, _, payload) = metrics.events[0]
    assert "label" not in payload

    # classify enabled + labeled item → payload carries it (R5)
    cfg2 = _classified_cfg()
    metrics2 = _CapturingMetrics()
    ctx2 = SimpleNamespace(cfg=cfg2, metrics=metrics2, batch_no=1)
    VerifyStage(cfg2)._emit_verdict_event(
        _VerdictEvent(record=_record(), verdict="pass", round_no=1, critiques=[C1],
                      judge=None, label="writing"), ctx2)
    (_, _, _, _, payload2) = metrics2.events[0]
    assert payload2["label"] == "writing"


def test_classic_waves_thread_label_through_judges_and_repair(monkeypatch):
    """classic driver injects item.classification.label into both waves: every
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

    async def mock_complete_validated(judge, prompt, *, schema, scope):
        prompts.append(prompt)
        return (next(script), Usage(1, 1), 1, "m")

    engine = SimpleNamespace(complete_validated=mock_complete_validated)

    captured = {}

    async def fake_annotate_record(record, ctx, opts=None):
        captured["label"] = opts.label
        captured["repair"] = opts.repair
        return _annotation({"intent": "修正后"})

    monkeypatch.setattr(
        "labelkit.operators.annotate.annotate_record_leaf",
        fake_annotate_record,
    )

    metrics = _CapturingMetrics()
    ctx = _task_context(
        cfg=cfg, schema_engine=engine, metrics=metrics, batch_no=1,
    )
    asyncio.run(stage.run([item], ctx))

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
        llm_profiles={name: _budget_profile(name, 1000000) for name in {"judge", "default", *judges}},
        stitch=StitchConfig(),
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
    return PipelineItem(record=record, status=status, session_id=sid, session_position=record.ref.pair_index)


def _episode(members, *, sid="s1", eid="e" * 16, transitions=None,
             annotation=None, classification=None) -> PipelineItem:
    first = members[0]
    record = Record(id=eid, modality=first.modality, text=None, raw=None,
                    ui_tree=None, image=None,
                    ref=RecordRef(first.ref.source_file, first.ref.line_no,
                                  first.ref.pair_index, ()),
                    kind="sequence", members=tuple(members))
    return PipelineItem(record=record, session_id=sid, member_positions=tuple(range(len(members))),
                        capacity=SequenceCapacity(SequenceBounds(0, 10000)),
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
        self.user_schema_text = json.dumps(USER_SCHEMA, ensure_ascii=False)
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls: list = []              # (profile, prompt, schema, record_ids)
        self.scopes = []

    async def complete_validated(self, profile, prompt, schema=None, *, scope):
        record_ids = scope.record_ids
        self.calls.append((profile, prompt, schema, record_ids))
        self.scopes.append(scope)
        out = self.scripts[record_ids[0]].pop(0)
        if isinstance(out, Exception):
            raise out
        return out, Usage(), 1, "glm-5.2"


def _stub_judge_window(monkeypatch, relation="continues"):
    calls = []

    async def fake(frames, ctx, *, span, digests=None):
        from labelkit.operators.segment import _WindowVerdict

        calls.append([f.id for f in frames])
        return _WindowVerdict(
            span=span,
            member_ids=tuple(frame.id for frame in frames),
            verdicts=tuple(relation for _frame in frames),
            model="stub",
            reasons=(),
        )

    monkeypatch.setattr("labelkit.operators.segment._call_window", fake)
    return calls


def _stub_extract(monkeypatch):
    calls = []

    async def fake(prev, curr, index, ctx, label=None):
        from labelkit.operators.extract import _ExtractOutcome

        calls.append((prev.id, curr.id, index, label))
        transition = Transition(
            index=index,
            action={"action_type": "other", "target": None, "value": None,
                    "description": f"{prev.id}->{curr.id}"},
            model="stub",
            attempts=1,
            detail={},
        )
        return _ExtractOutcome(transition=transition, fallback=None)

    monkeypatch.setattr("labelkit.operators.extract._extract_transition_outcome", fake)
    return calls


def _stub_annotate(monkeypatch, output=None):
    calls = []

    async def fake(record, ctx, opts=None):        # v1.9 逐片段配额（T14）随 opts 到
        calls.append(SimpleNamespace(record=record, repair=opts.repair,
                                     label=opts.label,
                                     transitions=opts.transitions,
                                     temporal_context=opts.temporal_context))
        return _annotation(output or {"task_label": "修正"})

    monkeypatch.setattr("labelkit.operators.annotate.annotate_record_leaf", fake)
    return calls


def _run_verify(cfg, batch, engine):
    _stamp_stream(batch, engine)
    metrics = _CapturingMetrics()
    ctx = _task_context(
        cfg=cfg, llm=None, schema_engine=engine,
        metrics=metrics, rng=None, batch_no=1,
    )
    out = asyncio.run(VerifyStage(cfg).run(batch, ctx))
    assert out is batch                    # stage contract: same list object
    return metrics


def _stamp_stream(batch, engine=None):
    """给历史内容夹具补上测试输入声明的明确出现位置。"""
    by_session = {}
    for item in batch:
        if item.record.kind == "single":
            frames = by_session.setdefault(item.session_id, [])
            item.session_position = len(frames)
            frames.append(item)
    record_positions = {id(frame.record): frame.session_position for frames in by_session.values() for frame in frames}
    content_positions = {frame.record.id: frame.session_position for frames in by_session.values() for frame in frames}
    for item in batch:
        if item.record.kind != "sequence":
            continue
        item.member_positions = tuple(record_positions.get(id(member), index)
                                      for index, member in enumerate(item.record.members))
        if item.capacity is None:
            item.capacity = SequenceCapacity(SequenceBounds(0, 10000))
        for name in ("member_classifications", "member_annotations"):
            values = getattr(item, name)
            if values is not None:
                mapped = {content_positions.get(key, key): value for key, value in values.items()}
                values.clear()
                values.update(mapped)
        if hasattr(item, "stitch_fragments"):
            start = 0
            fragments = []
            for fragment in item.stitch_fragments:
                stop = start + fragment["member_count"]
                positions = fragment.get("member_positions", list(item.member_positions[start:stop]))
                fragments.append({**fragment, "member_positions": positions})
                start = stop
            item.stitch_fragments = tuple(fragments)
    if engine is not None and hasattr(engine, "scripts"):
        for outcomes in engine.scripts.values():
            for outcome in outcomes:
                if isinstance(outcome, dict):
                    for defect in outcome.get("defects", []):
                        if defect.get("members") is not None:
                            defect["members"] = [content_positions.get(member, member) for member in defect["members"]]


# ── sequence review prompt: system text + six-section user order (§10.5) ────

def test_sequence_system_text_verbatim_without_extra_criteria():
    assert verify_sequence_system_text("") == (
        "你是标注质量审核员。给定任务指令、完整成员证据、动作序列与边界余量，独立判断该序列\n"
        "（episode）的标注是否合格。\n"
        "评审维度: ① 是否遵循任务指令 ② 与完整成员及动作序列证据的事实一致性 ③ 字段语义是否正确填写\n"
        "④ 段边界与成员构成是否成立（对照下列缺陷类型）\n"
        "缺陷类型（发现即列入 defects，可为空数组）:\n"
        "- label_mismatch: 标注的任务标签与序列证据不符\n"
        "- off_task_members: 段内混入与任务无关的成员帧（members 列出这些成员出现位置）\n"
        "- missing_head: 段首缺少任务起点帧（结合边界余量判断）\n"
        "- missing_tail: 段尾缺少任务终点帧（结合边界余量判断）\n"
        "- missing_members: 段中缺失成员帧（members 列出可指认的帧出现位置，无从指认则为 null）\n"
        "- wrong_stitch: 线索缝合错误——各碎片并非同一任务的延续（结合片段结构判断）\n"
        "先逐维度给出简短意见，再列缺陷表，最后给结论。\n"
        "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
        '{"critiques": [{"aspect": <维度>, "opinion": <一句话意见>}, ...],\n'
        ' "defects": [{"kind": <缺陷类型>, "members": <非负整数出现位置数组|null>,\n'
        '              "position": <位置说明|null>, "detail": <一句话>}, ...],\n'
        ' "verdict": "pass"|"fail"}'
    )


def test_sequence_system_text_extra_criteria_line_position():
    text = verify_sequence_system_text("⑤ 领域合规")
    assert ("④ 段边界与成员构成是否成立（对照下列缺陷类型）\n"
            "⑤ 领域合规\n"
            "缺陷类型（发现即列入 defects，可为空数组）:") in text


def test_sequence_prompt_complete_members_images_and_transitions():
    cfg = _stream_cfg()
    members = [_ui_frame(f"f{i}", f"完整帧证据 {i}") for i in range(3)]
    episode = _episode(members)
    steps = (_transition(0, description="点击登录"), _transition(1, description="向下滚动"))
    options = VerifyPromptOptions(member_positions=(2, 4, 7), transitions=steps, boundary_margin="邻帧")
    prompt = build_verify_prompt(episode.record, {"task_label": "外卖"}, cfg, options)
    parts = prompt.messages[1].parts
    assert [part.image for part in parts if part.kind == "image"] == [member.image for member in members]
    text = "\n".join(part.text for part in parts if part.kind == "text")
    assert all(f"[成员出现位置 {position}]" in text for position in (2, 4, 7))
    assert all(f"完整帧证据 {index}" in text for index in range(3))
    assert all(json.dumps(dict(step.action), ensure_ascii=False) in text for step in steps)
    assert parts[-1].text == '[标注结果] {"task_label": "外卖"}'


def test_sequence_prompt_action_section_omitted_when_transitions_none():
    episode = _episode([_frame("f0"), _frame("f1")])
    prompt = build_verify_prompt(episode.record, {}, _stream_cfg(),
                                VerifyPromptOptions(member_positions=(0, 1), boundary_margin="边界"))
    parts = prompt.messages[1].parts
    assert len([part for part in parts if part.kind == "image"]) == 2
    assert not any("[动作序列]" in part.text for part in parts if part.kind == "text")


def test_sequence_step_line_frozen_format():
    assert sequence_step_line(_transition(
        3, action_type="input_text", target="搜索框", value="奶茶",
        description="输入关键词")) == "3. input_text（对象: 搜索框；值: 奶茶）输入关键词"
    assert sequence_step_line(_transition(
        0, action_type="navigate_back", target=None, value=None,
        description="返回")) == "0. navigate_back（对象: —；值: —）返回"


# ── v1.9 stitch adaptation (T14/T15): seam suffix + [片段结构] + wrong_stitch ─

def test_sequence_step_line_thread_seam_suffix():
    """T14 (deliberate v1.9 revision of the no-suffix rule): thread-seam
    placeholder steps carry the 「（线索接缝：被 X 打断）」 suffix; the S16
    extraction-fallback marker still never appears in review evidence."""
    seam = _transition(2, action_type="app_switch", target=None, value=None,
                       description="线索接缝：被打车打断后恢复",
                       detail={"kind": "thread_seam", "interrupted_by": ["打车"]})
    assert sequence_step_line(seam) == (
        "2. app_switch（对象: —；值: —）线索接缝：被打车打断后恢复（线索接缝：被打车打断）")
    fallback = _transition(1, action_type="other", target=None, value=None,
                           description="", detail={"kind": "extraction_invalid",
                                                   "message": "L3 耗尽"})
    assert sequence_step_line(fallback) == "1. other（对象: —；值: —）"


def test_defect_kinds_six_values_with_wrong_stitch():
    assert DEFECT_KINDS == ("label_mismatch", "off_task_members", "missing_head",
                            "missing_tail", "missing_members", "wrong_stitch")


def test_fragment_structure_text_fragments_and_seam_table():
    episode = _episode([_frame(f"f{i}") for i in range(4)])
    episode.member_positions = (0, 2, 4, 6)
    episode.stitch_fragments = ({"member_positions": [0, 4]}, {"member_positions": [2, 6]})
    episode.seam_indexes = (1,)
    episode.seam_interrupted_by = (("打车",),)
    assert fragment_structure_text(episode) == (
        "碎片 1/2: 成员出现位置 [0, 4]（2 帧）\n"
        "碎片 2/2: 成员出现位置 [2, 6]（2 帧）\n接缝位置: 步 1（被打车打断）")
    plain = _episode([_frame("a"), _frame("b")])
    assert fragment_structure_text(plain) == "碎片 1/1: 成员出现位置 [0, 1]（2 帧）\n接缝位置: 无"


def test_sequence_prompt_includes_optional_fragment_structure_after_steps():
    episode = _episode([_frame("f0"), _frame("f1")])
    options = VerifyPromptOptions(member_positions=(0, 1), transitions=(_transition(0),),
                                  boundary_margin="边界", fragment_structure="碎片结构")
    prompt = build_verify_prompt(episode.record, {}, _stream_cfg(), options)
    text = "\n".join(part.text for part in prompt.messages[1].parts if part.kind == "text")
    assert text.index("[动作序列]") < text.index("[片段结构]") < text.index("[边界余量]")
    without = build_verify_prompt(episode.record, {}, _stream_cfg(), replace(options, fragment_structure=""))
    assert not any("[片段结构]" in part.text for part in without.messages[1].parts if part.kind == "text")


def test_session_episodes_ordinals_skip_stitched_shells():
    """T15 (major-5): _session_episodes filters stitched shells — the boundary
    margin's "第 n 段" ordinals must not be polluted by a shell's stale members."""
    f0, f1, f2, f3 = (_ui_frame("f0", "首页"), _ui_frame("f1", "下单"),
                      _ui_frame("f2", "搜索"), _ui_frame("f3", "支付"))
    e0, e1, e2, e3 = _env(f0), _env(f1), _env(f2), _env(f3)
    shell = _episode([f0, f1], eid="a" * 16)
    shell.status = "stitched"                          # merged-away shell
    thread = _episode([f0, f1], eid="b" * 16)          # the surviving thread
    under_review = _episode([f2, f3], eid="c" * 16)
    batch = [e0, e1, e2, e3, shell, thread, under_review]
    _stamp_stream(batch)
    text = boundary_margin_text(under_review, batch)
    # without the filter the review target would render as 第 3 段's neighbor
    # (shell counted); with it, f1's fate reads 第 1 段 (= the thread)
    assert f"段首前 1: 出现位置 1: {f1.ui_tree.serialize(None)}（去向: 第 1 段）" in text


def test_wrong_stitch_routes_mark_only_and_fail_stands(monkeypatch):
    """T15: wrong_stitch is an INDEPENDENT mark-only branch — no reclaim scan,
    no reannotation, no boundary_flags/membership counters; the fail verdict
    stands (dropped_verify) and the defect stays in the table."""
    cfg = _stream_cfg()
    jw_calls = _stub_judge_window(monkeypatch)
    annotate_calls = _stub_annotate(monkeypatch)
    f0, f1 = _frame("f0"), _frame("f1")
    ep = _episode([f0, f1], transitions=(_transition(0),))
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[_defect("wrong_stitch",
                                          detail="两碎片非同一任务")]),
    ]})
    metrics = _run_verify(cfg, [_env(f0), _env(f1), ep], engine)
    assert jw_calls == [] and annotate_calls == []     # never entered any repair
    assert ep.status == "dropped_verify"
    (d,) = ep.verification.defects
    assert d["kind"] == "wrong_stitch" and "suspected" not in d
    assert [m.id for m in ep.record.members] == ["f0", "f1"]   # no unstitching
    assert "verify.boundary_flags" not in metrics.counters
    assert "verify.membership_repairs" not in metrics.counters
    assert metrics.counters["verify.defects.wrong_stitch"] == 1


def test_stream_driver_threads_fragment_structure_and_quota(monkeypatch):
    """T14/T15 穿参义务: with stitch enabled the review prompt carries the
    [片段结构] section rendered from the M16 duck marks, and the repair
    re-annotation call receives fragment_lens."""
    cfg = replace(_stream_cfg(), stitch=StitchConfig(enabled=True))
    annotate_calls = _stub_annotate(monkeypatch)
    f0, f1, f2 = _frame("f0"), _frame("f1"), _frame("f2")
    ep = _episode([f0, f1, f2], transitions=(_transition(0), _transition(1)))
    ep.thread_id = ep.record.id
    ep.stitch_fragments = (
        {"order_span": [0, 1], "member_count": 2, "cause": "origin",
         "source_episode": ep.record.id},
        {"order_span": [4, 4], "member_count": 1, "cause": "resumed",
         "source_episode": "f" * 16},
    )
    ep.seam_indexes = (1,)
    ep.seam_interrupted_by = (("打车",),)
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[_defect("label_mismatch")]),
        _seq_obj("pass"),
    ]})
    _run_verify(cfg, [_env(f0), _env(f1), _env(f2), ep], engine)
    first_prompt = engine.calls[0][1]
    structure_parts = [p.text for p in first_prompt.messages[1].parts
                       if p.kind == "text" and p.text.startswith("[片段结构]")]
    assert len(structure_parts) == 1
    assert "碎片 1/2: 成员出现位置 [0, 1]（2 帧）" in structure_parts[0]
    assert "接缝位置: 步 1（被打车打断）" in structure_parts[0]
    (call,) = annotate_calls
    assert ep.status == "active"


# ── boundary margin: three fate states (spec 3.7.2) ─────────────────────────

def test_boundary_margin_three_fate_states():
    f0, f1, f2, f3 = (_ui_frame("f0", "首页"), _ui_frame("f1", "弹窗"),
                      _ui_frame("f2", "搜索"), _ui_frame("f3", "下单"))
    e0, e1, e2, e3 = _env(f0), _env(f1, status="dropped_noise"), _env(f2), _env(f3)
    e1.noise_attribution = ("segment", "noise")
    ep1 = _episode([f0], eid="a" * 16)             # 第 1 段 (batch order)
    ep2 = _episode([f2, f3], eid="b" * 16)         # under review = 第 2 段
    batch = [e0, e1, e2, e3, ep1, ep2]
    _stamp_stream(batch)
    text = boundary_margin_text(ep2, batch)
    assert text == (
        f"段首前 2: 出现位置 0: {f0.ui_tree.serialize(None)}（去向: 第 1 段）\n"
        f"段首前 1: 出现位置 1: {f1.ui_tree.serialize(None)}（去向: noise）\n"
        "段尾后 1: 无\n"
        "段尾后 2: 无"
    )


def test_boundary_margin_frame_with_no_fate_renders_none():
    f0, f1 = _ui_frame("f0", "残留"), _ui_frame("f1", "搜索")
    e0 = _env(f0, status="failed")                 # exists, neither noise nor member
    e1 = _env(f1)
    ep = _episode([f1], eid="c" * 16)
    _stamp_stream([e0, e1, ep])
    text = boundary_margin_text(ep, [e0, e1, ep])
    assert text == (
        "段首前 2: 无\n"
        f"段首前 1: 出现位置 0: {f0.ui_tree.serialize(None)}（去向: 无）\n"
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


def _stub_reseam_failure(monkeypatch):
    """让真实 stream repair 驱动在 reseam 叶稳定返回 ordinary 失败。"""
    calls = []

    async def fail(prev, curr, index, ctx, label=None):
        calls.append((prev.id, curr.id, index, label))
        raise ProviderRetryableError("reseam failed", "default", 5)

    monkeypatch.setattr(
        "labelkit.operators.extract._extract_transition_outcome", fail,
    )
    return calls


def test_reseam_failure_rolls_back_reclaim_envelope_atomically(monkeypatch):
    """回收已通过但 reseam 失败时，候选信封与 repair counter 一并回到尝试前。"""
    cfg = _stream_cfg()
    _stub_judge_window(monkeypatch, relation="continues")
    reseam_calls = _stub_reseam_failure(monkeypatch)
    first, second, candidate = _frame("f0"), _frame("f1"), _frame("f2")
    noise = _env(candidate, status="dropped_noise")
    noise.noise_attribution = ("segment", "noise")
    original_transition = _transition(0)
    episode = _episode([first, second], transitions=(original_transition,))
    engine = SeqJudgeEngine({episode.record.id: [
        _seq_obj("fail", defects=[_defect("missing_tail")]),
    ]})

    metrics = _run_verify(
        cfg, [_env(first), _env(second), noise, episode], engine,
    )

    assert reseam_calls == [("f1", "f2", 1, None)]
    assert episode.status == "failed"
    assert [member.id for member in episode.record.members] == ["f0", "f1"]
    assert episode.transitions == (original_transition,)
    assert noise.status == "dropped_noise"
    assert noise.noise_attribution == ("segment", "noise")
    assert "verify.membership_repairs" not in metrics.counters


def test_reseam_failure_rolls_back_shrink_envelope_atomically(monkeypatch):
    """收缩已路由但 reseam 失败时，成员信封状态与动态归因均不泄漏。"""
    cfg = _stream_cfg()
    reseam_calls = _stub_reseam_failure(monkeypatch)
    first, removed, last = _frame("f0"), _frame("f1"), _frame("f2")
    first_env, removed_env, last_env = _env(first), _env(removed), _env(last)
    original_transitions = (_transition(0), _transition(1))
    episode = _episode(
        [first, removed, last], transitions=original_transitions,
    )
    engine = SeqJudgeEngine({episode.record.id: [
        _seq_obj(
            "fail",
            defects=[_defect("off_task_members", members=["f1"])],
        ),
    ]})

    metrics = _run_verify(
        cfg, [first_env, removed_env, last_env, episode], engine,
    )

    assert reseam_calls == [("f0", "f2", 0, None)]
    assert episode.status == "failed"
    assert [member.id for member in episode.record.members] == ["f0", "f1", "f2"]
    assert episode.transitions == original_transitions
    assert removed_env.status == "absorbed"
    assert not hasattr(removed_env, "noise_attribution")
    assert "verify.membership_repairs" not in metrics.counters


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


def _reclaim_overflow_run(monkeypatch, origin):
    """One reclaim round whose judge_window call raises a reactive
    ContextOverflowError of the given origin; returns (noise env, metrics)."""
    cfg = _stream_cfg()

    class FeedMetrics(_CapturingMetrics):
        def __init__(self):
            super().__init__()
            self.fed = []

        def record_provider_result(self, fatal, *, hard=False):
            self.fed.append(fatal)

    async def overflowing_judge(frames, ctx, *, span, digests=None):
        raise ContextOverflowError("prompt is too long", phase="reactive",
                                   profile="default", origin=origin)

    monkeypatch.setattr("labelkit.operators.segment._call_window",
                        overflowing_judge)
    _stub_annotate(monkeypatch)
    f0, f1, f2 = _frame("f0"), _frame("f1"), _frame("f2")
    e2 = _env(f2, status="dropped_noise")
    e2.noise_attribution = ("segment", "noise")
    ep = _episode([f0, f1], transitions=(_transition(0),))
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[_defect("missing_tail")]),
    ]})
    metrics = FeedMetrics()
    ctx = _task_context(
        cfg=cfg, llm=None, schema_engine=engine,
        metrics=metrics, rng=None, batch_no=1,
    )
    from labelkit.common.errors import SessionCapacityError
    batch = [_env(f0), _env(f1), e2, ep]
    _stamp_stream(batch, engine)
    with pytest.raises(SessionCapacityError) as raised:
        asyncio.run(VerifyStage(cfg).run(batch, ctx))
    assert raised.value.failures[0].stage == "verify"
    assert raised.value.failures[0].unit == "transition"
    assert ep.member_positions == (0, 1)
    return e2, metrics


def test_reclaim_rejudgment_reactive_400_feeds_breaker_exactly_once(monkeypatch):
    """A7 blind-spot fix: the reclaim re-judgment swallow (mark-only
    degradation) is the exception's terminal — the 400-sniffed reactive
    overflow settles its exactly-once breaker feed there, while the
    record-level disposition stays mark-only (never fails the episode)."""
    e2, metrics = _reclaim_overflow_run(monkeypatch, "http_400")
    assert e2.status == "dropped_noise"            # mark-only, frame untouched
    assert "verify.boundary_flags" not in metrics.counters
    assert metrics.fed == []                   # fed exactly once


def test_reclaim_rejudgment_finish_origin_never_feeds(monkeypatch):
    """The 200-shaped oracle rode a successful HTTP interaction — the reclaim
    swallow must NOT feed the breaker (§7.8 matrix), only mark the flag."""
    e2, metrics = _reclaim_overflow_run(monkeypatch, "finish")
    assert e2.status == "dropped_noise"
    assert "verify.boundary_flags" not in metrics.counters
    assert metrics.fed == []


# ── mark-only downgrades: session_split / neighbor-held / capture_gap ───────

def test_capacity_boundary_suspicion_does_not_reclaim_or_fail(monkeypatch):
    cfg = _stream_cfg()
    calls = _stub_judge_window(monkeypatch)
    frames = [_env(_frame(f"f{i}")) for i in range(3)]
    frames[2].status = "dropped_noise"
    episode = _episode([frames[0].record, frames[1].record])
    cut = CapacityCut(1, 2, "annotate", "default", "precheck")
    episode.capacity = SequenceCapacity(SequenceBounds(0, 2, after=cut), sealed=True)
    engine = SeqJudgeEngine({episode.record.id: [_seq_obj("fail", defects=[_defect("missing_tail")])]})
    metrics = _run_verify(cfg, [*frames, episode], engine)
    assert episode.status == "active" and episode.verification.verdict == "pass"
    assert episode.verification.defects[0]["suspected"] == "capacity"
    assert calls == [] and frames[2].status == "dropped_noise"
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


# ── missing_members: the INTERIOR reclaim scan (_interior_claim) ────────────

def _noise_env(record, *, sid="s1", stage="segment", reason="noise"):
    """段内噪声池里的一帧（可回收候选的默认形态）。

    @param record 帧记录
    @param sid 会话 id
    @param stage 噪声归因阶段（"verify" = 自弃帧，永不回收）
    @param reason 噪声归因原因
    @return dropped_noise 状态的帧信封
    """
    env = _env(record, sid=sid, status="dropped_noise")
    env.noise_attribution = (stage, reason)
    return env


def test_missing_members_interior_reclaim_claims_the_first_qualifying_frame(
        monkeypatch):
    # spec 3.7.3 回收三级判定的 missing_members 支：候选取「段首末成员之间」的首个
    # 内部噪声帧；复裁窗为 §3.7.3② 静态保证的固定三帧 [前成员, 候选, 后成员]。
    cfg = _stream_cfg(extract_enabled=False)
    jw_calls = _stub_judge_window(monkeypatch, relation="continues")
    annotate_calls = _stub_annotate(monkeypatch)
    f0, f1, fn, f2, f3 = (_frame("f0"), _frame("f1"), _frame("fn"),
                          _frame("f2"), _frame("f3"))
    en = _noise_env(fn)                                # 段内部的可回收噪声帧
    ep = _episode([f0, f1, f2, f3])                    # 四个成员夹住 fn
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[_defect("missing_members")]),
        _seq_obj("pass"),
    ]})
    metrics = _run_verify(
        cfg, [_env(f0), _env(f1), en, _env(f2), _env(f3), ep], engine)

    # 复裁窗恒为三帧、且取候选的**紧邻**成员（不是段首末）
    assert jw_calls == [["f1", "fn", "f2"]]
    assert en.status == "absorbed"                     # dropped_noise → absorbed
    assert [m.id for m in ep.record.members] == ["f0", "f1", "fn", "f2", "f3"]
    assert ep.stream_repaired is True
    assert len(annotate_calls) == 1
    assert ep.status == "active"
    assert (ep.verification.verdict, ep.verification.rounds) == ("pass", 2)
    assert metrics.counters["verify.membership_repairs"] == 1
    assert "verify.boundary_flags" not in metrics.counters


def test_missing_members_interior_named_filter_skips_unnamed_frames(monkeypatch):
    # defect.members 点名时，内部扫描只回收被点名的帧——未点名的可回收帧原样留在
    # 噪声池（judge 的指认是收窄条件，不是提示）。
    cfg = _stream_cfg(extract_enabled=False)
    jw_calls = _stub_judge_window(monkeypatch, relation="advances")
    _stub_annotate(monkeypatch)
    f0, f1, f2, f3 = _frame("f0"), _frame("f1"), _frame("f2"), _frame("f3")
    e1, e2 = _noise_env(f1), _noise_env(f2)            # 两个都可回收
    ep = _episode([f0, f3])
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[_defect("missing_members", members=["f2"])]),
        _seq_obj("pass"),
    ]})
    metrics = _run_verify(cfg, [_env(f0), e1, e2, _env(f3), ep], engine)

    assert jw_calls == [["f0", "f2", "f3"]]            # 跳过未点名的 f1
    assert e1.status == "dropped_noise"                # 未点名 ⇒ 不动
    assert e2.status == "absorbed"
    assert [m.id for m in ep.record.members] == ["f0", "f2", "f3"]
    assert metrics.counters["verify.membership_repairs"] == 1


def test_missing_members_interior_contention_marks_neighbor(monkeypatch):
    # 二级判定：段内部的唯一候选已被本轮更早的 episode（批位序在先）预定 ⇒ 标
    # "neighbor"（只计 boundary_flags），绝不标 suspected="capture_gap"——帧确实
    # 存在，只是被邻段拿走了（D5）。
    cfg = _stream_cfg(extract_enabled=False)
    jw_calls = _stub_judge_window(monkeypatch, relation="continues")
    _stub_annotate(monkeypatch)
    f0, f1, fn, f2, f3 = (_frame("f0"), _frame("f1"), _frame("fn"),
                          _frame("f2"), _frame("f3"))
    en = _noise_env(fn)                                # 被两段同时觊觎的噪声帧
    early = _episode([f1, f2], eid="a" * 16)           # 位序 1..3，内部含 fn(2)
    late = _episode([f0, f3], eid="b" * 16)            # 位序 0..4，内部也含 fn
    batch = [_env(f0), _env(f1), en, _env(f2), _env(f3), early, late]
    engine = SeqJudgeEngine({
        early.record.id: [_seq_obj("fail", defects=[_defect("missing_members")]),
                          _seq_obj("pass")],
        late.record.id: [_seq_obj("fail", defects=[_defect("missing_members")])],
    })
    metrics = _run_verify(cfg, batch, engine)

    assert jw_calls == [["f1", "fn", "f2"]]            # 只有 early 复裁了一次
    assert en.status == "absorbed"
    assert [m.id for m in early.record.members] == ["f1", "fn", "f2"]
    assert early.status == "active"
    # late 输掉争用：仅标记，缺陷条目上没有 suspected 键
    assert [m.id for m in late.record.members] == ["f0", "f3"]
    assert late.status == "dropped_verify"
    (d,) = late.verification.defects
    assert d["kind"] == "missing_members" and "suspected" not in d
    assert metrics.counters["verify.boundary_flags"] == 1
    assert metrics.counters["verify.membership_repairs"] == 1


def test_missing_members_interior_without_candidates_returns_none(monkeypatch):
    # 三级判定：段内部无任何可回收候选（中间那帧是 verify 自己弃掉的，收缩↔回收
    # 乒乓护栏）⇒ 无处可寻 ⇒ 缺陷条目增 suspected="capture_gap"。
    cfg = _stream_cfg(extract_enabled=False)
    jw_calls = _stub_judge_window(monkeypatch)
    _stub_annotate(monkeypatch)
    f0, f1, f2 = _frame("f0"), _frame("f1"), _frame("f2")
    e1 = _noise_env(f1, stage="verify", reason="off_task_member")
    ep = _episode([f0, f2])
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[_defect("missing_members")]),
    ]})
    metrics = _run_verify(cfg, [_env(f0), e1, _env(f2), ep], engine)

    assert jw_calls == []                              # 无候选 ⇒ 零复裁调用
    assert e1.status == "dropped_noise"
    assert [m.id for m in ep.record.members] == ["f0", "f2"]
    (d,) = ep.verification.defects
    assert d["suspected"] == "capture_gap"
    assert metrics.counters["verify.boundary_flags"] == 1
    assert "verify.membership_repairs" not in metrics.counters


def test_missing_members_with_a_single_member_has_no_interior(monkeypatch):
    # 边界形态：段只有一个成员 ⇒ head == tail ⇒ 内部扫描区间为空，即便相邻位置
    # 存在可回收噪声帧也不越界回收（missing_members 只管段内部，边缘归 _edge_claim）。
    cfg = _stream_cfg(extract_enabled=False)
    jw_calls = _stub_judge_window(monkeypatch, relation="continues")
    _stub_annotate(monkeypatch)
    f0, f1 = _frame("f0"), _frame("f1")
    e1 = _noise_env(f1)
    ep = _episode([f0])
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[_defect("missing_members")]),
    ]})
    metrics = _run_verify(cfg, [_env(f0), e1, ep], engine)

    assert jw_calls == []
    assert e1.status == "dropped_noise"
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
                             verify=base.verify, extract=base.extract,
                             model_schema=base.model_user_schema)
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


# ── v1.11 context-budget packing (spec 3.7.2/3.7.3 v1.11 rows, V25②③/V21/V27①) ─

def _budget_profile(name: str, context_window: int, *, default_image_px: int = 0,
                    max_image_px: int = 2048, supports_structured_output=False):
    from labelkit.common.config.model import LLMProfile
    return LLMProfile(name=name, provider="openai_compatible", base_url="http://x",
                      model="m", api_key_env="K", max_output_tokens=256,
                      context_window=context_window,
                      default_image_px=default_image_px,
                      max_image_px=max_image_px,
                      supports_structured_output=supports_structured_output)


class _FixedCalibrator:
    def __init__(self, value: int):
        self.value = value

    def cost(self, profile: str) -> int:
        return self.value


def _ui_single_record(n_nodes: int = 80) -> Record:
    nodes = [UINode("1", None, 0, "FrameLayout", "", "", (0, 0, 1080, 1920), True, {})]
    nodes += [UINode(str(i + 2), "1", 1, "TextView", "文本行" + "字" * 15, "",
                     (0, i, 1080, i + 10), True, {}) for i in range(n_nodes)]
    return Record(id="9" * 16, modality="ui", text=None, raw=None,
                  ui_tree=UITree(tuple(nodes)),
                  image=ImageRef(path=Path("shot.png"), format="png", size_bytes=1),
                  ref=RecordRef("b/uitree_2.jsonl", None, 2, ()))


def test_panel_fit_min_over_declared_judges():
    cfg = replace(trace_cfg(enabled=False),
                  verify=VerifyConfig(enabled=True, llm="judge",
                                      judges=("j1", "j2", "j3")),
                  llm_profiles={"j1": _budget_profile("j1", 4096),
                                "j2": _budget_profile("j2", 2000),
                                "j3": _budget_profile("j3", 0)})   # undeclared
    stage = VerifyStage(cfg)
    ctx = SimpleNamespace(cfg=cfg, llm=SimpleNamespace(calibrator=_FixedCalibrator(70)))
    fit = stage._panel_fit(ctx, _record(), VERDICT_SCHEMA)
    # min over DECLARED budgets only (V25②); text record → no image cost
    assert fit.input_budget == budget_mod.input_budget(_budget_profile("j2", 2000))
    assert fit.image_cost == 0
    # a UI record pulls the MAX calibrated cost across the declared panel
    fit_ui = stage._panel_fit(ctx, _ui_single_record(), VERDICT_SCHEMA)
    assert fit_ui.image_cost == 70
    # all-undeclared panel → budget OFF
    off = replace(cfg, llm_profiles={"j1": _budget_profile("j1", 0)})
    assert VerifyStage(off)._panel_fit(ctx, _record(), VERDICT_SCHEMA) is None


def test_build_verify_prompt_ui_tree_dynamic_cap_and_frozen_off_path():
    rec = _ui_single_record()
    cfg = trace_cfg(enabled=False)
    fit = _PromptFit(input_budget=900, image_cost=100)
    bundle = build_verify_prompt(rec, {"intent": "x"}, cfg,
                                 VerifyPromptOptions(fit=fit))
    assert not fit.overflow and fit.truncations == 1
    tail = bundle.messages[1].parts[2].text
    tree_lines = tail.split("\n")
    marker_lines = [line for line in tree_lines if line.startswith("…(truncated ")
                    and line.endswith(" nodes)")]
    assert marker_lines                                    # §3.3③ marker in place
    assert tree_lines[-1].startswith("[标注结果] ")         # V25③ JSON kept verbatim
    # deterministic rerun
    again = build_verify_prompt(
        rec, {"intent": "x"}, cfg,
        VerifyPromptOptions(fit=_PromptFit(input_budget=900, image_cost=100)))
    assert again == bundle
    # fit=None (budget off) is byte-identical to the pre-v1.11 build
    plain = build_verify_prompt(rec, {"intent": "x"}, cfg)
    assert plain == build_verify_prompt(rec, {"intent": "x"}, cfg,
                                        VerifyPromptOptions(fit=None))
    assert rec.ui_tree.serialize(max_chars=cfg.input.ui_tree_max_chars) in \
        plain.messages[1].parts[2].text


def test_sequence_step_block_preserves_every_step_under_budget_pressure():
    episode = _episode([_frame("a"), _frame("b")])
    steps = tuple(_transition(index, description=f"step-{index}-" + "长证据" * 100) for index in range(12))
    fit = _PromptFit(input_budget=1, image_cost=100)
    prompt = build_verify_prompt(episode.record, {"task_label": "外卖"}, _stream_cfg(),
                                VerifyPromptOptions(member_positions=(0, 1), transitions=steps, fit=fit))
    text = "\n".join(part.text for part in prompt.messages[1].parts if part.kind == "text")
    assert all(f"step-{index}-" in text for index in range(12))
    assert "truncated" not in text and fit.truncations == 0


def test_minimal_unit_unfittable_rejects_record_no_call():
    # V10 via _settle_fit: the request is never sent, the record fails with
    # kind=context_overflow and budget.overflow_records counts the reject.
    cfg = replace(trace_cfg(enabled=False),
                  verify=VerifyConfig(enabled=True, llm="judge"),
                  llm_profiles={"judge": _budget_profile("judge", 600)})
    item = PipelineItem(record=_record(text="长" * 900), annotation=_annotation())

    class Exploding:
        async def complete_validated(self, *a, **k):
            raise AssertionError("must not be called")

    metrics = _CapturingMetrics()
    ctx = _task_context(
        cfg=cfg, llm=SimpleNamespace(calibrator=_FixedCalibrator(1)),
        schema_engine=Exploding(), metrics=metrics, rng=None, batch_no=1,
    )
    asyncio.run(VerifyStage(cfg).run([item], ctx))
    assert item.status == "failed"
    assert item.errors[0].kind == "context_overflow"
    assert metrics.counters["budget.overflow_records"] == 1


def test_classify_error_budget_vocabulary_first_and_stage_disposition():
    assert _classify_error(ContextOverflowError("x", phase="precheck"), "text") == (
        "context_overflow", False)
    assert _classify_error(OutputTruncatedError("x"), "text") == (
        "output_truncated", False)

    class FeedMetrics(_CapturingMetrics):
        def __init__(self):
            super().__init__()
            self.fed = []

        def record_provider_result(self, fatal, *, hard=False):
            self.fed.append(fatal)

    cfg = trace_cfg(enabled=False)                 # budget off — finish oracle only

    class Overflowing:
        async def complete_validated(self, *a, **k):
            exc = ContextOverflowError("sniff", phase="reactive")
            raise exc

    item = PipelineItem(record=_record(), annotation=_annotation())
    metrics = FeedMetrics()
    ctx = _task_context(
        cfg=cfg, llm=None, schema_engine=Overflowing(),
        metrics=metrics, rng=None, batch_no=1,
    )
    asyncio.run(VerifyStage(cfg).run([item], ctx))
    assert item.status == "failed"
    assert item.errors[0].kind == "context_overflow"
    assert metrics.counters["budget.overflow_records"] == 1
    assert metrics.fed == [True]                   # reactive-400 terminal: fed once


# ── V21 repair-ladder (spec 3.7.3 修复路径与上下文预算的交互 ①) ───────────────

FRAME_INSTRUCTION = "标注该帧的意图。"


def _frame_views(*, skip_chitchat=True):
    """两个帧类视图：task_request 正常标注；chitchat 跳过（enabled=false）。"""
    return {
        "task_request": FrameClassView(instruction=FRAME_INSTRUCTION,
                                       examples=(), enabled=True),
        "chitchat": FrameClassView(instruction=FRAME_INSTRUCTION,
                                   examples=(), enabled=not skip_chitchat),
    }


def _frame_stream_cfg(base=None, *, classify_on=True, annotate_on=True,
                      **kw) -> ResolvedConfig:
    """_stream_cfg（或显式 base）+ 帧粒度配置：帧类表 task_request/chitchat，
    chitchat 视图跳过标注；对应开关关闭时保持默认（enabled=false、views 空、
    frame_schema None）。"""
    base = base if base is not None else _stream_cfg(**kw)
    frame_classify = FrameClassifyConfig(
        enabled=True, llm="default", fallback_class="task_request",
        classes=(ClassSpec(name="task_request", description="任务请求帧"),
                 ClassSpec(name="chitchat", description="闲聊帧")),
    ) if classify_on else FrameClassifyConfig()
    frame_annotate = FrameAnnotateConfig(
        enabled=True, llm="default", instruction=FRAME_INSTRUCTION,
        schema_inline='{"type": "object"}',
    ) if annotate_on else FrameAnnotateConfig()
    return replace(base, frame_classify=frame_classify,
                   frame_annotate=frame_annotate,
                   frame_class_views=_frame_views() if classify_on else {},
                   frame_schema={"type": "object"} if annotate_on else None,
                   model_frame_schema={"type": "object"} if annotate_on else None)


def _member_cls(label="task_request") -> Classification:
    return Classification(label=label, labels=(label,), source="llm", detail={})


def _stub_classify_frames(monkeypatch, label="task_request"):
    """帧分类纯窗口桩：记录每次调用的成员 id 列表，全员判给定帧类。"""
    calls = []

    async def fake(plan, span, ctx):
        from labelkit.operators.classify import _FrameWindowOutcome

        members = plan.members[span[0]:span[1]]
        calls.append([member.id for member in members])
        return _FrameWindowOutcome(
            leaves=((span, [label for _member in members]),),
            calls=1,
            degrade_retries=0,
        )

    monkeypatch.setattr("labelkit.operators.classify._run_frame_plan", fake)
    return calls


def _stub_annotate_member(monkeypatch, *, fail=False):
    """帧标注纯叶桩：记录 (member_id, label)；fail=True 时抛 ordinary 失败。"""
    calls = []

    async def fake(member, ctx, label=None, target=None):
        calls.append((member.id, label))
        if fail:
            raise SchemaViolation(["/intent: invalid"], "{}")
        return _annotation({"intent": "帧", "entities": []})

    monkeypatch.setattr("labelkit.operators.annotate.annotate_member_leaf", fake)
    return calls


def test_verify_backfill_duplicate_content_uses_distinct_occurrences(monkeypatch):
    """帧分类与帧标注补跑按成员 id first-wins，只派发首个重复成员。"""
    cfg = _frame_stream_cfg()
    classify_calls = _stub_classify_frames(monkeypatch)
    annotate_calls = _stub_annotate_member(monkeypatch)
    first = _frame("duplicate", pair_index=0)
    duplicate = _frame("duplicate", pair_index=1)
    episode = _episode([first, duplicate])
    episode.member_classifications = {}
    episode.member_annotations = {}
    state = _EpisodeReview(episode, 0)
    state.rounds = 1
    metrics = _CapturingMetrics()
    runner = _TaskRunner()
    ctx = _task_context(
        cfg=cfg,
        llm=None,
        schema_engine=None,
        metrics=metrics,
        rng=None,
        batch_no=1,
        tasks=runner,
    )
    driver = StreamVerifyDriver(VerifyStage(cfg))

    planned, dead = driver._plan_frame_classify_jobs([state], ctx)
    assert dead == set()
    assert len(planned) == 2 and [job.position for job in planned] == [0, 1]
    assert asyncio.run(driver._backfill_frame_classify([state], ctx)) == set()
    annotate_jobs = driver._frame_annotate_jobs(state, ctx)
    assert len(annotate_jobs) == 2 and [job.position for job in annotate_jobs] == [0, 1]
    assert asyncio.run(driver._backfill_frame_annotate([state], ctx)) == set()

    assert classify_calls == [["duplicate"], ["duplicate"]]
    assert annotate_calls == [("duplicate", "task_request")] * 2
    assert [len(group) for group in runner.groups] == [2, 2]
    assert set(episode.member_classifications) == {0, 1}
    assert set(episode.member_annotations) == {0, 1}


def test_shrink_deletes_stale_keys_including_none_values(monkeypatch):
    """收缩同步：被剔成员的键从两 dict 删除（含值为 None 的 failed 占位键），
    键集与新成员集一致；dict 对象不更换；无键缺位 ⇒ 零补跑调用。"""
    cfg = _frame_stream_cfg(extract_enabled=False)
    _stub_annotate(monkeypatch)
    cf_calls = _stub_classify_frames(monkeypatch)
    am_calls = _stub_annotate_member(monkeypatch)
    f0, f1, f2 = _frame("f0"), _frame("f1"), _frame("f2")
    e0, e1, e2 = _env(f0), _env(f1), _env(f2)
    ep = _episode([f0, f1, f2])
    c0 = _member_cls()
    a0 = _annotation({"intent": "帧"})
    mc = {"f0": c0, "f1": _member_cls("chitchat"), "f2": _member_cls()}
    ma = {"f0": a0, "f1": None, "f2": _annotation({"intent": "帧"})}
    ep.member_classifications = mc
    ep.member_annotations = ma
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[_defect("off_task_members", members=["f1"])]),
        _seq_obj("pass"),
    ]})
    _run_verify(cfg, [e0, e1, e2, ep], engine)
    assert [m.id for m in ep.record.members] == ["f0", "f2"]
    assert ep.member_classifications is mc         # 对象不换（克隆共享前提）
    assert ep.member_annotations is ma
    assert set(mc) == {0, 2}
    assert set(ma) == {0, 2}                 # f1 的 None 值键一并删除
    assert mc[0] is c0 and ma[0] is a0       # 既有键原样保留
    assert cf_calls == [] and am_calls == []       # 无键缺位 ⇒ 零补跑


def test_reclaim_backfills_both_frame_products_only_missing(monkeypatch):
    """回收补跑：新成员补帧分类（单元素调用）+ 帧标注（label = 新鲜帧类）；
    既有键只字未动（对象同一性断言）——幂等 = 只补缺位，含值为 None 的
    failed 占位键不重跑。"""
    cfg = _frame_stream_cfg(extract_enabled=False)
    _stub_judge_window(monkeypatch, relation="continues")
    _stub_annotate(monkeypatch)
    cf_calls = _stub_classify_frames(monkeypatch, label="task_request")
    am_calls = _stub_annotate_member(monkeypatch)
    f0, f1, f2 = _frame("f0"), _frame("f1"), _frame("f2")
    e2 = _env(f2, status="dropped_noise")
    e2.noise_attribution = ("segment", "noise")
    ep = _episode([f0, f1])
    c0, c1 = _member_cls(), _member_cls("chitchat")
    a0 = _annotation({"intent": "帧"})
    ep.member_classifications = {"f0": c0, "f1": c1}
    ep.member_annotations = {"f0": a0, "f1": None}     # f1 = failed 占键 None
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[_defect("missing_tail")]),
        _seq_obj("pass"),
    ]})
    _run_verify(cfg, [_env(f0), _env(f1), e2, ep], engine)
    assert [m.id for m in ep.record.members] == ["f0", "f1", "f2"]
    assert cf_calls == [["f2"]]                        # 单元素调用、只补缺位
    assert am_calls == [("f2", "task_request")]        # 帧类取自补跑判决
    assert ep.member_classifications[0] is c0       # 幂等：既有键对象同一
    assert ep.member_classifications[1] is c1
    assert ep.member_annotations[0] is a0
    assert ep.member_annotations[1] is None         # failed 占位不被重跑
    assert ep.member_classifications[2].label == "task_request"
    assert ep.member_annotations[2] is not None
    assert ep.status == "active"


def test_reclaim_skip_class_member_occupies_no_key(monkeypatch):
    """回收成员帧类为跳过类（视图 enabled=false）⇒ 帧标注不跑、不占键
    （emitter 按缺键推导 skipped）；帧分类照常补跑落键。"""
    cfg = _frame_stream_cfg(extract_enabled=False)
    _stub_judge_window(monkeypatch, relation="continues")
    _stub_annotate(monkeypatch)
    cf_calls = _stub_classify_frames(monkeypatch, label="chitchat")
    am_calls = _stub_annotate_member(monkeypatch)
    f0, f1, f2 = _frame("f0"), _frame("f1"), _frame("f2")
    e2 = _env(f2, status="dropped_noise")
    e2.noise_attribution = ("segment", "noise")
    ep = _episode([f0, f1])
    ep.member_classifications = {"f0": _member_cls(), "f1": _member_cls()}
    ep.member_annotations = {"f0": _annotation({"intent": "帧"}),
                             "f1": _annotation({"intent": "帧"})}
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[_defect("missing_tail")]),
        _seq_obj("pass"),
    ]})
    metrics = _run_verify(cfg, [_env(f0), _env(f1), e2, ep], engine)
    assert [m.id for m in ep.record.members] == ["f0", "f1", "f2"]
    assert cf_calls == [["f2"]]
    assert ep.member_classifications[2].label == "chitchat"
    assert am_calls == []                              # 跳过类不跑帧标注
    assert 2 not in ep.member_annotations           # 不占键（skipped 语义）
    # 终审缺陷修复：回收路径的跳过类与 M5 供数点同口径计 skipped——
    # report 与 members[] 状态直方图可对账。
    assert metrics.counters.get("frame_annotate.skipped") == 1


def test_reclaim_frame_classify_off_uses_global_instruction(monkeypatch):
    """frame.classify 关而 frame.annotate 开：回收补跑不碰分类字段（保持
    None，不得无中生有），帧标注走全局指令（label=None）。"""
    cfg = _frame_stream_cfg(classify_on=False, extract_enabled=False)
    _stub_judge_window(monkeypatch, relation="continues")
    _stub_annotate(monkeypatch)
    cf_calls = _stub_classify_frames(monkeypatch)
    am_calls = _stub_annotate_member(monkeypatch)
    f0, f1, f2 = _frame("f0"), _frame("f1"), _frame("f2")
    e2 = _env(f2, status="dropped_noise")
    e2.noise_attribution = ("segment", "noise")
    ep = _episode([f0, f1])
    ep.member_annotations = {"f0": _annotation({"intent": "帧"}),
                             "f1": _annotation({"intent": "帧"})}
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[_defect("missing_tail")]),
        _seq_obj("pass"),
    ]})
    _run_verify(cfg, [_env(f0), _env(f1), e2, ep], engine)
    assert [m.id for m in ep.record.members] == ["f0", "f1", "f2"]
    assert cf_calls == []                              # 帧分类关：不补跑
    assert ep.member_classifications is None           # 恒 None，不无中生有
    assert am_calls == [("f2", None)]                  # label=None 全局指令
    assert ep.member_annotations[2] is not None


def test_surgery_never_touches_none_frame_products(monkeypatch):
    """dict None（降格会话/帧 pass 未运行语义）：手术全程不触碰两字段——
    回收进成员也不补跑、不建 dict，恒保持 None。"""
    cfg = _frame_stream_cfg(extract_enabled=False)     # 帧粒度双开
    _stub_judge_window(monkeypatch, relation="continues")
    _stub_annotate(monkeypatch)
    cf_calls = _stub_classify_frames(monkeypatch)
    am_calls = _stub_annotate_member(monkeypatch)
    f0, f1, f2 = _frame("f0"), _frame("f1"), _frame("f2")
    e2 = _env(f2, status="dropped_noise")
    e2.noise_attribution = ("segment", "noise")
    ep = _episode([f0, f1])                            # 两字段默认 None
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[_defect("missing_tail")]),
        _seq_obj("pass"),
    ]})
    _run_verify(cfg, [_env(f0), _env(f1), e2, ep], engine)
    assert [m.id for m in ep.record.members] == ["f0", "f1", "f2"]
    assert ep.member_classifications is None
    assert ep.member_annotations is None
    assert cf_calls == [] and am_calls == []


def test_reclaim_annotate_member_failure_occupies_key_none(monkeypatch):
    """annotate_member 返回 None（不可修复）⇒ 占键 None（failed 语义）——
    键在场使后续轮次/pass 不再重跑该成员。"""
    cfg = _frame_stream_cfg(extract_enabled=False)
    _stub_judge_window(monkeypatch, relation="continues")
    _stub_annotate(monkeypatch)
    _stub_classify_frames(monkeypatch)
    am_calls = _stub_annotate_member(monkeypatch, fail=True)
    f0, f1, f2 = _frame("f0"), _frame("f1"), _frame("f2")
    e2 = _env(f2, status="dropped_noise")
    e2.noise_attribution = ("segment", "noise")
    ep = _episode([f0, f1])
    ep.member_classifications = {"f0": _member_cls(), "f1": _member_cls()}
    ep.member_annotations = {"f0": _annotation({"intent": "帧"}),
                             "f1": _annotation({"intent": "帧"})}
    engine = SeqJudgeEngine({ep.record.id: [
        _seq_obj("fail", defects=[_defect("missing_tail")]),
        _seq_obj("pass"),
    ]})
    _run_verify(cfg, [_env(f0), _env(f1), e2, ep], engine)
    assert am_calls == [("f2", "task_request")]
    assert 2 in ep.member_annotations                  # 明确出现位置占键在场。
    assert ep.member_annotations[2] is None         # 值 None = failed


def test_clone_surgery_ban_keeps_frame_products_untouched(monkeypatch):
    """克隆信封手术禁令（S8）下无帧产物同步分支：按引用共享的两 dict 键集
    与值对象原样，两个直调面零调用（成员缺陷全部降格 mark-only）。"""
    cfg = _frame_stream_cfg(base=_stream_classified_cfg())
    _stub_judge_window(monkeypatch)
    _stub_extract(monkeypatch)
    _stub_annotate(monkeypatch)
    cf_calls = _stub_classify_frames(monkeypatch)
    am_calls = _stub_annotate_member(monkeypatch)
    f0, f1, f2 = _frame("f0"), _frame("f1"), _frame("f2")
    e0, e1 = _env(f0), _env(f1)
    e2 = _env(f2, status="dropped_noise")              # 真实候选在场
    e2.noise_attribution = ("segment", "noise")
    original = _episode([f0, f1], classification=Classification(
        label="a", labels=("a", "b"), source="llm", detail={}))
    clone = PipelineItem(record=original.record, session_id="s1",
                         annotation=_annotation({"task_label": "外卖"}),
                         classification=Classification(
                             label="b", labels=("a", "b"), source="llm",
                             detail={}))
    c0 = _member_cls()
    a0 = _annotation({"intent": "帧"})
    mc = {"f0": c0, "f1": _member_cls()}
    ma = {"f0": a0, "f1": None}
    original.member_classifications = mc               # 克隆按引用共享（扇出裁决）
    original.member_annotations = ma
    clone.member_classifications = mc
    clone.member_annotations = ma
    engine = SeqJudgeEngine({original.record.id: [
        _seq_obj("pass"),                              # 原信封先评审
        _seq_obj("fail", defects=[_defect("off_task_members", members=["f1"]),
                                  _defect("missing_tail")]),
    ]})
    _run_verify(cfg, [e0, e1, e2, original, clone], engine)
    assert clone.status == "dropped_verify"            # 缺陷降格 mark-only
    assert [m.id for m in clone.record.members] == ["f0", "f1"]
    assert clone.member_classifications is mc and set(mc) == {0, 1}
    assert clone.member_annotations is ma and set(ma) == {0, 1}
    assert mc[0] is c0 and ma[0] is a0 and ma[1] is None
    assert cf_calls == [] and am_calls == []           # 无手术 ⇒ 无同步分支


# ── v1.13 按序列类标注 Schema 在 V21 试装上的取值（裁决·按类标注 Schema）──────
#
# 修复路径的重标注本身经 annotate_record 的 label 自然穿透（M5 单点取值），M7
# 侧唯一的消费点是升清换档的试装估算：提示词 Schema 文本与 schema_eff 计价都
# 必须按类取值，否则试装与真实调用不同源。以下两例分别钉住文本侧与计价侧。

def _text_member(rid: str, text: str) -> Record:
    return Record(id=rid, modality="text", text=text, raw={"text": text},
                  ui_tree=None, image=None,
                  ref=RecordRef("out/res.stream.jsonl", int(rid[0]), None, ()))


def _assembled_sequence(n: int = 3) -> Record:
    members = tuple(_text_member(f"{i}" * 16, f"帧内容第 {i} 句") for i in range(1, n + 1))
    return Record(id="e" * 16, modality="text", text=None, raw=None, ui_tree=None,
                  image=None, ref=members[0].ref, kind="sequence", members=members)


def _verdict_cfg(*, policy="drop", max_repair_rounds=1,
                 with_views=False) -> ResolvedConfig:
    base = trace_cfg(enabled=False)
    cfg = replace(base,
                  run=replace(base.run, mode="generate_only", input=None),
                  verify=VerifyConfig(enabled=True, llm="judge", policy=policy,
                                      max_repair_rounds=max_repair_rounds))
    if with_views:
        view = ClassView(
            name="faq", quality=cfg.quality, rubric=cfg.rubric,
            annotate=AnnotateConfig(enabled=True, instruction="按类标注 faq。"),
            generate=cfg.generate,
            verify=VerifyConfig(enabled=True, llm="judge",
                                extra_criteria="④ 序列连贯性"),
            extract=cfg.extract, model_schema=cfg.model_user_schema)
        cfg = replace(cfg, classify=ClassifyConfig(
            enabled=True,
            classes=(ClassSpec(name="faq", description="d"),)),
            class_views={"faq": view})
    return cfg


def test_verdict_sequence_system_text_shape():
    text = verify_verdict_sequence_system_text("")
    assert text == (
        "你是标注质量审核员。给定任务指令、成员帧摘要与标注结果，独立判断该序列"
        "（episode）的标注是否合格。\n"
        "评审维度: ① 是否遵循任务指令 ② 与成员帧摘要证据的事实一致性 ③ 字段语义是否正确填写\n"
        "先逐维度给出简短意见，再给结论。\n"
        "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
        '{"critiques": [{"aspect": <维度>, "opinion": <一句话意见>}, ...],\n'
        ' "verdict": "pass"|"fail"}')
    # 判决形不带缺陷词表（defects 键被 VERDICT_SCHEMA 禁止）
    assert "缺陷类型" not in text and "defects" not in text
    with_extra = verify_verdict_sequence_system_text("④ 额外准则")
    assert "④ 额外准则\n先逐维度给出简短意见，再给结论。" in with_extra


def test_verdict_form_prompt_sections_and_member_digests():
    cfg = _verdict_cfg()
    record = _assembled_sequence(3)
    bundle = build_verify_prompt(record, {"intent": "问答"}, cfg,
                                 VerifyPromptOptions(verdict_form=True))
    assert len(bundle.messages) == 2
    system_text = bundle.messages[0].parts[0].text
    assert system_text == verify_verdict_sequence_system_text("")
    parts = bundle.messages[1].parts
    assert [p.kind for p in parts] == ["text", "text", "text"]     # 无截图段
    assert parts[0].text == "[任务指令] 给指令分类。"
    digest_lines = parts[1].text.split("\n")
    assert digest_lines[0] == "[成员帧摘要]"
    assert digest_lines[1:] == ["1. 帧内容第 1 句", "2. 帧内容第 2 句",
                                "3. 帧内容第 3 句"]
    assert parts[2].text == '[标注结果] {"intent": "问答"}'
    # 判决形无缺陷表/边界余量/片段结构三段
    joined = "\n".join(p.text for p in parts)
    assert "[边界余量]" not in joined and "[片段结构]" not in joined
    assert "[动作序列]" not in joined


def test_verdict_form_uses_class_effective_instruction_and_criteria():
    cfg = _verdict_cfg(with_views=True)
    bundle = build_verify_prompt(
        _assembled_sequence(), {"intent": "x"}, cfg,
        VerifyPromptOptions(label="faq", verdict_form=True))
    system_text = bundle.messages[0].parts[0].text
    assert "④ 序列连贯性" in system_text
    assert bundle.messages[1].parts[0].text == "[任务指令] 按类标注 faq。"


def test_verdict_form_off_keeps_defect_variant_byte_identical():
    """verdict_form 缺省 False ⇒ 既有 §10.5 缺陷词表序列变体零改动（流式驱动器
    调用面）。"""
    cfg = _verdict_cfg()
    record = _assembled_sequence()
    bundle = build_verify_prompt(record, {"intent": "x"}, cfg,
                                 VerifyPromptOptions(member_positions=(0, 1, 2), boundary_margin="段首前 1: 无"))
    system_text = bundle.messages[0].parts[0].text
    assert "缺陷类型" in system_text
    assert any("[边界余量]" in (p.text or "")
               for p in bundle.messages[1].parts if p.kind == "text")


class _VerdictEngine:
    """经典路径判决形桩：按 record_ids[0] 弹出既定判决对象（VERDICT_SCHEMA 形）。"""

    def __init__(self, scripts):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls: list = []              # (profile, prompt, schema, record_ids)

    async def complete_validated(self, profile, prompt, schema=None, *, scope):
        record_ids = scope.record_ids
        self.calls.append((profile, prompt, schema, record_ids))
        out = self.scripts[record_ids[0]].pop(0)
        if isinstance(out, Exception):
            raise out
        return out, Usage(), 1, "m"


def _verdict_obj(verdict, critiques=None):
    return {"critiques": critiques if critiques is not None
            else [{"aspect": "指令遵循", "opinion": "一致"}],
            "verdict": verdict}


def test_classic_path_sequence_pairs_verdict_form_with_verdict_schema():
    """直装序列（segment 关）走经典路径：prompt = 判决形模板 × schema =
    VERDICT_SCHEMA（模板/schema 配对错位即 v1.12 的审计缺陷面）；fail ⇒
    dropped_verify；VerificationResult.defects 恒空。"""
    cfg = _verdict_cfg()
    passing = PipelineItem(record=_assembled_sequence(), status="active",
                           annotation=_annotation({"intent": "问答"}))
    failing = PipelineItem(record=replace(_assembled_sequence(), id="f" * 16),
                           status="active",
                           annotation=_annotation({"intent": "错标"}))
    engine = _VerdictEngine({"e" * 16: [_verdict_obj("pass")],
                             "f" * 16: [_verdict_obj("fail")]})
    metrics = _CapturingMetrics()
    ctx = _task_context(
        cfg=cfg, llm=None, schema_engine=engine, metrics=metrics,
        rng=None, batch_no=1,
    )
    asyncio.run(VerifyStage(cfg).run([passing, failing], ctx))

    for _, prompt, schema, _ids in engine.calls:
        assert schema is VERDICT_SCHEMA
        system_text = prompt.messages[0].parts[0].text
        assert system_text.startswith(_VERDICT_HEAD)
        assert "缺陷类型" not in system_text
        assert any("[成员帧摘要]" in (p.text or "")
                   for p in prompt.messages[1].parts)
    assert passing.status == "active"
    assert passing.verification.verdict == "pass"
    assert passing.verification.defects == ()
    assert failing.status == "dropped_verify"
    assert failing.verification.defects == ()


def test_verify_judge_receives_complete_annotation_with_code_owned_field():
    """判决提示必须携带最终完整对象，包括只由后处理函数补齐的字段。"""
    full_schema = {
        "type": "object",
        "properties": {
            "intent": {"type": "string"},
            "summary_length": {
                "type": "integer",
                "x-labelkit-postprocessor": True,
            },
        },
        "required": ["intent", "summary_length"],
        "additionalProperties": False,
    }
    model_schema = {
        "type": "object",
        "properties": {"intent": {"type": "string"}},
        "required": ["intent"],
        "additionalProperties": False,
    }

    postprocessor_calls = []

    def complete(obj, record):
        postprocessor_calls.append((dict(obj), record))
        obj["summary_length"] += 1
        return obj

    base = _verdict_cfg()
    cfg = replace(
        base,
        annotate=replace(
            base.annotate,
            resolved_postprocessor=ResolvedHook("/project/hooks.py:complete", complete),
        ),
        user_schema=full_schema,
        model_user_schema=model_schema,
    )
    output = {"intent": "问答", "summary_length": 2}
    item = PipelineItem(
        record=_assembled_sequence(), status="active", annotation=_annotation(output),
    )
    engine = _VerdictEngine({"e" * 16: [_verdict_obj("pass")]})
    ctx = _task_context(
        cfg=cfg, llm=None, schema_engine=engine, metrics=_CapturingMetrics(),
        rng=None, batch_no=1,
    )

    asyncio.run(VerifyStage(cfg).run([item], ctx))

    ((_, prompt, schema, _),) = engine.calls
    annotation_parts = [
        part.text for part in prompt.messages[1].parts
        if (part.text or "").startswith("[标注结果] ")
    ]
    assert len(annotation_parts) == 1
    assert json.loads(annotation_parts[0].removeprefix("[标注结果] ")) == output
    assert "summary_length" not in model_schema["properties"]
    assert schema is VERDICT_SCHEMA
    assert item.verification.verdict == "pass"
    assert postprocessor_calls == []


_VERDICT_HEAD = "你是标注质量审核员。给定任务指令、成员帧摘要与标注结果"


def test_classic_path_sequence_repair_policy_reannotates(monkeypatch):
    """修复 = 既有 policy 重标注（annotate_record 修复面穿透）：首轮 fail →
    重标注 → 次轮 pass ⇒ active + 修复后标注落信封。"""
    cfg = _verdict_cfg(policy="repair", max_repair_rounds=1, with_views=True)
    temporal_context = SequenceTemporalContext(())
    item = PipelineItem(record=_assembled_sequence(), status="active",
                        annotation=_annotation({"intent": "错标"}),
                        classification=Classification(
                            label="faq", labels=("faq",), source="inherited",
                            detail={}),
                        temporal_context=temporal_context)
    engine = _VerdictEngine({"e" * 16: [_verdict_obj("fail"),
                                        _verdict_obj("pass")]})
    calls = _stub_annotate(monkeypatch, output={"intent": "修正"})
    metrics = _CapturingMetrics()
    ctx = _task_context(
        cfg=cfg, llm=None, schema_engine=engine, metrics=metrics,
        rng=None, batch_no=1,
    )
    asyncio.run(VerifyStage(cfg).run([item], ctx))

    assert item.status == "active"
    assert item.annotation.output == {"intent": "修正"}
    assert item.verification.rounds == 2
    (call,) = calls
    assert call.label == "faq"                     # 修复穿按类取值面
    assert call.record.kind == "sequence"
    assert call.temporal_context is temporal_context


def test_stream_driver_path_unperturbed_by_verdict_form():
    """流式驱动器路径零改动：segment 开 ⇒ 序列走 §10.5 缺陷词表变体 +
    defect_verdict_schema（判决形永不触发）。"""
    cfg = _stream_cfg(policy="drop", extract_enabled=False)
    members = [_frame("m1"), _frame("m2")]
    batch = [_env(members[0]), _env(members[1]),
             _episode(members, eid="e" * 16)]
    engine = SeqJudgeEngine({"e" * 16: [_seq_obj("pass")]})
    metrics = _CapturingMetrics()
    ctx = _task_context(
        cfg=cfg, llm=None, schema_engine=engine, metrics=metrics,
        rng=None, batch_no=1,
    )
    asyncio.run(VerifyStage(cfg).run(batch, ctx))

    ((_, prompt, schema, _ids),) = [engine.calls[0]]
    assert schema == defect_verdict_schema()
    system_text = prompt.messages[0].parts[0].text
    assert "缺陷类型" in system_text
    assert any("[边界余量]" in (p.text or "") for p in prompt.messages[1].parts)


def test_verdict_form_budget_fit_trims_digest_block_only():
    """v1.11 装填相容：判决形的唯一可裁槽位 = 成员摘要块（edges 裁剪计
    truncations）；[标注结果] 恒计不裁；地板超预算 ⇒ overflow（V10）。"""
    from labelkit.operators.verify import _PromptFit, _build_verdict_sequence_prompt

    record = _assembled_sequence(30)
    cfg = _verdict_cfg()
    fit = _PromptFit(input_budget=260, image_cost=0)
    bundle = _build_verdict_sequence_prompt(record, {"intent": "x"}, cfg,
                                            ("指令", ""), fit)
    assert fit.truncations == 1 and fit.overflow is False
    digest_part = bundle.messages[1].parts[1].text
    assert "…(truncated" in digest_part
    assert digest_part.split("\n")[1] == "1. 帧内容第 1 句"    # 首行恒保留

    tight = _PromptFit(input_budget=10, image_cost=0)
    _build_verdict_sequence_prompt(record, {"intent": "x"}, cfg, ("指令", ""),
                                   tight)
    assert tight.overflow is True


@pytest.mark.parametrize("error", [
    ProviderFatalError("fatal", "default", 401),
    ProviderRetryableError("retryable", "default", 5),
    CircuitBreakerTripped("breaker"),
])
def test_verify_attempt_seam_propagates_terminal_errors_without_dataset_commit(
        monkeypatch, error):
    from contextlib import contextmanager
    from labelkit.common.contracts.generation import (
        AttemptTransaction,
        DownstreamAttemptRequest,
    )

    class AttemptMetrics:
        def __init__(self):
            self.counters = {}
            self.captured = None

        @contextmanager
        def capture_counts(self):
            self.captured = {}
            try:
                yield self.captured
            finally:
                self.captured = None

        def count(self, key, n=1):
            target = self.captured if self.captured is not None else self.counters
            target[key] = target.get(key, 0) + n

    cfg = trace_cfg()
    metrics = AttemptMetrics()
    item = PipelineItem(record=_record(), annotation=_annotation())
    transaction = AttemptTransaction((item,), {}, ())
    context = _task_context(
        cfg=cfg, llm=None, schema_engine=None, metrics=metrics,
        rng=None, batch_no=1,
    )
    request = DownstreamAttemptRequest(transaction, context)
    stage = VerifyStage(cfg)

    async def fail(batch, run_context):
        run_context.metrics.count("verify.passed")
        raise error

    monkeypatch.setattr(stage, "run", fail)
    with pytest.raises(type(error)) as caught:
        asyncio.run(stage.run_attempt(request))
    assert caught.value is error
    assert metrics.counters == {}


# ── v1.19 verify runtime 波次、纯叶与错误拓扑 ──────────────────────────────

def _business_snapshot(batch, metrics):
    """冻结叶任务禁止修改的 verify 业务状态。"""
    items = []
    for item in batch:
        classifications = (
            None if item.member_classifications is None
            else tuple(sorted(item.member_classifications.items()))
        )
        annotations = (
            None if item.member_annotations is None
            else tuple(sorted(item.member_annotations.items()))
        )
        items.append((
            item.status,
            item.record,
            item.annotation,
            item.verification,
            tuple(item.errors),
            item.transitions,
            classifications,
            annotations,
            getattr(item, "noise_attribution", None),
            getattr(item, "stream_repaired", None),
        ))
    return tuple(items), tuple(metrics.events), tuple(sorted(metrics.counters.items()))


def _group_phase(task_ids):
    """从不含数据内容的任务身份读取 stream 波次名。"""
    task_id = task_ids[0]
    for phase in (
        "frame-classify",
        "frame-annotate",
        "reannotate",
        "reseam",
        "claim",
        "review",
    ):
        if f":{phase}:" in task_id:
            return phase
    raise AssertionError(f"unknown verify task phase: {task_id}")


def test_classic_reverse_completion_preserves_input_reduce_and_round_barriers(
        monkeypatch):
    """judge/repair 叶逆序完成仍按输入序归并，且三组保持 round 屏障。"""
    cfg = replace(
        trace_cfg(enabled=True),
        verify=VerifyConfig(enabled=True, llm="default", policy="repair",
                            max_repair_rounds=1),
    )
    first = PipelineItem(record=_record("a" * 16), annotation=_annotation({"v": 0}))
    second = PipelineItem(record=_record("b" * 16), annotation=_annotation({"v": 0}))
    batch = [first, second]
    engine = _VerdictEngine({
        first.record.id: [_verdict_obj("fail"), _verdict_obj("pass")],
        second.record.id: [_verdict_obj("fail"), _verdict_obj("pass")],
    })

    async def repair(record, ctx, opts):
        return _annotation({"v": 1})

    monkeypatch.setattr("labelkit.operators.annotate.annotate_record_leaf", repair)
    metrics = _CapturingMetrics()
    runner = _ReverseTaskRunner(lambda: _business_snapshot(batch, metrics))
    ctx = _task_context(
        cfg=cfg,
        llm=None,
        schema_engine=engine,
        metrics=metrics,
        rng=None,
        batch_no=1,
        tasks=runner,
    )

    asyncio.run(VerifyStage(cfg).run(batch, ctx))

    assert runner.completion_orders == [[1, 0], [1, 0], [1, 0]]
    assert [":repair:" in group[0] for group in runner.groups] == [False, True, False]
    verdict_ids = [event[3][0] for event in metrics.events
                   if event[0] == "verify.verdict"]
    assert verdict_ids == [first.record.id, second.record.id] * 2
    assert [item.verification.rounds for item in batch] == [2, 2]
    assert [item.annotation.output for item in batch] == [{"v": 1}, {"v": 1}]


def test_stream_strict_waves_use_pure_leaves_and_fresh_frame_results(monkeypatch):
    """完整回收修复链保持严格波次；每个 TaskGroup 返回前业务状态均未被叶修改。"""
    cfg = _frame_stream_cfg(extract_enabled=True)
    _stub_judge_window(monkeypatch, relation="continues")
    _stub_extract(monkeypatch)
    _stub_classify_frames(monkeypatch, label="task_request")
    member_calls = _stub_annotate_member(monkeypatch)
    annotate_calls = _stub_annotate(monkeypatch, output={"task_label": "修正"})

    async def forbidden(*args, **kwargs):
        raise AssertionError("verify called a grouped operator surface from a leaf")

    monkeypatch.setattr("labelkit.operators.segment.judge_window", forbidden)
    monkeypatch.setattr("labelkit.operators.extract.extract_transition", forbidden)
    monkeypatch.setattr("labelkit.operators.classify.classify_frames", forbidden)
    monkeypatch.setattr("labelkit.operators.annotate.annotate_record", forbidden)
    monkeypatch.setattr("labelkit.operators.annotate.annotate_member", forbidden)

    first, second, reclaimed = _frame("f0"), _frame("f1"), _frame("f2")
    noise = _env(reclaimed, status="dropped_noise")
    noise.noise_attribution = ("segment", "noise")
    episode = _episode([first, second], transitions=(_transition(0),))
    temporal_context = SequenceTemporalContext(())
    episode.temporal_context = temporal_context
    episode.member_classifications = {
        first.id: _member_cls(),
        second.id: _member_cls(),
    }
    episode.member_annotations = {
        first.id: _annotation({"intent": "帧"}),
        second.id: _annotation({"intent": "帧"}),
    }
    batch = [_env(first), _env(second), noise, episode]
    engine = SeqJudgeEngine({episode.record.id: [
        _seq_obj("fail", defects=[_defect("missing_tail")]),
        _seq_obj("pass"),
    ]})
    metrics = _CapturingMetrics()
    runner = _ReverseTaskRunner(lambda: _business_snapshot(batch, metrics))
    ctx = _task_context(
        cfg=cfg,
        llm=None,
        schema_engine=engine,
        metrics=metrics,
        rng=None,
        batch_no=1,
        tasks=runner,
    )

    _stamp_stream(batch, engine)
    asyncio.run(VerifyStage(cfg).run(batch, ctx))

    assert [_group_phase(group) for group in runner.groups] == [
        "review",
        "claim",
        "reseam",
        "frame-classify",
        "frame-annotate",
        "reannotate",
        "review",
    ]
    assert noise.status == "absorbed"
    assert [member.id for member in episode.record.members] == ["f0", "f1", "f2"]
    assert episode.member_classifications[2].label == "task_request"
    assert member_calls == [("f2", "task_request")]
    assert episode.member_annotations[2].output == {"intent": "帧", "entities": []}
    assert len(annotate_calls) == 1
    assert annotate_calls[0].temporal_context is temporal_context
    assert episode.verification.verdict == "pass"


def test_ordinary_provider_fatal_isolated_without_cancelling_sibling():
    """普通 verify 把 ProviderFatal 转成记录级失败，兄弟仍按输入序提交。"""
    cfg = trace_cfg(enabled=True)
    failed = PipelineItem(record=_record("a" * 16), annotation=_annotation())
    passing = PipelineItem(record=_record("b" * 16), annotation=_annotation())
    fatal = ProviderFatalError("fatal", "default", 401)
    engine = _VerdictEngine({
        failed.record.id: [fatal],
        passing.record.id: [_verdict_obj("pass")],
    })
    metrics = _CapturingMetrics()
    ctx = _task_context(
        cfg=cfg,
        llm=None,
        schema_engine=engine,
        metrics=metrics,
        rng=None,
        batch_no=1,
    )

    asyncio.run(VerifyStage(cfg).run([failed, passing], ctx))

    assert failed.status == "failed"
    assert failed.errors[0].kind == "provider_fatal"
    assert passing.status == "active"
    assert passing.verification.verdict == "pass"


class _AttemptMetrics(_CapturingMetrics):
    """隔离 attempt dataset counters 的 verify 测试指标汇。"""

    def __init__(self):
        super().__init__()
        self.captured = None

    @contextmanager
    def capture_counts(self):
        captured = {}
        self.captured = captured
        try:
            yield captured
        finally:
            self.captured = None

    def count(self, key, n=1):
        target = self.captured if self.captured is not None else self.counters
        target[key] = target.get(key, 0) + n


class _RaisingEngine:
    """每次结构化调用原样抛出指定异常。"""

    def __init__(self, error):
        self.error = error

    async def complete_validated(self, profile, prompt, schema=None, *, scope):
        raise self.error


def _attempt_case(error):
    """建立走真实 classic driver 的单 variant attempt。"""
    from labelkit.common.contracts.generation import (
        AttemptTransaction,
        DownstreamAttemptRequest,
    )

    cfg = _verdict_cfg()
    metrics = _AttemptMetrics()
    item = PipelineItem(record=_assembled_sequence(), annotation=_annotation())
    transaction = AttemptTransaction((item,), {}, ())
    ctx = _task_context(
        cfg=cfg,
        llm=None,
        schema_engine=_RaisingEngine(error),
        metrics=metrics,
        rng=None,
        batch_no=1,
        tasks=_SerialTaskRunner(),
    )
    request = DownstreamAttemptRequest(transaction, ctx)
    return VerifyStage(cfg), request, item, metrics


@pytest.mark.parametrize("error", [
    ProviderFatalError("fatal", "judge", 401),
    CircuitBreakerTripped("breaker"),
    asyncio.CancelledError("cancelled"),
])
def test_sequence_attempt_fatal_and_control_errors_escape_unchanged(error):
    """sequence ProviderFatal、熔断与取消不变形穿透且不提交 dataset counter。"""
    stage, request, _item, metrics = _attempt_case(error)

    with pytest.raises(type(error)) as caught:
        asyncio.run(stage.run_attempt(request))

    assert caught.value is error
    assert metrics.counters == {}


def test_sequence_attempt_retryable_error_is_recoverable_outcome():
    """sequence ProviderRetryable 消耗当前 attempt，而不是升级 runtime fatal。"""
    error = ProviderRetryableError("retryable", "judge", 5)
    stage, request, item, metrics = _attempt_case(error)

    result = asyncio.run(stage.run_attempt(request))

    assert result.accepted is False
    assert result.rejected_stage == "verify"
    assert result.dataset_counters == {}
    assert item.status == "failed"
    assert item.errors[0].kind == "provider_retryable_exhausted"
    assert metrics.counters == {}


class _FinalizedFailureStreamEngine(SeqJudgeEngine):
    """Judge normally, then fail the annotation finalized boundary."""

    user_schema_text = json.dumps(USER_SCHEMA, ensure_ascii=False,
                                  separators=(", ", ": "))

    def __init__(self, scripts, error):
        super().__init__(scripts)
        self.error = error
        self.finalized_calls = []

    async def complete_finalized(self, request):
        self.finalized_calls.append(request)
        raise self.error


def _enable_record_postprocessor(cfg):
    def complete(obj, record):
        return obj

    hook = ResolvedHook("/project/hooks.py:complete", complete)
    return replace(
        cfg, annotate=replace(cfg.annotate, resolved_postprocessor=hook),
    )


def _stream_attempt(cfg, items, engine):
    from labelkit.common.contracts.generation import (
        AttemptTransaction,
        DownstreamAttemptRequest,
    )

    metrics = _AttemptMetrics()
    _stamp_stream(items, engine)
    transaction = AttemptTransaction(tuple(items), {}, ())
    ctx = _task_context(
        cfg=cfg, llm=None, schema_engine=engine, metrics=metrics,
        rng=None, batch_no=1, tasks=_SerialTaskRunner(),
    )
    return VerifyStage(cfg), DownstreamAttemptRequest(transaction, ctx), metrics


def test_stream_episode_repair_postprocessor_error_is_attempt_fatal():
    cfg = _enable_record_postprocessor(_stream_cfg(extract_enabled=False))
    episode = _episode([_frame("f0"), _frame("f1")])
    error = PostprocessorError()
    engine = _FinalizedFailureStreamEngine({
        episode.record.id: [_seq_obj("fail", defects=[_defect("label_mismatch")])],
    }, error)
    stage, request, metrics = _stream_attempt(cfg, [episode], engine)

    with pytest.raises(PostprocessorError) as caught:
        asyncio.run(stage.run_attempt(request))

    assert caught.value is error
    assert len(engine.finalized_calls) == 1
    assert metrics.counters == {}


def test_stream_episode_repair_final_schema_error_is_attempt_fatal():
    cfg = _enable_record_postprocessor(_stream_cfg(extract_enabled=False))
    episode = _episode([_frame("f0"), _frame("f1")])
    engine = _FinalizedFailureStreamEngine({
        episode.record.id: [_seq_obj("fail", defects=[_defect("label_mismatch")])],
    }, CandidateFinalizerContractError())
    stage, request, metrics = _stream_attempt(cfg, [episode], engine)

    with pytest.raises(InternalError, match="candidate finalizer contract failed"):
        asyncio.run(stage.run_attempt(request))

    assert len(engine.finalized_calls) == 1
    assert metrics.counters == {}


def test_stream_frame_backfill_final_schema_error_is_attempt_fatal(monkeypatch):
    hook = ResolvedHook("/project/hooks.py:complete_frame", lambda obj, record: obj)
    base = _frame_stream_cfg(extract_enabled=False)
    views = {
        name: replace(view, resolved_postprocessor=hook)
        for name, view in base.frame_class_views.items()
    }
    cfg = replace(
        base,
        frame_annotate=replace(
            base.frame_annotate, resolved_postprocessor=hook,
        ),
        frame_class_views=views,
    )
    _stub_judge_window(monkeypatch, relation="continues")
    _stub_classify_frames(monkeypatch, label="task_request")
    first, second, reclaimed = (
        _ui_frame("f0"), _ui_frame("f1"), _ui_frame("f2"),
    )
    noise = _env(reclaimed, status="dropped_noise")
    noise.noise_attribution = ("segment", "noise")
    episode = _episode([first, second])
    episode.member_classifications = {
        first.id: _member_cls(), second.id: _member_cls(),
    }
    episode.member_annotations = {
        first.id: _annotation({"intent": "帧"}),
        second.id: _annotation({"intent": "帧"}),
    }
    engine = _FinalizedFailureStreamEngine({
        episode.record.id: [_seq_obj("fail", defects=[_defect("missing_tail")])],
    }, CandidateFinalizerContractError())
    stage, request, metrics = _stream_attempt(
        cfg, [_env(first), _env(second), noise, episode], engine,
    )

    with pytest.raises(InternalError, match="frame annotation candidate finalizer"):
        asyncio.run(stage.run_attempt(request))

    assert len(engine.finalized_calls) == 1
    assert engine.finalized_calls[0].scope.record_ids == (reclaimed.id,)
    assert metrics.counters == {}


def test_zero_call_verify_submits_no_task_group():
    """没有 eligible 记录时返回原批次，且不会提交空任务组。"""
    batch = [PipelineItem(record=_record(), status="active", annotation=None)]
    ctx = _task_context(tasks=_RejectTaskRunner())

    result = asyncio.run(VerifyStage(None).run(batch, ctx))

    assert result is batch


def test_preview_capacity_prices_full_members_and_actual_schema_without_model():
    cfg = _stream_cfg(policy="drop")
    profiles = {name: replace(profile, context_window=4000, max_output_tokens=100)
                for name, profile in cfg.llm_profiles.items()}
    cfg = replace(cfg, llm_profiles=profiles)
    episode = _episode([_ui_frame("a", "unique-middle" + "x" * 50000), _frame("b")])
    engine = SeqJudgeEngine({})
    ctx = _task_context(cfg=cfg, schema_engine=engine, metrics=_CapturingMetrics())
    failure = VerifyStage(cfg).preview_capacity(episode, ctx)
    assert failure.stage == "verify" and failure.unit == "sequence"
    assert failure.targets[0].member_positions == (0, 1)
    assert failure.error.phase == "precheck" and failure.error.profile == "judge"
    assert engine.calls == []


def test_actual_review_overflow_escapes_before_model_and_preserves_all_items():
    from labelkit.common.errors import SessionCapacityError
    cfg = _stream_cfg(policy="drop")
    cfg = replace(cfg, llm_profiles={name: replace(profile, context_window=4000, max_output_tokens=100)
                                   for name, profile in cfg.llm_profiles.items()})
    frame = _env(_ui_frame("a", "x" * 50000))
    episode = _episode([frame.record])
    batch = [frame, episode]
    _stamp_stream(batch)
    original = [dict(item.__dict__) for item in batch]
    engine = SeqJudgeEngine({})
    ctx = _task_context(cfg=cfg, schema_engine=engine, metrics=_CapturingMetrics())
    with pytest.raises(SessionCapacityError) as captured:
        asyncio.run(VerifyStage(cfg).run(batch, ctx))
    assert captured.value.failures[0].unit == "sequence"
    assert engine.calls == []
    assert [item.__dict__ for item in batch] == original


def test_capacity_boundary_exemption_is_specific_to_touching_edge():
    from labelkit.operators.verify_capacity import boundary_suspicion
    episode = _episode([_frame("a"), _frame("b")])
    episode.member_positions = (3, 4)
    before, after = CapacityCut(2, 3, "annotate", "default", "precheck"), CapacityCut(4, 5, "annotate", "default", "precheck")
    episode.capacity = SequenceCapacity(SequenceBounds(3, 5, before, after), sealed=True)
    assert boundary_suspicion(episode, _defect("missing_head"))
    assert boundary_suspicion(episode, _defect("missing_tail"))
    assert not boundary_suspicion(episode, _defect("missing_members", members=[2]))
    assert not boundary_suspicion(episode, _defect("missing_tail", members=[100]))
    assert boundary_suspicion(episode, _defect("missing_tail", members=[5]))
    assert not boundary_suspicion(episode, _defect("label_mismatch"))
    episode.member_positions = (4,)
    episode.record = replace(episode.record, members=episode.record.members[1:])
    assert not boundary_suspicion(episode, _defect("missing_head"))


def test_capacity_suspicion_does_not_exempt_other_real_defects(monkeypatch):
    cfg = _stream_cfg(policy="drop")
    frame = _env(_frame("f0"))
    episode = _episode([frame.record])
    cut = CapacityCut(0, 1, "annotate", "default", "precheck")
    episode.capacity = SequenceCapacity(SequenceBounds(0, 1, after=cut), sealed=True)
    engine = SeqJudgeEngine({episode.record.id: [_seq_obj("fail", defects=[
        _defect("missing_tail"), _defect("label_mismatch")])]})
    _run_verify(cfg, [frame, episode], engine)
    assert episode.status == "dropped_verify"
    assert episode.verification.verdict == "fail"
    assert [defect["kind"] for defect in episode.verification.defects] == ["label_mismatch", "missing_tail"]


def test_expanded_reannotation_overflow_reports_working_positions_and_rolls_back(monkeypatch):
    from labelkit.common.errors import SessionCapacityError
    cfg = _stream_cfg(extract_enabled=False)
    _stub_judge_window(monkeypatch)
    observed = []

    async def overflow(record, ctx, opts=None):
        observed.append(tuple(member.id for member in record.members))
        raise ContextOverflowError("expanded evidence overflow", profile="default", phase="reactive", origin="finish")

    monkeypatch.setattr("labelkit.operators.annotate.annotate_record_leaf", overflow)
    frames = [_env(_frame(f"f{i}")) for i in range(3)]
    frames[2].status = "dropped_noise"
    frames[2].noise_attribution = ("segment", "noise")
    episode = _episode([frames[0].record, frames[1].record])
    batch = [*frames, episode]
    engine = SeqJudgeEngine({episode.record.id: [_seq_obj("fail", defects=[_defect("missing_tail")])]})
    _stamp_stream(batch, engine)
    original_annotation = episode.annotation
    ctx = _task_context(cfg=cfg, schema_engine=engine, metrics=_CapturingMetrics())
    with pytest.raises(SessionCapacityError) as captured:
        asyncio.run(VerifyStage(cfg).run(batch, ctx))
    failure = captured.value.failures[0]
    assert failure.stage == "verify" and failure.unit == "sequence"
    assert failure.targets[0].member_positions == (0, 1, 2)
    assert observed == [("f0", "f1", "f2")]
    assert episode.member_positions == (0, 1) and episode.annotation == original_annotation
    assert frames[2].status == "dropped_noise" and frames[2].noise_attribution == ("segment", "noise")
    assert not hasattr(episode, "stream_repaired") and episode.verification is None


def test_named_duplicate_content_shrink_removes_only_target_occurrence(monkeypatch):
    cfg = _stream_cfg(extract_enabled=False)
    _stub_annotate(monkeypatch)
    frames = [_env(_frame("same", pair_index=index)) for index in range(3)]
    episode = _episode([frame.record for frame in frames])
    engine = SeqJudgeEngine({episode.record.id: [
        _seq_obj("fail", defects=[_defect("off_task_members", members=[1])]), _seq_obj("pass")]})
    _run_verify(cfg, [*frames, episode], engine)
    assert episode.member_positions == (0, 2)
    assert [frame.status for frame in frames] == ["absorbed", "dropped_noise", "absorbed"]
    assert episode.record.members == (frames[0].record, frames[2].record)


def test_claim_and_rebuild_recheck_allowed_bounds_before_committing():
    cfg = _stream_cfg(extract_enabled=False)
    episode = _episode([_frame("a"), _frame("b")])
    episode.capacity = SequenceCapacity(SequenceBounds(0, 2))
    state = _EpisodeReview(episode, 0)
    frame = _env(_frame("outside", pair_index=2), status="dropped_noise")
    driver = StreamVerifyDriver(VerifyStage(cfg))
    with pytest.raises(InternalError, match="claim violates"):
        driver._make_claim(state, frame, 2)
    state.working_positions.append(2)
    state.working_members.append(frame.record)
    with pytest.raises(InternalError, match="rebuild violates"):
        driver._rebuild_episode(state)
    assert episode.member_positions == (0, 1) and frame.status == "dropped_noise"


@pytest.mark.parametrize("side", ["lower", "upper"])
def test_reclaim_commit_rejects_claim_when_capacity_bounds_narrow_after_planning(side, caplog):
    frames = [_env(_frame(f"f{i}", pair_index=i), status="dropped_noise") for i in range(4)]
    for frame in frames:
        frame.noise_attribution = ("segment", "noise")
    frames[1].status = frames[2].status = "absorbed"
    episode = _episode([frames[1].record, frames[2].record], transitions=(_transition(0),))
    episode.member_positions = (1, 2)
    episode.capacity = SequenceCapacity(SequenceBounds(0, 4), root_id="root")
    state = _EpisodeReview(episode, 0)
    driver = StreamVerifyDriver(VerifyStage(_stream_cfg()))
    position = 0 if side == "lower" else 3
    claim = driver._make_claim(state, frames[position], position)
    assert claim.position == position and claim.envelope is frames[position]
    assert claim.window_positions == ((0, 1) if side == "lower" else (2, 3))
    cut = CapacityCut(0, 1, "annotate", "default", "precheck") if side == "lower" else (
        CapacityCut(2, 3, "annotate", "default", "precheck"))
    bounds = SequenceBounds(1, 4, before=cut) if side == "lower" else SequenceBounds(0, 3, after=cut)
    episode.capacity = replace(episode.capacity, bounds=bounds, sealed=True)
    original_frame_states = [dict(vars(frame)) for frame in frames]
    original_episode = dict(vars(episode))
    original_members, original_positions = list(state.working_members), list(state.working_positions)

    with pytest.raises(InternalError, match="claim commit violates capacity bounds"):
        driver._apply_reclaim(state, claim)

    assert [vars(frame) for frame in frames] == original_frame_states
    assert vars(episode) == original_episode
    assert episode.record is original_episode["record"]
    assert episode.annotation is original_episode["annotation"]
    assert state.working_members == original_members and state.working_positions == original_positions
    assert not state.surgical
    assert driver._repair_snapshots == {} and driver._repair_counts == {}
    assert "stream verify claim commit violates capacity bounds" in caplog.text


@pytest.mark.parametrize("surgery, expected_positions, expected_pair", [
    ("shrink", (2, 4), ("f2", "f4", 0, None)),
    ("reclaim_head", (1, 2, 3, 4), ("f1", "f2", 0, None)),
    ("reclaim_tail", (2, 3, 4, 5), ("f4", "f5", 2, None)),
])
def test_successful_member_surgery_preserves_capacity_bounds_cuts_and_emitted_metadata(
        monkeypatch, tmp_path, surgery, expected_positions, expected_pair):
    from labelkit.operators.verify_capacity import allows_position
    from tests.operators.test_emitter import USER_SCHEMA as EMIT_SCHEMA
    from tests.operators.test_emitter import make_cfg as emitter_cfg, read_jsonl, run_emitter

    cfg = replace(_stream_cfg(), user_schema=EMIT_SCHEMA)
    claims = _stub_judge_window(monkeypatch)
    extracts = _stub_extract(monkeypatch)
    output = {"intent": "request", "topic": "capacity", "difficulty": "easy"}
    annotations = _stub_annotate(monkeypatch, output)
    frames = [_env(_frame(f"f{i}", pair_index=i), status="dropped_noise") for i in range(7)]
    for frame in frames:
        frame.noise_attribution = ("segment", "noise")
    for position in (2, 3, 4):
        frames[position].status = "absorbed"
    episode = _episode([frames[i].record for i in (2, 3, 4)], annotation=_annotation(output),
                       transitions=(_transition(0), _transition(1)))
    before = CapacityCut(0, 1, "annotate", "default", "precheck")
    after = CapacityCut(5, 6, "quality", "judge", "reactive")
    original_capacity = SequenceCapacity(SequenceBounds(1, 6, before, after), True, "root", "parent")
    episode.capacity = original_capacity
    original_record = episode.record
    defect = (_defect("off_task_members", members=[3]) if surgery == "shrink" else
              _defect("missing_head" if surgery == "reclaim_head" else "missing_tail"))
    engine = SeqJudgeEngine({episode.record.id: [_seq_obj("fail", defects=[defect]), _seq_obj("pass")]})

    metrics = _run_verify(cfg, [*frames, episode], engine)

    assert episode.status == "active" and episode.stream_repaired
    assert episode.record is not original_record and episode.record.id == original_record.id
    assert episode.member_positions == expected_positions
    assert episode.record.members == tuple(frames[i].record for i in expected_positions)
    assert episode.capacity == original_capacity
    assert episode.capacity.bounds.lower == 1 and episode.capacity.bounds.upper == 6
    assert episode.capacity.bounds.before == before and episode.capacity.bounds.after == after
    assert not allows_position(episode, 0) and not allows_position(episode, 6)
    assert extracts == [expected_pair]
    assert len(annotations) == 1 and annotations[0].record is episode.record
    assert annotations[0].transitions == episode.transitions
    assert (episode.verification.verdict, episode.verification.rounds) == ("pass", 2)
    assert metrics.counters["verify.membership_repairs"] == 1
    assert claims == ([] if surgery == "shrink" else [["f1", "f2"]] if surgery == "reclaim_head" else [["f4", "f5"]])
    assert [frame.status for frame in frames] == [
        "absorbed" if i in expected_positions else "dropped_noise" for i in range(7)]
    assert frames[0].noise_attribution == frames[6].noise_attribution == ("segment", "noise")
    delivery_cfg = emitter_cfg(tmp_path, modality="ui", segment=cfg.segment)
    _, result = run_emitter(delivery_cfg, [episode])
    assert result.emitted == 1 and result.rejected == 0
    stream = read_jsonl(Path(delivery_cfg.paths.output))[0]["_meta"]["stream"]
    assert stream["member_positions"] == list(expected_positions) and stream["repaired"] is True
    assert stream["capacity"] == {
        "sealed": True, "allowed_positions": [1, 6],
        "before": {"left_position": 0, "right_position": 1, "stage": "annotate",
                   "profile": "default", "phase": "precheck"},
        "after": {"left_position": 5, "right_position": 6, "stage": "quality",
                  "profile": "judge", "phase": "reactive"},
        "root_id": "root", "parent_id": "parent",
    }


def test_stitch_final_task_name_survives_real_verify_reclaim_and_seam_rebuild(monkeypatch):
    from labelkit.operators.extract import _seam_placeholder
    from tests.operators.test_stitch import (
        QueueEngine, StitchStage, envelope, episode_of, make_cfg as stitch_cfg,
        make_ctx as stitch_ctx, obj, ui_frame,
    )

    frames = [envelope(ui_frame(f"f{i}", i)) for i in range(9)]
    for frame in frames:
        frame.status = "dropped_noise"
        frame.noise_attribution = ("segment", "noise")
    first = episode_of([frames[0], frames[4]])
    second = episode_of([frames[2], frames[6]])
    continuation = episode_of([frames[7], frames[8]])
    batch = [*frames, first, second, continuation]
    cfg = stitch_cfg(bias="llm", repass=False, rescue_short=False)
    stitch_engine = QueueEngine([obj(task="task-A"), obj(task="task-B"), obj("resume", 1, task="task-B-final")])

    asyncio.run(StitchStage(cfg).run(batch, stitch_ctx(cfg, stitch_engine)))

    assert continuation.status == "stitched" and second.member_positions == (2, 6, 7, 8)
    assert first.stitch_task_name == "task-A" and second.stitch_task_name == "task-B-final"
    assert first.seam_interrupted_by == (("task-B-final",),)
    for episode in (first, second):
        seams = dict(zip(episode.seam_indexes, episode.seam_interrupted_by, strict=True))
        episode.transitions = tuple(_seam_placeholder(i, seams[i]) if i in seams else _transition(i)
                                    for i in range(len(episode.record.members) - 1))
        episode.annotation = _annotation()
    claims = _stub_judge_window(monkeypatch)
    extracts = _stub_extract(monkeypatch)
    annotations = _stub_annotate(monkeypatch)
    verify_cfg = replace(_stream_cfg(), stitch=StitchConfig(enabled=True))
    judge = SeqJudgeEngine({
        first.record.id: [_seq_obj("fail", defects=[_defect("missing_members", members=[1])]), _seq_obj("pass")],
        second.record.id: [_seq_obj("pass")],
    })

    _run_verify(verify_cfg, batch, judge)

    assert first.member_positions == (0, 1, 4) and second.member_positions == (2, 6, 7, 8)
    assert first.seam_indexes == (1,) and first.seam_interrupted_by == (("task-B-final",),)
    assert first.transitions[1].detail["interrupted_by"] == ["task-B-final"]
    assert second.seam_indexes == (0,) and second.seam_interrupted_by == (("task-A",),)
    assert first.stitch_task_name == "task-A" and second.stitch_task_name == "task-B-final"
    assert claims == [["f0", "f1", "f4"]] and extracts == [("f0", "f1", 0, None)]
    assert [call.record.id for call in annotations] == [first.record.id]
    assert annotations[0].transitions[1].detail["interrupted_by"] == ["task-B-final"]
    assert first.verification.verdict == second.verification.verdict == "pass"
    assert first.verification.rounds == 2 and second.verification.rounds == 1


def test_fragment_projection_assigns_reclaim_to_previous_original_fragment():
    from labelkit.operators.verify_capacity import project_fragments
    records = [_frame("same", pair_index=index) for index in range(6)]
    episode = _episode(records)
    episode.stitch_fragments = (
        {"order_span": [1, 3], "member_count": 2, "cause": "origin", "source_episode": "left", "member_positions": [1, 3]},
        {"order_span": [5, 5], "member_count": 1, "cause": "resumed", "source_episode": "right", "member_positions": [5]},
    )
    project_fragments(episode, (1, 3, 5))
    assert [fragment["member_positions"] for fragment in episode.stitch_fragments] == [[0, 1, 2, 3, 4], [5]]
    assert [fragment["source_episode"] for fragment in episode.stitch_fragments] == ["left", "right"]
    assert sum(fragment["member_count"] for fragment in episode.stitch_fragments) == len(records)


def test_seam_rebuild_uses_actual_occurrence_ownership_and_preserved_task_names():
    from labelkit.operators.verify_capacity import plan_seams
    records = [_frame("same", pair_index=index) for index in range(5)]
    first = _episode([records[0], records[4]], eid="first")
    first.member_positions = (0, 4)
    first.stitch_task_name = "first-task"
    second = _episode([records[2]], eid="second")
    second.member_positions = (2,)
    second.stitch_task_name = "foreign-task"
    state = _EpisodeReview(first, 0)
    plan_seams([state], [first, second])
    assert state.seams == {0: ("foreign-task",)}


@pytest.mark.parametrize("batch_size", [1, 2, 100])
def test_stream_review_leaf_groups_preserve_panel_votes(batch_size):
    cfg = _stream_cfg(policy="drop", judges=("j1", "j2", "j3"))
    cfg = replace(cfg, run=replace(cfg.run, batch_size=batch_size))
    frame = _env(_frame("a"))
    episode = _episode([frame.record])
    engine = SeqJudgeEngine({episode.record.id: [_seq_obj("pass"), _seq_obj("fail"), _seq_obj("pass")]})
    runner = _TaskRunner()
    ctx = _task_context(cfg=cfg, schema_engine=engine, metrics=_CapturingMetrics(), tasks=runner)
    asyncio.run(VerifyStage(cfg).run([frame, episode], ctx))
    assert episode.verification.verdict == "pass"
    assert [len(group) for group in runner.groups] == ([1, 1, 1] if batch_size == 1 else [2, 1] if batch_size == 2 else [3])


def test_boundary_preview_includes_allowed_full_tree_and_image_but_never_crosses_cut():
    cfg = replace(_stream_cfg(), llm_profiles={"judge": _budget_profile("judge", 8192),
                                             "default": _budget_profile("default", 8192)})
    frames = [_env(_ui_frame("outside-left", "CUT_LEFT" * 20000)),
              _env(_ui_frame("member", "MEMBER")),
              _env(_ui_frame("neighbor", "NEIGHBOR_FULL_TREE")),
              _env(_ui_frame("outside-right", "CUT_RIGHT" * 20000))]
    episode = _episode([frames[1].record])
    episode.capacity = SequenceCapacity(SequenceBounds(
        1, 3, CapacityCut(0, 1, "annotate", "default", "precheck"),
        CapacityCut(2, 3, "annotate", "default", "precheck")))
    batch = [*frames, episode]
    _stamp_stream(batch)
    engine = SeqJudgeEngine({})
    ctx = _task_context(cfg=cfg, schema_engine=engine, metrics=_CapturingMetrics())
    plan = StreamVerifyDriver(VerifyStage(cfg))._plan_episode_review(_EpisodeReview(episode, 0), batch, ctx)
    parts = plan.prompt.messages[1].parts
    text = "\n".join(part.text for part in parts if part.kind == "text")
    assert frames[2].record.ui_tree.serialize(None) in text
    assert "CUT_LEFT" not in text and "CUT_RIGHT" not in text
    assert [part.image for part in parts if part.kind == "image"] == [frames[1].record.image, frames[2].record.image]
    assert engine.calls == []


def _seam_dependency_batch():
    from labelkit.operators.extract import _seam_placeholder
    frames = [_env(_frame(f"f{index}", pair_index=index),
                   status="dropped_noise" if index in (1, 3) else "absorbed") for index in range(6)]
    first = _episode([frames[0].record, frames[4].record], eid="first", transitions=(_seam_placeholder(0, ("B",)),))
    second = _episode([frames[2].record, frames[5].record], eid="second", transitions=(_seam_placeholder(0, ("A",)),))
    first.stitch_task_name, second.stitch_task_name = "A", "B"
    first.seam_indexes, second.seam_indexes = (0,), (0,)
    first.seam_interrupted_by, second.seam_interrupted_by = (("B",),), (("A",),)
    batch = [*frames, first, second]
    _stamp_stream(batch)
    return batch, first, second


@pytest.mark.parametrize("multi", [False, True])
def test_foreign_member_shrink_reopens_passed_seam_owner_with_full_repair_and_review(monkeypatch, multi):
    from labelkit.operators.classify import ClassifyStage
    cfg = replace(_stream_classified_cfg() if multi else _stream_cfg(), stitch=StitchConfig(enabled=True))
    extracts = _stub_extract(monkeypatch)
    annotations = _stub_annotate(monkeypatch)
    batch, first, second = _seam_dependency_batch()
    if multi:
        first.classification = Classification("a", ("a",), "llm", {})
        second.classification = Classification("a", ("a", "b"), "llm", {})
        ClassifyStage._fan_out(batch, [first, second])
        clone = batch[-1]
        clone.annotation, clone.transitions = _annotation({"task_label": "B"}), second.transitions
    engine = SeqJudgeEngine({first.record.id: [_seq_obj("pass"), _seq_obj("pass")],
                             second.record.id: [_seq_obj("fail", defects=[_defect("off_task_members", members=[2])]),
                                                *[_seq_obj("pass") for _ in range(2 if multi else 1)]]})
    _run_verify(cfg, batch, engine)
    assert first.member_positions == (0, 4) and second.member_positions == (5,)
    assert extracts == [("f0", "f4", 0, "a" if multi else None)]
    assert [call.record.id for call in annotations] == ["second", "first"]
    assert first.seam_indexes == () and first.seam_interrupted_by == ()
    assert first.transitions[0].action["description"] == "f0->f4"
    assert first.verification.rounds == second.verification.rounds == 2
    assert first.verification.verdict == second.verification.verdict == "pass"
    assert batch[2].status == "dropped_noise"
    if multi:
        assert clone.record.id == second.record.id and clone.member_positions == (2, 5)
        assert clone.seam_indexes == (0,) and clone.seam_interrupted_by == (("A",),)
        assert clone.verification.verdict == "pass" and clone.verification.rounds == 1


def test_multi_owner_reclaim_does_not_invent_a_self_interruption_for_its_clone(monkeypatch):
    from labelkit.operators.classify import ClassifyStage
    cfg = replace(_stream_classified_cfg(), stitch=StitchConfig(enabled=True))
    claims = _stub_judge_window(monkeypatch)
    extracts = _stub_extract(monkeypatch)
    annotations = _stub_annotate(monkeypatch)
    frames = [_env(_frame(f"f{i}", pair_index=i), status="absorbed" if i in (0, 4) else "dropped_noise")
              for i in range(5)]
    owner = _episode([frames[0].record, frames[4].record], transitions=(_transition(0),),
                     classification=Classification("a", ("a", "b"), "llm", {}))
    owner.stitch_task_name = "same-task"
    owner.seam_indexes, owner.seam_interrupted_by = (), ()
    batch = [*frames, owner]
    _stamp_stream(batch)
    ClassifyStage._fan_out(batch, [owner])
    clone = batch[-1]
    clone.annotation, clone.transitions = _annotation({"task_label": "B"}), owner.transitions
    original_clone_annotation = clone.annotation
    engine = SeqJudgeEngine({owner.record.id: [
        _seq_obj("fail", defects=[_defect("missing_members", members=[2])]), _seq_obj("pass"), _seq_obj("pass")]})
    _run_verify(cfg, batch, engine)
    assert owner.member_positions == (0, 2, 4) and clone.member_positions == (0, 4)
    assert frames[2].status == "absorbed" and frames[1].status == frames[3].status == "dropped_noise"
    assert claims == [["f0", "f2", "f4"]]
    assert extracts == [("f0", "f2", 0, "a"), ("f2", "f4", 1, "a")]
    assert [(call.record.id, call.label) for call in annotations] == [(owner.record.id, "a")]
    assert clone.seam_indexes == clone.seam_interrupted_by == ()
    assert owner.seam_indexes == owner.seam_interrupted_by == ()
    assert clone.annotation is original_clone_annotation
    assert owner.verification.rounds == 2 and clone.verification.rounds == 1
    assert owner.verification.verdict == clone.verification.verdict == "pass"


def test_same_task_name_from_an_independent_sequence_still_interrupts_a_clone():
    from labelkit.operators.classify import ClassifyStage
    from labelkit.operators.verify_capacity import current_seams
    frames = [_frame(f"f{i}", pair_index=i) for i in range(5)]
    owner = _episode([frames[0], frames[4]], eid="owner",
                     classification=Classification("a", ("a", "b"), "llm", {}))
    owner.member_positions, owner.stitch_task_name = (0, 4), "same-task"
    batch = [owner]
    ClassifyStage._fan_out(batch, [owner])
    clone = batch[-1]
    owner.record, owner.member_positions = replace(owner.record, members=(frames[0], frames[2], frames[4])), (0, 2, 4)
    assert current_seams(clone, batch) == {}
    foreign = _episode([frames[3]], eid="foreign")
    foreign.member_positions, foreign.stitch_task_name = (3,), "same-task"
    assert current_seams(clone, [*batch, foreign]) == {0: ("same-task",)}


def test_clone_seam_dependency_preserves_owner_reclaimed_shared_frame_products(monkeypatch):
    from labelkit.operators.classify import ClassifyStage
    cfg = replace(_frame_stream_cfg(_stream_classified_cfg()), stitch=StitchConfig(enabled=True))
    _stub_judge_window(monkeypatch)
    _stub_extract(monkeypatch)
    annotations = _stub_annotate(monkeypatch)
    classifications = _stub_classify_frames(monkeypatch)
    frame_annotations = _stub_annotate_member(monkeypatch)
    batch, first, second = _seam_dependency_batch()
    first.classification = Classification("a", ("a", "b"), "llm", {})
    second.classification = Classification("a", ("a",), "llm", {})
    first.member_classifications = {0: _member_cls(), 4: _member_cls()}
    first.member_annotations = {0: _annotation({"intent": "frame"}), 4: None}
    ClassifyStage._fan_out(batch, [first, second])
    clone = batch[-1]
    clone.annotation, clone.transitions = _annotation({"task_label": "A"}), first.transitions
    engine = SeqJudgeEngine({first.record.id: [
        _seq_obj("fail", defects=[_defect("missing_members", members=[1])]),
        _seq_obj("pass"), _seq_obj("pass"), _seq_obj("pass")], second.record.id: [
        _seq_obj("fail", defects=[_defect("off_task_members", members=[2])]), _seq_obj("pass")]})
    _run_verify(cfg, batch, engine)
    assert first.member_positions == (0, 1, 4) and clone.member_positions == (0, 4)
    assert first.member_classifications is clone.member_classifications
    assert first.member_annotations is clone.member_annotations
    assert set(first.member_classifications) == set(first.member_annotations) == {0, 1, 4}
    assert classifications == [["f1"]] and frame_annotations == [("f1", "task_request")]
    assert [(call.record.id, call.label) for call in annotations] == [("first", "a"), ("second", "a"), ("first", "b")]
    assert clone.seam_indexes == clone.seam_interrupted_by == ()
    assert first.verification.verdict == second.verification.verdict == clone.verification.verdict == "pass"
    assert first.verification.rounds == second.verification.rounds == clone.verification.rounds == 2


def test_shrink_reorders_interleaved_fragments_by_first_surviving_position(monkeypatch):
    cfg = replace(_stream_cfg(), stitch=StitchConfig(enabled=True))
    extracts = _stub_extract(monkeypatch)
    annotations = _stub_annotate(monkeypatch)
    frames = [_env(_frame(f"f{i}", pair_index=i), status="absorbed" if i in (0, 4, 6, 8) else "dropped_noise")
              for i in range(9)]
    episode = _episode([frames[i].record for i in (0, 4, 6, 8)],
                       transitions=tuple(_transition(i) for i in range(3)))
    episode.stitch_task_name = "task"
    episode.stitch_fragments = (
        {"order_span": [0, 8], "member_count": 2, "cause": "origin", "source_episode": "left", "member_positions": [0, 8]},
        {"order_span": [4, 6], "member_count": 2, "cause": "resumed", "source_episode": "right", "member_positions": [4, 6]},
    )
    engine = SeqJudgeEngine({episode.record.id: [
        _seq_obj("fail", defects=[_defect("off_task_members", members=[0])]), _seq_obj("pass")]})
    _run_verify(cfg, [*frames, episode], engine)
    assert episode.member_positions == (4, 6, 8) and frames[0].status == "dropped_noise"
    assert [fragment["member_positions"] for fragment in episode.stitch_fragments] == [[4, 6], [8]]
    assert [fragment["source_episode"] for fragment in episode.stitch_fragments] == ["right", "left"]
    assert [fragment["cause"] for fragment in episode.stitch_fragments] == ["resumed", "origin"]
    assert [fragment["member_count"] for fragment in episode.stitch_fragments] == [2, 1]
    assert all(list(fragment) == ["order_span", "member_count", "cause", "source_episode", "member_positions"]
               for fragment in episode.stitch_fragments)
    assert extracts == [] and len(annotations) == 1
    assert [transition.index for transition in episode.transitions] == [0, 1]
    assert episode.verification.verdict == "pass" and episode.verification.rounds == 2


def test_dependency_capacity_failure_restores_surgeon_and_previously_passed_owner(monkeypatch):
    from labelkit.common.errors import SessionCapacityError
    cfg = replace(_stream_cfg(), stitch=StitchConfig(enabled=True))
    _stub_extract(monkeypatch)
    batch, first, second = _seam_dependency_batch()
    originals = [dict(item.__dict__) for item in batch]

    async def annotate(record, ctx, opts=None):
        if record.id == "first":
            raise ContextOverflowError("dependency overflow", profile="default", phase="reactive", origin="finish")
        return _annotation({"task_label": "updated"})

    monkeypatch.setattr("labelkit.operators.annotate.annotate_record_leaf", annotate)
    engine = SeqJudgeEngine({first.record.id: [_seq_obj("pass")], second.record.id: [
        _seq_obj("fail", defects=[_defect("off_task_members", members=[2])])]})
    ctx = _task_context(cfg=cfg, schema_engine=engine, metrics=_CapturingMetrics())
    with pytest.raises(SessionCapacityError) as captured:
        asyncio.run(VerifyStage(cfg).run(batch, ctx))
    assert captured.value.failures[0].stage == "verify"
    assert captured.value.failures[0].targets[0].member_positions == (0, 4)
    assert [item.__dict__ for item in batch] == originals


def test_seam_dependency_preserves_exhausted_round_budget_and_fails_stale_result(caplog):
    cfg = replace(_stream_cfg(max_repair_rounds=1), stitch=StitchConfig(enabled=True))
    batch, first, second = _seam_dependency_batch()
    second.record = replace(second.record, members=second.record.members[1:])
    second.member_positions = (5,)
    state = _EpisodeReview(first, 0)
    state.rounds, state.verdict = 2, "pass"
    ctx = _task_context(cfg=cfg, schema_engine=SeqJudgeEngine({}), metrics=_CapturingMetrics())
    assert StreamVerifyDriver(VerifyStage(cfg))._seam_dependents([state], batch, ctx) == []
    assert first.status == "dropped_verify" and first.verification.verdict == "fail"
    assert first.verification.rounds == 2
    assert first.verification.defects[0]["detail"] == "Seam dependency repair budget exhausted."
    assert "seam dependency repair budget exhausted" in caplog.text


@pytest.mark.parametrize("batch_size", [1, 100])
@pytest.mark.parametrize("nested", [False, True])
def test_review_wave_reports_every_actual_overflow_across_computation_groups(batch_size, nested):
    from labelkit.common.contracts.sequence_capacity import capacity_failures, capacity_target
    from labelkit.common.errors import SessionCapacityError
    cfg = _stream_cfg(policy="drop")
    cfg = replace(cfg, run=replace(cfg.run, batch_size=batch_size))
    frames = [_env(_frame("left")), _env(_frame("right"))]
    episodes = [_episode([frame.record], eid=frame.record.id) for frame in frames]
    engine = SeqJudgeEngine({episode.record.id: [ContextOverflowError(
        "actual overflow", profile="judge", phase="reactive", origin="http_400")] for episode in episodes})
    batch = [*frames, *episodes]
    _stamp_stream(batch)
    runner = _TaskRunner()
    ctx = _task_context(cfg=cfg, schema_engine=engine, metrics=_CapturingMetrics(), tasks=runner)
    if nested:
        episode = episodes[1]
        engine.scripts[episode.record.id][0] = SessionCapacityError(capacity_failures(
            ctx, (capacity_target(episode),), engine.scripts[episode.record.id][0], "sequence"))
    with pytest.raises(SessionCapacityError) as captured:
        asyncio.run(VerifyStage(cfg).run(batch, ctx))
    assert [failure.targets[0].member_positions for failure in captured.value.failures] == [(0,), (1,)]
    assert len(engine.calls) == 2
    assert [len(group) for group in runner.groups] == ([1, 1] if batch_size == 1 else [2])
    assert all(scope.complete_evidence for scope in engine.scopes)
    assert all(episode.verification is None and episode.status == "active" for episode in episodes)


def test_review_planning_collects_all_episode_and_judge_capacity_failures_before_calls():
    from labelkit.common.errors import SessionCapacityError
    cfg = _stream_cfg(judges=("j1", "j2", "j3"))
    cfg = replace(cfg, llm_profiles={name: _budget_profile(name, 8192)
                                     for name in ("j1", "j2", "j3", "judge", "default")})
    frames = [_env(_text_member(name, "full evidence " * 20000)) for name in ("1-left", "2-right")]
    episodes = [_episode([frame.record], eid=frame.record.id) for frame in frames]
    batch = [*frames, *episodes]
    _stamp_stream(batch)
    engine = SeqJudgeEngine({})
    ctx = _task_context(cfg=cfg, schema_engine=engine, metrics=_CapturingMetrics())
    with pytest.raises(SessionCapacityError) as captured:
        asyncio.run(VerifyStage(cfg).run(batch, ctx))
    assert [(failure.targets[0].member_positions, failure.error.profile) for failure in captured.value.failures] == [
        ((position,), profile) for position in range(2) for profile in ("j1", "j2", "j3")]
    assert engine.calls == [] and all(episode.status == "active" for episode in episodes)


@pytest.mark.parametrize("phase", ["claim", "reseam", "reannotate", "frame_classify", "frame_annotate"])
def test_each_repair_wave_collects_all_capacity_outcomes_before_any_product_commit(monkeypatch, phase):
    from labelkit.common.errors import SessionCapacityError
    from labelkit.operators.classify import _FrameWindowOutcome
    cfg = _frame_stream_cfg()
    cfg = replace(cfg, run=replace(cfg.run, batch_size=1))
    frames = [_env(_frame(f"f{index}", pair_index=index)) for index in range(6)]
    episodes = [_episode([frame.record for frame in frames[start:start + 3]], eid=f"ep{start}",
                         transitions=(_transition(0), _transition(1))) for start in (0, 3)]
    _stamp_stream([*frames, *episodes])
    states = [_EpisodeReview(episode, index) for index, episode in enumerate(episodes)]
    runner = _TaskRunner()
    ctx = _task_context(cfg=cfg, schema_engine=SeqJudgeEngine({}), metrics=_CapturingMetrics(), tasks=runner)
    driver = StreamVerifyDriver(VerifyStage(cfg))
    calls = []

    async def fail(*args, **kwargs):
        calls.append(args)
        raise ContextOverflowError("wave overflow", profile="default", phase="reactive", origin="finish")

    if phase == "claim":
        monkeypatch.setattr("labelkit.operators.segment._call_window", fail)
        for state, middle in zip(states, (1, 4)):
            state.working_members.pop(1)
            state.working_positions.pop(1)
            frames[middle].status = "dropped_noise"
            state.claims = [driver._make_claim(state, frames[middle], middle)]
        operation = driver._resolve_claims(states, ctx)
    elif phase == "reseam":
        monkeypatch.setattr("labelkit.operators.extract._extract_transition_outcome", fail)
        for state in states:
            state.working_members.pop(1)
            state.working_positions.pop(1)
            state.surgical = True
        operation = driver._reseam_episodes(states, ctx)
    elif phase == "reannotate":
        monkeypatch.setattr("labelkit.operators.annotate.annotate_record_leaf", fail)
        operation = driver._reannotate_round(states, ctx)
    elif phase == "frame_classify":
        async def frame_failure(plan, span, context):
            calls.append(plan)
            error = ContextOverflowError("frame overflow", profile="default", phase="reactive", origin="finish")
            return _FrameWindowOutcome(((span, error),), 1, 0)
        monkeypatch.setattr("labelkit.operators.classify._run_frame_plan", frame_failure)
        for episode in episodes:
            episode.member_classifications = {}
        operation = driver._backfill_frame_classify(states, ctx)
    else:
        monkeypatch.setattr("labelkit.operators.annotate.annotate_member_leaf", fail)
        for episode in episodes:
            episode.member_annotations = {}
        operation = driver._backfill_frame_annotate(states, ctx)
    with pytest.raises(SessionCapacityError) as captured:
        asyncio.run(operation)
    expected = 6 if phase.startswith("frame_") else 2
    assert len(captured.value.failures) == len(calls) == expected
    assert all(failure.stage == "verify" for failure in captured.value.failures)
    assert [len(group) for group in runner.groups] == [1] * expected
    assert all(not episode.member_classifications and not episode.member_annotations for episode in episodes)


def test_frame_planning_collects_every_synchronous_capacity_error(monkeypatch):
    from labelkit.common.errors import SessionCapacityError
    cfg = _frame_stream_cfg()
    episodes = [_episode([_frame(f"f{index}")], eid=f"e{index}") for index in range(3)]
    for index, episode in enumerate(episodes):
        episode.member_positions = (index,)
        episode.member_classifications = {}
    calls = []

    def fail(members, ctx, episode_id, item_ordinal, target=None):
        calls.append(target.member_positions)
        raise ContextOverflowError("plan overflow", profile="default", phase="precheck")

    monkeypatch.setattr("labelkit.operators.classify._plan_frame_episode", fail)
    ctx = _task_context(cfg=cfg, schema_engine=SeqJudgeEngine({}), metrics=_CapturingMetrics())
    with pytest.raises(SessionCapacityError) as captured:
        StreamVerifyDriver(VerifyStage(cfg))._plan_frame_classify_jobs(
            [_EpisodeReview(episode, index) for index, episode in enumerate(episodes)], ctx)
    assert calls == [(0,), (1,), (2,)]
    assert [failure.targets[0].member_positions for failure in captured.value.failures] == calls
    assert all(episode.status == "active" for episode in episodes)

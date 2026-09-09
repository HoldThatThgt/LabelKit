"""完整序列证据、显式出现位置及会话容量信号的算子边界测试。"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from types import SimpleNamespace

import pytest

from labelkit.common.config.model import (
    ClassifyConfig, ClassSpec, ClassView, Criterion, ExtractConfig, FrameAnnotateConfig, FrameClassView,
    FrameClassifyConfig, LLMProfile, QualityConfig, Rubric, SegmentConfig,
)
from labelkit.common.contracts.sequence_capacity import (
    CapacityTarget, SessionAttemptScope, SessionCapacityFailure, capacity_target,
)
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import Classification, PipelineItem, SequenceBounds, SequenceCapacity, Usage
from labelkit.common.errors import ContextOverflowError, ProviderFatalError, SessionCapacityError
from labelkit.common.inference.sequence_evidence import record_evidence
from labelkit.operators.annotate import AnnotatePromptOptions, AnnotateStage, build_annotate_prompt
from labelkit.operators.classify import ClassifyStage
from labelkit.operators.extract import ExtractStage, extract_transition_for_item
from labelkit.operators.quality import QualityStage

from test_annotate import SCHEMA_TEXT, USER_SCHEMA, make_cfg, make_episode, text_member, ui_member


class PostprocessorSequenceLeaf:
    """进程内纯叶对象；不实现网络，真实 SchemaEngine 负责解析、后处理和完整校验。"""

    def __init__(self):
        self.calls = []
        self.prompts = []
        self.calibrator = SimpleNamespace(cost=lambda profile: 0)

    async def complete(self, profile, prompt, response_schema=None):
        from labelkit.common.inference.llm_client import LLMResponse

        text = content(prompt)
        is_review = "defects" in response_schema["properties"]
        self.calls.append(("verify" if is_review else "annotate", text, response_schema))
        self.prompts.append(prompt)
        if is_review:
            value = {"critiques": [], "defects": [], "verdict": "pass"}
        else:
            assert "expanded" not in response_schema["properties"]
            value = {"positions": [int(value) for value in re.findall(r"POSITION=(\d+)", text)]}
        return LLMResponse(json.dumps(value), None, Usage(9, 3), "in-process-unit-leaf", 0)


def postprocessor_session_setup(tmp_path, mode):
    """装配实际工程文件、SchemaEngine、算子和会话控制器，仅叶响应留在进程内。"""
    from labelkit.common.extensions.hooks import load_hook
    from labelkit.common.extensions.postprocessing import project_postprocessor_schema
    from labelkit.common.inference import budget
    from labelkit.common.inference.schema_engine import SchemaEngine
    from labelkit.operators.dedup import DedupIndex, DedupStage
    from labelkit.operators.verify import VerifyPromptOptions, VerifyStage, boundary_margin_text
    from labelkit.operators.verify_capacity import review_request
    from labelkit.orchestration.process_workflow import ProcessWorkflow
    from labelkit.orchestration.session_workflow import SessionWorkflow
    from tests.orchestration.test_process_workflow import FakeMetrics, services
    from tests.orchestration.test_session_workflow import CapturingEmitter, PartitionStage

    padding = "5000" if mode == "minimum" else f"{1000 + int(mode == 'split')} * len(obj['positions'])"
    (tmp_path / "project_postprocessor.py").write_text(
        "calls = []\ndef complete(obj, record):\n"
        "    assert record is None\n    calls.append(tuple(obj['positions']))\n"
        f"    obj['expanded'] = '扩' * ({padding})\n    return obj\n", encoding="utf-8")
    hook = load_hook("project_postprocessor.py:complete", tmp_path)
    full_schema = {"type": "object", "properties": {
        "positions": {"type": "array", "items": {"type": "integer"}},
        "expanded": {"type": "string", "x-labelkit-postprocessor": True}},
        "required": ["positions", "expanded"], "additionalProperties": False}
    cfg = configuration()
    cfg = replace(cfg, user_schema=full_schema, model_user_schema=project_postprocessor_schema(full_schema),
                  annotate=replace(cfg.annotate, resolved_postprocessor=hook),
                  quality=replace(cfg.quality, enabled=False), verify=replace(cfg.verify, enabled=True, llm="default"),
                  dedup=replace(cfg.dedup, minhash_threshold=0.95),
                  run=replace(cfg.run, output=str(tmp_path / "out.jsonl")))
    records = tuple(replace(text_member(index), text=f"POSITION={index} 完整成员证据") for index in range(2))
    item = item_with_members(records)
    from labelkit.common.contracts.types import Annotation

    item.annotation = Annotation({"positions": [0, 1], "expanded": "扩" * 2000}, "unit", 1, Usage(0, 0))
    frames = [PipelineItem(record, session_id="session", session_position=index, status="absorbed")
              for index, record in enumerate(records)]
    probe_ctx = context(cfg, Engine(), "verify")
    options = VerifyPromptOptions(member_positions=(0, 1), boundary_margin=boundary_margin_text(item, [*frames, item]))
    request = review_request(VerifyStage(cfg), item, probe_ctx, options, "default")
    profile = cfg.llm_profiles["default"]
    exact_budget = budget.est_prompt(request.prompt, profile, None, image_cost=0)
    window = next(value for value in range(321, 20000)
                  if budget.input_budget(replace(profile, context_window=value)) == exact_budget)
    cfg = replace(cfg, llm_profiles={"default": replace(profile, context_window=window)})
    metrics, leaf = FakeMetrics(), PostprocessorSequenceLeaf()
    engine = SchemaEngine(full_schema, leaf, cfg.output, metrics)
    observed = {"starts": [], "completed": [], "reviewed": [], "failures": []}

    class ObservedAnnotate(AnnotateStage):
        async def run(self, batch, ctx):
            active = [item for item in batch if item.status == "active"]
            observed["starts"].extend((ctx.session_attempt.attempt, item, item.annotation) for item in active)
            await super().run(batch, ctx)
            observed["completed"].extend((ctx.session_attempt.attempt, item, item.annotation) for item in active)
            return batch

    class ObservedVerify(VerifyStage):
        async def run(self, batch, ctx):
            observed["reviewed"].extend(item.member_positions for item in batch if item.status == "active")
            try:
                return await super().run(batch, ctx)
            except SessionCapacityError as error:
                observed["failures"].extend(error.failures)
                raise

    dedup = DedupStage(cfg.dedup, DedupIndex(cfg.dedup, "text"))
    emitter, segment = CapturingEmitter(cfg), PartitionStage()
    workflow = ProcessWorkflow(cfg, [segment, dedup, ObservedAnnotate(cfg), ObservedVerify(cfg)], None,
                               emitter, services(metrics, leaf, engine))
    driver = SessionWorkflow(workflow, "session", 1)
    return SimpleNamespace(cfg=cfg, records=records, driver=driver, emitter=emitter, dedup=dedup,
                           metrics=metrics, leaf=leaf, engine=engine, observed=observed, hook=hook,
                           exact_budget=exact_budget, segment=segment)


class Requests:
    def __init__(self):
        self.groups = []

    async def run_group(self, request):
        self.groups.append(request)
        return tuple([await task.operation() for task in request.tasks])


class Metrics:
    def __init__(self):
        self.counters = {}
        self.events = []
        self.fed = []

    def count(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n

    def event(self, event, **values):
        self.events.append((event, values))

    def record_provider_result(self, fatal, *, hard=False):
        self.fed.append(fatal)


class Engine:
    user_schema_text = SCHEMA_TEXT

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def complete_validated(self, profile, prompt, schema=None, *, scope, **kwargs):
        self.calls.append((profile, prompt, schema, scope))
        if self.error is not None:
            raise self.error
        properties = schema.get("properties", {}) if schema is not None else {}
        if "class" in properties:
            value = {"class": "main"}
        elif "classes" in properties:
            value = {"classes": ["main", "other"]}
        elif "labels" in properties:
            value = {"labels": ["main"] * properties["labels"]["minItems"]}
        elif "action_type" in properties:
            value = {"action_type": "wait", "target": None, "value": None, "description": "完整动作"}
        elif "scores" in properties:
            value = {"scores": [{"key": "complete", "score": 5, "reason": "完整"}]}
        elif "judgments" in properties:
            value = {"judgments": [{"criterion": "complete", "winner": "tie", "reason": "完整"}]}
        else:
            value = {"intent": "read", "topic": "sequence", "difficulty": "easy"}
        return value, Usage(17, 5), 1, "unit-engine"


def configuration(modality="text", *, context_window=131072, batch_size=2):
    cfg = make_cfg(modality=modality, ui_tree_max_chars=30)
    profile = LLMProfile("default", "openai_compatible", "http://unused.invalid/v1", "unit", "UNUSED",
                         supports_vision=True, context_window=context_window, max_output_tokens=64)
    criterion = Criterion("complete", "内容完整", "哪条记录完整？", pointwise_levels=tuple(str(i) for i in range(6)))
    return replace(cfg, segment=SegmentConfig(enabled=True), llm_profiles={"default": profile},
                   run=replace(cfg.run, batch_size=batch_size), quality=QualityConfig(mode="pointwise"),
                   rubric=Rubric("complete", (criterion,)), model_frame_schema=USER_SCHEMA, frame_schema=USER_SCHEMA)


def item_with_members(members, *, start=0, record_id="sequence"):
    positions = tuple(range(start, start + len(members)))
    return PipelineItem(record=make_episode(tuple(members), record_id), session_id="session",
                        member_positions=positions,
                        capacity=SequenceCapacity(SequenceBounds(0, 1000), root_id=record_id))


def context(cfg, engine, stage, failures=()):
    return RunContext(cfg=cfg, llm=SimpleNamespace(calibrator=SimpleNamespace(cost=lambda profile: 37)),
                      schema_engine=engine, rng=__import__("random").Random(7), batch_no=1, metrics=Metrics(),
                      tasks=Requests(), task_namespace=f"session:1:attempt:1:{stage}",
                      session_attempt=SessionAttemptScope("session", 1, 1, stage, failures))


def content(prompt):
    return "\n".join(part.text or "" for message in prompt.messages for part in message.parts if part.kind == "text")


def images(prompt):
    return [part.image for message in prompt.messages for part in message.parts if part.kind == "image"]


@pytest.mark.parametrize("stage_type", [ClassifyStage, QualityStage, AnnotateStage])
@pytest.mark.parametrize("modality", ["text", "ui"])
def test_actual_sequence_requests_keep_every_member(stage_type, modality):
    cfg = configuration(modality)
    if stage_type is ClassifyStage:
        cfg = replace(cfg, classify=ClassifyConfig(enabled=True, max_labels=1, fallback_class="other",
                      classes=(ClassSpec("main", "目标"), ClassSpec("other", "其他"))))
    members = [ui_member(i) if modality == "ui" else replace(text_member(i), text=f"开始{i}" + "内容" * 500 + f"末尾{i}")
               for i in range(25)]
    item = item_with_members(members)
    engine = Engine()
    ctx = context(cfg, engine, stage_type.name)
    asyncio.run(stage_type(cfg).run([item], ctx))
    assert item.status == "active"
    assert len(engine.calls) == 1
    prompt = engine.calls[0][1]
    for member in members:
        assert record_evidence(member) in content(prompt)
    assert images(prompt) == ([member.image for member in members] if modality == "ui" else [])
    assert "truncated" not in content(prompt)
    assert not any(key.startswith("budget.truncations") for key in ctx.metrics.counters)


def test_annotation_complete_steps_and_repair_suffix_survive_small_tree_cap():
    from test_annotate import RepairContext, SEQ_TRANSITIONS

    cfg = configuration("ui")
    members = tuple(ui_member(i) for i in range(25))
    transitions = tuple(replace(SEQ_TRANSITIONS[0], index=i,
                               action={**SEQ_TRANSITIONS[0].action, "description": f"步骤{i}" + "动作" * 500})
                        for i in range(24))
    options = AnnotatePromptOptions(transitions=transitions,
                                    repair=RepairContext({"intent": "read"}, "中间帧决定结论"))
    prompt = build_annotate_prompt(make_episode(members), cfg, SCHEMA_TEXT, options)
    assert images(prompt) == [member.image for member in members]
    for transition in transitions:
        assert transition.action["description"] in content(prompt)
    assert "中间帧决定结论" in prompt.messages[-1].parts[-1].text


@pytest.mark.parametrize("stage_type", [ClassifyStage, ExtractStage, QualityStage, AnnotateStage])
def test_reactive_capacity_preserves_original_error_before_record_failure(stage_type):
    cfg = configuration("ui")
    if stage_type is ClassifyStage:
        cfg = replace(cfg, classify=ClassifyConfig(enabled=True, max_labels=1, fallback_class="other",
                      classes=(ClassSpec("main", "目标"), ClassSpec("other", "其他"))))
    if stage_type is ExtractStage:
        cfg = replace(cfg, extract=ExtractConfig(enabled=True, on_error="fallback"))
    item = item_with_members([ui_member(0), ui_member(1), ui_member(2)])
    original = ContextOverflowError("token limit", profile="default", phase="reactive")
    engine = Engine(original)
    ctx = context(cfg, engine, stage_type.name)
    with pytest.raises(SessionCapacityError) as caught:
        asyncio.run(stage_type(cfg).run([item], ctx))
    failure = caught.value.failures[0]
    assert failure.error is original
    assert failure.stage == stage_type.name
    assert failure.unit == ("transition" if stage_type is ExtractStage else "sequence")
    assert failure.targets[0].member_positions == ((0, 1) if stage_type is ExtractStage else (0, 1, 2))
    assert item.status == "active" and item.errors == []
    assert item.annotation is None and item.transitions is None
    assert ctx.metrics.fed == []
    assert ctx.metrics.counters.get("budget.overflow_records", 0) == 0


def test_pairwise_capacity_contains_both_views_without_tie_or_breaker_feed():
    cfg = configuration("ui")
    cfg = replace(cfg, quality=QualityConfig(mode="pairwise", rounds=1))
    batch = [item_with_members([ui_member(i) for i in range(3)], record_id="a"),
             item_with_members([ui_member(i) for i in range(3, 5)], start=3, record_id="b")]
    original = ContextOverflowError("token limit", profile="default", phase="reactive")
    engine = Engine(original)
    ctx = context(cfg, engine, "quality")
    with pytest.raises(SessionCapacityError) as caught:
        asyncio.run(QualityStage(cfg).run(batch, ctx))
    assert caught.value.failures[0].unit == "pairwise"
    assert {target.record_id for target in caught.value.failures[0].targets} == {"a", "b"}
    assert len(images(engine.calls[0][1])) == 5
    assert all(item.status == "active" and not item.scores and not item.errors for item in batch)
    assert ctx.metrics.fed == []


@pytest.mark.parametrize("stage_type", [ClassifyStage, ExtractStage, QualityStage, AnnotateStage])
def test_preview_is_pure_and_identifies_fixed_overhead(stage_type):
    cfg = configuration("ui", context_window=700)
    huge = "固定指令" * 500
    cfg = replace(cfg, classify=ClassifyConfig(enabled=True, instruction=huge, max_labels=1,
                  fallback_class="fallback", classes=(ClassSpec("main", "目标"), ClassSpec("other", "其他"),
                                                      ClassSpec("fallback", "兜底"))),
                  extract=ExtractConfig(enabled=True, instruction=huge),
                  annotate=replace(cfg.annotate, instruction=huge),
                  rubric=Rubric("huge", (Criterion("complete", huge, huge, pointwise_levels=(huge,) * 6),)))
    cfg = replace(cfg, classify=replace(cfg.classify, enabled=stage_type is ClassifyStage))
    item = item_with_members([ui_member(0), ui_member(1)])
    engine = Engine()
    ctx = context(cfg, engine, stage_type.name)
    before = dict(item.__dict__)
    failure = stage_type(cfg).preview_capacity(item, ctx)
    assert failure is not None and failure.unit == ("transition" if stage_type is ExtractStage else "fixed")
    assert failure.error.profile == "default" and failure.error.phase == "precheck"
    assert engine.calls == [] and ctx.metrics.counters == {} and ctx.metrics.events == []
    assert item.__dict__ == before


@pytest.mark.parametrize("path", ["classify", "frame_classify", "annotate", "quality_point", "quality_pair"])
def test_complete_fixed_envelope_overflow_does_not_split_or_call_model(path):
    from labelkit.common.inference import budget
    from labelkit.common.inference.llm_client import PromptBundle
    from labelkit.operators.annotate_capacity import sequence_request as annotate_request
    from labelkit.operators.classify_capacity import frame_request, sequence_request as classify_request
    from labelkit.operators.quality_capacity import _preview_requests
    from labelkit.orchestration.session_capacity import SessionPartition
    from test_annotate import SEQ_TRANSITIONS

    cfg = configuration()
    classes = (ClassSpec("main", "目标", examples=("固定类别示例",)), ClassSpec("other", "其他"))
    item = item_with_members([text_member(0), text_member(1)])
    item.transitions = SEQ_TRANSITIONS
    if path == "classify":
        cfg = replace(cfg, classify=ClassifyConfig(enabled=True, max_labels=1, fallback_class="other", classes=classes))
        stage = ClassifyStage(cfg)
        request = classify_request(item.record, cfg)
    elif path == "frame_classify":
        cfg = replace(cfg, frame_classify=FrameClassifyConfig(enabled=True, fallback_class="other", classes=classes))
        stage = ClassifyStage(cfg)
        request = frame_request(item.record.members, cfg)
    elif path == "annotate":
        stage = AnnotateStage(cfg)
        request = annotate_request(item.record, context(cfg, Engine(), stage.name), SCHEMA_TEXT,
                                   AnnotatePromptOptions(transitions=item.transitions))
    else:
        cfg = replace(cfg, quality=replace(cfg.quality, mode="pairwise" if path == "quality_pair" else "pointwise"))
        stage = QualityStage(cfg)
        request = next(_preview_requests(stage, item, cfg))
    profile = cfg.llm_profiles["default"]
    schema = request.schema if profile.supports_structured_output else None
    fixed_cost = budget.est_prompt(request.fixed_prompt, profile, schema, image_cost=0)
    system_cost = budget.est_prompt(PromptBundle(messages=request.fixed_prompt.messages[:-1]), profile, schema,
                                    image_cost=0)
    window = next(value for value in range(321, 10000)
                  if budget.input_budget(replace(profile, context_window=value)) == fixed_cost - 1)
    profile = replace(profile, context_window=window)
    assert system_cost <= budget.input_budget(profile) < fixed_cost
    cfg = replace(cfg, llm_profiles={"default": profile})
    engine = Engine()
    ctx = context(cfg, engine, stage.name)
    stage = type(stage)(cfg)
    assert stage.preview_capacity(item, ctx).unit == "fixed"
    batch = [item]
    if path == "quality_pair":
        other = item_with_members([text_member(2), text_member(3)], start=2, record_id="other")
        other.transitions = SEQ_TRANSITIONS
        batch.append(other)
    partition = SessionPartition(batch, 4)
    with pytest.raises(SessionCapacityError) as caught:
        asyncio.run(stage.run(batch, ctx))
    assert caught.value.failures and all(failure.unit == "fixed" for failure in caught.value.failures)
    assert all(partition.split(failure) is None for failure in caught.value.failures)
    assert partition.net_episodes == 0 and len(partition.items) == len(batch)
    assert engine.calls == []


def test_annotation_fixed_baseline_preserves_steps_label_examples_and_repair():
    from labelkit.operators.annotate_capacity import sequence_request
    from test_annotate import RepairContext, SEQ_TRANSITIONS

    cfg = configuration()
    item = item_with_members([text_member(0), text_member(1)])
    repair = RepairContext({"intent": "read"}, "修复必须保留完整固定后缀")
    request = sequence_request(item.record, context(cfg, Engine(), "annotate"), SCHEMA_TEXT,
                               AnnotatePromptOptions(transitions=SEQ_TRANSITIONS, repair=repair))
    fixed = content(request.fixed_prompt)
    assert "[动作序列]" in fixed and "[序列成员]" in fixed and "修复必须保留完整固定后缀" in fixed
    assert len(request.fixed_prompt.messages) == len(request.prompt.messages)
    assert all(member.text not in fixed for member in item.record.members)
    assert images(request.fixed_prompt) == []


@pytest.mark.parametrize("mode", ["fit", "split", "minimum"])
def test_project_postprocessor_expansion_reaches_real_schema_verify_and_session_capacity(tmp_path, mode):
    from labelkit.common.inference import budget
    from labelkit.orchestration.session_capacity import validate_session

    run = postprocessor_session_setup(tmp_path, mode)
    assert run.driver.checker.preview(item_with_members(run.records), run.driver._context("segment", 0)) is None
    frames = [PipelineItem(record, session_id="session", session_position=index)
              for index, record in enumerate(run.records)]
    run.emitter.open()
    asyncio.run(run.driver.run(frames))
    assert frames == [] and run.segment.calls == 1 and len(run.emitter.products) == 1
    final = run.emitter.products[0]
    validate_session(list(final))
    sequences = [item for item in final if item.record.kind == "sequence"]
    positions = [position for item in sequences for position in item.member_positions]
    assert positions == [0, 1] and run.metrics.counters["counts.absorbed"] == 2
    assert all(member is run.records[position] for item in sequences
               for position, member in zip(item.member_positions, item.record.members, strict=True))
    assert all(previous is None for _attempt, _item, previous in run.observed["starts"])
    assert all(item.annotation.output["positions"] == list(item.member_positions) for item in sequences)
    assert set(run.dedup.index._digest_by_id) == {item.record.id for item in sequences}
    profile = run.cfg.llm_profiles["default"]
    assert budget.input_budget(profile) == run.exact_budget
    verify_calls = [text for stage, text, _schema in run.leaf.calls if stage == "verify"]
    verify_requests = [prompt for (stage, _text, _schema), prompt in zip(run.leaf.calls, run.leaf.prompts, strict=True)
                       if stage == "verify"]
    annotations = [text for stage, text, _schema in run.leaf.calls if stage == "annotate"]
    assert len(run.hook.target.__globals__["calls"]) == len(annotations)
    assert run.engine.stats["l0_or_clean"] == len(annotations)
    assert all("expanded" not in text for text in annotations)
    assert len(run.observed["reviewed"]) == len(set(run.observed["reviewed"]))
    expected_episodes = 1 if mode == "fit" else 2
    assert run.metrics.counters["counts.episodes"] == expected_episodes
    assert run.metrics.counters.get("capacity.splits", 0) == int(mode != "fit")
    assert run.metrics.counters.get("capacity.sealed", 0) == 2 * int(mode != "fit")
    assert run.metrics.counters.get("capacity.minimum_failures", 0) == (2 if mode == "minimum" else 0)
    assert run.metrics.counters.get("capacity.recomputations", 0) == {"fit": 0, "split": 1, "minimum": 2}[mode]
    assert run.metrics.counters["counts.failed"] == (2 if mode == "minimum" else 0), [
        item.errors for item in sequences]
    assert run.metrics.counters["counts.emitted"] == (0 if mode == "minimum" else expected_episodes)
    assert run.metrics.counters.get("budget.overflow_records", 0) == (2 if mode == "minimum" else 0)
    assert len(verify_calls) == {"fit": 1, "split": 2, "minimum": 0}[mode]
    assert len(verify_calls) == len(set(verify_calls))
    assert len(annotations) == {"fit": 1, "split": 3, "minimum": 5}[mode]
    if mode == "fit":
        assert budget.est_prompt(verify_requests[0], profile, None, image_cost=0) == budget.input_budget(profile)
        assert budget.est_text("扩" * 2000) > 1000
        assert '"expanded": "' + "扩" * 2000 + '"' in verify_calls[0]
        assert run.observed["failures"] == []
    else:
        assert all(failure.stage == "verify" and failure.unit == "sequence"
                   and failure.error.phase == "precheck" and failure.error.profile == "default"
                   for failure in run.observed["failures"])
        assert run.observed["failures"][0].targets[0].member_positions == (0, 1)
        old_annotation = run.observed["completed"][0][2]
        assert all(item.annotation is not old_annotation for item in sequences)
        old_annotation.output["positions"].append(999)
        assert all(999 not in item.annotation.output["positions"] for item in sequences)
        assert all(item.member_positions != (0, 1) for item in sequences)
        if mode == "split":
            assert all('"expanded": "' + "扩" * 1001 + '"' in text for text in verify_calls)
            assert all(budget.est_prompt(prompt, profile, None, image_cost=0) <= budget.input_budget(profile)
                       for prompt in verify_requests)
        else:
            assert [failure.targets[0].member_positions for failure in run.observed["failures"]] == [
                (0, 1), (0,), (1,)]
            assert all(item.status == "failed" and item.errors[0].stage == "verify" for item in sequences)


def test_frame_classification_keeps_duplicate_content_occurrences_and_batches_only_tasks():
    cfg = configuration()
    cfg = replace(cfg, annotate=replace(cfg.annotate, enabled=False),
                  frame_classify=FrameClassifyConfig(enabled=True, fallback_class="other",
                      classes=(ClassSpec("main", "目标"), ClassSpec("other", "其他"))))
    member = replace(text_member(0), text="完整成员" * 300)
    item = item_with_members([member] * 5, start=9)
    engine = Engine()
    ctx = context(cfg, engine, "classify")
    asyncio.run(ClassifyStage(cfg).run([item], ctx))
    assert set(item.member_classifications) == {9, 10, 11, 12, 13}
    assert len(engine.calls) == 1
    assert [len(group.tasks) for group in ctx.tasks.groups] == [1]
    assert content(engine.calls[0][1]).count(member.text) == 5
    assert engine.calls[0][2]["properties"]["labels"]["minItems"] == 5


def test_terminal_frame_failure_is_projected_only_to_matching_occurrence_and_label():
    cfg = configuration()
    cfg = replace(cfg, annotate=replace(cfg.annotate, enabled=False),
                  frame_annotate=FrameAnnotateConfig(enabled=True, instruction="标注完整成员"))
    repeated = text_member(0)
    item = item_with_members([repeated, repeated], start=7)
    original = ContextOverflowError("single frame too long", profile="default", phase="reactive")
    target = capacity_target(item, (7,))
    failure = SessionCapacityFailure("annotate", (target,), "frame", original)
    engine = Engine()
    ctx = context(cfg, engine, "annotate", (failure,))
    asyncio.run(AnnotateStage(cfg).run([item], ctx))
    assert len(engine.calls) == 1
    assert item.member_annotations[7] is None
    assert item.member_annotations[8] is not None
    assert ctx.metrics.fed == [True]


def test_extract_verify_helper_uses_explicit_positions_and_owner_stage():
    cfg = configuration("ui")
    cfg = replace(cfg, extract=ExtractConfig(enabled=True))
    member = ui_member(0)
    item = item_with_members([member, member], start=4)
    original = ContextOverflowError("pair too long", profile="default", phase="reactive")
    ctx = context(cfg, Engine(original), "verify")
    with pytest.raises(SessionCapacityError) as caught:
        asyncio.run(extract_transition_for_item(item, 0, ctx))
    assert caught.value.failures[0].stage == "verify"
    assert caught.value.failures[0].targets[0].member_positions == (4, 5)
    assert caught.value.failures[0].error is original


@pytest.mark.parametrize("stage_type", [ClassifyStage, ExtractStage, QualityStage, AnnotateStage])
def test_provider_fatal_remains_run_control_in_process_session(stage_type):
    cfg = configuration("ui")
    cfg = replace(cfg, extract=ExtractConfig(enabled=True),
                  classify=ClassifyConfig(enabled=True, max_labels=1, fallback_class="other",
                      classes=(ClassSpec("main", "目标"), ClassSpec("other", "其他"))))
    item = item_with_members([ui_member(0), ui_member(1)])
    original = ProviderFatalError("fatal", "default", 401)
    with pytest.raises(ProviderFatalError) as caught:
        asyncio.run(stage_type(cfg).run([item], context(cfg, Engine(original), stage_type.name)))
    assert caught.value is original


@pytest.mark.parametrize("stage_type", [ClassifyStage, ExtractStage, QualityStage, AnnotateStage])
def test_successful_capacity_preview_keeps_all_objects_unchanged(stage_type):
    cfg = configuration("ui")
    cfg = replace(cfg, extract=ExtractConfig(enabled=True),
                  frame_annotate=FrameAnnotateConfig(enabled=True, instruction="完整成员"))
    item = item_with_members([ui_member(0), ui_member(1)])
    ctx = context(cfg, Engine(), stage_type.name)
    before = dict(item.__dict__)
    assert stage_type(cfg).preview_capacity(item, ctx) is None
    assert item.__dict__ == before and not ctx.schema_engine.calls
    assert not ctx.metrics.counters and not ctx.metrics.events and not ctx.tasks.groups


@pytest.mark.parametrize("criteria_per_call", ["all", "single"])
def test_pairwise_preview_and_actual_success_keep_full_member_images(criteria_per_call):
    from test_annotate import SEQ_TRANSITIONS

    cfg = configuration("ui")
    cfg = replace(cfg, quality=QualityConfig(mode="pairwise", rounds=1, criteria_per_call=criteria_per_call))
    batch = [item_with_members([ui_member(0), ui_member(1)], record_id="a"),
             item_with_members([ui_member(2), ui_member(3)], start=2, record_id="b")]
    for item in batch:
        item.transitions = SEQ_TRANSITIONS[:1]
    ctx = context(cfg, Engine(), "quality")
    stage = QualityStage(cfg)
    assert stage.preview_capacity(batch[0], ctx) is None
    asyncio.run(stage.run(batch, ctx))
    assert all(item.status == "active" and item.scores for item in batch)
    assert len(ctx.schema_engine.calls) == 1
    assert len(images(ctx.schema_engine.calls[0][1])) == 4
    assert "点击登录按钮" in content(ctx.schema_engine.calls[0][1])


def test_frame_preview_checks_unknown_classes_and_preserves_frame_failure_unit():
    cfg = configuration("ui", context_window=2000)
    cfg = replace(cfg, annotate=replace(cfg.annotate, enabled=False),
                  frame_annotate=FrameAnnotateConfig(enabled=True, instruction="global"),
                  frame_classify=FrameClassifyConfig(enabled=True),
                  frame_class_views={"skip": FrameClassView("unused", (), False),
                                     "large": FrameClassView("大指令" * 1000, (), True)})
    item = item_with_members([ui_member(0)], start=5)
    ctx = context(cfg, Engine(), "annotate")
    failure = AnnotateStage(cfg).preview_capacity(item, ctx)
    assert failure.unit == "frame"
    assert failure.targets[0].label == "large" and failure.targets[0].member_positions == (5,)
    item.member_classifications = {5: Classification("skip", ("skip",), "llm", {})}
    assert AnnotateStage(cfg).preview_capacity(item, ctx) is None


def test_unknown_sequence_class_preview_uses_each_reachable_schema_and_instruction():
    cfg = configuration(context_window=2000)
    views = {}
    for label in ("small", "large"):
        annotate = replace(cfg.annotate, instruction="完整标注" if label == "small" else "大指令" * 1000)
        views[label] = ClassView(label, cfg.quality, cfg.rubric, annotate, cfg.generate,
                                 cfg.verify, cfg.extract, model_schema=cfg.model_user_schema)
    cfg = replace(cfg, classify=replace(cfg.classify, enabled=True), class_views=views)
    item = item_with_members([text_member(0)])
    ctx = context(cfg, Engine(), "annotate")
    failure = AnnotateStage(cfg).preview_capacity(item, ctx)
    assert failure.unit == "fixed" and failure.targets[0].label == "large"
    item.classification = Classification("small", ("small",), "llm", {})
    assert AnnotateStage(cfg).preview_capacity(item, ctx) is None
    quality = QualityStage(cfg)
    assert quality.preview_capacity(item, replace(ctx, session_attempt=replace(ctx.session_attempt, stage="quality"))) is None


@pytest.mark.parametrize("stage_type", [ClassifyStage, QualityStage, AnnotateStage])
def test_actual_fixed_overhead_is_terminal_unit_without_model_dispatch(stage_type):
    cfg = configuration(context_window=1000)
    huge = "固定开销" * 1000
    cfg = replace(cfg, classify=ClassifyConfig(enabled=True, instruction=huge, max_labels=1,
                  fallback_class="other", classes=(ClassSpec("main", "目标"), ClassSpec("other", "其他"))),
                  annotate=replace(cfg.annotate, instruction=huge),
                  rubric=Rubric("fixed", (Criterion("complete", huge, huge, pointwise_levels=(huge,) * 6),)))
    item = item_with_members([text_member(0), text_member(1)])
    ctx = context(cfg, Engine(), stage_type.name)
    with pytest.raises(SessionCapacityError) as caught:
        asyncio.run(stage_type(cfg).run([item], ctx))
    assert caught.value.failures[0].unit == "fixed"
    assert not ctx.schema_engine.calls and not item.errors and item.status == "active"


def test_fanout_copies_capacity_identity_and_shares_only_current_attempt_frame_products():
    cfg = configuration()
    cfg = replace(cfg, classify=ClassifyConfig(enabled=True, assignment="multi", max_labels=2,
                  fallback_class="fallback", classes=(ClassSpec("main", "目标"), ClassSpec("other", "其他"),
                                                      ClassSpec("fallback", "兜底"))),
                  frame_annotate=FrameAnnotateConfig(enabled=True))
    item = item_with_members([text_member(0), text_member(0)], start=6)
    item.capacity = replace(item.capacity, sealed=True)
    item.stitch_task_name = "reservation"
    batch = [item]
    asyncio.run(ClassifyStage(cfg).run(batch, context(cfg, Engine(), "classify")))
    assert len(batch) == 2
    sibling = batch[1]
    assert sibling.capacity == item.capacity and sibling.member_positions == (6, 7)
    assert sibling.stitch_task_name == "reservation"
    assert sibling.record is item.record
    assert sibling.member_annotations is item.member_annotations
    assert sibling.errors is not item.errors and sibling.scores is not item.scores


class ReverseRequests(Requests):
    async def run_group(self, request):
        self.groups.append(request)
        outcomes = [None] * len(request.tasks)
        for index in reversed(range(len(request.tasks))):
            outcomes[index] = await request.tasks[index].operation()
        return tuple(outcomes)


@pytest.mark.parametrize("stage_type", [ClassifyStage, ExtractStage, QualityStage, AnnotateStage])
@pytest.mark.parametrize("samples", [0, 3])
def test_complete_wave_reports_every_overflow_before_any_business_write(stage_type, samples):
    cfg = configuration("ui", batch_size=64)
    cfg = replace(cfg, classify=ClassifyConfig(enabled=stage_type is ClassifyStage, max_labels=1,
                  fallback_class="other", self_consistency=samples,
                  classes=(ClassSpec("main", "目标"), ClassSpec("other", "其他"))),
                  extract=ExtractConfig(enabled=True), annotate=replace(cfg.annotate, self_consistency=samples))
    items = [item_with_members([ui_member(0), ui_member(1)], record_id="first"),
             item_with_members([ui_member(2), ui_member(3)], start=2, record_id="second")]
    engine = Engine(ContextOverflowError("overflow", profile="default", phase="reactive"))
    ctx = context(cfg, engine, stage_type.name)
    ctx.tasks = ReverseRequests()
    with pytest.raises(SessionCapacityError) as caught:
        asyncio.run(stage_type(cfg).run(items, ctx))
    repetitions = samples or 1 if stage_type in (ClassifyStage, AnnotateStage) else 1
    failures = caught.value.failures
    assert [failure.targets[0].record_id for failure in failures] == ["first"] * repetitions + ["second"] * repetitions
    assert all(failure.error is engine.error for failure in failures)
    assert len(engine.calls) == 2 * repetitions
    assert all(item.status == "active" and not item.errors and item.classification is None and
               item.annotation is None and item.transitions is None and not item.scores for item in items)
    assert not ctx.metrics.counters and not ctx.metrics.events


@pytest.mark.parametrize("stage_type", [ClassifyStage, AnnotateStage])
def test_all_minimum_frame_failures_replay_as_products_without_resending(stage_type):
    cfg = configuration(batch_size=64)
    cfg = replace(cfg, annotate=replace(cfg.annotate, enabled=False),
                  frame_classify=FrameClassifyConfig(enabled=stage_type is ClassifyStage,
                    fallback_class="other", classes=(ClassSpec("main", "目标"), ClassSpec("other", "其他"))),
                  frame_annotate=FrameAnnotateConfig(enabled=stage_type is AnnotateStage))
    items = [item_with_members([text_member(0), text_member(1)], record_id="first"),
             item_with_members([text_member(2), text_member(3)], start=2, record_id="second")]
    if stage_type is ClassifyStage:
        items = [item_with_members([text_member(index)], start=index, record_id=f"item-{index}") for index in range(4)]
    engine = Engine(ContextOverflowError("overflow", profile="default", phase="reactive"))
    ctx = context(cfg, engine, stage_type.name)
    ctx.tasks = ReverseRequests()
    stage = stage_type(cfg)
    with pytest.raises(SessionCapacityError) as caught:
        asyncio.run(stage.run(items, ctx))
    failures = caught.value.failures
    assert [failure.targets[0].member_positions for failure in failures] == [(0,), (1,), (2,), (3,)]
    assert len(engine.calls) == 4 and not ctx.metrics.counters
    ctx.session_attempt = replace(ctx.session_attempt, attempt=2, terminal_failures=failures)
    asyncio.run(stage.run(items, ctx))
    assert len(engine.calls) == 4
    for item in items:
        products = item.member_classifications if stage_type is ClassifyStage else item.member_annotations
        assert set(products) == set(item.member_positions)


def test_pairwise_complete_wave_preserves_every_pair_and_view_before_tie():
    cfg = replace(configuration(batch_size=64), quality=QualityConfig(mode="pairwise", rounds=2, both_orders=True))
    items = [item_with_members([text_member(index)], start=index, record_id=f"item-{index}") for index in range(4)]
    engine = Engine(ContextOverflowError("overflow", profile="default", phase="reactive"))
    ctx = context(cfg, engine, "quality")
    with pytest.raises(SessionCapacityError) as caught:
        asyncio.run(QualityStage(cfg).run(items, ctx))
    assert len(caught.value.failures) == len(engine.calls) == 8
    assert all(failure.unit == "pairwise" and len(failure.targets) == 2 for failure in caught.value.failures)
    assert not ctx.metrics.counters and all(not item.scores for item in items)


@pytest.mark.parametrize("stage_type", [ClassifyStage, AnnotateStage])
def test_synchronous_plan_and_model_failures_are_collected_in_declaration_order(monkeypatch, stage_type):
    from labelkit.operators import annotate, classify

    cfg = configuration(batch_size=64)
    cfg = replace(cfg, classify=ClassifyConfig(enabled=stage_type is ClassifyStage, max_labels=1,
                  fallback_class="other", classes=(ClassSpec("main", "目标"), ClassSpec("other", "其他"))))
    first = ContextOverflowError("planned overflow", profile="default", phase="precheck")
    second = ContextOverflowError("actual overflow", profile="default", phase="reactive")
    items = [item_with_members([text_member(0)], record_id="first"),
             item_with_members([text_member(1)], start=1, record_id="second")]
    module, name = (classify, "_plan_classification") if stage_type is ClassifyStage else (annotate, "_sample_plans")
    original = getattr(module, name)

    def plan(record, *args, **kwargs):
        if record.id == "first":
            raise first
        return original(record, *args, **kwargs)

    monkeypatch.setattr(module, name, plan)
    engine = Engine(second)
    ctx = context(cfg, engine, stage_type.name)
    with pytest.raises(SessionCapacityError) as caught:
        asyncio.run(stage_type(cfg).run(items, ctx))
    assert tuple(failure.error for failure in caught.value.failures) == (first, second)
    assert len(engine.calls) == 1 and not ctx.metrics.counters and all(not item.errors for item in items)


def test_frame_classify_keeps_whole_episode_context_and_collects_both_sequence_failures():
    cfg = configuration("ui", batch_size=64)
    cfg = replace(cfg, frame_classify=FrameClassifyConfig(enabled=True, fallback_class="other",
                  classes=(ClassSpec("main", "目标"), ClassSpec("other", "其他"))))
    items = [item_with_members([ui_member(0), ui_member(1)], record_id="first"),
             item_with_members([ui_member(2), ui_member(3)], start=2, record_id="second")]
    engine = Engine(ContextOverflowError("overflow", profile="default", phase="reactive"))
    ctx = context(cfg, engine, "classify")
    with pytest.raises(SessionCapacityError) as caught:
        asyncio.run(ClassifyStage(cfg).run(items, ctx))
    assert [failure.unit for failure in caught.value.failures] == ["sequence", "sequence"]
    assert [failure.targets[0].member_positions for failure in caught.value.failures] == [(0, 1), (2, 3)]
    assert len(engine.calls) == 2
    for call, item in zip(engine.calls, items):
        assert len(images(call[1])) == 2 and call[3].complete_evidence is True
        assert all(record_evidence(member) in content(call[1]) for member in item.record.members)


def test_extract_preview_omits_the_same_mechanical_seams_as_actual_calls():
    cfg = replace(configuration("ui", context_window=1000), extract=ExtractConfig(enabled=True, instruction="动作" * 3000))
    item = item_with_members([ui_member(0), ui_member(1)])
    ctx = context(cfg, Engine(), "extract")
    stage = ExtractStage(cfg)
    assert stage.preview_capacity(item, ctx) is not None
    item.seam_indexes = (-1, 0, 100)
    item.seam_interrupted_by = (("message",),)
    assert stage.preview_capacity(item, ctx) is None
    asyncio.run(stage.run([item], ctx))
    assert len(item.transitions) == 1 and item.transitions[0].detail["kind"] == "thread_seam"
    assert not ctx.schema_engine.calls


@pytest.mark.parametrize("stage_type", [ClassifyStage, ExtractStage, QualityStage, AnnotateStage])
def test_all_process_sequence_calls_request_complete_evidence_schema_repairs(stage_type):
    cfg = replace(configuration("ui"), classify=ClassifyConfig(enabled=stage_type is ClassifyStage,
                  max_labels=1, fallback_class="other", classes=(ClassSpec("main", "目标"), ClassSpec("other", "其他"))),
                  extract=ExtractConfig(enabled=True))
    item = item_with_members([ui_member(0), ui_member(1)])
    ctx = context(cfg, Engine(), stage_type.name)
    asyncio.run(stage_type(cfg).run([item], ctx))
    assert ctx.schema_engine.calls and all(call[3].complete_evidence for call in ctx.schema_engine.calls)

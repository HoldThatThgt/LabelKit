"""无树样本经过 classify、quality、annotate 的路径（SPEC-image-region-segmentation §12.1、§15.2）。

C7 的复现：这些路径原先无保护地调用 ``record.ui_tree.serialize()``，无树样本抛 ``AttributeError``。
离线测试用 ``complete_validated`` 接缝的进程内桩，没有 LLM。
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ClassSpec,
    ConsoleConfig,
    Criterion,
    DedupConfig,
    ExtractConfig,
    GenerateConfig,
    InputConfig,
    LLMProfile,
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
from labelkit.common.contracts.regions import CandidateRegion, ReferenceBox, RegionPolygon, RegionSet
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import ImageRef, PipelineItem, Record, RecordRef, UINode, UITree, Usage
from labelkit.common.errors import ContextOverflowError
from labelkit.operators.annotate import AnnotatePromptOptions, RepairContext, _pack_prompt, build_annotate_prompt
from labelkit.operators.classify import ClassifyStage, _assemble_classify, _PromptFit, build_classify_prompt
from labelkit.operators.quality import (
    QualityStage,
    _build_pairwise_prompt,
    _build_pointwise_prompt,
    _CallFit,
    _Comparison,
)

CLASSES = (ClassSpec(name="login", description="登录页"), ClassSpec(name="feed", description="信息流"),
           ClassSpec(name="other", description="其余"))
CRITERION = Criterion(
    key="clarity", description="清晰度：图片内容是否清楚。",
    weight=1.0, pairwise_prompt="比较两张图片，哪一张更清楚？",
    pointwise_levels=tuple(f"{score} 分" for score in range(6)))
SCHEMA_TEXT = '{"type": "object"}'
REGIONS = RegionSet(
    width=10, height=10,
    boxes=(ReferenceBox("n1", (0, 0, 5, 5), "候选文字"),),
    regions=(CandidateRegion(
        region_id="n1",
        polygons=(RegionPolygon(((0, 0), (5, 0), (5, 5), (0, 5), (0, 0)), ()),),
        box_id="n1", source_box_ids=("n1",), label_anchor=(2.5, 2.5)),))


def make_cfg(*, assignment="single", quality=None) -> ResolvedConfig:
    return ResolvedConfig(
        tool=ToolConfig(),
        console=ConsoleConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality="ui", input="in"),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(enabled=True, mode="image", instruction="生成候选"),
        stitch=StitchConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(enabled=True, llm="default", assignment=assignment, max_labels=3,
                                fallback_class="other", classes=CLASSES),
        quality=quality or QualityConfig(enabled=False),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(instruction="标注"),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=TraceConfig(),
        rubric=Rubric(name="test", criteria=(CRITERION,)),
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


def image_record(rec_id: str = "a" * 16) -> Record:
    """摄取产出的图片模式记录形态：无树、无 raw。"""
    return Record(id=rec_id, modality="ui", text=None, raw=None, ui_tree=None,
                  image=ImageRef(path=Path(f"{rec_id}.png"), format="png", size_bytes=1),
                  ref=RecordRef(f"{rec_id}.png", None, 1, ()), image_sha256="0" * 64)


def tree_record(rec_id: str = "b" * 16) -> Record:
    """普通 UI 记录：恒有树。"""
    nodes = (UINode("1", None, 0, "FrameLayout", "", "", (0, 0, 100, 100), True, {}),
             UINode("2", "1", 1, "Button", "登录", "", (1, 2, 3, 4), True, {}))
    return Record(id=rec_id, modality="ui", text=None, raw=None, ui_tree=UITree(nodes),
                  image=ImageRef(path=Path(f"{rec_id}.png"), format="png", size_bytes=1),
                  ref=RecordRef("uitree_1.jsonl", None, 1, ()))


TREE_TEXT = 'FrameLayout [0,0,100,100]\n  Button "登录" [1,2,3,4]'


def shape(parts) -> list:
    """把部件压成可比较的形态：文本取原文，图片取对象。"""
    return [part.text if part.kind == "text" else part.image for part in parts]


class Metrics:
    def __init__(self):
        self.events: list = []
        self.counters: dict[str, int] = {}

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None):
        self.events.append((ev, stage, tuple(record_ids), dict(payload or {})))

    def count(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n


class Tasks:
    async def run_group(self, request):
        return tuple([await task.operation() for task in request.tasks])


class Engine:
    """``complete_validated`` 接缝的进程内桩：按固定结果作答并留下每次请求。"""

    def __init__(self, output):
        self.output = output
        self.prompts: list = []

    async def complete_validated(self, _profile, prompt, _schema=None, *, scope):
        self.prompts.append(prompt)
        return self.output, Usage(), 1, "stub"


def make_ctx(cfg: ResolvedConfig, engine: Engine) -> RunContext:
    return RunContext(cfg=cfg, llm=None, schema_engine=engine, metrics=Metrics(), rng=random.Random(0),
                      batch_no=1, tasks=Tasks(), task_namespace="run:batch:1:stage:test")


# ── classify ────────────────────────────────────────────────────────────────


def test_classify_image_mode_prompt_omits_tree():
    record = image_record()
    bundle = build_classify_prompt(record, make_cfg(), False)
    assert bundle.messages[-1].role == "user"
    assert shape(bundle.messages[-1].parts) == ["[图片]", record.image]
    # system 文本与普通 UI 记录完全相同。
    assert bundle.messages[0] == build_classify_prompt(tree_record(), make_cfg(), False).messages[0]


def test_classify_image_mode_fit_path_counts_one_image_and_can_overflow():
    record = image_record()
    roomy = _PromptFit(input_budget=100000, image_cost=50)
    bundle = _assemble_classify(record, make_cfg(), False, fit=roomy)
    assert shape(bundle.messages[-1].parts) == ["[图片]", record.image]
    assert roomy.overflow is False and roomy.truncations == 0
    tight = _PromptFit(input_budget=10, image_cost=50)
    _assemble_classify(record, make_cfg(), False, fit=tight)
    assert tight.overflow is True


# ── quality ─────────────────────────────────────────────────────────────────


def test_quality_image_mode_prompts_omit_tree():
    first, second = image_record("a" * 16), image_record("b" * 16)
    pairwise = _build_pairwise_prompt(_Comparison(first, second), (CRITERION,), False, 30000)
    assert shape(pairwise.messages[1].parts) == ["[记录 A 图片]", first.image, "[记录 B 图片]", second.image]
    pointwise = _build_pointwise_prompt(first, CRITERION, 30000)
    assert shape(pointwise.messages[1].parts) == ["[记录内容 图片]", first.image]
    # system 文本不变。
    other = _build_pointwise_prompt(tree_record(), CRITERION, 30000)
    assert pointwise.messages[0] == other.messages[0]


def test_quality_no_tree_fit_counts_only_the_label():
    first, second = image_record("a" * 16), image_record("b" * 16)
    roomy = _CallFit(record_budget=1000, sides=2)
    _build_pairwise_prompt(_Comparison(first, second), (CRITERION,), False, 30000, fit=roomy)
    assert roomy.overflow is False and roomy.truncations == 0
    tight = _CallFit(record_budget=1, sides=1)
    bundle = _build_pointwise_prompt(first, CRITERION, 30000, fit=tight)
    assert tight.overflow is True
    assert shape(bundle.messages[1].parts) == ["[记录内容 图片]", first.image]


def test_classify_quality_receive_no_candidates():
    """信封带候选时，两个旁路算子的请求里也只有原图，没有叠图、参考框与候选文字。"""
    cfg = make_cfg(quality=QualityConfig(enabled=True, mode="pointwise"))
    item = PipelineItem(record=image_record(), regions=REGIONS)
    classify = Engine({"class": "login"})
    asyncio.run(ClassifyStage(cfg).run([item], make_ctx(cfg, classify)))
    scored = PipelineItem(record=item.record, regions=REGIONS)
    score = Engine({"scores": [{"criterion": "clarity", "reason": "清楚。", "score": 4}]})
    asyncio.run(QualityStage(cfg).run([scored], make_ctx(cfg, score)))
    assert len(classify.prompts) == 1 and len(score.prompts) == 1
    for prompt, label in ((classify.prompts[0], "[图片]"), (score.prompts[0], "[记录内容 图片]")):
        parts = prompt.messages[-1].parts
        assert shape(parts) == [label, item.record.image]
        assert parts[1].image is item.record.image
    assert item.status == "active" and item.classification.label == "login"
    assert scored.status == "active" and scored.scores["clarity"].detail["raw_score"] == 4


def test_fan_out_shares_regions_by_reference():
    cfg = make_cfg(assignment="multi")
    item = PipelineItem(record=image_record(), regions=REGIONS)
    batch = [item]
    asyncio.run(ClassifyStage(cfg).run(batch, make_ctx(cfg, Engine({"classes": ["login", "feed"]}))))
    assert [entry.classification.label for entry in batch] == ["login", "feed"]
    assert batch[1].regions is item.regions is REGIONS
    assert batch[1].record is item.record


def test_fan_out_keeps_none_regions():
    cfg = make_cfg(assignment="multi")
    batch = [PipelineItem(record=tree_record())]
    asyncio.run(ClassifyStage(cfg).run(batch, make_ctx(cfg, Engine({"classes": ["login", "feed"]}))))
    assert len(batch) == 2 and batch[1].regions is None


# ── annotate 的无树保护（§12.1 防御路径） ───────────────────────────────────


def test_annotate_tree_less_record_never_serializes_a_tree():
    record = image_record()
    bundle = build_annotate_prompt(record, make_cfg(), SCHEMA_TEXT)
    assert shape(bundle.messages[-1].parts) == ["[原图]", record.image]
    assert not any("控件树" in (part.text or "") for message in bundle.messages for part in message.parts)
    # 不属于流语义：图片模式请求照常带 image_px。
    with_px = build_annotate_prompt(record, make_cfg(), SCHEMA_TEXT, AnnotatePromptOptions(image_px=512))
    assert with_px.image_px == 512


def test_annotate_tree_less_repair_suffix_is_its_own_text_part():
    record = image_record()
    opts = AnnotatePromptOptions(repair=RepairContext(previous_output={"a": 1}, critiques_text="x: y"))
    parts = build_annotate_prompt(record, make_cfg(), SCHEMA_TEXT, opts).messages[-1].parts
    assert [part.kind for part in parts] == ["text", "image", "text"]
    assert parts[1].image is record.image
    assert parts[2].text.startswith('[上一版标注] {"a": 1}\n[审核意见] x: y\n')


# ── 非图片字节不变 ──────────────────────────────────────────────────────────


def test_non_image_ui_prompts_unchanged():
    record = tree_record()
    assert shape(build_classify_prompt(record, make_cfg(), False).messages[-1].parts) == [
        "[屏幕截图]", record.image, f"[UI 控件树]\n{TREE_TEXT}"]
    other = replace(record, id="c" * 16)
    pairwise = _build_pairwise_prompt(_Comparison(record, other), (CRITERION,), False, 30000)
    assert shape(pairwise.messages[1].parts) == [
        "[记录 A 屏幕截图]", record.image, f"[记录 A UI 控件树]\n{TREE_TEXT}",
        "[记录 B 屏幕截图]", other.image, f"[记录 B UI 控件树]\n{TREE_TEXT}"]
    pointwise = _build_pointwise_prompt(record, CRITERION, 30000)
    assert shape(pointwise.messages[1].parts) == [
        "[记录内容 屏幕截图]", record.image, f"[记录内容 UI 控件树]\n{TREE_TEXT}"]
    annotate = build_annotate_prompt(record, make_cfg(), SCHEMA_TEXT)
    assert shape(annotate.messages[-1].parts) == [
        "[屏幕截图]", record.image, f"[UI 控件树]\n{TREE_TEXT}"]
    opts = AnnotatePromptOptions(repair=RepairContext(previous_output={"a": 1}, critiques_text="x: y"))
    repaired = build_annotate_prompt(record, make_cfg(), SCHEMA_TEXT, opts).messages[-1].parts
    assert [part.kind for part in repaired] == ["text", "image", "text"]
    assert repaired[2].text.startswith(f"[UI 控件树]\n{TREE_TEXT}\n[上一版标注]")


# ── annotate 无树记录的预算路径 ─────────────────────────────────────────────


def _budget_ctx(cfg: ResolvedConfig, context_window: int) -> tuple[RunContext, LLMProfile]:
    profile = LLMProfile(name="default", provider="openai_compatible", base_url="http://x", model="m",
                         api_key_env="K", max_output_tokens=256, context_window=context_window)
    ctx = RunContext(cfg=cfg, llm=SimpleNamespace(calibrator=SimpleNamespace(cost=lambda _name: 100)),
                     schema_engine=None, metrics=Metrics(), rng=random.Random(0), batch_no=1,
                     tasks=Tasks(), task_namespace="run:batch:1:stage:annotate")
    return ctx, profile


def test_annotate_tree_less_budget_counts_one_image_and_overflows_explicitly():
    record = image_record()
    cfg = make_cfg()
    ctx, roomy = _budget_ctx(cfg, 100000)
    bundle, images = _pack_prompt(record, ctx, roomy, SCHEMA_TEXT, AnnotatePromptOptions())
    assert images == 1
    assert shape(bundle.messages[-1].parts) == ["[原图]", record.image]
    assert ctx.metrics.counters == {}
    ctx, tight = _budget_ctx(cfg, 300)
    with pytest.raises(ContextOverflowError) as caught:
        _pack_prompt(record, ctx, tight, SCHEMA_TEXT, AnnotatePromptOptions())
    assert caught.value.phase == "precheck"

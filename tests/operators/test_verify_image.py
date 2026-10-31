"""M7 对图片模式样本的处理（SPEC-image-region-segmentation §14）。

C7 的 verify 一侧：单记录评审与 trace excerpt 原先无保护地调用 ``record.ui_tree.serialize()``。
"""
from __future__ import annotations

import ast
import asyncio
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ConsoleConfig,
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
from labelkit.common.contracts.regions import (
    ReferenceBox,
    RegionOverlayRef,
    RegionSet,
    render_candidates,
    render_material_note,
    render_reference_boxes,
)
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import Annotation, ImageRef, PipelineItem, Record, RecordRef, UINode, UITree, Usage
from labelkit.common.errors import ContextOverflowError, SchemaViolation
from labelkit.common.extensions.hooks import ResolvedHook
from labelkit.common.inference import budget
from labelkit.common.inference.schema_engine import VERDICT_SCHEMA, SchemaEngine
from labelkit.operators import verify as verify_module
from labelkit.operators.annotate import AnnotatePromptOptions, AnnotateStage, build_annotate_prompt
from labelkit.operators.segment_geometry import boxes_as_regions, validate_regions
from labelkit.operators.segment_image import SegmentImageStage, build_segment_image_prompt
from labelkit.operators.stream_verify import StreamVerifyDriver
from labelkit.operators.verify import (
    VerifyPromptOptions,
    VerifyStage,
    _excerpt_source,
    _PromptFit,
    build_verify_prompt,
)
from tests.image_damage_helpers import ALL_DAMAGES, write_damaged_image


def make_cfg() -> ResolvedConfig:
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
        classify=ClassifyConfig(),
        quality=QualityConfig(enabled=False),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(enabled=True, llm="default", instruction="标注区域"),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=TraceConfig(),
        rubric=Rubric(name="test", criteria=()),
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


IMAGE_RECORD = Record(id="a" * 16, modality="ui", text=None, raw=None, ui_tree=None,
                      image=ImageRef(path=Path("image_1.png"), format="png", size_bytes=1),
                      ref=RecordRef("image_1.png", None, 1, ()))
TREE_RECORD = Record(id="b" * 16, modality="ui", text=None, raw=None,
                     ui_tree=UITree((UINode("1", None, 0, "Button", "登录", "", (1, 2, 3, 4), True, {}),)),
                     image=ImageRef(path=Path("image_2.png"), format="png", size_bytes=1),
                     ref=RecordRef("uitree_2.jsonl", None, 2, ()))
OUTPUT = {"items": [{"region_id": "n1"}]}


def test_excerpt_source_empty_without_tree():
    assert _excerpt_source(IMAGE_RECORD) == ""
    assert _excerpt_source(TREE_RECORD) == 'Button "登录" [1,2,3,4]'


def test_tree_less_review_prompt_has_image_and_no_tree_section():
    system, user = build_verify_prompt(IMAGE_RECORD, OUTPUT, make_cfg()).messages
    assert system.role == "system"
    assert [part.kind for part in user.parts] == ["text", "image", "text"]
    assert user.parts[0].text == "[任务指令] 标注区域\n[原始数据]\n[原图]"
    assert user.parts[1].image is IMAGE_RECORD.image
    assert user.parts[2].text == f"[标注结果] {json.dumps(OUTPUT, ensure_ascii=False)}"


def test_tree_less_review_fit_counts_the_image_and_reports_overflow():
    roomy = _PromptFit(input_budget=100000, image_cost=50)
    build_verify_prompt(IMAGE_RECORD, OUTPUT, make_cfg(), VerifyPromptOptions(fit=roomy))
    assert roomy.overflow is False and roomy.truncations == 0
    tight = _PromptFit(input_budget=10, image_cost=50)
    build_verify_prompt(IMAGE_RECORD, OUTPUT, make_cfg(), VerifyPromptOptions(fit=tight))
    assert tight.overflow is True


def test_ui_review_prompt_bytes_unchanged():
    _system, user = build_verify_prompt(TREE_RECORD, OUTPUT, make_cfg()).messages
    assert user.parts[0].text == "[任务指令] 标注区域\n[原始数据]\n[屏幕截图]"
    assert user.parts[1].image is TREE_RECORD.image
    assert user.parts[2].text == (
        f'[UI 控件树]\nButton "登录" [1,2,3,4]\n[标注结果] {json.dumps(OUTPUT, ensure_ascii=False)}')


# ── 图片变体（§14，AC-03、AC-12、AC-136）───────────────────────────────────────

NOTE = ("原图与候选区域叠图来自同一样本；叠图中每个区域的标识就是该候选的 region_id；候选只是辅助信息，"
        "不要求全部选中。引用候选只能用 region_id；没有成为候选的参考框不能被引用。候选与参考框坐标为 0–1000 网格，"
        "画布为 1080×2340 像素。")
N12 = ReferenceBox(box_id="n12", bbox=(43, 140, 821, 1264), text="Button 登录")
N5 = ReferenceBox(box_id="n5", bbox=(540, 1170, 540.5, 1170.5), text="不足一个网格单位")
DRAWN = {"box_id": None, "source_box_ids": ["n5", "n5"],
         "polygons": [{"exterior": [[100, 100], [300, 100], [300, 300], [100, 300], [100, 100]], "holes": []}]}
REGIONS = validate_regions(
    {"regions": [{"box_id": "n12", "polygons": None, "source_box_ids": None}, DRAWN]}, (N12, N5), 1080, 2340).proof
PASS = {"verdict": "pass", "critiques": [{"aspect": "一致性", "opinion": "合格"}]}
FAIL = {"verdict": "fail", "critiques": [{"aspect": "一致性", "opinion": "漏选"}]}
ANNOTATE_SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"],
                   "additionalProperties": False}


def review_cfg(*, segment_instruction="生成候选", judges=(), policy="drop", context_window=1_000_000,
               segment=None) -> ResolvedConfig:
    names = {"judge", "default", *judges}
    profiles = {name: LLMProfile(name=name, provider="openai_compatible", base_url="http://x", model="m",
                                 api_key_env="K", max_output_tokens=256, context_window=context_window,
                                 supports_vision=True)
                for name in names}
    return dataclasses.replace(
        make_cfg(),
        llm_profiles=profiles,
        segment=segment or SegmentConfig(enabled=True, mode="image", instruction=segment_instruction),
        verify=VerifyConfig(enabled=True, llm="judge", judges=tuple(judges), policy=policy, max_repair_rounds=1),
        user_schema=ANNOTATE_SCHEMA,
        model_user_schema=ANNOTATE_SCHEMA,
    )


class _Metrics:
    def __init__(self):
        self.counters = {}
        self.fed = []
        self.events = []

    def event(self, *args, **kwargs):
        self.events.append((args, kwargs))

    def count(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n

    def record_provider_result(self, fatal, *, hard=False):
        self.fed.append(fatal)


class _Engine:
    """complete_validated 接缝的进程内替身：按 profile 回放脚本并记录提示词。"""

    user_schema_text = json.dumps(ANNOTATE_SCHEMA, ensure_ascii=False)

    def __init__(self, script: dict[str, list]):
        self.script = {profile: list(items) for profile, items in script.items()}
        self.calls: list[tuple[str, object]] = []
        self.load_images = False

    async def complete_validated(self, profile, prompt, *, schema=None, scope=None):
        self.calls.append((profile, prompt))
        if self.load_images:
            for message in prompt.messages:
                for part in message.parts:
                    if part.kind == "image":
                        part.image.load_base64(64)
        item = self.script[profile].pop(0)
        if isinstance(item, Exception):
            raise item
        return item, Usage(1, 1), 1, "m"

    async def complete_post_validated(self, _request):
        raise AssertionError("verify must never start a segmentation call")


class _SerialTasks:
    async def run_group(self, request):
        return tuple([await spec.operation() for spec in request.tasks])


def review_ctx(cfg, engine, *, image_cost=10):
    metrics = _Metrics()
    calibrator = SimpleNamespace(cost=lambda _profile: image_cost)
    return RunContext(cfg=cfg, llm=SimpleNamespace(calibrator=calibrator), schema_engine=engine, rng=None,
                      batch_no=1, metrics=metrics, tasks=_SerialTasks(),
                      task_namespace="test:run:batch:1:stage:verify")


def run_review(cfg, engine, *, regions=REGIONS, image_cost=10, record=IMAGE_RECORD):
    item = PipelineItem(record=record, regions=regions, annotation=Annotation(output=OUTPUT, model="m", attempts=1,
                                                                              usage=Usage(1, 1)))
    ctx = review_ctx(cfg, engine, image_cost=image_cost)
    asyncio.run(VerifyStage(cfg).run([item], ctx))
    return item, ctx.metrics


def overlay_of(prompt) -> RegionOverlayRef:
    (overlay,) = [part.image for message in prompt.messages for part in message.parts
                  if isinstance(part.image, RegionOverlayRef)]
    return overlay


def evidence_of(prompt) -> dict:
    """提取当前记录消息里的候选证据：叠图、参考框正文、候选正文。"""
    texts = [part.text for part in prompt.messages[-1].parts if part.kind == "text"]
    boxes = [text for text in texts if text.startswith("[参考框]\n")]
    (candidates,) = [text for text in texts if text.startswith("[候选区域]\n")]
    lines = candidates.split("\n")
    return {"overlay": overlay_of(prompt), "boxes": boxes, "candidates": lines[1], "note": lines[2]}


def test_verify_image_prompt_mirrors_annotate_evidence():
    cfg = review_cfg()
    system, user = build_verify_prompt(IMAGE_RECORD, OUTPUT, cfg, VerifyPromptOptions(regions=REGIONS)).messages
    assert system == build_verify_prompt(IMAGE_RECORD, OUTPUT, cfg).messages[0]
    assert [part.kind for part in user.parts] == ["text", "image", "text", "image", "text", "text"]
    assert user.parts[0].text == "[任务指令] 标注区域\n[原始数据]\n[原图]"
    assert user.parts[1].image is IMAGE_RECORD.image
    assert user.parts[2].text == "[候选区域叠图]"
    assert user.parts[3].image.base is IMAGE_RECORD.image and user.parts[3].image.regions is REGIONS
    assert user.parts[4].text == f"[参考框]\n{render_reference_boxes(REGIONS)}"
    assert user.parts[5].text == (f"[候选区域]\n{render_candidates(REGIONS)}\n{NOTE}\n"
                                  f"[标注结果] {json.dumps(OUTPUT, ensure_ascii=False)}")
    annotate = build_annotate_prompt(IMAGE_RECORD, cfg, "{}", AnnotatePromptOptions(regions=REGIONS))
    review = build_verify_prompt(IMAGE_RECORD, OUTPUT, cfg, VerifyPromptOptions(regions=REGIONS))
    assert evidence_of(annotate) == evidence_of(review)
    assert evidence_of(review)["note"] == render_material_note(REGIONS) == NOTE


def test_boxes_only_review_omits_reference_block():
    regions = boxes_as_regions((N12,), 1080, 2340)
    cfg = review_cfg(segment_instruction="")
    _system, user = build_verify_prompt(IMAGE_RECORD, OUTPUT, cfg, VerifyPromptOptions(regions=regions)).messages
    assert [part.kind for part in user.parts] == ["text", "image", "text", "image", "text"]
    assert not any((part.text or "").startswith("[参考框]") for part in user.parts)
    assert json.loads(user.parts[-1].text.split("\n")[1])[0]["polygons"][0]["exterior"][2] == [761, 541]


def test_segment_annotate_verify_share_numbering_and_grid():
    cfg = review_cfg()
    canvas = RegionSet(1080, 2340, REGIONS.boxes, ())
    segment = build_segment_image_prompt(IMAGE_RECORD, canvas, "生成候选").messages[-1].parts[-1].text
    annotate = evidence_of(build_annotate_prompt(IMAGE_RECORD, cfg, "{}", AnnotatePromptOptions(regions=REGIONS)))
    review = evidence_of(build_verify_prompt(IMAGE_RECORD, OUTPUT, cfg, VerifyPromptOptions(regions=REGIONS)))
    assert annotate["boxes"] == review["boxes"] == [segment]
    assert annotate["candidates"] == review["candidates"]
    shown = [json.loads(line) for line in segment.split("\n")[1:]]
    assert [box["box_id"] for box in shown] == ["n12", "n5"]
    assert shown[1]["bbox"] == [500, 500, 501, 501]      # 不足一个网格单位仍有正面积
    reused, drawn = json.loads(review["candidates"])
    assert (reused["region_id"], reused["box_id"], reused["source_box_ids"]) == ("n12", "n12", ["n12"])
    assert (drawn["region_id"], drawn["box_id"], drawn["source_box_ids"]) == ("r1", None, ["n5"])
    left, top, right, bottom = shown[0]["bbox"]
    assert reused["polygons"][0]["exterior"] == [[left, top], [right, top], [right, bottom], [left, bottom], [left, top]]
    assert drawn["polygons"][0]["exterior"] == DRAWN["polygons"][0]["exterior"]


def test_panel_fit_bills_two_images():
    cfg = review_cfg()
    stage = VerifyStage(cfg)
    ctx = SimpleNamespace(cfg=cfg, llm=SimpleNamespace(calibrator=SimpleNamespace(cost=lambda _profile: 70)))
    assert stage._panel_fit(ctx, IMAGE_RECORD, VERDICT_SCHEMA).image_cost == 70
    fit = stage._panel_fit(ctx, IMAGE_RECORD, VERDICT_SCHEMA, REGIONS)
    assert fit.image_cost == 140
    build_verify_prompt(IMAGE_RECORD, OUTPUT, cfg, VerifyPromptOptions(regions=REGIONS, fit=fit))
    assert fit.overflow is False and fit.truncations == 0


def test_review_fit_counts_every_fixed_block_like_the_request_gate():
    cfg = review_cfg()
    prompt = build_verify_prompt(IMAGE_RECORD, OUTPUT, cfg, VerifyPromptOptions(regions=REGIONS))
    total = budget.est_prompt(prompt, None, None, image_cost=70)
    fits = _PromptFit(input_budget=total, image_cost=140)
    same = build_verify_prompt(IMAGE_RECORD, OUTPUT, cfg, VerifyPromptOptions(regions=REGIONS, fit=fits))
    assert fits.overflow is False
    assert same == prompt                                # 装填不改变任何字节
    tight = _PromptFit(input_budget=total - 1, image_cost=140)
    assert build_verify_prompt(IMAGE_RECORD, OUTPUT, cfg, VerifyPromptOptions(regions=REGIONS, fit=tight)) == prompt
    assert tight.overflow is True and tight.truncations == 0


def test_stage_precheck_overflows_on_second_image_and_never_sends():
    cfg = review_cfg()
    prompt = build_verify_prompt(IMAGE_RECORD, OUTPUT, cfg, VerifyPromptOptions(regions=REGIONS))
    # 该 profile 不声明结构化输出，Schema 不占预算。
    room = budget.input_budget(cfg.llm_profiles["judge"]) - budget.est_prompt(prompt, None, None, image_cost=0)
    engine = _Engine({"judge": [PASS]})
    item, _metrics = run_review(cfg, engine, image_cost=room // 2)
    assert item.status == "active" and len(engine.calls) == 1

    over = room // 2 + 1
    assert over <= room                                  # 只算一张图时放得下
    engine = _Engine({"judge": [PASS]})
    item, metrics = run_review(cfg, engine, image_cost=over)
    assert engine.calls == []
    assert item.status == "failed" and item.errors[-1].kind == "context_overflow"
    assert metrics.counters["budget.overflow_records"] == 1
    assert metrics.fed == []


def test_stage_fills_review_regions_from_the_envelope():
    engine = _Engine({"judge": [PASS]})
    item, _metrics = run_review(review_cfg(), engine)
    assert item.verification.verdict == "pass"
    assert overlay_of(engine.calls[0][1]).regions is REGIONS


def test_verify_repair_passes_same_region_set_object():
    engine = _Engine({"judge": [FAIL, PASS], "default": [{"items": []}]})
    item, _metrics = run_review(review_cfg(policy="repair"), engine)
    assert item.status == "active" and item.verification.rounds == 2
    assert [profile for profile, _prompt in engine.calls] == ["judge", "default", "judge"]
    for _profile, prompt in engine.calls:
        assert overlay_of(prompt).regions is REGIONS
    repair_text = engine.calls[1][1].messages[-1].parts[-1].text
    assert repair_text.startswith(f"[候选区域]\n{render_candidates(REGIONS)}\n{NOTE}\n[上一版标注] ")
    assert "[审核意见] 一致性: 漏选\n" in repair_text
    assert item.regions is REGIONS


def test_verify_never_calls_segment(monkeypatch):
    def forbid(*_args, **_kwargs):
        raise AssertionError("verify must never re-segment")

    monkeypatch.setattr(SegmentImageStage, "run", forbid)
    engine = _Engine({"judge": [FAIL, PASS], "default": [{"items": []}]})
    item, _metrics = run_review(review_cfg(policy="repair"), engine)
    assert item.status == "active" and item.errors == []
    assert item.regions is REGIONS
    assert all(profile in {"judge", "default"} for profile, _prompt in engine.calls)
    tree = ast.parse(Path(verify_module.__file__).read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not {"labelkit.operators.segment", "labelkit.operators.segment_image",
                "labelkit.operators.segment_geometry"} & imported


class _QueueLLM:
    """按调用序回放文本并像 M9 那样逐张取图片字节；记录 (profile, prompt)。"""

    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = []

    async def complete(self, profile, prompt, response_schema=None):
        self.calls.append((profile, prompt))
        for message in prompt.messages:
            for part in message.parts:
                if part.kind == "image":
                    part.image.load_base64(64)
        return SimpleNamespace(text=self.texts.pop(0), structured=None, usage=Usage(5, 2), model="m")


def test_real_stage_chain_shares_one_region_set_and_never_resegments(tmp_path):
    path = tmp_path / "image_7.png"
    Image.new("RGB", (108, 234), (200, 200, 200)).save(path)
    record = Record(id="f" * 16, modality="ui", text=None, raw=None, ui_tree=None,
                    image=ImageRef(path, "png", path.stat().st_size), ref=RecordRef("image_7.png", None, 7, ()),
                    companion=b"{}", image_sha256="0" * 64)

    def parse(_companion, _image):
        return [{"id": "n12", "bbox": [4, 14, 82, 126], "text": "Button 登录"}]

    segment = SegmentConfig(enabled=True, mode="image", llm="seg", instruction="生成候选", box_parser="hooks.py:parse",
                            companion_suffix=".json", resolved_box_parser=ResolvedHook("hooks.py:parse", parse))
    base = review_cfg(segment=segment, policy="repair")
    profiles = dict(base.llm_profiles, seg=dataclasses.replace(base.llm_profiles["judge"], name="seg"))
    cfg = dataclasses.replace(base, llm_profiles=profiles)
    drawn = {"box_id": None, "source_box_ids": ["n12"],
             "polygons": [{"exterior": [[0, 0], [500, 0], [500, 500], [0, 0]], "holes": []}]}
    reused = {"box_id": "n12", "polygons": None, "source_box_ids": None}
    llm = _QueueLLM(json.dumps({"regions": [reused, drawn]}), '{"items": []}', json.dumps(FAIL),
                    '{"items": [1]}', json.dumps(PASS))
    metrics = _Metrics()
    calibrator = SimpleNamespace(cost=lambda _profile: 10)
    ctx = RunContext(cfg=cfg, llm=SimpleNamespace(calibrator=calibrator), rng=None, batch_no=1, metrics=metrics,
                     schema_engine=SchemaEngine(ANNOTATE_SCHEMA, llm, cfg.output, metrics), tasks=_SerialTasks(),
                     task_namespace="test:run:batch:1:stage:chain")
    item = PipelineItem(record=record)
    batch = [item]
    asyncio.run(SegmentImageStage(cfg).run(batch, ctx))
    regions = item.regions
    assert [region.region_id for region in regions.regions] == ["n12", "r1"]
    asyncio.run(AnnotateStage(cfg).run(batch, ctx))
    asyncio.run(VerifyStage(cfg).run(batch, ctx))

    assert item.status == "active" and item.verification.rounds == 2
    assert item.annotation.output == {"items": [1]}
    assert item.regions is regions
    assert [profile for profile, _prompt in llm.calls] == ["seg", "default", "judge", "default", "judge"]
    for _profile, prompt in llm.calls[1:]:
        assert overlay_of(prompt).regions is regions
        assert evidence_of(prompt)["candidates"] == render_candidates(regions)


def _sequence_record() -> Record:
    member = Record(id="c" * 16, modality="text", text="帧", raw={"text": "帧"}, ui_tree=None, image=None,
                    ref=RecordRef("data.jsonl", 1, None, ()))
    return Record(id="d" * 16, modality="text", text=None, raw=None, ui_tree=None, image=None,
                  ref=RecordRef("data.jsonl", 1, None, ()), kind="sequence", members=(member,))


def test_image_samples_never_reach_stream_driver(monkeypatch):
    reached = []

    async def driver_run(_self, _batch, episodes, _ctx):
        reached.extend(episodes)

    monkeypatch.setattr(StreamVerifyDriver, "run", driver_run)
    engine = _Engine({"judge": [PASS, PASS]})
    cfg = review_cfg()
    assert cfg.segment.enabled and not cfg.segment.sequence_resolved
    annotation = Annotation(output=OUTPUT, model="m", attempts=1, usage=Usage(1, 1))
    sample = PipelineItem(record=IMAGE_RECORD, regions=REGIONS, annotation=annotation)
    # 按 enabled 路由的回归只有带序列信封才显形：图片工程里也不许进流驱动器。
    episode = PipelineItem(record=_sequence_record(), annotation=annotation)
    asyncio.run(VerifyStage(cfg).run([sample, episode], review_ctx(cfg, engine)))
    assert reached == []
    assert len(engine.calls) == 2 and sample.verification.verdict == "pass"

    # 正控制：流语义配置下同一序列信封确实由替身驱动器接手。
    stream = review_cfg(segment=SegmentConfig(enabled=True, mode="sequence", sequence_resolved=True))
    episode = PipelineItem(record=_sequence_record(), annotation=annotation)
    asyncio.run(VerifyStage(stream).run([episode], review_ctx(stream, _Engine({}))))
    assert reached == [episode]


@pytest.mark.parametrize("damage", ["missing", *ALL_DAMAGES])
def test_unreadable_image_is_decode_error(tmp_path, damage):
    image = ImageRef(tmp_path / "gone.png", "png", 1)
    if damage in ALL_DAMAGES:
        image = write_damaged_image(tmp_path, damage)
    record = dataclasses.replace(IMAGE_RECORD, image=image)
    engine = _Engine({"judge": [PASS]})
    engine.load_images = True
    item, _metrics = run_review(review_cfg(), engine, record=record)
    assert item.status == "failed"
    assert (item.errors[-1].stage, item.errors[-1].kind) == ("verify", "image_decode_error")


@pytest.mark.parametrize("failure", [
    ContextOverflowError("server says too large", "reactive", "j2", "http_400"),
    SchemaViolation(["repair round overflowed"], ""),      # 评委自身修复轮溢出在 M8 收敛成它
], ids=["reactive", "own-repair-overflow"])
def test_judge_overflow_counts_as_judge_error(failure):
    cfg = review_cfg(judges=("j1", "j2", "j3"))
    engine = _Engine({"j1": [PASS], "j2": [failure], "j3": [PASS]})
    item, metrics = run_review(cfg, engine)
    assert item.status == "active" and item.errors == []
    assert item.verification.verdict == "pass"
    entries = [entry for entry in item.verification.critiques if entry["aspect"] == "judge_error"]
    assert [entry["judge"] for entry in entries] == ["j2"]
    assert metrics.fed == []                             # 降成反对票，不喂熔断
    assert "budget.overflow_records" not in metrics.counters


def test_single_judge_reactive_overflow_is_context_overflow():
    overflow = ContextOverflowError("server says too large", "reactive", "judge", "http_400")
    item, metrics = run_review(review_cfg(), _Engine({"judge": [overflow]}))
    assert item.status == "failed" and item.errors[-1].kind == "context_overflow"
    assert metrics.fed == [True]
    assert metrics.counters["budget.overflow_records"] == 1


def test_text_and_ui_prompts_unchanged():
    cfg = make_cfg()
    text = Record(id="e" * 16, modality="text", text="原文", raw={"text": "原文"}, ui_tree=None, image=None,
                  ref=RecordRef("data.jsonl", 1, None, ()))
    system, user = build_verify_prompt(text, OUTPUT, cfg).messages
    assert system.parts[0].text == (
        "你是标注质量审核员。给定任务指令、原始数据与标注结果，独立判断标注是否合格。\n"
        "评审维度: ① 是否遵循任务指令 ② 与原始数据的事实一致性 ③ 字段语义是否正确填写\n"
        "先逐维度给出简短意见，再给结论。")
    assert [part.kind for part in user.parts] == ["text"]
    assert user.parts[0].text == f"[任务指令] 标注区域\n[原始数据] 原文\n[标注结果] {json.dumps(OUTPUT, ensure_ascii=False)}"

    ui_prompt = build_verify_prompt(TREE_RECORD, OUTPUT, cfg)
    assert ui_prompt == build_verify_prompt(TREE_RECORD, OUTPUT, cfg, VerifyPromptOptions(regions=None))
    _system, ui_user = ui_prompt.messages
    assert [part.kind for part in ui_user.parts] == ["text", "image", "text"]
    assert ui_user.parts[0].text == "[任务指令] 标注区域\n[原始数据]\n[屏幕截图]"
    assert ui_user.parts[2].text == (
        f'[UI 控件树]\nButton "登录" [1,2,3,4]\n[标注结果] {json.dumps(OUTPUT, ensure_ascii=False)}')
    for prompt in (ui_prompt, build_verify_prompt(text, OUTPUT, cfg)):
        assert not any(isinstance(part.image, RegionOverlayRef) for part in prompt.messages[-1].parts)

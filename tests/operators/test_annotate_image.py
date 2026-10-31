"""M5 对图片模式样本的标注材料与容量（SPEC-image-region-segmentation §12，AC-03、AC-09、AC-12、AC-132、AC-133）。

只在 complete_* 接缝与 LLM 桩上替身，不 mock 服务端或传输层。
"""
from __future__ import annotations

import ast
import asyncio
import base64
import dataclasses
import io
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
    FewShotExample,
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
from labelkit.common.contracts.types import ImageRef, PipelineItem, Record, RecordRef, Usage
from labelkit.common.errors import ContextOverflowError
from labelkit.common.inference import budget
from labelkit.common.inference.schema_engine import SchemaEngine
from labelkit.operators import annotate as annotate_module
from labelkit.operators.annotate import (
    AnnotatePromptOptions,
    AnnotateStage,
    RepairContext,
    build_annotate_prompt,
)
from labelkit.operators.segment_geometry import boxes_as_regions, validate_regions
from tests.image_damage_helpers import ALL_DAMAGES, write_damaged_image

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["a", "b"]},
        "items": {"type": "array", "items": {
            "type": "object", "properties": {"region_id": {"type": "string"}},
            "required": ["region_id"], "additionalProperties": False}},
    },
    "required": ["kind", "items"],
    "additionalProperties": False,
}
SCHEMA_TEXT = json.dumps(SCHEMA, ensure_ascii=False, separators=(", ", ": "))
GOOD = '{"kind": "a", "items": [{"region_id": "n12"}]}'
BAD = '{"kind": "c", "items": []}'
INSTRUCTION = "标注按钮区域"
SEGMENT_INSTRUCTION = "生成候选"

NOTE_1080 = ("原图与候选区域叠图来自同一样本；叠图中每个区域的标识就是该候选的 region_id；候选只是辅助信息，"
             "不要求全部选中。引用候选只能用 region_id；没有成为候选的参考框不能被引用。候选与参考框坐标为 0–1000 网格，"
             "画布为 1080×2340 像素。")
N12 = ReferenceBox(box_id="n12", bbox=(43, 140, 821, 1264), text="Button 登录")
N5 = ReferenceBox(box_id="n5", bbox=(0.5, 1.25, 999.75, 2339.5), text="未复用的\n框")
IMAGE = ImageRef(path=Path("image_7.png"), format="png", size_bytes=1)
RECORD = Record(id="8f3c2a91d0b47e65", modality="ui", text=None, raw=None, ui_tree=None, image=IMAGE,
                ref=RecordRef("image_7.png", None, 7, ()))


def reuse(*boxes: ReferenceBox, width: int = 1080, height: int = 2340) -> RegionSet:
    """经真实几何校验构造「模型复用第一个框」的分割结果。"""
    model = {"regions": [{"box_id": boxes[0].box_id, "polygons": None, "source_box_ids": None}]}
    result = validate_regions(model, boxes, width, height)
    assert result.violations == ()
    return result.proof


REUSED = reuse(N12)
MAKE_CFG_DEFAULTS = {"instruction": INSTRUCTION, "segment_instruction": SEGMENT_INSTRUCTION, "self_consistency": 0,
                     "context_window": 1_000_000, "examples": (), "max_repair_attempts": 2}


def make_cfg(**overrides) -> ResolvedConfig:
    """构造图片模式配置。

    @param overrides 覆盖 ``MAKE_CFG_DEFAULTS`` 的键
    @return 已解析配置
    """
    unknown = overrides.keys() - MAKE_CFG_DEFAULTS.keys()
    assert not unknown, f"unknown make_cfg option: {sorted(unknown)}"
    values = {**MAKE_CFG_DEFAULTS, **overrides}
    instruction, segment_instruction, self_consistency, context_window, examples, max_repair_attempts = (
        values[key] for key in MAKE_CFG_DEFAULTS)
    profile = LLMProfile(name="vision", provider="openai_compatible", base_url="http://x", model="m",
                         api_key_env="K", max_output_tokens=256, context_window=context_window,
                         supports_vision=True, supports_structured_output=False)
    return ResolvedConfig(
        tool=ToolConfig(),
        console=ConsoleConfig(),
        llm_profiles={"vision": profile},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality="ui", input="in"),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(enabled=True, mode="image", instruction=segment_instruction),
        stitch=StitchConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(),
        quality=QualityConfig(enabled=False),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(enabled=True, llm="vision", instruction=instruction,
                                self_consistency=self_consistency, examples=tuple(examples)),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline=SCHEMA_TEXT, max_repair_attempts=max_repair_attempts),
        trace=TraceConfig(),
        rubric=Rubric(name="test", criteria=()),
        class_views={},
        user_schema=SCHEMA,
        model_user_schema=SCHEMA,
        limit=None,
        strict=False,
        dry_run=False,
        config_path="config.toml",
        project_path="project.toml",
        config_digest="sha256:0",
        project_digest="sha256:0",
    )


class _SerialTasks:
    """按声明序逐个执行任务，使队列式 LLM 桩的响应归属确定。"""

    async def run_group(self, request):
        return tuple([await spec.operation() for spec in request.tasks])


class _Metrics:
    """捕获计数、事件与熔断喂入。"""

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


class _QueueLLM:
    """按队列返回文本，队列项为异常时抛出；记录每轮提示词。"""

    def __init__(self, *items, load_images: bool = False):
        self.items = list(items)
        self.prompts = []
        self.load_images = load_images
        self.loaded = []

    async def complete(self, profile, prompt, response_schema=None):
        self.prompts.append(prompt)
        if self.load_images:                     # 与 M9 编码请求时相同：逐张惰性取字节
            self.loaded = [part.image.load_base64(prompt.image_px or 64)
                           for message in prompt.messages for part in message.parts if part.kind == "image"]
        item = self.items[min(len(self.prompts) - 1, len(self.items) - 1)]
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=item, structured=None, usage=Usage(5, 2), model="m")


class _Calibrator:
    def __init__(self, value: int):
        self.value = value

    def cost(self, _profile: str) -> int:
        return self.value


def make_ctx(cfg: ResolvedConfig, llm: _QueueLLM, *, image_cost: int = 10) -> tuple[RunContext, _Metrics]:
    metrics = _Metrics()
    engine = SchemaEngine(SCHEMA, llm, cfg.output, metrics)
    ctx = RunContext(cfg=cfg, llm=SimpleNamespace(calibrator=_Calibrator(image_cost)), schema_engine=engine,
                     rng=None, batch_no=1, metrics=metrics, tasks=_SerialTasks(),
                     task_namespace="test:run:batch:1:stage:annotate")
    return ctx, metrics


def run_stage(cfg: ResolvedConfig, llm: _QueueLLM, regions: RegionSet = REUSED, *, image_cost: int = 10,
              record: Record = RECORD) -> tuple[PipelineItem, _Metrics]:
    item = PipelineItem(record=record, regions=regions)
    ctx, metrics = make_ctx(cfg, llm, image_cost=image_cost)
    asyncio.run(AnnotateStage(cfg).run([item], ctx))
    return item, metrics


def prompt_for(cfg: ResolvedConfig, regions: RegionSet = REUSED, **options):
    return build_annotate_prompt(RECORD, cfg, SCHEMA_TEXT, AnnotatePromptOptions(regions=regions, **options))


def current_parts(prompt):
    return prompt.messages[-1].parts


def overlays(prompt) -> list[RegionOverlayRef]:
    return [part.image for message in prompt.messages for part in message.parts
            if isinstance(part.image, RegionOverlayRef)]


def block(prompt, label: str) -> str | None:
    """取当前记录消息中以 ``label`` 开头的文本段正文（去掉首行标签）。"""
    for part in current_parts(prompt):
        if part.kind == "text" and part.text.startswith(f"{label}\n"):
            return part.text[len(label) + 1:]
    return None


def candidates_in(prompt) -> list[dict]:
    return json.loads(block(prompt, "[候选区域]").split("\n")[0])


# ── 提示词（§12.1）────────────────────────────────────────────────────────────

def test_annotate_image_prompt_parts_in_frozen_order():
    prompt = prompt_for(make_cfg())
    parts = current_parts(prompt)
    assert [part.kind for part in parts] == ["text", "image", "text", "image", "text", "text"]
    assert parts[0].text == "[原图]"
    assert parts[1].image is RECORD.image
    assert parts[2].text == "[候选区域叠图]"
    assert parts[3].image == RegionOverlayRef(RECORD.image, REUSED)
    assert parts[3].image.base is RECORD.image and parts[3].image.regions is REUSED
    assert parts[4].text == '[参考框]\n{"box_id": "n12", "bbox": [39, 59, 761, 541], "text": "Button 登录"}'
    # 提案 §4.6 的同一样本：json.dumps 默认分隔符下的 CONTRACTS §10.1 字节。
    assert parts[5].text == (
        '[候选区域]\n[{"region_id": "n12", "polygons": [{"exterior": [[39, 59], [761, 59], [761, 541], [39, 541], '
        '[39, 59]], "holes": []}], "box_id": "n12", "source_box_ids": ["n12"], "text": "Button 登录"}]\n' + NOTE_1080)


def test_candidate_json_reproduces_the_proposal_example():
    # 提案 §4.6 紧凑写法的内容与实际渲染逐值相同；与 [参考框] 的 [39,59,761,541] 同值。
    example = ('[{"region_id": "n12", "polygons": [{"exterior": [[39,59],[761,59],[761,541],[39,541],[39,59]], '
               '"holes": []}], "box_id": "n12", "source_box_ids": ["n12"], "text": "Button 登录"}]')
    assert candidates_in(prompt_for(make_cfg())) == json.loads(example)


def test_boxes_only_prompt_omits_reference_block():
    regions = boxes_as_regions((N12,), 1080, 2340)
    prompt = prompt_for(make_cfg(segment_instruction=""), regions)
    parts = current_parts(prompt)
    assert [part.kind for part in parts] == ["text", "image", "text", "image", "text"]
    assert block(prompt, "[参考框]") is None
    assert parts[-1].text.startswith("[候选区域]\n")
    assert parts[-1].text.endswith(NOTE_1080)
    assert candidates_in(prompt) == candidates_in(prompt_for(make_cfg()))


def test_model_path_without_boxes_has_no_reference_block():
    drawn = {"regions": [{"box_id": None, "source_box_ids": None,
                          "polygons": [{"exterior": [[0, 0], [100, 0], [100, 100], [0, 0]], "holes": []}]}]}
    regions = validate_regions(drawn, (), 1000, 2000).proof
    prompt = prompt_for(make_cfg(), regions)
    assert block(prompt, "[参考框]") is None
    assert candidates_in(prompt)[0]["region_id"] == "r1"
    assert candidates_in(prompt)[0]["box_id"] is None
    assert "text" not in candidates_in(prompt)[0]


def test_repair_suffix_appended_to_final_text_part():
    repair = RepairContext(previous_output={"kind": "a", "items": []}, critiques_text="字段: 漏选")
    plain = current_parts(prompt_for(make_cfg()))
    repaired = current_parts(prompt_for(make_cfg(), repair=repair))
    assert repaired[:-1] == plain[:-1]
    assert repaired[-1].text == (
        f"{plain[-1].text}\n[上一版标注] " + '{"kind": "a", "items": []}' + "\n[审核意见] 字段: 漏选\n"
        "请依据原始数据和上一版标注核验全部历史批评，保留已正确内容，只修正仍成立的问题。只输出修正后的 JSON。")
    assert sum(part.kind == "image" for part in repaired) == 2


def test_system_message_unchanged():
    examples = (FewShotExample(input="示例", output={"kind": "a", "items": []}),)
    cfg = make_cfg(examples=examples)
    prompt = prompt_for(cfg)
    assert prompt.messages[0].role == "system"
    assert prompt.messages[0].parts[0].text == (
        f"{INSTRUCTION}\n输出必须是符合以下 JSON Schema 的单个 JSON 对象，不输出任何其他内容：\n{SCHEMA_TEXT}")
    assert prompt.messages[:-1] == build_annotate_prompt(RECORD, cfg, SCHEMA_TEXT).messages[:-1]
    assert all(part.kind == "text" for message in prompt.messages[:-1] for part in message.parts)
    assert budget.TEMPLATE_HEAD_TOKENS["annotate"] == budget.est_text(annotate_module._SCHEMA_SENTENCE)


def test_empty_candidates_render_empty_array():
    regions = RegionSet(1080, 2340, (), ())
    prompt = prompt_for(make_cfg(), regions)
    assert current_parts(prompt)[-1].text == f"[候选区域]\n[]\n{NOTE_1080}"
    assert overlays(prompt) == [RegionOverlayRef(RECORD.image, regions)]


def test_switching_instruction_keeps_candidate_set():
    regions = reuse(N12, N5)
    first = prompt_for(make_cfg(instruction="只选按钮"), regions)
    second = prompt_for(make_cfg(instruction="只选输入框"), regions)
    assert first.messages[0] != second.messages[0]
    assert current_parts(first) == current_parts(second)
    assert overlays(first)[0].regions is overlays(second)[0].regions is regions


def test_material_note_is_the_last_line_of_the_final_part():
    prompt = prompt_for(make_cfg())
    assert current_parts(prompt)[-1].text.split("\n")[-1] == render_material_note(REUSED) == NOTE_1080


# ── 同一框的网格坐标（§5.4，AC-03）─────────────────────────────────────────────

def rectangle(grid: list[int]) -> list[list[int]]:
    left, top, right, bottom = grid
    return [[left, top], [right, top], [right, bottom], [left, bottom], [left, top]]


@pytest.mark.parametrize(("box", "expected"), [
    (N12, [39, 59, 761, 541]),
    (ReferenceBox("f", (0.5, 1.25, 999.75, 2339.5), "浮点框"), [0, 0, 926, 1000]),
    (ReferenceBox("sub", (540, 1170, 540.5, 1170.5), "不足一个网格单位"), [500, 500, 501, 501]),
], ids=["integer", "float", "sub-grid"])
def test_reused_box_shows_identical_grid_in_both_blocks(box, expected):
    prompt = prompt_for(make_cfg(), reuse(box, N5))
    lines = [json.loads(line) for line in block(prompt, "[参考框]").split("\n")]
    assert [line["box_id"] for line in lines] == [box.box_id, "n5"]
    assert lines[0]["bbox"] == expected
    assert expected[0] < expected[2] and expected[1] < expected[3]
    (candidate,) = candidates_in(prompt)
    assert candidate["polygons"] == [{"exterior": rectangle(expected), "holes": []}]
    assert candidate["text"] == box.text


@pytest.mark.parametrize(("box", "expected"), [
    (N12, [39, 59, 761, 541]),
    (ReferenceBox("sub", (540, 1170, 540.5, 1170.5), ""), [500, 500, 501, 501]),
], ids=["integer", "sub-grid"])
def test_boxes_only_candidate_shows_the_reference_render_grid(box, expected):
    regions = boxes_as_regions((box,), 1080, 2340)
    prompt = prompt_for(make_cfg(segment_instruction=""), regions)
    assert json.loads(render_reference_boxes(regions))["bbox"] == expected
    assert expected[0] < expected[2] and expected[1] < expected[3]
    assert candidates_in(prompt)[0]["polygons"] == [{"exterior": rectangle(expected), "holes": []}]


def test_prompt_uses_the_shared_renderers_byte_for_byte():
    regions = reuse(N12, N5)
    prompt = prompt_for(make_cfg(), regions)
    assert block(prompt, "[参考框]") == render_reference_boxes(regions)
    assert block(prompt, "[候选区域]") == f"{render_candidates(regions)}\n{render_material_note(regions)}"
    # 文本里的换行被转义，不能伪造出另一行。
    assert len(block(prompt, "[参考框]").split("\n")) == 2


# ── 对象同一性（AC-12）──────────────────────────────────────────────────────────

def test_self_consistency_and_l3_share_region_set_object():
    llm = _QueueLLM(BAD, GOOD, GOOD)
    item, _metrics = run_stage(make_cfg(self_consistency=2), llm)
    assert item.status == "active" and item.annotation.attempts == 3
    assert item.annotation.sc == {"n": 2, "agreement_ratio": 1.0}
    assert len(llm.prompts) == 3
    first, l3, second = llm.prompts
    assert l3 != first                           # 第二轮确是修复轮：首轮提示词加修复指令
    for prompt in llm.prompts:
        (overlay,) = overlays(prompt)
        assert overlay.regions is REUSED and overlay.base is RECORD.image
    assert first == second
    assert item.regions is REUSED


def test_stage_without_regions_sends_no_overlay():
    llm = _QueueLLM(GOOD)
    tree_less = dataclasses.replace(RECORD, id="b" * 16)
    item, _metrics = run_stage(make_cfg(), llm, None, record=tree_less)
    assert item.status == "active"
    assert overlays(llm.prompts[0]) == []
    assert [part.kind for part in current_parts(llm.prompts[0])] == ["text", "image"]


# ── 容量（§12.2，AC-133）────────────────────────────────────────────────────────

def text_tokens(cfg: ResolvedConfig, regions: RegionSet = REUSED) -> int:
    return budget.est_prompt(prompt_for(cfg, regions), None, None, image_cost=0)


def test_two_images_billed_on_precheck():
    cfg = make_cfg()
    room = budget.input_budget(cfg.llm_profiles["vision"]) - text_tokens(cfg)
    fits = room // 2
    llm = _QueueLLM(GOOD)
    item, _metrics = run_stage(cfg, llm, image_cost=fits)
    assert item.status == "active" and len(llm.prompts) == 1

    # 只算一张图时这个成本放得下，两张放不下：请求从不发出。
    over = fits + 1
    assert over <= room
    llm = _QueueLLM(GOOD)
    item, _metrics = run_stage(cfg, llm, image_cost=over)
    assert llm.prompts == []
    assert item.status == "failed" and item.errors[-1].kind == "context_overflow"


def test_candidate_blocks_never_trimmed_precheck_overflow():
    boxes = tuple(ReferenceBox(f"n{i}", (i, i, i + 10, i + 10), "很长的控件描述" * 40) for i in range(1, 121))
    regions = reuse(*boxes)
    base = make_cfg()
    needed = text_tokens(base, regions) + 2
    window = needed
    while budget.input_budget(dataclasses.replace(base.llm_profiles["vision"], context_window=window)) < needed:
        window += 1
    cfg = make_cfg(context_window=window)
    room = budget.input_budget(cfg.llm_profiles["vision"]) - text_tokens(cfg, regions)
    assert room >= 2

    llm = _QueueLLM(GOOD)
    item, metrics = run_stage(cfg, llm, regions, image_cost=room // 2)
    assert item.status == "active"
    assert llm.prompts[0] == prompt_for(cfg, regions)
    assert len(block(llm.prompts[0], "[参考框]").split("\n")) == 120
    assert "budget.truncations.annotate" not in metrics.counters

    llm = _QueueLLM(GOOD)
    item, metrics = run_stage(cfg, llm, regions, image_cost=room // 2 + 1)
    assert llm.prompts == []
    assert item.status == "failed" and item.errors[-1].kind == "context_overflow"
    assert "k=2" in item.errors[-1].message
    assert "budget.truncations.annotate" not in metrics.counters
    assert metrics.fed == []                     # 预检超限永不喂熔断


def test_first_round_reactive_overflow_is_context_overflow():
    overflow = ContextOverflowError("too large", "reactive", "vision", "http_400")
    llm = _QueueLLM(overflow)
    item, metrics = run_stage(make_cfg(), llm)
    assert item.status == "failed" and item.errors[-1].kind == "context_overflow"
    assert metrics.fed == [True]


def test_repair_overflow_is_schema_violation():
    overflow = ContextOverflowError("repair too large", "reactive", "vision", "http_400")
    llm = _QueueLLM(BAD, overflow, GOOD)
    item, metrics = run_stage(make_cfg(), llm)
    assert len(llm.prompts) == 2                 # 溢出短路其余修复预算
    assert overlays(llm.prompts[1])[0].regions is REUSED
    assert item.status == "failed"
    assert (item.errors[-1].stage, item.errors[-1].kind) == ("annotate", "schema_violation")
    assert metrics.fed == [True]
    assert "budget.overflow_records" not in metrics.counters


@pytest.mark.parametrize("repair_round", [
    BAD,
    ContextOverflowError("repair too large", "reactive", "vision", "http_400"),
], ids=["repair-exhausted", "repair-overflow"])
def test_self_consistency_sample_abstains_on_repair_exhaustion(repair_round):
    llm = _QueueLLM(BAD, repair_round, GOOD, GOOD)
    item, _metrics = run_stage(make_cfg(self_consistency=3, max_repair_attempts=1), llm)
    assert item.status == "active" and item.errors == []
    assert item.annotation.sc == {"n": 3, "agreement_ratio": 2 / 3}
    assert item.annotation.output["items"] == [{"region_id": "n12"}]


# ── 不可读图片（AC-27）──────────────────────────────────────────────────────────

def write_png(path: Path, size: tuple[int, int] = (120, 240)) -> ImageRef:
    Image.new("RGB", size, (20, 40, 60)).save(path, format="PNG")
    return ImageRef(path=path, format="png", size_bytes=path.stat().st_size)


@pytest.mark.parametrize("damage", ["missing", "garbage", *ALL_DAMAGES])
def test_unreadable_image_is_decode_error(tmp_path, damage):
    path = tmp_path / "image_7.png"
    if damage == "garbage":
        path.write_bytes(b"not an image")
    image = ImageRef(path=path, format="png", size_bytes=1)
    if damage in ALL_DAMAGES:
        image = write_damaged_image(tmp_path, damage)
    record = dataclasses.replace(RECORD, image=image)
    llm = _QueueLLM(GOOD, load_images=True)
    item, _metrics = run_stage(make_cfg(), llm, record=record)
    assert item.status == "failed"
    assert (item.errors[-1].stage, item.errors[-1].kind) == ("annotate", "image_decode_error")


def test_request_renders_both_images_at_the_same_size(tmp_path):
    record = dataclasses.replace(RECORD, image=write_png(tmp_path / "image_7.png"))
    regions = reuse(ReferenceBox("n1", (10, 20, 60, 120), ""), width=120, height=240)
    llm = _QueueLLM(GOOD, load_images=True)
    item, _metrics = run_stage(make_cfg(), llm, regions, record=record)
    assert item.status == "active"
    assert [media for media, _body in llm.loaded] == ["image/png", "image/png"]
    original, overlay = (Image.open(io.BytesIO(base64.b64decode(body))) for _media, body in llm.loaded)
    assert original.size == overlay.size == (32, 64)
    assert original.convert("RGB").tobytes() != overlay.convert("RGB").tobytes()


def test_annotate_imports_no_segment_module():
    tree = ast.parse(Path(annotate_module.__file__).read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not {"labelkit.operators.segment_image", "labelkit.operators.segment_geometry"} & imported


# ── 区域引用（AC-135）────────────────────────────────────────────────────────────

def test_self_consistency_sample_abstains_on_unknown_reference():
    """单个样本的未知引用修复穷尽时只让该样本弃权；违规不回显模型给出的值。"""
    unknown = '{"kind": "a", "items": [{"region_id": "ghost-41"}]}'
    llm = _QueueLLM(unknown, unknown, GOOD, GOOD)
    cfg = dataclasses.replace(make_cfg(self_consistency=3, max_repair_attempts=1),
                              region_ref_paths=("/items/*/region_id",))
    item, _metrics = run_stage(cfg, llm)
    assert item.status == "active" and item.errors == []
    assert item.annotation.sc == {"n": 3, "agreement_ratio": 2 / 3}
    assert item.annotation.output["items"] == [{"region_id": "n12"}]
    lines = current_parts(llm.prompts[1])[-1].text.split(chr(10))
    assert [line for line in lines if "unknown region reference" in line] == [
        "1. /items/0/region_id: unknown region reference"]

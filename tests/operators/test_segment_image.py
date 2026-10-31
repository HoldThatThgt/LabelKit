"""M14 图片阶段（SPEC-image-region-segmentation §10、§9.4；AC-20 至 AC-28、AC-38 至 AC-47、AC-125）。

没有 mock LLM 服务：多数用例是 ``complete_post_validated`` 接缝的进程内桩，修复轮用例是真实
``SchemaEngine`` 加 ``complete`` 接缝的进程内桩。
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import random
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from labelkit.common.config.model import LLMProfile, OutputConfig, SegmentConfig
from labelkit.common.contracts.post_validation import ValidatedPostCall
from labelkit.common.contracts.regions import ReferenceBox, RegionSet, render_reference_boxes
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import ImageRef, PipelineItem, Record, RecordRef, Usage
from labelkit.common.errors import (
    CircuitBreakerTripped,
    ContextOverflowError,
    OutputTruncatedError,
    ProviderFatalError,
    ProviderRetryableError,
)
from labelkit.common.extensions.hooks import ResolvedHook
from labelkit.common.inference.schema_engine import SchemaEngine, segment_image_schema
from labelkit.common.observability import obslog
from labelkit.operators import segment_image as stage_module
from labelkit.operators.segment_image import SegmentImageStage, _read_canvas, _SYSTEM_STATIC, build_segment_image_prompt
from labelkit.orchestration.factory import build_stages
from tests.image_damage_helpers import ALL_DAMAGES, RESIZED_PX, UNDER_REQUEST_PX, write_damaged_image
from tests.operators.test_segment import make_cfg as stream_cfg

ROOT = Path(__file__).resolve().parents[2]
INSTRUCTION = "把每个按钮分割出来。"
PROFILE = "vision"
SECRET = "SECRET-INPUT-TEXT"
BOX = {"id": "n12", "bbox": [43, 140, 821, 1264], "text": "Button 登录"}
SQUARE = [[100, 100], [300, 100], [300, 300], [100, 300], [100, 100]]
DRAWN = {"box_id": None, "polygons": [{"exterior": SQUARE, "holes": []}], "source_box_ids": None}
BOWTIE = [[111, 222], [333, 444], [333, 222], [111, 444], [111, 222]]


def profile(context_window: int = 1_000_000) -> LLMProfile:
    return LLMProfile(name=PROFILE, provider="openai_compatible", base_url="http://localhost", model="glm-5.2",
                      api_key_env="K", max_output_tokens=256, context_window=context_window, supports_vision=True)


def make_cfg(*, instruction: str = INSTRUCTION, hook=None, context_window: int = 1_000_000):
    """图片模式配置；resolved_box_parser 直接给可调用对象，不经文件加载。"""
    resolved = None if hook is None else ResolvedHook(reference="hooks.py:parse", target=hook)
    segment = SegmentConfig(enabled=True, mode="image", llm=PROFILE, instruction=instruction,
                            box_parser=None if hook is None else "hooks.py:parse",
                            companion_suffix=None if hook is None else ".xml", resolved_box_parser=resolved)
    profiles = {PROFILE: profile(context_window)} if instruction else {}
    return dataclasses.replace(stream_cfg(llm_profiles=profiles), segment=segment)


def write_image(path: Path, size: tuple[int, int] = (1080, 2340)) -> Path:
    Image.new("RGB", size, (200, 200, 200)).save(path)
    return path


def make_item(tmp_path: Path, index: int = 1, *, companion: bytes | None = None,
              size: tuple[int, int] = (1080, 2340)) -> PipelineItem:
    path = write_image(tmp_path / f"image_{index}.png", size)
    image = ImageRef(path=path, format="png", size_bytes=path.stat().st_size)
    record = Record(id=f"{index:016d}", modality="ui", text=None, raw=None, ui_tree=None, image=image,
                    ref=RecordRef(path.name, None, index, ()), companion=companion, image_sha256="0" * 64)
    return PipelineItem(record=record)


class Metrics:
    """记录计数、事件与熔断喂入。"""

    def __init__(self):
        self.counts: dict[str, int] = {}
        self.events: list[tuple] = []
        self.fed: list[bool] = []

    def count(self, key, n=1):
        self.counts[key] = self.counts.get(key, 0) + n

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None):
        self.events.append((ev, stage, tuple(record_ids), payload))

    def record_provider_result(self, fatal, *, hard=False):
        self.fed.append(fatal)

    def named(self, name):
        return [event for event in self.events if event[0] == name]


class Tasks:
    """按声明序内联执行叶任务，异常原样传播。"""

    def __init__(self):
        self.groups = []

    async def run_group(self, request):
        self.groups.append(request)
        return tuple([await task.operation() for task in request.tasks])


class ReverseTasks(Tasks):
    """按声明序的逆序完成叶任务，结果仍按输入序返回。"""

    async def run_group(self, request):
        self.groups.append(request)
        results = [None] * len(request.tasks)
        for index in reversed(range(len(request.tasks))):
            results[index] = await request.tasks[index].operation()
        return tuple(results)


class SeamEngine:
    """``complete_post_validated`` 接缝的进程内桩：候选真实经过请求自带的校验器。"""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    async def complete_post_validated(self, request):
        outcome = self.outcomes[min(len(self.requests), len(self.outcomes) - 1)]
        self.requests.append(request)
        if isinstance(outcome, BaseException):
            raise outcome
        result = request.post_validator(outcome)
        assert result.violations == ()
        return ValidatedPostCall(outcome, result.proof, "l0_or_clean", Usage(11, 7), 1, "glm-5.2")


class QueueLLM:
    """``complete`` 接缝的进程内桩，供真实 SchemaEngine 跑修复轮；每轮用量固定 (5, 2)。"""

    def __init__(self, *items):
        self.items = list(items)
        self.prompts = []

    async def complete(self, profile_name, prompt, response_schema=None):
        item = self.items[min(len(self.prompts), len(self.items) - 1)]
        self.prompts.append(prompt)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=item, structured=None, usage=Usage(5, 2), model="glm-5.2")


class EncodingLLM(QueueLLM):
    """与 M9 编码请求时相同：先逐张取字节，全部取到才算发出（才记入 prompts）。"""

    def __init__(self, *items, max_px: int):
        super().__init__(*items)
        self.max_px = max_px

    async def complete(self, profile_name, prompt, response_schema=None):
        for message in prompt.messages:
            for part in message.parts:
                if part.kind == "image":
                    part.image.load_base64(self.max_px)
        return await super().complete(profile_name, prompt, response_schema)


def make_ctx(cfg, engine, *, tasks=None, metrics=None) -> RunContext:
    return RunContext(cfg=cfg, llm=SimpleNamespace(calibrator=SimpleNamespace(cost=lambda name: 100)),
                      schema_engine=engine, rng=random.Random(0), batch_no=3, metrics=metrics or Metrics(),
                      tasks=tasks or Tasks(), task_namespace="run:batch:3:stage:segment")


def run(cfg, batch, engine=None, **kw):
    ctx = make_ctx(cfg, engine if engine is not None else SeamEngine({"regions": []}), **kw)
    returned = asyncio.run(SegmentImageStage(cfg).run(batch, ctx))
    assert returned is batch
    return ctx


def real_engine(llm, metrics, repairs: int = 2) -> SchemaEngine:
    return SchemaEngine({}, llm=llm, cfg=OutputConfig(max_repair_attempts=repairs), metrics=metrics)


def text_of(message) -> str:
    return "\n".join(part.text for part in message.parts if part.kind == "text")


def assert_failed(item: PipelineItem, kind: str) -> None:
    assert item.status == "failed"
    assert item.regions is None
    assert [(error.stage, error.kind) for error in item.errors] == [("segment", kind)]


# ── 提示词（§10.2）──────────────────────────────────────────────────────────

def _frozen_block(document: str, heading: str) -> str:
    text = (ROOT / document).read_text(encoding="utf-8")
    return text.split(heading, 1)[1].split("```text\n", 1)[1].split("\n```", 1)[0]


def test_system_prompt_matches_frozen_text(tmp_path):
    item = make_item(tmp_path)
    assert _SYSTEM_STATIC == _frozen_block("docs/dev/SPEC-image-region-segmentation.md", "### 10.2 ")
    assert _SYSTEM_STATIC == _frozen_block("docs/CONTRACTS.md", "### 10.20 ")
    prompt = build_segment_image_prompt(item.record, RegionSet(10, 10, (), ()), INSTRUCTION)
    system = prompt.messages[0]
    assert system.role == "system"
    assert [part.kind for part in system.parts] == ["text"]
    assert system.parts[0].text == f"{_SYSTEM_STATIC}\n{INSTRUCTION}"


def test_images_only_in_user_message(tmp_path):
    item = make_item(tmp_path)
    prompt = build_segment_image_prompt(item.record, RegionSet(10, 10, (), ()), INSTRUCTION)
    assert [message.role for message in prompt.messages] == ["system", "user"]
    assert [part.kind for part in prompt.messages[1].parts] == ["text", "image", "text"]
    assert prompt.messages[1].parts[0].text == "[图片]"
    assert prompt.messages[1].parts[1].image is item.record.image


def test_instruction_only_prompt_has_no_boxes(tmp_path):
    engine = SeamEngine({"regions": []})
    run(make_cfg(), [make_item(tmp_path)], engine)
    assert engine.requests[0].prompt.messages[1].parts[2].text == "本样本没有参考框。"


def test_instruction_and_boxes_prompt_contains_rendered_boxes(tmp_path):
    engine = SeamEngine({"regions": []})
    item = make_item(tmp_path, companion=b"<xml/>")
    run(make_cfg(hook=lambda companion, image: [BOX]), [item], engine)
    assert engine.requests[0].prompt.messages[1].parts[2].text == (
        '[参考框]\n{"box_id": "n12", "bbox": [39, 59, 761, 541], "text": "Button 登录"}')
    assert engine.requests[0].prompt.messages[1].parts[2].text.endswith(render_reference_boxes(item.regions))


def test_request_uses_internal_schema_and_scope(tmp_path):
    engine = SeamEngine({"regions": []})
    tasks = Tasks()
    item = make_item(tmp_path)
    run(make_cfg(), [item], engine, tasks=tasks)
    spec = tasks.groups[0].tasks[0]
    assert spec.resource_key == ("llm", PROFILE)
    assert spec.stage == "segment"
    request = engine.requests[0]
    assert request.profile == PROFILE
    assert request.scope.record_ids == (item.record.id,)
    assert request.scope.batch_no == 3
    assert request.scope.repair_context_bytes is None
    assert request.schema == _frozen_schema()


def _frozen_schema():
    from labelkit.common.contracts.post_validation import _freeze_json

    return _freeze_json(segment_image_schema())


# ── 三条候选路径 ────────────────────────────────────────────────────────────

def test_valid_boxes_frozen_into_region_set(tmp_path):
    calls = []

    def hook(companion, image):
        calls.append((companion, image))
        return [BOX, {"id": "n13", "bbox": [0.5, 1.5, 2.5, 3.5], "text": ""}]

    item = make_item(tmp_path, companion=b"\xff\xfe<raw>")
    run(make_cfg(hook=hook), [item], SeamEngine({"regions": []}))
    assert calls == [(b"\xff\xfe<raw>", {"width": 1080, "height": 2340})]
    assert item.regions.boxes == (ReferenceBox("n12", (43, 140, 821, 1264), "Button 登录"),
                                  ReferenceBox("n13", (0.5, 1.5, 2.5, 3.5), ""))
    assert item.regions.regions == ()


def test_hook_called_once_and_boxes_shared_by_first_round_and_repair(tmp_path):
    calls = []

    def hook(companion, image):
        calls.append(companion)
        return [BOX]

    metrics = Metrics()
    llm = QueueLLM('{"regions": [{"box_id": "nope", "polygons": null, "source_box_ids": null}]}',
                   '{"regions": [{"box_id": "n12", "polygons": null, "source_box_ids": null}]}')
    item = make_item(tmp_path, companion=b"x")
    run(make_cfg(hook=hook), [item], real_engine(llm, metrics), metrics=metrics)
    assert calls == [b"x"]
    assert len(llm.prompts) == 2
    assert [region.region_id for region in item.regions.regions] == ["n12"]
    assert llm.prompts[1].messages[1] is llm.prompts[0].messages[1]


def test_no_companion_with_instruction_skips_hook(tmp_path):
    def hook(companion, image):
        raise AssertionError("hook must not run without a companion")

    item = make_item(tmp_path, companion=None)
    engine = SeamEngine({"regions": [DRAWN]})
    run(make_cfg(hook=hook), [item], engine)
    assert item.status == "active"
    assert item.regions.boxes == ()
    assert [region.region_id for region in item.regions.regions] == ["r1"]


def test_zero_byte_companion_passed_to_hook(tmp_path):
    seen = []

    def hook(companion, image):
        seen.append(companion)
        return []

    run(make_cfg(hook=hook), [make_item(tmp_path, companion=b"")], SeamEngine({"regions": []}))
    assert seen == [b""]


@pytest.mark.parametrize("boxes", [[], [{"bbox": [0, 0, 1080, 2340], "text": "FrameLayout"}]],
                         ids=["empty", "window_shell"])
def test_model_path_continues_when_hook_returns_empty_or_shell(tmp_path, boxes):
    """钩子返回空数组或只有外壳框时，模型照常依据图片生成候选。"""
    item = make_item(tmp_path, companion=b"x")
    engine = SeamEngine({"regions": [DRAWN]})
    run(make_cfg(hook=lambda companion, image: boxes), [item], engine)
    assert len(engine.requests) == 1
    assert len(item.regions.boxes) == len(boxes)
    assert [region.region_id for region in item.regions.regions] == ["r1"]


def test_unreused_boxes_do_not_become_candidates(tmp_path):
    other = {"id": "n13", "bbox": [0, 0, 5, 5], "text": ""}
    item = make_item(tmp_path, companion=b"x")
    run(make_cfg(hook=lambda companion, image: [BOX, other]), [item],
        SeamEngine({"regions": [{"box_id": "n13", "polygons": None, "source_box_ids": None}]}))
    assert len(item.regions.boxes) == 2
    assert [region.region_id for region in item.regions.regions] == ["n13"]


def test_reused_box_geometry_equals_hook_pixel_rectangle(tmp_path):
    """复用框不经网格：几何与钩子给出的像素矩形逐值相等，数值类型保留。"""
    box = {"id": "b", "bbox": [1, 2.25, 333, 444.5], "text": ""}
    item = make_item(tmp_path, companion=b"x", size=(1000, 1000))
    run(make_cfg(hook=lambda companion, image: [box]), [item],
        SeamEngine({"regions": [{"box_id": "b", "polygons": None, "source_box_ids": None}]}))
    exterior = item.regions.regions[0].polygons[0].exterior
    assert exterior == ((1, 2.25), (333, 2.25), (333, 444.5), (1, 444.5), (1, 2.25))
    assert [type(value) for value in exterior[0]] == [int, float]


def test_boxes_only_submits_no_task_group(tmp_path):
    """D15：不提交任务组、不碰 runtime、LLM、Schema 引擎与预算，连 profile 表都是空的。"""
    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError(f"D15 route touched a runtime resource: {name}")

    metrics = Metrics()
    cfg = make_cfg(instruction="", hook=lambda companion, image: [BOX])
    assert cfg.llm_profiles == {}
    ctx = RunContext(cfg=cfg, llm=Forbidden(), schema_engine=Forbidden(), rng=Forbidden(), batch_no=1,
                     metrics=metrics, tasks=Forbidden(), task_namespace="unused")
    item = make_item(tmp_path, companion=b"x")
    assert asyncio.run(SegmentImageStage(cfg).run([item], ctx))[0] is item
    assert [region.region_id for region in item.regions.regions] == ["n12"]
    assert "segment_image.calls" not in metrics.counts
    assert "segment_image.prompt_tokens" not in metrics.counts
    assert metrics.named("segment.regions")[0][3]["attempts"] == 0


def test_boxes_only_candidates_match_boxes_in_order(tmp_path):
    boxes = [{"id": name, "bbox": [index, 0, index + 1, 1], "text": name}
             for index, name in enumerate(("z", "a", "m"))]
    item = make_item(tmp_path, companion=b"x")
    run(make_cfg(instruction="", hook=lambda companion, image: boxes), [item])
    regions = item.regions
    assert [box.box_id for box in regions.boxes] == ["z", "a", "m"]
    assert [region.region_id for region in regions.regions] == ["z", "a", "m"]
    assert [region.box_id for region in regions.regions] == ["z", "a", "m"]
    assert [region.source_box_ids for region in regions.regions] == [("z",), ("a",), ("m",)]
    assert regions.regions[1].label_anchor == (1.5, 0.5)


def test_boxes_only_empty_list_is_legal_region_set(tmp_path):
    item = make_item(tmp_path, companion=b"x")
    ctx = run(make_cfg(instruction="", hook=lambda companion, image: []), [item])
    assert item.status == "active"
    assert item.errors == []
    assert item.regions == RegionSet(1080, 2340, (), ())
    assert ctx.metrics.counts["segment_image.empty"] == 1


def test_boxes_only_empty_and_shell_candidates(tmp_path):
    empty, shell = make_item(tmp_path, 1, companion=b"e"), make_item(tmp_path, 2, companion=b"s")
    outer = {"bbox": [0, 0, 1080, 2340], "text": "FrameLayout"}

    def hook(companion, image):
        return [] if companion == b"e" else [outer]

    run(make_cfg(instruction="", hook=hook), [empty, shell])
    assert empty.regions.regions == ()
    assert [(region.region_id, region.box_id) for region in shell.regions.regions] == [("1", "1")]


def test_empty_model_list_is_legal_region_set(tmp_path):
    item = make_item(tmp_path)
    ctx = run(make_cfg(), [item], SeamEngine({"regions": []}))
    assert item.status == "active"
    assert item.errors == []
    assert item.regions == RegionSet(1080, 2340, (), ())
    assert ctx.metrics.counts["segment_image.empty"] == 1
    assert ctx.metrics.named("error") == []


# ── 失败语义（§10.7）────────────────────────────────────────────────────────

def test_hook_none_fails_companion_invalid_without_model_call(tmp_path):
    engine = SeamEngine({"regions": []})
    tasks = Tasks()
    item = make_item(tmp_path, companion=b"x")
    ctx = run(make_cfg(hook=lambda companion, image: None), [item], engine, tasks=tasks)
    assert_failed(item, "companion_invalid")
    assert item.errors[0].message == "box_parser rejected companion"
    assert item.errors[0].retryable is False
    assert engine.requests == []
    assert tasks.groups == []
    assert ctx.metrics.counts["segment_image.failed.companion_invalid"] == 1
    assert "bad_input" not in ctx.metrics.counts
    assert ctx.metrics.named("error")[0][3]["kind"] == "companion_invalid"


def _boom(companion, image):
    raise ValueError(SECRET)


@pytest.mark.parametrize("hook, reason", [
    (_boom, "exception"),
    (lambda companion, image: ({"bbox": [0, 0, 1, 1], "text": ""},), "return_type"),
    (lambda companion, image: [{"bbox": [0, 0, math.nan, 1], "text": ""}], "invalid_json"),
    (lambda companion, image: [{"bbox": [0, 0, 1, 1], "text": "", "note": "x"}], "invalid_box"),
    (lambda companion, image: [{"bbox": [1, 0, 1, 1], "text": ""}], "invalid_box"),
    (lambda companion, image: [{"bbox": [0, 0, 1081, 1], "text": ""}], "invalid_box"),
    (lambda companion, image: [{"id": "a", "bbox": [0, 0, 1, 1], "text": ""},
                               {"id": "a", "bbox": [0, 0, 2, 2], "text": ""}], "invalid_box"),
    (lambda companion, image: [{"id": "r2", "bbox": [0, 0, 1, 1], "text": ""}], "invalid_box"),
    (lambda companion, image: [{"id": "a b", "bbox": [0, 0, 1, 1], "text": ""}], "invalid_box"),
    (lambda companion, image: [{"id": "a\nb", "bbox": [0, 0, 1, 1], "text": ""}], "invalid_box"),
    (lambda companion, image: [{"id": "a", "bbox": [0, 0, 1, 1], "text": ""},
                               {"bbox": [0, 0, 2, 2], "text": ""}], "invalid_box"),
], ids=["raises", "tuple", "nan", "extra_key", "zero_area", "out_of_canvas", "duplicate_id", "reserved_id",
        "id_with_space", "id_with_newline", "partial_naming"])
def test_box_parser_contract_violation_fails_internal_error_without_model_call(tmp_path, hook, reason):
    engine = SeamEngine({"regions": []})
    tasks = Tasks()
    item = make_item(tmp_path, companion=b"x")
    ctx = run(make_cfg(hook=hook), [item], engine, tasks=tasks)
    assert_failed(item, "internal_error")
    assert engine.requests == []
    assert tasks.groups == []
    assert ctx.metrics.counts["segment_image.failed.internal_error"] == 1
    assert item.errors[0].message == f"BoxParserError: {reason}"


def test_box_parser_failure_log_has_no_data(tmp_path, caplog):
    def hook(companion, image):
        return [{"id": "ok", "bbox": [0, 0, 1, 1], "text": SECRET},
                {"id": "bad", "bbox": [0, 0, 0, 0], "text": SECRET}]

    item, raising = make_item(tmp_path, 1, companion=SECRET.encode()), make_item(tmp_path, 2, companion=b"x")

    def dispatch(companion, image):
        return hook(companion, image) if companion != b"x" else _boom(companion, image)

    with caplog.at_level(logging.DEBUG):
        ctx = run(make_cfg(hook=dispatch), [item, raising])
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "reason=invalid_box box_index=1 hook=hooks.py:parse" in logged
    assert "reason=exception box_index=None hook=hooks.py:parse" in logged
    assert SECRET not in logged
    assert SECRET not in caplog.text
    assert SECRET not in repr(ctx.metrics.events)
    assert SECRET not in repr(item.errors) + repr(raising.errors)


def test_precheck_overflow_fails_without_feeding_breaker(tmp_path):
    engine = SeamEngine({"regions": []})
    item = make_item(tmp_path)
    ctx = run(make_cfg(context_window=300), [item], engine)
    assert_failed(item, "context_overflow")
    assert engine.requests == []
    assert ctx.metrics.counts["budget.overflow_records"] == 1
    assert ctx.metrics.fed == []
    assert ctx.metrics.counts["segment_image.calls"] == 1


@pytest.mark.parametrize("origin, fed", [("http_400", [True]), ("finish", [])])
def test_reactive_overflow_feeds_breaker_exactly_once(tmp_path, origin, fed):
    """服务端 400 终局恰一次喂熔断；200 形态的 finish 不喂。"""
    error = ContextOverflowError("provider says too long", phase="reactive", profile=PROFILE, origin=origin)
    first, second = make_item(tmp_path, 1), make_item(tmp_path, 2)
    ctx = run(make_cfg(), [first], SeamEngine(error))
    assert_failed(first, "context_overflow")
    assert ctx.metrics.fed == fed
    assert ctx.metrics.counts["budget.overflow_records"] == 1
    # 同一个异常对象再次穿越也不重复喂
    ctx = run(make_cfg(), [second], SeamEngine(error))
    assert ctx.metrics.fed == []


def test_first_round_overflow_through_real_engine_is_context_overflow(tmp_path):
    metrics = Metrics()
    llm = QueueLLM(ContextOverflowError("too long", phase="reactive", profile=PROFILE))
    item = make_item(tmp_path)
    run(make_cfg(), [item], real_engine(llm, metrics), metrics=metrics)
    assert_failed(item, "context_overflow")
    assert metrics.fed == [True]
    assert metrics.counts["budget.overflow_records"] == 1


def _candidate(exterior) -> str:
    import json

    return json.dumps({"regions": [{"box_id": None, "polygons": [{"exterior": exterior, "holes": []}],
                                    "source_box_ids": None}]})


def test_repair_exhausted_is_segmentation_invalid(tmp_path):
    metrics = Metrics()
    llm = QueueLLM(_candidate(BOWTIE))
    item = make_item(tmp_path)
    run(make_cfg(), [item], real_engine(llm, metrics), metrics=metrics)
    assert_failed(item, "segmentation_invalid")
    assert len(llm.prompts) == 3                       # 首轮 + 最多两轮 L3
    assert item.errors[0].message == "(post-validator) /regions/0/polygons/0: invalid geometry (Self-intersection)"
    assert metrics.counts["segment_image.prompt_tokens"] == 15
    assert metrics.counts["segment_image.completion_tokens"] == 6
    assert metrics.named("segment.regions") == []
    assert "budget.overflow_records" not in metrics.counts


def test_repair_overflow_is_segmentation_invalid(tmp_path):
    """D11：修复轮溢出沿用现行归因，由 M8 恰一次喂熔断，不是 context_overflow。"""
    metrics = Metrics()
    llm = QueueLLM(_candidate(BOWTIE), ContextOverflowError("repair too long", phase="reactive", profile=PROFILE))
    item = make_item(tmp_path)
    run(make_cfg(), [item], real_engine(llm, metrics), metrics=metrics)
    assert_failed(item, "segmentation_invalid")
    assert metrics.fed == [True]
    assert "budget.overflow_records" not in metrics.counts
    assert metrics.counts["segment_image.prompt_tokens"] == 5


def test_truncated_output_is_not_empty_candidates(tmp_path):
    item = make_item(tmp_path)
    ctx = run(make_cfg(), [item], SeamEngine(OutputTruncatedError("hit max tokens", profile=PROFILE)))
    assert_failed(item, "output_truncated")
    assert "segment_image.empty" not in ctx.metrics.counts
    assert ctx.metrics.fed == []


@pytest.mark.parametrize("error, kind, retryable", [
    (ProviderRetryableError("exhausted", PROFILE, 3), "provider_retryable_exhausted", True),
    (ProviderFatalError("forbidden", PROFILE, 403), "provider_fatal", False),
], ids=["retryable", "fatal"])
def test_provider_errors_keep_standard_kinds(tmp_path, error, kind, retryable):
    item = make_item(tmp_path)
    run(make_cfg(), [item], SeamEngine(error))
    assert_failed(item, kind)
    assert item.errors[0].retryable is retryable


@pytest.mark.parametrize("error", [CircuitBreakerTripped("open"), asyncio.CancelledError()],
                         ids=["breaker", "cancelled"])
def test_circuit_breaker_and_cancellation_propagate(tmp_path, error):
    item = make_item(tmp_path)
    cfg = make_cfg()
    with pytest.raises(type(error)):
        asyncio.run(SegmentImageStage(cfg).run([item], make_ctx(cfg, SeamEngine(error))))
    assert item.errors == []


def _raises(candidate, boxes, width, height):
    raise RuntimeError(SECRET)


@pytest.mark.parametrize("validator, code", [(_raises, "post_validator_exception"),
                                             (lambda *args: object(), "post_validator_invalid")],
                         ids=["raises", "invalid_return"])
def test_validator_exception_is_internal_error_without_repair(tmp_path, monkeypatch, validator, code):
    monkeypatch.setattr(stage_module, "validate_regions", validator)
    metrics = Metrics()
    llm = QueueLLM(_candidate(SQUARE))
    item = make_item(tmp_path)
    run(make_cfg(), [item], real_engine(llm, metrics), metrics=metrics)
    assert_failed(item, "internal_error")
    assert item.errors[0].message == code
    assert len(llm.prompts) == 1
    assert SECRET not in repr(metrics.events)


def test_non_region_set_proof_is_internal_error(tmp_path, monkeypatch):
    from labelkit.common.contracts.post_validation import PostValidationResult

    monkeypatch.setattr(stage_module, "validate_regions", lambda *args: PostValidationResult((), object()))
    metrics = Metrics()
    item = make_item(tmp_path)
    run(make_cfg(), [item], real_engine(QueueLLM(_candidate(SQUARE)), metrics), metrics=metrics)
    assert_failed(item, "internal_error")


@pytest.mark.parametrize("damage", ["deleted", "garbage", "pixel_limit", "pixel_warning"])
def test_image_changed_after_ingest_is_decode_error(tmp_path, monkeypatch, damage):
    """文件头再读失败归 image_decode_error：不调用钩子、不调用模型。"""
    def hook(companion, image):
        raise AssertionError("hook must not run on an unreadable image")

    item = make_item(tmp_path, companion=b"x", size=(100, 100))
    path = item.record.image.path
    if damage == "deleted":
        path.unlink()
    elif damage == "garbage":
        path.write_bytes(b"not an image")
    elif damage == "pixel_limit":
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)   # 超过 2 倍上限：DecompressionBombError
    else:
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 9000)  # 1–2 倍上限：DecompressionBombWarning
    engine = SeamEngine({"regions": []})
    ctx = run(make_cfg(hook=hook), [item], engine)
    assert_failed(item, "image_decode_error")
    assert engine.requests == []
    assert ctx.metrics.counts["segment_image.failed.image_decode_error"] == 1


def test_bomb_is_not_an_oserror_but_read_canvas_raises_one(tmp_path, monkeypatch):
    assert not issubclass(Image.DecompressionBombError, OSError)
    path = write_image(tmp_path / "image_1.png", (100, 100))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
    with pytest.raises(OSError):
        _read_canvas(ImageRef(path, "png", 1))


@pytest.mark.parametrize("max_px", [UNDER_REQUEST_PX, RESIZED_PX], ids=["unscaled", "resized"])
@pytest.mark.parametrize("damage", ALL_DAMAGES)
def test_image_load_failure_during_call_is_decode_error(tmp_path, damage, max_px):
    """文件头完好、像素数据在编码请求时才读不出：M9 的 OSError 穿过 M8 仍归 image_decode_error，字节不发出。"""
    metrics = Metrics()
    item = make_item(tmp_path)
    item.record = dataclasses.replace(item.record, image=write_damaged_image(tmp_path, damage))
    llm = EncodingLLM('{"regions": []}', max_px=max_px)
    run(make_cfg(), [item], real_engine(llm, metrics), metrics=metrics)
    assert_failed(item, "image_decode_error")
    assert llm.prompts == []
    assert metrics.counts["segment_image.failed.image_decode_error"] == 1


def test_unexpected_exception_is_internal_error_not_segmentation_invalid(tmp_path):
    item = make_item(tmp_path)
    run(make_cfg(), [item], SeamEngine(ValueError("broken invariant")))
    assert_failed(item, "internal_error")


# ── 修复轮 ──────────────────────────────────────────────────────────────────

def test_geometry_violation_enters_l3_and_recovers(tmp_path):
    metrics = Metrics()
    llm = QueueLLM(_candidate(BOWTIE), _candidate(SQUARE))
    item = make_item(tmp_path)
    run(make_cfg(), [item], real_engine(llm, metrics), metrics=metrics)
    assert item.status == "active"
    assert [region.region_id for region in item.regions.regions] == ["r1"]
    repair = llm.prompts[1]
    assert repair.messages[:2] == llm.prompts[0].messages       # 首轮消息（含图片）原样重放
    assert repair.messages[2].role == "assistant"
    assert text_of(repair.messages[3]) == (
        "[违规清单]\n1. (post-validator) /regions/0/polygons/0: invalid geometry (Self-intersection)\n\n"
        "只输出修正后的 JSON。")
    assert metrics.counts["segment_image.prompt_tokens"] == 10
    assert metrics.counts["segment_image.completion_tokens"] == 4
    assert metrics.named("segment.regions")[0][3]["attempts"] == 2


@pytest.mark.parametrize("region", [
    {"box_id": None, "polygons": [{"exterior": [[123, 456], [789, 456], [789, 987]] + [[123, 457]], "holes": []}],
     "source_box_ids": None},
    {"box_id": None, "polygons": [{"exterior": [[123, 456], [789, 987], [789, 456], [123, 987], [123, 456]],
                                   "holes": []}], "source_box_ids": None},
    {"box_id": None, "polygons": [{"exterior": [[123, 456], [789, 456], [789, 987], [123, 987], [123, 456]],
                                   "holes": []},
                                  {"exterior": [[789, 456], [901, 456], [901, 987], [789, 987], [789, 456]],
                                   "holes": []}], "source_box_ids": None},
    {"box_id": "secret-box", "polygons": None, "source_box_ids": None},
    {"box_id": None, "polygons": [{"exterior": SQUARE, "holes": []}], "source_box_ids": ["secret-box"]},
], ids=["unclosed", "self_intersection", "shared_edge", "unknown_box", "unknown_source"])
def test_repair_instruction_echoes_no_coordinates_or_model_values(tmp_path, region):
    """L3 新增的 user 消息只含受控文本，不回显坐标或模型给出的值。"""
    import json

    metrics = Metrics()
    llm = QueueLLM(json.dumps({"regions": [region]}), '{"regions": []}')
    item = make_item(tmp_path, companion=b"x")
    run(make_cfg(hook=lambda companion, image: [BOX]), [item], real_engine(llm, metrics), metrics=metrics)
    assert len(llm.prompts) == 2
    new_text = text_of(llm.prompts[1].messages[-1])
    assert "(post-validator) /regions/0" in new_text
    for value in ("123", "456", "789", "987", "901", "secret-box"):
        assert value not in new_text


def test_grid_range_violation_rendered_by_l2(tmp_path):
    """越界由 L2 判定并按 §10.6 的期望与实际措辞回显标量，不走后校验前缀。"""
    metrics = Metrics()
    llm = QueueLLM(_candidate([[0, 0], [1200, 0], [1200, 100], [0, 0]]), _candidate(SQUARE))
    item = make_item(tmp_path)
    run(make_cfg(), [item], real_engine(llm, metrics), metrics=metrics)
    new_text = text_of(llm.prompts[1].messages[-1])
    assert "/regions/0/polygons/0/exterior/1/0" in new_text
    assert "1200" in new_text
    assert "(post-validator)" not in new_text
    assert item.status == "active"


# ── 画布、批结算与事件 ──────────────────────────────────────────────────────

def test_canvas_uses_stored_pixels_without_rotation(tmp_path):
    """D4：带 Orientation = 6 的 JPEG 仍按存储像素网格投影，几何不旋转。"""
    path = tmp_path / "image_1.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6
    Image.new("RGB", (400, 100), (10, 20, 30)).save(path, exif=exif)
    with Image.open(path) as im:
        assert im.getexif()[0x0112] == 6
    record = Record(id="1" * 16, modality="ui", text=None, raw=None, ui_tree=None,
                    image=ImageRef(path, "jpeg", path.stat().st_size), ref=RecordRef(path.name, None, 1, ()))
    item = PipelineItem(record=record)
    run(make_cfg(), [item], SeamEngine({"regions": [DRAWN]}))
    assert (item.regions.width, item.regions.height) == (400, 100)
    assert item.regions.regions[0].polygons[0].exterior[1] == (120.0, 10.0)


def test_failures_are_isolated_and_only_active_records_are_processed(tmp_path):
    good, rejected, dropped = make_item(tmp_path, 1, companion=b"good"), make_item(tmp_path, 2, companion=b"bad"), \
        make_item(tmp_path, 3, companion=b"never")
    dropped.status = "dropped_dup"
    seen = []

    def hook(companion, image):
        seen.append(companion)
        return None if companion == b"bad" else [BOX]

    engine = SeamEngine({"regions": [{"box_id": "n12", "polygons": None, "source_box_ids": None}]})
    ctx = run(make_cfg(hook=hook), [good, rejected, dropped], engine)
    assert seen == [b"good", b"bad"]
    assert len(engine.requests) == 1
    assert good.status == "active" and good.regions is not None
    assert_failed(rejected, "companion_invalid")
    assert dropped.status == "dropped_dup" and dropped.regions is None
    assert ctx.metrics.counts["segment_image.samples"] == 2
    assert ctx.metrics.counts["segment_image.calls"] == 1
    assert ctx.metrics.counts["segment_image.boxes"] == 1
    assert ctx.metrics.counts["segment_image.regions"] == 1


def test_results_commit_in_declaration_order(tmp_path):
    first, second = make_item(tmp_path, 1), make_item(tmp_path, 2)
    tasks = ReverseTasks()
    ctx = run(make_cfg(), [first, second], SeamEngine({"regions": [DRAWN]}), tasks=tasks)
    assert [event[2] for event in ctx.metrics.named("segment.regions")] == [(first.record.id,), (second.record.id,)]
    specs = tasks.groups[0].tasks
    assert [spec.declaration_key for spec in specs] == [(3, 0, 0), (3, 0, 1)]
    assert len({spec.task_id for spec in specs}) == 2


def test_segment_regions_event_is_counts_only(tmp_path):
    assert obslog.EV_SEGMENT_REGIONS == "segment.regions"
    item = make_item(tmp_path, companion=b"x")
    engine = SeamEngine({"regions": [DRAWN, {"box_id": "n12", "polygons": None, "source_box_ids": None}]})
    ctx = run(make_cfg(hook=lambda companion, image: [BOX]), [item], engine)
    (ev, stage, record_ids, payload), = ctx.metrics.named("segment.regions")
    assert (ev, stage, record_ids) == ("segment.regions", "segment", (item.record.id,))
    assert list(payload) == ["regions", "reused", "drawn", "width", "height", "attempts"]
    assert payload == {"regions": 2, "reused": 1, "drawn": 1, "width": 1080, "height": 2340, "attempts": 1}
    assert all(type(value) is int for value in payload.values())
    assert ctx.metrics.counts["segment_image.prompt_tokens"] == 11
    assert ctx.metrics.counts["segment_image.completion_tokens"] == 7
    for tier in ("none", "refs", "excerpt", "full"):
        assert obslog.redact_payload(payload, tier) == payload


def test_factory_selects_stage_class_by_mode():
    cfg = make_cfg()
    assert type(build_stages(cfg)[0]) is SegmentImageStage
    assert build_stages(cfg)[0].name == "segment"
    from labelkit.operators.segment import SegmentStage

    assert type(build_stages(stream_cfg())[0]) is SegmentStage


def test_stage_does_not_use_session_or_stream_machinery(tmp_path):
    """图片样本走普通批路径：不打 session 戳，不改 Record，不追加信封。"""
    item = make_item(tmp_path)
    record = item.record
    batch = [item]
    run(make_cfg(), batch, SeamEngine({"regions": [DRAWN]}))
    assert batch == [item]
    assert item.record is record
    assert item.session_id is None

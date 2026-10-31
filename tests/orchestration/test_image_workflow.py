"""图片模式端到端编排（SPEC-image-region-segmentation §19）；进程内桩，没有 LLM。"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import logging
import random
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from shapely.geometry import Polygon, box, mapping

from labelkit.common.config.model import ClassifyConfig, CliOverrides, LLMProfile, SegmentConfig
from labelkit.common.contracts.regions import RegionOverlayRef, RegionSet
from labelkit.common.contracts.types import Annotation, ImageRef, PipelineItem, Record, RecordRef, StageError, Usage
from labelkit.common.extensions.hooks import ResolvedHook
from labelkit.common.inference.schema_engine import SchemaEngine
from labelkit.operators.segment_image import SegmentImageStage
from labelkit.orchestration import application, process_workflow, session_workflow
from labelkit.orchestration.process_workflow import ProcessWorkflow
from tests.integration import test_segment_image_llm as gates
from tests.operators.test_annotate_image import make_cfg as annotate_make_cfg, prompt_for as annotate_prompt_for
from tests.operators.test_image_examples import UNKNOWN_REFERENCE, Scripted
from tests.operators.test_segment_image import DRAWN, SeamEngine
from tests.orchestration.test_process_workflow import build, make_cfg

PROFILE = "vision"


def _image_record(tmp_path, index: int, companion: bytes | None = None) -> Record:
    path = tmp_path / f"image_{index}.png"
    Image.new("RGB", (64, 32), (index, index, index)).save(path)
    return Record(id=f"{index:016d}", modality="ui", text=None, raw=None, ui_tree=None,
                  image=ImageRef(path, "png", path.stat().st_size), ref=RecordRef(path.name, None, index, ()),
                  companion=companion, image_sha256=f"{index:064d}")


class _SeesAttempt:
    """紧跟 segment 的探针：记录每个阶段上下文的会话归属与信封。"""

    name = "annotate"

    def __init__(self):
        self.attempts = []
        self.batches = []

    async def run(self, batch, ctx):
        self.attempts.append(ctx.session_attempt)
        self.batches.append(list(batch))
        return batch


def _forbid_sessions(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("image records must never reach the session workflow")

    monkeypatch.setattr(session_workflow, "SessionWorkflow", forbidden)


@pytest.mark.parametrize("route", ["model", "boxes_only"])
async def test_image_records_never_reach_session_workflow(tmp_path, monkeypatch, route):
    """两张独立图片走普通批路径：同一批、session_attempt 为空、没有会话 id，也没有序列信封。"""
    _forbid_sessions(monkeypatch)
    hook = ResolvedHook("hooks.py:parse", lambda companion, image: [{"id": "n1", "bbox": [0, 0, 8, 8], "text": ""}])
    if route == "model":
        segment = SegmentConfig(enabled=True, mode="image", llm=PROFILE, instruction="分割")
        companion = None
    else:
        segment = SegmentConfig(enabled=True, mode="image", box_parser="hooks.py:parse", companion_suffix=".xml",
                                resolved_box_parser=hook)
        companion = b"x"
    cfg = make_cfg(tmp_path, batch_size=8, dedup=False, annotate=True, segment=segment, modality="ui")
    assert cfg.segment.sequence_resolved is False
    if route == "model":
        profile = LLMProfile(name=PROFILE, provider="openai_compatible", base_url="http://localhost", model="m",
                             api_key_env="K", max_output_tokens=256, context_window=1_000_000, supports_vision=True)
        cfg = dataclasses.replace(cfg, llm_profiles={PROFILE: profile})
    probe = _SeesAttempt()
    engine = SeamEngine({"regions": [DRAWN]})
    calibrator = SimpleNamespace(cost=lambda name: 10, freeze_batch=lambda: None)
    orch, _metrics, _emitter, ingestor = build(
        cfg, [SegmentImageStage(cfg), probe],
        [_image_record(tmp_path, 1, companion), _image_record(tmp_path, 2, companion)],
        schema_engine=engine, llm=SimpleNamespace(calibrator=calibrator))
    await orch.run()
    assert ingestor.records_called is True
    assert probe.attempts == [None]
    batch = probe.batches[0]
    assert [item.record.ref.pair_index for item in batch] == [1, 2]
    assert all(item.session_id is None and item.session_position is None for item in batch)
    assert all(item.record.kind == "single" for item in batch)
    assert all(item.status == "active" and item.regions is not None for item in batch)
    assert len(engine.requests) == (2 if route == "model" else 0)


# ── 真实 M1、摄取、阶段链、引擎与发射器；只在 LLMClient.complete 接缝上替身 ───────────────

_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = """
schema_version = 1

[llm.default]
provider = "anthropic"
base_url = "https://api.z.ai/api/anthropic"
model = "glm-5.2"
api_key_env = "LABELKIT_ZAI_KEY"
supports_structured_output = true
supports_vision = true
context_window = 131072

[llm.other]
provider = "anthropic"
base_url = "https://api.z.ai/api/anthropic"
model = "glm-5.2"
api_key_env = "LABELKIT_ZAI_KEY"
supports_structured_output = true
supports_vision = true
context_window = 131072
"""
_HOOKS = """
from pathlib import Path

CALLS = Path(__file__).with_name("hook-calls.log")


def parse(companion, image):
    kind = companion.decode("utf-8")
    with CALLS.open("a", encoding="utf-8") as handle:
        handle.write(kind + chr(10))
    if kind == "reject":
        return None
    if kind == "boom":
        raise ValueError("defect")
    if kind == "empty":
        return []
    if kind == "unnamed":
        return [{"bbox": [0, 0, 10, 10], "text": "甲"}, {"bbox": [10, 10, 20, 20], "text": "乙"}]
    return [{"id": "n1", "bbox": [0, 0, 10, 10], "text": "甲"}, {"id": "n2", "bbox": [10, 10, 20, 20], "text": "乙"}]


def complete(candidate, context):
    if context is None:
        return candidate
    known = [box["id"] for box in context["boxes"]]
    return {"items": [{**item, "context_box_ids": known} for item in candidate["items"]]}
"""
_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["region_id", "label", "context_box_ids"],
        "properties": {
            "region_id": {"type": "string", "x-labelkit-region-ref": True},
            "label": {"type": "string"},
            "context_box_ids": {"x-labelkit-postprocessor": True, "type": "array", "items": {"type": "string"}},
        }}}},
}
_MODEL = 'llm = "default"\ninstruction = "分割"'
_MODEL_WITH_HOOK = _MODEL + '\nbox_parser = "hooks.py:parse"\ncompanion_suffix = ".cmp"'
_BOXES_ONLY = 'box_parser = "hooks.py:parse"\ncompanion_suffix = ".cmp"'
_ANNOTATE = '[annotate]\nenabled = true\nllm = "default"\ninstruction = "标注"\npostprocessor = "hooks.py:complete"\n'
_RING = [[100, 100], [400, 100], [400, 400], [100, 100]]
_DRAWN_REGION = {"box_id": None, "polygons": [{"exterior": _RING, "holes": []}], "source_box_ids": None}
_REUSED_REGION = {"box_id": "n1", "polygons": None, "source_box_ids": None}
_UNCLOSED_REGION = {"box_id": None, "source_box_ids": None,
                    "polygons": [{"exterior": [[0, 0], [10, 0], [10, 10], [0, 10]], "holes": []}]}
_TRACE = '[trace]\nenabled = true\nchannels = ["segment"]\ncontent = "{content}"\n'


class _Workspace:
    """临时工程目录：写配置、钩子、Schema 与输入，用真实装配跑一次。"""

    def __init__(self, root: Path, monkeypatch):
        self.root = root
        self.monkeypatch = monkeypatch
        self.data = root / "data"
        self.data.mkdir()
        (root / "out").mkdir()
        (root / "config.toml").write_text(_CONFIG, encoding="utf-8")
        (root / "hooks.py").write_text(_HOOKS, encoding="utf-8")
        (root / "schema.json").write_text(json.dumps(_SCHEMA), encoding="utf-8")
        monkeypatch.chdir(root)
        monkeypatch.setenv("LABELKIT_ZAI_KEY", "offline-test-key")
        monkeypatch.setattr(application, "setup_logging", lambda cfg: None)

    def sample(self, index: int, companion: str | None = None) -> None:
        """写一张噪声图（pHash 互不相近）与可选的伴随文件。"""
        pixels = random.Random(index).randbytes(100 * 50 * 3)
        Image.frombytes("RGB", (100, 50), pixels).save(self.data / f"image_{index}.png")
        if companion is not None:
            (self.data / f"image_{index}.cmp").write_text(companion, encoding="utf-8")

    def project(self, segment: str, *, annotate: str = _ANNOTATE, sections: str = "",
                output: str = 'meta_mode = "inline"\nrejects = "full"\n') -> None:
        """写工程文件；sections 追加 [dedup]、[trace] 等节。"""
        dedup = "" if "[dedup]" in sections else "[dedup]\nenabled = false\n"
        verify = "" if "[verify]" in sections else "[verify]\nenabled = false\n"
        (self.root / "project.toml").write_text(
            'schema_version = 1\n\n[run]\ninput = "./data"\noutput = "./out/run.jsonl"\nmodality = "ui"\n'
            f'batch_size = 8\nseed = 7\n\n[segment]\nenabled = true\nmode = "image"\n{segment}\n\n'
            f'[quality]\nenabled = false\n\n{annotate}\n{dedup}\n{verify}\n{sections}\n'
            f'[output]\nschema_path = "schema.json"\n{output}', encoding="utf-8")

    def run(self, respond, usage=None) -> Scripted:
        scripted = Scripted(respond, usage)
        self.monkeypatch.setattr(application, "LLMClient", scripted.client_type())
        code = application.execute_run(Path("config.toml"), Path("project.toml"), CliOverrides(console="plain"))
        assert code == 0
        return scripted

    @property
    def report(self) -> dict:
        return json.loads((self.root / "out" / "run.report.json").read_text(encoding="utf-8"))

    @property
    def rows(self) -> dict[int, dict]:
        lines = (self.root / "out" / "run.jsonl").read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in lines]
        return {row["_meta"]["source"]["pair_index"]: row for row in rows}

    @property
    def rejects(self) -> list[dict]:
        lines = (self.root / "out" / "run.rejects.jsonl").read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def events(self, name: str) -> list[dict]:
        lines = (self.root / "out" / "run.trace.jsonl").read_text(encoding="utf-8").splitlines()
        return [event for event in map(json.loads, lines) if event["ev"] == name]

    @property
    def hook_calls(self) -> list[str]:
        return (self.root / "hook-calls.log").read_text(encoding="utf-8").splitlines()


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> _Workspace:
    return _Workspace(tmp_path, monkeypatch)


def _select_all(call) -> dict:
    """annotate 应答：选中全部候选。"""
    return {"items": [{"region_id": candidate["region_id"], "label": "控件"} for candidate in call.candidates]}


def _draw_then_select(call) -> dict:
    """segment 画一个多边形，annotate 选中全部候选。"""
    return {"regions": [_DRAWN_REGION]} if call.kind == "segment" else _select_all(call)


def _conserved(counts: dict) -> bool:
    terminal = (counts["emitted"] + counts["dropped_dup"] + counts["dropped_lowq"] + counts["dropped_verify"]
                + counts["failed"] + counts["bad_input"])
    return terminal == counts["scanned"] + counts["generated"]


# ── 报表（§16.3，AC-141） ───────────────────────────────────────────────────


def test_report_segment_image_block_shape(workspace):
    """五个样本走遍成功、空候选、修复穷尽、钩子拒收与钩子缺陷，每个键都有独立的值。"""
    for index, companion in enumerate(("boxes", "empty", "empty", "reject", "boom"), start=1):
        workspace.sample(index, companion)
    workspace.project(_MODEL_WITH_HOOK, sections="[dedup]\nenabled = true\nsemantic = false\n")

    def respond(call):
        if call.kind == "annotate":
            return _select_all(call)
        name = call.image_name
        if name == "image_1.png":
            return {"regions": [_REUSED_REGION, _DRAWN_REGION]}
        return {"regions": []} if name == "image_2.png" else {"regions": [_UNCLOSED_REGION]}

    scripted = workspace.run(respond)
    assert len(scripted.of("segment")) == 5     # image_3 首轮加两轮修复
    report = workspace.report
    keys = list(report)
    assert keys[:3] == ["run", "counts", "segment_image"]
    assert keys.index("segment_image") < keys.index("dedup") < keys.index("schema_engine")
    block = report["segment_image"]
    assert list(block) == ["calls", "samples", "boxes", "regions", "empty", "failed", "usage"]
    assert block == {
        "calls": 3, "samples": 5, "boxes": 2, "regions": 2, "empty": 1,
        "failed": {"companion_invalid": 1, "internal_error": 1, "segmentation_invalid": 1},
        "usage": {"prompt_tokens": 50, "completion_tokens": 25},
    }
    assert list(block["failed"]) == sorted(block["failed"])
    assert list(block["usage"]) == ["prompt_tokens", "completion_tokens"]
    assert report["counts"]["failed"] == 3
    assert report["counts"]["emitted"] == 2
    assert _conserved(report["counts"])


def test_report_failed_is_empty_object_without_failures(workspace):
    workspace.sample(1)
    workspace.project(_MODEL)
    workspace.run(_draw_then_select)
    assert workspace.report["segment_image"]["failed"] == {}


def test_boxes_only_report_has_zero_calls_and_usage(workspace):
    """D15：annotate 照常调用，但 segment 的调用数与用量恒为 0；样本与候选照常计数。"""
    workspace.sample(1, "boxes")
    workspace.sample(2, "empty")
    workspace.project(_BOXES_ONLY)
    scripted = workspace.run(_select_all)
    assert len(scripted.of("annotate")) == 2 and scripted.of("segment") == []
    assert workspace.report["segment_image"] == {
        "calls": 0, "samples": 2, "boxes": 2, "regions": 2, "empty": 1, "failed": {},
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
    }


def test_usage_separates_segment_from_annotate_on_shared_profile(workspace):
    """segment 与 annotate 共用同一 profile：块内用量只含分割调用，不含标注。"""
    workspace.sample(1)
    workspace.sample(2)
    workspace.project(_MODEL)
    scripted = workspace.run(_draw_then_select,
                             usage=lambda call: Usage(7, 3) if call.kind == "segment" else Usage(1000, 500))
    assert {call.profile for call in scripted.calls} == {"default"}
    assert len(scripted.of("annotate")) == 2
    assert workspace.report["segment_image"]["usage"] == {"prompt_tokens": 14, "completion_tokens": 6}


def test_report_counts_companion_invalid(workspace):
    """钩子拒收：单独计数，不算 bad_input，进 rejects，不调用模型。"""
    workspace.sample(1, "reject")
    workspace.sample(2, "boxes")
    workspace.project(_BOXES_ONLY)
    scripted = workspace.run(_select_all)
    assert [call.image_name for call in scripted.calls] == ["image_2.png"]
    report = workspace.report
    assert report["segment_image"]["failed"] == {"companion_invalid": 1}
    assert report["counts"]["bad_input"] == 0
    assert report["counts"]["failed"] == 1
    reject, = workspace.rejects
    assert (reject["_meta"]["stage"], reject["_meta"]["reason"]) == ("segment", "companion_invalid")
    assert reject["_meta"]["errors"] == ["box_parser rejected companion"]


def test_ignored_files_counted_and_conservation_holds(workspace):
    """杂项文件只进 ignored_files；缺对记 bad_input；scanned 不受杂项文件影响，守恒式成立。"""
    workspace.sample(1, "boxes")
    workspace.sample(2, "reject")
    workspace.sample(3)                         # D15 下缺伴随文件是缺对
    (workspace.data / "notes.txt").write_text("stray", encoding="utf-8")
    (workspace.data / "image_1.txt").write_text("stray", encoding="utf-8")
    (workspace.data / "sub").mkdir()
    (workspace.data / "sub" / "readme.md").write_text("stray", encoding="utf-8")
    workspace.project(_BOXES_ONLY)
    workspace.run(_select_all)
    counts = workspace.report["counts"]
    assert counts["ignored_files"] == 3
    assert counts["scanned"] == 3
    assert counts["ingested"] == 2
    assert counts["bad_input"] == 1
    assert counts["emitted"] == counts["failed"] == 1
    assert _conserved(counts)


def test_ignored_files_key_only_in_image_mode(workspace):
    """图片模式恒有该键，没有杂项文件时为 0；segment 关闭的工程没有这个键。"""
    workspace.sample(1)
    workspace.project(_MODEL)
    workspace.run(_draw_then_select)
    counts = workspace.report["counts"]
    assert list(counts)[-1] == "ignored_files" and counts["ignored_files"] == 0

    workflow = SimpleNamespace(cfg=make_cfg(workspace.root), _circuit_broken=False, _interrupted=False)
    plain = ProcessWorkflow._report_counts(workflow, process_workflow._CounterView({}),
                                           SimpleNamespace(scanned=1, ingested=1, bad_input=0, ignored_files=9))
    assert "ignored_files" not in plain


def test_ignored_files_sits_after_fanout_and_before_unprocessed(workspace):
    """冻结键位：fanout 之后、unprocessed 之前。"""
    cfg = make_cfg(workspace.root, modality="ui", segment=SegmentConfig(enabled=True, mode="image", instruction="分割"),
                   classify=ClassifyConfig(enabled=True, assignment="multi"))
    workflow = SimpleNamespace(cfg=cfg, _circuit_broken=True, _interrupted=False,
                               _unprocessed_residual=lambda counts: 0)
    counts = ProcessWorkflow._report_counts(workflow, process_workflow._CounterView({}),
                                            SimpleNamespace(scanned=1, ingested=1, bad_input=0, ignored_files=9))
    assert counts["ignored_files"] == 9
    assert list(counts)[-3:] == ["fanout", "ignored_files", "unprocessed"]


def test_examples_ui_report_has_no_new_keys(tmp_path, monkeypatch):
    """非图片工程真实跑一遍：没有新块、没有新计数键、_meta 没有 regions，计数不变。"""

    def from_schema(schema: dict):
        kind = schema.get("type")
        if "enum" in schema:
            return schema["enum"][0]
        if kind == "object":
            return {key: from_schema(schema["properties"][key]) for key in schema.get("required", ())}
        if kind == "array":
            return [from_schema(schema["items"]) for _ in range(schema.get("minItems", 0))]
        if kind in ("integer", "number"):
            return schema.get("minimum", 0)
        return False if kind == "boolean" else "x"

    shutil.copytree(_ROOT / "examples" / "ui", tmp_path / "ui", ignore=shutil.ignore_patterns("out", "__pycache__"))
    shutil.copy(_ROOT / "examples" / "config.toml", tmp_path / "config.toml")
    (tmp_path / "ui" / "out").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path / "ui")
    monkeypatch.setenv("LABELKIT_ZAI_KEY", "offline-test-key")
    monkeypatch.setattr(application, "setup_logging", lambda cfg: None)
    scripted = Scripted(lambda call: from_schema(call.schema))
    monkeypatch.setattr(application, "LLMClient", scripted.client_type())
    output = tmp_path / "ui.jsonl"
    code = application.execute_run(Path("../config.toml"), Path("project.toml"),
                                   CliOverrides(console="plain", output=str(output)))
    assert code == 0
    report = json.loads((tmp_path / "ui.report.json").read_text(encoding="utf-8"))
    assert "segment_image" not in report
    assert list(report["counts"]) == ["scanned", "ingested", "bad_input", "dropped_dup", "dropped_lowq",
                                      "dropped_verify", "failed", "generated", "emitted"]
    counts = report["counts"]
    assert (counts["scanned"], counts["ingested"], counts["bad_input"]) == (6, 5, 1)
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert rows and all("regions" not in row["_meta"] for row in rows)
    assert not [image for call in scripted.calls for image in call.images
                if isinstance(image.source, RegionOverlayRef)]


# ── trace（§16.4，AC-142） ──────────────────────────────────────────────────


def test_segment_regions_event_payload(workspace):
    """每条成功记录一个事件：键序冻结、只有计数、regions = reused + drawn，失败记录没有事件。"""
    workspace.sample(1, "boxes")
    workspace.sample(2, "empty")
    workspace.sample(3, "reject")
    workspace.project(_MODEL_WITH_HOOK, sections=_TRACE.format(content="full"))
    seen: list[str] = []

    def respond(call):
        if call.kind == "annotate":
            return _select_all(call)
        seen.append(call.image_name)
        if call.image_name == "image_2.png":
            return {"regions": []}
        if seen.count("image_1.png") == 1:          # 首轮违规，修复后才成功：attempts = 2
            return {"regions": [_UNCLOSED_REGION]}
        return {"regions": [_REUSED_REGION, _DRAWN_REGION, _DRAWN_REGION]}

    workspace.run(respond)
    events = {event["record_ids"][0]: event for event in workspace.events("segment.regions")}
    rows = workspace.rows
    assert len(events) == 2 and set(events) == {row["_meta"]["id"] for row in rows.values()}

    first, second = events[rows[1]["_meta"]["id"]], events[rows[2]["_meta"]["id"]]
    assert first["stage"] == "segment"
    assert list(first["payload"]) == ["regions", "reused", "drawn", "width", "height", "attempts"]
    assert first["payload"] == {"regions": 3, "reused": 1, "drawn": 2, "width": 100, "height": 50, "attempts": 2}
    assert second["payload"] == {"regions": 0, "reused": 0, "drawn": 0, "width": 100, "height": 50, "attempts": 1}
    for event in events.values():
        payload = event["payload"]
        assert payload["regions"] == payload["reused"] + payload["drawn"]
        assert all(type(value) is int for value in payload.values())
    text = json.dumps(list(events.values()), ensure_ascii=False)
    assert "n1" not in text and "coordinates" not in text and "geometry" not in text


def test_segment_regions_event_attempts_zero_on_boxes_only(workspace):
    workspace.sample(1, "boxes")
    workspace.project(_BOXES_ONLY, sections=_TRACE.format(content="none"))
    workspace.run(_select_all)
    event, = workspace.events("segment.regions")
    assert event["payload"] == {"regions": 2, "reused": 2, "drawn": 0, "width": 100, "height": 50, "attempts": 0}


@pytest.mark.parametrize("content", ["none", "refs", "excerpt", "full"])
def test_segment_regions_event_kept_on_all_tiers(workspace, content):
    """全部键都是计数，任何档位都不脱敏；也不镜像到 stderr。"""
    workspace.sample(1)
    workspace.project(_MODEL, sections=_TRACE.format(content=content))
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("labelkit")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        workspace.run(_draw_then_select)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    event, = workspace.events("segment.regions")
    assert event["payload"] == {"regions": 1, "reused": 0, "drawn": 1, "width": 100, "height": 50, "attempts": 1}
    assert not [record for record in records if "segment.regions" in record.getMessage()]


# ── 启动日志、流语义门与叠图不落盘（AC-14、AC-143、AC-144） ──────────────────


def _budget_log_lines(cfg) -> list[str]:
    lines: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: lines.append(record.getMessage())
    previous = process_workflow._log.level
    process_workflow._log.addHandler(handler)
    process_workflow._log.setLevel(logging.INFO)
    try:
        ProcessWorkflow._log_budget_startup(SimpleNamespace(cfg=cfg, _budget_profiles=lambda: [("default", 1000, 10)]))
    finally:
        process_workflow._log.removeHandler(handler)
        process_workflow._log.setLevel(previous)
    return lines


def test_budget_startup_log_has_no_minimum_frames(tmp_path):
    profile = LLMProfile(name="default", provider="anthropic", base_url="http://localhost", model="m",
                         api_key_env="K", context_window=1000, supports_vision=True)
    image = make_cfg(tmp_path, annotate=True, modality="ui",
                     segment=SegmentConfig(enabled=True, mode="image", instruction="分割"))
    sequence = make_cfg(tmp_path, annotate=True, modality="ui",
                        segment=SegmentConfig(enabled=True, mode="sequence", sequence_resolved=True))
    image_lines = _budget_log_lines(dataclasses.replace(image, llm_profiles={"default": profile}))
    assert image_lines == ["budget: default=1000/10"]
    sequence_lines = _budget_log_lines(dataclasses.replace(sequence, llm_profiles={"default": profile}))
    assert any("minimum_frames" in line for line in sequence_lines)


def test_stream_gates_not_triggered_in_image_mode(workspace):
    """流语义门全部不触发：没有流块、流计数、会话事件，_meta.stream 为 null。"""
    workspace.sample(1)
    workspace.sample(2)
    workspace.project(_MODEL, sections=_TRACE.format(content="none"))
    workspace.run(_draw_then_select)
    report = workspace.report
    assert "stream" not in report
    assert "minimum_frames" not in report.get("budget", {})
    assert not {"episodes", "absorbed", "dropped_noise", "stitched", "threads", "unprocessed"} & set(report["counts"])
    assert workspace.events("segment.session") == [] and workspace.events("segment.boundary") == []
    for row in workspace.rows.values():
        assert row["_meta"]["stream"] is None
        assert isinstance(row["_meta"]["regions"], dict)


def test_run_writes_no_overlay_files(workspace):
    """叠图只存在于请求生命周期：运行前后只多出既有通道文件，没有任何新图片。"""
    workspace.sample(1)
    workspace.sample(2)
    workspace.project(_MODEL, sections=_TRACE.format(content="full"))
    before = {path for path in workspace.root.rglob("*") if path.is_file() and "__pycache__" not in path.parts}
    scripted = workspace.run(_draw_then_select)
    assert all(isinstance(call.images[1].source, RegionOverlayRef) for call in scripted.of("annotate"))
    after = {path for path in workspace.root.rglob("*") if path.is_file() and "__pycache__" not in path.parts}
    created = sorted(path.relative_to(workspace.root).as_posix() for path in after - before)
    assert created == ["out/run.jsonl", "out/run.rejects.jsonl", "out/run.report.json", "out/run.trace.jsonl"]


def test_segment_disabled_has_no_candidates_or_overlay(workspace):
    """segment 关闭：阶段不入链，普通截图加控件树只发一张图，输出与报表都没有候选。"""
    shutil.copy(_ROOT / "examples" / "ui" / "data" / "image_1.png", workspace.data)
    shutil.copy(_ROOT / "examples" / "ui" / "data" / "uitree_1.jsonl", workspace.data)
    schema = {"type": "object", "additionalProperties": False, "required": ["label"],
              "properties": {"label": {"type": "string"}}}
    (workspace.root / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    (workspace.root / "project.toml").write_text(
        'schema_version = 1\n\n[run]\ninput = "./data"\noutput = "./out/run.jsonl"\nmodality = "ui"\n\n'
        '[dedup]\nenabled = false\n\n[quality]\nenabled = false\n\n[verify]\nenabled = false\n\n'
        '[annotate]\nenabled = true\nllm = "default"\ninstruction = "标注"\n\n'
        '[output]\nschema_path = "schema.json"\nmeta_mode = "inline"\n', encoding="utf-8")
    scripted = workspace.run(lambda call: {"label": "x"})
    call, = scripted.calls
    assert [type(image.source) for image in call.images] == [ImageRef]
    assert "regions" not in call.schema["properties"]
    assert all("[候选区域]" not in text for text in call.texts)
    row, = workspace.rows.values()
    assert "regions" not in row["_meta"]
    assert "segment_image" not in workspace.report
    assert "ignored_files" not in workspace.report["counts"]


# ── 端到端行为（AC-05、AC-10、AC-18、AC-44、AC-72） ─────────────────────────


def test_segment_only_run_emits_payload_and_regions(workspace):
    """只跑分割（D9）：主输出是图片模式载荷加 _meta.regions，没有标注与 quality 相关项。"""
    workspace.sample(1, "boxes")
    workspace.sample(2)
    workspace.project(_MODEL_WITH_HOOK, annotate="[annotate]\nenabled = false\n")
    scripted = workspace.run(lambda call: {"regions": [_REUSED_REGION] if call.image_name == "image_1.png"
                                           else [_DRAWN_REGION]})
    assert [call.kind for call in scripted.calls] == ["segment", "segment"]
    first, second = workspace.rows[1], workspace.rows[2]
    assert list(first) == ["image_path", "companion_path", "_meta"]
    assert Path(first["companion_path"]).name == "image_1.cmp"
    assert list(second) == ["image_path", "_meta"]
    assert Path(second["image_path"]).name == "image_2.png"
    assert first["_meta"]["regions"]["items"] == [{
        "region_id": "n1", "box_id": "n1", "source_box_ids": ["n1"],
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]}}]
    assert second["_meta"]["regions"]["items"][0]["region_id"] == "r1"
    assert second["_meta"]["regions"]["boxes"] == []
    report = workspace.report
    assert "quality" not in report and "annotate" not in report
    assert report["counts"]["emitted"] == 2
    assert report["segment_image"]["calls"] == 2


def test_empty_candidates_not_rejected_and_no_fullimage_box(workspace):
    """空候选是合法结果：不进 rejects、不补整图框，annotate 看到空数组。"""
    workspace.sample(1)
    workspace.project(_MODEL)

    def respond(call):
        if call.kind == "segment":
            return {"regions": []}
        assert call.candidates == []
        return {"items": []}

    scripted = workspace.run(respond)
    assert len(scripted.of("annotate")) == 1
    row = workspace.rows[1]
    assert row["_meta"]["regions"] == {"image": {"width": 100, "height": 50}, "boxes": [], "items": []}
    assert row["items"] == []
    assert workspace.rejects == []
    report = workspace.report
    assert report["counts"]["emitted"] == 1 and report["counts"]["failed"] == 0
    assert report["segment_image"]["empty"] == 1 and report["segment_image"]["regions"] == 0


def test_boxes_only_annotate_non_default_profile_runs(workspace):
    """D15 + annotate 用非 default profile：运行时没有 segment.llm 的资源键也不会失败。"""
    workspace.sample(1, "boxes")
    workspace.project(_BOXES_ONLY, annotate=_ANNOTATE.replace('llm = "default"', 'llm = "other"'))
    scripted = workspace.run(_select_all)
    assert [call.profile for call in scripted.calls] == ["other"]
    assert workspace.report["counts"]["emitted"] == 1
    assert [item["region_id"] for item in workspace.rows[1]["_meta"]["regions"]["items"]] == ["n1", "n2"]


@pytest.mark.parametrize("route", ["model", "boxes_only"])
def test_autonumbered_names_are_strings_everywhere(workspace, monkeypatch, route):
    """补名 "1"、"2" 在提示词、上下文、输出与 CallScope 里都是字符串；未成为候选的框名可修复。"""
    workspace.sample(1, "unnamed")
    workspace.project(_MODEL_WITH_HOOK if route == "model" else _BOXES_ONLY)
    scopes = []
    original = SchemaEngine.complete_finalized

    async def spy(self, request):
        scopes.append(request.scope)
        return await original(self, request)

    monkeypatch.setattr(SchemaEngine, "complete_finalized", spy)

    def respond(call):
        if call.kind == "segment":
            return {"regions": [{"box_id": "1", "polygons": None, "source_box_ids": None}]}
        if route == "model" and not call.is_repair:
            return {"items": [{"region_id": "2", "label": "没有成为候选的框"}]}
        return {"items": [{"region_id": "1", "label": "x"}]}

    scripted = workspace.run(respond)
    annotate = scripted.of("annotate")
    row = workspace.rows[1]
    regions = row["_meta"]["regions"]
    assert [box["id"] for box in regions["boxes"]] == ["1", "2"]
    assert row["items"][0]["context_box_ids"] == ["1", "2"]
    assert row["items"][0]["region_id"] == "1"
    if route == "model":
        assert json.loads(scripted.of("segment")[0].block("[参考框]"))["box_id"] == "1"
        assert annotate[0].block("[参考框]") is not None
        assert [item["region_id"] for item in regions["items"]] == ["1"]
        assert scopes[0].region_ids == frozenset({"1"})
        assert annotate[1].is_repair
        violations = [line for text in annotate[1].texts for line in text.split("\n") if UNKNOWN_REFERENCE in line]
        assert violations == ["1. /items/0/region_id: unknown region reference"]
    else:
        assert [item["region_id"] for item in regions["items"]] == ["1", "2"]
        assert [item["box_id"] for item in regions["items"]] == ["1", "2"]
        assert scopes[0].region_ids == frozenset({"1", "2"})
    assert all(type(value) is str for value in scopes[0].region_ids)


def test_box_parser_called_once_per_sample_across_pipeline(workspace):
    """多标签扇出、L3 修复与 verify 修复都不再调用钩子，所有信封共用同一候选。"""
    workspace.sample(1, "boxes")
    workspace.sample(2, "empty")
    classify = ('[classify]\nenabled = true\nllm = "default"\nassignment = "multi"\nfallback_class = "other"\n\n'
                '[[classify.classes]]\nname = "account"\ndescription = "账号"\n\n'
                '[[classify.classes]]\nname = "settings"\ndescription = "设置"\n\n'
                '[[classify.classes]]\nname = "other"\ndescription = "其他"\n')
    verify = '[verify]\nenabled = true\nllm = "default"\npolicy = "repair"\nmax_repair_rounds = 1\n'
    workspace.project(_MODEL_WITH_HOOK, sections=f"{classify}\n{verify}")
    judged: dict[str, int] = {}
    annotated: dict[str, int] = {}

    def respond(call):
        keys = set(call.schema["properties"])
        if "regions" in keys:
            return {"regions": [_DRAWN_REGION]}
        if "items" in keys:
            key = call.image_name
            annotated[key] = annotated.get(key, 0) + 1
            if annotated[key] == 1:
                return {"items": [{"region_id": "ghost", "label": "x"}]}
            return {"items": [{"region_id": "r1", "label": "x"}]}
        if "verdict" in keys:
            judged[call.image_name] = judged.get(call.image_name, 0) + 1
            verdict = "fail" if judged[call.image_name] == 1 else "pass"
            return {"verdict": verdict, "critiques": [{"aspect": "选择", "opinion": "看过了"}]}
        return {"classes": ["account", "settings"]}

    scripted = workspace.run(respond)
    assert len(scripted.of("segment")) == 2
    assert any(call.is_repair for call in scripted.of("annotate"))
    assert judged and max(judged.values()) >= 2
    assert sorted(workspace.hook_calls) == ["boxes", "empty"]
    assert workspace.report["counts"]["fanout"] == 2


# ── 真实端点门的确定性部分：打分、夹具与消融处理（门本身在 tests/integration，这里不发请求） ──


def _square(left: float, top: float, size: float) -> Polygon:
    return Polygon([(left, top), (left + size, top), (left + size, top + size), (left, top + size)])


def test_gate_iou_matching_is_greedy_one_to_one_at_spec_threshold():
    assert gates.IOU_THRESHOLD == 0.5
    reference = _square(0, 0, 10)
    near, far = _square(0, 0, 10), _square(1, 0, 10)
    # 两个候选都够得着同一个参考：只有 IoU 更高的那个命中，另一个算多余。
    assert gates.match_regions([far, near], [reference]) == [(1, 0, 1.0)]
    # 恰好 0.5 命中（并集 200、交集 100 的等价形状），略低于 0.5 不命中。
    half = Polygon([(0, 0), (20, 0), (20, 10), (0, 10)])
    assert gates.match_regions([half], [reference]) == [(0, 0, 0.5)]
    below = Polygon([(0, 0), (21, 0), (21, 10), (0, 10)])
    assert gates.match_regions([below], [reference]) == []
    # 高 IoU 配对先占用参考，后面的候选不能再匹配它，但可以匹配别的参考。
    other = _square(100, 100, 10)
    assert gates.match_regions([near, far, _square(100, 100, 10)], [reference, other]) == [(0, 0, 1.0), (2, 1, 1.0)]


def test_gate_score_sample_reports_recall_iou_and_extra():
    def item(region_id: str, polygon: Polygon) -> dict:
        return {"region_id": region_id, "geometry": mapping(polygon)}

    references = [{"label": "a", "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]},
                  {"label": "b", "polygon": [[50, 50], [60, 50], [60, 60], [50, 60]]}]
    score = gates.score_sample([item("r1", _square(0, 0, 10)), item("r2", _square(200, 200, 5))], references)
    assert (score["references"], score["candidates"], score["matched"], score["extra"]) == (2, 2, 1, 1)
    assert score["matched_labels"] == ["a"]
    assert score["matched_region_ids"] == {"r1": "a"}
    assert score["mean_iou"] == 1.0
    assert gates.recall_of([score]) == 0.5
    assert gates.score_sample([], references)["mean_iou"] is None


def test_gate_score_selection_separates_selection_label_and_failure():
    regions = RegionSet(10, 10, (), ())
    record = _image_record_stub()
    good = PipelineItem(record=record, regions=regions, annotation=Annotation(
        output={"items": [{"region_id": "r1", "label": " Blue "}, {"region_id": "r2", "label": "wrong"}]},
        model="m", attempts=1, usage=Usage()))
    score = gates.score_selection(good, {"r1": "blue", "r2": "red"})
    assert (score["parsed"], score["exact"], score["label_correct"], score["misread"]) == (True, True, 1, 1)
    extra = gates.score_selection(good, {"r1": "blue"})
    assert (extra["exact"], extra["label_correct"], extra["misread"]) == (False, 1, 0)
    failed = PipelineItem(record=record, regions=regions, status="failed")
    failed.errors.append(StageError(stage="annotate", kind="schema_violation", message="m", retryable=False))
    missing = gates.score_selection(failed, {"r1": "blue"})
    assert (missing["parsed"], missing["exact"], missing["errors"]) == (False, False, ["schema_violation"])


def _image_record_stub() -> Record:
    return Record(id="a" * 16, modality="ui", text=None, raw=None, ui_tree=None,
                  image=ImageRef(Path("image_1.png"), "png", 1), ref=RecordRef("image_1.png", None, 1, ()))


def test_gate_fixtures_are_well_formed():
    """人工标注都是画布内的合法多边形，夹具引用的文件都在，固定候选能过真实几何校验。"""
    fixtures = gates.load_fixtures()
    assert {group["name"] for group in fixtures["quality_groups"]} == {
        "pure_image", "hook_returns_empty", "hook_returns_shell", "aligned_reference_boxes"}
    for group in fixtures["quality_groups"]:
        for sample in group["samples"]:
            assert group["hook"] is (sample["companion"] is not None)
            with Image.open(_ROOT / sample["image"]) as picture:
                canvas = box(0, 0, *picture.size)
            assert sample["references"]
            for reference in sample["references"]:
                polygon = Polygon(reference["polygon"])
                assert polygon.is_valid and polygon.area > 0
                assert canvas.contains(polygon), reference["label"]
            if group["hook"] and "path" in sample["companion"]:
                assert (_ROOT / sample["companion"]["path"]).is_file()
    for key in ("annotate_cases", "ablation_cases"):
        for case in fixtures[key]:
            regions = gates.fixed_regions(_ROOT / case["image"], case["candidates"])
            known = {region.region_id for region in regions.regions}
            assert len(known) == len(case["candidates"])
            assert len(case["prompts"]) >= 2
            for prompt in case["prompts"]:
                assert prompt["instruction"] and prompt["expected"]
                assert set(prompt["expected"]) <= known
    rework = fixtures["rework_case"]
    assert set(rework["expected"]) == {item["region_id"] for item in rework["wrong_output"]["items"]}
    assert all(rework["expected"][item["region_id"]] != item["label"] for item in rework["wrong_output"]["items"])


def test_gate_group_workspace_routes_companions_through_hooks(tmp_path):
    """工程目录不带内容线索：图片按序号命名；路由钩子把字面量与示例格式分别转换。"""
    group = next(group for group in gates.load_fixtures()["quality_groups"] if group["name"] == "aligned_reference_boxes")
    root = gates.group_workspace(tmp_path, "aligned", group["samples"])
    assert sorted(path.name for path in (root / "data").iterdir()) == [
        "image_1.cmp", "image_1.png", "image_2.cmp", "image_2.png"]
    spec = importlib.util.spec_from_file_location("gate_hooks_under_test", root / "hooks.py")
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)
    image = {"width": 540, "height": 960}
    assert [item["id"] for item in hooks.route_boxes((root / "data" / "image_1.cmp").read_bytes(), image)] == ["n1", "n2", "n3"]
    assert [item["id"] for item in hooks.route_boxes((root / "data" / "image_2.cmp").read_bytes(), image)] == ["a1", "a2"]
    assert hooks.route_boxes(b"empty", image) == []
    assert hooks.route_boxes(b"shell", image) == [{"id": "shell", "bbox": [0, 0, 540, 960], "text": "window"}]


def test_gate_remove_overlay_matches_the_real_prompt_and_refuses_silence():
    """消融门去掉的正是真实提示词里的叠图、标签与说明句；什么都没去掉时报错，不会静默变成对照组。"""
    prompt = annotate_prompt_for(annotate_make_cfg())
    assert any(part.kind == "image" and isinstance(part.image, RegionOverlayRef)
               for message in prompt.messages for part in message.parts)
    ablated = gates.remove_overlay(prompt)
    parts = [part for message in ablated.messages for part in message.parts]
    assert [type(part.image) for part in parts if part.kind == "image"] == [ImageRef]
    texts = [part.text for part in parts if part.kind == "text"]
    assert gates.OVERLAY_LABEL not in texts
    assert not any("叠图" in text for text in texts)
    assert any("[原图]" in text for text in texts) and any("[候选区域]" in text for text in texts)
    with pytest.raises(AssertionError):
        gates.remove_overlay(ablated)

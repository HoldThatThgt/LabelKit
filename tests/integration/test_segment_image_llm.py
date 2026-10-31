"""segment 图片模式的 z.ai glm-5.2 真实端点五道门（SPEC-image-region-segmentation §20.1）。

| 门标记 | 测试节点 |
|---|---|
| ``zai-pixel-perception`` | ``test_zai_pixel_perception`` |
| ``segment-image-zai`` | ``test_zai_segment_image_end_to_end``、``test_zai_reference_box_inputs``、``test_zai_boxes_only_path``、``test_zai_scaled_request_and_verify_repair`` |
| ``segment-image-overlay-ablation`` | ``test_zai_overlay_ablation`` |
| ``segment-image-quality`` | ``test_zai_segmentation_quality`` |
| ``segment-image-annotate-quality`` | ``test_zai_annotate_quality`` |

没有替身：请求经真实 ``LLMClient`` 发往真实端点。``SpyClient`` 只记录请求形态、用量与耗时后转交父类；消融门另在
转交前去掉叠图，这正是该门要比较的变量。每个节点把传输、解析、语义正确、重复运行稳定分别写进
``out/verification-segment-image/<marker>--<node>.json``（``out/`` 不入库），断言在写完之后才执行，失败的运行同样留下记录。
``test_zai_segment_image_end_to_end`` 与 ``test_zai_scaled_request_and_verify_repair`` 的断言逐条求值，结果写在所属维度的
``checks`` 下，节点最后一次判定，前一条失败不会挡住后一条。
结果抄进 ``docs/dev/E2E-FINDINGS.md`` 之前，对应门保持 ``[PENDING-EVIDENCE:<marker>]``。

运行方式（需要 ``LABELKIT_ZAI_KEY``）::

    uv run pytest -m "integration and zai" tests/integration/test_segment_image_llm.py -s
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import io
import json
import os
import random
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from shapely.geometry import Polygon, shape

from labelkit.common.config.loader import load
from labelkit.common.config.model import CliOverrides, LLMProfile, OutputConfig, ResolvedConfig
from labelkit.common.contracts.regions import RegionOverlayRef, RegionSet
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import Annotation, ImageRef, PipelineItem, Record, RecordRef, Usage
from labelkit.common.errors import OutputTruncatedError, ProviderFatalError, ProviderRetryableError, SchemaViolation
from labelkit.common.inference.credentials import RuntimeCredentials, resolve_credentials
from labelkit.common.inference.llm_client import LLMClient, Message, Part, PromptBundle
from labelkit.common.inference.schema_engine import CallScope, SchemaEngine
from labelkit.common.observability.obslog import EventLog, MetricsSink
from labelkit.operators.annotate import AnnotateStage
from labelkit.operators.segment_geometry import validate_regions
from labelkit.operators.verify import VerifyStage
from labelkit.orchestration import application
from labelkit.runtime.scheduler import ExecutionRuntime

from tests.conftest import ZAI_BASE_URL, ZAI_KEY_ENV, ZAI_MODEL
from tests.llm_client_helpers import make_llm_client

pytestmark = [pytest.mark.integration, pytest.mark.zai]

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "image-segmentation"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "segment_image_gates.json"
EVIDENCE_DIR = ROOT / "out" / "verification-segment-image"

IOU_THRESHOLD = 0.5                 # SPEC §20.1 冻结：一对一贪心匹配的命中阈值
REPEATS = 2                         # 重复运行次数，稳定性单独记录
PERCEPTION_TRIALS = 6
PERCEPTION_FLOOR = 5 / 6            # 随机猜中位置与颜色的概率只有 1/36
RECALL_FLOOR = 0.5                  # 语义门槛：低于它就不能说模型遵守了网格约定
SELECTION_FLOOR = 0.6
LABEL_FLOOR = 0.6
REUSE_FLOOR = 2 / 3
SCALED_SIZE = (2048, 1475)          # 示例 fixed 样本发给模型的请求尺寸

OVERLAY_LABEL = "[候选区域叠图]"
OVERLAY_SENTENCE = "原图与候选区域叠图来自同一样本；叠图中每个区域的标识就是该候选的 region_id；"
ROUTE_HOOK = '''

def route_boxes(companion, image):
    """门用的钩子：字面量给出空数组或外壳框，其余数据交给示例钩子转换。"""
    if companion == b"empty":
        return []
    if companion == b"shell":
        return [{"id": "shell", "bbox": [0, 0, image["width"], image["height"]], "text": "window"}]
    if companion.lstrip().startswith(b"<"):
        return uiautomator_to_boxes(companion, image)
    return accessibility_to_boxes(companion, image)
'''
SELECTION_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["region_id", "label"],
        "properties": {"region_id": {"type": "string", "x-labelkit-region-ref": True},
                       "label": {"type": "string"}}}}},
}
PERCEPTION_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["row", "column", "color", "dots"],
    "properties": {"row": {"type": "integer", "minimum": 0, "maximum": 3},
                   "column": {"type": "integer", "minimum": 0, "maximum": 3},
                   "color": {"type": "string", "enum": ["red", "green", "blue", "yellow", "none"]},
                   "dots": {"type": "integer", "minimum": 0, "maximum": 9}},
}
PERCEPTION_QUESTION = ("图片是白底 3×3 棋盘。行、列都从 1 开始数。如果有一个格子被涂满颜色，给出它的行号、列号和颜色；"
                       "另一个格子里可能有黑色圆点，dots 写圆点个数。没有涂色格子时 row 与 column 写 0，color 写 none；"
                       "没有圆点时 dots 写 0。")
PERCEPTION_COLORS = {"red": (220, 40, 40), "green": (40, 170, 70), "blue": (40, 80, 220), "yellow": (240, 210, 40)}


def load_fixtures() -> dict:
    """@return 人工标注夹具。"""
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


# ── 证据记录 ─────────────────────────────────────────────────────────────────


@dataclass
class Evidence:
    """一个节点的证据：四个维度分开记录，不用最终 JSON 通过 Schema 代替语义正确。"""

    marker: str
    node: str
    transport: dict = field(default_factory=dict)
    parse: dict = field(default_factory=dict)
    semantic: dict = field(default_factory=dict)
    stability: dict = field(default_factory=dict)

    def check(self, dimension: str, name: str, passed: object) -> None:
        """记下一条断言的结果，归入它所属的维度；这里不抛出，前一条失败不会挡住后一条。

        @param dimension transport、parse、semantic 或 stability
        @param name 断言名，同一维度内唯一
        @param passed 断言表达式的值，按真假记录
        """
        getattr(self, dimension).setdefault("checks", {})[name] = bool(passed)

    def failed(self) -> list[str]:
        """@return 全部没有通过的断言，形如 ``semantic.run0.recall``。"""
        return [f"{dimension}.{name}" for dimension in ("transport", "parse", "semantic", "stability")
                for name, passed in getattr(self, dimension).get("checks", {}).items() if not passed]

    def write(self) -> Path:
        """写出证据文件并打印一行摘要。

        @return 证据文件路径
        """
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        path = EVIDENCE_DIR / f"{self.marker}--{self.node}.json"
        body = {"marker": self.marker, "node": self.node, "model": ZAI_MODEL,
                "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "transport": self.transport, "parse": self.parse, "semantic": self.semantic,
                "stability": self.stability}
        text = json.dumps(body, ensure_ascii=False, indent=2)
        path.write_bytes((text + "\n").encode("utf-8"))
        print(f"\n[{self.marker}] {self.node}\n{text}")
        return path


# ── 请求记录（真实 LLMClient 的薄子类） ──────────────────────────────────────


@dataclass
class SpyCall:
    """一次真实请求的形态与账目；不保存图片字节。"""

    profile: str
    kind: str
    images: list[object]
    texts: list[str]
    sizes: list[tuple[int, int]] = field(default_factory=list)
    error: str | None = None
    finish: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0

    def block(self, label: str) -> str | None:
        """取 ``label`` 标签下一行的内容；块不存在时为 None。"""
        for text in self.texts:
            lines = text.split("\n")
            if label in lines:
                return lines[lines.index(label) + 1]
        return None

    @property
    def image_types(self) -> list[str]:
        return [type(image).__name__ for image in self.images]


def schema_kind(schema: dict | None) -> str:
    """按响应 Schema 的键判断请求属于哪个算子。"""
    keys = set((schema or {}).get("properties", ()))
    if "regions" in keys:
        return "segment"
    if "items" in keys:
        return "annotate"
    return "verify" if "verdict" in keys else "other"


class Spy:
    """记录经过 ``LLMClient.complete`` 的真实请求；transform 只用于消融门。"""

    def __init__(self, transform=None, measure_images: bool = False):
        self.transform = transform
        self.measure_images = measure_images
        self.calls: list[SpyCall] = []

    def client_type(self) -> type:
        spy = self

        class SpyClient(LLMClient):
            async def complete(self, profile, prompt, response_schema=None):
                sent = spy.transform(prompt) if spy.transform is not None else prompt
                call = spy.record(profile, sent, response_schema, self._llm_profiles[profile])
                started = time.perf_counter()
                try:
                    response = await super().complete(profile, sent, response_schema)
                except Exception as exc:
                    call.error = type(exc).__name__
                    call.latency_s = time.perf_counter() - started
                    raise
                call.latency_s = time.perf_counter() - started
                call.finish = response.finish
                call.prompt_tokens = response.usage.prompt_tokens
                call.completion_tokens = response.usage.completion_tokens
                return response

        return SpyClient

    def record(self, profile: str, prompt: PromptBundle, schema: dict | None, settings: LLMProfile) -> SpyCall:
        images = [part.image for message in prompt.messages for part in message.parts if part.kind == "image"]
        texts = [part.text for message in prompt.messages for part in message.parts if part.kind == "text"]
        call = SpyCall(profile, schema_kind(schema), images, texts)
        if self.measure_images:
            px = min(prompt.image_px or settings.default_image_px or settings.max_image_px, settings.max_image_px)
            for image in images:
                _media, body = image.load_base64(px)
                with Image.open(io.BytesIO(base64.b64decode(body))) as picture:
                    call.sizes.append(picture.size)
        self.calls.append(call)
        return call

    def of(self, kind: str) -> list[SpyCall]:
        return [call for call in self.calls if call.kind == kind]

    def transport(self) -> dict:
        """@return 传输维度摘要：调用数、异常、终止原因、用量与耗时。"""
        errors: dict[str, int] = {}
        finishes: dict[str, int] = {}
        for call in self.calls:
            if call.error is not None:
                errors[call.error] = errors.get(call.error, 0) + 1
            finishes[str(call.finish)] = finishes.get(str(call.finish), 0) + 1
        return {"calls": len(self.calls), "by_kind": {kind: len(self.of(kind)) for kind in ("segment", "annotate", "verify")},
                "errors": errors, "finish": finishes,
                "prompt_tokens": sum(call.prompt_tokens for call in self.calls),
                "completion_tokens": sum(call.completion_tokens for call in self.calls),
                "latency_s": round(sum(call.latency_s for call in self.calls), 3)}


# ── 打分（测试代码计算，不进生产路径） ───────────────────────────────────────


def iou(left, right) -> float:
    """@return 两个 Shapely 几何的交并比；并集为空时为 0。"""
    union = left.union(right).area
    return left.intersection(right).area / union if union else 0.0


def match_regions(candidates: list, references: list, threshold: float = IOU_THRESHOLD) -> list[tuple[int, int, float]]:
    """按 IoU 降序一对一贪心匹配（SPEC §20.1）。

    @param candidates 候选几何
    @param references 参考几何
    @param threshold 命中阈值，IoU 不小于它才算命中
    @return (候选序号, 参考序号, IoU) 列表，按 IoU 降序
    """
    pairs = sorted(((iou(candidate, reference), c, r) for c, candidate in enumerate(candidates)
                    for r, reference in enumerate(references)), key=lambda pair: (-pair[0], pair[1], pair[2]))
    used_candidates: set[int] = set()
    used_references: set[int] = set()
    matched = []
    for value, c, r in pairs:
        if value < threshold:
            break
        if c not in used_candidates and r not in used_references:
            used_candidates.add(c)
            used_references.add(r)
            matched.append((c, r, value))
    return matched


def score_sample(items: list[dict], references: list[dict]) -> dict:
    """把一个样本的 ``_meta.regions.items`` 与人工标注比较。

    @param items 候选（带 GeoJSON 形态的 geometry）
    @param references 人工标注（带 label 与 polygon 或 geometry）
    @return 命中数、平均 IoU、多余候选数与命中的参考名
    """
    candidates = [shape(item["geometry"]) for item in items]
    truth = [shape(reference["geometry"]) if "geometry" in reference else Polygon(reference["polygon"])
             for reference in references]
    matched = match_regions(candidates, truth)
    values = [value for _c, _r, value in matched]
    return {"references": len(references), "candidates": len(items), "matched": len(matched),
            "matched_labels": sorted(references[r]["label"] for _c, r, _value in matched),
            "matched_region_ids": {items[c]["region_id"]: references[r]["label"] for c, r, _value in matched},
            "iou_sum": sum(values), "mean_iou": round(sum(values) / len(values), 4) if values else None,
            "extra": len(items) - len(matched)}


def recall_of(scores: list[dict]) -> float:
    """@return 全部样本的参考区域召回率。"""
    total = sum(score["references"] for score in scores)
    return sum(score["matched"] for score in scores) / total if total else 0.0


def score_selection(item: PipelineItem, expected: dict[str, str]) -> dict:
    """把 annotate 的选择与标签和期望比较；标签忽略大小写与首尾空白。

    @param item 跑过 annotate 或 verify 的信封
    @param expected {region_id: 期望标签}
    @return 选择是否完全正确、标签正确数与失败种类
    """
    if item.status == "failed" or item.annotation is None:
        return {"parsed": False, "errors": [error.kind for error in item.errors], "selected": [], "exact": False,
                "label_correct": 0, "label_total": len(expected), "misread": 0, "output": None}
    items = item.annotation.output["items"]
    labels = {entry["region_id"]: entry["label"] for entry in items}
    hits = [region_id for region_id in expected if region_id in labels]
    correct = [region_id for region_id in hits
               if labels[region_id].strip().casefold() == expected[region_id].strip().casefold()]
    return {"parsed": True, "errors": [], "selected": sorted(labels), "exact": set(labels) == set(expected),
            "label_correct": len(correct), "label_total": len(expected), "misread": len(hits) - len(correct),
            "output": labels}


def selection_summary(scores: list[dict]) -> dict:
    """@return 区域选择正确率、标签正确率与误读数。"""
    total = len(scores)
    labels = sum(score["label_total"] for score in scores)
    return {"cases": total, "parsed": sum(score["parsed"] for score in scores),
            "selection_accuracy": sum(score["exact"] for score in scores) / total if total else 0.0,
            "label_accuracy": sum(score["label_correct"] for score in scores) / labels if labels else 0.0,
            "misread": sum(score["misread"] for score in scores)}


def pair_stability(left: list, right: list) -> tuple[int, int]:
    """比较两次重复的逐项结果。None 表示该次失败：失败属于传输或解析，不算稳定，也不算比较过。

    @param left 第一次的结果
    @param right 第二次的结果，与 left 对齐
    @return (完全相同的对数, 两边都成功的对数)
    """
    compared = [(first, second) for first, second in zip(left, right) if first is not None and second is not None]
    return sum(first == second for first, second in compared), len(compared)


def parsed_selections(scores: list[dict]) -> list[list[str] | None]:
    """@return 每个样本选中的区域；解析失败的样本为 None，不与合法的空选择混同。"""
    return [score["selected"] if score["parsed"] else None for score in scores]


def sample_matches(run: dict) -> list[list[str] | None]:
    """@return 分割质量门一次运行里，按组、样本序号展平的命中参考名；没有输出行的样本为 None。"""
    return [group["scores"][index]["matched_labels"] if index in group["scored"] else None
            for group in run["groups"].values() for index in group["scores"]]


# ── 跑真实工程 ───────────────────────────────────────────────────────────────


@dataclass
class ProjectRun:
    """一次 ``execute_run`` 的结果。"""

    spy: Spy
    exit_code: int
    report: dict
    rows: dict[int, dict]
    rejects: list[dict]

    def parse_summary(self) -> dict:
        counts = self.report["counts"]
        return {"exit_code": self.exit_code, "emitted": counts["emitted"], "failed": counts["failed"],
                "segment_failed": self.report["segment_image"]["failed"],
                "rejects": [row["_meta"]["reason"] for row in self.rejects]}


def run_project(root: Path, project: str, monkeypatch, stem: str, spy: Spy | None = None) -> ProjectRun:
    """用真实装配跑一个工程，输出写到 ``out/<stem>.jsonl``，重复运行互不覆盖。

    @param root 工程目录
    @param project 工程文件名
    @param monkeypatch 用来替换客户端类型与日志装配
    @param stem 输出文件名主干
    @param spy 请求记录器；None 时新建一个
    @return 运行结果
    """
    spy = spy or Spy()
    monkeypatch.chdir(root)
    monkeypatch.setattr(application, "LLMClient", spy.client_type())
    monkeypatch.setattr(application, "setup_logging", lambda cfg: None)
    (root / "out").mkdir(exist_ok=True)
    output = root / "out" / f"{stem}.jsonl"
    code = application.execute_run(Path("../config.toml"), Path(project),
                                   CliOverrides(console="plain", output=str(output)))
    report = json.loads(output.with_name(f"{stem}.report.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()] if output.exists() else []
    rejects_path = output.with_name(f"{stem}.rejects.jsonl")
    rejects = [json.loads(line) for line in rejects_path.read_text(encoding="utf-8").splitlines()]
    return ProjectRun(spy, code, report, {row["_meta"]["source"]["pair_index"]: row for row in rows}, rejects)


def example_workspace(tmp_path: Path) -> Path:
    """把示例工程复制到临时目录，运行产物不落进仓库。"""
    project = tmp_path / "image-segmentation"
    shutil.copytree(EXAMPLE, project, ignore=shutil.ignore_patterns("out", "__pycache__"))
    shutil.copy(ROOT / "examples" / "config.toml", tmp_path / "config.toml")
    return project


def group_workspace(tmp_path: Path, name: str, samples: list[dict]) -> Path:
    """为一组样本建工程目录：图片按序号命名，文件名不带内容线索，钩子是示例钩子加路由。

    @param tmp_path 临时根目录
    @param name 工程目录名
    @param samples 样本（image、companion）
    @return 工程目录
    """
    root = tmp_path / name
    data = root / "data"
    data.mkdir(parents=True)
    shutil.copy(ROOT / "examples" / "config.toml", tmp_path / "config.toml")
    hooks = (EXAMPLE / "hooks.py").read_text(encoding="utf-8") + ROUTE_HOOK
    (root / "hooks.py").write_bytes(hooks.encode("utf-8"))
    (root / "selection.json").write_text(json.dumps(SELECTION_SCHEMA), encoding="utf-8")
    shutil.copy(EXAMPLE / "schemas" / "region-polygons.json", root / "polygons.json")
    for index, sample in enumerate(samples, start=1):
        shutil.copy(ROOT / sample["image"], data / f"image_{index}.png")
        companion = sample.get("companion")
        if companion is None:
            continue
        body = companion["literal"].encode("utf-8") if "literal" in companion else (ROOT / companion["path"]).read_bytes()
        (data / f"image_{index}.cmp").write_bytes(body)
    return root


def image_project(segment_instruction: str, *, hook: bool, annotate: str, schema: str, extra: str = "") -> str:
    """组装图片模式工程文本。

    @param segment_instruction segment.instruction
    @param hook 是否注册 box_parser
    @param annotate [annotate] 节文本
    @param schema 输出 Schema 文件名
    @param extra 追加的节（默认关闭 verify）
    @return project.toml 文本
    """
    box_parser = 'box_parser = "hooks.py:route_boxes"\ncompanion_suffix = ".cmp"\n' if hook else ""
    verify = "" if "[verify]" in extra else "[verify]\nenabled = false\n\n"
    return (f'schema_version = 1\n\n[run]\ninput = "./data"\noutput = "./out/run.jsonl"\nmodality = "ui"\n'
            f'batch_size = 4\nseed = 20261030\n\n[dedup]\nenabled = false\n\n[segment]\nenabled = true\nmode = "image"\n'
            f'llm = "default"\n{box_parser}instruction = {json.dumps(segment_instruction, ensure_ascii=False)}\n\n'
            f'[quality]\nenabled = false\n\n{annotate}\n\n{verify}{extra}\n[trace]\nenabled = false\n\n'
            f'[output]\nschema_path = "{schema}"\nmeta_mode = "inline"\nrejects = "refs"\n')


def write_project(root: Path, text: str) -> None:
    """按 UTF-8 与 LF 写 project.toml。"""
    (root / "project.toml").write_bytes(text.encode("utf-8"))


def annotate_section(instruction: str, postprocessor: str | None = None) -> str:
    """@return [annotate] 节文本。"""
    hook = f'postprocessor = "{postprocessor}"\n' if postprocessor else ""
    return (f'[annotate]\nenabled = true\nllm = "default"\n{hook}'
            f'instruction = {json.dumps(instruction, ensure_ascii=False)}\n')


# ── 固定候选注入真实阶段 ─────────────────────────────────────────────────────


def fixed_regions(image: Path, rectangles: list[list[int]]) -> RegionSet:
    """把网格矩形经真实几何校验构造成固定候选，编号 r1…rN。

    @param image 图片路径
    @param rectangles 0-1000 网格 [l, t, r, b]
    @return 冻结的 RegionSet
    """
    with Image.open(image) as picture:
        width, height = picture.size
    model = {"regions": [{"box_id": None, "source_box_ids": None, "polygons": [
        {"exterior": [[left, top], [right, top], [right, bottom], [left, bottom], [left, top]], "holes": []}]}
        for left, top, right, bottom in rectangles]}
    result = validate_regions(model, (), width, height)
    assert result.violations == ()
    return result.proof


def injected_item(image: Path, regions: RegionSet, annotation: Annotation | None = None) -> PipelineItem:
    """直接构造带 RegionSet 的信封（proposal §5 第 5 道门的注入方式）。"""
    identifier = hashlib.sha256(image.read_bytes()).hexdigest()[:16]
    record = Record(id=identifier, modality="ui", text=None, raw=None, ui_tree=None,
                    image=ImageRef(image, "png", image.stat().st_size), ref=RecordRef(image.name, None, 1, ()))
    return PipelineItem(record=record, regions=regions, annotation=annotation)


def injection_config(root: Path, instruction: str, extra: str = "") -> ResolvedConfig:
    """经真实 M1 得到注入用配置：工程只含 annotate（可选 verify）与选择 Schema。

    @param root 工程目录，父目录已有 config.toml
    @param instruction annotate.instruction
    @param extra 追加的节
    @return 已解析配置
    """
    write_project(root, image_project("分割", hook=False, annotate=annotate_section(instruction),
                                      schema="selection.json", extra=extra))
    return load(root.parent / "config.toml", root / "project.toml", CliOverrides(console="plain"))


def run_stage(cfg: ResolvedConfig, stage, items: list[PipelineItem], spy: Spy) -> None:
    """用真实资源、运行时、客户端与 Schema 引擎跑一个阶段。

    @param cfg 已解析配置
    @param stage 真实阶段实例
    @param items 注入的信封，阶段就地修改
    @param spy 请求记录器
    """
    run_id = "segmentimage"
    event_log = EventLog(cfg.trace, run_id)
    metrics = MetricsSink(cfg, run_id, event_log)
    resources = application._build_resources(cfg, metrics)
    runtime = ExecutionRuntime(resources, metrics)
    client = spy.client_type()(cfg.llm_profiles, cfg.embedding_profiles, resolve_credentials(cfg), resources, metrics)
    engine = SchemaEngine(dict(cfg.user_schema), client, cfg.output, metrics, validator=application._l25_validator(cfg))
    ctx = RunContext(cfg=cfg, llm=client, schema_engine=engine, rng=random.Random(f"{cfg.run.seed}:1:{stage.name}"),
                     batch_no=1, metrics=metrics, tasks=runtime, task_namespace=f"{run_id}:batch:1:stage:{stage.name}")

    async def main() -> None:
        try:
            await runtime.run(lambda: stage.run(items, ctx))
        finally:
            await client.aclose()

    try:
        asyncio.run(main())
    finally:
        event_log.close()


def remove_overlay(prompt: PromptBundle) -> PromptBundle:
    """消融门的处理：去掉叠图、它的标签与材料说明里描述叠图的句子，其余逐字保留。

    @param prompt annotate 的真实提示词
    @return 只含原图与候选数据的提示词
    @raises AssertionError 没有找到叠图或说明句，消融没有真正发生
    """
    removed = sentence = 0
    messages = []
    for message in prompt.messages:
        parts = []
        for part in message.parts:
            if part.kind == "image" and isinstance(part.image, RegionOverlayRef):
                removed += 1
                continue
            if part.kind == "text" and part.text == OVERLAY_LABEL:
                continue
            if part.kind == "text" and OVERLAY_SENTENCE in part.text:
                sentence += 1
                part = dataclasses.replace(part, text=part.text.replace(OVERLAY_SENTENCE, ""))
            parts.append(part)
        messages.append(dataclasses.replace(message, parts=tuple(parts)))
    assert removed == 1 and sentence == 1, (removed, sentence)
    return dataclasses.replace(prompt, messages=tuple(messages))


def run_case(root: Path, case: dict, prompt: dict, spy: Spy) -> tuple[dict, str | None]:
    """把一个固定候选样本注入真实 annotate，并打分。

    @param root 工程目录
    @param case 夹具样本（image、candidates）
    @param prompt 夹具提示（instruction、expected）
    @param spy 请求记录器
    @return (打分结果, 请求里的候选块文本)
    """
    image = ROOT / case["image"]
    regions = fixed_regions(image, case["candidates"])
    item = injected_item(image, regions)
    cfg = injection_config(root, prompt["instruction"])
    before = len(spy.calls)
    run_stage(cfg, AnnotateStage(cfg), [item], spy)
    calls = spy.calls[before:]
    assert all(overlay.regions is regions for call in calls for overlay in call.images
               if isinstance(overlay, RegionOverlayRef))
    score = score_selection(item, prompt["expected"])
    return {"case": case["name"], "instruction": prompt["instruction"], **score}, calls[0].block("[候选区域]") if calls else None


def injection_workspace(tmp_path: Path) -> Path:
    """建注入用的工程目录：M1 要求 run.input 存在，这里放一张占位图。"""
    root = group_workspace(tmp_path, "injection", [{"image": "examples/image-segmentation/data/photos/image_1.png"}])
    (root / "out").mkdir()
    return root


# ── 第 1 道门：只有看懂像素才能答对 ──────────────────────────────────────────


def perception_board(path: Path, rng: random.Random) -> dict:
    """画一张随机棋盘：答案只在像素里，文件名与提示词都不含线索。

    @param path 输出 PNG
    @param rng 随机源
    @return 期望答案
    """
    cell = 200
    image = Image.new("RGB", (cell * 3, cell * 3), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for offset in range(0, cell * 3 + 1, cell):
        draw.line((offset, 0, offset, cell * 3), fill=(120, 120, 120), width=3)
        draw.line((0, offset, cell * 3, offset), fill=(120, 120, 120), width=3)
    cells = [(row, column) for row in range(3) for column in range(3)]
    rng.shuffle(cells)
    (row, column), (dot_row, dot_column) = cells[0], cells[1]
    color = rng.choice(sorted(PERCEPTION_COLORS))
    draw.rectangle((column * cell + 4, row * cell + 4, (column + 1) * cell - 4, (row + 1) * cell - 4),
                   fill=PERCEPTION_COLORS[color])
    dots = rng.randint(1, 4)
    for dot in range(dots):
        x = dot_column * cell + 40 + dot * 40
        y = dot_row * cell + 100
        draw.ellipse((x - 12, y - 12, x + 12, y + 12), fill=(0, 0, 0))
    image.save(path)
    return {"row": row + 1, "column": column + 1, "color": color, "dots": dots}


def blank_board(path: Path) -> dict:
    """画一张空棋盘作为反向对照：没有涂色也没有圆点。"""
    cell = 200
    image = Image.new("RGB", (cell * 3, cell * 3), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for offset in range(0, cell * 3 + 1, cell):
        draw.line((offset, 0, offset, cell * 3), fill=(120, 120, 120), width=3)
        draw.line((0, offset, cell * 3, offset), fill=(120, 120, 120), width=3)
    image.save(path)
    return {"row": 0, "column": 0, "color": "none", "dots": 0}


def perception_profile() -> LLMProfile:
    """@return 第 1 道门用的 glm-5.2 档案。"""
    return LLMProfile(name="vision", provider="anthropic", base_url=ZAI_BASE_URL, model=ZAI_MODEL,
                      api_key_env=ZAI_KEY_ENV, max_concurrency=2, timeout_s=120, max_retries=2,
                      supports_structured_output=True, supports_vision=True, max_output_tokens=4096,
                      context_window=131072, temperature=0.0)


async def ask_boards(boards: list[Path]) -> list[dict]:
    """每张棋盘问一次，把传输失败与解析失败分开记录。

    @param boards 棋盘图片路径
    @return 与 boards 对齐的 {"answer", "transport_error", "parse_error"}
    """
    profile = perception_profile()
    client = make_llm_client({"vision": profile}, {},
                             RuntimeCredentials(llm={"vision": (os.environ[ZAI_KEY_ENV],)}, embedding={}))
    engine = SchemaEngine({}, client, OutputConfig(schema_inline="{}", max_repair_attempts=0))
    results = []
    try:
        for path in boards:
            bundle = PromptBundle(messages=(Message(role="user", parts=(
                Part(kind="text", text=PERCEPTION_QUESTION),
                Part(kind="image", image=ImageRef(path, "png", path.stat().st_size)))),))
            outcome = {"answer": None, "transport_error": None, "parse_error": None}
            try:
                outcome["answer"], _usage, _attempts, _model = await engine.complete_validated(
                    "vision", bundle, PERCEPTION_SCHEMA, scope=CallScope())
            except (ProviderFatalError, ProviderRetryableError) as exc:
                outcome["transport_error"] = type(exc).__name__
            except (SchemaViolation, OutputTruncatedError) as exc:
                outcome["parse_error"] = type(exc).__name__
            results.append(outcome)
    finally:
        await client.aclose()
    return results


def test_zai_pixel_perception(tmp_path):
    """[PENDING-EVIDENCE:zai-pixel-perception]：随机棋盘的格子、颜色与圆点数只有看懂像素才能答对。"""
    seed = random.SystemRandom().randrange(2 ** 32)
    rng = random.Random(seed)
    boards = [tmp_path / f"a{index}.png" for index in range(PERCEPTION_TRIALS)]
    truth = [perception_board(path, rng) for path in boards]
    control = tmp_path / "control.png"
    control_truth = blank_board(control)

    first = asyncio.run(ask_boards([*boards, control]))
    repeat = asyncio.run(ask_boards(boards))

    trials = first[:PERCEPTION_TRIALS]
    answered = [outcome["answer"] for outcome in trials]
    located = [answer is not None and (answer["row"], answer["column"], answer["color"]) == (expected["row"], expected["column"], expected["color"])
               for answer, expected in zip(answered, truth)]
    counted = [answer is not None and answer["dots"] == expected["dots"] for answer, expected in zip(answered, truth)]
    control_answer = first[PERCEPTION_TRIALS]["answer"]
    repeat_answers = [outcome["answer"] for outcome in repeat]
    repeat_identical, repeat_total = pair_stability(answered, repeat_answers)

    evidence = Evidence("zai-pixel-perception", "test_zai_pixel_perception")
    everything = first + repeat
    evidence.transport = {"calls": len(everything),
                          "errors": [outcome["transport_error"] for outcome in everything if outcome["transport_error"]]}
    evidence.parse = {"parsed": sum(outcome["answer"] is not None for outcome in everything),
                      "errors": [outcome["parse_error"] for outcome in everything if outcome["parse_error"]]}
    evidence.semantic = {"seed": seed, "trials": PERCEPTION_TRIALS, "located": sum(located), "dots_correct": sum(counted),
                         "truth": truth, "answers": answered,
                         "control_truth": control_truth, "control_answer": control_answer}
    evidence.stability = {"repeat_identical": repeat_identical, "repeat_total": repeat_total,
                          "trials": PERCEPTION_TRIALS, "repeat_answers": repeat_answers}
    evidence.write()

    assert evidence.transport["errors"] == []
    assert evidence.parse["errors"] == []
    assert sum(located) / PERCEPTION_TRIALS >= PERCEPTION_FLOOR
    assert control_answer is not None and control_answer["color"] == "none"


# ── 第 2 道门：分割 → 叠图 → 标注 → 后处理 ───────────────────────────────────


def labels_with_keywords(rows: dict[int, dict], references_by_index: dict[int, list[dict]]) -> dict:
    """统计命中的区域里，标注标签含人工关键词的比例，语义不靠 Schema 通过判断。

    @param rows 按序号索引的输出行
    @param references_by_index 按序号索引的人工标注
    @return 命中、关键词命中与分样本打分
    """
    scores = {}
    keyword_hits = keyword_total = 0
    for index, references in references_by_index.items():
        row = rows.get(index)
        if row is None:
            scores[index] = None
            continue
        score = score_sample(row["_meta"]["regions"]["items"], references)
        scores[index] = score
        keywords = {reference["label"]: reference["keywords"] for reference in references}
        labels = {item["region_id"]: item["label"] for item in row["items"]}
        for region_id, label in score["matched_region_ids"].items():
            if region_id in labels and keywords[label]:
                keyword_total += 1
                text = labels[region_id].casefold()
                keyword_hits += any(keyword.casefold() in text for keyword in keywords[label])
    return {"scores": scores, "keyword_hits": keyword_hits, "keyword_total": keyword_total}


def geometry_from_candidates(row: dict) -> bool:
    """@return 输出行每个标注项的几何是否都逐值等于它引用的候选几何。"""
    geometry = {item["region_id"]: item["geometry"] for item in row["_meta"]["regions"]["items"]}
    return all(item["region_id"] in geometry and item.get("geometry") == geometry[item["region_id"]]
               for item in row["items"])


def check_end_to_end_run(evidence: Evidence, label: str, run: ProjectRun, semantic: dict) -> None:
    """逐条求值一次端到端运行的断言并写进证据；第一次运行失败不挡住第二次的判定。

    @param evidence 节点证据
    @param label 运行名，作断言名的前缀
    @param run 运行结果
    @param semantic labels_with_keywords 的结果
    """
    evidence.check("parse", f"{label}.exit_code", run.exit_code == 0)
    evidence.check("transport", f"{label}.segment_calls", run.report["segment_image"]["calls"] == 2)
    # 请求形态：segment 只有原图，annotate 原图加叠图，同一候选块贯穿首轮与修复。
    evidence.check("semantic", f"{label}.segment_request_images",
                   all(call.image_types == ["ImageRef"] for call in run.spy.of("segment")))
    evidence.check("semantic", f"{label}.annotate_request_images",
                   run.spy.of("annotate") and all(call.image_types == ["ImageRef", "RegionOverlayRef"]
                                                  for call in run.spy.of("annotate")))
    evidence.check("semantic", f"{label}.geometry_from_candidates",
                   all(geometry_from_candidates(row) for row in run.rows.values()))
    scores = [score for score in semantic["scores"].values() if score]
    evidence.check("semantic", f"{label}.scored_samples", len(scores) == 2)
    evidence.check("semantic", f"{label}.recall", recall_of(scores) >= RECALL_FLOOR)
    evidence.check("semantic", f"{label}.label_keywords",
                   semantic["keyword_total"] and semantic["keyword_hits"] / semantic["keyword_total"] >= LABEL_FLOOR)


def test_zai_segment_image_end_to_end(tmp_path, monkeypatch):
    """[PENDING-EVIDENCE:segment-image-zai]：纯图片最小端到端，annotate 按 region_id 选择，几何由后处理写入。"""
    fixtures = load_fixtures()
    samples = next(group for group in fixtures["quality_groups"] if group["name"] == "pure_image")["samples"]
    references = {1: samples[0]["references"], 2: samples[1]["references"]}
    root = example_workspace(tmp_path)

    runs = [run_project(root, "project.toml", monkeypatch, f"photos-{run}") for run in range(REPEATS)]
    semantics = [labels_with_keywords(run.rows, references) for run in runs]

    evidence = Evidence("segment-image-zai", "test_zai_segment_image_end_to_end")
    evidence.transport = {"runs": [run.spy.transport() for run in runs]}
    evidence.parse = {"runs": [run.parse_summary() for run in runs]}
    evidence.semantic = {"runs": semantics, "recall": [recall_of([s for s in semantic["scores"].values() if s])
                                                       for semantic in semantics]}
    evidence.stability = {"candidates": [[len(row["_meta"]["regions"]["items"]) for _i, row in sorted(run.rows.items())]
                                         for run in runs],
                          "matched": [{index: score and score["matched_labels"] for index, score in semantic["scores"].items()}
                                      for semantic in semantics]}
    for run, (project_run, semantic) in enumerate(zip(runs, semantics)):
        check_end_to_end_run(evidence, f"run{run}", project_run, semantic)
    evidence.write()

    assert evidence.failed() == []


def test_zai_reference_box_inputs(tmp_path, monkeypatch):
    """[PENDING-EVIDENCE:segment-image-zai]：注册 box_parser 后，对齐的框可复用，空数组与外壳框都继续分割。"""
    samples = [
        {"image": "examples/image-segmentation/data/screens-xml/image_1.png",
         "companion": {"path": "examples/image-segmentation/data/screens-xml/image_1.xml"}},
        {"image": "examples/image-segmentation/data/screens-xml/image_2.png",
         "companion": {"path": "examples/image-segmentation/data/screens-xml/image_2.xml"}},
        {"image": "examples/image-segmentation/data/screens-xml/image_1.png", "companion": {"literal": "empty"}},
        {"image": "examples/image-segmentation/data/photos/image_2.png", "companion": {"literal": "shell"}},
    ]
    root = group_workspace(tmp_path, "reference-boxes", samples)
    write_project(root, image_project(
        "结合图片和参考框生成候选区域。参考框准确时直接用 box_id 复用，不要为同一个控件另画多边形。"
        "没有参考框或参考框只是整个窗口时，依据图片生成候选。不要生成覆盖整张图片的背景区域。",
        hook=True, schema="polygons.json",
        annotate=annotate_section("判断每个候选区域是什么。通过 region_id 引用候选，label 写名称。", "hooks.py:keep_polygons")))

    runs = [run_project(root, "project.toml", monkeypatch, f"boxes-{run}") for run in range(REPEATS)]

    def reused(run: ProjectRun) -> list[str]:
        return sorted(item["box_id"] for item in run.rows[1]["_meta"]["regions"]["items"] if item["box_id"])

    evidence = Evidence("segment-image-zai", "test_zai_reference_box_inputs")
    evidence.transport = {"runs": [run.spy.transport() for run in runs]}
    evidence.parse = {"runs": [run.parse_summary() for run in runs]}
    evidence.semantic = {"runs": [{
        "aligned_boxes": [box["id"] for box in run.rows[1]["_meta"]["regions"]["boxes"]] if 1 in run.rows else None,
        "aligned_reused": reused(run) if 1 in run.rows else None,
        "candidates": {index: len(row["_meta"]["regions"]["items"]) for index, row in sorted(run.rows.items())},
    } for run in runs]}
    evidence.stability = {"aligned_reused": [reused(run) if 1 in run.rows else None for run in runs]}
    evidence.write()

    for run in runs:
        assert run.exit_code == 0
        assert sorted(run.rows) == [1, 2, 3, 4], run.parse_summary()
        aligned, shell_tree, empty, shell = (run.rows[index]["_meta"]["regions"] for index in (1, 2, 3, 4))
        assert [box["id"] for box in aligned["boxes"]] == ["n1", "n2", "n3"]
        assert shell_tree["boxes"] == [] and empty["boxes"] == []
        assert [box["id"] for box in shell["boxes"]] == ["shell"]
        # 钩子返回空数组或外壳框时仍依据图片生成候选。
        assert empty["items"] and shell["items"]
        # 复用的框几何逐值等于钩子给出的像素矩形。
        rectangles = {box["id"]: box["bbox"] for box in aligned["boxes"]}
        reused_items = [item for item in aligned["items"] if item["box_id"]]
        assert len(reused_items) / len(aligned["boxes"]) >= REUSE_FLOOR
        for item in reused_items:
            left, top, right, bottom = rectangles[item["box_id"]]
            assert item["region_id"] == item["box_id"]
            assert item["geometry"] == {"type": "Polygon", "coordinates": [
                [[left, top], [right, top], [right, bottom], [left, bottom], [left, top]]]}
        prompts = {call.block("[参考框]") is not None for call in run.spy.of("segment")}
        assert prompts == {True, False}
        assert sum("本样本没有参考框。" in call.texts for call in run.spy.of("segment")) >= 2


def test_zai_boxes_only_path(tmp_path, monkeypatch):
    """[PENDING-EVIDENCE:segment-image-zai]：只用参考框，segment 不调用模型，annotate 在这组候选上选择。"""
    keywords = {"n1": ("登录", "sign"), "n2": ("注册", "register"), "n3": ("忘记", "找回", "密码", "forgot", "password")}
    root = example_workspace(tmp_path)
    runs = [run_project(root, "project-boxes.toml", monkeypatch, f"only-{run}") for run in range(REPEATS)]

    def label_hits(run: ProjectRun) -> tuple[int, int]:
        items = run.rows[1]["items"] if 1 in run.rows else []
        total = [item for item in items if item["region_id"] in keywords]
        return sum(any(word in item["label"].casefold() for word in keywords[item["region_id"]]) for item in total), len(total)

    evidence = Evidence("segment-image-zai", "test_zai_boxes_only_path")
    evidence.transport = {"runs": [run.spy.transport() for run in runs]}
    evidence.parse = {"runs": [run.parse_summary() for run in runs]}
    evidence.semantic = {"runs": [{"label_hits": label_hits(run),
                                   "labels": [[item["region_id"], item["label"]] for item in run.rows[1]["items"]]
                                   if 1 in run.rows else None} for run in runs]}
    evidence.stability = {"selected": [sorted(item["region_id"] for item in run.rows[1]["items"]) if 1 in run.rows else None
                                       for run in runs]}
    evidence.write()

    for run in runs:
        assert run.exit_code == 0
        assert run.spy.of("segment") == []
        block = run.report["segment_image"]
        assert (block["calls"], block["usage"]) == (0, {"prompt_tokens": 0, "completion_tokens": 0})
        assert (block["samples"], block["regions"], block["empty"]) == (2, 3, 1)
        regions = run.rows[1]["_meta"]["regions"]
        assert [item["region_id"] for item in regions["items"]] == ["n1", "n2", "n3"]
        assert all(call.block("[参考框]") is None for call in run.spy.of("annotate"))
        rectangles = {box["id"]: box["bbox"] for box in regions["boxes"]}
        assert run.rows[1]["items"] and all(item["bbox"] == rectangles[item["region_id"]] for item in run.rows[1]["items"])
        assert run.rows[2]["_meta"]["regions"]["items"] == []
        hits, total = label_hits(run)
        assert total and hits / total >= LABEL_FLOOR


@dataclass
class RepairRun:
    """一次 verify 真实返工：请求记录、跑完的信封、注入的候选与重标注的打分。"""

    spy: Spy
    item: PipelineItem
    regions: RegionSet
    repaired: dict


def run_repair(injection: Path, fixed: dict) -> RepairRun:
    """注入明显错误的标注，真实评委判负后真实重标注。

    @param injection 注入用工程目录
    @param fixed 示例的固定候选夹具
    @return 这次返工的结果
    """
    rework = load_fixtures()["rework_case"]
    regions = validate_regions(fixed["model_output"], (), fixed["image"]["width"], fixed["image"]["height"]).proof
    verify_section = '[verify]\nenabled = true\nllm = "judge"\npolicy = "repair"\nmax_repair_rounds = 1\n\n'
    cfg = injection_config(injection, rework["instruction"], extra=verify_section)
    wrong = Annotation(output=rework["wrong_output"], model=ZAI_MODEL, attempts=1, usage=Usage())
    item = injected_item(EXAMPLE / "data" / "fixed" / "image_1.png", regions, wrong)
    spy = Spy(measure_images=True)
    run_stage(cfg, VerifyStage(cfg), [item], spy)
    return RepairRun(spy, item, regions, score_selection(item, rework["expected"]))


def repair_confirmation(repair: RepairRun) -> dict:
    """汇总返工是否用同一候选与叠图（SPEC §20.1 第 2 道门），判定与证据都取这一份事实。

    @param repair 一次返工的结果
    @return 候选块数、叠图数与同一性、逐请求图片尺寸、是否重新分割
    """
    material = repair.spy.of("annotate") + repair.spy.of("verify")
    blocks = {call.block("[候选区域]") for call in material}
    overlays = [image for call in material for image in call.images if isinstance(image, RegionOverlayRef)]
    return {"segment_calls": len(repair.spy.of("segment")), "annotate_calls": len(repair.spy.of("annotate")),
            "candidate_blocks": len(blocks), "candidate_block_missing": None in blocks,
            "overlays": len(overlays), "overlays_same_regions": all(overlay.regions is repair.regions for overlay in overlays),
            "request_sizes": [call.sizes for call in material], "item_regions_same": repair.item.regions is repair.regions}


def record_scaled(evidence: Evidence, label: str, scaled: ProjectRun, references: list[dict]) -> list[str] | None:
    """把一次缩放运行的事实与断言结果写进证据。

    @param evidence 节点证据
    @param label 运行名，作断言名的前缀
    @param scaled 缩放运行的结果
    @param references 人工标注
    @return 命中的参考名，用来比较重复运行；没有输出行时为 None
    """
    score = score_sample(scaled.rows[1]["_meta"]["regions"]["items"], references) if 1 in scaled.rows else None
    evidence.transport["scaled"].append(scaled.spy.transport())
    evidence.parse["scaled"].append(scaled.parse_summary())
    evidence.semantic["scaled"].append({"score": score, "request_sizes": sorted(
        {size for call in scaled.spy.calls for size in call.sizes})})
    evidence.check("parse", f"{label}.scaled.exit_code", scaled.exit_code == 0)
    segment_calls = scaled.spy.of("segment")
    evidence.check("semantic", f"{label}.scaled.segment_request_size",
                   segment_calls and all(call.sizes == [SCALED_SIZE] for call in segment_calls))
    evidence.check("semantic", f"{label}.scaled.annotate_request_size",
                   all(call.sizes == [SCALED_SIZE, SCALED_SIZE] for call in scaled.spy.of("annotate")))
    evidence.check("semantic", f"{label}.scaled.recall", score is not None and recall_of([score]) >= RECALL_FLOOR)
    return None if score is None else score["matched_labels"]


def record_repair(evidence: Evidence, label: str, repair: RepairRun) -> list[str] | None:
    """把一次返工的事实与断言结果写进证据，缩放失败时这些确认同样被求值并留下。

    @param evidence 节点证据
    @param label 运行名，作断言名的前缀
    @param repair 一次返工的结果
    @return 重标注选中的区域，用来比较重复运行；解析失败时为 None
    """
    item, repaired = repair.item, repair.repaired
    facts = repair_confirmation(repair)
    evidence.transport["repair"].append(repair.spy.transport())
    evidence.parse["repair"].append({"status": item.status, "errors": [error.kind for error in item.errors]})
    evidence.semantic["repair"].append({
        "verification": None if item.verification is None else {
            "verdict": item.verification.verdict, "rounds": item.verification.rounds},
        "repaired": repaired, "confirmation": facts})
    # 真实评委判负，确实返工。
    evidence.check("semantic", f"{label}.repair.reworked", item.verification is not None and item.verification.rounds >= 2)
    # 返工不重新分割。
    evidence.check("semantic", f"{label}.repair.no_segment_call", facts["segment_calls"] == 0)
    evidence.check("semantic", f"{label}.repair.annotate_called", facts["annotate_calls"] > 0)
    evidence.check("semantic", f"{label}.repair.same_candidate_block",
                   facts["candidate_blocks"] == 1 and not facts["candidate_block_missing"])
    evidence.check("semantic", f"{label}.repair.same_overlay", facts["overlays"] > 0 and facts["overlays_same_regions"])
    evidence.check("semantic", f"{label}.repair.request_size",
                   all(sizes == [SCALED_SIZE, SCALED_SIZE] for sizes in facts["request_sizes"]))
    evidence.check("semantic", f"{label}.repair.item_regions_same", facts["item_regions_same"])
    evidence.check("parse", f"{label}.repair.parsed", repaired["parsed"])
    evidence.check("semantic", f"{label}.repair.label_accuracy",
                   repaired["label_correct"] / repaired["label_total"] >= LABEL_FLOOR)
    return parsed_selections([repaired])[0]


def survive(evidence: Evidence, name: str, work):
    """跑实验的一半：异常记进传输维度而不外抛，另一半和后面的重复照常运行。

    @param evidence 节点证据
    @param name 这一半的名字，作断言名
    @param work 无参函数，真正发请求
    @return work 的返回值；异常时为 None
    """
    try:
        outcome = work()
    except Exception as exc:  # noqa: BLE001 —— 记录后由节点末尾的断言判失败
        evidence.transport["errors"][name] = f"{type(exc).__name__}: {exc}"
        outcome = None
    evidence.check("transport", f"{name}.completed", name not in evidence.transport["errors"])
    return outcome


def repeat_result(values: list) -> dict:
    """@return 同一路径各次重复的结果，以及两次都成功时是否相同；失败的一次为 None。"""
    identical, compared = pair_stability(values[:1], values[1:])
    return {"values": values, "compared": compared, "identical": identical}


def test_zai_scaled_request_and_verify_repair(tmp_path, monkeypatch):
    """[PENDING-EVIDENCE:segment-image-zai]：缩放请求下几何仍落回原图；verify 真实返工用同一候选与叠图。

    两半是独立实验，只有这个节点走到，所以各跑 REPEATS 次记录稳定性；一半失败时另一半照常运行、照常判定。
    """
    fixed = json.loads((EXAMPLE / "fixtures" / "fixed-candidates.json").read_text(encoding="utf-8"))
    references = [{"label": item["region_id"], "geometry": item["geometry"]} for item in fixed["expected_items"]]
    root = example_workspace(tmp_path)
    injection = injection_workspace(tmp_path)

    evidence = Evidence("segment-image-zai", "test_zai_scaled_request_and_verify_repair")
    evidence.transport = {"scaled": [], "repair": [], "errors": {}}
    evidence.parse = {"scaled": [], "repair": []}
    evidence.semantic = {"scaled": [], "repair": []}
    scaled_matched: list[list[str] | None] = []
    repair_selected: list[list[str] | None] = []
    for run in range(REPEATS):
        # 缩放：请求尺寸与原图不同，投影公式按原图宽高把网格坐标换算回去。
        scaled_matched.append(survive(evidence, f"run{run}.scaled", lambda: record_scaled(evidence, f"run{run}", run_project(
            root, "project-fixed.toml", monkeypatch, f"fixed-{run}", Spy(measure_images=True)), references)))
        repair_selected.append(survive(evidence, f"run{run}.repair", lambda: record_repair(
            evidence, f"run{run}", run_repair(injection, fixed))))
    evidence.stability = {"scaled_matched": repeat_result(scaled_matched), "repair_selected": repeat_result(repair_selected)}
    evidence.write()

    assert evidence.failed() == []


# ── 第 3 道门：叠图消融 ──────────────────────────────────────────────────────


def test_zai_overlay_ablation(tmp_path):
    """[PENDING-EVIDENCE:segment-image-overlay-ablation]：固定候选下比较「原图 + 候选数据」与「再加叠图」。"""
    cases = load_fixtures()["ablation_cases"]
    root = injection_workspace(tmp_path)
    arms = {"with_overlay": None, "without_overlay": remove_overlay}
    results: dict[str, list[list[dict]]] = {arm: [] for arm in arms}
    spies: dict[str, list[Spy]] = {arm: [] for arm in arms}
    for arm, transform in arms.items():
        for _repeat in range(REPEATS):
            spy = Spy(transform=transform)
            scores = [run_case(root, case, prompt, spy)[0] for case in cases for prompt in case["prompts"]]
            results[arm].append(scores)
            spies[arm].append(spy)

    def stability(arm: str) -> dict:
        identical, compared = pair_stability(parsed_selections(results[arm][0]), parsed_selections(results[arm][1]))
        return {"identical_selection": identical, "compared": compared, "cases": len(results[arm][0])}

    evidence = Evidence("segment-image-overlay-ablation", "test_zai_overlay_ablation")
    evidence.transport = {arm: [spy.transport() for spy in spies[arm]] for arm in arms}
    evidence.parse = {arm: [{"parsed": sum(score["parsed"] for score in scores), "cases": len(scores),
                             "errors": [score["errors"] for score in scores if score["errors"]]}
                            for scores in results[arm]] for arm in arms}
    evidence.semantic = {arm: {"summary": [selection_summary(scores) for scores in results[arm]],
                               "cases": results[arm]} for arm in arms}
    evidence.stability = {arm: stability(arm) for arm in arms}
    evidence.write()

    for spy in spies["with_overlay"]:
        assert spy.calls and all(call.image_types == ["ImageRef", "RegionOverlayRef"] for call in spy.calls)
    for spy in spies["without_overlay"]:
        assert spy.calls and all(call.image_types == ["ImageRef"] for call in spy.calls)
        assert all(OVERLAY_LABEL not in text for call in spy.calls for text in call.texts)
    # 叠图是否有益是实验问题：只要求两臂都跑通且叠图臂的语义不低于门槛，差值只记录，不断言方向。
    for arm in arms:
        assert all(summary["parsed"] == summary["cases"] for summary in map(selection_summary, results[arm]))
    assert all(selection_summary(scores)["selection_accuracy"] >= SELECTION_FLOOR for scores in results["with_overlay"])


# ── 第 4 道门：分割质量 ──────────────────────────────────────────────────────


def test_zai_segmentation_quality(tmp_path, monkeypatch):
    """[PENDING-EVIDENCE:segment-image-quality]：只分割运行读取 _meta.regions，与人工标注比 IoU。"""
    fixtures = load_fixtures()
    runs: list[dict] = []
    transports: list[dict] = []
    parses: list[dict] = []
    for repeat in range(REPEATS):
        groups: dict[str, dict] = {}
        for group in fixtures["quality_groups"]:
            root = tmp_path / f"run-{repeat}"
            root.mkdir(exist_ok=True)
            project = group_workspace(root, group["name"], group["samples"])
            write_project(project, image_project(fixtures["segmentation_instruction"], hook=group["hook"],
                                                 annotate="[annotate]\nenabled = false\n", schema="polygons.json"))
            run = run_project(project, "project.toml", monkeypatch, group["name"])
            transports.append({"repeat": repeat, "group": group["name"], **run.spy.transport()})
            parses.append({"repeat": repeat, "group": group["name"], **run.parse_summary()})
            # 没有输出行的样本按零候选计分：召回率的分母是全部人工标注，失败的样本不能从分母里消失。
            scores = {index: score_sample(run.rows[index]["_meta"]["regions"]["items"] if index in run.rows else [],
                                          sample["references"])
                      for index, sample in enumerate(group["samples"], start=1)}
            failed = run.report["segment_image"]["failed"]
            samples = len(group["samples"])
            groups[group["name"]] = {
                "samples": samples, "scores": scores, "scored": [index for index in scores if index in run.rows],
                "recall": recall_of(list(scores.values())),
                "mean_iou": _mean_iou(list(scores.values())),
                "extra": sum(score["extra"] for score in scores.values()),
                "empty_rate": run.report["segment_image"]["empty"] / samples,
                "truncated_rate": failed.get("output_truncated", 0) / samples,
                "segmentation_invalid_rate": failed.get("segmentation_invalid", 0) / samples,
                "failed": failed,
                "boxes": run.report["segment_image"]["boxes"],
                "calls": run.report["segment_image"]["calls"],
                "usage": run.report["segment_image"]["usage"],
            }
        all_scores = [score for group in groups.values() for score in group["scores"].values()]
        total_samples = sum(group["samples"] for group in groups.values())
        runs.append({"groups": groups, "recall": recall_of(all_scores), "mean_iou": _mean_iou(all_scores),
                     "extra": sum(score["extra"] for score in all_scores),
                     "empty_rate": sum(group["empty_rate"] * group["samples"] for group in groups.values()) / total_samples,
                     "truncated_rate": sum(group["truncated_rate"] * group["samples"] for group in groups.values()) / total_samples,
                     "segmentation_invalid_rate": sum(group["segmentation_invalid_rate"] * group["samples"]
                                                      for group in groups.values()) / total_samples})

    def matched(run: dict) -> dict:
        return {name: {index: group["scores"][index]["matched_labels"] for index in group["scored"]}
                for name, group in run["groups"].items()}

    identical, compared = pair_stability(sample_matches(runs[0]), sample_matches(runs[1]))
    total = len(sample_matches(runs[0]))

    evidence = Evidence("segment-image-quality", "test_zai_segmentation_quality")
    evidence.transport = {"runs": transports}
    evidence.parse = {"runs": parses}
    evidence.semantic = {"iou_threshold": IOU_THRESHOLD, "runs": runs}
    evidence.stability = {"recall": [run["recall"] for run in runs], "matched": [matched(run) for run in runs],
                          "compared": compared, "samples": total,
                          # 任一样本在任一次重复里没有输出行，就不算完全一致。
                          "identical_matched": compared == total and identical == total}
    evidence.write()

    assert all(not entry["errors"] for entry in transports)
    assert all(entry["exit_code"] == 0 for entry in parses)
    assert all(run["recall"] >= RECALL_FLOOR for run in runs)
    # 钩子路径确实走到了：对齐组有框，空数组组没有框。
    for run in runs:
        assert run["groups"]["aligned_reference_boxes"]["boxes"] > 0
        assert run["groups"]["hook_returns_empty"]["boxes"] == 0


def _mean_iou(scores: list[dict]) -> float | None:
    """@return 全部命中配对的平均 IoU；没有命中时为 None。"""
    matched = sum(score["matched"] for score in scores)
    return round(sum(score["iou_sum"] for score in scores) / matched, 4) if matched else None


# ── 第 5 道门：标注质量 ──────────────────────────────────────────────────────


def test_zai_annotate_quality(tmp_path):
    """[PENDING-EVIDENCE:segment-image-annotate-quality]：固定候选注入真实 annotate，切换指令后候选块不变。"""
    cases = load_fixtures()["annotate_cases"]
    root = injection_workspace(tmp_path)
    spies: list[Spy] = []
    repeats: list[list[dict]] = []
    candidate_blocks: dict[str, set[str | None]] = {case["name"]: set() for case in cases}
    for _repeat in range(REPEATS):
        spy = Spy()
        spies.append(spy)
        scores = []
        for case in cases:
            for prompt in case["prompts"]:
                score, block = run_case(root, case, prompt, spy)
                scores.append(score)
                candidate_blocks[case["name"]].add(block)
        repeats.append(scores)

    summaries = [selection_summary(scores) for scores in repeats]
    identical, compared = pair_stability(parsed_selections(repeats[0]), parsed_selections(repeats[1]))

    evidence = Evidence("segment-image-annotate-quality", "test_zai_annotate_quality")
    evidence.transport = {"runs": [spy.transport() for spy in spies]}
    evidence.parse = {"runs": [{"parsed": summary["parsed"], "cases": summary["cases"],
                                "errors": [score["errors"] for score in scores if score["errors"]]}
                               for summary, scores in zip(summaries, repeats)]}
    evidence.semantic = {"summary": summaries, "cases": repeats}
    evidence.stability = {"identical_selection": identical, "compared": compared, "cases": len(repeats[0])}
    evidence.write()

    # 同一候选集切换两套指令，候选块字节不变；两次重复也不变。
    assert all(len(blocks) == 1 and None not in blocks for blocks in candidate_blocks.values())
    for spy in spies:
        assert all(call.image_types == ["ImageRef", "RegionOverlayRef"] for call in spy.calls)
        assert all(call.error is None for call in spy.calls)
    for summary in summaries:
        assert summary["parsed"] == summary["cases"]
        assert summary["selection_accuracy"] >= SELECTION_FLOOR
        assert summary["label_accuracy"] >= LABEL_FLOOR

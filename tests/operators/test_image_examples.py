"""图片候选区域示例工程：钩子单测、keyless 检查与端到端离线运行（SPEC-image-region-segmentation §17.4，AC-04、AC-07、AC-13、AC-146）。

端到端用例走真实的 M1、摄取、阶段链、SchemaEngine、运行时与发射器，只在 ``LLMClient.complete`` 接缝上替身；
替身与 M9 一样在请求时加载图片字节，所以叠图真的按请求尺寸渲染。
"""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from PIL import Image

from labelkit.cli import main
from labelkit.common.config.model import CliOverrides
from labelkit.common.contracts.regions import RegionOverlayRef
from labelkit.common.contracts.types import ImageRef, Usage, scaled_size
from labelkit.common.errors import PostprocessorError
from labelkit.common.extensions.box_parser import invoke_box_parser
from labelkit.common.extensions.hooks import ResolvedHook
from labelkit.common.extensions.postprocessing import invoke_postprocessor
from labelkit.common.inference.llm_client import LLMClient, LLMResponse
from labelkit.orchestration import application

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "image-segmentation"
PROJECTS = ("project.toml", "project-boxes.toml", "project-uiautomator.toml", "project-accessibility.toml",
            "project-ocr.toml", "project-fixed.toml")
MARKER = "x-labelkit-region-ref"
UNKNOWN_REFERENCE = "unknown region reference"


def load_example_module(filename: str, name: str):
    """从示例工程路径装载一个独立 Python 模块。"""
    spec = importlib.util.spec_from_file_location(name, EXAMPLE / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def hooks():
    return load_example_module("hooks.py", "image_segmentation_example_hooks")


@pytest.fixture(scope="module")
def fixture_data() -> dict:
    return json.loads((EXAMPLE / "fixtures" / "fixed-candidates.json").read_text(encoding="utf-8"))


def parsed(function, companion: bytes, width: int, height: int):
    """经框架真实校验后返回参考框；钩子返回 None 时为 None。"""
    return invoke_box_parser(ResolvedHook("hooks.py:hook", function), companion, width, height)


# ── box_parser 钩子 ──────────────────────────────────────────────────────────


def test_uiautomator_and_ocr_hooks_produce_valid_boxes(hooks):
    """同一个框架校验接受两种完全不同的伴随格式。"""
    xml = (EXAMPLE / "data" / "screens-xml" / "image_1.xml").read_bytes()
    boxes = parsed(hooks.uiautomator_to_boxes, xml, 540, 960)
    assert [(box.box_id, box.bbox, box.text) for box in boxes] == [
        ("n1", (60, 300, 480, 380), "Button Sign in"),
        ("n2", (60, 420, 480, 500), "Button Register"),
        ("n3", (60, 540, 480, 600), "Button Forgot password"),
    ]
    shell = (EXAMPLE / "data" / "screens-xml" / "image_2.xml").read_bytes()
    assert parsed(hooks.uiautomator_to_boxes, shell, 540, 960) == ()

    ocr = (EXAMPLE / "data" / "documents" / "image_1.ocr.json").read_bytes()
    boxes = parsed(hooks.ocr_to_boxes, ocr, 800, 1000)
    assert [(box.box_id, box.bbox, box.text) for box in boxes] == [
        ("1", (60.5, 80.25, 420.75, 118.5), "Invoice 2041"),
        ("2", (60.5, 180.0, 512.25, 214.75), "Total due 318.40"),
    ]


def test_uiautomator_hook_policy_and_rejections(hooks):
    utf16 = ('<?xml version="1.0" encoding="UTF-16"?><hierarchy>'
             '<node class="a.b.TextView" text="登录" bounds="[-5,10][600,50]" />'
             '<node class="a.b.View" text="" clickable="true" bounds="[0,0][10,10]" />'
             '<node class="a.b.View" content-desc="说明" bounds="[0,0][10,10]" />'
             '<node class="a.b.View" text="看不见" bounds="[700,0][800,10]" /></hierarchy>').encode("utf-16")
    boxes = parsed(hooks.uiautomator_to_boxes, utf16, 540, 960)
    assert [(box.box_id, box.bbox, box.text) for box in boxes] == [
        ("n0", (0, 10, 540, 50), "TextView 登录"),      # 裁到画布内是钩子的策略
        ("n1", (0, 0, 10, 10), "View"),
        ("n2", (0, 0, 10, 10), "View 说明"),
    ]
    for rejected in (b"", b"not xml", b"<other/>", b'<hierarchy><node bounds="oops" /></hierarchy>',
                     b'<?xml version="1.0" encoding="foo"?><hierarchy/>',
                     b'<?xml version="1.0" encoding="shift_jis"?><hierarchy/>',
                     b'<hierarchy><node bounds="[--5,0][10,10]" text="a" /></hierarchy>',
                     f'<hierarchy><node bounds="[{"9" * 5000},0][10,10]" text="a" /></hierarchy>'.encode()):
        assert parsed(hooks.uiautomator_to_boxes, rejected, 540, 960) is None


SAME = 'bounds="[0,100][200,200]"'
LABELLED_OK = f'<node class="a.Button" text="OK" {SAME} />'
EMPTY_CLICKABLE = f'<node class="a.Image" clickable="true" {SAME} />'


@pytest.mark.parametrize(("nodes", "expected"), [
    pytest.param(f'<node class="a.Frame" clickable="true" {SAME}>{LABELLED_OK}</node>',
                 [("n1", "Button OK")], id="empty-wrapper-labelled-child"),
    pytest.param(f'<node class="a.View" content-desc="Save" {SAME}>{EMPTY_CLICKABLE}</node>',
                 [("n0", "View Save")], id="labelled-parent-empty-child"),
    pytest.param(f'<node class="a.View" text="Card" {SAME}>{LABELLED_OK}</node>',
                 [("n1", "Button OK")], id="both-labelled-keeps-innermost"),
    pytest.param(f'<node class="a.Frame" clickable="true" {SAME}><node class="a.Linear" clickable="true" {SAME}>'
                 f'{EMPTY_CLICKABLE}</node></node>',
                 [("n2", "Image")], id="none-labelled-keeps-innermost"),
    pytest.param(f'<node class="a.Frame" clickable="true" {SAME}><node class="a.Linear" {SAME}>{LABELLED_OK}</node></node>',
                 [("n2", "Button OK")], id="chain-through-filtered-node"),
    pytest.param(f'<node class="a.View" text="Card" {SAME}><node class="a.Frame" clickable="true" {SAME}>'
                 f'{LABELLED_OK}</node></node>',
                 [("n2", "Button OK")], id="labelled-empty-labelled"),
    pytest.param(f'<node class="a.Frame" clickable="true" {SAME}><node class="a.View" text="Card" {SAME}>'
                 f'{EMPTY_CLICKABLE}</node></node>',
                 [("n1", "View Card")], id="empty-labelled-empty"),
    pytest.param(f'<node class="a.Frame" clickable="true" bounds="[0,100][700,200]">'
                 f'<node class="a.Button" text="OK" bounds="[0,100][540,200]" /></node>',
                 [("n1", "Button OK")], id="same-after-clipping"),
])
def test_uiautomator_hook_keeps_one_box_per_nested_same_bbox_chain(hooks, nodes, expected):
    """同一条父子链上 bbox 相同的框只留一个：有文字的优先，都有或都没有时留最内层。"""
    boxes = parsed(hooks.uiautomator_to_boxes, f"<hierarchy>{nodes}</hierarchy>".encode(), 540, 960)
    assert [(box.box_id, box.text) for box in boxes] == expected


def test_uiautomator_hook_keeps_boxes_that_are_not_nested_duplicates(hooks):
    """兄弟节点不算嵌套，框不同的父子节点也不算同框，都留下。"""
    siblings = f'<node class="a.Frame" clickable="true" {SAME}>{LABELLED_OK}<node class="a.Text" text="B" {SAME} /></node>'
    boxes = parsed(hooks.uiautomator_to_boxes, f"<hierarchy>{siblings}</hierarchy>".encode(), 540, 960)
    assert [(box.box_id, box.text) for box in boxes] == [("n1", "Button OK"), ("n2", "Text B")]

    # 兄弟的先后不改变结果：有文字的外层被有文字的内层淘汰，空内层也不留。
    for children, expected in ((f"{EMPTY_CLICKABLE}{LABELLED_OK}", "n2"), (f"{LABELLED_OK}{EMPTY_CLICKABLE}", "n1")):
        mixed = f'<node class="a.View" text="Card" {SAME}>{children}</node>'
        boxes = parsed(hooks.uiautomator_to_boxes, f"<hierarchy>{mixed}</hierarchy>".encode(), 540, 960)
        assert [(box.box_id, box.text) for box in boxes] == [(expected, "Button OK")]

    different = f'<node class="a.Frame" text="Card" bounds="[0,0][300,300]">{LABELLED_OK}</node>'
    boxes = parsed(hooks.uiautomator_to_boxes, f"<hierarchy>{different}</hierarchy>".encode(), 540, 960)
    assert [box.box_id for box in boxes] == ["n0", "n1"]


def test_accessibility_hook_produces_valid_boxes(hooks):
    data = (EXAMPLE / "data" / "screens-json" / "image_1.json").read_bytes()
    boxes = parsed(hooks.accessibility_to_boxes, data, 540, 960)
    assert [(box.box_id, box.bbox, box.text) for box in boxes] == [
        ("a1", (60, 300, 480, 380), "button Sign in"),
        ("a2", (60, 420, 480, 500), "button Register"),
    ]
    nested = {"root": {"role": "window", "name": "壳", "rect": {"x": 0, "y": 0, "width": 100, "height": 100},
                       "children": [{"role": "list", "name": "", "rect": {"x": 0, "y": 0, "width": 50, "height": 50},
                                     "children": [{"role": "item", "name": "甲", "children": [],
                                                   "rect": {"x": 1, "y": 2, "width": 3, "height": 4}}]},
                                    {"role": "item", "name": "乙", "children": [],
                                     "rect": {"x": 5, "y": 6, "width": 7, "height": 8}}]}}
    boxes = parsed(hooks.accessibility_to_boxes, json.dumps(nested).encode("utf-8"), 100, 100)
    assert [(box.box_id, box.bbox) for box in boxes] == [
        ("a1", (0, 0, 100, 100)), ("a2", (1, 2, 4, 6)), ("a3", (5, 6, 12, 14))]
    for rejected in (b"", b"{}", b"[]", b'{"root": {"role": "x"}}', b"\xff\xfe"):
        assert parsed(hooks.accessibility_to_boxes, rejected, 540, 960) is None


def accessibility_node(role: str, name: str, *children: dict, rect: tuple = (0, 100, 200, 100)) -> dict:
    """组一个无障碍树节点；rect 为 (x, y, width, height)，默认都是同一个框。"""
    x, y, width, height = rect
    return {"role": role, "name": name, "rect": {"x": x, "y": y, "width": width, "height": height},
            "children": list(children)}


@pytest.mark.parametrize(("root", "expected"), [
    pytest.param(accessibility_node("group", "", accessibility_node("button", "OK")),
                 [("a1", "button OK")], id="empty-wrapper-labelled-child"),
    pytest.param(accessibility_node("button", "Save", accessibility_node("image", "")),
                 [("a1", "button Save")], id="labelled-parent-empty-child"),
    pytest.param(accessibility_node("group", "Card", accessibility_node("button", "OK")),
                 [("a1", "button OK")], id="both-labelled-keeps-innermost"),
    pytest.param(accessibility_node("group", "Card", accessibility_node("group", "", accessibility_node("button", "OK"))),
                 [("a1", "button OK")], id="chain-through-filtered-node"),
    pytest.param(accessibility_node("group", "Card", accessibility_node("text", "A"), accessibility_node("text", "B")),
                 [("a1", "text A"), ("a2", "text B")], id="siblings-not-merged"),
    pytest.param(accessibility_node("group", "Card", accessibility_node("text", "A", rect=(0, 100, 50, 50))),
                 [("a1", "group Card"), ("a2", "text A")], id="different-bbox-kept"),
])
def test_accessibility_hook_keeps_one_box_per_nested_same_bbox_chain(hooks, root, expected):
    """无障碍钩子只收有名称的节点，同框嵌套时留最内层；框名按留下的框连续编号。"""
    boxes = parsed(hooks.accessibility_to_boxes, json.dumps({"root": root}).encode("utf-8"), 540, 960)
    assert [(box.box_id, box.text) for box in boxes] == expected


def test_ocr_hook_rejects_foreign_data(hooks):
    for rejected in (b"", b"{}", b"not json", b'{"lines": [{"text": "a"}]}', b'{"lines": 3}'):
        assert parsed(hooks.ocr_to_boxes, rejected, 800, 1000) is None
    assert parsed(hooks.ocr_to_boxes, b'{"lines": []}', 800, 1000) == ()


# ── 后处理钩子 ───────────────────────────────────────────────────────────────


def context_of(fixture_data: dict) -> dict:
    return {"sample_id": "s", "source": {"file": "image_1.png", "pair_index": 1}, "image": fixture_data["image"],
            "boxes": [], "regions": fixture_data["expected_items"]}


def test_keep_polygons_and_bounding_box_postprocessors(hooks, fixture_data):
    """两种写法都过严格 JSON 边界：几何保留孔洞与部件，外接矩形只看外环。"""
    expected = fixture_data["expected_items"]
    candidate = {"items": [{"region_id": "r2", "label": "双块"}, {"region_id": "r1", "label": "环"}]}
    snapshot = json.loads(json.dumps(candidate))

    kept = invoke_postprocessor(ResolvedHook("hooks.py:keep", hooks.keep_polygons), candidate, context_of(fixture_data))
    assert kept == {"items": [{"region_id": "r2", "label": "双块", "geometry": expected[1]["geometry"]},
                              {"region_id": "r1", "label": "环", "geometry": expected[0]["geometry"]}]}
    assert kept["items"][1]["geometry"]["coordinates"][1], "hole is preserved"

    boxed = invoke_postprocessor(ResolvedHook("hooks.py:box", hooks.to_bounding_boxes), candidate,
                                 context_of(fixture_data))
    width, height = fixture_data["image"]["width"], fixture_data["image"]["height"]
    assert boxed["items"][0]["bbox"] == [560 * width / 1000, 100 * height / 1000, 930 * width / 1000, 880 * height / 1000]
    assert boxed["items"][1]["bbox"] == [100 * width / 1000, 120 * height / 1000, 480 * width / 1000, 500 * height / 1000]
    assert all(type(item["bbox"]) is list for item in boxed["items"])
    assert candidate == snapshot


@pytest.mark.parametrize("name", ["keep_polygons", "to_bounding_boxes"])
def test_postprocessors_handle_probe_without_sample(hooks, name):
    """探针路径没有样本：不虚构几何，原样返回。"""
    candidate = {"items": [{"region_id": "r1", "label": "x"}]}
    result = invoke_postprocessor(ResolvedHook(f"hooks.py:{name}", getattr(hooks, name)), candidate, None)
    assert result == candidate


@pytest.mark.parametrize("name", ["keep_polygons", "to_bounding_boxes"])
def test_postprocessor_unknown_id_is_a_program_error(hooks, fixture_data, name):
    """引用检查漏掉的情形在钩子里是程序错误，不是模型修复。"""
    with pytest.raises(PostprocessorError):
        invoke_postprocessor(ResolvedHook(f"hooks.py:{name}", getattr(hooks, name)),
                             {"items": [{"region_id": "ghost", "label": "x"}]}, context_of(fixture_data))


# ── 工程与夹具 ───────────────────────────────────────────────────────────────


def test_prepare_regenerates_text_fixtures_byte_for_byte(tmp_path, monkeypatch):
    """伴随文件与夹具的文本字节由 prepare.py 决定；图片字节依赖 Pillow 构建，只比较尺寸。"""
    prepare = load_example_module("prepare.py", "image_segmentation_example_prepare")
    monkeypatch.setattr(prepare, "ROOT", tmp_path)
    monkeypatch.setattr(prepare, "DATA", tmp_path / "data")
    prepare.main()
    generated = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*") if path.is_file())
    committed = sorted(path.relative_to(EXAMPLE).as_posix() for directory in ("data", "fixtures")
                       for path in (EXAMPLE / directory).rglob("*") if path.is_file())
    assert generated == committed
    for relative in generated:
        if relative.endswith(".png"):
            with Image.open(tmp_path / relative) as fresh, Image.open(EXAMPLE / relative) as stored:
                assert fresh.size == stored.size
        else:
            assert (tmp_path / relative).read_bytes() == (EXAMPLE / relative).read_bytes()


def test_fixture_expected_geometry_uses_the_projection_formula(fixture_data):
    """期望几何等于网格坐标按 x*W/1000、y*H/1000 投影，夹具自身没有写错。"""
    width, height = fixture_data["image"]["width"], fixture_data["image"]["height"]
    for region, item in zip(fixture_data["model_output"]["regions"], fixture_data["expected_items"], strict=True):
        parts = [[polygon["exterior"], *polygon["holes"]] for polygon in region["polygons"]]
        pixels = [[[[x * width / 1000, y * height / 1000] for x, y in ring] for ring in rings] for rings in parts]
        coordinates = item["geometry"]["coordinates"]
        assert (coordinates if item["geometry"]["type"] == "MultiPolygon" else [coordinates]) == pixels


@pytest.fixture(autouse=True)
def restore_logging():
    """main 的 setup_logging 会把 handler 绑到被捕获的 stderr，用完摘掉。"""
    logger = logging.getLogger("labelkit")
    saved = logger.handlers[:]
    yield
    logger.handlers = saved


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Path:
    """把示例工程复制到临时目录并切进去；运行产物不落进仓库。"""
    project = tmp_path / "image-segmentation"
    shutil.copytree(EXAMPLE, project, ignore=shutil.ignore_patterns("out", "__pycache__"))
    shutil.copy(ROOT / "examples" / "config.toml", tmp_path / "config.toml")
    (project / "out").mkdir()
    monkeypatch.chdir(project)
    return project


@pytest.mark.parametrize("project", PROJECTS)
def test_all_image_projects_validate_and_dry_run(workspace, monkeypatch, capsys, project):
    """无凭据：全部工程 validate 与 --dry-run 通过，且不写主输出。"""
    for name in [name for name in os.environ if name.startswith("LABELKIT_")]:
        monkeypatch.delenv(name)
    assert main(["validate", "--config", "../config.toml", "--project", project]) == 0
    assert "configuration valid" in capsys.readouterr().err
    assert main(["run", "--config", "../config.toml", "--project", project, "--dry-run", "--console", "plain"]) == 0
    assert "dry-run: estimated LLM calls" in capsys.readouterr().err
    assert not [path for path in (workspace / "out").iterdir() if path.name.endswith(".jsonl")]


# ── 端到端离线运行 ───────────────────────────────────────────────────────────


@dataclass
class LoadedImage:
    source: object      # ImageRef 或 RegionOverlayRef
    media_type: str
    picture: Image.Image


@dataclass
class Call:
    profile: str
    prompt: object
    schema: dict
    images: list[LoadedImage]

    @property
    def kind(self) -> str:
        keys = set(self.schema["properties"])
        if "regions" in keys:
            return "segment"
        if "items" in keys:
            return "annotate"
        return "verify" if "verdict" in keys else "classify"

    @property
    def texts(self) -> list[str]:
        return [part.text for message in self.prompt.messages for part in message.parts if part.kind == "text"]

    @property
    def is_repair(self) -> bool:
        return any(UNKNOWN_REFERENCE in text for text in self.texts)

    def block(self, label: str) -> str | None:
        """取 ``label`` 标签下一行的内容；块不存在时为 None。"""
        for text in self.texts:
            lines = text.split("\n")
            if label in lines:
                return lines[lines.index(label) + 1]
        return None

    @property
    def candidates(self) -> list[dict]:
        return json.loads(self.block("[候选区域]"))

    @property
    def image_name(self) -> str:
        return Path(self.images[0].source.path).name

    @property
    def directory(self) -> str:
        return Path(self.images[0].source.path).parent.name


class Scripted:
    """``LLMClient.complete`` 接缝的进程内替身：与 M9 同样解析工作点并惰性取字节。"""

    def __init__(self, respond, usage=None):
        self.respond = respond
        self.usage = usage or (lambda call: Usage(10, 5))
        self.calls: list[Call] = []

    def client_type(self) -> type:
        scripted = self

        class Client(LLMClient):
            async def complete(self, profile, prompt, response_schema=None):
                settings = self._llm_profiles[profile]
                px = min(prompt.image_px or settings.default_image_px or settings.max_image_px, settings.max_image_px)
                images = []
                for message in prompt.messages:
                    for part in message.parts:
                        if part.kind == "image":
                            media_type, body = part.image.load_base64(px)
                            picture = Image.open(io.BytesIO(base64.b64decode(body)))
                            picture.load()
                            images.append(LoadedImage(part.image, media_type, picture))
                call = Call(profile, prompt, response_schema, images)
                scripted.calls.append(call)
                return LLMResponse(text=json.dumps(scripted.respond(call), ensure_ascii=False), structured=None,
                                   usage=scripted.usage(call), model="offline", latency_ms=0, finish="end_turn")

        return Client

    def of(self, kind: str) -> list[Call]:
        return [call for call in self.calls if call.kind == kind]


def run_project(monkeypatch, project: str, respond) -> Scripted:
    scripted = Scripted(respond)
    monkeypatch.setattr(application, "LLMClient", scripted.client_type())
    monkeypatch.setattr(application, "setup_logging", lambda cfg: None)
    monkeypatch.setenv("LABELKIT_ZAI_KEY", "offline-test-key")
    code = application.execute_run(Path("../config.toml"), Path(project), CliOverrides(console="plain"))
    assert code == 0
    # 全部请求：注解不上行到 Schema，也不进提示词。
    for call in scripted.calls:
        assert MARKER not in json.dumps(call.schema)
        assert all(MARKER not in text for text in call.texts)
    return scripted


def rows_of(workspace: Path, name: str) -> dict[int, dict]:
    lines = (workspace / "out" / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]
    return {row["_meta"]["source"]["pair_index"]: row for row in rows}


def report_of(workspace: Path, name: str) -> dict:
    return json.loads((workspace / "out" / f"{name}.report.json").read_text(encoding="utf-8"))


def rectangle(left, top, right, bottom) -> dict:
    return {"type": "Polygon", "coordinates": [[[left, top], [right, top], [right, bottom], [left, bottom], [left, top]]]}


def grid_point(x: int, y: int, size: tuple[int, int]) -> tuple[int, int]:
    return x * size[0] // 1000, y * size[1] // 1000


def test_fixed_project_preserves_fixture_geometry_through_overlay_and_postprocess(workspace, monkeypatch, fixture_data):
    """孔洞与多部件贯通真实链路：缩放请求、叠图、引用修复、后处理、_meta.regions 都逐值等于夹具。"""
    expected = fixture_data["expected_items"]
    good = {"items": [{"region_id": region_id, "label": label}
                      for region_id, label in (("r1", "环"), ("r2", "双块"), ("r3", "三角"))]}

    def respond(call):
        if call.kind == "segment":
            return fixture_data["model_output"]
        return good if call.is_repair else {"items": [{"region_id": "ghost-999", "label": "错"}]}

    scripted = run_project(monkeypatch, "project-fixed.toml", respond)
    row = rows_of(workspace, "fixed")[1]

    # 输出：候选与最终标注的几何都是夹具值，不取整，顶点与环序不变。
    regions = row["_meta"]["regions"]
    assert regions == {"image": fixture_data["image"], "boxes": [], "items": expected}
    assert [item["geometry"] for item in row["items"]] == [item["geometry"] for item in expected]
    assert row["items"][0]["geometry"]["type"] == "Polygon" and len(row["items"][0]["geometry"]["coordinates"]) == 2
    assert row["items"][1]["geometry"]["type"] == "MultiPolygon"
    assert (row["_meta"]["source"]["file"], row["_meta"]["source"]["pair_index"]) == ("image_1.png", 1)
    assert row["_meta"]["stream"] is None
    assert report_of(workspace, "fixed")["counts"]["emitted"] == 1

    # 请求：segment 只有原图；annotate 首轮与修复轮都是原图加叠图，按同一工作点缩放。
    target = scaled_size(fixture_data["image"]["width"], fixture_data["image"]["height"], 2048)
    assert target == (2048, 1475)
    segment, = scripted.of("segment")
    assert [type(image.source) for image in segment.images] == [ImageRef]
    assert segment.images[0].picture.size == target
    first, repair = scripted.of("annotate")
    for call in (first, repair):
        assert [type(image.source) for image in call.images] == [ImageRef, RegionOverlayRef]
        assert [image.picture.size for image in call.images] == [target, target]
        assert call.images[1].media_type == "image/png" and call.images[1].picture.mode == "RGB"
    assert first.candidates == repair.candidates
    assert [candidate["region_id"] for candidate in first.candidates] == ["r1", "r2", "r3"]

    # 修复：违规不回显模型给出的值，后处理没有在未知引用上运行（否则记录失败）。
    violations = [line for text in repair.texts for line in text.split("\n") if UNKNOWN_REFERENCE in line]
    assert violations == ["1. /items/0/region_id: unknown region reference"]

    # 叠图：区域内像素被着色，孔洞内与区域外保持原样。
    base = first.images[0].picture.convert("RGB")
    overlay = first.images[1].picture
    for grid in ((290, 310), (950, 50), (530, 950)):
        point = grid_point(*grid, target)
        assert overlay.getpixel(point) == base.getpixel(point), grid
    for grid in ((120, 140), (650, 200), (800, 700), (295, 700)):
        point = grid_point(*grid, target)
        assert overlay.getpixel(point) != base.getpixel(point), grid


def test_boxes_project_runs_without_a_segment_call(workspace, monkeypatch):
    """D15：参考框直接成为候选；外壳样本得到合法空候选，不判负。"""

    def respond(call):
        assert call.kind == "annotate"
        return {"items": [{"region_id": candidate["region_id"], "label": "控件"} for candidate in call.candidates]}

    scripted = run_project(monkeypatch, "project-boxes.toml", respond)
    assert scripted.of("segment") == []
    assert sorted(call.image_name for call in scripted.calls) == ["image_1.png", "image_2.png"]
    # 每个候选本身就是参考框，模型路径才有的 [参考框] 块被省略。
    assert all(call.block("[参考框]") is None for call in scripted.calls)
    rows = rows_of(workspace, "boxes")
    assert report_of(workspace, "boxes")["counts"]["emitted"] == 2

    full = rows[1]
    assert [item["region_id"] for item in full["_meta"]["regions"]["items"]] == ["n1", "n2", "n3"]
    assert full["_meta"]["regions"]["items"][0] == {
        "region_id": "n1", "geometry": rectangle(60, 300, 480, 380), "box_id": "n1", "source_box_ids": ["n1"]}
    assert [box["id"] for box in full["_meta"]["regions"]["boxes"]] == ["n1", "n2", "n3"]
    assert full["items"][0] == {"region_id": "n1", "label": "控件", "bbox": [60, 300, 480, 380]}
    assert full["items"][2]["bbox"] == [60, 540, 480, 600]

    shell = rows[2]
    assert shell["_meta"]["regions"] == {"image": {"width": 540, "height": 960}, "boxes": [], "items": []}
    assert shell["items"] == []
    assert not (workspace / "out" / "boxes.rejects.jsonl").read_text(encoding="utf-8").strip()


def test_manual_trace_example_is_what_the_boxes_project_emits(workspace, monkeypatch):
    """手册 28.10 声称示例来自 project-boxes.toml：逐字段与这个工程的真实事件比较，annotate 替身碰不到 segment。"""
    project = workspace / "project-boxes.toml"
    enabled = '[trace]\nenabled = true\nchannels = ["segment"]'
    project.write_text(project.read_text(encoding="utf-8").replace("[trace]\nenabled = false", enabled), encoding="utf-8")
    run_project(monkeypatch, "project-boxes.toml", lambda call: {"items": []})
    lines = (workspace / "out" / "boxes.trace.jsonl").read_text(encoding="utf-8").splitlines()
    emitted = [event for event in map(json.loads, lines) if event["ev"] == "segment.regions"]
    assert len(emitted) == 2

    section = (ROOT / "docs" / "manual" / "28-image-segmentation.md").read_text(encoding="utf-8").split("## 28.10 ")[1]
    example = next(block for block in section.split("```json\n")[1:] if '"segment.regions"' in block).split("```")[0]
    decoder, documented, position = json.JSONDecoder(), [], 0
    while position < len(example.rstrip()):
        event, end = decoder.raw_decode(example, position)
        documented.append(event)
        position = end + 1
    assert documented == [{key: event[key] for key in documented[0]} for event in emitted]


def test_uiautomator_project_shares_candidates_through_verify_repair(workspace, monkeypatch):
    """segment、annotate、verify 共用同一候选：未成为候选的框名可修复，verify 返工不重新分割。"""
    polygon = {"exterior": [[100, 100], [300, 100], [300, 200], [100, 100]], "holes": []}
    judged: list[str] = []
    annotated: list[Call] = []

    def respond(call):
        if call.kind == "segment":
            if call.image_name == "image_1.png":
                return {"regions": [{"box_id": "n1", "polygons": None, "source_box_ids": None},
                                    {"box_id": None, "polygons": [polygon], "source_box_ids": ["n2", "n2"]}]}
            return {"regions": [{"box_id": None, "polygons": [polygon], "source_box_ids": None}]}
        if call.kind == "verify":
            judged.append(call.image_name)
            fail = call.image_name == "image_1.png" and judged.count("image_1.png") == 1
            return {"verdict": "fail" if fail else "pass", "critiques": [{"aspect": "选择", "opinion": "看过了"}]}
        if call.image_name == "image_1.png":
            annotated.append(call)
            if len(annotated) == 1:
                return {"items": [{"region_id": "n2", "label": "没有成为候选的框"}]}   # n2 只是参考框
            return {"items": [{"region_id": "n1", "label": "登录"}, {"region_id": "r1", "label": "新画"}]}
        return {"items": [{"region_id": "r1", "label": "外壳"}]}

    scripted = run_project(monkeypatch, "project-uiautomator.toml", respond)

    assert len(scripted.of("segment")) == 2
    rows = rows_of(workspace, "uiautomator")
    first = rows[1]
    assert [item["region_id"] for item in first["_meta"]["regions"]["items"]] == ["n1", "r1"]
    assert first["_meta"]["regions"]["items"][1]["source_box_ids"] == ["n2"]
    assert first["items"][0]["geometry"] == rectangle(60, 300, 480, 380)
    assert first["items"][1]["geometry"]["coordinates"][0][1] == [300 * 540 / 1000, 100 * 960 / 1000]
    assert first["_meta"]["verification"]["rounds"] == 2

    # 所有 annotate 与 verify 请求看到同一候选块，叠图引用同一个 RegionSet 对象。
    material = [call for call in scripted.calls if call.kind in ("annotate", "verify") and call.image_name == "image_1.png"]
    assert {call.kind for call in material} == {"annotate", "verify"}
    assert len({call.block("[候选区域]") for call in material}) == 1
    assert len({id(call.images[1].source.regions) for call in material}) == 1
    assert {call.block("[参考框]") is not None for call in material} == {True}

    # 引用 n2 的那一轮进入修复，违规不回显框名。
    repair = next(call for call in material if call.kind == "annotate" and call.is_repair)
    assert [line for text in repair.texts for line in text.split("\n") if UNKNOWN_REFERENCE in line] == [
        "1. /items/0/region_id: unknown region reference"]

    # 外壳样本：钩子返回 []，模型照常依据图片生成候选。
    assert rows[2]["_meta"]["regions"]["boxes"] == []
    assert rows[2]["items"][0]["region_id"] == "r1"
    assert report_of(workspace, "uiautomator")["counts"]["emitted"] == 2


def test_accessibility_project_routes_class_view_reference_check(workspace, monkeypatch):
    """经 classify 后取类视图的路径与模型 Schema；classify 不接收候选。"""

    def respond(call):
        if call.kind == "classify":
            return {"class": "account"}
        if call.kind == "segment":
            assert call.block("[参考框]") is not None
            return {"regions": [{"box_id": "a2", "polygons": None, "source_box_ids": None}]}
        return {"items": [{"region_id": "a2" if call.is_repair else "a1", "label": "注册"}]}

    scripted = run_project(monkeypatch, "project-accessibility.toml", respond)
    classify, = scripted.of("classify")
    assert [type(image.source) for image in classify.images] == [ImageRef]
    assert classify.block("[候选区域]") is None
    assert len(scripted.of("annotate")) == 2
    row = rows_of(workspace, "accessibility")[1]
    assert row["_meta"]["classification"]["labels"] == ["account"]
    assert row["items"][0]["geometry"] == rectangle(60, 420, 480, 500)
    assert [box["id"] for box in row["_meta"]["regions"]["boxes"]] == ["a1", "a2"]


def test_ocr_project_outputs_rectangles_with_autonumbered_names(workspace, monkeypatch):
    """框架补名是字符串，出现在提示词、候选与输出里；转矩形后原多边形留在 _meta.regions。"""

    def respond(call):
        if call.kind == "segment":
            return {"regions": [{"box_id": "2", "polygons": None, "source_box_ids": None}]}
        return {"items": [{"region_id": "1" if not call.is_repair else "2", "label": "total"}]}

    scripted = run_project(monkeypatch, "project-ocr.toml", respond)
    segment, = scripted.of("segment")
    assert json.loads(segment.block("[参考框]")) == {"box_id": "1", "bbox": [75, 80, 526, 119], "text": "Invoice 2041"}
    row = rows_of(workspace, "ocr")[1]
    assert row["items"] == [{"region_id": "2", "label": "total", "bbox": [60.5, 180.0, 512.25, 214.75]}]
    assert row["_meta"]["regions"]["boxes"] == [
        {"id": "1", "bbox": [60.5, 80.25, 420.75, 118.5], "text": "Invoice 2041"},
        {"id": "2", "bbox": [60.5, 180.0, 512.25, 214.75], "text": "Total due 318.40"},
    ]
    assert row["_meta"]["regions"]["items"] == [{
        "region_id": "2", "geometry": rectangle(60.5, 180.0, 512.25, 214.75), "box_id": "2", "source_box_ids": ["2"]}]


def test_pure_image_project_keeps_drawn_polygons(workspace, monkeypatch):
    """纯图片：没有伴随文件也没有参考框，模型新画的多边形按出现顺序编号。"""

    def respond(call):
        if call.kind == "segment":
            assert call.block("[参考框]") is None
            assert [type(image.source) for image in call.images] == [ImageRef]
            return {"regions": [{"box_id": None, "source_box_ids": None,
                                 "polygons": [{"exterior": [[0, 0], [500, 0], [500, 500], [0, 0]], "holes": []}]}]}
        assert [type(image.source) for image in call.images] == [ImageRef, RegionOverlayRef]
        return {"items": [{"region_id": "r1", "label": "物体"}]}

    scripted = run_project(monkeypatch, "project.toml", respond)
    assert len(scripted.of("segment")) == 2 and len(scripted.of("annotate")) == 2
    rows = rows_of(workspace, "photos")
    assert sorted(rows) == [1, 2]
    for row in rows.values():
        assert row["_meta"]["regions"]["boxes"] == []
        assert row["items"][0]["geometry"]["coordinates"] == [[[0.0, 0.0], [450.0, 0.0], [450.0, 300.0], [0.0, 0.0]]]

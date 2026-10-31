"""M2 图片模式发现、计数、记录 id 与文件头检查（SPEC-image-region-segmentation §8）。

全部走真实文件与 Pillow，没有 LLM。``test_non_image_counts_unchanged`` 把 ``examples/ui`` 当回归锚点。
"""
from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageFile

from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ConsoleConfig,
    DedupConfig,
    ExtractConfig,
    GenerateConfig,
    InputConfig,
    OutputConfig,
    QualityConfig,
    ResolvedConfig,
    ResolvedPaths,
    Rubric,
    RunConfig,
    SegmentConfig,
    StitchConfig,
    StreamConfig,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)
from labelkit.common.errors import InputError
from labelkit.operators.ingest import Ingestor
from tests.common.config.test_segment_mode_config import BOXES, Env

REPO_ROOT = Path(__file__).resolve().parents[2]
PIXELS_ONLY = SegmentConfig(enabled=True, mode="image", instruction="生成候选")
HOOK = {"box_parser": "hooks.py:to_boxes", "companion_suffix": ".xml"}
BOXES_ONLY = SegmentConfig(enabled=True, mode="image", **HOOK)
MODEL_AND_BOXES = SegmentConfig(enabled=True, mode="image", instruction="生成候选", **HOOK)
WARN_TEXT = "image mode: ignored {} file(s) that are neither images nor declared companion files"


def make_cfg(tmp_path: Path, segment: SegmentConfig = PIXELS_ONLY, root: Path | None = None,
             **input_kw) -> ResolvedConfig:
    """显式构造图片模式配置，输入根默认是 ``tmp_path/in``。"""
    root = root if root is not None else tmp_path / "in"
    output = str(tmp_path / "out.jsonl")
    return ResolvedConfig(
        tool=ToolConfig(),
        console=ConsoleConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output=output, modality="ui", input=str(root)),
        input=InputConfig(**input_kw),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=segment,
        stitch=StitchConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(),
        quality=QualityConfig(enabled=False),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=TraceConfig(),
        rubric=Rubric(name="t", criteria=()),
        class_views={},
        user_schema={"type": "object"},
        limit=None,
        strict=False,
        dry_run=False,
        config_path="config.toml",
        project_path="project.toml",
        config_digest="sha256:0",
        project_digest="sha256:0",
        paths=ResolvedPaths(
            project=str(tmp_path / "project.toml"), project_root=str(tmp_path), input=str(root),
            output=output, report=str(tmp_path / "out.report.json"),
            rejects=str(tmp_path / "out.rejects.jsonl"), sidecar=None, trace=None, stream=None,
            manifest=None, failed_report=None),
    )


class EventRecorder:
    """记录摄取期 trace 事件。"""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None):
        assert (stage, batch_no) == ("ingest", 0)
        self.events.append((ev, dict(payload or {})))


def png(path: Path, color: str = "white", size: tuple[int, int] = (4, 4)) -> bytes:
    """写一张合法 PNG 并返回文件字节。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return path.read_bytes()


def write(path: Path, data: bytes = b"<hierarchy/>") -> bytes:
    """写一个任意内容的文件并返回字节。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def ingest(cfg: ResolvedConfig) -> tuple[list, Ingestor]:
    """跑完整条记录流。"""
    ingestor = Ingestor(cfg)
    ingestor.metrics = EventRecorder()
    return list(ingestor.records()), ingestor


# ── AC-01 / AC-05 / AC-42：图片必需，伴随文件可选 ───────────────────────────


def test_image_only_directory_ingests_without_companion(tmp_path):
    first = png(tmp_path / "in" / "image_1.png", "red")
    second = png(tmp_path / "in" / "image_2.png", "blue")
    records, ingestor = ingest(make_cfg(tmp_path))
    assert [record.ref.pair_index for record in records] == [1, 2]
    for record, data in zip(records, (first, second)):
        assert record.modality == "ui"
        assert record.kind == "single"
        assert record.ui_tree is None and record.raw is None and record.text is None
        assert record.image.format == "png" and record.image.size_bytes == len(data)
        assert record.companion is None and record.companion_path is None
    report = ingestor.report
    assert (report.scanned, report.ingested, report.bad_input, report.missing_pair) == (2, 2, 0, 0)
    assert ingestor.metrics.events == []


def test_boxes_only_missing_companion_counts_missing_pair(tmp_path):
    root = tmp_path / "in"
    png(root / "image_1.png")
    write(root / "image_1.xml")
    png(root / "image_2.png", "blue")
    records, ingestor = ingest(make_cfg(tmp_path, BOXES_ONLY))
    assert [record.ref.pair_index for record in records] == [1]
    report = ingestor.report
    assert (report.scanned, report.ingested, report.missing_pair, report.bad_input) == (2, 1, 1, 1)
    assert report.bad_locations == [{
        "file": "image_2.png", "line_no": None, "index": 2,
        "reason": "missing pair: only the image side file is present"}]
    assert ingestor.metrics.events == [
        ("ingest.missing_pair", {"index": 2, "present": "image", "file": "image_2.png"})]


def test_boxes_only_missing_companion_fail_policy_fails_run(tmp_path):
    png(tmp_path / "in" / "image_1.png")
    cfg = make_cfg(tmp_path, BOXES_ONLY, on_missing_pair="fail")
    with pytest.raises(InputError, match="index=1 has only the image side"):
        Ingestor(cfg).scan(estimate=False)
    with pytest.raises(InputError, match="index=1 has only the image side"):
        list(Ingestor(cfg).records())


def test_boxes_only_all_missing_has_no_valid_records(tmp_path):
    png(tmp_path / "in" / "image_1.png")
    with pytest.raises(InputError, match="no valid records.*missing_pair=1"):
        list(Ingestor(make_cfg(tmp_path, BOXES_ONLY)).records())


def test_instruction_path_missing_companion_is_legal(tmp_path):
    image = png(tmp_path / "in" / "image_1.png")
    cfg = make_cfg(tmp_path, MODEL_AND_BOXES)
    assert Ingestor(cfg).scan().pairs == ((1, None, "image_1.png"),)
    records, ingestor = ingest(cfg)
    assert records[0].companion is None
    assert records[0].id == hashlib.sha256(image).hexdigest()[:16]
    assert (ingestor.report.missing_pair, ingestor.report.bad_input) == (0, 0)


# ── AC-19：跨子目录配对与两类异常 ───────────────────────────────────────────


def test_companion_pairs_across_subdirectories_by_index(tmp_path):
    root = tmp_path / "in"
    png(root / "shots" / "image_007.png")
    companion = write(root / "dumps" / "deep" / "image_7.xml", b"<seven/>")
    png(root / "shots" / "image_8.png", "blue")
    write(root / "image_08.xml", b"<eight/>")
    records, ingestor = ingest(make_cfg(tmp_path, BOXES_ONLY))
    assert [record.ref.pair_index for record in records] == [7, 8]
    assert records[0].companion == companion
    # 与 str(record.image.path) 同形：都由 M1 冻结的输入根拼出。
    assert Path(records[0].companion_path) == Path(str(root / "dumps" / "deep" / "image_7.xml"))
    assert records[0].companion_path == str(records[0].image.path.parent.parent / "dumps" / "deep" / "image_7.xml")
    assert records[1].companion == b"<eight/>"
    assert ingestor.report.missing_pair == 0


@pytest.mark.parametrize("names", [
    ("image_1.png", "image_1.jpg"),
    ("a/image_1.png", "b/image_1.png"),
    ("image_1.png", "b/image_01.PNG"),
])
def test_duplicate_image_index_is_conflict(tmp_path, names):
    root = tmp_path / "in"
    for name in names:
        png(root / name) if name.lower().endswith("png") else write(root / name, b"\xff\xd8\xff")
    png(root / "image_2.png", "blue")
    with pytest.raises(InputError, match="index=1 matches multiple files"):
        Ingestor(make_cfg(tmp_path)).scan(estimate=False)
    records, ingestor = ingest(make_cfg(tmp_path, on_index_conflict="skip"))
    assert [record.ref.pair_index for record in records] == [2]
    report = ingestor.report
    assert (report.scanned, report.index_conflict, report.bad_input, report.ingested) == (2, 1, 1, 1)
    assert ingestor.metrics.events[0][0] == "ingest.index_conflict"
    assert sorted(ingestor.metrics.events[0][1]["files"]) == sorted(names)


def test_duplicate_companion_index_is_conflict(tmp_path):
    root = tmp_path / "in"
    png(root / "image_1.png")
    write(root / "a" / "image_1.xml")
    write(root / "b" / "image_1.xml")
    with pytest.raises(InputError, match="index=1 matches multiple files"):
        Ingestor(make_cfg(tmp_path, MODEL_AND_BOXES)).scan(estimate=False)
    with pytest.raises(InputError, match="no valid records.*index_conflict=1"):
        list(Ingestor(make_cfg(tmp_path, MODEL_AND_BOXES, on_index_conflict="skip")).records())


def test_companion_without_image_is_missing_pair(tmp_path):
    root = tmp_path / "in"
    png(root / "image_1.png")
    write(root / "image_1.xml")
    write(root / "image_2.xml")
    records, ingestor = ingest(make_cfg(tmp_path, MODEL_AND_BOXES))
    assert len(records) == 1
    report = ingestor.report
    assert (report.scanned, report.missing_pair, report.bad_input) == (2, 1, 1)
    assert ingestor.metrics.events == [
        ("ingest.missing_pair", {"index": 2, "present": "companion", "file": "image_2.xml"})]
    with pytest.raises(InputError, match="index=2 has only the companion side"):
        Ingestor(make_cfg(tmp_path, MODEL_AND_BOXES, on_missing_pair="fail")).scan(estimate=False)


def test_companion_suffix_match_is_case_strict(tmp_path):
    root = tmp_path / "in"
    png(root / "image_1.png")
    write(root / "image_1.XML")
    records, ingestor = ingest(make_cfg(tmp_path, MODEL_AND_BOXES))
    assert records[0].companion is None and records[0].companion_path is None
    assert ingestor.report.ignored_files == 1


def test_multi_dot_suffix_pairs_only_the_exact_name(tmp_path):
    root = tmp_path / "in"
    png(root / "image_1.png")
    write(root / "image_1.ocr.json", b"{}")
    write(root / "image_1xocrxjson", b"{}")
    write(root / "image_1.json", b"{}")
    segment = replace(MODEL_AND_BOXES, companion_suffix=".ocr.json")
    records, ingestor = ingest(make_cfg(tmp_path, segment))
    assert Path(records[0].companion_path) == Path(str(root / "image_1.ocr.json"))
    assert ingestor.report.ignored_files == 2


# ── AC-16：方向不做任何处理 ─────────────────────────────────────────────────


def test_exif_oriented_jpeg_and_plain_png_ingest(tmp_path):
    root = tmp_path / "in"
    oriented = Image.new("RGB", (8, 4), "white")
    exif = oriented.getexif()
    exif[0x0112] = 6
    buffer = io.BytesIO()
    oriented.save(buffer, format="JPEG", exif=exif)
    jpeg = write(root / "image_1.jpg", buffer.getvalue())
    plain = png(root / "image_2.png", size=(8, 4))
    records, ingestor = ingest(make_cfg(tmp_path))
    assert ingestor.report.bad_input == 0
    assert [record.image.format for record in records] == ["jpeg", "png"]
    assert records[0].id == hashlib.sha256(jpeg).hexdigest()[:16]
    assert records[1].id == hashlib.sha256(plain).hexdigest()[:16]
    # 文件原样保留：存储像素网格不旋转，方向标签也没被处理掉。
    with Image.open(records[0].image.path) as stored:
        assert stored.size == (8, 4)
        assert stored.getexif()[0x0112] == 6


# ── AC-43 / AC-131：记录与计划形态 ──────────────────────────────────────────


def test_zero_byte_and_absent_companion_share_id(tmp_path):
    image = png(tmp_path / "with" / "image_1.png")
    write(tmp_path / "with" / "image_1.xml", b"")
    write(tmp_path / "without" / "image_1.png", image)
    empty, _ = ingest(make_cfg(tmp_path, MODEL_AND_BOXES, root=tmp_path / "with"))
    absent, _ = ingest(make_cfg(tmp_path, MODEL_AND_BOXES, root=tmp_path / "without"))
    assert empty[0].id == absent[0].id == hashlib.sha256(image).hexdigest()[:16]
    # 零字节文件仍要交给钩子，所以和「没有文件」在信封上可区分。
    assert empty[0].companion == b"" and absent[0].companion is None
    assert empty[0].companion_sha256 == hashlib.sha256(b"").hexdigest()
    assert absent[0].companion_sha256 is None


def test_record_id_formula(tmp_path):
    root = tmp_path / "in"
    image = png(root / "image_1.png")
    companion = write(root / "image_1.xml", b"<tree/>")
    records, _ = ingest(make_cfg(tmp_path, BOXES_ONLY))
    assert records[0].id == hashlib.sha256(companion + image).hexdigest()[:16]
    assert records[0].id != hashlib.sha256(image + companion).hexdigest()[:16]


def test_plan_pairs_and_files_shape(tmp_path):
    root = tmp_path / "in"
    png(root / "b" / "image_2.png")
    png(root / "a" / "image_1.png", "blue")
    write(root / "c" / "image_1.xml")
    plan = Ingestor(make_cfg(tmp_path, MODEL_AND_BOXES)).scan()
    assert plan.pairs == ((1, "c/image_1.xml", "a/image_1.png"), (2, None, "b/image_2.png"))
    assert plan.files == ("c/image_1.xml", "a/image_1.png", "b/image_2.png")
    assert plan.estimated_records == 2
    assert plan.session_lens == ()


def test_scan_does_not_touch_conservation_counters(tmp_path):
    root = tmp_path / "in"
    png(root / "image_1.png")
    write(root / "notes.txt")
    ingestor = Ingestor(make_cfg(tmp_path))
    ingestor.scan()
    report = ingestor.report
    assert (report.scanned, report.ingested, report.bad_input) == (0, 0, 0)
    assert report.ignored_files == 1


def test_source_file_is_image_path(tmp_path):
    root = tmp_path / "in"
    png(root / "shots" / "image_3.png")
    write(root / "dumps" / "image_3.xml")
    records, _ = ingest(make_cfg(tmp_path, BOXES_ONLY))
    ref = records[0].ref
    assert (ref.source_file, ref.pair_index, ref.line_no) == ("shots/image_3.png", 3, None)
    assert ref.generated_from == ()


def test_companion_kept_as_raw_bytes(tmp_path):
    root = tmp_path / "in"
    png(root / "image_1.png")
    raw = write(root / "image_1.xml", "<a>登录</a>".encode("utf-16") + b"\xff\xfe\x00\n{not json")
    records, _ = ingest(make_cfg(tmp_path, BOXES_ONLY))
    assert records[0].companion == raw
    assert records[0].ui_tree is None


def test_digest_fields_written(tmp_path):
    root = tmp_path / "in"
    image = png(root / "image_1.png")
    companion = write(root / "image_1.xml")
    records, _ = ingest(make_cfg(tmp_path, BOXES_ONLY))
    assert records[0].image_sha256 == hashlib.sha256(image).hexdigest()
    assert records[0].companion_sha256 == hashlib.sha256(companion).hexdigest()
    assert len(records[0].image_sha256) == 64
    assert records[0].exact_dedup_text is None


def test_unregistered_hook_never_reads_companion(tmp_path, monkeypatch):
    root = tmp_path / "in"
    image = png(root / "image_1.png")
    write(root / "image_1.xml")
    write(root / "uitree_1.jsonl", b'{"id": "0"}\n')
    read: list[str] = []
    real_read = Path.read_bytes

    def spying_read(path: Path) -> bytes:
        read.append(path.name)
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", spying_read)
    records, ingestor = ingest(make_cfg(tmp_path))
    assert read == ["image_1.png"]
    assert records[0].companion is None and records[0].companion_sha256 is None
    assert records[0].id == hashlib.sha256(image).hexdigest()[:16]
    # uitree_<n>.jsonl 在图片模式没有特殊含义，也不产生缺对。
    assert ingestor.report.ignored_files == 2
    assert ingestor.report.missing_pair == 0


def test_unreadable_companion_is_bad_sample(tmp_path, monkeypatch):
    root = tmp_path / "in"
    png(root / "image_1.png")
    write(root / "image_1.xml")
    png(root / "image_2.png", "blue")
    write(root / "image_2.xml")
    real_read = Path.read_bytes

    def failing_read(path: Path) -> bytes:
        if path.name == "image_1.xml":
            raise OSError("denied")
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", failing_read)
    records, ingestor = ingest(make_cfg(tmp_path, BOXES_ONLY))
    assert [record.ref.pair_index for record in records] == [2]
    assert ingestor.report.bad_input == 1
    assert ingestor.report.bad_locations[0]["file"] == "image_1.xml"
    assert ingestor.report.bad_locations[0]["reason"] == "cannot read companion file: denied"


# ── AC-131：忽略计数与冻结的 WARN ───────────────────────────────────────────


def test_ignored_files_warning_has_count_only(tmp_path, caplog):
    root = tmp_path / "in"
    png(root / "image_1.png")
    write(root / "secret-name.txt")
    write(root / "sub" / "other.xml")
    ingestor = Ingestor(make_cfg(tmp_path))
    with caplog.at_level(logging.WARNING, logger="labelkit.ingest"):
        ingestor.scan()
        records = list(ingestor.records())
    assert len(records) == 1
    warns = [entry.getMessage() for entry in caplog.records if entry.levelno == logging.WARNING]
    assert warns == [WARN_TEXT.format(2)]
    report = ingestor.report
    assert report.ignored_files == 2
    assert (report.scanned, report.ingested, report.bad_input) == (1, 1, 0)


def test_no_warning_without_ignored_files(tmp_path, caplog):
    png(tmp_path / "in" / "image_1.png")
    with caplog.at_level(logging.WARNING, logger="labelkit.ingest"):
        _, ingestor = ingest(make_cfg(tmp_path))
    assert ingestor.report.ignored_files == 0
    assert [entry for entry in caplog.records if entry.levelno == logging.WARNING] == []


def test_directory_without_images_is_input_error_after_warning(tmp_path, caplog):
    write(tmp_path / "in" / "notes.txt")
    with caplog.at_level(logging.WARNING, logger="labelkit.ingest"):
        with pytest.raises(InputError, match="no image_<index>"):
            Ingestor(make_cfg(tmp_path)).scan()
    assert WARN_TEXT.format(1) in [entry.getMessage() for entry in caplog.records]


def test_input_must_be_a_directory(tmp_path):
    single = tmp_path / "in"
    png(single)
    with pytest.raises(InputError, match="requires run.input to be a directory"):
        Ingestor(make_cfg(tmp_path)).scan()


# ── AC-131：文件头检查由 ingest 独占 ────────────────────────────────────────


def _bad_sample_only(tmp_path: Path, caplog) -> tuple[dict, list[str]]:
    """摄取一个只有坏图的目录旁加一张好图，返回坏样本定位与英文错误日志。"""
    png(tmp_path / "in" / "image_9.png", "blue")
    with caplog.at_level(logging.ERROR, logger="labelkit.ingest"):
        records, ingestor = ingest(make_cfg(tmp_path))
    assert [record.ref.pair_index for record in records] == [9]
    assert ingestor.report.bad_input == 1
    assert ingestor.report.scanned == 2
    assert ingestor.metrics.events[0][0] == "ingest.bad_line"
    errors = [entry.getMessage() for entry in caplog.records if entry.levelno == logging.ERROR]
    return ingestor.report.bad_locations[0], errors


OVERSIZE = "image pixel count exceeds the Pillow MAX_IMAGE_PIXELS limit"
UNKNOWN = "image header is not a recognized image format"


@pytest.mark.parametrize(("side", "reason"), [
    (None, UNKNOWN),    # 魔数正确但文件头不可识别
    (5, OVERSIZE),      # 25 > 16 且不到两倍：Pillow 只发 DecompressionBombWarning
    (8, OVERSIZE),      # 64 > 2 × 16：Pillow 直接抛 DecompressionBombError
])
def test_header_check_rejects_oversize_and_unknown(tmp_path, caplog, monkeypatch, side, reason):
    bad = tmp_path / "in" / "image_1.png"
    if side is None:
        write(bad, b"\x89PNG\r\n\x1a\n" + b"garbage" * 8)
    else:
        png(bad, size=(side, side))
    # 好图的边长是 4，恰好落在上限上：不算超限。
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 16)
    location, errors = _bad_sample_only(tmp_path, caplog)
    assert location == {"file": "image_1.png", "line_no": None, "index": 1, "reason": reason}
    assert errors == [f"image mode: bad sample: index=1 file=image_1.png reason={reason}"]


def test_header_check_failure_honours_on_bad_line_fail(tmp_path):
    write(tmp_path / "in" / "image_1.png", b"\x89PNG\r\n\x1a\n" + b"garbage")
    with pytest.raises(InputError, match="input.on_bad_line"):
        list(Ingestor(make_cfg(tmp_path, on_bad_line="fail")).records())


def test_magic_number_and_size_limit_still_apply(tmp_path):
    root = tmp_path / "in"
    write(root / "image_1.png", b"not a png")
    write(root / "image_2.jpg", b"\x89PNG\r\n\x1a\n")
    with pytest.raises(InputError, match="no valid records.*bad_input=2"):
        list(Ingestor(make_cfg(tmp_path)).records())
    oversized = make_cfg(tmp_path, root=tmp_path / "big", max_image_mb=1)
    write(tmp_path / "big" / "image_1.png", png(tmp_path / "x.png") + b"\0" * (1024 * 1024 + 1))
    ingestor = Ingestor(oversized)
    with pytest.raises(InputError, match="no valid records"):
        list(ingestor.records())
    assert "exceeds the input.max_image_mb" in ingestor.report.bad_locations[0]["reason"]


def test_header_check_never_decodes_pixels(tmp_path, monkeypatch):
    # 像素数据截断的 PNG 文件头完好：摄取放行，解码失败属于之后的调用（§10.7）。
    data = png(tmp_path / "source.png", size=(32, 32))
    write(tmp_path / "in" / "image_1.png", data[: len(data) - 20])

    def forbidden(self):
        raise AssertionError("ingest must read the header only")

    monkeypatch.setattr(ImageFile.ImageFile, "load", forbidden)
    monkeypatch.setattr(ImageFile.ImageFile, "verify", forbidden)
    records, ingestor = ingest(make_cfg(tmp_path))
    assert len(records) == 1
    assert ingestor.report.bad_input == 0


# ── AC-131：非图片行为不变 ──────────────────────────────────────────────────


@pytest.mark.parametrize("segment", [
    SegmentConfig(),
    SegmentConfig(enabled=False, mode="image", instruction="停放配置"),
])
def test_segment_disabled_image_only_still_no_valid_records(tmp_path, segment):
    png(tmp_path / "in" / "image_1.png")
    png(tmp_path / "in" / "image_2.png", "blue")
    ingestor = Ingestor(make_cfg(tmp_path, segment))
    with pytest.raises(InputError, match="no valid records.*missing_pair=2"):
        list(ingestor.records())
    assert ingestor.report.ignored_files == 0


def test_non_image_counts_unchanged(tmp_path):
    root = REPO_ROOT / "examples" / "ui" / "data"
    cfg = make_cfg(tmp_path, SegmentConfig(), root=root)
    plan = Ingestor(cfg).scan()
    records, ingestor = ingest(cfg)
    report = ingestor.report
    assert (report.scanned, report.ingested, report.bad_input) == (6, 5, 1)
    assert report.ignored_files == 0
    assert plan.pairs[0][1].startswith("uitree_")
    for record in records:
        assert record.ui_tree is not None
        assert record.ref.source_file.split("/")[-1].startswith("uitree_")
        assert record.companion is None and record.companion_path is None
        assert record.image_sha256 is None and record.companion_sha256 is None


def test_m1_resolved_config_drives_discovery(tmp_path):
    """走 M1 冻结的配置：伴随文件配对，缺它的样本按 D15 缺对，segment 钩子一次也不调用。"""
    env = Env(tmp_path)
    capture = tmp_path / "capture"
    png(capture / "image_1.png")
    companion = write(capture / "sub" / "image_1.xml", b"<a/>")
    png(capture / "image_2.png", "blue")
    write(capture / "readme.md")
    cfg = env.load(env.project(BOXES))
    ingestor = Ingestor(cfg)
    assert ingestor.scan().pairs == ((1, "sub/image_1.xml", "image_1.png"),)
    records = list(ingestor.records())
    assert [record.companion for record in records] == [companion]
    report = ingestor.report
    assert (report.scanned, report.ingested, report.missing_pair, report.bad_input) == (2, 1, 1, 1)
    assert report.ignored_files == 1

"""Offline unit tests for M2 ingest (labelkit/operators/ingest.py).

Covers both record ingestion and v1.8 stream-mode sessionization. Pure I/O
logic — no LLM.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    DedupConfig,
    ExtractConfig,
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
from labelkit.common.errors import InputError
from labelkit.common.contracts.types import ImageRef, Record, UITree
from labelkit.operators.ingest import Ingestor, Session, _parse_order_key

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


def make_cfg(tmp_path: Path, modality: str = "text", **input_kw) -> ResolvedConfig:
    return ResolvedConfig(
        tool=ToolConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output=str(tmp_path / "out.jsonl"), modality=modality,
                      input=str(tmp_path / "in")),
        input=InputConfig(**input_kw),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(),
        stitch=StitchConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(),
        quality=QualityConfig(),
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
    )


class EventRecorder:
    """Tiny stand-in for MetricsSink.event() — records emitted trace events."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None):
        assert stage == "ingest"
        assert batch_no == 0
        self.events.append((ev, dict(payload or {})))


# ── text modality ───────────────────────────────────────────────────────────

SPEC_LINES = [
    {"instruction": "帮我写一条请假条，明天上午要去医院", "source": "ime-log",
     "ts": "2026-06-30T10:12:00Z"},
    {"instruction": "把这句话翻译成英文：会议改到周五下午三点", "source": "ime-log",
     "ts": "2026-06-30T10:15:21Z"},
    {"query": "今天天气怎么样", "source": "ime-log", "ts": "2026-06-30T10:20:05Z"},
]


def write_jsonl(path: Path, objs, raw_lines=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(o, ensure_ascii=False) for o in objs]
    lines.extend(raw_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_text_spec_example_ids_and_report(tmp_path):
    """Reproduces spec 3.2.7 example ① exactly, including the frozen ids."""
    cfg = make_cfg(tmp_path, text_field="instruction")
    write_jsonl(tmp_path / "in" / "ime-2026-06-30.jsonl", SPEC_LINES)
    ing = Ingestor(cfg)
    recs = list(ing.records())

    assert [r.id for r in recs] == ["1cda030abc565f17", "a9bbd04dca155b52"]
    r1 = recs[0]
    assert isinstance(r1, Record)
    assert r1.modality == "text"
    assert r1.text == "帮我写一条请假条，明天上午要去医院"
    assert r1.raw == SPEC_LINES[0]
    assert r1.ui_tree is None and r1.image is None
    assert r1.ref.source_file == "ime-2026-06-30.jsonl"
    assert r1.ref.line_no == 1
    assert r1.ref.pair_index is None
    assert r1.ref.generated_from == ()
    assert recs[1].ref.line_no == 2

    rep = ing.report
    assert (rep.scanned, rep.ingested, rep.bad_input) == (3, 2, 1)
    assert rep.missing_pair == 0 and rep.index_conflict == 0
    assert rep.bad_locations == [{
        "file": "ime-2026-06-30.jsonl", "line_no": 3, "index": None,
        "reason": 'input.text_field "instruction" 未命中',
    }]


def test_text_id_is_content_deterministic(tmp_path):
    """Same raw object → same id regardless of file name / line number."""
    cfg = make_cfg(tmp_path)
    write_jsonl(tmp_path / "in" / "b.jsonl", [{"text": "hello"}, {"text": "x"}])
    write_jsonl(tmp_path / "in" / "a.jsonl", [{"text": "x"}])
    recs = list(Ingestor(cfg).records())
    # a.jsonl sorts before b.jsonl (lexicographic file order)
    assert [r.ref.source_file for r in recs] == ["a.jsonl", "b.jsonl", "b.jsonl"]
    assert recs[0].id == recs[2].id                 # both {"text": "x"}
    assert recs[0].id != recs[1].id


def test_text_single_file_input(tmp_path):
    cfg = make_cfg(tmp_path)
    file = tmp_path / "in"                          # run.input IS the file
    write_jsonl(file.parent / "tmp.jsonl", [{"text": "a"}])
    (file.parent / "tmp.jsonl").rename(file)
    recs = list(Ingestor(cfg).records())
    assert len(recs) == 1
    assert recs[0].ref.source_file == "in"


def test_text_scan_plan(tmp_path):
    cfg = make_cfg(tmp_path)
    write_jsonl(tmp_path / "in" / "b.jsonl", [{"text": "1"}, {"text": "2"}])
    write_jsonl(tmp_path / "in" / "a.jsonl", [{"text": "3"}])
    (tmp_path / "in" / "notes.txt").write_text("ignored", encoding="utf-8")
    plan = Ingestor(cfg).scan()
    assert plan.files == ("a.jsonl", "b.jsonl")
    assert plan.pairs == ()
    assert plan.estimated_records == 3


def test_text_dotted_path_and_container_serialization(tmp_path):
    cfg = make_cfg(tmp_path, text_field="conversation.turns")
    write_jsonl(tmp_path / "in" / "d.jsonl", [
        {"conversation": {"turns": "plain string"}},
        {"conversation": {"turns": [{"b": 1, "a": "文"}, "x"]}},
        {"conversation": {"other": 1}},             # miss → bad line
        {"conversation": "not-a-dict"},             # non-mapping intermediate → bad line
    ])
    ing = Ingestor(cfg)
    recs = list(ing.records())
    assert len(recs) == 2
    assert recs[0].text == "plain string"
    # canonical JSON: sorted keys, no spaces, ensure_ascii=False
    assert recs[1].text == '[{"a":"文","b":1},"x"]'
    assert ing.report.bad_input == 2
    assert ing.report.scanned == 4


def test_text_bad_lines_skip_and_events(tmp_path):
    cfg = make_cfg(tmp_path)
    write_jsonl(tmp_path / "in" / "d.jsonl", [{"text": "good"}],
                raw_lines=["{not json", '"just a string"', "", "   "])
    ing = Ingestor(cfg)
    rec_events = EventRecorder()
    ing.metrics = rec_events
    recs = list(ing.records())
    assert len(recs) == 1
    rep = ing.report
    # empty / whitespace-only lines are silent: not scanned, not bad
    assert (rep.scanned, rep.ingested, rep.bad_input) == (3, 1, 2)
    evs = [ev for ev, _ in rec_events.events]
    assert evs == ["ingest.bad_line", "ingest.bad_line"]
    payload = rec_events.events[0][1]
    assert payload["file"] == "d.jsonl" and payload["line_no"] == 2
    assert "reason" in payload


def test_text_invalid_utf8_line_is_bad_line(tmp_path):
    """Spec 6.1 (UTF-8 JSONL) + 3.2.1 (原样保留): invalid bytes must become a
    bad line, never be silently replaced with U+FFFD and ingested."""
    cfg = make_cfg(tmp_path)
    path = tmp_path / "in" / "d.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(
        b'{"text": "good"}\n'
        b'{"text": "bad \xff\xfe"}\n'      # invalid UTF-8 inside the line
        b'{"text": "also good"}\n'
    )
    ing = Ingestor(cfg)
    rec_events = EventRecorder()
    ing.metrics = rec_events
    recs = list(ing.records())
    assert [r.text for r in recs] == ["good", "also good"]
    rep = ing.report
    assert (rep.scanned, rep.ingested, rep.bad_input) == (3, 2, 1)
    assert rep.bad_locations == [{
        "file": "d.jsonl", "line_no": 2, "index": None,
        "reason": "行不是合法 UTF-8",
    }]
    ev, payload = rec_events.events[0]
    assert ev == "ingest.bad_line"
    assert payload["line_no"] == 2 and payload["reason"] == "行不是合法 UTF-8"
    # no replacement character ever reaches a Record
    assert all("�" not in r.text for r in recs)


def test_text_invalid_utf8_line_fail_and_scan(tmp_path):
    cfg = make_cfg(tmp_path, on_bad_line="fail")
    path = tmp_path / "in" / "d.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"text": "ok"}\n\xff\xfe\xfd\n')
    # scan() only counts lines — invalid bytes must not break the estimate
    assert Ingestor(cfg).scan().estimated_records == 2
    ing = Ingestor(cfg)
    it = ing.records()
    assert next(it).text == "ok"
    with pytest.raises(InputError):
        next(it)


def test_text_on_bad_line_fail(tmp_path):
    cfg = make_cfg(tmp_path, on_bad_line="fail")
    write_jsonl(tmp_path / "in" / "d.jsonl", [{"text": "ok"}, {"other": 1}])
    ing = Ingestor(cfg)
    it = ing.records()
    assert next(it).text == "ok"
    with pytest.raises(InputError):
        next(it)


def test_text_null_hit_is_bad_line(tmp_path):
    cfg = make_cfg(tmp_path)
    write_jsonl(tmp_path / "in" / "d.jsonl", [{"text": None}])
    ing = Ingestor(cfg)
    # The only line is bad → the stream exhausts with zero valid records,
    # which is itself an InputError (spec §2.4「无任何合法记录」→ exit 3).
    with pytest.raises(InputError, match="无任何合法记录"):
        list(ing.records())
    assert ing.report.bad_input == 1
    assert ing.report.ingested == 0


# ── zero-valid-record guard（spec §2.4「无任何合法记录」→ exit 3）────────────

def test_text_all_lines_miss_text_field_raises(tmp_path):
    # The empirical footgun this guard exists for: a wrong input.text_field
    # turns EVERY line into a bad line under the default skip policy; the run
    # must end with InputError (exit 3), not a "successful" empty output.
    cfg = make_cfg(tmp_path)
    write_jsonl(tmp_path / "in" / "d.jsonl",
                [{"query": "今天天气怎么样"}, {"query": "在吗"}])
    ing = Ingestor(cfg)
    with pytest.raises(InputError, match="无任何合法记录"):
        list(ing.records())
    assert (ing.report.scanned, ing.report.ingested, ing.report.bad_input) == (2, 0, 2)


def test_text_empty_file_raises_no_valid_records(tmp_path):
    cfg = make_cfg(tmp_path)
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "d.jsonl").write_text("\n\n", encoding="utf-8")
    ing = Ingestor(cfg)
    with pytest.raises(InputError, match="无任何合法记录"):
        list(ing.records())
    assert (ing.report.scanned, ing.report.ingested) == (0, 0)


def test_text_partial_bad_lines_do_not_raise(tmp_path):
    # Boundary pin: the guard fires only at ingested == 0 — a stream with at
    # least one valid record completes normally however many bad lines it has.
    cfg = make_cfg(tmp_path)
    write_jsonl(tmp_path / "in" / "d.jsonl",
                [{"query": "bad"}, {"text": "好的"}, {"query": "bad"}])
    ing = Ingestor(cfg)
    recs = list(ing.records())
    assert len(recs) == 1
    assert (ing.report.ingested, ing.report.bad_input) == (1, 2)


def test_ui_all_pairs_missing_counterpart_raises(tmp_path):
    cfg = make_cfg(tmp_path, modality="ui")  # on_missing_pair 默认 skip
    root = make_ui_dir(tmp_path)
    (root / "uitree_1.jsonl").write_text('{"id": "0", "class": "V"}\n', encoding="utf-8")
    (root / "uitree_2.jsonl").write_text('{"id": "0", "class": "V"}\n', encoding="utf-8")
    ing = Ingestor(cfg)
    with pytest.raises(InputError, match="无任何合法记录"):
        list(ing.records())
    assert ing.report.missing_pair == 2
    assert ing.report.ingested == 0


def test_missing_input_path_raises(tmp_path):
    cfg = make_cfg(tmp_path)                        # tmp_path/"in" never created
    with pytest.raises(InputError):
        Ingestor(cfg).scan()
    with pytest.raises(InputError):
        list(Ingestor(cfg).records())


def test_text_empty_dir_raises(tmp_path):
    cfg = make_cfg(tmp_path)
    (tmp_path / "in").mkdir()
    with pytest.raises(InputError):
        Ingestor(cfg).scan()


# ── UI modality ─────────────────────────────────────────────────────────────

FLAT_TREE = "\n".join([
    '{"id": "0", "class": "FrameLayout", "bounds": [0, 0, 1080, 2340], "visible": true}',
    '{"id": "1", "parent": "0", "class": "TextView", "text": "登录", "bounds": [72, 296, 264, 392], "visible": true}',
    '{"id": "2", "parent": "0", "class": "EditText", "bounds": [72, 520, 1008, 664], "visible": true, "hint": "请输入手机号"}',
    '{"id": "3", "parent": "0", "class": "EditText", "bounds": [72, 712, 672, 856], "visible": true, "hint": "请输入验证码"}',
    '{"id": "4", "parent": "0", "class": "Button", "text": "获取验证码", "bounds": [704, 712, 1008, 856], "visible": true}',
    '{"id": "5", "parent": "0", "class": "Button", "text": "登录", "bounds": [72, 952, 1008, 1096], "visible": true}',
]) + "\n"

SPEC_SERIALIZED = "\n".join([
    "FrameLayout [0,0,1080,2340]",
    '  TextView "登录" [72,296,264,392]',
    "  EditText [72,520,1008,664] hint=请输入手机号",
    "  EditText [72,712,672,856] hint=请输入验证码",
    '  Button "获取验证码" [704,712,1008,856]',
    '  Button "登录" [72,952,1008,1096]',
])


def make_ui_dir(tmp_path: Path) -> Path:
    root = tmp_path / "in"
    root.mkdir(parents=True, exist_ok=True)
    return root


def put_pair(root: Path, index: int, tree_sub: str = "", image_sub: str = "",
             tree_text: str | None = None, image_bytes: bytes | None = None,
             image_ext: str = "png"):
    tree_dir = root / tree_sub if tree_sub else root
    image_dir = root / image_sub if image_sub else root
    tree_dir.mkdir(parents=True, exist_ok=True)
    image_dir.mkdir(parents=True, exist_ok=True)
    (tree_dir / f"uitree_{index}.jsonl").write_text(
        tree_text if tree_text is not None else FLAT_TREE, encoding="utf-8")
    magic = PNG_MAGIC if image_ext == "png" else JPEG_MAGIC
    (image_dir / f"image_{index}.{image_ext}").write_bytes(
        image_bytes if image_bytes is not None else magic + b"pixels")


def test_ui_pairing_across_subdirs_spec_example(tmp_path):
    """Tree in b/, image in c/, one shared index namespace (spec 3.2.7 ②)."""
    cfg = make_cfg(tmp_path, modality="ui")
    root = make_ui_dir(tmp_path)
    put_pair(root, 2, tree_sub="b", image_sub="c")
    ing = Ingestor(cfg)
    recs = list(ing.records())
    assert len(recs) == 1
    rec = recs[0]
    assert rec.modality == "ui"
    assert rec.text is None and rec.raw is None
    assert rec.ref.source_file == "b/uitree_2.jsonl"
    assert rec.ref.pair_index == 2 and rec.ref.line_no is None
    assert isinstance(rec.ui_tree, UITree)
    assert rec.ui_tree.serialize() == SPEC_SERIALIZED
    assert isinstance(rec.image, ImageRef)
    assert rec.image.format == "png"
    assert rec.image.path == root / "c" / "image_2.png"
    assert rec.image.size_bytes == (root / "c" / "image_2.png").stat().st_size
    assert (ing.report.scanned, ing.report.ingested) == (1, 1)


def test_ui_record_id_is_sha256_of_tree_plus_image_bytes(tmp_path):
    import hashlib
    cfg = make_cfg(tmp_path, modality="ui")
    root = make_ui_dir(tmp_path)
    put_pair(root, 1)
    rec = next(Ingestor(cfg).records())
    tree_b = (root / "uitree_1.jsonl").read_bytes()
    img_b = (root / "image_1.png").read_bytes()
    assert rec.id == hashlib.sha256(tree_b + img_b).hexdigest()[:16]
    assert len(rec.id) == 16


def test_ui_same_index_in_different_subdirs_is_conflict(tmp_path):
    """One shared index namespace: two trees with index 3 in different subdirs."""
    cfg = make_cfg(tmp_path, modality="ui", on_index_conflict="skip")
    root = make_ui_dir(tmp_path)
    put_pair(root, 3, tree_sub="a", image_sub="a")
    (root / "b").mkdir()
    (root / "b" / "uitree_3.jsonl").write_text(FLAT_TREE, encoding="utf-8")
    ing = Ingestor(cfg)
    rec_events = EventRecorder()
    ing.metrics = rec_events
    # The conflicted index is the only candidate → zero valid records → InputError.
    with pytest.raises(InputError, match="无任何合法记录"):
        list(ing.records())
    rep = ing.report
    assert rep.index_conflict == 1 and rep.bad_input == 1 and rep.scanned == 1
    assert rep.bad_locations[0]["index"] == 3
    ev, payload = rec_events.events[0]
    assert ev == "ingest.index_conflict"
    assert payload["index"] == 3
    # all files of the conflicted index (both offending trees + its image)
    assert sorted(payload["files"]) == [
        "a/image_3.png", "a/uitree_3.jsonl", "b/uitree_3.jsonl"]


def test_ui_png_plus_jpg_same_index_is_conflict(tmp_path):
    cfg = make_cfg(tmp_path, modality="ui", on_index_conflict="skip")
    root = make_ui_dir(tmp_path)
    put_pair(root, 7, image_ext="png")
    (root / "image_7.jpg").write_bytes(JPEG_MAGIC + b"x")
    ing = Ingestor(cfg)
    # The sole index is skipped as a conflict → zero valid records → InputError.
    with pytest.raises(InputError, match="无任何合法记录"):
        list(ing.records())
    assert ing.report.index_conflict == 1
    assert ing.report.ingested == 0


def test_ui_index_conflict_fail_default(tmp_path):
    """Default on_index_conflict='fail': scan() and records() both raise."""
    cfg = make_cfg(tmp_path, modality="ui")
    root = make_ui_dir(tmp_path)
    put_pair(root, 1)
    (root / "sub").mkdir()
    (root / "sub" / "image_1.jpeg").write_bytes(JPEG_MAGIC + b"x")
    with pytest.raises(InputError):
        Ingestor(cfg).scan()
    with pytest.raises(InputError):
        list(Ingestor(cfg).records())


def test_ui_missing_pair_skip_default(tmp_path):
    cfg = make_cfg(tmp_path, modality="ui")
    root = make_ui_dir(tmp_path)
    put_pair(root, 1)
    (root / "uitree_2.jsonl").write_text(FLAT_TREE, encoding="utf-8")   # no image_2
    (root / "image_3.png").write_bytes(PNG_MAGIC + b"x")                # no uitree_3
    ing = Ingestor(cfg)
    rec_events = EventRecorder()
    ing.metrics = rec_events
    recs = list(ing.records())
    assert [r.ref.pair_index for r in recs] == [1]
    rep = ing.report
    assert rep.missing_pair == 2
    assert rep.bad_input == 2
    assert rep.scanned == 3
    ev_payloads = {p["index"]: p for ev, p in rec_events.events
                   if ev == "ingest.missing_pair"}
    assert ev_payloads[2]["present"] == "tree"
    assert ev_payloads[2]["file"] == "uitree_2.jsonl"
    assert ev_payloads[3]["present"] == "image"


def test_ui_missing_pair_fail(tmp_path):
    cfg = make_cfg(tmp_path, modality="ui", on_missing_pair="fail")
    root = make_ui_dir(tmp_path)
    put_pair(root, 1)
    (root / "uitree_2.jsonl").write_text(FLAT_TREE, encoding="utf-8")
    with pytest.raises(InputError):
        list(Ingestor(cfg).records())


def test_ui_scan_plan_pairs_and_files(tmp_path):
    cfg = make_cfg(tmp_path, modality="ui", on_index_conflict="skip")
    root = make_ui_dir(tmp_path)
    put_pair(root, 10, tree_sub="x", image_sub="x")
    put_pair(root, 2, image_ext="jpg")
    (root / "uitree_5.jsonl").write_text(FLAT_TREE, encoding="utf-8")   # missing pair
    plan = Ingestor(cfg).scan()
    assert plan.pairs == ((2, "uitree_2.jsonl", "image_2.jpg"),
                          (10, "x/uitree_10.jsonl", "x/image_10.png"))
    assert plan.files == ("uitree_2.jsonl", "image_2.jpg",
                          "x/uitree_10.jsonl", "x/image_10.png")
    assert plan.estimated_records == 2


def test_ui_index_leading_zeros_and_case_insensitive_ext(tmp_path):
    cfg = make_cfg(tmp_path, modality="ui")
    root = make_ui_dir(tmp_path)
    (root / "uitree_007.jsonl").write_text(FLAT_TREE, encoding="utf-8")
    (root / "image_7.PNG").write_bytes(PNG_MAGIC + b"x")
    recs = list(Ingestor(cfg).records())
    # index parsed base-10: uitree_007 pairs with image_7
    assert len(recs) == 1
    assert recs[0].ref.pair_index == 7
    assert recs[0].image.format == "png"


def test_ui_jpg_maps_to_jpeg_format(tmp_path):
    cfg = make_cfg(tmp_path, modality="ui")
    root = make_ui_dir(tmp_path)
    put_pair(root, 1, image_ext="jpg")
    rec = next(Ingestor(cfg).records())
    assert rec.image.format == "jpeg"


def test_ui_oversized_image_is_bad_record(tmp_path):
    cfg = make_cfg(tmp_path, modality="ui", max_image_mb=1)
    root = make_ui_dir(tmp_path)
    put_pair(root, 1, image_bytes=PNG_MAGIC + b"\0" * (1024 * 1024))   # > 1 MiB
    put_pair(root, 2)
    ing = Ingestor(cfg)
    recs = list(ing.records())
    assert [r.ref.pair_index for r in recs] == [2]
    rep = ing.report
    assert rep.bad_input == 1
    assert rep.bad_locations[0]["file"] == "image_1.png"
    assert rep.bad_locations[0]["index"] == 1


def test_ui_bad_magic_is_bad_record(tmp_path):
    cfg = make_cfg(tmp_path, modality="ui")
    root = make_ui_dir(tmp_path)
    put_pair(root, 1, image_bytes=b"GIF89a not a png")
    ing = Ingestor(cfg)
    # The only pair is a bad record → zero valid records → InputError.
    with pytest.raises(InputError, match="无任何合法记录"):
        list(ing.records())
    assert ing.report.bad_input == 1
    assert ing.report.ingested == 0


def test_ui_empty_or_all_bad_tree_is_bad_record(tmp_path):
    cfg = make_cfg(tmp_path, modality="ui")
    root = make_ui_dir(tmp_path)
    put_pair(root, 1, tree_text="")                           # empty file
    put_pair(root, 2, tree_text="{broken\nnot json either\n")  # all bad lines
    put_pair(root, 3)
    ing = Ingestor(cfg)
    recs = list(ing.records())
    assert [r.ref.pair_index for r in recs] == [3]
    assert ing.report.bad_input == 2
    assert ing.report.ingested == 1


def test_ui_bad_record_on_bad_line_fail(tmp_path):
    cfg = make_cfg(tmp_path, modality="ui", on_bad_line="fail")
    root = make_ui_dir(tmp_path)
    put_pair(root, 1, tree_text="")
    with pytest.raises(InputError):
        list(Ingestor(cfg).records())


def test_ui_partial_bad_tree_lines_are_skipped(tmp_path):
    cfg = make_cfg(tmp_path, modality="ui")
    root = make_ui_dir(tmp_path)
    tree = '{"id":"0","class":"Root","visible":true}\n{oops\n{"id":"1","parent":"0","class":"Leaf"}\n'
    put_pair(root, 1, tree_text=tree)
    rec = next(Ingestor(cfg).records())
    assert [n.role for n in rec.ui_tree.nodes] == ["Root", "Leaf"]
    assert rec.ui_tree.nodes[1].depth == 1


def test_flat_tree_out_of_file_order_rebuilt_depth_first(tmp_path):
    """Spec 4.1 declares UITree.nodes 深度优先序 as a type contract: a flat
    export not already in DFS order (e.g. BFS accessibility dumps) must be
    reordered and get depths from the parent_id graph, not from file order."""
    cfg = make_cfg(tmp_path, modality="ui")
    root = make_ui_dir(tmp_path)
    tree = "\n".join([
        '{"id": "0", "class": "Root"}',
        '{"id": "2", "parent": "1", "class": "Leaf"}',   # parent appears later
        '{"id": "1", "parent": "0", "class": "Mid"}',
    ]) + "\n"
    put_pair(root, 1, tree_text=tree)
    rec = next(Ingestor(cfg).records())
    nodes = rec.ui_tree.nodes
    assert [n.node_id for n in nodes] == ["0", "1", "2"]   # DFS order
    assert [n.depth for n in nodes] == [0, 1, 2]
    assert rec.ui_tree.serialize() == "Root [0,0,0,0]\n  Mid [0,0,0,0]\n    Leaf [0,0,0,0]"


def test_flat_tree_bfs_order_sibling_children_keep_file_order(tmp_path):
    cfg = make_cfg(tmp_path, modality="ui")
    root = make_ui_dir(tmp_path)
    tree = "\n".join([                                     # BFS-ordered dump
        '{"id": "r", "class": "Root"}',
        '{"id": "a", "parent": "r", "class": "A"}',
        '{"id": "b", "parent": "r", "class": "B"}',
        '{"id": "a1", "parent": "a", "class": "A1"}',
        '{"id": "b1", "parent": "b", "class": "B1"}',
    ]) + "\n"
    put_pair(root, 1, tree_text=tree)
    rec = next(Ingestor(cfg).records())
    assert [n.node_id for n in rec.ui_tree.nodes] == ["r", "a", "a1", "b", "b1"]
    assert [n.depth for n in rec.ui_tree.nodes] == [0, 1, 2, 1, 2]


def test_flat_tree_unknown_parent_and_cycle_are_kept_as_roots(tmp_path):
    """Nodes with an unknown parent id are roots (depth 0); a parent-id cycle
    is unreachable from any root but must not be dropped or loop forever."""
    cfg = make_cfg(tmp_path, modality="ui")
    root = make_ui_dir(tmp_path)
    tree = "\n".join([
        '{"id": "x", "parent": "ghost", "class": "Orphan"}',
        '{"id": "c1", "parent": "c2", "class": "Cyc1"}',
        '{"id": "c2", "parent": "c1", "class": "Cyc2"}',
    ]) + "\n"
    put_pair(root, 1, tree_text=tree)
    rec = next(Ingestor(cfg).records())
    nodes = rec.ui_tree.nodes
    assert len(nodes) == 3                                 # nothing dropped
    assert [n.node_id for n in nodes] == ["x", "c1", "c2"]
    assert nodes[0].depth == 0                             # unknown parent → root
    assert nodes[1].depth == 0 and nodes[2].depth == 1     # cycle entered in file order


def test_ui_scanned_counted_lazily_under_partial_consumption(tmp_path):
    """§6.4 invariant under --limit / early stop (CONTRACTS §7.9): scanned
    reflects indexes actually handled, not the whole eager scan."""
    cfg = make_cfg(tmp_path, modality="ui")
    root = make_ui_dir(tmp_path)
    put_pair(root, 1)
    put_pair(root, 2)
    put_pair(root, 3)
    (root / "uitree_9.jsonl").write_text(FLAT_TREE, encoding="utf-8")  # missing pair
    ing = Ingestor(cfg)
    it = ing.records()
    rec = next(it)                                         # consume ONE pair only
    assert rec.ref.pair_index == 1
    rep = ing.report
    # 1 missing-pair anomaly (handled up front) + 1 consumed pair
    assert rep.scanned == 2
    assert rep.ingested == 1 and rep.bad_input == 1 and rep.missing_pair == 1
    list(it)                                               # drain the rest
    assert (ing.report.scanned, ing.report.ingested) == (4, 3)


# ── UI tree field normalization (spec §6.2) ─────────────────────────────────

def test_node_field_mapping_and_defaults(tmp_path):
    cfg = make_cfg(tmp_path, modality="ui")
    root = make_ui_dir(tmp_path)
    tree = "\n".join([
        # no id → line-number string; no class-family key → "unknown"; no bounds → zeros
        '{"visible_to_user": false, "foo": 3}',
        # aliases: node_id / className / label / contentDescription / string bounds
        json.dumps({"node_id": "n2", "className": "Button", "label": "OK",
                    "contentDescription": "confirm", "bounds": "[1,2][30,40]"},
                   ensure_ascii=False),
        # precedence: "class" wins over "type"/"role"; "text" wins over "label"
        json.dumps({"id": 5, "parent": "n2", "class": "A", "type": "B", "role": "C",
                    "text": "t", "label": "l", "extra_num": 1.5,
                    "extra_obj": {"b": 1, "a": 2}}),
    ]) + "\n"
    put_pair(root, 1, tree_text=tree)
    rec = next(Ingestor(cfg).records())
    n1, n2, n3 = rec.ui_tree.nodes

    assert n1.node_id == "1"                       # line-number string default
    assert n1.parent_id is None
    assert n1.role == "unknown"
    assert n1.text == "" and n1.content_desc == ""
    assert n1.bounds == (0, 0, 0, 0)
    assert n1.visible is False                     # visible_to_user alias
    assert n1.extra == {"foo": "3"}

    assert n2.node_id == "n2"
    assert n2.role == "Button"
    assert n2.text == "OK"                         # label alias
    assert n2.content_desc == "confirm"            # contentDescription alias
    assert n2.bounds == (1, 2, 30, 40)             # "[l,t][r,b]" string form
    assert n2.visible is True                      # default

    assert n3.node_id == "5"                       # stringified
    assert n3.parent_id == "n2"
    assert n3.role == "A"                          # precedence: class first
    assert n3.text == "t"                          # precedence: text first
    # remaining fields land in extra, stringified; containers → canonical JSON
    assert n3.extra == {"role": "C", "type": "B", "label": "l",
                        "extra_num": "1.5", "extra_obj": '{"a":2,"b":1}'}
    assert n3.depth == 1                           # parent n2 has depth 0


def test_flat_style_children_field_lands_in_extra(tmp_path):
    """§6.2 extra row: in FLAT style `children` is not structural — a row
    carrying it (e.g. child-id lists in accessibility exports) keeps it in
    extra, value stringified. Only the nested style consumes `children`."""
    cfg = make_cfg(tmp_path, modality="ui")
    root = make_ui_dir(tmp_path)
    tree = "\n".join([
        '{"id": "0", "class": "Root"}',                    # first line: no children
        '{"id": "1", "parent": "0", "class": "X", "children": [1, 2]}',
    ]) + "\n"
    put_pair(root, 1, tree_text=tree)
    rec = next(Ingestor(cfg).records())
    assert rec.ui_tree.nodes[1].extra == {"children": "[1,2]"}


def test_nested_style_tree_parsing(tmp_path):
    cfg = make_cfg(tmp_path, modality="ui")
    root = make_ui_dir(tmp_path)
    nested = json.dumps({
        "id": "root", "class": "Frame", "bounds": [0, 0, 100, 100],
        "children": [
            {"class": "Text", "text": "hi",
             "children": [{"id": "leaf", "class": "Leaf"}]},
            {"id": "sib", "class": "Btn", "text": "go"},
        ],
    }, ensure_ascii=False) + "\n"
    put_pair(root, 1, tree_text=nested)
    rec = next(Ingestor(cfg).records())
    nodes = rec.ui_tree.nodes
    # depth-first order
    assert [n.role for n in nodes] == ["Frame", "Text", "Leaf", "Btn"]
    assert [n.depth for n in nodes] == [0, 1, 2, 1]
    assert nodes[1].parent_id == "root"            # structural parent
    assert nodes[1].node_id == "2"                 # DFS-order default id
    assert nodes[2].parent_id == "2"
    assert nodes[3].parent_id == "root"
    # `children` never leaks into extra
    assert all("children" not in n.extra for n in nodes)


def test_ui_records_are_frozen_and_lazy(tmp_path):
    cfg = make_cfg(tmp_path, modality="ui")
    root = make_ui_dir(tmp_path)
    put_pair(root, 1)
    rec = next(Ingestor(cfg).records())
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.id = "x"  # type: ignore[misc]
    # ImageRef holds path + stat info only; no pixel data attribute exists
    assert set(f.name for f in dataclasses.fields(rec.image)) == \
        {"path", "format", "size_bytes"}


def test_scan_missing_pair_fail_raises_early(tmp_path):
    # Review finding (P2-4 follow-up): the pre-scan must die on a missing pair
    # under the fail policy — before run.start ever touches the trace file.
    cfg = make_cfg(tmp_path, modality="ui", on_missing_pair="fail")
    root = make_ui_dir(tmp_path)
    (root / "uitree_1.jsonl").write_text('{"id": "0", "class": "V"}\n', encoding="utf-8")
    ing = Ingestor(cfg)
    with pytest.raises(InputError, match="缺对"):
        ing.scan()


def test_scan_estimate_false_skips_line_count(tmp_path):
    cfg = make_cfg(tmp_path)
    write_jsonl(tmp_path / "in" / "d.jsonl", [{"text": "好"}, {"text": "的"}])
    ing = Ingestor(cfg)
    plan = ing.scan(estimate=False)
    assert plan.estimated_records == 0          # not counted — and no full read
    assert ing.scan().estimated_records == 2    # default still estimates


# ── stream-mode sessionization (spec 3.2.8, v1.8) ─────────────────────────
# Covers timestamp parsing, per-key monotonicity, all close causes, frame-level
# limits, report counters, scan fusion, and trace event payload shapes.

MINI_TREE = '{"id": "0", "class": "FrameLayout", "visible": true}\n'


def make_stream_cfg(tmp_path: Path, *, modality: str = "text",
             stream: StreamConfig | None = None,
             segment: SegmentConfig | None = None,
             limit: int | None = None, **input_kw) -> ResolvedConfig:
    return ResolvedConfig(
        tool=ToolConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output=str(tmp_path / "out.jsonl"), modality=modality,
                      input=str(tmp_path / "in")),
        input=InputConfig(**input_kw),
        stream=stream if stream is not None else StreamConfig(),
        dedup=DedupConfig(),
        segment=segment if segment is not None else SegmentConfig(enabled=True),
        stitch=StitchConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=TraceConfig(),
        rubric=Rubric(name="t", criteria=()),
        class_views={},
        user_schema={"type": "object"},
        limit=limit,
        strict=False,
        dry_run=False,
        config_path="config.toml",
        project_path="project.toml",
        config_digest="sha256:0",
        project_digest="sha256:0",
    )


class StreamEventRecorder:
    """Same style as test_ingest.py: records emitted trace events."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None):
        assert stage == "ingest"
        assert batch_no == 0
        assert record_ids == ()
        self.events.append((ev, dict(payload or {})))


def write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ts_lines(*ts_values, device: str | None = None) -> list[str]:
    lines = []
    for ts in ts_values:
        obj = {"text": f"t-{ts}", "ts": ts}
        if device is not None:
            obj["device"] = device
        lines.append(json.dumps(obj, ensure_ascii=False))
    return lines


def run_sessions(cfg) -> tuple[list[Session], Ingestor, StreamEventRecorder]:
    ing = Ingestor(cfg)
    rec = StreamEventRecorder()
    ing.metrics = rec
    return list(ing.sessions()), ing, rec


# ── S20 timestamp parsing, all branches ──────────────────────────────────────

def test_parse_numeric_epoch_seconds():
    assert _parse_order_key(1_719_742_320) == 1_719_742_320.0
    assert _parse_order_key(0) == 0.0
    assert _parse_order_key(1_719_742_320.5) == 1_719_742_320.5
    # everything below 1e11 is seconds, as-is
    assert _parse_order_key(99_999_999_999.9) == 99_999_999_999.9


def test_parse_numeric_epoch_milliseconds():
    assert _parse_order_key(1_719_742_320_000) == 1_719_742_320.0
    # boundary: exactly 1e11 is the first millisecond value
    assert _parse_order_key(10**11) == 10**8


def test_parse_numeric_rejections():
    assert _parse_order_key(-1) is None                  # v < 0
    assert _parse_order_key(-0.5) is None
    assert _parse_order_key(10**14) is None              # v >= 1e14
    assert _parse_order_key(float("inf")) is None
    assert _parse_order_key(float("nan")) is None        # fits no S20 bucket
    assert _parse_order_key(True) is None                # bool is not a timestamp
    assert _parse_order_key(None) is None
    assert _parse_order_key([1]) is None
    assert _parse_order_key({"t": 1}) is None


def test_parse_string_numeric_follows_numeric_rules():
    assert _parse_order_key("1719742320") == 1_719_742_320.0
    assert _parse_order_key("1719742320000") == 1_719_742_320.0   # ms
    assert _parse_order_key("-5") is None
    assert _parse_order_key("100000000000000") is None            # 1e14
    assert _parse_order_key("nan") is None
    assert _parse_order_key("inf") is None


def test_parse_iso_with_z_suffix():
    expected = datetime(2026, 6, 30, 10, 12, 0, tzinfo=timezone.utc).timestamp()
    assert _parse_order_key("2026-06-30T10:12:00Z") == expected


def test_parse_iso_aware_offset():
    v = _parse_order_key("2026-06-30T18:12:00+08:00")
    assert v == datetime(2026, 6, 30, 10, 12, 0, tzinfo=timezone.utc).timestamp()


def test_parse_iso_naive_interpreted_as_utc():
    assert (_parse_order_key("2026-06-30T10:12:00")
            == _parse_order_key("2026-06-30T10:12:00Z"))


def test_parse_invalid_strings():
    assert _parse_order_key("not-a-time") is None
    assert _parse_order_key("") is None
    assert _parse_order_key("2026-13-45T99:99:99") is None


# ── S19 per-partition-key cursors & on_disorder ──────────────────────────────

def meta_stream_cfg(tmp_path, *, key=("meta:device",), on_disorder="skip",
                    gap_s=300, gap_steps=0, session_max_len=200,
                    session_max_span_s=0, limit=None):
    return make_stream_cfg(
        tmp_path,
        stream=StreamConfig(order_by="meta:ts", on_disorder=on_disorder,
                            key=tuple(key), gap_s=gap_s, gap_steps=gap_steps,
                            session_max_len=session_max_len,
                            session_max_span_s=session_max_span_s),
        limit=limit,
    )


def test_per_key_cursors_grouped_input_each_monotonic(tmp_path):
    """Two keys, grouped: b restarts at a LOWER timestamp than a's cursor —
    per-partition cursors must not flag it; a's later group continues from
    a's own persistent cursor."""
    cfg = meta_stream_cfg(tmp_path)
    write_lines(tmp_path / "in" / "d.jsonl",
                ts_lines(100, 110, device="a")
                + ts_lines(5, 15, device="b")          # lower than a's 110 — fine
                + ts_lines(120, 130, device="a"))      # a resumes above its cursor
    sessions, ing, _ = run_sessions(cfg)
    assert ing.report.disorder == 0
    assert [s.cause for s in sessions] == ["key", "key", "eof"]
    assert [len(s.records) for s in sessions] == [2, 2, 2]
    assert ing.report.sessions == 3


def test_same_key_disorder_skipped_and_counted(tmp_path):
    """Same-key out-of-order record is skipped under the default skip policy:
    bad_input + disorder + bad_locations + per-record event; the session flow
    continues without it. Cursors persist across sessions (grouped input)."""
    cfg = meta_stream_cfg(tmp_path)
    write_lines(tmp_path / "in" / "d.jsonl",
                ts_lines(100, 110, device="a")
                + ts_lines(105, device="a")            # 105 < cursor 110 → disorder
                + ts_lines(50, 60, device="b")
                + ts_lines(90, device="a"))            # a cursor is STILL 110 → disorder
    sessions, ing, rec = run_sessions(cfg)
    rep = ing.report
    assert rep.disorder == 2
    assert rep.bad_input == 2                          # disorder ⊂ bad_input
    assert rep.sessions == 2
    assert [s.cause for s in sessions] == ["key", "eof"]
    assert [len(s.records) for s in sessions] == [2, 2]
    assert [loc["line_no"] for loc in rep.bad_locations] == [3, 6]
    assert all(loc["reason"].startswith("乱序：") for loc in rep.bad_locations)
    disorder_events = [p for ev, p in rec.events if ev == "ingest.disorder"]
    assert len(disorder_events) == 2


def test_timestamp_parse_failure_walks_disorder_path(tmp_path):
    cfg = meta_stream_cfg(tmp_path)
    lines = ts_lines(100, device="a") + [
        json.dumps({"text": "no-ts", "device": "a"}),               # field missing
        json.dumps({"text": "bad-ts", "ts": "next tuesday", "device": "a"}),
    ] + ts_lines(110, device="a")
    write_lines(tmp_path / "in" / "d.jsonl", lines)
    sessions, ing, rec = run_sessions(cfg)
    assert ing.report.disorder == 2
    assert [len(s.records) for s in sessions] == [2]
    reasons = [loc["reason"] for loc in ing.report.bad_locations]
    assert all(r.startswith("时间戳解析失败：") for r in reasons)
    assert "字段缺失" in reasons[0]
    assert "next tuesday" in reasons[1]                # timestamp value may enter reason


def test_on_disorder_fail_raises_input_error(tmp_path):
    cfg = meta_stream_cfg(tmp_path, on_disorder="fail")
    write_lines(tmp_path / "in" / "d.jsonl",
                ts_lines(100, 90, device="a"))
    ing = Ingestor(cfg)
    with pytest.raises(InputError, match=r'stream\.on_disorder = "fail"') as exc_info:
        list(ing.sessions())
    assert "d.jsonl:2" in str(exc_info.value)          # message carries the location
    assert ing.report.disorder == 1


def test_disorder_stderr_warn_only_once_per_run(tmp_path, caplog):
    cfg = meta_stream_cfg(tmp_path)
    write_lines(tmp_path / "in" / "d.jsonl",
                ts_lines(100, 90, 80, 110, device="a"))   # two disorder records
    ing = Ingestor(cfg)
    rec = StreamEventRecorder()
    ing.metrics = rec
    with caplog.at_level(logging.WARNING, logger=".".join(("labelkit", "ingest"))):
        sessions = list(ing.sessions())
    assert ing.report.disorder == 2
    assert len([ev for ev, _ in rec.events if ev == "ingest.disorder"]) == 2
    warns = [r for r in caplog.records if "ingest.disorder" in r.message]
    assert len(warns) == 1                             # ONE stderr WARN per run
    assert [len(s.records) for s in sessions] == [2]


# ── session close causes ─────────────────────────────────────────────────────

def test_cause_gap_s(tmp_path):
    cfg = meta_stream_cfg(tmp_path, key=(), gap_s=300)
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(0, 100, 500))
    sessions, ing, _ = run_sessions(cfg)               # 500-100=400 > 300 → gap
    assert [(s.cause, len(s.records)) for s in sessions] == [("gap", 2), ("eof", 1)]
    assert ing.report.sessions == 2


def test_cause_gap_steps_text_line_no(tmp_path):
    # blank lines advance line_no without being scanned: records sit on
    # lines 1 and 5 → step gap 4 > 3 → gap close
    cfg = make_stream_cfg(tmp_path, stream=StreamConfig(gap_steps=3))
    write_lines(tmp_path / "in" / "d.jsonl",
                ['{"text": "a"}', "", "", "", '{"text": "b"}'])
    sessions, _, _ = run_sessions(cfg)
    assert [(s.cause, len(s.records)) for s in sessions] == [("gap", 1), ("eof", 1)]


def test_cause_gap_steps_ui_pair_index(tmp_path):
    root = tmp_path / "in"
    for index in (1, 2, 7):                            # 7-2=5 > 3 → gap
        root.mkdir(parents=True, exist_ok=True)
        (root / f"uitree_{index}.jsonl").write_text(MINI_TREE, encoding="utf-8")
        (root / f"image_{index}.png").write_bytes(PNG_MAGIC + b"x")
    cfg = make_stream_cfg(tmp_path, modality="ui", stream=StreamConfig(gap_steps=3))
    sessions, _, _ = run_sessions(cfg)
    assert [(s.cause, len(s.records)) for s in sessions] == [("gap", 2), ("eof", 1)]
    assert [r.ref.pair_index for r in sessions[0].records] == [1, 2]


def test_cause_key_change(tmp_path):
    cfg = meta_stream_cfg(tmp_path, key=("meta:device",))
    write_lines(tmp_path / "in" / "d.jsonl",
                ts_lines(1, 2, device="a") + ts_lines(3, 4, device="b"))
    sessions, _, _ = run_sessions(cfg)
    assert [s.cause for s in sessions] == ["key", "eof"]


def test_cause_key_change_source_dir_ui(tmp_path):
    root = tmp_path / "in"
    for sub, index in (("x", 1), ("x", 2), ("y", 3)):
        d = root / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / f"uitree_{index}.jsonl").write_text(MINI_TREE, encoding="utf-8")
        (d / f"image_{index}.png").write_bytes(PNG_MAGIC + b"x")
    cfg = make_stream_cfg(tmp_path, modality="ui",
                   stream=StreamConfig(key=("source_dir",)))
    sessions, _, _ = run_sessions(cfg)
    assert [(s.cause, len(s.records)) for s in sessions] == [("key", 2), ("eof", 1)]
    assert [r.ref.pair_index for r in sessions[1].records] == [3]


def test_cause_max_len_hard_close(tmp_path):
    cfg = meta_stream_cfg(tmp_path, key=(), session_max_len=2)
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(1, 2, 3, 4, 5))
    sessions, _, _ = run_sessions(cfg)
    assert [(s.cause, len(s.records)) for s in sessions] == \
        [("max_len", 2), ("max_len", 2), ("eof", 1)]


def test_cause_max_span(tmp_path):
    # adjacent gaps stay under gap_s; the span cap closes BEFORE the frame
    # that would push first→last beyond the limit, so emitted spans honor it
    cfg = meta_stream_cfg(tmp_path, key=(), gap_s=1000, session_max_span_s=100)
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(0, 90, 180, 270))
    sessions, _, _ = run_sessions(cfg)
    assert [(s.cause, len(s.records)) for s in sessions] == \
        [("max_span", 2), ("eof", 2)]


def test_cause_eof(tmp_path):
    cfg = meta_stream_cfg(tmp_path, key=())
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(1, 2))
    sessions, _, _ = run_sessions(cfg)
    assert [s.cause for s in sessions] == ["eof"]


def test_cause_limit_flushes_tail_and_warns_once(tmp_path, caplog):
    cfg = meta_stream_cfg(tmp_path, key=(), limit=3)
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(1, 2, 3, 4, 5))
    ing = Ingestor(cfg)
    with caplog.at_level(logging.WARNING, logger=".".join(("labelkit", "ingest"))):
        sessions = list(ing.sessions())
    assert [(s.cause, len(s.records)) for s in sessions] == [("limit", 3)]
    assert ing.report.sessions == 1
    # D3: the WARN states budget exhaustion, never claims truncation (exact
    # exhaustion at EOF is indistinguishable without an extra pull).
    warns = [r for r in caplog.records if "--limit 预算耗尽处闭合" in r.message]
    assert len(warns) == 1
    # frame-level limit: only 3 records were ever consumed from the parse stream
    assert ing.report.ingested == 3


def test_limit_not_reached_is_plain_eof(tmp_path, caplog):
    cfg = meta_stream_cfg(tmp_path, key=(), limit=10)
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(1, 2))
    ing = Ingestor(cfg)
    with caplog.at_level(logging.WARNING, logger=".".join(("labelkit", "ingest"))):
        sessions = list(ing.sessions())
    assert [s.cause for s in sessions] == ["eof"]
    assert not [r for r in caplog.records if "截断" in r.message]


def test_text_input_order_file_boundary_closes_session(tmp_path):
    """input_order text: a source-file change always closes the session
    (cause="key") — no timestamp can bridge the file boundary."""
    cfg = make_stream_cfg(tmp_path, stream=StreamConfig())    # order_by="input_order"
    write_lines(tmp_path / "in" / "a.jsonl", ['{"text": "a1"}', '{"text": "a2"}'])
    write_lines(tmp_path / "in" / "b.jsonl", ['{"text": "b1"}'])
    sessions, ing, _ = run_sessions(cfg)
    assert [(s.cause, len(s.records)) for s in sessions] == [("key", 2), ("eof", 1)]
    assert [r.ref.source_file for r in sessions[0].records] == ["a.jsonl", "a.jsonl"]
    assert sessions[1].records[0].ref.source_file == "b.jsonl"


def test_meta_order_file_boundary_is_transparent(tmp_path):
    """meta:* ordering: file boundaries do NOT close sessions (rotated logs)."""
    cfg = meta_stream_cfg(tmp_path, key=())
    write_lines(tmp_path / "in" / "a.jsonl", ts_lines(1, 2))
    write_lines(tmp_path / "in" / "b.jsonl", ts_lines(3, 4))
    sessions, _, _ = run_sessions(cfg)
    assert [(s.cause, len(s.records)) for s in sessions] == [("eof", 4)]


# ── Session object invariants ────────────────────────────────────────────────

def test_session_id_deterministic_and_matches_formula(tmp_path):
    cfg = meta_stream_cfg(tmp_path, key=())
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(1, 2, 3))
    sessions_a, _, _ = run_sessions(cfg)
    sessions_b, _, _ = run_sessions(make_stream_cfg(
        tmp_path, stream=StreamConfig(order_by="meta:ts", key=())))
    assert [s.session_id for s in sessions_a] == [s.session_id for s in sessions_b]
    s = sessions_a[0]
    expected = hashlib.sha256(
        "\n".join(r.id for r in s.records).encode("utf-8")).hexdigest()[:16]
    assert s.session_id == expected
    assert len(s.session_id) == 16


def test_identical_content_sessions_get_distinct_ids(tmp_path):
    """D2 collision guard: record ids are content hashes, so two sessions with
    byte-identical members would derive the same session_id and be silently
    MERGED by M14's session_id regrouping once packed into one batch. The
    first occurrence keeps the plain derivation (id stability); repeats fold a
    per-run ordinal into the hash."""
    cfg = make_stream_cfg(tmp_path, stream=StreamConfig(
        order_by="input_order", key=(), session_max_len=2))
    # 4 byte-identical lines → max_len closes two sessions with identical
    # member id sequences.
    write_lines(tmp_path / "in" / "d.jsonl",
                ['{"text": "同一行"}'] * 4)
    sessions, _, _ = run_sessions(cfg)
    assert len(sessions) == 2
    assert sessions[0].session_id != sessions[1].session_id
    # first occurrence keeps the plain content-derived id
    expected = hashlib.sha256(
        "\n".join(r.id for r in sessions[0].records).encode("utf-8")
    ).hexdigest()[:16]
    assert sessions[0].session_id == expected
    # deterministic across runs
    rerun, _, _ = run_sessions(cfg)
    assert [s.session_id for s in rerun] == [s.session_id for s in sessions]


def test_session_preserves_frame_order(tmp_path):
    cfg = meta_stream_cfg(tmp_path, key=())
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(10, 20, 30, 40))
    sessions, _, _ = run_sessions(cfg)
    (s,) = sessions
    assert [r.ref.line_no for r in s.records] == [1, 2, 3, 4]
    assert [json.loads(json.dumps(r.raw))["ts"] for r in s.records] == [10, 20, 30, 40]
    assert isinstance(s.records, tuple)


def test_session_is_frozen(tmp_path):
    import dataclasses
    cfg = meta_stream_cfg(tmp_path, key=())
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(1))
    sessions, _, _ = run_sessions(cfg)
    with pytest.raises(dataclasses.FrozenInstanceError):
        sessions[0].cause = "gap"  # type: ignore[misc]


def test_mixed_second_ms_iso_timestamps_one_stream(tmp_path):
    """S20 end to end: seconds, milliseconds and ISO forms interleave freely
    as long as the decoded epochs stay monotonic."""
    base = datetime(2026, 6, 30, 10, 12, 0, tzinfo=timezone.utc).timestamp()
    lines = [
        json.dumps({"text": "1", "ts": int(base)}),
        json.dumps({"text": "2", "ts": int((base + 10) * 1000)}),   # ms
        json.dumps({"text": "3", "ts": "2026-06-30T10:12:20Z"}),
        json.dumps({"text": "4", "ts": "2026-06-30T10:12:30"}),     # naive = UTC
    ]
    cfg = meta_stream_cfg(tmp_path, key=(), gap_s=60)
    write_lines(tmp_path / "in" / "d.jsonl", lines)
    sessions, ing, _ = run_sessions(cfg)
    assert ing.report.disorder == 0
    assert [(s.cause, len(s.records)) for s in sessions] == [("eof", 4)]


# ── report counters ──────────────────────────────────────────────────────────

def test_report_sessions_and_disorder_counters(tmp_path):
    cfg = meta_stream_cfg(tmp_path, key=("meta:device",), session_max_len=2)
    write_lines(tmp_path / "in" / "d.jsonl",
                ts_lines(1, 2, 3, device="a")          # max_len 2 → [1,2] + [3
                + ts_lines(2, device="a")              # disorder (2 < 3)
                + ts_lines(7, device="b"))             # key → 3] closes, then [7
    sessions, ing, _ = run_sessions(cfg)
    rep = ing.report
    assert rep.sessions == 3
    assert rep.disorder == 1
    assert rep.bad_input == 1
    assert rep.ingested == 5                           # parse-level count unchanged
    assert [s.cause for s in sessions] == ["max_len", "key", "eof"]
    assert rep.sessions == len(sessions)


# ── S23 scan fusion: IngestPlan.session_lens ────────────────────────────────

def test_scan_session_lens_stream_off_is_empty(tmp_path):
    cfg = make_stream_cfg(tmp_path, segment=SegmentConfig(enabled=False))
    write_lines(tmp_path / "in" / "d.jsonl", ['{"text": "a"}', '{"text": "b"}'])
    plan = Ingestor(cfg).scan()
    assert plan.session_lens == ()
    assert plan.estimated_records == 2


def test_scan_session_lens_text_matches_real_run(tmp_path):
    """The fused single-pass rehearsal (line count + session dry-run) must
    agree with the real sessions() lengths, skipping bad lines and disorder
    records the same way; estimated_records still counts every non-blank line."""
    lines = (ts_lines(0, 100, device="a")
             + ["{not json"]                            # bad line — skipped
             + ts_lines(50, device="a")                 # disorder — skipped
             + ts_lines(600, device="a")                # gap (600-100 > 300)
             + ts_lines(610, device="b"))               # key change
    cfg = meta_stream_cfg(tmp_path)
    write_lines(tmp_path / "in" / "d.jsonl", lines)
    plan = Ingestor(cfg).scan()
    assert plan.estimated_records == 6                  # all non-blank lines
    sessions, _, _ = run_sessions(cfg)
    assert plan.session_lens == tuple(len(s.records) for s in sessions)
    assert plan.session_lens == (2, 1, 1)


def test_scan_session_lens_estimate_false_is_empty(tmp_path):
    cfg = meta_stream_cfg(tmp_path)
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(1, 2))
    plan = Ingestor(cfg).scan(estimate=False)
    assert plan.session_lens == ()
    assert plan.estimated_records == 0


def test_scan_session_lens_ui_from_pairing_table(tmp_path):
    root = tmp_path / "in"
    for index in (1, 2, 3, 10):                        # 10-3=7 > 4 → gap
        root.mkdir(parents=True, exist_ok=True)
        (root / f"uitree_{index}.jsonl").write_text(MINI_TREE, encoding="utf-8")
        (root / f"image_{index}.png").write_bytes(PNG_MAGIC + b"x")
    cfg = make_stream_cfg(tmp_path, modality="ui", stream=StreamConfig(gap_steps=4))
    plan = Ingestor(cfg).scan()
    assert plan.session_lens == (3, 1)
    sessions, _, _ = run_sessions(cfg)
    assert plan.session_lens == tuple(len(s.records) for s in sessions)


def test_scan_session_lens_ui_source_dir_key(tmp_path):
    root = tmp_path / "in"
    for sub, index in (("x", 1), ("x", 2), ("y", 3)):
        d = root / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / f"uitree_{index}.jsonl").write_text(MINI_TREE, encoding="utf-8")
        (d / f"image_{index}.png").write_bytes(PNG_MAGIC + b"x")
    cfg = make_stream_cfg(tmp_path, modality="ui",
                   stream=StreamConfig(key=("source_dir",)))
    plan = Ingestor(cfg).scan()
    assert plan.session_lens == (2, 1)


def test_scan_session_lens_text_max_len_and_multi_file(tmp_path):
    # input_order: file boundary closes; max_len splits inside a file
    cfg = make_stream_cfg(tmp_path,
                   stream=StreamConfig(session_max_len=2))
    write_lines(tmp_path / "in" / "a.jsonl",
                ['{"text": "1"}', '{"text": "2"}', '{"text": "3"}'])
    write_lines(tmp_path / "in" / "b.jsonl", ['{"text": "4"}'])
    plan = Ingestor(cfg).scan()
    assert plan.session_lens == (2, 1, 1)
    sessions, _, _ = run_sessions(cfg)
    assert [(s.cause, len(s.records)) for s in sessions] == \
        [("max_len", 2), ("key", 1), ("eof", 1)]


# ── trace event payload shapes ───────────────────────────────────────────────

def test_segment_session_event_payload_meta_order(tmp_path):
    cfg = meta_stream_cfg(tmp_path, key=(), gap_s=300)
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(10, 20, 900))
    sessions, _, rec = run_sessions(cfg)
    evs = [p for ev, p in rec.events if ev == "segment.session"]
    assert len(evs) == 2
    first_ev = evs[0]
    assert set(first_ev) == {"session_id", "first", "last", "len", "cause"}
    assert first_ev["session_id"] == sessions[0].session_id
    assert first_ev["first"] == 10.0 and first_ev["last"] == 20.0   # epoch floats
    assert first_ev["len"] == 2 and first_ev["cause"] == "gap"
    assert evs[1]["first"] == 900.0 and evs[1]["cause"] == "eof"


def test_segment_session_event_payload_input_order_text(tmp_path):
    cfg = make_stream_cfg(tmp_path, stream=StreamConfig())
    write_lines(tmp_path / "in" / "a.jsonl", ['{"text": "1"}', '{"text": "2"}'])
    _, _, rec = run_sessions(cfg)
    (payload,) = [p for ev, p in rec.events if ev == "segment.session"]
    assert payload["first"] == "a.jsonl:1"             # "file:line_no" presentation
    assert payload["last"] == "a.jsonl:2"
    assert payload["cause"] == "eof"


def test_segment_session_event_payload_ui(tmp_path):
    root = tmp_path / "in"
    for index in (4, 6):
        root.mkdir(parents=True, exist_ok=True)
        (root / f"uitree_{index}.jsonl").write_text(MINI_TREE, encoding="utf-8")
        (root / f"image_{index}.png").write_bytes(PNG_MAGIC + b"x")
    cfg = make_stream_cfg(tmp_path, modality="ui", stream=StreamConfig())
    _, _, rec = run_sessions(cfg)
    (payload,) = [p for ev, p in rec.events if ev == "segment.session"]
    assert payload["first"] == 4 and payload["last"] == 6   # pair_index presentation
    assert payload["len"] == 2


def test_ingest_disorder_event_payload_text(tmp_path):
    cfg = meta_stream_cfg(tmp_path, key=())
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(100, 90))
    _, _, rec = run_sessions(cfg)
    (payload,) = [p for ev, p in rec.events if ev == "ingest.disorder"]
    assert set(payload) == {"file", "line_no", "reason"}
    assert payload["file"] == "d.jsonl" and payload["line_no"] == 2
    assert payload["reason"].startswith("乱序：")
    assert "90" in payload["reason"]                   # timestamp values may appear


def test_disorder_event_emitted_before_fail_raise(tmp_path):
    cfg = meta_stream_cfg(tmp_path, key=(), on_disorder="fail")
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(100, 90))
    ing = Ingestor(cfg)
    rec = StreamEventRecorder()
    ing.metrics = rec
    with pytest.raises(InputError):
        list(ing.sessions())
    assert [ev for ev, _ in rec.events if ev == "ingest.disorder"] == ["ingest.disorder"]


# ── regression anchors ───────────────────────────────────────────────────────

def test_records_path_untouched_by_stream_fields(tmp_path):
    """records() is the non-stream regression anchor: stream config in place
    must not alter it; the new report fields default to zero there."""
    cfg = meta_stream_cfg(tmp_path)
    write_lines(tmp_path / "in" / "d.jsonl", ts_lines(100, 90, 80))  # "disorder" if streamed
    ing = Ingestor(cfg)
    recs = list(ing.records())
    assert len(recs) == 3                              # no monotonicity check here
    assert ing.report.sessions == 0
    assert ing.report.disorder == 0


def test_sessions_reuses_records_semantics_for_bad_lines(tmp_path):
    """Bad lines are handled by the records() layer (bad_line events/counts);
    the disorder layer only sees valid records."""
    cfg = meta_stream_cfg(tmp_path, key=())
    write_lines(tmp_path / "in" / "d.jsonl",
                ts_lines(1) + ["{broken"] + ts_lines(2))
    sessions, ing, rec = run_sessions(cfg)
    assert ing.report.bad_input == 1
    assert ing.report.disorder == 0
    assert [ev for ev, _ in rec.events] == \
        ["ingest.bad_line", "segment.session"]
    assert [len(s.records) for s in sessions] == [2]

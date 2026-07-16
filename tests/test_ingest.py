"""Offline unit tests for M2 ingest (labelkit/ingest.py). Pure I/O logic — no LLM."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from labelkit.config.model import (
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
    StreamConfig,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)
from labelkit.errors import InputError
from labelkit.ingest import Ingestor
from labelkit.types import ImageRef, Record, UITree

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

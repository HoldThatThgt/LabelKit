"""Unit tests for labelkit/types.py (contract §3: UITree.serialize, ImageRef.load_base64,
PipelineItem defaults, Classification (v1.7), frozen-ness)."""
from __future__ import annotations

import base64
import dataclasses
import io
from pathlib import Path

import pytest
from PIL import Image

from labelkit.types import (
    Classification,
    ImageRef,
    PipelineItem,
    Record,
    RecordRef,
    UINode,
    UITree,
    Usage,
)


def _node(
    node_id: str,
    depth: int,
    role: str,
    text: str = "",
    content_desc: str = "",
    bounds: tuple[int, int, int, int] = (0, 0, 10, 10),
    visible: bool = True,
    extra: dict[str, str] | None = None,
    parent_id: str | None = None,
) -> UINode:
    return UINode(
        node_id=node_id,
        parent_id=parent_id,
        depth=depth,
        role=role,
        text=text,
        content_desc=content_desc,
        bounds=bounds,
        visible=visible,
        extra=extra or {},
    )


def _record(rec_id: str = "a" * 16) -> Record:
    return Record(
        id=rec_id,
        modality="text",
        text="hello",
        raw={"text": "hello"},
        ui_tree=None,
        image=None,
        ref=RecordRef(source_file="in.jsonl", line_no=1, pair_index=None, generated_from=()),
    )


# ── UITree.serialize ────────────────────────────────────────────────────────

class TestUITreeSerialize:
    def test_line_format_indent_text_desc_extra(self):
        tree = UITree(nodes=(
            _node("1", 0, "frame", bounds=(0, 0, 1080, 1920)),
            _node("2", 1, "button", text="OK", content_desc="confirm",
                  bounds=(10, 20, 110, 60),
                  extra={"clickable": "true", "enabled": ""}, parent_id="1"),
            _node("3", 2, "label", text="Save", bounds=(12, 24, 100, 50), parent_id="2"),
        ))
        expected = (
            "frame [0,0,1080,1920]\n"
            '  button "OK" desc="confirm" [10,20,110,60] clickable=true\n'
            '    label "Save" [12,24,100,50]'
        )
        assert tree.serialize() == expected

    def test_no_trailing_newline_and_empty_fields_omitted(self):
        tree = UITree(nodes=(_node("1", 0, "view"),))
        out = tree.serialize()
        assert out == "view [0,0,10,10]"
        assert not out.endswith("\n")
        assert '""' not in out and "desc=" not in out

    def test_invisible_nodes_filtered(self):
        tree = UITree(nodes=(
            _node("1", 0, "frame"),
            _node("2", 1, "button", text="hidden", visible=False, parent_id="1"),
            _node("3", 1, "label", text="shown", parent_id="1"),
        ))
        out = tree.serialize()
        assert "hidden" not in out
        assert out == 'frame [0,0,10,10]\n  label "shown" [0,0,10,10]'

    def test_quantization_floor_division(self):
        tree = UITree(nodes=(_node("1", 0, "button", bounds=(10, 20, 110, 63)),))
        assert tree.serialize(quantize_px=4) == "button [2,5,27,15]"
        # quantize_px=0 means no quantization
        assert tree.serialize(quantize_px=0) == "button [10,20,110,63]"

    def test_extra_insertion_order_and_empty_value_skip(self):
        tree = UITree(nodes=(
            _node("1", 0, "input",
                  extra={"b_key": "2", "a_key": "1", "empty": ""}),
        ))
        assert tree.serialize() == "input [0,0,10,10] b_key=2 a_key=1"

    def test_max_chars_no_truncation_when_fits(self):
        tree = UITree(nodes=(_node("1", 0, "view"),))
        full = tree.serialize()
        assert tree.serialize(max_chars=len(full)) == full

    def test_max_chars_truncation_marker_and_budget(self):
        # 10 visible lines of exactly 14 chars each: 'n0 [0,0,10,10]'
        tree = UITree(nodes=tuple(
            _node(str(i), 0, f"n{i}") for i in range(10)
        ))
        full = tree.serialize()
        assert len(full) == 10 * 14 + 9  # 149

        out = tree.serialize(max_chars=60)
        # keep=2 lines (15k+20 <= 60 → k=2), 8 visible nodes omitted
        assert out == "n0 [0,0,10,10]\nn1 [0,0,10,10]\n…(truncated 8 nodes)"
        assert len(out) <= 60

    def test_truncation_counts_only_visible_nodes(self):
        nodes = [_node(str(i), 0, f"n{i}") for i in range(5)]
        nodes.insert(2, _node("x", 0, "ghost", visible=False))
        tree = UITree(nodes=tuple(nodes))
        out = tree.serialize(max_chars=40)
        # 15k+20 <= 40 → k=1; omitted visible = 5 - 1 = 4 (ghost not counted)
        assert out == "n0 [0,0,10,10]\n…(truncated 4 nodes)"

    def test_truncation_marker_alone_when_budget_tiny(self):
        tree = UITree(nodes=tuple(_node(str(i), 0, f"n{i}") for i in range(3)))
        out = tree.serialize(max_chars=5)
        assert out == "…(truncated 3 nodes)"


# ── ImageRef.load_base64 ────────────────────────────────────────────────────

class TestImageRefLoadBase64:
    def _write_png(self, path: Path, size: tuple[int, int]) -> bytes:
        Image.new("RGB", size, (255, 0, 0)).save(path, format="PNG")
        return path.read_bytes()

    def test_png_no_resize_returns_original_bytes(self, tmp_path: Path):
        p = tmp_path / "img.png"
        raw = self._write_png(p, (200, 100))
        ref = ImageRef(path=p, format="png", size_bytes=len(raw))
        media_type, b64 = ref.load_base64(max_px=300)
        assert media_type == "image/png"
        assert base64.b64decode(b64) == raw

    def test_png_downscaled_when_long_edge_exceeds_max_px(self, tmp_path: Path):
        p = tmp_path / "img.png"
        raw = self._write_png(p, (200, 100))
        ref = ImageRef(path=p, format="png", size_bytes=len(raw))
        media_type, b64 = ref.load_base64(max_px=50)
        assert media_type == "image/png"
        with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
            assert im.format == "PNG"
            assert im.size == (50, 25)  # proportional: long edge → 50

    def test_jpeg_media_type_and_resize(self, tmp_path: Path):
        p = tmp_path / "img.jpg"
        Image.new("RGB", (100, 40), (0, 128, 0)).save(p, format="JPEG")
        ref = ImageRef(path=p, format="jpeg", size_bytes=p.stat().st_size)
        media_type, b64 = ref.load_base64(max_px=50)
        assert media_type == "image/jpeg"
        with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
            assert im.format == "JPEG"
            assert im.size == (50, 20)

    def test_exact_max_px_not_resized(self, tmp_path: Path):
        p = tmp_path / "img.png"
        raw = self._write_png(p, (64, 32))
        ref = ImageRef(path=p, format="png", size_bytes=len(raw))
        _, b64 = ref.load_base64(max_px=64)  # long edge == max_px → untouched
        assert base64.b64decode(b64) == raw


# ── PipelineItem defaults / frozen-ness ─────────────────────────────────────

class TestPipelineItem:
    def test_defaults(self):
        item = PipelineItem(record=_record())
        assert item.status == "active"
        assert item.classification is None
        assert item.dedup is None
        assert item.scores == {}
        assert item.annotation is None
        assert item.verification is None
        assert item.errors == []

    def test_default_containers_are_independent(self):
        a = PipelineItem(record=_record("a" * 16))
        b = PipelineItem(record=_record("b" * 16))
        a.scores["__aggregate__"] = object()  # type: ignore[assignment]
        a.errors.append(object())             # type: ignore[arg-type]
        a.classification = Classification(label="faq", labels=("faq",),
                                          source="llm", detail={})
        assert b.scores == {} and b.errors == []
        assert b.classification is None       # v1.7: new field not shared either

    def test_item_is_mutable(self):
        item = PipelineItem(record=_record())
        item.status = "dropped_dup"
        assert item.status == "dropped_dup"


class TestClassification:
    def test_is_frozen(self):
        c = Classification(label="faq", labels=("faq",), source="llm", detail={})
        with pytest.raises(dataclasses.FrozenInstanceError):
            c.label = "other"  # type: ignore[misc]

    def test_all_four_fields_required_no_defaults(self):
        # Spec §4.1 shape: label / labels / source / detail, none defaulted.
        with pytest.raises(TypeError):
            Classification(label="faq", labels=("faq",), source="llm")  # type: ignore[call-arg]

    def test_field_values_round_trip(self):
        c = Classification(label="faq", labels=("faq", "chitchat"), source="inherited",
                           detail={"reason": "既问且聊"})
        assert (c.label, c.labels, c.source, c.detail) == (
            "faq", ("faq", "chitchat"), "inherited", {"reason": "既问且聊"})


class TestFrozen:
    def test_record_is_frozen(self):
        rec = _record()
        with pytest.raises(dataclasses.FrozenInstanceError):
            rec.text = "changed"  # type: ignore[misc]

    def test_other_shared_types_frozen(self):
        ref = RecordRef("f", 1, None, ())
        with pytest.raises(dataclasses.FrozenInstanceError):
            ref.line_no = 2  # type: ignore[misc]
        node = _node("1", 0, "view")
        with pytest.raises(dataclasses.FrozenInstanceError):
            node.role = "button"  # type: ignore[misc]

    def test_usage_add(self):
        assert Usage(1, 2) + Usage(10, 20) == Usage(11, 22)

"""Unit tests for labelkit/common/contracts/types.py (contract §3: UITree.serialize, ImageRef.load_base64,
PipelineItem defaults, Classification (v1.7), stream types + frame helpers (v1.8),
frozen-ness)."""
from __future__ import annotations

import base64
import dataclasses
import io
from pathlib import Path
from typing import get_args

import pytest
from PIL import Image

from labelkit.common.contracts.types import (
    Classification,
    ImageRef,
    PipelineItem,
    Record,
    RecordRef,
    Status,
    Transition,
    UINode,
    UITree,
    Usage,
    VerificationResult,
    digest_is_poor,
    frame_digest,
    tree_diff,
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


# ── v1.8: Status full set / Transition / sequence Record / stream envelopes ─

class TestStatusEnum:
    def test_status_literal_full_set(self):
        # spec §4.1 (v1.8): exactly seven values — absorbed / dropped_noise joined
        assert frozenset(get_args(Status)) == frozenset({
            "active", "dropped_dup", "dropped_lowq", "dropped_verify",
            "failed", "absorbed", "dropped_noise"})
        assert len(get_args(Status)) == 7


class TestTransition:
    def test_is_frozen(self):
        t = Transition(index=0, action={"action_type": "click"}, model="m",
                       attempts=1, detail={})
        with pytest.raises(dataclasses.FrozenInstanceError):
            t.index = 1  # type: ignore[misc]

    def test_all_five_fields_required_no_defaults(self):
        with pytest.raises(TypeError):
            Transition(index=0, action={}, model="m", attempts=1)  # type: ignore[call-arg]

    def test_field_values_round_trip(self):
        t = Transition(index=2,
                       action={"action_type": "other", "target": None,
                               "value": None, "description": "未知动作"},
                       model="glm-5.2", attempts=3,
                       detail={"kind": "extraction_invalid", "message": "L3 耗尽"})
        assert (t.index, t.model, t.attempts) == (2, "glm-5.2", 3)
        assert t.action["action_type"] == "other"
        assert t.detail["kind"] == "extraction_invalid"


class TestRecordSequenceFields:
    def test_defaults_are_single_and_empty(self):
        rec = _record()
        assert rec.kind == "single"
        assert rec.members == ()

    def test_sequence_record_construction(self):
        m1, m2 = _record("a" * 16), _record("b" * 16)
        seq = Record(id="c" * 16, modality="text", text=None, raw=None,
                     ui_tree=None, image=None, ref=m1.ref,
                     kind="sequence", members=(m1, m2))
        assert seq.kind == "sequence"
        assert seq.members == (m1, m2)
        assert seq.text is None and seq.raw is None
        with pytest.raises(dataclasses.FrozenInstanceError):
            seq.members = ()  # type: ignore[misc]


class TestStreamEnvelopeFields:
    def test_pipeline_item_stream_defaults(self):
        item = PipelineItem(record=_record())
        assert item.transitions is None
        assert item.session_id is None

    def test_pipeline_item_stream_fields_writable(self):
        item = PipelineItem(record=_record())
        item.session_id = "s-0001"
        item.transitions = (Transition(index=0, action={}, model="m",
                                       attempts=1, detail={}),)
        assert item.session_id == "s-0001"
        assert len(item.transitions) == 1

    def test_verification_result_defects_default(self):
        vr = VerificationResult(verdict="pass", rounds=1, critiques=())
        assert vr.defects == ()
        vr2 = VerificationResult(verdict="fail", rounds=2, critiques=(),
                                 defects=({"kind": "missing_tail", "members": None,
                                           "position": "tail", "detail": "缺尾帧"},))
        assert vr2.defects[0]["kind"] == "missing_tail"


# ── v1.8 shared frame helpers: frame_digest / digest_is_poor / tree_diff ────

def _ui_record(*nodes: UINode, rec_id: str = "d" * 16) -> Record:
    return Record(id=rec_id, modality="ui", text=None, raw=None,
                  ui_tree=UITree(nodes=tuple(nodes)), image=None,
                  ref=RecordRef("a/uitree_1.jsonl", None, 1, ()))


class TestFrameDigest:
    def test_ui_with_package_title_and_salient(self):
        rec = _ui_record(
            _node("1", 0, "FrameLayout", extra={"package": "com.example.mail"}),
            _node("2", 1, "TextView", text="收件箱"),
            _node("3", 1, "Button", text="写邮件"),
            _node("4", 1, "TextView", text="隐藏", visible=False),
        )
        assert frame_digest(rec, 400) == "[com.example.mail] 收件箱｜收件箱、*写邮件"

    def test_ui_with_activity_inserted_after_app(self):
        rec = _ui_record(
            _node("1", 0, "FrameLayout",
                  extra={"package": "com.foo", "activity": "MainActivity"}),
            _node("2", 1, "TextView", text="首页"),
        )
        assert frame_digest(rec, 400) == "[com.foo activity=MainActivity] 首页｜首页"

    def test_ui_without_package_omits_head_segment(self):
        rec = _ui_record(
            _node("1", 0, "FrameLayout"),
            _node("2", 1, "TextView", text="标题"),
        )
        assert frame_digest(rec, 400) == "标题｜标题"

    def test_alternate_app_keys_and_interactive_star(self):
        rec = _ui_record(
            _node("1", 0, "FrameLayout", extra={"pkg": "com.bar"}),
            _node("2", 1, "ImageButton", content_desc="返回"),
            _node("3", 1, "EditText", text="请输入手机号"),
        )
        # no visible non-empty text before EditText → title = EditText's text
        assert frame_digest(rec, 400) == "[com.bar] 请输入手机号｜*返回、*请输入手机号"

    def test_salient_deduplicates_in_order(self):
        rec = _ui_record(
            _node("1", 0, "TextView", text="确定"),
            _node("2", 0, "TextView", text="确定"),
            _node("3", 0, "TextView", text="取消"),
        )
        assert frame_digest(rec, 400) == "确定｜确定、取消"

    def test_barren_tree_digest_empty(self):
        rec = _ui_record(_node("1", 0, "FrameLayout"), _node("2", 1, "View"))
        assert frame_digest(rec, 400) == ""

    def test_text_modality_truncates_plain(self):
        rec = _record()                            # text = "hello"
        assert frame_digest(rec, 400) == "hello"
        assert frame_digest(rec, 3) == "hel"

    def test_ui_truncation_appends_ellipsis_within_budget(self):
        rec = _ui_record(_node("1", 0, "TextView", text="x" * 50))
        digest = frame_digest(rec, 20)
        assert len(digest) == 20
        assert digest.endswith("…")


class TestDigestIsPoor:
    def test_barren_ui_tree_is_poor(self):
        rec = _ui_record(_node("1", 0, "FrameLayout", extra={"package": "com.foo"}))
        assert digest_is_poor(rec) is True

    def test_visible_text_or_desc_not_poor(self):
        assert digest_is_poor(
            _ui_record(_node("1", 0, "TextView", text="登录页面标题"))) is False
        assert digest_is_poor(
            _ui_record(_node("1", 0, "View", content_desc="用户头像图标按钮"))) is False

    def test_short_digest_is_poor(self):
        # Second disjunct (spec §4 poverty judgment): a visible text node exists
        # but the rendered digest is < 8 chars — still poor.
        assert digest_is_poor(_ui_record(_node("1", 0, "TextView", text="hi"))) is True

    def test_invisible_text_still_poor(self):
        rec = _ui_record(_node("1", 0, "TextView", text="hi", visible=False))
        assert digest_is_poor(rec) is True

    def test_text_modality_never_poor(self):
        assert digest_is_poor(_record()) is False


def _tree(*nodes: UINode) -> UITree:
    return UITree(nodes=tuple(nodes))


class TestTreeDiff:
    def test_identical_structure_zero_changes(self):
        # node_id differs on purpose — it is NOT a cross-frame identity (S13)
        a = _tree(_node("1", 0, "Frame"), _node("2", 1, "TextView", text="hi"))
        b = _tree(_node("9", 0, "Frame"), _node("8", 1, "TextView", text="hi"))
        assert tree_diff(a, b, 0) == {
            "added": 0, "removed": 0, "text_changed": 0,
            "change_ratio": 0.0, "app_changed": False, "title_changed": False}

    def test_full_screen_replacement_counts_both_sides(self):
        a = _tree(_node("1", 0, "ListView", text="列表", bounds=(0, 0, 100, 100)))
        b = _tree(_node("1", 0, "WebView", text="网页", bounds=(0, 0, 200, 200)),
                  _node("2", 1, "Button", text="确定", bounds=(0, 0, 50, 50)))
        d = tree_diff(a, b, 0)
        assert (d["removed"], d["added"], d["text_changed"]) == (1, 2, 0)
        assert d["change_ratio"] == pytest.approx(3 / 2)
        assert d["title_changed"] is True

    def test_text_changed_and_ratio(self):
        a = _tree(_node("1", 0, "Frame"), _node("2", 1, "EditText", text=""),
                  _node("3", 1, "Button", text="登录"))
        b = _tree(_node("1", 0, "Frame"),
                  _node("2", 1, "EditText", text="13800138000"),
                  _node("3", 1, "Button", text="登录"))
        d = tree_diff(a, b, 0)
        assert (d["added"], d["removed"], d["text_changed"]) == (0, 0, 1)
        assert d["change_ratio"] == pytest.approx(1 / 3)

    def test_swapped_texts_within_key_are_multiset_matched(self):
        # lower-bound semantics: identical (text, desc) multisets pair up fully
        a = _tree(_node("1", 0, "TextView", text="A"), _node("2", 0, "TextView", text="B"))
        b = _tree(_node("1", 0, "TextView", text="B"), _node("2", 0, "TextView", text="A"))
        d = tree_diff(a, b, 0)
        assert (d["added"], d["removed"], d["text_changed"]) == (0, 0, 0)

    def test_none_boundaries_count_as_all_added_or_removed(self):
        b = _tree(_node("1", 0, "Frame"), _node("2", 1, "TextView", text="hi"))
        d = tree_diff(None, b, 0)
        assert (d["added"], d["removed"]) == (2, 0)
        assert d["change_ratio"] == pytest.approx(1.0)
        d2 = tree_diff(b, None, 0)
        assert (d2["added"], d2["removed"]) == (0, 2)
        assert d2["change_ratio"] == pytest.approx(1.0)
        d3 = tree_diff(None, None, 0)
        assert d3 == {"added": 0, "removed": 0, "text_changed": 0,
                      "change_ratio": 0.0, "app_changed": False,
                      "title_changed": False}

    def test_quantization_merges_nearby_bounds(self):
        a = _tree(_node("1", 0, "Button", text="OK", bounds=(10, 20, 110, 60)))
        b = _tree(_node("1", 0, "Button", text="OK", bounds=(11, 21, 111, 61)))
        exact = tree_diff(a, b, 0)
        assert exact["added"] == 1 and exact["removed"] == 1
        quantized = tree_diff(a, b, 16)
        assert (quantized["added"], quantized["removed"],
                quantized["text_changed"]) == (0, 0, 0)

    def test_app_changed_via_extra_keys(self):
        a = _tree(_node("1", 0, "Frame", extra={"package": "com.a"}))
        b = _tree(_node("1", 0, "Frame", extra={"package": "com.b"}))
        d = tree_diff(a, b, 0)
        assert d["app_changed"] is True
        assert (d["added"], d["removed"], d["text_changed"]) == (0, 0, 0)

    def test_invisible_nodes_ignored(self):
        a = _tree(_node("1", 0, "Frame"))
        b = _tree(_node("1", 0, "Frame"),
                  _node("2", 1, "TextView", text="ghost", visible=False))
        d = tree_diff(a, b, 0)
        assert d["added"] == 0 and d["change_ratio"] == 0.0

from __future__ import annotations

import pytest

from labelkit.common.config.model import SegmentConfig
from labelkit.common.contracts.types import Annotation, CapacityCut, Classification, SequenceBounds, SequenceCapacity, Usage
from tests.operators.test_emitter import (
    FRAME_SCHEMA, frame_annotate_cfg, frame_classify_cfg, make_cfg, make_item, make_record,
    make_seq_record, read_jsonl, run_emitter,
)


def test_repeated_content_occurrences_keep_distinct_frame_products_in_output(tmp_path):
    member = make_record("a" * 16)
    item = make_item(record=make_seq_record([member, member]))
    item.member_positions = (2, 8)
    item.capacity = SequenceCapacity(SequenceBounds(0, 10), root_id=item.record.id)
    item.member_classifications = {
        2: Classification("task_request", ("task_request",), "llm", {}),
        8: Classification("other", ("other",), "llm", {}),
    }
    item.member_annotations = {
        2: Annotation({"intent": "first", "entities": []}, "local", 1, Usage()),
        8: Annotation({"intent": "second", "entities": []}, "local", 1, Usage()),
    }
    cfg = make_cfg(tmp_path, segment=SegmentConfig(enabled=True), frame_classify=frame_classify_cfg(),
                   frame_annotate=frame_annotate_cfg(), frame_schema=FRAME_SCHEMA)
    _, result = run_emitter(cfg, [item])
    assert result.emitted == 1
    stream = read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]["stream"]
    assert stream["member_ids"] == [member.id, member.id]
    assert stream["member_positions"] == [2, 8]
    assert stream["capacity"] is None
    assert [row["label"] for row in stream["members"]] == ["task_request", "other"]
    assert [row["annotation"]["intent"] for row in stream["members"]] == ["first", "second"]
    assert [row["status"] for row in stream["members"]] == ["annotated", "annotated"]
    assert "session_split" not in stream


@pytest.mark.parametrize("sealed", [False, True])
def test_capacity_cut_output_has_exact_position_bounds_and_lineage(tmp_path, sealed):
    item = make_item(record=make_seq_record([make_record()]))
    before = CapacityCut(1, 2, "annotate", "local", "reactive")
    after = CapacityCut(8, 9, "quality", "judge", "precheck")
    item.member_positions = (4,)
    item.capacity = SequenceCapacity(SequenceBounds(2, 9, before, after), sealed, "root", "direct-parent")
    cfg = make_cfg(tmp_path, segment=SegmentConfig(enabled=True))
    run_emitter(cfg, [item])
    capacity = read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]["stream"]["capacity"]
    assert list(capacity) == ["sealed", "allowed_positions", "before", "after", "root_id", "parent_id"]
    assert capacity["sealed"] is sealed
    assert capacity["allowed_positions"] == [2, 9]
    assert (capacity["root_id"], capacity["parent_id"]) == ("root", "direct-parent")
    assert capacity["before"] == {"left_position": 1, "right_position": 2,
                                   "stage": "annotate", "profile": "local", "phase": "reactive"}
    assert list(capacity["before"]) == ["left_position", "right_position", "stage", "profile", "phase"]
    assert capacity["after"]["right_position"] == 9

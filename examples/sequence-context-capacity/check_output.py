"""独立核验 process 会话工件；不导入 LabelKit 实现或生成模型。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def read_rows(path):
    """读取 JSONL 正式工件。"""
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def check_structure(output, expected_positions):
    """按输入出现位置核对唯一归属、顺序、容量边界和正式行数。"""
    rows = read_rows(output)
    positions = []
    for row in rows:
        stream = row["_meta"]["stream"]
        member_positions = stream["member_positions"]
        assert member_positions == sorted(set(member_positions))
        assert len(member_positions) == stream["member_count"] == len(stream["member_ids"])
        positions.extend(member_positions)
        capacity = stream["capacity"]
        if capacity is not None:
            lower, upper = capacity["allowed_positions"]
            assert all(lower <= position < upper for position in member_positions)
            assert capacity["root_id"]
            for side in ("before", "after"):
                cut = capacity[side]
                if cut is not None:
                    assert cut["left_position"] < cut["right_position"]
                    assert cut["phase"] in {"precheck", "reactive"}
    assert sorted(positions) == list(expected_positions), positions
    report_path = Path(output).with_suffix(".report.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["counts"]["emitted"] == len(rows)
    assert report["run"]["exit_code"] == 0
    assert "capacity" in report["stream"]
    return rows, report


def check_text(output):
    """用固定业务答案检查中间改色消息和最后修改，而非仅检查 Schema。"""
    rows, report = check_structure(output, range(6))
    assert len(rows) == 1
    assert {key: rows[0][key] for key in ("request_id", "destination", "color", "quantity")} == {
        "request_id": "RIVER-42", "destination": "东仓", "color": "蓝色", "quantity": 7,
    }
    assert rows[0]["_meta"]["stream"]["capacity"] is None
    assert report["counts"]["failed"] == 0
    return rows, report


def semantic_signature(rows):
    """排除时间、耗时、模型自然语言，保留确定性业务与成员结构。"""
    return [{"answer": {key: value for key, value in row.items() if key != "_meta"},
             "positions": row["_meta"]["stream"]["member_positions"],
             "capacity": row["_meta"]["stream"]["capacity"]} for row in rows]


def check_ui(output):
    """图像中间帧的唯一异色必须参与最终答案。"""
    rows, report = check_structure(output, range(25))
    assert len(rows) == 1 and rows[0]["marker_color"].lower() == "green" and rows[0]["frame_count"] == 25
    return rows, report


def check_stitch(output):
    """按冻结位置与业务值检查跨中断恢复，合并计数必须真实存在。"""
    rows, report = check_structure(output, range(8))
    dispatch = [row for row in rows if row["task"] == "dispatch"]
    assert len(dispatch) == 1
    row = dispatch[0]
    assert {key: row[key] for key in ("request_id", "destination", "color", "quantity")} == {
        "request_id": "RIVER-42", "destination": "东仓", "color": "蓝色", "quantity": 7}
    assert row["_meta"]["stream"]["member_positions"] == [0, 1, 2, 5, 6, 7]
    assert len(row["_meta"]["stream"]["fragments"]) >= 2
    assert report["counts"]["stitched"] >= 1 and report["stream"]["stitch"]["judgments"] >= 1
    return rows, report


def check_partition_boundaries(rows, frame_count):
    """逐段核对完整连续夹具的半开范围与共享切点，不接受缺边界或错位边界。"""
    streams = [row["_meta"]["stream"] for row in rows]
    assert [stream["member_positions"][0] for stream in streams] == sorted(
        stream["member_positions"][0] for stream in streams)
    for index, stream in enumerate(streams):
        capacity = stream["capacity"]
        before, after = capacity["before"], capacity["after"]
        lower = streams[index]["member_positions"][0] if index else 0
        upper = streams[index + 1]["member_positions"][0] if index + 1 < len(streams) else frame_count
        assert capacity["allowed_positions"] == [lower, upper]
        assert stream["member_positions"] == list(range(lower, upper))
        if index == 0:
            assert before is None
        else:
            assert before == streams[index - 1]["capacity"]["after"]
            assert (before["left_position"], before["right_position"]) == (lower - 1, lower)
        if index + 1 == len(streams):
            assert after is None
        else:
            assert (after["left_position"], after["right_position"]) == (upper - 1, upper)
            assert after["profile"] == "default" and after["stage"] in {"annotate", "verify"}


def check_capacity(output, case):
    """检查完整出现位置、不可重并边界、失败次数和先前会话交付。"""
    rows, report = check_structure(output, range(6 if case == "static" else 2))
    assert all(row["request_id"] == "CAPACITY" for row in rows)
    capacity = report["stream"]["capacity"]
    if case == "minimum":
        assert len(rows) == 1 and report["counts"]["failed"] >= 1 and capacity["minimum_failures"] == 1
    else:
        assert len(rows) >= 2 and report["counts"]["failed"] == 0
        assert capacity["sealed"] >= 1 and capacity["minimum_failures"] == 0
        check_partition_boundaries(rows, 6 if case == "static" else 2)
    if case == "reactive":
        assert all(row["_meta"]["stream"]["capacity"]["sealed"] for row in rows)
        assert capacity["splits"] >= 1 and capacity["recomputations"] >= 1
    elif case == "static":
        assert [row["_meta"]["stream"]["capacity"]["sealed"] for row in rows] == [True] * (len(rows) - 1) + [False]
        assert capacity["sealed"] == capacity["splits"] == len(rows) - 1
        assert all(row["_meta"]["stream"]["capacity"]["after"]["phase"] == "precheck" for row in rows[:-1])
        assert capacity["recomputations"] == 0
    return rows, report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--case", choices=("text", "ui", "stitch", "static", "reactive", "minimum"), default="text")
    args = parser.parse_args()
    if args.case in {"static", "reactive", "minimum"}:
        rows, report = check_capacity(args.output, args.case)
    else:
        rows, report = {"text": check_text, "ui": check_ui, "stitch": check_stitch}[args.case](args.output)
    print(json.dumps({"checked_rows": len(rows), "case": args.case, "semantic_oracle": "passed"}))

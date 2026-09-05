"""独立检查标注后处理示例的最终工件，不调用工程后处理函数。"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parent


def _load_json(path: str | Path) -> dict:
    """读取一个 UTF-8 JSON object。

    @param path JSON 文件路径。
    @return 文件中的 JSON object。
    """
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_jsonl(path: str | Path) -> list[dict]:
    """读取 UTF-8 JSONL objects。

    @param path JSONL 文件路径。
    @return 文件中的 JSON objects。
    """
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def _validate(value: Mapping, schema: Mapping, surface: str) -> None:
    """用完整 Schema 独立终验一个输出对象。

    @param value 待验证的输出对象。
    @param schema 完整输出 Schema。
    @param surface 错误消息中的验证面名称。
    @return None。
    @raises AssertionError 输出不满足完整 Schema。
    """
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda item: tuple(str(part) for part in item.path),
    )
    if errors:
        raise AssertionError(f"{surface} fails complete schema: {errors[0].message}")


def _canonical_plate(value: str) -> str:
    """以独立实现规范化检查器看到的车牌字符串。

    @param value 原文中的车牌字符串。
    @return 去分隔符并转大写的规范值。
    """
    return "".join(char.upper() for char in value if char not in " \t\r\n·-")


def _plate_source(row: Mapping, sources: Mapping[str, dict]) -> dict:
    """按公开 passthrough case_id 找到输入记录。

    @param row 最终普通标注行。
    @param sources 以 case_id 索引的原始输入。
    @return 与最终行对应的原始输入。
    @raises AssertionError 最终行引用未知 case_id。
    """
    case_id = row["_meta"]["source"]["fields"]["case_id"]
    if case_id not in sources:
        raise AssertionError(f"unknown ordinary case_id: {case_id}")
    return sources[case_id]


def _check_plate_row(row: Mapping, source: Mapping, expected: Sequence[str], schema: Mapping) -> dict:
    """以原文切片和预期集合检查一条普通标注。

    @param row 最终普通标注行。
    @param source 对应的原始输入。
    @param expected 独立 oracle 中的规范实体序列。
    @param schema 完整普通标注 Schema。
    @return 可跨运行比较的实体、区间及数量。
    @raises AssertionError 标注不满足独立预期。
    """
    annotation = {key: value for key, value in row.items() if key != "_meta"}
    _validate(annotation, schema, "ordinary annotation")
    entities = annotation["entities"]
    actual = [entity["value"] for entity in entities]
    if actual != expected or annotation["entity_count"] != len(expected):
        raise AssertionError("ordinary entity values or count differ from the input oracle")
    text = source["text"]
    previous_end = -1
    spans = []
    for entity in entities:
        start, end = entity["start"], entity["end"]
        if not 0 <= start < end <= len(text) or start < previous_end:
            raise AssertionError("ordinary entity offsets are invalid or out of order")
        if _canonical_plate(text[start:end]) != entity["value"]:
            raise AssertionError("ordinary entity offsets do not select their normalized value")
        spans.append([start, end])
        previous_end = end
    return {"entities": actual, "spans": spans, "entity_count": len(entities)}


def check_ordinary(output_path: str | Path, input_path: str | Path,
                   oracle_path: str | Path) -> dict:
    """独立检查普通记录输出并返回可跨运行比较的代码字段。

    @param output_path 最终普通标注 JSONL 路径。
    @param input_path 原始输入 JSONL 路径。
    @param oracle_path 独立实体 oracle JSON 路径。
    @return 行数及各 case 的确定性字段。
    @raises AssertionError 工件与输入或 oracle 不一致。
    """
    rows = _load_jsonl(output_path)
    inputs = _load_jsonl(input_path)
    sources = {row["case_id"]: row for row in inputs}
    oracles = _load_json(oracle_path)
    schema = _load_json(ROOT / "schemas" / "plate-annotation.json")
    if len(rows) != len(sources) or set(oracles) != set(sources):
        raise AssertionError("ordinary output row count differs from input")
    checked = {}
    for row in rows:
        source = _plate_source(row, sources)
        expected = oracles[source["case_id"]]
        checked[source["case_id"]] = _check_plate_row(row, source, expected, schema)
    if set(checked) != set(sources):
        raise AssertionError("ordinary output does not cover every input case")
    return {"rows": len(rows), "cases": checked}


def _canonical_delivery_row(row: Mapping) -> bytes:
    """独立实现交付摘要使用的去墙钟 canonical JSON。

    @param row 最终 main 或 stream 行。
    @return 移除墙钟字段后的 canonical JSON bytes。
    """
    value = copy.deepcopy(row)
    run = value.get("_meta", {}).get("run")
    if isinstance(run, dict):
        for key in ("started_at", "finished_at", "duration_ms"):
            run.pop(key, None)
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _delivery_digest(main: Sequence[Mapping], stream: Sequence[Mapping]) -> str:
    """按公开 framing 独立重算 main 与 stream 的交付摘要。

    @param main 最终 main 行序列。
    @param stream 最终 stream 行序列。
    @return 交付摘要的十六进制 SHA-256。
    """
    digest = hashlib.sha256(b"labelkit:v1.20:delivery\n")
    for row in (*main, *stream):
        payload = _canonical_delivery_row(row)
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b":")
        digest.update(payload)
    return digest.hexdigest()


def _check_frame(row: Mapping, schema: Mapping) -> None:
    """以最终 payload 独立检查一条帧标注的语义与代码字段。

    @param row 最终 stream 行。
    @param schema 完整帧标注 Schema。
    @return None。
    @raises AssertionError 帧语义或代码字段与 payload 不一致。
    """
    payload = row["payload"]
    annotation = row["_meta"]["annotation"]
    _validate(annotation, schema, "frame annotation")
    if annotation["request_id"] != payload["request_id"]:
        raise AssertionError("frame request_id differs from final payload")
    if annotation["observed_status"] != payload["status"]:
        raise AssertionError("frame observed_status differs from final payload")
    if annotation["request_id_length"] != len(annotation["request_id"]):
        raise AssertionError("frame request_id_length differs from the checked request_id")
    if annotation["utterance_length"] != len(payload["utterance"]):
        raise AssertionError("frame utterance_length differs from the final payload")
    if annotation["summary_length"] != len(annotation["summary"]):
        raise AssertionError("frame summary_length differs from the final summary")


def _check_sequence_rows(main: list[dict], stream: list[dict]) -> list[dict]:
    """检查一个三事件 primary、一个 replay 及两份成员视图一致性。

    @param main 最终 main 行。
    @param stream 最终 stream 行。
    @return 按交付顺序排列的三个 primary 行。
    @raises AssertionError 序列布局或成员视图不一致。
    """
    if len(main) != 1 or len(stream) != 6:
        raise AssertionError("sequence example must deliver one main row and six stream rows")
    primary = [row for row in stream if "replay_sequence_id" not in row["_meta"]["event"]]
    replay = [row for row in stream if "replay_sequence_id" in row["_meta"]["event"]]
    roles = [row["_meta"]["event"]["role"] for row in primary]
    if roles != ["request", "acknowledge", "confirm"] or len(replay) != 3:
        raise AssertionError("sequence primary or replay layout differs from the declared plan")
    primary_by_id = {row["_meta"]["event"]["event_id"]: row for row in primary}
    for row in replay:
        source = primary_by_id[row["_meta"]["event"]["duplicate_of_event_id"]]
        if row["_meta"]["annotation"] != source["_meta"]["annotation"]:
            raise AssertionError("replay frame annotation differs from its primary source")
    member_by_id = {item["id"]: item for item in main[0]["_meta"]["stream"]["members"]}
    for row in primary:
        event_id = row["_meta"]["event"]["event_id"]
        if member_by_id[event_id]["annotation"] != row["_meta"]["annotation"]:
            raise AssertionError("main member annotation differs from its primary stream row")
    return primary


def _check_sequence_annotation(annotation: Mapping, primary: list[dict], schema: Mapping) -> None:
    """以 primary payload 检查序列语义与代码长度。

    @param annotation 最终序列标注。
    @param primary 三个 primary stream 行。
    @param schema 完整序列标注 Schema。
    @return None。
    @raises AssertionError 序列语义或代码字段与 payload 不一致。
    """
    _validate(annotation, schema, "sequence annotation")
    request_ids = {row["payload"]["request_id"] for row in primary}
    confirmation = next(row for row in primary if row["_meta"]["event"]["role"] == "confirm")
    expected = {
        "intent": "book_train_ticket",
        "outcome": "ticketed",
        "request_id": next(iter(request_ids)) if len(request_ids) == 1 else None,
        "ticket_id": confirmation["payload"]["ticket_id"],
    }
    if any(annotation[key] != value for key, value in expected.items()):
        raise AssertionError("sequence semantics differ from the final primary payloads")
    if annotation["summary_length"] != len(annotation["summary"]):
        raise AssertionError("sequence summary_length differs from the final summary")


def _check_manifest(paths: Mapping[str, Path], main: list[dict], stream: list[dict], report: dict) -> None:
    """独立核对 manifest-last、文件哈希、行数与交付摘要。

    @param paths 最终交付路径。
    @param main 最终 main 行。
    @param stream 最终 stream 行。
    @param report 最终 report object。
    @return None。
    @raises AssertionError manifest 与最终工件不一致。
    """
    manifest = _load_json(paths["manifest"])
    if manifest["artifacts_committed"] is not True:
        raise AssertionError("sequence manifest is not committed")
    for name, rows in (("main", main), ("stream", stream)):
        path = paths[name]
        if manifest[name]["rows"] != len(rows):
            raise AssertionError(f"manifest {name} row count differs")
        if manifest[name]["sha256"] != hashlib.sha256(path.read_bytes()).hexdigest():
            raise AssertionError(f"manifest {name} sha256 differs")
    report_hash = hashlib.sha256(paths["report"].read_bytes()).hexdigest()
    if manifest["report"]["sha256"] != report_hash:
        raise AssertionError("manifest report sha256 differs")
    if manifest["delivery_digest"] != _delivery_digest(main, stream):
        raise AssertionError("manifest delivery_digest differs")
    if manifest["run_id"] != report["generate"]["sequence"]["run_id"]:
        raise AssertionError("manifest and report run_id differ")


def check_sequence(output_path: str | Path, stream_path: str | Path,
                   report_path: str | Path, manifest_path: str | Path) -> dict:
    """独立检查带帧标注和 replay 的 sequence 最终交付。

    @param output_path 最终 main JSONL 路径。
    @param stream_path 最终 stream JSONL 路径。
    @param report_path 最终 report JSON 路径。
    @param manifest_path 最终 manifest JSON 路径。
    @return 行数、调用用量及交付摘要。
    @raises AssertionError 最终交付不满足独立预期。
    """
    paths = {"main": Path(output_path), "stream": Path(stream_path),
             "report": Path(report_path), "manifest": Path(manifest_path)}
    main, stream = _load_jsonl(paths["main"]), _load_jsonl(paths["stream"])
    report = _load_json(paths["report"])
    sequence_schema = _load_json(ROOT / "schemas" / "sequence-annotation.json")
    frame_schema = _load_json(ROOT / "schemas" / "frame-annotation.json")
    primary = _check_sequence_rows(main, stream)
    annotation = {key: value for key, value in main[0].items() if key != "_meta"}
    _check_sequence_annotation(annotation, primary, sequence_schema)
    for row in stream:
        _check_frame(row, frame_schema)
    _check_manifest(paths, main, stream, report)
    counts = report["generate"]["sequence"]
    expected = {"planned_sets": 1, "delivered_sets": 1, "planned_sequences": 1,
                "delivered_sequences": 1, "primary_events": 3, "noise_events": 0,
                "replay_sequences": 1, "replay_events": 3, "stream_rows": 6}
    if any(counts[key] != value for key, value in expected.items()):
        raise AssertionError("sequence report counts differ from the declared example")
    return {"main_rows": len(main), "stream_rows": len(stream),
            "llm_usage": report["llm_usage"], "delivery_digest": counts["delivery_digest"]}


def main() -> None:
    """运行普通或 sequence 默认工件的独立检查器。

    @return None。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--sequence", action="store_true")
    args = parser.parse_args()
    out = ROOT / "out"
    if args.sequence:
        result = check_sequence(out / "sequence-labels.jsonl", out / "sequence-labels.stream.jsonl",
                                out / "sequence-labels.report.json", out / "sequence-labels.manifest.json")
    else:
        result = check_ordinary(
            out / "plate-labels.jsonl",
            ROOT / "data" / "plates.jsonl",
            ROOT / "oracles" / "plates.json",
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

"""验证 v1.20 序列生成教学工程的用户可见工件。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import tomllib
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
HEX32 = frozenset("0123456789abcdef")
SEQUENCE_REPORT_ORDER = (
    "mode", "run_attempt_id", "run_id", "delivery_digest", "artifacts_committed",
    "program_digest", "planned_sets", "delivered_sets", "planned_sequences",
    "delivered_sequences", "primary_events", "primary_sessions",
    "crossed_primary_sessions", "noise_events", "replay_sequences", "replay_events",
    "replay_tail_sessions", "stream_rows", "sequence_slot_attempts",
    "noise_slot_attempts", "sequence_calls", "by_pattern", "rejected_attempts",
)
SEQUENCE_REPORT_KEYS = frozenset(SEQUENCE_REPORT_ORDER)
SEQUENCE_CALL_ORDER = (
    "scenario_seed_calls", "baseline_event_plan_calls", "variant_event_plan_calls",
    "frame_render_calls", "semantic_evaluation_calls", "noise_render_calls",
    "noise_evaluation_calls",
)
SEQUENCE_CALL_KEYS = frozenset(SEQUENCE_CALL_ORDER)
REJECTION_ORDER = (
    "scenario_schema", "event_schema", "post_validator_invalid",
    "post_validator_exception", "state_transition", "frame_schema",
    "coupling_evaluation", "pattern_evaluation", "state_evaluation",
    "semantic_evaluation", "sequence_memory_budget", "context_overflow",
    "output_truncated", "provider_retryable_exhausted", "dedup", "quality",
    "annotate", "verify", "reconcile", "noise_schema", "noise_semantic",
    "noise_similarity", "noise_memory_budget",
    "noise_context_overflow", "noise_output_truncated",
    "noise_provider_retryable_exhausted", "noise_reconcile",
)
REJECTION_KEYS = frozenset(REJECTION_ORDER)
EXPECTED_VARIANT_VIOLATIONS = {
    "positive": {},
    "missing_acknowledgement": {
        "kind": "missing_role", "target": "acknowledge",
    },
    "confirmation_before_acknowledgement": {
        "kind": "reordered", "before": "acknowledge", "after": "confirm",
    },
    "confirmation_timeout": {
        "kind": "gap_above_max", "target": "acknowledge_to_confirm",
    },
}
ROLE_ORDER = ("request", "acknowledge", "confirm")
ROLE_FRAMES = {
    "request": "task_request",
    "acknowledge": "acknowledgement",
    "confirm": "confirmation",
}
ROLE_ACTORS = {
    "request": "requester",
    "acknowledge": "system",
    "confirm": "system",
}
GAP_RULES_US = {
    "request_to_acknowledge": ("request", "acknowledge", 5_000_000, 120_000_000),
    "acknowledge_to_confirm": ("acknowledge", "confirm", 30_000_000, 1_200_000_000),
}
EXPECTED_LOGICAL_LAYOUTS = {
    "positive": (("request", 0), ("acknowledge", 5_000_000), ("confirm", 35_000_000)),
    "missing_acknowledgement": (("request", 0), ("confirm", 35_000_000)),
    "confirmation_before_acknowledgement": (
        ("request", 0), ("confirm", 5_000_000), ("acknowledge", 35_000_000),
    ),
    "confirmation_timeout": (
        ("request", 0), ("acknowledge", 5_000_000), ("confirm", 1_206_000_000),
    ),
}
DECLARED_TIMESTAMPS = (
    "2026-01-05T09:00:00.000000+08:00", "2026-01-05T09:00:05.000000+08:00",
    "2026-01-05T09:00:35.000000+08:00", "2026-01-05T10:00:35.000000+08:00",
    "2026-01-05T10:01:10.000000+08:00", "2026-01-05T11:01:10.000000+08:00",
    "2026-01-05T11:01:15.000000+08:00", "2026-01-05T11:01:45.000000+08:00",
    "2026-01-05T13:00:00.000000+08:00", "2026-01-05T13:00:05.000000+08:00",
    "2026-01-05T13:20:06.000000+08:00", "2026-01-05T14:20:06.000000+08:00",
    "2026-01-05T14:20:11.000000+08:00", "2026-01-05T14:20:41.000000+08:00",
    "2026-01-05T15:20:41.000000+08:00", "2026-01-05T15:21:16.000000+08:00",
    "2026-01-05T16:21:16.000000+08:00", "2026-01-05T16:21:21.000000+08:00",
    "2026-01-05T16:21:51.000000+08:00", "2026-01-05T17:21:51.000000+08:00",
    "2026-01-05T17:21:56.000000+08:00", "2026-01-05T17:41:57.000000+08:00",
    "2026-01-05T18:41:57.000000+08:00", "2026-01-05T18:42:02.000000+08:00",
    "2026-01-05T19:42:02.000000+08:00", "2026-01-05T19:42:07.000000+08:00",
    "2026-01-05T19:42:37.000000+08:00",
)
INSTRUCTION_TIMESTAMPS = (
    "2026-01-05T09:00:00.000000+08:00",
    "2026-01-05T09:00:15.000000+08:00",
    "2026-01-05T09:00:30.000000+08:00",
)
FRAME_ONLY_TIMESTAMPS = (
    "2026-01-06T09:00:00.000000+08:00",
    "2026-01-06T09:00:15.000000+08:00",
    "2026-01-06T09:00:30.000000+08:00",
)


def _load_json(path: Path) -> dict[str, Any]:
    """读取一个 JSON object。

    @param path JSON 文件路径。
    @return 已解析的对象。
    """
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), "JSON artifact must be an object"
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取非空 JSONL object 列表。

    @param path JSONL 文件路径。
    @return 按文件顺序排列的对象。
    """
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows and all(isinstance(row, dict) for row in rows), "JSONL artifact must contain objects"
    return rows


def _canonical_row(row: dict[str, Any]) -> bytes:
    """移除 emitter 墙钟字段并产生摘要用 canonical bytes。

    @param row 最终 main 或 stream 行。
    @return delivery digest 使用的规范字节。
    """
    value = copy.deepcopy(row)
    run = value.get("_meta", {}).get("run")
    if isinstance(run, dict):
        for key in ("started_at", "finished_at", "duration_ms"):
            run.pop(key, None)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def _canonical_json(value: object) -> str:
    """返回与生成器一致的 compact canonical JSON。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _derive_id(domain: str, components: list[object]) -> str:
    """按公开 v1.20 公式独立派生 32 位 generation ID。"""
    material = _canonical_json(["labelkit:v1.20", domain, components])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _timestamp_us(value: str) -> int:
    """把固定 offset ISO8601 文本转换为 UTC epoch 微秒。"""
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None and parsed.microsecond >= 0, "Timestamp must be aware"
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed.astimezone(timezone.utc) - epoch
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def _assert_example_timestamp(value: str) -> int:
    """校验教学工程冻结的 +08:00 微秒文本并返回 epoch 微秒。"""
    parsed = datetime.fromisoformat(value)
    assert parsed.utcoffset() == timedelta(hours=8), "Timestamp offset must be +08:00"
    assert parsed.isoformat(timespec="microseconds") == value, "Timestamp text is not canonical"
    return _timestamp_us(value)


def _delivery_digest(main: list[dict[str, Any]], stream: list[dict[str, Any]]) -> str:
    """按冻结 framing 重新计算 delivery digest。

    @param main 最终主视图行。
    @param stream 最终时间流视图行。
    @return 64 位 SHA-256 十六进制摘要。
    """
    digest = hashlib.sha256(b"labelkit:v1.20:delivery\n")
    for row in (*main, *stream):
        body = _canonical_row(row)
        digest.update(str(len(body)).encode("ascii"))
        digest.update(b":")
        digest.update(body)
    return digest.hexdigest()


def _artifact_sha(path: Path) -> str:
    """计算文件原始字节的 SHA-256。

    @param path 工件路径。
    @return 64 位十六进制摘要。
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_hex32(value: object) -> bool:
    """判断值是否为 32 位小写十六进制 ID。

    @param value 待检查值。
    @return 格式合法时为 true。
    """
    return isinstance(value, str) and len(value) == 32 and set(value) <= HEX32


def _is_hex64(value: object) -> bool:
    """判断值是否为完整小写 SHA-256。"""
    return isinstance(value, str) and len(value) == 64 and set(value) <= HEX32


def _assert_sequence_report(
    sequence: dict[str, Any],
    expected: dict[str, Any],
    minimum_calls: dict[str, int],
    by_pattern: dict[str, Any],
    max_slot_attempts: int,
) -> None:
    """校验 sequence report 的完整闭集、重试守恒与教学计划算术。"""
    assert tuple(sequence) == SEQUENCE_REPORT_ORDER, "Sequence report fields differ"
    assert _is_hex32(sequence["run_attempt_id"]) and _is_hex32(sequence["run_id"])
    assert _is_hex64(sequence["program_digest"])
    assert _is_hex64(sequence["delivery_digest"])
    assert sequence["artifacts_committed"] is True
    assert type(max_slot_attempts) is int and max_slot_attempts > 0
    for key in SEQUENCE_REPORT_ORDER[6:20]:
        assert type(sequence[key]) is int and sequence[key] >= 0, (
            f"Sequence report count is not an integer: {key}"
        )
    for key, value in expected.items():
        assert sequence[key] == value, f"Sequence report mismatch: {key}"
    calls = sequence["sequence_calls"]
    assert tuple(calls) == SEQUENCE_CALL_ORDER
    assert all(type(value) is int and value >= 0 for value in calls.values())
    for key, lower_bound in minimum_calls.items():
        assert calls[key] >= lower_bound, f"Sequence call count below lower bound: {key}"
    if minimum_calls["scenario_seed_calls"] == 0:
        assert calls["scenario_seed_calls"] == 0, "Catalog example called scenario seed"
    observed_patterns = sequence["by_pattern"]
    assert observed_patterns == by_pattern, "Pattern report differs"
    for variants in observed_patterns.values():
        for counts in variants.values():
            assert all(type(value) is int and value >= 0 for value in counts.values())
    rejected = sequence["rejected_attempts"]
    assert tuple(rejected) == REJECTION_ORDER, "Rejection report fields differ"
    assert all(type(value) is int and value >= 0 for value in rejected.values())
    sequence_rejections = sum(
        value for key, value in rejected.items() if not key.startswith("noise_")
    )
    noise_rejections = sum(
        value for key, value in rejected.items() if key.startswith("noise_")
    )
    assert sequence["sequence_slot_attempts"] == sequence["planned_sets"] + sequence_rejections
    assert sequence["noise_slot_attempts"] == sequence["noise_events"] + noise_rejections
    assert sequence["sequence_slot_attempts"] <= sequence["planned_sets"] * max_slot_attempts
    assert sequence["noise_slot_attempts"] <= sequence["noise_events"] * max_slot_attempts
    for key in SEQUENCE_CALL_ORDER[:5]:
        lower_bound = minimum_calls[key]
        assert lower_bound % sequence["planned_sets"] == 0
        per_attempt = lower_bound // sequence["planned_sets"]
        assert calls[key] <= per_attempt * sequence["sequence_slot_attempts"]
    assert calls["noise_render_calls"] == sequence["noise_slot_attempts"]
    assert sequence["noise_events"] <= calls["noise_evaluation_calls"]
    assert calls["noise_evaluation_calls"] <= calls["noise_render_calls"]


def _declared_paths() -> tuple[Path, Path, Path, Path]:
    """返回 declared 工程的四个成功工件路径。

    @return main、stream、report、manifest 路径。
    """
    stem = OUT / "sequence-labels"
    return (stem.with_suffix(".jsonl"), OUT / "sequence-labels.stream.jsonl",
            OUT / "sequence-labels.report.json", OUT / "sequence-labels.manifest.json")


def _assert_manifest(paths: tuple[Path, Path, Path, Path], rows: tuple[list, list]) -> None:
    """校验 manifest、文件摘要与唯一 delivery digest。

    @param paths main、stream、report、manifest 路径。
    @param rows main 与 stream 行。
    """
    main_path, stream_path, report_path, manifest_path = paths
    main, stream = rows
    report = _load_json(report_path)
    manifest = _load_json(manifest_path)
    sequence = report["generate"]["sequence"]
    assert tuple(manifest) == (
        "schema_version", "run_id", "delivery_digest", "artifacts_committed",
        "main", "stream", "report", "committed_at",
    ), "Manifest fields differ from the contract"
    assert tuple(manifest["main"]) == ("path", "sha256", "rows")
    assert tuple(manifest["stream"]) == ("path", "sha256", "rows")
    assert tuple(manifest["report"]) == ("path", "sha256")
    assert type(manifest["schema_version"]) is int and manifest["schema_version"] == 1
    assert manifest["run_id"] == sequence["run_id"]
    assert sequence["artifacts_committed"] is True
    assert manifest["artifacts_committed"] is True
    assert manifest["main"]["path"] == str(main_path.resolve())
    assert manifest["stream"]["path"] == str(stream_path.resolve())
    assert manifest["report"]["path"] == str(report_path.resolve())
    committed = manifest["committed_at"]
    assert isinstance(committed, str) and committed.endswith("Z")
    assert datetime.fromisoformat(committed).tzinfo is not None
    digest = _delivery_digest(main, stream)
    assert sequence["delivery_digest"] == digest, "Report delivery digest mismatch"
    assert manifest["delivery_digest"] == digest, "Manifest delivery digest mismatch"
    assert manifest["main"]["sha256"] == _artifact_sha(main_path), "Main artifact hash mismatch"
    assert manifest["stream"]["sha256"] == _artifact_sha(stream_path), "Stream artifact hash mismatch"
    assert manifest["report"]["sha256"] == _artifact_sha(report_path), "Report artifact hash mismatch"
    assert type(manifest["main"]["rows"]) is int
    assert type(manifest["stream"]["rows"]) is int
    assert manifest["main"]["rows"] == len(main), "Manifest main row count mismatch"
    assert manifest["stream"]["rows"] == len(stream), "Manifest stream row count mismatch"


def _split_stream(rows: list[dict[str, Any]]) -> tuple[list, list, list]:
    """按冻结 event truth 把 stream 分成 primary、noise 与 replay。

    @param rows 时间流行。
    @return 三类行，均保持原顺序。
    """
    primary: list[dict[str, Any]] = []
    noise: list[dict[str, Any]] = []
    replay: list[dict[str, Any]] = []
    for row in rows:
        event = row["_meta"]["event"]
        if event.get("noise") is True:
            noise.append(row)
        elif "replay_sequence_id" in event:
            replay.append(row)
        else:
            primary.append(row)
    return primary, noise, replay


def _assert_stream_timestamps(rows: list[dict[str, Any]], expected: tuple[str, ...]) -> None:
    """校验全文件严格递增顺序与教学计划的逐位时间。"""
    texts = tuple(row["_meta"]["event"]["timestamp"] for row in rows)
    times = tuple(_assert_example_timestamp(value) for value in texts)
    assert all(left < right for left, right in zip(times, times[1:])), (
        "Stream timestamps must be globally strictly increasing"
    )
    assert texts == expected, "Stream timestamps differ from the frozen teaching plan"


def _visible_bindings(rows: list[dict[str, Any]]) -> tuple[dict[str, dict], list[dict]]:
    """仅从可见 frame cardinality 绑定角色，不信任自报 role。"""
    by_frame: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_frame[row["_meta"]["event"]["frame_class"]].append(row)
    bindings: dict[str, dict] = {}
    violations: list[dict] = []
    for role in ROLE_ORDER:
        frame, events = ROLE_FRAMES[role], by_frame.pop(ROLE_FRAMES[role], [])
        if events:
            bindings[role] = events[0]
            event = events[0]["_meta"]["event"]
            assert event["role"] == role, "Visible role/frame mismatch"
            assert event["actor"] == ROLE_ACTORS[role], "Visible role/actor mismatch"
        else:
            violations.append({"kind": "missing_role", "target": role})
        violations.extend({"kind": "extra_role", "target": frame} for _row in events[1:])
    for frame, events in by_frame.items():
        violations.extend({"kind": "extra_role", "target": frame} for _row in events)
    return bindings, violations


def _visible_pattern_violations(rows: list[dict[str, Any]]) -> list[dict]:
    """独立重算教学 pattern 的 cardinality、顺序、gap 与 max span。"""
    bindings, violations = _visible_bindings(rows)
    times = {
        role: _assert_example_timestamp(row["_meta"]["event"]["timestamp"])
        for role, row in bindings.items()
    }
    for before, after in zip(ROLE_ORDER, ROLE_ORDER[1:]):
        if before in times and after in times and times[before] > times[after]:
            violations.append({"kind": "reordered", "before": before, "after": after})
    reordered = {(item.get("before"), item.get("after")) for item in violations
                 if item["kind"] == "reordered"}
    for name, (before, after, minimum, maximum) in GAP_RULES_US.items():
        if before not in times or after not in times or (before, after) in reordered:
            continue
        delta = times[after] - times[before]
        if delta < minimum:
            violations.append({"kind": "gap_below_min", "target": name})
        elif delta > maximum:
            violations.append({"kind": "gap_above_max", "target": name})
    if len(times) == len(ROLE_ORDER) and max(times.values()) - min(times.values()) > 2_400_000_000:
        violations.append({"kind": "max_span_exceeded", "target": "booking_success"})
    return violations


def _assert_owner_timeline(rows: list[dict[str, Any]]) -> None:
    """校验 owner 内 logical time 与 artifact time 的精确同差关系。"""
    logical = tuple(row["_meta"]["event"]["logical_time_us"] for row in rows)
    assert logical and all(isinstance(value, int) and not isinstance(value, bool)
                           for value in logical), "Logical time must use integer microseconds"
    artifact = tuple(_timestamp_us(row["_meta"]["event"]["timestamp"]) for row in rows)
    assert logical[0] == 0, "Teaching owner logical time must start at zero"
    assert tuple(value - artifact[0] for value in artifact) == logical, (
        "Artifact and logical deltas differ"
    )


def _assert_declared_patterns(main: list[dict[str, Any]], primary: list[dict[str, Any]]) -> None:
    """按 main declaration order 独立重算每个 owner 的可见 pattern。"""
    owner_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in primary:
        owner_rows[row["_meta"]["event"]["owner_sequence_id"]].append(row)
    for row in main:
        truth = row["_meta"]["generation"]
        rows = owner_rows[row["_meta"]["id"]]
        expected = EXPECTED_VARIANT_VIOLATIONS[truth["variant"]]
        actual = _visible_pattern_violations(rows)
        _assert_owner_timeline(rows)
        layout = tuple((item["_meta"]["event"]["role"],
                        item["_meta"]["event"]["logical_time_us"]) for item in rows)
        assert layout == EXPECTED_LOGICAL_LAYOUTS[truth["variant"]], (
            "Declared logical layout mismatch"
        )
        assert actual == ([] if not expected else [expected]), "Visible pattern truth mismatch"
        assert truth["expected_violation"] == expected and truth["actual_violations"] == actual


def _assert_declared_main(rows: list[dict[str, Any]]) -> None:
    """校验八条 declared 主序列及四变体精确真值。

    @param rows 主输出行。
    """
    assert len(rows) == 8, "Declared example must deliver eight sequences"
    variants = Counter(row["_meta"]["generation"]["variant"] for row in rows)
    assert variants == {
        "positive": 2,
        "missing_acknowledgement": 2,
        "confirmation_before_acknowledgement": 2,
        "confirmation_timeout": 2,
    }, "Variant counts mismatch"
    declaration_order = tuple(
        (index, variant)
        for index in range(2)
        for variant in EXPECTED_VARIANT_VIOLATIONS
    )
    observed_order = tuple(
        (row["_meta"]["generation"]["scenario_index"],
         row["_meta"]["generation"]["variant"])
        for row in rows
    )
    assert observed_order == declaration_order, "Declared main order mismatch"
    for row in rows:
        truth = row["_meta"]["generation"]
        assert truth["validation_mode"] == "declared", "Declared mode truth missing"
        assert truth["actor_knowledge_validation"] == "mechanical_and_semantic"
        assert truth["scenario_set"] == "booking_success_training"
        assert truth["pattern"] == "booking_success"
        assert truth["sequence_class"] == "ticket_booking"
        expected = EXPECTED_VARIANT_VIOLATIONS[truth["variant"]]
        assert truth["expected_violation"] == expected, "Expected violation shape mismatch"
        actual = [] if not expected else [expected]
        assert truth["actual_violations"] == actual, "Actual violation set mismatch"
        assert _is_hex32(truth["scenario_id"]) and _is_hex32(truth["world_branch_id"])


def _branch_ids(truth: dict[str, Any], program_digest: str) -> tuple[str, str]:
    """从报告 program digest 与 main truth 独立派生场景和分支 ID。"""
    index = truth["scenario_index"]
    if truth["validation_mode"] == "declared":
        scenario = _derive_id(
            "declared_scenario_id", [program_digest, truth["scenario_set"], index]
        )
        world = _derive_id("declared_world_branch_id", [scenario, truth["variant"]])
    else:
        scenario = _derive_id(
            "instruction_scenario_id", [program_digest, truth["instruction_slot"], index]
        )
        world = _derive_id("instruction_world_branch_id", [scenario, "instruction_only"])
    return scenario, world


def _event_key(truth: dict[str, Any], event: dict[str, Any], index: int) -> str:
    """按当前模式独立派生 primary event key。"""
    if truth["validation_mode"] == "declared":
        return _derive_id("declared_event_key", [truth["scenario_id"], event["role"]])
    return _derive_id("instruction_event_key", [
        truth["scenario_id"], truth["instruction_slot"], truth["scenario_index"], index,
    ])


def _expected_session_id(truth: dict[str, Any]) -> str:
    """从教学声明序独立恢复 primary session ID。"""
    if truth["validation_mode"] == "instruction_only":
        ordinal = truth["scenario_index"]
    else:
        variants = tuple(EXPECTED_VARIANT_VIOLATIONS)
        ordinal = truth["scenario_index"] * len(variants) + variants.index(truth["variant"])
    return f"primary_{ordinal:06d}"


def _assert_primary_meta(primary: dict[str, Any]) -> None:
    """校验 primary 行的闭集元数据与 inherited frame 类。"""
    assert tuple(primary) == ("payload", "_meta"), "Primary row fields differ"
    meta = primary["_meta"]
    expected = {"event", "generation", "classification"}
    if "annotation" in meta:
        expected.add("annotation")
    assert set(meta) == expected, "Primary metadata fields differ from the contract"
    event = meta["event"]
    assert set(event) == {
        "event_id", "event_key", "owner_sequence_id", "role", "frame_class",
        "actor", "logical_time_us", "timestamp", "duration_us", "resources",
        "time_bindings",
    }, "Primary event fields differ from the contract"
    classification = meta["classification"]
    assert classification == {
        "label": event["frame_class"],
        "labels": [event["frame_class"]],
        "source": "inherited",
    }, "Primary inherited classification differs from frame truth"
    if meta["generation"]["validation_mode"] == "declared":
        role = event["role"]
        assert event["frame_class"] == ROLE_FRAMES[role], "Declared frame differs from role"
        assert event["actor"] == ROLE_ACTORS[role], "Declared actor differs from role"
    _assert_time_payload(primary)


def _pointer_parts(path: str) -> tuple[str, ...]:
    """解析 checker 使用的非根 RFC 6901 object path。"""
    assert path.startswith("/") and path != "/", "Time binding path is invalid"
    return tuple(token.replace("~1", "/").replace("~0", "~") for token in path[1:].split("/"))


def _pointer_value(payload: dict[str, Any], path: str) -> Any:
    """读取一个只穿过 object parent 的业务时间叶子。"""
    value: Any = payload
    for token in _pointer_parts(path):
        assert isinstance(value, dict) and token in value, "Time binding path is missing"
        value = value[token]
    return value


def _time_value(source: str, start_us: int, duration_us: int, timestamp: str) -> Any:
    """独立计算 stream descriptor 声明的业务时间值。"""
    if source == "event_start_milliseconds":
        return start_us // 1000
    if source == "event_end_milliseconds":
        return (start_us + duration_us) // 1000
    if source == "event_duration_milliseconds":
        return duration_us // 1000
    zone = datetime.fromisoformat(timestamp).tzinfo
    epoch_us = start_us + (duration_us if source == "event_end_iso8601" else 0)
    instant = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=epoch_us)
    assert source in {"event_start_iso8601", "event_end_iso8601"}
    return instant.astimezone(zone).isoformat(timespec="microseconds")


def _assert_time_payload(row: dict[str, Any]) -> None:
    """从自描述 event 独立复算 payload 的全部业务时间叶子。"""
    event, payload = row["_meta"]["event"], row["payload"]
    duration, resources = event["duration_us"], event["resources"]
    assert isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0
    assert duration % 1000 == 0 and len(resources) == len(set(resources))
    assert duration > 0 or resources == [], "Point event cannot occupy a resource"
    start = _assert_example_timestamp(event["timestamp"])
    for binding in event["time_bindings"]:
        assert set(binding) == {"payload_path", "source"}, "Time descriptor fields differ"
        expected = _time_value(binding["source"], start, duration, event["timestamp"])
        assert _pointer_value(payload, binding["payload_path"]) == expected


def _without_time(payload: dict[str, Any], descriptor: list[dict[str, str]]) -> dict[str, Any]:
    """在副本中删除 descriptor 声明的业务时间叶子。"""
    value = json.loads(json.dumps(payload, ensure_ascii=False))
    for binding in descriptor:
        parts = _pointer_parts(binding["payload_path"])
        parent = value
        for token in parts[:-1]:
            parent = parent[token]
        parent.pop(parts[-1])
    return value


def _assert_member_views(stream: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    """逐位对账 main member、source 与 primary frame product。"""
    expected_sources = [{"file": "", "pair_index": None} for _row in rows]
    assert stream["member_sources"] == expected_sources, "Member sources differ from teaching input"
    members = stream["members"]
    assert len(members) == len(rows), "Main member products are incomplete"
    for index, (member, primary) in enumerate(zip(members, rows, strict=True)):
        meta, event = primary["_meta"], primary["_meta"]["event"]
        has_annotation = "annotation" in meta
        expected_fields = ({"index", "id", "label", "annotation", "status"}
                           if has_annotation else {"index", "id", "label"})
        assert set(member) == expected_fields, "Main member fields differ from the contract"
        assert (member["index"], member["id"], member["label"]) == (
            index, event["event_id"], event["frame_class"],
        ), "Main member identity differs from primary row"
        if has_annotation:
            assert member["annotation"] == meta["annotation"]
            expected_status = "annotated" if meta["annotation"] is not None else member["status"]
            assert member["status"] == expected_status
            assert member["status"] in {"annotated", "failed", "skipped"}


def _assert_primary_owner(row: dict[str, Any], rows: list[dict[str, Any]],
                          program_digest: str) -> None:
    """独立重算一个 main owner 的全部 primary 身份。"""
    meta, truth = row["_meta"], row["_meta"]["generation"]
    assert set(meta) == {
        "id", "source", "stream", "scores", "dedup", "classification",
        "annotation", "verification", "generation",
    }, "Main metadata fields differ from the contract"
    assert meta["classification"] == {
        "label": truth["sequence_class"],
        "labels": [truth["sequence_class"]],
        "source": "inherited",
    }, "Main inherited classification differs from sequence truth"
    scenario, world = _branch_ids(truth, program_digest)
    assert truth["scenario_id"] == scenario and truth["world_branch_id"] == world
    event_ids: list[str] = []
    stream_truth = {key: value for key, value in truth.items()
                    if key not in {"expected_violation", "actual_violations"}}
    for index, primary in enumerate(rows):
        _assert_primary_meta(primary)
        event = primary["_meta"]["event"]
        assert primary["_meta"]["generation"] == stream_truth
        assert event["event_key"] == _event_key(truth, event, index)
        expected = _derive_id("primary_event_id", [
            world, event["event_key"], _assert_example_timestamp(event["timestamp"]),
            event["duration_us"], event["resources"], event["time_bindings"],
            primary["payload"],
        ])
        assert event["event_id"] == expected and event["owner_sequence_id"] == meta["id"]
        event_ids.append(expected)
    expected_owner = _derive_id("sequence_id", [world, event_ids])
    assert meta["id"] == expected_owner, "Sequence ID does not match source rows"
    stream = meta["stream"]
    assert set(stream) == {
        "episode_id", "session_id", "member_count", "member_ids",
        "member_sources", "members",
    }, "Main stream fields differ from the contract"
    assert stream["episode_id"] == expected_owner
    assert stream["member_count"] == len(rows)
    assert stream["member_ids"] == event_ids
    assert stream["session_id"] == _expected_session_id(truth)
    _assert_member_views(stream, rows)


def _assert_cross_view(main: list[dict[str, Any]], primary: list[dict[str, Any]],
                       program_digest: str) -> None:
    """独立重算 main owner 与 primary 的全部双向身份。

    @param main 主序列行。
    @param primary primary stream 行。
    @param program_digest 成功报告中的冻结程序摘要。
    """
    owners: dict[str, list[str]] = defaultdict(list)
    owner_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in primary:
        event = row["_meta"]["event"]
        assert _is_hex32(event["event_id"]), "Primary event ID malformed"
        assert _is_hex32(event["owner_sequence_id"]), "Primary owner ID malformed"
        owners[event["owner_sequence_id"]].append(event["event_id"])
        owner_rows[event["owner_sequence_id"]].append(row)
    main_ids = {row["_meta"]["id"] for row in main}
    assert len(main_ids) == len(main) and main_ids == set(owners)
    for row in main:
        owner = row["_meta"]["id"]
        _assert_primary_owner(row, owner_rows[owner], program_digest)
        assert row["_meta"]["stream"]["member_ids"] == owners[owner]


def _assert_replay(primary: list[dict[str, Any]], replay: list[dict[str, Any]],
                   stream: list[dict[str, Any]]) -> None:
    """校验 replay 完整同源且身份重新派生。

    @param primary primary stream 行。
    @param replay replay stream 行。
    """
    assert len(replay) == 3, "Example replay must contain three events"
    source_by_id = {row["_meta"]["event"]["event_id"]: row for row in primary}
    replay_ids = {row["_meta"]["event"]["replay_sequence_id"] for row in replay}
    assert len(replay_ids) == 1, "Replay rows must form one explicit sequence"
    source_owner = primary[0]["_meta"]["event"]["owner_sequence_id"]
    source_rows = [row for row in primary
                   if row["_meta"]["event"]["owner_sequence_id"] == source_owner]
    assert stream[-len(replay):] == replay, "Replay must be the explicit stream tail"
    expected_start = _timestamp_us(stream[-len(replay) - 1]["_meta"]["event"]["timestamp"])
    expected_start += 3_600_000_000
    shifts: set[int] = set()
    for row in replay:
        assert tuple(row) == ("payload", "_meta"), "Replay row fields differ"
        event = row["_meta"]["event"]
        source = source_by_id[event["duplicate_of_event_id"]]
        assert tuple(source) == ("payload", "_meta"), "Replay source row fields differ"
        source_event = source["_meta"]["event"]
        assert set(row["_meta"]) == set(source["_meta"]), (
            "Replay metadata fields differ from source"
        )
        assert set(event) == {
            "event_id", "event_key", "owner_sequence_id", "role", "frame_class",
            "actor", "logical_time_us", "timestamp", "duration_us", "resources",
            "time_bindings", "replay_sequence_id", "replay_ordinal",
            "duplicate_of_sequence_id", "duplicate_of_event_id",
        }, "Replay event fields differ from the contract"
        assert source_event["owner_sequence_id"] == source_owner
        expected_sequence = _derive_id(
            "replay_sequence_id", [source_owner, event["replay_ordinal"]]
        )
        expected_event = _derive_id("replay_event_id", [
            expected_sequence, source_event["event_id"],
            _assert_example_timestamp(event["timestamp"]), event["duration_us"],
            row["payload"],
        ])
        assert event["duration_us"] == source_event["duration_us"]
        assert event["resources"] == source_event["resources"]
        assert event["time_bindings"] == source_event["time_bindings"]
        assert _without_time(row["payload"], event["time_bindings"]) == _without_time(
            source["payload"], source_event["time_bindings"]
        ), "Replay non-time payload differs from source"
        _assert_time_payload(row)
        shift = _timestamp_us(event["timestamp"]) - _timestamp_us(source_event["timestamp"])
        assert shift > 0 and shift % 1000 == 0, "Replay shift must be positive milliseconds"
        shifts.add(shift)
        replay_extra = {key: value for key, value in row["_meta"].items()
                        if key not in {"event", "generation"}}
        source_extra = {key: value for key, value in source["_meta"].items()
                        if key not in {"event", "generation"}}
        assert replay_extra == source_extra, "Replay downstream metadata differs from source"
        for key in ("event_key", "role", "frame_class", "actor", "logical_time_us"):
            assert event[key] == source_event[key], "Replay semantic field differs from source"
        assert event["owner_sequence_id"] is None
        assert event["replay_sequence_id"] == expected_sequence
        assert event["duplicate_of_sequence_id"] == source_owner
        assert event["event_id"] == expected_event and event["event_id"] != source_event["event_id"]
        generation = row["_meta"]["generation"]
        source_truth = source["_meta"]["generation"]
        expected_generation = {
            "validation_mode": "replay",
            "source_validation_mode": source_truth["validation_mode"],
            "sequence_class": source_truth["sequence_class"],
            "scenario_id": source_truth["scenario_id"],
            "source_pattern": source_truth.get("pattern"),
            "source_variant": source_truth.get("variant"),
            "duplicate_of_sequence_id": source_owner,
        }
        assert generation == expected_generation, "Replay generation truth differs from source"
        assert event["replay_ordinal"] == 0
        assert generation["duplicate_of_sequence_id"] == source_owner
        assert generation["scenario_id"] == source_truth["scenario_id"]
    duplicate_ids = tuple(row["_meta"]["event"]["duplicate_of_event_id"] for row in replay)
    assert duplicate_ids == tuple(row["_meta"]["event"]["event_id"] for row in source_rows)
    assert len(shifts) == 1, "Replay members must share one constant shift"
    actual_start = _timestamp_us(replay[0]["_meta"]["event"]["timestamp"])
    assert actual_start == expected_start, "Replay timestamps differ from the frozen tail layout"


def _assert_noise(rows: list[dict[str, Any]], program_digest: str, run_id: str) -> None:
    """按 NoiseSlot 顺序独立重算 noise event key 与 event ID。"""
    assert len(rows) == 2, "Example must contain two planned noise rows"
    for ordinal, row in enumerate(rows):
        assert tuple(row) == ("payload", "_meta"), "Noise row fields differ"
        meta = row["_meta"]
        assert set(meta) == {"event", "generation"}, "Noise metadata fields differ"
        event = meta["event"]
        assert set(event) == {
            "event_id", "event_key", "owner_sequence_id", "role", "frame_class",
            "actor", "logical_time_us", "timestamp", "duration_us", "resources",
            "time_bindings", "noise",
        }, "Noise event fields differ"
        event_key = _derive_id("noise_event_key", [program_digest, "noise", ordinal])
        event_id = _derive_id("noise_event_id", [
            run_id, event_key, _assert_example_timestamp(event["timestamp"]),
            event["duration_us"], event["resources"], event["time_bindings"], row["payload"],
        ])
        assert event["event_key"] == event_key and event["event_id"] == event_id
        assert event["owner_sequence_id"] is None and event["noise"] is True
        assert event["frame_class"] == "noise" and event["role"] is None
        assert event["actor"] is None and event["logical_time_us"] is None
        assert event["duration_us"] == 0 and event["resources"] == []
        _assert_time_payload(row)
        assert meta["generation"] is None


def check_declared() -> None:
    """验证 declared 教学工程的全部用户可见恒等式。"""
    paths = _declared_paths()
    main, stream = _load_jsonl(paths[0]), _load_jsonl(paths[1])
    _assert_stream_timestamps(stream, DECLARED_TIMESTAMPS)
    primary, noise, replay = _split_stream(stream)
    report = _load_json(paths[2])
    sequence = report["generate"]["sequence"]
    assert (len(primary), len(noise), len(replay)) == (22, 2, 3), "Stream composition mismatch"
    _assert_declared_main(main)
    _assert_cross_view(main, primary, sequence["program_digest"])
    _assert_declared_patterns(main, primary)
    _assert_noise(noise, sequence["program_digest"], sequence["run_id"])
    _assert_replay(primary, replay, stream)
    serialized = json.dumps((main, stream), ensure_ascii=False)
    assert "catalog-secret-" not in serialized and "hidden_sentinel" not in serialized
    _assert_sequence_report(
        sequence,
        {
            "mode": "declared", "planned_sets": 2, "delivered_sets": 2,
            "planned_sequences": 8, "delivered_sequences": 8, "primary_events": 22,
            "primary_sessions": 8, "crossed_primary_sessions": 0, "noise_events": 2,
            "replay_sequences": 1, "replay_events": 3, "replay_tail_sessions": 1,
            "stream_rows": 27,
        },
        {
            "scenario_seed_calls": 0, "baseline_event_plan_calls": 6,
            "variant_event_plan_calls": 8, "frame_render_calls": 14,
            "semantic_evaluation_calls": 8, "noise_render_calls": 2,
            "noise_evaluation_calls": 2,
        },
        {"booking_success": {
            variant: {"planned": 2, "delivered": 2}
            for variant in EXPECTED_VARIANT_VIOLATIONS
        }},
        8,
    )
    _assert_manifest(paths, (main, stream))


def check_instruction_only() -> None:
    """验证 instruction-only 不伪造 declared 结构真值。"""
    main_path = OUT / "instruction-only-labels.jsonl"
    stream_path = OUT / "instruction-only-labels.stream.jsonl"
    main, stream = _load_jsonl(main_path), _load_jsonl(stream_path)
    report_path = OUT / "instruction-only-labels.report.json"
    manifest_path = OUT / "instruction-only-labels.manifest.json"
    sequence = _load_json(report_path)["generate"]["sequence"]
    assert len(main) == 1 and len(stream) == 3, "Instruction-only cardinality mismatch"
    _assert_stream_timestamps(stream, INSTRUCTION_TIMESTAMPS)
    truth = main[0]["_meta"]["generation"]
    assert truth["validation_mode"] == "instruction_only"
    assert truth["actor_knowledge_validation"] == "semantic"
    for key in ("scenario_set", "pattern", "variant", "expected_violation", "actual_violations"):
        assert key not in truth, "Instruction-only emitted declared truth"
    expected_frames = ("task_request", "acknowledgement", "confirmation")
    for index, row in enumerate(stream):
        event = row["_meta"]["event"]
        assert event["role"] == f"position_{index:03d}", "Instruction-only role mismatch"
        assert event["frame_class"] == expected_frames[index], "Instruction-only frame mismatch"
        assert event["logical_time_us"] == index * 15_000_000
    _assert_owner_timeline(stream)
    _assert_cross_view(main, stream, sequence["program_digest"])
    _assert_sequence_report(
        sequence,
        {
            "mode": "instruction_only", "planned_sets": 1, "delivered_sets": 1,
            "planned_sequences": 1, "delivered_sequences": 1, "primary_events": 3,
            "primary_sessions": 1, "crossed_primary_sessions": 0, "noise_events": 0,
            "replay_sequences": 0, "replay_events": 0, "replay_tail_sessions": 0,
            "stream_rows": 3,
        },
        {
            "scenario_seed_calls": 1, "baseline_event_plan_calls": 3,
            "variant_event_plan_calls": 0, "frame_render_calls": 3,
            "semantic_evaluation_calls": 1, "noise_render_calls": 0,
            "noise_evaluation_calls": 0,
        },
        {},
        4,
    )
    _assert_manifest((main_path, stream_path, report_path, manifest_path), (main, stream))


def _frame_only_validator() -> Draft202012Validator:
    """校验 frame-only project 的开关组合并返回帧 Schema validator。

    @return 已检查 draft 2020-12 Schema 的 validator。
    """
    project = tomllib.loads((ROOT / "project-frame-only.toml").read_text(encoding="utf-8"))
    frame = project["frame"]
    assert "segment" not in project, "Frame-only project must not declare segment"
    assert project["run"]["mode"] == "generate_only"
    generate = project["generate"]
    assert generate["form"] == "sequence" and generate["mode"] == "instruction_only"
    slots = generate["instruction_only"]
    assert len(slots) == 1 and slots[0]["count"] == 1 and slots[0]["len_range"] == [3, 3]
    assert project["annotate"]["enabled"] is False
    assert frame["classify"]["enabled"] is False
    assert frame["annotate"]["enabled"] is True
    quality = project["quality"]
    assert quality["enabled"] is True and quality["mode"] == "pointwise"
    assert quality["threshold"] == 0.0
    output = project["output"]
    assert output["meta_mode"] == "inline" and output["rejects"] == "none"
    schema_path = ROOT / frame["annotate"]["schema_path"]
    schema = _load_json(schema_path)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    examples = schema.get("examples")
    assert isinstance(examples, list) and examples, "Frame annotation Schema needs an example"
    for example in examples:
        validator.validate(example)
    return validator


def check_frame_only_static() -> None:
    """不读取运行工件，校验 frame-only 配置与 Schema 教学契约。"""
    _frame_only_validator()


def _assert_frame_only_annotations(
    row: dict[str, Any], stream: list[dict[str, Any]], validator: Draft202012Validator,
) -> None:
    """校验 main member 与 primary stream 的帧标注完整且同源。

    @param row 唯一 main sequence 行。
    @param stream 三条 primary stream 行。
    @param validator 帧标注 Schema validator。
    """
    members = row["_meta"]["stream"]["members"]
    assert len(members) == len(stream) == 3, "Frame-only member cardinality mismatch"
    expected = ("task_request", "acknowledgement", "confirmation")
    assert tuple(member["label"] for member in members) == expected
    stream_by_id = {item["_meta"]["event"]["event_id"]: item for item in stream}
    for member in members:
        assert member["status"] == "annotated", "Every member must be annotated"
        validator.validate(member["annotation"])
        primary = stream_by_id[member["id"]]
        assert primary["_meta"]["annotation"] == member["annotation"]
    for primary in stream:
        validator.validate(primary["_meta"]["annotation"])


def check_frame_only() -> None:
    """验证 sequence frame-only 的零序列标注与完整帧标注。"""
    validator = _frame_only_validator()
    stem = OUT / "frame-only-labels"
    paths = (stem.with_suffix(".jsonl"), OUT / "frame-only-labels.stream.jsonl",
             OUT / "frame-only-labels.report.json", OUT / "frame-only-labels.manifest.json")
    main, stream = _load_jsonl(paths[0]), _load_jsonl(paths[1])
    report = _load_json(paths[2])
    assert len(main) == 1 and len(stream) == 3, "Frame-only cardinality mismatch"
    _assert_stream_timestamps(stream, FRAME_ONLY_TIMESTAMPS)
    row = main[0]
    assert row["_meta"]["annotation"] is None, "Sequence annotation must be absent"
    resolved = report["schema_engine"]["resolved_at"]
    assert sum(resolved.values()) == 0, "Sequence annotation Schema calls must be zero"
    scores = row["_meta"]["scores"]
    assert isinstance(scores, dict) and "__aggregate__" in scores, "Pointwise quality must run"
    _assert_frame_only_annotations(row, stream, validator)
    sequence = report["generate"]["sequence"]
    _assert_owner_timeline(stream)
    _assert_cross_view(main, stream, sequence["program_digest"])
    assert not any(key.startswith("frame_annotate.") for key in report["counts"])
    assert sequence["rejected_attempts"]["annotate"] == 0
    _assert_sequence_report(
        sequence,
        {
            "mode": "instruction_only", "planned_sets": 1, "delivered_sets": 1,
            "planned_sequences": 1, "delivered_sequences": 1, "primary_events": 3,
            "primary_sessions": 1, "crossed_primary_sessions": 0, "noise_events": 0,
            "replay_sequences": 0, "replay_events": 0, "replay_tail_sessions": 0,
            "stream_rows": 3,
        },
        {
            "scenario_seed_calls": 1, "baseline_event_plan_calls": 3,
            "variant_event_plan_calls": 0, "frame_render_calls": 3,
            "semantic_evaluation_calls": 1, "noise_render_calls": 0,
            "noise_evaluation_calls": 0,
        },
        {},
        4,
    )
    _assert_manifest(paths, (main, stream))


def check_replay() -> None:
    """验证普通 process replay 的冻结报告计数。"""
    report = _load_json(OUT / "replay-labels.report.json")
    expected = {"scanned": 27, "absorbed": 25, "dropped_noise": 2,
                "episodes": 9, "dropped_dup": 1, "emitted": 8, "failed": 0}
    for key, value in expected.items():
        assert report["counts"][key] == value, f"Replay count mismatch: {key}"
    assert len(_load_jsonl(OUT / "replay-labels.jsonl")) == 8, "Replay output row count mismatch"


def main() -> None:
    """解析检查模式并执行对应工件断言。"""
    parser = argparse.ArgumentParser(description="Validate the sequence-generation example artifacts")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--instruction-only", action="store_true")
    mode.add_argument("--frame-only", action="store_true")
    mode.add_argument("--replay", action="store_true")
    parser.add_argument("--static", action="store_true")
    args = parser.parse_args()
    if args.static and not args.frame_only:
        parser.error("--static requires --frame-only")
    if args.frame_only and args.static:
        check_frame_only_static()
    elif args.frame_only:
        check_frame_only()
    elif args.instruction_only:
        check_instruction_only()
    elif args.replay:
        check_replay()
    else:
        check_declared()
    print("sequence-generation artifacts: PASS")


if __name__ == "__main__":
    main()

"""v1.21 交织计划与 execution runtime 在真实本地四槽 llama-server 上的验收。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from labelkit.cli.parser import CliOverrides
from labelkit.common.config import load
from labelkit.operators.generation.project import canonical_delivery_row
from labelkit.operators.generation.planner import compile_scenario_plan
from labelkit.operators.generation.program import compile_generation_program
from labelkit.orchestration.application import execute_run

from tests.conftest import LOCAL_LLM_KEY_ENV


pytestmark = [pytest.mark.integration, pytest.mark.local_llm]

_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE = _ROOT / "examples" / "sequence-generation"
_METRICS_URL = "http://127.0.0.1:18081/metrics"
_RUNTIME_KEYS = {
    "queue_high_water",
    "running_high_water",
    "resource_wait_high_water",
    "commit_waiting_high_water",
    "candidate_bytes_high_water",
    "cancelled_tasks",
    "resource_wait_ms",
    "http_pool_wait_ms",
    "commit_ms",
}


def _copy_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    """复制真实工程并排除任何旧输出。"""
    project_root = tmp_path / "sequence-generation"
    shutil.copytree(_EXAMPLE, project_root, ignore=shutil.ignore_patterns("out", "__pycache__"))
    (project_root / "out").mkdir()
    return (
        project_root,
        project_root / "config-local-4b.toml",
        project_root / "project-runtime-four-slot.toml",
    )


def _load_json(path: str | Path) -> dict:
    """读取 UTF-8 JSON 对象。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_jsonl(path: str | Path) -> list[dict]:
    """读取 UTF-8 JSONL 行。"""
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def _metric(text: str, name: str) -> float:
    """从 Prometheus 文本读取一个无 label 数值。"""
    prefix = f"{name} "
    line = next((value for value in text.splitlines() if value.startswith(prefix)), None)
    if line is None:
        pytest.fail(f"llama-server metric is missing: {name}", pytrace=False)
    return float(line.removeprefix(prefix))


async def _run_with_metrics(config_path: Path, project_path: Path) -> tuple[int, float]:
    """在线程中运行同步入口，并轮询真实 server 的处理请求高水位。"""
    overrides = CliOverrides(console="plain")
    execution = asyncio.create_task(asyncio.to_thread(
        execute_run, config_path, project_path, overrides,
    ))
    high_water = 0.0
    async with httpx.AsyncClient(timeout=2.0) as client:
        while not execution.done():
            response = await client.get(_METRICS_URL)
            response.raise_for_status()
            high_water = max(high_water, _metric(response.text, "llamacpp:requests_processing"))
            await asyncio.sleep(0.05)
    return await execution, high_water


def _delivery_digest(main: list[dict], stream: list[dict]) -> str:
    """按正式 framing 独立重算交付摘要。"""
    digest = hashlib.sha256(b"labelkit:v1.20:delivery\n")
    for row in (*main, *stream):
        payload = canonical_delivery_row(row)
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b":")
        digest.update(payload)
    return digest.hexdigest()


def _assert_manifest(cfg, main: list[dict], stream: list[dict], report: dict) -> None:
    """独立核对 manifest-last 交付与三个正式工件。"""
    manifest = _load_json(cfg.paths.manifest)
    sequence = report["generate"]["sequence"]
    assert manifest["artifacts_committed"] is True
    assert manifest["run_id"] == sequence["run_id"]
    assert manifest["delivery_digest"] == _delivery_digest(main, stream)
    for name, path, rows in (
        ("main", Path(cfg.paths.output), len(main)),
        ("stream", Path(cfg.paths.stream), len(stream)),
        ("report", Path(cfg.paths.report), None),
    ):
        assert manifest[name]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        if rows is not None:
            assert manifest[name]["rows"] == rows


def _timestamp_us(value: str) -> int:
    """把带 offset 的工件时间转成整数 epoch 微秒。"""
    parsed = datetime.fromisoformat(value).astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed - epoch
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _owner_runs(owners: list[str]) -> int:
    """计算 timestamp owner word 的 maximal run 数。"""
    return sum(index == 0 or owner != owners[index - 1]
               for index, owner in enumerate(owners))


def _visible_plan_events(plan) -> dict[str, tuple[str, object]]:
    """按 event key 索引可见 primary 计划事件及其 slot。"""
    return {
        event.event_key: (slot_key, event)
        for block in plan.blocks
        for (slot_key, variant_name), events in block.items()
        if variant_name is not None
        for event in events
    }


def _assert_planned_event(program, slots, plan_index, row: dict) -> object:
    """证明最终 primary 行逐字段消费冻结计划且未改 interval 元数据。"""
    actual = row["_meta"]["event"]
    slot_key, planned = plan_index[actual["event_key"]]
    slot = slots[slot_key]
    pattern = program.patterns[slot.pattern_name]
    role = next(item for item in pattern.roles if item.name == planned.role)
    frame = program.frame_classes[role.frame_class]
    assert actual["role"] == planned.role and actual["logical_time_us"] == planned.logical_time_us
    assert _timestamp_us(actual["timestamp"]) == planned.timestamp_us
    assert actual["frame_class"] == role.frame_class
    assert actual["duration_us"] == planned.duration_us == frame.duration_us
    assert tuple(actual["resources"]) == planned.resources == frame.resources
    return planned


def _assert_interleaving(main, stream, sequence, program, plan) -> None:
    """从最终双视图机械证明两个强制交织 session。"""
    by_owner = {row["_meta"]["id"]: row for row in main}
    plan_index = _visible_plan_events(plan)
    slots = {item.slot_key: item for item in plan.delivery_slots}
    owner_sessions = {
        owner: row["_meta"]["stream"]["session_id"]
        for owner, row in by_owner.items()
    }
    sessions: dict[str, list[dict]] = defaultdict(list)
    for row in stream:
        event = row["_meta"]["event"]
        owner = event.get("owner_sequence_id")
        if owner in owner_sessions and "replay_sequence_id" not in event:
            sessions[owner_sessions[owner]].append(row)
    assert len(sessions) == 2
    for rows in sessions.values():
        owners = [row["_meta"]["event"]["owner_sequence_id"] for row in rows]
        assert len(rows) == 6 and len(set(owners)) == 2 and _owner_runs(owners) >= 3
        sources = {by_owner[owner]["_meta"]["generation"]["scenario_set"] for owner in owners}
        assert sources == {"runtime_trigger", "runtime_partner"}
        for owner in set(owners):
            events = [row["_meta"]["event"] for row in rows
                      if row["_meta"]["event"]["owner_sequence_id"] == owner]
            planned = [_assert_planned_event(program, slots, plan_index, row) for row in rows
                       if row["_meta"]["event"]["owner_sequence_id"] == owner]
            final_owner_positions = [event.position for event in planned]
            assert final_owner_positions == list(range(len(planned)))
            logical = [event["logical_time_us"] for event in events]
            artifact = [_timestamp_us(event["timestamp"]) for event in events]
            assert [value - logical[0] for value in logical] == [
                value - artifact[0] for value in artifact
            ]
    assert sequence["interleaving_opportunities"] == 2
    assert sequence["primary_sessions"] == 2
    assert sequence["interleaved_primary_sessions"] == 2
    assert sequence["by_interleaving_pattern"] == {
        "runtime_pair": {"eligible_opportunities": 2, "selected_sessions": 2},
    }


async def test_real_local_four_slot_sequence_runtime(tmp_path):
    """四个真实物理槽重叠执行，并交付两个真正 owner 交织 session。"""
    _project_root, config_path, project_path = _copy_project(tmp_path)
    cfg = load(config_path, project_path, CliOverrides(console="plain"))
    program = compile_generation_program(cfg)
    plan = compile_scenario_plan(program)

    exit_code, server_high_water = await _run_with_metrics(config_path, project_path)

    main = _load_jsonl(cfg.paths.output)
    stream = _load_jsonl(cfg.paths.stream)
    report = _load_json(cfg.paths.report)
    sequence = report["generate"]["sequence"]
    assert exit_code == 0
    assert server_high_water == 4
    assert len(main) == 4 and len(stream) == 16
    assert sequence["planned_sets"] == sequence["delivered_sets"] == 4
    assert sequence["planned_sequences"] == sequence["delivered_sequences"] == 4
    assert sequence["noise_events"] == 1 and sequence["replay_events"] == 3
    assert sequence["plan_digest"] == plan.digest and len(plan.interleaving_layouts) == 2
    _assert_interleaving(main, stream, sequence, program, plan)
    assert set(report["runtime"]) == _RUNTIME_KEYS
    assert report["runtime"]["commit_waiting_high_water"] >= 1
    assert report["runtime"]["candidate_bytes_high_water"] > 0
    assert set(report["llm_usage"]) == {"default", "judge"}
    assert all(
        entry["calls"] > 0
        and entry["prompt_tokens"] > 0
        and entry["completion_tokens"] > 0
        for entry in report["llm_usage"].values()
    )
    _assert_manifest(cfg, main, stream, report)
    secret = os.environ[LOCAL_LLM_KEY_ENV]
    assert all(secret not in path.read_text(encoding="utf-8") for path in (
        Path(cfg.paths.output), Path(cfg.paths.stream), Path(cfg.paths.report), Path(cfg.paths.manifest),
    ))

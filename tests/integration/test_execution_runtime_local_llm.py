"""v1.19 execution runtime 在真实本地四槽 llama-server 上的验收。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path

import httpx
import pytest

from labelkit.cli.parser import CliOverrides
from labelkit.common.config import load
from labelkit.operators.generation.project import canonical_delivery_row
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


async def test_real_local_four_slot_sequence_runtime(tmp_path):
    """四个真实物理槽重叠执行，并按声明序完整提交四个 set。"""
    _project_root, config_path, project_path = _copy_project(tmp_path)
    cfg = load(config_path, project_path, CliOverrides(console="plain"))

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
    assert set(report["runtime"]) == _RUNTIME_KEYS
    assert report["runtime"]["commit_waiting_high_water"] >= 1
    assert report["runtime"]["candidate_bytes_high_water"] > 0
    _assert_manifest(cfg, main, stream, report)
    secret = os.environ[LOCAL_LLM_KEY_ENV]
    assert all(secret not in path.read_text(encoding="utf-8") for path in (
        Path(cfg.paths.output), Path(cfg.paths.stream), Path(cfg.paths.report), Path(cfg.paths.manifest),
    ))

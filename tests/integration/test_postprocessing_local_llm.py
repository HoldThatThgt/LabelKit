"""标注后处理在真实本地 Qwen3.5-4B 上的最终工件验收。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from labelkit.cli.parser import CliOverrides
from labelkit.common.config import load
from labelkit.orchestration.application import execute_run

from tests.conftest import LOCAL_LLM_KEY_ENV


pytestmark = [pytest.mark.integration, pytest.mark.local_llm]

_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE = _ROOT / "examples" / "annotation-postprocessing"
_SEQUENCE_EXAMPLE = _ROOT / "examples" / "sequence-generation"
_MODEL_PATH = Path("/Users/atishoo/models/Qwen3.5-4B-GGUF/Qwen3.5-4B-Q6_K.gguf")
_SERVER_PATH = Path("/opt/homebrew/bin/llama-server")
_CODE_FIELDS = {
    "ordinary": frozenset({"start", "end", "entity_count"}),
    "sequence": frozenset({"summary_length"}),
    "frame": frozenset({"summary_length", "request_id_length", "utterance_length"}),
}
_MODEL_FIELDS = {
    frozenset({"entities"}): "ordinary",
    frozenset({"intent", "outcome", "request_id", "ticket_id", "summary"}): "sequence",
    frozenset({"request_id", "observed_status", "summary"}): "frame",
}


def _copy_projects(tmp_path: Path) -> tuple[Path, Path, Path]:
    """复制后处理与被引用的 sequence 教学工程，并排除旧工件。"""
    examples = tmp_path / "examples"
    project_root = examples / "annotation-postprocessing"
    sequence_root = examples / "sequence-generation"
    ignored = shutil.ignore_patterns("out", "__pycache__")
    shutil.copytree(_EXAMPLE, project_root, ignore=ignored)
    shutil.copytree(_SEQUENCE_EXAMPLE, sequence_root, ignore=ignored)
    (project_root / "out").mkdir()
    return project_root, project_root / "config-local-4b.toml", project_root / "project-sequence.toml"


def _load_checker(project_root: Path):
    """从复制工程装载不依赖后处理函数的最终工件检查器。"""
    path = project_root / "check_output.py"
    spec = importlib.util.spec_from_file_location("postprocessing_local_checker", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prompt_text(prompt) -> str:
    """只在内存拼接文本片段，用于字段名缺失布尔检查。"""
    return "\n".join(
        part.text or ""
        for message in prompt.messages
        for part in message.parts
        if part.kind == "text"
    )


def _prompt_excludes_fields(prompt, fields: frozenset[str]) -> bool:
    """检查提示词未以 JSON 或代码字段形式暴露工程字段名。"""
    text = _prompt_text(prompt)
    return all(
        token not in text
        for field in fields
        for token in (f'"{field}"', f"'{field}'", f"`{field}`")
    )


def _install_request_observer(monkeypatch):
    """透明装饰真实 Anthropic 序列化器，只保留无内容边界证据。"""
    from labelkit.common.inference import llm_client

    original = llm_client._build_anthropic_body
    observations: list[dict[str, object]] = []

    def wrapped(profile, prompt, response_schema):
        body = original(profile, prompt, response_schema)
        properties = response_schema.get("properties", {}) if isinstance(response_schema, Mapping) else {}
        kind = _MODEL_FIELDS.get(frozenset(properties))
        if kind is not None:
            serialized = json.dumps(response_schema, ensure_ascii=False, sort_keys=True)
            observations.append({
                "kind": kind,
                "model": body["model"],
                "schema_clean": all(field not in serialized for field in _CODE_FIELDS[kind]),
                "prompt_clean": _prompt_excludes_fields(prompt, _CODE_FIELDS[kind]),
                "structured_off": "tools" not in body and "tool_choice" not in body,
                "thinking_disabled": body.get("thinking") == {"type": "disabled"},
            })
        return body

    monkeypatch.setattr(llm_client, "_build_anthropic_body", wrapped)
    return observations


def _load_json(path: str | Path) -> dict:
    """读取一个 UTF-8 JSON object。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _artifact_paths(cfg) -> tuple[Path, ...]:
    """返回本轮可能包含内容或凭据的全部正式路径。"""
    values = (
        cfg.paths.output, cfg.paths.report, cfg.paths.rejects, cfg.paths.sidecar,
        cfg.paths.trace, cfg.paths.stream, cfg.paths.manifest, cfg.paths.failed_report,
    )
    return tuple(Path(value) for value in values if value is not None)


def _assert_secret_absent(secret: str, stderr: str, configs: tuple) -> None:
    """检查本地服务凭据未进入日志或任一存在的工件。"""
    assert secret not in stderr
    for cfg in configs:
        for path in _artifact_paths(cfg):
            if path.exists():
                assert secret not in path.read_text(encoding="utf-8")


def _run(config_path: Path, project_path: Path, output_path: Path) -> tuple[object, dict, float]:
    """经真实 execute_run 装配面运行并读取正式报告。"""
    overrides = CliOverrides(output=str(output_path), console="plain")
    cfg = load(config_path, project_path, overrides)
    started = time.perf_counter()
    exit_code = execute_run(config_path, project_path, overrides)
    wall_time_s = time.perf_counter() - started
    assert exit_code == 0
    return cfg, _load_json(cfg.paths.report), wall_time_s


def _usage_evidence(report: Mapping) -> dict[str, dict[str, int]]:
    """提取可公开报告的真实调用与 token 计数。"""
    evidence = {}
    for name, item in report["llm_usage"].items():
        assert item["calls"] > 0
        assert item["prompt_tokens"] > 0
        assert item["completion_tokens"] > 0
        evidence[name] = {
            "calls": item["calls"],
            "prompt_tokens": item["prompt_tokens"],
            "completion_tokens": item["completion_tokens"],
        }
    return evidence


def _file_sha256(path: Path) -> str:
    """流式计算大模型文件 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _server_build() -> str:
    """读取固定 llama-server 二进制的 build 标识。"""
    completed = subprocess.run(
        (_SERVER_PATH, "--version"), check=True, capture_output=True, text=True, timeout=10,
    )
    lines = (completed.stdout or completed.stderr).splitlines()
    assert lines
    return lines[0].strip()


def _assert_request_observations(observations: list[dict[str, object]]) -> dict[str, int]:
    """证明三类真实请求都消费投影 Schema 与投影提示词。"""
    counts = {kind: sum(item["kind"] == kind for item in observations) for kind in _CODE_FIELDS}
    assert counts["ordinary"] >= 4
    assert counts["sequence"] >= 1
    assert counts["frame"] >= 3
    assert all(
        item["model"] == "Qwen3.5-4B-Q6_K"
        and item["schema_clean"]
        and item["prompt_clean"]
        and item["structured_off"]
        and item["thinking_disabled"]
        for item in observations
    )
    return counts


def _run_ordinary_pair(project_root: Path, config_path: Path, checker) -> tuple[list[dict], list]:
    """连续运行两轮真实普通标注并独立验收代码字段稳定性。"""
    project_path = project_root / "project.toml"
    results = []
    configs = []
    for name in ("ordinary-first.jsonl", "ordinary-second.jsonl"):
        cfg, report, wall = _run(config_path, project_path, project_root / "out" / name)
        checked = checker.check_ordinary(
            cfg.paths.output,
            cfg.paths.input,
            project_root / "oracles" / "plates.json",
        )
        results.append({"check": checked, "usage": _usage_evidence(report), "wall_time_s": wall})
        configs.append(cfg)
    assert results[0]["check"]["cases"] == results[1]["check"]["cases"]
    return results, configs


def test_real_local_postprocessing_delivery(monkeypatch, tmp_path, capsys):
    """两轮普通标注与一轮含帧和 replay 的序列均通过独立最终检查。"""
    project_root, config_path, sequence_project = _copy_projects(tmp_path)
    checker = _load_checker(project_root)
    observations = _install_request_observer(monkeypatch)
    ordinary_runs, configs = _run_ordinary_pair(project_root, config_path, checker)

    sequence_cfg, sequence_report, sequence_wall = _run(
        config_path, sequence_project, project_root / "out" / "sequence-live.jsonl",
    )
    sequence_check = checker.check_sequence(
        sequence_cfg.paths.output,
        sequence_cfg.paths.stream,
        sequence_cfg.paths.report,
        sequence_cfg.paths.manifest,
    )
    sequence_usage = _usage_evidence(sequence_report)
    assert set(sequence_usage) == {"default", "judge"}
    configs.append(sequence_cfg)
    request_counts = _assert_request_observations(observations)

    captured = capsys.readouterr()
    _assert_secret_absent(os.environ[LOCAL_LLM_KEY_ENV], captured.err, tuple(configs))
    profile = sequence_cfg.llm_profiles["default"]
    assert _MODEL_PATH.is_file() and _SERVER_PATH.is_file()
    evidence = {
        "model_sha256": _file_sha256(_MODEL_PATH),
        "llama_server_build": _server_build(),
        "service_parameters": {
            "base_url": profile.base_url,
            "model": profile.model,
            "max_concurrency": profile.max_concurrency,
            "max_output_tokens": profile.max_output_tokens,
            "context_window": profile.context_window,
            "thinking": profile.thinking,
        },
        "ordinary_runs": ordinary_runs,
        "sequence_run": {
            "check": sequence_check,
            "usage": sequence_usage,
            "wall_time_s": sequence_wall,
        },
        "observed_annotation_requests": request_counts,
    }
    print("POSTPROCESSING_LOCAL_4B_EVIDENCE=" + json.dumps(evidence, ensure_ascii=False, sort_keys=True))

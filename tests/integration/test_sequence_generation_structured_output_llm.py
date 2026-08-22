"""v1.18 序列生成在真实 z.ai 原生 structured output 上的验收测试。"""
from __future__ import annotations

import json
import os
import shutil
from contextvars import ContextVar
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from labelkit.cli.parser import CliOverrides
from labelkit.common.config import load
from labelkit.common.inference.llm_client import (
    STRUCTURED_TOOL_DESCRIPTION,
    STRUCTURED_TOOL_NAME,
)
from labelkit.operators.generation.project import canonical_json
from labelkit.orchestration.application import execute_run

from tests.conftest import ZAI_BASE_URL, ZAI_KEY_ENV, ZAI_MODEL


pytestmark = [pytest.mark.integration, pytest.mark.zai]

_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE = _ROOT / "examples" / "sequence-generation"
_KINDS = {"scenario_seed", "event_plan", "frame", "semantic_evaluation"}


def _copy_zai_project(tmp_path: Path) -> tuple[Path, Path]:
    """复制 instruction-only 工程，并只替换两个 profile 的真实端点声明。"""
    project_root = tmp_path / "sequence-generation"
    shutil.copytree(_EXAMPLE, project_root)
    config_path = project_root / "config.toml"
    config = config_path.read_text(encoding="utf-8")
    config = config.replace("https://api.deepseek.com/anthropic", ZAI_BASE_URL)
    config = config.replace("deepseek-v4-flash", ZAI_MODEL)
    config = config.replace("LABELKIT_DEEPSEEK_KEY", ZAI_KEY_ENV)
    config = config.replace("supports_structured_output = false", "supports_structured_output = true")
    config_path.write_text(config, encoding="utf-8")
    project_path = project_root / "project-instruction-only.toml"
    project = project_path.read_text(encoding="utf-8")
    project += '\n[trace]\nenabled = true\nchannels = ["schema", "llm"]\ncontent = "refs"\n'
    project_path.write_text(project, encoding="utf-8")
    return config_path, project_path


def _schema_kind(schema: dict) -> str | None:
    """按冻结属性闭集识别四类 sequence 内部 Schema。"""
    properties = set(schema.get("properties", {}))
    if {"initial_state", "actors", "shared_facts", "style", "time_context"} <= properties:
        return "scenario_seed"
    if {"frame_class", "actor", "intent", "patch"} == properties:
        return "event_plan"
    if {"utterance", "request_id"} <= properties:
        return "frame"
    if {
        "causal_consistency",
        "actor_knowledge",
        "goal_consistency",
        "temporal_plausibility",
        "cross_frame_consistency",
        "realism",
        "reason_codes",
    } == properties:
        return "semantic_evaluation"
    return None


def _install_structured_observers(monkeypatch):
    """装饰生产 body serializer 与 structured extractor，保留真实网络路径。"""
    from labelkit.common.inference import llm_client
    from labelkit.common.inference import schema_engine

    original_body = llm_client._build_anthropic_body
    original_extract = schema_engine._extract_object
    body_checks: list[dict[str, bool]] = []
    responses: list[tuple[str, dict, object, object, bool]] = []
    key_leaked = [False]
    current_schema: ContextVar[dict | None] = ContextVar("zai_response_schema", default=None)

    def body(profile, prompt, response_schema):
        value = original_body(profile, prompt, response_schema)
        if profile.base_url.rstrip("/") != ZAI_BASE_URL:
            return value
        secret = os.environ.get(ZAI_KEY_ENV, "")
        key_leaked[0] = key_leaked[0] or bool(secret and secret in canonical_json(value))
        current_schema.set(response_schema)
        if response_schema is not None:
            tools = value.get("tools", [])
            tool = tools[0] if len(tools) == 1 else {}
            body_checks.append({
                "model": profile.model == ZAI_MODEL,
                "structured_on": profile.supports_structured_output is True,
                "one_tool": len(tools) == 1,
                "tool_name": tool.get("name") == STRUCTURED_TOOL_NAME,
                "tool_description": tool.get("description") == STRUCTURED_TOOL_DESCRIPTION,
                "schema": canonical_json(tool.get("input_schema")) == canonical_json(response_schema),
                "choice": value.get("tool_choice")
                == {"type": "tool", "name": STRUCTURED_TOOL_NAME},
                "thinking": value.get("thinking") == {"type": "disabled"},
            })
        return value

    def extract(response):
        result = original_extract(response)
        response_schema = current_schema.get()
        if response_schema is not None:
            kind = _schema_kind(response_schema)
            if kind is not None:
                structured = response.structured
                responses.append((kind, response_schema, structured, response.usage,
                                  result[0] is structured))
        return result

    monkeypatch.setattr(llm_client, "_build_anthropic_body", body)
    monkeypatch.setattr(schema_engine, "_extract_object", extract)
    return body_checks, responses, key_leaked


def _artifact_paths(cfg) -> tuple[Path, ...]:
    """返回成功与失败通道的全部可检查路径。"""
    paths = cfg.paths
    values = (
        paths.output,
        paths.stream,
        paths.report,
        paths.trace,
        paths.manifest,
        paths.failed_report,
    )
    return tuple(Path(value) for value in values if value is not None)


def _assert_api_key_absent(secret: str, stderr: str, paths, body_leaked: bool) -> None:
    """用固定失败文本检查凭据不在内存 body、stderr 或工件中。"""
    if body_leaked:
        pytest.fail("API key leaked into a production request body", pytrace=False)
    if secret and secret in stderr:
        pytest.fail("API key leaked into captured stderr", pytrace=False)
    for path in paths:
        if path.exists() and secret and secret in path.read_text(encoding="utf-8"):
            pytest.fail("API key leaked into an integration artifact", pytrace=False)


def _assert_structured_responses(responses) -> None:
    """证明四类均有至少一个被直接消费且完整有效的原生 structured response。"""
    kinds = {kind for kind, _schema, _structured, _usage, _consumed in responses}
    assert kinds == _KINDS
    native_kinds = set()
    for kind, schema, structured, usage, consumed in responses:
        if not isinstance(structured, dict):
            continue
        assert next(Draft202012Validator(schema).iter_errors(structured), None) is None
        assert consumed, "Native structured output was not consumed directly"
        assert usage.prompt_tokens > 0
        assert usage.completion_tokens > 0
        native_kinds.add(kind)
    assert native_kinds == _KINDS, "A schema kind lacked valid native structured output"


def test_sequence_combined_schemas_use_real_zai_structured_output(
    monkeypatch,
    tmp_path,
    capsys,
):
    """四类组合 Schema 经生产单工具 body 到达 z.ai，并返回原生对象。"""
    config_path, project_path = _copy_zai_project(tmp_path)
    body_checks, responses, key_leaked = _install_structured_observers(monkeypatch)
    overrides = CliOverrides(console="plain")
    cfg = load(config_path, project_path, overrides)

    exit_code = execute_run(config_path, project_path, overrides)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert body_checks, "Structured Anthropic serializer was not exercised"
    assert all(all(check.values()) for check in body_checks), "Structured request body drifted"
    _assert_structured_responses(responses)
    report = json.loads(Path(cfg.paths.report).read_text(encoding="utf-8"))
    assert report["generate"]["sequence"]["delivered_sequences"] == 1
    assert all(
        entry["calls"] > 0
        and entry["prompt_tokens"] > 0
        and entry["completion_tokens"] > 0
        for entry in report["llm_usage"].values()
    )
    _assert_api_key_absent(
        os.environ[ZAI_KEY_ENV],
        captured.err,
        _artifact_paths(cfg),
        key_leaked[0],
    )

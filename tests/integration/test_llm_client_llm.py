"""M9 integration tests against the REAL endpoint (glm-5.2 via api.z.ai, anthropic
provider). No mocks — auto-skipped by tests/conftest.py when LABELKIT_ZAI_KEY is absent.
Kept small: short prompts, modest max_tokens."""
from __future__ import annotations

import asyncio
import json
import os

import jsonschema
import pytest

from labelkit.config.model import (
    AnnotateConfig,
    Criterion,
    DedupConfig,
    GenerateConfig,
    InputConfig,
    LLMProfile,
    OutputConfig,
    QualityConfig,
    ResolvedConfig,
    Rubric,
    RunConfig,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)
from labelkit.errors import ProviderFatalError
from labelkit.llm_client import LLMClient, Message, Part, PromptBundle
from labelkit.obslog import EventLog, MetricsSink
from tests.conftest import ZAI_BASE_URL, ZAI_KEY_ENV, ZAI_MODEL

pytestmark = pytest.mark.integration


def _profile(**over) -> LLMProfile:
    defaults = dict(
        name="default",
        provider="anthropic",
        base_url=ZAI_BASE_URL,
        model=ZAI_MODEL,
        api_key_env=ZAI_KEY_ENV,
        max_concurrency=2,
        timeout_s=120,
        max_retries=2,
        retry_base_delay_s=1.0,
        supports_structured_output=True,
        max_output_tokens=512,
        temperature=0.0,
        api_key=os.environ.get(ZAI_KEY_ENV, ""),
    )
    defaults.update(over)
    return LLMProfile(**defaults)


def _prompt(text: str) -> PromptBundle:
    return PromptBundle(messages=(
        Message(role="system", parts=(Part(kind="text", text="回答简短，直接给出答案。"),)),
        Message(role="user", parts=(Part(kind="text", text=text),)),
    ))


async def test_plain_completion_returns_text_and_usage():
    client = LLMClient({"default": _profile()}, {})
    try:
        resp = await client.complete("default", _prompt("1+1 等于几？只回答数字。"))
    finally:
        await client.aclose()
    assert resp.text.strip()
    assert resp.structured is None                # no schema → no native payload
    assert resp.usage.prompt_tokens > 0
    assert resp.usage.completion_tokens > 0
    assert resp.model
    assert resp.latency_ms > 0
    acc = client.usage_by_profile["default"]
    assert acc.calls == 1
    assert acc.prompt_tokens == resp.usage.prompt_tokens
    assert acc.completion_tokens == resp.usage.completion_tokens
    assert acc.est_cost_usd is None               # no prices configured


async def test_forced_tool_structured_output_is_schema_valid():
    schema = {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "confident": {"type": "boolean"},
        },
        "required": ["answer", "confident"],
        "additionalProperties": False,
    }
    client = LLMClient({"default": _profile()}, {})
    try:
        resp = await client.complete(
            "default",
            _prompt("中国的首都是哪座城市？给出答案与你是否确定。"),
            response_schema=schema,
        )
    finally:
        await client.aclose()
    assert isinstance(resp.structured, dict)
    jsonschema.validate(resp.structured, schema)  # raises on violation
    assert resp.usage.prompt_tokens > 0


async def test_concurrency_smoke_4_calls_under_semaphore_2():
    client = LLMClient({"default": _profile(max_concurrency=2, max_output_tokens=128)}, {})
    sem = client._semaphore("llm", "default", 2)
    assert sem._value == 2
    prompts = [_prompt(f"{n}+{n} 等于几？只回答数字。") for n in (1, 2, 3, 4)]
    try:
        responses = await asyncio.gather(
            *(client.complete("default", p) for p in prompts))
    finally:
        await client.aclose()
    assert len(responses) == 4
    assert all(r.text.strip() for r in responses)
    assert client.usage_by_profile["default"].calls == 4
    assert sem._value == 2                        # semaphore fully released


def _full_trace_cfg(tmp_path, profile: LLMProfile) -> ResolvedConfig:
    return ResolvedConfig(
        tool=ToolConfig(),
        llm_profiles={"default": profile},
        embedding_profiles={},
        run=RunConfig(output=str(tmp_path / "out.jsonl"), modality="text",
                      input=str(tmp_path), fatal_error_threshold=3),
        input=InputConfig(),
        dedup=DedupConfig(),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=TraceConfig(enabled=True, path=str(tmp_path / "run.trace.jsonl"),
                          channels=("llm",), content="full"),
        rubric=Rubric(name="t", criteria=(
            Criterion(key="clarity", description="d", pairwise_prompt="p"),)),
        user_schema={"type": "object"},
        limit=None,
        strict=False,
        dry_run=False,
        config_path="config.toml",
        project_path="project.toml",
        config_digest="sha256:0",
        project_digest="sha256:0",
    )


async def test_full_content_trace_carries_input_and_output_messages(tmp_path):
    # spec 7.4 content="full" / CONTRACTS §8.2+§8.3: the llm.call event payload
    # must include BOTH gen_ai.input.messages and gen_ai.output.messages.
    profile = _profile()
    cfg = _full_trace_cfg(tmp_path, profile)
    event_log = EventLog(cfg.trace, "0123456789ab")
    sink = MetricsSink(cfg, "0123456789ab", event_log)
    client = LLMClient({"default": profile}, {}, metrics=sink)
    prompt = _prompt("1+1 等于几？只回答数字。")
    try:
        resp = await client.complete("default", prompt)
    finally:
        await client.aclose()
        event_log.flush()
        event_log.close()

    lines = [json.loads(line) for line in
             (tmp_path / "run.trace.jsonl").read_text(encoding="utf-8").splitlines()]
    calls = [ln for ln in lines if ln["ev"] == "llm.call"]
    assert len(calls) == 1
    payload = calls[0]["payload"]
    assert payload["status"] == "ok"
    # input side: the rendered prompt messages, roles preserved
    inputs = payload["gen_ai.input.messages"]
    assert [m["role"] for m in inputs] == ["system", "user"]
    assert inputs[1]["content"] == [{"type": "text", "text": "1+1 等于几？只回答数字。"}]
    # output side: present in the SERIALIZED event and matches the response
    outputs = payload["gen_ai.output.messages"]
    assert outputs == [{"role": "assistant", "content": resp.text}]
    assert resp.text.strip()
    assert payload["gen_ai.usage.input_tokens"] == resp.usage.prompt_tokens
    assert payload["gen_ai.usage.output_tokens"] == resp.usage.completion_tokens


async def test_wrong_key_is_provider_fatal_not_retried():
    # Key env deliberately points at a bogus variable → empty/invalid credential.
    bogus = _profile(name="bogus", api_key_env="LABELKIT_ZAI_KEY_DOES_NOT_EXIST",
                     api_key="", max_retries=3)
    client = LLMClient({"bogus": bogus}, {})
    try:
        with pytest.raises(ProviderFatalError) as ei:
            await client.complete("bogus", _prompt("ping"))
    finally:
        await client.aclose()
    assert ei.value.profile == "bogus"
    assert ei.value.status_code in (400, 401, 403, 404)
    # fatal error is immediate: no retries were burned
    assert client.usage_by_profile.get("bogus", None) is None or \
        client.usage_by_profile["bogus"].retries == 0


async def test_auth_fatal_trips_breaker_and_queued_calls_fail_fast(tmp_path):
    """P2-3 regression, REAL endpoint: a bad key's first 401 opens the breaker
    immediately (hard trip) and the calls queued behind it on the semaphore
    fail fast WITHOUT firing doomed HTTP requests (per-attempt breaker
    re-check after semaphore acquisition)."""
    from labelkit.errors import CircuitBreakerTripped
    from tests.test_obslog import make_cfg as obslog_cfg

    prof = _profile(api_key_env="LABELKIT_BAD_KEY_TEST", max_concurrency=1)
    os.environ["LABELKIT_BAD_KEY_TEST"] = "definitely-not-a-key"
    prof = prof.__class__(**{**prof.__dict__, "api_key": "definitely-not-a-key"})
    sink = MetricsSink(obslog_cfg(tmp_path), "itest", EventLog(obslog_cfg(tmp_path).trace, "itest"))
    client = LLMClient({"default": prof}, {}, sink)
    prompt = PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="text", text="hi", image=None),)),))

    results = await asyncio.gather(
        *[client.complete("default", prompt) for _ in range(6)],
        return_exceptions=True)
    fatals = [r for r in results if isinstance(r, ProviderFatalError)]
    tripped = [r for r in results if isinstance(r, CircuitBreakerTripped)]
    # max_concurrency=1 → strictly serial: exactly one real 401, the rest
    # never leave the queue.
    assert len(fatals) == 1 and fatals[0].status_code == 401
    assert len(tripped) == 5
    assert sink.circuit_broken

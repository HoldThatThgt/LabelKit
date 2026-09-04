"""M9 llm_client 的离线单测——只测纯逻辑（零网络、零 mock LLM）：退避序列、
可重试分类、Retry-After 解析、两个 provider 的请求体装配、响应解析、用量与成本
记账、探针形态，以及重试引擎的各个步骤函数。

重试引擎用例逐个手工构造引擎的入参对象（``_CallSpec`` / ``_KeyPool`` /
``ProfileUsage`` / ``_RetryContext`` / ``_KeyState`` / ``KeyUsage`` /
``_AttemptFailure``）后直调单个步骤函数，断言状态转移与返回值。真正需要一次 HTTP
往返的两个函数（``_dispatch_attempt`` / ``_post_with_retries``）改由真端点集成套件
承保。全程不睡真实墙钟：``_Clock`` 把模块内的 ``time.monotonic`` 与
``asyncio.sleep`` 换成虚拟时钟。"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import math
import random
import time
from collections import deque
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from labelkit.common.config.model import EmbeddingProfile, LLMProfile, TraceConfig
from labelkit.common.errors import (
    CircuitBreakerTripped,
    ContextOverflowError,
    OutputTruncatedError,
    ProviderFatalError,
    ProviderRetryableError,
)
from labelkit.common.inference import budget, llm_client
from labelkit.common.inference.credentials import RuntimeCredentials
from labelkit.common.inference.llm_client import (
    ANTHROPIC_VERSION,
    KeySnapshot,
    KeyUsage,
    LLMClient,
    LLMResponse,
    Message,
    Part,
    ProbeResult,
    ProfileSnapshot,
    ProfileUsage,
    PromptBundle,
    _accumulate_usage,
    _AttemptFailure,
    _backoff_delay,
    _build_anthropic_body,
    _CallOutcome,
    _build_embeddings_body,
    _build_headers,
    _build_openai_body,
    _CallSpec,
    _classify_http_failure,
    _is_retryable_status,
    _parse_anthropic_response,
    _parse_embeddings_response,
    _parse_openai_response,
    _parse_retry_after,
    _key_cooldown_upper,
    _KeyPool,
    _pool_members,
    _ProbeTarget,
    _render_output_messages,
    _render_trace_messages,
    _result_usage,
    _RetryContext,
    _split_embed,
)
from labelkit.common.contracts.types import ImageRef, Usage
from tests.common.config.test_config import BASE_CONFIG, Env, env, has  # noqa: F401 (fixture)
from tests.llm_client_helpers import make_llm_client as _client


# ── fixtures ────────────────────────────────────────────────────────────────

def _creds(llm: dict | None = None, emb: dict | None = None) -> RuntimeCredentials:
    """v1.17 Wave 2b 构造契约的最小凭据载体（默认空表 = 纯逻辑用例的静态面）。"""
    return RuntimeCredentials(llm=llm or {}, embedding=emb or {})


def _llm_profile(**over) -> LLMProfile:
    defaults = dict(
        name="default", provider="openai_compatible",
        base_url="https://llm-gw.example.com/v1", model="test-model",
        api_key_env="TEST_KEY", max_concurrency=2, timeout_s=30, max_retries=5,
        retry_base_delay_s=1.0, supports_structured_output=True,
        supports_vision=True, max_output_tokens=4096, temperature=0.0,
        max_image_px=2048)
    defaults.update(over)
    return LLMProfile(**defaults)


def _embedding_profile(**over) -> EmbeddingProfile:
    defaults = dict(name="embed", base_url="https://emb.example.com/v1",
                    model="embed-model", api_key_env="TEST_KEY", dims=4)
    defaults.update(over)
    return EmbeddingProfile(**defaults)


@pytest.fixture()
def png_image(tmp_path: Path) -> ImageRef:
    path = tmp_path / "image_1.png"
    Image.new("RGB", (4, 4), (255, 0, 0)).save(path, format="PNG")
    return ImageRef(path=path, format="png", size_bytes=path.stat().st_size)


SCHEMA = {"type": "object",
          "properties": {"answer": {"type": "string"}},
          "required": ["answer"], "additionalProperties": False}


# ── backoff schedule (seeded, deterministic) ───────────────────────────────

def test_backoff_schedule_matches_full_jitter_formula():
    rng = random.Random(42)
    mirror = random.Random(42)
    for i in range(1, 7):
        expected = mirror.uniform(0.0, min(60.0, 1.0 * 2 ** i))
        assert _backoff_delay(i, 1.0, rng) == expected


def test_backoff_is_within_bounds_and_capped_at_60s():
    rng = random.Random(7)
    for i in range(1, 12):
        delay = _backoff_delay(i, 1.0, rng)
        assert 0.0 <= delay <= min(60.0, 2.0 ** i)
    # huge base: the upper bound itself is capped at 60 s
    for _ in range(200):
        assert _backoff_delay(1, 100.0, rng) <= 60.0


def test_backoff_uses_retry_number_exponent():
    # spec 3.9.4 ③: wait after attempt 2 uses i=2 → random(0, base*4)
    values = [_backoff_delay(2, 1.0, random.Random(s)) for s in range(300)]
    assert max(values) > 2.0          # exceeds the i=1 bound → exponent really is 2
    assert all(v <= 4.0 for v in values)


# ── retryability classification ─────────────────────────────────────────────

@pytest.mark.parametrize("status,retryable", [
    (408, True), (409, True), (429, True),
    (500, True), (502, True), (503, True), (504, True), (599, True),
    (400, False), (401, False), (403, False), (404, False),
    (402, False), (410, False), (418, False), (422, False),
    (200, False), (301, False),
])
def test_retryable_status_classification(status, retryable):
    assert _is_retryable_status(status) is retryable


# ── Retry-After parsing ─────────────────────────────────────────────────────

def test_retry_after_delta_seconds():
    assert _parse_retry_after("5") == 5.0
    assert _parse_retry_after(" 12 ") == 12.0
    assert _parse_retry_after("0") == 0.0
    assert _parse_retry_after("2.5") == 2.5


def test_retry_after_negative_clamped_to_zero():
    assert _parse_retry_after("-3") == 0.0


def test_retry_after_http_date():
    now = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
    header = format_datetime(datetime(2026, 7, 2, 12, 0, 30, tzinfo=timezone.utc), usegmt=True)
    assert _parse_retry_after(header, now=now) == 30.0
    past = format_datetime(datetime(2026, 7, 2, 11, 0, 0, tzinfo=timezone.utc), usegmt=True)
    assert _parse_retry_after(past, now=now) == 0.0


def test_retry_after_invalid_or_absent():
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after("soon") is None


def test_retry_after_naive_http_date_is_read_as_utc():
    # RFC 5322 的 "-0000"（本地时区未知）解析为 naive datetime——按 UTC 处理
    now = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
    assert _parse_retry_after("Thu, 02 Jul 2026 12:00:30 -0000", now=now) == 30.0


# ── anthropic request-body assembly ─────────────────────────────────────────

def test_anthropic_body_text_and_image_exact(png_image: ImageRef):
    prof = _llm_profile(provider="anthropic", temperature=0.3)
    prompt = PromptBundle(messages=(
        Message(role="system", parts=(Part(kind="text", text="系统指令"),)),
        Message(role="user", parts=(
            Part(kind="text", text="[屏幕截图]"),
            Part(kind="image", image=png_image),
            Part(kind="text", text="[UI 控件树]\nFrameLayout [0,0,10,10]"),
        )),
    ))
    b64 = base64.b64encode(png_image.path.read_bytes()).decode("ascii")
    body = _build_anthropic_body(prof, prompt, response_schema=SCHEMA)
    assert prof.thinking is None
    assert "thinking" not in body
    assert body == {
        "model": "test-model",
        "max_tokens": 4096,
        "temperature": 0.3,
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "[屏幕截图]"},
                {"type": "image", "source": {"type": "base64",
                                             "media_type": "image/png",
                                             "data": b64}},
                {"type": "text", "text": "[UI 控件树]\nFrameLayout [0,0,10,10]"},
            ]},
        ],
        "system": "系统指令",
        "tools": [{
            "name": "emit",
            "description": (
                "Use this tool to return the final JSON object requested by the user. "
                "Populate every required field according to input_schema. "
                "Do not answer with prose or Markdown. Call the tool exactly once."
            ),
            "input_schema": SCHEMA,
        }],
        "tool_choice": {"type": "tool", "name": "emit"},
    }


def test_anthropic_schema_ignored_without_structured_support():
    prof = _llm_profile(provider="anthropic", supports_structured_output=False)
    prompt = PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="text", text="hi"),)),))
    body = _build_anthropic_body(prof, prompt, response_schema=SCHEMA)
    assert "tools" not in body and "tool_choice" not in body


def test_anthropic_temperature_defaults_to_profile():
    prof = _llm_profile(provider="anthropic", temperature=0.7)
    prompt = PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="text", text="hi"),)),))
    assert _build_anthropic_body(prof, prompt, None)["temperature"] == 0.7
    prompt2 = PromptBundle(messages=prompt.messages, temperature=0.9)
    assert _build_anthropic_body(prof, prompt2, None)["temperature"] == 0.9


@pytest.mark.parametrize("thinking", ["enabled", "disabled"])
def test_anthropic_thinking_is_explicit_top_level_field(thinking):
    prof = _llm_profile(provider="anthropic", thinking=thinking)
    prompt = PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="text", text="hi"),)),))
    body = _build_anthropic_body(prof, prompt, None)
    assert body["thinking"] == {"type": thinking}


def test_anthropic_headers():
    assert _build_headers("anthropic", "sk-test") == {
        "x-api-key": "sk-test",
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    assert ANTHROPIC_VERSION == "2023-06-01"


# ── openai request-body assembly ────────────────────────────────────────────

def test_openai_body_text_and_image_exact(png_image: ImageRef):
    prof = _llm_profile()
    prompt = PromptBundle(messages=(
        Message(role="system", parts=(Part(kind="text", text="你是标注员。"),)),
        Message(role="user", parts=(
            Part(kind="text", text="[屏幕截图]"),
            Part(kind="image", image=png_image),
        )),
    ))
    b64 = base64.b64encode(png_image.path.read_bytes()).decode("ascii")
    body = _build_openai_body(prof, prompt, response_schema=SCHEMA)
    assert prof.thinking is None
    assert "thinking" not in body
    assert body == {
        "model": "test-model",
        "temperature": 0.0,
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": "你是标注员。"},   # single text part → plain string
            {"role": "user", "content": [
                {"type": "text", "text": "[屏幕截图]"},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]},
        ],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "user_schema", "strict": True,
                                            "schema": SCHEMA}},
    }


def test_openai_schema_ignored_without_structured_support():
    prof = _llm_profile(supports_structured_output=False)
    prompt = PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="text", text="hi"),)),))
    body = _build_openai_body(prof, prompt, response_schema=SCHEMA)
    assert "response_format" not in body


@pytest.mark.parametrize("thinking", ["enabled", "disabled"])
def test_openai_thinking_is_explicit_top_level_field(thinking):
    prof = _llm_profile(thinking=thinking)
    prompt = PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="text", text="hi"),)),))
    body = _build_openai_body(prof, prompt, None)
    assert body["thinking"] == {"type": thinking}


def test_openai_headers_bearer():
    assert _build_headers("openai_compatible", "sk-test") == {
        "Authorization": "Bearer sk-test",
        "Content-Type": "application/json",
    }


def test_embeddings_body():
    prof = _embedding_profile()
    assert _build_embeddings_body(prof, ["a", "b"]) == {
        "model": "embed-model", "input": ["a", "b"]}


# ── response parsing ────────────────────────────────────────────────────────

def test_anthropic_parse_tool_use_extraction():
    data = {"model": "glm-x", "content": [
        {"type": "tool_use", "id": "t1", "name": "emit",
         "input": {"answer": "登录页"}}],
        "usage": {"input_tokens": 31, "output_tokens": 8},
        "stop_reason": "tool_use"}
    text, structured, usage, model, finish = _parse_anthropic_response(data, "fallback")
    assert structured == {"answer": "登录页"}
    assert text == ""
    assert usage == Usage(31, 8)
    assert model == "glm-x"
    assert finish == "tool_use"          # v1.11: raw stop_reason surfaces (V23③)


def test_anthropic_parse_text_fallback_and_thinking_skipped():
    data = {"content": [
        {"type": "thinking", "thinking": "..."},
        {"type": "text", "text": "第一段"},
        {"type": "text", "text": "第二段"}],
        "usage": {"input_tokens": 10, "output_tokens": 5}}
    text, structured, usage, model, finish = _parse_anthropic_response(data, "fallback")
    assert structured is None
    assert text == "第一段\n第二段"
    assert model == "fallback"          # no model in payload → profile model
    assert finish is None               # provider sent no stop_reason


def test_anthropic_parse_skips_junk_blocks_and_keeps_the_first_tool_use():
    data = {"content": [
        None, "junk",                                        # 非 Mapping 块直接跳过
        {"type": "tool_use", "input": "not a mapping"},      # 载荷不可用 → 继续找
        {"type": "tool_use", "input": {"answer": "第一个"}},
        {"type": "tool_use", "input": {"answer": "第二个"}}]}  # 后续 tool_use 不覆盖
    text, structured, usage, model, finish = _parse_anthropic_response(data, "fb")
    assert structured == {"answer": "第一个"}
    assert (text, usage, model, finish) == ("", Usage(0, 0), "fb", None)


def test_openai_parse_joins_typed_content_fragments():
    # 部分网关回带类型的片段列表而不是字符串
    data = {"choices": [{"message": {"content": [
        {"type": "text", "text": "第一段"}, {"type": "text"}, "junk",
        {"type": "text", "text": "第二段"}]}}]}
    text, *_rest = _parse_openai_response(data, "fb")
    assert text == "第一段第二段"


def test_openai_parse_text_and_usage():
    data = {"model": "qwen2.5", "choices": [
        {"index": 0, "finish_reason": "stop",
         "message": {"role": "assistant", "content": '{"answer":"ok"}'}}],
        "usage": {"prompt_tokens": 3184, "completion_tokens": 156, "total_tokens": 3340}}
    text, structured, usage, model, finish = _parse_openai_response(data, "fallback")
    assert text == '{"answer":"ok"}'
    assert structured is None            # openai json_schema output stays text (M8 parses)
    assert usage == Usage(3184, 156)
    assert model == "qwen2.5"
    assert finish == "stop"              # v1.11: raw finish_reason surfaces (V23③)


def test_openai_parse_missing_bits_degrade_to_defaults():
    text, structured, usage, model, finish = _parse_openai_response({}, "fb")
    assert (text, structured, usage, model, finish) == ("", None, Usage(0, 0), "fb", None)


@pytest.mark.parametrize("data", [
    {"choices": [None]},                            # null choice
    {"choices": "x"},                               # choices not a list
    {"choices": [{"message": None}]},               # null message
    {"choices": [{"message": "x"}]},                # message not a mapping
    {"choices": [{}], "usage": ["bogus"]},          # usage not a mapping
    {"choices": [{"message": {"content": 42}}]},    # content of unknown type
])
def test_openai_parse_malformed_shapes_degrade_never_raise(data):
    # A 2xx body with an unexpected shape must not escape M9 as a raw
    # AttributeError (spec 3.9.2: complete raises Provider* errors only).
    text, structured, usage, model, finish = _parse_openai_response(data, "fb")
    assert (text, structured, usage, model, finish) == ("", None, Usage(0, 0), "fb", None)


def test_embeddings_parse_non_mapping_items_are_fatal_not_attribute_error():
    # {"data": [null]} / {"data": "x"} → classified ProviderFatalError
    # (count mismatch), never an unclassified AttributeError.
    with pytest.raises(ProviderFatalError):
        _parse_embeddings_response({"data": [None]}, 1, "embed", dims=None)
    with pytest.raises(ProviderFatalError):
        _parse_embeddings_response({"data": "x"}, 1, "embed", dims=None)


def test_embeddings_parse_orders_by_index_and_counts_usage():
    data = {"data": [
        {"index": 1, "embedding": [0.0, 1.0, 0.0, 0.0]},
        {"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]}],
        "usage": {"prompt_tokens": 6, "total_tokens": 6}}
    vectors, usage = _parse_embeddings_response(data, 2, "embed", dims=4)
    assert vectors == [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
    assert usage == Usage(6, 0)


def test_embeddings_dims_mismatch_is_fatal():
    data = {"data": [{"index": 0, "embedding": [1.0, 2.0]}], "usage": {}}
    with pytest.raises(ProviderFatalError) as ei:
        _parse_embeddings_response(data, 1, "embed", dims=4)
    assert ei.value.profile == "embed"
    assert ei.value.status_code is None


def test_embeddings_count_mismatch_is_fatal():
    with pytest.raises(ProviderFatalError):
        _parse_embeddings_response({"data": []}, 2, "embed", dims=None)


def test_embeddings_undeclared_dims_skips_the_width_check():
    data = {"data": [{"index": 0, "embedding": [1.0, 2.0, 3.0]}]}
    vectors, usage = _parse_embeddings_response(data, 1, "embed", dims=None)
    assert vectors == [[1.0, 2.0, 3.0]] and usage == Usage(0, 0)


# ── full-content trace rendering (spec 7.4 / CONTRACTS §8.2) ───────────────

def test_render_trace_messages_references_images_by_path(png_image: ImageRef):
    prompt = PromptBundle(messages=(
        Message(role="system", parts=(Part(kind="text", text="sys"),)),
        Message(role="user", parts=(
            Part(kind="text", text="[屏幕截图]"),
            Part(kind="image", image=png_image),
        )),
    ))
    assert _render_trace_messages(prompt) == [
        {"role": "system", "content": [{"type": "text", "text": "sys"}]},
        {"role": "user", "content": [
            {"type": "text", "text": "[屏幕截图]"},
            {"type": "image", "path": str(png_image.path)},   # path, never base64
        ]},
    ]


def test_render_output_messages_text_and_structured():
    # openai_compatible: raw text payload
    assert _render_output_messages('{"answer":"ok"}', None) == [
        {"role": "assistant", "content": '{"answer":"ok"}'}]
    # anthropic forced tool: native structured payload wins over text
    assert _render_output_messages("", {"answer": "登录页"}) == [
        {"role": "assistant", "content": {"answer": "登录页"}}]


def test_success_llm_call_event_merges_output_messages_over_trace_extra():
    # _post_with_retries finalizes the success payload BEFORE _emit_llm_call
    # serializes it; verify the emit path carries both gen_ai.* message keys.
    events: list[tuple[str, dict]] = []

    class _Recorder:
        def event(self, ev, *, stage, batch_no, record_ids=(), payload=None):
            events.append((ev, dict(payload or {})))

    client = _client({"default": _llm_profile()}, {}, _creds(), metrics=_Recorder())
    extra = {"gen_ai.input.messages": [{"role": "user", "content": []}]}
    merged = dict(extra)
    merged.update({"gen_ai.output.messages": _render_output_messages("hi", None)})
    client._emit_llm_call(_llm_profile(),
                          _CallOutcome(latency_ms=12, usage=Usage(3, 1), retries=0,
                                       status="ok", extra=merged),
                          operation=None)
    (ev, payload), = events
    assert ev == "llm.call"
    assert payload["gen_ai.input.messages"] == extra["gen_ai.input.messages"]
    assert payload["gen_ai.output.messages"] == [{"role": "assistant", "content": "hi"}]
    assert payload["status"] == "ok"


# ── usage / cost accounting ────────────────────────────────────────────────

def test_usage_accounting_matches_spec_example():
    # spec 3.9.4 ④: 3 calls, 9552/486 tokens, prices 0.6/1.8 → est_cost 0.006606
    acc = ProfileUsage()
    for completion in (156, 162, 168):
        _accumulate_usage(acc, Usage(3184, completion), 0, 0.6, 1.8)
    acc.retries += 2
    assert acc.calls == 3
    assert acc.prompt_tokens == 9552
    assert acc.completion_tokens == 486
    assert acc.retries == 2
    assert acc.est_cost_usd == pytest.approx(0.006606)


def test_usage_accounting_no_cost_without_both_prices():
    acc = ProfileUsage()
    _accumulate_usage(acc, Usage(100, 10), 1, None, 1.8)
    assert acc.est_cost_usd is None
    _accumulate_usage(acc, Usage(100, 10), 0, 0.6, None)
    assert acc.est_cost_usd is None
    assert acc.calls == 2 and acc.retries == 1


# ── client-level pure behavior (no network) ────────────────────────────────

def test_unknown_llm_profile_raises_value_error():
    client = _client({}, {}, _creds())
    prompt = PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="text", text="hi"),)),))
    with pytest.raises(ValueError):
        asyncio.run(client.complete("nope", prompt))


def test_embed_rejects_llm_profile_names():
    client = _client({"default": _llm_profile()}, {}, _creds())
    with pytest.raises(ValueError, match=r"\[llm\.\*\] name"):
        asyncio.run(client.embed("default", ["x"]))
    with pytest.raises(ValueError):
        asyncio.run(client.embed("missing", ["x"]))


def test_resource_manager_uses_configured_bounds_and_separate_namespaces():
    client = _client(
        {"default": _llm_profile(max_concurrency=2)},
        {"default": _embedding_profile(name="default", max_concurrency=8)},
        _creds(),
    )

    assert client._resources.admission_capacity(("llm", "default")) == 2
    assert client._resources.admission_capacity(("embedding", "default")) == 8
    assert client._resources.origin_for(("llm", "default")) == (
        "https", "llm-gw.example.com", 443,
    )
    assert client._resources.origin_for(("embedding", "default")) == (
        "https", "emb.example.com", 443,
    )


def test_probe_unknown_profile_never_raises():
    client = _client({}, {}, _creds())
    result = asyncio.run(client.probe(("llm", "ghost")))
    assert result.ok is False
    assert result.kind == "llm"
    assert result.profile == "ghost"
    assert "unknown [llm.*] profile" in (result.error or "")


# ── v1.6 key-pool configuration and pure logic ─────────────────────────────

POOL_CONFIG = BASE_CONFIG.replace(
    'api_key_env = "LK_TEST_KEY_DEFAULT"',
    'api_key_envs = ["LK_TEST_KEY_A", "LK_TEST_KEY_B"]',
    1,
)


# ── M1: api_key_envs parsing / validation / normalization ──────────────────


def test_pool_parses_env_names_only(env, monkeypatch):
    """v1.17 secret-free：静态 load 只归一环境变量名，不解析任何密钥值。"""
    monkeypatch.delenv("LK_TEST_KEY_A", raising=False)
    monkeypatch.delenv("LK_TEST_KEY_B", raising=False)
    cfg = env.load(config_text=POOL_CONFIG)
    prof = cfg.llm_profiles["default"]
    assert prof.api_key_envs == ("LK_TEST_KEY_A", "LK_TEST_KEY_B")
    assert prof.api_key_env == "LK_TEST_KEY_A"
    assert not hasattr(prof, "api_keys")


def test_scalar_form_normalizes_to_one_tuple(env, monkeypatch):
    """Existing single-key configs parse to a pool of one — keyless load OK."""
    monkeypatch.delenv("LK_TEST_KEY_DEFAULT", raising=False)
    cfg = env.load()
    prof = cfg.llm_profiles["default"]
    assert prof.api_key_envs == ("LK_TEST_KEY_DEFAULT",)
    assert not hasattr(prof, "api_key")


def test_both_forms_is_config_error(env, monkeypatch):
    monkeypatch.setenv("LK_TEST_KEY_A", "sk-a")
    both = BASE_CONFIG.replace(
        'api_key_env = "LK_TEST_KEY_DEFAULT"',
        'api_key_env = "LK_TEST_KEY_DEFAULT"\n'
        'api_key_envs = ["LK_TEST_KEY_A"]',
        1,
    )
    errors = env.errors(config_text=both)
    has(errors, "[llm.default].api_key_envs")
    has(errors, "mutually exclusive")


def test_neither_form_is_config_error(env):
    neither = BASE_CONFIG.replace('api_key_env = "LK_TEST_KEY_DEFAULT"\n', "", 1)
    errors = env.errors(config_text=neither)
    has(errors, "[llm.default].api_key_env")
    has(errors, "exactly one of")


def test_empty_array_is_config_error(env):
    empty = BASE_CONFIG.replace(
        'api_key_env = "LK_TEST_KEY_DEFAULT"', "api_key_envs = []", 1)
    errors = env.errors(config_text=empty)
    has(errors, "[llm.default].api_key_envs")
    has(errors, "non-empty")


def test_duplicate_env_names_are_config_error(env, monkeypatch):
    monkeypatch.setenv("LK_TEST_KEY_A", "sk-a")
    dup = BASE_CONFIG.replace(
        'api_key_env = "LK_TEST_KEY_DEFAULT"',
        'api_key_envs = ["LK_TEST_KEY_A", "LK_TEST_KEY_A"]', 1)
    errors = env.errors(config_text=dup)
    has(errors, "[llm.default].api_key_envs[2]")
    has(errors, "duplicate")


def test_missing_env_values_are_not_static_errors(env, monkeypatch):
    """v1.17 secret-free（SPEC-SP §5.2）：缺密钥值不再是静态 CONFIG_ERROR——
    run/probe 期的 RuntimeCredentials 才物化凭据。"""
    monkeypatch.setenv("LK_TEST_KEY_B", "sk-b")
    monkeypatch.delenv("LK_TEST_KEY_A", raising=False)
    monkeypatch.delenv("LK_TEST_KEY_C", raising=False)
    three = BASE_CONFIG.replace(
        'api_key_env = "LK_TEST_KEY_DEFAULT"',
        'api_key_envs = ["LK_TEST_KEY_A", "LK_TEST_KEY_B", "LK_TEST_KEY_C"]', 1)
    cfg = env.load(config_text=three)
    assert cfg.llm_profiles["default"].api_key_envs == (
        "LK_TEST_KEY_A", "LK_TEST_KEY_B", "LK_TEST_KEY_C")


def test_unreferenced_pooled_profile_needs_no_keys(env):
    """Rule 12 scope unchanged: unreferenced profiles are never resolved."""
    pooled_judge = BASE_CONFIG.replace(
        'api_key_env = "LK_TEST_KEY_JUDGE"',
        'api_key_envs = ["LK_TEST_KEY_J1", "LK_TEST_KEY_J2"]', 1)
    cfg = env.load(config_text=pooled_judge)   # judge unreferenced → no error
    assert cfg.llm_profiles["judge"].api_key_envs == ("LK_TEST_KEY_J1", "LK_TEST_KEY_J2")


def test_embedding_pool_keeps_env_names_keyless(env, monkeypatch):
    """v1.17 secret-free：语义引用侧同样不解析密钥值。"""
    monkeypatch.delenv("LK_TEST_KEY_E1", raising=False)
    monkeypatch.delenv("LK_TEST_KEY_E2", raising=False)
    emb_pool = BASE_CONFIG.replace(
        'api_key_env = "LK_TEST_KEY_EMB"',
        'api_key_envs = ["LK_TEST_KEY_E1", "LK_TEST_KEY_E2"]', 1)
    body = '[dedup]\nsemantic = true\nsemantic_embedding = "emb"'
    cfg = env.load(config_text=emb_pool, project_text=env.project(body=body))
    assert cfg.embedding_profiles["emb"].api_key_envs == (
        "LK_TEST_KEY_E1", "LK_TEST_KEY_E2")


def test_max_park_s_default_parse_and_bounds(env):
    assert env.load().run.max_park_s == 3600
    cfg = env.load(project_text=env.project(run_extra="max_park_s = 0"))
    assert cfg.run.max_park_s == 0
    errors = env.errors(project_text=env.project(run_extra="max_park_s = -1"))
    has(errors, "[run].max_park_s")


# ── M9: _KeyPool pure logic ─────────────────────────────────────────────────


def make_pool(n: int = 3) -> _KeyPool:
    return _KeyPool([(f"ENV_{i}", f"sk-{i}") for i in range(n)])


def test_select_least_in_flight_tie_by_declaration_order():
    pool = make_pool()
    assert pool.select(now=0.0).env == "ENV_0"          # all zero → index 0
    pool.states[0].in_flight = 2
    pool.states[1].in_flight = 1
    assert pool.select(now=0.0).env == "ENV_2"          # least in-flight
    pool.states[2].in_flight = 1
    assert pool.select(now=0.0).env == "ENV_1"          # tie → lower index


def test_select_skips_cooling_and_disabled_keys():
    pool = make_pool()
    pool.states[0].cooldown_until = 10.0
    pool.states[1].disabled = True
    assert pool.select(now=5.0).env == "ENV_2"
    pool.states[2].cooldown_until = 8.0
    assert pool.select(now=5.0) is None                 # all cooling/disabled
    assert pool.select(now=10.0).env == "ENV_0"         # deadline inclusive


def test_earliest_wake_ignores_disabled_keys():
    pool = make_pool()
    pool.states[0].disabled = True
    pool.states[0].cooldown_until = 1.0                 # dead key must not count
    pool.states[1].cooldown_until = 30.0
    pool.states[2].cooldown_until = 12.0
    assert pool.earliest_wake(now=10.0) == pytest.approx(2.0)
    assert pool.earliest_wake(now=50.0) == 0.0          # never negative


def test_live_and_size():
    pool = make_pool()
    assert pool.size == 3 and len(pool.live()) == 3
    pool.states[1].disabled = True
    assert pool.size == 3 and len(pool.live()) == 2


def test_key_cooldown_upper_caps_at_300s():
    assert _key_cooldown_upper(1.0, 1) == 2.0
    assert _key_cooldown_upper(1.0, 8) == 256.0
    assert _key_cooldown_upper(1.0, 9) == 300.0         # cap (spec 3.9.3)
    assert _key_cooldown_upper(2.0, 1) == 4.0


# ── M9: pool membership resolution ──────────────────────────────────────────


def _prof(**over) -> LLMProfile:
    defaults = dict(name="p", provider="openai_compatible",
                    base_url="https://x", model="m", api_key_env="E1")
    defaults.update(over)
    return LLMProfile(**defaults)


def test_pool_members_come_from_credentials_not_the_environment(monkeypatch):
    """v1.17 Wave 2b（CONTRACTS §7.19.3）：成员 (env 名, 密钥值) 序对来自
    RuntimeCredentials——env 现读腿已删除，os.environ.get 在本模块零调用。"""
    def _boom(name, default=None):  # noqa: ANN001
        raise AssertionError(f"llm_client must not read env values (got {name!r})")

    monkeypatch.setattr("os.environ.get", _boom)
    prof = _prof(api_key_envs=("E1", "E2"))
    creds = _creds(llm={"p": ("k1", "k2")})
    assert _pool_members("llm", prof, creds) == [("E1", "k1"), ("E2", "k2")]


def test_pool_members_single_key_profile():
    assert _pool_members("llm", _prof(), _creds(llm={"p": ("k1",)})) == [("E1", "k1")]


def test_pool_members_embedding_profile():
    prof = EmbeddingProfile(name="e", base_url="https://x", model="m",
                            api_key_env="E1", api_key_envs=("E1",))
    assert _pool_members("embedding", prof, _creds(emb={"e": ("k1",)})) == [("E1", "k1")]


def test_pool_members_duplicate_values_collapse_to_first_declaration():
    """§7.19.3 值去重：同一密钥值被两个环境变量别名 ⇒ 池内一把键，首声明者
    获得身份位（KeyUsage / trace / probe 的 key_env 语义）。"""
    prof = _prof(api_key_envs=("E1", "E2"))
    creds = _creds(llm={"p": ("k1",)})       # ("k1","k1") 已被构造期去重
    assert _pool_members("llm", prof, creds) == [("E1", "k1")]


def test_pool_members_absent_credentials_fail_closed():
    """剖面缺席于凭据 ⇒ fail-closed（内部不变式破裂），绝不拿空串密钥静默组池。"""
    with pytest.raises(ValueError, match="absent from runtime credentials"):
        _pool_members("llm", _prof(), _creds())


def test_pool_members_without_any_declared_env_name():
    """完全没有环境变量名的剖面（直接构造）退化为一把无名空密钥；身份位始终是
    空串，绝不是密钥值。"""
    assert _pool_members("llm", _prof(api_key_env=""), _creds()) == [("", "")]


def test_client_construction_requires_credentials_and_resources():
    """冻结构造契约要求凭据与共享资源限制器，metrics 保持可选。"""
    parameters = tuple(inspect.signature(LLMClient).parameters.values())

    assert tuple(parameter.name for parameter in parameters) == (
        "llm_profiles", "embedding_profiles", "credentials", "resources", "metrics",
    )
    assert all(parameter.default is inspect.Parameter.empty for parameter in parameters[:4])
    assert parameters[4].default is None


def test_client_never_reads_the_environment_for_keys(monkeypatch):
    """构造与快照（未材料化池的纯读面）全程零 os.environ.get——静态面纪律。"""
    calls: list[str] = []

    def _spy(name, default=None):  # noqa: ANN001
        calls.append(name)
        return None

    monkeypatch.setattr("os.environ.get", _spy)
    client = _client({"default": _llm_profile(api_key_envs=("KEY_A", "KEY_B"))},
                       {"embed": _embedding_profile()}, _creds())
    snaps = client.snapshot()
    assert client._pools == {}                    # 快照不材料化
    assert [k.env for s in snaps for k in s.keys] == ["KEY_A", "KEY_B", "TEST_KEY"]
    assert calls == []                            # 全程零环境变量读取


# ── M9: usage merging / probe shape ─────────────────────────────────────────


def test_merge_usage_merges_keys_and_park_stats():
    client = _client({}, {}, _creds())
    src = ProfileUsage(calls=2, prompt_tokens=10, completion_tokens=5,
                       retries=1, parked_calls=1, parked_ms=1500,
                       keys={"E1": KeyUsage(calls=2, rate_limited=3),
                             "E2": KeyUsage(disabled=True)})
    client._merge_usage({"p": src})
    client._merge_usage({"p": ProfileUsage(
        keys={"E1": KeyUsage(calls=1)}, parked_calls=1, parked_ms=500)})
    acc = client.usage_by_profile["p"]
    assert acc.calls == 2 and acc.parked_calls == 2 and acc.parked_ms == 2000
    assert acc.keys["E1"].calls == 3 and acc.keys["E1"].rate_limited == 3
    assert acc.keys["E2"].disabled is True


def test_probe_result_key_env_defaults_none():
    r = ProbeResult(kind="llm", profile="p", ok=True, model="m", latency_ms=1)
    assert r.key_env is None


def test_pool_creation_preseeds_key_usage_for_pools():
    """Report gate fix (review): every member of a pooled profile appears in
    ProfileUsage.keys from pool creation — serialized traffic that only ever
    selects key 0 must not make a pool look single-key in report.llm_usage."""
    prof = _prof(api_key_envs=("E1", "E2"))
    client = _client({"p": prof}, {}, _creds(llm={"p": ("k1", "k2")}))
    client._pool("llm", prof)
    keys = client.usage_by_profile["p"].keys
    assert set(keys) == {"E1", "E2"}
    assert all(ku.calls == 0 and ku.rate_limited == 0 and not ku.disabled
               for ku in keys.values())


def test_pool_creation_does_not_seed_single_key_profiles():
    prof = _prof()
    client = _client({"p": prof}, {}, _creds(llm={"p": ("k1",)}))
    client._pool("llm", prof)
    usage = client.usage_by_profile.get("p")
    assert usage is None or not usage.keys


def test_max_park_s_reads_run_config(tmp_path):
    """run.max_park_s must reach M9 through the metrics sink's cfg — incl. the
    0 = 不驻留 setting; no metrics → the built-in 3600 default."""
    from dataclasses import replace as dc_replace

    from labelkit.common.observability.obslog import EventLog, MetricsSink
    from tests.common.observability.test_obslog import make_cfg

    cfg = make_cfg(tmp_path)
    sink = MetricsSink(cfg, "t", EventLog(cfg.trace, "t"))
    assert _client({}, {}, _creds(), sink)._max_park_s() == 3600.0
    cfg0 = dc_replace(cfg, run=dc_replace(cfg.run, max_park_s=0))
    sink0 = MetricsSink(cfg0, "t", EventLog(cfg0.trace, "t"))
    assert _client({}, {}, _creds(), sink0)._max_park_s() == 0.0
    assert _client({}, {}, _creds())._max_park_s() == 3600.0


# ── v1.10: snapshot() read-only console pull (spec 3.9.2/3.9.3 快照行) ──────


def test_snapshot_unmaterialized_pool_zero_values():
    """No traffic yet: keys derive from the DECLARED env names, everything
    else is zero/None — and snapshot() must NOT materialize self._pools."""
    client = _client({"default": _llm_profile()}, {"embed": _embedding_profile()}, _creds(), _creds())
    snaps = client.snapshot()
    assert client._pools == {}                      # read never materializes
    assert [(s.kind, s.name) for s in snaps] == [("llm", "default"),
                                                 ("embedding", "embed")]
    llm_snap, emb_snap = snaps
    assert llm_snap == ProfileSnapshot(
        name="default", kind="llm", in_flight=0,
        max_concurrency=2,                          # mirrors the profile
        calls=0, retries=0, prompt_tokens=0, completion_tokens=0,
        est_cost_usd=None, p50_latency_ms=None,
        keys=(KeySnapshot(env="TEST_KEY", state="ok"),))
    assert emb_snap.max_concurrency == 8
    assert emb_snap.keys == (KeySnapshot(env="TEST_KEY", state="ok"),)


def test_snapshot_unmaterialized_multi_key_pool_lists_declared_envs():
    prof = _llm_profile(api_key_envs=("KEY_A", "KEY_B"))
    client = _client({"default": prof}, {}, _creds())
    (snap,) = client.snapshot()
    assert snap.keys == (KeySnapshot(env="KEY_A", state="ok"),
                         KeySnapshot(env="KEY_B", state="ok"))
    assert client._pools == {}


def test_snapshot_enumerates_llm_then_embedding_in_declaration_order():
    client = _client(
        {"default": _llm_profile(), "judge": _llm_profile(name="judge")},
        {"embed": _embedding_profile()}, _creds())
    assert [(s.kind, s.name) for s in client.snapshot()] == [
        ("llm", "default"), ("llm", "judge"), ("embedding", "embed")]


def test_snapshot_key_states_cooldown_remaining_with_injected_now():
    """Pool three-state row (spec 3.9.2): disabled wins over a future cooldown;
    cooldown carries ceil remaining seconds; deadline-passed keys are ok.
    in_flight = Σ key in_flight."""
    prof = _llm_profile(api_key_envs=("KEY_A", "KEY_B", "KEY_C"))
    client = _client({"default": prof}, {},
                       _creds(llm={"default": ("ka", "kb", "kc")}))
    pool = client._pool("llm", prof)                # materialize (as traffic would)
    pool.states[0].in_flight = 2
    pool.states[1].in_flight = 1
    pool.states[1].cooldown_until = 100.0
    pool.states[2].disabled = True
    pool.states[2].cooldown_until = 999.0           # disabled wins over cooldown

    (snap,) = client.snapshot(now=87.6)
    assert snap.in_flight == 3
    assert snap.keys == (
        KeySnapshot(env="KEY_A", state="ok"),
        KeySnapshot(env="KEY_B", state="cooldown",
                    cooldown_remaining_s=13),       # ceil(100 - 87.6) = 13
        KeySnapshot(env="KEY_C", state="disabled"),
    )
    # cooldown deadline reached → back to ok, remaining 0
    (snap2,) = client.snapshot(now=100.0)
    assert snap2.keys[1] == KeySnapshot(env="KEY_B", state="ok")


def test_snapshot_usage_mirror_and_cost():
    client = _client({"default": _llm_profile()}, {}, _creds())
    client._usage["default"] = ProfileUsage(
        calls=3, prompt_tokens=9552, completion_tokens=486, retries=2,
        est_cost_usd=0.0066)
    (snap,) = client.snapshot()
    assert (snap.calls, snap.retries) == (3, 2)
    assert (snap.prompt_tokens, snap.completion_tokens) == (9552, 486)
    assert snap.est_cost_usd == 0.0066


def test_snapshot_p50_median_window_and_none_when_empty():
    client = _client({"default": _llm_profile()}, {}, _creds())
    (snap,) = client.snapshot()
    assert snap.p50_latency_ms is None              # no samples yet
    client._latencies[("llm", "default")] = deque([100, 200, 300], maxlen=256)
    (snap,) = client.snapshot()
    assert snap.p50_latency_ms == 200
    # even count: median 150.5 → int() per spec signature (int | None)
    client._latencies[("llm", "default")] = deque([100, 201], maxlen=256)
    (snap,) = client.snapshot()
    assert snap.p50_latency_ms == 150


def test_snapshot_p50_window_is_bounded_at_256():
    client = _client({"default": _llm_profile()}, {}, _creds())
    window = client._latencies.setdefault(("llm", "default"), deque(maxlen=256))
    for v in range(300):                            # 0..299 → window keeps 44..299
        window.append(v)
    (snap,) = client.snapshot()
    assert len(window) == 256
    assert snap.p50_latency_ms == int((171 + 172) / 2)


def test_snapshot_kind_disambiguates_same_name_profiles():
    """spec 3.9.2: _usage buckets by NAME (existing quirk) — kind disambiguates
    the snapshot identity, and the p50 window is keyed by (kind, name)."""
    client = _client({"shared": _llm_profile(name="shared")},
                       {"shared": _embedding_profile(name="shared")}, _creds())
    client._usage["shared"] = ProfileUsage(calls=3)
    client._latencies[("llm", "shared")] = deque([100], maxlen=256)
    client._latencies[("embedding", "shared")] = deque([300], maxlen=256)
    llm_snap, emb_snap = client.snapshot()
    assert (llm_snap.kind, emb_snap.kind) == ("llm", "embedding")
    assert llm_snap.name == emb_snap.name == "shared"
    assert llm_snap.calls == emb_snap.calls == 3    # by-name bucket, both mirror
    assert llm_snap.p50_latency_ms == 100
    assert emb_snap.p50_latency_ms == 300


def test_snapshot_never_mutates_client_state():
    prof = _llm_profile(api_key_envs=("KEY_A", "KEY_B"))
    emb = _embedding_profile()
    client = _client({"default": prof}, {"embed": emb},
                       _creds(llm={"default": ("ka", "kb")}))
    client._pool("llm", prof)                       # one materialized pool
    client._latencies[("llm", "default")] = deque([50, 60], maxlen=256)
    client._usage["default"].calls = 7

    pools_before = dict(client._pools)
    usage_before = {k: (v.calls, v.retries) for k, v in client._usage.items()}
    lat_before = {k: list(v) for k, v in client._latencies.items()}

    first = client.snapshot(now=10.0)
    second = client.snapshot(now=10.0)
    assert first == second                          # pure read is idempotent
    assert client._pools == pools_before            # embed pool NOT materialized
    assert set(client._pools) == {("llm", "default")}
    assert {k: (v.calls, v.retries) for k, v in client._usage.items()} == usage_before
    assert {k: list(v) for k, v in client._latencies.items()} == lat_before


def test_snapshot_joins_per_key_usage_mirror():
    """KeySnapshot carries the per-key KeyUsage mirror (calls / rate_limited)
    — the panel's 'l' expanded view data source (spec 3.9.2 / §7.7)."""
    prof = _llm_profile(api_key_envs=("KEY_A", "KEY_B"))
    client = _client({"default": prof}, {},
                       _creds(llm={"default": ("ka", "kb")}))
    client._pool("llm", prof)
    client._usage["default"].keys["KEY_A"].calls = 41
    client._usage["default"].keys["KEY_B"].calls = 12
    client._usage["default"].keys["KEY_B"].rate_limited = 3
    (snap,) = client.snapshot(now=0.0)
    assert (snap.keys[0].calls, snap.keys[0].rate_limited) == (41, 0)
    assert (snap.keys[1].calls, snap.keys[1].rate_limited) == (12, 3)


def test_snapshot_nonblocking_inside_running_loop():
    """spec §7.8 协议 row: snapshot() is a plain sync read — callable from a
    coroutine amid a concurrent gather without awaiting, locking, or blocking
    the event loop (U26: the render tick calls it between awaits)."""
    prof = _llm_profile(api_key_envs=("KEY_A", "KEY_B"))
    client = _client({"default": prof}, {},
                       _creds(llm={"default": ("ka", "kb")}))
    client._pool("llm", prof)

    async def sampler() -> list:
        out = []
        for _ in range(50):
            out.append(client.snapshot())
            await asyncio.sleep(0)                  # yield to the sibling task
        return out

    async def main() -> list:
        a, b = await asyncio.gather(sampler(), sampler())
        return a + b

    for snaps in asyncio.run(main()):
        (snap,) = snaps
        assert snap.name == "default" and len(snap.keys) == 2


# ── v1.11: context budget — precheck / finish disposition / V20 sniff ───────


class _BreakerRecorder:
    """Minimal MetricsSink stand-in: records breaker feeds and events."""
    circuit_broken = False

    def __init__(self):
        self.results: list = []
        self.events: list = []

    def record_provider_result(self, fatal, hard=False):
        self.results.append((fatal, hard))

    def event(self, ev, **kw):
        self.events.append(ev)


def test_precheck_raises_context_overflow_before_any_network():
    """V16: budget-declared profile + over-window prompt → the precheck fires
    at the complete() throat — zero provider interaction (no transport ever
    created), nothing fed to the breaker, no llm.call event, no retry burned."""
    rec = _BreakerRecorder()
    prof = _llm_profile(context_window=512, max_output_tokens=256)
    client = _client({"default": prof}, {}, _creds(), metrics=rec)
    prompt = PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="text", text="hello"),)),))
    with pytest.raises(ContextOverflowError) as ei:
        asyncio.run(client.complete("default", prompt))
    assert ei.value.phase == "precheck"
    assert ei.value.profile == "default"
    assert client._http_client is None          # dispatch never reached
    assert rec.results == []                    # breaker never fed
    assert rec.events == []                     # no llm.call emitted


def test_precheck_counts_images_via_the_calibrator_prior(png_image: ImageRef):
    # 131072 window fits the text alone easily; 60 images × the openai prior
    # readout (ceil(1445 × 1.2) = 1734 each @2048) blow the 113868 budget.
    rec = _BreakerRecorder()
    prof = _llm_profile(context_window=131072)
    client = _client({"default": prof}, {}, _creds(), metrics=rec)
    parts = tuple(Part(kind="image", image=png_image) for _ in range(80))
    prompt = PromptBundle(messages=(Message(role="user", parts=parts),))
    with pytest.raises(ContextOverflowError) as ei:
        asyncio.run(client.complete("default", prompt))
    assert ei.value.phase == "precheck"
    assert client._http_client is None


def test_finish_surfaces_from_both_providers_pure_parse():
    # openai: choices[0].finish_reason
    data = {"choices": [{"finish_reason": "length",
                         "message": {"content": '{"a": 1'}}]}
    *_, finish = _parse_openai_response(data, "fb")
    assert finish == "length"
    data = {"choices": [{"finish_reason": "model_context_window_exceeded",
                         "message": {"content": ""}}]}
    *_, finish = _parse_openai_response(data, "fb")
    assert finish == "model_context_window_exceeded"
    # anthropic: top-level stop_reason
    data = {"content": [{"type": "text", "text": "部分输出"}],
            "stop_reason": "max_tokens"}
    *_, finish = _parse_anthropic_response(data, "fb")
    assert finish == "max_tokens"
    data = {"content": [], "stop_reason": "model_context_window_exceeded"}
    *_, finish = _parse_anthropic_response(data, "fb")
    assert finish == "model_context_window_exceeded"


def test_raise_for_finish_disposition_is_a_closed_map():
    """V11/V24: length (openai) / max_tokens (anthropic) → OutputTruncatedError;
    model_context_window_exceeded (both protocols) → reactive overflow; every
    other value — stop, tool_use, end_turn, z.ai sensitive/network_error,
    None — flows on unchanged (V11③)."""
    from labelkit.common.inference.llm_client import _raise_for_finish

    with pytest.raises(OutputTruncatedError) as ti:
        _raise_for_finish("length", "default", 4096)
    assert ti.value.profile == "default" and ti.value.finish == "length"
    with pytest.raises(OutputTruncatedError):
        _raise_for_finish("max_tokens", "default", 4096)
    with pytest.raises(ContextOverflowError) as ci:
        _raise_for_finish("model_context_window_exceeded", "default", 4096)
    assert ci.value.phase == "reactive"
    for benign in ("stop", "tool_use", "end_turn", "sensitive",
                   "network_error", None):
        _raise_for_finish(benign, "default", 4096)   # no raise


# [C-75] five empirical overflow-body families (V20 seeds, frozen set).
_OVERFLOW_BODIES = [
    # OpenAI/Azure: code + message family
    '{"error": {"message": "This model\'s maximum context length is 128000 '
    'tokens. However, your messages resulted in 130531 tokens.", '
    '"type": "invalid_request_error", "param": "messages", '
    '"code": "context_length_exceeded"}}',
    # vLLM: same message family, type=BadRequestError, NO code
    '{"object": "error", "message": "This model\'s maximum context length is '
    '4096 tokens. However, you requested 5021 tokens.", '
    '"type": "BadRequestError", "code": 400}',
    # anthropic protocol: invalid_request_error ∧ "prompt is too long"
    '{"type": "error", "error": {"type": "invalid_request_error", '
    '"message": "prompt is too long: 213481 tokens > 200000 maximum"}}',
    # z.ai business code 1261 / "Prompt too long"
    '{"error": {"code": "1261", "message": "The input exceeds the maximum '
    'context length supported by the model."}}',
    '{"error": {"code":"1261", "message": "Prompt too long."}}',
    # OpenRouter: error_type == context_length_exceeded
    '{"error": {"message": "This endpoint\'s maximum context length is '
    '131072 tokens.", "type": "context_length_exceeded"}}',
]


@pytest.mark.parametrize("body", _OVERFLOW_BODIES)
def test_overflow_body_matcher_hits_all_five_families(body):
    from labelkit.common.inference.llm_client import overflow_body_matches
    assert overflow_body_matches(body) is True


def test_overflow_body_matcher_is_case_insensitive_and_selective():
    from labelkit.common.inference.llm_client import overflow_body_matches
    assert overflow_body_matches('{"message": "MAXIMUM CONTEXT LENGTH"}') is True
    assert overflow_body_matches('{"error": "invalid api key"}') is False
    assert overflow_body_matches("") is False


def test_sniff_gate_requires_budget_and_status_400():
    """V20 budget gating: context_window == 0 → the sniff never engages (the
    400 walks the v1.10 fatal path byte-identically); non-400 never sniffs."""
    from labelkit.common.inference.llm_client import _sniff_overflow_400
    body = _OVERFLOW_BODIES[0]
    assert _sniff_overflow_400(131072, 400, body) is True
    assert _sniff_overflow_400(0, 400, body) is False        # budget off
    assert _sniff_overflow_400(131072, 404, body) is False   # wrong status
    assert _sniff_overflow_400(131072, 400, '{"error": "nope"}') is False


def test_llm_response_finish_field_default_and_carry():
    resp = LLMResponse(text="x", structured=None, usage=Usage(1, 1),
                       model="m", latency_ms=3)
    assert resp.finish is None                   # additive default (V23③)
    resp = LLMResponse(text="x", structured=None, usage=Usage(1, 1),
                       model="m", latency_ms=3, finish="stop")
    assert resp.finish == "stop"


def test_prompt_bundle_image_px_field_default_and_carry():
    prompt = PromptBundle(messages=())
    assert prompt.image_px is None               # additive default (V23①)
    assert PromptBundle(messages=(), image_px=1092).image_px == 1092


def test_result_usage_adapts_to_the_five_tuple_shape():
    from labelkit.common.inference.llm_client import _result_usage
    complete_result = ("text", None, Usage(31, 8), "m", "stop")   # F9: 5-tuple
    assert _result_usage(complete_result) == Usage(31, 8)
    embed_result = (([[1.0, 0.0]], Usage(6, 0)),)                 # 1-tuple
    assert _result_usage(embed_result) == Usage(6, 0)


def test_effective_image_px_chain_and_clamp():
    from labelkit.common.inference.llm_client import _effective_image_px
    prof = _llm_profile()                                          # max 2048
    prompt = PromptBundle(messages=())
    assert _effective_image_px(prof, prompt) == 2048               # v1.10 leg
    prof = _llm_profile(default_image_px=1024)
    assert _effective_image_px(prof, prompt) == 1024               # working point
    escalated = PromptBundle(messages=(), image_px=1536)
    assert _effective_image_px(prof, escalated) == 1536            # V21 carrier
    over = PromptBundle(messages=(), image_px=4096)
    assert _effective_image_px(prof, over) == 2048                 # ceiling clamp


def test_client_self_constructs_calibrator_with_working_points():
    """V23②: LLMClient holds its own calibrator seeded from the profile table
    — anthropic prior 1568 @2048 and openai prior 765 @1024 working point,
    both × PRIOR_INFLATION until samples accumulate."""
    client = _client({
        "a": _llm_profile(name="a", provider="anthropic"),
        "o": _llm_profile(name="o", default_image_px=1024),
    }, {}, _creds())
    assert client.calibrator.cost("a") == 1882   # ceil(1568 × 1.2)
    assert client.calibrator.cost("o") == 918    # ceil(765 × 1.2)


# ── probe family: offline shapes (spec 3.9.2 probe / probe_all) ────────────


class _ProbeChild:
    """探针子客户端的桩：只实现 complete / embed 两个协程。

    桩住的是**进程内协作者**（LLMClient 的一次性子客户端），不是 HTTP 传输层
    ——传输层的桩化被仓库纪律禁止，这里零网络。
    """

    def __init__(self, *, model: str = "probe-model", latency_ms: int = 7,
                 raises: Exception | None = None):
        self.model = model
        self.latency_ms = latency_ms
        self.raises = raises
        self.calls: list[tuple] = []
        self._usage: dict[str, ProfileUsage] = {}

    def _account(self, profile: str) -> None:
        """记一次子客户端用量（父客户端应把它并回），随后按需抛出。"""
        self._usage.setdefault(profile, ProfileUsage()).calls += 1
        if self.raises is not None:
            raise self.raises

    async def complete(self, profile, prompt, response_schema=None):
        self.calls.append(("complete", profile, prompt))
        self._account(profile)
        return LLMResponse(text="pong", structured=None, usage=Usage(1, 1),
                           model=self.model, latency_ms=self.latency_ms)

    async def embed(self, profile, texts):
        self.calls.append(("embed", profile, list(texts)))
        self._account(profile)
        return [[0.0, 0.0, 0.0, 0.0]]


def _target(prof, *, profile: str = "default", is_llm: bool = True,
            env: str = "TEST_KEY", key_env: str | None = None) -> _ProbeTarget:
    """构造一个探测目标（剖面 + 具体某把密钥）。"""
    return _ProbeTarget(profile=profile, prof=prof, is_llm=is_llm, env=env,
                        key="sk-test", key_env=key_env)


def test_probe_all_unknown_profile_never_raises():
    results = asyncio.run(_client({}, {}, _creds()).probe_all(("llm", "ghost")))
    assert len(results) == 1
    assert results[0].ok is False and results[0].key_env is None
    assert "unknown [llm.*] profile" in (results[0].error or "")


def test_probe_all_single_key_degrades_to_one_result(monkeypatch):
    child = _ProbeChild(model="glm-x")
    monkeypatch.setattr(LLMClient, "_probe_client", lambda self, target: child)
    client = _client({"default": _llm_profile()}, {},
                       _creds(llm={"default": ("ka",)}))
    results = asyncio.run(client.probe_all(("llm", "default")))
    assert len(results) == 1
    assert results[0].ok is True and results[0].model == "glm-x"
    assert results[0].key_env is None          # 单键剖面不回填 key_env


def test_probe_all_pool_yields_one_result_per_key_in_declaration_order(monkeypatch):
    seen: list[tuple[str, str]] = []
    child = _ProbeChild()

    def _child(self, target):
        # 密钥**值**只走到子客户端，绝不进结果；这里连同环境变量名一起记下来做断言。
        seen.append((target.env, target.key))
        return child

    for env, key in (("KEY_A", "ka"), ("KEY_B", "kb"), ("KEY_C", "kc")):
        monkeypatch.setenv(env, key)   # v1.17: 值经 env 物化
    monkeypatch.setattr(LLMClient, "_probe_client", _child)
    prof = _llm_profile(api_key_envs=("KEY_A", "KEY_B", "KEY_C"))
    client = _client({"default": prof}, {},
                       _creds(llm={"default": ("ka", "kb", "kc")}))
    results = asyncio.run(client.probe_all(("llm", "default")))
    assert [r.key_env for r in results] == ["KEY_A", "KEY_B", "KEY_C"]
    assert seen == [("KEY_A", "ka"), ("KEY_B", "kb"), ("KEY_C", "kc")]
    assert all(r.ok for r in results)
    # probe() 是探首密钥的单条形态——一条结果，key_env 留空
    single = asyncio.run(client.probe(("llm", "default")))
    assert single.key_env is None and seen[-1] == ("KEY_A", "ka")


def test_probe_all_pool_keys_overlap_but_results_keep_declaration_order(monkeypatch):
    """三把密钥必须同时到达屏障，返回顺序仍按环境变量声明。"""
    for env_name, key in (("KEY_A", "ka"), ("KEY_B", "kb"), ("KEY_C", "kc")):
        monkeypatch.setenv(env_name, key)
    prof = _llm_profile(api_key_envs=("KEY_A", "KEY_B", "KEY_C"))
    client = _client(
        {"default": prof}, {}, _creds(llm={"default": ("ka", "kb", "kc")})
    )
    all_started = asyncio.Event()
    started: list[str] = []

    async def probe_one(target):
        started.append(target.env)
        if len(started) == 3:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=1)
        return ProbeResult(
            kind="llm", profile=target.profile, ok=True, model=target.prof.model,
            latency_ms=0, key_env=target.key_env,
        )

    monkeypatch.setattr(client, "_probe_one", probe_one)
    results = asyncio.run(client.probe_all(("llm", "default")))

    assert started == ["KEY_A", "KEY_B", "KEY_C"]
    assert [result.key_env for result in results] == ["KEY_A", "KEY_B", "KEY_C"]


def test_probe_all_constructor_failures_become_ordered_results(monkeypatch):
    """子客户端构造失败也必须落入 ProbeResult，而不是泄漏 ExceptionGroup。"""
    for env_name, key in (("KEY_A", "ka"), ("KEY_B", "kb")):
        monkeypatch.setenv(env_name, key)
    prof = _llm_profile(api_key_envs=("KEY_A", "KEY_B"))
    client = _client(
        {"default": prof}, {}, _creds(llm={"default": ("ka", "kb")})
    )

    def fail_constructor(_target):
        raise RuntimeError(f"construct-{_target.env}")

    monkeypatch.setattr(client, "_probe_client", fail_constructor)
    results = asyncio.run(client.probe_all(("llm", "default")))

    assert [result.key_env for result in results] == ["KEY_A", "KEY_B"]
    assert [result.error for result in results] == ["construct-KEY_A", "construct-KEY_B"]
    assert all(result.ok is False for result in results)


@pytest.mark.parametrize("mode", ["raise", "self_cancel"])
def test_probe_all_child_cancellation_cleans_siblings(monkeypatch, mode):
    """直接抛取消与 task 自取消都必须清理同池 siblings。"""
    for env_name, key in (("KEY_A", "ka"), ("KEY_B", "kb"), ("KEY_C", "kc")):
        monkeypatch.setenv(env_name, key)
    prof = _llm_profile(api_key_envs=("KEY_A", "KEY_B", "KEY_C"))
    client = _client(
        {"default": prof}, {}, _creds(llm={"default": ("ka", "kb", "kc")})
    )
    all_started = asyncio.Event()
    never = asyncio.Event()
    cancelled = asyncio.CancelledError("key cancelled")
    started = cleaned = 0

    async def probe_one(target):
        nonlocal started, cleaned
        started += 1
        if started == 3:
            all_started.set()
        try:
            await asyncio.wait_for(all_started.wait(), timeout=1)
            if target.env == "KEY_A":
                if mode == "raise":
                    raise cancelled
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
            await never.wait()
        finally:
            cleaned += 1

    monkeypatch.setattr(client, "_probe_one", probe_one)
    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(asyncio.wait_for(
            client.probe_all(("llm", "default")),
            timeout=2,
        ))

    if mode == "raise":
        assert caught.value is cancelled
    assert started == 3 and cleaned == 3


def test_probe_all_external_cancellation_cleans_children(monkeypatch):
    """父探针取消应沿结构化取消路径清理全部密钥任务。"""
    for env_name, key in (("KEY_A", "ka"), ("KEY_B", "kb"), ("KEY_C", "kc")):
        monkeypatch.setenv(env_name, key)
    prof = _llm_profile(api_key_envs=("KEY_A", "KEY_B", "KEY_C"))
    client = _client(
        {"default": prof}, {}, _creds(llm={"default": ("ka", "kb", "kc")})
    )
    all_started = asyncio.Event()
    never = asyncio.Event()
    started = cleaned = 0

    async def probe_one(_target):
        nonlocal started, cleaned
        started += 1
        if started == 3:
            all_started.set()
        try:
            await never.wait()
        finally:
            cleaned += 1

    async def cancel_parent():
        task = asyncio.create_task(client.probe_all(("llm", "default")))
        await asyncio.wait_for(all_started.wait(), timeout=1)
        task.cancel("probe parent cancelled")
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value.args == ("probe parent cancelled",)

    monkeypatch.setattr(client, "_probe_one", probe_one)
    asyncio.run(cancel_parent())

    assert started == 3 and cleaned == 3


def test_probe_all_deduped_alias_keeps_first_declared_key_identity(monkeypatch):
    """同值别名坍缩后仍是声明层池，唯一结果保留首个 key_env。"""
    child = _ProbeChild()
    monkeypatch.setattr(LLMClient, "_probe_client", lambda self, target: child)
    prof = _llm_profile(api_key_envs=("KEY_A", "KEY_B"))
    client = _client({"default": prof}, {}, _creds(llm={"default": ("ka",)}))

    results = asyncio.run(client.probe_all(("llm", "default")))

    assert len(results) == 1
    assert results[0].ok is True and results[0].key_env == "KEY_A"


def test_probe_all_embedding_profile_follows_the_same_rule(monkeypatch):
    child = _ProbeChild()
    monkeypatch.setattr(LLMClient, "_probe_client", lambda self, target: child)
    emb = _embedding_profile(api_key_envs=("KEY_A", "KEY_B"))
    results = asyncio.run(_client(
        {}, {"embed": emb}, _creds(emb={"embed": ("ka", "kb")})).probe_all(
            ("embedding", "embed")))
    assert [r.key_env for r in results] == ["KEY_A", "KEY_B"]
    assert [r.model for r in results] == ["embed-model", "embed-model"]


def test_probe_same_named_profiles_uses_exact_resource_kind(monkeypatch):
    """同名 LLM 与 embedding 剖面分别命中自己的端点调用。"""
    child = _ProbeChild()
    monkeypatch.setattr(LLMClient, "_probe_client", lambda self, target: child)
    client = _client(
        {"shared": _llm_profile(name="shared")},
        {"shared": _embedding_profile(name="shared")},
        _creds(llm={"shared": ("llm-key",)}, emb={"shared": ("embedding-key",)}),
    )

    llm_result = asyncio.run(client.probe(("llm", "shared")))
    embedding_result = asyncio.run(client.probe(("embedding", "shared")))

    assert (llm_result.kind, embedding_result.kind) == ("llm", "embedding")
    assert [call[0] for call in child.calls] == ["complete", "embed"]


def test_probe_client_narrows_profile_to_one_key_and_shares_pool():
    prof = _llm_profile(api_key_envs=("KEY_A", "KEY_B"))
    client = _client({"default": prof}, {"embed": _embedding_profile()},
                       _creds())
    child = client._probe_client(_ProbeTarget(
        profile="default", prof=prof, is_llm=True, env="KEY_B", key="kb",
        key_env="KEY_B"))
    narrowed = child._llm_profiles["default"]
    assert narrowed.api_key_envs == ("KEY_B",)      # v1.17: 只收窄 env 名
    assert narrowed.api_key_env == "KEY_B"
    assert narrowed.max_output_tokens == 1          # 1 token 活体调用
    assert child._embedding_profiles == {}
    assert child._http_client is client._http()     # 共享连接池
    assert child._resources is client._resources    # 共享资源限制器
    assert child._credentials.llm["default"] == ("kb",)   # v1.17: 凭据收窄到单把密钥

    emb = client._embedding_profiles["embed"]
    echild = client._probe_client(_target(emb, profile="embed", is_llm=False))
    assert echild._llm_profiles == {}
    assert echild._embedding_profiles["embed"].api_key_envs == ("TEST_KEY",)
    assert echild._credentials.embedding["embed"] == ("sk-test",)  # 单把密钥直达子端
    asyncio.run(client.aclose())


def test_probe_call_llm_branch_sends_one_token_ping():
    prof = _llm_profile()
    client = _client({"default": prof}, {}, _creds())
    child = _ProbeChild(model="glm-x", latency_ms=42)
    model, latency_ms = asyncio.run(
        client._probe_call(child, _target(prof), time.monotonic()))
    assert (model, latency_ms) == ("glm-x", 42)     # 取子客户端回报的模型名与耗时
    (kind, profile, prompt), = child.calls
    assert (kind, profile) == ("complete", "default")
    assert prompt.messages == (
        Message(role="user", parts=(Part(kind="text", text="ping"),)),)


def test_probe_call_swallows_output_truncated_as_liveness():
    """v1.11 P6：max_output_tokens=1 的探针按构造必然终止于输出上限——那恰恰**就是**
    活体证明，不是探测失败。"""
    prof = _llm_profile()
    client = _client({"default": prof}, {}, _creds())
    child = _ProbeChild(raises=OutputTruncatedError(
        "cap", profile="default", finish="max_tokens"))
    model, latency_ms = asyncio.run(
        client._probe_call(child, _target(prof), time.monotonic()))
    assert model == "test-model"                    # 回落到剖面模型名
    assert latency_ms >= 0


def test_probe_call_embedding_branch_pings_embed():
    emb = _embedding_profile()
    client = _client({}, {"embed": emb}, _creds())
    child = _ProbeChild()
    model, _latency = asyncio.run(client._probe_call(
        child, _target(emb, profile="embed", is_llm=False), time.monotonic()))
    assert model == "embed-model"
    assert child.calls == [("embed", "embed", ["ping"])]


def test_probe_one_returns_failure_result_and_never_raises(monkeypatch):
    child = _ProbeChild(raises=RuntimeError("boom"))
    monkeypatch.setattr(LLMClient, "_probe_client", lambda self, target: child)
    prof = _llm_profile()
    client = _client({"default": prof}, {}, _creds())
    result = asyncio.run(client._probe_one(_target(prof, key_env="KEY_A")))
    assert result.ok is False and result.key_env == "KEY_A"
    assert result.model == "test-model" and "boom" in (result.error or "")
    # 失败路径同样把子客户端的用量并回父客户端
    assert client.usage_by_profile["default"].calls == 1


def test_probe_one_merges_child_usage_on_success(monkeypatch):
    child = _ProbeChild()
    monkeypatch.setattr(LLMClient, "_probe_client", lambda self, target: child)
    prof = _llm_profile()
    client = _client({"default": prof}, {}, _creds())
    result = asyncio.run(client._probe_one(_target(prof)))
    assert result.ok is True and result.error is None
    assert client.usage_by_profile["default"].calls == 1


# ── shared httpx client: lazy creation and release (spec 3.9.2 aclose) ─────


def test_http_client_is_a_lazy_shared_singleton():
    client = _client({"default": _llm_profile()}, {}, _creds())
    assert client._http_client is None              # 用到之前不建立
    first = client._http()
    assert client._http() is first
    # 超时由每次调用传入（_dispatch_attempt），不挂在客户端上
    assert (first.timeout.connect, first.timeout.read) == (None, None)
    asyncio.run(client.aclose())


def test_http_client_uses_exact_declared_connection_capacity(monkeypatch):
    """共享 HTTPX pool 的连接与 keepalive 上界都等于全部 origin 容量。"""
    captured = {}
    sentinel = object()

    def client_factory(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(llm_client.httpx, "AsyncClient", client_factory)
    client = _client(
        {"default": _llm_profile(max_concurrency=2)},
        {"embed": _embedding_profile(name="embed", max_concurrency=8)},
        _creds(),
    )

    assert client._http() is sentinel
    limits = captured["limits"]
    assert captured["timeout"] is None
    assert limits.max_connections == limits.max_keepalive_connections == 10


def test_aclose_releases_and_is_idempotent():
    client = _client({"default": _llm_profile()}, {}, _creds())
    client._http()
    asyncio.run(client.aclose())
    assert client._http_client is None
    asyncio.run(client.aclose())                    # 二次调用为 no-op
    assert client._http_client is None


# ── full-content trace gate (spec 7.4 full 档) ─────────────────────────────


def _trace_metrics(**over) -> SimpleNamespace:
    """构造只带 cfg.trace 的观测汇替身（M9 只从 metrics 上读 cfg）。"""
    return SimpleNamespace(cfg=SimpleNamespace(trace=TraceConfig(**over)))


@pytest.mark.parametrize("metrics,expected", [
    (None, False),                                              # 无观测汇
    (SimpleNamespace(cfg=SimpleNamespace()), False),            # cfg 没有 trace 属性
    (_trace_metrics(enabled=False, content="full", channels=("llm",)), False),
    (_trace_metrics(enabled=True, content="refs", channels=("llm",)), False),
    (_trace_metrics(enabled=True, content="full", channels=("quality",)), False),
    (_trace_metrics(enabled=True, content="full", channels=("quality", "llm")), True),
])
def test_full_content_trace_gate_is_a_three_way_conjunction(metrics, expected):
    client = _client({"default": _llm_profile()}, {}, _creds(), metrics=metrics)
    assert client._full_content_trace_enabled() is expected


def test_non_full_trace_tier_carries_no_gen_ai_message_keys():
    """这道门的唯一消费者：低于 full 档时 llm.call 载荷永不出现
    gen_ai.input.messages / gen_ai.output.messages（spec 7.4）。"""
    prof = _llm_profile()
    prompt = PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="text", text="hi"),)),))
    lean = _client({"default": prof}, {}, _creds(),
                     metrics=_trace_metrics(enabled=True, content="refs",
                                            channels=("llm",)))
    spec = lean._complete_spec(prof, prompt, None)
    assert spec.trace_extra == {} and spec.finalize_extra is None
    full = _client({"default": prof}, {}, _creds(),
                     metrics=_trace_metrics(enabled=True, content="full",
                                            channels=("llm",)))
    spec = full._complete_spec(prof, prompt, None)
    assert "gen_ai.input.messages" in spec.trace_extra
    assert spec.finalize_extra is not None


# ── key-pool events: payload discipline (spec 7.2 / 7.4) ───────────────────


def test_key_pool_events_carry_env_names_only(tmp_path, caplog, monkeypatch):
    """v1.6 的三条密钥池事件都落在 llm 通道，且只携带环境变量**名**——密钥**值**
    绝不出现在 trace 或 stderr 里（spec 7.4 红线）。"""
    from labelkit.common.observability.obslog import EventLog, MetricsSink
    from tests.common.observability.test_obslog import make_cfg

    sentinel = "sk-SENTINEL-VALUE-MUST-NEVER-BE-LOGGED"
    trace_path = tmp_path / "run.trace.jsonl"
    cfg = make_cfg(tmp_path, trace=TraceConfig(
        enabled=True, path=str(trace_path), channels=("llm",), content="refs"))
    sink = MetricsSink(cfg, "abcdef123456", EventLog(cfg.trace, "abcdef123456"))
    # v1.17 Wave 2b：哨兵密钥值经 RuntimeCredentials 进入客户端并材料化成池——
    # 值驻留进程内，但绝不出现在任何 trace / 日志 / repr 面。
    client = _client({"default": _llm_profile()}, {},
                       _creds(llm={"default": (sentinel,)}), metrics=sink)
    client._pool("llm", _llm_profile())
    assert sentinel not in repr(client._credentials)

    with caplog.at_level(logging.WARNING, logger="labelkit.llm"):
        client._emit_event("llm.key_cooldown",
                           {"profile": "default", "key_env": "KEY_A",
                            "cooldown_s": 30.0, "retry_after": True})
        client._emit_event("llm.key_disabled",
                           {"profile": "default", "key_env": "KEY_A",
                            "status_code": 401})
        client._emit_event("llm.pool_parked",
                           {"profile": "default", "wait_s": 12.0, "live_keys": 2})
    sink.event_log.close()

    rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert [r["ev"] for r in rows] == ["llm.key_cooldown", "llm.key_disabled",
                                       "llm.pool_parked"]
    assert {r["stage"] for r in rows} == {"llm"}
    assert all(r["record_ids"] == [] and r["batch_no"] == 0 for r in rows)
    assert set(rows[0]["payload"]) == {"profile", "key_env", "cooldown_s", "retry_after"}
    assert set(rows[1]["payload"]) == {"profile", "key_env", "status_code"}
    assert set(rows[2]["payload"]) == {"profile", "wait_s", "live_keys"}
    # key_disabled / pool_parked 镜像为 stderr WARN；key_cooldown 仅入 trace
    mirrored = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(mirrored) == 2
    assert mirrored[0].startswith("llm.key_disabled")
    assert mirrored[1].startswith("llm.pool_parked")
    # 红线：密钥值哪儿都不出现
    assert sentinel not in trace_path.read_text(encoding="utf-8")
    assert sentinel not in caplog.text


def test_emit_event_is_a_no_op_without_a_metrics_sink():
    _client({}, {}, _creds())._emit_event("llm.key_cooldown", {"profile": "default"})
    _client({}, {}, _creds(), metrics=SimpleNamespace())._emit_event(
        "llm.key_cooldown", {"profile": "default"})


# ── embedding metering shape adapter (spec 3.9.3 v1.2 embedding) ───────────


def test_split_embed_wraps_vectors_and_usage_for_the_retry_engine():
    prof = _embedding_profile()
    data = {"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]}],
            "usage": {"prompt_tokens": 6, "total_tokens": 6}}
    result = _split_embed(data, 1, prof)
    assert len(result) == 1                      # 单元素元组……
    vectors, usage = result[0]                   # ……内层为 (向量列表, 用量)
    assert vectors == [[1.0, 0.0, 0.0, 0.0]] and usage == Usage(6, 0)
    # 重试引擎用同一个取用量的口子给所有调用记账
    assert _result_usage(result) is usage
    with pytest.raises(ProviderFatalError):      # 条数/维数校验照常生效
        _split_embed({"data": []}, 1, prof)


# ── non-2xx classification (spec 3.9.5 V20 / F5) ───────────────────────────


def test_classify_http_failure_keeps_the_full_body_but_truncates_the_message():
    """F5：嗅探跑在**完整**响应体上，所以归类必须原样留全体，而失败消息自身仍截到
    300 字符。"""
    body = "x" * 1000
    failure = _classify_http_failure(httpx.Response(400, text=body))
    assert failure.body_text == body
    assert failure.message == "HTTP 400: " + "x" * 300
    assert failure.status_code == 400
    assert failure.retryable is False and failure.retry_after is None


def test_classify_http_failure_429_parses_retry_after_and_stays_retryable():
    failure = _classify_http_failure(
        httpx.Response(429, headers={"Retry-After": "5"}, text="slow down"))
    assert failure.retry_after == 5.0 and failure.retryable is True
    assert failure.status_code == 429 and failure.body_text == "slow down"
    # Retry-After 只在 429 上解析；其余可重试态一律 None
    other = _classify_http_failure(
        httpx.Response(503, headers={"Retry-After": "5"}, text="upstream down"))
    assert other.retryable is True and other.retry_after is None


# ══ retry engine: the step functions (spec 3.9.3 重试 / 限流 / 密钥池行) ═════
#
# 全部零网络：手工构造入参对象 → 直调步骤函数 → 断言状态转移与返回值。


class _EngineRecorder:
    """观测汇替身：把熔断喂入与事件按**调用序**记进同一条流水，便于断言三步顺序。

    桩住的是进程内协作者（M12 的位置），不是 HTTP 传输层。
    """

    def __init__(self, *, circuit_broken: bool = False):
        self.circuit_broken = circuit_broken
        self.journal: list[tuple] = []

    def record_provider_result(self, fatal: bool, hard: bool = False) -> None:
        self.journal.append(("breaker", fatal, hard))

    def event(self, ev: str, *, stage: str, batch_no: int,
              record_ids: tuple = (), payload=None) -> None:
        self.journal.append(("event", ev, stage, dict(payload or {})))

    def kinds(self) -> list[str]:
        """@return 流水里每一格的种类序列（"breaker" / "event"）。"""
        return [row[0] for row in self.journal]

    def payload(self, index: int) -> dict:
        """@return 第 index 格事件的载荷。"""
        return self.journal[index][3]


class _Clock:
    """可注入的单调时钟：``sleep`` 直接推进虚拟时刻，用例零真实等待。"""

    def __init__(self, start: float = 1000.0, on_sleep=None):
        self.now = start
        self.slept: list[float] = []
        self._on_sleep = on_sleep

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.slept.append(delay)
        self.now += delay
        if self._on_sleep is not None:
            self._on_sleep()


def _install_clock(monkeypatch, clock: _Clock) -> _Clock:
    """把虚拟时钟装进 llm_client 模块命名空间（只影响被测模块）。"""
    monkeypatch.setattr(llm_client, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(llm_client, "asyncio",
                        SimpleNamespace(sleep=clock.sleep,
                                        Semaphore=asyncio.Semaphore))
    return clock


def _spec(prof, *, kind: str = "llm", operation: str | None = None,
          trace_extra=None, finalize_extra=None, parse=None) -> _CallSpec:
    """构造一次逻辑调用的请求装配契约（步骤函数从不发请求，build_body 取平凡值）。"""
    return _CallSpec(
        kind=kind, prof=prof,
        url=prof.base_url.rstrip("/") + "/chat/completions",
        build_body=lambda: {"model": prof.model},
        parse=parse or (lambda data: _parse_openai_response(data, prof.model)),
        operation=operation, trace_extra=trace_extra,
        finalize_extra=finalize_extra)


def _engine(prof=None, *, keys=(("KEY_A", "ka"),), metrics=None,
            park_budget: float = 3600.0, spec=None):
    """装配「客户端 + 重试上下文」二元组。

    @return (LLMClient, _RetryContext)
    """
    prof = prof if prof is not None else _llm_profile()
    client = _client({prof.name: prof}, {}, _creds(llm={prof.name: tuple(k for _, k in keys)}), metrics=metrics)
    acc = client.usage_by_profile.setdefault(prof.name, ProfileUsage())
    ctx = _RetryContext(spec=spec or _spec(prof), pool=_KeyPool(list(keys)),
                        acc=acc, park_budget=park_budget)
    return client, ctx


def _key(ctx: _RetryContext, index: int = 0):
    """取第 index 把密钥的 (运行时状态, 用量累加器)。"""
    ks = ctx.pool.states[index]
    return ks, ctx.acc.keys.setdefault(ks.env, KeyUsage())


def _failure(**over) -> _AttemptFailure:
    """构造一次尝试级失败描述。"""
    defaults = dict(message="HTTP 500: upstream", status_code=500,
                    body_text="upstream", retry_after=None, retryable=True)
    defaults.update(over)
    return _AttemptFailure(**defaults)


# ── _RetryContext: the two accessors ───────────────────────────────────────


def test_retry_context_prof_and_key_extra_pool_gate():
    """key_env 只在池 > 1 且已有尝试选过密钥时才搭上 llm.call 载荷
    （spec 7.2 llm.call 行，v1.6）。"""
    prof = _llm_profile()
    trace_extra = {"gen_ai.input.messages": [{"role": "user", "content": []}]}
    spec = _spec(prof, trace_extra=trace_extra)

    _client, single = _engine(prof, spec=spec)
    assert single.prof is prof                      # 转发 spec.prof
    single.last_env = "KEY_A"
    assert single.key_extra() is trace_extra        # 池 = 1 → 原样返回

    _client, pooled = _engine(prof, keys=(("KEY_A", "ka"), ("KEY_B", "kb")),
                              spec=spec)
    assert pooled.key_extra() is trace_extra        # 零尝试 → 原样返回
    pooled.last_env = "KEY_B"
    merged = pooled.key_extra()
    assert merged["key_env"] == "KEY_B"
    assert merged["gen_ai.input.messages"] == trace_extra["gen_ai.input.messages"]
    assert "key_env" not in trace_extra             # 契约里的载荷永不被就地改写


# ── _guard_breaker: post-resource-permit re-check (spec 3.9.2 fast-fail) ───


def test_guard_breaker_passes_while_the_breaker_is_closed():
    rec = _EngineRecorder()
    client, ctx = _engine(metrics=rec)
    assert client._guard_breaker(ctx) is None
    assert rec.journal == []
    client, ctx = _engine()                         # 无观测汇 → 无从复检，静默通过
    assert client._guard_breaker(ctx) is None


def test_guard_breaker_aborts_after_resource_permit():
    """从未上线的调用不留痕；已经烧过重试的调用不能从 report.llm_usage 与 llm.call
    trace 里凭空消失（spec 7.2 status="breaker_aborted" = 退避途中熔断打开）。"""
    rec = _EngineRecorder(circuit_broken=True)
    client, ctx = _engine(metrics=rec)
    with pytest.raises(CircuitBreakerTripped):
        client._guard_breaker(ctx)
    assert rec.journal == [] and ctx.acc.retries == 0

    ctx.retries_used, ctx.latency_ms, ctx.last_env = 2, 31, "KEY_A"
    with pytest.raises(CircuitBreakerTripped):
        client._guard_breaker(ctx)
    assert ctx.acc.retries == 2                     # 已发生的尝试照常记账
    (kind, ev, stage, payload), = rec.journal
    assert (kind, ev, stage) == ("event", "llm.call", "llm")
    assert payload["status"] == "breaker_aborted"
    assert (payload["retries"], payload["latency_ms"]) == (2, 31)
    assert payload["gen_ai.usage.input_tokens"] == 0      # 失败终态一律空 Usage
    assert "key_env" not in payload                       # 池大小为 1


# ── _select_key: least-in-flight, dead pool, parking ───────────────────────


def test_select_key_prefers_least_in_flight_then_declaration_order(monkeypatch):
    clock = _install_clock(monkeypatch, _Clock())
    client, ctx = _engine(keys=(("KEY_A", "ka"), ("KEY_B", "kb"), ("KEY_C", "kc")))
    ctx.pool.states[0].in_flight = 2
    ctx.pool.states[1].in_flight = 1
    assert asyncio.run(client._select_key(ctx)).env == "KEY_C"
    ctx.pool.states[2].in_flight = 1
    assert asyncio.run(client._select_key(ctx)).env == "KEY_B"   # 平手取声明序靠前者
    assert clock.slept == []                                     # 有可用键 → 不驻留


def test_select_key_with_a_fully_dead_pool_is_terminal_fatal(monkeypatch):
    """防御性终态：打光最后一把键的那次调用早已硬熔断，本路径只在并发兄弟调用抢跑时
    才走得到。"""
    _install_clock(monkeypatch, _Clock())
    rec = _EngineRecorder()
    client, ctx = _engine(keys=(("KEY_A", "ka"), ("KEY_B", "kb")), metrics=rec)
    for state in ctx.pool.states:
        state.disabled = True
    ctx.last_env = "KEY_B"
    with pytest.raises(ProviderFatalError) as ei:
        asyncio.run(client._select_key(ctx))
    assert "auth-disabled" in str(ei.value)
    assert ei.value.profile == "default" and ei.value.key_env == "KEY_B"
    assert rec.kinds() == ["breaker", "event"]
    assert rec.journal[0] == ("breaker", True, False)
    assert rec.payload(1)["status"] == "fatal"


def test_select_key_parks_when_every_live_key_is_cooling(monkeypatch):
    clock = _install_clock(monkeypatch, _Clock())
    rec = _EngineRecorder()
    client, ctx = _engine(keys=(("KEY_A", "ka"), ("KEY_B", "kb")), metrics=rec)
    ctx.pool.states[0].cooldown_until = clock.now + 10.0
    ctx.pool.states[1].cooldown_until = clock.now + 25.0

    assert asyncio.run(client._select_key(ctx)) is None   # 驻留结束 → 回环顶重新选键
    assert clock.slept == [10.0]                          # 等到最早唤醒，一个分片
    assert ctx.parked is True and ctx.acc.parked_calls == 1
    assert ctx.acc.parked_ms == 10_000
    assert ctx.park_spent == pytest.approx(10.0)
    assert ctx.retries_used == 0                          # 驻留不消耗重试预算
    assert rec.payload(0) == {"profile": "default", "wait_s": 10.0, "live_keys": 2}
    assert rec.journal[0][1] == "llm.pool_parked"
    assert ctx.pool.select(clock.now).env == "KEY_A"      # 此刻冷却已结束


def test_park_slices_at_sixty_seconds(monkeypatch):
    clock = _install_clock(monkeypatch, _Clock())
    client, ctx = _engine(keys=(("KEY_A", "ka"), ("KEY_B", "kb")),
                          metrics=_EngineRecorder())
    asyncio.run(client._park(ctx, 150.0, 2, clock.now))
    assert clock.slept == [60.0, 60.0, 30.0]              # ≤ 60 s 分片
    assert ctx.acc.parked_ms == 150_000
    assert ctx.acc.parked_calls == 1
    # parked_calls 统计的是**逻辑调用**数，不是驻留段数
    asyncio.run(client._park(ctx, 5.0, 2, clock.now))
    assert ctx.acc.parked_calls == 1
    assert ctx.park_spent == pytest.approx(155.0)


def test_park_rechecks_the_breaker_between_slices(monkeypatch):
    rec = _EngineRecorder()
    clock = _install_clock(
        monkeypatch, _Clock(on_sleep=lambda: setattr(rec, "circuit_broken", True)))
    client, ctx = _engine(metrics=rec)
    asyncio.run(client._park(ctx, 150.0, 1, clock.now))
    assert clock.slept == [60.0]                          # 熔断打开 → 当片跳出
    assert ctx.acc.parked_ms == 60_000


@pytest.mark.parametrize("park_budget,spent", [(5.0, 0.0), (0.0, 0.0), (30.0, 25.0)])
def test_park_budget_overrun_fails_as_retry_exhaustion(monkeypatch, park_budget, spent):
    """run.max_park_s 超限（含 0 = 不驻留）走重试耗尽路径：记录 failed，终态计入
    熔断窗口。第三组是跨多段驻留累计后超限的形态。"""
    clock = _install_clock(monkeypatch, _Clock())
    rec = _EngineRecorder()
    client, ctx = _engine(metrics=rec, park_budget=park_budget)
    ctx.park_spent = spent
    ctx.pool.states[0].cooldown_until = clock.now + 10.0
    with pytest.raises(ProviderRetryableError) as ei:
        asyncio.run(client._select_key(ctx))
    assert "park budget exhausted" in str(ei.value)
    assert ei.value.retries == 0 and ei.value.profile == "default"
    assert clock.slept == []                              # 注定无望 → 不空耗墙钟
    assert rec.kinds() == ["breaker", "event"]
    assert rec.journal[0] == ("breaker", True, False)
    assert rec.payload(1)["status"] == "retryable_exhausted"


# ── _settle_2xx / _record_success ──────────────────────────────────────────


def _ok_response() -> httpx.Response:
    """构造一条形状正常的 openai 2xx 响应。"""
    return httpx.Response(200, json={
        "model": "qwen2.5", "choices": [
            {"index": 0, "finish_reason": "stop",
             "message": {"role": "assistant", "content": '{"answer":"ok"}'}}],
        "usage": {"prompt_tokens": 3184, "completion_tokens": 156}})


def test_settle_2xx_parses_and_records_success():
    rec = _EngineRecorder()
    client, ctx = _engine(metrics=rec)
    ctx.latency_ms = 4820
    ks, ku = _key(ctx)
    assert client._settle_2xx(ctx, ks, ku, _ok_response()) is None
    assert ctx.result == ('{"answer":"ok"}', None, Usage(3184, 156), "qwen2.5", "stop")
    assert ku.calls == 1
    assert rec.journal[0] == ("breaker", False, False)    # 成功清零熔断连击
    payload = rec.payload(1)
    assert payload["status"] == "ok"
    assert payload["gen_ai.usage.input_tokens"] == 3184
    assert payload["gen_ai.usage.output_tokens"] == 156
    assert payload["latency_ms"] == 4820


@pytest.mark.parametrize("resp,fragment", [
    (httpx.Response(200, text="<html>gateway</html>"), "unparseable JSON"),
    (httpx.Response(200, json={"choices": 42}), "malformed provider response"),
])
def test_settle_2xx_degrades_bad_bodies_into_retryable_failures(resp, fragment):
    """解析不了的 2xx 属于 provider 故障，而不是一个逃出 M9 的裸 AttributeError
    （spec 3.9.2 / CONTRACTS §7.8）。"""
    rec = _EngineRecorder()
    client, ctx = _engine(metrics=rec, spec=_spec(
        _llm_profile(), parse=lambda data: data["choices"][0]))
    ks, ku = _key(ctx)
    failure = client._settle_2xx(ctx, ks, ku, resp)
    assert failure is not None and failure.retryable is True
    assert failure.status_code is None and fragment in failure.message
    assert ctx.result is None and ku.calls == 0
    assert rec.journal == []                              # 还没到终态，不记账


def test_settle_2xx_reraises_a_fatal_parse_after_terminal_accounting():
    """embedding 条数/维数不符是 ProviderFatalError——终态先记账（熔断 + llm.call），
    异常再原样上抛出 M9。"""
    rec = _EngineRecorder()

    def _fatal_parse(_data):
        raise ProviderFatalError("dims mismatch", profile="default")

    client, ctx = _engine(metrics=rec, spec=_spec(_llm_profile(), parse=_fatal_parse))
    ks, ku = _key(ctx)
    with pytest.raises(ProviderFatalError):
        client._settle_2xx(ctx, ks, ku, _ok_response())
    assert rec.kinds() == ["breaker", "event"]
    assert rec.payload(1)["status"] == "fatal"


def test_record_success_clears_429_counter_and_feeds_the_p50_window():
    rec = _EngineRecorder()
    prof = _llm_profile()
    spec = _spec(prof, operation="embedding", trace_extra={"a": 1},
                 finalize_extra=lambda result: {"gen_ai.output.messages": [result[0]]})
    client, ctx = _engine(prof, keys=(("KEY_A", "ka"), ("KEY_B", "kb")),
                          metrics=rec, spec=spec)
    ctx.latency_ms, ctx.retries_used, ctx.last_env = 120, 1, "KEY_B"
    ks, ku = _key(ctx, 1)
    ks.consec_429 = 4
    client._record_success(ctx, ks, ku, ("hi", None, Usage(11, 3), "m", "stop"))

    assert ks.consec_429 == 0                     # **该密钥自身**的成功清零 c
    assert ku.calls == 1
    assert list(client._latencies[("llm", "default")]) == [120]
    assert client._latencies[("llm", "default")].maxlen == 256
    assert ctx.result == ("hi", None, Usage(11, 3), "m", "stop")
    payload = rec.payload(1)
    assert payload["operation"] == "embedding"
    assert payload["retries"] == 1 and payload["key_env"] == "KEY_B"
    assert payload["a"] == 1 and payload["gen_ai.output.messages"] == ["hi"]


# ── _absorb_auth_failure: per-key auth disable (spec 3.9.3 认证禁用) ────────


@pytest.mark.parametrize("status", [401, 403])
def test_absorb_auth_failure_disables_the_key_and_warns_once(status):
    rec = _EngineRecorder()
    client, ctx = _engine(keys=(("KEY_A", "ka"), ("KEY_B", "kb")), metrics=rec)
    ks, ku = _key(ctx)
    failure = _failure(message=f"HTTP {status}: bad key", status_code=status,
                       retryable=False)

    assert client._absorb_auth_failure(ctx, ks, ku, failure) is True
    assert ks.disabled is True and ku.disabled is True
    assert rec.journal == [("event", "llm.key_disabled", "llm",
                            {"profile": "default", "key_env": "KEY_A",
                             "status_code": status})]
    assert ctx.retries_used == 0                  # 吸收：不消耗重试、不喂熔断

    rec.journal.clear()                           # 并发在飞的调用观察到同一把键
    assert client._absorb_auth_failure(ctx, ks, ku, failure) is True
    assert rec.journal == []                      # 每密钥每运行至多一条事件


def test_absorb_auth_failure_passes_non_auth_failures_through():
    client, ctx = _engine(metrics=_EngineRecorder())
    ks, ku = _key(ctx)
    assert client._absorb_auth_failure(ctx, ks, ku, _failure(status_code=429)) is False
    assert client._absorb_auth_failure(ctx, ks, ku, _failure(status_code=None)) is False
    assert ks.disabled is False


def test_absorb_auth_failure_on_the_last_live_key_hard_trips():
    """打光最后一把活键 = v1.5 的认证首错语义：立即硬熔断 → 退出码 4
    （spec 3.9.3 密钥池行 / 3.10.3）。"""
    rec = _EngineRecorder()
    client, ctx = _engine(keys=(("KEY_A", "ka"), ("KEY_B", "kb")), metrics=rec)
    ctx.pool.states[1].disabled = True             # KEY_A 即最后一把活键
    ks, ku = _key(ctx)
    with pytest.raises(ProviderFatalError) as ei:
        client._absorb_auth_failure(ctx, ks, ku, _failure(
            message="HTTP 401: bad key", status_code=401, retryable=False))
    assert ei.value.status_code == 401 and ei.value.key_env == "KEY_A"
    assert rec.kinds() == ["event", "breaker", "event"]
    assert rec.journal[0][1] == "llm.key_disabled"
    assert rec.journal[1] == ("breaker", True, True)     # 硬熔断，不等连击攒够
    assert rec.journal[2][1] == "llm.call"
    assert rec.payload(2)["status"] == "fatal"


# ── _settle_non_retryable: 400/404 and the V20 sniff branch ────────────────


def test_settle_non_retryable_lets_retryable_failures_continue():
    rec = _EngineRecorder()
    client, ctx = _engine(metrics=rec)
    ks, _ku = _key(ctx)
    assert client._settle_non_retryable(ctx, ks, _failure(status_code=429)) is None
    assert client._settle_non_retryable(ctx, ks, _failure(status_code=503)) is None
    assert rec.journal == []


@pytest.mark.parametrize("status,body", [
    (404, "no such model"),
    (400, "bad request"),
    (400, _OVERFLOW_BODIES[0]),
])
def test_settle_non_retryable_routes_400_and_404_to_fatal(status, body):
    """预算关闭（context_window == 0）⇒ 嗅探不启用，连溢出形态的 400 都逐字节保持
    v1.10 的 fatal 老路。"""
    rec = _EngineRecorder()
    client, ctx = _engine(metrics=rec)
    ks, _ku = _key(ctx)
    with pytest.raises(ProviderFatalError) as ei:
        client._settle_non_retryable(ctx, ks, _failure(
            message=f"HTTP {status}: {body[:300]}", status_code=status,
            body_text=body, retryable=False))
    assert ei.value.status_code == status and ei.value.key_env == "KEY_A"
    assert rec.journal[0] == ("breaker", True, False)
    assert rec.payload(1)["status"] == "fatal"


def test_settle_non_retryable_sniffs_overflow_400_without_feeding_the_breaker():
    """V20/A7：reactive-400 的终态由**属主算子**补喂恰一次，M9 抛出时不喂——但
    llm.call 事件仍发 status="fatal"（它确实是一次 provider-fatal 形态的交互）。"""
    rec = _EngineRecorder()
    client, ctx = _engine(_llm_profile(context_window=131072), metrics=rec)
    ks, _ku = _key(ctx)
    with pytest.raises(ContextOverflowError) as ei:
        client._settle_non_retryable(ctx, ks, _failure(
            message="HTTP 400: too long", status_code=400,
            body_text=_OVERFLOW_BODIES[0], retryable=False))
    assert ei.value.phase == "reactive" and ei.value.origin == "http_400"
    assert ei.value.profile == "default"
    assert rec.kinds() == ["event"]                          # 熔断**不**喂
    assert rec.payload(0)["status"] == "fatal"


# ── _apply_429_cooldown: per-key cooldown (spec 3.9.3 每密钥 429 冷却) ──────


def test_apply_429_cooldown_honors_retry_after_in_full(monkeypatch):
    clock = _install_clock(monkeypatch, _Clock())
    rec = _EngineRecorder()
    client, ctx = _engine(metrics=rec)
    ks, ku = _key(ctx)
    client._apply_429_cooldown(ctx, ks, ku, _failure(
        message="HTTP 429", status_code=429, retry_after=30.0))
    assert ks.cooldown_until == pytest.approx(clock.now + 30.0)
    assert (ks.consec_429, ku.rate_limited) == (1, 1)
    assert rec.journal == [("event", "llm.key_cooldown", "llm",
                            {"profile": "default", "key_env": "KEY_A",
                             "cooldown_s": 30.0, "retry_after": True})]


def test_apply_429_cooldown_without_header_uses_the_cross_call_counter(monkeypatch):
    """无 Retry-After ⇒ 全抖动 random(0, base × 2^c) 且封顶 300 s；c = 该密钥
    **跨逻辑调用**累计的连续 429 计数。"""
    clock = _install_clock(monkeypatch, _Clock())
    rec = _EngineRecorder()
    client, ctx = _engine(metrics=rec)
    client._jitter_rng = random.Random(4242)
    mirror = random.Random(4242)
    ks, ku = _key(ctx)
    ks.consec_429 = 2                              # 由更早的逻辑调用累计而来

    client._apply_429_cooldown(ctx, ks, ku, _failure(status_code=429))
    assert ks.consec_429 == 3
    expected = mirror.uniform(0.0, _key_cooldown_upper(1.0, 3))
    assert ks.cooldown_until == pytest.approx(clock.now + expected)
    assert rec.payload(0)["retry_after"] is False
    assert rec.payload(0)["cooldown_s"] == pytest.approx(round(expected, 3))

    ks.consec_429 = 40                             # 指数远超封顶点
    client._apply_429_cooldown(ctx, ks, ku, _failure(status_code=429))
    assert ks.cooldown_until - clock.now <= 300.0
    assert ku.rate_limited == 2


def test_apply_429_cooldown_ignores_other_statuses(monkeypatch):
    _install_clock(monkeypatch, _Clock())
    rec = _EngineRecorder()
    client, ctx = _engine(metrics=rec)
    ks, ku = _key(ctx)
    client._apply_429_cooldown(ctx, ks, ku, _failure(status_code=500))
    assert (ks.cooldown_until, ks.consec_429, ku.rate_limited) == (0.0, 0, 0)
    assert rec.journal == []


# ── _guard_retry_budget / _settle_terminal ─────────────────────────────────


def test_guard_retry_budget_allows_attempts_below_max_retries():
    rec = _EngineRecorder()
    client, ctx = _engine(_llm_profile(max_retries=2), metrics=rec)
    ks, _ku = _key(ctx)
    ctx.attempt = 1
    assert client._guard_retry_budget(ctx, ks, _failure()) is None
    assert rec.journal == []


def test_guard_retry_budget_exhaustion_feeds_the_breaker_window():
    """spec 7.6 provider_retryable_exhausted：重试耗尽与 fatal 一样计入熔断窗口。"""
    rec = _EngineRecorder()
    client, ctx = _engine(_llm_profile(max_retries=2), metrics=rec)
    ks, _ku = _key(ctx)
    ctx.attempt, ctx.retries_used, ctx.latency_ms = 2, 2, 77
    with pytest.raises(ProviderRetryableError) as ei:
        client._guard_retry_budget(ctx, ks, _failure(message="HTTP 500: upstream"))
    assert ei.value.retries == 2 and ei.value.key_env == "KEY_A"
    assert "retries exhausted (2)" in str(ei.value)
    assert ctx.acc.retries == 2
    assert rec.journal[0] == ("breaker", True, False)
    payload = rec.payload(1)
    assert payload["status"] == "retryable_exhausted"
    assert (payload["retries"], payload["latency_ms"]) == (2, 77)


class _OrderWatcher(_EngineRecorder):
    """在每个副作用点抓一次 acc.retries 快照，用来钉住三步的**相对**次序。"""

    def __init__(self):
        super().__init__()
        self.acc: ProfileUsage | None = None
        self.retries_seen: list[tuple[str, int]] = []

    def record_provider_result(self, fatal: bool, hard: bool = False) -> None:
        self.retries_seen.append(("breaker", self.acc.retries))
        super().record_provider_result(fatal, hard)

    def event(self, ev: str, **kw) -> None:
        self.retries_seen.append((ev, self.acc.retries))
        super().event(ev, **kw)


def test_settle_terminal_three_step_order_is_frozen():
    """冻结的三步顺序：熔断计数 → retries 汇总 → llm.call 事件。"""
    rec = _OrderWatcher()
    client, ctx = _engine(keys=(("KEY_A", "ka"), ("KEY_B", "kb")), metrics=rec)
    rec.acc = ctx.acc
    ctx.retries_used, ctx.last_env = 3, "KEY_B"
    client._settle_terminal(ctx, "fatal")
    assert rec.kinds() == ["breaker", "event"]
    # 汇总夹在两者**中间**：熔断喂入先于它，事件后于它
    assert rec.retries_seen == [("breaker", 0), ("llm.call", 3)]
    assert ctx.acc.retries == 3
    assert rec.payload(1)["key_env"] == "KEY_B"          # 池 > 1 → key_env 随行

    rec.journal.clear()
    client._settle_terminal(ctx, "fatal", feed_breaker=False)   # reactive-400（A7）
    assert rec.kinds() == ["event"]

    rec.journal.clear()
    client._settle_terminal(ctx, "fatal", hard=True)            # 最后一把活键鉴权已死
    assert rec.journal[0] == ("breaker", True, True)


def test_settle_terminal_without_retries_leaves_the_accumulator_alone():
    rec = _EngineRecorder()
    client, ctx = _engine(metrics=rec)
    client._settle_terminal(ctx, "retryable_exhausted")
    assert ctx.acc.retries == 0
    assert rec.payload(1) == {
        "profile": "default", "gen_ai.request.model": "test-model",
        "latency_ms": 0, "gen_ai.usage.input_tokens": 0,
        "gen_ai.usage.output_tokens": 0, "retries": 0,
        "status": "retryable_exhausted"}


# ── _complete_spec: per-provider request contract assembly ─────────────────


def test_complete_spec_assembles_url_body_parser_per_provider():
    prompt = PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="text", text="hi"),)),))

    prof = _llm_profile(base_url="https://llm-gw.example.com/v1/")
    spec = _client({"default": prof}, {}, _creds())._complete_spec(prof, prompt, SCHEMA)
    assert spec.kind == "llm" and spec.operation is None
    assert spec.url == "https://llm-gw.example.com/v1/chat/completions"
    assert spec.build_body()["response_format"]["json_schema"]["schema"] == SCHEMA
    assert spec.parse({"model": "m", "choices": [
        {"message": {"content": "x"}, "finish_reason": "stop"}]}) == (
        "x", None, Usage(0, 0), "m", "stop")

    prof = _llm_profile(provider="anthropic")
    spec = _client({"default": prof}, {}, _creds())._complete_spec(prof, prompt, SCHEMA)
    assert spec.url == "https://llm-gw.example.com/v1/v1/messages"
    assert spec.build_body()["tools"] == [{
        "name": "emit",
        "description": (
            "Use this tool to return the final JSON object requested by the user. "
            "Populate every required field according to input_schema. "
            "Do not answer with prose or Markdown. Call the tool exactly once."
        ),
        "input_schema": SCHEMA,
    }]
    assert spec.parse({"content": [{"type": "text", "text": "y"}]})[0] == "y"
    assert spec.trace_extra == {} and spec.finalize_extra is None


def test_complete_spec_renders_both_message_faces_at_the_full_tier():
    prof = _llm_profile()
    prompt = PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="text", text="hi"),)),))
    client = _client({"default": prof}, {}, _creds(), metrics=_trace_metrics(
        enabled=True, content="full", channels=("llm",)))
    spec = client._complete_spec(prof, prompt, None)
    assert spec.trace_extra == {"gen_ai.input.messages": [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]}]}
    result = _parse_anthropic_response(
        {"content": [{"type": "tool_use", "input": {"a": 1}}]}, "m")
    assert spec.finalize_extra(result) == {"gen_ai.output.messages": [
        {"role": "assistant", "content": {"a": 1}}]}


# ── _feed_calibrator: the V19 sampling feed point ──────────────────────────


def test_feed_calibrator_samples_only_image_requests_with_usage(png_image: ImageRef):
    prof = _llm_profile()
    client = _client({"default": prof}, {}, _creds())
    prompt = PromptBundle(messages=(Message(role="user", parts=(
        Part(kind="text", text="[screenshot]"),
        Part(kind="image", image=png_image),
        Part(kind="image", image=png_image))),))

    client._feed_calibrator(prof, prompt, None, Usage(4000, 12))
    text_est = budget.est_prompt(prompt, prof, None, image_cost=0)
    expected = max(1, math.ceil((4000 - text_est) / 2))
    assert client.calibrator._current["default"] == [expected]

    text_only = PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="text", text="hi"),)),))
    client._feed_calibrator(prof, text_only, None, Usage(4000, 12))
    assert client.calibrator._current["default"] == [expected]   # 不含图 → 不记样本


def test_feed_calibrator_warns_once_per_profile_when_usage_is_missing(png_image, caplog):
    """[C-64] 企业网关会回 usage: null——不记样本、每剖面只 WARN 一次，此后无限期
    停留在先验 × PRIOR_INFLATION 上。"""
    prof = _llm_profile()
    client = _client({"default": prof}, {}, _creds())
    prompt = PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="image", image=png_image),)),))
    with caplog.at_level(logging.WARNING, logger="labelkit.llm"):
        client._feed_calibrator(prof, prompt, None, Usage(0, 0))
        client._feed_calibrator(prof, prompt, None, Usage(0, 0))
    assert "default" not in client.calibrator._current
    warnings = [r for r in caplog.records if "calibration inactive" in r.message]
    assert len(warnings) == 1
    assert client._calibration_warned == {"default"}


# ── _record_provider_result: the breaker feed point ────────────────────────


def test_record_provider_result_feeds_streak_and_hard_trip(tmp_path):
    from labelkit.common.observability.obslog import EventLog, MetricsSink
    from tests.common.observability.test_obslog import make_cfg

    cfg = make_cfg(tmp_path)                       # 阈值 fatal_error_threshold = 3
    sink = MetricsSink(cfg, "t", EventLog(cfg.trace, "t"))
    client = _client({}, {}, _creds(), metrics=sink)
    client._record_provider_result(fatal=True)
    client._record_provider_result(fatal=True)
    assert sink.fatal_streak == 2 and sink.circuit_broken is False
    client._record_provider_result(fatal=False)    # 任何一次成功都清零连击
    assert sink.fatal_streak == 0
    for _ in range(3):
        client._record_provider_result(fatal=True)
    assert sink.circuit_broken is True

    hard_sink = MetricsSink(cfg, "t", EventLog(cfg.trace, "t"))
    _client({}, {}, _creds(), metrics=hard_sink)._record_provider_result(fatal=True, hard=True)
    assert hard_sink.circuit_broken is True and hard_sink.fatal_streak == 1


def test_record_provider_result_is_a_no_op_without_a_sink():
    _client({}, {}, _creds())._record_provider_result(fatal=True)
    _client({}, {}, _creds(), metrics=SimpleNamespace())._record_provider_result(fatal=True)


def test_emit_llm_call_is_a_no_op_without_an_event_face():
    outcome = _CallOutcome(latency_ms=1, usage=Usage(1, 1), retries=0, status="ok")
    _client({}, {}, _creds())._emit_llm_call(_llm_profile(), outcome)
    _client({}, {}, _creds(), metrics=SimpleNamespace())._emit_llm_call(_llm_profile(), outcome)


def test_pool_materializes_once_per_kind_and_profile():
    prof = _llm_profile(api_key_envs=("KEY_A", "KEY_B"))
    client = _client({"default": prof}, {},
                       _creds(llm={"default": ("ka", "kb")}))
    first = client._pool("llm", prof)
    assert client._pool("llm", prof) is first          # 缓存命中，绝不重建
    client.usage_by_profile["default"].keys["KEY_A"].calls = 5
    assert client._pool("llm", prof).states[0].env == "KEY_A"
    assert client.usage_by_profile["default"].keys["KEY_A"].calls == 5   # 不被重新预置


def test_merge_usage_sums_child_cost_estimates():
    client = _client({}, {}, _creds())
    client._merge_usage({"p": ProfileUsage(calls=1, est_cost_usd=0.002)})
    client._merge_usage({"p": ProfileUsage(calls=1, est_cost_usd=0.004)})
    assert client.usage_by_profile["p"].est_cost_usd == pytest.approx(0.006)


def test_precheck_budget_skips_when_off_and_passes_when_it_fits():
    prompt = PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="text", text="hi"),)),))
    off = _llm_profile(context_window=0)
    # context_window == 0 → 该剖面预算关闭，整段终检跳过
    assert _client({"default": off}, {}, _creds())._precheck_budget(off, prompt, None) is None
    # 声明了窗口且余量充足 → 不变式成立，什么都不抛
    roomy = _llm_profile(context_window=131072)
    assert _client({"default": roomy}, {}, _creds())._precheck_budget(
        roomy, prompt, SCHEMA) is None


# ── complete() / embed(): the accounting around the retry engine ───────────
#
# 桩掉的是重试引擎这一**进程内协作者**（返回一份 parse 结果元组），既不起 mock
# 服务器也不替换传输层——HTTP 往返本身仍由真端点集成套件承保。


def _stub_engine(monkeypatch, result: tuple, *, latency_ms: int = 4820,
                 retries: int = 2) -> list:
    """把 _post_with_retries 换成返回定值的协程。

    @return 收到的 _CallSpec 列表（供断言请求契约装配）
    """
    seen: list = []

    async def _fake(self, spec):
        seen.append(spec)
        return result, latency_ms, retries

    monkeypatch.setattr(LLMClient, "_post_with_retries", _fake)
    return seen


def test_complete_meters_the_call_and_returns_the_parsed_response(monkeypatch,
                                                                  png_image: ImageRef):
    _stub_engine(monkeypatch, ("{}", None, Usage(3184, 156), "qwen2.5", "stop"))
    prof = _llm_profile(price_per_mtok_in=0.6, price_per_mtok_out=1.8)
    client = _client({"default": prof}, {}, _creds())
    prompt = PromptBundle(messages=(Message(role="user", parts=(
        Part(kind="text", text="[screenshot]"),
        Part(kind="image", image=png_image))),))

    resp = asyncio.run(client.complete("default", prompt, SCHEMA))
    assert resp == LLMResponse(text="{}", structured=None, usage=Usage(3184, 156),
                               model="qwen2.5", latency_ms=4820, finish="stop")
    acc = client.usage_by_profile["default"]
    assert (acc.calls, acc.prompt_tokens, acc.completion_tokens) == (1, 3184, 156)
    assert acc.retries == 2
    assert acc.est_cost_usd == pytest.approx(3184 / 1e6 * 0.6 + 156 / 1e6 * 1.8)
    assert client.calibrator._current["default"]      # 含图响应喂了一个校准样本


@pytest.mark.parametrize("finish,exc", [
    ("length", OutputTruncatedError),
    ("max_tokens", OutputTruncatedError),
    ("model_context_window_exceeded", ContextOverflowError),
])
def test_complete_disposes_the_finish_reason_after_accounting(monkeypatch, finish, exc):
    """V11/V24：两种终局都在成功记账**之后**抛出——HTTP 交互确实成功了，所以用量照记、
    熔断永不喂。"""
    _stub_engine(monkeypatch, ("partial", None, Usage(10, 4), "m", finish), retries=0)
    rec = _BreakerRecorder()
    client = _client({"default": _llm_profile()}, {}, _creds(), metrics=rec)
    prompt = PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="text", text="hi"),)),))
    with pytest.raises(exc):
        asyncio.run(client.complete("default", prompt))
    assert client.usage_by_profile["default"].calls == 1
    assert rec.results == []                          # M9 对这两种终局都不喂熔断


def test_complete_hides_the_schema_from_the_budget_when_l0_is_off(monkeypatch):
    """schema 只在真的随请求上行时才计价（L0 条款）——
    supports_structured_output=false 的剖面不该为它买单。"""
    seen = _stub_engine(monkeypatch, ("{}", None, Usage(1, 1), "m", "stop"))
    prof = _llm_profile(supports_structured_output=False, context_window=131072)
    client = _client({"default": prof}, {}, _creds())
    prompt = PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="text", text="hi"),)),))
    asyncio.run(client.complete("default", prompt, SCHEMA))
    assert "response_format" not in seen[0].build_body()


def test_embed_assembles_the_embeddings_call_and_meters_it(monkeypatch):
    vectors = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
    seen = _stub_engine(monkeypatch, ((vectors, Usage(6, 0)),), latency_ms=12,
                        retries=0)
    prof = _embedding_profile()
    client = _client({}, {"embed": prof}, _creds())
    assert asyncio.run(client.embed("embed", ["a", "b"])) == vectors

    (spec,) = seen
    assert spec.kind == "embedding" and spec.operation == "embedding"
    assert spec.url == "https://emb.example.com/v1/embeddings"
    assert spec.build_body() == {"model": "embed-model", "input": ["a", "b"]}
    assert spec.parse({"data": [{"index": 0, "embedding": vectors[0]},
                                {"index": 1, "embedding": vectors[1]}],
                       "usage": {"prompt_tokens": 6}}) == ((vectors, Usage(6, 0)),)
    acc = client.usage_by_profile["embed"]
    assert (acc.calls, acc.prompt_tokens) == (1, 6)
    assert acc.est_cost_usd is None                   # embedding 剖面不配价目


def test_complete_and_embed_fast_fail_once_the_breaker_is_open():
    rec = _BreakerRecorder()
    rec.circuit_broken = True
    client = _client({"default": _llm_profile()}, {"embed": _embedding_profile()}, _creds(),
                       metrics=rec)
    prompt = PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="text", text="hi"),)),))
    with pytest.raises(CircuitBreakerTripped):
        asyncio.run(client.complete("default", prompt))
    with pytest.raises(CircuitBreakerTripped):
        asyncio.run(client.embed("embed", ["a"]))
    assert client._http_client is None                # 从未派发过任何请求


def test_embedding_profiles_share_the_same_engine_steps(monkeypatch):
    """spec 3.9.3：embed() 走的是同一套重试/限流机制——(kind, name) 双键正是让同名的
    llm 与 embedding 剖面互不串用的那道分隔。"""
    clock = _install_clock(monkeypatch, _Clock())
    emb = _embedding_profile(name="default")
    rec = _EngineRecorder()
    client = _client({"default": _llm_profile()}, {"default": emb}, _creds(llm={"default": ("k",)}, emb={"default": ("k",)}), metrics=rec)
    result = ((([0.0] * 4,), Usage(6, 0)),)
    ctx = _RetryContext(
        spec=_spec(emb, kind="embedding", operation="embedding",
                   parse=lambda data: result),
        pool=client._pool("embedding", emb),
        acc=client.usage_by_profile.setdefault("default", ProfileUsage()),
        park_budget=3600.0)
    ctx.latency_ms = 12
    ks, ku = _key(ctx)
    client._record_success(ctx, ks, ku, result)
    assert list(client._latencies[("embedding", "default")]) == [12]
    assert ("llm", "default") not in client._latencies
    assert rec.payload(1)["operation"] == "embedding"
    assert rec.payload(1)["gen_ai.usage.input_tokens"] == 6
    assert clock.slept == []

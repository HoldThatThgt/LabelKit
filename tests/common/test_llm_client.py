"""Offline unit tests for M9 llm_client — pure logic only (no network, no mock LLMs):
backoff schedule, retryability classification, Retry-After parsing, request-body
assembly for both providers, response parsing, usage/cost accounting."""
from __future__ import annotations

import asyncio
import base64
import random
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest
from PIL import Image

from labelkit.common.config.model import EmbeddingProfile, LLMProfile
from labelkit.common.errors import ProviderFatalError
from labelkit.common.runtime.llm_client import (
    ANTHROPIC_VERSION,
    LLMClient,
    Message,
    Part,
    ProfileUsage,
    PromptBundle,
    _accumulate_usage,
    _backoff_delay,
    _build_anthropic_body,
    _build_embeddings_body,
    _build_headers,
    _build_openai_body,
    _is_retryable_status,
    _parse_anthropic_response,
    _parse_embeddings_response,
    _parse_openai_response,
    _parse_retry_after,
    _render_output_messages,
    _render_trace_messages,
)
from labelkit.common.contracts.types import ImageRef, Usage


# ── fixtures ────────────────────────────────────────────────────────────────

def _llm_profile(**over) -> LLMProfile:
    defaults = dict(
        name="default", provider="openai_compatible",
        base_url="https://llm-gw.example.com/v1", model="test-model",
        api_key_env="TEST_KEY", max_concurrency=2, timeout_s=30, max_retries=5,
        retry_base_delay_s=1.0, supports_structured_output=True,
        supports_vision=True, max_output_tokens=4096, temperature=0.0,
        max_image_px=2048, api_key="sk-test")
    defaults.update(over)
    return LLMProfile(**defaults)


def _embedding_profile(**over) -> EmbeddingProfile:
    defaults = dict(name="embed", base_url="https://emb.example.com/v1",
                    model="embed-model", api_key_env="TEST_KEY", dims=4,
                    api_key="sk-test")
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
        "tools": [{"name": "emit", "input_schema": SCHEMA}],
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
        "usage": {"input_tokens": 31, "output_tokens": 8}}
    text, structured, usage, model = _parse_anthropic_response(data, "fallback")
    assert structured == {"answer": "登录页"}
    assert text == ""
    assert usage == Usage(31, 8)
    assert model == "glm-x"


def test_anthropic_parse_text_fallback_and_thinking_skipped():
    data = {"content": [
        {"type": "thinking", "thinking": "..."},
        {"type": "text", "text": "第一段"},
        {"type": "text", "text": "第二段"}],
        "usage": {"input_tokens": 10, "output_tokens": 5}}
    text, structured, usage, model = _parse_anthropic_response(data, "fallback")
    assert structured is None
    assert text == "第一段\n第二段"
    assert model == "fallback"          # no model in payload → profile model


def test_openai_parse_text_and_usage():
    data = {"model": "qwen2.5", "choices": [
        {"index": 0, "finish_reason": "stop",
         "message": {"role": "assistant", "content": '{"answer":"ok"}'}}],
        "usage": {"prompt_tokens": 3184, "completion_tokens": 156, "total_tokens": 3340}}
    text, structured, usage, model = _parse_openai_response(data, "fallback")
    assert text == '{"answer":"ok"}'
    assert structured is None            # openai json_schema output stays text (M8 parses)
    assert usage == Usage(3184, 156)
    assert model == "qwen2.5"


def test_openai_parse_missing_bits_degrade_to_defaults():
    text, structured, usage, model = _parse_openai_response({}, "fb")
    assert (text, structured, usage, model) == ("", None, Usage(0, 0), "fb")


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
    text, structured, usage, model = _parse_openai_response(data, "fb")
    assert (text, structured, usage, model) == ("", None, Usage(0, 0), "fb")


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

    client = LLMClient({"default": _llm_profile()}, {}, metrics=_Recorder())
    extra = {"gen_ai.input.messages": [{"role": "user", "content": []}]}
    merged = dict(extra)
    merged.update({"gen_ai.output.messages": _render_output_messages("hi", None)})
    client._emit_llm_call(_llm_profile(), latency_ms=12, usage=Usage(3, 1),
                          retries=0, status="ok", operation=None, extra=merged)
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
    client = LLMClient({}, {})
    prompt = PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="text", text="hi"),)),))
    with pytest.raises(ValueError):
        asyncio.run(client.complete("nope", prompt))


def test_embed_rejects_llm_profile_names():
    client = LLMClient({"default": _llm_profile()}, {})
    with pytest.raises(ValueError, match=r"\[llm\.\*\] name"):
        asyncio.run(client.embed("default", ["x"]))
    with pytest.raises(ValueError):
        asyncio.run(client.embed("missing", ["x"]))


def test_semaphore_shared_per_profile_with_configured_bound():
    client = LLMClient({"default": _llm_profile(max_concurrency=2)}, {})
    sem1 = client._semaphore("llm", "default", 2)
    sem2 = client._semaphore("llm", "default", 2)
    assert sem1 is sem2
    assert sem1._value == 2
    # embedding namespace never collides with an llm profile of the same name
    sem3 = client._semaphore("embedding", "default", 8)
    assert sem3 is not sem1


def test_probe_unknown_profile_never_raises():
    client = LLMClient({}, {})
    result = asyncio.run(client.probe("ghost"))
    assert result.ok is False
    assert result.profile == "ghost"
    assert "unknown profile" in (result.error or "")

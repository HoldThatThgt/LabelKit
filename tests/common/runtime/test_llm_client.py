"""Offline unit tests for M9 llm_client — pure logic only (no network, no mock LLMs):
backoff schedule, retryability classification, Retry-After parsing, request-body
assembly for both providers, response parsing, usage/cost accounting."""
from __future__ import annotations

import asyncio
import base64
import random
from collections import deque
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest
from PIL import Image

from labelkit.common.config.model import EmbeddingProfile, LLMProfile
from labelkit.common.errors import ProviderFatalError
from labelkit.common.runtime.llm_client import (
    ANTHROPIC_VERSION,
    KeySnapshot,
    KeyUsage,
    LLMClient,
    Message,
    Part,
    ProbeResult,
    ProfileSnapshot,
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
    _key_cooldown_upper,
    _KeyPool,
    _pool_members,
    _render_output_messages,
    _render_trace_messages,
)
from labelkit.common.contracts.types import ImageRef, Usage
from tests.common.config.test_config import BASE_CONFIG, Env, env, has  # noqa: F401 (fixture)


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


# ── v1.6 key-pool configuration and pure logic ─────────────────────────────

POOL_CONFIG = BASE_CONFIG.replace(
    'api_key_env = "LK_TEST_KEY_DEFAULT"',
    'api_key_envs = ["LK_TEST_KEY_A", "LK_TEST_KEY_B"]',
    1,
)


# ── M1: api_key_envs parsing / validation / normalization ──────────────────


def test_pool_parses_and_resolves_every_key(env, monkeypatch):
    monkeypatch.setenv("LK_TEST_KEY_A", "sk-a")
    monkeypatch.setenv("LK_TEST_KEY_B", "sk-b")
    cfg = env.load(config_text=POOL_CONFIG)
    prof = cfg.llm_profiles["default"]
    assert prof.api_key_envs == ("LK_TEST_KEY_A", "LK_TEST_KEY_B")
    assert prof.api_keys == ("sk-a", "sk-b")
    # api_key_env / api_key mirror element 0 (CONTRACTS §6.1, v1.6)
    assert prof.api_key_env == "LK_TEST_KEY_A"
    assert prof.api_key == "sk-a"


def test_scalar_form_normalizes_to_one_tuple(env):
    """Existing single-key configs parse to a pool of one — v1.5 compat."""
    cfg = env.load()
    prof = cfg.llm_profiles["default"]
    assert prof.api_key_envs == ("LK_TEST_KEY_DEFAULT",)
    assert prof.api_keys == ("sk-default",)
    assert prof.api_key == "sk-default"


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
    has(errors, "互斥")


def test_neither_form_is_config_error(env):
    neither = BASE_CONFIG.replace('api_key_env = "LK_TEST_KEY_DEFAULT"\n', "", 1)
    errors = env.errors(config_text=neither)
    has(errors, "[llm.default].api_key_env")
    has(errors, "恰提供其一")


def test_empty_array_is_config_error(env):
    empty = BASE_CONFIG.replace(
        'api_key_env = "LK_TEST_KEY_DEFAULT"', "api_key_envs = []", 1)
    errors = env.errors(config_text=empty)
    has(errors, "[llm.default].api_key_envs")
    has(errors, "非空")


def test_duplicate_env_names_are_config_error(env, monkeypatch):
    monkeypatch.setenv("LK_TEST_KEY_A", "sk-a")
    dup = BASE_CONFIG.replace(
        'api_key_env = "LK_TEST_KEY_DEFAULT"',
        'api_key_envs = ["LK_TEST_KEY_A", "LK_TEST_KEY_A"]', 1)
    errors = env.errors(config_text=dup)
    has(errors, "[llm.default].api_key_envs[2]")
    has(errors, "重复")


def test_missing_env_vars_reported_per_element(env, monkeypatch):
    """Rule 12 (v1.6): EVERY listed variable of a referenced profile must be
    set — one aggregated error line per missing variable, [N] addressed."""
    monkeypatch.setenv("LK_TEST_KEY_B", "sk-b")
    monkeypatch.delenv("LK_TEST_KEY_A", raising=False)
    monkeypatch.delenv("LK_TEST_KEY_C", raising=False)
    three = BASE_CONFIG.replace(
        'api_key_env = "LK_TEST_KEY_DEFAULT"',
        'api_key_envs = ["LK_TEST_KEY_A", "LK_TEST_KEY_B", "LK_TEST_KEY_C"]', 1)
    errors = env.errors(config_text=three)
    has(errors, "[llm.default].api_key_envs[1]")
    has(errors, "[llm.default].api_key_envs[3]")
    assert not any("api_key_envs[2]" in e for e in errors)


def test_unreferenced_pooled_profile_needs_no_keys(env):
    """Rule 12 scope unchanged: unreferenced profiles are never resolved."""
    pooled_judge = BASE_CONFIG.replace(
        'api_key_env = "LK_TEST_KEY_JUDGE"',
        'api_key_envs = ["LK_TEST_KEY_J1", "LK_TEST_KEY_J2"]', 1)
    cfg = env.load(config_text=pooled_judge)   # judge unreferenced → no error
    assert cfg.llm_profiles["judge"].api_keys == ()


def test_embedding_pool_resolves_via_semantic_reference(env, monkeypatch):
    monkeypatch.setenv("LK_TEST_KEY_E1", "sk-e1")
    monkeypatch.delenv("LK_TEST_KEY_E2", raising=False)
    emb_pool = BASE_CONFIG.replace(
        'api_key_env = "LK_TEST_KEY_EMB"',
        'api_key_envs = ["LK_TEST_KEY_E1", "LK_TEST_KEY_E2"]', 1)
    body = '[dedup]\nsemantic = true\nsemantic_embedding = "emb"'
    errors = env.errors(config_text=emb_pool,
                        project_text=env.project(body=body))
    has(errors, "[embedding.emb].api_key_envs[2]")
    monkeypatch.setenv("LK_TEST_KEY_E2", "sk-e2")
    cfg = env.load(config_text=emb_pool, project_text=env.project(body=body))
    assert cfg.embedding_profiles["emb"].api_keys == ("sk-e1", "sk-e2")


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


def test_pool_members_normalized_profile():
    prof = _prof(api_key_envs=("E1", "E2"), api_keys=("k1", "k2"))
    assert _pool_members(prof) == [("E1", "k1"), ("E2", "k2")]


def test_pool_members_single_key_fallback_matches_v15():
    """Directly-constructed single-key profiles (tests, probe children) keep
    the pre-v1.6 api_key → env fallback."""
    assert _pool_members(_prof(api_key="k")) == [("E1", "k")]


def test_pool_members_env_fallback(monkeypatch):
    monkeypatch.setenv("E1", "k1")
    monkeypatch.setenv("E2", "k2")
    prof = _prof(api_key_envs=("E1", "E2"))             # api_keys unresolved
    assert _pool_members(prof) == [("E1", "k1"), ("E2", "k2")]


def test_pool_members_embedding_profile():
    prof = EmbeddingProfile(name="e", base_url="https://x", model="m",
                            api_key_env="E1", api_key_envs=("E1",),
                            api_keys=("k1",))
    assert _pool_members(prof) == [("E1", "k1")]


# ── M9: usage merging / probe shape ─────────────────────────────────────────


def test_merge_usage_merges_keys_and_park_stats():
    client = LLMClient({}, {})
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
    r = ProbeResult(profile="p", ok=True, model="m", latency_ms=1)
    assert r.key_env is None


def test_pool_creation_preseeds_key_usage_for_pools():
    """Report gate fix (review): every member of a pooled profile appears in
    ProfileUsage.keys from pool creation — serialized traffic that only ever
    selects key 0 must not make a pool look single-key in report.llm_usage."""
    prof = _prof(api_key_envs=("E1", "E2"), api_keys=("k1", "k2"))
    client = LLMClient({"p": prof}, {})
    client._pool("llm", prof)
    keys = client.usage_by_profile["p"].keys
    assert set(keys) == {"E1", "E2"}
    assert all(ku.calls == 0 and ku.rate_limited == 0 and not ku.disabled
               for ku in keys.values())


def test_pool_creation_does_not_seed_single_key_profiles():
    prof = _prof(api_key="k")
    client = LLMClient({"p": prof}, {})
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
    assert LLMClient({}, {}, sink)._max_park_s() == 3600.0
    cfg0 = dc_replace(cfg, run=dc_replace(cfg.run, max_park_s=0))
    sink0 = MetricsSink(cfg0, "t", EventLog(cfg0.trace, "t"))
    assert LLMClient({}, {}, sink0)._max_park_s() == 0.0
    assert LLMClient({}, {})._max_park_s() == 3600.0


# ── v1.10: snapshot() read-only console pull (spec 3.9.2/3.9.3 快照行) ──────


def test_snapshot_unmaterialized_pool_zero_values():
    """No traffic yet: keys derive from the DECLARED env names, everything
    else is zero/None — and snapshot() must NOT materialize self._pools."""
    client = LLMClient({"default": _llm_profile()}, {"embed": _embedding_profile()})
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
    prof = _llm_profile(api_key_envs=("KEY_A", "KEY_B"), api_keys=("ka", "kb"))
    client = LLMClient({"default": prof}, {})
    (snap,) = client.snapshot()
    assert snap.keys == (KeySnapshot(env="KEY_A", state="ok"),
                         KeySnapshot(env="KEY_B", state="ok"))
    assert client._pools == {}


def test_snapshot_enumerates_llm_then_embedding_in_declaration_order():
    client = LLMClient(
        {"default": _llm_profile(), "judge": _llm_profile(name="judge")},
        {"embed": _embedding_profile()})
    assert [(s.kind, s.name) for s in client.snapshot()] == [
        ("llm", "default"), ("llm", "judge"), ("embedding", "embed")]


def test_snapshot_key_states_cooldown_remaining_with_injected_now():
    """Pool three-state row (spec 3.9.2): disabled wins over a future cooldown;
    cooldown carries ceil remaining seconds; deadline-passed keys are ok.
    in_flight = Σ key in_flight."""
    prof = _llm_profile(api_key_envs=("KEY_A", "KEY_B", "KEY_C"),
                        api_keys=("ka", "kb", "kc"))
    client = LLMClient({"default": prof}, {})
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
    client = LLMClient({"default": _llm_profile()}, {})
    client._usage["default"] = ProfileUsage(
        calls=3, prompt_tokens=9552, completion_tokens=486, retries=2,
        est_cost_usd=0.0066)
    (snap,) = client.snapshot()
    assert (snap.calls, snap.retries) == (3, 2)
    assert (snap.prompt_tokens, snap.completion_tokens) == (9552, 486)
    assert snap.est_cost_usd == 0.0066


def test_snapshot_p50_median_window_and_none_when_empty():
    client = LLMClient({"default": _llm_profile()}, {})
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
    client = LLMClient({"default": _llm_profile()}, {})
    window = client._latencies.setdefault(("llm", "default"), deque(maxlen=256))
    for v in range(300):                            # 0..299 → window keeps 44..299
        window.append(v)
    (snap,) = client.snapshot()
    assert len(window) == 256
    assert snap.p50_latency_ms == int((171 + 172) / 2)


def test_snapshot_kind_disambiguates_same_name_profiles():
    """spec 3.9.2: _usage buckets by NAME (existing quirk) — kind disambiguates
    the snapshot identity, and the p50 window is keyed by (kind, name)."""
    client = LLMClient({"shared": _llm_profile(name="shared")},
                       {"shared": _embedding_profile(name="shared")})
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
    prof = _llm_profile(api_key_envs=("KEY_A", "KEY_B"), api_keys=("ka", "kb"))
    emb = _embedding_profile()
    client = LLMClient({"default": prof}, {"embed": emb})
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
    prof = _llm_profile(api_key_envs=("KEY_A", "KEY_B"), api_keys=("ka", "kb"))
    client = LLMClient({"default": prof}, {})
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
    prof = _llm_profile(api_key_envs=("KEY_A", "KEY_B"), api_keys=("ka", "kb"))
    client = LLMClient({"default": prof}, {})
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

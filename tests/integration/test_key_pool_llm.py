"""v1.6 key-pool integration tests against the REAL endpoint (glm-5.2 via
api.z.ai). No mocks — per project policy the rotation / auth-disable paths are
exercised with real HTTP: pool members alias the one real key (rotation), and
the 401-disable path uses a deliberately INVALID key value in a second env var
(a genuine 401 from the provider, zero mock infrastructure).

Auto-skipped by tests/conftest.py when LABELKIT_ZAI_KEY is absent.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from labelkit.config.model import LLMProfile
from labelkit.errors import ProviderFatalError
from labelkit.llm_client import LLMClient, Message, Part, PromptBundle
from labelkit.obslog import EventLog, MetricsSink
from tests.conftest import ZAI_BASE_URL, ZAI_KEY_ENV, ZAI_MODEL
from tests.test_obslog import make_cfg as obslog_cfg

pytestmark = pytest.mark.integration

BOGUS_KEY = "definitely-not-a-key"


def _pool_profile(envs_keys: list[tuple[str, str]], **over) -> LLMProfile:
    """Pooled profile whose members are (env-var name, key value) pairs; the
    env vars are exported so the M1-normalized shape is faithful."""
    for env, key in envs_keys:
        os.environ[env] = key
    envs = tuple(env for env, _ in envs_keys)
    keys = tuple(key for _, key in envs_keys)
    defaults = dict(
        name="default",
        provider="anthropic",
        base_url=ZAI_BASE_URL,
        model=ZAI_MODEL,
        api_key_env=envs[0],
        api_key=keys[0],
        api_key_envs=envs,
        api_keys=keys,
        max_concurrency=2,
        timeout_s=120,
        max_retries=2,
        retry_base_delay_s=1.0,
        max_output_tokens=128,
        temperature=0.0,
    )
    defaults.update(over)
    return LLMProfile(**defaults)


def _prompt(text: str) -> PromptBundle:
    return PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="text", text=text),)),))


def _real_key() -> str:
    return os.environ[ZAI_KEY_ENV]


async def test_rotation_two_aliases_of_real_key():
    """Both pool members alias the ONE real key: least-in-flight selection must
    spread concurrent calls across both, and every call succeeds."""
    prof = _pool_profile([("LK_POOL_ITEST_A", _real_key()),
                          ("LK_POOL_ITEST_B", _real_key())])
    client = LLMClient({"default": prof}, {})
    prompts = [_prompt(f"{n}+{n} 等于几？只回答数字。") for n in (1, 2, 3, 4)]
    try:
        responses = await asyncio.gather(
            *(client.complete("default", p) for p in prompts))
    finally:
        await client.aclose()
    assert len(responses) == 4 and all(r.text.strip() for r in responses)
    usage = client.usage_by_profile["default"]
    assert usage.calls == 4
    key_calls = {env: ku.calls for env, ku in usage.keys.items()}
    assert sum(key_calls.values()) == 4
    # max_concurrency=2 + in-flight accounting → both keys carry traffic
    assert key_calls.get("LK_POOL_ITEST_A", 0) >= 1
    assert key_calls.get("LK_POOL_ITEST_B", 0) >= 1
    assert not any(ku.disabled for ku in usage.keys.values())


async def test_bogus_first_key_absorbed_and_rotates(tmp_path):
    """A revoked/invalid key must NOT kill a pool with healthy siblings
    (spec 3.9.3 认证禁用): the real 401 disables the key, the SAME attempt
    re-dispatches on the good key, no retry budget is consumed, and nothing
    feeds the breaker. llm.key_disabled fires exactly once per key (7.2)."""
    import json

    from labelkit.config.model import TraceConfig

    prof = _pool_profile([("LK_POOL_ITEST_BAD", BOGUS_KEY),
                          ("LK_POOL_ITEST_GOOD", _real_key())],
                         max_concurrency=1)
    trace_path = tmp_path / "pool.trace.jsonl"
    cfg = obslog_cfg(tmp_path, trace=TraceConfig(
        enabled=True, path=str(trace_path), channels=("llm",)))
    log = EventLog(cfg.trace, "itest")
    sink = MetricsSink(cfg, "itest", log)
    client = LLMClient({"default": prof}, {}, sink)
    try:
        resp = await client.complete("default", _prompt("1+1 等于几？只回答数字。"))
    finally:
        await client.aclose()
        log.flush()
        log.close()
    assert resp.text.strip()
    usage = client.usage_by_profile["default"]
    assert usage.keys["LK_POOL_ITEST_BAD"].disabled is True
    assert usage.keys["LK_POOL_ITEST_GOOD"].calls == 1
    assert usage.retries == 0                     # absorbed: no retry consumed
    assert sink.circuit_broken is False           # nothing fed to the breaker
    events = [json.loads(line) for line in
              trace_path.read_text(encoding="utf-8").splitlines()]
    disabled = [e for e in events if e["ev"] == "llm.key_disabled"]
    assert len(disabled) == 1                     # at most once per key per run
    assert disabled[0]["payload"]["key_env"] == "LK_POOL_ITEST_BAD"
    calls = [e for e in events if e["ev"] == "llm.call"]
    assert len(calls) == 1 and calls[0]["payload"]["status"] == "ok"
    assert calls[0]["payload"]["key_env"] == "LK_POOL_ITEST_GOOD"


async def test_all_keys_bogus_last_live_key_hard_trips(tmp_path):
    """Pool generalization of the P2-3 guarantee: when the LAST live key
    auth-fails, the run trips immediately (hard) — a fully-revoked pool can
    never grind on silently."""
    prof = _pool_profile([("LK_POOL_ITEST_BAD1", BOGUS_KEY),
                          ("LK_POOL_ITEST_BAD2", BOGUS_KEY + "-2")],
                         max_concurrency=1)
    cfg = obslog_cfg(tmp_path)
    sink = MetricsSink(cfg, "itest", EventLog(cfg.trace, "itest"))
    client = LLMClient({"default": prof}, {}, sink)
    try:
        with pytest.raises(ProviderFatalError) as ei:
            await client.complete("default", _prompt("ping"))
    finally:
        await client.aclose()
    assert ei.value.status_code in (401, 403)
    assert ei.value.key_env == "LK_POOL_ITEST_BAD2"   # the last live key
    assert sink.circuit_broken is True                # hard trip preserved
    usage = client.usage_by_profile["default"]
    assert usage.keys["LK_POOL_ITEST_BAD1"].disabled is True
    assert usage.keys["LK_POOL_ITEST_BAD2"].disabled is True


async def test_probe_all_pooled_probes_every_key():
    prof = _pool_profile([("LK_POOL_ITEST_P1", _real_key()),
                          ("LK_POOL_ITEST_P2", _real_key())])
    client = LLMClient({"default": prof}, {})
    try:
        results = await client.probe_all("default")
        single = await client.probe("default")
    finally:
        await client.aclose()
    assert [r.key_env for r in results] == ["LK_POOL_ITEST_P1", "LK_POOL_ITEST_P2"]
    assert all(r.ok for r in results), [r.error for r in results]
    # probe() keeps the legacy single-result shape (first key, key_env=None)
    assert single.ok and single.key_env is None

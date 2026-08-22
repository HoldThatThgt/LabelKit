"""v1.6 key-pool integration tests against the REAL endpoint (glm-5.2 via
api.z.ai). No mocks — per project policy the rotation / auth-disable paths are
exercised with real HTTP: distinct key values rotate across pool members, and
the 401-disable path uses a deliberately INVALID key value (a genuine 401 from
the provider, zero mock infrastructure).

v1.17 Wave 2b（CONTRACTS §7.19.3）：密钥值经 RuntimeCredentials 进入客户端——
两个环境变量别名**同一**密钥值时，凭据构造期的值去重让池内只剩一把键（首声明
者获得 key_env 身份位）；跨**不同**密钥值的轮换/鉴权禁用语义与 v1.6 一致。

Auto-skipped by tests/conftest.py when LABELKIT_ZAI_KEY is absent.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from labelkit.common.config.model import LLMProfile
from labelkit.common.errors import ProviderFatalError
from labelkit.common.inference.credentials import RuntimeCredentials
from labelkit.common.inference.llm_client import Message, Part, PromptBundle
from labelkit.common.observability.obslog import EventLog, MetricsSink
from tests.conftest import ZAI_BASE_URL, ZAI_KEY_ENV, ZAI_MODEL
from tests.common.observability.test_obslog import make_cfg as obslog_cfg
from tests.llm_client_helpers import make_llm_client as _client

pytestmark = pytest.mark.integration

BOGUS_KEY = "definitely-not-a-key"


def _pool_profile(envs: list[str], **over) -> LLMProfile:
    """Pooled profile declaring the given env-var names (values live in creds)."""
    names = tuple(envs)
    defaults = dict(
        name="default",
        provider="anthropic",
        base_url=ZAI_BASE_URL,
        model=ZAI_MODEL,
        api_key_env=names[0],
        api_key_envs=names,
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


def _pool_creds(values: list[str]) -> RuntimeCredentials:
    """凭据携带池成员的密钥值——§7.19.3 构造期值去重的真实落点。"""
    return RuntimeCredentials(llm={"default": tuple(values)}, embedding={})


async def test_duplicate_key_values_collapse_into_one_pool_member():
    """v1.17 §7.19.3 值去重真面：两个环境变量别名**同一**真实密钥 ⇒ 凭据构造期
    去重让池内只剩一把键，首声明者获得 key_env 身份位；流量全部成功。"""
    prof = _pool_profile(["LK_POOL_ITEST_A", "LK_POOL_ITEST_B"])
    client = _client({"default": prof}, {},
                       _pool_creds([_real_key(), _real_key()]))
    pool = client._pool("llm", prof)
    assert pool.size == 1                       # 同值别名坍缩成一把键
    assert pool.states[0].env == "LK_POOL_ITEST_A"
    prompts = [_prompt(f"{n}+{n} 等于几？只回答数字。") for n in (1, 2)]
    try:
        responses = await asyncio.gather(
            *(client.complete("default", p) for p in prompts))
    finally:
        await client.aclose()
    assert len(responses) == 2 and all(r.text.strip() for r in responses)
    usage = client.usage_by_profile["default"]
    assert usage.calls == 2
    assert set(usage.keys) == {"LK_POOL_ITEST_A"}   # 身份位 = 首个声明者
    assert not any(ku.disabled for ku in usage.keys.values())


async def test_bogus_first_key_absorbed_and_rotates(tmp_path):
    """A revoked/invalid key must NOT kill a pool with healthy siblings
    (spec 3.9.3 认证禁用): the real 401 disables the key, the SAME attempt
    re-dispatches on the good key, no retry budget is consumed, and nothing
    feeds the breaker. llm.key_disabled fires exactly once per key (7.2)."""
    import json

    from labelkit.common.config.model import TraceConfig

    prof = _pool_profile(["LK_POOL_ITEST_BAD", "LK_POOL_ITEST_GOOD"],
                         max_concurrency=1)
    creds = _pool_creds([BOGUS_KEY, _real_key()])
    trace_path = tmp_path / "pool.trace.jsonl"
    cfg = obslog_cfg(tmp_path, trace=TraceConfig(
        enabled=True, path=str(trace_path), channels=("llm",)))
    log = EventLog(cfg.trace, "itest")
    sink = MetricsSink(cfg, "itest", log)
    client = _client({"default": prof}, {}, creds, sink)
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
    prof = _pool_profile(["LK_POOL_ITEST_BAD1", "LK_POOL_ITEST_BAD2"],
                         max_concurrency=1)
    creds = _pool_creds([BOGUS_KEY, BOGUS_KEY + "-2"])
    cfg = obslog_cfg(tmp_path)
    sink = MetricsSink(cfg, "itest", EventLog(cfg.trace, "itest"))
    client = _client({"default": prof}, {}, creds, sink)
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


async def test_probe_all_deduped_pool_probes_the_unique_key_once():
    """v1.17 §7.19.3：同值别名坍缩后，probe_all 只探那把唯一键（key_env = 首个
    声明者）；probe() 保持单条结果形态（key_env=None）。"""
    prof = _pool_profile(["LK_POOL_ITEST_P1", "LK_POOL_ITEST_P2"])
    client = _client({"default": prof}, {},
                       _pool_creds([_real_key(), _real_key()]))
    try:
        results = await client.probe_all(("llm", "default"))
        single = await client.probe(("llm", "default"))
    finally:
        await client.aclose()
    assert [r.key_env for r in results] == ["LK_POOL_ITEST_P1"]
    assert all(r.ok for r in results), [r.error for r in results]
    assert single.ok and single.key_env is None

"""Offline unit tests for the v1.6 key pool (spec 3.9.3 密钥池行, 5.1/5.2).

Pure logic only, per the no-mock policy: M1 parsing/validation/normalization of
``api_key_envs`` + ``run.max_park_s``, and M9's ``_KeyPool`` selection /
cooldown / park arithmetic and usage merging. Retry-loop BEHAVIOR (rotation,
absorbed 401, parking against a live endpoint) lives in tests/integration/.
"""
from __future__ import annotations

import pytest

from labelkit.common.config.model import EmbeddingProfile, LLMProfile
from labelkit.common.runtime.llm_client import (
    KeyUsage,
    LLMClient,
    ProbeResult,
    ProfileUsage,
    _key_cooldown_upper,
    _KeyPool,
    _pool_members,
)
from tests.common.test_config import BASE_CONFIG, Env, env, has  # noqa: F401 (fixture)

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
    from tests.common.test_obslog import make_cfg

    cfg = make_cfg(tmp_path)
    sink = MetricsSink(cfg, "t", EventLog(cfg.trace, "t"))
    assert LLMClient({}, {}, sink)._max_park_s() == 3600.0
    cfg0 = dc_replace(cfg, run=dc_replace(cfg.run, max_park_s=0))
    sink0 = MetricsSink(cfg0, "t", EventLog(cfg0.trace, "t"))
    assert LLMClient({}, {}, sink0)._max_park_s() == 0.0
    assert LLMClient({}, {})._max_park_s() == 3600.0

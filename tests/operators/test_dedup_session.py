"""Session delta tests use real indexes and pure vectors, never an inference substitute."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from labelkit.common.config.model import DedupConfig, EmbeddingProfile
from labelkit.common.contracts.sequence_capacity import SessionAttemptScope
from labelkit.common.contracts.types import PipelineItem
from labelkit.common.errors import ContextOverflowError, InternalError, ProviderFatalError, SessionCapacityError
from labelkit.operators.dedup import DedupIndex, DedupStage, _EmbeddingOutcome, _PreparedRecord
from labelkit.operators.dedup_session import _SessionIndex, _SessionStage
from tests.operators.test_dedup import R1, R4, make_ctx, text_record


def context():
    ctx = make_ctx()
    ctx.session_attempt = SessionAttemptScope("session", 1, 1, "dedup")
    return ctx


def stage(scope="global"):
    cfg = DedupConfig(scope=scope)
    return DedupStage(cfg, DedupIndex(cfg, "text"))


@pytest.mark.parametrize("decision", ["commit", "discard"])
async def test_reservation_queries_prefix_and_local_without_mutating_prefix(decision):
    owner = stage()
    owner.index.probe_and_add(text_record(R1, "prefix"))
    old_probe = owner.index._last_probe
    old_similarity = owner.index.last_similarity
    old_generation = owner.index._ordinary_generation
    items = [PipelineItem(text_record(R1, "copy")), PipelineItem(text_record(R4, "fresh")),
             PipelineItem(text_record(R4, "fresh-copy"))]
    reservation = await owner.reserve_session(items, context())
    assert [item.status for item in items] == ["dropped_dup", "active", "dropped_dup"]
    assert items[-1].dedup.kept_id == "fresh"
    assert owner.index._last_probe is old_probe
    assert owner.index.last_similarity == old_similarity
    assert owner.index._ordinary_generation == old_generation
    assert owner._counted_clusters == set()
    assert set(owner.index._digest_by_id) == {"prefix"}
    getattr(owner, f"{decision}_session")(reservation)
    expected = {"prefix", "fresh"} if decision == "commit" else {"prefix"}
    assert set(owner.index._digest_by_id) == expected
    assert len(owner._counted_clusters) == (2 if decision == "commit" else 0)
    with pytest.raises(InternalError, match="consumed"):
        owner.discard_session(reservation)


async def test_final_dedup_admissions_commit_even_when_later_filter_rejects():
    owner = stage()
    item = PipelineItem(text_record(R1, "filtered"))
    reservation = await owner.reserve_session([item], context())
    item.status = "dropped_lowq"
    owner.commit_session(reservation)
    assert owner.index.probe_and_add(text_record(R1, "later")).kept_id == "filtered"


async def test_batch_scope_ignores_previous_session_but_only_resets_on_commit():
    owner = stage("batch")
    owner.index.probe_and_add(text_record(R1, "old"))
    first = PipelineItem(text_record(R1, "new"))
    reservation = await owner.reserve_session([first], context())
    assert first.status == "active"
    assert set(owner.index._digest_by_id) == {"old"}
    owner.discard_session(reservation)
    assert set(owner.index._digest_by_id) == {"old"}
    reservation = await owner.reserve_session([PipelineItem(text_record(R4, "final"))], context())
    owner.commit_session(reservation)
    assert set(owner.index._digest_by_id) == {"final"}


async def test_committed_cluster_is_a_readonly_prefix_for_new_attempts():
    owner = stage()
    owner.index.probe_and_add(text_record(R1, "old"))
    ctx = context()
    reservation = await owner.reserve_session([PipelineItem(text_record(R1, "copy"))], ctx)
    owner.commit_session(reservation)
    assert ctx.metrics.counters["dedup.clusters"] == 1
    ctx2 = context()
    reservation = await owner.reserve_session([PipelineItem(text_record(R1, "copy2"))], ctx2)
    assert "dedup.clusters" not in ctx2.metrics.counters
    owner.discard_session(reservation)


async def test_stale_reservation_rejects_formal_commit():
    owner = stage()
    reservation = await owner.reserve_session([PipelineItem(text_record(R1, "new"))], context())
    owner.index.probe_and_add(text_record(R4, "interloper"))
    with pytest.raises(InternalError, match="stale"):
        owner.commit_session(reservation)


def test_near_and_semantic_queries_choose_highest_score_then_prefix_ties():
    owner = stage()
    owner.index.probe_and_add(text_record(R1, "prefix"))
    local = _SessionIndex(owner)
    local.commit_prepared("local", local.prepare(text_record(R1 + "补充", "local")))
    info = local.probe_prepared(local.prepare(text_record(R1 + "详情", "query")))
    assert info.kind == "near_text"
    assert info.kept_id == "prefix"
    owner.index.add_vector("prefix", "prefix-key", [1, 0])
    local.add_vector("local", "local-key", [1, 0])
    assert local.semantic_probe([1, 0])[:2] == ("prefix", "prefix-key")


def test_semantic_terminal_controls_do_not_become_skipped_embedding():
    cfg = DedupConfig(semantic=True, semantic_embedding="embed")
    owner = DedupStage(cfg, DedupIndex(cfg, "text"))
    local = _SessionStage(owner)
    item = PipelineItem(text_record(R1, "r"), member_positions=(0, 1))
    prepared = _PreparedRecord(0, item, local.index.prepare(item.record), R1)
    overflow = ContextOverflowError("too long", phase="reactive", profile="embed")
    with pytest.raises(SessionCapacityError) as caught:
        local._reduce_one(prepared, _EmbeddingOutcome(None, overflow), context())
    assert caught.value.failures[0].error is overflow
    assert not owner.index._exact and not local.index._exact
    fatal = ProviderFatalError("denied", profile="embed", status_code=401)
    with pytest.raises(ProviderFatalError):
        local._reduce_one(prepared, _EmbeddingOutcome(None, fatal), context())


def test_complete_embedding_preview_does_not_truncate_or_touch_metrics():
    cfg = DedupConfig(semantic=True, semantic_embedding="embed")
    owner = DedupStage(cfg, DedupIndex(cfg, "text"))
    ctx = context()
    ctx.cfg = SimpleNamespace(embedding_profiles={"embed": EmbeddingProfile(
        name="embed", model="model", base_url="http://unused", api_key_env="UNUSED", context_window=128,
    )})
    item = PipelineItem(text_record(R1 * 100, "long"), member_positions=(0, 1))
    failure = owner.preview_capacity(item, ctx)
    assert failure.stage == "dedup" and failure.unit == "sequence"
    assert ctx.metrics.counters == {}
    assert owner.index._last_probe is None
    with pytest.raises(ContextOverflowError):
        owner._embed_input(owner.index.prepare(item.record), ctx)
    assert "budget.truncations.dedup" not in ctx.metrics.counters


def test_prepare_failures_preserve_capacity_control_and_ordinary_failure_product():
    local = _SessionStage(stage())
    item = PipelineItem(text_record(R1, "r"), member_positions=(0,))
    ctx = context()
    with pytest.raises(SessionCapacityError):
        local._fail_item(item, ContextOverflowError("too long", "precheck", "embed"), ctx)
    assert item.status == "active" and not item.errors
    with pytest.raises(ProviderFatalError):
        local._fail_item(item, ProviderFatalError("denied", "embed", 401), ctx)
    local._fail_item(item, ValueError("invalid source evidence"), ctx)
    assert item.status == "failed" and item.errors[0].kind == "internal_error"


async def test_precomputed_vectors_commit_with_admitted_identity_and_empty_commit_keeps_probe():
    owner = stage()
    reservation = await owner.reserve_session([PipelineItem(text_record(R1, "new"))], context())
    detail = reservation.local.index.accepted["new"]
    reservation.local.index.add_vector("new", detail.own_key, [1, 0])
    owner.commit_session(reservation)
    assert owner.index.semantic_probe([1, 0])[:2] == ("new", detail.own_key)
    previous = owner.index._last_probe
    empty = await owner.reserve_session([], context())
    owner.commit_session(empty)
    assert owner.index._last_probe is previous
    assert _SessionIndex(stage("batch")).semantic_probe([1, 0]) is None


def test_embedding_preview_skips_exact_only_and_accepts_complete_small_input():
    cfg = DedupConfig(semantic=True, semantic_embedding="embed")
    owner = DedupStage(cfg, DedupIndex(cfg, "text"))
    ctx = context()
    ctx.cfg = SimpleNamespace(embedding_profiles={"embed": EmbeddingProfile(
        name="embed", model="model", base_url="http://unused", api_key_env="UNUSED", context_window=4096,
    )})
    assert owner.preview_capacity(PipelineItem(text_record(R1, "small")), ctx) is None
    exact = replace(text_record(R1 * 10000, "exact"), exact_dedup_text="proven generation payload")
    assert owner.preview_capacity(PipelineItem(exact), ctx) is None


async def test_embedding_wave_collects_all_capacity_failures_in_declaration_order(monkeypatch):
    cfg = DedupConfig(semantic=True, semantic_embedding="embed")
    owner = DedupStage(cfg, DedupIndex(cfg, "text"))
    local = _SessionStage(owner)
    items = [PipelineItem(text_record(text, str(index)), member_positions=(index,))
             for index, text in enumerate((R1, R4))]
    prepared = [_PreparedRecord(index, item, local.index.prepare(item.record), item.record.text)
                for index, item in enumerate(items)]
    errors = [ContextOverflowError(f"overflow {index}", "reactive", "embed") for index in range(2)]

    async def completed_wave(self, values, ctx):
        return {1: _EmbeddingOutcome(None, errors[1]), 0: _EmbeddingOutcome(None, errors[0])}

    monkeypatch.setattr(DedupStage, "_run_embeddings", completed_wave)
    with pytest.raises(SessionCapacityError) as caught:
        await local._run_embeddings(prepared, context())
    assert [failure.error for failure in caught.value.failures] == errors
    assert [failure.targets[0].member_positions for failure in caught.value.failures] == [(0,), (1,)]
    assert owner.index._exact == local.index._exact == {}
    assert all(item.status == "active" and not item.errors for item in items)


async def test_embedding_fatal_control_dominates_capacity_collection(monkeypatch):
    local = _SessionStage(stage())
    fatal = ProviderFatalError("denied", "embed", 401)

    async def completed_wave(self, values, ctx):
        return {0: _EmbeddingOutcome(None, ContextOverflowError("long", "reactive", "embed")),
                1: _EmbeddingOutcome(None, fatal)}

    monkeypatch.setattr(DedupStage, "_run_embeddings", completed_wave)
    ctx = context()
    with pytest.raises(ProviderFatalError) as caught:
        await local._run_embeddings([], ctx)
    assert caught.value is fatal
    assert ctx.metrics.counters["dedup.embedding_failures"] == 1


@pytest.mark.parametrize("order", [("short", "left", "right"), ("left", "short", "right")])
async def test_preparation_collects_all_real_overflows_before_any_embedding_or_item_commit(order):
    cfg = DedupConfig(semantic=True, semantic_embedding="embed")
    owner = DedupStage(cfg, DedupIndex(cfg, "text"))
    ctx = context()
    ctx.cfg = SimpleNamespace(embedding_profiles={"embed": EmbeddingProfile(
        name="embed", model="model", base_url="http://unused", api_key_env="UNUSED", context_window=512,
    )})
    items = [PipelineItem(text_record(R1 if name == "short" else R1 * 30, name), member_positions=(position,))
             for position, name in enumerate(order)]

    class NoEmbedding:
        async def embed(self, *args, **kwargs):
            raise AssertionError("a preparation failure must prevent every embedding dispatch")

    ctx.llm = NoEmbedding()
    with pytest.raises(SessionCapacityError) as caught:
        await owner.reserve_session(items, ctx)
    failures = caught.value.failures
    assert [failure.targets[0].record_id for failure in failures] == [name for name in order if name != "short"]
    assert [failure.targets[0].member_positions for failure in failures] == [
        (position,) for position, name in enumerate(order) if name != "short"]
    assert all(failure.error.phase == "precheck" and failure.error.profile == "embed" for failure in failures)
    assert ctx.tasks.requests == [] and ctx.metrics.counters == {}
    assert all(item.status == "active" and item.dedup is None and item.errors == [] for item in items)
    assert owner.index._exact == {} and owner.index._last_probe is None and owner._counted_clusters == set()


async def test_preparation_capacity_barrier_precedes_ordinary_failure_products(monkeypatch):
    owner = stage()
    original = _SessionIndex.prepare
    items = [PipelineItem(text_record(R1, name), member_positions=(index,))
             for index, name in enumerate(("invalid", "too-large", "valid"))]
    visited = []

    def prepare(index, record):
        visited.append(record.id)
        if record.id == "invalid":
            raise ValueError("invalid source")
        if record.id == "too-large":
            raise ContextOverflowError("too large", "precheck", "embed")
        return original(index, record)

    monkeypatch.setattr(_SessionIndex, "prepare", prepare)
    ctx = context()
    with pytest.raises(SessionCapacityError):
        await owner.reserve_session(items, ctx)
    assert visited == ["invalid", "too-large", "valid"]
    assert all(item.status == "active" and item.errors == [] for item in items)
    assert ctx.metrics.counters == {} and ctx.tasks.requests == []


async def test_preparation_fatal_has_priority_after_complete_synchronous_plan(monkeypatch):
    owner = stage()
    fatal = ProviderFatalError("denied", "embed", 401)
    errors = [ContextOverflowError("large", "precheck", "embed"), fatal]
    visited = []

    def prepare(index, record):
        visited.append(record.id)
        raise errors[len(visited) - 1]

    monkeypatch.setattr(_SessionIndex, "prepare", prepare)
    items = [PipelineItem(text_record(R1, str(index)), member_positions=(index,)) for index in range(2)]
    with pytest.raises(ProviderFatalError) as caught:
        await owner.reserve_session(items, context())
    assert caught.value is fatal and visited == ["0", "1"]
    assert all(item.status == "active" and not item.errors for item in items)


async def test_ordinary_preparation_error_keeps_existing_failure_and_final_admission(monkeypatch):
    owner = stage()
    original = _SessionIndex.prepare

    def prepare(index, record):
        if record.id == "invalid":
            raise ValueError("invalid source")
        return original(index, record)

    monkeypatch.setattr(_SessionIndex, "prepare", prepare)
    items = [PipelineItem(text_record(R1, "invalid")), PipelineItem(text_record(R4, "valid"))]
    reservation = await owner.reserve_session(items, context())
    assert items[0].status == "failed" and items[0].errors[0].kind == "internal_error"
    assert items[1].status == "active" and items[1].dedup.kind == "unique"
    assert owner.index._exact == {}
    owner.commit_session(reservation)
    assert set(owner.index._digest_by_id) == {"valid"}


async def test_synchronous_preparation_control_escapes_without_dispatch_or_admission(monkeypatch):
    from labelkit.common.errors import CircuitBreakerTripped

    owner = stage()
    error = CircuitBreakerTripped("already open")
    visited = []

    def prepare(index, record):
        visited.append(record.id)
        raise error

    monkeypatch.setattr(_SessionIndex, "prepare", prepare)
    items = [PipelineItem(text_record(R1, name)) for name in ("first", "unreached")]
    ctx = context()
    with pytest.raises(CircuitBreakerTripped) as caught:
        await owner.reserve_session(items, ctx)
    assert caught.value is error and visited == ["first"]
    assert ctx.tasks.requests == [] and owner.index._exact == {}
    assert all(item.status == "active" and not item.errors for item in items)

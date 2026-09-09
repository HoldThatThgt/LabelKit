"""Session delta tests use real indexes and pure vectors, never an inference substitute."""
from dataclasses import replace
import gc
import weakref
from types import SimpleNamespace

import pytest

from labelkit.common.config.model import DedupConfig, EmbeddingProfile
from labelkit.common.contracts.sequence_capacity import SessionAttemptScope
from labelkit.common.contracts.types import PipelineItem
from labelkit.common.errors import ContextOverflowError, InternalError, ProviderFatalError, SessionCapacityError
from labelkit.operators.dedup import DedupIndex, DedupStage, _EmbeddingOutcome, _PreparedRecord
from labelkit.operators.dedup_session import _SessionIndex, _SessionStage
from tests.operators.test_dedup import InlineTaskExecutor, R1, R4, make_ctx, text_record


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
    assert items[0].dedup.kind == items[-1].dedup.kind == "exact"
    assert owner.index._last_probe is old_probe
    assert owner.index.last_similarity == old_similarity
    assert owner.index._ordinary_generation == old_generation
    assert owner._counted_clusters == set()
    assert set(owner.index._digest_by_id) == {"prefix"}
    local_ref = weakref.ref(reservation.local)
    index_ref = weakref.ref(reservation.local.index)
    getattr(owner, f"{decision}_session")(reservation)
    expected = {"prefix", "fresh"} if decision == "commit" else {"prefix"}
    assert set(owner.index._digest_by_id) == expected
    assert len(owner._counted_clusters) == (2 if decision == "commit" else 0)
    gc.collect()
    assert reservation.local is None and local_ref() is None and index_ref() is None
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


def test_near_text_queries_choose_highest_actual_score_across_prefix_and_local_indexes():
    owner = stage()
    prefix = text_record(R1, "prefix")
    owner.index.probe_and_add(prefix)
    local = _SessionIndex(owner)
    local_features = local.prepare(text_record(R1 + "补充", "local"))
    local.commit_prepared("local", local_features)
    query = local.prepare(text_record(R1 + "详情", "query"))
    prefix_score = query.minhash.jaccard(owner.index.prepare(prefix).minhash)
    local_score = query.minhash.jaccard(local_features.minhash)
    assert prefix_score == 0.96875 and local_score == 0.921875
    assert prefix_score > local_score >= owner.cfg.minhash_threshold
    assert owner.index._lsh.query(query.minhash) == ["prefix"]
    assert local._lsh.query(query.minhash) == ["local"]
    info = local.probe_prepared(query)
    assert info.kind == "near_text" and info.kept_id == "prefix"
    assert local.last_similarity == prefix_score


async def test_real_near_text_equal_scores_prefer_committed_prefix_over_local_admission():
    cfg = DedupConfig(ngram=1, minhash_threshold=0.3)
    owner = DedupStage(cfg, DedupIndex(cfg, "text"))
    prefix = text_record("OnorJg", "prefix")
    local = PipelineItem(text_record("kxmHEV", "local"))
    query = PipelineItem(text_record("OnorJgkxmHEV", "query"))
    assert owner.index.probe_and_add(prefix).kind == "unique"
    reservation = await owner.reserve_session([local, query], context())
    assert local.status == "active" and local.dedup.kind == "unique"
    query_features = owner.index.prepare(query.record)
    prefix_features = owner.index.prepare(prefix)
    local_features = owner.index.prepare(local.record)
    assert query_features.minhash.jaccard(prefix_features.minhash) == 0.5
    assert query_features.minhash.jaccard(local_features.minhash) == 0.5
    assert owner.index._lsh.query(query_features.minhash) == ["prefix"]
    assert reservation.local.index._lsh.query(query_features.minhash) == ["local"]
    assert query.status == "dropped_dup" and query.dedup.kind == "near_text"
    assert query.dedup.kept_id == "prefix" and reservation.local.index.last_similarity == 0.5
    assert tuple(owner.index._digest_by_id) == ("prefix",)
    assert tuple(reservation.local.index.accepted) == ("local",)
    owner.discard_session(reservation)


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


class PureVectors:
    """只对测试数据返回预先给定的数学向量，不实现网络或模型协议。"""

    def __init__(self, vectors):
        self.vectors = vectors
        self.calls = []

    async def embed(self, profile, texts):
        self.calls.append((profile, tuple(texts)))
        return [list(self.vectors[text]) for text in texts]


def semantic_session_context(vectors, batch_size=64):
    ctx = context()
    ctx.cfg = SimpleNamespace(
        run=SimpleNamespace(batch_size=batch_size),
        embedding_profiles={"embed": EmbeddingProfile(
            name="embed", model="vector-test", base_url="http://unused", api_key_env="UNUSED", context_window=4096)},
    )
    ctx.llm = PureVectors(vectors)
    return ctx


@pytest.mark.parametrize("query_vector, expected_id, expected_score", [
    ([0.6, 0.8], "local", 0.8), ([0.8, 0.6], "prefix", 0.8),
    ([1, 1], "prefix", 2 ** -0.5),
])
async def test_semantic_session_chooses_highest_real_cosine_and_prefix_on_equal_scores(
        query_vector, expected_id, expected_score):
    cfg = DedupConfig(ngram=1, minhash_threshold=0.3, semantic=True,
                      semantic_embedding="embed", semantic_threshold=0.5)
    owner = DedupStage(cfg, DedupIndex(cfg, "text"))
    vectors = {"aaaaaaa": [1, 0], "bbbbbbb": [0, 1], "ccccccc": query_vector}
    prefix = PipelineItem(text_record("aaaaaaa", "prefix"))
    first = await owner.reserve_session([prefix], semantic_session_context(vectors))
    assert prefix.status == "active" and prefix.dedup.kind == "unique"
    owner.commit_session(first)
    items = [PipelineItem(text_record("bbbbbbb", "local")), PipelineItem(text_record("ccccccc", "query"))]
    ctx = semantic_session_context(vectors)
    reservation = await owner.reserve_session(items, ctx)
    assert items[0].status == "active" and items[0].dedup.kind == "unique"
    assert items[1].status == "dropped_dup" and items[1].dedup.kind == "near_semantic"
    assert items[1].dedup.kept_id == expected_id
    normalized = [value / sum(value ** 2 for value in query_vector) ** 0.5 for value in query_vector]
    prefix_hit = owner.index.semantic_probe(normalized)
    local_hit = DedupIndex.semantic_probe(reservation.local.index, normalized)
    assert prefix_hit[0] == "prefix" and local_hit[0] == "local"
    assert prefix_hit[2] >= cfg.semantic_threshold and local_hit[2] >= cfg.semantic_threshold
    assert reservation.local.index.semantic_probe(normalized)[2] == pytest.approx(expected_score)
    duplicate_events = [event[4] for event in ctx.metrics.events if event[0] == "dedup.duplicate"]
    assert len(duplicate_events) == 1 and duplicate_events[0]["cosine"] == pytest.approx(expected_score)
    assert ctx.llm.calls == [("embed", ("bbbbbbb",)), ("embed", ("ccccccc",))]
    assert tuple(owner.index._digest_by_id) == ("prefix",)
    assert tuple(reservation.local.index.accepted) == ("local",)
    owner.discard_session(reservation)


async def test_committing_real_near_match_updates_formal_probe_and_similarity_once():
    cfg = DedupConfig(ngram=1, minhash_threshold=0.3)
    owner = DedupStage(cfg, DedupIndex(cfg, "text"))
    assert owner.index.probe_and_add(text_record("OnorJg", "prefix")).kind == "unique"
    original_probe = owner.index._last_probe
    assert owner.index.last_similarity is None
    items = [PipelineItem(text_record("kxmHEV", "local")), PipelineItem(text_record("OnorJgkxmHEV", "query"))]
    reservation = await owner.reserve_session(items, context())
    assert items[0].dedup.kind == "unique"
    assert items[1].dedup.kind == "near_text" and items[1].dedup.kept_id == "prefix"
    final_probe = reservation.local.index._last_probe
    assert final_probe.dedup_text == "OnorJgkxmHEV" and final_probe.verdict == items[1].dedup
    assert reservation.local.index.last_similarity == 0.5
    assert owner.index._last_probe is original_probe and owner.index.last_similarity is None
    owner.commit_session(reservation)
    assert owner.index._last_probe is final_probe and owner.index.last_similarity == 0.5
    assert tuple(owner.index._digest_by_id) == ("prefix", "local")
    assert reservation.local is None
    with pytest.raises(InternalError, match="consumed"):
        owner.commit_session(reservation)
    assert owner.index._last_probe is final_probe and owner.index.last_similarity == 0.5


async def test_real_session_embedding_groups_change_without_changing_declaration_order_or_results():
    cfg = DedupConfig(ngram=1, minhash_threshold=0.3, semantic=True,
                      semantic_embedding="embed", semantic_threshold=0.5)
    texts = [character * 7 for character in "abcde"]
    vectors = {text: [float(index == axis) for axis in range(5)] for index, text in enumerate(texts)}
    expected_sizes = {1: [1, 1, 1, 1, 1], 2: [2, 2, 1], 64: [5]}
    observations = []
    for batch_size in (1, 2, 64):
        owner = DedupStage(cfg, DedupIndex(cfg, "text"))
        ctx = semantic_session_context(vectors, batch_size)
        items = []
        for index, text in enumerate(texts):
            member = text_record(text, f"frame-{index}")
            sequence = replace(member, id=f"episode-{index}", kind="sequence", members=(member,))
            items.append(PipelineItem(sequence, session_id="session", member_positions=(index,)))
        reservation = await owner.reserve_session(items, ctx)
        assert isinstance(ctx.tasks, InlineTaskExecutor)
        assert [len(group.tasks) for group in ctx.tasks.requests] == expected_sizes[batch_size]
        tasks = [task for group in ctx.tasks.requests for task in group.tasks]
        assert [task.declaration_key for task in tasks] == [(1, 2, index) for index in range(5)]
        assert [task.task_id for task in tasks] == [f"test:dedup:semantic:{index}" for index in range(5)]
        assert ctx.llm.calls == [("embed", (text,)) for text in texts]
        assert owner.index._exact == {} and owner.index._last_probe is None
        assert all(item.status == "active" and item.dedup.kind == "unique" and not item.errors for item in items)
        assert tuple(reservation.local.index.accepted) == tuple(item.record.id for item in items)
        observations.append([(item.record.id, item.member_positions, item.dedup) for item in items])
        owner.commit_session(reservation)
        assert tuple(owner.index._digest_by_id) == tuple(item.record.id for item in items)
        for item, text in zip(items, texts):
            assert owner.index.semantic_probe(vectors[text])[0] == item.record.id
    assert observations[0] == observations[1] == observations[2]

"""Offline unit tests for M3 dedup (labelkit/operators/dedup.py) — pure logic only, no LLM.

Covers: normalization recipe (verified against the spec 3.3.5 worked example, including its
published sha256 prefixes), exact dedup, MinHash near-text via real datasketch, pHash on
generated PIL images, the ui_dup_requires matrix, batch vs global scope, first-writer-wins
ordering, trace events / counters, and the semantic level's cosine/threshold clustering logic
tested directly on DedupIndex with precomputed vectors.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import random

import pytest
from PIL import Image

from labelkit.common.config.model import DedupConfig
from labelkit.operators.dedup import (
    DedupIndex,
    DedupStage,
    _build_minhash,
    _ProbeDetail,
    _dedup_text,
    _l2_normalize,
    _normalize_text,
    _phash_int,
    _shingles,
)
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import ImageRef, PipelineItem, Record, RecordRef, UINode, UITree

# ── fixtures / helpers ─────────────────────────────────────────────────────

R1 = ("帮我写一条请假条，明天上午要去医院复诊，大概十点到医院，下午两点前能回公司，"
      "请按半天事假写，语气客气一点，落款写研发部小李，谢谢")
R2 = " " + R1 + "　"            # leading half-width space + trailing full-width space
R3 = R1 + " 10:12"
R4 = "把这段会议纪要翻译成英文，术语保留原文"


class FakeMetrics:
    """Minimal MetricsSink stand-in (no LLM involved) recording events and counters."""

    def __init__(self):
        self.events: list[tuple] = []
        self.counters: dict[str, int] = {}

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None):
        self.events.append((ev, stage, batch_no, record_ids, payload or {}))

    def count(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n


def make_ctx(batch_no: int = 1) -> RunContext:
    return RunContext(cfg=None, llm=None, schema_engine=None,
                      metrics=FakeMetrics(), rng=random.Random(0), batch_no=batch_no)


def text_record(text: str, rid: str, line_no: int = 1) -> Record:
    return Record(id=rid, modality="text", text=text, raw={"instruction": text},
                  ui_tree=None, image=None,
                  ref=RecordRef(source_file="in.jsonl", line_no=line_no,
                                pair_index=None, generated_from=()))


def make_tree(labels: list[str], role: str = "text") -> UITree:
    nodes = [UINode(node_id=str(i), parent_id=None, depth=1, role=role, text=lbl,
                    content_desc="", bounds=(0, 32 * i, 400, 32 * i + 30),
                    visible=True, extra={})
             for i, lbl in enumerate(labels)]
    return UITree(nodes=tuple(nodes))


def _noise_image(path, seed: int) -> None:
    """Deterministic low-frequency noise: 16x16 random grayscale upscaled to 128x128.
    Rich DCT content, so pHash is meaningful (uniform gradients degenerate)."""
    rng = random.Random(seed)
    small = Image.new("L", (16, 16))
    small.putdata([rng.randrange(256) for _ in range(256)])
    small.resize((128, 128), Image.Resampling.BICUBIC).save(path, format="PNG")


def screen_a_image(path) -> None:            # identical pixels every call
    _noise_image(path, seed=1)


def screen_b_image(path) -> None:            # visually different content
    _noise_image(path, seed=2)


def ui_record(tmp_path, rid: str, tree: UITree, image_maker, image_name: str) -> Record:
    p = tmp_path / image_name
    image_maker(p)
    ref = ImageRef(path=p, format="png", size_bytes=p.stat().st_size)
    return Record(id=rid, modality="ui", text=None, raw=None, ui_tree=tree, image=ref,
                  ref=RecordRef(source_file=image_name, line_no=None,
                                pair_index=1, generated_from=()))


def run_stage(stage: DedupStage, items: list[PipelineItem], ctx: RunContext):
    return asyncio.run(stage.run(items, ctx))


# ── normalization / hashing (spec 3.3.5 worked example) ────────────────────

def test_normalize_recipe_matches_spec_example():
    assert _normalize_text(R1) == R1                       # already canonical
    assert _normalize_text(R2) == R1                       # spaces stripped incl. U+3000
    assert len(_normalize_text(R1)) == 64
    assert len(_normalize_text(R3)) == 70

    def key(t):
        return hashlib.sha256(_normalize_text(t).encode("utf-8")).hexdigest()[:16]

    assert key(R1) == "57e3f858bc013a54"                   # spec-published prefixes
    assert key(R2) == "57e3f858bc013a54"
    assert key(R3) == "07aaef398c499831"
    assert key(R4) == "e49758a83ce5efec"


def test_normalize_collapses_internal_whitespace_runs():
    assert _normalize_text("a \t\n b　　c") == "a b c"


def test_nfc_normalization_applied():
    decomposed = "étude"                             # e + combining acute
    composed = "étude"
    assert _normalize_text(decomposed) == _normalize_text(composed)


def test_ui_dedup_text_uses_quantized_serialization():
    cfg = DedupConfig(bounds_quantize_px=4)
    tree_a = make_tree(["登录"])
    # Same tree, bounds jittered by < 4px: quantized serialization identical.
    jittered = UITree(nodes=tuple(
        UINode(node_id=n.node_id, parent_id=n.parent_id, depth=n.depth, role=n.role,
               text=n.text, content_desc=n.content_desc,
               bounds=(n.bounds[0] + 1, n.bounds[1] + 1, n.bounds[2] + 1, n.bounds[3] + 1),
               visible=n.visible, extra=n.extra)
        for n in tree_a.nodes))
    rec_a = Record(id="a", modality="ui", text=None, raw=None, ui_tree=tree_a, image=None,
                   ref=RecordRef("x", None, 1, ()))
    rec_b = Record(id="b", modality="ui", text=None, raw=None, ui_tree=jittered, image=None,
                   ref=RecordRef("y", None, 2, ()))
    assert _dedup_text(rec_a, cfg) == _dedup_text(rec_b, cfg)


# ── shingles / minhash ─────────────────────────────────────────────────────

def test_shingles():
    assert _shingles("", 5) == set()
    assert _shingles("abc", 5) == {"abc"}                  # shorter than n → whole text
    assert _shingles("abcdef", 5) == {"abcde", "bcdef"}
    assert len(_shingles(_normalize_text(R1), 5)) == 60    # spec: 64 chars → 60 shingles
    assert len(_shingles(_normalize_text(R3), 5)) == 66


def test_minhash_estimated_jaccard_near_spec_value():
    mh1 = _build_minhash(_normalize_text(R1), 5, 128)
    mh3 = _build_minhash(_normalize_text(R3), 5, 128)
    est = mh1.jaccard(mh3)
    # true J = 60/66 ≈ 0.909; 128-perm estimate σ ≈ 0.025 — allow a generous band
    assert 0.80 <= est <= 1.0
    assert est >= 0.85                                     # must clear the default threshold


def test_build_minhash_empty_text_returns_none():
    assert _build_minhash("", 5, 128) is None


# ── exact + near-text dedup, spec 3.3.5 four-record scenario ───────────────

def spec_batch() -> list[PipelineItem]:
    ids = ["ff21d1c3963d17db", "350d516a359a6174", "6cd63faf65b8cfb7", "08363dbb4cd11f29"]
    texts = [R1, R2, R3, R4]
    return [PipelineItem(record=text_record(t, i, n + 1))
            for n, (t, i) in enumerate(zip(texts, ids))]


def test_spec_scenario_exact_and_near_text():
    cfg = DedupConfig()
    stage = DedupStage(cfg, DedupIndex(cfg, "text"))
    ctx = make_ctx()
    batch = spec_batch()
    out = run_stage(stage, batch, ctx)
    assert out is batch                                    # same list object, no removals
    assert len(out) == 4

    r1, r2, r3, r4 = out
    assert r1.status == "active"
    assert r1.dedup.kind == "unique"
    assert r1.dedup.cluster_key == "57e3f858bc013a54"
    assert r1.dedup.kept_id is None

    assert r2.status == "dropped_dup"
    assert r2.dedup.kind == "exact"
    assert r2.dedup.cluster_key == "57e3f858bc013a54"
    assert r2.dedup.kept_id == "ff21d1c3963d17db"

    assert r3.status == "dropped_dup"
    assert r3.dedup.kind == "near_text"
    assert r3.dedup.cluster_key == "57e3f858bc013a54"
    assert r3.dedup.kept_id == "ff21d1c3963d17db"

    assert r4.status == "active"
    assert r4.dedup.kind == "unique"
    assert r4.dedup.cluster_key == "e49758a83ce5efec"

    m = ctx.metrics
    assert m.counters == {"dedup.exact": 1, "dedup.near_text": 1, "dedup.clusters": 1}
    assert [e[0] for e in m.events] == ["dedup.duplicate", "dedup.duplicate"]
    ev_exact, ev_near = m.events
    assert ev_exact[3] == ("350d516a359a6174",)
    assert ev_exact[4] == {"kind": "exact", "cluster_key": "57e3f858bc013a54",
                           "kept_id": "ff21d1c3963d17db"}   # exact carries no metric
    assert ev_near[4]["kind"] == "near_text"
    assert "jaccard" in ev_near[4] and "hamming" not in ev_near[4] and "cosine" not in ev_near[4]
    assert ev_near[4]["jaccard"] >= 0.85


def test_dissimilar_texts_stay_unique():
    cfg = DedupConfig()
    stage = DedupStage(cfg, DedupIndex(cfg, "text"))
    ctx = make_ctx()
    items = [PipelineItem(record=text_record(R1, "a")),
             PipelineItem(record=text_record(R4, "b"))]
    run_stage(stage, items, ctx)
    assert all(i.status == "active" and i.dedup.kind == "unique" for i in items)
    assert ctx.metrics.counters == {}
    assert ctx.metrics.events == []


def test_first_writer_wins_ordering():
    cfg = DedupConfig()
    stage = DedupStage(cfg, DedupIndex(cfg, "text"))
    ctx = make_ctx()
    items = [PipelineItem(record=text_record(R1, f"id{i}", i + 1)) for i in range(3)]
    run_stage(stage, items, ctx)
    assert items[0].status == "active"
    assert items[1].status == items[2].status == "dropped_dup"
    assert items[1].dedup.kept_id == "id0"
    assert items[2].dedup.kept_id == "id0"                 # head, not the previous dup
    assert ctx.metrics.counters["dedup.exact"] == 2
    assert ctx.metrics.counters["dedup.clusters"] == 1     # one cluster only


def test_non_active_items_are_skipped():
    cfg = DedupConfig()
    stage = DedupStage(cfg, DedupIndex(cfg, "text"))
    ctx = make_ctx()
    first = PipelineItem(record=text_record(R1, "a"))
    first.status = "failed"
    second = PipelineItem(record=text_record(R1, "b"))
    run_stage(stage, [first, second], ctx)
    assert first.dedup is None                             # untouched
    assert second.status == "active"                       # 'a' never entered the index
    assert second.dedup.kind == "unique"


# ── scope: global vs batch ─────────────────────────────────────────────────

def test_global_scope_dedups_across_batches():
    cfg = DedupConfig(scope="global")
    stage = DedupStage(cfg, DedupIndex(cfg, "text"))
    b1 = [PipelineItem(record=text_record(R1, "a"))]
    b2 = [PipelineItem(record=text_record(R1, "b"))]
    run_stage(stage, b1, make_ctx(1))
    run_stage(stage, b2, make_ctx(2))
    assert b1[0].status == "active"
    assert b2[0].status == "dropped_dup"
    assert b2[0].dedup.kind == "exact"
    assert b2[0].dedup.kept_id == "a"


def test_batch_scope_resets_index_between_batches():
    cfg = DedupConfig(scope="batch")
    stage = DedupStage(cfg, DedupIndex(cfg, "text"))
    b1 = [PipelineItem(record=text_record(R1, "a")),
          PipelineItem(record=text_record(R1, "a2"))]
    b2 = [PipelineItem(record=text_record(R1, "b"))]
    run_stage(stage, b1, make_ctx(1))
    run_stage(stage, b2, make_ctx(2))
    assert b1[1].status == "dropped_dup"                   # in-batch dup still caught
    assert b2[0].status == "active"                        # cross-batch: index was reset
    assert b2[0].dedup.kind == "unique"


# ── pHash on generated PIL images ──────────────────────────────────────────

def test_phash_identical_vs_different_images(tmp_path):
    p1, p2, p3 = tmp_path / "a.png", tmp_path / "b.png", tmp_path / "c.png"
    screen_a_image(p1)
    screen_a_image(p2)                                     # visually identical
    screen_b_image(p3)                                      # visually different
    h1, h2, h3 = _phash_int(p1), _phash_int(p2), _phash_int(p3)
    assert (h1 ^ h2).bit_count() <= 8
    assert (h1 ^ h3).bit_count() > 8
    assert 0 <= h1 < (1 << 64)


# ── UI composite verdict matrix (ui_dup_requires) ──────────────────────────

LABELS_A = [f"item {i:03d}" for i in range(30)]
LABELS_NEAR = LABELS_A[:-1] + ["item 02X"]                 # one label differs → near, not exact
LABELS_FAR = [f"完全不同的控件文本 {i}" for i in range(30)]


def ui_pair(tmp_path, tree_labels, image_maker, requires: str):
    """First record indexed as head; second probed. Returns the second item + ctx."""
    cfg = DedupConfig(ui_dup_requires=requires)
    stage = DedupStage(cfg, DedupIndex(cfg, "ui"))
    ctx = make_ctx()
    head = PipelineItem(record=ui_record(tmp_path, "head", make_tree(LABELS_A),
                                         screen_a_image, "head.png"))
    probe = PipelineItem(record=ui_record(tmp_path, "probe", make_tree(tree_labels),
                                          image_maker, "probe.png"))
    run_stage(stage, [head, probe], ctx)
    assert head.status == "active"
    return probe, ctx


def test_ui_requires_both_tree_and_image_hit(tmp_path):
    probe, ctx = ui_pair(tmp_path, LABELS_NEAR, screen_a_image, "both")
    assert probe.status == "dropped_dup"
    assert probe.dedup.kind == "near_both"
    assert probe.dedup.kept_id == "head"
    payload = ctx.metrics.events[0][4]
    assert "jaccard" in payload and "hamming" not in payload
    assert ctx.metrics.counters == {"dedup.near_both": 1, "dedup.clusters": 1}


def test_ui_requires_both_tree_only_not_dup(tmp_path):
    # Spec 3.3.5 UI example: tree near-hit + image miss under "both" → unique.
    probe, ctx = ui_pair(tmp_path, LABELS_NEAR, screen_b_image, "both")
    assert probe.status == "active"
    assert probe.dedup.kind == "unique"
    assert probe.dedup.kept_id is None
    assert ctx.metrics.events == []


def test_ui_requires_both_image_only_not_dup(tmp_path):
    probe, _ = ui_pair(tmp_path, LABELS_FAR, screen_a_image, "both")
    assert probe.status == "active"
    assert probe.dedup.kind == "unique"


def test_ui_requires_tree_tree_only_is_dup(tmp_path):
    # Spec 3.3.5 设计意图: requires="tree" → the same probe is dropped as near_text.
    probe, ctx = ui_pair(tmp_path, LABELS_NEAR, screen_b_image, "tree")
    assert probe.status == "dropped_dup"
    assert probe.dedup.kind == "near_text"
    assert "jaccard" in ctx.metrics.events[0][4]


def test_ui_requires_tree_image_only_not_dup(tmp_path):
    probe, _ = ui_pair(tmp_path, LABELS_FAR, screen_a_image, "tree")
    assert probe.status == "active"


def test_ui_requires_image_image_only_is_dup(tmp_path):
    probe, ctx = ui_pair(tmp_path, LABELS_FAR, screen_a_image, "image")
    assert probe.status == "dropped_dup"
    assert probe.dedup.kind == "near_image"
    payload = ctx.metrics.events[0][4]
    assert "hamming" in payload and payload["hamming"] <= 8
    assert "jaccard" not in payload


def test_ui_requires_image_tree_only_not_dup(tmp_path):
    probe, _ = ui_pair(tmp_path, LABELS_NEAR, screen_b_image, "image")
    assert probe.status == "active"


def test_ui_exact_hit_wins_unconditionally(tmp_path):
    # Identical trees but different images: level ① fires regardless of "both".
    probe, ctx = ui_pair(tmp_path, LABELS_A, screen_b_image, "both")
    assert probe.status == "dropped_dup"
    assert probe.dedup.kind == "exact"
    assert probe.dedup.kept_id == "head"
    assert "hamming" not in ctx.metrics.events[0][4]


def broken_image_item(tmp_path, tree_labels) -> PipelineItem:
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"not an image at all")
    bad_ref = ImageRef(path=bad, format="png", size_bytes=bad.stat().st_size)
    return PipelineItem(record=Record(
        id="probe", modality="ui", text=None, raw=None, ui_tree=make_tree(tree_labels),
        image=bad_ref, ref=RecordRef("broken.png", None, 2, ())))


@pytest.mark.parametrize("requires", ["tree", "both", "image"])
def test_ui_image_decode_failure_falls_back_to_tree(tmp_path, requires):
    # Spec 3.3.4 / CONTRACTS.md §7.2: decode failure ⇒ the record skips the pHash layer
    # and is judged by the tree alone — under EVERY ui_dup_requires setting ("both" and
    # "image" degrade to "tree" for that record).
    cfg = DedupConfig(ui_dup_requires=requires)
    stage = DedupStage(cfg, DedupIndex(cfg, "ui"))
    ctx = make_ctx()
    head = PipelineItem(record=ui_record(tmp_path, "head", make_tree(LABELS_A),
                                         screen_a_image, "ok.png"))
    probe = broken_image_item(tmp_path, LABELS_NEAR)
    run_stage(stage, [head, probe], ctx)
    # tree-only verdict: still a near_text dup; record got no StageError
    assert probe.status == "dropped_dup"
    assert probe.dedup.kind == "near_text"
    assert probe.dedup.kept_id == "head"
    assert probe.errors == []
    assert ctx.metrics.counters["dedup.image_decode_failures"] == 1
    payload = ctx.metrics.events[0][4]
    assert "jaccard" in payload and "hamming" not in payload


@pytest.mark.parametrize("requires", ["tree", "both", "image"])
def test_ui_image_decode_failure_tree_miss_stays_unique(tmp_path, requires):
    # Degradation must not over-drop: decode failure + no tree hit → unique and active.
    cfg = DedupConfig(ui_dup_requires=requires)
    stage = DedupStage(cfg, DedupIndex(cfg, "ui"))
    ctx = make_ctx()
    head = PipelineItem(record=ui_record(tmp_path, "head", make_tree(LABELS_A),
                                         screen_a_image, "ok.png"))
    probe = broken_image_item(tmp_path, LABELS_FAR)
    run_stage(stage, [head, probe], ctx)
    assert probe.status == "active"
    assert probe.dedup.kind == "unique"
    assert probe.errors == []
    assert ctx.metrics.counters["dedup.image_decode_failures"] == 1
    assert ctx.metrics.events == []


# ── per-record failure isolation ───────────────────────────────────────────

def test_record_failure_isolated_to_item(tmp_path):
    cfg = DedupConfig()
    stage = DedupStage(cfg, DedupIndex(cfg, "text"))
    ctx = make_ctx()

    class ExplodingStr(str):
        def split(self, *a, **k):
            raise RuntimeError("boom")

    bad = PipelineItem(record=text_record("x", "bad"))
    object.__setattr__(bad.record, "text", ExplodingStr("x"))
    good = PipelineItem(record=text_record(R4, "good"))
    out = run_stage(stage, [bad, good], ctx)
    assert out is not None
    assert bad.status == "failed"
    assert bad.errors[0].stage == "dedup"
    assert bad.errors[0].kind == "internal_error"
    assert good.status == "active"                         # failure did not escape
    errs = [e for e in ctx.metrics.events if e[0] == "error"]
    assert len(errs) == 1 and errs[0][4]["kind"] == "internal_error"


# ── semantic level ④: cosine/threshold clustering on precomputed vectors ───

def unit(*components: float) -> list[float]:
    return list(_l2_normalize(list(components)))


def test_l2_normalize():
    v = _l2_normalize([3.0, 4.0])
    assert math.isclose(float((v * v).sum()), 1.0, rel_tol=1e-12)
    assert list(_l2_normalize([0.0, 0.0])) == [0.0, 0.0]   # zero vector left as-is


def semantic_index() -> DedupIndex:
    cfg = DedupConfig(semantic=True, semantic_embedding="emb", semantic_threshold=0.95)
    return DedupIndex(cfg, "text")


def test_semantic_probe_empty_index():
    assert semantic_index().semantic_probe(unit(1, 0, 0)) is None


def test_semantic_probe_threshold_boundary():
    idx = semantic_index()
    idx.add_vector("a", "keyA", unit(1, 0, 0))
    # cosine exactly at threshold counts as >= (dup)
    at = unit(0.95, math.sqrt(1 - 0.95 ** 2), 0)
    hit = idx.semantic_probe(at)
    assert hit is not None
    kept_id, cluster_key, cosine = hit
    assert kept_id == "a" and cluster_key == "keyA"
    assert math.isclose(cosine, 0.95, abs_tol=1e-9)
    # just below threshold → no hit
    below = unit(0.94, math.sqrt(1 - 0.94 ** 2), 0)
    assert idx.semantic_probe(below) is None
    # orthogonal → no hit
    assert idx.semantic_probe(unit(0, 0, 1)) is None
    # identical → cosine 1.0
    hit2 = idx.semantic_probe(unit(1, 0, 0))
    assert hit2 is not None and math.isclose(hit2[2], 1.0, abs_tol=1e-12)


def test_semantic_probe_returns_best_match():
    idx = semantic_index()
    idx.add_vector("a", "keyA", unit(1, 0, 0))
    idx.add_vector("b", "keyB", unit(0, 1, 0))
    near_b = unit(0.1, 0.99, 0)
    hit = idx.semantic_probe(near_b)
    assert hit is not None and hit[0] == "b" and hit[1] == "keyB"


def test_semantic_probe_tie_prefers_first_writer():
    idx = semantic_index()
    idx.add_vector("first", "k1", unit(1, 0))
    idx.add_vector("second", "k2", unit(1, 0))
    hit = idx.semantic_probe(unit(1, 0))
    assert hit is not None and hit[0] == "first"


def test_add_vector_growth_beyond_initial_capacity():
    idx = semantic_index()
    for i in range(40):                                    # exceeds the initial 16 rows
        angle = i * 0.07
        idx.add_vector(f"id{i}", f"k{i}", unit(math.cos(angle), math.sin(angle)))
    hit = idx.semantic_probe(unit(math.cos(39 * 0.07), math.sin(39 * 0.07)))
    assert hit is not None and hit[0] == "id39"


def test_semantic_reset_clears_vectors():
    idx = semantic_index()
    idx.add_vector("a", "keyA", unit(1, 0))
    idx.reset()
    assert idx.semantic_probe(unit(1, 0)) is None


def ui_semantic_stage(requires: str) -> DedupStage:
    cfg = DedupConfig(semantic=True, semantic_embedding="emb",
                      ui_dup_requires=requires)
    return DedupStage(cfg, DedupIndex(cfg, "ui"))


def probe_detail(*, decode_failed: bool = False, image_hit=None,
                 is_sequence: bool = False) -> "_ProbeDetail":
    return _ProbeDetail(dedup_text="t", digest=b"\x00" * 32, own_key="0" * 16,
                        image_decode_failed=decode_failed, image_hit=image_hit,
                        is_sequence=is_sequence)


def test_semantic_verdict_kind_ui_matrix():
    img_hit = ("head", "k", 3)
    # "both": ④ is a tree-level hit; the image level must also hit.
    both = ui_semantic_stage("both")
    assert both._semantic_verdict_kind(probe_detail()) is None
    assert both._semantic_verdict_kind(probe_detail(image_hit=img_hit)) == "near_both"
    # decode failure degrades "both" to tree-only: ④ alone suffices (spec 3.3.4).
    assert both._semantic_verdict_kind(probe_detail(decode_failed=True)) == "near_semantic"
    # "tree": ④ alone suffices; ④+③ together still records near_both.
    tree = ui_semantic_stage("tree")
    assert tree._semantic_verdict_kind(probe_detail()) == "near_semantic"
    assert tree._semantic_verdict_kind(probe_detail(image_hit=img_hit)) == "near_both"
    # "image" degraded to tree-only on decode failure: ④ hit is a duplicate.
    image = ui_semantic_stage("image")
    assert image._semantic_verdict_kind(probe_detail(decode_failed=True)) == "near_semantic"
    # text modality: always near_semantic.
    cfg = DedupConfig(semantic=True, semantic_embedding="emb")
    text_stage = DedupStage(cfg, DedupIndex(cfg, "text"))
    assert text_stage._semantic_verdict_kind(probe_detail()) == "near_semantic"


def test_semantic_participates_matrix():
    # Under "image", ④ takes no part in the verdict → no embedding spent (spec 3.3.3) —
    # except when the record's image failed to decode and it is judged by the tree alone.
    image = ui_semantic_stage("image")
    assert image._semantic_participates(probe_detail()) is False
    assert image._semantic_participates(probe_detail(decode_failed=True)) is True
    assert ui_semantic_stage("both")._semantic_participates(probe_detail()) is True
    assert ui_semantic_stage("tree")._semantic_participates(probe_detail()) is True
    cfg = DedupConfig(semantic=True, semantic_embedding="emb")
    text_stage = DedupStage(cfg, DedupIndex(cfg, "text"))
    assert text_stage._semantic_participates(probe_detail()) is True


def test_retract_removes_all_probe_entries():
    cfg = DedupConfig()
    idx = DedupIndex(cfg, "text")
    rec = text_record(R1, "a")
    info = idx.probe_and_add(rec)
    assert info.kind == "unique"
    idx._retract("a")
    # Both exact and near-text probes see an empty index again.
    again = idx.probe_and_add(text_record(R1, "b"))
    assert again.kind == "unique"
    near = idx.probe_and_add(text_record(R3, "c"))
    assert near.kind == "near_text" and near.kept_id == "b"


# ── DedupIndex API details ─────────────────────────────────────────────────

def test_last_similarity_metric_by_kind():
    cfg = DedupConfig()
    idx = DedupIndex(cfg, "text")
    idx.probe_and_add(text_record(R1, "a"))
    idx.probe_and_add(text_record(R1, "a-dup"))
    assert idx.last_similarity is None                     # exact → None
    idx.probe_and_add(text_record(R3, "b"))
    assert idx.last_similarity is not None
    assert idx.last_similarity >= 0.85                     # near_text → estimated Jaccard


def test_probe_does_not_index_duplicates():
    cfg = DedupConfig()
    idx = DedupIndex(cfg, "text")
    idx.probe_and_add(text_record(R1, "head"))
    idx.probe_and_add(text_record(R3, "neardup"))          # judged near_text, NOT indexed
    # A text near-identical to the dup but further from the head must be compared
    # against the head only (first-writer-wins keeps only the head in the index).
    info = idx.probe_and_add(text_record(R3, "again"))
    assert info.kept_id == "head"


# ── v1.8 sequence records (S10, spec 3.3.3 sequence row) ────────────────────

def seq_record(rid: str, members: list[Record]) -> Record:
    """S24 sequence-record convention: text/raw/ui_tree/image = None, modality =
    the members' modality, ref inherited from the first member."""
    first = members[0]
    return Record(id=rid, modality=first.modality, text=None, raw=None, ui_tree=None,
                  image=None,
                  ref=RecordRef(source_file=first.ref.source_file,
                                line_no=first.ref.line_no,
                                pair_index=first.ref.pair_index, generated_from=()),
                  kind="sequence", members=tuple(members))


def ui_frame(rid: str, tree: UITree, pair_index: int, *, image=None) -> Record:
    return Record(id=rid, modality="ui", text=None, raw=None, ui_tree=tree,
                  image=image,
                  ref=RecordRef(f"{rid}.jsonl", None, pair_index, ()))


def test_sequence_dedup_text_joins_member_recipes_with_rs():
    cfg = DedupConfig()
    # Member lines recurse into the single-record recipe: R2 normalizes to R1.
    seq = seq_record("epA", [text_record(R2, "m1"), text_record(R4, "m2")])
    joined = _dedup_text(seq, cfg)
    assert joined == _normalize_text(R1) + "\x1e" + _normalize_text(R4)
    # Separator invariants (spec 3.3.3 sequence row): ASCII RS is whitespace to
    # Python, so the whitespace-collapsed single-record recipe can never emit it.
    assert "\x1e".isspace() is True
    assert "\x1e" not in _dedup_text(text_record(R1, "x"), cfg)
    # Deterministic: same member content (different ids) → identical joined text.
    again = seq_record("epB", [text_record(R2, "m3"), text_record(R4, "m4")])
    assert _dedup_text(again, cfg) == joined


def test_sequence_dedup_text_ui_members_use_quantized_serialization():
    cfg = DedupConfig(bounds_quantize_px=4)
    m1 = ui_frame("f1", make_tree(["登录"]), 1)
    m2 = ui_frame("f2", make_tree(["首页"]), 2)
    seq = seq_record("ep", [m1, m2])
    assert _dedup_text(seq, cfg) == (m1.ui_tree.serialize(quantize_px=4) + "\x1e"
                                     + m2.ui_tree.serialize(quantize_px=4))


def test_sequence_episodes_same_members_exact_dup():
    cfg = DedupConfig()
    stage = DedupStage(cfg, DedupIndex(cfg, "text"))
    ctx = make_ctx()
    a = PipelineItem(record=seq_record("epA", [text_record(R1, "m1"),
                                               text_record(R4, "m2")]))
    b = PipelineItem(record=seq_record("epB", [text_record(R1, "m3"),
                                               text_record(R4, "m4")]))
    run_stage(stage, [a, b], ctx)
    assert a.status == "active" and a.dedup.kind == "unique"
    assert b.status == "dropped_dup"
    assert b.dedup.kind == "exact"
    assert b.dedup.kept_id == "epA"
    assert ctx.metrics.counters == {"dedup.exact": 1, "dedup.clusters": 1}


def test_sequence_episodes_different_members_stay_unique():
    cfg = DedupConfig()
    stage = DedupStage(cfg, DedupIndex(cfg, "text"))
    ctx = make_ctx()
    a = PipelineItem(record=seq_record("epA", [text_record(R1, "m1"),
                                               text_record(R4, "m2")]))
    b = PipelineItem(record=seq_record("epB", [
        text_record("帮我把下周的项目评审会改到周三下午三点", "m3"),
        text_record("查一下从公司到虹桥机场的打车预估价", "m4")]))
    run_stage(stage, [a, b], ctx)
    assert a.status == "active" and a.dedup.kind == "unique"
    assert b.status == "active" and b.dedup.kind == "unique"
    assert ctx.metrics.events == [] and ctx.metrics.counters == {}


def test_sequence_ui_requires_both_degrades_to_tree():
    # Sequence records carry image=None ⇒ the image level can never fire; under
    # "both" the composite verdict degrades to tree semantics (spec 3.3.3 sequence
    # row, image_decode_failed-isomorphic): a tree near-hit ALONE drops the episode.
    cfg = DedupConfig(ui_dup_requires="both")
    stage = DedupStage(cfg, DedupIndex(cfg, "ui"))
    ctx = make_ctx()
    head = PipelineItem(record=seq_record("epA", [
        ui_frame("f1", make_tree(LABELS_A), 1),
        ui_frame("f2", make_tree(LABELS_FAR), 2)]))
    probe = PipelineItem(record=seq_record("epB", [
        ui_frame("f3", make_tree(LABELS_NEAR), 3),
        ui_frame("f4", make_tree(LABELS_FAR), 4)]))
    run_stage(stage, [head, probe], ctx)
    assert head.status == "active" and head.dedup.kind == "unique"
    assert probe.status == "dropped_dup"
    assert probe.dedup.kind == "near_text"                 # tree-only hit, no image level
    assert probe.dedup.kept_id == "epA"
    payload = ctx.metrics.events[0][4]
    assert "jaccard" in payload and "hamming" not in payload
    assert ctx.metrics.counters == {"dedup.near_text": 1, "dedup.clusters": 1}


def test_sequence_ui_requires_both_tree_miss_stays_unique():
    # Degradation must not over-drop: no tree hit → the episode stays unique.
    cfg = DedupConfig(ui_dup_requires="both")
    stage = DedupStage(cfg, DedupIndex(cfg, "ui"))
    ctx = make_ctx()
    head = PipelineItem(record=seq_record("epA", [ui_frame("f1", make_tree(LABELS_A), 1)]))
    probe = PipelineItem(record=seq_record("epB", [ui_frame("f2", make_tree(LABELS_FAR), 2)]))
    run_stage(stage, [head, probe], ctx)
    assert probe.status == "active" and probe.dedup.kind == "unique"
    assert ctx.metrics.events == []


def test_sequence_phash_never_called(monkeypatch):
    # Level ③ auto-skips sequences through the existing `rec.image is not None` gate —
    # even when the member frames themselves carry images — and the skip must not
    # mis-set the image_decode_failed path (no image_decode_failures counted).
    import labelkit.operators.dedup as dedup_module
    from pathlib import Path

    calls: list = []
    monkeypatch.setattr(dedup_module, "_phash_int",
                        lambda path: calls.append(path) or 0)
    cfg = DedupConfig(ui_dup_requires="both")
    stage = DedupStage(cfg, DedupIndex(cfg, "ui"))
    ctx = make_ctx()
    fake_img = ImageRef(path=Path("never_opened.png"), format="png", size_bytes=1)
    a = PipelineItem(record=seq_record("epA", [
        ui_frame("f1", make_tree(LABELS_A), 1, image=fake_img)]))
    b = PipelineItem(record=seq_record("epB", [
        ui_frame("f2", make_tree(LABELS_A), 2, image=fake_img)]))
    run_stage(stage, [a, b], ctx)
    assert calls == []                                     # pHash never computed
    assert a.status == "active"
    assert b.status == "dropped_dup" and b.dedup.kind == "exact"   # judged without ③
    assert "dedup.image_decode_failures" not in ctx.metrics.counters
    assert a.record.members[0].image is fake_img           # member images untouched


def test_semantic_participates_sequence_cases():
    # Under "image" a sequence still participates in ④: its dedup face IS the
    # concatenated member text (S10) — unlike a plain record, which sits ④ out.
    image = ui_semantic_stage("image")
    assert image._semantic_participates(probe_detail(is_sequence=True)) is True
    assert image._semantic_participates(probe_detail()) is False   # non-sequence control
    assert ui_semantic_stage("both")._semantic_participates(
        probe_detail(is_sequence=True)) is True
    assert ui_semantic_stage("tree")._semantic_participates(
        probe_detail(is_sequence=True)) is True
    cfg = DedupConfig(semantic=True, semantic_embedding="emb")
    text_stage = DedupStage(cfg, DedupIndex(cfg, "text"))
    assert text_stage._semantic_participates(probe_detail(is_sequence=True)) is True


def test_semantic_verdict_kind_sequence_cases():
    # "both" walks the tree-only branch for sequences: the always-absent image hit
    # must not block a near_semantic verdict (S10).
    both = ui_semantic_stage("both")
    assert both._semantic_verdict_kind(probe_detail(is_sequence=True)) == "near_semantic"
    assert both._semantic_verdict_kind(probe_detail()) is None     # non-sequence control
    tree = ui_semantic_stage("tree")
    assert tree._semantic_verdict_kind(probe_detail(is_sequence=True)) == "near_semantic"
    image = ui_semantic_stage("image")
    assert image._semantic_verdict_kind(probe_detail(is_sequence=True)) == "near_semantic"
    # Text-member episodes ride the text-modality branch: plain near_semantic.
    cfg = DedupConfig(semantic=True, semantic_embedding="emb")
    text_stage = DedupStage(cfg, DedupIndex(cfg, "text"))
    assert text_stage._semantic_verdict_kind(probe_detail(is_sequence=True)) == "near_semantic"


# ── v1.11 embed-input budget truncation (V15, spec 3.3.3 嵌入输入预算截断) ──

def _emb_profile(context_window: int) -> "EmbeddingProfile":
    from labelkit.common.config.model import EmbeddingProfile
    return EmbeddingProfile(name="emb", base_url="http://x", model="m",
                            api_key_env="K", context_window=context_window)


class _EmbedRecorder:
    """Minimal ctx.llm stand-in: records embed inputs, returns a fixed unit vector."""

    def __init__(self, exc: Exception | None = None):
        self.texts: list[str] = []
        self.exc = exc

    async def embed(self, profile, texts):
        if self.exc is not None:
            raise self.exc
        self.texts.extend(texts)
        return [[1.0, 0.0]]


def _semantic_ctx(context_window: int, llm: _EmbedRecorder) -> RunContext:
    from types import SimpleNamespace
    cfg = SimpleNamespace(embedding_profiles={"emb": _emb_profile(context_window)})
    return RunContext(cfg=cfg, llm=llm, schema_engine=None, metrics=FakeMetrics(),
                      rng=random.Random(0), batch_no=1)


def _long_text_item() -> PipelineItem:
    # 40 lines × 60 CJK chars ≈ 2400 tokens under est_text — far over a 512 window.
    text = "\n".join("行" + str(i) + "内" * 60 for i in range(40))
    return PipelineItem(record=text_record(text, "1" * 16))


def test_semantic_embed_input_head_truncated_under_declared_window():
    from labelkit.common.runtime import budget as budget_mod

    cfg = DedupConfig(semantic=True, semantic_embedding="emb")
    llm = _EmbedRecorder()
    ctx = _semantic_ctx(512, llm)
    item = _long_text_item()
    run_stage(DedupStage(cfg, DedupIndex(cfg, "text")), [item], ctx)
    assert item.status == "active"                         # unique, vector indexed
    (sent,) = llm.texts
    full = _dedup_text(item.record, cfg)
    expected = budget_mod.fit_text(full, budget_mod.embed_budget(_emb_profile(512)),
                                   keep="head")
    assert sent == expected                                # deterministic head keep
    assert sent != full and full.startswith(sent)          # strict prefix (line-bounded)
    assert budget_mod.est_text(sent) <= budget_mod.embed_budget(_emb_profile(512))
    assert ctx.metrics.counters["budget.truncations.dedup"] == 1

    # Determinism: an identical re-run sends the identical truncated text.
    llm2 = _EmbedRecorder()
    ctx2 = _semantic_ctx(512, llm2)
    run_stage(DedupStage(cfg, DedupIndex(cfg, "text")), [_long_text_item()], ctx2)
    assert llm2.texts == [sent]


def test_semantic_embed_input_untouched_when_window_undeclared():
    # cw == 0 → embed budget OFF: the v1.10 full-text call is byte-identical and
    # no truncation counter appears (the §1 byte-equivalence anchor).
    cfg = DedupConfig(semantic=True, semantic_embedding="emb")
    llm = _EmbedRecorder()
    ctx = _semantic_ctx(0, llm)
    item = _long_text_item()
    run_stage(DedupStage(cfg, DedupIndex(cfg, "text")), [item], ctx)
    assert llm.texts == [_dedup_text(item.record, cfg)]
    assert "budget.truncations.dedup" not in ctx.metrics.counters


def test_embedding_failure_skip_path_unchanged_under_budget():
    # V15: the existing embedding_failures skip path stays the fallback — a
    # provider failure after truncation still resolves the record on ①–③.
    from labelkit.common.errors import ProviderRetryableError

    cfg = DedupConfig(semantic=True, semantic_embedding="emb")
    llm = _EmbedRecorder(exc=ProviderRetryableError("boom", "emb", 5))
    ctx = _semantic_ctx(512, llm)
    item = _long_text_item()
    run_stage(DedupStage(cfg, DedupIndex(cfg, "text")), [item], ctx)
    assert item.status == "active" and item.dedup.kind == "unique"
    assert ctx.metrics.counters["dedup.embedding_failures"] == 1


def test_context_overflow_from_embed_classified_precisely():
    # V27①: a ContextOverflowError escaping the embed path lands in the stage's
    # per-record classifier FIRST — kind context_overflow (never internal_error),
    # record-level failed → rejects, budget.overflow_records counted, and the
    # precheck phase never feeds the breaker.
    from labelkit.common.errors import ContextOverflowError

    cfg = DedupConfig(semantic=True, semantic_embedding="emb")
    llm = _EmbedRecorder(exc=ContextOverflowError("over", phase="precheck"))
    ctx = _semantic_ctx(512, llm)
    item = _long_text_item()
    run_stage(DedupStage(cfg, DedupIndex(cfg, "text")), [item], ctx)
    assert item.status == "failed"
    assert item.errors[0].kind == "context_overflow"
    assert ctx.metrics.counters["budget.overflow_records"] == 1
    ev = [e for e in ctx.metrics.events if e[0] == "error"]
    assert ev and ev[0][4]["kind"] == "context_overflow"

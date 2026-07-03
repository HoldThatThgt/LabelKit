"""M3 dedup (spec 3.3): exact SHA-256 → MinHash-LSH near-text → pHash near-image (UI)
→ optional semantic (embedding cosine, v1.2). First-writer-wins; duplicates are status-flagged,
never removed. Default configuration calls no LLM/embedding API."""
from __future__ import annotations

import asyncio
import hashlib
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Sequence

import imagehash
import numpy as np
from datasketch import MinHash, MinHashLSH
from PIL import Image

from labelkit.errors import (
    CircuitBreakerTripped,
    ErrorKind,
    ProviderFatalError,
    ProviderRetryableError,
)
from labelkit.types import DedupInfo, PipelineItem, Record, StageError

if TYPE_CHECKING:
    from labelkit.config.model import DedupConfig
    from labelkit.stage import RunContext

# Event names (defined canonically in labelkit.obslog; literals used here so this module
# never imports obslog — tests assert the exact strings, CONTRACTS.md §7.11/§8.1).
_EV_DEDUP_DUPLICATE = "dedup.duplicate"
_EV_ERROR = "error"


# ── pure helpers ───────────────────────────────────────────────────────────


def _normalize_text(text: str) -> str:
    """Level-① normalization recipe (spec 3.3.3): NFC normalization, whitespace-run
    collapse to a single space, strip. str.split() splits on all Unicode whitespace
    (incl. U+3000), so join+split both collapses and strips."""
    return " ".join(unicodedata.normalize("NFC", text).split())


def _dedup_text(rec: Record, cfg: "DedupConfig") -> str:
    """The text every dedup level operates on. Text modality: normalized extracted text;
    UI modality: canonical UITree serialization with quantized bounds (spec 3.3.3 ①)."""
    if rec.modality == "ui":
        if rec.ui_tree is None:
            return ""
        return rec.ui_tree.serialize(quantize_px=cfg.bounds_quantize_px)
    return _normalize_text(rec.text or "")


def _shingles(text: str, n: int) -> set[str]:
    """Character n-gram sliding-window shingle set over the (already collapsed) text."""
    if not text:
        return set()
    if len(text) <= n:
        return {text}
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def _build_minhash(text: str, ngram: int, num_perm: int) -> MinHash | None:
    sh = _shingles(text, ngram)
    if not sh:
        return None
    mh = MinHash(num_perm=num_perm)
    for s in sh:
        mh.update(s.encode("utf-8"))
    return mh


def _phash_int(image_path) -> int:
    """64-bit perceptual hash (imagehash default DCT pHash) packed into an int."""
    with Image.open(image_path) as im:
        h = imagehash.phash(im)
    value = 0
    for bit in h.hash.flatten():
        value = (value << 1) | int(bit)
    return value


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _l2_normalize(vec: Sequence[float]) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float64)
    norm = float(np.linalg.norm(v))
    if norm == 0.0:
        return v
    return v / norm


@dataclass
class _ProbeDetail:
    """Internal per-record probe scratchpad shared between DedupIndex and DedupStage
    (needed by the semantic level's composite verdict and by trace-event payloads)."""

    dedup_text: str
    digest: bytes
    own_key: str                                        # digest.hex()[:16]
    minhash: MinHash | None = None
    tree_hit: tuple[str, str, float] | None = None      # (kept_id, cluster_key, est. Jaccard)
    phash: int | None = None
    image_hit: tuple[str, str, int] | None = None       # (kept_id, cluster_key, Hamming)
    image_decode_failed: bool = False
    verdict: DedupInfo | None = None


# ── index ──────────────────────────────────────────────────────────────────


class DedupIndex:
    """In-memory dedup index: exact set[bytes] + datasketch.MinHashLSH + list[(id, phash)]
    (+ list[(id, unit_vec)] when dedup.semantic). scope='batch' → reset per batch."""

    def __init__(self, cfg: "DedupConfig", modality: Literal["text", "ui"]):
        self.cfg = cfg
        self.modality = modality
        self._last_similarity: float | None = None
        self._last_probe: _ProbeDetail | None = None
        self.reset()

    def reset(self) -> None:
        """Drop all index state. Called by DedupStage at batch start when scope='batch'."""
        self._exact: dict[bytes, str] = {}              # exact key digest -> kept record id
        self._digest_by_id: dict[str, bytes] = {}
        self._lsh = MinHashLSH(
            threshold=self.cfg.minhash_threshold, num_perm=self.cfg.minhash_num_perm
        )
        self._minhashes: dict[str, tuple[MinHash, str]] = {}   # id -> (signature, cluster_key)
        self._minhash_seq: dict[str, int] = {}                 # id -> insertion order
        self._seq = 0
        self._phashes: list[tuple[int, str, str]] = []         # (phash, id, cluster_key)
        self._vec_ids: list[str] = []
        self._vec_keys: list[str] = []
        self._vec_buf: np.ndarray | None = None
        self._vec_count = 0

    @property
    def last_similarity(self) -> float | None:
        """Measured metric of the most recent duplicate verdict: estimated Jaccard
        (near_text / near_both), Hamming distance (near_image), or None (exact)."""
        return self._last_similarity

    def probe_and_add(self, rec: Record) -> DedupInfo:
        """Levels ①②(③) probe; on unique, adds the record's keys/signature/phash to the
        index (first-writer-wins). Returns the DedupInfo for the record."""
        text = _dedup_text(rec, self.cfg)
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        detail = _ProbeDetail(dedup_text=text, digest=digest, own_key=digest.hex()[:16])
        self._last_probe = detail

        # ① exact — a hit is an unconditional duplicate in both modalities.
        kept = self._exact.get(digest)
        if kept is not None:
            self._last_similarity = None
            info = DedupInfo(kind="exact", cluster_key=detail.own_key, kept_id=kept)
            detail.verdict = info
            return info

        # ② near-text: char n-gram MinHash signature → LSH candidates → verify by
        # signature-estimated Jaccard (candidates checked in insertion order; best wins).
        mh = _build_minhash(text, self.cfg.ngram, self.cfg.minhash_num_perm)
        detail.minhash = mh
        if mh is not None:
            best: tuple[str, str, float] | None = None
            candidates = sorted(
                self._lsh.query(mh), key=lambda c: self._minhash_seq.get(c, 1 << 62)
            )
            for cand_id in candidates:
                entry = self._minhashes.get(cand_id)
                if entry is None:
                    continue
                est = float(mh.jaccard(entry[0]))
                if est >= self.cfg.minhash_threshold and (best is None or est > best[2]):
                    best = (cand_id, entry[1], est)
            detail.tree_hit = best

        # ③ near-image (UI modality only): 64-bit pHash, linear scan over kept hashes.
        # (Spec 3.3.3 suggests 16-bit-prefix bucketing as an acceleration; exact-prefix
        # bucketing is not sound for Hamming ≤ 8, so we keep the correct linear scan the
        # same spec row declares acceptable.)
        if self.modality == "ui" and rec.image is not None:
            try:
                detail.phash = _phash_int(rec.image.path)
            except Exception:
                detail.image_decode_failed = True
            if detail.phash is not None:
                best_img: tuple[str, str, int] | None = None
                for stored, sid, skey in self._phashes:
                    d = _hamming(stored, detail.phash)
                    if d <= self.cfg.image_phash_max_distance and (
                        best_img is None or d < best_img[2]
                    ):
                        best_img = (sid, skey, d)
                detail.image_hit = best_img

        info = self._compose(detail)
        detail.verdict = info
        if info.kind == "unique":
            self._add(rec.id, detail)
        return info

    def _compose(self, detail: _ProbeDetail) -> DedupInfo:
        """Composite ②③ verdict (spec 3.3.3/3.3.5): text modality = level ② alone;
        UI modality per dedup.ui_dup_requires. Both levels hitting → kind='near_both'."""
        tree, image = detail.tree_hit, detail.image_hit
        unique = DedupInfo(kind="unique", cluster_key=detail.own_key, kept_id=None)

        if self.modality == "text":
            if tree is None:
                return unique
            self._last_similarity = tree[2]
            return DedupInfo(kind="near_text", cluster_key=tree[1], kept_id=tree[0])

        requires = self.cfg.ui_dup_requires
        if detail.image_decode_failed:
            # Image decode failure ⇒ this record skips the pHash layer and is judged by
            # the tree alone (spec 3.3.4 "跳过 pHash 层（按树判定）", CONTRACTS.md §7.2):
            # "both" and "image" degrade to "tree" for this record.
            requires = "tree"
        if requires == "both":
            is_dup = tree is not None and image is not None
        elif requires == "tree":
            is_dup = tree is not None
        else:  # "image"
            is_dup = image is not None
        if not is_dup:
            return unique

        if tree is not None and image is not None:
            self._last_similarity = tree[2]
            return DedupInfo(kind="near_both", cluster_key=tree[1], kept_id=tree[0])
        if tree is not None:
            self._last_similarity = tree[2]
            return DedupInfo(kind="near_text", cluster_key=tree[1], kept_id=tree[0])
        assert image is not None
        self._last_similarity = float(image[2])
        return DedupInfo(kind="near_image", cluster_key=image[1], kept_id=image[0])

    def _add(self, rec_id: str, detail: _ProbeDetail) -> None:
        """Index a kept (unique) record: exact key, MinHash signature, pHash."""
        self._exact[detail.digest] = rec_id
        self._digest_by_id[rec_id] = detail.digest
        if detail.minhash is not None:
            self._lsh.insert(rec_id, detail.minhash)
            self._minhashes[rec_id] = (detail.minhash, detail.own_key)
            self._minhash_seq[rec_id] = self._seq
            self._seq += 1
        if detail.phash is not None:
            self._phashes.append((detail.phash, rec_id, detail.own_key))

    def _retract(self, rec_id: str) -> None:
        """Remove a record's ①②③ entries again — used when the semantic level (which runs
        after probe_and_add already indexed the record as unique) flips the verdict to
        duplicate, preserving first-writer-wins (only kept records stay indexed)."""
        digest = self._digest_by_id.pop(rec_id, None)
        if digest is not None and self._exact.get(digest) == rec_id:
            del self._exact[digest]
        if rec_id in self._minhashes:
            del self._minhashes[rec_id]
            self._minhash_seq.pop(rec_id, None)
            try:
                self._lsh.remove(rec_id)
            except Exception:
                pass
        self._phashes = [e for e in self._phashes if e[1] != rec_id]

    # ── semantic level ④ (only used when cfg.semantic) ────────────────────

    def semantic_probe(self, vec: list[float]) -> tuple[str, str, float] | None:
        """Returns (kept_id, cluster_key, cosine) of the best match with cosine >= threshold,
        else None. vec must be L2-normalized (cosine = dot product, spec 3.3.3 ④)."""
        if self._vec_count == 0:
            return None
        sims = self._vec_buf[: self._vec_count] @ np.asarray(vec, dtype=np.float64)
        best = int(np.argmax(sims))  # ties → lowest index = earliest writer
        cosine = float(sims[best])
        if cosine >= self.cfg.semantic_threshold:
            return (self._vec_ids[best], self._vec_keys[best], cosine)
        return None

    def add_vector(self, rec_id: str, cluster_key: str, vec: list[float]) -> None:
        v = np.asarray(vec, dtype=np.float64)
        if self._vec_buf is None:
            self._vec_buf = np.empty((16, v.shape[0]), dtype=np.float64)
        elif self._vec_count == self._vec_buf.shape[0]:
            grown = np.empty((self._vec_buf.shape[0] * 2, self._vec_buf.shape[1]),
                             dtype=np.float64)
            grown[: self._vec_count] = self._vec_buf[: self._vec_count]
            self._vec_buf = grown
        self._vec_buf[self._vec_count] = v
        self._vec_ids.append(rec_id)
        self._vec_keys.append(cluster_key)
        self._vec_count += 1


# ── stage ──────────────────────────────────────────────────────────────────


class DedupStage:
    name = "dedup"

    def __init__(self, cfg: "DedupConfig", index: DedupIndex):
        self.cfg = cfg
        self.index = index
        self._counted_clusters: set[str] = set()   # run-level distinct duplicate clusters

    async def run(self, batch: list[PipelineItem], ctx: "RunContext") -> list[PipelineItem]:
        if self.cfg.scope == "batch":
            self.index.reset()
        for item in batch:
            if item.status != "active":
                continue
            try:
                await self._process(item, ctx)
            except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as exc:  # single-record failure never escapes to batch level
                err = StageError(
                    stage=self.name,
                    kind=ErrorKind.INTERNAL_ERROR.value,
                    message=f"{type(exc).__name__}: {exc}",
                    retryable=False,
                )
                item.errors.append(err)
                item.status = "failed"
                ctx.metrics.event(
                    _EV_ERROR,
                    stage=self.name,
                    batch_no=ctx.batch_no,
                    record_ids=(item.record.id,),
                    payload={"stage": err.stage, "kind": err.kind,
                             "message": err.message, "retryable": err.retryable},
                )
        return batch

    async def _process(self, item: PipelineItem, ctx: "RunContext") -> None:
        rec = item.record
        info = self.index.probe_and_add(rec)
        detail = self.index._last_probe
        assert detail is not None
        if detail.image_decode_failed:
            # Skip pHash for this record (tree-only verdict); record stays active,
            # no StageError (CONTRACTS.md §7.2 [FROZEN HERE]).
            ctx.metrics.count("dedup.image_decode_failures")

        metric: tuple[str, int | float] | None = None
        if info.kind == "unique" and self.cfg.semantic and self._semantic_participates(detail):
            sem = await self._semantic_level(rec, detail, ctx)
            if sem is not None:
                info, metric = sem
                self.index._retract(rec.id)
        elif info.kind != "unique":
            metric = self._metric_for(info, detail)

        if info.kind == "unique":
            item.dedup = info
            return

        item.status = "dropped_dup"
        item.dedup = info
        payload: dict = {"kind": info.kind, "cluster_key": info.cluster_key,
                         "kept_id": info.kept_id}
        if metric is not None:
            payload[metric[0]] = metric[1]
        ctx.metrics.event(
            _EV_DEDUP_DUPLICATE,
            stage=self.name,
            batch_no=ctx.batch_no,
            record_ids=(rec.id,),
            payload=payload,
        )
        ctx.metrics.count(f"dedup.{info.kind}")
        if info.cluster_key not in self._counted_clusters:
            self._counted_clusters.add(info.cluster_key)
            ctx.metrics.count("dedup.clusters")

    @staticmethod
    def _metric_for(info: DedupInfo, detail: _ProbeDetail) -> tuple[str, int | float] | None:
        """Exactly one metric per dedup.duplicate event (CONTRACTS.md §8.1): jaccard for
        near_text (and ②-driven near_both), hamming for near_image, none for exact."""
        if info.kind in ("near_text", "near_both") and detail.tree_hit is not None:
            return ("jaccard", detail.tree_hit[2])
        if info.kind == "near_image" and detail.image_hit is not None:
            return ("hamming", detail.image_hit[2])
        return None

    def _semantic_participates(self, detail: _ProbeDetail) -> bool:
        # ④ counts as a tree-level hit; under ui_dup_requires="image" it does not take part
        # in the verdict (spec 3.3.3), so no embedding is spent there — unless this record's
        # image failed to decode, in which case the record is judged by the tree alone
        # (spec 3.3.4) and ④ participates as it would under "tree".
        if self.index.modality == "text" or self.cfg.ui_dup_requires != "image":
            return True
        return detail.image_decode_failed

    def _semantic_verdict_kind(self, detail: _ProbeDetail) -> str | None:
        """Composite kind for a level-④ hit (spec 3.3.3: ④ counts as a tree-level hit).
        None → the hit alone does not constitute a duplicate (record stays unique)."""
        if self.index.modality == "text":
            return "near_semantic"
        if self.cfg.ui_dup_requires == "both" and not detail.image_decode_failed:
            # ④ is a tree-level hit: "both" additionally needs the image level.
            return "near_both" if detail.image_hit is not None else None
        # "tree" — including "both"/"image" degraded to tree-only when this record's
        # image failed to decode (spec 3.3.4). ④+③ together still records near_both.
        return "near_both" if detail.image_hit is not None else "near_semantic"

    async def _semantic_level(
        self, rec: Record, detail: _ProbeDetail, ctx: "RunContext"
    ) -> tuple[DedupInfo, tuple[str, float]] | None:
        """Level ④: one embed() call for this record; verdict per the composite rules.
        Returns (duplicate DedupInfo, ('cosine', value)) or None (record stays unique)."""
        try:
            vecs = await ctx.llm.embed(self.cfg.semantic_embedding, [detail.dedup_text])
        except (ProviderRetryableError, ProviderFatalError):
            # Retries exhausted / fatal for this call: skip level ④ for this record,
            # verdict stands on ①–③ (spec 3.3.4). Breaker bookkeeping is M9's job.
            ctx.metrics.count("dedup.embedding_failures")
            return None
        vec = _l2_normalize(vecs[0])
        hit = self.index.semantic_probe(list(vec))
        kind = None if hit is None else self._semantic_verdict_kind(detail)

        if kind is None:
            # Kept: its vector joins the index (first-writer-wins).
            self.index.add_vector(rec.id, detail.own_key, list(vec))
            return None
        kept_id, cluster_key, cosine = hit
        return (
            DedupInfo(kind=kind, cluster_key=cluster_key, kept_id=kept_id),
            ("cosine", cosine),
        )

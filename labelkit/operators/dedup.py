"""M3 去重（spec 3.3）：精确 SHA-256 → MinHash-LSH 近文本 → pHash 近图像（UI）
→ 可选语义级（嵌入余弦，v1.2）。先到先得；重复项只改状态标记，绝不从列表中移除。
默认配置下不调用任何 LLM / 嵌入 API。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NoReturn, Sequence

import imagehash
import numpy as np
from datasketch import MinHash, MinHashLSH
from PIL import Image

from labelkit.common.errors import (
    CircuitBreakerTripped,
    ContextOverflowError,
    ErrorKind,
    InternalError,
    ProviderFatalError,
    ProviderRetryableError,
)
from labelkit.common.contracts.execution import TaskGroupRequest, TaskSpec
from labelkit.common.contracts.generation import DedupGroupRequest, DedupReservation
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import DedupInfo, PipelineItem, Record, StageError
from labelkit.common.inference import budget

if TYPE_CHECKING:
    from labelkit.common.config.model import DedupConfig

# 事件名（规范定义在 labelkit.obslog；这里用字面量以免本模块反向依赖 obslog ——
# 测试会逐字断言这些字符串，CONTRACTS.md §7.11/§8.1）
_EV_DEDUP_DUPLICATE = "dedup.duplicate"
_EV_ERROR = "error"

# 本模块的 stderr 运行日志通道（spec §7.1：日志恒不含数据内容）
_LOGGER = logging.getLogger("labelkit.dedup")
# 日志记录附加字段：索引层不知道批次号，故 batch 留空（文本格式化器渲染为 "-"）
_LOG_EXTRA: dict[str, Any] = {"stage": "dedup", "batch": None}


def _dedup_contract_error(message: str) -> NoReturn:
    """记录并抛出 sequence dedup 事务契约错误。

    @param message 固定英文错误文本。
    @return 不返回。
    """
    _LOGGER.error(message, extra=_LOG_EXTRA)
    raise InternalError(message)


# ── 纯函数辅助


def _normalize_text(text: str) -> str:
    """① 级归一化配方（spec 3.3.3）：NFC 归一 + 连续空白折叠为单个空格 + 去首尾空白。

    str.split() 按全部 Unicode 空白切分（含 U+3000），故 join+split 同时完成折叠与
    去首尾。

    @param text 原始文本
    @return 归一化后的文本
    """
    return " ".join(unicodedata.normalize("NFC", text).split())


def _dedup_text(rec: Record, cfg: "DedupConfig") -> str:
    """各去重级共同作用的那份文本。

    文本模态：归一化后的提取文本；UI 模态：bounds 量化后的 UITree 规范序列化
    （spec 3.3.3 ①）。

    序列记录（v1.8、S10 —— 本分支优先于模态分支）：各成员各自的单记录配方，按成员
    顺序以分隔符 "\\x1e"（ASCII Record Separator, 0x1E）拼接。该分隔符在 Python 眼里
    是空白（isspace() == True），所以折叠空白后的文本配方永远吐不出它 —— 拼接串对任
    何单记录配方输出都结构性免撞（spec 3.3.3 序列行）。

    @param rec 待判重记录
    @param cfg [dedup] 配置节
    @return 该记录的判重文本
    """
    if rec.kind == "sequence":
        return "\x1e".join(_dedup_text(m, cfg) for m in rec.members)
    if rec.modality == "ui":
        if rec.ui_tree is None:
            return ""
        return rec.ui_tree.serialize(quantize_px=cfg.bounds_quantize_px)
    return _normalize_text(rec.text or "")


def _generation_exact_material(rec: Record) -> str | None:
    """构造 generation stream Record 的 v1.20 exact-only canonical 材料。

    @param rec M2 已写入 non-temporal payload carrier 的记录。
    @return v1.20 域分离材料；普通或 mixed sequence 返回 None。
    """
    if rec.kind == "sequence":
        values = tuple(member.exact_dedup_text for member in rec.members)
        if not values or any(value is None for value in values):
            return None
    elif rec.exact_dedup_text is not None:
        values = (rec.exact_dedup_text,)
    else:
        return None
    return json.dumps(
        ["labelkit:v1.20", "generation_stream_exact", values],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _shingles(text: str, n: int) -> set[str]:
    """在（已折叠空白的）文本上做字符 n-gram 滑窗，取 shingle 集合。

    @param text 判重文本
    @param n n-gram 长度
    @return shingle 集合（空文本返回空集）
    """
    if not text:
        return set()
    if len(text) <= n:
        return {text}
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def _build_minhash(text: str, ngram: int, num_perm: int) -> MinHash | None:
    """为文本构造 MinHash 签名。

    @param text 判重文本
    @param ngram 字符 n-gram 长度
    @param num_perm MinHash 置换数
    @return MinHash 签名；shingle 集为空时返回 None
    """
    sh = _shingles(text, ngram)
    if not sh:
        return None
    mh = MinHash(num_perm=num_perm)
    for s in sh:
        mh.update(s.encode("utf-8"))
    return mh


def _phash_int(image_path) -> int:
    """计算 64 位感知哈希（imagehash 默认的 DCT pHash）并打包成整数。

    @param image_path 图像文件路径
    @return 64 位 pHash 的整数形式
    """
    with Image.open(image_path) as im:
        h = imagehash.phash(im)
    value = 0
    for bit in h.hash.flatten():
        value = (value << 1) | int(bit)
    return value


def _hamming(a: int, b: int) -> int:
    """计算两个整数形式哈希的汉明距离。

    @param a 哈希一
    @param b 哈希二
    @return 汉明距离（不同比特位数）
    """
    return (a ^ b).bit_count()


def _l2_normalize(vec: Sequence[float]) -> np.ndarray:
    """把向量做 L2 归一（零向量原样返回）。

    @param vec 原始向量
    @return 单位向量（此后余弦相似度 = 点积）
    """
    v = np.asarray(vec, dtype=np.float64)
    norm = float(np.linalg.norm(v))
    if norm == 0.0:
        return v
    return v / norm


@dataclass
class _ProbeDetail:
    """逐记录的探测便签（内部），由 DedupIndex 与 DedupStage 共用
    （语义级的复合判决与 trace 事件 payload 都要读它）。"""

    dedup_text: str                                     # 该记录的判重文本
    digest: bytes                                       # 判重文本的 SHA-256 摘要
    own_key: str                                        # digest.hex()[:16]
    is_sequence: bool = False                           # v1.8：rec.kind == "sequence"（S10）
    exact_only: bool = False                            # generation stream 仅运行 exact 层
    minhash: MinHash | None = None                      # ② 级 MinHash 签名
    tree_hit: tuple[str, str, float] | None = None      # (保留记录 id, 簇键, Jaccard 估计)
    phash: int | None = None                            # ③ 级 64 位 pHash
    image_hit: tuple[str, str, int] | None = None       # (保留记录 id, 簇键, 汉明距离)
    image_decode_failed: bool = False                   # 图像解码失败 ⇒ 本记录跳过 pHash 层
    verdict: DedupInfo | None = None                    # 最终判决（复合后回填）


@dataclass(frozen=True)
class _PreparedRecord:
    """普通去重归并前的一条纯计算记录计划。"""

    ordinal: int                                        # active 输入序
    item: PipelineItem                                  # 只在归并阶段修改的信封
    detail: _ProbeDetail                                # 冻结 CPU 特征
    embedding_input: str | None                         # 静态参与语义层时的输入


@dataclass(frozen=True)
class _EmbeddingOutcome:
    """一次普通 semantic embedding 叶调用的冻结结果。"""

    vector: tuple[float, ...] | None                    # 成功时的单位向量
    error: Exception | None                             # 记录级失败；未失败时为空


@dataclass(frozen=True)
class _GroupFeatures:
    """一次已提交 sequence set 的三族判重特征。"""

    record_ids: tuple[str, ...]                         # 与特征同序的记录标识
    record_digests: tuple[str, ...]                     # 记录与判重文本的绑定摘要
    exact_features: tuple[str, ...]                     # 全文精确摘要
    minhash_features: tuple[MinHash | None, ...]        # 近文本签名
    embedding_features: tuple[tuple[float, ...], ...]   # 单位语义向量；关闭时为空


@dataclass
class _ReservationEntry:
    """DedupIndex 私有 registry 中的一次 pending reservation。"""

    records: tuple[Record, ...]                         # 记录引用，提交前复算摘要
    features: _GroupFeatures                            # reserve 阶段预计算的完整特征
    exempt_pairs: frozenset[tuple[str, str]]            # 当前组内豁免对
    epoch: int                                          # 创建时 reset epoch
    state: Literal["reserved", "validated"]            # 当前状态
    validated_generation: int | None                    # 成功重验的正式索引代次


# ── 索引


class DedupIndex:
    """内存判重索引：精确 set[bytes] + datasketch.MinHashLSH + list[(id, phash)]
    （dedup.semantic 开启时再加 list[(id, 单位向量)]）。scope='batch' ⇒ 每批重置。"""

    def __init__(self, cfg: "DedupConfig", modality: Literal["text", "ui"]):
        """构造判重索引。

        @param cfg [dedup] 配置节
        @param modality 运行模态（"text" | "ui"）
        """
        self.cfg = cfg
        self.modality = modality
        self._last_similarity: float | None = None
        self._last_probe: _ProbeDetail | None = None
        self.reset()

    def reset(self) -> None:
        """清空全部索引状态。scope='batch' 时由 DedupStage 在批开始处调用。

        @return 无
        """
        self._exact: dict[bytes, str] = {}              # 精确键摘要 → 保留记录 id
        self._digest_by_id: dict[str, bytes] = {}       # 记录 id → 精确键摘要
        self._lsh = MinHashLSH(
            threshold=self.cfg.minhash_threshold, num_perm=self.cfg.minhash_num_perm
        )
        self._minhashes: dict[str, tuple[MinHash, str]] = {}   # id → (签名, 簇键)
        self._minhash_seq: dict[str, int] = {}                 # id → 插入序号
        self._seq = 0                                          # 插入序号发号器
        self._phashes: list[tuple[int, str, str]] = []         # (pHash, id, 簇键)
        self._vec_ids: list[str] = []                          # ④ 级：向量对应的记录 id
        self._vec_keys: list[str] = []                         # ④ 级：向量对应的簇键
        self._vec_buf: np.ndarray | None = None                # ④ 级：向量缓冲（倍增扩容）
        self._vec_count = 0                                    # ④ 级：已入缓冲的向量数
        self._group_exact: dict[str, str] = {}                   # sequence 精确索引
        self._group_lsh = MinHashLSH(                            # sequence MinHash 索引
            threshold=self.cfg.minhash_threshold, num_perm=self.cfg.minhash_num_perm
        )
        self._group_minhashes: dict[str, MinHash] = {}           # LSH 候选复核签名
        self._group_vec_ids: list[str] = []                      # sequence semantic 标识
        self._group_vec_buf: np.ndarray | None = None            # sequence 单位向量索引
        self._group_vec_count = 0                                # 已提交 semantic 向量数
        self._group_reservations: dict[str, _ReservationEntry] = {}  # pending registry
        self._group_epoch = getattr(self, "_group_epoch", -1) + 1
        self._group_generation = 0

    @property
    def last_similarity(self) -> float | None:
        """最近一次判重判决所用的度量值。

        @return Jaccard 估计（near_text / near_both）、汉明距离（near_image）或
                None（exact）
        """
        return self._last_similarity

    def probe_and_add(self, rec: Record) -> DedupInfo:
        """①②（③）级探测；判为唯一时把记录的键 / 签名 / pHash 写入索引。

        @param rec 待判重记录
        @return 该记录的 DedupInfo 判决
        """
        detail = self.prepare(rec)
        info = self.probe_prepared(detail)
        if info.kind == "unique":
            self._add(rec.id, detail)
        return info

    def prepare(self, rec: Record) -> _ProbeDetail:
        """不查询或修改正式索引地冻结一条记录的全部 CPU 特征。

        @param rec 待判重记录
        @return exact、MinHash、pHash 与静态身份特征
        """
        exact_material = _generation_exact_material(rec)
        text = exact_material if exact_material is not None else _dedup_text(rec, self.cfg)
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        detail = _ProbeDetail(dedup_text=text, digest=digest, own_key=digest.hex()[:16],
                              is_sequence=rec.kind == "sequence",
                              exact_only=exact_material is not None)
        if not detail.exact_only:
            detail.minhash = _build_minhash(text, self.cfg.ngram, self.cfg.minhash_num_perm)
        if not detail.exact_only and self.modality == "ui" and rec.image is not None:
            self._prepare_image(rec.image.path, detail)
        return detail

    def probe_prepared(self, detail: _ProbeDetail) -> DedupInfo:
        """按最新正式索引探测冻结特征，但不提交唯一记录。

        @param detail 由 prepare 产生的 CPU 特征
        @return 当前正式前缀下的判决
        """
        self._last_probe = detail
        kept = self._exact.get(detail.digest)
        if kept is not None:
            self._last_similarity = None
            info = DedupInfo(kind="exact", cluster_key=detail.own_key, kept_id=kept)
            detail.verdict = info
            return info
        self._query_near_text(detail)
        self._query_near_image(detail)
        info = self._compose(detail)
        detail.verdict = info
        return info

    def commit_prepared(self, rec_id: str, detail: _ProbeDetail) -> None:
        """把已完成全层判决的唯一记录写入正式 CPU 索引。

        @param rec_id 被保留记录标识
        @param detail 已探测的冻结特征
        """
        self._add(rec_id, detail)

    def _query_near_text(self, detail: _ProbeDetail) -> None:
        """用预计算 MinHash 查询最新正式近文本索引。

        @param detail 本记录的探测便签（就地写入 minhash / tree_hit）
        @return 无
        """
        mh = detail.minhash
        if mh is None:
            return
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

    @staticmethod
    def _prepare_image(image_path, detail: _ProbeDetail) -> None:
        """不查询正式索引地计算一条 UI 记录的 pHash。

        @param image_path 本记录的图像路径
        @param detail 本记录的冻结特征
        """
        try:
            detail.phash = _phash_int(image_path)
        except Exception as exc:
            detail.image_decode_failed = True
            _LOGGER.debug("image decode failed, dedup judges this record by tree alone: %s",
                          type(exc).__name__, extra=_LOG_EXTRA)

    def _query_near_image(self, detail: _ProbeDetail) -> None:
        """用预计算 pHash 查询最新正式图像索引。

        @param detail 本记录的探测便签（就地写入 image_hit）
        """
        if detail.phash is None:
            return
        best_img: tuple[str, str, int] | None = None
        for stored, sid, skey in self._phashes:
            d = _hamming(stored, detail.phash)
            if d <= self.cfg.image_phash_max_distance and (
                best_img is None or d < best_img[2]
            ):
                best_img = (sid, skey, d)
        detail.image_hit = best_img

    def _compose(self, detail: _ProbeDetail) -> DedupInfo:
        """②③ 复合判决（spec 3.3.3/3.3.5）：文本模态只看 ② 级；UI 模态按
        dedup.ui_dup_requires 判。两级同时命中 ⇒ kind='near_both'。

        序列记录（v1.8，spec 3.3.3 序列行）：image 恒为 None ⇒ image_hit 恒为 None，
        于是复合判决退化成 "tree" 语义 —— 与图像解码失败同形的退化（spec 3.3.4）。

        @param detail 本记录的探测便签
        @return 复合后的 DedupInfo 判决
        """
        tree, image = detail.tree_hit, detail.image_hit
        unique = DedupInfo(kind="unique", cluster_key=detail.own_key, kept_id=None)

        if self.modality == "text":
            if tree is None:
                return unique
            self._last_similarity = tree[2]
            return DedupInfo(kind="near_text", cluster_key=tree[1], kept_id=tree[0])

        requires = self.cfg.ui_dup_requires
        if detail.image_decode_failed or detail.is_sequence:
            # 图像解码失败 ⇒ 本记录跳过 pHash 层、按树判定（spec 3.3.4「跳过 pHash 层
            # （按树判定）」、CONTRACTS.md §7.2）：对本记录而言 "both" 与 "image" 都退
            # 化为 "tree"。序列记录走同构的退化（spec 3.3.3 序列行，S10）。
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
        """把一条被保留（唯一）的记录写入索引：精确键、MinHash 签名、pHash。

        @param rec_id 记录 id
        @param detail 本记录的探测便签
        @return 无
        """
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
        """把一条记录的 ①②③ 条目再撤回。

        用于语义级（它跑在 probe_and_add 之后，那时记录已按唯一入索引）把判决翻成
        重复的场合，从而维持先到先得（只有被保留的记录才留在索引里）。

        @param rec_id 记录 id
        @return 无
        """
        digest = self._digest_by_id.pop(rec_id, None)
        if digest is not None and self._exact.get(digest) == rec_id:
            del self._exact[digest]
        if rec_id in self._minhashes:
            del self._minhashes[rec_id]
            self._minhash_seq.pop(rec_id, None)
            try:
                self._lsh.remove(rec_id)
            except Exception as exc:
                _LOGGER.debug("LSH entry was already absent on retract: %s",
                              type(exc).__name__, extra=_LOG_EXTRA)
        self._phashes = [e for e in self._phashes if e[1] != rec_id]

    # ── 语义级 ④（仅 cfg.semantic 开启时使用）

    def semantic_probe(self, vec: list[float]) -> tuple[str, str, float] | None:
        """在向量索引里找余弦 >= 阈值的最优匹配。

        vec 必须已 L2 归一（此时余弦 = 点积，spec 3.3.3 ④）。

        @param vec 本记录的单位向量
        @return (保留记录 id, 簇键, 余弦)；无命中返回 None
        """
        if self._vec_count == 0:
            return None
        sims = self._vec_buf[: self._vec_count] @ np.asarray(vec, dtype=np.float64)
        best = int(np.argmax(sims))  # 并列取下标最小者 = 最早写入者
        cosine = float(sims[best])
        if cosine >= self.cfg.semantic_threshold:
            return (self._vec_ids[best], self._vec_keys[best], cosine)
        return None

    def add_vector(self, rec_id: str, cluster_key: str, vec: list[float]) -> None:
        """把一条被保留记录的向量加入 ④ 级索引（缓冲满则倍增扩容）。

        @param rec_id 记录 id
        @param cluster_key 该记录的簇键
        @param vec 单位向量
        @return 无
        """
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

    async def group_reserve(
        self,
        request: "DedupGroupRequest",
        context: "RunContext",
    ) -> "DedupReservation":
        """无正式索引突变地探测整组记录并创建一次性 reservation。

        @param request 当前 set 的记录、组内豁免对与可选 embedding profile。
        @param context 与 GenerationServices 共享对象身份的运行上下文。
        @return 当前 coordinator 唯一拥有的冻结 reservation。
        @raises DedupGroupRejected 与已提交组或非豁免组内记录重复。
        """
        records = tuple(request.records)
        texts = tuple(_dedup_text(record, self.cfg) for record in records)
        exact = tuple(hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts)
        minhash = tuple(_build_minhash(text, self.cfg.ngram, self.cfg.minhash_num_perm)
                        for text in texts)
        embeddings = await self._group_embeddings(texts, request.embedding_profile, context)
        features = _GroupFeatures(
            record_ids=tuple(record.id for record in records),
            record_digests=self._group_record_digests(records),
            exact_features=exact,
            minhash_features=minhash,
            embedding_features=embeddings,
        )
        self._validate_group_commit(features)
        self._reject_group_duplicate(features, request.exempt_pairs)
        capability_id = secrets.token_hex(16)
        self._group_reservations[capability_id] = _ReservationEntry(
            records=records,
            features=features,
            exempt_pairs=request.exempt_pairs,
            epoch=self._group_epoch,
            state="reserved",
            validated_generation=None,
        )
        return DedupReservation(
            capability_id=capability_id,
            epoch=self._group_epoch,
            record_digests=features.record_digests,
            exact_cluster_keys=tuple(value[:16] for value in features.exact_features),
        )

    def group_revalidate(self, reservation: "DedupReservation") -> None:
        """对最新正式索引重验一个 Reserved reservation。

        @param reservation 当前 epoch 的 Reserved capability。
        @return None。
        @raises DedupGroupRejected 最新正式前缀与当前组冲突。
        @raises InternalError capability 缺失、过期、被篡改或状态非法。
        """
        entry = self._reservation_entry(reservation, {"reserved"})
        self._validate_group_commit(entry.features)
        self._reject_group_duplicate(entry.features, entry.exempt_pairs)
        entry.state = "validated"
        entry.validated_generation = self._group_generation

    def group_commit(self, reservation: "DedupReservation") -> None:
        """消费当前 generation 已重验的 reservation 并写入正式索引。

        @param reservation 当前 generation 的 Validated capability。
        @return None。
        @raises InternalError capability 无效、状态非法或 generation 已变化。
        """
        entry = self._reservation_entry(reservation, {"validated"})
        if entry.validated_generation != self._group_generation:
            _dedup_contract_error("generation_dedup_transaction: stale validated reservation")
        self._commit_group_features(entry.features)
        del self._group_reservations[reservation.capability_id]
        self._group_generation += 1

    def group_discard(self, reservation: "DedupReservation") -> None:
        """严格消费一个 Reserved 或 Validated reservation，且不写正式索引。

        @param reservation 当前 coordinator 或候选缓冲拥有的 capability。
        @return None。
        @raises InternalError capability 无效、过期、被篡改或已消费。
        """
        self._reservation_entry(reservation, {"reserved", "validated"})
        del self._group_reservations[reservation.capability_id]

    def _reservation_entry(
        self,
        reservation: "DedupReservation",
        states: set[str],
    ) -> _ReservationEntry:
        """校验外部 capability 并返回唯一 registry entry。

        @param reservation 外部冻结 capability。
        @param states 当前操作允许的内部状态集合。
        @return 与 capability 一一对应的私有 entry。
        """
        entry = self._group_reservations.get(reservation.capability_id)
        if entry is None:
            _dedup_contract_error("generation_dedup_transaction: invalid reservation")
        if reservation.epoch != self._group_epoch or entry.epoch != self._group_epoch:
            _dedup_contract_error("generation_dedup_transaction: stale reservation")
        exact_keys = tuple(value[:16] for value in entry.features.exact_features)
        if reservation.record_digests != entry.features.record_digests:
            _dedup_contract_error("generation_dedup_transaction: altered reservation")
        if reservation.exact_cluster_keys != exact_keys:
            _dedup_contract_error("generation_dedup_transaction: altered reservation")
        if self._group_record_digests(entry.records) != entry.features.record_digests:
            _dedup_contract_error("generation_dedup_transaction: stale reservation")
        if entry.state not in states:
            _dedup_contract_error("generation_dedup_transaction: invalid reservation state")
        return entry

    def _commit_group_features(self, features: _GroupFeatures) -> None:
        """在全部能力预检通过后同步写入三类正式索引。

        @param features 只有 DedupIndex 持有的特征。
        @return None。
        """
        generation = self._group_generation
        for index, record_id in enumerate(features.record_ids):
            exact = features.exact_features[index]
            self._group_exact.setdefault(exact, record_id)
            signature = features.minhash_features[index]
            key = f"g{generation}:{index}"
            if signature is not None:
                self._group_lsh.insert(key, signature)
                self._group_minhashes[key] = signature
            if features.embedding_features:
                self._group_add_vector(record_id, features.embedding_features[index])

    def _validate_group_commit(self, features: _GroupFeatures) -> None:
        """在任何正式索引突变前验证整组形状与 semantic 维度。

        @param features 只有 DedupIndex 持有的特征。
        @return None。
        @raises InternalError 特征表错位或向量维度不一致。
        """
        size = len(features.record_ids)
        aligned = (
            len(features.record_digests) == size
            and len(features.exact_features) == size
            and len(features.minhash_features) == size
            and (not features.embedding_features or len(features.embedding_features) == size)
        )
        if not aligned:
            _dedup_contract_error("generation_dedup_transaction: feature count mismatch")
        if len(set(features.record_ids)) != size:
            _dedup_contract_error("generation_dedup_transaction: duplicate record id")
        vectors = features.embedding_features
        if not vectors:
            return
        dimensions = {len(vector) for vector in vectors}
        if len(dimensions) != 1 or 0 in dimensions:
            _dedup_contract_error("generation_dedup_transaction: embedding dimension mismatch")
        if any(not np.all(np.isfinite(vector)) for vector in vectors):
            _dedup_contract_error("generation_dedup_transaction: invalid embedding value")
        dimension = next(iter(dimensions))
        if self._group_vec_buf is not None and self._group_vec_buf.shape[1] != dimension:
            _dedup_contract_error("generation_dedup_transaction: embedding dimension mismatch")

    def _group_add_vector(self, record_id: str, vector: tuple[float, ...]) -> None:
        """向 sequence semantic 索引追加一个单位向量。

        @param record_id 记录标识。
        @param vector 已归一化向量。
        @return None。
        """
        value = np.asarray(vector, dtype=np.float64)
        if self._group_vec_buf is None:
            self._group_vec_buf = np.empty((16, value.shape[0]), dtype=np.float64)
        elif self._group_vec_count == self._group_vec_buf.shape[0]:
            shape = (self._group_vec_buf.shape[0] * 2, self._group_vec_buf.shape[1])
            grown = np.empty(shape, dtype=np.float64)
            grown[:self._group_vec_count] = self._group_vec_buf[:self._group_vec_count]
            self._group_vec_buf = grown
        self._group_vec_buf[self._group_vec_count] = value
        self._group_vec_ids.append(record_id)
        self._group_vec_count += 1

    def _group_record_digests(
        self,
        records: tuple[Record, ...],
    ) -> tuple[str, ...]:
        """绑定记录标识与精确内容特征，供 commit 前重验。

        @param records 与特征同序的冻结记录。
        @return 十六进制绑定摘要。
        """
        digests: list[str] = []
        for record in records:
            text = _dedup_text(record, self.cfg).encode("utf-8")
            content = hashlib.sha256(text).hexdigest()
            material = f"{record.id}\x00{content}".encode("utf-8")
            digests.append(hashlib.sha256(material).hexdigest())
        return tuple(digests)

    async def _group_embeddings(
        self, texts: tuple[str, ...], profile: str | None, context: "RunContext",
    ) -> tuple[tuple[float, ...], ...]:
        """为当前组计算 attempt-local 单位向量；关闭时不调用端点。

        @param texts 完整判重文本。
        @param profile embedding profile；None 表示关闭语义层。
        @param context LLM 与观测服务根。
        @return 与 records 同序的单位向量；关闭时为空 tuple。
        """
        if profile is None:
            return ()
        vectors = await context.llm.embed(profile, list(texts))
        if len(vectors) != len(texts):
            _dedup_contract_error("generation_dedup_transaction: embedding count mismatch")
        return tuple(tuple(float(value) for value in _l2_normalize(vector))
                     for vector in vectors)

    def _reject_group_duplicate(
        self, features: _GroupFeatures, exempt_pairs: frozenset[tuple[str, str]],
    ) -> None:
        """与已提交组及当前组非豁免配对比较，命中即拒绝整个 set。

        @param features 当前 set 的完整特征。
        @param exempt_pairs 允许相似的组内记录对。
        @return None。
        @raises DedupGroupRejected 任一不可豁免重复命中。
        """
        for index in range(len(features.record_ids)):
            if self._group_hits_committed(features, index):
                raise DedupGroupRejected("sequence group duplicates a committed set")
        for left in range(len(features.record_ids)):
            for right in range(left):
                pair = (features.record_ids[right], features.record_ids[left])
                reverse = (pair[1], pair[0])
                if pair not in exempt_pairs and reverse not in exempt_pairs:
                    if self._group_duplicate(features, left, features, right):
                        raise DedupGroupRejected("sequence group contains a duplicate pair")

    def _group_hits_committed(self, features: _GroupFeatures, index: int) -> bool:
        """使用三类正式索引判定一条记录是否命中已提交集。

        @param features 当前 attempt 特征。
        @param index 当前记录下标。
        @return exact、MinHash 或 semantic 任一命中时为 True。
        """
        if features.exact_features[index] in self._group_exact:
            return True
        signature = features.minhash_features[index]
        if signature is not None:
            for key in self._group_lsh.query(signature):
                if signature.jaccard(self._group_minhashes[key]) >= self.cfg.minhash_threshold:
                    return True
        if not features.embedding_features or self._group_vec_count == 0:
            return False
        return self._group_semantic_hit(features.embedding_features[index])

    def _group_semantic_hit(self, vector: tuple[float, ...]) -> bool:
        """在已提交单位向量索引中检查阈值命中。

        @param vector 当前记录的单位向量。
        @return 最大余弦相似度达阈值时为 True。
        """
        assert self._group_vec_buf is not None
        value = np.asarray(vector, dtype=np.float64)
        committed = self._group_vec_buf[:self._group_vec_count]
        if committed.shape[1] != value.shape[0]:
            _dedup_contract_error("generation_dedup_transaction: embedding dimension mismatch")
        similarities = committed @ value
        return bool(np.max(similarities) >= self.cfg.semantic_threshold)

    def _group_duplicate(
        self, left: _GroupFeatures, left_index: int,
        right: _GroupFeatures, right_index: int,
    ) -> bool:
        """按 exact、MinHash、semantic 顺序比较两个预计算记录槽。

        @param left 左侧特征组。
        @param left_index 左侧槽下标。
        @param right 右侧特征组。
        @param right_index 右侧槽下标。
        @return 任一启用层命中则 True。
        """
        if left.exact_features[left_index] == right.exact_features[right_index]:
            return True
        lhs, rhs = left.minhash_features[left_index], right.minhash_features[right_index]
        if lhs is not None and rhs is not None and lhs.jaccard(rhs) >= self.cfg.minhash_threshold:
            return True
        if not left.embedding_features or not right.embedding_features:
            return False
        cosine = float(np.dot(left.embedding_features[left_index],
                              right.embedding_features[right_index]))
        return cosine >= self.cfg.semantic_threshold


class DedupGroupRejected(Exception):
    """当前 sequence set 未通过正常 group dedup 准入。"""


# ── 算子


class DedupStage:
    """M3 去重算子：逐条探测判重，重复项只改状态为 dropped_dup，绝不移除列表元素。"""

    name = "dedup"

    def __init__(self, cfg: "DedupConfig", index: DedupIndex):
        """构造去重算子。

        @param cfg [dedup] 配置节
        @param index 共享的判重索引
        """
        self.cfg = cfg
        self.index = index
        self._counted_clusters: set[str] = set()   # 运行级去重后的重复簇集合

    async def run(self, batch: list[PipelineItem], ctx: "RunContext") -> list[PipelineItem]:
        """投机取得静态 semantic 结果，再按输入序完成全层判重提交。

        @param batch 本批信封列表
        @param ctx 运行上下文
        @return 原列表（就地改状态，元素永不移除）
        @raises CircuitBreakerTripped 熔断器已跳闸（批级传播）
        """
        if self.cfg.scope == "batch":
            self.index.reset()
        prepared: list[_PreparedRecord] = []
        for item in batch:
            if item.status != "active":
                continue
            try:
                detail = self.index.prepare(item.record)
                embed_input = None
                if self.cfg.semantic and self._semantic_participates(detail):
                    embed_input = self._embed_input(detail, ctx)
                prepared.append(_PreparedRecord(len(prepared), item, detail, embed_input))
            except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as exc:  # 单条失败绝不逃逸到批级
                _LOGGER.debug("record-level dedup failure: %s", type(exc).__name__,
                              extra={"stage": self.name, "batch": ctx.batch_no})
                self._fail_item(item, exc, ctx)
        outcomes = await self._run_embeddings(prepared, ctx)
        self._record_embedding_failures(outcomes, ctx)
        for value in prepared:
            try:
                self._reduce_one(value, outcomes.get(value.ordinal), ctx)
            except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as exc:
                _LOGGER.debug("record-level dedup failure: %s", type(exc).__name__,
                              extra={"stage": self.name, "batch": ctx.batch_no})
                self._fail_item(value.item, exc, ctx)
        return batch

    async def _run_embeddings(
        self, prepared: list[_PreparedRecord], ctx: "RunContext"
    ) -> dict[int, _EmbeddingOutcome]:
        """把全部静态参与项一次性提交给 embedding 资源通道。

        @param prepared 当前批纯计算计划
        @param ctx 当前运行上下文
        @return active ordinal 到真实叶结果的映射
        """
        active = [value for value in prepared if value.embedding_input is not None]
        tasks = tuple(TaskSpec(
            task_id=f"{ctx.task_namespace}:semantic:{value.ordinal}",
            declaration_key=(ctx.batch_no, 2, value.ordinal),
            stage=self.name,
            resource_key=("embedding", self.cfg.semantic_embedding),
            operation=lambda value=value: self._embed_one(value, ctx),
        ) for value in active)
        if not tasks:
            return {}
        results = await ctx.tasks.run_group(TaskGroupRequest(tasks=tasks))
        return {value.ordinal: result for value, result in zip(active, results, strict=True)}

    async def _embed_one(
        self, prepared: _PreparedRecord, ctx: "RunContext"
    ) -> _EmbeddingOutcome:
        """执行一个不修改信封或索引的 embedding 叶调用。

        @param prepared 单条纯计算计划
        @param ctx 当前运行上下文
        @return 单位向量或记录级异常
        """
        assert prepared.embedding_input is not None
        try:
            values = await ctx.llm.embed(self.cfg.semantic_embedding, [prepared.embedding_input])
            if len(values) != 1:
                raise InternalError("dedup embedding count mismatch")
            vector = _l2_normalize(values[0])
            if vector.ndim != 1 or not np.all(np.isfinite(vector)):
                raise InternalError("invalid dedup embedding vector")
            return _EmbeddingOutcome(tuple(float(value) for value in vector), None)
        except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as exc:
            return _EmbeddingOutcome(None, exc)

    def _record_embedding_failures(
        self, outcomes: dict[int, _EmbeddingOutcome], ctx: "RunContext"
    ) -> None:
        """记录全部已派发 provider 失败，不因其结果随后未使用而回滚。

        @param outcomes 全部静态参与项的叶结果
        @param ctx 当前运行上下文
        """
        for outcome in outcomes.values():
            if not isinstance(outcome.error, (ProviderRetryableError, ProviderFatalError)):
                continue
            _LOGGER.debug("level-4 embedding call failed, verdict stands on levels 1-3: %s",
                          type(outcome.error).__name__, extra=_LOG_EXTRA)
            ctx.metrics.count("dedup.embedding_failures")

    def _fail_item(self, item: PipelineItem, exc: Exception, ctx: "RunContext") -> None:
        """把单条失败落到信封上：判错误 kind、按矩阵喂熔断、记 StageError 并发 error 事件。

        @param item 出错的信封
        @param exc 捕获到的异常
        @param ctx 运行上下文
        @return 无
        """
        # v1.11（V27①）：预算词表优先路由 —— kind 不精确会落进 internal_error，破坏
        # §3.5 归因与 overflow_records 计数。可达路径是嵌入调用（M9 的咽喉/收尾处置）；
        # 嵌入输入已按 embed_budget 预截断（V15），所以这里是防御性的残余分支。
        kind = budget.classify_stage_error(exc) or ErrorKind.INTERNAL_ERROR.value
        if kind == ErrorKind.CONTEXT_OVERFLOW.value:
            ctx.metrics.count("budget.overflow_records")
            # A7/§7.8 熔断矩阵：只有 reactive-400（嗅探体）终局喂连败计数，且每个异常
            # 只喂一次（鸭子标记防传播路径重复喂）；precheck 与 200 形收尾神谕都不喂。
            # `origin` 取值做防御式读取，等 errors.py 修订后收敛。
            if (isinstance(exc, ContextOverflowError)
                    and exc.phase == "reactive"
                    and getattr(exc, "origin", "http_400") == "http_400"
                    and not getattr(exc, "_breaker_fed", False)):
                exc._breaker_fed = True  # type: ignore[attr-defined]
                ctx.metrics.record_provider_result(fatal=True)
        err = StageError(
            stage=self.name,
            kind=kind,
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

    def _reduce_one(
        self, prepared: _PreparedRecord, outcome: _EmbeddingOutcome | None,
        ctx: "RunContext",
    ) -> None:
        """按输入序使用最新正式索引归并一条推测结果。

        @param prepared 单条纯计算计划
        @param outcome 静态参与 semantic 时的真实叶结果
        @param ctx 当前运行上下文
        """
        item = prepared.item
        rec = item.record
        detail = prepared.detail
        if detail.image_decode_failed:
            ctx.metrics.count("dedup.image_decode_failures")
        info = self.index.probe_prepared(detail)
        if info.kind != "unique":
            self._apply_verdict(item, info, self._metric_for(info, detail), ctx)
            return
        if prepared.embedding_input is None:
            self.index.commit_prepared(rec.id, detail)
            self._apply_verdict(item, info, None, ctx)
            return
        if outcome is None:
            _LOGGER.error("dedup semantic outcome is missing", extra=_LOG_EXTRA)
            raise InternalError("dedup semantic outcome is missing")
        semantic = self._resolve_semantic(prepared, outcome)
        if semantic is None:
            self.index.commit_prepared(rec.id, detail)
            self._apply_verdict(item, info, None, ctx)
            return
        semantic_info, vector, metric = semantic
        if semantic_info is None:
            self.index.commit_prepared(rec.id, detail)
            self.index.add_vector(rec.id, detail.own_key, list(vector))
            self._apply_verdict(item, info, None, ctx)
            return
        detail.verdict = semantic_info
        self._apply_verdict(item, semantic_info, metric, ctx)

    def _resolve_semantic(
        self, prepared: _PreparedRecord, outcome: _EmbeddingOutcome,
    ) -> tuple[DedupInfo | None, tuple[float, ...], tuple[str, float] | None] | None:
        """在最新 semantic 索引上解释一个已保存叶结果。

        @param prepared 当前输入序记录计划
        @param outcome 对应真实 embedding 结果
        @return provider 跳过为 None；否则返回判决、向量与可选度量
        """
        if isinstance(outcome.error, (ProviderRetryableError, ProviderFatalError)):
            return None
        if outcome.error is not None:
            raise outcome.error
        if outcome.vector is None:
            _LOGGER.error("dedup semantic vector is missing", extra=_LOG_EXTRA)
            raise InternalError("dedup semantic vector is missing")
        hit = self.index.semantic_probe(list(outcome.vector))
        kind = None if hit is None else self._semantic_verdict_kind(prepared.detail)
        if kind is None:
            return None, outcome.vector, None
        kept_id, cluster_key, cosine = hit
        info = DedupInfo(kind=kind, cluster_key=cluster_key, kept_id=kept_id)
        return info, outcome.vector, ("cosine", cosine)

    def _apply_verdict(
        self, item: PipelineItem, info: DedupInfo,
        metric: tuple[str, int | float] | None, ctx: "RunContext",
    ) -> None:
        """把一个已按输入序冻结的判决写入信封与观测。

        @param item 当前信封
        @param info 最终判决
        @param metric 重复判决的唯一度量
        @param ctx 当前运行上下文
        """
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
            record_ids=(item.record.id,),
            payload=payload,
        )
        ctx.metrics.count(f"dedup.{info.kind}")
        if info.cluster_key not in self._counted_clusters:
            self._counted_clusters.add(info.cluster_key)
            ctx.metrics.count("dedup.clusters")

    @staticmethod
    def _metric_for(info: DedupInfo, detail: _ProbeDetail) -> tuple[str, int | float] | None:
        """每条 dedup.duplicate 事件恰好带一个度量（CONTRACTS.md §8.1）：near_text
        （以及由 ② 驱动的 near_both）带 jaccard，near_image 带 hamming，exact 不带。

        @param info 判重判决
        @param detail 本记录的探测便签
        @return (度量名, 度量值)；exact 判决返回 None
        """
        if info.kind in ("near_text", "near_both") and detail.tree_hit is not None:
            return ("jaccard", detail.tree_hit[2])
        if info.kind == "near_image" and detail.image_hit is not None:
            return ("hamming", detail.image_hit[2])
        return None

    def _semantic_participates(self, detail: _ProbeDetail) -> bool:
        """判定 ④ 级是否参与本记录的判决（决定要不要花这次嵌入调用）。

        ④ 算作树级命中；在 ui_dup_requires="image" 下它不参与判决（spec 3.3.3），因此
        那里不花嵌入 —— 除非本记录图像解码失败，此时记录按树判定（spec 3.3.4），④ 就
        按 "tree" 的方式参与。序列记录（v1.8、S10）在 "image" 下同样仍参与：它们的判重
        面本来就是拼接后的成员文本（spec 3.3.3 序列行）。

        @param detail 本记录的探测便签
        @return True 表示 ④ 级参与
        """
        if detail.exact_only:
            return False
        if self.index.modality == "text" or self.cfg.ui_dup_requires != "image":
            return True
        return detail.image_decode_failed or detail.is_sequence

    def _semantic_verdict_kind(self, detail: _ProbeDetail) -> str | None:
        """④ 级命中后的复合 kind（spec 3.3.3：④ 算作树级命中）。

        @param detail 本记录的探测便签
        @return 复合判重 kind；返回 None 表示仅凭该命中还不构成重复（记录保持唯一）
        """
        if self.index.modality == "text":
            return "near_semantic"
        if (self.cfg.ui_dup_requires == "both" and not detail.image_decode_failed
                and not detail.is_sequence):
            # ④ 是树级命中："both" 还额外需要图像级也命中
            return "near_both" if detail.image_hit is not None else None
        # "tree" —— 也包括本记录图像解码失败（spec 3.3.4）或本记录是序列（v1.8、S10：
        # image_hit 恒为 None，不得因此挡住 near_semantic 判决）时，由 "both"/"image"
        # 退化成的纯树判定。④+③ 同时命中仍记 near_both。
        return "near_both" if detail.image_hit is not None else "near_semantic"

    def _embed_input(self, detail: _ProbeDetail, ctx: "RunContext") -> str:
        """v1.11（V15，spec 3.3.3 嵌入输入预算截断）：[embedding.*] profile 声明了
        context_window 时，④ 级的嵌入输入（即 _dedup_text 产物，含序列/线程拼接）在
        调用前截断到 embed_budget = context_window − margin —— 确定性保头（嵌入的语义
        主体在文本前段）；cw == 0 时与 v1.10 的全文逐字节等价。①–③ 这些哈希级永远看
        到全文。

        @param detail 本记录的探测便签
        @param ctx 运行上下文
        @return 送去嵌入的文本
        """
        text = detail.dedup_text
        prof = (ctx.cfg.embedding_profiles.get(self.cfg.semantic_embedding)
                if ctx.cfg is not None else None)
        if prof is None or prof.context_window <= 0:
            return text
        cap = budget.embed_budget(prof)
        if budget.est_text(text) <= cap:
            return text
        ctx.metrics.count("budget.truncations.dedup")
        return budget.fit_text(text, cap, keep="head")

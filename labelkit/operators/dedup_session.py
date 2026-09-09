"""普通会话去重的只读正式前缀与当前尝试增量。"""
from __future__ import annotations

import logging
from dataclasses import replace

from labelkit.common.contracts.sequence_capacity import (
    SessionCapacityFailure, capacity_failures, capacity_target, raise_session_capacities, raise_session_capacity,
)
from labelkit.common.errors import ContextOverflowError, InternalError, ProviderFatalError
from labelkit.common.inference import budget
from labelkit.operators.dedup import DedupIndex, DedupStage

_log = logging.getLogger(__name__)


class _SessionIndex(DedupIndex):
    """只把当前尝试写进普通索引，正式前缀始终只读。"""

    def __init__(self, owner: DedupStage):
        """建立空增量。@param owner 拥有正式索引的阶段。"""
        super().__init__(owner.cfg, owner.index.modality)
        self.prefix = owner.index if owner.cfg.scope == "global" else None
        self.accepted: dict = {}
        self.vectors: dict = {}

    def probe_prepared(self, detail):
        """合并两段纯查询，正式前缀在同分时优先。@param detail 当前冻结特征。"""
        self._last_probe = detail
        self._last_similarity = None
        indexes = (self,) if self.prefix is None else (self.prefix, self)
        for index in indexes:
            kept = index._exact.get(detail.digest)
            if kept is not None:
                from labelkit.common.contracts.types import DedupInfo
                info = DedupInfo(kind="exact", cluster_key=detail.own_key, kept_id=kept)
                detail.verdict = info
                return info
        probes = []
        for index in indexes:
            probe = replace(detail, tree_hit=None, image_hit=None, verdict=None)
            index._query_near_text(probe)
            index._query_near_image(probe)
            probes.append(probe)
        trees = [probe.tree_hit for probe in probes if probe.tree_hit is not None]
        images = [probe.image_hit for probe in probes if probe.image_hit is not None]
        detail.tree_hit = max(trees, key=lambda hit: hit[2]) if trees else None
        detail.image_hit = min(images, key=lambda hit: hit[2]) if images else None
        detail.verdict = self._compose(detail)
        return detail.verdict

    def semantic_probe(self, vec):
        """合并语义近邻，不修改正式探测便签。@param vec 当前单位向量。"""
        hits = [super().semantic_probe(vec)]
        if self.prefix is not None:
            hits.insert(0, self.prefix.semantic_probe(vec))
        present = [hit for hit in hits if hit is not None]
        return max(present, key=lambda hit: hit[2]) if present else None

    def commit_prepared(self, rec_id, detail):
        """只接纳到尝试增量。@param rec_id 记录身份。@param detail 完整特征。"""
        super().commit_prepared(rec_id, detail)
        self.accepted[rec_id] = detail

    def add_vector(self, rec_id, cluster_key, vec):
        """保存可直接提交的向量。@param rec_id 身份。@param cluster_key 簇键。@param vec 向量。"""
        super().add_vector(rec_id, cluster_key, vec)
        self.vectors[rec_id] = (cluster_key, tuple(vec))


class _SessionStage(DedupStage):
    """复用普通去重判决链，将簇和错误的尝试归属显式化。"""

    def __init__(self, owner: DedupStage):
        """绑定正式簇集合与空增量。@param owner 正式阶段。"""
        super().__init__(owner.cfg, _SessionIndex(owner))
        self.owner = owner

    def _count_cluster(self, cluster_key, ctx):
        """正式簇只查询，新增簇只记本尝试。@param cluster_key 簇键。@param ctx 上下文。"""
        if cluster_key not in self.owner._counted_clusters:
            super()._count_cluster(cluster_key, ctx)

    def _settle_preparation_errors(self, errors, ctx):
        """先收齐同步容量问题，再允许错误产品或向量派发。@param errors 声明序错误。@param ctx 上下文。"""
        fatal = next((error for _, error in errors if isinstance(error, ProviderFatalError)), None)
        if fatal is not None:
            _log.error("session dedup preparation contains a provider fatal error: profile=%s", fatal.profile)
            raise fatal
        failures = tuple(failure for item, error in errors
                         for failure in capacity_failures(ctx, (capacity_target(item),), error, "sequence"))
        raise_session_capacities(ctx, failures)
        super()._settle_preparation_errors(errors, ctx)

    async def _run_embeddings(self, prepared, ctx):
        """收齐整波向量失败后一次上抛容量集合。@param prepared 声明序计划。@param ctx 会话上下文。@return 正常结果集。"""
        outcomes = await super()._run_embeddings(prepared, ctx)
        fatal = next((outcome.error for outcome in outcomes.values()
                      if isinstance(outcome.error, ProviderFatalError)), None)
        if fatal is not None:
            _log.error("session embedding wave contains a provider fatal error: profile=%s", fatal.profile)
            self._record_embedding_failures(outcomes, ctx)
            raise fatal
        failures = tuple(failure for value in prepared if value.ordinal in outcomes
                         for failure in capacity_failures(
                             ctx, (capacity_target(value.item),), outcomes[value.ordinal].error, "sequence"))
        if failures:
            self._record_embedding_failures(outcomes, ctx)
            raise_session_capacities(ctx, failures)
        return outcomes

    def _fail_item(self, item, exc, ctx):
        """容量错误先交协调器，致命错误保持控制流。@param item 信封。@param exc 错误。@param ctx 上下文。"""
        if isinstance(exc, ProviderFatalError):
            _log.error("session dedup preparation encountered a provider fatal error: profile=%s", exc.profile)
            raise exc
        raise_session_capacity(ctx, (capacity_target(item),), exc, "sequence")
        super()._fail_item(item, exc, ctx)

    def _reduce_one(self, prepared, outcome, ctx):
        """在正式判决前处理该声明序的实际容量失败。@param prepared 计划。@param outcome 结果。@param ctx 上下文。"""
        if outcome is not None and outcome.error is not None:
            if isinstance(outcome.error, ProviderFatalError):
                _log.error("session dedup reduction encountered a provider fatal error: profile=%s",
                           outcome.error.profile)
                raise outcome.error
            raise_session_capacity(ctx, (capacity_target(prepared.item),), outcome.error, "sequence")
        super()._reduce_one(prepared, outcome, ctx)


class SessionDedupReservation:
    """单一会话尝试的去重增量；只能提交或丢弃一次。"""

    def __init__(self, owner: DedupStage, local: _SessionStage):
        """保存正式前缀代次。@param owner 正式阶段。@param local 已运行的增量阶段。"""
        self.owner = owner
        self.local = local
        self.index = owner.index
        self.generation = owner.index._ordinary_generation
        self.consumed = False

    def _validate(self, owner: DedupStage):
        """拒绝重复消费或过期前缀。@param owner 调用方阶段。"""
        if (owner is not self.owner or self.consumed or owner.index is not self.index
                or self.generation != owner.index._ordinary_generation):
            _log.error("session dedup reservation is stale or already consumed")
            raise InternalError("session dedup reservation is stale or already consumed")

    def commit(self, owner: DedupStage):
        """同步提交全部去重接纳身份，后续过滤不追溯修改。@param owner 正式阶段。"""
        self._validate(owner)
        if owner.cfg.scope == "batch":
            owner.index.reset()
        local = self.local.index
        for rec_id, detail in local.accepted.items():
            owner.index.commit_prepared(rec_id, detail)
            if rec_id in local.vectors:
                key, vector = local.vectors[rec_id]
                owner.index.add_vector(rec_id, key, list(vector))
        if local._last_probe is not None:
            owner.index._last_probe = local._last_probe
            owner.index._last_similarity = local._last_similarity
        owner._counted_clusters.update(self.local._counted_clusters)
        self._release()

    def discard(self, owner: DedupStage):
        """丢弃当前增量，正式前缀和簇保持原值。@param owner 正式阶段。"""
        self._validate(owner)
        self._release()

    def _release(self):
        """消费后立即释放增量特征。@return 无。"""
        self.consumed = True
        self.local = None


async def reserve_session(owner, batch, ctx) -> SessionDedupReservation:
    """运行完整会话去重但暂不提交。@param owner 正式阶段。@param batch 尝试信封。@param ctx 上下文。"""
    local = _SessionStage(owner)
    reservation = SessionDedupReservation(owner, local)
    await local.run(batch, ctx)
    return reservation


def preview_capacity(stage, item, ctx) -> SessionCapacityFailure | None:
    """检查完整语义嵌入输入，不查正式索引。@param stage 阶段。@param item 候选序列。@param ctx 上下文。"""
    if not stage.cfg.semantic:
        return None
    detail = stage.index.prepare(item.record)
    if not stage._semantic_participates(detail):
        return None
    profile = ctx.cfg.embedding_profiles[stage.cfg.semantic_embedding]
    if budget.est_text(detail.dedup_text) <= budget.embed_budget(profile):
        return None
    error = ContextOverflowError(
        "complete sequence embedding input exceeds context capacity",
        phase="precheck", profile=stage.cfg.semantic_embedding,
    )
    return SessionCapacityFailure(ctx.session_attempt.stage, (capacity_target(item),), "sequence", error)

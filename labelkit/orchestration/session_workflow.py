"""普通离线会话的一次上游冻结与有限下游重算。"""
from __future__ import annotations

import time
import logging
from dataclasses import asdict, replace

from labelkit.common.contracts.sequence_capacity import SessionAttemptScope
from labelkit.common.errors import InternalError, SessionCapacityError
from labelkit.orchestration.session_capacity import SessionPartition, project_terminals, validate_session

_log = logging.getLogger(__name__)


class SessionCapacityChecker:
    """组合已经启用的真实下游请求构造器，不额外发送模型请求。"""

    def __init__(self, stages):
        """冻结预览顺序。@param stages 下游规范链。"""
        self.stages = tuple(stages)

    def preview(self, item, ctx):
        """逐阶段纯预览完整证据。@param item 候选序列。@param ctx 上游上下文。@return 首个容量问题。"""
        for stage in self.stages:
            scope = replace(ctx.session_attempt, stage=stage.name)
            failure = stage.preview_capacity(item, replace(ctx, session_attempt=scope))
            if failure is not None:
                return failure
        return None


class SessionWorkflow:
    """当前会话的暂存边界；完成提交后不保留任何会话数据。"""

    def __init__(self, workflow, session_id: str, ordinal: int):
        """绑定既有工作流服务。@param workflow 进程编排器。@param session_id 完整会话。@param ordinal 声明序。"""
        self.workflow = workflow
        self.session_id = session_id
        self.ordinal = ordinal
        chain = workflow._compose_chain(include_generate=False)
        self.upstream = tuple(stage for stage in chain if stage.name in ("segment", "stitch"))
        self.downstream = tuple(stage for stage in chain if stage.name not in ("segment", "stitch"))
        self.checker = SessionCapacityChecker(self.downstream)
        self.terminals = ()
        self.reservation = None
        self.dedup = next((stage for stage in self.downstream if stage.name == "dedup"), None)

    def _context(self, stage: str, attempt: int):
        """同会话同阶段复用随机种子，任务身份区分尝试。@param stage 阶段。@param attempt 尝试序。@return 上下文。"""
        ctx = self.workflow._make_ctx(self.ordinal, stage)
        ctx.session_attempt = SessionAttemptScope(self.session_id, self.ordinal, attempt, stage, self.terminals)
        ctx.capacity_checker = self.checker
        ctx.task_namespace = f"{ctx.task_namespace}:session-attempt:{attempt}"
        return ctx

    async def run(self, frames):
        """冻结一次上游，再尝试到分区或最小失败收敛。@param frames 已声明出现位置的完整会话。"""
        metrics = self.workflow.metrics
        started = time.perf_counter()
        metrics.observe_session_frames(len(frames))
        metrics.event("batch.start", stage="run", batch_no=self.ordinal, payload={"size": len(frames)})
        with metrics.session_attempt(self.session_id, 0), metrics.capture_counts() as upstream_counts:
            initial = len(frames)
            await self._chain(frames, self.upstream, 0)
            episodes = len(frames) - initial
        partition = SessionPartition(frames, initial)
        frames.clear()
        attempt = 1
        while True:
            batch = partition.fresh()
            with metrics.session_attempt(self.session_id, attempt):
                try:
                    with metrics.capture_counts() as counts:
                        fanout = await self._chain(batch, self.downstream, attempt)
                        validate_session(batch)
                except SessionCapacityError as exc:
                    self._discard()
                    self._advance(partition, exc.failures)
                    attempt += 1
                    continue
                except BaseException:
                    self._discard()
                    raise
                self._commit(batch, (upstream_counts, counts), (fanout, episodes + partition.net_episodes), started)
                return

    async def _chain(self, batch, stages, attempt):
        """每阶段完整规划后归并，特殊去重只产生待提交增量。@param batch 信封。@param stages 链。@param attempt 尝试序。"""
        fanout = 0
        for stage in stages:
            ctx = self._context(stage.name, attempt)
            project_terminals(batch, ctx)
            before = len(batch)
            await self._stage(stage, batch, ctx)
            if stage.name == "classify":
                fanout += len(batch) - before
        return fanout

    async def _stage(self, stage, batch, ctx):
        """复用唯一执行域和阶段耗时观测。@param stage 阶段。@param batch 信封。@param ctx 上下文。"""
        workflow = self.workflow
        workflow.metrics.stage_begin(stage.name, self.ordinal)
        started = time.perf_counter()
        try:
            if stage.name == "dedup":
                self.reservation = await stage.reserve_session(batch, ctx)
            else:
                await stage.run(batch, ctx)
        finally:
            elapsed = time.perf_counter() - started
            workflow._stage_time[stage.name] = workflow._stage_time.get(stage.name, 0.0) + elapsed
            workflow.metrics.add_stage_time(stage.name, elapsed)

    def _discard(self):
        """丢弃未提交的去重增量。@return 无。"""
        if self.reservation is not None:
            self.dedup.discard_session(self.reservation)
            self.reservation = None

    def _advance(self, partition, failures):
        """按声明序消费整波失败，只启动一次重算。@param partition 冻结分区。@param failures 全部已完成请求失败。"""
        metrics = self.workflow.metrics
        pending = tuple(failures)
        progress = False
        for failure in pending:
            failure.error.__traceback__ = None
            failure.error.__context__ = None
            failure.error.__cause__ = None
        while pending:
            failure, *tail = pending
            pending = tuple(tail)
            if not _request_current(partition, failure):
                continue
            if any(_failure_key(known) == _failure_key(failure) for known in self.terminals):
                continue
            children = self._advance_one(partition, failure)
            progress = True
            if children is not None:
                pending = _project_failure_targets(pending, children)
        if not progress:
            _log.error("capacity wave made no partition or terminal progress")
            raise InternalError("capacity wave made no partition or terminal progress")
        metrics.count("capacity.recomputations")
        self._event("recompute", failures[0])

    def _advance_one(self, partition, failure):
        """建立一个仍可达请求的切点或最小失败。@param partition 冻结分区。@param failure 当前容量问题。@return 子分区。"""
        metrics = self.workflow.metrics
        children = partition.split(failure)
        if children is None:
            self.terminals += (failure,)
            metrics.count("capacity.minimum_failures")
            self._event("minimum_failure", failure)
        else:
            metrics.count("capacity.splits")
            metrics.count("capacity.sealed", 2)
            self._event("split", failure, children)
            self._event("seal", failure, children)
            self.terminals = _project_failure_targets(self.terminals, children)
        return children

    def _event(self, action, failure, children=None):
        """写入可审计出现位置与切点。@param action 转换。@param failure 请求归属。@param children 实际切分结果。"""
        payload = {"action": action, "unit": failure.unit,
                   "profile": failure.error.profile, "phase": failure.error.phase,
                   "targets": [asdict(target) for target in failure.targets]}
        if children is not None:
            payload["cut"] = asdict(children[0].capacity.bounds.after)
        self.workflow.metrics.event(
            "sequence.capacity", stage=failure.stage, batch_no=self.ordinal,
            record_ids=tuple(target.record_id for target in failure.targets),
            payload=payload,
        )

    def _commit(self, batch, captured, deltas, started):
        """最终尝试同步提交后落盘，以写后状态计数。@param batch 产品。@param captured 暂存计数。@param deltas 净增量。@param started 起点。"""
        workflow = self.workflow
        if self.reservation is not None:
            self.dedup.commit_session(self.reservation)
            self.reservation = None
        for counters in captured:
            workflow.metrics.merge_counts(counters)
        workflow.metrics.count("counts.fanout", deltas[0])
        workflow.metrics.count("counts.episodes", deltas[1])
        if workflow.cfg.quality.enabled:
            workflow._collect_quality_stats(batch)
        emitted = workflow.emitter.emit_batch(batch, self.ordinal)
        workflow._output_lines += emitted.emitted
        workflow._rejects_lines += emitted.rejected
        tally = workflow._tally_statuses(batch, emitted)
        workflow.metrics.event("batch.end", stage="run", batch_no=self.ordinal,
                               payload=workflow._batch_end_payload(tally, deltas, started))
        workflow.metrics.flush()


def _project_failure_targets(failures, children):
    """保留仍可达的固定、帧及相邻对请求，丢弃已替换的大序列请求。@param failures 已知失败。@param children 新分区。"""
    projected = []
    parent_id = children[0].capacity.parent_id
    for failure in failures:
        changed = any(target.record_id == parent_id for target in failure.targets)
        if changed and failure.unit in ("sequence", "pairwise"):
            continue
        targets = []
        for target in failure.targets:
            if target.record_id != parent_id:
                targets.append(target)
                continue
            for child in children:
                bounds = child.capacity.bounds
                positions = child.member_positions if failure.unit == "fixed" else target.member_positions
                if positions and all(bounds.lower <= position < bounds.upper for position in positions):
                    targets.append(replace(target, record_id=child.record.id, member_positions=positions))
        if targets:
            projected.append(replace(failure, targets=tuple(targets)))
    return tuple(projected)


def _request_current(partition, failure):
    """检查请求的全部目标仍属于当前基线，禁止把过时父项误判最小单位。@param partition 分区。@param failure 请求。"""
    items = {item.record.id: item for item in partition.items if item.record.kind == "sequence"
             and item.status == "active"}
    for target in failure.targets:
        item = items.get(target.record_id)
        if item is None or item.capacity.root_id != target.root_id:
            return False
    return bool(failure.targets)


def _failure_key(failure):
    """获取单调最小失败身份，不把异常对象身份当成进度。@param failure 容量事实。@return 稳定身份。"""
    return failure.stage, failure.error.profile, failure.unit, failure.targets

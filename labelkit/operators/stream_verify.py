"""普通 stream verify 的严格波次驱动器（v1.19）。"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Mapping

from labelkit.common.contracts.execution import TaskGroupRequest, TaskSpec
from labelkit.common.errors import ContextOverflowError, InternalError, SessionCapacityError
from labelkit.common.contracts.sequence_capacity import (
    capacity_failures, capacity_target, member_key, raise_session_capacities,
)
from labelkit.common.inference.sequence_evidence import terminal_error
from labelkit.operators.verify_capacity import (
    allows_position, boundary_suspicion, check_review, check_reviews, current_seams, review_request,
    plan_seams, project_fragments, restore_items, snapshot_items, working_failures, working_item,
)
from labelkit.common.contracts.types import (
    Annotation,
    PipelineItem,
    Record,
    Status,
    Transition,
    VerificationResult,
)
from labelkit.operators.verify import (
    _BIG_THREE,
    _ATTEMPT_MODE,
    _COUNTER_BOUNDARY_FLAGS,
    _COUNTER_DEFECTS_PREFIX,
    _COUNTER_MEMBERSHIP_REPAIRS,
    _DEFAULT_FAIL_DEFECT,
    _EpisodeReview,
    _MISSING_KINDS,
    _RECLAIM_RELATIONS,
    _ReclaimClaim,
    _RoutingScope,
    _VerdictEvent,
    VerifyPromptOptions,
    _critique_entries,
    _feed_reactive_terminal,
    _qualifies_for_reclaim,
    _session_frame_envelopes,
    boundary_margin_text,
    boundary_frames,
    fragment_structure_text,
    majority_verdict,
    normalize_defects,
    render_critiques_text,
)

if TYPE_CHECKING:
    from labelkit.common.inference.llm_client import PromptBundle
    from labelkit.common.contracts.stage import RunContext
    from labelkit.operators.verify import VerifyStage


_log = logging.getLogger("labelkit.verify.stream")


@dataclass(frozen=True)
class _StreamReviewPlan:
    """一个 episode 在当前 round 的共享 panel 计划。"""

    state: _EpisodeReview
    prompt: "PromptBundle"
    schema: Mapping
    judges: tuple[str, ...]


@dataclass(frozen=True)
class _ClaimOutcome:
    """一个成员回收复判叶的冻结结果。"""

    relation: str       # 候选成员的关系判定
    boundary: object    # segment 纯叶返回的完整窗口裁决


@dataclass(frozen=True)
class _FrameClassifyJob:
    """一个帧分类补跑任务的冻结输入。"""

    state: _EpisodeReview  # 所属 episode 台账
    member: Record         # 待补分类的成员
    position: int          # 当前成员的明确出现位置。
    ordinal: int           # 本波次声明序
    plan: object           # classify 冻结窗口计划


@dataclass(frozen=True)
class _FrameAnnotateJob:
    """一个出现位置的帧标注补跑任务。"""

    state: _EpisodeReview  # 所属工作序列。
    member: Record         # 完整成员证据。
    position: int          # 会话内出现位置。
    label: str | None      # 当前帧分类视图。


@dataclass(frozen=True)
class _EnvelopeSnapshot:
    """成员修复前的信封状态快照。"""

    envelope: PipelineItem       # 被改写的成员信封
    status: Status               # 修复前的状态
    had_attribution: bool        # 修复前是否存在归因标位
    attribution: object | None   # 修复前的归因值


async def _capture_leaf(operation: Callable[[], Awaitable[object]]) -> object:
    """把 ordinary 记录级失败收敛为纯 outcome，控制流异常原样上抛。

    @param operation 不写共享业务状态的异步叶调用
    @return 调用结果或 ordinary 记录级异常
    """
    try:
        return await operation()
    except _BIG_THREE:
        raise
    except Exception as exc:
        _log.error("stream verify leaf failed: kind=%s", type(exc).__name__)
        return exc


def _propagate_attempt_internal(outcome: object) -> None:
    """让 sequence attempt 中的程序错误越过普通记录隔离边界。

    @param outcome 叶任务结果或被捕获的异常。
    @raises InternalError attempt 模式中的内部错误原样抛出。
    """
    if _ATTEMPT_MODE.get() and isinstance(outcome, InternalError):
        raise outcome


async def _claim_call(state: _EpisodeReview, claim: _ReclaimClaim, ctx: "RunContext") -> _ClaimOutcome:
    """执行不发事件的成员回收窗口复判。

    @param claim 回收预定
    @param state 当前完整工作序列台账。
    @param ctx 运行上下文
    @return 候选关系与待归并的窗口裁决
    """
    from labelkit.operators.segment import _call_window

    target = capacity_target(working_item(state), claim.window_positions)
    known = terminal_error(ctx, target, "transition", ctx.cfg.segment.llm)
    if known is not None:
        raise known
    boundary = await _call_window(
        claim.window, ctx, span=(0, len(claim.window)),
    )
    return _ClaimOutcome(_claim_relation(boundary.verdicts, claim), boundary)


def _claim_relation(verdicts: tuple[str, ...], claim: _ReclaimClaim) -> str:
    """从窗口裁决中读取候选成员的关系。

    @param verdicts 与复判窗口对齐的关系表
    @param claim 回收预定
    @return 候选成员关系
    """
    return verdicts[claim.candidate_index]


def _commit_claim(outcome: _ClaimOutcome, ctx: "RunContext") -> None:
    """按声明序提交成员回收窗口的业务事件。

    @param outcome 纯叶冻结结果
    @param ctx 运行上下文
    """
    from labelkit.operators.segment import _emit_boundary

    _emit_boundary(ctx, outcome.boundary, session_id=None)


async def _reseam_call(prev: Record, curr: Record, index: int,
                       ctx: "RunContext", label: str | None) -> object:
    """执行不提交指标或事件的接缝重抽。

    @param prev 前一成员
    @param curr 后一成员
    @param index 重建后的步骤序号
    @param ctx 运行上下文
    @param label episode 分类标签
    @return extract 纯叶冻结结果
    """
    from labelkit.operators.extract import _extract_transition_outcome

    return await _extract_transition_outcome(prev, curr, index, ctx, label)


def _commit_reseam(outcome: object, ctx: "RunContext") -> Transition:
    """按声明序提交接缝重抽事实并取出步骤。

    @param outcome extract 纯叶冻结结果
    @param ctx 运行上下文
    @return 重抽步骤
    """
    from labelkit.operators.extract import _commit_extract_outcome

    _commit_extract_outcome(outcome, ctx)
    return outcome.transition


async def _frame_classify_call(plan: object, ctx: "RunContext") -> tuple[object, ...]:
    """执行一个成员的纯帧分类计划。

    @param plan classify 冻结窗口计划
    @param ctx 运行上下文
    @return 与原始窗口声明序一致的纯结果
    """
    from labelkit.operators.classify import _run_frame_plan

    outcomes = []
    for span in plan.spans:
        outcomes.append(await _run_frame_plan(plan, span, ctx))
    return tuple(outcomes)


def _commit_frame_classify(plan: object, outcomes: tuple[object, ...],
                           ctx: "RunContext") -> dict[str, object]:
    """按声明序归并一个成员的帧分类结果。

    @param plan classify 冻结窗口计划
    @param outcomes 与原始窗口声明序一致的纯结果
    @param ctx 运行上下文
    @return 成员分类表
    """
    from labelkit.operators.classify import _reduce_frame_plan

    result, _calls, _fallback = _reduce_frame_plan(plan, outcomes, ctx)
    return result


class StreamVerifyDriver:
    """普通 stream episode 的评审、手术与重标注驱动器。"""

    def __init__(self, stage: "VerifyStage"):
        """绑定 classic verify 核心。

        @param stage 共享提示词、评审归并与记录失败策略的 verify 阶段
        """
        self._stage = stage
        self._repair_snapshots: dict[int, dict[int, _EnvelopeSnapshot]] = {}
        self._repair_counts: dict[int, int] = {}

    async def run(self, batch: list[PipelineItem],
                  episodes: list[PipelineItem], ctx: "RunContext") -> None:
        """流式驱动器主循环：并发评审 → 同步路由手术 → 并发修复 → 下一轮复评。
        @param batch 本批信封列表（邻域与成员信封来源）
        @param episodes 本批待评审的序列信封（批位序）
        @param ctx 运行上下文
        """
        snapshots = snapshot_items(batch)
        try:
            await self._run_rounds(batch, episodes, ctx)
        except SessionCapacityError:
            restore_items(batch, snapshots)
            self._repair_snapshots.clear()
            self._repair_counts.clear()
            raise

    async def _run_rounds(self, batch: list[PipelineItem],
                          episodes: list[PipelineItem], ctx: "RunContext") -> None:
        """执行完整会话评审轮。@param batch 会话帧。@param episodes 序列。@param ctx 当前上下文。"""
        states = [_EpisodeReview(item, ordinal) for ordinal, item in enumerate(episodes)]
        pending = states
        while pending:
            reviewed = await self._review_round(pending, batch, ctx)        # (a)
            self._repair_snapshots.clear()
            self._repair_counts.clear()
            finalize, routed = self._route_round(reviewed, batch, ctx)      # (b)
            await self._resolve_claims(routed, ctx)                         # (c)
            repairing: list[_EpisodeReview] = []
            for state in routed:
                if state.surgical or state.needs_reannotate:
                    repairing.append(state)
                else:
                    finalize.append(state)   # 无可修复项——fail 结论维持
            next_pending = await self._repair_wave(repairing, batch, ctx)
            for state in finalize:
                self._finalize_episode(state, ctx)
            dependents = self._seam_dependents(states, batch, ctx)
            refreshed = await self._repair_wave(dependents, batch, ctx)
            pending_ids = {id(state) for state in next_pending + refreshed}
            pending = [state for state in states if id(state) in pending_ids and state.item.status == "active"]

    async def _repair_wave(self, repairing: list[_EpisodeReview], batch: list[PipelineItem],
                           ctx: "RunContext") -> list[_EpisodeReview]:
        """复用同一修复波次处理成员手术与接缝依赖，不改变已消耗评审轮数。

        @param repairing 当前修复台账。
        @param batch 完整会话信封。
        @param ctx 当前会话上下文。
        @return 已成功重标注并等待复评的台账。
        """
        plan_seams(repairing, batch)
        dead = await self._reseam_episodes(repairing, ctx)
        for state in repairing:
            if id(state) in dead or not state.surgical:
                continue
            try:
                self._rebuild_episode(state)
            except Exception as exc:
                self._rollback_repair(state)
                _log.error("stream verify rebuild failed: kind=%s", type(exc).__name__)
                raise
            self._commit_repair(state, ctx)
        dead |= await self._sync_frame_products(repairing, dead, ctx)
        return await self._reannotate_round([state for state in repairing if id(state) not in dead], ctx)

    def _seam_dependents(self, states: list[_EpisodeReview], batch: list[PipelineItem],
                         ctx: "RunContext") -> list[_EpisodeReview]:
        """成员手术已成功或回滚后，以最终归属找出失效的评审结果。

        @param states 全会话既有评审台账。
        @param batch 当前已提交成员状态。
        @param ctx 当前上下文。
        @return 仍有修复预算的接缝依赖台账。
        """
        dirty = []
        for state in states:
            item = state.item
            if item.status != "active" or not hasattr(item, "stitch_task_name"):
                continue
            previous = dict(zip(getattr(item, "seam_indexes", ()), getattr(item, "seam_interrupted_by", ())))
            if previous == current_seams(item, batch):
                continue
            if state.rounds > self._stage.cfg.verify.max_repair_rounds:
                _log.error("stream verify seam dependency repair budget exhausted")
                state.verdict = "fail"
                state.defects = [{**_DEFAULT_FAIL_DEFECT, "detail": "Seam dependency repair budget exhausted."}]
                self._finalize_episode(state, ctx)
                continue
            state.begin_round()
            state.surgical = True
            state.needs_reannotate = True
            state.fail_critiques = [{"aspect": "sequence evidence", "opinion": "Rebuild the changed seam evidence."}]
            dirty.append(state)
        return dirty

    async def _review_round(self, pending: list[_EpisodeReview],
                            batch: list[PipelineItem],
                            ctx: "RunContext") -> list[_EpisodeReview]:
        """执行 episode 与 judge 笛卡尔积的纯评审波次并按输入序归并。

        @param pending 本轮待评审台账
        @param batch 本批信封列表
        @param ctx 运行上下文
        @return 评审成功台账
        """
        plans = self._plan_review_wave(pending, batch, ctx)
        if not plans:
            return []
        specs = self._review_specs(plans, ctx)
        outcomes = await ctx.run_group(TaskGroupRequest(specs))
        return self._reduce_review_wave(plans, outcomes, ctx)

    def _plan_review_wave(
            self, pending: list[_EpisodeReview], batch: list[PipelineItem],
            ctx: "RunContext") -> list[_StreamReviewPlan]:
        """按 episode 输入序冻结 prompt、Schema 与 panel。

        @param pending 本轮待评审台账
        @param batch 本批信封列表
        @param ctx 运行上下文
        @return 可执行的 episode 计划
        """
        plans: list[_StreamReviewPlan] = []
        errors = []
        for state in pending:
            try:
                plans.append(self._plan_episode_review(state, batch, ctx))
            except _BIG_THREE:
                raise
            except Exception as exc:
                errors.append((state, exc))
        failures = tuple(failure for state, error in errors for failure in working_failures(state, error, ctx))
        raise_session_capacities(ctx, failures)
        for state, error in errors:
            self._stage._fail_item(state.item, error, ctx)
        return plans

    def _plan_episode_review(
            self, state: _EpisodeReview, batch: list[PipelineItem],
            ctx: "RunContext") -> _StreamReviewPlan:
        """冻结一个 episode 当前轮的共享 panel 输入。

        @param state 当前 episode 台账
        @param batch 本批信封列表
        @param ctx 运行上下文
        @return 当前轮评审计划
        """
        from labelkit.common.inference.schema_engine import defect_verdict_schema

        margin = boundary_margin_text(state.item, batch)
        structure = fragment_structure_text(state.item) if self._stage.cfg.stitch.enabled else ""
        options = VerifyPromptOptions(label=state.label, transitions=state.item.transitions,
                                      boundary_margin=margin, fragment_structure=structure,
                                      member_positions=state.item.member_positions,
                                      boundary_records=tuple((frame.session_position, frame.record) for frame in
                                                             boundary_frames(state.item, batch)))
        judges, _multi = self._stage._judge_panel()
        requests = [review_request(self._stage, state.item, ctx, options, judge) for judge in judges]
        check_reviews(state.item, requests, ctx)
        prompt, schema = requests[0].prompt, requests[0].schema
        return _StreamReviewPlan(state, prompt, schema, tuple(judges))

    def _review_specs(
            self, plans: list[_StreamReviewPlan], ctx: "RunContext",
            ) -> tuple[TaskSpec[object], ...]:
        """把 episode 与 judge 展开为输入序任务。

        @param plans episode 输入序计划
        @param ctx 运行上下文
        @return 扁平任务 tuple
        """
        specs: list[TaskSpec[object]] = []
        for plan in plans:
            for judge_ordinal, judge in enumerate(plan.judges):
                round_no = plan.state.rounds + 1
                specs.append(TaskSpec(
                    task_id=(f"{ctx.task_namespace}:verify:stream:review:"
                             f"{round_no}:{plan.state.ordinal}:{judge_ordinal}"),
                    declaration_key=(ctx.batch_no, 8, round_no, 0,
                                     plan.state.ordinal, judge_ordinal),
                    stage=self._stage.name,
                    resource_key=("llm", judge),
                    operation=lambda plan=plan, judge=judge: self._review_leaf(
                        plan, judge, ctx),
                ))
        return tuple(specs)

    async def _review_leaf(self, plan: _StreamReviewPlan, judge: str,
                           ctx: "RunContext") -> object:
        """执行一个不写业务状态的 episode judge 叶。

        @param plan 当前 episode 共享计划
        @param judge 当前评委 profile
        @param ctx 运行上下文
        @return 成功四元组或 ordinary 记录级异常
        """
        from labelkit.common.inference.schema_engine import CallScope

        async def call() -> object:
            """@return 当前评委的结构化结果四元组。"""
            return await ctx.schema_engine.complete_validated(
                judge, plan.prompt, schema=plan.schema,
                scope=CallScope(
                    record_ids=(plan.state.item.record.id,),
                    batch_no=ctx.batch_no,
                    complete_evidence=ctx.session_attempt is not None,
                ),
            )

        return await _capture_leaf(call)

    def _reduce_review_wave(
            self, plans: list[_StreamReviewPlan], outcomes: tuple[object, ...],
            ctx: "RunContext") -> list[_EpisodeReview]:
        """按 episode 与 judge ordinal 提交 verdict、缺陷与事件。

        @param plans episode 输入序计划
        @param outcomes TaskExecutor 输入序结果
        @param ctx 运行上下文
        @return 评审成功台账
        """
        offset = 0
        failures = []
        for plan in plans:
            for outcome in outcomes[offset:offset + len(plan.judges)]:
                failures.extend(working_failures(plan.state, outcome, ctx))
            offset += len(plan.judges)
        raise_session_capacities(ctx, failures)
        reviewed: list[_EpisodeReview] = []
        offset = 0
        for plan in plans:
            selected = list(outcomes[offset:offset + len(plan.judges)])
            offset += len(plan.judges)
            try:
                result = self._fold_defect_round(plan.state, selected, ctx)
            except _BIG_THREE:
                raise
            except Exception as exc:
                self._stage._fail_item(plan.state.item, exc, ctx)
                continue
            self._commit_review(plan.state, result, ctx)
            reviewed.append(plan.state)
        return reviewed

    @staticmethod
    def _commit_review(state: _EpisodeReview, result: tuple,
                       ctx: "RunContext") -> None:
        """把一个 episode 的 panel 结果按输入序写入私有台账。

        @param state 当前 episode 台账
        @param result 归并后的 verdict、意见与缺陷
        @param ctx 运行上下文
        """
        verdict, merged, failed, defects = result
        state.rounds += 1
        state.critiques.extend(merged)
        state.verdict = verdict
        state.fail_critiques = failed
        state.defects = defects
        for defect in defects:
            ctx.metrics.count(f"{_COUNTER_DEFECTS_PREFIX}{defect['kind']}")

    def _route_round(self, reviewed: list[_EpisodeReview], batch: list[PipelineItem],
                     ctx: "RunContext") -> tuple[list[_EpisodeReview],
                                                 list[_EpisodeReview]]:
        """(b) 同步缺陷路由与成员手术，严格按批位序（"先到"被定死成"位序先到"，S8）。
        @param reviewed 评审成功的台账
        @param batch 本批信封列表
        @param ctx 运行上下文
        @return (进入终审的台账, 已路由待修复的台账)
        """
        vcfg = self._stage.cfg.verify
        finalize: list[_EpisodeReview] = []
        routed: list[_EpisodeReview] = []
        claimed: set[int] = set()      # 本轮已预定的噪声信封 id()
        for state in reviewed:
            state.begin_round()
            self._normalize_capacity_defects(state, ctx)
            if state.verdict == "pass":
                finalize.append(state)
                continue
            repairs_done = state.rounds - 1
            if vcfg.policy != "repair" or repairs_done >= vcfg.max_repair_rounds:
                finalize.append(state)   # 预算/策略：fail 结论维持（现行语义）
                continue
            self._route_defects(state, batch, claimed, ctx)
            routed.append(state)
        return finalize, routed

    @staticmethod
    def _normalize_capacity_defects(state: _EpisodeReview, ctx: "RunContext") -> None:
        """准确命中人工边界的疑点保留审计，但不独立造成失败。

        @param state 当前评审台账。
        @param ctx 当前上下文。
        """
        for index, defect in enumerate(state.defects):
            if boundary_suspicion(state.item, defect):
                state.defects[index] = {**defect, "suspected": "capacity"}
                ctx.metrics.count(_COUNTER_BOUNDARY_FLAGS)
        if state.defects and all(defect.get("suspected") == "capacity" for defect in state.defects):
            state.verdict = "pass"

    async def _resolve_claims(self, routed: list[_EpisodeReview],
                              ctx: "RunContext") -> None:
        """经 segment 完整窗口并发复判回收候选，先传播容量信号，再按声明序提交。
        普通复判失败仅保留疑点；容量失败由会话控制器重算，本阶段回滚全部认领。
        @param routed 已路由待修复的台账
        @param ctx 运行上下文
        @raises BaseException 大三样原样上抛
        """
        claims = [(state, claim) for state in routed for claim in state.claims]
        if not claims:
            return
        specs = tuple(
            TaskSpec(
                task_id=(f"{ctx.task_namespace}:verify:stream:claim:"
                         f"{state.rounds}:{state.ordinal}:{claim_ordinal}"),
                declaration_key=(ctx.batch_no, 8, state.rounds, 1,
                                 state.ordinal, claim_ordinal),
                stage=self._stage.name,
                resource_key=("llm", self._stage.cfg.segment.llm),
                operation=lambda state=state, claim=claim: _capture_leaf(
                    lambda: _claim_call(state, claim, ctx)),
            )
            for claim_ordinal, (state, claim) in enumerate(claims)
        )
        outcomes = await ctx.run_group(TaskGroupRequest(specs))
        failures = tuple(failure for (state, claim), outcome in zip(claims, outcomes)
                         for failure in working_failures(state, outcome, ctx, "transition", claim.window_positions))
        raise_session_capacities(ctx, failures)
        for (state, claim), outcome in zip(claims, outcomes):
            if isinstance(outcome, BaseException):
                _feed_reactive_terminal(outcome, ctx.metrics)
                ctx.metrics.count(_COUNTER_BOUNDARY_FLAGS)
                continue
            _commit_claim(outcome, ctx)
            if outcome.relation in _RECLAIM_RELATIONS:
                self._apply_reclaim(state, claim)
            else:
                ctx.metrics.count(_COUNTER_BOUNDARY_FLAGS)

    async def _reannotate_round(self, jobs: list[_EpisodeReview],
                                ctx: "RunContext") -> list[_EpisodeReview]:
        """(f) 并发重标注手术过/判 label_mismatch 的 episode。
        @param jobs 待重标注的台账
        @param ctx 运行上下文
        @return 进入下一轮复评的台账
        @raises BaseException 大三样原样上抛
        """
        next_pending: list[_EpisodeReview] = []
        if not jobs:
            return next_pending
        specs = tuple(
            TaskSpec(
                task_id=(f"{ctx.task_namespace}:verify:stream:reannotate:"
                         f"{state.rounds}:{state.ordinal}"),
                declaration_key=(ctx.batch_no, 8, state.rounds, 5,
                                 state.ordinal),
                stage=self._stage.name,
                resource_key=("llm", self._stage.cfg.annotate.llm),
                operation=lambda state=state: _capture_leaf(
                    lambda: self._reannotate_episode(state, ctx)),
            )
            for state in jobs
        )
        outcomes = await ctx.run_group(TaskGroupRequest(specs))
        failures = tuple(failure for state, outcome in zip(jobs, outcomes)
                         for failure in working_failures(state, outcome, ctx))
        raise_session_capacities(ctx, failures)
        for state, outcome in zip(jobs, outcomes):
            if isinstance(outcome, BaseException):
                _propagate_attempt_internal(outcome)
                self._stage._fail_item(state.item, outcome, ctx)
                continue
            state.item.annotation = outcome
            next_pending.append(state)
        return next_pending

    def _fold_defect_round(self, state: _EpisodeReview, results: list,
                           ctx: "RunContext") -> tuple[str, list[dict],
                                                       list[dict], list[dict]]:
        """折叠缺陷词表形的一轮结果：意见照旧收集，缺陷取判 fail 者的并集并按 S31 确定性规范化；
        终局判 fail 而缺陷表为空时补一条默认 label_mismatch（S7）。
        @param state episode 台账
        @param results 与面板同序的结果列表
        @param ctx 运行上下文
        @return (面板结论, 合并意见, 判 fail 意见, 规范化缺陷表)
        """
        judges, multi = self._stage._judge_panel()
        record = state.item.record
        round_no = state.rounds + 1
        merged: list[dict] = []
        fail_critiques: list[dict] = []
        verdicts: list[str] = []
        defects_union: list[Mapping] = []
        for judge, result in zip(judges, results):
            if isinstance(result, BaseException):
                entry = self._stage._judge_error_entry(result, judge, multi)
                verdicts.append("fail")
                merged.append(entry)
                fail_critiques.append(entry)
                continue
            obj, _usage, _attempts, _model = result
            verdict = obj["verdict"]
            verdicts.append(verdict)
            entries = _critique_entries(obj["critiques"], judge if multi else None)
            merged.extend(entries)
            if verdict == "fail":
                fail_critiques.extend(entries)
                defects_union.extend(obj["defects"])
            self._stage._emit_verdict_event(
                _VerdictEvent(record=record, verdict=verdict, round_no=round_no,
                              critiques=obj["critiques"],
                              judge=judge if multi else None, label=state.label,
                              defects=obj["defects"]), ctx)
        final = majority_verdict(verdicts)
        defects = normalize_defects(defects_union)
        if final == "fail" and not defects:
            defects = [dict(_DEFAULT_FAIL_DEFECT)]
        return final, merged, fail_critiques, defects

    # ── (b) 缺陷路由（同步；收缩就地执行，回收预定留给 claim 波次）──

    def _route_defects(self, state: _EpisodeReview, batch: list[PipelineItem],
                       claimed: set[int], ctx: "RunContext") -> None:
        """建立本 episode 的路由作用域，并逐条路由缺陷表。
        @param state episode 台账
        @param batch 本批信封列表
        @param claimed 本轮已预定的噪声信封 id()（跨 episode 共享，位序优先）
        @param ctx 运行上下文
        """
        item = state.item
        frames = _session_frame_envelopes(batch, item.session_id)
        # S8：多标签扇出的克隆兄弟（classification.label 不是命中集首项）永不执行成员手术
        # ——共享的成员帧属于原信封。
        classification = item.classification
        scope = _RoutingScope(
            frames=frames, claimed=claimed,
            clone=bool(classification is not None and classification.labels
                       and classification.label != classification.labels[0]))
        for idx in range(len(state.defects)):
            self._route_one_defect(state, idx, scope, ctx)

    def _route_one_defect(self, state: _EpisodeReview, idx: int,
                          scope: _RoutingScope, ctx: "RunContext") -> None:
        """单条缺陷的路由：重标注 / 仅标记 / 收缩 / 三级回收判定。
        @param state episode 台账
        @param idx 缺陷在 state.defects 中的下标（就地写回 suspected 标记）
        @param scope 批级路由作用域
        @param ctx 运行上下文
        """
        defect = state.defects[idx]
        kind = defect["kind"]
        if defect.get("suspected") == "capacity":
            return
        if kind == "label_mismatch":
            state.needs_reannotate = True
            return
        if kind == "wrong_stitch":
            # v1.9（T15）：独立的仅标记 + fail 分支——不存在拆缝手术（§4 非目标 4），缺陷留在表
            # 里、不触发任何修复动作、fail 结论维持；它不在 _MISSING_KINDS 里，绝不进回收扫描。
            return
        if scope.clone:
            # 仅标记降级；missing_* 计作边界判定，off_task 收缩降级不计数（只统计边界类缺陷）。
            if kind in _MISSING_KINDS:
                ctx.metrics.count(_COUNTER_BOUNDARY_FLAGS)
            return
        if kind == "off_task_members":
            self._shrink_off_task(state, defect, scope)
            return
        # missing_head / missing_tail / missing_members——三级回收判定（噪声池 → 邻段 → 无处可寻）。
        found = self._find_reclaim_candidate(kind, defect, state, scope)
        if isinstance(found, _ReclaimClaim):
            scope.claimed.add(id(found.envelope))
            state.claims.append(found)
        elif found == "neighbor":
            # 相邻帧已被别的 episode 吸收：仅标记，绝不跨段抢帧（S8）。
            ctx.metrics.count(_COUNTER_BOUNDARY_FLAGS)
        else:
            state.defects[idx] = {**defect, "suspected": "capture_gap"}
            ctx.metrics.count(_COUNTER_BOUNDARY_FLAGS)

    def _shrink_off_task(self, state: _EpisodeReview, defect: Mapping,
                         scope: _RoutingScope) -> None:
        """off_task_members 收缩：把被指认的成员帧移出段并翻成 dropped_noise（②b M7 豁免）；无可
        指认对象、或 judge 点名了每个成员（空 episode 不可存在，改由 fail 结论整段丢弃）时不动手。
        @param state episode 台账
        @param defect 缺陷条目
        @param scope 批级路由作用域
        """
        named = set(defect.get("members") or ())
        shrink_ids = set(state.working_positions) & named
        if not shrink_ids or len(shrink_ids) == len(state.working_members):
            return
        kept = [(position, member) for position, member in zip(state.working_positions, state.working_members)
                if position not in shrink_ids]
        state.working_positions = [position for position, _ in kept]
        state.working_members = [member for _, member in kept]
        for frame in scope.frames:
            if frame.status == "absorbed" and frame.session_position in shrink_ids:
                self._remember_envelope(state, frame)
                frame.status = "dropped_noise"
                frame.noise_attribution = ("verify", "off_task_member")  # type: ignore[attr-defined]
        state.surgical = True
        self._record_repair(state)

    def _find_reclaim_candidate(self, kind: str, defect: Mapping,
                                state: _EpisodeReview,
                                scope: _RoutingScope) -> "_ReclaimClaim | str | None":
        """在缺陷邻域内确定性地找回收候选（批位序）：head = 段首成员之前一帧，tail = 段尾成员之后
        一帧（连续性——跨过非成员帧回收会打洞），members = 段内首个内部噪声帧（judge 点名时限定
        在 defect.members 内）。
        @param kind 缺陷种类（missing_head / missing_tail / missing_members）
        @param defect 缺陷条目
        @param state episode 台账
        @param scope 批级路由作用域
        @return 回收预定 / "neighbor"（帧被别的 episode 持有）/ None（无候选）
        """
        member_positions = state.working_positions
        if not member_positions:
            return None
        head, tail = member_positions[0], member_positions[-1]
        if kind == "missing_head":
            return self._edge_claim(state, scope, head - 1)
        if kind == "missing_tail":
            return self._edge_claim(state, scope, tail + 1)
        return self._interior_claim(state, scope, defect, (head, tail))

    def _edge_claim(self, state: _EpisodeReview, scope: _RoutingScope,
                    position: int) -> "_ReclaimClaim | str | None":
        """段首/段尾外的单帧回收判定。
        @param state episode 台账
        @param scope 批级路由作用域
        @param position 候选帧的会话内批位序
        @return 回收预定 / "neighbor" / None
        """
        frames = scope.frames
        if not allows_position(state.item, position) or not 0 <= position < len(frames):
            return None
        frame = frames[position]
        if _qualifies_for_reclaim(frame, scope.claimed):
            return self._make_claim(state, frame, position)
        if frame.status == "absorbed" or id(frame) in scope.claimed:
            # 被别的 episode 持有——要么已被吸收，要么在本同步段里被更早的 episode 预定
            # （位序优先，S8）：属二级"neighbor"，不是采集空洞（D5）。
            return "neighbor"
        return None

    def _interior_claim(self, state: _EpisodeReview, scope: _RoutingScope,
                        defect: Mapping,
                        span: tuple[int, int]) -> "_ReclaimClaim | str | None":
        """段内部（首末成员之间）的缺失成员回收判定。
        @param state episode 台账
        @param scope 批级路由作用域
        @param defect 缺陷条目（members 点名时作为筛选）
        @param span (段首成员位序, 段尾成员位序)
        @return 回收预定 / "neighbor"（本轮被更早 episode 预定）/ None
        """
        head, tail = span
        named = set(defect.get("members") or ())
        contended = False
        for pos in range(head + 1, tail):
            if not allows_position(state.item, pos):
                continue
            frame = scope.frames[pos]
            if not _qualifies_for_reclaim(frame, scope.claimed):
                if id(frame) in scope.claimed:
                    contended = True       # 本轮被更早的 episode 预定走了
                continue
            if named and frame.session_position not in named:
                continue
            return self._make_claim(state, frame, pos)
        return "neighbor" if contended else None

    @staticmethod
    def _make_claim(state: _EpisodeReview, frame: PipelineItem,
                    position: int) -> _ReclaimClaim:
        """按候选帧的会话位置取 [前成员, 候选, 后成员] 复判窗口（边缘候选无前/后成员）。
        @param state episode 台账
        @param frame 候选噪声帧信封
        @param position 候选帧的会话内批位序
        @return 回收预定
        """
        if not allows_position(state.item, position):
            _log.error("stream verify claim violates capacity bounds")
            raise InternalError("stream verify claim violates capacity bounds")
        entries = list(zip(state.working_positions, state.working_members, strict=True))
        previous = next((entry for entry in reversed(entries) if entry[0] < position), None)
        following = next((entry for entry in entries if entry[0] > position), None)
        selected = ([previous] if previous is not None else []) + [(position, frame.record)]
        candidate_index = len(selected) - 1
        if following is not None:
            selected.append(following)
        return _ReclaimClaim(frame, position, [member for _, member in selected], candidate_index,
                             tuple(pos for pos, _ in selected))

    def _apply_reclaim(self, state: _EpisodeReview, claim: _ReclaimClaim) -> None:
        """回收通过：噪声信封翻回 absorbed（②b M7 豁免——绝不翻回 active），记录按批位序插回。
        @param state episode 台账
        @param claim 回收预定
        """
        if not allows_position(state.item, claim.position):
            _log.error("stream verify claim commit violates capacity bounds")
            raise InternalError("stream verify claim commit violates capacity bounds")
        self._remember_envelope(state, claim.envelope)
        claim.envelope.status = "absorbed"
        insert_at = sum(position < claim.position for position in state.working_positions)
        state.working_members.insert(insert_at, claim.envelope.record)
        state.working_positions.insert(insert_at, claim.position)
        state.surgical = True
        self._record_repair(state)

    def _remember_envelope(self, state: _EpisodeReview,
                           envelope: PipelineItem) -> None:
        """首次改写前保存成员信封状态。

        @param state episode 台账
        @param envelope 待改写的成员信封
        """
        snapshots = self._repair_snapshots.setdefault(id(state), {})
        if id(envelope) in snapshots:
            return
        snapshots[id(envelope)] = _EnvelopeSnapshot(
            envelope=envelope,
            status=envelope.status,
            had_attribution=hasattr(envelope, "noise_attribution"),
            attribution=getattr(envelope, "noise_attribution", None),
        )

    def _record_repair(self, state: _EpisodeReview) -> None:
        """在重建成功前暂存成员手术计数。

        @param state episode 台账
        """
        state_id = id(state)
        self._repair_counts[state_id] = self._repair_counts.get(state_id, 0) + 1

    def _rollback_repair(self, state: _EpisodeReview) -> None:
        """回滚未通过 reseam 与重建屏障的成员信封改写。

        @param state episode 台账
        """
        for snapshot in self._repair_snapshots.pop(id(state), {}).values():
            snapshot.envelope.status = snapshot.status
            if snapshot.had_attribution:
                snapshot.envelope.noise_attribution = snapshot.attribution  # type: ignore[attr-defined]
            elif hasattr(snapshot.envelope, "noise_attribution"):
                delattr(snapshot.envelope, "noise_attribution")
        self._repair_counts.pop(id(state), None)

    def _commit_repair(self, state: _EpisodeReview, ctx: "RunContext") -> None:
        """重建成功后提交成员手术计数并丢弃回滚快照。

        @param state episode 台账
        @param ctx 运行上下文
        """
        state_id = id(state)
        count = self._repair_counts.pop(state_id, 0)
        if count:
            ctx.metrics.count(_COUNTER_MEMBERSHIP_REPAIRS, count)
        self._repair_snapshots.pop(state_id, None)

    # ── (d)/(e) 接缝重抽 + 重建 ───────────────────────────────────────────

    def _affected_pairs(self,
                        state: _EpisodeReview) -> list[tuple[int, Record, Record]]:
        """手术前成员表里不存在的重建相邻对——需重抽接缝的触点（每次手术 1–2 处）。
        @param state episode 台账
        @return [(重建后的步序号, 左成员, 右成员)]
        """
        old_adjacent = set(zip(state.orig_positions, state.orig_positions[1:]))
        old_seams = {tuple(state.orig_positions[index:index + 2])
                     for index in getattr(state.item, "seam_indexes", ())}
        return [(index, state.working_members[index], state.working_members[index + 1])
                for index, pair in enumerate(zip(state.working_positions, state.working_positions[1:]))
                if (pair not in old_adjacent or pair in old_seams) and index not in state.seams]

    async def _reseam_episodes(self, repairing: list[_EpisodeReview],
                               ctx: "RunContext") -> set[int]:
        """经 M15 公开直调面并发重抽接缝（CONTRACTS §7.15；仅 extract.enabled）。
        @param repairing 本轮待修复的台账
        @param ctx 运行上下文
        @return 因重抽出错而阵亡的台账 id() 集合（记录级隔离）
        @raises BaseException 大三样原样上抛
        """
        jobs = [(state, j, a, b) for state in repairing
                if state.surgical and self._stage.cfg.extract.enabled and state.item.transitions is not None
                for j, a, b in self._affected_pairs(state)]
        dead: set[int] = set()
        if not jobs:
            return dead
        specs = tuple(
            TaskSpec(
                task_id=(f"{ctx.task_namespace}:verify:stream:reseam:"
                         f"{state.rounds}:{state.ordinal}:{job_ordinal}"),
                declaration_key=(ctx.batch_no, 8, state.rounds, 2,
                                 state.ordinal, job_ordinal),
                stage=self._stage.name,
                resource_key=("llm", self._stage.cfg.extract.llm),
                operation=lambda state=state, j=j, a=a, b=b: _capture_leaf(
                    lambda: _reseam_call(a, b, j, ctx, state.label)),
            )
            for job_ordinal, (state, j, a, b) in enumerate(jobs)
        )
        try:
            outcomes = await ctx.run_group(TaskGroupRequest(specs))
        except _BIG_THREE as exc:
            for state in repairing:
                self._rollback_repair(state)
            _log.error("stream verify reseam wave aborted: kind=%s", type(exc).__name__)
            raise
        failures = tuple(failure for (state, j, _a, _b), outcome in zip(jobs, outcomes)
                         for failure in working_failures(state, outcome, ctx, "transition",
                                                          state.working_positions[j:j + 2]))
        raise_session_capacities(ctx, failures)
        for (state, j, _a, _b), outcome in zip(jobs, outcomes):
            if isinstance(outcome, BaseException):
                if id(state) not in dead:
                    dead.add(id(state))
                    self._rollback_repair(state)
                    self._stage._fail_item(state.item, outcome, ctx)
                continue
            state.reseams[j] = _commit_reseam(outcome, ctx)
        return dead

    def _rebuild_episode(self, state: _EpisodeReview) -> None:
        """全部收缩/回收完成后的同步重建：新成员元组 + 全量重编号的步表。序列 id 永不重算（spec
        3.14.4）；未触碰的步保留原 Transition 只改写 index，手术触点换成新抽取结果并把
        {"reseamed": True} 并入 detail。不变式：len(transitions) == len(members) − 1。
        @param state episode 台账
        """
        item = state.item
        if any(not allows_position(item, position) for position in state.working_positions):
            _log.error("stream verify rebuild violates capacity bounds")
            raise InternalError("stream verify rebuild violates capacity bounds")
        new_members = tuple(state.working_members)
        new_record = dataclasses.replace(item.record, members=new_members)
        new_transitions = item.transitions
        if item.transitions is not None:
            old_by_pair = {
                pair: transition
                for pair, transition in zip(zip(state.orig_positions, state.orig_positions[1:]),
                                            item.transitions)
            }
            rebuilt: list[Transition] = []
            for j, (a, b) in enumerate(zip(new_members, new_members[1:])):
                if j in state.seams:
                    from labelkit.operators.extract import _seam_placeholder
                    rebuilt.append(_seam_placeholder(j, state.seams[j]))
                elif j in state.reseams:
                    fresh = state.reseams[j]
                    rebuilt.append(dataclasses.replace(
                        fresh, index=j,
                        detail={**dict(fresh.detail), "reseamed": True}))
                else:
                    rebuilt.append(dataclasses.replace(old_by_pair[tuple(state.working_positions[j:j + 2])],
                                                       index=j))
            new_transitions = tuple(rebuilt)
        item.record = new_record
        item.member_positions = tuple(state.working_positions)
        item.transitions = new_transitions
        project_fragments(item, state.orig_positions)
        item.seam_indexes = tuple(state.seams)
        item.seam_interrupted_by = tuple(state.seams.values())
        item.stream_repaired = True  # type: ignore[attr-defined]  # → _meta.stream.repaired

    # ── (e2) v1.12 帧产物同步（SPEC-frame-annotation §3.4 手术同步）──────────

    async def _sync_frame_products(self, repairing: list[_EpisodeReview],
                                   dead: set[int], ctx: "RunContext") -> set[int]:
        """帧产物同步：先收缩删键，再回收补跑，且只由拥有成员的首标签写共享产物。
        克隆可因接缝依赖重建步骤，但不能按自己的旧成员视图删除或补跑原信封的帧产物。
        @param repairing 本轮待修复的台账
        @param dead 先前阶段已阵亡的台账 id()
        @param ctx 运行上下文
        @return 本阶段新阵亡的台账 id() 集合
        """
        synced = [state for state in repairing
                  if id(state) not in dead and state.surgical
                  and (state.item.classification is None
                       or state.item.classification.label == state.item.classification.labels[0])]
        for state in synced:
            self._shrink_frame_products(state.item)
        newly_dead = await self._backfill_frame_classify(synced, ctx)
        newly_dead |= await self._backfill_frame_annotate(
            [state for state in synced if id(state) not in newly_dead], ctx)
        return newly_dead

    @staticmethod
    def _shrink_frame_products(item: PipelineItem) -> None:
        """收缩同步：成员手术后不再属于当前序列的出现位置从两个帧产物 dict 中删键（含值为
        None 的 failed 占位键，不留无主条目）。仅当对应 dict 非 None 时操作（dict None = 帧 pass
        未运行：降格会话/帧粒度关闭/非首标签，语义必须保持）；dict 对象本身从不更换——扇出克隆
        按引用共享同一 dict 的前提。
        @param item 手术后的序列信封
        """
        kept = set(item.member_positions)
        for products in (item.member_classifications, item.member_annotations):
            if products is None:
                continue
            for member_id in [k for k in products if k not in kept]:
                del products[member_id]

    async def _backfill_frame_classify(self, states: list[_EpisodeReview],
                                       ctx: "RunContext") -> set[int]:
        """回收补跑·帧分类：冻结单成员计划，并发执行纯窗口叶，最后按声明序归并。

        @param states 已完成收缩删键的台账
        @param ctx 运行上下文
        @return 本步新阵亡的台账 id() 集合
        @raises BaseException 大三样原样上抛
        """
        jobs, dead = self._plan_frame_classify_jobs(states, ctx)
        if not jobs:
            return dead
        specs = tuple(
            TaskSpec(
                task_id=(f"{ctx.task_namespace}:verify:stream:frame-classify:"
                         f"{job.state.rounds}:{job.state.ordinal}:{job.ordinal}"),
                declaration_key=(ctx.batch_no, 8, job.state.rounds, 3,
                                 job.state.ordinal, job.ordinal),
                stage=self._stage.name,
                resource_key=("llm", self._stage.cfg.frame_classify.llm),
                operation=lambda job=job: _capture_leaf(
                    lambda: _frame_classify_call(job.plan, ctx)),
            )
            for job in jobs
        )
        outcomes = await ctx.run_group(TaskGroupRequest(specs))
        self._reduce_frame_classify(jobs, outcomes, dead, ctx)
        return dead

    def _plan_frame_classify_jobs(
            self, states: list[_EpisodeReview],
            ctx: "RunContext") -> tuple[list[_FrameClassifyJob], set[int]]:
        """按 episode 与成员输入序冻结帧分类计划。

        @param states 已完成收缩删键的台账
        @param ctx 运行上下文
        @return (可执行任务, 计划失败台账 id 集合)
        """
        from labelkit.operators.classify import _plan_frame_episode

        jobs: list[_FrameClassifyJob] = []
        dead: set[int] = set()
        errors = []
        ordinal = 0
        if not self._stage.cfg.frame_classify.enabled:
            return jobs, dead
        for state in states:
            classifications = state.item.member_classifications
            if classifications is None:
                continue
            for index, member in enumerate(state.item.record.members):
                position = member_key(state.item, index)
                if position in classifications:
                    continue
                target = capacity_target(state.item, (position,))
                try:
                    plan = _plan_frame_episode((member,), ctx, member.id, ordinal, target)
                except _BIG_THREE:
                    raise
                except Exception as exc:
                    errors.append((state, target, exc))
                    continue
                jobs.append(_FrameClassifyJob(state, member, position, ordinal, plan))
                ordinal += 1
        failures = tuple(failure for _state, target, error in errors
                         for failure in capacity_failures(ctx, (target,), error, "frame"))
        raise_session_capacities(ctx, failures)
        for state, _target, error in errors:
            dead.add(id(state))
            self._stage._fail_item(state.item, error, ctx)
        return [job for job in jobs if id(job.state) not in dead], dead

    def _reduce_frame_classify(
            self, jobs: list[_FrameClassifyJob], outcomes: tuple[object, ...],
            dead: set[int], ctx: "RunContext") -> None:
        """按任务输入序提交帧分类结果。

        @param jobs 帧分类任务输入序
        @param outcomes TaskExecutor 输入序结果
        @param dead 已阵亡台账 id 集合
        @param ctx 运行上下文
        """
        from labelkit.operators.classify_capacity import frame_plan_failures

        failures = []
        for job, outcome in zip(jobs, outcomes):
            if isinstance(outcome, BaseException):
                failures.extend(capacity_failures(ctx, (job.plan.target,), outcome, "frame"))
            else:
                failures.extend(frame_plan_failures(job.plan, outcome, ctx))
        raise_session_capacities(ctx, failures)
        for job, outcome in zip(jobs, outcomes):
            state = job.state
            if id(state) in dead:
                continue
            if isinstance(outcome, BaseException):
                if id(state) not in dead:
                    dead.add(id(state))
                    self._stage._fail_item(state.item, outcome, ctx)
                continue
            result = _commit_frame_classify(job.plan, outcome, ctx)
            state.item.member_classifications[job.position] = result[job.position]

    async def _backfill_frame_annotate(self, states: list[_EpisodeReview],
                                       ctx: "RunContext") -> set[int]:
        """按明确成员位置补跑帧标注，再按声明序提交产物。

        @param states 已完成帧分类的工作台账。
        @param ctx 当前会话上下文。
        @return 普通帧失败沿原 None 产物语义，不额外失败整个序列。
        """
        from labelkit.operators.annotate import _record_member_failure

        jobs = [job for state in states for job in self._frame_annotate_jobs(state, ctx)]
        specs = tuple(TaskSpec(
            task_id=f"{ctx.task_namespace}:verify:stream:frame-annotate:{job.state.rounds}:{ordinal}",
            declaration_key=(ctx.batch_no, 8, job.state.rounds, 4, job.state.ordinal, ordinal),
            stage=self._stage.name, resource_key=("llm", self._stage.cfg.frame_annotate.llm),
            operation=lambda job=job: _capture_leaf(lambda: self._frame_annotation_leaf(job, ctx)),
        ) for ordinal, job in enumerate(jobs))
        outcomes = await ctx.run_group(TaskGroupRequest(specs))
        failures = []
        for job, outcome in zip(jobs, outcomes, strict=True):
            target = dataclasses.replace(capacity_target(job.state.item, (job.position,)), label=job.label)
            failures.extend(capacity_failures(ctx, (target,), outcome, "frame"))
        raise_session_capacities(ctx, failures)
        for job, outcome in zip(jobs, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                _propagate_attempt_internal(outcome)
                _record_member_failure(job.member, ctx, outcome)
                job.state.item.member_annotations[job.position] = None
            else:
                job.state.item.member_annotations[job.position] = outcome
        return set()

    @staticmethod
    async def _frame_annotation_leaf(job: _FrameAnnotateJob, ctx: "RunContext") -> Annotation:
        """传递完整工作序列和帧类视图。@param job 单帧工单。@param ctx 上下文。@return 标注产物。"""
        from labelkit.operators.annotate import annotate_member_leaf

        target = dataclasses.replace(capacity_target(job.state.item, (job.position,)), label=job.label)
        return await annotate_member_leaf(job.member, ctx, job.label, target)

    def _frame_annotate_jobs(self, state: _EpisodeReview, ctx: "RunContext") -> list[_FrameAnnotateJob]:
        """按明确成员键选择尚未标注且类别启用的帧。

        @param state 当前工作序列。
        @param ctx 帧分类视图和计数来源。
        @return 按出现位置排列的补标工单。
        """
        item = state.item
        if not self._stage.cfg.frame_annotate.enabled or item.member_annotations is None:
            return []
        jobs = []
        for index, member in enumerate(item.record.members):
            position = member_key(item, index)
            if position in item.member_annotations:
                continue
            classification = (item.member_classifications or {}).get(position)
            label = classification.label if classification is not None else None
            view = self._stage.cfg.frame_class_views.get(label) if label is not None else None
            if view is not None and not view.enabled:
                ctx.metrics.count("frame_annotate.skipped")
                continue
            jobs.append(_FrameAnnotateJob(state, member, position, label))
        return jobs

    # ── (f) 重标注 + 终审 ─────────────────────────────────────────────────

    async def _reannotate_episode(self, state: _EpisodeReview,
                                  ctx: "RunContext") -> Annotation:
        """以全部工作成员、图片、动作与当前批评重标注，实际请求完整检查容量。
        @param state episode 台账
        @param ctx 运行上下文
        @return 重标注结果
        """
        from labelkit.operators.annotate import (
            AnnotatePromptOptions,
            RepairContext,
            annotate_record_leaf,
        )

        repair = RepairContext(
            previous_output=state.item.annotation.output,
            critiques_text=render_critiques_text(state.fail_critiques),
        )
        opts = AnnotatePromptOptions(repair=repair, label=state.label,
                                     transitions=state.item.transitions,
                                     temporal_context=state.item.temporal_context)
        from labelkit.operators.annotate import class_schema_text
        from labelkit.operators.annotate_capacity import sequence_request

        request = sequence_request(state.item.record, ctx, class_schema_text(ctx, state.label), opts)
        check_review(state.item, request, ctx)
        return await annotate_record_leaf(state.item.record, ctx, opts)

    def _finalize_episode(self, state: _EpisodeReview, ctx: "RunContext") -> None:
        """终审落地：写 VerificationResult，判 fail 即 dropped_verify。verify.defects.<kind> 在
        评审时计数（D4），不在这里——被修掉的缺陷也必须进报告直方图。
        @param state episode 台账
        @param ctx 运行上下文（保留签名一致性，终审本身不计数）
        """
        item = state.item
        item.verification = VerificationResult(
            verdict=state.verdict, rounds=state.rounds,
            critiques=tuple(state.critiques), defects=tuple(state.defects))
        if state.verdict == "fail":
            item.status = "dropped_verify"

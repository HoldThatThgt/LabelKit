"""M15 extract 阶段（spec 3.15，CONTRACTS.md §7.15）。

对序列信封（episode）做动作摘取：每对相邻成员 ⟨s_i, s_{i+1}⟩ 经 LLM 得一个结构化
动作——确定性提示词装配（CONTRACTS §10.10：两张截图 + 可选的树变更证据 + 恒在的
帧摘要尾部）、M8 内部 schema 保证（schema_engine.action_schema——不计 resolved_at、
不过 L2.5），以及 on_error 的 fallback/fail 策略（S16：兜底取证落在
Transition.detail，绝不入 item.errors——R4 家族）。步骤数恒等于成员数 − 1（失败的
步骤变成兜底占位，绝不留洞）。v1.9（T10/T20）：落在 M16 ``seam_indexes`` 上的相邻
对永不进 LLM——它们取机械的线索接缝占位（detail.kind == "thread_seam"）且**不进**
extract 计数器；相邻救援的拼接处是真实步骤，照常摘取。链位：classify → extract →
quality（标签已就位，故 [class.<label>.extract] 的按类指令得以生效）；multi 扇出的
兄弟信封各自按自己的标签独立摘取（S9——绝不按 record id 去重）。仅 UI 模态序列
（由 M1 强制，此处再防御式复核）。

并发：全批所有 episode 的所有步骤冻结为一个 TaskGroupRequest；结果按
（episode 批内位置, 步骤序号）回写——与完成顺序无关、零 rng。

``extract_transition`` 是**公开直调面**：M7 手术后的接缝重摘直接调用它（每次手术
1–2 次调用；stage 本身永不重跑——``transitions is not None`` 即跳过，重入零调用）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

from labelkit.common.contracts.execution import TaskGroupRequest, TaskSpec
from labelkit.common.errors import (
    CircuitBreakerTripped,
    ContextOverflowError,
    ErrorKind,
    OutputTruncatedError,
    ProviderFatalError,
    ProviderRetryableError,
    SchemaViolation,
)
from labelkit.common.contracts.types import (
    PipelineItem,
    Record,
    StageError,
    Transition,
    frame_digest,
    tree_diff,
)
from labelkit.common.inference import budget

from labelkit.common.inference.llm_client import Message, Part, PromptBundle
from labelkit.common.inference.schema_engine import CallScope, action_schema

if TYPE_CHECKING:
    from labelkit.common.config.model import ResolvedConfig
    from labelkit.common.contracts.stage import RunContext


_logger = logging.getLogger("labelkit.extract")

_STAGE_NAME = "extract"

# 事件名（严格取 CONTRACTS.md §7.15 / §8.1 的字面串）。
_EV_STEP = "extract.step"
_EV_ERROR = "error"

# M15 自有的计数器键（CONTRACTS.md §9.3；report.stream.extract）。
_COUNTER_TRANSITIONS = "extract.transitions"        # 全部最终步骤数（含兜底）
_COUNTER_FALLBACK_STEPS = "extract.fallback_steps"
_COUNTER_FAILURES = "extract.failures"              # 失败的 episode 数
_COUNTER_BY_TYPE_PREFIX = "extract.by_type."        # 按 action_type，含兜底的 "other"

# 中文提示词片段——逐字取自 CONTRACTS.md §10.10（spec 3.15.4），包括其中记录在案的
# 换行。词表条目与 OpenCUA 锚定句是冻结模板文本，绝不随配置变化。
_SYSTEM_HEAD = (
    "你是屏幕操作流的动作摘取员。给定同一操作流中相邻的前后两帧屏幕状态，推断用户在两帧之间\n"
    "执行的动作。action_type 只能取以下值：\n"
    "- click / long_press / drag: 点击 / 长按 / 拖拽某控件\n"
    "- input_text: 在输入框键入文本\n"
    "- scroll: 滚动屏幕或列表\n"
    "- open_app: 打开一个应用；app_switch: 切换到另一已打开的应用\n"
    "- navigate_back / navigate_home: 系统返回 / 回到桌面\n"
    "- wait: 无用户交互，仅等待界面加载或变化\n"
    "- other: 无法归入以上任何一类（把语义写进 description）\n"
    "锚定约定：前一帧是动作发生前最后一个稳定状态，后一帧是动作完成后的首个稳定状态；推断\n"
    "二者之间发生的单个语义动作；若变化由多个低层事件构成（连续滚动、连续键入），归并为一个\n"
    "语义动作。"
)
_STRUCTURE_SENTENCE = "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容："
_STRUCTURE_SHAPE = ('{"action_type": <词表值>, "target": <目标控件文本引用或 null>,\n'
                    ' "value": <动作参数或 null>, "description": <一句话动作描述>}')
_LABEL_PREV = "[前一帧截图]"
_LABEL_NEXT = "[后一帧截图]"
_LABEL_DIFF = "[树变更摘要]"
_LABEL_DIGESTS = "[前后帧树摘要]"

# [前后帧树摘要] 尾部的每帧摘要上限。[extract] 表不带摘要键；400 = segment.
# digest_max_chars 的缺省值——与 M4 序列分支为其成员摘要所作的取舍同一口径
# （quality._MEMBER_DIGEST_MAX_CHARS）。
_DIGEST_MAX_CHARS = 400

# 代码侧兜底动作（S16）：与 LLM 确认的 "other" 的区分点，是
# Transition.detail["kind"] == "extraction_invalid" 的在场。
_FALLBACK_ACTION: Mapping = {"action_type": "other", "target": None,
                             "value": None, "description": ""}


def _seam_placeholder(index: int, interrupted_by: tuple[str, ...]) -> Transition:
    """v1.9（T10，四个键钉死）：写在线索接缝步骤上的零 LLM 机械占位。

    action_type="app_switch" **不**承诺任何语义（M-1 备注：同应用交错也落这里）
    ——下游一律以 detail.kind == "thread_seam" 区分。interrupted_by = 打断者线索
    按缺口序去重后的 task_name（M-1 保证 ≥ 1）；model=""/attempts=0 表示从未发生
    过任何调用。

    @param index 步骤序号（相邻对序号）
    @param interrupted_by 打断者线索的 task_name（按缺口序）
    @return 接缝占位步骤记录
    """
    names = "、".join(interrupted_by)
    action = {"action_type": "app_switch", "target": None, "value": None,
              "description": f"线索接缝：被{names}打断后恢复"}
    return Transition(index=index, action=action, model="", attempts=0,
                      detail={"kind": "thread_seam",
                              "interrupted_by": list(interrupted_by)})


def _diff_text(diff: Mapping) -> str:
    """把 tree_diff 映射确定性地文本化为 [树变更摘要]（spec 3.15.4：新增 / 移除 /
    文本变化的节点数、变更比例、应用与标题是否变化）。

    与 M14 的 §10.9 [帧 {i} 变更] 渲染同一固定形态——算子模块之间从不相互依赖
    （spec §2.2），故这是 M15 自己的一份共享格式副本（quality / annotate 的步骤行
    先例）。

    @param diff tree_diff 产出的差异映射
    @return [树变更摘要] 的正文文本
    """
    text = (f"新增 {diff['added']} 节点，移除 {diff['removed']} 节点，"
            f"文本变化 {diff['text_changed']} 处，"
            f"变更比例 {diff['change_ratio']:.0%}")
    if diff["app_changed"]:
        text += "，应用切换"
    if diff["title_changed"]:
        text += "，标题变化"
    return text


def build_extract_prompt(prev: Record, curr: Record, cfg: "ResolvedConfig",
                         label: str | None) -> PromptBundle:
    """§10.10 模板的确定性装配。

    system：摘取指令 + 11 值词表条目 + OpenCUA 锚定句 + 可选的指令行（label 非
    None ⇒ 取 class_views[label].extract 的生效值，这是唯一进白名单的按类键；为空
    时整行省略）+ 结构句与结构形。
    user：**一条**消息、五个部件——文本 [前一帧截图]、图像 s_i、文本 [后一帧截图]、
    图像 s_{i+1}，以及恒在的收尾文本部件：[树变更摘要] 行（仅 include_diff=true；
    结构化树差异，S14）加上**恒在**的 [前后帧树摘要] 行（S6：末部件必为文本）。
    tree_diff 的量化复用 dedup.bounds_quantize_px——配置里唯一的坐标量化概念，
    吸收的正是它本就为之而生的采集侧 bounds 抖动（spec §4.3）。

    @param prev 前一帧记录
    @param curr 后一帧记录
    @param cfg 本次运行的解析配置
    @param label 序列级标签；非 None 时取按类摘取指令
    @return 装配好的提示词包
    """
    ecfg = cfg.class_views[label].extract if label is not None else cfg.extract
    lines = [_SYSTEM_HEAD]
    if ecfg.instruction:
        lines.append(ecfg.instruction)
    lines.append(_STRUCTURE_SENTENCE)
    lines.append(_STRUCTURE_SHAPE)
    system = Message(role="system", parts=(Part(kind="text", text="\n".join(lines)),))

    tail_lines: list[str] = []
    if cfg.extract.include_diff:
        diff = tree_diff(prev.ui_tree, curr.ui_tree, cfg.dedup.bounds_quantize_px)
        tail_lines.append(f"{_LABEL_DIFF} {_diff_text(diff)}")
    tail_lines.append(f"{_LABEL_DIGESTS} {frame_digest(prev, _DIGEST_MAX_CHARS)}"
                      f" → {frame_digest(curr, _DIGEST_MAX_CHARS)}")
    user = Message(role="user", parts=(
        Part(kind="text", text=_LABEL_PREV),
        Part(kind="image", image=prev.image),
        Part(kind="text", text=_LABEL_NEXT),
        Part(kind="image", image=curr.image),
        Part(kind="text", text="\n".join(tail_lines)),
    ))
    return PromptBundle(messages=(system, user))


def _feed_reactive_terminal(exc: BaseException, metrics) -> None:
    """A7/§7.8 熔断矩阵：只有 reactive-400（响应体嗅探）溢出终局喂致命连续计数。

    每个异常对象恰喂一次（duck 标位守住重复喂给）；precheck 与 200 形态的 finish
    判据永不喂——后者搭乘的本就是一次成功的 HTTP 交互。

    @param exc 待结算的异常对象
    @param metrics 本次运行的 M12 计数汇（MetricsSink）
    @return 无（副作用为 record_provider_result）
    """
    if (isinstance(exc, ContextOverflowError) and exc.phase == "reactive"
            and getattr(exc, "origin", "http_400") == "http_400"
            and not getattr(exc, "_breaker_fed", False)):
        exc._breaker_fed = True  # type: ignore[attr-defined]
        metrics.record_provider_result(fatal=True)


@dataclass(frozen=True, slots=True)
class _FallbackFact:
    """待归并屏障提交的兜底计数与错误事件事实。"""

    ids: tuple[str, str] # 前后帧身份
    message: str         # 冻结错误消息


@dataclass(frozen=True, slots=True)
class _ExtractOutcome:
    """一个纯叶调用的冻结摘取结果。"""

    transition: Transition              # 正常或机械兜底步骤
    fallback: _FallbackFact | None      # 仅兜底时在场


@dataclass(frozen=True, slots=True)
class _PairJob:
    """一个相邻成员调用的冻结输入。"""

    prev: Record        # 前一帧
    curr: Record        # 后一帧
    index: int          # episode 内步骤序号
    label: str | None   # 可选分类标签


def _fallback_transition(index: int, ids: tuple[str, str], message: str,
                         attempts: int, batch_no: int) -> _ExtractOutcome:
    """构造不修改共享指标或业务对象的机械兜底结果。

    兜底取证只进 Transition.detail，**绝不**进 item.errors（rejects 归属读的是
    errors[0]，R4）；model="" 表示不存在通过校验的输出——动作是代码侧构造物。

    @param index 步骤序号（相邻对序号）
    @param ids （前一帧 id, 后一帧 id）
    @param message 英文错误消息
    @param attempts 该终局形态下已消耗的调用预算
    @param batch_no 当前批号，只用于无数据日志
    @return 兜底步骤及其待提交事实
    """
    kind = ErrorKind.EXTRACTION_INVALID.value
    _logger.debug("transition extraction fell back: index=%d kind=%s", index, kind,
                  extra={"stage": _STAGE_NAME, "batch": batch_no})
    transition = Transition(
        index=index,
        action=dict(_FALLBACK_ACTION),
        model="",
        attempts=attempts,
        detail={"kind": kind, "message": message},
    )
    return _ExtractOutcome(transition=transition,
                           fallback=_FallbackFact(ids=ids, message=message))


async def extract_transition(prev: Record, curr: Record, index: int,
                             ctx: "RunContext", label: str | None = None) -> Transition:
    """一个步骤一次调用——经 complete_validated(schema=action_schema())。

    修复穷尽遵循 extract.on_error（S16）："fallback"（缺省）返回代码侧兜底
    Transition（action_type="other"、detail.kind="extraction_invalid"），外加
    extract.fallback_steps 计数与 error trace 事件，绝不入 item.errors（R4）；
    "fail" 原样上抛，由 stage 层置 episode failed。provider / 内部错误恒向调用方
    传播。后校验归一：scroll 的方向值在代码侧转小写（spec 3.15.4 字段语义表）。
    公开直调面：M7 手术后的接缝重摘直接调用本函数（CONTRACTS §7.15）。

    @param prev 前一帧记录
    @param curr 后一帧记录
    @param index 步骤序号（相邻对序号）
    @param ctx 本次（批次, 阶段）运行上下文
    @param label 序列级标签；非 None 时取 [class.<label>.extract] 的按类指令
    @return 步骤记录（正常摘取产物，或代码侧兜底占位）
    @raises SchemaViolation on_error="fail" 时的修复穷尽 / 溢出 / 截断原样上抛
    """
    outcome = await _extract_transition_outcome(prev, curr, index, ctx, label)
    _commit_extract_outcome(outcome, ctx)
    return outcome.transition


async def _extract_transition_outcome(prev: Record, curr: Record, index: int,
                                      ctx: "RunContext", label: str | None) -> _ExtractOutcome:
    """执行一次摘取调用并返回不带业务突变的冻结结果。

    @param prev 前一帧记录
    @param curr 后一帧记录
    @param index episode 内步骤序号
    @param ctx 运行上下文
    @param label 可选分类标签
    @return 正常或兜底结果
    """
    cfg = ctx.cfg
    prompt = build_extract_prompt(prev, curr, cfg, label)
    ids = (prev.id, curr.id)
    try:
        obj, _usage, attempts, model = await ctx.schema_engine.complete_validated(
            cfg.extract.llm, prompt, action_schema(),
            scope=CallScope(record_ids=ids, batch_no=ctx.batch_no))
    except SchemaViolation as e:
        if cfg.extract.on_error == "fail":
            raise
        # attempts=1+L3 预算：反映已耗尽的调用预算。
        return _fallback_transition(index, ids, str(e),
                                    1 + cfg.output.max_repair_attempts, ctx.batch_no)
    except (ContextOverflowError, OutputTruncatedError) as e:
        # v1.11（spec 3.15.4「上下文预算」行）：恒定的 2 帧 / 2 图调用无物可缩——
        # 无装填、无降级面，由 M9 咽喉 / finish 处置兜底（V16）；溢出与截断原样
        # 搭乘**既有**的机械兜底语义（detail.kind 驱动下游的（摘取兜底）后缀与
        # report 归属，S16），"fail" 则上抛、由 stage 分类器记精确 kind（V27①）。
        if cfg.extract.on_error == "fail":
            raise
        # A7 终局结算：兜底**就是**本次调用的终局（无降级面），reactive-400 在此
        # 恰喂一次熔断。attempts=1：溢出 / 截断在任何 L3 修复轮开跑之前就终结了
        # 本次调用（V11/V25①——绝非"已修复"）。
        _feed_reactive_terminal(e, ctx.metrics)
        return _fallback_transition(index, ids, str(e), 1, ctx.batch_no)
    if obj.get("action_type") == "scroll" and isinstance(obj.get("value"), str):
        obj = {**obj, "value": obj["value"].lower()}
    transition = Transition(index=index, action=obj, model=model, attempts=attempts,
                            detail={})
    return _ExtractOutcome(transition=transition, fallback=None)


def _commit_extract_outcome(outcome: _ExtractOutcome, ctx: "RunContext") -> None:
    """按声明序提交一个叶结果携带的兜底事实。

    @param outcome 冻结摘取结果
    @param ctx 运行上下文
    """
    fallback = outcome.fallback
    if fallback is None:
        return
    ctx.metrics.count(_COUNTER_FALLBACK_STEPS)
    ctx.metrics.event(
        _EV_ERROR,
        stage=_STAGE_NAME,
        batch_no=ctx.batch_no,
        record_ids=fallback.ids,
        payload={
            "stage": _STAGE_NAME,
            "kind": ErrorKind.EXTRACTION_INVALID.value,
            "message": fallback.message,
            "retryable": False,
        },
    )


def _plan_episode_pairs(todo: list[PipelineItem]) -> tuple[
        list[tuple[PipelineItem, int, frozenset[int]]], list[_PairJob]]:
    """为每个待摘取 episode 排布相邻对，并冻结扁平任务表。

    计划顺序 =（episode 批内位置, 步骤序号），TaskExecutor 按输入序返回，故调用方的回写与调度
    顺序无关。v1.9（T20）：落在接缝序号上的相邻对被**跳过**——它们不进 TaskGroupRequest
    （零 LLM），在收尾阶段取机械的 T10 占位；v1.9 之前"一对一协程"的切片口径正
    因此换成了按 episode 的已判决对数记账。

    @param todo 本批待摘取的序列信封（按批内位置序）
    @return 每 episode 的跨度表与扁平相邻对计划
    """
    spans: list[tuple[PipelineItem, int, frozenset[int]]] = []
    jobs: list[_PairJob] = []
    for item in todo:
        members = item.record.members
        label = item.classification.label if item.classification else None
        pairs = max(0, len(members) - 1)
        seams = frozenset(i for i in getattr(item, "seam_indexes", ()) or ()
                          if 0 <= i < pairs)
        spans.append((item, pairs, seams))
        for i in range(pairs):
            if i in seams:
                continue
            jobs.append(_PairJob(members[i], members[i + 1], i, label))
    return spans, jobs


def _finalize_transitions(row: list, pairs: int, seams: frozenset[int],
                          interrupted: tuple[str, ...]) -> tuple[Transition, ...]:
    """把已判决步骤与机械接缝占位按序号拼成完整步骤元组。

    长度恒为 pairs：接缝序号取 T10 占位（其 interrupted_by 按缝序与
    seam_interrupted_by 对齐），其余序号按序取已判决结果。

    @param row 本 episode 的已判决步骤（按步骤序号）
    @param pairs 相邻对总数
    @param seams 接缝序号集合
    @param interrupted 各接缝的打断者线索名（按缝序）
    @return 完整步骤元组（长度 == pairs）
    """
    seam_order = sorted(seams)
    transitions: list[Transition] = []
    judged_iter = iter(row)
    for i in range(pairs):
        if i in seams:
            names = (interrupted[seam_order.index(i)]
                     if seam_order.index(i) < len(interrupted) else ())
            transitions.append(_seam_placeholder(i, tuple(names)))
        else:
            transitions.append(next(judged_iter))
    return tuple(transitions)


class ExtractStage:
    """M15 extract 阶段的 Stage 实现（spec 3.15.2）。

    全批所有 episode 的所有相邻对合进同一个 TaskGroupRequest，随后按批内位置序同步收尾。
    """

    name = "extract"

    def __init__(self, cfg: "ResolvedConfig"):
        """绑定本次运行的解析配置。

        @param cfg 本次运行的不可变解析配置（M1 产物）
        """
        self.cfg = cfg

    async def run(self, batch: list[PipelineItem],
                  ctx: "RunContext") -> list[PipelineItem]:
        """对本批的 active 序列信封做动作摘取。

        @param batch 本批信封列表（唯一可变载体）
        @param ctx 本次（批次, 阶段）运行上下文
        @return 传入的同一列表对象（契约 ②）
        """
        # 选取与幂等（spec 3.15.2）：尚未摘取的 active 序列信封；transitions 非
        # None 即跳过（任何重入零调用——M7 修复改走 extract_transition 直调）。
        # 模态由 M1 保证为 "ui"，此处再防御式复核。multi 扇出的兄弟信封**不**按
        # record id 去重——每个信封按自己的标签独立摘取（S9）。
        todo = [item for item in batch
                if item.status == "active" and item.record.kind == "sequence"
                and item.transitions is None and item.record.modality == "ui"]
        if not todo:
            return batch

        spans, jobs = _plan_episode_pairs(todo)
        results = await self._run_jobs(jobs, ctx)

        # 按批内位置序同步收尾：任一步骤异常外溢的 episode 整体失败（它其余步骤的
        # 结果一并丢弃——步骤元组是全有或全无的不变式）；否则 len(transitions) ==
        # 成员数 − 1，兜底占位与（v1.9）接缝占位按其钉死的序号拼入。
        pos = 0
        for item, pairs, seams in spans:
            judged = pairs - len(seams)
            outcomes = results[pos:pos + judged]
            pos += judged
            exc = next((result for result in outcomes
                        if isinstance(result, BaseException)), None)
            if exc is not None:
                self._fail(item, ctx, exc)
                continue
            for outcome in outcomes:
                if isinstance(outcome, _ExtractOutcome):
                    _commit_extract_outcome(outcome, ctx)
            row = [result.transition for result in outcomes
                   if isinstance(result, _ExtractOutcome)]
            interrupted = tuple(getattr(item, "seam_interrupted_by", ()) or ())
            item.transitions = _finalize_transitions(row, pairs, seams, interrupted)
            self._register(item, ctx)
        return batch                            # 传入的同一列表对象（契约 ②）

    async def _run_jobs(self, jobs: list[_PairJob], ctx: "RunContext") -> tuple[object, ...]:
        """经共享 TaskExecutor 执行全部纯相邻对叶任务。

        @param jobs 输入序冻结任务表
        @param ctx 运行上下文
        @return 输入序摘取结果或记录级异常
        """
        specs = tuple(
            TaskSpec(
                task_id=f"{ctx.task_namespace}:extract:{ordinal}",
                declaration_key=(ctx.batch_no, 4, ordinal),
                stage=self.name,
                resource_key=("llm", self.cfg.extract.llm),
                operation=lambda job=job: self._run_job(job, ctx),
            )
            for ordinal, job in enumerate(jobs)
        )
        return await ctx.tasks.run_group(TaskGroupRequest(specs))

    @staticmethod
    async def _run_job(job: _PairJob, ctx: "RunContext") -> object:
        """把记录级异常收敛为普通 outcome，保留 control 语义。

        @param job 冻结相邻对计划
        @param ctx 运行上下文
        @return 摘取结果或记录级异常
        """
        try:
            return await _extract_transition_outcome(
                job.prev, job.curr, job.index, ctx, job.label,
            )
        except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as exc:  # 记录级隔离，归并屏障负责 item 写入
            _logger.error("extract transition failed: index=%d exc=%s", job.index,
                          type(exc).__name__, extra={"stage": _STAGE_NAME,
                                                    "batch": ctx.batch_no})
            return exc

    def _register(self, item: PipelineItem, ctx: "RunContext") -> None:
        """计数器 + 每个最终步骤一发的 extract.step 事件（兜底步骤也在内，§8.1）。

        extract.transitions / extract.by_type.<action_type> 对**每个**最终步骤都
        计数（兜底落在 by_type.other）。payload 字段原样进——S27 的脱敏
        （_DATA_KEYS/_FREE_TEXT_KEYS）是 M12 的职责。
        v1.9（T20 计数器口径）：线索接缝占位**不是**摘取产物——它既不进计数器
        （其零 LLM 的 app_switch 不得污染 by_type），也不发 extract.step 事件；
        接缝唯一的计量点是 stream.stitch.seams。

        @param item 已完成摘取的序列信封
        @param ctx 本次（批次, 阶段）运行上下文
        @return 无（副作用为计数与事件）
        """
        members = item.record.members
        for t in item.transitions:
            if t.detail.get("kind") == "thread_seam":
                continue
            action_type = t.action["action_type"]
            ctx.metrics.count(_COUNTER_TRANSITIONS)
            ctx.metrics.count(_COUNTER_BY_TYPE_PREFIX + action_type)
            ctx.metrics.event(_EV_STEP, stage=self.name, batch_no=ctx.batch_no,
                              record_ids=(members[t.index].id,
                                          members[t.index + 1].id),
                              payload={"episode_id": item.record.id,
                                       "index": t.index,
                                       "action_type": action_type,
                                       "description": t.action["description"],
                                       "target": t.action["target"],
                                       "value": t.action["value"]})

    def _fail(self, item: PipelineItem, ctx: "RunContext", exc: BaseException) -> None:
        """episode 级失败：on_error="fail" 的修复穷尽，或某步骤调用的任何
        provider / 内部错误。

        kind 的归类与 classify 的失败处置同款；extract 的记录恒为 UI 模态，故
        OSError 即 M9 惰性加载浮上来的图像解码失败。
        v1.11（V27①）：预算词表**先行**——精确取 context_overflow /
        output_truncated（绝不落 internal_error）；溢出拒绝计 budget.
        overflow_records，reactive-400 终局恰喂一次熔断（A7——duck 标位守住重复喂给）。

        @param item 失败的序列信封
        @param ctx 本次（批次, 阶段）运行上下文
        @param exc 步骤路径抛出的异常
        @return 无（副作用为状态写入、计数与事件）
        """
        budget_kind = budget.classify_stage_error(exc)
        if budget_kind is not None:
            kind, retryable = budget_kind, False
            message = str(exc)
            if kind == ErrorKind.CONTEXT_OVERFLOW.value:
                ctx.metrics.count("budget.overflow_records")
                _feed_reactive_terminal(exc, ctx.metrics)
        elif isinstance(exc, SchemaViolation):
            kind, retryable = ErrorKind.EXTRACTION_INVALID.value, False
            message = str(exc)
        elif isinstance(exc, ProviderRetryableError):
            kind, retryable = ErrorKind.PROVIDER_RETRYABLE_EXHAUSTED.value, True
            message = str(exc)
        elif isinstance(exc, ProviderFatalError):
            kind, retryable = ErrorKind.PROVIDER_FATAL.value, False
            message = str(exc)
        elif isinstance(exc, OSError):
            kind, retryable = ErrorKind.IMAGE_DECODE_ERROR.value, False
            message = f"{type(exc).__name__}: {exc}"
        else:
            kind, retryable = ErrorKind.INTERNAL_ERROR.value, False
            message = f"{type(exc).__name__}: {exc}"
        err = StageError(stage=self.name, kind=kind, message=message,
                         retryable=retryable)
        item.errors.append(err)
        item.status = "failed"
        ctx.metrics.count(_COUNTER_FAILURES)
        ctx.metrics.event(_EV_ERROR, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(item.record.id,),
                          payload={"stage": self.name, "kind": kind,
                                   "message": message, "retryable": retryable})

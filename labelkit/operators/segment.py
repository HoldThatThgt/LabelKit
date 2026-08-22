"""M14 分段阶段（spec 3.14、CONTRACTS.md §7.14）——v1.8 流式模式算子。

把本批次的候选会话精化为 episode：按 PipelineItem.session_id 重新聚合活跃帧信封
（record.kind == "single"；批内位置序即会话序，由 M10 的整会话装箱保证），跑可选的
LLM 滑窗边界裁决（确定性 §10.9 提示词、经 schema_engine.segment_window_schema 取得
M8 内部 Schema 保证、代码侧首次命中缝合），随后确定性成段：噪音帧 → ``dropped_noise``
（鸭子类型 reason "noise"）、边界切分、min_len 校验（只作用于 LLM 精化过的段，S11——
reason "below_min_len"）、成员 → ``absorbed``，每段尾追加一个 sequence 信封到**同一个**
批次列表（Stage 合同 ②b）。链位置：链首，dedup 之前。失败策略 segment.on_error：
"keep" 把整个会话降级为一个 episode 并留下 S26 证据三件套（鸭子类型
``segment_degraded`` → _meta.stream.degraded + error 事件 + segment.failures 计数器，
绝不写 item.errors）；"fail" 让该会话全体成员失败。``judge_window`` 是 M7 成员回收
重判的**公开直调面**（获批的算子间导入例外）。

v1.11（上下文预算，SPEC-context-budget V4/V9/V13④/V20/V24/V27①）：帧摘要在装窗
**之前**按会话预计算一次，装箱定价与每个窗口提示词共用同一向量；segment profile 声明了
``context_window > 0`` 时，窗口切分从固定跨度切换为贪心预算装箱器
``budget.pack_windows``（v1.12 装箱器下沉：原 M14 私有 ``_pack_windows`` 原样迁入 budget
模块公开面，行为字节等价）——window 退化为纯上限，1 帧重叠与「接缝帧归后一窗」语义保留；
未声明预算则字节等价保持 v1.10 的固定切分。窗口调用抛出反应式 ``ContextOverflowError``
时对半重切重试（有界，≤ 2 层——400 嗅探终态恰好喂一次熔断器，A7）；窗口失败先经
``budget.classify_stage_error`` 归类，再回落 segmentation_invalid；每个实际派发的窗口
计一次 ``segment.windows``（→ report.stream.windows）。
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
from typing import TYPE_CHECKING, Mapping, Sequence

from labelkit.common.errors import (
    CircuitBreakerTripped,
    ContextOverflowError,
    ErrorKind,
    InternalError,
)
from labelkit.common.contracts.execution import TaskGroupRequest, TaskSpec
from labelkit.common.contracts.types import (
    PipelineItem,
    Record,
    RecordRef,
    StageError,
    digest_is_poor,
    frame_digest,
    tree_diff,
)

from labelkit.common.inference import budget as budget_mod
from labelkit.common.inference.budget import pack_windows
from labelkit.common.inference.llm_client import Message, Part, PromptBundle
from labelkit.common.inference.schema_engine import CallScope, segment_window_schema

if TYPE_CHECKING:
    from labelkit.common.config.model import ResolvedConfig
    from labelkit.common.contracts.stage import RunContext

_logger = logging.getLogger("labelkit.segment")

# 事件名（严格取 CONTRACTS.md §7.14 / §8.1 的字面串）
_EV_BOUNDARY = "segment.boundary"
_EV_ERROR = "error"

# M14 自有的计数器键（CONTRACTS.md §9.3；counts.episodes / absorbed /
# dropped_noise 由 M10 计量）。v1.11：segment.windows = 实际派发的窗口数，含 V20
# 拆分出的子窗（→ report.stream.windows，V13④——与 estimate_run 的 w_min 上界对账）；
# budget.degrade_retries 统计每一次 V20 对半降级（→ report.budget.degrade_retries，V13⑤）。
_COUNTER_FAILURES = "segment.failures"
_COUNTER_BELOW_MIN_LEN = "segment.below_min_len"
_COUNTER_DIGEST_POOR = "segment.digest_poor_frames"
_COUNTER_WINDOWS = "segment.windows"
_COUNTER_DEGRADE_RETRIES = "budget.degrade_retries"

# V20 降级上界：每个原始窗口最多 2 层降级（对半之后再对半一次）——乘性递减、有界
# （AIMD 家族，spec 3.14.4 溢出降级重试）。
_MAX_DEGRADE_LEVELS = 2

# 演绎映射（spec 3.14.4，代码侧查表——边界问题从不交给 LLM 回答）：
# continues/advances → 非边界；returns_to_entry/context_switch → 边界（该帧起新段）；
# interruption → 噪音。会话首帧恒为段首。
_BOUNDARY_RELATIONS = frozenset({"returns_to_entry", "context_switch"})
_NOISE_RELATION = "interruption"

# 中文提示词片段——逐字取自 CONTRACTS.md §10.9（spec 3.14.4）。
# 组装时把 "{N}" 替换为窗内帧数。
_SYSTEM_HEAD = (
    "你是屏幕操作流的分段审核员。下面给出同一会话中按时间顺序排列的 {N} 帧状态摘要\n"
    "（含相邻帧的确定性变更提示）。按三步作业：\n"
    "一、双向上下文概括：通读全窗，把握每帧之前若干帧正在进行的活动与之后若干帧的走向，再判断该帧。\n"
    "二、逐帧关系分类：对每一帧，判断它相对进行中活动的功能角色，只能从以下封闭词表中取恰一值：\n"
    "- continues: 同一流程的推进。\n"
    "- advances: 屏幕或 App 变了，但可见的任务实体延续（验证码、订单号、餐厅名等跨屏出现）——\n"
    "  跨 App 的同一任务属此值，不是边界。\n"
    "- returns_to_entry: 回到入口/搜索/桌面后开启新流程（同 App 背靠背任务的断点）。\n"
    "- context_switch: 交互对象与环境不连续且无实体延续——相关但无实体延续的新流程也取此值。\n"
    "- interruption: 与前后活动均无关的短暂插入（通知、弹窗、误触）。\n"
    "三、只输出逐帧关系，不判断边界（边界由既定规则从关系推导）。\n"
    "锚定约定：分段粒度取「完整任务」层级（整段录屏之下一层）；只看前台 App/前台窗口，\n"
    "忽略状态栏、后台通知等背景变化。"
)
_STRUCTURE_SENTENCE = "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容："
_STRUCTURE_PLAIN = ('{"frames": [{"index": <窗内帧序号>, "relation": <词表值>}, ...]}'
                    "（恰 {N} 项）")
_STRUCTURE_REASON = ('{"frames": [{"index": <窗内帧序号>, "relation": <词表值>, '
                     '"reason": <一句话理由>}, ...]}（恰 {N} 项）')
_FRAME_LABEL_TMPL = "[帧 {i}] {digest}"
_DIFF_LABEL_TMPL = "[帧 {i} 变更] {diff}"

# 摘要贫瘠护栏（S12）每次运行只打一次的 stderr WARN——与数据内容无关。
# v1.11（V4）：指引指向 profile 能力——原 use_vision 键已移除，选 profile 即选能力（V1）。
_DIGEST_POOR_WARNING = (
    "poor frame digest (zero visible text nodes): text-only boundary verdicts "
    "lack evidence; attach frame screenshots by pointing segment.llm at a "
    "supports_vision=true profile")


def _reason_requested(cfg: "ResolvedConfig") -> bool:
    """判断本次运行是否要求窗口裁决附带一句话理由。

    当且仅当 trace.enabled 且 "segment" ∈ trace.channels 时为真（§8.1 †，沿用
    classify 的 R29 构造——否则零额外 token）。

    @param cfg 已解析的不可变配置
    @return 是否请求 reason 字段
    """
    return cfg.trace.enabled and "segment" in cfg.trace.channels


def render_tree_diff(diff: Mapping) -> str:
    """把一个 tree_diff 映射固定文本化为 §10.9 的「[帧 {i} 变更]」行。

    计数 + 变更比例，app/title 标志位仅在置位时追加。纯确定性字符串拼装。

    @param diff tree_diff 产出的映射（added/removed/text_changed/change_ratio/…）
    @return 变更提示行的正文
    """
    text = (f"新增 {diff['added']} 节点，移除 {diff['removed']} 节点，"
            f"文本变化 {diff['text_changed']} 处，"
            f"变更比例 {diff['change_ratio']:.0%}")
    if diff["app_changed"]:
        text += "，应用切换"
    if diff["title_changed"]:
        text += "，标题变化"
    return text


def build_segment_prompt(frames: Sequence[Record], diffs: Sequence[Mapping | None],
                         cfg: "ResolvedConfig", with_reason: bool,
                         digests: Sequence[str]) -> PromptBundle:
    """确定性组装 CONTRACTS §10.9 模板——模板字节在 v1.11 未变。

    system：冻结的三步演绎判据（替入窗内帧数）+ 可选的 segment.context 行（为空则省略）
    + 带或不带 reason 片段的结构行。user：**一条**消息，每帧一个文本 part——
    "[帧 {i}] {digest}"，从第二帧起且调用方给了 diff 时再追加 "[帧 {i} 变更]" 行；
    segment.vision_resolved（v1.11 V1 解析产物）为真时，每帧摘要 part 之前先放该帧图片
    part（§10.1/§10.10 单消息多 part 形态）。

    ``digests``（v1.11 V9 冻结签名修订，CONTRACTS §7.14）：与 ``frames`` 对齐的逐帧摘要串，
    在装窗**之前**按会话预计算一次——装箱器与提示词共用同一向量，接缝帧不再被摘要两次；
    本组装器自身绝不计算摘要。

    @param frames 窗内帧记录，按会话序
    @param diffs 与 frames 对齐的相邻帧变更映射；窗首帧为 None
    @param cfg 已解析的不可变配置
    @param with_reason 结构行是否带 reason 片段
    @param digests 与 frames 对齐的逐帧摘要串
    @return 可直接交给 M9 的提示词包
    """
    seg = cfg.segment
    n = str(len(frames))
    lines = [_SYSTEM_HEAD.replace("{N}", n)]
    if seg.context:
        lines.append(seg.context)
    lines.append(_STRUCTURE_SENTENCE)
    structure = _STRUCTURE_REASON if with_reason else _STRUCTURE_PLAIN
    lines.append(structure.replace("{N}", n))
    messages: list[Message] = [
        Message(role="system", parts=(Part(kind="text", text="\n".join(lines)),))]

    parts: list[Part] = []
    for i, frame in enumerate(frames):
        if seg.vision_resolved and frame.image is not None:
            parts.append(Part(kind="image", image=frame.image))
        text = _FRAME_LABEL_TMPL.format(i=i, digest=digests[i])
        diff = diffs[i] if i < len(diffs) else None
        if i >= 1 and diff is not None:            # 窗首帧无此行
            text += "\n" + _DIFF_LABEL_TMPL.format(i=i, diff=render_tree_diff(diff))
        parts.append(Part(kind="text", text=text))
    messages.append(Message(role="user", parts=tuple(parts)))
    return PromptBundle(messages=tuple(messages))


def _static_prompt_est(cfg: "ResolvedConfig") -> int:
    """估算 §10.9 提示词静态（与记录无关）部分的 token 数。

    即装箱条件里的 est_static_system 项（SPEC-context-budget §3.3-1）。照
    build_segment_prompt 逐项列举：system 消息恰为 "\\n".join(_SYSTEM_HEAD,
    [segment.context], _STRUCTURE_SENTENCE, 结构行)——这里按常量形态求值（{N} 不替换；
    1–2 字符的帧数替换按设计留在 margin 余量里，V7），结构行的 with_reason 变体由 cfg
    确定性解析——再为 system / user 两条消息各加一份 MSG_OVERHEAD_TOKENS。逐帧的
    "[帧 {i}] " 标签与 "[帧 {i} 变更] {渲染后的 diff}" 行属帧脚手架，由逐帧
    DIFF_MAX_TOKENS 最坏常量覆盖（V9：diff 在装窗之后才算，其渲染行结构上远小于 128 token，
    含标签在内）。

    @param cfg 已解析的不可变配置
    @return 静态部分的 token 估计值
    """
    seg = cfg.segment
    lines = [_SYSTEM_HEAD]
    if seg.context:
        lines.append(seg.context)
    lines.append(_STRUCTURE_SENTENCE)
    lines.append(_STRUCTURE_REASON if _reason_requested(cfg) else _STRUCTURE_PLAIN)
    return (budget_mod.est_text("\n".join(lines))
            + 2 * budget_mod.MSG_OVERHEAD_TOKENS)


# ── 窗口裁决（一窗一次调用）────────────────────────────────────────────────

def _adjacent_diffs(frames: Sequence[Record],
                    quantize: int) -> list[Mapping | None]:
    """代码侧预拼相邻帧变更；窗首帧没有这一行。

    坐标量化复用工具唯一的量化旋钮（dedup.bounds_quantize_px——沿用 M3 序列化先例），
    像素抖动因此不会淹没 added/removed。

    @param frames 窗内帧记录，按会话序
    @param quantize 边界框量化像素
    @return 与 frames 对齐的变更映射列表，首元素恒为 None
    """
    diffs: list[Mapping | None] = [None]
    for i in range(1, len(frames)):
        diffs.append(tree_diff(frames[i - 1].ui_tree, frames[i].ui_tree, quantize))
    return diffs


def _align_relations(entries: Sequence[Mapping],
                     n: int) -> tuple[dict[int, Mapping], list[str]]:
    """LLM 逐帧关系的代码侧后校验：首次命中建表，缺席帧回落 "continues"。

    重复 index 保留首次出现的条目；缺席帧取保守中性值（沿用 quality「判据缺席即打平」先例）。

    @param entries LLM 返回的 frames 数组条目
    @param n 窗内帧数
    @return （index → 条目的首次命中表，与帧对齐的关系列表）
    """
    table: dict[int, Mapping] = {}
    for entry in entries:
        table.setdefault(entry["index"], entry)
    verdicts = ["continues" if table.get(i) is None else table[i]["relation"]
                for i in range(n)]
    return table, verdicts


@dataclasses.dataclass(frozen=True, slots=True)
class _WindowVerdict:
    """叶任务返回的不可变窗口裁决。"""

    span: tuple[int, int]       # 会话内窗口跨度
    member_ids: tuple[str, ...] # 窗内成员身份
    verdicts: tuple[str, ...]   # 与成员对齐的关系
    model: str                  # 实际响应模型
    reasons: tuple[str, ...]    # 可选理由，未请求或缺席时为空


async def judge_window(frames: Sequence[Record], ctx: "RunContext") -> list[str]:
    """一窗一次调用——经 complete_validated(schema=segment_window_schema(…)) 取裁决。

    后校验在本函数内部完成：首次命中建表（重复 index 保留首次出现）、缺席帧回落
    "continues"（保守中性，沿用 quality「判据缺席即打平」先例）；返回与 ``frames``
    对齐的逐帧关系列表。每窗发一条 segment.boundary 事件。**公开直调面**：M7 的成员
    回收重判直接调用本函数（CONTRACTS §7.14）——基本规则里登记在案的获批导入例外，
    名字与签名冻结。

    @param frames 窗内帧记录，按会话序
    @param ctx 运行上下文
    @return 与 frames 对齐的逐帧关系列表
    """
    outcome = await _call_window(frames, ctx, span=(0, len(frames)))
    _emit_boundary(ctx, outcome, session_id=None)
    return list(outcome.verdicts)


async def _call_window(frames: Sequence[Record], ctx: "RunContext", *,
                       span: tuple[int, int],
                       digests: Sequence[str] | None = None) -> _WindowVerdict:
    """执行一次纯窗口调用并返回待声明序归并的冻结裁决。

    仅限关键字的额外参数携带窗口跨度，以及 v1.11 按会话预计算的 ``digests`` 切片。
    ``digests=None`` 即公开
    judge_window 路径（M7 的 ≤3 帧重判表）：摘要表在此自行计算，从而保持公开签名不变
    （CONTRACTS §7.14）。

    @param frames 窗内帧记录，按会话序
    @param ctx 运行上下文
    @param span 窗口跨度 [start, end)，进事件载荷
    @param digests 与 frames 对齐的摘要切片；None 表示本函数自行计算
    @return 不修改业务对象或发业务事件的冻结窗口裁决
    """
    cfg = ctx.cfg
    with_reason = _reason_requested(cfg)
    if digests is None:
        digests = [frame_digest(frame, cfg.segment.digest_max_chars)
                   for frame in frames]
    diffs = _adjacent_diffs(frames, cfg.dedup.bounds_quantize_px)
    prompt = build_segment_prompt(frames, diffs, cfg, with_reason, digests)
    schema = segment_window_schema(len(frames), with_reason)
    obj, _usage, _attempts, model = await ctx.schema_engine.complete_validated(
        cfg.segment.llm, prompt, schema,
        scope=CallScope(record_ids=(frames[0].id,), batch_no=ctx.batch_no))

    table, verdicts = _align_relations(obj["frames"], len(frames))
    reasons: tuple[str, ...] = ()
    if with_reason:
        reasons = tuple(table[i]["reason"] for i in range(len(frames))
                        if i in table and "reason" in table[i])
    return _WindowVerdict(
        span=span,
        member_ids=tuple(frame.id for frame in frames),
        verdicts=tuple(verdicts),
        model=model,
        reasons=reasons,
    )


def _emit_boundary(ctx: "RunContext", outcome: _WindowVerdict,
                   session_id: str | None) -> None:
    """在归并屏障按声明序发出窗口业务事件。

    @param ctx 运行上下文
    @param outcome 冻结窗口裁决
    @param session_id 会话身份；公开直调面为 None
    """
    payload = {
        "session_id": session_id,
        "window": [outcome.span[0], outcome.span[1]],
        "member_ids": list(outcome.member_ids),
        "relations": [{"index": i, "relation": relation}
                      for i, relation in enumerate(outcome.verdicts)],
        "model": outcome.model,
    }
    if outcome.reasons:
        payload["reason"] = list(outcome.reasons)
    ctx.metrics.event(_EV_BOUNDARY, stage="segment", batch_no=ctx.batch_no,
                      record_ids=(), payload=payload)


def _window_spans(n: int, window: int) -> list[tuple[int, int]]:
    """会话 n 帧上的固定滑窗跨度（spec 3.14.4 伪代码）。

    window = [start, end)，步长 = window − 1（1 帧重叠——缝合时接缝帧的整条裁决归后一窗）。
    v1.11：这是**预算未声明**的切分（segment profile 缺失或 context_window == 0）——原样
    保留，使降级路径按构造与 v1.10 字节一致（V9 回归锚）；声明了预算的会话走
    budget.pack_windows（v1.12 下沉后的同一纯函数）。

    @param n 会话帧数
    @param window 窗口帧数上限
    @return 升序排列的窗口跨度列表
    """
    spans: list[tuple[int, int]] = []
    start = 0
    while start < n:
        end = min(start + window, n)
        spans.append((start, end))
        if end == n:
            break
        start += window - 1
    return spans


# v1.12（装箱器下沉裁决）：贪心预算装箱器 _pack_windows 原样迁至
# budget.pack_windows（公开面，行为字节等价，M13 帧级批量判决复用），本模块经
# 顶部 import 以既有调用形继续使用——归属句见 CONTRACTS §7.17。


async def _judge_span_degrading(judge, span: tuple[int, int],
                                ctx: "RunContext", *,
                                level: int = 0) -> list[tuple[tuple[int, int], _WindowVerdict]]:
    """V20 窗口分半降级重试（spec 3.14.4 溢出降级重试；SPEC-context-budget V20/V24/A7）。

    ``judge`` = 异步可调用 callable(span) -> 该跨度的逐帧关系（依赖注入——正是这道缝
    让本纯逻辑可离线测试）。返回叶子结果 [(子跨度, 关系列表), ...]，按跨度升序，可直接
    做与调度无关的 rel[] 覆写（后一子窗依旧拥有它的接缝帧）。

    抛出反应式 ContextOverflowError 的窗口调用被对半重切：[s, m+1) 与 [m, e)，m = 中点
    ——1 帧重叠与「接缝帧归后一窗」语义在拆分后依旧成立，且不丢帧。乘性递减、有界：每个
    原始窗口最多 _MAX_DEGRADE_LEVELS（2）层降级；每次对半计一次 budget.degrade_retries。
    两半**顺序**执行——熔断记账确定（第一个终态即停止整棵树；降级流量只在反应式路径上
    出现且罕见，为并发牺牲确定性不划算）。

    终态（不再拆分）：非反应式 phase（precheck = 装箱层 bug，防御式捕获，永不可降级）、
    最小 2 帧窗口（< 3 帧无法拆成两个 ≥ 2 帧的半窗）、或触及层数上界。按 SPEC §3.5 的
    熔断矩阵，**只有** 400 嗅探形态的反应式终态（origin="http_400"）喂熔断器——恰好一次，
    就在这里的叶子上（A7：M9 抛出时刻意没喂）；200 形态的 origin="finish" 神谕搭乘的是一次
    成功 HTTP 交互，其 ok 已清空连续计数，而 precheck 根本没有 provider 交互。异常随后
    重新抛出，落到该会话的 on_error 处置（父层递归绝不重复结算——只有直接的 judge() 调用
    位于 try 之内）。

    @param judge 异步可调用：接受跨度、返回该跨度的逐帧关系
    @param span 本层的窗口跨度 [start, end)
    @param ctx 运行上下文
    @param level 当前降级层数（递归内部使用）
    @return 叶子结果 [(子跨度, 关系列表), ...]，按跨度升序
    @raises ContextOverflowError 终态不可再降级时原样重新抛出
    """
    start, end = span
    try:
        return [(span, await judge(span))]
    except ContextOverflowError as exc:
        if exc.phase != "reactive" or end - start < 3 or level >= _MAX_DEGRADE_LEVELS:
            if exc.phase == "reactive" and exc.origin == "http_400":
                ctx.metrics.record_provider_result(fatal=True)
            raise
        ctx.metrics.count(_COUNTER_DEGRADE_RETRIES)
        mid = (start + end) // 2
        results = await _judge_span_degrading(judge, (start, mid + 1), ctx,
                                              level=level + 1)
        results.extend(await _judge_span_degrading(judge, (mid, end), ctx,
                                                   level=level + 1))
        return results


# ── 阶段内部参数对象（入参收拢，公开面不变）──────────────────────────────

@dataclasses.dataclass(frozen=True, slots=True)
class _WindowJob:
    """一次原始窗口派发所需的全部输入（会话级向量 + 窗口跨度）。"""

    records: list[Record]      # 会话级 Record 向量；子窗按跨度切片，不重新摘要
    digests: list[str]         # 与 records 对齐的会话级摘要向量（V9 预计算）
    sid: str                   # 会话 id，进 segment.boundary 事件载荷
    span: tuple[int, int]      # 原始窗口跨度 [start, end)
    degrade: bool              # 是否启用 V20 分半降级重试（= 该 profile 声明了预算）


@dataclasses.dataclass(frozen=True, slots=True)
class _SessionPass:
    """第二阶段单会话结算所需的上下文（批次列表 + 会话成员 + 切分标记）。"""

    batch: list[PipelineItem]  # 本批次信封列表；episode 尾追加到同一对象（②b）
    ctx: "RunContext"          # 运行上下文（配置 / 指标 / Schema 引擎）
    sid: str                   # 会话 id
    items: list[PipelineItem]  # 该会话按会话序排列的活跃帧信封
    split: bool                # 会话是否带 M10 硬切分标记（S21）


# ── 阶段 ─────────────────────────────────────────────────────────────────────

class SegmentStage:
    """M14 分段算子：把候选会话精化为 episode 序列记录（Stage 合同 ②b）。"""

    name = "segment"

    def __init__(self, cfg: "ResolvedConfig"):
        """构造分段阶段。

        @param cfg 已解析的不可变配置
        """
        self.cfg = cfg
        self._digest_poor_warned = False           # 每次运行只打一次的 WARN（S12）

    async def run(self, batch: list[PipelineItem],
                  ctx: "RunContext") -> list[PipelineItem]:
        """按会话精化本批次帧信封，并尾追加 episode 信封。

        三步：① 按 session_id 重新聚合活跃帧；② 经 TaskExecutor 跑完所有会话的所有窗口；
        ③ 按批内会话序做同步确定性结算。返回的永远是入参那**同一个**列表对象（②b）。

        @param batch 本批次信封列表（原地改状态并尾追加，绝不删除元素）
        @param ctx 运行上下文
        @return 与入参同一个列表对象
        """
        sessions = self._group_sessions(batch)
        if not sessions:
            return batch
        refine = self.cfg.segment.strategy in ("llm", "hybrid")
        jobs_meta, jobs = self._plan_windows(sessions, ctx, refine)
        outcomes = await self._run_windows(jobs_meta, jobs, ctx)
        self._settle_sessions(batch, ctx, sessions, outcomes, refine)
        return batch                               # 同一个列表对象（②b）

    @staticmethod
    def _group_sessions(
            batch: list[PipelineItem]) -> dict[str, list[PipelineItem]]:
        """按 session_id 重新聚合活跃帧信封；批内位置序即会话序（M10 整会话装箱）。

        kind == "sequence" 的信封永不进入处理面——天然幂等；未打戳的帧（session_id 为
        None）防御式忽略。

        @param batch 本批次信封列表
        @return 会话 id → 该会话的活跃帧信封列表（插入序即会话序）
        """
        sessions: dict[str, list[PipelineItem]] = {}
        for item in batch:
            if item.status != "active" or item.record.kind != "single":
                continue
            if item.session_id is None:
                continue
            sessions.setdefault(item.session_id, []).append(item)
        return sessions

    def _plan_windows(self, sessions: dict[str, list[PipelineItem]],
                      ctx: "RunContext",
                      refine: bool) -> tuple[list[tuple[str, tuple[int, int]]], list[_WindowJob]]:
        """为每个需要精化的会话切窗并冻结叶任务输入（第一阶段）。

        v1.11（V9）：当且仅当 segment profile 声明了上下文窗口，切分才走预算装箱；未声明
        则回到 v1.10 的固定切分，字节一致。静态提示词估计与逐图成本都是配置/批次冻结值，
        在此一次性算好——装箱因而保持为 (输入, 配置) 的纯函数。

        @param sessions 会话 id → 活跃帧信封列表
        @param ctx 运行上下文
        @param refine 策略是否要求 LLM 精化（llm / hybrid）
        @return （窗口元信息 [(会话 id, 跨度)]，与之对齐的协程列表）
        """
        seg = self.cfg.segment
        prof = self.cfg.llm_profiles.get(seg.llm)
        budget_on = refine and prof is not None and prof.context_window > 0
        pack_budget = (budget_mod.input_budget(prof) - _static_prompt_est(self.cfg)
                       if budget_on else 0)
        jobs_meta: list[tuple[str, tuple[int, int]]] = []
        jobs: list[_WindowJob] = []
        for sid, items in sessions.items():
            if not refine or len(items) == 1:      # 规则 / 单帧会话：零 LLM
                continue
            self._guard_digest_poverty(items, ctx)
            # V9 会话级摘要预计算——每帧每会话恰算一次，且在装窗之前；装箱定价与每个窗口
            # 提示词（含 V20 子窗）共用本向量，接缝帧不再被摘要两次。上面的贫瘠护栏是一条
            # 独立计算路径（digest_is_poor 自带硬编码上限），按设计不受影响。
            records = [item.record for item in items]
            digests = [frame_digest(record, seg.digest_max_chars)
                       for record in records]
            spans = (self._pack_spans(digests, ctx, pack_budget) if budget_on
                     else _window_spans(len(items), seg.window))
            for span in spans:
                jobs_meta.append((sid, span))
                jobs.append(_WindowJob(
                    records=records,
                    digests=digests,
                    sid=sid,
                    span=span,
                    degrade=budget_on,
                ))
        return jobs_meta, jobs

    def _guard_digest_poverty(self, items: list[PipelineItem],
                              ctx: "RunContext") -> None:
        """摘要贫瘠护栏（S12）：逐帧计数，整次运行只打一次 WARN。

        @param items 该会话的活跃帧信封
        @param ctx 运行上下文
        """
        for item in items:
            if digest_is_poor(item.record):
                ctx.metrics.count(_COUNTER_DIGEST_POOR)
                if not self._digest_poor_warned:
                    self._digest_poor_warned = True
                    _logger.warning(_DIGEST_POOR_WARNING,
                                    extra={"stage": self.name,
                                           "batch": ctx.batch_no})

    def _pack_spans(self, digests: list[str], ctx: "RunContext",
                    pack_budget: int) -> list[tuple[int, int]]:
        """预算已声明时的贪心装箱切窗（V9）。

        逐图成本每会话只读一次校准器——快照是批次冻结的（V19），因此批内任何位置读到的
        值都相同、确定。

        @param digests 会话级摘要向量
        @param ctx 运行上下文
        @param pack_budget 扣除静态提示词后的可用 token 预算
        @return 升序排列的窗口跨度列表
        """
        seg = self.cfg.segment
        image_cost = (ctx.llm.calibrator.cost(seg.llm)
                      if seg.vision_resolved else 0)
        costs = [budget_mod.est_text(digest) + budget_mod.DIFF_MAX_TOKENS
                 + image_cost for digest in digests]
        return pack_windows(costs, pack_budget, seg.window)

    async def _run_windows(self, jobs_meta: list[tuple[str, tuple[int, int]]],
                           jobs: list[_WindowJob], ctx: "RunContext"
                           ) -> dict[str, list[tuple[tuple[int, int], object]]]:
        """经共享 TaskExecutor 跑完全批窗口，并按输入序归拢。

        缝合是随后的同步遍历，按窗口跨度定位——与调度无关、零 rng。

        @param jobs_meta 与 jobs 对齐的 (会话 id, 跨度) 元信息
        @param jobs 冻结窗口计划
        @param ctx 运行上下文
        @return 会话 id → [(子跨度, 关系列表 | 失败异常), ...]
        """
        outcomes: dict[str, list[tuple[tuple[int, int], object]]] = {}
        if not jobs:
            return outcomes
        specs = tuple(
            TaskSpec(
                task_id=f"{ctx.task_namespace}:segment:{ordinal}",
                declaration_key=(ctx.batch_no, 0, ordinal),
                stage=self.name,
                resource_key=("llm", self.cfg.segment.llm),
                operation=lambda job=job: self._run_window(job, ctx),
            )
            for ordinal, job in enumerate(jobs)
        )
        results = await ctx.tasks.run_group(TaskGroupRequest(specs))
        for (sid, span), result in zip(jobs_meta, results):
            bucket = outcomes.setdefault(sid, [])
            if isinstance(result, BaseException):
                bucket.append((span, result))
            else:
                bucket.extend(result)              # V20 叶子结果，按跨度序
        return outcomes

    def _settle_sessions(self, batch: list[PipelineItem], ctx: "RunContext",
                         sessions: dict[str, list[PipelineItem]],
                         outcomes: dict[str, list[tuple[tuple[int, int], object]]],
                         refine: bool) -> None:
        """第二阶段：按批内会话序做同步、确定性的逐会话结算。

        @param batch 本批次信封列表（episode 尾追加于此）
        @param ctx 运行上下文
        @param sessions 会话 id → 活跃帧信封列表
        @param outcomes 会话 id → 窗口结果（成功关系列表或失败异常）
        @param refine 策略是否要求 LLM 精化
        """
        for sid, items in sessions.items():
            split = any(getattr(item, "session_split", False) for item in items)
            if not refine or len(items) == 1:
                # 规则 / 单帧降级：会话原样成为一个 episode；noise_filter / min_len
                # 不适用（S11）。
                self._emit_episode(batch, sid, items, split=split)
                continue
            session = _SessionPass(batch=batch, ctx=ctx, sid=sid, items=items,
                                   split=split)
            session_outcomes = outcomes[sid]
            for _, result in session_outcomes:
                if isinstance(result, _WindowVerdict):
                    _emit_boundary(ctx, result, session_id=sid)
            failures = [result for _, result in session_outcomes
                        if isinstance(result, BaseException)]
            if failures:
                self._dispose_failed(session, failures)
                continue
            rel: list[str | None] = [None] * len(items)
            for (start, end), outcome in session_outcomes:
                if not isinstance(outcome, _WindowVerdict):
                    _logger.error("segment reducer received an invalid window outcome")
                    raise InternalError("segment reducer received an invalid window outcome")
                for i in range(end - start):       # 无条件覆写 ⇒
                    rel[start + i] = outcome.verdicts[i]  # 接缝帧归后一窗
            self._assemble(session, rel)

    async def _run_window(self, job: _WindowJob, ctx: "RunContext"):
        """派发一个原始窗口，并做窗口级错误捕获。

        只有「大三样」向上逃逸（合同 ④——其余一律转成会话级 on_error 处置，S26）。
        ``job.records`` / ``job.digests`` 是**会话级**向量，子跨度只做切片，因此 V20 拆分
        重派时不会重新摘要。``job.degrade`` = 预算已开（V20 的分半重试是预算模式下的反应；
        预算关闭时的溢出信号——那个无条件的 200 形态神谕——走下面的普通失败路径，由
        _dispose_failed 归类）。每个实际派发的窗口（含拆分子窗）计一次 segment.windows（V13④）。

        @param job 本次原始窗口的派发参数
        @param ctx 运行上下文
        @return 叶子结果 [(子跨度, 关系列表), ...]，或捕获到的失败异常对象
        """
        async def judge(sub: tuple[int, int]) -> _WindowVerdict:
            """派发一个（子）窗口并计数。

            @param sub 子窗口跨度 [start, end)
            @return 该子窗口的冻结裁决
            """
            ctx.metrics.count(_COUNTER_WINDOWS)
            return await _call_window(job.records[sub[0]:sub[1]], ctx,
                                      span=sub, digests=job.digests[sub[0]:sub[1]])

        try:
            if job.degrade:
                return await _judge_span_degrading(judge, job.span, ctx)
            return [(job.span, await judge(job.span))]
        except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 — 记录级隔离绝对优先
            _logger.error("segment window judgment failed: session=%s "
                          "window=[%d,%d) exc=%s", job.sid, job.span[0],
                          job.span[1], type(exc).__name__,
                          extra={"stage": self.name, "batch": ctx.batch_no})
            return exc

    def _dispose_failed(self, session: _SessionPass,
                        failures: list[BaseException]) -> None:
        """窗口失败的两形态处置（spec 3.14.6）。

        "keep"（默认）：会话放弃**全部**窗口裁决，整体作为一个 episode 存活——证据三件套 =
        鸭子类型 segment_degraded（→ _meta.stream.degraded）+ error 事件 + segment.failures
        计数器，绝不写 item.errors（S26）。"fail"：会话全体成员失败 → rejects。
        v1.11（V27①）：kind 先经 budget.classify_stage_error 归类——ContextOverflowError →
        "context_overflow"（每个被拒成员在此拒收点计一次 budget.overflow_records，与
        annotate/quality/verify 共用的 V13② 约定——报表读计数器，绝不读 rejects），
        OutputTruncatedError → "output_truncated"——再回落既有的 segmentation_invalid；
        此处词表不精确会破坏 §3.5 的归因。首个失败决定归类与消息（沿用 v1.11 之前的消息语义）。

        @param session 本会话的结算上下文
        @param failures 本会话所有失败窗口的异常，按跨度序
        """
        ctx = session.ctx
        first = failures[0]
        kind = (budget_mod.classify_stage_error(first)
                or ErrorKind.SEGMENTATION_INVALID.value)
        windows_failed = len(failures)
        message = str(first)
        if self.cfg.segment.on_error == "fail":
            error = StageError(stage=self.name, kind=kind, message=message,
                               retryable=False)
            for item in session.items:
                item.errors.append(error)
                item.status = "failed"
                if kind == ErrorKind.CONTEXT_OVERFLOW.value:
                    ctx.metrics.count("budget.overflow_records")  # V13②：每个拒收各计一次
        else:                                      # "keep"
            self._emit_episode(session.batch, session.sid, session.items,
                               split=session.split,
                               degraded={"kind": kind,
                                         "windows_failed": windows_failed})
        ctx.metrics.count(_COUNTER_FAILURES)
        ctx.metrics.event(_EV_ERROR, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(),
                          payload={"stage": self.name, "kind": kind,
                                   "message": message, "retryable": False})

    def _assemble(self, session: _SessionPass, rel: list[str | None]) -> None:
        """确定性成段（spec 3.14.4 成段流程）。

        ① 剔噪（noise_filter=true；false 则把 interruption 帧当作非边界成员保留）、
        ② 边界切分（会话首帧恒为段首）、③ 对 LLM 精化过的段做 min_len 校验（S11）、
        ④ 每个存活段发一个 episode。

        @param session 本会话的结算上下文
        @param rel 与会话帧对齐的逐帧关系（缝合后的最终裁决）
        """
        seg = self.cfg.segment
        kept: list[tuple[int, PipelineItem]] = []
        for idx, item in enumerate(session.items):
            if seg.noise_filter and rel[idx] == _NOISE_RELATION:   # 含首帧
                item.status = "dropped_noise"
                item.noise_attribution = ("segment", "noise")  # type: ignore[attr-defined]
            else:
                kept.append((idx, item))

        segments: list[list[PipelineItem]] = []
        current: list[PipelineItem] = []
        for idx, item in kept:
            # rel[0] 的边界值永不切分（会话首帧恒为段首）。
            if current and idx != 0 and rel[idx] in _BOUNDARY_RELATIONS:
                segments.append(current)
                current = []
            current.append(item)
        if current:
            segments.append(current)

        for members in segments:
            if len(members) < seg.min_len:         # S11：只作用于 LLM 精化过的切分
                for item in members:
                    item.status = "dropped_noise"
                    item.noise_attribution = ("segment", "below_min_len")  # type: ignore[attr-defined]
                    session.ctx.metrics.count(_COUNTER_BELOW_MIN_LEN)
                continue
            self._emit_episode(session.batch, session.sid, members,
                               split=session.split)

    @staticmethod
    def _emit_episode(batch: list[PipelineItem], sid: str,
                      members: list[PipelineItem], *, split: bool,
                      degraded: Mapping | None = None) -> None:
        """吸收成员信封并尾追加一个 sequence 信封（合同 ②b）。

        id = sha256("\\n".join(成员 id))[:16]，成形时即固定；text/raw/ui_tree/image 全为
        None；ref 继承首个成员（S24）；打上 session_id；session_split / segment_degraded
        以鸭子类型属性随行，供 M11 的 _meta.stream 使用。

        @param batch 本批次信封列表（尾追加于此）
        @param sid 会话 id
        @param members 本段成员帧信封，按会话序
        @param split 会话是否带 M10 硬切分标记
        @param degraded 降级证据（kind / windows_failed）；None 表示未降级
        """
        records = tuple(item.record for item in members)
        joined = "\n".join(record.id for record in records)
        first = records[0]
        episode_record = Record(
            id=hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16],
            modality=first.modality,
            text=None, raw=None, ui_tree=None, image=None,
            ref=RecordRef(source_file=first.ref.source_file,
                          line_no=first.ref.line_no,
                          pair_index=first.ref.pair_index,
                          generated_from=(), generator=None),
            kind="sequence", members=records)
        for item in members:
            item.status = "absorbed"
        episode = PipelineItem(record=episode_record, session_id=sid)
        if split:
            episode.session_split = True  # type: ignore[attr-defined]
        if degraded is not None:
            episode.segment_degraded = dict(degraded)  # type: ignore[attr-defined]
        batch.append(episode)

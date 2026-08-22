"""M13 classify 阶段（spec 3.13，CONTRACTS.md §7.13）。

对 active 且尚未判决的信封，按用户类别表做闭集 LLM 分类：确定性提示词装配
（CONTRACTS §10.8）、M8 内部 schema 保证（schema_engine.classification_schema——
不计 resolved_at、不过 L2.5）、后校验的确定性标签归一、可选自洽投票（自有投票
规则——绝非 annotate._majority_vote，R26）、on_error 的 fallback/fail 策略
（R4：兜底取证落在 Classification.detail，绝不入 item.errors），以及 multi 装配
下就地追加到批尾的兄弟信封扇出（Stage 契约 ②a）。链位：dedup → classify → quality。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Mapping, Sequence

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
    Classification,
    PipelineItem,
    Record,
    StageError,
    frame_digest,
)
from labelkit.common.contracts.execution import TaskGroupRequest, TaskSpec

from labelkit.common.inference import budget
from labelkit.common.inference.llm_client import Message, Part, PromptBundle
from labelkit.common.inference.schema_engine import (
    CallScope,
    classification_schema,
    frame_classify_schema,
)

if TYPE_CHECKING:
    from labelkit.common.config.model import ClassifyConfig, ResolvedConfig
    from labelkit.common.contracts.stage import RunContext

_logger = logging.getLogger("labelkit.classify")

# 事件名（严格取 CONTRACTS.md §7.13 / §8.1 的字面串）。
_EV_DECISION = "classify.decision"
_EV_ERROR = "error"

# M13 自有的计数器键（CONTRACTS.md §9.3；counts.fanout 由 M10 记账）。
_COUNTER_CLASSES_PREFIX = "classify.classes."
_COUNTER_FALLBACK = "classify.fallback"
_COUNTER_FAILURES = "classify.failures"
_COUNTER_MULTI_LABEL = "classify.multi_label_records"

# 中文提示词片段——逐字取自 CONTRACTS.md §10.8（spec 3.13.3）。
_SYSTEM_HEAD_SINGLE = "你是数据分类员。阅读待分类数据，判断它属于以下类别中的哪一类。类别表："
_SYSTEM_HEAD_MULTI = ("你是数据分类员。阅读待分类数据，判断它适用于以下哪些类别"
                      "（至少 1 类，至多 {max_labels} 类）。类别表：")
_STRUCTURE_SENTENCE = "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容："
_STRUCTURE_SINGLE = '{"class": <类名>}'
_STRUCTURE_SINGLE_REASON = '{"class": <类名>, "reason": <一句话理由>}'
_STRUCTURE_MULTI = '{"classes": [<类名>, ...]}'
_STRUCTURE_MULTI_REASON = '{"classes": [<类名>, ...], "reason": <一句话理由>}'
_LABEL_EXAMPLE_TMPL = "[类别示例·{name}] {example}"
_LABEL_RECORD = "[待分类数据]"
_LABEL_SCREENSHOT = "[屏幕截图]"
_LABEL_UI_TREE = "[UI 控件树]"
# v1.8 序列变体标签 + 截断标记（CONTRACTS §10.8 [FROZEN HERE]）。
_LABEL_RECORD_SEQ = "[待分类数据·序列]"
_LABEL_FIRST_FRAME = "[首帧截图]"
_SEQ_TRUNCATION_MARKER = "…(truncated {n} members)"

# ── v1.12 帧级批量判决（SPEC-frame-annotation §3.2，实现后 verbatim 捕进 CONTRACTS §10.12）──
# 事件与计数器：命名空间 frame_classify.* 与序列级 classify.* 严格分离（计数命名空间裁决）。
_EV_FRAME = "classify.frame"
_COUNTER_FRAME_CALLS = "frame_classify.calls"
_COUNTER_FRAME_FALLBACK = "frame_classify.fallback"
_COUNTER_FRAME_WINDOW_FAILURES = "frame_classify.window_failures"
_COUNTER_FRAME_SKIPPED_DEGRADED = "frame_classify.skipped_degraded"
_COUNTER_DEGRADE_RETRIES = "budget.degrade_retries"

# V20 镜像（segment 同款）：单个原始窗口至多两级对半降级重试。
_MAX_FRAME_DEGRADE_LEVELS = 2

# 帧级模板头（冻结常量；budget.TEMPLATE_HEAD_TOKENS["frame_classify"] 跨层等式测试引用）。
# {N} 装配期以 str.replace 代入窗内成员数（segment _SYSTEM_HEAD 同款——est 取未代入
# 常量形，1–2 字符代入量由 margin 吸收，V7）。
_FRAME_SYSTEM_HEAD = (
    "[任务]\n"
    "你是数据流的逐帧分类员。下面给出同一会话中按时间顺序排列的 {N} 帧成员摘要，"
    "对每一帧独立判断它属于以下类别中的哪一类，只能从以下封闭类别表中取恰一值。类别表："
)
_FRAME_STRUCTURE = ('{"labels": [<第 1 帧类名>, <第 2 帧类名>, ...]}'
                    "（恰 {N} 项，按帧序与成员摘要行对齐）")
_LABEL_FRAME_MEMBERS = "[会话成员帧]"
_LABEL_MEMBER_SCREENSHOT = "[成员 {i} 截图]"
_FRAME_MEMBER_LINE = "{m}. {digest}"


@dataclass(frozen=True)
class _ClassifyPlan:
    """一条信封的冻结样本计划。"""

    record: Record
    item_ordinal: int
    prompt: PromptBundle
    schema: dict
    with_reason: bool
    sample_count: int


@dataclass(frozen=True)
class _ClassifySampleOutcome:
    """一个纯样本叶任务的结果或普通记录级异常。"""

    value: Mapping | BaseException


@dataclass(frozen=True)
class _FrameEpisodePlan:
    """一条 episode 的冻结帧窗口计划。"""

    item_ordinal: int
    episode_id: str
    members: tuple[Record, ...]
    digests: tuple[str, ...]
    spans: tuple[tuple[int, int], ...]
    budget_on: bool


@dataclass
class _FrameRunStats:
    """单个原始窗口叶任务的局部调用统计。"""

    calls: int = 0
    degrade_retries: int = 0


@dataclass(frozen=True)
class _FrameWindowOutcome:
    """一个原始帧窗口叶任务的纯结果。"""

    leaves: tuple[tuple[tuple[int, int], object], ...]
    calls: int
    degrade_retries: int


def _reason_requested(cfg: "ResolvedConfig") -> bool:
    """R29：当且仅当 trace.enabled 且 "classify" ∈ trace.channels 时索取 reason。

    @param cfg 本次运行的不可变解析配置
    @return 是否在提示词与判决产物中索取一句话理由
    """
    return cfg.trace.enabled and "classify" in cfg.trace.channels


# ── v1.11 上下文预算装填（spec 3.13.4「上下文预算装填」行）───────────────────

_TREE_MARKER_RE = re.compile(r"^…\(truncated (\d+) nodes\)$")


@dataclass
class _PromptFit:
    """单次分类调用的记录侧装填状态（spec 3.13.4 v1.11 行）。

    唯一可修剪槽位是当前记录的树渲染（单条 UI）或 episode 摘要正文（序列，同族
    上限）；类别表 / instruction / 类别示例是用户静态语义资产——属 V13③ 的
    M1 预检地界，**绝不**动态修剪。
    """
    input_budget: int      # 输入预算 = input_budget(classify profile)，profile 随发
                           # 结构化输出时再减去 schema 的 est
    image_cost: int        # 每图校准读数（批冻结，V19）；提示词无图时为 0
    truncations: int = 0   # 本次装配实际发生的修剪次数（计 budget.truncations.classify）
    overflow: bool = False  # 整篇复核仍超预算：最小单元不可装填（V10，调用方拒绝该记录）


def _fit_tree_text(rendered: str, budget_tokens: int) -> tuple[str, bool]:
    """§3.3③ 序列化 UI 树的动态上限。

    渲染文本（已在 input.ui_tree_max_chars 绝对上限之下）以 est_text 复核；超出
    份额时自尾部丢弃 NODE 行，并以 serialize 家族标记 "…(truncated N nodes)"
    收尾——N 累加到既有标记的计数上，标记语义仍归 UITree.serialize 所有。
    确定性：est_text 对前缀单调，故用二分找到最大可容前缀。

    @param rendered 已渲染的 UI 树文本
    @param budget_tokens 本槽位可用的 token 份额
    @return (修剪后的文本, 是否发生修剪)
    """
    if budget.est_text(rendered) <= budget_tokens:
        return rendered, False
    lines = rendered.split("\n")
    base = 0
    m = _TREE_MARKER_RE.match(lines[-1])
    if m is not None:
        base = int(m.group(1))
        lines = lines[:-1]
    total = len(lines)

    def candidate(keep: int) -> str:
        """构造保留前 keep 行、以累计标记收尾的候选文本。

        @param keep 保留的 NODE 行数
        @return 候选文本
        """
        marker = f"…(truncated {base + total - keep} nodes)"
        return "\n".join(lines[:keep] + [marker])

    lo, hi = 0, total - 1                        # keep == total 已知装不下
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if budget.est_text(candidate(mid)) <= budget_tokens:
            lo = mid
        else:
            hi = mid - 1
    return candidate(lo), True


def _fit_digest_body(body: str, budget_tokens: int) -> tuple[str, bool]:
    """§3.3③ 序列摘要正文的同族上限。

    已按字符数封顶的 _sequence_digest_block 输出以 est_text 复核；超出份额时丢弃
    **中间**成员行（首末恒保留），并以冻结标记 "…(truncated N members)" 收尾——
    即该块自有的 §10.8 截断约定（标记置于最后一条成员行之后），N 累加到既有标记
    的计数上。

    @param body 摘要正文（每成员一行）
    @param budget_tokens 本槽位可用的 token 份额
    @return (修剪后的正文, 是否发生修剪)
    """
    if budget.est_text(body) <= budget_tokens:
        return body, False
    lines = body.split("\n")
    base = 0
    m = re.match(r"^…\(truncated (\d+) members\)$", lines[-1])
    if m is not None:
        base = int(m.group(1))
        lines = lines[:-1]
    n = len(lines)
    if n > 2:
        for keep_middle in range(n - 3, -1, -1):
            marker = _SEQ_TRUNCATION_MARKER.format(n=base + n - 2 - keep_middle)
            cand = "\n".join(lines[: 1 + keep_middle] + [lines[-1], marker])
            if budget.est_text(cand) <= budget_tokens:
                return cand, True
    return _SEQ_TRUNCATION_MARKER.format(n=base + n), True


def _feed_reactive_terminal(exc: BaseException, metrics) -> None:
    """A7/§7.8 熔断矩阵：只有 reactive-400（响应体嗅探）溢出终局喂致命连续计数。

    每个异常对象恰喂一次（duck 标位守住异常跨算子传递时的重复喂给）；precheck 与
    200 形态的 finish 判据永不喂。``origin`` 取防御式读法（缺省 "http_400"）。

    @param exc 待结算的异常对象
    @param metrics 本次运行的 M12 计数汇（MetricsSink）
    @return 无（副作用为 record_provider_result）
    """
    if (isinstance(exc, ContextOverflowError) and exc.phase == "reactive"
            and getattr(exc, "origin", "http_400") == "http_400"
            and not getattr(exc, "_breaker_fed", False)):
        exc._breaker_fed = True  # type: ignore[attr-defined]
        metrics.record_provider_result(fatal=True)


def _prompt_fit(record: Record, cfg: "ResolvedConfig", ctx: "RunContext",
                schema: dict) -> _PromptFit | None:
    """构造本次分类调用的装填状态。

    返回 None 即预算关（profile 缺失或 context_window == 0——与 v1.10 行为字节
    等价）。schema est 仅在 profile 声明 supports_structured_output 时计入（M9
    咽喉也只在此时随请求发送）。

    @param record 待判决记录
    @param cfg 本次运行的解析配置
    @param ctx 本次（批次, 阶段）运行上下文
    @param schema 本次调用的内部分类 schema
    @return 装填状态；预算关时为 None
    """
    prof = cfg.llm_profiles.get(cfg.classify.llm)
    if prof is None or prof.context_window <= 0:
        return None
    b = budget.input_budget(prof)
    if prof.supports_structured_output:
        b -= budget.est_text(json.dumps(schema, ensure_ascii=False))
    # 单条 UI 记录与 UI episode 都恰携一张图（[屏幕截图] / [首帧截图]）；
    # text 模态不携图。
    cost = ctx.llm.calibrator.cost(prof.name) if record.modality == "ui" else 0
    return _PromptFit(input_budget=b, image_cost=cost)


def _sequence_digest_block(record: Record, cfg: "ResolvedConfig") -> str:
    """§10.8 序列变体的 episode 摘要正文（spec 3.13.3 序列行）。

    按成员序每成员一行——"{m}. {frame_digest(member, segment.digest_max_chars)}"，
    序号 1-based——**总长**以 input.ui_tree_max_chars 封顶。超出上限时整行丢弃
    **中间**行（首末成员恒保留，幸存序号自然暴露缺口），并以冻结标记行
    "…(truncated N members)" 收尾，N = 被省略的成员行数（UITree.serialize 截断约定）。

    @param record 序列记录（episode / thread）
    @param cfg 本次运行的解析配置
    @return 摘要正文文本
    """
    max_chars = cfg.input.ui_tree_max_chars
    lines = [f"{m}. {frame_digest(member, cfg.segment.digest_max_chars)}"
             for m, member in enumerate(record.members, start=1)]
    full = "\n".join(lines)
    if len(full) <= max_chars:
        return full

    n = len(lines)
    # prefix_len[k] = len("\n".join(lines[:k]))——serialize 的前缀和手法。
    prefix_len = [0] * (n + 1)
    for i, line in enumerate(lines):
        prefix_len[i + 1] = prefix_len[i] + (1 if i else 0) + len(line)
    last_len = len(lines[-1])
    # 保留首行、尽可能长的中间行前缀、以及末行；既然已超上限，至少要丢一条中间行，
    # 故保留的中间行数取值域为 [0, n-3]，标记恒收尾。
    for keep_middle in range(n - 3, -1, -1):
        marker = _SEQ_TRUNCATION_MARKER.format(n=n - 2 - keep_middle)
        total = (prefix_len[1 + keep_middle] + 1 + last_len + 1 + len(marker))
        if total <= max_chars:
            return "\n".join(lines[: 1 + keep_middle] + [lines[-1], marker])
    # 退化上限（连首 + 末 + 标记都塞不下，或 n <= 2）：serialize 的最后一档——
    # 单条标记代表全部成员。
    return _SEQ_TRUNCATION_MARKER.format(n=n)


def build_classify_prompt(record: Record, cfg: "ResolvedConfig",
                          with_reason: bool) -> PromptBundle:
    """CONTRACTS §10.8 模板的确定性装配（公开面冻结）。

    system（单/多标签变体头、按 [[classify.classes]] 声明序的类别表、可选的
    classify.instruction 行、带或不带 reason 片段的结构句），随后每条已配置类别
    示例一条 user 消息（类别声明序，再数组序），最后是当前记录的 user 消息——
    文本部件，或 §10.1 同形的三部件「截图 + 控件树」形（R27）。

    v1.8 序列记录（record.kind == "sequence"，spec 3.13.3 序列行）：system 与
    few-shot 消息不变；当前记录消息改为 §10.8 序列变体——[待分类数据·序列] 的
    episode 摘要块，外加（仅 UI 模态——classify 恒在视觉引用集内）[首帧截图]
    标签与首成员截图部件。

    v1.11：冻结签名保持不动——预算路径经私有装配器的尾参 ``fit`` 进入
    （classify_record），绝不经由此处。

    @param record 待判决记录（单条或序列）
    @param cfg 本次运行的解析配置
    @param with_reason 是否索取一句话理由（R29）
    @return 装配好的提示词包
    """
    return _assemble_classify(record, cfg, with_reason)


def _classify_system_message(c: "ClassifyConfig", with_reason: bool) -> Message:
    """装配 §10.8 的 system 消息：变体头 + 类别表 + 可选指令行 + 结构句。

    @param c 已解析的 classify 配置
    @param with_reason 是否索取一句话理由
    @return system 消息
    """
    lines: list[str] = []
    if c.assignment == "single":
        lines.append(_SYSTEM_HEAD_SINGLE)
    else:
        lines.append(_SYSTEM_HEAD_MULTI.format(max_labels=c.max_labels))
    for spec in c.classes:
        lines.append(f"- {spec.name}: {spec.description}")
    if c.instruction:
        lines.append(c.instruction)
    lines.append(_STRUCTURE_SENTENCE)
    if c.assignment == "single":
        lines.append(_STRUCTURE_SINGLE_REASON if with_reason else _STRUCTURE_SINGLE)
    else:
        lines.append(_STRUCTURE_MULTI_REASON if with_reason else _STRUCTURE_MULTI)
    return Message(role="system", parts=(Part(kind="text", text="\n".join(lines)),))


def _classify_example_messages(c: "ClassifyConfig") -> list[Message]:
    """按类别声明序、再数组序展开的类别示例 few-shot 消息。

    @param c 已解析的 classify 配置
    @return few-shot user 消息列表（无示例时为空表）
    """
    messages: list[Message] = []
    for spec in c.classes:
        for example in spec.examples:
            text = _LABEL_EXAMPLE_TMPL.format(name=spec.name, example=example)
            messages.append(Message(role="user",
                                    parts=(Part(kind="text", text=text),)))
    return messages


def _record_slot_budget(fit: _PromptFit, messages: Sequence[Message],
                        record: Record) -> int:
    """v1.11 槽位份额：先把可修剪块**之外**的一切计满，余额归当前记录槽位。

    计满项 = 静态 system 侧 + 标签开销 + 消息包络 + 校准后的图像成本。

    @param fit 本次调用的装填状态
    @param messages 当前记录消息**之前**已就位的消息（system + few-shot）
    @param record 待判决记录
    @return 当前记录槽位的 token 份额（可能为负，由调用方以 0 为下限使用）
    """
    n_messages = len(messages) + 1
    n_images = 1 if record.modality == "ui" else 0
    static_est = (sum(budget.est_text(m.parts[0].text or "") for m in messages)
                  + budget.MSG_OVERHEAD_TOKENS * n_messages
                  + n_images * fit.image_cost)
    return fit.input_budget - static_est


def _sequence_record_parts(record: Record, cfg: "ResolvedConfig",
                           slot_budget: int | None,
                           fit: _PromptFit | None) -> tuple[Part, ...]:
    """v1.8 序列变体（§10.8）的当前记录部件。

    摘要文本部件在先；UI 模态追加 [首帧截图] 标签与**首成员**图像（由 M9 在调用
    期编码）。文本模态的序列只携摘要部件。

    @param record 序列记录
    @param cfg 本次运行的解析配置
    @param slot_budget 当前记录槽位份额；预算关时为 None
    @param fit 本次调用的装填状态；预算关时为 None
    @return 当前记录 user 消息的部件元组
    """
    digest_block = _sequence_digest_block(record, cfg)
    if slot_budget is not None and fit is not None:
        head_est = budget.est_text(f"{_LABEL_RECORD_SEQ}\n")
        if record.modality == "ui":
            head_est += budget.est_text(_LABEL_FIRST_FRAME)
        digest_block, trimmed = _fit_digest_body(
            digest_block, max(0, slot_budget - head_est))
        if trimmed:
            fit.truncations += 1
    parts: tuple[Part, ...] = (
        Part(kind="text", text=f"{_LABEL_RECORD_SEQ}\n{digest_block}"),
    )
    if record.modality == "ui":
        parts += (
            Part(kind="text", text=_LABEL_FIRST_FRAME),
            Part(kind="image", image=record.members[0].image),
        )
    return parts


def _ui_record_parts(record: Record, cfg: "ResolvedConfig",
                     slot_budget: int | None,
                     fit: _PromptFit | None) -> tuple[Part, ...]:
    """UI 模态的当前记录部件：一条 user 消息内的三部件（与 §10.1 同形，R27）。

    @param record 待判决记录
    @param cfg 本次运行的解析配置
    @param slot_budget 当前记录槽位份额；预算关时为 None
    @param fit 本次调用的装填状态；预算关时为 None
    @return 当前记录 user 消息的部件元组
    """
    tree_text = record.ui_tree.serialize(max_chars=cfg.input.ui_tree_max_chars)
    if slot_budget is not None and fit is not None:
        label_est = (budget.est_text(_LABEL_SCREENSHOT)
                     + budget.est_text(f"{_LABEL_UI_TREE}\n"))
        tree_text, trimmed = _fit_tree_text(
            tree_text, max(0, slot_budget - label_est))
        if trimmed:
            fit.truncations += 1
    return (
        Part(kind="text", text=_LABEL_SCREENSHOT),
        Part(kind="image", image=record.image),
        Part(kind="text", text=f"{_LABEL_UI_TREE}\n{tree_text}"),
    )


def _assemble_classify(record: Record, cfg: "ResolvedConfig", with_reason: bool,
                       fit: _PromptFit | None = None) -> PromptBundle:
    """§10.8 装配本体。

    ``fit`` 非 None 时施加 spec 3.13.4 的 v1.11 装填——当前记录槽位份额 =
    fit.input_budget −（system + 类别示例文本 + 记录部件标签开销 + 消息包络 +
    图像成本），全部花在**唯一**可修剪块上；收尾的整篇 est 复核置 fit.overflow
    （V10——由调用方记为拒绝）。fit=None 即与 v1.10 字节等价的路径。

    @param record 待判决记录
    @param cfg 本次运行的解析配置
    @param with_reason 是否索取一句话理由
    @param fit 装填状态；None 为无预算的公开面路径
    @return 装配好的提示词包
    """
    c = cfg.classify
    messages: list[Message] = [_classify_system_message(c, with_reason)]
    messages.extend(_classify_example_messages(c))
    slot_budget = (_record_slot_budget(fit, messages, record)
                   if fit is not None else None)

    parts: tuple[Part, ...]
    if record.kind == "sequence":
        parts = _sequence_record_parts(record, cfg, slot_budget, fit)
    elif record.modality == "text":
        parts = (Part(kind="text", text=f"{_LABEL_RECORD} {record.text}"),)
    else:
        parts = _ui_record_parts(record, cfg, slot_budget, fit)
    messages.append(Message(role="user", parts=parts))

    bundle = PromptBundle(messages=tuple(messages))
    if fit is not None:
        # 与 M9 咽喉同一估算器复核整篇（schema est 已折进 fit.input_budget）：
        # 超出 ⇒ 连最小单元（单条记录）都装不下——V10，调用方拒绝该记录。
        est = budget.est_prompt(bundle, None, None, image_cost=fit.image_cost)
        fit.overflow = est > fit.input_budget
    return bundle


# ── M8 之后的归一（确定性，固定顺序）──────────────────────────────────────────

def _hit_labels(obj: Mapping, assignment: str) -> tuple[str, ...]:
    """取一个 M8 已校验判决对象的原始命中集。

    @param obj 已通过 M8 校验的判决对象
    @param assignment 装配方式 "single" | "multi"
    @return 原始命中的类名元组（未归一）
    """
    if assignment == "single":
        return (obj["class"],)
    return tuple(obj["classes"])


def _normalize_labels(raw: Sequence[str], c: "ClassifyConfig") -> tuple[str, ...]:
    """spec 3.13.4 的归一：①映射到类别表声明序并去重；②兜底类别与具体类别共现时
    丢弃兜底（纯兜底命中则保留）。只会收窄一个已校验的集合（schema 侧刻意不带
    uniqueItems，R1）。

    @param raw 原始命中的类名序列
    @param c 已解析的 classify 配置
    @return 归一后的类名元组（按类别表声明序）
    """
    hit = set(raw)
    ordered = [spec.name for spec in c.classes if spec.name in hit]
    if len(ordered) > 1 and c.fallback_class in ordered:
        ordered = [name for name in ordered if name != c.fallback_class]
    return tuple(ordered)


# ── 记录级判决路径 ───────────────────────────────────────────────────────────

def _classify_prompt(record: Record, ctx: "RunContext", schema: dict,
                     with_reason: bool) -> PromptBundle:
    """装配一次判决的提示词，并施加 v1.11 预算装填（spec 3.13.4 v1.11 行）。

    声明了预算的 profile 让当前记录槽位按派生份额装填（fit=None 保持 v1.10 字节
    等价）；实际修剪次数计入 budget.truncations.classify。

    @param record 待判决记录
    @param ctx 本次（批次, 阶段）运行上下文
    @param schema 本次调用的内部分类 schema
    @param with_reason 是否索取一句话理由
    @return 装配好的提示词包
    @raises ContextOverflowError 连单条记录都装不下（phase="precheck"，V10）
    """
    cfg = ctx.cfg
    fit = _prompt_fit(record, cfg, ctx, schema)
    prompt = _assemble_classify(record, cfg, with_reason, fit=fit)
    if fit is not None:
        if fit.truncations:
            ctx.metrics.count("budget.truncations.classify", fit.truncations)
        if fit.overflow:
            # V10：连最小单元（单条记录）都装不下——绝不发出注定失败的请求；
            # 该记录进 rejects（spec 3.13.4：溢出绕开兜底类别），
            # phase=precheck 永不喂熔断。
            raise ContextOverflowError(
                "classification prompt exceeds the input budget at the minimal "
                "unit (single record)", phase="precheck", profile=cfg.classify.llm)
    return prompt


def _plan_classification(record: Record, ctx: "RunContext",
                         item_ordinal: int) -> _ClassifyPlan:
    """同步冻结一条记录的 schema、prompt 与样本数。

    @param record 待判决记录
    @param ctx 本次运行上下文
    @param item_ordinal 批内输入序
    @return 不含共享写入的样本计划
    """
    c = ctx.cfg.classify
    with_reason = _reason_requested(ctx.cfg)
    names = [spec.name for spec in c.classes]
    schema = classification_schema(names, c.assignment, c.max_labels, with_reason)
    prompt = _classify_prompt(record, ctx, schema, with_reason)
    sample_count = c.self_consistency or 1
    if c.self_consistency:
        prompt = replace(prompt, temperature=c.sc_temperature)
    return _ClassifyPlan(record, item_ordinal, prompt, schema, with_reason,
                         sample_count)


async def _call_classify_sample(plan: _ClassifyPlan,
                                ctx: "RunContext") -> Mapping:
    """执行一个不写 PipelineItem 或 stage 事件的分类样本。

    @param plan 冻结样本计划
    @param ctx 本次运行上下文
    @return M8 已校验的分类对象
    """
    obj, _usage, _attempts, _model = await ctx.schema_engine.complete_validated(
        ctx.cfg.classify.llm, plan.prompt, plan.schema,
        scope=CallScope(record_ids=(plan.record.id,), batch_no=ctx.batch_no))
    return obj


async def _run_classify_sample(plan: _ClassifyPlan,
                               ctx: "RunContext") -> _ClassifySampleOutcome:
    """把普通记录级失败收敛为纯 outcome，保留运行级控制流。

    @param plan 冻结样本计划
    @param ctx 本次运行上下文
    @return 分类对象或普通记录级异常
    """
    try:
        return _ClassifySampleOutcome(await _call_classify_sample(plan, ctx))
    except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
        raise
    except Exception as exc:  # noqa: BLE001 — 普通记录级异常交给 reducer
        _logger.debug("classification sample failed: item=%d exc=%s",
                      plan.item_ordinal, type(exc).__name__,
                      extra={"stage": "classify", "batch": ctx.batch_no})
        return _ClassifySampleOutcome(exc)


def _vote_labels(sample_sets: Sequence[tuple[str, ...]], c: "ClassifyConfig",
                 names: Sequence[str]) -> tuple[tuple[str, ...], dict]:
    """自有投票规则（R26）：在归一后的样本集合上按标签统计出现的集合数，保留出现
    在多于 n/2 个集合中的标签。

    single 装配是同一条规则——每个样本恰贡献一个标签，故"多于 n/2 个集合"正是
    多数票；无多数 ⇒ 落兜底类别（绝不像 annotate 的字段投票那样"取第一个样本"）。

    @param sample_sets 各有效样本归一后的标签集合
    @param c 已解析的 classify 配置
    @param names 类别表声明序的类名列表
    @return (最终标签元组, sc 明细 {"n", "agreement_ratio"})
    """
    n = c.self_consistency
    votes = {name: 0 for name in names}
    for labels_ in sample_sets:
        for label in labels_:
            votes[label] += 1
    kept = tuple(name for name in names if votes[name] * 2 > n)
    final = kept if kept else (c.fallback_class,)
    return final, {"n": n,
                   "agreement_ratio": min(votes[label] for label in final) / n}


def _single_classification(plan: _ClassifyPlan, outcome: _ClassifySampleOutcome,
                           c: "ClassifyConfig") -> Classification:
    """把单样本 outcome 归并为冻结 Classification。

    @param plan 记录级冻结计划
    @param outcome 唯一样本 outcome
    @param c 分类配置
    @return 分类产物
    @raises Exception 样本携带的普通记录级异常
    """
    if isinstance(outcome.value, BaseException):
        raise outcome.value
    labels = _normalize_labels(_hit_labels(outcome.value, c.assignment), c)
    detail = {"reason": outcome.value["reason"]} if plan.with_reason else {}
    return Classification(label=labels[0], labels=labels, source="llm",
                          detail=detail)


def _sc_classification(plan: _ClassifyPlan,
                       outcomes: Sequence[_ClassifySampleOutcome],
                       c: "ClassifyConfig") -> Classification:
    """按样本输入序归并自洽投票，完成序不参与语义。

    @param plan 记录级冻结计划
    @param outcomes 按 sample ordinal 排列的 outcome
    @param c 分类配置
    @return 投票后的分类产物
    @raises SchemaViolation 所有样本均弃权
    @raises Exception 首个样本序普通记录级异常
    """
    sample_sets: list[tuple[str, ...]] = []
    reasons: list[str] = []
    last_violation: SchemaViolation | None = None
    for outcome in outcomes:
        value = outcome.value
        if isinstance(value, SchemaViolation):
            last_violation = value
        elif isinstance(value, BaseException):
            raise value
        else:
            sample_sets.append(_normalize_labels(_hit_labels(value, c.assignment), c))
            if plan.with_reason:
                reasons.append(value["reason"])
    if not sample_sets:
        raise last_violation if last_violation is not None else SchemaViolation(
            ["self-consistency: all samples failed"], "")
    names = [spec.name for spec in c.classes]
    final, sc_detail = _vote_labels(sample_sets, c, names)
    detail = {"sc": sc_detail}
    if plan.with_reason:
        detail["reason"] = reasons[0]
    return Classification(label=final[0], labels=final, source="llm", detail=detail)


def _reduce_classification(plan: _ClassifyPlan,
                           outcomes: Sequence[_ClassifySampleOutcome],
                           c: "ClassifyConfig") -> Classification:
    """按 sample ordinal 把叶结果归并为一条记录级判决。

    @param plan 记录级冻结计划
    @param outcomes 与计划样本一一对应的输入序结果
    @param c 分类配置
    @return 分类产物
    """
    if c.self_consistency == 0:
        return _single_classification(plan, outcomes[0], c)
    return _sc_classification(plan, outcomes, c)


async def classify_record(record: Record, ctx: "RunContext") -> Classification:
    """单条公开调用面：复用 planner/leaf/reducer，样本按序执行。

    @param record 待判决记录（单条或序列）
    @param ctx 本次（批次, 阶段）运行上下文
    @return 判决产物
    @raises SchemaViolation M8 修复穷尽（自洽形态下为全部样本均失败）
    @raises ProviderRetryableError provider 可重试错误且重试耗尽
    @raises ProviderFatalError provider 不可重试错误
    @raises ContextOverflowError 最小单元不可装填（phase="precheck"，V10）
    """
    plan = _plan_classification(record, ctx, 0)
    outcomes = []
    for _sample_ordinal in range(plan.sample_count):
        outcomes.append(await _run_classify_sample(plan, ctx))
    return _reduce_classification(plan, outcomes, ctx.cfg.classify)


# ── v1.12 帧级批量判决（SPEC-frame-annotation §3.2） ─────────────────────────

def build_frame_classify_prompt(members: Sequence[Record], cfg: "ResolvedConfig",
                                digests: Sequence[str]) -> PromptBundle:
    """§10.12 帧级批量判决模板的确定性装配（公开面冻结）。

    system：_FRAME_SYSTEM_HEAD（{N} 代入窗内成员数）+ 帧类表行 "- name: description"
    （[[frame.classify.classes]] 声明序）+ 结构句 + _FRAME_STRUCTURE 输出契约。
    user：单条消息——[会话成员帧] 文本部件承载 1-based 摘要行（``digests`` 与
    ``members`` 对齐，调用方按 segment.digest_max_chars 预计算，segment V9 同款——
    装配器自身永不计算摘要）；frame_classify.vision_resolved 时每成员追加
    "[成员 i 截图]" 文本标签 + image part（工作点 = profile 图像工作点，M9 编码期
    生效——镜像 segment 窗口判决的视觉形态）。

    v1.11 形态延续：冻结签名不动——预算路径经私有装配器 _assemble_frame_classify
    的尾参 ``fit`` 进入（build_classify_prompt 的「公开面冻结 + 私有 fit 尾参」同款）。

    @param members 窗内成员帧记录（按帧序）
    @param cfg 本次运行的解析配置
    @param digests 与 ``members`` 对齐的成员摘要（调用方预计算）
    @return 装配好的提示词包
    """
    return _assemble_frame_classify(members, cfg, digests)


def _assemble_frame_classify(members: Sequence[Record], cfg: "ResolvedConfig",
                             digests: Sequence[str],
                             fit: _PromptFit | None = None) -> PromptBundle:
    """§10.12 装配本体；``fit`` 非 None 只做整篇 est 复核置 fit.overflow（窗口装填
    本身即 fit——内容修剪不适用：窗内容量由 pack_windows 先行保证，残余溢出仅来自
    强制 2 帧兜底窗，走 precheck 跳过），fit=None 为字节等价的公开面路径。

    @param members 窗内成员帧记录（按帧序）
    @param cfg 本次运行的解析配置
    @param digests 与 ``members`` 对齐的成员摘要
    @param fit 装填状态；None 为无预算的公开面路径
    @return 装配好的提示词包
    """
    fc = cfg.frame_classify
    n = str(len(members))
    lines = [_FRAME_SYSTEM_HEAD.replace("{N}", n)]
    for spec in fc.classes:
        lines.append(f"- {spec.name}: {spec.description}")
    lines.append(_STRUCTURE_SENTENCE)
    lines.append(_FRAME_STRUCTURE.replace("{N}", n))
    messages: list[Message] = [
        Message(role="system", parts=(Part(kind="text", text="\n".join(lines)),))]

    body = "\n".join(_FRAME_MEMBER_LINE.format(m=m, digest=digest)
                     for m, digest in enumerate(digests, start=1))
    parts: list[Part] = [Part(kind="text", text=f"{_LABEL_FRAME_MEMBERS}\n{body}")]
    if fc.vision_resolved:
        for m, member in enumerate(members, start=1):
            if member.image is None:               # 防御：无图成员只留摘要行
                continue
            parts.append(Part(kind="text",
                              text=_LABEL_MEMBER_SCREENSHOT.format(i=m)))
            parts.append(Part(kind="image", image=member.image))
    messages.append(Message(role="user", parts=tuple(parts)))
    bundle = PromptBundle(messages=tuple(messages))
    if fit is not None:
        # 与 M9 咽喉同一估算器复核整篇（schema est 已折进 fit.input_budget）：
        # 超出 ⇒ 本窗即最小单元不可装填，调用方走 precheck 跳过（不喂熔断）。
        est = budget.est_prompt(bundle, None, None, image_cost=fit.image_cost)
        fit.overflow = est > fit.input_budget
    return bundle


def _frame_prompt_fit(cfg: "ResolvedConfig", ctx: "RunContext",
                      schema: dict) -> _PromptFit | None:
    """_prompt_fit 的帧级镜像：None = 预算关（profile 缺失或 context_window == 0）。

    schema est 仅在 profile 声明 supports_structured_output 时计入（M9 咽喉只在
    此时随请求发送）；图像成本 = vision_resolved 时的校准器批冻结读数（V19）。

    @param cfg 本次运行的解析配置
    @param ctx 本次（批次, 阶段）运行上下文
    @param schema 本窗的定长枚举数组 schema
    @return 装填状态；预算关时为 None
    """
    prof = cfg.llm_profiles.get(cfg.frame_classify.llm)
    if prof is None or prof.context_window <= 0:
        return None
    b = budget.input_budget(prof)
    if prof.supports_structured_output:
        b -= budget.est_text(json.dumps(schema, ensure_ascii=False))
    cost = (ctx.llm.calibrator.cost(prof.name)
            if cfg.frame_classify.vision_resolved else 0)
    return _PromptFit(input_budget=b, image_cost=cost)


def _frame_static_est(cfg: "ResolvedConfig", prof, n_members: int) -> int:
    """帧级提示词静态部件的 est（装填条件的 est_static_system 项，segment
    _static_prompt_est 同族）：system 全文（{N} 取未代入常量形，代入量由 margin
    吸收）+ 两条消息包络 + [会话成员帧] 标签行 + （structured output 时）以最大
    可能窗 n_members 评估的 schema est 上界。

    @param cfg 本次运行的解析配置
    @param prof 帧分类所用的 [llm.*] profile
    @param n_members 最大可能窗的成员数（schema est 上界的评估口径）
    @return 静态部件的 token est
    """
    fc = cfg.frame_classify
    lines = [_FRAME_SYSTEM_HEAD]
    for spec in fc.classes:
        lines.append(f"- {spec.name}: {spec.description}")
    lines.append(_STRUCTURE_SENTENCE)
    lines.append(_FRAME_STRUCTURE)
    est = (budget.est_text("\n".join(lines)) + 2 * budget.MSG_OVERHEAD_TOKENS
           + budget.est_text(f"{_LABEL_FRAME_MEMBERS}\n"))
    if prof.supports_structured_output:
        names = [spec.name for spec in fc.classes]
        schema = frame_classify_schema(names, n_members)
        est += budget.est_text(json.dumps(schema, ensure_ascii=False))
    return est


def _frame_windows(members: Sequence[Record], digests: Sequence[str],
                   cfg: "ResolvedConfig", ctx: "RunContext") -> list[tuple[int, int]]:
    """预算分窗（pack_windows 的零重叠调用形，SPEC §3.2 调用形态）。

    per-frame 成本 = est_text(摘要) + 最坏序号前缀 + （vision 时）最坏截图标签 +
    批冻结图像成本；帧分类无窗口上限键 ⇒ cap = 成员总数（预算是唯一切分力）。
    pack_windows 的跨度链自带 1 帧重叠（后窗首帧 = 前窗末帧，M14 缝帧语义）——
    帧分类是不重叠切分：按其 docstring 约定的零重叠调用形，自第二窗起丢弃与前窗
    重叠的首帧（[start+1, end)），前窗持有缝帧判决；所得跨度两两不交且完整覆盖。

    @param members 本 episode 的全部成员帧记录
    @param digests 与 ``members`` 对齐的成员摘要
    @param cfg 本次运行的解析配置
    @param ctx 本次（批次, 阶段）运行上下文
    @return 零重叠的窗口跨度列表 [(start, end), ...]
    """
    fc = cfg.frame_classify
    prof = cfg.llm_profiles[fc.llm]
    image_cost = ctx.llm.calibrator.cost(fc.llm) if fc.vision_resolved else 0
    ordinal = budget.est_text(_FRAME_MEMBER_LINE.format(m=len(members), digest=""))
    label_worst = (budget.est_text(_LABEL_MEMBER_SCREENSHOT.format(i=len(members)))
                   if fc.vision_resolved else 0)
    costs = [budget.est_text(digest) + ordinal + label_worst + image_cost
             for digest in digests]
    pack_budget = (budget.input_budget(prof)
                   - _frame_static_est(cfg, prof, len(members)))
    spans = budget.pack_windows(costs, pack_budget, len(members))
    return [(start + 1, end) if idx else (start, end)
            for idx, (start, end) in enumerate(spans)]


async def _judge_frame_window(window_members: Sequence[Record],
                              window_digests: Sequence[str], ctx: "RunContext",
                              ids: tuple[str, ...]) -> list[str | None]:
    """一窗一调用——经 complete_validated(schema=frame_classify_schema(names, n))。

    位次对齐后校验在本函数内（first-wins 家族）：labels 数组按位置对齐窗内成员序，
    超长截断（保留前 n 项）、缺项补 None（调用方落 fallback_class）。溢出 precheck
    在派发前自查（fit.overflow ⇒ 掷 phase="precheck"，永不发出注定失败的请求）。

    @param window_members 窗内成员帧记录（按帧序）
    @param window_digests 与 ``window_members`` 对齐的成员摘要
    @param ctx 本次（批次, 阶段）运行上下文
    @param ids 本次调用的事件归属记录 id 元组
    @return 与窗内成员一一对齐的标签表（缺项为 None）
    @raises ContextOverflowError 本窗即最小单元仍不可装填（phase="precheck"）
    """
    cfg = ctx.cfg
    fc = cfg.frame_classify
    names = [spec.name for spec in fc.classes]
    n = len(window_members)
    schema = frame_classify_schema(names, n)
    fit = _frame_prompt_fit(cfg, ctx, schema)
    prompt = _assemble_frame_classify(window_members, cfg, window_digests, fit=fit)
    if fit is not None and fit.overflow:
        raise ContextOverflowError(
            "frame classification prompt exceeds the input budget at the "
            "minimal window", phase="precheck", profile=fc.llm)
    obj, _usage, _attempts, _model = await ctx.schema_engine.complete_validated(
        fc.llm, prompt, schema,
        scope=CallScope(record_ids=ids, batch_no=ctx.batch_no))
    raw: list[str | None] = list(obj["labels"])[:n]
    return raw + [None] * (n - len(raw))


async def _judge_frames_degrading(
        judge, span: tuple[int, int], stats: _FrameRunStats, *,
        level: int = 0) -> list[tuple[tuple[int, int], list[str | None]]]:
    """V20 对半降级重试的帧级镜像（segment._judge_span_degrading 同形，零重叠版）。

    反应式 ContextOverflowError ⇒ 窗口对半重切 [s, m) / [m, e)（m = 中点；帧分类
    窗口零重叠，切分不保缝帧）、顺序执行（确定性熔断记账）、每次对半计
    budget.degrade_retries，至多 _MAX_FRAME_DEGRADE_LEVELS 级。终止（不再切分）：
    非 reactive 相位（precheck = 最小单元不可装填，不可降级）、单帧窗（< 2 帧
    切不出两个非空半窗）、级数耗尽——异常原样上抛，由调用方按窗失败兜底；
    熔断喂给在调用方吞点经 _feed_reactive_terminal 恰一次执行（A7 纪律）。

    @param judge 单窗判决协程工厂（接受跨度，返回按位标签表）
    @param span 本级的窗口跨度 [start, end)
    @param stats 当前叶任务的局部调用统计
    @param level 当前对半级数（内部递归用，外部恒取默认 0）
    @return 叶结果列表 [(跨度, 标签表)]
    @raises ContextOverflowError 不可再降级时原样上抛
    """
    start, end = span
    try:
        return [(span, await judge(span))]
    except ContextOverflowError as exc:
        if (exc.phase != "reactive" or end - start < 2
                or level >= _MAX_FRAME_DEGRADE_LEVELS):
            raise
        stats.degrade_retries += 1
        mid = (start + end) // 2
        results = await _judge_frames_degrading(judge, (start, mid), stats,
                                                level=level + 1)
        results.extend(await _judge_frames_degrading(judge, (mid, end), stats,
                                                     level=level + 1))
        return results


def _frame_failure_kind(exc: BaseException) -> str:
    """窗口失败留痕的 kind 归类：§7.6 零新 kind——预算词表先行（V27① 同序），
    修复穷尽复用 classification_invalid（M13 自有词表），其余落既有供应商/内部
    kind。仅入 Classification.detail 留痕（R4 哲学下推），不产 error 事件。

    @param exc 窗口失败的异常对象
    @return §7.6 的 kind 字符串
    """
    kind = budget.classify_stage_error(exc)
    if kind is not None:
        return kind
    if isinstance(exc, SchemaViolation):
        return ErrorKind.CLASSIFICATION_INVALID.value
    if isinstance(exc, ProviderRetryableError):
        return ErrorKind.PROVIDER_RETRYABLE_EXHAUSTED.value
    if isinstance(exc, ProviderFatalError):
        return ErrorKind.PROVIDER_FATAL.value
    return ErrorKind.INTERNAL_ERROR.value


def _fold_window_outcomes(outcomes, ctx: "RunContext") -> tuple[dict, dict]:
    """把各窗叶结果折叠为（位置→标签, 位置→失败留痕）。

    窗口跨度零重叠 ⇒ 写入互不覆盖，折叠结果与调度顺序无关；失败窗计
    window_failures 并按 A7 纪律恰一次喂熔断（仅 reactive-400 终局；precheck 与
    200 形终局永不喂）。

    @param outcomes 各窗的叶结果列表（元素为 [(跨度, 标签表 | 异常)]）
    @param ctx 本次（批次, 阶段）运行上下文
    @return (位置→标签 表, 位置→失败留痕 表)
    """
    aligned: dict[int, str] = {}
    detail: dict[int, dict] = {}
    for leaves in outcomes:
        for (start, end), got in leaves:
            if isinstance(got, BaseException):
                _feed_reactive_terminal(got, ctx.metrics)
                ctx.metrics.count(_COUNTER_FRAME_WINDOW_FAILURES)
                kind = _frame_failure_kind(got)
                for i in range(start, end):
                    detail[i] = {"kind": kind, "message": str(got)}
            else:
                for i, label in enumerate(got):
                    if label is not None:
                        aligned[start + i] = label
    return aligned, detail


def _assemble_frame_results(members: Sequence[Record], fc, aligned: dict,
                            detail: dict) -> tuple[dict[str, Classification], int]:
    """按成员序落判决表：命中 ⇒ source="llm"；缺位（窗失败或对齐缺项）⇒
    fallback_class + source="fallback"（窗失败者带 kind/message 留痕，对齐缺项
    detail 为空）。

    @param members 本 episode 的全部成员帧记录
    @param fc 已解析的 frame_classify 配置
    @param aligned 位置→标签 表
    @param detail 位置→失败留痕 表
    @return (成员判决表 {member.id: Classification}, fallback 帧数)
    """
    result: dict[str, Classification] = {}
    fallback = 0
    for i, member in enumerate(members):
        if member.id in result:
            # 成员 id 是内容哈希，episode 内字节相同的帧同 id（ingest D2 已知
            # 碰撞面）——first-wins：同 id 同产物，后位次不覆盖、不重复计数
            # （终审缺陷修复；温度 0 下同内容判决本就应一致）。
            continue
        label = aligned.get(i)
        if label is None:
            fallback += 1
            result[member.id] = Classification(
                label=fc.fallback_class, labels=(fc.fallback_class,),
                source="fallback", detail=detail.get(i, {}))
        else:
            result[member.id] = Classification(label=label, labels=(label,),
                                               source="llm", detail={})
    return result, fallback


async def classify_frames(members: Sequence[Record],
                          ctx: "RunContext") -> dict[str, Classification]:
    """对给定成员 Record 序列做帧级闭集批量判决，返回 {member.id: Classification}
    （source ∈ {"llm", "fallback"}）。

    预算声明时按 budget.pack_windows 的零重叠调用形分窗（预算关 ⇒ 单窗全成员）；
    单窗修复穷尽/不可恢复 ⇒ 该窗全部成员落 frame_classify.fallback_class（v1.7
    fallback 哲学下推），本函数永不抛出记录级异常（大三样除外）。PUBLIC
    DIRECT-CALL SURFACE：M7 verify 的成员回收补跑直接调用本函数（单成员回收即
    单元素调用），CONTRACTS §1.1 算子间导入白名单第四向——judge_window（§7.14）
    同款契约地位的 sanctioned import exception。

    @param members 待判决的成员帧记录序列（按帧序）
    @param ctx 本次（批次, 阶段）运行上下文
    @return 成员判决表 {member.id: Classification}
    """
    plan = _plan_frame_episode(members, ctx, members[0].id, 0)
    outcomes = []
    for span in plan.spans:
        outcomes.append(await _run_frame_plan(plan, span, ctx))
    result, _windows, _fallback = _reduce_frame_plan(plan, outcomes, ctx)
    return result


def _plan_frame_episode(members: Sequence[Record], ctx: "RunContext",
                        episode_id: str, item_ordinal: int) -> _FrameEpisodePlan:
    """同步冻结一条 episode 的摘要、预算态与原始窗口。

    @param members episode 成员序列
    @param ctx 本次运行上下文
    @param episode_id stage 事件归属 id
    @param item_ordinal 批内输入序
    @return 帧窗口计划
    """
    frozen_members = tuple(members)
    digests = tuple(frame_digest(member, ctx.cfg.segment.digest_max_chars)
                    for member in frozen_members)
    prof = ctx.cfg.llm_profiles.get(ctx.cfg.frame_classify.llm)
    budget_on = prof is not None and prof.context_window > 0
    spans = (_frame_windows(frozen_members, digests, ctx.cfg, ctx) if budget_on
             else [(0, len(frozen_members))])
    return _FrameEpisodePlan(item_ordinal, episode_id, frozen_members, digests,
                             tuple(spans), budget_on)


async def _run_frame_plan(plan: _FrameEpisodePlan, span: tuple[int, int],
                          ctx: "RunContext") -> _FrameWindowOutcome:
    """执行一个纯原始帧窗口叶任务，普通失败收敛为 outcome。

    @param plan episode 冻结计划
    @param span 原始窗口跨度
    @param ctx 本次运行上下文
    @return 窗口叶结果与局部调用统计
    """
    stats = _FrameRunStats()

    async def judge(current: tuple[int, int]) -> list[str | None]:
        """执行降级树中的一次局部窗调用并累计叶内统计。

        @param current 当前局部窗口跨度
        @return 与局部窗口成员序对齐的标签表
        """
        stats.calls += 1
        start, end = current
        return await _judge_frame_window(plan.members[start:end],
                                         plan.digests[start:end], ctx,
                                         (plan.episode_id,))

    try:
        if plan.budget_on:
            leaves = await _judge_frames_degrading(judge, span, stats)
        else:
            leaves = [(span, await judge(span))]
    except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
        raise
    except Exception as exc:  # noqa: BLE001 — 普通窗口失败由 reducer 兜底
        _logger.warning("frame classification window failed: span=%d-%d exc=%s",
                        span[0], span[1], type(exc).__name__,
                        extra={"stage": "classify", "batch": ctx.batch_no})
        leaves = [(span, exc)]
    return _FrameWindowOutcome(tuple(leaves), stats.calls, stats.degrade_retries)


def _reduce_frame_plan(plan: _FrameEpisodePlan,
                       outcomes: Sequence[_FrameWindowOutcome],
                       ctx: "RunContext") -> tuple[dict[str, Classification], int, int]:
    """按 window ordinal 归并一条 episode 的纯叶结果。

    @param plan episode 冻结计划
    @param outcomes 与原始窗口一一对应的输入序 outcome
    @param ctx 本次运行上下文
    @return (成员判决表, 实际调用数, fallback 帧数)
    """
    calls = sum(outcome.calls for outcome in outcomes)
    degrade_retries = sum(outcome.degrade_retries for outcome in outcomes)
    if calls:
        ctx.metrics.count(_COUNTER_FRAME_CALLS, calls)
    if degrade_retries:
        ctx.metrics.count(_COUNTER_DEGRADE_RETRIES, degrade_retries)
    aligned, detail = _fold_window_outcomes(
        [outcome.leaves for outcome in outcomes], ctx,
    )
    result, fallback = _assemble_frame_results(
        plan.members, ctx.cfg.frame_classify, aligned, detail,
    )
    if fallback:
        ctx.metrics.count(_COUNTER_FRAME_FALLBACK, fallback)
    return result, calls, fallback


# ── stage ────────────────────────────────────────────────────────────────────

class ClassifyStage:
    """M13 classify 阶段的 Stage 实现（spec 3.13.2）。

    一批内先做序列级闭集判决，再（v1.12）做帧级批量判决，最后在 multi 装配下
    同步扇出兄弟信封（Stage 契约 ②a）。
    """

    name = "classify"

    def __init__(self, cfg: "ResolvedConfig"):
        """绑定本次运行的解析配置。

        @param cfg 本次运行的不可变解析配置（M1 产物）
        """
        self.cfg = cfg

    async def run(self, batch: list[PipelineItem],
                  ctx: "RunContext") -> list[PipelineItem]:
        """执行本批的序列级判决、帧级判决与 multi 扇出。

        @param batch 本批信封列表（唯一可变载体）
        @param ctx 本次（批次, 阶段）运行上下文
        @return 传入的同一列表对象（契约 ②a）
        """
        todo: list[PipelineItem] = []
        if self.cfg.classify.enabled:
            todo = [item for item in batch
                    if item.status == "active" and item.classification is None]
        plans = self._plan_sequence_wave(todo, ctx)
        sample_outcomes = await self._run_sequence_wave(plans, ctx)
        self._reduce_sequence_wave(todo, plans, sample_outcomes, ctx)

        frame_plans = self._plan_frame_wave(batch, ctx)
        frame_outcomes = await self._run_frame_wave(frame_plans, ctx)
        self._reduce_frame_wave(frame_plans, frame_outcomes, ctx)
        if todo and self.cfg.classify.assignment == "multi":
            self._pin_shared_annotations(todo)
            self._fan_out(batch, todo)
        return batch

    def _plan_sequence_wave(self, todo: list[PipelineItem],
                            ctx: "RunContext") -> list[_ClassifyPlan | BaseException]:
        """按 item ordinal 同步冻结第一波样本计划。

        @param todo 待判决信封输入序
        @param ctx 本次运行上下文
        @return 与 todo 对齐的计划或同步计划异常
        """
        plans: list[_ClassifyPlan | BaseException] = []
        for item_ordinal, item in enumerate(todo):
            try:
                plans.append(_plan_classification(item.record, ctx, item_ordinal))
            except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as exc:  # noqa: BLE001 — reducer 施加记录级策略
                _logger.debug("classification planning failed: item=%d exc=%s",
                              item_ordinal, type(exc).__name__,
                              extra={"stage": self.name, "batch": ctx.batch_no})
                plans.append(exc)
        return plans

    async def _run_sequence_wave(
            self, plans: Sequence[_ClassifyPlan | BaseException],
            ctx: "RunContext") -> tuple[_ClassifySampleOutcome, ...]:
        """经共享 TaskExecutor 执行全批第一波分类样本。

        @param plans 与待判决信封对齐的计划表
        @param ctx 本次运行上下文
        @return 按 item/sample ordinal 排列的纯 outcome
        """
        specs: list[TaskSpec[_ClassifySampleOutcome]] = []
        for plan in plans:
            if isinstance(plan, BaseException):
                continue
            for sample_ordinal in range(plan.sample_count):
                specs.append(TaskSpec(
                    task_id=(f"{ctx.task_namespace}:classify:sample:"
                             f"{plan.item_ordinal}:{sample_ordinal}"),
                    declaration_key=(ctx.batch_no, 3, 0, plan.item_ordinal,
                                     sample_ordinal),
                    stage=self.name,
                    resource_key=("llm", self.cfg.classify.llm),
                    operation=lambda plan=plan: _run_classify_sample(plan, ctx),
                ))
        return await ctx.tasks.run_group(TaskGroupRequest(tuple(specs)))

    def _reduce_sequence_wave(
            self, todo: list[PipelineItem],
            plans: Sequence[_ClassifyPlan | BaseException],
            outcomes: Sequence[_ClassifySampleOutcome], ctx: "RunContext") -> None:
        """按 item/sample ordinal 提交分类、失败、计数与事件。

        @param todo 待判决信封输入序
        @param plans 与 todo 对齐的计划或同步计划异常
        @param outcomes 扁平输入序样本结果
        @param ctx 本次运行上下文
        @return 无
        """
        offset = 0
        for item, plan in zip(todo, plans):
            if isinstance(plan, BaseException):
                self._settle_classification(item, plan, (), ctx)
                continue
            selected = outcomes[offset:offset + plan.sample_count]
            offset += plan.sample_count
            self._settle_classification(item, plan, selected, ctx)

    def _settle_classification(
            self, item: PipelineItem, plan: _ClassifyPlan | BaseException,
            outcomes: Sequence[_ClassifySampleOutcome], ctx: "RunContext") -> None:
        """同步提交一条记录的分类或既有失败策略。

        @param item 目标信封
        @param plan 冻结计划或同步计划异常
        @param outcomes 该记录的输入序样本结果
        @param ctx 本次运行上下文
        @return 无
        """
        try:
            if isinstance(plan, BaseException):
                raise plan
            classification = _reduce_classification(plan, outcomes,
                                                     self.cfg.classify)
        except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 — 记录级隔离由 reducer 落盘
            classification = self._dispose_failure(item, ctx, exc)
            if classification is None:
                return
        item.classification = classification
        self._register(item, ctx, classification)

    def _pin_shared_annotations(self, todo: list[PipelineItem]) -> None:
        """扇出共享裁决的时序补丁（终审缺陷修复）：帧标注 pass 在 M5 才运行，
        克隆若在此共享 None，M5 对原信封的「None ⇒ 重绑新 dict」将使克隆永远
        看不到帧标注——对将要扇出的首标签序列信封先钉住共享容器 {}（M5 只补
        缺位、从不换对象）。降格信封除外：其帧 pass 恒跳过，dict 保持 None =
        「未运行」语义（emitter 在场规则的单一真相）。

        @param todo 本批已完成序列级判决的信封列表
        @return 无（副作用为钉住共享的 member_annotations 容器）
        """
        if not self.cfg.frame_annotate.enabled:
            return
        for item in todo:
            cls = item.classification
            if (cls is not None and len(cls.labels) >= 2
                    and item.record.kind == "sequence"
                    and item.member_annotations is None
                    and getattr(item, "segment_degraded", None) is None):
                item.member_annotations = {}

    def _plan_frame_wave(
            self, batch: list[PipelineItem],
            ctx: "RunContext") -> list[tuple[PipelineItem, _FrameEpisodePlan]]:
        """在 sequence reducer 后按稳定分类冻结第二波 episode 窗口。

        @param batch 已完成序列级提交的批次
        @param ctx 本次运行上下文
        @return 首标签信封与对应帧计划
        """
        if not self.cfg.frame_classify.enabled:
            return []
        return [
            (item, _plan_frame_episode(item.record.members, ctx,
                                       item.record.id, item_ordinal))
            for item_ordinal, item in self._frame_gate(batch, ctx)
        ]

    async def _run_frame_wave(
            self, plans: Sequence[tuple[PipelineItem, _FrameEpisodePlan]],
            ctx: "RunContext") -> tuple[_FrameWindowOutcome, ...]:
        """经共享 TaskExecutor 执行全批第二波原始帧窗口。

        @param plans 输入序 episode 计划
        @param ctx 本次运行上下文
        @return 按 episode/window ordinal 排列的纯窗口 outcome
        """
        specs: list[TaskSpec[_FrameWindowOutcome]] = []
        for _item, plan in plans:
            for window_ordinal, span in enumerate(plan.spans):
                specs.append(TaskSpec(
                    task_id=(f"{ctx.task_namespace}:classify:frame:"
                             f"{plan.item_ordinal}:{window_ordinal}"),
                    declaration_key=(ctx.batch_no, 3, 1, plan.item_ordinal,
                                     window_ordinal),
                    stage=self.name,
                    resource_key=("llm", self.cfg.frame_classify.llm),
                    operation=(lambda plan=plan, span=span:
                               _run_frame_plan(plan, span, ctx)),
                ))
        return await ctx.tasks.run_group(TaskGroupRequest(tuple(specs)))

    def _reduce_frame_wave(
            self, plans: Sequence[tuple[PipelineItem, _FrameEpisodePlan]],
            outcomes: Sequence[_FrameWindowOutcome], ctx: "RunContext") -> None:
        """按 episode/window ordinal 提交 member map、计数与事件。

        @param plans 输入序 episode 计划
        @param outcomes 扁平输入序窗口结果
        @param ctx 本次运行上下文
        @return 无
        """
        offset = 0
        for item, plan in plans:
            selected = outcomes[offset:offset + len(plan.spans)]
            offset += len(plan.spans)
            result, windows, fallback = _reduce_frame_plan(plan, selected, ctx)
            item.member_classifications = result
            ctx.metrics.event(
                _EV_FRAME, stage=self.name, batch_no=ctx.batch_no,
                record_ids=(plan.episode_id,),
                payload={"members": len(plan.members), "windows": windows,
                         "fallback": fallback},
            )

    def _frame_gate(self, batch: list[PipelineItem],
                    ctx: "RunContext") -> list[tuple[int, PipelineItem]]:
        """帧级 pass 执行门（SPEC §3.2）：active ∧ kind=="sequence" ∧ 首标签信封
        （克隆判据 classification.label != classification.labels[0]，verify S8
        同款；classification 为 None 视同非克隆——克隆恒携 classification）∧
        幂等门 member_classifications 缺位 ∧ 非降格——segment_degraded duck 标
        在场 ⇒ 计 frame_classify.skipped_degraded 并跳过（降格 = 噪声未剔，
        不为垃圾帧付费，降格会话跳过裁决）。

        @param batch 本批信封列表
        @param ctx 本次（批次, 阶段）运行上下文
        @return (批内位置, 信封) 输入序列表
        """
        todo: list[tuple[int, PipelineItem]] = []
        for item_ordinal, item in enumerate(batch):
            if item.status != "active" or item.record.kind != "sequence":
                continue
            cls = item.classification
            if cls is not None and cls.labels and cls.label != cls.labels[0]:
                continue
            if item.member_classifications is not None:
                continue
            if getattr(item, "segment_degraded", None) is not None:
                ctx.metrics.count(_COUNTER_FRAME_SKIPPED_DEGRADED)
                continue
            todo.append((item_ordinal, item))
        return todo

    def _dispose_failure(self, item: PipelineItem, ctx: "RunContext",
                         exc: BaseException) -> Classification | None:
        """按异常类型施加 on_error 处置（分派顺序即 v1.11 V27① 的词表顺序）。

        SchemaViolation：on_error="fail" 时把最后一次模型原始输出经 emitter 读取
        的 duck 通道转交 M11 的 rejects "full" 档（§9.2）；否则记录以兜底类别存活。
        预算词表**先行**——精确 kind、记录级 failed → rejects（spec 3.13.4 v1.11
        行：溢出绕开 on_error 兜底类别），reactive-400 终局恰喂一次熔断（A7）。

        @param item 目标信封
        @param ctx 本次（批次, 阶段）运行上下文
        @param exc 判决路径抛出的异常
        @return 兜底判决产物；已置 failed 时为 None（调用方直接收手）
        """
        if isinstance(exc, SchemaViolation):
            if self.cfg.classify.on_error == "fail":
                item.raw_last_output = exc.raw_last_output  # type: ignore[attr-defined]
                self._fail(item, ctx, ErrorKind.CLASSIFICATION_INVALID.value,
                           str(exc), retryable=False)
                return None
            return self._fallback(item, ctx, str(exc))
        if isinstance(exc, (ContextOverflowError, OutputTruncatedError)):
            _feed_reactive_terminal(exc, ctx.metrics)
            self._fail(item, ctx, budget.classify_stage_error(exc), str(exc),
                       retryable=False)
        elif isinstance(exc, ProviderRetryableError):
            self._fail(item, ctx, ErrorKind.PROVIDER_RETRYABLE_EXHAUSTED.value,
                       str(exc), retryable=True)
        elif isinstance(exc, ProviderFatalError):
            self._fail(item, ctx, ErrorKind.PROVIDER_FATAL.value, str(exc),
                       retryable=False)
        else:
            kind = (ErrorKind.IMAGE_DECODE_ERROR.value
                    if item.record.modality == "ui" and isinstance(exc, OSError)
                    else ErrorKind.INTERNAL_ERROR.value)
            self._fail(item, ctx, kind, f"{type(exc).__name__}: {exc}",
                       retryable=False)
        return None

    def _fallback(self, item: PipelineItem, ctx: "RunContext",
                  message: str) -> Classification:
        """on_error="fallback"（R4）：记录以兜底类别存活。

        取证进 Classification.detail，**绝不**进 item.errors（rejects 归属读的是
        errors[0]）；同时发出 error trace 事件并计 classify.fallback。

        @param item 目标信封
        @param ctx 本次（批次, 阶段）运行上下文
        @param message 英文错误消息（取自异常）
        @return 兜底判决产物
        """
        kind = ErrorKind.CLASSIFICATION_INVALID.value
        ctx.metrics.count(_COUNTER_FALLBACK)
        ctx.metrics.event(_EV_ERROR, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(item.record.id,),
                          payload={"stage": self.name, "kind": kind,
                                   "message": message, "retryable": False})
        fallback = self.cfg.classify.fallback_class
        return Classification(label=fallback, labels=(fallback,), source="fallback",
                              detail={"kind": kind, "message": message})

    def _register(self, item: PipelineItem, ctx: "RunContext",
                  classification: Classification) -> None:
        """计数器 + 每条记录一发的 classify.decision 事件。

        兜底判决也在内——决策事件对**每条**已判决记录都发（§7.13）。

        @param item 目标信封
        @param ctx 本次（批次, 阶段）运行上下文
        @param classification 已落定的判决产物
        @return 无（副作用为计数与事件）
        """
        for label in classification.labels:        # 按标签逐一计数（multi 全计）
            ctx.metrics.count(_COUNTER_CLASSES_PREFIX + label)
        if len(classification.labels) >= 2:
            ctx.metrics.count(_COUNTER_MULTI_LABEL)
        payload: dict = {"label": classification.label}
        if self.cfg.classify.assignment == "multi":
            payload["labels"] = list(classification.labels)
        payload["source"] = classification.source
        if "reason" in classification.detail:
            payload["reason"] = classification.detail["reason"]
        if "sc" in classification.detail:
            payload["sc"] = dict(classification.detail["sc"])
        ctx.metrics.event(_EV_DECISION, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(item.record.id,), payload=payload)

    def _fail(self, item: PipelineItem, ctx: "RunContext", kind: str, message: str,
              retryable: bool) -> None:
        """把记录级失败落进信封：errors + status='failed' + 计数 + error 事件。

        @param item 目标信封
        @param ctx 本次（批次, 阶段）运行上下文
        @param kind §7.6 的错误分类码
        @param message 英文错误消息（不含数据内容）
        @param retryable 该错误是否属可重试类
        @return 无（副作用为状态写入、计数与事件）
        """
        err = StageError(stage=self.name, kind=kind, message=message,
                         retryable=retryable)
        item.errors.append(err)
        item.status = "failed"
        if kind == ErrorKind.CONTEXT_OVERFLOW.value:
            ctx.metrics.count("budget.overflow_records")   # V13②：拒绝即计，全相位
        ctx.metrics.count(_COUNTER_FAILURES)
        ctx.metrics.event(_EV_ERROR, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(item.record.id,),
                          payload={"stage": self.name, "kind": kind,
                                   "message": message, "retryable": retryable})

    @staticmethod
    def _fan_out(batch: list[PipelineItem], processed: list[PipelineItem]) -> None:
        """归一命中集 k ≥ 2 时的兄弟信封扇出（Stage 契约 ②a）。

        原信封已携首标签；其余每个标签克隆一个兄弟信封追加到批尾。克隆**按引用**
        共享 record 与 dedup，并继承 session_id（v1.8：兄弟 episode 对 M7 的边界
        余量 / 邻域查询保持可寻址，spec 3.13.4）与 thread_id（v1.9 T14：这是真字段
        ——线索身份属于 record 而非信封）；classification 只换 label（labels 仍是
        同一完整集合）；scores/annotation/verification/errors 均为全新默认容器
        （spec 3.13.4）。v1.12（扇出共享裁决）：member_classifications /
        member_annotations 与 record/dedup 同族按引用共享——帧产物描述成员帧本身
        而非信封路由，克隆行渲染同一 dict；帧级两 pass 只在首标签信封执行
        （克隆判据 label != labels[0]），克隆自身永不重跑。

        @param batch 本批信封列表（克隆追加至其尾部）
        @param processed 本批已完成序列级判决的信封列表
        @return 无（副作用为向 batch 追加克隆信封）
        """
        for item in processed:
            classification = item.classification
            if classification is None or len(classification.labels) < 2:
                continue
            for label in classification.labels[1:]:
                clone = PipelineItem(
                    record=item.record,
                    status="active",
                    classification=replace(classification, label=label),
                    dedup=item.dedup,
                    session_id=item.session_id,
                    thread_id=item.thread_id,
                    member_classifications=item.member_classifications,
                    member_annotations=item.member_annotations,
                )
                # v1.8（D6）：session_split / segment_degraded 描述的是 **episode**
                # 的会话与切分，不是信封——兄弟行不得与原信封的 _meta.stream 矛盾。
                # v1.9（T14）：M16 的标位一并入列——seam_indexes 驱动兄弟自己的
                # extract pass，seam_interrupted_by 供其占位文本，stitch_fragments
                # 供其 _meta.stream.fragments 与 annotate 配额。
                for mark in ("session_split", "segment_degraded", "seam_indexes",
                             "seam_interrupted_by", "stitch_fragments"):
                    value = getattr(item, mark, None)
                    if value is not None:
                        setattr(clone, mark, value)
                batch.append(clone)

"""M5 标注算子（spec 3.5，CONTRACTS.md §7.4）。

确定性提示词装配（任务指令 + few-shot + 记录内容；ui 模态追加截图与序列化控件树）、
把结构保证委派给 M8（``SchemaEngine.complete_validated``）、可选的自洽采样与字段级
多数投票（spec 3.5.2），以及 M7 verify 使用的公开修复面。

v1.8 序列标注（S5/S6/S28，CONTRACTS §10.1 序列变体）：episode 信封
（``record.kind == "sequence"``）把「当前记录」用户消息换成 ① [动作序列] 步骤行
（transitions 为 None 时整段省略）→ ② 每个保留关键帧的
``[关键帧 {i}/{k}·成员 {m}]`` 文本 + 图像（确定性均匀降采样到
annotate.sequence_frames；text 模态序列跳过 ②）→ ③ **恒在**的收尾 [成员帧摘要]
文本段。模板不变式（S6）：最后一段恒为 ③ 文本段——修复后缀直接拼到
``parts[-1].text``，修复侧代码零改动。transitions 是 ``AnnotatePromptOptions`` 上
继 v1.7 label 之后的取值；None 让 v1.8 之前的每个调用点字节等价。v1.9（T14）再加
fragment_lens——线索（thread）的逐片段关键帧配额（每个片段至少保留一个关键帧）；
None 保持 v1.8 的均匀降采样。

v1.12 帧级逐帧标注（SPEC-frame-annotation §3.3）：process 序列在自身标注成功后追加
逐成员帧 pass；v1.18 sequence attempt 在序列标注关闭时直接执行同一 pass，序列标注调用
精确为零。公开直调面 ``annotate_member`` 填充 ``item.member_annotations``——修复面族的
新成员（M7 verify 的成员回收补跑懒加载直调它）。帧调用把 ``cfg.frame_schema`` 显式路由
进 ``complete_validated(schema=...)``：内部 Schema 待遇——无 L2.5、不计 resolved_at。

按序列类标注 Schema：
某个类可经 ``[class.<name>.annotate].schema_path/schema_inline`` 覆盖全局
``output.schema``。``class_annotate_schema`` 是**单点**取值函数（label →
``cfg.class_views[label].schema``；label 缺失、类表外的未知类或无覆盖的类一律回落
全局 Schema），本模块每个 Schema 消费点都经它取值——两处标注调用、提示词 Schema
文本、自洽投票与预算装填计价——保证「计价的 Schema 就是调用的 Schema」。按类
Schema 的调用显式路由 ``schema=<类 Schema>`` 且 ``CallScope(user_treatment=True)``
（裁决·M8 显式待遇参数）：记录级标注恒属用户待遇族，L2.5 与 resolved_at 记账保留。
未配置任何按类 Schema 时，统一使用全局 Schema。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Mapping, Sequence

from labelkit.common.config._temporal import inject_temporal_values, project_temporal_instance
from labelkit.common.errors import (
    CircuitBreakerTripped,
    ContextOverflowError,
    ErrorKind,
    InternalError,
    OutputTruncatedError,
    ProviderFatalError,
    ProviderRetryableError,
    SchemaViolation,
)
from labelkit.common.contracts.execution import TaskGroupRequest, TaskSpec
from labelkit.common.contracts.generation import (
    DownstreamAttemptRequest, DownstreamAttemptResult, SequenceTemporalContext,
)
from labelkit.common.contracts.types import (
    Annotation,
    PipelineItem,
    Record,
    StageError,
    Transition,
    Usage,
    frame_digest,
)

from labelkit.common.inference import budget
from labelkit.common.inference.llm_client import Message, Part, PromptBundle
from labelkit.common.inference.schema_engine import (
    CallScope, CandidateFinalizerContractError, FinalizedCallRequest, _thaw_json,
)

if TYPE_CHECKING:
    from labelkit.common.config.model import AnnotateConfig, LLMProfile, ResolvedConfig
    from labelkit.common.contracts.stage import RunContext


EV_ANNOTATE_DONE = "annotate.done"
EV_ANNOTATE_FRAME = "annotate.frame"   # v1.12：帧级逐帧标注事件（每成员一发，前缀
                                       # 自动路由既有 annotate 通道，§7.2 目录加行）
EV_ERROR = "error"

_logger = logging.getLogger("labelkit.annotate")
_ATTEMPT_MODE: ContextVar[bool] = ContextVar("annotate_attempt_mode", default=False)
_ATTEMPT_CONFIG: ContextVar[object | None] = ContextVar(
    "annotate_attempt_config", default=None
)

# 中文提示词片段——逐字取自 CONTRACTS.md §10.1/§10.5（spec 3.5.2/3.7.3）。
_SCHEMA_SENTENCE = "输出必须是符合以下 JSON Schema 的单个 JSON 对象，不输出任何其他内容："
_LABEL_EXAMPLE_IN = "[示例输入]"
_LABEL_EXAMPLE_OUT = "[示例输出]"
_LABEL_TEXT_RECORD = "[待标注数据]"
_LABEL_SCREENSHOT = "[屏幕截图]"
_LABEL_UI_TREE = "[UI 控件树]"
_LABEL_PREV_OUTPUT = "[上一版标注]"
_LABEL_CRITIQUES = "[审核意见]"
_REPAIR_TAIL = "请修正后重新输出"

# v1.8 序列变体片段（CONTRACTS §10.1 序列变体，S5/S6）。
_LABEL_ACTION_SEQUENCE = "[动作序列]"
_LABEL_MEMBER_DIGESTS = "[成员帧摘要]"

# v1.12 帧级标注模板片段（SPEC-frame-annotation §3.3；实现后 verbatim 捕进
# CONTRACTS §10.13）。[任务] 标出生效指令段；[成员帧] 是 text 模态成员内容行的
# 标签（ui 模态复用上方单记录三段形的 [屏幕截图]/[UI 控件树]）。
_FRAME_LABEL_TASK = "[任务]"
_FRAME_LABEL_MEMBER = "[成员帧]"
# 跨层等式载体：帧级系统侧完整静态脚手架（[任务] 标签 + Schema 约束句——生效
# 指令与帧 Schema 文本是配置量，在 M1 静态预算预检（V13③）各自计量）。
# budget.TEMPLATE_HEAD_TOKENS["frame_annotate"] 钉住 est_text(本常量)，
# tests/common/inference/test_budget.py 的跨层等式测试守护两侧同步。
_FRAME_SYSTEM_STATIC = f"{_FRAME_LABEL_TASK}\n{_SCHEMA_SENTENCE}"

# 算子模块之间互不依赖（spec §2.2）：M4 quality 自持一份同格式的步骤行模板（外加
# （摘取兜底）兜底后缀）；此处是 M5 自己的副本。
_MEMBER_DIGEST_MAX_CHARS = 400   # 单成员 frame_digest 上限（segment.digest_max_chars 默认值）


def _step_line(transition: Transition) -> str:
    """渲染一条 [动作序列] 行（§10.1 冻结格式）。

    @param transition 该步骤的 Transition；action 的 target/value 为 null 时渲染 "—"
    @return 形如 ``{index}. {action_type}（对象: {target|—}；值: {value|—}）{description}``
        的单行文本。标注证据**不**带（摘取兜底）后缀——那个 S16 分隔标记只属于 M4 的
        评分段。
    """
    action = transition.action
    target = action.get("target")
    value = action.get("value")
    return (f"{transition.index}. {action.get('action_type')}"
            f"（对象: {'—' if target is None else target}；"
            f"值: {'—' if value is None else value}）"
            f"{action.get('description')}")


def _keyframe_indexes(n: int, k: int,
                      fragment_lens: Sequence[int] | None = None) -> list[int]:
    """S28 确定性降采样：在 n 个成员上按上限 k 选出关键帧下标。

    @param n 成员总数
    @param k 关键帧上限（annotate.sequence_frames 或其外部收窄值）
    @param fragment_lens v1.9（T14）线索的逐片段成员数（成员元组序；片段是会话序的
        连续块）；None = v1.8 均匀降采样
    @return 严格递增的成员下标列表；n <= k 时保留全部成员

    n > k 时 ``idx_i = i*(n-1)//(k-1)``（i = 0..k-1）——纯整数运算、零随机、首末帧恒
    保留、无重复。给出 fragment_lens 时升级为逐片段配额，保证**每个**片段至少保留
    一个关键帧（均匀采样会把小片段整段抽干，minor-8）：m 个片段各得 1 个，再按
    (Lᵢ − 1) 加权用最大余数法分配 k − m 的余量（同余数取小下标）；片段内部跑同一个
    S28 均匀公式（配额为 1 时保留该片段的**首**成员——末片段保留其**末**成员，故
    全局首末不变式成立）。fragment_lens 缺失/单片段/与 n 不自洽，或 k < m（至少一帧
    不可行）时，退化回 v1.8 均匀路径。
    """
    if n <= k:
        return list(range(n))
    if (not fragment_lens or len(fragment_lens) <= 1
            or sum(fragment_lens) != n or len(fragment_lens) > k):
        return [i * (n - 1) // (k - 1) for i in range(k)]
    m = len(fragment_lens)
    extra_total = k - m
    weight_total = n - m                       # Σ (Lᵢ − 1) ≥ 1，因为 n > k ≥ m
    base = [(length - 1) * extra_total // weight_total for length in fragment_lens]
    remainders = [(length - 1) * extra_total % weight_total
                  for length in fragment_lens]
    leftover = extra_total - sum(base)
    granted = set(sorted(range(m), key=lambda i: (-remainders[i], i))[:leftover])
    out: list[int] = []
    start = 0
    for i, length in enumerate(fragment_lens):
        quota = 1 + base[i] + (1 if i in granted else 0)
        if quota == 1:
            picks = [length - 1] if i == m - 1 else [0]
        else:
            picks = [j * (length - 1) // (quota - 1) for j in range(quota)]
        out.extend(start + p for p in picks)
        start += length
    return out


def _member_digest_lines(members: tuple[Record, ...], max_total_chars: int) -> list[str]:
    """渲染 [成员帧摘要] 的各行：每成员一行 ``{m}. {frame_digest(member, 400)}``。

    @param members episode 的成员帧元组（m 从 1 起，按成员序）
    @param max_total_chars 全部行合计的字符上限（input.ui_tree_max_chars）
    @return 摘要行列表；超上限时首末行**恒**保留，中间条目整行丢弃并就地替换为一条
        ``…(truncated N members)`` 标记行（serialize/§10.8 的截断约定）
    """
    lines = [f"{m}. {frame_digest(member, _MEMBER_DIGEST_MAX_CHARS)}"
             for m, member in enumerate(members, start=1)]
    if len(lines) <= 2 or len("\n".join(lines)) <= max_total_chars:
        return lines
    last = lines[-1]
    keep = 1                 # 即便触底超预算，首行也要活下来
    for k in range(len(lines) - 2, 0, -1):
        marker = f"…(truncated {len(lines) - k - 1} members)"
        if len("\n".join(lines[:k] + [marker, last])) <= max_total_chars:
            keep = k
            break
    marker = f"…(truncated {len(lines) - keep - 1} members)"
    return lines[:keep] + [marker, last]


@dataclass(frozen=True)
class RepairContext:
    """M7 verify 重标注时透传的修复上下文（§10.5 修复后缀的两个载体）。"""

    previous_output: Mapping                       # 上一版标注对象
    critiques_text: str                            # 渲染后的审核意见行 "aspect: opinion"
                                                   # （多评委形："judge_name/aspect: opinion"）


@dataclass(frozen=True)
class AnnotatePromptOptions:
    """一次标注调用的装配变体参数（CONTRACTS §7.4 两个公开面的收拢入参形）。

    ``build_annotate_prompt`` / ``annotate_record`` 都以本对象承载全部变体取值；
    模块内部的装配器、装填器与调用器传的也是同一个对象（``dataclasses.replace``
    做逐档改写，如 V20 折半与 V21 修复梯）。
    """

    repair: RepairContext | None = None            # §10.5 修复上下文；None = 首次标注
    temperature: float | None = None               # 采样温度；None = profile 默认
    label: str | None = None                       # 分类标签，同时选择按类 Schema
    transitions: tuple[Transition, ...] | None = None   # v1.8 [动作序列] 步骤；None = 整段省略
    fragment_lens: tuple[int, ...] | None = None   # v1.9（T14）逐片段成员数；None = 均匀降采样
    k_eff: int | None = None                       # v1.11（V20/V21）关键帧上限的外部收窄值
    image_px: int | None = None                    # v1.11（V23①）升档后的图像采样边长
    temporal_context: SequenceTemporalContext | None = None  # v1.20 同一最终 sequence 冻结时间上下文


_DEFAULT_PROMPT_OPTIONS = AnnotatePromptOptions()


def _dumps(obj: object) -> str:
    """@return 按提示词口径序列化的非 ASCII 单行 JSON。"""
    return json.dumps(_thaw_json(obj), ensure_ascii=False)


# ── 按序列类标注 Schema ───────────────────────────────────────────────────
#
# 三个取值函数是本特性在 M5 侧的单点真相：调用 Schema、提示词 Schema 文本、
# 预算计价与自洽投票全部经此取值，保证「计价的 Schema 就是调用的 Schema」。
# M7 verify 的 V21 试装经懒加载复用同一组函数；M11 emitter 不得跨算子导入，
# 按 spec §2.2 在其内部做最小镜像（语义须与 class_annotate_schema 一致）。


@dataclass(frozen=True)
class _AnnotationFinalizer:
    """一次 sequence annotation 的冻结时间注入器。"""

    paths: tuple[str, ...]                       # 完整 Schema 中的时间路径
    values: tuple[tuple[str, object], ...]        # 路径到最早 resource start
    context: SequenceTemporalContext              # 本次调用显式复用的唯一冻结上下文

    def __call__(self, candidate: Mapping[str, object]) -> Mapping[str, object]:
        """注入 annotation 时间且拒绝任何非时间改写。"""
        projected = project_temporal_instance(candidate, self.paths)
        if _canonical_mapping(projected) != _canonical_mapping(candidate):
            raise ValueError("annotation candidate contains a business time field")
        finalized = inject_temporal_values(candidate, dict(self.values))
        if _canonical_mapping(project_temporal_instance(finalized, self.paths)) \
                != _canonical_mapping(candidate):
            raise ValueError("annotation finalizer changed a non-time field")
        return finalized


@dataclass(frozen=True)
class _AnnotationProjector:
    """annotation L3 只读可达时间叶子的 total 投影器。"""

    paths: tuple[str, ...]                       # 需删除的业务时间路径

    def __call__(self, candidate: Mapping[str, object]) -> Mapping[str, object]:
        """@return 不创建或替换 parent 的 model-space 副本。"""
        return project_temporal_instance(candidate, self.paths)


def _canonical_mapping(value: Mapping[str, object]) -> str:
    """把 mapping 转为稳定字节等价文本。"""
    return json.dumps(_thaw_json(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _class_view(cfg: "ResolvedConfig", label: str | None):
    """读取标签的冻结类视图；非法标签不猜测。"""
    return None if label is None else cfg.class_views.get(label)

def class_annotate_schema(cfg: "ResolvedConfig",
                          label: str | None) -> Mapping | None:
    """取该记录所属序列类的标注 Schema 覆盖。

    @param cfg 已解析配置
    @param label 记录分类标签
    @return 类 Schema；无标签、未知类或无覆盖时为 None
    """
    if label is None:
        return None
    view = cfg.class_views.get(label)
    return None if view is None else view.schema


def class_effective_schema(cfg: "ResolvedConfig", label: str | None) -> Mapping:
    """类有效标注 Schema = 按类覆盖 ?? 全局 ``output.schema``。

    @param cfg 已解析配置
    @param label 记录分类标签
    @return 自洽投票与预算计价共用的有效 Schema
    """
    override = class_annotate_schema(cfg, label)
    return cfg.user_schema if override is None else override


def class_effective_model_schema(cfg: "ResolvedConfig", label: str | None) -> Mapping:
    """取一条标注调用的 provider-facing model Schema。

    @param cfg 已解析配置
    @param label 序列类标签
    @return 投影后或既有的有效 Schema
    """
    view = _class_view(cfg, label)
    if view is None or not view.time_bindings:
        return class_effective_schema(cfg, label)
    if view.model_schema is None:
        _annotation_contract_error("annotation model schema is missing")
    return view.model_schema


def class_schema_text(ctx: "RunContext", label: str | None) -> str:
    """提示词内嵌的类有效 Schema 文本（canonical 单行 dump）。

    @param ctx 运行上下文
    @param label 记录分类标签
    @return provider-facing Schema 文本
    """
    view = _class_view(ctx.cfg, label)
    if view is not None and view.time_bindings:
        return json.dumps(_thaw_json(class_effective_model_schema(ctx.cfg, label)),
                          ensure_ascii=False, separators=(", ", ": "))
    override = class_annotate_schema(ctx.cfg, label)
    if override is None:
        return ctx.schema_engine.user_schema_text
    return json.dumps(_thaw_json(override), ensure_ascii=False, separators=(", ", ": "))


def _annotation_time_values(record: Record, cfg: "ResolvedConfig", opts: AnnotatePromptOptions):
    """从同一冻结 temporal context 解析 annotation 机械值。"""
    view = _class_view(cfg, opts.label)
    if view is None or not view.time_bindings:
        return {}
    context = opts.temporal_context
    if not isinstance(context, SequenceTemporalContext) or record.kind != "sequence":
        _annotation_contract_error("annotation temporal context is missing")
    if tuple(member.event_id for member in context.members) != tuple(member.id for member in record.members):
        _annotation_contract_error("annotation temporal context differs from sequence members")
    values: dict[str, object] = {}
    for binding in view.time_bindings:
        values[binding.payload_path] = _first_resource_start(context, binding)
    if tuple(values) != view.business_time_paths:
        _annotation_contract_error("annotation binding order differs from its Schema paths")
    return context, values


def _first_resource_start(context: SequenceTemporalContext, binding) -> int:
    """读取一个 resource 的最早正区间毫秒起点。"""
    if binding.source != "first_resource_start_milliseconds" or not binding.resource:
        _annotation_contract_error("annotation time binding source is invalid")
    starts = []
    for member in context.members:
        valid = (isinstance(member.timestamp_us, int) and not isinstance(member.timestamp_us, bool)
                 and isinstance(member.duration_us, int) and not isinstance(member.duration_us, bool)
                 and member.timestamp_us % 1000 == 0 and member.duration_us >= 0
                 and member.duration_us % 1000 == 0)
        if not valid:
            _annotation_contract_error("annotation temporal context contains an invalid interval")
        if binding.resource in member.resources:
            if member.duration_us == 0:
                _annotation_contract_error("annotation resource interval is not positive")
            starts.append(member.timestamp_us)
    if not starts:
        _annotation_contract_error("annotation temporal context lacks its resource interval")
    return min(starts) // 1000


def _annotation_transforms(record: Record, cfg: "ResolvedConfig", opts: AnnotatePromptOptions
                           ) -> tuple[_AnnotationFinalizer, _AnnotationProjector]:
    """为一条序列标注构造同 context 的冻结变换对。"""
    view = _class_view(cfg, opts.label)
    paths = () if view is None else view.business_time_paths
    context, values = _annotation_time_values(record, cfg, opts)
    return _AnnotationFinalizer(paths, tuple(values.items()), context), _AnnotationProjector(paths)


def _project_repair_options(cfg: "ResolvedConfig", opts: AnnotatePromptOptions) -> AnnotatePromptOptions:
    """从 M7 外层 repair prompt 的 previous_output 删除机械时间。"""
    if opts.repair is None:
        return opts
    view = _class_view(cfg, opts.label)
    paths = () if view is None else view.business_time_paths
    previous = project_temporal_instance(opts.repair.previous_output, paths)
    return replace(opts, repair=replace(opts.repair, previous_output=previous))


async def _complete_annotation(ctx: "RunContext", record: Record, prompt: PromptBundle, opts: AnnotatePromptOptions,
                               ) -> tuple[dict, Usage, int, str]:
    """把一次记录级标注调用路由进 M8。

    @param ctx 运行上下文
    @param record 被标注记录
    @param prompt 已装配提示词
    @param opts 类标签、repair 与冻结时间上下文
    @return M8 成功四元组
    """
    profile = ctx.cfg.annotate.llm
    scope = CallScope(record_ids=(record.id,), batch_no=ctx.batch_no,
                      record=record.raw)
    view = _class_view(ctx.cfg, opts.label)
    if view is not None and view.time_bindings:
        finalizer, projector = _annotation_transforms(record, ctx.cfg, opts)
        try:
            return await ctx.schema_engine.complete_finalized(FinalizedCallRequest(
                profile=profile,
                prompt=prompt,
                model_schema=class_effective_model_schema(ctx.cfg, opts.label),
                final_schema=class_effective_schema(ctx.cfg, opts.label),
                scope=replace(scope, user_treatment=True),
                candidate_finalizer=finalizer,
                repair_projector=projector,
            ))
        except CandidateFinalizerContractError:
            _annotation_contract_error("annotation candidate finalizer contract failed")
    schema = class_annotate_schema(ctx.cfg, opts.label)
    if schema is None:
        return await ctx.schema_engine.complete_validated(profile, prompt,
                                                          scope=scope)
    return await ctx.schema_engine.complete_validated(
        profile, prompt, schema=dict(schema),
        scope=replace(scope, user_treatment=True))


def _annotation_contract_error(reason: str):
    """以 generation downstream 终态结束时间标注契约破坏。"""
    _logger.error("generation_downstream_contract: %s", reason)
    raise InternalError(f"generation_downstream_contract: {reason}")


# ── v1.11 上下文预算装填（spec 3.5.2 上下文预算装填与修复升级换档）───────────

_TREE_MARKER_RE = re.compile(r"^…\(truncated (\d+) nodes\)$")


@dataclass
class _PackState:
    """单次装配的裁剪指令（spec 3.5.2 v1.11 段，份额定序 ④）：可裁文本块各自的
    token 预算——None = 本次装配不裁该块。V25③ 的不可裁块（修复的 [上一版标注]/
    [审核意见] 后缀）与 ① 静态系统侧（指令 / 用户 Schema / few-shot）绝不出现在
    这里——它们只被装箱器**计量**，从不裁剪。"""

    step_budget: int | None = None     # [动作序列] 块体（边缘裁剪，§3.3⑤）
    digest_budget: int | None = None   # [成员帧摘要] 块体（同族边缘裁剪）
    tree_budget: int | None = None     # 单记录 ui 控件树渲染（§3.3③）
    truncations: int = 0               # 本次装配累计的裁剪次数（计入 budget.truncations.*）


@dataclass(frozen=True)
class _PackScale:
    """④ 裁剪阶段的度量口径快照（本次装配的即时读数，spec 3.5.2 v1.11 段）。"""

    profile: str        # 归属 profile 名（V10 溢出信号的载体）
    limit: int          # 输入预算上限（budget.input_budget）
    schema_est: int     # Schema 文本计量（结构化输出关闭时为 0）
    text_est: int       # 本次装配的文本侧计量
    image_cost: int     # 单图成本（无图时 0）
    k_fin: int          # ③ 定档后的关键帧数（无图时 0）


def _feed_reactive_terminal(exc: BaseException, metrics) -> None:
    """A7/§7.8 熔断矩阵：**只有**反应式 400（响应体嗅探）溢出终态喂连续致命计数。

    @param exc 待判定的异常（非反应式 400 一律不喂）
    @param metrics 指标汇（record_provider_result 入口）
    @return 无

    每个异常对象恰喂一次——duck 标位防同一异常穿越算子时重复喂（例如 M7→M5 修复
    链）；precheck 与 200 形态的终止判据永不喂。``origin`` 防御性读取（默认
    "http_400"），等 errors.py 修订到位。
    """
    if (isinstance(exc, ContextOverflowError) and exc.phase == "reactive"
            and getattr(exc, "origin", "http_400") == "http_400"
            and not getattr(exc, "_breaker_fed", False)):
        exc._breaker_fed = True  # type: ignore[attr-defined]
        metrics.record_provider_result(fatal=True)


def _fit_tree_text(rendered: str, budget_tokens: int) -> tuple[str, bool]:
    """§3.3③ 序列化控件树的动态帽。

    @param rendered 已在 input.ui_tree_max_chars 绝对上限之下的树渲染文本
    @param budget_tokens 本次装配分给树的 token 份额
    @return (裁剪后的文本, 是否发生裁剪)

    超份额时从尾部丢 NODE 行，并以 serialize 族标记 "…(truncated N nodes)" 收尾——
    N 累加到已有标记的计数上。est_text 对前缀单调 ⇒ 用二分求最大保留行数。
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
        """构造「保留前 keep 行 + 收尾标记」的候选文本。

        @param keep 保留的 NODE 行数
        @return 候选树文本
        """
        marker = f"…(truncated {base + total - keep} nodes)"
        return "\n".join(lines[:keep] + [marker])

    lo, hi = 0, total - 1                        # keep == total 已知塞不下
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if budget.est_text(candidate(mid)) <= budget_tokens:
            lo = mid
        else:
            hi = mid - 1
    return candidate(lo), True


def _fit_block(body: str, share: int | None) -> tuple[str, int]:
    """一次 §3.3⑤ 边缘裁剪：保留首末行、整行丢弃中段、就地插入
    "…(truncated N lines)" 标记。

    @param body 待裁块体
    @param share 该块的 token 份额；None = 不裁
    @return (裁剪后的块体, 本次裁剪次数 0/1)
    """
    if share is None or budget.est_text(body) <= share:
        return body, 0
    return budget.fit_text(body, max(0, share), keep="edges"), 1


# ── §10.1 提示词装配（公开面签名按 CONTRACTS §7.4 冻结）──────────────────────
#
# 段序固定：system（任务指令 + Schema 约束句 + Schema 文本）→ 每条 few-shot 一条
# user 消息（配置序）→ 当前记录 user 消息（text 一段，或 ui 的截图 + 控件树三段）。
# 序列记录（record.kind == "sequence"，判定先于模态）走 S6 段序 ① [动作序列] →
# ② 保留关键帧（text 标签 + 图像；S28 降采样到 annotate.sequence_frames；text 模态
# 跳过）→ ③ **恒在**的收尾 [成员帧摘要] 文本段，故 parts[-1] 恒为文本段，§10.5
# 修复后缀直接拼到其 text 上，修复侧代码零改动（S6 模板不变式）。

def build_annotate_prompt(record: Record, cfg: "ResolvedConfig", schema_text: str,
                          opts: AnnotatePromptOptions = _DEFAULT_PROMPT_OPTIONS,
                          ) -> PromptBundle:
    """按 CONTRACTS.md §10.1（+ §10.5 修复后缀）确定性装配标注提示词。

    @param record 待标注记录（单记录或 v1.8 序列 episode）
    @param cfg 已解析配置
    @param schema_text 类有效 Schema 文本（``class_schema_text`` 取值：无按类覆盖时
        即 M8 的 ``SchemaEngine.user_schema_text`` 属性）
    @param opts 装配变体参数（``AnnotatePromptOptions``）：``repair`` §10.5 修复上
        下文；``temperature`` 采样温度；``label`` v1.7（R2）分类标签，非 None ⇒
        指令/few-shot 取 ``cfg.class_views[label].annotate``；``transitions``
        v1.8（S5）[动作序列] 步骤源，None = 整段省略；``fragment_lens`` v1.9（T14）
        逐片段关键帧配额（每片段至少保留一帧），None = 均匀降采样；``k_eff``
        v1.11（V20/V21）**生效关键帧上限**，② 的降采样按 k =
        min(annotate.sequence_frames, k_eff) 跑；``image_px`` v1.11（V23①）升档
        分辨率，随 ``PromptBundle.image_px`` 下传（M9 构建器算生效 px = image_px
        or profile.default_image_px or profile.max_image_px，再截到
        min(·, max_image_px)）。缺省对象即 v1.7 之前的全局无变体装配
    @return 装配好的 PromptBundle

    预算装填本身走私有装配器的尾参 ``fit``（在 annotate_record 内），绝不在此。
    """
    return _assemble_prompt(record, cfg, schema_text, opts)


def _assemble_prompt(record: Record, cfg: "ResolvedConfig", schema_text: str,
                     opts: AnnotatePromptOptions,
                     fit: _PackState | None = None) -> PromptBundle:
    """§10.1 装配体。

    @param record 待标注记录
    @param cfg 已解析配置
    @param schema_text 类有效 Schema 文本
    @param opts 装配变体参数
    @param fit 非 None 时应用 spec 3.5.2 v1.11 ④ 的文本块裁剪（步骤行/成员摘要走边缘
        裁剪，单记录树走动态帽）——份额定序由 _pack_prompt 负责；None = 装填前的字节
        等价路径（预算关，或本次装配本就塞得下）
    @return 装配好的 PromptBundle
    """
    opts = _project_repair_options(cfg, opts)
    acfg = cfg.class_views[opts.label].annotate if opts.label is not None else cfg.annotate
    messages = _prelude_messages(acfg, schema_text)
    if record.kind == "sequence":              # v1.8 序列变体（判定先于模态）
        parts = _sequence_parts(record, cfg, opts, fit)
    else:
        parts = _single_record_parts(record, cfg, fit)
    if opts.repair is not None:
        parts = _with_repair_suffix(parts, opts.repair)
    messages.append(Message(role="user", parts=parts))
    return PromptBundle(messages=tuple(messages), temperature=opts.temperature,
                        image_px=opts.image_px)


def _prelude_messages(acfg: "AnnotateConfig", schema_text: str) -> list[Message]:
    """装配 system 段 + 每条 few-shot 一条 user 消息（§10.1 固定段序的前半）。

    @param acfg 生效的标注配置（全局 [annotate] 或 v1.7 按类视图）
    @param schema_text 类有效 Schema 文本
    @return 消息列表（调用方随后追加当前记录消息）
    """
    system_text = (f"{acfg.instruction}\n"
                   f"{_SCHEMA_SENTENCE}\n"
                   f"{schema_text}")
    messages: list[Message] = [
        Message(role="system", parts=(Part(kind="text", text=system_text),))]
    for example in acfg.examples:
        example_text = (f"{_LABEL_EXAMPLE_IN} {example.input}\n"
                        f"{_LABEL_EXAMPLE_OUT} {_dumps(example.output)}")
        messages.append(Message(role="user", parts=(Part(kind="text", text=example_text),)))
    return messages


def _effective_k_cap(cfg: "ResolvedConfig", k_eff: int | None) -> int:
    """算出本次装配的生效关键帧上限。

    @param cfg 已解析配置（annotate.sequence_frames 是配置侧上限）
    @param k_eff 外部收窄值；None = 直接用配置值
    @return min(配置上限, max(2, k_eff))——外部帽与配置值取小（§7.4），并落在 V10 的
        最小单元 2 上：每个受认可的载体（V20 折半、V21 梯、§3.3⑥③ 装填）本就以 2
        触底，且 k=1 没有降采样形态
    """
    cap = cfg.annotate.sequence_frames
    return cap if k_eff is None else min(cap, max(2, k_eff))


def _sequence_parts(record: Record, cfg: "ResolvedConfig", opts: AnnotatePromptOptions,
                    fit: _PackState | None) -> tuple[Part, ...]:
    """装配 v1.8 序列变体的 ①②③ 段（S6 段序；末段恒为 ③ 文本段）。

    @param record kind == "sequence" 的 episode 记录
    @param cfg 已解析配置
    @param opts 装配变体参数
    @param fit ④ 裁剪指令；None = 不裁
    @return 当前记录 user 消息的 parts 元组
    """
    parts: list[Part] = []
    if opts.transitions is not None:           # ① transitions 为 None 时整段省略
        steps = "\n".join(_step_line(t) for t in opts.transitions)
        if fit is not None:
            steps, trims = _fit_block(steps, fit.step_budget)
            fit.truncations += trims
        parts.append(Part(kind="text", text=f"{_LABEL_ACTION_SEQUENCE}\n{steps}"))
    if record.modality == "ui":                # ② text 模态序列退化为 ① + ③
        kept = _keyframe_indexes(len(record.members),
                                 _effective_k_cap(cfg, opts.k_eff),
                                 opts.fragment_lens)
        k = len(kept)
        for i, m_idx in enumerate(kept, start=1):
            member = record.members[m_idx]
            parts.append(Part(kind="text", text=f"[关键帧 {i}/{k}·成员 {m_idx + 1}]"))
            parts.append(Part(kind="image", image=member.image))
    digests = "\n".join(
        _member_digest_lines(record.members, cfg.input.ui_tree_max_chars))
    if fit is not None:
        digests, trims = _fit_block(digests, fit.digest_budget)
        fit.truncations += trims
    parts.append(Part(kind="text", text=f"{_LABEL_MEMBER_DIGESTS}\n{digests}"))
    return tuple(parts)


def _single_record_parts(record: Record, cfg: "ResolvedConfig",
                         fit: _PackState | None) -> tuple[Part, ...]:
    """装配单记录（非序列）的内容段：text 模态一段，ui 模态三段。

    @param record 待标注单记录
    @param cfg 已解析配置
    @param fit ④ 裁剪指令（ui 模态的树渲染是唯一可裁槽位）；None = 不裁
    @return 当前记录 user 消息的 parts 元组
    """
    if record.modality == "text":
        return (Part(kind="text", text=f"{_LABEL_TEXT_RECORD} {record.text}"),)
    tree_text = record.ui_tree.serialize(max_chars=cfg.input.ui_tree_max_chars)
    if fit is not None and fit.tree_budget is not None:
        tree_text, trimmed = _fit_tree_text(tree_text, max(0, fit.tree_budget))
        if trimmed:
            fit.truncations += 1
    return (
        Part(kind="text", text=_LABEL_SCREENSHOT),
        Part(kind="image", image=record.image),
        Part(kind="text", text=f"{_LABEL_UI_TREE}\n{tree_text}"),
    )


def _with_repair_suffix(parts: tuple[Part, ...],
                        repair: RepairContext) -> tuple[Part, ...]:
    """把 §10.5 修复后缀拼到末段文本上。

    @param parts 当前记录消息的 parts（末段恒为文本段，S6 模板不变式）
    @param repair 修复上下文
    @return 末段被替换为「原文 + 后缀」的新 parts 元组

    V25③：修复后缀是每记录的语义资产——被装箱器**计量**、永不裁剪（在全部裁剪
    之后才追加）。
    """
    suffix = (f"{_LABEL_PREV_OUTPUT} {_dumps(repair.previous_output)}\n"
              f"{_LABEL_CRITIQUES} {repair.critiques_text}\n"
              f"{_REPAIR_TAIL}")
    last = parts[-1]
    return parts[:-1] + (Part(kind="text", text=f"{last.text}\n{suffix}"),)


# ── v1.11 装填驱动（spec 3.5.2 v1.11 段，确定性份额定序）─────────────────────
#
# ① 静态系统侧（指令 / 用户 Schema / few-shot）只**计量**、永不裁（V13③ M1 预检
#    领地）；② 文本块（步骤行 + 成员摘要；单记录控件树）按各自绝对上限渲染并计量；
# ③ 图像吃余量——k_eff = min(cap, max(2, ⌊余量/单图成本⌋))，首末关键帧恒保留、中间
#    均匀降采样（只收缩 k；T14 逐片段配额按其既定规则退化）；④ k = 2 仍超 ⇒ 文本块
#    边缘裁剪（成员摘要是兜底裁决证据，**最后**才让步）；⑤ 仍超 ⇒ V10
#    ContextOverflowError(phase="precheck")——记录由 stage 层落 rejects，注定失败的
#    请求永不发出。

def _image_unit_cost(prof: "LLMProfile", ctx: "RunContext",
                     image_px: int | None) -> int:
    """算出装填用的单图计量。

    @param prof 归属 [llm.*] profile
    @param ctx 运行上下文（校准器读数入口）
    @param image_px V21 升档后的采样边长；None = 工作点
    @return 该 profile 在本批冻结快照下的单图 token 估算

    取批次冻结的校准读数（工作点）；升档 px 另以「该 px 下的 provider 先验 ×
    PRIOR_INFLATION」触底——校准器只认识工作点，取大让升档估算保持诚实且偏保守。
    """
    cost = ctx.llm.calibrator.cost(prof.name)
    if image_px is not None:
        cost = max(cost, math.ceil(budget.est_image_prior(prof, image_px)
                                   * budget.PRIOR_INFLATION))
    return cost


def _prompt_text_est(bundle: PromptBundle, schema_est: int) -> tuple[int, int]:
    """计量一次已装配提示词的文本侧规模。

    @param bundle 已装配的提示词
    @param schema_est Schema 文本计量（结构化输出关闭时为 0）
    @return (文本侧计量, 图像数量)——即 image_cost=0 下的 est_prompt 公式，与 M9
        咽喉处的记账口径完全一致
    """
    return (budget.est_prompt(bundle, None, None, image_cost=0) + schema_est,
            sum(1 for m in bundle.messages for p in m.parts if p.kind == "image"))


def _pack_prompt(record: Record, ctx: "RunContext", prof: "LLMProfile",
                 schema_text: str,
                 opts: AnnotatePromptOptions) -> tuple[PromptBundle, int]:
    """按上方份额定序装填一次标注提示词（①②③，超限则转 ④⑤）。

    @param record 待标注记录
    @param ctx 运行上下文（校准器读数与预算计数）
    @param prof 归属 [llm.*] profile（已声明 context_window）
    @param schema_text 类有效 Schema 文本
    @param opts 装配变体参数
    @return (装填后的 PromptBundle, 实际装入的图像数——无图为 0)
    @raises ContextOverflowError ⑤ 最小单元仍超限（phase="precheck"）
    """
    cfg = ctx.cfg
    b = budget.input_budget(prof)
    # 计价对象 = 类有效 Schema（与本次调用实际传出的 Schema 同源）。
    schema_est = (budget.est_text(_dumps(class_effective_model_schema(cfg, opts.label)))
                  if prof.supports_structured_output else 0)
    bundle = _assemble_prompt(record, cfg, schema_text, opts)   # ①② 按请求上限全量计量
    text_est, n_images = _prompt_text_est(bundle, schema_est)
    image_cost = k_fin = 0
    if n_images == 0:
        if text_est <= b:
            return bundle, 0
    else:
        image_cost = _image_unit_cost(prof, ctx, opts.image_px)
        k_fin = min(n_images, max(2, (b - text_est) // image_cost))   # ③ 图像吃余量
        if k_fin < n_images:
            bundle = _assemble_prompt(record, cfg, schema_text,
                                      replace(opts, k_eff=k_fin))
            text_est, n_images = _prompt_text_est(bundle, schema_est)
        if text_est + n_images * image_cost <= b:
            return bundle, n_images
    scale = _PackScale(profile=prof.name, limit=b, schema_est=schema_est,
                       text_est=text_est, image_cost=image_cost, k_fin=k_fin)
    return _trim_pack(record, ctx, schema_text, opts, scale)


def _trim_state(record: Record, cfg: "ResolvedConfig", opts: AnnotatePromptOptions,
                scale: _PackScale) -> _PackState:
    """推导 ④ 的各块份额。

    @param record 待标注记录
    @param cfg 已解析配置
    @param opts 装配变体参数
    @param scale 本次装配的度量口径快照
    @return 本次装配的裁剪指令
    @raises ContextOverflowError 单条纯文本记录不属于可裁类（§3.3 词表）→ V10

    份额取自**本次**装配的未裁块体：两个块之外的一切（含 V25③ 修复后缀与单记录
    标签）都视为定量。
    """
    if record.kind == "sequence":
        steps_body = ("\n".join(_step_line(t) for t in opts.transitions)
                      if opts.transitions is not None else "")
        digest_body = "\n".join(
            _member_digest_lines(record.members, cfg.input.ui_tree_max_chars))
        # fixed 保留各段小标题（它们计在 parts 的 est 内）；下面的份额只针对
        # ⑤ 族裁剪真正切走的块**体**。
        fixed = (scale.text_est - budget.est_text(steps_body)
                 - budget.est_text(digest_body))
        avail = scale.limit - fixed - scale.k_fin * scale.image_cost
        digest_share = min(budget.est_text(digest_body), max(0, avail))
        step_share = max(0, avail - digest_share)
        return _PackState(
            step_budget=step_share if opts.transitions is not None else None,
            digest_budget=digest_share)
    if record.modality == "ui":
        # §3.3③ 单记录族：树渲染是唯一可裁槽位。
        tree_body = (record.ui_tree.serialize(max_chars=cfg.input.ui_tree_max_chars)
                     if record.ui_tree else "")
        fixed = scale.text_est - budget.est_text(tree_body)
        return _PackState(tree_budget=(scale.limit - fixed
                                       - scale.k_fin * scale.image_cost))
    raise ContextOverflowError(
        "annotation prompt exceeds the input budget at the minimal unit "
        "(single text record — no trimmable block)", phase="precheck",
        profile=scale.profile)


def _trim_pack(record: Record, ctx: "RunContext", schema_text: str,
               opts: AnnotatePromptOptions,
               scale: _PackScale) -> tuple[PromptBundle, int]:
    """④⑤：在关键帧触底后裁文本块，仍超限即 V10。

    @param record 待标注记录
    @param ctx 运行上下文（裁剪计数入口）
    @param schema_text 类有效 Schema 文本
    @param opts 装配变体参数
    @param scale 本次装配的度量口径快照
    @return (装填后的 PromptBundle, 图像数)
    @raises ContextOverflowError 全部可裁份额耗尽后，不可裁触底（静态侧 + V25③
        后缀 + 2 个关键帧）仍超预算 → V10
    """
    fit = _trim_state(record, ctx.cfg, opts, scale)
    is_ui_sequence = record.kind == "sequence" and record.modality == "ui"
    k_arg = scale.k_fin if is_ui_sequence else opts.k_eff
    bundle = _assemble_prompt(record, ctx.cfg, schema_text,
                              replace(opts, k_eff=k_arg), fit=fit)
    if fit.truncations:
        ctx.metrics.count("budget.truncations.annotate", fit.truncations)
    text_est, n_images = _prompt_text_est(bundle, scale.schema_est)
    if text_est + n_images * scale.image_cost > scale.limit:
        raise ContextOverflowError(
            "annotation prompt exceeds the input budget at the minimal unit "
            f"(k={n_images}, text floor untrimmable)", phase="precheck",
            profile=scale.profile)
    return bundle, n_images


async def _budgeted_call(record: Record, ctx: "RunContext", schema_text: str,
                         opts: AnnotatePromptOptions) -> tuple[dict, Usage, int, str]:
    """经 M8 四层保证发出一次标注调用。

    @param record 待标注记录
    @param ctx 运行上下文
    @param schema_text 类有效 Schema 文本
    @param opts 装配变体参数
    @return complete_validated 的四元组（对象、用量、尝试数、模型）
    @raises SchemaViolation L3 修复穷尽
    @raises ContextOverflowError 装填 V10 或反应式溢出终态

    预算未声明（cw == 0）⇒ 走 v1.11 之前的装配/调用路径，字节等价（200 形态的溢出
    仍可能浮现，直接上抛且不喂熔断）；已声明 ⇒ 交给 _degrading_call。两个调用点
    都经 _complete_annotation，携带按类 Schema 覆盖。
    """
    cfg = ctx.cfg
    prof = cfg.llm_profiles.get(cfg.annotate.llm)
    if prof is not None and prof.context_window > 0:
        return await _degrading_call(record, ctx, schema_text, opts, prof)
    prompt = _assemble_prompt(record, cfg, schema_text, opts)
    return await _complete_annotation(ctx, record, prompt, opts)


async def _degrading_call(record: Record, ctx: "RunContext", schema_text: str,
                          opts: AnnotatePromptOptions,
                          prof: "LLMProfile") -> tuple[dict, Usage, int, str]:
    """预算已声明时的装填调用 + V20 有界降级重试。

    @param record 待标注记录
    @param ctx 运行上下文
    @param schema_text 类有效 Schema 文本
    @param opts 装配变体参数
    @param prof 归属 [llm.*] profile
    @return complete_validated 的四元组
    @raises ContextOverflowError 装填 V10，或降级次数耗尽后的反应式终态

    关键帧折半（k → max(2, ⌈k/2⌉)），至多 2 次降级并计 budget.degrade_retries；
    终态遵循 §3.5 熔断矩阵——反应式 400 恰喂一次熔断。
    """
    shape = opts
    degrades = 0
    pending: ContextOverflowError | None = None
    while True:
        try:
            prompt, k_used = _pack_prompt(record, ctx, prof, schema_text, shape)
        except ContextOverflowError:
            # 装箱器抛的 V10：驱动本次降级的那个反应式溢出（若有）在此结算终态
            # （A7）；precheck 抛出本身永不喂。
            if pending is not None:
                _feed_reactive_terminal(pending, ctx.metrics)
            raise
        try:
            return await _complete_annotation(ctx, record, prompt, shape)
        except ContextOverflowError as exc:
            if k_used > 2 and degrades < 2:
                degrades += 1
                pending = exc
                ctx.metrics.count("budget.degrade_retries")
                shape = replace(shape, k_eff=max(2, math.ceil(k_used / 2)))  # V20 折半
                continue
            _feed_reactive_terminal(exc, ctx.metrics)
            raise


# ── 自洽采样的字段级多数投票（spec 3.5.2）───────────────────────────────────

_MISSING = object()          # 哨兵：该被投票属性在样本中缺席


def _voted_keys(user_schema: Mapping) -> tuple[str, ...]:
    """挑出参与逐字段投票的顶层属性。

    @param user_schema 类有效标注 Schema
    @return 参与投票的属性名元组（enum / boolean / integer 三类）
    """
    keys: list[str] = []
    for key, prop in (user_schema.get("properties") or {}).items():
        if not isinstance(prop, Mapping):
            continue
        if "enum" in prop:
            keys.append(key)
            continue
        t = prop.get("type")
        types = {t} if isinstance(t, str) else set(t or ())
        if types and types <= {"boolean", "integer"}:
            keys.append(key)
    return tuple(keys)


def _field_value(sample: Mapping, key: str) -> object:
    """取样本中某个被投票字段的取值。

    @param sample 一个 Schema 合法样本
    @param key 被投票的属性名
    @return 该字段取值；字段缺席时返回 _MISSING 哨兵
    """
    return sample[key] if key in sample else _MISSING


def _freeze(value: object) -> object:
    """把取值折成可哈希的计票身份。

    @param value 字段取值（枚举实践上是 JSON 标量，但容忍任意 JSON）
    @return 可哈希的计票键
    """
    if value is _MISSING:
        return ("__missing__",)
    if isinstance(value, bool):                    # 让 True 与 1 保持可区分
        return ("bool", value)
    try:
        hash(value)
        return ("v", value)
    except TypeError:
        # 不可哈希取值（数组/对象）退化为 canonical JSON 键——非错误路径，取证只记
        # 类型名，绝不记内容（隐私红线）。
        _logger.debug("unhashable voted field value; using canonical JSON key: type=%s",
                      type(value).__name__)
        return ("json", json.dumps(value, sort_keys=True, ensure_ascii=False))


def _modal_combination(samples: Sequence[Mapping],
                       voted: Sequence[str]) -> tuple | None:
    """逐字段严格多数投票，给出众数组合。

    @param samples Schema 合法的样本序列
    @param voted 参与投票的属性名序列
    @return 众数组合（按 voted 序的计票键元组）；任一字段无严格多数（平局）时为
        None。voted 为空时返回空元组（等价于全体样本组合一致）
    """
    modal: list[object] = []
    for key in voted:
        counts: dict[object, int] = {}
        order: list[object] = []
        for sample in samples:
            fv = _freeze(_field_value(sample, key))
            if fv not in counts:
                order.append(fv)
            counts[fv] = counts.get(fv, 0) + 1
        best = max(counts.values())
        winners = [fv for fv in order if counts[fv] == best]
        if len(winners) != 1:                      # 平局 ⇒ 无众数组合
            return None
        modal.append(winners[0])
    return tuple(modal)


def _majority_vote(samples: Sequence[Mapping],
                   user_schema: Mapping) -> tuple[Mapping, int, bool]:
    """在 Schema 合法样本上做字段级多数投票（spec 3.5.2）。

    @param samples Schema 合法的样本序列（至少一个）
    @param user_schema 类有效标注 Schema（决定哪些字段参与投票）
    @return (定稿对象, matches, disagreed)：matches = 被投票字段与**最终**组合完全
        相同的样本数；disagreed = 是否走了全面分歧兜底
    @raises ValueError samples 为空

    enum/boolean/integer 顶层属性各自独立投票（逐字段严格模式：某取值的计数严格
    大于其余全部）；其余字段整体取自**第一个**被投票字段全等于众数组合的样本。
    任一字段无严格多数，或众数组合不匹配任何样本 ⇒ 全面分歧：整体取样本 #1。
    """
    if not samples:
        raise ValueError("_majority_vote requires at least one sample")

    voted = _voted_keys(user_schema)

    def combo(sample: Mapping) -> tuple:
        """算出一个样本的被投票字段组合。

        @param sample 一个样本
        @return 按 voted 序的计票键元组
        """
        return tuple(_freeze(_field_value(sample, k)) for k in voted)

    target = _modal_combination(samples, voted)
    chosen: Mapping | None = None
    if target is not None:
        chosen = next((s for s in samples if combo(s) == target), None)
    disagreed = chosen is None
    if disagreed:                                  # 平局或众数组合匹配不到样本
        chosen = samples[0]

    final_combo = combo(chosen)
    matches = sum(1 for sample in samples if combo(sample) == final_combo)
    return chosen, matches, disagreed


# ── 记录级标注路径（M7 的公开修复面）──────────────────────────────────────

@dataclass(frozen=True, slots=True)
class _SamplePlan:
    """一个记录级标注样本的纯叶计划。

    @param record 待标注记录
    @param schema_text 类有效 Schema 文本
    @param options 本样本的装配参数
    """

    record: Record                         # 待标注记录
    schema_text: str                       # 类有效 Schema 文本
    options: AnnotatePromptOptions         # 本样本的装配参数


@dataclass(frozen=True, slots=True)
class _SampleOutcome:
    """一个样本的冻结成功值或普通失败。

    @param value M8 成功四元组
    @param error 待声明序归并的失败
    """

    value: tuple[dict, Usage, int, str] | None  # M8 成功四元组
    error: Exception | None                    # 待声明序归并的失败


def _sample_plans(record: Record, ctx: "RunContext",
                  opts: AnnotatePromptOptions) -> tuple[_SamplePlan, ...]:
    """按声明序冻结一条记录的采样计划。

    @param record 待标注记录
    @param ctx 运行上下文
    @param opts 装配变体参数
    @return 单次或自洽样本计划
    """
    base = replace(opts, temperature=None)
    count = ctx.cfg.annotate.self_consistency if opts.repair is None else 0
    if count == 0:
        count = 1
        sample_options = base
    else:
        sample_options = replace(base, temperature=ctx.cfg.annotate.sc_temperature)
    schema_text = class_schema_text(ctx, opts.label)
    return tuple(_SamplePlan(record, schema_text, sample_options)
                 for _ in range(count))


async def _run_sample(plan: _SamplePlan, ctx: "RunContext",
                      isolate: bool) -> _SampleOutcome:
    """执行一个不写共享业务对象的标注样本叶。

    @param plan 冻结样本计划
    @param ctx 运行上下文
    @param isolate 是否把普通失败收敛为 outcome
    @return 成功值或普通失败
    """
    try:
        value = await _budgeted_call(
            plan.record, ctx, plan.schema_text, plan.options,
        )
        return _SampleOutcome(value=value, error=None)
    except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
        raise
    except SchemaViolation as exc:
        return _SampleOutcome(value=None, error=exc)
    except ProviderFatalError as exc:
        if _ATTEMPT_MODE.get() or not isolate:
            raise
        return _SampleOutcome(value=None, error=exc)
    except Exception as exc:  # 纯叶只回传冻结失败
        if not isolate:
            raise
        return _SampleOutcome(value=None, error=exc)


async def _execute_samples(plans: Sequence[_SamplePlan], ctx: "RunContext",
                           isolate: bool, start: int = 0) -> tuple[_SampleOutcome, ...]:
    """通过共享 TaskExecutor 执行一个完整采样波次。

    @param plans 声明序样本计划
    @param ctx 运行上下文
    @param isolate 是否收敛普通失败
    @param start 本波次在 annotate stage 内的起始 ordinal
    @return 与 plans 对齐的输入序结果
    """
    if not plans:
        return ()
    specs = tuple(
        TaskSpec(
            task_id=f"{ctx.task_namespace}:annotate:{start + ordinal}",
            declaration_key=(ctx.batch_no, 7, start + ordinal),
            stage="annotate",
            resource_key=("llm", ctx.cfg.annotate.llm),
            operation=lambda plan=plan: _run_sample(plan, ctx, isolate),
        )
        for ordinal, plan in enumerate(plans)
    )
    return await ctx.tasks.run_group(TaskGroupRequest(specs))


async def annotate_record_leaf(record: Record, ctx: "RunContext",
                               opts: AnnotatePromptOptions) -> Annotation:
    """verify TaskSpec 可直接调用的无调度单记录纯叶面。

    @param record 待标注记录
    @param ctx verify 波次派生的运行上下文
    @param opts 单调用装配参数；verify repair 必须携带 repair
    @return 单次 M8 调用的 Annotation
    @raises InternalError 请求了必须由外层调度的自洽采样
    """
    if opts.repair is None and ctx.cfg.annotate.self_consistency > 0:
        _logger.error("annotation leaf cannot execute self-consistency")
        raise InternalError("annotation leaf cannot execute self-consistency")
    shape = replace(opts, temperature=None)
    schema_text = class_schema_text(ctx, shape.label)
    obj, usage, attempts, model = await _budgeted_call(record, ctx, schema_text, shape)
    return Annotation(output=obj, model=model, attempts=attempts, usage=usage)


async def annotate_record(record: Record, ctx: "RunContext",
                          opts: AnnotatePromptOptions = _DEFAULT_PROMPT_OPTIONS,
                          ) -> Annotation:
    """跑完一条记录的完整标注路径（含自洽投票）。

    @param record 待标注记录（序列记录 raw = None，故 L2.5 回调收到 record=None——
        已知限制）
    @param ctx 为本次公开调用派生唯一 task_namespace 的运行上下文
    @param opts 装配变体参数（``AnnotatePromptOptions``）：``repair`` 非 None 时
        **跳过**自洽（修复重标注恒为 profile 默认温度下的单次调用）；``label``
        选择按类指令/few-shot 与标注
        Schema——提示词文本、M8 调用、自洽投票与预算计价四处同源取值，按类 Schema
        调用保留用户待遇（L2.5 + resolved_at），而 llm / self_consistency /
        sc_temperature 仍取全局（白名单）；``transitions`` v1.8（S5）步骤源，M7 穿
        的是成员手术后的**重建**值；``fragment_lens`` v1.9（T14）逐片段关键帧配额，
        来自 M16 的 stitch_fragments 标位；``k_eff`` v1.11（V21 梯 / F3）关键帧上限
        收窄值，M5 自身的 V20 降级在 _degrading_call 内部单独传；``image_px``
        v1.11（V23①）升档后的图像采样边长。``temperature`` 由 M5 内部设定（单次
        调用取 profile 默认，自洽样本取 sc_temperature），调用方置值一律忽略
    @return 该记录的 Annotation
    @raises SchemaViolation L3 修复穷尽（含 L2.5 回调违规）
    @raises ProviderRetryableError 重试耗尽
    @raises ProviderFatalError 不可重试的 provider 错误
    @raises ContextOverflowError 装填 V10 或反应式溢出终态

    每个变体取值在**所有**路径（单次调用、每个自洽样本、修复重标注）上一致穿参；
    缺省对象即与引入这些取值之前的调用形字节等价。M7 的修复重标注传同一个 label，
    故按类 Schema 无需修复侧改动。
    """
    plans = _sample_plans(record, ctx, opts)
    outcomes = await _execute_samples(plans, ctx, isolate=False)
    return _annotation_from_samples(outcomes, record, ctx, opts)


def _split_sc_results(results: Sequence[_SampleOutcome]
                      ) -> tuple[list[tuple[dict, Usage, int, str]],
                                 SchemaViolation | None]:
    """把自洽采样结果拆成有效样本与最后一条弃权违规。

    @param results TaskExecutor 按声明序返回的样本结果
    @return (有效样本列表, 最后一条 SchemaViolation 或 None)
    @raises BaseException 非 SchemaViolation 的异常原样上抛（provider / 内部错误升级）
    """
    valid: list[tuple[dict, Usage, int, str]] = []
    last_violation: SchemaViolation | None = None
    for outcome in results:
        if isinstance(outcome.error, SchemaViolation):
            last_violation = outcome.error
        elif outcome.error is not None:
            raise outcome.error
        elif outcome.value is not None:
            valid.append(outcome.value)
    return valid, last_violation


def _annotation_from_samples(outcomes: Sequence[_SampleOutcome], record: Record,
                             ctx: "RunContext", opts: AnnotatePromptOptions) -> Annotation:
    """按声明序归并一条记录的样本并定稿。

    @param outcomes TaskExecutor 返回的输入序样本结果
    @param ctx 运行上下文
    @param opts 装配变体参数
    @return 定稿 Annotation（sc 携带 n 与一致率）
    @raises SchemaViolation 全部样本都违规（抛最后一条；无违规记录时抛占位违规）

    SchemaViolation 样本弃权（分母仍为 n）；provider / 内部异常直接上抛。
    """
    if len(outcomes) == 1:
        outcome = outcomes[0]
        if outcome.error is not None:
            raise outcome.error
        if outcome.value is None:
            raise InternalError("annotation sample completed without a value")
        obj, usage, attempts, model = outcome.value
        _verify_temporal_annotation(obj, record, ctx.cfg, opts)
        return Annotation(output=obj, model=model, attempts=attempts, usage=usage)
    valid, last_violation = _split_sc_results(outcomes)
    if not valid:
        raise last_violation if last_violation is not None else SchemaViolation(
            ["self-consistency: all samples failed"], "")

    # 可投票字段取自类有效 Schema（按类 Schema 的字段集可与全局不同）。
    chosen, matches, disagreed = _majority_vote(
        [obj for obj, _, _, _ in valid], class_effective_schema(ctx.cfg, opts.label))
    if disagreed:
        ctx.metrics.count("annotate.sc_disagreements")
    n = len(outcomes)
    _verify_temporal_annotation(chosen, record, ctx.cfg, opts)
    return Annotation(output=chosen, model=valid[0][3],
                      attempts=sum(attempts for _, _, attempts, _ in valid),
                      usage=sum((usage for _, usage, _, _ in valid), Usage()),
                      sc={"n": n, "agreement_ratio": matches / n})


def _verify_temporal_annotation(output: Mapping[str, object], record: Record,
                                cfg: "ResolvedConfig", opts: AnnotatePromptOptions) -> None:
    """确认 sample 或 vote 定稿仍等于同 context 的机械注入结果。"""
    view = _class_view(cfg, opts.label)
    if view is None or not view.time_bindings:
        return
    finalizer, _projector = _annotation_transforms(record, cfg, opts)
    model = project_temporal_instance(output, view.business_time_paths)
    try:
        expected = finalizer(model)
    except ValueError:
        _annotation_contract_error("annotation vote temporal finalization failed")
    if _canonical_mapping(expected) != _canonical_mapping(output):
        _annotation_contract_error("annotation vote replaced its temporal context")


# ── v1.12 帧级逐帧标注（SPEC-frame-annotation §3.3；公开直调面 = 修复面族新成员）──
#
# 段序冻结（§10.13）：system（[任务] + 生效指令 + Schema 约束句 + 帧 Schema 文本——
# Schema 嵌入手法镜像序列级 build_annotate_prompt）→ 每条 few-shot 一条 user 消息
# （配置序，§10.1 同形）→ 成员内容 user 消息（text 模态 = "[成员帧] {行文本}"；
# ui 模态 = [屏幕截图] + 该成员截图 + [UI 控件树] + 树渲染三段，与单记录标注三段形
# 同款，树渲染绝对上限 input.ui_tree_max_chars）。

def build_frame_annotate_prompt(member: Record, cfg: "ResolvedConfig",
                                schema_text: str,
                                label: str | None = None) -> PromptBundle:
    """确定性装配帧级标注提示词（SPEC-frame-annotation §3.3，CONTRACTS §10.13）。

    @param member 单个成员帧记录
    @param cfg 已解析配置
    @param schema_text ``cfg.frame_schema`` 的 canonical 单行 dump（形态对齐
        ``SchemaEngine.user_schema_text``：ensure_ascii=False + separators=(", ", ": ")）
    @param label 帧类标签：非 None ⇒ 指令/few-shot 取 ``cfg.frame_class_views[label]``
        （帧类覆盖视图）；None ⇒ 全局 [frame.annotate]（frame.classify 关闭时的全员形态）
    @return 装配好的 PromptBundle

    预算装填经私有装配器的尾参 ``fit`` 进入（annotate_member 内），永不在此。
    """
    return _assemble_frame_prompt(member, cfg, schema_text, label)


def _assemble_frame_prompt(member: Record, cfg: "ResolvedConfig",
                           schema_text: str, label: str | None,
                           fit: _PackState | None = None) -> PromptBundle:
    """§10.13 装配体。

    @param member 单个成员帧记录
    @param cfg 已解析配置
    @param schema_text 帧 Schema 文本
    @param label 帧类标签；None = 全局 [frame.annotate]
    @param fit 非 None 时应用 §3.3③ 单记录族的树动态帽（_fit_tree_text——ui 成员的
        树渲染是唯一可裁块；生效指令 / few-shot / 帧 Schema 文本是静态语义资产，
        只计不裁，V13③ M1 预检领地）
    @return 装配好的 PromptBundle
    """
    view = cfg.frame_class_views[label] if label is not None else None
    acfg = cfg.frame_annotate
    instruction = view.instruction if view is not None else acfg.instruction
    examples = view.examples if view is not None else acfg.examples

    system_text = (f"{_FRAME_LABEL_TASK}\n{instruction}\n"
                   f"{_SCHEMA_SENTENCE}\n{schema_text}")
    messages: list[Message] = [
        Message(role="system", parts=(Part(kind="text", text=system_text),))]
    for example in examples:
        example_text = (f"{_LABEL_EXAMPLE_IN} {example.input}\n"
                        f"{_LABEL_EXAMPLE_OUT} {_dumps(example.output)}")
        messages.append(Message(role="user",
                                parts=(Part(kind="text", text=example_text),)))

    if member.modality == "text":
        parts: tuple[Part, ...] = (
            Part(kind="text", text=f"{_FRAME_LABEL_MEMBER} {member.text}"),
        )
    else:  # ui 成员：三段形（镜像本文件单记录 ui 标注）
        tree_text = member.ui_tree.serialize(max_chars=cfg.input.ui_tree_max_chars)
        if fit is not None and fit.tree_budget is not None:
            tree_text, trimmed = _fit_tree_text(tree_text, max(0, fit.tree_budget))
            if trimmed:
                fit.truncations += 1
        parts = (
            Part(kind="text", text=_LABEL_SCREENSHOT),
            Part(kind="image", image=member.image),
            Part(kind="text", text=f"{_LABEL_UI_TREE}\n{tree_text}"),
        )
    messages.append(Message(role="user", parts=parts))
    return PromptBundle(messages=tuple(messages))


def _pack_frame_prompt(member: Record, ctx: "RunContext", prof: "LLMProfile",
                       schema_text: str, label: str | None) -> PromptBundle:
    """帧级提示词的预算装填（§3.3③ 单记录族的帧级镜像）。

    @param member 单个成员帧记录
    @param ctx 运行上下文（校准器读数与裁剪计数）
    @param prof 归属 [llm.*] profile（已声明 context_window）
    @param schema_text 帧 Schema 文本
    @param label 帧类标签；None = 全局 [frame.annotate]
    @return 装填后的 PromptBundle
    @raises ContextOverflowError 裁树后仍超限（phase="precheck"）

    ui 成员的树渲染是唯一可裁块（动态帽，绝对上限仍是 input.ui_tree_max_chars）；
    text 成员的行文本非裁剪类。帧 prompt 本身就是最小单元——单成员、至多单图，无窗
    可分、无关键帧可减，故**无降级梯**：直接 V10，调用方按成员失败处置，注定失败的
    请求永不发出。图像成本恒取 profile 工作点（校准器按 profile 聚合的前提，
    V18/V19——帧调用不设独立尺寸）。
    """
    cfg = ctx.cfg
    b = budget.input_budget(prof)
    schema_est = (budget.est_text(schema_text)
                  if prof.supports_structured_output else 0)
    bundle = _assemble_frame_prompt(member, cfg, schema_text, label)
    text_est, n_images = _prompt_text_est(bundle, schema_est)
    image_cost = _image_unit_cost(prof, ctx, None) if n_images else 0
    if text_est + n_images * image_cost <= b:
        return bundle
    if member.modality == "ui" and member.ui_tree is not None:
        tree_body = member.ui_tree.serialize(max_chars=cfg.input.ui_tree_max_chars)
        fixed = text_est - budget.est_text(tree_body)
        fit = _PackState(tree_budget=b - fixed - n_images * image_cost)
        bundle = _assemble_frame_prompt(member, cfg, schema_text, label, fit=fit)
        if fit.truncations:
            ctx.metrics.count("budget.truncations.frame_annotate", fit.truncations)
        text_est, n_images = _prompt_text_est(bundle, schema_est)
        if text_est + n_images * image_cost <= b:
            return bundle
    raise ContextOverflowError(
        "frame annotation prompt exceeds the input budget at the minimal unit "
        "(single member — no degrade ladder)", phase="precheck",
        profile=prof.name)


def _frame_error_kind(member: Record, exc: BaseException) -> str:
    """把帧失败归入 §7.6 既有词表（stage 层分类器的成员级镜像）。

    @param member 出错的成员帧记录
    @param exc 捕获的异常
    @return §7.6 的错误 kind 字符串

    预算词表由调用方经 budget.classify_stage_error 先行路由（V27① 同序）。帧 Schema
    走内部 Schema 待遇、无 L2.5 ⇒ callback_violation 不可达，SchemaViolation 恒归
    schema_violation——零新错误 kind（spec 明写）。
    """
    if isinstance(exc, SchemaViolation):
        return ErrorKind.SCHEMA_VIOLATION.value
    if isinstance(exc, ProviderRetryableError):
        return ErrorKind.PROVIDER_RETRYABLE_EXHAUSTED.value
    if isinstance(exc, ProviderFatalError):
        return ErrorKind.PROVIDER_FATAL.value
    if member.modality == "ui" and isinstance(exc, OSError):
        return ErrorKind.IMAGE_DECODE_ERROR.value
    return ErrorKind.INTERNAL_ERROR.value


# ``annotate_member`` 是修复面族的新成员：M7 verify 的成员回收补跑经懒加载直调它，
# 与 annotate_record / segment.judge_window / extract.extract_transition 同列（算子间
# 导入白名单第四向），契约地位与 annotate_record 修复面同款，签名冻结。
#
# Schema 路由（裁决·帧 Schema 显式路由）：complete_validated 显式传
# schema=cfg.frame_schema ⇒ 内部 Schema 待遇——L0–L3 四层全在、无 L2.5、不计
# resolved_at（§6.4 恒等式「resolved_at 加总 = 进入 M5 的记录数」不被帧调用污染）；
# 勿改走 schema=None。
#
# 失败语义分路径：ordinary process/flat 隔离成员失败，计 frame_annotate.failed 并写
# WARN；sequence attempt 把异常原样交给 whole-set 重试，既不提前计失败也不局部接收。
# 两条路径都不把数据内容或提示词写进日志。成功计 frame_annotate.annotated；M7 回收
# 补跑共享 ordinary 路径。溢出纪律：precheck/finish 永不喂熔断；ordinary 路径的反应
# 式 http_400 终端才经 _feed_reactive_terminal 恰一次喂入（A7）。

@dataclass(frozen=True, slots=True)
class _MemberOutcome:
    """一个成员标注叶的冻结成功值或普通失败。

    @param annotation 成功标注
    @param error 待声明序归并的失败
    """

    annotation: Annotation | None          # 成功标注
    error: Exception | None                # 待声明序归并的失败


async def annotate_member_leaf(member: Record, ctx: "RunContext",
                               label: str | None = None) -> Annotation:
    """verify TaskSpec 可直接调用的无调度单成员纯叶面。

    @param member 待标注成员帧
    @param ctx verify 波次派生的运行上下文
    @param label 成员类标签；None 使用全局帧指令
    @return 成功 Annotation；异常原样上抛
    """
    cfg = ctx.cfg
    schema = _thaw_json(cfg.frame_schema)
    schema_text = json.dumps(schema, ensure_ascii=False, separators=(", ", ": "))
    prof = cfg.llm_profiles.get(cfg.frame_annotate.llm)
    if prof is None or prof.context_window <= 0:
        prompt = build_frame_annotate_prompt(member, cfg, schema_text, label=label)
    else:
        prompt = _pack_frame_prompt(member, ctx, prof, schema_text, label)
    obj, usage, attempts, model = await ctx.schema_engine.complete_validated(
        cfg.frame_annotate.llm, prompt, schema=schema,
        scope=CallScope(record_ids=(member.id,), batch_no=ctx.batch_no))
    return Annotation(output=obj, model=model, attempts=attempts, usage=usage)


async def _run_member(member: Record, ctx: "RunContext",
                      label: str | None) -> _MemberOutcome:
    """执行纯成员叶，把可隔离失败收敛为普通结果。

    @param member 待标注成员帧
    @param ctx 运行上下文
    @param label 成员类标签
    @return 成功值或普通失败
    """
    try:
        return _MemberOutcome(await annotate_member_leaf(member, ctx, label), None)
    except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
        raise
    except ProviderFatalError as exc:
        if _ATTEMPT_MODE.get():
            raise
        return _MemberOutcome(None, exc)
    except Exception as exc:  # 纯叶只回传冻结失败
        return _MemberOutcome(None, exc)


async def _execute_members(members: Sequence[tuple[Record, str | None]],
                           ctx: "RunContext", start: int) -> tuple[_MemberOutcome, ...]:
    """通过共享 TaskExecutor 执行一个成员标注波次。

    @param members 成员记录与类标签对
    @param ctx 运行上下文
    @param start 本波次在 annotate stage 内的起始 ordinal
    @return 与 members 对齐的输入序结果
    """
    if not members:
        return ()
    profile = ctx.cfg.frame_annotate.llm
    specs = tuple(
        TaskSpec(
            task_id=f"{ctx.task_namespace}:annotate:{start + ordinal}",
            declaration_key=(ctx.batch_no, 7, start + ordinal),
            stage="annotate",
            resource_key=("llm", profile),
            operation=lambda member=member, label=label: _run_member(member, ctx, label),
        )
        for ordinal, (member, label) in enumerate(members)
    )
    return await ctx.tasks.run_group(TaskGroupRequest(specs))


def _record_member_failure(member: Record, ctx: "RunContext", exc: Exception) -> None:
    """提交一个 ordinary 成员失败的计数与无数据日志。

    @param member 失败成员帧
    @param ctx 运行上下文
    @param exc 待分类失败
    """
    if isinstance(exc, ContextOverflowError):
        _feed_reactive_terminal(exc, ctx.metrics)
    kind = budget.classify_stage_error(exc) or _frame_error_kind(member, exc)
    ctx.metrics.count("frame_annotate.failed")
    _logger.warning("frame annotation failed: member=%s kind=%s exc=%s",
                    member.id, kind, type(exc).__name__,
                    extra={"stage": "annotate", "batch": ctx.batch_no})


async def annotate_member(member: Record, ctx: "RunContext",
                          label: str | None = None) -> Annotation | None:
    """v1.12 帧级逐帧标注的公开直调面（SPEC-frame-annotation §3.3/§3.4，签名冻结）。

    @param member 单个成员帧记录
    @param ctx 为本次公开调用派生唯一 task_namespace 的运行上下文
    @param label 帧类标签：非 None ⇒ 指令/few-shot 取 ``cfg.frame_class_views[label]``
        （类覆盖）；None ⇒ 全局 [frame.annotate]。类视图 enabled=false 的跳过判定归
        调用方（M5 帧 pass / M7 回收），本面不重复判定
    @return 成功的 Annotation；ordinary process/flat 路径在修复穷尽或不可恢复时
        隔离失败并返回 None，由调用方按「failed 占键 None」落 dict
    @raises Exception sequence attempt 模式把 Schema、上下文、截断、provider 与其他
        成员级异常原样上抛，由 M5 拒绝当前 whole-set attempt
    @raises CircuitBreakerTripped 运行级控制流在所有模式照常上抛（KeyboardInterrupt /
        CancelledError 同理）
    """
    (outcome,) = await _execute_members(((member, label),), ctx, 0)
    if outcome.error is not None:
        if _ATTEMPT_MODE.get():
            raise outcome.error
        _record_member_failure(member, ctx, outcome.error)
        return None
    ctx.metrics.count("frame_annotate.annotated")
    if outcome.annotation is None:
        raise InternalError("frame annotation completed without a value")
    return outcome.annotation


# ── Stage 实现 ───────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class _RecordSpan:
    """一个信封在全批 sample 波次中的输入序切片。

    @param item 待归并信封
    @param options 本信封的装配参数
    @param start sample 结果起始位置
    @param count sample 结果数
    """

    item: PipelineItem                    # 待归并信封
    options: AnnotatePromptOptions        # 本信封的装配参数
    start: int                            # sample 结果起始位置
    count: int                            # sample 结果数


@dataclass(frozen=True, slots=True)
class _FramePlan:
    """一个唯一成员的声明序归并计划。

    @param item 所属序列信封
    @param member 待标注成员
    @param label 成员类标签
    @param skipped 类视图是否禁用
    @param call_index 成员叶结果位置
    """

    item: PipelineItem                    # 所属序列信封
    member: Record                        # 待标注成员
    label: str | None                     # 成员类标签
    skipped: bool                         # 类视图是否禁用
    call_index: int | None                # 成员叶结果位置


class AnnotateStage:
    """M5 标注阶段（spec §4.3 阶段契约：只处理 active 信封，绝不增删列表元素，
    单记录失败绝不外溢到批次层）。"""

    name = "annotate"

    def __init__(self, cfg: "ResolvedConfig"):
        """构造标注阶段。

        @param cfg 已解析配置
        """
        self._cfg = cfg

    @property
    def cfg(self) -> "ResolvedConfig":
        """@return 当前 attempt 的程序视图配置；普通批次返回构造期配置。"""
        active = _ATTEMPT_CONFIG.get()
        return self._cfg if active is None else active  # type: ignore[return-value]

    async def run(self, batch: list[PipelineItem], ctx: "RunContext") -> list[PipelineItem]:
        """先并发全批 sequence samples，归并后再并发 frame members。

        @param batch 本批信封列表（只改状态，绝不增删元素）
        @param ctx 运行上下文
        @return 同一个 batch 列表
        """
        active = [item for item in batch if item.status == "active"]
        next_ordinal = await self._sequence_wave(active, ctx)
        await self._frame_wave(active, ctx, next_ordinal)
        return batch

    async def _annotate_item(self, item: PipelineItem, ctx: "RunContext") -> None:
        """公开测试面：对单信封执行同样的两波标注。

        @param item 待标注信封
        @param ctx 运行上下文
        """
        next_ordinal = await self._sequence_wave([item], ctx)
        await self._frame_wave([item], ctx, next_ordinal)

    async def run_attempt(
        self,
        request: DownstreamAttemptRequest,
    ) -> DownstreamAttemptResult:
        """执行一次 whole-set 标注 gate，不把异常降级为 item error。

        @param request 当前 AttemptTransaction 与共享运行上下文。
        @return 接受状态、标注拒绝归因与 attempt-local dataset counters。
        """
        items = list(request.transaction.items)
        config_token = _ATTEMPT_CONFIG.set(request.run_context.cfg)
        try:
            token = _ATTEMPT_MODE.set(True)
            try:
                with request.run_context.metrics.capture_counts() as counters:
                    try:
                        await self.run(items, request.run_context)
                    except (SchemaViolation, ContextOverflowError,
                            OutputTruncatedError, ProviderRetryableError):
                        return DownstreamAttemptResult(
                            accepted=False, rejected_stage="annotate",
                            dataset_counters=dict(counters))
            finally:
                _ATTEMPT_MODE.reset(token)
            self._assert_no_provider_fatal(items)
            accepted = all(self._attempt_item_accepted(item) for item in items)
            return DownstreamAttemptResult(
                accepted=accepted,
                rejected_stage=None if accepted else "annotate",
                dataset_counters=dict(counters),
            )
        finally:
            _ATTEMPT_CONFIG.reset(config_token)

    @staticmethod
    def _assert_no_provider_fatal(items: list[PipelineItem]) -> None:
        """把 attempt 路径上的 provider-fatal item error 升为协议破坏。

        @param items 当前 attempt 的唯一信封真值。
        @return None。
        @raises RuntimeError 发现被错误隔离的 provider fatal。
        """
        if any(error.kind == ErrorKind.PROVIDER_FATAL.value
               for item in items for error in item.errors):
            _logger.error("generation_downstream_contract: isolated provider fatal")
            raise InternalError("generation_downstream_contract: isolated provider fatal")

    def _attempt_item_accepted(self, item: PipelineItem) -> bool:
        """判断一个 sequence item 是否通过序列与帧标注。

        @param item 下游原地修改后的信封。
        @return 所有启用的标注面均成功则 True。
        """
        if item.status != "active":
            return False
        if self.cfg.annotate.enabled and item.annotation is None:
            return False
        if not self.cfg.frame_annotate.enabled or item.record.kind != "sequence":
            return True
        annotations = item.member_annotations or {}
        for member in item.record.members:
            cls = (item.member_classifications or {}).get(member.id)
            view = self.cfg.frame_class_views.get(cls.label) if cls is not None else None
            if (view is None or view.enabled) and annotations.get(member.id) is None:
                return False
        return True

    def _sequence_plans(self, items: Sequence[PipelineItem], ctx: "RunContext"
                        ) -> tuple[list[_RecordSpan], list[_SamplePlan]]:
        """按 item/sample 声明序冻结全批 sequence 采样。

        @param items 进入标注阶段的 active 信封
        @param ctx 运行上下文
        @return 信封切片与样本计划
        """
        spans: list[_RecordSpan] = []
        samples: list[_SamplePlan] = []
        if not self.cfg.annotate.enabled:
            return spans, samples
        for item in items:
            label = item.classification.label if item.classification else None
            fragments = getattr(item, "stitch_fragments", None)
            fragment_lens = (tuple(int(f["member_count"]) for f in fragments)
                             if fragments else None)
            options = AnnotatePromptOptions(
                label=label, transitions=item.transitions, fragment_lens=fragment_lens,
                temporal_context=item.temporal_context,
            )
            planned = _sample_plans(item.record, ctx, options)
            spans.append(_RecordSpan(item, options, len(samples), len(planned)))
            samples.extend(planned)
        return spans, samples

    async def _sequence_wave(self, items: Sequence[PipelineItem],
                             ctx: "RunContext") -> int:
        """执行全批 sample 波次并按 item/sample 声明序归并。

        @param items 进入标注阶段的 active 信封
        @param ctx 运行上下文
        @return 下一波次的起始 ordinal
        """
        spans, samples = self._sequence_plans(items, ctx)
        outcomes = await _execute_samples(samples, ctx, isolate=True)
        for span in spans:
            result_slice = outcomes[span.start:span.start + span.count]
            try:
                annotation = _annotation_from_samples(
                    result_slice, span.item.record, ctx, span.options,
                )
            except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as exc:  # 归并屏障才决定记录失败
                if _ATTEMPT_MODE.get():
                    raise
                self._settle_annotation_failure(span.item, ctx, exc)
            else:
                span.item.annotation = annotation
                self._commit_annotation(span.item, ctx)
        return len(samples)

    def _settle_annotation_failure(self, item: PipelineItem, ctx: "RunContext",
                                   exc: Exception) -> None:
        """在归并屏障提交一个 ordinary 记录失败。

        @param item 失败信封
        @param ctx 运行上下文
        @param exc 待分类失败
        """
        failure = self._classify_failure(item, exc)
        kind = failure[0]
        _logger.warning("annotation failed: record=%s kind=%s exc=%s",
                        item.record.id, kind, type(exc).__name__,
                        extra={"stage": self.name, "batch": ctx.batch_no})
        self._fail(item, ctx, failure)

    def _classify_failure(self, item: PipelineItem,
                          exc: BaseException) -> tuple[str, str, bool]:
        """把标注异常映射为 §7.6 错误分类（判定序与既有 except 分支序一致）。

        @param item 出错的信封（SchemaViolation 时在此挂载 rejects 取证）
        @param exc 捕获的异常
        @return (kind, message, retryable) 三元组
        """
        if isinstance(exc, SchemaViolation):
            # 把最后一版模型原文经 duck 通道透传给 M11 的 rejects "full" 档（§9.2）。
            item.raw_last_output = exc.raw_last_output  # type: ignore[attr-defined]
            kind = (ErrorKind.CALLBACK_VIOLATION if getattr(exc, "callback_only", False)
                    else ErrorKind.SCHEMA_VIOLATION)
            return kind.value, str(exc), False
        if isinstance(exc, (ContextOverflowError, OutputTruncatedError)):
            # v1.11（V27①）：预算词表先行路由——精确 kind，记录级 failed → rejects。
            # 终态喂熔断已在 _degrading_call 内发生（A7——duck 标位幂等）。
            return budget.classify_stage_error(exc), str(exc), False
        if isinstance(exc, ProviderRetryableError):
            return ErrorKind.PROVIDER_RETRYABLE_EXHAUSTED.value, str(exc), True
        if isinstance(exc, ProviderFatalError):
            return ErrorKind.PROVIDER_FATAL.value, str(exc), False
        if item.record.modality == "ui" and isinstance(exc, OSError):
            return (ErrorKind.IMAGE_DECODE_ERROR.value,
                    f"{type(exc).__name__}: {exc}", False)
        return ErrorKind.INTERNAL_ERROR.value, f"{type(exc).__name__}: {exc}", False

    def _commit_annotation(self, item: PipelineItem, ctx: "RunContext") -> None:
        """按 item 声明序提交 annotate.done 事件。

        @param item 已标注的信封
        @param ctx 运行上下文
        @return 无
        """
        record = item.record
        label = item.classification.label if item.classification else None
        payload: dict = {"attempts": item.annotation.attempts}
        if item.annotation.sc is not None:
            payload["sc"] = dict(item.annotation.sc)
        if label is not None:
            payload["label"] = label
        excerpt = self._excerpt_payload(record)
        if excerpt is not None:
            payload["excerpt"] = excerpt
        ctx.metrics.event(EV_ANNOTATE_DONE, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(record.id,), payload=payload)

    async def _frame_pass(self, item: PipelineItem, ctx: "RunContext") -> None:
        """公开测试面：对单信封执行帧标注波次。

        @param item 待执行帧 pass 的信封
        @param ctx 运行上下文
        """
        await self._frame_wave([item], ctx, 0)

    def _frame_plans(self, items: Sequence[PipelineItem]
                     ) -> tuple[list[_FramePlan], list[tuple[Record, str | None]]]:
        """按 item/member 序冻结唯一成员与跳过事实。

        @param items sequence 归并后仍 active 的信封
        @return 归并计划与需要真实调用的成员对
        """
        plans: list[_FramePlan] = []
        calls: list[tuple[Record, str | None]] = []
        for item in items:
            if not self._frame_gate(item):
                continue
            occupied = item.member_annotations or {}
            seen: set[str] = set()
            for member in item.record.members:
                if member.id in occupied or member.id in seen:
                    continue
                seen.add(member.id)
                label = self._member_label(item, member)
                view = self.cfg.frame_class_views.get(label) if label is not None else None
                skipped = view is not None and not view.enabled
                call_index = None if skipped else len(calls)
                plans.append(_FramePlan(item, member, label, skipped, call_index))
                if not skipped:
                    calls.append((member, label))
        return plans, calls

    def _frame_gate(self, item: PipelineItem) -> bool:
        """判定一个信封是否进入帧标注 pass。

        @param item 待判定信封
        @return 是否进入帧 pass
        """
        if (item.status != "active" or not self.cfg.frame_annotate.enabled
                or item.record.kind != "sequence"):
            return False
        cls = item.classification
        if cls is not None and cls.labels and cls.label != cls.labels[0]:
            return False
        return getattr(item, "segment_degraded", None) is None

    @staticmethod
    def _member_label(item: PipelineItem, member: Record) -> str | None:
        """读取一个成员的新鲜帧类标签。

        @param item 所属序列信封
        @param member 成员帧
        @return 成员类标签；未分类时为 None
        """
        classification = (item.member_classifications or {}).get(member.id)
        return classification.label if classification is not None else None

    async def _frame_wave(self, items: Sequence[PipelineItem], ctx: "RunContext",
                          start: int) -> None:
        """在 sequence 归并屏障后执行全批成员叶并声明序提交。

        @param items sequence 归并后的信封
        @param ctx 运行上下文
        @param start 本波次起始 ordinal
        """
        plans, calls = self._frame_plans(items)
        outcomes = await _execute_members(calls, ctx, start)
        for plan in plans:
            outcome = None if plan.skipped else outcomes[plan.call_index]
            self._settle_frame(plan, outcome, ctx)

    def _settle_frame(self, plan: _FramePlan, outcome: _MemberOutcome | None,
                      ctx: "RunContext") -> None:
        """按 item/member 声明序提交成员表、计数与事件。

        @param plan 成员归并计划
        @param outcome 成员叶结果；skipped 时为 None
        @param ctx 运行上下文
        """
        member = plan.member
        annotations = plan.item.member_annotations
        if annotations is None:
            annotations = {}
            plan.item.member_annotations = annotations
        if plan.skipped:
            ctx.metrics.count("frame_annotate.skipped")
            payload: dict = {"member_id": member.id, "status": "skipped", "attempts": 0}
        else:
            if outcome is None:
                raise InternalError("frame annotation result is missing")
            annotation = self._settle_member_outcome(plan, outcome, ctx)
            annotations[member.id] = annotation
            payload = {"member_id": member.id,
                       "status": "annotated" if annotation is not None else "failed",
                       "attempts": annotation.attempts if annotation is not None else 0}
            if self._frame_excerpt_enabled(annotation):
                payload["excerpt"] = {member.id: _dumps(annotation.output)[:200]}
        ctx.metrics.event(EV_ANNOTATE_FRAME, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(plan.item.record.id,), payload=payload)

    def _settle_member_outcome(self, plan: _FramePlan, outcome: _MemberOutcome,
                               ctx: "RunContext") -> Annotation | None:
        """把一个成员叶结果提交为 ordinary 或 attempt 语义。

        @param plan 成员归并计划
        @param outcome 成员叶结果
        @param ctx 运行上下文
        @return 成功 Annotation 或 ordinary 失败 None
        """
        if outcome.error is not None:
            if _ATTEMPT_MODE.get():
                raise outcome.error
            _record_member_failure(plan.member, ctx, outcome.error)
            return None
        if outcome.annotation is None:
            raise InternalError("frame annotation completed without a value")
        ctx.metrics.count("frame_annotate.annotated")
        return outcome.annotation

    def _frame_excerpt_enabled(self, annotation: Annotation | None) -> bool:
        """判定成员事件是否可以附带截断标注。

        @param annotation 待输出的成员标注
        @return 是否附带 excerpt
        """
        return (annotation is not None and self.cfg.trace.enabled
                and self.cfg.trace.content in ("excerpt", "full"))

    def _excerpt_payload(self, record: Record) -> dict | None:
        """算出 annotate.done 事件的 `excerpt` payload 增项。

        @param record 被标注记录
        @return {record_id: 摘录} 字典；档位不满足时为 None

        §7.4：四个 trace.content 档位逐档递增——"full" 含 "excerpt" 的一切，故摘录
        在两个档位都附上。
        """
        if not (self.cfg.trace.enabled and self.cfg.trace.content in ("excerpt", "full")):
            return None
        return {record.id: self._excerpt(record)}

    @staticmethod
    def _excerpt(record: Record) -> str:
        """截取记录内容的前 200 字作为取证摘录。

        @param record 被标注记录（text 模态取正文，ui 模态取控件树渲染）
        @return 至多 200 字的摘录文本
        """
        content = record.text if record.modality == "text" else (
            record.ui_tree.serialize() if record.ui_tree is not None else "")
        return (content or "")[:200]

    def _fail(self, item: PipelineItem, ctx: "RunContext",
              failure: tuple[str, str, bool]) -> None:
        """把信封落为记录级失败（写 item.errors、置 failed、发 error 事件）。

        @param item 失败的信封
        @param ctx 运行上下文
        @param failure (kind, message, retryable) 三元组
        @return 无
        """
        kind, message, retryable = failure
        err = StageError(stage=self.name, kind=kind, message=message, retryable=retryable)
        item.errors.append(err)
        item.status = "failed"
        if kind == ErrorKind.CONTEXT_OVERFLOW.value:
            ctx.metrics.count("budget.overflow_records")  # V13②：被拒记录，全相位
        ctx.metrics.event(EV_ERROR, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(item.record.id,),
                          payload={"stage": self.name, "kind": kind,
                                   "message": message, "retryable": retryable})

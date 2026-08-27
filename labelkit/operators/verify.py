"""M7 verify：经典记录评审、流式序列修复与 sequence attempt gate。"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
from contextvars import ContextVar
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Mapping, Sequence

from labelkit.common.errors import (
    CircuitBreakerTripped,
    ContextOverflowError,
    ErrorKind,
    InternalError,
    ProviderFatalError,
    ProviderRetryableError,
    SchemaViolation,
)
from labelkit.common.contracts.generation import DownstreamAttemptRequest, DownstreamAttemptResult
from labelkit.common.contracts.execution import TaskGroupRequest, TaskSpec
from labelkit.common.contracts.types import (
    Annotation,
    PipelineItem,
    Record,
    StageError,
    Transition,
    VerificationResult,
    frame_digest,
)
from labelkit.common.inference import budget
from labelkit.common.inference.schema_engine import _thaw_json

if TYPE_CHECKING:
    from labelkit.common.config.model import LLMProfile, ResolvedConfig
    from labelkit.common.inference.llm_client import PromptBundle
    from labelkit.common.contracts.stage import RunContext
    from labelkit.operators.annotate import RepairContext

_log = logging.getLogger("labelkit.verify")
_ATTEMPT_MODE: ContextVar[bool] = ContextVar("verify_attempt_mode", default=False)
_ATTEMPT_CONFIG: ContextVar[object | None] = ContextVar("verify_attempt_config", default=None)
EV_VERIFY_VERDICT = "verify.verdict"
EV_ERROR = "error"

_SYSTEM_HEAD = (
    "你是标注质量审核员。给定任务指令、原始数据与标注结果，独立判断标注是否合格。"
)
_SYSTEM_DIMS = (
    "评审维度: ① 是否遵循任务指令 ② 与原始数据的事实一致性 ③ 字段语义是否正确填写"
)
_SYSTEM_TAIL = "先逐维度给出简短意见，再给结论。"

_SEQ_SYSTEM_HEAD = (
    "你是标注质量审核员。给定任务指令、动作序列、边界余量与首末帧截图，独立判断该序列\n"
    "（episode）的标注是否合格。"
)
_SEQ_SYSTEM_DIMS = (
    "评审维度: ① 是否遵循任务指令 ② 与动作序列及首末帧证据的事实一致性 "
    "③ 字段语义是否正确填写\n"
    "④ 段边界与成员构成是否成立（对照下列缺陷类型）"
)
_SEQ_SYSTEM_DEFECT_TYPES = (
    "缺陷类型（发现即列入 defects，可为空数组）:\n"
    "- label_mismatch: 标注的任务标签与序列证据不符\n"
    "- off_task_members: 段内混入与任务无关的成员帧（members 列出这些成员帧 id）\n"
    "- missing_head: 段首缺少任务起点帧（结合边界余量判断）\n"
    "- missing_tail: 段尾缺少任务终点帧（结合边界余量判断）\n"
    "- missing_members: 段中缺失成员帧（members 列出可指认的帧 id，无从指认则为 null）\n"
    "- wrong_stitch: 线索缝合错误——各碎片并非同一任务的延续（结合片段结构判断）")
_SEQ_SYSTEM_TAIL = "先逐维度给出简短意见，再列缺陷表，最后给结论。"
_SEQ_SYSTEM_STRUCTURE = (
    "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
    '{"critiques": [{"aspect": <维度>, "opinion": <一句话意见>}, ...],\n'
    ' "defects": [{"kind": <缺陷类型>, "members": <帧 id 数组|null>,\n'
    '              "position": <位置说明|null>, "detail": <一句话>}, ...],\n'
    ' "verdict": "pass"|"fail"}')

_LABEL_ACTION_SEQUENCE = "[动作序列]"
_LABEL_FRAGMENT_STRUCTURE = "[片段结构]"   # v1.9（T15）：第七段
_LABEL_BOUNDARY_MARGIN = "[边界余量]"
_LABEL_FIRST_FRAME = "[首帧截图]"
_LABEL_LAST_FRAME = "[末帧截图]"
_MEMBER_DIGEST_MAX_CHARS = 400   # 序列 excerpt 档摘要上限（镜像 M4 §7.3）

_VERDICT_SEQ_SYSTEM_HEAD = (
    "你是标注质量审核员。给定任务指令、成员帧摘要与标注结果，独立判断该序列"
    "（episode）的标注是否合格。"
)
_VERDICT_SEQ_SYSTEM_DIMS = ("评审维度: ① 是否遵循任务指令 ② 与成员帧摘要证据的事实一致性 "
                            "③ 字段语义是否正确填写")
_VERDICT_SEQ_SYSTEM_STRUCTURE = (
    "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
    '{"critiques": [{"aspect": <维度>, "opinion": <一句话意见>}, ...],\n'
    ' "verdict": "pass"|"fail"}')
_LABEL_MEMBER_DIGESTS = "[成员帧摘要]"

DEFECT_KINDS = ("label_mismatch", "off_task_members", "missing_head",
                "missing_tail", "missing_members", "wrong_stitch")
_MISSING_KINDS = frozenset({"missing_head", "missing_tail", "missing_members"})
_RECLAIM_RELATIONS = frozenset({"continues", "advances"})
_DEFAULT_FAIL_DEFECT: Mapping = {
    "kind": "label_mismatch", "members": None, "position": None,
    "detail": "评审判 fail 但未指认缺陷，默认视同标签不符",
}
_BOUNDARY_MARGIN_K = 2
_COUNTER_MEMBERSHIP_REPAIRS = "verify.membership_repairs"
_COUNTER_BOUNDARY_FLAGS = "verify.boundary_flags"
_COUNTER_DEFECTS_PREFIX = "verify.defects."


_TREE_MARKER_RE = re.compile(r"^…\(truncated (\d+) nodes\)$")
@dataclasses.dataclass
class _PromptFit:
    """一次评审提示词的面板最小预算装填状态。"""
    input_budget: int       # 面板最小可用输入预算（token）
    image_cost: int         # 单图成本（面板内最大标定读数；text 模态为 0）
    truncations: int = 0    # 本次装填实际发生的裁剪次数（进 budget.truncations.verify）
    overflow: bool = False  # 最小单元仍越预算（V10——调用方拒绝，请求从不发出）


@dataclasses.dataclass(frozen=True)
class VerifyPromptOptions:
    """``build_verify_prompt`` 的可选装配项；缺省实例是经典单记录调用形态。"""
    label: str | None = None                            # 类标签；None = 取全局指令与准则（v1.7 R3）
    transitions: tuple[Transition, ...] | None = None   # 序列步表；None = 整段省略 [动作序列]（v1.8 S7）
    boundary_margin: str = ""              # [边界余量] 段正文（驱动器预渲染）
    fragment_structure: str = ""                        # [片段结构] 段正文；空串 = 整段省略（v1.9 T15）
    fit: "_PromptFit | None" = None                     # 面板最小预算装填状态；None = 预算关（v1.11）
    verdict_form: bool = False                          # 生成序列走 §10.16 判决形变体


_DEFAULT_PROMPT_OPTIONS = VerifyPromptOptions()


@dataclasses.dataclass(frozen=True)
class _VerdictEvent:
    """一条 ``verify.verdict`` trace 事件的载荷来源（§7.2 事件目录，字段只增不改）。"""
    record: Record                # 被评审记录（取 id / 模态 / 摘要源）
    verdict: str                  # 该 judge 的结论："pass" | "fail"
    round_no: int                 # 评审轮次（1 基，含首评）
    critiques: Sequence[Mapping]  # 该 judge 的逐维度意见（原样携带，脱敏归 M12）
    judge: str | None             # judge profile 名；单 judge 面板为 None
    label: str | None = None      # 类标签（仅 classify 启用时进 payload，v1.7 R5）
    defects: Sequence[Mapping] | None = None  # 缺陷表（仅流式序列评审携带，S27/S31）


@dataclasses.dataclass(frozen=True)
class _RoutingScope:
    """一个 episode 做缺陷路由时的批级作用域（同步阶段只读，claimed 由路由方写入）。"""
    frames: list[PipelineItem]  # 本会话的帧信封（批位序 = 会话序）
    claimed: set[int]           # 本轮已被预定的噪声信封 id()（跨 episode 共享）
    clone: bool                 # 多标签扇出克隆信封——禁止成员手术（S8）
    split: bool                 # 会话在 batch_size 处被硬切（S21）——回收降级为仅标记


@dataclasses.dataclass(frozen=True)
class _LadderTrial:
    """V21 修复梯的一次升档试装参数（zero 调用，只做估算）。"""
    item: PipelineItem                     # 待重标注的序列信封（取 record 与 transitions）
    repair: "RepairContext"                # M5 修复上下文（试装提示词需嵌入）
    label: str | None                      # 类标签（按类取指令与 Schema）
    fragment_lens: tuple[int, ...] | None  # 每碎片成员数（关键帧配额，T14 穿参义务）
    k_eff: int                             # 试装用关键帧配额（k 减半后的值）
    image_px: int                          # 试装用图像采样上限（升档后的像素）


def _feed_reactive_terminal(exc: BaseException, metrics) -> None:
    """A7/§7.8 熔断矩阵：只有 reactive-400 溢出终态喂熔断连击。

    每个异常对象恰喂一次；precheck 与 200 形态的 finish 判据永不喂。
    ``origin`` 防御性读取（默认 "http_400"）。
    @param exc 记录级终态异常
    @param metrics M12 计数器汇（record_provider_result 入口）
    """
    if (isinstance(exc, ContextOverflowError) and exc.phase == "reactive"
            and getattr(exc, "origin", "http_400") == "http_400"
            and not getattr(exc, "_breaker_fed", False)):
        exc._breaker_fed = True  # type: ignore[attr-defined]
        metrics.record_provider_result(fatal=True)


def _fit_tree_text(rendered: str, budget_tokens: int) -> tuple[str, bool]:
    """§3.3③ 序列化控件树的动态收口：超份额则从尾部丢 NODE 行，以 serialize 家族同款标记
    "…(truncated N nodes)" 收口——N 累加到既有标记的计数上。est_text 前缀单调 ⇒ 二分。
    @param rendered 控件树渲染文本（已在 ui_tree_max_chars 绝对上限内）
    @param budget_tokens 该槽位可用 token 份额
    @return (收口后的文本, 是否发生了裁剪)
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
        """保留前 keep 行并以累加计数的截断标记收口的候选文本。
        @param keep 保留的 NODE 行数
        @return 候选渲染文本
        """
        marker = f"…(truncated {base + total - keep} nodes)"
        return "\n".join(lines[:keep] + [marker])

    lo, hi = 0, total - 1                        # keep == total 已知放不下
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if budget.est_text(candidate(mid)) <= budget_tokens:
            lo = mid
        else:
            hi = mid - 1
    return candidate(lo), True


# ── 纯提示词文本装配（可单测，无服务导入）────────────────────────────

def verify_system_text(extra_criteria: str) -> str:
    """§10.5 评审提示词的 system 段；``verify.extra_criteria`` 为空时整行省略。
    @param extra_criteria 类有效附加评审准则
    @return 换行拼接的 system 文本
    """
    lines = [_SYSTEM_HEAD, _SYSTEM_DIMS]
    if extra_criteria:
        lines.append(extra_criteria)
    lines.append(_SYSTEM_TAIL)
    return "\n".join(lines)


def verify_user_text(instruction: str, record_text: str, output: Mapping) -> str:
    """§10.5 评审提示词的 user 段，text 模态。
    @param instruction 类有效任务指令
    @param record_text 原始数据文本
    @param output 待评审的标注对象
    @return 三段拼接的 user 文本
    """
    return (
        f"[任务指令] {instruction}\n"
        f"[原始数据] {record_text}\n"
        f"[标注结果] {json.dumps(output, ensure_ascii=False)}"
    )


def verify_sequence_system_text(extra_criteria: str) -> str:
    """§10.5 v1.8 序列变体的 system 段：评审维度 + 缺陷类型说明 + 结构句；extra_criteria 为空
    时整行省略（与非流规则同款）。
    @param extra_criteria 类有效附加评审准则
    @return 换行拼接的 system 文本
    """
    lines = [_SEQ_SYSTEM_HEAD, _SEQ_SYSTEM_DIMS]
    if extra_criteria:
        lines.append(extra_criteria)
    lines.extend((_SEQ_SYSTEM_DEFECT_TYPES, _SEQ_SYSTEM_TAIL, _SEQ_SYSTEM_STRUCTURE))
    return "\n".join(lines)


def verify_verdict_sequence_system_text(extra_criteria: str) -> str:
    """判决形生成序列变体（§10.16）的 system 段：三维评审 + 结论指令 +
    VERDICT_SCHEMA 结构句——无缺陷词表；extra_criteria 为空时整行省略（非流规则同款）。
    @param extra_criteria 类有效附加评审准则（[class.<name>.verify] 覆盖后取值）
    @return 换行拼接的 system 文本
    """
    lines = [_VERDICT_SEQ_SYSTEM_HEAD, _VERDICT_SEQ_SYSTEM_DIMS]
    if extra_criteria:
        lines.append(extra_criteria)
    lines.extend((_SYSTEM_TAIL, _VERDICT_SEQ_SYSTEM_STRUCTURE))
    return "\n".join(lines)


def _member_digest_lines(members: Sequence[Record], max_total_chars: int) -> list[str]:
    """构造逐成员的成员帧摘要行。

    总量受 max_total_chars 约束：首末行恒保留，中段整行丢弃并以截断标记收口。
    @param members 成员帧记录（成员序）
    @param max_total_chars 摘要块总字符上限（input.ui_tree_max_chars）
    @return 摘要行列表
    """
    lines = [f"{m}. {frame_digest(member, _MEMBER_DIGEST_MAX_CHARS)}"
             for m, member in enumerate(members, start=1)]
    if len(lines) <= 2 or len("\n".join(lines)) <= max_total_chars:
        return lines
    last = lines[-1]
    keep = 1                 # 首行即使超预算也保留（M5 同款地板）
    for k in range(len(lines) - 2, 0, -1):
        marker = f"…(truncated {len(lines) - k - 1} members)"
        if len("\n".join(lines[:k] + [marker, last])) <= max_total_chars:
            keep = k
            break
    marker = f"…(truncated {len(lines) - keep - 1} members)"
    return lines[:keep] + [marker, last]


def sequence_step_line(transition: Transition) -> str:
    """构造一行 [动作序列] 步文本。

    普通评审证据不带摘取兜底后缀；线索接缝占位步保留打断来源。
    @param transition 步记录
    @return 单行步文本
    """
    action = transition.action
    target = action.get("target")
    value = action.get("value")
    line = (f"{transition.index}. {action.get('action_type')}"
            f"（对象: {'—' if target is None else target}；"
            f"值: {'—' if value is None else value}）"
            f"{action.get('description')}")
    if transition.detail.get("kind") == "thread_seam":
        names = "、".join(transition.detail.get("interrupted_by") or ())
        line += f"（线索接缝：被{names}打断）"
    return line


def normalize_defects(entries: Sequence[Mapping]) -> list[dict]:
    """按 kind、position 与 members 确定性排序并去重缺陷表。

    同键条目由 judge 配置序与条目序决定幸存者；结果做浅拷贝。
    @param entries 多 judge 判 fail 时给出的缺陷条目并集
    @return 规范化后的缺陷表
    """
    def key(entry: Mapping) -> tuple:
        """缺陷条目的确定性排序/去重键。
        @param entry 单条缺陷
        @return (kind 枚举序, position, members 元组)
        """
        return (DEFECT_KINDS.index(entry["kind"]),
                entry.get("position") or "",
                tuple(entry.get("members") or ()))

    seen: set[tuple] = set()
    out: list[dict] = []
    for entry in sorted(entries, key=key):
        k = key(entry)
        if k in seen:
            continue
        seen.add(k)
        out.append(dict(entry))
    return out


def _session_frame_envelopes(batch: Sequence[PipelineItem],
                             session_id: str | None) -> list[PipelineItem]:
    """按批位序取得本会话的帧信封。
    @param batch 本批信封列表
    @param session_id 会话 id
    @return 该会话的单记录信封列表
    """
    return [it for it in batch
            if it.record.kind == "single" and it.session_id == session_id]


def _session_episodes(batch: Sequence[PipelineItem],
                      session_id: str | None) -> list[PipelineItem]:
    """按批位序取得本会话的 episode 信封，并按 record id 去重。

    stitched 壳被排除，避免陈旧成员集污染段序与余量去向。
    @param batch 本批信封列表
    @param session_id 会话 id
    @return 该会话的序列信封列表
    """
    seen: set[str] = set()
    out: list[PipelineItem] = []
    for it in batch:
        if (it.record.kind == "sequence" and it.session_id == session_id
                and it.status != "stitched" and it.record.id not in seen):
            seen.add(it.record.id)
            out.append(it)
    return out


def _seam_position_line(item: PipelineItem) -> str:
    """[片段结构] 段的接缝位置行（seam_indexes 为左成员下标 = 步下标，m-8）。
    @param item 线索信封（读 M16 的 duck 标记）
    @return "接缝位置: …" 单行（无接缝为「无」）
    """
    seams = tuple(getattr(item, "seam_indexes", ()) or ())
    interrupted = tuple(getattr(item, "seam_interrupted_by", ()) or ())
    if not seams:
        return "接缝位置: 无"
    entries = []
    for j, idx in enumerate(seams):
        names = "、".join(interrupted[j]) if j < len(interrupted) else ""
        entries.append(f"步 {idx}（被{names}打断）" if names else f"步 {idx}")
    return "接缝位置: " + "；".join(entries)


def fragment_structure_text(item: PipelineItem, digest_max_chars: int) -> str:
    """构造 [片段结构] 段正文。

    每碎片一行，末行给出接缝位置；未缝合或不匹配时降级为单个隐含碎片。
    @param item 线索/episode 信封
    @param digest_max_chars 首帧摘要字符上限
    @return 多行段正文
    """
    members = item.record.members
    fragments = list(getattr(item, "stitch_fragments", ()) or ())
    counts = [int(f.get("member_count", 0)) for f in fragments]
    if not fragments or sum(counts) != len(members):
        counts = [len(members)]                      # 单个隐含碎片
    lines: list[str] = []
    start = 0
    total = len(counts)
    for k, count in enumerate(counts, 1):
        end = start + count - 1
        digest = (frame_digest(members[start], digest_max_chars)
                  if start < len(members) else "")
        lines.append(f"碎片 {k}/{total}: 成员 {start}–{end}（{count} 帧）"
                     f"｜首帧摘要: {digest}")
        start += count
    lines.append(_seam_position_line(item))
    return "\n".join(lines)


def _episode_membership(batch: Sequence[PipelineItem],
                        session_id: str | None) -> dict[str, int]:
    """帧 id → 所属 episode 的会话内序号（1 基，批序）。
    @param batch 本批信封列表
    @param session_id 会话 id
    @return 归属表；首次出现者胜出
    """
    membership: dict[str, int] = {}
    for ordinal, episode in enumerate(_session_episodes(batch, session_id), 1):
        for member in episode.record.members:
            membership.setdefault(member.id, ordinal)
    return membership


def _frame_fate(frame: PipelineItem, membership: Mapping[str, int]) -> str:
    """一帧在 [边界余量] 里的去向文本。
    @param frame 帧信封
    @param membership 帧 id → episode 序号
    @return "noise" / "第 n 段" / "无"
    """
    if frame.status == "dropped_noise":
        return "noise"
    ordinal = membership.get(frame.record.id)
    return f"第 {ordinal} 段" if ordinal is not None else "无"


def boundary_margin_text(item: PipelineItem, batch: Sequence[PipelineItem],
                         digest_max_chars: int) -> str:
    """[边界余量] 段正文（spec 3.7.2）：每侧段边界外 k = 2 帧的摘要与去向，取同 session_id 的批位序
    邻域（段首成员之前 / 段尾成员之后），越界位置渲染成裸「无」行；行序按时间（段首前 2、段首前
    1、段尾后 1、段尾后 2）。纯代码读批状态——零 LLM 调用、零随机。
    @param item 被评审的序列信封
    @param batch 本批信封列表（邻域来源）
    @param digest_max_chars 帧摘要字符上限
    @return 四行段正文
    """
    frames = _session_frame_envelopes(batch, item.session_id)
    position_of: dict[str, int] = {}
    for i, frame in enumerate(frames):
        position_of.setdefault(frame.record.id, i)
    members = item.record.members
    head = position_of.get(members[0].id) if members else None
    tail = position_of.get(members[-1].id) if members else None
    membership = _episode_membership(batch, item.session_id)

    lines: list[str] = []
    for label, base, offsets in (("段首前", head, (-_BOUNDARY_MARGIN_K, -1)),
                                 ("段尾后", tail, (1, _BOUNDARY_MARGIN_K))):
        for offset in offsets:
            distance = abs(offset)
            pos = None if base is None else base + offset
            if pos is None or not 0 <= pos < len(frames):
                lines.append(f"{label} {distance}: 无")
            else:
                frame = frames[pos]
                digest = frame_digest(frame.record, digest_max_chars)
                lines.append(f"{label} {distance}: {digest}"
                             f"（去向: {_frame_fate(frame, membership)}）")
    return "\n".join(lines)


def majority_verdict(verdicts: Sequence[str]) -> Literal["pass", "fail"]:
    """奇数个 pass/fail 判决的多数表决（单 judge 即长度 1）。
    @param verdicts 各 judge 的结论
    @return 面板结论
    """
    fails = sum(1 for v in verdicts if v == "fail")
    return "fail" if fails * 2 > len(verdicts) else "pass"


def render_critiques_text(critiques: Sequence[Mapping]) -> str:
    """渲染 M5 修复后缀所需的意见文本（§10.5）：一条一行 ``aspect: opinion``；带 ``judge`` 键的
    条目（多 judge）渲染成 ``judge_name/aspect: opinion``。
    @param critiques 意见条目
    @return 多行文本
    """
    lines = []
    for c in critiques:
        prefix = f"{c['judge']}/" if "judge" in c else ""
        lines.append(f"{prefix}{c['aspect']}: {c['opinion']}")
    return "\n".join(lines)


def _critique_entries(critiques: Sequence[Mapping], judge: str | None) -> list[dict]:
    """把一个 judge 的逐维度意见转成内部条目（多 judge 面板附 ``judge`` 字段）。
    @param critiques judge 返回的意见数组
    @param judge judge profile 名；None = 单 judge，不附字段
    @return 意见条目列表
    """
    entries: list[dict] = []
    for c in critiques:
        entry = {"aspect": c["aspect"], "opinion": c["opinion"]}
        if judge is not None:
            entry["judge"] = judge
        entries.append(entry)
    return entries


def _class_effective_texts(cfg: "ResolvedConfig",
                           label: str | None) -> tuple[str, str]:
    """评审模板的类有效文本（v1.7 R3）。
    @param cfg 已解析配置
    @param label 类标签；None = 取全局配置
    @return (任务指令, 附加评审准则)
    """
    if label is not None:
        view = cfg.class_views[label]
        return view.annotate.instruction, view.verify.extra_criteria
    return cfg.annotate.instruction, cfg.verify.extra_criteria


def _build_verdict_sequence_prompt(record: Record, output: Mapping,
                                   cfg: "ResolvedConfig",
                                   texts: tuple[str, str],
                                   fit: "_PromptFit | None") -> "PromptBundle":
    """判决形生成序列 prompt 装配（§10.16）：user 段 = [任务指令] →
    [成员帧摘要]（400 字/成员、ui_tree_max_chars 总量中段丢弃——镜像 M5 渲染）→ [标注结果]；无
    缺陷表/边界余量/片段结构，无截图段（生成序列是 text 模态）。``fit`` 非 None 时成员摘要块是唯一
    可裁槽位（§3.3⑤ edges 裁剪），[标注结果]/指令恒计不裁（V25③）；不可裁地板超预算 ⇒
    fit.overflow（V10——调用方拒绝，请求从不发出）。
    @param record kind == "sequence" 的生成序列记录
    @param output 待评审的标注对象
    @param cfg 已解析配置（ui_tree_max_chars 总量取值）
    @param texts (类有效任务指令, 类有效 extra_criteria)
    @param fit v1.11 面板最小预算装填状态；None = 预算关
    @return 两条消息的提示词包
    """
    from labelkit.common.inference.llm_client import Message, Part, PromptBundle

    instruction, extra_criteria = texts
    system_text = verify_verdict_sequence_system_text(extra_criteria)
    head = f"[任务指令] {instruction}"
    digest_body = "\n".join(
        _member_digest_lines(record.members, cfg.input.ui_tree_max_chars))
    result_text = f"[标注结果] {json.dumps(output, ensure_ascii=False)}"
    if fit is not None:
        fixed = (budget.est_text(system_text) + budget.est_text(head)
                 + budget.est_text(f"{_LABEL_MEMBER_DIGESTS}\n")
                 + budget.est_text(result_text) + 2 * budget.MSG_OVERHEAD_TOKENS)
        slot = fit.input_budget - fixed
        if budget.est_text(digest_body) > slot:
            digest_body = budget.fit_text(digest_body, max(0, slot), keep="edges")
            fit.truncations += 1
        fit.overflow = fixed + budget.est_text(digest_body) > fit.input_budget
    parts = (Part(kind="text", text=head),
             Part(kind="text", text=f"{_LABEL_MEMBER_DIGESTS}\n{digest_body}"),
             Part(kind="text", text=result_text))
    return PromptBundle(messages=(
        Message(role="system", parts=(Part(kind="text", text=system_text),)),
        Message(role="user", parts=parts)))


def _fit_sequence_parts(parts: list, system_text: str, steps_at: int,
                        n_images: int, fit: _PromptFit) -> None:
    """§10.5 序列变体的预算装填：唯一可裁槽位是 [动作序列] 步表块（§3.3⑤ edges 裁剪），其余文本
    段与图像成本恒计不裁（V25③）；装填后总量仍越预算则置 fit.overflow（V10）。
    @param parts user 消息的 Part 列表（就地替换步表段）
    @param system_text system 段文本（计入固定量）
    @param steps_at 步表段在 parts 中的下标；-1 = 该段整段省略
    @param n_images 图像段数量（UI 序列为 2，text 为 0）
    @param fit 面板最小预算装填状态
    """
    from labelkit.common.inference.llm_client import Part

    fixed = (budget.est_text(system_text)
             + sum(budget.est_text(p.text or "") for i, p in enumerate(parts)
                   if p.kind == "text" and i != steps_at)
             + 2 * budget.MSG_OVERHEAD_TOKENS + n_images * fit.image_cost)
    if steps_at >= 0:
        slot = (fit.input_budget - fixed
                - budget.est_text(f"{_LABEL_ACTION_SEQUENCE}\n"))
        steps = parts[steps_at].text.split("\n", 1)[1]
        if budget.est_text(steps) > slot:
            steps = budget.fit_text(steps, max(0, slot), keep="edges")
            fit.truncations += 1
            parts[steps_at] = Part(kind="text",
                                   text=f"{_LABEL_ACTION_SEQUENCE}\n{steps}")
    total = (budget.est_text(system_text)
             + sum(budget.est_text(p.text or "") for p in parts if p.kind == "text")
             + 2 * budget.MSG_OVERHEAD_TOKENS + n_images * fit.image_cost)
    fit.overflow = total > fit.input_budget


def _build_defect_sequence_prompt(record: Record, output: Mapping,
                                  cfg: "ResolvedConfig", texts: tuple[str, str],
                                  options: VerifyPromptOptions) -> "PromptBundle":
    """§10.5 v1.8 缺陷词表序列变体（流式驱动器调用面）。段序：[任务指令] → [动作序列]（transitions
    为 None 时整段省略）→ v1.9 [片段结构]（T15：驱动器按 M16 duck 标记预渲染，空串时整段省略——
    stitch 关则六段形态逐字节不变）→ [边界余量]（驱动器预渲染，它持有批上下文）→ [首帧截图] + 图
    → [末帧截图] + 图 → [标注结果]；text 模态序列降级为无截图段（M5 S6 先例）。
    @param record kind == "sequence" 的记录
    @param output 待评审的标注对象
    @param cfg 已解析配置
    @param texts (类有效任务指令, 类有效 extra_criteria)
    @param options 装配项（步表 / 边界余量 / 片段结构 / 预算装填）
    @return 两条消息的提示词包
    """
    from labelkit.common.inference.llm_client import Message, Part, PromptBundle

    instruction, extra_criteria = texts
    system_text = verify_sequence_system_text(extra_criteria)
    parts: list[Part] = [Part(kind="text", text=f"[任务指令] {instruction}")]
    steps_at = -1
    if options.transitions is not None:      # transitions 为 None 即整段省略
        steps = "\n".join(sequence_step_line(t) for t in options.transitions)
        steps_at = len(parts)
        parts.append(Part(kind="text", text=f"{_LABEL_ACTION_SEQUENCE}\n{steps}"))
    if options.fragment_structure:           # v1.9 第七段（仅 stitch 开启，T15）
        parts.append(Part(
            kind="text",
            text=f"{_LABEL_FRAGMENT_STRUCTURE}\n{options.fragment_structure}"))
    parts.append(Part(kind="text",
                      text=f"{_LABEL_BOUNDARY_MARGIN}\n{options.boundary_margin}"))
    n_images = 0
    if record.modality == "ui":
        parts.append(Part(kind="text", text=_LABEL_FIRST_FRAME))
        parts.append(Part(kind="image", image=record.members[0].image))
        parts.append(Part(kind="text", text=_LABEL_LAST_FRAME))
        parts.append(Part(kind="image", image=record.members[-1].image))
        n_images = 2
    parts.append(Part(kind="text",
                      text=f"[标注结果] {json.dumps(output, ensure_ascii=False)}"))
    if options.fit is not None:
        _fit_sequence_parts(parts, system_text, steps_at, n_images, options.fit)
    return PromptBundle(messages=(
        Message(role="system", parts=(Part(kind="text", text=system_text),)),
        Message(role="user", parts=tuple(parts))))


def _fit_ui_tree(record: Record, cfg: "ResolvedConfig", texts: tuple[str, str, str],
                 fit: _PromptFit | None) -> str:
    """单记录 UI 分支的控件树渲染 + §3.3③ 动态收口。
    @param record UI 模态记录
    @param cfg 已解析配置（ui_tree_max_chars 绝对上限）
    @param texts (system 文本, head 段, [标注结果] 段)——计入固定量
    @param fit 面板最小预算装填状态；None = 预算关，原样渲染
    @return 控件树文本
    """
    tree = record.ui_tree.serialize(max_chars=cfg.input.ui_tree_max_chars)
    if fit is None:
        return tree
    system_text, head, result_text = texts
    fixed = (budget.est_text(system_text) + budget.est_text(head)
             + budget.est_text("[UI 控件树]\n")
             + budget.est_text(f"\n{result_text}")
             + 2 * budget.MSG_OVERHEAD_TOKENS + fit.image_cost)
    tree, trimmed = _fit_tree_text(tree, max(0, fit.input_budget - fixed))
    if trimmed:
        fit.truncations += 1
    return tree


def _build_single_record_prompt(record: Record, output: Mapping,
                                cfg: "ResolvedConfig", texts: tuple[str, str],
                                fit: _PromptFit | None) -> "PromptBundle":
    """§10.5 经典单记录评审 prompt（text / ui 两支，回归锚）。
    @param record kind == "single" 的记录
    @param output 待评审的标注对象
    @param cfg 已解析配置
    @param texts (类有效任务指令, 类有效 extra_criteria)
    @param fit 面板最小预算装填状态；None = 预算关（v1.10 逐字节路径）
    @return 两条消息的提示词包
    """
    from labelkit.common.inference.llm_client import Message, Part, PromptBundle

    instruction, extra_criteria = texts
    system_text = verify_system_text(extra_criteria)
    system = Message(role="system", parts=(Part(kind="text", text=system_text),))
    if record.modality == "text":
        user_text = verify_user_text(instruction, record.text or "", output)
        if fit is not None:
            # 纯文本与标注 JSON 都不是可裁类 → 单记录最小单元放不下即 V10。
            total = (budget.est_text(system_text) + budget.est_text(user_text)
                     + 2 * budget.MSG_OVERHEAD_TOKENS)
            fit.overflow = total > fit.input_budget
        return PromptBundle(messages=(
            system,
            Message(role="user", parts=(Part(kind="text", text=user_text),))))

    head = f"[任务指令] {instruction}\n[原始数据]\n[屏幕截图]"
    result_text = f"[标注结果] {json.dumps(output, ensure_ascii=False)}"
    tree = _fit_ui_tree(record, cfg, (system_text, head, result_text), fit)
    tail = f"[UI 控件树]\n{tree}\n{result_text}"
    if fit is not None:
        total = (budget.est_text(system_text) + budget.est_text(head)
                 + budget.est_text(tail) + 2 * budget.MSG_OVERHEAD_TOKENS
                 + fit.image_cost)
        fit.overflow = total > fit.input_budget
    user = Message(role="user", parts=(Part(kind="text", text=head),
                                       Part(kind="image", image=record.image),
                                       Part(kind="text", text=tail)))
    return PromptBundle(messages=(system, user))


def build_verify_prompt(record: Record, output: Mapping, cfg: "ResolvedConfig",
                        options: VerifyPromptOptions | None = None) -> "PromptBundle":
    """装配一对（记录, 标注结果）的 §10.5 judge 提示词，按记录形态与装配项选模板——三条互斥路线：
    序列 ∧ ``options.verdict_form`` → §10.16 生成判决形变体（whole-set delivery 路径传 True，
    普通 segmentation 驱动器不传）；普通序列 → §10.5 v1.8 缺陷
    词表变体；单记录 → §10.5 经典变体（UI 按 §10.1/§10.2 携带截图与序列化控件树）。
    @param record 被评审记录
    @param output 待评审的标注对象
    @param cfg 已解析配置
    @param options 可选装配项；None = 缺省（v1.7 之前的单记录经典调用形态）
    @return 两条消息的提示词包
    """
    opts = options if options is not None else _DEFAULT_PROMPT_OPTIONS
    texts = _class_effective_texts(cfg, opts.label)
    if record.kind == "sequence" and opts.verdict_form:
        return _build_verdict_sequence_prompt(record, output, cfg, texts, opts.fit)
    if record.kind == "sequence":
        return _build_defect_sequence_prompt(record, output, cfg, texts, opts)
    return _build_single_record_prompt(record, output, cfg, texts, opts.fit)


# ── 策略状态机（纯控制流；judge / repair 由调用方注入）────────────────────────

JudgeRound = Callable[[Annotation, int], Awaitable[tuple[str, list[dict], list[dict]]]]
Reannotate = Callable[[Annotation, list[dict]], Awaitable[Annotation]]


async def run_verify_loop(
    annotation: Annotation,
    judge_round: JudgeRound,
    reannotate: Reannotate,
    policy: Literal["drop", "repair"],
    max_repair_rounds: int,
) -> tuple[Literal["pass", "fail"], int, list[dict], Annotation]:
    """驱动评审/修复循环（spec 3.7.3）。
    @param annotation 起始标注
    @param judge_round ``judge_round(annotation, round_no)`` → (结论, 合并意见, 判 fail 意见)
    @param reannotate ``reannotate(annotation, fail_critiques)`` → 修复后的标注（M5 钩子）
    @param policy 判 fail 后的处置策略
    @param max_repair_rounds 最多重标注轮数
    @return (结论, 已评审轮数（首评计 1）, 累计意见, 最终标注)
    """
    critiques_all: list[dict] = []
    rounds = 0
    while True:
        rounds += 1
        verdict, merged, fail_critiques = await judge_round(annotation, rounds)
        critiques_all.extend(merged)
        if verdict == "pass":
            return "pass", rounds, critiques_all, annotation
        repairs_done = rounds - 1
        if policy != "repair" or repairs_done >= max_repair_rounds:
            return "fail", rounds, critiques_all, annotation
        annotation = await reannotate(annotation, fail_critiques)


def _classify_error(exc: Exception, modality: str) -> tuple[str, bool]:
    """把记录级异常映射成 (StageError.kind, 可重试)。v1.11（V27①）：预算词表优先路由——
    context_overflow / output_truncated 精确归类，绝不落成 internal_error（§3.5 归因）。
    @param exc 记录级异常
    @param modality 记录模态（ui 下的 OSError 归 image_decode_error）
    @return (错误种类, 是否可重试)
    """
    kind = budget.classify_stage_error(exc)
    if kind is not None:
        return kind, False
    if isinstance(exc, SchemaViolation):
        return ErrorKind.SCHEMA_VIOLATION.value, False
    if isinstance(exc, ProviderRetryableError):
        return ErrorKind.PROVIDER_RETRYABLE_EXHAUSTED.value, True
    if isinstance(exc, ProviderFatalError):
        return ErrorKind.PROVIDER_FATAL.value, False
    if modality == "ui" and isinstance(exc, OSError):
        # 图像字节在调用时才懒加载，Pillow 解码/读取失败表现为 OSError
        # （spec §7.6：M7 → failed，kind=image_decode_error）。
        return ErrorKind.IMAGE_DECODE_ERROR.value, False
    return ErrorKind.INTERNAL_ERROR.value, False


def _excerpt_source(record: Record) -> str:
    """trace excerpt 档的取样源（§7.4）；序列记录 text/ui_tree 均为 None，取首成员 frame_digest
    头部（M4 §7.3 的序列规则）。
    @param record 被评审记录
    @return 摘要源文本（调用方再按 200 字截断）
    """
    if record.kind == "sequence":
        return (frame_digest(record.members[0], _MEMBER_DIGEST_MAX_CHARS)
                if record.members else "")
    if record.modality == "text":
        return record.text or ""
    return record.ui_tree.serialize()


class _EpisodeReview:
    """流式驱动器的单 episode 台账（一个序列信封一份，跨驱动器轮次存活）。"""

    # 跨轮字段：被评审信封、类标签（首标签）、已评轮数、累计意见、本轮面板结论、本轮判
    # fail 的意见（回灌 M5）、本轮规范化缺陷表。
    __slots__ = ("item", "ordinal", "label", "rounds", "critiques", "verdict", "fail_critiques",
                 "defects",
                 # 轮内手术字段（begin_round 重置）：手术前成员元组（重建时比对相邻对）、
                 # 成员工作副本、帧 id → 会话内批位序、需重标注标志（label_mismatch）、
                 # 本轮发生过成员手术标志、本轮预定的回收候选、重建步下标 → 重抽 Transition。
                 "orig_members", "working_members", "session_positions",
                 "needs_reannotate", "surgical", "claims", "reseams")

    def __init__(self, item: PipelineItem, ordinal: int):
        """建立一个 episode 的评审台账。
        @param item 待评审的序列信封
        @param ordinal episode 在批内的声明序位置
        """
        self.item = item
        self.ordinal = ordinal
        self.label = item.classification.label if item.classification else None
        self.rounds = 0
        self.critiques: list[dict] = []
        self.verdict: str = ""
        self.fail_critiques: list[dict] = []
        self.defects: list[dict] = []
        self.begin_round()

    def begin_round(self) -> None:
        """重置轮内手术字段（成员工作副本、回收预定、接缝重抽结果）。"""
        self.orig_members: tuple[Record, ...] = self.item.record.members
        self.working_members: list[Record] = list(self.item.record.members)
        self.session_positions: dict[str, int] = {}
        self.needs_reannotate = False
        self.surgical = False
        self.claims: list["_ReclaimClaim"] = []
        self.reseams: dict[int, Transition] = {}


class _ReclaimClaim:
    """一个被预定的回收候选：噪声信封、其会话位置与复判窗口。"""

    __slots__ = ("envelope",         # 候选噪声帧信封
                 "position",         # 候选帧的会话内批位序
                 "window",           # [前成员, 候选, 后成员] 复判窗口
                 "candidate_index")  # 候选帧在窗口中的下标

    def __init__(self, envelope: PipelineItem, position: int,
                 window: list[Record], candidate_index: int):
        """建立一条回收预定。
        @param envelope 候选噪声帧信封
        @param position 候选帧的会话内批位序
        @param window [前成员, 候选, 后成员] 复判窗口（边缘候选无前/后成员）
        @param candidate_index 候选帧在窗口中的下标
        """
        self.envelope = envelope
        self.position = position
        self.window = window
        self.candidate_index = candidate_index


def _qualifies_for_reclaim(frame: PipelineItem, claimed: set[int]) -> bool:
    """候选帧是否可被回收：必须是尚未被本轮预定的噪声帧；verify 自己丢掉的帧（off_task 收缩）
    永不重入——收缩↔回收乒乓的护栏。
    @param frame 帧信封
    @param claimed 本轮已被预定的信封 id() 集合
    @return True = 可作回收候选
    """
    if frame.status != "dropped_noise" or id(frame) in claimed:
        return False
    attribution = getattr(frame, "noise_attribution", None)
    return not (attribution and attribution[0] == "verify")


def _next_image_rung(prof: "LLMProfile") -> int | None:
    """V21 分辨率升档一级：default_image_px × 1.5（取整）并夹在 max_image_px 内；
    default_image_px == 0 表示工作点已经就是 max_image_px——无档可升。
    @param prof annotate profile
    @return 升档后的像素上限；无档可升为 None
    """
    if prof.default_image_px <= 0:
        return None
    candidate = min(int(round(prof.default_image_px * 1.5)), prof.max_image_px)
    return candidate if candidate > prof.default_image_px else None


_BIG_THREE = (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError)


@dataclasses.dataclass
class _ClassicReview:
    """classic verify 的单信封跨轮台账。"""

    item: PipelineItem
    ordinal: int
    label: str | None
    annotation: Annotation
    rounds: int = 0
    critiques: list[dict] = dataclasses.field(default_factory=list)
    verdict: str = ""
    fail_critiques: list[dict] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class _ClassicJudgePlan:
    """classic judge 波次中的一个 item 与 panel 计划。"""

    state: _ClassicReview
    prompt: "PromptBundle"
    judges: tuple[str, ...]
    schema: Mapping


async def _judge_leaf(plan: _ClassicJudgePlan, judge: str,
                      ctx: "RunContext") -> object:
    """执行一个不写业务对象、事件或错误的 judge 叶调用。

    @param plan 冻结的记录评审计划
    @param judge 当前评委 profile
    @param ctx 本次运行上下文
    @return 成功四元组或普通记录级异常
    """
    from labelkit.common.inference.schema_engine import CallScope

    record = plan.state.item.record
    try:
        return await ctx.schema_engine.complete_validated(
            judge, plan.prompt, schema=plan.schema,
            scope=CallScope(record_ids=(record.id,), batch_no=ctx.batch_no),
        )
    except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
        raise
    except ProviderFatalError as exc:
        if _ATTEMPT_MODE.get():
            raise
        return exc
    except Exception as exc:  # 叶任务只回传 ordinary 记录级失败
        return exc


class VerifyStage:
    """M7 评审阶段：经典逐条路径 + v1.8 流式序列驱动器（spec 3.7）。"""

    name = "verify"

    def __init__(self, cfg: "ResolvedConfig"):
        """构造阶段实例。
        @param cfg 已解析配置（判决策略、面板、帧粒度开关均从此读取）
        """
        self._cfg = cfg

    @property
    def cfg(self) -> "ResolvedConfig":
        """@return 当前 attempt 的程序视图配置；普通批次返回构造期配置。"""
        active = _ATTEMPT_CONFIG.get()
        return self._cfg if active is None else active  # type: ignore[return-value]

    async def run(self, batch: list[PipelineItem],
                  ctx: "RunContext") -> list[PipelineItem]:
        """阶段入口：只处理 active 且已有标注的信封（spec §4.3 契约）。v1.8：stream 模式下的
        序列信封分流进驱动器，同批内的单记录残余仍走经典逐条路径；无序列（或 segment 关）时
        末行 v1.8 之前的代码逐字节照跑（回归锚）。
        @param batch 本批信封列表（唯一可变载体）
        @param ctx 本次（批次, 阶段）运行上下文
        @return 传入的同一列表对象
        """
        eligible = [it for it in batch
                    if it.status == "active" and it.annotation is not None]
        if not eligible:
            return batch
        episodes = [item for item in eligible if item.record.kind == "sequence"]
        if episodes and self.cfg.segment.enabled:
            singles = [item for item in eligible if item.record.kind != "sequence"]
            await self._run_classic(singles, ctx)
            from labelkit.operators.stream_verify import StreamVerifyDriver

            await StreamVerifyDriver(self).run(batch, episodes, ctx)
            return batch
        await self._run_classic(eligible, ctx)
        return batch

    async def run_attempt(
        self,
        request: DownstreamAttemptRequest,
    ) -> DownstreamAttemptResult:
        """执行 whole-set verify gate。@param request 当前事务与运行上下文。@return 接受状态、拒绝阶段和计数。"""
        items = list(request.transaction.items)
        config_token = _ATTEMPT_CONFIG.set(request.run_context.cfg)
        try:
            token = _ATTEMPT_MODE.set(True)
            try:
                with request.run_context.metrics.capture_counts() as counters:
                    try:
                        await self.run(items, request.run_context)
                    except SchemaViolation:
                        return DownstreamAttemptResult(
                            accepted=False, rejected_stage="verify",
                            dataset_counters=dict(counters))
            finally:
                _ATTEMPT_MODE.reset(token)
            self._assert_no_provider_fatal(items)
            accepted = all(item.status == "active" and item.verification is not None
                           for item in items)
            return DownstreamAttemptResult(
                accepted=accepted,
                rejected_stage=None if accepted else "verify",
                dataset_counters=dict(counters),
            )
        finally:
            _ATTEMPT_CONFIG.reset(config_token)

    @staticmethod
    def _assert_no_provider_fatal(items: list[PipelineItem]) -> None:
        """把 attempt items 中误隔离的 provider fatal 升为下游协议破坏。"""
        if any(error.kind == ErrorKind.PROVIDER_FATAL.value
               for item in items for error in item.errors):
            _log.error("generation_downstream_contract: isolated provider fatal")
            raise InternalError("generation_downstream_contract: isolated provider fatal")

    # ── v1.11 预算接线（spec 3.7.2 v1.11 行）──────────────────────────────

    def _judge_panel(self) -> tuple[list[str], bool]:
        """评审面板取值。
        @return (judge profile 名列表, 是否多 judge)；judges 为空即单 judge = verify.llm
        """
        vcfg = self.cfg.verify
        return (list(vcfg.judges) or [vcfg.llm]), bool(vcfg.judges)

    def _panel_fit(self, ctx: "RunContext", record: Record,
                   schema: Mapping) -> _PromptFit | None:
        """面板级装填预算：一轮只建一份提示词并广播，故按 V25② 取 min-over-panel——预算 = 各声明
        预算 judge 的 (input_budget − 其结构化输出 Schema 估值) 的最小值，单图成本 = 其中最大的
        标定读数（共享构建下取保守值）；未声明预算的面板成员不施加约束。
        @param ctx 运行上下文（标定器读数来源）
        @param record 被评审记录（决定是否计图像成本）
        @param schema 本轮结构化输出 Schema
        @return 装填状态；None = 整面板预算关（与 v1.10 逐字节一致）
        """
        judges, _multi = self._judge_panel()
        declared: list["LLMProfile"] = []
        for judge in judges:
            prof = self.cfg.llm_profiles.get(judge)
            if prof is not None and prof.context_window > 0:
                declared.append(prof)
        if not declared:
            return None
        schema_est = budget.est_text(json.dumps(_thaw_json(schema), ensure_ascii=False))
        min_budget = min(
            budget.input_budget(p)
            - (schema_est if p.supports_structured_output else 0)
            for p in declared)
        cost = (max(ctx.llm.calibrator.cost(p.name) for p in declared)
                if record.modality == "ui" else 0)
        return _PromptFit(input_budget=min_budget, image_cost=cost)

    def _settle_fit(self, fit: _PromptFit | None, ctx: "RunContext") -> None:
        """结算装填：计裁剪次数；溢出即最小单元也放不下 → V10 记录级拒绝。
        @param fit 装填状态；None = 预算关，直接返回
        @param ctx 运行上下文（计数器）
        @raises ContextOverflowError phase="precheck"，归为 context_overflow；请求从不发出也不喂熔断
        """
        if fit is None:
            return
        if fit.truncations:
            ctx.metrics.count("budget.truncations.verify", fit.truncations)
        if fit.overflow:
            raise ContextOverflowError(
                "verify prompt exceeds the panel-min input budget at the "
                "minimal unit (single record)", phase="precheck")

    # ── classic 全批波次驱动 ─────────────────────────────────────────────

    async def _run_classic(self, items: list[PipelineItem],
                           ctx: "RunContext") -> None:
        """按 judge wave、输入序归并、repair wave 与 round 屏障推进 classic 记录。

        @param items 输入序待评审信封
        @param ctx 本次运行上下文
        """
        pending = [
            _ClassicReview(
                item=item,
                ordinal=ordinal,
                label=item.classification.label if item.classification else None,
                annotation=item.annotation,
            )
            for ordinal, item in enumerate(items)
        ]
        while pending:
            reviewed = await self._classic_review_wave(pending, ctx)
            repairs = self._reduce_classic_round(reviewed, ctx)
            pending = await self._classic_repair_wave(repairs, ctx)

    async def _classic_review_wave(
            self, pending: list[_ClassicReview],
            ctx: "RunContext") -> list[_ClassicReview]:
        """冻结本轮全部 item 与 judge 计划并经共享执行器运行。

        @param pending 本轮输入序台账
        @param ctx 本次运行上下文
        @return 成功完成 panel 归并的台账
        """
        plans = self._plan_classic_review(pending, ctx)
        if not plans:
            return []
        specs = self._classic_judge_specs(plans, ctx)
        outcomes = await ctx.tasks.run_group(TaskGroupRequest(specs))
        return self._reduce_classic_reviews(plans, outcomes, ctx)

    def _plan_classic_review(
            self, pending: list[_ClassicReview],
            ctx: "RunContext") -> list[_ClassicJudgePlan]:
        """按 item 输入序构建共享 panel 提示词与 Schema。

        @param pending 本轮输入序台账
        @param ctx 本次运行上下文
        @return 可执行的 item 评审计划
        """
        from labelkit.common.inference.schema_engine import VERDICT_SCHEMA

        judges, _multi = self._judge_panel()
        plans: list[_ClassicJudgePlan] = []
        for state in pending:
            try:
                fit = self._panel_fit(ctx, state.item.record, VERDICT_SCHEMA)
                prompt = build_verify_prompt(
                    state.item.record, state.annotation.output, ctx.cfg,
                    VerifyPromptOptions(
                        label=state.label, fit=fit,
                        verdict_form=state.item.record.kind == "sequence",
                    ),
                )
                self._settle_fit(fit, ctx)
            except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as exc:
                self._settle_classic_error(state, exc, ctx)
                continue
            plans.append(_ClassicJudgePlan(
                state=state, prompt=prompt, judges=tuple(judges),
                schema=VERDICT_SCHEMA,
            ))
        return plans

    def _classic_judge_specs(
            self, plans: list[_ClassicJudgePlan], ctx: "RunContext",
            ) -> tuple[TaskSpec[object], ...]:
        """把本轮 item 与 judge 笛卡尔积冻结为声明序任务。

        @param plans 输入序 item 评审计划
        @param ctx 本次运行上下文
        @return 扁平 TaskSpec tuple
        """
        specs: list[TaskSpec[object]] = []
        for plan in plans:
            for judge_ordinal, judge in enumerate(plan.judges):
                key = (ctx.batch_no, 8, plan.state.rounds, plan.state.ordinal,
                       judge_ordinal)
                specs.append(TaskSpec(
                    task_id=(f"{ctx.task_namespace}:verify:judge:"
                             f"{plan.state.rounds}:{plan.state.ordinal}:"
                             f"{judge_ordinal}"),
                    declaration_key=key,
                    stage=self.name,
                    resource_key=("llm", judge),
                    operation=lambda plan=plan, judge=judge: _judge_leaf(
                        plan, judge, ctx),
                ))
        return tuple(specs)

    def _reduce_classic_reviews(
            self, plans: list[_ClassicJudgePlan], outcomes: tuple[object, ...],
            ctx: "RunContext") -> list[_ClassicReview]:
        """按 item 与 judge ordinal 归并本轮 panel 结果。

        @param plans 输入序 item 计划
        @param outcomes TaskExecutor 输入序结果
        @param ctx 本次运行上下文
        @return 可进入轮次决策的台账
        """
        reviewed: list[_ClassicReview] = []
        offset = 0
        for plan in plans:
            selected = list(outcomes[offset:offset + len(plan.judges)])
            offset += len(plan.judges)
            if _ATTEMPT_MODE.get() and any(
                    isinstance(result, BaseException) for result in selected):
                error = next(result for result in selected
                             if isinstance(result, BaseException))
                self._settle_classic_error(plan.state, error, ctx)
                continue
            try:
                self._fold_classic_plan(plan, selected, ctx)
            except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as exc:
                self._settle_classic_error(plan.state, exc, ctx)
                continue
            reviewed.append(plan.state)
        return reviewed

    def _fold_classic_plan(self, plan: _ClassicJudgePlan,
                           selected: list[object], ctx: "RunContext") -> None:
        """归并一个 item 的 panel 结果并更新其私有台账。

        @param plan 当前 item 评审计划
        @param selected 当前 item 的 judge 输入序结果
        @param ctx 本次运行上下文
        """
        state = plan.state
        verdict, critiques, failed = self._fold_verdict_round(state, selected, ctx)
        state.rounds += 1
        state.verdict = verdict
        state.critiques.extend(critiques)
        state.fail_critiques = failed

    def _reduce_classic_round(
            self, reviewed: list[_ClassicReview],
            ctx: "RunContext") -> list[_ClassicReview]:
        """冻结本轮终局与 repair 集合，任何写入只按输入序发生。

        @param reviewed panel 已归并台账
        @param ctx 本次运行上下文
        @return 输入序 repair 台账
        """
        repairs: list[_ClassicReview] = []
        vcfg = self.cfg.verify
        for state in reviewed:
            repairable = (
                state.verdict == "fail"
                and vcfg.policy == "repair"
                and state.rounds - 1 < vcfg.max_repair_rounds
            )
            if repairable:
                repairs.append(state)
            else:
                self._finalize_classic(state)
        return repairs

    async def _classic_repair_wave(
            self, repairs: list[_ClassicReview],
            ctx: "RunContext") -> list[_ClassicReview]:
        """执行整轮 repair 叶任务并按 item 输入序提交新标注。

        @param repairs 本轮输入序修复台账
        @param ctx 本次运行上下文
        @return 成功修复、进入下一 judge round 的台账
        """
        if not repairs:
            return []
        specs = tuple(self._classic_repair_spec(state, ctx) for state in repairs)
        outcomes = await ctx.tasks.run_group(TaskGroupRequest(specs))
        pending: list[_ClassicReview] = []
        for state, outcome in zip(repairs, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                self._settle_classic_error(state, outcome, ctx)
                continue
            state.annotation = outcome
            pending.append(state)
        return pending

    def _classic_repair_spec(self, state: _ClassicReview,
                             ctx: "RunContext") -> TaskSpec[object]:
        """冻结一条 classic repair 任务。

        @param state 当前待修复台账
        @param ctx 本次运行上下文
        @return 纯修复叶任务
        """
        return TaskSpec(
            task_id=(f"{ctx.task_namespace}:verify:repair:"
                     f"{state.rounds}:{state.ordinal}"),
            declaration_key=(ctx.batch_no, 8, state.rounds, state.ordinal),
            stage=self.name,
            resource_key=("llm", self.cfg.annotate.llm),
            operation=lambda: self._reannotate_state(state, ctx),
        )

    async def _reannotate_state(self, state: _ClassicReview,
                                ctx: "RunContext") -> object:
        """执行不写共享业务对象的单调用 repair 叶。

        @param state 当前待修复台账
        @param ctx 本次运行上下文
        @return 新 Annotation 或 ordinary 记录级异常
        """
        try:
            return await self._reannotate_leaf(state, ctx)
        except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
            raise
        except ProviderFatalError as exc:
            if _ATTEMPT_MODE.get():
                raise
            return exc
        except InternalError as exc:
            if _ATTEMPT_MODE.get():
                raise
            return exc
        except Exception as exc:
            return exc

    def _settle_classic_error(self, state: _ClassicReview,
                              exc: BaseException, ctx: "RunContext") -> None:
        """在 reducer 按输入序提交一条 classic 记录失败。

        @param state 失败台账
        @param exc 记录级异常
        @param ctx 本次运行上下文
        """
        self._fail_item(state.item, exc, ctx)

    @staticmethod
    def _finalize_classic(state: _ClassicReview) -> None:
        """把台账终态提交到 PipelineItem。

        @param state 已完成全部轮次的台账
        """
        item = state.item
        item.annotation = state.annotation
        item.verification = VerificationResult(
            verdict=state.verdict,
            rounds=state.rounds,
            critiques=tuple(state.critiques),
        )
        if state.verdict == "fail":
            item.status = "dropped_verify"

    @staticmethod
    def _judge_error_entry(exc: BaseException, judge: str, multi: bool) -> dict:
        """单个 judge 崩溃（SchemaViolation / ProviderError 等）降级为一条 fail 意见，不丢失其他
        judge 的裁决（对标 quality.py 的同款处置）。
        @param exc 该 judge 的异常
        @param judge judge profile 名
        @param multi 是否多 judge 面板
        @return judge_error 意见条目
        @raises BaseException 大三样原样上抛；单 judge 面板亦原样上抛，交 _classify_error 归类
        """
        _log.error("verify judge failed: kind=%s", type(exc).__name__)
        if isinstance(exc, _BIG_THREE):
            raise exc
        if _ATTEMPT_MODE.get():
            raise exc
        if not multi:
            raise exc
        return {"aspect": "judge_error", "opinion": str(exc), "judge": judge}

    def _fold_verdict_round(
            self, state: _ClassicReview, results: list,
            ctx: "RunContext") -> tuple[str, list[dict], list[dict]]:
        """折叠 VERDICT_SCHEMA 形的一轮结果（经典路径，回归锚）。
        @param state 当前记录的跨轮台账
        @param results 与面板同序的结果列表
        @param ctx 运行上下文
        @return (面板结论, 合并意见, 判 fail 意见)
        """
        judges, multi = self._judge_panel()
        record = state.item.record
        round_no = state.rounds + 1
        merged: list[dict] = []
        fail_critiques: list[dict] = []
        verdicts: list[str] = []
        for judge, result in zip(judges, results):
            if isinstance(result, BaseException):
                entry = self._judge_error_entry(result, judge, multi)
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
            self._emit_verdict_event(
                _VerdictEvent(record=record, verdict=verdict, round_no=round_no,
                              critiques=obj["critiques"],
                              judge=judge if multi else None,
                              label=state.label), ctx)
        return majority_verdict(verdicts), merged, fail_critiques

    def _emit_verdict_event(self, event: _VerdictEvent, ctx: "RunContext") -> None:
        """发一条 ``verify.verdict`` trace 事件（载荷按 trace.content 档位递增）。§8.3：tier
        "none" 不带任何 LLM 产出的自由文本；缺陷表原样携带——脱敏是 M12 的职责（"defects" ∈
        obslog 自由文本键集，S27/S31），不是阶段的。
        @param event 事件载荷来源
        @param ctx 运行上下文
        """
        record = event.record
        content = ctx.cfg.trace.content
        payload: dict = {"verdict": event.verdict, "round": event.round_no}
        if content != "none":
            payload["critiques"] = [
                {"aspect": c["aspect"], "opinion": c["opinion"]}
                for c in event.critiques
            ]
        if event.judge is not None:
            payload["judge"] = event.judge
        if ctx.cfg.classify.enabled and event.label is not None:  # v1.7 R5
            payload["label"] = event.label
        if event.defects is not None:                             # v1.8 流式序列评审
            payload["defects"] = [dict(d) for d in event.defects]
        if ctx.cfg.trace.enabled and content in ("excerpt", "full"):
            # §7.4：四个 trace.content 档位逐档递增——"full" 含 "excerpt" 的一切，故两档都挂
            # 摘要（CONTRACTS.md §8.3）。以 trace.enabled 为门，关闭时永不计算 serialize()。
            payload["excerpt"] = {record.id: _excerpt_source(record)[:200]}
        ctx.metrics.event(
            EV_VERIFY_VERDICT,
            stage=self.name,
            batch_no=ctx.batch_no,
            record_ids=(record.id,),
            payload=payload,
        )

    # ── 回灌 M5 的修复钩子（获授权的跨算子导入，§7.4/§7.6）───────────────

    async def _reannotate_leaf(
            self, state: _ClassicReview, ctx: "RunContext") -> Annotation:
        """把判 fail 的意见回灌 M5 标注器重标注一次。
        @param state 当前记录的跨轮台账
        @param ctx 运行上下文
        @return 重标注结果
        """
        from labelkit.operators.annotate import (
            AnnotatePromptOptions,
            RepairContext,
            annotate_record_leaf,
        )

        repair = RepairContext(
            previous_output=state.annotation.output,
            critiques_text=render_critiques_text(state.fail_critiques),
        )
        return await annotate_record_leaf(
            state.item.record, ctx,
            AnnotatePromptOptions(
                repair=repair,
                label=state.label,
                temporal_context=state.item.temporal_context,
            ),
        )

    # ── classic 与 stream 共享的失败归并 ─────────────────────────────

    def _fail_item(self, item: PipelineItem, exc: BaseException,
                   ctx: "RunContext") -> str:
        """记录级失败落地：_verify_item except 分支与流式驱动器共用的对手件（阶段契约④：单条失败
        不得抛到批层面）。
        @param item 失败的信封
        @param exc 触发失败的异常
        @param ctx 运行上下文
        @return 归类得到的 StageError.kind（调用方据此写错误日志）
        """
        kind, retryable = _classify_error(exc, item.record.modality)
        _log.error("verify record failed: kind=%s", kind)
        if isinstance(exc, SchemaViolation):
            # M11 为 rejects 的 "full" 档读取的 duck 通道（§9.2）。
            item.raw_last_output = exc.raw_last_output  # type: ignore[attr-defined]
        if kind == ErrorKind.CONTEXT_OVERFLOW.value:
            # V13②：在拒绝处计数，覆盖所有 phase；reactive-400 终态恰喂一次熔断
            # （A7——duck 标记幂等，跨 M7→M5 修复链有效）。
            ctx.metrics.count("budget.overflow_records")
            _feed_reactive_terminal(exc, ctx.metrics)
        item.errors.append(StageError(stage=self.name, kind=kind,
                                      message=str(exc), retryable=retryable))
        item.status = "failed"
        ctx.metrics.event(
            EV_ERROR,
            stage=self.name,
            batch_no=ctx.batch_no,
            record_ids=(item.record.id,),
            payload={
                "stage": self.name,
                "kind": kind,
                "message": str(exc),
                "retryable": retryable,
            },
        )
        return kind

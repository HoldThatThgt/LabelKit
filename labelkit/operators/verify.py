"""M7 verify 阶段——对（记录, 标注）对做 LLM-as-a-Judge 评审（spec 3.7）。

按 CONTRACTS.md §7.6：每个带标注的 active 信封用 §10.5 模板对 ``VERDICT_SCHEMA``（§10.7）取判决；
可选多 judge 面板（奇数、多数表决、意见并入 ``judge`` 字段）。policy ``drop`` 判 fail 即丢；
``repair`` 把判 fail 的意见回灌 M5（``annotate_record`` + ``RepairContext``）重标注至多
``verify.max_repair_rounds`` 轮，仍不过则 ``dropped_verify``。每轮每 judge 发一条 ``verify.verdict``
trace 事件。

v1.8 流式分支（S7/S8/S31）：``segment.enabled`` 下的序列信封分流进阶段层驱动器——§10.5 序列变体评审
（缺陷词表 system + 六段 user 证据含 ``[边界余量]``；stitch 开启时 v1.9 T15 的 ``[片段结构]`` 插成
七段；对 ``defect_verdict_schema()`` 校验），并在 ``policy = "repair"`` 下按轮做两阶段批级成员手术：
并发评审 → 批位序同步缺陷路由（收缩 / 回收预定 / 仅标记）→ 并发回收复判（``judge_window`` 直调）→
并发接缝重抽（``extract_transition`` 直调）→ 同步重建记录与步表 → v1.12 帧产物同步（收缩删键 +
``classify_frames`` / ``annotate_member`` 回收补跑，SPEC-frame-annotation §3.4）→ 并发重标注 → 下一
轮复评。非流式路径是回归锚：``run_verify_loop``、``VERDICT_SCHEMA`` 用法与经典模板逐字节不变。

导入约定：所组合的服务与兄弟模块（llm_client、schema_engine、annotate，以及流式修复路径上获授权的
直调面 ``segment.judge_window`` / ``extract.extract_transition`` / ``classify.classify_frames`` /
``annotate.annotate_member``——CONTRACTS §1.1 算子间导入白名单第四向）一律在用到它们的函数内部懒
加载，故 import 本模块不要求那些文件已存在；名字与用法与 CONTRACTS.md 一致。
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import math
import re
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Mapping, Sequence

from labelkit.common.errors import (
    CircuitBreakerTripped,
    ContextOverflowError,
    ErrorKind,
    ProviderFatalError,
    ProviderRetryableError,
    SchemaViolation,
)
from labelkit.common.contracts.types import (
    Annotation,
    PipelineItem,
    Record,
    StageError,
    Transition,
    VerificationResult,
    frame_digest,
)
from labelkit.common.runtime import budget

if TYPE_CHECKING:
    from labelkit.common.config.model import LLMProfile, ResolvedConfig
    from labelkit.common.runtime.llm_client import PromptBundle
    from labelkit.common.contracts.stage import RunContext
    from labelkit.operators.annotate import AnnotatePromptOptions, RepairContext

_log = logging.getLogger("labelkit.verify")

EV_VERIFY_VERDICT = "verify.verdict"
EV_ERROR = "error"

# §10.5 评审提示词，固定中文文本（spec 3.7.2，逐字冻结）。
_SYSTEM_HEAD = "你是标注质量审核员。给定任务指令、原始数据与标注结果，独立判断标注是否合格。"
_SYSTEM_DIMS = "评审维度: ① 是否遵循任务指令 ② 与原始数据的事实一致性 ③ 字段语义是否正确填写"
_SYSTEM_TAIL = "先逐维度给出简短意见，再给结论。"

# §10.5 v1.8 序列变体，固定中文文本（CONTRACTS §10.5 处冻结）。
_SEQ_SYSTEM_HEAD = ("你是标注质量审核员。给定任务指令、动作序列、边界余量与首末帧截图，独立判断该序列\n"
                    "（episode）的标注是否合格。")
_SEQ_SYSTEM_DIMS = ("评审维度: ① 是否遵循任务指令 ② 与动作序列及首末帧证据的事实一致性 ③ 字段语义是否正确填写\n"
                    "④ 段边界与成员构成是否成立（对照下列缺陷类型）")
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

# 序列证据的段标签 + §10.1 冻结步行格式。算子之间不互相依赖（spec §2.2）：M5/M4 各自
# 持有同格式行模板的副本，这份是 M7 的副本。
_LABEL_ACTION_SEQUENCE = "[动作序列]"
_LABEL_FRAGMENT_STRUCTURE = "[片段结构]"   # v1.9（T15）：第七段
_LABEL_BOUNDARY_MARGIN = "[边界余量]"
_LABEL_FIRST_FRAME = "[首帧截图]"
_LABEL_LAST_FRAME = "[末帧截图]"
_MEMBER_DIGEST_MAX_CHARS = 400   # 序列 excerpt 档摘要上限（镜像 M4 §7.3）

# §10.16 v1.13 判决形序列变体（裁决·直装评审判决形，实现即冻结面）：经典路径遇
# kind=="sequence"（segment 关闭——直装序列信封）时的评审模板——判决指令 system
# 文本（非缺陷词表，defects 键被 VERDICT_SCHEMA 禁止）+ [任务指令]/[成员帧摘要]/
# [标注结果] 三段 user 证据；无缺陷表/边界余量/片段结构。
_VERDICT_SEQ_SYSTEM_HEAD = ("你是标注质量审核员。给定任务指令、成员帧摘要与标注结果，独立判断该序列"
                            "（episode）的标注是否合格。")
_VERDICT_SEQ_SYSTEM_DIMS = ("评审维度: ① 是否遵循任务指令 ② 与成员帧摘要证据的事实一致性 "
                            "③ 字段语义是否正确填写")
_VERDICT_SEQ_SYSTEM_STRUCTURE = (
    "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
    '{"critiques": [{"aspect": <维度>, "opinion": <一句话意见>}, ...],\n'
    ' "verdict": "pass"|"fail"}')
_LABEL_MEMBER_DIGESTS = "[成员帧摘要]"

# 缺陷种类闭集，按 schema/enum 序——S31 确定性去重排序键的第一分量。
# v1.9（T15）：追加 wrong_stitch（六种）。
DEFECT_KINDS = ("label_mismatch", "off_task_members", "missing_head",
                "missing_tail", "missing_members", "wrong_stitch")
_MISSING_KINDS = frozenset({"missing_head", "missing_tail", "missing_members"})
# 接纳回收候选帧重入段内的 judge_window 关系值
# （spec 3.7.3 流式修复路由 / §7.14 演绎映射：非边界值）。
_RECLAIM_RELATIONS = frozenset({"continues", "advances"})
# S7：判 fail 但 defects 为空数组时，代码侧规范化为一条默认 label_mismatch
# （修复路由建立在缺陷表之上）。
_DEFAULT_FAIL_DEFECT: Mapping = {
    "kind": "label_mismatch", "members": None, "position": None,
    "detail": "评审判 fail 但未指认缺陷，默认视同标签不符",
}
# 每侧段边界外 k = 2 帧进 [边界余量] 证据段
# （spec 3.7.2——VAD hangover 惯例移植，零额外 LLM 调用）。
_BOUNDARY_MARGIN_K = 2

# M7 自持的流式计数器（CONTRACTS §9.3，S31 → report.stream.verify）。
_COUNTER_MEMBERSHIP_REPAIRS = "verify.membership_repairs"
_COUNTER_BOUNDARY_FLAGS = "verify.boundary_flags"
_COUNTER_DEFECTS_PREFIX = "verify.defects."


# ── v1.11 上下文预算装填（spec 3.7.2「上下文预算装填」行，V25②③）──────────────

_TREE_MARKER_RE = re.compile(r"^…\(truncated (\d+) nodes\)$")


@dataclasses.dataclass
class _PromptFit:
    """一次评审提示词的记录侧装填状态（spec 3.7.2 v1.11 行）：每轮只建一份提示词广播给整个面板，
    故取面板最小输入预算（V25②）与面板内最大单图标定成本（保守广播）。唯一可裁槽位是
    ``[动作序列]`` 步表块（序列，§3.3⑤）或控件树渲染（单记录，§3.3③）；``[标注结果]``、边界
    余量、片段结构恒计不裁（V25③）。
    """
    input_budget: int       # 面板最小可用输入预算（token）
    image_cost: int         # 单图成本（面板内最大标定读数；text 模态为 0）
    truncations: int = 0    # 本次装填实际发生的裁剪次数（进 budget.truncations.verify）
    overflow: bool = False  # 最小单元仍越预算（V10——调用方拒绝，请求从不发出）


@dataclasses.dataclass(frozen=True)
class VerifyPromptOptions:
    """``build_verify_prompt`` 的可选装配项（v1.7/v1.8/v1.9/v1.11/v1.13 增量的收拢面）；缺省实例
    即 v1.7 之前的单记录经典调用形态：无类标签、无序列证据、无预算装填、非判决形。
    """
    label: str | None = None                            # 类标签；None = 取全局指令与准则（v1.7 R3）
    transitions: tuple[Transition, ...] | None = None   # 序列步表；None = 整段省略 [动作序列]（v1.8 S7）
    boundary_margin: str = ""                           # [边界余量] 段正文（驱动器预渲染，持批上下文）
    fragment_structure: str = ""                        # [片段结构] 段正文；空串 = 整段省略（v1.9 T15）
    fit: "_PromptFit | None" = None                     # 面板最小预算装填状态；None = 预算关（v1.11）
    verdict_form: bool = False                          # 序列走 §10.16 判决形变体（v1.13 直装序列）


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
    """A7/§7.8 熔断矩阵：只有 reactive-400（body-sniff）溢出终态喂熔断连击，且每个异常对象恰喂
    一次（duck 标记挡住同一异常跨算子重复喂食，如 M7→M5 修复链）；precheck 与 200 形态的 finish
    判据永不喂。``origin`` 防御性读取（默认 "http_400"）。
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


# ── 纯提示词文本装配（可单测，无服务导入）────────────────────────────────────

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
    """判决形序列变体（§10.16，v1.13 裁决·直装评审判决形）的 system 段：三维评审 + 结论指令 +
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
    """[成员帧摘要] 行——逐成员 ``{m}. {frame_digest(member, 400)}``（m 1 基，成员序）。总量受
    max_total_chars 约束：首末行恒保留，中段整行丢弃并以 ``…(truncated N members)`` 收口（镜像 M5
    渲染——算子间不互导，M7 自持副本，annotate._member_digest_lines 同式）。
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
    """一行 [动作序列] 步行，§10.1 冻结格式
    ``{index}. {action_type}（对象: {target|—}；值: {value|—}）{description}``（null 渲染成 "—"）。
    评审证据不带（摘取兜底）后缀——S16 标记只属于 M4 评分段（M5 规则，此处镜像）。v1.9（T14，对
    无后缀规则的刻意修订）：线索接缝占位步（detail.kind == "thread_seam"）确实带「（线索接缝：
    被 X 打断）」后缀——没有它评审者会把机械占位读成无解释跳变而误报缺陷。
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
    """S31 确定性规范化（多 judge 并集的）缺陷表：按 (kind 枚举序, position, members) 排序去重。
    ``sorted`` 稳定，故同键条目由并集顺序（judge 配置序，再条目序）决定幸存者——与调度无关；
    条目做浅拷贝（后续路由标注的是副本，绝不写 judge 的原始载荷）。
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
    """本会话的帧信封，按批位序（= 会话序，M10 整会话装批；S4：邻域查询 = session_id 过滤 +
    批列表位置序）。
    @param batch 本批信封列表
    @param session_id 会话 id
    @return 该会话的单记录信封列表
    """
    return [it for it in batch
            if it.record.kind == "single" and it.session_id == session_id]


def _session_episodes(batch: Sequence[PipelineItem],
                      session_id: str | None) -> list[PipelineItem]:
    """本会话的 episode 信封，按批位序、按 record id 去重（扇出克隆共享 id 并折叠到原信封，故段
    序号在扇出下仍稳定）。v1.9（T15，major-5）：stitched 壳被排除——壳的陈旧成员集会污染
    「第 n 段」序号与余量去向。
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
    """[片段结构] 段正文（v1.9 T15——§10.5 序列模板的第七段）：每碎片一行（线索内序号、成员下标区
    间、成员数、首帧摘要），末行接缝位置表。读 M16 duck 标记；stitch 未触碰的线索（或手术前的不
    匹配）降级为单个隐含碎片。纯代码读信封状态——零 LLM 调用、零随机。
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
    """判决形序列 prompt 装配（§10.16，v1.13 裁决·直装评审判决形）：user 段 = [任务指令] →
    [成员帧摘要]（400 字/成员、ui_tree_max_chars 总量中段丢弃——镜像 M5 渲染）→ [标注结果]；无
    缺陷表/边界余量/片段结构，无截图段（直装序列 text 模态）。``fit`` 非 None 时成员摘要块是唯一
    可裁槽位（§3.3⑤ edges 裁剪），[标注结果]/指令恒计不裁（V25③）；不可裁地板超预算 ⇒
    fit.overflow（V10——调用方拒绝，请求从不发出）。
    @param record kind == "sequence" 的直装序列记录
    @param output 待评审的标注对象
    @param cfg 已解析配置（ui_tree_max_chars 总量取值）
    @param texts (类有效任务指令, 类有效 extra_criteria)
    @param fit v1.11 面板最小预算装填状态；None = 预算关
    @return 两条消息的提示词包
    """
    from labelkit.common.runtime.llm_client import Message, Part, PromptBundle

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
    from labelkit.common.runtime.llm_client import Part

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
    from labelkit.common.runtime.llm_client import Message, Part, PromptBundle

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
    from labelkit.common.runtime.llm_client import Message, Part, PromptBundle

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
    序列 ∧ ``options.verdict_form`` → §10.16 判决形变体（v1.13，调用方按「流式驱动器是否在场」选形：
    经典路径遇直装序列传 True，驱动器从不传，故缺陷词表变体逐字节不变）；序列 → §10.5 v1.8 缺陷
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
    __slots__ = ("item", "label", "rounds", "critiques", "verdict", "fail_critiques",
                 "defects",
                 # 轮内手术字段（begin_round 重置）：手术前成员元组（重建时比对相邻对）、
                 # 成员工作副本、帧 id → 会话内批位序、需重标注标志（label_mismatch）、
                 # 本轮发生过成员手术标志、本轮预定的回收候选、重建步下标 → 重抽 Transition。
                 "orig_members", "working_members", "session_positions",
                 "needs_reannotate", "surgical", "claims", "reseams")

    def __init__(self, item: PipelineItem):
        """建立一个 episode 的评审台账。
        @param item 待评审的序列信封
        """
        self.item = item
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


class VerifyStage:
    """M7 评审阶段：经典逐条路径 + v1.8 流式序列驱动器（spec 3.7）。"""

    name = "verify"

    def __init__(self, cfg: "ResolvedConfig"):
        """构造阶段实例。
        @param cfg 已解析配置（判决策略、面板、帧粒度开关均从此读取）
        """
        self.cfg = cfg

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
        episodes = [it for it in eligible if it.record.kind == "sequence"]
        if episodes and self.cfg.segment.enabled:
            singles = [it for it in eligible if it.record.kind != "sequence"]
            if singles:
                await asyncio.gather(*(self._verify_item(item, ctx)
                                       for item in singles))
            await self._run_stream_driver(batch, episodes, ctx)
            return batch
        await asyncio.gather(*(self._verify_item(item, ctx) for item in eligible))
        return batch

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
        schema_est = budget.est_text(json.dumps(schema, ensure_ascii=False))
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

    # ── 经典逐条驱动 ──────────────────────────────────────────────────────

    async def _verify_item(self, item: PipelineItem, ctx: "RunContext") -> None:
        """经典路径：跑评审/修复循环并落地终态（判 fail ⇒ dropped_verify）。
        @param item 待评审信封（active 且已有标注）
        @param ctx 运行上下文
        @raises CircuitBreakerTripped/KeyboardInterrupt/CancelledError 原样上抛
        """
        vcfg = self.cfg.verify
        label = item.classification.label if item.classification else None  # v1.7 R3
        try:
            verdict, rounds, critiques, annotation = await run_verify_loop(
                item.annotation,
                judge_round=lambda ann, rnd: self._judge_round(
                    item.record, ann, rnd, ctx, label=label),
                reannotate=lambda ann, fc: self._reannotate(
                    item.record, ann, fc, ctx, label=label),
                policy=vcfg.policy,
                max_repair_rounds=vcfg.max_repair_rounds,
            )
        except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as exc:  # 单条失败不外逃（阶段契约④）
            kind = self._fail_item(item, exc, ctx)
            _log.error("verify review failed for one record: kind=%s", kind)
            return
        item.annotation = annotation  # 修复后的标注顶替原标注（§7.6）
        item.verification = VerificationResult(verdict=verdict, rounds=rounds,
                                               critiques=tuple(critiques))
        if verdict == "fail":
            item.status = "dropped_verify"

    # ── 一轮评审（全 judge，多数表决）───────────────────────────────────

    async def _judge_round(
        self, record: Record, annotation: Annotation, round_no: int, ctx: "RunContext",
        label: str | None = None,
    ) -> tuple[str, list[dict], list[dict]]:
        """经典路径的一轮评审：schema 恒 VERDICT_SCHEMA。v1.13（裁决·直装评审判决形）：遇序列
        信封（segment 关闭的直装形态——驱动器在场时序列永不进本函数）走判决形模板，schema 不变。
        @param record 被评审记录
        @param annotation 本轮送审的标注
        @param round_no 轮次（1 基）
        @param ctx 运行上下文
        @param label 类标签；None = 全局取值
        @return (面板结论, 合并意见, 判 fail 意见)
        """
        from labelkit.common.runtime.schema_engine import VERDICT_SCHEMA

        # v1.11：min-over-panel 装填那一份广播提示词；fit=None = 预算关，逐字节构建。
        fit = self._panel_fit(ctx, record, VERDICT_SCHEMA)
        prompt = build_verify_prompt(
            record, annotation.output, ctx.cfg,
            VerifyPromptOptions(label=label, fit=fit,
                                verdict_form=(record.kind == "sequence")))
        self._settle_fit(fit, ctx)
        results = await self._broadcast_to_judges(prompt, VERDICT_SCHEMA, record, ctx)
        return self._fold_verdict_round(record, results, round_no, ctx, label)

    async def _broadcast_to_judges(self, prompt: "PromptBundle", schema: Mapping,
                                   record: Record, ctx: "RunContext") -> list:
        """把同一份提示词并发广播给整个面板。
        @param prompt 本轮提示词包
        @param schema 结构化输出 Schema
        @param record 被评审记录（record_ids 归因）
        @param ctx 运行上下文
        @return 与面板同序的结果列表（异常以对象形式返回，不外逃）
        """
        from labelkit.common.runtime.schema_engine import CallScope

        judges, _multi = self._judge_panel()
        scope = CallScope(record_ids=(record.id,), batch_no=ctx.batch_no)
        return await asyncio.gather(
            *(ctx.schema_engine.complete_validated(judge, prompt, schema=schema,
                                                   scope=scope)
              for judge in judges),
            return_exceptions=True)

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
        if isinstance(exc, _BIG_THREE):
            raise exc
        if not multi:
            raise exc
        return {"aspect": "judge_error", "opinion": str(exc), "judge": judge}

    def _fold_verdict_round(self, record: Record, results: list, round_no: int,
                            ctx: "RunContext",
                            label: str | None) -> tuple[str, list[dict], list[dict]]:
        """折叠 VERDICT_SCHEMA 形的一轮结果（经典路径，回归锚）。
        @param record 被评审记录
        @param results 与面板同序的结果列表
        @param round_no 轮次（1 基）
        @param ctx 运行上下文
        @param label 类标签
        @return (面板结论, 合并意见, 判 fail 意见)
        """
        judges, multi = self._judge_panel()
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
                              judge=judge if multi else None, label=label), ctx)
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

    async def _reannotate(
        self, record: Record, annotation: Annotation, fail_critiques: list[dict],
        ctx: "RunContext", label: str | None = None,
    ) -> Annotation:
        """把判 fail 的意见回灌 M5 标注器重标注一次。
        @param record 被评审记录
        @param annotation 上一轮标注（进 RepairContext.previous_output）
        @param fail_critiques 判 fail 的意见
        @param ctx 运行上下文
        @param label 类标签
        @return 重标注结果
        """
        from labelkit.operators.annotate import (AnnotatePromptOptions,
                                                 RepairContext, annotate_record)

        repair = RepairContext(
            previous_output=annotation.output,
            critiques_text=render_critiques_text(fail_critiques),
        )
        return await annotate_record(
            record, ctx, AnnotatePromptOptions(repair=repair, label=label))

    # ── v1.8 流式驱动器：序列评审 + 两阶段批级修复（S7/S8）──────────────
    # 确定性契约（S8）：零随机；LLM 调用只发生在几处 gather 内——(a) 评审、(c) 回收复判、
    # (d) 接缝重抽、(e2) v1.12 帧产物补跑、(f) 重标注；一切状态写入都发生在它们之间的同步
    # 段，且按批位序进行（对标 classify 扇出先例）。

    async def _run_stream_driver(self, batch: list[PipelineItem],
                                 episodes: list[PipelineItem],
                                 ctx: "RunContext") -> None:
        """流式驱动器主循环：并发评审 → 同步路由手术 → 并发修复 → 下一轮复评。
        @param batch 本批信封列表（邻域与成员信封来源）
        @param episodes 本批待评审的序列信封（批位序）
        @param ctx 运行上下文
        """
        pending = [_EpisodeReview(item) for item in episodes]   # 批位序
        while pending:
            reviewed = await self._review_round(pending, batch, ctx)        # (a)
            finalize, routed = self._route_round(reviewed, batch, ctx)      # (b)
            await self._resolve_claims(routed, ctx)                         # (c)
            repairing: list[_EpisodeReview] = []
            for state in routed:
                if state.surgical or state.needs_reannotate:
                    repairing.append(state)
                else:
                    finalize.append(state)   # 无可修复项——fail 结论维持
            dead = await self._reseam_episodes(repairing, ctx)              # (d)
            for state in repairing:                                         # (e)
                if id(state) in dead or not state.surgical:
                    continue
                self._rebuild_episode(state)
            dead |= await self._sync_frame_products(repairing, dead, ctx)   # (e2)
            next_pending = await self._reannotate_round(                    # (f)
                [state for state in repairing if id(state) not in dead], ctx)
            for state in finalize:
                self._finalize_episode(state, ctx)
            pending = next_pending                                          # (g)

    async def _review_round(self, pending: list[_EpisodeReview],
                            batch: list[PipelineItem],
                            ctx: "RunContext") -> list[_EpisodeReview]:
        """(a) 并发评审全部待评 episode，并在评审时记缺陷直方图（D4：报告要呈现 judge 指认过的
        每条缺陷含后续轮修掉的，与 membership_repairs/boundary_flags 的路由时语义一致）。
        @param pending 本轮待评审台账
        @param batch 本批信封列表
        @param ctx 运行上下文
        @return 评审成功的台账（失败者已落 failed，不再参与本轮后续阶段）
        @raises BaseException 大三样原样上抛
        """
        results = await asyncio.gather(
            *(self._review_episode(state, batch, ctx) for state in pending),
            return_exceptions=True)
        reviewed: list[_EpisodeReview] = []
        for state, result in zip(pending, results):
            if isinstance(result, BaseException):
                if isinstance(result, _BIG_THREE):
                    raise result
                self._fail_item(state.item, result, ctx)
                continue
            verdict, merged, fail_critiques, defects = result
            state.rounds += 1
            state.critiques.extend(merged)
            state.verdict = verdict
            state.fail_critiques = fail_critiques
            state.defects = defects
            for defect in defects:
                ctx.metrics.count(f"{_COUNTER_DEFECTS_PREFIX}{defect['kind']}")
            reviewed.append(state)
        return reviewed

    def _route_round(self, reviewed: list[_EpisodeReview], batch: list[PipelineItem],
                     ctx: "RunContext") -> tuple[list[_EpisodeReview],
                                                 list[_EpisodeReview]]:
        """(b) 同步缺陷路由与成员手术，严格按批位序（"先到"被定死成"位序先到"，S8）。
        @param reviewed 评审成功的台账
        @param batch 本批信封列表
        @param ctx 运行上下文
        @return (进入终审的台账, 已路由待修复的台账)
        """
        vcfg = self.cfg.verify
        finalize: list[_EpisodeReview] = []
        routed: list[_EpisodeReview] = []
        claimed: set[int] = set()      # 本轮已预定的噪声信封 id()
        for state in reviewed:
            state.begin_round()
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

    async def _resolve_claims(self, routed: list[_EpisodeReview],
                              ctx: "RunContext") -> None:
        """(c) 经 segment.judge_window 并发复判回收候选，并按预定序同步落地。复判失败降级为仅
        标记（记录级隔离）；这一吞是该异常的终态——回收窗口没有降级面（V24），故 reactive-400
        溢出的 A7「恰好一次」熔断喂食在此结清（duck 标记幂等；precheck 与 finish 判据永不喂）。
        @param routed 已路由待修复的台账
        @param ctx 运行上下文
        @raises BaseException 大三样原样上抛
        """
        claims = [(state, claim) for state in routed for claim in state.claims]
        if not claims:
            return
        outcomes = await asyncio.gather(
            *(self._rejudge_claim(claim, ctx) for _, claim in claims),
            return_exceptions=True)
        for (state, claim), outcome in zip(claims, outcomes):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, _BIG_THREE):
                    raise outcome
                _feed_reactive_terminal(outcome, ctx.metrics)
                ctx.metrics.count(_COUNTER_BOUNDARY_FLAGS)
                continue
            if outcome in _RECLAIM_RELATIONS:
                self._apply_reclaim(state, claim, ctx)
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
        outcomes = await asyncio.gather(
            *(self._reannotate_episode(state, ctx) for state in jobs),
            return_exceptions=True)
        for state, outcome in zip(jobs, outcomes):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, _BIG_THREE):
                    raise outcome
                self._fail_item(state.item, outcome, ctx)
                continue
            state.item.annotation = outcome
            next_pending.append(state)
        return next_pending

    async def _review_episode(self, state: _EpisodeReview,
                              batch: list[PipelineItem], ctx: "RunContext"):
        """单 episode 的一轮评审：先渲染批上下文证据段，再送判。
        @param state episode 台账
        @param batch 本批信封列表（边界余量取邻域）
        @param ctx 运行上下文
        @return (面板结论, 合并意见, 判 fail 意见, 规范化缺陷表)
        """
        margin = boundary_margin_text(state.item, batch,
                                      self.cfg.segment.digest_max_chars)
        structure = (fragment_structure_text(state.item,
                                             self.cfg.stitch.digest_max_chars)
                     if self.cfg.stitch.enabled else "")   # v1.9（T15/m-11）
        return await self._judge_round_sequence(state, ctx, margin, structure)

    async def _judge_round_sequence(
        self, state: _EpisodeReview, ctx: "RunContext", boundary_margin: str,
        fragment_structure: str,
    ) -> tuple[str, list[dict], list[dict], list[dict]]:
        """序列的一轮评审：_judge_round 骨架换成 defect_verdict_schema()；v1.11 同款
        min-over-panel 装填——序列变体里 [动作序列] 块是可裁槽位。
        @param state episode 台账（取信封、标注、轮次与类标签）
        @param ctx 运行上下文
        @param boundary_margin [边界余量] 段正文
        @param fragment_structure [片段结构] 段正文；空串 = 整段省略
        @return (面板结论, 合并意见, 判 fail 意见, 规范化缺陷表)
        """
        from labelkit.common.runtime.schema_engine import defect_verdict_schema

        item = state.item
        record = item.record
        schema = defect_verdict_schema()
        fit = self._panel_fit(ctx, record, schema)
        prompt = build_verify_prompt(
            record, item.annotation.output, ctx.cfg,
            VerifyPromptOptions(label=state.label, transitions=item.transitions,
                                boundary_margin=boundary_margin,
                                fragment_structure=fragment_structure, fit=fit))
        self._settle_fit(fit, ctx)
        results = await self._broadcast_to_judges(prompt, schema, record, ctx)
        return self._fold_defect_round(state, results, ctx)

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
        judges, multi = self._judge_panel()
        record = state.item.record
        round_no = state.rounds + 1
        merged: list[dict] = []
        fail_critiques: list[dict] = []
        verdicts: list[str] = []
        defects_union: list[Mapping] = []
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
                defects_union.extend(obj["defects"])
            self._emit_verdict_event(
                _VerdictEvent(record=record, verdict=verdict, round_no=round_no,
                              critiques=obj["critiques"],
                              judge=judge if multi else None, label=state.label,
                              defects=obj["defects"]), ctx)
        final = majority_verdict(verdicts)
        defects = normalize_defects(defects_union)
        if final == "fail" and not defects:
            defects = [dict(_DEFAULT_FAIL_DEFECT)]
        return final, merged, fail_critiques, defects

    # ── (b) 缺陷路由（同步；收缩就地执行，回收只作为预定留给 (c) 的 gather）──

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
        positions: dict[str, int] = {}
        for i, frame in enumerate(frames):
            positions.setdefault(frame.record.id, i)
        state.session_positions = positions
        # S8：多标签扇出的克隆兄弟（classification.label 不是命中集首项）永不执行成员手术
        # ——共享的成员帧属于原信封。
        classification = item.classification
        scope = _RoutingScope(
            frames=frames, claimed=claimed,
            clone=bool(classification is not None and classification.labels
                       and classification.label != classification.labels[0]),
            split=bool(getattr(item, "session_split", False)))
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
            self._shrink_off_task(state, defect, scope, ctx)
            return
        # missing_head / missing_tail / missing_members——三级回收判定（噪声池 → 邻段 → 无处可寻）。
        if scope.split:
            # 会话在 batch_size 处被硬切（S21）：缺失帧可能落在别的批——回收降级为仅标记。
            state.defects[idx] = {**defect, "suspected": "session_split"}
            ctx.metrics.count(_COUNTER_BOUNDARY_FLAGS)
            return
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
                         scope: _RoutingScope, ctx: "RunContext") -> None:
        """off_task_members 收缩：把被指认的成员帧移出段并翻成 dropped_noise（②b M7 豁免）；无可
        指认对象、或 judge 点名了每个成员（空 episode 不可存在，改由 fail 结论整段丢弃）时不动手。
        @param state episode 台账
        @param defect 缺陷条目
        @param scope 批级路由作用域
        @param ctx 运行上下文
        """
        named = set(defect.get("members") or ())
        shrink_ids = {m.id for m in state.working_members if m.id in named}
        if not shrink_ids or len(shrink_ids) == len(state.working_members):
            return
        state.working_members = [m for m in state.working_members
                                 if m.id not in shrink_ids]
        for frame in scope.frames:
            if frame.status == "absorbed" and frame.record.id in shrink_ids:
                frame.status = "dropped_noise"
                frame.noise_attribution = ("verify", "off_task_member")  # type: ignore[attr-defined]
        state.surgical = True
        ctx.metrics.count(_COUNTER_MEMBERSHIP_REPAIRS)

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
        positions = state.session_positions
        member_positions = sorted(positions[m.id] for m in state.working_members
                                  if m.id in positions)
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
        if not 0 <= position < len(frames):
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
            frame = scope.frames[pos]
            if not _qualifies_for_reclaim(frame, scope.claimed):
                if id(frame) in scope.claimed:
                    contended = True       # 本轮被更早的 episode 预定走了
                continue
            if named and frame.record.id not in named:
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
        positions = state.session_positions
        prev_member = next_member = None
        for member in state.working_members:
            member_pos = positions.get(member.id)
            if member_pos is None:
                continue
            if member_pos < position:
                prev_member = member                 # 位序在下方的最后一个胜出
            elif member_pos > position and next_member is None:
                next_member = member
        window: list[Record] = []
        if prev_member is not None:
            window.append(prev_member)
        candidate_index = len(window)
        window.append(frame.record)
        if next_member is not None:
            window.append(next_member)
        return _ReclaimClaim(frame, position, window, candidate_index)

    async def _rejudge_claim(self, claim: _ReclaimClaim, ctx: "RunContext") -> str:
        """直调 M14 的公开复判面（CONTRACTS §7.14 获授权的导入例外）。
        @param claim 回收预定
        @param ctx 运行上下文
        @return 候选帧的关系判定值
        """
        from labelkit.operators.segment import judge_window

        relations = await judge_window(claim.window, ctx)
        return relations[claim.candidate_index]

    def _apply_reclaim(self, state: _EpisodeReview, claim: _ReclaimClaim,
                       ctx: "RunContext") -> None:
        """回收通过：噪声信封翻回 absorbed（②b M7 豁免——绝不翻回 active），记录按批位序插回。
        @param state episode 台账
        @param claim 回收预定
        @param ctx 运行上下文
        """
        claim.envelope.status = "absorbed"
        positions = state.session_positions
        insert_at = 0
        for i, member in enumerate(state.working_members):
            if positions.get(member.id, -1) < claim.position:
                insert_at = i + 1
        state.working_members.insert(insert_at, claim.envelope.record)
        state.surgical = True
        ctx.metrics.count(_COUNTER_MEMBERSHIP_REPAIRS)

    # ── (d)/(e) 接缝重抽 + 重建 ───────────────────────────────────────────

    def _affected_pairs(self,
                        state: _EpisodeReview) -> list[tuple[int, Record, Record]]:
        """手术前成员表里不存在的重建相邻对——需重抽接缝的触点（每次手术 1–2 处）。
        @param state episode 台账
        @return [(重建后的步序号, 左成员, 右成员)]
        """
        old_adjacent = {(a.id, b.id)
                        for a, b in zip(state.orig_members, state.orig_members[1:])}
        return [(j, a, b)
                for j, (a, b) in enumerate(zip(state.working_members,
                                               state.working_members[1:]))
                if (a.id, b.id) not in old_adjacent]

    async def _reseam_episodes(self, repairing: list[_EpisodeReview],
                               ctx: "RunContext") -> set[int]:
        """经 M15 公开直调面并发重抽接缝（CONTRACTS §7.15；仅 extract.enabled）。
        @param repairing 本轮待修复的台账
        @param ctx 运行上下文
        @return 因重抽出错而阵亡的台账 id() 集合（记录级隔离）
        @raises BaseException 大三样原样上抛
        """
        jobs: list[tuple[_EpisodeReview, int, Record, Record]] = []
        for state in repairing:
            if not state.surgical:
                continue
            if not (self.cfg.extract.enabled and state.item.transitions is not None):
                continue
            for j, a, b in self._affected_pairs(state):
                jobs.append((state, j, a, b))
        dead: set[int] = set()
        if not jobs:
            return dead
        from labelkit.operators.extract import extract_transition

        outcomes = await asyncio.gather(
            *(extract_transition(a, b, j, ctx, label=state.label)
              for state, j, a, b in jobs),
            return_exceptions=True)
        for (state, j, _a, _b), outcome in zip(jobs, outcomes):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, _BIG_THREE):
                    raise outcome
                if id(state) not in dead:
                    dead.add(id(state))
                    self._fail_item(state.item, outcome, ctx)
                continue
            state.reseams[j] = outcome
        return dead

    def _rebuild_episode(self, state: _EpisodeReview) -> None:
        """全部收缩/回收完成后的同步重建：新成员元组 + 全量重编号的步表。序列 id 永不重算（spec
        3.14.4）；未触碰的步保留原 Transition 只改写 index，手术触点换成新抽取结果并把
        {"reseamed": True} 并入 detail。不变式：len(transitions) == len(members) − 1。
        @param state episode 台账
        """
        item = state.item
        new_members = tuple(state.working_members)
        item.record = dataclasses.replace(item.record, members=new_members)
        if item.transitions is not None:
            old_by_pair = {
                (a.id, b.id): t
                for (a, b), t in zip(zip(state.orig_members, state.orig_members[1:]),
                                     item.transitions)
            }
            rebuilt: list[Transition] = []
            for j, (a, b) in enumerate(zip(new_members, new_members[1:])):
                if j in state.reseams:
                    fresh = state.reseams[j]
                    rebuilt.append(dataclasses.replace(
                        fresh, index=j,
                        detail={**dict(fresh.detail), "reseamed": True}))
                else:
                    rebuilt.append(dataclasses.replace(old_by_pair[(a.id, b.id)],
                                                       index=j))
            item.transitions = tuple(rebuilt)
        item.stream_repaired = True  # type: ignore[attr-defined]  # → _meta.stream.repaired

    # ── (e2) v1.12 帧产物同步（SPEC-frame-annotation §3.4 手术同步）──────────

    async def _sync_frame_products(self, repairing: list[_EpisodeReview],
                                   dead: set[int], ctx: "RunContext") -> set[int]:
        """帧产物同步：先收缩删键（同步、批位序），再回收补跑（帧分类先行、帧标注后随）。克隆信封被
        既有 S8 判据挡在手术之外（_route_defects 对克隆永不置 surgical），故帧产物同步天然只发生在
        首标签信封上，无克隆分支——克隆按引用共享同一 dict，随之生效。
        @param repairing 本轮待修复的台账
        @param dead 先前阶段已阵亡的台账 id()
        @param ctx 运行上下文
        @return 本阶段新阵亡的台账 id() 集合
        """
        synced = [state for state in repairing
                  if id(state) not in dead and state.surgical]
        for state in synced:
            self._shrink_frame_products(state.item)
        newly_dead = await self._backfill_frame_classify(synced, ctx)
        newly_dead |= await self._backfill_frame_annotate(
            [state for state in synced if id(state) not in newly_dead], ctx)
        return newly_dead

    @staticmethod
    def _shrink_frame_products(item: PipelineItem) -> None:
        """收缩同步：成员手术后不再属于 record.members 的成员 id 从两个帧产物 dict 中删键（含值为
        None 的 failed 占位键，不留无主条目）。仅当对应 dict 非 None 时操作（dict None = 帧 pass
        未运行：降格会话/帧粒度关闭/非首标签，语义必须保持）；dict 对象本身从不更换——扇出克隆
        按引用共享同一 dict 的前提。
        @param item 手术后的序列信封
        """
        kept = {member.id for member in item.record.members}
        for products in (item.member_classifications, item.member_annotations):
            if products is None:
                continue
            for member_id in [k for k in products if k not in kept]:
                del products[member_id]

    async def _backfill_frame_classify(self, states: list[_EpisodeReview],
                                       ctx: "RunContext") -> set[int]:
        """回收补跑·帧分类：新入 record.members 且缺键的成员经 classify_frames 单元素补分类，只补缺
        位（幂等），门 = frame_classify.enabled ∧ dict 非 None。窗口失败在 classify_frames 内落
        fallback_class（§3.2 语义，契约上不抛记录级异常）；gather + dead 集合形态镜像
        _reseam_episodes（记录级隔离，兜底大三样之外的意外逃逸）。
        @param states 已完成收缩删键的台账
        @param ctx 运行上下文
        @return 本步新阵亡的台账 id() 集合
        @raises BaseException 大三样原样上抛
        """
        jobs: list[tuple[_EpisodeReview, Record]] = []
        if self.cfg.frame_classify.enabled:
            for state in states:
                classifications = state.item.member_classifications
                if classifications is None:
                    continue
                jobs.extend((state, member)
                            for member in state.item.record.members
                            if member.id not in classifications)
        dead: set[int] = set()
        if not jobs:
            return dead
        # 懒加载 M13 公开直调面（CONTRACTS §1.1 算子间导入白名单第四向）。
        from labelkit.operators.classify import classify_frames

        outcomes = await asyncio.gather(
            *(classify_frames([member], ctx) for _, member in jobs),
            return_exceptions=True)
        for (state, member), outcome in zip(jobs, outcomes):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, _BIG_THREE):
                    raise outcome
                if id(state) not in dead:
                    dead.add(id(state))
                    self._fail_item(state.item, outcome, ctx)
                continue
            state.item.member_classifications[member.id] = outcome[member.id]
        return dead

    async def _backfill_frame_annotate(self, states: list[_EpisodeReview],
                                       ctx: "RunContext") -> set[int]:
        """回收补跑·帧标注：member_annotations 缺键的成员按帧类走 annotate_member。门 =
        frame_annotate.enabled ∧ dict 非 None，只补缺位（幂等）；必须在帧分类补跑落键之后运行
        （帧类取新鲜判决）。annotate_member 不可修复返回 None ⇒ 占键 None（failed 语义）；
        gather + dead 集合形态镜像 _reseam_episodes（annotate_member 契约上不抛，兜底同上）。
        @param states 已完成帧分类补跑的台账
        @param ctx 运行上下文
        @return 本步新阵亡的台账 id() 集合
        @raises BaseException 大三样原样上抛
        """
        jobs: list[tuple[_EpisodeReview, Record, str | None]] = []
        if self.cfg.frame_annotate.enabled:
            for state in states:
                jobs.extend(self._frame_annotate_jobs(state, ctx))
        dead: set[int] = set()
        if not jobs:
            return dead
        # 懒加载 M5 修复面族成员（CONTRACTS §1.1 算子间导入白名单第四向）。
        from labelkit.operators.annotate import annotate_member

        outcomes = await asyncio.gather(
            *(annotate_member(member, ctx, label=label)
              for _, member, label in jobs),
            return_exceptions=True)
        for (state, member, _label), outcome in zip(jobs, outcomes):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, _BIG_THREE):
                    raise outcome
                if id(state) not in dead:
                    dead.add(id(state))
                    self._fail_item(state.item, outcome, ctx)
                continue
            state.item.member_annotations[member.id] = outcome
        return dead

    def _frame_annotate_jobs(
        self, state: _EpisodeReview, ctx: "RunContext",
    ) -> list[tuple[_EpisodeReview, Record, str | None]]:
        """单 episode 的帧标注补跑工单：缺键成员 × 帧类视图门。label 与视图判定镜像 M5 帧 pass 的成
        员槽位规则（annotate._frame_member），含跳过类的 frame_annotate.skipped 计数（与 M5 供数点
        同口径，report 与 members[] 状态直方图可对账）；视图 enabled=false ⇒ 跳过类不占键（emitter
        按缺键推导 skipped），frame.classify 关 ⇒ label=None 走全局指令。
        @param state episode 台账
        @param ctx 运行上下文（跳过计数）
        @return [(台账, 成员记录, 帧类标签)]
        """
        item = state.item
        if item.member_annotations is None:
            return []
        jobs: list[tuple[_EpisodeReview, Record, str | None]] = []
        for member in item.record.members:
            if member.id in item.member_annotations:
                continue
            cls = (item.member_classifications or {}).get(member.id)
            label = cls.label if cls is not None else None
            view = (self.cfg.frame_class_views.get(label)
                    if label is not None else None)
            if view is not None and not view.enabled:
                ctx.metrics.count("frame_annotate.skipped")
                continue                     # 跳过类不占键（skipped 语义）
            jobs.append((state, member, label))
        return jobs

    # ── (f) 重标注 + 终审 ─────────────────────────────────────────────────

    def _rung_fits(self, trial: _LadderTrial, prof: "LLMProfile",
                   ctx: "RunContext") -> bool:
        """升档试装：按 (k 减半, 升档像素) 建一次提示词并估算，看是否仍在输入预算内。单图成本取
        max(标定读数, 供应商先验 @ 升档像素 × PRIOR_INFLATION)。v1.13：试装的 Schema 文本与计价
        对象都取类有效 Schema——否则试装估算与真实重标注调用不同源。
        @param trial 试装参数
        @param prof annotate profile（预算与像素上限来源）
        @param ctx 运行上下文（标定器与按类 Schema 查询）
        @return True = 升档站得住；False = 只保留 k 减半
        """
        from labelkit.operators.annotate import (AnnotatePromptOptions,
                                                 build_annotate_prompt,
                                                 class_effective_schema,
                                                 class_schema_text)

        item = trial.item
        prompt = build_annotate_prompt(
            item.record, ctx.cfg, class_schema_text(ctx, trial.label),
            AnnotatePromptOptions(
                repair=trial.repair, label=trial.label,
                transitions=item.transitions, fragment_lens=trial.fragment_lens,
                k_eff=trial.k_eff, image_px=trial.image_px))
        cost_up = max(ctx.llm.calibrator.cost(prof.name),
                      math.ceil(budget.est_image_prior(prof, trial.image_px)
                                * budget.PRIOR_INFLATION))
        schema_eff = (dict(class_effective_schema(ctx.cfg, trial.label))
                      if prof.supports_structured_output else None)
        est = budget.est_prompt(prompt, prof, schema_eff, image_cost=cost_up)
        return est <= budget.input_budget(prof)

    def _repair_ladder(self, item: PipelineItem, ctx: "RunContext",
                       opts: "AnnotatePromptOptions") -> "AnnotatePromptOptions":
        """V21 修复梯：为判 fail ∧ policy="repair" 的重标注算出换档取值（spec 3.7.3「修复路径与
        上下文预算的交互 ①」的唯一触发面，此处正是那条路径）。以 annotate profile 预算为门（cw ==
        0 保持 v1.10 调用形逐字节不变），只有 UI 序列带关键帧面。梯级：k → max(2,
        ⌈sequence_frames/2⌉)（F3）；px → 工作点上一级（见 _next_image_rung），经
        _rung_fits 复核，越预算则丢掉 px 档只保留 k 减半。budget.escalations 每次真正升档记一次；
        单发——由既有 max_repair_rounds 循环限界。
        @param item 待重标注的序列信封
        @param ctx 运行上下文
        @param opts 本次重标注的基准装配变体参数（修复上下文 / 类标签 / 碎片配额）
        @return 换档后的装配变体参数（预算关或非 UI 序列时原样返回 opts）
        """
        record = item.record
        acfg = self.cfg.annotate
        prof = self.cfg.llm_profiles.get(acfg.llm)
        if (prof is None or prof.context_window <= 0
                or record.kind != "sequence" or record.modality != "ui"):
            return opts
        k_half = max(2, math.ceil(acfg.sequence_frames / 2))
        px_up = _next_image_rung(prof)
        if px_up is not None and not self._rung_fits(
                _LadderTrial(item=item, repair=opts.repair, label=opts.label,
                             fragment_lens=opts.fragment_lens, k_eff=k_half,
                             image_px=px_up), prof, ctx):
            px_up = None
        if px_up is not None:
            ctx.metrics.count("budget.escalations")
        return dataclasses.replace(opts, k_eff=k_half, image_px=px_up)

    async def _reannotate_episode(self, state: _EpisodeReview,
                                  ctx: "RunContext") -> Annotation:
        """重标注一个手术过/判 label_mismatch 的 episode。v1.9（T14 穿参义务）：每碎片关键帧配额
        从 M16 duck 标记穿到两个 annotate 调用点——此处丢掉它会把修复重标注悄悄降级成均匀降采样。
        v1.11（V21/F3）：换档取值只在修复梯活着（预算开 + UI 序列）时改写基准变体参数，预算关
        时原样透传，调用形保持逐字节不变。
        @param state episode 台账
        @param ctx 运行上下文
        @return 重标注结果
        """
        from labelkit.operators.annotate import (AnnotatePromptOptions,
                                                 RepairContext, annotate_record)

        repair = RepairContext(
            previous_output=state.item.annotation.output,
            critiques_text=render_critiques_text(state.fail_critiques),
        )
        fragments = getattr(state.item, "stitch_fragments", None)
        fragment_lens = (tuple(int(f["member_count"]) for f in fragments)
                         if fragments else None)
        opts = AnnotatePromptOptions(repair=repair, label=state.label,
                                     transitions=state.item.transitions,
                                     fragment_lens=fragment_lens)
        return await annotate_record(state.item.record, ctx,
                                     self._repair_ladder(state.item, ctx, opts))

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

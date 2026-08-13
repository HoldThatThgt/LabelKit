"""M16 缝合阶段（spec 3.16、CONTRACTS.md §7.16）——v1.9 线索缝合。

保守地把同会话的碎片重新缝回线索：逐会话（批内位置序即会话序，M10 整会话装箱）把分段
产物——活跃的 episode 信封，加上由 ``below_min_len`` 丢帧的连续段重新成形出的救援候选
（T11）——按会话序走一条**单调**选择池。每个候选一次 LLM 判决（§10.11 提示词：开放线索
摘要卡按最近活跃降序 + 候选摘要卡，经 ``schema_engine.stitch_schema()`` 校验），由 T9 保守
合取把关：合并要求 LLM 判 ``resume`` **且**命中机械先验白名单（App 集合相交 / 摘要实体重叠
/ 回到同一页面）。合并把存活信封的 Record 重绑为成员并集（按会话序升序；record.id **绝不**
重算——沿用 M7 手术先例），并把被并走的 episode 信封标为 ``stitched``——即合同 ②c 的形态；
救援命中还会把其成员帧 dropped_noise → absorbed（②c③）。有界的二次重判（T19）把 pass 1
留下的单碎片线索对其余会话线索再判一次，且存活方向**反转**（候选变成空壳）。多碎片线索最后
获得 ``seam_indexes`` / ``seam_interrupted_by`` 鸭子标记（T20/M-1：一对拼接当且仅当其会话序
间隙里至少有 1 帧被**别的**线索吸收时才算接缝），以及 M11 渲染成 ``_meta.stream.fragments``
的 ``stitch_fragments`` 元数据。链位置：segment → stitch → dedup。失败策略
``stitch.on_error``："keep"（默认）让 episode 候选自开一条线索，证据对 = error 事件 +
``stitch.failures``（绝不写 item.errors，S26 形态）；"fail" **只**让 episode 候选信封失败
（kind=stitch_invalid；成员帧仍为 absorbed）；救援候选永不走 fail 路径——救援判决失败即漏缝
（B-2）。

``stitch.votes`` > 1（T18）按 profile 默认温度对同一判决采 n 个样本，并要求完整
(verdict, thread_ref) 对上的**严格**多数（M-4）；任何分裂都回退到保守结果
（episode → new，rescue → 漏缝）。

下面的 app/activity/title/entity 抽取循环是 M16 **自有的** ``types.frame_digest`` 内部逻辑
副本（T9 可行性裁定——沿用 extract._diff_text 先例：算子模块之间从不互相依赖，且渲染后的
摘要串从不被反向解析）。
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import TYPE_CHECKING, Mapping, Sequence

from labelkit.common.errors import (
    CircuitBreakerTripped,
    ContextOverflowError,
    ErrorKind,
    SchemaViolation,
)
from labelkit.common.contracts.types import (
    PipelineItem,
    Record,
    StageError,
    frame_digest,
    tree_diff,
)
from labelkit.common.runtime import budget

from labelkit.common.runtime.llm_client import Message, Part, PromptBundle
from labelkit.common.runtime.schema_engine import CallScope, stitch_schema

if TYPE_CHECKING:
    from labelkit.common.config.model import ResolvedConfig
    from labelkit.common.contracts.stage import RunContext

_logger = logging.getLogger("labelkit.stitch")

# 事件名（严格取 CONTRACTS.md §7.16 / §8.1 的字面串）
_EV_JUDGE = "stitch.judge"
_EV_THREAD = "stitch.thread"
_EV_ERROR = "error"

# M16 自有的计数器键（CONTRACTS.md §9.3 → report.stream.stitch；
# counts.stitched / counts.threads 由 M10 计量或推导）。
_COUNTER_JUDGMENTS = "stitch.judgments"            # pass-1 判决数（episode + rescue）
_COUNTER_REPASS_JUDGMENTS = "stitch.repass_judgments"
_COUNTER_RESCUED_SHORT = "stitch.rescued_short"    # 单位 = 被翻转的**帧**数（m-10）
_COUNTER_SEAMS = "stitch.seams"
_COUNTER_FAILURES = "stitch.failures"

# 机械先验白名单的三条腿名（T9，析取；同时是 trace 载荷词表）
_PRIOR_APP = "app_overlap"
_PRIOR_ENTITY = "entity_overlap"
_PRIOR_PAGE = "same_page"

# M16 自有的 frame_digest 抽取键副本（T9 裁定——见模块 docstring；types.py 保持不动）
_APP_KEYS = ("package", "package_name", "pkg")
_ACTIVITY_KEYS = ("activity", "activity_name", "window_title")

# 中文提示词片段——§10.11 缝合判决模板（随 CONTRACTS.md 落地即冻结；
# 组装约定镜像 segment.py 的 §10.9 构造器）。
_SYSTEM_HEAD = (
    "你是屏幕操作流的线索缝合审核员。下面给出当前会话中 {P} 条开放线索的摘要卡"
    "（按最近活跃降序排列）与一张候选碎片摘要卡。\n"
    "判断该候选碎片是恢复其中某条线索（用户切回了之前挂起的同一任务），还是开启一个新任务：\n"
    "- resume: 候选与某条线索是同一任务的延续——任务实体跨碎片延续（订单号、地点、商品、"
    "联系人等再次出现）、返回同一页面继续操作、或 App 与操作语境明确承接；给出该线索编号。\n"
    "- new: 候选是一个新任务。\n"
    "保守偏置：仅在证据明确指向同一任务时判 resume；证据不足、模糊或仅有表面相似"
    "（同 App 不同任务、同类页面不同对象）时一律判 new——错缝的代价高于漏缝。\n"
    "若当前无开放线索，恒判 new。\n"
    "task_name 用一句话概括任务：resume 时给出该线索合并候选后的任务名（滚动更新），"
    "new 时给出新任务名。"
)
_STRUCTURE_SENTENCE = "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容："
_STRUCTURE_SHAPE = ('{"verdict": "resume"|"new", "thread_ref": <线索编号|null>,\n'
                    ' "task_name": <一句话任务名>, "reason": <一句话理由>,\n'
                    ' "confidence": "high"|"medium"|"low"}')
_EMPTY_POOL_LINE = "（当前无开放线索）"
_THREAD_CARD_HEAD = "[线索 {i}] 任务名: {task_name}"
_CANDIDATE_CARD_HEAD = "[候选碎片] 类型: {kind}"
_CANDIDATE_KIND_EPISODE = "分段产出"
_CANDIDATE_KIND_RESCUE = "短段救援"
_SEAM_PAIR_LABEL = "接续对（线索尾帧 → 候选首帧）变更: "


# ── 纯证据抽取（M16 自有副本，T9）───────────────────────────────────────────

def record_app(record: Record) -> str | None:
    """取一帧的 App 值：DFS 序可见节点上 package/package_name/pkg 的首个非空值。

    与 frame_digest 的取首规则字节一致。

    @param record 帧记录
    @return App 值；无树或全空时为 None
    """
    if record.ui_tree is None:
        return None
    for node in record.ui_tree.nodes:
        if not node.visible:
            continue
        for key in _APP_KEYS:
            value = node.extra.get(key)
            if value:
                return value
    return None


def app_set(records: Sequence[Record]) -> frozenset[str]:
    """一段成员序列上的去重 App 集合（先验腿①）。

    纯文本模态的帧没有树 → 空集（该腿静默地永不命中）。

    @param records 成员帧记录序列
    @return 去重后的 App 值集合
    """
    return frozenset(app for app in (record_app(r) for r in records) if app)


def entity_set(record: Record) -> frozenset[str]:
    """一帧的显著实体片段——frame_digest 的显著规则（可见节点的非空 text/content_desc
    有序去重）取成**集合**，供先验腿②的重叠判断使用。

    纯文本模态 → 空集。

    @param record 帧记录
    @return 显著实体片段集合
    """
    if record.ui_tree is None:
        return frozenset()
    pieces: set[str] = set()
    for node in record.ui_tree.nodes:
        if not node.visible:
            continue
        for piece in (node.text, node.content_desc):
            if piece:
                pieces.add(piece)
    return frozenset(pieces)


def page_identity(record: Record) -> tuple[str, str, str | None] | None:
    """先验腿③的页面身份 = app + activity（+ DFS 首个可见标题）。

    要求 app 与 activity **同时**存在——采集侧的 dump 常常缺 activity（types.py 注："often
    absent"），此时该腿静默失效（可接受的析取降级，T9 数据依赖条款）。

    @param record 帧记录
    @return (app, activity, title) 三元组；证据不足时为 None
    """
    if record.ui_tree is None:
        return None
    app = activity = title = None
    for node in record.ui_tree.nodes:
        if not node.visible:
            continue
        if app is None:
            for key in _APP_KEYS:
                value = node.extra.get(key)
                if value:
                    app = value
                    break
        if activity is None:
            for key in _ACTIVITY_KEYS:
                value = node.extra.get(key)
                if value:
                    activity = value
                    break
        if title is None and node.text:
            title = node.text
    if app is None or activity is None:
        return None
    return (app, activity, title)


def prior_hits(thread_members: Sequence[Record],
               fragment_tails: Sequence[Record],
               candidate_members: Sequence[Record]) -> list[str]:
    """T9 机械先验白名单——线索与候选之间命中了哪几条析取腿。确定性、零 LLM：

    ① app_overlap：线索 App 集合 ∩ 候选 App 集合 ≠ ∅
    ② entity_overlap：线索**尾帧**实体 ∩ 候选**首帧**实体 ≠ ∅（E5 配对：挂起尾 × 恢复首）
    ③ same_page：候选首帧的页面身份等于**某个**碎片尾帧的页面身份（E6 线索引导的恢复；
       activity 缺席时该腿失效）

    @param thread_members 线索当前的全部成员帧，按会话序
    @param fragment_tails 线索各碎片的尾帧
    @param candidate_members 候选的成员帧，按会话序
    @return 命中的腿名列表，按 ①②③ 固定顺序
    """
    hits: list[str] = []
    if app_set(thread_members) & app_set(candidate_members):
        hits.append(_PRIOR_APP)
    if thread_members and candidate_members and (
            entity_set(thread_members[-1]) & entity_set(candidate_members[0])):
        hits.append(_PRIOR_ENTITY)
    cand_page = page_identity(candidate_members[0]) if candidate_members else None
    if cand_page is not None and any(
            page_identity(tail) == cand_page for tail in fragment_tails):
        hits.append(_PRIOR_PAGE)
    return hits


# ── 纯卡片 / 提示词组装（§10.11）───────────────────────────────────────────

def _diff_text(diff: Mapping) -> str:
    """把 tree_diff 映射固定文本化为摘要卡的「接续对」行。

    形态与 M14 的 §10.9 渲染一致；这是 M16 **自有的**副本（算子模块之间从不互相依赖，
    spec §2.2）。

    @param diff tree_diff 产出的映射
    @return 变更证据文本
    """
    text = (f"新增 {diff['added']} 节点，移除 {diff['removed']} 节点，"
            f"文本变化 {diff['text_changed']} 处，"
            f"变更比例 {diff['change_ratio']:.0%}")
    if diff["app_changed"]:
        text += "，应用切换"
    if diff["title_changed"]:
        text += "，标题变化"
    return text


@dataclasses.dataclass(frozen=True)
class ThreadCard:
    """一张开放线索摘要卡的线索侧取值（``render_thread_card`` 的入参形）。"""

    index: int                 # 卡片在池中的 1 基编号（即提示词里的线索编号）
    task_name: str             # 线索当前任务名；为空渲染占位符
    members: Sequence[Record]  # 线索当前的全部成员帧，按会话序
    span: tuple[int, int]      # 线索的会话序跨度 [首, 尾]
    fragment_count: int        # 线索当前的碎片数


def render_thread_card(card: ThreadCard, candidate_head: Record | None,
                       cfg: "ResolvedConfig") -> str:
    """渲染一张开放线索摘要卡（T8 证据面）。

    内容：App 集合、会话序跨度 + 帧数/碎片数、首尾帧摘要，以及——当调用方给了候选首帧时
    ——E5 恢复配对（线索尾帧 × 候选首帧）及其确定性 tree_diff 变更证据。帧摘要按
    stitch.digest_max_chars 截断（m-9）。

    @param card 线索侧取值（编号 / 任务名 / 成员帧 / 会话序跨度 / 碎片数）
    @param candidate_head 候选首帧；None 表示不渲染接续对行
    @param cfg 已解析的不可变配置
    @return 摘要卡文本
    """
    st = cfg.stitch
    members = card.members
    apps = "、".join(sorted(app_set(members))) or "（未知）"
    lines = [
        _THREAD_CARD_HEAD.format(i=card.index,
                                 task_name=card.task_name or "（未命名）"),
        f"App 集合: {apps}",
        f"序号跨度: [{card.span[0]}, {card.span[1]}]｜帧数 {len(members)}"
        f"｜碎片数 {card.fragment_count}",
        f"首帧摘要: {frame_digest(members[0], st.digest_max_chars)}",
        f"尾帧摘要: {frame_digest(members[-1], st.digest_max_chars)}",
    ]
    if candidate_head is not None:
        diff = tree_diff(members[-1].ui_tree, candidate_head.ui_tree,
                         cfg.dedup.bounds_quantize_px)
        lines.append(_SEAM_PAIR_LABEL + _diff_text(diff))
    return "\n".join(lines)


def render_candidate_card(kind: str, members: Sequence[Record],
                          span: tuple[int, int], cfg: "ResolvedConfig") -> str:
    """渲染候选碎片的摘要卡。

    @param kind 候选类型，∈ {"episode", "rescue"}，分别渲染为 分段产出 / 短段救援
    @param members 候选的成员帧，按会话序
    @param span 候选的会话序跨度 [首, 尾]
    @param cfg 已解析的不可变配置
    @return 候选摘要卡文本
    """
    st = cfg.stitch
    kind_text = (_CANDIDATE_KIND_RESCUE if kind == "rescue"
                 else _CANDIDATE_KIND_EPISODE)
    apps = "、".join(sorted(app_set(members))) or "（未知）"
    return "\n".join([
        _CANDIDATE_CARD_HEAD.format(kind=kind_text),
        f"App 集合: {apps}",
        f"序号跨度: [{span[0]}, {span[1]}]｜帧数 {len(members)}",
        f"首帧摘要: {frame_digest(members[0], st.digest_max_chars)}",
        f"末帧摘要: {frame_digest(members[-1], st.digest_max_chars)}",
    ])


def build_stitch_prompt(thread_cards: Sequence[str], candidate_card: str,
                        cfg: "ResolvedConfig") -> PromptBundle:
    """确定性组装 §10.11 模板。

    system：冻结的保守偏置指令（替入池内卡片数）、可选的 stitch.context 行（为空则省略）、
    结构句与结构形。user：**一条**消息——每张线索卡一个文本 part（调用方已按最近活跃降序
    排好，T8 位置偏置缓解；空池渲染固定的零卡行），候选卡作为最后一个文本 part。纯文本：
    stitch 从不附图。

    @param thread_cards 已排好序的线索摘要卡
    @param candidate_card 候选摘要卡
    @param cfg 已解析的不可变配置
    @return 可直接交给 M9 的提示词包
    """
    st = cfg.stitch
    lines = [_SYSTEM_HEAD.replace("{P}", str(len(thread_cards)))]
    if st.context:
        lines.append(st.context)
    lines.append(_STRUCTURE_SENTENCE)
    lines.append(_STRUCTURE_SHAPE)
    system = Message(role="system", parts=(Part(kind="text", text="\n".join(lines)),))

    parts: list[Part] = [Part(kind="text", text=card) for card in thread_cards]
    if not thread_cards:
        parts.append(Part(kind="text", text=_EMPTY_POOL_LINE))
    parts.append(Part(kind="text", text=candidate_card))
    return PromptBundle(messages=(system, Message(role="user", parts=tuple(parts))))


# ── 票数聚合（T18/M-4，纯逻辑）─────────────────────────────────────────────

def aggregate_votes(samples: Sequence[Mapping]) -> Mapping | None:
    """在完整的 (verdict, thread_ref) 判决键上取严格多数（M-4）。

    某个键的计数严格超过 n/2 即获胜，并整体返回该多数簇的**首个**样本
    （task_name/reason 随之带出）。任何不足严格多数的分裂——包括 verdict 占多数但
    thread_ref 分裂——一律返回 None（调用方回退到保守结果：episode → new，rescue → 漏缝）。

    @param samples 同一提示词的多次采样结果
    @return 获胜样本；无严格多数或样本为空时为 None
    """
    if not samples:
        return None
    counts: dict[tuple, int] = {}
    first: dict[tuple, Mapping] = {}
    for sample in samples:
        key = (sample["verdict"], sample["thread_ref"])
        counts[key] = counts.get(key, 0) + 1
        first.setdefault(key, sample)
    best_key, best_n = max(counts.items(), key=lambda kv: kv[1])
    if best_n * 2 > len(samples):
        return first[best_key]
    return None


# ── 一次判决（votes 感知）──────────────────────────────────────────────────

async def judge_stitch(thread_cards: Sequence[str], candidate_card: str,
                       ctx: "RunContext",
                       record_ids: tuple[str, ...] = ()) -> Mapping | None:
    """对一个候选做一次判决，经 complete_validated(schema=stitch_schema())。

    votes == 1（默认）：按 profile 默认温度发一次调用。votes > 1（T18）：同一提示词并发采
    n 个样本，按 M-4 严格多数聚合；SchemaViolation 的样本视为弃权，provider/内部错误照常
    上抛（沿用 classify 自一致的纪律）——存活样本为零时重抛最后一次违规，交由阶段的
    on_error 处置。

    @param thread_cards 已排好序的线索摘要卡
    @param candidate_card 候选摘要卡
    @param ctx 运行上下文
    @param record_ids 计入 llm.call 的记录 id（候选首个成员帧）
    @return 获胜的判决对象；票数分裂不足严格多数时为 None
    @raises SchemaViolation votes > 1 且全部样本都违规时重抛最后一次违规
    """
    cfg = ctx.cfg
    prompt = build_stitch_prompt(thread_cards, candidate_card, cfg)
    schema = stitch_schema()
    n = cfg.stitch.votes
    scope = CallScope(record_ids=record_ids, batch_no=ctx.batch_no)
    if n == 1:
        obj, _usage, _attempts, _model = await ctx.schema_engine.complete_validated(
            cfg.stitch.llm, prompt, schema, scope=scope)
        return obj

    results = await asyncio.gather(
        *(ctx.schema_engine.complete_validated(
            cfg.stitch.llm, prompt, schema, scope=scope) for _ in range(n)),
        return_exceptions=True)
    samples: list[Mapping] = []
    last_violation: SchemaViolation | None = None
    for res in results:
        if isinstance(res, SchemaViolation):
            last_violation = res                   # 该样本弃权
        elif isinstance(res, BaseException):
            raise res                              # provider / 内部错误照常上抛
        else:
            obj, _usage, _attempts, _model = res
            samples.append(obj)
    if not samples:
        raise last_violation if last_violation is not None else SchemaViolation(
            ["stitch votes: all samples failed"], "")
    return aggregate_votes(samples)


# ── 会话内状态 ──────────────────────────────────────────────────────────────

class _Fragment:
    """一个线索碎片：自有成员在会话序上的一个连续块。"""

    __slots__ = ("first_pos", "last_pos", "member_count", "cause",
                 "source_episode", "first", "last")

    def __init__(self, members: Sequence[Record], first_pos: int, last_pos: int,
                 cause: str, source_episode: str | None):
        """构造一个碎片。

        @param members 本碎片的成员帧，按会话序
        @param first_pos 首帧的会话序位置
        @param last_pos 尾帧的会话序位置
        @param cause 成因，∈ {"origin", "resumed", "rescued"}
        @param source_episode 原 episode id；救援碎片没有 episode 形态，传 None
        """
        self.first_pos = first_pos
        self.last_pos = last_pos
        self.member_count = len(members)
        self.cause = cause                      # "origin" | "resumed" | "rescued"
        self.source_episode = source_episode    # 原 episode id；救援 → None
        self.first = members[0]
        self.last = members[-1]


class _Thread:
    """一条开放/关闭的线索：存活信封 + 滚动更新的卡片状态。"""

    __slots__ = ("envelope", "fragments", "task_name", "last_active", "alive",
                 "head_pos", "tail_pos")

    def __init__(self, envelope: PipelineItem, fragment: _Fragment,
                 task_name: str, clock: int):
        """构造一条线索。

        @param envelope 存活的 episode 信封（线索的产物载体）
        @param fragment 开线索时的首个碎片
        @param task_name 线索任务名
        @param clock 会话内的判决时钟（用于最近活跃排序）
        """
        self.envelope = envelope
        self.fragments: list[_Fragment] = [fragment]
        self.task_name = task_name
        self.last_active = clock
        self.alive = True
        self.head_pos = fragment.first_pos
        self.tail_pos = fragment.last_pos

    @property
    def members(self) -> tuple[Record, ...]:
        """线索当前的全部成员帧（即存活信封 Record 的成员元组）。

        @return 成员帧元组，按会话序
        """
        return self.envelope.record.members

    def fragment_tails(self) -> list[Record]:
        """各碎片的尾帧——先验腿③（same_page）的比对面。

        @return 每个碎片的尾帧列表，按碎片顺序
        """
        return [fragment.last for fragment in self.fragments]


@dataclasses.dataclass(frozen=True, slots=True)
class _Candidate:
    """一个 pass-1 候选：episode 信封，或一段待救援的短段帧。"""

    kind: str                              # 候选类型："episode" | "rescue"
    members: tuple[Record, ...]            # 候选的成员帧记录，按会话序
    first_pos: int                         # 候选首帧的会话序位置
    last_pos: int                          # 候选尾帧的会话序位置
    envelope: PipelineItem | None = None   # 仅 episode 候选有信封；rescue 恒为 None
    frames: tuple[PipelineItem, ...] = ()  # 仅 rescue 候选有帧信封；episode 恒为空


@dataclasses.dataclass(frozen=True, slots=True)
class _MergeDecision:
    """一次判决归结出的合并结论（判决 × 机械先验合取之后）。"""

    target: _Thread | None                 # 合并目标线索；None = 不合并（开新线索 / 漏缝）
    priors: tuple[str, ...]                # 命中的 T9 机械先验腿名（trace 载荷词表）


@dataclasses.dataclass(frozen=True, slots=True)
class _SessionState:
    """单会话缝合过程的载体：只读上下文 + 会话内可变的线索集合。"""

    sid: str                               # 会话 id
    ctx: "RunContext"                      # 运行上下文（配置 / 指标 / Schema 引擎）
    position_of: Mapping[str, int]         # 帧 id → 会话序位置（首次出现为准）
    threads: list[_Thread]                 # 线索创建序列表，含被淘汰出池者
    pool: list[_Thread]                    # 开放线索池（单调选择池），容量 stitch.max_open


def select_eviction(pool: Sequence[_Thread], candidate_pos: int,
                    stale_gap_steps: int) -> _Thread:
    """池满时的淘汰优先级（T8/M-3）。

    ① 挂起跨度（候选位置 − 线索尾位置）超过 stale_gap_steps 的线索优先（0 = 该腿关闭），
    过期者之间取 LRU；② 否则退回纯 LRU。对池的插入序确定（同分保留更早的线索）。

    @param pool 当前开放线索池
    @param candidate_pos 当前候选的首帧会话序位置
    @param stale_gap_steps 过期判定阈值；0 表示关闭该腿
    @return 应被淘汰出池的线索
    """
    if stale_gap_steps > 0:
        stale = [t for t in pool
                 if candidate_pos - t.tail_pos > stale_gap_steps]
        if stale:
            return min(stale, key=lambda t: t.last_active)
    return min(pool, key=lambda t: t.last_active)


def span_distance(a_head: int, a_tail: int, b_head: int, b_tail: int) -> int:
    """两个跨度之间的会话序距离——T19 池截断度量。

    重叠时为 0，否则取较近两端之间的间隔。

    @param a_head 跨度 A 的首位置
    @param a_tail 跨度 A 的尾位置
    @param b_head 跨度 B 的首位置
    @param b_tail 跨度 B 的尾位置
    @return 会话序距离，重叠为 0
    """
    if a_tail < b_head:
        return b_head - a_tail
    if b_tail < a_head:
        return a_head - b_tail
    return 0


def compute_seams(members: Sequence[Record], position_of: Mapping[str, int],
                  owner_task: Mapping[str, str],
                  own_ids: frozenset[str],
                  frame_ids_by_pos: Sequence[str],
) -> tuple[tuple[int, ...], tuple[tuple[str, ...], ...]]:
    """接缝判定（T20/M-1）。

    相邻成员对 ⟨i, i+1⟩ 是接缝，当且仅当两成员之间的会话序间隙里至少有 1 帧被**别的**线索
    吸收。纯噪音间隙（以及无主帧构成的间隙）**不是**接缝——那些对由 extract 正常判决，与
    v1.8 的剔噪约定一致。

    @param members 线索重绑后的成员帧元组，按会话序
    @param position_of 帧 id → 会话序位置
    @param owner_task 帧 id → 吸收它的线索任务名
    @param own_ids 本线索自有成员的 id 集合
    @param frame_ids_by_pos 会话序位置 → 帧 id
    @return （接缝下标元组，各接缝的打断者任务名元组）——接缝下标是重绑成员元组里**左**成员
            的下标（m-8：与 Transition.index 同坐标，范围 [0, len(members)−2]），打断者按
            间隙顺序去重列出（M-1：接缝的打断者列表永不为空）
    """
    seams: list[int] = []
    interrupted: list[tuple[str, ...]] = []
    for i in range(len(members) - 1):
        left = position_of.get(members[i].id)
        right = position_of.get(members[i + 1].id)
        if left is None or right is None:
            continue
        names: list[str] = []
        for pos in range(left + 1, right):
            frame_id = frame_ids_by_pos[pos]
            if frame_id in own_ids:
                continue
            task = owner_task.get(frame_id)
            if task is not None and task not in names:
                names.append(task)
        if names:
            seams.append(i)
            interrupted.append(tuple(names))
    return tuple(seams), tuple(interrupted)


def _feed_breaker_once(exc: BaseException, ctx: "RunContext") -> None:
    """反应式 400 终态恰好喂一次熔断器（A7；v1.11 V27①）。

    只有 phase="reactive" 且 origin="http_400" 的溢出终态可以喂——200 形态搭乘的是一次成功
    HTTP 交互，其 ok 已清空连续计数。异常对象上打一个已喂标记，避免同一异常被喂第二次。

    @param exc 捕获到的异常
    @param ctx 运行上下文
    """
    if (isinstance(exc, ContextOverflowError) and exc.phase == "reactive"
            and getattr(exc, "origin", "http_400") == "http_400"
            and not getattr(exc, "_breaker_fed", False)):
        exc._breaker_fed = True  # type: ignore[attr-defined]
        ctx.metrics.record_provider_result(fatal=True)


# ── 阶段 ─────────────────────────────────────────────────────────────────────

class StitchStage:
    """M16 缝合算子：把同会话的 episode 碎片保守地缝回线索（Stage 合同 ②c）。"""

    name = "stitch"

    def __init__(self, cfg: "ResolvedConfig"):
        """构造缝合阶段。

        @param cfg 已解析的不可变配置
        """
        self.cfg = cfg

    async def run(self, batch: list[PipelineItem],
                  ctx: "RunContext") -> list[PipelineItem]:
        """逐会话缝合本批次的分段产物。

        选择与幂等：会话严格按批内位置序（= 会话序）处理；episode 候选是尚未缝过的活跃
        sequence 信封（thread_id 在开线索时打戳，因此重入零调用）。零 episode 候选的会话整个
        跳过——救援候选独自面对空池永远无法合并（B-2）。会话之间**串行**：池是一个串行决策
        过程、线索状态是会话局部的——事件与判决顺序因此确定，零 rng（按 §3.5 成本模型，
        每会话的调用数很少）。

        @param batch 本批次信封列表（原地改状态，绝不删除元素）
        @param ctx 运行上下文
        @return 与入参同一个列表对象
        """
        frames_by_sid: dict[str, list[PipelineItem]] = {}
        episodes_by_sid: dict[str, list[PipelineItem]] = {}
        order: list[str] = []
        for item in batch:
            if item.session_id is None:
                continue
            if item.session_id not in frames_by_sid:
                frames_by_sid[item.session_id] = []
                episodes_by_sid[item.session_id] = []
                order.append(item.session_id)
            if item.record.kind == "single":
                frames_by_sid[item.session_id].append(item)
            elif (item.record.kind == "sequence" and item.status == "active"
                    and item.thread_id is None):
                episodes_by_sid[item.session_id].append(item)

        for sid in order:
            if not episodes_by_sid[sid]:
                continue
            await self._run_session(sid, frames_by_sid[sid],
                                    episodes_by_sid[sid], ctx)
        return batch                            # 同一个列表对象（②c）

    # ── 单会话驱动 ───────────────────────────────────────────────────────────

    async def _run_session(self, sid: str, frames: list[PipelineItem],
                           episodes: list[PipelineItem],
                           ctx: "RunContext") -> None:
        """跑完一个会话：候选流 → 单调池判决 → 可选二次重判 → 收尾标记。

        @param sid 会话 id
        @param frames 该会话的全部帧信封，按会话序
        @param episodes 该会话尚未缝过的活跃 episode 信封
        @param ctx 运行上下文
        """
        position_of: dict[str, int] = {}
        for i, frame in enumerate(frames):
            position_of.setdefault(frame.record.id, i)
        session = _SessionState(sid=sid, ctx=ctx, position_of=position_of,
                                threads=[], pool=[])

        candidates = self._assemble_candidates(frames, episodes, position_of)
        clock = 0
        for cand in candidates:
            if cand.kind == "rescue" and not session.pool:
                continue                        # B-2：零调用，保持 dropped_noise
            clock = await self._judge_candidate(session, cand, clock)

        if self.cfg.stitch.repass:
            clock = await self._repass(session, clock)

        self._finalize_session(session, frames)

    def _assemble_candidates(self, frames: list[PipelineItem],
                             episodes: list[PipelineItem],
                             position_of: Mapping[str, int]) -> list[_Candidate]:
        """按会话序组装候选流（T11）。

        每个 episode 信封一个候选；stitch.rescue_short 打开时，再为每一段
        below_min_len 丢帧的**连续**会话序游程（中间不夹别的帧）加一个救援候选——游程重新
        成形刻意忽略原来的分段切分，相邻短段会融成一个候选。reason="noise" 的帧永不进入
        候选流（V4 闭合）。

        @param frames 该会话的全部帧信封，按会话序
        @param episodes 该会话尚未缝过的活跃 episode 信封
        @param position_of 帧 id → 会话序位置
        @return 按首帧位置升序排列的候选列表
        """
        candidates: list[_Candidate] = []
        for episode in episodes:
            members = episode.record.members
            positions = [position_of[m.id] for m in members
                         if m.id in position_of]
            first = min(positions) if positions else 0
            last = max(positions) if positions else 0
            candidates.append(_Candidate("episode", tuple(members), first, last,
                                         envelope=episode))
        if self.cfg.stitch.rescue_short:
            run: list[PipelineItem] = []
            for frame in frames + [None]:       # 哨兵：冲洗尾部游程
                rescueable = (
                    frame is not None
                    and frame.status == "dropped_noise"
                    and getattr(frame, "noise_attribution", None)
                    == ("segment", "below_min_len"))
                if rescueable:
                    run.append(frame)
                    continue
                if run:
                    members = tuple(f.record for f in run)
                    candidates.append(_Candidate(
                        "rescue", members,
                        position_of[members[0].id], position_of[members[-1].id],
                        frames=tuple(run)))
                    run = []
        candidates.sort(key=lambda c: c.first_pos)
        return candidates

    def _pool_cards(self, pool_view: Sequence[_Thread],
                    candidate_head: Record) -> list[str]:
        """按池的展示序渲染线索摘要卡（1 基编号即提示词里的线索编号）。

        @param pool_view 已按最近活跃降序排好的线索视图
        @param candidate_head 候选首帧，用于渲染 E5 接续对行
        @return 与 pool_view 对齐的摘要卡列表
        """
        return [render_thread_card(
                    ThreadCard(index=i, task_name=t.task_name, members=t.members,
                               span=(t.head_pos, t.tail_pos),
                               fragment_count=len(t.fragments)),
                    candidate_head, self.cfg)
                for i, t in enumerate(pool_view, start=1)]

    async def _judge_candidate(self, session: _SessionState, cand: _Candidate,
                               clock: int) -> int:
        """对一个 pass-1 候选跑一次判决并落状态。

        判决失败走 on_error 处置（合同 ④ 记录级隔离：只有「大三样」向上逃逸）。

        @param session 本会话状态
        @param cand 当前候选
        @param clock 会话内判决时钟
        @return 递增后的判决时钟
        """
        pool_view = sorted(session.pool, key=lambda t: t.last_active,
                           reverse=True)
        cards = self._pool_cards(pool_view, cand.members[0])
        cand_card = render_candidate_card(cand.kind, cand.members,
                                          (cand.first_pos, cand.last_pos),
                                          self.cfg)
        try:
            outcome = await judge_stitch(cards, cand_card, session.ctx,
                                         record_ids=(cand.members[0].id,))
        except (CircuitBreakerTripped, KeyboardInterrupt,
                asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 — 记录级隔离绝对优先
            _logger.error("stitch judgment failed: session=%s candidate=%s "
                          "exc=%s", session.sid, cand.kind,
                          type(exc).__name__,
                          extra={"stage": self.name,
                                 "batch": session.ctx.batch_no})
            self._dispose_failure(session, cand, exc, clock)
            return clock + 1
        session.ctx.metrics.count(_COUNTER_JUDGMENTS)
        decision = self._resolve_merge(outcome, pool_view, cand)
        self._apply_decision(session, cand, outcome, decision, clock)
        self._emit_judge(session, cand, outcome, decision, repass=False)
        return clock + 1

    # ── 合并闸门（T8 判决 × T9 先验）─────────────────────────────────────────

    def _resolve_merge(self, outcome: Mapping | None,
                       pool_view: Sequence[_Thread],
                       cand: _Candidate) -> _MergeDecision:
        """把一次判决结果映射为合并结论。

        None（票数分裂）与非 resume 判决都归结为不合并；resume 必须给出合法的 1 基池内卡片
        编号，且在 bias="conservative" 下还要清过 T9 先验合取——超过 stale_gap_steps 时先验
        降级为需要**两条**腿（E7 时间衰减）。

        @param outcome 判决对象；None 表示票数分裂
        @param pool_view 本次判决展示给 LLM 的线索视图（编号即下标 + 1）
        @param cand 当前候选
        @return 合并结论（目标线索 + 命中的先验腿名）
        """
        st = self.cfg.stitch
        if outcome is None or outcome.get("verdict") != "resume":
            return _MergeDecision(target=None, priors=())
        ref = outcome.get("thread_ref")
        if not isinstance(ref, int) or not 1 <= ref <= len(pool_view):
            return _MergeDecision(target=None, priors=())  # 保守：非法编号即 new
        thread = pool_view[ref - 1]
        hits = tuple(prior_hits(thread.members, thread.fragment_tails(),
                                cand.members))
        if st.bias == "llm":
            return _MergeDecision(target=thread, priors=hits)
        required = 1
        if st.stale_gap_steps > 0 and (
                cand.first_pos - thread.tail_pos > st.stale_gap_steps):
            required = 2
        if len(hits) >= required:
            return _MergeDecision(target=thread, priors=hits)
        return _MergeDecision(target=None, priors=hits)

    # ── 状态迁移（合同 ②c）──────────────────────────────────────────────────

    def _apply_decision(self, session: _SessionState, cand: _Candidate,
                        outcome: Mapping | None, decision: _MergeDecision,
                        clock: int) -> None:
        """按合并结论落 pass-1 状态：开线索 / 合并 / 救援命中。

        @param session 本会话状态
        @param cand 当前候选
        @param outcome 判决对象；None 表示票数分裂
        @param decision 合并结论
        @param clock 会话内判决时钟
        """
        if cand.kind == "episode":
            if decision.target is None:
                task_name = outcome["task_name"] if outcome else ""
                self._open_thread(session, cand, task_name, clock)
            else:
                self._merge_pass1(session, decision.target, cand, outcome, clock)
        elif decision.target is not None:       # 救援命中
            self._merge_rescue(session, decision.target, cand, outcome, clock)

    def _open_thread(self, session: _SessionState, cand: _Candidate,
                     task_name: str, clock: int) -> None:
        """episode 候选开一条新线索（救援候选**永不**到这里，B-2）。

        池满时先淘汰一条开放线索（M-3：关闭只发生在这里；被淘汰的线索仍是 pass-2 的目标，
        也仍是一个正常产物）。

        @param session 本会话状态
        @param cand 当前候选（必为 episode 形态）
        @param task_name 新线索的任务名；判决失败的 keep 路径传空串
        @param clock 会话内判决时钟
        """
        if len(session.pool) >= self.cfg.stitch.max_open:
            evicted = select_eviction(session.pool, cand.first_pos,
                                      self.cfg.stitch.stale_gap_steps)
            session.pool.remove(evicted)
        envelope = cand.envelope
        assert envelope is not None
        envelope.thread_id = envelope.record.id     # T22 身份链
        fragment = _Fragment(cand.members, cand.first_pos, cand.last_pos,
                             cause="origin", source_episode=envelope.record.id)
        thread = _Thread(envelope, fragment, task_name, clock)
        session.threads.append(thread)
        session.pool.append(thread)

    def _merge_pass1(self, session: _SessionState, target: _Thread,
                     cand: _Candidate, outcome: Mapping, clock: int) -> None:
        """pass-1 合并：开线索的那个信封存活（m-7），候选信封变成 stitched 空壳（②c①/②）。

        @param session 本会话状态
        @param target 合并目标线索
        @param cand 当前候选（必为 episode 形态）
        @param outcome 判决对象
        @param clock 会话内判决时钟
        """
        assert cand.envelope is not None
        self._rebind(target, cand.members, session.position_of)
        cand.envelope.status = "stitched"
        target.fragments.append(_Fragment(
            cand.members, cand.first_pos, cand.last_pos,
            cause="resumed", source_episode=cand.envelope.record.id))
        self._touch(target, outcome, clock)

    def _merge_rescue(self, session: _SessionState, target: _Thread,
                      cand: _Candidate, outcome: Mapping, clock: int) -> None:
        """救援命中：成员帧 dropped_noise → absorbed（②c③）并打 rescued_by 审计标记。

        rescued_short 计的是**帧**数（m-10）。不产生空壳——救援候选没有信封形态（T7 范围规则）。

        @param session 本会话状态
        @param target 合并目标线索
        @param cand 当前候选（必为 rescue 形态）
        @param outcome 判决对象
        @param clock 会话内判决时钟
        """
        self._rebind(target, cand.members, session.position_of)
        for frame in cand.frames:
            frame.status = "absorbed"
            frame.rescued_by = target.envelope.record.id  # type: ignore[attr-defined]
        session.ctx.metrics.count(_COUNTER_RESCUED_SHORT, len(cand.frames))
        target.fragments.append(_Fragment(
            cand.members, cand.first_pos, cand.last_pos,
            cause="rescued", source_episode=None))
        self._touch(target, outcome, clock)

    @staticmethod
    def _rebind(target: _Thread, new_members: Sequence[Record],
                position_of: Mapping[str, int]) -> None:
        """Record 重绑（②c②）：成员并集按会话序升序；record.id **绝不**重算
        （T6/T22——沿用 M7 手术先例）。

        @param target 存活线索
        @param new_members 并入的成员帧
        @param position_of 帧 id → 会话序位置
        """
        merged = sorted(
            (*target.members, *new_members),
            key=lambda record: position_of.get(record.id, 0))
        target.envelope.record = dataclasses.replace(
            target.envelope.record, members=tuple(merged))

    @staticmethod
    def _touch(target: _Thread, outcome: Mapping, clock: int) -> None:
        """合并后的滚动卡片状态：任务名取命中判决（M-6），跨度放宽，最近活跃前移。

        @param target 存活线索
        @param outcome 命中的判决对象
        @param clock 会话内判决时钟
        """
        task_name = outcome.get("task_name")
        if task_name:
            target.task_name = task_name
        first = min(fragment.first_pos for fragment in target.fragments)
        last = max(fragment.last_pos for fragment in target.fragments)
        target.head_pos, target.tail_pos = first, last
        target.last_active = clock

    def _dispose_failure(self, session: _SessionState, cand: _Candidate,
                         exc: Exception, clock: int) -> None:
        """stitch_invalid 的两形态处置（§3.16 失败语义）。

        "keep"（默认）：episode 候选自开一条线索——证据对 = error 事件 + stitch.failures
        计数器，绝不写 item.errors（S26 形态）；救援候选保持 dropped_noise，证据同款。
        "fail"：**只有** episode 候选信封失败（成员帧仍是 absorbed——②c 不授予
        absorbed→failed 的迁移）；救援候选永不走 fail 路径——失败即漏缝（B-2）。
        v1.11（V27①/spec 3.16.4 上下文预算行）：凡是记录 kind 的地方一律先走预算词表
        ——on_error 处置本身不变（卡片池是静态有界的，属 M1 WARN 领域；运行期兜底 = M9 咽喉
        + 本 keep/fail 路径）。context_overflow 的**拒收**计一次 budget.overflow_records；
        反应式 400 终态恰好喂一次熔断器（A7）。

        @param session 本会话状态
        @param cand 判决失败的候选
        @param exc 捕获到的异常
        @param clock 会话内判决时钟
        """
        ctx = session.ctx
        kind = budget.classify_stage_error(exc) or ErrorKind.STITCH_INVALID.value
        message = str(exc)
        if cand.kind == "episode":
            if self.cfg.stitch.on_error == "fail":
                assert cand.envelope is not None
                cand.envelope.errors.append(StageError(
                    stage=self.name, kind=kind, message=message,
                    retryable=False))
                cand.envelope.status = "failed"
                if kind == ErrorKind.CONTEXT_OVERFLOW.value:
                    ctx.metrics.count("budget.overflow_records")
            else:                               # "keep"：无名引导一条线索
                self._open_thread(session, cand, "", clock)
        _feed_breaker_once(exc, ctx)
        ctx.metrics.count(_COUNTER_FAILURES)
        ctx.metrics.event(_EV_ERROR, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(cand.members[0].id,),
                          payload={"stage": self.name, "kind": kind,
                                   "message": message, "retryable": False})

    # ── 二次重判（T19/M-2）───────────────────────────────────────────────────

    async def _repass(self, session: _SessionState, clock: int) -> int:
        """有界的二次重判。

        候选 = pass 1 **结束时**仍只有一个碎片的线索（按其碎片的会话序）；目标集 = 该会话
        其余所有存活线索。目标集是一个**活视图**——一次合并立即更新跨度与卡片；被并走的
        候选就此消费。

        @param session 本会话状态
        @param clock 会话内判决时钟
        @return 递增后的判决时钟
        """
        snapshot = [t for t in session.threads
                    if t.alive and len(t.fragments) == 1]
        snapshot.sort(key=lambda t: t.fragments[0].first_pos)
        for cand_thread in snapshot:
            if not cand_thread.alive:
                continue                        # 已在 pass 2 早些时候被并走
            others = [t for t in session.threads
                      if t.alive and t is not cand_thread]
            if not others:
                continue                        # 零调用
            clock = await self._repass_one(session, cand_thread, others, clock)
        return clock

    async def _repass_one(self, session: _SessionState, cand_thread: _Thread,
                          others: list[_Thread], clock: int) -> int:
        """对一条单碎片线索跑一次 pass-2 判决。

        目标集超过 max_open 时按跨度距离截断到最近的 max_open 条（M-2：不是区间相交），
        再按最近活跃降序展示。合并方向**反转**（T6 存活规则）：候选信封变成空壳，目标线索
        存活并把碎片按会话序重排。

        @param session 本会话状态
        @param cand_thread 作为候选的单碎片线索
        @param others 该会话其余存活线索（非空）
        @param clock 会话内判决时钟
        @return 递增后的判决时钟
        """
        st = self.cfg.stitch
        if len(others) > st.max_open:
            others = sorted(
                others,
                key=lambda t: (span_distance(cand_thread.head_pos,
                                             cand_thread.tail_pos,
                                             t.head_pos, t.tail_pos),
                               t.head_pos))[:st.max_open]
        pool_view = sorted(others, key=lambda t: t.last_active, reverse=True)
        cards = self._pool_cards(pool_view, cand_thread.members[0])
        cand = _Candidate("episode", cand_thread.members,
                          cand_thread.head_pos, cand_thread.tail_pos,
                          envelope=cand_thread.envelope)
        cand_card = render_candidate_card("episode", cand.members,
                                          (cand.first_pos, cand.last_pos),
                                          self.cfg)
        try:
            outcome = await judge_stitch(cards, cand_card, session.ctx,
                                         record_ids=(cand.members[0].id,))
        except (CircuitBreakerTripped, KeyboardInterrupt,
                asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 — 记录级隔离
            _logger.error("stitch repass judgment failed: session=%s exc=%s",
                          session.sid, type(exc).__name__,
                          extra={"stage": self.name,
                                 "batch": session.ctx.batch_no})
            self._dispose_repass_failure(session, cand, exc)
            return clock + 1
        session.ctx.metrics.count(_COUNTER_REPASS_JUDGMENTS)
        decision = self._resolve_merge(outcome, pool_view, cand)
        if decision.target is not None:
            self._merge_pass2(session, decision.target, cand_thread, outcome,
                              clock)
        self._emit_judge(session, cand, outcome, decision, repass=True)
        return clock + 1

    def _dispose_repass_failure(self, session: _SessionState, cand: _Candidate,
                                exc: Exception) -> None:
        """pass-2 判决失败的处置：绝不让已开的线索失败，候选就保持自己那条线索
        （等价于 keep）。

        v1.11（V27①）：事件里记精确的预算 kind；反应式 400 终态恰好喂一次熔断器（A7）。

        @param session 本会话状态
        @param cand 判决失败的候选（pass-2 形态恒为 episode）
        @param exc 捕获到的异常
        """
        ctx = session.ctx
        kind = budget.classify_stage_error(exc) or ErrorKind.STITCH_INVALID.value
        _feed_breaker_once(exc, ctx)
        ctx.metrics.count(_COUNTER_FAILURES)
        ctx.metrics.event(
            _EV_ERROR, stage=self.name, batch_no=ctx.batch_no,
            record_ids=(cand.members[0].id,),
            payload={"stage": self.name, "kind": kind,
                     "message": str(exc), "retryable": False})

    def _merge_pass2(self, session: _SessionState, target: _Thread,
                     cand_thread: _Thread, outcome: Mapping,
                     clock: int) -> None:
        """pass-2 合并，方向反转（T6 存活规则）：候选线索的信封变成空壳，目标线索存活。

        转移过来的碎片保留各自的成员块；候选的 origin 碎片改因为 "resumed"（它是经一次
        resume 判决并入目标的；rescued 碎片保持 "rescued"）；episode_id/thread_id 跟随存活
        信封（T22）。

        @param session 本会话状态
        @param target 存活的目标线索
        @param cand_thread 作为候选、就此变成空壳的线索
        @param outcome 命中的判决对象
        @param clock 会话内判决时钟
        """
        self._rebind(target, cand_thread.members, session.position_of)
        cand_thread.envelope.status = "stitched"
        cand_thread.alive = False
        for fragment in cand_thread.fragments:
            if fragment.cause == "origin":
                fragment.cause = "resumed"
            target.fragments.append(fragment)
        target.fragments.sort(key=lambda fragment: fragment.first_pos)
        self._touch(target, outcome, clock)

    # ── 收尾：接缝 + 碎片元数据 + 事件 ───────────────────────────────────────

    def _finalize_session(self, session: _SessionState,
                          frames: list[PipelineItem]) -> None:
        """给每条存活线索打上鸭子标记，并逐线索发一条 stitch.thread 事件。

        标记含 stitch_fragments（按会话序的碎片表 → _meta.stream.fragments）与
        seam_indexes + seam_interrupted_by（T20/M-1）。信封级的 order_span 仍归 M11
        （包络语义：多碎片线索的跨度里可能夹着别的线索的帧）。

        @param session 本会话状态
        @param frames 该会话的全部帧信封，按会话序
        """
        alive = [t for t in session.threads if t.alive]
        owner_task: dict[str, str] = {}
        for thread in alive:
            for member in thread.members:
                owner_task.setdefault(member.id, thread.task_name)
        frame_ids_by_pos = [frame.record.id for frame in frames]

        for thread in alive:
            thread.fragments.sort(key=lambda fragment: fragment.first_pos)
            envelope = thread.envelope
            envelope.stitch_fragments = tuple(  # type: ignore[attr-defined]
                {"order_span": [_order_key_repr(fragment.first),
                                _order_key_repr(fragment.last)],
                 "member_count": fragment.member_count,
                 "cause": fragment.cause,
                 "source_episode": fragment.source_episode}
                for fragment in thread.fragments)
            own_ids = frozenset(member.id for member in thread.members)
            seams, interrupted = compute_seams(
                thread.members, session.position_of, owner_task, own_ids,
                frame_ids_by_pos)
            envelope.seam_indexes = seams  # type: ignore[attr-defined]
            envelope.seam_interrupted_by = interrupted  # type: ignore[attr-defined]
            if seams:
                session.ctx.metrics.count(_COUNTER_SEAMS, len(seams))
            self._emit_thread(session, thread, seams)

    def _emit_thread(self, session: _SessionState, thread: _Thread,
                     seams: tuple[int, ...]) -> None:
        """发一条 stitch.thread 事件（每条存活线索一条）。

        @param session 本会话状态
        @param thread 存活线索
        @param seams 该线索的接缝下标元组
        """
        envelope = thread.envelope
        session.ctx.metrics.event(
            _EV_THREAD, stage=self.name, batch_no=session.ctx.batch_no,
            record_ids=(envelope.record.id,),
            payload={"session_id": session.sid,
                     "thread_id": envelope.record.id,
                     "task_name": thread.task_name,
                     "fragments": [dict(f) for f
                                   in envelope.stitch_fragments],  # type: ignore[attr-defined]
                     "seam_indexes": list(seams)})

    # ── trace 事件（stitch.judge）─────────────────────────────────────────────

    def _emit_judge(self, session: _SessionState, cand: _Candidate,
                    outcome: Mapping | None, decision: _MergeDecision, *,
                    repass: bool) -> None:
        """每次判决发一条 stitch.judge 事件（T16）。

        record_ids = 候选碎片的首个成员 id；载荷含 verdict/thread_ref/confidence 与命中的
        先验腿；outcome 为 None 即票数分裂（按保守回退的 verdict 记录）。

        @param session 本会话状态
        @param cand 当前候选
        @param outcome 判决对象；None 表示票数分裂
        @param decision 合并结论
        @param repass 是否属于二次重判
        """
        target = decision.target
        payload: dict = {
            "session_id": session.sid,
            "candidate": cand.kind,
            "repass": repass,
            "verdict": outcome.get("verdict") if outcome else "new",
            "thread_ref": outcome.get("thread_ref") if outcome else None,
            "confidence": outcome.get("confidence") if outcome else None,
            "priors": list(decision.priors),
            "merged": target is not None,
        }
        if outcome is None:
            payload["votes_split"] = True       # M-4 回退留下的痕迹
        else:
            payload["task_name"] = outcome.get("task_name")
            payload["reason"] = outcome.get("reason")
        if target is not None:
            payload["target_thread_id"] = target.envelope.record.id
        session.ctx.metrics.event(_EV_JUDGE, stage=self.name,
                                  batch_no=session.ctx.batch_no,
                                  record_ids=(cand.members[0].id,),
                                  payload=payload)


def _order_key_repr(member: Record) -> str | int | None:
    """`fragments[].order_span` 的元素——成员的排序键呈现形态
    （文本 = "file:line_no"，UI = pair_index）。

    这是 M16 **自有的** M11 渲染副本（算子模块之间从不互相依赖，spec §2.2）。

    @param member 成员帧记录
    @return 排序键的呈现值
    """
    ref = member.ref
    if ref.line_no is not None:
        return f"{ref.source_file}:{ref.line_no}"
    return ref.pair_index

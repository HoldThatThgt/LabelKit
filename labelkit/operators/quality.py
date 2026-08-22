"""M4 质量打分（QuRating）——spec 3.4、CONTRACTS.md §7.3 / §10.2 / §10.3。

pairwise 模式：批内 k 轮种子化随机完美匹配，LLM 成对判定（可选双序呈现、可选多评委
逐准则多数票），Bradley-Terry MM 算法拟合（Hunter 2004），批内 log-theta 百分位归一化
到 [0, 1]。

pointwise 模式：逐记录逐准则 0–5 加性量表打分，除以 5 归一化。

聚合分 = 按 rubric 权重对非空准则分加权平均。门控：threshold 或 top_ratio 选择；未打分
记录按 quality.on_unscored 处置。

v1.7 按类分池（spec 3.4.3 按类分池、CONTRACTS.md §7.3）：classify 开启时，批内 active 项
按 item.classification.label 划分为按类池，各池在 cfg.class_views 给出的类生效
(QualityConfig, Rubric) 下打分与门控。两阶段执行（R13）：所有池的配对计划按类名字典序
同步预抽（全阶段唯一的 ctx.rng 消费——抽签顺序只取决于池顺序，与调用调度无关），随后所有
池的 LLM 判定调用冻结进一个 TaskGroupRequest（跨池有界并发）。池间失败隔离（R15）。classify 关闭 =
唯一匿名池，与 v1.7 之前逐字节一致（扁平计数键，payload 无 "pool" 字段）。

v1.8 序列打分（spec 3.4.3 sequence 行、CONTRACTS §7.3 / §10.2 / §10.3）：episode 信封
（record.kind == "sequence"）即使在 UI 模态下也渲染为纯文本（rule-34 视觉豁免的唯一一处，
S30）——[步骤序列]（item.transitions 逐步骤行；兜底步骤带（摘取兜底）后缀，与 LLM 确认的
"other" 区分开，S16）+ [成员帧摘要]（逐成员 frame_digest，有界）。transitions 经私有提示词
装配函数的尾部参数下传（非冻结面）；单条记录路径取默认 None，与 v1.7 逐字节一致。

v1.11 上下文预算装填（spec 3.4.3 v1.11 行、§3.3③④⑤）：每次 (比较, 评委) 调用各按该评委
profile 的预算装填（V25② 与 verify 的 min-over-panel 相对）；反应式溢出按 V20 至多降档
重试一次，最小语义单元仍装不下则按 V10 处置。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal, Mapping, Sequence

import numpy as np

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
from labelkit.common.contracts.generation import DownstreamAttemptRequest, DownstreamAttemptResult
from labelkit.common.contracts.types import (
    PipelineItem,
    QualityScore,
    Record,
    StageError,
    Transition,
    frame_digest,
)
from labelkit.operators.quality_calls import (
    PairwiseQualityCall,
    PairwiseQualityOutcome,
    PointwiseQualityCall,
    PointwiseQualityOutcome,
    QualityCall,
    QualityCallFailure,
    QualityCallOutcome,
    QualityFitRequest,
    QualityJudgment,
)

if TYPE_CHECKING:
    from labelkit.common.config.model import Criterion, LLMProfile, QualityConfig, ResolvedConfig
    from labelkit.common.contracts.stage import RunContext

# M9（llm_client）/ M8（schema_engine）公开面，见 CONTRACTS.md §7.8 / §7.7。
from labelkit.common.inference import budget
from labelkit.common.inference.llm_client import Message, Part, PromptBundle
from labelkit.common.inference.schema_engine import (
    CallScope,
    _thaw_json,
    judgment_schema,
    pointwise_schema,
)


AGGREGATE_KEY = "__aggregate__"

_logger = logging.getLogger("labelkit.quality")
_ATTEMPT_MODE: ContextVar[bool] = ContextVar("quality_attempt_mode", default=False)
_ATTEMPT_CONFIG: ContextVar[object | None] = ContextVar(
    "quality_attempt_config", default=None
)

# 事件名（逐字对齐 CONTRACTS.md §7.11 / §8.1）。
_EV_JUDGMENT = "quality.judgment"
_EV_POINTWISE = "quality.pointwise"
_EV_BT_FIT = "quality.bt_fit"
_EV_GATE = "quality.gate"
_EV_ERROR = "error"

_COUNTER_JUDGMENT_FAILURES = "quality.judgment_failures"


# ── Bradley-Terry 拟合（MM 算法，Hunter 2004） ────────────────────────────────

def fit_bradley_terry(n_items: int, comparisons: list[tuple[int, int, float]],
                      l2_pseudo: float = 0.1, tol: float = 1e-6,
                      max_iter: int = 200) -> np.ndarray:
    """用 MM 迭代（Hunter 2004）拟合 Bradley-Terry 强度。

    以 lambda=l2_pseudo 的伪比赛正则化（对一个 theta=1 的虚拟对手记半胜半负），每轮
    重归一化到 prod(theta)=1；max|delta log theta| < tol 或达到 max_iter 即停。

    @param n_items 参与拟合的记录条数
    @param comparisons (胜者下标, 负者下标, 权重) 列表；平局拆成两条权重 0.5 的记录
    @param l2_pseudo 伪比赛强度（L2 式正则化，防止全胜/全负记录的 theta 发散）
    @param tol 收敛阈值，作用在相邻两轮 log theta 的最大绝对变化上
    @param max_iter 最大迭代轮数
    @return 长度为 n_items 的 log-theta 数组
    """
    log_theta, _, _ = _fit_bradley_terry_details(n_items, comparisons, l2_pseudo, tol, max_iter)
    return log_theta


def _fit_bradley_terry_details(n_items: int, comparisons: list[tuple[int, int, float]],
                               l2_pseudo: float = 0.1, tol: float = 1e-6,
                               max_iter: int = 200) -> tuple[np.ndarray, int, bool]:
    """同 fit_bradley_terry，另外返回 quality.bt_fit 事件所需的迭代诊断。

    @param n_items 参与拟合的记录条数
    @param comparisons (胜者下标, 负者下标, 权重) 列表
    @param l2_pseudo 伪比赛强度
    @param tol 收敛阈值
    @param max_iter 最大迭代轮数
    @return (log-theta 数组, 实际迭代轮数, 是否收敛)
    """
    if n_items == 0:
        return np.zeros(0), 0, True
    # W[i] = 胜场总和，含对虚拟对手的 lambda/2 伪半胜。
    w = np.full(n_items, l2_pseudo / 2.0, dtype=float)
    # n[i][j] = i 与 j 之间的比较权重合计（对称矩阵）。
    n = np.zeros((n_items, n_items), dtype=float)
    for winner, loser, weight in comparisons:
        w[winner] += weight
        n[winner, loser] += weight
        n[loser, winner] += weight

    theta = np.ones(n_items, dtype=float)
    iterations = 0
    converged = False
    for iterations in range(1, max_iter + 1):
        # denom_i = sum_j n_ij/(theta_i+theta_j) + lambda/(theta_i + 1)（虚拟对手项）
        pair_sums = theta[:, None] + theta[None, :]
        denom = (n / pair_sums).sum(axis=1) + l2_pseudo / (theta + 1.0)
        new_theta = w / denom
        # 重归一化到 prod(theta) = 1（除以几何平均）。
        new_theta = new_theta / np.exp(np.mean(np.log(new_theta)))
        delta = float(np.max(np.abs(np.log(new_theta) - np.log(theta))))
        theta = new_theta
        if delta < tol:
            converged = True
            break
    return np.log(theta), iterations, converged


# ── 纯函数辅助（有直接单测覆盖） ──────────────────────────────────────────────

def _pairing_plan(n_items: int, rounds: int, rng) -> list[tuple[int, int, int, bool]]:
    """k 轮随机完美匹配：打乱下标后相邻配对，奇数条时多出的一条本轮轮空。

    全部随机性来自 rng（ctx.rng），且在任何 LLM 派发之前抽完——抽签消费顺序是可复现性
    硬约束，任何改写都不得改变 rng 调用的先后与次数。

    @param n_items 池内记录条数
    @param rounds 匹配轮数（quality.rounds）
    @param rng 已按 run.seed 播种的 random.Random 实例
    @return (轮次 1-based, 抽样序第一条下标, 抽样序第二条下标, 第一条是否占 A 位) 列表；
            (first_idx, second_idx) 是抽样序，呈现序由 first_is_a 决定（可能再被
            both_orders 翻转一次）
    """
    plan: list[tuple[int, int, int, bool]] = []
    for round_no in range(1, rounds + 1):
        order = list(range(n_items))
        rng.shuffle(order)
        for k in range(n_items // 2):
            i, j = order[2 * k], order[2 * k + 1]
            first_is_a = rng.random() < 0.5
            plan.append((round_no, i, j, first_is_a))
    return plan


def _average_ranks(values: Sequence[float]) -> list[float]:
    """升序 1-based 排名，完全相等的值取平均名次。

    @param values 待排名的数值序列
    @return 与 values 等长、按原顺序对齐的名次列表
    """
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    pos = 0
    while pos < n:
        end = pos
        while end + 1 < n and values[order[end + 1]] == values[order[pos]]:
            end += 1
        avg = (pos + end) / 2.0 + 1.0
        for k in range(pos, end + 1):
            ranks[order[k]] = avg
        pos = end + 1
    return ranks


def _percentile_scores(values: Sequence[float]) -> list[float]:
    """百分位归一化 score = (rank-1)/(N-1)，按升序平均名次计算。

    @param values 待归一化的数值序列
    @return 归一化到 [0, 1] 的分数列表；N == 1 时返回 [0.5]，空序列返回 []
    """
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.5]
    return [(rank - 1.0) / (n - 1.0) for rank in _average_ranks(values)]


def _weighted_aggregate(criteria: Sequence["Criterion"],
                        scores: Mapping[str, float | None]) -> float | None:
    """按 rubric 权重对非空准则分做加权平均。

    @param criteria 生效 rubric 的准则列表（权重来源）
    @param scores 准则 key → 归一化分（None 表示该准则未打分）
    @return Sum(w_i * s_i) / Sum(w_i)，全部为空时返回 None
    """
    num = 0.0
    den = 0.0
    for crit in criteria:
        s = scores.get(crit.key)
        if s is None:
            continue
        num += crit.weight * s
        den += crit.weight
    if den == 0.0:
        return None
    return num / den


def _top_ratio_selection(scored: Sequence[tuple[str, float]],
                         top_ratio: float) -> tuple[set[str], dict[str, int]]:
    """top_ratio 选择：按 (聚合分降序, id 升序) 保留前 ceil(top_ratio * N) 条。

    未打分记录不在 scored 内——它们不占配额位（spec 3.4.3）。

    @param scored (record_id, 聚合分) 序列，只含已打分记录
    @param top_ratio 保留比例（0, 1]
    @return (保留的 record_id 集合, record_id → 1-based 名次)
    """
    ranked = sorted(scored, key=lambda t: (-t[1], t[0]))
    quota = math.ceil(top_ratio * len(ranked))
    ranks = {rec_id: pos + 1 for pos, (rec_id, _) in enumerate(ranked)}
    kept = {rec_id for rec_id, _ in ranked[:quota]}
    return kept, ranks


def _pointwise_label(description: str) -> str:
    """从准则描述里取 pointwise 提示词用的短标签。

    @param description 准则描述（约定形如 "标签：详细说明"）
    @return 第一个 '：' 之前的部分；无冒号时返回整条描述
    """
    return description.split("：", 1)[0]


def _criterion_percentiles(log_theta: Sequence[float],
                           unscored: set[int]) -> list[float | None]:
    """spec 3.4.3 归一化：对批内全部 log θ 升序排名（CONTRACTS.md §7.3），再把未打分
    记录自身的分置空——它们的排除绝不能挪动其他记录的名次。

    @param log_theta 池内全部记录的 BT log θ
    @param unscored 未打分记录的下标集合
    @return 与 log_theta 等长的分数列表，未打分位置为 None
    """
    pct = _percentile_scores([float(v) for v in log_theta])
    return [None if k in unscored else pct[k] for k in range(len(pct))]


def _classify_call_error(exc: Exception) -> tuple[str, bool]:
    """把一次判定调用的失败（非 schema 不合法裁决）映射到 §7.6 错误分类。

    M9 重试耗尽 = 记录级 provider_retryable_exhausted；M9 认证/4xx = 运行级
    provider_fatal（obslog 会把该 kind 以 ERROR 级镜像到 stderr）；其余视为不变式破损。
    v1.11（V27①）：预算词汇优先匹配——分类不精确会落进 internal_error，破坏 §3.5 的
    归因与 overflow_records 计数。

    @param exc 判定调用抛出的异常
    @return (StageError.kind, 是否可重试)
    """
    kind = budget.classify_stage_error(exc)
    if kind is not None:
        return kind, False
    if isinstance(exc, ProviderRetryableError):
        return ErrorKind.PROVIDER_RETRYABLE_EXHAUSTED.value, True
    if isinstance(exc, ProviderFatalError):
        return ErrorKind.PROVIDER_FATAL.value, False
    return ErrorKind.INTERNAL_ERROR.value, False


def _aggregate_of(item: PipelineItem) -> float | None:
    """读取信封的聚合分。

    @param item 流水线信封
    @return 聚合分；尚未写入聚合项时返回 None
    """
    qs = item.scores.get(AGGREGATE_KEY)
    return qs.score if qs is not None else None


def _pooled(payload: dict, pool: str | None) -> dict:
    """v1.7（R16）：classify 开启时给事件 payload 追加 pool 归属字段。

    @param payload 待补充的事件 payload（就地修改）
    @param pool 池名；None = 匿名池，保持 v1.7 之前的扁平字节形
    @return 同一个 payload 对象，便于链式传参
    """
    if pool is not None:
        payload["pool"] = pool
    return payload


# ── v1.11 上下文预算装填（spec 3.4.3 上下文预算装填 行、§3.3③④⑤） ───────────

_TREE_MARKER_RE = re.compile(r"^…\(truncated (\d+) nodes\)$")


@dataclass
class _CallFit:
    """一次判定调用在已声明预算下的记录侧装填状态。

    quality 按 (比较, 评委) 构造：预算取自「本评委」的 profile（V25② 与 verify 的
    min-over-panel 相对）——record_budget = input_budget(评委) − est(静态系统侧：
    pairwise/pointwise 系统文本、两条消息信封、profile 走结构化输出时的 schema)
    − 图片数 × 标定后的单图成本（UI 成对：×2 在两侧切分「之前」计入，§3.3④）。
    """
    record_budget: int      # 记录侧可用预算（token），已扣掉系统侧与图片成本
    sides: int = 1          # 记录侧切分份数：2 = pairwise 两半，1 = pointwise 单槽
    tighten: int = 1        # V20 反应式降档在文本份额上的除数：1 = 正常，2 = 唯一一次收紧重试
    truncations: int = 0    # 本次装填触发的截断次数（累加到 budget.truncations.quality）
    overflow: bool = False  # 槽位不可裁剪下限仍超出份额（V10 最小语义单元装不下，调用方处置）

    @property
    def side_share(self) -> int:
        """@return 单个记录槽位可用的 token 份额（记录侧预算按份数与收紧档均分）"""
        return self.record_budget // (self.sides * self.tighten)


class _Attempt:
    """一次 LLM 调用的装填与降档重试状态（V20：至多一次收紧重试）。

    A7/§7.8 的「恰好一次」熔断喂食由 budget.feed_reactive_terminal 统一实现（common 层，
    M8 schema_engine 共用同一个 _breaker_fed 鸭子标记）；本类只负责保存触发降档的那次
    反应式异常，供终态时补喂。
    """

    def __init__(self, base: _CallFit | None):
        """构造重试状态。

        @param base 基准装填状态；None = 该 profile 未声明预算（预算关闭，v1.10 逐字节路径）
        """
        self.base = base                                       # 基准装填状态（每次尝试的模板）
        self.pending = base                                    # 下次尝试使用的装填状态
        self.degraded = False                                  # 唯一一次收紧重试是否已用掉
        self.overflow_exc: ContextOverflowError | None = None  # 触发降档的反应式溢出异常

    def next_fit(self) -> _CallFit | None:
        """取本次尝试的装填状态副本——装填过程会就地累计 truncations / overflow。

        @return 装填状态副本；预算关闭时返回 None
        """
        return replace(self.pending) if self.pending is not None else None

    def degrade(self, ctx: "RunContext", exc: ContextOverflowError) -> bool:
        """V20 降档：把记录侧文本份额收紧一档并记账，供调用方重发一次。

        @param ctx 运行上下文（降档计数落在 budget.degrade_retries）
        @param exc 触发本次降档的反应式溢出异常，终态时用于补喂熔断器
        @return True = 已收紧、调用方应重试；False = 预算关闭或额度用尽，调用方走终态
        """
        if self.base is None or self.degraded:
            return False
        self.degraded = True
        self.overflow_exc = exc
        ctx.metrics.count("budget.degrade_retries")
        self.pending = replace(self.base, tighten=2)
        return True


def _fit_tree_text(rendered: str, budget_tokens: int) -> tuple[str, bool]:
    """§3.3③ UI 控件树序列化文本的动态封顶。

    渲染结果（已在 input.ui_tree_max_chars 绝对上限之下）用 est_text 复核；超出份额时
    从尾部丢 NODE 行，并以 serialize 家族的标记 "…(truncated N nodes)" 收尾——N 会累加到
    已有标记的计数上。est_text 对前缀单调，故用二分求最大可保留行数。

    @param rendered 已序列化的控件树文本
    @param budget_tokens 该文本可用的 token 份额
    @return (裁剪后的文本, 是否发生裁剪)
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
        """@param keep 保留的 NODE 行数
        @return 保留前 keep 行并追加截断标记后的候选文本"""
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


def _violation_summary(exc: SchemaViolation) -> str:
    """把 SchemaViolation 渲染成不含数据内容的错误事件消息。

    exc.errors 是 '<json-pointer>: <描述>' 字符串，描述部分嵌有 LLM 输出的实例值——可能
    引用记录内容，而错误事件的 message 会被镜像到 stderr 运行日志，后者绝不能携带数据内容
    （spec 7.1、CONTRACTS.md §8.4）。这里只保留 JSON Pointer（schema 定义的键 / 数组下标）。

    @param exc schema 校验失败异常
    @return 形如 "N violation(s) at <pointers>" 的脱敏摘要
    """
    pointers = ", ".join(
        dict.fromkeys(v.split(":", 1)[0] or "<root>" for v in exc.errors))
    return f"{len(exc.errors)} violation(s) at {pointers}"


# ── v1.8 序列渲染（spec 3.4.3 sequence 行、CONTRACTS §10.2/§10.3） ────────────
# 算子模块之间互不依赖（spec §2.2）：annotate 自带同格式的步骤行模板，这里是 M4 的副本。

_MEMBER_DIGEST_MAX_CHARS = 400   # 逐成员 frame_digest 上限（segment.digest_max_chars 默认值）
_FALLBACK_STEP_SUFFIX = "（摘取兜底）"


def _step_line(transition: Transition) -> str:
    """渲染一行 [步骤序列]，格式为 §10.1 冻结形
    `{index}. {action_type}（对象: {target|—}；值: {value|—}）{description}`。

    target/value 为空时渲染成 "—"。兜底步骤（Transition.detail.kind ==
    "extraction_invalid"）追加（摘取兜底）后缀——与 LLM 确认的 "other" 分开列出，避免兜底
    噪音污染连贯性锚点（S16；M5 annotate 渲染时不带该后缀）。v1.9（T14）：线索接缝占位步骤
    （detail.kind == "thread_seam"）追加对应的「（线索接缝：被 X 打断）」后缀——没有它，
    trajectory rubric 的 noise_residue / coherence 准则会把接缝读成噪音残留或无解释跳变。

    @param transition 一条动作摘取结果
    @return 单行步骤文本
    """
    action = transition.action
    target = action.get("target")
    value = action.get("value")
    line = (f"{transition.index}. {action.get('action_type')}"
            f"（对象: {'—' if target is None else target}；"
            f"值: {'—' if value is None else value}）"
            f"{action.get('description')}")
    if transition.detail.get("kind") == "extraction_invalid":
        line += _FALLBACK_STEP_SUFFIX
    elif transition.detail.get("kind") == "thread_seam":
        names = "、".join(transition.detail.get("interrupted_by") or ())
        line += f"（线索接缝：被{names}打断）"
    return line


def _member_digest_lines(members: Sequence[Record], max_total_chars: int) -> list[str]:
    """渲染 [成员帧摘要] 各行——逐成员 `{m}. {frame_digest(member, 400)}`（m 从 1 起，按成员序）。

    总长受 max_total_chars（input.ui_tree_max_chars）约束：首行与末行「始终」保留；中间条目
    整条丢弃，并就地替换成一行 `…(truncated N members)` 标记（serialize/§10.8 的截断约定）。

    @param members episode 的成员帧记录
    @param max_total_chars 摘要块的总字符上限
    @return 摘要行列表
    """
    lines = [f"{m}. {frame_digest(member, _MEMBER_DIGEST_MAX_CHARS)}"
             for m, member in enumerate(members, start=1)]
    if len(lines) <= 2 or len("\n".join(lines)) <= max_total_chars:
        return lines
    last = lines[-1]
    keep = 1                 # 即便下限超出预算，首行也保留
    for k in range(len(lines) - 2, 0, -1):
        marker = f"…(truncated {len(lines) - k - 1} members)"
        if len("\n".join(lines[:k] + [marker, last])) <= max_total_chars:
            keep = k
            break
    marker = f"…(truncated {len(lines) - keep - 1} members)"
    return lines[:keep] + [marker, last]


# ── 提示词装配（CONTRACTS.md §10.2 / §10.3，中文逐字节冻结） ──────────────────

def _pairwise_system_text(criteria: Sequence["Criterion"], with_reason: bool) -> str:
    """装配成对比较的系统消息文本（§10.2 冻结形）。

    @param criteria 本次调用覆盖的准则组
    @param with_reason 是否要求裁决附带一句话理由
    @return 系统消息文本
    """
    lines = ["你将对两条记录进行成对质量比较。准则如下："]
    for crit in criteria:
        lines.append(f"- {crit.key}: {crit.description}")
        lines.append(f"  {crit.pairwise_prompt}")
    lines.append("对每条准则给出裁决。输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：")
    if with_reason:
        lines.append('{"judgments": [{"criterion": <准则 key>, "winner": "A"|"B"|"tie", '
                     '"reason": <一句话理由>}]}')
    else:
        lines.append('{"judgments": [{"criterion": <准则 key>, "winner": "A"|"B"|"tie"}]}')
    return "\n".join(lines)


def _pointwise_system_text(criterion: "Criterion") -> str:
    """装配逐条打分的系统消息文本（§10.3 冻结形）。

    @param criterion 本次调用的单条准则
    @return 系统消息文本
    """
    label = _pointwise_label(criterion.description)
    lines = [f"按以下 0–5 加性量表为记录的 {criterion.key}（{label}）打分，"
             "先给两句理由再给整数分："]
    lines.extend(criterion.pointwise_levels)
    lines.append('输出 JSON：{"scores": [{"criterion": <准则 key>, "reason": <两句理由>, '
                 '"score": 0..5}]}')
    return "\n".join(lines)


@dataclass(frozen=True)
class _Comparison:
    """一次成对比较的记录侧输入：两条被比较的记录及各自的步骤序列。"""
    rec_a: Record                                          # 呈现序 A 位的记录
    rec_b: Record                                          # 呈现序 B 位的记录
    transitions_a: tuple[Transition, ...] | None = None    # A 位的步骤序列；单条记录为 None
    transitions_b: tuple[Transition, ...] | None = None    # B 位的步骤序列；单条记录为 None


def _record_parts(record: Record, label: str, ui_tree_max_chars: int,
                  transitions: tuple[Transition, ...] | None = None,
                  fit: _CallFit | None = None) -> list[Part]:
    """渲染一条记录在提示词里占的内容槽位。

    text 模态：一条 '[label] text' 文本；UI 模态：§10.2 的三段（截图头行 + 图片 + 控件树）。
    v1.8 序列记录（record.kind == "sequence"，先于模态判断）渲染成「一条纯文本」——
    `[{label}·操作序列]` 头行，随后是 §10.2/§10.3 的 [步骤序列]（transitions 为 None 时整段
    省略）+ [成员帧摘要]——即便在 UI 模态下也不带图片部件（rule-34 视觉豁免，S30）。

    v1.11（fit 非 None，spec 3.4.3 v1.11 行）：本槽位按 fit.side_share 装填——UI 控件树走
    §3.3③ 动态封顶，[步骤序列] 走 §3.3⑤ 边缘裁剪；记录正文与成员摘要块（已按字符封顶）
    不属于可裁剪类，下限仍超份额时置 fit.overflow（V10）。fit=None 即 v1.10 逐字节路径。

    @param record 待渲染的记录
    @param label 槽位标签（如 "记录 A" / "记录内容"）
    @param ui_tree_max_chars 控件树 / 成员摘要块的字符上限（input.ui_tree_max_chars）
    @param transitions 序列记录的步骤序列；None 表示不渲染 [步骤序列]
    @param fit 装填状态；None = 预算关闭
    @return 该槽位的消息部件列表
    """
    if record.kind == "sequence":
        return _sequence_parts(record, label, ui_tree_max_chars, transitions, fit)
    if record.modality == "text":
        parts = [Part(kind="text", text=f"[{label}] {record.text}")]
        if fit is not None and budget.est_text(parts[0].text) > fit.side_share:
            fit.overflow = True                  # 记录正文不是可裁剪类
        return parts
    return _ui_parts(record, label, ui_tree_max_chars, fit)


def _sequence_parts(record: Record, label: str, ui_tree_max_chars: int,
                    transitions: tuple[Transition, ...] | None,
                    fit: _CallFit | None) -> list[Part]:
    """渲染序列记录的纯文本槽位（[步骤序列] + [成员帧摘要]）。

    @param record 序列记录（record.kind == "sequence"）
    @param label 槽位标签
    @param ui_tree_max_chars 成员摘要块的字符上限
    @param transitions 步骤序列；None 表示整段省略
    @param fit 装填状态；None = 预算关闭
    @return 只含一条文本部件的列表
    """
    lines = [f"[{label}·操作序列]"]
    if transitions is not None:
        lines.append("[步骤序列]")
        step_body = "\n".join(_step_line(t) for t in transitions)
        if fit is not None:
            step_body = _fit_step_body(step_body, label, record, ui_tree_max_chars, fit)
        lines.extend(step_body.split("\n"))
    lines.append("[成员帧摘要]")
    lines.extend(_member_digest_lines(record.members, ui_tree_max_chars))
    parts = [Part(kind="text", text="\n".join(lines))]
    if fit is not None and budget.est_text(parts[0].text) > fit.side_share:
        fit.overflow = True
    return parts


def _fit_step_body(step_body: str, label: str, record: Record,
                   ui_tree_max_chars: int, fit: _CallFit) -> str:
    """§3.3⑤：步骤块拿走槽位固定部分之外的余量，超出则做边缘裁剪。

    成员摘要块是裁决兜底证据，最后才让位（图片先于文本、文本中摘要最珍贵——§3.3⑥ 同族理由）。

    @param step_body 已渲染的步骤行整体文本
    @param label 槽位标签（参与固定部分估算）
    @param record 序列记录（成员摘要块来源）
    @param ui_tree_max_chars 成员摘要块的字符上限
    @param fit 装填状态，裁剪时就地累加 truncations
    @return 裁剪后的步骤块文本
    """
    fixed = "\n".join([f"[{label}·操作序列]", "[步骤序列]", "[成员帧摘要]"]
                      + _member_digest_lines(record.members, ui_tree_max_chars))
    step_share = fit.side_share - budget.est_text(fixed)
    if budget.est_text(step_body) > step_share:
        step_body = budget.fit_text(step_body, max(0, step_share), keep="edges")
        fit.truncations += 1
    return step_body


def _ui_parts(record: Record, label: str, ui_tree_max_chars: int,
              fit: _CallFit | None) -> list[Part]:
    """渲染单帧 UI 记录的 §10.2 三段槽位（截图头行 + 图片 + 控件树）。

    @param record UI 模态的单帧记录
    @param label 槽位标签
    @param ui_tree_max_chars 控件树序列化的字符上限
    @param fit 装填状态；None = 预算关闭
    @return 三条消息部件
    """
    tree = record.ui_tree.serialize(max_chars=ui_tree_max_chars) if record.ui_tree else ""
    head = f"[{label} 屏幕截图]"
    tree_label = f"[{label} UI 控件树]\n"
    if fit is not None:
        tree_share = (fit.side_share - budget.est_text(head)
                      - budget.est_text(tree_label))
        tree, trimmed = _fit_tree_text(tree, max(0, tree_share))
        if trimmed:
            fit.truncations += 1
    parts = [Part(kind="text", text=head),
             Part(kind="image", image=record.image),
             Part(kind="text", text=f"{tree_label}{tree}")]
    if fit is not None and (budget.est_text(head)
                            + budget.est_text(parts[2].text)) > fit.side_share:
        fit.overflow = True
    return parts


def _build_pairwise_prompt(pair: _Comparison, criteria: Sequence["Criterion"],
                           with_reason: bool, ui_tree_max_chars: int,
                           fit: _CallFit | None = None) -> PromptBundle:
    """装配一次成对比较的 PromptBundle（§10.2 冻结形）。

    @param pair 被比较的两条记录及各自的步骤序列
    @param criteria 本次调用覆盖的准则组
    @param with_reason 是否要求裁决附带理由
    @param ui_tree_max_chars 控件树 / 成员摘要块的字符上限
    @param fit 装填状态；None = 预算关闭，与 v1.10 逐字节一致
    @return 系统消息 + 用户消息组成的提示词
    """
    rec_a, rec_b = pair.rec_a, pair.rec_b
    system = Message(role="system", parts=(
        Part(kind="text", text=_pairwise_system_text(criteria, with_reason)),))

    if rec_a.kind == "sequence" or rec_b.kind == "sequence":
        # v1.8：序列子段落落在各自的 [记录 X] 内容槽位里；文本模态的序列不能走纯文本
        # 快路径（record.text 为 None）。
        user_parts = (_record_parts(rec_a, "记录 A", ui_tree_max_chars,
                                    pair.transitions_a, fit=fit)
                      + _record_parts(rec_b, "记录 B", ui_tree_max_chars,
                                      pair.transitions_b, fit=fit))
    elif rec_a.modality == "text":
        user_parts = [Part(kind="text",
                           text=f"[记录 A] {rec_a.text}\n[记录 B] {rec_b.text}")]
        if fit is not None and budget.est_text(
                user_parts[0].text) > 2 * fit.side_share:
            fit.overflow = True                  # 合并槽位 = 两半之和（§3.3④）
    else:
        user_parts = (_record_parts(rec_a, "记录 A", ui_tree_max_chars, fit=fit)
                      + _record_parts(rec_b, "记录 B", ui_tree_max_chars, fit=fit))
    user = Message(role="user", parts=tuple(user_parts))
    return PromptBundle(messages=(system, user))


def _build_pointwise_prompt(record: Record, criterion: "Criterion",
                            ui_tree_max_chars: int,
                            transitions: tuple[Transition, ...] | None = None,
                            fit: _CallFit | None = None) -> PromptBundle:
    """装配一次逐条打分的 PromptBundle（§10.3 冻结形）。

    @param record 被打分的记录
    @param criterion 本次调用的单条准则
    @param ui_tree_max_chars 控件树 / 成员摘要块的字符上限
    @param transitions 序列记录的步骤序列；None 表示不渲染 [步骤序列]
    @param fit 装填状态；None = 预算关闭
    @return 系统消息 + 用户消息组成的提示词
    """
    system = Message(role="system", parts=(
        Part(kind="text", text=_pointwise_system_text(criterion)),))
    user = Message(role="user",
                   parts=tuple(_record_parts(record, "记录内容", ui_tree_max_chars,
                                             transitions, fit=fit)))
    return PromptBundle(messages=(system, user))


def _judgments_by_key(obj: Mapping) -> dict[str, Mapping]:
    """按准则 key 归并裁决数组，重复项保留首个。

    @param obj 已通过 schema 校验的裁决对象
    @return 准则 key → 裁决条目
    """
    by_key: dict[str, Mapping] = {}
    for entry in obj.get("judgments", []):
        by_key.setdefault(entry["criterion"], entry)
    return by_key


def _judgment_verdicts(by_key: Mapping[str, Mapping], keys: Sequence[str],
                       a_idx: int, b_idx: int) -> dict[str, int | str | None]:
    """把 A/B 胜者映射回记录下标。

    @param by_key 准则 key → 裁决条目
    @param keys 本次调用覆盖的准则 key 列表
    @param a_idx 呈现序 A 位对应的记录下标
    @param b_idx 呈现序 B 位对应的记录下标
    @return 准则 key → 胜者下标 | 'tie'；模型漏答的准则记为 tie
    """
    verdicts: dict[str, int | str | None] = {}
    for key in keys:
        entry = by_key.get(key)
        winner = entry["winner"] if entry else "tie"  # 未覆盖的准则记为 tie
        if winner == "A":
            verdicts[key] = a_idx
        elif winner == "B":
            verdicts[key] = b_idx
        else:
            verdicts[key] = "tie"
    return verdicts


# ── 算子本体 ──────────────────────────────────────────────────────────────────

@dataclass
class _Pool:
    """一个打分池（spec 3.4.3 按类分池）：批内 active 项的一个子集，以及它生效的
    (QualityConfig, criteria)。pool=None 是匿名池——classify 关闭（= 整批），或 active 项
    没带分类时的防御性兜底——保持 v1.7 之前的字节形：全局配置、扁平计数键、payload 无
    "pool" 字段。
    """
    pool: str | None                                       # 池名（类名）；None = 匿名池
    items: list[PipelineItem]                              # 本池的 active 信封
    q: "QualityConfig"                                     # 本池生效的质量配置
    criteria: tuple["Criterion", ...]                      # 本池生效 rubric 的准则
    plan: list[tuple[int, int, int, bool]] | None = None   # 预抽的配对计划；仅 >= 2 条的成对池
    results: list = field(default_factory=list)            # 第二阶段的成对调用结果
    dead: bool = False                                     # 已被内部错误毒化（R15）

    @property
    def mode(self) -> Literal["pairwise_bt", "pointwise"]:
        """@return 本池的打分模式（QualityScore.mode 取值）"""
        return "pairwise_bt" if self.q.mode == "pairwise" else "pointwise"


@dataclass
class _CriterionTally:
    """单条准则在池内全部比较上的合成结果与统计。"""
    entries: list[tuple[int, int, float]]  # BT 拟合输入：(胜者下标, 负者下标, 权重)
    comp_count: list[int]                  # 逐记录参与的比较数
    wins: list[int]                        # 逐记录的胜场数
    ties: list[int]                        # 逐记录的平局数
    success: list[int]                     # 逐记录「至少一次判定成功」的比较数
    n_judged: int = 0                      # 至少有一次成功判定的比较数
    n_tie_judged: int = 0                  # 其中判定为平局的比较数


class QualityStage:
    """M4 质量打分算子：按池打分（pairwise BT / pointwise）、聚合、门控。"""

    name = "quality"

    def __init__(self, cfg: "ResolvedConfig"):
        """@param cfg 已解析的不可变运行配置"""
        self._cfg = cfg

    @property
    def cfg(self) -> "ResolvedConfig":
        """@return 当前 attempt 的程序视图配置；普通批次返回构造期配置。"""
        active = _ATTEMPT_CONFIG.get()
        return self._cfg if active is None else active  # type: ignore[return-value]

    async def run(self, batch: list[PipelineItem], ctx: "RunContext") -> list[PipelineItem]:
        """对一批信封打分并门控（三阶段：预抽配对 → 合并判定 → 逐池后处理）。

        @param batch 本批全部信封（只处理 status == "active" 的项）
        @param ctx 运行上下文
        @return 原 batch 对象（Stage 契约：只改状态，不增删元素）
        """
        items = [it for it in batch if it.status == "active"]
        if not items:
            return batch
        pools = self._partition(items, ctx)
        if pools is None:
            return batch
        self._predraw_plans(pools, ctx)
        await self._judge_pools(pools, ctx)
        self._finish_pools(pools, ctx)
        return batch

    async def run_attempt(
        self,
        request: DownstreamAttemptRequest,
    ) -> DownstreamAttemptResult:
        """执行一次 whole-set 质量 gate，延迟合并所有 dataset counters。

        @param request 当前 AttemptTransaction 与共享运行上下文。
        @return 接受状态、质量拒绝归因与 attempt-local counter delta。
        """
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
                            accepted=False, rejected_stage="quality",
                            dataset_counters=dict(counters))
            finally:
                _ATTEMPT_MODE.reset(token)
            self._assert_no_provider_fatal(items)
            accepted = all(item.status == "active" for item in items)
            return DownstreamAttemptResult(
                accepted=accepted,
                rejected_stage=None if accepted else "quality",
                dataset_counters=dict(counters),
            )
        finally:
            _ATTEMPT_CONFIG.reset(config_token)

    @staticmethod
    def _assert_no_provider_fatal(items: list[PipelineItem]) -> None:
        """检测误用普通 Stage 隔离入口产生的 provider-fatal item error。

        @param items 当前 attempt 的唯一信封真值。
        @return None。
        @raises RuntimeError 发现协议破坏。
        """
        if any(error.kind == ErrorKind.PROVIDER_FATAL.value
               for item in items for error in item.errors):
            _logger.error("generation_downstream_contract: isolated provider fatal")
            raise InternalError("generation_downstream_contract: isolated provider fatal")

    # ── 分池与阶段编排（v1.7 spec 3.4.3、R13/R15） ──────────────────────────

    def _partition(self, items: list[PipelineItem],
                   ctx: "RunContext") -> list["_Pool"] | None:
        """把 active 项划分成打分池。

        @param items 本批 active 信封
        @param ctx 运行上下文
        @return 池列表；划分本身破损时返回 None（该批记录已整体失败）
        """
        try:
            return self._build_pools(items)
        except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as exc:
            if _ATTEMPT_MODE.get():
                raise
            # 划分破损（例如标签不在 class_views 里——M1/M13 不变式被破坏）发生在任何池
            # 之前，因此沿用 v1.7 之前的批级兜底：失败的是这批记录，不是整个运行（契约④）。
            _logger.error("quality pool partition failed: exc=%s", type(exc).__name__,
                          extra={"stage": self.name, "batch": ctx.batch_no})
            self._fail_pool(_Pool(pool=None, items=items, q=self.cfg.quality,
                                  criteria=self.cfg.rubric.criteria), ctx, exc)
            return None

    def _predraw_plans(self, pools: list["_Pool"], ctx: "RunContext") -> None:
        """第一阶段（R13）：按类名字典序「同步」预抽每个成对池的配对与呈现计划。

        这是本算子唯一的 ctx.rng 消费点，故抽签序列只由池顺序决定，绝不受调用调度影响。
        单条池不抽签（N=1 规则，无调用）。

        @param pools 池列表（已按类名字典序）
        @param ctx 运行上下文（提供 ctx.rng）
        """
        for pool in pools:
            if pool.mode != "pairwise_bt" or len(pool.items) <= 1:
                continue
            try:
                pool.plan = _pairing_plan(len(pool.items), pool.q.rounds, ctx.rng)
            except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as exc:
                if _ATTEMPT_MODE.get():
                    raise
                _logger.error("quality pairing pre-draw failed: pool=%s exc=%s",
                              pool.pool, type(exc).__name__,
                              extra={"stage": self.name, "batch": ctx.batch_no})
                self._fail_pool(pool, ctx, exc)

    async def _judge_pools(self, pools: list["_Pool"], ctx: "RunContext") -> None:
        """第二阶段：经共享 TaskExecutor 执行纯叶调用，再按声明序归并。

        @param pools 池列表
        @param ctx 运行上下文
        """
        plans = self._collect_calls(pools, ctx)
        specs = tuple(
            TaskSpec(
                task_id=f"{ctx.task_namespace}:quality:{plan.ordinal}",
                declaration_key=(ctx.batch_no, 5, plan.ordinal),
                stage=self.name,
                resource_key=("llm", plan.profile),
                operation=lambda plan=plan: self._run_call(plan, ctx),
            )
            for plan in plans
        )
        outcomes = await ctx.tasks.run_group(TaskGroupRequest(specs))
        for plan, outcome in zip(plans, outcomes, strict=True):
            self._reduce_call(pools, plan, outcome, ctx)

    def _collect_calls(self, pools: list["_Pool"],
                       ctx: "RunContext") -> list[QualityCall]:
        """按池与调用声明序冻结全部纯叶计划。

        @param pools 池列表
        @param ctx 运行上下文
        @return 按池、比较或记录、评委、呈现序与准则展开的调用计划
        """
        plans: list[QualityCall] = []
        for pool_ordinal, pool in enumerate(pools):
            if pool.dead:
                continue
            try:
                calls = (
                    self._pairwise_calls(pool, pool_ordinal, len(plans))
                    if pool.mode == "pairwise_bt"
                    else self._pointwise_calls(pool, pool_ordinal, len(plans))
                )
            except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as exc:
                if _ATTEMPT_MODE.get():
                    raise
                _logger.error("quality call assembly failed: pool=%s exc=%s",
                              pool.pool, type(exc).__name__,
                              extra={"stage": self.name, "batch": ctx.batch_no})
                self._fail_pool(pool, ctx, exc)
                continue
            plans.extend(calls)
        return plans

    async def _run_call(self, plan: QualityCall,
                        ctx: "RunContext") -> QualityCallOutcome:
        """执行一个不写共享业务对象、dataset 事件或错误的叶调用。

        @param plan 冻结调用计划
        @param ctx 运行上下文
        @return 成功结果或有类型的普通失败
        """
        try:
            if isinstance(plan, PairwiseQualityCall):
                return await self._run_pairwise_call(plan, ctx)
            return await self._run_pointwise_call(plan, ctx)
        except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as exc:
            if _ATTEMPT_MODE.get():
                raise
            _logger.error("quality judging leaf failed: ordinal=%d exc=%s",
                          plan.ordinal, type(exc).__name__,
                          extra={"stage": self.name, "batch": ctx.batch_no})
            return QualityCallFailure(exc, "pool")

    def _reduce_call(self, pools: list["_Pool"], plan: QualityCall,
                     outcome: QualityCallOutcome, ctx: "RunContext") -> None:
        """按 TaskGroupRequest 声明序提交一个叶结果。

        @param pools 当前批冻结池表
        @param plan 当前调用计划
        @param outcome 当前纯叶结果
        @param ctx 运行上下文
        """
        pool = pools[plan.pool_ordinal]
        if isinstance(outcome, QualityCallFailure) and outcome.scope == "pool":
            if not pool.dead:
                self._fail_pool(pool, ctx, outcome.error)
            return
        if isinstance(plan, PairwiseQualityCall):
            self._reduce_pairwise_call(pool, plan, outcome, ctx)
            return
        self._reduce_pointwise_call(pool, plan, outcome, ctx)

    def _finish_pools(self, pools: list["_Pool"], ctx: "RunContext") -> None:
        """第三阶段：逐池后处理（裁决合成 / BT 拟合 / 百分位归一化 / 聚合）与门控。

        top_ratio 的配额基数天然就是本池的已打分幸存者。

        @param pools 池列表
        @param ctx 运行上下文
        """
        for pool in pools:
            if pool.dead:
                continue
            try:
                if pool.mode == "pairwise_bt":
                    self._pairwise_finish(pool, ctx)
                else:
                    self._set_aggregates(pool.items, "pointwise", pool.criteria)
                self._apply_gate(pool.items, ctx, pool.q, pool.pool)
            except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as exc:
                if _ATTEMPT_MODE.get():
                    raise
                _logger.error("quality pool post-processing failed: pool=%s exc=%s",
                              pool.pool, type(exc).__name__,
                              extra={"stage": self.name, "batch": ctx.batch_no})
                self._fail_pool(pool, ctx, exc)

    # ── 分池管道（v1.7 spec 3.4.3） ─────────────────────────────────────────

    def _build_pools(self, items: list[PipelineItem]) -> list["_Pool"]:
        """把 active 项划分成打分池。

        classify 关闭 → 唯一匿名池 = 整批（v1.7 之前的行为，零变更锚点）。classify 开启 →
        按分类标签一类一池，按类名字典序排列，各自采用 class_views[label] 的生效
        (QualityConfig, Rubric)；没有分类的 active 项（M13 不产生，防御性）归入匿名池，
        排在具名池之前。

        @param items 本批 active 信封
        @return 池列表
        """
        if not any(item.classification is not None for item in items):
            return [_Pool(pool=None, items=list(items), q=self.cfg.quality,
                          criteria=self.cfg.rubric.criteria)]
        grouped: dict[str | None, list[PipelineItem]] = {}
        for it in items:
            label = it.classification.label if it.classification is not None else None
            grouped.setdefault(label, []).append(it)
        pools: list[_Pool] = []
        if None in grouped:
            pools.append(_Pool(pool=None, items=grouped[None], q=self.cfg.quality,
                               criteria=self.cfg.rubric.criteria))
        for label in sorted(k for k in grouped if k is not None):
            view = self.cfg.class_views[label]
            pools.append(_Pool(pool=label, items=grouped[label], q=view.quality,
                               criteria=view.rubric.criteria))
        return pools

    def _fail_pool(self, pool: "_Pool", ctx: "RunContext", exc: Exception) -> None:
        """池级隔离（R15）：把 v1.7 之前的批级内部错误兜底缩到「一个池」——本池仍 active
        的项失败，其他池照常推进。

        @param pool 被毒化的池
        @param ctx 运行上下文
        @param exc 触发毒化的异常（其文本进入 StageError.message）
        """
        pool.dead = True
        for it in pool.items:
            if it.status == "active":
                err = StageError(stage=self.name, kind=ErrorKind.INTERNAL_ERROR.value,
                                 message=f"quality stage internal error: {exc}",
                                 retryable=False)
                it.errors.append(err)
                it.status = "failed"
                self._emit_error(ctx, (it.record.id,), err)

    # ── 共用管道 ────────────────────────────────────────────────────────────

    def _reasons_effective(self) -> bool:
        """判断本次运行是否要求裁决/打分附带理由。

        @return judgment_reasons 的生效值；"auto" 时取决于 quality 追踪通道是否开启
        """
        jr = self.cfg.quality.judgment_reasons
        if jr == "auto":
            return self.cfg.trace.enabled and "quality" in self.cfg.trace.channels
        return bool(jr)

    def _excerpt_payload(self, records: Sequence[Record]) -> dict | None:
        """为 excerpt/full 两档 trace.content 生成 `excerpt` 附加字段（§8.3）。

        v1.8 序列分支：episode 的摘录 = 首个成员 frame_digest 的前 200 字符（成员摘要渲染的
        开头，§7.3）。

        @param records 参与本次调用的记录
        @return record_id → 摘录文本；追踪关闭或档位不含内容时返回 None
        """
        if not (self.cfg.trace.enabled and self.cfg.trace.content in ("excerpt", "full")):
            return None
        out: dict[str, str] = {}
        for rec in records:
            if rec.kind == "sequence":
                content = (frame_digest(rec.members[0], _MEMBER_DIGEST_MAX_CHARS)
                           if rec.members else "")
            elif rec.modality == "text":
                content = rec.text
            else:
                content = rec.ui_tree.serialize() if rec.ui_tree else ""
            out[rec.id] = (content or "")[:200]
        return out

    def _emit_error(self, ctx: "RunContext", record_ids: tuple[str, ...],
                    err: StageError) -> None:
        """发出 error 事件（obslog 依 payload.kind 决定 stderr 镜像级别）。

        @param ctx 运行上下文
        @param record_ids 涉及的记录 id
        @param err 已构造的阶段错误
        """
        ctx.metrics.event(_EV_ERROR, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=record_ids,
                          payload={"stage": err.stage, "kind": err.kind,
                                   "message": err.message, "retryable": err.retryable})

    def _record_judgment_failure(self, ctx: "RunContext", items: Sequence[PipelineItem],
                                 message: str) -> None:
        """M8 修复后裁决仍不合 schema（spec 3.4.3 裁决失败）：比较级失败，按平局计入
        （对 BT 中性），记录保持 active；这是唯一会递增 quality.judgment_failures
        （§7.5 rubric 诊断）的路径。

        @param ctx 运行上下文
        @param items 涉及的信封
        @param message 英文错误消息（已脱敏）
        """
        ctx.metrics.count(_COUNTER_JUDGMENT_FAILURES)
        err = StageError(stage=self.name, kind=ErrorKind.JUDGMENT_INVALID.value,
                         message=message, retryable=False)
        for it in items:
            it.errors.append(err)
        self._emit_error(ctx, tuple(it.record.id for it in items), err)

    def _record_call_failure(self, ctx: "RunContext", items: Sequence[PipelineItem],
                             exc: Exception, what: str) -> None:
        """判定调用的 provider / 内部失败。与「裁决不合 schema」不同，这不是 rubric 问题：
        按 spec 7.6，涉及记录直接失败（无平局兜底），且不递增 quality.judgment_failures。

        @param ctx 运行上下文
        @param items 涉及的信封
        @param exc 失败异常（决定 §7.6 kind 与可重试标记）
        @param what 英文失败场景描述，进入 StageError.message
        """
        kind, retryable = _classify_call_error(exc)
        err = StageError(stage=self.name, kind=kind,
                         message=f"{what} ({type(exc).__name__}): {exc}",
                         retryable=retryable)
        for it in items:
            it.errors.append(err)
            it.status = "failed"
        self._emit_error(ctx, tuple(it.record.id for it in items), err)

    # ── v1.11 预算管道（spec 3.4.3 v1.11 行） ───────────────────────────────

    def _call_fit(self, ctx: "RunContext",
                  request: QualityFitRequest) -> _CallFit | None:
        """按某个评委的 profile 预算构造记录侧装填状态。

        quality 按 (比较, 评委) 装填：每次调用各自贴合本评委的预算（V25② 对照）。schema 估算
        只在 profile 走结构化输出时计入；单帧 UI 记录各带一张图片（[记录 X 屏幕截图]）按标定
        成本计价——在两侧切分「之前」计入（§3.3④）；序列记录渲染无图（rule-34/S30）。
        每条记录占一个槽位，故切分份数 = len(records)。

        @param ctx 运行上下文（提供图片成本标定器）
        @param request profile、系统文本、Schema 与记录快照
        @return 装填状态；profile 缺失或未声明 context_window 时返回 None（预算关闭）
        """
        prof: "LLMProfile | None" = self.cfg.llm_profiles.get(request.profile)
        if prof is None or prof.context_window <= 0:
            return None
        static = budget.est_text(request.system_text) + 2 * budget.MSG_OVERHEAD_TOKENS
        if prof.supports_structured_output:
            schema = json.dumps(_thaw_json(request.schema), ensure_ascii=False)
            static += budget.est_text(schema)
        n_images = sum(1 for r in request.records
                       if r.kind != "sequence" and r.modality == "ui"
                       and r.image is not None)
        image_est = n_images * ctx.llm.calibrator.cost(prof.name) if n_images else 0
        return _CallFit(record_budget=budget.input_budget(prof) - static - image_est,
                        sides=len(request.records))

    @staticmethod
    def _precheck_failure(fit: _CallFit | None, attempt: _Attempt,
                          ctx: "RunContext", kind: str) -> QualityCallFailure | None:
        """结算一次装填结果，并把最小单元溢出冻结成纯失败。

        @param fit 本次尝试的装填状态
        @param attempt 本调用的降档状态
        @param ctx 运行上下文
        @param kind pairwise 或 pointwise
        @return 无溢出时为 None，否则为发送前失败
        """
        if fit is None:
            return None
        if fit.truncations:
            ctx.metrics.count("budget.truncations.quality", fit.truncations)
        if not fit.overflow:
            return None
        if attempt.overflow_exc is not None:
            budget.feed_reactive_terminal(attempt.overflow_exc, ctx.metrics)
        message = f"{kind} slot exceeds the record-side budget at the minimal unit"
        return QualityCallFailure(
            ContextOverflowError(message, phase="precheck"), "call", precheck=True,
        )

    @staticmethod
    def _call_failure(exc: Exception, attempt: _Attempt, ctx: "RunContext",
                      ordinal: int) -> QualityCallFailure | None:
        """把一次模型失败收敛为重试或纯结果，保留 sequence/control 边界。

        @param exc 模型调用异常
        @param attempt 本调用的降档状态
        @param ctx 运行上下文
        @param ordinal 调用声明序，仅用于无数据日志
        @return None 表示已降档应重试，否则为冻结调用失败
        """
        _logger.warning("quality scoring call failed: ordinal=%d exc=%s",
                        ordinal, type(exc).__name__,
                        extra={"stage": "quality", "batch": ctx.batch_no})
        if isinstance(exc, ContextOverflowError):
            if attempt.degrade(ctx, exc):
                return None
            budget.feed_reactive_terminal(exc, ctx.metrics)
        recoverable = (
            ProviderRetryableError,
            SchemaViolation,
            ContextOverflowError,
            OutputTruncatedError,
        )
        if _ATTEMPT_MODE.get() and not isinstance(exc, recoverable):
            raise exc
        return QualityCallFailure(exc, "call")

    def _overflow_tie(self, ctx: "RunContext", items: Sequence[PipelineItem],
                      message: str) -> None:
        """成对最小单元溢出的处置——沿用 judgment_invalid 的「粒度」先例（spec 3.4.3
        裁决失败 行）：该比较判为平局（对 BT 中性，outcomes 为 None），涉及记录保持 active
        且仍可打分——「每一次比较都溢出」的记录会落进既有的 on_unscored 家族。

        精确的 kind 随 StageError 与 error 事件下发（V27①）；quality.judgment_failures 仍是
        rubric 专属诊断（不递增），budget.overflow_records 只统计 rejects（§9.3）——这里不产生
        reject。注意：这是对 V10 在「两条记录」这一成对单元上的刻意粒度读法；单条记录的
        pointwise 单元走记录级 reject（_overflow_fail_record）。

        @param ctx 运行上下文
        @param items 涉及的两个信封
        @param message 英文错误消息
        """
        err = StageError(stage=self.name, kind=ErrorKind.CONTEXT_OVERFLOW.value,
                         message=message, retryable=False)
        for it in items:
            it.errors.append(err)
        self._emit_error(ctx, tuple(it.record.id for it in items), err)

    def _overflow_fail_record(self, ctx: "RunContext", item: PipelineItem,
                              message: str) -> None:
        """pointwise / 单记录的 V10 终态：记录级 failed → rejects，kind=context_overflow，
        计入 budget.overflow_records（spec §3.5 / 3.4.3 v1.11 行）。

        @param ctx 运行上下文
        @param item 涉及的信封
        @param message 英文错误消息
        """
        err = StageError(stage=self.name, kind=ErrorKind.CONTEXT_OVERFLOW.value,
                         message=message, retryable=False)
        item.errors.append(err)
        item.status = "failed"
        ctx.metrics.count("budget.overflow_records")
        self._emit_error(ctx, (item.record.id,), err)

    # ── pairwise 模式（按 R13 拆成 计划 / 派发 / 收尾 三段） ────────────────

    def _pairwise_calls(self, pool: "_Pool", pool_ordinal: int,
                        start_ordinal: int) -> list[PairwiseQualityCall]:
        """按池内预抽计划冻结成对判定调用。
        单条池不发起调用（其 N=1 规则在 _pairwise_finish 里生效）。
        @param pool 待判定的池（携带 items / q / criteria / plan）
        @param pool_ordinal 当前池声明序
        @param start_ordinal 当前池首个全批任务序号
        @return 按比较、评委、呈现序与准则组展开的冻结计划
        """
        if len(pool.items) == 1:
            return []
        q = pool.q
        with_reason = self._reasons_effective()
        judges: tuple[str, ...] = q.judges if q.judges else (q.llm,)
        orders = (False, True) if q.both_orders else (False,)
        if q.criteria_per_call == "all":
            crit_groups: list[tuple["Criterion", ...]] = [tuple(pool.criteria)]
        else:
            crit_groups = [(c,) for c in pool.criteria]

        calls: list[PairwiseQualityCall] = []
        for comp_idx, (_round_no, i, j, first_is_a) in enumerate(pool.plan):
            for judge in judges:
                for flipped in orders:
                    a_idx, b_idx = (i, j) if (first_is_a != flipped) else (j, i)
                    for group in crit_groups:
                        a_item = pool.items[a_idx]
                        b_item = pool.items[b_idx]
                        calls.append(PairwiseQualityCall(
                            ordinal=start_ordinal + len(calls),
                            pool_ordinal=pool_ordinal,
                            comparison_ordinal=comp_idx,
                            first_item_ordinal=i,
                            second_item_ordinal=j,
                            a_item_ordinal=a_idx,
                            b_item_ordinal=b_idx,
                            record_a=a_item.record,
                            record_b=b_item.record,
                            transitions_a=(tuple(a_item.transitions)
                                           if a_item.transitions is not None else None),
                            transitions_b=(tuple(b_item.transitions)
                                           if b_item.transitions is not None else None),
                            profile=judge,
                            flipped=flipped,
                            criteria=group,
                            with_reason=with_reason,
                            multi_judge=len(judges) > 1,
                            pool=pool.pool,
                        ))
        return calls

    def _pairwise_finish(self, pool: "_Pool", ctx: "RunContext") -> None:
        """收尾段：裁决合成、BT 拟合、池内百分位归一化、聚合。

        @param pool 已完成判定的池
        @param ctx 运行上下文
        """
        if len(pool.items) == 1:      # 单条池：不发判定调用，各准则分固定 0.5
            self._score_single(pool.items[0], pool.criteria)
            return
        results = self._index_results(pool.results)
        for crit in pool.criteria:
            self._score_criterion(pool, ctx, crit, results)
        self._set_aggregates(pool.items, "pairwise_bt", pool.criteria)

    @staticmethod
    def _score_single(item: PipelineItem, criteria: Sequence["Criterion"]) -> None:
        """单条池的 N=1 规则：每条准则与聚合分都固定 0.5。

        @param item 池内唯一的信封
        @param criteria 本池生效的准则
        """
        for crit in criteria:
            item.scores[crit.key] = QualityScore(
                criterion=crit.key, score=0.5, mode="pairwise_bt",
                detail={"comparisons": 0, "wins": 0, "ties": 0, "log_theta": 0.0})
        item.scores[AGGREGATE_KEY] = QualityScore(
            criterion=AGGREGATE_KEY, score=0.5, mode="pairwise_bt", detail={})

    @staticmethod
    def _index_results(raw_results: list) -> dict:
        """把并发返回的判定结果索引成 [comp_idx][准则][评委][是否翻转] → 裁决。

        @param raw_results (comp_idx, 评委, 是否翻转, {准则: 裁决}) 元组列表
        @return 四层嵌套索引，裁决取值为 胜者下标 | 'tie' | None（该次调用失败）
        """
        results: dict[int, dict[str, dict[str, dict[bool, int | str | None]]]] = {}
        for comp_idx, judge, flipped, verdicts in raw_results:
            comp = results.setdefault(comp_idx, {})
            for crit_key, outcome in verdicts.items():
                comp.setdefault(crit_key, {}).setdefault(judge, {})[flipped] = outcome
        return results

    def _score_criterion(self, pool: "_Pool", ctx: "RunContext",
                         crit: "Criterion", results: dict) -> None:
        """单条准则的打分闭环：合成裁决 → 平局计数 → BT 拟合 → 百分位归一化 → 写分。

        某条记录在本准则上「未打分」当且仅当它参与了 >= 1 次比较且每一次都失败；零参与的记录
        由 BT 正则化伪计数覆盖（spec 3.4.3），仍算已打分。百分位排名跨越本池全部 n 条记录
        （spec 3.4.3『将批内全部 log θ 升序排名』，v1.7 起池就是排名论域），未打分记录只把
        自己的分置空。

        @param pool 本池
        @param ctx 运行上下文
        @param crit 本条准则
        @param results _index_results 产出的四层索引
        """
        tally = self._tally_criterion(pool, crit.key, results)
        self._count_ties(ctx, pool.pool, crit.key, tally)
        log_theta = self._fit_criterion(ctx, pool, crit.key, tally)
        unscored = {k for k in range(len(pool.items))
                    if tally.comp_count[k] > 0 and tally.success[k] == 0}
        scores = _criterion_percentiles([float(v) for v in log_theta], unscored)
        for k, item in enumerate(pool.items):
            item.scores[crit.key] = QualityScore(
                criterion=crit.key, score=scores[k], mode="pairwise_bt",
                detail={"comparisons": tally.comp_count[k], "wins": tally.wins[k],
                        "ties": tally.ties[k], "log_theta": float(log_theta[k])})

    def _tally_criterion(self, pool: "_Pool", crit_key: str,
                         results: dict) -> _CriterionTally:
        """逐比较合成裁决（先做每评委的双序一致性，再做跨评委多数票）并累计统计。

        @param pool 本池
        @param crit_key 本条准则的 key
        @param results _index_results 产出的四层索引
        @return 本准则的合成统计
        """
        n = len(pool.items)
        judges: tuple[str, ...] = pool.q.judges if pool.q.judges else (pool.q.llm,)
        orders = (False, True) if pool.q.both_orders else (False,)
        tally = _CriterionTally(entries=[], comp_count=[0] * n, wins=[0] * n,
                                ties=[0] * n, success=[0] * n)
        for comp_idx, (_round_no, i, j, _first_is_a) in enumerate(pool.plan):
            per_judge = results.get(comp_idx, {}).get(crit_key, {})
            votes: list[int | str | None] = []
            for judge in judges:
                outcomes = per_judge.get(judge, {})
                votes.append(self._compose_orders(
                    [outcomes.get(flipped) for flipped in orders]))
            outcome = self._majority(votes, i, j)
            tally.comp_count[i] += 1
            tally.comp_count[j] += 1
            if outcome is not None:  # 至少一次判定调用成功
                tally.success[i] += 1
                tally.success[j] += 1
                tally.n_judged += 1
                if outcome == "tie":
                    tally.n_tie_judged += 1
            verdict: int | str = "tie" if outcome is None else outcome
            if verdict == "tie":
                tally.ties[i] += 1
                tally.ties[j] += 1
                tally.entries.append((i, j, 0.5))
                tally.entries.append((j, i, 0.5))
            else:
                winner = int(verdict)
                loser = j if winner == i else i
                tally.wins[winner] += 1
                tally.entries.append((winner, loser, 1.0))
        return tally

    @staticmethod
    def _count_ties(ctx: "RunContext", pool: str | None, crit_key: str,
                    tally: _CriterionTally) -> None:
        """累计 report.quality.per_criterion_tie_rate 所需的逐准则平局分子分母
        （E2E finding P4-9）：成对百分位均值天然是 0.5，报告靠这一对计数携带区分度信号。

        只统计「产生了裁决」的比较（评审意见）：把 provider 失败折进来会抬高平局率，把用户
        引向改 rubric 措辞，而真正的元凶是端点——调用失败在 counts.failed 与
        judgment_failures 里另有体现。v1.7（R12）：classify 开启时计数键带池维度——不同类
        rubric 里的同名准则不能共享同一份统计；classify 关闭时扁平键逐字节不变。

        @param ctx 运行上下文
        @param pool 池名；None = 匿名池（扁平键）
        @param crit_key 准则 key
        @param tally 本准则的合成统计
        """
        suffix = f"{pool}.{crit_key}" if pool is not None else crit_key
        ctx.metrics.count(f"quality.tie_outcomes.{suffix}", tally.n_tie_judged)
        ctx.metrics.count(f"quality.tie_comparisons.{suffix}", tally.n_judged)

    def _fit_criterion(self, ctx: "RunContext", pool: "_Pool", crit_key: str,
                       tally: _CriterionTally) -> np.ndarray:
        """对本准则做 BT 拟合并发出 quality.bt_fit 事件。

        @param ctx 运行上下文
        @param pool 本池
        @param crit_key 准则 key
        @param tally 本准则的合成统计（提供 BT 输入）
        @return 池内全部记录的 log θ
        """
        log_theta, iterations, converged = _fit_bradley_terry_details(
            len(pool.items), tally.entries)
        bt_payload: dict = {"criterion": crit_key, "iterations": iterations,
                            "converged": converged, "comparisons": len(pool.plan)}
        if pool.pool is not None:  # v1.7（R16）：把拟合归属到它的池
            bt_payload["pool"] = pool.pool
        ctx.metrics.event(_EV_BT_FIT, stage=self.name, batch_no=ctx.batch_no,
                          payload=bt_payload)
        return log_theta

    @staticmethod
    def _compose_orders(outcomes: list[int | str | None]) -> int | str | None:
        """单个评委内部的双序合成。

        单序直接透传；both_orders 时两个结果一致（同一条记录，或都为平局）取该结果，不一致
        取平局；某一序失败按平局计（spec 3.4.3 判定失败）；两序都失败返回 None。

        @param outcomes 该评委在各呈现序上的裁决
        @return 合成后的裁决；两序都失败时为 None
        """
        if len(outcomes) == 1:
            return outcomes[0]
        o1, o2 = outcomes
        if o1 is None and o2 is None:
            return None
        v1: int | str = "tie" if o1 is None else o1
        v2: int | str = "tie" if o2 is None else o2
        return v1 if v1 == v2 else "tie"

    @staticmethod
    def _majority(votes: list[int | str | None], i: int, j: int) -> int | str | None:
        """跨评委多数票：在 {i 胜, j 胜, 平局} 三类上取严格多数，无多数则平局。

        失败的评委按平局计，除非「全部」评委都失败（返回 None）。

        @param votes 各评委的合成裁决
        @param i 抽样序第一条记录的下标
        @param j 抽样序第二条记录的下标
        @return 多数票裁决；全部失败时为 None
        """
        if all(v is None for v in votes):
            return None
        counted = ["tie" if v is None else v for v in votes]
        total = len(counted)
        for cls in (i, j, "tie"):
            if counted.count(cls) * 2 > total:
                return cls
        return "tie"

    async def _run_pairwise_call(self, call: PairwiseQualityCall,
                                 ctx: "RunContext") -> QualityCallOutcome:
        """执行一次不修改 item、pool、event 或 error 的成对判定。

        @param call 冻结成对计划
        @param ctx 运行上下文
        @return 成功裁决或普通失败
        """
        keys = [criterion.key for criterion in call.criteria]
        schema = judgment_schema(keys, call.with_reason)
        fit = self._call_fit(ctx, QualityFitRequest(
            profile=call.profile,
            system_text=_pairwise_system_text(call.criteria, call.with_reason),
            schema=schema,
            records=(call.record_a, call.record_b),
        ))
        attempt = _Attempt(fit)
        while True:
            current = attempt.next_fit()
            prompt = self._pairwise_prompt(call, current)
            precheck = self._precheck_failure(current, attempt, ctx, "pairwise")
            if precheck is not None:
                return precheck
            try:
                obj, _usage, _attempts, model = await ctx.schema_engine.complete_validated(
                    call.profile, prompt, schema,
                    scope=CallScope(record_ids=self._pairwise_ids(call), batch_no=ctx.batch_no),
                )
            except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as exc:
                failure = self._call_failure(exc, attempt, ctx, call.ordinal)
                if failure is None:
                    continue
                return failure
            return self._pairwise_success(obj, keys, model)

    def _pairwise_prompt(self, call: PairwiseQualityCall,
                         fit: _CallFit | None) -> PromptBundle:
        """从冻结调用计划装配本次成对提示词。

        @param call 冻结成对计划
        @param fit 本次尝试的装填状态
        @return 提示词
        """
        pair = _Comparison(
            rec_a=call.record_a,
            rec_b=call.record_b,
            transitions_a=call.transitions_a,
            transitions_b=call.transitions_b,
        )
        return _build_pairwise_prompt(
            pair, call.criteria, call.with_reason,
            self.cfg.input.ui_tree_max_chars, fit,
        )

    @staticmethod
    def _pairwise_ids(call: PairwiseQualityCall) -> tuple[str, str]:
        """返回成对调用的抽样序记录身份。

        @param call 冻结成对计划
        @return 抽样序记录 id
        """
        if call.first_item_ordinal == call.a_item_ordinal:
            return call.record_a.id, call.record_b.id
        return call.record_b.id, call.record_a.id

    @staticmethod
    def _pairwise_success(obj: Mapping, keys: Sequence[str],
                          model: str) -> PairwiseQualityOutcome:
        """把可变 Schema 结果冻结为准则声明序裁决。

        @param obj 已通过 M8 校验的对象
        @param keys 本调用的准则声明序
        @param model 实际应答模型名
        @return 冻结成功结果
        """
        by_key = _judgments_by_key(obj)
        judgments = []
        for key in keys:
            entry = by_key.get(key)
            if entry is None:
                continue
            judgments.append(QualityJudgment(
                criterion=key,
                winner=entry["winner"],
                reason=entry.get("reason"),
            ))
        return PairwiseQualityOutcome(tuple(judgments), model)

    def _reduce_pairwise_call(self, pool: "_Pool", call: PairwiseQualityCall,
                              outcome: QualityCallOutcome, ctx: "RunContext") -> None:
        """按调用声明序提交一次成对结果及其事件与错误。

        @param pool 归属池
        @param call 冻结成对计划
        @param outcome 纯叶结果
        @param ctx 运行上下文
        """
        involved = (pool.items[call.first_item_ordinal],
                    pool.items[call.second_item_ordinal])
        keys = [criterion.key for criterion in call.criteria]
        if isinstance(outcome, QualityCallFailure):
            self._reduce_pairwise_failure(ctx, involved, outcome)
            verdicts = {key: None for key in keys}
        elif isinstance(outcome, PairwiseQualityOutcome):
            by_key = self._judgment_map(outcome.judgments)
            self._emit_judgment(ctx, pool.items, call, outcome)
            verdicts = _judgment_verdicts(
                by_key, keys, call.a_item_ordinal, call.b_item_ordinal,
            )
        else:
            _logger.error("quality pairwise outcome mismatch: ordinal=%d", call.ordinal,
                          extra={"stage": self.name, "batch": ctx.batch_no})
            raise InternalError("quality pairwise outcome mismatch")
        pool.results.append((call.comparison_ordinal, call.profile,
                             call.flipped, verdicts))

    def _reduce_pairwise_failure(self, ctx: "RunContext",
                                 involved: Sequence[PipelineItem],
                                 failure: QualityCallFailure) -> None:
        """把成对叶失败转成既有比较或记录级事实。

        @param ctx 运行上下文
        @param involved 涉及的两个信封
        @param failure 冻结调用失败
        """
        exc = failure.error
        if isinstance(exc, ContextOverflowError):
            message = (
                "pairwise slot exceeds the record-side budget at the minimal unit (2 records)"
                if failure.precheck
                else f"pairwise judgment overflow terminal ({type(exc).__name__}): {exc}"
            )
            self._overflow_tie(ctx, involved, message)
        elif isinstance(exc, SchemaViolation):
            self._record_judgment_failure(
                ctx, involved,
                f"pairwise judgment failed (SchemaViolation): {_violation_summary(exc)}",
            )
        else:
            self._record_call_failure(ctx, involved, exc, "pairwise judgment call failed")

    @staticmethod
    def _judgment_map(judgments: Sequence[QualityJudgment]) -> dict[str, dict]:
        """把冻结裁决还原成 reducer 内的事件与 verdict 视图。

        @param judgments 准则声明序冻结裁决
        @return 准则键到事件条目的映射
        """
        out: dict[str, dict] = {}
        for judgment in judgments:
            entry: dict = {"criterion": judgment.criterion, "winner": judgment.winner}
            if judgment.reason is not None:
                entry["reason"] = judgment.reason
            out[judgment.criterion] = entry
        return out

    def _emit_judgment(self, ctx: "RunContext", items: list[PipelineItem],
                       call: PairwiseQualityCall,
                       outcome: PairwiseQualityOutcome) -> None:
        """发出 quality.judgment 事件。

        record_ids 用「抽样序」，不是呈现的 A/B 序（§8.1）。

        @param ctx 运行上下文
        @param items 本池的信封列表
        @param call 本次调用的全部参数
        @param outcome 冻结裁决与实际应答模型
        """
        rec_first = items[call.first_item_ordinal].record
        rec_second = items[call.second_item_ordinal].record
        keys = [criterion.key for criterion in call.criteria]
        by_key = self._judgment_map(outcome.judgments)
        payload: dict = {"order": {"A": items[call.a_item_ordinal].record.id,
                                   "B": items[call.b_item_ordinal].record.id},
                         "model": outcome.model,
                         "judgments": [dict(by_key[k]) for k in keys if k in by_key]}
        if call.multi_judge:
            payload["judge"] = call.profile
        if call.pool is not None:  # v1.7（R16）：仅 classify 开启时
            payload["pool"] = call.pool
        excerpt = self._excerpt_payload((rec_first, rec_second))
        if excerpt is not None:
            payload["excerpt"] = excerpt
        ctx.metrics.event(_EV_JUDGMENT, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(rec_first.id, rec_second.id), payload=payload)

    # ── pointwise 模式 ──────────────────────────────────────────────────────

    def _pointwise_calls(self, pool: "_Pool", pool_ordinal: int,
                         start_ordinal: int) -> list[PointwiseQualityCall]:
        """按记录与准则声明序冻结逐条打分调用。

        @param pool 待打分的池
        @param pool_ordinal 当前池声明序
        @param start_ordinal 当前池首个全批任务序号
        @return 冻结逐条调用计划
        """
        calls: list[PointwiseQualityCall] = []
        for item_ordinal, item in enumerate(pool.items):
            for criterion in pool.criteria:
                calls.append(PointwiseQualityCall(
                    ordinal=start_ordinal + len(calls),
                    pool_ordinal=pool_ordinal,
                    item_ordinal=item_ordinal,
                    record=item.record,
                    transitions=(tuple(item.transitions)
                                 if item.transitions is not None else None),
                    criterion=criterion,
                    profile=pool.q.llm,
                    pool=pool.pool,
                ))
        return calls

    async def _run_pointwise_call(self, call: PointwiseQualityCall,
                                  ctx: "RunContext") -> QualityCallOutcome:
        """执行一次不修改 item、pool、event 或 error 的逐条判定。

        @param call 冻结逐条计划
        @param ctx 运行上下文
        @return 成功打分或普通失败
        """
        schema = pointwise_schema(call.criterion.key)
        fit = self._call_fit(ctx, QualityFitRequest(
            profile=call.profile,
            system_text=_pointwise_system_text(call.criterion),
            schema=schema,
            records=(call.record,),
        ))
        attempt = _Attempt(fit)
        while True:
            current = attempt.next_fit()
            prompt = _build_pointwise_prompt(
                call.record, call.criterion, self.cfg.input.ui_tree_max_chars,
                transitions=call.transitions, fit=current,
            )
            precheck = self._precheck_failure(current, attempt, ctx, "pointwise")
            if precheck is not None:
                return precheck
            try:
                obj, _usage, _attempts, _model = await ctx.schema_engine.complete_validated(
                    call.profile, prompt, schema,
                    scope=CallScope(record_ids=(call.record.id,), batch_no=ctx.batch_no),
                )
            except (CircuitBreakerTripped, KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as exc:
                failure = self._call_failure(exc, attempt, ctx, call.ordinal)
                if failure is None:
                    continue
                return failure
            entry = obj["scores"][0]
            return PointwiseQualityOutcome(int(entry["score"]), entry.get("reason", ""))

    def _reduce_pointwise_call(self, pool: "_Pool", call: PointwiseQualityCall,
                               outcome: QualityCallOutcome, ctx: "RunContext") -> None:
        """按调用声明序提交一次逐条结果及其事件与错误。

        @param pool 归属池
        @param call 冻结逐条计划
        @param outcome 纯叶结果
        @param ctx 运行上下文
        """
        item = pool.items[call.item_ordinal]
        if isinstance(outcome, QualityCallFailure):
            self._reduce_pointwise_failure(ctx, item, call, outcome)
            return
        if not isinstance(outcome, PointwiseQualityOutcome):
            _logger.error("quality pointwise outcome mismatch: ordinal=%d", call.ordinal,
                          extra={"stage": self.name, "batch": ctx.batch_no})
            raise InternalError("quality pointwise outcome mismatch")
        score = QualityScore(
            criterion=call.criterion.key,
            score=outcome.raw_score / 5.0,
            mode="pointwise",
            detail={"raw_score": outcome.raw_score, "reason": outcome.reason},
        )
        item.scores[call.criterion.key] = score
        self._emit_pointwise(ctx, item.record, score, call.pool)

    def _reduce_pointwise_failure(self, ctx: "RunContext", item: PipelineItem,
                                  call: PointwiseQualityCall,
                                  failure: QualityCallFailure) -> None:
        """把逐条叶失败转成既有记录级事实。

        @param ctx 运行上下文
        @param item 涉及的信封
        @param call 冻结逐条计划
        @param failure 冻结调用失败
        """
        exc = failure.error
        key = call.criterion.key
        if isinstance(exc, ContextOverflowError):
            message = (
                f"pointwise slot for criterion {key} exceeds the record-side budget "
                "at the minimal unit (1 record)"
                if failure.precheck
                else f"pointwise scoring overflow terminal for criterion {key} "
                     f"({type(exc).__name__}): {exc}"
            )
            self._overflow_fail_record(ctx, item, message)
            return
        if isinstance(exc, SchemaViolation):
            self._record_judgment_failure(
                ctx, (item,),
                f"pointwise scoring failed for criterion {key} "
                f"(SchemaViolation): {_violation_summary(exc)}",
            )
            item.scores[key] = QualityScore(
                criterion=key, score=None, mode="pointwise", detail={},
            )
            if _ATTEMPT_MODE.get():
                item.status = "failed"
            return
        self._record_call_failure(
            ctx, (item,), exc, f"pointwise scoring call failed for criterion {key}",
        )

    def _emit_pointwise(self, ctx: "RunContext", record: Record,
                        score: QualityScore, pool: str | None) -> None:
        """发出 quality.pointwise 事件。

        @param ctx 运行上下文
        @param record 被打分的记录
        @param score 刚写入的准则分（携带原始 0–5 分与理由）
        @param pool 池名；None = 匿名池
        """
        payload: dict = {"criterion": score.criterion,
                         "score": score.detail["raw_score"]}
        if pool is not None:  # v1.7（R16）：仅 classify 开启时
            payload["pool"] = pool
        if self._reasons_effective():
            payload["reason"] = score.detail["reason"]
        excerpt = self._excerpt_payload((record,))
        if excerpt is not None:
            payload["excerpt"] = excerpt
        ctx.metrics.event(_EV_POINTWISE, stage=self.name, batch_no=ctx.batch_no,
                          record_ids=(record.id,), payload=payload)

    # ── 聚合与门控 ──────────────────────────────────────────────────────────

    def _set_aggregates(self, items: Sequence[PipelineItem],
                        mode: Literal["pairwise_bt", "pointwise"],
                        criteria: Sequence["Criterion"] | None = None) -> None:
        """按 rubric 权重写入各信封的聚合分。

        @param items 待聚合的信封
        @param mode 写入 QualityScore.mode 的打分模式
        @param criteria 生效准则；None 时取全局 rubric
        """
        if criteria is None:
            criteria = self.cfg.rubric.criteria
        for item in items:
            per_crit = {key: qs.score for key, qs in item.scores.items()
                        if key != AGGREGATE_KEY}
            agg = _weighted_aggregate(criteria, per_crit)
            item.scores[AGGREGATE_KEY] = QualityScore(
                criterion=AGGREGATE_KEY, score=agg, mode=mode, detail={})

    def _apply_gate(self, items: Sequence[PipelineItem], ctx: "RunContext",
                    q: "QualityConfig | None" = None, pool: str | None = None) -> None:
        """按池执行质量门控（v1.7）。

        q = 本池生效的 QualityConfig（threshold / selection / top_ratio；on_unscored 永远取
        全局值，但搭在同一个对象上），因此 top_ratio 的配额基数就是本池的已打分幸存者。

        @param items 本池的信封
        @param ctx 运行上下文
        @param q 本池生效的质量配置；None 时取全局配置
        @param pool 池名；None = 匿名池
        """
        if q is None:
            q = self.cfg.quality
        active = [it for it in items if it.status == "active"]
        scored = [(it, _aggregate_of(it)) for it in active
                  if _aggregate_of(it) is not None]
        unscored = [it for it in active if _aggregate_of(it) is None]

        if q.selection == "top_ratio":
            self._gate_top_ratio(ctx, scored, q, pool)
        elif q.threshold is not None:
            self._gate_threshold(ctx, scored, q, pool)
        self._gate_unscored(ctx, unscored, q, pool)

    def _gate_top_ratio(self, ctx: "RunContext",
                        scored: Sequence[tuple[PipelineItem, float]],
                        q: "QualityConfig", pool: str | None) -> None:
        """top_ratio 选择：配额外的已打分记录置 dropped_lowq。

        @param ctx 运行上下文
        @param scored (信封, 聚合分) 序列，只含已打分记录
        @param q 本池生效的质量配置
        @param pool 池名；None = 匿名池
        """
        kept, ranks = _top_ratio_selection(
            [(it.record.id, agg) for it, agg in scored], q.top_ratio)
        for it, agg in scored:
            keep = it.record.id in kept
            ctx.metrics.event(_EV_GATE, stage=self.name, batch_no=ctx.batch_no,
                              record_ids=(it.record.id,),
                              payload=_pooled({"aggregate": agg,
                                               "decision": "keep" if keep else "drop",
                                               "selection": "top_ratio",
                                               "top_ratio": q.top_ratio,
                                               "rank": ranks[it.record.id]}, pool))
            if not keep:
                it.status = "dropped_lowq"

    def _gate_threshold(self, ctx: "RunContext",
                        scored: Sequence[tuple[PipelineItem, float]],
                        q: "QualityConfig", pool: str | None) -> None:
        """阈值门控：聚合分 < threshold 的记录置 dropped_lowq（相等保留）。

        @param ctx 运行上下文
        @param scored (信封, 聚合分) 序列，只含已打分记录
        @param q 本池生效的质量配置
        @param pool 池名；None = 匿名池
        """
        for it, agg in scored:
            keep = agg >= q.threshold
            ctx.metrics.event(_EV_GATE, stage=self.name, batch_no=ctx.batch_no,
                              record_ids=(it.record.id,),
                              payload=_pooled({"aggregate": agg,
                                               "decision": "keep" if keep else "drop",
                                               "threshold": q.threshold}, pool))
            if not keep:
                it.status = "dropped_lowq"

    def _gate_unscored(self, ctx: "RunContext", unscored: Sequence[PipelineItem],
                       q: "QualityConfig", pool: str | None) -> None:
        """未打分记录的处置：on_unscored 与门控模式无关地生效（spec 3.4.3 判定失败 行）。

        "keep" ⇒ 保持 active、分为空，且不占 top_ratio 配额位。

        @param ctx 运行上下文
        @param unscored 未打分的信封
        @param q 本池生效的质量配置
        @param pool 池名；None = 匿名池
        """
        gating = q.selection == "top_ratio" or q.threshold is not None
        keep = q.on_unscored == "keep"
        for it in unscored:
            if gating or not keep:
                payload: dict = {"aggregate": None,
                                 "decision": "keep" if keep else "drop"}
                if q.selection == "top_ratio":
                    payload["selection"] = "top_ratio"
                    payload["top_ratio"] = q.top_ratio
                elif q.threshold is not None:
                    payload["threshold"] = q.threshold
                ctx.metrics.event(_EV_GATE, stage=self.name, batch_no=ctx.batch_no,
                                  record_ids=(it.record.id,),
                                  payload=_pooled(payload, pool))
            if not keep:
                it.status = "dropped_lowq"

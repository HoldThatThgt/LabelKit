"""M10 编排器（spec 3.10、CONTRACTS.md §7.9）。

纯编排/调度——零业务逻辑、不直接发 LLM 调用、不写文件（输出通道属主是 M11）。职责：

- 把 M2 记录流（generate_only 模式下是 M6 generate_all 的产物）按 ``run.batch_size``
  切批；流模式（v1.8，``segment.enabled``）改消费 M2 会话流视图，按 next-fit **整会话**
  装箱——仅一只开口箱、超长会话硬切（S21）——并在帧信封上盖章 ``session_id``（S4）；
- 按配置开关（2.3.1 矩阵）以规范链序 segment → stitch → dedup → classify → extract →
  quality → generate → annotate → verify 组链（v1.9 单一超集元组；segment/stitch/extract
  默认关，关闭时逐字节退化为 v1.7 链）；
- 以 classify / segment 阶段前后的 len 差值计量 ``counts.fanout`` / ``counts.episodes``
  （v1.7 R9 / v1.8 §7.9——``counts.*`` 所有权归 M10，M13/M14 只就地追加信封）；post-emit
  清点 ``counts.stitched``，并以 episodes − stitched 单点导出 ``counts.threads``（v1.9 T7）；
- 调度单轮生成再流转（子批自 M3 重新入链，绝不回到 M6——不递归）；
- 驱动每批生命周期：每 (批, 阶段) 新建 RunContext、经 M11 落盘、冲洗 trace，然后丢弃全部
  批内中间态（内存释放）；
- 汇总运行级统计为 §9.3 报表结构并交给 ``Emitter.finalize``；
- 熔断处理（``CircuitBreakerTripped`` → 退出码 4，报告照写，``.part`` 不改名）与
  SIGINT/SIGTERM 优雅中断；
- ``--limit`` 截断与 ``--dry-run``（M1/M2 + stderr 静态调用/成本估算，零 LLM 调用、无主
  输出与 rejects——只有报告，以及 ``trace.enabled`` 时的 trace 通道）；
- v1.10 console 旁路（spec 3.10.3 console 行，SPEC-tui-console U11/U13/U17/U19/U20）：
  ``estimate_run(cfg, plan)`` 作为模块级纯函数导出（dry-run 与渲染器批级分母共用）、live
  路径复用 P2-4 预扫描发 ``metrics.run_estimate``（绝不二次 scan）、每次 stage.run 前发
  ``metrics.stage_begin``、``stop_requested`` 转发、rich 档 dry-run 打印让位。未挂
  ProgressListener 时以上全为 no-op（与 v1.9 逐字节一致）；
- v1.11 上下文预算（spec 3.10.3 上下文预算行，SPEC-context-budget V12/V13/V19）：
  ``estimate_run`` 把 segment 窗宽实参钳到 ``budget.min_window(cfg)``（上界语义）、每批派发
  后（以及 finalize 再一次）冻结 ``llm.calibrator``（F8 批冻结校准快照）、报表增 ``budget``
  节与 ``report.stream.windows``、启动期打印数据无关的预算 INFO 行。以上全部以「声明了
  ``context_window``」为门，全未声明的配置与 v1.10 行为逐字节一致。
"""
from __future__ import annotations

import asyncio
import logging
import random
import signal as _signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Sequence

from labelkit import TOOL_VERSION, __version__
from labelkit.common.config.model import (
    effective_frame_rules,
    effective_frame_windows,
    effective_tiers,
)
from labelkit.common.contracts.stage import RunContext, Stage
from labelkit.common.contracts.types import PipelineItem, Record
from labelkit.common.errors import CircuitBreakerTripped, InternalError
from labelkit.common.runtime import budget
# v1.17 Wave 2b：referenced_profiles 收集器已下沉 common 层（CONTRACTS §7.19.3）。
from labelkit.common.runtime.credentials import referenced_profiles

if TYPE_CHECKING:
    from labelkit.common.config.model import LLMProfile, ResolvedConfig, TierSpec
    from labelkit.common.observability.obslog import MetricsSink
    from labelkit.common.runtime.llm_client import LLMClient
    from labelkit.common.runtime.schema_engine import SchemaEngine
    from labelkit.operators.emitter import Emitter
    from labelkit.operators.ingest import IngestPlan, Ingestor

# 事件名——逐字对齐 CONTRACTS.md §7.11/§8.1（镜像
# ``labelkit.common.observability.obslog`` 的常量；此处用字面量避免运行时导入，
# 并由测试对着契约反查）。
_EV_RUN_START = "run.start"
_EV_RUN_END = "run.end"
_EV_BATCH_START = "batch.start"
_EV_BATCH_END = "batch.end"

# 规范链序（spec §2.2 / CONTRACTS.md §2/§7.9）：v1.9 的**单一超集元组**——v1.7 把
# classify 插在 dedup 之后；v1.8 前置 segment 并把 extract 排在 classify 与 quality
# 之间；v1.9 把 stitch 排在 segment 与 dedup 之间（T5）。segment/stitch/extract 默认关，
# 故有效链逐字节退化为 v1.7 的六名形（generate 与 segment 按 M1 互斥，两者永不同链）。
_CHAIN_ORDER = ("segment", "stitch", "dedup", "classify", "extract", "quality",
                "generate", "annotate", "verify")

# v1.8 闭集词表（§9.3 报表零基直方图）——必须等于 schema_engine 的枚举：
# action_schema() 的 action_type 11 值（S15）与 defect_verdict_schema() 的 defect
# kind 6 值（S31；v1.9 T15 追加 wrong_stitch）。
_ACTION_TYPES = ("click", "long_press", "input_text", "scroll", "drag", "open_app",
                 "app_switch", "navigate_back", "navigate_home", "wait", "other")
_DEFECT_KINDS = ("label_mismatch", "off_task_members", "missing_head",
                 "missing_tail", "missing_members", "wrong_stitch")

_log = logging.getLogger("labelkit.orchestrator")

# report.json 的 quality.aggregate_histogram 桶标签——冻结（§9.3）。
_HIST_LABELS = tuple(f"{i / 10:.1f}-{(i + 1) / 10:.1f}" for i in range(10))

_SCHEMA_STATS_ZERO = {"l0_or_clean": 0, "l1": 0, "l3_1": 0, "l3_2": 0, "rejected": 0}

# estimate_run 返回字典里十个调用数键的**冻结键序**（v1.12 起 frame_classify_calls
# 紧跟 classify_calls、frame_annotate_calls 紧跟 annotate_calls）；total_calls 恒等于
# 这十项之和。records / batches 两键在其前，total_calls 在其后。
_ESTIMATE_CALL_ORDER = ("generate_calls", "segment_calls", "stitch_calls",
                        "classify_calls", "frame_classify_calls", "extract_calls",
                        "quality_calls", "annotate_calls", "frame_annotate_calls",
                        "verify_calls")

# 时序流五键的零值底盘（非流分支恒取此值）。
_STREAM_CALLS_ZERO = {"segment_calls": 0, "stitch_calls": 0, "extract_calls": 0,
                      "frame_classify_calls": 0, "frame_annotate_calls": 0}

# post-emit 状态清点中从 failed 兜底公式里扣除的终态（v1.8 增 absorbed /
# dropped_noise，v1.9 增 stitched——不扣除则这些终态信封被误计为 failed）；
# 顺序即 counts.* 的落账顺序。
_DEDUCTED_STATUSES = ("dropped_dup", "dropped_lowq", "dropped_verify",
                      "absorbed", "dropped_noise", "stitched")


def _ceil_div(a: int, b: int) -> int:
    """向上取整除法（除数非正时返回 0）。

    @param a: 被除数
    @param b: 除数
    @return: ``ceil(a / b)``；``b <= 0`` 时返回 0
    """
    return -(-a // b) if b > 0 else 0


def _pack_next_fit(session_lens: Sequence[int],
                   batch_size: int) -> tuple[list[int], list[int]]:
    """会话长度序列的 next-fit 装箱空跑——与 ``_run_process_stream`` 逐条同构，
    故 dry-run 批数是**精确值**（S21/S22）。超长会话硬切成 ``batch_size`` 片，每片
    自成一批。

    @param session_lens: 按到达序的会话帧数序列
    @param batch_size: 批容量（帧）
    @return: (每批帧数, 每批会话片数)
    """
    frames: list[int] = []
    pieces: list[int] = []
    open_frames = open_pieces = 0
    for length in session_lens:
        if length > batch_size:
            if open_frames:
                frames.append(open_frames)
                pieces.append(open_pieces)
                open_frames = open_pieces = 0
            full, rest = divmod(length, batch_size)
            frames.extend([batch_size] * full)
            pieces.extend([1] * full)
            if rest:
                frames.append(rest)
                pieces.append(1)
            continue
        if open_frames and open_frames + length > batch_size:
            frames.append(open_frames)
            pieces.append(open_pieces)
            open_frames = open_pieces = 0
        open_frames += length
        open_pieces += 1
    if open_frames:
        frames.append(open_frames)
        pieces.append(open_pieces)
    return frames, pieces


@dataclass(frozen=True)
class _EstimateScale:
    """静态估算的规模量——``estimate_run`` 的中间产物，仅本模块内部使用。"""

    total_records: int                             # 进链记录总数（摄取 + 生成）
    ingested: int                                  # 摄取记录数（generate_only 恒 0）
    generated: int                                 # 生成记录数
    generate_calls: int                            # 生成调用数
    batches: int                                   # 批次数
    pools: list[int]                               # 每批 pairwise 评审池大小
    downstream_base: int                           # quality/annotate/verify 的记录基数
    stream_calls: dict                             # 时序流五键调用数（非流分支全 0）


@dataclass(frozen=True)
class _CounterView:
    """报表组装期的计数器只读视图——一次快照供各分节共读，避免组装中途漂移。"""

    values: Mapping                                # 计数器键 → 累计值的快照

    def __call__(self, key: str) -> int:
        """读一个计数器。

        @param key: 计数器键名
        @return: 累计值；键缺席记 0
        """
        return int(self.values.get(key, 0))


def _estimate_generate_only(cfg: "ResolvedConfig") -> tuple[int, int]:
    """generate_only 模式的生成量估算（3.6.2 量公式，静态、无需 scan）。

    v1.17（SPEC-SP §10.2）：``generate_stream.enabled`` 时直读 M1 冻结的
    ``cfg.scenario_plan``——records = len(slots)、generate_calls = 2 × len(slots) +
    len(noise_slots)（每 sequence slot 一次 brief + 一次 realize，每 noise slot 一次
    realize；duplicates 零 LLM）。不含 delivery retry、LLMClient 内 provider retry 与
    Schema repair。

    @param cfg: 已解析配置
    @return: (生成调用数, 生成记录数)
    """
    g = cfg.generate
    if cfg.generate_stream.enabled:
        plan = cfg.scenario_plan
        records = len(plan.slots)
        return 2 * records + len(plan.noise_slots), records
    if g.seed_examples:
        calls = _ceil_div(len(g.seed_examples) * g.num_per_record, g.num_per_call)
    else:
        calls = _ceil_div(g.standalone_count or 0, g.num_per_call)
    if cfg.limit is not None:
        calls = min(calls, _ceil_div(cfg.limit, g.num_per_call))
    records = calls * g.num_per_call
    if cfg.limit is not None:
        records = min(records, cfg.limit)
    return calls, records


def _estimate_inline_generate(cfg: "ResolvedConfig", n_ingested: int) -> tuple[int, int]:
    """process 模式下在链内 generate 工位的生成量估算（关闭时全 0）。

    @param cfg: 已解析配置
    @param n_ingested: 摄取记录数（已按 ``--limit`` 截断）
    @return: (生成调用数, 生成记录数)
    """
    g = cfg.generate
    if not g.enabled:
        return 0, 0
    calls = _ceil_div(n_ingested * g.num_per_record, g.num_per_call)
    return calls, calls * g.num_per_call


def _estimate_stream_calls(cfg: "ResolvedConfig",
                           session_lens: Sequence[int]) -> dict:
    """时序流五键（segment/stitch/extract/帧分类/帧标注）的调用数估算。

    ``segment_calls = Σ ceil((L−1)/(w−1))``（L ≥ 2 的会话求和；L = 1 或
    ``strategy="rules"`` 计 0），其中 v1.11 V12 起 ``w = min(segment.window,
    budget.min_window(cfg))``——最坏保证装填量（**上界**语义：实际每窗装填 ≥ w_min 帧，
    故实际窗数 ≤ 估算）。min_window 按设计**不自带上限**（M1 的 V9 护栏要用原始预算导出
    值），故在**本调用点**钳到窗宽上限；预算未声明 ⇒ min_window 返回 window ⇒ 数值与
    v1.10 逐字节一致（V26 的 examples 声明的实效窗足够大，w_min > window，八个 dry-run
    golden 不动）。``stitch_calls``（v1.9 T16 估算，沿用 S22 的 episodes ≈ sessions 下界
    基数）= 每 episode 候选一次判断 × votes 采样 × repass 开启时翻倍。``extract_calls =
    Σ(L−1)``（上界）。v1.12 帧粒度两键 = 预扫描帧总数 Σ session_lens 的粗上界（与
    segment_calls 完全同源；帧分类实际按窗批量、帧标注跳过噪声成员，均 ≤ 帧总数）。

    @param cfg: 已解析配置
    @param session_lens: 预扫描得到的会话帧数序列
    @return: 五键调用数字典（对应开关关闭 ⇒ 该键 0）
    """
    calls = dict(_STREAM_CALLS_ZERO)
    if cfg.segment.strategy in ("llm", "hybrid"):
        w = min(cfg.segment.window, budget.min_window(cfg))
        calls["segment_calls"] = sum(_ceil_div(length - 1, w - 1)
                                     for length in session_lens if length >= 2)
    if cfg.stitch.enabled:
        calls["stitch_calls"] = (len(session_lens) * cfg.stitch.votes
                                 * (2 if cfg.stitch.repass else 1))
    if cfg.extract.enabled:
        calls["extract_calls"] = sum(length - 1 for length in session_lens)
    if cfg.frame_classify.enabled:
        calls["frame_classify_calls"] = sum(session_lens)
    if cfg.frame_annotate.enabled:
        calls["frame_annotate_calls"] = sum(session_lens)
    return calls


def _estimate_scale(cfg: "ResolvedConfig", plan: "IngestPlan | None") -> _EstimateScale:
    """算出估算的规模量：记录数、批数、评审池、下游基数、生成量与时序流调用数。

    v1.8 流模式（segment × generate_only 被 M1 禁止，故这里的 plan 恒为 process 模式的
    扫描结果）：会话表 → **精确**批数（next-fit 空跑），episodes ≈ sessions 作为下游记录
    基数（**下界**，stderr 另有注记），pairwise 每批池大小 = 该批装入的会话片数。

    @param cfg: 已解析配置
    @param plan: M2 扫描结果；generate_only 传 None
    @return: 规模量聚合对象
    @raises AssertionError: process 模式未提供扫描结果
    """
    n_ingested = 0
    if cfg.run.mode == "generate_only":
        gen_calls, gen_records = _estimate_generate_only(cfg)
    else:
        assert plan is not None, "process-mode estimate requires an IngestPlan"
        n_ingested = plan.estimated_records
        if cfg.limit is not None:
            n_ingested = min(n_ingested, cfg.limit)
        gen_calls, gen_records = _estimate_inline_generate(cfg, n_ingested)

    total_records = n_ingested + gen_records
    bs = cfg.run.batch_size
    pools = [bs] * (total_records // bs)
    if total_records % bs:
        pools.append(total_records % bs)
    n_batches, downstream_base = len(pools), total_records
    stream_calls = dict(_STREAM_CALLS_ZERO)
    if cfg.segment.enabled and cfg.run.mode == "process":
        session_lens = tuple(getattr(plan, "session_lens", ()) or ())
        frame_sizes, pools = _pack_next_fit(session_lens, bs)
        n_batches, downstream_base = len(frame_sizes), len(session_lens)
        stream_calls = _estimate_stream_calls(cfg, session_lens)
    return _EstimateScale(total_records=total_records, ingested=n_ingested,
                          generated=gen_records, generate_calls=gen_calls,
                          batches=n_batches, pools=pools,
                          downstream_base=downstream_base,
                          stream_calls=stream_calls)


def _estimate_classify_calls(cfg: "ResolvedConfig", scale: _EstimateScale) -> int:
    """classify 调用数估算。

    v1.7 R11：process 模式 = ingested × max(1, self_consistency)（再流转子批继承分类、
    跳过 M13，不计入），generate_only = 生成记录数 × max(1, self_consistency)，流模式改
    以 episodes ≈ sessions 为基数。v1.13：时间流形态的序列标签直接继承（inherited，
    v1.7 R11 幂等哲学）——classify_calls 恒 0，classify.enabled 只作类表载体。

    @param cfg: 已解析配置
    @param scale: 规模量
    @return: classify 调用数
    """
    if not cfg.classify.enabled or cfg.generate_stream.enabled:
        return 0
    if cfg.run.mode == "generate_only":
        base = scale.generated
    elif cfg.segment.enabled:
        base = scale.downstream_base
    else:
        base = scale.ingested
    return base * max(1, cfg.classify.self_consistency)


def _estimate_quality_calls(cfg: "ResolvedConfig", scale: _EstimateScale) -> int:
    """quality 调用数估算（pairwise 按每批评审池计，pointwise 按记录数 × 准则数）。

    @param cfg: 已解析配置
    @param scale: 规模量
    @return: quality 调用数
    """
    if not cfg.quality.enabled:
        return 0
    n_criteria = len(cfg.rubric.criteria)
    if cfg.quality.mode != "pairwise":
        return scale.downstream_base * n_criteria
    judges = max(1, len(cfg.quality.judges))
    orders = 2 if cfg.quality.both_orders else 1
    per_call = n_criteria if cfg.quality.criteria_per_call == "single" else 1
    return (sum(cfg.quality.rounds * (b // 2) for b in scale.pools)
            * per_call * judges * orders)


def estimate_run(cfg: "ResolvedConfig", plan: "IngestPlan | None") -> dict:
    """静态记录数 / LLM 调用数估算——v1.10（U20）把原 ``Orchestrator._estimate`` 的函数体
    导出为吃 (cfg, plan) 的**模块级纯函数**，由 dry-run、live 的 ``metrics.run_estimate``
    与渲染器的批级分母共用。``plan`` 是 M2 的扫描结果（``IngestPlan``）；generate_only 传
    None（3.6.2 量公式是静态的，不需要 scan）。**返回键集与键序冻结**（见
    ``_ESTIMATE_CALL_ORDER``）。

    全部估算假定零丢弃（上界）且不含重试与修复调用；各分项的上/下界语义与版本沿革见
    ``_estimate_scale`` / ``_estimate_stream_calls`` / ``_estimate_classify_calls`` 的
    说明（v1.8 S22 流模式、v1.11 V12 预算钳位、v1.12 帧粒度两键、v1.13 时间流精确复演）。

    @param cfg: 已解析配置
    @param plan: M2 扫描结果；generate_only 传 None
    @return: 冻结键集的估算字典
    @raises AssertionError: process 模式未提供扫描结果
    """
    scale = _estimate_scale(cfg, plan)
    calls = dict(scale.stream_calls)
    calls["generate_calls"] = scale.generate_calls
    calls["classify_calls"] = _estimate_classify_calls(cfg, scale)
    calls["quality_calls"] = _estimate_quality_calls(cfg, scale)
    sc = cfg.annotate.self_consistency
    calls["annotate_calls"] = (scale.downstream_base * (sc if sc >= 3 else 1)
                               if cfg.annotate.enabled else 0)
    calls["verify_calls"] = (scale.downstream_base * max(1, len(cfg.verify.judges))
                             if cfg.verify.enabled else 0)
    est: dict = {"records": scale.total_records, "batches": scale.batches}
    for key in _ESTIMATE_CALL_ORDER:
        est[key] = calls[key]
    est["total_calls"] = sum(calls[key] for key in _ESTIMATE_CALL_ORDER)
    if cfg.generate_stream.enabled and cfg.scenario_plan is not None:
        est["scenario"] = _estimate_scenario(cfg)
    return est


def _estimate_scenario(cfg: "ResolvedConfig") -> dict:
    """v1.17（SPEC-SP §10.2）estimate 的 scenario 子块（冻结键序，直读计划）。

    @param cfg: 已解析配置（``scenario_plan`` 已由 M1 冻结）
    @return: 十键字典（models 的 families 值为 {variables, constraints} 映射）
    """
    from datetime import datetime, timedelta, timezone as tz

    from labelkit.operators.generate_stream import _us_iso

    plan = cfg.scenario_plan
    schedule = cfg.generate_stream.schedule
    models = {
        name: {"entries": stats.variables + stats.constraints,
               "families": {family: {"variables": row.variables,
                                     "constraints": row.constraints}
                            for family, row in stats.families.items()}}
        for name, stats in plan.models.items()
    }
    return {
        "target_sequences": len(plan.slots),
        "task_frames": sum(len(layout.frames) for layout in plan.layouts),
        "noise_frames": len(plan.noise_slots),
        "sessions": len(plan.sessions),
        "crossed_sessions": cfg.generate_stream.crossed_sessions,
        "schedule_start": _us_iso(schedule.start_us, cfg),
        "schedule_end": _us_iso(schedule.end_us, cfg),
        "calendar_days_spanned": plan.objectives.calendar_days_spanned,
        "plan_digest": plan.plan_digest,
        "models": models,
    }


@dataclass(frozen=True)                            # [FROZEN in CONTRACTS.md §7.9]
class RunSummary:
    """一次运行的对外摘要——CLI 据此收敛退出码与终端摘要行。"""

    counts: Mapping                                # 与 report.json "counts" 同键集（§9.3）
    interrupted: bool                              # 是否被 SIGINT/SIGTERM 优雅中断
    exit_code: int                                 # 4（熔断）| 1（strict 且有 rejects）| 0
    wall_s: float                                  # 运行墙钟秒数
    output_lines: int                              # 主输出行数
    rejects_lines: int                             # rejects 行数


@dataclass(frozen=True)
class RunServices:
    """编排器的运行期共享服务与运行身份——构造参数对象（CONTRACTS.md §7.9）。

    spec 3.10.3 列出 (cfg, stages, ingestor, emitter, llm)；schema_engine/metrics 是构造
    RunContext 所需，run_id/run_started_at 供报表组装与运行级事件使用（**不进**
    RunContext，spec 3.12.3）。
    """

    llm: "LLMClient"                               # M9 LLM 客户端（用量/校准器读取面）
    schema_engine: "SchemaEngine"                  # M8 Schema 引擎（resolved_at 统计面）
    metrics: "MetricsSink"                         # M12 计数与事件汇（含 console 旁路）
    run_id: str                                    # 本次运行标识
    run_started_at: datetime                       # 运行起点（带时区）


class Orchestrator:
    """驱动整轮运行的编排器；构造签名冻结于 CONTRACTS.md §7.9。"""

    def __init__(self, cfg: ResolvedConfig, stages: list[Stage],
                 ingestor: Ingestor | None, emitter: Emitter,
                 services: RunServices):
        """装配编排器（不做任何 I/O，真正的运行发生在 ``run()``）。

        @param cfg: 已解析配置
        @param stages: M1 开关筛出的算子实例（组链时按规范链序重排）
        @param ingestor: M2 摄取器；generate_only 模式传 None
        @param emitter: M11 输出器
        @param services: 运行期共享服务与运行身份
        """
        self.cfg = cfg
        self.stages = stages
        self.ingestor = ingestor
        self.emitter = emitter
        self.llm = services.llm
        self.schema_engine = services.schema_engine
        self.metrics = services.metrics
        self.run_id = services.run_id
        self.run_started_at = services.run_started_at
        # v1.12：向 M11 注入帧计数通路（frame_annotate.failed/discarded ——
        # Ingestor.metrics 同款装配期鸭子面；emitter 构造签名冻结不改）。
        emitter.metrics = services.metrics

        # 运行级汇总状态（不含数据内容，spec 3.10.3）。
        self._stage_time: dict[str, float] = {}
        self._agg_hist = [0] * 10
        self._crit_sum: dict[str, float] = {}
        self._crit_n: dict[str, int] = {}
        # v1.7 R12：上面三个累加器的按池镜像，仅 classify 启用时喂入
        # （池 = item.classification.label）。
        self._pool_agg_hist: dict[str, list[int]] = {}
        self._pool_crit_sum: dict[str, dict[str, float]] = {}
        self._pool_crit_n: dict[str, dict[str, int]] = {}
        self._output_lines = 0
        self._rejects_lines = 0
        self._batch_no = 0
        self._pending: deque[list[PipelineItem]] = deque()  # 生成再流转队列
        self._split_warned = False                 # 会话硬切 WARN 每轮仅一次

        # 控制流。
        self._stop = False
        self._interrupted = False
        self._circuit_broken = False
        self._current_task: asyncio.Task | None = None
        self._installed_signals: list[int] = []
        self._timer_handles: list[asyncio.TimerHandle] = []
        self._t0 = 0.0
        self._estimate_cache: dict | None = None

    # ── 对外入口 ───────────────────────────────────────────────────────────

    async def run(self) -> RunSummary:
        """跑完一轮：预扫描 → 装信号 → 起运行 → 按模式驱动 → finalize。

        @return: 运行摘要（含退出码）
        """
        self._t0 = time.perf_counter()
        if self.cfg.dry_run:
            return self._run_dry()

        plan, plan_estimated = self._prescan()
        self._install_signal_handlers()
        try:
            self._emit_run_start(plan, plan_estimated)
            self.emitter.open()
            try:
                if self.cfg.run.mode == "generate_only":
                    await self._run_generate_only()
                else:
                    await self._run_process()
            except CircuitBreakerTripped:
                # 连续致命错误达 run.fatal_error_threshold：剩余工作放弃，报告照写，
                # .part 不改名（v1.6 熔断交付另行决定已完成批的交付）。
                _log.error("circuit breaker tripped: remaining batches abandoned",
                           extra={"stage": "run", "batch": self._batch_no})
                self._circuit_broken = True
        finally:
            self._remove_signal_handlers()
        return self._finalize()

    def _prescan(self) -> tuple["IngestPlan | None", bool]:
        """P2-4 预扫描：在首个 trace 落笔（即打开并截断 trace 文件）**之前**先对路径/
        候选/配对错误 fail fast，免得一个出生即死的运行毁掉上一轮的 trace。扫描期把
        metrics 摘掉，彩排一声不响；真正的 records() 遍历会按序重发全部事件。

        v1.10（U17 复用铁律）：**同一次**彩排扫描兼作 live 估算的数据源，绝不扫第二遍。
        UI 模态下 estimate=True 是白送的（配对表本来就要建），文本模态则是显式 opt-in
        （console.estimate 会把输入再读一遍）；否则 estimate=False 维持只 fail-fast 的
        行为（文本模态数行数要读完每个字节，结果却没人用——等于每轮运行的输入 I/O 翻倍）。

        @return: (扫描计划, 该计划是否带估算)；generate_only 返回 (None, False)
        """
        if self.cfg.run.mode != "process" or self.ingestor is None:
            return None, False
        saved_metrics = getattr(self.ingestor, "metrics", None)
        self.ingestor.metrics = None
        estimate = (self.cfg.run.modality == "ui") or self.cfg.console.estimate
        try:
            plan = self.ingestor.scan(estimate=estimate)
        finally:
            self.ingestor.metrics = saved_metrics
        return plan, estimate

    def _emit_run_start(self, plan: "IngestPlan | None", plan_estimated: bool) -> None:
        """发 run.start、（可用时）发 live 估算、打印启动期预算 INFO 行。

        v1.10（U17/U19/U20）：live 估算只在**确有可用估算**时发——process 模式带估算的
        计划，或 generate_only（plan=None，3.6.2 量公式是静态的、无需 scan）。文本模态未
        开 console.estimate 时什么都不发（渲染器于是只显示「批 i」不带分母）。v1.16
        时间流估算会运行联合规划，因此未挂 listener 时跳过这次无人消费的规划；其余静态估算
        仍保持既有调用路径。

        @param plan: 预扫描计划
        @param plan_estimated: 该计划是否带估算
        """
        self.metrics.event(_EV_RUN_START, stage="run", batch_no=0,
                           payload={"tool_version": TOOL_VERSION,
                                    "config_digest": self.cfg.config_digest,
                                    "project_digest": self.cfg.project_digest,
                                    "trace_schema_version": 1})
        should_estimate = self.cfg.run.mode == "generate_only" or plan_estimated
        stream_listener_ready = (
            not self.cfg.generate_stream.enabled or self.metrics.has_listener
        )
        if should_estimate and stream_listener_ready:
            self._estimate_cache = estimate_run(self.cfg, plan)
            self.metrics.run_estimate(self._estimate_cache)
        # v1.11（V13①，spec 3.10.3 上下文预算行）：启动期预算 INFO 归 M10 启动段所有
        # （绝不归 loader：加载期 logging 尚未按 CLI 覆盖定级）。--dry-run 走不到这里
        # （_run_dry 已在上游返回），故即便 examples 现已声明窗宽（V26），dry-run 的
        # golden 仍逐字节冻结。
        self._log_budget_startup()

    # ── 模式驱动 ───────────────────────────────────────────────────────────

    async def _run_process(self) -> None:
        """process 模式主驱动：按 batch_size 切批，生成子批插在父批之后。

        @raises AssertionError: 未提供摄取器
        """
        assert self.ingestor is not None, "process mode requires an Ingestor"
        if getattr(self.ingestor, "metrics", None) is None:
            self.ingestor.metrics = self.metrics   # trace 接线（CONTRACTS §7.1）
        if self.cfg.segment.enabled:
            # v1.8 流模式：改走 M2 会话流视图的整会话 next-fit 装箱（generate 与 segment
            # 按 M1 互斥，故再流转队列在这条路径上永不填充）。
            await self._run_process_stream()
            return
        stream = iter(self.ingestor.records())
        if self.cfg.limit is not None:
            stream = islice(stream, self.cfg.limit)

        main_chain = self._compose_chain(include_generate=True)
        reflow_chain = self._compose_chain(include_generate=False)

        while not self._stop:
            if self._pending:
                # 生成子批紧跟父批执行，批号连续，且绝不二次生成。
                batch = self._pending.popleft()
                chain = reflow_chain
            else:
                records = list(islice(stream, self.cfg.run.batch_size))
                if not records:
                    break
                batch = [PipelineItem(record=r) for r in records]
                chain = main_chain
            await self._dispatch(batch, chain)
            del batch                              # 批级内存生命周期：落盘后不留引用

    async def _run_process_stream(self) -> None:
        """v1.8 流模式装箱（S21/S4，CONTRACTS §7.9）：消费 ``ingestor.sessions()``
        （``--limit`` 的 islice 住在 M2 内部、夹在解析流与组装器之间——S17），按 next-fit
        整会话装箱：恰一只开口箱，装不下的会话封掉当前批、另开一箱。批容量 =
        run.batch_size **帧**。超过 batch_size 的会话**硬切**成 batch_size 片，每片自成
        一批。M10 在构造信封时盖章 ``PipelineItem.session_id``（S4）；会话流耗尽后，残留的
        开口箱原样发出。收到 SIGINT/SIGTERM 后不再派发**新**批——缓冲中的帧滞留为中断残差
        （S18）。

        @raises AssertionError: 未提供摄取器
        """
        assert self.ingestor is not None, "process mode requires an Ingestor"
        chain = self._compose_chain(include_generate=True)
        bs = self.cfg.run.batch_size
        open_batch: list[PipelineItem] = []

        for sess in self.ingestor.sessions():
            if self._stop:
                break
            frames = [PipelineItem(record=r, session_id=sess.session_id)
                      for r in sess.records]
            if len(frames) > bs:
                self._mark_split_session(frames)
                if open_batch:
                    await self._dispatch(open_batch, chain)
                    open_batch = []
                await self._dispatch_split_session(frames, chain)
                continue
            if open_batch and len(open_batch) + len(frames) > bs:
                # 唯一的待装箱溢出会话——新增的跨批存活项（§11 ⑤），装箱即释放。
                await self._dispatch(open_batch, chain)
                open_batch = []
            open_batch.extend(frames)
        if open_batch and not self._stop:
            await self._dispatch(open_batch, chain)  # 残留开口箱原样发出

    def _mark_split_session(self, frames: list[PipelineItem]) -> None:
        """给被硬切会话的每一帧打 duck-typed ``session_split`` 标（S21），并每轮 WARN
        一次（M7 缺帧判定的降级依据，落到 ``_meta.stream.session_split``）。

        @param frames: 该会话的全部帧信封
        """
        if not self._split_warned:
            self._split_warned = True
            _log.warning("session exceeds batch_size and was hard-split "
                         "(warned once per run)",
                         extra={"stage": "run", "batch": self._batch_no})
        for item in frames:
            item.session_split = True

    async def _dispatch_split_session(self, frames: list[PipelineItem],
                                      chain: Sequence[Stage]) -> None:
        """硬切派发（S21）：按 batch_size 切片，每片自成一批、按序派发。

        @param frames: 该会话的全部帧信封
        @param chain: 本批的阶段链
        """
        bs = self.cfg.run.batch_size
        for i in range(0, len(frames), bs):
            if self._stop:
                break
            await self._dispatch(frames[i:i + bs], chain)

    async def _run_generate_only(self) -> None:
        """generate_only 模式驱动：一次性生成 → 切批走再流转链（不含 generate 工位）。

        @raises InternalError: 阶段表里没有 generate 算子
        """
        gen = next((s for s in self.stages if s.name == "generate"), None)
        if gen is None:
            raise InternalError("generate_only mode requires a generate stage")
        # 预抽 PRNG 固定在 batch_no=0（spec 3.10.3）：Random(f"{seed}:0:generate")。
        ctx0 = self._make_ctx(0, "generate")
        if self.cfg.generate_stream.enabled:
            # v1.13 时间流形态（SPEC-stream-generation §3.2/§3.6）分支。
            await self._run_generate_stream(gen, ctx0)
            return
        product = await self._await_generate(gen.generate_all(ctx0))
        records: list[Record] = list(product) if product is not None else []
        if self.cfg.limit is not None:
            records = records[: self.cfg.limit]    # generate_all 已截断，此处兜底
        if records:
            self.metrics.count("counts.generated", len(records))
        # 生成 0 条 → 循环空转 → 正常 finalize，退出码 0。
        chain = self._compose_chain(include_generate=False)
        bs = self.cfg.run.batch_size
        for i in range(0, len(records), bs):
            if self._stop:
                break
            batch = [PipelineItem(record=r) for r in records[i:i + bs]]
            await self._dispatch(batch, chain)
            del batch

    async def _run_generate_stream(self, gen, ctx0: RunContext) -> None:
        """v1.13 时间流形态驱动（SPEC-stream-generation §3.2/§3.6）：一次
        ``generate_stream_all`` → 工件经 M11 工件通道落盘 → ``counts.generated`` = 进链
        序列条数 → 直装信封按 batch_size 切批走再流转链（信封已带 session_id/
        classification/member_classifications，绝不 ``PipelineItem(record=r)`` 裸构造
        重建）。``--limit`` 已在 M6 计划期配额层前缀截断，此处兜底再截一次。

        @param gen: generate 算子实例
        @param ctx0: batch_no=0 的生成期上下文
        """
        product = await self._await_generate(gen.generate_stream_all(ctx0))
        if product is None:
            return                                 # 中断即无产物、无工件、无批次
        # 工件先于任何批派发落盘（.part + flush；finalize 与主输出同批改名）。
        self.emitter.write_stream_artifact(list(product.artifact_lines))
        envelopes = list(product.envelopes)
        if self.cfg.limit is not None:
            envelopes = envelopes[: self.cfg.limit]
        if envelopes:
            self.metrics.count("counts.generated", len(envelopes))
        chain = self._compose_chain(include_generate=False)
        bs = self.cfg.run.batch_size
        for i in range(0, len(envelopes), bs):
            if self._stop:
                break
            await self._dispatch(envelopes[i:i + bs], chain)

    async def _await_generate(self, coro):
        """守护式执行生成协程——与 ``_guarded_batch`` 同形，故 SIGINT/SIGTERM 能停下它：
        ``_request_stop`` 的 30 s 计时器会取消 ``self._current_task``（spec 3.10.3 中断行；
        CONTRACTS §7.9「等当前批 ≤ 30 s 再取消」）。其墙钟像任何启用阶段一样计入
        report.timing.per_stage_s。

        @param coro: 生成协程（``generate_all`` 或 ``generate_stream_all``）
        @return: 协程产物；被我方中断取消时返回 None（生成期中断 ⇒ 无产物，finalize 照常
                 以 interrupted=true 收尾）
        @raises asyncio.CancelledError: 非我方 stop 触发的外部取消原样上抛
        """
        task = asyncio.ensure_future(coro)
        self._current_task = task
        t_gen = time.perf_counter()
        try:
            return await task
        except asyncio.CancelledError:
            if not self._stop:
                raise                              # 外部取消，不是我方中断
            return None
        finally:
            self._current_task = None
            elapsed = time.perf_counter() - t_gen
            self._stage_time["generate"] = self._stage_time.get("generate", 0.0) + elapsed
            self.metrics.add_stage_time("generate", elapsed)

    # ── 批生命周期 ─────────────────────────────────────────────────────────

    async def _dispatch(self, batch: list[PipelineItem],
                        chain: Sequence[Stage]) -> None:
        """批号自增后派发一批（三条模式驱动的唯一派发口）。

        @param batch: 本批信封
        @param chain: 本批的阶段链
        """
        self._batch_no += 1
        await self._guarded_batch(batch, self._batch_no, chain)

    async def _guarded_batch(self, batch: list[PipelineItem], batch_no: int,
                             chain: Sequence[Stage]) -> None:
        """把一批包成 task 跑，好让 SIGINT 的 30 s 超时能取消它。

        @param batch: 本批信封
        @param batch_no: 批号
        @param chain: 本批的阶段链
        @raises asyncio.CancelledError: 非我方 stop 触发的外部取消原样上抛
        """
        task = asyncio.ensure_future(self._process_batch(batch, batch_no, chain))
        self._current_task = task
        try:
            await task
        except asyncio.CancelledError:
            if not self._stop:
                raise                              # 外部取消，不是我方中断
            # 批中途被中断：已冲洗的输出行依然有效
        finally:
            self._current_task = None
            # v1.11（V19，spec 3.10.3 上下文预算行）：批边界校准冻结——在本批落定**之后**、
            # 下一批派发**之前**，故第 N+1 批只按 ≤ N 批的聚合值装填（F8 确定性护栏）。
            # 每一个派发出去的批都经由这里（三条模式驱动皆然）：生成再流转子批作为独立外层
            # 批派发，冻结待遇与普通批相同——批内阶段循环里绝不冻结。
            self._freeze_calibrator()

    async def _process_batch(self, batch: list[PipelineItem], batch_no: int,
                             chain: Sequence[Stage]) -> None:
        """一批的完整生命周期：链上跑完 → 质量统计 → 落盘 → 状态清点 → 事件与冲洗。

        @param batch: 本批信封
        @param batch_no: 批号
        @param chain: 本批的阶段链
        """
        t_batch = time.perf_counter()
        self.metrics.event(_EV_BATCH_START, stage="run", batch_no=batch_no,
                           payload={"size": len(batch)})
        deltas = await self._run_chain(batch, batch_no, chain)
        if self.cfg.quality.enabled:
            self._collect_quality_stats(batch)

        emit = self.emitter.emit_batch(batch, batch_no)
        self._output_lines += emit.emitted
        self._rejects_lines += emit.rejected

        tally = self._tally_statuses(batch, emit)
        self.metrics.event(_EV_BATCH_END, stage="run", batch_no=batch_no,
                           payload=self._batch_end_payload(tally, deltas, t_batch))
        self.metrics.flush()                       # trace 冲洗跟在输出冲洗之后

    async def _run_chain(self, batch: list[PipelineItem], batch_no: int,
                         chain: Sequence[Stage]) -> tuple[int, int]:
        """按链序跑完一批的全部阶段，并计量 fanout / episodes 两个 len 差值。

        v1.8 §7.9 / v1.7 R9：``counts.episodes`` 与 ``counts.fanout`` **在此处**计量——
        segment / classify 调用前后的 len 差值（M14/M13 只就地追加信封、从不碰
        ``counts.*``；与从 generate 返回值导出 counts.generated 是同一构造）。

        @param batch: 本批信封（阶段可就地追加尾部信封）
        @param batch_no: 批号
        @param chain: 本批的阶段链
        @return: (本批 fanout 增量, 本批 episodes 增量)
        """
        batch_fanout = 0
        batch_episodes = 0
        for stage in chain:
            ctx = self._make_ctx(batch_no, stage.name)
            size_before = len(batch)
            # v1.10（U11，spec 3.10.3 console 行）：进程内进度信号，按链序在**每次**
            # stage.run 之前发——只转发、不产生 TraceEvent（3.12.3），无 listener 即 no-op。
            self.metrics.stage_begin(stage.name, batch_no)
            t_stage = time.perf_counter()
            try:
                result = await stage.run(batch, ctx)
            finally:
                elapsed = time.perf_counter() - t_stage
                self._stage_time[stage.name] = self._stage_time.get(stage.name, 0.0) + elapsed
                self.metrics.add_stage_time(stage.name, elapsed)
            delta = len(batch) - size_before
            if stage.name == "segment" and delta > 0:
                self.metrics.count("counts.episodes", delta)
                batch_episodes += delta
            if stage.name == "classify" and delta > 0:
                self.metrics.count("counts.fanout", delta)
                batch_fanout += delta
            if stage.name == "generate":
                self._enqueue_generated(result)
        return batch_fanout, batch_episodes

    def _enqueue_generated(self, result) -> None:
        """链外工位 generate 的产物入队：按 batch_size 切成子批，自 M3 重新入链（单轮）。

        @param result: generate 阶段返回的新子批（None 视同空）
        """
        sub = list(result) if result is not None else []
        if not sub:
            return
        self.metrics.count("counts.generated", len(sub))
        bs = self.cfg.run.batch_size
        for i in range(0, len(sub), bs):
            self._pending.append(sub[i:i + bs])

    def _tally_statuses(self, batch: list[PipelineItem], emit) -> dict[str, int]:
        """post-emit 状态清点并落 counts.*（此时 emitter 可能已把内部错误改判）。

        counts 不变量：既没落盘也没被丢弃的，一律算 failed（把 emitter 改判的
        internal_error 项也覆盖进来）。v1.8 起若不扣除 absorbed/dropped_noise，episode
        成员会被误计为 failed（§7.9）；v1.9（T7 blocker-1）stitched 同理入扣除项——壳是
        终态，不是失败。

        @param batch: 本批信封
        @param emit: M11 的落盘结果（emitted/rejected 计数）
        @return: 原始状态直方图（供 batch.end payload 读取）
        """
        tally: dict[str, int] = {}
        for item in batch:
            tally[item.status] = tally.get(item.status, 0) + 1
        deducted = {name: tally.get(name, 0) for name in _DEDUCTED_STATUSES}
        failed = max(len(batch) - emit.emitted - sum(deducted.values()), 0)
        self.metrics.count("counts.emitted", emit.emitted)
        for name, value in deducted.items():
            self.metrics.count(f"counts.{name}", value)
        self.metrics.count("counts.failed", failed)
        return tally

    def _batch_end_payload(self, tally: dict[str, int], deltas: tuple[int, int],
                           t_batch: float) -> dict:
        """组装 batch.end 事件 payload。

        v1.7 R20（§8.1）：batch.start.size 恒为批入口信封数，扇出增量由 batch.end 携带
        （仅 classify 启用时）；v1.8 的 episodes/absorbed/dropped_noise 与 v1.9 的
        stitched/threads 同款形制（仅对应开关启用时在场——m-11 关模式逐字节等价条件），
        stderr 进度/摘要行**不增键**（§7.9）。

        @param tally: 原始状态直方图
        @param deltas: (fanout 增量, episodes 增量)
        @param t_batch: 本批起始的 perf_counter 读数
        @return: 事件 payload
        """
        cfg = self.cfg
        batch_fanout, batch_episodes = deltas
        stitched = tally.get("stitched", 0)
        payload: dict = {"active": tally.get("active", 0),
                         "dropped_dup": tally.get("dropped_dup", 0),
                         "dropped_lowq": tally.get("dropped_lowq", 0),
                         "dropped_verify": tally.get("dropped_verify", 0),
                         "failed": tally.get("failed", 0),
                         "duration_ms": int((time.perf_counter() - t_batch) * 1000)}
        if cfg.classify.enabled:
            payload["fanout"] = batch_fanout
        if cfg.segment.enabled:
            payload["episodes"] = batch_episodes
            payload["absorbed"] = tally.get("absorbed", 0)
            payload["dropped_noise"] = tally.get("dropped_noise", 0)
        if cfg.stitch.enabled:
            payload["stitched"] = stitched
            payload["threads"] = batch_episodes - stitched
        return payload

    def _make_ctx(self, batch_no: int, stage_name: str) -> RunContext:
        """每 (批, 阶段) 新建 RunContext；rng 派生式冻结（spec 3.10.3）。

        @param batch_no: 批号
        @param stage_name: 阶段名
        @return: 该 (批, 阶段) 的运行上下文
        """
        return RunContext(cfg=self.cfg, llm=self.llm, schema_engine=self.schema_engine,
                          metrics=self.metrics,
                          rng=random.Random(f"{self.cfg.run.seed}:{batch_no}:{stage_name}"),
                          batch_no=batch_no)

    def _freeze_calibrator(self) -> None:
        """v1.11（V19/V23②，§7.17）：把刚跑完这批的按 profile 图片成本样本桶折进校准器的
        批最大值冻结窗口（对无序样本集取 max——asyncio 完成序永远漏不进可读快照）。每派发
        一批触发一次（_guarded_batch），finalize 时再触发一次，好让
        ``report.budget.image_cost`` 拿到校准**终值**（V13⑤）。与 _build_report 里的
        ``usage_by_profile`` 读法同为鸭子面——单测夹具传 llm=None；无样本的冻结是 no-op，
        故预算关闭的运行观察不到任何动静。
        """
        calibrator = getattr(self.llm, "calibrator", None)
        if calibrator is not None:
            calibrator.freeze_batch()

    def _budget_profiles(self) -> list[tuple[str, int, int]]:
        """v1.11（V13①②）：本轮实际解析到的 profile 中声明了预算的那些——
        referenced_profiles 的 LLM 与 embedding **两条腿**都算（spec §6.4：「任一被启用
        阶段引用的 profile」），筛 ``context_window > 0``（保序；V6 的「被启用阶段引用」
        口径）。条目为 (名字, context_window, input_budget)，其中 LLM profile 取
        budget.input_budget、embedding profile 取 budget.embed_budget（不预留输出，V15）。
        非空 ⇔ 预算观测面（启动 INFO 行、report.budget 节）在场；全未声明的运行输出与
        v1.10 逐字节一致（CONTRACTS §9.3 条款）。

        @return: (profile 名, 上下文窗, 输入预算) 三元组列表
        """
        llm_names, emb_names = referenced_profiles(self.cfg)
        declared: list[tuple[str, int, int]] = []
        for name in llm_names:
            prof = self.cfg.llm_profiles.get(name)
            if prof is not None and prof.context_window > 0:
                declared.append((name, prof.context_window,
                                 budget.input_budget(prof)))
        for name in emb_names:
            emb = self.cfg.embedding_profiles.get(name)
            if emb is not None and emb.context_window > 0:
                declared.append((name, emb.context_window,
                                 budget.embed_budget(emb)))
        return declared

    def _log_budget_startup(self) -> None:
        """v1.11（V13①，spec 3.10.3 上下文预算行）：打一行数据无关的 INFO 罗列已声明的
        预算参数——``budget: <name>=<cw>/<input_budget> ...``——并在 segment 启用且其
        profile 有预算时补一行 ``segment: w_min=<w_min> window=<cap> (budget)``（w_min 取
        budget.min_window 的原始值，即 V9 护栏/INFO 的打印值）。只有计数与参数，绝无数据
        内容（§2.6）；无任何被引用 profile 声明窗宽时静默。
        """
        declared = self._budget_profiles()
        if not declared:
            return
        _log.info("budget: %s",
                  " ".join(f"{name}={cw}/{ib}" for name, cw, ib in declared),
                  extra={"stage": "run", "batch": 0})
        cfg = self.cfg
        seg_prof = (cfg.llm_profiles.get(cfg.segment.llm)
                    if cfg.segment.enabled else None)
        if seg_prof is not None and seg_prof.context_window > 0:
            _log.info("segment: w_min=%d window=%d (budget)",
                      budget.min_window(cfg), cfg.segment.window,
                      extra={"stage": "run", "batch": 0})

    def _compose_chain(self, include_generate: bool) -> list[Stage]:
        """按 2.3.1 开关矩阵以规范链序组链。

        CLI 交进来的是已构造的启用算子；这里只负责按规范序重排并剔掉配置关闭的。
        ``generate`` 只出现在 process 模式的主批链上（再流转子批绝不含它，generate_only
        下它是链头另行驱动）。``classify`` 在主链、再流转链与 generate_only 链上**都**在
        （v1.7，§7.9）——已分类的信封靠 M13 的幂等跳过。

        @param include_generate: 是否把 generate 工位纳入本链
        @return: 按规范链序排好的阶段列表
        """
        cfg = self.cfg
        enabled = {
            "segment": cfg.segment.enabled,
            "stitch": cfg.stitch.enabled,
            "dedup": cfg.dedup.enabled,
            # v1.12 或门（与 factory.build_stages 同口径）：仅帧级分类开启时
            # ClassifyStage 仍须在链——序列级判决由 stage 内 classify.enabled 门控。
            "classify": cfg.classify.enabled or cfg.frame_classify.enabled,
            "extract": cfg.extract.enabled,
            "quality": cfg.quality.enabled,
            "generate": (cfg.generate.enabled and include_generate
                         and cfg.run.mode == "process"),
            "annotate": cfg.annotate.enabled,
            "verify": cfg.verify.enabled,
        }
        by_name = {s.name: s for s in self.stages}
        return [by_name[n] for n in _CHAIN_ORDER if enabled[n] and n in by_name]

    # ── 运行级统计汇总 ─────────────────────────────────────────────────────

    def _collect_quality_stats(self, batch: list[PipelineItem]) -> None:
        """把质量分汇进报表直方图与均值（报表组装归 M10，只记数、绝不记数据内容）。

        v1.7 R12：classify 启用时累加器额外按池（= item.classification.label）分列；
        classify 关闭时与 v1.6 的平坦路径逐字节一致。

        @param batch: 本批信封
        """
        classify_on = self.cfg.classify.enabled
        for item in batch:
            pool = (item.classification.label
                    if classify_on and item.classification is not None else None)
            agg = item.scores.get("__aggregate__")
            if agg is not None and agg.score is not None:
                bucket = min(int(agg.score * 10), 9)
                self._agg_hist[bucket] += 1
                if pool is not None:
                    self._pool_agg_hist.setdefault(pool, [0] * 10)[bucket] += 1
            for key, qs in item.scores.items():
                if key == "__aggregate__" or qs.score is None:
                    continue
                self._crit_sum[key] = self._crit_sum.get(key, 0.0) + qs.score
                self._crit_n[key] = self._crit_n.get(key, 0) + 1
                if pool is not None:
                    psum = self._pool_crit_sum.setdefault(pool, {})
                    pn = self._pool_crit_n.setdefault(pool, {})
                    psum[key] = psum.get(key, 0.0) + qs.score
                    pn[key] = pn.get(key, 0) + 1

    # ── finalize 与报表 ────────────────────────────────────────────────────

    def _finalize(self) -> RunSummary:
        """收尾：定退出码 → 组报表 → 交付 → 发 run.end。

        @return: 运行摘要
        """
        wall_s = time.perf_counter() - self._t0
        # v1.11（V19/V13⑤）：末批之后、组报表之前再冻结一次——report.budget.image_cost
        # 必须读到校准**终值**（每批派发边界已冻结过；当前桶为空时这次是 no-op）。
        self._freeze_calibrator()
        # 事实源是 MetricsSink 的标志位：熔断可能在一批的尾部调用上打开而
        # CircuitBreakerTripped 从未逃出任何阶段（在飞调用都先在记录级失败）——这轮仍须
        # 以 4 / 未交付收尾。
        self._circuit_broken = (self._circuit_broken
                                or bool(getattr(self.metrics, "circuit_broken", False)))
        if self._circuit_broken:
            exit_code = 4
        elif self.cfg.generate_stream.enabled and self.metrics.counters.get(
                "generate.stream.delivery.incomplete", 0):
            exit_code = 1
        elif self.cfg.strict and self._rejects_lines > 0:
            exit_code = 1
        else:
            exit_code = 0
        report = self._build_report(exit_code=exit_code, wall_s=wall_s)
        # v1.6 熔断交付（spec 3.10.3，1.6 对齐决策 ②）：熔断**同样**交付已完成的批——
        # fsync + 原子改名，报告标 run.partial_delivery=true 并以 counts.unprocessed 作
        # 平衡残差。这里的 deliver=True 是无条件的；emitter 在通道写失败后仍会拒绝改名
        # （_undeliverable），而 dry-run 走的是另一处的 deliver=False。
        self.emitter.finalize(report, deliver=True)
        self.metrics.event(_EV_RUN_END, stage="run", batch_no=0,
                           payload={"counts": report["counts"], "exit_code": exit_code})
        self.metrics.flush()
        return RunSummary(counts=report["counts"], interrupted=self._interrupted,
                          exit_code=exit_code, wall_s=wall_s,
                          output_lines=self._output_lines,
                          rejects_lines=self._rejects_lines)

    def _build_report(self, exit_code: int, wall_s: float) -> dict:
        """从 ingestor.report、metrics 计数器、schema_engine.stats、llm.usage_by_profile
        与阶段计时组装 §9.3 报表字典（顶层键序冻结）。

        @param exit_code: 本轮退出码
        @param wall_s: 运行墙钟秒数
        @return: 报表字典
        """
        cfg = self.cfg
        c = _CounterView(dict(getattr(self.metrics, "counters", {}) or {}))
        ingest_report = getattr(self.ingestor, "report", None) if self.ingestor else None
        counts = self._report_counts(c, ingest_report)
        report: dict = {"run": self._report_run_block(exit_code), "counts": counts}
        if cfg.dry_run:
            # dry-run 报告直接引用本次唯一 estimate_run 结果，避免 console/JSON 漂移。
            report["estimate"] = self._estimate_cache or self._estimate()
        if cfg.segment.enabled:
            report["stream"] = self._report_stream(c, counts, ingest_report)
        if cfg.dedup.enabled:
            report["dedup"] = self._report_dedup(c)
        if cfg.quality.enabled:
            report["quality"] = self._report_quality(c)
        stats = getattr(self.schema_engine, "stats", None) if self.schema_engine else None
        report["schema_engine"] = {
            "resolved_at": dict(stats) if stats else dict(_SCHEMA_STATS_ZERO)}
        if cfg.annotate.enabled and cfg.annotate.self_consistency >= 3:
            report["annotate"] = {"sc_disagreements": c("annotate.sc_disagreements")}
        if cfg.generate.enabled:
            report["generate"] = self._report_generate(c)
        if cfg.classify.enabled:
            report["classify"] = self._report_classify(c)
        budget_block = self._report_budget(c)
        if budget_block is not None:
            report["budget"] = budget_block
        report["trace"] = self._report_trace()
        report["llm_usage"] = self._report_llm_usage()
        report["timing"] = {
            "wall_s": round(wall_s, 3),
            "per_stage_s": {name: round(seconds, 3)
                            for name, seconds in self._stage_time.items()},
        }
        return report

    def _report_counts(self, c: _CounterView, ingest_report) -> dict:
        """组装 counts 节（键的在场条件即 §9.3 的只增约定）。

        v1.7 R9/R10：fanout 键只在 multi 分配下出现（single 永不扇出；计数由
        _run_chain 的 len 差值计量喂入，M10 属主）。v1.8 只增：segment 启用时三个流计数
        才出现（episodes 来自 len 差值计量，absorbed/dropped_noise 来自 post-emit 清点）。
        v1.9 只增（T7）：stitched 来自 post-emit 清点，threads 是**单点导出**
        threads = episodes − stitched（不设第二计数器——T16 双落点护栏）。

        @param c: 计数器视图
        @param ingest_report: M2 摄取报表（generate_only 下为 None）
        @return: counts 字典
        """
        cfg = self.cfg
        counts = {
            "scanned": int(getattr(ingest_report, "scanned", 0)),
            "ingested": int(getattr(ingest_report, "ingested", 0)),
            "bad_input": int(getattr(ingest_report, "bad_input", 0)),
            "dropped_dup": c("counts.dropped_dup"),
            "dropped_lowq": c("counts.dropped_lowq"),
            "dropped_verify": c("counts.dropped_verify"),
            "failed": c("counts.failed"),
            "generated": c("counts.generated"),
            "emitted": c("counts.emitted"),
        }
        if cfg.classify.enabled and cfg.classify.assignment == "multi":
            counts["fanout"] = c("counts.fanout")
        if cfg.segment.enabled:
            counts["episodes"] = c("counts.episodes")
            counts["absorbed"] = c("counts.absorbed")
            counts["dropped_noise"] = c("counts.dropped_noise")
        if cfg.stitch.enabled:
            counts["stitched"] = c("counts.stitched")
            counts["threads"] = counts["episodes"] - counts["stitched"]
        if self._circuit_broken or (cfg.segment.enabled and self._interrupted):
            counts["unprocessed"] = self._unprocessed_residual(counts)
        return counts

    def _unprocessed_residual(self, counts: dict) -> int:
        """counts.unprocessed = 平衡残差，使守恒式扩展为 emitted + dropped_* + failed +
        bad_input + unprocessed = scanned + generated [+ fanout] [+ episodes]（即进了流水线
        却没走到任何终态计数的记录，含 generate_only 里生成了但从未装批的记录；fanout 项
        是 v1.7 R10——扇出兄弟也是信封）。v1.8（S18）：**流模式**下该键在中断的运行上也出现
        （SIGINT 叠加会话缓冲会滞留在飞记录），两侧同步扩展——源侧 + episodes，终态侧
        + absorbed + dropped_noise。非流的中断运行残差可证为零、永不加键（回归锚）。

        @param counts: 已填好各终态的 counts 字典
        @return: 非负残差
        """
        residual = (counts["scanned"] + counts["generated"]
                    + counts.get("fanout", 0)
                    + counts.get("episodes", 0)
                    - counts["emitted"] - counts["dropped_dup"]
                    - counts["dropped_lowq"] - counts["dropped_verify"]
                    - counts["failed"] - counts["bad_input"]
                    - counts.get("absorbed", 0)
                    - counts.get("dropped_noise", 0)
                    - counts.get("stitched", 0))   # v1.9（T7）：壳是终态
        return max(0, residual)

    def _report_run_block(self, exit_code: int) -> dict:
        """组装 run 节（工具版本、起止时刻、中断/熔断标志、模态、种子、两个摘要）。

        @param exit_code: 本轮退出码
        @return: run 节字典
        """
        cfg = self.cfg
        run_block: dict = {
            "tool_version": __version__,
            "started_at": self.run_started_at.isoformat(),
            "finished_at": datetime.now().astimezone().isoformat(),
            "interrupted": self._interrupted,
            # 显式熔断标志（E2E 发现 P4-10）：熔断时 interrupted 仍为 false——只看
            # exit_code=4 太容易读错。
            "circuit_broken": self._circuit_broken,
            "exit_code": exit_code,
            "modality": cfg.run.modality,
            "seed": cfg.run.seed,
            "config_digest": cfg.config_digest,
            "project_digest": cfg.project_digest,
        }
        run_block["paths"] = self._report_paths()
        artifact = getattr(self.emitter, "artifact_summary", None)
        if artifact:
            # v1.13（裁决·观测面）：run 摘要族的工件条目（路径/sha256/行数，主输出
            # 同款形态）——仅工件通道实际写入时在场（dry-run/形态关闭恒缺席）。
            run_block["artifact"] = dict(artifact)
        if self._circuit_broken:
            # v1.6 熔断交付（spec 6.4，只增）：partial_delivery 仅熔断交付时在场。
            run_block["partial_delivery"] = True
        return run_block

    def _report_paths(self) -> dict:
        """输出九键绝对路径；旧 fixture 缺少 paths 时按已知字段归一化。"""
        paths = getattr(self.cfg, "paths", None)
        if paths is not None:
            return {
                "project": paths.project, "project_root": paths.project_root,
                "input": paths.input, "output": paths.output,
                "report": paths.report, "rejects": paths.rejects,
                "sidecar": paths.sidecar, "trace": paths.trace,
                "stream_artifact": paths.stream_artifact,
            }
        project = str(Path(self.cfg.project_path).resolve())
        output = str(Path(self.cfg.run.output).resolve())
        stem = str(Path(output).with_suffix(""))
        return {
            "project": project, "project_root": str(Path(project).parent),
            "input": (str(Path(self.cfg.run.input).resolve())
                      if self.cfg.run.input else None),
            "output": output,
            "report": stem + (".dryrun.report.json" if self.cfg.dry_run else ".report.json"),
            "rejects": None if self.cfg.output.rejects == "none" else stem + ".rejects.jsonl",
            "sidecar": stem + ".meta.jsonl" if self.cfg.output.meta_mode == "sidecar" else None,
            "trace": (str(Path(self.cfg.trace.path).resolve())
                      if self.cfg.trace.enabled and self.cfg.trace.path else None),
            "stream_artifact": stem + ".stream.jsonl" if self.cfg.generate_stream.enabled else None,
        }

    def _report_stream(self, c: _CounterView, counts: dict, ingest_report) -> dict:
        """组装 v1.8 stream 节（§9.3/spec §6.4：紧跟 counts 之后）。

        sessions 的数据源是 IngestReport（M2 属主，§7.1）；below_min_len /
        digest_poor_frames / segment_failures 直出 M14 计数器；各子块按闭集词表直出
        M15/M16/M13/M5/M7 的计数器（零基，与 report.classify.classes 同款）。

        @param c: 计数器视图
        @param counts: 已组装的 counts 节
        @param ingest_report: M2 摄取报表
        @return: stream 节字典
        """
        cfg = self.cfg
        episodes = counts["episodes"]
        absorbed = counts["absorbed"]
        block: dict = {
            "sessions": int(getattr(ingest_report, "sessions", 0)),
            "episodes": episodes,
            "mean_episode_len": (round(absorbed / episodes, 2) if episodes else 0.0),
            "absorbed": absorbed,
            "dropped_noise": counts["dropped_noise"],
            "below_min_len": c("segment.below_min_len"),
            "digest_poor_frames": c("segment.digest_poor_frames"),
            "segment_failures": c("segment.failures"),
        }
        block.update(self._report_stream_operators(c))
        return block

    # v1.9（T16，链序槽位在 extract 之前）stitch：stitched 镜像 counts.stitched，其余五键
    # 直出 M16 计数器。v1.12（spec §3.7 report 行）frame_classify 在 stitch 之后、extract
    # 之前，frame_annotate 在 extract 之后、verify 之前：两者各四键零基闭集，计数面分别由
    # M13 帧 pass 与 M5 帧 pass 供给（后者的 failed 另含 M11 写前校验兜底，discarded 由
    # M11 沉没成本记账供给）。
    def _report_stream_operators(self, c: _CounterView) -> dict:
        """stream 节里按链序排布的算子子块（各自以开关为门）。

        @param c: 计数器视图
        @return: 按链序排好的子块字典（未启用的开关不出现）
        """
        cfg = self.cfg
        blocks: dict = {}
        if cfg.stitch.enabled:
            blocks["stitch"] = {
                "stitched": c("counts.stitched"),
                "rescued_short": c("stitch.rescued_short"),
                "seams": c("stitch.seams"),
                "judgments": c("stitch.judgments"),
                "repass_judgments": c("stitch.repass_judgments"),
                "failures": c("stitch.failures"),
            }
        if cfg.frame_classify.enabled:
            blocks["frame_classify"] = {
                "calls": c("frame_classify.calls"),
                "fallback": c("frame_classify.fallback"),
                "window_failures": c("frame_classify.window_failures"),
                "skipped_degraded": c("frame_classify.skipped_degraded"),
            }
        if cfg.extract.enabled:
            blocks["extract"] = {
                "transitions": c("extract.transitions"),
                "fallback_steps": c("extract.fallback_steps"),
                "failures": c("extract.failures"),
                "by_type": {t: c(f"extract.by_type.{t}") for t in _ACTION_TYPES},
            }
        if cfg.frame_annotate.enabled:
            blocks["frame_annotate"] = {
                "annotated": c("frame_annotate.annotated"),
                "skipped": c("frame_annotate.skipped"),
                "failed": c("frame_annotate.failed"),
                "discarded": c("frame_annotate.discarded"),
            }
        if cfg.verify.enabled:
            blocks["verify"] = {
                "membership_repairs": c("verify.membership_repairs"),
                "boundary_flags": c("verify.boundary_flags"),
                "defects": {k: c(f"verify.defects.{k}") for k in _DEFECT_KINDS},
            }
        return blocks

    def _report_dedup(self, c: _CounterView) -> dict:
        """组装 dedup 节（语义层两键仅 semantic 开启时在场）。

        @param c: 计数器视图
        @return: dedup 节字典
        """
        block = {
            "exact": c("dedup.exact"),
            "near_text": c("dedup.near_text"),
            "near_image": c("dedup.near_image"),
            "near_both": c("dedup.near_both"),
            "clusters": c("dedup.clusters"),
            "image_decode_failures": c("dedup.image_decode_failures"),
        }
        if self.cfg.dedup.semantic:
            block["near_semantic"] = c("dedup.near_semantic")
            block["embedding_failures"] = c("dedup.embedding_failures")
        return block

    def _report_quality(self, c: _CounterView) -> dict:
        """组装 quality 节。

        顶层 mode/rounds 即便存在按类覆盖也保持全局继承的基值（v1.7 R14）；各池的实效值由
        by_class 承载。平局率发射门（v1.7 R14）：全局 pairwise，或——classify 启用时——
        至少存在一个 pairwise 池。pairwise 的百分位均值按构造恒 ≈ 0.5，平局率才是有区分度
        的按准则信号（E2E P4-9）。

        @param c: 计数器视图
        @return: quality 节字典
        """
        cfg = self.cfg
        block: dict = {
            "mode": "pairwise_bt" if cfg.quality.mode == "pairwise" else "pointwise",
            "rounds": cfg.quality.rounds,
            "judgment_failures": c("quality.judgment_failures"),
            "aggregate_histogram": {label: self._agg_hist[i]
                                    for i, label in enumerate(_HIST_LABELS)},
            "per_criterion_mean": {key: self._crit_sum[key] / self._crit_n[key]
                                   for key in sorted(self._crit_sum)
                                   if self._crit_n.get(key)},
        }
        tie_rate, pool_tie_rate = self._quality_tie_rates(c)
        any_pairwise_pool = cfg.classify.enabled and any(
            view.quality.mode == "pairwise" for view in cfg.class_views.values())
        if cfg.quality.mode == "pairwise" or any_pairwise_pool:
            block["per_criterion_tie_rate"] = tie_rate
        if cfg.classify.enabled:
            block["by_class"] = self._quality_by_class(pool_tie_rate)
        return block

    def _quality_tie_rates(self, c: _CounterView) -> tuple[dict, dict]:
        """解析平局计数器为按准则平局率与按池平局率。

        v1.7 R12：classify 启用时平局计数器是按池分维的
        （quality.tie_outcomes.<pool>.<crit>），一趟解析同时产出按池率与跨池聚合率。

        @param c: 计数器视图
        @return: (按准则平局率, 按池按准则平局率)
        """
        prefix = "quality.tie_outcomes."
        tie_rate: dict[str, float] = {}
        pool_tie_rate: dict[str, dict[str, float]] = {}
        if not self.cfg.classify.enabled:
            for key, ties in sorted(c.values.items()):
                if not key.startswith(prefix):
                    continue
                crit = key[len(prefix):]
                comps = c.values.get(f"quality.tie_comparisons.{crit}", 0)
                if comps:
                    tie_rate[crit] = ties / comps
            return tie_rate, pool_tie_rate
        crit_ties: dict[str, int] = {}
        crit_comps: dict[str, int] = {}
        for key, ties in sorted(c.values.items()):
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix):]
            if "." not in rest:                    # 畸形/平坦键：跳过
                continue
            pool, _, crit = rest.partition(".")
            comps = c.values.get(f"quality.tie_comparisons.{pool}.{crit}", 0)
            if comps:
                pool_tie_rate.setdefault(pool, {})[crit] = ties / comps
                crit_ties[crit] = crit_ties.get(crit, 0) + ties
                crit_comps[crit] = crit_comps.get(crit, 0) + comps
        tie_rate = {crit: crit_ties[crit] / crit_comps[crit]
                    for crit in sorted(crit_ties) if crit_comps.get(crit)}
        return tie_rate, pool_tie_rate

    def _quality_by_class(self, pool_tie_rate: dict) -> dict:
        """v1.7 R12/R14 的 quality.by_class：每个**声明类**一条（零基，与
        report.classify.classes 同款）；mode/rounds 取该池自 cfg.class_views 的**实效值**。

        @param pool_tie_rate: 按池按准则平局率
        @return: by_class 字典
        """
        by_class: dict[str, dict] = {}
        for pool in sorted(self.cfg.class_views):
            view_q = self.cfg.class_views[pool].quality
            hist = self._pool_agg_hist.get(pool, [0] * 10)
            psum = self._pool_crit_sum.get(pool, {})
            pn = self._pool_crit_n.get(pool, {})
            by_class[pool] = {
                "mode": "pairwise_bt" if view_q.mode == "pairwise" else "pointwise",
                "rounds": view_q.rounds,
                "aggregate_histogram": {label: hist[i]
                                        for i, label in enumerate(_HIST_LABELS)},
                "per_criterion_mean": {key: psum[key] / pn[key]
                                       for key in sorted(psum) if pn.get(key)},
                "per_criterion_tie_rate": pool_tie_rate.get(pool, {}),
            }
        return by_class

    def _report_generate(self, c: _CounterView) -> dict:
        """组装 generate 节（按桶计数 + v1.13 时间流子块）。

        rejected_by_validator 已并入白名单（bug 修复，spec v1.7 §6：M6 自 v1.5 起就在计数，
        报表解析却静默丢弃）；零初始化保住三个恒在字段，第四个仅在其计数器出现时（即配了
        validator）才写。

        @param c: 计数器视图
        @return: generate 节字典
        """
        buckets: dict[str, dict] = {}
        prefix = "generate.buckets."
        for key, value in c.values.items():
            if not key.startswith(prefix):
                continue
            bucket, _, field_name = key[len(prefix):].rpartition(".")
            if not bucket or field_name not in ("calls", "produced", "survived_dedup",
                                                "rejected_by_validator"):
                continue
            buckets.setdefault(bucket, {"calls": 0, "produced": 0,
                                        "survived_dedup": 0})[field_name] = int(value)
        block: dict = {"buckets": buckets}
        if self.cfg.generate_stream.enabled:
            block["stream"] = self._report_generate_stream(c)
        return block

    def _report_generate_stream(self, c: _CounterView) -> dict:
        """v1.13（裁决·观测面）时间流子块——counts-only，形态开启才在场；键集与键序冻结；
        sequences 按声明类零基（report.classify.classes 同款），计数面由 M6 供给
        （generate.stream.* 前缀）。

        v1.14（裁决·报表显式装配）：档位表非空时在 sequences 之后、frames 之前插入 tiers
        子块（配额族相邻）——本节是显式键装配而非计数器前缀树，零额档与全作废档的在场由
        装配保证，不依赖计数器首触序。

        @param c: 计数器视图
        @return: generate.stream 子块字典
        """
        plan = self.cfg.scenario_plan
        block: dict = {
            "sessions": len(plan.sessions),
            "crossed_sessions": self.cfg.generate_stream.crossed_sessions,
            "sequences": {
                spec.name: {
                    "planned": c(f"generate.stream.sequences.{spec.name}.planned"),
                    "produced": c(f"generate.stream.sequences.{spec.name}.produced"),
                }
                for spec in self.cfg.classify.classes
            },
        }
        if self.cfg.generate_stream.tiers:
            block["tiers"] = self._report_stream_tiers(c)
        self._append_stream_constraint_report(block, c)
        block.update({
            "frames": c("generate.stream.frames"),
            "noise_frames": c("generate.stream.noise_frames"),
            "duplicates": c("generate.stream.duplicates"),
            "brief_calls": c("generate.stream.brief_calls"),
            "realize_calls": c("generate.stream.realize_calls"),
            "noise_calls": c("generate.stream.noise_calls"),
        })
        self._append_stream_plan_report(block, c)
        return block

    def _append_stream_plan_report(self, block: dict, c: _CounterView) -> None:
        """追加 v1.17 planner、delivery 与 quota 报表面。"""
        plan = self.cfg.scenario_plan
        block["plan_digest"] = plan.plan_digest
        block["planner"] = {
            "models": {
                name: {"entries": stats.variables + stats.constraints,
                       "families": {family: {"variables": row.variables,
                                             "constraints": row.constraints}
                                    for family, row in stats.families.items()}}
                for name, stats in plan.models.items()
            },
            "objectives": {
                "preference_deviation": plan.objectives.preference_deviation,
                "calendar_days_spanned": plan.objectives.calendar_days_spanned,
                "timeline_end_us": plan.objectives.timeline_end_us,
            },
        }
        target_sequences = len(plan.slots)
        delivered_sequences = sum(
            c(f"generate.stream.sequences.{spec.name}.produced")
            for spec in self.cfg.classify.classes)
        target_noise = len(plan.noise_slots)
        delivered_noise = c("generate.stream.noise_frames")
        target_duplicates = len(plan.duplicates)
        delivered_duplicates = c("generate.stream.duplicates")
        failure_keys = (
            "brief", "realize", "noise", "context_overflow",
            "sample_validator", "sample_validator_exception", "correlation",
            "temporal", "sequence_validator", "sequence_validator_exception",
            "similarity", "scenario_validator", "scenario_validator_exception")
        failures = {key: c(f"generate.stream.delivery.failures.{key}")
                    for key in failure_keys}
        attempts = c("generate.stream.delivery.attempts")
        complete = (c("generate.stream.delivery.incomplete") == 0
                    and delivered_sequences == target_sequences
                    and delivered_noise == target_noise
                    and delivered_duplicates == target_duplicates)
        block["delivery"] = {
            "target_sequences": target_sequences,
            "delivered_sequences": delivered_sequences,
            "target_noise": target_noise,
            "delivered_noise": delivered_noise,
            "target_duplicates": target_duplicates,
            "delivered_duplicates": delivered_duplicates,
            "duplicate_shortfall": max(target_duplicates - delivered_duplicates, 0),
            "attempts": attempts,
            "complete": complete,
            "interrupted": self._interrupted,
            "exhausted_slots": c("generate.stream.delivery.exhausted"),
            "failures": failures,
        }
        allocations = {quota.name: quota.allocation for quota in self.cfg.generate_stream.quotas}
        block["quotas"] = [
            {"name": row.name, "period": row.period, "bucket": row.bucket,
             "class": row.sequence_class, "target": row.target,
             "delivered": c(f"generate.stream.sequences.{row.sequence_class}.produced"),
             "allocation": allocations.get(row.name),
             "realized_ratio": (c(f"generate.stream.sequences.{row.sequence_class}.produced")
                                 / row.target if row.target else 0.0),
             "deviation": c(f"generate.stream.sequences.{row.sequence_class}.produced")
                          - row.target}
            for row in plan.quota_summary
        ]

    def _append_stream_constraint_report(self, block: dict, c: _CounterView) -> None:
        """按实际配额前缀追加 v1.16 条件报表块。

        @param block: 正在组装的 generate.stream 子块
        @param c: 计数器视图
        """
        rules_active, windows_active = self._stream_constraint_faces()
        if rules_active:
            block["frame_rules"] = {
                "sampled": c("generate.stream.frame_rules.sampled"),
            }
        v116_face_active = (
            rules_active or windows_active
            or bool(self.cfg.generate.sequence_validator)
        )

    def _stream_constraint_faces(self) -> tuple[bool, bool]:
        """按冻结计划的参与类判断 frame_rules/frame_windows 报表条件面。"""
        rules_active = False
        windows_active = False
        for slot in self.cfg.scenario_plan.slots:
            view = self.cfg.class_views[slot.sequence_class]
            rules_active |= bool(effective_frame_rules(
                view.frame_rules, self.cfg.generate_stream.frame_rules))
            windows_active |= bool(effective_frame_windows(
                view.frame_windows, self.cfg.generate_stream.frame_windows))
        return rules_active, windows_active

    def _report_stream_tiers(self, c: _CounterView) -> dict:
        """v1.14 档位配额子块：按声明档位表零基铺开 planned / produced（v1.15 双形）。

        平面形（全部序列类都回落全局表）= v1.14 原形 ``{"<rank>": …}``，逐 rank 对**全部
        声明类**的类段计数跨类求和 —— 与 v1.14 报表逐字节相等（彼时的平面计数本就是跨类
        聚合值）。类嵌套形（任一按类表在场；裁决·嵌套报表全类铺开）=
        ``{"<class>": {"<rank>": …}}``，外层全部声明类按声明序、内层该类**生效表** rank
        升序。零配额类与全作废档一律呈现 0/0（显式装配，不依赖计数器首触序）。

        @param c: 计数器视图
        @return: 平面形或类嵌套形的 tiers 子块（十进制字符串键，键序 = 装配插入序）
        """
        cfg, views = self.cfg, self.cfg.class_views
        names = [spec.name for spec in cfg.classify.classes]
        if not any(view.tiers is not None for view in views.values()):
            return self._tier_ranks(c, names, cfg.generate_stream.tiers)
        return {name: self._tier_ranks(c, [name], effective_tiers(
            views[name].tiers if name in views else None, cfg.generate_stream.tiers))
            for name in names}

    def _tier_ranks(self, c: _CounterView, names: list[str],
                    table: "Sequence[TierSpec]") -> dict:
        """把一张档位表铺成 ``{"<rank>": {planned, produced}}``，逐 rank 跨给定类段求和。

        @param c: 计数器视图
        @param names: 参与求和的序列类名（平面形 = 全部声明类；嵌套形 = 该类一个）
        @param table: 该形态下的档位表（M1 解析期已按 tier_rank 升序定序）
        @return: rank 升序的十进制字符串键字典
        """
        return {
            str(spec.tier_rank): {
                field: sum(c(f"generate.stream.tiers.{name}.{spec.tier_rank}.{field}")
                           for name in names)
                for field in ("planned", "produced")
            }
            for spec in table
        }

    def _report_classify(self, c: _CounterView) -> dict:
        """v1.7 §9.3 classify 节：classes 直方图对**全部声明类**零基铺开（声明序）；计数器
        归 M13 所有（classify.fallback 以 fallback_count 露面）。

        @param c: 计数器视图
        @return: classify 节字典
        """
        cfg = self.cfg
        block: dict = {
            "assignment": cfg.classify.assignment,
            "classes": {spec.name: c(f"classify.classes.{spec.name}")
                        for spec in cfg.classify.classes},
            "fallback_count": c("classify.fallback"),
            "failures": c("classify.failures"),
        }
        if cfg.classify.assignment == "multi":
            block["multi_label_records"] = c("classify.multi_label_records")
        return block

    def _report_budget(self, c: _CounterView) -> dict | None:
        """v1.11 report.budget（V13②④⑤；键名冻结于 §9.3）：**整节**仅在 ≥ 1 个本轮被引用
        的 profile 声明了窗宽时才出现——全未声明时 report.json 与 v1.10 逐字节一致。只记数
        与统计，绝无数据内容（§2.6）。M10 在组报表时自 ResolvedConfig、budget.min_window
        与 llm.calibrator 组装 profiles/w_min/image_cost，其余键直出算子属主的计数器。

        image_cost = 各 profile 的校准**终值**（V19；上面 finalize 的冻结已把末批折进去）。
        最小忠实形态：只列校准器真正采过样的 profile（≥ 1 个冻结图片样本——低于最小样本数时
        cost() 读到的仍是先验 ×1.2 的装填值）；样本台账取校准器批冻结的 _frozen_total，与
        上面 metrics/_event_log 同为鸭子读法。

        @param c: 计数器视图
        @return: budget 节字典；无声明预算的 profile 时返回 None
        """
        budget_profiles = self._budget_profiles()
        if not budget_profiles:
            return None
        block: dict = {
            "profiles": {name: {"context_window": cw, "input_budget": ib}
                         for name, cw, ib in budget_profiles},
        }
        if self.cfg.segment.enabled:
            # 冻结子键 "segment.window" 下的 [cap, w_min]——w_min 是 budget.min_window 的
            # **原始**值（按设计不带上限：钳位发生在估算自己的调用点，V12/V26）。
            block["w_min"] = {"segment.window": [self.cfg.segment.window,
                                                 budget.min_window(self.cfg)]}
        trunc_prefix = "budget.truncations."
        block["truncations"] = {key[len(trunc_prefix):]: int(value)
                                for key, value in sorted(c.values.items())
                                if key.startswith(trunc_prefix) and value}  # 只列非零阶段
        block["overflow_records"] = c("budget.overflow_records")
        calibrator = getattr(self.llm, "calibrator", None)
        frozen_totals = dict(getattr(calibrator, "_frozen_total", None) or {})
        block["image_cost"] = {name: int(calibrator.cost(name))
                               for name in sorted(frozen_totals) if frozen_totals[name] > 0}
        block["degrade_retries"] = c("budget.degrade_retries")
        block["escalations"] = c("budget.escalations")
        return block

    def _report_trace(self) -> dict:
        """组装 trace 节。

        终局的 run.end 事件要等本报表组装完才发（它的 payload 带报表 counts，且 §8.1 规定它
        是 trace 的最后一行、写在 finalize 之后）。这里预先把它记上，好让 report.trace 与最终
        的 trace 文件对得上：通道还开着就多记一行已写，写失败已关闭通道就多记一条丢弃。

        @return: trace 节字典
        """
        event_log = (getattr(self.metrics, "event_log", None)
                     or getattr(self.metrics, "_event_log", None))
        trace_events = int(getattr(event_log, "events_written", 0) or 0)
        trace_dropped = int(getattr(event_log, "dropped_events", 0) or 0)
        if self.cfg.trace.enabled:
            if getattr(event_log, "closed", False):
                trace_dropped += 1
            else:
                trace_events += 1
        return {
            "enabled": self.cfg.trace.enabled,
            # EventLog 可能写在改道后的路径上（dry-run 用 "<name>.dryrun<suffix>"，
            # P2-4）——报**实际**文件。
            "path": (getattr(getattr(event_log, "cfg", None), "path", None)
                     or self.cfg.trace.path),
            "events": trace_events,
            "dropped_events": trace_dropped,
        }

    def _report_llm_usage(self) -> dict:
        """组装 llm_usage 节（零活动 profile 略去，保持 v1.5 报表形态）。

        @return: llm_usage 节字典
        """
        usage_by_profile = getattr(self.llm, "usage_by_profile", None) if self.llm else None
        llm_usage: dict[str, dict] = {}
        for name, usage in (usage_by_profile or {}).items():
            entry = _usage_entry(usage)
            if entry is not None:
                llm_usage[name] = entry
        return llm_usage

    # ── dry-run ────────────────────────────────────────────────────────────

    def _run_dry(self) -> RunSummary:
        """--dry-run：M1 已通过；跑 M2 扫描（process 模式）或 3.6.2 静态量公式
        （generate_only），把调用/成本估算打到 stderr，写报告，**不发**任何 LLM 调用、
        **不产**主输出与 rejects（Emitter.open 从不被调用）。trace 通道——一个 opt-in 的
        一等输出通道（spec 2.6），只承载运行事件、绝无数据内容——在 trace.enabled 时照常
        收到它的 run.start / run.end 生命周期事件。

        @return: 运行摘要（退出码恒 0）
        """
        cfg = self.cfg
        self.metrics.event(_EV_RUN_START, stage="run", batch_no=0,
                           payload={"tool_version": TOOL_VERSION,
                                    "config_digest": cfg.config_digest,
                                    "project_digest": cfg.project_digest,
                                    "trace_schema_version": 1})
        est = self._estimate()
        self._estimate_cache = est
        if (cfg.console.mode_resolved == "rich"
                and getattr(self.metrics, "has_listener", False)):
            # v1.10（U13）：rich 档且挂了 listener——估算打印行让位于渲染器的表格（数值
            # 逐项一致，经旁路送达）；plain 档走下面逐字节一致的行式输出（dry-run 黄金锚，
            # U24 第 ② 层）。
            self.metrics.run_estimate(est)
        else:
            self._print_dry_estimate(est)

        wall_s = time.perf_counter() - self._t0
        report = self._build_report(exit_code=0, wall_s=wall_s)
        self.emitter.finalize(report, deliver=False)   # 只写报告；不存在 .part
        self.metrics.event(_EV_RUN_END, stage="run", batch_no=0,
                           payload={"counts": report["counts"], "exit_code": 0})
        self.metrics.flush()
        return RunSummary(counts=report["counts"], interrupted=False, exit_code=0,
                          wall_s=wall_s, output_lines=0, rejects_lines=0)

    def _print_dry_estimate(self, est: dict) -> None:
        """plain 档 dry-run 的 stderr 行式输出（逐字节回归锚）。

        v1.12：帧粒度两键按冻结键序**无条件**打印（非流工程恒 = 0，v1.9 stitch_calls 先例）。

        @param est: estimate_run 的返回字典
        """
        cfg = self.cfg
        print(f"dry-run: mode={cfg.run.mode} estimated_records={est['records']} "
              f"batches={est['batches']}", file=sys.stderr)
        print(f"dry-run: estimated LLM calls — generate_calls={est['generate_calls']} "
              f"segment_calls={est['segment_calls']} "
              f"stitch_calls={est['stitch_calls']} "
              f"classify_calls={est['classify_calls']} "
              f"frame_classify_calls={est['frame_classify_calls']} "
              f"extract_calls={est['extract_calls']} "
              f"quality_calls={est['quality_calls']} annotate_calls={est['annotate_calls']} "
              f"frame_annotate_calls={est['frame_annotate_calls']} "
              f"verify_calls={est['verify_calls']} total={est['total_calls']} "
              f"(excludes retries and repair calls)", file=sys.stderr)
        self._print_dry_notes()
        side_channels = "report and trace only" if cfg.trace.enabled else "report only"
        print(f"dry-run: no LLM calls made, no output written ({side_channels})",
              file=sys.stderr)

    def _print_dry_notes(self) -> None:
        """两条口径注记（措辞固定，与 rich 面板的注记文案严格一致）。

        v1.7 R28：按类覆盖会让静态估算不精确，multi 扇出又把下游调用数乘上一个（不可知的）
        标签数——两者都用固定措辞标注。v1.8 S22（R28 式）：下游估算按 episodes ≈ sessions，
        而 LLM 边界精化只会**增加**段数，故这些数字是下界。v1.11（V12，spec 3.10.3 时序流
        行）：预算装填低于窗宽上限（w_min < window）时，该注记**追加一句**把 segment_calls
        标为最坏装填上界；w_min ≥ window（V26 的 examples）或预算关闭时注记逐字节不变
        （dry-run 黄金锚）。
        """
        cfg = self.cfg
        if cfg.classify.enabled and (cfg.classify.assignment == "multi"
                                     or self._class_overrides_exist()):
            print("dry-run: note: estimated with global config / multi reports a "
                  "lower bound at label multiplier 1", file=sys.stderr)
        if cfg.segment.enabled and cfg.segment.strategy in ("llm", "hybrid"):
            note = ("dry-run: note: stream estimate: downstream reports a lower bound "
                    "at episodes≈sessions (LLM refinement only adds segments)")
            if budget.min_window(cfg) < cfg.segment.window:
                note += "; segment reports an upper bound at worst-case budget packing"
            print(note, file=sys.stderr)

    def _class_overrides_exist(self) -> bool:
        """是否至少存在一处偏离全局节的 [class.*] 覆盖（class_views 对**每个声明类**都持有
        一份合并视图，故仅凭非空说明不了什么——必须与全局基值逐节比较）。

        @return: 存在偏离全局的按类覆盖则 True
        """
        cfg = self.cfg
        return any(view.quality != cfg.quality or view.rubric != cfg.rubric
                   or view.annotate != cfg.annotate or view.generate != cfg.generate
                   or view.verify != cfg.verify or view.extract != cfg.extract
                   for view in cfg.class_views.values())

    def _estimate(self) -> dict:
        """对导出纯函数的薄封装（v1.10 U20）：经既有扫描路径拿到 plan（estimate=True 是
        默认值——dry-run 行为逐字节不变）再委派给 ``estimate_run``。generate_only 传
        plan=None（3.6.2 静态量公式，不需要 scan）。

        @return: 估算字典
        @raises AssertionError: process 模式未提供摄取器
        """
        plan = None
        if self.cfg.run.mode != "generate_only":
            assert self.ingestor is not None, "process mode requires an Ingestor"
            plan = self.ingestor.scan()
        return estimate_run(self.cfg, plan)

    # ── 信号 ───────────────────────────────────────────────────────────────

    def _install_signal_handlers(self) -> None:
        """装 SIGINT/SIGTERM 处理器（无事件循环或平台不支持时静默降级为不可中断）。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _log.debug("no running event loop: signal handlers not installed",
                       extra={"stage": "run", "batch": 0})
            return
        for sig in (_signal.SIGINT, _signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_stop)
                self._installed_signals.append(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                _log.debug("signal handler not supported for signal %d", sig,
                           extra={"stage": "run", "batch": 0})

    def _remove_signal_handlers(self) -> None:
        """摘掉已装的信号处理器并取消挂起的取消计时器。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _log.debug("no running event loop: signal handlers not removed",
                       extra={"stage": "run", "batch": 0})
            return
        for sig in self._installed_signals:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                _log.debug("signal handler not removable for signal %d", sig,
                           extra={"stage": "run", "batch": 0})
        self._installed_signals.clear()
        for handle in self._timer_handles:
            handle.cancel()
        self._timer_handles.clear()

    def _request_stop(self) -> None:
        """SIGINT/SIGTERM：不再取新批，给在飞的批 30 s 再取消它。finalize 照常运行
        （该改名就改名，报告带 interrupted=true）。
        """
        self._stop = True
        self._interrupted = True
        # v1.10（U19，spec 3.10.3 console 行）：中断横幅通路——只转发，无 listener 即 no-op。
        self.metrics.stop_requested()
        task = self._current_task
        if task is not None and not task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                _log.debug("no running event loop: cancel timer not scheduled",
                           extra={"stage": "run", "batch": self._batch_no})
                return
            self._timer_handles.append(loop.call_later(30.0, task.cancel))


def _usage_entry(usage) -> dict | None:
    """把一个 profile 的用量折成 report.llm_usage 条目。

    v1.6 密钥池（spec 6.4，只增）：keys 子对象仅在池 > 1 时出现（M9 会预置每个成员，故
    len == 池大小；密钥身份 = 环境变量**名**，1.6 决策 ⑤）；parked 统计在池 > 1 或任一项
    非零时出现——单密钥停泊也必须在报表里留下证据。零活动 profile（例如它唯一的调用在任何
    尝试之前就被熔断掉了）略去，以保住 v1.5 的报表形态。

    @param usage: M9 的按 profile 用量对象
    @return: 报表条目；零活动 profile 返回 None
    """
    key_usages = getattr(usage, "keys", None) or {}
    parked_calls = getattr(usage, "parked_calls", 0)
    parked_ms = getattr(usage, "parked_ms", 0)
    emit_keys = len(key_usages) > 1
    emit_parked = emit_keys or bool(parked_calls) or bool(parked_ms)
    if (usage.calls == 0 and usage.retries == 0
            and usage.prompt_tokens == 0 and usage.completion_tokens == 0
            and usage.est_cost_usd is None
            and not emit_keys and not emit_parked):
        return None
    entry = {
        "calls": usage.calls,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "retries": usage.retries,
    }
    if usage.est_cost_usd is not None:
        entry["est_cost_usd"] = usage.est_cost_usd
    if emit_keys:
        entry["keys"] = {
            env: {"calls": ku.calls, "rate_limited": ku.rate_limited,
                  "disabled": ku.disabled}
            for env, ku in sorted(key_usages.items())
        }
    if emit_parked:
        entry["parked_calls"] = parked_calls
        entry["parked_ms"] = parked_ms
    return entry

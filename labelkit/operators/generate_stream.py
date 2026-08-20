"""M6 时间流生成的计划、交织、回填与装配纯逻辑。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from labelkit.common.config.model import (
    apportion_tiers,
    effective_rules,
    effective_tiers,
    effective_windows,
)
from labelkit.common.contracts.types import Classification, PipelineItem, Record, RecordRef
from labelkit.common.errors import ContextOverflowError, InternalError
from labelkit.common.runtime.sequence_planner import PlannerConfigError, PlannerInternalError

if TYPE_CHECKING:
    import random
    from collections.abc import Mapping
    from labelkit.common.config.model import (
        ClassSpec,
        FrameClassView,
        GenerateConfig,
        GenerateStyle,
        ResolvedConfig,
        TierSpec,
    )
    from labelkit.common.contracts.stage import RunContext

_log = logging.getLogger("labelkit.generate")


def canonical_json(obj) -> str:
    """规范化 JSON，保持生成记录与工件 id 的历史字节语义。

    @param obj 待序列化对象。
    @return 键序稳定、非 ASCII 保真、无冗余空白的紧凑 JSON 文本。
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def predraw_llm_style(
    g: "GenerateConfig", num_calls: int, rng: "random.Random",
    styles_by_index: Sequence[tuple["GenerateStyle", ...]] | None = None,
) -> list[tuple[str, "GenerateStyle | None"]]:
    """按冻结抽签顺序预抽每个调用的模型与风格。

    @param g 全局生成配置。
    @param num_calls 需要预抽的调用数。
    @param rng 单流伪随机数生成器。
    @param styles_by_index 各调用的风格池；None 表示统一使用全局风格池。
    @return 按调用序排列的 profile 名与风格对。
    """
    pairs: list[tuple[str, "GenerateStyle | None"]] = []
    for index in range(num_calls):
        if g.mixture == "weighted":
            llm = rng.choices(list(g.llms), weights=list(g.weights), k=1)[0]
        else:
            llm = g.llms[index % len(g.llms)]
        styles = g.styles if styles_by_index is None else styles_by_index[index]
        style = rng.choice(styles) if styles else None
        pairs.append((llm, style))
    return pairs


# 蓝图模板静态脚手架：budget.TEMPLATE_HEAD_TOKENS["generate_plan"] 钉住
# est_text(_PLAN_SYSTEM_STATIC)（V22 家族跨层等式，tests/common/runtime/
# test_budget.py 守护两侧同步）；类生成指令与帧类表是配置量，在 M1 静态预算
# 预检（V13③）各自计量。
_PLAN_SYSTEM_HEAD = (
    "你是时间流数据规划器。给定任务描述与帧类表，为一条序列规划逐步"
    "蓝图：每一步选定一个帧类，并用一句话写明该步内容要点。")
_PLAN_LABEL_TASK = "[任务]"
_PLAN_LABEL_FRAME_TABLE = "[帧类表]"
_PLAN_STRUCTURE = (
    "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
    '{"steps": [{"frame_class": <帧类名>, "brief": <一句话要点>}, ...]}\n'
    "字段说明：steps 恰为要求的步数，一步一项，按时间顺序排列；frame_class 必须取自 "
    "[帧类表] 中的帧类名；brief 用一句话写明该步内容要点，供逐帧实现展开。")
_PLAN_SYSTEM_STATIC = "\n".join((_PLAN_SYSTEM_HEAD, _PLAN_LABEL_TASK,
                                 _PLAN_LABEL_FRAME_TABLE, _PLAN_STRUCTURE))

# v1.16 联合规划蓝图的固定脚手架；帧类词已由 planner 冻结，不再由 LLM 返回。
_BRIEF_SYSTEM_HEAD = (
    "你是时间流数据规划器。根据已冻结的帧类词，为每一步写一句"
    "内容要点。")
_BRIEF_LABEL_TASK = "[任务]"
_BRIEF_LABEL_WORD = "[固定帧类词]"
_BRIEF_LABEL_CONSTRAINTS = "[约束]"
_BRIEF_STRUCTURE = (
    "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
    '{"steps": [{"brief": <一句话要点>}, ...]}\n'
    "字段说明：steps 恰为要求的步数，每项只包含 brief，按时间顺序对应固定帧类。")
_BRIEF_SYSTEM_STATIC = "\n".join((
    _BRIEF_SYSTEM_HEAD, _BRIEF_LABEL_TASK, _BRIEF_LABEL_WORD,
    _BRIEF_LABEL_CONSTRAINTS, _BRIEF_STRUCTURE,
))

# 帧实现模板静态脚手架：TEMPLATE_HEAD_TOKENS["generate_realize"] 钉住
# est_text(_REALIZE_SYSTEM_STATIC)。逐位契约行把帧类生成 Schema 文本按步重复——
# L0 关端点（DeepSeek anthropic 路由硬拒强制 tool call）上结构服从性靠该契约。
_REALIZE_LABEL_TASK = "[任务]"
_REALIZE_LABEL_STYLE = "[风格要求]"
_REALIZE_STRUCTURE = (
    "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
    '{"frames": [<第 1 帧内容>, <第 2 帧内容>, ...]}\n'
    "字段说明：frames 恰为蓝图步数，一帧一项，与蓝图步序逐位对应；逐帧"
    "内容契约如下：")
_REALIZE_FREE_TEXT = "自由文本一段"
_REALIZE_SYSTEM_STATIC = "\n".join((_REALIZE_LABEL_TASK, _REALIZE_LABEL_STYLE,
                                    _REALIZE_STRUCTURE))

_MAX_STREAM_DEGRADE_LEVELS = 2   # 实现调用对半降级级数上限（裁决·预算头两键，AIMD ≤2）


def render_plan_prompt_texts(instruction: str, frame_classes: Sequence,
                             class_name: str, length: int,
                             cover_all: bool = False) -> tuple[str, str]:
    """蓝图调用的纯文本装配（§10.14）：返回 (system_text, user_text)。

    入参已达 5 个上限（v1.14 增 cover_all）——后续再增须改参数对象。

    @param instruction 类有效生成指令（[class.<name>.generate].instruction）。
    @param frame_classes 待渲染的帧类表（无档位 = 全表；档位表在场 = 档内子集，
        按 [[frame.classify.classes]] 声明序，过滤由调用方完成）。
    @param class_name 序列类名（user 段引用）。
    @param length 步数 L（与 plan_schema 的 minItems=maxItems 同源）。
    @param cover_all v1.14（裁决·蓝图双向硬约束）：True 时 user 行取覆盖变体，与
        ``plan_schema(cover_all=True)`` 的 contains 硬约束成对出现；False 的输出与
        v1.13 逐字节一致。
    @return (system_text, user_text) 二元组。
    """
    table = "\n".join(f"{c.name}: {c.description}" for c in frame_classes)
    system = "\n".join((_PLAN_SYSTEM_HEAD,
                        f"{_PLAN_LABEL_TASK} {instruction}",
                        f"{_PLAN_LABEL_FRAME_TABLE}\n{table}",
                        _PLAN_STRUCTURE))
    user = f"请为一条「{class_name}」序列产出 {length} 步蓝图"
    if cover_all:
        return system, f"{user}，且 {_PLAN_LABEL_FRAME_TABLE} 中每个帧类都至少出现一次。"
    return system, f"{user}。"


def render_brief_prompt_texts(instruction: str, frame_classes: Sequence[str],
                              class_name: str, length: int,
                              constraints: str = "") -> tuple[str, str]:
    """装配 planner 冻结帧类词下的 sampled brief 提示词。

    @param instruction 类有效生成指令。
    @param frame_classes planner 冻结的逐位帧类名。
    @param class_name 序列类名。
    @param length 序列步数。
    @param constraints 规则与窗口约束文本；空串表示无额外约束。
    @return (system_text, user_text) 二元组。
    """
    positions = "\n".join(f"{index}: {name}"
                           for index, name in enumerate(frame_classes, 1))
    constraint_block = (f"{_BRIEF_LABEL_CONSTRAINTS}\n{constraints}"
                        if constraints else f"{_BRIEF_LABEL_CONSTRAINTS}\nnone")
    system = "\n".join((_BRIEF_SYSTEM_HEAD, f"{_BRIEF_LABEL_TASK} {instruction}",
                         f"{_BRIEF_LABEL_WORD}\n{positions}", constraint_block,
                         _BRIEF_STRUCTURE))
    user = f"请为一条「{class_name}」序列产出固定的 {length} 个 brief。"
    return system, user


def render_realize_prompt_texts(instruction: str, style_prompt: str | None,
                                steps: Sequence[tuple[str, str]],
                                contracts: Sequence[str],
                                constraints: str = "") -> tuple[str, str]:
    """帧实现调用的纯文本装配（§10.15）：返回 (system_text, user_text)。

    @param instruction 类有效生成指令。
    @param style_prompt 预抽风格提示；None = 无风格段（蓝图不带风格，实现才带）。
    @param steps 蓝图步序列 [(frame_class, brief), ...]（对半降级时为切片，局部重编号）。
    @param contracts 与 steps 对位的逐帧内容契约文本（Schema 单行 dump 或自由文本句）。
    @param constraints 规则与窗口约束文本；空串表示无额外约束。
    @return (system_text, user_text) 二元组。
    """
    lines = [f"{_REALIZE_LABEL_TASK} {instruction}"]
    if style_prompt is not None:
        lines.append(f"{_REALIZE_LABEL_STYLE} {style_prompt}")
    if constraints:
        lines.append(f"[约束]\n{constraints}")
    lines.append(_REALIZE_STRUCTURE)
    for i, ((frame_class, _brief), contract) in enumerate(zip(steps, contracts), 1):
        lines.append(f"第 {i} 帧（{frame_class}）须符合：{contract}")
    user_lines = [f"{i}. [{frame_class}] {brief}"
                  for i, (frame_class, brief) in enumerate(steps, 1)]
    user_lines.append(f"请实现全部 {len(steps)} 帧内容。")
    return "\n".join(lines), "\n".join(user_lines)


def _plan_schema(names: Sequence[str], length: int, cover_all: bool = False) -> dict:
    """取蓝图调用的内部 Schema。

    :param names: 帧类名闭集（逐步 frame_class 的 enum 域；档位表在场 = 档内子集）。
    :param length: 步数 L（minItems = maxItems）。
    :param cover_all: v1.14：True 时注入逐类 contains 覆盖约束（构成恰等）。
    :returns: draft 2020-12 Schema 对象。
    """
    # 懒导入：内部 Schema 构造器归 M8（CONTRACTS §7.7/§10.7）。
    from labelkit.common.runtime.schema_engine import plan_schema

    return plan_schema(names, length, cover_all=cover_all)


def _brief_schema(length: int) -> dict:
    """取 v1.16 sampled brief 的内部 Schema。"""
    from labelkit.common.runtime.schema_engine import brief_schema

    return brief_schema(length)


def _realize_schema(step_schemas: Sequence[dict]) -> dict:
    """取帧实现调用的内部 Schema（原生 prefixItems 逐位约束）。

    :param step_schemas: 与蓝图步序对位的逐帧内容 Schema。
    :returns: draft 2020-12 Schema 对象。
    """
    # 懒导入：同上。
    from labelkit.common.runtime.schema_engine import realize_schema

    return realize_schema(step_schemas)


def _text_bundle(system_text: str, user_text: str,
                 temperature: float) -> "PromptBundle":
    """单 system + 单 user 的纯文本 PromptBundle（蓝图/实现两模板共用装配尾）。"""
    from labelkit.common.runtime.llm_client import Message, Part, PromptBundle

    return PromptBundle(
        messages=(Message(role="system", parts=(Part(kind="text", text=system_text),)),
                  Message(role="user", parts=(Part(kind="text", text=user_text),))),
        temperature=temperature)


# v1.13 计划期纯函数：estimate_run 精确复演

@dataclass(frozen=True)
class SequencePlan:
    """一条待生成序列的计划期定稿（蓝图与帧实现共用）。"""
    index: int                  # 计划序全局序号 0 基（配额展开序）
    class_name: str             # 所属序列类
    ordinal: int                # 类内序数 0 基（= 工件 truth.sequence）
    length: int                 # 步数 L（rng.randint(类有效 len_range)）
    llm: str                    # 预抽 profile——蓝图+实现绑定同一 profile
    style_name: str | None      # 预抽风格名（实现才生效，蓝图不带风格）
    style_prompt: str | None    # 预抽风格提示词
    tier_rank: int | None = None    # v1.14 档位序数（配分的连续分块查表所得，零 rng）；
                                    # None = 档位面不在场（档位表缺省）
    frame_classes: tuple[str, ...] = ()  # v1.16 planner 冻结的逐位帧类词；默认路径为空
    timestamps_us: tuple[int, ...] = ()  # v1.16 planner 冻结的任务帧微秒时间


@dataclass(frozen=True)
class NoiseCallPlan:
    """一次噪音帧批量实现调用的计划期定稿（复用平面生成模板）。"""
    index: int                  # 噪音批调用序号 0 基
    llm: str                    # 独立预抽 profile（裁决·生成键效力矩阵）
    style_name: str | None      # 预抽风格名（全局 styles 池）
    style_prompt: str | None    # 预抽风格提示词


@dataclass(frozen=True)
class StreamPlan:
    """时间流生成的整轮计划期产物（M10 estimate_run 精确复演的同一对象）。"""
    sequences: tuple[SequencePlan, ...]     # 计划序（类字典序 × 类内序数）
    noise_target: int                       # round(noise_ratio × Σ length)
    noise_plans: tuple[NoiseCallPlan, ...]  # ⌈noise_target / num_per_call⌉ 个
    planner_active: bool = False            # 是否启用 v1.16 联合 planner 路径
    planner_question: Any | None = None    # M6/estimate 共用的冻结问题
    planner_layout: Any | None = None      # LLM 前冻结的 skeleton
    duplicate_order: tuple[int, ...] = ()   # 预抽的 primary source 序列下标
    noise_requested: int = 0                # noise_ratio 目标，可能高于最优可行槽数


@dataclass(frozen=True)
class RealizedSequence:
    """蓝图 + 帧实现都成功后的一条序列（交织器与直装组装的输入单元）。"""
    plan: SequencePlan                      # 该序列的计划期定稿
    frame_classes: tuple[str, ...]          # 蓝图逐步帧类（帧级真值）
    payloads: tuple = ()                    # 逐帧 text_field 值（str 或结构化帧对象）


@dataclass(frozen=True)
class StreamGenerateProduct:
    """``generate_stream_all`` 的富返回（裁决·时间流入口与配额截断）——
    ``PipelineItem(record=r)`` 裸构造无法携带 session_id/classification/
    member_classifications，故必须整信封交付。"""
    envelopes: list[PipelineItem]           # 直装序列信封（计划序）
    artifact_lines: list[str]               # 工件行（交织序定稿；行号 = 列表序 + 1）


def expand_stream_quota(cfg: "ResolvedConfig") -> list[tuple[str, int]]:
    """计划期第①步（零 rng）：类按类名字典序展开配额为 (类名, 类内序数) 列表；
    ``--limit`` 在此做前缀截断（配额层截断 ⇒ 作废序列不再生成、不进交织，工件与
    主输出覆盖面恒一致）。

    @param cfg 已解析配置。
    @return 按类名字典序排列的 (类名, 类内序数) 列表。
    """
    entries: list[tuple[str, int]] = []
    for name in sorted(cfg.class_views):
        for ordinal in range(cfg.class_views[name].generate.sequences):
            entries.append((name, ordinal))
    if cfg.limit is not None:
        entries = entries[: cfg.limit]
    return entries


def tier_rank_for_ordinal(sequences: int, tiers: "Sequence[TierSpec]",
                          ordinal: int) -> int | None:
    """v1.14（裁决·零抽签配分）：把一个类内序数映射到它所属档位的序数。

    配分结果（``apportion_tiers``，整数域最大余额法）按 tier_rank 升序把类配额切成
    **连续分块**，类内序数落进哪一块就属哪一档——前缀和查表，零 rng。推论：``--limit``
    的前缀截断只切掉尾部序数，即类内从最高 tier_rank 侧截起，截断与映射可交换。

    @param sequences 该序列类的**全量**配额（``[class.<name>.generate].sequences``）；
        绝不能传 ``--limit`` 截断后的条数，否则分块会随截断漂移。
    @param tiers 该类的**生效**档位表（v1.15：``effective_tiers`` 的产物，按 tier_rank
        升序存放）；空 = 档位面不在场。
    @param ordinal 类内序数 0 基（= 工件 truth.sequence）。
    @return 该序数所属档的 tier_rank；档位表为空时 None。
    """
    upper = 0
    for spec, quota in zip(tiers, apportion_tiers(sequences, tiers)):
        upper += quota
        if ordinal < upper:
            return spec.tier_rank
    return None


def _joint_constraints_active(cfg: "ResolvedConfig",
                              entries: Sequence[tuple[str, int]]) -> bool:
    """判断非零配额类是否有生效规则或窗口。"""
    global_rules = cfg.generate_stream.rules
    global_windows = cfg.generate_stream.windows
    for name, _ in entries:
        view = cfg.class_views[name]
        if effective_rules(view.rules, global_rules) or effective_windows(
                view.windows, global_windows):
            return True
    return False


def _build_joint_plan(cfg: "ResolvedConfig", rng: "random.Random",
                      entries: Sequence[tuple[str, int]]) -> StreamPlan:
    """执行 v1.16 长度条件抽样、skeleton 冻结与调用预抽。"""
    from labelkit.common.runtime.sequence_planner import (
        question_from_config,
        select_feasible_plan,
    )

    solver_seed = rng.getrandbits(31)
    question = question_from_config(cfg, solver_seed=solver_seed)
    question, layout = select_feasible_plan(question, rng)
    if layout.planned_noise_slots < question.noise_target:
        _log.warning("planner could not place the complete noise target: target=%d placed=%d",
                     question.noise_target, layout.planned_noise_slots)
    g = cfg.generate
    pairs = _predraw_joint_pairs(cfg, entries, layout, rng)
    duplicate_order = _predraw_duplicate_order(cfg, len(entries), rng)
    noise_count = math.ceil(layout.planned_noise_slots / g.num_per_call)
    styles = [g.styles] * noise_count
    noise_pairs = predraw_llm_style(g, noise_count, rng, styles_by_index=styles)
    sequences = tuple(_sequence_plan_from_layout(
        index, entries[index][1], item, layout, pairs[index])
        for index, item in enumerate(question.attempts))
    noises = tuple(_noise_plan(index, noise_pairs[index]) for index in range(noise_count))
    return StreamPlan(sequences=sequences, noise_target=layout.planned_noise_slots,
                      noise_plans=noises, planner_active=True,
                      planner_question=question, planner_layout=layout,
                      duplicate_order=duplicate_order,
                      noise_requested=question.noise_target)


def _predraw_joint_pairs(cfg: "ResolvedConfig", entries: Sequence[tuple[str, int]],
                         layout: Any, rng: "random.Random") -> list[tuple[str, Any]]:
    """按序列类有效风格池预抽联合路径的模型与风格。"""
    styles = [cfg.class_views[name].generate.styles for name, _ in entries]
    return predraw_llm_style(cfg.generate, len(layout.words), rng, styles)


def _predraw_duplicate_order(cfg: "ResolvedConfig", count: int,
                             rng: "random.Random") -> tuple[int, ...]:
    """在任何内容调用前冻结 duplicate source 排列。"""
    del cfg
    return tuple(rng.sample(range(count), count)) if count else ()


def _sequence_plan_from_layout(index: int, ordinal: int, attempt: Any,
                               layout: Any, pair: tuple[str, Any]) -> SequencePlan:
    """将 planner attempt 与预抽模型风格合成为 M6 序列计划。"""
    style = pair[1]
    return SequencePlan(index=index, class_name=attempt.class_name,
                        ordinal=ordinal, length=attempt.length, llm=pair[0],
                        style_name=style.name if style else None,
                        style_prompt=style.prompt if style else None,
                        tier_rank=attempt.tier_rank,
                        frame_classes=tuple(layout.words[index]),
                        timestamps_us=tuple(layout.timestamps_us[index]))


def _noise_plan(index: int, pair: tuple[str, Any]) -> NoiseCallPlan:
    """构造单个噪音调用计划。"""
    style = pair[1]
    return NoiseCallPlan(index=index, llm=pair[0],
                         style_name=style.name if style else None,
                         style_prompt=style.prompt if style else None)


def plan_stream(cfg: "ResolvedConfig", rng: "random.Random") -> StreamPlan:
    """计划期纯函数（M10 estimate_run 精确复演共用，裁决·估算精确复演）。

    抽签消费顺序冻结（裁决·抽签消费顺序表，测试钉住）：①配额展开（截断，零 rng）
    ②逐序列 L = rng.randint(类有效 len_range) ③逐序列 (llm, style) 预抽——噪音批
    调用独立预抽，紧随序列预抽在同一 predraw 流内消费（round_robin 不耗 rng、
    weighted 逐位 rng.choices、styles 非空逐位 rng.choice；噪音批取全局 styles）。
    v1.14 的档位赋值插在①与②之间，零 rng ⇒ 同 seed 下有无档位表的抽签流逐字节一致；
    v1.15 逐类改吃 ``effective_tiers``（按类表 ?? 全局表），仍是纯查表、零消费。

    @param cfg 已解析配置。
    @param rng 单流伪随机数生成器。
    @return 时间流生成的冻结计划。
    """
    entries = expand_stream_quota(cfg)
    if _joint_constraints_active(cfg, entries):
        try:
            return _build_joint_plan(cfg, rng, entries)
        except PlannerConfigError as exc:
            _log.error("time-stream planner violated an M1-validated invariant at M6 boundary")
            raise InternalError("time-stream planner violated a validated invariant") from exc
        except PlannerInternalError as exc:
            _log.error("time-stream planner failed an internal invariant at M6 boundary")
            raise InternalError("time-stream planner internal failure") from exc
    return _plan_default_stream(cfg, rng, entries)


def _plan_default_stream(cfg: "ResolvedConfig", rng: "random.Random",
                         entries: Sequence[tuple[str, int]]) -> StreamPlan:
    """按 v1.15 默认路径复演长度、档位、调用 profile 与风格。"""
    gs_tiers = cfg.generate_stream.tiers
    ranks = [tier_rank_for_ordinal(
        cfg.class_views[name].generate.sequences,
        effective_tiers(cfg.class_views[name].tiers, gs_tiers), ordinal)
        for name, ordinal in entries]
    lengths: list[int] = []
    for name, _ in entries:
        lo, hi = cfg.class_views[name].generate.len_range
        lengths.append(rng.randint(lo, hi))
    g = cfg.generate
    noise_target = round(cfg.generate_stream.noise_ratio * sum(lengths))
    n_noise = math.ceil(noise_target / g.num_per_call) if noise_target > 0 else 0
    styles_by_index = ([cfg.class_views[name].generate.styles for name, _ in entries]
                       + [g.styles] * n_noise)
    pairs = predraw_llm_style(g, len(entries) + n_noise, rng,
                              styles_by_index=styles_by_index)
    sequences = tuple(
        SequencePlan(index=i, class_name=name, ordinal=ordinal, length=lengths[i],
                     llm=pairs[i][0],
                     style_name=pairs[i][1].name if pairs[i][1] else None,
                     style_prompt=pairs[i][1].prompt if pairs[i][1] else None,
                     tier_rank=ranks[i])
        for i, (name, ordinal) in enumerate(entries))
    offset = len(entries)
    noise_plans = tuple(
        NoiseCallPlan(index=j, llm=pairs[offset + j][0],
                      style_name=(pairs[offset + j][1].name
                                  if pairs[offset + j][1] else None),
                      style_prompt=(pairs[offset + j][1].prompt
                                    if pairs[offset + j][1] else None))
        for j in range(n_noise))
    return StreamPlan(sequences=sequences, noise_target=noise_target,
                      noise_plans=noise_plans)


# v1.13 机械交织器：纯函数族，零 LLM、零 IO

@dataclass
class _StreamSlot:
    """交织后的一帧槽位（工件行装配前形态；仅本模块内部可变）。"""
    payload: "str | Mapping"    # text_field 值（结构化帧 = 行内对象）
    truth: dict                 # 冻结键集 truth（session 值交织尾声回填）
    owner: int | None           # 幸存序列下标（任务帧）；噪音/重复帧 = None
    ts: str = ""                # ⑨ 铺设的 ISO-8601 时间戳


def _tier_truth(tier_rank: int | None, tiered: bool) -> dict:
    """truth 的档位片段（裁决·真值键序重冻结）：档位表在场才有这一键，位于
    ``sequence`` 之后、``frame_class`` 之前——三个槽位构造点按同一位置内联展开。

    :param tier_rank: 该帧承载的档位序数（噪音帧恒 None）。
    :param tiered: 档位表是否在场；False ⇒ 空片段（键不在场，v1.13 字节回退）。
    :returns: 可直接解包进 truth 字面量的单键或空字典。
    """
    return {"tier_rank": tier_rank} if tiered else {}


def _sequence_slots(index: int, seq: RealizedSequence) -> list[_StreamSlot]:
    """一条幸存序列的任务帧槽位（truth.session 占位 −1，交织尾声回填）。v1.14：
    档位表在场时 truth 带本序列的档位序数。"""
    plan = seq.plan
    tier = _tier_truth(plan.tier_rank, tiered=plan.tier_rank is not None)
    return [_StreamSlot(payload=seq.payloads[i],
                        truth={"session": -1, "sequence_class": plan.class_name,
                               "sequence": plan.ordinal, **tier,
                               "frame_class": seq.frame_classes[i], "noise": False},
                        owner=index)
            for i in range(len(seq.payloads))]


def _duplicate_slots(seq: RealizedSequence) -> list[_StreamSlot]:
    """⑧ 一条重复序列的流尾新会话槽位：帧 text_field 值逐字节同源（同对象再序列
    化），truth 带 duplicate_of = 原序列类内序数、sequence = null（重发副本无自身
    计划期身份，归属经 duplicate_of 对账——裁决·工件行真值字段集）。v1.14：档位
    序数承源（裁决·重发帧承源档与同源载荷）。"""
    plan = seq.plan
    tier = _tier_truth(plan.tier_rank, tiered=plan.tier_rank is not None)
    return [_StreamSlot(payload=seq.payloads[i],
                        truth={"session": -1, "sequence_class": plan.class_name,
                               "sequence": None, **tier,
                               "frame_class": seq.frame_classes[i],
                               "noise": False, "duplicate_of": plan.ordinal},
                        owner=None)
            for i in range(len(seq.payloads))]


def _noise_slot(payload: str, tiered: bool) -> _StreamSlot:
    """一帧插入噪音的槽位（真值三 null + noise=true；档位表在场时档位序数亦 null
    ——噪音帧不属任何序列，自然不承档）。"""
    return _StreamSlot(payload=payload,
                       truth={"session": -1, "sequence_class": None, "sequence": None,
                              **_tier_truth(None, tiered=tiered),
                              "frame_class": None, "noise": True},
                       owner=None)


def _cross_session(slots_a: list[_StreamSlot], slots_b: list[_StreamSlot],
                   rng: "random.Random") -> list[_StreamSlot]:
    """⑥ 单个交叉会话的切换点掷签：形态 A 段+B 段+A 余段[+B 余段]（裁决·会话装箱
    定容）——cut_a ∈ [1, len(A)−1] 保证真交叉（A 必在 B 头部之后回续），cut_b ∈
    [1, len(B)]（= len(B) 时无 B 余段）。A 不足 2 帧时与 B 互换；两者都不足 ⇒
    真交叉不可构造，退化为顺次拼接（纯长度条件，确定性，零 rng 消费）。"""
    if len(slots_a) < 2 <= len(slots_b):
        slots_a, slots_b = slots_b, slots_a
    if len(slots_a) < 2:
        return slots_a + slots_b
    cut_a = rng.randint(1, len(slots_a) - 1)
    cut_b = rng.randint(1, len(slots_b))
    return slots_a[:cut_a] + slots_b[:cut_b] + slots_a[cut_a:] + slots_b[cut_b:]


def _pack_sessions(survivors: Sequence[RealizedSequence], declared: int,
                   rng: "random.Random") -> tuple[list[list[_StreamSlot]], int]:
    """⑤ 装箱定容：洗牌后前 Σ幸存 − sessions_eff 对成对交叉（sessions_eff =
    min(sessions, Σ幸存)），其余单序列会话；会话序 = 洗牌序（交叉会话在前）。"""
    order = list(range(len(survivors)))
    rng.shuffle(order)
    sessions_eff = min(declared, len(order))
    n_cross = len(order) - sessions_eff
    sessions: list[list[_StreamSlot]] = []
    for pair in range(n_cross):
        a, b = order[2 * pair], order[2 * pair + 1]
        sessions.append(_cross_session(_sequence_slots(a, survivors[a]),
                                       _sequence_slots(b, survivors[b]), rng))
    for index in order[2 * n_cross:]:
        sessions.append(_sequence_slots(index, survivors[index]))
    return sessions, n_cross


def _insert_noise(sessions: list[list[_StreamSlot]], slots: Sequence[_StreamSlot],
                  session_max_len: int, rng: "random.Random") -> int:
    """⑦ 逐噪音帧 (会话, 槽位) 掷签：满员会话（len ≥ session_max_len）退出签池；
    签池耗尽 ⇒ 余帧从交织缺席（不补生成）。返回实际织入帧数。v1.14：槽位由持有
    cfg 的 ``weave_stream`` 预先构造后传入（噪音真值要条件写档位键），本函数只掷签
    落位——入参数不变。"""
    woven = 0
    for slot in slots:
        pool = [session for session in sessions if len(session) < session_max_len]
        if not pool:
            _log.warning("noise weaving stopped: every session is at "
                         "stream.session_max_len; %d noise frame(s) dropped",
                         len(slots) - woven,
                         extra={"stage": "generate", "batch": 0})
            break
        target = rng.choice(pool)
        target.insert(rng.randint(0, len(target)), slot)
        woven += 1
    return woven


def _lay_timestamps(sessions: list[list[_StreamSlot]], cfg: "ResolvedConfig",
                    rng: "random.Random") -> None:
    """⑨ ts 铺设：起点 ts_start（流首帧零消费）；帧间隔 uniform(frame_gap_s)、会话
    间隔 uniform(gap_s + lo, gap_s + hi)（恒 > stream.gap_s ⇒ 摄取侧按同一 gap_s
    复演出相同会话切分）；datetime + timedelta 正间隔累加 ⇒ 严格递增；isoformat
    微秒精度写出。"""
    lo, hi = cfg.generate_stream.frame_gap_s
    gap = float(cfg.stream.gap_s)
    current = datetime.fromisoformat(cfg.generate_stream.ts_start)
    first = True
    for session in sessions:
        for position, slot in enumerate(session):
            if first:
                first = False
            elif position == 0:
                current += timedelta(seconds=rng.uniform(gap + lo, gap + hi))
            else:
                current += timedelta(seconds=rng.uniform(lo, hi))
            slot.ts = current.isoformat(timespec="microseconds")


def weave_stream(survivors: Sequence[RealizedSequence], noise_payloads: Sequence[str],
                 cfg: "ResolvedConfig", rng: "random.Random",
                 ) -> tuple[list[list[_StreamSlot]], dict]:
    """机械交织器入口（纯函数族，零 LLM 零 IO；裁决·抽签消费顺序表④–⑨单流顺序
    消费）：④重复选取 rng.sample ⑤装箱洗牌+成对交叉 ⑥逐交叉会话切换点 ⑦逐噪音帧
    掷签 ⑧重复序列成流尾新会话（零 rng）⑨ts 铺设；尾声回填 truth.session 全流会话
    序数。

    @param survivors 通过序列校验与相似度过滤的序列。
    @param noise_payloads 已生成的噪音帧文本。
    @param cfg 已解析配置。
    @param rng 单流伪随机数生成器。
    @return (会话列表, 仅计数统计)；sessions 不含重复尾会话。
    """
    gs = cfg.generate_stream
    dup_k = min(gs.duplicates, len(survivors))
    if dup_k < gs.duplicates:
        _log.warning("duplicates clamped to the surviving sequence count: %d -> %d",
                     gs.duplicates, dup_k, extra={"stage": "generate", "batch": 0})
    chosen = rng.sample(list(survivors), dup_k) if dup_k else []           # ④
    sessions, crossed = _pack_sessions(survivors, gs.sessions, rng)        # ⑤⑥
    noise_slots = [_noise_slot(payload, bool(gs.tiers))                    # 零 rng
                   for payload in noise_payloads]
    woven_noise = _insert_noise(sessions, noise_slots,
                                cfg.stream.session_max_len, rng)           # ⑦
    for source in chosen:                                                  # ⑧
        sessions.append(_duplicate_slots(source))
    for session_no, session in enumerate(sessions):
        for slot in session:
            slot.truth["session"] = session_no
    _lay_timestamps(sessions, cfg, rng)                                    # ⑨
    stats = {"sessions": len(sessions) - dup_k, "crossed_sessions": crossed,
             "frames": sum(len(seq.payloads) for seq in survivors),
             "noise_frames": woven_noise, "duplicates": dup_k}
    return sessions, stats


def _timestamp_text(timestamp_us: int, cfg: "ResolvedConfig") -> str:
    """把 planner 微秒时间转换为固定 offset 的 ISO 文本。"""
    from datetime import timezone
    from labelkit.common.runtime.temporal import fixed_offset

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    value = epoch + timedelta(microseconds=timestamp_us)
    return value.astimezone(fixed_offset(cfg.generate_stream.ts_start)).isoformat(
        timespec="microseconds")


def _planned_task_slot(local_owner: int, sequence: RealizedSequence,
                       position: int, timestamp_us: int) -> _StreamSlot:
    """构造一条 planner task slot 并保持旧 truth 键序。"""
    plan = sequence.plan
    tier = _tier_truth(plan.tier_rank, tiered=plan.tier_rank is not None)
    truth = {"session": -1, "sequence_class": plan.class_name,
             "sequence": plan.ordinal, **tier,
             "frame_class": sequence.frame_classes[position], "noise": False}
    return _StreamSlot(payload=sequence.payloads[position], truth=truth,
                       owner=local_owner)


def _planned_noise_slot(payload: str, tiered: bool) -> _StreamSlot:
    """构造一条固定 timestamp 的噪音 slot。"""
    return _noise_slot(payload, tiered)


def _projected_primary_sessions(survivors: Sequence[RealizedSequence], plan: StreamPlan,
                                noise_payloads: Sequence[str], cfg: "ResolvedConfig",
                                ) -> tuple[list[list[_StreamSlot]], dict[int, int], int]:
    """按 planner projection 组装未含 duplicate 的固定会话。"""
    from labelkit.common.runtime.sequence_planner import project_survivors

    original = {seq.plan.index: seq for seq in survivors}
    projected = project_survivors(plan.planner_layout, set(original))
    ordered = tuple(sorted(survivors, key=lambda item: item.plan.index))
    local = {seq.plan.index: index for index, seq in enumerate(ordered)}
    noise_index = 0
    sessions: list[list[_StreamSlot]] = []
    for session in projected.sessions:
        slots: list[_StreamSlot] = []
        for frame in session.frames:
            if frame.noise:
                if noise_index >= len(noise_payloads):
                    continue
                slot = _planned_noise_slot(noise_payloads[noise_index],
                                           bool(cfg.generate_stream.tiers))
                noise_index += 1
            else:
                sequence = original[frame.owner]
                slot = _planned_task_slot(local[frame.owner], sequence,
                                          int(frame.position), frame.timestamp_us)
            slot.ts = _timestamp_text(frame.timestamp_us, cfg)
            slot.truth["session"] = len(sessions)
            slots.append(slot)
        if slots:
            sessions.append(slots)
    return sessions, local, projected.crossed_sessions


def _duplicate_windows(sequence: RealizedSequence, cfg: "ResolvedConfig") -> dict:
    """读取 duplicate source 所属类的生效窗口表。"""
    from labelkit.common.runtime.temporal import normalize_calendar_windows

    view = cfg.class_views[sequence.plan.class_name]
    windows = effective_windows(view.windows, cfg.generate_stream.windows)
    return {item.frame_class: item for item in normalize_calendar_windows(windows)}


def _append_planned_duplicates(sessions: list[list[_StreamSlot]],
                               survivors: Sequence[RealizedSequence], plan: StreamPlan,
                               local: dict[int, int], cfg: "ResolvedConfig") -> int:
    """按预抽 source 顺序追加固定尾部 duplicate session。"""
    from labelkit.common.runtime.temporal import minimal_duplicate_shift

    by_plan = {seq.plan.index: seq for seq in survivors}
    from labelkit.common.runtime.temporal import timestamp_us

    tail = max((timestamp_us(frame.ts) for session in sessions for frame in session), default=0)
    used = 0
    for source_index in plan.duplicate_order:
        if used >= cfg.generate_stream.duplicates:
            break
        source = by_plan.get(source_index)
        if source is None:
            continue
        source_slots = [slot for session in sessions for slot in session
                        if slot.owner == local[source_index]]
        if not source_slots:
            continue
        source_times = tuple((timestamp_us(slot.ts), slot.truth["frame_class"])
                             for slot in source_slots)
        windows = _duplicate_windows(source, cfg)
        shift = minimal_duplicate_shift(source_times, tail,
                                        int(cfg.stream.gap_s * 1_000_000), windows,
                                        cfg.generate_stream.ts_start)
        duplicate: list[_StreamSlot] = []
        for slot, (timestamp, frame_class) in zip(source_slots, source_times):
            truth = {"session": len(sessions), "sequence_class": source.plan.class_name,
                     "sequence": None, **_tier_truth(source.plan.tier_rank,
                     source.plan.tier_rank is not None), "frame_class": frame_class,
                     "noise": False, "duplicate_of": source.plan.ordinal}
            item = _StreamSlot(payload=copy.deepcopy(slot.payload), truth=truth, owner=None)
            item.ts = _timestamp_text(timestamp + shift, cfg)
            duplicate.append(item)
        sessions.append(duplicate)
        tail = max(timestamp_us(item.ts) for item in duplicate)
        used += 1
    return used


def weave_planned_stream(survivors: Sequence[RealizedSequence], noise_payloads: Sequence[str],
                         plan: StreamPlan, cfg: "ResolvedConfig") -> tuple[list[list[_StreamSlot]], dict]:
    """把已冻结 planner skeleton 投影为最终 primary、noise 与 duplicate 流。

    @param survivors 通过序列校验与相似度过滤的序列。
    @param noise_payloads 已生成的噪音帧文本。
    @param plan 已冻结的时间流计划。
    @param cfg 已解析配置。
    @return (会话列表, 仅计数统计)；sessions 不含重复尾会话。
    """
    sessions, local, crossed = _projected_primary_sessions(survivors, plan,
                                                            noise_payloads, cfg)
    backfill_time_fields(sessions, cfg)
    duplicates = _append_planned_duplicates(sessions, survivors, plan, local, cfg)
    stats = {"sessions": len(sessions) - duplicates, "crossed_sessions": crossed,
             "frames": sum(len(seq.payloads) for seq in survivors),
             "noise_frames": sum(1 for session in sessions for slot in session
                                  if slot.truth["noise"]),
             "duplicates": duplicates,
             "calendar_days_spanned": _calendar_days_spanned(sessions, cfg)}
    return sessions, stats


def _calendar_days_spanned(sessions: Sequence[Sequence[_StreamSlot]],
                           cfg: "ResolvedConfig") -> int:
    """计算联合流中非噪音任务帧覆盖的 fixed-offset 自然日数。"""
    from labelkit.common.runtime.temporal import fixed_offset, timestamp_us

    timestamps = [timestamp_us(slot.ts) for session in sessions for slot in session
                  if not slot.truth["noise"]]
    if not timestamps:
        return 0
    offset = fixed_offset(cfg.generate_stream.ts_start)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    first = (epoch + timedelta(microseconds=min(timestamps))).astimezone(offset).date()
    last = (epoch + timedelta(microseconds=max(timestamps))).astimezone(offset).date()
    return (last - first).days + 1


# v1.14 时间字段面：缩减 Schema 与机械回填（§3.3）

def _reduced_gen_schema(view: "FrameClassView") -> dict | None:
    """派生 LLM 面向的逐位 Schema =「生成 Schema − 绑定键」（裁决·绑定即剔除）。

    绑定字段注定被回填尾声按已铺时间轴覆写，故从逐位 Schema 与契约行里一并剔除——
    不为注定被覆写的字段付 token 与修复环成本，契约也不误导。层级拷贝纪律：顶层与
    ``properties`` 两层重建、``required`` 取差集（容忍绑定键不在 required），其余关键字
    连同各属性子 Schema 引用原样——``FrameClassView.gen_schema`` 是 M1 冻结产物（静态
    预算预检与契约行渲染同源读它），绝不就地改动。

    :param view: 该帧类的配置视图（``gen_schema`` + ``time_fields``）。
    :returns: 派生产物（恒为新顶层字典；无绑定表时逐字段等于原 Schema）；纯文本帧
        （未声明生成 Schema）返回 None。
    """
    schema = view.gen_schema
    if schema is None:
        return None
    bound = set(view.time_fields or ())
    reduced = dict(schema)
    if "properties" in reduced:
        reduced["properties"] = {name: sub
                                 for name, sub in reduced["properties"].items()
                                 if name not in bound}
    if "required" in reduced:
        reduced["required"] = [name for name in reduced["required"]
                               if name not in bound]
    return reduced


def _time_field_values(stamps: Sequence[datetime], position: int,
                       ts: str) -> dict[str, object]:
    """一帧的语义词表四值（裁决·语义词表四值 / 序内间隔口径）。

    间隔按**本序列相邻成员**计——交叉会话夹入的外序列帧与噪音帧本就占用其间墙钟，
    序内差值与下游从数据实测的口径一致。数值取 ``round(·, 6)``（微秒精度，与
    isoformat 写出的分辨率对齐）；首帧的 ``gap_prev_s``/``elapsed_s`` 与末帧的
    ``gap_next_s`` 恒 0.0。

    :param stamps: 本序列全部成员的已铺时间戳（序内成员序）。
    :param position: 本帧在序内的位置（0 基）。
    :param ts: 该槽位已铺的 ISO-8601 串（``ts`` 词直取）。
    :returns: {语义词 → 值} 的四键字典。
    """
    current = stamps[position]
    previous = (current - stamps[position - 1]).total_seconds() if position else 0.0
    following = ((stamps[position + 1] - current).total_seconds()
                 if position + 1 < len(stamps) else 0.0)
    return {"ts": ts,
            "gap_prev_s": round(previous, 6),
            "gap_next_s": round(following, 6),
            "elapsed_s": round((current - stamps[0]).total_seconds(), 6)}


def backfill_time_fields(sessions: list[list[_StreamSlot]],
                         cfg: "ResolvedConfig") -> None:
    """机械回填尾声（纯函数族，零 rng 零 LLM 零 IO；裁决·时间字段回填方向）。

    调用点 = ``weave_stream`` 之后、``assemble_stream`` 之前：回填先于行对象与 id 计算
    （裁决·回填后计 id），故工件行、成员 ``Record.raw``/``text``/id、序列 id 与 session_id
    全部含回填值，工件重放逐字节同 id 同会话。只遍历任务帧槽位（``owner`` 非 None）并按
    owner 归组（会话序即序内成员序——交叉切片不改序内次序），对绑定帧类逐帧
    把绑定值
    **就地写入共享载荷对象**（载荷恒为 JSON 对象由 M1 的绑定表前提保证：仅结构化帧可
    绑定）；每个载荷对象恰被写入一次（一条序列在 owned 会话中恰出现一次）。
    重发槽位不
    遍历也不触碰——它与源槽位引用同一载荷对象，回填自动生效且其 ``ts`` 绑定值承源、
    ≠ 自身行 ts（裁决·重发帧承源档与同源载荷）。噪音帧与无绑定帧类：不触碰。

    @param sessions 已交织并铺好 ts 的会话列表（就地修改其中的载荷对象）。
    @param cfg 已解析配置（读 ``frame_class_views`` 的绑定表）。
    """
    views = cfg.frame_class_views
    groups: dict[int, list[_StreamSlot]] = {}
    for session in sessions:
        for slot in session:
            if slot.owner is not None:
                groups.setdefault(slot.owner, []).append(slot)
    for slots in groups.values():
        stamps = [datetime.fromisoformat(slot.ts) for slot in slots]
        for position, slot in enumerate(slots):
            bindings = views[slot.truth["frame_class"]].time_fields
            if not bindings:
                continue
            values = _time_field_values(stamps, position, slot.ts)
            for field, semantic in bindings.items():
                slot.payload[field] = values[semantic]


# v1.13 直装组装

def stream_artifact_path(cfg: "ResolvedConfig") -> str:
    """工件路径推导：输出路径去末级后缀 + ".stream.jsonl"。M11 Emitter 的工件通道
    用同一规则各自推导（算子间不互导，两侧等式由测试钉住）。

    @param cfg 已解析配置。
    @return 时间流工件 JSONL 路径。
    """
    return str(Path(cfg.run.output).with_suffix("")) + ".stream.jsonl"


def _payload_text(payload: "str | Mapping") -> str:
    """text_field 值的 M2 语义投影：字符串直取、对象 canonical JSON（重放时 M2
    的 dotted-path 提取产出同一投影——裁决·工件行即 raw）。"""
    return payload if isinstance(payload, str) else canonical_json(payload)


def _stream_envelope(seq: RealizedSequence, records: tuple[Record, ...],
                     session_id: str) -> PipelineItem:
    """一条幸存序列的直装信封：sequence Record（S24 字段惯例、ref = 首成员 ref、
    id = M14 公式 sha256("\\n".join(member ids))[:16]）+ session_id + 序列级/帧级
    两级 inherited 标签（帧级真值随 member_classifications 落 members[]）。"""
    joined = "\n".join(record.id for record in records)
    label = seq.plan.class_name
    sequence_record = Record(
        id=hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16],
        modality="text", text=None, raw=None, ui_tree=None, image=None,
        ref=records[0].ref, kind="sequence", members=records)
    member_classifications = {
        record.id: Classification(label=frame_class, labels=(frame_class,),
                                  source="inherited", detail={})
        for record, frame_class in zip(records, seq.frame_classes)}
    return PipelineItem(
        record=sequence_record, session_id=session_id,
        classification=Classification(label=label, labels=(label,),
                                      source="inherited", detail={}),
        member_classifications=member_classifications)


def assemble_stream(sessions: list[list[_StreamSlot]],
                    survivors: Sequence[RealizedSequence],
                    cfg: "ResolvedConfig") -> tuple[list[str], list[PipelineItem]]:
    """直装组装（裁决·工件行即 raw / 真值不携最终 id）：逐行构造工件行对象
    ``{<ts字段>: …, <text_field>: …, "truth": {…}}``（行序列化 json.dumps
    ensure_ascii=False 族；canonical_json 只用于 id 计算）与成员 Record（id =
    M2 公式、行号 = 列表序 + 1）；session_id = M2 公式（含噪音帧与重复帧）；
    噪音/重复帧只活在工件。信封按计划序返回。v1.14：档位表在场时成员
    ``ref.generator`` 增第三键 tier_rank（= owner 序列的档位序数）。

    @param sessions 已交织并完成时间铺设的会话列表。
    @param survivors 通过过滤且按计划序排列的幸存序列。
    @param cfg 已解析配置。
    @return (工件 JSONL 行列表, 序列信封列表)。
    """
    ts_field = cfg.stream.order_by[len("meta:"):]
    text_field = cfg.input.text_field
    path = stream_artifact_path(cfg)
    lines: list[str] = []
    session_ids: list[str] = []
    members: dict[int, list[Record]] = {}
    owner_session: dict[int, int] = {}
    for session_no, session in enumerate(sessions):
        frame_ids: list[str] = []
        for slot in session:
            row = {ts_field: slot.ts, text_field: slot.payload, "truth": slot.truth}
            rec_id = hashlib.sha256(
                canonical_json(row).encode("utf-8")).hexdigest()[:16]
            frame_ids.append(rec_id)
            lines.append(json.dumps(row, ensure_ascii=False))
            if slot.owner is None:
                continue                   # 噪音/重复帧不构造信封
            plan = survivors[slot.owner].plan
            generator: dict = {"llm": plan.llm, "style": plan.style_name}
            if plan.tier_rank is not None:      # v1.14 裁决·档位标识三点落位其一
                generator["tier_rank"] = plan.tier_rank
            members.setdefault(slot.owner, []).append(Record(
                id=rec_id, modality="text", text=_payload_text(slot.payload),
                raw=row, ui_tree=None, image=None,
                ref=RecordRef(source_file=path, line_no=len(lines), pair_index=None,
                              generated_from=(), generator=generator)))
            owner_session.setdefault(slot.owner, session_no)
        session_ids.append(hashlib.sha256(
            "\n".join(frame_ids).encode("utf-8")).hexdigest()[:16])
    envelopes = [_stream_envelope(survivors[owner], tuple(members[owner]),
                                  session_ids[owner_session[owner]])
                 for owner in sorted(members)]
    return lines, envelopes


async def _realize_degrading(realize, span: tuple[int, int], ctx: "RunContext",
                             level: int = 0) -> list[list]:
    """帧实现的反应式对半降级（classify._judge_frames_degrading 零重叠版同型）：
    reactive ContextOverflowError ⇒ [s, m) / [m, e) 顺序重试（每次对半计
    budget.degrade_retries，≤ _MAX_STREAM_DEGRADE_LEVELS 级；schema 与蓝图概要
    随切片同步减半）；precheck 相位、单步跨度或级数耗尽 ⇒ 原样上抛由调用方作废
    序列。返回跨度序的叶结果列表（帧载荷列表）。"""
    try:
        return [await realize(span)]
    except ContextOverflowError as exc:
        start, end = span
        if (exc.phase != "reactive" or end - start < 2
                or level >= _MAX_STREAM_DEGRADE_LEVELS):
            raise
        ctx.metrics.count("budget.degrade_retries")
        middle = (start + end) // 2
        leaves = await _realize_degrading(realize, (start, middle), ctx, level + 1)
        leaves.extend(await _realize_degrading(realize, (middle, end), ctx, level + 1))
        return leaves


# 算子本体

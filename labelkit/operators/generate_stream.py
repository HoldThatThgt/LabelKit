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

from labelkit.common.config.model import effective_frame_rules, effective_frame_windows
from labelkit.common.contracts.types import Classification, PipelineItem, Record, RecordRef
from labelkit.common.errors import ContextOverflowError

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
    from labelkit.common.runtime.scenario.model import NoiseSlot, ScenarioPlan

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


# v1.17 交付计划：cfg.scenario_plan（M1 冻结）→ M6 的逐槽位调用计划

@dataclass(frozen=True)
class SequencePlan:
    """一条 sequence slot 的交付计划（brief 与帧实现共用）。"""
    index: int                  # 计划序全局序号 0 基（= ScenarioPlan.slots 顺序）
    class_name: str             # 所属序列类
    ordinal: int                # 类内序数 0 基（= 工件 truth.sequence）
    length: int                 # 步数 L（slot 冻结的 length_target）
    llm: str                    # 预抽 profile——brief+实现绑定同一 profile
    style_name: str | None      # 预抽风格名（实现才生效，brief 不带风格）
    style_prompt: str | None    # 预抽风格提示词
    slot_key: str = ""          # ScenarioPlan 的稳定槽位键（sequence:<class>:<ordinal>）
    tier_rank: int | None = None    # 档位序数（slot spec 承源；None = 档位面不在场）
    frame_classes: tuple[str, ...] = ()  # planner 冻结的逐位帧类词
    timestamps_us: tuple[int, ...] = ()  # planner 冻结的任务帧微秒时间


@dataclass(frozen=True)
class NoiseCallPlan:
    """一个 noise slot 的交付计划（单槽一次 realize 调用，复用帧类 realize 路径）。"""
    index: int                  # 噪音槽计划序号 0 基（= ScenarioPlan.noise_slots 顺序）
    frame_class: str            # 该槽的噪音帧类（frame_class 真值实名）
    llm: str                    # 独立预抽 profile
    style_name: str | None      # 预抽风格名（全局 styles 池）
    style_prompt: str | None    # 预抽风格提示词


@dataclass(frozen=True)
class StreamPlan:
    """时间流生成的交付计划（计划面全部承源 ``cfg.scenario_plan``，零重排）。"""
    sequences: tuple[SequencePlan, ...]     # 计划序（ScenarioPlan.slots 顺序）
    noise_plans: tuple[NoiseCallPlan, ...]  # 计划序（ScenarioPlan.noise_slots 顺序）


@dataclass(frozen=True)
class RealizedSequence:
    """brief + 帧实现都成功后的一条序列（装配的输入单元）。"""
    plan: SequencePlan                      # 该序列的交付计划
    frame_classes: tuple[str, ...]          # planner 冻结词（帧级真值）
    payloads: tuple = ()                    # 逐帧 text_field 值（str 或结构化帧对象）


@dataclass(frozen=True)
class RealizedNoise:
    """一个交付成功的 noise slot（槽位身份 + 帧载荷）。"""
    slot: "NoiseSlot"                       # ScenarioPlan 冻结的噪音槽位
    payload: "str | Mapping"                # 帧内容（结构化噪音 = JSON 对象）


@dataclass(frozen=True)
class StreamGenerateProduct:
    """``generate_stream_all`` 的富返回（裁决·时间流入口与配额截断）——
    ``PipelineItem(record=r)`` 裸构造无法携带 session_id/classification/
    member_classifications，故必须整信封交付。"""
    envelopes: list[PipelineItem]           # 直装序列信封（计划序）
    artifact_lines: list[str]               # 工件行（时间序定稿；行号 = 列表序 + 1）


def plan_delivery(cfg: "ResolvedConfig") -> StreamPlan:
    """把 M1 冻结的 ``ScenarioPlan`` 展开为 M6 的逐槽位调用计划（§11 delivery.profile）。

    布局（word/length/session/timestamp/tier）零重排承源 ``cfg.scenario_plan``；
    本函数只按 ``Random(f"{seed}:delivery.profile")`` 流预抽每槽位的 (llm, style)——
    sequence 槽位在前（slot 顺序，风格池取类有效表）、noise 槽位在后（全局风格池），
    同 seed 下与槽位交付成败无关地逐字节一致。

    @param cfg 已解析配置（``scenario_plan`` 已由 M1 冻结）。
    @return 交付计划。
    """
    import random as _random

    plan = cfg.scenario_plan
    g = cfg.generate
    noise_plans_src = plan.noise_slots
    styles_by_index = ([cfg.class_views[slot.sequence_class].generate.styles
                        for slot in plan.slots]
                       + [g.styles] * len(noise_plans_src))
    pairs = predraw_llm_style(g, len(plan.slots) + len(noise_plans_src),
                              _random.Random(f"{cfg.run.seed}:delivery.profile"),
                              styles_by_index=styles_by_index)
    words: dict[str, tuple[str, ...]] = {}
    stamps: dict[str, tuple[int, ...]] = {}
    for layout in plan.layouts:
        words[layout.slot_key] = tuple(frame.frame_class for frame in layout.frames)
        stamps[layout.slot_key] = tuple(frame.start_us for frame in layout.frames)
    sequences = tuple(
        SequencePlan(index=i, slot_key=slot.key, class_name=slot.sequence_class,
                     ordinal=slot.class_ordinal, length=slot.length_target,
                     llm=pairs[i][0],
                     style_name=pairs[i][1].name if pairs[i][1] else None,
                     style_prompt=pairs[i][1].prompt if pairs[i][1] else None,
                     tier_rank=slot.tier_rank,
                     frame_classes=words.get(slot.key, ()),
                     timestamps_us=stamps.get(slot.key, ()))
        for i, slot in enumerate(plan.slots))
    offset = len(plan.slots)
    noise_plans = tuple(
        NoiseCallPlan(index=j, frame_class=slot.frame_class,
                      llm=pairs[offset + j][0],
                      style_name=(pairs[offset + j][1].name
                                  if pairs[offset + j][1] else None),
                      style_prompt=(pairs[offset + j][1].prompt
                                    if pairs[offset + j][1] else None))
        for j, slot in enumerate(noise_plans_src))
    return StreamPlan(sequences=sequences, noise_plans=noise_plans)


# v1.17 布局驱动装配：纯函数族，零 LLM、零 IO、零 rng

@dataclass
class _StreamSlot:
    """装配后的一帧槽位（工件行装配前形态；仅本模块内部可变）。"""
    payload: "str | Mapping"    # text_field 值（结构化帧 = 行内对象）
    truth: dict                 # 冻结键集 truth（session 值装配尾声回填）
    owner: int | None           # 幸存序列下标（任务帧）；噪音/重复帧 = None
    ts: str = ""                # planner µs 铺出的 ISO-8601 时间戳
    ts_us: int = 0              # 该帧的绝对微秒（内部排序/回填用）


def _tier_truth(tier_rank: int | None, tiered: bool) -> dict:
    """truth 的档位片段（裁决·真值键序重冻结）：档位表在场才有这一键，位于
    ``sequence`` 之后、``frame_class`` 之前——三个槽位构造点按同一位置内联展开。

    :param tier_rank: 该帧承载的档位序数（噪音帧恒 None）。
    :param tiered: 档位表是否在场；False ⇒ 空片段（键不在场，v1.13 字节回退）。
    :returns: 可直接解包进 truth 字面量的单键或空字典。
    """
    return {"tier_rank": tier_rank} if tiered else {}


def _us_iso(timestamp_us: int, cfg: "ResolvedConfig") -> str:
    """把 planner 绝对微秒转换为 schedule 固定 offset 的 ISO 文本。"""
    from datetime import datetime, timedelta, timezone as tz

    offset = tz(timedelta(minutes=cfg.generate_stream.schedule.utc_offset_minutes))
    epoch = datetime(1970, 1, 1, tzinfo=tz.utc)
    return (epoch + timedelta(microseconds=timestamp_us)).astimezone(offset).isoformat(
        timespec="microseconds")


def _task_slots(local_owner: int, sequence: RealizedSequence, layout: Any,
                cfg: "ResolvedConfig") -> list[_StreamSlot]:
    """一条幸存序列的任务帧槽位（truth.session 占位 −1，装配尾声回填）。"""
    plan = sequence.plan
    tier = _tier_truth(plan.tier_rank, tiered=plan.tier_rank is not None)
    return [_StreamSlot(payload=sequence.payloads[i],
                        truth={"session": -1, "sequence_class": plan.class_name,
                               "sequence": plan.ordinal, **tier,
                               "frame_class": sequence.frame_classes[i], "noise": False},
                        owner=local_owner,
                        ts=_us_iso(frame.start_us, cfg),
                        ts_us=frame.start_us)
            for i, frame in enumerate(layout.frames)]


def _noise_frame_slot(realized: RealizedNoise,
                      cfg: "ResolvedConfig") -> _StreamSlot:
    """一帧 noise 槽位（v1.17：truth.frame_class = 实际噪音类名，其余三 null）。"""
    return _StreamSlot(payload=realized.payload,
                       truth={"session": -1, "sequence_class": None, "sequence": None,
                              **_tier_truth(None, tiered=bool(cfg.generate_stream.tiers)),
                              "frame_class": realized.slot.frame_class, "noise": True},
                       owner=None,
                       ts=_us_iso(realized.slot.timestamp_us, cfg),
                       ts_us=realized.slot.timestamp_us)


def _duplicate_slots(source: RealizedSequence, dup: Any,
                     cfg: "ResolvedConfig") -> list[_StreamSlot]:
    """一条流尾 duplicate 的槽位：载荷深拷贝（含已回填时间字段，裁决·双时间语义），
    ts = DuplicateLayout.frames 的平移后时间；truth.duplicate_of = 源类内序数。"""
    import copy as _copy

    plan = source.plan
    tier = _tier_truth(plan.tier_rank, tiered=plan.tier_rank is not None)
    return [_StreamSlot(payload=_copy.deepcopy(source.payloads[frame.position]),
                        truth={"session": -1, "sequence_class": plan.class_name,
                               "sequence": None, **tier,
                               "frame_class": frame.frame_class,
                               "noise": False, "duplicate_of": plan.ordinal},
                        owner=None,
                        ts=_us_iso(frame.start_us, cfg),
                        ts_us=frame.start_us)
            for frame in dup.frames]


def weave_scenario_stream(survivors: Sequence[RealizedSequence],
                          noises: Sequence[RealizedNoise], plan: "ScenarioPlan",
                          cfg: "ResolvedConfig") -> tuple[list[list[_StreamSlot]], dict]:
    """把冻结 ``ScenarioPlan`` 投影为最终 primary、noise 与 duplicate 流（零 rng）。

    作废槽位只缺席（v1.16 void 语义过渡）：空 session 消失、幸存者保留 planner
    时间戳、session 按时间重编号；noise 仅当严格落于其 session 幸存任务帧的
    首末之间才存活；duplicate 的 source 未交付则整条省略（§9.4 shortfall 语义，
    计数面挂 Wave 6/7 的 delivery 块）。

    @param survivors 通过校验的序列（计划序）。
    @param noises 交付成功的 noise 槽位。
    @param plan M1 冻结的场景计划。
    @param cfg 已解析配置。
    @return (会话列表, 仅计数统计)。
    """
    layout_by_slot = {layout.slot_key: layout for layout in plan.layouts}
    local_index = {seq.plan.index: i for i, seq in enumerate(survivors)}
    by_slot_key = {seq.plan.slot_key: seq for seq in survivors}
    sessions: list[list[_StreamSlot]] = []
    crossed = 0
    for session in plan.sessions:
        entries: list[tuple[int, _StreamSlot]] = []
        for slot_key in (session.primary_slot_key, session.secondary_slot_key):
            sequence = by_slot_key.get(slot_key) if slot_key is not None else None
            if sequence is None:
                continue
            layout = layout_by_slot[slot_key]
            entries.extend(zip(
                (frame.start_us for frame in layout.frames),
                _task_slots(local_index[sequence.plan.index], sequence, layout, cfg)))
        task_times = [stamp for stamp, _ in entries]
        for realized in noises:
            if realized.slot.session_index != session.index or not task_times:
                continue    # 任务帧全部作废的 session ⇒ noise 一并不存活
            if not min(task_times) < realized.slot.timestamp_us < max(task_times):
                continue
            entries.append((realized.slot.timestamp_us,
                            _noise_frame_slot(realized, cfg)))
        if not any(slot.owner is not None for _, slot in entries):
            continue            # 无任务帧 ⇒ session 消失
        if (session.secondary_slot_key in by_slot_key
                and session.primary_slot_key in by_slot_key):
            crossed += 1
        entries.sort(key=lambda item: item[0])
        sessions.append([slot for _, slot in entries])
    backfill_time_fields(sessions, cfg)     # 先回填：duplicate 深拷贝承源回填值
    duplicates = 0
    for dup in plan.duplicates:
        source = by_slot_key.get(dup.source_slot_key)
        if source is None:
            continue            # 源槽位未交付 ⇒ 省略（shortfall 语义）
        sessions.append(_duplicate_slots(source, dup, cfg))
        duplicates += 1
    for session_no, session in enumerate(sessions):
        for slot in session:
            slot.truth["session"] = session_no
    stats = {"sessions": len(sessions) - duplicates, "crossed_sessions": crossed,
             "frames": sum(len(seq.payloads) for seq in survivors),
             "noise_frames": sum(1 for session in sessions for slot in session
                                  if slot.truth["noise"]),
             "duplicates": duplicates,
             "calendar_days_spanned": _calendar_days_spanned(sessions, cfg)}
    return sessions, stats


def _calendar_days_spanned(sessions: Sequence[Sequence[_StreamSlot]],
                           cfg: "ResolvedConfig") -> int:
    """计算流中非噪音任务帧（含 duplicate 尾）覆盖的固定 offset 自然日数。"""
    from datetime import datetime, timedelta, timezone as tz

    timestamps = [slot.ts_us for session in sessions for slot in session
                  if not slot.truth["noise"]]
    if not timestamps:
        return 0
    offset = tz(timedelta(minutes=cfg.generate_stream.schedule.utc_offset_minutes))
    epoch = datetime(1970, 1, 1, tzinfo=tz.utc)
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

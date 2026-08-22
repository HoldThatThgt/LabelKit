"""v1.18 序列生成配置载体与聚合解析器。

本模块只拥有 sequence 形态的配置语义。flat 生成仍由 ``config.model.GenerateConfig``
承载；二者没有兼容键、迁移或运行期回落。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Mapping
from jsonpointer import JsonPointer, JsonPointerException
from labelkit.common.config._collect import _Collector, _fmt
from labelkit.common.config._schemas import _GenerationSchemaRequest, _load_generation_schema
from labelkit.common.extensions.hooks import (
    ResolvedHook,
    check_hook_arity,
    clone_state_input,
    normalize_state_violations,
    state_probe_input,
)
if TYPE_CHECKING:
    from labelkit.common.config.model import FrameClassView
    from labelkit.common.contracts.generation import GenerationParseContext
_NAME_RE = re.compile(r"[a-z0-9_]+")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_OBJECT_SCHEMA: Mapping[str, object] = MappingProxyType({"type": "object"})
def is_generation_frame_eligible(view: "FrameClassView") -> bool:
    """判断帧类是否具备 instruction-only 事件生成的完整契约。

    @param view 生效 FrameClassView
    @return 描述、生成指令与对象 Schema 均存在时为 true
    """
    return bool(
        view.description.strip()
        and view.gen_instruction
        and view.gen_schema is not None
    )


def _freeze_value(value: object) -> object:
    """递归复制并冻结配置中的 JSON 容器。

    @param value 待冻结值
    @return 不可变 Mapping/tuple 或原标量
    """
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_value(item) for item in value)
    return value


class _ImmutableConfig:
    """为 generation 配置载体统一冻结嵌套 JSON 容器。"""

    def __post_init__(self) -> None:
        """复制并冻结当前 dataclass 的容器字段。"""
        for item in fields(self):
            value = getattr(self, item.name)
            frozen = _freeze_value(value)
            if frozen is not value:
                object.__setattr__(self, item.name, frozen)


@dataclass(frozen=True)
class SequenceClassGenerationConfig(_ImmutableConfig):
    """一个 declared sequence class 的 v1.18 生成专用配置。"""

    instruction: str                              # 类级世界生成指令
    state_schema: Mapping[str, object]            # 完整状态 Draft 2020-12 Schema
    initial_state_source: Literal["llm", "catalog"]  # ScenarioSeed 来源
    initial_state_catalog_path: str | None        # catalog 的绝对规范路径
    initial_state_catalog: tuple[Mapping[str, object], ...]  # 启动期校验后的完整行


@dataclass(frozen=True)
class PayloadBindingSpec(_ImmutableConfig):
    """一个从状态快照到渲染 payload 的机械 binding。"""

    payload_path: str                             # payload 内 RFC 6901 path
    state_phase: Literal["before", "after"]      # 取执行前或执行后状态
    state_path: str                               # state 内 RFC 6901 path


@dataclass(frozen=True)
class RoleSpec(_ImmutableConfig):
    """declared sequence pattern 中恰好出现一次的业务 role。"""

    name: str                                     # pattern 内唯一角色名
    frame_class: str                              # 引用的帧类名
    actor: str                                    # 执行事件的 actor
    read_roots: tuple[str, ...]                   # 可读取状态根
    write_roots: tuple[str, ...]                  # 可修改状态根
    publish_roots: tuple[str, ...]                # 可发布给 observers 的根
    observers: tuple[str, ...]                    # 可观察发布结果的 actor
    state_instruction: str                        # 状态转换指令
    pre_state_schema: Mapping[str, object] | None # 可选执行前完整状态 Schema
    payload_bindings: tuple[PayloadBindingSpec, ...]  # 权威 payload 覆盖声明
    calendar_window: str | None                   # 可选命名日历窗口


@dataclass(frozen=True)
class GapSpec(_ImmutableConfig):
    """两个正向 role 之间的一条闭区间整数微秒约束。"""

    name: str                                     # pattern 内唯一 gap 名
    before: str                                   # 前置 role
    after: str                                    # 后置 role
    min_gap_us: int                               # 最小闭区间微秒
    max_gap_us: int                               # 最大闭区间微秒


@dataclass(frozen=True)
class SequencePattern(_ImmutableConfig):
    """一个精确 declared role 全集、顺序、gap 集与跨度。"""

    name: str                                     # 全局唯一 pattern 名
    sequence_class: str                           # 引用的 sequence class
    description: str                              # 盲审使用的自然语言描述
    roles: tuple[RoleSpec, ...]                   # 声明序 role 表
    order: tuple[str, ...]                        # 全量唯一 role 排列
    gaps: tuple[GapSpec, ...]                     # 正向 gap 集
    max_span_us: int                              # 最大闭区间跨度微秒


@dataclass(frozen=True)
class VariantSpec(_ImmutableConfig):
    """一个派生 positive 或 counterfactual branch。"""

    name: str                                     # set 内唯一变体名
    kind: Literal[                         # 正向或三种反事实变体种类
        "positive", "missing", "reordered", "interval_exceeded"]
    target: Mapping[str, str | int]               # 规范化目标参数
    outcome_schema: Mapping[str, object]          # 最终状态结果 Schema
    expected_violation: Mapping[str, str]         # 独立 oracle 的唯一期望违规
    divergence_role: str | None                   # 首个因果分叉 role


@dataclass(frozen=True)
class CounterfactualSetSpec(_ImmutableConfig):
    """一个共享 ScenarioSeed 的精确数量 declared 交付组。"""

    name: str                                     # 全局唯一 set 名
    pattern: str                                  # 引用的 pattern 名
    count: int                                    # 精确场景数量
    variants: tuple[VariantSpec, ...]             # 声明序变体表


@dataclass(frozen=True)
class InstructionOnlySpec(_ImmutableConfig):
    """一条精确数量 instruction-only 交付声明。"""

    name: str                                     # 全局唯一声明名
    sequence_class: str                           # 引用的 sequence class
    count: int                                    # 精确序列数量
    len_range: tuple[int, int]                    # 冻结长度抽取域
    instruction: str                              # 完整生成指令
    state_schema: Mapping[str, object]            # 完整状态 Schema


@dataclass(frozen=True)
class TimelineSpec(_ImmutableConfig):
    """冻结整数时间线与精确交付基数。"""

    timestamp_start_us: int                       # 带固定 offset 的起始 epoch 微秒
    utc_offset_minutes: int                       # 起始时间固定 UTC offset
    event_gap_us: tuple[int, int]                 # instruction/noise/replay 间隔闭区间
    primary_sessions: int                         # primary session 精确数
    crossed_primary_sessions: int                 # 双 owner session 精确数
    session_max_events: int                       # 单 session 事件容量
    session_max_span_us: int                      # 单 session 最大跨度微秒
    session_gap_us: int                           # session 起点间隔微秒
    noise_events: int                             # noise 精确事件数
    duplicate_sequences: int                     # replay 精确序列数


@dataclass(frozen=True)
class CalendarWindowSpec(_ImmutableConfig):
    """一个固定 UTC offset 的命名 calendar window。"""

    name: str                                     # 全局唯一窗口名
    utc_offset_minutes: int                       # 固定 UTC offset
    days: tuple[                           # 生效星期名闭集
        Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"], ...]
    intervals_us: tuple[tuple[int, int], ...]     # 同日半开墙钟区间


@dataclass(frozen=True)
class NoiseSpec(_ImmutableConfig):
    """可选的精确 noise-slot 渲染声明。"""

    frame_class: str                              # noise 专用帧类
    instruction: str                              # 完整 noise 生成指令
    topics: tuple[str, ...]                       # 按 ordinal 唯一绑定的话题


@dataclass(frozen=True)
class GenerationLimits(_ImmutableConfig):
    """不可配置的 v1.18 编译期与 retained-content 上限。"""

    pattern_roles: int = 32                       # 单 pattern 最大 role 数
    variants_per_counterfactual_set: int = 8      # 单 set 最大 variant 数
    instruction_only_events: int = 8              # instruction-only 最大事件数
    scenario_seed_bytes: int = 65536              # ScenarioSeed canonical byte 上限
    state_or_outcome_schema_bytes: int = 65536    # 状态或结果 Schema byte 上限
    frame_schema_bytes: int = 65536               # 帧 Schema byte 上限
    event_patch_bytes: int = 16384                 # 单事件 patch byte 上限
    rendered_payload_bytes: int = 65536            # 单 payload byte 上限
    prompt_value_bytes: int = 32768                 # 单个运行期动态提示值 byte 上限
    repair_context_bytes: int = 32768               # 单轮 L3 新增上下文 byte 上限
    prompt_text_bytes: int = 32768                 # 单项生成提示文本 UTF-8 byte 上限
    record_units: int = 500000                     # 派生 record unit 上限
    stream_rows: int = 500000                      # 派生 stream row 上限
    retained_content_bytes: int = 536870912        # 最终内存内容 byte 上限


@dataclass(frozen=True)
class SequenceGenerationConfig(_ImmutableConfig):
    """冻结的 v1.18 sequence-only 解析产物；flat generation 时不存在。"""

    mode: Literal["declared", "instruction_only"]  # 唯一生成模式
    semantic_profile: str                         # 生成与渲染 profile
    evaluation_profile: str                       # 独立语义判定 profile
    max_slot_attempts: int                        # 每个精确槽的交付尝试上限
    state_validator: ResolvedHook | None          # 可选冻结状态 hook
    patterns: tuple[SequencePattern, ...]         # declared pattern 表
    counterfactual_sets: tuple[CounterfactualSetSpec, ...]  # declared set 表
    instruction_only: tuple[InstructionOnlySpec, ...]  # instruction-only 表
    timeline: TimelineSpec                        # 完整时间线声明
    calendar_windows: Mapping[str, CalendarWindowSpec]  # 窗口名到冻结窗口
    noise: NoiseSpec | None                       # 可选 noise 声明
    limits: GenerationLimits                      # 固定实现上限


@dataclass(frozen=True)
class _ParseState:
    """序列解析期间共享的来源、上下文与固定上限。"""

    raw: Mapping[str, object]                     # 原始 project 映射
    context: Any                                  # GenerationParseContext，避免导入环
    generate: Mapping[str, object]                # 原始 generate 表
    limits: GenerationLimits                      # 固定上限


@dataclass(frozen=True)
class _BudgetCase:
    """一次实际 sequence LLM 调用的启动期最小包络。"""

    profile: str                                  # 首轮 profile 名
    prompt: object                                # 完整最小 PromptBundle
    schema: Mapping[str, object]                  # 本次完整 active Schema
    post_validated: bool                          # EventPlan 后置修复对话形态
    dynamic_byte_limits: tuple[int, ...] = ()     # 运行期动态值的逐值 byte 上界


def _error(state: _ParseState, location: str, message: str) -> None:
    """追加一条稳定的 sequence 配置错误。

    @param state 当前解析状态
    @param location project 内键定位
    @param message 英文错误描述
    """
    state.context.collector.error(
        f"{state.context.project_root / 'project.toml'}:{location}: {message}"
    )


def _table(state: _ParseState, parent: Mapping[str, object], key: str,
           location: str) -> Mapping[str, object]:
    """读取一张可选子表并聚合类型错误。

    @param state 当前解析状态
    @param parent 父映射
    @param key 子表键
    @param location 子表定位
    @return 合法映射或空映射
    """
    value = parent.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        _error(state, location, f"expected table, got {_fmt(value)}")
        return {}
    return value


def _rows(state: _ParseState, parent: Mapping[str, object], key: str,
          location: str) -> tuple[Mapping[str, object], ...]:
    """读取一个 TOML 表数组并忽略非法元素。

    @param state 当前解析状态
    @param parent 父映射
    @param key 表数组键
    @param location 表数组定位
    @return 合法映射元素元组
    """
    value = parent.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        _error(state, location, f"expected array of tables, got {_fmt(value)}")
        return ()
    out: list[Mapping[str, object]] = []
    for index, row in enumerate(value, 1):
        if isinstance(row, Mapping):
            out.append(row)
        else:
            _error(state, f"{location}[{index}]", f"expected table, got {_fmt(row)}")
    return tuple(out)


def _check_keys(state: _ParseState, row: Mapping[str, object],
                allowed: frozenset[str], location: str) -> None:
    """拒绝 sequence-owned 表中的未知或已删除键。

    @param state 当前解析状态
    @param row 原始表
    @param allowed 精确键闭集
    @param location 表定位
    """
    for key in row:
        if key not in allowed:
            _error(state, f"{location}.{key}",
                   "generation_config_invalid: unknown or deleted sequence key")


def _string(state: _ParseState, row: Mapping[str, object], key: str,
            location: str, required: bool = True) -> str:
    """读取一个可选非空字符串。

    @param state 当前解析状态
    @param row 来源表
    @param key 键名
    @param location 键定位
    @param required 缺失是否报错
    @return 合法字符串或空串
    """
    value = row.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value.strip():
        _error(state, location, f"expected non-empty string, got {_fmt(value)}")
        return ""
    return value


def _integer(state: _ParseState, row: Mapping[str, object], key: str,
             location: str, minimum: int = 0) -> int:
    """读取一个有下界的整数。

    @param state 当前解析状态
    @param row 来源表
    @param key 键名
    @param location 键定位
    @param minimum 闭区间下界
    @return 合法整数或下界
    """
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _error(state, location, f"expected integer >= {minimum}, got {_fmt(value)}")
        return minimum
    return value


def _seconds_us(state: _ParseState, value: object, location: str,
                positive: bool) -> int:
    """无损把最多六位小数的秒数转换为整数微秒。

    @param state 当前解析状态
    @param value TOML 数值
    @param location 键定位
    @param positive 是否要求严格大于零
    @return 合法整数微秒或零
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _error(state, location, f"expected numeric seconds, got {_fmt(value)}")
        return 0
    try:
        decimal = Decimal(str(value))
    except InvalidOperation:
        _error(state, location, f"expected finite decimal seconds, got {_fmt(value)}")
        return 0
    scaled = decimal * 1_000_000
    if not decimal.is_finite() or scaled != scaled.to_integral_value():
        _error(state, location, "expected at most six decimal places")
        return 0
    result = int(scaled)
    if result < 0 or (positive and result == 0):
        condition = "> 0" if positive else ">= 0"
        _error(state, location, f"expected seconds {condition}, got {_fmt(value)}")
        return 0
    return result


def _name(state: _ParseState, value: object, location: str) -> str:
    """读取一个小写下划线标识符。

    @param state 当前解析状态
    @param value 原始值
    @param location 键定位
    @return 合法名称或空串
    """
    if not isinstance(value, str) or _NAME_RE.fullmatch(value) is None:
        _error(state, location, f"expected name matching [a-z0-9_]+, got {_fmt(value)}")
        return ""
    return value


def _string_tuple(state: _ParseState, value: object, location: str) -> tuple[str, ...]:
    """读取字符串数组并逐元素定位错误。

    @param state 当前解析状态
    @param value 原始值
    @param location 键定位
    @return 合法字符串元组
    """
    if not isinstance(value, list):
        _error(state, location, f"expected string array, got {_fmt(value)}")
        return ()
    if any(not isinstance(item, str) for item in value):
        _error(state, location, f"expected string array, got {_fmt(value)}")
        return ()
    return tuple(value)


def _load_schema(state: _ParseState, row: Mapping[str, object], location: str,
                 optional: bool = False) -> Mapping[str, object] | None:
    """从 path/inline 二选一装载一份受限生成 Schema。

    @param state 当前解析状态
    @param row 含 schema_path/schema_inline 的表
    @param location 不含 schema 键的节定位
    @param optional 是否允许两键均缺失
    @return 可用 Schema 或 None
    """
    path = row.get("schema_path")
    inline = row.get("schema_inline")
    if optional and path is None and inline is None:
        return None
    request = _GenerationSchemaRequest(
        file=str(state.context.project_root / "project.toml"),
        section=location.strip("[]"),
        path=path,
        inline=inline,
        project_root=state.context.project_root,
        max_bytes=state.limits.state_or_outcome_schema_bytes,
    )
    return _load_generation_schema(state.context.collector, request)


def _pointer_parts(state: _ParseState, value: str, location: str,
                   allow_root: bool = True) -> tuple[str, ...] | None:
    """用 jsonpointer 解析 RFC 6901 path。

    @param state 当前解析状态
    @param value 待解析 path
    @param location 键定位
    @param allow_root 是否允许空 root pointer
    @return 解码 token 元组或 None
    """
    if not isinstance(value, str) or (not allow_root and value == ""):
        _error(state, location, f"expected non-root RFC 6901 pointer, got {_fmt(value)}")
        return None
    try:
        return tuple(JsonPointer(value).parts)
    except JsonPointerException:
        _error(state, location, f"expected RFC 6901 pointer, got {_fmt(value)}")
        return None


def _check_root_set(state: _ParseState, values: tuple[str, ...], location: str) -> None:
    """拒绝一个 roots 列表内的重复与祖先/后代项。

    @param state 当前解析状态
    @param values path 声明序元组
    @param location 键定位
    """
    parsed = [(value, _pointer_parts(state, value, location)) for value in values]
    valid = [(value, parts) for value, parts in parsed if parts is not None]
    for index, (left, left_parts) in enumerate(valid):
        for right, right_parts in valid[index + 1:]:
            if _is_prefix(left_parts, right_parts) or _is_prefix(right_parts, left_parts):
                _error(state, location, f"redundant pointer roots {left!r} and {right!r}")


def _is_prefix(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """判断解码 token 是否为祖先或自身。

    @param left 候选前缀 token
    @param right 完整 token
    @return left 是 right 前缀时为 true
    """
    return len(left) <= len(right) and left == right[:len(left)]


def _covered(path: tuple[str, ...] | None, roots: tuple[str, ...]) -> bool:
    """判断一个 pointer 是否被任一声明 root 覆盖。

    @param path 已解析 pointer token
    @param roots 原始 root pointer 表
    @return path 合法且至少命中一个 root 时为 true
    """
    if path is None:
        return False
    for root in roots:
        try:
            if _is_prefix(tuple(JsonPointer(root).parts), path):
                return True
        except JsonPointerException:
            continue
    return False


def _parse_binding(state: _ParseState, role: Mapping[str, object], location: str,
                   index: int) -> PayloadBindingSpec | None:
    """解析一个 payload binding 并校验权限。

    @param state 当前解析状态
    @param role 当前 role 原始表
    @param location role 定位
    @param index binding 一基序号
    @return 合法载体或 None
    """
    rows = role.get("payload_bindings")
    if not isinstance(rows, list) or index >= len(rows) or not isinstance(rows[index], Mapping):
        return None
    row = rows[index]
    base = f"{location}.payload_bindings[{index + 1}]"
    _check_keys(state, row, frozenset({"payload_path", "state_phase", "state_path"}), base)
    payload = _string(state, row, "payload_path", f"{base}.payload_path")
    phase = row.get("state_phase")
    if phase not in ("before", "after"):
        _error(state, f"{base}.state_phase", f"expected before | after, got {_fmt(phase)}")
        phase = "before"
    state_path = _string(state, row, "state_path", f"{base}.state_path")
    payload_parts = _pointer_parts(state, payload, f"{base}.payload_path", allow_root=False)
    state_parts = _pointer_parts(state, state_path, f"{base}.state_path")
    read_roots = tuple(item for item in role.get("read_roots", []) if isinstance(item, str))
    publish_roots = tuple(item for item in role.get("publish_roots", []) if isinstance(item, str))
    if not _covered(state_parts, read_roots) or not _covered(state_parts, publish_roots):
        _error(state, f"{base}.state_path", "must be covered by read_roots and publish_roots")
    if payload_parts is None or state_parts is None:
        return None
    return PayloadBindingSpec(payload, phase, state_path)


def _parse_bindings(state: _ParseState, role: Mapping[str, object],
                    location: str) -> tuple[PayloadBindingSpec, ...]:
    """解析并检查一条 role 的全部 binding path 冲突。

    @param state 当前解析状态
    @param role 当前 role 原始表
    @param location role 定位
    @return 合法 binding 元组
    """
    raw = role.get("payload_bindings", [])
    if not isinstance(raw, list):
        _error(state, f"{location}.payload_bindings", f"expected array, got {_fmt(raw)}")
        return ()
    bindings = tuple(filter(None, (
        _parse_binding(state, role, location, index) for index in range(len(raw))
    )))
    parsed = [(item, tuple(JsonPointer(item.payload_path).parts)) for item in bindings]
    for index, (left, left_parts) in enumerate(parsed):
        for right, right_parts in parsed[index + 1:]:
            if _is_prefix(left_parts, right_parts) or _is_prefix(right_parts, left_parts):
                _error(state, f"{location}.payload_bindings",
                       f"conflicting payload paths {left.payload_path!r} and {right.payload_path!r}")
    return bindings


def _parse_role(state: _ParseState, row: Mapping[str, object],
                location: str) -> RoleSpec:
    """解析并冻结一个 declared role。

    @param state 当前解析状态
    @param row role 原始表
    @param location role 定位
    @return 冻结 role 配置
    """
    _check_keys(state, row, frozenset({
        "name", "frame_class", "actor", "read_roots", "write_roots", "publish_roots",
        "observers", "state_instruction", "pre_state_schema_path", "payload_bindings",
        "calendar_window",
    }), location)
    name = _name(state, row.get("name"), f"{location}.name")
    frame = _name(state, row.get("frame_class"), f"{location}.frame_class")
    actor = _name(state, row.get("actor"), f"{location}.actor")
    roots = tuple(_string_tuple(state, row.get(key), f"{location}.{key}")
                  for key in ("read_roots", "write_roots", "publish_roots"))
    for key, values in zip(("read_roots", "write_roots", "publish_roots"), roots):
        _check_root_set(state, values, f"{location}.{key}")
    observers = _string_tuple(state, row.get("observers"), f"{location}.observers")
    if _duplicates(observers) or any(_NAME_RE.fullmatch(item) is None for item in observers):
        _error(state, f"{location}.observers", "expected unique [a-z0-9_]+ actor names")
    instruction = _string(state, row, "state_instruction", f"{location}.state_instruction")
    schema_row = {"schema_path": row.get("pre_state_schema_path")}
    pre_schema = _load_schema(state, schema_row, f"{location}.pre_state", optional=True)
    window = row.get("calendar_window")
    if window is not None and (not isinstance(window, str) or not window):
        _error(state, f"{location}.calendar_window", f"expected non-empty string, got {_fmt(window)}")
        window = None
    return RoleSpec(name, frame, actor, roots[0], roots[1], roots[2], observers,
                    instruction, pre_schema, _parse_bindings(state, row, location), window)


def _parse_gap(state: _ParseState, row: Mapping[str, object],
               location: str) -> GapSpec:
    """解析并冻结一个 declared gap。

    @param state 当前解析状态
    @param row gap 原始表
    @param location gap 定位
    @return 冻结 gap 配置
    """
    _check_keys(state, row, frozenset({
        "name", "before", "after", "min_gap_s", "max_gap_s",
    }), location)
    name = _name(state, row.get("name"), f"{location}.name")
    before = _name(state, row.get("before"), f"{location}.before")
    after = _name(state, row.get("after"), f"{location}.after")
    minimum = _seconds_us(state, row.get("min_gap_s", 0), f"{location}.min_gap_s", False)
    maximum = _seconds_us(state, row.get("max_gap_s"), f"{location}.max_gap_s", False)
    if minimum > maximum:
        _error(state, location, "min_gap_s must be <= max_gap_s")
    return GapSpec(name, before, after, minimum, maximum)


def _parse_pattern(state: _ParseState, name: str,
                   row: Mapping[str, object]) -> SequencePattern:
    """解析并交叉校验一个 declared pattern。

    @param state 当前解析状态
    @param name pattern 名
    @param row pattern 原始表
    @return 冻结 pattern 配置
    """
    base = f"[generate.pattern.{name}]"
    _check_keys(state, row, frozenset({
        "sequence_class", "description", "roles", "order", "gaps", "max_span_s",
    }), base)
    sequence_class = _name(state, row.get("sequence_class"), f"{base}.sequence_class")
    description = _string(state, row, "description", f"{base}.description")
    roles_raw = _rows(state, row, "roles", f"{base}.roles")
    roles = tuple(_parse_role(state, item, f"{base}.roles[{index}]")
                  for index, item in enumerate(roles_raw, 1))
    order = _string_tuple(state, row.get("order"), f"{base}.order")
    gaps_raw = _rows(state, row, "gaps", f"{base}.gaps")
    gaps = tuple(_parse_gap(state, item, f"{base}.gaps[{index}]")
                 for index, item in enumerate(gaps_raw, 1))
    span = _seconds_us(state, row.get("max_span_s"), f"{base}.max_span_s", True)
    pattern = SequencePattern(name, sequence_class, description, roles, order, gaps, span)
    _check_pattern(state, pattern, base)
    return pattern


def _duplicates(values: tuple[str, ...]) -> tuple[str, ...]:
    """返回声明序中的重复值去重表。

    @param values 待检查值
    @return 首次成为重复项的值元组
    """
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return tuple(duplicates)


def _check_pattern(state: _ParseState, pattern: SequencePattern, location: str) -> None:
    """校验 pattern 的排列、gap 闭包与引用。

    @param state 当前解析状态
    @param pattern 待检查 pattern
    @param location pattern 定位
    """
    role_names = tuple(role.name for role in pattern.roles)
    if not 1 <= len(role_names) <= state.limits.pattern_roles:
        _error(state, f"{location}.roles", f"expected 1..{state.limits.pattern_roles} roles")
    if _duplicates(role_names):
        _error(state, f"{location}.roles", f"duplicate role names: {_duplicates(role_names)!r}")
    if len(pattern.order) != len(role_names) or set(pattern.order) != set(role_names):
        _error(state, f"{location}.order", "must be an exact permutation of all role names")
    positions = {name: index for index, name in enumerate(pattern.order)}
    gap_names = tuple(gap.name for gap in pattern.gaps)
    if _duplicates(gap_names):
        _error(state, f"{location}.gaps", f"duplicate gap names: {_duplicates(gap_names)!r}")
    pairs = tuple((gap.before, gap.after) for gap in pattern.gaps)
    if _duplicates(tuple(f"{left}\0{right}" for left, right in pairs)):
        _error(state, f"{location}.gaps", "duplicate before/after pair")
    for gap in pattern.gaps:
        if gap.before not in positions or gap.after not in positions:
            _error(state, f"{location}.gaps", "gap roles must exist in order")
        elif positions[gap.before] >= positions[gap.after]:
            _error(state, f"{location}.gaps", "gap must point forward in order")
    adjacent = set(zip(pattern.order, pattern.order[1:]))
    if not adjacent.issubset(set(pairs)):
        _error(state, f"{location}.gaps", "every adjacent role pair requires exactly one gap")


def _parse_patterns(state: _ParseState) -> tuple[SequencePattern, ...]:
    """按 TOML 声明序解析全部 pattern。

    @param state 当前解析状态
    @return 冻结 pattern 元组
    """
    raw = _table(state, state.generate, "pattern", "[generate.pattern]")
    patterns: list[SequencePattern] = []
    for raw_name, value in raw.items():
        name = _name(state, raw_name, "[generate.pattern].name")
        if not isinstance(value, Mapping):
            _error(state, f"[generate.pattern.{name}]", f"expected table, got {_fmt(value)}")
            continue
        patterns.append(_parse_pattern(state, name, value))
    return tuple(patterns)


def _variant_target(state: _ParseState, row: Mapping[str, object], kind: str,
                    location: str) -> tuple[dict[str, str | int], dict[str, str]]:
    """把 kind 专用键冻结为 target 与 expected violation。

    @param state 当前解析状态
    @param row variant 原始表
    @param kind 变体种类
    @param location variant 定位
    @return 规范化 target 与 expected violation
    """
    if kind == "positive":
        return {}, {}
    if kind == "missing":
        role = _name(state, row.get("target_role"), f"{location}.target_role")
        return {"role": role}, {"kind": "missing_role", "target": role}
    if kind == "reordered":
        before = _name(state, row.get("target_before"), f"{location}.target_before")
        after = _name(state, row.get("target_after"), f"{location}.target_after")
        return ({"before": before, "after": after},
                {"kind": "reordered", "before": before, "after": after})
    gap = _name(state, row.get("target_gap"), f"{location}.target_gap")
    minimum = _seconds_us(state, row.get("min_excess_s"), f"{location}.min_excess_s", True)
    maximum = _seconds_us(state, row.get("max_excess_s"), f"{location}.max_excess_s", True)
    if minimum > maximum:
        _error(state, location, "min_excess_s must be <= max_excess_s")
    return ({"gap": gap, "min_excess_us": minimum, "max_excess_us": maximum},
            {"kind": "gap_above_max", "target": gap})


def _parse_variant(state: _ParseState, row: Mapping[str, object],
                   location: str, pattern: SequencePattern | None) -> VariantSpec:
    """解析并冻结一个 counterfactual variant。

    @param state 当前解析状态
    @param row variant 原始表
    @param location variant 定位
    @param pattern 已解析 pattern
    @return 冻结 variant 配置
    """
    _check_keys(state, row, frozenset({
        "name", "kind", "target_role", "target_before", "target_after", "target_gap",
        "min_excess_s", "max_excess_s", "outcome_schema_path", "outcome_schema_inline",
    }), location)
    name = _name(state, row.get("name"), f"{location}.name")
    kind = row.get("kind")
    if kind not in ("positive", "missing", "reordered", "interval_exceeded"):
        _error(state, f"{location}.kind", f"expected supported variant kind, got {_fmt(kind)}")
        kind = "positive"
    _check_variant_shape(state, row, kind, location)
    target, violation = _variant_target(state, row, kind, location)
    if "outcome_schema_inline" in row:
        _error(state, f"{location}.outcome_schema_inline",
               "generation_config_invalid: only outcome_schema_path is supported")
    schema_row = {"schema_path": row.get("outcome_schema_path")}
    outcome = _load_schema(state, schema_row, f"{location}.outcome") or _OBJECT_SCHEMA
    divergence = _variant_divergence(state, pattern, kind, target, location)
    return VariantSpec(name, kind, target, outcome, violation, divergence)


def _check_variant_shape(state: _ParseState, row: Mapping[str, object],
                         kind: str, location: str) -> None:
    """校验每种 variant 的目标键恰好属于该种类。

    @param state 当前解析状态
    @param row variant 原始表
    @param kind 已规整 kind
    @param location variant 定位
    """
    target_keys = {"target_role", "target_before", "target_after", "target_gap",
                   "min_excess_s", "max_excess_s"}
    required = {
        "positive": set(),
        "missing": {"target_role"},
        "reordered": {"target_before", "target_after"},
        "interval_exceeded": {"target_gap", "min_excess_s", "max_excess_s"},
    }[kind]
    missing = required - set(row)
    extra = (set(row) & target_keys) - required
    if missing:
        _error(state, location, f"missing required variant target keys: {sorted(missing)!r}")
    if extra:
        _error(state, location, f"forbidden variant target keys: {sorted(extra)!r}")


def _variant_divergence(state: _ParseState, pattern: SequencePattern | None,
                        kind: str, target: Mapping[str, str | int], location: str) -> str | None:
    """校验目标并求首个因果分叉 role。

    @param state 当前解析状态
    @param pattern 所属 pattern
    @param kind 变体种类
    @param target 规范化目标
    @param location variant 定位
    @return 分叉 role；positive 为 None
    """
    if kind == "positive" or pattern is None:
        return None
    positions = {name: index for index, name in enumerate(pattern.order)}
    if kind == "missing":
        role = str(target.get("role", ""))
        if role not in positions:
            _error(state, location, f"target role {role!r} does not exist")
        return role
    if kind == "reordered":
        before, after = str(target.get("before", "")), str(target.get("after", ""))
        if positions.get(after) != positions.get(before, -2) + 1:
            _error(state, location, "reordered targets must be adjacent in pattern order")
        return before
    gap_name = str(target.get("gap", ""))
    gap = next((item for item in pattern.gaps if item.name == gap_name), None)
    if gap is None:
        _error(state, location, f"target gap {gap_name!r} does not exist")
        return None
    return gap.after


def _parse_set(state: _ParseState, row: Mapping[str, object], index: int,
               patterns: Mapping[str, SequencePattern]) -> CounterfactualSetSpec:
    """解析并冻结一个 counterfactual set。

    @param state 当前解析状态
    @param row set 原始表
    @param index 一基声明序号
    @param patterns pattern 名表
    @return 冻结 set 配置
    """
    location = f"[[generate.counterfactual_sets]][{index}]"
    _check_keys(state, row, frozenset({"name", "pattern", "count", "variants"}), location)
    name = _name(state, row.get("name"), f"{location}.name")
    pattern_name = _name(state, row.get("pattern"), f"{location}.pattern")
    count = _integer(state, row, "count", f"{location}.count", 1)
    variants_raw = _rows(state, row, "variants", f"{location}.variants")
    variants = tuple(_parse_variant(state, item, f"{location}.variants[{item_index}]",
                                    patterns.get(pattern_name))
                     for item_index, item in enumerate(variants_raw, 1))
    if not 1 <= len(variants) <= state.limits.variants_per_counterfactual_set:
        _error(state, f"{location}.variants", "expected 1..8 variants")
    names = tuple(item.name for item in variants)
    if _duplicates(names):
        _error(state, f"{location}.variants", f"duplicate variant names: {_duplicates(names)!r}")
    signatures = tuple(json.dumps(dict(item.expected_violation), sort_keys=True) for item in variants)
    if _duplicates(signatures):
        _error(state, f"{location}.variants", "expected violation signatures must be unique")
    return CounterfactualSetSpec(name, pattern_name, count, variants)


def _parse_sets(state: _ParseState,
                patterns: tuple[SequencePattern, ...]) -> tuple[CounterfactualSetSpec, ...]:
    """按声明序解析全部 counterfactual set。

    @param state 当前解析状态
    @param patterns 已解析 pattern 表
    @return 冻结 set 元组
    """
    rows = _rows(state, state.generate, "counterfactual_sets",
                 "[[generate.counterfactual_sets]]")
    pattern_map = {item.name: item for item in patterns}
    sets = tuple(_parse_set(state, row, index, pattern_map)
                 for index, row in enumerate(rows, 1))
    names = tuple(item.name for item in sets)
    if _duplicates(names):
        _error(state, "[[generate.counterfactual_sets]]", f"duplicate names: {_duplicates(names)!r}")
    return sets


def _len_range(state: _ParseState, value: object, location: str) -> tuple[int, int]:
    """读取 1..8 的 instruction-only 长度闭区间。

    @param state 当前解析状态
    @param value 原始值
    @param location 键定位
    @return 合法区间或 (1,1)
    """
    valid = (isinstance(value, list) and len(value) == 2
             and all(isinstance(item, int) and not isinstance(item, bool) for item in value))
    if not valid:
        _error(state, location, f"expected integer range [1,8], got {_fmt(value)}")
        return 1, 1
    low, high = int(value[0]), int(value[1])
    if not 1 <= low <= high <= state.limits.instruction_only_events:
        _error(state, location, f"expected 1 <= low <= high <= 8, got {_fmt(value)}")
        return 1, 1
    return low, high


def _parse_instruction_only(state: _ParseState) -> tuple[InstructionOnlySpec, ...]:
    """解析全部 instruction-only 精确交付行。

    @param state 当前解析状态
    @return 冻结声明元组
    """
    rows = _rows(state, state.generate, "instruction_only", "[[generate.instruction_only]]")
    out: list[InstructionOnlySpec] = []
    for index, row in enumerate(rows, 1):
        location = f"[[generate.instruction_only]][{index}]"
        _check_keys(state, row, frozenset({
            "name", "sequence_class", "count", "len_range", "instruction",
            "state_schema_path", "state_schema_inline",
        }), location)
        name = _name(state, row.get("name"), f"{location}.name")
        sequence_class = _name(state, row.get("sequence_class"), f"{location}.sequence_class")
        count = _integer(state, row, "count", f"{location}.count", 1)
        lengths = _len_range(state, row.get("len_range"), f"{location}.len_range")
        instruction = _string(state, row, "instruction", f"{location}.instruction")
        if "state_schema_inline" in row:
            _error(state, f"{location}.state_schema_inline",
                   "generation_config_invalid: only state_schema_path is supported")
        schema_row = {"schema_path": row.get("state_schema_path")}
        schema = _load_schema(state, schema_row, f"{location}.state", optional=True)
        out.append(InstructionOnlySpec(name, sequence_class, count, lengths,
                                       instruction, schema or _OBJECT_SCHEMA))
    names = tuple(item.name for item in out)
    if _duplicates(names):
        _error(state, "[[generate.instruction_only]]", f"duplicate names: {_duplicates(names)!r}")
    return tuple(out)


def _timestamp(state: _ParseState, value: object,
               location: str) -> tuple[int, int]:
    """解析带固定 offset 的 ISO-8601 时间。

    @param state 当前解析状态
    @param value 原始值
    @param location 键定位
    @return epoch 微秒与 offset 分钟
    """
    if not isinstance(value, str):
        _error(state, location, f"expected ISO-8601 string with offset, got {_fmt(value)}")
        return 0, 0
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _error(state, location, f"expected ISO-8601 string with offset, got {_fmt(value)}")
        return 0, 0
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None:
        _error(state, location, "timestamp must include a fixed UTC offset")
        return 0, 0
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        delta = parsed.astimezone(timezone.utc) - epoch
    except (OverflowError, ValueError):
        _error(state, location, "timestamp is outside the supported UTC range")
        return 0, 0
    micros = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    return micros, int(offset.total_seconds() // 60)


def _pair_us(state: _ParseState, value: object, location: str) -> tuple[int, int]:
    """解析非负秒数闭区间。

    @param state 当前解析状态
    @param value 原始二元素数组
    @param location 键定位
    @return 整数微秒闭区间
    """
    if not isinstance(value, list) or len(value) != 2:
        _error(state, location, f"expected two-number array, got {_fmt(value)}")
        return 0, 0
    low = _seconds_us(state, value[0], f"{location}[1]", False)
    high = _seconds_us(state, value[1], f"{location}[2]", False)
    if low > high:
        _error(state, location, "range lower bound must be <= upper bound")
    return low, high


def _parse_timeline(state: _ParseState) -> TimelineSpec:
    """解析完整 timeline 表。

    @param state 当前解析状态
    @return 冻结时间线载体
    """
    row = _table(state, state.generate, "timeline", "[generate.timeline]")
    _check_keys(state, row, frozenset({
        "timestamp_start", "event_gap_s", "primary_sessions", "crossed_primary_sessions",
        "session_max_events", "session_max_span_s", "session_gap_s", "noise_events",
        "duplicate_sequences",
    }), "[generate.timeline]")
    timestamp, offset = _timestamp(state, row.get("timestamp_start"),
                                   "[generate.timeline].timestamp_start")
    gap = _pair_us(state, row.get("event_gap_s"), "[generate.timeline].event_gap_s")
    primary = _integer(state, row, "primary_sessions", "[generate.timeline].primary_sessions", 1)
    crossed = _integer(state, row, "crossed_primary_sessions",
                       "[generate.timeline].crossed_primary_sessions", 0)
    max_events = _integer(state, row, "session_max_events",
                          "[generate.timeline].session_max_events", 1)
    max_span = _seconds_us(state, row.get("session_max_span_s"),
                           "[generate.timeline].session_max_span_s", True)
    session_gap = _seconds_us(state, row.get("session_gap_s"),
                              "[generate.timeline].session_gap_s", True)
    noise = _integer(state, row, "noise_events", "[generate.timeline].noise_events", 0)
    duplicate = _integer(state, row, "duplicate_sequences",
                         "[generate.timeline].duplicate_sequences", 0)
    return TimelineSpec(timestamp, offset, gap, primary, crossed, max_events,
                        max_span, session_gap, noise, duplicate)


def _offset_minutes(state: _ParseState, value: object, location: str) -> int:
    """解析 ``+HH:MM`` 或 ``-HH:MM`` 固定 offset。

    @param state 当前解析状态
    @param value 原始值
    @param location 键定位
    @return 有符号分钟数
    """
    if not isinstance(value, str) or re.fullmatch(r"[+-](?:0\d|1\d|2[0-3]):[0-5]\d", value) is None:
        _error(state, location, f"expected fixed UTC offset +HH:MM or -HH:MM, got {_fmt(value)}")
        return 0
    sign = 1 if value[0] == "+" else -1
    return sign * (int(value[1:3]) * 60 + int(value[4:6]))


def _clock_us(state: _ParseState, value: object, location: str) -> int:
    """解析同日 ``HH:MM:SS`` 墙钟时间。

    @param state 当前解析状态
    @param value 原始值
    @param location 键定位
    @return 当日整数微秒
    """
    if not isinstance(value, str) or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d", value) is None:
        _error(state, location, f"expected HH:MM:SS, got {_fmt(value)}")
        return 0
    hour, minute, second = (int(part) for part in value.split(":"))
    return ((hour * 60 + minute) * 60 + second) * 1_000_000


def _intervals(state: _ParseState, value: object,
               location: str) -> tuple[tuple[int, int], ...]:
    """解析非空、同日、不重叠的半开墙钟区间。

    @param state 当前解析状态
    @param value 原始区间数组
    @param location 键定位
    @return 声明序整数微秒区间
    """
    if not isinstance(value, list) or not value:
        _error(state, location, f"expected non-empty interval array, got {_fmt(value)}")
        return ()
    out: list[tuple[int, int]] = []
    for index, pair in enumerate(value, 1):
        if not isinstance(pair, list) or len(pair) != 2:
            _error(state, f"{location}[{index}]", f"expected [start,end], got {_fmt(pair)}")
            continue
        start = _clock_us(state, pair[0], f"{location}[{index}][1]")
        end = _clock_us(state, pair[1], f"{location}[{index}][2]")
        if start >= end:
            _error(state, f"{location}[{index}]", "interval must be non-empty and same-day")
        out.append((start, end))
    ordered = sorted(out)
    if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
        _error(state, location, "calendar intervals must not overlap")
    return tuple(out)


def _parse_windows(state: _ParseState) -> Mapping[str, CalendarWindowSpec]:
    """解析全部命名 calendar window。

    @param state 当前解析状态
    @return 只读窗口映射
    """
    raw = _table(state, state.generate, "calendar_window", "[generate.calendar_window]")
    out: dict[str, CalendarWindowSpec] = {}
    for raw_name, value in raw.items():
        name = _name(state, raw_name, "[generate.calendar_window].name")
        location = f"[generate.calendar_window.{name}]"
        if not isinstance(value, Mapping):
            _error(state, location, f"expected table, got {_fmt(value)}")
            continue
        _check_keys(state, value, frozenset({"utc_offset", "days", "intervals"}), location)
        offset = _offset_minutes(state, value.get("utc_offset"), f"{location}.utc_offset")
        days = _string_tuple(state, value.get("days"), f"{location}.days")
        if not days or any(day not in _DAYS for day in days) or _duplicates(days):
            _error(state, f"{location}.days", "expected non-empty unique weekday names")
        intervals = _intervals(state, value.get("intervals"), f"{location}.intervals")
        out[name] = CalendarWindowSpec(name, offset, days, intervals)  # type: ignore[arg-type]
    return MappingProxyType(out)


def _parse_noise(state: _ParseState) -> NoiseSpec | None:
    """解析可选 noise 表。

    @param state 当前解析状态
    @return 冻结 noise 配置或 None
    """
    raw = state.generate.get("noise")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        _error(state, "[generate.noise]", f"expected table, got {_fmt(raw)}")
        return None
    _check_keys(
        state, raw, frozenset({"frame_class", "instruction", "topics"}),
        "[generate.noise]",
    )
    frame_class = _name(state, raw.get("frame_class"), "[generate.noise].frame_class")
    instruction = _string(state, raw, "instruction", "[generate.noise].instruction")
    topics = _string_tuple(state, raw.get("topics"), "[generate.noise].topics")
    if any(not item.strip() for item in topics) or _duplicates(topics):
        _error(state, "[generate.noise].topics", "expected non-empty unique topic strings")
    return NoiseSpec(frame_class, instruction, topics)


def _parse_state_hook(state: _ParseState) -> ResolvedHook | None:
    """解析并以双独立深拷贝探针校验 state hook。

    @param state 当前解析状态
    @return 可用冻结 hook 或 None
    """
    value = state.generate.get("state_validator")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        _error(state, "[generate].state_validator", f"expected hook string, got {_fmt(value)}")
        return None
    try:
        hook = state.context.hook_loader(value, state.context.project_root)
    except ValueError as exc:
        _error(state, "[generate].state_validator", str(exc))
        return None
    arity = check_hook_arity(hook, 1)
    if arity is not None:
        _error(state, "[generate].state_validator", arity)
        return None
    return hook if _probe_state_hook(state, hook) else None


def _probe_state_hook(state: _ParseState, hook: ResolvedHook) -> bool:
    """要求两次少数分支探针产生逐字节相同的严格字符串列表。

    @param state 当前解析状态
    @param hook 已解析 hook
    @return 探针合法且确定时为 true
    """
    probe = state_probe_input()
    outputs: list[tuple[str, ...]] = []
    for _ in range(2):
        try:
            result = hook.target(clone_state_input(probe))
        except Exception as exc:
            _error(state, "[generate].state_validator",
                   f"synthetic dry-run raised {type(exc).__name__}")
            return False
        try:
            outputs.append(normalize_state_violations(result, hook.reference))
        except TypeError:
            _error(state, "[generate].state_validator",
                   "synthetic dry-run returned an invalid value")
            return False
    if outputs[0] != outputs[1]:
        _error(state, "[generate].state_validator", "synthetic dry-run is nondeterministic")
        return False
    return True


def _check_mode(state: _ParseState, mode: str, patterns: tuple[SequencePattern, ...],
                sets: tuple[CounterfactualSetSpec, ...], instructions: tuple[InstructionOnlySpec, ...]) -> None:
    """校验 declared 与 instruction-only 表面互斥。

    @param state 当前解析状态
    @param mode 当前 sequence 模式
    @param patterns pattern 表
    @param sets counterfactual set 表
    @param instructions instruction-only 表
    """
    if mode == "declared":
        if not patterns or not sets:
            _error(state, "[generate]", "declared mode requires pattern and counterfactual_sets")
        if instructions:
            _error(state, "[generate].instruction_only", "forbidden in declared mode")
    else:
        if not instructions:
            _error(state, "[generate]", "instruction_only mode requires instruction_only rows")
        if patterns or sets:
            _error(state, "[generate]", "patterns and counterfactual_sets are forbidden in instruction_only mode")


def _check_references(state: _ParseState, patterns: tuple[SequencePattern, ...],
                      instructions: tuple[InstructionOnlySpec, ...], windows: Mapping[str, CalendarWindowSpec]) -> None:
    """校验 sequence class、frame class 与 calendar 引用。

    @param state 当前解析状态
    @param patterns pattern 表
    @param instructions instruction-only 表
    @param windows 日历窗口表
    """
    classes = state.context.class_views
    frames = state.context.frame_classes
    for pattern in patterns:
        if pattern.sequence_class not in classes:
            _error(state, f"[generate.pattern.{pattern.name}].sequence_class", "unknown sequence class")
        for role in pattern.roles:
            if role.frame_class not in frames:
                _error(state, f"[generate.pattern.{pattern.name}].roles", "unknown frame class")
            if role.calendar_window is not None and role.calendar_window not in windows:
                _error(state, f"[generate.pattern.{pattern.name}].roles", "unknown calendar window")
    for item in instructions:
        if item.sequence_class not in classes:
            _error(state, "[[generate.instruction_only]].sequence_class", "unknown sequence class")
        else:
            generation = classes[item.sequence_class].sequence_generation
            if generation is not None and generation.initial_state_source == "catalog":
                _error(state, f"[class.{item.sequence_class}.generate]",
                       "catalog source is forbidden in instruction_only mode")


def declared_actor_names(pattern: SequencePattern) -> tuple[str, ...]:
    """按首次声明序取一个 pattern 的 actor 闭集。

    @param pattern 当前 pattern
    @return actor 与 observer 并集
    """
    names: list[str] = []
    for role in pattern.roles:
        for name in (role.actor, *role.observers):
            if name not in names:
                names.append(name)
    return tuple(names)


def _check_class_registry(state: _ParseState, patterns: tuple[SequencePattern, ...],
                          sets: tuple[CounterfactualSetSpec, ...]) -> None:
    """校验 declared class 世界配置、actor 闭集与 catalog 外壳容量。

    @param state 当前解析状态
    @param patterns pattern 表
    @param sets counterfactual set 表
    """
    for cname, view in state.context.class_views.items():
        if not view.description.strip():
            _error(state, f"[class.{cname}].description", "expected non-empty string")
    grouped: dict[str, list[SequencePattern]] = {}
    for pattern in patterns:
        grouped.setdefault(pattern.sequence_class, []).append(pattern)
    for cname, class_patterns in grouped.items():
        view = state.context.class_views.get(cname)
        if view is None:
            continue
        generation = view.sequence_generation
        if generation is None:
            _error(state, f"[class.{cname}.generate]", "declared class generation config is required")
            continue
        actor_sets = tuple(frozenset(declared_actor_names(item)) for item in class_patterns)
        if any(value != actor_sets[0] for value in actor_sets[1:]):
            _error(state, f"[class.{cname}]", "all patterns must declare the same actor set")
        if generation.initial_state_source == "catalog":
            _check_catalog_capacity(state, cname, class_patterns, sets)


def _check_catalog_capacity(state: _ParseState, cname: str,
                            patterns: list[SequencePattern],
                            sets: tuple[CounterfactualSetSpec, ...]) -> None:
    """校验 catalog actor 闭集和按 class slot 数的无放回容量。

    @param state 当前解析状态
    @param cname sequence class 名
    @param patterns 该 class 的 pattern
    @param sets counterfactual set 表
    """
    view = state.context.class_views[cname]
    generation = view.sequence_generation
    if generation is None:
        return
    actor_names = frozenset(declared_actor_names(patterns[0]))
    for index, row in enumerate(generation.initial_state_catalog, 1):
        actors = row.get("actors")
        if not isinstance(actors, Mapping) or frozenset(actors) != actor_names:
            _error(state, f"[class.{cname}.generate].initial_state_catalog_path[{index}]",
                   "catalog actors must exactly match the class actor set")
    pattern_names = {item.name for item in patterns}
    required = sum(item.count for item in sets if item.pattern in pattern_names)
    if len(generation.initial_state_catalog) < required:
        _error(state, f"[class.{cname}.generate].initial_state_catalog_path",
               f"catalog has {len(generation.initial_state_catalog)} rows but {required} slots require rows")


def _check_frame_registry(state: _ParseState, patterns: tuple[SequencePattern, ...],
                          config: SequenceGenerationConfig) -> None:
    """校验 role/noise/instruction-only 可用 frame class 契约。

    @param state 当前解析状态
    @param patterns pattern 表
    @param config sequence 配置
    """
    for name, view in state.context.frame_classes.items():
        if not view.description.strip():
            _error(state, f"[frame.class.{name}].description", "expected non-empty string")
    role_frames = {role.frame_class for pattern in patterns for role in pattern.roles}
    required = set(role_frames)
    if config.noise is not None:
        required.add(config.noise.frame_class)
        if config.noise.frame_class not in state.context.frame_classes:
            _error(state, "[generate.noise].frame_class", "unknown noise frame class")
        if config.noise.frame_class in role_frames:
            _error(state, "[generate.noise].frame_class", "noise frame class cannot be used by a role")
    for name in required:
        view = state.context.frame_classes.get(name)
        if view is None:
            continue
        if not is_generation_frame_eligible(view):
            _error(state, f"[frame.class.{name}]",
                   "referenced frame class requires description, instruction and object schema")
    if config.mode == "instruction_only":
        eligible = [view for name, view in state.context.frame_classes.items()
                    if name not in required or config.noise is None or name != config.noise.frame_class]
        if not any(is_generation_frame_eligible(view) for view in eligible):
            _error(state, "[frame.class]", "instruction_only requires an eligible frame class")


def _check_variant_targets(state: _ParseState, patterns: tuple[SequencePattern, ...],
                           sets: tuple[CounterfactualSetSpec, ...]) -> None:
    """校验反事实目标的 frame 唯一性与相邻交换语义。

    @param state 当前解析状态
    @param patterns pattern 表
    @param sets counterfactual set 表
    """
    pattern_map = {item.name: item for item in patterns}
    for group in sets:
        pattern = pattern_map.get(group.pattern)
        if pattern is None:
            _error(state, f"[[generate.counterfactual_sets]].pattern", "unknown pattern")
            continue
        roles = {role.name: role for role in pattern.roles}
        for variant in group.variants:
            if variant.kind == "missing":
                target = roles.get(str(variant.target.get("role", "")))
                count = sum(role.frame_class == target.frame_class for role in roles.values()) if target else 0
                if count != 1:
                    _error(state, f"[[generate.counterfactual_sets]].{variant.name}",
                           "missing target frame class must be unique in its pattern")
                if len(roles) == 1 and target is not None:
                    _error(state, f"[[generate.counterfactual_sets]].{variant.name}",
                           "missing variant must retain at least one role")
            if variant.kind == "reordered":
                before = roles.get(str(variant.target.get("before", "")))
                after = roles.get(str(variant.target.get("after", "")))
                if before is not None and after is not None and before.frame_class == after.frame_class:
                    _error(state, f"[[generate.counterfactual_sets]].{variant.name}",
                           "reordered target roles must have different frame classes")


def _declared_counts(patterns: tuple[SequencePattern, ...],
                     sets: tuple[CounterfactualSetSpec, ...], duplicates: int) -> tuple[int, int, int]:
    """计算 declared primary sequence/event 与 replay event 数。

    @param patterns pattern 表
    @param sets counterfactual set 表
    @param duplicates replay sequence 数
    @return primary sequences、primary events、replay events
    """
    pattern_map = {item.name: item for item in patterns}
    sequences = 0
    events = 0
    positive_lengths: list[int] = []
    for group in sets:
        pattern = pattern_map.get(group.pattern)
        if pattern is None:
            continue
        sequences += group.count * len(group.variants)
        for variant in group.variants:
            length = len(pattern.roles) - (1 if variant.kind == "missing" else 0)
            events += group.count * length
            if variant.kind == "positive":
                positive_lengths.extend([length] * group.count)
    replay = sum(positive_lengths[:duplicates])
    return sequences, events, replay


def _check_timeline_counts(state: _ParseState, config: SequenceGenerationConfig) -> None:
    """校验 timeline 精确基数、noise/replay 与静态容量上限。

    @param state 当前解析状态
    @param config 完整 sequence 配置
    """
    timeline = config.timeline
    if config.mode == "declared":
        sequences, events, replay = _declared_counts(
            config.patterns, config.counterfactual_sets, timeline.duplicate_sequences)
        positive = sum(group.count for group in config.counterfactual_sets
                       if any(item.kind == "positive" for item in group.variants))
        if timeline.duplicate_sequences > positive:
            _error(state, "[generate.timeline].duplicate_sequences",
                   "not enough positive primary sources for replay")
    else:
        sequences = sum(item.count for item in config.instruction_only)
        events = sum(item.count * item.len_range[0] for item in config.instruction_only)
        replay = 0
        if timeline.crossed_primary_sessions != 0 or timeline.duplicate_sequences != 0:
            _error(state, "[generate.timeline]",
                   "instruction_only requires zero crossing and duplicate sequences")
    if timeline.primary_sessions != sequences - timeline.crossed_primary_sessions:
        _error(state, "[generate.timeline].primary_sessions",
               "must equal primary sequence total minus crossed_primary_sessions")
    if (timeline.noise_events > 0) != (config.noise is not None):
        _error(state, "[generate.noise]", "noise table must be present iff noise_events > 0")
    if config.noise is not None and len(config.noise.topics) != timeline.noise_events:
        _error(
            state, "[generate.noise].topics",
            "topic count must equal generate.timeline.noise_events",
        )
    stream_rows = events + timeline.noise_events + replay
    record_units = sequences + stream_rows
    if not 1 <= stream_rows <= config.limits.stream_rows:
        _error(state, "[generate]", "derived stream_rows must be in 1..500000")
    if not 1 <= record_units <= config.limits.record_units:
        _error(state, "[generate]", "derived record_units must be in 1..500000")


def _check_gap_shell(state: _ParseState, patterns: tuple[SequencePattern, ...]) -> None:
    """执行不依赖 CP-SAT 的 gap/max-span 必要可行性检查。

    @param state 当前解析状态
    @param patterns pattern 表
    """
    for pattern in patterns:
        by_pair = {(gap.before, gap.after): gap for gap in pattern.gaps}
        minimum_span = sum(by_pair[pair].min_gap_us
                           for pair in zip(pattern.order, pattern.order[1:])
                           if pair in by_pair)
        if minimum_span > pattern.max_span_us:
            _error(state, f"[generate.pattern.{pattern.name}].max_span_s",
                   "adjacent minimum gaps exceed max_span_s")


def _profile_name(state: _ParseState, key: str) -> str:
    """读取并验证一个 sequence profile 名。

    @param state 当前解析状态
    @param key generate 下的 profile 键
    @return profile 名或空串
    """
    name = _string(state, state.generate, key, f"[generate].{key}")
    profile = state.context.llm_profiles.get(name)
    if profile is None:
        _error(state, f"[generate].{key}", f"referenced profile {name!r} does not exist")
    elif profile.context_window <= 0:
        _error(state, f"[generate].{key}", "profile must declare context_window > 0")
    return name


def _budget_role(role) -> dict[str, object]:
    """投影 EventPlan prompt 实际携带的 RoleSpec 字段。"""
    return {
        "name": role.name,
        "frame_class": role.frame_class,
        "actor": role.actor,
        "read_roots": role.read_roots,
        "write_roots": role.write_roots,
        "publish_roots": role.publish_roots,
        "observers": role.observers,
        "state_instruction": role.state_instruction,
        "pre_state_schema": role.pre_state_schema,
    }


def _budget_frame_list(state, names) -> list[dict[str, object]]:
    """按 registry 顺序投影 EventPlan 的帧闭集。"""
    return [{
        "name": name,
        "description": state.context.frame_classes[name].description,
        "generation_instruction": state.context.frame_classes[name].gen_instruction,
    } for name in names]


def _seed_actor_contract(mode: str, actors) -> dict[str, object]:
    """重建 ScenarioSeed prompt 的冻结 actor contract。"""
    profile = {"each_value": "object", "required": ["goal", "identity", "style"]}
    if mode == "declared":
        return {"actor_names": actors, "actor_profile": profile}
    return {
        "actor_name": "non-empty string", "actor_profile": profile,
        "maximum_actor_count": 8, "minimum_actor_count": 1,
    }


def _minimum_seed(actors) -> dict[str, object]:
    """构造 semantic prompt 所需的最小完整 ScenarioSeed。"""
    profiles = {actor: {"goal": {}, "identity": {}, "style": {}} for actor in actors}
    return {
        "initial_state": {}, "actors": profiles,
        "shared_facts": {"public": {}, "hidden": {}}, "style": {}, "time_context": {},
    }


def _minimum_history(count: int, actor: str = "a") -> list[dict[str, object]]:
    """构造 instruction history/observation 的非递归最小 witness。"""
    return [{
        "event_key": "0" * 32, "logical_time_us": index,
        "frame_class": "a", "actor": actor, "intent": "a",
        "patch": _minimum_patch(), "state_before_hash": "0" * 64,
        "state_after_hash": "0" * 64, "publish_snapshot": {}, "payload": {},
    } for index in range(count)]


def _minimum_patch() -> list[dict[str, object]]:
    """构造 EventPlan 允许的最小 test + mutation patch。"""
    return [
        {"op": "test", "path": "/a", "value": None},
        {"op": "replace", "path": "/a", "value": None},
    ]


def _minimum_actor_view(actor: str, logical: int, observations=()):
    """构造保留完整 carrier 形状的最小 ActorView。"""
    return {
        "actor": actor, "goal": {}, "read_state": {},
        "observations": observations, "logical_time_us": logical,
        "wait_since_previous_us": 0,
    }


def _scenario_budget_cases(state, config) -> list[_BudgetCase]:
    """枚举会真实调用 LLM 的 ScenarioSeed 最小 PromptBundle。"""
    from labelkit.common.inference.generation_prompts import scenario_seed_prompt
    from labelkit.common.inference.schema_engine import scenario_seed_schema

    patterns = {item.name: item for item in config.patterns}
    cases = []
    for source in config.counterfactual_sets:
        pattern = patterns.get(source.pattern)
        view = None if pattern is None else state.context.class_views.get(pattern.sequence_class)
        generation = None if view is None else view.sequence_generation
        if pattern is None or generation is None or generation.initial_state_source == "catalog":
            continue
        actors = declared_actor_names(pattern)
        fields = _seed_fields(
            config, (source.name, pattern.sequence_class),
            (view.description, generation.instruction, actors, generation.state_schema),
        )
        cases.append(_BudgetCase(config.semantic_profile, scenario_seed_prompt(fields),
                                 scenario_seed_schema(actors, generation.state_schema), False))
    for source in config.instruction_only:
        view = state.context.class_views.get(source.sequence_class)
        if view is None:
            continue
        fields = _seed_fields(
            config, (source.name, source.sequence_class),
            (view.description, source.instruction, (), source.state_schema),
        )
        cases.append(_BudgetCase(config.semantic_profile, scenario_seed_prompt(fields),
                                 scenario_seed_schema(None, source.state_schema), False))
    return cases


def _seed_fields(config, identity, content):
    """构造 ScenarioSeed 的配置态完整最小插值表。"""
    source, sequence_class = identity
    description, instruction, actors, schema = content
    return {
        "mode": config.mode, "slot_key": f"{source}/000000", "source_name": source,
        "scenario_index": 0, "attempt_index": config.max_slot_attempts - 1,
        "sequence_class": sequence_class, "class_description": description,
        "generation_instruction": instruction,
        "actor_contract": _seed_actor_contract(config.mode, actors), "state_schema": schema,
    }


def _declared_branches(pattern, source):
    """返回 hidden baseline 与全部 variant 的精确 role word/outcome。"""
    positive = next((item for item in source.variants if item.kind == "positive"), None)
    branches = [(None, pattern.order, None if positive is None else positive.outcome_schema)]
    for variant in source.variants:
        roles = list(pattern.order)
        if variant.kind == "missing":
            target = variant.target.get("role")
            if target in roles:
                roles.remove(target)
        elif variant.kind == "reordered":
            before, after = variant.target.get("before"), variant.target.get("after")
            if before in roles and after in roles:
                left, right = roles.index(before), roles.index(after)
                roles[left], roles[right] = roles[right], roles[left]
        branches.append((variant.name, tuple(roles), variant.outcome_schema))
    return tuple(branches)


def _event_budget_cases(state, config) -> list[_BudgetCase]:
    """枚举 EventPlan 的实际 branch/position 最小 PromptBundle。"""
    cases = _declared_event_cases(state, config)
    cases.extend(_instruction_event_cases(state, config))
    return cases


def _declared_event_cases(state, config) -> list[_BudgetCase]:
    """枚举 declared EventPlan 调用。"""
    from labelkit.common.inference.generation_prompts import event_plan_prompt
    from labelkit.common.inference.schema_engine import event_plan_schema

    patterns, cases = {item.name: item for item in config.patterns}, []
    for source in config.counterfactual_sets:
        pattern = patterns.get(source.pattern)
        view = None if pattern is None else state.context.class_views.get(pattern.sequence_class)
        if pattern is None or view is None or view.sequence_generation is None:
            continue
        role_map = {role.name: role for role in pattern.roles}
        for _variant, roles, outcome in _declared_branches(pattern, source):
            for position, role_name in enumerate(roles):
                role, final = role_map.get(role_name), position == len(roles) - 1
                if role is None:
                    continue
                frame = state.context.frame_classes.get(role.frame_class)
                if frame is None:
                    continue
                frames, actors = _budget_frame_list(state, (role.frame_class,)), (role.actor,)
                fields = _event_fields(config, source.name, role_name, position, len(roles))
                fields.update(_declared_event_fields(view, role, frames, actors,
                                                     outcome if final else None))
                schema = event_plan_schema((role.frame_class,), actors)
                dynamic = (config.limits.prompt_value_bytes,) * 2
                cases.append(_BudgetCase(
                    config.semantic_profile, event_plan_prompt(fields), schema, True, dynamic,
                ))
    return cases


def _declared_event_fields(view, role, frames, actors, outcome) -> dict[str, object]:
    """构造 declared EventPlan 专属最小字段。"""
    return {
        "generation_instruction": view.sequence_generation.instruction,
        "role_contract": _budget_role(role), "eligible_frame_classes": frames,
        "eligible_actors": actors, "actor_view": _minimum_actor_view(role.actor, 0),
        "visible_state": None, "state_schema": None, "outcome_schema": outcome,
        "history": None, "actor_profiles": None, "public_facts": {},
    }


def _instruction_event_cases(state, config) -> list[_BudgetCase]:
    """枚举 instruction-only EventPlan 调用。"""
    from labelkit.common.inference.generation_prompts import event_plan_prompt
    from labelkit.common.inference.schema_engine import event_plan_schema

    noise = None if config.noise is None else config.noise.frame_class
    names = tuple(name for name, view in state.context.frame_classes.items()
                  if name != noise and is_generation_frame_eligible(view))
    frames, actors = _budget_frame_list(state, names), ("a",)
    schema = event_plan_schema(names, actors)
    cases = []
    for source in config.instruction_only:
        if source.sequence_class not in state.context.class_views:
            continue
        for position in range(source.len_range[0]):
            fields = _event_fields(
                config, source.name, f"position_{position:03d}",
                position, source.len_range[0],
            )
            fields.update(_instruction_event_fields(source, frames, actors, position))
            dynamic = (config.limits.prompt_value_bytes,) * 5
            cases.append(_BudgetCase(
                config.semantic_profile, event_plan_prompt(fields), schema, True, dynamic,
            ))
    return cases


def _event_fields(config, source, role, position: int, length: int) -> dict[str, object]:
    """构造两种 EventPlan 共用的完整身份字段。"""
    return {
        "mode": config.mode, "slot_key": f"{source}/000000",
        "attempt_index": config.max_slot_attempts - 1, "variation_nonce": "0" * 64,
        "event_key": "0" * 32, "role": role, "position": position,
        "sequence_length": length, "logical_time_us": position,
        "wait_since_previous_us": 0,
    }


def _instruction_event_fields(source, frames, actors, position: int) -> dict[str, object]:
    """构造 instruction-only EventPlan 专属最小字段。"""
    return {
        "generation_instruction": source.instruction, "role_contract": None,
        "eligible_frame_classes": frames, "eligible_actors": actors, "actor_view": None,
        "visible_state": {}, "state_schema": source.state_schema, "outcome_schema": None,
        "history": _minimum_history(position),
        "actor_profiles": {"a": {"goal": {}, "identity": {}, "style": {}}},
        "public_facts": {},
    }


def _frame_budget_cases(state, config) -> list[_BudgetCase]:
    """枚举 FrameRenderer 的实际帧与位置最小 PromptBundle。"""
    cases = _declared_frame_cases(state, config)
    cases.extend(_instruction_frame_cases(state, config))
    return cases


def _declared_frame_cases(state, config) -> list[_BudgetCase]:
    """枚举 declared FrameRenderer 调用。"""
    patterns, cases = {item.name: item for item in config.patterns}, []
    for source in config.counterfactual_sets:
        pattern = patterns.get(source.pattern)
        if pattern is None:
            continue
        role_map = {role.name: role for role in pattern.roles}
        for _variant, roles, _outcome in _declared_branches(pattern, source):
            for position, role_name in enumerate(roles):
                role = role_map.get(role_name)
                frame = None if role is None else state.context.frame_classes.get(
                    role.frame_class)
                if role is not None and frame is not None:
                    event = (role_name, position, role.actor, ())
                    cases.append(_frame_case(
                        config, source.name, event, (role.frame_class, frame), role,
                    ))
    return cases


def _instruction_frame_cases(state, config) -> list[_BudgetCase]:
    """枚举 instruction-only FrameRenderer 调用。"""
    noise = None if config.noise is None else config.noise.frame_class
    frames = [(name, view) for name, view in state.context.frame_classes.items()
              if name != noise and is_generation_frame_eligible(view)]
    cases = []
    for source in config.instruction_only:
        for position in range(source.len_range[0]):
            for frame_name, frame in frames:
                event = (f"position_{position:03d}", position, "a",
                         _minimum_history(position))
                cases.append(_frame_case(
                    config, source.name, event, (frame_name, frame), None,
                ))
    return cases


def _frame_case(config, source, event, frame_entry, role) -> _BudgetCase:
    """构造一个 FrameRenderer 最小预算 case。"""
    from labelkit.common.inference.generation_prompts import frame_render_prompt

    role_name, position, actor, observations = event
    frame_name, frame = frame_entry
    bindings = [] if role is None else [
        {"payload_path": item.payload_path, "value": None} for item in role.payload_bindings
    ]
    fields = {
        "slot_key": f"{source}/000000", "attempt_index": config.max_slot_attempts - 1,
        "event_key": "0" * 32, "role": role_name, "position": position,
        "frame_class": frame_name, "actor": actor, "logical_time_us": position,
        "wait_since_previous_us": 0, "intent": "a", "patch": _minimum_patch(),
        "actor_view": _minimum_actor_view(actor, position, observations),
        "public_facts": {},
        "publish_snapshot": {}, "state_before_hash": "0" * 64,
        "state_after_hash": "0" * 64, "frame_instruction": frame.gen_instruction,
        "frame_description": frame.description, "binding_values": bindings,
        "frame_schema": frame.gen_schema,
    }
    dynamic = [
        config.limits.prompt_value_bytes,
        config.limits.event_patch_bytes,
        config.limits.prompt_value_bytes,
        config.limits.prompt_value_bytes,
        config.limits.prompt_value_bytes,
        config.limits.prompt_value_bytes,
    ]
    if role is None:
        dynamic.append(config.limits.prompt_value_bytes)
    return _BudgetCase(
        config.semantic_profile, frame_render_prompt(fields), frame.gen_schema, False,
        tuple(dynamic),
    )


def _semantic_budget_cases(state, config) -> list[_BudgetCase]:
    """枚举 SemanticEvaluator 的实际 branch 长度最小 PromptBundle。"""
    from labelkit.common.inference.generation_prompts import semantic_evaluation_prompt
    from labelkit.common.inference.schema_engine import semantic_evaluation_schema

    patterns, cases = {item.name: item for item in config.patterns}, []
    for source in config.counterfactual_sets:
        pattern = patterns.get(source.pattern)
        view = None if pattern is None else state.context.class_views.get(pattern.sequence_class)
        if pattern is None or view is None:
            continue
        role_map = {role.name: role.actor for role in pattern.roles}
        actors = declared_actor_names(pattern)
        for _variant, roles, _outcome in _declared_branches(pattern, source):
            review = _minimum_reviews(tuple(role_map[role] for role in roles if role in role_map))
            fields = _semantic_fields(
                config, (pattern.sequence_class, view.description, pattern.description),
                (_minimum_seed(actors), review),
            )
            dynamic = (
                config.limits.scenario_seed_bytes,
                config.limits.prompt_value_bytes,
                config.limits.prompt_value_bytes,
            )
            cases.append(_BudgetCase(
                config.evaluation_profile, semantic_evaluation_prompt(fields),
                semantic_evaluation_schema(), False, dynamic,
            ))
    cases.extend(_instruction_semantic_cases(state, config))
    return cases


def _instruction_semantic_cases(state, config) -> list[_BudgetCase]:
    """枚举 instruction-only SemanticEvaluator 调用。"""
    from labelkit.common.inference.generation_prompts import semantic_evaluation_prompt
    from labelkit.common.inference.schema_engine import semantic_evaluation_schema

    cases = []
    for source in config.instruction_only:
        view = state.context.class_views.get(source.sequence_class)
        if view is None:
            continue
        fields = _semantic_fields(
            config, (source.sequence_class, view.description, source.instruction),
            (_minimum_seed(("a",)), _minimum_reviews(("a",) * source.len_range[0])),
        )
        dynamic = (
            config.limits.scenario_seed_bytes,
            config.limits.prompt_value_bytes,
            config.limits.prompt_value_bytes,
        )
        cases.append(_BudgetCase(
            config.evaluation_profile, semantic_evaluation_prompt(fields),
            semantic_evaluation_schema(), False, dynamic,
        ))
    return cases


def _minimum_reviews(actors) -> list[dict[str, object]]:
    """构造保持完整字段形状的最小 SemanticReviewEvent 序列。"""
    return [{
        "frame_class": "a", "actor": actor, "logical_time_us": index,
        "wait_since_previous_us": 0,
        "actor_view": _minimum_actor_view(
            actor, index, _minimum_history(index, actor)
        ),
        "intent": "a",
        "patch": _minimum_patch(), "state_before_hash": "0" * 64,
        "state_after_hash": "0" * 64, "publish_snapshot": {}, "payload": {},
    } for index, actor in enumerate(actors)]


def _semantic_fields(config, identity, content):
    """构造 SemanticEvaluator 完整最小字段。"""
    sequence_class, description, pattern = identity
    seed, reviews = content
    return {
        "mode": config.mode, "sequence_class": sequence_class,
        "attempt_index": config.max_slot_attempts - 1,
        "class_description": description, "pattern_description": pattern,
        "scenario_seed": seed, "review_events": reviews, "final_state": {},
    }


def _noise_budget_cases(state, config) -> list[_BudgetCase]:
    """构造 NoiseRenderer 与 NoiseEvaluator 的两个最小 PromptBundle。"""
    from labelkit.common.inference.generation_prompts import (
        noise_evaluation_prompt,
        noise_render_prompt,
    )
    from labelkit.common.inference.schema_engine import noise_semantic_evaluation_schema

    if config.noise is None:
        return []
    frame = state.context.frame_classes.get(config.noise.frame_class)
    if frame is None:
        return []
    classes = {name: view.description for name, view in state.context.class_views.items()}
    frames = {name: view.description for name, view in state.context.frame_classes.items()}
    cases = []
    for topic in config.noise.topics:
        render = noise_render_prompt(_noise_render_fields(
            config, frame, (classes, frames), topic,
        ))
        evaluate = noise_evaluation_prompt({
            "attempt_index": config.max_slot_attempts - 1,
            "class_descriptions": classes, "frame_descriptions": frames,
            "planned_topic": topic, "payload": {},
        })
        cases.extend((
            _BudgetCase(config.semantic_profile, render, frame.gen_schema, False),
            _BudgetCase(
                config.evaluation_profile, evaluate,
                noise_semantic_evaluation_schema(), False,
                (config.limits.rendered_payload_bytes,),
            ),
        ))
    return cases


def _noise_render_fields(config, frame, registries, topic: str) -> dict[str, object]:
    """构造 NoiseRenderer 完整最小字段。"""
    classes, frames = registries
    return {
        "event_key": "0" * 32, "noise_ordinal": 0,
        "attempt_index": config.max_slot_attempts - 1,
        "frame_class": config.noise.frame_class,
        "timestamp_us": config.timeline.timestamp_start_us, "session_id": "noise_000000",
        "class_descriptions": classes, "frame_descriptions": frames,
        "planned_topic": topic,
        "noise_instruction": config.noise.instruction,
        "frame_instruction": frame.gen_instruction, "frame_schema": frame.gen_schema,
    }


def _check_context_budget(state: _ParseState, config: SequenceGenerationConfig) -> None:
    """校验六个 sequence prompt family 的完整静态上界预算。

    @param state 当前解析状态
    @param config 完整 sequence 配置
    """
    from labelkit.common.config._generation_budget import (
        check_generation_context_budget,
    )

    cases = [
        *_scenario_budget_cases(state, config), *_event_budget_cases(state, config),
        *_frame_budget_cases(state, config), *_semantic_budget_cases(state, config),
        *_noise_budget_cases(state, config),
    ]
    check_generation_context_budget(state, config, cases)


def parse_generation_config(
    raw_project: Mapping[str, object],
    context: "GenerationParseContext",
) -> SequenceGenerationConfig:
    """解析 v1.18 序列配置并聚合全部配置错误。

    @param raw_project 原始项目配置
    @param context 配置解析所需的冻结上下文
    @return 完整校验后的序列生成配置
    """
    raw_generate = raw_project.get("generate")
    generate = raw_generate if isinstance(raw_generate, Mapping) else {}
    state = _ParseState(raw_project, context, generate, GenerationLimits())
    mode_value = generate.get("mode")
    if mode_value not in ("declared", "instruction_only"):
        _error(state, "[generate].mode", f"expected declared | instruction_only, got {_fmt(mode_value)}")
        mode_value = "declared"
    semantic = _profile_name(state, "semantic_llm")
    evaluation = _profile_name(state, "evaluation_llm")
    if semantic and semantic == evaluation:
        _error(state, "[generate].evaluation_llm", "must differ from semantic_llm")
    attempts = generate.get("max_slot_attempts", 3)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 20:
        _error(state, "[generate].max_slot_attempts", f"expected integer in 1..20, got {_fmt(attempts)}")
        attempts = 3
    patterns = _parse_patterns(state)
    sets = _parse_sets(state, patterns)
    instructions = _parse_instruction_only(state)
    windows = _parse_windows(state)
    config = SequenceGenerationConfig(
        mode_value, semantic, evaluation, attempts, _parse_state_hook(state), patterns,
        sets, instructions, _parse_timeline(state), windows, _parse_noise(state), state.limits,
    )
    _check_mode(state, mode_value, patterns, sets, instructions)
    _check_references(state, patterns, instructions, windows)
    _check_class_registry(state, patterns, sets)
    _check_frame_registry(state, patterns, config)
    _check_variant_targets(state, patterns, sets)
    _check_gap_shell(state, patterns)
    _check_timeline_counts(state, config)
    from labelkit.common.config._generation_budget import check_generation_content_limits
    check_generation_content_limits(state, config)
    _check_context_budget(state, config)
    return config

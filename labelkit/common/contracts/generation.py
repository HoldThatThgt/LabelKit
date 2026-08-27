"""v1.20 序列生成内核的共享冻结载体。

本模块只声明跨层数据，不实现业务算法。所有 Mapping 输入在构造时递归复制并以
``MappingProxyType`` 暴露；tuple 内的 JSON 容器同样递归冻结。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol, TypeAlias

from labelkit.common.config._collect import _Collector
from labelkit.common.config.generation import (
    CalendarWindowSpec,
    CounterfactualSetSpec,
    GenerationLimits,
    InstructionOnlySpec,
    NoiseSpec,
    RoleSpec,
    SequencePattern,
    TimelineSpec,
    VariantSpec,
)
from labelkit.common.config.model import (
    ClassView,
    FrameClassView,
    LLMProfile,
    ResolvedConfig,
    ResolvedPaths,
)
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import PipelineItem, Record, Usage
from labelkit.common.contracts.execution import TaskExecutor
from labelkit.common.extensions.hooks import ResolvedHook
from labelkit.common.observability.obslog import MetricsSink
from labelkit.common.inference.llm_client import LLMClient, PromptBundle


JsonObject: TypeAlias = Mapping[str, object]
Violation: TypeAlias = Mapping[str, str]
StateValidator: TypeAlias = Callable[["StateTransitionInput"], list[str]]
ScenarioBlock: TypeAlias = Mapping[
    tuple[str, str | None], tuple["PlannedEvent", ...]
]  # 键为 (slot_key, variant_name)；hidden baseline 与 instruction-only 使用 None


def _freeze_value(value: object) -> object:
    """递归复制并冻结 JSON 容器，不复制服务或业务对象。

    @param value 待冻结字段值
    @return 不可变 Mapping/tuple/frozenset 或原对象
    """
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_value(item) for item in value)
    return value


def _freeze_config_tree(value: object) -> object:
    """复制 GenerationProgram 配置视图并冻结其全部嵌套容器。

    @param value class 或 frame class 配置子树
    @return 与原配置隔离的冻结 dataclass 子树
    """
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_config_tree(item) for key, item in value.items()})
    if isinstance(value, tuple):
        return tuple(_freeze_config_tree(item) for item in value)
    if isinstance(value, list):
        return tuple(_freeze_config_tree(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_config_tree(item) for item in value)
    if is_dataclass(value):
        updates = {
            item.name: _freeze_config_tree(getattr(value, item.name))
            for item in fields(value)
        }
        return replace(value, **updates)
    return value


def _freeze_record(record: Record) -> Record:
    """冻结投影源 Record 的 raw 子树与全部成员 raw 子树。

    @param record 待作为 CrossView 源真值保存的记录
    @return 不共享可变 JSON 容器的冻结记录
    """
    members = tuple(_freeze_record(member) for member in record.members)
    return replace(record, raw=_freeze_value(record.raw), members=members)


class _ImmutableCarrier:
    """为冻结 dataclass 提供统一的嵌套容器冻结步骤。"""

    def __post_init__(self) -> None:
        """冻结当前 dataclass 的全部 Mapping 与 tuple 子树。"""
        for item in fields(self):
            value = getattr(self, item.name)
            frozen = _freeze_value(value)
            if frozen is not value:
                object.__setattr__(self, item.name, frozen)


@dataclass(frozen=True)
class GenerationProgram(_ImmutableCarrier):
    """配置编译器冻结且不含随机规划结果的生成程序。"""

    mode: Literal["declared", "instruction_only"]  # 唯一生成模式
    semantic_profile: str                         # 内容生成 profile
    evaluation_profile: str                       # 独立判定 profile
    max_slot_attempts: int                        # 单槽交付尝试上限
    planner_seed: int                             # 唯一 ScenarioPlanner 随机种子
    class_views: Mapping[str, ClassView]           # sequence class 冻结视图
    frame_classes: Mapping[str, FrameClassView]    # frame class 冻结视图
    frame_schema: Mapping[str, object] | None      # 最终帧标注 Schema
    patterns: Mapping[str, SequencePattern]        # pattern 名映射
    counterfactual_sets: tuple[CounterfactualSetSpec, ...]  # declared set 表
    instruction_only: tuple[InstructionOnlySpec, ...]  # instruction-only 表
    timeline: TimelineSpec                        # 精确时间线
    calendar_windows: Mapping[str, CalendarWindowSpec]  # 固定窗口表
    noise: NoiseSpec | None                       # 可选 noise 声明
    limits: GenerationLimits                      # 固定实现上限
    state_validator: ResolvedHook | None          # 可选状态 hook
    digest: str                                   # 规范化程序摘要

    def __post_init__(self) -> None:
        """复制 class 与 frame class 视图后冻结完整程序。"""
        object.__setattr__(self, "class_views", _freeze_config_tree(self.class_views))
        object.__setattr__(self, "frame_classes", _freeze_config_tree(self.frame_classes))
        object.__setattr__(self, "frame_schema", _freeze_config_tree(self.frame_schema))
        super().__post_init__()


@dataclass(frozen=True)
class DeliverySlot(_ImmutableCarrier):
    """ScenarioPlanner 在声明序冻结的一组精确交付槽。"""

    slot_key: str                                 # 全局唯一槽键
    source_name: str                              # set 或 instruction 声明名
    scenario_index: int                           # 声明内零基场景序号
    sequence_class: str                           # sequence class 名
    pattern_name: str | None                      # declared pattern；instruction-only 为 None
    variant_names: tuple[str, ...]                # 声明序交付分支
    catalog_row_index: int | None                 # catalog 固定行；LLM source 为 None


@dataclass(frozen=True)
class PlannedEvent(_ImmutableCarrier):
    """计划器冻结的事件位置、时间与互斥资源。"""

    event_key: str                                # 跨分支稳定事件键
    role: str                                     # declared role 或 position_NNN
    position: int                                 # 分支内零基位置
    logical_time_us: int                          # 世界语义时间
    timestamp_us: int                             # 工件全局时间
    duration_us: int                              # 固定非负时长；零表示点事件
    resources: tuple[str, ...]                    # 声明序互斥资源
    session_id: str                               # 工件 session 标识


@dataclass(frozen=True)
class NoiseSlot(_ImmutableCarrier):
    """与 PlannedEvent 分离的精确 noise 事件槽。"""

    event_key: str                                # 稳定 noise 事件键
    ordinal: int                                  # 零基 noise 序号
    frame_class: str                              # noise 专用帧类
    topic: str                                    # 显式声明的唯一噪声话题
    timestamp_us: int                             # 工件全局时间
    duration_us: int                              # 固定为零的点事件时长
    resources: tuple[str, ...]                    # 固定为空的资源表
    session_id: str                               # 独立 session 标识


@dataclass(frozen=True)
class ReplayLayout(_ImmutableCarrier):
    """一个 positive source 的零 LLM replay 布局。"""

    source_slot_key: str                          # replay 来源交付槽
    source_variant_name: str                      # replay 来源 positive 变体
    replay_ordinal: int                           # 全局 replay 序号
    session_id: str                               # replay 独立 session
    shift_us: int                                 # source 全部 member 共享的正平移量


@dataclass(frozen=True)
class ScenarioPlan(_ImmutableCarrier):
    """唯一 OPTIMAL 解码得到的完整冻结场景计划。"""

    blocks: tuple[ScenarioBlock, ...]             # 有界 session block 表
    delivery_slots: tuple[DeliverySlot, ...]      # 声明序交付槽
    noise_slots: tuple[NoiseSlot, ...]            # 精确 noise 槽
    replay_layouts: tuple[ReplayLayout, ...]      # 精确 replay 布局
    primary_sessions: int                         # primary session 数
    digest: str                                   # 规范化计划摘要


@dataclass(frozen=True)
class SequenceTemporalMember(_ImmutableCarrier):
    """序列标注可见的单个最终 member 时间事实。"""

    event_id: str                                 # 最终 member 事件 ID
    timestamp_us: int                             # Planner 权威起点
    duration_us: int                              # Planner 权威时长
    resources: tuple[str, ...]                    # 帧类声明序资源


@dataclass(frozen=True)
class SequenceTemporalContext(_ImmutableCarrier):
    """一个最终 sequence 不含 payload 的冻结时间上下文。"""

    members: tuple[SequenceTemporalMember, ...]   # 最终 member 顺序时间事实


@dataclass(frozen=True)
class ScenarioSeed(_ImmutableCarrier):
    """事件发生前的完整世界快照与 actor profile。"""

    initial_state: JsonObject                     # 完整初始状态
    actors: Mapping[str, JsonObject]              # actor 到 goal/identity/style
    shared_facts: JsonObject                      # public 与 hidden 事实
    style: JsonObject                             # 场景共享风格
    time_context: JsonObject                      # 世界时间上下文


@dataclass(frozen=True)
class ActorView(_ImmutableCarrier):
    """某 actor 在一个 logical time 可见的机械视图。"""

    actor: str                                    # actor 名
    goal: JsonObject                              # 当前 actor 的目标
    read_state: JsonObject                        # read roots 投影视图
    observations: tuple[JsonObject, ...]          # 已发布给 actor 的有序事实
    logical_time_us: int                          # 当前世界时间
    wait_since_previous_us: int                   # 相对上一事件等待时间


@dataclass(frozen=True)
class EventPlan(_ImmutableCarrier):
    """EventPlanner 输出的一次意图与 RFC 6902 patch。"""

    frame_class: str                              # 选定帧类
    actor: str                                    # 选定 actor
    intent: str                                   # 当前事件业务意图
    patch: tuple[JsonObject, ...]                 # 完整 patch 操作序列


@dataclass(frozen=True)
class EventExecution(_ImmutableCarrier):
    """一次事件 patch 恰好执行一次得到的缓存证明。"""

    state_before: JsonObject                      # 执行前深冻结状态
    state_after: JsonObject                       # 执行后深冻结状态
    state_before_hash: str                        # 执行前 canonical hash
    state_after_hash: str                         # 执行后 canonical hash
    publish_snapshot: JsonObject                  # publish roots 机械投影
    normalized_patch: tuple[JsonObject, ...]      # 规范化且已执行 patch


@dataclass(frozen=True)
class EventDraft(_ImmutableCarrier):
    """独立 pattern binding 前唯一允许进入生成 history 的事件。"""

    event_key: str                                # 跨分支稳定事件键
    event_id: str                                 # 当前分支主事件 ID
    frame_class: str                              # 实际帧类
    actor: str                                    # 实际 actor
    logical_time_us: int                          # 世界时间
    timestamp_us: int                             # 工件时间
    duration_us: int                              # 计划器冻结的事件时长
    actor_view: ActorView                         # 事件前 actor 知识
    intent: str                                   # 事件意图
    patch: tuple[JsonObject, ...]                 # 已执行 patch
    state_before_hash: str                        # 执行前状态 hash
    state_after_hash: str                         # 执行后状态 hash
    publish_snapshot: JsonObject                  # 发布快照
    payload: JsonObject                           # 最终绑定后 payload


@dataclass(frozen=True)
class EventTruth(_ImmutableCarrier):
    """独立 pattern binding 后带实际 role 的事件真值。"""

    event_key: str                                # 跨分支稳定事件键
    event_id: str                                 # 当前分支事件 ID
    role: str                                     # 独立绑定的 declared role
    frame_class: str                              # 实际帧类
    actor: str                                    # 实际 actor
    logical_time_us: int                          # 世界时间
    timestamp_us: int                             # 工件时间
    duration_us: int                              # 计划器冻结的事件时长
    actor_view: ActorView                         # 事件前 actor 知识
    intent: str                                   # 事件意图
    patch: tuple[JsonObject, ...]                 # 已执行 patch
    state_before_hash: str                        # 执行前状态 hash
    state_after_hash: str                         # 执行后状态 hash
    publish_snapshot: JsonObject                  # 发布快照
    payload: JsonObject                           # 最终 payload


@dataclass(frozen=True)
class ObservedEvent(_ImmutableCarrier):
    """PatternEvaluator 唯一可见的最小观察事件。"""

    event_id: str                                 # 事件 ID
    frame_class: str                              # 实际帧类
    timestamp_us: int                             # 实际工件顺序坐标
    duration_us: int                              # 实际工件区间时长


@dataclass(frozen=True)
class SemanticReviewEvent(_ImmutableCarrier):
    """SemanticEvaluator 的直接盲审事件视图。"""

    frame_class: str                              # 实际帧类
    actor: str                                    # 实际 actor
    logical_time_us: int                          # 世界时间
    duration_us: int                              # 计划器冻结的事件时长
    wait_since_previous_us: int                   # 相对等待时间
    actor_view: ActorView                         # actor 知识视图
    intent: str                                   # 事件意图
    patch: tuple[JsonObject, ...]                 # 已执行 patch
    state_before_hash: str                        # 执行前状态 hash
    state_after_hash: str                         # 执行后状态 hash
    publish_snapshot: JsonObject                  # 发布快照
    payload: JsonObject                           # 最终 payload


@dataclass(frozen=True)
class PatternEvaluation(_ImmutableCarrier):
    """独立 pattern 绑定与规范化违规结果。"""

    actual_bindings: Mapping[str, str]            # event_id 到 role 的全双射
    actual_violations: tuple[Violation, ...]      # 独立观察到的违规闭集


@dataclass(frozen=True)
class StateEvaluation(_ImmutableCarrier):
    """独立状态重放、binding、outcome 与前缀判定。"""

    replay_hash: str                              # 独立重放终态 hash
    final_state_hash: str                         # 交付终态 hash
    bindings_valid: bool                         # binding 值是否一致
    outcome_valid: bool                          # outcome Schema 是否通过
    protected_prefix_valid: bool                 # 反事实前缀是否受保护


@dataclass(frozen=True)
class SemanticEvaluation(_ImmutableCarrier):
    """六项独立语义判定及闭集原因码。"""

    causal_consistency: bool                      # 因果是否一致
    actor_knowledge: bool                         # actor 知识是否合规
    goal_consistency: bool                        # goal 是否一致
    temporal_plausibility: bool                   # 世界时间是否可信
    cross_frame_consistency: bool                 # 跨帧语义是否一致
    realism: bool                                 # 整体是否真实
    reason_codes: tuple[Literal[
        "causal_inconsistency",
        "actor_knowledge_violation",
        "goal_inconsistency",
        "temporal_implausibility",
        "cross_frame_inconsistency",
        "unrealistic",
    ], ...]                                      # 失败原因闭集


@dataclass(frozen=True)
class NoiseSemanticEvaluation(_ImmutableCarrier):
    """noise 的四项独立语义判定及闭集原因码。"""

    unrelated_to_declared_tasks: bool             # 与声明任务无关
    no_executable_task: bool                      # 不含可执行任务
    realism: bool                                 # 文本是否真实
    matches_planned_topic: bool                   # payload 是否忠实已声明话题
    reason_codes: tuple[Literal[
        "related_to_declared_task",
        "executable_task_present",
        "unrealistic",
        "planned_noise_topic_mismatch",
    ], ...]                                      # 失败原因闭集


@dataclass(frozen=True)
class EventTrace(_ImmutableCarrier):
    """通过全部独立 gate 后组装的完整世界真值。"""

    scenario_id: str                              # 跨分支场景 ID
    world_branch_id: str                          # 当前世界分支 ID
    sequence_class: str                           # sequence class 名
    pattern_name: str | None                      # declared pattern；instruction-only 为 None
    variant_name: str | None                      # declared variant；instruction-only 为 None
    scenario_seed: ScenarioSeed                   # 完整初始世界
    events: tuple[EventTruth, ...]                # 按实际发生顺序的事件真值
    final_state: JsonObject                       # 当前分支最终状态
    pattern_evaluation: PatternEvaluation | None  # instruction-only 为 None
    state_evaluation: StateEvaluation             # 独立状态判定
    semantic_evaluation: SemanticEvaluation       # 独立语义判定


@dataclass(frozen=True)
class GenerationParseContext(_ImmutableCarrier):
    """M1 解析 sequence namespace 所需的唯一上下文。"""

    project_root: Path                            # project.toml 所在目录
    class_views: Mapping[str, ClassView]           # 已冻结 sequence registry
    frame_classes: Mapping[str, FrameClassView]    # 已冻结 frame registry
    llm_profiles: Mapping[str, LLMProfile]         # secret-free profile 表
    max_repair_attempts: int                       # M8 L3 修复轮数
    repair_profile: str | None                     # 显式 repair profile；None 沿用首轮
    hook_loader: Callable[[str, Path], ResolvedHook]  # 工程根相对 hook loader
    collector: _Collector                         # 全量错误聚合器


@dataclass(frozen=True)
class ScenarioSeedRequest(_ImmutableCarrier):
    """ScenarioSeed 生成或 catalog 选择请求。"""

    program: GenerationProgram                    # 冻结程序
    slot: DeliverySlot                            # 当前槽
    attempt_index: int                            # 零基尝试序号
    random_seed: int                              # 目的域完整整数 seed


@dataclass(frozen=True)
class EventPlanRequest(_ImmutableCarrier):
    """从唯一执行上下文机械投影的 prompt-safe 规划请求。"""

    mode: Literal["declared", "instruction_only"]  # 当前模式
    semantic_profile: str                         # 内容 profile
    slot_key: str                                 # 当前槽键
    planned_event: PlannedEvent                   # 冻结计划事件
    role: RoleSpec | None                         # declared role；instruction-only 为 None
    generation_instruction: str                   # 类或 instruction-only 指令
    sequence_length: int                          # 冻结序列长度
    eligible_frame_classes: Mapping[str, FrameClassView]  # 有序闭集帧视图
    eligible_actors: tuple[str, ...]              # 有序 actor 闭集
    actor_view: ActorView | None                  # declared 机械视图
    visible_state: JsonObject | None              # instruction-only 完整可见状态
    state_schema: Mapping[str, object] | None     # instruction-only 完整状态约束
    outcome_schema: Mapping[str, object] | None   # declared 末事件 branch 后置条件
    history: tuple[EventDraft, ...] | None        # instruction-only 的 draft 历史
    actor_profiles: Mapping[str, JsonObject] | None  # instruction-only 的 actor 档案
    public_facts: JsonObject                      # 共享 public facts
    attempt_index: int                            # 当前尝试序号
    variation_nonce: str                          # 当前事件变化 nonce


@dataclass(frozen=True)
class EventExecutionContext(_ImmutableCarrier):
    """事件规划与后置执行的单根上下文。"""

    program: GenerationProgram                    # 冻结程序
    plan: ScenarioPlan                            # 冻结计划
    slot: DeliverySlot                            # 当前交付槽
    variant_name: str | None                      # 当前 declared 分支
    event_index: int                              # 当前计划事件下标
    scenario_seed: ScenarioSeed                   # 当前场景 seed
    current_state: JsonObject                     # 当前完整状态
    history: tuple[EventDraft, ...]               # 仅包含已完成 draft


@dataclass(frozen=True)
class StateTransitionInput(_ImmutableCarrier):
    """state validator 唯一可见的深冻结转换输入。"""

    slot_key: str                                 # 当前槽键
    variant: str | None                           # 当前 declared variant
    role: str | None                              # declared RoleSpec 名；instruction-only 为 None
    state_before: JsonObject                      # 执行前状态
    state_after: JsonObject                       # 执行后状态
    patch: tuple[JsonObject, ...]                 # 规范化 patch


@dataclass(frozen=True)
class PostValidationResult(_ImmutableCarrier):
    """M8 单次后置校验产生的违规或唯一执行证明。"""

    violations: tuple[str, ...]                   # 可修复违规；成功时为空
    event_execution: EventExecution | None        # 成功时唯一非空证明


CallPostValidator: TypeAlias = Callable[
    [Mapping[str, object]], PostValidationResult
]


@dataclass(frozen=True)
class PostValidatedCallRequest(_ImmutableCarrier):
    """一次带 request-local 后置校验器的 M8 调用。"""

    profile: str                                  # 调用 profile
    prompt: PromptBundle                          # 完整提示包
    schema: JsonObject                            # 完整内部 Schema
    scope: CallScope                              # 记账与 trace 范围
    post_validator: CallPostValidator             # 仅本请求生效的后置验证器


@dataclass(frozen=True)
class ValidatedGenerationCall(_ImmutableCarrier):
    """M8 后置验证成功且保留同一执行证明的结果。"""

    object: JsonObject                            # 成功候选对象
    event_execution: EventExecution               # 同一候选唯一执行证明
    resolved_at: Literal["l0_or_clean", "l1", "l3_1", "l3_2"]  # 内部解析路径
    usage: Usage                                  # 累计 token 用量
    attempts: int                                 # 总 LLM 调用次数
    model: str                                    # 首轮实际模型名


@dataclass(frozen=True)
class RenderEventRequest(_ImmutableCarrier):
    """FrameRenderer 的完整但不含状态正文的请求。"""

    semantic_profile: str                         # 内容 profile
    slot_key: str                                 # 当前槽键
    planned_event: PlannedEvent                   # 冻结计划事件
    event_plan: EventPlan                         # 已执行的事件计划
    actor_view: ActorView                         # 当前 actor 可见视图
    publish_snapshot: JsonObject                  # 可发布状态投影
    state_before_hash: str                        # 执行前状态 hash
    state_after_hash: str                         # 执行后状态 hash
    binding_values: Mapping[str, object]          # payload path 到权威值
    frame_spec: FrameClassView                    # 完整帧指令与 Schema
    role: RoleSpec | None                         # declared binding 声明
    public_facts: JsonObject                      # 共享 public facts
    attempt_index: int                            # 当前尝试序号
    utc_offset_minutes: int                       # timeline 固定 UTC offset 分钟
    limits: GenerationLimits                      # 程序摘要绑定的唯一生成上限


@dataclass(frozen=True)
class StateEvaluationRequest(_ImmutableCarrier):
    """StateEvaluator 的独立重放请求。"""

    program: GenerationProgram                    # 冻结程序与 state hook
    slot: DeliverySlot                            # 当前槽
    pattern: SequencePattern | None               # instruction-only 为 None
    variant: VariantSpec | None                   # instruction-only 为 None
    scenario_seed: ScenarioSeed                   # 完整初始世界
    events: tuple[EventTruth, ...]                # 当前分支真值
    baseline_events: tuple[EventTruth, ...]       # declared hidden baseline 真值
    final_state: JsonObject                       # 当前分支终态


@dataclass(frozen=True)
class CouplingEvaluationRequest(_ImmutableCarrier):
    """Counterfactual protected prefix 的逐字节比较请求。"""

    variant: VariantSpec                          # 当前反事实声明
    baseline_events: tuple[EventTruth, ...]       # baseline 真值
    events: tuple[EventTruth, ...]                # 当前分支真值
    frame_classes: Mapping[str, FrameClassView]   # 去除帧类时间 path 的冻结声明


@dataclass(frozen=True)
class SemanticEvaluationRequest(_ImmutableCarrier):
    """不含结构目标或先验 verdict 的直接盲审请求。"""

    evaluation_profile: str                       # 独立判定 profile
    mode: Literal["declared", "instruction_only"]  # 当前模式
    sequence_class: str                           # sequence class 名
    class_description: str                        # sequence class 描述
    pattern_description: str                      # pattern 或 instruction 描述
    scenario_seed: ScenarioSeed                   # 完整世界 seed
    review_events: tuple[SemanticReviewEvent, ...]  # 盲审事件表
    final_state: JsonObject                       # 完整终态
    attempt_index: int                            # 当前尝试序号
    limits: GenerationLimits                      # 程序摘要绑定的唯一生成上限


@dataclass(frozen=True)
class NoiseRenderRequest(_ImmutableCarrier):
    """独立 noise payload 渲染请求。"""

    semantic_profile: str                         # 内容 profile
    noise_slot: NoiseSlot                         # 专型 noise 槽
    noise_spec: NoiseSpec                         # noise 指令
    frame_spec: FrameClassView                    # 完整帧 Schema
    class_descriptions: Mapping[str, str]         # sequence class 描述表
    frame_descriptions: Mapping[str, str]         # frame class 描述表
    attempt_index: int                            # 当前尝试序号
    utc_offset_minutes: int                       # timeline 固定 UTC offset 分钟
    limits: GenerationLimits                      # 程序摘要绑定的唯一生成上限


@dataclass(frozen=True)
class NoiseEvaluationRequest(_ImmutableCarrier):
    """独立 noise 语义判定请求。"""

    evaluation_profile: str                       # 独立判定 profile
    payload: JsonObject                           # 待判定 noise payload
    planned_topic: str                            # NoiseSlot 显式声明的话题
    class_descriptions: Mapping[str, str]         # declared task 描述表
    frame_descriptions: Mapping[str, str]         # frame 描述表
    attempt_index: int                            # 当前尝试序号
    limits: GenerationLimits                      # 程序摘要绑定的唯一生成上限


@dataclass(frozen=True)
class ProjectionRequest(_ImmutableCarrier):
    """EventTrace 到 main/primary stream 双视图的投影请求。"""

    program: GenerationProgram                    # 冻结程序
    plan: ScenarioPlan                            # 公开入口必须自证的唯一计划
    slot: DeliverySlot                            # 当前槽
    trace: EventTrace                             # 已过全部 gate 的真值


@dataclass(frozen=True)
class NoiseProjectionRequest(_ImmutableCarrier):
    """noise payload 到最终 stream row 的投影请求。"""

    program: GenerationProgram                    # 冻结程序
    run_id: str                                   # 当前 run ID
    noise_slot: NoiseSlot                         # noise 槽
    payload: JsonObject                           # 已通过 gate 的 payload


@dataclass(frozen=True)
class ReplayProjectionRequest(_ImmutableCarrier):
    """最终 source rows 到 replay rows 的投影请求。"""

    program: GenerationProgram                    # 冻结程序
    plan: ScenarioPlan                            # 已验证且摘要自洽的唯一计划
    layout: ReplayLayout                          # 冻结 replay 布局
    source: "SequenceRows"                        # 已经下游装配的 source rows


@dataclass(frozen=True)
class ProjectedSequence(_ImmutableCarrier):
    """进入 attempt 下游前的 main record 与 primary stream 投影。"""

    main_record: Record                           # 唯一 sequence Record
    primary_stream_rows: tuple[JsonObject, ...]   # 对应成员 primary rows

    def __post_init__(self) -> None:
        """递归冻结行与 Record.raw，使投影可充当 CrossView 源真值。"""
        super().__post_init__()
        object.__setattr__(self, "main_record", _freeze_record(self.main_record))


@dataclass(frozen=True)
class SequenceRows(_ImmutableCarrier):
    """下游完成后的最终 main 与 primary stream 行。"""

    main_row: JsonObject                          # 最终 main JSON 对象
    primary_stream_rows: tuple[JsonObject, ...]   # 最终 primary stream 行
    retained_content_bytes: int                   # 两视图 canonical byte 费用


@dataclass(frozen=True)
class SequenceAssemblyRequest(_ImmutableCarrier):
    """M11 从最终 attempt item 装配交付行的闭包请求。"""

    program: GenerationProgram                    # 唯一 program-bound Schema 真值
    schema_engine: "SchemaEngine"                 # M8 独立写前终检实例
    item: PipelineItem                            # 下游完成后的唯一信封
    projection: ProjectedSequence                 # 与信封对齐的基础投影
    batch_no: int                                 # 一基 declaration ordinal


@dataclass(frozen=True)
class ReplayRows(_ImmutableCarrier):
    """一次完整 replay 的最终 stream rows 与 byte 费用。"""

    rows: tuple[JsonObject, ...]                  # source 顺序 replay 行
    retained_content_bytes: int                   # canonical JSONL byte 费用


@dataclass(frozen=True)
class ProjectionWitness(_ImmutableCarrier):
    """不保留源内容的 CrossView projector 摘要证明。"""

    main_record_id: str                           # projector 产出的 sequence ID
    generation_digest: str                        # main generation 的完整 SHA-256
    member_sources_digest: str                    # member sources 的完整 SHA-256
    primary_base_digests: tuple[str, ...]          # 每行基础三字段 full SHA-256


@dataclass(frozen=True)
class PrimaryCandidateReconcileRequest(_ImmutableCarrier):
    """当前 primary 候选的封闭本地 CrossView 请求。"""

    program: GenerationProgram                    # 独立身份派生程序
    plan: ScenarioPlan                            # 已验证且摘要自洽的唯一计划
    run_id: str                                   # 当前运行身份
    slot: DeliverySlot                            # 当前声明序交付槽
    projection_witnesses: tuple[ProjectionWitness, ...]  # 严格 variant 序摘要
    sequences: tuple[SequenceRows, ...]           # 严格 variant 序最终行
    replay_layouts: tuple[ReplayLayout, ...]      # 当前 source 的完整 replay 布局
    replays: tuple[ReplayRows, ...]                # 与布局一一对应的 replay 行
    retained_content_bytes: int                   # 当前候选全部行的费用


@dataclass(frozen=True)
class NoiseCandidateReconcileRequest(_ImmutableCarrier):
    """当前 noise 候选的封闭本地 CrossView 请求。"""

    program: GenerationProgram                    # 独立身份派生程序
    run_id: str                                   # 当前运行身份
    noise_slot: NoiseSlot                         # 当前声明序 noise 槽
    payload_digest: str                           # post-gate payload 摘要
    row: JsonObject                               # 最终 noise stream 行
    retained_content_bytes: int                   # 当前 noise 行费用


@dataclass(frozen=True)
class ReconcileRequest(_ImmutableCarrier):
    """CrossViewReconciler 的最终全量行请求。"""

    program: GenerationProgram                    # 独立身份派生程序
    plan: ScenarioPlan                            # 冻结计划
    run_id: str                                   # noise 身份派生 run ID
    projection_witnesses: tuple[ProjectionWitness, ...]  # 与最终行对齐的源摘要
    sequences: tuple[SequenceRows, ...]           # 交付声明序 sequence rows
    noise_payload_digests: tuple[str, ...]         # post-gate noise payload 摘要
    noise_rows: tuple[JsonObject, ...]            # NoiseSlot 顺序行
    replays: tuple[ReplayRows, ...]                # ReplayLayout 顺序分组行
    retained_content_bytes: int                   # 最终全部行总费用


@dataclass(frozen=True)
class GenerationServices(_ImmutableCarrier):
    """sequence 生成与下游共享的唯一服务根。"""

    config: ResolvedConfig                        # 完整冻结配置
    schema_engine: SchemaEngine                   # 唯一 M8 实例
    llm: LLMClient                                # 唯一 M9 实例
    metrics: MetricsSink                          # 唯一 M12 实例
    tasks: TaskExecutor                           # Application 拥有的唯一任务执行器


@dataclass(frozen=True)
class DeliveryRequest(_ImmutableCarrier):
    """一次精确交付运行的计划、路径与身份。"""

    program: GenerationProgram                    # 冻结程序
    plan: ScenarioPlan                            # 冻结计划
    paths: ResolvedPaths                          # 固定输出路径
    run_attempt_id: str                           # program 与 seed 派生 ID
    run_id: str                                   # attempt 与 plan 派生 ID


class DownstreamAttemptCollaborator(Protocol):
    """不采用 Stage 记录隔离语义的 attempt-local 下游 gate。"""

    async def run_attempt(
        self,
        request: "DownstreamAttemptRequest",
    ) -> "DownstreamAttemptResult":
        """执行一次事务 gate，并保持 run-terminal 异常原样穿透。

        @param request 当前 attempt 唯一事务与运行上下文
        @return 接受状态、拒绝阶段与局部 dataset counter delta
        """
        ...


class DedupIndex(Protocol):
    """sequence-group 原子准入所需的全局 dedup 协议。"""

    async def group_reserve(
        self,
        request: "DedupGroupRequest",
        context: RunContext,
    ) -> "DedupReservation":
        """无正式索引突变地计算特征并创建 pending reservation。

        @param request 整组记录、豁免对与 embedding profile
        @param context 与 GenerationServices 共享身份的运行上下文
        @return 当前 coordinator 唯一拥有的 reservation capability
        """
        ...

    def group_revalidate(self, reservation: "DedupReservation") -> None:
        """无 await 地对最新正式索引重验并进入 Validated 状态。

        @param reservation 当前 epoch 的 Reserved capability
        @return None
        """
        ...

    def group_commit(self, reservation: "DedupReservation") -> None:
        """无 await 地消费并提交一个当前 generation 的 Validated reservation。

        @param reservation 当前 generation 已重验的 capability
        @return None
        """
        ...

    def group_discard(self, reservation: "DedupReservation") -> None:
        """严格消费一次未提交 reservation，且不修改正式索引。

        @param reservation 当前 coordinator 或候选缓冲拥有的 capability
        @return None
        """
        ...


class SequenceDeliveryEmitter(Protocol):
    """延迟打开输出的序列装配与 manifest-last 提交协议。"""

    def assemble_sequence(
        self,
        request: SequenceAssemblyRequest,
    ) -> SequenceRows:
        """从闭包请求装配不可变行并执行最终 Schema 终检。

        @param request program、M8、最终 item、投影与批号
        @return 最终 rows 与 byte 费用
        """
        ...

    def prepare_product(
        self,
        main_rows: Sequence[Mapping[str, object]],
        stream_rows: Sequence[Mapping[str, object]],
        report: Mapping[str, object],
    ) -> "GenerationProduct":
        """计算唯一 delivery digest 并冻结产物。

        @param main_rows 最终 main rows
        @param stream_rows 最终 stream rows
        @param report 尚无 delivery digest 的报告
        @return 已带 digest 的冻结产物
        """
        ...

    def commit(self, product: "GenerationProduct") -> Mapping[str, object]:
        """按 main、stream、report、manifest-last 原子替换。

        @param product 已冻结产物
        @return 已提交 manifest
        """
        ...

    def write_failed_report(self, report: Mapping[str, object]) -> None:
        """尽力原子写入无内容 failed report。

        @param report 冻结失败报告
        @return None
        """
        ...


@dataclass(frozen=True)
class DeliveryServices(_ImmutableCarrier):
    """交付控制器的唯一 generation 根与下游协作者。"""

    generation: GenerationServices                # 唯一生成服务根
    dedup: DedupIndex                             # 原子 group dedup 协作者
    quality: DownstreamAttemptCollaborator | None # 可选 pointwise quality gate
    annotate: DownstreamAttemptCollaborator | None  # 可选 annotation gate
    verify: DownstreamAttemptCollaborator | None  # 可选 verify gate
    emitter: SequenceDeliveryEmitter              # 延迟打开 emitter


@dataclass(frozen=True)
class AttemptTransaction(_ImmutableCarrier):
    """一次 slot attempt 内唯一可回滚事务真值。"""

    items: tuple[PipelineItem, ...]               # 下游原地修改的唯一 item 表
    class_views: Mapping[str, ClassView]           # 当前分支有效类视图
    projected_sequences: tuple[ProjectedSequence, ...]  # 与 items 对齐的生成投影


@dataclass(frozen=True)
class DownstreamAttemptRequest(_ImmutableCarrier):
    """一个下游 attempt gate 的请求。"""

    transaction: AttemptTransaction               # 当前事务
    run_context: RunContext                       # 身份一致的运行上下文


@dataclass(frozen=True)
class DownstreamAttemptResult(_ImmutableCarrier):
    """下游 gate 的接受状态与局部数据集计数。"""

    accepted: bool                                # gate 是否接受
    rejected_stage: Literal["quality", "annotate", "verify"] | None  # 拒绝阶段
    dataset_counters: Mapping[str, int]            # 仅成功提交后合并的 delta


@dataclass(frozen=True)
class DedupGroupRequest(_ImmutableCarrier):
    """整组原子 dedup reservation 请求。"""

    records: tuple[Record, ...]                   # 当前 set 的全部 Record
    exempt_pairs: frozenset[tuple[str, str]]      # set 内豁免记录对
    embedding_profile: str | None                 # 可选 semantic profile


@dataclass(frozen=True)
class DedupReservation(_ImmutableCarrier):
    """绑定 registry 与 reset epoch 的一次性 reservation capability。"""

    capability_id: str                            # 不可猜测能力标识
    epoch: int                                    # reset 后递增的 registry epoch
    record_digests: tuple[str, ...]               # 记录内容绑定摘要
    exact_cluster_keys: tuple[str, ...]           # 小型 exact 簇键


@dataclass(frozen=True)
class PreparedCandidate(_ImmutableCarrier):
    """candidate-local 成功后深度冻结的 primary 候选。"""

    slot: DeliverySlot                            # 当前声明序交付槽
    attempt_index: int                            # 当前槽一基 attempt 序号
    projection_witnesses: tuple[ProjectionWitness, ...]  # 严格 variant 序摘要
    sequences: tuple[SequenceRows, ...]           # 严格 variant 序最终行
    replays: tuple[ReplayRows, ...]                # 当前 source 的全部 replay 行
    reservation: DedupReservation                 # 唯一拥有的 pending dedup reservation
    dataset_counters: Mapping[str, int]            # 提交时合并的 dataset delta
    retained_content_bytes: int                   # 当前候选全部行的费用
    digest: str                                   # 除自身外全部字段的规范摘要


@dataclass(frozen=True)
class PreparedNoiseCandidate(_ImmutableCarrier):
    """candidate-local 成功后深度冻结的 noise 候选。"""

    noise_slot: NoiseSlot                         # 当前声明序 noise 槽
    attempt_index: int                            # 当前槽一基 attempt 序号
    payload_digest: str                           # post-gate payload 摘要
    row: JsonObject                               # 最终 noise stream 行
    similarity_signature: tuple[int, ...]         # 提交时消费的相似度签名
    dataset_counters: Mapping[str, int]            # 提交时合并的 dataset delta
    retained_content_bytes: int                   # 当前 noise 行费用
    digest: str                                   # 除自身外全部字段的规范摘要


@dataclass(frozen=True)
class ResourceInterval(_ImmutableCarrier):
    """提交前索引的一个容量一资源半开区间。"""

    resource: str                                 # 互斥资源名
    start_us: int                                 # 包含的区间起点
    end_us: int                                   # 不包含的区间终点
    event_id: str                                 # 占用区间的最终事件 ID
    source_key: str                               # primary/noise/replay 来源身份


@dataclass(frozen=True)
class CrossViewDelta(_ImmutableCarrier):
    """CrossViewFrontier 检查后尚未应用的当前候选增量。"""

    phase: Literal["primary", "noise"]           # 当前 frontier phase
    ordinal: int                                  # 当前 phase 声明序 ordinal
    event_ids: tuple[str, ...]                     # 当前候选新增 event ID
    timestamps_us: tuple[int, ...]                 # 当前候选新增工件时间
    source_keys: tuple[str, ...]                   # 当前候选新增 source 身份
    resource_intervals: tuple[ResourceInterval, ...]  # 按资源、起点排序的区间


@dataclass(frozen=True)
class GenerationProduct(_ImmutableCarrier):
    """提交前冻结的完整 main、stream 与报告产物。"""

    main_rows: tuple[JsonObject, ...]             # 最终 main rows
    stream_rows: tuple[JsonObject, ...]           # 最终 stream rows
    report: JsonObject                            # 含唯一 delivery digest 的报告


# 解析函数位于 config.generation；延迟补全其公开注解的运行期命名空间。
from labelkit.common.config import generation as _config_generation  # noqa: E402
from labelkit.common.inference import schema_engine as _schema_engine  # noqa: E402

_config_generation.GenerationParseContext = GenerationParseContext
CallScope = _schema_engine.CallScope
SchemaEngine = _schema_engine.SchemaEngine
_schema_engine.PostValidatedCallRequest = PostValidatedCallRequest
_schema_engine.ValidatedGenerationCall = ValidatedGenerationCall

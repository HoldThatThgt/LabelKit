"""v1.18 六个 sequence prompt 家族的唯一文本构造器。"""
from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Mapping

from labelkit.common.errors import ContextOverflowError
from labelkit.common.inference.llm_client import Message, Part, PromptBundle


_log = logging.getLogger("labelkit.generation.prompts")
SCENARIO_SEED_SYSTEM = """\
你是场景世界初始化器。创建一个在任何目标事件发生之前就已经存在、\
内部一致的世界快照。
只依据给定的序列类别、类别指令、参与者闭集和状态 Schema 工作。
initial_state 必须满足状态 Schema；actors 必须描述每个参与者的目标、身份和表达风格；
shared_facts.public 是后续事件可公开使用的事实，shared_facts.hidden 只供独立判定使用；
style 与 time_context 必须在同一次尝试的全部分支中保持稳定。
不得写入模式名、变体名、角色顺序、目标违规、最终结果或尚未发生的事件。
只返回一个 JSON 对象，不要 Markdown、代码围栏、解释或额外字段。"""

EVENT_PLAN_SYSTEM = """\
你是逐事件状态规划器。为一个已经冻结逻辑位置的事件规划 frame_class、\
actor、intent 和 JSON Patch。
不得增删事件、改变位置或逻辑时间；不得生成或推断工件 timestamp、\
session 或其他投影坐标。
declared 模式只能读取 ActorView 和 public facts：test 操作必须位于 read_roots，
add、remove、replace 操作必须位于 write_roots；至少一个 test，\
且全部 test 连续位于变更操作之前。
patch 只允许 test、add、remove、replace，不允许 move 或 copy。
instruction_only 模式可以读取明确提供的完整当前状态、状态 Schema、历史和参与者档案，
但 frame_class 和 actor 仍必须来自闭集。
test 的 value 必须逐字取自当前可见状态；只修改完成本事件所需的最少叶子 path。
必须保持未修改字段以及所有 object、array、string、number、boolean、null \
的既有类型与容器形状。
instruction_only 模式的 patch 后完整状态必须满足所提供的状态 Schema。
若提供末事件 Outcome Schema，patch 后完整状态必须同时满足它。
不要生成 payload，不要声称状态已经提交。只返回一个 JSON 对象，\
不要 Markdown、代码围栏、解释或额外字段。"""

FRAME_RENDER_SYSTEM = """\
你是单事件载荷渲染器。把已经通过状态执行的一个事件写成自然、真实且\
与当前 actor 已知信息一致的 JSON object。
只能表达给定 intent、ActorView、公开事实和 publish snapshot 中可见的内容。
不得改变 frame_class、actor、role、intent、patch、状态哈希、逻辑时间或事件数量。
不得生成或推断工件 timestamp、session 或其他投影坐标。
不得猜测 hidden facts。机械绑定值必须出现在返回对象的指定 path，且等于给定值。
payload 中面向人的自然语言必须把内部状态翻译成业务表达，
不得照抄状态枚举、内部指标或实现术语，不得用两个同义短语机械复述一个结果。
同一面向用户的句子不得重复同一业务终态关键词来再次声明结果。
时间相关叙述必须用真正经历等待的动作、阶段或参与方作主语。\
“请求 R-1 等待已超过可用时间”把请求误作等待主体，属于错误搭配；\
“从受理到确认的等待已超过可用时间”以过程作主语，属于自然表达。
当前可见状态已经是失败或结束状态时，除非 intent 与 ActorView
明确给出重开事实，否则不得声称正在、继续或重新处理。
后续消息需说明先前结果不变时，引用先前通知即可，不得再用新的近义短语复述该终态。
每句话的动作发出者与接收对象必须符合 actor 身份；\
不得把当前 actor 正在发出的消息写成它收到的对象。
只返回满足给定完整帧 Schema 的一个完整 JSON 对象，不要 Markdown、\
代码围栏、解释或额外字段。"""

SEMANTIC_EVALUATION_SYSTEM = """\
你是独立序列语义判定器，不参与生成。根据完整、未裁剪的场景种子、\
逐事件 ActorView、事件意图、
patch、状态哈希、发布快照、逻辑等待、最终载荷和最终状态，独立判断六项语义性质。
causal_consistency：因果与状态变化一致；actor_knowledge：每个 actor 只使用其当时可知的信息；
goal_consistency：行为与 actor goal 一致；temporal_plausibility：等待与时间语义合理；
cross_frame_consistency：跨帧实体、请求与结果一致；realism：整体像真实交互。
作答前必须按时间顺序做反例优先审查，不得用未提供的隐藏理由替候选补故事：
失败或结束结果之后又声称正在、继续或重新处理，
而可见事件没有明确重开或迟到通知语义时，causal_consistency 与 realism 都必须为 false；
面向人的文本照抄状态枚举、内部指标或实现术语，
机械复述同一个结果，同一句重复终态关键词来再次声明结果，
后续消息引用已有终态又用近义短语重述它，
或跨场景呈现明显模板拼接时，realism 必须为 false。
只有在语法主语直接是请求、消息或业务实体时，\
“请求 R-1 等待已超过可用时间”这类搭配才使 temporal_plausibility 与 realism 都为 false；
“从受理到确认的等待已超过可用时间”以过程作主语，不属于该缺陷。
消息的主语、宾语或收发关系与 actor 身份相反，\
例如发件者把自己正在发出的消息当成收到的对象时，
goal_consistency 与 realism 必须为 false。
缺步骤、顺序异常或长等待本身不自动失败；
它仍必须形成可由可见状态解释、actor 不提前知情、表达自然的交互。
只有审查证据足以支持时才返回 true。
每一项只能返回 boolean。任一 false 必须加入对应闭集 reason code；\
全部 true 时 reason_codes 必须为空。
reason_codes 不得包含用户数据、实体值或自由文本。只返回 JSON，\
不要 Markdown、代码围栏、解释或额外字段。"""

NOISE_RENDER_SYSTEM = (
    "你是独立噪声事件渲染器。生成一条自然、真实，但与所有已声明任务无关且"
    "不包含可执行诉求的输入。\n"
    "不得复用任何主序列的实体、请求、票号、设备、目标、状态或措辞；"
    "不得生成任务的起点、进展或结果。\n"
    "计划噪声话题是当前 ordinal 的唯一话题；不得改换、混合或泛化为其他话题。\n"
    "生成前必须在内部列出 attempt_index + 2 个符合该话题的自然表达角度，"
    "再选择下标 attempt_index 对应的角度。\n"
    "不同 attempt 必须使用明显不同的措辞；不得输出候选表或复述内部标识。\n"
    "Schema 中的 examples 只描述形状，禁止复制或改写其内容。\n"
    "只能返回满足给定噪声帧 Schema 的一个 JSON 对象，不要 Markdown、"
    "代码围栏、解释或额外字段。"
)

NOISE_EVALUATION_SYSTEM = """\
你是独立噪声语义判定器，不参与生成。判断候选是否与全部已声明任务无关、\
是否不含可执行任务、以及是否自然真实。
候选没有忠实表达计划噪声话题，或混入其他主题时，matches_planned_topic 必须为 false，
reason_codes 必须包含 planned_noise_topic_mismatch。
四项只能返回 boolean；任一 false 必须加入对应闭集 reason code，
全部 true 时 reason_codes 必须为空。reason_codes 不得包含候选内容或自由文本。
只返回 JSON，不要 Markdown、代码围栏、解释或额外字段。"""


def scenario_seed_prompt(values: Mapping[str, object]) -> PromptBundle:
    """构造 ScenarioSeed 的冻结两消息提示词。

    @param values 完整插值字段
    @return system/user PromptBundle
    """
    user = f"""[交付槽]
mode={values['mode']}
slot_key={values['slot_key']}
source_name={values['source_name']}
scenario_index={values['scenario_index']}
attempt_index={values['attempt_index']}

[序列类别]
name={values['sequence_class']}
description={values['class_description']}

[生成指令]
{values['generation_instruction']}

[参与者约束]
{_canonical_json(values['actor_contract'])}

[状态 Schema]
{_canonical_json(values['state_schema'])}

[输出契约]
严格返回：
{{"initial_state":{{}},"actors":{{"<actor_name>":{{"goal":{{}},\
"identity":{{}},"style":{{}}}}}},"shared_facts":{{"public":{{}},\
"hidden":{{}}}},"style":{{}},"time_context":{{}}}}
字段形状必须通过随请求提供的 JSON Schema。"""
    return _prompt(SCENARIO_SEED_SYSTEM, user)


def event_plan_prompt(values: Mapping[str, object]) -> PromptBundle:
    """构造 EventPlan 的冻结两消息提示词。

    @param values 完整插值字段
    @return system/user PromptBundle
    """
    user = _event_plan_head(values) + _event_plan_tail(values)
    return _prompt(EVENT_PLAN_SYSTEM, user)


def _event_plan_head(values: Mapping[str, object]) -> str:
    """构造 EventPlan user message 的身份与闭集前半段。"""
    return f"""[尝试身份]
mode={values['mode']}
slot_key={values['slot_key']}
attempt_index={values['attempt_index']}
variation_nonce={values['variation_nonce']}

[冻结事件]
event_key={values['event_key']}
role={values['role']}
position={values['position']}
sequence_length={values['sequence_length']}
logical_time_us={values['logical_time_us']}
wait_since_previous_us={values['wait_since_previous_us']}

[生成指令]
{values['generation_instruction']}

[角色契约]
{_canonical_json(values['role_contract'])}

[可选帧类别]
{_canonical_json(values['eligible_frame_classes'])}

[可选参与者]
{_canonical_json(values['eligible_actors'])}
"""


def _event_plan_tail(values: Mapping[str, object]) -> str:
    """构造 EventPlan user message 的状态、历史与输出后半段。"""
    contract = (
        '{"frame_class":"...","actor":"...","intent":"...","patch":['
        '{"op":"test","path":"/...","value":null},'
        '{"op":"replace","path":"/...","value":null}]}'
    )
    return f"""
[ActorView]
{_canonical_json(values['actor_view'])}

[完整可见状态]
{_canonical_json(values['visible_state'])}

[完整状态 Schema]
{_canonical_json(values['state_schema'])}

[末事件 Outcome Schema]
{_canonical_json(values['outcome_schema'])}

[既有事件历史]
{_canonical_json(values['history'])}

[参与者档案]
{_canonical_json(values['actor_profiles'])}

[公开事实]
{_canonical_json(values['public_facts'])}

[输出契约]
严格返回：
{contract}
结果必须通过随请求提供的 JSON Schema，并能在当前状态副本上原子执行。"""


def frame_render_prompt(values: Mapping[str, object]) -> PromptBundle:
    """构造 FrameRenderer 的冻结两消息提示词。

    @param values 完整插值字段
    @return system/user PromptBundle
    """
    user = f"""[尝试身份]
slot_key={values['slot_key']}
attempt_index={values['attempt_index']}

[冻结事件]
event_key={values['event_key']}
role={values['role']}
position={values['position']}
frame_class={values['frame_class']}
actor={values['actor']}
logical_time_us={values['logical_time_us']}
wait_since_previous_us={values['wait_since_previous_us']}
intent={values['intent']}
patch={_canonical_json(values['patch'])}

[ActorView]
{_canonical_json(values['actor_view'])}

[公开事实]
{_canonical_json(values['public_facts'])}

[本事件发布快照]
{_canonical_json(values['publish_snapshot'])}

[状态哈希]
before={values['state_before_hash']}
after={values['state_after_hash']}

[帧生成指令]
{values['frame_instruction']}

[帧类别描述]
{values['frame_description']}

[机械绑定值]
{_canonical_json(values['binding_values'])}

[完整帧 Schema]
{_canonical_json(values['frame_schema'])}

[输出契约]
只返回一个通过完整帧 Schema 的完整 JSON object；\
机械绑定 path 的值必须与上方给定值完全相同。"""
    return _prompt(FRAME_RENDER_SYSTEM, user)


def semantic_evaluation_prompt(values: Mapping[str, object]) -> PromptBundle:
    """构造 SemanticEvaluator 的冻结两消息提示词。

    @param values 完整插值字段
    @return system/user PromptBundle
    """
    contract = (
        '{"causal_consistency":true,"actor_knowledge":true,'
        '"goal_consistency":true,"temporal_plausibility":true,'
        '"cross_frame_consistency":true,"realism":true,"reason_codes":[]}'
    )
    codes = (
        '["causal_inconsistency","actor_knowledge_violation","goal_inconsistency",'
        '"temporal_implausibility","cross_frame_inconsistency","unrealistic"]'
    )
    user = f"""[审查身份]
mode={values['mode']}
sequence_class={values['sequence_class']}
attempt_index={values['attempt_index']}

[类别描述]
{values['class_description']}

[模式或生成指令描述]
{values['pattern_description']}

[完整场景种子]
{_canonical_json(values['scenario_seed'])}

[顺序语义事件]
{_canonical_json(values['review_events'])}

[最终状态]
{_canonical_json(values['final_state'])}

[输出契约]
严格返回：
{contract}
reason_codes 只能取：
{codes}"""
    return _prompt(SEMANTIC_EVALUATION_SYSTEM, user)


def noise_render_prompt(values: Mapping[str, object]) -> PromptBundle:
    """构造 NoiseRenderer 的冻结两消息提示词。

    @param values 完整插值字段
    @return system/user PromptBundle
    """
    user = f"""[尝试身份]
event_key={values['event_key']}
noise_ordinal={values['noise_ordinal']}
attempt_index={values['attempt_index']}
frame_class={values['frame_class']}
timestamp_us={values['timestamp_us']}
session_id={values['session_id']}

[已声明序列类别]
{_canonical_json(values['class_descriptions'])}

[已声明帧类别]
{_canonical_json(values['frame_descriptions'])}

[计划噪声话题]
{values['planned_topic']}

[噪声指令]
{values['noise_instruction']}

[帧生成指令]
{values['frame_instruction']}

[噪声帧 Schema]
{_canonical_json(values['frame_schema'])}

[输出契约]
只返回一个通过噪声帧 Schema 的 JSON object。"""
    return _prompt(NOISE_RENDER_SYSTEM, user)


def noise_evaluation_prompt(values: Mapping[str, object]) -> PromptBundle:
    """构造 NoiseEvaluator 的冻结两消息提示词。

    @param values 完整插值字段
    @return system/user PromptBundle
    """
    user = f"""[审查身份]
attempt_index={values['attempt_index']}

[已声明序列类别]
{_canonical_json(values['class_descriptions'])}

[已声明帧类别]
{_canonical_json(values['frame_descriptions'])}

[计划噪声话题]
{values['planned_topic']}

[候选 payload]
{_canonical_json(values['payload'])}

[输出契约]
严格返回：
{{"unrelated_to_declared_tasks":true,"no_executable_task":true,"realism":true,\
"matches_planned_topic":true,"reason_codes":[]}}
reason_codes 只能取：
["related_to_declared_task","executable_task_present","unrealistic",\
"planned_noise_topic_mismatch"]"""
    return _prompt(NOISE_EVALUATION_SYSTEM, user)


def enforce_prompt_value_limit(
    profile: str,
    byte_limit: int,
    values: Mapping[str, object],
) -> None:
    """在派发前限制每个完整运行期动态提示值。

    string 按原始插值文本计费，其余值按与提示构造器相同的 canonical JSON 计费。
    超限值绝不裁剪，也不进入 provider 请求。

    @param profile 本次调用的 profile 名
    @param byte_limit 单个动态提示值的 UTF-8 byte 上限
    @param values 稳定字段名到完整动态值的映射
    @return None
    @raises ContextOverflowError 任一值超过冻结上限
    """
    for value in values.values():
        text = value if isinstance(value, str) else _canonical_json(value)
        if len(text.encode("utf-8")) <= byte_limit:
            continue
        _log.warning("sequence prompt value exceeds the frozen byte limit")
        raise ContextOverflowError(
            "sequence prompt value exceeds the frozen byte limit",
            phase="precheck",
            profile=profile,
        )


def _prompt(system: str, user: str) -> PromptBundle:
    """构造两条纯文本消息。"""
    return PromptBundle(messages=(
        Message(role="system", parts=(Part(kind="text", text=system),)),
        Message(role="user", parts=(Part(kind="text", text=user),)),
    ))


def _canonical_json(value: object) -> str:
    """把冻结 carrier 或 JSON 值渲染为 canonical JSON。"""
    return json.dumps(_thaw(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _thaw(value: object) -> object:
    """把 dataclass、Mapping 与 tuple 递归转换为 JSON 容器。"""
    if dataclasses.is_dataclass(value):
        return {field.name: _thaw(getattr(value, field.name))
                for field in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    return value

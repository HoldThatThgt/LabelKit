"""v1.18 生成提示词的冻结行为测试。"""

from labelkit.common.inference.generation_prompts import (
    noise_evaluation_prompt,
    noise_render_prompt,
)


def test_noise_prompt_requires_identity_driven_variation():
    """noise prompt 的完整双消息字节必须匹配冻结契约。"""
    values = {
        "event_key": "noise-event",
        "noise_ordinal": 1,
        "attempt_index": 2,
        "frame_class": "noise",
        "timestamp_us": 10,
        "session_id": "session-noise",
        "class_descriptions": {"ticket": "订票"},
        "frame_descriptions": {"noise": "闲聊"},
        "planned_topic": "手工面包出炉时的香气",
        "noise_instruction": "写一句闲聊",
        "frame_instruction": "生成 utterance",
        "frame_schema": {"type": "object"},
    }

    prompt = noise_render_prompt(values)

    system = prompt.messages[0].parts[0].text
    user = prompt.messages[1].parts[0].text
    assert system == """你是独立噪声事件渲染器。生成一条自然、真实，但与所有已声明任务无关且不包含可执行诉求的输入。
不得复用任何主序列的实体、请求、票号、设备、目标、状态或措辞；不得生成任务的起点、进展或结果。
计划噪声话题是当前 ordinal 的唯一话题；不得改换、混合或泛化为其他话题。
生成前必须在内部列出 attempt_index + 2 个符合该话题的自然表达角度，再选择下标 attempt_index 对应的角度。
不同 attempt 必须使用明显不同的措辞；不得输出候选表或复述内部标识。
Schema 中的 examples 只描述形状，禁止复制或改写其内容。
只能返回满足给定噪声帧 Schema 的一个 JSON 对象，不要 Markdown、代码围栏、解释或额外字段。"""
    assert user == """[尝试身份]
event_key=noise-event
noise_ordinal=1
attempt_index=2
frame_class=noise
timestamp_us=10
session_id=session-noise

[已声明序列类别]
{"ticket":"订票"}

[已声明帧类别]
{"noise":"闲聊"}

[计划噪声话题]
手工面包出炉时的香气

[噪声指令]
写一句闲聊

[帧生成指令]
生成 utterance

[噪声帧 Schema]
{"type":"object"}

[输出契约]
只返回一个通过噪声帧 Schema 的 JSON object。"""


def test_noise_evaluation_prompt_freezes_planned_topic_gate():
    """noise evaluator prompt 逐字节声明话题绑定。"""
    prompt = noise_evaluation_prompt({
        "attempt_index": 2,
        "class_descriptions": {"ticket": "订票"},
        "frame_descriptions": {"noise": "闲聊"},
        "planned_topic": "手工面包出炉时的香气",
        "payload": {"utterance": "面包刚出炉时闻起来很香。"},
    })
    assert prompt.messages[0].parts[0].text == """你是独立噪声语义判定器，不参与生成。判断候选是否与全部已声明任务无关、是否不含可执行任务、以及是否自然真实。
候选没有忠实表达计划噪声话题，或混入其他主题时，matches_planned_topic 必须为 false，
reason_codes 必须包含 planned_noise_topic_mismatch。
四项只能返回 boolean；任一 false 必须加入对应闭集 reason code，
全部 true 时 reason_codes 必须为空。reason_codes 不得包含候选内容或自由文本。
只返回 JSON，不要 Markdown、代码围栏、解释或额外字段。"""
    assert prompt.messages[1].parts[0].text == """[审查身份]
attempt_index=2

[已声明序列类别]
{"ticket":"订票"}

[已声明帧类别]
{"noise":"闲聊"}

[计划噪声话题]
手工面包出炉时的香气

[候选 payload]
{"utterance":"面包刚出炉时闻起来很香。"}

[输出契约]
严格返回：
{"unrelated_to_declared_tasks":true,"no_executable_task":true,"realism":true,"matches_planned_topic":true,"reason_codes":[]}
reason_codes 只能取：
["related_to_declared_task","executable_task_present","unrealistic","planned_noise_topic_mismatch"]"""

"""时间流教学工程的四类确定性 file hook。"""

from labelkit.common.contracts.types import ScenarioValidationInput, SequenceValidationInput


def validate_output(obj, record) -> list[str]:
    """校验最终序列标注对象具备教学用 intent。"""
    return [] if isinstance(obj.get("intent"), str) and obj["intent"] else ["intent must be non-empty"]


def validate_sample(text: str) -> list[str]:
    """校验生成内容不是空文本；违规样本不进入 similarity。"""
    return [] if isinstance(text, str) and text.strip() else ["sample text must be non-empty"]


def validate_sequence(value: SequenceValidationInput) -> list[str]:
    """校验序列位置连续，且从请求开始、以确认收尾。"""
    frames = value.frames
    violations: list[str] = []
    if tuple(frame.position for frame in frames) != tuple(range(len(frames))):
        violations.append("sequence positions must be contiguous")
    if not frames or frames[0].frame_class != "task_request":
        violations.append("sequence must start with task_request")
    if not frames or frames[-1].frame_class != "confirmation":
        violations.append("sequence must end with confirmation")
    return violations


def validate_scenario(value: ScenarioValidationInput) -> list[str]:
    """校验 accepted 前缀与 candidate 不共享 sequence slot。"""
    prior = {item.slot_key for item in value.accepted}
    return ["candidate slot_key already accepted"] if value.candidate.slot_key in prior else []

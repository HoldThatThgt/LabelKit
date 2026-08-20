"""时间流教学工程的确定性序列验证钩子。"""

from labelkit.common.contracts.types import SequenceValidationInput


def validate_sequence(value: SequenceValidationInput) -> list[str]:
    """校验序列位置连续，且从请求开始、以确认收尾。

    @param value LabelKit 提供的深拷贝序列视图
    @return 空列表表示通过，否则返回稳定的英文违规信息
    """
    frames = value.frames
    violations: list[str] = []
    if tuple(frame.position for frame in frames) != tuple(range(len(frames))):
        violations.append("sequence positions must be contiguous")
    if not frames or frames[0].frame_class != "task_request":
        violations.append("sequence must start with task_request")
    if not frames or frames[-1].frame_class != "confirmation":
        violations.append("sequence must end with confirmation")
    return violations

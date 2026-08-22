"""序列生成教学工程的确定性状态与输出校验钩子。"""

from __future__ import annotations

from labelkit.common.contracts.generation import StateTransitionInput


def validate_state(value: StateTransitionInput) -> list[str]:
    """阻止隐藏哨兵被事件 patch 修改或删除。

    @param value 当前候选状态转换的只读深拷贝。
    @return 空列表表示通过，否则返回稳定的违规说明。
    """
    before = value.state_before.get("hidden_sentinel")
    after = value.state_after.get("hidden_sentinel")
    return [] if before == after else ["hidden_sentinel must remain unchanged"]


def validate_output(obj: dict, record: dict | None) -> list[str]:
    """要求最终序列标注包含非空意图与闭集结果。

    @param obj 待验证的用户标注对象。
    @param record 当前序列记录的只读投影；本钩子不读取它。
    @return 空列表表示通过，否则返回稳定的违规说明。
    """
    del record
    errors: list[str] = []
    if not isinstance(obj.get("intent"), str) or not obj["intent"].strip():
        errors.append("intent must be non-empty")
    if obj.get("outcome") not in {"ticketed", "not_ticketed", "expired"}:
        errors.append("outcome must use the declared vocabulary")
    return errors

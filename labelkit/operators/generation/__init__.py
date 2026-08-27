"""v1.20 序列生成内核与平面生成辅助模块。"""

from __future__ import annotations


class GenerationAttemptRejected(Exception):
    """表示一次可恢复的 sequence slot 尝试拒绝。

    异常只携带 report 冻结的分类与 slot 身份，不携带用户数据。
    """

    def __init__(self, kind: str, slot_key: str):
        """构造一个安全的尝试拒绝。

        @param kind report rejected_attempts 的冻结键。
        @param slot_key 当前交付槽身份。
        """
        self.kind = kind
        self.slot_key = slot_key
        super().__init__(f"{kind}: slot={slot_key}")

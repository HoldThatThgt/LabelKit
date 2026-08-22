"""Sample output and flat-generation validation hooks used by offline tests."""
from __future__ import annotations


def ok(obj, record):                       # output.validator: always passes
    return []


def topic_max6(obj, record):               # output.validator: business rule
    topic = obj.get("topic", "")
    if len(topic) > 6:
        return [f"topic 须 ≤ 6 个字符，得到 {len(topic)} 个：请压缩为名词短语"]
    return []


def needs_record(obj, record):             # output.validator: uses record context
    if record is None:
        return ["record 缺失"]
    if obj.get("topic") == record.get("instruction"):
        return ["topic 不得整句复述原文"]
    return []


def sample_min10(text):                    # generate.sample_validator
    return [] if len(text) >= 10 else ["样本长度须 ≥ 10 字符"]


def boom(obj, record):                     # misbehaving hook: raises (2 positional)
    raise RuntimeError("hook exploded")


def bad_return(obj, record):               # misbehaving hook: wrong return type
    return "not-a-list"


def intent_raises(obj, record):            # synthetic 干跑通过、few-shot 干跑抛异常
    if "intent" in obj:
        raise RuntimeError("hook exploded")
    return []


NOT_CALLABLE = 42


def always_reject(obj, record):            # output.validator: unsatisfiable
    return ["该输出永远不合格（用于耗尽修复预算的测试）"]

"""Sample validation hooks used by the hook tests (spec 3.8.2 L2.5 / 3.6.2).

v1.17：本文件既被 ``<python-file>:<attribute-path>`` 文件形态装载，也被过渡期
invoke 面按 ``tests.hook_samples`` 模块导入——签名与返回值语义保持稳定。
"""
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


def sequence_ok(value):                    # generate.sequence_validator: pass
    return []


def sequence_reject(value):                # generate.sequence_validator: violation
    return ["sequence rejected"]


def sequence_mutates(value):               # generate.sequence_validator: mutate probe
    value.frames[0].payload["nested"]["value"] = "changed"
    return []


def sequence_boom(value):                  # generate.sequence_validator: exception
    raise RuntimeError("sequence hook exploded")


def sequence_bad_return(value):            # generate.sequence_validator: bad return
    return "not-a-list"

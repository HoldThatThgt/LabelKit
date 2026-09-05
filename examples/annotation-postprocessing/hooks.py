"""标注后处理示例：模型给语义，工程代码计算可复核字段。"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping


_PLATE_RE = re.compile(r"[\u4e00-\u9fff][A-Za-z](?:[\u00b7\- ]?[A-Za-z0-9]){5,6}")
_NORMAL_PLATE_RE = re.compile(r"[\u4e00-\u9fff][A-Z][A-Z0-9]{5,6}")
_SEPARATORS = frozenset({" ", "\t", "\r", "\n", "\u00b7", "-"})


def _normalize_plate(value: object) -> str:
    """规范化一个模型识别的车牌号。

    @param value 模型返回的实体值。
    @return 去分隔符并转大写的车牌号。
    @raises ValueError 值不是受支持的车牌形式。
    """
    if type(value) is not str:
        raise ValueError("plate value must be a string")
    normalized = "".join(char for char in value if char not in _SEPARATORS).upper()
    if _NORMAL_PLATE_RE.fullmatch(normalized) is None:
        raise ValueError("plate value has an invalid shape")
    return normalized


def _plate_candidates(text: str) -> list[tuple[str, int, int]]:
    """枚举原文中的车牌候选及 Unicode code point 区间。

    @param text 原始文本。
    @return 按原文位置排列的规范值、起点与终点。
    """
    return [
        (_normalize_plate(match.group(0)), match.start(), match.end())
        for match in _PLATE_RE.finditer(text)
    ]


def complete_plate_annotation(obj: dict, record: dict | None) -> dict:
    """按模型实体匹配真实原文并补齐排他位置与数量。

    @param obj 仅含模型语义字段的合格候选。
    @param record 当前输入 JSON 行的独立副本。
    @return 规范化且补齐工程字段的完整标注。
    @raises ValueError 原文或模型实体无法建立一一对应。
    """
    if not isinstance(record, Mapping) or type(record.get("text")) is not str:
        raise ValueError("plate postprocessor requires record.text")
    entities = obj.get("entities")
    if type(entities) is not list:
        raise ValueError("entities must be a list")
    candidates = _plate_candidates(record["text"])
    normalized_values = []
    for entity in entities:
        if not isinstance(entity, Mapping):
            raise ValueError("entity must be an object")
        normalized_values.append(_normalize_plate(entity.get("value")))
    if Counter(normalized_values) != Counter(item[0] for item in candidates):
        raise ValueError("model entities do not match every plate occurrence in record.text")
    used: set[int] = set()
    completed = []
    for value in normalized_values:
        index = next((i for i, item in enumerate(candidates)
                      if i not in used and item[0] == value), None)
        if index is None:
            raise ValueError("model entity occurrence cannot be located in record.text")
        used.add(index)
        normalized, start, end = candidates[index]
        completed.append({"value": normalized, "start": start, "end": end})
    completed.sort(key=lambda item: (item["start"], item["end"]))
    result = dict(obj)
    result["entities"] = completed
    result["entity_count"] = len(completed)
    return result


def complete_sequence_annotation(obj: dict, record: dict | None) -> dict:
    """规范化序列摘要并计算其 Unicode code point 长度。

    @param obj 模型生成的完整序列语义。
    @param record 序列记录必须由框架传入 None。
    @return 带 summary_length 的完整序列标注。
    @raises ValueError 上下文或摘要违反示例契约。
    """
    if record is not None:
        raise ValueError("sequence postprocessor record must be None")
    summary = obj.get("summary")
    if type(summary) is not str or not summary.strip():
        raise ValueError("sequence summary must be a non-empty string")
    result = dict(obj)
    result["summary"] = summary.strip()
    result["summary_length"] = len(result["summary"])
    return result


def complete_frame_annotation(obj: dict, record: dict | None) -> dict:
    """用模型语义和真实成员 raw 计算三个 Unicode 长度字段。

    @param obj 模型生成的当前帧语义。
    @param record 当前 primary stream row 的独立副本。
    @return 带三个派生长度的完整帧标注。
    @raises ValueError 帧语义无法与真实 payload 对齐。
    """
    payload = record.get("payload") if isinstance(record, Mapping) else None
    if not isinstance(payload, Mapping):
        raise ValueError("frame postprocessor requires record.payload")
    request_id = obj.get("request_id")
    utterance = payload.get("utterance")
    if type(request_id) is not str or request_id != payload.get("request_id"):
        raise ValueError("frame request_id differs from record.payload")
    if obj.get("observed_status") != payload.get("status"):
        raise ValueError("frame observed_status differs from record.payload")
    if type(utterance) is not str:
        raise ValueError("frame postprocessor requires record.payload.utterance")
    summary = obj.get("summary")
    if type(summary) is not str or not summary.strip():
        raise ValueError("frame summary must be a non-empty string")
    result = dict(obj)
    result["summary"] = summary.strip()
    result["summary_length"] = len(result["summary"])
    result["request_id_length"] = len(request_id)
    result["utterance_length"] = len(utterance)
    return result

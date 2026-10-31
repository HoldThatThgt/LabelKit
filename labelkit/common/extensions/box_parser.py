"""工程伴随数据钩子 ``segment.box_parser``：加载、同步函数边界、调用与参考框校验（CONTRACTS §6.4）。

LabelKit 不判断伴随数据是什么格式：钩子把原始字节转成统一参考框数组，框架只严格校验这个数组，
不裁剪、不取整、不去重、不合并。校验顺序冻结：返回类型 → 严格 JSON 复制 → 逐项形状 →
命名一致性 → 逐框值 → 补名。日志只记固定原因、框序号与钩子引用，绝不记数据。
"""
from __future__ import annotations

import inspect
import logging
import re
from pathlib import Path
from typing import NoReturn

from labelkit.common.contracts.regions import ReferenceBox
from labelkit.common.errors import BoxParserError
from labelkit.common.extensions.hooks import ResolvedHook, load_hook
from labelkit.common.extensions.postprocessing import _copy_json

_log = logging.getLogger("labelkit.box_parser")
_BOX_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_RESERVED_NAME = re.compile(r"r[1-9][0-9]*")   # 留给模型新画的多边形
_NAMED_KEYS = frozenset({"id", "bbox", "text"})
_UNNAMED_KEYS = frozenset({"bbox", "text"})


def resolve_box_parser(reference: str, project_root: Path) -> ResolvedHook:
    """加载并静态检查工程函数，绝不使用虚构业务输入调用它。

    @param reference 文件与属性引用
    @param project_root 工程根目录
    @return 已装载且签名合格的冻结函数
    @raises ValueError 装载或同步签名检查失败
    """
    try:
        hook = load_hook(reference, project_root)
    except Exception:
        _log.error("Cannot load box_parser: hook=%s", reference)
        raise ValueError(f"cannot load box_parser {reference!r}") from None
    target = hook.target
    checks = (inspect.iscoroutinefunction, inspect.isgeneratorfunction, inspect.isasyncgenfunction)
    try:
        non_sync = any(check(candidate) for candidate in (target, getattr(target, "__call__", None))
                       for check in checks)
        parameters = tuple(inspect.signature(target).parameters.values())
    except Exception:
        _signature_error(reference)
    kinds = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    if (non_sync or len(parameters) != 2 or any(item.kind not in kinds for item in parameters)
            or any(item.default is not inspect.Parameter.empty for item in parameters)):
        _signature_error(reference)
    return hook


def _signature_error(reference: str) -> None:
    """拒绝不满足恰好两个无默认值位置参数的同步函数。

    @param reference 工程函数位置
    @raises ValueError 始终抛出
    """
    _log.error("Invalid box_parser signature: hook=%s", reference)
    raise ValueError("box_parser must be synchronous with exactly two positional parameters and no defaults")


def _failure(reason: str, box_index: int | None, hook: ResolvedHook) -> NoReturn:
    """记录固定原因并抛出脱敏程序错误。

    @param reason 固定英文原因
    @param box_index 出错框的 0 基序号；不针对具体框时为 None
    @param hook 出错的工程函数
    @raises BoxParserError 始终抛出
    """
    _log.error("box_parser failed: reason=%s box_index=%s hook=%s", reason, box_index, hook.reference)
    raise BoxParserError(reason, box_index, hook.reference) from None


def invoke_box_parser(hook: ResolvedHook, companion: bytes, width: int,
                      height: int) -> tuple[ReferenceBox, ...] | None:
    """调用一次工程函数并校验返回的参考框数组。

    @param hook 已解析的工程函数
    @param companion 伴随文件原始字节，不解码、不解释
    @param width 原图存储像素宽
    @param height 原图存储像素高
    @return 校验并补名后的冻结参考框；钩子返回 None（拒收本样本）时为 None
    @raises BoxParserError 函数异常或返回契约违规
    """
    try:
        result = hook.target(companion, {"width": width, "height": height})
    except Exception:
        _failure("exception", None, hook)
    if result is None:
        return None
    if type(result) is not list:
        _failure("return_type", None, hook)
    try:
        items = _copy_json(result, True, set())
    except Exception:
        _failure("invalid_json", None, hook)
    return _validate_boxes(items, width, height, hook)


def _validate_boxes(items: list, width: int, height: int, hook: ResolvedHook) -> tuple[ReferenceBox, ...]:
    """按冻结顺序校验严格 JSON 副本：逐项形状、命名一致性、逐框值，最后补名。

    @param items 钩子返回值的严格 JSON 副本
    @param width 原图存储像素宽
    @param height 原图存储像素高
    @param hook 工程函数，仅用于错误定位
    @return 冻结参考框
    @raises BoxParserError 第一个违规框
    """
    for index, item in enumerate(items):
        if type(item) is not dict or item.keys() not in (_NAMED_KEYS, _UNNAMED_KEYS):
            _failure("invalid_box", index, hook)
    named = [item.get("id", "") != "" for item in items]
    for index, is_named in enumerate(named):
        if is_named != named[0]:
            _failure("invalid_box", index, hook)
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not _box_values_valid(item, seen, width, height):
            _failure("invalid_box", index, hook)
    return tuple(
        ReferenceBox(box_id=item["id"] if named[0] else str(index), bbox=tuple(item["bbox"]), text=item["text"])
        for index, item in enumerate(items, start=1))


def _box_values_valid(item: dict, seen: set[str], width: int, height: int) -> bool:
    """检查单框的 id、bbox 与 text；合法的已命名框写入 ``seen``。

    @param item 形状已校验的框
    @param seen 本样本已出现的框名
    @param width 原图存储像素宽
    @param height 原图存储像素高
    @return 该框是否合法
    """
    if not _bbox_valid(item["bbox"], width, height) or not _text_valid(item["text"]):
        return False
    box_id = item.get("id", "")
    if box_id == "":
        return True
    if (type(box_id) is not str or _BOX_NAME.fullmatch(box_id) is None
            or _RESERVED_NAME.fullmatch(box_id) is not None or box_id in seen):
        return False
    seen.add(box_id)
    return True


def _text_valid(text: object) -> bool:
    """检查 text：必须是字符串，且能编码为 UTF-8。

    孤立代理写不进 UTF-8 输出通道，放行会让一个样本的钩子缺陷在写出时中止整次运行。

    @param text 候选 text 值
    @return 是否合法
    """
    if type(text) is not str:
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _bbox_valid(bbox: object, width: int, height: int) -> bool:
    """检查 bbox：恰为四个数值、不含布尔，且严格落在画布内、面积为正。

    严格 JSON 复制已拒绝 NaN 与无穷，这里只需判定精确类型与范围。

    @param bbox 候选 bbox 值
    @param width 原图存储像素宽
    @param height 原图存储像素高
    @return 是否合法
    """
    if type(bbox) is not list or len(bbox) != 4 or any(type(value) not in (int, float) for value in bbox):
        return False
    left, top, right, bottom = bbox
    return 0 <= left < right <= width and 0 <= top < bottom <= height

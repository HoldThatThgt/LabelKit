"""M1 装载期的错误聚合器与 TOML 表类型化读取器(spec 3.1.5)。

本模块只承担两件事: 把整轮装载的错误/警告聚合成一份报告(``_Collector``), 以及在
单张 TOML 表上做类型化读取(``_Tbl``)——任何违规都就地记账并回落默认值, 绝不提前
抛出, 从而保证 M1 一次性报出全部错误。

错误消息格式(spec 3.1.5): ``"<file>:[section].key: <expected>, got <actual>"``,
其中 ``"<file>:[section].key:"`` 是机器稳定的定位前缀; 表数组元素以
``"[[section.key]][N]"`` 定位, N 从 1 起。
"""
from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass
from typing import Any

_logger = logging.getLogger("labelkit.config")

# 缺键哨兵: 与"键存在但值为 None"区分开(TOML 无 null, 但默认值可以是 None)。
_MISSING = object()

# rubric criterion.key 与类名的字符集约束。
_KEY_RE = re.compile(r"[a-z0-9_]+")


def _fmt(value: Any) -> str:
    """按 spec 样例的 JSON 风格渲染一个违规值。

    @param value 待渲染的值(可能是任意 TOML 标量/容器)
    @return JSON 文本; 不可序列化时(如 TOML 原生 datetime)回落 ``repr``
    """
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        # 装载期 stderr 只承载聚合报告, 此处用 debug 级别避免污染(TOML 原生
        # datetime 落此分支属常规路径, 不是故障)。
        _logger.debug("value is not JSON-serializable, rendering with repr")
        return repr(value)


@dataclass(frozen=True)
class _NumBound:
    """浮点读取的边界约束捆包(把 gt/ge/le 三个关键字收成一个参数对象)。"""

    gt: float | None = None   # 开区间下界: 要求 value > gt
    ge: float | None = None   # 闭区间下界: 要求 value >= ge
    le: float | None = None   # 闭区间上界: 要求 value <= le


# 全库仅用到的四种数值边界形态, 具名常量化以免调用点重复构造。
_GT0 = _NumBound(gt=0)                    # 正数
_GE0 = _NumBound(ge=0)                    # 非负数值
_UNIT_HALF_OPEN = _NumBound(gt=0, le=1)   # (0,1]
_UNIT_CLOSED = _NumBound(ge=0, le=1)      # [0,1]


class _Collector:
    """整轮装载的错误/警告聚合器(spec 3.1.5: 绝不"首错即抛")。"""

    def __init__(self) -> None:
        """初始化两条空账: 错误列表与警告列表。"""
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        """记一条错误(最终汇成 ConfigError, 退出码 2)。

        @param msg 已带定位前缀的英文错误消息
        """
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        """记一条警告(最终打到 stderr, 永不升级为错误)。

        @param msg 已带定位前缀的英文警告消息
        """
        self.warnings.append(msg)


class _Tbl:
    """单张 TOML 表的类型化读取器: 记录错误并回落默认值, 从不抛出。"""

    def __init__(self, col: _Collector, file: str, label: str, data: Any) -> None:
        """绑定一张表到聚合器与定位前缀。

        @param col 错误聚合器
        @param file 配置文件路径字符串(定位前缀的第一段)
        @param label 节名, 如 ``"[run]"`` / ``"[llm.default]"``; 顶层为空串
        @param data 该节的原始值; 非字典一律视为空表(类型错误由调用方上报)
        """
        self.col = col
        self.file = file
        self.label = label
        self.data: dict = data if isinstance(data, dict) else {}
        self.seen: set[str] = set()

    def loc(self, key: str) -> str:
        """拼出一个键的机器稳定定位前缀(不含末尾冒号)。

        @param key 键名
        @return ``"<file>:[section].key"`` 或顶层的 ``"<file>:key"``
        """
        return f"{self.file}:{self.label}.{key}" if self.label else f"{self.file}:{key}"

    def err(self, key: str, expected: str, got: Any = _MISSING) -> None:
        """记录一条键级错误(缺键与类型/取值错误两种形态)。

        @param key 键名
        @param expected 期望描述(英文短语)
        @param got 实到值; 省略表示"键缺失"
        """
        if got is _MISSING:
            self.col.error(f"{self.loc(key)}: missing required key, expected {expected}")
        else:
            self.col.error(f"{self.loc(key)}: expected {expected}, got {_fmt(got)}")

    def take(self, key: str) -> Any:
        """取一个键的原始值并标记为已消费(未消费的键在 finish() 里告警)。

        @param key 键名
        @return 原始值; 键缺失时返回 ``_MISSING``
        """
        self.seen.add(key)
        return self.data.get(key, _MISSING)

    def get_str(self, key: str, default: Any = None, *, required: bool = False,
                enum: tuple[str, ...] | None = None, nonempty: bool = False) -> Any:
        """读取一个字符串键(可选枚举约束与非空约束)。

        @param key 键名
        @param default 违规或缺键时的回落值
        @param required 缺键是否为错误
        @param enum 允许的取值集合; 非 None 时期望描述渲染为枚举
        @param nonempty 是否要求去空白后非空
        @return 合法值或 ``default``
        """
        if enum is not None:
            expected = " | ".join(json.dumps(e) for e in enum)
        elif nonempty:
            expected = "non-empty string"
        else:
            expected = "string"
        v = self.take(key)
        if v is _MISSING:
            if required:
                self.err(key, expected)
            return default
        if not isinstance(v, str):
            self.err(key, expected, v)
            return default
        if enum is not None and v not in enum:
            self.err(key, expected, v)
            return default
        if nonempty and not v.strip():
            self.err(key, "non-empty string", v)
            return default
        return v

    def get_int(self, key: str, default: Any = None, *,
                minimum: int | None = None) -> Any:
        """读取一个整数键(可选下界约束; bool 不算整数)。

        整数/数值/布尔三类键在全库都是可选键(缺键即取默认值), 故不设 ``required``
        ——只有 ``get_str`` 承载必填形态(如 profile 的 model / base_url)。

        @param key 键名
        @param default 违规或缺键时的回落值
        @param minimum 允许的最小值
        @return 合法值或 ``default``
        """
        if minimum == 1:
            expected = "positive integer"
        elif minimum == 0:
            expected = "non-negative integer"
        else:
            expected = "integer"
        v = self.take(key)
        if v is _MISSING:
            return default
        if isinstance(v, bool) or not isinstance(v, int) or (minimum is not None and v < minimum):
            self.err(key, expected, v)
            return default
        return v

    def get_float(self, key: str, default: Any = None, *,
                  bound: _NumBound | None = None) -> Any:
        """读取一个数值键(边界约束经 ``_NumBound`` 参数对象传入)。

        @param key 键名
        @param default 违规或缺键时的回落值
        @param bound 数值边界; None 表示只校验类型
        @return 合法的 float 或 ``default``
        """
        b = bound or _NumBound()
        if b.gt == 0 and b.le == 1:
            expected = "number in (0,1]"
        elif b.ge == 0 and b.le == 1:
            expected = "number in [0,1]"
        elif b.gt == 0:
            expected = "positive number"
        elif b.ge == 0:
            expected = "non-negative number"
        else:
            expected = "number"
        v = self.take(key)
        if v is _MISSING:
            return default
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            self.err(key, expected, v)
            return default
        f = float(v)
        if (b.gt is not None and not f > b.gt) or (b.ge is not None and not f >= b.ge) \
                or (b.le is not None and not f <= b.le):
            self.err(key, expected, v)
            return default
        return f

    def get_bool(self, key: str, default: Any = None) -> Any:
        """读取一个布尔键。

        @param key 键名
        @param default 违规或缺键时的回落值
        @return 合法值或 ``default``
        """
        v = self.take(key)
        if v is _MISSING:
            return default
        if not isinstance(v, bool):
            self.err(key, "boolean", v)
            return default
        return v

    def get_str_tuple(self, key: str, default: tuple = (), *,
                      elem_enum: tuple[str, ...] | None = None) -> tuple:
        """读取一个字符串数组键(逐元素定位报错)。

        @param key 键名
        @param default 违规或缺键时的回落值
        @param elem_enum 元素允许的取值集合
        @return 合法元素组成的元组; 任一元素违规则返回 ``default``
        """
        v = self.take(key)
        if v is _MISSING:
            return default
        if not isinstance(v, list):
            self.err(key, "string array", v)
            return default
        out: list[str] = []
        ok = True
        for i, e in enumerate(v, 1):
            if not isinstance(e, str):
                self.col.error(f"{self.loc(key)}[{i}]: expected string, got {_fmt(e)}")
                ok = False
            elif elem_enum is not None and e not in elem_enum:
                allowed = " | ".join(json.dumps(x) for x in elem_enum)
                self.col.error(f"{self.loc(key)}[{i}]: expected {allowed}, got {_fmt(e)}")
                ok = False
            else:
                out.append(e)
        return tuple(out) if ok else default

    def get_float_tuple(self, key: str, default: tuple = ()) -> tuple:
        """读取一个数值数组键(逐元素定位报错, 首个违规即回落)。

        @param key 键名
        @param default 违规或缺键时的回落值
        @return 合法元素组成的元组或 ``default``
        """
        v = self.take(key)
        if v is _MISSING:
            return default
        if not isinstance(v, list):
            self.err(key, "number array", v)
            return default
        out: list[float] = []
        for i, e in enumerate(v, 1):
            if isinstance(e, bool) or not isinstance(e, (int, float)):
                self.col.error(f"{self.loc(key)}[{i}]: expected number, got {_fmt(e)}")
                return default
            out.append(float(e))
        return tuple(out)

    def finish(self) -> None:
        """对未消费的键发前向兼容警告(永远不是错误)。"""
        for k in self.data:
            if k not in self.seen:
                self.col.warn(f"{self.loc(k)}: unknown key, ignored (forward compatibility)")


def _int_pair(t: _Tbl, key: str, default: tuple[int, int]) -> tuple[int, int]:
    """v1.13: 读取 ``[lo, hi]`` 整数闭区间(len_range 形)。

    要求长度恰 2、元素为整数、``1 <= lo <= hi``; 任一违反记录错误并返回默认值
    (聚合式, 绝不提前抛)。

    @param t 所属表读取器
    @param key 键名
    @param default 违规或缺键时的回落区间
    @return 合法区间或 ``default``
    """
    v = t.take(key)
    if v is _MISSING:
        return default
    expected = "integer range array of length 2 [lo, hi] (1 <= lo <= hi)"
    if (not isinstance(v, list) or len(v) != 2
            or any(isinstance(e, bool) or not isinstance(e, int) for e in v)):
        t.err(key, expected, v)
        return default
    lo, hi = int(v[0]), int(v[1])
    if lo < 1 or lo > hi:
        t.err(key, expected, v)
        return default
    return lo, hi


def _num_pair(t: _Tbl, key: str, default: tuple[float, float]) -> tuple[float, float]:
    """v1.13: 读取 ``[lo, hi]`` 数值闭区间(frame_gap_s 形)。

    要求长度恰 2、元素为数值、``0 < lo <= hi``；跨节上界由形态约束簇裁定：默认
    v1.15 路径（含仅 sequence_validator、无实际非零 rules/windows 前缀）要求
    ``hi < stream.gap_s``，仅实际非零 rules/windows 配额前缀的 v1.16 联合路径允许
    ``hi == stream.gap_s``。

    @param t 所属表读取器
    @param key 键名
    @param default 违规或缺键时的回落区间
    @return 合法区间或 ``default``
    """
    v = t.take(key)
    if v is _MISSING:
        return default
    expected = "number range array of length 2 [lo, hi] (0 < lo <= hi, seconds)"
    if (not isinstance(v, list) or len(v) != 2
            or any(isinstance(e, bool) or not isinstance(e, (int, float)) for e in v)):
        t.err(key, expected, v)
        return default
    lo, hi = float(v[0]), float(v[1])
    if lo <= 0 or lo > hi:
        t.err(key, expected, v)
        return default
    return lo, hi


def _section(col: _Collector, top: _Tbl, key: str) -> Any:
    """取一张顶层表: 缺省返回 None(走默认值), 类型不符记录错误。

    @param col 错误聚合器
    @param top 顶层表读取器
    @param key 顶层键名
    @return 该节的字典, 或 None
    """
    v = top.take(key)
    if v is _MISSING:
        return None
    if not isinstance(v, dict):
        col.error(f"{top.file}:{key}: expected table, got {_fmt(v)}")
        return None
    return v


def _check_schema_version(col: _Collector, top: _Tbl) -> None:
    """校验顶层 ``schema_version`` 必填且恒为 1。

    @param col 错误聚合器
    @param top 顶层表读取器
    """
    v = top.take("schema_version")
    if v is _MISSING:
        col.error(f"{top.file}:schema_version: missing required key, expected 1")
    elif isinstance(v, bool) or not isinstance(v, int) or v != 1:
        col.error(f"{top.file}:schema_version: expected 1, got {_fmt(v)}")


def _flush_warnings(col: _Collector) -> None:
    """把未知键与建议性发现作为警告打到 stderr——永不升级为错误。

    (spec 3.1.4 TOML 结构行; 装载期 M12 日志尚未配置, 故直接写 stderr。)

    @param col 错误聚合器
    """
    for w in col.warnings:
        print(f"warning: {w}", file=sys.stderr)

"""M2 — 数据摄取（spec 3.2、6.1/6.2；CONTRACTS.md §7.1）。

把 ``run.input`` 物化为惰性 ``Record`` 迭代器：

- text 模态：逐行解析 JSONL，按 ``input.text_field`` 点分路径取文本，
  确定性 id = sha256(canonical_json(raw))[:16]；
- UI 模态：递归扫描，``uitree_<index>.jsonl`` / ``image_<index>.*`` 跨子目录
  配对（共用一个 index 命名空间），按 §6.2 字段映射解析 UI 树节点，惰性
  ``ImageRef``（只校验魔数与尺寸，不解码像素），
  id = sha256(tree_bytes + image_bytes)[:16]。

坏数据遵循 input.on_bad_line / on_missing_pair / on_index_conflict
（"skip" → 计数 + trace 事件；"fail" → InputError，CLI 退出码 3）。

v1.8（stream 模式，spec 3.2.8）：``sessions()`` 暴露 M10 消费的会话流视图 ——
输入侧按 ``[stream]`` 排序（S20 时间戳解析）、按分区键的单调性校验配
``stream.on_disorder``（S19）、以及规则层会话装配器（key 变更 / gap_s /
gap_steps / session_max_len / session_max_span_s）。``scan()`` 把文本行计数与
会话干跑融合进同一遍读取（S23，``IngestPlan.session_lens``）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Literal, Mapping

from jsonpointer import JsonPointer, JsonPointerException

from labelkit.common.config._temporal import (
    paths_overlap,
    project_temporal_instance,
    resolve_frame_time_values,
)
from labelkit.common.config.model import ResolvedConfig, StreamConfig, TimeBindingSpec
from labelkit.common.errors import InputError
from labelkit.common.contracts.types import ImageRef, Record, RecordRef, UINode, UITree
from labelkit.operators.generation.project import derive_generation_id

__all__ = ["IngestPlan", "IngestReport", "Ingestor", "Session"]

# 本模块的 stderr 运行日志通道（spec §7.1：日志恒不含数据内容）
_LOGGER = logging.getLogger("labelkit.ingest")
# 日志记录附加字段：ingest 恒属批次 0（文本格式化器读取 stage / batch）
_LOG_EXTRA: dict[str, Any] = {"stage": "ingest", "batch": 0}


# ── 文件名模式（spec 3.2.4；扩展名匹配大小写不敏感）────────────────────────
_TREE_RE = re.compile(r"^uitree_(\d+)\.(?i:jsonl)$")
_IMAGE_RE = re.compile(r"^image_(\d+)\.(?i:png|jpg|jpeg)$")

# 图像魔数（spec 3.2.4：仅校验魔数与尺寸，不解码全图）
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"

# ── §6.2 字段映射：接受的源字段名，按优先级排列 ────────────────────────────
_NODE_ID_KEYS = ("id", "node_id")
_PARENT_KEYS = ("parent", "parent_id")
_ROLE_KEYS = ("class", "className", "type", "role")
_TEXT_KEYS = ("text", "label")
_DESC_KEYS = ("content_desc", "contentDescription", "desc")
_BOUNDS_KEYS = ("bounds",)
_VISIBLE_KEYS = ("visible", "visible_to_user")

_BOUNDS_STR_RE = re.compile(
    r"^\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\s*$"
)
_GENERATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_GENERATION_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}[+-]\d{2}:\d{2}$"
)
_PRIMARY_EVENT_FIELDS = frozenset({
    "event_id", "event_key", "owner_sequence_id", "role", "frame_class",
    "actor", "logical_time_us", "timestamp", "duration_us", "resources", "time_bindings",
})
_REPLAY_EVENT_FIELDS = frozenset({
    *_PRIMARY_EVENT_FIELDS, "replay_sequence_id", "replay_ordinal",
    "duplicate_of_sequence_id", "duplicate_of_event_id",
})
_NOISE_EVENT_FIELDS = frozenset({*_PRIMARY_EVENT_FIELDS, "noise"})
_PRIMARY_META_FIELDS = (
    frozenset({"event", "generation", "classification"}),
    frozenset({"event", "generation", "classification", "annotation"}),
)
_DECLARED_GENERATION_FIELDS = frozenset({
    "validation_mode", "actor_knowledge_validation", "scenario_set", "scenario_index",
    "scenario_id", "world_branch_id", "sequence_class", "pattern", "variant",
})
_INSTRUCTION_GENERATION_FIELDS = frozenset({
    "validation_mode", "actor_knowledge_validation", "instruction_slot", "scenario_index",
    "scenario_id", "world_branch_id", "sequence_class",
})
_FRAME_TIME_SOURCES = frozenset({
    "event_start_milliseconds",
    "event_end_milliseconds",
    "event_duration_milliseconds",
    "event_start_iso8601",
    "event_end_iso8601",
})
_RESOURCE_RE = re.compile(r"^[a-z0-9_]+$")


def _canonical_json(obj: Any) -> str:
    """规范 JSON 序列化（spec 3.2.5：sort_keys、ensure_ascii=False、紧凑分隔符）。

    @param obj 任意可 JSON 序列化的对象
    @return 规范化 JSON 字符串（记录 id 的哈希输入）
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _text_record_id(raw: Mapping) -> str:
    """计算文本模态记录 id（spec 3.2.5：整行 raw 的规范 JSON 摘要前 16 位十六进制）。

    @param raw 整行解析出的 JSON object
    @return 确定性记录 id
    """
    return hashlib.sha256(_canonical_json(raw).encode("utf-8")).hexdigest()[:16]


def _generation_timestamp_us(value: object) -> int:
    """严格解析生成工件的带 offset 六位微秒时间戳。

    @param value `_meta.event.timestamp`。
    @return UTC epoch 微秒。
    @raises ValueError 格式不符合冻结工件时间。
    """
    if not isinstance(value, str) or _GENERATION_TIMESTAMP_RE.fullmatch(value) is None:
        raise ValueError("invalid artifact timestamp")
    parsed = datetime.fromisoformat(value).astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed - epoch
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _generation_offset_minutes(value: object) -> int:
    """从已通过工件格式检查的时间文本提取整分钟 offset。

    @param value `_meta.event.timestamp`。
    @return UTC offset 分钟。
    @raises ValueError offset 不存在或不是整分钟。
    """
    parsed = datetime.fromisoformat(str(value))
    offset = parsed.utcoffset()
    seconds = None if offset is None else offset.total_seconds()
    if seconds is None or seconds % 60:
        raise ValueError("artifact timestamp offset must use whole minutes")
    return int(seconds // 60)


def _artifact_time_bindings(value: object) -> tuple[TimeBindingSpec, ...]:
    """解析自描述 stream 中声明序封闭的 frame time bindings。"""
    if not isinstance(value, list):
        raise ValueError("generation time bindings must be an array")
    bindings: list[TimeBindingSpec] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"payload_path", "source"}:
            raise ValueError("generation time binding shape is invalid")
        path, source = item.get("payload_path"), item.get("source")
        _validate_artifact_time_path(path, bindings)
        if source not in _FRAME_TIME_SOURCES:
            raise ValueError("generation time binding source is invalid")
        bindings.append(TimeBindingSpec(str(path), source))
    return tuple(bindings)


def _validate_artifact_time_path(path: object, bindings: list[TimeBindingSpec]) -> None:
    """拒绝非法、重复或互为前缀的 artifact binding path。"""
    try:
        parts = tuple(JsonPointer(path).parts) if isinstance(path, str) else ()
    except JsonPointerException as error:
        raise ValueError("generation time binding path is invalid") from error
    if not parts or "-" in parts:
        raise ValueError("generation time binding path is invalid")
    if any(paths_overlap(str(path), item.payload_path) for item in bindings):
        raise ValueError("generation time binding paths conflict")


def _artifact_resources(value: object, duration_us: object) -> tuple[str, ...]:
    """解析 generation event 的声明序容量一资源。"""
    valid_duration = (isinstance(duration_us, int) and not isinstance(duration_us, bool)
                      and duration_us >= 0 and duration_us % 1000 == 0)
    if not valid_duration:
        raise ValueError("generation event duration is invalid")
    if not isinstance(value, list):
        raise ValueError("generation event resources are invalid")
    if not all(isinstance(item, str) and _RESOURCE_RE.fullmatch(item) for item in value):
        raise ValueError("generation event resources are invalid")
    if len(set(value)) != len(value):
        raise ValueError("generation event resources are invalid")
    if value and duration_us == 0:
        raise ValueError("generation event resources require positive duration")
    return tuple(value)


def _validate_artifact_payload_time(row: "_GenerationInputRow") -> None:
    """复算一行 descriptor 的全部机械值并逐路径比较 payload。"""
    offset = _generation_offset_minutes(row.event.get("timestamp"))
    values = resolve_frame_time_values(
        row.time_bindings, row.timestamp_us, row.duration_us, offset)
    payload = row.raw["payload"]
    for path, expected in values.items():
        try:
            actual = JsonPointer(path).resolve(payload)
        except JsonPointerException as error:
            raise ValueError("generation payload time path is missing") from error
        if type(actual) is not type(expected) or actual != expected:
            raise ValueError("generation payload time value is invalid")


def _extract_text_field(obj: Mapping, dotted_path: str) -> str | None:
    """按点分路径取文本（spec 3.2.5）。

    命中字符串 → 原样返回；命中数组 / object（或任何其它非 null 的 JSON 值）
    → 取规范 JSON 序列化；缺键 / null / 中间层非 Mapping → 未命中。

    @param obj 待取值的 JSON object
    @param dotted_path 点分路径（如 "meta.text"）
    @return 命中的文本；未命中返回 None
    """
    cur: Any = obj
    for part in dotted_path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    if cur is None:
        return None
    if isinstance(cur, str):
        return cur
    return _canonical_json(cur)


# ── v1.8 stream 模式：时间戳解析（S20，spec 6.1）─────────────────────────────

_MISS = object()  # raw 取值未命中标记（字面 None 值同样算解析失败）


def _lookup_raw(obj: Mapping | None, dotted_path: str) -> Any:
    """在 Record.raw 上做原始点分路径取值（路径语义同 input.text_field，spec 3.2.8）。

    @param obj 记录的 raw object（可为 None）
    @param dotted_path 点分路径
    @return 原始值；路径未命中返回哨兵 _MISS
    """
    cur: Any = obj
    for part in dotted_path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return _MISS
        cur = cur[part]
    return cur


def _numeric_order_key(v: float) -> float | None:
    """S20 数值规则：v<0 ∨ v≥1e14 判失败；v<1e11 判 epoch 秒；
    1e11≤v<1e14 判 epoch 毫秒（÷1000）。NaN 落不进任何区间 ⇒ 失败。

    @param v 待判定的数值
    @return 内部序键（float 秒）；解析失败返回 None
    """
    if math.isnan(v) or v < 0 or v >= 1e14:
        return None
    if v < 1e11:
        return float(v)
    return v / 1000.0


def _parse_order_key(value: Any) -> float | None:
    """S20：把 stream.order_by="meta:<字段>" 的取值解析为内部序键（float epoch 秒）。

    数值（bool 除外 —— JSON true/false 不是时间戳）走数值规则；字符串先按数值
    规则试 float()，再试 datetime.fromisoformat（Python 3.11+ 原生接受 Z 后缀）：
    aware 值取 .timestamp()，naive 值按 UTC 解释。

    @param value 时间戳字段的原始取值
    @return 内部序键（float 秒）；解析失败返回 None
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _numeric_order_key(float(value))
    if isinstance(value, str):
        try:
            return _numeric_order_key(float(value))
        except ValueError:
            # 非纯数字字符串是正常分支：继续试 ISO-8601（日志不含取值本身）
            _LOGGER.debug("order key is not numeric, trying ISO-8601", extra=_LOG_EXTRA)
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            _LOGGER.debug("order key is neither numeric nor ISO-8601", extra=_LOG_EXTRA)
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def _clip(value: Any, limit: int = 120) -> str:
    """把时间戳源值裁成有界字符串，用于 reason 文案
    （时间戳自身的值在 reason 中是被允许的 —— S20 / spec 7.2）。

    @param value 时间戳源值
    @param limit 保留的最大字符数
    @return 裁剪后的字符串（超长时尾部补省略号）
    """
    s = str(value)
    return s if len(s) <= limit else s[:limit] + "…"


@dataclass(frozen=True)
class IngestPlan:
    """扫描计划（不解析数据）：文件清单、UI 配对表、预估记录数、会话干跑长度。"""
    files: tuple[str, ...]                     # 文本：.jsonl 文件（按文件名字典序）；
                                               # UI：全部命中文件，每对先树后图、按 index
                                               # 升序。路径相对 run.input（即
                                               # RecordRef.source_file）
    pairs: tuple[tuple[int, str, str], ...]    # UI 配对表（spec 3.2.3 配对表）：
                                               # (index, 树路径, 图路径)，按 index 升序；
                                               # 文本模态恒为 ()
    estimated_records: int                     # 文本：总行数（廉价计数）；UI：len(pairs)
    session_lens: tuple[int, ...] = ()         # v1.8（S23）：供 dry-run next-fit 装箱用的
                                               # 会话干跑长度；estimate=False 或
                                               # segment.enabled=False 时为 ()


@dataclass
class IngestReport:
    """摄取账本：计数与坏数据位置（只含计数与定位，不含数据内容，spec §6.4）。"""
    scanned: int = 0                           # 已看过的行数 / pair index 数
    ingested: int = 0                          # 已产出的合法记录数
    bad_input: int = 0                         # 坏行 + 跳过的 index 冲突 + 缺对
                                               # （v1.8：+ 跳过的乱序记录）
    missing_pair: int = 0                      # 仅 UI 模态：缺对数
    index_conflict: int = 0                    # 仅 UI 模态：index 冲突数
    sessions: int = 0                          # v1.8：装配器闭合的候选会话数
                                               # （仅 stream 模式）
    disorder: int = 0                          # v1.8：被单调性校验跳过的记录数（乱序或
                                               # 时间戳解析失败；是 bad_input 的子集，S20）
    # 坏数据位置表：{"file": str, "line_no": int|None, "index": int|None, "reason": str}
    bad_locations: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class Session:
    """v1.8 会话（CONTRACTS §7.1 [FROZEN THERE]）：M10 消费的会话流单元。"""
    session_id: str                            # sha256("\n".join(成员记录 id))[:16]，
                                               # 成员按会话顺序参与拼接
    records: tuple[Record, ...]                # 会话成员，按会话（序键）顺序
    # 会话闭合原因（spec 3.2.8 / S17 词表；= segment.session 事件 payload 的 cause）
    cause: Literal["gap", "key", "max_len", "max_span", "eof", "limit"]


@dataclass(frozen=True)
class _UIScan:
    """UI 全量扫描结果（内部）：命中配对 + 两类异常，全部按 index 组织。"""
    pairs: tuple[tuple[int, str, str], ...]            # 命中配对，按 index 升序
    conflicts: tuple[tuple[int, tuple[str, ...]], ...]  # (index, 冲突文件组)，按 index 升序
    missing: tuple[tuple[int, str, str], ...]           # (index, 在场侧 "tree"|"image", 文件)


@dataclass(frozen=True)
class _GenerationInputRow:
    """已解析但尚未信任身份的 generation stream 行。"""

    raw: Mapping[str, object]                    # 完整 JSON object 行
    source_file: str                             # 相对输入文件名
    line_no: int                                 # 一基文件内行号
    event: Mapping[str, object]                  # `_meta.event` 对象
    timestamp_us: int                            # 严格解析后的 UTC epoch 微秒
    duration_us: int                             # descriptor 固定非负毫秒量化时长
    resources: tuple[str, ...]                   # descriptor 声明序容量一资源
    time_bindings: tuple[TimeBindingSpec, ...]   # descriptor 声明序 frame binding
    exact_dedup_text: str                        # 删除已验证 time paths 的 payload canonical JSON


class _Assembler:
    """规则层会话闭合状态机（spec 3.2.8），由 sessions()（真实运行）与 scan()
    （S23 干跑彩排）共用。只维护规则状态 —— 不持有 Record，缓冲由调用方自理。

    每帧调用方先调 pre_close()（返回让当前开放会话在本帧加入前闭合的原因，
    或 None），再调 feed()（把本帧计入；True = 触达 session_max_len，按
    cause="max_len" 硬闭合）。"""

    def __init__(self, scfg: StreamConfig, *, text: bool, meta: bool):
        """构造会话状态机。

        @param scfg [stream] 配置节
        @param text 是否文本模态（gap_steps 按同文件 line_no 计）
        @param meta order_by 是否为 "meta:<字段>"（决定 gap_s / max_span 是否生效）
        """
        self._scfg = scfg
        self._text = text                     # 文本模态（gap_steps = 同文件 line_no 差）
        self._meta = meta                     # order_by = "meta:<字段>"（gap_s / max_span 生效）
        self.length = 0                       # 当前开放会话内的帧数
        self._boundary: tuple | None = None   # 当前会话的边界键
        self._first_key: float | None = None  # 会话首帧序键（算 max_span 用）
        self._prev_key: float | None = None   # 上一帧序键（算 gap_s 用）
        self._prev_step: int | None = None    # 上一帧步号：line_no（文本）/ pair_index（UI）
        self._prev_file: str | None = None    # 上一帧来源文件（文本模态判同文件用）

    def pre_close(self, boundary: tuple, order_key: float | None,
                  step: int | None, source_file: str) -> str | None:
        """判定本帧加入前是否需要先闭合当前开放会话。

        @param boundary 本帧的边界键
        @param order_key 本帧序键（无 meta 排序时为 None）
        @param step 本帧步号：line_no（文本）/ pair_index（UI）
        @param source_file 本帧来源文件（相对路径）
        @return 闭合原因（"key" | "gap" | "max_span"）；无需闭合返回 None
        """
        if self.length == 0:
            return None
        if boundary != self._boundary:
            return "key"
        s = self._scfg
        if (self._meta and order_key is not None and self._prev_key is not None
                and order_key - self._prev_key > s.gap_s):
            return "gap"
        # gap_steps：UI 取 pair_index 差；文本取「同一文件内」的 line_no 差
        # （行号逐文件重置 —— meta:* 排序下文件边界是透明的，故跨文件相邻跳过本检查）
        if (s.gap_steps > 0 and step is not None and self._prev_step is not None
                and (not self._text or source_file == self._prev_file)
                and step - self._prev_step > s.gap_steps):
            return "gap"
        if (self._meta and s.session_max_span_s > 0 and order_key is not None
                and self._first_key is not None
                and order_key - self._first_key > s.session_max_span_s):
            return "max_span"
        return None

    def feed(self, boundary: tuple, order_key: float | None,
             step: int | None, source_file: str) -> bool:
        """把本帧计入当前开放会话。

        @param boundary 本帧的边界键
        @param order_key 本帧序键（无 meta 排序时为 None）
        @param step 本帧步号：line_no（文本）/ pair_index（UI）
        @param source_file 本帧来源文件（相对路径）
        @return True 表示已触达 session_max_len，调用方须按 cause="max_len" 硬闭合
        """
        if self.length == 0:
            self._boundary = boundary
            self._first_key = order_key
        self.length += 1
        self._prev_key = order_key
        self._prev_step = step
        self._prev_file = source_file
        return 0 < self._scfg.session_max_len <= self.length

    def reset(self) -> None:
        """清空开放会话状态，准备装配下一个会话。

        @return 无
        """
        self.length = 0
        self._boundary = None
        self._first_key = None


@dataclass
class _TextRehearsal:
    """S23 文本会话干跑的可变状态（纯计数彩排：不产 Record、不发事件、不改账本）。"""
    asm: _Assembler                  # 规则层会话状态机
    meta_field: str | None           # stream.order_by 的 meta 字段名（None = input_order）
    text_field: str                  # input.text_field 点分路径
    limit: int | None                # 帧级 --limit 预算（None = 不限）
    cursors: dict[tuple, float] = field(default_factory=dict)   # 分区键 → 单调性游标
    lens: list[int] = field(default_factory=list)               # 已闭合会话的长度
    estimated: int = 0               # 已计入的非空行数
    frames: int = 0                  # 已喂给装配器的帧数（= islice 单位）


def _parse_text_line(line_bytes: bytes,
                     text_field: str) -> tuple[Any, str | None, str | None]:
    """解析一行文本输入（spec 6.1 / 3.2.5）：UTF-8 严格解码 → JSON → object 判定 →
    按 input.text_field 取文本。

    @param line_bytes 原始行字节
    @param text_field input.text_field 点分路径
    @return (raw object, 提取出的文本, 坏行原因)；坏行时前两项恒为 None
    """
    try:
        line = line_bytes.decode("utf-8")
    except UnicodeDecodeError:
        _LOGGER.debug("input line is not valid UTF-8", extra=_LOG_EXTRA)
        return None, None, "line is not valid UTF-8"
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        _LOGGER.debug("input line is not valid JSON: %s", exc.msg, extra=_LOG_EXTRA)
        return None, None, f"JSON parse failure: {exc.msg}"
    if not isinstance(raw, dict):
        return None, None, "JSON line is not an object"
    text = _extract_text_field(raw, text_field)
    if text is None:
        return None, None, f'input.text_field "{text_field}" missed'
    return raw, text, None


def _rehearse_parse(line_bytes: bytes, text_field: str) -> dict | None:
    """S23 干跑用的行解析：只判「records() 会不会产出这一帧」，不产 Record。

    @param line_bytes 原始行字节
    @param text_field input.text_field 点分路径
    @return 解析出的 JSON object；真实运行会跳过这一行时返回 None
    """
    try:
        raw = json.loads(line_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _LOGGER.debug("session dry-run skipped a line that is not decodable JSON",
                      extra=_LOG_EXTRA)
        return None
    if not isinstance(raw, dict):
        return None
    if _extract_text_field(raw, text_field) is None:
        return None
    return raw


def _input_root(cfg: ResolvedConfig) -> Path | None:
    """v1.17（SPEC-SP §5.1）：从 M1 冻结的 ResolvedPaths 取输入根。

    @param cfg 已解析配置
    @return 输入根路径；generate_only 形态（``paths.input`` 为 None）为 None
    @raises ValueError ``cfg.paths`` 缺席——M2 只消费 M1 派生的绝对路径，
        绝不静默回落按 cwd 重解 ``run.input``
    """
    if cfg.paths is None:
        raise ValueError("ResolvedConfig.paths is None: ingest consumes "
                         "M1-derived absolute paths only (no cwd fallback)")
    return Path(cfg.paths.input) if cfg.paths.input else None


class Ingestor:
    """M2 摄取器。不是 Stage —— 它没有 ctx；CLI / 编排层在调用 ``records()`` 前
    设置公开属性 ``ingestor.metrics``（默认 None），使摄取期 trace 事件以
    batch_no=0 发出。"""

    def __init__(self, cfg: ResolvedConfig):
        """构造摄取器。

        v1.17（SPEC-SP §5.1）：输入根只消费 M1 冻结的 ``cfg.paths.input``——
        不再从 ``run.input`` 字符串做 cwd 二次推导，也不静默回落。

        @param cfg M1 解析冻结后的运行配置
        @raises ValueError ``cfg.paths`` 缺席（直接构造 ResolvedConfig 的旧
            fixture 面）
        """
        self._cfg = cfg
        self._root = _input_root(cfg)
        self._report = IngestReport()
        self.metrics = None  # MetricsSink | None，由外部接线（CONTRACTS §7.1）
        self._disorder_warned = False  # 全运行仅一条 stderr WARN（spec 7.2、S19）
        self._session_id_seen: dict[str, int] = {}  # D2：运行内 session_id 撞号守卫

    @property
    def report(self) -> IngestReport:
        """读取摄取账本。

        @return 本次摄取的 IngestReport（计数与坏数据位置）
        """
        return self._report

    # ── 扫描 ────────────────────────────────────────────────────────────────

    def scan(self, *, estimate: bool = True) -> IngestPlan:
        """只扫描不解析：文件清单、配对表、预估记录数。

        供 --dry-run、`validate` 与编排层 P2-4 预扫描使用。``estimate=False``
        跳过文本模态的行计数（该计数会读完每一个输入字节）—— 预扫描只需要快速
        失败检查，不需要预估值，也不应把输入 I/O 翻倍。

        v1.8（S23）：segment.enabled 且 estimate=True 时，计划另带
        ``session_lens`` —— 在同一遍读取（文本）或配对表（UI，零额外 I/O）上做
        会话干跑。纯计数彩排：不产 Record、不发事件、不改账本；坏行与乱序记录
        的跳过方式与真实 skip 策略运行完全一致。

        @param estimate 是否计算预估记录数与会话干跑长度
        @return 扫描计划 IngestPlan
        @raises InputError run.input 缺失/不可读，或 UI 配对异常命中 "fail" 策略
        """
        root = self._require_root()
        stream_mode = self._cfg.segment.enabled
        if self._cfg.run.modality == "text":
            return self._scan_text(root, estimate=estimate, stream_mode=stream_mode)
        return self._scan_ui_plan(root, estimate=estimate, stream_mode=stream_mode)

    def _scan_text(self, root: Path, *, estimate: bool, stream_mode: bool) -> IngestPlan:
        """文本模态扫描分支：文件清单 +（可选）行计数与会话干跑。

        @param root run.input 根路径
        @param estimate 是否计算预估记录数
        @param stream_mode 是否 stream 模式（segment.enabled）
        @return 扫描计划 IngestPlan
        @raises InputError 输入文件不可读
        """
        files = self._text_files(root)
        generation_rows: tuple[_GenerationInputRow, ...] | None = None
        if self._is_generation_stream(root, files):
            try:
                generation_rows = self._read_generation_rows(root, files, count_report=False)
                self._validate_generation_rows(generation_rows)
            except (KeyError, TypeError, ValueError) as exc:
                self._generation_input_failure(str(exc))
        estimated = 0
        session_lens: tuple[int, ...] = ()
        if estimate and stream_mode:
            estimated, session_lens = self._fused_text_scan(root, files)
        elif estimate:
            estimated = (len(generation_rows) if generation_rows is not None
                         else self._count_text_lines(root, files))
        return IngestPlan(files=tuple(files), pairs=(), estimated_records=estimated,
                          session_lens=session_lens)

    def _count_text_lines(self, root: Path, files: list[str]) -> int:
        """统计文本输入的非空行数（预估记录数的廉价口径）。

        @param root run.input 根路径
        @param files 相对文件名清单
        @return 非空行总数
        @raises InputError 输入文件不可读
        """
        estimated = 0
        for rel in files:
            path = root / rel if root.is_dir() else root
            try:
                with path.open("rb") as fh:
                    estimated += sum(1 for line in fh if line.strip())
            except OSError as exc:
                raise InputError(f"cannot read input file {path}: {exc}") from exc
        return estimated

    def _scan_ui_plan(self, root: Path, *, estimate: bool, stream_mode: bool) -> IngestPlan:
        """UI 模态扫描分支：配对表 + 两类异常的 fail 策略快速失败。

        @param root run.input 根目录
        @param estimate 是否计算会话干跑长度
        @param stream_mode 是否 stream 模式（segment.enabled）
        @return 扫描计划 IngestPlan
        @raises InputError 配对异常命中 "fail" 策略
        """
        ui = self._scan_ui(root)
        self._scan_fail_fast(ui)
        plan_files: list[str] = []
        for _, tree, image in ui.pairs:
            plan_files.append(tree)
            plan_files.append(image)
        session_lens = (self._rehearse_ui_sessions(ui.pairs)
                        if estimate and stream_mode else ())
        return IngestPlan(files=tuple(plan_files), pairs=ui.pairs,
                          estimated_records=len(ui.pairs),
                          session_lens=session_lens)

    def _scan_fail_fast(self, ui: _UIScan) -> None:
        """扫描期两类配对异常的 fail 策略快速失败（先 index 冲突，后缺对）。

        缺对分支与冲突分支共用同一条快速失败契约（P2-4 评审）：命中 "fail" 的
        运行必须死在这里 —— 早于 run.start 打开（并截断）上一次运行的 trace 文件。

        @param ui UI 全量扫描结果
        @return 无
        @raises InputError index 冲突或缺对命中 "fail" 策略
        """
        if ui.conflicts and self._cfg.input.on_index_conflict == "fail":
            index, files_ = ui.conflicts[0]
            self._emit("ingest.index_conflict", {"index": index, "files": list(files_)})
            self._stderr_fallback(
                "ingest.index_conflict index=%s files=%s", index, list(files_))
            raise InputError(
                f"UI index conflict: index={index} matches multiple files {list(files_)}"
                f" (input.on_index_conflict = \"fail\")"
            )
        if ui.missing and self._cfg.input.on_missing_pair == "fail":
            index, present, file_ = ui.missing[0]
            self._emit("ingest.missing_pair",
                       {"index": index, "present": present, "file": file_})
            self._stderr_fallback(
                "ingest.missing_pair index=%s present=%s file=%s", index, present, file_)
            raise InputError(
                f"UI missing pair: index={index} has only the {present} side ({file_})"
                f" (input.on_missing_pair = \"fail\")"
            )

    # ── 记录流 ──────────────────────────────────────────────────────────────

    def records(self) -> Iterator[Record]:
        """惰性 Record 流。

        解析错误遵循 input.on_bad_line / on_missing_pair / on_index_conflict
        （"skip" → 计数 + trace 事件；"fail" → 抛 InputError）。若整条流耗尽后
        没有任何合法记录，抛 InputError（"no valid records"，spec §2.4 → 退出码
        3）—— 什么都产不出的运行是输入错误，不是成功。

        @return Record 迭代器
        @raises InputError 命中 "fail" 策略，或全流零合法记录
        """
        root = self._require_root()
        if self._cfg.run.modality == "text":
            yield from self._text_records(root)
        else:
            yield from self._ui_records(root)
        if self._report.ingested == 0:
            r = self._report
            raise InputError(
                f"no valid records: {root} (scanned={r.scanned} bad_input={r.bad_input}"
                f" missing_pair={r.missing_pair} index_conflict={r.index_conflict})"
            )

    # ── v1.8 stream 模式：会话流视图（spec 3.2.8、CONTRACTS §7.1）───────────

    def sessions(self) -> Iterator[Session]:
        """v1.8（stream 模式）：M10 用来替代 records() 的会话流视图。

        管线：解析流（= records() 语义，含 stream.order_by 排序与按分区键的单调性
        校验 stream.on_disorder，S19/S20）→ 帧级 --limit islice 落在解析流与装配器
        「之间」（S17；limit 单位恒为帧，绝不是会话）→ 规则层会话装配器（stream.key
        变更 / gap_s / gap_steps / session_max_len / session_max_span_s，任一触发即
        闭合）。每闭合一个会话发一条 `segment.session` trace 事件（属主 M2；segment.*
        前缀把它路由到 segment 通道，S1）并累加 IngestReport.sessions；--limit 截断
        按 EOF 处理，未闭合的尾会话以 cause="limit" 冲刷 + 一条 stderr WARN（S17）。

        @return Session 迭代器
        @raises InputError 命中 "fail" 策略（含 stream.on_disorder = "fail"）
        """
        meta_field = self._meta_field()
        modality = self._cfg.run.modality
        asm = _Assembler(self._cfg.stream, text=modality == "text",
                         meta=meta_field is not None)
        cursors: dict[tuple, float] = {}   # 按分区键各自维护的单调性游标（S19）
        buf: list[tuple[Record, float | None]] = []   # (记录, 序键)
        stream, limit = self._limited_record_stream()

        consumed = 0
        for rec in stream:
            consumed += 1
            order_key: float | None = None
            if meta_field is not None:
                order_key = self._order_key_for(rec, meta_field)
                if order_key is None:
                    continue
            part_key, boundary = self._stream_keys(rec.raw, rec.ref.source_file)
            if (meta_field is not None
                    and not self._monotonic_ok(rec, part_key, order_key, cursors)):
                continue
            step = rec.ref.pair_index if modality == "ui" else rec.ref.line_no
            cause = asm.pre_close(boundary, order_key, step, rec.ref.source_file)
            if cause is not None:
                yield self._close_session(buf, cause)
                buf = []
                asm.reset()
            buf.append((rec, order_key))
            if asm.feed(boundary, order_key, step, rec.ref.source_file):
                yield self._close_session(buf, "max_len")
                buf = []
                asm.reset()

        if buf:
            yield self._flush_tail(buf, limit is not None and consumed == limit)

    def _limited_record_stream(self) -> tuple[Iterator[Record], int | None]:
        """构造帧级 --limit 截断后的解析流（S17：limit 单位恒为帧，绝不是会话）。

        @return (记录迭代器, 帧预算 limit；未设 --limit 时预算为 None)
        """
        limit = self._cfg.limit
        stream: Iterator[Record] = self.records()
        if limit is not None:
            stream = islice(stream, limit)
        return stream, limit

    def _order_key_for(self, rec: Record, meta_field: str) -> float | None:
        """取本帧序键；解析失败按 stream.on_disorder 处置（S20）。

        @param rec 当前记录
        @param meta_field stream.order_by = "meta:<字段>" 声明的字段名
        @return 序键（float 秒）；解析失败返回 None（记录已按策略处置）
        @raises InputError stream.on_disorder = "fail"
        """
        raw_value = _lookup_raw(rec.raw, meta_field)
        order_key = None if raw_value is _MISS else _parse_order_key(raw_value)
        if order_key is None:
            detail = ("field missing" if raw_value is _MISS
                      else f"value {_clip(raw_value)} is unparseable")
            self._disorder(rec, f"timestamp parse failure: meta:{meta_field} {detail}")
        return order_key

    def _monotonic_ok(self, rec: Record, part_key: tuple, order_key: float,
                      cursors: dict[tuple, float]) -> bool:
        """按分区键做单调性校验，通过则推进该分区游标（S19）。

        @param rec 当前记录
        @param part_key 本帧分区键
        @param order_key 本帧序键
        @param cursors 分区键 → 游标的可变映射（就地推进）
        @return True 表示单调，False 表示已按 on_disorder 跳过
        @raises InputError stream.on_disorder = "fail"
        """
        cursor = cursors.get(part_key)
        if cursor is not None and order_key < cursor:
            self._disorder(
                rec, f"out of order: timestamp {order_key} is below partition cursor {cursor}")
            return False
        cursors[part_key] = order_key
        return True

    def _flush_tail(self, buf: list[tuple[Record, float | None]],
                    at_budget: bool) -> Session:
        """冲刷未闭合的尾会话。

        cause="limit" 陈述的是一个事实（--limit 预算恰在此闭合点耗尽）；其后是否
        还有输入，不多拉一条并解析就无从得知，而多拉一条会扰动
        scanned/bad_input 账本（D3）。因此 WARN 报告的是预算耗尽，而非断言截断。

        @param buf 尾会话缓冲：(记录, 序键) 列表
        @param at_budget --limit 预算是否恰在此耗尽
        @return 闭合后的 Session（cause = "limit" 或 "eof"）
        """
        session = self._close_session(buf, "limit" if at_budget else "eof")
        if at_budget:
            _LOGGER.warning(
                "tail session closed where the --limit budget was exhausted "
                "(cause=limit; whether more input followed is unknown) "
                "session_id=%s len=%s",
                session.session_id, len(session.records), extra=_LOG_EXTRA)
        return session

    def _meta_field(self) -> str | None:
        """解析 stream.order_by 中的 meta 字段名。

        @return "meta:<字段>" 的字段名；非 meta 排序返回 None
        """
        order_by = self._cfg.stream.order_by
        return order_by[len("meta:"):] if order_by.startswith("meta:") else None

    def _stream_keys(self, raw: Mapping | None, source_file: str) -> tuple[tuple, tuple]:
        """按 spec 3.2.8 计算 (分区键, 边界键)。

        分区键 = stream.key 各分量 —— "meta:<字段>" 的点分路径取值（文本）或
        "source_dir"（ref.source_file 的父目录）。文本 input_order 排序下，边界键
        额外并入来源文件：此时换文件必断会话（没有时间戳能跨越文件边界），而
        meta:* 排序下文件边界是透明的（轮转日志场景）。

        @param raw 记录的 raw object（UI 模态为 None）
        @param source_file 记录来源文件（相对路径）
        @return (分区键, 边界键) 二元组
        """
        parts: list = []
        for key in self._cfg.stream.key:
            if key == "source_dir":
                parts.append(PurePosixPath(source_file).parent.as_posix())
            else:  # "meta:<字段>" —— 形态已由 M1 校验（仅文本模态）
                parts.append(_extract_text_field(raw or {}, key[len("meta:"):]))
        part_key = tuple(parts)
        if self._cfg.run.modality == "text" and self._meta_field() is None:
            return part_key, part_key + (source_file,)
        return part_key, part_key

    def _close_session(self, buf: list[tuple[Record, float | None]],
                       cause: str) -> Session:
        """闭合一个会话：算 session_id、累加账本、发 segment.session 事件。

        @param buf 会话缓冲：(记录, 序键) 列表
        @param cause 闭合原因（spec 3.2.8 / S17 词表）
        @return 闭合后的 Session
        """
        records = tuple(rec for rec, _ in buf)
        joined = "\n".join(r.id for r in records)
        session_id = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
        # D2 唯一性守卫：记录 id 是内容哈希，因此成员逐字节相同的两个会话（被
        # max_len 切开的重复日志行、彼此相同的空闲屏孤帧……）会撞号，并在装进同
        # 一批后被 M14 的 session_id 重新分组「静默合并」。撞号时把一个确定性的
        # 运行内重复序数折进哈希 —— 首次出现仍走朴素推导，故正常流的 id 稳定。
        repeat = self._session_id_seen.get(session_id, 0)
        self._session_id_seen[session_id] = repeat + 1
        if repeat:
            session_id = hashlib.sha256(
                f"{joined}\n#repeat:{repeat}".encode("utf-8")).hexdigest()[:16]
        self._report.sessions += 1
        self._emit("segment.session", {
            "session_id": session_id,
            "first": self._order_repr(buf[0]),
            "last": self._order_repr(buf[-1]),
            "len": len(records),
            "cause": cause,
        })
        return Session(session_id=session_id, records=records, cause=cause)

    def _order_repr(self, entry: tuple[Record, float | None]) -> float | int | str:
        """segment.session 事件 first/last 的序键展示形：meta:* → epoch float；
        input_order 文本 → "文件:行号"；UI → pair_index。

        @param entry (记录, 序键) 二元组
        @return 序键的展示值
        """
        rec, key = entry
        if key is not None:
            return key
        if self._cfg.run.modality == "ui":
            return rec.ref.pair_index
        return f"{rec.ref.source_file}:{rec.ref.line_no}"

    def _disorder(self, rec: Record, reason: str) -> None:
        """S19/S20：被单调性校验拒绝的记录（乱序或时间戳解析失败）按
        stream.on_disorder 处置。

        skip：不喂给会话流 —— 计 bad_input + disorder + bad_locations + 每记录一条
        ingest.disorder trace 事件 + 全运行一条不含数据内容的 stderr WARN（就在这
        里打；该事件在 obslog 无镜像行 —— 镜像会逐记录触发并带出 reason 里的时间戳
        取值，D1）。fail：抛 InputError（退出码 3）。

        @param rec 被拒记录
        @param reason 拒绝原因（含时间戳取值，只进 trace 通道）
        @return 无
        @raises InputError stream.on_disorder = "fail"
        """
        ref = rec.ref
        text = self._cfg.run.modality == "text"
        line_no = ref.line_no if text else None
        index = None if text else ref.pair_index
        self._report.disorder += 1
        self._bad(file=ref.source_file, line_no=line_no, index=index, reason=reason)
        payload: dict = {"file": ref.source_file}
        if text:
            payload["line_no"] = line_no
        else:
            payload["index"] = index
        payload["reason"] = reason
        self._emit("ingest.disorder", payload)
        if self._cfg.stream.on_disorder == "fail":
            loc = (f"{ref.source_file}:{line_no}" if text
                   else f"{ref.source_file} index={index}")
            raise InputError(f"{loc}: {reason} (stream.on_disorder = \"fail\")")
        if not self._disorder_warned:
            self._disorder_warned = True
            # 按设计不含数据内容（spec §7.1 ①）：逐记录的 reason 里嵌着时间戳/游标
            # 取值，只留在 trace 通道。
            _LOGGER.warning(
                "out-of-order / timestamp-parse-failure records detected and skipped "
                "per stream.on_disorder = \"skip\" (this warning fires once per run; "
                "see the trace ingest.disorder events and report.counts.bad_input for "
                "per-record detail)",
                extra=_LOG_EXTRA)

    def _fused_text_scan(self, root: Path,
                         files: list[str]) -> tuple[int, tuple[int, ...]]:
        """S23：一遍读取同时产出行数预估与会话干跑长度。

        这是 sessions() 的纯计数彩排：不产 Record、不发事件、不改账本，坏行与乱序
        行绝不抛错 —— 其跳过方式与真实 skip 策略运行完全一致。帧级 --limit 作用在
        解析与装配之间（S17），而行计数仍覆盖每一行（estimated_records 语义不变）。

        @param root run.input 根路径
        @param files 相对文件名清单
        @return (非空行总数, 会话干跑长度元组)
        @raises InputError 输入文件不可读
        """
        meta_field = self._meta_field()
        st = _TextRehearsal(
            asm=_Assembler(self._cfg.stream, text=True, meta=meta_field is not None),
            meta_field=meta_field,
            text_field=self._cfg.input.text_field,
            limit=self._cfg.limit)
        for rel in files:
            path = root / rel if root.is_dir() else root
            try:
                with path.open("rb") as fh:
                    for line_no, line_bytes in enumerate(fh, 1):
                        self._rehearse_text_line(line_bytes, rel, line_no, st)
            except OSError as exc:
                raise InputError(f"cannot read input file {path}: {exc}") from exc
        if st.asm.length:
            st.lens.append(st.asm.length)
        return st.estimated, tuple(st.lens)

    def _rehearse_text_line(self, line_bytes: bytes, rel: str, line_no: int,
                            st: _TextRehearsal) -> None:
        """把一行文本喂进会话干跑（S23）：先计行数，再按真实运行的跳过规则装配。

        @param line_bytes 原始行字节
        @param rel 来源文件相对路径
        @param line_no 文件内行号（从 1 起）
        @param st 干跑状态（就地推进）
        @return 无
        """
        if not line_bytes.strip():
            return
        st.estimated += 1
        if st.limit is not None and st.frames >= st.limit:
            return         # 装配器已见到最后一帧；行计数继续
        raw = _rehearse_parse(line_bytes, st.text_field)
        if raw is None:
            return
        st.frames += 1     # records() 会产出的一帧（= islice 计数单位）
        order_key: float | None = None
        if st.meta_field is not None:
            raw_value = _lookup_raw(raw, st.meta_field)
            order_key = (None if raw_value is _MISS
                         else _parse_order_key(raw_value))
            if order_key is None:
                return     # 时间戳解析失败 → 跳过
        part_key, boundary = self._stream_keys(raw, rel)
        if st.meta_field is not None:
            cursor = st.cursors.get(part_key)
            if cursor is not None and order_key < cursor:
                return     # 乱序 → 跳过
            st.cursors[part_key] = order_key
        if st.asm.pre_close(boundary, order_key, line_no, rel) is not None:
            st.lens.append(st.asm.length)
            st.asm.reset()
        if st.asm.feed(boundary, order_key, line_no, rel):
            st.lens.append(st.asm.length)
            st.asm.reset()

    def _rehearse_ui_sessions(
            self, pairs: tuple[tuple[int, str, str], ...]) -> tuple[int, ...]:
        """S23（UI）：只凭配对表做会话干跑 —— index 顺序 + gap_steps /
        session_max_len / source_dir 键规则，零额外 I/O（meta:* 排序仅文本模态，
        故 gap_s / max_span 永不生效）。真实运行会当坏记录跳过的配对，这里一律近似
        为在场，口径与 estimated_records = len(pairs) 一致。

        @param pairs UI 配对表
        @return 会话干跑长度元组
        """
        asm = _Assembler(self._cfg.stream, text=False, meta=False)
        lens: list[int] = []
        limit = self._cfg.limit
        for n, (index, tree_rel, _image_rel) in enumerate(pairs):
            if limit is not None and n >= limit:
                break
            _part_key, boundary = self._stream_keys(None, tree_rel)
            if asm.pre_close(boundary, None, index, tree_rel) is not None:
                lens.append(asm.length)
                asm.reset()
            if asm.feed(boundary, None, index, tree_rel):
                lens.append(asm.length)
                asm.reset()
        if asm.length:
            lens.append(asm.length)
        return tuple(lens)

    # ── 共用辅助 ────────────────────────────────────────────────────────────

    def _require_root(self) -> Path:
        """取输入根路径并做存在性检查。

        @return ``paths.input`` 对应的 Path（v1.17：M1 冻结的绝对输入路径）
        @raises InputError 输入未设置或路径不存在
        """
        if self._root is None:
            raise InputError("run.input is not set (required in process mode)")
        if not self._root.exists():
            raise InputError(f"run.input path does not exist: {self._root}")
        return self._root

    def _stderr_fallback(self, msg: str, *args) -> None:
        """metrics 脱扣时，为扫描期 "fail" 策略补一条 ERROR 级 stderr 行。

        编排层预扫描以 metrics=None 运行以免触碰 trace，而按 ingest.* 事件名做匹配
        的日志管线仍须看到这条结构化行（spec §7.2「fail 策略 error 级」）。

        @param msg 日志格式串（英文）
        @param args 格式参数
        @return 无
        """
        if self.metrics is None:
            _LOGGER.error(msg, *args, extra=_LOG_EXTRA)

    def _emit(self, ev: str, payload: dict) -> None:
        """发一条摄取期 trace 事件（metrics 未接线时静默丢弃）。

        @param ev 事件名（ingest.* / segment.session）
        @param payload 事件载荷
        @return 无
        """
        if self.metrics is not None:
            self.metrics.event(ev, stage="ingest", batch_no=0, payload=payload)

    def _bad(self, *, file: str, line_no: int | None, index: int | None,
             reason: str) -> None:
        """把一条坏数据记入账本（bad_input 计数 + bad_locations 定位）。

        @param file 来源文件相对路径
        @param line_no 文件内行号（UI 模态为 None）
        @param index UI 配对 index（文本模态为 None）
        @param reason 坏数据原因
        @return 无
        """
        self._report.bad_input += 1
        self._report.bad_locations.append(
            {"file": file, "line_no": line_no, "index": index, "reason": reason})

    # ── 文本模态 ────────────────────────────────────────────────────────────

    def _text_files(self, root: Path) -> list[str]:
        """列出相对 .jsonl 文件名，按文件名字典序（spec 3.2.2）。

        @param root run.input 根路径（文件或目录）
        @return 相对文件名清单
        @raises InputError run.input 既非文件也非目录，或目录下没有 .jsonl 文件
        """
        if root.is_file():
            return [root.name]
        if not root.is_dir():
            raise InputError(f"run.input is neither a file nor a directory: {root}")
        files = sorted(p.name for p in root.iterdir()
                       if p.is_file() and p.suffix == ".jsonl")
        if not files:
            raise InputError(f"no .jsonl files under the run.input directory: {root}")
        return files

    def _text_records(self, root: Path) -> Iterator[Record]:
        """文本模态记录流：逐文件逐行解析，坏行按 input.on_bad_line 处置。

        @param root run.input 根路径
        @return Record 迭代器
        @raises InputError 坏行命中 input.on_bad_line = "fail"
        """
        files = self._text_files(root)
        if self._is_generation_stream(root, files):
            yield from self._generation_stream_records(root, files)
            return
        on_bad = self._cfg.input.on_bad_line
        text_field = self._cfg.input.text_field
        for rel in files:
            path = root / rel if root.is_dir() else root
            # 二进制读 + 逐行严格解码：spec 6.1 规定 UTF-8 JSONL、3.2.1 规定原样保留
            # —— 非法字节必须成为坏行，绝不能被静默替换（errors="replace"）后当成
            # 被改写过的数据摄入。
            with path.open("rb") as fh:
                for line_no, line_bytes in enumerate(fh, 1):
                    if not line_bytes.strip():
                        continue  # 空行静默跳过（spec 6.1）
                    self._report.scanned += 1
                    raw, text, reason = _parse_text_line(line_bytes, text_field)
                    if reason is not None:
                        self._bad(file=rel, line_no=line_no, index=None, reason=reason)
                        self._emit("ingest.bad_line",
                                   {"file": rel, "line_no": line_no, "reason": reason})
                        if on_bad == "fail":
                            raise InputError(f"{rel}:{line_no}: {reason}"
                                             f" (input.on_bad_line = \"fail\")")
                        continue
                    self._report.ingested += 1
                    yield Record(
                        id=_text_record_id(raw),
                        modality="text",
                        text=text,
                        raw=raw,
                        ui_tree=None,
                        image=None,
                        ref=RecordRef(source_file=rel, line_no=line_no,
                                      pair_index=None, generated_from=()),
                    )

    @staticmethod
    def _is_generation_stream(root: Path, files: list[str]) -> bool:
        """扫描全部非空行，发现任一 v1.18 envelope 即进入严格分支。

        @param root 输入根。
        @param files 字典序 JSONL 文件表。
        @return 任一可解析行声明 `_meta.event` 时为 True。
        """
        for rel in files:
            path = root / rel if root.is_dir() else root
            with path.open("rb") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        raw = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    meta = raw.get("_meta") if isinstance(raw, Mapping) else None
                    if isinstance(meta, Mapping) and "event" in meta:
                        return True
        return False

    def _generation_stream_records(
        self,
        root: Path,
        files: list[str],
    ) -> Iterator[Record]:
        """先完整验证自包含 provenance，再产出 generation stream Records。

        @param root 输入根。
        @param files 字典序 JSONL 文件表。
        @return 验证通过的 Record 迭代器。
        """
        try:
            rows = self._read_generation_rows(root, files, count_report=True)
            self._validate_generation_rows(rows)
        except (KeyError, TypeError, ValueError) as exc:
            self._generation_input_failure(str(exc))
        for row in rows:
            self._report.ingested += 1
            yield Record(
                id=str(row.event["event_id"]),
                modality="text",
                text=_canonical_json(row.raw["payload"]),
                raw=row.raw,
                ui_tree=None,
                image=None,
                ref=RecordRef(source_file=row.source_file, line_no=row.line_no,
                              pair_index=None, generated_from=()),
                exact_dedup_text=row.exact_dedup_text,
            )

    def _read_generation_rows(
        self,
        root: Path,
        files: list[str],
        *,
        count_report: bool,
    ) -> tuple[_GenerationInputRow, ...]:
        """严格读取整个 generation stream 工件。

        @param root 输入根。
        @param files 字典序 JSONL 文件表。
        @param count_report 是否把完整验证读取计入 live ingest report。
        @return 尚未信任身份的完整行表。
        """
        rows: list[_GenerationInputRow] = []
        for rel in files:
            path = root / rel if root.is_dir() else root
            with path.open("rb") as handle:
                for line_no, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    if count_report:
                        self._report.scanned += 1
                    raw = json.loads(line.decode("utf-8"))
                    rows.append(self._parse_generation_row(raw, rel, line_no))
        if not rows:
            raise ValueError("generation stream is empty")
        return tuple(rows)

    @staticmethod
    def _parse_generation_row(raw: object, source_file: str,
                              line_no: int) -> _GenerationInputRow:
        """解析一行固定 generation envelope，不接受 legacy fallback。"""
        if not isinstance(raw, Mapping) or tuple(raw) != ("payload", "_meta"):
            raise ValueError("generation stream row shape is invalid")
        if not isinstance(raw["payload"], Mapping):
            raise ValueError("generation stream payload is not an object")
        meta = raw["_meta"]
        event = meta.get("event") if isinstance(meta, Mapping) else None
        if not isinstance(event, Mapping):
            raise ValueError("generation stream event metadata is invalid")
        timestamp_us = _generation_timestamp_us(event.get("timestamp"))
        duration_us = event.get("duration_us")
        resources = _artifact_resources(event.get("resources"), duration_us)
        bindings = _artifact_time_bindings(event.get("time_bindings"))
        paths = tuple(item.payload_path for item in bindings)
        exact = _canonical_json(project_temporal_instance(raw["payload"], paths))
        row = _GenerationInputRow(
            raw, source_file, line_no, event, timestamp_us,
            int(duration_us), resources, bindings, exact,
        )
        _validate_artifact_payload_time(row)
        return row

    def _generation_input_failure(self, reason: str) -> None:
        """记录 data-free 错误并拒绝整个 generation stream。

        @param reason 固定结构错误原因。
        @return 不返回。
        """
        self._report.bad_input += 1
        _LOGGER.error("generation_input_invalid: %s", reason, extra=_LOG_EXTRA)
        raise InputError(f"generation_input_invalid: {reason}")

    @staticmethod
    def _validate_generation_rows(rows: tuple[_GenerationInputRow, ...]) -> None:
        """验证全局 ID/起点/资源与 primary/replay owner 闭包。"""
        primary: dict[str, list[_GenerationInputRow]] = {}
        replay: dict[str, list[_GenerationInputRow]] = {}
        event_ids: set[str] = set()
        for row in rows:
            event_id = row.event.get("event_id")
            if not isinstance(event_id, str) or _GENERATION_ID_RE.fullmatch(event_id) is None:
                raise ValueError("generation event ID format is invalid")
            if event_id in event_ids:
                raise ValueError("generation event IDs are not globally unique")
            event_ids.add(event_id)
            Ingestor._classify_generation_row(row, primary, replay)
        Ingestor._validate_generation_timeline(rows)
        Ingestor._validate_primary_groups(primary)
        for replay_id, group in replay.items():
            Ingestor._validate_replay_group(replay_id, group, primary)

    @staticmethod
    def _classify_generation_row(row, primary, replay) -> None:
        """把一行放入 primary/replay 组，或验证 noise 固定空身份。"""
        event = row.event
        owner = event.get("owner_sequence_id")
        replay_id = event.get("replay_sequence_id")
        if isinstance(owner, str):
            Ingestor._validate_primary_row(row)
            primary.setdefault(owner, []).append(row)
            return
        if replay_id is not None:
            if not isinstance(replay_id, str) or _GENERATION_ID_RE.fullmatch(replay_id) is None:
                raise ValueError("replay sequence ID format is invalid")
            if owner is not None or event.get("noise") is not None:
                raise ValueError("replay owner metadata is invalid")
            Ingestor._validate_replay_shape(row)
            replay.setdefault(replay_id, []).append(row)
            return
        Ingestor._validate_noise_row(row)

    @staticmethod
    def _validate_primary_row(row: _GenerationInputRow) -> None:
        """独立重算一条 primary event ID 并验证生成分支身份。"""
        event, meta = row.event, row.raw["_meta"]
        if set(event) != _PRIMARY_EVENT_FIELDS or frozenset(meta) not in _PRIMARY_META_FIELDS:
            raise ValueError("primary metadata fields are invalid")
        generation = meta.get("generation")
        Ingestor._validate_primary_generation(generation)
        world = generation.get("world_branch_id") if isinstance(generation, Mapping) else None
        event_key = event.get("event_key")
        if not all(isinstance(value, str) and _GENERATION_ID_RE.fullmatch(value)
                   for value in (event.get("owner_sequence_id"), world, event_key)):
            raise ValueError("primary generation identity is invalid")
        expected = derive_generation_id(
            "primary_event_id", [
                world,
                event_key,
                row.timestamp_us,
                row.duration_us,
                list(row.resources),
                row.event["time_bindings"],
                row.raw["payload"],
            ]
        )
        if event.get("event_id") != expected:
            raise ValueError("primary event ID does not match its source")
        if event.get("noise") is not None or event.get("replay_sequence_id") is not None:
            raise ValueError("primary event contains replay or noise identity")
        logical = event.get("logical_time_us")
        if (not all(isinstance(event.get(key), str) and bool(event.get(key))
                    for key in ("role", "frame_class", "actor"))
                or not isinstance(logical, int) or isinstance(logical, bool) or logical < 0):
            raise ValueError("primary event semantics are invalid")
        classification = {
            "label": event.get("frame_class"),
            "labels": [event.get("frame_class")],
            "source": "inherited",
        }
        if meta.get("classification") != classification:
            raise ValueError("primary classification differs from frame truth")

    @staticmethod
    def _validate_primary_generation(generation) -> None:
        """封闭 declared 或 instruction-only primary generation truth。"""
        if not isinstance(generation, Mapping):
            raise ValueError("primary generation truth is invalid")
        mode = generation.get("validation_mode")
        expected = (_DECLARED_GENERATION_FIELDS if mode == "declared"
                    else _INSTRUCTION_GENERATION_FIELDS if mode == "instruction_only" else None)
        if expected is None or set(generation) != expected:
            raise ValueError("primary generation fields are invalid")
        knowledge = ("mechanical_and_semantic" if mode == "declared" else "semantic")
        if generation.get("actor_knowledge_validation") != knowledge:
            raise ValueError("primary generation validation mode is invalid")
        ids = (generation.get("scenario_id"), generation.get("world_branch_id"))
        index = generation.get("scenario_index")
        if (not all(isinstance(value, str) and _GENERATION_ID_RE.fullmatch(value)
                    for value in ids)
                or not isinstance(index, int) or isinstance(index, bool) or index < 0
                or not isinstance(generation.get("sequence_class"), str)
                or not generation.get("sequence_class")):
            raise ValueError("primary generation identity is invalid")
        names = (("scenario_set", "pattern", "variant") if mode == "declared"
                 else ("instruction_slot",))
        if not all(isinstance(generation.get(key), str) and generation.get(key) for key in names):
            raise ValueError("primary generation source is invalid")

    @staticmethod
    def _validate_noise_row(row: _GenerationInputRow) -> None:
        """封闭 noise event、meta 与空身份。"""
        event, meta = row.event, row.raw["_meta"]
        if set(meta) != {"event", "generation"} or set(event) != _NOISE_EVENT_FIELDS:
            raise ValueError("noise metadata fields are invalid")
        event_key, frame = event.get("event_key"), event.get("frame_class")
        if (not isinstance(event_key, str) or _GENERATION_ID_RE.fullmatch(event_key) is None
                or not isinstance(frame, str) or not frame):
            raise ValueError("noise identity is invalid")
        if (event.get("owner_sequence_id") is not None or event.get("noise") is not True
                or event.get("role") is not None or event.get("actor") is not None
                or event.get("logical_time_us") is not None or meta.get("generation") is not None):
            raise ValueError("noise metadata is invalid")
        if row.duration_us != 0 or row.resources:
            raise ValueError("noise temporal descriptor is invalid")

    @staticmethod
    def _validate_replay_shape(row: _GenerationInputRow) -> None:
        """封闭 replay event 与允许的下游元数据字段集。"""
        meta = row.raw["_meta"]
        if set(row.event) != _REPLAY_EVENT_FIELDS or frozenset(meta) not in _PRIMARY_META_FIELDS:
            raise ValueError("replay metadata fields are invalid")

    @staticmethod
    def _validate_primary_groups(primary: Mapping[str, list[_GenerationInputRow]]) -> None:
        """按工件顺序重算每个 primary owner sequence ID。"""
        for owner, group in primary.items():
            truths = {
                _canonical_json(row.raw["_meta"]["generation"])
                for row in group
            }
            if len(truths) != 1:
                raise ValueError("primary owner has multiple generation truths")
            worlds = {row.raw["_meta"]["generation"]["world_branch_id"] for row in group}
            if len(worlds) != 1:
                raise ValueError("primary owner has multiple world branches")
            event_ids = [row.event["event_id"] for row in group]
            if derive_generation_id("sequence_id", [next(iter(worlds)), event_ids]) != owner:
                raise ValueError("primary sequence ID does not match ordered events")

    @staticmethod
    def _validate_replay_group(replay_id: str, group, primary) -> None:
        """重算 replay sequence/event IDs 并验证逐位 source provenance。"""
        first = group[0].event
        source_id, ordinal = first.get("duplicate_of_sequence_id"), first.get("replay_ordinal")
        if (not isinstance(source_id, str) or source_id not in primary
                or not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0):
            raise ValueError("replay source identity is invalid")
        if derive_generation_id("replay_sequence_id", [source_id, ordinal]) != replay_id:
            raise ValueError("replay sequence ID does not match its source")
        sources = primary[source_id]
        if len(group) != len(sources):
            raise ValueError("replay event count does not match its source")
        shifts = {row.timestamp_us - source.timestamp_us
                  for row, source in zip(group, sources, strict=True)}
        if len(shifts) != 1 or next(iter(shifts)) <= 0 or next(iter(shifts)) % 1000:
            raise ValueError("replay events do not use one positive constant shift")
        for row, source in zip(group, sources, strict=True):
            Ingestor._validate_replay_row(row, source, replay_id, source_id, ordinal)

    @staticmethod
    def _validate_replay_row(row, source, replay_id: str,
                             source_id: str, ordinal: int) -> None:
        """验证一条 replay 的位置引用、内容副本与新生 event ID。"""
        event, source_event = row.event, source.event
        if (event.get("replay_sequence_id") != replay_id
                or event.get("replay_ordinal") != ordinal
                or event.get("duplicate_of_sequence_id") != source_id
                or event.get("duplicate_of_event_id") != source_event.get("event_id")):
            raise ValueError("replay positional provenance is invalid")
        expected = derive_generation_id(
            "replay_event_id", [
                replay_id,
                source_event["event_id"],
                row.timestamp_us,
                source.duration_us,
                row.raw["payload"],
            ]
        )
        if event.get("event_id") != expected:
            raise ValueError("replay event ID does not match its source")
        Ingestor._validate_replay_copy(row, source)
        Ingestor._validate_replay_generation(row, source, source_id)

    @staticmethod
    def _validate_replay_copy(row, source) -> None:
        """验证 replay 只重绑机械时间，其他 source 内容逐位相同。"""
        fields = (
            "event_key", "role", "frame_class", "actor", "logical_time_us",
            "duration_us", "resources", "time_bindings",
        )
        if any(row.event.get(key) != source.event.get(key) for key in fields):
            raise ValueError("replay event content differs from its source")
        row_meta, source_meta = row.raw["_meta"], source.raw["_meta"]
        row_other = {key: value for key, value in row_meta.items()
                     if key not in {"event", "generation"}}
        source_other = {key: value for key, value in source_meta.items()
                        if key not in {"event", "generation"}}
        if row.exact_dedup_text != source.exact_dedup_text or row_other != source_other:
            raise ValueError("replay final row differs from its source")

    @staticmethod
    def _validate_generation_timeline(rows: tuple[_GenerationInputRow, ...]) -> None:
        """校验全局起点唯一与每个 exclusive resource 的半开区间互斥。"""
        starts: set[int] = set()
        intervals: dict[str, list[tuple[int, int]]] = {}
        for row in rows:
            if row.timestamp_us in starts:
                raise ValueError("generation event timestamps are not globally unique")
            starts.add(row.timestamp_us)
            for resource in row.resources:
                intervals.setdefault(resource, []).append(
                    (row.timestamp_us, row.timestamp_us + row.duration_us))
        for values in intervals.values():
            ordered = sorted(values)
            if any(current[0] < previous[1]
                   for previous, current in zip(ordered, ordered[1:], strict=False)):
                raise ValueError("generation resource intervals overlap")

    @staticmethod
    def _validate_replay_generation(row, source, source_id: str) -> None:
        """验证 replay generation truth 只引用 source，不伪造 primary truth。"""
        source_truth = source.raw["_meta"].get("generation")
        truth = row.raw["_meta"].get("generation")
        if not isinstance(source_truth, Mapping) or not isinstance(truth, Mapping):
            raise ValueError("replay generation provenance is invalid")
        expected = {
            "validation_mode": "replay",
            "source_validation_mode": source_truth.get("validation_mode"),
            "sequence_class": source_truth.get("sequence_class"),
            "scenario_id": source_truth.get("scenario_id"),
            "source_pattern": source_truth.get("pattern"),
            "source_variant": source_truth.get("variant"),
            "duplicate_of_sequence_id": source_id,
        }
        if dict(truth) != expected:
            raise ValueError("replay generation provenance does not match its source")

    # ── UI 模态：扫描与配对（spec 3.2.4）────────────────────────────────────

    def _scan_ui(self, root: Path) -> _UIScan:
        """递归扫描输入目录，按 index 组装配对表与两类异常（spec 3.2.4）。

        @param root run.input 根目录
        @return UI 全量扫描结果 _UIScan
        @raises InputError run.input 不是目录，或目录下没有任何可识别文件
        """
        if not root.is_dir():
            raise InputError(f"UI modality requires run.input to be a directory: {root}")
        trees: dict[int, list[str]] = {}
        images: dict[int, list[str]] = {}
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            m = _TREE_RE.match(path.name)
            if m:
                trees.setdefault(int(m.group(1), 10), []).append(rel)
                continue
            m = _IMAGE_RE.match(path.name)
            if m:
                images.setdefault(int(m.group(1), 10), []).append(rel)
        if not trees and not images:
            raise InputError(
                "no uitree_<index>.jsonl / image_<index>.(png|jpg|jpeg) files under the "
                f"UI modality directory: {root}")

        pairs: list[tuple[int, str, str]] = []
        conflicts: list[tuple[int, tuple[str, ...]]] = []
        missing: list[tuple[int, str, str]] = []
        for index in sorted(set(trees) | set(images)):
            t = trees.get(index, [])
            i = images.get(index, [])
            if len(t) >= 2 or len(i) >= 2:
                conflicts.append((index, tuple(t + i)))
            elif not i:
                missing.append((index, "tree", t[0]))
            elif not t:
                missing.append((index, "image", i[0]))
            else:
                pairs.append((index, t[0], i[0]))
        return _UIScan(pairs=tuple(pairs), conflicts=tuple(conflicts),
                       missing=tuple(missing))

    def _ui_records(self, root: Path) -> Iterator[Record]:
        """UI 模态记录流：先按 index 升序报告两类异常，再逐对解析产出记录。

        `scanned` 是在每个 index 被真正处理时逐个累加的（不是扫描完就一次记满），
        因此被部分消费的流（--limit、熔断、SIGINT）仍满足 §6.4 报告恒等式
        emitted + dropped_* + failed + bad_input = scanned + generated。

        @param root run.input 根目录
        @return Record 迭代器
        @raises InputError 配对异常或坏记录命中对应 "fail" 策略
        """
        icfg = self._cfg.input
        ui = self._scan_ui(root)
        self._report_ui_anomalies(ui)
        max_bytes = icfg.max_image_mb * 1024 * 1024
        for pair in ui.pairs:
            self._report.scanned += 1
            record, reason, bad_file = self._load_ui_pair(root, pair, max_bytes)
            if reason is not None:
                self._bad(file=bad_file, line_no=None, index=pair[0], reason=reason)
                self._emit("ingest.bad_line",
                           {"file": bad_file, "line_no": None, "reason": reason})
                if icfg.on_bad_line == "fail":
                    raise InputError(
                        f"{bad_file}: {reason} (input.on_bad_line = \"fail\")")
                continue
            self._report.ingested += 1
            yield record

    def _report_ui_anomalies(self, ui: _UIScan) -> None:
        """在配对解析之前，按 index 升序报告 index 冲突与缺对两类异常。

        @param ui UI 全量扫描结果
        @return 无
        @raises InputError 命中 on_index_conflict / on_missing_pair 的 "fail" 策略
        """
        icfg = self._cfg.input
        for index, files in ui.conflicts:
            self._report.scanned += 1
            self._report.index_conflict += 1
            self._emit("ingest.index_conflict", {"index": index, "files": list(files)})
            if icfg.on_index_conflict == "fail":
                raise InputError(f"UI index conflict: index={index} matches multiple files "
                                 f"{list(files)} (input.on_index_conflict = \"fail\")")
            self._bad(file=files[0], line_no=None, index=index,
                      reason=f"index conflict: {list(files)}")
        for index, present, rel in ui.missing:
            self._report.scanned += 1
            self._report.missing_pair += 1
            self._emit("ingest.missing_pair",
                       {"index": index, "present": present, "file": rel})
            if icfg.on_missing_pair == "fail":
                raise InputError(f"UI missing pair: index={index} has only the {present} "
                                 f"side file {rel} (input.on_missing_pair = \"fail\")")
            self._bad(file=rel, line_no=None, index=index,
                      reason=f"missing pair: only the {present} side file is present")

    def _load_ui_pair(self, root: Path, pair: tuple[int, str, str],
                      max_bytes: int) -> tuple[Record | None, str | None, str]:
        """加载一对 UI 文件（图像魔数/尺寸校验 + UI 树解析）并组装 Record。

        @param root run.input 根目录
        @param pair 配对表条目 (index, 树相对路径, 图相对路径)
        @param max_bytes 图像字节数上限（由 input.max_image_mb 换算）
        @return (Record, 坏记录原因, 归因文件)；坏记录时 Record 为 None
        """
        index, tree_rel, image_rel = pair
        image_path = root / image_rel
        reason = self._check_image(image_path, max_bytes)
        bad_file = image_rel
        ui_tree: UITree | None = None
        tree_bytes = b""
        if reason is None:
            bad_file = tree_rel
            try:
                tree_bytes = (root / tree_rel).read_bytes()
            except OSError as exc:
                _LOGGER.debug("cannot read UI tree file: %s", exc, extra=_LOG_EXTRA)
                reason = f"cannot read UI tree file: {exc}"
            else:
                ui_tree, reason = _parse_ui_tree(tree_bytes)
        if reason is not None:
            return None, reason, bad_file

        image_bytes = image_path.read_bytes()
        rec_id = hashlib.sha256(tree_bytes + image_bytes).hexdigest()[:16]
        ext = image_path.suffix.lower().lstrip(".")
        image_ref = ImageRef(
            path=image_path,
            format="png" if ext == "png" else "jpeg",
            size_bytes=len(image_bytes),
        )
        del image_bytes  # 只参与哈希 —— 像素保持惰性（spec §2.6）
        record = Record(
            id=rec_id,
            modality="ui",
            text=None,
            raw=None,
            ui_tree=ui_tree,
            image=image_ref,
            ref=RecordRef(source_file=tree_rel, line_no=None,
                          pair_index=index, generated_from=()),
        )
        return record, None, bad_file

    @staticmethod
    def _check_image(path: Path, max_bytes: int) -> str | None:
        """只做魔数与尺寸校验，不解码全图（spec 3.2.4）。

        @param path 图像文件路径
        @param max_bytes 图像字节数上限
        @return 坏图原因；图像合格返回 None
        """
        try:
            size = path.stat().st_size
            with path.open("rb") as fh:
                head = fh.read(8)
        except OSError as exc:
            _LOGGER.debug("cannot read image file: %s", exc, extra=_LOG_EXTRA)
            return f"cannot read image file: {exc}"
        if size > max_bytes:
            return (f"image size {size} bytes exceeds the input.max_image_mb = "
                    f"{max_bytes // (1024 * 1024)} limit")
        ext = path.suffix.lower().lstrip(".")
        if ext == "png":
            if not head.startswith(_PNG_MAGIC):
                return "image magic number does not match the .png extension"
        else:
            if not head.startswith(_JPEG_MAGIC):
                return f"image magic number does not match the .{ext} extension"
        return None


# ── UI 树解析（spec 3.2.4 + §6.2 字段映射）──────────────────────────────────

def _iter_node_objects(lines: list[tuple[int, str]]) -> Iterator[tuple[int, dict]]:
    """遍历 UI 树文件里的有效节点行（单条坏行逐条跳过，spec 3.2.4）。

    @param lines (行号, 行文本) 列表
    @return (行号, 节点 object) 迭代器
    """
    for line_no, ln in lines:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            _LOGGER.debug("skipped a bad UI tree node line", extra=_LOG_EXTRA)
            continue
        if isinstance(obj, dict):
            yield line_no, obj


def _parse_ui_tree(data: bytes) -> tuple[UITree | None, str | None]:
    """解析一个 uitree_<index>.jsonl 文件。

    空文件或全坏行 ⇒ 该记录按坏记录跳过（spec 3.2.4）；单条坏节点行只跳过该行。

    @param data UI 树文件的原始字节
    @return 成功返回 (UITree, None)；失败返回 (None, 坏记录原因)
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _LOGGER.debug("UI tree file is not valid UTF-8", extra=_LOG_EXTRA)
        return None, "UI tree file is not valid UTF-8"
    lines = [(no, ln) for no, ln in enumerate(text.splitlines(), 1) if ln.strip()]
    if not lines:
        return None, "UI tree file is empty"

    # 首行探针：带 `children` 数组的 object ⇒ 嵌套式
    nested = False
    try:
        first = json.loads(lines[0][1])
        nested = isinstance(first, dict) and isinstance(first.get("children"), list)
    except json.JSONDecodeError:
        _LOGGER.debug("UI tree first-line probe failed, assuming flat style",
                      extra=_LOG_EXTRA)

    nodes: list[UINode] = []
    if nested:
        counter = [0]
        for _, obj in _iter_node_objects(lines):
            _walk_nested(obj, parent_id=None, depth=0, counter=counter, out=nodes)
    else:
        flat = [_normalize_node(obj, default_node_id=str(line_no),
                                structural_parent=None)
                for line_no, obj in _iter_node_objects(lines)]
        nodes = _flat_to_dfs(flat)
    if not nodes:
        return None, "UI tree file has only bad lines"
    return UITree(nodes=tuple(nodes)), None


def _flat_to_dfs(flat: list[UINode]) -> list[UINode]:
    """把平铺式节点重建成深度优先序，深度由 parent_id 图推导。

    spec 4.1 规定 ``UITree.nodes # 深度优先序`` —— 这是一条与文件内顺序无关的类型
    契约（例如按广度优先导出的无障碍树也必须满足）。根 = parent_id 为 None 或指向
    未知 id 的节点；子节点保持文件内顺序；任何从根不可达的节点（parent_id 成环）
    回落为深度 0 的根，同样保持文件内顺序。

    @param flat 平铺式节点列表（文件内顺序）
    @return 深度优先序的节点列表
    """
    known_ids = {n.node_id for n in flat}
    roots: list[int] = []
    children: dict[str, list[int]] = {}
    for i, node in enumerate(flat):
        if node.parent_id is None or node.parent_id not in known_ids:
            roots.append(i)
        else:
            children.setdefault(node.parent_id, []).append(i)

    out: list[UINode] = []
    visited: set[int] = set()

    def _visit(i: int, depth: int) -> None:
        """深度优先访问一个节点及其子树（已访问节点直接返回）。

        @param i 节点在 flat 中的下标
        @param depth 该节点的深度
        @return 无
        """
        if i in visited:
            return
        visited.add(i)
        node = flat[i]
        out.append(_with_depth(node, depth))
        for child in children.get(node.node_id, ()):
            _visit(child, depth + 1)

    for i in roots:
        _visit(i, 0)
    for i in range(len(flat)):  # 成环且从任何根都不可达的节点
        _visit(i, 0)
    return out


def _walk_nested(obj: dict, *, parent_id: str | None, depth: int,
                 counter: list[int], out: list[UINode]) -> None:
    """深度优先遍历嵌套式 UI 树（spec 3.2.4）。

    @param obj 当前节点 object
    @param parent_id 结构父节点 id（根为 None）
    @param depth 当前深度
    @param counter 单元素列表形式的自增计数器（缺省 node_id 用）
    @param out 输出列表（就地追加）
    @return 无
    """
    counter[0] += 1
    node = _normalize_node(obj, default_node_id=str(counter[0]),
                           structural_parent=parent_id, consume_children=True)
    out.append(_with_depth(node, depth))
    children = obj.get("children")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                _walk_nested(child, parent_id=node.node_id, depth=depth + 1,
                             counter=counter, out=out)


def _with_depth(node: UINode, depth: int) -> UINode:
    """返回深度被改写为给定值的节点（深度已相同则原样返回）。

    @param node 原节点
    @param depth 目标深度
    @return 深度为 depth 的 UINode
    """
    if node.depth == depth:
        return node
    return UINode(node_id=node.node_id, parent_id=node.parent_id, depth=depth,
                  role=node.role, text=node.text, content_desc=node.content_desc,
                  bounds=node.bounds, visible=node.visible, extra=node.extra)


def _first_present(obj: dict, keys: tuple[str, ...]) -> tuple[str | None, Any]:
    """按优先级取首个在场的源字段（§6.2 字段映射）。

    @param obj 节点原始 object
    @param keys 接受的源字段名，按优先级排列
    @return (命中的源字段名, 其取值)；全都缺席返回 (None, None)
    """
    for key in keys:
        if key in obj:
            return key, obj[key]
    return None, None


def _parse_bounds(value: Any) -> tuple[int, int, int, int] | None:
    """解析 bounds：接受 [l,t,r,b] 数组与 "[l,t][r,b]" 字符串两种形态（spec §6.2）。

    @param value bounds 源取值
    @return (l, t, r, b) 四元组；解析失败返回 None
    """
    if isinstance(value, list) and len(value) == 4:
        try:
            return tuple(int(v) for v in value)  # type: ignore[return-value]
        except (TypeError, ValueError):
            _LOGGER.debug("bounds array is not integral", extra=_LOG_EXTRA)
            return None
    if isinstance(value, str):
        m = _BOUNDS_STR_RE.match(value)
        if m:
            return tuple(int(g) for g in m.groups())  # type: ignore[return-value]
    return None


def _coerce_visible(value: Any) -> bool:
    """把 visible 源取值折成布尔（§6.2）：字符串按假值词表判定，其余走 bool()。

    @param value visible 源取值
    @return 可见性布尔值
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "")
    return bool(value)


def _stringify_extra(value: Any) -> str:
    """把落入 extra 的字段值转字符串（§6.2「其余全部字段，值转字符串」）。

    @param value 源字段取值
    @return 字符串（非字符串取规范 JSON 序列化）
    """
    if isinstance(value, str):
        return value
    return _canonical_json(value)


def _mapped_str(obj: dict, keys: tuple[str, ...], consumed: set[str],
                default: str) -> str:
    """§6.2 标量字段映射：取首个在场源字段转字符串，并登记已消费的源字段名。

    @param obj 节点原始 object
    @param keys 该目标字段接受的源字段名，按优先级排列
    @param consumed 已消费源字段名集合（就地登记）
    @param default 源字段缺席或取值为 null 时的默认值
    @return 映射后的字符串
    """
    key, value = _first_present(obj, keys)
    if key is not None:
        consumed.add(key)
    return str(value) if key is not None and value is not None else default


def _mapped_parent(obj: dict, consumed: set[str],
                   structural_parent: str | None) -> str | None:
    """§6.2 父节点映射：源字段在场时以其取值为准（null ⇒ 无父），缺席才回落结构父。

    @param obj 节点原始 object
    @param consumed 已消费源字段名集合（就地登记）
    @param structural_parent 嵌套式遍历给出的结构父 id
    @return 父节点 id；无父返回 None
    """
    key, value = _first_present(obj, _PARENT_KEYS)
    if key is None:
        return structural_parent
    consumed.add(key)
    return str(value) if value is not None else None


def _mapped_bounds(obj: dict, consumed: set[str]) -> tuple[int, int, int, int]:
    """§6.2 bounds 映射：字段缺席或解析失败一律回落 (0, 0, 0, 0)。

    @param obj 节点原始 object
    @param consumed 已消费源字段名集合（就地登记）
    @return (l, t, r, b) 四元组
    """
    key, value = _first_present(obj, _BOUNDS_KEYS)
    if key is None:
        return (0, 0, 0, 0)
    consumed.add(key)
    parsed = _parse_bounds(value)
    return parsed if parsed is not None else (0, 0, 0, 0)


def _mapped_visible(obj: dict, consumed: set[str]) -> bool:
    """§6.2 visible 映射：字段缺席或取值为 null 时默认可见。

    @param obj 节点原始 object
    @param consumed 已消费源字段名集合（就地登记）
    @return 可见性布尔值
    """
    key, value = _first_present(obj, _VISIBLE_KEYS)
    if key is not None:
        consumed.add(key)
    return _coerce_visible(value) if key is not None and value is not None else True


def _normalize_node(obj: dict, *, default_node_id: str,
                    structural_parent: str | None,
                    consume_children: bool = False) -> UINode:
    """§6.2 字段映射：每个目标字段取首个在场源字段 + 逐字段默认值，剩余字段值转
    字符串后落进 `extra`（保持插入顺序）。

    `children` 只在嵌套式里是结构字段（consume_children=True）；平铺式行上带的
    `children` 字段按 §6.2 extra 行（其余全部字段，值转字符串）留在 `extra` 里。

    @param obj 节点原始 object
    @param default_node_id 源 id 字段缺席时的兜底 node_id
    @param structural_parent 嵌套式遍历给出的结构父 id
    @param consume_children 是否把 `children` 当结构字段消费掉
    @return 归一化后的 UINode（depth 恒为 0，由调用方改写）
    """
    consumed: set[str] = {"children"} if consume_children else set()
    node_id = _mapped_str(obj, _NODE_ID_KEYS, consumed, default_node_id)
    parent_id = _mapped_parent(obj, consumed, structural_parent)
    role = _mapped_str(obj, _ROLE_KEYS, consumed, "unknown")
    text = _mapped_str(obj, _TEXT_KEYS, consumed, "")
    content_desc = _mapped_str(obj, _DESC_KEYS, consumed, "")
    bounds = _mapped_bounds(obj, consumed)
    visible = _mapped_visible(obj, consumed)
    extra = {k: _stringify_extra(v) for k, v in obj.items() if k not in consumed}
    return UINode(node_id=node_id, parent_id=parent_id, depth=0, role=role,
                  text=text, content_desc=content_desc, bounds=bounds,
                  visible=visible, extra=extra)

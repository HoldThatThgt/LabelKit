"""M8 结构引擎（spec 3.8，CONTRACTS.md §7.7）。

对每一个 LLM 产出的对象施加四层结构保障：

- L0：把 ``response_schema`` 原样交给 ``LLMClient.complete()``；厂商机制（OpenAI 的
  ``response_format`` / Anthropic 的强制工具调用）由客户端决定，profile 未声明
  ``supports_structured_output`` 时客户端忽略之。L0 从不豁免 L2。
- L1：确定性修复——纯模块级函数 ``deterministic_repair``：剥 Markdown 代码围栏 →
  取首个花括号配平子串 → ``json_repair.loads``。
- L2：``jsonschema.Draft202012Validator.iter_errors`` 收集「全部」违规，附 JSON Pointer 路径。
- L2.5：可选的用户校验回调（v1.5 方案 A），仅对用户 Schema 待遇的调用生效。
- L3：有界 LLM 修复环（提示词见 CONTRACTS.md §10.6 / spec 3.8.4），预算为
  ``output.max_repair_attempts``；每轮修复输出重跑 L1 → L2。预算耗尽抛
  ``SchemaViolation(errors, raw_last_output)``。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

import json_repair
from jsonschema import Draft202012Validator

from labelkit.common.contracts.types import Usage
from labelkit.common.errors import ContextOverflowError, SchemaViolation
from labelkit.common.runtime.budget import feed_reactive_terminal

if TYPE_CHECKING:
    from labelkit.common.runtime.llm_client import LLMClient
    from labelkit.common.observability.obslog import MetricsSink

from labelkit.common.observability.obslog import EV_SCHEMA_REPAIR
from labelkit.common.runtime.llm_client import Message, Part, PromptBundle

_logger = logging.getLogger("labelkit.schema")


# ── L1：确定性修复（纯函数，无副作用） ──────────────────────────────────────

def _strip_markdown_fences(text: str) -> str:
    """L1 步骤 ①：剥掉 Markdown 代码围栏（锚定式）。

    只有当文本首个非空白字符就开启围栏时才按「带围栏」处理：去掉开栏行，以及收尾
    的闭栏（末尾那处 ``` 恰好收束全文时）；两者之间的内容——包括嵌在 JSON 字符串值
    里的 ``` ——一律保留，交给步骤 ② 的字符串感知配平扫描。未锚定的文本原样透传
    （其中若含 JSON，由步骤 ② 负责切出）。

    @param text 响应原始文本。
    @return 剥去围栏后的文本；未锚定时为原文本。
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    newline = stripped.find("\n")
    body = stripped[newline + 1:] if newline != -1 else stripped[3:]
    body = body.rstrip()
    if body.endswith("```"):
        body = body[:-3].rstrip()
    return body


def _first_balanced_braces(text: str) -> str | None:
    """L1 步骤 ②：切出首个花括号配平的子串。

    扫描能识别双引号字符串（并正确处理转义），字符串内的花括号不计入配平。输入不配平
    （被截断）时退化为「自首个 '{' 起的后缀」，交给 json_repair 去补全。

    @param text 已剥围栏的文本。
    @return 配平子串或退化后缀；文本中根本没有 '{' 时返回 None。
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


def deterministic_repair(text: str) -> dict | None:
    """L1 确定性修复（spec 3.8.2）：剥围栏 → 取配平子串 → ``json_repair.loads``。

    若围栏派生出的候选串产不出 JSON 对象，则对「原始文本」再跑一遍同样的扫描——这样
    落在锚定围栏之外的 JSON（前置散文里带内联围栏、JSON 在靠后的围栏块里）依然能在
    L1 修好。返回值只由入参决定（异常分支仅记 debug 日志），可穷举单测。

    @param text 响应原始文本。
    @return 解析出的 JSON 对象；每条路径都产不出对象时返回 None。
    """
    fence_stripped = _strip_markdown_fences(text)
    sources = [fence_stripped] if fence_stripped == text else [fence_stripped, text]
    for source in sources:
        candidate = _first_balanced_braces(source)
        if candidate is None:
            candidate = source
        try:
            obj = json_repair.loads(candidate)
        except Exception as exc:
            # 只记异常类型，绝不记候选串内容（脱敏）；换下一个来源继续尝试。
            _logger.debug("L1 candidate is not repairable into JSON (%s); trying the next source",
                          type(exc).__name__)
            continue
        if isinstance(obj, dict):
            return obj
    return None


_L1_LOSS_RATIO = 0.8       # 修复结果须保留候选串 >= 80% 的体量……
_L1_LOSS_MIN_CHARS = 40    # ……除非丢失的字符数还不到这个下限。


def l1_repair_is_lossy(obj: dict, raw: str) -> bool:
    """判定一次 L1 修复是否疑似「截断了内容」（E2E 发现 P2-5 的启发式）。

    故障形态：未转义的内层引号会让 ``json_repair`` 提前收束字符串并「丢掉」其后内容
    ——结果能解析、甚至能过校验，但内容已经静默缺失。因此当修复对象的再序列化长度
    远小于其所修复的花括号区段时，即标记嫌疑。返回值只由入参决定（异常分支仅记 debug 日志）。

    误报护栏（评审发现）：候选串本身就能干净解析（本次修复只是剥了围栏）按定义无损；
    长度基线也不能被美化缩进或 ``\\uXXXX`` 转义撑大，故保留侧取「转义/不转义两种序列
    化的较大者」，候选侧则先去掉全部空白。

    @param obj L1 修复后得到的对象。
    @param raw 该次响应的原始文本。
    @return 疑似截断为 True，否则 False。
    """
    candidate = _first_balanced_braces(_strip_markdown_fences(raw))
    if candidate is None:
        return False
    try:
        if json.loads(candidate) == obj:
            return False                    # 仅剥围栏的修复：什么都没丢
    except ValueError:
        # 候选串本就不是合法 JSON（正是 L1 真出手的情形），转由长度启发式判定。
        _logger.debug("L1 lossy check: the brace region is not clean JSON; "
                      "falling back to the length heuristic")
    kept = max(len(json.dumps(obj, ensure_ascii=False, separators=(",", ":"))),
               len(json.dumps(obj, ensure_ascii=True, separators=(",", ":"))))
    base = len(re.sub(r"\s", "", candidate))
    lost = base - kept
    return lost > _L1_LOSS_MIN_CHARS and kept < _L1_LOSS_RATIO * base


# ── L2 渲染助手 ──────────────────────────────────────────────────────────────

def _json_pointer(path: Any) -> str:
    """把 jsonschema 的错误路径（str|int 组成的 deque）渲染成 RFC 6901 JSON Pointer。

    @param path jsonschema 错误对象的 absolute_path。
    @return JSON Pointer 字符串（按 RFC 6901 转义 ~ 与 /）。
    """
    return "".join(
        "/" + str(token).replace("~", "~0").replace("/", "~1") for token in path
    )


def _render_error(error: Any) -> str:
    """把一条违规渲染成 '<json-pointer>: <描述>'（面向修复提示词）。

    枚举类违规按 spec 3.8.4 的「期望/实际」措辞自行渲染；其它关键字直接携带
    jsonschema 的原始消息。渲染文本双重用途——既进 L3 修复提示词的违规清单，也进
    StageError / rejects 的错误报告面，故按「报错输出英文」规则统一为英文。

    @param error jsonschema 的一条 ValidationError。
    @return 渲染后的违规行。
    """
    pointer = _json_pointer(error.absolute_path)
    if error.validator == "enum":
        expected = json.dumps(list(error.validator_value), ensure_ascii=False)
        actual = json.dumps(error.instance, ensure_ascii=False)
        description = f"expected one of enum {expected}, got {actual}"
    else:
        description = error.message
    return f"{pointer}: {description}"


def _summarize_error(error: Any) -> str:
    """把一条违规摘要成 trace 载荷形态：只留 JSON Pointer + 违反的关键字，不含数据值。

    @param error jsonschema 的一条 ValidationError。
    @return 脱敏后的违规摘要行。
    """
    return f"{_json_pointer(error.absolute_path)}: {error.validator}"


# 连 L1 都产不出 JSON 对象时使用的渲染违规（根指针形态）。
_UNPARSEABLE_VIOLATION = ": output is not parseable as a JSON object"
_UNPARSEABLE_SUMMARY = ": unparseable"


# ── L3 修复提示词（CONTRACTS.md §10.6、spec 3.8.4——逐字节冻结） ─────────────

def _build_repair_prompt(raw_output: str, violations: list[str]) -> str:
    """拼装单条 user 消息：[原始输出] 段 + [违规清单] 一基编号列表 + 收尾指令。

    确定性字符串拼装，不做任何改写。

    @param raw_output 待修复的原始响应文本。
    @param violations 渲染后的违规清单（顺序即编号顺序）。
    @return 修复提示词文本。
    """
    numbered = "\n".join(f"{i}. {v}" for i, v in enumerate(violations, 1))
    return f"[原始输出]\n{raw_output}\n\n[违规清单]\n{numbered}\n\n只输出修正后的 JSON。"


# ── resolved_at 桶归类（纯函数） ─────────────────────────────────────────────

def _bucket_for(l1_fixed: bool, repair_round: int) -> str:
    """给一次「成功定案」归出 resolved_at 桶名。

    首轮即干净通过（L0 生效或文本直接可解析）→ 'l0_or_clean'；L1 出手修过 → 'l1'；
    L3 第 1/2 轮通过 → 'l3_1'/'l3_2'（超过 2 的轮次并入 'l3_2'——冻结的 stats 字典
    没有更多键）。

    @param l1_fixed 首轮结果是否经 L1 修复而来。
    @param repair_round 产出通过对象的 L3 轮次；0 = 未经 L3 即通过。
    @return 桶名字符串。
    """
    if repair_round <= 0:
        return "l1" if l1_fixed else "l0_or_clean"
    return "l3_1" if repair_round == 1 else "l3_2"


# ── 内部 Schema（CONTRACTS.md §10.7——JSON 逐字冻结） ────────────────────────

def judgment_schema(criteria_keys: list[str], with_reason: bool) -> dict:
    """M4 成对判决（QuRating）的内部 Schema：每个准则一条 A/B/tie 判决。

    minItems = maxItems 钉死数组长度 = 准则数；准则名取自闭集 ``criteria_keys``。

    @param criteria_keys 准则键列表（闭集枚举，同时决定数组长度）。
    @param with_reason 是否要求每条判决附 reason 字段。
    @return draft 2020-12 Schema 对象。
    """
    item_props: dict = {"criterion": {"type": "string", "enum": list(criteria_keys)},
                        "winner": {"type": "string", "enum": ["A", "B", "tie"]}}
    required = ["criterion", "winner"]
    if with_reason:
        item_props["reason"] = {"type": "string"}
        required = ["criterion", "winner", "reason"]
    return {"type": "object",
            "properties": {"judgments": {"type": "array",
                "items": {"type": "object", "properties": item_props,
                          "required": required, "additionalProperties": False},
                "minItems": len(criteria_keys), "maxItems": len(criteria_keys)}},
            "required": ["judgments"], "additionalProperties": False}


def pointwise_schema(criterion_key: str) -> dict:
    """M4 逐条打分（pointwise 门）的内部 Schema：单准则 0–5 整数分 + 理由。

    数组长度钉死为 1（minItems = maxItems = 1），准则名为单元素闭集。

    @param criterion_key 本次打分的准则键。
    @return draft 2020-12 Schema 对象。
    """
    return {"type": "object",
            "properties": {"scores": {"type": "array",
                "items": {"type": "object",
                          "properties": {"criterion": {"type": "string", "enum": [criterion_key]},
                                         "reason": {"type": "string"},
                                         "score": {"type": "integer", "minimum": 0, "maximum": 5}},
                          "required": ["criterion", "reason", "score"],
                          "additionalProperties": False},
                "minItems": 1, "maxItems": 1}},
            "required": ["scores"], "additionalProperties": False}


VERDICT_SCHEMA = {          # critiques 在 verdict 之前：先理由后结论（spec 3.8.3 注）
    "type": "object",
    "properties": {"critiques": {"type": "array",
                       "items": {"type": "object",
                                 "properties": {"aspect": {"type": "string"},
                                                "opinion": {"type": "string"}},
                                 "required": ["aspect", "opinion"],
                                 "additionalProperties": False}},
                   "verdict": {"type": "string", "enum": ["pass", "fail"]}},
    "required": ["critiques", "verdict"], "additionalProperties": False}


def samples_schema(num_per_call: int) -> dict:
    """M6 批量生成的内部 Schema：一次调用产出定长的纯文本样本数组。

    @param num_per_call 单次调用的样本条数（同时钉死 minItems 与 maxItems）。
    @return draft 2020-12 Schema 对象。
    """
    return {"type": "object",
            "properties": {"samples": {"type": "array", "items": {"type": "string"},
                                       "minItems": num_per_call, "maxItems": num_per_call}},
            "required": ["samples"], "additionalProperties": False}


def segment_window_schema(frame_count: int, with_reason: bool) -> dict:
    """v1.8 M14（spec §3.2.2 / CONTRACTS §10.7）：一个滑动窗口内逐帧的闭集关系判决。

    minItems = maxItems 钉死数组长度（judgment_schema 先例）；下标对齐在代码侧兜底
    （first-wins，缺省 "continues"）——Schema 表达不了「排列」（R1：不用 uniqueItems）。

    @param frame_count 窗内帧数（同时钉死数组长度与 index 上界）。
    @param with_reason 是否要求每帧判决附 reason 字段。
    @return draft 2020-12 Schema 对象。
    """
    relations = ["continues", "advances", "returns_to_entry", "context_switch", "interruption"]
    item_props: dict = {"index": {"type": "integer", "minimum": 0, "maximum": frame_count - 1},
                        "relation": {"type": "string", "enum": relations}}
    required = ["index", "relation"]
    if with_reason:
        item_props["reason"] = {"type": "string"}
        required = ["index", "relation", "reason"]
    return {"type": "object",
            "properties": {"frames": {"type": "array",
                "items": {"type": "object", "properties": item_props,
                          "required": required, "additionalProperties": False},
                "minItems": frame_count, "maxItems": frame_count}},
            "required": ["frames"], "additionalProperties": False}


def action_schema() -> dict:
    """v1.8 M15（spec 3.15.3 / CONTRACTS §10.7）：相邻帧对的单条动作判决。

    所有键均 required 且可空用联合类型表达——OpenAI strict 模式拒绝可选属性
    （S7，与 R1 同一课）；``["string","null"]`` 是被认可的写法。动作枚举顺序已冻结
    （S15：AndroidControl 全集 ∪ UI-TARS-mobile + other）。

    @return draft 2020-12 Schema 对象。
    """
    actions = ["click", "long_press", "input_text", "scroll", "drag", "open_app",
               "app_switch", "navigate_back", "navigate_home", "wait", "other"]
    return {"type": "object",
            "properties": {"action_type": {"type": "string", "enum": actions},
                           "target": {"type": ["string", "null"]},
                           "value": {"type": ["string", "null"]},
                           "description": {"type": "string"}},
            "required": ["action_type", "target", "value", "description"],
            "additionalProperties": False}


def stitch_schema() -> dict:
    """v1.9 M16（spec 3.16 / CONTRACTS §10.7）：每个候选一条线程缝合判决。

    所有键均 required，thread_ref 可空（strict-safe，S7 的教训）；thread_ref 是所示
    线程池卡片的一基序号（范围检查在代码侧——Schema 看不到池大小）；confidence 只作
    trace 观测，绝不作门（T9）。

    @return draft 2020-12 Schema 对象。
    """
    return {"type": "object",
            "properties": {"verdict": {"type": "string", "enum": ["resume", "new"]},
                           "thread_ref": {"type": ["integer", "null"]},
                           "task_name": {"type": "string"},
                           "reason": {"type": "string"},
                           "confidence": {"type": "string",
                                          "enum": ["high", "medium", "low"]}},
            "required": ["verdict", "thread_ref", "task_name", "reason", "confidence"],
            "additionalProperties": False}


def defect_verdict_schema() -> dict:
    """v1.8 M7 的 stream 变体（spec 3.7.2 / CONTRACTS §10.7）：评语 + 定型缺陷表判决。

    critiques 原样保留（修复回喂环建立在它之上），再加一张定型缺陷表；意见与缺陷都排在
    verdict 之前（先理由后结论，沿用 VERDICT_SCHEMA 先例）。所有键均 required，
    members/position 可空（strict-safe，S7）。verdict = "fail" 却给出空 defects 数组时，
    代码侧归一为一条缺省的 label_mismatch。v1.9（T15）：缺陷种类共六种——追加
    wrong_stitch（仅标记 + 走 fail 路由）。

    @return draft 2020-12 Schema 对象。
    """
    kinds = ["label_mismatch", "off_task_members", "missing_head", "missing_tail",
             "missing_members", "wrong_stitch"]
    return {"type": "object",
            "properties": {
                "critiques": {"type": "array", "items": {"type": "object",
                    "properties": {"aspect": {"type": "string"},
                                   "opinion": {"type": "string"}},
                    "required": ["aspect", "opinion"], "additionalProperties": False}},
                "defects": {"type": "array", "items": {"type": "object",
                    "properties": {"kind": {"type": "string", "enum": kinds},
                                   "members": {"type": ["array", "null"],
                                               "items": {"type": "string"}},
                                   "position": {"type": ["string", "null"]},
                                   "detail": {"type": "string"}},
                    "required": ["kind", "members", "position", "detail"],
                    "additionalProperties": False}},
                "verdict": {"type": "string", "enum": ["pass", "fail"]}},
            "required": ["critiques", "defects", "verdict"],
            "additionalProperties": False}


def classification_schema(class_names: list[str], assignment: str,
                          max_labels: int, with_reason: bool) -> dict:
    """v1.7 M13 序列级/记录级闭集分类的内部 Schema（单标签或多标签两形）。

    v1.7 R1：刻意「不用」uniqueItems（OpenAI strict 模式硬拒该关键字，而 L0 无条件透传
    Schema）——重复标签由 M8 校验「之后」的 classify 侧归一化确定性去重。

    @param class_names 类名闭集列表。
    @param assignment 赋值形态："single" 出 class 字段，其余出 classes 数组。
    @param max_labels 多标签形态下的标签数上限（maxItems）。
    @param with_reason 是否要求附 reason 字段。
    @return draft 2020-12 Schema 对象。
    """
    if assignment == "single":
        props: dict = {"class": {"type": "string", "enum": list(class_names)}}
        required = ["class"]
    else:
        props = {"classes": {"type": "array",
                             "items": {"type": "string", "enum": list(class_names)},
                             "minItems": 1, "maxItems": max_labels}}
        required = ["classes"]
    if with_reason:
        props["reason"] = {"type": "string"}
        required += ["reason"]
    return {"type": "object", "properties": props,
            "required": required, "additionalProperties": False}


def frame_classify_schema(names: Sequence[str], n: int) -> dict:
    """v1.12 M13 帧级批量判决（SPEC-frame-annotation §3.2）：一窗成员帧的闭集标签数组。

    数组按窗内成员序位置对齐。minItems = maxItems 钉死数组长度（judgment_schema /
    segment_window_schema 先例）；长度与索引对齐的后校验在代码侧（first-wins，缺项 ⇒
    该帧取 fallback_class）；同 R1 不用 uniqueItems（帧标签本就允许重复）。

    @param names 帧类名闭集。
    @param n 窗内成员帧数（同时钉死数组长度）。
    @return draft 2020-12 Schema 对象。
    """
    return {"type": "object",
            "properties": {"labels": {"type": "array",
                "items": {"type": "string", "enum": list(names)},
                "minItems": n, "maxItems": n}},
            "required": ["labels"], "additionalProperties": False}


def plan_schema(names: Sequence[str], length: int) -> dict:
    """v1.13 M6 时间流形态·蓝图调用的内部 Schema（裁决·蓝图实现内部 Schema）。

    一条序列的 ``length`` 步计划：每步给出所属帧类（闭集，取自 ``names`` 帧类表）与
    一句话内容要点 ``brief``（供帧实现调用逐位展开）。minItems = maxItems 钉死步数
    （judgment_schema / frame_classify_schema 先例）；不用 uniqueItems——同一帧类在
    一条序列里本就可重复出现（R1 同理，strict 网关硬拒该关键字）。

    @param names 帧类名闭集。
    @param length 本条序列的步数（同时钉死数组长度）。
    @return draft 2020-12 Schema 对象。
    """
    return {"type": "object",
            "properties": {"steps": {"type": "array",
                "items": {"type": "object",
                          "properties": {"frame_class": {"type": "string",
                                                         "enum": list(names)},
                                         "brief": {"type": "string"}},
                          "required": ["frame_class", "brief"],
                          "additionalProperties": False},
                "minItems": length, "maxItems": length}},
            "required": ["steps"], "additionalProperties": False}


def realize_schema(step_schemas: Sequence[dict]) -> dict:
    """v1.13 M6 时间流形态·帧实现调用的内部 Schema（裁决·蓝图实现内部 Schema）。

    逐位包装器：第 i 帧服从蓝图第 i 步帧类的**用户生成 Schema**（纯文本帧由调用方
    传 ``{"type": "string"}``）。``prefixItems`` 是 draft 2020-12 原生关键字
    （jsonschema ≥ 4.21 直接可校验，L2 无需翻译层），``"items": false`` 封尾禁止
    超长数组，minItems = maxItems 再钉一次长度。用户生成 Schema 随 L0 原样透传，
    不做关键字白名单 lint（output.schema 今日同款暴露面）。

    @param step_schemas 逐位步骤 Schema 序列（纯文本帧由调用方传 ``{"type": "string"}``）。
    @return draft 2020-12 Schema 对象。
    """
    steps = list(step_schemas)
    return {"type": "object",
            "properties": {"frames": {"type": "array",
                "prefixItems": steps,
                "minItems": len(steps), "maxItems": len(steps),
                "items": False}},
            "required": ["frames"], "additionalProperties": False}


# ── 引擎本体 ─────────────────────────────────────────────────────────────────

def _extract_object(response: Any) -> tuple[dict | None, bool, str]:
    """从一条 LLMResponse 中取出 (对象, 是否经 L1 修复, 原始文本)。

    厂商原生结构化载荷（Anthropic tool_choice，即 L0）直接采用；否则先按干净文本解析
    （clean 通过），再退到 deterministic_repair（L1 出手）。

    @param response LLM 响应对象（鸭子类型，读取 structured / text）。
    @return 三元组 (对象或 None, 是否经 L1 修复, 原始文本)。
    """
    structured = getattr(response, "structured", None)
    if isinstance(structured, dict):
        return structured, False, json.dumps(structured, ensure_ascii=False)
    raw = response.text or ""
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj, False, raw
    except ValueError:
        # 响应文本不是干净 JSON——这正是 L1 存在的理由，转交确定性修复。
        _logger.debug("response text is not clean JSON; handing it to the L1 deterministic repair")
    repaired = deterministic_repair(raw)
    return repaired, repaired is not None, raw


@dataclass(frozen=True)
class CallScope:
    """一次结构引擎调用的记账与追踪范围（``complete_validated`` 的公开入参形）。

    与私有的 ``_CallContext`` 并排而非合并：本对象是调用方**声明**的取值，
    ``_CallContext`` 还带 ``active`` / ``user_treated`` 两个引擎侧派生量。
    """

    record_ids: tuple[str, ...] = ()    # 本次调用覆盖的记录 id，仅用于 trace 事件
    batch_no: int = 0                   # 批次号，仅用于 trace 事件与日志 extra
    record: Any = None                  # L2.5 回调第二入参（Record.raw），无则 None
    user_treatment: bool | None = None  # 显式待遇门；None ⇒ 按 schema is None 推断


_DEFAULT_SCOPE = CallScope()


@dataclass(frozen=True)
class _CallContext:
    """一次 ``complete_validated`` 调用贯穿 L0→L3 的不变上下文（记账与追踪所需）。"""

    active: dict                   # 本次调用生效的 Schema（用户 Schema 或显式传入的内部 Schema）
    user_treated: bool             # 是否按「用户 Schema 待遇」处理：计 resolved_at 记账并启用 L2.5
    record_ids: tuple[str, ...]    # 本次调用覆盖的记录 id，仅用于 trace 事件
    batch_no: int                  # 批次号，仅用于 trace 事件与日志 extra
    record: Any                    # L2.5 回调的第二入参（Record.raw 原始输入映射），无则 None


@dataclass(frozen=True)
class _Pending:
    """首轮未通过时移交 L3 修复环的现场快照（环内以局部变量推进，不回写本对象）。"""

    raw: str                # 最近一次响应的原始文本，作修复提示词的 [原始输出] 段
    rendered: list[str]     # 面向修复提示词的违规清单（含 L2.5 的 "(validator) " 前缀项）
    summaries: list[str]    # 面向 trace 的违规摘要（仅 JSON Pointer + 关键字，不含数据值）
    usage: Usage            # 截至目前累计的 token 用量
    model: str              # 首轮响应回报的模型名（修复轮不覆盖，随最终结果原样返回）
    attempts: int           # 截至目前发生的调用次数（首轮计 1）


class SchemaEngine:
    """「LLM 调用 → 通过校验的 JSON 对象」的唯一网关（spec 3.8.1）。

    绝不放出任何未过 L2 的对象。
    """

    def __init__(self, user_schema: dict, llm: "LLMClient", cfg,
                 metrics: "MetricsSink | None" = None):
        """装配结构引擎，并在此解析一次 L2.5 用户校验回调。

        @param user_schema 用户输出 Schema（缺省待遇下的生效 Schema）。
        @param llm M9 客户端，承担 L0 与各轮实际调用。
        @param cfg 输出配置（读取 validator / repair_llm / max_repair_attempts）。
        @param metrics 指标汇；None 时不发 trace 事件（validate 等无指标路径）。
        """
        self._user_schema = user_schema
        self._llm = llm
        self._cfg = cfg
        self._metrics = metrics
        self._stats = {"l0_or_clean": 0, "l1": 0, "l3_1": 0, "l3_2": 0, "rejected": 0}
        # L2.5（v1.5 方案 A）：output.validator 钩子只在此解析一次。M1 启动时已校验过
        # 该引用；此处才失败属于部署竞态，直接以其本来的 ValueError 形态暴露。
        self._validator = None
        self._validator_ref = getattr(cfg, "validator", None)
        if self._validator_ref:
            from labelkit.common.extensions.hooks import resolve_hook
            self._validator = resolve_hook(self._validator_ref)

    _CB_PREFIX = "(validator) "

    def _callback_violations(self, obj: dict, record) -> list[str]:
        """执行 L2.5 用户校验回调，违规按 '(validator) <消息>' 渲染。

        回调自身抛出的异常原样上抛——由算子的记录级隔离归为 internal_error（spec 3.8.2）。

        @param obj 已过 L2 的候选对象。
        @param record 回调第二入参（Record.raw 原始输入映射），无则 None。
        @return 带前缀的违规列表；空列表 = 回调放行。
        """
        from labelkit.common.extensions.hooks import normalize_violations
        raw = self._validator(dict(obj), record)          # 防御性拷贝
        return [self._CB_PREFIX + v
                for v in normalize_violations(raw, self._validator_ref)]

    @property
    def user_schema_text(self) -> str:
        """注入提示词的用户 Schema 单行规范文本。

        @return 单行 JSON 文本（不转义非 ASCII，键值分隔符固定）。
        """
        return json.dumps(self._user_schema, ensure_ascii=False, separators=(", ", ": "))

    @property
    def stats(self) -> dict:
        """resolved_at 计数器——只统计「用户待遇」调用（对应 report.schema_engine）。

        v1.13（裁决·M8 显式待遇参数）：口径是「用户待遇族」而非「schema 参数为
        None」——按序列类标注 Schema 显式传 schema 但同属记录级标注调用，照常记账
        （§6.4 恒等式：resolved_at 加总 = 进入 M5 的记录级标注调用数）；帧级标注等
        内部待遇调用仍不计。
        """
        return dict(self._stats)

    def validate_only(self, obj: dict, schema: dict | None = None) -> list[str]:
        """把 L2 当作独立检查使用：渲染「全部」违规为 '<json-pointer>: <描述>'。

        @param obj 待校验对象。
        @param schema 生效 Schema；None ⇒ 用户 Schema。
        @return 确定序排列的违规列表；空列表 = 通过。
        """
        active = self._user_schema if schema is None else schema
        errors = Draft202012Validator(active).iter_errors(obj)
        return sorted(_render_error(e) for e in errors)

    def _validate_full(self, obj: dict, schema: dict) -> tuple[list[str], list[str]]:
        """跑一次 L2，同时产出两份对齐且确定序的清单。

        @param obj 待校验对象。
        @param schema 生效 Schema。
        @return (面向修复提示词的渲染违规, 面向 trace 的脱敏摘要)，两者逐项对齐。
        """
        errors = sorted(Draft202012Validator(schema).iter_errors(obj),
                        key=lambda e: (_json_pointer(e.absolute_path), e.message))
        return [_render_error(e) for e in errors], [_summarize_error(e) for e in errors]

    def _resolve(self, bucket: str, ctx: _CallContext, *,
                 violations: list[str], l1_lossy: bool = False) -> None:
        """定案记账：计桶（仅「用户待遇」调用，v1.13）并为任何非 clean 定案发
        schema.repair 追踪事件。

        @param bucket resolved_at 桶名。
        @param ctx 本次调用的不变上下文（提供 user_treated / record_ids / batch_no）。
        @param violations 面向 trace 的脱敏违规摘要。
        @param l1_lossy 疑似「丢内容」的 L1 修复时为真，附加同名可选 payload 字段
               （7.2 payload 只增不改）。
        @return 无。
        """
        if ctx.user_treated:
            self._stats[bucket] += 1
        if bucket != "l0_or_clean" and self._metrics is not None:
            payload: dict = {"resolved_at": bucket, "violations": violations}
            if l1_lossy:
                payload["l1_lossy"] = True
            self._metrics.event(EV_SCHEMA_REPAIR, stage="schema", batch_no=ctx.batch_no,
                                record_ids=ctx.record_ids, payload=payload)

    def _inspect(self, obj: dict | None, ctx: _CallContext) -> tuple[list[str], list[str]]:
        """把一次响应结果判成违规清单：L2 通过后（且属用户待遇）续跑 L2.5。

        @param obj 本轮取出的对象；None 表示连 L1 都产不出 JSON 对象。
        @param ctx 本次调用的不变上下文。
        @return (渲染违规, trace 摘要)；两者皆空 = 本轮通过。
        """
        if obj is None:
            return [_UNPARSEABLE_VIOLATION], [_UNPARSEABLE_SUMMARY]
        rendered, summaries = self._validate_full(obj, ctx.active)
        if not rendered and ctx.user_treated and self._validator is not None:
            cb = self._callback_violations(obj, ctx.record)          # L2.5
            return cb, list(cb)
        return rendered, summaries

    def _settle_clean(self, obj: dict, raw: str, l1_fixed: bool,
                      ctx: _CallContext) -> None:
        """首轮直接通过时的定案：桶归类 + L1 截断嫌疑告警 + 记账/追踪。

        @param obj 通过校验的对象。
        @param raw 本轮响应原始文本。
        @param l1_fixed 本轮结果是否经 L1 修复而来。
        @param ctx 本次调用的不变上下文。
        @return 无。
        """
        lossy = l1_fixed and l1_repair_is_lossy(obj, raw)
        if lossy:
            # 只报运行态摘要——讲长度，绝不讲内容（P2-5）。
            _logger.warning(
                "L1 repair may have dropped content (unescaped-quote failure mode): the repaired "
                "object keeps only part of the original JSON region, so the structure validates "
                "but text may be missing; see the l1_lossy flag on the schema.repair trace event",
                extra={"stage": "schema", "batch": ctx.batch_no})
        self._resolve(_bucket_for(l1_fixed, 0), ctx, violations=[], l1_lossy=lossy)

    async def complete_validated(self, profile: str, prompt: "PromptBundle",
                                 schema: dict | None = None, *,
                                 scope: CallScope = _DEFAULT_SCOPE,
                                 ) -> tuple[dict, Usage, int, str]:
        """走完 L0 → L1 → L2[→ L2.5] → L3 的四层保障（spec 3.8.2）。

        ``schema`` 传 None ⇒ 用用户 Schema，且本次调用计入 resolved_at 桶；配置了
        output.validator 时以 ``scope.record``（原始输入映射）为第二入参跑 L2.5。

        v1.11：首轮 complete() 可能抛 ContextOverflowError / OutputTruncatedError，
        两者原样上抛给调用方（由算子归类，V27①）；「修复调用」抛出的
        ContextOverflowError 则判本轮失败并直接短路到耗尽（V25①）。

        v1.13（裁决·M8 显式待遇参数）：``scope.user_treatment`` 显式声明本次调用
        是否按「用户 Schema 待遇」处理——None = 按 ``schema is None`` 推断；True =
        计 resolved_at 记账 + 启 L2.5（按序列类标注 Schema 即此形，正面修掉「显式
        Schema = 放弃记账与回调」的弯折）；False = 内部待遇。

        @param profile 本次调用所用 LLM profile 名。
        @param prompt 提示词包。
        @param schema 生效 Schema；None ⇒ 用户 Schema。
        @param scope 本次调用的记账与追踪范围（记录 id / 批次号 / L2.5 入参 /
               显式待遇门）；缺省即无归属记录的内部待遇调用。
        @return (通过校验的对象, 累计用量, 累计调用次数 = 1 + L3 修复调用数, 模型名)。
        @raises SchemaViolation L3 预算耗尽仍未通过；剩余违规全部来自 L2.5 回调时
                置 callback_only=True。
        """
        treated = ((schema is None) if scope.user_treatment is None
                   else scope.user_treatment)
        ctx = _CallContext(
            active=self._user_schema if schema is None else schema,
            user_treated=treated, record_ids=scope.record_ids,
            batch_no=scope.batch_no, record=scope.record)
        # L0：Schema 恒交给客户端；仅当 profile 声明 supports_structured_output 时，
        # 客户端才施加厂商结构化输出机制。
        response = await self._llm.complete(profile, prompt, response_schema=ctx.active)
        obj, l1_fixed, raw = _extract_object(response)
        rendered, summaries = self._inspect(obj, ctx)
        if obj is not None and not rendered:
            self._settle_clean(obj, raw, l1_fixed, ctx)
            return obj, response.usage, 1, response.model
        return await self._repair_until_valid(
            profile, ctx,
            _Pending(raw=raw, rendered=rendered, summaries=summaries,
                     usage=response.usage, model=response.model, attempts=1))

    async def _repair_until_valid(self, profile: str, ctx: _CallContext,
                                  pending: _Pending) -> tuple[dict, Usage, int, str]:
        """L3 有界修复环：每轮修复输出重跑 L1 → L2[→ L2.5]，通过即定案返回。

        @param profile 首轮所用 profile 名（未配置 repair_llm 时沿用）。
        @param ctx 本次调用的不变上下文。
        @param pending 首轮遗留的现场（原始文本 / 违规清单 / 用量 / 轮次）。
        @return (通过校验的对象, 累计用量, 累计调用次数, 首轮模型名)。
        @raises SchemaViolation 修复预算耗尽（或修复调用超预算短路）仍未通过。
        """
        raw, rendered, summaries = pending.raw, pending.rendered, pending.summaries
        total_usage, attempts = pending.usage, pending.attempts
        repair_profile = self._cfg.repair_llm or profile
        for repair_round in range(1, self._cfg.max_repair_attempts + 1):
            repair_prompt = PromptBundle(messages=(
                Message(role="user",
                        parts=(Part(kind="text", text=_build_repair_prompt(raw, rendered)),)),
            ))
            try:
                response = await self._llm.complete(repair_profile, repair_prompt,
                                                    response_schema=ctx.active)
            except ContextOverflowError as overflow:
                # v1.11（V25①，spec §3.3⑨）：修复调用超预算 ⇒ 判本轮失败并短路其余轮次
                # （修复提示词恒定，后续轮必然同样失败）。下方耗尽路径仍按
                # schema_violation / callback_violation 归因拒绝（绝不改记 context_overflow：
                # 修复源文本从不截断，截断会破坏修复语义）。异常在此被吞掉即终结其生命，
                # 故 A7「恰好一次」的 reactive-400 熔断喂入在这里结清（§7.8 矩阵；下方抛出的
                # SchemaViolation 不会再抵达算子的溢出拒绝点）——预检与 finish 来源从不喂入，
                # _breaker_fed 鸭子标志保证幂等。
                _logger.warning("L3 repair call exceeded the context budget; failing this round "
                                "and short-circuiting the remaining repair budget",
                                extra={"stage": "schema", "batch": ctx.batch_no})
                feed_reactive_terminal(overflow, self._metrics)
                break
            total_usage = total_usage + response.usage
            attempts += 1

            obj, _, raw = _extract_object(response)
            new_rendered, new_summaries = self._inspect(obj, ctx)
            if obj is not None and not new_rendered:
                self._resolve(_bucket_for(False, repair_round), ctx, violations=summaries)
                return obj, total_usage, attempts, pending.model
            rendered, summaries = new_rendered, new_summaries

        self._resolve("rejected", ctx, violations=summaries)
        raise SchemaViolation(
            rendered, raw,
            callback_only=bool(rendered) and all(
                v.startswith(self._CB_PREFIX) for v in rendered))

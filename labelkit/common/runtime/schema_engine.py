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
import hashlib
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

import json_repair
from jsonschema import Draft202012Validator

from labelkit.common.contracts.types import Usage
from labelkit.common.errors import (
    ContextOverflowError,
    PostValidatorInvalidError,
    SchemaViolation,
)
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
    jsonschema 的原始消息。文本既进入 L3 修复提示词，也进入 StageError/rejects，
    因此统一使用英文。

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


def _thaw_json(value: Any) -> Any:
    """把冻结 JSON 容器递归复制成可序列化的标准容器。

    @param value MappingProxyType/tuple 也可能出现的 JSON 值
    @return 仅含 dict/list 与 JSON 标量的新值
    """
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw_json(item) for item in value]
    return value


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


def _build_post_repair_prompt(
    original: PromptBundle,
    raw_output: str,
    violations: list[str],
) -> PromptBundle:
    """重放 prompt-safe 上下文并附加可执行后置验证修复回合。

    @param original 首轮完整 prompt-safe 对话。
    @param raw_output 上一候选原始输出。
    @param violations 值受控的后置验证违规清单。
    @return 原对话加 assistant 候选和 user 修复指令。
    """
    repair = _build_post_repair_instruction(violations)
    messages = original.messages + (
        Message(role="assistant", parts=(Part(kind="text", text=raw_output),)),
        Message(role="user", parts=(Part(kind="text", text=repair),)),
    )
    return PromptBundle(messages=messages)


def _build_post_repair_instruction(violations: list[str]) -> str:
    """构造 post-validated 修复回合的新增 user 文本。"""
    numbered = "\n".join(f"{index}. {item}" for index, item in enumerate(violations, 1))
    return f"[违规清单]\n{numbered}\n\n只输出修正后的 JSON。"


def _repair_context_fits(texts: Sequence[str], byte_limit: int | None) -> bool:
    """判断本轮新增修复消息正文是否位于可选 UTF-8 byte 上限内。"""
    if byte_limit is None:
        return True
    return sum(len(item.encode("utf-8")) for item in texts) <= byte_limit


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


_ACTOR_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "object"},
        "identity": {"type": "object"},
        "style": {"type": "object"},
    },
    "required": ["goal", "identity", "style"],
    "additionalProperties": False,
}

def _scenario_state_resource(
    state_schema: Mapping[str, object],
) -> tuple[dict, str]:
    """把用户状态 Schema 作为独立嵌入资源组合进 ScenarioSeed。

    没有 ``$id`` 的 Schema 在嵌入后会把本地 ``#`` 引用错误解析到 ScenarioSeed 根。
    补入内容寻址资源标识可保留完整 Schema 及其本地引用作用域；已有绝对 ``$id``
    原样保留。这里只复制组合对象，不修改 ResolvedConfig 中的用户 Schema。

    @param state_schema 完整用户状态 Schema。
    @return 独立资源副本及其绝对资源标识。
    """
    embedded = _thaw_json(state_schema)
    declared = embedded.get("$id")
    if isinstance(declared, str) and urlsplit(declared).scheme:
        return embedded, declared
    canonical = json.dumps(
        embedded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    resource_id = f"urn:labelkit:state-schema:{digest}"
    embedded["$id"] = resource_id
    return embedded, resource_id


def scenario_seed_schema(actor_names: Sequence[str] | None,
                         state_schema: Mapping[str, object]) -> dict:
    """构造完整 ScenarioSeed 内部 Schema。

    @param actor_names declared actor 闭集；None 表示 instruction-only 动态闭集
    @param state_schema 完整初始状态 Schema
    @return 精确 ScenarioSeed Draft 2020-12 Schema
    """
    actors = _scenario_actor_schema(actor_names)
    state_resource, _state_resource_id = _scenario_state_resource(state_schema)
    return {
        "type": "object",
        "properties": {
            "initial_state": state_resource,
            "actors": actors,
            "shared_facts": {
                "type": "object",
                "properties": {
                    "public": {"type": "object"},
                    "hidden": {"type": "object"},
                },
                "required": ["public", "hidden"],
                "additionalProperties": False,
            },
            "style": {"type": "object"},
            "time_context": {"type": "object"},
        },
        "required": ["initial_state", "actors", "shared_facts", "style", "time_context"],
        "additionalProperties": False,
    }


def _scenario_actor_schema(actor_names: Sequence[str] | None) -> dict:
    """构造 declared 或 instruction-only actor 子 Schema。

    @param actor_names actor 闭集；None 表示一至八个动态 actor
    @return actor object Schema
    """
    if actor_names is None:
        return {"type": "object", "additionalProperties": _ACTOR_SCHEMA,
                "propertyNames": {"type": "string", "minLength": 1},
                "minProperties": 1, "maxProperties": 8}
    return {"type": "object",
            "properties": {name: _ACTOR_SCHEMA for name in actor_names},
            "required": list(actor_names), "additionalProperties": False}


def event_plan_schema(frame_names: Sequence[str], actor_names: Sequence[str]) -> dict:
    """构造 EventPlan 内部 Schema；可执行约束留给后置验证。

    @param frame_names 允许的帧类闭集
    @param actor_names 允许的 actor 闭集
    @return 精确 EventPlan Draft 2020-12 Schema
    """
    with_value = {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["test", "add", "replace"]},
            "path": {"type": "string"},
            "value": {},
        },
        "required": ["op", "path", "value"],
        "additionalProperties": False,
    }
    remove = {
        "type": "object",
        "properties": {"op": {"type": "string", "const": "remove"},
                       "path": {"type": "string"}},
        "required": ["op", "path"], "additionalProperties": False,
    }
    return _event_plan_root(frame_names, actor_names, with_value, remove)


def _event_plan_root(frame_names: Sequence[str], actor_names: Sequence[str],
                     with_value: dict, remove: dict) -> dict:
    """组装 EventPlan 顶层，保持公开构造函数短小。

    @param frame_names 帧类闭集
    @param actor_names actor 闭集
    @param with_value 带 value 的 patch 操作 Schema
    @param remove remove 操作 Schema
    @return EventPlan 顶层 Schema
    """
    return {
        "type": "object",
        "properties": {
            "frame_class": {"type": "string", "enum": list(frame_names)},
            "actor": {"type": "string", "enum": list(actor_names)},
            "intent": {"type": "string"},
            "patch": {"type": "array", "items": {"oneOf": [with_value, remove]},
                      "minItems": 2},
        },
        "required": ["frame_class", "actor", "intent", "patch"],
        "additionalProperties": False,
    }


def semantic_evaluation_schema() -> dict:
    """构造六项盲审判定内部 Schema。

    @return 精确 SemanticEvaluation Draft 2020-12 Schema
    """
    codes = ["causal_inconsistency", "actor_knowledge_violation", "goal_inconsistency",
             "temporal_implausibility", "cross_frame_inconsistency", "unrealistic"]
    bools = ("causal_consistency", "actor_knowledge", "goal_consistency",
             "temporal_plausibility", "cross_frame_consistency", "realism")
    properties = {name: {"type": "boolean"} for name in bools}
    properties["reason_codes"] = {"type": "array",
                                  "items": {"type": "string", "enum": codes}}
    return {"type": "object", "properties": properties,
            "required": [*bools, "reason_codes"], "additionalProperties": False}


def noise_semantic_evaluation_schema() -> dict:
    """构造四项 noise 独立判定内部 Schema。

    @return 精确 NoiseSemanticEvaluation Draft 2020-12 Schema
    """
    codes = [
        "related_to_declared_task", "executable_task_present", "unrealistic",
        "planned_noise_topic_mismatch",
    ]
    bools = (
        "unrelated_to_declared_tasks", "no_executable_task", "realism",
        "matches_planned_topic",
    )
    properties = {name: {"type": "boolean"} for name in bools}
    properties["reason_codes"] = {"type": "array",
                                  "items": {"type": "string", "enum": codes}}
    return {"type": "object", "properties": properties,
            "required": [*bools, "reason_codes"], "additionalProperties": False}


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
    repair_context_bytes: int | None = None  # 当前调用单轮新增 L3 正文 byte 上限


_DEFAULT_SCOPE = CallScope()


@dataclass(frozen=True)
class _CallContext:
    """一次 ``complete_validated`` 调用贯穿 L0→L3 的不变上下文（记账与追踪所需）。"""

    active: dict                   # 本次调用生效的 Schema（用户 Schema 或显式传入的内部 Schema）
    user_treated: bool             # 是否按「用户 Schema 待遇」处理：计 resolved_at 记账并启用 L2.5
    record_ids: tuple[str, ...]    # 本次调用覆盖的记录 id，仅用于 trace 事件
    batch_no: int                  # 批次号，仅用于 trace 事件与日志 extra
    record: Any                    # L2.5 回调的第二入参（Record.raw 原始输入映射），无则 None
    repair_context_bytes: int | None = None  # 单轮新增 L3 正文 byte 上限


@dataclass(frozen=True)
class _Pending:
    """首轮未通过时移交 L3 修复环的现场快照（环内以局部变量推进，不回写本对象）。"""

    raw: str                # 最近一次响应的原始文本，作修复提示词的 [原始输出] 段
    rendered: list[str]     # 面向修复提示词的违规清单（含 L2.5 的 "(validator) " 前缀项）
    summaries: list[str]    # 面向 trace 的违规摘要（仅 JSON Pointer + 关键字，不含数据值）
    usage: Usage            # 截至目前累计的 token 用量
    model: str              # 首轮响应回报的模型名（修复轮不覆盖，随最终结果原样返回）
    attempts: int           # 截至目前发生的调用次数（首轮计 1）


@dataclass(frozen=True)
class _PostInspection:
    """一次 L2 与 request-local 后置验证的归一结果。"""

    rendered: list[str]             # 可进入 L3 的违规文本
    summaries: list[str]            # trace 使用的脱敏摘要
    execution: object | None        # 唯一成功执行证明
    terminal_kind: str | None       # invalid/exception 终态分类


@dataclass(frozen=True)
class _PostPending:
    """后置验证 L3 修复环的现场快照。"""

    raw: str                        # 最近一轮原始输出
    rendered: list[str]             # 最近一轮违规文本
    summaries: list[str]            # 最近一轮脱敏摘要
    usage: Usage                    # 累计 usage
    model: str                      # 首轮模型名
    attempts: int                   # 累计调用数


@dataclass(frozen=True)
class _PostRepairRequest:
    """一轮 post-validated L3 调用的参数对象。"""

    profile: str                    # 修复 profile
    request: object                 # 后置验证调用请求
    rendered: list[str]             # 仅违规文本
    raw: str                        # 最近原始输出
    context: _CallContext           # trace 范围


_POST_PREFIX = "(post-validator) "


def _inspect_post_candidate(obj: dict | None, schema: Mapping[str, object],
                            validator: Callable[[Mapping[str, object]], object]) -> _PostInspection:
    """对一个候选先跑 L2，再恰好一次执行后置验证器。

    @param obj L1 后候选；None 表示不可解析
    @param schema 完整内部 Schema
    @param validator request-local 后置验证器
    @return 可修复违规、成功证明或无内容终态分类
    """
    if obj is None:
        return _PostInspection([_UNPARSEABLE_VIOLATION], [_UNPARSEABLE_SUMMARY], None, None)
    errors = sorted(Draft202012Validator(schema).iter_errors(obj),
                    key=lambda item: (_json_pointer(item.absolute_path), item.message))
    if errors:
        return _PostInspection([_render_error(item) for item in errors],
                               [_summarize_error(item) for item in errors], None, None)
    try:
        result = validator(obj)
    except PostValidatorInvalidError:
        _logger.warning("Post-validator returned an invalid value", extra={"stage": "schema"})
        return _PostInspection([], [], None, "post_validator_invalid")
    except Exception:
        _logger.warning("Post-validator raised an exception", extra={"stage": "schema"})
        return _PostInspection([], [], None, "post_validator_exception")
    return _normalize_post_result(result)


def _normalize_post_result(result: object) -> _PostInspection:
    """校验 PostValidationResult 的两种且仅两种合法形状。

    @param result 后置验证器原始返回值
    @return 成功证明、可修复违规或 invalid 终态
    """
    from labelkit.common.contracts.generation import EventExecution, PostValidationResult

    if not isinstance(result, PostValidationResult):
        return _PostInspection([], [], None, "post_validator_invalid")
    violations = result.violations
    valid_strings = isinstance(violations, tuple) and all(
        isinstance(item, str) and bool(item.strip()) for item in violations)
    if not valid_strings:
        return _PostInspection([], [], None, "post_validator_invalid")
    if not violations and isinstance(result.event_execution, EventExecution):
        return _PostInspection([], [], result.event_execution, None)
    if violations and result.event_execution is None:
        rendered = [_POST_PREFIX + item for item in violations]
        summaries = ["post-validator" for _ in violations]
        return _PostInspection(rendered, summaries, None, None)
    return _PostInspection([], [], None, "post_validator_invalid")


def _raise_post_terminal(kind: str) -> None:
    """以无异常文本、无候选内容的 SchemaViolation 终结当前候选。

    @param kind post_validator_invalid 或 post_validator_exception
    @raises SchemaViolation 始终抛出
    """
    raise SchemaViolation([kind], "")


class SchemaEngine:
    """「LLM 调用 → 通过校验的 JSON 对象」的唯一网关（spec 3.8.1）。

    绝不放出任何未过 L2 的对象。
    """

    def __init__(self, user_schema: dict, llm: "LLMClient", cfg,
                 metrics: "MetricsSink | None" = None, *,
                 validator: "Callable[[dict, Any], Any] | None" = None):
        """装配结构引擎；L2.5 用户校验回调由装配方以冻结 callable 传入。

        v1.17（Wave 2b，CONTRACTS §7.19.3 / rule 70）：删除了按 ``cfg.validator``
        字符串二次 resolve 的旧腿——装配方（orchestration 装配面）从 M1 冻结载体
        ``ResolvedConfig.validation_hooks.output.target`` 取 callable 传入；引擎内
        不再 import 解析器。

        @param user_schema 用户输出 Schema（缺省待遇下的生效 Schema）。
        @param llm M9 客户端，承担 L0 与各轮实际调用。
        @param cfg 输出配置（读取 repair_llm / max_repair_attempts）。
        @param metrics 指标汇；None 时不发 trace 事件（validate 等无指标路径）。
        @param validator L2.5 回调的冻结 callable（``fn(obj, record) -> list[str]``）；
               None = 未配置 output.validator。
        """
        self._user_schema = user_schema
        self._llm = llm
        self._cfg = cfg
        self._metrics = metrics
        self._stats = {"l0_or_clean": 0, "l1": 0, "l3_1": 0, "l3_2": 0, "rejected": 0}
        # L2.5（v1.5 方案 A）：回调以冻结 callable 直达；错误定位标签从 callable
        # 自身派生（只含函数名，不含任何配置原文或数据面）。
        self._validator = validator
        self._validator_ref = (getattr(validator, "__qualname__", None)
                               or "<validator>")

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

        口径是「用户待遇族」而非「schema 参数为 None」：按类标注显式传 schema
        但仍属记录级标注调用，照常记账；帧级标注等内部待遇调用不计。

        @return 用户待遇调用的各类计数副本。
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
        """定案记账：仅为「用户待遇」调用计桶，并为任何非 clean 定案发
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

        ``scope.user_treatment`` 显式声明是否按「用户 Schema 待遇」处理：None 按
        ``schema is None`` 推断；True 计 resolved_at 并启用 L2.5；False 为内部待遇。

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
        active = _thaw_json(self._user_schema if schema is None else schema)
        ctx = _CallContext(
            active=active,
            user_treated=treated, record_ids=scope.record_ids,
            batch_no=scope.batch_no, record=scope.record,
            repair_context_bytes=scope.repair_context_bytes)
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

    async def complete_post_validated(
        self,
        request: PostValidatedCallRequest,
    ) -> ValidatedGenerationCall:
        """对每个 L2 候选恰验证一次并返回匹配的执行证明。

        @param request 含 request-local 后置验证器的完整调用请求
        @return 已验证对象及该同一候选的执行证明
        """
        active = _thaw_json(request.schema)
        ctx = _CallContext(active=active, user_treated=False,
                           record_ids=request.scope.record_ids,
                           batch_no=request.scope.batch_no, record=None,
                           repair_context_bytes=request.scope.repair_context_bytes)
        response = await self._llm.complete(
            request.profile, request.prompt, response_schema=active)
        obj, l1_fixed, raw = _extract_object(response)
        inspected = _inspect_post_candidate(obj, active, request.post_validator)
        if inspected.terminal_kind is not None:
            _raise_post_terminal(inspected.terminal_kind)
        if obj is not None and inspected.execution is not None:
            from labelkit.common.contracts.generation import ValidatedGenerationCall

            bucket = _bucket_for(l1_fixed, 0)
            self._resolve(bucket, ctx, violations=[])
            return ValidatedGenerationCall(obj, inspected.execution, bucket,
                                           response.usage, 1, response.model)
        pending = _PostPending(raw, inspected.rendered, inspected.summaries,
                               response.usage, response.model, 1)
        return await self._repair_post_validated(request, ctx, pending)

    async def _repair_post_validated(
        self,
        request: PostValidatedCallRequest,
        ctx: _CallContext,
        pending: _PostPending,
    ) -> ValidatedGenerationCall:
        """在完整 Schema 与纯违规文本上执行最多两轮 L3。

        @param request 原始 post-validated 请求
        @param ctx 内部待遇调用上下文
        @param pending 首轮未通过现场
        @return 成功候选及同一次后置执行证明
        @raises SchemaViolation 修复预算耗尽或 post-validator 终态
        """
        raw, rendered, summaries = pending.raw, pending.rendered, pending.summaries
        usage, attempts = pending.usage, pending.attempts
        repair_profile = self._cfg.repair_llm or request.profile
        for repair_round in range(1, min(self._cfg.max_repair_attempts, 2) + 1):
            repair = _PostRepairRequest(repair_profile, request, rendered, raw, ctx)
            response = await self._post_repair_call(repair)
            if response is None:
                break
            usage, attempts = usage + response.usage, attempts + 1
            obj, _, raw = _extract_object(response)
            schema = _thaw_json(request.schema)
            inspected = _inspect_post_candidate(obj, schema, request.post_validator)
            if inspected.terminal_kind is not None:
                _raise_post_terminal(inspected.terminal_kind)
            if obj is not None and inspected.execution is not None:
                from labelkit.common.contracts.generation import ValidatedGenerationCall

                bucket = f"l3_{repair_round}"
                self._resolve(bucket, ctx, violations=summaries)
                return ValidatedGenerationCall(obj, inspected.execution, bucket, usage,
                                               attempts, pending.model)
            rendered, summaries = inspected.rendered, inspected.summaries
        raise SchemaViolation(rendered, raw)

    async def _post_repair_call(self, repair: _PostRepairRequest):
        """执行一轮 post-validated L3 调用并归一 overflow 短路。

        @param repair 修复 profile、请求、违规、原始输出与 trace 范围
        @return LLMResponse；overflow 时为 None
        """
        instruction = _build_post_repair_instruction(repair.rendered)
        if not _repair_context_fits(
            (repair.raw, instruction), repair.context.repair_context_bytes
        ):
            _logger.warning("L3 repair context exceeds the frozen byte limit",
                            extra={"stage": "schema", "batch": repair.context.batch_no})
            return None
        prompt = _build_post_repair_prompt(
            repair.request.prompt, repair.raw, repair.rendered)
        try:
            schema = _thaw_json(repair.request.schema)
            return await self._llm.complete(repair.profile, prompt, response_schema=schema)
        except ContextOverflowError as overflow:
            _logger.warning("L3 repair call exceeded the context budget",
                            extra={"stage": "schema", "batch": repair.context.batch_no})
            feed_reactive_terminal(overflow, self._metrics)
            return None

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
            response = await self._generic_repair_call(
                repair_profile, ctx, raw, rendered,
            )
            if response is None:
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

    async def _generic_repair_call(self, profile, ctx, raw, rendered):
        """执行一轮普通 L3 修复并把超预算归一为 None。"""
        repair_text = _build_repair_prompt(raw, rendered)
        if not _repair_context_fits((repair_text,), ctx.repair_context_bytes):
            _logger.warning("L3 repair context exceeds the frozen byte limit",
                            extra={"stage": "schema", "batch": ctx.batch_no})
            return None
        prompt = PromptBundle(messages=(
            Message(role="user", parts=(Part(kind="text", text=repair_text),)),
        ))
        try:
            return await self._llm.complete(profile, prompt, response_schema=ctx.active)
        except ContextOverflowError as overflow:
            # 修复提示词恒定，后续轮必然同败；被吞异常在这里恰好一次喂入熔断。
            _logger.warning(
                "L3 repair call exceeded the context budget; failing this round and "
                "short-circuiting the remaining repair budget",
                extra={"stage": "schema", "batch": ctx.batch_no},
            )
            feed_reactive_terminal(overflow, self._metrics)
            return None


# 公开签名的运行期注解绑定；载体模块同时把 SchemaEngine 注回自身命名空间。
from labelkit.common.contracts.generation import (  # noqa: E402
    PostValidatedCallRequest,
    ValidatedGenerationCall,
)

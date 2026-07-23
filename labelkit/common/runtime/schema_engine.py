"""M8 schema engine (spec 3.8, CONTRACTS.md §7.7).

Four-layer structural guarantee for every LLM-produced object:

- L0: pass ``response_schema`` through to ``LLMClient.complete()``; the client decides
  the vendor mechanics (OpenAI ``response_format`` / Anthropic forced tool call) and
  ignores it when the profile lacks ``supports_structured_output``. L0 never exempts L2.
- L1: deterministic repair — a PURE module-level function ``deterministic_repair``:
  strip Markdown code fences -> first balanced-brace substring -> ``json_repair.loads``.
- L2: ``jsonschema.Draft202012Validator.iter_errors`` collecting ALL violations with
  JSON Pointer paths.
- L3: bounded LLM repair loop (prompt per CONTRACTS.md §10.6 / spec 3.8.4), budget
  ``output.max_repair_attempts``; each repair output re-runs L1 -> L2. Exhaustion raises
  ``SchemaViolation(errors, raw_last_output)``.
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

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


# ── L1: deterministic repair (pure function, no side effects) ───────────────

def _strip_markdown_fences(text: str) -> str:
    """Step ①: strip Markdown code fences — anchored: the text is treated as fenced
    only when its first non-whitespace characters open a fence. The opening fence
    line and a trailing closing fence (the last ``` when it closes the text) are
    removed; everything in between — including ``` embedded inside JSON string
    values — is preserved for the string-aware balanced-brace scan of step ②.
    Non-anchored text passes through untouched (step ② isolates any JSON in it)."""
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
    """Step ②: first balanced-brace substring, brace-aware inside double-quoted
    strings (escapes honored). Unbalanced (truncated) input falls back to the suffix
    starting at the first '{' so json_repair can complete it; no '{' at all -> None."""
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
    """L1 (spec 3.8.2), in order: strip Markdown fences -> first balanced-brace
    substring -> ``json_repair.loads``. When the fence-derived candidate yields no
    JSON object, the same scan re-runs over the original text so JSON living outside
    an anchored fence (prose with inline fences before it, JSON in a later fenced
    block) still repairs at L1. Returns the parsed object, or None when every step
    fails to yield a JSON object. Pure function — unit-testable exhaustively."""
    fence_stripped = _strip_markdown_fences(text)
    sources = [fence_stripped] if fence_stripped == text else [fence_stripped, text]
    for source in sources:
        candidate = _first_balanced_braces(source)
        if candidate is None:
            candidate = source
        try:
            obj = json_repair.loads(candidate)
        except Exception:
            continue
        if isinstance(obj, dict):
            return obj
    return None


_L1_LOSS_RATIO = 0.8       # repaired object must retain >= 80% of the candidate…
_L1_LOSS_MIN_CHARS = 40    # …unless fewer than this many chars went missing.


def l1_repair_is_lossy(obj: dict, raw: str) -> bool:
    """Heuristic for the json-repair truncation failure mode (E2E finding P2-5):
    an unescaped inner quote makes ``json_repair`` end a string early and DROP
    the remainder — the result parses and may even validate, but content is
    silently gone. Flag an L1 repair whose re-serialization is much shorter
    than the brace-region it was repaired from. Pure function.

    False-positive guards (review finding): a candidate that already parses
    cleanly (fence-stripping was the only repair) is lossless by definition;
    and the length baseline must not be inflated by pretty-print whitespace or
    ``\\uXXXX`` escapes, so the retained side takes the larger of the escaped /
    unescaped serializations and the candidate side is whitespace-stripped."""
    candidate = _first_balanced_braces(_strip_markdown_fences(raw))
    if candidate is None:
        return False
    try:
        if json.loads(candidate) == obj:
            return False                    # fence-only repair: nothing dropped
    except ValueError:
        pass
    kept = max(len(json.dumps(obj, ensure_ascii=False, separators=(",", ":"))),
               len(json.dumps(obj, ensure_ascii=True, separators=(",", ":"))))
    base = len(re.sub(r"\s", "", candidate))
    lost = base - kept
    return lost > _L1_LOSS_MIN_CHARS and kept < _L1_LOSS_RATIO * base


# ── L2 rendering helpers ─────────────────────────────────────────────────────

def _json_pointer(path: Any) -> str:
    """RFC 6901 JSON Pointer from a jsonschema error path (deque of str|int)."""
    return "".join(
        "/" + str(token).replace("~", "~0").replace("/", "~1") for token in path
    )


def _render_error(error: Any) -> str:
    """One violation as '<json-pointer>: <message>'. Enum violations use the exact
    Chinese expected-vs-actual wording of the spec 3.8.4 worked example; other
    keywords carry the raw jsonschema message."""
    pointer = _json_pointer(error.absolute_path)
    if error.validator == "enum":
        expected = json.dumps(list(error.validator_value), ensure_ascii=False)
        actual = json.dumps(error.instance, ensure_ascii=False)
        description = f"期望为枚举 {expected} 之一，实际值为 {actual}"
    else:
        description = error.message
    return f"{pointer}: {description}"


def _summarize_error(error: Any) -> str:
    """Trace-payload form: JSON Pointer + violated keyword only, NO data values."""
    return f"{_json_pointer(error.absolute_path)}: {error.validator}"


# Rendered violation used when even L1 cannot produce a JSON object (root pointer).
_UNPARSEABLE_VIOLATION = ": 输出无法解析为 JSON 对象"
_UNPARSEABLE_SUMMARY = ": unparseable"


# ── L3 repair prompt (CONTRACTS.md §10.6, spec 3.8.4 — byte-exact) ──────────

def _build_repair_prompt(raw_output: str, violations: list[str]) -> str:
    """Single user message: [原始输出] section, [违规清单] 1-based numbered list,
    closing instruction. Deterministic string assembly, no rewriting."""
    numbered = "\n".join(f"{i}. {v}" for i, v in enumerate(violations, 1))
    return f"[原始输出]\n{raw_output}\n\n[违规清单]\n{numbered}\n\n只输出修正后的 JSON。"


# ── resolved_at bucketing (pure) ─────────────────────────────────────────────

def _bucket_for(l1_fixed: bool, repair_round: int) -> str:
    """Bucket name for a SUCCESSFUL resolution. repair_round = number of the L3 round
    that produced the passing object (0 = first response passed without L3).
    Clean first-response pass (L0 active or trivially parsed) -> 'l0_or_clean';
    L1 had to fix something -> 'l1'; L3 round 1/2 -> 'l3_1'/'l3_2' (rounds beyond 2
    fold into 'l3_2' — the frozen stats dict has no further keys)."""
    if repair_round <= 0:
        return "l1" if l1_fixed else "l0_or_clean"
    return "l3_1" if repair_round == 1 else "l3_2"


# ── Internal schemas (CONTRACTS.md §10.7 — exact JSON) ───────────────────────

def judgment_schema(criteria_keys: list[str], with_reason: bool) -> dict:
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


VERDICT_SCHEMA = {          # critiques BEFORE verdict: reason-then-conclusion (spec 3.8.3 note)
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
    return {"type": "object",
            "properties": {"samples": {"type": "array", "items": {"type": "string"},
                                       "minItems": num_per_call, "maxItems": num_per_call}},
            "required": ["samples"], "additionalProperties": False}


def segment_window_schema(frame_count: int, with_reason: bool) -> dict:
    # v1.8 M14 (spec §3.2.2 / CONTRACTS §10.7): per-frame closed-set relation verdicts
    # for one sliding window. minItems=maxItems pins the array length (judgment_schema
    # precedent); index alignment is enforced code-side (first-wins, default "continues")
    # — schemas cannot express a permutation (R1: no uniqueItems).
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
    # v1.8 M15 (spec 3.15.3 / CONTRACTS §10.7): one adjacent-pair action verdict.
    # All keys required with nullable unions — OpenAI strict mode rejects optional
    # properties (S7, same lesson as R1); ["string","null"] is the sanctioned form.
    # Enum order is frozen (S15: AndroidControl full set ∪ UI-TARS-mobile + other).
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
    # v1.9 M16 (spec 3.16 / CONTRACTS §10.7): one thread-stitch verdict per candidate.
    # All keys required with a nullable thread_ref (strict-safe, S7 lesson); thread_ref
    # is the 1-based ordinal of a presented pool card (range-checked code-side — schemas
    # cannot see the pool size); confidence is trace observation ONLY, never a gate (T9).
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
    # v1.8 M7 stream variant (spec 3.7.2 / CONTRACTS §10.7): critiques kept verbatim
    # (the repair feed-back loop is built on them) + typed defect table; opinions/defects
    # before verdict (reason-then-conclusion, VERDICT_SCHEMA precedent). All keys
    # required, members/position nullable (strict-safe, S7). "fail" with an empty
    # defects array is normalized code-side to a default label_mismatch entry.
    # v1.9 (T15): six kinds — wrong_stitch appended (mark-only + fail routing).
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
    # v1.7 R1: deliberately NO uniqueItems (OpenAI strict mode rejects it; L0 passes the
    # schema through unconditionally) — duplicate labels are deterministically de-duplicated
    # by classify-side normalization AFTER M8 validation.
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


# ── The engine ───────────────────────────────────────────────────────────────

def _extract_object(response: Any) -> tuple[dict | None, bool, str]:
    """(obj, l1_fixed, raw_text) from an LLMResponse. A native structured payload
    (Anthropic tool_choice, L0) is used directly; otherwise the text is parsed
    trivially first (clean pass) and via deterministic_repair second (L1 fix)."""
    structured = getattr(response, "structured", None)
    if isinstance(structured, dict):
        return structured, False, json.dumps(structured, ensure_ascii=False)
    raw = response.text or ""
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj, False, raw
    except ValueError:
        pass
    repaired = deterministic_repair(raw)
    return repaired, repaired is not None, raw


class SchemaEngine:
    """Sole gateway 'LLM call -> validated JSON object' (spec 3.8.1). Never releases
    an object that has not passed L2."""

    def __init__(self, user_schema: dict, llm: "LLMClient", cfg,
                 metrics: "MetricsSink | None" = None):
        self._user_schema = user_schema
        self._llm = llm
        self._cfg = cfg
        self._metrics = metrics
        self._stats = {"l0_or_clean": 0, "l1": 0, "l3_1": 0, "l3_2": 0, "rejected": 0}
        # L2.5 (v1.5 plan A): the output.validator hook, resolved once. M1 has
        # already validated the reference at startup; a late failure here is a
        # deployment race and surfaces as the ValueError it is.
        self._validator = None
        self._validator_ref = getattr(cfg, "validator", None)
        if self._validator_ref:
            from labelkit.common.extensions.hooks import resolve_hook
            self._validator = resolve_hook(self._validator_ref)

    _CB_PREFIX = "(validator) "

    def _callback_violations(self, obj: dict, record) -> list[str]:
        """L2.5: run the user hook; violations rendered '(validator) <msg>'.
        Hook exceptions propagate — the stage's record-level isolation turns
        them into internal_error (spec 3.8.2)."""
        from labelkit.common.extensions.hooks import normalize_violations
        raw = self._validator(dict(obj), record)          # defensive copy
        return [self._CB_PREFIX + v
                for v in normalize_violations(raw, self._validator_ref)]

    @property
    def user_schema_text(self) -> str:
        """Canonical single-line user-schema text injected into prompts."""
        return json.dumps(self._user_schema, ensure_ascii=False, separators=(", ", ": "))

    @property
    def stats(self) -> dict:
        """resolved_at counters — user-schema calls only (report.schema_engine)."""
        return dict(self._stats)

    def validate_only(self, obj: dict, schema: dict | None = None) -> list[str]:
        """L2 as a standalone check: ALL violations rendered '<json-pointer>: <message>',
        deterministically ordered. Empty list = valid."""
        active = self._user_schema if schema is None else schema
        errors = Draft202012Validator(active).iter_errors(obj)
        return sorted(_render_error(e) for e in errors)

    def _validate_full(self, obj: dict, schema: dict) -> tuple[list[str], list[str]]:
        """(rendered violations, trace summaries), aligned and deterministically ordered."""
        errors = sorted(Draft202012Validator(schema).iter_errors(obj),
                        key=lambda e: (_json_pointer(e.absolute_path), e.message))
        return [_render_error(e) for e in errors], [_summarize_error(e) for e in errors]

    def _resolve(self, bucket: str, *, is_user_schema: bool,
                 record_ids: tuple[str, ...], batch_no: int,
                 violations: list[str], l1_lossy: bool = False) -> None:
        """Count the bucket (user-schema calls only) and emit the schema.repair trace
        event for any non-clean resolution. ``l1_lossy`` adds an optional payload
        field (7.2 payload 只增不改) flagging a suspected content-dropping repair."""
        if is_user_schema:
            self._stats[bucket] += 1
        if bucket != "l0_or_clean" and self._metrics is not None:
            payload: dict = {"resolved_at": bucket, "violations": violations}
            if l1_lossy:
                payload["l1_lossy"] = True
            self._metrics.event(EV_SCHEMA_REPAIR, stage="schema", batch_no=batch_no,
                                record_ids=record_ids, payload=payload)

    async def complete_validated(self, profile: str, prompt: "PromptBundle",
                                 schema: dict | None = None, *,
                                 record_ids: tuple[str, ...] = (),
                                 batch_no: int = 0,
                                 record: "Mapping | None" = None,
                                 ) -> tuple[dict, Usage, int, str]:
        """L0 -> L1 -> L2 [-> L2.5] -> L3 (spec 3.8.2). schema=None -> user schema (and
        the call counts toward the resolved_at buckets; the output.validator hook, when
        configured, runs as L2.5 with ``record`` = the raw input mapping). Returns
        (validated_obj, total_usage, attempts, model) where attempts = 1 + L3 repair
        calls. Raises SchemaViolation once the L3 budget is exhausted — with
        callback_only=True when every remaining violation came from the hook.
        v1.11: the INITIAL complete() may raise ContextOverflowError /
        OutputTruncatedError — both propagate to the caller untouched (operators
        classify them, V27①); a ContextOverflowError from a REPAIR call fails
        that round and short-circuits straight to exhaustion (V25①)."""
        is_user_schema = schema is None
        active = self._user_schema if schema is None else schema
        use_hook = is_user_schema and self._validator is not None

        # L0: always hand the schema to the client; it applies vendor structured-output
        # mechanics only when the profile declares supports_structured_output.
        response = await self._llm.complete(profile, prompt, response_schema=active)
        total_usage: Usage = response.usage
        model: str = response.model
        attempts = 1

        obj, l1_fixed, raw = _extract_object(response)
        if obj is not None:
            rendered, summaries = self._validate_full(obj, active)
            if not rendered and use_hook:
                cb = self._callback_violations(obj, record)     # L2.5
                rendered, summaries = cb, list(cb)
            if not rendered:
                bucket = _bucket_for(l1_fixed, 0)
                lossy = l1_fixed and l1_repair_is_lossy(obj, raw)
                if lossy:
                    # Operational summary only — lengths, never content (P2-5).
                    _logger.warning(
                        "L1 修复疑似截断了内容（未转义引号类故障）：修复后仅保留原始 JSON "
                        "区段的一部分，字段结构合法但文本可能残缺；详见 trace schema.repair "
                        "事件的 l1_lossy 标记",
                        extra={"stage": "schema", "batch": batch_no})
                self._resolve(bucket, is_user_schema=is_user_schema,
                              record_ids=record_ids, batch_no=batch_no, violations=[],
                              l1_lossy=lossy)
                return obj, total_usage, attempts, model
        else:
            rendered, summaries = [_UNPARSEABLE_VIOLATION], [_UNPARSEABLE_SUMMARY]

        # L3 repair loop: each repair output re-runs L1 -> L2.
        repair_profile = self._cfg.repair_llm or profile
        for repair_round in range(1, self._cfg.max_repair_attempts + 1):
            repair_prompt = PromptBundle(messages=(
                Message(role="user",
                        parts=(Part(kind="text", text=_build_repair_prompt(raw, rendered)),)),
            ))
            try:
                response = await self._llm.complete(repair_profile, repair_prompt,
                                                    response_schema=active)
            except ContextOverflowError as overflow:
                # v1.11 (V25①, spec §3.3⑨): a repair call over budget counts
                # the round as failed AND short-circuits the remaining rounds —
                # the repair prompt is constant, so every later round fails
                # identically. The exhaustion path below keeps the reject
                # attribution schema_violation/callback_violation (never
                # context_overflow; the repair source text is never truncated —
                # truncating it would break the repair semantics). The swallow
                # ends the exception's life here, so the A7 exactly-once
                # reactive-400 breaker feed settles NOW (§7.8 matrix; the
                # SchemaViolation raised below never reaches an operator
                # overflow reject site) — precheck/finish-origin never feed,
                # and the _breaker_fed duck flag keeps this idempotent.
                feed_reactive_terminal(overflow, self._metrics)
                break
            total_usage = total_usage + response.usage
            attempts += 1

            obj, _, raw = _extract_object(response)
            if obj is not None:
                new_rendered, new_summaries = self._validate_full(obj, active)
                if not new_rendered and use_hook:
                    cb = self._callback_violations(obj, record)  # L2.5 (each round)
                    new_rendered, new_summaries = cb, list(cb)
                if not new_rendered:
                    bucket = _bucket_for(False, repair_round)
                    self._resolve(bucket, is_user_schema=is_user_schema,
                                  record_ids=record_ids, batch_no=batch_no,
                                  violations=summaries)
                    return obj, total_usage, attempts, model
                rendered, summaries = new_rendered, new_summaries
            else:
                rendered, summaries = [_UNPARSEABLE_VIOLATION], [_UNPARSEABLE_SUMMARY]

        self._resolve("rejected", is_user_schema=is_user_schema,
                      record_ids=record_ids, batch_no=batch_no, violations=summaries)
        raise SchemaViolation(
            rendered, raw,
            callback_only=bool(rendered) and all(
                v.startswith(self._CB_PREFIX) for v in rendered))

"""M8 integration tests — REAL endpoint (glm-5.2 via api.z.ai, anthropic protocol).

No mock LLMs (project policy). SchemaEngine is exercised end-to-end through
complete_validated against the live endpoint. Uses the real labelkit.common.runtime.llm_client once
M9 lands; until then a minimal REAL-HTTP client implementing the exact contract
surface M8 needs (async complete(profile, prompt, response_schema) -> LLMResponse-shaped
object, Anthropic tool_choice structured output with tool name "emit") stands in, so
the four-layer path is live either way.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx
import pytest

from labelkit.common.runtime.schema_engine import Message, Part, PromptBundle, SchemaEngine
from labelkit.common.config.model import LLMProfile, OutputConfig
from labelkit.common.contracts.types import Usage

from tests.conftest import ZAI_BASE_URL, ZAI_KEY_ENV, ZAI_MODEL

pytestmark = pytest.mark.integration

USER_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string",
                   "enum": ["writing_assist", "qa", "translation", "chitchat", "other"]},
        "topic": {"type": "string"},
        "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]},
    },
    "required": ["intent", "topic", "difficulty"],
    "additionalProperties": False,
}


def make_profile(name: str, structured: bool) -> LLMProfile:
    return LLMProfile(
        name=name,
        provider="anthropic",
        base_url=ZAI_BASE_URL,
        model=ZAI_MODEL,
        api_key_env=ZAI_KEY_ENV,
        max_concurrency=2,
        timeout_s=120,
        max_retries=2,
        supports_structured_output=structured,
        max_output_tokens=400,
        temperature=0.0,
        api_key=os.environ.get(ZAI_KEY_ENV, ""),
    )


try:
    from labelkit.common.runtime.llm_client import LLMClient as _RealLLMClient

    def make_client(profiles: dict[str, LLMProfile]):
        return _RealLLMClient(profiles, {})
except ImportError:
    @dataclass(frozen=True)
    class _Resp:
        text: str
        structured: dict | None
        usage: Usage
        model: str
        latency_ms: int

    class _MinimalRealAnthropicClient:
        """Real HTTP calls to the real endpoint — contract subset of M9 LLMClient
        (3.9.3: POST {base_url}/v1/messages, x-api-key + anthropic-version headers,
        structured output = single tool "emit" forced via tool_choice)."""

        def __init__(self, profiles: dict[str, LLMProfile]):
            self._profiles = profiles

        async def complete(self, profile: str, prompt, response_schema: dict | None = None):
            p = self._profiles[profile]
            system_chunks: list[str] = []
            messages: list[dict] = []
            for msg in prompt.messages:
                text = "\n".join(part.text for part in msg.parts
                                 if part.kind == "text" and part.text)
                if msg.role == "system":
                    system_chunks.append(text)
                else:
                    messages.append({"role": msg.role,
                                     "content": [{"type": "text", "text": text}]})
            body: dict = {
                "model": p.model,
                "max_tokens": p.max_output_tokens,
                "temperature": prompt.temperature if prompt.temperature is not None
                               else p.temperature,
                "messages": messages,
            }
            if system_chunks:
                body["system"] = "\n".join(system_chunks)
            if response_schema is not None and p.supports_structured_output:
                body["tools"] = [{"name": "emit",
                                  "description": "Emit the structured result.",
                                  "input_schema": response_schema}]
                body["tool_choice"] = {"type": "tool", "name": "emit"}
            headers = {"x-api-key": p.api_key,
                       "anthropic-version": "2023-06-01",
                       "content-type": "application/json"}
            started = time.monotonic()
            async with httpx.AsyncClient(timeout=p.timeout_s) as client:
                resp = await client.post(f"{p.base_url}/v1/messages",
                                         json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            structured: dict | None = None
            texts: list[str] = []
            for block in data.get("content", []):
                if block.get("type") == "tool_use" and structured is None:
                    if isinstance(block.get("input"), dict):
                        structured = block["input"]
                elif block.get("type") == "text":
                    texts.append(block.get("text", ""))
            usage = Usage(data.get("usage", {}).get("input_tokens", 0),
                          data.get("usage", {}).get("output_tokens", 0))
            return _Resp(text="\n".join(texts), structured=structured, usage=usage,
                         model=data.get("model", p.model),
                         latency_ms=int((time.monotonic() - started) * 1000))

    def make_client(profiles: dict[str, LLMProfile]):
        return _MinimalRealAnthropicClient(profiles)


def user_message(text: str) -> Message:
    return Message(role="user", parts=(Part(kind="text", text=text),))


def system_message(text: str) -> Message:
    return Message(role="system", parts=(Part(kind="text", text=text),))


async def test_structured_output_l0_valid_first_try():
    """L0 ON (supports_structured_output → tool_choice forced tool "emit"): the very
    first response must already be a schema-valid object — attempts == 1, bucket
    l0_or_clean."""
    profiles = {"default": make_profile("default", structured=True)}
    engine = SchemaEngine(USER_SCHEMA, make_client(profiles), OutputConfig())
    prompt = PromptBundle(messages=(
        system_message("你是数据标注助手。对用户提供的一条输入法日志进行标注，"
                       "输出必须是符合以下 JSON Schema 的单个 JSON 对象，不输出任何其他内容：\n"
                       + engine.user_schema_text),
        user_message("[待标注数据] 帮我写一条请假条，明天上午要去医院"),
    ))
    obj, usage, attempts, model = await engine.complete_validated("default", prompt)
    assert engine.validate_only(obj) == []
    assert obj["intent"] == "writing_assist"
    assert attempts == 1
    assert engine.stats == {"l0_or_clean": 1, "l1": 0, "l3_1": 0, "l3_2": 0, "rejected": 0}
    assert usage.prompt_tokens > 0 and usage.completion_tokens > 0
    assert model


async def test_fenced_json_resolved_without_rejection():
    """L0 OFF; the prompt is engineered to make the model wrap its JSON in a Markdown
    code fence, exercising the L1 deterministic-repair path (and L3 as a safety net).
    The engine must still return a schema-valid object and never reject."""
    profiles = {"default": make_profile("default", structured=False)}
    engine = SchemaEngine(USER_SCHEMA, make_client(profiles), OutputConfig())
    prompt = PromptBundle(messages=(
        system_message("你是数据标注助手。对用户提供的一条输入法日志进行标注。"
                       "输出符合以下 JSON Schema 的单个 JSON 对象：\n"
                       + engine.user_schema_text + "\n"
                       "必须先输出一行说明文字，然后把 JSON 放在 markdown 代码围栏"
                       "（```json 与 ``` 之间）中输出。"),
        user_message("[待标注数据] 把这句话翻译成英文：今天天气怎么样"),
    ))
    obj, usage, attempts, model = await engine.complete_validated("default", prompt)
    assert engine.validate_only(obj) == []
    assert obj["intent"] == "translation"
    stats = engine.stats
    assert stats["rejected"] == 0
    assert sum(stats.values()) == 1
    # The engineered prompt should land in the L1 bucket (fence stripped); allow the
    # clean/L3 buckets too since model formatting is not fully deterministic, but the
    # resolution must exist.
    assert stats["l1"] + stats["l0_or_clean"] + stats["l3_1"] + stats["l3_2"] == 1
    assert attempts >= 1
    assert usage.completion_tokens > 0


# ── v1.5 plan A: L2.5 hook through the REAL repair loop ──────────────────────

async def test_l25_hook_violation_repaired_by_loop():
    """The hook's violation text joins the repair prompt; the model must obey
    it on the repair round — the hook is a coach, not just a gate."""
    prof = make_profile("default", structured=False)
    engine = SchemaEngine(USER_SCHEMA, make_client({"default": prof}),
                          OutputConfig(max_repair_attempts=2,
                                       validator="tests.hook_samples:topic_max6"))
    prompt = PromptBundle(messages=(
        Message(role="system", parts=(Part(kind="text", text=(
            "你是意图标注员。输出必须是符合以下 JSON Schema 的单个 JSON 对象："
            + engine.user_schema_text
            + "\n注意：topic 字段请填写『这是一个非常长的主题短语示例』（一字不差）。"),
            image=None),)),
        Message(role="user", parts=(Part(kind="text",
            text="[待标注数据] 帮我写一条请假条，明天上午要去医院", image=None),)),
    ))
    obj, usage, attempts, model = await engine.complete_validated(
        "default", prompt, record={"instruction": "帮我写一条请假条"})
    assert len(obj["topic"]) <= 6            # 回调的要求最终被满足
    assert attempts >= 2                     # 至少经过一轮 L3 修复（回调当教练）
    assert engine.stats["rejected"] == 0


async def test_l25_unsatisfiable_hook_exhausts_as_callback_violation():
    from labelkit.common.errors import SchemaViolation
    prof = make_profile("default", structured=False)
    engine = SchemaEngine(USER_SCHEMA, make_client({"default": prof}),
                          OutputConfig(max_repair_attempts=1,
                                       validator="tests.hook_samples:always_reject"))
    prompt = PromptBundle(messages=(
        Message(role="system", parts=(Part(kind="text", text=(
            "你是意图标注员。输出必须是符合以下 JSON Schema 的单个 JSON 对象："
            + engine.user_schema_text), image=None),)),
        Message(role="user", parts=(Part(kind="text",
            text="[待标注数据] 在吗", image=None),)),
    ))
    with pytest.raises(SchemaViolation) as ei:
        await engine.complete_validated("default", prompt, record=None)
    assert ei.value.callback_only is True
    assert all(v.startswith("(validator) ") for v in ei.value.errors)
    assert engine.stats["rejected"] == 1

"""M8 integration tests — REAL endpoint (glm-5.2 via api.z.ai, anthropic protocol).

No mock LLMs (project policy). SchemaEngine is exercised end-to-end through
complete_validated and the production LLMClient against the live endpoint.
"""
from __future__ import annotations

import os

import pytest

from labelkit.common.runtime.schema_engine import (
    CallScope,
    Message,
    Part,
    PromptBundle,
    SchemaEngine,
)
from labelkit.common.config.model import LLMProfile, OutputConfig
from labelkit.common.runtime.credentials import RuntimeCredentials
from labelkit.common.runtime.llm_client import LLMClient

from tests import hook_samples
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
    )


def make_client(profiles: dict[str, LLMProfile]) -> LLMClient:
    """用冻结运行凭据装配生产 LLMClient。"""
    credentials = RuntimeCredentials(
        llm={name: (os.environ[ZAI_KEY_ENV],) for name in profiles},
        embedding={},
    )
    return LLMClient(profiles, {}, credentials)


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
                          OutputConfig(max_repair_attempts=2),
                          validator=hook_samples.topic_max6)
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
        "default", prompt,
        scope=CallScope(record={"instruction": "帮我写一条请假条"}))
    assert len(obj["topic"]) <= 6            # 回调的要求最终被满足
    assert attempts >= 2                     # 至少经过一轮 L3 修复（回调当教练）
    assert engine.stats["rejected"] == 0


async def test_l25_unsatisfiable_hook_exhausts_as_callback_violation():
    from labelkit.common.errors import SchemaViolation
    prof = make_profile("default", structured=True)
    engine = SchemaEngine(USER_SCHEMA, make_client({"default": prof}),
                          OutputConfig(max_repair_attempts=1),
                          validator=hook_samples.always_reject)
    prompt = PromptBundle(messages=(
        Message(role="system", parts=(Part(kind="text", text=(
            "你是意图标注员。输出必须是符合以下 JSON Schema 的单个 JSON 对象："
            + engine.user_schema_text), image=None),)),
        Message(role="user", parts=(Part(kind="text",
            text="[待标注数据] 在吗", image=None),)),
    ))
    with pytest.raises(SchemaViolation) as ei:
        await engine.complete_validated("default", prompt,
                                        scope=CallScope(record=None))
    assert ei.value.callback_only is True
    assert all(v.startswith("(validator) ") for v in ei.value.errors)
    assert engine.stats["rejected"] == 1

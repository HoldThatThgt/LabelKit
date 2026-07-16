"""Integration tests for M7 verify against the REAL glm-5.2 endpoint (no mock LLMs).

One obviously-correct and one deliberately-wrong (record, annotation) pair are judged
with policy="drop"; we assert a pass and a fail verdict respectively.

The sibling service modules M8/M9 may not be implemented yet. When absent, this test
registers *contract-shaped data types* (Part/Message/PromptBundle, VERDICT_SCHEMA —
copied verbatim from CONTRACTS.md §7.8/§10.7) so `labelkit.verify` can assemble prompts,
and drives the LLM through a minimal in-test engine that makes REAL httpx calls to the
z.ai anthropic endpoint (tool-forced structured output per §7.8) and validates the
verdict with jsonschema. Nothing is faked: every judge verdict comes from glm-5.2.
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Literal

import httpx
import jsonschema
import pytest

from labelkit.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    Criterion,
    DedupConfig,
    ExtractConfig,
    GenerateConfig,
    InputConfig,
    LLMProfile,
    OutputConfig,
    QualityConfig,
    ResolvedConfig,
    Rubric,
    RunConfig,
    SegmentConfig,
    StreamConfig,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)
from labelkit.errors import SchemaViolation
from labelkit.types import Annotation, PipelineItem, Record, RecordRef, Usage

from tests.conftest import ZAI_BASE_URL, ZAI_KEY_ENV, ZAI_MODEL

pytestmark = pytest.mark.integration

# ── contract-shaped prompt types (only registered if M9 has not landed yet) ──

_VERDICT_SCHEMA = {  # CONTRACTS.md §10.7, verbatim
    "type": "object",
    "properties": {
        "critiques": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "aspect": {"type": "string"},
                    "opinion": {"type": "string"},
                },
                "required": ["aspect", "opinion"],
                "additionalProperties": False,
            },
        },
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
    },
    "required": ["critiques", "verdict"],
    "additionalProperties": False,
}


def _ensure_contract_modules() -> None:
    """Register CONTRACTS.md data shapes for modules other engineers have not landed."""
    try:
        import labelkit.llm_client  # noqa: F401
    except ImportError:
        mod = ModuleType("labelkit.llm_client")

        @dataclass(frozen=True)
        class Part:
            kind: Literal["text", "image"]
            text: str | None = None
            image: object | None = None

        @dataclass(frozen=True)
        class Message:
            role: Literal["system", "user", "assistant"]
            parts: tuple

        @dataclass(frozen=True)
        class PromptBundle:
            messages: tuple
            temperature: float | None = None

        mod.Part, mod.Message, mod.PromptBundle = Part, Message, PromptBundle
        sys.modules["labelkit.llm_client"] = mod

    try:
        import labelkit.schema_engine  # noqa: F401
    except ImportError:
        mod = ModuleType("labelkit.schema_engine")
        mod.VERDICT_SCHEMA = _VERDICT_SCHEMA
        sys.modules["labelkit.schema_engine"] = mod


_ensure_contract_modules()

from labelkit.stage import RunContext  # noqa: E402
from labelkit.verify import VerifyStage  # noqa: E402


# ── minimal REAL-endpoint engine (anthropic provider adaptation, §7.8) ──────

class RealJudgeEngine:
    """complete_validated() over real POST {base_url}/v1/messages with a tool-forced
    structured output named "emit" (CONTRACTS.md §7.8), jsonschema-validated."""

    def __init__(self, profiles: dict[str, LLMProfile]):
        self.profiles = profiles

    async def complete_validated(self, profile, prompt, schema=None, *,
                                 record_ids=(), batch_no=0):
        p = self.profiles[profile]
        system = "\n".join(
            part.text
            for msg in prompt.messages if msg.role == "system"
            for part in msg.parts if part.kind == "text"
        )
        messages = [
            {"role": msg.role,
             "content": [{"type": "text", "text": part.text}
                         for part in msg.parts if part.kind == "text"]}
            for msg in prompt.messages if msg.role != "system"
        ]
        body = {
            "model": p.model,
            "max_tokens": 700,
            "temperature": 0.0,
            "system": system,
            "messages": messages,
            "tools": [{"name": "emit", "description": "输出评审结果",
                       "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": "emit"},
        }
        async with httpx.AsyncClient(timeout=p.timeout_s) as client:
            resp = await client.post(
                f"{p.base_url}/v1/messages",
                headers={"x-api-key": p.api_key,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json=body,
            )
            resp.raise_for_status()
        data = resp.json()
        obj = next(b["input"] for b in data["content"] if b["type"] == "tool_use")
        errors = [
            f"{'/' + '/'.join(str(x) for x in e.absolute_path)}: {e.message}"
            for e in jsonschema.Draft202012Validator(schema).iter_errors(obj)
        ]
        if errors:
            raise SchemaViolation(errors, str(obj))
        usage = Usage(data["usage"]["input_tokens"], data["usage"]["output_tokens"])
        return obj, usage, 1, data.get("model", p.model)


class CollectingMetrics:
    def __init__(self):
        self.events = []

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None):
        self.events.append({"ev": ev, "stage": stage, "batch_no": batch_no,
                            "record_ids": record_ids, "payload": payload or {}})


# ── fixtures ────────────────────────────────────────────────────────────────

_INSTRUCTION = ("你是输入法中文指令的意图标注员。判断每条用户指令的意图类别（intent）"
                "与主题（topic）。intent 取值：writing_assist（写作协助）、weather_query"
                "（天气查询）、translation（翻译）、other（其他）。")

_USER_SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string"}, "topic": {"type": "string"}},
    "required": ["intent", "topic"],
    "additionalProperties": False,
}


def _cfg(trace: TraceConfig | None = None) -> ResolvedConfig:
    judge = LLMProfile(
        name="judge", provider="anthropic", base_url=ZAI_BASE_URL, model=ZAI_MODEL,
        api_key_env=ZAI_KEY_ENV, supports_structured_output=True, max_output_tokens=700,
        api_key=os.environ[ZAI_KEY_ENV],
    )
    return ResolvedConfig(
        tool=ToolConfig(),
        llm_profiles={"judge": judge},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality="text", input="in"),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(enabled=True, llm="judge", instruction=_INSTRUCTION),
        verify=VerifyConfig(enabled=True, llm="judge", policy="drop"),
        output=OutputConfig(schema_inline="{}"),
        trace=trace or TraceConfig(),
        rubric=Rubric(name="t", criteria=(Criterion(key="c", description="d",
                                                    pairwise_prompt="p"),)),
        class_views={},
        user_schema=_USER_SCHEMA,
        limit=None, strict=False, dry_run=False,
        config_path="config.toml", project_path="project.toml",
        config_digest="sha256:0", project_digest="sha256:0",
    )


def _item(rec_id: str, text: str, output: dict) -> PipelineItem:
    record = Record(
        id=rec_id, modality="text", text=text, raw={"instruction": text},
        ui_tree=None, image=None,
        ref=RecordRef(source_file="in.jsonl", line_no=1, pair_index=None,
                      generated_from=()),
    )
    return PipelineItem(record=record,
                        annotation=Annotation(output=output, model=ZAI_MODEL,
                                              attempts=1, usage=Usage()))


def _run_verify(item: PipelineItem, trace: TraceConfig | None = None):
    cfg = _cfg(trace)
    metrics = CollectingMetrics()
    ctx = RunContext(cfg=cfg, llm=None, schema_engine=RealJudgeEngine(cfg.llm_profiles),
                     metrics=metrics, rng=None, batch_no=1)
    stage = VerifyStage(cfg)
    asyncio.run(stage.run([item], ctx))
    return item, metrics


# ── tests ───────────────────────────────────────────────────────────────────

def test_obviously_correct_annotation_passes():
    item, metrics = _run_verify(
        _item("1cda030abc565f17", "帮我写一条请假条，明天上午要去医院",
              {"intent": "writing_assist", "topic": "请假条写作"}),
        trace=TraceConfig(enabled=True, content="full"),
    )
    assert item.errors == []
    assert item.verification is not None
    assert item.verification.verdict == "pass"
    assert item.verification.rounds == 1
    assert item.status == "active"
    verdict_events = [e for e in metrics.events if e["ev"] == "verify.verdict"]
    assert len(verdict_events) == 1
    assert verdict_events[0]["payload"]["verdict"] == "pass"
    assert verdict_events[0]["payload"]["round"] == 1
    assert verdict_events[0]["record_ids"] == ("1cda030abc565f17",)
    # §7.4/§8.3: tiers are cumulative — trace.content="full" carries the excerpt too.
    assert verdict_events[0]["payload"]["excerpt"] == {
        "1cda030abc565f17": "帮我写一条请假条，明天上午要去医院"}


def test_deliberately_wrong_annotation_fails_and_drops():
    item, metrics = _run_verify(
        _item("2fdb141bcd676f28", "帮我写一条请假条，明天上午要去医院",
              {"intent": "weather_query", "topic": "明日天气预报"})
    )
    assert item.errors == []
    assert item.verification is not None
    assert item.verification.verdict == "fail"
    assert item.verification.rounds == 1
    assert item.status == "dropped_verify"          # policy = drop
    assert len(item.verification.critiques) >= 1    # judge explained the failure
    verdict_events = [e for e in metrics.events if e["ev"] == "verify.verdict"]
    assert len(verdict_events) == 1
    assert verdict_events[0]["payload"]["verdict"] == "fail"

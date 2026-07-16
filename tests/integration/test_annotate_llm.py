"""M5 integration tests — REAL endpoint (glm-5.2 via api.z.ai, anthropic protocol).

No mock LLMs (project policy). Uses the real labelkit.common.runtime.llm_client / labelkit.common.runtime.schema_engine
when those modules have landed; until then, minimal REAL-HTTP stand-ins implementing the
exact contract surface annotate needs (SchemaEngine.user_schema_text /
complete_validated) are used so the annotate path is exercised end-to-end against the
live endpoint either way.
"""
from __future__ import annotations

import json
import os
import random
import time

import httpx
import jsonschema
import pytest

from labelkit.operators.annotate import AnnotateStage
from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
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
from labelkit.common.errors import SchemaViolation
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import PipelineItem, Record, RecordRef, Usage

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


def make_cfg(trace: TraceConfig | None = None) -> ResolvedConfig:
    profile = LLMProfile(
        name="default",
        provider="anthropic",
        base_url=ZAI_BASE_URL,
        model=ZAI_MODEL,
        api_key_env=ZAI_KEY_ENV,
        max_concurrency=2,
        timeout_s=120,
        max_retries=2,
        max_output_tokens=500,
        temperature=0.0,
        api_key=os.environ.get(ZAI_KEY_ENV, ""),
    )
    return ResolvedConfig(
        tool=ToolConfig(),
        llm_profiles={"default": profile},
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
        annotate=AnnotateConfig(
            enabled=True,
            llm="default",
            instruction=("你是输入法中文用户指令的意图标注员。判断给定指令属于哪类意图、"
                         "其主题是什么、以及完成该指令对语言模型的难度。"),
        ),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline=json.dumps(USER_SCHEMA)),
        trace=trace or TraceConfig(),
        rubric=Rubric(name="default:text", criteria=()),
        class_views={},
        user_schema=USER_SCHEMA,
        limit=None,
        strict=False,
        dry_run=False,
        config_path="config.toml",
        project_path="project.toml",
        config_digest="sha256:0",
        project_digest="sha256:0",
    )


# ── minimal REAL-endpoint stand-ins (used only until M8/M9 land) ─────────────

class _MiniAnthropicClient:
    """Real HTTP calls to {base_url}/v1/messages per CONTRACTS §7.8 — not a mock."""

    def __init__(self, profiles):
        self._profiles = profiles

    async def complete(self, profile: str, prompt, response_schema=None):
        p = self._profiles[profile]
        system_chunks: list[str] = []
        messages: list[dict] = []
        for msg in prompt.messages:
            text = "\n".join(part.text for part in msg.parts if part.kind == "text")
            if msg.role == "system":
                system_chunks.append(text)
            else:
                messages.append({"role": msg.role,
                                 "content": [{"type": "text", "text": text}]})
        body = {
            "model": p.model,
            "max_tokens": p.max_output_tokens,
            "messages": messages,
            "temperature": p.temperature if prompt.temperature is None
                           else prompt.temperature,
        }
        if system_chunks:
            body["system"] = "\n".join(system_chunks)
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=p.timeout_s) as client:
            resp = await client.post(
                f"{p.base_url.rstrip('/')}/v1/messages",
                headers={"x-api-key": p.api_key,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json=body,
            )
        resp.raise_for_status()
        data = resp.json()
        text = "".join(block.get("text", "") for block in data.get("content", [])
                       if block.get("type") == "text")
        usage = data.get("usage", {})

        class _Resp:
            pass

        r = _Resp()
        r.text = text
        r.structured = None
        r.usage = Usage(int(usage.get("input_tokens", 0)),
                        int(usage.get("output_tokens", 0)))
        r.model = data.get("model", p.model)
        r.latency_ms = int((time.monotonic() - started) * 1000)
        return r


class _MiniSchemaEngine:
    """Contract-shaped complete_validated (L1 deterministic parse + L2 validation,
    no L3 repair loop) over the real client."""

    def __init__(self, user_schema: dict, llm):
        self._schema = user_schema
        self._llm = llm

    @property
    def user_schema_text(self) -> str:
        return json.dumps(self._schema, ensure_ascii=False, separators=(", ", ": "))

    async def complete_validated(self, profile, prompt, schema=None, *,
                                 record_ids=(), batch_no=0):
        target = schema if schema is not None else self._schema
        resp = await self._llm.complete(profile, prompt)
        obj = self._parse(resp.text)
        if obj is None:
            raise SchemaViolation(["/: not parseable as a JSON object"], resp.text)
        errors = [f"/{'/'.join(str(p) for p in e.absolute_path)}: {e.message}"
                  for e in jsonschema.Draft202012Validator(target).iter_errors(obj)]
        if errors:
            raise SchemaViolation(errors, resp.text)
        return obj, resp.usage, 1, resp.model

    @staticmethod
    def _parse(text: str):
        import json_repair
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                return None
            obj = json_repair.loads(text[start:end + 1])
        return obj if isinstance(obj, dict) else None


class _RecordingMetrics:
    def __init__(self):
        self.counters: dict[str, int] = {}
        self.events: list[tuple] = []

    def count(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None) -> None:
        self.events.append((ev, stage, batch_no, record_ids, payload or {}))


def make_ctx(cfg) -> RunContext:
    metrics = _RecordingMetrics()
    try:
        from labelkit.common.runtime.llm_client import LLMClient
        from labelkit.common.runtime.schema_engine import SchemaEngine
    except ImportError:
        llm = _MiniAnthropicClient(cfg.llm_profiles)
        engine = _MiniSchemaEngine(dict(cfg.user_schema), llm)
    else:
        llm = LLMClient(cfg.llm_profiles, cfg.embedding_profiles, metrics=None)
        engine = SchemaEngine(dict(cfg.user_schema), llm, cfg.output, metrics=None)
    return RunContext(cfg=cfg, llm=llm, schema_engine=engine, metrics=metrics,
                      rng=random.Random("0:1:annotate"), batch_no=1)


def text_record(rec_id: str, text: str) -> Record:
    return Record(id=rec_id, modality="text", text=text, raw={"instruction": text},
                  ui_tree=None, image=None, ref=RecordRef("data.jsonl", 1, None, ()))


async def test_annotate_two_real_text_records():
    # trace at the "full" tier — §7.4 tiers are cumulative, so annotate.done must
    # still carry the excerpt payload (full ⊇ excerpt).
    cfg = make_cfg(trace=TraceConfig(
        enabled=True, channels=("quality", "verify", "schema", "annotate"),
        content="full"))
    ctx = make_ctx(cfg)
    records = [
        text_record("1cda030abc565f17", "帮我写一条请假条，明天上午要去医院"),
        text_record("7be2a91c04d3f508", "把「今天天气很好」翻译成英文"),
    ]
    batch = [PipelineItem(record=r) for r in records]

    stage = AnnotateStage(cfg)
    result = await stage.run(batch, ctx)

    assert result is batch                          # stage returns the same list object
    validator = jsonschema.Draft202012Validator(USER_SCHEMA)
    for item in batch:
        assert item.status == "active", f"errors: {item.errors}"
        ann = item.annotation
        assert ann is not None
        validator.validate(dict(ann.output))        # schema-valid output
        assert ann.attempts >= 1
        assert ann.model                            # provider model string present
        assert ann.usage.prompt_tokens > 0 and ann.usage.completion_tokens > 0
        assert ann.sc is None                       # self-consistency off

    done = [e for e in ctx.metrics.events if e[0] == "annotate.done"]
    assert len(done) == 2
    done_ids = {e[3][0] for e in done}
    assert done_ids == {r.id for r in records}
    by_id = {r.id: r for r in records}
    for e in done:
        assert e[4]["attempts"] >= 1
        rid = e[3][0]
        assert e[4]["excerpt"] == {rid: by_id[rid].text[:200]}

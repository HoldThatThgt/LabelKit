"""M6 integration test against the REAL z.ai endpoint (glm-5.2). No mock LLMs.

generate_only mode with 2 seed_examples; one small generation call; asserts at least one
novel sample comes back with generator provenance set and non-empty text.

M8/M9 may not be implemented yet by their owners: when their modules are missing, this
file registers CONTRACT-VERBATIM stand-ins (the frozen §7.7/§7.8/§10.7 dataclasses and
samples_schema — pure data containers, not mocks) and drives the real endpoint through a
minimal Anthropic-protocol engine implementing ``complete_validated``.
"""
import json
import os
import random
import sys
import types
from dataclasses import dataclass

import httpx
import json_repair
import pytest
from jsonschema import Draft202012Validator

from labelkit.common.errors import SchemaViolation
from labelkit.common.contracts.types import Usage

from tests.conftest import ZAI_BASE_URL, ZAI_KEY_ENV, ZAI_MODEL

pytestmark = pytest.mark.integration


# ── contract-verbatim stand-ins for not-yet-landed service modules ─────────

def _ensure_llm_client_module():
    try:
        import labelkit.common.runtime.llm_client  # noqa: F401
        return
    except ImportError:
        pass
    mod = types.ModuleType("labelkit.common.runtime.llm_client")

    @dataclass(frozen=True)
    class Part:
        kind: str
        text: str | None = None
        image: object | None = None

    @dataclass(frozen=True)
    class Message:
        role: str
        parts: tuple = ()

    @dataclass(frozen=True)
    class PromptBundle:
        messages: tuple = ()
        temperature: float | None = None

    mod.Part, mod.Message, mod.PromptBundle = Part, Message, PromptBundle
    sys.modules["labelkit.common.runtime.llm_client"] = mod


def _ensure_schema_engine_module():
    try:
        import labelkit.common.runtime.schema_engine  # noqa: F401
        return
    except ImportError:
        pass
    mod = types.ModuleType("labelkit.common.runtime.schema_engine")

    def samples_schema(num_per_call):                  # exact JSON per CONTRACTS §10.7
        return {"type": "object",
                "properties": {"samples": {"type": "array", "items": {"type": "string"},
                                           "minItems": num_per_call,
                                           "maxItems": num_per_call}},
                "required": ["samples"], "additionalProperties": False}

    mod.samples_schema = samples_schema
    sys.modules["labelkit.common.runtime.schema_engine"] = mod


_ensure_llm_client_module()
_ensure_schema_engine_module()

from labelkit.operators.generate import GenerateStage  # noqa: E402  (needs the modules above)
from labelkit.common.contracts.stage import RunContext  # noqa: E402


# ── minimal REAL engine: Anthropic messages protocol against z.ai ──────────

class RealSamplesEngine:
    """Implements the SchemaEngine.complete_validated surface used by M6, with real
    HTTP calls to the z.ai Anthropic-compatible endpoint and one bounded repair round."""

    def __init__(self, api_key: str, max_tokens: int = 800):
        self._api_key = api_key
        self._max_tokens = max_tokens

    async def _call(self, system: str, messages: list[dict], temperature: float | None) -> dict:
        body = {"model": ZAI_MODEL, "max_tokens": self._max_tokens,
                "system": system, "messages": messages}
        if temperature is not None:
            body["temperature"] = temperature
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                f"{ZAI_BASE_URL}/v1/messages",
                headers={"x-api-key": self._api_key,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json=body)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _parse(text: str) -> dict | None:
        try:
            obj = json_repair.loads(text)
        except Exception:
            return None
        return obj if isinstance(obj, dict) else None

    async def complete_validated(self, profile: str, prompt, schema: dict | None = None, *,
                                 record_ids: tuple = (), batch_no: int = 0):
        system = "\n".join(part.text for msg in prompt.messages if msg.role == "system"
                           for part in msg.parts if part.text)
        messages = [{"role": msg.role,
                     "content": [{"type": "text", "text": part.text} for part in msg.parts]}
                    for msg in prompt.messages if msg.role != "system"]
        data = await self._call(system, messages, prompt.temperature)
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
        usage = Usage(data.get("usage", {}).get("input_tokens", 0),
                      data.get("usage", {}).get("output_tokens", 0))
        attempts = 1
        validator = Draft202012Validator(schema or {"type": "object"})
        obj = self._parse(text)
        errors = ([f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message}"
                   for e in validator.iter_errors(obj)] if obj is not None
                  else ["/: not a JSON object"])
        if errors:
            # One real repair round (§10.6 shape), still against the live endpoint.
            listing = "\n".join(f"{i}. {err}" for i, err in enumerate(errors, start=1))
            repair_user = f"[原始输出]\n{text}\n\n[违规清单]\n{listing}\n\n只输出修正后的 JSON。"
            data = await self._call("", [{"role": "user", "content":
                                          [{"type": "text", "text": repair_user}]}], None)
            text = "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text")
            usage = usage + Usage(data.get("usage", {}).get("input_tokens", 0),
                                  data.get("usage", {}).get("output_tokens", 0))
            attempts += 1
            obj = self._parse(text)
            errors = ([f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message}"
                       for e in validator.iter_errors(obj)] if obj is not None
                      else ["/: not a JSON object"])
            if errors:
                raise SchemaViolation(errors, text)
        return obj, usage, attempts, data.get("model", ZAI_MODEL)


class Recorder:
    def __init__(self):
        self.counters: dict[str, int] = {}
        self.events: list[dict] = []

    def count(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None):
        self.events.append({"ev": ev, "stage": stage, "batch_no": batch_no,
                            "record_ids": tuple(record_ids), "payload": payload or {}})


SEEDS = ("帮我写一条请假条，明天上午要去医院", "写一份周报模板")


def _mk_cfg():
    from labelkit.common.config.model import (AnnotateConfig, ClassifyConfig, DedupConfig,
                                       ExtractConfig, GenerateConfig, InputConfig,
                                       OutputConfig, QualityConfig, ResolvedConfig,
                                       Rubric, RunConfig, SegmentConfig,
                                       StitchConfig,
                                       StreamConfig, ToolConfig, TraceConfig,
                                       VerifyConfig)
    generate = GenerateConfig(
        enabled=True,
        llms=("glm",),
        instruction=("你是中文输入法的真实用户。模仿示例指令的口吻与场景，生成全新的一句话中文指令："
                     "日常场景、口语化、诉求明确；只借鉴风格与题材范围，不得复述示例内容。"),
        num_per_record=1,
        seeds_per_call=2,
        num_per_call=2,                # C = ceil(2*1/2) = 1 call — keep it small
        seed_examples=SEEDS,
    )
    return ResolvedConfig(
        tool=ToolConfig(), llm_profiles={}, embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality="text", mode="generate_only", seed=0),
        input=InputConfig(text_field="instruction"),
        stream=StreamConfig(),
        dedup=DedupConfig(), segment=SegmentConfig(), stitch=StitchConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(),
        quality=QualityConfig(), generate=generate,
        annotate=AnnotateConfig(), verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"), trace=TraceConfig(),
        rubric=Rubric(name="r", criteria=()), class_views={},
        user_schema={"type": "object"},
        limit=None, strict=False, dry_run=False,
        config_path="config.toml", project_path="project.toml",
        config_digest="sha256:0", project_digest="sha256:0",
    )


async def test_generate_only_seed_pool_real_llm():
    cfg = _mk_cfg()
    engine = RealSamplesEngine(os.environ[ZAI_KEY_ENV])
    metrics = Recorder()
    ctx = RunContext(cfg=cfg, llm=None, schema_engine=engine, metrics=metrics,
                     rng=random.Random(f"{cfg.run.seed}:0:generate"), batch_no=0)
    records = await GenerateStage(cfg).generate_all(ctx)

    assert len(records) >= 1, "real endpoint produced no novel sample"
    for rec in records:
        assert rec.modality == "text"
        assert isinstance(rec.text, str) and rec.text.strip()
        assert rec.text not in SEEDS                     # novel vs the seed pool
        assert rec.raw == {"instruction": rec.text}
        assert rec.ref.generator == {"llm": "glm", "style": None}
        assert rec.ref.generated_from == ()              # generate_only semantics
        assert len(rec.id) == 16
    # bucket stats: exactly one call in the glm×null bucket, survivors counted
    assert metrics.counters["generate.buckets.glm×null.calls"] == 1
    assert metrics.counters["generate.buckets.glm×null.produced"] == 2
    assert metrics.counters["generate.buckets.glm×null.survived_dedup"] == len(records)
    # counts.generated is owned exclusively by M10 (CONTRACTS §9.3); M6 must not set it
    assert "counts.generated" not in metrics.counters
    # M6 emits NO trace events (§8.1 defines none for generate; buckets are its only
    # observability). In particular no off-catalog "generate.sample" and no "error"
    # event carrying str(exc) — voided calls log a value-free stderr line instead.
    assert metrics.events == []

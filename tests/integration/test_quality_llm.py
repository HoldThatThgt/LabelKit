"""Integration tests for M4 quality — REAL LLM calls to the z.ai anthropic endpoint.

No mocks (project policy). Runs glm-5.2 via https://api.z.ai/api/anthropic; auto-skipped
by tests/conftest.py when LABELKIT_ZAI_KEY is unavailable.

M8 (SchemaEngine) / M9 (LLMClient) belong to other engineers and may not have landed yet,
so these tests carry a minimal self-contained real-endpoint adapter exposing the one
surface QualityStage consumes: `complete_validated(profile, prompt, schema, *,
record_ids, batch_no)` per CONTRACTS.md §7.7. Every LLM response is a genuine glm-5.2
completion; parsing/validation is local (fence strip + json_repair + jsonschema).
"""
from __future__ import annotations

import asyncio
import os
import random
import re

import httpx
import json_repair
import pytest
from jsonschema import Draft202012Validator

from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ConsoleConfig,
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
    StitchConfig,
    StreamConfig,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)
from labelkit.common.errors import ProviderFatalError, SchemaViolation
from labelkit.operators.quality import AGGREGATE_KEY, QualityStage
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import PipelineItem, Record, RecordRef, Usage

pytestmark = pytest.mark.integration

ZAI_BASE_URL = "https://api.z.ai/api/anthropic"
ZAI_MODEL = "glm-5.2"
ZAI_KEY_ENV = "LABELKIT_ZAI_KEY"

EDU = Criterion(
    key="educational_value",
    description="教育/训练价值：作为模型训练数据能带来多少可学习的能力。",
    pairwise_prompt="比较两段文本，哪一段更有教育价值、更值得用于训练语言模型？",
    weight=1.0,
    pointwise_levels=(
        "0: 无学习价值（噪声、纯广告、无意义重复）。",
        "1: 学习价值极低，内容浅表。",
        "2: 在 1 的基础上，有一定可学习内容但组织松散。",
        "3: 在 2 的基础上，内容系统、有清晰的知识或任务示范价值。",
        "4: 在 3 的基础上，示范性强，覆盖推理/解释/结构化表达等能力。",
        "5: 在 4 的基础上，训练价值突出，属稀缺的高质量样本。"),
)

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")


def _parse_json(text: str) -> dict | None:
    """Deterministic parse: strip Markdown fences, take the first balanced-braces
    substring, json_repair. Mirrors M8 L1 semantics for test purposes."""
    cleaned = _FENCE_RE.sub("", text.strip()).strip()
    start = cleaned.find("{")
    if start >= 0:
        depth = 0
        for pos in range(start, len(cleaned)):
            if cleaned[pos] == "{":
                depth += 1
            elif cleaned[pos] == "}":
                depth -= 1
                if depth == 0:
                    cleaned = cleaned[start:pos + 1]
                    break
    try:
        obj = json_repair.loads(cleaned)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


class RealEndpointEngine:
    """Minimal SchemaEngine.complete_validated stand-in doing REAL glm-5.2 calls."""

    def __init__(self, api_key: str, max_tokens: int = 1500, attempts: int = 2):
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.attempts = attempts
        self._sem = asyncio.Semaphore(3)

    @staticmethod
    def _messages(prompt) -> tuple[str, list[dict]]:
        system_chunks: list[str] = []
        user_msgs: list[dict] = []
        for msg in prompt.messages:
            text = "\n".join(p.text for p in msg.parts if p.kind == "text" and p.text)
            if msg.role == "system":
                system_chunks.append(text)
            else:
                user_msgs.append({"role": msg.role,
                                  "content": [{"type": "text", "text": text}]})
        return "\n".join(system_chunks), user_msgs

    async def complete_validated(self, profile: str, prompt, schema: dict | None = None,
                                 *, record_ids: tuple[str, ...] = (),
                                 batch_no: int = 0) -> tuple[dict, Usage, int, str]:
        system, messages = self._messages(prompt)
        body = {"model": ZAI_MODEL, "max_tokens": self.max_tokens,
                "temperature": prompt.temperature if prompt.temperature is not None else 0.0,
                "system": system, "messages": messages}
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        validator = Draft202012Validator(schema) if schema is not None else None
        last_text = ""
        errors: list[str] = []
        for attempt in range(1, self.attempts + 1):
            async with self._sem:
                async with httpx.AsyncClient(timeout=180) as client:
                    resp = await client.post(f"{ZAI_BASE_URL}/v1/messages",
                                             headers=headers, json=body)
            if resp.status_code in (400, 401, 403, 404):
                # Non-retryable provider error — mapped exactly as M9 does (§7.6).
                raise ProviderFatalError(f"HTTP {resp.status_code}", profile,
                                         resp.status_code)
            resp.raise_for_status()
            data = resp.json()
            last_text = "".join(block.get("text", "") for block in data.get("content", [])
                                if block.get("type") == "text")
            obj = _parse_json(last_text)
            if obj is not None:
                errors = ([f"{'/' + '/'.join(str(p) for p in e.path)}: {e.message}"
                           for e in validator.iter_errors(obj)] if validator else [])
                if not errors:
                    usage = Usage(int(data.get("usage", {}).get("input_tokens", 0)),
                                  int(data.get("usage", {}).get("output_tokens", 0)))
                    return obj, usage, attempt, data.get("model", ZAI_MODEL)
            else:
                errors = ["/: not a JSON object"]
        raise SchemaViolation(errors, last_text)


class Recorder:
    """Metrics recorder (counts/events only — not an LLM)."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self.counters: dict[str, int] = {}

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None):
        self.events.append((ev, {"record_ids": tuple(record_ids),
                                 "payload": dict(payload or {})}))

    def count(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n

    def of(self, ev_name):
        return [p for ev, p in self.events if ev == ev_name]


def make_cfg(quality: QualityConfig) -> ResolvedConfig:
    profile = LLMProfile(name="default", provider="anthropic", base_url=ZAI_BASE_URL,
                         model=ZAI_MODEL, api_key_env=ZAI_KEY_ENV,
                         api_key=os.environ.get(ZAI_KEY_ENV, ""))
    return ResolvedConfig(
        tool=ToolConfig(),
        console=ConsoleConfig(),
        llm_profiles={"default": profile},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality="text", seed=7),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(),
        stitch=StitchConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(),
        quality=quality,
        generate=GenerateConfig(),
        annotate=AnnotateConfig(instruction="x"),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=TraceConfig(),
        rubric=Rubric(name="itest-rubric", criteria=(EDU,)),
        class_views={},
        user_schema={"type": "object"},
        limit=None,
        strict=False,
        dry_run=False,
        config_path="config.toml",
        project_path="project.toml",
        config_digest="sha256:0",
        project_digest="sha256:0",
    )


def make_item(rec_id: str, text: str) -> PipelineItem:
    rec = Record(id=rec_id, modality="text", text=text, raw={"text": text},
                 ui_tree=None, image=None,
                 ref=RecordRef(source_file="a.jsonl", line_no=1, pair_index=None,
                               generated_from=()))
    return PipelineItem(record=rec)


def make_ctx(cfg: ResolvedConfig, metrics: Recorder) -> RunContext:
    engine = RealEndpointEngine(os.environ[ZAI_KEY_ENV])
    return RunContext(cfg=cfg, llm=None, schema_engine=engine, metrics=metrics,
                      rng=random.Random(f"{cfg.run.seed}:1:quality"), batch_no=1)


POINTWISE_TEXTS = [
    ("p1", "解释一下二分查找为什么是 O(log n)，能不能举个在通讯录里找人的例子"),
    ("p2", "哈哈哈哈哈哈"),
    ("p3", "把“会议改到周五下午三点”翻译成英文"),
    ("p4", "总结一下光合作用的光反应和暗反应分别发生在叶绿体的哪个部位，产物有什么区别"),
    ("p5", "在吗"),
    ("p6", "帮我写一条请假条，明天上午要去医院"),
]


async def test_pointwise_end_to_end_real_llm():
    cfg = make_cfg(QualityConfig(mode="pointwise", judgment_reasons=True))
    rec = Recorder()
    batch = [make_item(rid, text) for rid, text in POINTWISE_TEXTS]
    out = await QualityStage(cfg).run(batch, make_ctx(cfg, rec))
    assert out is batch

    for item in batch:
        assert item.status == "active"
        qs = item.scores["educational_value"]
        assert qs.mode == "pointwise"
        assert qs.score is not None and 0.0 <= qs.score <= 1.0
        raw = qs.detail["raw_score"]
        assert isinstance(raw, int) and 0 <= raw <= 5
        assert qs.score == pytest.approx(raw / 5.0)
        assert isinstance(qs.detail["reason"], str) and qs.detail["reason"]
        agg = item.scores[AGGREGATE_KEY]
        assert agg.score is not None and 0.0 <= agg.score <= 1.0
        assert agg.score == pytest.approx(qs.score)  # single criterion, weight 1.0

    events = rec.of("quality.pointwise")
    assert len(events) == len(batch)
    assert rec.counters.get("quality.judgment_failures", 0) == 0
    # Sanity: the trivial chit-chat should not outscore the algorithm-explanation ask.
    by_id = {it.record.id: it.scores["educational_value"].detail["raw_score"]
             for it in batch}
    assert by_id["p1"] >= by_id["p5"]


async def test_pairwise_batch_real_llm():
    cfg = make_cfg(QualityConfig(mode="pairwise", rounds=2, judgment_reasons=False))
    rec = Recorder()
    batch = [
        make_item("w1", "解释一下二分查找为什么是 O(log n)，能不能举个在通讯录里找人的例子"),
        make_item("w2", "哈哈哈哈哈哈"),
        make_item("w3", "总结一下光合作用的光反应和暗反应分别发生在叶绿体的哪个部位"),
        make_item("w4", "帮我写一条请假条，明天上午要去医院"),
    ]
    out = await QualityStage(cfg).run(batch, make_ctx(cfg, rec))
    assert out is batch

    for item in batch:
        assert item.status == "active"  # no threshold configured -> score only
        qs = item.scores["educational_value"]
        assert qs.mode == "pairwise_bt"
        assert qs.score is not None and 0.0 <= qs.score <= 1.0
        assert qs.detail["comparisons"] == 2  # k = 2 rounds, even batch
        assert "log_theta" in qs.detail
        agg = item.scores[AGGREGATE_KEY]
        assert agg.score is not None and 0.0 <= agg.score <= 1.0

    # N=4, k=2 -> 4 comparisons, one judgment call each (single judge, single order).
    judgments = rec.of("quality.judgment")
    failures = rec.counters.get("quality.judgment_failures", 0)
    assert len(judgments) + failures == 4
    assert failures == 0
    for j in judgments:
        assert set(j["payload"]["order"].keys()) == {"A", "B"}
        winners = {e["winner"] for e in j["payload"]["judgments"]}
        assert winners <= {"A", "B", "tie"}
        assert "reason" not in j["payload"]["judgments"][0]  # reasons off
    fits = rec.of("quality.bt_fit")
    assert len(fits) == 1
    assert fits[0]["payload"]["criterion"] == "educational_value"
    assert fits[0]["payload"]["comparisons"] == 4


async def test_pairwise_provider_fatal_fails_records_real_endpoint():
    """A judging call answered by a REAL 401 (bogus key against the live endpoint) is a
    provider_fatal, NOT a judgment_invalid (spec 7.6): the involved records fail, the tie
    fallback does not apply, and quality.judgment_failures (a rubric diagnostic, spec 7.5)
    stays 0."""
    cfg = make_cfg(QualityConfig(mode="pairwise", rounds=1))
    rec = Recorder()
    batch = [make_item("f1", "解释一下二分查找为什么是 O(log n)"),
             make_item("f2", "帮我写一条请假条，明天上午要去医院")]
    engine = RealEndpointEngine("labelkit-invalid-key-provider-fatal-test")
    ctx = RunContext(cfg=cfg, llm=None, schema_engine=engine, metrics=rec,
                     rng=random.Random(f"{cfg.run.seed}:1:quality"), batch_no=1)
    out = await QualityStage(cfg).run(batch, ctx)
    assert out is batch
    for item in batch:
        assert item.status == "failed"
        kinds = {err.kind for err in item.errors}
        assert kinds == {"provider_fatal"}
        assert all(err.retryable is False for err in item.errors)
    assert rec.counters.get("quality.judgment_failures", 0) == 0
    errs = rec.of("error")
    assert errs and all(p["payload"]["kind"] == "provider_fatal" for p in errs)

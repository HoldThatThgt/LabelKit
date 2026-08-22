"""Integration tests for M7 verify against the REAL glm-5.2 endpoint (no mock LLMs).

One obviously-correct and one deliberately-wrong (record, annotation) pair are judged
with policy="drop" through the production LLMClient and SchemaEngine.
"""
from __future__ import annotations

import asyncio
import os

import pytest

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
from labelkit.common.contracts.types import Annotation, PipelineItem, Record, RecordRef, Usage
from labelkit.common.contracts.stage import RunContext
from labelkit.common.inference.credentials import RuntimeCredentials
from labelkit.common.inference.schema_engine import SchemaEngine
from labelkit.operators.verify import VerifyStage
from labelkit.runtime.scheduler import ExecutionRuntime

from tests.conftest import ZAI_BASE_URL, ZAI_KEY_ENV, ZAI_MODEL
from tests.llm_client_helpers import make_llm_client as _client

pytestmark = pytest.mark.integration


class CollectingMetrics:
    def __init__(self):
        self.events = []
        self.counters = {}
        self.runtime_high_water = {}
        self.runtime_totals = {}

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None):
        self.events.append({"ev": ev, "stage": stage, "batch_no": batch_no,
                            "record_ids": record_ids, "payload": payload or {}})

    def count(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n

    def observe_runtime_high_water(self, key, value):
        self.runtime_high_water[key] = max(
            self.runtime_high_water.get(key, 0), value,
        )

    def add_runtime_total(self, key, value):
        self.runtime_totals[key] = self.runtime_totals.get(key, 0) + value


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
    )
    return ResolvedConfig(
        tool=ToolConfig(),
        console=ConsoleConfig(),
        llm_profiles={"judge": judge},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality="text", input="in"),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(),
        stitch=StitchConfig(),
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
    credentials = RuntimeCredentials(
        llm={"judge": (os.environ[ZAI_KEY_ENV],)}, embedding={}
    )
    client = _client(cfg.llm_profiles, {}, credentials)
    engine = SchemaEngine(cfg.user_schema, client, cfg.output)
    runtime = ExecutionRuntime(client._resources, metrics)
    ctx = RunContext(cfg=cfg, llm=client, schema_engine=engine,
                     metrics=metrics, rng=None, batch_no=1, tasks=runtime,
                     task_namespace="integration:batch:1:stage:verify")
    stage = VerifyStage(cfg)

    async def run() -> None:
        try:
            await runtime.run(lambda: stage.run([item], ctx))
        finally:
            await client.aclose()

    asyncio.run(run())
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

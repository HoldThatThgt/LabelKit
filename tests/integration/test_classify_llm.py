"""M13 integration tests — REAL endpoint (glm-5.2 via api.z.ai, anthropic protocol).

No mock LLMs (project policy). Exercises the classify stage end-to-end against the
live endpoint: enum-constrained single assignment, multi assignment with sibling
fan-out (acceptance ② of SPEC-classify-operator.md §5), and the on_error="fallback"
path forced through a real SchemaViolation (a max_output_tokens budget too small for
any parseable JSON, so L1/L3 exhaust for real — same trick family as the key-pool
401 test)."""
from __future__ import annotations

import json
import os
import random

import pytest

from labelkit.operators.classify import ClassifyStage
from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ClassSpec,
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
from labelkit.common.runtime.llm_client import LLMClient
from labelkit.common.runtime.schema_engine import SchemaEngine
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import PipelineItem, Record, RecordRef

from tests.conftest import ZAI_BASE_URL, ZAI_KEY_ENV, ZAI_MODEL

pytestmark = pytest.mark.integration

# Any object schema works for the (unused) user-schema engine slot.
USER_SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}},
               "required": ["x"], "additionalProperties": False}

CLASSES = (
    ClassSpec(name="writing",
              description="写作协助类请求：代写、改写、模板、文案等需要模型产出一段文本的请求",
              examples=("帮我写一条请假条，明天上午要去医院",)),
    ClassSpec(name="qa",
              description="知识问答与解释类请求：询问事实、原理或要求讲解概念"),
    ClassSpec(name="other",
              description="不属于以上任何一类的请求"),
)


def make_cfg(classify: ClassifyConfig, max_output_tokens: int = 500,
             max_repair_attempts: int = 2) -> ResolvedConfig:
    profile = LLMProfile(
        name="default",
        provider="anthropic",
        base_url=ZAI_BASE_URL,
        model=ZAI_MODEL,
        api_key_env=ZAI_KEY_ENV,
        max_concurrency=2,
        timeout_s=120,
        max_retries=2,
        max_output_tokens=max_output_tokens,
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
        stitch=StitchConfig(),
        extract=ExtractConfig(),
        classify=classify,
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(enabled=True, llm="default", instruction="标注。"),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline=json.dumps(USER_SCHEMA),
                            max_repair_attempts=max_repair_attempts),
        trace=TraceConfig(),
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
    llm = LLMClient(cfg.llm_profiles, cfg.embedding_profiles, metrics=None)
    engine = SchemaEngine(dict(cfg.user_schema), llm, cfg.output, metrics=None)
    return RunContext(cfg=cfg, llm=llm, schema_engine=engine, metrics=metrics,
                      rng=random.Random("42:1:classify"), batch_no=1)


def text_record(rec_id: str, text: str) -> Record:
    return Record(id=rec_id, modality="text", text=text, raw={"instruction": text},
                  ui_tree=None, image=None, ref=RecordRef("data.jsonl", 1, None, ()))


VOCAB = {"writing", "qa", "other"}


async def test_classify_single_real_labels_within_vocabulary():
    cfg = make_cfg(ClassifyConfig(enabled=True, llm="default", assignment="single",
                                  max_labels=3, fallback_class="other",
                                  classes=CLASSES))
    ctx = make_ctx(cfg)
    records = [
        text_record("aaaa000000000001", "帮我写一段给客户的道歉话术，快递发错货了"),
        text_record("aaaa000000000002", "北回归线穿过我国哪几个省份？"),
        text_record("aaaa000000000003", "哈哈哈哈哈哈哈"),
    ]
    batch = [PipelineItem(record=r) for r in records]

    result = await ClassifyStage(cfg).run(batch, ctx)

    assert result is batch
    assert len(batch) == 3                          # single mode: no fan-out ever
    by_id = {it.record.id: it for it in batch}
    for item in batch:
        assert item.status == "active"
        cl = item.classification
        assert cl is not None and cl.source == "llm"
        assert cl.label in VOCAB                    # enum hard constraint held
        assert cl.labels == (cl.label,)
        assert not item.errors
    # Clear-cut records land on their obvious class at temperature 0.
    assert by_id["aaaa000000000001"].classification.label == "writing"
    assert by_id["aaaa000000000002"].classification.label == "qa"

    decisions = [e for e in ctx.metrics.events if e[0] == "classify.decision"]
    assert len(decisions) == 3
    assert all(e[4]["source"] == "llm" and "labels" not in e[4] for e in decisions)
    assert sum(v for k, v in ctx.metrics.counters.items()
               if k.startswith("classify.classes.")) == 3


async def test_classify_multi_real_fans_out_dual_intent_record():
    cfg = make_cfg(ClassifyConfig(enabled=True, llm="default", assignment="multi",
                                  max_labels=2, fallback_class="other",
                                  classes=CLASSES))
    ctx = make_ctx(cfg)
    dual = text_record("bbbb000000000001",
                       "写一段 300 字的短文，讲解光合作用的基本原理，面向初中生")
    plain = text_record("bbbb000000000002", "秦始皇统一六国是在哪一年？")
    batch = [PipelineItem(record=dual), PipelineItem(record=plain)]

    result = await ClassifyStage(cfg).run(batch, ctx)

    assert result is batch
    for item in batch:
        assert item.status == "active"
        assert item.classification is not None
        assert set(item.classification.labels) <= VOCAB
        assert item.classification.label in item.classification.labels

    dual_items = [it for it in batch if it.record.id == dual.id]
    # The deliberately dual-intent record (write a text THAT explains a concept)
    # must hit both classes at temperature 0 → one sibling clone appended at tail.
    assert len(dual_items) == 2, (
        f"expected fan-out for the dual-intent record, got labels "
        f"{[it.classification.labels for it in dual_items]}")
    assert dual_items[0].classification.labels == dual_items[1].classification.labels
    assert {it.classification.label for it in dual_items} == {"qa", "writing"}
    assert dual_items[0].record is dual_items[1].record       # shared frozen Record
    assert batch[-1] is dual_items[1]                          # clone at the tail
    assert ctx.metrics.counters.get("classify.multi_label_records") == 1

    plain_items = [it for it in batch if it.record.id == plain.id]
    assert len(plain_items) == 1                               # no fan-out for k=1


async def test_classify_fallback_real_schema_exhaustion():
    # max_output_tokens=2 truncates every response (first call AND both L3 repair
    # rounds) below any parseable JSON → a REAL SchemaViolation after repair
    # exhaustion → on_error="fallback" files the record into the fallback class,
    # keeps it alive, and leaves item.errors empty (R4).
    cfg = make_cfg(ClassifyConfig(enabled=True, llm="default", assignment="single",
                                  max_labels=3, fallback_class="other",
                                  on_error="fallback", classes=CLASSES),
                   max_output_tokens=2, max_repair_attempts=1)
    ctx = make_ctx(cfg)
    batch = [PipelineItem(record=text_record(
        "cccc000000000001", "帮我写一条请假条，明天上午要去医院"))]

    result = await ClassifyStage(cfg).run(batch, ctx)

    item = result[0]
    assert item.status == "active"
    cl = item.classification
    assert cl is not None
    assert cl.source == "fallback"
    assert cl.label == "other" and cl.labels == ("other",)
    assert cl.detail.get("kind") == "classification_invalid"
    assert not item.errors                          # R4: no StageError on fallback
    assert ctx.metrics.counters.get("classify.fallback") == 1
    errors = [e for e in ctx.metrics.events if e[0] == "error"]
    assert errors and errors[0][4]["kind"] == "classification_invalid"
    decisions = [e for e in ctx.metrics.events if e[0] == "classify.decision"]
    assert len(decisions) == 1 and decisions[0][4]["source"] == "fallback"

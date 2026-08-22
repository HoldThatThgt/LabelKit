"""v1.12 frame-granularity integration tests — REAL endpoint (glm-5.2 via api.z.ai,
anthropic protocol).

No mock LLMs (project policy). Pins the two v1.12 frame-level LLM surfaces
(SPEC-frame-annotation.md §3.9 integration row) against the live endpoint:

1. M13 classify_frames — ONE batched closed-set verdict call over a five-member
   text session (the public direct-call surface, verify's fourth lazy-loaded
   repair leg): every member lands inside the frame class table with
   source="llm" — no fallback.
2. M5 annotate_member — one text member through the full four-layer schema
   path (explicit schema=cfg.frame_schema routing — L0–L3, no L2.5, no
   resolved_at): the product validates against the frame schema.
3. M5 annotate_member on a ui member — single-frame screenshot + tree smoke
   over the REAL examples/ui fixture (image_1.png + uitree_1.jsonl): one real
   vision call yields a schema-valid frame object.

The integration suite stays on the z.ai glm-5.2 endpoint (model discipline
unchanged); the DeepSeek endpoint of examples/mix belongs to the mix E2E
acceptance run, never to this suite.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
from pathlib import Path

import jsonschema
import pytest

from labelkit.operators.annotate import annotate_member
from labelkit.operators.classify import classify_frames
from labelkit.operators.ingest import _parse_ui_tree
from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ClassSpec,
    ConsoleConfig,
    DedupConfig,
    ExtractConfig,
    FrameAnnotateConfig,
    FrameClassifyConfig,
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
from labelkit.common.inference.credentials import resolve_credentials
from labelkit.common.inference.schema_engine import SchemaEngine
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import ImageRef, Record, RecordRef

from tests.conftest import ZAI_BASE_URL, ZAI_KEY_ENV, ZAI_MODEL
from tests.llm_client_helpers import make_llm_client as _client

pytestmark = pytest.mark.integration

UI_DATA_DIR = Path(__file__).resolve().parents[2] / "examples" / "ui" / "data"

# Any object schema works for the (unused) user-schema engine slot.
USER_SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}},
               "required": ["x"], "additionalProperties": False}

# The examples/mix frame schema shape ({intent, entities}) — the §3.9 small schema.
FRAME_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "entities": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["intent", "entities"],
    "additionalProperties": False,
}

FRAME_CLASSES = (
    ClassSpec(name="task_request",
              description="发起一项新任务的请求：新的查询、新的委托，开启一件此前未在办的事"),
    ClassSpec(name="followup",
              description="对进行中任务的追问、修改、补充约束或收尾要求"),
    ClassSpec(name="chitchat",
              description="与任务无关的寒暄、感叹、闲聊"),
    ClassSpec(name="other",
              description="不属于以上任何一类的请求"),
)

FRAME_VOCAB = {"task_request", "followup", "chitchat", "other"}

TEXT_FRAME_INSTRUCTION = (
    "你是个人助理请求流的帧级标注员。对给定的单条用户请求，标注其意图"
    "（intent，一个动宾短语）与其中出现的实体列表（entities，如时间、地点、"
    "车次、金额；没有实体则给空数组）。")
UI_FRAME_INSTRUCTION = (
    "你是移动端界面单帧的帧级标注员。根据这一帧的屏幕截图与 UI 控件树，"
    "标注该帧上正在进行的操作意图（intent，一个动宾短语）与画面可见的"
    "关键实体列表（entities，如商品名、金额、按钮文字；没有则给空数组）。")


def _profile(max_output_tokens: int = 4096) -> LLMProfile:
    # Mirrors examples/config.toml ([llm.default]): structured output (anthropic
    # forced tool use) + vision on; retries trimmed for test budget; no
    # context_window declared — budget off ⇒ classify_frames dispatches a
    # single window over all members (deterministic one-call shape).
    return LLMProfile(
        name="default",
        provider="anthropic",
        base_url=ZAI_BASE_URL,
        model=ZAI_MODEL,
        api_key_env=ZAI_KEY_ENV,
        max_concurrency=4,
        timeout_s=120,
        max_retries=2,
        supports_structured_output=True,
        supports_vision=True,
        max_output_tokens=max_output_tokens,
        temperature=0.0,
    )


def make_cfg(*, modality: str = "text",
             frame_classify: FrameClassifyConfig | None = None,
             frame_annotate: FrameAnnotateConfig | None = None) -> ResolvedConfig:
    return ResolvedConfig(
        tool=ToolConfig(),
        console=ConsoleConfig(),
        llm_profiles={"default": _profile()},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality=modality, input="in"),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(enabled=True),
        stitch=StitchConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(enabled=True, llm="default", instruction="标注。"),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline=json.dumps(USER_SCHEMA)),
        trace=TraceConfig(),
        rubric=Rubric(name="default:trajectory", criteria=()),
        class_views={},
        user_schema=USER_SCHEMA,
        limit=None,
        strict=False,
        dry_run=False,
        config_path="config.toml",
        project_path="project.toml",
        config_digest="sha256:0",
        project_digest="sha256:0",
        frame_classify=frame_classify if frame_classify is not None
        else FrameClassifyConfig(),
        frame_annotate=frame_annotate if frame_annotate is not None
        else FrameAnnotateConfig(),
        frame_class_views={},
        frame_schema=FRAME_SCHEMA if frame_annotate is not None else None,
    )


class _RecordingMetrics:
    def __init__(self):
        self.counters: dict[str, int] = {}
        self.events: list[tuple] = []

    def count(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None) -> None:
        self.events.append((ev, stage, batch_no, record_ids, payload or {}))


class _DirectTasks:
    async def run_group(self, request):
        results = [None] * len(request.tasks)

        async def run_one(index, spec):
            results[index] = await spec.operation()

        async with asyncio.TaskGroup() as group:
            for index, spec in enumerate(request.tasks):
                group.create_task(run_one(index, spec))
        return tuple(results)


def make_ctx(cfg) -> RunContext:
    metrics = _RecordingMetrics()
    llm = _client(cfg.llm_profiles, cfg.embedding_profiles,
                       resolve_credentials(cfg), metrics=None)
    engine = SchemaEngine(dict(cfg.user_schema), llm, cfg.output, metrics=None)
    return RunContext(cfg=cfg, llm=llm, schema_engine=engine,
                      rng=random.Random("42:1:frame"), batch_no=1,
                      metrics=metrics, tasks=_DirectTasks(),
                      task_namespace="integration:frame:1")


def text_record(rec_id: str, text: str) -> Record:
    return Record(id=rec_id, modality="text", text=text, raw={"text": text},
                  ui_tree=None, image=None, ref=RecordRef("data.jsonl", 1, None, ()))


def ui_record(n: int) -> Record:
    """uitree_<n>.jsonl + image_<n>.png (examples/ui fixture) → ui Record,
    following the M2 rules: §6.2 parse via the real parser and
    id = sha256(tree_bytes + image_bytes)[:16] (test_stream_llm 同款)."""
    tree_bytes = (UI_DATA_DIR / f"uitree_{n}.jsonl").read_bytes()
    image_path = UI_DATA_DIR / f"image_{n}.png"
    image_bytes = image_path.read_bytes()
    tree, reason = _parse_ui_tree(tree_bytes)
    assert tree is not None, f"uitree_{n}.jsonl: {reason}"
    return Record(
        id=hashlib.sha256(tree_bytes + image_bytes).hexdigest()[:16],
        modality="ui", text=None, raw=None, ui_tree=tree,
        image=ImageRef(path=image_path, format="png",
                       size_bytes=len(image_bytes)),
        ref=RecordRef(source_file=f"uitree_{n}.jsonl", line_no=None,
                      pair_index=n, generated_from=()),
    )


# ── 1. M13 classify_frames: batched closed-set verdict reliability ──────────

async def test_classify_frames_real_batch_within_vocabulary_no_fallback():
    cfg = make_cfg(frame_classify=FrameClassifyConfig(
        enabled=True, llm="default", fallback_class="other",
        classes=FRAME_CLASSES))
    ctx = make_ctx(cfg)
    members = [
        text_record("aaaa000000000001", "帮我查下周五上海到北京的高铁票，上午出发的"),
        text_record("aaaa000000000002", "订 G102 次的二等座一张"),
        text_record("aaaa000000000003", "换个靠窗的座位"),
        text_record("aaaa000000000004", "今天天气真不错，适合出去走走"),
        text_record("aaaa000000000005", "改签好了把行程单发到我邮箱"),
    ]

    result = await classify_frames(members, ctx)    # ONE real window call

    assert set(result) == {m.id for m in members}   # every member keyed
    for member in members:
        cl = result[member.id]
        assert cl.source == "llm", (member.id, cl)  # no fallback anywhere
        assert cl.label in FRAME_VOCAB, (member.id, cl.label)
        assert cl.labels == (cl.label,)             # frame verdicts are single-label
        assert not cl.detail                        # clean verdict, no failure trace
    # Clear-cut members land on their obvious frame class at temperature 0
    # (sanctioned relaxation if the model ever drifts: keep the vocabulary +
    # no-fallback assertions above as the pinned behavior).
    assert result["aaaa000000000001"].label == "task_request"
    assert result["aaaa000000000004"].label == "chitchat"

    # Budget off ⇒ exactly one window dispatched; no window failure counted.
    assert ctx.metrics.counters.get("frame_classify.calls") == 1
    assert "frame_classify.fallback" not in ctx.metrics.counters
    assert "frame_classify.window_failures" not in ctx.metrics.counters


# ── 2. M5 annotate_member: the full four-layer path on a text member ────────

async def test_annotate_member_real_text_product_passes_frame_schema():
    cfg = make_cfg(frame_annotate=FrameAnnotateConfig(
        enabled=True, llm="default", instruction=TEXT_FRAME_INSTRUCTION))
    ctx = make_ctx(cfg)
    member = text_record("bbbb000000000001",
                         "帮我查下周五上海到北京的高铁票，上午出发的")

    annotation = await annotate_member(member, ctx)  # ONE real call, L0–L3

    assert annotation is not None
    # complete_validated returned ⇒ the L2 guarantee held; re-checked explicitly
    # against the frame schema (annotate_llm 同款断言口径).
    jsonschema.Draft202012Validator(FRAME_SCHEMA).validate(dict(annotation.output))
    assert set(annotation.output) == {"intent", "entities"}
    assert str(annotation.output["intent"]).strip()
    assert annotation.attempts >= 1
    assert annotation.model                          # provider model string present
    assert annotation.usage.prompt_tokens > 0
    assert annotation.usage.completion_tokens > 0
    assert ctx.metrics.counters.get("frame_annotate.annotated") == 1
    assert "frame_annotate.failed" not in ctx.metrics.counters


# ── 3. M5 annotate_member: ui single-frame screenshot + tree smoke ──────────

async def test_annotate_member_real_ui_single_frame_smoke():
    cfg = make_cfg(modality="ui", frame_annotate=FrameAnnotateConfig(
        enabled=True, llm="default", instruction=UI_FRAME_INSTRUCTION))
    ctx = make_ctx(cfg)
    member = ui_record(1)                            # examples/ui 既有截图+树

    annotation = await annotate_member(member, ctx)  # ONE real vision call

    assert annotation is not None
    jsonschema.Draft202012Validator(FRAME_SCHEMA).validate(dict(annotation.output))
    assert set(annotation.output) == {"intent", "entities"}
    assert str(annotation.output["intent"]).strip()
    assert annotation.attempts >= 1 and annotation.model
    assert ctx.metrics.counters.get("frame_annotate.annotated") == 1
    assert "frame_annotate.failed" not in ctx.metrics.counters

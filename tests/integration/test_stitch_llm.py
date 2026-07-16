"""v1.9 stitch integration tests — REAL endpoint (glm-5.2 via api.z.ai, anthropic protocol).

No mock LLMs (project policy). Pins the M16 judgment surface against the live
endpoint using the REAL examples/stream fixtures (task A 点外卖 frames 1-8 with
the frame-5 social interruption screen; task B 打车 frames 9-13; frame 14 the
trailing launcher screen) — the four §3.6 judgment cases:

1. clear resume — the post-interruption 点外卖 tail (frames 6-8) against an
   open 点外卖 thread (frames 1-4): verdict resume naming that thread, and the
   T9 mechanical priors independently confirm the merge (the full conservative
   conjunction holds end to end).
2. clear new — the 打车 head (frames 9-11) against the same 点外卖 thread:
   verdict new (thread_ref null ∨ non-resume), zero prior support asserted.
3. ambiguous refuse — the unrelated social-notification frame (frame 5) as a
   rescue-shaped single-frame candidate: the conservative-bias instruction
   demands new (错缝的代价高于漏缝).
4. position perturbation [N-8] — case-1's candidate against a TWO-thread pool
   presented in both orders: the verdict must keep pointing at the 点外卖
   thread's card ordinal under either presentation.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
from pathlib import Path

import pytest

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
    StitchConfig,
    StreamConfig,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)
from labelkit.operators.ingest import _parse_ui_tree
from labelkit.common.runtime.llm_client import LLMClient
from labelkit.common.runtime.schema_engine import SchemaEngine
from labelkit.operators.stitch import (
    judge_stitch,
    prior_hits,
    render_candidate_card,
    render_thread_card,
)
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import ImageRef, Record, RecordRef

from tests.conftest import ZAI_BASE_URL, ZAI_KEY_ENV, ZAI_MODEL

pytestmark = pytest.mark.integration

DATA_DIR = Path(__file__).resolve().parents[2] / "examples" / "stream" / "data"

# examples/stream/project.toml-style domain hint, adapted to the stitch face.
STITCH_CONTEXT = ("手机屏幕操作录屏流；同一任务可能被其他 App 打断后恢复，"
                  "任务实体（商品、订单、地点）跨碎片延续是恢复的关键证据")

VERDICT_VOCAB = {"resume", "new"}
CONFIDENCE_VOCAB = {"high", "medium", "low"}


def _profile(name: str) -> LLMProfile:
    # Mirrors examples/config.toml [llm.default]: structured output (anthropic
    # forced tool use); vision irrelevant — stitch judgments are pure text.
    return LLMProfile(
        name=name,
        provider="anthropic",
        base_url=ZAI_BASE_URL,
        model=ZAI_MODEL,
        api_key_env=ZAI_KEY_ENV,
        max_concurrency=4,
        timeout_s=120,
        max_retries=2,
        supports_structured_output=True,
        supports_vision=True,
        max_output_tokens=2048,
        temperature=0.0,
        api_key=os.environ.get(ZAI_KEY_ENV, ""),
    )


def make_cfg(votes: int = 1) -> ResolvedConfig:
    return ResolvedConfig(
        tool=ToolConfig(),
        llm_profiles={"default": _profile("default")},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality="ui", input="data"),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(enabled=True, strategy="hybrid", llm="default"),
        stitch=StitchConfig(enabled=True, llm="default", context=STITCH_CONTEXT,
                            votes=votes),
        extract=ExtractConfig(),
        classify=ClassifyConfig(),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(enabled=True, llm="default", instruction="标注"),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=TraceConfig(),
        rubric=Rubric(name="default:trajectory", criteria=()),
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
                      rng=random.Random("42:1:stitch"), batch_no=1)


# ── real fixture loading (examples/stream/data, the M2 id rules) ────────────

_FRAME_CACHE: dict[int, Record] = {}


def stream_frame(n: int) -> Record:
    """uitree_<n>.jsonl + image_<n>.png → Record via the real M2 parser;
    id = sha256(tree_bytes + image_bytes)[:16]."""
    if n not in _FRAME_CACHE:
        tree_bytes = (DATA_DIR / f"uitree_{n}.jsonl").read_bytes()
        image_path = DATA_DIR / f"image_{n}.png"
        image_bytes = image_path.read_bytes()
        tree, reason = _parse_ui_tree(tree_bytes)
        assert tree is not None, f"uitree_{n}.jsonl: {reason}"
        _FRAME_CACHE[n] = Record(
            id=hashlib.sha256(tree_bytes + image_bytes).hexdigest()[:16],
            modality="ui", text=None, raw=None, ui_tree=tree,
            image=ImageRef(path=image_path, format="png",
                           size_bytes=len(image_bytes)),
            ref=RecordRef(source_file=f"uitree_{n}.jsonl", line_no=None,
                          pair_index=n, generated_from=()),
        )
    return _FRAME_CACHE[n]


def frames(*ns: int) -> list[Record]:
    return [stream_frame(n) for n in ns]


def food_thread_card(cfg, candidate_head: Record | None, index: int = 1) -> str:
    """The open 点外卖 thread (task-A head, frames 1-4): session span [0, 3]."""
    return render_thread_card(index, "点外卖", frames(1, 2, 3, 4), (0, 3), 1,
                              candidate_head, cfg)


def taxi_thread_card(cfg, candidate_head: Record | None, index: int) -> str:
    """An open 打车 thread (frames 9-10): session span [8, 9]."""
    return render_thread_card(index, "打车", frames(9, 10), (8, 9), 1,
                              candidate_head, cfg)


# ── 1. clear resume: 点外卖 tail resumes the 点外卖 thread ───────────────────

async def test_clear_resume_names_the_open_thread():
    cfg = make_cfg()
    ctx = make_ctx(cfg)
    candidate = frames(6, 7, 8)                # 加购/下单/订单完成 tail of task A
    cards = [food_thread_card(cfg, candidate[0])]
    cand_card = render_candidate_card("episode", candidate, (5, 7), cfg)

    outcome = await judge_stitch(cards, cand_card, ctx,
                                 record_ids=(candidate[0].id,))  # ONE real call

    assert outcome is not None
    assert outcome["verdict"] in VERDICT_VOCAB
    assert outcome["confidence"] in CONFIDENCE_VOCAB
    assert isinstance(outcome["task_name"], str) and outcome["task_name"].strip()
    assert isinstance(outcome["reason"], str)
    # Real semantic assertion: the candidate continues the 麻辣烫 order flow —
    # entity continuity across the interruption is unambiguous at temperature 0.
    assert outcome["verdict"] == "resume", outcome
    assert outcome["thread_ref"] == 1, outcome
    # The T9 conjunction's mechanical side independently confirms the merge —
    # the full conservative gate (LLM resume ∧ prior hit) holds end to end.
    hits = prior_hits(frames(1, 2, 3, 4), [stream_frame(4)], candidate)
    assert hits, "mechanical priors must support the resume on this fixture"


# ── 2. clear new: 打车 head against the 点外卖 thread ────────────────────────

async def test_clear_new_task_opens_thread():
    cfg = make_cfg()
    ctx = make_ctx(cfg)
    candidate = frames(9, 10, 11)              # 打车首页/目的地/车型 — task B
    cards = [food_thread_card(cfg, candidate[0])]
    cand_card = render_candidate_card("episode", candidate, (8, 10), cfg)

    outcome = await judge_stitch(cards, cand_card, ctx,
                                 record_ids=(candidate[0].id,))  # ONE real call

    assert outcome is not None
    assert outcome["verdict"] == "new", outcome
    # schema shape held: thread_ref is null-or-int; new carries no valid ref
    assert outcome["thread_ref"] is None or isinstance(outcome["thread_ref"], int)
    # the mechanical priors agree — no leg fires across food × taxi
    assert prior_hits(frames(1, 2, 3, 4), [stream_frame(4)], candidate) == []


# ── 3. ambiguous refuse: unrelated notification screen stays unstitched ─────

async def test_ambiguous_candidate_refused_conservatively():
    cfg = make_cfg()
    ctx = make_ctx(cfg)
    candidate = frames(5)                      # com.example.social notification
    cards = [food_thread_card(cfg, candidate[0])]
    cand_card = render_candidate_card("rescue", candidate, (4, 4), cfg)

    outcome = await judge_stitch(cards, cand_card, ctx,
                                 record_ids=(candidate[0].id,))  # ONE real call

    assert outcome is not None
    # Conservative bias (T9/[N-6][N-7]): an unrelated single-frame insert must
    # NOT be stitched into the food thread — 证据不足一律判 new.
    assert outcome["verdict"] == "new", outcome


# ── 4. position perturbation does not flip the verdict ([N-8]) ──────────────

async def test_candidate_position_perturbation_keeps_target():
    cfg = make_cfg()
    ctx = make_ctx(cfg)
    candidate = frames(6, 7, 8)                # task-A tail (case-1 candidate)
    cand_card = render_candidate_card("episode", candidate, (5, 7), cfg)

    # order 1: food card first; order 2: taxi card first — TWO real calls
    order1 = [food_thread_card(cfg, candidate[0], index=1),
              taxi_thread_card(cfg, candidate[0], index=2)]
    order2 = [taxi_thread_card(cfg, candidate[0], index=1),
              food_thread_card(cfg, candidate[0], index=2)]
    outcome1 = await judge_stitch(order1, cand_card, ctx,
                                  record_ids=(candidate[0].id,))
    outcome2 = await judge_stitch(order2, cand_card, ctx,
                                  record_ids=(candidate[0].id,))

    assert outcome1 is not None and outcome2 is not None
    assert outcome1["verdict"] == "resume", outcome1
    assert outcome2["verdict"] == "resume", outcome2
    # the referenced CARD tracks the food thread across both presentations
    assert outcome1["thread_ref"] == 1, outcome1
    assert outcome2["thread_ref"] == 2, outcome2

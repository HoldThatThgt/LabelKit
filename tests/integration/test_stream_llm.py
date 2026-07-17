"""v1.8 stream integration tests — REAL endpoint (glm-5.2 via api.z.ai, anthropic protocol).

No mock LLMs (project policy). Pins the stream-mode LLM surfaces against the live
endpoint using the REAL examples/stream/data/s1-serial-noise fixtures (task A 点外卖
frames 1-8 with the frame-5 social interruption screen; task B 打车 frames 9-13):

1. M14 judge_window — one 8-frame window, per-frame relation within the frozen
   5-value vocabulary (enum hard constraint via forced tool use) + the frame-5
   noise-semantics assertion (SPEC-stream-segmentation.md §3.3 criteria template).
2. M15 extract_transition — one adjacent-pair call, action_type within the frozen
   11-value vocabulary (S15), non-empty description, Transition.index carried.
3. M5 sequence annotation — a 25-member episode downsampled to sequence_frames=20
   keyframes: EXACTLY 20 image blocks in one request, right at the Anthropic
   >20-images/request hard-reject threshold (S28) — the endpoint-behavior pin of
   acceptance item ④ (SPEC §6).
4. M7 stream review round — defect_verdict_schema() roundtrip on a deliberately
   mismatched (episode, annotation) pair: forced-tool structured output with
   ["array","null"]/["string","null"] nullable unions accepted by the real
   endpoint (S7), verdict in {pass, fail}, defect entries carrying all four keys.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest

from labelkit.operators.annotate import annotate_record, build_annotate_prompt
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
from labelkit.operators.extract import extract_transition
from labelkit.operators.ingest import _parse_ui_tree
from labelkit.common.runtime.llm_client import LLMClient
from labelkit.common.runtime.schema_engine import SchemaEngine
from labelkit.operators.segment import judge_window
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import (
    Annotation,
    ImageRef,
    PipelineItem,
    Record,
    RecordRef,
    Transition,
    Usage,
)
from labelkit.operators.verify import VerifyStage, boundary_margin_text

from tests.conftest import ZAI_BASE_URL, ZAI_KEY_ENV, ZAI_MODEL

pytestmark = pytest.mark.integration

DATA_DIR = (Path(__file__).resolve().parents[2]
            / "examples" / "stream" / "data" / "s1-serial-noise")

# The examples/stream/project.toml user schema (three required string fields).
USER_SCHEMA = {
    "type": "object",
    "properties": {
        "task_label": {"type": "string"},
        "app": {"type": "string"},
        "summary": {"type": "string", "maxLength": 100},
    },
    "required": ["task_label", "app", "summary"],
    "additionalProperties": False,
}

# examples/stream/project.toml-style domain context + its sequence-annotation task.
SEGMENT_CONTEXT = ("手机屏幕操作录屏流；通知面板、弹窗等与前后操作无关的短暂插入屏"
                   "属于干扰帧")
ANNOTATE_INSTRUCTION = ("你是移动端操作序列标注员。根据动作序列与关键帧，\n"
                        "标注该操作序列的任务标签（用户在做什么）、所属应用与一句话摘要。")

# Frozen closed-set vocabularies, mirrored literally from the spec (§3.3 / S15 / S7)
# so a schema-side drift would fail here rather than pass tautologically.
RELATION_VOCAB = {"continues", "advances", "returns_to_entry", "context_switch",
                  "interruption"}
ACTION_VOCAB = {"click", "long_press", "input_text", "scroll", "drag", "open_app",
                "app_switch", "navigate_back", "navigate_home", "wait", "other"}
DEFECT_KIND_VOCAB = {"label_mismatch", "off_task_members", "missing_head",
                     "missing_tail", "missing_members"}


def _profile(name: str, max_output_tokens: int) -> LLMProfile:
    # Mirrors examples/config.toml ([llm.default]/[llm.judge]): structured output
    # (anthropic forced tool use) + vision on; retries trimmed for test budget.
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
        max_output_tokens=max_output_tokens,
        temperature=0.0,
        api_key=os.environ.get(ZAI_KEY_ENV, ""),
    )


def make_cfg(sequence_frames: int = 20) -> ResolvedConfig:
    return ResolvedConfig(
        tool=ToolConfig(),
        llm_profiles={"default": _profile("default", 4096),
                      "judge": _profile("judge", 2048)},
        embedding_profiles={},
        run=RunConfig(output="out.jsonl", modality="ui", input="data"),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(enabled=True, strategy="hybrid", llm="default",
                              window=8, min_len=2, context=SEGMENT_CONTEXT),
        stitch=StitchConfig(),
        extract=ExtractConfig(enabled=True, llm="default"),
        classify=ClassifyConfig(),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(enabled=True, llm="default",
                                instruction=ANNOTATE_INSTRUCTION,
                                sequence_frames=sequence_frames),
        verify=VerifyConfig(enabled=True, llm="judge", policy="repair",
                            max_repair_rounds=1),
        output=OutputConfig(schema_inline=json.dumps(USER_SCHEMA, ensure_ascii=False)),
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
                      rng=random.Random("42:1:stream"), batch_no=1)


# ── real fixture loading (examples/stream/data/s1-serial-noise, M2 rules) ──

_FRAME_CACHE: dict[int, Record] = {}


def stream_frame(n: int) -> Record:
    """uitree_<n>.jsonl + image_<n>.png → Record, following the M2 UI rules:
    §6.2 node field mapping via the real parser (package lands in extra) and
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


def make_episode(members: tuple[Record, ...]) -> Record:
    """Sequence Record per the S24 convention (segment._emit_episode mirror):
    id = sha256("\\n".join(member ids))[:16], payload fields None, ref from the
    first member."""
    first = members[0]
    joined = "\n".join(r.id for r in members)
    return Record(
        id=hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16],
        modality="ui", text=None, raw=None, ui_tree=None, image=None,
        ref=RecordRef(source_file=first.ref.source_file, line_no=None,
                      pair_index=first.ref.pair_index, generated_from=()),
        kind="sequence", members=members)


# ── 1. M14 window judgment: vocabulary + frame-5 noise semantics ────────────

async def test_segment_window_judgment_within_vocabulary():
    cfg = make_cfg()
    ctx = make_ctx(cfg)
    frames = [stream_frame(n) for n in range(1, 9)]   # 帧 1–8（帧 5 = social 无关屏）

    verdicts = await judge_window(frames, ctx)        # ONE real window call

    assert len(verdicts) == 8
    # Enum hard constraint: successful parse ⇒ every relation is in-vocabulary;
    # asserted against the test-local literal copy so vocabulary drift fails loudly.
    assert all(v in RELATION_VOCAB for v in verdicts), verdicts
    # Real semantic assertion: frame 5 (index 4) is the com.example.social
    # notification screen deliberately inserted into the 点外卖 flow — the §3.3
    # criteria define it as interruption (与前后活动均无关的短暂插入). Sanctioned
    # relaxation if the model drifts: verdicts[4] in {"interruption",
    # "context_switch"}.
    assert verdicts[4] == "interruption", verdicts
    # The 点外卖 flow around it keeps advancing — frame 4→6 entity continuity
    # (川味麻辣烫/¥32) means the direct neighbors are non-noise at temperature 0.
    assert verdicts[3] != "interruption" and verdicts[5] != "interruption", verdicts

    boundary_events = [e for e in ctx.metrics.events if e[0] == "segment.boundary"]
    assert len(boundary_events) == 1
    payload = boundary_events[0][4]
    assert [r["relation"] for r in payload["relations"]] == verdicts
    assert payload["model"]                            # verdict came from the LLM


# ── 2. M15 transition extraction: 11-value vocabulary + index carry ─────────

async def test_extract_transition_action_in_vocabulary():
    cfg = make_cfg()
    ctx = make_ctx(cfg)
    prev, curr = stream_frame(9), stream_frame(10)     # 打车首页 → 目的地输入

    transition = await extract_transition(prev, curr, 0, ctx)  # ONE real call

    assert transition.index == 0                       # carried through unchanged
    action = transition.action
    assert action["action_type"] in ACTION_VOCAB, action
    assert isinstance(action["description"], str) and action["description"].strip()
    # A clean extraction, not the S16 fallback placeholder (which would mask a
    # real schema failure behind on_error="fallback").
    assert not transition.detail, transition.detail
    assert transition.model and transition.attempts >= 1


# ── 3. M5 sequence annotation: 25 members → 20 keyframes (Anthropic image cap) ──

async def test_sequence_annotate_downsampling_25_frames():
    cfg = make_cfg(sequence_frames=20)
    ctx = make_ctx(cfg)
    # 25 members cycling the task-A frames (noise frame 5 excluded); member ids
    # de-duplicated via dataclasses.replace with an ordinal suffix (id is not
    # recomputed — the id rule binds at M2, S24).
    base = [stream_frame(n) for n in (1, 2, 3, 4, 6, 7, 8)]
    members = tuple(
        replace(base[i % len(base)], id=f"{base[i % len(base)].id[:12]}{i:04x}")
        for i in range(25))
    assert len({m.id for m in members}) == 25
    episode = make_episode(members)

    # Endpoint-behavior pin (S28 / acceptance ④): the S28 downsample formula keeps
    # EXACTLY 20 keyframes for n=25, k=20 — 20 image blocks in ONE request, right
    # at the Anthropic >20-images hard-reject threshold.
    prompt = build_annotate_prompt(episode, cfg, ctx.schema_engine.user_schema_text)
    image_parts = [p for m in prompt.messages for p in m.parts if p.kind == "image"]
    assert len(image_parts) == 20

    annotation = await annotate_record(episode, ctx)   # ONE real 20-image call

    # complete_validated returned ⇒ the L2 guarantee held; re-checked explicitly.
    jsonschema.validate(annotation.output, USER_SCHEMA)
    assert set(annotation.output) == {"task_label", "app", "summary"}
    assert all(str(annotation.output[k]).strip()
               for k in ("task_label", "app", "summary"))
    assert annotation.attempts >= 1 and annotation.model


# ── 4. M7 stream review: defect_verdict_schema roundtrip on a mismatch ──────

# Hand-built task-B transitions (the §10.1 frozen step-line evidence); the
# annotation below deliberately describes task A (点外卖) instead — 标注与步骤不符.
_TAXI_ACTIONS = (
    {"action_type": "input_text", "target": "输入目的地", "value": "虹桥机场",
     "description": "在目的地输入框键入虹桥机场"},
    {"action_type": "click", "target": "经济型 ¥58", "value": None,
     "description": "选择经济型车型"},
    {"action_type": "click", "target": "呼叫经济型 ¥58", "value": None,
     "description": "确认呼叫经济型网约车"},
    {"action_type": "wait", "target": None, "value": None,
     "description": "等待司机接单，行程开始"},
)
_MISMATCHED_OUTPUT = {"task_label": "点外卖",
                      "app": "美食外卖",
                      "summary": "在外卖应用中搜索麻辣烫、加入购物车并完成下单"}


async def test_defect_verdict_schema_roundtrip():
    cfg = make_cfg()
    ctx = make_ctx(cfg)
    members = tuple(stream_frame(n) for n in range(9, 14))   # 打车帧 9–13
    episode = make_episode(members)
    sid = "sess-taxi-000001"

    item = PipelineItem(record=episode, session_id=sid)
    item.transitions = tuple(
        Transition(index=i, action=action, model=ZAI_MODEL, attempts=1, detail={})
        for i, action in enumerate(_TAXI_ACTIONS))
    item.annotation = Annotation(output=_MISMATCHED_OUTPUT, model=ZAI_MODEL,
                                 attempts=1, usage=Usage())
    # Post-segment batch state for the [边界余量] evidence: the five absorbed
    # member frames plus the trailing launcher frame 14 judged noise.
    batch = [PipelineItem(record=m, status="absorbed", session_id=sid)
             for m in members]
    batch.append(PipelineItem(record=stream_frame(14), status="dropped_noise",
                              session_id=sid))
    batch.append(item)
    margin = boundary_margin_text(item, batch, cfg.segment.digest_max_chars)
    assert "去向: noise" in margin                     # margin evidence assembled

    stage = VerifyStage(cfg)
    verdict, merged, fail_critiques, defects = await stage._judge_round_sequence(
        item, item.annotation, 1, ctx, boundary_margin=margin)  # ONE real call

    assert verdict in {"pass", "fail"}
    # Real semantic assertion: the annotation claims 点外卖 while steps and
    # first/last screenshots are unambiguously the taxi flow — fail at
    # temperature 0. (Relax to the schema-only assertions above if the model
    # ever drifts; the schema roundtrip is the pinned behavior.)
    assert verdict == "fail", (verdict, merged)
    assert defects                                     # S7: fail ⇒ non-empty table
    for defect in defects:
        assert set(defect) == {"kind", "members", "position", "detail"}, defect
        assert defect["kind"] in DEFECT_KIND_VOCAB
        assert defect["members"] is None or isinstance(defect["members"], list)
        assert defect["position"] is None or isinstance(defect["position"], str)
        assert isinstance(defect["detail"], str)
    assert isinstance(fail_critiques, list)

    verdict_events = [e for e in ctx.metrics.events if e[0] == "verify.verdict"]
    assert len(verdict_events) == 1
    assert "defects" in verdict_events[0][4]           # defect table rides the event

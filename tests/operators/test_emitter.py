"""Offline tests for M11 emitter (pure I/O + assembly logic; no LLM involved)."""
from __future__ import annotations

import json
import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from labelkit import TOOL_VERSION
from labelkit.common.config.model import (
    AnnotateConfig, ClassifyConfig, ClassSpec, ClassView, Criterion, DedupConfig,
    ConsoleConfig,
    ExtractConfig, FrameAnnotateConfig, FrameClassifyConfig, GenerateConfig,
    InputConfig, OutputConfig, QualityConfig,
    ResolvedConfig, ResolvedPaths, Rubric, RunConfig, SegmentConfig,
    StitchConfig, StreamConfig,
    ToolConfig,
    TraceConfig, VerifyConfig,
)
from labelkit.operators.emitter import EmitResult, Emitter, SequenceDeliveryEmitter
from labelkit.common.contracts.generation import ProjectedSequence, SequenceAssemblyRequest
from labelkit.common.errors import GenerationProjectionMismatch, InternalError, LabelKitError
from labelkit.common.contracts.types import (
    Annotation, Classification, DedupInfo, ImageRef, PipelineItem, QualityScore,
    Record, RecordRef, StageError, Transition, UINode, UITree, Usage,
    VerificationResult,
)

USER_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "topic": {"type": "string"},
        "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]},
    },
    "required": ["intent", "topic", "difficulty"],
    "additionalProperties": False,
}

# v1.12：帧级输出 Schema（spec §3.6 members 示例同形），写前校验兜底用
FRAME_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "entities": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["intent", "entities"],
    "additionalProperties": False,
}

RUN_STARTED_AT = datetime(2026, 7, 2, 10, 27, 41, tzinfo=timezone.utc)


class EngineStub:
    """Real jsonschema validation only — validate_only is pure logic (no LLM)."""

    def __init__(self, schema=USER_SCHEMA):
        self._validator = Draft202012Validator(schema)

    def validate_only(self, obj, schema=None):
        v = self._validator if schema is None else Draft202012Validator(schema)
        return [
            "/" + "/".join(str(p) for p in e.absolute_path) + ": " + e.message
            for e in v.iter_errors(obj)
        ]


class MetricsStub:
    """v1.12：帧计数通路桩——只收 count() 键累计（M10 装配期注入面的测试端）。"""

    def __init__(self):
        self.counters: dict[str, int] = {}    # 键 = 计数器名，值 = 累计计数

    def count(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n


def make_cfg(tmp_path: Path, **kw) -> ResolvedConfig:
    out = tmp_path / "out" / "res.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    output = kw.pop("output", str(out))
    meta_mode = kw.pop("meta_mode", "inline")
    rejects = kw.pop("rejects", "refs")
    annotate_enabled = kw.pop("annotate_enabled", True)
    passthrough = tuple(kw.pop("passthrough_fields", ()))
    selection = kw.pop("selection", "threshold")
    modality = kw.pop("modality", "text")
    quality_rubric = kw.pop("quality_rubric", "default:text")
    log_format = kw.pop("log_format", "text")
    classify = kw.pop("classify", ClassifyConfig())
    segment = kw.pop("segment", SegmentConfig())
    stitch = kw.pop("stitch", StitchConfig())
    console = kw.pop("console", ConsoleConfig())   # v1.10: mode_resolved gate
    # v1.12：帧粒度三件（默认全关 = 字节等价 v1.11）
    frame_classify = kw.pop("frame_classify", FrameClassifyConfig())
    frame_annotate = kw.pop("frame_annotate", FrameAnnotateConfig())
    frame_schema = kw.pop("frame_schema", None)
    dry_run = kw.pop("dry_run", False)
    assert not kw, f"unknown overrides: {kw}"
    # v1.17（SPEC-SP §5.1）：paths 逐字段镜像 M1 的 loader._resolved_paths 派生
    # 公式（live/dry-run 的 report 后缀由这里一次裁决）——emitter 只消费它。
    stem = str(Path(output).with_suffix(""))
    paths = ResolvedPaths(
        project=str(tmp_path / "project.toml"),
        project_root=str(tmp_path),
        input=None,
        output=output,
        report=stem + (".dryrun.report.json" if dry_run else ".report.json"),
        rejects=None if rejects == "none" else stem + ".rejects.jsonl",
        sidecar=stem + ".meta.jsonl" if meta_mode == "sidecar" else None,
        trace=None,
        stream=None,
        manifest=None,
        failed_report=None,
    )
    return ResolvedConfig(
        tool=ToolConfig(log_format=log_format),
        console=console,
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output=output, modality=modality, seed=7),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=segment,
        stitch=stitch,
        extract=ExtractConfig(),
        classify=classify,
        quality=QualityConfig(
            selection=selection,
            threshold=0.3 if selection == "threshold" else None,
            top_ratio=0.5 if selection == "top_ratio" else None,
            rubric=quality_rubric,
        ),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(enabled=annotate_enabled, instruction="标注意图"),
        verify=VerifyConfig(),
        output=OutputConfig(
            schema_inline=json.dumps(USER_SCHEMA),
            meta_mode=meta_mode,
            passthrough_fields=passthrough,
            rejects=rejects,
        ),
        trace=TraceConfig(),
        rubric=Rubric(
            name="my_inline_rubric",
            criteria=(Criterion(key="clarity", description="d", pairwise_prompt="p"),),
        ),
        class_views={},
        user_schema=USER_SCHEMA,
        limit=None,
        strict=False,
        dry_run=dry_run,
        config_path="config.toml",
        project_path="project.toml",
        config_digest="sha256:c",
        project_digest="sha256:p",
        frame_classify=frame_classify,
        frame_annotate=frame_annotate,
        frame_schema=frame_schema,
        paths=paths,
    )


def classify_cfg(assignment="single", classes=("faq", "chat")) -> ClassifyConfig:
    return ClassifyConfig(
        enabled=True, assignment=assignment,
        max_labels=len(classes) if assignment == "multi" else None,
        fallback_class=classes[-1],
        classes=tuple(ClassSpec(name=n, description="d") for n in classes))


def frame_classify_cfg() -> FrameClassifyConfig:
    """v1.12：M1 形状的帧分类配置（帧类表 + fallback ∈ 表）。"""
    return FrameClassifyConfig(
        enabled=True, fallback_class="other",
        classes=(ClassSpec(name="task_request", description="d"),
                 ClassSpec(name="other", description="d")))


def frame_annotate_cfg() -> FrameAnnotateConfig:
    """v1.12：M1 形状的帧标注配置（指令 + 帧 Schema 恰一）。"""
    return FrameAnnotateConfig(enabled=True, instruction="帧标注",
                               schema_inline=json.dumps(FRAME_SCHEMA))


def make_record(rec_id="a" * 16, line_no=1, raw=None, generated=False):
    if raw is None:
        raw = {"instruction": "帮我写一条请假条", "source": "ime-log", "ts": "t"}
    if generated:
        ref = RecordRef(source_file="", line_no=None, pair_index=None,
                        generated_from=("b" * 16,),
                        generator={"llm": "default", "style": "concise"})
    else:
        ref = RecordRef(source_file="ime-2026-06.jsonl", line_no=line_no,
                        pair_index=None, generated_from=())
    return Record(id=rec_id, modality="text", text=raw.get("instruction"),
                  raw=raw, ui_tree=None, image=None, ref=ref)


def make_ui_record(rec_id="c" * 16, pair_index=2, source_file="b/uitree_2.jsonl"):
    tree = UITree(nodes=(UINode(node_id="1", parent_id=None, depth=0, role="Button",
                                text="登录", content_desc="", bounds=(0, 0, 10, 10),
                                visible=True, extra={}),))
    ref = RecordRef(source_file=source_file, line_no=None, pair_index=pair_index,
                    generated_from=())
    return Record(id=rec_id, modality="ui", text=None, raw=None, ui_tree=tree,
                  image=ImageRef(path=Path("b/image_2.png"), format="png", size_bytes=9),
                  ref=ref)


def make_seq_record(members, rec_id="e" * 16):
    """A v1.8 sequence Record per the S24 field convention: text/raw/ui_tree/
    image None, ref inherited from the FIRST member, members in order."""
    first = members[0]
    return Record(
        id=rec_id, modality=first.modality, text=None, raw=None, ui_tree=None,
        image=None,
        ref=RecordRef(source_file=first.ref.source_file, line_no=first.ref.line_no,
                      pair_index=first.ref.pair_index, generated_from=(),
                      generator=None),
        kind="sequence", members=tuple(members))


def make_item(status="active", record=None, annotated=True, scores=False,
              verified=False, dedup=True, errors=(), output=None,
              classification=None):
    record = record or make_record()
    item = PipelineItem(record=record, status=status, classification=classification)
    if dedup:
        item.dedup = DedupInfo(kind="unique", cluster_key="k" * 16, kept_id=None)
    if scores:
        item.scores = {
            "clarity": QualityScore(criterion="clarity", score=0.72,
                                    mode="pairwise_bt", detail={}),
            "__aggregate__": QualityScore(criterion="__aggregate__", score=0.72,
                                          mode="pairwise_bt", detail={}),
        }
    if annotated:
        item.annotation = Annotation(
            output=output or {"intent": "writing_assist", "topic": "请假条",
                              "difficulty": "easy"},
            model="glm-5.2", attempts=1, usage=Usage(10, 5), sc=None)
    if verified:
        item.verification = VerificationResult(verdict="pass", rounds=1, critiques=())
    item.errors = list(errors)
    return item


def run_emitter(cfg, batch, batch_no=1, finalize=True, report=None, deliver=True,
                metrics=None):
    em = Emitter(cfg, EngineStub(), run_id="ab12cd34ef56", run_started_at=RUN_STARTED_AT)
    if metrics is not None:
        em.metrics = metrics      # v1.12：M10 装配期注入的帧计数通路（鸭子面）
    em.open()
    result = em.emit_batch(batch, batch_no)
    if finalize:
        em.finalize(report or {"counts": {}}, deliver=deliver)
    return em, result


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ── main output: three meta modes ─────────────────────────────────────────

def test_inline_meta_mode_structure(tmp_path):
    cfg = make_cfg(tmp_path, meta_mode="inline", passthrough_fields=("source",))
    item = make_item(scores=True, verified=True)
    _, result = run_emitter(cfg, [item])
    assert result == EmitResult(emitted=1, rejected=0)

    rows = read_jsonl(tmp_path / "out" / "res.jsonl")
    assert len(rows) == 1
    row = rows[0]
    meta = row.pop("_meta")
    # stripping _meta must yield an object passing the user schema
    Draft202012Validator(USER_SCHEMA).validate(row)
    assert row == {"intent": "writing_assist", "topic": "请假条", "difficulty": "easy"}
    # exact _meta structure per §6.3 — all keys always present (v1.7 adds the
    # ALWAYS-PRESENT classification key between dedup and annotation; v1.8 adds
    # the ALWAYS-PRESENT stream key between source and scores)
    assert list(meta) == ["id", "run", "source", "stream", "scores", "dedup",
                          "classification", "annotation", "verification"]
    assert meta["id"] == "a" * 16
    assert meta["run"] == {"tool": TOOL_VERSION,
                           "started_at": RUN_STARTED_AT.isoformat(),
                           "project_file": "project.toml",
                           "rubric": "default:text", "seed": 7}
    assert meta["source"] == {"file": "ime-2026-06.jsonl", "line_no": 1,
                              "generated_from": [], "fields": {"source": "ime-log"},
                              "generator": None}
    assert meta["stream"] is None                  # segment disabled → null (v1.8)
    assert meta["scores"] == {"clarity": 0.72, "__aggregate__": 0.72,
                              "mode": "pairwise_bt", "batch_no": 1}
    assert meta["dedup"] == {"kind": "unique"}
    assert meta["classification"] is None          # classify disabled → null
    assert meta["annotation"] == {"model": "glm-5.2", "attempts": 1}
    # non-stream verification block: no defects key (v1.8, §9.1)
    assert meta["verification"] == {"verdict": "pass", "rounds": 1}


def test_rubric_selector_trajectory(tmp_path):
    """v1.8 (S29): _meta.run.rubric must report the trajectory selector — both
    for an explicit "default:trajectory" and for the empty selector resolved
    under stream mode (loader rule 16 mirror). Regression: the pre-v1.8
    whitelist fell through to the modality default ("default:ui")."""
    cfg = make_cfg(tmp_path, quality_rubric="default:trajectory")
    run_emitter(cfg, [make_item()])
    meta = read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]
    assert meta["run"]["rubric"] == "default:trajectory"

    cfg = make_cfg(tmp_path, quality_rubric="",
                   segment=SegmentConfig(enabled=True))
    run_emitter(cfg, [make_item()])
    meta = read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]
    assert meta["run"]["rubric"] == "default:trajectory"


def test_inline_disabled_stages_are_null(tmp_path):
    cfg = make_cfg(tmp_path)
    item = make_item(scores=False, verified=False, dedup=False)
    run_emitter(cfg, [item])
    meta = read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]
    assert meta["stream"] is None
    assert meta["scores"] is None
    assert meta["dedup"] is None
    assert meta["classification"] is None
    assert meta["verification"] is None


# ── v1.7 classification meta / scores.pool / rejects label ─────────────────

def test_meta_classification_three_states(tmp_path):
    """§9.1 classification key tri-state: null (unclassified), single-label,
    multi-label — {label, labels, source} with labels always a list."""
    cfg = make_cfg(tmp_path, classify=classify_cfg(assignment="multi"))
    unclassified = make_item(record=make_record("1" * 16, 1))
    single = make_item(
        record=make_record("2" * 16, 2),
        classification=Classification(label="faq", labels=("faq",),
                                      source="llm", detail={}))
    multi = make_item(
        record=make_record("3" * 16, 3),
        classification=Classification(label="chat", labels=("faq", "chat"),
                                      source="inherited", detail={}))
    run_emitter(cfg, [unclassified, single, multi])

    by_id = {r["_meta"]["id"]: r["_meta"]
             for r in read_jsonl(tmp_path / "out" / "res.jsonl")}
    assert by_id["1" * 16]["classification"] is None
    assert by_id["2" * 16]["classification"] == {
        "label": "faq", "labels": ["faq"], "source": "llm"}
    assert by_id["3" * 16]["classification"] == {
        "label": "chat", "labels": ["faq", "chat"], "source": "inherited"}
    # detail never reaches _meta (three-key closed shape, §9.1)
    for meta in by_id.values():
        if meta["classification"] is not None:
            assert set(meta["classification"]) == {"label", "labels", "source"}


def test_scores_pool_only_when_classify_enabled(tmp_path):
    """§9.1: scores.pool = the envelope's routing label, present ONLY when
    classify is enabled; the disabled scores block stays byte-identical."""
    cls = Classification(label="faq", labels=("faq",), source="llm", detail={})
    cfg_on = make_cfg(tmp_path, classify=classify_cfg())
    item = make_item(scores=True, classification=cls)
    run_emitter(cfg_on, [item])
    scores = read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]["scores"]
    assert scores == {"clarity": 0.72, "__aggregate__": 0.72,
                      "mode": "pairwise_bt", "batch_no": 1, "pool": "faq"}

    out2 = tmp_path / "off" / "res.jsonl"
    out2.parent.mkdir(parents=True)
    cfg_off = make_cfg(tmp_path, output=str(out2))
    # even a (stray) classification must not leak pool when classify is off
    run_emitter(cfg_off, [make_item(scores=True, classification=cls)])
    scores_off = read_jsonl(out2)[0]["_meta"]["scores"]
    assert "pool" not in scores_off
    assert scores_off == {"clarity": 0.72, "__aggregate__": 0.72,
                          "mode": "pairwise_bt", "batch_no": 1}


def test_rejects_label_key_when_classify_enabled_refs_and_full(tmp_path):
    """R5 (§9.2): classify enabled turns the closed five-key refs enumeration
    into six keys — label = routing label, null when never classified; the
    full tier carries it too."""
    cls_a = Classification(label="faq", labels=("faq", "chat"), source="llm",
                           detail={})
    cfg = make_cfg(tmp_path, classify=classify_cfg(assignment="multi"))
    classified = make_item(status="dropped_lowq", annotated=False,
                           record=make_record("1" * 16, 1), classification=cls_a)
    unclassified = make_item(status="dropped_dup", annotated=False,
                             record=make_record("2" * 16, 2))
    run_emitter(cfg, [classified, unclassified])

    rows = {r["_meta"]["id"]: r["_meta"]
            for r in read_jsonl(tmp_path / "out" / "res.rejects.jsonl")}
    for meta in rows.values():
        assert list(meta) == ["id", "source", "stage", "reason", "errors", "label"]
    assert rows["1" * 16]["label"] == "faq"
    assert rows["2" * 16]["label"] is None         # dropped before classify

    # full tier: label present alongside the record payload
    out2 = tmp_path / "full" / "res.jsonl"
    out2.parent.mkdir(parents=True)
    cfg_full = make_cfg(tmp_path, output=str(out2), rejects="full",
                        classify=classify_cfg())
    item = make_item(status="dropped_verify", record=make_record("3" * 16, 3),
                     classification=Classification(label="chat", labels=("chat",),
                                                   source="fallback", detail={}))
    run_emitter(cfg_full, [item])
    row = read_jsonl(tmp_path / "full" / "res.rejects.jsonl")[0]
    assert row["_meta"]["label"] == "chat"
    assert "record" in row


# ── v1.8 stream: absorbed route / dropped_noise attribution / _meta.stream ──

def test_absorbed_third_route_neither_channel_but_counted(tmp_path):
    """§7.10 v1.8 third route: absorbed goes to NEITHER the main output NOR
    rejects — counted only (the generic per-status tally feeds M10's post-emit
    accounting)."""
    cfg = make_cfg(tmp_path, segment=SegmentConfig(enabled=True))
    absorbed = [make_item(status="absorbed", record=make_record(f"{i:016x}", i),
                          annotated=False) for i in (1, 2, 3)]
    active = make_item(record=make_record("a" * 16, 9))
    em, result = run_emitter(cfg, absorbed + [active])
    assert result == EmitResult(emitted=1, rejected=0)
    assert len(read_jsonl(tmp_path / "out" / "res.jsonl")) == 1
    rejects = tmp_path / "out" / "res.rejects.jsonl"
    assert not rejects.exists() or read_jsonl(rejects) == []
    assert em._status_totals["absorbed"] == 3          # counted, not routed


def test_dropped_noise_rejects_attribution_three_forms(tmp_path):
    """§9.2 v1.8: dropped_noise rows read the flipping stage's duck-typed
    noise_attribution mark — exactly three (stage, reason) combinations; these
    frames write no item.errors, so `errors` stays []."""
    cfg = make_cfg(tmp_path, segment=SegmentConfig(enabled=True))
    noise = make_item(status="dropped_noise", record=make_record("1" * 16, 1),
                      annotated=False)
    noise.noise_attribution = ("segment", "noise")
    short = make_item(status="dropped_noise", record=make_record("2" * 16, 2),
                      annotated=False)
    short.noise_attribution = ("segment", "below_min_len")
    shrunk = make_item(status="dropped_noise", record=make_record("3" * 16, 3),
                       annotated=False)
    shrunk.noise_attribution = ("verify", "off_task_member")
    unmarked = make_item(status="dropped_noise", record=make_record("4" * 16, 4),
                         annotated=False)                # mark-less fallback
    _, result = run_emitter(cfg, [noise, short, shrunk, unmarked])
    assert result == EmitResult(emitted=0, rejected=4)

    rows = {r["_meta"]["id"]: r["_meta"]
            for r in read_jsonl(tmp_path / "out" / "res.rejects.jsonl")}
    assert (rows["1" * 16]["stage"], rows["1" * 16]["reason"]) == ("segment", "noise")
    assert (rows["2" * 16]["stage"], rows["2" * 16]["reason"]) == ("segment", "below_min_len")
    assert (rows["3" * 16]["stage"], rows["3" * 16]["reason"]) == ("verify", "off_task_member")
    assert (rows["4" * 16]["stage"], rows["4" * 16]["reason"]) == ("segment", "noise")
    for meta in rows.values():
        assert meta["errors"] == []


def test_meta_stream_episode_full_structure_text(tmp_path):
    """§9.1 v1.8 `_meta.stream` episode shape (text modality): order_span in
    "file:line_no" presentation, per-member sources carrying line_no, default
    marks false/null, steps null while extract is off."""
    cfg = make_cfg(tmp_path, segment=SegmentConfig(enabled=True))
    members = [make_record("1" * 16, 3), make_record("2" * 16, 5),
               make_record("3" * 16, 8)]
    item = make_item(record=make_seq_record(members))
    item.session_id = "ime-log/0"
    run_emitter(cfg, [item])

    meta = read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]
    # key position: after source, before scores (chain-order mirror)
    keys = list(meta)
    assert keys.index("stream") == keys.index("source") + 1
    assert keys.index("scores") == keys.index("stream") + 1
    stream = meta["stream"]
    # v1.12 锚：帧粒度全关 ⇒ 无 members 键，键序与 v1.11 字节等价
    assert list(stream) == ["episode_id", "session_id", "order_span",
                            "member_count", "member_ids", "member_sources",
                            "session_split", "repaired", "degraded", "steps"]
    assert stream == {
        "episode_id": "e" * 16,
        "session_id": "ime-log/0",
        "order_span": ["ime-2026-06.jsonl:3", "ime-2026-06.jsonl:8"],
        "member_count": 3,
        "member_ids": ["1" * 16, "2" * 16, "3" * 16],
        "member_sources": [{"file": "ime-2026-06.jsonl", "line_no": 3},
                           {"file": "ime-2026-06.jsonl", "line_no": 5},
                           {"file": "ime-2026-06.jsonl", "line_no": 8}],
        "session_split": False,
        "repaired": False,
        "degraded": None,
        "steps": None,
    }


def test_meta_stream_ui_order_span_marks_and_steps(tmp_path):
    """UI episode: order_span = pair_index values, member_sources carry
    pair_index (exactly one of line_no/pair_index per entry); the duck-typed
    session_split / stream_repaired / segment_degraded marks and the rendered
    transitions all surface."""
    cfg = make_cfg(tmp_path, segment=SegmentConfig(enabled=True), modality="ui",
                   quality_rubric="default:ui")
    members = [make_ui_record("1" * 16, pair_index=2, source_file="a/uitree_2.jsonl"),
               make_ui_record("2" * 16, pair_index=5, source_file="b/uitree_5.jsonl")]
    item = make_item(record=make_seq_record(members, rec_id="f" * 16))
    item.session_id = "capture/0"
    item.session_split = True
    item.stream_repaired = True
    item.segment_degraded = {"kind": "segmentation_invalid", "windows_failed": 1}
    item.transitions = (
        Transition(index=0,
                   action={"action_type": "click", "target": "登录",
                           "value": None, "description": "点击登录按钮"},
                   model="glm-5.2", attempts=1, detail={}),
    )
    run_emitter(cfg, [item])

    stream = read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]["stream"]
    assert stream["episode_id"] == "f" * 16
    assert stream["session_id"] == "capture/0"
    assert stream["order_span"] == [2, 5]
    assert stream["member_sources"] == [{"file": "a/uitree_2.jsonl", "pair_index": 2},
                                        {"file": "b/uitree_5.jsonl", "pair_index": 5}]
    for entry in stream["member_sources"]:          # exactly one of the two keys
        assert set(entry) & {"line_no", "pair_index"} == {"pair_index"}
    assert stream["session_split"] is True
    assert stream["repaired"] is True
    assert stream["degraded"] == {"kind": "segmentation_invalid", "windows_failed": 1}
    assert stream["steps"] == [{"index": 0, "action_type": "click", "target": "登录",
                                "value": None, "description": "点击登录按钮"}]


def test_meta_stream_null_for_single_record_even_in_stream_mode(tmp_path):
    """Frame records never reach the main output under stream — a single record
    getting there yields the defensive null, never a broken episode block."""
    cfg = make_cfg(tmp_path, segment=SegmentConfig(enabled=True))
    run_emitter(cfg, [make_item()])
    assert read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]["stream"] is None


# ── v1.9 stitch: fourth route + _meta.stream thread keys (T21/T16/m-11) ─────

def test_stitched_fourth_route_neither_channel_but_counted(tmp_path):
    """§7.10 v1.9 fourth route (T21): a stitched shell goes to NEITHER the main
    output NOR rejects — counted only; it must never hit the else→rejects
    fallback (which would pollute rejects and trip --strict)."""
    cfg = make_cfg(tmp_path, segment=SegmentConfig(enabled=True),
                   stitch=StitchConfig(enabled=True))
    members = [make_record("1" * 16, 1), make_record("2" * 16, 4)]
    shell = make_item(status="stitched", record=make_seq_record(members),
                      annotated=False)
    active = make_item(record=make_record("a" * 16, 9))
    em, result = run_emitter(cfg, [shell, active])
    assert result == EmitResult(emitted=1, rejected=0)
    assert len(read_jsonl(tmp_path / "out" / "res.jsonl")) == 1
    rejects = tmp_path / "out" / "res.rejects.jsonl"
    assert not rejects.exists() or read_jsonl(rejects) == []
    assert em._status_totals["stitched"] == 1          # counted, not routed


def test_meta_stream_stitch_keys_present_only_when_enabled(tmp_path):
    """T16/m-11: thread_id (after episode_id) / fragments (before steps) / the
    per-step resumed flag appear ONLY when stitch is enabled; the resumed flag
    derives from detail.kind == "thread_seam", never from action_type."""
    cfg = make_cfg(tmp_path, segment=SegmentConfig(enabled=True),
                   stitch=StitchConfig(enabled=True))
    members = [make_record("1" * 16, 3), make_record("2" * 16, 5),
               make_record("3" * 16, 8)]
    item = make_item(record=make_seq_record(members))
    item.session_id = "ime-log/0"
    item.thread_id = "e" * 16
    item.stitch_fragments = (
        {"order_span": ["ime-2026-06.jsonl:3", "ime-2026-06.jsonl:5"],
         "member_count": 2, "cause": "origin", "source_episode": "e" * 16},
        {"order_span": ["ime-2026-06.jsonl:8", "ime-2026-06.jsonl:8"],
         "member_count": 1, "cause": "rescued", "source_episode": None},
    )
    item.transitions = (
        Transition(index=0, action={"action_type": "click", "target": "登录",
                                    "value": None, "description": "点击"},
                   model="glm-5.2", attempts=1, detail={}),
        Transition(index=1, action={"action_type": "app_switch", "target": None,
                                    "value": None,
                                    "description": "线索接缝：被打车打断后恢复"},
                   model="", attempts=0,
                   detail={"kind": "thread_seam", "interrupted_by": ["打车"]}),
    )
    run_emitter(cfg, [item])

    stream = read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]["stream"]
    # v1.12 锚：帧粒度全关 ⇒ 无 members 键（v1.9 stitch 键序不变）
    assert list(stream) == ["episode_id", "thread_id", "session_id",
                            "order_span", "member_count", "member_ids",
                            "member_sources", "session_split", "repaired",
                            "degraded", "fragments", "steps"]
    assert stream["thread_id"] == "e" * 16             # == episode_id (T22)
    assert stream["fragments"] == [dict(f) for f in item.stitch_fragments]
    # top-level order_span stays the envelope span (§6.3 包络 rule)
    assert stream["order_span"] == ["ime-2026-06.jsonl:3", "ime-2026-06.jsonl:8"]
    assert [row["resumed"] for row in stream["steps"]] == [False, True]

    # stitch OFF: the v1.8 key set byte-identical — none of the three appear
    out2 = tmp_path / "off" / "res.jsonl"
    out2.parent.mkdir(parents=True)
    cfg_off = make_cfg(tmp_path, output=str(out2),
                       segment=SegmentConfig(enabled=True))
    item_off = make_item(record=make_seq_record(members))
    item_off.transitions = item.transitions
    run_emitter(cfg_off, [item_off])
    stream_off = read_jsonl(out2)[0]["_meta"]["stream"]
    assert list(stream_off) == ["episode_id", "session_id", "order_span",
                                "member_count", "member_ids", "member_sources",
                                "session_split", "repaired", "degraded", "steps"]
    assert all("resumed" not in row for row in stream_off["steps"])


# ── v1.12 帧粒度：members 块 / status 三值 / 写前校验兜底 / discarded ─────────

def test_meta_stream_members_block_shape_and_frozen_position(tmp_path):
    """v1.12（spec §3.6）：members 位于 member_sources 之后、session_split 之前
    （位置冻结）；条目字段序冻结为 index, id, label, annotation, status；逐成员
    按 rec.members 序，index 0 基；label 缺键 ⇒ null，annotation 走三值判定。"""
    cfg = make_cfg(tmp_path, segment=SegmentConfig(enabled=True),
                   frame_classify=frame_classify_cfg(),
                   frame_annotate=frame_annotate_cfg(),
                   frame_schema=FRAME_SCHEMA)
    members = [make_record("1" * 16, 3), make_record("2" * 16, 5),
               make_record("3" * 16, 8)]
    item = make_item(record=make_seq_record(members))
    item.session_id = "ime-log/0"
    item.member_classifications = {
        "1" * 16: Classification(label="task_request", labels=("task_request",),
                                 source="llm", detail={}),
        "3" * 16: Classification(label="other", labels=("other",),
                                 source="fallback", detail={}),
    }
    item.member_annotations = {
        "1" * 16: Annotation(output={"intent": "book_train", "entities": ["上海"]},
                             model="glm-5.2", attempts=1, usage=Usage()),
        "3" * 16: None,                        # failed 占键为 None（spec §3.3）
    }
    run_emitter(cfg, [item])

    stream = read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]["stream"]
    assert list(stream) == ["episode_id", "session_id", "order_span",
                            "member_count", "member_ids", "member_sources",
                            "members",
                            "session_split", "repaired", "degraded", "steps"]
    assert stream["members"] == [
        {"index": 0, "id": "1" * 16, "label": "task_request",
         "annotation": {"intent": "book_train", "entities": ["上海"]},
         "status": "annotated"},
        {"index": 1, "id": "2" * 16, "label": None,
         "annotation": None, "status": "skipped"},
        {"index": 2, "id": "3" * 16, "label": "other",
         "annotation": None, "status": "failed"},
    ]
    for row in stream["members"]:              # 条目字段序冻结
        assert list(row) == ["index", "id", "label", "annotation", "status"]


def test_members_key_presence_rules_per_switch(tmp_path):
    """v1.12（spec §3.6 在场规则）：label 键仅 frame.classify 开启时在场（dict
    None ⇒ null）；annotation/status 两键仅 frame.annotate 开启时在场；members
    数组本身任一开关开启即在场。"""
    members = [make_record("1" * 16, 3), make_record("2" * 16, 5)]

    # 只开 frame.classify：条目只有 index, id, label
    cfg_c = make_cfg(tmp_path, segment=SegmentConfig(enabled=True),
                     frame_classify=frame_classify_cfg())
    item = make_item(record=make_seq_record(members))
    item.member_classifications = None         # dict None ⇒ 全员 label null
    run_emitter(cfg_c, [item])
    rows = read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]["stream"]["members"]
    assert rows == [{"index": 0, "id": "1" * 16, "label": None},
                    {"index": 1, "id": "2" * 16, "label": None}]
    assert all(list(r) == ["index", "id", "label"] for r in rows)

    # 只开 frame.annotate：条目只有 index, id, annotation, status
    out2 = tmp_path / "ann" / "res.jsonl"
    out2.parent.mkdir(parents=True)
    cfg_a = make_cfg(tmp_path, output=str(out2),
                     segment=SegmentConfig(enabled=True),
                     frame_annotate=frame_annotate_cfg(),
                     frame_schema=FRAME_SCHEMA)
    item2 = make_item(record=make_seq_record(members))
    item2.member_annotations = {
        "1" * 16: Annotation(output={"intent": "x", "entities": []},
                             model="m", attempts=1, usage=Usage())}
    run_emitter(cfg_a, [item2])
    rows2 = read_jsonl(out2)[0]["_meta"]["stream"]["members"]
    assert rows2 == [
        {"index": 0, "id": "1" * 16,
         "annotation": {"intent": "x", "entities": []}, "status": "annotated"},
        {"index": 1, "id": "2" * 16, "annotation": None, "status": "skipped"},
    ]
    assert all(list(r) == ["index", "id", "annotation", "status"] for r in rows2)


def test_members_position_frozen_with_stitch_keys(tmp_path):
    """v1.12：stitch 键共存时 members 位置不变——仍在 member_sources 之后、
    session_split 之前（thread_id/fragments 的 v1.9 键序照旧）。"""
    cfg = make_cfg(tmp_path, segment=SegmentConfig(enabled=True),
                   stitch=StitchConfig(enabled=True),
                   frame_classify=frame_classify_cfg())
    members = [make_record("1" * 16, 3), make_record("2" * 16, 5)]
    item = make_item(record=make_seq_record(members))
    item.thread_id = "e" * 16
    run_emitter(cfg, [item])
    stream = read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]["stream"]
    assert list(stream) == ["episode_id", "thread_id", "session_id",
                            "order_span", "member_count", "member_ids",
                            "member_sources", "members",
                            "session_split", "repaired", "degraded",
                            "fragments", "steps"]


def test_member_status_matrix_and_prewrite_guard(tmp_path):
    """v1.12（spec §3.6）：status 三值判定矩阵——缺键 ⇒ (null, skipped)；值 None
    ⇒ (null, failed)；合法对象 ⇒ (对象, annotated)；非法对象 ⇒ 写前
    validate_only(schema=帧 Schema) 兜底翻 (null, failed) 且 frame_annotate.failed
    计数，非法帧对象零落盘；成员失败不改信封状态、不写 item.errors。"""
    cfg = make_cfg(tmp_path, segment=SegmentConfig(enabled=True),
                   frame_annotate=frame_annotate_cfg(), frame_schema=FRAME_SCHEMA)
    members = [make_record(str(i) * 16, i) for i in (1, 2, 3, 4)]
    item = make_item(record=make_seq_record(members))
    item.member_annotations = {
        "1" * 16: Annotation(output={"intent": "ok", "entities": ["a"]},
                             model="m", attempts=1, usage=Usage()),
        "2" * 16: None,                                       # M5 侧不可修复
        "3" * 16: Annotation(output={"intent": 42, "entities": "bad"},
                             model="m", attempts=1, usage=Usage()),  # 违反帧 Schema
        # "4"*16 缺键 ⇒ skipped
    }
    metrics = MetricsStub()
    _, result = run_emitter(cfg, [item], metrics=metrics)
    assert result == EmitResult(emitted=1, rejected=0)        # 信封照常发射

    rows = read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]["stream"]["members"]
    by_id = {r["id"]: (r["annotation"], r["status"]) for r in rows}
    assert by_id["1" * 16] == ({"intent": "ok", "entities": ["a"]}, "annotated")
    assert by_id["2" * 16] == (None, "failed")
    assert by_id["3" * 16] == (None, "failed")                # 写前兜底置 null
    assert by_id["4" * 16] == (None, "skipped")
    assert {r["status"] for r in rows} <= {"annotated", "skipped", "failed"}
    # 写前兜底恰计 1 次（值 None 的成员失败由 M5 计，emitter 不重计）
    assert metrics.counters == {"frame_annotate.failed": 1}
    assert item.status == "active" and item.errors == []


def test_frame_annotate_discarded_counts_terminal_envelopes(tmp_path):
    """v1.12 沉没成本记账（spec §3.6）：终态非 active 且携带 member_annotations
    的序列信封按非 None 条目数累计 frame_annotate.discarded（仅计数）；active
    发射行与无帧产物的终态行不计；rejects 行键集零改动。"""
    cfg = make_cfg(tmp_path, segment=SegmentConfig(enabled=True),
                   frame_annotate=frame_annotate_cfg(), frame_schema=FRAME_SCHEMA)
    members = [make_record("a1" * 8, 1), make_record("a2" * 8, 2)]
    ann = Annotation(output={"intent": "x", "entities": []}, model="m",
                     attempts=1, usage=Usage())
    dropped = make_item(status="dropped_verify",
                        record=make_seq_record(members, rec_id="d" * 16))
    dropped.member_annotations = {"a1" * 8: ann, "a2" * 8: None}   # 非 None 1 条
    failed = make_item(status="failed", annotated=False,
                       record=make_seq_record(members, rec_id="f" * 16),
                       errors=[StageError(stage="annotate", kind="internal_error",
                                          message="x", retryable=False)])
    failed.member_annotations = {"a1" * 8: ann, "a2" * 8: ann}     # 非 None 2 条
    active = make_item(record=make_seq_record(members, rec_id="e" * 16))
    active.member_annotations = {"a1" * 8: ann}                    # 发射行不计
    bare = make_item(status="dropped_dup", annotated=False,
                     record=make_seq_record(members, rec_id="b" * 16))  # 无帧产物
    metrics = MetricsStub()
    _, result = run_emitter(cfg, [dropped, failed, active, bare], metrics=metrics)
    assert result == EmitResult(emitted=1, rejected=3)
    assert metrics.counters == {"frame_annotate.discarded": 3}     # 1 + 2

    # rejects 面零改动：refs 行键集维持五键闭集（classify 关）
    rows = read_jsonl(tmp_path / "out" / "res.rejects.jsonl")
    for row in rows:
        assert list(row["_meta"]) == ["id", "source", "stage", "reason", "errors"]


def test_frame_annotate_discarded_skips_clone_envelopes(tmp_path):
    """终审缺陷修复：扇出克隆共享同一 member_annotations dict——沉没成本记账
    只取首标签信封视角（克隆终态不重复计，否则共享产物被计 k 次）。"""
    from labelkit.common.contracts.types import Classification
    cfg = make_cfg(tmp_path, segment=SegmentConfig(enabled=True),
                   frame_annotate=frame_annotate_cfg(), frame_schema=FRAME_SCHEMA)
    members = [make_record("a1" * 8, 1)]
    ann = Annotation(output={"intent": "x", "entities": []}, model="m",
                     attempts=1, usage=Usage())
    shared = {"a1" * 8: ann}
    rec = make_seq_record(members, rec_id="o" * 16)
    orig = make_item(status="dropped_verify", annotated=False, record=rec,
                     errors=[StageError(stage="verify", kind="defect",
                                        message="x", retryable=False)])
    orig.classification = Classification(label="x", labels=("x", "y"),
                                         source="llm", detail={})
    orig.member_annotations = shared
    clone = make_item(status="dropped_verify", annotated=False, record=rec,
                      errors=[StageError(stage="verify", kind="defect",
                                         message="x", retryable=False)])
    clone.classification = Classification(label="y", labels=("x", "y"),
                                          source="llm", detail={})
    clone.member_annotations = shared
    metrics = MetricsStub()
    run_emitter(cfg, [orig, clone], metrics=metrics)
    assert metrics.counters == {"frame_annotate.discarded": 1}   # 共享只计一次


def test_frame_all_off_stream_block_byte_equivalent_and_no_counting(tmp_path):
    """v1.12 全关字节等价：帧开关全关时 `_meta.stream` 无 members 键（键序 =
    v1.11，上方两处既有有序精确断言为锚），且即便信封误携帧产物字段也零计数、
    零渲染（开关门控先于字段读取）。"""
    cfg = make_cfg(tmp_path, segment=SegmentConfig(enabled=True))
    members = [make_record("1" * 16, 3), make_record("2" * 16, 5)]
    item = make_item(record=make_seq_record(members))
    item.member_classifications = {
        "1" * 16: Classification(label="x", labels=("x",), source="llm", detail={})}
    item.member_annotations = {
        "1" * 16: Annotation(output={"intent": "x", "entities": []},
                             model="m", attempts=1, usage=Usage())}
    metrics = MetricsStub()
    run_emitter(cfg, [item], metrics=metrics)
    stream = read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]["stream"]
    assert "members" not in stream
    assert list(stream) == ["episode_id", "session_id", "order_span",
                            "member_count", "member_ids", "member_sources",
                            "session_split", "repaired", "degraded", "steps"]
    assert metrics.counters == {}              # active 行永不计 discarded


def test_meta_verification_defects_stream_only(tmp_path):
    """§9.1 v1.8: in stream mode _meta.verification carries the ALWAYS-PRESENT
    defects key ([] when none); non-stream blocks never carry it — even against
    a stray defects value."""
    defect = {"kind": "off_task_members", "members": ["2" * 16], "position": None,
              "detail": "成员 2 偏离任务"}
    cfg = make_cfg(tmp_path, segment=SegmentConfig(enabled=True))
    clean = make_item(record=make_record("1" * 16, 1), verified=True)
    flagged = make_item(record=make_record("2" * 16, 2))
    flagged.verification = VerificationResult(verdict="pass", rounds=2,
                                              critiques=(), defects=(defect,))
    run_emitter(cfg, [clean, flagged])
    by_id = {r["_meta"]["id"]: r["_meta"]
             for r in read_jsonl(tmp_path / "out" / "res.jsonl")}
    assert by_id["1" * 16]["verification"] == {"verdict": "pass", "rounds": 1,
                                               "defects": []}
    assert by_id["2" * 16]["verification"] == {"verdict": "pass", "rounds": 2,
                                               "defects": [defect]}

    out2 = tmp_path / "nostream" / "res.jsonl"
    out2.parent.mkdir(parents=True)
    cfg_off = make_cfg(tmp_path, output=str(out2))
    item = make_item()
    item.verification = VerificationResult(verdict="pass", rounds=1,
                                           critiques=(), defects=(defect,))
    run_emitter(cfg_off, [item])
    assert read_jsonl(out2)[0]["_meta"]["verification"] == {
        "verdict": "pass", "rounds": 1}


def test_rejects_full_sequence_record_payload(tmp_path):
    """S25 (§9.2): the rejects full tier renders a sequence record as
    {"kind","member_ids","member_sources"}; a segmentation_invalid failure line
    carries no raw_last_output (known accepted gap since v1.7)."""
    cfg = make_cfg(tmp_path, rejects="full", segment=SegmentConfig(enabled=True))
    members = [make_record("1" * 16, 1), make_record("2" * 16, 4)]
    item = make_item(
        status="failed", record=make_seq_record(members), annotated=False,
        errors=[StageError(stage="segment", kind="segmentation_invalid",
                           message="窗口修复耗尽", retryable=False)])
    run_emitter(cfg, [item])
    row = read_jsonl(tmp_path / "out" / "res.rejects.jsonl")[0]
    assert (row["_meta"]["stage"], row["_meta"]["reason"]) == (
        "segment", "segmentation_invalid")
    assert row["record"] == {
        "kind": "sequence",
        "member_ids": ["1" * 16, "2" * 16],
        "member_sources": [{"file": "ime-2026-06.jsonl", "line_no": 1},
                           {"file": "ime-2026-06.jsonl", "line_no": 4}],
    }
    assert "raw_last_output" not in row


def test_sidecar_meta_mode_alignment(tmp_path):
    cfg = make_cfg(tmp_path, meta_mode="sidecar")
    items = [
        make_item(record=make_record("1" * 16, line_no=1)),
        make_item(status="dropped_dup", record=make_record("2" * 16, line_no=2),
                  annotated=False),
        make_item(record=make_record("3" * 16, line_no=3)),
    ]
    run_emitter(cfg, items)
    main_rows = read_jsonl(tmp_path / "out" / "res.jsonl")
    meta_rows = read_jsonl(tmp_path / "out" / "res.meta.jsonl")
    # pure user objects in main; row-aligned metas wrapped as {"_meta": {...}}
    assert len(main_rows) == len(meta_rows) == 2
    for row in main_rows:
        assert "_meta" not in row
        Draft202012Validator(USER_SCHEMA).validate(row)
    assert [list(m) for m in meta_rows] == [["_meta"], ["_meta"]]
    assert [m["_meta"]["id"] for m in meta_rows] == ["1" * 16, "3" * 16]


def test_none_meta_mode(tmp_path):
    cfg = make_cfg(tmp_path, meta_mode="none")
    run_emitter(cfg, [make_item()])
    rows = read_jsonl(tmp_path / "out" / "res.jsonl")
    assert rows == [{"intent": "writing_assist", "topic": "请假条", "difficulty": "easy"}]
    assert not (tmp_path / "out" / "res.meta.jsonl").exists()


def test_generated_record_source_block(tmp_path):
    cfg = make_cfg(tmp_path)
    item = make_item(record=make_record(generated=True))
    run_emitter(cfg, [item])
    src = read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]["source"]
    # generated records emit pair_index: null, never line_no (§12.20)
    assert "line_no" not in src
    assert src["pair_index"] is None
    assert src["generated_from"] == ["b" * 16]
    assert src["generator"] == {"llm": "default", "style": "concise"}


def test_inline_rubric_name_used_for_inline_selector(tmp_path):
    cfg = make_cfg(tmp_path, quality_rubric="inline")
    run_emitter(cfg, [make_item()])
    meta = read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]
    assert meta["run"]["rubric"] == "my_inline_rubric"


def test_annotation_sc_block(tmp_path):
    cfg = make_cfg(tmp_path)
    item = make_item()
    item.annotation = Annotation(output=item.annotation.output, model="glm-5.2",
                                 attempts=4, usage=Usage(), sc={"n": 3,
                                                               "agreement_ratio": 0.67})
    run_emitter(cfg, [item])
    meta = read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]
    assert meta["annotation"] == {"model": "glm-5.2", "attempts": 4,
                                  "sc": {"n": 3, "agreement_ratio": 0.67}}


# ── annotate disabled ──────────────────────────────────────────────────────

def test_annotate_disabled_passthrough_raw(tmp_path):
    cfg = make_cfg(tmp_path, annotate_enabled=False)
    raw = {"instruction": "不符合用户Schema的原始行", "extra_key": 1}
    item = make_item(record=make_record(raw=raw), annotated=False)
    _, result = run_emitter(cfg, [item])
    # raw is emitted as-is, no validate_only gate, annotation null
    assert result.emitted == 1
    row = read_jsonl(tmp_path / "out" / "res.jsonl")[0]
    meta = row.pop("_meta")
    assert row == raw
    assert meta["annotation"] is None


def test_annotate_disabled_ui_payload(tmp_path):
    cfg = make_cfg(tmp_path, annotate_enabled=False, modality="ui",
                   quality_rubric="default:ui")
    item = make_item(record=make_ui_record(), annotated=False)
    run_emitter(cfg, [item])
    row = read_jsonl(tmp_path / "out" / "res.jsonl")[0]
    meta = row.pop("_meta")
    assert row == {"ui_tree": 'Button "登录" [0,0,10,10]', "image_path": "b/image_2.png"}
    assert meta["source"] == {"file": "b/uitree_2.jsonl", "pair_index": 2,
                              "generated_from": [], "fields": {}, "generator": None}
    assert meta["run"]["rubric"] == "default:ui"


# ── rejects channel ────────────────────────────────────────────────────────

def test_rejects_refs_exact_shape_and_reasons(tmp_path):
    cfg = make_cfg(tmp_path, rejects="refs")
    dup = make_item(status="dropped_dup", record=make_record("1" * 16, 1),
                    annotated=False)
    dup.dedup = DedupInfo(kind="near_text", cluster_key="k" * 16, kept_id="9" * 16)
    lowq = make_item(status="dropped_lowq", record=make_record("2" * 16, 2),
                     annotated=False)
    ver = make_item(status="dropped_verify", record=make_record("3" * 16, 3))
    failed = make_item(
        status="failed", record=make_record("4" * 16, 4), annotated=False,
        errors=[StageError(stage="annotate", kind="schema_violation",
                           message="/difficulty: 期望枚举之一", retryable=False)])
    _, result = run_emitter(cfg, [dup, lowq, ver, failed])
    assert result == EmitResult(emitted=0, rejected=4)

    rows = read_jsonl(tmp_path / "out" / "res.rejects.jsonl")
    assert len(rows) == 4
    # refs tier: each line carries exactly {"_meta": {...five keys...}}
    for row in rows:
        assert list(row) == ["_meta"]
        assert list(row["_meta"]) == ["id", "source", "stage", "reason", "errors"]
        assert "fields" not in row["_meta"]["source"]
    by_id = {r["_meta"]["id"]: r["_meta"] for r in rows}
    assert (by_id["1" * 16]["stage"], by_id["1" * 16]["reason"]) == ("dedup", "near_text")
    assert (by_id["2" * 16]["stage"], by_id["2" * 16]["reason"]) == ("quality", "below_threshold")
    assert (by_id["3" * 16]["stage"], by_id["3" * 16]["reason"]) == ("verify", "verify_fail")
    assert (by_id["4" * 16]["stage"], by_id["4" * 16]["reason"]) == ("annotate", "schema_violation")
    assert by_id["4" * 16]["errors"] == ["/difficulty: 期望枚举之一"]
    assert by_id["1" * 16]["errors"] == []  # always present, [] when none


def test_rejects_top_ratio_reason(tmp_path):
    cfg = make_cfg(tmp_path, selection="top_ratio")
    lowq = make_item(status="dropped_lowq", annotated=False)
    run_emitter(cfg, [lowq])
    row = read_jsonl(tmp_path / "out" / "res.rejects.jsonl")[0]
    assert row["_meta"]["reason"] == "top_ratio"


def test_rejects_full_adds_record_and_raw_last_output(tmp_path):
    cfg = make_cfg(tmp_path, rejects="full")
    raw = {"instruction": "第213行", "source": "ime-log"}
    failed = make_item(
        status="failed", record=make_record("5" * 16, 213, raw=raw), annotated=False,
        errors=[StageError(stage="annotate", kind="schema_violation",
                           message="bad", retryable=False)])
    dup = make_item(status="dropped_dup", record=make_record("6" * 16, 7),
                    annotated=False)
    dup.dedup = DedupInfo(kind="exact", cluster_key="k" * 16, kept_id="9" * 16)
    run_emitter(cfg, [failed, dup])
    rows = {r["_meta"]["id"]: r for r in
            read_jsonl(tmp_path / "out" / "res.rejects.jsonl")}
    assert rows["5" * 16]["record"] == raw
    assert "raw_last_output" in rows["5" * 16]  # schema_violation only
    assert rows["6" * 16]["record"] == {"instruction": "帮我写一条请假条",
                                        "source": "ime-log", "ts": "t"}
    assert "raw_last_output" not in rows["6" * 16]


def test_rejects_full_ui_record_payload(tmp_path):
    cfg = make_cfg(tmp_path, rejects="full", modality="ui")
    item = make_item(status="dropped_dup", record=make_ui_record(), annotated=False)
    run_emitter(cfg, [item])
    row = read_jsonl(tmp_path / "out" / "res.rejects.jsonl")[0]
    assert row["record"] == {"ui_tree": 'Button "登录" [0,0,10,10]',
                             "image_path": "b/image_2.png"}


def test_rejects_none_writes_no_file_but_counts(tmp_path):
    cfg = make_cfg(tmp_path, rejects="none")
    _, result = run_emitter(cfg, [make_item(status="dropped_dup", annotated=False),
                                  make_item()])
    assert result == EmitResult(emitted=1, rejected=1)
    assert not (tmp_path / "out" / "res.rejects.jsonl").exists()


def test_rejects_generator_included_when_present(tmp_path):
    cfg = make_cfg(tmp_path)
    item = make_item(status="dropped_dup", record=make_record(generated=True),
                     annotated=False)
    run_emitter(cfg, [item])
    src = read_jsonl(tmp_path / "out" / "res.rejects.jsonl")[0]["_meta"]["source"]
    assert src["generator"] == {"llm": "default", "style": "concise"}
    assert "fields" not in src


# ── final validate_only gate ───────────────────────────────────────────────

def test_validate_only_failure_diverts_to_rejects(tmp_path):
    cfg = make_cfg(tmp_path)
    bad = make_item(output={"intent": "x", "topic": "y", "difficulty": "非常难"})
    good = make_item(record=make_record("b" * 16, 2))
    _, result = run_emitter(cfg, [bad, good])
    assert result == EmitResult(emitted=1, rejected=1)
    assert bad.status == "failed"
    assert bad.errors and bad.errors[0].kind == "internal_error"
    row = read_jsonl(tmp_path / "out" / "res.rejects.jsonl")[0]
    assert row["_meta"]["stage"] == "emitter"
    assert row["_meta"]["reason"] == "internal_error"
    # main output holds only the good record
    assert len(read_jsonl(tmp_path / "out" / "res.jsonl")) == 1


def test_validate_only_rejects_errors_one_element_per_violation(tmp_path):
    """Spec 3.11.3 ②: the rejects `errors` array carries ONE element per violation
    ('<pointer>: <violation>'), never a joined string."""
    cfg = make_cfg(tmp_path)
    # two violations: enum on /difficulty + additionalProperties at root
    bad = make_item(output={"intent": "x", "topic": "y", "difficulty": "非常难",
                            "confidence": 0.9})
    _, result = run_emitter(cfg, [bad])
    assert result == EmitResult(emitted=0, rejected=1)
    errors = read_jsonl(tmp_path / "out" / "res.rejects.jsonl")[0]["_meta"]["errors"]
    assert len(errors) == 2                      # one array element per violation
    assert all(e.startswith("/") for e in errors)
    assert not any("; /" in e for e in errors)   # no semicolon-joined collapse
    # granularity preserved on the item itself (one StageError per violation)
    assert len(bad.errors) == 2
    assert all(e.kind == "internal_error" and e.stage == "emitter"
               for e in bad.errors)


def test_stderr_log_never_carries_data_content(tmp_path, caplog):
    """Spec §7.1 ①: the stderr run log carries operational events only — never data
    values. Violation text (with values) goes only to the rejects channel; the
    stack of an unexpected exception goes to debug level (§7.6)."""
    cfg = make_cfg(tmp_path, rejects="full")

    class LeakyEngine(EngineStub):
        calls = 0

        def validate_only(self, obj, schema=None):
            LeakyEngine.calls += 1
            if LeakyEngine.calls == 1:
                return super().validate_only(obj, schema)
            raise RuntimeError("boom with data: 请假条SECRET")

    bad_enum = make_item(output={"intent": "x", "topic": "y", "difficulty": "非常难"})
    crash = make_item(record=make_record("b" * 16, 2))
    em = Emitter(cfg, LeakyEngine(), run_id="ab12cd34ef56",
                 run_started_at=RUN_STARTED_AT)
    em.open()
    with caplog.at_level(logging.DEBUG, logger=".".join(("labelkit", "emitter"))):
        result = em.emit_batch([bad_enum, crash], 1)
    em.finalize({"counts": {}})
    assert result == EmitResult(emitted=0, rejected=2)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 2
    # ① validate_only failure: data-free summary (record id + violation count)
    assert "final validate_only failed: record " + "a" * 16 in warnings[0].getMessage()
    assert "1 violation(s)" in warnings[0].getMessage()
    # ② generic failure: exception TYPE only, str(exc) never reaches stderr
    assert warnings[1].getMessage() == "internal_error: emitter failure: RuntimeError"
    # nothing at info+ contains data values
    for rec in caplog.records:
        if rec.levelno >= logging.INFO:
            assert "非常难" not in rec.getMessage()
            assert "SECRET" not in rec.getMessage()
    # stack lands at debug level (§7.6)
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG and r.exc_info]
    assert len(debugs) == 1

    # full violation / exception text still reaches the rejects channel
    rows = {r["_meta"]["id"]: r for r in
            read_jsonl(tmp_path / "out" / "res.rejects.jsonl")}
    assert any("非常难" in e for e in rows["a" * 16]["_meta"]["errors"])
    assert rows["b" * 16]["_meta"]["errors"] == [
        "emitter failure: boom with data: 请假条SECRET"]


def test_active_without_annotation_is_internal_error(tmp_path):
    cfg = make_cfg(tmp_path)
    item = make_item(annotated=False)  # active + annotate enabled + no annotation
    _, result = run_emitter(cfg, [item])
    assert result == EmitResult(emitted=0, rejected=1)
    row = read_jsonl(tmp_path / "out" / "res.rejects.jsonl")[0]
    assert row["_meta"]["reason"] == "internal_error"


def test_emit_batch_never_raises_on_broken_item(tmp_path):
    cfg = make_cfg(tmp_path)
    broken = make_item()
    broken.annotation = Annotation(output={"intent": object()},  # not JSON-serializable
                                   model="m", attempts=1, usage=Usage())
    _, result = run_emitter(cfg, [broken, make_item(record=make_record("b" * 16, 2))])
    assert result.emitted == 1
    assert result.rejected == 1


# ── counts invariant across synthetic mixes ────────────────────────────────

@pytest.mark.parametrize("mix", [
    {"active": 5, "dropped_dup": 2, "dropped_lowq": 1, "dropped_verify": 1, "failed": 3},
    {"active": 0, "dropped_dup": 4},
    {"active": 7},
])
def test_counts_invariant(tmp_path, mix):
    cfg = make_cfg(tmp_path)
    batch, i = [], 0
    for status, n in mix.items():
        for _ in range(n):
            i += 1
            rec = make_record(f"{i:016x}", line_no=i)
            errors = ([StageError(stage="annotate", kind="internal_error",
                                  message="x", retryable=False)]
                      if status == "failed" else [])
            batch.append(make_item(status=status, record=rec,
                                   annotated=(status in ("active", "dropped_verify")),
                                   errors=errors))
    _, result = run_emitter(cfg, batch)
    total = sum(mix.values())
    assert result.emitted + result.rejected == total
    assert result.emitted == mix.get("active", 0)
    assert len(read_jsonl(tmp_path / "out" / "res.jsonl")) == result.emitted
    rejects = tmp_path / "out" / "res.rejects.jsonl"
    got_rejects = len(read_jsonl(rejects)) if rejects.exists() else 0
    assert got_rejects == result.rejected


# ── atomic delivery ────────────────────────────────────────────────────────

def test_atomic_part_naming_and_rename(tmp_path):
    cfg = make_cfg(tmp_path, meta_mode="sidecar")
    out = tmp_path / "out" / "res.jsonl"
    em, _ = run_emitter(cfg, [make_item()], finalize=False)
    # simulated crash point between batches: only .part files exist
    assert (tmp_path / "out" / "res.jsonl.part").exists()
    assert (tmp_path / "out" / "res.meta.jsonl.part").exists()
    assert not out.exists()
    assert not (tmp_path / "out" / "res.meta.jsonl").exists()
    # flushed prefix already valid JSONL
    assert len(read_jsonl(tmp_path / "out" / "res.jsonl.part")) == 1

    em.finalize({"counts": {}}, deliver=True)
    assert out.exists()
    assert (tmp_path / "out" / "res.meta.jsonl").exists()
    assert not (tmp_path / "out" / "res.jsonl.part").exists()
    assert not (tmp_path / "out" / "res.meta.jsonl.part").exists()


def test_finalize_deliver_false_leaves_part_writes_report(tmp_path):
    cfg = make_cfg(tmp_path)
    em, _ = run_emitter(cfg, [make_item()], finalize=False)
    em.finalize({"counts": {"emitted": 1}}, deliver=False)
    assert (tmp_path / "out" / "res.jsonl.part").exists()
    assert not (tmp_path / "out" / "res.jsonl").exists()
    report = json.loads((tmp_path / "out" / "res.report.json").read_text("utf-8"))
    assert report == {"counts": {"emitted": 1}}


def test_open_unwritable_output_raises_labelkit_error(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    cfg = make_cfg(tmp_path, output=str(blocker / "res.jsonl"))
    em = Emitter(cfg, EngineStub(), "ab12cd34ef56", RUN_STARTED_AT)
    with pytest.raises(LabelKitError):
        em.open()


# ── channel write failures are run-level (spec 3.11.3 ④, §9.4) ─────────────

class ExplodingWriter:
    """File-handle proxy: write() raises OSError from the Nth call on."""

    def __init__(self, fh, fail_from=1):
        self._fh = fh
        self._writes = 0
        self._fail_from = fail_from

    def write(self, s):
        self._writes += 1
        if self._writes >= self._fail_from:
            raise OSError(28, "No space left on device")
        return self._fh.write(s)

    def __getattr__(self, name):
        return getattr(self._fh, name)


def test_main_write_oserror_propagates_and_blocks_delivery(tmp_path):
    """A mid-write I/O failure on the main channel is NOT a record-level reject:
    emit_batch raises LabelKitError (exit 4) and finalize can never rename the
    possibly-corrupted .part into the final name."""
    cfg = make_cfg(tmp_path)
    em = Emitter(cfg, EngineStub(), "ab12cd34ef56", RUN_STARTED_AT)
    em.open()
    em._main_fh = ExplodingWriter(em._main_fh, fail_from=2)
    batch = [make_item(record=make_record("1" * 16, 1)),
             make_item(record=make_record("2" * 16, 2))]
    with pytest.raises(LabelKitError, match="write failed"):
        em.emit_batch(batch, 1)
    # the failing item was NOT double-represented as a reject
    rejects = tmp_path / "out" / "res.rejects.jsonl"
    assert not rejects.exists() or read_jsonl(rejects) == []
    # even an explicit deliver=True finalize must leave .part in place
    em.finalize({"counts": {}}, deliver=True)
    assert (tmp_path / "out" / "res.jsonl.part").exists()
    assert not (tmp_path / "out" / "res.jsonl").exists()
    # report.json is still written
    assert (tmp_path / "out" / "res.report.json").exists()


def test_sidecar_write_oserror_propagates(tmp_path):
    cfg = make_cfg(tmp_path, meta_mode="sidecar")
    em = Emitter(cfg, EngineStub(), "ab12cd34ef56", RUN_STARTED_AT)
    em.open()
    em._sidecar_fh = ExplodingWriter(em._sidecar_fh)
    with pytest.raises(LabelKitError, match="sidecar"):
        em.emit_batch([make_item()], 1)
    em.finalize({"counts": {}}, deliver=True)
    assert (tmp_path / "out" / "res.meta.jsonl.part").exists()
    assert not (tmp_path / "out" / "res.meta.jsonl").exists()


def test_rejects_write_oserror_propagates(tmp_path):
    cfg = make_cfg(tmp_path)
    em = Emitter(cfg, EngineStub(), "ab12cd34ef56", RUN_STARTED_AT)
    em.open()
    em._rejects_fh = ExplodingWriter(em._rejects_fh)
    with pytest.raises(LabelKitError, match="rejects"):
        em.emit_batch([make_item(status="dropped_dup", annotated=False)], 1)


def test_sidecar_serialization_failure_keeps_line_alignment(tmp_path):
    """Both lines of a sidecar pair are serialized before either is written:
    a record whose _meta cannot serialize must not leave an orphan main line
    (spec 3.11.3 ①: 主输出第 k 行 ↔ meta 第 k 行)."""
    cfg = make_cfg(tmp_path, meta_mode="sidecar")
    # unserializable _meta (generator payload) while the user object is fine
    bad_rec = Record(
        id="1" * 16, modality="text", text="t", raw={"instruction": "t"},
        ui_tree=None, image=None,
        ref=RecordRef(source_file="", line_no=None, pair_index=None,
                      generated_from=(), generator={"llm": object()}))
    bad = make_item(record=bad_rec)
    good = make_item(record=make_record("2" * 16, 2))
    _, result = run_emitter(cfg, [bad, good])
    assert result == EmitResult(emitted=1, rejected=1)
    main_rows = read_jsonl(tmp_path / "out" / "res.jsonl")
    meta_rows = read_jsonl(tmp_path / "out" / "res.meta.jsonl")
    assert len(main_rows) == len(meta_rows) == 1        # no orphan main line
    assert meta_rows[0]["_meta"]["id"] == "2" * 16      # alignment intact
    assert bad.status == "failed"


# ── report.json ────────────────────────────────────────────────────────────

FULL_REPORT = {
    "run": {"tool_version": "1.0.0", "started_at": "2026-07-02T10:27:41+00:00",
            "finished_at": "2026-07-02T10:41:23+00:00", "interrupted": False,
            "exit_code": 0, "modality": "text", "seed": 7,
            "config_digest": "sha256:c", "project_digest": "sha256:p"},
    "counts": {"scanned": 10, "ingested": 10, "bad_input": 0, "dropped_dup": 2,
               "dropped_lowq": 1, "dropped_verify": 0, "failed": 1, "generated": 0,
               "emitted": 6},
    "dedup": {"exact": 1, "near_text": 1, "near_image": 0, "near_both": 0,
              "clusters": 2, "image_decode_failures": 0},
    "quality": {"mode": "pairwise_bt", "rounds": 4, "judgment_failures": 0,
                "aggregate_histogram": {f"0.{i}-{'1.0' if i == 9 else f'0.{i+1}'}": 0
                                        for i in range(10)},
                "per_criterion_mean": {"clarity": 0.5}},
    "schema_engine": {"resolved_at": {"l0_or_clean": 6, "l1": 0, "l3_1": 1,
                                      "l3_2": 0, "rejected": 1}},
    "trace": {"enabled": False, "path": "", "events": 0, "dropped_events": 0},
    "llm_usage": {"default": {"calls": 12, "prompt_tokens": 100,
                              "completion_tokens": 50, "est_cost_usd": 0.01,
                              "retries": 0}},
    "timing": {"wall_s": 3.5, "per_stage_s": {"dedup": 0.1, "quality": 2.0,
                                              "annotate": 1.0}},
}


def test_report_written_verbatim_and_complete(tmp_path):
    cfg = make_cfg(tmp_path)
    run_emitter(cfg, [make_item()], report=FULL_REPORT)
    written = json.loads((tmp_path / "out" / "res.report.json").read_text("utf-8"))
    assert written == FULL_REPORT
    # contract §9.3 top-level blocks all present
    assert set(written) >= {"run", "counts", "dedup", "quality", "schema_engine",
                            "trace", "llm_usage", "timing"}
    c = written["counts"]
    assert (c["emitted"] + c["dropped_dup"] + c["dropped_lowq"] + c["dropped_verify"]
            + c["failed"] + c["bad_input"]) == c["scanned"] + c["generated"]
    assert list(written["quality"]["aggregate_histogram"])[0] == "0.0-0.1"
    assert list(written["quality"]["aggregate_histogram"])[-1] == "0.9-1.0"


def test_finalize_report_write_failure_raises_after_delivery(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    em, _ = run_emitter(cfg, [make_item()], finalize=False)
    (tmp_path / "out" / "res.report.json").mkdir()  # force IsADirectoryError
    with pytest.raises(LabelKitError, match="report write failed"):
        em.finalize({"counts": {}}, deliver=True)
    # delivery still happened before the report failure
    assert (tmp_path / "out" / "res.jsonl").exists()


# ── finalize stderr run-tail line (spec 3.11.3 ③) ──────────────────────────

def test_finalize_logs_rejects_line_count_and_report_path(tmp_path, caplog):
    cfg = make_cfg(tmp_path, rejects="refs")
    batch = [make_item(),
             make_item(status="dropped_dup", record=make_record("2" * 16, 2),
                       annotated=False),
             make_item(status="failed", record=make_record("3" * 16, 3),
                       annotated=False,
                       errors=[StageError(stage="annotate", kind="internal_error",
                                          message="x", retryable=False)])]
    with caplog.at_level(logging.INFO, logger=".".join(("labelkit", "emitter"))):
        run_emitter(cfg, batch)
    msgs = [r.getMessage() for r in caplog.records]
    expect = (f"wrote {tmp_path / 'out' / 'res.rejects.jsonl'} (2 lines) "
              f"and {tmp_path / 'out' / 'res.report.json'}")
    assert expect in msgs
    # ordering: batch flush line → finalize rename line → wrote line
    assert msgs.index(expect) > msgs.index(
        next(m for m in msgs if m.startswith("finalize: fsync + rename")))


def test_finalize_logs_report_only_when_rejects_none(tmp_path, caplog):
    cfg = make_cfg(tmp_path, rejects="none")
    # a rejected item that is COUNTED but never written (rejects='none'):
    # the "wrote" line must not claim a rejects file
    batch = [make_item(), make_item(status="dropped_dup",
                                    record=make_record("2" * 16, 2),
                                    annotated=False)]
    with caplog.at_level(logging.INFO, logger=".".join(("labelkit", "emitter"))):
        run_emitter(cfg, batch)
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("wrote ")]
    assert lines == [f"wrote {tmp_path / 'out' / 'res.report.json'}"]


# ── TTY progress line (spec §7.7) ───────────────────────────────────────────

class FakeTTY:
    def __init__(self):
        self.text = ""

    def isatty(self):
        return True

    def write(self, s):
        self.text += s

    def flush(self):
        pass


def test_progress_line_shows_batch_no_and_status_counts(tmp_path, monkeypatch):
    import sys as _sys
    cfg = make_cfg(tmp_path)
    em = Emitter(cfg, EngineStub(), "ab12cd34ef56", RUN_STARTED_AT)
    em.open()
    fake = FakeTTY()
    monkeypatch.setattr(_sys, "stderr", fake)
    em.emit_batch([make_item(),
                   make_item(status="dropped_dup", record=make_record("2" * 16, 2),
                             annotated=False),
                   make_item(status="dropped_lowq", record=make_record("3" * 16, 3),
                             annotated=False),
                   make_item(status="failed", record=make_record("4" * 16, 4),
                             annotated=False,
                             errors=[StageError(stage="annotate", kind="internal_error",
                                                message="x", retryable=False)])], 1)
    em.emit_batch([make_item(record=make_record("5" * 16, 5))], 2)
    line = fake.text.rsplit("\r", 1)[-1]
    assert "batch 2" in line
    assert "emitted=2" in line
    assert "dropped_dup=1" in line
    assert "dropped_lowq=1" in line
    assert "dropped_verify=0" in line
    assert "failed=1" in line
    monkeypatch.undo()
    em.finalize({"counts": {}})


def test_progress_suppressed_for_jsonl_log_format(tmp_path, monkeypatch):
    import sys as _sys
    cfg = make_cfg(tmp_path, log_format="jsonl")
    em = Emitter(cfg, EngineStub(), "ab12cd34ef56", RUN_STARTED_AT)
    em.open()
    fake = FakeTTY()
    monkeypatch.setattr(_sys, "stderr", fake)
    em.emit_batch([make_item()], 1)
    assert "\r" not in fake.text
    monkeypatch.undo()
    em.finalize({"counts": {}})


# ── v1.10: rich static gate + console_format parity (U21, spec 3.11.2) ──────


def test_rich_mode_resolved_suppresses_progress_and_summary(tmp_path, monkeypatch):
    """mode_resolved="rich": _progress and _print_summary return before any
    stderr write (the CLI panel supersedes both faces) — while the three file
    channels stay fully live (the gate is display-only)."""
    import sys as _sys
    # Byte-exact stderr assertion: detach any ambient 'labelkit' logging handler
    # (earlier e2e tests leave one bound to a closed capture stream, whose
    # logging-error report would land in the monkeypatched stderr).
    monkeypatch.setattr(logging.getLogger("labelkit"), "handlers", [])
    cfg = make_cfg(tmp_path, console=ConsoleConfig(mode_resolved="rich"))
    em = Emitter(cfg, EngineStub(), "ab12cd34ef56", RUN_STARTED_AT)
    em.open()
    fake = FakeTTY()                               # isatty()=True: only the rich
    monkeypatch.setattr(_sys, "stderr", fake)      # gate can be doing the muting
    em.emit_batch([make_item()], 1)
    em.finalize({"counts": {"scanned": 1, "emitted": 1}})
    monkeypatch.undo()
    assert fake.text == ""                         # no \r progress, no summary
    rows = read_jsonl(tmp_path / "out" / "res.jsonl")
    assert len(rows) == 1                          # main output unaffected


def test_plain_progress_line_equals_console_format_golden(tmp_path, monkeypatch):
    """Plain path byte-parity (U24 ①): the emitter's progress write IS the
    console_format pure-function string for its running totals."""
    import sys as _sys
    from labelkit.common.observability.console_format import format_progress_line

    monkeypatch.setattr(logging.getLogger("labelkit"), "handlers", [])
    cfg = make_cfg(tmp_path)                       # mode_resolved default "plain"
    em = Emitter(cfg, EngineStub(), "ab12cd34ef56", RUN_STARTED_AT)
    em.open()
    fake = FakeTTY()
    monkeypatch.setattr(_sys, "stderr", fake)
    em.emit_batch([make_item(),
                   make_item(status="dropped_dup", record=make_record("2" * 16, 2),
                             annotated=False)], 1)
    em.emit_batch([make_item(record=make_record("5" * 16, 5))], 2)
    monkeypatch.undo()
    first, second = fake.text.split("\r")[1:]      # two \r-prefixed writes
    assert "\r" + first == format_progress_line(
        1, 1, {"dropped_dup": 1, "active": 1})
    assert "\r" + second == format_progress_line(
        2, 2, {"dropped_dup": 1, "active": 2})
    em.finalize({"counts": {}})


def test_plain_summary_equals_console_format_golden(tmp_path, monkeypatch):
    """Plain path byte-parity (U24 ①): _print_summary writes exactly the
    console_format summary lines newline-joined with a trailing newline."""
    import sys as _sys
    from labelkit.common.observability.console_format import format_summary_lines

    monkeypatch.setattr(logging.getLogger("labelkit"), "handlers", [])
    counts = {"scanned": 60, "ingested": 58, "bad_input": 2, "generated": 12,
              "dropped_dup": 5, "dropped_lowq": 6, "dropped_verify": 1,
              "failed": 0, "emitted": 41}
    cfg = make_cfg(tmp_path)

    class PlainStderr(FakeTTY):
        def isatty(self):                          # non-TTY: no progress writes,
            return False                           # summary is unconditional

    em = Emitter(cfg, EngineStub(), "ab12cd34ef56", RUN_STARTED_AT)
    em.open()
    em.emit_batch([make_item()], 1)
    fake = PlainStderr()
    monkeypatch.setattr(_sys, "stderr", fake)
    em.finalize({"counts": counts})
    monkeypatch.undo()
    assert fake.text == "\n".join(format_summary_lines(counts)) + "\n"
    assert fake.text == (
        "   ── final summary (matches report.counts item by item) ──\n"
        "   scanned=60  ingested=58  bad_input=2  generated=12\n"
        "   dropped_dup=5  dropped_lowq=6  dropped_verify=1  failed=0  emitted=41\n"
    )


# ── passthrough fields ─────────────────────────────────────────────────────

def test_passthrough_fields_subset_and_missing(tmp_path):
    cfg = make_cfg(tmp_path, passthrough_fields=("source", "absent_key"))
    run_emitter(cfg, [make_item()])
    meta = read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]
    assert meta["source"]["fields"] == {"source": "ime-log"}  # missing keys skipped


def test_passthrough_empty_gives_empty_object(tmp_path):
    cfg = make_cfg(tmp_path)
    run_emitter(cfg, [make_item()])
    meta = read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]
    assert meta["source"]["fields"] == {}


def test_dry_run_report_path_is_diverted(tmp_path):
    # P2-4: a rehearsal writes <stem>.dryrun.report.json, never the real ledger.
    cfg = make_cfg(tmp_path, dry_run=True)
    em = Emitter(cfg, engine=None, run_id="a" * 12,
                 run_started_at=datetime.now().astimezone())
    assert str(em._report_path).endswith(".dryrun.report.json")
    em_real = Emitter(make_cfg(tmp_path), engine=None, run_id="a" * 12,
                      run_started_at=datetime.now().astimezone())
    assert str(em_real._report_path).endswith("res.report.json")


# ── v1.17（SPEC-SP §5.1）：通道路径只消费 ResolvedPaths，消灭 cwd 二次推导 ─────


def test_channel_paths_are_cwd_invariant_via_resolved_paths(tmp_path,
                                                            monkeypatch):
    """run.output 是相对诱饵时，普通四通道路径仍逐一等于 M1 派生值，
    且与构造时 cwd 无关（旧实现按 cwd 重解相对 output，会随目录漂移）。"""
    cfg = make_cfg(tmp_path, meta_mode="sidecar")
    cfg = _dc_replace(
        cfg, run=_dc_replace(cfg.run, output="out/res.jsonl"))  # 相对诱饵
    seen = []
    for cwd in (tmp_path / "cwd-a", tmp_path / "cwd-b"):
        cwd.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(cwd)
        em = Emitter(cfg, engine=None, run_id="a" * 12,
                     run_started_at=datetime.now().astimezone())
        seen.append((em._output_path, em._report_path, em._rejects_path,
                     em._sidecar_path))
    assert seen[0] == seen[1]
    assert str(seen[0][0]) == cfg.paths.output
    assert str(seen[0][1]) == cfg.paths.report
    assert str(seen[0][2]) == cfg.paths.rejects
    assert str(seen[0][3]) == cfg.paths.sidecar


def test_report_path_takes_m1_verdict_not_command_mode(tmp_path):
    """report 命名只认 paths.report（live/dry-run 后缀已由 M1 一次裁决）——
    emitter 不再按 cfg.dry_run 追加后缀：命令位与 M1 派生不一致时以 paths 为准。"""
    cfg = make_cfg(tmp_path)                    # dry_run=False 的 live 命令位
    dry_report = (str(Path(cfg.paths.output).with_suffix(""))
                  + ".dryrun.report.json")
    cfg = _dc_replace(cfg, paths=_dc_replace(cfg.paths, report=dry_report))
    em = Emitter(cfg, engine=None, run_id="a" * 12,
                 run_started_at=datetime.now().astimezone())
    assert str(em._report_path) == dry_report


def test_missing_paths_fails_fast_without_cwd_fallback(tmp_path):
    """paths=None（直接构造 ResolvedConfig 的旧 fixture 面）构造即 ValueError
    ——绝不静默回落按 cwd 推导。"""
    cfg = _dc_replace(make_cfg(tmp_path), paths=None)
    with pytest.raises(ValueError, match="paths"):
        Emitter(cfg, engine=None, run_id="a" * 12,
                run_started_at=datetime.now().astimezone())


# ── v1.13 写前终检按行取类有效 Schema（裁决·按类标注 Schema，spec §6.3）───────

from dataclasses import replace as _dc_replace                    # noqa: E402

# faq 类 Schema：不要求 difficulty（全局 USER_SCHEMA 要求）——按类取值时才放行
FAQ_SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string"}, "topic": {"type": "string"}},
    "required": ["intent", "topic"],
    "additionalProperties": False,
}
# chat 类 Schema：额外要求 answer（全局 USER_SCHEMA 无此字段）——按类取值时才拒
CHAT_SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string"}, "answer": {"type": "string"}},
    "required": ["intent", "answer"],
    "additionalProperties": False,
}


def class_schema_cfg(tmp_path, assignment="single"):
    """三个序列类：faq / chat 各自声明按类标注 Schema，plain 零覆盖（回落全局
    USER_SCHEMA），构成「按类 vs 回落」的对照面。"""
    cfg = make_cfg(tmp_path, classify=classify_cfg(
        assignment=assignment, classes=("faq", "chat", "plain")))
    views = {
        name: ClassView(name=name, quality=cfg.quality, rubric=cfg.rubric,
                        annotate=cfg.annotate, generate=cfg.generate,
                        verify=cfg.verify, extract=cfg.extract, schema=schema)
        for name, schema in (("faq", FAQ_SCHEMA), ("chat", CHAT_SCHEMA),
                            ("plain", None))
    }
    return _dc_replace(cfg, class_views=views)


def _cls(label, labels=None):
    return Classification(label=label, labels=tuple(labels or (label,)),
                          source="llm", detail={})


def test_prewrite_check_routes_per_row_class_schema(tmp_path):
    cfg = class_schema_cfg(tmp_path)
    # faq 行：过 faq Schema，但缺 difficulty ⇒ 若按全局终检会被拒（按类取值证明）
    faq = make_item(record=make_record("1" * 16, 1), classification=_cls("faq"),
                    output={"intent": "写作", "topic": "请假条"})
    # chat 行：过全局 Schema，但缺 answer ⇒ 该类 Schema 违规 ⇒ 进 rejects
    chat = make_item(record=make_record("2" * 16, 2), classification=_cls("chat"),
                     output={"intent": "闲聊", "topic": "天气", "difficulty": "easy"})
    # plain 行（零覆盖）：违反全局枚举 ⇒ 回落路径照常拒（字节等价 v1.12）
    plain = make_item(record=make_record("3" * 16, 3), classification=_cls("plain"),
                      output={"intent": "问答", "topic": "t", "difficulty": "极难"})
    _, result = run_emitter(cfg, [faq, chat, plain])

    assert result == EmitResult(emitted=1, rejected=2)
    rows = read_jsonl(tmp_path / "out" / "res.jsonl")
    assert len(rows) == 1                                # 违规行零落盘
    assert rows[0]["_meta"]["classification"]["label"] == "faq"
    assert rows[0]["intent"] == "写作" and "difficulty" not in rows[0]

    rejects = {r["_meta"]["id"]: r["_meta"]
               for r in read_jsonl(tmp_path / "out" / "res.rejects.jsonl")}
    assert set(rejects) == {"2" * 16, "3" * 16}
    for meta in rejects.values():
        assert (meta["stage"], meta["reason"]) == ("emitter", "internal_error")
    assert any("answer" in e for e in rejects["2" * 16]["errors"])
    assert chat.status == "failed" and plain.status == "failed"


def test_prewrite_check_multi_fanout_rows_use_own_label(tmp_path):
    # multi 扇出：兄弟信封共享 record 但各带自己的标签 ⇒ 各过各的类 Schema。
    cfg = class_schema_cfg(tmp_path, assignment="multi")
    record = make_record("4" * 16, 4)
    output = {"intent": "写作", "topic": "请假条"}      # 过 faq，不过 chat
    first = make_item(record=record, output=dict(output),
                      classification=_cls("faq", ("faq", "chat")))
    clone = make_item(record=record, output=dict(output),
                      classification=_cls("chat", ("faq", "chat")))
    _, result = run_emitter(cfg, [first, clone])

    assert result == EmitResult(emitted=1, rejected=1)
    rows = read_jsonl(tmp_path / "out" / "res.jsonl")
    assert [row["_meta"]["classification"]["label"] for row in rows] == ["faq"]
    reject = read_jsonl(tmp_path / "out" / "res.rejects.jsonl")[0]["_meta"]
    assert (reject["id"], reject["label"]) == ("4" * 16, "chat")


def test_rubric_selector_off_form_keeps_modality_default(tmp_path):
    cfg = make_cfg(tmp_path, quality_rubric="")
    run_emitter(cfg, [make_item()])
    meta = read_jsonl(tmp_path / "out" / "res.jsonl")[0]["_meta"]
    assert meta["run"]["rubric"] == "default:text"


# ── v1.18 sequence manifest-last delivery ────────────────────────────────────────


def _sequence_emitter(tmp_path: Path) -> SequenceDeliveryEmitter:
    """构造六个固定路径都存在的 sequence emitter。"""
    cfg = make_cfg(tmp_path)
    stem = tmp_path / "out" / "sequence"
    paths = _dc_replace(
        cfg.paths,
        output=str(stem.with_suffix(".jsonl")),
        stream=str(stem.with_suffix(".stream.jsonl")),
        report=str(stem.with_suffix(".report.json")),
        manifest=str(stem.with_suffix(".manifest.json")),
        failed_report=str(stem.with_suffix(".failed.report.json")),
        rejects=None,
        sidecar=None,
    )
    return SequenceDeliveryEmitter(paths)


def _sequence_assembly_request(tmp_path, item, projection, batch_no: int):
    """构造仅含 M11 终检所需程序真值的闭包请求。"""
    cfg = make_cfg(tmp_path)
    class_view = ClassView(
        name="ticket_booking", quality=cfg.quality, rubric=cfg.rubric,
        annotate=cfg.annotate, generate=cfg.generate, verify=cfg.verify,
        extract=cfg.extract, schema=USER_SCHEMA,
    )
    frame_schema = {
        "type": "object",
        "properties": {"frame": {"type": "string"}},
        "required": ["frame"],
        "additionalProperties": False,
    }
    frame_views = {
        name: SimpleNamespace(enabled=True) for name in ("request", "confirm")
    }
    program = SimpleNamespace(
        class_views={"ticket_booking": class_view},
        frame_classes=frame_views,
        frame_schema=frame_schema,
    )
    return SequenceAssemblyRequest(
        program, EngineStub(), item, projection, batch_no
    )


def _sequence_projection_and_item():
    """构造含全部下游产物的最终信封与 pre-downstream 投影。"""
    first = make_record("1" * 32, 1, {"utterance": "request"})
    second = make_record("2" * 32, 2, {"utterance": "confirmed"})
    sequence = make_seq_record((first, second), "3" * 32)
    truth = {
        "validation_mode": "declared", "sequence_class": "ticket_booking",
        "scenario_id": "4" * 32, "world_branch_id": "5" * 32,
        "pattern": "booking_success", "variant": "positive",
        "expected_violation": {}, "actual_violations": [],
    }
    sequence = _dc_replace(sequence, raw={"_meta": {"generation": truth}})
    primary = (
        {"payload": {"utterance": "request"}, "_meta": {"event": {
            "event_id": first.id, "frame_class": "request",
        }, "generation": {"validation_mode": "declared"}}},
        {"payload": {"utterance": "confirmed"}, "_meta": {"event": {
            "event_id": second.id, "frame_class": "confirm",
        }, "generation": {"validation_mode": "declared"}}},
    )
    projection = ProjectedSequence(sequence, primary)
    item = make_item(
        record=sequence, scores=True, verified=True,
        classification=Classification(
            "ticket_booking", ("ticket_booking",), "inherited", {}
        ),
        output={"intent": "book_ticket", "topic": "rail", "difficulty": "easy"},
    )
    item.session_id = "primary_000000"
    item.member_classifications = {
        first.id: Classification("request", ("request",), "inherited", {}),
        second.id: Classification("confirm", ("confirm",), "inherited", {}),
    }
    item.member_annotations = {
        first.id: Annotation({"frame": "request"}, "deepseek-v4-flash", 1, Usage()),
        second.id: Annotation({"frame": "confirm"}, "deepseek-v4-flash", 1, Usage()),
    }
    return projection, item, truth


def test_sequence_assembly_uses_final_item_for_every_output_byte(tmp_path):
    """最终 main/primary 与 retained 只来自同一 final item + projection。"""
    from labelkit.operators.generation.project import canonical_delivery_row

    emitter = _sequence_emitter(tmp_path)
    projection, item, truth = _sequence_projection_and_item()
    rows = emitter.assemble_sequence(
        _sequence_assembly_request(tmp_path, item, projection, 7)
    )

    main = json.loads(canonical_delivery_row(rows.main_row))
    meta = main["_meta"]
    assert main["intent"] == "book_ticket"
    assert meta["generation"] == truth
    assert meta["classification"] == {
        "label": "ticket_booking", "labels": ["ticket_booking"], "source": "inherited",
    }
    assert meta["scores"] == {
        "clarity": 0.72, "__aggregate__": 0.72,
        "mode": "pairwise_bt", "batch_no": 7, "pool": "ticket_booking",
    }
    assert meta["annotation"] == {"model": "glm-5.2", "attempts": 1}
    assert meta["verification"] == {"verdict": "pass", "rounds": 1}
    assert meta["stream"]["session_id"] == "primary_000000"
    assert [member["label"] for member in meta["stream"]["members"]] == [
        "request", "confirm",
    ]
    assert [member["annotation"] for member in meta["stream"]["members"]] == [
        {"frame": "request"}, {"frame": "confirm"},
    ]
    primary = [json.loads(canonical_delivery_row(row)) for row in rows.primary_stream_rows]
    assert [row["_meta"]["annotation"] for row in primary] == [
        {"frame": "request"}, {"frame": "confirm"},
    ]
    expected = len(canonical_delivery_row(rows.main_row)) + 1
    expected += sum(len(canonical_delivery_row(row)) + 1
                    for row in rows.primary_stream_rows)
    assert rows.retained_content_bytes == expected
    assert projection.primary_stream_rows[0]["_meta"].get("annotation") is None


def test_sequence_assembly_uses_only_program_annotation_schemas(tmp_path):
    """M11 不回落 engine 默认 Schema，也不混用 frame generate Schema。"""
    emitter = _sequence_emitter(tmp_path)
    projection, item, _truth = _sequence_projection_and_item()
    request = _sequence_assembly_request(tmp_path, item, projection, 1)

    class CountingEngine(EngineStub):
        """以 poison 默认 Schema 记录显式终检路由。"""

        def __init__(self):
            super().__init__({"type": "object", "required": ["poison"]})
            self.schemas = []

        def validate_only(self, obj, schema=None):
            self.schemas.append(schema)
            return super().validate_only(obj, schema=schema)

    engine = CountingEngine()
    frame_views = {
        name: SimpleNamespace(
            enabled=True,
            gen_schema={"type": "object", "required": ["poison"]},
        )
        for name in ("request", "confirm")
    }
    program = SimpleNamespace(
        class_views=request.program.class_views,
        frame_classes=frame_views,
        frame_schema=request.program.frame_schema,
    )
    emitter.assemble_sequence(
        SequenceAssemblyRequest(program, engine, item, projection, 1)
    )
    assert len(engine.schemas) == 5
    assert engine.schemas[0] == USER_SCHEMA
    assert all(schema == request.program.frame_schema for schema in engine.schemas[1:])


def test_sequence_assembly_rejects_invalid_sequence_annotation_as_projection(tmp_path):
    """最终 sequence annotation 违规时整条装配不返回任何 rows。"""
    emitter = _sequence_emitter(tmp_path)
    projection, item, _truth = _sequence_projection_and_item()
    item.annotation = Annotation(
        {"intent": "book_ticket"}, "offline", 1, Usage()
    )
    request = _sequence_assembly_request(tmp_path, item, projection, 1)
    with pytest.raises(GenerationProjectionMismatch):
        emitter.assemble_sequence(request)


def test_sequence_assembly_rejects_invalid_frame_annotation_as_projection(tmp_path):
    """任一实际 frame annotation 违规时不得降格或返回局部 rows。"""
    emitter = _sequence_emitter(tmp_path)
    projection, item, _truth = _sequence_projection_and_item()
    first = item.record.members[0]
    item.member_annotations[first.id] = Annotation(
        {"unexpected": True}, "offline", 1, Usage()
    )
    request = _sequence_assembly_request(tmp_path, item, projection, 1)
    with pytest.raises(GenerationProjectionMismatch):
        emitter.assemble_sequence(request)


@pytest.mark.parametrize("missing", ("sequence_schema", "frame_schema"))
def test_sequence_assembly_rejects_missing_program_schema_as_contract(
        tmp_path, missing):
    """缺失 program Schema 是终止契约错误，不消耗数据重试。"""
    emitter = _sequence_emitter(tmp_path)
    projection, item, _truth = _sequence_projection_and_item()
    request = _sequence_assembly_request(tmp_path, item, projection, 1)
    class_views = dict(request.program.class_views)
    frame_schema = request.program.frame_schema
    if missing == "sequence_schema":
        view = class_views["ticket_booking"]
        class_views["ticket_booking"] = _dc_replace(view, schema=None)
    else:
        frame_schema = None
    program = SimpleNamespace(
        class_views=class_views,
        frame_classes=request.program.frame_classes,
        frame_schema=frame_schema,
    )
    with pytest.raises(InternalError, match="generation_downstream_contract"):
        emitter.assemble_sequence(
            SequenceAssemblyRequest(program, request.schema_engine, item, projection, 1)
        )


@pytest.mark.parametrize("missing", ("sequence_view", "frame_view"))
def test_sequence_assembly_retries_unknown_program_class_view(tmp_path, missing):
    """未知 sequence 或 frame class view 属于整组可重试的投影不一致。"""
    emitter = _sequence_emitter(tmp_path)
    projection, item, _truth = _sequence_projection_and_item()
    request = _sequence_assembly_request(tmp_path, item, projection, 1)
    class_views = dict(request.program.class_views)
    frame_classes = dict(request.program.frame_classes)
    if missing == "sequence_view":
        class_views.clear()
    else:
        frame_classes.pop("request")
    program = SimpleNamespace(
        class_views=class_views,
        frame_classes=frame_classes,
        frame_schema=request.program.frame_schema,
    )
    with pytest.raises(GenerationProjectionMismatch):
        emitter.assemble_sequence(
            SequenceAssemblyRequest(program, request.schema_engine, item, projection, 1)
        )


def test_sequence_assembly_frame_only_skips_user_schema_and_validates_both_views(tmp_path):
    """frame-only 只终检两份帧对象，序列 annotation 与用户 Schema 调用为零。"""
    emitter = _sequence_emitter(tmp_path)
    projection, item, _truth = _sequence_projection_and_item()
    request = _sequence_assembly_request(tmp_path, item, projection, 1)
    class_view = request.program.class_views["ticket_booking"]
    class_view = _dc_replace(
        class_view,
        annotate=_dc_replace(class_view.annotate, enabled=False),
    )

    class CountingEngine(EngineStub):
        """记录所有显式终检 Schema。"""

        def __init__(self):
            super().__init__({"type": "object", "required": ["poison"]})
            self.schemas = []

        def validate_only(self, obj, schema=None):
            self.schemas.append(schema)
            return super().validate_only(obj, schema=schema)

    engine = CountingEngine()
    program = SimpleNamespace(
        class_views={"ticket_booking": class_view},
        frame_classes=request.program.frame_classes,
        frame_schema=request.program.frame_schema,
    )
    item.annotation = None
    rows = emitter.assemble_sequence(
        SequenceAssemblyRequest(program, engine, item, projection, 1)
    )
    assert rows.main_row["_meta"]["annotation"] is None
    assert len(engine.schemas) == 4
    assert all(schema == request.program.frame_schema for schema in engine.schemas)


def _success_report() -> dict:
    """返回尚未由 M11 写 digest 的最小成功报告。"""
    return {"generate": {"sequence": {
        "run_id": "a" * 32,
        "delivery_digest": None,
        "artifacts_committed": False,
    }}}


def test_sequence_prepare_and_commit_use_one_digest_and_manifest_last(tmp_path, monkeypatch):
    """prepare 唯一计算 digest，三件工件落盘后 manifest 引用同一值。"""
    emitter = _sequence_emitter(tmp_path)
    paths = emitter._paths
    operations = []
    original_replace = os.replace
    original_write = emitter._write_part

    def observe_write(target, data):
        operations.append(("write", Path(target)))
        original_write(target, data)

    def observe_replace(source, target):
        operations.append(("replace", Path(target)))
        original_replace(source, target)

    monkeypatch.setattr(emitter, "_write_part", observe_write)
    monkeypatch.setattr(os, "replace", observe_replace)
    projection, item, _truth = _sequence_projection_and_item()
    rows = emitter.assemble_sequence(
        _sequence_assembly_request(tmp_path, item, projection, 1)
    )
    product = emitter.prepare_product(
        (rows.main_row,), rows.primary_stream_rows, _success_report()
    )
    manifest = emitter.commit(product)
    assert operations == [
        ("write", Path(paths.output)),
        ("write", Path(paths.stream)),
        ("write", Path(paths.report)),
        ("replace", Path(paths.output)),
        ("replace", Path(paths.stream)),
        ("replace", Path(paths.report)),
        ("write", Path(paths.manifest)),
        ("replace", Path(paths.manifest)),
    ]

    digest = product.report["generate"]["sequence"]["delivery_digest"]
    assert len(digest) == 64
    assert manifest["delivery_digest"] == digest
    assert manifest["run_id"] == "a" * 32
    assert manifest["main"]["rows"] == 1
    assert manifest["stream"]["rows"] == 2
    report_doc = json.loads(Path(paths.report).read_text())
    assert tuple(report_doc["generate"]["sequence"]) == (
        "run_id", "delivery_digest", "artifacts_committed",
    )
    assert report_doc["generate"]["sequence"] == {
        "artifacts_committed": True,
        "delivery_digest": digest,
        "run_id": "a" * 32,
    }
    from labelkit.operators.generation.project import canonical_json

    assert read_jsonl(Path(paths.output)) == [json.loads(canonical_json(rows.main_row))]
    assert read_jsonl(Path(paths.stream)) == [
        json.loads(canonical_json(row)) for row in rows.primary_stream_rows
    ]
    stream_doc = [json.loads(line) for line in Path(paths.stream).read_text().splitlines()]
    assert all(tuple(row) == ("payload", "_meta") for row in stream_doc)
    manifest_doc = json.loads(Path(paths.manifest).read_text())
    assert tuple(manifest_doc) == (
        "schema_version", "run_id", "delivery_digest", "artifacts_committed",
        "main", "stream", "report", "committed_at",
    )
    assert tuple(manifest_doc["main"]) == ("path", "sha256", "rows")
    assert tuple(manifest_doc["stream"]) == ("path", "sha256", "rows")
    assert tuple(manifest_doc["report"]) == ("path", "sha256")
    assert manifest_doc["delivery_digest"] == digest
    assert manifest["main"]["sha256"] == hashlib.sha256(
        Path(paths.output).read_bytes()
    ).hexdigest()


def test_sequence_manifest_replace_failure_preserves_previous_success_marker(
        tmp_path, monkeypatch):
    """子工件可能已替换时，manifest 失败仍保留上一轮可信 marker。"""
    emitter = _sequence_emitter(tmp_path)
    paths = emitter._paths
    Path(paths.manifest).write_text("old-manifest", encoding="utf-8")
    product = emitter.prepare_product(({"main": 1},), ({"stream": 1},), _success_report())
    original_replace = os.replace

    def fail_manifest(source, target):
        if Path(target) == Path(paths.manifest):
            raise OSError("injected manifest failure")
        original_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_manifest)
    with pytest.raises(LabelKitError, match="generation_commit_io"):
        emitter.commit(product)

    assert Path(paths.manifest).read_text(encoding="utf-8") == "old-manifest"
    assert read_jsonl(Path(paths.output)) == [{"main": 1}]
    assert read_jsonl(Path(paths.stream)) == [{"stream": 1}]


@pytest.mark.parametrize("failed_name", ("output", "stream", "report"))
def test_sequence_artifact_replace_failure_never_advances_manifest(
    tmp_path, monkeypatch, failed_name,
):
    """任一前置工件替换失败都不能发布新 manifest。"""
    emitter = _sequence_emitter(tmp_path)
    paths = emitter._paths
    Path(paths.manifest).write_text("old-manifest", encoding="utf-8")
    product = emitter.prepare_product(({"main": 1},), ({"stream": 1},), _success_report())
    original_replace = os.replace

    def fail_artifact(source, target):
        if Path(target) == Path(getattr(paths, failed_name)):
            raise OSError("injected artifact failure")
        original_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_artifact)
    with pytest.raises(LabelKitError, match="generation_commit_io"):
        emitter.commit(product)
    assert Path(paths.manifest).read_text(encoding="utf-8") == "old-manifest"


def test_sequence_failed_report_is_atomic_and_does_not_touch_success_artifacts(tmp_path):
    """独立 failed report 替换不改变已有 main/stream/report/manifest。"""
    emitter = _sequence_emitter(tmp_path)
    paths = emitter._paths
    success_paths = tuple(Path(value) for value in (
        paths.output, paths.stream, paths.report, paths.manifest,
    ))
    for index, path in enumerate(success_paths):
        path.write_bytes(f"success-{index}".encode())
    report = {
        "run_attempt_id": "b" * 32, "run_id": None,
        "artifacts_committed": False, "failed_slot": None, "attempts_used": 0,
        "terminal_error_kind": "generation_plan_infeasible",
        "llm_usage": {}, "rejected_attempts": {},
    }

    emitter.write_failed_report(report)

    assert [path.read_bytes() for path in success_paths] == [
        f"success-{index}".encode() for index in range(4)
    ]
    assert json.loads(Path(paths.failed_report).read_text()) == report
    assert not Path(str(paths.failed_report) + ".part").exists()


@pytest.mark.parametrize("mutation", ("digest", "run_id", "missing_report"))
def test_sequence_invalid_product_identity_fails_before_any_part(
        tmp_path, mutation):
    """非法 digest/run identity 在打开任何 part 前 terminal。"""
    emitter = _sequence_emitter(tmp_path)
    report = _success_report()
    if mutation == "missing_report":
        report = {}
    else:
        report["generate"]["sequence"][mutation] = "invalid"
        if mutation == "digest":
            report["generate"]["sequence"]["delivery_digest"] = "invalid"
        else:
            report["generate"]["sequence"]["delivery_digest"] = "0" * 64
    from labelkit.common.contracts.generation import GenerationProduct

    product = GenerationProduct(({"main": 1},), ({"stream": 1},), report)
    with pytest.raises(InternalError, match="generation_downstream_contract"):
        emitter.commit(product)
    assert not list((tmp_path / "out").glob("*.part"))

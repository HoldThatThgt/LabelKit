"""Offline tests for M11 emitter (pure I/O + assembly logic; no LLM involved)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from labelkit import TOOL_VERSION
from labelkit.common.config.model import (
    AnnotateConfig, ClassifyConfig, ClassSpec, Criterion, DedupConfig,
    ExtractConfig, GenerateConfig, InputConfig, OutputConfig, QualityConfig,
    ResolvedConfig, Rubric, RunConfig, SegmentConfig, StitchConfig, StreamConfig,
    ToolConfig,
    TraceConfig, VerifyConfig,
)
from labelkit.operators.emitter import EmitResult, Emitter
from labelkit.common.errors import LabelKitError
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
    assert not kw, f"unknown overrides: {kw}"
    return ResolvedConfig(
        tool=ToolConfig(log_format=log_format),
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
        dry_run=False,
        config_path="config.toml",
        project_path="project.toml",
        config_digest="sha256:c",
        project_digest="sha256:p",
    )


def classify_cfg(assignment="single", classes=("faq", "chat")) -> ClassifyConfig:
    return ClassifyConfig(
        enabled=True, assignment=assignment,
        max_labels=len(classes) if assignment == "multi" else None,
        fallback_class=classes[-1],
        classes=tuple(ClassSpec(name=n, description="d") for n in classes))


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


def run_emitter(cfg, batch, batch_no=1, finalize=True, report=None, deliver=True):
    em = Emitter(cfg, EngineStub(), run_id="ab12cd34ef56", run_started_at=RUN_STARTED_AT)
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
    expect = (f"已写出 {tmp_path / 'out' / 'res.rejects.jsonl'}（2 行）"
              f"与 {tmp_path / 'out' / 'res.report.json'}")
    assert expect in msgs
    # ordering: batch flush line → finalize rename line → 已写出 line
    assert msgs.index(expect) > msgs.index(
        next(m for m in msgs if m.startswith("finalize：fsync + rename")))


def test_finalize_logs_report_only_when_rejects_none(tmp_path, caplog):
    cfg = make_cfg(tmp_path, rejects="none")
    # a rejected item that is COUNTED but never written (rejects='none'):
    # the 已写出 line must not claim a rejects file
    batch = [make_item(), make_item(status="dropped_dup",
                                    record=make_record("2" * 16, 2),
                                    annotated=False)]
    with caplog.at_level(logging.INFO, logger=".".join(("labelkit", "emitter"))):
        run_emitter(cfg, batch)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("已写出")]
    assert lines == [f"已写出 {tmp_path / 'out' / 'res.report.json'}"]


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
    assert "批 2" in line
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
    from dataclasses import replace
    cfg = replace(make_cfg(tmp_path), dry_run=True)
    em = Emitter(cfg, engine=None, run_id="a" * 12,
                 run_started_at=datetime.now().astimezone())
    assert str(em._report_path).endswith(".dryrun.report.json")
    em_real = Emitter(make_cfg(tmp_path), engine=None, run_id="a" * 12,
                      run_started_at=datetime.now().astimezone())
    assert str(em_real._report_path).endswith("res.report.json")

"""v1.17 时间流形态的 M6 离线测试（SPEC-scenario-planning §6/§9/§11；零 LLM——
LLM 调用点经仓内既有 SchemaEngine 进程内桩先例路由，禁 mock 传输层）。

覆盖：``plan_delivery`` 的 delivery.profile 流与布局承源（word/timestamps/tier 零
重排）、布局驱动装配（任务帧槽位 truth 键序含 tier_rank、noise 槽位 frame_class 实名
与 interiority、duplicate 深拷贝与 source-shortfall 省略、空 session 消失与时间重
编号、crossed 计数）、时间字段回填（µs 算术、先回填后 duplicate 拷贝）、工件行/
直装信封/replay id、brief/realize 提示装配与 counters、noise realize 的
sample_validator 闸门与作废缺席、similarity 过滤。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
from dataclasses import replace
from types import SimpleNamespace

import pytest

from labelkit.common.config.model import (
    AnnotateConfig,
    ClassView,
    ClassifyConfig,
    ConsoleConfig,
    DedupConfig,
    ExtractConfig,
    FrameClassView,
    FrameClassifyConfig,
    GenerateConfig,
    GenerateStreamConfig,
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
    TierSpec,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
    render_constraint_text,
)
from labelkit.common.contracts.types import Usage
from labelkit.common.errors import ContextOverflowError, SchemaViolation
from labelkit.common.runtime.scenario.model import (
    CorrelationSpec,
    DuplicateLayout,
    FrameLayout,
    FrameRuleSpec,
    FrameWindowSpec,
    NoiseSlot,
    PlannerModelStats,
    PlannerObjectives,
    QuotaSummary,
    ScenarioPlan,
    ScheduleSpec,
    SequenceLayout,
    SequenceSlotSpec,
    SessionLayout,
)
from labelkit.operators.generate import GenerateStage
from labelkit.operators.generate_stream import (
    RealizedNoise,
    RealizedSequence,
    SequencePlan,
    _StreamSlot,
    _payload_text,
    _reduced_gen_schema,
    _us_iso,
    assemble_stream,
    backfill_time_fields,
    canonical_json,
    plan_delivery,
    render_brief_prompt_texts,
    render_realize_prompt_texts,
    stream_artifact_path,
    weave_scenario_stream,
)

SCHED = ScheduleSpec(start_us=1_767_580_800 * 1_000_000,
                     end_us=(1_767_580_800 + 8 * 3600) * 1_000_000,
                     utc_offset_minutes=480, exclude_dates=())


def mk_cfg(tmp_path, *, seed=7, tiers=(), frame_rules=(), duplicates=1) -> ResolvedConfig:
    """直构 M1 形状的 v1.17 ResolvedConfig（含冻结 ScenarioPlan）。"""
    plan = mk_plan(tiers=tiers, duplicates=duplicates)
    view = ClassView(
        name="alpha", quality=QualityConfig(), rubric=Rubric(name="r", criteria=()),
        annotate=AnnotateConfig(), verify=VerifyConfig(), extract=ExtractConfig(),
        generate=GenerateConfig(enabled=True, instruction="生成序列",
                                len_range=(2, 2)))
    frame_views = {
        "task_request": FrameClassView(instruction="", examples=(), enabled=False,
                                       gen_instruction="生成首帧"),
        "followup": FrameClassView(instruction="", examples=(), enabled=False,
                                   gen_instruction="生成跟进帧"),
        "chatter": FrameClassView(instruction="", examples=(), enabled=False,
                                  gen_instruction="生成闲聊"),
    }
    return ResolvedConfig(
        tool=ToolConfig(), console=ConsoleConfig(), llm_profiles={}, embedding_profiles={},
        run=RunConfig(output=str(tmp_path / "o.jsonl"), modality="text",
                      mode="generate_only", seed=seed),
        input=InputConfig(text_field="text"),
        stream=StreamConfig(order_by="meta:ts", gap_s=900, session_max_len=12),
        dedup=DedupConfig(), segment=SegmentConfig(), stitch=StitchConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(enabled=True, classes=()),
        quality=QualityConfig(), generate=GenerateConfig(enabled=True),
        annotate=AnnotateConfig(instruction="标注"),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"), trace=TraceConfig(),
        rubric=Rubric(name="r", criteria=()),
        class_views={"alpha": view}, user_schema={},
        frame_classify=FrameClassifyConfig(),
        frame_class_views=frame_views,
        generate_stream=GenerateStreamConfig(
            enabled=True, schedule=SCHED, tiers=tuple(tiers),
            frame_rules=tuple(frame_rules), duplicates=duplicates),
        scenario_plan=plan,
        limit=None, strict=False, dry_run=False, config_path="c.toml",
        project_path="p.toml", config_digest="sha256:c", project_digest="sha256:p",
    )


def mk_layout(slot_key: str, session_index: int, words, base_us: int,
              owner_role: str = "primary") -> SequenceLayout:
    step = 10_000_000
    frames = tuple(FrameLayout(position=i, frame_class=word,
                               start_us=base_us + i * step, end_us=base_us + i * step,
                               duration_target_us=None, resources=())
                   for i, word in enumerate(words))
    return SequenceLayout(slot_key=slot_key, session_index=session_index,
                          owner_role=owner_role, anchor_date="2026-01-05",
                          start_us=base_us, last_point_us=base_us,
                          end_us=base_us + len(frames) * step, frames=frames)


def mk_plan(*, tiers=(), duplicates=1) -> ScenarioPlan:
    """两 slot / 两 session / 一 noise / 一 duplicate 的最小冻结计划。"""
    slots = (SequenceSlotSpec(key="sequence:alpha:0", sequence_class="alpha",
                              class_ordinal=0,
                              tier_rank=tiers[0].tier_rank if tiers else None,
                              length_target=2, length_range=(2, 2)),
             SequenceSlotSpec(key="sequence:alpha:1", sequence_class="alpha",
                              class_ordinal=1,
                              tier_rank=tiers[1].tier_rank if len(tiers) > 1 else None,
                              length_target=2, length_range=(2, 2)))
    base = SCHED.start_us
    layouts = (mk_layout("sequence:alpha:0", 0, ("task_request", "followup"),
                         base + 60_000_000),
               mk_layout("sequence:alpha:1", 1, ("task_request", "followup"),
                         base + 600_000_000))
    sessions = (SessionLayout(index=0, primary_slot_key="sequence:alpha:0",
                              secondary_slot_key=None, start_us=base + 60_000_000,
                              last_point_us=base + 60_000_000, end_us=base + 70_000_000,
                              noise_count=1),
                SessionLayout(index=1, primary_slot_key="sequence:alpha:1",
                              secondary_slot_key=None, start_us=base + 600_000_000,
                              last_point_us=base + 600_000_000,
                              end_us=base + 610_000_000, noise_count=0))
    noise = (NoiseSlot(key="noise:chatter:0", frame_class="chatter", class_ordinal=0,
                       session_index=0,
                       timestamp_us=base + 60_000_000 + 5_000_000),)
    dup_frames = tuple(replace(frame, start_us=frame.start_us + 3_600_000_000_000,
                               end_us=frame.end_us + 3_600_000_000_000)
                       for frame in layouts[0].frames)
    dups = (DuplicateLayout(key="duplicate:0", ordinal=0,
                            source_slot_key="sequence:alpha:0", session_index=2,
                            offset_us=3_600_000_000_000, frames=dup_frames),) \
        if duplicates else ()
    stats = PlannerModelStats(0, 0, {})
    return ScenarioPlan(
        slots=slots, layouts=layouts, sessions=sessions, noise_slots=noise,
        duplicates=dups,
        quota_summary=(QuotaSummary(name="q", period="schedule", bucket="schedule",
                                    sequence_class="alpha", target=2),),
        objectives=PlannerObjectives(0, 1, base + 7_200_000_000_000),
        models={"quota": stats, "timeline": stats}, plan_digest="sha256:test")


def mk_sequence(cfg: ResolvedConfig, index: int, payloads) -> RealizedSequence:
    plan = plan_delivery(cfg).sequences[index]
    return RealizedSequence(plan=plan, frame_classes=plan.frame_classes,
                            payloads=tuple(payloads))


class FakeEngine:
    """SchemaEngine 进程内桩：brief 与 realize 都回既定内容。"""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls: list[dict] = []

    async def complete_validated(self, llm, prompt, schema=None, scope=None):
        self.calls.append({"llm": llm, "system": prompt.messages[0].parts[0].text,
                           "user": prompt.messages[1].parts[0].text})
        if "steps" in schema["properties"]:                     # brief_schema
            length = 2
            obj = {"steps": [{"brief": f"要点{i}"} for i in range(length)]}
        else:                                                   # realize_schema
            from labelkit.common.errors import SchemaViolation
            if not self._payloads:
                raise SchemaViolation(["stub has no payloads left"], {})
            take = min(len(schema["properties"]["frames"]["prefixItems"]),
                       len(self._payloads))
            frames = [self._payloads.pop(0) for _ in range(take)]
            obj = {"frames": frames}
        return obj, Usage(prompt_tokens=1, completion_tokens=1), 1, "test-model"


class FailingEngine(FakeEngine):
    """按调用阶段注入一次或持续失败。"""

    def __init__(self, payloads, failures):
        super().__init__(payloads)
        self._failures = list(failures)

    async def complete_validated(self, llm, prompt, schema=None, scope=None):
        phase = "brief" if "steps" in schema["properties"] else (
            "noise" if "[chatter]" in (prompt.messages[0].parts[0].text
                                        + prompt.messages[1].parts[0].text) else "realize")
        if self._failures and self._failures[0] == phase:
            self._failures.pop(0)
            raise SchemaViolation([f"{phase} failed"], {})
        return await super().complete_validated(llm, prompt, schema, scope)


class OverflowEngine(FakeEngine):
    async def complete_validated(self, llm, prompt, schema=None, scope=None):
        raise ContextOverflowError("overflow", phase="reactive", profile=llm)


class FakeMetrics:
    def __init__(self):
        self.counters: dict[str, int] = {}

    def count(self, key, value=1):
        self.counters[key] = self.counters.get(key, 0) + value

    def record_provider_result(self, **kw):
        pass


def run_stage(cfg: ResolvedConfig, engine):
    stage = GenerateStage(cfg)
    ctx = SimpleNamespace(rng=random.Random(0), batch_no=0, metrics=FakeMetrics(),
                          schema_engine=engine)
    product = asyncio.run(stage.generate_stream_all(ctx))
    return product, ctx.metrics


# ── plan_delivery：布局承源与 delivery.profile 流 ──────────────────────────


def test_plan_delivery_inherits_words_timestamps_and_tiers(tmp_path):
    cfg = mk_cfg(tmp_path)
    delivery = plan_delivery(cfg)
    assert [p.frame_classes for p in delivery.sequences] == [
        ("task_request", "followup"), ("task_request", "followup")]
    assert all(len(p.timestamps_us) == 2 for p in delivery.sequences)
    assert [p.slot_key for p in delivery.sequences] == [
        "sequence:alpha:0", "sequence:alpha:1"]
    assert delivery.noise_plans[0].frame_class == "chatter"


def test_plan_delivery_predraw_is_stable_across_replays(tmp_path):
    cfg = mk_cfg(tmp_path)
    first = plan_delivery(cfg)
    second = plan_delivery(cfg)
    assert [(p.llm, p.style_name) for p in first.sequences] == [
        (p.llm, p.style_name) for p in second.sequences]
    assert [p.llm for p in first.noise_plans] == [p.llm for p in second.noise_plans]


def test_us_iso_uses_the_schedule_offset(tmp_path):
    cfg = mk_cfg(tmp_path)
    text = _us_iso(SCHED.start_us, cfg)
    assert text == "2026-01-05T10:40:00.000000+08:00"


# ── 布局驱动装配 ────────────────────────────────────────────────────────────


def weaved(tmp_path, *, tiers=(), duplicates=1, include=(0, 1), noise=True):
    cfg = mk_cfg(tmp_path, tiers=tiers, duplicates=duplicates)
    survivors = [mk_sequence(cfg, i, [{"utterance": f"帧{i}-{j}"} for j in range(2)])
                 for i in include]
    noises = ([RealizedNoise(slot=cfg.scenario_plan.noise_slots[0],
                             payload="闲聊一句")] if noise else [])
    sessions, stats = weave_scenario_stream(survivors, noises, cfg.scenario_plan, cfg)
    return cfg, survivors, noises, sessions, stats


def test_weave_assembles_task_noise_and_duplicate_sessions(tmp_path):
    cfg, survivors, noises, sessions, stats = weaved(tmp_path)
    assert len(sessions) == 3                    # 两个 primary + 一条 duplicate 尾
    task_rows = [slot for session in sessions[:2] for slot in session
                 if slot.owner is not None]
    assert len(task_rows) == 4
    noise_rows = [slot for session in sessions for slot in session
                  if slot.truth["noise"]]
    assert len(noise_rows) == 1
    assert noise_rows[0].truth["frame_class"] == "chatter"   # v1.17 实名
    dup_rows = sessions[2]
    assert all(slot.truth["duplicate_of"] == 0 for slot in dup_rows)
    assert all(slot.truth["sequence"] is None for slot in dup_rows)
    assert stats["sessions"] == 2 and stats["duplicates"] == 1
    assert stats["noise_frames"] == 1 and stats["frames"] == 4


def test_weave_orders_sessions_in_time_and_renumbers(tmp_path):
    cfg, _survivors, _noises, sessions, _stats = weaved(tmp_path)
    stamps = [slot.ts_us for session in sessions for slot in session]
    assert stamps == sorted(stamps)
    assert [slot.truth["session"] for session in sessions for slot in session] == [
        i for i, session in enumerate(sessions) for _ in session]


def test_weave_drops_noise_outside_surviving_task_bounds(tmp_path):
    cfg, survivors, _noises, sessions, stats = weaved(tmp_path, include=(1,))
    assert len(sessions) == 1                    # session 0 任务全灭 ⇒ 连 noise 消失
    assert stats["noise_frames"] == 0
    assert stats["frames"] == 2


def test_weave_omits_duplicate_when_source_undelivered(tmp_path):
    cfg, _s, _n, sessions, stats = weaved(tmp_path, include=(1,))
    assert len(sessions) == 1
    assert stats["duplicates"] == 0


def test_weave_carries_tier_truth_only_when_tiered(tmp_path):
    tiers = (TierSpec(tier_rank=1, weight=1, frame_classes=("task_request", "followup")),
             TierSpec(tier_rank=2, weight=1, frame_classes=("followup",)))
    cfg, _s, _n, sessions, _stats = weaved(tmp_path, tiers=tiers)
    truth = [slot.truth for session in sessions for slot in session]
    assert all("tier_rank" in row for row in truth[:4])
    plain_cfg, _s2, _n2, plain_sessions, _ = weaved(tmp_path)
    plain_truth = [slot.truth for session in plain_sessions for slot in session]
    assert all("tier_rank" not in row for row in plain_truth)


def test_crossed_sessions_counted_when_both_owners_delivered(tmp_path):
    cfg = mk_cfg(tmp_path)
    plan = cfg.scenario_plan
    crossed_sessions = (SessionLayout(
        index=0, primary_slot_key="sequence:alpha:0",
        secondary_slot_key="sequence:alpha:1",
        start_us=plan.sessions[0].start_us,
        last_point_us=plan.sessions[0].last_point_us,
        end_us=plan.sessions[0].end_us, noise_count=0), plan.sessions[1])
    crossed_layouts = (replace(plan.layouts[0], session_index=0),
                       replace(plan.layouts[1], session_index=0,
                               owner_role="secondary"))
    crossed_plan = replace(plan, sessions=crossed_sessions, layouts=crossed_layouts)
    survivors = [mk_sequence(cfg, i, [{"utterance": f"帧{i}-{j}"} for j in range(2)])
                 for i in (0, 1)]
    sessions, stats = weave_scenario_stream(survivors, (), crossed_plan, cfg)
    assert stats["crossed_sessions"] == 1
    assert stats["sessions"] == 1 + len(crossed_plan.duplicates)


# ── 时间字段回填（µs 算术）与 duplicate 承源 ────────────────────────────────


TIMED_FRAME_VIEW = FrameClassView(
    instruction="", examples=(), enabled=False, gen_instruction="生成首帧",
    gen_schema={"type": "object",
                "properties": {"utterance": {"type": "string"},
                               "started_at": {"type": "string"},
                               "gap": {"type": "number"}},
                "required": ["utterance", "started_at", "gap"],
                "additionalProperties": False},
    time_fields={"started_at": "ts", "gap": "gap_next_s"})


def test_backfill_writes_microsecond_values_into_payloads(tmp_path):
    cfg, survivors, noises, sessions, _stats = weaved(tmp_path)
    cfg.frame_class_views["task_request"] = TIMED_FRAME_VIEW
    sessions, _stats = weave_scenario_stream(survivors, noises, cfg.scenario_plan, cfg)
    tasks = [slot for slot in sessions[0] if slot.owner is not None]
    first = tasks[0].payload
    assert first["started_at"] == tasks[0].ts
    assert first["gap"] == 10.0                  # 10s 序内间隔（下一帧同为序内成员）
    assert "gap" not in tasks[1].payload         # 绑定只写绑定帧类（followup 无绑定）


def test_duplicate_payloads_inherit_backfilled_values(tmp_path):
    cfg, survivors, noises, sessions, _stats = weaved(tmp_path)
    cfg.frame_class_views["task_request"] = TIMED_FRAME_VIEW
    sessions, _stats = weave_scenario_stream(survivors, noises, cfg.scenario_plan, cfg)
    dup = sessions[-1]
    assert dup[0].payload["started_at"] == sessions[0][0].payload["started_at"]
    assert dup[0].ts != sessions[0][0].ts        # 行 ts 平移、payload 时间承源


def test_backfill_first_and_last_boundaries(tmp_path):
    cfg = mk_cfg(tmp_path)
    cfg.frame_class_views["task_request"] = TIMED_FRAME_VIEW
    slots = [_StreamSlot(payload={"utterance": "a"}, truth={"frame_class": "task_request"},
                         owner=0, ts=_us_iso(SCHED.start_us, cfg),
                         ts_us=SCHED.start_us),
             _StreamSlot(payload={"utterance": "b"}, truth={"frame_class": "task_request"},
                         owner=0, ts=_us_iso(SCHED.start_us + 10_000_000, cfg),
                         ts_us=SCHED.start_us + 10_000_000)]
    backfill_time_fields([slots], cfg)
    assert slots[0].payload["gap"] == 10.0
    assert slots[1].payload["gap"] == 0.0


# ── 工件行 / 直装信封 / replay id ──────────────────────────────────────────


def test_assemble_stream_rows_envelopes_and_replay_ids(tmp_path):
    cfg, survivors, noises, sessions, _stats = weaved(tmp_path)
    lines, envelopes = assemble_stream(sessions, survivors, cfg)
    assert len(lines) == 4 + 1 + 2               # 任务 + noise + duplicate
    rows = [json.loads(line) for line in lines]
    assert all(set(row) == {"ts", "text", "truth"} for row in rows)
    assert rows[-1]["truth"]["duplicate_of"] == 0
    assert len(envelopes) == 2
    for envelope in envelopes:
        assert envelope.record.kind == "sequence"
        assert envelope.classification.source == "inherited"
        assert all(c.source == "inherited"
                   for c in envelope.member_classifications.values())
        assert envelope.session_id
    member_ids = []
    for session in sessions:
        for slot in session:
            row = {"ts": slot.ts, "text": slot.payload, "truth": slot.truth}
            member_ids.append(hashlib.sha256(
                canonical_json(row).encode("utf-8")).hexdigest()[:16])
    flat = {rid for envelope in envelopes
            for rid in [r.id for r in envelope.record.members]}
    assert flat <= set(member_ids)


def test_stream_artifact_path_derives_from_output_stem(tmp_path):
    cfg = mk_cfg(tmp_path)
    assert stream_artifact_path(cfg).endswith("o.stream.jsonl")


# ── 提示装配（§10.17/§10.18 面）────────────────────────────────────────────


def test_render_brief_prompt_texts_carries_word_and_constraints():
    system, user = render_brief_prompt_texts(
        "指令", ("task_request", "followup"), "alpha", 2, "规则：x")
    assert "1: task_request" in system and "2: followup" in system
    assert "[约束]" in system and "规则：x" in system
    assert "请为一条「alpha」序列产出固定的 2 个 brief。" == user


def test_render_constraint_text_converts_us_and_names():
    rule = FrameRuleSpec(name="r", template="chain_response", source="a", target="b",
                         time_us=(1_200_000_000, 2_400_000_000),
                         correlation=CorrelationSpec(source_field="id",
                                                     target_field="id"))
    window = FrameWindowSpec(name="w", frame_class="a",
                             of_day_us=((8 * 3600 * 1_000_000,
                                         11 * 3600 * 1_000_000),), of_week=(1,))
    text = render_constraint_text((rule,), (window,))
    assert "name=r" in text and "time_s=[1200, 2400) 秒" in text
    assert "source.id 与 target.id 的 JSON 类型及值必须相同" in text
    assert "name=w" in text and "08:00" in text and "11:00" in text and "mon" in text
    assert render_constraint_text((), ()) == "none"


def test_render_realize_prompt_texts_contract_lines():
    system, user = render_realize_prompt_texts(
        "指令", None, [("a", "要点")], ["内容契约：自由文本"], constraints="规则：x")
    assert "第 1 帧（a）须符合：内容契约：自由文本" in system
    assert "[约束]" in system and "1. [a] 要点" in user


def test_reduced_gen_schema_strips_bound_fields():
    reduced = _reduced_gen_schema(TIMED_FRAME_VIEW)
    assert set(reduced["properties"]) == {"utterance"}
    assert reduced["required"] == ["utterance"]
    assert TIMED_FRAME_VIEW.gen_schema["properties"]["started_at"]   # 原件不污染


def test_payload_text_projects_structured_frames():
    assert _payload_text("纯文本") == "纯文本"
    assert _payload_text({"b": 2, "a": 1}) == '{"a":1,"b":2}'


# ── 算子主面：slot 交付、noise realize、counter 落账 ────────────────────────


def test_generate_stream_all_delivers_slots_noise_and_artifact(tmp_path):
    cfg = mk_cfg(tmp_path)
    engine = FakeEngine([
        {"utterance": "首帧0"}, {"utterance": "跟进0"},
        {"utterance": "首帧1"}, {"utterance": "跟进1"},
        "闲聊一句",
    ])
    product, metrics = run_stage(cfg, engine)
    assert len(product.envelopes) == 2
    assert len(product.artifact_lines) == 7
    assert metrics.counters["generate.stream.brief_calls"] == 2
    assert metrics.counters["generate.stream.realize_calls"] == 2
    assert metrics.counters["generate.stream.noise_calls"] == 1
    assert metrics.counters["generate.stream.sequences.alpha.planned"] == 2
    assert metrics.counters["generate.stream.sequences.alpha.produced"] == 2
    assert metrics.counters["generate.stream.sessions"] == 2
    assert metrics.counters["generate.stream.duplicates"] == 1
    assert metrics.counters["generate.stream.frame_rules.sampled"] == 2


def test_voided_sequence_absents_without_failed_records(tmp_path):
    cfg = mk_cfg(tmp_path)
    engine = FakeEngine([
        {"utterance": "首帧0"}, {"utterance": "跟进0"},
    ])                                          # slot 1 的 realize 无料 ⇒ 作废
    product, metrics = run_stage(cfg, engine)
    assert len(product.envelopes) == 1
    assert metrics.counters["generate.stream.sequences.alpha.produced"] == 1


def test_brief_prompt_uses_frozen_word_in_realize(tmp_path):
    cfg = mk_cfg(tmp_path)
    engine = FakeEngine([
        {"utterance": "首帧0"}, {"utterance": "跟进0"},
        {"utterance": "首帧1"}, {"utterance": "跟进1"}, "x",
    ])
    run_stage(cfg, engine)
    realize_calls = [c for c in engine.calls if "请实现全部" in c["user"]]
    assert len(realize_calls) == 3              # 两条序列 + 一个 noise slot
    assert "[task_request] 要点0" in realize_calls[0]["user"]
    assert "[chatter]" in realize_calls[2]["user"]


def test_sample_validator_voids_noise_slot(tmp_path):
    cfg = mk_cfg(tmp_path)
    cfg = replace(cfg, generate=replace(
        cfg.generate, sample_validator="hook.py:reject"),
        validation_hooks=SimpleNamespace(
            sample=SimpleNamespace(
                target=lambda text: ["violation"] if "闲聊" in text else [])))
    engine = FakeEngine([
        {"utterance": "首帧0"}, {"utterance": "跟进0"},
        {"utterance": "首帧1"}, {"utterance": "跟进1"},
        "闲聊一句",
    ])
    product, metrics = run_stage(cfg, engine)
    assert len(product.envelopes) == 2
    assert len(product.artifact_lines) == 6      # noise 槽位作废 ⇒ 缺席
    assert metrics.counters["generate.buckets.default×null.rejected_by_validator"] >= 1


def test_sequence_validator_voids_whole_slot(tmp_path):
    cfg = mk_cfg(tmp_path)
    cfg = replace(cfg, generate=replace(cfg.generate,
                                        sequence_validator="hook.py:reject"),
                  validation_hooks=SimpleNamespace(
                      sequence=SimpleNamespace(
                          target=lambda value: ["violation"])))
    stage = GenerateStage(cfg)
    ctx = SimpleNamespace(rng=random.Random(0), batch_no=0, metrics=FakeMetrics(),
                          schema_engine=FakeEngine([
                              {"utterance": "首帧0"}, {"utterance": "跟进0"},
                              {"utterance": "首帧1"}, {"utterance": "跟进1"},
                              "x"]))
    product = asyncio.run(stage.generate_stream_all(ctx))
    assert product.envelopes == []
    assert ctx.metrics.counters["generate.buckets.alpha×default×null.rejected_by_validator"] == 2


@pytest.mark.parametrize(("kind", "hook_name", "hook_attr", "bucket"), [
    ("sample", "sample_validator", "sample", "sample_validator"),
    ("sample_exception", "sample_validator", "sample", "sample_validator_exception"),
    ("sequence", "sequence_validator", "sequence", "sequence_validator"),
    ("sequence_exception", "sequence_validator", "sequence", "sequence_validator_exception"),
])
def test_validator_delivery_buckets_and_attempt_conservation(
        tmp_path, kind, hook_name, hook_attr, bucket):
    def reject(_value):
        if kind.endswith("exception"):
            raise RuntimeError("injected validator failure")
        return ["violation"]

    cfg = mk_cfg(tmp_path)
    cfg = replace(cfg, generate=replace(cfg.generate, **{hook_name: "hook.py:reject"}),
                  validation_hooks=SimpleNamespace(
                      **{hook_attr: SimpleNamespace(target=reject)}))
    _product, metrics = run_stage(cfg, FakeEngine([
        {"utterance": "首帧0"}, {"utterance": "跟进0"},
        {"utterance": "首帧1"}, {"utterance": "跟进1"}, "x"]))
    failures = sum(metrics.counters.get(
        f"generate.stream.delivery.failures.{name}", 0)
        for name in ("brief", "realize", "noise", "context_overflow",
                     "sample_validator", "sample_validator_exception",
                     "correlation", "temporal", "sequence_validator",
                     "sequence_validator_exception", "similarity",
                     "scenario_validator", "scenario_validator_exception"))
    assert metrics.counters[f"generate.stream.delivery.failures.{bucket}"] == 2
    assert metrics.counters["generate.stream.delivery.attempts"] == failures


@pytest.mark.parametrize("exception", [False, True])
def test_noise_validator_delivery_buckets(tmp_path, exception):
    cfg = mk_cfg(tmp_path)
    def hook(text):
        if exception and "闲聊" in text:
            raise RuntimeError("injected noise validator failure")
        return ["violation"] if "闲聊" in text else []
    cfg = replace(cfg, generate=replace(cfg.generate,
                                        sample_validator="hook.py:reject"),
                  validation_hooks=SimpleNamespace(sample=SimpleNamespace(target=hook)))
    _product, metrics = run_stage(cfg, FakeEngine([
        {"utterance": "首帧0"}, {"utterance": "跟进0"},
        {"utterance": "首帧1"}, {"utterance": "跟进1"}, "闲聊一句"]))
    bucket = "sample_validator_exception" if exception else "sample_validator"
    assert metrics.counters[f"generate.stream.delivery.failures.{bucket}"] == 1


def test_similarity_filter_drops_near_duplicate_sequences(tmp_path):
    cfg = replace(mk_cfg(tmp_path), dedup=DedupConfig(
        minhash_threshold=0.1, minhash_num_perm=32, ngram=2))
    engine = FakeEngine([
        {"utterance": "完全相同的序列内容甲"}, {"utterance": "完全相同的序列内容乙"},
        {"utterance": "完全相同的序列内容丙"}, {"utterance": "完全相同的序列内容丁"},
        "x",
    ])
    product, metrics = run_stage(cfg, engine)
    assert len(product.envelopes) <= 1           # 两条近重序列至多幸存一条


def test_tier_counters_feed_class_segmented_keys(tmp_path):
    tiers = (TierSpec(tier_rank=1, weight=2,
                      frame_classes=("task_request", "followup")),
             TierSpec(tier_rank=2, weight=1, frame_classes=("followup",)))
    cfg = mk_cfg(tmp_path, tiers=tiers)
    engine = FakeEngine([
        {"utterance": "首帧0"}, {"utterance": "跟进0"},
        {"utterance": "首帧1"}, {"utterance": "跟进1"}, "x",
    ])
    product, metrics = run_stage(cfg, engine)
    assert metrics.counters["generate.stream.tiers.alpha.1.planned"] == 1
    assert metrics.counters["generate.stream.tiers.alpha.2.planned"] == 1
    assert (metrics.counters["generate.stream.tiers.alpha.1.produced"]
            + metrics.counters["generate.stream.tiers.alpha.2.produced"]) == 2


def test_delivery_call_failures_use_phase_buckets_and_conserve_attempts(tmp_path):
    cfg = replace(mk_cfg(tmp_path), generate_stream=replace(
        mk_cfg(tmp_path).generate_stream, max_attempts_per_slot=2))
    engine = FailingEngine([
        {"utterance": "首帧0"}, {"utterance": "跟进0"},
        {"utterance": "首帧1"}, {"utterance": "跟进1"}, "x",
    ], ["brief"])
    _product, metrics = run_stage(cfg, engine)
    assert metrics.counters["generate.stream.delivery.failures.brief"] == 1
    assert metrics.counters["generate.stream.delivery.attempts"] == 4


def test_delivery_context_overflow_is_exhausted_without_retry(tmp_path):
    cfg = replace(mk_cfg(tmp_path), llm_profiles={"default": LLMProfile(
        name="default", provider="anthropic", base_url="", model="m",
        api_key_env="KEY", context_window=1, max_output_tokens=1)})
    stage = GenerateStage(cfg)
    ctx = SimpleNamespace(rng=random.Random(0), batch_no=0, metrics=FakeMetrics(),
                          schema_engine=OverflowEngine([]))
    asyncio.run(stage.generate_stream_all(ctx))
    assert ctx.metrics.counters["generate.stream.delivery.failures.context_overflow"] == 3
    assert ctx.metrics.counters["generate.stream.delivery.attempts"] == 3


@pytest.mark.parametrize(("phase", "bucket"), [("realize", "realize"), ("noise", "noise")])
def test_delivery_realize_and_noise_failures_are_bucketed(tmp_path, phase, bucket):
    cfg = mk_cfg(tmp_path)
    engine = FailingEngine([
        {"utterance": "首帧0"}, {"utterance": "跟进0"},
        {"utterance": "首帧1"}, {"utterance": "跟进1"}, "x",
    ], [phase])
    _product, metrics = run_stage(cfg, engine)
    assert metrics.counters[f"generate.stream.delivery.failures.{bucket}"] == 1


def test_scenario_observes_only_similarity_committed_prefix(tmp_path):
    cfg = mk_cfg(tmp_path)
    seen = []
    cfg = replace(cfg, generate=replace(cfg.generate, scenario_validator="hook.py:scenario"),
                  validation_hooks=SimpleNamespace(
                      scenario=SimpleNamespace(target=lambda value: seen.append(
                          len(value.accepted)) or [])))
    engine = FakeEngine([
        {"utterance": "相同"}, {"utterance": "相同"},
        {"utterance": "相同"}, {"utterance": "相同"}, "x",
    ])
    run_stage(cfg, engine)
    assert seen == [0]


    cfg = mk_cfg(tmp_path)
    assert cfg.generate_stream.max_attempts_per_slot == 3
    cfg = replace(cfg, generate_stream=replace(
        cfg.generate_stream, max_attempts_per_slot=5))
    assert cfg.generate_stream.max_attempts_per_slot == 5

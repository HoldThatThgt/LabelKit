"""v1.17 compile_scenario 测试（SPEC-SP §6/§7/§8；含 §13.2 planner scale gate）。

scale gate 默认必跑（v1.17 Wave 4b 移除 ``@pytest.mark.scale`` 标记——
``scale`` 未在 pyproject markers 注册，留标记会产生
benign unknown-mark 警告，由后续波注册。
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import labelkit.common.runtime.scenario.diagnostics as diagnostics
from labelkit.common.runtime.scenario import (
    FrameClassDomain,
    FrameWindowSpec,
    NoiseClassSpec,
    PlannerBudgetError,
    PlannerInfeasibleError,
    QuotaSpec,
    ScenarioConfig,
    ScheduleSpec,
    SequenceClassDomain,
    TierDomain,
    compile_scenario,
    derive_stream_bounds,
    half_even_noise_target,
    local_date,
    solve_quota_targets,
    static_class_targets,
)

_TZ = timezone(timedelta(hours=8))
_DAY = 86_400_000_000


def _us(day: str, hour: int, minute: int = 0) -> int:
    """本地 +08:00 时刻的绝对微秒。"""
    moment = datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00+08:00")
    return int(moment.timestamp()) * 1_000_000


def _config(*, seed: int = 7, start_day: str = "2026-01-05", days: int = 1,
            exclude: tuple[str, ...] = (), quotas=None, crossed: int = 0,
            classes=None, frame_classes=None, gap: tuple[int, int] = (5, 60),
            session_gap_s: int = 60, max_len: int = 6, max_span_s=None,
            noise_ratio: str = "0", duplicates: int = 0) -> ScenarioConfig:
    """组装测试用 ScenarioConfig（缺省双类双帧的小形）。"""
    schedule = ScheduleSpec(
        _us(start_day, 0), _us(start_day, 0) + days * _DAY, 480, exclude)
    if quotas is None:
        quotas = (QuotaSpec(
            name="mix", period="schedule", of_week=(),
            counts=(("mail", 1), ("commute", 1)), total=None, weights=(),
            allocation=None),)
    if classes is None:
        classes = (SequenceClassDomain("mail", (1, 3), (), (), ()),
                   SequenceClassDomain("commute", (2, 2), (), (), ()))
    if frame_classes is None:
        frame_classes = (FrameClassDomain("open_app", None, ()),
                         FrameClassDomain("view", None, ()),
                         FrameClassDomain("notify", None, ()),
                         FrameClassDomain("ping", None, ()))
    return ScenarioConfig(
        seed=seed, schedule=schedule, quotas=quotas, sequence_classes=classes,
        frame_classes=frame_classes, sequence_rules=(),
        crossed_sessions=crossed, frame_gap_us=(gap[0] * 1_000_000,
                                                gap[1] * 1_000_000),
        session_gap_us=session_gap_s * 1_000_000, session_max_len=max_len,
        session_max_span_us=None if max_span_s is None
        else max_span_s * 1_000_000,
        noise_ratio=Decimal(noise_ratio),
        noise_classes=(NoiseClassSpec("notify", 3), NoiseClassSpec("ping", 1)),
        duplicates=duplicates)


# ------------------------------------------------------ 基础行为 ----

def test_same_config_same_digest_and_stats():
    """同 config 两次 build：plan_digest 与 family stats 逐项相同。"""
    cfg = _config()
    first, second = compile_scenario(cfg), compile_scenario(cfg)
    assert first.plan_digest == second.plan_digest
    assert first.plan_digest.startswith("sha256:")
    assert dict(first.models) == dict(second.models)
    assert first.objectives == second.objectives


def test_different_seed_different_digest():
    """不同 seed 改变 length 抽签 → digest 不同（4 slot × 100 值域碰撞率 1e-8）。"""
    wide = (SequenceClassDomain("mail", (1, 100), (), (), ()),
            SequenceClassDomain("commute", (1, 100), (), (), ()))
    quotas = (QuotaSpec(name="four", period="schedule", of_week=(),
                        counts=(("mail", 2), ("commute", 2)), total=None,
                        weights=(), allocation=None),)
    first = compile_scenario(_config(seed=7, classes=wide, quotas=quotas,
                                     max_len=105))
    second = compile_scenario(_config(seed=8, classes=wide, quotas=quotas,
                                      max_len=105))
    lengths_first = [slot.length_target for slot in first.slots]
    lengths_second = [slot.length_target for slot in second.slots]
    assert lengths_first != lengths_second
    assert first.plan_digest != second.plan_digest


def test_day_quota_anchors_on_monday():
    """day quota mon ×2 类各 1 → N=2，两 slot anchor 都落周一。"""
    quotas = (QuotaSpec(
        name="monday_pair", period="day", of_week=(1,),
        counts=(("mail", 1), ("commute", 1)), total=None, weights=(),
        allocation=None),)
    plan = compile_scenario(_config(quotas=quotas))
    assert [s.key for s in plan.slots] == ["sequence:mail:0",
                                           "sequence:commute:0"]
    assert all(layout.anchor_date == "2026-01-05" for layout in plan.layouts)


def test_single_day_all_frames_within_schedule():
    """单日 schedule：全部帧 ts ∈ [start, end)。"""
    cfg = _config()
    plan = compile_scenario(cfg)
    start, end = cfg.schedule.start_us, cfg.schedule.end_us
    for layout in plan.layouts:
        assert all(start <= frame.start_us < end for frame in layout.frames)
        assert all(frame.end_us == frame.start_us for frame in layout.frames)
        stamps = [frame.start_us for frame in layout.frames]
        assert stamps == sorted(stamps)
        for left, right in zip(stamps, stamps[1:]):
            assert 5_000_000 <= right - left <= 60_000_000


def test_zero_crossing_family_zero_and_families_present():
    """D=0：crossing family 恒 0/0；十个 timeline 族全部在场。"""
    plan = compile_scenario(_config())
    families = dict(plan.models["timeline"].families)
    assert families["crossing"].variables == 0
    assert families["crossing"].constraints == 0
    expected = {"frame_domain", "frame_rule", "frame_window", "session_slot",
                "crossing", "quota_period", "sequence_rule", "resource",
                "noise_reserve", "objective"}
    assert set(families) == expected
    assert plan.models["quota"].families.keys() >= {
        "quota_domain", "quota_row", "objective"}


def test_objectives_single_day_calendar_span_one():
    """E2E-45 绿锚：单日工程 calendar_days_spanned == 1；L1 恒 0。"""
    plan = compile_scenario(_config())
    assert plan.objectives.preference_deviation == 0
    assert plan.objectives.calendar_days_spanned == 1
    assert 0 < plan.objectives.timeline_end_us < _DAY


def test_length_draw_order_frozen_by_plan_order():
    """scenario.preference 流按 slot 声明序消费：追加零 target 类不改变抽签。"""
    wide = (SequenceClassDomain("mail", (1, 100), (), (), ()),)
    extra = wide + (SequenceClassDomain("other", (1, 100), (), (), ()),)
    quotas = (QuotaSpec(name="only_mail", period="schedule", of_week=(),
                        counts=(("mail", 3),), total=None, weights=(),
                        allocation=None),)
    base = compile_scenario(_config(classes=wide, quotas=quotas, max_len=105))
    shifted = compile_scenario(_config(classes=extra, quotas=quotas,
                                       max_len=105))
    assert [s.length_target for s in base.slots] == \
           [s.length_target for s in shifted.slots]
    assert all(s.sequence_class == "mail" for s in shifted.slots)


def test_tier_slots_word_composition():
    """tier 分配走最大余额：ordinal 按 rank 升序分块、word 恰为档内构成。"""
    tiers = (TierDomain(1, 1, ("open_app",)), TierDomain(2, 3, ("open_app",
                                                                "view")))
    classes = (SequenceClassDomain("mail", (2, 2), tiers, (), ()),)
    quotas = (QuotaSpec(name="tiers", period="schedule", of_week=(),
                        counts=(("mail", 3),), total=None, weights=(),
                        allocation=None),)
    plan = compile_scenario(_config(classes=classes, quotas=quotas))
    assert [s.tier_rank for s in plan.slots] == [1, 2, 2]
    words = [{frame.frame_class for frame in layout.frames}
             for layout in plan.layouts]
    assert words[0] == {"open_app"}
    assert words[1] == {"open_app", "view"}
    assert words[2] == {"open_app", "view"}


def test_duration_interval_decode_and_resource_stats():
    """duration class 解码半开 interval/target/resource，resource family 记录实际建模。"""
    frames = (FrameClassDomain("open_app", (10, 10), ("audio_focus",)),
              FrameClassDomain("notify", None, ()),
              FrameClassDomain("ping", None, ()))
    tiers = (TierDomain(1, 1, ("open_app",)),)
    classes = (SequenceClassDomain("mail", (1, 1), tiers, (), ()),)
    quotas = (QuotaSpec(name="one", period="schedule", of_week=(),
                        counts=(("mail", 1),), total=None, weights=(),
                        allocation=None),)
    plan = compile_scenario(_config(classes=classes, quotas=quotas,
                                    frame_classes=frames, max_len=3))
    frame = plan.layouts[0].frames[0]
    assert frame.end_us - frame.start_us == 10
    assert frame.duration_target_us == 10
    assert frame.resources == ("audio_focus",)
    resource = plan.models["timeline"].families["resource"]
    assert resource.variables > 0 and resource.constraints > 0


def test_resource_intervals_forbid_overlap_across_sequences():
    """相同 resource 的两条固定 duration frame 在紧窗口内无可重叠解。"""
    frames = (FrameClassDomain("open_app", (10, 10), ("audio_focus",)),
              FrameClassDomain("notify", None, ()),
              FrameClassDomain("ping", None, ()))
    tiers = (TierDomain(1, 1, ("open_app",)),)
    classes = (SequenceClassDomain("mail", (1, 1), tiers, (), ()),)
    quotas = (QuotaSpec(name="two", period="schedule", of_week=(),
                        counts=(("mail", 2),), total=None, weights=(),
                        allocation=None),)
    base = _config(classes=classes, quotas=quotas, frame_classes=frames,
                   max_len=3, session_gap_s=0)
    cfg = replace(base, session_gap_us=1,
                  schedule=ScheduleSpec(base.schedule.start_us,
                                        base.schedule.start_us + 19, 480, ()))
    with pytest.raises(PlannerInfeasibleError):
        compile_scenario(cfg)


def test_duration_targets_are_seeded_and_digest_stable():
    """duration target 由 preference 流冻结：同 seed digest 稳定，不同 seed 改变 target。"""
    frames = (FrameClassDomain("open_app", (10, 100), ()),
              FrameClassDomain("notify", None, ()),
              FrameClassDomain("ping", None, ()))
    tiers = (TierDomain(1, 1, ("open_app",)),)
    classes = (SequenceClassDomain("mail", (1, 1), tiers, (), ()),)
    quotas = (QuotaSpec(name="one", period="schedule", of_week=(),
                        counts=(("mail", 1),), total=None, weights=(),
                        allocation=None),)
    first = compile_scenario(_config(seed=7, classes=classes, quotas=quotas,
                                     frame_classes=frames, max_len=3))
    again = compile_scenario(_config(seed=7, classes=classes, quotas=quotas,
                                     frame_classes=frames, max_len=3))
    second = compile_scenario(_config(seed=8, classes=classes, quotas=quotas,
                                      frame_classes=frames, max_len=3))
    assert first.plan_digest == again.plan_digest
    assert first.layouts[0].frames[0].duration_target_us == \
           again.layouts[0].frames[0].duration_target_us
    assert first.layouts[0].frames[0].duration_target_us != \
           second.layouts[0].frames[0].duration_target_us


# ------------------------------------------------------ noise 与 duplicate ----

def test_noise_slots_interior_and_class_weights():
    """noise 严格落在 session 首尾之间且总数恰为 half-even target。"""
    plan = compile_scenario(_config(noise_ratio="0.5", max_len=8))
    spans = {row.index: (row.start_us, row.last_point_us)
             for row in plan.sessions}
    for slot in plan.noise_slots:
        lo, hi = spans[slot.session_index]
        assert lo < slot.timestamp_us < hi
    total = sum(row.noise_count for row in plan.sessions)
    planned = sum(slot.length_target for slot in plan.slots)
    assert 3 <= planned <= 5
    assert total == half_even_noise_target(Decimal("0.5"), planned)
    assert len(plan.noise_slots) == total


def test_duplicates_layout_frozen():
    """duplicate：source 选择种子确定、offset>0、session_index 从 S 起、整体平移。"""
    cfg = _config(duplicates=1)
    plan = compile_scenario(cfg)
    again = compile_scenario(cfg)
    assert [d.source_slot_key for d in plan.duplicates] == \
           [d.source_slot_key for d in again.duplicates]
    duplicate = plan.duplicates[0]
    source = next(layout for layout in plan.layouts
                  if layout.slot_key == duplicate.source_slot_key)
    sessions = len(plan.slots) - cfg.crossed_sessions
    assert duplicate.session_index == sessions
    assert duplicate.ordinal == 0
    assert duplicate.offset_us > 0
    assert duplicate.frames[-1].start_us < cfg.schedule.end_us
    assert [f.frame_class for f in duplicate.frames] == \
           [f.frame_class for f in source.frames]
    assert all(frame.start_us == origin.start_us + duplicate.offset_us
               for frame, origin in zip(duplicate.frames, source.frames,
                                        strict=True))
    assert len(plan.sessions) == sessions  # duplicate 不生成 SessionLayout


# ------------------------------------------------------ sequence rules ----

@pytest.mark.parametrize("template", ("precedence", "response", "succession"))
def test_sequence_rule_templates_change_plan_feasibility(template):
    """四模板正向规则进入 CP 模型并能改变可行性。"""
    rule = __import__("labelkit.common.runtime.scenario", fromlist=["SequenceRuleSpec"]).SequenceRuleSpec(
        f"r_{template}", template, "mail", "commute", "schedule",
        gap_us=(0, 120 * 1_000_000))
    feasible = replace(_config(), sequence_rules=(rule,))
    plan = compile_scenario(feasible)
    assert plan.models["timeline"].families["sequence_rule"].constraints > 0
    impossible = replace(feasible, sequence_rules=(replace(rule, gap_us=(0, 1)),))
    with pytest.raises(PlannerInfeasibleError, match=rule.name):
        compile_scenario(impossible)


def test_sequence_rule_not_co_existence_changes_plan_feasibility():
    """not_co_existence 禁止同 bucket 的两类 occurrence 共存。"""
    from labelkit.common.runtime.scenario import SequenceRuleSpec
    rule = SequenceRuleSpec("exclusive", "not_co_existence", "mail", "commute", "day")
    with pytest.raises(PlannerInfeasibleError, match="exclusive"):
        compile_scenario(replace(_config(), sequence_rules=(rule,)))


def test_sequence_rule_period_bucket_allows_cross_day_occurrences():
    """day bucket 隔离：跨日 occurrence 不互作 witness 或冲突。"""
    from labelkit.common.runtime.scenario import SequenceRuleSpec
    rule = SequenceRuleSpec("daily", "not_co_existence", "mail", "commute", "day")
    quotas = (QuotaSpec(name="days", period="schedule", of_week=(),
                        counts=(("mail", 1), ("commute", 1)), total=None,
                        weights=(), allocation=None),)
    cfg = _config(days=2, quotas=quotas, session_gap_s=60)
    plan = compile_scenario(replace(cfg, sequence_rules=(rule,)))
    assert len(plan.layouts) == 2
    assert {layout.anchor_date for layout in plan.layouts} == {
        "2026-01-05", "2026-01-06"}


# ------------------------------------------------------ 诊断与错误面 ----

def test_static_infeasible_quota_on_excluded_day():
    """矛盾 quota：目标 weekday 被 schedule 排除 → PlannerInfeasibleError 含 quota 名。

    week 形态下该周 bucket 在场但无合法日（day 形态的 bucket 会整体消失，
    静态点名走 bucket-在场路径）。
    """
    quotas = (QuotaSpec(
        name="tuesday_only", period="week", of_week=(2,),
        counts=(("mail", 1),), total=None, weights=(), allocation=None),)
    cfg = _config(quotas=quotas, exclude=("2026-01-06",),
                  start_day="2026-01-05", days=2)
    with pytest.raises(PlannerInfeasibleError, match="tuesday_only"):
        compile_scenario(cfg)


def test_derive_stream_bounds_aggregates_without_short_circuit():
    """多错误同轮聚合：crossed 超限与 gap/span 关联错误同时出现。"""
    errors = derive_stream_bounds(_config(crossed=2))
    assert any("crossed_sessions" in text for text in errors)
    errors = derive_stream_bounds(_config(session_gap_s=86_400))
    assert any("session_gap_us" in text or "schedule provides" in text
               for text in errors)
    assert derive_stream_bounds(_config()) == []


def test_derive_stream_bounds_reference_and_form_errors():
    """引用域 / noise 域 / weights exact 兼容与排除日越界的聚合分支。"""
    quotas = (QuotaSpec(
        name="unknown_class", period="schedule", of_week=(),
        counts=(("ghost", 1),), total=None, weights=(), allocation=None),)
    errors = derive_stream_bounds(_config(quotas=quotas))
    assert any("unknown sequence class 'ghost'" in text for text in errors)
    weights = (QuotaSpec(
        name="ratio60_25_15", period="schedule", of_week=(), counts=(),
        total=9, weights=(("mail", 60), ("commute", 40)),
        allocation="exact"),)
    errors = derive_stream_bounds(_config(quotas=weights))
    assert any("minimum exact cohort" in text and "nearest multiples"
               in text for text in errors)
    errors = derive_stream_bounds(_config(exclude=("2026-01-10",)))
    assert any("outside the schedule local date span" in text
               for text in errors)
    errors = derive_stream_bounds(_config(noise_ratio="1.5"))
    assert any("noise_ratio must satisfy" in text for text in errors)


def test_budget_error_when_deterministic_time_capped(monkeypatch):
    """紧 budget 下非 OPTIMAL → PlannerBudgetError（monkeypatch 确定性构造）。

    该形态在完整 10.0 dtime 下 3.6s 内 OPTIMAL，压缩到 0.05 后稳定停在
    非 OPTIMAL——测试的是 budget 分支本身，不是不可解配置。
    """
    monkeypatch.setattr(diagnostics, "SOLVE_DETERMINISTIC_TIME", 0.05)
    classes = (SequenceClassDomain("short", (2, 2), (), (), ()),
               SequenceClassDomain("long", (15, 15), (), (), ()))
    quotas = (QuotaSpec(name="mix", period="schedule", of_week=(),
                        counts=(("short", 60), ("long", 4)), total=None,
                        weights=(), allocation=None),)
    cfg = _config(classes=classes, quotas=quotas, max_len=23)
    with pytest.raises(PlannerBudgetError, match="model=timeline"):
        compile_scenario(cfg)


def test_quota_model_targets_match_static():
    """quota 模型最优 target 与静态逐类和一致（counts 形与 weights 形）。"""
    cfg = _config()
    solution = solve_quota_targets(cfg.quotas, cfg.schedule, cfg.seed)
    static = {name: value for quota in cfg.quotas
              for name, value in static_class_targets(quota, cfg.schedule)}
    assert dict(solution.class_targets) == static
    weights = (QuotaSpec(
        name="weighted", period="schedule", of_week=(), counts=(),
        total=20, weights=(("mail", 60), ("commute", 40)),
        allocation="largest_remainder"),)
    weighted = _config(quotas=weights)
    solution = solve_quota_targets(weighted.quotas, weighted.schedule,
                                   weighted.seed)
    assert dict(solution.class_targets) == {"mail": 12, "commute": 8}


def test_crossed_sessions_decode_and_interleave():
    """D=1：secondary 恰 1 条、crossed session 双 owner、交错前提由解码核验。"""
    classes = (SequenceClassDomain("main", (2, 2), (), (), ()),)
    quotas = (QuotaSpec(name="mix", period="schedule", of_week=(),
                        counts=(("main", 4),), total=None, weights=(),
                        allocation=None),)
    cfg = _config(classes=classes, quotas=quotas, crossed=1, max_len=8)
    plan = compile_scenario(cfg)
    assert cfg.crossed_sessions == 1
    roles = [layout.owner_role for layout in plan.layouts]
    assert roles.count("secondary") == 1
    crossed = [row for row in plan.sessions if row.secondary_slot_key]
    assert len(crossed) == 1
    crossed_session = crossed[0]
    primary = next(layout for layout in plan.layouts
                   if layout.slot_key == crossed_session.primary_slot_key)
    secondary = next(layout for layout in plan.layouts
                     if layout.slot_key == crossed_session.secondary_slot_key)
    primary_stamps = [f.start_us for f in primary.frames]
    secondary_stamps = [f.start_us for f in secondary.frames]
    interleaved = (min(primary_stamps) < secondary_stamps[-1]
                   and secondary_stamps[0] < max(primary_stamps)) or \
                  (min(secondary_stamps) < primary_stamps[-1]
                   and primary_stamps[0] < max(secondary_stamps))
    assert interleaved
    crossing = plan.models["timeline"].families["crossing"]
    assert crossing.variables > 0 and crossing.constraints > 0


def test_frame_window_constrains_task_and_duplicate_frames():
    """frame window：word==受约束类的帧落允许日段；duplicate 按新时间重执行。

    用 tier 构成把 view 档强制进 word（自由 word 下 solver 会完全避开窗口类）。
    """
    windows = (FrameWindowSpec(name="view_hours", frame_class="view",
                               of_day_us=((10 * 3600 * 1_000_000,
                                           11 * 3600 * 1_000_000),),
                               of_week=()),)
    tiers = (TierDomain(1, 1, ("open_app",)), TierDomain(2, 1, ("view",)))
    classes = (SequenceClassDomain("mail", (2, 2), tiers, (), windows),)
    quotas = (QuotaSpec(name="two", period="schedule", of_week=(),
                        counts=(("mail", 2),), total=None, weights=(),
                        allocation=None),)
    cfg = _config(classes=classes, quotas=quotas, max_len=8, duplicates=1)
    plan = compile_scenario(cfg)
    base = cfg.schedule.start_us
    for layout in list(plan.layouts) + [d for d in plan.duplicates]:
        for frame in layout.frames:
            if frame.frame_class == "view":
                offset = frame.start_us - base
                assert 10 * 3600 * 1_000_000 <= offset < 11 * 3600 * 1_000_000
    assert any(frame.frame_class == "view"
               for layout in plan.layouts for frame in layout.frames)
    family = plan.models["timeline"].families["frame_window"]
    assert family.constraints > 0


def test_duration_frames_decode_interval_end_and_preference_target():
    """duration frame 解码真实 end=start+duration，并保留 seeded target。"""
    frames = (FrameClassDomain("app", (10_000_000, 20_000_000), ()),
              FrameClassDomain("view", None, ()),
              FrameClassDomain("notify", None, ()),
              FrameClassDomain("ping", None, ()))
    tiers = (TierDomain(1, 1, ("app",)),)
    classes = (SequenceClassDomain("mail", (2, 2), tiers, (), ()),)
    quotas = (QuotaSpec(name="duration", period="schedule", of_week=(),
                        counts=(("mail", 1),), total=None, weights=(),
                        allocation=None),)
    plan = compile_scenario(_config(classes=classes, frame_classes=frames,
                                    quotas=quotas, gap=(30, 60)))
    layout = plan.layouts[0]
    assert all(frame.frame_class == "app" for frame in layout.frames)
    assert all(10_000_000 <= frame.end_us - frame.start_us <= 20_000_000
               for frame in layout.frames)
    assert all(frame.duration_target_us == frame.end_us - frame.start_us
               for frame in layout.frames)
    assert plan.objectives.preference_deviation == 0


def test_resource_intervals_are_globally_non_overlapping_and_duplicate_shifted():
    """同名 resource 跨 sequence 与 duplicate 均由全局 interval 互斥。"""
    frames = (FrameClassDomain("app", (20_000_000, 20_000_000), ("audio",)),
              FrameClassDomain("view", None, ()),
              FrameClassDomain("notify", None, ()),
              FrameClassDomain("ping", None, ()))
    tiers = (TierDomain(1, 1, ("app",)),)
    classes = (SequenceClassDomain("mail", (1, 1), tiers, (), ()),)
    quotas = (QuotaSpec(name="resource", period="schedule", of_week=(),
                        counts=(("mail", 2),), total=None, weights=(),
                        allocation=None),)
    plan = compile_scenario(_config(classes=classes, frame_classes=frames,
                                    quotas=quotas, duplicates=1, gap=(1, 2),
                                    session_gap_s=1, max_len=2))
    intervals = [(frame.start_us, frame.end_us)
                 for layout in plan.layouts for frame in layout.frames]
    intervals += [(frame.start_us, frame.end_us)
                  for duplicate in plan.duplicates for frame in duplicate.frames]
    assert all(left[1] <= right[0] or right[1] <= left[0]
               for index, left in enumerate(intervals)
               for right in intervals[index + 1:])
    duplicate = plan.duplicates[0]
    source = plan.layouts[0]
    assert duplicate.frames[0].end_us - duplicate.frames[0].start_us == 20_000_000
    assert duplicate.frames[0].start_us == source.frames[0].start_us + duplicate.offset_us


def test_excluded_day_hole_is_respected():
    """两日 schedule 排除次日：task/noise/duplicate 都不占用排除日。"""
    from datetime import date

    cfg = _config(days=2, exclude=("2026-01-06",), noise_ratio="0.5",
                  max_len=8, duplicates=1)
    plan = compile_scenario(cfg)
    excluded = {date.fromisoformat("2026-01-06")}
    stamps = [frame.start_us for layout in plan.layouts
              for frame in layout.frames]
    stamps += [frame.start_us for dup in plan.duplicates
               for frame in dup.frames]
    stamps += [slot.timestamp_us for slot in plan.noise_slots]
    days = {local_date(ts, cfg.schedule.utc_offset_minutes) for ts in stamps}
    assert not (days & excluded)


# ------------------------------------------------------ §13.2 scale gate ----

def _gate_config(short_target: int, short_len: int, long_target: int,
                 long_len: int, crossed: int = 0, max_len: int = 0):
    """五点曲线/crossing 组的工程形配置（Σ task frames = 180）。"""
    classes = (SequenceClassDomain("short", (short_len, short_len), (), (), ()),
               SequenceClassDomain("long", (long_len, long_len), (), (), ()))
    quotas = (QuotaSpec(
        name="mix", period="schedule", of_week=(),
        counts=(("short", short_target), ("long", long_target)), total=None,
        weights=(), allocation=None),)
    return _config(seed=11, classes=classes, quotas=quotas, crossed=crossed,
                   max_len=max_len or long_len + 8, noise_ratio="0.1",
                   duplicates=0)


GATE_POINTS = ((4, 22, 4, 23), (28, 5, 4, 10), (60, 2, 4, 15),
               (124, 1, 4, 14), (154, 1, 13, 2))


@pytest.mark.parametrize("short_target,short_len,long_target,long_len",
                         GATE_POINTS)
def test_scale_gate_curve(short_target, short_len, long_target, long_len):
    """五点曲线（8/32/64/128/167，Σ=180 帧）：entries<250k、零 crossing 族 0、
    每层在冻结 budget 内 OPTIMAL（compile 成功即证）。"""
    cfg = _gate_config(short_target, short_len, long_target, long_len)
    plan = compile_scenario(cfg)
    timeline = plan.models["timeline"]
    entries = timeline.variables + timeline.constraints
    assert entries < 250_000
    quota_entries = (plan.models["quota"].variables
                     + plan.models["quota"].constraints)
    assert quota_entries < 250_000
    assert timeline.families["crossing"].variables == 0
    assert timeline.families["crossing"].constraints == 0
    assert sum(slot.length_target for slot in plan.slots) == 180
    assert len(plan.noise_slots) == 18
    assert plan.objectives.calendar_days_spanned == 1


def test_scale_gate_digest_stability_at_167():
    """167 点两次 build：family stats 与 plan digest 相同（§13.2 门槛）。"""
    cfg = _gate_config(154, 1, 13, 2)
    first, second = compile_scenario(cfg), compile_scenario(cfg)
    assert first.plan_digest == second.plan_digest
    assert dict(first.models) == dict(second.models)


def test_scale_gate_crossing_entries_grow_linearly():
    """crossing 组 D=1 与 D=floor(N/4)：crossing entries 随 crossed 数线性。

    16/D=4 与 32/D=8 两点比值锚：线性约 2×，平方（owner 对）约 4×。
    """
    small = compile_scenario(_gate_config(16, 2, 0, 2, crossed=4, max_len=8))
    large = compile_scenario(_gate_config(32, 2, 0, 2, crossed=8, max_len=8))
    single = compile_scenario(_gate_config(4, 2, 0, 2, crossed=1, max_len=8))
    small_family = small.models["timeline"].families["crossing"]
    large_family = large.models["timeline"].families["crossing"]
    single_family = single.models["timeline"].families["crossing"]
    assert single_family.variables > 0
    ratio = (large_family.variables + large_family.constraints) / \
            (small_family.variables + small_family.constraints)
    assert 1.5 <= ratio <= 3.0

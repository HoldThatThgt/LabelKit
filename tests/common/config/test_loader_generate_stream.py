"""Offline unit tests for the v1.17 scenario-planning time-stream form in M1
(labelkit/common/config/loader.py; dev spec SPEC-scenario-planning.md §4/§6.2).

Covers the whole M1 constraint table row by row (positive AND negative): the
form premise conjunction, the ten deleted keys' directed errors, the quota
dual-form gates and the rule-71 class domain, the schedule cluster, the noise
table domain, the frame_rules/frame_windows rename (v1.16 semantics + the
shared natural-name domain), the v1.14 time-field bindings, the v1.15 tier
tables, the ``--limit`` mutual exclusion, the parked-section waivers and the
``ResolvedConfig.scenario_plan`` assembly (slots/sessions/digest determinism,
planner exception lanes). Pure config logic + the real CP-SAT compile — zero
LLM.
"""
from __future__ import annotations

import pytest

from labelkit.common.errors import ConfigError, InternalError
from tests.common.config.test_config import (  # noqa: F401 (env is a fixture)
    BASE_CONFIG,
    HOOK_PY,
    SCHEMA,
    Env,
    env,
    has,
)

# ── the canonical v1.17 time-stream project body (mirrors examples/synth-stream) ──

GS_STREAM = """\
[stream]
order_by = "meta:ts"
gap_s = 900
session_max_len = 12
"""

GS_CLASSIFY = """\
[classify]
enabled = true

[[classify.classes]]
name = "ticket_booking"
description = "高铁购票任务序列"

[[classify.classes]]
name = "smart_home"
description = "智能家居指令序列"
"""

GS_GENERATE = """\
[generate]
enabled = true

[generate.stream]
enabled = true
crossed_sessions = 0
noise_ratio = 0.1
duplicates = 1
frame_gap_s = [5, 60]
max_attempts_per_slot = 3

[generate.stream.schedule]
start = "2026-01-05T09:00:00+08:00"
end = "2026-01-06T20:00:00+08:00"

[[generate.stream.quotas]]
name = "daily_mix"
period = "schedule"
counts = { ticket_booking = 2, smart_home = 1 }

[[generate.stream.noise]]
frame_class = "chatter"
weight = 1

[class.ticket_booking.generate]
instruction = "生成一段高铁购票的用户请求序列"
len_range = [3, 5]

[class.smart_home.generate]
instruction = "生成一段智能家居指令序列"
len_range = [3, 5]
"""

GS_FRAMES = """\
[[frame.classify.classes]]
name = "task_request"
description = "发起购票任务的首帧请求"

[[frame.classify.classes]]
name = "followup"
description = "补充出发地与日期等信息"

[[frame.classify.classes]]
name = "chatter"
description = "与任务无关的闲聊干扰"

[frame.class.task_request.generate]
instruction = "生成一条发起购票任务的用户话语"

[frame.class.followup.generate]
instruction = "生成一条补充信息的用户话语"

[frame.class.chatter.generate]
instruction = "生成一条与任务无关的闲聊消息"
"""

GS_BODY = "\n".join((GS_STREAM, GS_CLASSIFY, GS_GENERATE, GS_FRAMES))


def gs_body(*, stream=GS_STREAM, classify=GS_CLASSIFY, generate=GS_GENERATE,
            frames=GS_FRAMES) -> str:
    """The canonical body with one part swapped out (每条约束反例只改一处)。"""
    return "\n".join((stream, classify, generate, frames))


FORM_OFF = """\
[generate]
enabled = true
instruction = "生成样本"
standalone_count = 1

[classify]
enabled = false

[[classify.classes]]
name = "ticket_booking"
description = "高铁购票任务序列"
"""


def gs_project(env: Env, body: str = GS_BODY, **kw) -> str:
    kw.setdefault("run_extra", 'mode = "generate_only"')
    return env.project(input_path=None, body=body, **kw)


def plan_of(env: Env, body: str = GS_BODY):
    """加载并返回冻结的 scenario_plan（happy-path 专用）。"""
    cfg = env.load(project_text=gs_project(env, body))
    assert cfg.scenario_plan is not None
    return cfg.scenario_plan


# ── happy path: parse products + frozen plan ─────────────────────────────────


def test_happy_path_parses_form_and_freezes_plan(env):
    plan = plan_of(env)
    gs_keys = {slot.sequence_class for slot in plan.slots}
    assert gs_keys == {"ticket_booking", "smart_home"}
    assert [slot.key for slot in plan.slots if
            slot.sequence_class == "ticket_booking"] == [
        "sequence:ticket_booking:0", "sequence:ticket_booking:1"]
    assert all(slot.length_range == (3, 5) for slot in plan.slots)
    assert len(plan.sessions) == len(plan.slots)          # crossed_sessions = 0
    assert len(plan.duplicates) == 1
    assert plan.noise_slots                              # noise_ratio = 0.1
    assert plan.plan_digest.startswith("sha256:")
    assert {row.name for row in plan.quota_summary} == {"daily_mix"}


def test_plan_digest_is_seed_stable_and_seed_sensitive(env):
    body_a = gs_project(env)
    cfg_a = env.load(project_text=body_a)
    cfg_b = env.load(project_text=body_a)
    assert cfg_a.scenario_plan.plan_digest == cfg_b.scenario_plan.plan_digest
    changed = gs_project(env, run_extra='mode = "generate_only"\nseed = 20260814')
    cfg_c = env.load(project_text=changed)
    assert cfg_c.scenario_plan.plan_digest != cfg_a.scenario_plan.plan_digest


def test_form_off_has_no_plan_and_keeps_v1_12_equivalence(env):
    cfg = env.load(project_text=gs_project(env, "\n".join((GS_STREAM, FORM_OFF))))
    assert cfg.scenario_plan is None
    assert cfg.generate_stream.enabled is False
    assert cfg.generate_stream.quotas == ()
    assert cfg.generate_stream.schedule is None
    assert cfg.generate_stream.noise_classes == ()
    assert cfg.generate_stream.crossed_sessions == 0
    assert cfg.generate_stream.max_attempts_per_slot == 3


# ── premise conjunction (spec 2.3.1) ────────────────────────────────────────


def test_premise_requires_generate_only(env):
    cfg_text = gs_project(env, run_extra='mode = "process"')
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=cfg_text)
    has(ei.value.errors, "the time-stream form requires run.mode")


def test_premise_requires_text_modality(env):
    text = gs_project(env, modality="ui")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=text)
    has(ei.value.errors, "requires run.modality")


def test_premise_requires_generate_enabled(env):
    body = gs_body(generate=GS_GENERATE.replace("enabled = true", "enabled = false", 1))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "requires generate.enabled = true")


def test_premise_requires_classify_enabled(env):
    body = gs_body(classify=GS_CLASSIFY.replace("enabled = true", "enabled = false"))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "requires classify.enabled = true")


def test_premise_requires_meta_order_by(env):
    body = gs_body(stream=GS_STREAM.replace('order_by = "meta:ts"',
                                            'order_by = "input_order"'))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, 'requires "meta:<field>"')


def test_premise_rejects_dotted_artifact_field_names(env):
    body = gs_body(stream=GS_STREAM.replace('meta:ts', 'meta:meta.ts'))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "must not contain")


def test_premise_rejects_artifact_key_collisions(env):
    body = gs_body(stream=GS_STREAM.replace('meta:ts', 'meta:text'),
                   generate=GS_GENERATE,
                   frames=GS_FRAMES.replace('text_field = "text"', ''))
    body = body.replace('[input]\ntext_field = "text"', '[input]\ntext_field = "text"')
    text = gs_project(env, body)
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=text)
    has(ei.value.errors, "must not have the same name")


def test_premise_requires_meta_mode_not_none(env):
    text = gs_project(env, include_output=False) + (
        '[output]\nmeta_mode = "none"\nschema_inline = """{}"""\n')
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=text)
    has(ei.value.errors, 'must not be "none"')


def test_frame_switches_are_mutually_exclusive_with_the_form(env):
    body = gs_body(generate=GS_GENERATE
                   + '[frame.classify]\nenabled = true\n')
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "mutually exclusive with the time-stream form")


# ── the ten deleted keys (CONTRACTS §6.3 rule 62) ───────────────────────────


@pytest.mark.parametrize("line,fragment", [
    ("sessions = 2", "[generate.stream].sessions: this key was removed in v1.17"),
    ('ts_start = "2026-01-01T00:00:00Z"',
     "[generate.stream].ts_start: this key was removed in v1.17 - use "
     "[generate.stream].schedule"),
    ('noise_instruction = "闲聊"',
     "[generate.stream].noise_instruction: this key was removed in v1.17 - use "
     "[[generate.stream.noise]]"),
    ("[[generate.stream.rules]]\ntemplate = \"init\"\nframe_class = \"task_request\"",
     "[generate.stream].rules: this key was removed in v1.17 - use "
     "[[generate.stream.frame_rules]]"),
    ("[[generate.stream.windows]]\nframe_class = \"task_request\"\n"
     'of_day = [["08:00", "12:00"]]',
     "[generate.stream].windows: this key was removed in v1.17 - use "
     "[[generate.stream.frame_windows]]"),
])
def test_stream_side_deleted_keys_are_directed_errors(env, line, fragment):
    generate = GS_GENERATE.replace("max_attempts_per_slot = 3\n",
                                   f"max_attempts_per_slot = 3\n{line}\n")
    body = gs_body(generate=generate)
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, fragment)


def test_global_sequences_key_is_a_directed_error(env):
    body = gs_body(generate=GS_GENERATE.replace("[generate]\n",
                                                '[generate]\nsequences = 3\n'))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "[generate].sequences: this key was removed in v1.17 - "
                         "sequence quotas are carried by [[generate.stream.quotas]]")


@pytest.mark.parametrize("line,fragment", [
    ("sequences = 3", "[class.ticket_booking.generate].sequences: this key was "
                      "removed in v1.17"),
    ("[[class.ticket_booking.generate.rules]]\ntemplate = \"init\"\n"
     "frame_class = \"task_request\"",
     "[class.ticket_booking.generate].rules: this key was removed in v1.17 - use "
     "[[class.ticket_booking.generate.frame_rules]]"),
    ("[[class.ticket_booking.generate.windows]]\nframe_class = \"task_request\"\n"
     'of_day = [["08:00", "12:00"]]',
     "[class.ticket_booking.generate].windows: this key was removed in v1.17 - use "
     "[[class.ticket_booking.generate.frame_windows]]"),
])
def test_class_side_deleted_keys_are_directed_errors(env, line, fragment):
    generate = GS_GENERATE.replace(
        "instruction = \"生成一段高铁购票的用户请求序列\"",
        line, 1)
    body = gs_body(generate=generate)
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, fragment)


def test_limit_is_mutually_exclusive_with_the_form(env):
    from labelkit.common.config.model import CliOverrides
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env), cli=CliOverrides(limit=1))
    has(ei.value.errors, "mutually exclusive with --limit")


# ── forbidden flat-form keys inside the form ────────────────────────────────


@pytest.mark.parametrize("key", ["seed_examples", "standalone_count",
                                 "num_per_record", "seeds_per_call"])
def test_generate_forbidden_keys_are_directed_errors(env, key):
    body = gs_body(generate=GS_GENERATE.replace(
        "[generate]\n", f'[generate]\n{key} = 1\n'))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, f"[generate].{key}: the time-stream form does not "
                         f"provide this key")


def test_class_generate_forbidden_keys_are_directed_errors(env):
    body = gs_body(generate=GS_GENERATE.replace(
        "len_range = [3, 5]", "num_per_record = 2\nlen_range = [3, 5]", 1))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "the time-stream form does not provide this key")


# ── quotas (SPEC-SP §4.4 / CONTRACTS rules 65+71) ───────────────────────────


def test_quota_table_is_required(env):
    body = gs_body(generate=GS_GENERATE.replace(
        '[[generate.stream.quotas]]\nname = "daily_mix"\nperiod = "schedule"\n'
        'counts = { ticket_booking = 2, smart_home = 1 }\n', ""))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "requires at least one quota table")


def test_quota_all_zero_targets_are_rejected(env):
    body = gs_body(generate=GS_GENERATE.replace(
        "counts = { ticket_booking = 2, smart_home = 1 }",
        "counts = { ticket_booking = 0, smart_home = 0 }"))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "no feasible plan")


def test_quota_class_domain_runs_before_arithmetic(env):
    body = gs_body(generate=GS_GENERATE.replace(
        "counts = { ticket_booking = 2, smart_home = 1 }",
        "counts = { ticket_booking = 2, smart_home = 1, nonexistent = 1 }"))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "sequence class \"nonexistent\" is not in "
                         "[[classify.classes]]")


def test_quota_counts_form_rejects_weights_keys(env):
    body = gs_body(generate=GS_GENERATE.replace(
        "counts = { ticket_booking = 2, smart_home = 1 }",
        'counts = { ticket_booking = 2 }\ntotal = 4\n'))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "counts")


def test_quota_weights_form_requires_allocation_and_two_classes(env):
    body = gs_body(generate=GS_GENERATE.replace(
        '[[generate.stream.quotas]]\nname = "daily_mix"\nperiod = "schedule"\n'
        'counts = { ticket_booking = 2, smart_home = 1 }\n',
        '[[generate.stream.quotas]]\nname = "weighted_mix"\nperiod = "schedule"\n'
        'total = 3\nweights = { ticket_booking = 2, smart_home = 1 }\n'))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "requires allocation")


def test_quota_exact_total_not_a_cohort_multiple_reports_nearest(env):
    body = gs_body(generate=GS_GENERATE.replace(
        '[[generate.stream.quotas]]\nname = "daily_mix"\nperiod = "schedule"\n'
        'counts = { ticket_booking = 2, smart_home = 1 }\n',
        '[[generate.stream.quotas]]\nname = "exact_mix"\nperiod = "schedule"\n'
        'total = 5\nweights = { ticket_booking = 2, smart_home = 1 }\n'
        'allocation = "exact"\n'))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "minimum exact cohort")


def test_quota_name_must_match_the_natural_name_grammar(env):
    body = gs_body(generate=GS_GENERATE.replace('name = "daily_mix"',
                                                'name = "Daily Mix"'))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "expected a match of [a-z0-9_]+")


def test_quota_period_enum_is_closed(env):
    body = gs_body(generate=GS_GENERATE.replace('period = "schedule"',
                                                'period = "month"'))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "period")


# ── schedule (SPEC-SP §4.3 / CONTRACTS rule 64) ─────────────────────────────


def test_schedule_is_required_in_the_form(env):
    body = gs_body(generate=GS_GENERATE.replace(
        '[generate.stream.schedule]\nstart = "2026-01-05T09:00:00+08:00"\n'
        'end = "2026-01-06T20:00:00+08:00"\n', ""))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "[generate.stream.schedule]: required in the time-stream "
                         "form")


def test_schedule_requires_explicit_offsets(env):
    body = gs_body(generate=GS_GENERATE.replace(
        'start = "2026-01-05T09:00:00+08:00"', 'start = "2026-01-05T09:00:00"'))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "[generate.stream.schedule]")


def test_schedule_rejects_mismatched_offsets_and_bad_order(env):
    mismatched = gs_body(generate=GS_GENERATE.replace(
        'end = "2026-01-06T20:00:00+08:00"', 'end = "2026-01-06T20:00:00+09:00"'))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, mismatched))
    has(ei.value.errors, "[generate.stream.schedule]")
    reversed_ = gs_body(generate=GS_GENERATE.replace(
        'end = "2026-01-06T20:00:00+08:00"', 'end = "2026-01-05T08:00:00+08:00"'))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, reversed_))
    has(ei.value.errors, "[generate.stream.schedule]")


def test_schedule_excludes_out_of_range_dates_fail_fast(env):
    body = gs_body(generate=GS_GENERATE.replace(
        'end = "2026-01-06T20:00:00+08:00"',
        'end = "2026-01-06T20:00:00+08:00"\nexclude_dates = ["2026-01-09"]'))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "outside the schedule's local-date range")


def test_schedule_duplicate_exclusions_are_rejected(env):
    body = gs_body(generate=GS_GENERATE.replace(
        'end = "2026-01-06T20:00:00+08:00"',
        'end = "2026-01-06T20:00:00+08:00"\nexclude_dates = '
        '["2026-01-06", "2026-01-06"]'))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "[generate.stream.schedule]")


# ── noise table (SPEC-SP §4.8 / CONTRACTS rule 69) ──────────────────────────


def test_noise_ratio_requires_a_table(env):
    body = gs_body(generate=GS_GENERATE.replace(
        '[[generate.stream.noise]]\nframe_class = "chatter"\nweight = 1\n', ""))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "required when noise_ratio > 0")


def test_noise_table_at_zero_ratio_is_rejected(env):
    body = gs_body(generate=GS_GENERATE.replace("noise_ratio = 0.1",
                                                "noise_ratio = 0.0"))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "a noise table at noise_ratio = 0")


@pytest.mark.parametrize("ratio", [-0.1, 1.0, 2.0])
def test_noise_ratio_half_open_unit_interval(env, ratio):
    body = gs_body(generate=GS_GENERATE.replace("noise_ratio = 0.1",
                                                f"noise_ratio = {ratio}"))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "expected a number in [0,1)")


def test_noise_class_must_be_in_the_frame_table_with_instruction(env):
    body = gs_body(generate=GS_GENERATE.replace('frame_class = "chatter"',
                                                'frame_class = "absent"'))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "is not in [[frame.classify.classes]]")
    no_instruction = gs_body(frames=GS_FRAMES.replace(
        'instruction = "生成一条与任务无关的闲聊消息"', ""))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, no_instruction))
    has(ei.value.errors, "must provide a non-empty generation instruction")


def test_noise_class_must_not_appear_in_tiers_rules_or_windows(env):
    tiered = gs_body(generate=GS_GENERATE + """\
[[generate.stream.tiers]]
tier_rank = 1
weight = 1
frame_classes = ["task_request", "chatter"]
""")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, tiered))
    has(ei.value.errors, "must not appear in a tier composition")
    windowed = gs_body(generate=GS_GENERATE + """\
[[generate.stream.frame_windows]]
name = "chatter_hours"
frame_class = "chatter"
of_day = [["08:00", "12:00"]]
""")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, windowed))
    has(ei.value.errors, "must not appear in a frame window")


def test_task_candidate_domain_must_not_be_empty(env):
    generate = GS_GENERATE.replace(
        '[[generate.stream.noise]]\nframe_class = "chatter"\nweight = 1\n',
        '[[generate.stream.noise]]\nframe_class = "chatter"\nweight = 1\n'
        '[[generate.stream.noise]]\nframe_class = "task_request"\nweight = 1\n'
        '[[generate.stream.noise]]\nframe_class = "followup"\nweight = 1\n')
    body = gs_body(generate=generate)
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "candidate domain is empty")


# ── frame rules / frame windows (rule 66; v1.16 semantics renamed) ──────────


def test_frame_rules_parse_with_names_and_us_quantization(env):
    body = gs_body(generate=GS_GENERATE + """\
[[generate.stream.frame_rules]]
name = "request_first"
template = "init"
frame_class = "task_request"

[[generate.stream.frame_rules]]
name = "chain_rule"
template = "chain_response"
source = "task_request"
target = "followup"
time_s = [1.5, 2.5]
""")
    cfg = env.load(project_text=gs_project(env, body))
    rules = cfg.generate_stream.frame_rules
    assert [rule.name for rule in rules] == ["request_first", "chain_rule"]
    assert rules[1].time_us == (1_500_000, 2_500_000)
    assert rules[1].correlation is None


def test_frame_rule_name_is_required_and_unique_in_shared_domain(env):
    unnamed = gs_body(generate=GS_GENERATE + """\
[[generate.stream.frame_rules]]
template = "init"
frame_class = "task_request"
""")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, unnamed))
    has(ei.value.errors, "required")
    duplicated = gs_body(generate=GS_GENERATE + """\
[[generate.stream.frame_rules]]
name = "request_first"
template = "init"
frame_class = "task_request"

[[generate.stream.frame_rules]]
name = "request_first"
template = "end"
frame_class = "task_request"
""")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, duplicated))
    has(ei.value.errors, "shared name domain violation")


def test_quota_and_window_names_share_the_global_domain(env):
    body = gs_body(generate=GS_GENERATE + """\
[[generate.stream.frame_windows]]
name = "daily_mix"
frame_class = "task_request"
of_day = [["08:00", "12:00"]]
""")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "shared name domain violation")


def test_frame_rule_shape_matrix_is_enforced(env):
    binary_missing = gs_body(generate=GS_GENERATE + """\
[[generate.stream.frame_rules]]
name = "bad_binary"
template = "response"
source = "task_request"
""")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, binary_missing))
    has(ei.value.errors, "source and target are required")
    count_missing = gs_body(generate=GS_GENERATE + """\
[[generate.stream.frame_rules]]
name = "bad_count"
template = "exactly"
frame_class = "task_request"
""")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, count_missing))
    has(ei.value.errors, ".count: required")
    unary_extra = gs_body(generate=GS_GENERATE + """\
[[generate.stream.frame_rules]]
name = "bad_unary"
template = "init"
frame_class = "task_request"
source = "followup"
""")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, unary_extra))
    has(ei.value.errors, "only legal for binary templates")
    same_pair = gs_body(generate=GS_GENERATE + """\
[[generate.stream.frame_rules]]
name = "bad_pair"
template = "response"
source = "task_request"
target = "task_request"
""")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, same_pair))
    has(ei.value.errors, "must name different frame classes")


def test_frame_rule_unknown_frame_class_is_rejected(env):
    body = gs_body(generate=GS_GENERATE + """\
[[generate.stream.frame_rules]]
name = "unknown_target"
template = "response"
source = "task_request"
target = "absent"
""")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "is not in [[frame.classify.classes]]")


def test_frame_rule_correlation_operator_key_is_rejected(env):
    body = gs_body(generate=GS_GENERATE + """\
[[generate.stream.frame_rules]]
name = "corr_rule"
template = "response"
source = "task_request"
target = "followup"
correlation = { operator = "equal", source_field = "utterance", target_field = "utterance" }
""")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "v1.17 correlation is equal-only")


def test_frame_windows_require_names_and_same_day_branches(env):
    body = gs_body(generate=GS_GENERATE + """\
[[generate.stream.frame_windows]]
name = "crossing_midnight"
frame_class = "task_request"
of_day = [["22:00", "23:30"], ["23:00", "23:45"]]
""")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "branches must not overlap")
    unnamed = gs_body(generate=GS_GENERATE + """\
[[generate.stream.frame_windows]]
frame_class = "task_request"
of_day = [["08:00", "12:00"]]
""")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, unnamed))
    has(ei.value.errors, "required")


def test_per_class_frame_rules_and_windows_override_whole_tables(env):
    body = gs_body(generate=GS_GENERATE + """
[[class.ticket_booking.generate.frame_rules]]
name = "class_rule"
template = "init"
frame_class = "task_request"
""")
    cfg = env.load(project_text=gs_project(env, body))
    assert [r.name for r in cfg.class_views["ticket_booking"].frame_rules] == [
        "class_rule"]
    assert cfg.class_views["smart_home"].frame_rules is None  # 继承全局空表


def test_per_class_sequence_rules_are_assembled_with_three_state_semantics(env, monkeypatch):
    body = gs_body(generate=GS_GENERATE + """
[[generate.stream.sequence_rules]]
name = "global_rule"
template = "response"
source = "ticket_booking"
target = "smart_home"
period = "schedule"

[[class.ticket_booking.generate.sequence_rules]]
name = "class_rule"
template = "precedence"
source = "smart_home"
target = "ticket_booking"
period = "schedule"
""")
    import labelkit.common.config._generate_stream_constraints as gsc
    captured = {}
    real_compile = gsc.compile_scenario

    def capture(config):
        captured["config"] = config
        return real_compile(config)

    monkeypatch.setattr(gsc, "compile_scenario", capture)
    cfg = env.load(project_text=gs_project(env, body))
    assert [rule.name for rule in cfg.generate_stream.sequence_rules] == ["global_rule"]
    assert [rule.name for rule in cfg.class_views["ticket_booking"].sequence_rules] == [
        "class_rule"]
    assert cfg.class_views["smart_home"].sequence_rules is None
    domains = {domain.name: domain for domain in captured["config"].sequence_classes}
    assert [rule.name for rule in domains["ticket_booking"].sequence_rules] == ["class_rule"]
    assert [rule.name for rule in domains["smart_home"].sequence_rules] == ["global_rule"]


def test_noise_class_rejects_duration_and_resources(env):
    frames = GS_FRAMES.replace(
        '[frame.class.chatter.generate]\n'
        'instruction = "生成一条与任务无关的闲聊消息"',
        '[frame.class.chatter.generate]\n'
        'instruction = "生成一条与任务无关的闲聊消息"\n'
        'duration_s = [1, 2]\n'
        'resources = ["voice"]')
    body = gs_body(frames=frames)
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "noise frame class must not declare duration_s or resources")



# ── tier tables (v1.14/v1.15 semantics carried over) ────────────────────────


def test_tier_table_covers_ranks_contiguously(env):
    body = gs_body(generate=GS_GENERATE + """\
[[generate.stream.tiers]]
tier_rank = 2
weight = 1
frame_classes = ["task_request", "followup"]
""")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "must be unique and cover 1..N contiguously")


def test_per_class_tier_table_requires_the_global_anchor(env):
    body = gs_body(generate=GS_GENERATE + """\
[[class.ticket_booking.generate.tiers]]
tier_rank = 1
weight = 1
frame_classes = ["task_request", "followup"]
""")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "which is absent")


def test_empty_per_class_tier_table_is_rejected(env):
    generate = GS_GENERATE.replace(
        "len_range = [3, 5]\n", "len_range = [3, 5]\ntiers = []\n", 1)
    body = gs_body(generate=generate)
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "expected a non-empty array of tier tables")


def test_len_range_lower_bound_must_cover_the_tier_composition(env):
    generate = GS_GENERATE.replace("len_range = [3, 5]", "len_range = [1, 4]") + """\
[[generate.stream.tiers]]
tier_rank = 1
weight = 1
frame_classes = ["task_request", "followup"]

[[generate.stream.tiers]]
tier_rank = 2
weight = 1
frame_classes = ["followup"]
"""
    body = gs_body(generate=generate)
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "lower bound must be >= the composition size")


def test_tier_composition_must_stay_within_the_frame_table(env):
    body = gs_body(generate=GS_GENERATE + """\
[[generate.stream.tiers]]
tier_rank = 1
weight = 1
frame_classes = ["task_request", "absent"]
""")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "is not in [[frame.classify.classes]]")


def test_plan_slots_carry_tier_ranks_from_apportionment(env):
    generate = GS_GENERATE + """\
[[generate.stream.tiers]]
tier_rank = 1
weight = 2
frame_classes = ["task_request", "followup"]

[[generate.stream.tiers]]
tier_rank = 2
weight = 1
frame_classes = ["followup"]
"""
    plan = plan_of(env, gs_body(generate=generate))
    ranks = sorted(slot.tier_rank for slot in plan.slots
                   if slot.sequence_class == "ticket_booking")
    assert ranks == [1, 2]      # 配额 2 在权重 2:1 上按最大余额法 ⇒ rank1/rank2 各一


# ── time fields (v1.14 face carried over) ───────────────────────────────────


STRUCTURED_FRAME = """\
[frame.class.task_request.generate]
instruction = "生成一条发起购票任务的用户话语"
schema_inline = \"\"\"
{
  "type": "object",
  "properties": {"utterance": {"type": "string"}, "ts": {"type": "string"}},
  "required": ["utterance", "ts"],
  "additionalProperties": false
}
\"\"\"

[frame.class.task_request.generate.time_fields]
ts = "gap_prev_s"
"""


def test_time_field_vocabulary_is_closed_and_type_literal(env):
    body = gs_body(frames=GS_FRAMES.replace(
        '[frame.class.task_request.generate]\n'
        'instruction = "生成一条发起购票任务的用户话语"\n',
        STRUCTURED_FRAME))
    bad_term = body.replace('ts = "gap_prev_s"', 'ts = "elapsed_minutes"')
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, bad_term))
    has(ei.value.errors, "time vocabulary terms")
    bad_type = body.replace('ts = "gap_prev_s"', 'utterance = "gap_prev_s"')
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, bad_type))
    has(ei.value.errors, 'must declare "type": "number"')


def test_time_field_binding_requires_a_structured_frame_class(env):
    body = gs_body(frames=GS_FRAMES + """
[frame.class.followup.generate.time_fields]
ts = "elapsed_s"
""")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "only legal on a structured frame class")


# ── crossed sessions / duplicates / packing (rule 63) ───────────────────────


def test_crossed_sessions_bounded_by_half_the_target(env):
    body = gs_body(generate=GS_GENERATE.replace("crossed_sessions = 0",
                                                "crossed_sessions = 2"))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "no feasible plan")
    ok = gs_body(generate=GS_GENERATE.replace("crossed_sessions = 0",
                                              "crossed_sessions = 1"))
    plan = plan_of(env, ok)
    assert len(plan.sessions) == len(plan.slots) - 1
    assert sum(1 for s in plan.sessions if s.secondary_slot_key) == 1


def test_duplicates_bounded_by_target(env):
    body = gs_body(generate=GS_GENERATE.replace("duplicates = 1",
                                                "duplicates = 9"))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "no feasible plan")


def test_max_attempts_per_slot_minimum_is_one(env):
    body = gs_body(generate=GS_GENERATE.replace("max_attempts_per_slot = 3",
                                                "max_attempts_per_slot = 0"))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "max_attempts_per_slot")


def test_frame_gap_submicrosecond_lower_bound_is_rejected(env):
    body = gs_body(generate=GS_GENERATE.replace("frame_gap_s = [5, 60]",
                                                "frame_gap_s = [0.0000001, 60]"))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    assert ei.value.errors, "sub-microsecond floor must be rejected"


def test_frame_gap_empty_quantization_is_rejected(env):
    body = gs_body(generate=GS_GENERATE.replace("frame_gap_s = [5, 60]",
                                                "frame_gap_s = [5.0000001, 5.0000002]"))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "empty")


def test_session_max_len_cannot_hold_the_minimum_session_load(env):
    body = gs_body(stream=GS_STREAM.replace("session_max_len = 12",
                                            "session_max_len = 1"))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "no feasible plan")


# ── parked-section waivers & form-only keys ─────────────────────────────────


def test_stream_section_keys_are_parked_without_the_form(env):
    body = gs_body(generate='[generate]\nenabled = true\n'
                            '[generate.stream]\nnoise_ratio = 0.1\n')
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "the stream sub-table is only legal in the time-stream "
                         "generation form")


def test_tier_table_is_parked_without_the_form(env):
    body = gs_body(generate='[generate]\nenabled = true\n'
                            '[[generate.stream.tiers]]\ntier_rank = 1\nweight = 1\n'
                            'frame_classes = ["task_request"]\n')
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "the tier table is only legal")


def test_sequence_and_scenario_validators_are_form_only(env):
    body = gs_body(generate='[generate]\nenabled = true\n'
                            'sequence_validator = "hooks.py:validate_sequence"\n')
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "sequence_validator is only legal in the time-stream")
    body2 = gs_body(generate='[generate]\nenabled = true\n'
                             'scenario_validator = "hooks.py:validate_scenario"\n')
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body2))
    has(ei.value.errors, "scenario_validator is only legal in the time-stream")


def test_participating_class_needs_a_nonempty_instruction(env):
    body = gs_body(generate=GS_GENERATE.replace(
        'instruction = "生成一段高铁购票的用户请求序列"', ""))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "must provide a non-empty generation instruction")


def test_task_frame_instruction_domain_is_enforced(env):
    body = gs_body(frames=GS_FRAMES.replace(
        'instruction = "生成一条补充信息的用户话语"', ""))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "must provide a non-empty generation instruction")


def test_frame_class_table_must_be_nonempty(env):
    body = gs_body(frames="")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "requires a non-empty frame class table")


# ── planner exception lanes (SPEC-SP §8.3) ─────────────────────────────────


def test_infeasible_plan_folds_into_config_error_exit_two(env):
    body = gs_body(generate=GS_GENERATE.replace(
        'end = "2026-01-06T20:00:00+08:00"', 'end = "2026-01-05T09:01:00+08:00"'))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, body))
    has(ei.value.errors, "no feasible plan")


def test_planner_capacity_and_budget_map_to_internal_error_exit_four(
        env, monkeypatch):
    import labelkit.common.config._generate_stream_constraints as gsc

    from labelkit.common.runtime.scenario.diagnostics import (
        PlannerBudgetError,
        PlannerCapacityError,
    )

    for exc_type in (PlannerCapacityError, PlannerBudgetError):
        def boom(_config, _exc=exc_type):
            raise _exc("sequence planner capacity exceeded: model=timeline")
        monkeypatch.setattr(gsc, "compile_scenario", boom)
        with pytest.raises(InternalError) as ei:
            env.load(project_text=gs_project(env))
        assert "model=timeline" in str(ei.value)


def test_form_disabled_skips_the_planner_entirely(env, monkeypatch):
    import labelkit.common.config._generate_stream_constraints as gsc

    def boom(_config):
        raise AssertionError("planner must not run with the form off")
    monkeypatch.setattr(gsc, "compile_scenario", boom)
    cfg = env.load(project_text=gs_project(env, "\n".join((GS_STREAM, FORM_OFF))))
    assert cfg.scenario_plan is None

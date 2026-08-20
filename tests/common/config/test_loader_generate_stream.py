"""Offline unit tests for the v1.13 time-stream generation form in M1
(labelkit/common/config/loader.py; dev spec SPEC-stream-generation.md §3.1).

Covers the whole M1 constraint table row by row (positive AND negative): the
form premise conjunction, the relaxed sequence-class rules, the directed
forbidden-key probes, packing consistency, the weaving caps, the parked-section
waivers, the classify.llm reference-set waiver, the S29 empty-selector
extension, the per-class annotate Schema surface (which is form-INDEPENDENT)
and the frame-class generate Schema surface. Pure config logic — zero LLM.

v1.14 adds the tier table and the time-field binding cluster; v1.15
(SPEC-per-class-tiers.md §3.1) adds the per-class tier table
[[class.<name>.generate.tiers]]: rule 61's three sub-clauses, the per-effective-
table identity/composition checks, the per-class quota pairs and the union-scoped
frame-class checks.

v1.16 增加序列规则/窗口表、类型化 correlation、sequence_validator，以及全局/按类三态整表语义。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from labelkit.common.config import ResolvedConfig
from labelkit.common.config.model import (
    CorrelationSpec,
    GenerateStreamConfig,
    SequenceRuleSpec,
    SequenceWindowSpec,
    TierSpec,
)
from labelkit.common.errors import ConfigError, InternalError
from tests.common.config.test_config import (  # noqa: F401 (env is a fixture)
    BASE_CONFIG,
    SCHEMA,
    Env,
    env,
    has,
)

# ── the canonical time-stream project body (mirrors examples/synth-stream) ──

GS_STREAM = """\
[stream]
order_by = "meta:ts"
gap_s = 900
"""

GS_CLASSIFY = """\
[classify]
enabled = true

[[classify.classes]]
name = "ticket_booking"
description = "高铁购票任务序列"
"""

GS_GENERATE = """\
[generate]
enabled = true

[generate.stream]
enabled = true
sessions = 2
noise_ratio = 0.1
noise_instruction = "生成一条与任务无关的闲聊消息"
duplicates = 1
frame_gap_s = [5, 60]
ts_start = "2026-01-01T09:00:00+08:00"

[class.ticket_booking.generate]
instruction = "生成一段高铁购票的用户请求序列"
sequences = 3
len_range = [3, 5]
"""

GS_FRAMES = """\
[[frame.classify.classes]]
name = "task_request"
description = "发起购票任务的首帧请求"

[[frame.classify.classes]]
name = "followup"
description = "补充出发地与日期等信息"

[frame.class.task_request.generate]
instruction = "生成一条发起购票任务的用户话语"

[frame.class.followup.generate]
instruction = "生成一条补充信息的用户话语"
"""

GS_BODY = "\n".join((GS_STREAM, GS_CLASSIFY, GS_GENERATE, GS_FRAMES))

FRAME_GEN_SCHEMA = json.dumps({
    "type": "object",
    "properties": {"utterance": {"type": "string"},
                   "entities": {"type": "array", "items": {"type": "string"}}},
    "required": ["utterance", "entities"],
    "additionalProperties": False,
}, ensure_ascii=False)

CLASS_SCHEMA = json.dumps({
    "type": "object",
    "properties": {"task": {"type": "string"}, "slots": {"type": "string"}},
    "required": ["task", "slots"],
    "additionalProperties": False,
}, ensure_ascii=False)


def gs_body(*, stream=GS_STREAM, classify=GS_CLASSIFY, generate=GS_GENERATE,
            frames=GS_FRAMES) -> str:
    """The canonical body with one part swapped out (每条约束反例只改一处)。"""
    return "\n".join((stream, classify, generate, frames))


def gs_project(env: Env, body: str = GS_BODY, **kw) -> str:
    kw.setdefault("run_extra", 'mode = "generate_only"')
    return env.project(input_path=None, body=body, **kw)


# ── happy path: parse products + materialized views ─────────────────────────


def test_happy_path_parses_form_and_materializes_views(env):
    cfg = env.load(project_text=gs_project(env))
    gs = cfg.generate_stream
    assert gs.enabled is True
    assert gs.sessions == 2 and gs.duplicates == 1
    assert gs.noise_ratio == pytest.approx(0.1)
    assert gs.noise_instruction == "生成一条与任务无关的闲聊消息"
    assert gs.frame_gap_s == (5.0, 60.0)
    assert gs.ts_start == "2026-01-01T09:00:00+08:00"
    # 按类配额载体
    view = cfg.class_views["ticket_booking"]
    assert view.generate.sequences == 3
    assert view.generate.len_range == (3, 5)
    assert view.generate.instruction == "生成一段高铁购票的用户请求序列"
    assert view.schema is None                       # 未声明 ⇒ 回落全局 output.schema
    # 帧类生成面（frame.classify 保持 false，视图照常物化）
    assert cfg.frame_classify.enabled is False
    assert set(cfg.frame_class_views) == {"task_request", "followup"}
    assert (cfg.frame_class_views["task_request"].gen_instruction
            == "生成一条发起购票任务的用户话语")
    assert cfg.frame_class_views["task_request"].gen_schema is None   # 纯文本帧
    # S29 扩展：空选择子在本形态解析为轨迹准则
    assert cfg.quality.rubric == "default:trajectory"
    assert cfg.rubric.name == "default-trajectory-v1"


def test_form_off_is_v1_12_equivalent(env, capsys):
    # [generate.stream] 在场但 enabled = false ⇒ 全部形态约束零执行（这里的 body 会
    # 违反几乎每一条形态约束：process 模式、classify 关、无帧类表……）
    body = "[generate]\nenabled = false\n\n[generate.stream]\nenabled = false\n" \
           "sessions = 99\n"
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.generate_stream.enabled is False
    assert cfg.generate_stream.sessions == 99        # 停放配置照常解析
    assert "time-stream form" not in capsys.readouterr().err


def test_generate_stream_section_must_be_a_table(env):
    generate = '[generate]\nenabled = true\nstream = 3\ninstruction = "生成"\n'
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate].stream: expected table")


def test_unknown_key_inside_the_form_section_warns(env, capsys):
    body = GS_GENERATE.replace("sessions = 2", "sessions = 2\nfuture_knob = 1")
    env.load(project_text=gs_project(env, gs_body(generate=body)))
    assert "[generate.stream].future_knob: unknown key" in capsys.readouterr().err


# ── 形态前提合取（SPEC §3.1 约束表第一行，逐分量反例）────────────────────────


def test_premise_requires_generate_only(env):
    errors = env.errors(project_text=gs_project(env, run_extra=""))
    has(errors, '[generate.stream].enabled: the time-stream form requires run.mode = "generate_only"')
    has(errors, "this form synthesizes a time stream from scratch and consumes no input data")


def test_premise_requires_text_modality(env):
    project = gs_project(env, modality="ui")
    errors = env.errors(project_text=project)
    has(errors, '[generate.stream].enabled: the time-stream form requires run.modality = "text"')


def test_premise_requires_generate_enabled(env):
    generate = GS_GENERATE.replace("[generate]\nenabled = true",
                                   "[generate]\nenabled = false")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream].enabled: the time-stream form requires generate.enabled = true")


def test_premise_requires_classify_enabled(env):
    classify = GS_CLASSIFY.replace("enabled = true", "enabled = false")
    errors = env.errors(project_text=gs_project(env, gs_body(classify=classify)))
    has(errors, "[generate.stream].enabled: the time-stream form requires classify.enabled = true")
    has(errors, "inherited")


def test_premise_requires_meta_order_by(env):
    for stream in ('[stream]\ngap_s = 900\n',
                   '[stream]\norder_by = "input_order"\ngap_s = 900\n'):
        errors = env.errors(project_text=gs_project(env, gs_body(stream=stream)))
        has(errors, '[stream].order_by: the time-stream form requires "meta:<field>"')


def test_premise_requires_meta_mode_not_none(env):
    body = GS_BODY + f"\n[output]\nmeta_mode = \"none\"\nschema_inline = '''\n{SCHEMA}\n'''\n"
    errors = env.errors(project_text=gs_project(env, body, include_output=False))
    has(errors, '[output].meta_mode: must not be "none" in the time-stream form')
    # sidecar 合法
    cfg = env.load(project_text=gs_project(
        env, body.replace('meta_mode = "none"', 'meta_mode = "sidecar"'),
        include_output=False))
    assert cfg.output.meta_mode == "sidecar"


def test_premise_rejects_dotted_artifact_field_names(env):
    # 工件行以字段名为字面顶层键，M2 按点路径解析——带点即不可往返（v1.13 工件键守卫）
    stream = GS_STREAM.replace('order_by = "meta:ts"', 'order_by = "meta:meta.ts"')
    errors = env.errors(project_text=gs_project(env, gs_body(stream=stream)))
    has(errors, "[stream].order_by: the timestamp field name of the time-stream form "
                'must not contain "."')
    body = GS_BODY + '\n[input]\ntext_field = "conversation.text"\n'
    errors = env.errors(project_text=gs_project(env, body))
    has(errors, "[input].text_field: the text field name of the time-stream form "
                'must not contain "."')


def test_premise_rejects_artifact_key_collisions(env):
    # 工件行三个顶层键（ts 字段 / 文本字段 / truth）互斥
    body = GS_BODY + '\n[input]\ntext_field = "ts"\n'
    errors = env.errors(project_text=gs_project(env, body))
    has(errors, "[input].text_field: must not have the same name as the timestamp field of "
                "[stream].order_by in the time-stream form")
    body = GS_BODY + '\n[input]\ntext_field = "truth"\n'
    errors = env.errors(project_text=gs_project(env, body))
    has(errors, '[input].text_field: the field name must not be "truth" in the time-stream form')
    stream = GS_STREAM.replace('order_by = "meta:ts"', 'order_by = "meta:truth"')
    errors = env.errors(project_text=gs_project(env, gs_body(stream=stream)))
    has(errors, '[stream].order_by: the field name must not be "truth" in the time-stream form')


# ── 类表与配额（≥1 类放宽 / fallback 免填 / 有效 sequences / 参与类指令）──────


def test_single_sequence_class_is_legal_in_the_form_only(env):
    cfg = env.load(project_text=gs_project(env))
    assert [c.name for c in cfg.classify.classes] == ["ticket_booking"]
    # 对照：非本形态的单类表仍被 ≥ 2 规则拒绝
    plain = GS_CLASSIFY + '\nfallback_class = "ticket_booking"\n'
    errors = env.errors(project_text=env.project(body=plain))
    has(errors, "[classify].classes: classify.enabled = true requires >= 2 declared classes")


def test_fallback_class_optional_in_the_form_but_checked_when_written(env):
    cfg = env.load(project_text=gs_project(env))
    assert cfg.classify.fallback_class == ""         # 免填（inherited 无判决路径）
    classify = GS_CLASSIFY.replace("enabled = true",
                                   'enabled = true\nfallback_class = "ghost"')
    errors = env.errors(project_text=gs_project(env, gs_body(classify=classify)))
    has(errors, '[classify].fallback_class: referenced class name "ghost" is not in '
                "[[classify.classes]], available: ticket_booking")
    classify_ok = GS_CLASSIFY.replace(
        "enabled = true", 'enabled = true\nfallback_class = "ticket_booking"')
    cfg = env.load(project_text=gs_project(env, gs_body(classify=classify_ok)))
    assert cfg.classify.fallback_class == "ticket_booking"


def test_at_least_one_class_needs_a_positive_quota(env):
    generate = GS_GENERATE.replace("sequences = 3", "sequences = 0")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[class.<name>.generate].sequences: the time-stream form requires at least one "
                "sequence class with an effective sequences >= 1")
    has(errors, "got a total of 0 across all classes")


def test_global_sequences_is_the_per_class_default(env):
    generate = GS_GENERATE.replace("[generate]\nenabled = true",
                                   "[generate]\nenabled = true\nsequences = 3")
    generate = generate.replace("sequences = 3\nlen_range = [3, 5]",
                                "len_range = [3, 5]")
    cfg = env.load(project_text=gs_project(env, gs_body(generate=generate)))
    assert cfg.generate.sequences == 3
    assert cfg.class_views["ticket_booking"].generate.sequences == 3


def test_participating_class_needs_a_nonempty_instruction(env):
    generate = GS_GENERATE.replace(
        'instruction = "生成一段高铁购票的用户请求序列"\n', "")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[class.ticket_booking.generate].instruction: a participating sequence class "
                "(effective sequences = 3) must provide a non-empty generation instruction")
    # 全局 [generate].instruction 可以充当默认（本形态下全局键不再必填）
    generate_ok = generate.replace("[generate]\nenabled = true",
                                   '[generate]\nenabled = true\ninstruction = "全局生成指令"')
    cfg = env.load(project_text=gs_project(env, gs_body(generate=generate_ok)))
    assert cfg.class_views["ticket_booking"].generate.instruction == "全局生成指令"


def test_global_generate_instruction_not_required_in_the_form(env):
    cfg = env.load(project_text=gs_project(env))
    assert cfg.generate.instruction == ""            # 本形态豁免 §5.2 † 必填
    # 对照：非本形态照旧必填
    errors = env.errors(project_text=env.project(
        body='[generate]\nenabled = true\nnum_per_record = 2'))
    has(errors, "[generate].instruction: required when generate.enabled = true")


def test_frame_class_table_must_be_nonempty_and_fully_instructed(env):
    errors = env.errors(project_text=gs_project(env, gs_body(frames="")))
    has(errors, "[[frame.classify.classes]]: the time-stream form requires a non-empty frame class table")
    # 帧类在表里但缺 generate 节
    frames = GS_FRAMES.replace(
        '[frame.class.followup.generate]\ninstruction = "生成一条补充信息的用户话语"\n', "")
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, "[frame.class.followup.generate].instruction: every frame class must provide a "
                "non-empty generation instruction")
    # generate 节在场但 instruction 为空串
    frames_empty = GS_FRAMES.replace('instruction = "生成一条补充信息的用户话语"',
                                     'instruction = "  "')
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames_empty)))
    has(errors, "[frame.class.followup.generate].instruction")


# ── 禁设键探针（v1.11 原始节探针机制）──────────────────────────────────────


@pytest.mark.parametrize("line, key", [
    ('seed_examples = ["种子"]', "seed_examples"),
    ("standalone_count = 5", "standalone_count"),
    ("num_per_record = 2", "num_per_record"),
    ("seeds_per_call = 3", "seeds_per_call"),
])
def test_generate_forbidden_keys_are_directed_errors(env, line, key):
    generate = GS_GENERATE.replace("[generate]\nenabled = true",
                                   f"[generate]\nenabled = true\n{line}")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, f"[generate].{key}: the time-stream form does not provide this key")
    has(errors, "[class.<name>.generate].sequences")     # 指引指向替代面


@pytest.mark.parametrize("line, key", [
    ("num_per_record = 2", "num_per_record"),
    ("seeds_per_call = 3", "seeds_per_call"),
])
def test_class_generate_forbidden_keys_are_directed_errors(env, line, key):
    generate = GS_GENERATE.replace("sequences = 3", f"sequences = 3\n{line}")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, f"[class.ticket_booking.generate].{key}: the time-stream form does not provide this key")


def test_class_generate_seeds_per_call_also_hits_the_whitelist(env):
    # seeds_per_call 从来不在 [class.*.generate] 白名单内（无条件规则），本形态的
    # 定向探针叠加其上 ⇒ 两条互补的错误行：一条说"该键根本不可按类覆盖"，一条说
    # "本形态另有配额面"。聚合式上报，两条都要在。
    generate = GS_GENERATE.replace("sequences = 3", "sequences = 3\nseeds_per_call = 3")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[class.ticket_booking.generate].seeds_per_call: [class.*.generate] cannot override this key")
    has(errors, "[class.ticket_booking.generate].seeds_per_call: the time-stream form does not provide this key")


def test_frame_switches_are_mutually_exclusive_with_the_form(env):
    frames = GS_FRAMES + '\n[frame.classify]\nenabled = true\nfallback_class = "followup"\n'
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, "[frame.classify].enabled: mutually exclusive with the time-stream form")
    has(errors, "[frame.class.<name>.generate]")
    frames = GS_FRAMES + ("\n[frame.annotate]\nenabled = true\ninstruction = \"标\"\n"
                          f"schema_inline = '''\n{FRAME_GEN_SCHEMA}\n'''\n")
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, "[frame.annotate].enabled: mutually exclusive with the time-stream form")


# ── 装箱一致性（sessions / duplicates / noise / frame_gap_s）────────────────


def test_sessions_must_be_positive(env):
    generate = GS_GENERATE.replace("sessions = 2", "sessions = 0")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream].sessions: expected an integer >= 1 (number of sessions), got 0")


@pytest.mark.parametrize("sessions", [1, 4])
def test_sessions_bracket_sequence_total(env, sessions):
    # Σsequences = 3 ⇒ 合法 sessions ∈ {2, 3}（sessions ≤ Σ ≤ 2 × sessions）
    generate = GS_GENERATE.replace("sessions = 2", f"sessions = {sessions}")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, '[generate.stream].sessions: expected sessions <= Σsequences <= 2 * sessions')
    has(errors, f"got sessions = {sessions}, Σsequences = 3")


@pytest.mark.parametrize("sessions", [2, 3])
def test_sessions_bracket_accepts_the_bounds(env, sessions):
    generate = GS_GENERATE.replace("sessions = 2", f"sessions = {sessions}")
    cfg = env.load(project_text=gs_project(env, gs_body(generate=generate)))
    assert cfg.generate_stream.sessions == sessions


def test_duplicates_bounded_by_sequence_total(env):
    generate = GS_GENERATE.replace("duplicates = 1", "duplicates = 4")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, '[generate.stream].duplicates: expected an integer in [0, Σsequences]')
    for value in (0, 3):
        generate = GS_GENERATE.replace("duplicates = 1", f"duplicates = {value}")
        cfg = env.load(project_text=gs_project(env, gs_body(generate=generate)))
        assert cfg.generate_stream.duplicates == value


@pytest.mark.parametrize("value", ["1.0", "1.5", "-0.1"])
def test_noise_ratio_half_open_unit_interval(env, value):
    generate = GS_GENERATE.replace("noise_ratio = 0.1", f"noise_ratio = {value}")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream].noise_ratio: expected a number in [0,1)")


def test_noise_instruction_required_only_above_zero(env):
    generate = GS_GENERATE.replace(
        'noise_instruction = "生成一条与任务无关的闲聊消息"\n', "")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream].noise_instruction: required when noise_ratio > 0")
    zero = generate.replace("noise_ratio = 0.1", "noise_ratio = 0.0")
    cfg = env.load(project_text=gs_project(env, gs_body(generate=zero)))
    assert cfg.generate_stream.noise_ratio == 0.0
    assert cfg.generate_stream.noise_instruction == ""


@pytest.mark.parametrize("value", ["[0, 60]", "[60, 5]", "[5]", '"5-60"',
                                   "[5, 60, 90]"])
def test_frame_gap_structural_errors(env, value):
    generate = GS_GENERATE.replace("frame_gap_s = [5, 60]", f"frame_gap_s = {value}")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream].frame_gap_s: expected number range array of length 2 [lo, "
                "hi] (0 < lo <= hi, seconds)")


def test_frame_gap_upper_bound_is_strict_on_default_v15_path(env):
    generate = GS_GENERATE.replace("frame_gap_s = [5, 60]", "frame_gap_s = [5, 900]")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream].frame_gap_s: the upper bound must be < stream.gap_s")
    generate = GS_GENERATE.replace("frame_gap_s = [5, 60]", "frame_gap_s = [5, 901]")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream].frame_gap_s: the upper bound must be < stream.gap_s")
    generate = GS_GENERATE.replace("frame_gap_s = [5, 60]", "frame_gap_s = [5, 899]")
    cfg = env.load(project_text=gs_project(env, gs_body(generate=generate)))
    assert cfg.generate_stream.frame_gap_s == (5.0, 899.0)


def test_frame_gap_upper_bound_at_session_gap_is_allowed_for_constrained_prefix(env):
    generate = GS_GENERATE.replace("frame_gap_s = [5, 60]", "frame_gap_s = [5, 900]")
    cfg = env.load(project_text=gs_project(env, sequence_tables_body(
        generate=generate, rules=RULE_INIT)))
    assert cfg.generate_stream.frame_gap_s == (5.0, 900.0)


def test_sequence_validator_alone_keeps_default_frame_gap_boundary(env):
    generate = GS_GENERATE.replace("frame_gap_s = [5, 60]", "frame_gap_s = [5, 900]")
    generate = generate.replace("[generate]\nenabled = true", "[generate]\nenabled = true\n"
                                'sequence_validator = "tests.hook_samples:sequence_ok"', 1)
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream].frame_gap_s: the upper bound must be < stream.gap_s")


def test_frame_gap_defaults_when_absent(env):
    generate = GS_GENERATE.replace("frame_gap_s = [5, 60]\n", "")
    cfg = env.load(project_text=gs_project(env, gs_body(generate=generate)))
    assert cfg.generate_stream.frame_gap_s == (5.0, 60.0)


# ── 织造上限（session_max_len / key / gap_steps / span）─────────────────────


def test_session_max_len_covers_two_longest_sequences(env):
    stream = GS_STREAM + "session_max_len = 9\n"
    errors = env.errors(project_text=gs_project(env, gs_body(stream=stream)))
    has(errors, "[stream].session_max_len: the time-stream form requires >= 2 * max(len_range upper bound)")
    has(errors, "got 9 < 10")
    stream_ok = GS_STREAM + "session_max_len = 10\n"
    cfg = env.load(project_text=gs_project(env, gs_body(stream=stream_ok)))
    assert cfg.stream.session_max_len == 10


def test_partition_key_and_gap_steps_must_be_neutral(env):
    stream = GS_STREAM + 'key = ["meta:user"]\ngap_steps = 5\n'
    errors = env.errors(project_text=gs_project(env, gs_body(stream=stream)))
    has(errors, "[stream].key: the time-stream form requires an empty array")
    has(errors, "[stream].gap_steps: the time-stream form requires 0")


def test_session_span_static_check(env):
    stream = GS_STREAM + "session_max_len = 10\nsession_max_span_s = 500\n"
    errors = env.errors(project_text=gs_project(env, gs_body(stream=stream)))
    has(errors, "[stream].session_max_span_s: worst-case session span (session_max_len - 1) * "
                "frame_gap_s upper bound = 540 s > 500 s")
    stream_ok = GS_STREAM + "session_max_len = 10\nsession_max_span_s = 540\n"
    cfg = env.load(project_text=gs_project(env, gs_body(stream=stream_ok)))
    assert cfg.stream.session_max_span_s == 540


def test_ts_start_must_be_iso8601_and_defaults_without_wall_clock(env):
    generate = GS_GENERATE.replace('ts_start = "2026-01-01T09:00:00+08:00"',
                                   'ts_start = "昨天早上"')
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream].ts_start: expected a parseable ISO-8601 instant")
    generate = GS_GENERATE.replace('ts_start = "2026-01-01T09:00:00+08:00"\n', "")
    cfg = env.load(project_text=gs_project(env, gs_body(generate=generate)))
    assert cfg.generate_stream.ts_start == "2026-01-01T00:00:00Z"


# ── 停放豁免（[stream] / [frame]）与引用集豁免（classify.llm）───────────────


def test_stream_and_frame_sections_are_not_parked_in_the_form(env, capsys):
    env.load(project_text=gs_project(env))
    err = capsys.readouterr().err
    assert "no effect" not in err
    assert "[stream]" not in err and "[frame]" not in err


def test_same_sections_are_parked_without_the_form(env, capsys):
    body = ('[stream]\norder_by = "meta:ts"\n\n'
            '[frame.classify]\nllm = "judge"\n')
    env.load(project_text=env.project(body=body))
    err = capsys.readouterr().err
    assert "[segment].enabled" in err and "no effect" in err
    assert "[stream]" in err and "[frame]" in err


def test_classify_llm_key_not_required_in_the_form(env, monkeypatch):
    monkeypatch.delenv("LK_TEST_KEY_JUDGE")
    classify = GS_CLASSIFY.replace("enabled = true", 'enabled = true\nllm = "judge"')
    cfg = env.load(project_text=gs_project(env, gs_body(classify=classify)))
    assert cfg.classify.llm == "judge"
    assert cfg.llm_profiles["judge"].api_key == ""       # 零判决调用 ⇒ 不强制活密钥
    # 存在性检查照旧（拼错 profile 名仍在启动期揪出）
    classify_bad = GS_CLASSIFY.replace("enabled = true",
                                       'enabled = true\nllm = "ghost"')
    errors = env.errors(project_text=gs_project(env, gs_body(classify=classify_bad)))
    has(errors, '[classify].llm: referenced profile "ghost" does not exist')


def test_classify_llm_key_still_required_without_the_form(env, monkeypatch):
    monkeypatch.delenv("LK_TEST_KEY_JUDGE")
    body = """\
[classify]
enabled = true
llm = "judge"
fallback_class = "qa"

[[classify.classes]]
name = "qa"
description = "问答"

[[classify.classes]]
name = "other"
description = "其他"
"""
    errors = env.errors(project_text=env.project(body=body))
    has(errors, 'environment variable "LK_TEST_KEY_JUDGE" is not set or empty')


# ── S29 扩展（空 rubric 选择子）────────────────────────────────────────────


def test_empty_rubric_selector_resolves_to_trajectory(env, capsys):
    cfg = env.load(project_text=gs_project(env))
    assert cfg.quality.rubric == "default:trajectory"
    assert cfg.class_views["ticket_booking"].quality.rubric == "default:trajectory"
    assert cfg.class_views["ticket_booking"].rubric is cfg.rubric
    # extract 在本形态不可用，故 S29 的「请启用 [extract]」组合 advisory 不适用
    assert "frame digests" not in capsys.readouterr().err


def test_explicit_rubric_selector_beats_the_trajectory_default(env):
    body = GS_BODY + '\n[quality]\nrubric = "default:text"\n'
    cfg = env.load(project_text=gs_project(env, body))
    assert cfg.rubric.name == "default-text-v1"


# ── 按类标注 Schema（裁决·按类标注 Schema；与形态无关）──────────────────────

CLASSIFY_TWO = """\
[classify]
enabled = true
fallback_class = "qa"

[[classify.classes]]
name = "qa"
description = "问答"

[[classify.classes]]
name = "writing"
description = "写作"
"""


def class_schema_body(qa_extra: str) -> str:
    return CLASSIFY_TWO + f"\n[class.qa.annotate]\n{qa_extra}\n"


def test_class_annotate_schema_overrides_global(env):
    cfg = env.load(project_text=env.project(
        body=class_schema_body(f"schema_inline = '''\n{CLASS_SCHEMA}\n'''")))
    assert cfg.class_views["qa"].schema == json.loads(CLASS_SCHEMA)
    assert cfg.class_views["writing"].schema is None     # 未声明 ⇒ 回落全局
    assert cfg.user_schema == json.loads(SCHEMA)         # 全局不被污染


def test_class_annotate_schema_path_variant_and_unreadable(env):
    schema_file = env.tmp / "qa_schema.json"
    schema_file.write_text(CLASS_SCHEMA, encoding="utf-8")
    cfg = env.load(project_text=env.project(
        body=class_schema_body(f'schema_path = "{schema_file}"')))
    assert cfg.class_views["qa"].schema == json.loads(CLASS_SCHEMA)
    errors = env.errors(project_text=env.project(
        body=class_schema_body('schema_path = "ghost/qa.json"')))
    has(errors, "[class.qa.annotate].schema_path: cannot read schema file")


def test_class_annotate_schema_at_most_one_source(env):
    errors = env.errors(project_text=env.project(body=class_schema_body(
        f"schema_path = \"x.json\"\nschema_inline = '''\n{CLASS_SCHEMA}\n'''")))
    has(errors, "[class.qa.annotate].schema_inline: exactly one of schema_path / schema_inline "
                "must be provided (mutually exclusive), got both set")


def test_class_annotate_schema_meta_validation_branches(env):
    errors = env.errors(project_text=env.project(
        body=class_schema_body("schema_inline = '{bad'")))
    has(errors, "[class.qa.annotate].schema_inline: expected valid JSON")
    errors = env.errors(project_text=env.project(
        body=class_schema_body("schema_inline = '[1, 2]'")))
    has(errors, "[class.qa.annotate].schema_inline: per-class annotation schema must be a JSON object at the top level")
    errors = env.errors(project_text=env.project(body=class_schema_body(
        'schema_inline = \'{"type": "object", "properties": 3}\'')))
    has(errors, "[class.qa.annotate].schema_inline: failed JSON Schema draft 2020-12 meta-schema validation")
    errors = env.errors(project_text=env.project(
        body=class_schema_body('schema_inline = \'{"type": "array"}\'')))
    has(errors, '[class.qa.annotate].schema_inline: per-class annotation schema top-level type must be "object"')


def test_class_annotate_schema_forbids_reserved_meta_key(env):
    bad = json.dumps({"type": "object",
                      "properties": {"_meta": {"type": "object"}}})
    errors = env.errors(project_text=env.project(
        body=class_schema_body(f"schema_inline = '''\n{bad}\n'''")))
    has(errors, "[class.qa.annotate].schema_inline: per-class annotation schema must not "
                'declare the reserved top-level key "_meta"')


def test_class_annotate_schema_dangling_ref_is_config_error(env):
    bad = json.dumps({"type": "object",
                      "properties": {"x": {"$ref": "#/$defs/ghost"}}})
    errors = env.errors(project_text=env.project(
        body=class_schema_body(f"schema_inline = '''\n{bad}\n'''")))
    has(errors, "[class.qa.annotate].schema_inline: per-class annotation schema has an unresolvable reference")


def test_class_examples_dryrun_against_the_class_schema(env):
    # 类示例在全局 Schema 下非法、在类 Schema 下合法 ⇒ 放行（v1.13 修正前会误报）
    body = class_schema_body(
        f"schema_inline = '''\n{CLASS_SCHEMA}\n'''\n"
        'examples = [{input = "订票", output = {task = "book", slots = "上海"}}]')
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.class_views["qa"].annotate.examples[0].output["task"] == "book"
    # 反例：类示例不过类 Schema
    bad = class_schema_body(
        f"schema_inline = '''\n{CLASS_SCHEMA}\n'''\n"
        'examples = [{input = "订票", output = {task = "book"}}]')
    errors = env.errors(project_text=env.project(body=bad))
    has(errors, "[[class.qa.annotate.examples]][1].output: failed per-class annotation schema validation")


def test_inherited_examples_rechecked_against_the_class_schema(env):
    # 类只声明 Schema、示例继承全局 ⇒ 继承来的示例也要过类 Schema（运行期就是按类
    # Schema 发出去的）
    global_examples = ('instruction = "标注意图"\n'
                       'examples = [{input = "写请假条", '
                       'output = {intent = "writing_assist", topic = "请假条"}}]')
    body = class_schema_body(f"schema_inline = '''\n{CLASS_SCHEMA}\n'''")
    errors = env.errors(project_text=env.project(annotate_body=global_examples,
                                                 body=body))
    has(errors, "[[class.qa.annotate.examples]][1].output: failed per-class annotation schema validation")


def test_class_without_own_schema_keeps_global_dryrun_wording(env):
    body = CLASSIFY_TWO + ("\n[class.qa.annotate]\n"
                           'examples = [{input = "问", output = {intent = "qa"}}]\n')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[[class.qa.annotate.examples]][1].output: failed user schema validation")


def test_class_annotate_schema_keys_are_whitelisted(env):
    cfg = env.load(project_text=env.project(
        body=class_schema_body(f"schema_inline = '''\n{CLASS_SCHEMA}\n'''")))
    assert isinstance(cfg, ResolvedConfig)
    errors = env.errors(project_text=env.project(
        body=class_schema_body('schema_url = "https://example.com/s.json"')))
    has(errors, "[class.qa.annotate].schema_url: [class.*.annotate] cannot override this key "
                "(whitelist: instruction, examples, schema_path, schema_inline)")


def test_class_generate_quota_keys_are_whitelisted(env):
    # v1.16: rules/windows 与 tiers 一并进入按类生成白名单
    errors = env.errors(project_text=env.project(
        body=CLASSIFY_TWO + "\n[class.qa.generate]\nsequence_count = 3\n"))
    has(errors, "[class.qa.generate].sequence_count: [class.*.generate] cannot override this "
                "key (whitelist: instruction, styles, num_per_record, temperature, sequences, "
                "len_range, tiers, rules, windows)")


def test_forbidden_generate_key_probe_skips_classes_without_a_generate_table(env):
    # 禁设键探针逐类扫 [class.*.generate]；没有该子表的类（这里只写了标注覆盖，
    # 且配额为 0 故不参与）直接跳过，不误报也不漏扫有该子表的那个类。
    classify = GS_CLASSIFY + """
[[classify.classes]]
name = "smart_home"
description = "智能家居控制序列"
"""
    body = (gs_body(classify=classify)
            + '\n[class.smart_home.annotate]\ninstruction = "标注家居意图"\n')
    cfg = env.load(project_text=gs_project(env, body))
    assert cfg.class_views["smart_home"].generate.sequences == 0     # 不参与配额
    assert cfg.class_views["smart_home"].annotate.instruction == "标注家居意图"
    # 反证：给同一个类补上带禁设键的 generate 子表，探针照常点名
    errors = env.errors(project_text=gs_project(
        env, body + "\n[class.smart_home.generate]\nnum_per_record = 2\n"))
    has(errors, "[class.smart_home.generate].num_per_record: the time-stream form does "
                "not provide this key")


@pytest.mark.parametrize("value", ["[0, 5]", "[5, 3]", "[3]", '"3-5"',
                                   "[3.5, 5.0]"])
def test_class_len_range_structural_errors(env, value):
    generate = GS_GENERATE.replace("len_range = [3, 5]", f"len_range = {value}")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[class.ticket_booking.generate].len_range: expected integer range array of "
                "length 2 [lo, hi] (1 <= lo <= hi)")


# ── 帧类生成 Schema（裁决·帧类生成面）───────────────────────────────────────


def frames_with_schema(extra: str) -> str:
    return GS_FRAMES.replace('instruction = "生成一条发起购票任务的用户话语"',
                             f'instruction = "生成一条发起购票任务的用户话语"\n{extra}')


def test_frame_class_generate_schema_parsed(env):
    frames = frames_with_schema(f"schema_inline = '''\n{FRAME_GEN_SCHEMA}\n'''")
    cfg = env.load(project_text=gs_project(env, gs_body(frames=frames)))
    view = cfg.frame_class_views["task_request"]
    assert view.gen_schema == json.loads(FRAME_GEN_SCHEMA)
    assert cfg.frame_class_views["followup"].gen_schema is None   # 纯文本帧


def test_frame_class_generate_schema_path_variant_and_unreadable(env):
    schema_file = env.tmp / "frame_gen.json"
    schema_file.write_text(FRAME_GEN_SCHEMA, encoding="utf-8")
    frames = frames_with_schema(f'schema_path = "{schema_file}"')
    cfg = env.load(project_text=gs_project(env, gs_body(frames=frames)))
    assert cfg.frame_class_views["task_request"].gen_schema == json.loads(
        FRAME_GEN_SCHEMA)
    frames = frames_with_schema('schema_path = "ghost/frame.json"')
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, "[frame.class.task_request.generate].schema_path: cannot read schema file")


def test_frame_class_generate_schema_at_most_one_source(env):
    frames = frames_with_schema(
        f"schema_path = \"x.json\"\nschema_inline = '''\n{FRAME_GEN_SCHEMA}\n'''")
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, "[frame.class.task_request.generate].schema_inline: exactly one of schema_path "
                "/ schema_inline must be provided (mutually exclusive), got both set")


def test_frame_class_generate_schema_meta_validation_branches(env):
    for snippet, expected in (
            ("schema_inline = '{bad'", "expected valid JSON"),
            ("schema_inline = '[1, 2]'",
             "frame-class generation schema must be a JSON object at the top level"),
            ('schema_inline = \'{"type": "object", "properties": 3}\'',
             "failed JSON Schema draft 2020-12 meta-schema validation"),
            ('schema_inline = \'{"type": "array"}\'',
             'frame-class generation schema top-level type must be "object"')):
        errors = env.errors(project_text=gs_project(
            env, gs_body(frames=frames_with_schema(snippet))))
        has(errors, f"[frame.class.task_request.generate].schema_inline: {expected}")


def test_frame_class_generate_schema_dangling_ref_is_config_error(env):
    bad = json.dumps({"type": "object",
                      "properties": {"x": {"$ref": "#/$defs/ghost"}}})
    frames = frames_with_schema(f"schema_inline = '''\n{bad}\n'''")
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, "[frame.class.task_request.generate].schema_inline: frame-class generation "
                "schema has an unresolvable reference")


def test_frame_class_generate_whitelist_enforced(env):
    # v1.14 起白名单四键（time_fields 入表，否则绑定子表会被判成未知键）
    frames = frames_with_schema("enabled = false")
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, "[frame.class.task_request.generate].enabled: [frame.class.*.generate] cannot "
                "override this key (whitelist: instruction, schema_path, schema_inline, "
                "time_fields)")


def test_frame_class_generate_section_only_legal_in_the_form(env):
    body = ("[segment]\nenabled = true\n\n"
            '[frame.classify]\nenabled = true\nfallback_class = "task_request"\n\n'
            "[[frame.classify.classes]]\nname = \"task_request\"\n"
            'description = "请求帧"\n\n'
            '[frame.class.task_request.generate]\ninstruction = "生成"\n')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[frame.class.task_request.generate]: this section is only legal in the "
                "time-stream generation form ([generate.stream].enabled = true)")


def test_frame_class_namespace_allowed_without_frame_classify_in_the_form(env):
    # v1.12 的「[frame.class.*] 在场要求 frame.classify.enabled」在本形态放宽
    cfg = env.load(project_text=gs_project(env))
    assert cfg.frame_classify.enabled is False
    assert set(cfg.frame_class_views) == {"task_request", "followup"}
    # 对照：两者都关时照旧报错，且报错文案给出两条出路
    body = '[frame.class.ghost.annotate]\ninstruction = "x"\n'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[frame.class.ghost]: the presence of [frame.class.*] requires "
                "frame.classify.enabled = true or generate.stream.enabled = true")


def test_frame_class_unknown_name_rejected_in_the_form(env):
    frames = GS_FRAMES + '\n[frame.class.ghost.generate]\ninstruction = "x"\n'
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, '[frame.class.ghost]: class name "ghost" is not in [[frame.classify.classes]]')


# ── V13③ 静态预算预检：蓝图段与帧实现段 ─────────────────────────────────────


def _cw(cw: int) -> str:
    return BASE_CONFIG.replace("supports_structured_output = true",
                               f"supports_structured_output = true\n"
                               f"context_window = {cw}", 1)


def test_static_precheck_plan_and_realize_segments(env):
    # ib(cw=4864) = 281 token；类指令 300 CJK 字直接压爆两段
    generate = GS_GENERATE.replace('instruction = "生成一段高铁购票的用户请求序列"',
                                   f'instruction = "{"生" * 300}"')
    errors = env.errors(config_text=_cw(4864),
                        project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream.plan]: static system-side prompt parts estimated")
    has(errors, "[generate.stream.realize]: static system-side prompt parts estimated")
    has(errors, "no record can fit")


def test_static_precheck_realize_counts_schema_times_len_max(env):
    # 帧类生成 Schema 只经帧实现段计价（× L_max），蓝图段不计
    big = json.dumps({"type": "object",
                      "properties": {"utterance": {"type": "string",
                                                   "description": "描" * 200}},
                      "required": ["utterance"], "additionalProperties": False},
                     ensure_ascii=False)
    frames = frames_with_schema(f"schema_inline = '''\n{big}\n'''")
    errors = env.errors(config_text=_cw(4864),
                        project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, "[generate.stream.realize]: static system-side prompt parts estimated")
    assert not any("[generate.stream.plan]:" in e for e in errors)


def test_static_precheck_silent_with_room(env, capsys):
    cfg = env.load(config_text=_cw(131072), project_text=gs_project(env))
    assert isinstance(cfg, ResolvedConfig)
    err = capsys.readouterr().err
    assert "[generate.stream.plan]" not in err
    assert "[generate.stream.realize]" not in err


def test_joint_realize_precheck_allows_bounded_halving_without_correlation(env):
    """只有无 correlation 规则时，9000 上下文仍保留 realize 对半降级。"""
    cfg = env.load(
        config_text=_cw(9000),
        project_text=gs_project(env, sequence_tables_body(rules=RULE_INIT)))
    assert isinstance(cfg, ResolvedConfig)


def test_joint_realize_precheck_reserves_the_sampled_brief_output_for_correlation(env):
    """有 correlation 时，完整 sampled brief 输出必须计入不可拆 realize 预算。"""
    correlation = ('correlation = {operator = "equal", source_field = "subject_id", '
                   'target_field = "subject_id"}')
    rules = RULE_RESPONSE.replace('target = "followup"',
                                  'target = "followup"\n' + correlation)
    constrained = env.errors(
        config_text=_cw(9000),
        project_text=gs_project(env, sequence_tables_body(
            rules=rules, frames=structured_correlation_frames())))
    has(constrained, "[generate.stream.realize]: static system-side prompt parts estimated")


# ── V13③ annotate 段的按类取值修订（3.1.4 时间流生成末段「annotate 段口径修订」）─

# 20 个带长描述的属性，est 1327 token —— 远大于全局 SCHEMA 的 est 87。
BIG_CLASS_SCHEMA = json.dumps({
    "type": "object",
    "properties": {f"field_{i}": {"type": "string", "description": "字段语义说明" * 8}
                   for i in range(20)},
    "required": ["field_0"],
    "additionalProperties": False,
}, ensure_ascii=False)


def test_static_precheck_annotate_segment_prices_per_class_schema(env, capsys):
    # 计价三项全部按类取：head 32 + 类 Schema 1327 + 继承来的全局 instruction 4
    # + 类 few-shot 9 = 1372 ≥ ib 1304（cw 6000）。
    body = class_schema_body(f"schema_inline = '''\n{BIG_CLASS_SCHEMA}\n'''\n"
                             'examples = [{input = "订票", output = {field_0 = "值"}}]')
    errors = env.errors(config_text=_cw(6000), project_text=env.project(body=body))
    has(errors, "[annotate]: static system-side prompt parts estimated at 1372 tokens "
                ">= the input budget of 1304 tokens")
    # 反例：只把该类 Schema 换小（整份和 96 token），其余一字不改 ⇒ 预检彻底安静
    body = class_schema_body(f"schema_inline = '''\n{CLASS_SCHEMA}\n'''\n"
                             'examples = [{input = "订票", output = {task = "book", '
                             'slots = "上海"}}]')
    cfg = env.load(config_text=_cw(6000), project_text=env.project(body=body))
    assert isinstance(cfg, ResolvedConfig)
    assert "[annotate]: static system-side prompt parts estimated" not in capsys.readouterr().err


def test_static_precheck_annotate_max_runs_over_whole_per_class_sums(env):
    # qa 只换 Schema（1327 + 继承来的全局 instruction 4），writing 只换 instruction
    # （回落全局 Schema 87 + 300）；max 取两份**整份和**的较大者 1363，而不是逐项取
    # max 后再相加（那会得到 32 + 1327 + 300 = 1659）。
    body = (class_schema_body(f"schema_inline = '''\n{BIG_CLASS_SCHEMA}\n'''")
            + f'\n[class.writing.annotate]\ninstruction = "{"标" * 300}"\n')
    errors = env.errors(config_text=_cw(6000), project_text=env.project(body=body))
    has(errors, "[annotate]: static system-side prompt parts estimated at 1363 tokens")
    assert "1659 tokens" not in "\n".join(errors)
    # 回归锚：去掉唯一那份按类 Schema，逐类回落全局 Schema 文本 ⇒ 数值退回 v1.12 形态
    # 的 32 + 87 + 300 = 419（ib 281 才够小到让它触发）。
    body = CLASSIFY_TWO + f'\n[class.writing.annotate]\ninstruction = "{"标" * 300}"\n'
    errors = env.errors(config_text=_cw(4864), project_text=env.project(body=body))
    has(errors, "[annotate]: static system-side prompt parts estimated at 419 tokens")


# ── aggregation discipline: every violation reported in one pass ────────────


def test_all_form_violations_aggregate_into_one_error(env):
    generate = (GS_GENERATE
                .replace("sessions = 2", "sessions = 9")
                .replace("duplicates = 1", "duplicates = 99")
                .replace("noise_ratio = 0.1", "noise_ratio = 2.0"))
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=gs_project(env, gs_body(generate=generate),
                                         run_extra=""))
    joined = "\n".join(ei.value.errors)
    assert "[generate.stream].sessions" in joined
    assert "[generate.stream].duplicates" in joined
    assert "[generate.stream].noise_ratio" in joined
    assert "[generate.stream].enabled: the time-stream form requires run.mode" in joined


def test_generate_stream_default_dataclass_is_all_off():
    gs = GenerateStreamConfig()
    assert gs.enabled is False and gs.sessions == 0
    assert gs.noise_ratio == 0.0 and gs.noise_instruction == ""
    assert gs.duplicates == 0 and gs.frame_gap_s == (5.0, 60.0)
    assert gs.ts_start == "2026-01-01T00:00:00Z"
    assert gs.tiers == ()                            # v1.14 档位面不在场


# ── v1.14 档位表（[[generate.stream.tiers]]；SPEC-generation-tiers §3.1）──────

TIER_ONE = """\
[[generate.stream.tiers]]
tier_rank = 1
weight = 2
frame_classes = ["task_request", "followup"]
"""

TIER_TWO = """\
[[generate.stream.tiers]]
tier_rank = 2
weight = 1
frame_classes = ["task_request", "followup", "confirmation"]
"""

TIERS = "\n" + TIER_ONE + "\n" + TIER_TWO

# 档位面的基线帧类表：三类（第 2 档的构成用满全表）
GS_FRAMES3 = GS_FRAMES + """
[[frame.classify.classes]]
name = "confirmation"
description = "确认下单的收尾帧"

[frame.class.confirmation.generate]
instruction = "生成一条确认下单的用户话语"
"""


def tier_body(tiers: str = TIERS, *, generate: str = GS_GENERATE,
              frames: str = GS_FRAMES3) -> str:
    """The canonical body plus a tier table（档位面在场的基线工程）。

    基线数：sequences = 3 按权重 (2, 1) 配分为 (2, 1)，len_range 下界 3 覆盖两档的
    最大构成 3 —— 全部约束刚好通过。
    """
    return gs_body(generate=generate + tiers, frames=frames)


def test_tier_table_parses_into_the_form_config(env):
    cfg = env.load(project_text=gs_project(env, tier_body()))
    tiers = cfg.generate_stream.tiers
    assert tiers == (
        TierSpec(tier_rank=1, weight=2, frame_classes=("task_request", "followup")),
        TierSpec(tier_rank=2, weight=1,
                 frame_classes=("task_request", "followup", "confirmation")))


def test_tier_table_is_stored_by_ascending_rank_whatever_the_declaration_order(env):
    # tiers[rank - 1] 直取是 M6 蓝图侧的取档方式 ⇒ 存放序必须由 rank 定，而非书写序
    cfg = env.load(project_text=gs_project(
        env, tier_body("\n" + TIER_TWO + "\n" + TIER_ONE)))
    assert [t.tier_rank for t in cfg.generate_stream.tiers] == [1, 2]
    assert cfg.generate_stream.tiers[0].weight == 2


def test_tier_table_requires_the_time_stream_form(env):
    body = ("[generate]\nenabled = false\n\n[generate.stream]\nenabled = false\n" + TIERS)
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[[generate.stream.tiers]]: the tier table is only legal in the time-stream "
                "generation form ([generate.stream].enabled = true)")


def test_tier_table_premise_probe_is_independent_of_the_parse(env):
    # v1.11 原始节探针机制：表内容非法（解析产物为空）也要照发前提错误
    body = ("[generate]\nenabled = false\n\n[generate.stream]\nenabled = false\n"
            "\n[[generate.stream.tiers]]\ntier_rank = 0\n")
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[[generate.stream.tiers]]: the tier table is only legal in the time-stream")
    has(errors, "[[generate.stream.tiers]][1].tier_rank: expected positive integer, got 0")


def test_no_tier_table_is_byte_equivalent_to_v1_13(env, capsys):
    cfg = env.load(project_text=gs_project(env))
    assert cfg.generate_stream.tiers == ()
    err = capsys.readouterr().err                    # 零档位告警：档位面整体缺席
    assert "[[generate.stream.tiers]]" not in err
    assert "tier composition" not in err


@pytest.mark.parametrize("line, expected", [
    ("tier_rank = 0", "tier_rank: expected positive integer, got 0"),
    ('tier_rank = "1"', 'tier_rank: expected positive integer, got "1"'),
    ("weight = 0", "weight: expected positive integer, got 0"),
    ("weight = 1.5", "weight: expected positive integer, got 1.5"),
    ('frame_classes = "task_request"',
     'frame_classes: expected string array, got "task_request"'),
])
def test_tier_entry_key_types(env, line, expected):
    key = line.split(" =")[0]
    kept = [row for row in TIER_ONE.splitlines() if not row.startswith(f"{key} =")]
    tiers = "\n" + "\n".join(kept + [line]) + "\n"
    errors = env.errors(project_text=gs_project(env, tier_body(tiers)))
    has(errors, f"[[generate.stream.tiers]][1].{expected}")


def test_tier_entry_requires_rank_and_weight(env):
    tiers = '\n[[generate.stream.tiers]]\nframe_classes = ["task_request"]\n'
    errors = env.errors(project_text=gs_project(env, tier_body(tiers)))
    has(errors, "[[generate.stream.tiers]][1].tier_rank: missing required key, expected "
                "positive integer")
    has(errors, "[[generate.stream.tiers]][1].weight: missing required key, expected "
                "positive integer")


def test_tier_table_shape_errors(env):
    generate = GS_GENERATE.replace("sessions = 2", "sessions = 2\ntiers = 3")
    errors = env.errors(project_text=gs_project(
        env, gs_body(generate=generate, frames=GS_FRAMES3)))
    has(errors, "[generate.stream].tiers: expected array of tables, got 3")
    generate = GS_GENERATE.replace("sessions = 2", 'sessions = 2\ntiers = ["x"]')
    errors = env.errors(project_text=gs_project(
        env, gs_body(generate=generate, frames=GS_FRAMES3)))
    has(errors, '[[generate.stream.tiers]][1]: expected table, got "x"')


def test_unknown_key_inside_a_tier_entry_warns(env, capsys):
    env.load(project_text=gs_project(
        env, tier_body(TIERS.replace("weight = 2", 'weight = 2\nname = "s"'))))
    assert "[[generate.stream.tiers]][1].name: unknown key" in capsys.readouterr().err


@pytest.mark.parametrize("swap, ranks", [
    (("tier_rank = 2", "tier_rank = 1"), "[1, 1]"),      # 重号
    (("tier_rank = 2", "tier_rank = 3"), "[1, 3]"),      # 缺号
    (("tier_rank = 1", "tier_rank = 3"), "[2, 3]"),      # 不从 1 起
])
def test_tier_ranks_must_be_unique_and_contiguous(env, swap, ranks):
    tiers = TIERS.replace(*swap)
    errors = env.errors(project_text=gs_project(env, tier_body(tiers)))
    has(errors, "[[generate.stream.tiers]].tier_rank: tier ranks must be unique and cover "
                "1..N contiguously (N = 2 = the number of tiers; the rank is the identity "
                f"of a tier, there is no name key), got {ranks}")


def test_tier_composition_must_be_nonempty(env):
    tiers = TIERS.replace('frame_classes = ["task_request", "followup"]\n',
                          "frame_classes = []\n", 1)
    errors = env.errors(project_text=gs_project(env, tier_body(tiers)))
    has(errors, "[[generate.stream.tiers]](tier_rank = 1).frame_classes: expected a "
                "non-empty array of frame class names (a tier IS its frame-class "
                "composition)")


def test_tier_composition_rejects_duplicates_within_a_tier(env):
    tiers = TIERS.replace('["task_request", "followup"]\n',
                          '["task_request", "task_request"]\n', 1)
    errors = env.errors(project_text=gs_project(env, tier_body(tiers)))
    has(errors, "[[generate.stream.tiers]](tier_rank = 1).frame_classes: frame class names "
                'must be distinct within a tier (the composition is a set), got duplicate '
                '"task_request"')


def test_tier_composition_names_must_be_in_the_frame_class_table(env):
    tiers = TIERS.replace('["task_request", "followup"]\n', '["task_request", "ghost"]\n', 1)
    errors = env.errors(project_text=gs_project(env, tier_body(tiers)))
    has(errors, '[[generate.stream.tiers]](tier_rank = 1).frame_classes: frame class name '
                '"ghost" is not in [[frame.classify.classes]], available: task_request, '
                "followup, confirmation")


def test_tier_compositions_must_be_pairwise_distinct(env):
    # 构成是集合：书写序不同、集合相同即语义重复
    tiers = TIERS.replace('["task_request", "followup", "confirmation"]',
                          '["followup", "task_request"]')
    errors = env.errors(project_text=gs_project(env, tier_body(tiers)))
    has(errors, "[[generate.stream.tiers]](tier_rank = 2).frame_classes: the composition is "
                "identical to the one of tier_rank = 1 - two tiers with the same "
                "frame-class set are semantically duplicates")


def test_len_range_must_cover_every_nonzero_quota_tier(env):
    generate = GS_GENERATE.replace("len_range = [3, 5]", "len_range = [2, 5]")
    errors = env.errors(project_text=gs_project(env, tier_body(generate=generate)))
    has(errors, "[class.ticket_booking.generate].len_range: the lower bound must be >= the "
                "composition size of every tier this class draws from (tier_rank = 2 "
                "declares 3 frame classes and is apportioned 1 of the 3 sequences, and each "
                "of them must appear at least once), got lower bound 2")
    # 构成大小 2 的第 1 档在下界 2 下照常放行——逐 (类, 档) 对裁定，不是全表取最大
    assert not any("tier_rank = 1 declares" in e for e in errors)


def test_a_class_that_does_not_participate_gets_no_quota_pair(env, capsys):
    # 有效 sequences = 0 的类整类跳过：既不发零额 WARN，也不裁定它的 len_range
    classify = GS_CLASSIFY + ('\n[[classify.classes]]\nname = "home_control"\n'
                              'description = "智能家居控制序列"\n')
    body = gs_body(classify=classify, generate=GS_GENERATE + TIERS, frames=GS_FRAMES3)
    cfg = env.load(project_text=gs_project(env, body))
    assert cfg.class_views["home_control"].generate.sequences == 0
    assert "home_control" not in capsys.readouterr().err


def test_zero_quota_pair_warns_and_is_exempt_from_the_length_rule(env, capsys):
    # 权重悬殊：3 条序列按 (5, 1) 最大余额法配分 = (3, 0) ⇒ 第 2 档零额
    tiers = TIERS.replace("weight = 2", "weight = 5")
    generate = GS_GENERATE.replace("len_range = [3, 5]", "len_range = [2, 5]")
    cfg = env.load(project_text=gs_project(env, tier_body(tiers, generate=generate)))
    assert cfg.class_views["ticket_booking"].generate.len_range == (2, 5)
    err = capsys.readouterr().err
    assert ('[[generate.stream.tiers]]: class "ticket_booking" apportions 0 sequences to '
            "tier_rank = 2" in err)
    assert "weights tier_rank 1: weight 5, tier_rank 2: weight 1" in err
    # 零额对豁免长度可覆盖（否则下界 2 < 第 2 档构成 3 会报错）
    assert "must be >= the composition size" not in err


def test_frame_class_outside_every_tier_warns_and_needs_no_instruction(env, capsys):
    # 裁决·指令必填域收窄：未入档的帧类永不被蓝图选中 ⇒ 生成面整体是死配置
    frames = GS_FRAMES + """
[[frame.classify.classes]]
name = "confirmation"
description = "确认下单的收尾帧"
"""
    cfg = env.load(project_text=gs_project(
        env, tier_body("\n" + TIER_ONE, frames=frames)))
    assert cfg.frame_class_views["confirmation"].gen_instruction is None
    assert ('[frame.class.confirmation.generate]: frame class "confirmation" is in no tier '
            "composition, so it can never be picked by a blueprint" in capsys.readouterr().err)


def test_frame_class_inside_a_tier_still_needs_its_instruction(env):
    frames = GS_FRAMES.replace(
        '[frame.class.followup.generate]\ninstruction = "生成一条补充信息的用户话语"\n', "")
    errors = env.errors(project_text=gs_project(
        env, tier_body("\n" + TIER_ONE, frames=frames)))
    has(errors, "[frame.class.followup.generate].instruction: every frame class must provide "
                "a non-empty generation instruction (the blueprint enum covers the union of "
                "the tier compositions, so any frame class of a tier may be picked)")


def test_frame_gap_lower_bound_has_a_microsecond_floor(env):
    # v1.14 裁决·微秒地板（v1.13 形态级缺陷修补）
    generate = GS_GENERATE.replace("frame_gap_s = [5, 60]", "frame_gap_s = [0.0000001, 60]")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream].frame_gap_s: the lower bound must be >= 1e-06 s (one "
                "microsecond)")
    has(errors, "must be strictly increasing")
    has(errors, "0.0 as its first/last frame boundary sentinel")
    generate_ok = GS_GENERATE.replace("frame_gap_s = [5, 60]", "frame_gap_s = [0.000001, 60]")
    cfg = env.load(project_text=gs_project(env, gs_body(generate=generate_ok)))
    assert cfg.generate_stream.frame_gap_s == (1e-06, 60.0)


def test_frame_gap_without_representable_microsecond_is_aggregated(env):
    """不可表示的微秒闭区间必须在 M1 聚合为 CONFIG_ERROR。"""
    generate = GS_GENERATE.replace("frame_gap_s = [5, 60]",
                                   "frame_gap_s = [0.0000011, 0.0000012]")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream].frame_gap_s: frame_gap_s has no representable "
                "microsecond value")


# ── v1.16 rules/windows 配置面 ─────────────────────────────────────────────

RULE_INIT = """\
[[generate.stream.rules]]
template = "init"
frame_class = "task_request"
"""

RULE_RESPONSE = """\
[[generate.stream.rules]]
template = "response"
source = "task_request"
target = "followup"
"""

WINDOW_REQUEST = """\
[[generate.stream.windows]]
frame_class = "task_request"
of_day = [["08:00", "11:00"], ["14:00", "17:00"]]
"""


def sequence_tables_body(*, rules: str = "", windows: str = "", generate: str = GS_GENERATE,
                         frames: str = GS_FRAMES) -> str:
    """在基础时间流配置的 generate.stream 与按类表之间插入 v1.16 表。"""
    marker = "\n[class.ticket_booking.generate]"
    inserted = f"\n{rules}\n{windows}" if rules or windows else ""
    assert marker in generate
    return gs_body(generate=generate.replace(marker, inserted + marker, 1), frames=frames)


def structured_correlation_frames(*, target_type: str = "string",
                                  source_required: bool = True,
                                  bind_subject: bool = False) -> str:
    """构造 correlation 两侧结构化生成 Schema。"""
    source_schema = {
        "type": "object",
        "properties": {"subject_id": {"type": "string"},
                        "utterance": {"type": "string"}},
        "required": ["subject_id", "utterance"],
        "additionalProperties": False,
    }
    target_schema = {
        "type": "object",
        "properties": {"subject_id": {"type": target_type},
                        "utterance": {"type": "string"}},
        "required": (["subject_id", "utterance"] if source_required
                      else ["utterance"]),
        "additionalProperties": False,
    }
    source_text = json.dumps(source_schema, ensure_ascii=False)
    target_text = json.dumps(target_schema, ensure_ascii=False)
    frames = GS_FRAMES.replace(
        'instruction = "生成一条发起购票任务的用户话语"',
        'instruction = "生成一条发起购票任务的用户话语"\n'
        f"schema_inline = '''\n{source_text}\n'''", 1)
    frames = frames.replace(
        'instruction = "生成一条补充信息的用户话语"',
        'instruction = "生成一条补充信息的用户话语"\n'
        f"schema_inline = '''\n{target_text}\n'''", 1)
    if bind_subject:
        frames += ('\n[frame.class.task_request.generate.time_fields]\n'
                   'subject_id = "ts"\n')
    return frames


def test_sequence_rule_and_window_tables_parse(env):
    cfg = env.load(project_text=gs_project(env, sequence_tables_body(
        rules=RULE_INIT + RULE_RESPONSE, windows=WINDOW_REQUEST)))
    assert cfg.generate_stream.rules == (
        SequenceRuleSpec(template="init", frame_class="task_request"),
        SequenceRuleSpec(template="response", source="task_request", target="followup"),
    )
    assert cfg.generate_stream.windows == (
        SequenceWindowSpec(frame_class="task_request",
                           of_day=(("08:00", "11:00"), ("14:00", "17:00"))),
    )


def test_sequence_rule_window_and_correlation_unknown_keys_warn(env, capsys):
    rule = RULE_RESPONSE.replace(
        'target = "followup"',
        'target = "followup"\nfuture_rule_key = true\n'
        'correlation = {operator = "equal", source_field = "subject_id", '
        'target_field = "subject_id", future_predicate = "x"}')
    windows = WINDOW_REQUEST.replace('of_day =', 'future_window_key = 1\nof_day =')
    frames = structured_correlation_frames()
    env.load(project_text=gs_project(env, sequence_tables_body(
        rules=rule, windows=windows, frames=frames)))
    err = capsys.readouterr().err
    assert "future_rule_key: unknown key" in err
    assert "correlation.future_predicate: unknown key" in err
    assert "future_window_key: unknown key" in err


def test_class_rules_and_windows_have_independent_three_state_tables(env):
    generate = GS_GENERATE + "\nrules = []\nwindows = []\n"
    cfg = env.load(project_text=gs_project(env, sequence_tables_body(
        generate=generate, rules=RULE_INIT, windows=WINDOW_REQUEST)))
    view = cfg.class_views["ticket_booking"]
    assert view.rules == () and view.windows == ()
    assert cfg.generate_stream.rules == (
        SequenceRuleSpec(template="init", frame_class="task_request"),)
    assert cfg.generate_stream.windows

    class_rule = """\
[[class.ticket_booking.generate.rules]]
template = "response"
source = "task_request"
target = "followup"
"""
    generate = GS_GENERATE + class_rule + "\n"
    cfg = env.load(project_text=gs_project(env, sequence_tables_body(
        generate=generate, rules=RULE_INIT, windows=WINDOW_REQUEST)))
    view = cfg.class_views["ticket_booking"]
    assert view.rules == (SequenceRuleSpec(template="response", source="task_request",
                                           target="followup"),)
    assert view.windows is None


def test_sequence_tables_and_validator_are_parked_when_form_is_off(env):
    body = ("[generate]\nsequence_validator = \"tests.hook_samples:ok\"\n"
            "\n[generate.stream]\nenabled = false\n\n" + RULE_INIT + WINDOW_REQUEST)
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[[generate.stream.rules]]: sequence rules are only legal")
    has(errors, "[[generate.stream.windows]]: sequence windows are only legal")
    has(errors, "[generate].sequence_validator: sequence_validator is only legal")


def test_sequence_validator_reference_is_resolved_in_enabled_form(env):
    body = sequence_tables_body(rules=RULE_INIT)
    body = body.replace("[generate]\nenabled = true", "[generate]\nenabled = true\n"
                        "sequence_validator = \"tests.hook_samples:sequence_ok\"", 1)
    cfg = env.load(project_text=gs_project(env, body))
    assert cfg.generate.sequence_validator == "tests.hook_samples:sequence_ok"

    bad = body.replace("tests.hook_samples:sequence_ok", "tests.hook_samples:missing_fn")
    errors = env.errors(project_text=gs_project(env, bad))
    has(errors, "[generate].sequence_validator: attribute 'missing_fn' not found")


@pytest.mark.parametrize(("hook", "expected"), [
    ("tests.hook_samples:ok", "hook must accept exactly one positional"),
    ("tests.hook_samples:sequence_bad_return", "dry-run returned an invalid value"),
    ("tests.hook_samples:sequence_boom", "dry-run raised RuntimeError"),
])
def test_sequence_validator_signature_and_dry_run_are_checked_at_startup(env, hook, expected):
    """M1 必须以代表性输入执行单参数钩子并规范化返回值。"""
    body = sequence_tables_body(rules=RULE_INIT)
    body = body.replace("[generate]\nenabled = true", "[generate]\nenabled = true\n"
                        f"sequence_validator = \"{hook}\"", 1)
    errors = env.errors(project_text=gs_project(env, body))
    has(errors, f"[generate].sequence_validator: {expected}")


def test_m1_reports_local_planner_infeasible_with_class_tier_and_length(env):
    """M1 局部矩阵把不可行状态聚合为含定位信息的 CONFIG_ERROR。"""
    impossible_rules = """\
[[generate.stream.rules]]
template = "init"
frame_class = "task_request"

[[generate.stream.rules]]
template = "exactly"
frame_class = "task_request"
count = 2

[[generate.stream.rules]]
template = "end"
frame_class = "followup"
"""
    tier = """\
[[generate.stream.tiers]]
tier_rank = 1
weight = 1
frame_classes = ["task_request", "followup"]
"""
    generate = GS_GENERATE.replace("len_range = [3, 5]", "len_range = [2, 2]")
    generate = generate.replace("\n[class.ticket_booking.generate]", f"\n{tier}\n"
                                "[class.ticket_booking.generate]", 1)
    errors = env.errors(project_text=gs_project(env, sequence_tables_body(
        generate=generate, rules=impossible_rules)))
    text = "\n".join(errors)
    has(errors, "[class.ticket_booking.generate].len_range: sequence planner found no "
                "feasible potential")
    has(errors, "tier_rank = 1")
    has(errors, "length = 2")
    has(errors, "INFEASIBLE")
    assert "payload" not in text


@pytest.mark.parametrize(
    ("status_name", "required", "forbidden"),
    [("UNKNOWN", "deterministic budget", ("infeasible", "no feasible", "unsatisfiable")),
     ("INFEASIBLE", "no feasible", ("deterministic budget",))],
)
def test_m1_planner_status_wording_is_closed(monkeypatch, env, status_name, required, forbidden):
    """M1 的 UNKNOWN 只表达预算未验证，INFEASIBLE 仍明确不可满足。"""
    from labelkit.common.runtime import sequence_planner

    status = sequence_planner.PlannerStatus[status_name]
    monkeypatch.setattr(sequence_planner, "check_question", lambda _question: status)

    def fail(*_args, **_kwargs):
        raise sequence_planner.PlannerConfigError(status_name)

    monkeypatch.setattr(sequence_planner, "select_feasible_plan", fail)
    errors = env.errors(project_text=gs_project(
        env, sequence_tables_body(rules=RULE_INIT)))
    text = "\n".join(errors).lower()
    assert required in text
    for word in forbidden:
        assert word not in text


def test_m1_model_invalid_is_public_internal_error(monkeypatch, env):
    """M1 的 MODEL_INVALID 必须只暴露公共 InternalError，且不带用户值。"""
    from labelkit.common.runtime import sequence_planner

    monkeypatch.setattr(sequence_planner, "_status_name",
                        lambda *_args: sequence_planner.PlannerStatus.MODEL_INVALID)
    with pytest.raises(InternalError) as caught:
        env.load(project_text=gs_project(env, sequence_tables_body(rules=RULE_INIT)))
    assert isinstance(caught.value, InternalError)
    assert str(caught.value) == "CP-SAT returned MODEL_INVALID"
    assert "payload" not in str(caught.value)


@pytest.mark.parametrize(("rule", "expected"), [
    ("template = \"missing\"\nframe_class = \"task_request\"",
     "expected \"existence\""),
    ("template = \"existence\"\nframe_class = \"task_request\"",
     "count: required for template existence"),
    ("template = \"response\"\nframe_class = \"task_request\"",
     "source and target are required"),
    ("template = \"response\"\nsource = \"task_request\"\n"
     "target = \"followup\"\ncount = 1", "count: forbidden"),
    ("template = \"response\"\nsource = \"task_request\"\n"
     "target = \"task_request\"", "source and target must name different"),
])
def test_sequence_rule_template_parameter_matrix(env, rule, expected):
    rules = f"[[generate.stream.rules]]\n{rule}\n"
    errors = env.errors(project_text=gs_project(env, sequence_tables_body(rules=rules)))
    has(errors, expected)


def test_duplicate_sequence_rules_are_rejected(env):
    rules = RULE_INIT + RULE_INIT
    errors = env.errors(project_text=gs_project(env, sequence_tables_body(rules=rules)))
    has(errors, "duplicate sequence rule declaration")


def test_global_rule_error_is_not_repeated_for_inheriting_classes(env):
    classify = GS_CLASSIFY + """
[[classify.classes]]
name = "smart_home"
description = "智能家居序列"
    """
    bad_rule = RULE_RESPONSE.replace('source = "task_request"', 'source = "ghost"')
    marker = "\n[class.ticket_booking.generate]"
    generate = GS_GENERATE.replace(marker, f"\n{bad_rule}{marker}", 1)
    body = gs_body(classify=classify, generate=generate)
    errors = env.errors(project_text=gs_project(env, body))
    assert sum("frame class \"ghost\" is not in [[frame.classify.classes]]" in e
               for e in errors) == 1


def test_rule_time_s_requires_exact_microseconds_and_half_open_range(env):
    rule = RULE_RESPONSE.replace(
        'target = "followup"', 'target = "followup"\ntime_s = [0.0000001, 1]')
    errors = env.errors(project_text=gs_project(env, sequence_tables_body(rules=rule)))
    has(errors, "time_s: expected number range array of length 2")
    rule = RULE_RESPONSE.replace(
        'target = "followup"', 'target = "followup"\ntime_s = [1, 1]')
    errors = env.errors(project_text=gs_project(env, sequence_tables_body(rules=rule)))
    has(errors, "time_s: expected non-empty half-open range")


def test_sequence_window_validation_sorts_before_overlap_check(env):
    windows = """\
[[generate.stream.windows]]
frame_class = "task_request"
of_day = [["14:00", "17:00"], ["08:00", "11:00"]]
"""
    cfg = env.load(project_text=gs_project(env, sequence_tables_body(windows=windows)))
    assert cfg.generate_stream.windows[0].of_day == (("14:00", "17:00"), ("08:00", "11:00"))

    overlap = windows.replace('["08:00", "11:00"]', '["10:00", "15:00"]')
    errors = env.errors(project_text=gs_project(env, sequence_tables_body(windows=overlap)))
    has(errors, "branches must not overlap")


def test_sequence_window_rejects_cross_midnight_duplicate_and_bad_weekday(env):
    windows = """\
[[generate.stream.windows]]
frame_class = "task_request"
of_day = [["22:00", "02:00"]]
of_week = ["mon", "mon"]
"""
    errors = env.errors(project_text=gs_project(env, sequence_tables_body(windows=windows)))
    has(errors, "window must satisfy start < end")
    has(errors, "weekday values must be distinct")
    bad_week = windows.replace('of_week = ["mon", "mon"]',
                               'of_week = ["mon", "noday"]')
    errors = env.errors(project_text=gs_project(env, sequence_tables_body(windows=bad_week)))
    has(errors, "expected \"mon\" | \"tue\"")


def test_correlation_requires_structured_required_same_type_and_unbound_fields(env):
    correlation = ("correlation = {operator = \"equal\", source_field = \"subject_id\", "
                   "target_field = \"subject_id\"}")
    rules = RULE_RESPONSE.replace("target = \"followup\"",
                                  "target = \"followup\"\n" + correlation)
    frames = structured_correlation_frames()
    cfg = env.load(project_text=gs_project(env, sequence_tables_body(rules=rules, frames=frames)))
    assert cfg.generate_stream.rules[0].correlation == CorrelationSpec(
        operator="equal", source_field="subject_id", target_field="subject_id")

    mismatch = structured_correlation_frames(target_type="number")
    errors = env.errors(project_text=gs_project(env, sequence_tables_body(
        rules=rules, frames=mismatch)))
    has(errors, "source_field and target_field must declare the same JSON Schema type")

    missing_required = structured_correlation_frames(source_required=False)
    errors = env.errors(project_text=gs_project(env, sequence_tables_body(
        rules=rules, frames=missing_required)))
    has(errors, "correlation.target_field")
    has(errors, "must be listed in the required array")

    bound = structured_correlation_frames(bind_subject=True)
    errors = env.errors(project_text=gs_project(env, sequence_tables_body(
        rules=rules, frames=bound)))
    has(errors, "must not be a bound time_fields property")


# ── v1.15 按类档位表（[[class.<name>.generate.tiers]]；SPEC-per-class-tiers §3.1）─

GS_CLASSIFY2 = GS_CLASSIFY + """
[[classify.classes]]
name = "smart_home"
description = "智能家居控制序列"
"""

SMART_HOME = """
[class.smart_home.generate]
instruction = "生成一段智能家居控制的用户请求序列"
sequences = 3
len_range = [3, 5]
"""

# 教学面（SPEC §3.5）：整表原子覆盖 + 构成与权重双差异（权重方向与全局表相反）
OWN_TIERS = ((1, 1, ("task_request", "followup")),
             (2, 2, ("task_request", "confirmation")))


def per_class_tiers(rows=OWN_TIERS, cname: str = "ticket_booking") -> str:
    """``[[class.<name>.generate.tiers]]`` 片段：逐行 (tier_rank, weight, 构成)。"""
    return "".join(
        f"\n[[class.{cname}.generate.tiers]]\ntier_rank = {rank}\nweight = {weight}\n"
        f"frame_classes = {json.dumps(list(names))}\n"
        for rank, weight, names in rows)


def mixed_body(*, own=OWN_TIERS, tiers: str = TIERS, generate: str = GS_GENERATE,
               smart_home: str = SMART_HOME, frames: str = GS_FRAMES3) -> str:
    """混合形态基线：ticket_booking 声明按类表、smart_home 回落全局表（各 3 条配额）。

    sessions 抬到 3 以满足 sessions <= Σsequences(6) <= 2 × sessions。
    """
    generate = generate.replace("sessions = 2", "sessions = 3") + smart_home
    own_toml = per_class_tiers(own) if own else ""
    return gs_body(classify=GS_CLASSIFY2, generate=generate + tiers + own_toml,
                   frames=frames)


def test_per_class_tier_table_overrides_the_global_one_atomically(env, capsys):
    cfg = env.load(project_text=gs_project(env, mixed_body()))
    assert cfg.class_views["ticket_booking"].tiers == (
        TierSpec(tier_rank=1, weight=1, frame_classes=("task_request", "followup")),
        TierSpec(tier_rank=2, weight=2,
                 frame_classes=("task_request", "confirmation")))
    assert cfg.class_views["smart_home"].tiers is None       # 未声明 ⇒ 回落全局
    assert cfg.generate_stream.tiers[1].frame_classes == (   # 全局表不被污染
        "task_request", "followup", "confirmation")
    assert "unknown key" not in capsys.readouterr().err      # 白名单第七键正例


def test_per_class_tier_table_is_stored_by_ascending_rank(env):
    # 生效表也走 tiers[rank - 1] 直取 ⇒ 存放序由 rank 定，而非书写序
    shuffled = tuple(reversed(OWN_TIERS))
    cfg = env.load(project_text=gs_project(env, mixed_body(own=shuffled)))
    tiers = cfg.class_views["ticket_booking"].tiers
    assert [t.tier_rank for t in tiers] == [1, 2]
    assert [t.weight for t in tiers] == [1, 2]


def test_per_class_tier_table_requires_the_time_stream_form(env):
    # rule 61①（parked 探针）：形态关闭时任何 [class.*.generate] 含 tiers 键即定向报错
    body = CLASSIFY_TWO + per_class_tiers(cname="qa")
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[class.qa.generate].tiers: the per-class tier table is only legal in the "
                "time-stream generation form ([generate.stream].enabled = true) - it "
                "overrides the global [[generate.stream.tiers]] table for sequences of "
                "this class")


def test_per_class_premise_probe_is_independent_of_the_parse(env):
    # 探针走原始节：表内容非法（解析产物为空）也要照发前提错误
    body = CLASSIFY_TWO + per_class_tiers(((0, 1, ("task_request",)),), cname="qa")
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[class.qa.generate].tiers: the per-class tier table is only legal in the "
                "time-stream generation form")
    has(errors, "[[class.qa.generate.tiers]][1].tier_rank: expected positive integer, got 0")


def test_per_class_tier_table_requires_the_global_anchor(env):
    # rule 61②：全局表是面开关兼未声明类的回落，缺席即 CONFIG_ERROR
    errors = env.errors(project_text=gs_project(env, mixed_body(tiers="")))
    has(errors, "[class.ticket_booking.generate].tiers: a per-class tier table overrides "
                "the global [[generate.stream.tiers]] table, which is absent - declare the "
                "global table (it is the fallback for classes without their own table and "
                "the switch of the whole tier face)")


def test_per_class_empty_tier_table_is_rejected(env):
    # rule 61③：显式空表在面统一下没有合法语义（"本类无档"态不存在）
    generate = GS_GENERATE + "tiers = []\n"
    errors = env.errors(project_text=gs_project(env, tier_body(generate=generate)))
    has(errors, "[class.ticket_booking.generate].tiers: expected a non-empty array of tier "
                "tables - omit the key to fall back to the global "
                "[[generate.stream.tiers]] table")


def test_per_class_tier_table_shape_errors_name_the_class(env):
    # 定位串参数化：整表形状错误落键级定位 [class.<name>.generate].tiers，
    # 行级错误落该表的表数组头（用户写的就是这个头）
    generate = GS_GENERATE + "tiers = 3\n"
    errors = env.errors(project_text=gs_project(env, tier_body(generate=generate)))
    has(errors, "[class.ticket_booking.generate].tiers: expected array of tables, got 3")
    generate = GS_GENERATE + 'tiers = ["x"]\n'
    errors = env.errors(project_text=gs_project(env, tier_body(generate=generate)))
    has(errors, '[[class.ticket_booking.generate.tiers]][1]: expected table, got "x"')


def test_rule_61_sub_clauses_2_and_3_are_mutually_exclusive(env):
    # rule 61②/③ 互斥（实现期裁决 2026-08-19）：同一个键一条错误一个修复动作——
    # 空表的修复动作（删键）与锚缺失的修复动作（补全局表）不同，叠报会误导
    generate = GS_GENERATE + "tiers = []\n"
    errors = env.errors(project_text=gs_project(env, tier_body("", generate=generate)))
    has(errors, "[class.ticket_booking.generate].tiers: expected a non-empty array of tier "
                "tables - omit the key to fall back to the global "
                "[[generate.stream.tiers]] table")
    assert not any("which is absent" in e for e in errors)


def test_a_shape_failed_per_class_table_lands_as_undeclared(env):
    # 形状错误（非数组）已在解析层报出且修复动作明确——按未声明落库，
    # rule 61 的空表错与锚错都不叠报（全局表缺席也不报锚错）
    generate = GS_GENERATE + "tiers = 3\n"
    errors = env.errors(project_text=gs_project(env, tier_body("", generate=generate)))
    has(errors, "[class.ticket_booking.generate].tiers: expected array of tables, got 3")
    assert not any("non-empty array of tier tables" in e for e in errors)
    assert not any("which is absent" in e for e in errors)


def test_each_effective_table_covers_1_to_n_on_its_own(env):
    # 裁决·rank 类内身份：每张生效表各自连续覆盖 1..N，N 可逐类不同
    cfg = env.load(project_text=gs_project(
        env, mixed_body(own=((1, 1, ("task_request", "followup")),))))
    assert [t.tier_rank for t in cfg.class_views["ticket_booking"].tiers] == [1]
    assert [t.tier_rank for t in cfg.generate_stream.tiers] == [1, 2]
    # 反例：按类表自己缺号（定位串带类名）
    gapped = ((1, 1, ("task_request", "followup")),
              (3, 2, ("task_request", "confirmation")))
    errors = env.errors(project_text=gs_project(env, mixed_body(own=gapped)))
    has(errors, "[class.ticket_booking.generate].tiers.tier_rank: tier ranks must be unique "
                "and cover 1..N contiguously (N = 2 = the number of tiers; the rank is the "
                "identity of a tier, there is no name key), got [1, 3]")


def test_same_composition_is_legal_across_tables_but_not_within_one(env):
    # 跨类同构成合法（各类都可有自己的「全类档」）——基线按类表第 1 档就与全局第 1 档同构成
    cfg = env.load(project_text=gs_project(env, mixed_body()))
    assert (cfg.class_views["ticket_booking"].tiers[0].frame_classes
            == cfg.generate_stream.tiers[0].frame_classes)
    # 单表之内照旧两两互异（构成是集合：书写序不同、集合相同即语义重复）
    dup = ((1, 1, ("task_request", "followup")),
           (2, 2, ("followup", "task_request")))
    errors = env.errors(project_text=gs_project(env, mixed_body(own=dup)))
    has(errors, "[class.ticket_booking.generate].tiers(tier_rank = 2).frame_classes: the "
                "composition is identical to the one of tier_rank = 1 - two tiers with the "
                "same frame-class set are semantically duplicates")


def test_quota_pairs_read_the_effective_table_of_each_class(env):
    # 下界裁定吃本类生效表：按类表第 2 档构成 3 类 vs 本类下界 2 ⇒ 错误指向声明类；
    # 回落全局表的 smart_home（下界 3）不受牵连
    own = ((1, 1, ("task_request", "followup")),
           (2, 2, ("task_request", "followup", "confirmation")))
    generate = GS_GENERATE.replace("len_range = [3, 5]", "len_range = [2, 5]")
    errors = env.errors(project_text=gs_project(
        env, mixed_body(own=own, generate=generate)))
    has(errors, "[class.ticket_booking.generate].len_range: the lower bound must be >= the "
                "composition size of every tier this class draws from (tier_rank = 2 "
                "declares 3 frame classes and is apportioned 2 of the 3 sequences, and each "
                "of them must appear at least once), got lower bound 2")
    assert not any("[class.smart_home.generate].len_range" in e for e in errors)


def test_zero_quota_warning_lists_the_weights_of_the_effective_table(env, capsys):
    # 按类权重悬殊：3 条按 (5, 1) 最大余额法配分 = (3, 0) ⇒ 本类第 2 档零额；
    # 回落全局表的 smart_home 按 (2, 1) 配成 (2, 1)，无零额
    own = ((1, 5, ("task_request", "followup")),
           (2, 1, ("task_request", "confirmation")))
    env.load(project_text=gs_project(env, mixed_body(own=own)))
    err = capsys.readouterr().err
    assert ('[[generate.stream.tiers]]: class "ticket_booking" apportions 0 sequences to '
            "tier_rank = 2" in err)
    assert "weights tier_rank 1: weight 5, tier_rank 2: weight 1" in err
    assert "smart_home" not in err


def test_dead_config_domain_is_the_union_of_the_effective_tables(env, capsys):
    # 裁决·校验域并集化：两类都声明按类表 ⇒ 全局表沦为纯锚，其独有帧类判死配置
    # （连生成指令都不必写），检查域精确反映"哪些帧类真会被蓝图选中"
    frames = GS_FRAMES + """
[[frame.classify.classes]]
name = "confirmation"
description = "确认下单的收尾帧"
"""
    own = ((1, 1, ("task_request", "followup")),)
    body = mixed_body(own=own, frames=frames,
                      smart_home=SMART_HOME + per_class_tiers(own, "smart_home"))
    cfg = env.load(project_text=gs_project(env, body))
    assert cfg.frame_class_views["confirmation"].gen_instruction is None
    assert ('[frame.class.confirmation.generate]: frame class "confirmation" is in no tier '
            "composition, so it can never be picked by a blueprint" in capsys.readouterr().err)
    # 反证：只要**某个**参与类的生效表收了它，指令就重新必填
    own_cf = ((1, 1, ("task_request", "confirmation")),)
    errors = env.errors(project_text=gs_project(
        env, mixed_body(own=own_cf, frames=frames,
                        smart_home=SMART_HOME + per_class_tiers(own, "smart_home"))))
    has(errors, "[frame.class.confirmation.generate].instruction: every frame class must "
                "provide a non-empty generation instruction (the blueprint enum covers the "
                "union of the tier compositions, so any frame class of a tier may be picked)")


def test_a_zero_quota_class_still_gets_its_table_structurally_checked(env, capsys):
    # 裁决·零额结构校验不豁免：坏配置早报，但配额对与零额 WARN 照旧豁免
    zero = SMART_HOME.replace("sequences = 3", "sequences = 0")
    ghost = ((1, 1, ("task_request", "ghost")),)
    errors = env.errors(project_text=gs_project(env, mixed_body(
        smart_home=zero + per_class_tiers(ghost, "smart_home"))))
    has(errors, '[class.smart_home.generate].tiers(tier_rank = 1).frame_classes: frame class '
                'name "ghost" is not in [[frame.classify.classes]], available: task_request, '
                "followup, confirmation")
    assert not any("[class.smart_home.generate].len_range" in e for e in errors)
    assert 'class "smart_home" apportions 0 sequences' not in capsys.readouterr().err


# ── v1.14 时间字段绑定表（[frame.class.<name>.generate.time_fields]）──────────

TIME_GEN_PROPS = {"utterance": {"type": "string"}, "duration": {"type": "number"}}


def bind_frames(binding: str = 'duration = "gap_next_s"',
                props: dict | None = None, **schema_extra) -> str:
    """A frames block whose task_request frame is structured AND carries a binding."""
    schema = json.dumps({"type": "object",
                         "properties": TIME_GEN_PROPS if props is None else props,
                         "additionalProperties": False, **schema_extra},
                        ensure_ascii=False)
    return frames_with_schema(f"schema_inline = '''\n{schema}\n'''\n\n"
                              f"[frame.class.task_request.generate.time_fields]\n{binding}")


def test_time_fields_parsed_into_the_frame_class_view(env, capsys):
    cfg = env.load(project_text=gs_project(env, gs_body(frames=bind_frames())))
    assert cfg.frame_class_views["task_request"].time_fields == {"duration": "gap_next_s"}
    assert cfg.frame_class_views["followup"].time_fields is None    # 无绑定
    assert "unknown key" not in capsys.readouterr().err             # 白名单已扩键


def test_time_fields_accept_the_whole_vocabulary_on_one_frame(env):
    props = {"utterance": {"type": "string"}, "duration": {"type": "number"},
             "since_start": {"type": "number"}, "waited": {"type": "number"},
             "at": {"type": "string"}}
    frames = bind_frames('duration = "gap_next_s"\nsince_start = "elapsed_s"\n'
                         'waited = "gap_prev_s"\nat = "ts"', props=props)
    cfg = env.load(project_text=gs_project(env, gs_body(frames=frames)))
    assert cfg.frame_class_views["task_request"].time_fields == {
        "duration": "gap_next_s", "since_start": "elapsed_s",
        "waited": "gap_prev_s", "at": "ts"}


def test_time_fields_require_a_structured_frame(env):
    frames = frames_with_schema('[frame.class.task_request.generate.time_fields]\n'
                                'duration = "gap_next_s"')
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, "[frame.class.task_request.generate.time_fields]: a time-field binding is "
                "only legal on a structured frame class - declare schema_path / "
                "schema_inline for [frame.class.task_request.generate] first")
    has(errors, "the payload must always be a JSON object")


def test_time_fields_stay_silent_when_the_schema_itself_is_unusable(env):
    # 顶层联合类型：生成 Schema 装载期就地报错（回填要求载荷恒为对象）——绑定簇不
    # 叠加"仅结构化帧合法"那条误导性第二错
    frames = bind_frames(props=TIME_GEN_PROPS)
    frames = frames.replace('{"type": "object"', '{"type": ["object", "null"]')
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, "[frame.class.task_request.generate].schema_inline: frame-class generation "
                'schema top-level type must be "object", got ["object", "null"]')
    assert not any("only legal on a structured frame class" in e for e in errors)


def test_time_fields_key_must_be_a_top_level_property(env):
    frames = bind_frames('elapsed = "elapsed_s"')
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, '[frame.class.task_request.generate.time_fields].elapsed: "elapsed" is not a '
                "top-level property of the frame-class generation schema, available: "
                "utterance, duration")


def test_time_fields_value_must_be_in_the_frozen_vocabulary(env):
    frames = bind_frames('duration = "gap_s"')
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, "[frame.class.task_request.generate.time_fields].duration: expected one of "
                "the time vocabulary terms ts, gap_prev_s, gap_next_s, elapsed_s (a frozen "
                'closed set), got "gap_s"')


@pytest.mark.parametrize("prop, got", [
    ({"type": "string"}, '"string"'),                       # number 词绑到串字段
    ({"type": ["number", "null"]}, '["number", "null"]'),   # 联合类型数组
    ({}, "null"),                                           # 缺失 type
    ({"$ref": "#/$defs/secs"}, "null"),                     # 经 $ref 间接声明
    ({"anyOf": [{"type": "number"}]}, "null"),              # 经组合关键字间接声明
])
def test_time_fields_type_must_match_literally(env, prop, got):
    props = {"utterance": {"type": "string"}, "duration": prop}
    frames = bind_frames(props=props, **{"$defs": {"secs": {"type": "number"}}})
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, f'[frame.class.task_request.generate.time_fields].duration: the bound '
                f'property must declare "type": "number" literally for the term '
                f'"gap_next_s" (a union type array, a missing type and an indirect '
                f"declaration through $ref or a combining keyword all count as a "
                f"mismatch), got {got}")


def test_time_fields_ts_term_requires_a_string_property(env):
    frames = bind_frames('duration = "ts"')
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, '[frame.class.task_request.generate.time_fields].duration: the bound '
                'property must declare "type": "string" literally for the term "ts"')


def test_time_fields_extra_keywords_warn_value_free(env, capsys):
    props = {"utterance": {"type": "string"},
             "duration": {"type": "number", "minimum": 0, "description": "本帧时长"}}
    env.load(project_text=gs_project(env, gs_body(frames=bind_frames(props=props))))
    err = capsys.readouterr().err
    for keyword in ('"minimum"', '"description"'):
        assert ("[frame.class.task_request.generate.time_fields].duration: the bound field "
                f"carries the keyword {keyword}, which is neither sent to the LLM nor "
                "enforced" in err)
    assert "本帧时长" not in err                     # 值-free：只点名关键字


def test_time_fields_must_leave_one_property_for_the_llm(env):
    frames = bind_frames(props={"duration": {"type": "number"}})
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, "[frame.class.task_request.generate.time_fields]: the bindings would remove "
                "every top-level property of the frame-class generation schema (top-level "
                "properties: 1, bound: 1)")


def test_time_fields_table_shape_errors(env):
    schema = json.dumps({"type": "object", "properties": TIME_GEN_PROPS},
                        ensure_ascii=False)
    frames = frames_with_schema(f"schema_inline = '''\n{schema}\n'''\ntime_fields = 3")
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, "[frame.class.task_request.generate.time_fields]: expected table, got 3")
    frames = bind_frames("duration = 3")
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, "[frame.class.task_request.generate.time_fields].duration: expected string "
                "(a time vocabulary term), got 3")


def test_full_planner_activation_uses_the_limited_quota_prefix():
    """`--limit` 完全截掉约束类时不激活全流 planner。"""
    from labelkit.common.config._generate_stream_constraints import (
        _has_nonzero_constraints,
    )

    clear = SimpleNamespace(generate=SimpleNamespace(sequences=1), rules=(), windows=())
    constrained = SimpleNamespace(
        generate=SimpleNamespace(sequences=1),
        rules=(SequenceRuleSpec(template="init", frame_class="task_request"),),
        windows=(),
    )
    stream = SimpleNamespace(rules=(), windows=())
    values = SimpleNamespace(class_views={"alpha": clear, "zeta": constrained}, limit=1)
    assert not _has_nonzero_constraints(stream, values)
    assert _has_nonzero_constraints(stream, SimpleNamespace(
        class_views=values.class_views, limit=2))

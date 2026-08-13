"""Offline unit tests for the v1.13 time-stream generation form in M1
(labelkit/common/config/loader.py; dev spec SPEC-stream-generation.md §3.1).

Covers the whole M1 constraint table row by row (positive AND negative): the
form premise conjunction, the relaxed sequence-class rules, the directed
forbidden-key probes, packing consistency, the weaving caps, the parked-section
waivers, the classify.llm reference-set waiver, the S29 empty-selector
extension, the per-class annotate Schema surface (which is form-INDEPENDENT)
and the frame-class generate Schema surface. Pure config logic — zero LLM.
"""
from __future__ import annotations

import json

import pytest

from labelkit.common.config import ResolvedConfig
from labelkit.common.config.model import GenerateStreamConfig
from labelkit.common.errors import ConfigError
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
    assert "时间流形态" not in capsys.readouterr().err


def test_generate_stream_section_must_be_a_table(env):
    generate = '[generate]\nenabled = true\nstream = 3\ninstruction = "生成"\n'
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate].stream: 期望表（table）")


def test_unknown_key_inside_the_form_section_warns(env, capsys):
    body = GS_GENERATE.replace("sessions = 2", "sessions = 2\nfuture_knob = 1")
    env.load(project_text=gs_project(env, gs_body(generate=body)))
    assert "[generate.stream].future_knob: 未知键" in capsys.readouterr().err


# ── 形态前提合取（SPEC §3.1 约束表第一行，逐分量反例）────────────────────────


def test_premise_requires_generate_only(env):
    errors = env.errors(project_text=gs_project(env, run_extra=""))
    has(errors, '[generate.stream].enabled: 时间流形态要求 run.mode = '
                '"generate_only"')
    has(errors, "本形态从零合成时间流，不消费输入数据")


def test_premise_requires_text_modality(env):
    project = gs_project(env, modality="ui")
    errors = env.errors(project_text=project)
    has(errors, '[generate.stream].enabled: 时间流形态要求 run.modality = "text"')


def test_premise_requires_generate_enabled(env):
    generate = GS_GENERATE.replace("[generate]\nenabled = true",
                                   "[generate]\nenabled = false")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream].enabled: 时间流形态要求 generate.enabled = true")


def test_premise_requires_classify_enabled(env):
    classify = GS_CLASSIFY.replace("enabled = true", "enabled = false")
    errors = env.errors(project_text=gs_project(env, gs_body(classify=classify)))
    has(errors, "[generate.stream].enabled: 时间流形态要求 classify.enabled = true")
    has(errors, "inherited")


def test_premise_requires_meta_order_by(env):
    for stream in ('[stream]\ngap_s = 900\n',
                   '[stream]\norder_by = "input_order"\ngap_s = 900\n'):
        errors = env.errors(project_text=gs_project(env, gs_body(stream=stream)))
        has(errors, '[stream].order_by: 时间流形态要求 "meta:<字段名>"')


def test_premise_requires_meta_mode_not_none(env):
    body = GS_BODY + f"\n[output]\nmeta_mode = \"none\"\nschema_inline = '''\n{SCHEMA}\n'''\n"
    errors = env.errors(project_text=gs_project(env, body, include_output=False))
    has(errors, '[output].meta_mode: 时间流形态下不得为 "none"')
    # sidecar 合法
    cfg = env.load(project_text=gs_project(
        env, body.replace('meta_mode = "none"', 'meta_mode = "sidecar"'),
        include_output=False))
    assert cfg.output.meta_mode == "sidecar"


def test_premise_rejects_dotted_artifact_field_names(env):
    # 工件行以字段名为字面顶层键，M2 按点路径解析——带点即不可往返（v1.13 工件键守卫）
    stream = GS_STREAM.replace('order_by = "meta:ts"', 'order_by = "meta:meta.ts"')
    errors = env.errors(project_text=gs_project(env, gs_body(stream=stream)))
    has(errors, '[stream].order_by: 时间流形态的时间戳字段名不得含 "."')
    body = GS_BODY + '\n[input]\ntext_field = "conversation.text"\n'
    errors = env.errors(project_text=gs_project(env, body))
    has(errors, '[input].text_field: 时间流形态的文本字段名不得含 "."')


def test_premise_rejects_artifact_key_collisions(env):
    # 工件行三个顶层键（ts 字段 / 文本字段 / truth）互斥
    body = GS_BODY + '\n[input]\ntext_field = "ts"\n'
    errors = env.errors(project_text=gs_project(env, body))
    has(errors, "[input].text_field: 时间流形态下不得与 [stream].order_by 的时间戳字段同名")
    body = GS_BODY + '\n[input]\ntext_field = "truth"\n'
    errors = env.errors(project_text=gs_project(env, body))
    has(errors, '[input].text_field: 时间流形态下字段名不得为 "truth"')
    stream = GS_STREAM.replace('order_by = "meta:ts"', 'order_by = "meta:truth"')
    errors = env.errors(project_text=gs_project(env, gs_body(stream=stream)))
    has(errors, '[stream].order_by: 时间流形态下字段名不得为 "truth"')


# ── 类表与配额（≥1 类放宽 / fallback 免填 / 有效 sequences / 参与类指令）──────


def test_single_sequence_class_is_legal_in_the_form_only(env):
    cfg = env.load(project_text=gs_project(env))
    assert [c.name for c in cfg.classify.classes] == ["ticket_booking"]
    # 对照：非本形态的单类表仍被 ≥ 2 规则拒绝
    plain = GS_CLASSIFY + '\nfallback_class = "ticket_booking"\n'
    errors = env.errors(project_text=env.project(body=plain))
    has(errors, "[classify].classes: classify.enabled = true 时须声明 ≥ 2 个类别")


def test_fallback_class_optional_in_the_form_but_checked_when_written(env):
    cfg = env.load(project_text=gs_project(env))
    assert cfg.classify.fallback_class == ""         # 免填（inherited 无判决路径）
    classify = GS_CLASSIFY.replace("enabled = true",
                                   'enabled = true\nfallback_class = "ghost"')
    errors = env.errors(project_text=gs_project(env, gs_body(classify=classify)))
    has(errors, '[classify].fallback_class: 引用的类名 "ghost" 不在 '
                "[[classify.classes]] 中，可用：ticket_booking")
    classify_ok = GS_CLASSIFY.replace(
        "enabled = true", 'enabled = true\nfallback_class = "ticket_booking"')
    cfg = env.load(project_text=gs_project(env, gs_body(classify=classify_ok)))
    assert cfg.classify.fallback_class == "ticket_booking"


def test_at_least_one_class_needs_a_positive_quota(env):
    generate = GS_GENERATE.replace("sequences = 3", "sequences = 0")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[class.<name>.generate].sequences: 时间流形态要求至少一个序列类的"
                "有效 sequences ≥ 1")
    has(errors, "得到各类合计 0")


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
    has(errors, "[class.ticket_booking.generate].instruction: 参与生成的序列类"
                "（有效 sequences = 3）须提供非空生成指令")
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
    has(errors, "[generate].instruction: generate.enabled = true 时必填")


def test_frame_class_table_must_be_nonempty_and_fully_instructed(env):
    errors = env.errors(project_text=gs_project(env, gs_body(frames="")))
    has(errors, "[[frame.classify.classes]]: 时间流形态要求非空帧类表")
    # 帧类在表里但缺 generate 节
    frames = GS_FRAMES.replace(
        '[frame.class.followup.generate]\ninstruction = "生成一条补充信息的用户话语"\n', "")
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, "[frame.class.followup.generate].instruction: 每个帧类都须提供非空"
                "生成指令")
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
    has(errors, f"[generate].{key}: 时间流形态不提供该键")
    has(errors, "[class.<name>.generate].sequences")     # 指引指向替代面


@pytest.mark.parametrize("line, key", [
    ("num_per_record = 2", "num_per_record"),
    ("seeds_per_call = 3", "seeds_per_call"),
])
def test_class_generate_forbidden_keys_are_directed_errors(env, line, key):
    generate = GS_GENERATE.replace("sequences = 3", f"sequences = 3\n{line}")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, f"[class.ticket_booking.generate].{key}: 时间流形态不提供该键")


def test_class_generate_seeds_per_call_also_hits_the_whitelist(env):
    # seeds_per_call 从来不在 [class.*.generate] 白名单内（无条件规则），本形态的
    # 定向探针叠加其上 ⇒ 两条互补的错误行：一条说"该键根本不可按类覆盖"，一条说
    # "本形态另有配额面"。聚合式上报，两条都要在。
    generate = GS_GENERATE.replace("sequences = 3", "sequences = 3\nseeds_per_call = 3")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[class.ticket_booking.generate].seeds_per_call: "
                "[class.*.generate] 不可覆盖该键")
    has(errors, "[class.ticket_booking.generate].seeds_per_call: 时间流形态不提供该键")


def test_frame_switches_are_mutually_exclusive_with_the_form(env):
    frames = GS_FRAMES + '\n[frame.classify]\nenabled = true\nfallback_class = "followup"\n'
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, "[frame.classify].enabled: 与时间流形态互斥")
    has(errors, "[frame.class.<name>.generate]")
    frames = GS_FRAMES + ("\n[frame.annotate]\nenabled = true\ninstruction = \"标\"\n"
                          f"schema_inline = '''\n{FRAME_GEN_SCHEMA}\n'''\n")
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, "[frame.annotate].enabled: 与时间流形态互斥")


# ── 装箱一致性（sessions / duplicates / noise / frame_gap_s）────────────────


def test_sessions_must_be_positive(env):
    generate = GS_GENERATE.replace("sessions = 2", "sessions = 0")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream].sessions: 期望 ≥ 1 的整数（会话数），得到 0")


@pytest.mark.parametrize("sessions", [1, 4])
def test_sessions_bracket_sequence_total(env, sessions):
    # Σsequences = 3 ⇒ 合法 sessions ∈ {2, 3}（sessions ≤ Σ ≤ 2 × sessions）
    generate = GS_GENERATE.replace("sessions = 2", f"sessions = {sessions}")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream].sessions: 期望 sessions ≤ Σsequences ≤ "
                "2 × sessions")
    has(errors, f"得到 sessions = {sessions}、Σsequences = 3")


@pytest.mark.parametrize("sessions", [2, 3])
def test_sessions_bracket_accepts_the_bounds(env, sessions):
    generate = GS_GENERATE.replace("sessions = 2", f"sessions = {sessions}")
    cfg = env.load(project_text=gs_project(env, gs_body(generate=generate)))
    assert cfg.generate_stream.sessions == sessions


def test_duplicates_bounded_by_sequence_total(env):
    generate = GS_GENERATE.replace("duplicates = 1", "duplicates = 4")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream].duplicates: 期望 [0, Σsequences] 内的整数")
    for value in (0, 3):
        generate = GS_GENERATE.replace("duplicates = 1", f"duplicates = {value}")
        cfg = env.load(project_text=gs_project(env, gs_body(generate=generate)))
        assert cfg.generate_stream.duplicates == value


@pytest.mark.parametrize("value", ["1.0", "1.5", "-0.1"])
def test_noise_ratio_half_open_unit_interval(env, value):
    generate = GS_GENERATE.replace("noise_ratio = 0.1", f"noise_ratio = {value}")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream].noise_ratio: 期望 [0,1) 内的数值")


def test_noise_instruction_required_only_above_zero(env):
    generate = GS_GENERATE.replace(
        'noise_instruction = "生成一条与任务无关的闲聊消息"\n', "")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream].noise_instruction: noise_ratio > 0 时必填")
    zero = generate.replace("noise_ratio = 0.1", "noise_ratio = 0.0")
    cfg = env.load(project_text=gs_project(env, gs_body(generate=zero)))
    assert cfg.generate_stream.noise_ratio == 0.0
    assert cfg.generate_stream.noise_instruction == ""


@pytest.mark.parametrize("value", ["[0, 60]", "[60, 5]", "[5]", '"5-60"',
                                   "[5, 60, 90]"])
def test_frame_gap_structural_errors(env, value):
    generate = GS_GENERATE.replace("frame_gap_s = [5, 60]", f"frame_gap_s = {value}")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream].frame_gap_s: 期望长度为 2 的数值区间数组 "
                "[lo, hi]（0 < lo ≤ hi，单位秒）")


def test_frame_gap_upper_bound_below_session_gap(env):
    generate = GS_GENERATE.replace("frame_gap_s = [5, 60]", "frame_gap_s = [5, 900]")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream].frame_gap_s: 上界须 < stream.gap_s（= 900")
    generate = GS_GENERATE.replace("frame_gap_s = [5, 60]", "frame_gap_s = [5, 899]")
    cfg = env.load(project_text=gs_project(env, gs_body(generate=generate)))
    assert cfg.generate_stream.frame_gap_s == (5.0, 899.0)


def test_frame_gap_defaults_when_absent(env):
    generate = GS_GENERATE.replace("frame_gap_s = [5, 60]\n", "")
    cfg = env.load(project_text=gs_project(env, gs_body(generate=generate)))
    assert cfg.generate_stream.frame_gap_s == (5.0, 60.0)


# ── 织造上限（session_max_len / key / gap_steps / span）─────────────────────


def test_session_max_len_covers_two_longest_sequences(env):
    stream = GS_STREAM + "session_max_len = 9\n"
    errors = env.errors(project_text=gs_project(env, gs_body(stream=stream)))
    has(errors, "[stream].session_max_len: 时间流形态要求 ≥ 2 × max(len_range 上界)")
    has(errors, "得到 9 < 10")
    stream_ok = GS_STREAM + "session_max_len = 10\n"
    cfg = env.load(project_text=gs_project(env, gs_body(stream=stream_ok)))
    assert cfg.stream.session_max_len == 10


def test_partition_key_and_gap_steps_must_be_neutral(env):
    stream = GS_STREAM + 'key = ["meta:user"]\ngap_steps = 5\n'
    errors = env.errors(project_text=gs_project(env, gs_body(stream=stream)))
    has(errors, "[stream].key: 时间流形态要求空数组")
    has(errors, "[stream].gap_steps: 时间流形态要求 0")


def test_session_span_static_check(env):
    stream = GS_STREAM + "session_max_len = 10\nsession_max_span_s = 500\n"
    errors = env.errors(project_text=gs_project(env, gs_body(stream=stream)))
    has(errors, "[stream].session_max_span_s: 最坏会话跨度 "
                "(session_max_len − 1) × frame_gap_s 上界 = 540 秒 > 500 秒")
    stream_ok = GS_STREAM + "session_max_len = 10\nsession_max_span_s = 540\n"
    cfg = env.load(project_text=gs_project(env, gs_body(stream=stream_ok)))
    assert cfg.stream.session_max_span_s == 540


def test_ts_start_must_be_iso8601_and_defaults_without_wall_clock(env):
    generate = GS_GENERATE.replace('ts_start = "2026-01-01T09:00:00+08:00"',
                                   'ts_start = "昨天早上"')
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[generate.stream].ts_start: 期望可解析的 ISO-8601 时刻")
    generate = GS_GENERATE.replace('ts_start = "2026-01-01T09:00:00+08:00"\n', "")
    cfg = env.load(project_text=gs_project(env, gs_body(generate=generate)))
    assert cfg.generate_stream.ts_start == "2026-01-01T00:00:00Z"


# ── 停放豁免（[stream] / [frame]）与引用集豁免（classify.llm）───────────────


def test_stream_and_frame_sections_are_not_parked_in_the_form(env, capsys):
    env.load(project_text=gs_project(env))
    err = capsys.readouterr().err
    assert "不会生效" not in err
    assert "[stream]" not in err and "[frame]" not in err


def test_same_sections_are_parked_without_the_form(env, capsys):
    body = ('[stream]\norder_by = "meta:ts"\n\n'
            '[frame.classify]\nllm = "judge"\n')
    env.load(project_text=env.project(body=body))
    err = capsys.readouterr().err
    assert "[segment].enabled" in err and "不会生效" in err
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
    has(errors, '[classify].llm: 引用的 profile "ghost" 不存在')


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
    has(errors, '环境变量 "LK_TEST_KEY_JUDGE" 未设置或为空')


# ── S29 扩展（空 rubric 选择子）────────────────────────────────────────────


def test_empty_rubric_selector_resolves_to_trajectory(env, capsys):
    cfg = env.load(project_text=gs_project(env))
    assert cfg.quality.rubric == "default:trajectory"
    assert cfg.class_views["ticket_booking"].quality.rubric == "default:trajectory"
    assert cfg.class_views["ticket_booking"].rubric is cfg.rubric
    # extract 在本形态不可用，故 S29 的「请启用 [extract]」组合 advisory 不适用
    assert "帧摘要" not in capsys.readouterr().err


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
    has(errors, "[class.qa.annotate].schema_path: 无法读取 Schema 文件")


def test_class_annotate_schema_at_most_one_source(env):
    errors = env.errors(project_text=env.project(body=class_schema_body(
        f"schema_path = \"x.json\"\nschema_inline = '''\n{CLASS_SCHEMA}\n'''")))
    has(errors, "[class.qa.annotate].schema_inline: 与 schema_path 恰好提供其一"
                "（互斥），得到两者均设置")


def test_class_annotate_schema_meta_validation_branches(env):
    errors = env.errors(project_text=env.project(
        body=class_schema_body("schema_inline = '{bad'")))
    has(errors, "[class.qa.annotate].schema_inline: 期望合法 JSON")
    errors = env.errors(project_text=env.project(
        body=class_schema_body("schema_inline = '[1, 2]'")))
    has(errors, "[class.qa.annotate].schema_inline: 按类标注 Schema 顶层必须为 "
                "JSON 对象")
    errors = env.errors(project_text=env.project(body=class_schema_body(
        'schema_inline = \'{"type": "object", "properties": 3}\'')))
    has(errors, "[class.qa.annotate].schema_inline: 未通过 JSON Schema draft "
                "2020-12 元 Schema 校验")
    errors = env.errors(project_text=env.project(
        body=class_schema_body('schema_inline = \'{"type": "array"}\'')))
    has(errors, '[class.qa.annotate].schema_inline: 按类标注 Schema 顶层 type '
                '必须为 "object"')


def test_class_annotate_schema_forbids_reserved_meta_key(env):
    bad = json.dumps({"type": "object",
                      "properties": {"_meta": {"type": "object"}}})
    errors = env.errors(project_text=env.project(
        body=class_schema_body(f"schema_inline = '''\n{bad}\n'''")))
    has(errors, '[class.qa.annotate].schema_inline: 按类标注 Schema 顶层不得声明'
                '保留键 "_meta"')


def test_class_annotate_schema_dangling_ref_is_config_error(env):
    bad = json.dumps({"type": "object",
                      "properties": {"x": {"$ref": "#/$defs/ghost"}}})
    errors = env.errors(project_text=env.project(
        body=class_schema_body(f"schema_inline = '''\n{bad}\n'''")))
    has(errors, "[class.qa.annotate].schema_inline: 按类标注 Schema 引用无法解析")


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
    has(errors, "[[class.qa.annotate.examples]][1].output: 未通过按类标注 Schema")


def test_inherited_examples_rechecked_against_the_class_schema(env):
    # 类只声明 Schema、示例继承全局 ⇒ 继承来的示例也要过类 Schema（运行期就是按类
    # Schema 发出去的）
    global_examples = ('instruction = "标注意图"\n'
                       'examples = [{input = "写请假条", '
                       'output = {intent = "writing_assist", topic = "请假条"}}]')
    body = class_schema_body(f"schema_inline = '''\n{CLASS_SCHEMA}\n'''")
    errors = env.errors(project_text=env.project(annotate_body=global_examples,
                                                 body=body))
    has(errors, "[[class.qa.annotate.examples]][1].output: 未通过按类标注 Schema")


def test_class_without_own_schema_keeps_global_dryrun_wording(env):
    body = CLASSIFY_TWO + ("\n[class.qa.annotate]\n"
                           'examples = [{input = "问", output = {intent = "qa"}}]\n')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[[class.qa.annotate.examples]][1].output: 未通过用户 Schema")


def test_class_annotate_schema_keys_are_whitelisted(env):
    cfg = env.load(project_text=env.project(
        body=class_schema_body(f"schema_inline = '''\n{CLASS_SCHEMA}\n'''")))
    assert isinstance(cfg, ResolvedConfig)
    errors = env.errors(project_text=env.project(
        body=class_schema_body('schema_url = "https://example.com/s.json"')))
    has(errors, "[class.qa.annotate].schema_url: [class.*.annotate] 不可覆盖该键"
                "（白名单：instruction、examples、schema_path、schema_inline）")


def test_class_generate_quota_keys_are_whitelisted(env):
    errors = env.errors(project_text=env.project(
        body=CLASSIFY_TWO + "\n[class.qa.generate]\nsequence_count = 3\n"))
    has(errors, "[class.qa.generate].sequence_count: [class.*.generate] 不可覆盖"
                "该键（白名单：instruction、styles、num_per_record、temperature、"
                "sequences、len_range）")


@pytest.mark.parametrize("value", ["[0, 5]", "[5, 3]", "[3]", '"3-5"',
                                   "[3.5, 5.0]"])
def test_class_len_range_structural_errors(env, value):
    generate = GS_GENERATE.replace("len_range = [3, 5]", f"len_range = {value}")
    errors = env.errors(project_text=gs_project(env, gs_body(generate=generate)))
    has(errors, "[class.ticket_booking.generate].len_range: 期望长度为 2 的整数"
                "区间数组 [lo, hi]（1 ≤ lo ≤ hi）")


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
    has(errors, "[frame.class.task_request.generate].schema_path: 无法读取 Schema 文件")


def test_frame_class_generate_schema_at_most_one_source(env):
    frames = frames_with_schema(
        f"schema_path = \"x.json\"\nschema_inline = '''\n{FRAME_GEN_SCHEMA}\n'''")
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, "[frame.class.task_request.generate].schema_inline: 与 schema_path "
                "恰好提供其一（互斥），得到两者均设置")


def test_frame_class_generate_schema_meta_validation_branches(env):
    for snippet, expected in (
            ("schema_inline = '{bad'", "期望合法 JSON"),
            ("schema_inline = '[1, 2]'", "帧类生成 Schema 顶层必须为 JSON 对象"),
            ('schema_inline = \'{"type": "object", "properties": 3}\'',
             "未通过 JSON Schema draft 2020-12 元 Schema 校验"),
            ('schema_inline = \'{"type": "array"}\'',
             '帧类生成 Schema 顶层 type 必须为 "object"')):
        errors = env.errors(project_text=gs_project(
            env, gs_body(frames=frames_with_schema(snippet))))
        has(errors, f"[frame.class.task_request.generate].schema_inline: {expected}")


def test_frame_class_generate_schema_dangling_ref_is_config_error(env):
    bad = json.dumps({"type": "object",
                      "properties": {"x": {"$ref": "#/$defs/ghost"}}})
    frames = frames_with_schema(f"schema_inline = '''\n{bad}\n'''")
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, "[frame.class.task_request.generate].schema_inline: 帧类生成 "
                "Schema 引用无法解析")


def test_frame_class_generate_whitelist_enforced(env):
    frames = frames_with_schema("enabled = false")
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, "[frame.class.task_request.generate].enabled: "
                "[frame.class.*.generate] 不可覆盖该键"
                "（白名单：instruction、schema_path、schema_inline）")


def test_frame_class_generate_section_only_legal_in_the_form(env):
    body = ("[segment]\nenabled = true\n\n"
            '[frame.classify]\nenabled = true\nfallback_class = "task_request"\n\n'
            "[[frame.classify.classes]]\nname = \"task_request\"\n"
            'description = "请求帧"\n\n'
            '[frame.class.task_request.generate]\ninstruction = "生成"\n')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[frame.class.task_request.generate]: 该节仅时间流生成形态"
                "（[generate.stream].enabled = true）合法")


def test_frame_class_namespace_allowed_without_frame_classify_in_the_form(env):
    # v1.12 的「[frame.class.*] 在场要求 frame.classify.enabled」在本形态放宽
    cfg = env.load(project_text=gs_project(env))
    assert cfg.frame_classify.enabled is False
    assert set(cfg.frame_class_views) == {"task_request", "followup"}
    # 对照：两者都关时照旧报错，且报错文案给出两条出路
    body = '[frame.class.ghost.annotate]\ninstruction = "x"\n'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[frame.class.ghost]: [frame.class.*] 在场要求 "
                "frame.classify.enabled = true 或 generate.stream.enabled = true")


def test_frame_class_unknown_name_rejected_in_the_form(env):
    frames = GS_FRAMES + '\n[frame.class.ghost.generate]\ninstruction = "x"\n'
    errors = env.errors(project_text=gs_project(env, gs_body(frames=frames)))
    has(errors, '[frame.class.ghost]: 类名 "ghost" 不在 [[frame.classify.classes]] 中')


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
    has(errors, "[generate.stream.plan]: 静态系统侧提示部件估算")
    has(errors, "[generate.stream.realize]: 静态系统侧提示部件估算")
    has(errors, "任何记录都装不下")


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
    has(errors, "[generate.stream.realize]: 静态系统侧提示部件估算")
    assert not any("[generate.stream.plan]:" in e for e in errors)


def test_static_precheck_silent_with_room(env, capsys):
    cfg = env.load(config_text=_cw(131072), project_text=gs_project(env))
    assert isinstance(cfg, ResolvedConfig)
    err = capsys.readouterr().err
    assert "[generate.stream.plan]" not in err
    assert "[generate.stream.realize]" not in err


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
    assert '[generate.stream].enabled: 时间流形态要求 run.mode' in joined


def test_generate_stream_default_dataclass_is_all_off():
    gs = GenerateStreamConfig()
    assert gs.enabled is False and gs.sessions == 0
    assert gs.noise_ratio == 0.0 and gs.noise_instruction == ""
    assert gs.duplicates == 0 and gs.frame_gap_s == (5.0, 60.0)
    assert gs.ts_start == "2026-01-01T00:00:00Z"

"""Offline unit tests for M1 (labelkit/common/config/loader.py).

Pure-logic coverage only: TOML parsing, three-source merge precedence, default
filling per spec ch.5 tables, every §6.3 validation rule, error aggregation and
message format, packaged default rubrics. M1 performs no LLM calls, so this
module has no integration counterpart.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from labelkit.common.config import ResolvedConfig, default_rubric, load
from labelkit.common.config import loader as loader_mod
from labelkit.common.config.model import (
    CliOverrides,
    ConsoleConfig,
)
from labelkit.common.errors import ConfigError

# ── fixtures / builders ────────────────────────────────────────────────────

SCHEMA = json.dumps({
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["writing_assist", "qa", "other"]},
        "topic": {"type": "string"},
    },
    "required": ["intent", "topic"],
    "additionalProperties": False,
}, ensure_ascii=False)

BASE_CONFIG = """\
schema_version = 1

[tool]
log_level = "info"

[llm.default]
provider = "openai_compatible"
base_url = "https://example.com/v1"
model = "main-model"
api_key_env = "LK_TEST_KEY_DEFAULT"
supports_structured_output = true
supports_vision = true

[llm.judge]
provider = "anthropic"
base_url = "https://example.com"
model = "judge-model"
api_key_env = "LK_TEST_KEY_JUDGE"
supports_vision = true

[embedding.emb]
base_url = "https://example.com/v1"
model = "bge"
api_key_env = "LK_TEST_KEY_EMB"
"""


def make_project(*, output_path, input_path=None, modality="text", run_extra="",
                 annotate_body='instruction = "标注意图"', body="", schema=SCHEMA,
                 include_output=True) -> str:
    parts = ["schema_version = 1", "", "[run]"]
    if input_path is not None:
        parts.append(f'input = "{input_path}"')
    if output_path is not None:
        parts.append(f'output = "{output_path}"')
    parts.append(f'modality = "{modality}"')
    if run_extra:
        parts.append(run_extra)
    parts += ["", "[annotate]"]
    if annotate_body:
        parts.append(annotate_body)
    parts.append("")
    if body:
        parts += [body, ""]
    if include_output:
        parts += ["[output]", "schema_inline = '''", schema, "'''"]
    return "\n".join(parts) + "\n"


class Env:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.input_file = tmp_path / "input.jsonl"
        self.input_file.write_text('{"text": "你好，世界"}\n', encoding="utf-8")
        self.input_dir = tmp_path / "capture"
        self.input_dir.mkdir()
        (self.input_dir / "uitree_1.jsonl").write_text("{}\n", encoding="utf-8")
        self.out_dir = tmp_path / "out"
        self.out_dir.mkdir()
        self.output = self.out_dir / "result.jsonl"

    def project(self, **kw) -> str:
        kw.setdefault("input_path", self.input_file)
        kw.setdefault("output_path", self.output)
        return make_project(**kw)

    def load(self, config_text: str = BASE_CONFIG, project_text: str | None = None,
             cli: CliOverrides | None = None) -> ResolvedConfig:
        c = self.tmp / "config.toml"
        p = self.tmp / "project.toml"
        c.write_text(config_text, encoding="utf-8")
        p.write_text(project_text if project_text is not None else self.project(),
                     encoding="utf-8")
        return load(c, p, cli or CliOverrides())

    def errors(self, **kw) -> list[str]:
        with pytest.raises(ConfigError) as ei:
            self.load(**kw)
        return ei.value.errors


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    monkeypatch.setenv("LK_TEST_KEY_DEFAULT", "sk-default")
    monkeypatch.setenv("LK_TEST_KEY_JUDGE", "sk-judge")
    monkeypatch.setenv("LK_TEST_KEY_EMB", "sk-emb")
    return Env(tmp_path)


def has(errors: list[str], sub: str) -> bool:
    assert any(sub in e for e in errors), f"no error contains {sub!r}:\n" + "\n".join(errors)
    return True


# v1.17：钩子引用统一 <python-file>:<attribute-path>——样本钩子文件按绝对路径引用。
HOOK_PY = str(Path(__file__).resolve().parents[2] / "hook_samples.py")


# ── happy path: merge, defaults, resolution ────────────────────────────────


def test_happy_path_defaults(env):
    cfg = env.load()
    # built-in defaults (ch.5 tables)
    assert cfg.run.batch_size == 256
    assert cfg.run.seed == 0
    assert cfg.run.mode == "process"
    assert cfg.run.fatal_error_threshold == 20
    assert cfg.dedup.enabled and cfg.dedup.minhash_threshold == 0.85
    assert cfg.dedup.ngram == 5 and cfg.dedup.minhash_num_perm == 128
    assert cfg.quality.enabled and cfg.quality.mode == "pairwise"
    assert cfg.quality.rounds == 4 and cfg.quality.threshold is None
    assert cfg.quality.judgment_reasons == "auto"
    assert cfg.generate.enabled is False
    assert cfg.annotate.llm == "default" and cfg.annotate.self_consistency == 0
    assert cfg.verify.enabled is False and cfg.verify.llm == "judge"
    assert cfg.output.meta_mode == "inline" and cfg.output.rejects == "refs"
    assert cfg.trace.enabled is False
    assert cfg.trace.channels == ("quality", "verify", "schema")
    # config.toml values
    assert cfg.tool.log_level == "info"
    assert cfg.llm_profiles["default"].max_concurrency == 8
    assert cfg.llm_profiles["default"].provider == "openai_compatible"
    assert cfg.llm_profiles["default"].thinking is None
    # resolution duties
    assert cfg.quality.rubric == "default:text"        # auto by modality
    assert cfg.rubric.name == "default-text-v1"
    # v1.17 secret-free：profile 只存环境变量名——值字段已删除（SPEC-SP §5.2）
    assert cfg.llm_profiles["default"].api_key_envs == ("LK_TEST_KEY_DEFAULT",)
    assert not hasattr(cfg.llm_profiles["default"], "api_key")
    assert not hasattr(cfg.llm_profiles["judge"], "api_keys")
    assert cfg.run.input == str(env.input_file)
    assert cfg.run.output == str(env.output)
    assert cfg.trace.path == str(env.out_dir / "result.trace.jsonl")
    assert cfg.limit is None and cfg.strict is False and cfg.dry_run is False
    assert cfg.user_schema["type"] == "object"
    # v1.12 帧粒度四字段：全关默认（字节等价 v1.11）
    assert cfg.frame_classify.enabled is False
    assert cfg.frame_annotate.enabled is False
    assert cfg.frame_class_views == {}
    assert cfg.frame_schema is None
    # v1.18 序列生成：未声明 sequence 形态时不产生序列配置。
    assert cfg.sequence_generation is None


def test_llm_thinking_accepts_explicit_value(env):
    config = BASE_CONFIG.replace(
        'supports_structured_output = true\n',
        'supports_structured_output = true\nthinking = "disabled"\n',
    ).replace('[llm.judge]', '[llm.judge]\nthinking = "enabled"')
    cfg = env.load(config_text=config)
    assert cfg.llm_profiles["default"].thinking == "disabled"
    assert cfg.llm_profiles["judge"].thinking == "enabled"


def test_llm_thinking_rejects_unknown_value(env):
    config = BASE_CONFIG.replace(
        'model = "main-model"\n',
        'model = "main-model"\nthinking = "automatic"\n',
    )
    errors = env.errors(config_text=config)
    has(errors, '[llm.default].thinking: expected "enabled" | "disabled", got "automatic"')


def test_digests_are_sha256_of_raw_bytes(env):
    cfg = env.load()
    raw_c = (env.tmp / "config.toml").read_bytes()
    raw_p = (env.tmp / "project.toml").read_bytes()
    assert cfg.config_digest == "sha256:" + hashlib.sha256(raw_c).hexdigest()
    assert cfg.project_digest == "sha256:" + hashlib.sha256(raw_p).hexdigest()
    assert cfg.config_path == str(env.tmp / "config.toml")


def test_project_overrides_builtin_defaults(env):
    cfg = env.load(project_text=env.project(run_extra="batch_size = 128\nseed = 42"))
    assert cfg.run.batch_size == 128
    assert cfg.run.seed == 42


def test_cli_overrides_beat_project(env):
    alt_in = env.tmp / "alt.jsonl"
    alt_in.write_text('{"text": "x"}\n', encoding="utf-8")
    alt_out = env.out_dir / "alt.jsonl"
    cli = CliOverrides(input=str(alt_in), output=str(alt_out), limit=100,
                       dry_run=True, strict=True, log_level="debug")
    cfg = env.load(cli=cli)
    assert cfg.run.input == str(alt_in)
    assert cfg.run.output == str(alt_out)
    assert cfg.limit == 100
    assert cfg.strict is True and cfg.dry_run is True
    assert cfg.tool.log_level == "debug"          # CLI > config.toml [tool]
    assert cfg.trace.path == str(alt_out.with_suffix("")) + ".trace.jsonl"


def test_ui_modality_auto_selects_ui_rubric(env):
    cfg = env.load(project_text=env.project(input_path=env.input_dir, modality="ui"))
    assert cfg.quality.rubric == "default:ui"
    assert cfg.rubric.name == "default-ui-v1"


def test_explicit_rubric_selector_beats_modality(env):
    cfg = env.load(project_text=env.project(body='[quality]\nrubric = "default:ui"'))
    assert cfg.quality.rubric == "default:ui"
    assert cfg.rubric.name == "default-ui-v1"


def test_trace_explicit_path_kept(env):
    cfg = env.load(project_text=env.project(
        body='[trace]\nenabled = true\npath = "custom.trace.jsonl"'))
    assert cfg.trace.path == str(env.tmp / "custom.trace.jsonl")
    assert cfg.trace.enabled is True


def test_schema_path_variant(env):
    schema_file = env.tmp / "schema.json"
    schema_file.write_text(SCHEMA, encoding="utf-8")
    body = f'[output]\nschema_path = "{schema_file}"'
    cfg = env.load(project_text=env.project(include_output=False, body=body))
    assert cfg.user_schema == json.loads(SCHEMA)
    assert cfg.output.schema_path == str(schema_file)


# ── rule 1: TOML structure ─────────────────────────────────────────────────


def test_schema_version_wrong_and_missing(env):
    bad_config = BASE_CONFIG.replace("schema_version = 1", "schema_version = 2")
    project = env.project().replace("schema_version = 1\n", "")
    errors = env.errors(config_text=bad_config, project_text=project)
    has(errors, "config.toml:schema_version: expected 1, got 2")
    has(errors, "project.toml:schema_version: missing required key, expected 1")


def test_type_mismatch_message_format(env):
    bad = BASE_CONFIG.replace('api_key_env = "LK_TEST_KEY_DEFAULT"',
                              'api_key_env = "LK_TEST_KEY_DEFAULT"\ntimeout_s = "abc"')
    errors = env.errors(config_text=bad)
    has(errors, '[llm.default].timeout_s: expected positive integer, got "abc"')


def test_missing_required_profile_key(env):
    bad = BASE_CONFIG.replace('model = "main-model"\n', "")
    errors = env.errors(config_text=bad)
    has(errors, "[llm.default].model: missing required key")


def test_unknown_keys_warn_not_error(env, capsys):
    cfg_text = BASE_CONFIG.replace("[tool]", "[tool]\nfancy_new_key = 1")
    project = env.project(run_extra="future_flag = true")
    cfg = env.load(config_text=cfg_text, project_text=project)
    assert isinstance(cfg, ResolvedConfig)
    err_out = capsys.readouterr().err
    assert "warning:" in err_out
    assert "[tool].fancy_new_key: unknown key" in err_out
    assert "[run].future_flag: unknown key" in err_out


def test_config_file_missing(env):
    with pytest.raises(ConfigError) as ei:
        load(env.tmp / "nope.toml", env.tmp / "also_nope.toml", CliOverrides())
    joined = "\n".join(ei.value.errors)
    assert "cannot read config file" in joined


def test_toml_parse_failure(env):
    errors = env.errors(config_text="schema_version = [oops")
    has(errors, "TOML parse failed")


def test_no_llm_profile(env):
    errors = env.errors(config_text='schema_version = 1\n[tool]\nlog_level = "info"\n')
    has(errors, "at least one [llm.<name>] profile is required")


# ── §3.1.5 类型化读取器：逐形态的定位前缀与 got 渲染 ────────────────────────


def test_scalar_type_mismatch_per_reader_kind(env):
    # 四种标量读取器各一条：字符串（枚举期望渲染成候选串）/ 整数 / 数值 / 布尔。
    project = env.project(run_extra="mode = 3\nseed = false",
                          body='[dedup]\nminhash_threshold = "high"\n'
                               '[quality]\nenabled = "yes"\n')
    errors = env.errors(project_text=project)
    has(errors, '[run].mode: expected "process" | "generate_only", got 3')
    has(errors, "[run].seed: expected integer, got false")
    has(errors, '[dedup].minhash_threshold: expected number in (0,1], got "high"')
    has(errors, '[quality].enabled: expected boolean, got "yes"')


@pytest.mark.parametrize("threshold", [0.99, 1.0])
def test_minhash_threshold_rejects_unconstructible_lsh_pair(env, threshold):
    project = env.project(body=f"[dedup]\nminhash_threshold = {threshold}\n")
    errors = env.errors(project_text=project)
    has(
        errors,
        "[dedup].minhash_threshold: expected a value compatible with "
        f"minhash_num_perm = 128 (lower minhash_threshold or increase "
        f"minhash_num_perm), got {threshold}",
    )


def test_minhash_threshold_accepts_constructible_lsh_pair(env):
    project = env.project(
        body="[dedup]\nminhash_threshold = 0.95\nminhash_num_perm = 64\n"
    )
    cfg = env.load(project_text=project)
    assert cfg.dedup.minhash_threshold == 0.95
    assert cfg.dedup.minhash_num_perm == 64


def test_array_readers_report_the_offending_element_position(env):
    # 数组读取器逐元素定位（`key[N]`，N 从 1 起）：字符串数组的非串元素 / 枚举外
    # 元素 / 数值数组的非数值元素，外加"整个值根本不是数组"的上层形态。
    project = env.project(body='[quality]\njudges = ["judge", 7, "judge"]\n'
                               '[trace]\nenabled = true\nchannels = ["llm", "ghost"]\n'
                               '[generate]\nenabled = true\ninstruction = "生成"\n'
                               'weights = [1.0, "x"]\n'
                               '[stream]\nkey = "meta:uid"\n')
    errors = env.errors(project_text=project)
    has(errors, "[quality].judges[2]: expected string, got 7")
    has(errors, '[trace].channels[2]: expected "ingest" | ')
    has(errors, '[trace].channels[2]: expected ')
    has(errors, 'got "ghost"')
    has(errors, '[generate].weights[2]: expected number, got "x"')
    has(errors, '[stream].key: expected string array, got "meta:uid"')


def test_whole_array_type_mismatch_falls_back_to_the_default(env):
    project = env.project(body='[generate]\nenabled = true\ninstruction = "生成"\n'
                               "weights = 3\n")
    errors = env.errors(project_text=project)
    has(errors, "[generate].weights: expected number array, got 3")


def test_top_level_section_must_be_a_table(env):
    errors = env.errors(project_text="dedup = 3\n" + env.project())
    has(errors, "project.toml:dedup: expected table, got 3")


def test_unrenderable_value_falls_back_to_repr(env):
    # TOML 原生 datetime 不是 JSON 可序列化值：got 段回落 repr，报错照常聚合
    # （spec 3.1.5 的定位前缀不变）。
    project = env.project(run_extra="seed = 2026-08-14T02:00:00Z")
    errors = env.errors(project_text=project)
    has(errors, "[run].seed: expected integer, got datetime.datetime(2026, 8, 14")


def _config_without_embedding(top_level: str = "") -> str:
    """BASE_CONFIG 去掉 [embedding.emb] 节，可在最前面插入若干顶层键。"""
    trimmed = BASE_CONFIG.split("[embedding.emb]", 1)[0]
    return trimmed.replace("schema_version = 1\n",
                           f"schema_version = 1\n{top_level}", 1)


def test_llm_sub_table_must_be_a_table(env):
    config = ("schema_version = 1\n\n[llm]\nbroken = 3\n"
              + BASE_CONFIG.replace("schema_version = 1\n", "", 1))
    errors = env.errors(config_text=config)
    has(errors, "config.toml:[llm.broken]: expected table, got 3")


def test_embedding_table_shape_errors(env):
    errors = env.errors(config_text=_config_without_embedding("embedding = 3\n"))
    has(errors, "config.toml:embedding: expected table, got 3")
    errors = env.errors(config_text=_config_without_embedding("embedding.other = 7\n"))
    has(errors, "config.toml:[embedding.other]: expected table, got 7")


def test_array_of_tables_shape_errors_locate_by_element(env):
    # 三张表数组（styles / examples / classes）共享同一套定位形制：整体非数组、
    # 元素非表、元素内字段类型错，各报一条 `[[section]][N]` 定位。
    body = ('[generate]\nenabled = true\ninstruction = "生成"\nstyles = 3\n'
            '[classify]\nenabled = true\nclasses = 3\n')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[generate].styles: expected array of tables, got 3")
    has(errors, "[classify].classes: expected array of tables, got 3")

    body = ('[generate]\nenabled = true\ninstruction = "生成"\nstyles = [3]\n'
            '[classify]\nenabled = true\nclasses = [7]\n')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[[generate.styles]][1]: expected table, got 3")
    has(errors, "[[classify.classes]][1]: expected table, got 7")


def test_annotate_examples_shape_errors(env):
    errors = env.errors(project_text=env.project(
        annotate_body='instruction = "标注意图"\nexamples = 3'))
    has(errors, "[annotate].examples: expected array of tables, got 3")
    errors = env.errors(project_text=env.project(
        annotate_body='instruction = "标注意图"\nexamples = [3]'))
    has(errors, "[[annotate.examples]][1]: expected table, got 3")
    errors = env.errors(project_text=env.project(
        annotate_body='instruction = "标注意图"\n'
                      'examples = [{input = "x", output = "not-a-table"}]'))
    has(errors, "[[annotate.examples]][1].output: expected table (object, must pass "
                'the user schema), got "not-a-table"')


def test_rubric_criteria_shape_errors(env):
    body = '[quality]\nrubric = "inline"\n[rubric]\ncriteria = 3\n'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[[rubric.criteria]]: expected array of tables, got 3")
    body = '[quality]\nrubric = "inline"\n[rubric]\ncriteria = [3]\n'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[[rubric.criteria]][1]: expected table, got 3")


def test_frame_sub_tables_must_be_tables(env):
    body = "[frame]\nclassify = 3\n"
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "project.toml:[frame].classify: expected table, got 3")


def test_pool_env_name_must_be_nonempty(env):
    config = BASE_CONFIG.replace('api_key_env = "LK_TEST_KEY_DEFAULT"',
                                 'api_key_envs = ["", "LK_TEST_KEY_DEFAULT"]')
    errors = env.errors(config_text=config)
    has(errors, '[llm.default].api_key_envs[1]: expected non-empty string, got ""')


def test_pool_element_error_is_not_doubled_by_a_shape_error(env):
    # 元素级错误已由 get_str_tuple 逐条报出，池解析不再补一条误导性的
    # "non-empty array" ——那条只留给真正写成 `[]` 的形态。
    config = BASE_CONFIG.replace('api_key_env = "LK_TEST_KEY_DEFAULT"',
                                 "api_key_envs = [3]")
    errors = env.errors(config_text=config)
    has(errors, "[llm.default].api_key_envs[1]: expected string, got 3")
    assert not any("expected a non-empty array of env var names" in e for e in errors)


def test_style_element_missing_name_is_not_uniqueness_checked(env):
    body = ('[generate]\nenabled = true\ninstruction = "生成"\n'
            'styles = [{prompt = "正式一些"}]\n')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[[generate.styles]][1].name: missing required key, "
                "expected non-empty string")
    assert not any("must be unique within the table" in e for e in errors)


def test_cli_log_level_enum_is_validated_by_m1(env):
    with pytest.raises(ConfigError) as ei:
        env.load(cli=CliOverrides(log_level="loud"))
    assert any('cli:--log-level: expected "debug" | "info" | "warn" | "error", '
               'got "loud"' in e for e in ei.value.errors)


# ── rules 2–5: profile references ──────────────────────────────────────────


def test_unknown_profile_reference_lists_available(env):
    errors = env.errors(project_text=env.project(body='[quality]\nllm = "fast"'))
    has(errors, '[quality].llm: referenced profile "fast" does not exist in config.toml '
                "[llm.*], available: default, judge")


def test_generate_llms_checked_per_element(env):
    body = '[generate]\nllms = ["default", "ghost"]'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[generate].llms[2]: referenced profile "ghost" does not exist')


def test_verify_llm_not_checked_when_disabled(env):
    # config without a "judge" profile; verify disabled with default llm="judge"
    solo = BASE_CONFIG.replace("""
[llm.judge]
provider = "anthropic"
base_url = "https://example.com"
model = "judge-model"
api_key_env = "LK_TEST_KEY_JUDGE"
supports_vision = true
""", "\n")
    cfg = env.load(config_text=solo)
    assert "judge" not in cfg.llm_profiles


def test_verify_llm_checked_when_enabled(env):
    body = '[verify]\nenabled = true\nllm = "ghost"'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[verify].llm: referenced profile "ghost" does not exist')


def test_repair_llm_checked_when_set(env):
    body = f"[output]\nrepair_llm = \"ghost\"\nschema_inline = '''\n{SCHEMA}\n'''"
    errors = env.errors(project_text=env.project(include_output=False, body=body))
    has(errors, '[output].repair_llm: referenced profile "ghost" does not exist')


def test_judges_must_be_odd(env):
    errors = env.errors(project_text=env.project(
        body='[quality]\njudges = ["default", "judge"]'))
    has(errors, "[quality].judges: must have an odd length when non-empty, got 2")


def test_verify_judges_odd_and_existing(env):
    body = '[verify]\nenabled = true\njudges = ["judge", "ghost"]'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[verify].judges[2]: referenced profile "ghost" does not exist')
    has(errors, "[verify].judges: must have an odd length when non-empty")


def test_ui_modality_requires_vision(env):
    config = BASE_CONFIG + """
[llm.novision]
provider = "openai_compatible"
base_url = "https://example.com/v1"
model = "blind-model"
api_key_env = "LK_TEST_KEY_DEFAULT"
"""
    project = env.project(input_path=env.input_dir, modality="ui",
                          annotate_body='llm = "novision"\ninstruction = "标注"')
    errors = env.errors(config_text=config, project_text=project)
    has(errors, "[llm.novision].supports_vision")
    assert not any("llm.default" in e for e in errors)   # vision profile is fine


def test_ui_modality_vision_set_drops_a_disabled_annotate(env):
    # 视觉必需集只收启用阶段：annotate 关掉后，它引用的纯文本 profile 不再被要求
    # supports_vision（quality 仍在册，故用一个有视觉能力的 judge 顶上）。
    config = BASE_CONFIG + """
[llm.novision]
provider = "openai_compatible"
base_url = "https://example.com/v1"
model = "blind-model"
api_key_env = "LK_TEST_KEY_DEFAULT"
"""
    project = env.project(input_path=env.input_dir, modality="ui",
                          annotate_body='enabled = false\nllm = "novision"',
                          body='[quality]\nllm = "judge"\nthreshold = 0.5\n')
    cfg = env.load(config_text=config, project_text=project)
    assert cfg.annotate.enabled is False and cfg.annotate.llm == "novision"


def test_semantic_dedup_requires_embedding_name(env):
    errors = env.errors(project_text=env.project(body="[dedup]\nsemantic = true"))
    has(errors, "[dedup].semantic_embedding: required when dedup.semantic = true")


def test_semantic_dedup_unknown_embedding(env):
    body = '[dedup]\nsemantic = true\nsemantic_embedding = "ghost"'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "does not exist in config.toml [embedding.*], available: emb")


def test_semantic_dedup_ok_resolves_embedding_key(env):
    body = '[dedup]\nsemantic = true\nsemantic_embedding = "emb"'
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.embedding_profiles["emb"].api_key_envs == ("LK_TEST_KEY_EMB",)


def test_semantic_dedup_key_resolution_skips_a_profile_with_no_declaration(env):
    # embedding profile 的密钥声明本身非法（两式皆无）时，规则 12 不再二次解析
    # ——只留声明侧那一条错误，不叠加"缺环境变量"。
    config = BASE_CONFIG.replace('api_key_env = "LK_TEST_KEY_EMB"\n', "")
    body = '[dedup]\nsemantic = true\nsemantic_embedding = "emb"'
    errors = env.errors(config_text=config, project_text=env.project(body=body))
    has(errors, "[embedding.emb].api_key_env: missing required key - exactly one of "
                "api_key_env / api_key_envs must be provided (v1.6)")
    assert not any("environment variable" in e for e in errors)


def test_declared_embedding_window_does_not_warn(env, capsys):
    config = BASE_CONFIG.replace('model = "bge"',
                                 'model = "bge"\ncontext_window = 8192', 1)
    body = '[dedup]\nsemantic = true\nsemantic_embedding = "emb"'
    env.load(config_text=config, project_text=env.project(body=body))
    assert "[embedding.emb].context_window" not in capsys.readouterr().err


def test_annotate_disabled_leaves_the_stage_out_of_the_reference_set(env):
    # 引用集只收启用阶段：annotate 关掉后 [llm.default] 只因 quality 在册。
    config = BASE_CONFIG.replace('model = "main-model"',
                                 'model = "main-model"\ncontext_window = 131072', 1)
    cfg = env.load(config_text=config,
                   project_text=env.project(annotate_body="enabled = false",
                                            body='[quality]\nllm = "judge"\n'
                                                 "threshold = 0.5\n"))
    assert cfg.annotate.enabled is False
    assert cfg.quality.llm == "judge"


# ── rules 6–9: cross-field constraints ─────────────────────────────────────


def test_top_ratio_required_when_selected(env):
    errors = env.errors(project_text=env.project(
        body='[quality]\nselection = "top_ratio"'))
    has(errors, '[quality].top_ratio: required when selection = "top_ratio"')


def test_top_ratio_threshold_mutually_exclusive(env):
    body = '[quality]\nselection = "top_ratio"\ntop_ratio = 0.5\nthreshold = 0.3'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[quality].threshold: mutually exclusive with quality.top_ratio")


def test_top_ratio_range(env):
    body = '[quality]\nselection = "top_ratio"\ntop_ratio = 1.5'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[quality].top_ratio: expected number in (0,1], got 1.5")
    # the "required" branch of rule 6 must not fire when the key was provided (just invalid)
    assert not any("required when" in e for e in errors)


@pytest.mark.parametrize("value", [2, 4, 1])
def test_self_consistency_rejects_bad_values(env, value):
    errors = env.errors(project_text=env.project(
        annotate_body=f'instruction = "标注"\nself_consistency = {value}'))
    has(errors, f"[annotate].self_consistency: expected 0 or an odd number >= 3, got {value}")


@pytest.mark.parametrize("value", [0, 3, 5])
def test_self_consistency_accepts_valid(env, value):
    cfg = env.load(project_text=env.project(
        annotate_body=f'instruction = "标注"\nself_consistency = {value}'))
    assert cfg.annotate.self_consistency == value


def test_weighted_mixture_requires_weights(env):
    body = ('[generate]\nenabled = true\ninstruction = "生成"\n'
            'llms = ["default", "judge"]\nmixture = "weighted"')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[generate].weights: required when mixture = "weighted"')


def test_weighted_mixture_length_and_positivity(env):
    body = ('[generate]\nenabled = true\ninstruction = "生成"\n'
            'llms = ["default", "judge"]\nmixture = "weighted"\nweights = [1.0]')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[generate].weights: expected length 2 (= generate.llms), got length 1")

    body = ('[generate]\nenabled = true\ninstruction = "生成"\n'
            'llms = ["default", "judge"]\nmixture = "weighted"\nweights = [1.0, -0.5]')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[generate].weights[2]: expected positive number, got -0.5")


def test_styles_unique_names_and_nonempty_prompts(env):
    body = ('[generate]\nenabled = true\ninstruction = "生成"\n'
            '[[generate.styles]]\nname = "formal"\nprompt = "正式风格"\n'
            '[[generate.styles]]\nname = "formal"\nprompt = ""')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[[generate.styles]][2].name: name must be unique within the table, got duplicate "formal"')
    has(errors, "[[generate.styles]][2].prompt: expected non-empty string")


def test_judgment_reasons_values(env):
    errors = env.errors(project_text=env.project(
        body='[quality]\njudgment_reasons = "always"'))
    has(errors, '[quality].judgment_reasons: expected "auto" | true | false')
    cfg = env.load(project_text=env.project(body="[quality]\njudgment_reasons = true"))
    assert cfg.quality.judgment_reasons is True


# ── rules 10/11: run mode (generate_only, v1.4) ────────────────────────────

GEN_BODY = '[generate]\nenabled = true\ninstruction = "生成中文指令样本"\n'


def test_generate_only_happy_standalone(env):
    project = env.project(input_path=None, run_extra='mode = "generate_only"',
                          body=GEN_BODY + "standalone_count = 10")
    cfg = env.load(project_text=project)
    assert cfg.run.mode == "generate_only"
    assert cfg.run.input is None
    assert cfg.generate.standalone_count == 10


def test_generate_only_happy_seed_pool(env):
    project = env.project(input_path=None, run_extra='mode = "generate_only"',
                          body=GEN_BODY + 'seed_examples = ["写一条请假条", "翻译这句话"]')
    cfg = env.load(project_text=project)
    assert cfg.generate.seed_examples == ("写一条请假条", "翻译这句话")


def test_generate_only_forbids_input(env):
    project = env.project(run_extra='mode = "generate_only"',
                          body=GEN_BODY + "standalone_count = 10")
    errors = env.errors(project_text=project)
    has(errors, '[run].input: must be absent when run.mode = "generate_only"')


def test_generate_only_forbids_cli_input(env):
    project = env.project(input_path=None, run_extra='mode = "generate_only"',
                          body=GEN_BODY + "standalone_count = 10")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=project, cli=CliOverrides(input=str(env.input_file)))
    has(ei.value.errors, 'cli:--input: must not provide an input path when run.mode = "generate_only"')


def test_generate_only_requires_text_modality(env):
    project = env.project(input_path=None, modality="ui",
                          run_extra='mode = "generate_only"',
                          body=GEN_BODY + "standalone_count = 10")
    errors = env.errors(project_text=project)
    has(errors, '[run].modality: run.mode = "generate_only" requires "text", got "ui"')


def test_generate_only_requires_generate_enabled(env):
    project = env.project(input_path=None, run_extra='mode = "generate_only"')
    errors = env.errors(project_text=project)
    has(errors, '[generate].enabled: run.mode = "generate_only" requires generate.enabled = true')


def test_generate_only_seed_forms_mutually_exclusive(env):
    project = env.project(input_path=None, run_extra='mode = "generate_only"',
                          body=GEN_BODY + 'standalone_count = 10\nseed_examples = ["a"]')
    errors = env.errors(project_text=project)
    has(errors, "[generate].seed_examples: mutually exclusive with standalone_count")


def test_generate_only_requires_one_seed_form(env):
    project = env.project(input_path=None, run_extra='mode = "generate_only"',
                          body=GEN_BODY)
    errors = env.errors(project_text=project)
    has(errors, "seed_examples (a non-empty string array) or standalone_count (>= 1)")


def test_generate_only_seed_examples_nonempty_strings(env):
    project = env.project(input_path=None, run_extra='mode = "generate_only"',
                          body=GEN_BODY + 'seed_examples = ["ok", " "]')
    errors = env.errors(project_text=project)
    has(errors, "[generate].seed_examples[2]: expected non-empty string")

    project = env.project(input_path=None, run_extra='mode = "generate_only"',
                          body=GEN_BODY + "seed_examples = []")
    errors = env.errors(project_text=project)
    has(errors, "[generate].seed_examples: expected a non-empty string array, got an empty array")


def test_process_mode_forbids_generate_only_keys(env):
    errors = env.errors(project_text=env.project(
        body='[generate]\nseed_examples = ["a"]'))
    has(errors, '[generate].seed_examples: can only be set when run.mode = "generate_only"')

    errors = env.errors(project_text=env.project(
        body="[generate]\nstandalone_count = 5"))
    has(errors, '[generate].standalone_count: can only be set when run.mode = "generate_only"')


# ── rule 12: API keys, referenced profiles only ────────────────────────────


def test_referenced_profile_needs_no_key_at_static_load(env, monkeypatch):
    """v1.17 secret-free：无 key 的静态 load 成功（SPEC-SP §5.2 / §13.1 credential 锚）。"""
    monkeypatch.delenv("LK_TEST_KEY_DEFAULT")
    cfg = env.load()
    assert cfg.llm_profiles["default"].api_key_envs == ("LK_TEST_KEY_DEFAULT",)


def test_unreferenced_profile_needs_no_key_at_static_load(env, monkeypatch):
    monkeypatch.delenv("LK_TEST_KEY_JUDGE")   # verify disabled → judge unreferenced
    cfg = env.load()
    assert cfg.llm_profiles["judge"].api_key_envs == ("LK_TEST_KEY_JUDGE",)


def test_verify_enabled_loads_keyless(env, monkeypatch):
    """v1.17 secret-free：judge 被引用也只在 run/probe 期需要密钥值。"""
    monkeypatch.delenv("LK_TEST_KEY_JUDGE")
    cfg = env.load(project_text=env.project(body="[verify]\nenabled = true"))
    assert cfg.verify.enabled


# ── rules 13–15: user schema + few-shot ────────────────────────────────────


def test_schema_exactly_one_source(env):
    body = f"[output]\nschema_path = \"x.json\"\nschema_inline = '''\n{SCHEMA}\n'''"
    errors = env.errors(project_text=env.project(include_output=False, body=body))
    has(errors, "exactly one of schema_path / schema_inline must be provided (mutually exclusive)")

    errors = env.errors(project_text=env.project(include_output=False))
    has(errors, "exactly one of schema_path or schema_inline must be provided")


def test_schema_path_unreadable(env):
    body = '[output]\nschema_path = "does/not/exist.json"'
    errors = env.errors(project_text=env.project(include_output=False, body=body))
    has(errors, "cannot read schema file")


def test_schema_invalid_json(env):
    errors = env.errors(project_text=env.project(schema="{not json"))
    has(errors, "[output].schema_inline: expected valid JSON")


def test_schema_meta_schema_violation(env):
    bad = json.dumps({"type": "object", "properties": {"a": {"type": 123}}})
    errors = env.errors(project_text=env.project(schema=bad))
    has(errors, "failed JSON Schema draft 2020-12 meta-schema validation")


def test_schema_top_level_must_be_object(env):
    bad = json.dumps({"type": "array", "items": {"type": "string"}})
    errors = env.errors(project_text=env.project(schema=bad))
    has(errors, 'user schema top-level type must be "object", got "array"')


def test_schema_reserved_meta_key(env):
    bad = json.dumps({"type": "object",
                      "properties": {"intent": {"type": "string"},
                                     "_meta": {"type": "object"}}})
    errors = env.errors(project_text=env.project(schema=bad))
    has(errors, 'user schema must not declare the reserved top-level key "_meta"')


def test_few_shot_output_validated_against_schema(env):
    good = ('instruction = "标注"\n'
            'examples = [{input = "你好", output = {intent = "qa", topic = "问候"}}]')
    cfg = env.load(project_text=env.project(annotate_body=good))
    assert cfg.annotate.examples[0].output == {"intent": "qa", "topic": "问候"}

    bad = ('instruction = "标注"\n'
           'examples = [{input = "你好", output = {intent = "nope", topic = "问候"}}]')
    errors = env.errors(project_text=env.project(annotate_body=bad))
    has(errors, "[[annotate.examples]][1].output: failed user schema validation")


def test_few_shot_with_unresolvable_ref_schema_is_config_error(env):
    """A $ref that passes check_schema meta-validation but cannot be resolved
    locally must aggregate — not crash — even with few-shot examples present
    (spec 3.1.2/3.1.5: output is ResolvedConfig OR ConfigError, exit 2;
    CONTRACTS §6.3 rule 13, §12 #23)."""
    ref_schema = json.dumps({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"intent": {"$ref": "https://example.invalid/defs.json"},
                       "topic": {"type": "string"}},
        "required": ["intent", "topic"],
    })
    body = ('instruction = "标注"\n'
            'examples = [{input = "你好", output = {intent = "qa", topic = "问候"}}]')
    errors = env.errors(project_text=env.project(annotate_body=body, schema=ref_schema))
    has(errors, "[output].schema_inline: user schema has an unresolvable reference")


def test_few_shot_unresolvable_ref_aggregates_with_other_errors(env):
    """The referencing failure joins the single aggregated ConfigError instead of
    wiping out errors collected earlier in the same pass (spec 3.1.5)."""
    ref_schema = json.dumps({
        "type": "object",
        "properties": {"intent": {"$ref": "./defs.json"}},
    })
    body = ('instruction = "标注"\n'
            'examples = [{input = "你好", output = {intent = "qa"}}]\n'
            'self_consistency = 2')  # rule 7 violation collected before rule 15
    errors = env.errors(project_text=env.project(annotate_body=body, schema=ref_schema))
    has(errors, "[annotate].self_consistency")
    has(errors, "[output].schema_inline: user schema has an unresolvable reference")


def test_unresolvable_ref_without_examples_is_config_error(env):
    """Even without few-shot examples an unresolvable $ref is an M1 error
    (CONTRACTS §6.3 rule 13 + §12 #23): the tool never retrieves external schema
    resources at runtime, so deferring the failure would crash every record in
    M8 — violating the M1 contract 不存在运行期配置错误 (spec 3.1)."""
    ref_schema = json.dumps({
        "type": "object",
        "properties": {"intent": {"$ref": "https://example.invalid/defs.json"}},
    })
    errors = env.errors(project_text=env.project(schema=ref_schema))
    has(errors, "[output].schema_inline: user schema has an unresolvable reference")


def test_local_refs_and_ref_shaped_data_are_not_flagged(env):
    """Resolvable local $refs (incl. inside $defs) pass, and '$ref'-shaped strings
    in data positions (const/enum/default/examples) are literal content, never
    resolution-checked (§12 #23)."""
    ref_schema = json.dumps({
        "type": "object",
        "properties": {
            "intent": {"$ref": "#/$defs/intent"},
            "marker": {"const": {"$ref": "https://example.invalid/not-a-ref"}},
        },
        "$defs": {"intent": {"type": "string",
                             "enum": ["qa", "chat"]}},
    })
    cfg = env.load(project_text=env.project(schema=ref_schema))
    assert isinstance(cfg, ResolvedConfig)


def test_dangling_local_ref_is_config_error(env):
    """A local '#/...' pointer to a nonexistent target passes check_schema but can
    never resolve at runtime — rejected by rule 13 like a remote ref (§12 #23)."""
    ref_schema = json.dumps({
        "type": "object",
        "properties": {"intent": {"$ref": "#/$defs/missing"}},
    })
    errors = env.errors(project_text=env.project(schema=ref_schema))
    has(errors, "[output].schema_inline: user schema has an unresolvable reference")


def test_few_shot_requires_input_and_output(env):
    body = 'instruction = "标注"\nexamples = [{input = "你好"}]'
    errors = env.errors(project_text=env.project(annotate_body=body))
    has(errors, "[[annotate.examples]][1].output: missing required key")


def test_nested_id_shifts_the_ref_base_uri(env):
    # $ref 遍历跟踪 $id 引起的基 URI 变化（RFC 3986 join）：同一个相对 ref 在
    # 顶层 $id 下能解析，被嵌套 $id 挪走基址后就解析不了。
    ok_schema = json.dumps({
        "$id": "https://example.com/labelkit/out.json",
        "type": "object",
        "properties": {"intent": {"$ref": "intent.json"}},
        "$defs": {"intent": {"$id": "intent.json", "type": "string"}},
    })
    assert isinstance(env.load(project_text=env.project(schema=ok_schema)),
                      ResolvedConfig)
    moved = json.dumps({
        "$id": "https://example.com/labelkit/out.json",
        "type": "object",
        "properties": {"wrap": {"$id": "https://elsewhere.invalid/w.json",
                                "properties": {"intent": {"$ref": "intent.json"}}}},
        "$defs": {"intent": {"$id": "intent.json", "type": "string"}},
    })
    errors = env.errors(project_text=env.project(schema=moved))
    has(errors, "[output].schema_inline: user schema has an unresolvable reference")


def test_repeated_unresolvable_ref_reported_once(env):
    # 同一个 ref 出现多次只报一条（按 ref 去重，避免同一病因刷屏）。
    ref_schema = json.dumps({
        "type": "object",
        "properties": {"a": {"$ref": "#/$defs/missing"},
                       "b": {"$ref": "#/$defs/missing"}},
    })
    errors = env.errors(project_text=env.project(schema=ref_schema))
    dangling = [e for e in errors if "unresolvable reference" in e]
    assert len(dangling) == 1                       # 两处同名 ref 合成一条
    assert "#/$defs/missing" in dangling[0]


def test_ref_traversal_is_best_effort_when_the_document_cannot_be_ingested():
    # 引用机制本身摄不进该文档时返回空表、绝不抛（规则 15 的运行期兜底仍在）——
    # 这类文档在 load() 里已被元校验拦下，此处直接钉纯函数的契约。
    from labelkit.common.config._schemas import _unresolvable_refs

    assert _unresolvable_refs({"$schema": 3, "type": "object"}) == []
    assert _unresolvable_refs({"$id": 3, "type": "object"}) == []


def test_fewshot_dryrun_reports_a_reference_error_the_static_walk_cannot_see(env):
    # $dynamicRef 静态遍历看不见，但 iter_errors 会抛 referencing 异常：必须并入
    # 聚合 ConfigError（退出码 2），绝不作为未捕获崩溃逃逸（退出码 4）。
    dyn = json.dumps({"type": "object",
                      "properties": {"intent": {"$dynamicRef": "#ghost"}}})
    errors = env.errors(project_text=env.project(
        schema=dyn,
        annotate_body=('instruction = "标注意图"\n'
                       'examples = [{input = "问路", output = {intent = "qa"}}]')))
    has(errors, "[output].schema_inline: user schema has an unresolvable reference, "
                "cannot validate the [[annotate.examples]] example outputs")


def test_fewshot_dryrun_reports_a_raising_validator_callback(env):
    # 回调自身有 bug（抛异常）⇒ 按配置错误上报并停跑其余示例，而不是退出码 4。
    errors = env.errors(project_text=env.project(
        annotate_body=('instruction = "标注意图"\n'
                       'examples = [{input = "问路", '
                       'output = {intent = "qa", topic = "问路"}}]'),
        body=_output_with(f'validator = "{HOOK_PY}:intent_raises"'),
        include_output=False,
    ))
    has(errors, "[output].validator: the callback raised while dry-running few-shot "
                "example 1: RuntimeError: hook exploded")


# ── rule 16: rubric ────────────────────────────────────────────────────────

INLINE_RUBRIC = """\
[quality]
rubric = "inline"

[rubric]
name = "intent-rubric"

[[rubric.criteria]]
key = "intent_clarity"
weight = 2.0
description = "指令意图是否清晰可辨"
pairwise_prompt = "哪条指令的意图更清晰？"
"""


def test_inline_rubric_happy(env):
    cfg = env.load(project_text=env.project(body=INLINE_RUBRIC))
    assert cfg.quality.rubric == "inline"
    assert cfg.rubric.name == "intent-rubric"
    assert cfg.rubric.criteria[0].key == "intent_clarity"
    assert cfg.rubric.criteria[0].weight == 2.0
    assert cfg.rubric.criteria[0].pointwise_levels == ()


def test_inline_selector_without_criteria(env):
    errors = env.errors(project_text=env.project(body='[quality]\nrubric = "inline"'))
    has(errors, '[quality].rubric: rubric = "inline" but [[rubric.criteria]] is not provided')


def test_rubric_key_pattern(env):
    body = INLINE_RUBRIC + """
[[rubric.criteria]]
key = "Topic-Match"
description = "话题是否明确可归类"
pairwise_prompt = "哪条指令的话题更明确？"
"""
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[[rubric.criteria]][2].key: expected a match of [a-z0-9_]+, got "Topic-Match"')


def test_rubric_duplicate_key(env):
    body = INLINE_RUBRIC + """
[[rubric.criteria]]
key = "intent_clarity"
description = "重复"
pairwise_prompt = "重复？"
"""
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[[rubric.criteria]][2].key: key must be unique")


def test_rubric_weight_positive(env):
    body = INLINE_RUBRIC.replace("weight = 2.0", "weight = 0.0")
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[[rubric.criteria]][1].weight: expected positive number, got 0.0")


def test_rubric_criteria_nonempty(env):
    body = '[quality]\nrubric = "inline"\n\n[rubric]\nname = "empty"\ncriteria = []'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[rubric].criteria: criteria must not be empty")


def test_pointwise_requires_six_levels_inline(env):
    body = INLINE_RUBRIC.replace('rubric = "inline"', 'rubric = "inline"\nmode = "pointwise"')
    body += 'pointwise_levels = ["0: 差", "1: 中", "2: 好"]\n'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[[rubric.criteria]][1].pointwise_levels: pointwise mode requires exactly 6 levels (0-5), got 3")


def test_pointwise_with_default_rubric_ok(env):
    cfg = env.load(project_text=env.project(body='[quality]\nmode = "pointwise"'))
    assert all(len(c.pointwise_levels) == 6 for c in cfg.rubric.criteria)


def test_default_rubrics_load_from_package():
    text = default_rubric("default:text")
    assert text.name == "default-text-v1"
    assert [c.key for c in text.criteria] == [
        "writing_style", "facts_trivia", "educational_value", "required_expertise"]
    assert all(len(c.pointwise_levels) == 6 for c in text.criteria)
    assert all(c.weight == 1.0 for c in text.criteria)

    ui = default_rubric("default:ui")
    assert ui.name == "default-ui-v1"
    assert len(ui.criteria) == 4
    assert ui.criteria[1].key == "tree_screen_consistency"
    assert ui.criteria[1].weight == 1.5

    with pytest.raises(ValueError):
        default_rubric("default:nope")  # type: ignore[arg-type]


# ── rules 17–19: stage-combination matrix (2.3.1) ─────────────────────────


def test_annotate_and_quality_not_both_disabled(env):
    errors = env.errors(project_text=env.project(
        annotate_body="enabled = false", body="[quality]\nenabled = false"))
    has(errors, "quality and annotate must not both be disabled")


def test_verify_requires_annotate(env):
    errors = env.errors(project_text=env.project(
        annotate_body="enabled = false", body="[verify]\nenabled = true"))
    has(errors, 'verify.enabled = true requires annotate.enabled = true (2.3.1 constraint ②)')


def test_generate_requires_text_modality(env):
    project = env.project(input_path=env.input_dir, modality="ui", body=GEN_BODY)
    errors = env.errors(project_text=project)
    has(errors, 'generate.enabled = true requires run.modality = "text"')


def test_generate_process_requires_quality(env):
    body = "[quality]\nenabled = false\n\n" + GEN_BODY
    errors = env.errors(project_text=env.project(body=body))
    has(errors, 'requires quality.enabled = true (seeds come from the quality gate, 2.3.1 constraint ③)')


def test_instruction_required_when_enabled(env):
    errors = env.errors(project_text=env.project(annotate_body=""))
    has(errors, "[annotate].instruction: required when annotate.enabled = true")

    errors = env.errors(project_text=env.project(body="[generate]\nenabled = true"))
    has(errors, "[generate].instruction: required when generate.enabled = true")


# ── rule 21: paths ─────────────────────────────────────────────────────────


def test_input_existence_not_checked_by_m1(env):
    # Input existence is M2's job (Ingestor -> InputError -> exit 3, spec §2.4);
    # M1 must NOT turn a missing input path into a ConfigError (exit 2).
    cfg = env.load(project_text=env.project(input_path=env.tmp / "ghost.jsonl"))
    assert cfg.run.input == str(env.tmp / "ghost.jsonl")


def test_input_required_in_process_mode(env):
    errors = env.errors(project_text=env.project(input_path=None))
    has(errors, "[run].input: required in process mode (may be supplied by CLI --input)")


def test_output_not_inside_input_dir(env):
    project = env.project(input_path=env.input_dir, modality="ui",
                          output_path=env.input_dir / "o.jsonl")
    errors = env.errors(project_text=project)
    has(errors, "[run].output: must not be inside the input directory (self-ingestion guard)")


def test_output_must_not_equal_input_file(env):
    errors = env.errors(project_text=env.project(output_path=env.input_file))
    has(errors, "[run].output: must not be the same as the input file")


def test_output_parent_must_exist(env):
    errors = env.errors(project_text=env.project(
        output_path=env.tmp / "no_dir" / "o.jsonl"))
    has(errors, "[run].output: output parent directory does not exist or is not writable")


def test_output_required(env):
    errors = env.errors(project_text=env.project(output_path=None))
    has(errors, "[run].output: missing required key")


# ── aggregation & warnings ─────────────────────────────────────────────────


def test_aggregates_all_errors_spec_example(env):
    """Reproduces spec 3.1.6 example ②: three errors, one ConfigError, table-row order."""
    schema_with_meta = json.dumps({
        "type": "object",
        "properties": {
            "intent": {"type": "string"},
            "topic": {"type": "string"},
            "_meta": {"type": "object"},
        },
        "required": ["intent", "topic"],
        "additionalProperties": False,
    })
    body = """\
[quality]
llm = "fast"
rubric = "inline"

[rubric]
name = "intent-rubric"

[[rubric.criteria]]
key = "intent_clarity"
weight = 2.0
description = "指令意图是否清晰可辨"
pairwise_prompt = "哪条指令的意图更清晰？"

[[rubric.criteria]]
key = "Topic-Match"
weight = 1.0
description = "话题是否明确可归类"
pairwise_prompt = "哪条指令的话题更明确、更易归类？"
"""
    errors = env.errors(project_text=env.project(body=body, schema=schema_with_meta))
    assert len(errors) == 3, "\n".join(errors)
    fp = str(env.tmp / "project.toml")
    assert 'referenced profile "fast" does not exist in config.toml [llm.*], available: default, judge' in errors[0]
    assert 'must not declare the reserved top-level key "_meta"' in errors[1]
    assert errors[2] == f'{fp}:[[rubric.criteria]][2].key: expected a match of [a-z0-9_]+, got "Topic-Match"'


def test_never_fails_on_first_error(env, monkeypatch):
    monkeypatch.delenv("LK_TEST_KEY_DEFAULT")
    project = env.project(
        input_path=env.tmp / "ghost.jsonl",
        annotate_body='llm = "nope"\ninstruction = "x"\nself_consistency = 2',
        body='[quality]\nselection = "top_ratio"',
        schema="{bad json",
    )
    errors = env.errors(project_text=project)
    assert len(errors) >= 4


def test_self_enhancement_warning(env, capsys):
    same_model = BASE_CONFIG.replace('model = "judge-model"', 'model = "main-model"')
    cfg = env.load(config_text=same_model,
                   project_text=env.project(body="[verify]\nenabled = true"))
    assert cfg.verify.enabled
    err_out = capsys.readouterr().err
    assert "self-enhancement bias" in err_out


def test_ignored_inline_rubric_warns(env, capsys):
    body = "[rubric]\nname = 'unused'\n[[rubric.criteria]]\nkey = 'x'\ndescription = 'd'\npairwise_prompt = 'p'"
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.rubric.name == "default-text-v1"
    assert "the inline rubric has no effect" in capsys.readouterr().err


# ── E2E-finding fixes: P3-7 top_ratio no-op warning / P3-8 judges exemption ──

NO_JUDGE_CONFIG = """\
schema_version = 1

[llm.default]
provider = "openai_compatible"
base_url = "https://example.com/v1"
model = "main-model"
api_key_env = "LK_TEST_KEY_DEFAULT"
"""


def test_top_ratio_without_selection_warns_but_loads(env, capsys):
    cfg = env.load(project_text=env.project(body="[quality]\ntop_ratio = 0.5"))
    assert cfg.quality.top_ratio == 0.5
    assert cfg.quality.selection == "threshold"
    err = capsys.readouterr().err
    assert "warning:" in err
    assert "top_ratio" in err and "no effect" in err


def test_top_ratio_with_selection_does_not_warn(env, capsys):
    cfg = env.load(project_text=env.project(
        body='[quality]\nselection = "top_ratio"\ntop_ratio = 0.5'))
    assert cfg.quality.selection == "top_ratio"
    assert "no effect" not in capsys.readouterr().err


def test_verify_judges_panel_exempts_verify_llm_existence(env):
    # verify.llm defaults to "judge", which does NOT exist in NO_JUDGE_CONFIG —
    # with a non-empty judges panel that must be fine (P3-8): the panel replaces
    # verify.llm at runtime, so only the members are checked.
    cfg = env.load(
        config_text=NO_JUDGE_CONFIG,
        project_text=env.project(
            body='[verify]\nenabled = true\njudges = ["default", "default", "default"]'),
    )
    assert cfg.verify.enabled and len(cfg.verify.judges) == 3


def test_verify_single_judge_still_requires_llm_existence(env):
    errors = env.errors(
        config_text=NO_JUDGE_CONFIG,
        project_text=env.project(body="[verify]\nenabled = true"),
    )
    has(errors, "[verify].llm")


def test_pointwise_judges_warns_noop(env, capsys):
    cfg = env.load(project_text=env.project(
        body='[quality]\nmode = "pointwise"\njudges = ["default", "default", "default"]'))
    assert cfg.quality.mode == "pointwise"
    err = capsys.readouterr().err
    assert "warning:" in err and "judges panel has no effect" in err


def test_pointwise_judges_panel_loads_keyless(env, tmp_path, monkeypatch):
    # v1.17 secret-free：静态面不再解析任何密钥值（run/probe 期才物化）。
    monkeypatch.delenv("LK_TEST_KEY_DEFAULT", raising=False)
    cfg = env.load(project_text=env.project(
        body='[quality]\nmode = "pointwise"\njudges = ["judge", "judge", "judge"]'))
    assert cfg.quality.judges == ("judge", "judge", "judge")


# ── v1.5 plan A: validation hooks (rule 17) ─────────────────────────────────

def _output_with(extra: str) -> str:
    return f"[output]\n{extra}\nschema_inline = \'\'\'\n{SCHEMA}\n\'\'\'"


def test_output_validator_loads_and_dryruns_examples(env):
    cfg = env.load(project_text=env.project(
        annotate_body=('instruction = "标注意图"\n'
                       'examples = [{input = "问路", '
                       'output = {intent = "qa", topic = "问路"}}]'),
        body=_output_with(f'validator = "{HOOK_PY}:topic_max6"'),
        include_output=False,
    ))
    assert cfg.output.validator == f"{HOOK_PY}:topic_max6"


def test_output_validator_bad_ref_is_config_error(env):
    errors = env.errors(project_text=env.project(
        body=_output_with('validator = "no_such_file_xyz.py:fn"'),
        include_output=False,
    ))
    has(errors, "[output].validator")
    has(errors, "does not exist")


def test_output_validator_rejecting_fewshot_is_config_error(env):
    errors = env.errors(project_text=env.project(
        annotate_body=('instruction = "标注意图"\n'
                       'examples = [{input = "问", '
                       'output = {intent = "qa", topic = "这是一个特别长的主题短语"}}]'),
        body=_output_with(f'validator = "{HOOK_PY}:topic_max6"'),
        include_output=False,
    ))
    has(errors, "failed the output.validator callback")


def test_sample_validator_checked_when_generate_enabled(env):
    errors = env.errors(project_text=env.project(
        body=('[quality]\nthreshold = 0.5\n\n'
              '[generate]\nenabled = true\ninstruction = "生成"\n'
              f'sample_validator = "{HOOK_PY}:NOT_CALLABLE"'),
    ))
    has(errors, "[generate].sample_validator")
    has(errors, "is not callable")


# ── v1.7: [classify] parsing + validation (spec 5.2; R8/R21/R24) ────────────

CLASSIFY_BODY = """\
[classify]
enabled = true
fallback_class = "other"

[[classify.classes]]
name = "writing"
description = "写作协助类指令"

[[classify.classes]]
name = "qa"
description = "知识问答类指令"
examples = ["世界上最高的山峰是哪座？"]

[[classify.classes]]
name = "other"
description = "不属于以上任何一类的指令"
"""


def test_classify_defaults_when_absent(env):
    cfg = env.load()
    assert cfg.classify.enabled is False
    assert cfg.classify.llm == "default"
    assert cfg.classify.assignment == "single"
    assert cfg.classify.max_labels is None        # backfill happens only when enabled
    assert cfg.classify.fallback_class == ""
    assert cfg.classify.self_consistency == 0
    assert cfg.classify.sc_temperature == 0.7
    assert cfg.classify.on_error == "fallback"
    assert cfg.classify.classes == ()
    assert cfg.class_views == {}


def test_classify_happy_path_materializes_all_views(env):
    body = CLASSIFY_BODY + """
[class.writing.quality]
threshold = 0.25
[class.writing.annotate]
instruction = "你是写作类指令的意图标注员。"
"""
    cfg = env.load(project_text=env.project(body=body))
    assert [c.name for c in cfg.classify.classes] == ["writing", "qa", "other"]
    assert cfg.classify.classes[1].examples == ("世界上最高的山峰是哪座？",)
    assert cfg.classify.fallback_class == "other"
    assert cfg.classify.max_labels == 3           # backfilled to len(classes)
    # every declared class gets a view — zero-override classes included
    assert set(cfg.class_views) == {"writing", "qa", "other"}
    w = cfg.class_views["writing"]
    assert w.quality.threshold == 0.25            # override applied
    assert w.quality.mode == "pairwise"           # everything else inherited
    assert w.annotate.instruction == "你是写作类指令的意图标注员。"
    assert w.annotate.examples == cfg.annotate.examples
    q = cfg.class_views["qa"]                     # zero-override view = global
    assert q.quality.threshold is None
    assert q.quality.rubric == "default:text"     # selector backfilled per view
    assert q.annotate.instruction == cfg.annotate.instruction
    assert q.rubric is cfg.rubric                 # same resolved rubric object
    assert q.generate == cfg.generate
    assert q.verify == cfg.verify
    # the global sections themselves are untouched by per-class overrides
    assert cfg.quality.threshold is None
    assert cfg.annotate.instruction == "标注意图"


def test_classify_trace_channel_accepted(env):
    body = CLASSIFY_BODY + '\n[trace]\nenabled = true\nchannels = ["classify", "quality"]'
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.trace.channels == ("classify", "quality")


def test_classify_requires_two_classes(env):
    body = """\
[classify]
enabled = true
fallback_class = "solo"

[[classify.classes]]
name = "solo"
description = "唯一类"
"""
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[classify].classes: classify.enabled = true requires >= 2 declared classes")


def test_classify_fallback_required_and_member(env):
    body = CLASSIFY_BODY.replace('fallback_class = "other"\n', "")
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[classify].fallback_class: required when classify.enabled = true")

    body = CLASSIFY_BODY.replace('fallback_class = "other"', 'fallback_class = "ghost"')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[classify].fallback_class: referenced class name "ghost" is not in '
                "[[classify.classes]], available: writing, qa, other")


def test_classify_assignment_and_on_error_enums(env):
    body = CLASSIFY_BODY.replace("enabled = true", 'enabled = true\nassignment = "both"')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[classify].assignment: expected "single" | "multi", got "both"')

    body = CLASSIFY_BODY.replace("enabled = true", 'enabled = true\non_error = "skip"')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[classify].on_error: expected "fallback" | "fail", got "skip"')


def test_classify_max_labels_multi_only(env):
    body = CLASSIFY_BODY.replace("enabled = true", "enabled = true\nmax_labels = 2")
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[classify].max_labels: can only be set when assignment = "multi"')


@pytest.mark.parametrize("value", [1, 4])
def test_classify_max_labels_range(env, value):
    body = CLASSIFY_BODY.replace(
        "enabled = true", f'enabled = true\nassignment = "multi"\nmax_labels = {value}')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, f"[classify].max_labels: expected an integer in [2, 3] (upper bound = number of classes), got {value}")


def test_classify_max_labels_multi_valid_and_backfill(env):
    body = CLASSIFY_BODY.replace(
        "enabled = true", 'enabled = true\nassignment = "multi"\nmax_labels = 2')
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.classify.max_labels == 2           # explicit value kept

    body = CLASSIFY_BODY.replace("enabled = true", 'enabled = true\nassignment = "multi"')
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.classify.max_labels == 3           # absent → backfilled to len(classes)


@pytest.mark.parametrize("value", [1, 2, 4])
def test_classify_self_consistency_rejects_bad_values(env, value):
    body = CLASSIFY_BODY.replace("enabled = true",
                                 f"enabled = true\nself_consistency = {value}")
    errors = env.errors(project_text=env.project(body=body))
    has(errors, f"[classify].self_consistency: expected 0 or an odd number >= 3, got {value}")


def test_classify_self_consistency_accepts_valid(env):
    body = CLASSIFY_BODY.replace("enabled = true",
                                 "enabled = true\nself_consistency = 3\nsc_temperature = 0.5")
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.classify.self_consistency == 3
    assert cfg.classify.sc_temperature == 0.5


def test_classes_name_pattern_uniqueness_description(env):
    body = """\
[classify]
enabled = true
fallback_class = "qa"

[[classify.classes]]
name = "Q-A"
description = "坏名字"

[[classify.classes]]
name = "qa"
description = "问答"

[[classify.classes]]
name = "qa"
description = "重复"

[[classify.classes]]
name = "empty_desc"
"""
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[[classify.classes]][1].name: expected a match of [a-z0-9_]+, got "Q-A"')
    has(errors, '[[classify.classes]][3].name: name must be unique within the table, got duplicate "qa"')
    has(errors, "[[classify.classes]][4].description: missing required key")


def test_classify_llm_profile_checked_when_enabled(env):
    body = CLASSIFY_BODY.replace("enabled = true", 'enabled = true\nllm = "ghost"')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[classify].llm: referenced profile "ghost" does not exist in config.toml [llm.*]')


def test_classify_llm_not_checked_when_disabled(env):
    cfg = env.load(project_text=env.project(body='[classify]\nllm = "ghost"'))
    assert cfg.classify.llm == "ghost"            # inert reference, like verify.llm


def test_classify_llm_keyless_load_when_enabled(env, monkeypatch):
    """v1.17 secret-free：classify 引用 judge 也只在 run/probe 期需要密钥值。"""
    monkeypatch.delenv("LK_TEST_KEY_JUDGE")
    body = CLASSIFY_BODY.replace("enabled = true", 'enabled = true\nllm = "judge"')
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.classify.llm == "judge"


def test_classify_ui_modality_requires_vision(env):
    config = BASE_CONFIG + """
[llm.novision]
provider = "openai_compatible"
base_url = "https://example.com/v1"
model = "blind-model"
api_key_env = "LK_TEST_KEY_DEFAULT"
"""
    body = CLASSIFY_BODY.replace("enabled = true", 'enabled = true\nllm = "novision"')
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    errors = env.errors(config_text=config, project_text=project)
    has(errors, "[llm.novision].supports_vision: a profile referenced by the classify stage(s) in UI modality")


def test_classify_disabled_with_tables_warns_once(env, capsys):
    """R8: parked class config (enabled=false + tables present) is a warning
    naming the ignored tables — NOT a config error."""
    body = (CLASSIFY_BODY.replace("enabled = true", "enabled = false")
            + "\n[class.writing.quality]\nthreshold = 0.25\n")
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.classify.enabled is False
    assert cfg.class_views == {}                  # views only materialize when enabled
    err = capsys.readouterr().err
    assert err.count("[classify].enabled") == 1   # one warning line, not one per table
    assert "[[classify.classes]]" in err
    assert "[class.writing]" in err
    assert "no effect" in err


# ── v1.7: [class.*] whitelist + per-class merge (R6/R7/R25) ────────────────


def test_class_unknown_name_rejected(env):
    body = CLASSIFY_BODY + "\n[class.ghost.quality]\nthreshold = 0.5\n"
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[class.ghost]: class name "ghost" is not in [[classify.classes]], available: writing, qa, other')


def test_class_override_tables_must_be_tables(env):
    body = CLASSIFY_BODY + "\n[class]\nqa = 3\n"
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[class.qa]: expected table, got 3")
    body = CLASSIFY_BODY + "\n[class.qa]\nannotate = 3\n"
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[class.qa.annotate]: expected table, got 3")


def test_class_section_whitelist_enforced(env):
    body = CLASSIFY_BODY + """
[class.qa.dedup]
enabled = false
[class.qa.quality]
llm = "judge"
threshold = 0.5
[class.qa.generate]
num_per_call = 8
"""
    errors = env.errors(project_text=env.project(body=body))
    # section outside the whitelist → error (R25), not a forward-compat warning
    has(errors, "[class.qa.dedup]: section is not in the [class.*] override whitelist "
                "(available: quality, rubric, annotate, generate, verify, extract)")
    # key outside a section's whitelist → error
    has(errors, "[class.qa.quality].llm: [class.*.quality] cannot override this key "
                "(whitelist: mode, rounds, rubric, threshold, selection, top_ratio)")
    has(errors, "[class.qa.generate].num_per_call: [class.*.generate] cannot override this key")
    # whitelisted keys in the same tables merge fine (no error about them)
    assert not any(".threshold" in e for e in errors)


def test_class_selection_group_merge_not_spuriously_exclusive(env):
    """R6 regression: a global threshold plus a class-side top_ratio selection
    (or the reverse) must NOT trip the mutual-exclusion check — the class takes
    over the whole selection group, dropping the global side's pair keys."""
    # forward: global threshold=0.3, class switches to top_ratio selection
    body = ("[quality]\nthreshold = 0.3\n\n" + CLASSIFY_BODY
            + '\n[class.qa.quality]\nselection = "top_ratio"\ntop_ratio = 0.5\n')
    cfg = env.load(project_text=env.project(body=body))
    qa = cfg.class_views["qa"].quality
    assert qa.selection == "top_ratio" and qa.top_ratio == 0.5
    assert qa.threshold is None                   # global pair key dropped from the view
    assert cfg.quality.threshold == 0.3           # global section itself untouched
    other = cfg.class_views["other"].quality      # untouched group inherits globally
    assert other.threshold == 0.3 and other.top_ratio is None

    # reverse: global top_ratio selection, class switches back to a threshold
    body = ('[quality]\nselection = "top_ratio"\ntop_ratio = 0.5\n\n' + CLASSIFY_BODY
            + "\n[class.qa.quality]\nthreshold = 0.3\n")
    cfg = env.load(project_text=env.project(body=body))
    qa = cfg.class_views["qa"].quality
    assert qa.threshold == 0.3 and qa.top_ratio is None
    assert qa.selection == "threshold"            # group restarts from built-in defaults
    assert cfg.class_views["other"].quality.top_ratio == 0.5


def test_class_selection_group_still_exclusive_within_class(env):
    body = (CLASSIFY_BODY
            + '\n[class.qa.quality]\nselection = "top_ratio"\ntop_ratio = 0.5\nthreshold = 0.3\n')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[class.qa.quality].threshold: mutually exclusive with quality.top_ratio")


def test_class_selection_top_ratio_required_on_merged_view(env):
    # the class asks for top_ratio selection but provides no ratio — the global
    # pair keys were dropped by the group takeover, so this is incomplete
    body = ("[quality]\ntop_ratio = 0.5\nselection = \"top_ratio\"\n\n" + CLASSIFY_BODY
            + '\n[class.qa.quality]\nselection = "top_ratio"\n')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[class.qa.quality].top_ratio: required when selection = "top_ratio"')


def test_class_top_ratio_noop_warns(env, capsys):
    body = CLASSIFY_BODY + "\n[class.qa.quality]\ntop_ratio = 0.5\n"
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.class_views["qa"].quality.top_ratio == 0.5
    err = capsys.readouterr().err
    assert "[class.qa.quality].top_ratio" in err and "no effect" in err


CLASS_INLINE_RUBRIC = """
[class.qa.quality]
mode = "pointwise"
rubric = "inline"

[class.qa.rubric]
name = "qa-rubric"

[[class.qa.rubric.criteria]]
key = "factual_density"
description = "事实密度与可核查性"
pairwise_prompt = "哪段问答指令的事实含量更高？"
pointwise_levels = ["0", "1", "2", "3", "4", "5"]
"""


def test_class_inline_rubric_resolved_per_class(env):
    cfg = env.load(project_text=env.project(body=CLASSIFY_BODY + CLASS_INLINE_RUBRIC))
    qa = cfg.class_views["qa"]
    assert qa.quality.mode == "pointwise"
    assert qa.quality.rubric == "inline"
    assert qa.rubric.name == "qa-rubric"
    assert qa.rubric.criteria[0].key == "factual_density"
    # global rubric unaffected; other classes keep the global default
    assert cfg.rubric.name == "default-text-v1"
    assert cfg.class_views["writing"].rubric is cfg.rubric


def test_class_pointwise_six_level_check_on_class_rubric(env):
    body = CLASSIFY_BODY + CLASS_INLINE_RUBRIC.replace(
        'pointwise_levels = ["0", "1", "2", "3", "4", "5"]',
        'pointwise_levels = ["0", "1", "2"]')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[[class.qa.rubric.criteria]][1].pointwise_levels: pointwise mode requires "
                "exactly 6 levels (0-5), got 3")


def test_class_pointwise_with_inherited_default_rubric_ok(env):
    # (class effective mode × class effective rubric): pointwise mode from the
    # class, rubric inherited from the global default — defaults carry 6 levels
    body = CLASSIFY_BODY + '\n[class.qa.quality]\nmode = "pointwise"\n'
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.class_views["qa"].quality.mode == "pointwise"
    assert all(len(c.pointwise_levels) == 6
               for c in cfg.class_views["qa"].rubric.criteria)


def test_class_rubric_table_ignored_when_selector_not_inline(env, capsys):
    body = CLASSIFY_BODY + """
[class.qa.rubric]
name = "unused"
[[class.qa.rubric.criteria]]
key = "x"
description = "d"
pairwise_prompt = "p"
"""
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.class_views["qa"].rubric.name == "default-text-v1"
    err = capsys.readouterr().err
    assert "[[class.qa.rubric.criteria]]" in err and "the inline rubric has no effect" in err


def test_class_selector_can_switch_to_another_packaged_rubric(env):
    # 合并 ③：类选择器与全局选择器不同名时按类重新装载打包准则（全局 default:text
    # 不变，该类拿到 default:ui 那份）。
    body = CLASSIFY_BODY + '\n[class.qa.quality]\nrubric = "default:ui"\n'
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.rubric.name == "default-text-v1"
    assert cfg.class_views["qa"].rubric.name == "default-ui-v1"
    assert cfg.class_views["writing"].rubric.name == "default-text-v1"


def test_class_inline_selector_without_table_errors(env):
    body = CLASSIFY_BODY + '\n[class.qa.quality]\nrubric = "inline"\n'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[class.qa.quality].rubric: rubric = "inline" but [[class.qa.rubric.criteria]] is not provided')


def test_class_inherits_global_inline_rubric(env):
    body = INLINE_RUBRIC + "\n" + CLASSIFY_BODY + "\n[class.qa.quality]\nrounds = 6\n"
    cfg = env.load(project_text=env.project(body=body))
    qa = cfg.class_views["qa"]
    assert qa.quality.rounds == 6
    assert qa.quality.rubric == "inline"          # selector inherited
    assert qa.rubric is cfg.rubric                # global inline product reused
    assert qa.rubric.name == "intent-rubric"


def test_class_annotate_examples_dryrun_against_global_schema(env):
    body = CLASSIFY_BODY + """
[class.qa.annotate]
instruction = "你是问答类指令的标注员。"
examples = [{input = "问路", output = {intent = "nope", topic = "问路"}}]
"""
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[[class.qa.annotate.examples]][1].output: failed user schema validation")


def test_class_annotate_examples_dryrun_through_validator_hook(env):
    body = (CLASSIFY_BODY + f"""
[output]
validator = "{HOOK_PY}:topic_max6"
schema_inline = '''
""" + SCHEMA + """
'''

[class.qa.annotate]
examples = [{input = "问", output = {intent = "qa", topic = "这是一个特别长的主题短语"}}]
""")
    errors = env.errors(project_text=env.project(body=body, include_output=False))
    has(errors, "[[class.qa.annotate.examples]][1].output: failed the output.validator callback")


def test_class_annotate_and_verify_overrides(env):
    body = CLASSIFY_BODY + """
[class.qa.annotate]
examples = [{input = "问路", output = {intent = "qa", topic = "问路"}}]
[class.qa.verify]
extra_criteria = "问答类须核对事实性。"
"""
    cfg = env.load(project_text=env.project(body=body))
    qa = cfg.class_views["qa"]
    assert qa.annotate.instruction == cfg.annotate.instruction   # inherited
    assert qa.annotate.examples[0].output == {"intent": "qa", "topic": "问路"}
    assert qa.verify.extra_criteria == "问答类须核对事实性。"
    assert cfg.verify.extra_criteria == ""        # global untouched


def test_class_annotate_instruction_must_be_nonempty(env):
    body = CLASSIFY_BODY + '\n[class.qa.annotate]\ninstruction = " "\n'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[class.qa.annotate].instruction: expected non-empty string")


def test_class_generate_overrides_with_styles(env):
    body = ("[quality]\nthreshold = 0.5\n\n"
            '[generate]\nenabled = true\ninstruction = "生成中文指令"\n\n'
            + CLASSIFY_BODY + """
[class.qa.generate]
instruction = "模仿示例生成全新的中文知识问答指令。"
num_per_record = 3
temperature = 0.7
[[class.qa.generate.styles]]
name = "colloquial"
prompt = "口语化提问"
""")
    cfg = env.load(project_text=env.project(body=body))
    qa = cfg.class_views["qa"].generate
    assert qa.instruction == "模仿示例生成全新的中文知识问答指令。"
    assert qa.num_per_record == 3 and qa.temperature == 0.7
    assert qa.styles[0].name == "colloquial"
    assert cfg.generate.styles == () and cfg.generate.num_per_record == 2
    # non-overridable keys stay global on the view
    assert qa.llms == cfg.generate.llms and qa.num_per_call == cfg.generate.num_per_call


def test_class_generate_style_errors_use_class_labels(env):
    body = CLASSIFY_BODY + """
[[class.qa.generate.styles]]
name = "dup"
prompt = "p"
[[class.qa.generate.styles]]
name = "dup"
prompt = ""
"""
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[[class.qa.generate.styles]][2].name: name must be unique within the table, got duplicate "dup"')
    has(errors, "[[class.qa.generate.styles]][2].prompt: expected non-empty string")


# ── v1.8: [stream]/[segment]/[extract] parsing + defaults ───────────────────

SEG_ON = "[segment]\nenabled = true\n"


def test_stream_sections_default_when_absent(env):
    cfg = env.load()
    assert cfg.stream.order_by == "input_order"
    assert cfg.stream.on_disorder == "skip"
    assert cfg.stream.key == ()
    assert cfg.stream.gap_s == 300
    assert cfg.stream.gap_steps == 0
    assert cfg.stream.session_max_len == 200
    assert cfg.stream.session_max_span_s == 0
    assert cfg.segment.enabled is False
    assert cfg.segment.strategy == "hybrid"
    assert cfg.segment.llm == "default"
    assert cfg.segment.window == 20
    assert cfg.segment.digest_max_chars == 400
    assert cfg.segment.noise_filter is True
    assert cfg.segment.min_len == 2
    assert cfg.segment.vision_resolved is False    # v1.11 parse product (V1):
    assert cfg.segment.context == ""               # segment disabled → False
    assert cfg.segment.on_error == "keep"
    assert cfg.extract.enabled is False
    assert cfg.extract.llm == "default"
    assert cfg.extract.instruction == ""
    assert cfg.extract.include_diff is True
    assert cfg.extract.on_error == "fallback"
    assert cfg.annotate.sequence_frames == 20


def test_stream_and_segment_sections_parse_explicit_values(env):
    body = """\
[stream]
order_by = "meta:ts"
on_disorder = "fail"
key = ["meta:device", "source_dir"]
gap_s = 600
gap_steps = 5
session_max_len = 100
session_max_span_s = 3600

[segment]
enabled = true
strategy = "llm"
llm = "judge"
window = 8
digest_max_chars = 200
noise_filter = false
min_len = 3
context = "外卖 App 采集流"
on_error = "fail"
"""
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.stream.order_by == "meta:ts"
    assert cfg.stream.on_disorder == "fail"
    assert cfg.stream.key == ("meta:device", "source_dir")
    assert cfg.stream.gap_s == 600 and cfg.stream.gap_steps == 5
    assert cfg.stream.session_max_len == 100
    assert cfg.stream.session_max_span_s == 3600
    assert cfg.segment.enabled is True
    assert cfg.segment.strategy == "llm"
    assert cfg.segment.llm == "judge"
    assert cfg.segment.window == 8
    assert cfg.segment.digest_max_chars == 200
    assert cfg.segment.noise_filter is False
    assert cfg.segment.min_len == 3
    assert cfg.segment.context == "外卖 App 采集流"
    assert cfg.segment.on_error == "fail"
    # text modality → the V1 parse product derives False even with segment on
    assert cfg.segment.vision_resolved is False


def test_extract_section_parses_explicit_values(env):
    body = SEG_ON + """
[extract]
enabled = true
llm = "judge"
instruction = "遵循动作词表。"
include_diff = false
on_error = "fail"
"""
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    cfg = env.load(project_text=project)
    assert cfg.extract.enabled is True
    assert cfg.extract.llm == "judge"
    assert cfg.extract.instruction == "遵循动作词表。"
    assert cfg.extract.include_diff is False
    assert cfg.extract.on_error == "fail"


def test_stream_family_enum_errors(env):
    errors = env.errors(project_text=env.project(body='[segment]\nstrategy = "auto"'))
    has(errors, '[segment].strategy: expected "rules" | "llm" | "hybrid", got "auto"')
    errors = env.errors(project_text=env.project(body='[segment]\non_error = "skip"'))
    has(errors, '[segment].on_error: expected "keep" | "fail", got "skip"')
    errors = env.errors(project_text=env.project(body='[stream]\non_disorder = "drop"'))
    has(errors, '[stream].on_disorder: expected "skip" | "fail", got "drop"')
    errors = env.errors(project_text=env.project(body='[extract]\non_error = "keep"'))
    has(errors, '[extract].on_error: expected "fallback" | "fail", got "keep"')


def test_stream_trace_channels_accepted(env):
    body = '[trace]\nenabled = true\nchannels = ["segment", "extract", "quality"]'
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.trace.channels == ("segment", "extract", "quality")


# ── v1.8 §3.6: stage-combination constraints ────────────────────────────────


def test_segment_requires_process_mode(env):
    project = env.project(input_path=None, run_extra='mode = "generate_only"',
                          body=GEN_BODY + "standalone_count = 10\n\n" + SEG_ON)
    errors = env.errors(project_text=project)
    has(errors, '[segment].enabled: segment.enabled = true requires run.mode = "process"')


def test_segment_generate_mutually_exclusive(env):
    errors = env.errors(project_text=env.project(body=GEN_BODY + "\n" + SEG_ON))
    has(errors, "[segment].enabled: segment.enabled = true and generate.enabled = true are mutually exclusive")


def test_segment_requires_annotate(env):
    errors = env.errors(project_text=env.project(
        annotate_body="enabled = false", body=SEG_ON))
    has(errors, "[segment].enabled: segment.enabled = true requires annotate.enabled = true")


def test_segment_happy_path_loads(env):
    cfg = env.load(project_text=env.project(body=SEG_ON))
    assert cfg.segment.enabled is True


def test_extract_requires_segment_and_ui_modality(env):
    errors = env.errors(project_text=env.project(body="[extract]\nenabled = true"))
    has(errors, "[extract].enabled: extract.enabled = true requires segment.enabled = true")
    has(errors, '[extract].enabled: extract.enabled = true requires run.modality = "ui"')


def test_extract_happy_on_ui_stream(env):
    body = SEG_ON + "\n[extract]\nenabled = true"
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    cfg = env.load(project_text=project)
    assert cfg.extract.enabled is True


def test_stream_order_by_domain(env):
    errors = env.errors(project_text=env.project(body='[stream]\norder_by = "timestamp"'))
    has(errors, '[stream].order_by: expected "input_order" | "meta:<field>", got "timestamp"')
    errors = env.errors(project_text=env.project(body='[stream]\norder_by = "meta:"'))
    has(errors, '[stream].order_by: expected "input_order" | "meta:<field>", got "meta:"')


def test_stream_meta_order_text_only(env):
    project = env.project(input_path=env.input_dir, modality="ui",
                          body='[stream]\norder_by = "meta:ts"')
    errors = env.errors(project_text=project)
    has(errors, '[stream].order_by: "meta:<field>" is only available in text modality')


def test_stream_meta_order_ok_on_text(env):
    cfg = env.load(project_text=env.project(body='[stream]\norder_by = "meta:ts"'))
    assert cfg.stream.order_by == "meta:ts"


def test_session_max_span_requires_meta_order(env):
    errors = env.errors(project_text=env.project(
        body="[stream]\nsession_max_span_s = 60"))
    has(errors, '[stream].session_max_span_s: > 0 requires order_by = "meta:<field>"')
    cfg = env.load(project_text=env.project(
        body='[stream]\norder_by = "meta:ts"\nsession_max_span_s = 60'))
    assert cfg.stream.session_max_span_s == 60


def test_gap_s_explicit_without_meta_warns_not_errors(env, capsys):
    cfg = env.load(project_text=env.project(body="[stream]\ngap_s = 60"))
    assert cfg.stream.gap_s == 60                 # loads — a warning, not an error
    err = capsys.readouterr().err
    assert "warning:" in err
    assert "[stream].gap_s" in err and "no effect" in err


def test_gap_s_default_not_treated_as_intent(env, capsys):
    # gap_s stays at its default (300) — no warning even without meta:* ordering
    env.load(project_text=env.project(body="[stream]\ngap_steps = 5"))
    assert "[stream].gap_s" not in capsys.readouterr().err


def test_gap_s_explicit_with_meta_order_no_warning(env, capsys):
    env.load(project_text=env.project(
        body='[stream]\norder_by = "meta:ts"\ngap_s = 60'))
    assert "[stream].gap_s" not in capsys.readouterr().err


def test_stream_key_element_domain(env):
    errors = env.errors(project_text=env.project(body='[stream]\nkey = ["device"]'))
    has(errors, '[stream].key[1]: expected "meta:<field>" (text only) | "source_dir", got "device"')


def test_stream_key_meta_text_only(env):
    project = env.project(input_path=env.input_dir, modality="ui",
                          body='[stream]\nkey = ["source_dir", "meta:device"]')
    errors = env.errors(project_text=project)
    has(errors, '[stream].key[2]: a "meta:<field>" partition key is only available in text modality')
    assert not any(".key[1]" in e for e in errors)   # source_dir legal on UI


def test_segment_window_minimum(env):
    errors = env.errors(project_text=env.project(body="[segment]\nwindow = 1"))
    has(errors, "[segment].window: expected an integer >= 2")
    cfg = env.load(project_text=env.project(body="[segment]\nwindow = 2"))
    assert cfg.segment.window == 2


@pytest.mark.parametrize("value", [1, 101])
def test_sequence_frames_range_rejected(env, value):
    errors = env.errors(project_text=env.project(
        annotate_body=f'instruction = "标注"\nsequence_frames = {value}'))
    has(errors, f"[annotate].sequence_frames: expected an integer in [2, 100], got {value}")


@pytest.mark.parametrize("value", [2, 100])
def test_sequence_frames_accepts_bounds(env, value):
    cfg = env.load(project_text=env.project(
        annotate_body=f'instruction = "标注"\nsequence_frames = {value}',
        body=SEG_ON))
    assert cfg.annotate.sequence_frames == value


def test_sequence_frames_image_px_warning(env, capsys):
    # default max_image_px = 2048 > 2000 — the S28 hazard fires past 20 frames
    cfg = env.load(project_text=env.project(
        annotate_body='instruction = "标注"\nsequence_frames = 25', body=SEG_ON))
    assert cfg.annotate.sequence_frames == 25
    err = capsys.readouterr().err
    assert "warning:" in err
    assert "[annotate].sequence_frames" in err and "max_image_px" in err


def test_sequence_frames_image_px_no_warning_at_2000(env, capsys):
    config = BASE_CONFIG.replace(
        "supports_structured_output = true",
        "supports_structured_output = true\nmax_image_px = 2000")
    env.load(config_text=config, project_text=env.project(
        annotate_body='instruction = "标注"\nsequence_frames = 25', body=SEG_ON))
    assert "max_image_px" not in capsys.readouterr().err


def test_session_max_len_exceeds_batch_warns(env, capsys):
    env.load(project_text=env.project(run_extra="batch_size = 100", body=SEG_ON))
    err = capsys.readouterr().err
    assert "[stream].session_max_len" in err and "hard-cut" in err


def test_session_max_len_within_batch_no_warning(env, capsys):
    env.load(project_text=env.project(body=SEG_ON))    # 200 <= 256
    assert "[stream].session_max_len" not in capsys.readouterr().err


# ── v1.8 no-op warnings (R8 family) ─────────────────────────────────────────


def test_stream_family_parked_warns_once_naming_tables(env, capsys):
    body = ('[stream]\ngap_steps = 5\n\n[segment]\nstrategy = "rules"\n\n'
            '[extract]\ninstruction = "x"\n')
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.segment.enabled is False
    err = capsys.readouterr().err
    assert err.count("[segment].enabled") == 1    # one warning line, not one per table
    assert "[stream]" in err and "[segment]" in err and "[extract]" in err
    assert "no effect" in err


def test_segment_enabled_false_alone_no_parked_warning(env, capsys):
    env.load(project_text=env.project(body="[segment]\nenabled = false"))
    assert "no effect" not in capsys.readouterr().err


def test_rules_strategy_noise_filter_noop_warns(env, capsys):
    cfg = env.load(project_text=env.project(
        body=SEG_ON + 'strategy = "rules"'))
    assert cfg.segment.strategy == "rules"
    err = capsys.readouterr().err
    assert "[segment].noise_filter" in err and "no effect" in err


def test_hybrid_strategy_no_noise_filter_warning(env, capsys):
    env.load(project_text=env.project(body=SEG_ON))
    assert "[segment].noise_filter" not in capsys.readouterr().err


def test_sequence_frames_noop_without_stream_warns(env, capsys):
    cfg = env.load(project_text=env.project(
        annotate_body='instruction = "标注"\nsequence_frames = 10'))
    assert cfg.annotate.sequence_frames == 10
    err = capsys.readouterr().err
    assert "[annotate].sequence_frames" in err and "no effect" in err


def test_stream_quality_without_extract_hints_frame_digest_scoring(env, capsys):
    env.load(project_text=env.project(body=SEG_ON))
    assert "frame digests" in capsys.readouterr().err


def test_stream_quality_with_extract_no_hint(env, capsys):
    body = SEG_ON + "\n[extract]\nenabled = true"
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    env.load(project_text=project)
    assert "frame digests" not in capsys.readouterr().err


def test_stream_explicit_non_trajectory_rubric_no_hint(env, capsys):
    # S29 advisory fires only when the EFFECTIVE rubric is default:trajectory —
    # an explicit default:text choice scores by its own criteria and must not
    # be told it is doing trajectory scoring.
    body = SEG_ON + '\n[quality]\nrubric = "default:text"'
    env.load(project_text=env.project(body=body))
    assert "frame digests" not in capsys.readouterr().err


# ── v1.8 rubric: default:trajectory + stream empty-selector resolution ─────


def test_default_trajectory_rubric_loads_from_package():
    tr = default_rubric("default:trajectory")
    assert tr.name == "default-trajectory-v1"
    assert [c.key for c in tr.criteria] == [
        "completion", "coherence", "purposefulness", "noise_residue"]
    assert all(len(c.pointwise_levels) == 6 for c in tr.criteria)
    assert all(c.weight == 1.0 for c in tr.criteria)
    assert all(c.description and c.pairwise_prompt for c in tr.criteria)


def test_stream_empty_rubric_resolves_trajectory_text(env):
    cfg = env.load(project_text=env.project(body=SEG_ON))
    assert cfg.quality.rubric == "default:trajectory"
    assert cfg.rubric.name == "default-trajectory-v1"


def test_stream_empty_rubric_resolves_trajectory_ui_too(env):
    body = SEG_ON + 'strategy = "rules"'
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    cfg = env.load(project_text=project)
    assert cfg.quality.rubric == "default:trajectory"
    assert cfg.rubric.name == "default-trajectory-v1"


def test_stream_explicit_selector_beats_trajectory_default(env):
    body = SEG_ON + '\n[quality]\nrubric = "default:text"'
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.rubric.name == "default-text-v1"


def test_trajectory_selector_explicit_without_stream(env):
    cfg = env.load(project_text=env.project(
        body='[quality]\nrubric = "default:trajectory"'))
    assert cfg.rubric.name == "default-trajectory-v1"


def test_stream_pointwise_trajectory_passes_six_level_check(env):
    body = SEG_ON + '\n[quality]\nmode = "pointwise"'
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.rubric.name == "default-trajectory-v1"
    assert all(len(c.pointwise_levels) == 6 for c in cfg.rubric.criteria)


def test_stream_classify_views_inherit_trajectory_selector(env):
    body = SEG_ON + "\n" + CLASSIFY_BODY
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.class_views["qa"].quality.rubric == "default:trajectory"
    assert cfg.class_views["qa"].rubric is cfg.rubric


# ── v1.8 reference sets (S30): segment/extract × existence/keys/vision ─────


def test_segment_llm_existence_only_for_llm_strategies(env):
    errors = env.errors(project_text=env.project(body=SEG_ON + 'llm = "ghost"'))
    has(errors, '[segment].llm: referenced profile "ghost" does not exist in config.toml [llm.*]')
    # rules strategy makes zero LLM calls — the same reference is inert
    cfg = env.load(project_text=env.project(
        body=SEG_ON + 'strategy = "rules"\nllm = "ghost"'))
    assert cfg.segment.llm == "ghost"


def test_segment_llm_keyless_for_llm_strategies(env, monkeypatch):
    """v1.17 secret-free：llm 策略引用 judge 的静态 load 无需密钥值。"""
    monkeypatch.delenv("LK_TEST_KEY_JUDGE")
    cfg = env.load(project_text=env.project(body=SEG_ON + 'llm = "judge"'))
    assert cfg.segment.llm == "judge"
    assert cfg.llm_profiles["judge"].api_key_envs == ("LK_TEST_KEY_JUDGE",)


def test_segment_llm_not_referenced_when_disabled(env, monkeypatch):
    monkeypatch.delenv("LK_TEST_KEY_JUDGE")
    cfg = env.load(project_text=env.project(body='[segment]\nllm = "judge"'))
    assert not hasattr(cfg.llm_profiles["judge"], "api_keys")


def test_extract_llm_existence_and_keyless_load_when_enabled(env, monkeypatch):
    body = SEG_ON + 'strategy = "rules"\n\n[extract]\nenabled = true\nllm = "judge"'
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    monkeypatch.delenv("LK_TEST_KEY_JUDGE")
    cfg = env.load(project_text=project)
    assert cfg.extract.llm == "judge"


NOVISION_PROFILE = """
[llm.novision]
provider = "openai_compatible"
base_url = "https://example.com/v1"
model = "blind-model"
api_key_env = "LK_TEST_KEY_DEFAULT"
"""


def test_extract_llm_always_needs_vision(env):
    body = SEG_ON + '\n[extract]\nenabled = true\nllm = "novision"'
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    errors = env.errors(config_text=BASE_CONFIG + NOVISION_PROFILE,
                        project_text=project)
    has(errors, "[llm.novision].supports_vision: a profile referenced by the extract stage(s) in UI modality")


def test_segment_llm_never_needs_vision_and_vision_resolved_derives(env):
    # v1.11 (V1/V3): segment is ADAPTIVE about vision — it never joins the
    # vision-required set; the parse product derives from profile capability.
    body = SEG_ON + 'llm = "novision"'
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    cfg = env.load(config_text=BASE_CONFIG + NOVISION_PROFILE, project_text=project)
    assert cfg.segment.llm == "novision"
    assert cfg.segment.vision_resolved is False    # capability off → pure text
    # capable profile under UI modality → the same config flips to multi-image
    body = SEG_ON + 'llm = "judge"'
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    cfg = env.load(project_text=project)
    assert cfg.segment.vision_resolved is True


def test_stream_quality_vision_relaxed(env):
    # S30: stream-mode quality scores sequences as pure text — a vision-less
    # quality profile is legal exactly when segment.enabled
    body = SEG_ON + 'strategy = "rules"\n\n[quality]\nllm = "novision"'
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    cfg = env.load(config_text=BASE_CONFIG + NOVISION_PROFILE, project_text=project)
    assert cfg.quality.llm == "novision"


def test_nonstream_quality_vision_still_required(env):
    project = env.project(input_path=env.input_dir, modality="ui",
                          body='[quality]\nllm = "novision"')
    errors = env.errors(config_text=BASE_CONFIG + NOVISION_PROFILE,
                        project_text=project)
    has(errors, "[llm.novision].supports_vision: a profile referenced by the quality stage(s) in UI modality")


# ── v1.8 [class.<name>.extract] whitelist (S2) ─────────────────────────────


def test_class_extract_instruction_override(env):
    body = CLASSIFY_BODY + '\n[class.qa.extract]\ninstruction = "问答类摘取指令。"\n'
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.class_views["qa"].extract.instruction == "问答类摘取指令。"
    # untouched classes carry the global extract; the global section is untouched
    assert cfg.class_views["writing"].extract == cfg.extract
    assert cfg.extract.instruction == ""


def test_class_extract_whitelist_rejects_other_keys(env):
    body = CLASSIFY_BODY + '\n[class.qa.extract]\nllm = "judge"\nenabled = true\n'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[class.qa.extract].llm: [class.*.extract] cannot override this key (whitelist: instruction)")
    has(errors, "[class.qa.extract].enabled: [class.*.extract] cannot override this key")


# ── v1.9: [stitch] parsing + defaults + constraints (T17) ───────────────────

STITCH_ON = SEG_ON + "\n[stitch]\nenabled = true\n"


def test_stitch_section_defaults_when_absent(env):
    cfg = env.load()
    assert cfg.stitch.enabled is False
    assert cfg.stitch.llm == "default"
    assert cfg.stitch.max_open == 4
    assert cfg.stitch.bias == "conservative"
    assert cfg.stitch.rescue_short is True
    assert cfg.stitch.repass is True
    assert cfg.stitch.stale_gap_steps == 0
    assert cfg.stitch.digest_max_chars == 400
    assert cfg.stitch.context == ""
    assert cfg.stitch.votes == 1
    assert cfg.stitch.on_error == "keep"


def test_stitch_section_parses_explicit_values(env):
    body = SEG_ON + """
[stitch]
enabled = true
llm = "judge"
max_open = 6
bias = "llm"
rescue_short = false
repass = false
stale_gap_steps = 8
digest_max_chars = 200
context = "同一任务可被切走后恢复"
votes = 3
on_error = "fail"
"""
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.stitch.enabled is True
    assert cfg.stitch.llm == "judge"
    assert cfg.stitch.max_open == 6
    assert cfg.stitch.bias == "llm"
    assert cfg.stitch.rescue_short is False
    assert cfg.stitch.repass is False
    assert cfg.stitch.stale_gap_steps == 8
    assert cfg.stitch.digest_max_chars == 200
    assert cfg.stitch.context == "同一任务可被切走后恢复"
    assert cfg.stitch.votes == 3
    assert cfg.stitch.on_error == "fail"


def test_stitch_enum_and_numeric_errors(env):
    errors = env.errors(project_text=env.project(body='[stitch]\nbias = "auto"'))
    has(errors, '[stitch].bias: expected "conservative" | "llm", got "auto"')
    errors = env.errors(project_text=env.project(body='[stitch]\non_error = "skip"'))
    has(errors, '[stitch].on_error: expected "keep" | "fail", got "skip"')
    errors = env.errors(project_text=env.project(body="[stitch]\nmax_open = 0"))
    has(errors, "[stitch].max_open: expected positive integer, got 0")
    errors = env.errors(project_text=env.project(body="[stitch]\nvotes = 0"))
    has(errors, "[stitch].votes: expected positive integer, got 0")


def test_stitch_requires_segment(env):
    errors = env.errors(project_text=env.project(body="[stitch]\nenabled = true"))
    has(errors, "[stitch].enabled: stitch.enabled = true requires segment.enabled = true "
                "(thread stitching only applies to segmentation products)")


def test_stitch_happy_path_loads(env):
    cfg = env.load(project_text=env.project(body=STITCH_ON))
    assert cfg.stitch.enabled is True


@pytest.mark.parametrize("value", [2, 4])
def test_stitch_votes_even_rejected(env, value):
    errors = env.errors(project_text=env.project(
        body=STITCH_ON + f"votes = {value}"))
    has(errors, f"[stitch].votes: expected an odd number >= 1 (strict majority over "
                f"(verdict, thread_ref)), got {value}")


@pytest.mark.parametrize("value", [1, 3, 5])
def test_stitch_votes_odd_accepted(env, value):
    cfg = env.load(project_text=env.project(body=STITCH_ON + f"votes = {value}"))
    assert cfg.stitch.votes == value


def test_stitch_rules_strategy_warns(env, capsys):
    body = SEG_ON + 'strategy = "rules"\n\n[stitch]\nenabled = true'
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.stitch.enabled is True                 # advisory, not an error
    err = capsys.readouterr().err
    assert "[stitch].enabled" in err and "stitching consumes coarse whole-session segments" in err


def test_stitch_hybrid_strategy_no_rules_warning(env, capsys):
    env.load(project_text=env.project(body=STITCH_ON))
    assert "coarse whole-session segments" not in capsys.readouterr().err


def test_stitch_trace_channel_accepted_eleven_values(env):
    from labelkit.common.config.loader import _TRACE_CHANNELS

    assert _TRACE_CHANNELS == ("ingest", "dedup", "segment", "stitch", "extract",
                               "classify", "quality", "annotate", "verify",
                               "schema", "llm")       # v1.9: 11 values (T16)
    body = '[trace]\nenabled = true\nchannels = ["stitch", "segment"]'
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.trace.channels == ("stitch", "segment")


def test_stitch_llm_existence_and_keyless_load_when_enabled(env, monkeypatch):
    errors = env.errors(project_text=env.project(body=STITCH_ON + 'llm = "ghost"'))
    has(errors, '[stitch].llm: referenced profile "ghost" does not exist in config.toml [llm.*]')
    monkeypatch.delenv("LK_TEST_KEY_JUDGE")
    cfg = env.load(project_text=env.project(body=STITCH_ON + 'llm = "judge"'))
    assert cfg.stitch.llm == "judge"


def test_stitch_llm_not_referenced_when_disabled(env, monkeypatch):
    monkeypatch.delenv("LK_TEST_KEY_JUDGE")
    cfg = env.load(project_text=env.project(body=SEG_ON + '\n[stitch]\nllm = "judge"'))
    assert not hasattr(cfg.llm_profiles["judge"], "api_keys")


def test_stitch_llm_never_needs_vision(env):
    # T16: pure-text judgment — a vision-less stitch profile is legal on UI
    # modality even while stitch is enabled (NOT in any vision-required set)
    body = (SEG_ON + 'strategy = "rules"\n\n[stitch]\nenabled = true\n'
            'llm = "novision"')
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    cfg = env.load(config_text=BASE_CONFIG + NOVISION_PROFILE, project_text=project)
    assert cfg.stitch.llm == "novision"


def test_class_stitch_section_rejected(env):
    # T17: [class.<name>.stitch] must not exist — stitch mirrors segment's
    # exclusion from the per-class override whitelist
    body = CLASSIFY_BODY + '\n[class.qa.stitch]\nbias = "llm"\n'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[class.qa.stitch]: section is not in the [class.*] override whitelist")


def test_stitch_payload_while_off_segment_on_warns_separately(env, capsys):
    # T17 branch ownership: segment ON keeps the parked list silent — the
    # combination gets its own warning (sequence_frames form)
    body = SEG_ON + "\n[stitch]\nmax_open = 6"
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.stitch.enabled is False and cfg.stitch.max_open == 6
    err = capsys.readouterr().err
    assert "[stitch].enabled" in err and "stitch.enabled = false" in err
    assert "no effect" in err


def test_stitch_payload_joins_parked_list_when_segment_off(env, capsys):
    body = "[stitch]\nmax_open = 6\n"
    env.load(project_text=env.project(body=body))
    err = capsys.readouterr().err
    assert "[segment].enabled" in err and "[stitch]" in err
    assert "no effect" in err


def test_stitch_enabled_false_alone_no_noop_warning(env, capsys):
    env.load(project_text=env.project(body=SEG_ON + "\n[stitch]\nenabled = false"))
    assert "[stitch]" not in capsys.readouterr().err


# ── v1.10: [console] parsing + mode_resolved (spec 5.1 / 3.1.4 console row, §7.7) ──


CONSOLE_RICH = BASE_CONFIG + '\n[console]\nmode = "rich"\n'
JSONL_TOOL = BASE_CONFIG.replace('log_level = "info"',
                                 'log_level = "info"\nlog_format = "jsonl"')


@pytest.fixture
def rich_importable(monkeypatch):
    """Pin the find_spec probe truthy — tests stay hermetic whether or not the
    venv happens to carry rich (it enters pyproject only in Wave 2)."""
    monkeypatch.setattr(loader_mod, "_find_spec", lambda name: object())


@pytest.fixture
def rich_unimportable(monkeypatch):
    monkeypatch.setattr(loader_mod, "_find_spec", lambda name: None)


def test_console_defaults_whole_section_optional(env, monkeypatch):
    monkeypatch.setenv("TERM", "dumb")            # pins the auto chain → plain
    cfg = env.load()
    assert cfg.console == ConsoleConfig(mode="auto", refresh_hz=5, heartbeat_s=0,
                                        estimate=False, interactive=True,
                                        mode_resolved="plain")


def test_console_section_parsed(env, monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    body = ('\n[console]\nmode = "plain"\nrefresh_hz = 10\nheartbeat_s = 30\n'
            "estimate = true\ninteractive = false\n")
    cfg = env.load(config_text=BASE_CONFIG + body)
    assert cfg.console.mode == "plain"
    assert cfg.console.refresh_hz == 10
    assert cfg.console.heartbeat_s == 30
    assert cfg.console.estimate is True
    assert cfg.console.interactive is False
    assert cfg.console.mode_resolved == "plain"


def test_console_mode_enum_rejected(env):
    errors = env.errors(config_text=BASE_CONFIG + '\n[console]\nmode = "fancy"\n')
    has(errors, '[console].mode: expected "auto" | "rich" | "plain", got "fancy"')


@pytest.mark.parametrize("value", [0, 11, -3])
def test_console_refresh_hz_out_of_range_rejected(env, value):
    errors = env.errors(config_text=BASE_CONFIG + f"\n[console]\nrefresh_hz = {value}\n")
    has(errors, f"[console].refresh_hz: expected an integer in [1, 10]")
    has(errors, f"got {value}")


@pytest.mark.parametrize("value", [1, 5, 10])
def test_console_refresh_hz_bounds_inclusive(env, value):
    cfg = env.load(config_text=BASE_CONFIG + f"\n[console]\nrefresh_hz = {value}\n")
    assert cfg.console.refresh_hz == value


def test_console_heartbeat_negative_rejected(env):
    errors = env.errors(config_text=BASE_CONFIG + "\n[console]\nheartbeat_s = -1\n")
    has(errors, "[console].heartbeat_s: expected non-negative integer, got -1")


def test_console_bool_keys_type_checked(env):
    errors = env.errors(config_text=BASE_CONFIG
                        + '\n[console]\nestimate = "yes"\ninteractive = 1\n')
    has(errors, '[console].estimate: expected boolean, got "yes"')
    has(errors, "[console].interactive: expected boolean, got 1")


def test_console_errors_aggregate_not_first_raise(env):
    """spec 3.1.5: ALL [console] violations join the single aggregated
    ConfigError alongside project-side errors — never first-error-only."""
    bad_console = BASE_CONFIG + "\n[console]\nrefresh_hz = 0\nheartbeat_s = -1\n"
    errors = env.errors(config_text=bad_console,
                        project_text=env.project(body='[quality]\nllm = "ghost"'))
    has(errors, "[console].refresh_hz: expected an integer in [1, 10]")
    has(errors, "[console].heartbeat_s: expected non-negative integer")
    has(errors, '[quality].llm: referenced profile "ghost" does not exist')
    assert len(errors) >= 3


def test_console_unknown_key_warns_section_owned(env, capsys, monkeypatch):
    """Unknown keys INSIDE [console] warn (forward compat); the [console] table
    itself is owned top-level now — no unknown-top-level-table warning."""
    monkeypatch.setenv("TERM", "dumb")
    cfg = env.load(config_text=BASE_CONFIG + "\n[console]\nfancy_new_key = 1\n")
    assert isinstance(cfg, ResolvedConfig)
    err = capsys.readouterr().err
    assert "[console].fancy_new_key: unknown key" in err
    assert ":console: unknown key" not in err          # not flagged as an unknown table


def test_console_jsonl_forces_plain_over_explicit_config_rich(env, capsys,
                                                              rich_importable):
    cfg = env.load(config_text=JSONL_TOOL + '\n[console]\nmode = "rich"\n')
    assert cfg.tool.log_format == "jsonl"
    assert cfg.console.mode_resolved == "plain"
    err = capsys.readouterr().err
    assert ('console: log_format="jsonl" forces plain - an explicit rich has no '
            "effect (the line-parseable stderr invariant, 7.7)") in err
    assert err.count("forces plain") == 1            # WARN exactly once


def test_console_jsonl_forces_plain_over_explicit_cli_rich(env, capsys,
                                                           rich_importable):
    cfg = env.load(config_text=JSONL_TOOL, cli=CliOverrides(console="rich"))
    assert cfg.console.mode == "rich"              # CLI precedence recorded
    assert cfg.console.mode_resolved == "plain"    # ... but jsonl wins (§7.7 铁律)
    assert "forces plain" in capsys.readouterr().err


def test_console_jsonl_auto_plain_without_warning(env, capsys):
    cfg = env.load(config_text=JSONL_TOOL)
    assert cfg.console.mode_resolved == "plain"
    assert "forces plain" not in capsys.readouterr().err   # no explicit rich, no WARN


def test_console_explicit_rich_honored_without_tty(env, rich_importable):
    """§7.7 matrix: --console rich is respected even when stderr is not a TTY
    (CI ANSI-recording scenario) — only importability/jsonl can demote it."""
    cfg = env.load(config_text=CONSOLE_RICH)
    assert cfg.console.mode == "rich"
    assert cfg.console.mode_resolved == "rich"


def test_console_rich_unimportable_degrades_plain_with_warning(env, capsys,
                                                               rich_unimportable):
    cfg = env.load(config_text=CONSOLE_RICH)
    assert cfg.console.mode_resolved == "plain"
    err = capsys.readouterr().err
    assert "console: rich is not importable, demoted to plain" in err
    assert err.count("rich is not importable") == 1         # WARN exactly once


def test_console_cli_overrides_config_mode(env, rich_importable):
    # CLI plain beats config rich (2.5 precedence) — no degrade warning path
    cfg = env.load(config_text=CONSOLE_RICH, cli=CliOverrides(console="plain"))
    assert cfg.console.mode == "plain"
    assert cfg.console.mode_resolved == "plain"
    # CLI rich beats config plain
    cfg = env.load(config_text=BASE_CONFIG + '\n[console]\nmode = "plain"\n',
                   cli=CliOverrides(console="rich"))
    assert cfg.console.mode == "rich"
    assert cfg.console.mode_resolved == "rich"


def test_console_auto_term_dumb_resolves_plain(env, monkeypatch, rich_importable):
    monkeypatch.setenv("TERM", "dumb")
    cfg = env.load()
    assert cfg.console.mode == "auto"
    assert cfg.console.mode_resolved == "plain"


def test_console_no_color_does_not_participate(env, monkeypatch, rich_importable):
    """U25: NO_COLOR never demotes to plain — rich natively strips color while
    keeping layout. Explicit rich under NO_COLOR stays rich."""
    monkeypatch.setenv("NO_COLOR", "1")
    cfg = env.load(config_text=CONSOLE_RICH)
    assert cfg.console.mode_resolved == "rich"


@pytest.mark.parametrize(
    "isatty,log_format,term,importable,expected",
    [
        (True, "text", "xterm-256color", True, "rich"),   # all four legs green
        (False, "text", "xterm-256color", True, "plain"), # not a TTY
        (True, "jsonl", "xterm-256color", True, "plain"), # jsonl line-parse rule
        (True, "text", "dumb", True, "plain"),            # TERM dumb
        (True, "text", "", True, "plain"),                # TERM empty
        (True, "text", None, True, "plain"),              # TERM absent
        (True, "text", "xterm-256color", False, "plain"), # rich not importable
        (False, "jsonl", None, False, "plain"),           # everything red
    ],
)
def test_auto_console_mode_chain_branches(isatty, log_format, term, importable,
                                          expected):
    """The §7.7 auto decision chain as a pure function over injected probes
    (U5/U25) — every branch offline, no TTY/env manipulation needed."""
    assert loader_mod._auto_console_mode(
        isatty=isatty, log_format=log_format, term=term,
        rich_importable=importable) == expected


# ── v1.11: context budget & vision auto-derivation (spec 3.1.4 上下文预算行) ─


def _cw_config(cw, *, max_out=None, extra="") -> str:
    """BASE_CONFIG with budget keys spliced into [llm.default] (POOL_CONFIG
    string-replacement pattern)."""
    add = f"context_window = {cw}"
    if max_out is not None:
        add = f"max_output_tokens = {max_out}\n" + add
    if extra:
        add += "\n" + extra
    return BASE_CONFIG.replace("supports_structured_output = true",
                               "supports_structured_output = true\n" + add, 1)


def test_context_window_parses_and_zero_is_the_default(env):
    cfg = env.load()
    assert cfg.llm_profiles["default"].context_window == 0    # undeclared = off
    assert cfg.embedding_profiles["emb"].context_window == 0
    cfg = env.load(config_text=_cw_config(131072))
    assert cfg.llm_profiles["default"].context_window == 131072


def test_embedding_context_window_parses(env):
    config = BASE_CONFIG.replace('model = "bge"',
                                 'model = "bge"\ncontext_window = 8192', 1)
    cfg = env.load(config_text=config)
    assert cfg.embedding_profiles["emb"].context_window == 8192


def test_context_window_negative_is_error(env):
    errors = env.errors(config_text=_cw_config(-1))
    has(errors, "[llm.default].context_window: expected non-negative integer, got -1")


def test_context_window_non_positive_budget_is_error(env):
    # cw == max_output_tokens (default 4096): margin swallows everything (V6)
    errors = env.errors(config_text=_cw_config(4096))
    has(errors, "[llm.default].context_window: declared window leaves a non-positive budget")
    # embedding flavor: cw ≤ margin floor (V15)
    config = BASE_CONFIG.replace('model = "bge"',
                                 'model = "bge"\ncontext_window = 200', 1)
    errors = env.errors(config_text=config)
    has(errors, "[embedding.emb].context_window: declared window leaves a non-positive budget")


def test_default_image_px_validation(env):
    cfg = env.load()                                          # 0 = use max (legal)
    assert cfg.llm_profiles["default"].default_image_px == 0
    config = _cw_config(131072, extra="default_image_px = 1024")
    cfg = env.load(config_text=config)
    assert cfg.llm_profiles["default"].default_image_px == 1024
    config = BASE_CONFIG.replace("supports_structured_output = true",
                                 "supports_structured_output = true\n"
                                 "default_image_px = 4096", 1)
    errors = env.errors(config_text=config)
    has(errors, "[llm.default].default_image_px: expected <= max_image_px (2048), got 4096")


def test_removed_use_vision_key_is_directed_error(env, capsys):
    # V2/V27②: raw-section probe → migration guidance, both key values
    for literal in ("false", "true"):
        errors = env.errors(project_text=env.project(
            body=SEG_ON + f"use_vision = {literal}"))
        has(errors, "[segment].use_vision: segment.use_vision was removed in v1.11")
        has(errors, "derived automatically from supports_vision")
        has(errors, "point segment.llm at a text-only profile")
    # never double-reported through the unknown-key forward-compat WARN
    assert "use_vision: unknown key" not in capsys.readouterr().err


@pytest.mark.parametrize("modality,strategy,profile,expected", [
    ("ui", "hybrid", "judge", True),
    ("ui", "llm", "judge", True),
    ("ui", "rules", "judge", False),        # rules strategy makes zero LLM calls
    ("text", "hybrid", "judge", False),     # text modality never attaches frames
    ("ui", "hybrid", "novision", False),    # capability off → pure text
])
def test_vision_resolved_derivation_matrix(env, modality, strategy, profile,
                                           expected):
    body = SEG_ON + f'strategy = "{strategy}"\nllm = "{profile}"'
    kw = dict(body=body)
    if modality == "ui":
        kw.update(input_path=env.input_dir, modality="ui")
    cfg = env.load(config_text=BASE_CONFIG + NOVISION_PROFILE,
                   project_text=env.project(**kw))
    assert cfg.segment.vision_resolved is expected


def test_vision_resolved_false_while_segment_disabled(env):
    project = env.project(input_path=env.input_dir, modality="ui",
                          body='[segment]\nllm = "judge"')
    cfg = env.load(project_text=project)
    assert cfg.segment.vision_resolved is False


def test_segment_vision_window_image_px_warning(env, capsys):
    # V5 (S28 sibling): vision_resolved ∧ window > 20 ∧ max_image_px > 2000
    body = SEG_ON + "window = 21"
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    env.load(project_text=project)      # [llm.default]: vision on, px 2048
    err = capsys.readouterr().err
    assert "[segment].window: window = 21 > 20" in err
    assert "max_image_px = 2048" in err
    # default window = 20 sits inside the boundary — never fires
    project = env.project(input_path=env.input_dir, modality="ui", body=SEG_ON)
    env.load(project_text=project)
    assert "[segment].window: window =" not in capsys.readouterr().err


def test_undeclared_context_window_reference_warns_once(env, capsys):
    env.load()                                              # default referenced
    err = capsys.readouterr().err
    assert "[llm.default].context_window: referenced by an enabled stage but not declared" in err
    assert err.count("[llm.default].context_window") == 1   # once per profile
    assert "[llm.judge].context_window" not in err          # unreferenced: silent


def test_declared_context_window_reference_does_not_warn(env, capsys):
    env.load(config_text=_cw_config(131072))
    assert "[llm.default].context_window" not in capsys.readouterr().err


def test_static_system_precheck_error_when_nothing_fits(env):
    # V13③ ERROR leg: cw 4864 / max_out 4096 → margin 487 → input budget 281;
    # annotate static = head 32 + instruction 300 + schema 87 ≥ 281.
    project = env.project(annotate_body=f'instruction = "{"标" * 300}"')
    errors = env.errors(config_text=_cw_config(4864), project_text=project)
    has(errors, "[annotate]: static system-side prompt parts estimated")
    has(errors, "no record can fit")


def test_static_system_precheck_warns_past_half_budget(env, capsys):
    # V13③ WARN leg (A5, 50%): cw 5500 → input budget 854; annotate static =
    # 32 + 400 + 87 = 519 ∈ (427, 854) → WARN, run loads.
    project = env.project(annotate_body=f'instruction = "{"标" * 400}"')
    cfg = env.load(config_text=_cw_config(5500), project_text=project)
    assert isinstance(cfg, ResolvedConfig)
    err = capsys.readouterr().err
    assert "[annotate]: static system-side prompt parts estimated" in err
    assert "50%" in err


def test_static_system_precheck_silent_with_room(env, capsys):
    env.load(config_text=_cw_config(131072))
    assert "static system-side prompt parts estimated" not in capsys.readouterr().err


def test_min_window_guard_warns_at_floor_two(env, capsys):
    # V9: cw 3200 / max_out 1024 → input budget 1856; per-frame worst 528,
    # segment static 492 (V22 full scaffolding) → w_min = 2 == floor
    # (verify off → floor 2).
    cfg = env.load(config_text=_cw_config(3200, max_out=1024),
                   project_text=env.project(body=SEG_ON))
    assert isinstance(cfg, ResolvedConfig)
    err = capsys.readouterr().err
    assert "[segment].window: worst-case guaranteed packing size w_min = 2" in err
    assert "every frame is a seam" in err


def test_min_window_guard_errors_below_repair_floor(env):
    # F14: verify.policy = "repair" lifts the floor to 3 (the fixed 3-frame
    # member-reclaim re-judgment window) → w_min 2 < 3 is a CONFIG_ERROR.
    body = SEG_ON + '\n[verify]\nenabled = true\npolicy = "repair"\nllm = "judge"'
    errors = env.errors(config_text=_cw_config(3200, max_out=1024),
                        project_text=env.project(body=body))
    has(errors, "[segment].window: worst-case guaranteed packing size w_min = 2 < floor = 3")


def test_min_window_guard_drop_policy_keeps_floor_two(env, capsys):
    # F14 counter-leg: policy = "drop" builds no reclaim window — floor stays 2
    body = SEG_ON + '\n[verify]\nenabled = true\npolicy = "drop"\nllm = "judge"'
    cfg = env.load(config_text=_cw_config(3200, max_out=1024),
                   project_text=env.project(body=body))
    assert isinstance(cfg, ResolvedConfig)
    assert "w_min = 2" in capsys.readouterr().err           # the WARN leg instead


def test_min_window_guard_silent_without_budget_or_with_room(env, capsys):
    env.load(project_text=env.project(body=SEG_ON))          # budget off
    assert "worst-case guaranteed packing size" not in capsys.readouterr().err
    env.load(config_text=_cw_config(131072),                 # w_min 214 ≫ floor
             project_text=env.project(body=SEG_ON))
    assert "worst-case guaranteed packing size" not in capsys.readouterr().err


def test_stitch_card_pool_worst_case_warns(env, capsys):
    # spec 3.16.5 上下文预算 row: stitch enabled ∧ its profile budgeted →
    # worst est = head 325 + context 0 + (max_open+1)=5 cards × 2 digests ×
    # est("好"×400)=400 → 4325 > ib 2316 (cw 3712 / max_out 1024) → WARN
    # (never an error, never an auto-shrunk max_open).
    cfg = env.load(config_text=_cw_config(3712, max_out=1024),
                   project_text=env.project(body=STITCH_ON))
    assert isinstance(cfg, ResolvedConfig)
    assert cfg.stitch.max_open == 4                          # untouched
    err = capsys.readouterr().err
    assert ("[stitch].max_open: worst-case stitch card-pool estimate 4325 tokens > "
            "the input budget of 2316 tokens") in err
    assert "max_open is never auto-shrunk" in err


def test_stitch_card_pool_within_budget_or_undeclared_stays_silent(env, capsys):
    # smaller digest cap fits: 325 + 5 × 2 × 100 = 1325 ≤ 2316 → silent
    body = STITCH_ON + "digest_max_chars = 100\n"
    env.load(config_text=_cw_config(3712, max_out=1024),
             project_text=env.project(body=body))
    assert "stitch card-pool" not in capsys.readouterr().err
    # undeclared stitch profile → the check never runs (budget off)
    env.load(project_text=env.project(body=STITCH_ON))
    assert "stitch card-pool" not in capsys.readouterr().err


def test_static_precheck_error_takes_max_over_class_annotate_views(env):
    # V13③ + per-class overrides: the GLOBAL annotate instruction is tiny
    # (est 4) but [class.qa.annotate] swaps in a 300-token one — the static
    # sum must take the max over views: 32 (head) + 87 (schema) + 300 = 419
    # ≥ ib 281 (cw 4864) → CONFIG_ERROR naming [annotate].
    body = CLASSIFY_BODY + f'\n[class.qa.annotate]\ninstruction = "{"标" * 300}"\n'
    errors = env.errors(config_text=_cw_config(4864),
                        project_text=env.project(body=body))
    has(errors, "[annotate]: static system-side prompt parts estimated at 419 tokens >= the input budget of 281 tokens")


def test_static_precheck_error_takes_max_over_class_verify_views(env):
    # Same mechanism on verify's per-class extra_criteria (its profile is
    # [llm.judge] — declared here via string splice): 192 (head) +
    # max(0, 300) + 4 (annotate instruction rides verify prompts) = 496 ≥ 281.
    config = _cw_config(4864).replace(
        'api_key_env = "LK_TEST_KEY_JUDGE"',
        'api_key_env = "LK_TEST_KEY_JUDGE"\ncontext_window = 4864', 1)
    body = (CLASSIFY_BODY + '\n[verify]\nenabled = true\nllm = "judge"\n'
            + f'\n[class.qa.verify]\nextra_criteria = "{"标" * 300}"\n')
    errors = env.errors(config_text=config, project_text=env.project(body=body))
    has(errors, "[verify]: static system-side prompt parts estimated at 496 tokens >= the input budget of 281 tokens")


def test_static_precheck_class_views_within_budget_stay_silent(env, capsys):
    # Counter-leg: a modest per-class instruction (est 100 → annotate static
    # 219, under 50% of ib 854 @ cw 5500) neither errors nor warns.
    body = CLASSIFY_BODY + f'\n[class.qa.annotate]\ninstruction = "{"标" * 100}"\n'
    cfg = env.load(config_text=_cw_config(5500),
                   project_text=env.project(body=body))
    assert isinstance(cfg, ResolvedConfig)
    assert "[annotate]: static system-side prompt parts estimated" not in capsys.readouterr().err


# ── v1.12: [frame.*] 帧级分类与标注（SPEC-frame-annotation §3.1 七条约束） ────


FRAME_SCHEMA = json.dumps({
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "entities": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["intent", "entities"],
    "additionalProperties": False,
}, ensure_ascii=False)

FRAME_CLASSIFY_ONLY = """\
[frame.classify]
enabled = true
fallback_class = "other"

[[frame.classify.classes]]
name = "task_request"
description = "发起一项新任务的请求"

[[frame.classify.classes]]
name = "chitchat"
description = "与任务无关的闲聊"

[[frame.classify.classes]]
name = "other"
description = "其余帧"
"""

FRAME_ANNOTATE_ONLY = f"""\
[frame.annotate]
enabled = true
instruction = "标注帧意图"
schema_inline = '''
{FRAME_SCHEMA}
'''
"""


def test_frame_sections_default_when_absent(env):
    cfg = env.load()
    assert cfg.frame_classify.enabled is False
    assert cfg.frame_classify.llm == "default"
    assert cfg.frame_classify.fallback_class == ""
    assert cfg.frame_classify.classes == ()
    assert cfg.frame_classify.vision_resolved is False
    assert cfg.frame_annotate.enabled is False
    assert cfg.frame_annotate.llm == "default"
    assert cfg.frame_annotate.instruction == ""
    assert cfg.frame_annotate.examples == ()
    assert cfg.frame_annotate.schema_path is None
    assert cfg.frame_annotate.schema_inline is None
    assert cfg.frame_class_views == {}
    assert cfg.frame_schema is None


def test_frame_happy_path_materializes_views_and_schema(env):
    body = (SEG_ON + "\n" + FRAME_CLASSIFY_ONLY + "\n" + FRAME_ANNOTATE_ONLY
            + 'examples = [{input = "订火车票", '
              'output = {intent = "book_train", entities = ["上海", "明天"]}}]\n'
            + """
[frame.class.task_request.annotate]
instruction = "标注任务请求帧的意图与实体。"

[frame.class.chitchat.annotate]
enabled = false
""")
    cfg = env.load(project_text=env.project(body=body))
    assert [c.name for c in cfg.frame_classify.classes] == [
        "task_request", "chitchat", "other"]
    assert cfg.frame_classify.fallback_class == "other"
    assert cfg.frame_annotate.instruction == "标注帧意图"
    assert cfg.frame_annotate.examples[0].output == {
        "intent": "book_train", "entities": ["上海", "明天"]}   # 干跑通过
    assert cfg.frame_schema == json.loads(FRAME_SCHEMA)
    # 每个已声明帧类各得一份视图（零覆盖类含在内，class_views 同款）
    assert set(cfg.frame_class_views) == {"task_request", "chitchat", "other"}
    t = cfg.frame_class_views["task_request"]
    assert t.instruction == "标注任务请求帧的意图与实体。"
    assert t.enabled is True
    assert t.examples == cfg.frame_annotate.examples   # 未覆盖键继承全局
    c = cfg.frame_class_views["chitchat"]
    assert c.instruction == "标注帧意图"                # 继承全局指令
    assert c.enabled is False                           # 该类成员跳过帧标注
    o = cfg.frame_class_views["other"]
    assert o.instruction == "标注帧意图" and o.enabled is True
    # 全局节不被类覆盖污染
    assert cfg.frame_annotate.instruction == "标注帧意图"


def test_frame_switches_require_segment_outside_sequence_generation(env):
    # frame classify 始终依赖 segment；frame annotate 的 sequence 例外另由 generation 测试覆盖。
    errors = env.errors(project_text=env.project(body=FRAME_CLASSIFY_ONLY))
    has(errors, "[frame.classify].enabled: frame.classify.enabled = true requires segment.enabled = true")
    errors = env.errors(project_text=env.project(body=FRAME_ANNOTATE_ONLY))
    has(errors, "[frame.annotate].enabled: frame.annotate.enabled = true requires segment.enabled = true")


def test_frame_class_requires_frame_classify(env):
    body = SEG_ON + '\n[frame.class.task_request.annotate]\ninstruction = "x"\n'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[frame.class.task_request]: the presence of [frame.class.*] requires frame.classify.enabled = true")


def test_frame_class_unknown_name_rejected(env):
    body = (SEG_ON + "\n" + FRAME_CLASSIFY_ONLY
            + '\n[frame.class.ghost.annotate]\ninstruction = "x"\n')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[frame.class.ghost]: class name "ghost" is not in [[frame.classify.classes]], '
                "available: task_request, chitchat, other")


def test_frame_class_whitelist_enforced(env):
    body = (SEG_ON + "\n" + FRAME_CLASSIFY_ONLY + """
[frame.class.task_request.quality]
threshold = 0.5

[frame.class.task_request.annotate]
llm = "judge"
instruction = "任务请求帧标注指令。"
""")
    errors = env.errors(project_text=env.project(body=body))
    # v1.18：节白名单包含 sequence 帧生成契约；具体合法组合由 generation 配置测试覆盖。
    has(errors, "[frame.class.task_request.quality]: section is not in the [frame.class.*] "
                "override whitelist (available: annotate, generate)")
    has(errors, "[frame.class.task_request.annotate].llm: [frame.class.*.annotate] cannot "
                "override this key (whitelist: instruction, examples, enabled)")
    # 白名单内键不误伤
    assert not any(".instruction" in e for e in errors)


def test_frame_class_override_tables_must_be_tables(env):
    body = SEG_ON + "\n" + FRAME_CLASSIFY_ONLY + "\n[frame.class]\ntask_request = 3\n"
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[frame.class.task_request]: expected table, got 3")
    body = (SEG_ON + "\n" + FRAME_CLASSIFY_ONLY
            + "\n[frame.class.task_request]\nannotate = 3\n")
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[frame.class.task_request.annotate]: expected table, got 3")


def test_frame_class_examples_skip_dryrun_without_a_frame_schema(env):
    # 帧分类开、帧标注关：没有帧 Schema 就没有校验器，类示例照常解析但不干跑
    # （干跑是 Schema 侧的事，无 Schema 时静默跳过而不是报错）。
    body = (SEG_ON + "\n" + FRAME_CLASSIFY_ONLY + """
[frame.class.task_request.annotate]
examples = [{input = "订票", output = {anything = 1}}]
""")
    cfg = env.load(project_text=env.project(body=body))
    view = cfg.frame_class_views["task_request"]
    assert view.examples[0].output == {"anything": 1}
    assert cfg.frame_schema is None


def test_frame_schema_exactly_one_source(env):
    body = SEG_ON + '\n[frame.annotate]\nenabled = true\ninstruction = "标"\n'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[frame.annotate].schema_path: exactly one of schema_path or schema_inline "
                "must be provided, got neither")
    body = SEG_ON + "\n" + FRAME_ANNOTATE_ONLY + 'schema_path = "x.json"\n'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[frame.annotate].schema_inline: exactly one of schema_path / schema_inline "
                "must be provided (mutually exclusive), got both set")


def test_frame_schema_meta_validation_branches(env):
    prefix = SEG_ON + '\n[frame.annotate]\nenabled = true\ninstruction = "标"\n'
    errors = env.errors(project_text=env.project(
        body=prefix + "schema_inline = '{bad'\n"))
    has(errors, "[frame.annotate].schema_inline: expected valid JSON")
    errors = env.errors(project_text=env.project(
        body=prefix + "schema_inline = '[1, 2]'\n"))
    has(errors, "[frame.annotate].schema_inline: frame schema must be a JSON object at the top level")
    errors = env.errors(project_text=env.project(
        body=prefix + 'schema_inline = \'{"type": "object", "properties": 3}\'\n'))
    has(errors, "[frame.annotate].schema_inline: failed JSON Schema draft 2020-12 meta-schema validation")
    errors = env.errors(project_text=env.project(
        body=prefix + 'schema_inline = \'{"type": "array"}\'\n'))
    has(errors, '[frame.annotate].schema_inline: frame schema top-level type must be "object"')


def test_frame_schema_path_variant_and_unreadable(env):
    schema_file = env.tmp / "frame_schema.json"
    schema_file.write_text(FRAME_SCHEMA, encoding="utf-8")
    prefix = SEG_ON + '\n[frame.annotate]\nenabled = true\ninstruction = "标"\n'
    cfg = env.load(project_text=env.project(
        body=prefix + f'schema_path = "{schema_file}"\n'))
    assert cfg.frame_schema == json.loads(FRAME_SCHEMA)
    assert cfg.frame_annotate.schema_path == str(schema_file)
    errors = env.errors(project_text=env.project(
        body=prefix + 'schema_path = "ghost/frame.json"\n'))
    has(errors, "[frame.annotate].schema_path: cannot read schema file")


def test_frame_schema_dangling_ref_is_config_error(env):
    bad = json.dumps({"type": "object",
                      "properties": {"x": {"$ref": "#/$defs/ghost"}}})
    body = (SEG_ON + '\n[frame.annotate]\nenabled = true\ninstruction = "标"\n'
            + f"schema_inline = '''\n{bad}\n'''\n")
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[frame.annotate].schema_inline: frame schema has an unresolvable reference")


def test_frame_examples_dryrun_against_frame_schema(env):
    body = (SEG_ON + "\n" + FRAME_ANNOTATE_ONLY
            + 'examples = [{input = "订票", '
              'output = {intent = "book", entities = 3}}]\n')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[[frame.annotate.examples]][1].output: failed frame schema validation")


def test_frame_class_examples_dryrun_against_frame_schema(env):
    body = (SEG_ON + "\n" + FRAME_CLASSIFY_ONLY + "\n" + FRAME_ANNOTATE_ONLY + """
[frame.class.task_request.annotate]
examples = [{input = "订票", output = {intent = "book", entities = "上海"}}]
""")
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[[frame.class.task_request.annotate.examples]][1].output: failed frame schema validation")


def test_frame_meta_mode_guard(env):
    body = (SEG_ON + "\n" + FRAME_CLASSIFY_ONLY + f"""
[output]
meta_mode = "none"
schema_inline = '''
{SCHEMA}
'''
""")
    errors = env.errors(project_text=env.project(body=body, include_output=False))
    has(errors, '[output].meta_mode: must not be "none" when frame granularity (frame.classify '
                "/ frame.annotate) is enabled")
    # sidecar 合法
    cfg = env.load(project_text=env.project(
        body=body.replace('meta_mode = "none"', 'meta_mode = "sidecar"'),
        include_output=False))
    assert cfg.output.meta_mode == "sidecar"


def test_frame_fallback_required_and_member(env):
    body = SEG_ON + "\n" + FRAME_CLASSIFY_ONLY.replace('fallback_class = "other"\n', "")
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[frame.classify].fallback_class: required when frame.classify.enabled = true, "
                "expected a class name from [[frame.classify.classes]]")
    body = SEG_ON + "\n" + FRAME_CLASSIFY_ONLY.replace('fallback_class = "other"',
                                                       'fallback_class = "ghost"')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[frame.classify].fallback_class: referenced class name "ghost" is not in '
                "[[frame.classify.classes]], available: task_request, chitchat, other")


def test_frame_fallback_with_empty_class_table_rejected(env):
    # fallback ∈ 帧类表 传递性地要求类表非空（v1.12 无独立 ≥N 类数规则）
    body = SEG_ON + '\n[frame.classify]\nenabled = true\nfallback_class = "x"\n'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[frame.classify].fallback_class: referenced class name "x" is not in '
                "[[frame.classify.classes]], available: (none)")


def test_frame_classes_name_pattern_uniqueness_description(env):
    body = SEG_ON + """
[frame.classify]
enabled = true
fallback_class = "qa"

[[frame.classify.classes]]
name = "Q-A"
description = "坏名字"

[[frame.classify.classes]]
name = "qa"
description = "问答"

[[frame.classify.classes]]
name = "qa"
description = "重复"

[[frame.classify.classes]]
name = "empty_desc"
"""
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[[frame.classify.classes]][1].name: expected a match of [a-z0-9_]+, got "Q-A"')
    has(errors, '[[frame.classify.classes]][3].name: name must be unique within the table, got duplicate "qa"')
    has(errors, "[[frame.classify.classes]][4].description: missing required key")


def test_frame_directed_probes(env, capsys):
    # 约束·定向探针（v1.11 use_vision 原始节探针同款）：两键显式书写 ⇒ 定向报错
    body = SEG_ON + "\n" + FRAME_CLASSIFY_ONLY.replace(
        "enabled = true", 'enabled = true\nassignment = "single"')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[frame.classify].assignment: frame classification does not provide assignment")
    has(errors, "[classify].assignment")            # 指引指向序列级
    body = SEG_ON + "\n" + FRAME_ANNOTATE_ONLY + "self_consistency = 3\n"
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[frame.annotate].self_consistency: frame annotation does not provide self_consistency")
    has(errors, "[annotate].self_consistency")      # 指引指向序列级
    # 永不经未知键前向兼容 WARN 双重上报
    err = capsys.readouterr().err
    assert "[frame.classify].assignment: unknown key" not in err
    assert "[frame.annotate].self_consistency: unknown key" not in err


def test_frame_class_examples_ignored_warns(env, capsys):
    # 帧级批量判决模板不渲染类别示例（§10.12，与序列级 §10.8 有意不同）——
    # 显名 WARN 而非静默无效；且静态预检口径同步不计 examples。
    body = SEG_ON + "\n" + FRAME_CLASSIFY_ONLY.replace(
        'description = "发起一项新任务的请求"',
        'description = "发起一项新任务的请求"\nexamples = ["帮我订一张明天的高铁票"]')
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.frame_classify.classes[0].examples == ("帮我订一张明天的高铁票",)
    err = capsys.readouterr().err
    assert "[frame.classify].classes" in err and "are not rendered" in err


def test_frame_class_no_examples_no_render_warning(env, capsys):
    env.load(project_text=env.project(body=SEG_ON + "\n" + FRAME_CLASSIFY_ONLY))
    assert "are not rendered" not in capsys.readouterr().err


def test_frame_parked_joins_parked_list_when_segment_off(env, capsys):
    # 约束·no-op：[frame.*] 在场 ∧ 均未启用 ∧ segment off ⇒ R8 停放清单
    cfg = env.load(project_text=env.project(body='[frame.classify]\nllm = "judge"\n'))
    assert cfg.frame_classify.enabled is False
    err = capsys.readouterr().err
    assert "[segment].enabled" in err and "[frame]" in err
    assert "no effect" in err


def test_frame_parked_no_warn_when_segment_on(env, capsys):
    body = SEG_ON + '\n[frame.classify]\nllm = "judge"\n'
    env.load(project_text=env.project(body=body))
    assert "[frame]" not in capsys.readouterr().err


def test_frame_enabled_never_joins_parked_list(env, capsys):
    # 任一帧开关启用 + segment off ⇒ CONFIG_ERROR 接管，不再入停放清单
    with pytest.raises(ConfigError):
        env.load(project_text=env.project(body=FRAME_CLASSIFY_ONLY))
    assert "[frame]" not in capsys.readouterr().err


def test_frame_annotate_llm_needs_vision_on_ui(env):
    # vision 语义分列：frame.annotate.llm 在 ui ∧ enabled 时无条件入 vision 必需集
    body = (SEG_ON + 'strategy = "rules"\n\n' + FRAME_ANNOTATE_ONLY
            + 'llm = "novision"\n')
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    errors = env.errors(config_text=BASE_CONFIG + NOVISION_PROFILE,
                        project_text=project)
    has(errors, "[llm.novision].supports_vision: a profile referenced by the frame.annotate stage(s) in UI modality")


def test_frame_classify_llm_never_needs_vision_and_vision_resolved_derives(env):
    # vision 语义分列：frame.classify.llm 永不入 vision 必需集——附图与否由
    # vision_resolved 解析产物自适应（ui ∧ enabled ∧ supports_vision）
    body = (SEG_ON + 'strategy = "rules"\n\n'
            + FRAME_CLASSIFY_ONLY.replace("enabled = true",
                                          'enabled = true\nllm = "novision"'))
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    cfg = env.load(config_text=BASE_CONFIG + NOVISION_PROFILE, project_text=project)
    assert cfg.frame_classify.llm == "novision"
    assert cfg.frame_classify.vision_resolved is False   # capability off → 纯文本判决
    body = (SEG_ON + 'strategy = "rules"\n\n'
            + FRAME_CLASSIFY_ONLY.replace("enabled = true",
                                          'enabled = true\nllm = "judge"'))
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    cfg = env.load(project_text=project)
    assert cfg.frame_classify.vision_resolved is True    # ui ∧ enabled ∧ 有视觉
    # 文本模态：即便 profile 有视觉能力也恒 False
    cfg = env.load(project_text=env.project(body=SEG_ON + "\n" + FRAME_CLASSIFY_ONLY))
    assert cfg.frame_classify.vision_resolved is False


def test_frame_vision_resolved_false_while_disabled(env):
    body = SEG_ON + 'strategy = "rules"\n\n[frame.classify]\nllm = "judge"'
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    cfg = env.load(project_text=project)
    assert cfg.frame_classify.vision_resolved is False


def test_frame_llm_existence_and_key_when_enabled(env, monkeypatch):
    body = SEG_ON + "\n" + FRAME_CLASSIFY_ONLY.replace(
        "enabled = true", 'enabled = true\nllm = "ghost"')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[frame.classify].llm: referenced profile "ghost" does not exist in config.toml [llm.*]')
    monkeypatch.delenv("LK_TEST_KEY_JUDGE")
    body = SEG_ON + "\n" + FRAME_CLASSIFY_ONLY.replace(
        "enabled = true", 'enabled = true\nllm = "judge"')
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.frame_classify.llm == "judge"
    body = SEG_ON + "\n" + FRAME_ANNOTATE_ONLY + 'llm = "judge"\n'
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.frame_annotate.llm == "judge"


def test_frame_llm_not_referenced_when_disabled(env, monkeypatch):
    monkeypatch.delenv("LK_TEST_KEY_JUDGE")
    body = (SEG_ON + '\n[frame.classify]\nllm = "judge"\n\n'
            '[frame.annotate]\nllm = "judge"\n')
    cfg = env.load(project_text=env.project(body=body))
    assert not hasattr(cfg.llm_profiles["judge"], "api_keys")


def test_frame_annotate_instruction_required_when_enabled(env):
    body = (SEG_ON + "\n[frame.annotate]\nenabled = true\n"
            + f"schema_inline = '''\n{FRAME_SCHEMA}\n'''\n")
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[frame.annotate].instruction: required when frame.annotate.enabled = true, "
                "expected a non-empty string")


def test_frame_static_precheck_error_when_nothing_fits(env):
    # V13③ 帧级标注段：头 96 + 帧 Schema + instruction("标"×300) ≥ ib 281 (cw 4864)
    body = (SEG_ON + "\n"
            + FRAME_ANNOTATE_ONLY.replace('instruction = "标注帧意图"',
                                          f'instruction = "{"标" * 300}"'))
    errors = env.errors(config_text=_cw_config(4864),
                        project_text=env.project(body=body))
    has(errors, "[frame.annotate]: static system-side prompt parts estimated")
    has(errors, "no record can fit")


def test_frame_classify_static_precheck_counts_class_table(env):
    # V13③ 帧级分类段：头 96 + 帧类表（300 CJK 描述）≥ ib 281 (cw 4864)
    body = SEG_ON + "\n" + FRAME_CLASSIFY_ONLY.replace(
        'description = "其余帧"', f'description = "{"类" * 300}"')
    errors = env.errors(config_text=_cw_config(4864),
                        project_text=env.project(body=body))
    has(errors, "[frame.classify]: static system-side prompt parts estimated")


def test_frame_static_precheck_silent_with_room(env, capsys):
    body = SEG_ON + "\n" + FRAME_CLASSIFY_ONLY + "\n" + FRAME_ANNOTATE_ONLY
    cfg = env.load(config_text=_cw_config(131072),
                   project_text=env.project(body=body))
    assert isinstance(cfg, ResolvedConfig)
    err = capsys.readouterr().err
    assert "[frame.classify]: static system-side prompt parts estimated" not in err
    assert "[frame.annotate]: static system-side prompt parts estimated" not in err

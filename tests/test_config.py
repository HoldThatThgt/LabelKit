"""Offline unit tests for M1 (labelkit/config/loader.py).

Pure-logic coverage only: TOML parsing, three-source merge precedence, default
filling per spec ch.5 tables, every §6.3 validation rule, error aggregation and
message format, packaged default rubrics. M1 performs no LLM calls, so this
module has no integration counterpart.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from labelkit.config import ResolvedConfig, default_rubric, load
from labelkit.config.model import CliOverrides
from labelkit.errors import ConfigError

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
    # resolution duties
    assert cfg.quality.rubric == "default:text"        # auto by modality
    assert cfg.rubric.name == "default-text-v1"
    assert cfg.llm_profiles["default"].api_key == "sk-default"   # referenced
    assert cfg.llm_profiles["judge"].api_key == ""               # unreferenced
    assert cfg.run.input == str(env.input_file)
    assert cfg.run.output == str(env.output)
    assert cfg.trace.path == str(env.out_dir / "result.trace.jsonl")
    assert cfg.limit is None and cfg.strict is False and cfg.dry_run is False
    assert cfg.user_schema["type"] == "object"


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
    assert cfg.trace.path == "custom.trace.jsonl"
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
    has(errors, "config.toml:schema_version: 期望 1，得到 2")
    has(errors, "project.toml:schema_version: 缺失必填键，期望 1")


def test_type_mismatch_message_format(env):
    bad = BASE_CONFIG.replace('api_key_env = "LK_TEST_KEY_DEFAULT"',
                              'api_key_env = "LK_TEST_KEY_DEFAULT"\ntimeout_s = "abc"')
    errors = env.errors(config_text=bad)
    has(errors, '[llm.default].timeout_s: 期望正整数，得到 "abc"')


def test_missing_required_profile_key(env):
    bad = BASE_CONFIG.replace('model = "main-model"\n', "")
    errors = env.errors(config_text=bad)
    has(errors, "[llm.default].model: 缺失必填键")


def test_unknown_keys_warn_not_error(env, capsys):
    cfg_text = BASE_CONFIG.replace("[tool]", "[tool]\nfancy_new_key = 1")
    project = env.project(run_extra="future_flag = true")
    cfg = env.load(config_text=cfg_text, project_text=project)
    assert isinstance(cfg, ResolvedConfig)
    err_out = capsys.readouterr().err
    assert "warning:" in err_out
    assert "[tool].fancy_new_key: 未知键" in err_out
    assert "[run].future_flag: 未知键" in err_out


def test_config_file_missing(env):
    with pytest.raises(ConfigError) as ei:
        load(env.tmp / "nope.toml", env.tmp / "also_nope.toml", CliOverrides())
    joined = "\n".join(ei.value.errors)
    assert "无法读取配置文件" in joined


def test_toml_parse_failure(env):
    errors = env.errors(config_text="schema_version = [oops")
    has(errors, "TOML 解析失败")


def test_no_llm_profile(env):
    errors = env.errors(config_text='schema_version = 1\n[tool]\nlog_level = "info"\n')
    has(errors, "至少需要 1 个 [llm.<name>] profile")


# ── rules 2–5: profile references ──────────────────────────────────────────


def test_unknown_profile_reference_lists_available(env):
    errors = env.errors(project_text=env.project(body='[quality]\nllm = "fast"'))
    has(errors, '[quality].llm: 引用的 profile "fast" 不存在于 config.toml [llm.*]，'
                "可用：default、judge")


def test_generate_llms_checked_per_element(env):
    body = '[generate]\nllms = ["default", "ghost"]'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[generate].llms[2]: 引用的 profile "ghost" 不存在')


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
    has(errors, '[verify].llm: 引用的 profile "ghost" 不存在')


def test_repair_llm_checked_when_set(env):
    body = f"[output]\nrepair_llm = \"ghost\"\nschema_inline = '''\n{SCHEMA}\n'''"
    errors = env.errors(project_text=env.project(include_output=False, body=body))
    has(errors, '[output].repair_llm: 引用的 profile "ghost" 不存在')


def test_judges_must_be_odd(env):
    errors = env.errors(project_text=env.project(
        body='[quality]\njudges = ["default", "judge"]'))
    has(errors, "[quality].judges: 非空时长度须为奇数，得到 2 个")


def test_verify_judges_odd_and_existing(env):
    body = '[verify]\nenabled = true\njudges = ["judge", "ghost"]'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[verify].judges[2]: 引用的 profile "ghost" 不存在')
    has(errors, "[verify].judges: 非空时长度须为奇数")


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


def test_semantic_dedup_requires_embedding_name(env):
    errors = env.errors(project_text=env.project(body="[dedup]\nsemantic = true"))
    has(errors, "[dedup].semantic_embedding: dedup.semantic = true 时必填")


def test_semantic_dedup_unknown_embedding(env):
    body = '[dedup]\nsemantic = true\nsemantic_embedding = "ghost"'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '不存在于 config.toml [embedding.*]，可用：emb')


def test_semantic_dedup_ok_resolves_embedding_key(env):
    body = '[dedup]\nsemantic = true\nsemantic_embedding = "emb"'
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.embedding_profiles["emb"].api_key == "sk-emb"


# ── rules 6–9: cross-field constraints ─────────────────────────────────────


def test_top_ratio_required_when_selected(env):
    errors = env.errors(project_text=env.project(
        body='[quality]\nselection = "top_ratio"'))
    has(errors, '[quality].top_ratio: selection = "top_ratio" 时必填')


def test_top_ratio_threshold_mutually_exclusive(env):
    body = '[quality]\nselection = "top_ratio"\ntop_ratio = 0.5\nthreshold = 0.3'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[quality].threshold: 与 quality.top_ratio 互斥")


def test_top_ratio_range(env):
    body = '[quality]\nselection = "top_ratio"\ntop_ratio = 1.5'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[quality].top_ratio: 期望(0,1] 内的数值，得到 1.5")
    # the "required" branch of rule 6 must not fire when the key was provided (just invalid)
    assert not any("时必填" in e for e in errors)


@pytest.mark.parametrize("value", [2, 4, 1])
def test_self_consistency_rejects_bad_values(env, value):
    errors = env.errors(project_text=env.project(
        annotate_body=f'instruction = "标注"\nself_consistency = {value}'))
    has(errors, f"[annotate].self_consistency: 期望 0 或 ≥3 的奇数，得到 {value}")


@pytest.mark.parametrize("value", [0, 3, 5])
def test_self_consistency_accepts_valid(env, value):
    cfg = env.load(project_text=env.project(
        annotate_body=f'instruction = "标注"\nself_consistency = {value}'))
    assert cfg.annotate.self_consistency == value


def test_weighted_mixture_requires_weights(env):
    body = ('[generate]\nenabled = true\ninstruction = "生成"\n'
            'llms = ["default", "judge"]\nmixture = "weighted"')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[generate].weights: mixture = "weighted" 时必填')


def test_weighted_mixture_length_and_positivity(env):
    body = ('[generate]\nenabled = true\ninstruction = "生成"\n'
            'llms = ["default", "judge"]\nmixture = "weighted"\nweights = [1.0]')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[generate].weights: 期望长度 2（= generate.llms），得到长度 1")

    body = ('[generate]\nenabled = true\ninstruction = "生成"\n'
            'llms = ["default", "judge"]\nmixture = "weighted"\nweights = [1.0, -0.5]')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[generate].weights[2]: 期望正数，得到 -0.5")


def test_styles_unique_names_and_nonempty_prompts(env):
    body = ('[generate]\nenabled = true\ninstruction = "生成"\n'
            '[[generate.styles]]\nname = "formal"\nprompt = "正式风格"\n'
            '[[generate.styles]]\nname = "formal"\nprompt = ""')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[[generate.styles]][2].name: 表内 name 须唯一，得到重复的 "formal"')
    has(errors, "[[generate.styles]][2].prompt: 期望非空字符串")


def test_judgment_reasons_values(env):
    errors = env.errors(project_text=env.project(
        body='[quality]\njudgment_reasons = "always"'))
    has(errors, '[quality].judgment_reasons: 期望 "auto" | true | false')
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
    has(errors, '[run].input: run.mode = "generate_only" 时必须缺省')


def test_generate_only_forbids_cli_input(env):
    project = env.project(input_path=None, run_extra='mode = "generate_only"',
                          body=GEN_BODY + "standalone_count = 10")
    with pytest.raises(ConfigError) as ei:
        env.load(project_text=project, cli=CliOverrides(input=str(env.input_file)))
    has(ei.value.errors, 'cli:--input: run.mode = "generate_only" 时不得提供输入路径')


def test_generate_only_requires_text_modality(env):
    project = env.project(input_path=None, modality="ui",
                          run_extra='mode = "generate_only"',
                          body=GEN_BODY + "standalone_count = 10")
    errors = env.errors(project_text=project)
    has(errors, '[run].modality: run.mode = "generate_only" 要求 "text"，得到 "ui"')


def test_generate_only_requires_generate_enabled(env):
    project = env.project(input_path=None, run_extra='mode = "generate_only"')
    errors = env.errors(project_text=project)
    has(errors, '[generate].enabled: run.mode = "generate_only" 要求 generate.enabled = true')


def test_generate_only_seed_forms_mutually_exclusive(env):
    project = env.project(input_path=None, run_extra='mode = "generate_only"',
                          body=GEN_BODY + 'standalone_count = 10\nseed_examples = ["a"]')
    errors = env.errors(project_text=project)
    has(errors, "[generate].seed_examples: 与 standalone_count 互斥")


def test_generate_only_requires_one_seed_form(env):
    project = env.project(input_path=None, run_extra='mode = "generate_only"',
                          body=GEN_BODY)
    errors = env.errors(project_text=project)
    has(errors, "seed_examples（非空字符串数组）或 standalone_count（≥ 1）其一")


def test_generate_only_seed_examples_nonempty_strings(env):
    project = env.project(input_path=None, run_extra='mode = "generate_only"',
                          body=GEN_BODY + 'seed_examples = ["ok", " "]')
    errors = env.errors(project_text=project)
    has(errors, "[generate].seed_examples[2]: 期望非空字符串")

    project = env.project(input_path=None, run_extra='mode = "generate_only"',
                          body=GEN_BODY + "seed_examples = []")
    errors = env.errors(project_text=project)
    has(errors, "[generate].seed_examples: 期望非空字符串数组，得到空数组")


def test_process_mode_forbids_generate_only_keys(env):
    errors = env.errors(project_text=env.project(
        body='[generate]\nseed_examples = ["a"]'))
    has(errors, '[generate].seed_examples: 仅 run.mode = "generate_only" 可设置')

    errors = env.errors(project_text=env.project(
        body="[generate]\nstandalone_count = 5"))
    has(errors, '[generate].standalone_count: 仅 run.mode = "generate_only" 可设置')


# ── rule 12: API keys, referenced profiles only ────────────────────────────


def test_referenced_profile_needs_env_key(env, monkeypatch):
    monkeypatch.delenv("LK_TEST_KEY_DEFAULT")
    errors = env.errors()
    has(errors, '[llm.default].api_key_env: 环境变量 "LK_TEST_KEY_DEFAULT" 未设置或为空')


def test_unreferenced_profile_key_not_required(env, monkeypatch):
    monkeypatch.delenv("LK_TEST_KEY_JUDGE")   # verify disabled → judge unreferenced
    cfg = env.load()
    assert cfg.llm_profiles["judge"].api_key == ""


def test_verify_enabled_makes_judge_referenced(env, monkeypatch):
    monkeypatch.delenv("LK_TEST_KEY_JUDGE")
    errors = env.errors(project_text=env.project(body="[verify]\nenabled = true"))
    has(errors, '环境变量 "LK_TEST_KEY_JUDGE" 未设置或为空')


# ── rules 13–15: user schema + few-shot ────────────────────────────────────


def test_schema_exactly_one_source(env):
    body = f"[output]\nschema_path = \"x.json\"\nschema_inline = '''\n{SCHEMA}\n'''"
    errors = env.errors(project_text=env.project(include_output=False, body=body))
    has(errors, "与 schema_path 恰好提供其一（互斥）")

    errors = env.errors(project_text=env.project(include_output=False))
    has(errors, "须恰好提供 schema_path 或 schema_inline 其一")


def test_schema_path_unreadable(env):
    body = '[output]\nschema_path = "does/not/exist.json"'
    errors = env.errors(project_text=env.project(include_output=False, body=body))
    has(errors, "无法读取 Schema 文件")


def test_schema_invalid_json(env):
    errors = env.errors(project_text=env.project(schema="{not json"))
    has(errors, "[output].schema_inline: 期望合法 JSON")


def test_schema_meta_schema_violation(env):
    bad = json.dumps({"type": "object", "properties": {"a": {"type": 123}}})
    errors = env.errors(project_text=env.project(schema=bad))
    has(errors, "未通过 JSON Schema draft 2020-12 元 Schema 校验")


def test_schema_top_level_must_be_object(env):
    bad = json.dumps({"type": "array", "items": {"type": "string"}})
    errors = env.errors(project_text=env.project(schema=bad))
    has(errors, '用户 Schema 顶层 type 必须为 "object"，得到 "array"')


def test_schema_reserved_meta_key(env):
    bad = json.dumps({"type": "object",
                      "properties": {"intent": {"type": "string"},
                                     "_meta": {"type": "object"}}})
    errors = env.errors(project_text=env.project(schema=bad))
    has(errors, '用户 Schema 顶层不得声明保留键 "_meta"')


def test_few_shot_output_validated_against_schema(env):
    good = ('instruction = "标注"\n'
            'examples = [{input = "你好", output = {intent = "qa", topic = "问候"}}]')
    cfg = env.load(project_text=env.project(annotate_body=good))
    assert cfg.annotate.examples[0].output == {"intent": "qa", "topic": "问候"}

    bad = ('instruction = "标注"\n'
           'examples = [{input = "你好", output = {intent = "nope", topic = "问候"}}]')
    errors = env.errors(project_text=env.project(annotate_body=bad))
    has(errors, "[[annotate.examples]][1].output: 未通过用户 Schema")


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
    has(errors, "[output].schema_inline: 用户 Schema 引用无法解析")


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
    has(errors, "[output].schema_inline: 用户 Schema 引用无法解析")


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
    has(errors, "[output].schema_inline: 用户 Schema 引用无法解析")


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
    has(errors, "[output].schema_inline: 用户 Schema 引用无法解析")


def test_few_shot_requires_input_and_output(env):
    body = 'instruction = "标注"\nexamples = [{input = "你好"}]'
    errors = env.errors(project_text=env.project(annotate_body=body))
    has(errors, "[[annotate.examples]][1].output: 缺失必填键")


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
    has(errors, '[quality].rubric: rubric = "inline" 但未提供 [[rubric.criteria]]')


def test_rubric_key_pattern(env):
    body = INLINE_RUBRIC + """
[[rubric.criteria]]
key = "Topic-Match"
description = "话题是否明确可归类"
pairwise_prompt = "哪条指令的话题更明确？"
"""
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[[rubric.criteria]][2].key: 期望匹配 [a-z0-9_]+，得到 "Topic-Match"')


def test_rubric_duplicate_key(env):
    body = INLINE_RUBRIC + """
[[rubric.criteria]]
key = "intent_clarity"
description = "重复"
pairwise_prompt = "重复？"
"""
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[[rubric.criteria]][2].key: key 须唯一")


def test_rubric_weight_positive(env):
    body = INLINE_RUBRIC.replace("weight = 2.0", "weight = 0.0")
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[[rubric.criteria]][1].weight: 期望正数，得到 0.0")


def test_rubric_criteria_nonempty(env):
    body = '[quality]\nrubric = "inline"\n\n[rubric]\nname = "empty"\ncriteria = []'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[rubric].criteria: criteria 不得为空")


def test_pointwise_requires_six_levels_inline(env):
    body = INLINE_RUBRIC.replace('rubric = "inline"', 'rubric = "inline"\nmode = "pointwise"')
    body += 'pointwise_levels = ["0: 差", "1: 中", "2: 好"]\n'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[[rubric.criteria]][1].pointwise_levels: pointwise 模式要求恰好 6 级（0–5），得到 3 级")


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
    has(errors, "quality 与 annotate 不得同时禁用")


def test_verify_requires_annotate(env):
    errors = env.errors(project_text=env.project(
        annotate_body="enabled = false", body="[verify]\nenabled = true"))
    has(errors, "verify.enabled = true 要求 annotate.enabled = true（2.3.1 约束②）")


def test_generate_requires_text_modality(env):
    project = env.project(input_path=env.input_dir, modality="ui", body=GEN_BODY)
    errors = env.errors(project_text=project)
    has(errors, 'generate.enabled = true 要求 run.modality = "text"')


def test_generate_process_requires_quality(env):
    body = "[quality]\nenabled = false\n\n" + GEN_BODY
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "要求 quality.enabled = true（种子来自质量门，2.3.1 约束③）")


def test_instruction_required_when_enabled(env):
    errors = env.errors(project_text=env.project(annotate_body=""))
    has(errors, "[annotate].instruction: annotate.enabled = true 时必填")

    errors = env.errors(project_text=env.project(body="[generate]\nenabled = true"))
    has(errors, "[generate].instruction: generate.enabled = true 时必填")


# ── rule 21: paths ─────────────────────────────────────────────────────────


def test_input_existence_not_checked_by_m1(env):
    # Input existence is M2's job (Ingestor -> InputError -> exit 3, spec §2.4);
    # M1 must NOT turn a missing input path into a ConfigError (exit 2).
    cfg = env.load(project_text=env.project(input_path=env.tmp / "ghost.jsonl"))
    assert cfg.run.input == str(env.tmp / "ghost.jsonl")


def test_input_required_in_process_mode(env):
    errors = env.errors(project_text=env.project(input_path=None))
    has(errors, "[run].input: process 模式必填（可用 CLI --input 提供）")


def test_output_not_inside_input_dir(env):
    project = env.project(input_path=env.input_dir, modality="ui",
                          output_path=env.input_dir / "o.jsonl")
    errors = env.errors(project_text=project)
    has(errors, "[run].output: 不得位于输入目录内部（防止自吞）")


def test_output_must_not_equal_input_file(env):
    errors = env.errors(project_text=env.project(output_path=env.input_file))
    has(errors, "[run].output: 不得与输入文件相同")


def test_output_parent_must_exist(env):
    errors = env.errors(project_text=env.project(
        output_path=env.tmp / "no_dir" / "o.jsonl"))
    has(errors, "[run].output: 输出父目录不存在或不可写")


def test_output_required(env):
    errors = env.errors(project_text=env.project(output_path=None))
    has(errors, "[run].output: 缺失必填键")


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
    assert '引用的 profile "fast" 不存在于 config.toml [llm.*]，可用：default、judge' in errors[0]
    assert '不得声明保留键 "_meta"' in errors[1]
    assert errors[2] == f'{fp}:[[rubric.criteria]][2].key: 期望匹配 [a-z0-9_]+，得到 "Topic-Match"'


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
    assert "自增强偏差" in err_out


def test_ignored_inline_rubric_warns(env, capsys):
    body = "[rubric]\nname = 'unused'\n[[rubric.criteria]]\nkey = 'x'\ndescription = 'd'\npairwise_prompt = 'p'"
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.rubric.name == "default-text-v1"
    assert "内联 rubric 未生效" in capsys.readouterr().err


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
    assert "top_ratio" in err and "不会生效" in err


def test_top_ratio_with_selection_does_not_warn(env, capsys):
    cfg = env.load(project_text=env.project(
        body='[quality]\nselection = "top_ratio"\ntop_ratio = 0.5'))
    assert cfg.quality.selection == "top_ratio"
    assert "不会生效" not in capsys.readouterr().err


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
    assert "warning:" in err and "评审团不生效" in err


def test_pointwise_judges_key_checked_on_quality_llm(env, tmp_path, monkeypatch):
    # Review finding: in pointwise mode the runtime uses quality.llm, so rule 12
    # must demand ITS key even when a judges panel is configured.
    monkeypatch.delenv("LK_TEST_KEY_DEFAULT", raising=False)
    errors = env.errors(project_text=env.project(
        body='[quality]\nmode = "pointwise"\njudges = ["judge", "judge", "judge"]'))
    has(errors, "[llm.default].api_key_env")

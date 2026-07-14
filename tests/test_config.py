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


# ── v1.5 plan A: validation hooks (rule 17) ─────────────────────────────────

def _output_with(extra: str) -> str:
    return f"[output]\n{extra}\nschema_inline = \'\'\'\n{SCHEMA}\n\'\'\'"


def test_output_validator_loads_and_dryruns_examples(env):
    cfg = env.load(project_text=env.project(
        annotate_body=('instruction = "标注意图"\n'
                       'examples = [{input = "问路", '
                       'output = {intent = "qa", topic = "问路"}}]'),
        body=_output_with('validator = "tests.hook_samples:topic_max6"'),
        include_output=False,
    ))
    assert cfg.output.validator == "tests.hook_samples:topic_max6"


def test_output_validator_bad_ref_is_config_error(env):
    errors = env.errors(project_text=env.project(
        body=_output_with('validator = "no_such_module_xyz:fn"'),
        include_output=False,
    ))
    has(errors, "[output].validator")
    has(errors, "无法导入模块")


def test_output_validator_rejecting_fewshot_is_config_error(env):
    errors = env.errors(project_text=env.project(
        annotate_body=('instruction = "标注意图"\n'
                       'examples = [{input = "问", '
                       'output = {intent = "qa", topic = "这是一个特别长的主题短语"}}]'),
        body=_output_with('validator = "tests.hook_samples:topic_max6"'),
        include_output=False,
    ))
    has(errors, "未通过 output.validator 回调")


def test_sample_validator_checked_when_generate_enabled(env):
    errors = env.errors(project_text=env.project(
        body=('[quality]\nthreshold = 0.5\n\n'
              '[generate]\nenabled = true\ninstruction = "生成"\n'
              'sample_validator = "tests.hook_samples:NOT_CALLABLE"'),
    ))
    has(errors, "[generate].sample_validator")
    has(errors, "不是可调用对象")


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
    has(errors, "[classify].classes: classify.enabled = true 时须声明 ≥ 2 个类别")


def test_classify_fallback_required_and_member(env):
    body = CLASSIFY_BODY.replace('fallback_class = "other"\n', "")
    errors = env.errors(project_text=env.project(body=body))
    has(errors, "[classify].fallback_class: classify.enabled = true 时必填")

    body = CLASSIFY_BODY.replace('fallback_class = "other"', 'fallback_class = "ghost"')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[classify].fallback_class: 引用的类名 "ghost" 不在 [[classify.classes]] 中，'
                "可用：writing、qa、other")


def test_classify_assignment_and_on_error_enums(env):
    body = CLASSIFY_BODY.replace("enabled = true", 'enabled = true\nassignment = "both"')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[classify].assignment: 期望 "single" | "multi"，得到 "both"')

    body = CLASSIFY_BODY.replace("enabled = true", 'enabled = true\non_error = "skip"')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[classify].on_error: 期望 "fallback" | "fail"，得到 "skip"')


def test_classify_max_labels_multi_only(env):
    body = CLASSIFY_BODY.replace("enabled = true", "enabled = true\nmax_labels = 2")
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[classify].max_labels: 仅 assignment = "multi" 时可设置')


@pytest.mark.parametrize("value", [1, 4])
def test_classify_max_labels_range(env, value):
    body = CLASSIFY_BODY.replace(
        "enabled = true", f'enabled = true\nassignment = "multi"\nmax_labels = {value}')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, f"[classify].max_labels: 期望 [2, 3] 内的整数（上界 = 类别数），得到 {value}")


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
    has(errors, f"[classify].self_consistency: 期望 0 或 ≥3 的奇数，得到 {value}")


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
    has(errors, '[[classify.classes]][1].name: 期望匹配 [a-z0-9_]+，得到 "Q-A"')
    has(errors, '[[classify.classes]][3].name: 表内 name 须唯一，得到重复的 "qa"')
    has(errors, "[[classify.classes]][4].description: 缺失必填键")


def test_classify_llm_profile_checked_when_enabled(env):
    body = CLASSIFY_BODY.replace("enabled = true", 'enabled = true\nllm = "ghost"')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[classify].llm: 引用的 profile "ghost" 不存在于 config.toml [llm.*]')


def test_classify_llm_not_checked_when_disabled(env):
    cfg = env.load(project_text=env.project(body='[classify]\nllm = "ghost"'))
    assert cfg.classify.llm == "ghost"            # inert reference, like verify.llm


def test_classify_llm_key_resolved_when_enabled(env, monkeypatch):
    monkeypatch.delenv("LK_TEST_KEY_JUDGE")
    body = CLASSIFY_BODY.replace("enabled = true", 'enabled = true\nllm = "judge"')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '环境变量 "LK_TEST_KEY_JUDGE" 未设置或为空')


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
    has(errors, "[llm.novision].supports_vision: UI 模态被 classify 阶段引用")


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
    assert "不会生效" in err


# ── v1.7: [class.*] whitelist + per-class merge (R6/R7/R25) ────────────────


def test_class_unknown_name_rejected(env):
    body = CLASSIFY_BODY + "\n[class.ghost.quality]\nthreshold = 0.5\n"
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[class.ghost]: 类名 "ghost" 不在 [[classify.classes]] 中，'
                "可用：writing、qa、other")


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
    has(errors, "[class.qa.dedup]: [class.*] 覆盖节不在白名单内"
                "（可用：quality、rubric、annotate、generate、verify、extract）")
    # key outside a section's whitelist → error
    has(errors, "[class.qa.quality].llm: [class.*.quality] 不可覆盖该键"
                "（白名单：mode、rounds、rubric、threshold、selection、top_ratio）")
    has(errors, "[class.qa.generate].num_per_call: [class.*.generate] 不可覆盖该键")
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
    has(errors, "[class.qa.quality].threshold: 与 quality.top_ratio 互斥")


def test_class_selection_top_ratio_required_on_merged_view(env):
    # the class asks for top_ratio selection but provides no ratio — the global
    # pair keys were dropped by the group takeover, so this is incomplete
    body = ("[quality]\ntop_ratio = 0.5\nselection = \"top_ratio\"\n\n" + CLASSIFY_BODY
            + '\n[class.qa.quality]\nselection = "top_ratio"\n')
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[class.qa.quality].top_ratio: selection = "top_ratio" 时必填')


def test_class_top_ratio_noop_warns(env, capsys):
    body = CLASSIFY_BODY + "\n[class.qa.quality]\ntop_ratio = 0.5\n"
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.class_views["qa"].quality.top_ratio == 0.5
    err = capsys.readouterr().err
    assert "[class.qa.quality].top_ratio" in err and "不会生效" in err


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
    has(errors, "[[class.qa.rubric.criteria]][1].pointwise_levels: "
                "pointwise 模式要求恰好 6 级（0–5），得到 3 级")


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
    assert "[[class.qa.rubric.criteria]]" in err and "内联 rubric 未生效" in err


def test_class_inline_selector_without_table_errors(env):
    body = CLASSIFY_BODY + '\n[class.qa.quality]\nrubric = "inline"\n'
    errors = env.errors(project_text=env.project(body=body))
    has(errors, '[class.qa.quality].rubric: rubric = "inline" 但未提供 '
                "[[class.qa.rubric.criteria]]")


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
    has(errors, "[[class.qa.annotate.examples]][1].output: 未通过用户 Schema")


def test_class_annotate_examples_dryrun_through_validator_hook(env):
    body = CLASSIFY_BODY + """
[output]
validator = "tests.hook_samples:topic_max6"
schema_inline = '''
""" + SCHEMA + """
'''

[class.qa.annotate]
examples = [{input = "问", output = {intent = "qa", topic = "这是一个特别长的主题短语"}}]
"""
    errors = env.errors(project_text=env.project(body=body, include_output=False))
    has(errors, "[[class.qa.annotate.examples]][1].output: 未通过 output.validator 回调")


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
    has(errors, "[class.qa.annotate].instruction: 期望非空字符串")


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
    has(errors, '[[class.qa.generate.styles]][2].name: 表内 name 须唯一，得到重复的 "dup"')
    has(errors, "[[class.qa.generate.styles]][2].prompt: 期望非空字符串")


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
    assert cfg.segment.use_vision is False
    assert cfg.segment.context == ""
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
use_vision = false
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
    has(errors, '[segment].strategy: 期望 "rules" | "llm" | "hybrid"，得到 "auto"')
    errors = env.errors(project_text=env.project(body='[segment]\non_error = "skip"'))
    has(errors, '[segment].on_error: 期望 "keep" | "fail"，得到 "skip"')
    errors = env.errors(project_text=env.project(body='[stream]\non_disorder = "drop"'))
    has(errors, '[stream].on_disorder: 期望 "skip" | "fail"，得到 "drop"')
    errors = env.errors(project_text=env.project(body='[extract]\non_error = "keep"'))
    has(errors, '[extract].on_error: 期望 "fallback" | "fail"，得到 "keep"')


def test_stream_trace_channels_accepted(env):
    body = '[trace]\nenabled = true\nchannels = ["segment", "extract", "quality"]'
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.trace.channels == ("segment", "extract", "quality")


# ── v1.8 §3.6: stage-combination constraints ────────────────────────────────


def test_segment_requires_process_mode(env):
    project = env.project(input_path=None, run_extra='mode = "generate_only"',
                          body=GEN_BODY + "standalone_count = 10\n\n" + SEG_ON)
    errors = env.errors(project_text=project)
    has(errors, '[segment].enabled: segment.enabled = true 要求 run.mode = "process"')


def test_segment_generate_mutually_exclusive(env):
    errors = env.errors(project_text=env.project(body=GEN_BODY + "\n" + SEG_ON))
    has(errors, "[segment].enabled: segment.enabled = true 与 generate.enabled = true 互斥")


def test_segment_requires_annotate(env):
    errors = env.errors(project_text=env.project(
        annotate_body="enabled = false", body=SEG_ON))
    has(errors, "[segment].enabled: segment.enabled = true 要求 annotate.enabled = true")


def test_segment_happy_path_loads(env):
    cfg = env.load(project_text=env.project(body=SEG_ON))
    assert cfg.segment.enabled is True


def test_extract_requires_segment_and_ui_modality(env):
    errors = env.errors(project_text=env.project(body="[extract]\nenabled = true"))
    has(errors, "[extract].enabled: extract.enabled = true 要求 segment.enabled = true")
    has(errors, '[extract].enabled: extract.enabled = true 要求 run.modality = "ui"')


def test_extract_happy_on_ui_stream(env):
    body = SEG_ON + "\n[extract]\nenabled = true"
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    cfg = env.load(project_text=project)
    assert cfg.extract.enabled is True


def test_stream_order_by_domain(env):
    errors = env.errors(project_text=env.project(body='[stream]\norder_by = "timestamp"'))
    has(errors, '[stream].order_by: 期望 "input_order" | "meta:<field>"，得到 "timestamp"')
    errors = env.errors(project_text=env.project(body='[stream]\norder_by = "meta:"'))
    has(errors, '[stream].order_by: 期望 "input_order" | "meta:<field>"，得到 "meta:"')


def test_stream_meta_order_text_only(env):
    project = env.project(input_path=env.input_dir, modality="ui",
                          body='[stream]\norder_by = "meta:ts"')
    errors = env.errors(project_text=project)
    has(errors, '[stream].order_by: "meta:<field>" 仅文本模态可用')


def test_stream_meta_order_ok_on_text(env):
    cfg = env.load(project_text=env.project(body='[stream]\norder_by = "meta:ts"'))
    assert cfg.stream.order_by == "meta:ts"


def test_session_max_span_requires_meta_order(env):
    errors = env.errors(project_text=env.project(
        body="[stream]\nsession_max_span_s = 60"))
    has(errors, '[stream].session_max_span_s: > 0 要求 order_by = "meta:<field>"')
    cfg = env.load(project_text=env.project(
        body='[stream]\norder_by = "meta:ts"\nsession_max_span_s = 60'))
    assert cfg.stream.session_max_span_s == 60


def test_gap_s_explicit_without_meta_warns_not_errors(env, capsys):
    cfg = env.load(project_text=env.project(body="[stream]\ngap_s = 60"))
    assert cfg.stream.gap_s == 60                 # loads — a warning, not an error
    err = capsys.readouterr().err
    assert "warning:" in err
    assert "[stream].gap_s" in err and "不会生效" in err


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
    has(errors, '[stream].key[1]: 期望 "meta:<field>"（仅文本）| "source_dir"，得到 "device"')


def test_stream_key_meta_text_only(env):
    project = env.project(input_path=env.input_dir, modality="ui",
                          body='[stream]\nkey = ["source_dir", "meta:device"]')
    errors = env.errors(project_text=project)
    has(errors, '[stream].key[2]: "meta:<field>" 分区键仅文本模态可用')
    assert not any(".key[1]" in e for e in errors)   # source_dir legal on UI


def test_segment_window_minimum(env):
    errors = env.errors(project_text=env.project(body="[segment]\nwindow = 1"))
    has(errors, "[segment].window: 期望 ≥ 2 的整数")
    cfg = env.load(project_text=env.project(body="[segment]\nwindow = 2"))
    assert cfg.segment.window == 2


@pytest.mark.parametrize("value", [1, 101])
def test_sequence_frames_range_rejected(env, value):
    errors = env.errors(project_text=env.project(
        annotate_body=f'instruction = "标注"\nsequence_frames = {value}'))
    has(errors, f"[annotate].sequence_frames: 期望 [2, 100] 内的整数，得到 {value}")


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
    assert "[stream].session_max_len" in err and "硬切" in err


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
    assert "不会生效" in err


def test_segment_enabled_false_alone_no_parked_warning(env, capsys):
    env.load(project_text=env.project(body="[segment]\nenabled = false"))
    assert "不会生效" not in capsys.readouterr().err


def test_rules_strategy_noise_filter_noop_warns(env, capsys):
    cfg = env.load(project_text=env.project(
        body=SEG_ON + 'strategy = "rules"'))
    assert cfg.segment.strategy == "rules"
    err = capsys.readouterr().err
    assert "[segment].noise_filter" in err and "不生效" in err


def test_hybrid_strategy_no_noise_filter_warning(env, capsys):
    env.load(project_text=env.project(body=SEG_ON))
    assert "[segment].noise_filter" not in capsys.readouterr().err


def test_sequence_frames_noop_without_stream_warns(env, capsys):
    cfg = env.load(project_text=env.project(
        annotate_body='instruction = "标注"\nsequence_frames = 10'))
    assert cfg.annotate.sequence_frames == 10
    err = capsys.readouterr().err
    assert "[annotate].sequence_frames" in err and "不会生效" in err


def test_stream_quality_without_extract_hints_frame_digest_scoring(env, capsys):
    env.load(project_text=env.project(body=SEG_ON))
    assert "帧摘要" in capsys.readouterr().err


def test_stream_quality_with_extract_no_hint(env, capsys):
    body = SEG_ON + "\n[extract]\nenabled = true"
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    env.load(project_text=project)
    assert "帧摘要" not in capsys.readouterr().err


def test_stream_explicit_non_trajectory_rubric_no_hint(env, capsys):
    # S29 advisory fires only when the EFFECTIVE rubric is default:trajectory —
    # an explicit default:text choice scores by its own criteria and must not
    # be told it is doing trajectory scoring.
    body = SEG_ON + '\n[quality]\nrubric = "default:text"'
    env.load(project_text=env.project(body=body))
    assert "帧摘要" not in capsys.readouterr().err


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
    has(errors, '[segment].llm: 引用的 profile "ghost" 不存在于 config.toml [llm.*]')
    # rules strategy makes zero LLM calls — the same reference is inert
    cfg = env.load(project_text=env.project(
        body=SEG_ON + 'strategy = "rules"\nllm = "ghost"'))
    assert cfg.segment.llm == "ghost"


def test_segment_llm_key_required_only_for_llm_strategies(env, monkeypatch):
    monkeypatch.delenv("LK_TEST_KEY_JUDGE")
    errors = env.errors(project_text=env.project(body=SEG_ON + 'llm = "judge"'))
    has(errors, '环境变量 "LK_TEST_KEY_JUDGE" 未设置或为空')
    cfg = env.load(project_text=env.project(
        body=SEG_ON + 'strategy = "rules"\nllm = "judge"'))
    assert cfg.llm_profiles["judge"].api_key == ""    # unreferenced, key not resolved


def test_segment_llm_not_referenced_when_disabled(env, monkeypatch):
    monkeypatch.delenv("LK_TEST_KEY_JUDGE")
    cfg = env.load(project_text=env.project(body='[segment]\nllm = "judge"'))
    assert cfg.llm_profiles["judge"].api_key == ""


def test_extract_llm_existence_and_key_when_enabled(env, monkeypatch):
    monkeypatch.delenv("LK_TEST_KEY_JUDGE")
    body = SEG_ON + 'strategy = "rules"\n\n[extract]\nenabled = true\nllm = "judge"'
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    errors = env.errors(project_text=project)
    has(errors, '环境变量 "LK_TEST_KEY_JUDGE" 未设置或为空')


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
    has(errors, "[llm.novision].supports_vision: UI 模态被 extract 阶段引用")


def test_segment_llm_needs_vision_only_when_use_vision(env):
    body = SEG_ON + 'llm = "novision"\nuse_vision = true'
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    errors = env.errors(config_text=BASE_CONFIG + NOVISION_PROFILE,
                        project_text=project)
    has(errors, "[llm.novision].supports_vision: UI 模态被 segment 阶段引用")
    # use_vision = false (default): pure-text window calls, no vision demand
    body = SEG_ON + 'llm = "novision"'
    project = env.project(input_path=env.input_dir, modality="ui", body=body)
    cfg = env.load(config_text=BASE_CONFIG + NOVISION_PROFILE, project_text=project)
    assert cfg.segment.llm == "novision"


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
    has(errors, "[llm.novision].supports_vision: UI 模态被 quality 阶段引用")


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
    has(errors, "[class.qa.extract].llm: [class.*.extract] 不可覆盖该键"
                "（白名单：instruction）")
    has(errors, "[class.qa.extract].enabled: [class.*.extract] 不可覆盖该键")

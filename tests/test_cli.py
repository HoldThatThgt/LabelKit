"""Offline unit tests for labelkit/cli.py.

Pure-logic coverage: exit-code mapping, argparse surface, rubric printing,
referenced-profile collection. Tests that exercise the `validate` / `run`
subcommands end-to-end need the M1 loader (and, for `run`, the whole module
graph); they auto-skip via importorskip until those modules land — no mock
LLMs, and no LLM is ever reached (both scenarios fail before any call).
"""
from __future__ import annotations

import tomllib
from importlib import resources
from pathlib import Path

import pytest

from labelkit import cli
from labelkit.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ClassSpec,
    Criterion,
    DedupConfig,
    EmbeddingProfile,
    GenerateConfig,
    InputConfig,
    LLMProfile,
    OutputConfig,
    QualityConfig,
    ResolvedConfig,
    Rubric,
    RunConfig,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)
from labelkit.errors import (
    EXIT_CONFIG,
    EXIT_FATAL,
    EXIT_INPUT,
    EXIT_OK,
    EXIT_STRICT,
    CircuitBreakerTripped,
    ConfigError,
    InputError,
    InternalError,
    LabelKitError,
    ProviderFatalError,
    ProviderRetryableError,
    SchemaViolation,
)

# ── exit-code mapper (spec §2.4) ───────────────────────────────────────────


@pytest.mark.parametrize(
    "exc,expected",
    [
        (ConfigError(["config.toml:[tool].log_level: bad"]), EXIT_CONFIG),
        (InputError("input path missing"), EXIT_INPUT),
        (ProviderFatalError("401", profile="default", status_code=401), EXIT_FATAL),
        (CircuitBreakerTripped("breaker open"), EXIT_FATAL),
        (LabelKitError("report write failed"), EXIT_STRICT),
        (LabelKitError("output path unwritable"), EXIT_FATAL),
        (ProviderRetryableError("timeout", profile="default", retries=5), EXIT_FATAL),
        (SchemaViolation(["/x: bad"], raw_last_output="{}"), EXIT_FATAL),
        (InternalError("invariant broken"), EXIT_FATAL),
        (RuntimeError("unexpected"), EXIT_FATAL),
    ],
)
def test_exit_code_for(exc, expected):
    assert cli.exit_code_for(exc) == expected


def test_config_error_aggregation_printed(capsys):
    exc = ConfigError(["a: err1", "b: err2"])
    cli._print_exception(exc)
    err = capsys.readouterr().err
    assert "ConfigError: 2 个配置错误" in err
    assert "a: err1" in err and "b: err2" in err


# ── argparse surface ───────────────────────────────────────────────────────


def test_parser_run_flags():
    args = cli.build_parser().parse_args(
        ["run", "--config", "c.toml", "--project", "p.toml", "--input", "in",
         "--output", "out", "--limit", "7", "--dry-run", "--strict",
         "--log-level", "debug"]
    )
    ov = cli._overrides_from_args(args)
    assert (ov.input, ov.output, ov.limit) == ("in", "out", 7)
    assert ov.dry_run is True and ov.strict is True and ov.log_level == "debug"


def test_parser_run_defaults():
    args = cli.build_parser().parse_args(
        ["run", "--config", "c.toml", "--project", "p.toml"])
    ov = cli._overrides_from_args(args)
    assert ov == type(ov)()  # all-default CliOverrides


def test_parser_requires_subcommand_and_flags():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["run", "--config", "c.toml"])  # missing --project
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["rubric", "--show", "default:nope"])


# ── --limit validation (spec 3.1.2 CLI dict / 3.1.5 no runtime config errors) ─


@pytest.mark.parametrize("bad", ["0", "-1", "-100", "abc", "1.5"])
def test_limit_rejects_non_positive_or_non_int(bad, capsys):
    """Zero/negative/non-integer --limit is a usage error → exit 2 (spec §2.4),
    never a runtime ValueError inside the orchestrator (spec 3.1.5)."""
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(
            ["run", "--config", "c.toml", "--project", "p.toml", "--limit", bad])
    assert excinfo.value.code == EXIT_CONFIG
    assert "期望 ≥ 1 的整数" in capsys.readouterr().err


def test_limit_rejected_via_main_exits_2(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["run", "--config", "c.toml", "--project", "p.toml", "--limit", "-1"])
    assert excinfo.value.code == EXIT_CONFIG


@pytest.mark.parametrize("good,expected", [("1", 1), ("7", 7), ("100", 100)])
def test_limit_accepts_positive_int(good, expected):
    args = cli.build_parser().parse_args(
        ["run", "--config", "c.toml", "--project", "p.toml", "--limit", good])
    assert cli._overrides_from_args(args).limit == expected


# ── rubric subcommand ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,filename",
    [("default:text", "default_text.toml"), ("default:ui", "default_ui.toml")],
)
def test_rubric_show_prints_packaged_toml_verbatim(capsys, name, filename):
    rc = cli.main(["rubric", "--show", name])
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    expected = (
        resources.files("labelkit")
        .joinpath("data", "rubrics", filename)
        .read_text(encoding="utf-8")
    )
    assert out == expected  # byte-for-byte verbatim
    parsed = tomllib.loads(out)  # must be valid TOML
    assert parsed["name"]
    assert isinstance(parsed["criteria"], list) and parsed["criteria"]
    for crit in parsed["criteria"]:
        assert crit["key"] and crit["pairwise_prompt"]


def test_rubric_without_show_lists_names(capsys):
    rc = cli.main(["rubric"])
    out = capsys.readouterr().out.splitlines()
    assert rc == EXIT_OK
    assert set(out) == {"default:text", "default:ui"}


# ── referenced_profiles (validate --probe helper) ──────────────────────────


def _profile(name: str) -> LLMProfile:
    return LLMProfile(name=name, provider="anthropic", base_url="https://x",
                      model="m", api_key_env="K")


def _cfg(**kw) -> ResolvedConfig:
    base = dict(
        tool=ToolConfig(),
        llm_profiles={n: _profile(n) for n in ("default", "judge", "fixer")},
        embedding_profiles={"emb": EmbeddingProfile(
            name="emb", base_url="https://x", model="e", api_key_env="K")},
        run=RunConfig(output="out.jsonl", modality="text", input="in.jsonl"),
        input=InputConfig(),
        dedup=DedupConfig(),
        classify=ClassifyConfig(),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(instruction="标注"),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=TraceConfig(),
        rubric=Rubric(name="r", criteria=(
            Criterion(key="c1", description="d", pairwise_prompt="p"),)),
        class_views={},
        user_schema={"type": "object"},
        limit=None, strict=False, dry_run=False,
        config_path="config.toml", project_path="project.toml",
        config_digest="sha256:0", project_digest="sha256:0",
    )
    base.update(kw)
    return ResolvedConfig(**base)


def test_referenced_profiles_default():
    llms, embs = cli.referenced_profiles(_cfg())
    assert llms == ["default"]  # quality + annotate both use "default"; dedup off
    assert embs == []


def test_referenced_profiles_all_stages():
    cfg = _cfg(
        quality=QualityConfig(judges=("default", "judge", "fixer")),
        generate=GenerateConfig(enabled=True, instruction="生成",
                                llms=("default", "judge")),
        verify=VerifyConfig(enabled=True, llm="judge"),
        output=OutputConfig(schema_inline="{}", repair_llm="fixer"),
        dedup=DedupConfig(semantic=True, semantic_embedding="emb"),
    )
    llms, embs = cli.referenced_profiles(cfg)
    assert llms == ["default", "judge", "fixer"]  # order-preserving, deduplicated
    assert embs == ["emb"]


def test_referenced_profiles_disabled_stages_not_probed():
    cfg = _cfg(
        quality=QualityConfig(enabled=False),
        annotate=AnnotateConfig(enabled=True, llm="judge", instruction="标注"),
        verify=VerifyConfig(enabled=False, llm="fixer"),  # disabled → not referenced
    )
    llms, embs = cli.referenced_profiles(cfg)
    assert llms == ["judge"]
    assert embs == []


def test_referenced_profiles_pairwise_judges_replace_quality_llm():
    """In pairwise mode a non-empty judges panel *replaces* quality.llm
    (spec 3.1.4 API-Key row, 3.1.6 example ①; M1 loader rule 12): the run
    never calls quality.llm, so `validate --probe` must not probe it."""
    cfg = _cfg(
        quality=QualityConfig(mode="pairwise", llm="fixer", judges=("judge",)),
        annotate=AnnotateConfig(enabled=False, instruction="标注"),
    )
    llms, _ = cli.referenced_profiles(cfg)
    assert llms == ["judge"]  # quality.llm ("fixer") is NOT referenced


def test_referenced_profiles_pointwise_ignores_judges():
    """In pointwise mode every scoring call uses quality.llm and the judges
    panel is never consulted (spec §3.4.4: judges are defined over pairwise
    comparisons), so only quality.llm is referenced/probed."""
    cfg = _cfg(
        quality=QualityConfig(mode="pointwise", llm="default",
                              judges=("judge", "fixer", "judge")),
        annotate=AnnotateConfig(enabled=False, instruction="标注"),
    )
    llms, _ = cli.referenced_profiles(cfg)
    assert llms == ["default"]  # judges are NOT referenced in pointwise mode


def test_referenced_profiles_verify_judges_replace_verify_llm():
    cfg = _cfg(
        quality=QualityConfig(enabled=False),
        annotate=AnnotateConfig(enabled=True, llm="default", instruction="标注"),
        verify=VerifyConfig(enabled=True, llm="fixer", judges=("judge",)),
    )
    llms, _ = cli.referenced_profiles(cfg)
    assert llms == ["default", "judge"]  # verify.llm ("fixer") is NOT referenced


# ── v1.7 classify (R24 reference-set point ③ + chain position) ──────────────


def _classify_cfg(llm: str = "default") -> ClassifyConfig:
    return ClassifyConfig(
        enabled=True, llm=llm, max_labels=2, fallback_class="other",
        classes=(ClassSpec(name="qa", description="问答"),
                 ClassSpec(name="other", description="其余")),
    )


def test_referenced_profiles_classify_enabled():
    cfg = _cfg(classify=_classify_cfg(llm="judge"))
    llms, _ = cli.referenced_profiles(cfg)
    # classify precedes quality/annotate (chain order); "default" deduplicated
    assert llms == ["judge", "default"]


def test_referenced_profiles_classify_disabled_not_referenced():
    # Loader rule-12 semantics: the guard is `classify.enabled`, so a disabled
    # stage's profile reference ("fixer") is never keyed nor probed.
    cfg = _cfg(classify=ClassifyConfig(enabled=False, llm="fixer"))
    llms, _ = cli.referenced_profiles(cfg)
    assert llms == ["default"]


def test_build_stages_inserts_classify_after_dedup_before_quality():
    from labelkit.classify import ClassifyStage

    cfg = _cfg(classify=_classify_cfg())
    stages = cli._build_stages(cfg)
    assert [s.name for s in stages] == ["dedup", "classify", "quality", "annotate"]
    assert isinstance(stages[1], ClassifyStage)


def test_build_stages_without_classify_unchanged():
    stages = cli._build_stages(_cfg())
    assert [s.name for s in stages] == ["dedup", "quality", "annotate"]


# ── validate / run end-to-end (skip until sibling modules land) ────────────

_VALID_CONFIG = """\
schema_version = 1

[tool]
log_level = "info"

[llm.default]
provider = "anthropic"
base_url = "https://api.z.ai/api/anthropic"
model = "glm-5.2"
api_key_env = "LABELKIT_CLI_TEST_KEY"
"""

_SCHEMA_INLINE = (
    '{"type": "object", "properties": {"intent": {"type": "string"}}, '
    '"required": ["intent"], "additionalProperties": false}'
)


def _project_toml(input_path: str, output_path: str) -> str:
    # annotate off / quality on satisfies stage-combination rule ① while
    # guaranteeing the run dies in M2 (missing input) before any LLM call.
    return f"""\
schema_version = 1

[run]
input = {input_path!r}
output = {output_path!r}
modality = "text"

[quality]
enabled = true
llm = "default"

[annotate]
enabled = false

[output]
schema_inline = '''{_SCHEMA_INLINE}'''
"""


def test_validate_broken_toml_pair_exits_2(tmp_path, capsys, monkeypatch):
    pytest.importorskip("labelkit.config.loader")
    monkeypatch.setenv("LABELKIT_CLI_TEST_KEY", "test-key")
    config = tmp_path / "config.toml"
    project = tmp_path / "project.toml"
    config.write_text(_VALID_CONFIG, encoding="utf-8")
    # Three aggregated errors: nonexistent profile reference, reserved _meta
    # key in the user schema, invalid rubric criterion key.
    project.write_text(
        f"""\
schema_version = 1

[run]
input = {str(tmp_path / 'in.jsonl')!r}
output = {str(tmp_path / 'out' / 'o.jsonl')!r}
modality = "text"

[quality]
enabled = true
llm = "nonexistent_profile"
rubric = "inline"

[[rubric.criteria]]
key = "Bad-Key"
weight = 1.0
description = "d"
pairwise_prompt = "p"

[annotate]
enabled = false

[output]
schema_inline = '''{{"type": "object", "properties": {{"_meta": {{"type": "object"}}}}}}'''
""",
        encoding="utf-8",
    )
    rc = cli.main(["validate", "--config", str(config), "--project", str(project)])
    err = capsys.readouterr().err
    assert rc == EXIT_CONFIG
    assert "ConfigError" in err
    # aggregated (not first-error-only) feedback
    assert "nonexistent_profile" in err
    assert "_meta" in err


def test_validate_missing_config_file_exits_2(tmp_path, capsys):
    pytest.importorskip("labelkit.config.loader")
    rc = cli.main([
        "validate",
        "--config", str(tmp_path / "nope-config.toml"),
        "--project", str(tmp_path / "nope-project.toml"),
    ])
    assert rc == EXIT_CONFIG
    assert capsys.readouterr().err  # some error text on stderr


def test_run_nonexistent_input_exits_3(tmp_path, capsys, monkeypatch):
    for mod in ("labelkit.config.loader", "labelkit.obslog", "labelkit.llm_client",
                "labelkit.schema_engine", "labelkit.ingest", "labelkit.dedup",
                "labelkit.quality", "labelkit.emitter", "labelkit.orchestrator"):
        pytest.importorskip(mod)
    monkeypatch.setenv("LABELKIT_CLI_TEST_KEY", "test-key")
    config = tmp_path / "config.toml"
    project = tmp_path / "project.toml"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    config.write_text(_VALID_CONFIG, encoding="utf-8")
    project.write_text(
        _project_toml(str(tmp_path / "does-not-exist.jsonl"),
                      str(out_dir / "o.jsonl")),
        encoding="utf-8",
    )
    rc = cli.main(["run", "--config", str(config), "--project", str(project)])
    # Missing input is an input error (spec §2.4 exit 3, process mode); the run
    # must die in M1 path checks / M2 scan, long before any LLM call.
    assert rc == EXIT_INPUT
    assert capsys.readouterr().err


# ── P2-4: the previous run's trace survives bad-input runs and dry runs ──────

_TRACE_PROJECT = """\
schema_version = 1

[run]
input = {input_path!r}
output = {output_path!r}
modality = "text"

[quality]
enabled = true
llm = "default"

[annotate]
enabled = false

[trace]
enabled = true

[output]
schema_inline = '''{schema}'''
"""


def _write_pair(tmp_path, input_path: str) -> tuple:
    config = tmp_path / "config.toml"
    project = tmp_path / "project.toml"
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    config.write_text(_VALID_CONFIG, encoding="utf-8")
    project.write_text(
        _TRACE_PROJECT.format(input_path=input_path,
                              output_path=str(out_dir / "o.jsonl"),
                              schema=_SCHEMA_INLINE),
        encoding="utf-8",
    )
    return config, project, out_dir


def test_bad_input_run_preserves_previous_trace(tmp_path, monkeypatch, capsys):
    for mod in ("labelkit.config.loader", "labelkit.obslog", "labelkit.llm_client",
                "labelkit.schema_engine", "labelkit.ingest", "labelkit.dedup",
                "labelkit.quality", "labelkit.emitter", "labelkit.orchestrator"):
        pytest.importorskip(mod)
    monkeypatch.setenv("LABELKIT_CLI_TEST_KEY", "test-key")
    config, project, out_dir = _write_pair(tmp_path, str(tmp_path / "missing.jsonl"))
    sentinel = out_dir / "o.trace.jsonl"
    sentinel.write_text("precious previous trace\n", encoding="utf-8")

    rc = cli.main(["run", "--config", str(config), "--project", str(project)])
    assert rc == EXIT_INPUT
    # The dead-on-arrival run must not have truncated the previous trace.
    assert sentinel.read_text(encoding="utf-8") == "precious previous trace\n"
    assert capsys.readouterr().err


def test_dry_run_diverts_trace_and_report(tmp_path, monkeypatch, capsys):
    for mod in ("labelkit.config.loader", "labelkit.obslog", "labelkit.llm_client",
                "labelkit.schema_engine", "labelkit.ingest", "labelkit.dedup",
                "labelkit.quality", "labelkit.emitter", "labelkit.orchestrator"):
        pytest.importorskip(mod)
    monkeypatch.setenv("LABELKIT_CLI_TEST_KEY", "test-key")
    data = tmp_path / "in.jsonl"
    data.write_text('{"text": "样例"}\n', encoding="utf-8")
    config, project, out_dir = _write_pair(tmp_path, str(data))
    trace_sentinel = out_dir / "o.trace.jsonl"
    trace_sentinel.write_text("precious previous trace\n", encoding="utf-8")
    report_sentinel = out_dir / "o.report.json"
    report_sentinel.write_text('{"precious": true}', encoding="utf-8")

    rc = cli.main(["run", "--config", str(config), "--project", str(project),
                   "--dry-run"])
    assert rc == 0
    # Rehearsal products live under .dryrun names; the real ledgers survive.
    assert trace_sentinel.read_text(encoding="utf-8") == "precious previous trace\n"
    assert report_sentinel.read_text(encoding="utf-8") == '{"precious": true}'
    assert (out_dir / "o.trace.dryrun.jsonl").exists()
    assert (out_dir / "o.dryrun.report.json").exists()
    assert "dry-run" in capsys.readouterr().err
